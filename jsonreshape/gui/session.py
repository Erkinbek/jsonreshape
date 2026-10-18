"""One open file in the GUI: :class:`DocSession` (docs/ARCHITECTURE.md §11.6).

A session owns the :class:`~jsonreshape.reader.Document`, the *view* (the ordered
:class:`~jsonreshape.recordset.RecordSet` the views show: ``sort(filter(base))``, or a
record set given with :meth:`DocSession.show_records`), the current node, the selection
history, the bookmarks and the external-change watch.

Open flow: :meth:`DocSession.start` runs ``Document.open`` in a task (kind ``open``),
then ``build_index`` in a task (kind ``index``). ``opened`` is emitted as soon as the
document exists, so the first records show while it is indexed; the live record count is
polled every 200 ms (``count_changed``). A cached complete index goes straight to
``ready``.

View pipeline: the main filter (:class:`FilterSpec`, expression or JMESPath) AND the
quick filters (column path -> expression text) run as ``filter_records`` passes (one
pass when the main filter is an expression: ``(main) and (q1) and (q2)``; a JMESPath main
filter plus quick filters = a second pass over the first result). The source of the
first pass is the whole document, or the record set shown with :meth:`DocSession.show_records`
(e.g. search results: a filter / sort then works on those records, the label says
"Qidiruv natijasi · Filtr: …"; :meth:`DocSession.reset_view` goes back to all records).
The growing result of the last pass becomes the view at once (partial results), then the
final result; a sort runs after the filter finished (on the filtered set; a sort-only
change reuses the previous filter result). A newer request cancels the running pipeline;
late results of older pipelines are ignored. A failing (or user-cancelled) pipeline
restores the last complete view and its specs (a cancel is told in the status bar);
errors are reported through ``app.report_error``. Replaced record sets are closed
:data:`RETIRE_DELAY_MS` later (loaders in other threads may still read them) or when the
session closes.

Reopening (F5, another record path / format) detaches the session's own tasks, but tasks
registered with :meth:`DocSession.keep_document_for` (exports) keep running on the old
document, which (with its record sets) is closed only after they ended; the record sets
such a task reads (the exported view) are leased when it is started, so a view replaced
while the task still waits in the heavy-task queue stays open for it; a finished task is
forgotten at once (``TaskManager.task_finished``); a reopen with
another record path / format clears the filter, quick filters, sort and shown records
(they refer to the old records). :meth:`DocSession.close` does the same and schedules
the deletion of the session object (``deleteLater``), so nothing connected to its signals
keeps a closed tab alive.

Threading: every method is called in the GUI thread **except** the blocking ones marked
"worker threads only" (:meth:`DocSession.node`, :meth:`~DocSession.resolve`,
:meth:`~DocSession.ref_for_path`, :meth:`~DocSession.ref_for_location`), which may read
and index the file. :meth:`~DocSession.record_at` and :meth:`~DocSession.view_len` may be
called from any thread. The GUI-thread methods only use cheap document calls
(``record_count``, ``info``, ``record_key`` of an indexed record: one small read).
"""

from __future__ import annotations

import logging
import os
import threading
import time
from array import array
from dataclasses import dataclass, field
from typing import Any, Callable, NamedTuple

from PySide6.QtCore import QObject, QTimer, Signal

from jsonreshape.errors import Cancelled, JsonReshapeError, UserInputError
from jsonreshape.gui.guistate import Bookmark
from jsonreshape.i18n import t
from jsonreshape.paths import Location, canonical_index, format_path, parse_path
from jsonreshape.query import Condition, Filter, Group, compile_filter
from jsonreshape.reader import Document, Node
from jsonreshape.recordset import RecordSet, filter_records, sort_records
from jsonreshape.sortkeys import SortKey, describe_keys
from jsonreshape.tasks import CancelToken, check

log = logging.getLogger(__name__)

__all__ = ["DocSession", "FilterSpec", "NodeRef", "HISTORY_MAX", "find_position", "find_positions"]

HISTORY_MAX = 200
_KEEP: Any = object()   # reopen(): keep the current option


class NodeRef(NamedTuple):
    """A node of a document: ``record`` (record number; ``None`` = outside the records:
    a single document's value, wrapper members of a wrapped document) and ``path`` (keys
    and array indices relative to the record value; absolute when ``record`` is None);
    ``ipath``: the child indices along ``path`` when known (tree identity)."""

    record: int | None
    path: tuple = ()
    ipath: tuple | None = None


def same_node(a: NodeRef | None, b: NodeRef | None) -> bool:
    """Both refer to the same node: same record and path and -- when both carry complete
    child indices (``ipath`` as long as ``path``) -- the same indices, because members with
    duplicate keys share a path."""
    if a is None or b is None:
        return a is b
    if a.record != b.record or tuple(a.path) != tuple(b.path):
        return False
    ia, ib = a.ipath, b.ipath
    if ia is not None and ib is not None and len(ia) == len(a.path) and len(ib) == len(b.path):
        return tuple(ia) == tuple(ib)
    return True


@dataclass
class FilterSpec:
    """A main filter: ``text`` in ``lang`` (``expr`` | ``jmespath``); ``builder`` =
    ``query.Group.to_dict()`` of the visual builder when it describes the same filter.
    The text is authoritative; the builder is used only when the text is empty."""

    text: str
    lang: str = "expr"
    builder: dict | None = None

    @property
    def empty(self) -> bool:
        return not (self.text or "").strip() and not self.builder

    def expr_text(self) -> str:
        """The filter text (rendered from the builder when the text is empty)."""
        if (self.text or "").strip() or not self.builder:
            return self.text
        return Group.from_dict(self.builder).to_text()

    def compile(self) -> Filter:
        """The compiled filter (``QuerySyntaxError`` / ``PathSyntaxError`` when invalid)."""
        if self.lang == "expr" and not (self.text or "").strip() and self.builder:
            return Filter.from_builder(Group.from_dict(self.builder))
        return compile_filter(self.text, self.lang)

    def to_dict(self) -> dict[str, Any]:
        return {"text": self.text, "lang": self.lang, "builder": self.builder}

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> FilterSpec:
        return cls(text=str(data.get("text") or ""), lang=str(data.get("lang") or "expr"),
                   builder=data.get("builder") if isinstance(data.get("builder"), dict) else None)


@dataclass
class _ViewState:
    """The last complete view and the specs that made it."""

    view: RecordSet | None = None
    filtered: RecordSet | None = None          # result of the filter passes (None: base)
    filtered_key: tuple | None = ()            # identity of those passes; None: not reusable
    filter: FilterSpec | None = None
    quick: dict = field(default_factory=dict)
    sort: list = field(default_factory=list)
    records_label: str | None = None
    records: RecordSet | None = None           # the shown record set the pipeline starts from
    filter_warnings: list = field(default_factory=list)   # localized warnings of the filter passes


class _Pipeline:
    """A running view computation: steps ``("filter", Filter)`` / ``("sort", keys)``."""

    __slots__ = ("gen", "steps", "source", "filtered", "filter_key", "partial", "task", "filter_warnings",
                 "sort_warnings")

    def __init__(self, gen: int) -> None:
        self.gen = gen
        self.steps: list[tuple[str, Any]] = []
        self.source: RecordSet | None = None
        self.filtered: RecordSet | None = None
        self.filter_key: tuple = ()
        self.partial: RecordSet | None = None
        self.task: Any = None
        self.filter_warnings: list[str] = []
        self.sort_warnings: list[str] = []


class _OpenHolder:
    """Hands the document of an open task to the session, or closes it when the session
    abandoned the task (closed / reopened) meanwhile."""

    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.doc: Document | None = None
        self.abandoned = False
        self.adopted = False

    def put(self, doc: Document) -> None:
        with self.lock:
            if not self.abandoned:
                self.doc = doc
                return
        doc.close()
        raise Cancelled()

    def abandon(self) -> Document | None:
        """Mark abandoned; the opened document nobody adopted (to be closed)."""
        with self.lock:
            self.abandoned = True
            doc, self.doc = self.doc, None
            return None if self.adopted else doc


def _seg_equal(a: Any, b: Any) -> bool:
    if isinstance(a, bool) or isinstance(b, bool):
        return a is b
    if type(a) is type(b):
        return a == b
    if isinstance(a, str) and isinstance(b, int):
        return canonical_index(a) == b
    if isinstance(a, int) and isinstance(b, str):
        return canonical_index(b) == a
    return False


def find_position(rs: RecordSet, record: int, cancel: CancelToken | None = None) -> int | None:
    """Position of ``record`` in ``rs`` (``None`` when absent): binary search for
    ascending sets, else a scan of the u64 chunks (``bytes.find`` of the 8-byte value
    with an alignment check)."""
    if record < 0:
        return None
    n = len(rs)
    if rs.is_ascending():
        lo, hi = 0, n
        while lo < hi:
            mid = (lo + hi) // 2
            if rs[mid] < record:
                lo = mid + 1
            else:
                hi = mid
        return lo if lo < n and rs[lo] == record else None
    needle = array("Q", [record]).tobytes()
    pos = 0
    for chunk in rs.iter_chunks():
        check(cancel)
        data = chunk.tobytes()
        start = 0
        while True:
            k = data.find(needle, start)
            if k < 0:
                break
            if k % 8 == 0:
                return pos + k // 8
            start = k + 1
        pos += len(chunk)
    return None


#: At most this many records are looked up with one ``bytes.find`` scan each; more use a
#: set intersection per chunk.
FIND_SCAN_MAX = 32


def find_positions(rs: RecordSet, records: Any, cancel: CancelToken | None = None) -> dict[int, int | None]:
    """Positions of several ``records`` in ``rs`` in one pass (``None`` when absent):
    binary searches for ascending sets; else one scan of the chunks (``bytes.find`` per
    record for a few records, a set intersection per chunk for many)."""
    out: dict[int, int | None] = {int(r): None for r in records}
    want = {r for r in out if r >= 0}
    if not want:
        return out
    if rs.is_ascending():
        for r in want:
            check(cancel)
            out[r] = find_position(rs, r, cancel)
        return out
    pos = 0
    if len(want) <= FIND_SCAN_MAX:
        needles = {r: array("Q", [r]).tobytes() for r in want}
        for chunk in rs.iter_chunks():
            check(cancel)
            data = chunk.tobytes()
            for r, needle in list(needles.items()):
                start = 0
                while True:
                    k = data.find(needle, start)
                    if k < 0:
                        break
                    if k % 8 == 0:
                        out[r] = pos + k // 8
                        del needles[r]
                        break
                    start = k + 1
            if not needles:
                break
            pos += len(chunk)
        return out
    for chunk in rs.iter_chunks():
        check(cancel)
        present = want.intersection(chunk)
        for r in present:
            out[r] = pos + chunk.index(r)
        want -= present
        if not want:
            break
        pos += len(chunk)
    return out


class DocSession(QObject):
    """One open file (see the module docstring)."""

    state_changed = Signal(str)          # new | opening | indexing | ready | failed | closed
    opened = Signal()
    open_failed = Signal(object)         # the exception (open or index failure, Cancelled included)
    index_finished = Signal()
    count_changed = Signal()             # record count / view length changed (<= 5/s while growing)
    view_changed = Signal()              # view RecordSet replaced
    view_busy_changed = Signal(bool)
    current_changed = Signal(object, object)   # (NodeRef | None, origin view or None)
    reveal_requested = Signal(object)          # NodeRef
    file_changed = Signal(str)                 # "changed" | "grown"
    warnings_changed = Signal()
    history_changed = Signal()                 # can_back / can_forward may have changed
    bookmarks_changed = Signal()

    COUNT_POLL_MS = 200      # live record count / growing view length
    CHANGE_POLL_MS = 3000    # external change check
    RETIRE_DELAY_MS = 5000   # replaced record sets are closed this much later

    def __init__(self, app: Any, path: str | os.PathLike[str], *, record_path: Any = None,
                 format: str | None = None, use_cache: bool = True, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self.app = app
        self.path = os.path.abspath(os.fspath(path))
        self.name = os.path.basename(self.path)
        self.record_path = record_path
        self.format = format
        self.use_cache = bool(use_cache)
        self.doc: Document | None = None
        self.state = "new"
        self.open_error: BaseException | None = None
        # view
        self.filter: FilterSpec | None = None
        self.quick_filters: dict[str, str] = {}
        self.sort_keys: list[SortKey] = []
        self.view_busy = False
        self.view_warnings: list[str] = []
        self._records_label: str | None = None
        self._records: RecordSet | None = None      # shown records (show_records): the pipeline's source
        self._base: RecordSet | None = None
        self._view: RecordSet | None = None
        self._view_serial = 0
        self._view_gen = 0
        self._stable = _ViewState()
        self._pipe: _Pipeline | None = None
        self._pending_specs = False
        self._retired: list[tuple[float, RecordSet]] = []
        self._pos_cache: dict[int, int | None] = {}
        self._last_count: tuple[int, int] | None = None
        # selection
        self.current: NodeRef | None = None
        self._history: list[NodeRef] = []
        self._hpos = -1
        # bookmarks
        self._bookmarks: list[Bookmark] = list(app.state.bookmarks(self.path))
        # lifetime
        self._started = False
        self._closed = False
        self._opens = 0
        self._holder: _OpenHolder | None = None
        self._doc_users: list[Any] = []              # tasks that keep the document open (exports)
        self._held: list[tuple[Any, RecordSet]] = []  # (task, record set it reads): not closed before it ended
        self._change_task: Any = None
        self._file_state: str | None = None
        self._last_appended = 0
        self._tick_timer = QTimer(self)
        self._tick_timer.setInterval(self.COUNT_POLL_MS)
        self._tick_timer.timeout.connect(self._tick)
        self._change_timer = QTimer(self)
        self._change_timer.setInterval(self.CHANGE_POLL_MS)
        self._change_timer.timeout.connect(self._check_change)
        self._retire_timer = QTimer(self)
        self._retire_timer.setInterval(max(50, min(1000, self.RETIRE_DELAY_MS // 2 or 50)))
        self._retire_timer.timeout.connect(self._retire_tick)
        # finished tasks given to keep_document_for are forgotten at once (they reference their
        # job, its dialog and its result)
        finished = getattr(getattr(app, "tasks", None), "task_finished", None)
        if finished is not None:
            finished.connect(self._on_task_finished)

    def __repr__(self) -> str:
        return f"<DocSession {self.name!r} {self.state}>"

    # ================================================================================
    # open / index
    # ================================================================================
    @property
    def closed(self) -> bool:
        return self._closed

    def start(self) -> None:
        """Open the file (background task), then index it (background task)."""
        if self._closed or self._started:
            return
        self._started = True
        self._open()

    def _run(self, fn: Callable[[Any], Any], *, title: str, kind: str, silent: bool = False,
             **callbacks: Any) -> Any:
        return self.app.tasks.run(fn, title=title, kind=kind, owner=self, silent=silent, **callbacks)

    def _set_state(self, state: str) -> None:
        if self.state == state:
            return
        self.state = state
        self.state_changed.emit(state)
        self._ensure_tick()

    def _open(self) -> None:
        self.open_error = None
        self._set_state("opening")
        holder = self._holder = _OpenHolder()
        path, rp, fmt, use_cache = self.path, self.record_path, self.format, self.use_cache
        settings = getattr(self.app, "settings", None)

        def fn(ctx: Any) -> Document:
            doc = Document.open(path, record_path=rp, format=fmt, settings=settings, use_cache=use_cache,
                                progress=ctx.progress, cancel=ctx.cancel)
            holder.put(doc)
            return doc

        self._run(fn, title=t("gui.session.task.open", name=self.name), kind="open",
                  on_done=lambda doc: self._opened(holder, doc),
                  on_error=lambda exc: self._failed(exc),
                  on_cancel=lambda: self._failed(Cancelled()))

    def _opened(self, holder: _OpenHolder, doc: Document) -> None:
        with holder.lock:
            holder.adopted = True
        self.doc = doc
        self._base = RecordSet.base(doc)
        self._view = self._base
        self._view_serial += 1
        self._stable = _ViewState(view=self._base)
        self._last_appended = doc.appended_bytes
        self._file_state = None
        self._change_timer.start(self.CHANGE_POLL_MS)
        self._opens += 1
        reopened = self._opens > 1
        if doc.index_complete:
            self._set_state("ready")
            self.opened.emit()
            if reopened and not self._closed and self.doc is doc:
                self.view_changed.emit()    # views that only follow the view see the new document
            if not self._closed and self.doc is doc:
                self._index_done_signals()
            return
        self._set_state("indexing")
        self.opened.emit()
        if reopened and not self._closed and self.doc is doc:
            self.view_changed.emit()
        if self._closed or self.doc is not doc:
            return
        self._emit_count()
        self._run(lambda ctx: doc.build_index(progress=ctx.progress, cancel=ctx.cancel),
                  title=t("gui.session.task.index", name=self.name), kind="index",
                  on_done=lambda _result: self._indexed(),
                  on_error=lambda exc: self._failed(exc),
                  on_cancel=lambda: self._failed(Cancelled()))

    def _indexed(self) -> None:
        self._set_state("ready")
        self._index_done_signals()

    def _index_done_signals(self) -> None:
        self.index_finished.emit()
        if self._closed:
            return
        self._emit_count()
        self.warnings_changed.emit()
        if not self._closed:
            self._apply_pending()

    def _failed(self, exc: BaseException) -> None:
        """Open or index failure (``Cancelled`` included): the tab decides what to show."""
        self.open_error = exc
        self._set_state("failed")
        self.open_failed.emit(exc)
        if not self._closed:
            self._emit_count()

    def reopen(self, *, record_path: Any = _KEEP, format: Any = _KEEP, use_cache: bool = True) -> None:
        """Open the file again (e.g. after it changed, or with another record path /
        format; omitted options keep their current value, ``None`` = automatic). The
        filter/sort specs are re-applied once the new index is ready (when they still
        compile and the document has records)."""
        if self._closed:
            return
        self._started = True
        new_rp = self.record_path if record_path is _KEEP else record_path
        new_fmt = self.format if format is _KEEP else format
        options_changed = (new_rp, new_fmt) != (self.record_path, self.format)
        if options_changed or self._records is not None:
            # other records (another record path / format), or shown records (search results)
            # that refer to the old record numbers: the specs do not apply any more
            self.filter, self.quick_filters, self.sort_keys = None, {}, []
            self._records = None
            self._records_label = None
        self._abandon_document()
        self._pending_specs = bool(self.filter is not None or self.quick_filters or self.sort_keys)
        self.record_path, self.format, self.use_cache = new_rp, new_fmt, bool(use_cache)
        if options_changed:
            self._history.clear()
            self._hpos = -1
            if self.current is not None:
                self.current = None
                self.current_changed.emit(None, None)
            self.history_changed.emit()
        self.view_changed.emit()
        self._emit_count()
        self._open()

    def _abandon_document(self) -> None:
        """Detach the session's tasks, stop the timers, retire the view, schedule closing
        the document(s) once the task threads ended (also the threads of the tasks given to
        :meth:`keep_document_for`, which go on)."""
        tasks = self.app.tasks.tasks(owner=self, include_silent=True)
        users = self._doc_user_tasks()
        self._doc_users = []
        self.app.tasks.detach(self)
        self._change_task = None
        self._tick_timer.stop()
        self._change_timer.stop()
        docs = []
        if self._holder is not None:
            unadopted = self._holder.abandon()
            if unadopted is not None:
                docs.append(unadopted)
            self._holder = None
        if self.doc is not None:
            docs.append(self.doc)
        pipe, self._pipe = self._pipe, None
        old_stable, self._stable = self._stable, _ViewState()
        old_sets = [self._view, old_stable.view, old_stable.filtered, old_stable.records]
        if pipe is not None:
            old_sets += [pipe.source, pipe.filtered, pipe.partial]
        self._view = None
        base, self._base = self._base, None
        self._view_serial += 1
        self._pos_cache.clear()
        self.doc = None
        self._file_state = None
        self._last_appended = 0
        if self.view_busy:
            self.view_busy = False
            self.view_busy_changed.emit(False)
        if self.view_warnings:
            self.view_warnings = []
        if users:
            # the old view may still be copied by an export: close it with the document
            sets = {id(rs): rs for rs in old_sets if rs is not None and rs is not base}
            for rs in sets.values():
                self._unretire(rs)
            _close_after(tasks + users, docs, list(sets.values()))
        else:
            for rs in old_sets:
                if rs is not base:
                    self._retire(rs)
            _close_after(tasks, docs)

    def keep_document_for(self, task: Any, sets: Any = ()) -> None:
        """Keep the current document (and the record sets of the view) open until the
        worker thread of ``task`` ended, even when the file is reopened or the session is
        closed meanwhile. For tasks not owned by the session (exports): they are not
        detached and finish on the document they started with.

        ``sets``: record sets of this session the task will read (e.g. the view an export
        writes). They are not closed before the task ended, also when the view is replaced
        meanwhile and the task still waits for its turn (a queued heavy task) -- a lease
        taken when the task is started, not when its thread runs."""
        if task is None or self._closed:
            return
        self._prune_users()
        self._doc_users.append(task)
        for rs in sets or ():
            if isinstance(rs, RecordSet) and rs is not self._base:
                self._held.append((task, rs))

    def _prune_users(self) -> None:
        self._doc_users = [x for x in self._doc_users if getattr(x, "thread_alive", False)]
        self._held = [(x, rs) for x, rs in self._held if getattr(x, "thread_alive", False)]

    def _doc_user_tasks(self) -> list[Any]:
        return [x for x in self._doc_users if getattr(x, "thread_alive", False)]

    def _holder_tasks(self, rs: RecordSet | None = None) -> list[Any]:
        """Alive tasks holding ``rs`` (any held set when ``None``)."""
        out: list[Any] = []
        for task, held in self._held:
            if (rs is None or held is rs) and getattr(task, "thread_alive", False) and task not in out:
                out.append(task)
        return out

    def held_sets(self) -> list[RecordSet]:
        """The record sets kept open for running tasks (:meth:`keep_document_for`)."""
        out: list[RecordSet] = []
        for task, rs in self._held:
            if getattr(task, "thread_alive", False) and not any(rs is x for x in out):
                out.append(rs)
        return out

    def _on_task_finished(self, task: Any) -> None:
        """A task ended: forget it when it kept the document / record sets (no reference to
        the finished job stays until the next export or the tab closes)."""
        if any(x is task for x in self._doc_users) or any(x is task for x, _rs in self._held):
            self._doc_users = [x for x in self._doc_users if x is not task]
            self._held = [(x, rs) for x, rs in self._held if x is not task]
            self._prune_users()

    @property
    def view_serial(self) -> int:
        """Changes whenever the view's record set is replaced (or the document reopened)."""
        return self._view_serial

    def close(self) -> None:
        """Detach the tasks, stop the timers, close the record sets and the document
        (once its task threads -- and those of :meth:`keep_document_for` -- ended); state
        ``closed``; the session object is deleted later (``deleteLater``: its signal
        connections go away with it). Idempotent; nothing calls back after it."""
        if self._closed:
            return
        self._closed = True
        tasks = self.app.tasks.tasks(owner=self, include_silent=True)
        users = self._doc_user_tasks()
        held = [(task, rs) for task, rs in self._held if getattr(task, "thread_alive", False)]
        self._doc_users = []
        self._held = []
        self.app.tasks.detach(self)
        for timer in (self._tick_timer, self._change_timer, self._retire_timer):
            timer.stop()
        docs = []
        if self._holder is not None:
            unadopted = self._holder.abandon()
            if unadopted is not None:
                docs.append(unadopted)
            self._holder = None
        if self.doc is not None:
            docs.append(self.doc)
        sets: dict[int, RecordSet] = {}
        pipe = self._pipe
        candidates = [self._view, self._base, self._records, self._stable.view, self._stable.filtered,
                      self._stable.records]
        if pipe is not None:
            candidates += [pipe.source, pipe.filtered, pipe.partial]
        candidates += [rs for _, rs in self._retired]
        for rs in candidates:
            if rs is not None:
                sets[id(rs)] = rs
        self._pipe = None
        self._retired.clear()
        self._view = self._base = self._records = None
        self._stable = _ViewState()
        self.doc = None
        # record sets leased by running tasks (an export started before a reopen, maybe still
        # queued) are closed once those tasks ended
        held_ids = {id(rs) for _task, rs in held}
        for rs_id in held_ids:
            rs = next(r for _t, r in held if id(r) == rs_id)
            sets.pop(rs_id, None)
            _close_after([task for task, r in held if r is rs], [], [rs])
        if users:
            _close_after(tasks + users, docs, list(sets.values()))   # an export may still copy the view
        else:
            for rs in sets.values():
                try:
                    rs.close()
                except Exception:  # pragma: no cover - close never raises
                    log.debug("closing a record set failed", exc_info=True)
            _close_after(tasks, docs)
        self.state = "closed"
        self.state_changed.emit("closed")
        self.deleteLater()

    # ================================================================================
    # warnings / external changes
    # ================================================================================
    def warning_texts(self) -> list[str]:
        """Localized warnings: the document's (format, appended data, folder fallbacks)
        and the current view's (filter/sort reports)."""
        return [text for _key, text in self.warning_items()]

    def warning_items(self) -> list[tuple[str, str]]:
        """:meth:`warning_texts` with their keys: the reader's i18n key of each document
        warning (e.g. ``reader.warn.appended``), ``"view"`` for the view's warnings."""
        out: list[tuple[str, str]] = []
        doc = self.doc
        if doc is not None:
            info = doc.info
            keys = list(getattr(info, "warnings", []) or [])
            texts = list(info.warning_texts())
            for i, text in enumerate(texts):
                out.append((str(keys[i]) if i < len(keys) else "", text))
        return out + [("view", text) for text in self.view_warnings]

    def _set_view_warnings(self, texts: list[str]) -> None:
        if texts != self.view_warnings:
            self.view_warnings = list(texts)
            self.warnings_changed.emit()

    def _check_change(self) -> None:
        doc = self.doc
        if self._closed or doc is None or self._change_task is not None or self._file_state == "changed":
            return

        def fn(ctx: Any) -> tuple[bool, int]:
            return doc.check_changed(), int(doc.appended_bytes)

        self._change_task = self._run(fn, title=t("gui.session.task.check", name=self.name), kind="check",
                                      silent=True, on_done=self._change_checked,
                                      on_error=self._change_check_failed, on_cancel=self._change_check_cancelled)

    def _change_checked(self, result: tuple[bool, int]) -> None:
        self._change_task = None
        changed, appended = result
        if changed:
            self._file_state = "changed"
            self._change_timer.stop()
            self.file_changed.emit("changed")
            return
        if appended > self._last_appended:
            self._last_appended = appended
            self._file_state = "grown"
            self.file_changed.emit("grown")
            if not self._closed:
                self.warnings_changed.emit()

    def _change_check_failed(self, exc: BaseException) -> None:
        self._change_task = None
        log.debug("change check of %s failed: %r", self.name, exc)

    def _change_check_cancelled(self) -> None:
        self._change_task = None

    # ================================================================================
    # count polling
    # ================================================================================
    def _ensure_tick(self) -> None:
        want = not self._closed and (self.state == "indexing" or self.view_busy)
        if want and not self._tick_timer.isActive():
            self._tick_timer.start(self.COUNT_POLL_MS)
        elif not want and self._tick_timer.isActive():
            self._tick_timer.stop()

    def _counts(self) -> tuple[int, int]:
        doc = self.doc
        return (int(doc.record_count) if doc is not None else 0), self.view_len()

    def _tick(self) -> None:
        if self._closed:
            return
        cur = self._counts()
        if cur != self._last_count:
            self._last_count = cur
            self.count_changed.emit()
        self._ensure_tick()

    def _emit_count(self) -> None:
        if self._closed:
            return
        self._last_count = self._counts()
        self.count_changed.emit()

    # ================================================================================
    # view
    # ================================================================================
    def view_recordset(self) -> RecordSet | None:
        """The view's record set (for export / search source); ``None`` before opening."""
        return self._view

    def view_len(self) -> int:
        """Records in the view (live while it grows). Any thread."""
        rs = self._view
        try:
            return len(rs) if rs is not None else 0
        except Exception:  # a set closed meanwhile
            return 0

    def record_at(self, pos: int) -> int:
        """Record number at view position ``pos`` (``IndexError`` when out of range).
        Any thread."""
        rs = self._view
        if rs is None:
            raise IndexError(pos)
        return int(rs[pos])

    @property
    def view_is_base(self) -> bool:
        """The view is the whole document in file order."""
        return self._view is not None and self._view is self._base

    def view_label(self) -> str:
        """Localized description of the view ("Barcha yozuvlar", "Filtr: …", "Tartib: …",
        the label given to :meth:`show_records`, or both: "Qidiruv natijasi … · Filtr: …")."""
        parts = [self._records_label] if self._records_label is not None else []
        ftext = self._filter_text()
        if ftext:
            parts.append(t("gui.session.label.filter", expr=ftext))
        if self.sort_keys:
            parts.append(t("gui.session.label.sort", keys=describe_keys(self.sort_keys)))
        return " \N{MIDDLE DOT} ".join(parts) if parts else t("gui.session.label.all")

    def _filter_text(self) -> str:
        texts = []
        if self.filter is not None:
            try:
                texts.append(self.filter.expr_text())
            except JsonReshapeError:
                texts.append(self.filter.text)
        texts += [q for q in self.quick_filters.values() if q and q.strip()]
        if len(texts) <= 1:
            return texts[0] if texts else ""
        return " and ".join(f"({x})" for x in texts)

    def _require_records(self) -> None:
        doc = self.doc
        if doc is not None and doc.kind == "single":
            raise UserInputError("gui.session.single_no_view")

    @staticmethod
    def _plan(spec: FilterSpec | None, quick: dict[str, str]) -> list[Filter]:
        """The filter passes of ``spec`` AND ``quick`` (raises the syntax errors)."""
        quick_parts = []
        for text in quick.values():
            if text and text.strip():
                compile_filter(text)   # its own error position
                quick_parts.append(f"({text})")
        passes: list[Filter] = []
        if spec is None or spec.empty:
            if quick_parts:
                passes.append(compile_filter(" and ".join(quick_parts)))
        elif spec.lang == "jmespath":
            passes.append(spec.compile())
            if quick_parts:
                passes.append(compile_filter(" and ".join(quick_parts)))
        else:
            main = spec.compile()
            if quick_parts:
                passes.append(compile_filter(" and ".join([f"({main.text})"] + quick_parts)))
            else:
                passes.append(main)
        return passes

    def _try_plan(self, spec: FilterSpec | None, quick: dict[str, str]) -> list[Filter] | None:
        try:
            return self._plan(spec, quick)
        except (JsonReshapeError, ValueError, TypeError) as exc:
            self.app.report_error(exc)
            self.view_busy_changed.emit(self.view_busy)
            return None

    @property
    def shown_records(self) -> RecordSet | None:
        """The record set given to :meth:`show_records` the view starts from (``None``:
        all records)."""
        return self._records

    @property
    def shown_records_label(self) -> str | None:
        """The label given with :meth:`show_records` (``None``: all records)."""
        return self._records_label if self._records is not None else None

    def set_filter(self, spec: FilterSpec | None) -> None:
        """Filter the records (``None``: no main filter) -- all records, or the shown
        record set (:meth:`show_records`). An invalid expression is reported and nothing
        changes. ``UserInputError`` for single documents."""
        if self._closed:
            return
        self._require_records()
        if spec is not None and spec.empty:
            spec = None
        passes = self._try_plan(spec, self.quick_filters)
        if passes is None:
            return
        self.filter = spec
        self._apply(passes)

    def set_quick_filter(self, column_path: str, expr_text: str | None) -> None:
        """Set (``None``/empty: remove) the quick filter of a table column (an
        expression, AND-ed with the main filter)."""
        if self._closed:
            return
        self._require_records()
        quick = dict(self.quick_filters)
        if expr_text is None or not expr_text.strip():
            if column_path not in quick:
                return
            del quick[column_path]
        else:
            quick[column_path] = expr_text
        passes = self._try_plan(self.filter, quick)
        if passes is None:
            return
        self.quick_filters = quick
        self._apply(passes)

    def set_quick_filters(self, mapping: dict[str, str | None] | None) -> None:
        """Replace every quick filter at once (``{column path: expression}``; empty /
        ``None`` values are dropped) with one pipeline run. An invalid expression is
        reported and nothing changes."""
        if self._closed:
            return
        self._require_records()
        quick = {str(k): str(v) for k, v in (mapping or {}).items() if v is not None and str(v).strip()}
        if quick == self.quick_filters:
            return
        passes = self._try_plan(self.filter, quick)
        if passes is None:
            return
        self.quick_filters = quick
        self._apply(passes)

    def set_sort(self, keys: list[SortKey] | None) -> None:
        """Sort the (filtered) records by ``keys`` (``None``/empty: file order)."""
        if self._closed:
            return
        self._require_records()
        norm: list[SortKey] = []
        for key in keys or []:
            if isinstance(key, SortKey):
                norm.append(key)
            elif isinstance(key, str):
                norm.append(SortKey(key))
            else:
                raise TypeError(f"expected SortKey, not {type(key).__name__}")
        passes = self._try_plan(self.filter, self.quick_filters)
        if passes is None:
            return
        self.sort_keys = norm
        self._apply(passes)

    def show_records(self, rs: RecordSet, label: str) -> None:
        """Show ``rs`` (e.g. search results) as the view; filter, quick filters and sort
        are cleared (a later filter / sort works on ``rs``). The session takes ownership:
        the set is closed (later) when it is replaced or the session closes."""
        if self._closed:
            rs.close()
            return
        self._cancel_pipeline(restore=False)
        self._view_gen += 1
        self._pending_specs = False
        self.filter, self.quick_filters, self.sort_keys = None, {}, []
        old = self._records
        self._records = rs
        self._records_label = str(label)
        self._install(rs)
        self._set_stable(_ViewState(view=rs, filtered=None, filtered_key=None, records_label=self._records_label,
                                    records=rs))
        if old is not None and old is not rs:
            self._retire(old)
        self._set_view_warnings([])
        self._set_busy(False)
        self._emit_count()

    def reset_view(self) -> None:
        """All records in file order (filter, quick filters, sort and shown records cleared)."""
        if self._closed:
            return
        self._cancel_pipeline(restore=False)
        self._view_gen += 1
        self._pending_specs = False
        self.filter, self.quick_filters, self.sort_keys = None, {}, []
        old = self._records
        self._records = None
        self._records_label = None
        if self._base is not None:
            self._install(self._base)
        self._set_stable(_ViewState(view=self._base))
        if old is not None:
            self._retire(old)
        self._set_view_warnings([])
        self._set_busy(False)
        self._emit_count()

    def _apply(self, passes: list[Filter]) -> None:
        if self.doc is None or self.state in ("opening", "new"):
            self._pending_specs = True   # applied when the index is ready
            return
        self._pending_specs = False
        self._start_pipeline(passes)

    def _apply_pending(self) -> None:
        if not self._pending_specs or self.doc is None:
            return
        self._pending_specs = False
        if self.doc.kind == "single":
            self.filter, self.quick_filters, self.sort_keys = None, {}, []
            return
        try:
            passes = self._plan(self.filter, self.quick_filters)
        except (JsonReshapeError, ValueError, TypeError) as exc:
            log.info("dropping the filter of %s after reopening: %s", self.name, exc)
            self.filter, self.quick_filters = None, {}
            passes = []
        if passes or self.sort_keys:
            self._start_pipeline(passes)

    # -- pipeline ----------------------------------------------------------------------------
    def _start_pipeline(self, passes: list[Filter]) -> None:
        self._cancel_pipeline(restore=True)
        self._view_gen += 1
        sort = list(self.sort_keys)
        origin = self._records                   # None: the whole document
        fkey = tuple((f.lang, f.text) for f in passes)
        key = ((("records", id(origin)),) + fkey) if (origin is not None and fkey) else fkey
        st = self._stable
        source: RecordSet | None = origin
        filtered: RecordSet | None = None
        steps: list[tuple[str, Any]] = []
        if not fkey:
            pass
        elif st.filtered_key == key and st.filtered is not None and st.filtered.complete and not st.filtered.closed:
            source = filtered = st.filtered      # sort-only change: reuse the filter result
        else:
            steps = [("filter", f) for f in passes]
        if sort:
            steps.append(("sort", sort))
        if not steps:
            target = filtered if filtered is not None else (origin if origin is not None else self._base)
            fw = list(st.filter_warnings) if filtered is not None else []
            self._install(target)
            self._set_stable(_ViewState(view=target, filtered=filtered, filtered_key=key, filter=self.filter,
                                        quick=dict(self.quick_filters), sort=[], filter_warnings=fw,
                                        records_label=self._records_label, records=origin))
            self._set_view_warnings(fw)
            self._set_busy(False)
            self._emit_count()
            return
        pipe = _Pipeline(self._view_gen)
        pipe.steps = steps
        pipe.source = source
        pipe.filtered = filtered
        pipe.filter_key = key
        if filtered is not None:
            pipe.filter_warnings = list(st.filter_warnings)
        self._pipe = pipe
        self._set_busy(True)
        self._next_step(pipe)

    def _next_step(self, pipe: _Pipeline) -> None:
        if pipe is not self._pipe:
            return
        if not pipe.steps:
            self._finish_pipeline(pipe)
            return
        kind, arg = pipe.steps.pop(0)
        doc = self.doc
        src = pipe.source
        if kind == "filter":
            last = not any(k == "filter" for k, _ in pipe.steps)
            flt = arg

            def run_filter(ctx: Any) -> RecordSet:
                return filter_records(doc, flt, src, progress=ctx.progress, cancel=ctx.cancel,
                                      on_batch=ctx.post if last else None)

            pipe.task = self._run(run_filter, title=t("gui.session.task.filter", name=self.name), kind="filter", heavy=True,
                                  on_item=lambda rs: self._partial(pipe, rs),
                                  on_done=lambda rs: self._filter_done(pipe, rs, last),
                                  on_error=lambda exc: self._pipeline_failed(pipe, exc),
                                  on_cancel=lambda: self._pipeline_failed(pipe, None))
        else:
            keys = arg

            def run_sort(ctx: Any) -> RecordSet:
                return sort_records(doc, keys, src, progress=ctx.progress, cancel=ctx.cancel)

            pipe.task = self._run(run_sort, title=t("gui.session.task.sort", name=self.name), kind="sort", heavy=True,
                                  on_done=lambda rs: self._sort_done(pipe, rs),
                                  on_error=lambda exc: self._pipeline_failed(pipe, exc),
                                  on_cancel=lambda: self._pipeline_failed(pipe, None))

    def _partial(self, pipe: _Pipeline, rs: Any) -> None:
        if pipe is not self._pipe or not isinstance(rs, RecordSet) or rs.closed:
            return
        pipe.partial = rs
        self._install(rs)

    @staticmethod
    def _report_warnings(rs: RecordSet) -> list[str]:
        report = getattr(rs, "report", None)
        if report is None:
            return []
        try:
            return list(report.warning_texts())
        except Exception:  # pragma: no cover - defensive
            log.debug("warning texts failed", exc_info=True)
            return []

    def _filter_done(self, pipe: _Pipeline, rs: RecordSet, last: bool) -> None:
        if pipe is not self._pipe:
            self._retire(rs)
            return
        pipe.filter_warnings.extend(self._report_warnings(rs))
        old = pipe.source
        pipe.source = rs
        if old is not None and old is not rs:
            self._retire(old)   # the first pass's result after the second pass
        if last:
            pipe.filtered = rs
            pipe.partial = None
            self._install(rs)
        self._next_step(pipe)

    def _sort_done(self, pipe: _Pipeline, rs: RecordSet) -> None:
        if pipe is not self._pipe:
            self._retire(rs)
            return
        pipe.sort_warnings.extend(self._report_warnings(rs))
        pipe.source = rs
        self._install(rs)
        self._next_step(pipe)

    def _finish_pipeline(self, pipe: _Pipeline) -> None:
        self._pipe = None
        self._set_stable(_ViewState(view=self._view, filtered=pipe.filtered, filtered_key=pipe.filter_key,
                                    filter=self.filter, quick=dict(self.quick_filters), sort=list(self.sort_keys),
                                    filter_warnings=list(pipe.filter_warnings), records_label=self._records_label,
                                    records=self._records))
        self._set_view_warnings(pipe.filter_warnings + pipe.sort_warnings)
        self._set_busy(False)
        self._emit_count()

    def _pipeline_failed(self, pipe: _Pipeline, exc: BaseException | None) -> None:
        """A step failed (``exc``) or was cancelled by the user (``None``): back to the
        last complete view and its specs."""
        if pipe is not self._pipe:
            return
        self._pipe = None
        for rs in (pipe.source, pipe.filtered, pipe.partial):
            self._retire(rs)
        st = self._stable
        self.filter, self.quick_filters, self.sort_keys = st.filter, dict(st.quick), list(st.sort)
        self._records_label = st.records_label
        self._records = st.records
        if st.view is not None:
            self._install(st.view)
        self._set_busy(False)
        self._emit_count()
        if exc is None:
            # cancelled by the user (tasks panel): say why the previous view came back
            show_status = getattr(self.app, "show_status", None)
            if callable(show_status) and not self._closed:
                show_status(t("gui.session.view_cancelled", label=self.view_label()), 8000)
        elif not isinstance(exc, Cancelled):
            self.app.report_error(exc)

    def _cancel_pipeline(self, *, restore: bool) -> None:
        pipe = self._pipe
        if pipe is None:
            return
        self._pipe = None
        if pipe.task is not None:
            pipe.task.cancel()
        for rs in (pipe.source, pipe.filtered, pipe.partial):
            self._retire(rs)
        if restore and self._stable.view is not None and self._view is not self._stable.view:
            self._install(self._stable.view)

    def _set_busy(self, busy: bool) -> None:
        if busy != self.view_busy:
            self.view_busy = busy
            self.view_busy_changed.emit(busy)
        self._ensure_tick()

    def _install(self, rs: RecordSet | None) -> None:
        old = self._view
        if rs is old:
            return
        self._view = rs
        self._view_serial += 1
        self._pos_cache.clear()
        self._retire(old)
        self.view_changed.emit()
        self._emit_count()

    def _set_stable(self, state: _ViewState) -> None:
        old, self._stable = self._stable, state
        for rs in (old.view, old.filtered):
            self._retire(rs)

    # -- deferred closing of replaced record sets ---------------------------------------------
    def _live_ids(self) -> set[int]:
        live = [self._view, self._base, self._records, self._stable.view, self._stable.filtered, self._stable.records]
        pipe = self._pipe
        if pipe is not None:
            live += [pipe.source, pipe.filtered, pipe.partial]
        return {id(rs) for rs in live if rs is not None}

    def _retire(self, rs: RecordSet | None) -> None:
        if rs is None or rs is self._base or rs is self._records or self._closed:
            return
        if any(r is rs for _, r in self._retired):
            return
        self._retired.append((time.monotonic() + self.RETIRE_DELAY_MS / 1000.0, rs))
        if not self._retire_timer.isActive():
            self._retire_timer.start()

    def _unretire(self, rs: RecordSet) -> None:
        self._retired = [(d, r) for d, r in self._retired if r is not rs]

    def _retire_tick(self) -> None:
        now = time.monotonic()
        live = self._live_ids()
        self._prune_users()
        keep = []
        for deadline, rs in self._retired:
            if id(rs) in live:
                continue              # in use again: retired again when it is replaced
            if deadline > now or self._holder_tasks(rs):
                keep.append((deadline, rs))   # a task (export) still reads it: closed after it ended
                continue
            try:
                rs.close()
            except Exception:  # pragma: no cover - close never raises
                log.debug("closing a record set failed", exc_info=True)
        self._retired = keep
        if not keep:
            self._retire_timer.stop()

    # ================================================================================
    # positions
    # ================================================================================
    def position_of(self, record: int, callback: Callable[[int | None], Any]) -> None:
        """Position of ``record`` in the view, delivered as ``callback(pos | None)`` in
        the GUI thread (asynchronously, also for the base view)."""
        if self._closed:
            return
        rs = self._view
        if rs is None or rs is self._base:
            count = self.doc.record_count if self.doc is not None else 0
            result = record if 0 <= record < count else None
            QTimer.singleShot(0, self, lambda: self._deliver(callback, result))
            return
        if record in self._pos_cache:
            cached = self._pos_cache[record]
            QTimer.singleShot(0, self, lambda: self._deliver(callback, cached))
            return
        serial = self._view_serial
        self._run(lambda ctx: find_position(rs, record, ctx.cancel), title=t("gui.session.task.locate", name=self.name),
                  kind="locate", silent=True,
                  on_done=lambda pos: self._located(serial, rs, record, pos, callback),
                  on_error=lambda exc: self._deliver(callback, None),
                  on_cancel=lambda: None)

    def positions_of(self, records: Any, callback: Callable[[dict[int, int | None]], Any]) -> None:
        """Positions of several records in the view, delivered as one
        ``callback({record: pos | None})`` in the GUI thread (one silent task scanning the
        view once; results of a complete view are cached)."""
        if self._closed:
            return
        wanted = list(dict.fromkeys(int(r) for r in records))
        rs = self._view
        if rs is None or rs is self._base:
            count = self.doc.record_count if self.doc is not None else 0
            result = {r: (r if 0 <= r < count else None) for r in wanted}
            QTimer.singleShot(0, self, lambda: self._deliver(callback, result))
            return
        known = {r: self._pos_cache[r] for r in wanted if r in self._pos_cache}
        missing = [r for r in wanted if r not in known]
        if not missing:
            QTimer.singleShot(0, self, lambda: self._deliver(callback, known))
            return
        serial = self._view_serial

        def done(found: dict[int, int | None]) -> None:
            if serial != self._view_serial:
                self.positions_of(wanted, callback)   # the view changed meanwhile: ask again
                return
            if rs.complete:
                self._pos_cache.update(found)
            self._deliver(callback, {r: (known[r] if r in known else found.get(r)) for r in wanted})

        self._run(lambda ctx: find_positions(rs, missing, ctx.cancel),
                  title=t("gui.session.task.locate", name=self.name), kind="locate", silent=True,
                  on_done=done, on_error=lambda exc: self._deliver(callback, {r: known.get(r) for r in wanted}),
                  on_cancel=lambda: None)

    def _located(self, serial: int, rs: RecordSet, record: int, pos: int | None,
                 callback: Callable[[int | None], Any]) -> None:
        if serial != self._view_serial:
            self.position_of(record, callback)   # the view changed meanwhile: ask again
            return
        if rs.complete:
            self._pos_cache[record] = pos
        self._deliver(callback, pos)

    def _deliver(self, callback: Callable[[Any], Any], value: Any) -> None:
        if self._closed:
            return
        try:
            callback(value)
        except Exception:
            log.exception("position_of callback failed")

    # ================================================================================
    # node references
    # ================================================================================
    def _record_base(self, doc: Document) -> tuple:
        return tuple(doc.info.record_path or ()) if doc.kind == "wrapped" else ()

    def _require_doc(self) -> Document:
        doc = self.doc
        if doc is None:
            raise UserInputError("gui.session.not_open", name=self.name)
        return doc

    def absolute_path(self, ref: NodeRef) -> tuple:
        """The absolute path of ``ref`` in the document (record base path + record
        segment + relative path). Member records read their key (a small read)."""
        if ref.record is None:
            return tuple(ref.path)
        doc = self.doc
        if doc is None or doc.kind == "single":
            return tuple(ref.path)
        seg = doc.record_key(ref.record) if doc.member_records else ref.record
        return self._record_base(doc) + (seg,) + tuple(ref.path)

    def location_text(self, ref: NodeRef) -> str:
        """``#12.a[0]`` / ``data.items`` / ``$``: text :func:`jsonreshape.paths.parse_location`
        reads back."""
        text = format_path(tuple(ref.path))
        if ref.record is None:
            return text or "$"
        if not text:
            return f"#{ref.record}"
        return f"#{ref.record}" + (text if text.startswith("[") else "." + text)

    def _not_found(self, ref: NodeRef) -> UserInputError:
        return UserInputError("reader.path_not_found", path=self.location_text(ref))

    def _record_node(self, doc: Document, record: int, cancel: CancelToken | None) -> Node:
        """Node of record ``record``, waiting while the index is being built."""
        while doc.record_count <= record and not doc.index_complete and self.state == "indexing" \
                and doc is self.doc:
            check(cancel)
            time.sleep(0.02)
        if record < 0 or record >= doc.record_count:
            if doc.index_complete:
                key = "gui.session.record_not_found"
            elif self.state == "indexing" and doc is self.doc:
                key = "gui.session.record_not_found_partial"
            else:
                # indexing stopped (cancelled / failed): it is not going on, so "try again
                # when indexing has finished" would never come true
                key = "gui.session.record_not_found_stopped"
            raise UserInputError(key, record=record, count=doc.record_count)
        return doc.record_node(record)

    def node(self, ref: NodeRef, cancel: CancelToken | None = None, *, doc: Document | None = None) -> Node:
        """The :class:`~jsonreshape.reader.Node` of ``ref`` (``UserInputError`` when it
        does not exist) in the session's document, or in ``doc`` (a document captured
        earlier: an export started before a reopen goes on with it). Blocking: worker
        threads only."""
        doc = doc if doc is not None else self._require_doc()
        if ref.record is None and ref.ipath is None:
            try:
                return doc.node(tuple(ref.path), cancel=cancel)
            except UserInputError:
                raise self._not_found(ref) from None
        cur = doc.root() if ref.record is None else self._record_node(doc, ref.record, cancel)
        path = tuple(ref.path)
        ipath = ref.ipath
        if ipath is not None and len(ipath) == len(path):
            try:
                node = cur
                for idx, seg in zip(ipath, path):
                    check(cancel)
                    node = node.child(idx)
                    if not _seg_equal(node.key, seg):
                        raise LookupError(seg)
                return node
            except (IndexError, LookupError):
                pass   # stale ipath: search by key
        for seg in path:
            check(cancel)
            nxt = cur.find_child(seg, cancel=cancel)
            if nxt is None:
                raise self._not_found(ref)
            cur = nxt
        return cur

    def resolve(self, ref: NodeRef, cancel: CancelToken | None = None) -> NodeRef:
        """``ref`` with ``ipath`` filled in and the path normalized (array indices as
        ``int``); ``UserInputError`` when the node does not exist. Blocking: worker
        threads only."""
        doc = self._require_doc()
        cur = doc.root() if ref.record is None else self._record_node(doc, ref.record, cancel)
        npath: list = []
        ipath: list[int] = []
        for seg in ref.path:
            check(cancel)
            idx = cur.find_child_index(seg, cancel=cancel)
            if idx is None:
                raise self._not_found(ref)
            child = cur.child(idx)
            npath.append(child.key if child.key is not None else seg)
            ipath.append(idx)
            cur = child
        return NodeRef(ref.record, tuple(npath), tuple(ipath))

    def ref_for_path(self, abs_path: Any, cancel: CancelToken | None = None) -> NodeRef:
        """The resolved NodeRef of an absolute path (text or segments): a record ref
        when the path goes through a record (member keys: the LAST record with that key,
        like ``doc.node``), else ``NodeRef(None, path)``. Blocking: worker threads only."""
        doc = self._require_doc()
        path = tuple(parse_path(abs_path) if isinstance(abs_path, str) else abs_path)
        if doc.kind == "single":
            return self.resolve(NodeRef(None, path), cancel)
        base = self._record_base(doc)
        n = len(base)
        if len(path) > n and all(_seg_equal(a, b) for a, b in zip(path[:n], base)):
            records = doc.records_node() if doc.kind == "wrapped" else doc.root()
            if records is None:  # pragma: no cover - single handled above
                raise self._not_found(NodeRef(None, path))
            idx = records.find_child_index(path[n], cancel=cancel)
            if idx is None:
                raise self._not_found(NodeRef(None, path))
            return self.resolve(NodeRef(idx, path[n + 1:]), cancel)
        return self.resolve(NodeRef(None, path), cancel)

    def ref_for_location(self, loc: Location, cancel: CancelToken | None = None) -> NodeRef:
        """The resolved NodeRef of a Ctrl+G location (``#N`` + relative path, or an
        absolute path). Blocking: worker threads only."""
        doc = self._require_doc()
        if loc.record is None:
            return self.ref_for_path(loc.path, cancel)
        if doc.kind == "single":
            if loc.record != 0:
                raise UserInputError("gui.session.record_not_found", record=loc.record, count=1)
            return self.resolve(NodeRef(None, tuple(loc.path)), cancel)
        return self.resolve(NodeRef(loc.record, tuple(loc.path)), cancel)

    # ================================================================================
    # selection & history
    # ================================================================================
    @property
    def can_back(self) -> bool:
        return self._hpos > 0

    @property
    def can_forward(self) -> bool:
        return 0 <= self._hpos < len(self._history) - 1

    def history(self) -> list[NodeRef]:
        """The selection history (oldest first) -- a copy."""
        return list(self._history)

    def select(self, ref: NodeRef | None, *, origin: Any = None, history: bool = True,
               reveal: bool = False) -> None:
        """Make ``ref`` the current node: ``current_changed(ref, origin)`` (skipped when
        it already is current, unless ``reveal``), a history entry (``history=True``) and,
        with ``reveal``, ``reveal_requested(ref)``."""
        if self._closed:
            return
        if same_node(ref, self.current) and not reveal:
            if ref is not None and ref.ipath is not None and self.current is not None and self.current.ipath is None:
                self.current = ref   # the same node, now with its tree identity
            return
        self.current = ref
        if history and ref is not None:
            self._push_history(ref)
        self.current_changed.emit(ref, origin)
        if reveal and ref is not None and not self._closed:
            self.reveal_requested.emit(ref)

    def _push_history(self, ref: NodeRef) -> None:
        if 0 <= self._hpos < len(self._history) and same_node(self._history[self._hpos], ref):
            self._history[self._hpos] = ref
            return
        del self._history[self._hpos + 1:]
        self._history.append(ref)
        if len(self._history) > HISTORY_MAX:
            del self._history[:len(self._history) - HISTORY_MAX]
        self._hpos = len(self._history) - 1
        self.history_changed.emit()

    def back(self) -> None:
        """Go to the previous node of the history (``current_changed(ref, None)`` +
        ``reveal_requested``)."""
        if self.can_back and not self._closed:
            self._go(self._hpos - 1)

    def forward(self) -> None:
        if self.can_forward and not self._closed:
            self._go(self._hpos + 1)

    def _go(self, pos: int) -> None:
        self._hpos = pos
        ref = self._history[pos]
        self.current = ref
        self.history_changed.emit()
        self.current_changed.emit(ref, None)
        if not self._closed:
            self.reveal_requested.emit(ref)

    # ================================================================================
    # actions
    # ================================================================================
    def filter_by_value(self, ref: NodeRef, value: Any) -> bool:
        """AND ``<path> == value`` to the filter: the builder is extended when the
        current expression filter has one, else ``(<old>) and <cond>``; with a JMESPath
        main filter it becomes a quick filter keyed by the path. Only for nodes inside a
        record (else False)."""
        doc = self.doc
        if ref is None or ref.record is None or doc is None or doc.kind == "single":
            return False
        field_text = format_path(tuple(ref.path)) or "$"
        cond = Condition(field=field_text, op="eq", value=value)
        try:
            cond_text = cond.to_text()
        except (JsonReshapeError, ValueError, TypeError) as exc:
            self.app.report_error(exc)
            return False
        spec = self.filter
        if spec is not None and not spec.empty and spec.lang == "jmespath":
            self.set_quick_filter(field_text, cond_text)
            return True
        new: FilterSpec | None = None
        if spec is None or spec.empty:
            group = Group(items=[cond])
            new = FilterSpec(text=cond_text, lang="expr", builder=group.to_dict())
        elif spec.builder:
            try:
                group = Group.from_dict(spec.builder)
                if spec.text.strip() and spec.text.strip() != group.to_text().strip():
                    raise ValueError("the builder does not describe the filter text")
                if group.op.lower() == "and" and not group.negate:
                    group.items.append(cond)
                else:
                    group = Group(op="and", items=[group, cond])
                new = FilterSpec(text=group.to_text(), lang="expr", builder=group.to_dict())
            except (JsonReshapeError, ValueError, TypeError, AttributeError, KeyError):
                new = None   # extend the text instead
        if new is None:
            assert spec is not None
            new = FilterSpec(text=f"({spec.expr_text()}) and {cond_text}", lang="expr", builder=None)
        self.set_filter(new)
        return True

    def sort_by_field(self, path: Any, descending: bool = False) -> None:
        """Sort by one field (``path``: segments or path text; ``()`` = the record)."""
        if isinstance(path, str):
            text = path.strip() or "$"
        else:
            text = format_path(tuple(path)) or "$"
        self.set_sort([SortKey(text, descending=bool(descending))])

    # ================================================================================
    # bookmarks
    # ================================================================================
    @property
    def bookmarks(self) -> list[Bookmark]:
        return list(self._bookmarks)

    def bookmark_ref(self, bookmark: Bookmark | int) -> NodeRef:
        bm = self._bookmarks[bookmark] if isinstance(bookmark, int) else bookmark
        return NodeRef(bm.record, tuple(bm.path))

    def add_bookmark(self, name: str, ref: NodeRef) -> Bookmark:
        """Bookmark ``ref`` (saved in the GUI state for this file)."""
        name = (name or "").strip() or self.location_text(ref)
        bm = Bookmark(name=name, record=ref.record, path=list(ref.path))
        self._bookmarks.append(bm)
        self._save_bookmarks()
        return bm

    def remove_bookmark(self, i: int) -> None:
        del self._bookmarks[i]
        self._save_bookmarks()

    def rename_bookmark(self, i: int, name: str) -> None:
        name = (name or "").strip()
        if not name:
            return
        self._bookmarks[i].name = name
        self._save_bookmarks()

    def _save_bookmarks(self) -> None:
        state = self.app.state
        state.set_bookmarks(self.path, self._bookmarks)
        save_state = getattr(self.app, "save_state", None)
        if callable(save_state):
            save_state()          # a silent background task: the file lock never blocks the GUI
        else:  # pragma: no cover - a bare context without the background saver
            state.save()
        self.bookmarks_changed.emit()


def _close_after(tasks: list[Any], docs: list[Document], sets: list[RecordSet] | None = None) -> None:
    """Close ``sets`` and ``docs`` once the worker threads of ``tasks`` ended (at once
    when they did): a document must not be closed under a running build or export."""
    docs = [d for d in docs if d is not None]
    sets = [rs for rs in (sets or []) if rs is not None]
    if not docs and not sets:
        return
    alive = [task for task in tasks if task.thread_alive]

    def close_all() -> None:
        for task in alive:
            # an export of a big file may take long: wait for it without a limit when it
            # keeps the document (it ends on cancel), builds get 120 s
            if not task.join(None if getattr(task, "kind", "") == "export" else 120):
                log.warning("task %r did not stop; closing its document anyway", task)
        for rs in sets:
            try:
                rs.close()
            except Exception:
                log.exception("closing a record set failed")
        for doc in docs:
            try:
                doc.close()
            except Exception:
                log.exception("closing %r failed", doc)

    if not alive:
        close_all()
        return
    threading.Thread(target=close_all, name="jsonreshape-gui-close", daemon=True).start()
