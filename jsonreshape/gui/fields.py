"""Shared structure (schema) of a document for the GUI: one cache per :class:`DocSession`.

The table (default columns, column chooser), the filter builder and the sort dialog (field
lists) and the schema view all need the fields of the records. :func:`get_schema` computes
:func:`jsonreshape.schema.infer_schema` once per opened document in a background task — a
sample of ``Settings.sample_size`` records, or every record when ``full=True`` — and hands
it to every caller; a full scan replaces the sample for everyone once it is done.

Edits (docs/ARCHITECTURE.md §12.9): a cached schema remembers the ``session.data_gen`` (the
overlay-state gen the session shows) it was computed for. The session calls :func:`forget` for
ChangeSets that may change the structure a lot (bulk, patch, restore, discard, resets and steps
touching more than 10 000 records); after other edits the cached schema is kept but
:func:`is_stale` tells (the schema view recomputes it when visible, the field lists of the
table / filter / sort dialogs keep using it). The computation reads one pinned state (the one
shown when it starts).

Stage 5:

* The field lists (:func:`field_paths`, :func:`default_columns`, :func:`is_container`,
  :func:`under_array`, :data:`DEFAULT_COLUMN_LIMIT`, :data:`DEFAULT_COLUMN_DEPTH`,
  :data:`CONTAINER_TYPES`) are the core helpers of :mod:`jsonreshape.schema` (docs/ARCHITECTURE.md
  §14.1; the CLI uses them without importing the GUI), re-exported here unchanged -- the same
  rules this module had (tests/test_gui_roundb_views.py compares them with the former code).
* :func:`store` feeds a schema computed elsewhere into the cache (e.g. the full scan of the JSON
  Schema generate dialog), so every view can use it -- only when it belongs to the document and
  the data generation the session shows.
"""

from __future__ import annotations

import logging
import weakref
from typing import Any, Callable

from jsonreshape.i18n import t
from jsonreshape.schema import (
    CONTAINER_TYPES,
    DEFAULT_COLUMN_DEPTH,
    DEFAULT_COLUMN_LIMIT,
    Schema,
    default_columns,
    field_paths,
    infer_schema,
    is_container,
    under_array,
)

log = logging.getLogger(__name__)

__all__ = [
    "CONTAINER_TYPES",
    "DEFAULT_COLUMN_DEPTH",
    "DEFAULT_COLUMN_LIMIT",
    "FailedCallback",
    "SchemaCallback",
    "cached_schema",
    "default_columns",
    "field_paths",
    "forget",
    "get_schema",
    "is_container",
    "is_stale",
    "note_same_data",
    "schema_gen",
    "store",
    "under_array",
]

SchemaCallback = Callable[[Schema], Any]
#: Called instead of the schema callback when the computation failed (the exception) or was
#: cancelled / superseded by a reopened document (``None``).
FailedCallback = Callable[[BaseException | None], Any]


class _Entry:
    """Cached schemas of one session (valid for one ``Document`` object)."""

    __slots__ = ("doc_ref", "sample", "full", "waiting", "running", "data_gen", "alias")

    def __init__(self, doc: Any) -> None:
        self.doc_ref = weakref.ref(doc)  # identity of the Document object (a reopened file is a new one)
        self.sample: Schema | None = None
        self.full: Schema | None = None
        self.waiting: dict[bool, list[tuple[SchemaCallback | None, FailedCallback | None]]] = {False: [], True: []}
        self.running: dict[bool, Any] = {}   # full flag -> GuiTask
        self.data_gen: int | None = None     # session.data_gen of the cached schemas
        self.alias: tuple[int, int] | None = None   # (old gen, new gen) showing the same data


_cache: "weakref.WeakKeyDictionary[Any, _Entry]" = weakref.WeakKeyDictionary()


def _entry(session: Any) -> _Entry | None:
    doc = getattr(session, "doc", None)
    if doc is None:
        return None
    entry = _cache.get(session)
    if entry is None or entry.doc_ref() is not doc:
        if entry is not None:  # the session reopened the file: old tasks are useless
            for task in entry.running.values():
                task.cancel()
            _fail_all(entry, None)
        entry = _Entry(doc)
        _cache[session] = entry
    return entry


def cached_schema(session: Any, *, full: bool = False) -> Schema | None:
    """The cached schema without starting anything (``full=False``: the full one when
    present, else the sample)."""
    entry = _entry(session)
    if entry is None:
        return None
    if full:
        return entry.full
    return entry.full or entry.sample


def get_schema(session: Any, callback: SchemaCallback | None = None, *, full: bool = False,
               refresh: bool = False, on_failed: FailedCallback | None = None) -> Schema | None:
    """The cached schema of ``session``'s document, or ``None`` while it is computed.

    When nothing suitable is cached (or ``refresh``), a background task computes it and
    ``callback(schema)`` is called on the GUI thread when it is ready (callbacks of the
    same request are queued). When the computation fails (the error is also reported
    through the app context) ``on_failed(exc)`` is called instead; when it is cancelled or
    its document was replaced (reopen, :func:`forget`) ``on_failed(None)`` -- so a waiting
    view can drop its "computing" state. ``full=False`` accepts the sample or a full
    schema; ``full=True`` wants every record analysed. Neither callback is called when a
    cached schema is returned.
    """
    entry = _entry(session)
    if entry is None:
        return None
    if not refresh:
        have = entry.full if full else (entry.full or entry.sample)
        if have is not None:
            return have
    if callback is not None or on_failed is not None:
        entry.waiting[full].append((callback, on_failed))
    if full not in entry.running:
        _start(session, entry, full)
    return None


def _notify_failed(waiters: list[tuple[SchemaCallback | None, FailedCallback | None]],
                   exc: BaseException | None) -> None:
    for _cb, on_failed in waiters:
        if on_failed is None:
            continue
        try:
            on_failed(exc)
        except Exception:
            log.exception("schema failure callback failed")


def _fail_all(entry: _Entry, exc: BaseException | None) -> None:
    for full in (False, True):
        waiters = entry.waiting[full]
        entry.waiting[full] = []
        _notify_failed(waiters, exc)


def _data_gen(session: Any) -> int:
    try:
        return int(getattr(session, "data_gen", 0) or 0)
    except (TypeError, ValueError):
        return 0


def is_stale(session: Any) -> bool:
    """A schema is cached for ``session`` but was computed for another data generation (the
    document was edited since): it may miss or show fields (the schema view recomputes it)."""
    entry = _cache.get(session)
    if entry is None or entry.doc_ref() is not getattr(session, "doc", None):
        return False
    if entry.sample is None and entry.full is None:
        return False
    return entry.data_gen != _data_gen(session)


def note_same_data(session: Any, old_gen: int, new_gen: int) -> None:
    """The session now shows a state with the same data as ``old_gen`` (editing became
    possible: its unedited initial state): a schema of ``old_gen`` -- cached or still being
    computed -- stays fresh."""
    entry = _entry(session)
    if entry is None:
        return
    entry.alias = (old_gen, new_gen)
    if entry.data_gen == old_gen:
        entry.data_gen = new_gen


def schema_gen(session: Any) -> int | None:
    """The ``session.data_gen`` the cached schema was computed for (``None``: nothing cached)."""
    entry = _cache.get(session)
    if entry is None or entry.doc_ref() is not getattr(session, "doc", None):
        return None
    if entry.sample is None and entry.full is None:
        return None
    return entry.data_gen


def _start(session: Any, entry: _Entry, full: bool) -> None:
    live = session.doc
    pinned = getattr(live, "pinned", None)
    doc = pinned() if callable(pinned) else live   # the state shown now (edits made later do not mix in)
    gen = _data_gen(session)
    app = session.app
    sample = None if full else max(1, int(app.settings.sample_size))
    name = live.source_path.name
    title = t("gui.fields.task.full", name=name) if full else t("gui.fields.task.sample", name=name)

    def work(ctx: Any) -> Schema:
        return infer_schema(doc, None, sample=sample, progress=ctx.progress, cancel=ctx.cancel)

    def done(schema: Schema) -> None:
        entry.running.pop(full, None)
        if entry.doc_ref() is not getattr(session, "doc", None):
            waiters = entry.waiting[full]   # the document was replaced: nobody can use it
            entry.waiting[full] = []
            _notify_failed(waiters, None)
            return
        result_gen = gen
        if entry.alias is not None and result_gen == entry.alias[0]:
            result_gen = entry.alias[1]
        if entry.data_gen != result_gen:   # the other cached schema belongs to other data
            entry.full = entry.sample = None
        entry.data_gen = result_gen
        if full or schema.complete:
            entry.full = schema
        if not full:
            entry.sample = schema
        callbacks = entry.waiting[full]
        entry.waiting[full] = []
        if full or schema.complete:  # a full schema also answers the sample requests
            callbacks += entry.waiting[False]
            entry.waiting[False] = []
        for cb, _failed in callbacks:
            if cb is None:
                continue
            try:
                cb(schema)
            except Exception:
                log.exception("schema callback failed")

    def failed(exc: BaseException) -> None:
        entry.running.pop(full, None)
        waiters = entry.waiting[full]
        entry.waiting[full] = []
        app.report_error(exc)
        _notify_failed(waiters, exc)

    def cancelled() -> None:
        entry.running.pop(full, None)
        waiters = entry.waiting[full]
        entry.waiting[full] = []
        _notify_failed(waiters, None)

    # a full scan is long pure-Python work: heavy (waits while another heavy task runs)
    entry.running[full] = app.tasks.run(work, title=title, kind="schema", owner=session, on_done=done,
                                        on_error=failed, on_cancel=cancelled, heavy=full)


def forget(session: Any) -> None:
    """Drop the cached schemas of ``session`` (cancels running computations)."""
    entry = _cache.pop(session, None)
    if entry is not None:
        for task in entry.running.values():
            task.cancel()
        _fail_all(entry, None)


def store(session: Any, schema: Schema, *, full: bool, gen: int | None = None, doc: Any = None) -> bool:
    """Put a schema computed elsewhere (GUI thread; e.g. the full scan of the JSON Schema
    generate dialog) into the cache of ``session``, as the full schema (``full``: every record
    was analysed -- also when ``schema.complete``, like the cache's own computations) or as the
    sample. ``gen``: the ``session.data_gen`` of the state it was computed from (default: the
    one shown now); ``doc``: that document (the live one or a pinned view of it).

    Nothing is stored -- False is returned -- when the session has no document, ``doc`` is of
    another (replaced) document, ``full`` is asked for a schema that did not analyse every
    record, or ``gen`` is not the data generation the session shows (a schema of older data:
    :func:`is_stale` would report it at once). A stored schema of the shown data replaces the
    cached schemas of other data. Running computations and their waiting callbacks are left
    alone (they still deliver their own result)."""
    entry = _entry(session)
    if entry is None or schema is None:
        return False
    if doc is not None and getattr(doc, "origin", doc) is not entry.doc_ref():
        return False
    complete = bool(getattr(schema, "complete", False))
    if full and not complete:
        return False
    result_gen = _data_gen(session) if gen is None else int(gen)
    if entry.alias is not None and result_gen == entry.alias[0]:
        result_gen = entry.alias[1]
    if result_gen != _data_gen(session):
        return False
    if entry.data_gen != result_gen:   # the cached schemas describe other data
        entry.full = entry.sample = None
    entry.data_gen = result_gen
    if full or complete:
        entry.full = schema
    if not full:
        entry.sample = schema
    return True
