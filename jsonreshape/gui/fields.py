"""Shared structure (schema) of a document for the GUI: one cache per :class:`DocSession`.

The table (default columns, column chooser), the filter builder and the sort dialog (field
lists) and the schema view all need the fields of the records. :func:`get_schema` computes
:func:`jsonreshape.schema.infer_schema` once per opened document in a background task — a
sample of ``Settings.sample_size`` records, or every record when ``full=True`` — and hands
it to every caller; a full scan replaces the sample for everyone once it is done.
"""

from __future__ import annotations

import logging
import weakref
from typing import Any, Callable

from jsonreshape.i18n import t
from jsonreshape.paths import Wild
from jsonreshape.schema import FieldInfo, Schema, infer_schema

log = logging.getLogger(__name__)

SchemaCallback = Callable[[Schema], Any]
#: Called instead of the schema callback when the computation failed (the exception) or was
#: cancelled / superseded by a reopened document (``None``).
FailedCallback = Callable[[BaseException | None], Any]

#: Fields suggested by :func:`default_columns` at most / their maximum depth.
DEFAULT_COLUMN_LIMIT = 50
DEFAULT_COLUMN_DEPTH = 3
CONTAINER_TYPES = ("object", "array")


class _Entry:
    """Cached schemas of one session (valid for one ``Document`` object)."""

    __slots__ = ("doc_ref", "sample", "full", "waiting", "running")

    def __init__(self, doc: Any) -> None:
        self.doc_ref = weakref.ref(doc)  # identity of the Document object (a reopened file is a new one)
        self.sample: Schema | None = None
        self.full: Schema | None = None
        self.waiting: dict[bool, list[tuple[SchemaCallback | None, FailedCallback | None]]] = {False: [], True: []}
        self.running: dict[bool, Any] = {}   # full flag -> GuiTask


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


def _start(session: Any, entry: _Entry, full: bool) -> None:
    doc = session.doc
    app = session.app
    sample = None if full else max(1, int(app.settings.sample_size))
    name = doc.source_path.name
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


# ----------------------------------------------------------------------------------
# field lists
# ----------------------------------------------------------------------------------
def is_container(fi: FieldInfo) -> bool:
    """The field's most frequent type is an object or an array."""
    return fi.main_type in CONTAINER_TYPES


def under_array(fi: FieldInfo) -> bool:
    """The field lies inside an array element (``orders[*].id``)."""
    return any(seg is Wild.ANY_INDEX for seg in fi.path)


def field_paths(schema: Schema | None, *, containers: bool = True, inside_arrays: bool = True,
                key_field: bool = True, max_depth: int | None = None) -> list[str]:
    """Field texts (path syntax of :mod:`jsonreshape.paths`, ``$key`` for member keys) in
    pre-order, filtered: ``containers`` keeps object/array fields, ``inside_arrays`` keeps
    fields under ``[*]``, ``key_field`` keeps ``$key``, ``max_depth`` limits the depth."""
    if schema is None:
        return []
    out: list[str] = []
    for fi in schema.fields():
        if fi.is_key:
            if key_field:
                out.append(fi.text)
            continue
        if max_depth is not None and fi.depth > max_depth:
            continue
        if not containers and is_container(fi):
            continue
        if not inside_arrays and under_array(fi):
            continue
        if any(seg is Wild.ANY_KEY for seg in fi.path):
            continue  # pooled map keys are not addressable fields
        out.append(fi.text)
    return out


def default_columns(schema: Schema | None, *, limit: int = DEFAULT_COLUMN_LIMIT,
                    max_depth: int = DEFAULT_COLUMN_DEPTH) -> list[str]:
    """Table columns suggested for a schema: ``$key`` (member records), then the scalar
    fields outside arrays up to ``max_depth`` and the arrays of the first level (shown as
    ``[n]``), in schema order, at most ``limit``; records that are not objects get the
    single column ``$``."""
    if schema is None:
        return ["$"]
    root = schema.root
    root_is_object = root.main_type == "object"
    cols: list[str] = []
    for fi in schema.fields():
        if len(cols) >= limit:
            break
        if fi.is_key:
            cols.append(fi.text)
            continue
        if not root_is_object or fi.depth > max_depth or under_array(fi):
            continue
        if any(seg is Wild.ANY_KEY for seg in fi.path):
            continue
        if fi.main_type == "object":
            continue  # its scalar children become columns
        if fi.main_type == "array" and fi.depth > 1:
            continue
        cols.append(fi.text)
    if not root_is_object:
        cols.append("$")
    return cols or ["$"]
