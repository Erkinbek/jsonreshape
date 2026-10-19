"""Compare windows: two nodes or two files side by side (docs/ARCHITECTURE.md §14.9 "Compare";
design: scratchpad ``s5/map_repair_diff.md`` §4.7; the core: :mod:`jsonreshape.diff`).

* :class:`CompareWindow` -- a non-modal top-level window.

  - **Node mode** (:meth:`CompareWindow.compare_nodes`): two nodes -- of one document or of two
    open tabs -- in a :class:`~jsonreshape.gui.diffview.DiffView`. Each side is read from the
    pinned state its tab showed when the comparison was asked for (an edited document is
    compared as shown, docs/ARCHITECTURE.md §12.3); nodes up to
    :data:`~jsonreshape.gui.diffview.FULL_VALUE_MAX` are parsed in a worker for the complete
    tree, bigger ones go through :func:`~jsonreshape.diff.diff_nodes` (memory-bounded Node API)
    and show only the differences.
  - **File mode** (:meth:`CompareWindow.compare_files`): :func:`~jsonreshape.diff.diff_files`
    runs as a heavy task (progress in bytes of A + B, Cancel) with ``results=True`` and
    ``max_details=0`` (every pair is decided exactly, the walk stops at the first change); the
    results grid on top reads the compact results file window by window and computes the
    details of the visible rows (key, number of changes, first path) with
    :func:`~jsonreshape.diff.record_diff` in a :class:`~jsonreshape.gui.loader.RowLoader`; the
    DiffView below shows the selected pair. Filters (changed / added / removed), "open as a
    record set" (A: changed + removed positions, B: changed + added -- ``session.show_records``
    with the run's gen), "export the report" (the diff again through
    :func:`~jsonreshape.diff.iter_record_diffs` + :func:`~jsonreshape.diff.write_diff`, JSON /
    JSON Lines / CSV; a report file another tab shows is refused while that tab has changes or
    the file's unsaved changes are kept for recovery, else asked about, and that tab opens the
    report afterwards: :func:`~jsonreshape.gui.searchpanel.open_target_check`, review S3),
    "show in the tree" of the A / B tab (a callback), swap A ⇄ B and
    options (both run the comparison again). Two single documents (or the "document" method)
    are compared as nodes (their roots). A side can be an open tab (its pinned snapshot; the
    document stays open for the run with ``keep_document_for``) or a file the window opens
    itself (closed with the window).
  - **Stale results**: ``data_changed`` / a reopen / Save As / closing the tab of a side
    shows a bar "… changed: the results are out of date" with Refresh (a closed tab's side
    is compared from its file); nothing is read from a closed session again. Closing the
    window detaches its tasks and removes the results file once the readers stopped.

* :class:`CompareFilesDialog` -- A and B (the current tab, another open tab, or a file), the
  method (auto / key / align / position / document), the record key (filled from the schema
  fields of a tab; ``id`` preselected when present), the array matching and "numbers as
  written".

* The compare mark: :func:`mark_for_compare` remembers one node per application (a weak
  reference to its session); :func:`compare_with_marked` compares another node with it.

Integration API (round B wiring; every function returns the window / dialog or ``None`` and
reports problems through ``app.report_error``): :func:`show_compare_files`,
:func:`compare_nodes`, :func:`compare_selected`, :func:`mark_for_compare`,
:func:`compare_with_marked`, :func:`marked`, :func:`clear_mark`, :func:`node_menu_items`
(ready-made ``actions.node_menu(extra=...)`` items).

Decisions: the record set of B holds its changed AND added records (A: changed and removed),
so both tabs can show every record that differs; in file mode the array matching of the
detail view is the run's (changed through "Options…", which runs the comparison again) so
the grid and the details always agree; F7 / Shift+F7 at the last / first difference of a
record go on to the next / previous record of the results.
"""

from __future__ import annotations

import logging
import os
import threading
import weakref
from array import array
from dataclasses import dataclass, field, replace
from typing import Any, Callable, Iterable

import shiboken6
from PySide6.QtCore import QPoint, Qt, QTimer, Signal
from PySide6.QtGui import QKeySequence, QShortcut
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFileDialog,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMenu,
    QProgressBar,
    QPushButton,
    QSplitter,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from jsonreshape import codec
from jsonreshape.diff import Change, DiffOptions, DiffReport, ValueDiff, diff_files, diff_nodes
from jsonreshape.diff import iter_record_diffs, record_diff, write_diff
from jsonreshape.dupes import KEY_FIELD, parse_key
from jsonreshape.errors import Cancelled, JsonReshapeError, TooLargeError, UserInputError
from jsonreshape.fileio import read_range
from jsonreshape.gui.difftree import ABSENT, STATUS_ROLES, UNKNOWN, DiffRow, DiffTreeModel, count_text
from jsonreshape.gui.difftree import value_kind
from jsonreshape.gui.diffview import FULL_VALUE_MAX, NEXT_KEY, PREV_KEY, TEXT_CAP, DiffView, PairSource
from jsonreshape.gui.diffview import SideText, gui_options
from jsonreshape.gui.loader import LoadFailed, RowLoader
from jsonreshape.gui.session import NodeRef
from jsonreshape.gui.vgrid import Cell, Column, VirtualGrid
from jsonreshape.gui.widgets import ElidedLabel, InfoBar, format_count, keep_alive, pretty_prefix, task_doc, task_node
from jsonreshape.i18n import t
from jsonreshape.paths import MISSING, format_path
from jsonreshape.reader import Document, split_member
from jsonreshape.recordset import RecordSet
from jsonreshape.tasks import CancelToken, check

log = logging.getLogger(__name__)

__all__ = [
    "CompareFilesDialog",
    "CompareMark",
    "CompareSpec",
    "CompareWindow",
    "FileSide",
    "NodePairSource",
    "NodeSide",
    "RecordPairSource",
    "capture_node_side",
    "clear_mark",
    "compare_nodes",
    "compare_selected",
    "compare_with_marked",
    "mark_for_compare",
    "marked",
    "node_label",
    "node_menu_items",
    "show_compare_files",
]

FILE_MODES = ("auto", "key", "align", "position", "document")
ARRAY_MODES = ("auto", "index", "unordered", "key")
STATUSES = ("changed", "added", "removed")
LOADER_BATCH = 32
DETAIL_DELAY_MS = 120         # the detail view follows the selected result row after this pause
RECORDS_SCAN = 1 << 16        # results entries read at once when filtering / building record sets
EXPORT_FORMATS = {".json": "json", ".jsonl": "jsonl", ".ndjson": "jsonl", ".csv": "csv"}

Reveal = Callable[[Any, NodeRef], Any]


def _alive(obj: Any) -> bool:
    try:
        return obj is not None and bool(shiboken6.isValid(obj))
    except Exception:   # pragma: no cover - shiboken always there with PySide6
        return obj is not None


def _session_ok(session: Any) -> bool:
    return session is not None and _alive(session) and not getattr(session, "closed", False)


def _error_text(exc: BaseException) -> str:
    if isinstance(exc, JsonReshapeError):
        return exc.message
    return t("gui.diff.unexpected")


def node_label(session: Any, ref: NodeRef) -> str:
    """``people.json · #12.address`` (the tab name and the location of the node)."""
    try:
        where = session.location_text(ref)
    except Exception:   # a fake / closed session
        where = format_path(tuple(ref.path)) or "$"
    return f"{session.name} · {where}"


# ==================================================================================
# node mode: sides and their source
# ==================================================================================
@dataclass(eq=False)
class NodeSide:
    """One compared node: its session (``None``: a document the window opened), the node
    reference, the pinned document it is read from (taken when the comparison was asked
    for), labels and the session's data gen at that moment."""

    session: Any
    ref: NodeRef
    doc: Any
    name: str
    location: str = "$"
    gen: int = 0

    @property
    def label(self) -> str:
        return f"{self.name} · {self.location}"

    def node(self, cancel: CancelToken | None) -> Any:
        """The node (worker threads)."""
        if self.session is not None:
            return task_node(self.session, self.ref, cancel, doc=self.doc)
        return _node_in(self.doc, self.ref, cancel)


def _node_in(doc: Any, ref: NodeRef, cancel: CancelToken | None) -> Any:
    """The node of ``ref`` in ``doc`` (no session; worker threads)."""
    cur = doc.root() if ref.record is None else doc.record_node(int(ref.record))
    for seg in ref.path:
        check(cancel)
        nxt = cur.find_child(seg, cancel=cancel)
        if nxt is None:
            raise UserInputError("reader.path_not_found", path=format_path(tuple(ref.path)) or "$")
        cur = nxt
    return cur


def capture_node_side(session: Any, ref: NodeRef) -> NodeSide:
    """The side of ``ref`` in the state ``session`` shows NOW (GUI thread; ``UserInputError``
    while the document is not ready)."""
    if not _session_ok(session) or getattr(session, "doc", None) is None:
        raise UserInputError("gui.compare.error.not_ready", name=getattr(session, "name", "?"))
    doc = task_doc(session)
    if doc is None:
        raise UserInputError("gui.compare.error.not_ready", name=session.name)
    try:
        location = session.location_text(ref)
    except Exception:
        location = format_path(tuple(ref.path)) or "$"
    return NodeSide(session, ref, doc, session.name, location, int(getattr(session, "data_gen", 0) or 0))


class NodePairSource(PairSource):
    """Two nodes (:class:`NodeSide`)."""

    def __init__(self, a: NodeSide, b: NodeSide) -> None:
        self.a = a
        self.b = b
        self.a_name = a.label
        self.b_name = b.label

    def load(self, opts: DiffOptions, swapped: bool, cancel: CancelToken) -> DiffTreeModel:
        sa, sb = (self.b, self.a) if swapped else (self.a, self.b)
        na = sa.node(cancel)
        nb = sb.node(cancel)
        vd = diff_nodes(sa.doc, na, sb.doc, nb, gui_options(opts), cancel=cancel)
        va: Any = UNKNOWN
        vb: Any = UNKNOWN
        if na.size <= FULL_VALUE_MAX and nb.size <= FULL_VALUE_MAX:
            try:
                va = na.value()
                vb = nb.value()
            except TooLargeError:
                va = vb = UNKNOWN
        check(cancel)
        return DiffTreeModel(va, vb, vd, opts=opts, a_kind=na.kind, b_kind=nb.kind)

    def texts(self, swapped: bool, cancel: CancelToken) -> tuple[SideText, SideText]:
        out = []
        for side in ((self.b, self.a) if swapped else (self.a, self.b)):
            node = side.node(cancel)
            data, complete = pretty_prefix(node, 2, TEXT_CAP, cancel)
            out.append(SideText(data.decode("utf-8", errors="ignore"), not complete, int(node.size)))
        return out[0], out[1]


# ==================================================================================
# file mode: the state a run leaves, its sources
# ==================================================================================
class _RunState:
    """What a finished file comparison leaves for the results grid and the detail view: plain
    Python shared with worker threads (never a QObject). Readers call :meth:`use` / :meth:`done`
    around their work; :meth:`release` (window closed, another run) waits for them in a daemon
    thread, then removes the results file and closes the documents the window opened."""

    def __init__(self, report: DiffReport | None, doc_a: Any, doc_b: Any, a_name: str, b_name: str,
                 owned: Iterable[Any] = ()) -> None:
        self.report = report
        self.doc_a = doc_a
        self.doc_b = doc_b
        self.a_name = a_name
        self.b_name = b_name
        self.owned = [d for d in owned if d is not None]
        self.cond = threading.Condition()
        self.users = 0
        self.alive = True
        self.released = threading.Event()
        key = report.key if report is not None else None
        value_only = key == KEY_FIELD
        self.wrap_a = bool(getattr(doc_a, "member_records", False)) and not value_only
        self.wrap_b = bool(getattr(doc_b, "member_records", False)) and not value_only

    def use(self) -> bool:
        with self.cond:
            if not self.alive:
                return False
            self.users += 1
            return True

    def done(self) -> None:
        with self.cond:
            self.users -= 1
            self.cond.notify_all()

    def release(self) -> None:
        """Stop the readers, then remove the results file and close the owned documents
        (a daemon thread; idempotent)."""
        with self.cond:
            if not self.alive:
                return
            self.alive = False

        def run() -> None:
            with self.cond:
                while self.users > 0:
                    self.cond.wait(1.0)
            rep, self.report = self.report, None
            if rep is not None:
                rep.close()
            for doc in self.owned:
                try:
                    doc.close()
                except Exception:   # pragma: no cover - best effort
                    log.debug("closing %r failed", doc, exc_info=True)
            self.owned = []
            self.released.set()

        threading.Thread(target=run, name="jsonreshape-compare-release", daemon=True).start()

    def check_open(self) -> None:
        """``UserInputError`` when a compared document was closed (its tab closed / reopened)."""
        for doc, name in ((self.doc_a, self.a_name), (self.doc_b, self.b_name)):
            if getattr(doc, "closed", False):
                raise UserInputError("gui.compare.error.closed", name=name)

    def results(self, start: int, count: int) -> list[tuple[str, int | None, int | None]]:
        if not self.use():
            raise Cancelled()
        try:
            rep = self.report
            return rep.read_results(start, count) if rep is not None else []
        finally:
            self.done()


def _record_value(doc: Any, pos: int) -> tuple[Any, str | None, str]:
    """``(value or UNKNOWN when too large to load, member key or None, kind)`` of a record."""
    s, e = doc.record_span(pos)
    if e - s > FULL_VALUE_MAX:
        node = doc.record_node(pos)
        return UNKNOWN, (doc.record_key(pos) if doc.member_records else None), node.kind
    raw = doc.record_bytes(pos)
    if doc.member_records:
        k, vo = split_member(raw)
        v = codec.loads(raw[vo:])
    else:
        k, v = None, codec.loads(raw)
    return v, k, value_kind(v) or "null"


def _strip(vd: ValueDiff) -> ValueDiff:
    """The changes with the first path segment (the member key of both records) removed."""
    out = ValueDiff(truncated=vd.truncated, counts=dict(vd.counts))
    out.changes = [Change(c.op, None if c.a_path is None else c.a_path[1:],
                          None if c.b_path is None else c.b_path[1:], c.a, c.b) for c in vd.changes]
    return out


def _key_text(key: Any) -> str:
    if key is MISSING or key is None:
        return ""
    if isinstance(key, str):
        return key
    try:
        text = codec.dumps(key).decode("utf-8", errors="replace")
    except (TypeError, ValueError):   # pragma: no cover - codec values
        text = repr(key)
    return text if len(text) <= 120 else text[:119] + "…"


class RecordPairSource(PairSource):
    """One entry of a file comparison's results: a record pair, or a record only in A / B."""

    can_swap = False

    def __init__(self, state: _RunState, entry: int, hint: tuple[str, int | None, int | None] | None = None) -> None:
        self.state = state
        self.entry = entry
        self.triple = hint
        self._name_parts()

    def _name_parts(self) -> None:
        st = self.state
        if self.triple is None:
            self.a_name, self.b_name = st.a_name, st.b_name
            return
        _status, a, b = self.triple
        self.a_name = f"{st.a_name} #{a}" if a is not None else f"{st.a_name} —"
        self.b_name = f"{st.b_name} #{b}" if b is not None else f"{st.b_name} —"

    def _entry(self) -> tuple[str, int | None, int | None]:
        if self.triple is None:
            got = self.state.results(self.entry, 1)
            if not got:
                raise UserInputError("gui.compare.error.results_gone")
            self.triple = got[0]
            self._name_parts()
        return self.triple

    def load(self, opts: DiffOptions, swapped: bool, cancel: CancelToken) -> DiffTreeModel:
        st = self.state
        status, a, b = self._entry()
        if not st.use():
            raise Cancelled()
        try:
            rep = st.report
            if rep is None:
                raise Cancelled()
            st.check_open()
            run_opts = rep.options
            rd = record_diff(st.doc_a, a, st.doc_b, b, run_opts, key=rep.key, cancel=cancel)
            vd = rd.diff or ValueDiff()
            va: Any = ABSENT
            vb: Any = ABSENT
            ka = kb = None
            kind_a = kind_b = None
            if a is not None:
                va, ka, kind_a = _record_value(st.doc_a, a)
            if b is not None:
                vb, kb, kind_b = _record_value(st.doc_b, b)
        finally:
            st.done()
        label = f"#{a} ↔ #{b}" if a is not None and b is not None else f"#{a if a is not None else b}"
        key = _key_text(rd.key)
        if key:
            label += f" · {key}"
        skip = 0
        if a is None or b is None:
            # a lone record: its value is the whole difference
            lone = ValueDiff()
            lone.counts[status] = 1
            value = va if a is not None else vb
            if value is UNKNOWN and vd.changes:
                ch = vd.changes[0]
                value = ch.a if a is not None else ch.b
            if a is not None:
                va = value
                lone.changes.append(Change(status, (), None, value, MISSING))
            else:
                vb = value
                lone.changes.append(Change(status, None, (), MISSING, value))
            return DiffTreeModel(va, vb, lone, opts=run_opts, root_label=label)
        if st.wrap_a or st.wrap_b:
            if st.wrap_a and st.wrap_b and ka == kb and ka is not None:
                vd = _strip(vd)                      # the same member key: compare the values
            else:
                skip = 1 if st.wrap_a else 0
                if va is UNKNOWN or vb is UNKNOWN:
                    return DiffTreeModel(UNKNOWN, UNKNOWN, vd, opts=run_opts, skip=skip,
                                         a_kind="object" if st.wrap_a else kind_a,
                                         b_kind="object" if st.wrap_b else kind_b, root_label=label)
                if st.wrap_a:
                    va = {ka: va}
                if st.wrap_b:
                    vb = {kb: vb}
        return DiffTreeModel(va, vb, vd, opts=run_opts, skip=skip, a_kind=kind_a, b_kind=kind_b, root_label=label)

    def texts(self, swapped: bool, cancel: CancelToken) -> tuple[SideText, SideText]:
        st = self.state
        _status, a, b = self._entry()
        if not st.use():
            raise Cancelled()
        try:
            st.check_open()
            out = []
            for doc, pos in ((st.doc_a, a), (st.doc_b, b)):
                if pos is None:
                    out.append(SideText(""))
                    continue
                node = doc.record_node(pos)
                data, complete = pretty_prefix(node, 2, TEXT_CAP, cancel)
                out.append(SideText(data.decode("utf-8", errors="ignore"), not complete, int(node.size)))
            return out[0], out[1]
        finally:
            st.done()


def _wrapper_value(doc: Any, cancel: CancelToken | None) -> Any:
    """The text around the record container with the container replaced by ``null`` (as the
    core compares wrappers), parsed; MISSING when the document has no wrapper."""
    spans = doc.wrapper_spans(cancel=cancel)
    if spans is None:
        return MISSING
    (s1, e1), (s2, e2) = spans
    if (e1 - s1) + (e2 - s2) > FULL_VALUE_MAX:
        return UNKNOWN
    with doc.open_data() as f:
        text = read_range(f, s1, e1) + b"null" + read_range(f, s2, e2)
    return codec.loads(text)


class _WrapperPairSource(PairSource):
    """The wrappers of two wrapped documents (the part around the records)."""

    can_swap = False

    def __init__(self, state: _RunState) -> None:
        self.state = state
        self.a_name = t("gui.compare.wrapper_of", name=state.a_name)
        self.b_name = t("gui.compare.wrapper_of", name=state.b_name)

    def _values(self, cancel: CancelToken) -> tuple[Any, Any]:
        st = self.state
        if not st.use():
            raise Cancelled()
        try:
            st.check_open()
            return _wrapper_value(st.doc_a, cancel), _wrapper_value(st.doc_b, cancel)
        finally:
            st.done()

    def load(self, opts: DiffOptions, swapped: bool, cancel: CancelToken) -> DiffTreeModel:
        st = self.state
        rep = st.report
        vd = rep.wrapper if rep is not None and rep.wrapper is not None else ValueDiff()
        va, vb = self._values(cancel)
        if va is MISSING or vb is MISSING:
            va = vb = UNKNOWN
        return DiffTreeModel(va, vb, vd, opts=rep.options if rep is not None else opts, a_kind="object",
                             b_kind="object", root_label=t("gui.compare.wrapper"))

    def texts(self, swapped: bool, cancel: CancelToken) -> tuple[SideText, SideText]:
        va, vb = self._values(cancel)
        out = []
        for v in (va, vb):
            if v is MISSING or v is UNKNOWN:
                out.append(SideText(""))
                continue
            data = codec.dumps(v, indent=2)
            out.append(SideText(data[:TEXT_CAP].decode("utf-8", errors="ignore"), len(data) > TEXT_CAP, len(data)))
        return out[0], out[1]


# ==================================================================================
# the compare mark
# ==================================================================================
@dataclass(eq=False)
class CompareMark:
    """A node marked for comparison (one per application): a weak reference to its session."""

    session_ref: Any
    ref: NodeRef
    label: str

    @property
    def session(self) -> Any:
        s = self.session_ref()
        return s if _session_ok(s) else None


_MARKS: "weakref.WeakKeyDictionary[Any, CompareMark]" = weakref.WeakKeyDictionary()


def _app_of(window: Any, session: Any = None) -> Any:
    app = getattr(window, "app", None)
    if app is not None and hasattr(app, "tasks"):
        return app
    if hasattr(window, "tasks") and hasattr(window, "report_error"):
        return window
    return getattr(session, "app", None)


def _status(app: Any, text: str) -> None:
    show = getattr(app, "show_status", None)
    if callable(show):
        show(text, 5000)


def mark_for_compare(app: Any, session: Any, ref: NodeRef) -> CompareMark:
    """Remember ``ref`` of ``session`` as the node to compare with (replaces an earlier mark)."""
    app = _app_of(app, session)
    mark = CompareMark(weakref.ref(session), ref, node_label(session, ref))
    _MARKS[app] = mark
    _status(app, t("gui.compare.marked", label=mark.label))
    return mark


def marked(app: Any) -> CompareMark | None:
    """The marked node (``None``: nothing marked, or its tab was closed)."""
    app = _app_of(app)
    mark = _MARKS.get(app) if app is not None else None
    if mark is None:
        return None
    if mark.session is None:
        _MARKS.pop(app, None)
        return None
    return mark


def clear_mark(app: Any) -> None:
    app = _app_of(app)
    if app is not None:
        _MARKS.pop(app, None)


# ==================================================================================
# file mode: specification + dialog
# ==================================================================================
@dataclass(eq=False)
class FileSide:
    """A compared file: an open tab (``session``) or a path the window opens itself."""

    session: Any = None
    path: str = ""

    @property
    def name(self) -> str:
        if self.session is not None and _alive(self.session):
            return str(self.session.name)
        return os.path.basename(self.path) or self.path


@dataclass(eq=False)
class CompareSpec:
    """What to compare in file mode: the sides, the method (``auto`` / ``key`` / ``align`` /
    ``position`` / ``document``), the record key and the value options."""

    a: FileSide
    b: FileSide
    mode: str = "auto"
    key: str | None = None
    opts: DiffOptions = field(default_factory=DiffOptions)

    def swapped(self) -> CompareSpec:
        return replace(self, a=self.b, b=self.a)


class CompareFilesDialog(QDialog):
    """Choose two files (open tabs or files) and how to compare them; ``spec_accepted(spec)``
    on "Taqqoslash". ``sessions``: the open tabs' sessions; ``current``: the current one."""

    spec_accepted = Signal(object)

    def __init__(self, app: Any, parent: QWidget | None = None, *, sessions: Iterable[Any] = (),
                 current: Any = None, spec: CompareSpec | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("compare_files_dialog")
        self.app = app
        self.setWindowTitle(t("gui.compare.files.title"))
        self.sessions = [s for s in sessions if _session_ok(s)]
        if current is not None and _session_ok(current) and current not in self.sessions:
            self.sessions.insert(0, current)
        self.current = current if _session_ok(current) else None
        layout = QVBoxLayout(self)
        form = QFormLayout()
        layout.addLayout(form)
        self.side_widgets: dict[str, tuple[QComboBox, QLineEdit, QPushButton]] = {}
        for side in ("a", "b"):
            row = QHBoxLayout()
            combo = QComboBox(self)
            combo.setObjectName(f"compare_source_{side}")
            for s in self.sessions:
                text = t("gui.compare.source.current", name=s.name) if s is self.current else t(
                    "gui.compare.source.tab", name=s.name)
                combo.addItem(text, s)
                combo.setItemData(combo.count() - 1, s.path, Qt.ItemDataRole.ToolTipRole)
            combo.addItem(t("gui.compare.source.file"), None)
            path = QLineEdit(self)
            path.setObjectName(f"compare_path_{side}")
            path.setPlaceholderText(t("gui.compare.path.placeholder"))
            browse = QPushButton(t("gui.compare.browse"), self)
            browse.setObjectName(f"compare_browse_{side}")
            browse.setAutoDefault(False)
            browse.clicked.connect(lambda _c=False, sd=side: self._browse(sd))
            combo.currentIndexChanged.connect(lambda _i, sd=side: self._source_changed(sd))
            row.addWidget(combo, 2)
            row.addWidget(path, 3)
            row.addWidget(browse)
            label = QLabel(t(f"gui.compare.side.{side}"), self)
            label.setBuddy(combo)
            form.addRow(label, row)
            self.side_widgets[side] = (combo, path, browse)
        self.mode_combo = QComboBox(self)
        self.mode_combo.setObjectName("compare_mode")
        for mode in FILE_MODES:
            self.mode_combo.addItem(t(f"gui.compare.mode.{mode}"), mode)
            self.mode_combo.setItemData(self.mode_combo.count() - 1, t(f"gui.compare.mode.{mode}.tip"),
                                        Qt.ItemDataRole.ToolTipRole)
        self.mode_combo.currentIndexChanged.connect(lambda _i: self._sync())
        mode_label = QLabel(t("gui.compare.mode"), self)
        mode_label.setBuddy(self.mode_combo)
        form.addRow(mode_label, self.mode_combo)
        self.key_combo = QComboBox(self)
        self.key_combo.setObjectName("compare_key")
        self.key_combo.setEditable(True)
        self.key_combo.setInsertPolicy(QComboBox.InsertPolicy.NoInsert)
        self.key_combo.setToolTip(t("gui.compare.key.tip"))
        if self.key_combo.lineEdit() is not None:
            self.key_combo.lineEdit().setPlaceholderText(t("gui.compare.key.placeholder"))
        key_label = QLabel(t("gui.compare.key"), self)
        key_label.setBuddy(self.key_combo)
        form.addRow(key_label, self.key_combo)
        arr = QHBoxLayout()
        self.array_combo = QComboBox(self)
        self.array_combo.setObjectName("compare_array_match")
        for mode in ARRAY_MODES:
            self.array_combo.addItem(t(f"gui.diff.match.{mode}"), mode)
            self.array_combo.setItemData(self.array_combo.count() - 1, t(f"gui.diff.match.{mode}.tip"),
                                         Qt.ItemDataRole.ToolTipRole)
        self.array_combo.currentIndexChanged.connect(lambda _i: self._sync())
        self.array_key = QLineEdit(self)
        self.array_key.setObjectName("compare_array_key")
        self.array_key.setPlaceholderText(t("gui.compare.array_key.placeholder"))
        self.array_key.setToolTip(t("gui.diff.match.key.tip"))
        arr.addWidget(self.array_combo)
        arr.addWidget(self.array_key, 1)
        arr_label = QLabel(t("gui.compare.array_match"), self)
        arr_label.setBuddy(self.array_combo)
        form.addRow(arr_label, arr)
        self.numbers_check = QCheckBox(t("gui.diff.numbers_text"), self)
        self.numbers_check.setObjectName("compare_numbers_text")
        self.numbers_check.setToolTip(t("gui.diff.numbers_text.tip"))
        form.addRow("", self.numbers_check)
        self.error_label = QLabel(self)
        self.error_label.setObjectName("compare_error")
        self.error_label.setWordWrap(True)
        self.error_label.setTextFormat(Qt.TextFormat.PlainText)
        self.error_label.setStyleSheet("color: palette(bright-text);")
        self.error_label.setVisible(False)
        layout.addWidget(self.error_label)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel, self)
        self.ok_button = buttons.button(QDialogButtonBox.StandardButton.Ok)
        self.ok_button.setText(t("gui.compare.run"))
        buttons.button(QDialogButtonBox.StandardButton.Cancel).setText(t("gui.common.cancel"))
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)
        self._fields_for: Any = None
        self._defaults(spec)
        self._sync()
        self.resize(640, self.sizeHint().height())

    # -- defaults ----------------------------------------------------------------------------
    def _select(self, side: str, session: Any, path: str = "") -> None:
        combo, edit, _browse = self.side_widgets[side]
        idx = combo.count() - 1
        if session is not None:
            for i in range(combo.count() - 1):
                if combo.itemData(i) is session:
                    idx = i
                    break
        combo.setCurrentIndex(idx)
        if idx == combo.count() - 1:
            edit.setText(path)
        self._source_changed(side)

    def _defaults(self, spec: CompareSpec | None) -> None:
        if spec is not None:
            self._select("a", spec.a.session if _session_ok(spec.a.session) else None, spec.a.path)
            self._select("b", spec.b.session if _session_ok(spec.b.session) else None, spec.b.path)
            self.mode_combo.setCurrentIndex(FILE_MODES.index(spec.mode) if spec.mode in FILE_MODES else 0)
            self.key_combo.setEditText(spec.key or "")
            o = spec.opts
            mode = "key" if o.array_keys else o.array_match
            self.array_combo.setCurrentIndex(ARRAY_MODES.index(mode) if mode in ARRAY_MODES else 0)
            if o.array_keys:
                pat, fld = o.array_keys[0]
                self.array_key.setText(fld if pat is None else f"{pat.text}={fld}")
            self.numbers_check.setChecked(o.numbers == "text")
            return
        first = self.current if self.current is not None else (self.sessions[0] if self.sessions else None)
        self._select("a", first)
        other = next((s for s in self.sessions if s is not first), None)
        self._select("b", other)

    def _source_changed(self, side: str) -> None:
        combo, edit, browse = self.side_widgets[side]
        session = combo.currentData()
        is_file = session is None
        edit.setReadOnly(not is_file)
        browse.setEnabled(is_file)
        if not is_file:
            edit.setText(session.path)
        elif edit.text() and any(edit.text() == s.path for s in self.sessions):
            edit.clear()
        self._load_fields()

    def _load_fields(self) -> None:
        session = None
        for side in ("a", "b"):
            s = self.side_widgets[side][0].currentData()
            if s is not None:
                session = s
                break
        if session is None or session is self._fields_for:
            return
        self._fields_for = session
        from jsonreshape.gui import fields as fields_mod

        ref = weakref.ref(self)

        def ready(schema: Any, s: Any = session) -> None:
            dlg = ref()
            if dlg is not None and _alive(dlg) and dlg._fields_for is s:
                dlg._fill_keys(s, schema)

        try:
            schema = fields_mod.get_schema(session, ready)
        except Exception:   # a session without a document
            log.debug("no schema for the key list", exc_info=True)
            return
        if schema is not None:
            self._fill_keys(session, schema)

    def _fill_keys(self, session: Any, schema: Any) -> None:
        from jsonreshape.gui import fields as fields_mod

        names = fields_mod.field_paths(schema, containers=False, inside_arrays=False, key_field=True, max_depth=3)
        if getattr(getattr(session, "doc", None), "member_records", False) and KEY_FIELD not in names:
            names.insert(0, KEY_FIELD)
        text = self.key_combo.currentText()
        self.key_combo.blockSignals(True)
        try:
            self.key_combo.clear()
            self.key_combo.addItems(names)
            if text:
                self.key_combo.setEditText(text)
            elif "id" in names:
                self.key_combo.setEditText("id")
            else:
                self.key_combo.setEditText("")
        finally:
            self.key_combo.blockSignals(False)

    def key_fields(self) -> list[str]:
        return [self.key_combo.itemText(i) for i in range(self.key_combo.count())]

    def _sync(self) -> None:
        mode = self.mode_combo.currentData()
        self.key_combo.setEnabled(mode in ("auto", "key"))
        self.array_key.setEnabled(self.array_combo.currentData() == "key")

    # -- the result ------------------------------------------------------------------------
    def ask_open_path(self) -> str | None:
        """The file chooser (tests replace this method)."""
        start = ""
        try:
            start = self.app.state.get("last_dir") or ""
        except Exception:
            start = ""
        path, _sel = QFileDialog.getOpenFileName(self, t("gui.compare.browse.title"), start,
                                                 t("gui.compare.browse.filter"))
        return path or None

    def _browse(self, side: str) -> None:
        path = self.ask_open_path()
        if path:
            combo, edit, _b = self.side_widgets[side]
            combo.setCurrentIndex(combo.count() - 1)
            edit.setText(os.path.normpath(path))

    def _side(self, side: str) -> FileSide:
        combo, edit, _b = self.side_widgets[side]
        session = combo.currentData()
        name = t(f"gui.compare.side.{side}").rstrip(":")
        if session is not None:
            if not _session_ok(session) or session.doc is None or session.state != "ready":
                raise UserInputError("gui.compare.error.not_ready", name=getattr(session, "name", name))
            return FileSide(session=session, path=session.path)
        path = edit.text().strip().strip('"')
        if not path:
            raise UserInputError("gui.compare.error.no_source", side=name)
        if not os.path.isfile(path):
            raise UserInputError("gui.compare.error.no_file", path=path)
        return FileSide(path=os.path.abspath(path))

    def spec(self) -> CompareSpec:
        """The comparison chosen (``UserInputError`` / ``PathSyntaxError`` for a bad choice)."""
        a = self._side("a")
        b = self._side("b")
        mode = self.mode_combo.currentData() or "auto"
        key: str | None = self.key_combo.currentText().strip() or None
        if mode not in ("auto", "key"):
            key = None
        if mode == "key" and key is None:
            raise UserInputError("gui.compare.error.key_needed")
        if key is not None:
            parse_key(key)
        amode = self.array_combo.currentData() or "auto"
        numbers = "text" if self.numbers_check.isChecked() else "value"
        if amode == "key":
            text = self.array_key.text().strip()
            if not text:
                raise UserInputError("diff.error.array_key_missing")
            opts = DiffOptions(array_match="key", array_keys=(text,), numbers=numbers)
        else:
            opts = DiffOptions(array_match=amode, numbers=numbers)
        return CompareSpec(a, b, mode, key, opts)

    def set_error(self, text: str | None) -> None:
        self.error_label.setText(text or "")
        self.error_label.setVisible(bool(text))

    def error_text(self) -> str:
        return self.error_label.text() if self.error_label.isVisible() else ""

    def accept(self) -> None:  # noqa: D401 (Qt name)
        try:
            spec = self.spec()
        except JsonReshapeError as exc:
            self.set_error(exc.message)
            return
        self.set_error(None)
        self.spec_accepted.emit(spec)
        super().accept()


# ==================================================================================
# the results grid
# ==================================================================================
@dataclass(slots=True)
class _ResultRow:
    status: str
    a: int | None
    b: int | None
    key: str = ""
    changes: int = 0
    path: str = ""
    error: str = ""


def _row_details(st: _RunState, status: str, a: int | None, b: int | None,
                 cancel: CancelToken) -> _ResultRow:
    rep = st.report
    if rep is None:
        raise Cancelled()
    try:
        st.check_open()
        rd = record_diff(st.doc_a, a, st.doc_b, b, rep.options, key=rep.key, cancel=cancel)
    except Cancelled:
        raise
    except (JsonReshapeError, OSError, ValueError, IndexError) as exc:
        return _ResultRow(status, a, b, error=_error_text(exc) if isinstance(exc, JsonReshapeError)
                          else t("gui.compare.row_failed"))
    vd = rd.diff
    changes = vd.total if vd is not None else 0
    path = ""
    if vd is not None and vd.changes and status == "changed":
        ch = vd.changes[0]
        first = ch.path
        if st.wrap_a and st.wrap_b and ch.a_path and ch.b_path and ch.a_path[0] == ch.b_path[0]:
            first = first[1:]               # inside the member of both records: relative to its value
        path = format_path(first) or "$"
    return _ResultRow(status, a, b, _key_text(rd.key), changes, path)


def _make_fetch(st: _RunState) -> Callable[[list, CancelToken], dict]:
    """The loader's fetch: entries -> :class:`_ResultRow` (worker thread; no QObject captured)."""

    def fetch(keys: list, cancel: CancelToken) -> dict:
        out: dict[int, _ResultRow] = {}
        keys = sorted(keys)
        k = 0
        while k < len(keys):
            start = keys[k]
            e = k
            while e + 1 < len(keys) and keys[e + 1] == keys[e] + 1:
                e += 1
            entries = st.results(start, e - k + 1)
            for off, (status, a, b) in enumerate(entries):
                check(cancel)
                if not st.use():
                    raise Cancelled()
                try:
                    out[start + off] = _row_details(st, status, a, b, cancel)
                finally:
                    st.done()
            k = e + 1
        return out

    return fetch


class _ResultsSource:
    """GridSource of the results grid (rows: the wrapper row when the wrappers differ, then the
    filtered entries of the results file)."""

    COLUMNS = ("status", "key", "a", "b", "changes", "path")

    def __init__(self, window: CompareWindow) -> None:
        self.window = window

    def row_count(self) -> int:
        return self.window._result_rows()

    def cell(self, row: int, column: Column) -> Cell | None:
        w = self.window
        entry = w.entry_of(row)
        if entry is None:
            return None
        cid = column.id
        if entry == "wrapper":
            rep = w._report()
            vd = rep.wrapper if rep is not None else None
            if cid == "status":
                return Cell(text=t("gui.compare.status.wrapper"), bg=STATUS_ROLES["changed"])
            if cid == "key":
                return Cell(text=t("gui.compare.wrapper"), role="muted", italic=True)
            if cid == "changes":
                return Cell(text=format_count(vd.total) if vd is not None else "", align="right")
            if cid == "path" and vd is not None and vd.changes:
                return Cell(text=format_path(vd.changes[0].path) or "$", role="key")
            return Cell()
        loader = w._loader
        value = loader.get(entry) if loader is not None else None
        if value is None:
            return None
        if isinstance(value, LoadFailed):
            if cid == "status":
                return Cell(text=t("gui.compare.row_failed"), role="error", tooltip=value.text)
            return Cell()
        r: _ResultRow = value
        if cid == "status":
            return Cell(text=t(f"gui.compare.status.{r.status}"), bg=STATUS_ROLES.get(r.status),
                        tooltip=r.error or None)
        if cid == "key":
            return Cell(text=r.key, role="key")
        if cid == "a":
            return Cell(text="" if r.a is None else f"#{r.a}", align="right", role="muted" if r.a is None else None)
        if cid == "b":
            return Cell(text="" if r.b is None else f"#{r.b}", align="right")
        if cid == "changes":
            if r.error:
                return Cell(text=r.error, role="error")
            return Cell(text=format_count(r.changes) if r.changes else "", align="right")
        return Cell(text=r.path, role="key" if r.path else None)

    def rows_shown(self, first: int, last: int) -> None:
        w = self.window
        loader = w._loader
        if loader is None:
            return
        keys = [e for e in (w.entry_of(r) for r in range(first, last + 1)) if isinstance(e, int)]
        loader.request(keys)


# ==================================================================================
# the run holder (worker <-> window handover)
# ==================================================================================
class _RunHolder:
    """Documents opened by a run's worker and its report: adopted by the window when the run
    finished, else closed (a failed / cancelled run, a window closed meanwhile)."""

    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.docs: list[Any] = []
        self.report: DiffReport | None = None
        self.adopted = False
        self.abandoned = False
        self.ended = False

    def open(self, path: str, settings: Any, ctx: Any) -> Any:
        doc = Document.open(path, settings=settings, progress=ctx.progress, cancel=ctx.cancel)
        with self.lock:
            self.docs.append(doc)
        return doc

    def _drop(self) -> None:
        docs, self.docs = self.docs, []
        rep, self.report = self.report, None
        if rep is not None:
            rep.close()
        for d in docs:
            try:
                d.close()
            except Exception:   # pragma: no cover
                log.debug("closing %r failed", d, exc_info=True)

    def failed(self) -> None:
        """Worker: the run raised -- nothing is handed over."""
        with self.lock:
            self.ended = True
            self.adopted = True       # nothing left to adopt
            docs, rep = self.docs, self.report
            self.docs, self.report = [], None
        holder = _RunHolder()
        holder.docs, holder.report = docs, rep
        holder._drop()

    def finished(self) -> None:
        """Worker: the run returned."""
        with self.lock:
            self.ended = True
            drop = self.abandoned and not self.adopted
        if drop:
            self._drop()

    def adopt(self) -> bool:
        """GUI: take the documents and the report (False: the window closed meanwhile)."""
        with self.lock:
            if self.abandoned:
                return False
            self.adopted = True
            return True

    def abandon(self) -> None:
        """GUI: the window closes; a finished run's leftovers are closed now, a running one's
        when it ends."""
        with self.lock:
            self.abandoned = True
            drop = self.ended and not self.adopted
        if drop:
            threading.Thread(target=self._drop, name="jsonreshape-compare-drop", daemon=True).start()


@dataclass
class _Outcome:
    report: DiffReport | None       # None: compare the documents as nodes (their roots)
    doc_a: Any
    doc_b: Any


# ==================================================================================
# the window
# ==================================================================================
class CompareWindow(QWidget):
    """Non-modal compare window (see the module docstring). ``reveal(session, ref)`` shows a
    node in its tab (default: ``session.select(ref, reveal=True)``).

    Signals: ``run_finished(report_or_None)`` (file mode: a run ended -- ``None`` when it failed,
    was cancelled or became a node comparison), ``records_opened(session)``,
    ``export_finished(count)``."""

    run_finished = Signal(object)
    records_opened = Signal(object)
    export_finished = Signal(object)

    def __init__(self, app: Any, parent: QWidget | None = None, *, reveal: Reveal | None = None) -> None:
        super().__init__(parent, Qt.WindowType.Window)
        self.setObjectName("compare_window")
        self.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
        self.app = app
        self._reveal_fn = reveal
        self._mode = ""                      # "nodes" | "files"
        self._node_sides: tuple[NodeSide, NodeSide] | None = None
        self._spec: CompareSpec | None = None
        self._run: _RunState | None = None
        self._holder: _RunHolder | None = None
        self._task: Any = None
        self._run_state = "idle"             # idle | running | done | failed | cancelled
        self._loader: RowLoader | None = None
        self._index: array | None = None     # filtered entry numbers (None: all)
        self._filter = set(STATUSES)
        self._filter_gen = 0
        self._after_load = ""                # "last": select the last difference when loaded
        self._stale: dict[str, str] = {}     # side -> reason text
        self._watched: list[Any] = []
        self._closed = False
        self._sessions: dict[str, Any] = {"a": None, "b": None}
        self.resize(1100, 760)

        root = QVBoxLayout(self)
        root.setContentsMargins(6, 6, 6, 6)
        root.setSpacing(4)
        self.stale_bar = InfoBar("warning", "", parent=self)
        self.stale_bar.setObjectName("compare_stale_bar")
        self.stale_bar.add_button(t("gui.compare.refresh"), self.refresh)
        self.stale_bar.setVisible(False)
        root.addWidget(self.stale_bar)
        self.header = ElidedLabel("", self, mode=Qt.TextElideMode.ElideMiddle)
        self.header.setObjectName("compare_header")
        root.addWidget(self.header)
        root.addWidget(self._build_files_panel())

        self.splitter = QSplitter(Qt.Orientation.Vertical, self)
        self.results = VirtualGrid(self.splitter)
        self.results.setObjectName("compare_results")
        self.results.set_selection_mode("single")
        self.results.set_columns([
            Column("status", t("gui.compare.col.status"), 120),
            Column("key", t("gui.compare.col.key"), 200),
            Column("a", t("gui.compare.col.a"), 90, align="right"),
            Column("b", t("gui.compare.col.b"), 90, align="right"),
            Column("changes", t("gui.compare.col.changes"), 100, align="right"),
            Column("path", t("gui.compare.col.path"), 320),
        ])
        self.results.set_placeholder_text(t("gui.compare.no_results"))
        self._source = _ResultsSource(self)
        self.results.set_source(self._source)
        self.results.current_changed.connect(lambda _r, _c: self._detail_timer.start())
        self.results.activated.connect(lambda row, _c: self._reveal_entry(int(row), None))
        self.results.context_menu_requested.connect(self._results_menu)
        self.view = DiffView(app, self.splitter, shortcuts=False, reveal=self._can_reveal)
        self.view.reveal_requested.connect(self._reveal_row)
        self.view.swap_requested.connect(self.swap)
        self.view.loaded.connect(self._view_loaded)
        self.splitter.addWidget(self.results)
        self.splitter.addWidget(self.view)
        self.splitter.setStretchFactor(0, 2)
        self.splitter.setStretchFactor(1, 3)
        root.addWidget(self.splitter, 1)

        self._detail_timer = QTimer(self)
        self._detail_timer.setSingleShot(True)
        self._detail_timer.setInterval(DETAIL_DELAY_MS)
        self._detail_timer.timeout.connect(self._show_detail)
        for seq, fn in ((NEXT_KEY, self.next_change), (PREV_KEY, self.prev_change),
                        (QKeySequence.StandardKey.Close, self.close)):
            sc = QShortcut(QKeySequence(seq), self)
            sc.setContext(Qt.ShortcutContext.WindowShortcut)
            sc.activated.connect(fn)
        tasks = getattr(app, "tasks", None)
        if tasks is not None:
            tasks.task_updated.connect(self._on_task_updated)
        self.destroyed.connect(lambda *_a, holder=[self._release_all]: holder[0]())
        self._set_mode("")

    # ------------------------------------------------------------------ construction
    def _build_files_panel(self) -> QWidget:
        panel = QWidget(self)
        panel.setObjectName("compare_files_panel")
        v = QVBoxLayout(panel)
        v.setContentsMargins(0, 0, 0, 0)
        v.setSpacing(4)
        top = QHBoxLayout()
        self.summary = ElidedLabel("", panel, mode=Qt.TextElideMode.ElideRight)
        self.summary.setObjectName("compare_summary")
        top.addWidget(self.summary, 1)
        self.progress = QProgressBar(panel)
        self.progress.setObjectName("compare_progress")
        self.progress.setMaximumWidth(220)
        self.progress.setVisible(False)
        top.addWidget(self.progress)
        self.cancel_button = QPushButton(t("gui.common.cancel"), panel)
        self.cancel_button.setObjectName("compare_cancel")
        self.cancel_button.setAutoDefault(False)
        self.cancel_button.clicked.connect(self.cancel)
        self.cancel_button.setVisible(False)
        top.addWidget(self.cancel_button)
        v.addLayout(top)
        bar = QHBoxLayout()
        self.filter_checks: dict[str, QCheckBox] = {}
        for status in STATUSES:
            box = QCheckBox(panel)
            box.setObjectName(f"compare_filter_{status}")
            box.setChecked(True)
            box.toggled.connect(lambda _on: self._filter_changed())
            self.filter_checks[status] = box
            bar.addWidget(box)
        bar.addStretch(1)
        self.records_button = QToolButton(panel)
        self.records_button.setObjectName("compare_records")
        self.records_button.setText(t("gui.compare.open_records"))
        self.records_button.setToolTip(t("gui.compare.open_records.tip"))
        self.records_button.setPopupMode(QToolButton.ToolButtonPopupMode.InstantPopup)
        self.records_menu = QMenu(self.records_button)
        self.records_menu.setToolTipsVisible(True)
        self.records_menu.aboutToShow.connect(self._fill_records_menu)
        self.records_button.setMenu(self.records_menu)
        bar.addWidget(self.records_button)
        self.export_button = QPushButton(t("gui.compare.export"), panel)
        self.export_button.setObjectName("compare_export")
        self.export_button.setAutoDefault(False)
        self.export_button.clicked.connect(self.choose_export_file)
        bar.addWidget(self.export_button)
        self.swap_button = QPushButton("A ⇄ B", panel)
        self.swap_button.setObjectName("compare_swap")
        self.swap_button.setToolTip(t("gui.compare.swap.tip"))
        self.swap_button.setAutoDefault(False)
        self.swap_button.clicked.connect(self.swap)
        bar.addWidget(self.swap_button)
        self.options_button = QPushButton(t("gui.compare.options"), panel)
        self.options_button.setObjectName("compare_options")
        self.options_button.setAutoDefault(False)
        self.options_button.clicked.connect(self.show_options)
        bar.addWidget(self.options_button)
        v.addLayout(bar)
        self.warnings_label = QLabel(panel)
        self.warnings_label.setObjectName("compare_warnings")
        self.warnings_label.setWordWrap(True)
        self.warnings_label.setTextFormat(Qt.TextFormat.PlainText)
        self.warnings_label.setVisible(False)
        v.addWidget(self.warnings_label)
        self.files_panel = panel
        return panel

    def _set_mode(self, mode: str) -> None:
        self._mode = mode
        files = mode == "files"
        self.files_panel.setVisible(files)
        self.results.setVisible(files)
        self.header.setVisible(mode == "nodes")
        if files:
            self.view.set_match_locked(True, t("gui.compare.match_locked"))
        else:
            self.view.set_match_locked(False)
        self._sync_buttons()

    # ------------------------------------------------------------------ sessions
    def _watch(self, side: str, session: Any) -> None:
        self._sessions[side] = session if _session_ok(session) else None
        if session is None or not _session_ok(session) or any(s is session for s in self._watched):
            return
        self._watched.append(session)
        for name, slot in (("data_changed", self._on_data_changed), ("opened", self._on_reopened),
                           ("path_changed", self._on_reopened), ("state_changed", self._on_state)):
            sig = getattr(session, name, None)
            if sig is not None:
                sig.connect(slot)

    def _unwatch(self) -> None:
        for session in self._watched:
            if not _alive(session):
                continue
            for name, slot in (("data_changed", self._on_data_changed), ("opened", self._on_reopened),
                               ("path_changed", self._on_reopened), ("state_changed", self._on_state)):
                sig = getattr(session, name, None)
                if sig is None:
                    continue
                try:
                    sig.disconnect(slot)
                except (RuntimeError, TypeError):
                    pass
        self._watched = []

    def _sides_of(self, session: Any) -> list[str]:
        return [side for side in ("a", "b") if self._sessions.get(side) is session]

    def _on_data_changed(self, *_args: Any) -> None:
        session = self.sender()
        for side in self._sides_of(session):
            self._mark_stale(side, t("gui.compare.stale.changed", name=session.name))

    def _on_reopened(self, *_args: Any) -> None:
        session = self.sender()
        for side in self._sides_of(session):
            self._mark_stale(side, t("gui.compare.stale.changed", name=session.name))

    def _on_state(self, state: str) -> None:
        if state != "closed":
            return
        session = self.sender()
        for side in self._sides_of(session):
            self._mark_stale(side, t("gui.compare.stale.closed", name=getattr(session, "name", "?")))
            self._sessions[side] = None
        self._sync_buttons()

    def _mark_stale(self, side: str, text: str) -> None:
        if self._closed:
            return
        self._stale[side] = text
        self.stale_bar.set_message(" ".join(self._stale[s] for s in ("a", "b") if s in self._stale)
                                   + " " + t("gui.compare.stale.hint"))
        self.stale_bar.setVisible(True)

    def is_stale(self) -> bool:
        return bool(self._stale)

    def _clear_stale(self) -> None:
        self._stale = {}
        self.stale_bar.setVisible(False)

    # ------------------------------------------------------------------ node mode
    def compare_nodes(self, a: NodeSide, b: NodeSide, opts: DiffOptions | None = None) -> None:
        """Compare two nodes (sides captured with :func:`capture_node_side`)."""
        self.cancel()
        self._release_run()
        self._set_mode("nodes")
        self._node_sides = (a, b)
        self._clear_stale()
        self._watch("a", a.session)
        self._watch("b", b.session)
        self.setWindowTitle(t("gui.compare.title", a=a.label, b=b.label))
        self.header.setText(t("gui.compare.header", a=a.label, b=b.label))
        self.view.set_source(NodePairSource(a, b), opts=opts or self.view.options())

    # ------------------------------------------------------------------ file mode
    def compare_files(self, spec: CompareSpec) -> Any:
        """Compare two files (a heavy task); returns the task (``None`` when a side is not ready:
        the error is shown)."""
        self._spec = spec
        self._set_mode("files")
        return self._start_run()

    def _start_run(self) -> Any:
        spec = self._spec
        if spec is None:
            return None
        self.cancel()
        self._release_run()
        self._set_mode("files")
        self._clear_stale()
        self.view.set_source(None)
        captured: list[tuple[Any, str]] = []
        try:
            for fs in (spec.a, spec.b):
                session = fs.session if _session_ok(fs.session) else None
                if session is not None:
                    if session.doc is None or session.state != "ready":
                        raise UserInputError("gui.compare.error.not_ready", name=session.name)
                    doc = task_doc(session)
                    if doc is None:
                        raise UserInputError("gui.compare.error.not_ready", name=session.name)
                    captured.append((doc, fs.path))
                else:
                    captured.append((None, fs.path))
        except JsonReshapeError as exc:
            self._run_state = "failed"
            self.summary.setText(t("gui.compare.failed", error=exc.message))
            self.run_finished.emit(None)
            self._sync_buttons()
            return None
        self._watch("a", spec.a.session)
        self._watch("b", spec.b.session)
        a_name, b_name = spec.a.name, spec.b.name
        self.setWindowTitle(t("gui.compare.title", a=a_name, b=b_name))
        holder = self._holder = _RunHolder()
        settings = getattr(self.app, "settings", None)
        mode, key, opts = spec.mode, spec.key, gui_options(spec.opts)

        def work(ctx: Any) -> _Outcome:
            try:
                da = captured[0][0] or holder.open(captured[0][1], settings, ctx)
                db = captured[1][0] or holder.open(captured[1][1], settings, ctx)
                if mode == "document" or (mode == "auto" and key is None and da.kind == "single"
                                          and db.kind == "single"):
                    for d in (da, db):
                        if not d.index_complete:
                            d.build_index(cancel=ctx.cancel)
                    outcome = _Outcome(None, da, db)
                else:
                    rep = diff_files(da, db, key=key, mode=mode, opts=opts, max_details=0, results=True,
                                     progress=ctx.progress, cancel=ctx.cancel)
                    holder.report = rep
                    outcome = _Outcome(rep, da, db)
            except BaseException:
                holder.failed()
                raise
            holder.finished()
            return outcome

        self._run_state = "running"
        self.summary.setText(t("gui.compare.running"))
        self.progress.setRange(0, 0)
        self.progress.setVisible(True)
        self.cancel_button.setVisible(True)
        task = self._task = self.app.tasks.run(
            work, title=t("gui.compare.task", a=a_name, b=b_name), kind="compare", owner=self, heavy=True,
            on_done=lambda out, h=holder: self._run_done(h, out), on_error=lambda exc, h=holder: self._run_failed(h, exc),
            on_cancel=lambda h=holder: self._run_cancelled(h))
        for fs, (doc, _path) in zip((spec.a, spec.b), captured):
            if doc is not None and _session_ok(fs.session):
                keep = getattr(fs.session, "keep_document_for", None)
                if callable(keep):
                    keep(task, doc=doc)
        self._sync_buttons()
        return task

    def _run_ended(self) -> None:
        self._task = None
        self.progress.setVisible(False)
        self.cancel_button.setVisible(False)

    def _run_done(self, holder: _RunHolder, out: _Outcome) -> None:
        if holder is not self._holder or not holder.adopt():
            return                              # a superseded run (its leftovers are closed)
        self._run_ended()
        spec = self._spec
        assert spec is not None
        owned = list(holder.docs)
        holder.docs = []
        if out.report is None:
            # two single documents: their roots side by side
            self._run = _RunState(None, out.doc_a, out.doc_b, spec.a.name, spec.b.name, owned)
            self._run_state = "done"
            sides = []
            for fs, doc in ((spec.a, out.doc_a), (spec.b, out.doc_b)):
                session = fs.session if _session_ok(fs.session) else None
                sides.append(NodeSide(session, NodeRef(None, ()), doc, fs.name, "$",
                                      int(getattr(session, "data_gen", 0) or 0) if session is not None else 0))
            self._set_mode("nodes")
            self._node_sides = (sides[0], sides[1])
            self.header.setText(t("gui.compare.header", a=sides[0].label, b=sides[1].label))
            self.view.set_source(NodePairSource(sides[0], sides[1]), opts=spec.opts)
            self.run_finished.emit(None)
            return
        rep = out.report
        holder.report = None
        self._run = _RunState(rep, out.doc_a, out.doc_b, spec.a.name, spec.b.name, owned)
        self._run_state = "done"
        self._loader = RowLoader(_make_fetch(self._run), capacity=4096, batch=LOADER_BATCH, name="compare",
                                 parent=self)
        self._loader.loaded.connect(lambda: self.results.refresh())
        self._index = None
        self._filter_gen += 1
        for status, box in self.filter_checks.items():
            box.blockSignals(True)
            box.setChecked(True)
            box.blockSignals(False)
        self._filter = set(STATUSES)
        self._update_summary()
        self.view.set_options(rep.options, reload=False)
        self.results.reset()
        self.results.refresh(request=True)
        if self._result_rows():
            self.results.set_current(0, 0)
            self._show_detail()
        self._sync_buttons()
        self.run_finished.emit(rep)

    def _run_failed(self, holder: _RunHolder, exc: BaseException) -> None:
        if holder is not self._holder:
            return
        self._run_ended()
        self._run_state = "failed"
        self.summary.setText(t("gui.compare.failed", error=_error_text(exc)))
        if isinstance(exc, JsonReshapeError):
            hint = getattr(exc, "hint", None)
            if hint:
                self.warnings_label.setText(str(hint))
                self.warnings_label.setVisible(True)
        else:
            log.error("comparing files failed", exc_info=(type(exc), exc, exc.__traceback__))
        self._sync_buttons()
        self.run_finished.emit(None)

    def _run_cancelled(self, holder: _RunHolder) -> None:
        if holder is not self._holder:
            return
        self._run_ended()
        self._run_state = "cancelled"
        self.summary.setText(t("gui.compare.cancelled"))
        self._sync_buttons()
        self.run_finished.emit(None)

    def run_state(self) -> str:
        """``idle`` | ``running`` | ``done`` | ``failed`` | ``cancelled``."""
        return self._run_state

    def report(self) -> DiffReport | None:
        return self._report()

    def _report(self) -> DiffReport | None:
        st = self._run
        return st.report if st is not None else None

    def cancel(self) -> None:
        """Cancel the running comparison."""
        task = self._task
        if task is not None:
            task.cancel()

    def _on_task_updated(self, task: Any) -> None:
        if task is not self._task or task is None:
            return
        info = task.info()
        if info is None:
            return
        if info.fraction is None:
            self.progress.setRange(0, 0)
        else:
            self.progress.setRange(0, 1000)
            self.progress.setValue(int(round(info.fraction * 1000)))
        if info.message:
            self.summary.setText(info.message)

    def _update_summary(self) -> None:
        rep = self._report()
        if rep is None:
            return
        if rep.mode == "key":
            method = t("gui.compare.method.key", key=rep.key or "")
        else:
            method = t(f"gui.compare.method.{rep.mode}")
        if rep.identical:
            text = t("gui.compare.summary.identical", equal=format_count(rep.equal), seconds=f"{rep.elapsed:.1f}")
        else:
            text = t("gui.compare.summary", changed=format_count(rep.changed), added=format_count(rep.added),
                     removed=format_count(rep.removed), equal=format_count(rep.equal),
                     a=format_count(rep.a_records), b=format_count(rep.b_records), seconds=f"{rep.elapsed:.1f}")
        self.summary.setText(f"{text} · {method}")
        counts = {"changed": rep.changed, "added": rep.added, "removed": rep.removed}
        for status, box in self.filter_checks.items():
            box.setText(t(f"gui.compare.filter.{status}", count=format_count(counts[status])))
        warnings = rep.warning_texts()
        self.warnings_label.setText("\n".join(warnings))
        self.warnings_label.setVisible(bool(warnings))

    # ------------------------------------------------------------------ rows
    def _has_wrapper(self) -> bool:
        rep = self._report()
        return rep is not None and rep.wrapper is not None

    def _result_rows(self) -> int:
        rep = self._report()
        if rep is None:
            return 0
        n = rep.results_count if self._index is None else len(self._index)
        return n + (1 if self._has_wrapper() else 0)

    def entry_of(self, row: int) -> int | str | None:
        """The results entry of a grid row (``"wrapper"`` for the wrapper row)."""
        if row < 0 or row >= self._result_rows():
            return None
        if self._has_wrapper():
            if row == 0:
                return "wrapper"
            row -= 1
        return row if self._index is None else int(self._index[row])

    def result_row(self, row: int) -> _ResultRow | None:
        """The loaded details of a grid row (``None``: not loaded yet / the wrapper row)."""
        entry = self.entry_of(row)
        if not isinstance(entry, int) or self._loader is None:
            return None
        value = self._loader.peek(entry)
        return value if isinstance(value, _ResultRow) else None

    def _filter_changed(self) -> None:
        wanted = {s for s, box in self.filter_checks.items() if box.isChecked()}
        if wanted == self._filter:
            return
        self._filter = wanted
        self._filter_gen += 1
        gen = self._filter_gen
        st = self._run
        if st is None or st.report is None:
            return
        if wanted == set(STATUSES):
            self._install_index(gen, None)
            return
        total = st.report.results_count

        def work(ctx: Any) -> array:
            out = array("Q")
            start = 0
            while start < total:
                check(ctx.cancel)
                chunk = st.results(start, RECORDS_SCAN)
                if not chunk:
                    break
                out.extend(start + k for k, (status, _a, _b) in enumerate(chunk) if status in wanted)
                start += len(chunk)
            return out

        self.app.tasks.run(work, title=t("gui.compare.task.filter"), kind="compare", owner=self, silent=True,
                           on_done=lambda idx: self._install_index(gen, idx),
                           on_error=lambda exc: self.app.report_error(exc, self))

    def _install_index(self, gen: int, index: array | None) -> None:
        if gen != self._filter_gen:
            return
        self._index = index
        self.results.reset()
        self.results.refresh(request=True)
        if self._result_rows():
            self.results.set_current(0, 0)
        else:
            self.view.set_source(None)
        self._sync_buttons()

    # ------------------------------------------------------------------ details
    def _show_detail(self) -> None:
        self._detail_timer.stop()
        st = self._run
        row = self.results.current()[0]
        entry = self.entry_of(row)
        if st is None or entry is None:
            return
        if entry == "wrapper":
            self.view.set_source(_WrapperPairSource(st), opts=st.report.options if st.report else None)
            return
        hint = None
        loaded = self._loader.peek(entry) if self._loader is not None else None
        if isinstance(loaded, _ResultRow):
            hint = (loaded.status, loaded.a, loaded.b)
        self.view.set_source(RecordPairSource(st, entry, hint), opts=st.report.options if st.report else None)

    def _view_loaded(self) -> None:
        if self._after_load == "last":
            self._after_load = ""
            self.view.select_last_change()

    def next_change(self) -> None:
        """F7: the next difference; after the last one of a record, the next record."""
        if self.view.next_change() is not None or self._mode != "files":
            return
        row = self.results.current()[0]
        if 0 <= row + 1 < self._result_rows():
            self._after_load = ""
            self.results.set_current(row + 1, 0)
            self._show_detail()

    def prev_change(self) -> None:
        """Shift+F7: the previous difference; before the first one, the previous record's last."""
        if self.view.prev_change() is not None or self._mode != "files":
            return
        row = self.results.current()[0]
        if row - 1 >= 0:
            self._after_load = "last"
            self.results.set_current(row - 1, 0)
            self._show_detail()

    # ------------------------------------------------------------------ reveal
    def _side_ref(self, side: str, row: DiffRow) -> tuple[Any, NodeRef] | None:
        path = row.a_path if side == "a" else row.b_path
        if path is None:
            return None
        if self._mode == "nodes" and self._node_sides is not None:
            ns = self._node_sides[0 if side == "a" else 1]
            if self.view.swapped():
                ns = self._node_sides[1 if side == "a" else 0]
            if ns.session is None or not _session_ok(ns.session):
                return None
            return ns.session, NodeRef(ns.ref.record, tuple(ns.ref.path) + tuple(path))
        st = self._run
        src = self.view.source()
        if st is None or not isinstance(src, RecordPairSource) or src.triple is None:
            return None
        _status, a, b = src.triple
        pos = a if side == "a" else b
        session = self._sessions.get(side)
        if pos is None or not _session_ok(session):
            return None
        wrapped = st.wrap_a if side == "a" else st.wrap_b
        model = self.view.model()
        rel = tuple(path)
        if wrapped and model is not None and model.skip:
            rel = rel[1:]
        return session, NodeRef(pos, rel)

    def _can_reveal(self, side: str, row: DiffRow) -> bool:
        return self._side_ref(side, row) is not None

    def _reveal_row(self, side: str, row: DiffRow) -> None:
        target = self._side_ref(side, row)
        if target is not None:
            self.reveal(*target)

    def reveal(self, session: Any, ref: NodeRef) -> None:
        """Show ``ref`` in the tab of ``session`` (the ``reveal`` callback, else ``select``)."""
        if not _session_ok(session):
            return
        fn = self._reveal_fn
        if fn is not None:
            fn(session, ref)
            return
        session.select(ref, reveal=True)
        parent = self.parentWidget()
        if parent is not None:
            win = parent.window()
            win.raise_()
            win.activateWindow()

    def _reveal_entry(self, row: int, side: str | None) -> None:
        entry = self.entry_of(row)
        if not isinstance(entry, int):
            return
        r = self.result_row(row)
        if r is None:
            return
        for sd in ((side,) if side else ("a", "b")):
            pos = r.a if sd == "a" else r.b
            session = self._sessions.get(sd)
            if pos is not None and _session_ok(session):
                self.reveal(session, NodeRef(pos, ()))
                return

    def _results_menu(self, row: Any, _col: int, pos: QPoint) -> None:
        if row is None or int(row) < 0:
            return
        menu = self.build_results_menu(int(row))
        if menu is not None:
            menu.aboutToHide.connect(menu.deleteLater)
            menu.popup(pos)

    def build_results_menu(self, row: int) -> QMenu | None:
        r = self.result_row(row)
        if r is None:
            return None
        menu = QMenu(self)
        menu.setObjectName("compare_results_menu")
        for side, pos in (("a", r.a), ("b", r.b)):
            act = menu.addAction(t(f"gui.compare.reveal.{side}"))
            act.setObjectName(f"compare_reveal_{side}")
            act.setEnabled(pos is not None and _session_ok(self._sessions.get(side)))
            act.triggered.connect(lambda _c=False, rr=row, sd=side: self._reveal_entry(rr, sd))
        return menu

    # ------------------------------------------------------------------ record sets
    def _fill_records_menu(self) -> None:
        menu = self.records_menu
        menu.clear()
        rep = self._report()
        for side in ("a", "b"):
            name = (self._spec.a.name if side == "a" else self._spec.b.name) if self._spec is not None else side
            act = menu.addAction(t(f"gui.compare.open_records.{side}", name=name))
            act.setObjectName(f"compare_records_{side}")
            session = self._sessions.get(side)
            count = 0 if rep is None else (rep.changed + (rep.removed if side == "a" else rep.added))
            if not _session_ok(session):
                act.setEnabled(False)
                act.setToolTip(t("gui.compare.open_records.no_tab", name=name))
            elif count == 0:
                act.setEnabled(False)
            act.triggered.connect(lambda _c=False, sd=side: self.open_records(sd))

    def open_records(self, side: str) -> Any:
        """Show the differing records of ``side`` in its tab (A: changed + removed, B: changed +
        added) as a record set; returns the task (``None`` when not possible)."""
        st = self._run
        session = self._sessions.get(side)
        rep = self._report()
        if st is None or rep is None or not _session_ok(session):
            return None
        wanted = ("changed", "removed") if side == "a" else ("changed", "added")
        total = rep.results_count
        gen = rep.a_gen if side == "a" else rep.b_gen
        other = self._spec.b.name if side == "a" and self._spec else self._spec.a.name if self._spec else ""

        def work(ctx: Any) -> RecordSet:
            positions = array("Q")
            start = 0
            while start < total:
                check(ctx.cancel)
                chunk = st.results(start, RECORDS_SCAN)
                if not chunk:
                    break
                for status, a, b in chunk:
                    if status in wanted:
                        pos = a if side == "a" else b
                        if pos is not None:
                            positions.append(pos)
                start += len(chunk)
            ordered = array("Q", sorted(set(positions)))
            label = count_text("gui.compare.records_label", len(ordered), other=other)
            return RecordSet.from_indices(ordered, kind="compare", description=label, gen=gen)

        def done(rs: RecordSet) -> None:
            if not _session_ok(session):
                rs.close()
                return
            session.show_records(rs, rs.description)
            self.records_opened.emit(session)

        return self.app.tasks.run(work, title=t("gui.compare.task.records"), kind="records", owner=self,
                                  silent=True, on_done=done, on_error=lambda exc: self.app.report_error(exc, self))

    # ------------------------------------------------------------------ export
    def ask_save_path(self, suggested: str) -> str | None:
        """The save dialog of the report (tests replace this method)."""
        path, _sel = QFileDialog.getSaveFileName(self, t("gui.compare.export.title"), suggested,
                                                 t("gui.compare.export.filter"))
        return path or None

    def choose_export_file(self) -> Any:
        if self._spec is None:
            return None
        base = os.path.splitext(self._spec.a.name)[0] + "-diff.json"
        folder = os.path.dirname(self._spec.a.path) if self._spec.a.path else ""
        path = self.ask_save_path(os.path.join(folder, base))
        if not path:
            return None
        return self.export_report(path)

    def export_report(self, path: str, fmt: str | None = None) -> Any:
        """Write every difference (with its changes) to ``path`` -- JSON / JSON Lines / CSV by the
        extension or ``fmt``: the comparison runs again over the same snapshots (a heavy task).
        Returns the task (``None`` on refusal: the reason is in the summary line)."""
        st = self._run
        rep = self._report()
        if st is None or rep is None:
            return None
        path = os.path.abspath(path)
        kind = fmt or EXPORT_FORMATS.get(os.path.splitext(path)[1].lower(), "json")
        from jsonreshape.gui.searchpanel import check_not_source, open_target_check, reload_tabs

        try:
            sources = [getattr(st.doc_a, "source_path", None), getattr(st.doc_b, "source_path", None)]
            if self._spec is not None:
                sources += [self._spec.a.path, self._spec.b.path]
            check_not_source(path, sources)
        except UserInputError as exc:
            self.summary.setText(exc.message)
            return None
        # a file another tab shows: refused while it has changes (or changes kept for recovery), else asked;
        # that tab opens the report afterwards (review S3)
        reload = open_target_check(self, path, on_refused=self.summary.setText)
        if reload is None:
            return None
        mode, key, opts = rep.mode, rep.key, rep.options

        def work(ctx: Any) -> int:
            if not st.use():
                raise Cancelled()
            try:
                st.check_open()
                diffs = iter_record_diffs(st.doc_a, st.doc_b, key=key, mode=mode, opts=opts,
                                          progress=ctx.progress, cancel=ctx.cancel)
                return write_diff(diffs, path, kind)
            finally:
                st.done()

        def done(count: int) -> None:
            reload_tabs(reload)
            text = t("gui.compare.export.done", count=format_count(count), path=path)
            self.summary.setText(text)
            _status(self.app, text)
            self.export_finished.emit(count)

        task = self.app.tasks.run(work, title=t("gui.compare.task.export", name=os.path.basename(path)),
                                  kind="export", owner=self, heavy=True, on_done=done,
                                  on_error=lambda exc: self.app.report_error(exc, self))
        for side, doc in (("a", st.doc_a), ("b", st.doc_b)):
            session = self._sessions.get(side)
            if _session_ok(session) and callable(getattr(session, "keep_document_for", None)):
                session.keep_document_for(task, doc=doc)
        return task

    # ------------------------------------------------------------------ swap / options / refresh
    def swap(self) -> None:
        """Swap A and B (file mode: the comparison runs again; node mode: the view swaps)."""
        if self._mode == "files" and self._spec is not None:
            self._spec = self._spec.swapped()
            self._start_run()
        elif self._mode == "nodes":
            self.view.swap()

    def show_options(self) -> CompareFilesDialog | None:
        """The files dialog prefilled with this comparison; accepting runs it again here."""
        if self._spec is None:
            return None
        sessions = [s for s in (self._spec.a.session, self._spec.b.session) if _session_ok(s)]
        dlg = CompareFilesDialog(self.app, self, sessions=sessions, spec=self._spec)
        dlg.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
        dlg.spec_accepted.connect(self.compare_files)
        dlg.setModal(False)
        dlg.show()
        return dlg

    def refresh(self) -> None:
        """Compare again with fresh snapshots (a closed tab's side: its file)."""
        if self._spec is not None:
            spec = self._spec
            sides = []
            for fs in (spec.a, spec.b):
                sides.append(fs if _session_ok(fs.session) else FileSide(path=fs.path))
            self._spec = replace(spec, a=sides[0], b=sides[1])
            self._start_run()
            return
        if self._mode == "nodes" and self._node_sides is not None:
            new = []
            try:
                for ns in self._node_sides:
                    if _session_ok(ns.session):
                        new.append(capture_node_side(ns.session, ns.ref))
                    else:
                        new.append(ns)
            except JsonReshapeError as exc:
                self.app.report_error(exc, self)
                return
            self.compare_nodes(new[0], new[1], self.view.options())

    def _sync_buttons(self) -> None:
        done = self._run_state == "done" and self._report() is not None
        self.records_button.setEnabled(done)
        self.export_button.setEnabled(done)
        running = self._run_state == "running"
        self.swap_button.setEnabled(not running and self._spec is not None)
        self.options_button.setEnabled(not running and self._spec is not None)
        for box in self.filter_checks.values():
            box.setEnabled(done)

    # ------------------------------------------------------------------ lifetime
    def _release_run(self) -> None:
        self._detail_timer.stop()
        loader, self._loader = self._loader, None
        if loader is not None:
            loader.close()                  # (its fetch stops at the next entry: the token is cancelled)
            loader.deleteLater()
        st, self._run = self._run, None
        if st is not None:
            st.release()
        holder, self._holder = self._holder, None
        if holder is not None:
            holder.abandon()
        self._index = None
        self._run_state = "idle"
        self._task = None
        self.progress.setVisible(False)
        self.cancel_button.setVisible(False)
        if _alive(self.results):
            self.results.reset()

    def _release_all(self) -> None:
        """Detach every task of the window and release what it holds (idempotent; also runs
        when the window is deleted without being closed)."""
        if getattr(self, "_released", False):
            return
        self._released = True
        self._closed = True
        tasks = getattr(self.app, "tasks", None)
        if tasks is not None:
            try:
                tasks.detach(self)
            except Exception:   # pragma: no cover
                log.debug("detach failed", exc_info=True)
        loader, self._loader = self._loader, None
        if loader is not None and _alive(loader):
            loader.close()                  # no worker thread outlives the window
        st, self._run = self._run, None
        if st is not None:
            st.release()
        holder, self._holder = self._holder, None
        if holder is not None:
            holder.abandon()

    def closeEvent(self, event: Any) -> None:  # noqa: N802 (Qt name)
        self._detail_timer.stop()
        self._unwatch()
        try:
            self.view.shutdown()
        except RuntimeError:   # pragma: no cover
            pass
        tasks = getattr(self.app, "tasks", None)
        if tasks is not None:
            try:
                tasks.task_updated.disconnect(self._on_task_updated)
            except (RuntimeError, TypeError):
                pass
        self._release_all()
        super().closeEvent(event)


# ==================================================================================
# integration API
# ==================================================================================
def _window_for(app: Any, parent: Any, reveal: Reveal | None) -> CompareWindow:
    win = CompareWindow(app, parent if isinstance(parent, QWidget) else None, reveal=reveal)
    if not isinstance(parent, QWidget):
        keep_alive(win)
    return win


def compare_nodes(window: Any, side_a: tuple[Any, NodeRef], side_b: tuple[Any, NodeRef], *, app: Any = None,
                  reveal: Reveal | None = None, opts: DiffOptions | None = None) -> CompareWindow | None:
    """Open a compare window for two nodes ``(session, NodeRef)`` (of one tab or two)."""
    app = app or _app_of(window, side_a[0])
    try:
        a = capture_node_side(*side_a)
        b = capture_node_side(*side_b)
    except JsonReshapeError as exc:
        if app is not None:
            app.report_error(exc, window if isinstance(window, QWidget) else None)
        return None
    win = _window_for(app, window, reveal)
    win.compare_nodes(a, b, opts)
    win.show()
    return win


def compare_with_marked(window: Any, session: Any, ref: NodeRef, *, app: Any = None,
                        reveal: Reveal | None = None) -> CompareWindow | None:
    """Compare ``ref`` with the marked node (the mark is A); a status message when nothing is
    marked."""
    app = app or _app_of(window, session)
    mark = marked(app)
    if mark is None:
        _status(app, t("gui.compare.no_mark"))
        return None
    return compare_nodes(window, (mark.session, mark.ref), (session, ref), app=app, reveal=reveal)


def compare_selected(window: Any, session: Any, refs: Iterable[NodeRef], *, app: Any = None,
                     reveal: Reveal | None = None) -> CompareWindow | None:
    """Compare exactly two selected nodes of one tab (``None`` for another count)."""
    refs = list(refs)
    if len(refs) != 2:
        return None
    return compare_nodes(window, (session, refs[0]), (session, refs[1]), app=app, reveal=reveal)


def show_compare_files(window: Any, session: Any = None, *, sessions: Iterable[Any] | None = None,
                       app: Any = None, reveal: Reveal | None = None) -> CompareFilesDialog:
    """The "Ikki faylni taqqoslash…" dialog (A defaults to ``session``, B to another open tab);
    accepting opens a :class:`CompareWindow` and starts the comparison."""
    app = app or _app_of(window, session)
    if sessions is None:
        tabs = getattr(window, "tab_widgets", None)
        sessions = [tab.session for tab in tabs()] if callable(tabs) else ([session] if session is not None else [])
    parent = window if isinstance(window, QWidget) else None
    dlg = CompareFilesDialog(app, parent, sessions=sessions, current=session)
    dlg.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)

    def run(spec: CompareSpec) -> None:
        win = _window_for(app, window, reveal)
        win.compare_files(spec)
        win.show()
        dlg.window_opened = win     # (tests)

    dlg.spec_accepted.connect(run)
    dlg.setModal(False)
    dlg.show()                                  # non-modal (docs/ARCHITECTURE.md §14.9)
    return dlg


def node_menu_items(window: Any, session: Any, ref: NodeRef, *, app: Any = None,
                    reveal: Reveal | None = None) -> list[tuple]:
    """Items for ``actions.node_menu(..., extra=...)``: "Taqqoslash uchun belgilash" and
    "Belgilangan bilan taqqoslash" (disabled with a reason when nothing is marked or the mark
    is this node)."""
    app = app or _app_of(window, session)
    mark = marked(app)
    items: list[tuple] = [(t("gui.compare.menu.mark"), lambda: mark_for_compare(app, session, ref))]
    if mark is None:
        items.append((t("gui.compare.menu.with_marked"), lambda: None, False, t("gui.compare.menu.with_marked.none")))
    elif mark.session is session and tuple(mark.ref.path) == tuple(ref.path) and mark.ref.record == ref.record:
        items.append((t("gui.compare.menu.with_marked"), lambda: None, False, t("gui.compare.menu.with_marked.same")))
    else:
        items.append((t("gui.compare.menu.with_marked"),
                      lambda: compare_with_marked(window, session, ref, app=app, reveal=reveal), True,
                      t("gui.compare.menu.with_marked.tip", label=mark.label)))
    return items

