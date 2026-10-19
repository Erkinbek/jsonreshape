"""Workspace files and the Qt part of the session restore (docs/ARCHITECTURE.md §15.0 "Session restore",
"Workspace file"; AGENT_PROMPT §5, §11 "Loyiha (workspace) faylini saqlash").

Workspace file
==============
``*.jrworkspace``: UTF-8 JSON, written atomically (:func:`write_workspace`, a temp file replacing the old
one: an interrupted save leaves the old file)::

    {"version": 1, "app": "0.1.0", "saved": "2026-10-19T17:00:00+05:00",
     "tabs": [{"path": "C:\\\\data\\\\x.json", "relpath": "x.json", "format": null, "record_path": null,
               "views": ["tree", "text"], "active_pane": 0, "second_pane": true,
               "current": "#12.contact", "expanded": ["#0", "#12", "#12.contact"],
               "filter": {"text": "age > 30", "lang": "expr", "builder": {...}}, "quick_filters": {},
               "sort": [{"path": "name", "descending": false, "nulls": "last", "natural": true,
                         "collation": "uz", "case_sensitive": false}],
               "search": {"text": "Toshkent", "mode": "plain", ...},
               "table_layout": {...}, "bookmarks": [{"name": "…", "record": 12, "path": ["a"], "created": 0}],
               "mapping": {<a mapping file, mapping.dumps_mapping>} | null, "edits": "none" | "pending"}],
     "active": 0, "geometry": {"window": "<base64>", "state": "<base64>"} | null}

A tab is the session model of :mod:`jsonreshape.gui.sessionstore` (:class:`~sessionstore.TabState`) plus
the bookmarks of the file, its table layout (the GUI state's layout object of the file) and the structure
builder's tree when the builder was used (an embedded mapping file). ``relpath`` is relative to the
workspace file's folder (``null`` on another drive); opening tries it first, then ``path``, so a moved
workspace folder still works. Edit layers are never copied: ``"edits": "pending"`` only says the file had
unsaved changes -- they stay in the file's edit layer and the tab's recovery bar offers them (nothing is
restored silently).

Reading is strict (:func:`read_workspace` / :func:`loads_workspace`): every problem raises
:class:`WorkspaceError` (``workspace.error.*``) naming the JSON pointer of the bad part; an embedded mapping
is validated by :func:`jsonreshape.mapping.load_mapping` (its error is reported with the full pointer).
Keys this version does not know are ignored. Recent workspaces (at most :data:`RECENT_MAX`) are kept in the
GUI state (:data:`RECENT_KEY`).

Qt part
=======
:class:`WorkspaceController` (one per main window: ``MainWindow.workspaces``):

* **Session restore.** :meth:`~WorkspaceController.at_start` is the start-up decision of ``app.main``: files
  given on the command line open alone and the saved session is kept for the next start (it is not
  overwritten while this window lives, unless there was none); without files the setting
  ``restore_session`` decides -- ``always`` restores at once, ``ask`` shows a non-modal bar "Oxirgi
  sessiyani tiklash? (N ta fayl)" with Tiklash / Yo'q above the tabs (the saved session is kept until it
  is answered), ``never`` does nothing. From then on the session is written into the GUI state when the
  window closes (:meth:`~WorkspaceController.at_close`, before the tabs are shut down) and every
  :data:`~sessionstore.AUTOSAVE_MS` (crash safety; also the window geometry), only when it changed.
* **Workspaces.** File ▸ "Workspace'ni ochish…" (:meth:`~WorkspaceController.open_workspace`: the file is
  read and checked in a background task; with tabs open the user chooses "close them" -- unsaved changes
  asked tab by tab through the existing close flow -- or "add to them"), "Oxirgi workspace'lar",
  "Workspace'ni saqlash…" (:meth:`~WorkspaceController.save_workspace`: captured in the GUI thread, the
  mapping trees serialized and the file written in a background task).
* :class:`TabRestorer` opens the tabs of a :class:`~sessionstore.RestorePlan` (computed in a background
  task: which files exist) and re-applies their state: the panes' views (the tab is created with them),
  the second pane and the active pane, the current node (revealed once the document is open), the filter
  / quick filters / sort (validated first; the session runs them in the background once the index is
  ready), the expanded nodes (``TreeView.restore_expanded`` once the index is complete and a tree is
  shown: bounded, one background task, applied at once), the search panel's inputs (when the tab becomes
  the current one) and -- workspaces -- the bookmarks (merged into the file's bookmarks), the table layout
  and the builder's tree (installed once the document is indexed). Missing files are skipped with one
  status line; changed files simply open (what no longer exists is skipped); a search result view is not
  restored (its search inputs are).

Decisions / deviations (documented):

* A workspace's window geometry and dock state are applied only when it replaces the open tabs (or none
  were open): added to open tabs it does not move the window.
* Bookmarks of a workspace are merged into the file's bookmarks (never removed); its table layout replaces
  the remembered one of the file.
* The embedded mapping is installed through the structure builder's own loading path
  (``MappingState.install_tree``: kind check, wrapper, "Asl holat"); the builder's "save mapping" default
  then points next to the data file, never to the workspace file.
* The question "close the open tabs or add?" is a non-blocking message box; :meth:`ask_open_mode` is the
  test seam.
"""

from __future__ import annotations

import base64
import binascii
import logging
import os
import weakref
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Callable

from PySide6.QtCore import QByteArray, QObject, Qt, QTimer, Signal
from PySide6.QtWidgets import QMenu, QMessageBox, QVBoxLayout, QWidget

from jsonreshape import codec
from jsonreshape.errors import Cancelled, JsonDecodeError, JsonReshapeError, UserInputError
from jsonreshape.gui import sessionstore as ss
from jsonreshape.gui.guistate import Bookmark, norm_path
from jsonreshape.gui.sessionstore import FormatProblem, RestorePlan, SessionState, TabState, json_pointer
from jsonreshape.i18n import has, t
from jsonreshape.tasks import format_bytes

log = logging.getLogger(__name__)

__all__ = [
    "LAST_DIR_KEY",
    "MAX_FILE_BYTES",
    "RECENT_KEY",
    "RECENT_MAX",
    "WORKSPACE_EXT",
    "WORKSPACE_VERSION",
    "RestoreReport",
    "TabRestorer",
    "Workspace",
    "WorkspaceController",
    "WorkspaceError",
    "add_recent_workspace",
    "clear_recent_workspaces",
    "dumps_workspace",
    "embedded_mapping",
    "loads_workspace",
    "mapping_document",
    "read_workspace",
    "recent_workspaces",
    "remove_recent_workspace",
    "workspace_document",
    "write_workspace",
]

WORKSPACE_VERSION = 1
WORKSPACE_EXT = ".jrworkspace"
#: A larger workspace file is refused (an embedded mapping tree is bounded by the schema limits).
MAX_FILE_BYTES = 64 << 20
#: GUI state keys: the recent workspaces (newest first) and the folder of the last one.
RECENT_KEY = "recent_workspaces"
RECENT_MAX = 10
LAST_DIR_KEY = "last_workspace_dir"
#: Status lines name at most this many missing files.
NAMES_SHOWN = 3
STATUS_MS = 15000


# ==================================================================================
# errors
# ==================================================================================
class WorkspaceError(UserInputError):
    """A workspace file cannot be used: ``workspace.error.*`` with the JSON ``pointer`` of the bad part
    (``params["pointer"]``). An ``expected`` type name (``string``, ``list`` ...) is shown localized
    (``workspace.type.<name>``)."""

    key = "workspace.error.invalid"

    @property
    def pointer(self) -> str:
        return str(self.params.get("pointer") or "")

    @property
    def message(self) -> str:
        params = dict(self.params)
        expected = params.get("expected")
        if isinstance(expected, str) and has(f"workspace.type.{expected}"):
            params["expected"] = t(f"workspace.type.{expected}")
        return t(self.key, **params)


def _problem_error(problem: FormatProblem) -> WorkspaceError:
    return WorkspaceError(f"workspace.error.{problem.code}", pointer=problem.pointer or "/", **problem.params)


def _short(v: Any) -> str:
    try:
        text = codec.dumps(v).decode("utf-8", "replace")
    except Exception:  # noqa: BLE001 - anything printable will do
        text = repr(v)
    return text if len(text) <= 60 else text[:59] + "\N{HORIZONTAL ELLIPSIS}"


def _is_int(v: Any) -> bool:
    return isinstance(v, int) and not isinstance(v, bool)


# ==================================================================================
# JSON
# ==================================================================================
def _parse_iterative(data: bytes) -> Any:
    """Decoding without recursion (an embedded mapping of a very deep document is deeper than the
    decoders allow): the events of :func:`jsonreshape.stream.exact_events`, values exactly as
    :func:`jsonreshape.codec.loads` gives them."""
    from jsonreshape.stream import exact_events

    text = data.decode("utf-8")
    root: Any = None
    stack: list[Any] = []
    keys: list[Any] = []

    def add(v: Any) -> None:
        nonlocal root
        if not stack:
            root = v
        elif isinstance(stack[-1], list):
            stack[-1].append(v)
        else:
            stack[-1][keys[-1]] = v

    for event, value in exact_events(text):
        if event in ("start_map", "start_array"):
            container: Any = {} if event == "start_map" else []
            add(container)
            stack.append(container)
            keys.append(None)
        elif event == "map_key":
            keys[-1] = value
        elif event in ("end_map", "end_array"):
            stack.pop()
            keys.pop()
        else:
            add(value)
    if stack:
        raise ValueError("truncated JSON")
    return root


def _parse_json(data: bytes) -> Any:
    if data.startswith(b"\xef\xbb\xbf"):
        data = data[3:]
    try:
        return codec.loads(data)
    except JsonDecodeError as exc:
        first = exc
    try:
        return _parse_iterative(data)
    except Exception:  # noqa: BLE001 - the first error describes the problem
        raise WorkspaceError("workspace.error.json", detail=first.params.get("detail") or first.message) from None


# ==================================================================================
# the model
# ==================================================================================
@dataclass
class Workspace:
    """A workspace: the tabs (:class:`~sessionstore.TabState` with the workspace extras), the active tab,
    the window geometry (``{"window": base64, "state": base64}``); ``path`` = the file it was read from;
    ``mappings`` = the embedded mappings already validated by :func:`loads_workspace` (tab index ->
    ``(MappingTree, input info)``)."""

    tabs: list[TabState] = field(default_factory=list)
    active: int | None = None
    geometry: dict[str, str] | None = None
    app: str = ""
    saved: str = ""
    path: str | None = None
    mappings: dict[int, tuple[Any, dict]] = field(default_factory=dict)


def workspace_document(ws: Workspace, path: str) -> dict[str, Any]:
    """The JSON object of ``ws`` written at ``path`` (``relpath`` of every tab relative to its folder)."""
    from jsonreshape import __version__

    base = os.path.dirname(os.path.abspath(path))
    tabs = []
    for tab in ws.tabs:
        data = tab.to_dict(workspace=True)
        absolute = os.path.abspath(tab.path)
        ordered = {"path": absolute, "relpath": ss.relative_path(absolute, base)}
        ordered.update((k, v) for k, v in data.items() if k not in ("path", "relpath"))
        tabs.append(ordered)
    active = ws.active if ws.active is not None and 0 <= ws.active < len(tabs) else None
    return {"version": WORKSPACE_VERSION, "app": __version__,
            "saved": datetime.now().astimezone().isoformat(timespec="seconds"),
            "tabs": tabs, "active": active, "geometry": ws.geometry or None}


def dumps_workspace(ws: Workspace, path: str) -> bytes:
    """The file bytes of ``ws`` at ``path``: UTF-8, numbers exact, the members of the document and of each
    tab one per line with compact values (indenting every level would make the file of a deep embedded
    mapping grow with the square of its depth)."""
    doc = workspace_document(ws, path)

    def members(obj: dict[str, Any], pad: bytes, value: Callable[[str, Any], bytes]) -> list[bytes]:
        items = list(obj.items())
        return [pad + codec.dumps(k) + b": " + value(k, v) + (b"," if i < len(items) - 1 else b"")
                for i, (k, v) in enumerate(items)]

    def top(key: str, v: Any) -> bytes:
        if key != "tabs" or not v:
            return codec.dumps(v)
        tabs = [b"    {\n" + b"\n".join(members(tab, b"      ", lambda _k, x: codec.dumps(x))) + b"\n    }"
                for tab in v]
        return b"[\n" + b",\n".join(tabs) + b"\n  ]"

    return b"{\n" + b"\n".join(members(doc, b"  ", top)) + b"\n}\n"


def write_workspace(ws: Workspace, path: str | os.PathLike[str]) -> str:
    """Write ``ws`` atomically (the old file stays when anything fails); returns the absolute path."""
    from jsonreshape.fileio import atomic_write

    target = os.path.abspath(os.fspath(path))
    data = dumps_workspace(ws, target)
    with atomic_write(target, "wb") as f:
        f.write(data)
    return target


def mapping_document(tree: Any, *, input_info: dict | None = None, orig: Any = None) -> dict:
    """An embedded mapping: the mapping file of ``tree`` (:func:`jsonreshape.mapping.dumps_mapping`) as a
    JSON object. Worker threads (the tree may be big)."""
    from jsonreshape import mapping as mp

    return _parse_json(mp.dumps_mapping(tree, input_info=input_info, orig=orig))


def embedded_mapping(obj: Any, pointer: str = "") -> tuple[Any, dict]:
    """Validate an embedded mapping like a mapping file: ``(MappingTree, input info)``;
    ``WorkspaceError("workspace.error.mapping")`` with ``pointer`` + the mapping's own pointer."""
    from jsonreshape import mapping as mp

    try:
        data = codec.dumps(obj)
    except (TypeError, ValueError) as exc:
        raise WorkspaceError("workspace.error.mapping", pointer=pointer or "/", detail=str(exc)) from None
    try:
        return mp.load_mapping(data)
    except JsonReshapeError as exc:
        inner = str(exc.params.get("pointer") or "")
        raise WorkspaceError("workspace.error.mapping", pointer=(pointer + inner) or "/", detail=exc.message) from None


def _geometry(v: Any, pointer: str) -> dict[str, str] | None:
    if v is None:
        return None
    if not isinstance(v, dict):
        raise WorkspaceError("workspace.error.type", pointer=pointer, expected="object")
    out: dict[str, str] = {}
    for name in ("window", "state"):
        item = v.get(name)
        if item is None:
            continue
        ptr = pointer + json_pointer(name)
        if not isinstance(item, str):
            raise WorkspaceError("workspace.error.type", pointer=ptr, expected="string")
        try:
            base64.b64decode(item.encode("ascii"), validate=True)
        except (UnicodeEncodeError, binascii.Error, ValueError):
            raise WorkspaceError("workspace.error.value", pointer=ptr, detail=_short(item)) from None
        out[name] = item
    return out or None


def loads_workspace(data: bytes, *, path: str | None = None) -> Workspace:
    """Read and validate workspace file bytes (strict, see the module docstring)."""
    doc = _parse_json(bytes(data))
    if not isinstance(doc, dict):
        raise WorkspaceError("workspace.error.not_object")
    if "version" not in doc:
        raise WorkspaceError("workspace.error.missing", pointer="/version")
    version = doc["version"]
    if not _is_int(version) or version != WORKSPACE_VERSION:
        raise WorkspaceError("workspace.error.version", version=_short(version), expected=WORKSPACE_VERSION)
    if "tabs" not in doc:
        raise WorkspaceError("workspace.error.missing", pointer="/tabs")
    items = doc["tabs"]
    if not isinstance(items, list):
        raise WorkspaceError("workspace.error.type", pointer="/tabs", expected="list")
    if len(items) > ss.TABS_MAX:
        raise WorkspaceError("workspace.error.too_many", pointer="/tabs", count=len(items), limit=ss.TABS_MAX)
    tabs: list[TabState] = []
    mappings: dict[int, tuple[Any, dict]] = {}
    for i, item in enumerate(items):
        ptr = json_pointer("tabs", i)
        try:
            tab = TabState.from_dict(item, pointer=ptr, strict=True, workspace=True)
        except FormatProblem as problem:
            raise _problem_error(problem) from None
        if tab.mapping is not None:
            mappings[i] = embedded_mapping(tab.mapping, ptr + json_pointer("mapping"))
        tabs.append(tab)
    active = doc.get("active")
    if active is not None and (not _is_int(active) or not 0 <= active < max(1, len(tabs))):
        raise WorkspaceError("workspace.error.value", pointer="/active", detail=_short(active))
    if active is not None and not tabs:
        active = None
    geometry = _geometry(doc.get("geometry"), "/geometry")
    for name in ("app", "saved"):
        value = doc.get(name)
        if value is not None and not isinstance(value, str):
            raise WorkspaceError("workspace.error.type", pointer=json_pointer(name), expected="string")
    return Workspace(tabs=tabs, active=active, geometry=geometry, app=str(doc.get("app") or ""),
                     saved=str(doc.get("saved") or ""), path=path, mappings=mappings)


def read_workspace(path: str | os.PathLike[str]) -> Workspace:
    """Read a workspace file (at most :data:`MAX_FILE_BYTES`); OS errors are localized
    (``error.file_not_found`` ...)."""
    from jsonreshape.fileio import open_shared, os_error, read_limited

    target = os.path.abspath(os.fspath(path))
    try:
        with open_shared(target, "rb") as f:
            size = os.fstat(f.fileno()).st_size
            if size > MAX_FILE_BYTES:
                raise WorkspaceError("workspace.error.too_big", path=target, size=format_bytes(size),
                                     limit=format_bytes(MAX_FILE_BYTES))
            data = read_limited(f, MAX_FILE_BYTES)
    except OSError as exc:
        raise os_error(exc, target) from exc
    if len(data) > MAX_FILE_BYTES:
        raise WorkspaceError("workspace.error.too_big", path=target, size=format_bytes(len(data)),
                             limit=format_bytes(MAX_FILE_BYTES))
    return loads_workspace(data, path=target)


# ==================================================================================
# recent workspaces (GUI state)
# ==================================================================================
def recent_workspaces(state: Any) -> list[str]:
    """The recent workspace files, newest first (at most :data:`RECENT_MAX`)."""
    items = state.get(RECENT_KEY) if state is not None else None
    if not isinstance(items, list):
        return []
    return [p for p in items if isinstance(p, str) and p][:RECENT_MAX]


def add_recent_workspace(state: Any, path: str) -> None:
    target = os.path.abspath(path)
    key = norm_path(target)
    items = [target] + [p for p in recent_workspaces(state) if norm_path(p) != key]
    state.set(RECENT_KEY, items[:RECENT_MAX])


def remove_recent_workspace(state: Any, path: str) -> None:
    key = norm_path(path)
    items = recent_workspaces(state)
    left = [p for p in items if norm_path(p) != key]
    if left != items:
        state.set(RECENT_KEY, left or None)


def clear_recent_workspaces(state: Any) -> None:
    state.set(RECENT_KEY, None)


# ==================================================================================
# Qt part
# ==================================================================================
@dataclass
class RestoreReport:
    """What a restore opened: ``source`` (``session`` | ``workspace``), the new tabs, the names of the
    files not found, how many tabs had unsaved changes (workspaces), the workspace file."""

    source: str
    opened: list = field(default_factory=list)
    missing: list[str] = field(default_factory=list)
    pending_edits: int = 0
    path: str | None = None


def _names(names: list[str]) -> str:
    shown = ", ".join(names[:NAMES_SHOWN])
    if len(names) > NAMES_SHOWN:
        shown += " " + t("gui.sessionstore.more", count=len(names) - NAMES_SHOWN)
    return shown


def report_text(report: RestoreReport) -> str:
    """The one status line of a restore."""
    parts: list[str] = []
    if report.source == "workspace":
        parts.append(t("gui.workspace.opened", name=os.path.basename(report.path or ""),
                       count=len(report.opened)))
    elif report.opened:
        parts.append(t("gui.sessionstore.restored", count=len(report.opened)))
    if report.missing:
        parts.append(t("gui.sessionstore.missing", count=len(report.missing), names=_names(report.missing)))
    if report.pending_edits:
        parts.append(t("gui.workspace.pending_edits", count=report.pending_edits))
    return " ".join(parts)


def _node_ref(text: str | None) -> Any:
    """The NodeRef of a location text (``None`` when there is none / it does not parse)."""
    if not text:
        return None
    from jsonreshape.gui.session import NodeRef
    from jsonreshape.paths import parse_location

    try:
        loc = parse_location(text)
    except JsonReshapeError:
        return None
    return NodeRef(loc.record, tuple(loc.path))


class _TabJob:
    """What is still to be applied to a restored tab (held by the restorer; the tab weakly)."""

    __slots__ = ("tab", "current", "expanded", "search", "mapping", "mapping_doc", "label")

    def __init__(self, tab: Any, state: TabState, *, mapping: Any = None, label: str = "") -> None:
        self.tab = weakref.ref(tab)
        self.current = state.current
        self.expanded: list[str] | None = list(state.expanded) or None
        self.search: dict | None = dict(state.search) if state.search else None
        self.mapping = mapping                     # (MappingTree, input info) of a workspace
        self.mapping_doc = state.mapping if mapping is not None else None
        self.label = label

    def pending(self) -> bool:
        return bool(self.expanded or self.search or self.mapping is not None)


class TabRestorer(QObject):
    """Opens the tabs of a :class:`~sessionstore.RestorePlan` in the main window and applies their state
    (see the module docstring). Things that need the document (expanded nodes, the builder's tree) or the
    tab to be current (the search inputs) wait in a job per tab; :meth:`job` lets the session capture
    take what was not applied yet."""

    expanded_restored = Signal(object, int)     # (tab, nodes expanded): a tree view applied the saved nodes

    def __init__(self, window: Any, parent: QObject | None = None) -> None:
        super().__init__(parent if parent is not None else window)
        self.window = window
        self._jobs: dict[int, _TabJob] = {}
        window.tab_closed.connect(self._on_tab_closed)
        window.current_tab_changed.connect(self._on_current_tab)

    # -- queries --------------------------------------------------------------------------
    def job(self, tab: Any) -> _TabJob | None:
        job = self._jobs.get(id(tab))
        return job if job is not None and job.tab() is tab else None

    def pending_tabs(self) -> list[Any]:
        """Tabs with something still to apply (tests)."""
        return [job.tab() for job in self._jobs.values() if job.tab() is not None and job.pending()]

    # -- opening ----------------------------------------------------------------------------
    def open_plan(self, plan: RestorePlan, *, workspace: Workspace | None = None) -> list[Any]:
        """Open the planned tabs (saved order) and make the saved active one current; returns the tabs."""
        window = self.window
        opened: list[Any] = []
        label = workspace.path if workspace is not None else ""
        for item in plan.tabs:
            ts = item.state
            if workspace is not None:
                self._prepare_file(item.path, ts)
            options: dict[str, Any] = {"panes": ts.panes()}
            if ts.format:
                options["format"] = ts.format
            if ts.record_path is not None:
                options["record_path"] = ts.record_path
            ref = _node_ref(ts.current)
            if ref is not None:
                options["ref"] = ref
            try:
                tab = window.open_path(item.path, options)
            except Exception as exc:  # noqa: BLE001 - one tab must not stop the others
                log.exception("restoring the tab of %s failed", item.path)
                window.app.report_error(exc, window)
                continue
            mapping = workspace.mappings.get(item.index) if workspace is not None else None
            for pane, kind in zip(tab.panes, ts.panes()):
                if pane.current_kind() != kind and kind in pane.kinds():
                    pane.set_view(kind)             # (a window that does not take the "panes" option)
            self._apply(tab, ts)
            job = _TabJob(tab, ts, mapping=mapping, label=label or "")
            if job.pending():
                self._jobs[id(tab)] = job
                tab.session.index_finished.connect(self._on_index_finished)
                tab.active_view_changed.connect(self._on_view_changed)
            opened.append(tab)
        if plan.active is not None and 0 <= plan.active < len(opened):
            window.tabs.setCurrentWidget(opened[plan.active])
        self._on_current_tab(window.current_tab())
        return opened

    def _prepare_file(self, path: str, ts: TabState) -> None:
        """A workspace's table layout and bookmarks of the file, before its session reads them."""
        state = self.window.state
        if ts.table_layout is not None:
            try:
                state.set_table_layout(path, ts.table_layout)
            except (TypeError, ValueError):
                log.warning("the table layout of %s in the workspace is not usable", path)
        if ts.bookmarks:
            marks = state.bookmarks(path)
            seen = {(bm.record, bm.layer, tuple(bm.path)) for bm in marks}
            for item in ts.bookmarks:
                try:
                    bm = Bookmark.from_dict(item)
                except (TypeError, ValueError):
                    continue
                key = (bm.record, bm.layer, tuple(bm.path))
                if key not in seen:
                    seen.add(key)
                    marks.append(bm)
            state.set_bookmarks(path, marks)

    def _apply(self, tab: Any, ts: TabState) -> None:
        """What can be applied at once: the panes and the view specs (pending in the session until its
        index is ready; an invalid one is left out)."""
        try:
            tab.set_second_pane_visible(bool(ts.second_pane))
            tab.set_active_pane(int(ts.active_pane))
        except Exception:  # noqa: BLE001 - the panes are a convenience
            log.debug("restoring the panes failed", exc_info=True)
        session = tab.session
        spec = _filter_spec(ts.filter)
        quick = {path: text for path, text in ts.quick_filters.items() if _compiles(text)}
        keys = []
        for item in ts.sort:
            try:
                keys.append(ss.sort_key_from_dict(item))
            except (JsonReshapeError, TypeError, ValueError):
                log.info("restore: a sort key of %s is left out", ts.path)
        try:
            if spec is not None:
                session.set_filter(spec)
            if quick:
                session.set_quick_filters(quick)
            if keys:
                session.set_sort(keys)
        except JsonReshapeError as exc:
            log.info("restore: the view of %s is not restored: %s", ts.path, exc)

    # -- later steps ------------------------------------------------------------------------
    def _job_of_session(self, session: Any) -> _TabJob | None:
        for job in self._jobs.values():
            tab = job.tab()
            if tab is not None and tab.session is session:
                return job
        return None

    def _on_index_finished(self) -> None:
        job = self._job_of_session(self.sender())
        if job is not None:
            # after the views handled the signal (the tree takes the complete document first)
            QTimer.singleShot(0, self, lambda j=job: self._index_ready(j))

    def _on_view_changed(self) -> None:
        job = self.job(self.sender())
        if job is not None and job.expanded:
            self._try_expand(job)

    def _index_ready(self, job: _TabJob) -> None:
        tab = job.tab()
        if tab is None or tab.closed or self._jobs.get(id(tab)) is not job:
            return
        if job.expanded:
            self._try_expand(job)
        if job.mapping is not None:
            self._install_mapping(job)
        self._forget_if_done(job)

    def _try_expand(self, job: _TabJob) -> None:
        """Expand the saved nodes in the tree views shown now (after the index finished)."""
        tab = job.tab()
        if tab is None or tab.closed or not job.expanded:
            return
        session = tab.session
        if session.state != "ready" or session.doc is None:
            return
        started = False
        for pane in tab.panes:
            if pane.index == 1 and not tab.second_pane_visible():
                continue
            if pane.current_kind() != "tree":
                continue
            fn = getattr(pane.current_view(), "restore_expanded", None)
            if callable(fn):
                ref = weakref.ref(tab)

                def applied(count: int, ref: Any = ref) -> None:
                    shown = ref()
                    if shown is not None:
                        self.expanded_restored.emit(shown, int(count))

                try:
                    started = bool(fn(list(job.expanded), on_done=applied)) or started
                except Exception:  # noqa: BLE001 - best effort
                    log.exception("restoring the expanded nodes of %s failed", session.name)
                    started = True
        if started:
            job.expanded = None
            self._forget_if_done(job)

    def _install_mapping(self, job: _TabJob) -> None:
        """The workspace's structure builder tree, once the document is indexed (a silent task reads the
        wrapper of a wrapped document)."""
        tab = job.tab()
        if tab is None or tab.closed or job.mapping is None:
            return
        session = tab.session
        from jsonreshape.gui import mapmodel

        state = mapmodel.state_for(session)
        if not state.doc_ready():
            return                                  # the next index_finished
        tree, info = job.mapping
        job.mapping = None
        job.mapping_doc = None
        doc = session.doc.pinned()
        label = job.label or session.path

        def work(ctx: Any) -> tuple[Any, bool]:
            wrapper = mapmodel.parse_wrapper(doc, ctx.cancel) if doc.kind == "wrapped" else None
            edited = tree.orig is not None and mapmodel.structure_of(tree) != mapmodel.structure_of(tree.orig)
            return wrapper, edited

        def done(result: tuple[Any, bool]) -> None:
            if getattr(session, "closed", False):
                return
            wrapper, edited = result
            if state.install_tree(tree, wrapper=wrapper, label=label, edited=edited, info=info):
                state.file_path = None              # "save mapping" proposes a file next to the data
                state.status_changed.emit()

        def failed(exc: BaseException) -> None:
            if not getattr(session, "closed", False):
                session.app.report_error(exc)

        session.app.tasks.run(work, title=t("gui.workspace.task.mapping", name=session.name), kind="mapping",
                              owner=session, silent=True, on_done=done, on_error=failed)

    def _on_current_tab(self, tab: Any) -> None:
        """The search inputs of a restored tab, once the search panel shows its document."""
        job = self.job(tab) if tab is not None else None
        if job is None or not job.search:
            return
        panel = (getattr(self.window, "panels", None) or {}).get("search")
        setter = getattr(panel, "set_options", None)
        if callable(setter):
            current = getattr(panel, "session", None)
            try:
                current = current() if callable(current) else current
            except Exception:  # noqa: BLE001
                current = None
            if current is not tab.session:
                return                              # the panel shows another document now: later
            try:
                setter(dict(job.search))
            except Exception:  # noqa: BLE001 - the inputs are a convenience
                log.debug("restoring the search inputs failed", exc_info=True)
        job.search = None                           # applied (or there is no search panel to apply them to)
        self._forget_if_done(job)

    def _forget_if_done(self, job: _TabJob) -> None:
        tab = job.tab()
        if tab is None or not job.pending():
            self._drop(job)

    def _drop(self, job: _TabJob) -> None:
        held = [key for key, value in self._jobs.items() if value is job]
        if not held:
            return                                  # dropped already
        for key in held:
            del self._jobs[key]
        tab = job.tab()
        if tab is not None and not tab.closed:
            for signal, slot in ((tab.session.index_finished, self._on_index_finished),
                                 (tab.active_view_changed, self._on_view_changed)):
                try:
                    signal.disconnect(slot)
                except (RuntimeError, TypeError):
                    pass

    def _on_tab_closed(self, tab: Any) -> None:
        job = self._jobs.pop(id(tab), None)
        if job is not None:
            job.expanded = job.search = job.mapping = None


def _filter_spec(data: dict | None) -> Any:
    """The ``FilterSpec`` of a saved filter when it still compiles (``None`` otherwise)."""
    if not data:
        return None
    from jsonreshape.gui.session import FilterSpec

    spec = FilterSpec.from_dict(data)
    if spec.empty:
        return None
    try:
        spec.compile()
    except (JsonReshapeError, ValueError, TypeError) as exc:
        log.info("restore: the filter %r is left out: %s", spec.text, exc)
        return None
    return spec


def _compiles(text: str) -> bool:
    from jsonreshape.query import compile_filter

    try:
        compile_filter(text)
    except (JsonReshapeError, ValueError, TypeError):
        return False
    return True


class WorkspaceController(QObject):
    """The session restore and the workspace files of one main window (see the module docstring)."""

    session_stored = Signal()          # the session was put into the GUI state
    restored = Signal(object)          # a RestoreReport (session or workspace)
    workspace_saved = Signal(str)      # the file written

    def __init__(self, window: Any, *, interval_ms: int | None = None) -> None:
        super().__init__(window)
        self.window = window
        self.restorer = TabRestorer(window, parent=self)
        self.armed = False             # the session is written from now on (after the start-up decision)
        self.keep_saved = False        # ... except while the saved one must be kept (see at_start)
        self.path: str | None = None   # the workspace file opened / saved last in this window
        self.saved_session: SessionState | None = None
        self.bar: Any = None           # the "restore the last session?" bar
        self._holder: QWidget | None = None
        self._mapping_snaps: dict[int, tuple[bytes, Any, dict]] = {}
        self.timer = QTimer(self)
        self.timer.setInterval(int(interval_ms or ss.AUTOSAVE_MS))
        self.timer.timeout.connect(self.autosave)
        for signal in (window.tab_opened, window.tab_closed, window.current_tab_changed):
            signal.connect(self.update_actions)
        QTimer.singleShot(0, self, self.update_actions)

    # ================================================================== session
    def set_autosave_interval(self, ms: int) -> None:
        self.timer.setInterval(max(1, int(ms)))

    def _state(self) -> Any:
        return self.window.state

    def _save_state(self) -> None:
        """Write the GUI state without blocking (``AppContext.save_state``: a silent task)."""
        saver = getattr(self.window.app, "save_state", None)
        if callable(saver):
            saver()
        else:  # pragma: no cover - a bare context
            self._state().save()

    def _arm(self) -> None:
        self.armed = True
        if not self.timer.isActive():
            self.timer.start()

    def at_start(self, command_line_tabs: list[Any] | None = None) -> str:
        """The start-up decision (``app.main``): ``"command_line"`` (files given: only they open, a saved
        session is kept for the next start), ``"restore"`` (restoring), ``"ask"`` (the bar is shown),
        ``"never"`` / ``"nothing"`` (no session to restore)."""
        state = self._state()
        saved = ss.load_session(state)
        count = len(saved.tabs) if saved is not None else 0
        self._arm()
        if command_line_tabs:
            self.keep_saved = count > 0
            return "command_line"
        mode = ss.restore_mode(state)
        if not count:
            return "nothing"
        if mode == "never":
            return "never"
        self.keep_saved = True              # until the user answered / the tabs are open
        self.saved_session = saved
        if mode == "ask":
            self.show_bar(saved)
            return "ask"
        self.restore_session(saved)
        return "restore"

    def restore_session(self, saved: SessionState | None = None) -> Any:
        """Restore ``saved`` (default: the session in the GUI state): which files exist is checked in a
        silent task, then the tabs open. Returns the task (``None``: nothing to restore)."""
        if saved is None:
            saved = ss.load_session(self._state())
        if saved is None or not saved.tabs:
            self.keep_saved = False
            return None
        tabs, active = list(saved.tabs), saved.active
        window = self.window

        def work(ctx: Any) -> RestorePlan:
            return ss.plan_restore(tabs, active=active, cancel=ctx.cancel)

        def done(plan: RestorePlan) -> None:
            if getattr(window, "_closing", False):
                return
            opened = self.restorer.open_plan(plan)
            self.keep_saved = False
            report = RestoreReport("session", opened=opened, missing=[tab.name for tab in plan.missing])
            self._tell(report)

        def failed(exc: BaseException) -> None:
            log.warning("restoring the last session failed: %s", exc)
            if not getattr(window, "_closing", False):
                window.app.show_status(t("gui.sessionstore.failed", error=str(exc)), STATUS_MS)

        return window.app.tasks.run(work, title=t("gui.sessionstore.task.plan"), kind="session", owner=self,
                                    silent=True, on_done=done, on_error=failed)

    def _tell(self, report: RestoreReport) -> None:
        text = report_text(report)
        if text:
            self.window.app.show_status(text, STATUS_MS)
        self.restored.emit(report)

    def _complete(self, tab: Any, ts: TabState) -> None:
        """What a restore did not apply yet stays in the session (the document is still being opened)."""
        job = self.restorer.job(tab)
        if job is None:
            return
        if job.expanded and not ts.expanded:
            ts.expanded = list(job.expanded)
        if job.search and ts.search is None:
            ts.search = dict(job.search)
        if ts.current is None and job.current:
            ts.current = job.current

    def capture(self) -> SessionState:
        """The window's session now."""
        return ss.capture_window(self.window, extra=self._complete)

    def store(self, *, save: bool = True) -> bool:
        """Put the session into the GUI state (``save``: and write the state in the background). False when
        nothing was stored: not armed yet, the saved session is being kept, or nothing changed."""
        if not self.armed or self.keep_saved:
            return False
        try:
            changed = ss.store_session(self._state(), self.capture())
        except Exception:  # noqa: BLE001 - never lose the window over the session
            log.exception("storing the session failed")
            return False
        if changed:
            self.session_stored.emit()
            if save:
                self._save_state()
        return changed

    def autosave(self) -> bool:
        """The timer (crash safety): the window geometry and the session into the GUI state."""
        window = self.window
        if getattr(window, "_closing", False) or not self.armed:
            return False
        try:
            window.save_window_state()
        except Exception:  # noqa: BLE001
            log.debug("saving the window state failed", exc_info=True)
        stored = self.store(save=False)
        if self._state().dirty:
            self._save_state()
        return stored

    def at_close(self) -> None:
        """The window closes (its tabs still open): the session into the GUI state (saved by the
        application context's shutdown)."""
        self.timer.stop()
        self.hide_bar(keep=True)
        self.store(save=False)

    # -- the "restore the last session?" bar ---------------------------------------------------
    def show_bar(self, saved: SessionState) -> Any:
        """The non-modal question above the tabs (``ask``): Tiklash restores, Yo'q / closing keeps the
        window as it is (the session follows the window from then on)."""
        from jsonreshape.gui.widgets import InfoBar

        self.hide_bar(keep=True)
        window = self.window
        names = [tab.name for tab in saved.tabs]
        # the bar sits above the window's central widget (tabs / welcome page) in a holder that is the
        # central widget while the question is open
        holder = QWidget(window)
        holder.setObjectName("session_bar_holder")
        layout = QVBoxLayout(holder)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)
        bar = InfoBar("info", t("gui.sessionstore.ask", count=len(names)), detail=_names(names),
                      buttons=[(t("gui.sessionstore.restore"), self._bar_restore),
                               (t("gui.sessionstore.skip"), self._bar_skip)], parent=holder)
        bar.setObjectName("session_restore_bar")
        bar.closed.connect(self._bar_skip)
        layout.addWidget(bar)
        central = window.takeCentralWidget()
        if central is not None:
            layout.addWidget(central, 1)
            central.show()
        window.setCentralWidget(holder)
        holder.show()
        self.bar = bar
        self._holder = holder
        return bar

    def hide_bar(self, *, keep: bool = False) -> None:
        """Take the bar away (``keep``: the saved session stays kept -- the question was not answered)."""
        holder, self._holder = self._holder, None
        bar, self.bar = self.bar, None
        if holder is None:
            return
        window = self.window
        if not keep:
            self.keep_saved = False
        central = getattr(window, "central", None)
        if window.centralWidget() is holder:
            window.takeCentralWidget()
        if central is not None and central.parent() is holder:
            holder.layout().removeWidget(central)
            if window.centralWidget() is None:
                window.setCentralWidget(central)
            else:                                   # (never deleted with the holder)
                central.setParent(window)
            central.show()
        if bar is not None:
            try:
                bar.closed.disconnect(self._bar_skip)
            except (RuntimeError, TypeError):
                pass
        holder.hide()
        holder.deleteLater()

    def _bar_restore(self) -> None:
        saved = self.saved_session
        self.hide_bar(keep=True)
        if saved is not None:
            self.restore_session(saved)
        else:
            self.keep_saved = False

    def _bar_skip(self) -> None:
        self.hide_bar(keep=False)

    # ================================================================== workspaces
    def update_actions(self, *_args: Any) -> None:
        acts = getattr(self.window, "acts", None) or {}
        action = acts.get("workspace_save")
        if action is not None and not getattr(self.window, "_closing", False):
            action.setEnabled(bool(self.window.tab_widgets()))

    @staticmethod
    def _filters() -> str:
        return ";;".join([t("gui.workspace.filter", patterns="*" + WORKSPACE_EXT), t("gui.main.filter.all")])

    def _folder(self) -> str:
        state = self._state()
        folder = state.get(LAST_DIR_KEY) or ""
        if not folder and self.path:
            folder = os.path.dirname(self.path)
        if not folder:
            tab = self.window.current_tab()
            folder = os.path.dirname(tab.session.path) if tab is not None else str(state.get("last_dir") or "")
        return str(folder)

    def _default_path(self) -> str:
        if self.path:
            return self.path
        tab = self.window.current_tab()
        stem = "workspace"
        if tab is not None:
            stem = tab.session.name
            for ext in (".gz", ".json", ".jsonl", ".ndjson", ".geojson"):
                if stem.lower().endswith(ext):
                    stem = stem[:-len(ext)]
            stem = stem or "workspace"
        return os.path.join(self._folder(), stem + WORKSPACE_EXT)

    def _remember(self, path: str) -> None:
        state = self._state()
        self.path = path
        try:
            add_recent_workspace(state, path)
            state.set(LAST_DIR_KEY, os.path.dirname(path))
        except (TypeError, ValueError):
            log.debug("could not remember the workspace", exc_info=True)
        self._save_state()

    # -- saving ----------------------------------------------------------------------------------
    def _complete_workspace(self, tab: Any, ts: TabState) -> None:
        self._complete(tab, ts)
        session = tab.session
        state = self._state()
        for view in tab.views("table"):
            fn = getattr(view, "save_layout", None)
            if callable(fn):
                try:
                    fn()
                except Exception:  # noqa: BLE001
                    log.debug("save_layout failed", exc_info=True)
        ts.table_layout = state.table_layout(session.path)
        ts.bookmarks = [bm.to_dict() for bm in getattr(session, "bookmarks", [])]
        ts.edits = "pending" if bool(getattr(tab, "dirty", False)) else "none"
        job = self.restorer.job(tab)
        if job is not None and job.mapping_doc is not None:
            ts.mapping = job.mapping_doc            # not installed yet: kept as read
            return
        if not tab.views("mapping"):
            return
        from jsonreshape.gui import mapmodel

        mstate = mapmodel.state_for(session)
        if mstate.designer is None:
            return
        snap = mstate.snapshot()
        if snap is not None:
            self._mapping_snaps[id(ts)] = (snap, mstate.designer.orig, mstate.input_info())

    def capture_workspace(self) -> tuple[Workspace, dict[int, tuple[bytes, Any, dict]]]:
        """The window as a workspace (GUI thread; the mapping trees as snapshots, serialized later)."""
        self._mapping_snaps = {}
        try:
            session = ss.capture_window(self.window, extra=self._complete_workspace)
            snaps = dict(self._mapping_snaps)
        finally:
            self._mapping_snaps = {}
        window = self.window
        geometry = {"window": bytes(window.saveGeometry().toBase64().data()).decode("ascii"),
                    "state": bytes(window.saveState().toBase64().data()).decode("ascii")}
        by_tab = {i: snaps[id(tab)] for i, tab in enumerate(session.tabs) if id(tab) in snaps}
        return Workspace(tabs=session.tabs, active=session.active, geometry=geometry), by_tab

    def save_workspace(self, path: str | os.PathLike[str] | None = None) -> Any:
        """File ▸ "Workspace'ni saqlash…": capture now, write in a silent task. Returns the task (``None``:
        nothing to save / cancelled)."""
        window = self.window
        if not window.tab_widgets():
            window.app.show_status(t("gui.workspace.empty"), 6000)
            return None
        if path is None:
            path = window._ask_save_path(t("gui.workspace.save.title"), self._default_path(), self._filters())
            if not path:
                return None
        target = os.path.abspath(os.fspath(path))
        if not os.path.splitext(target)[1]:
            target += WORKSPACE_EXT
        ws, snaps = self.capture_workspace()

        def work(_ctx: Any) -> str:
            from jsonreshape import mapping as mp

            for index, (snap, orig, info) in snaps.items():
                ws.tabs[index].mapping = mapping_document(mp.MappingTree.restore(snap), input_info=info, orig=orig)
            return write_workspace(ws, target)

        def done(written: str) -> None:
            self._remember(written)
            window.app.show_status(t("gui.workspace.saved", path=os.path.normpath(written)), 8000)
            self.workspace_saved.emit(written)

        def failed(exc: BaseException) -> None:
            window.app.report_error(exc, window)

        return window.app.tasks.run(work, title=t("gui.workspace.task.save", name=os.path.basename(target)),
                                    kind="workspace", owner=self, silent=True, on_done=done, on_error=failed)

    # -- opening ---------------------------------------------------------------------------------
    def open_workspace(self, path: str | os.PathLike[str] | None = None, *, from_recent: bool = False) -> Any:
        """File ▸ "Workspace'ni ochish…": read and check the file (and which of its files exist) in a
        silent task, then open it (see :meth:`ask_open_mode`). Returns the task (``None``: cancelled)."""
        window = self.window
        if path is None:
            path = window._ask_open_path(t("gui.workspace.open.title"), self._folder(), self._filters())
            if not path:
                return None
        target = os.path.abspath(os.fspath(path))

        def work(ctx: Any) -> tuple[Workspace, RestorePlan]:
            ws = read_workspace(target)
            plan = ss.plan_restore(ws.tabs, active=ws.active, base_dir=os.path.dirname(target), cancel=ctx.cancel)
            return ws, plan

        def done(result: tuple[Workspace, RestorePlan]) -> None:
            if not getattr(window, "_closing", False):
                self._loaded(target, *result)

        def failed(exc: BaseException) -> None:
            if isinstance(exc, Cancelled) or getattr(window, "_closing", False):
                return
            if from_recent and getattr(exc, "key", None) == "error.file_not_found":
                remove_recent_workspace(self._state(), target)
                self._save_state()
                window.app.show_status(t("gui.workspace.recent.missing", path=os.path.normpath(target)), 8000)
                return
            window.app.report_error(exc, window)

        return window.app.tasks.run(work, title=t("gui.workspace.task.read", name=os.path.basename(target)),
                                    kind="workspace", owner=self, silent=True, on_done=done, on_error=failed)

    def _loaded(self, path: str, ws: Workspace, plan: RestorePlan) -> None:
        window = self.window
        open_tabs = [tab for tab in window.tab_widgets() if not tab.closed]
        if not open_tabs:
            self._apply_workspace(path, ws, plan, replace=True)
            return

        def answered(choice: str) -> None:
            if getattr(window, "_closing", False):
                return
            if choice == "replace":
                window.close_all_tabs(on_done=lambda ok: ok and self._apply_workspace(path, ws, plan, replace=True))
            elif choice == "add":
                self._apply_workspace(path, ws, plan, replace=False)

        self.ask_open_mode(len(open_tabs), answered)

    def ask_open_mode(self, count: int, callback: Callable[[str], Any]) -> Any:
        """Tabs are open: close them first (``"replace"``), add the workspace to them (``"add"``) or
        ``"cancel"`` -- a non-blocking question (tests replace this method)."""
        box = QMessageBox(QMessageBox.Icon.Question, t("gui.workspace.ask.title"),
                          t("gui.workspace.ask", count=count), QMessageBox.StandardButton.NoButton, self.window)
        box.setObjectName("workspace_open_question")
        box.setTextFormat(Qt.TextFormat.PlainText)
        replace = box.addButton(t("gui.workspace.ask.replace"), QMessageBox.ButtonRole.AcceptRole)
        add = box.addButton(t("gui.workspace.ask.add"), QMessageBox.ButtonRole.ActionRole)
        cancel = box.addButton(t("gui.common.cancel"), QMessageBox.ButtonRole.RejectRole)
        box.setDefaultButton(add)
        box.setEscapeButton(cancel)
        box.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
        answered: list[str] = []

        def finished(_result: int) -> None:
            if answered:
                return
            clicked = box.clickedButton()
            answered.append("replace" if clicked is replace else "add" if clicked is add else "cancel")
            callback(answered[0])

        box.finished.connect(finished)
        box.open()
        return box

    def _apply_workspace(self, path: str, ws: Workspace, plan: RestorePlan, *, replace: bool) -> None:
        window = self.window
        if getattr(window, "_closing", False):
            return
        if self.bar is not None:
            self.hide_bar(keep=False)       # the user chose what to open
        if replace and ws.geometry:
            self._restore_geometry(ws.geometry)
        ws.path = path
        opened = self.restorer.open_plan(plan, workspace=ws)
        self.keep_saved = False
        self._remember(path)
        pending = sum(1 for item in plan.tabs if item.state.edits == "pending")
        self._tell(RestoreReport("workspace", opened=opened, missing=[tab.name for tab in plan.missing],
                                 pending_edits=pending, path=path))

    def _restore_geometry(self, geometry: dict[str, str]) -> None:
        window = self.window
        try:
            if geometry.get("window"):
                window.restoreGeometry(QByteArray.fromBase64(geometry["window"].encode("ascii")))
            if geometry.get("state"):
                window.restoreState(QByteArray.fromBase64(geometry["state"].encode("ascii")))
        except Exception:  # noqa: BLE001 - the geometry is a convenience
            log.debug("restoring the workspace geometry failed", exc_info=True)

    # -- recent workspaces ----------------------------------------------------------------------
    def fill_recent_menu(self, menu: QMenu) -> None:
        """File ▸ "Oxirgi workspace'lar" (filled when it opens)."""
        menu.clear()
        files = recent_workspaces(self._state())
        if not files:
            empty = menu.addAction(t("gui.workspace.recent.empty"))
            empty.setEnabled(False)
        for i, path in enumerate(files, start=1):
            name = (os.path.basename(path) or path).replace("&", "&&")
            action = menu.addAction(f"&{i} {name}" if i < 10 else name)
            action.setObjectName(f"recent_workspace_{i}")
            native = os.path.normpath(path)
            action.setToolTip(native)
            action.setStatusTip(native)
            action.setData(path)
            action.triggered.connect(lambda _checked=False, p=path: self.open_workspace(p, from_recent=True))
        menu.addSeparator()
        clear = menu.addAction(t("gui.workspace.recent.clear"))
        clear.setObjectName("recent_workspaces_clear")
        clear.setEnabled(bool(files))
        clear.triggered.connect(self.clear_recent)

    def clear_recent(self) -> None:
        clear_recent_workspaces(self._state())
        self._save_state()
