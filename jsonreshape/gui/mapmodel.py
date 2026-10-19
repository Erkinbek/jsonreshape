"""The structure builder's state and tree model (docs/ARCHITECTURE.md §13.5; AGENT_PROMPT §7).

* :class:`MappingState` -- one per :class:`~jsonreshape.gui.session.DocSession`
  (:func:`state_for`): the :class:`~jsonreshape.mapping.Designer` of the document's mapping tree,
  how the tree was made (a sample of ``Settings.sample_size`` records at the positions of
  :func:`jsonreshape.schema.sample_positions`, every record -- "To'liq skanerlash" -- or a mapping
  file), the background work (building the tree, the read part of a recursive flatten, opening and
  saving mapping files) and the operations (each one undo step of the designer; refused ones give
  their reason). Every structure-builder view of the session (one per pane) shows the same
  designer: switching views keeps the tree and its history.
* :func:`visible_rows` -- the rows of the tree the way the prototype's ``walkRender`` lists them
  (children of open nodes only; at most :data:`MORE_STEP` children per node, then a "yana N ta
  maydonni ko'rsatish" row that shows :data:`MORE_STEP` more).
* :class:`MapModel` -- a flat ``QAbstractListModel`` of those rows for the ``QTreeView`` and the
  delegate of :mod:`jsonreshape.gui.mapview`; :class:`RowInfo` = what one row shows (label, type
  chips, meta, badges with tooltips and theme roles, samples, the enabled row actions), built
  lazily from :mod:`jsonreshape.mapping`'s row helpers.
* :func:`preview_text` -- the prototype's ``fmt`` (pretty JSON, arrays cut after 20 items with "…
  davomi eksportda" / "… yana N ta", objects after 200 keys, strings after 300 characters),
  iterative and bounded; :func:`compute_preview` -- the live preview of one record (worker thread).

Threads. Everything here runs in the GUI thread except the task functions, which read ONE pinned
state of the document (``doc.pinned()``) and never touch Qt objects. The designer is changed only
in the GUI thread; the one exception is a recursive flatten, whose read-only plan
(:meth:`~jsonreshape.mapping.Designer.plan_flatten`) runs in a task -- while it runs every other
designer operation is refused with a message (expanding / collapsing rows stays possible: the plan
does not read it). Previews, saves and exports evaluate a private copy of the tree
(:meth:`~jsonreshape.mapping.MappingTree.snapshot` taken in the GUI thread, restored in the worker),
so they never read a tree that changes meanwhile. Document-reading tasks are owned by the session:
a reopen or a closed tab detaches them and the session closes its old document only after their
threads ended.

Decisions (documented deviations / choices where §13.5 is open):

* **Reopen.** The designer belongs to the document kind and record path it was made for. A reopen
  with the same kind and record path keeps an edited designer, one opened from a mapping file and
  a Full scan tree (a sample tree must not silently replace fields only the full scan found; "To'liq
  skanerlash" again reads the new document): its tree and history stay, previews read the new
  document, flatten re-infers from the new document; an unedited sample tree is inferred again from
  the new document's sample (fresh counts and samples). Another kind / record path drops the
  designer and infers a new tree (a message says so) -- the old tree describes other records.
* **Full scan / open mapping** replace the designer (a new history); the views ask first when the
  current one has changes. A Full scan tree's flatten re-infers from every record (a visible heavy
  task); a sample tree's from the sample (a silent task). The tree is inferred by
  :func:`jsonreshape.mapping.infer_document`, exactly like the CLI's ``flatten``.
* **History.** The designer keeps ``Settings.undo_limit`` steps (the app's settings). "Asl holat" is
  enabled while there are undoable steps (the prototype's rule) and also right after opening a
  mapping file whose tree differs from the original stored in it (:attr:`MappingState.can_reset`).
* **Huge records** (> ``Settings.large_node_bytes``) are not in the inferred tree (counted in the
  header, like the schema view); the export stops on them unless they are skipped.
* **Wrapped documents** whose records are object members are refused
  (``engine.error.member_container``, the engine refuses them too); a wrapper that never closes
  (partial document) as well (``engine.error.partial_wrapper``).
* The tree of a records document keeps the reference's root: ``N ta yozuv`` is the document's
  record count, the record element's ``N ta`` the number of records the tree was inferred from.
"""

from __future__ import annotations

import json
import logging
import os
import re
import weakref
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Callable, Iterator

from PySide6.QtCore import QAbstractListModel, QModelIndex, QObject, Qt, Signal

from jsonreshape import codec
from jsonreshape import mapping as mp
from jsonreshape.errors import JsonReshapeError, TooLargeError, UserInputError
from jsonreshape.gui import copyfmt
from jsonreshape.i18n import t
from jsonreshape.tasks import CancelToken, format_bytes

log = logging.getLogger(__name__)

__all__ = [
    "ACTION_ORDER",
    "MORE_STEP",
    "PREVIEW_LIMIT",
    "PREVIEW_MAX_BYTES",
    "PREVIEW_MAX_CHARS",
    "PREVIEW_MAX_LINES",
    "RECORD_KINDS",
    "VALUE_KEEP_MAX",
    "BuildResult",
    "MapModel",
    "MapRow",
    "MappingState",
    "PreviewResult",
    "RowInfo",
    "build_tree",
    "compute_preview",
    "display_text",
    "forget_state",
    "make_population",
    "parse_wrapper",
    "place_records",
    "preview_text",
    "row_info",
    "state_for",
    "structure_of",
    "visible_rows",
]

#: Children shown per node before a "yana N ta maydonni ko'rsatish" row (the prototype's 400).
MORE_STEP = 400
#: The prototype's ``PREVIEW_LIMIT``: every produced array stops after 21 items (20 are shown).
PREVIEW_LIMIT = 21
PREVIEW_ITEMS = 20            # array items shown by preview_text
PREVIEW_KEYS = 200            # object keys shown by preview_text
PREVIEW_STRING = 300          # characters of a string shown by preview_text
#: Records larger than this are not previewed (parsed objects take ~10x their text).
PREVIEW_MAX_BYTES = 8 * 1024 * 1024
#: Record values up to this size are kept for the next preview of the same record (navigation,
#: Natija / Asl, mapping changes); larger ones are read again.
VALUE_KEEP_MAX = 1 << 20
#: Preview text bounds (a QPlainTextEdit with highlighting stays fast below them).
PREVIEW_MAX_CHARS = 400_000
PREVIEW_MAX_LINES = 10_000
#: Record document kinds (the tree's root is the array of records).
RECORD_KINDS = ("array", "jsonl", "stream", "brace_list")
#: The row actions in the prototype's button order.
ACTION_ORDER = ("group", "unwrap", "flatten", "rename", "delete")
#: Model roles.
ROW_ROLE = int(Qt.ItemDataRole.UserRole) + 1
INFO_ROLE = int(Qt.ItemDataRole.UserRole) + 2

_DISPLAY_RE = re.compile("[\x00-\x1f\x7f-\x9f\u2028\u2029\u200e\u200f\u202a-\u202e\u2066-\u2069\ufeff\ud800-\udfff]")


def display_text(text: str) -> str:
    """``text`` safe for one painted line: control / bidi characters and lone surrogates as
    ``\\uXXXX`` (a key may hold a line break)."""
    if not text or (text.isascii() and text.isprintable()):
        return text
    return _DISPLAY_RE.sub(lambda m: "\\u%04x" % ord(m.group()), text)


# ==================================================================================
# rows (walkRender)
# ==================================================================================
@dataclass(slots=True, eq=False)
class MapRow:
    """One visible row: a node, or the "show more" row of a node with many children."""

    kind: str                       # "node" | "more"
    node: Any                       # node rows: the MapNode; "more" rows: None
    node_id: int                    # node rows: the node's id; "more" rows: the parent's id
    parent: Any                     # the parent MapNode (None for the root)
    depth: int
    ctx: tuple                      # the src of every element node above (badges)
    hidden: int = 0                 # "more" rows: children not shown yet


def visible_rows(tree: Any, limits: dict[int, int] | None = None) -> list[MapRow]:
    """The rows of ``tree`` in display order (the prototype's ``walkRender``, iterative): every
    node whose ancestors are open; a node shows ``limits[id]`` (default :data:`MORE_STEP`)
    children, then a "more" row."""
    limits = limits or {}
    rows: list[MapRow] = []
    stack: list[Any] = [(tree.root, None, 0, ())]
    while stack:
        item = stack.pop()
        if isinstance(item, MapRow):
            rows.append(item)
            continue
        n, p, d, ctx = item
        rows.append(MapRow("node", n, n.id, p, d, ctx))
        kids = n.children
        if not n.open or not kids:
            continue
        ctx2 = ctx + (tuple(n.src),) if n.is_elem and n.src is not None else ctx
        lim = max(1, int(limits.get(n.id, MORE_STEP)))
        if len(kids) > lim:
            stack.append(MapRow("more", None, n.id, n, d + 1, ctx2, len(kids) - lim))
        for c in reversed(kids[:lim]):
            stack.append((c, n, d + 1, ctx2))
    return rows


@dataclass(slots=True)
class RowInfo:
    """What a node row shows (the prototype's ``rowHTML``)."""

    label: str
    style: str                                  # "root" | "elem" | "key"
    chips: list[tuple[str, str, str]]           # (glyph, tooltip, theme role)
    meta: list[tuple[str, str]]                 # (text, tooltip)
    badges: list[tuple[str, str, str]]          # (text, tooltip, theme role)
    sample: str
    actions: list[str]                          # enabled row actions (ACTION_ORDER subset)
    reasons: dict[str, str | None]              # every action -> None (enabled) or why not
    has_children: bool
    open: bool
    draggable: bool
    renamable: bool
    more_text: str = ""                         # "more" rows


def row_info(tree: Any, designer: Any, row: MapRow, *, file_name: str | None = None,
             record_count: int | None = None) -> RowInfo:
    """The texts and states of ``row`` (GUI thread; the mapping module's row helpers)."""
    if row.kind == "more":
        return RowInfo(label="", style="more", chips=[], meta=[], badges=[], sample="", actions=[], reasons={},
                       has_children=False, open=False, draggable=False, renamable=False,
                       more_text=t("mapping.more", count=_count(row.hidden)))
    n, p = row.node, row.parent
    label = display_text(mp.node_label(tree, n, p, file_name=file_name))
    style = "root" if n.is_root else "elem" if n.is_elem else "key"
    count = record_count if n.is_root and tree.root_kind in ("records", "keyed") else None
    reasons = designer.actions(n) if designer is not None else {a: "" for a in ACTION_ORDER}
    return RowInfo(
        label=label,
        style=style,
        chips=mp.type_chips(n),
        meta=mp.node_meta(tree, n, record_count=count),
        badges=[(display_text(text), tip, role) for text, tip, role in mp.badges(tree, n, p, row.ctx)],
        sample=display_text(mp.sample_display(n)),
        actions=[a for a in ACTION_ORDER if reasons.get(a) is None],
        reasons=reasons,
        has_children=bool(n.children),
        open=bool(n.open),
        draggable=not n.is_root and not n.is_elem,
        renamable=not n.is_root and not n.is_elem,
    )


def _count(n: int) -> str:
    from jsonreshape.gui.widgets import format_count

    return format_count(n)


class MapModel(QAbstractListModel):
    """The visible rows of a mapping tree (flat: the view paints the indentation). Row data are
    :class:`MapRow` (``ROW_ROLE``) and :class:`RowInfo` (``INFO_ROLE``, built when first asked
    for). Editing a row (``EditRole``) does not change the model: ``rename_requested(node id,
    text)`` is emitted and the owner renames through the designer (the rows are rebuilt then)."""

    rename_requested = Signal(int, str)

    def __init__(self, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._rows: list[MapRow] = []
        self._infos: dict[int, RowInfo] = {}
        self._by_id: dict[int, int] = {}
        self._info_fn: Callable[[MapRow], RowInfo] | None = None

    # -- content ---------------------------------------------------------------------------
    def set_rows(self, rows: list[MapRow], info_fn: Callable[[MapRow], RowInfo] | None) -> None:
        """Replace every row (a model reset)."""
        self.beginResetModel()
        self._rows = list(rows)
        self._infos = {}
        self._info_fn = info_fn
        self._by_id = {r.node_id: i for i, r in enumerate(self._rows) if r.kind == "node"}
        self.endResetModel()

    def rows(self) -> list[MapRow]:
        return list(self._rows)

    def row(self, i: int) -> MapRow | None:
        return self._rows[i] if 0 <= i < len(self._rows) else None

    def row_of(self, node_id: int | None) -> int | None:
        """The row index of the node ``node_id`` (``None`` when it is not visible)."""
        return None if node_id is None else self._by_id.get(int(node_id))

    def info(self, i: int) -> RowInfo | None:
        info = self._infos.get(i)
        if info is None:
            row = self.row(i)
            if row is None or self._info_fn is None:
                return None
            info = self._info_fn(row)
            self._infos[i] = info
        return info

    # -- Qt model --------------------------------------------------------------------------
    def rowCount(self, parent: QModelIndex = QModelIndex()) -> int:  # noqa: B008, N802 (Qt name)
        return 0 if parent.isValid() else len(self._rows)

    def data(self, index: QModelIndex, role: int = int(Qt.ItemDataRole.DisplayRole)) -> Any:
        if not index.isValid():
            return None
        i = index.row()
        row = self.row(i)
        if row is None:
            return None
        if role == ROW_ROLE:
            return row
        if role == INFO_ROLE:
            return self.info(i)
        if role == int(Qt.ItemDataRole.DisplayRole):
            info = self.info(i)
            return (info.more_text if row.kind == "more" else info.label) if info is not None else ""
        if role == int(Qt.ItemDataRole.EditRole):
            return row.node.key if row.kind == "node" else ""
        if role == int(Qt.ItemDataRole.AccessibleTextRole):
            info = self.info(i)
            if info is None:
                return ""
            if row.kind == "more":
                return info.more_text
            parts = [info.label] + [c[1] for c in info.chips] + [m[0] for m in info.meta] + [b[0] for b in info.badges]
            return ", ".join(p for p in parts if p)
        return None

    def flags(self, index: QModelIndex) -> Qt.ItemFlag:
        if not index.isValid():
            return Qt.ItemFlag.NoItemFlags
        flags = Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsSelectable
        row = self.row(index.row())
        if row is not None and row.kind == "node" and not row.node.is_root and not row.node.is_elem:
            flags |= Qt.ItemFlag.ItemIsEditable
        return flags

    def setData(self, index: QModelIndex, value: Any, role: int = int(Qt.ItemDataRole.EditRole)) -> bool:  # noqa: N802
        row = self.row(index.row()) if index.isValid() else None
        if row is None or row.kind != "node" or role != int(Qt.ItemDataRole.EditRole):
            return False
        self.rename_requested.emit(int(row.node_id), str(value))
        return False


# ==================================================================================
# preview text (the prototype's fmt) and the preview computation
# ==================================================================================
def _json_string(s: str) -> str:
    text = json.dumps(s, ensure_ascii=False)
    if "\u2028" in text or "\u2029" in text:
        text = text.replace("\u2028", "\\u2028").replace("\u2029", "\\u2029")
    return text


def _scalar(v: Any) -> str:
    if v is True:
        return "true"
    if v is False:
        return "false"
    if isinstance(v, str):
        return _json_string(v[:PREVIEW_STRING] + "…" if len(v) > PREVIEW_STRING else v)
    if isinstance(v, (int, float)) or codec.is_number(v):
        try:
            return codec.number_text(v)
        except (TypeError, ValueError):
            return str(v)
    return "null"


def preview_text(value: Any, *, mode: str = "out", max_chars: int = PREVIEW_MAX_CHARS,
                 max_lines: int = PREVIEW_MAX_LINES) -> str:
    """Pretty JSON of ``value`` like the prototype's ``fmt``: two-space indent, the first 20
    items of an array then "… davomi eksportda" (``mode="out"``) or "… yana N ta" (``mode="src"``),
    the first 200 keys of an object then "… yana N ta kalit", strings cut after 300 characters
    (``…`` inside the quotes), exact numbers, ``null`` for a missing value. Iterative (any depth)
    and bounded: after ``max_chars`` characters or ``max_lines`` lines a last line says the preview
    was cut. Lone surrogates are written as ``\\uXXXX`` (Qt would drop them)."""
    parts: list[str] = []
    size = 0
    lines = 0
    stack: list[tuple[int, Any, int]] = [(0, value, 0)]     # (0, value, indent) | (1, text, 0)
    more_out = t("gui.mapview.preview.more_out")
    missing = _MISSING()
    while stack:
        kind, v, ind = stack.pop()
        if kind == 1:
            piece = v
        elif v is None or v is missing:
            piece = "null"
        elif isinstance(v, (list, tuple)):
            if not v:
                piece = "[]"
            else:
                pad = "  " * ind
                pad1 = pad + "  "
                stack.append((1, "\n" + pad + "]", 0))
                if len(v) > PREVIEW_ITEMS:
                    marker = more_out if mode == "out" else t("gui.mapview.preview.more_items",
                                                              count=_count(len(v) - PREVIEW_ITEMS))
                    stack.append((1, ",\n" + pad1 + marker, 0))
                shown = v[:PREVIEW_ITEMS]
                for k in range(len(shown) - 1, -1, -1):
                    stack.append((0, shown[k], ind + 1))
                    stack.append((1, pad1 if k == 0 else ",\n" + pad1, 0))
                piece = "["
                stack.append((1, "\n", 0))
        elif isinstance(v, dict):
            if not v:
                piece = "{}"
            else:
                pad = "  " * ind
                pad1 = pad + "  "
                stack.append((1, "\n" + pad + "}", 0))
                keys = list(v.keys()) if len(v) <= PREVIEW_KEYS else [k for k, _ in zip(v.keys(), range(PREVIEW_KEYS))]
                if len(v) > PREVIEW_KEYS:
                    stack.append((1, ",\n" + pad1 + t("gui.mapview.preview.more_keys",
                                                        count=_count(len(v) - PREVIEW_KEYS)), 0))
                for k in range(len(keys) - 1, -1, -1):
                    key = keys[k]
                    stack.append((0, v[key], ind + 1))
                    stack.append((1, (pad1 if k == 0 else ",\n" + pad1) + _json_string(str(key)) + ": ", 0))
                piece = "{"
                stack.append((1, "\n", 0))
        else:
            piece = _scalar(v)
        parts.append(piece)
        size += len(piece)
        lines += piece.count("\n")
        if size > max_chars or lines > max_lines:
            parts.append("\n" + t("gui.mapview.preview.cut"))
            break
    return copyfmt.escape_surrogates("".join(parts))


_MISSING_OBJ: Any = None


def _MISSING() -> Any:  # noqa: N802 - the engine's sentinel, imported lazily
    global _MISSING_OBJ
    if _MISSING_OBJ is None:
        from jsonreshape.engine import MISSING

        _MISSING_OBJ = MISSING
    return _MISSING_OBJ


def place_records(wrapper: Any, record_path: tuple, records: list) -> Any:
    """A copy of the parsed ``wrapper`` with ``records`` as the record array at ``record_path``
    (the containers along the path are copied; ``ValueError`` when the path is not there)."""
    if not record_path:
        return records
    if isinstance(wrapper, dict):
        root: Any = dict(wrapper)
    elif isinstance(wrapper, list):
        root = list(wrapper)
    else:
        raise ValueError("the wrapper is not an object or an array")
    cur = root
    for i, seg in enumerate(record_path):
        ok = (isinstance(cur, dict) and isinstance(seg, str) and seg in cur) or (
            isinstance(cur, list) and isinstance(seg, int) and not isinstance(seg, bool) and 0 <= seg < len(cur))
        if not ok:
            raise ValueError(f"record path {list(record_path)!r} not found in the wrapper")
        if i == len(record_path) - 1:
            cur[seg] = records
        else:
            nxt = cur[seg]
            nxt = dict(nxt) if isinstance(nxt, dict) else list(nxt) if isinstance(nxt, list) else None
            if nxt is None:
                raise ValueError(f"record path {list(record_path)!r} not found in the wrapper")
            cur[seg] = nxt
            cur = nxt
    return root


def parse_wrapper(doc: Any, cancel: CancelToken | None = None) -> Any:
    """The parsed wrapper of a wrapped document with an empty record array (``prefix + "[]" +
    suffix``; worker threads): :func:`jsonreshape.mapping.document_wrapper` -- ``MappingError``
    for records that are object members (the engine refuses them) and for a record array that
    never closes (a partial document)."""
    return mp.document_wrapper(doc, cancel=cancel)


def structure_of(tree: Any) -> list[tuple]:
    """The nodes of ``tree`` without ids, expansion and statistics: two trees with the same
    structure map every document the same way."""
    return [(d, n.key, n.kind, n.src, n.synthetic, n.rec_key, n.computed, n.ctx, n.is_elem, n.is_root)
            for n, _p, d in tree.walk()]


def _doc_key(doc: Any) -> tuple:
    """(kind, record path) a designer belongs to."""
    rp = getattr(doc, "record_path", None)
    return (doc.kind, tuple(rp) if rp is not None else None)


def _state_key(doc: Any) -> tuple:
    """Identity of one data state of a document (records / wrapper cache keys)."""
    origin = getattr(doc, "origin", doc)
    return (id(origin), int(getattr(doc, "gen", 0) or 0))


@dataclass
class PreviewResult:
    """What a preview task hands to the view."""

    text: str = ""
    error: str = ""
    message: str = ""               # an inline note instead of a value (no records, member not written)
    index: int = 0                  # the record shown (0-based)
    count: int = 0                  # records of the document
    key: str | None = None          # keyed documents: the member key
    value_key: tuple | None = None  # cache key of the record value (None: too large to keep)
    value: Any = None               # the record value, only when at most VALUE_KEEP_MAX bytes (the view keeps it)
    wrapper_key: tuple | None = None
    wrapper: Any = None             # wrapped documents: the parsed wrapper (the session's state keeps it)
    edited: bool = False            # the document shows unsaved changes


def compute_preview(doc: Any, *, kind: str, record_path: tuple | None, index: int, mode: str,
                    snapshot: bytes | None, value_cache: tuple | None = None,
                    wrapper_cache: tuple | None = None, cancel: CancelToken | None = None) -> PreviewResult:
    """The preview of record ``index`` of the pinned document ``doc`` (worker threads): ``mode``
    ``out`` = the record transformed by the tree of ``snapshot`` (``engine.preview``: record
    documents ``transform([record])`` with the 1-element unwrap; wrapped documents the wrapper with
    this one record; keyed documents ``{key: member value}``; single documents the value), ``src`` =
    the record unchanged. Records above :data:`PREVIEW_MAX_BYTES` are not read
    (``UserInputError``)."""
    from jsonreshape import engine

    res = PreviewResult(edited=bool(getattr(doc, "edited", False)))
    count = int(doc.record_count)
    res.count = count
    if count <= 0:
        res.message = t("gui.mapview.preview.no_records")
        return res
    i = 0 if kind == "single" else min(max(0, int(index)), count - 1)
    res.index = i
    vkey = _state_key(doc) + (i,)
    if value_cache is not None and value_cache[0] == vkey:
        value, key = value_cache[1], value_cache[2]
        keep = True
    else:
        s, e = doc.record_span(i)
        size = e - s
        if size > PREVIEW_MAX_BYTES:
            raise UserInputError("gui.mapview.preview.too_large", record=i, size=format_bytes(size),
                                 limit=format_bytes(PREVIEW_MAX_BYTES))
        try:
            value = doc.record(i)
        except TooLargeError as exc:
            raise UserInputError("gui.mapview.preview.too_large", record=i, size=exc.params.get("size", "?"),
                                 limit=format_bytes(PREVIEW_MAX_BYTES)) from exc
        key = doc.record_key(i) if getattr(doc, "member_records", False) else None
        keep = size <= VALUE_KEEP_MAX           # a parsed record takes ~10x its text: big ones are not kept
    if keep:
        res.value_key = vkey
        res.value = value
    res.key = key
    if cancel is not None:
        cancel.check()
    wrapper = None
    rp = tuple(record_path or ())
    if kind == "wrapped":
        wkey = _state_key(doc)
        res.wrapper_key = wkey
        if wrapper_cache is not None and wrapper_cache[0] == wkey:
            wrapper = wrapper_cache[1]
        else:
            wrapper = parse_wrapper(doc, cancel)
        res.wrapper = wrapper
    if mode == "src":
        if kind == "keyed":
            shown: Any = {key if key is not None else "": value}
        elif kind == "wrapped":
            shown = place_records(wrapper, rp, [value])
        else:
            shown = value
        res.text = preview_text(shown, mode="src")
        return res
    if snapshot is None:
        res.message = t("gui.mapview.msg.not_ready")
        return res
    tree = mp.MappingTree.restore(snapshot)
    if cancel is not None:
        cancel.check()
    if kind in RECORD_KINDS:
        out = engine.preview(tree, value, limit=PREVIEW_LIMIT, records=True)
    elif kind == "wrapped":
        out = engine.CompiledMapping(tree, limit=PREVIEW_LIMIT).transform_wrapper(wrapper, [value], rp)
    elif kind == "keyed":
        r = engine.CompiledMapping(tree, limit=PREVIEW_LIMIT).transform_member(value)
        if r is engine.MISSING:
            res.message = t("gui.mapview.preview.missing_member", key=display_text(str(key)))
            return res
        out = {key if key is not None else "": r}
    else:
        out = engine.preview(tree, value, limit=PREVIEW_LIMIT, records=False)
    if cancel is not None:
        cancel.check()
    res.text = preview_text(out, mode="out")
    return res


# ==================================================================================
# building a tree (worker threads)
# ==================================================================================
@dataclass
class BuildResult:
    """A tree inferred from a document."""

    tree: Any
    wrapper: Any = None
    record_path: tuple | None = None
    kind: str = ""
    scanned: int = 0                # values the tree was inferred from
    skipped: int = 0                # huge records left out
    total: int = 0                  # records of the document
    full: bool = False
    sample: int | None = None
    state_key: tuple | None = None  # the data state read (the wrapper's cache key)


def build_tree(doc: Any, *, sample: int | None, full: bool, progress: Any = None,
               cancel: CancelToken | None = None) -> BuildResult:
    """Infer the default mapping tree of the pinned document ``doc`` from a sample of ``sample``
    records (:func:`jsonreshape.schema.sample_positions`) or, with ``full``, from every record
    (``progress`` in bytes): :func:`jsonreshape.mapping.infer_document`, the inference of the
    CLI's ``flatten`` too. Worker threads."""
    res = mp.infer_document(doc, sample=None if full else sample, progress=progress, cancel=cancel)
    return BuildResult(tree=res.tree, wrapper=res.wrapper, record_path=res.record_path, kind=doc.kind,
                       scanned=res.values, skipped=res.skipped, total=res.total, full=full,
                       sample=None if full else sample, state_key=_state_key(doc))


def make_population(doc: Any, *, sample: int | None, holder: dict) -> Callable[[], Iterator[Any]]:
    """The designer's ``population`` for a recursive flatten: the values of the same records
    the tree is made of (:func:`jsonreshape.mapping.sample_source` of the current record count,
    or every record), read from the pinned document ``doc`` by
    :func:`jsonreshape.mapping.document_values`; ``holder["cancel"]`` is the running task's
    token."""

    def population() -> Iterator[Any]:
        source = mp.sample_source(int(doc.record_count), sample)
        return mp.document_values(doc, source, cancel=holder.get("cancel"))

    return population


# ==================================================================================
# the per-session state
# ==================================================================================
class MappingState(QObject):
    """The structure builder of one session (see the module docstring).

    Signals: ``changed(bool)`` -- the rows must be rebuilt; ``True`` when the mapping itself
    changed (the preview too); ``status_changed()`` -- the phase, the source of the tree, the
    busy state or "Ixcham" changed; ``message(str)`` -- a toast (an operation's message or a
    refusal reason); ``replaced()`` -- the designer object was replaced or dropped (a new tree:
    node ids the views still hold -- a drag, an open context menu, a rename being typed --
    belong to the old one and are dropped).

    ``phase``: ``idle`` (nothing built yet), ``waiting`` (the document is not open / indexed),
    ``building``, ``ready``, ``failed`` (``error`` says why)."""

    changed = Signal(bool)
    status_changed = Signal()
    message = Signal(str)
    replaced = Signal()

    def __init__(self, session: Any) -> None:
        super().__init__()
        self._session_ref = weakref.ref(session)
        self.designer: Any = None
        self.phase = "idle"
        self.error: BaseException | None = None
        self.source = "sample"              # sample | full | file
        self.scanned = 0
        self.skipped = 0
        self.total = 0
        self.sample: int | None = None
        self.file_path: str | None = None
        self.doc_key: tuple | None = None
        self.wrapper: Any = None
        self.limits: dict[int, int] = {}
        self.compact = False
        self.cut_id: int | None = None
        self.loaded_edited = False          # a mapping file whose tree differs from its stored original
        self.input_base: dict[str, Any] = {}   # the mapping file's "input": what the tree was made from
        self.wrapper_cache: tuple | None = None   # (state key, parsed wrapper) shared by the views' previews
        self.version = 0                    # bumped whenever the mapping changes (new designer included)
        self._wanted = False                # a view wants a tree (built once the document is ready)
        self._pending_full = False
        self._task: Any = None              # build / full scan
        self._flatten_task: Any = None
        self._flatten_id: int | None = None
        self._io_tasks: list[Any] = []
        self._snap: tuple[int, bytes] | None = None
        self._opens = 0                     # "opening" states seen (a reopen replaces the document)
        self._made_for = -1                 # the open the designer belongs to
        self._failed_for = -1               # the open whose build failed (retry() builds again)
        self._rebuilt_note = False
        session.state_changed.connect(self._on_session_state)
        session.index_finished.connect(self._on_index_finished)
        data_changed = getattr(session, "data_changed", None)
        if data_changed is not None and hasattr(data_changed, "connect"):
            data_changed.connect(self._on_data_changed)

    # -- basics ---------------------------------------------------------------------------
    @property
    def session(self) -> Any:
        return self._session_ref()

    @property
    def tree(self) -> Any:
        return self.designer.tree if self.designer is not None else None

    @property
    def busy(self) -> bool:
        """The operations wait: a recursive flatten is being computed, or every record is being
        scanned ("To'liq skanerlash" replaces the tree when it is done: a change made meanwhile
        would be lost)."""
        return self._flatten_task is not None or self.full_running

    @property
    def flattening(self) -> bool:
        """A recursive flatten is being computed."""
        return self._flatten_task is not None

    @property
    def building(self) -> bool:
        return self._task is not None

    @property
    def can_reset(self) -> bool:
        """"Asl holat" can change something: there are undoable steps (the prototype's rule), or
        the tree came from a mapping file whose stored original differs from it (§13.1: the
        file keeps the original so "Asl holat" works after reopening a saved mapping)."""
        d = self.designer
        return d is not None and not self.busy and (d.can_undo or self.loaded_edited)

    @property
    def full_running(self) -> bool:
        return self._task is not None and self._pending_full

    def _doc(self) -> Any:
        s = self.session
        if s is None or getattr(s, "closed", False):
            return None
        return s.doc

    def doc_ready(self) -> bool:
        """The document is open and indexed (a tree can be built from it)."""
        s = self.session
        doc = self._doc()
        return doc is not None and s.state == "ready" and bool(getattr(doc, "index_complete", False))

    def record_count(self) -> int:
        doc = self._doc()
        try:
            return int(doc.record_count) if doc is not None else 0
        except Exception:  # pragma: no cover - a closing document
            return 0

    def doc_kind(self) -> str | None:
        doc = self._doc()
        return doc.kind if doc is not None else None

    def record_path(self) -> tuple | None:
        doc = self._doc()
        rp = getattr(doc, "record_path", None) if doc is not None else None
        return tuple(rp) if rp is not None else None

    def _bump(self, mapping: bool = True) -> None:
        if mapping:
            self.version += 1
            self._snap = None
        self.changed.emit(bool(mapping))

    def set_compact(self, on: bool) -> None:
        """"Ixcham" (every view of the session shows the same box)."""
        if bool(on) != self.compact:
            self.compact = bool(on)
            self.status_changed.emit()

    def _set_designer(self, designer: Any) -> None:
        """Replace (or drop) the designer: views forget what they hold of the old tree."""
        if designer is self.designer:
            return
        self.designer = designer
        self.cut_id = None
        self.replaced.emit()

    def _say(self, text: str) -> None:
        if not text:
            return
        text = display_text(text)               # node keys may hold line breaks / bidi controls
        self.message.emit(text)
        s = self.session
        app = getattr(s, "app", None) if s is not None else None
        show = getattr(app, "show_status", None)
        if callable(show):
            show(text, 4200)

    # -- building ----------------------------------------------------------------------------
    def ensure(self) -> None:
        """A view became visible: build the tree from the sample when there is none yet (as soon
        as the document is ready)."""
        self._wanted = True
        if self.designer is not None or self._task is not None:
            return
        s = self.session
        if s is None or getattr(s, "closed", False):
            return
        if not self.doc_ready():
            new = "failed" if s.state == "failed" else "waiting"
            if self.phase != new:
                self.phase = new
                self.status_changed.emit()
            return
        if self.phase == "failed" and self._failed_for == self._opens:
            return                          # retry() builds again
        self.build(full=False)

    def retry(self) -> None:
        if self.phase == "failed":
            self.phase = "idle"
        self.error = None
        self.build(full=self._pending_full)

    def build(self, *, full: bool = False) -> Any:
        """Infer the tree from the sample (a silent task) or from every record (``full``: a
        visible heavy task with progress); the result replaces the designer."""
        s = self.session
        if s is None or getattr(s, "closed", False) or not self.doc_ready():
            return None
        if self._task is not None:
            self._task.cancel()
            self._task = None
        doc = s.doc.pinned()
        sample = None if full else max(1, int(s.app.settings.sample_size))
        self._pending_full = bool(full)
        name = s.name
        title = t("gui.mapview.task.full", name=name) if full else t("gui.mapview.task.build", name=name)

        def work(ctx: Any) -> BuildResult:
            return build_tree(doc, sample=sample, full=full, progress=ctx.progress if full else None,
                              cancel=ctx.cancel)

        task_holder: list[Any] = []

        def done(result: BuildResult) -> None:
            if not task_holder or self._task is not task_holder[0]:
                return
            self._task = None
            self._install_built(result)

        def failed(exc: BaseException) -> None:
            if not task_holder or self._task is not task_holder[0]:
                return
            self._task = None
            self.error = exc
            if self.designer is None:
                self.phase = "failed"
                self._failed_for = self._opens
            else:
                self.phase = "ready"            # a failed full scan keeps the tree on screen
                s2 = self.session
                if s2 is not None:
                    s2.app.report_error(exc)
            self._pending_full = False
            self.status_changed.emit()

        def cancelled() -> None:
            if not task_holder or self._task is not task_holder[0]:
                return
            self._task = None
            self._pending_full = False
            if self.designer is None:
                self.phase = "idle"
                self._wanted = False
            self.status_changed.emit()

        task = s.app.tasks.run(work, title=title, kind="mapping", owner=s, on_done=done, on_error=failed,
                               on_cancel=cancelled, silent=not full, heavy=full)
        task_holder.append(task)
        self._task = task
        if self.designer is None:
            self.phase = "building"
        self.error = None
        self.status_changed.emit()
        return task

    def _install_built(self, result: BuildResult) -> None:
        s = self.session
        if s is None or getattr(s, "closed", False):
            return
        self._new_designer(result.tree, wrapper=result.wrapper, record_path=result.record_path)
        self.source = "full" if result.full else "sample"
        self.sample = result.sample
        self.scanned = result.scanned
        self.skipped = result.skipped
        self.total = result.total
        self.file_path = None
        doc = self._doc()
        self.input_base = mp.make_input_info(doc, sample=result.sample, full_scan=result.full) if doc is not None \
            else {}
        if result.wrapper is not None and result.state_key is not None:
            self.wrapper_cache = (result.state_key, result.wrapper)
        self._pending_full = False
        self.phase = "ready"
        self.error = None
        if self._rebuilt_note:
            self._rebuilt_note = False
            self._say(t("gui.mapview.msg.rebuilt"))
        self.status_changed.emit()
        self._bump(True)

    def _new_designer(self, tree: Any, *, wrapper: Any = None, record_path: tuple | None = None) -> None:
        doc = self._doc()
        s = self.session
        limit = int(s.app.settings.undo_limit) if s is not None else None
        self._set_designer(mp.Designer(tree, population=None, wrapper=wrapper, record_path=record_path,
                                       undo_limit=limit))
        self.loaded_edited = False
        self.wrapper = wrapper
        self.doc_key = _doc_key(doc) if doc is not None else None
        self._made_for = self._opens
        self.limits = {}

    def full_scan(self) -> Any:
        """"To'liq skanerlash": infer the tree from every record (the designer is replaced when
        done; the views ask first when it has changes)."""
        return self.build(full=True)

    # -- session events --------------------------------------------------------------------
    def _on_session_state(self, state: str) -> None:
        if state == "opening":
            self._opens += 1
        if state in ("opening", "closed"):
            # the session detached its own tasks (they read the old document): forget them
            self._task = None
            self._flatten_task = None
            self._flatten_id = None
            self._io_tasks = []
            self._pending_full = False
            self.wrapper_cache = None           # parsed from the old document
            if state == "closed":
                self._set_designer(None)
                self.phase = "idle"
            elif self.phase in ("building", "failed"):
                self.phase = "idle"
            self.status_changed.emit()
        elif state == "failed" and self.designer is None:
            self.phase = "failed"
            self.status_changed.emit()

    def _on_index_finished(self) -> None:
        s = self.session
        doc = self._doc()
        if s is None or doc is None:
            return
        if self.designer is not None and self._made_for != self._opens:
            same = _doc_key(doc) == self.doc_key
            keep = same and (self.designer.can_undo or self.source in ("file", "full"))
            if keep:
                self._made_for = self._opens
                self.total = self.record_count()
                self.status_changed.emit()
                self._bump(True)
                return
            if not same and self.designer.can_undo:
                self._rebuilt_note = True
            self._set_designer(None)
            self.phase = "idle"
            self._bump(True)
            self.status_changed.emit()
            if self._wanted and self.doc_ready():
                self.build(full=False)
            return
        if self.designer is None and self._wanted and self.doc_ready() and self._task is None:
            if self.phase != "failed" or self._failed_for != self._opens:
                self.build(full=False)
        else:
            self.status_changed.emit()

    def _on_data_changed(self, *_args: Any) -> None:
        """Edits (§12.9): the tree stays; the rows are built again (the root row's record count
        follows; the header keeps saying how many records the tree was inferred from), the
        previews read the new state themselves."""
        self.wrapper_cache = None
        if self.designer is not None:
            self.changed.emit(False)

    # -- operations ------------------------------------------------------------------------
    def can_operate(self) -> str | None:
        """``None`` when an operation may run now, else why not."""
        if self.designer is None:
            return t("gui.mapview.why.no_tree")
        if self.full_running:
            return t("gui.mapview.why.scanning")
        if self._flatten_task is not None:
            return t("gui.mapview.why.busy")
        return None

    def run(self, op: str, *args: Any) -> Any:
        """A designer operation (``move``, ``delete``, ``unwrap``, ``add_group``, ``rename``,
        ``move_up``, ``move_down``, ``undo``, ``redo``, ``reset``): the result's message (or the
        refusal reason) is told; the views are rebuilt when it applied. ``None`` when no
        operation can run now (the reason is told)."""
        why = self.can_operate()
        if why is not None:
            self._say(why)
            return None
        fn = getattr(self.designer, op)
        try:
            res = fn(*args)
        except Exception as exc:   # pragma: no cover - a designer bug must not break the GUI
            log.exception("designer operation %s failed", op)
            s = self.session
            if s is not None:
                s.app.report_error(exc)
            return None
        if op in ("move", "move_up", "move_down", "delete", "unwrap") and res.applied:
            self.cut_id = None
        if res.applied:
            self._bump(True)
            self._say(res.message)
        else:
            self._say(res.reason)
        return res

    def undo(self) -> Any:
        return self.run("undo")

    def redo(self) -> Any:
        return self.run("redo")

    def reset(self) -> Any:
        return self.run("reset")

    def set_open(self, node_id: int, open_: bool) -> None:
        if self.designer is None:
            return
        n = self.designer.tree.find(node_id)
        if n is None or bool(n.open) == bool(open_):
            return
        self.designer.set_open(node_id, open_)
        self._bump(False)

    def toggle(self, node_id: int) -> None:
        if self.designer is None:
            return
        n = self.designer.tree.find(node_id)
        if n is not None and n.children:
            self.set_open(node_id, not n.open)

    def expand_all(self) -> None:
        if self.designer is not None:
            self.designer.expand_all()
            self._bump(False)

    def collapse_all(self) -> None:
        if self.designer is not None:
            self.designer.collapse_all()
            self._bump(False)

    def expand_to_level(self, node_id: int | None, levels: int) -> None:
        """``levels`` levels below the node ``node_id`` (default: the root) open, the deeper ones
        closed; its ancestors open. Like the other expansions: no undo step."""
        if self.designer is None:
            return
        tree = self.designer.tree
        top = tree.find(int(node_id)) if node_id is not None else None
        if top is None:
            top = tree.root
        stack = [(top, 0)]
        while stack:
            n, depth = stack.pop()
            n.open = depth < levels
            stack.extend((c, depth + 1) for c in n.children)
        p = tree.parent(top)
        while p is not None:
            p.open = True
            p = tree.parent(p)
        self._bump(False)

    def show_more(self, node_id: int) -> None:
        """The "yana N ta maydonni ko'rsatish" row: :data:`MORE_STEP` more children."""
        self.limits[int(node_id)] = self.limits.get(int(node_id), MORE_STEP) + MORE_STEP
        self._bump(False)

    def set_cut(self, node_id: int | None) -> None:
        if node_id != self.cut_id:
            self.cut_id = node_id
            self._bump(False)

    # -- recursive flatten -----------------------------------------------------------------
    def flatten(self, node_id: int) -> Any:
        """Rekursiv tekislash: the read part (re-inference over the sample / every record) runs in
        a task; the designer applies its plan in the GUI thread when it is done (a plan for a tree
        that changed meanwhile is refused with the reason)."""
        why = self.can_operate()
        if why is not None:
            self._say(why)
            return None
        d = self.designer
        if not d.can_flatten(node_id):
            self._say(t("mapping.why.not_flattenable"))
            return None
        s = self.session
        if s is None or not self.doc_ready():
            self._say(t("gui.mapview.why.no_tree"))
            return None
        doc = s.doc.pinned()
        kind = doc.kind
        full = self.source == "full"
        sample = None if full else max(1, int(self.sample or s.app.settings.sample_size))
        holder: dict[str, Any] = {}
        population = make_population(doc, sample=sample, holder=holder)
        node = d.tree.find(node_id)
        key = node.key if node is not None and not node.is_root else s.name

        def work(ctx: Any) -> Any:
            holder["cancel"] = ctx.cancel
            # only this task uses the designer's population / wrapper (operations wait meanwhile)
            if kind == "wrapped":
                d.wrapper = parse_wrapper(doc, ctx.cancel)
            d.population = population
            return d.plan_flatten(node_id, progress=ctx.progress, cancel=ctx.cancel)

        task_holder: list[Any] = []

        def finish() -> bool:
            if not task_holder or self._flatten_task is not task_holder[0]:
                return False
            self._flatten_task = None
            self._flatten_id = None
            self.status_changed.emit()
            return True

        def done(plan: Any) -> None:
            if not finish() or self.designer is not d:
                return
            if plan is None:
                self._say(t("mapping.why.not_flattenable"))
                return
            res = d.flatten(node_id, plan=plan)
            if res.applied:
                self._bump(True)
                self._say(res.message)
            else:
                self._say(res.reason)

        def failed(exc: BaseException) -> None:
            if finish():
                s2 = self.session
                if s2 is not None:
                    s2.app.report_error(exc)

        def cancelled() -> None:
            finish()

        task = s.app.tasks.run(work, title=t("gui.mapview.task.flatten", name=display_text(key)), kind="mapping",
                               owner=s, on_done=done, on_error=failed, on_cancel=cancelled, silent=not full,
                               heavy=full, unit="items")
        task_holder.append(task)
        self._flatten_task = task
        self._flatten_id = int(node_id)
        self.status_changed.emit()
        self._say(t("gui.mapview.msg.flattening", key=display_text(key)))
        return task

    # -- snapshots (previews, saves, exports) ---------------------------------------------------
    def snapshot(self) -> bytes | None:
        """The current tree as snapshot bytes (cached per :attr:`version`; GUI thread)."""
        if self.designer is None:
            return None
        if self._snap is None or self._snap[0] != self.version:
            self._snap = (self.version, self.designer.tree.snapshot())
        return self._snap[1]

    def tree_copy(self) -> Any:
        """A private copy of the current tree (export / tests)."""
        snap = self.snapshot()
        return mp.MappingTree.restore(snap) if snap is not None else None

    def stats_text(self) -> str:
        if self.designer is None:
            return ""
        return mp.stats_text(self.designer.tree, self.designer.changes)

    def input_info(self) -> dict[str, Any]:
        """The mapping file's ``input`` section: what the tree was made from -- taken when the
        tree was built or opened (a save while the document is being reopened, or of a tree
        opened from a file made for another record path, describes the tree, not the document of
        the moment) -- with the time and the version of this save."""
        from jsonreshape import __version__

        info = dict(self.input_base)
        if not info:
            doc = self._doc()
            if doc is None:
                return {}
            info = mp.make_input_info(doc, sample=None if self.source == "full" else self.sample,
                                      full_scan=self.source == "full")
        info["created"] = datetime.now().astimezone().isoformat(timespec="seconds")
        info["app"] = __version__
        return info

    # -- mapping files -----------------------------------------------------------------------
    def save_mapping(self, path: str, on_done: Callable[[bool], Any] | None = None) -> Any:
        """Write the mapping file (a silent task; the original tree is kept for "Asl holat")."""
        s = self.session
        if self.designer is None or s is None:
            self._say(t("gui.mapview.why.no_tree"))
            return None
        snap = self.snapshot()
        orig = self.designer.orig
        info = self.input_info()
        path = os.path.abspath(path)

        def work(_ctx: Any) -> str:
            tree = mp.MappingTree.restore(snap)
            mp.save_mapping(tree, path, input_info=info, orig=orig)
            return path

        def done(p: str) -> None:
            self.file_path = p
            self._say(t("gui.mapview.msg.saved", path=os.path.normpath(p)))
            self.status_changed.emit()
            if on_done is not None:
                on_done(True)

        def failed(exc: BaseException) -> None:
            s2 = self.session
            if s2 is not None:
                s2.app.report_error(exc)
            if on_done is not None:
                on_done(False)

        task = s.app.tasks.run(work, title=t("gui.mapview.task.save", name=os.path.basename(path)), kind="mapping",
                               owner=self, on_done=done, on_error=failed, silent=True)
        self._keep_io(task)
        return task

    def open_mapping(self, path: str, on_done: Callable[[bool], Any] | None = None) -> Any:
        """Read a mapping file (a silent task) and make it the designer's tree when it fits the
        document (``engine.check_kind``; a wrapped mapping made for another record path is accepted
        with the engine's warning). Errors are reported; the current tree stays then."""
        s = self.session
        if s is None or not self.doc_ready():
            self._say(t("gui.mapview.why.no_tree"))
            return None
        path = os.path.abspath(path)
        doc = s.doc.pinned()

        def work(ctx: Any) -> tuple[Any, dict, Any, bool, tuple]:
            tree, info = mp.load_mapping(path)
            wrapper = parse_wrapper(doc, ctx.cancel) if doc.kind == "wrapped" else None
            edited = tree.orig is not None and structure_of(tree) != structure_of(tree.orig)
            return tree, info, wrapper, edited, _state_key(doc)

        def done(result: tuple[Any, dict, Any, bool, tuple]) -> None:
            tree, info, wrapper, edited, key = result
            ok = self._install_loaded(tree, wrapper, path, edited=edited, info=info)
            if ok and wrapper is not None:
                self.wrapper_cache = (key, wrapper)
            if on_done is not None:
                on_done(ok)

        def failed(exc: BaseException) -> None:
            s2 = self.session
            if s2 is not None:
                s2.app.report_error(exc)
            if on_done is not None:
                on_done(False)

        task = s.app.tasks.run(work, title=t("gui.mapview.task.open", name=os.path.basename(path)), kind="mapping",
                               owner=s, on_done=done, on_error=failed, silent=True)
        self._keep_io(task)
        return task

    def _keep_io(self, task: Any) -> None:
        self._io_tasks = [x for x in self._io_tasks if getattr(x, "state", "") == "running"] + [task]

    def install_tree(self, tree: Any, *, wrapper: Any = None, label: str = "", edited: bool = False,
                     info: dict | None = None) -> bool:
        """Put a mapping tree that was read elsewhere (an opened mapping file, a workspace's embedded mapping)
        into the builder the way "Open mapping…" does: the document-kind check (an error is reported, False),
        the wrapper of wrapped documents, the file's ``input`` info, "Asl holat" from the stored original.
        ``label`` names its source (a path or a workspace)."""
        return self._install_loaded(tree, wrapper, label, edited=edited, info=info)

    def _install_loaded(self, tree: Any, wrapper: Any, path: str, *, edited: bool = False,
                        info: dict | None = None) -> bool:
        from jsonreshape import engine

        s = self.session
        doc = self._doc()
        if s is None or doc is None:
            return False
        try:
            engine.check_kind(doc, tree)
        except JsonReshapeError as exc:
            s.app.report_error(exc)
            return False
        if self._task is not None:
            self._task.cancel()
            self._task = None
            self._pending_full = False
        rp = self.record_path()
        self._new_designer(tree, wrapper=wrapper, record_path=rp)
        self.loaded_edited = bool(edited)
        # the file's own "input" (the tree was made from it; another record path stays recorded)
        self.input_base = dict(info) if isinstance(info, dict) and info.get("kind") else \
            mp.make_input_info(doc, sample=None, full_scan=False)
        self.source = "file"
        self.file_path = path
        self.phase = "ready"
        self.error = None
        self.scanned = self.skipped = 0
        self.total = self.record_count()
        rs = getattr(tree, "record_src", None)
        if doc.kind == "wrapped" and rs is not None and rp is not None:
            pattern = tuple(mp.WILD if isinstance(x, int) and not isinstance(x, bool) else x for x in rp)
            if tuple(rs) != pattern:
                self._say(t("engine.warn.record_path", mapping=mp.path_label(tuple(rs), root=""),
                            input=mp.path_label(pattern, root="")))
        self.status_changed.emit()
        self._bump(True)
        self._say(t("gui.mapview.msg.loaded", path=os.path.normpath(path)))
        return True

    # -- lifetime --------------------------------------------------------------------------
    def shutdown(self) -> None:
        for task in [self._task, self._flatten_task, *self._io_tasks]:
            if task is not None:
                try:
                    task.cancel()
                except Exception:  # pragma: no cover - defensive
                    pass
        self._task = self._flatten_task = None
        self._io_tasks = []


_states: "weakref.WeakKeyDictionary[Any, MappingState]" = weakref.WeakKeyDictionary()


def state_for(session: Any) -> MappingState:
    """The session's :class:`MappingState` (created on first use; one per session, so every view
    of the session shows the same designer)."""
    state = _states.get(session)
    if state is None:
        state = MappingState(session)
        _states[session] = state
    return state


def forget_state(session: Any) -> None:
    """Drop the session's state (tests)."""
    state = _states.pop(session, None)
    if state is not None:
        state.shutdown()
