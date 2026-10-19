"""Session restore: the Qt-free model (docs/ARCHITECTURE.md §15.0 "Session restore"; AGENT_PROMPT §5
"oxirgi sessiyani tiklash: ochiq fayllar, joylashuv, ochilgan tugunlar, filtrlar").

A *session* is what the main window shows, per tab: the file, the forced format / record path, the two
panes' view kinds (with the active pane and whether the second pane is shown), the current node and the
expanded tree nodes as location texts (:meth:`DocSession.location_text`, read back by
:func:`jsonreshape.paths.parse_location`), the filter (expression text, language and the builder spec of
``FilterSpec.to_dict``), the table's quick filters, the sort keys and the search panel's inputs; plus the
active tab. Window geometry and dock state stay where they always were (``geometry`` / ``window_state`` of
the GUI state).

This module holds that model (:class:`TabState`, :class:`SessionState`) and everything about it that
needs no Qt:

* its JSON form -- ``gui_state["session"]`` (:data:`SESSION_KEY`, :func:`store_session` /
  :func:`load_session`) and, with the workspace extras (``relpath``, ``table_layout``, ``bookmarks``,
  ``mapping``, ``edits``), the tabs of a ``*.jrworkspace`` file (:mod:`jsonreshape.gui.workspace`);
* validation: :class:`FormatProblem` names the JSON pointer (RFC 6901) of the bad part. Workspace files
  are read strictly (the problem is reported); the GUI state leniently (a bad part is dropped and
  logged, a tab without a usable path is left out -- a damaged session never stops the start);
* the capture from a window (:func:`capture_window`, :func:`capture_tab`: duck typing on the main window,
  its tabs, sessions, tree views and search panel; no I/O, nothing imported from Qt);
* the restore plan (:func:`plan_restore`): which files exist (a workspace's ``relpath`` first, then
  ``path``); it checks the file system, so the GUI runs it in a background task;
* the ``restore_session`` setting of the GUI state (:data:`RESTORE_MODES`, :func:`restore_mode`).

The Qt part -- the autosave timer, the start-up decision, the "restore?" bar, applying a plan to the
window, workspace files -- is :mod:`jsonreshape.gui.workspace`.

Decisions (§15.0 leaves them open; the safest behaviour for the user's data was chosen):

* A tab showing search results (``DocSession.show_records``) keeps neither the found records nor the
  filter / sort applied on top of them (they described the found records, not the file); its search
  inputs are kept, so the search is one click away ("a search result view is not restored, the search
  is").
* Expanded nodes are taken from the tree view's loaded rows (no I/O while the window closes): a node
  whose row data is no longer loaded is left out -- except top-level records and the elements of
  expanded arrays, which are named without it. At most :data:`EXPANDED_MAX` per tab (parents first);
  location texts longer than :data:`LOCATION_MAX_CHARS` are left out.
* The session kept in the GUI state is bounded (:data:`SESSION_MAX_BYTES`): beyond it the longest
  lists of expanded nodes are shortened first, then the last tabs are left out (:func:`fit_session`).
* Keys this version does not know are ignored (a newer version may add some); the version number
  must be :data:`SESSION_VERSION`.
"""

from __future__ import annotations

import json
import logging
import os
import re
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Callable, Iterable

from jsonreshape.errors import JsonReshapeError

log = logging.getLogger(__name__)

__all__ = [
    "AUTOSAVE_MS",
    "BOOKMARKS_MAX",
    "DEFAULT_VIEWS",
    "EDITS_STATES",
    "EXPANDED_MAX",
    "FILTER_LANGS",
    "LOCATION_MAX_CHARS",
    "QUICK_FILTERS_MAX",
    "RESTORE_DEFAULT",
    "RESTORE_KEY",
    "RESTORE_MODES",
    "SEARCH_FIELDS",
    "SESSION_KEY",
    "SESSION_MAX_BYTES",
    "SESSION_VERSION",
    "SORT_KEYS_MAX",
    "TABS_MAX",
    "FormatProblem",
    "PlannedTab",
    "RestorePlan",
    "SessionState",
    "TabState",
    "capture_tab",
    "capture_window",
    "filter_dict",
    "fit_session",
    "json_pointer",
    "load_session",
    "location_text",
    "plan_restore",
    "relative_path",
    "resolve_tab_path",
    "restore_mode",
    "search_inputs",
    "session_document",
    "sort_key_dict",
    "sort_key_from_dict",
    "store_session",
]

SESSION_VERSION = 1
#: GUI state key of the saved session (a JSON object, see :meth:`SessionState.to_dict`).
SESSION_KEY = "session"
#: GUI state key of the start-up choice: ``always`` (default) | ``ask`` | ``never``.
RESTORE_KEY = "restore_session"
RESTORE_MODES = ("always", "ask", "never")
RESTORE_DEFAULT = "always"
#: The session is written to the GUI state this often while the window lives (crash safety).
AUTOSAVE_MS = 60_000
#: Expanded tree nodes kept per tab (parents first).
EXPANDED_MAX = 2000
#: Tabs kept in a session / read from a workspace.
TABS_MAX = 100
#: Longer location texts (very long keys) are not kept.
LOCATION_MAX_CHARS = 4096
SORT_KEYS_MAX = 64
QUICK_FILTERS_MAX = 1000
BOOKMARKS_MAX = 10_000
#: Upper bound of the session's JSON text in the GUI state (the state file refuses 16 MiB).
SESSION_MAX_BYTES = 2 << 20
#: The panes' views of a new tab (``doctab.DEFAULT_PANES``).
DEFAULT_VIEWS = ("tree", "text")
FILTER_LANGS = ("expr", "jmespath")
#: ``"edits"`` of a workspace tab: the file had unsaved changes when it was saved (informs only).
EDITS_STATES = ("none", "pending")
#: The search panel's inputs (``SearchPanel.input_values``) and their types.
SEARCH_FIELDS: dict[str, type] = {"text": str, "mode": str, "case_sensitive": bool, "whole_word": bool,
                                  "translit": bool, "scope": str, "path": str}
_KIND_RE = re.compile(r"[a-z][a-z0-9_]{0,31}\Z")
_TEXT_MAX = 1 << 20          # longest filter / quick filter / search text kept


# ==================================================================================
# problems
# ==================================================================================
class FormatProblem(ValueError):
    """A part of a session / workspace document is invalid: ``code`` (``missing`` | ``type`` |
    ``value`` | ``too_many``), ``pointer`` (RFC 6901 JSON pointer of the bad part) and ``params``
    (``expected`` -- a type name -- for ``type``; ``count`` / ``limit`` for ``too_many``; ``detail``
    for ``value``). :mod:`jsonreshape.gui.workspace` turns it into a localized
    ``workspace.error.<code>``."""

    def __init__(self, code: str, pointer: str, **params: Any) -> None:
        super().__init__(f"{code} at {pointer or '/'}: {params}")
        self.code = code
        self.pointer = pointer
        self.params = params


def json_pointer(*parts: Any) -> str:
    """The RFC 6901 pointer of ``parts`` (``~`` and ``/`` escaped): ``json_pointer("tabs", 0, "path")``
    -> ``/tabs/0/path``."""
    return "".join("/" + str(p).replace("~", "~0").replace("/", "~1") for p in parts)


def _child(pointer: str, part: Any) -> str:
    return pointer + json_pointer(part)


def _json_ok(value: Any) -> bool:
    try:
        json.dumps(value, ensure_ascii=False, allow_nan=False)
    except (TypeError, ValueError, RecursionError):
        return False
    return True


def _is_int(v: Any) -> bool:
    return isinstance(v, int) and not isinstance(v, bool)


# ==================================================================================
# field helpers (validation)
# ==================================================================================
def _text(v: Any, pointer: str, *, empty: bool = True, limit: int = _TEXT_MAX) -> str:
    if not isinstance(v, str):
        raise FormatProblem("type", pointer, expected="string")
    if not empty and not v.strip():
        raise FormatProblem("value", pointer, detail='""')
    if len(v) > limit:
        raise FormatProblem("value", pointer, detail=f"{len(v)} > {limit}")
    return v


def _opt_text(v: Any, pointer: str) -> str | None:
    return None if v is None else _text(v, pointer)


def _flag(v: Any, pointer: str) -> bool:
    if not isinstance(v, bool):
        raise FormatProblem("type", pointer, expected="bool")
    return v


def _kind(v: Any, pointer: str) -> str | None:
    if v is None:
        return None
    if not isinstance(v, str):
        raise FormatProblem("type", pointer, expected="string")
    if not _KIND_RE.match(v):
        raise FormatProblem("value", pointer, detail=_short(v))
    return v


def _short(v: Any) -> str:
    try:
        text = json.dumps(v, ensure_ascii=False)
    except (TypeError, ValueError, RecursionError):
        text = repr(v)
    return text if len(text) <= 60 else text[:59] + "\N{HORIZONTAL ELLIPSIS}"


def _location(v: Any, pointer: str) -> str:
    """A location text :func:`jsonreshape.paths.parse_location` reads."""
    from jsonreshape.paths import parse_location

    text = _text(v, pointer, empty=False, limit=LOCATION_MAX_CHARS)
    try:
        parse_location(text)
    except JsonReshapeError as exc:
        raise FormatProblem("value", pointer, detail=exc.message) from None
    return text


def _record_path(v: Any, pointer: str) -> str | list | None:
    """The record path a session was opened with: a path text or a list of keys / indices."""
    if v is None:
        return None
    if isinstance(v, str):
        from jsonreshape.paths import parse_path

        try:
            parse_path(v)
        except JsonReshapeError as exc:
            raise FormatProblem("value", pointer, detail=exc.message) from None
        return v
    if isinstance(v, list):
        for i, seg in enumerate(v):
            if not (isinstance(seg, str) or (_is_int(seg) and seg >= 0)):
                raise FormatProblem("value", _child(pointer, i), detail=_short(seg))
        return list(v)
    raise FormatProblem("type", pointer, expected="string_or_list")


def _views(v: Any, pointer: str) -> list[str | None]:
    if not isinstance(v, list):
        raise FormatProblem("type", pointer, expected="list")
    if len(v) > 2:
        raise FormatProblem("too_many", pointer, count=len(v), limit=2)
    out = [_kind(item, _child(pointer, i)) for i, item in enumerate(v)]
    return out + [None] * (2 - len(out))


def _expanded(v: Any, pointer: str, strict: bool) -> list[str]:
    if not isinstance(v, list):
        raise FormatProblem("type", pointer, expected="list")
    if len(v) > EXPANDED_MAX:
        if strict:
            raise FormatProblem("too_many", pointer, count=len(v), limit=EXPANDED_MAX)
        v = v[:EXPANDED_MAX]
    out: list[str] = []
    for i, item in enumerate(v):
        try:
            out.append(_location(item, _child(pointer, i)))
        except FormatProblem:
            if strict:
                raise
    return out


def _filter(v: Any, pointer: str) -> dict | None:
    """``{"text", "lang", "builder"}`` of a ``FilterSpec`` (``None``: no filter)."""
    if v is None:
        return None
    if not isinstance(v, dict):
        raise FormatProblem("type", pointer, expected="object")
    text = _text(v.get("text", ""), _child(pointer, "text"))
    lang = v.get("lang", "expr")
    if lang not in FILTER_LANGS:
        raise FormatProblem("value", _child(pointer, "lang"), detail=_short(lang))
    builder = v.get("builder")
    if builder is not None and not isinstance(builder, dict):
        raise FormatProblem("type", _child(pointer, "builder"), expected="object")
    if not text.strip() and not builder:
        return None
    return {"text": text, "lang": lang, "builder": builder}


def _quick(v: Any, pointer: str) -> dict[str, str]:
    if not isinstance(v, dict):
        raise FormatProblem("type", pointer, expected="object")
    if len(v) > QUICK_FILTERS_MAX:
        raise FormatProblem("too_many", pointer, count=len(v), limit=QUICK_FILTERS_MAX)
    out: dict[str, str] = {}
    for key, text in v.items():
        out[str(key)] = _text(text, _child(pointer, key))
    return {k: t_ for k, t_ in out.items() if t_.strip()}


def sort_key_dict(key: Any) -> dict[str, Any]:
    """The JSON form of a :class:`~jsonreshape.sortkeys.SortKey`."""
    return {"path": str(key.path), "descending": bool(key.descending), "nulls": str(key.nulls),
            "natural": bool(key.natural), "collation": str(key.collation),
            "case_sensitive": bool(key.case_sensitive)}


def sort_key_from_dict(data: Any) -> Any:
    """The :class:`~jsonreshape.sortkeys.SortKey` of :func:`sort_key_dict` (``UserInputError`` /
    ``PathSyntaxError`` / ``TypeError`` when invalid)."""
    from jsonreshape.sortkeys import SortKey

    if not isinstance(data, dict):
        raise TypeError("a sort key must be an object")
    kwargs = {name: data[name] for name in ("descending", "nulls", "natural", "collation", "case_sensitive")
              if name in data}
    return SortKey(data.get("path"), **kwargs)


def _sort(v: Any, pointer: str) -> list[dict]:
    if not isinstance(v, list):
        raise FormatProblem("type", pointer, expected="list")
    if len(v) > SORT_KEYS_MAX:
        raise FormatProblem("too_many", pointer, count=len(v), limit=SORT_KEYS_MAX)
    out = []
    for i, item in enumerate(v):
        ptr = _child(pointer, i)
        if not isinstance(item, dict):
            raise FormatProblem("type", ptr, expected="object")
        try:
            out.append(sort_key_dict(sort_key_from_dict(item)))
        except JsonReshapeError as exc:
            raise FormatProblem("value", ptr, detail=exc.message) from None
        except (TypeError, ValueError) as exc:
            raise FormatProblem("value", ptr, detail=str(exc)) from None
    return out


def _search(v: Any, pointer: str) -> dict | None:
    if v is None:
        return None
    if not isinstance(v, dict):
        raise FormatProblem("type", pointer, expected="object")
    out: dict[str, Any] = {}
    for name, typ in SEARCH_FIELDS.items():
        if name not in v or v[name] is None:
            continue
        value = v[name]
        if typ is bool:
            out[name] = _flag(value, _child(pointer, name))
        else:
            out[name] = _text(value, _child(pointer, name))
    return out if str(out.get("text") or "").strip() else None


def _bookmarks(v: Any, pointer: str) -> list[dict]:
    from jsonreshape.gui.guistate import Bookmark

    if not isinstance(v, list):
        raise FormatProblem("type", pointer, expected="list")
    if len(v) > BOOKMARKS_MAX:
        raise FormatProblem("too_many", pointer, count=len(v), limit=BOOKMARKS_MAX)
    out = []
    for i, item in enumerate(v):
        try:
            out.append(Bookmark.from_dict(item).to_dict())
        except (TypeError, ValueError) as exc:
            raise FormatProblem("value", _child(pointer, i), detail=str(exc)) from None
    return out


# ==================================================================================
# the model
# ==================================================================================
@dataclass
class TabState:
    """One tab of a session (see the module docstring). ``path`` is absolute; ``views`` holds the
    two panes' view kinds (``None``: the default of that pane); ``current`` / ``expanded`` are location
    texts; ``filter`` = ``FilterSpec.to_dict()``; ``sort`` = :func:`sort_key_dict` items; ``search`` =
    the search panel's inputs. The workspace extras (``relpath`` relative to the workspace file,
    ``table_layout`` -- the GUI state's layout object of the file --, ``bookmarks`` -- ``Bookmark.to_dict``
    items --, ``mapping`` -- an embedded mapping file --, ``edits``) are written only to workspace
    files."""

    path: str
    format: str | None = None
    record_path: str | list | None = None
    views: list[str | None] = field(default_factory=lambda: [None, None])
    active_pane: int = 0
    second_pane: bool = True
    current: str | None = None
    expanded: list[str] = field(default_factory=list)
    filter: dict | None = None
    quick_filters: dict[str, str] = field(default_factory=dict)
    sort: list[dict] = field(default_factory=list)
    search: dict | None = None
    # workspace extras
    relpath: str | None = None
    table_layout: dict | None = None
    bookmarks: list[dict] = field(default_factory=list)
    mapping: dict | None = None
    edits: str = "none"

    @property
    def name(self) -> str:
        """The file name (status lines)."""
        return os.path.basename(self.path) or self.path

    def panes(self) -> tuple[str, str]:
        """The two panes' view kinds with the defaults filled in."""
        views = list(self.views or []) + [None, None]
        return (views[0] or DEFAULT_VIEWS[0], views[1] or DEFAULT_VIEWS[1])

    def to_dict(self, *, workspace: bool = False) -> dict[str, Any]:
        out: dict[str, Any] = {
            "path": self.path,
            "format": self.format,
            "record_path": list(self.record_path) if isinstance(self.record_path, (list, tuple)) else self.record_path,
            "views": list((list(self.views or []) + [None, None])[:2]),
            "active_pane": int(self.active_pane),
            "second_pane": bool(self.second_pane),
            "current": self.current,
            "expanded": list(self.expanded),
            "filter": self.filter,
            "quick_filters": dict(self.quick_filters),
            "sort": list(self.sort),
            "search": self.search,
        }
        if workspace:
            out["relpath"] = self.relpath
            out["table_layout"] = self.table_layout
            out["bookmarks"] = list(self.bookmarks)
            out["mapping"] = self.mapping
            out["edits"] = self.edits if self.edits in EDITS_STATES else "none"
        return out

    @classmethod
    def from_dict(cls, data: Any, *, pointer: str = "", strict: bool = True, workspace: bool = False) -> TabState:
        """Validate one tab object. ``strict``: any bad part raises :class:`FormatProblem`; otherwise a bad
        optional part is dropped (logged) and only a missing / invalid ``path`` raises. ``workspace``:
        read the workspace extras too (an embedded ``mapping`` is only checked to be an object here --
        :mod:`jsonreshape.gui.workspace` validates it as a mapping file)."""
        if not isinstance(data, dict):
            raise FormatProblem("type", pointer, expected="object")
        if "path" not in data:
            raise FormatProblem("missing", _child(pointer, "path"))
        path = _text(data["path"], _child(pointer, "path"), empty=False, limit=32767)
        tab = cls(path=path)

        def take(name: str, parse: Callable[[Any, str], Any]) -> None:
            if name not in data:
                return
            ptr = _child(pointer, name)
            try:
                setattr(tab, name, parse(data[name], ptr))
            except FormatProblem as problem:
                if strict:
                    raise
                log.info("session: dropping %s of %s (%s)", problem.pointer, path, problem)

        take("format", _kind)
        take("record_path", _record_path)
        take("views", _views)
        take("active_pane", lambda v, ptr: _pane_index(v, ptr))
        take("second_pane", _flag)
        take("current", lambda v, ptr: None if v is None else _location(v, ptr))
        take("expanded", lambda v, ptr: _expanded(v, ptr, strict))
        take("filter", _filter)
        take("quick_filters", _quick)
        take("sort", _sort)
        take("search", _search)
        if workspace:
            take("relpath", _relpath)
            take("table_layout", _layout)
            take("bookmarks", _bookmarks)
            take("mapping", _mapping_object)
            take("edits", _edits)
        return tab


def _pane_index(v: Any, pointer: str) -> int:
    if not _is_int(v):
        raise FormatProblem("type", pointer, expected="int")
    if v not in (0, 1):
        raise FormatProblem("value", pointer, detail=str(v))
    return v


def _relpath(v: Any, pointer: str) -> str | None:
    if v is None:
        return None
    text = _text(v, pointer, empty=False, limit=32767)
    if os.path.isabs(text) or os.path.splitdrive(text)[0]:
        raise FormatProblem("value", pointer, detail=_short(text))
    return text


def _layout(v: Any, pointer: str) -> dict | None:
    if v is None:
        return None
    if not isinstance(v, dict):
        raise FormatProblem("type", pointer, expected="object")
    if not _json_ok(v):
        raise FormatProblem("value", pointer, detail="")
    return v


def _mapping_object(v: Any, pointer: str) -> dict | None:
    if v is None:
        return None
    if not isinstance(v, dict):
        raise FormatProblem("type", pointer, expected="object")
    return v


def _edits(v: Any, pointer: str) -> str:
    if v not in EDITS_STATES:
        raise FormatProblem("value", pointer, detail=_short(v))
    return v


@dataclass
class SessionState:
    """The tabs of a window and the active one (an index into ``tabs`` or ``None``)."""

    tabs: list[TabState] = field(default_factory=list)
    active: int | None = None

    def to_dict(self) -> dict[str, Any]:
        """The JSON object kept in ``gui_state["session"]`` (without the workspace extras)."""
        from jsonreshape import __version__

        active = self.active if self.active is not None and 0 <= self.active < len(self.tabs) else None
        return {"version": SESSION_VERSION, "app": __version__,
                "saved": datetime.now().astimezone().isoformat(timespec="seconds"),
                "tabs": [tab.to_dict() for tab in self.tabs], "active": active}

    @classmethod
    def from_dict(cls, data: Any) -> SessionState:
        """Read a saved session leniently: a tab that cannot be used is left out (logged), bad parts of
        a tab are dropped; :class:`FormatProblem` only when the whole object is unusable."""
        if not isinstance(data, dict):
            raise FormatProblem("type", "", expected="object")
        version = data.get("version")
        if not _is_int(version) or version != SESSION_VERSION:
            raise FormatProblem("value", "/version", detail=_short(version))
        items = data.get("tabs")
        if not isinstance(items, list):
            raise FormatProblem("type", "/tabs", expected="list")
        active_in = data.get("active")
        tabs: list[TabState] = []
        active: int | None = None
        for i, item in enumerate(items[:TABS_MAX]):
            try:
                tab = TabState.from_dict(item, pointer=json_pointer("tabs", i), strict=False)
            except FormatProblem as problem:
                log.info("session: leaving out tab %d (%s)", i, problem)
                continue
            if _is_int(active_in) and active_in == i:
                active = len(tabs)
            tabs.append(tab)
        return cls(tabs=tabs, active=active)


# ==================================================================================
# the GUI state
# ==================================================================================
def restore_mode(state: Any) -> str:
    """The ``restore_session`` setting of the GUI state (:data:`RESTORE_DEFAULT` when unset / unknown)."""
    try:
        value = state.get(RESTORE_KEY) if state is not None else None
    except Exception:  # pragma: no cover - a broken state object
        value = None
    return value if value in RESTORE_MODES else RESTORE_DEFAULT


def load_session(state: Any) -> SessionState | None:
    """The session saved in the GUI state (``None`` when there is none or it is unusable)."""
    data = state.get(SESSION_KEY) if state is not None else None
    if data is None:
        return None
    try:
        return SessionState.from_dict(data)
    except FormatProblem as problem:
        log.warning("the saved session is not usable (%s); it is ignored", problem)
        return None


def fit_session(data: dict[str, Any], limit: int = SESSION_MAX_BYTES) -> dict[str, Any]:
    """``data`` (:meth:`SessionState.to_dict`) within ``limit`` bytes of JSON: the longest lists of
    expanded nodes are halved first, then the last tabs are left out (the active one last)."""
    def size(value: Any) -> int:
        return len(json.dumps(value, ensure_ascii=False).encode("utf-8", "surrogatepass"))

    excess = size(data) - limit
    if excess <= 0:
        return data
    tabs = data.get("tabs") or []
    # the expanded nodes first: the longest list (in bytes) is halved until the excess is gone
    lists = [size(tab.get("expanded") or []) for tab in tabs]
    while excess > 0 and lists and max(lists) > 2:
        i = max(range(len(lists)), key=lists.__getitem__)
        items = tabs[i]["expanded"]
        tabs[i]["expanded"] = items[:len(items) // 2]
        now = size(tabs[i]["expanded"])
        excess -= lists[i] - now
        lists[i] = now
    if excess > 0:
        # then whole tabs from the end (the active one last)
        sizes = [size(tab) + 1 for tab in tabs]
        while tabs and excess > 0:
            active = data.get("active")
            drop = len(tabs) - 1
            if active == drop and drop > 0:
                drop -= 1
            excess -= sizes.pop(drop)
            del tabs[drop]
            if isinstance(active, int) and active > drop:
                data["active"] = active - 1
            elif active == drop:
                data["active"] = None
    if data.get("active") is not None and not 0 <= data["active"] < len(tabs):
        data["active"] = None
    return data


def session_document(session: SessionState, *, limit: int = SESSION_MAX_BYTES) -> dict[str, Any]:
    """The JSON object of ``session`` for the GUI state, within ``limit`` bytes (:func:`fit_session`)."""
    return fit_session(session.to_dict(), limit)


def _comparable(data: Any) -> Any:
    if isinstance(data, dict):
        return {k: v for k, v in data.items() if k != "saved"}
    return data


def store_session(state: Any, session: SessionState) -> bool:
    """Put ``session`` into the GUI state (not saved to disk here: ``AppContext.save_state``). False --
    nothing changed -- when the stored one is the same apart from its time."""
    data = session_document(session)
    old = state.get(SESSION_KEY)
    if _comparable(old) == _comparable(data):
        return False
    state.set(SESSION_KEY, data)
    return True


# ==================================================================================
# capture (duck typing: main window, document tabs, sessions, views, the search panel)
# ==================================================================================
def location_text(session: Any, ref: Any) -> str | None:
    """``session.location_text(ref)`` when it is short enough to keep (``None`` otherwise)."""
    if ref is None or session is None:
        return None
    try:
        text = session.location_text(ref)
    except Exception:  # noqa: BLE001 - a ref of a closed / other document
        log.debug("no location text for %r", ref, exc_info=True)
        return None
    return text if isinstance(text, str) and text and len(text) <= LOCATION_MAX_CHARS else None


def filter_dict(spec: Any) -> dict | None:
    """The JSON form of a ``FilterSpec`` (``None`` for no / an empty filter). A builder spec that is not
    plain JSON is dropped (the text is authoritative; it is rendered from the builder when empty)."""
    if spec is None or bool(getattr(spec, "empty", False)):
        return None
    text = str(getattr(spec, "text", "") or "")
    lang = getattr(spec, "lang", "expr")
    builder = getattr(spec, "builder", None)
    if not isinstance(builder, dict) or not _json_ok(builder):
        if builder is not None and not text.strip():
            try:
                text = str(spec.expr_text())
            except Exception:  # noqa: BLE001 - an invalid builder: nothing to keep
                text = ""
        builder = None
    if lang not in FILTER_LANGS or len(text) > _TEXT_MAX or (not text.strip() and builder is None):
        return None
    return {"text": text, "lang": lang, "builder": json.loads(json.dumps(builder)) if builder else None}


def search_inputs(panel: Any, session: Any) -> dict | None:
    """The search panel's inputs for ``session``: shown now (``panel.input_values()``) when it is the
    panel's session, else the ones remembered for it (``panel.state(session).inputs`` or the options of
    its last search). ``None`` without a query text."""
    if panel is None or session is None:
        return None
    current = getattr(panel, "session", None)
    try:
        current = current() if callable(current) else current
    except Exception:  # noqa: BLE001 - a panel being torn down
        current = None
    data: Any = None
    try:
        if current is session and callable(getattr(panel, "input_values", None)):
            data = panel.input_values()
        else:
            state_fn = getattr(panel, "state", None)
            st = state_fn(session) if callable(state_fn) else None
            data = getattr(st, "inputs", None) if st is not None else None
            opts = getattr(st, "opts", None) if st is not None else None
            if data is None and opts is not None and callable(getattr(opts, "to_dict", None)):
                data = opts.to_dict()
    except Exception:  # noqa: BLE001 - the inputs are a convenience
        log.debug("could not read the search inputs", exc_info=True)
        return None
    if not isinstance(data, dict):
        return None
    try:
        return _search(data, "")
    except FormatProblem:
        return None


def _tree_expanded(tab: Any, limit: int) -> list[str]:
    views = getattr(tab, "views", None)
    try:
        trees = list(views("tree")) if callable(views) else []
    except Exception:  # noqa: BLE001
        trees = []
    for view in trees:
        fn = getattr(view, "expanded_locations", None)
        if not callable(fn):
            continue
        try:
            found = list(fn(limit))
        except Exception:  # noqa: BLE001 - a view being torn down: nothing to keep
            log.debug("expanded_locations failed", exc_info=True)
            continue
        return [x for x in found if isinstance(x, str) and x and len(x) <= LOCATION_MAX_CHARS][:limit]
    return []


def _record_path_json(value: Any) -> str | list | None:
    if value is None:
        return None
    if isinstance(value, str):
        return value
    if isinstance(value, (list, tuple)) and all(isinstance(s, str) or (_is_int(s) and s >= 0) for s in value):
        return list(value)
    return None


def capture_tab(tab: Any, *, search_panel: Any = None, expanded_max: int = EXPANDED_MAX) -> TabState | None:
    """The :class:`TabState` of a document tab now (``None`` without a session / path). Reads only what
    the window holds in memory (see the module docstring)."""
    session = getattr(tab, "session", None)
    path = getattr(session, "path", None)
    if not isinstance(path, str) or not path:
        return None
    ts = TabState(path=os.path.abspath(path))
    fmt = getattr(session, "format", None)
    ts.format = fmt if isinstance(fmt, str) and _KIND_RE.match(fmt) else None
    ts.record_path = _record_path_json(getattr(session, "record_path", None))
    views: list[str | None] = []
    for pane in list(getattr(tab, "panes", None) or [])[:2]:
        kind = pane.current_kind() if callable(getattr(pane, "current_kind", None)) else None
        views.append(kind if isinstance(kind, str) and _KIND_RE.match(kind) else None)
    ts.views = views + [None] * (2 - len(views))
    index = tab.active_pane_index() if callable(getattr(tab, "active_pane_index", None)) else 0
    ts.active_pane = index if index in (0, 1) else 0
    visible = tab.second_pane_visible() if callable(getattr(tab, "second_pane_visible", None)) else True
    ts.second_pane = bool(visible)
    ref = getattr(session, "current", None)
    if ref is None:
        ref = getattr(tab, "pending_ref", None)        # not revealed yet (the document is still opening)
    ts.current = location_text(session, ref)
    ts.expanded = _tree_expanded(tab, expanded_max)
    if getattr(session, "shown_records", None) is None:
        ts.filter = filter_dict(getattr(session, "filter", None))
        quick = getattr(session, "quick_filters", None) or {}
        ts.quick_filters = {str(k): str(v) for k, v in list(quick.items())[:QUICK_FILTERS_MAX]
                            if isinstance(v, str) and v.strip() and len(v) <= _TEXT_MAX}
        keys = []
        for key in list(getattr(session, "sort_keys", None) or [])[:SORT_KEYS_MAX]:
            try:
                keys.append(sort_key_dict(key))
            except (AttributeError, TypeError):
                continue
        ts.sort = keys
    ts.search = search_inputs(search_panel, session)
    return ts


def capture_window(window: Any, *, search_panel: Any = None,
                   extra: Callable[[Any, TabState], Any] | None = None) -> SessionState:
    """The session of ``window`` (its tabs in order, at most :data:`TABS_MAX`, and the current one).
    ``extra(tab, state)`` may add to / correct each tab's state (the restore still on its way, the
    workspace extras)."""
    tabs = list(window.tab_widgets()) if callable(getattr(window, "tab_widgets", None)) else []
    current = window.current_tab() if callable(getattr(window, "current_tab", None)) else None
    if search_panel is None:
        search_panel = (getattr(window, "panels", None) or {}).get("search")
    out: list[TabState] = []
    active: int | None = None
    for tab in tabs:
        if len(out) >= TABS_MAX:
            break
        if getattr(tab, "closed", False):
            continue
        ts = capture_tab(tab, search_panel=search_panel)
        if ts is None:
            continue
        if extra is not None:
            try:
                extra(tab, ts)
            except Exception:  # noqa: BLE001 - the extras must not lose the session
                log.exception("completing the state of %s failed", ts.path)
        if tab is current:
            active = len(out)
        out.append(ts)
    return SessionState(tabs=out, active=active)


# ==================================================================================
# restore plan (any thread: it looks at the file system)
# ==================================================================================
@dataclass
class PlannedTab:
    """A tab to open: its state, the file found for it and its position in the saved list."""

    state: TabState
    path: str
    index: int


@dataclass
class RestorePlan:
    """What :func:`plan_restore` found: the tabs to open (saved order), the ones whose file is missing
    and the active tab (an index into ``tabs``; the saved active tab, else the nearest before it)."""

    tabs: list[PlannedTab] = field(default_factory=list)
    missing: list[TabState] = field(default_factory=list)
    active: int | None = None


def _exists(path: str) -> bool:
    from jsonreshape.fileio import long_path

    try:
        return os.path.isfile(long_path(path))
    except (OSError, ValueError):
        return False


def resolve_tab_path(tab: TabState, base_dir: str | None = None,
                     exists: Callable[[str], bool] | None = None) -> str | None:
    """The file to open for ``tab``: ``relpath`` relative to ``base_dir`` (a workspace's folder) first --
    a moved workspace folder still works --, then ``path`` (a relative one also against ``base_dir``);
    ``None`` when neither exists."""
    check = exists or _exists
    candidates: list[str] = []
    if base_dir and tab.relpath:
        candidates.append(os.path.normpath(os.path.join(base_dir, tab.relpath)))
    path = tab.path
    if not os.path.isabs(path) and base_dir:
        path = os.path.join(base_dir, path)
    candidates.append(os.path.normpath(os.path.abspath(path)))
    for candidate in candidates:
        try:
            if check(candidate):
                return candidate
        except (OSError, ValueError):
            continue
    return None


def plan_restore(tabs: Iterable[TabState], *, active: int | None = None, base_dir: str | None = None,
                 exists: Callable[[str], bool] | None = None, cancel: Any = None) -> RestorePlan:
    """Which of ``tabs`` can be opened (:func:`resolve_tab_path`); ``active`` is an index into ``tabs``."""
    from jsonreshape.tasks import check

    plan = RestorePlan()
    for i, tab in enumerate(tabs):
        check(cancel)
        found = resolve_tab_path(tab, base_dir, exists)
        if found is None:
            plan.missing.append(tab)
            continue
        if active is not None and i <= active:
            plan.active = len(plan.tabs)
        plan.tabs.append(PlannedTab(state=tab, path=found, index=i))
    if plan.active is None and plan.tabs and active is not None:
        plan.active = 0
    return plan


def relative_path(path: str, base_dir: str) -> str | None:
    """``path`` relative to ``base_dir`` (``None`` when there is no relative way: another drive)."""
    try:
        rel = os.path.relpath(os.path.abspath(path), os.path.abspath(base_dir))
    except ValueError:
        return None
    return None if os.path.isabs(rel) or os.path.splitdrive(rel)[0] else rel
