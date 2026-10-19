"""The GUI side of editing (docs/ARCHITECTURE.md §12.9): :class:`EditController`, its worker and the ref helpers.

One :class:`EditController` belongs to one opened document of a :class:`~jsonreshape.gui.session.DocSession`
(``session.edit``; created when the index is finished, replaced on every reopen). The GUI thread never calls the core
:class:`~jsonreshape.edits.EditSession` / :class:`~jsonreshape.editstore.EditStore`: a plain :class:`_EditWorker`
(thread + queue + weak reference to the controller, the pattern of ``loader._State``) owns the edit session
(``auto_apply=False``) and runs, in order: opening (preconditions, the layer lock, ``find_pending``), the point
commands, undo/redo, discard, bulk apply and the pending-layer actions. Long operations run as tasks of the
:class:`~jsonreshape.gui.qtasks.TaskManager` (visible, cancellable) whose function calls the edit session in the task
thread: save / Save As (heavy, kind ``save``), bulk preview (heavy, kind ``bulk``), record delete / duplicate of big
selections (kind ``edit``), JSON Patch import (heavy, kind ``patch``) and export (kind ``export``), export of a pending
layer. Every result is handed to the GUI thread (the worker posts through one process-wide queued signal; tasks use
their ``on_done``), where :meth:`DocSession._apply_change` installs the new state in ONE call, then
:attr:`EditController.changed` is emitted.

Rules
=====
* **One command in flight.** A command (or undo, redo, save, discard, bulk apply, patch import, pending action) while
  another is running is refused with a status message (``app.show_status``) -- holding Alt+Down moves exactly as fast
  as steps commit. Bulk previews may run beside commands (Apply then needs ``preview.gen == applied_gen``).
* **Refs** are GUI :class:`~jsonreshape.gui.session.NodeRef` objects in positions of :attr:`EditController.applied_gen`
  (the state the GUI shows); every command carries ``expect_gen=applied_gen`` (a stale ref ->
  ``edits.error.stale_refs``, shown as a status message). :func:`to_edit_ref` / :func:`from_edit_ref` convert single
  documents (GUI ``record=None`` <-> core ``record=0``).
* **The initial state is installed lazily.** When the edit session opens, its state reads exactly like the file the
  document shows, so it is not set on the document: ``applied_gen`` is its gen at once (commands and previews carry
  it), while ``session.data_gen`` / ``doc.gen`` stay 0 until the first ChangeSet installs its state (or a record set of
  the plain file, gen 0, is used as an edit scope: the state is installed then and the session's sets re-tagged).
  Opening a document therefore never invalidates view caches keyed by ``data_gen``; record sets are compared with
  ``doc.gen`` (the core rule), not with ``data_gen``.
* **Availability** (``available`` / ``reason``): ``indexing`` (the worker is still opening), ``partial`` (a JSON
  error), ``locked`` (another tab or process edits the file; retried every :data:`LOCK_RETRY_MS`), ``huge_single``
  (a single document above ``large_node_bytes``), ``recovery_pending`` (a layer of this file version with unsaved
  changes, or an interrupted save, waits for Restore / Discard), ``file_changed`` / ``file_grown`` (the source changed
  on disk), ``saving``, ``applying`` (a long change task), ``closed`` (detached, or the edit session ended after a
  save) and -- an addition -- ``unavailable`` (editing could not be set up, e.g. an unusable layer folder; the error
  was reported). :attr:`EditController.reason_text` is the localized sentence. Saving as another file stays possible
  while the file grew; exporting the changes as a JSON Patch and discarding them stay possible while it changed or
  grew.
* **Pending layers** (``pending_found(list[PendingInfo])``, also :attr:`EditController.pending`): only a layer of this
  file version (``matches``) that is not held elsewhere blocks editing; stale layers of older versions are offered for
  Export / Delete without blocking (decision: an old layer cannot be applied anyway and does not stop a new one,
  except in a ``.jredit`` folder it occupies -- then the first step reports ``editstore.error.stale``); locked ones
  are information only. An interrupted save journal is rolled (:meth:`EditController.roll_pending`, also done
  first by :meth:`~EditController.restore_pending`): forward / completed -> the layer is stale and is deleted, back
  -> it can be restored, conflict -> the message names the files and the journal is kept. A damaged layer offers
  "restore up to the last good step" (``LayerDamagedError.last_good``); Delete uses ``editstore.delete_layer`` when
  the layer cannot be opened.
* **Save sequence** (§12.9): refused while busy; the brace_list and broken-lines questions come first
  (``widgets.ask``, non-blocking); then busy, ``session.pause_change_check()``, a heavy task kind ``save`` kept by
  ``keep_document_for``; success: the core closed the edit session (layer discarded), bookmarks become positions of
  the saved file, in place -> ``session.reopen(discard_edits=True, keep_view=True, format=..., record_path=...)``
  with ``SaveReport.reopen_options``; Save As -> ``session.switch_path(target)`` and the same reopen; failure: the
  change check resumes, busy clears, the error is reported -- ``saver.error.broken_lines`` asks and saves again with
  ``drop_broken=True``; ``saver.error.grown`` / ``fileio.error.read_only`` offer Save As.
* :meth:`EditController.ask_close` (why = ``close`` | ``reopen`` | ``quit``): a non-blocking 3-way question when
  dirty; the buttons adapt (file changed on disk: Save changes as JSON Patch / Discard / Cancel; file grew: Save As /
  Discard / Cancel). "Discard" deletes the layer. Test seams: the module attributes :data:`ask_save_handler`
  ``(parent, name, choices) -> choice`` and :data:`ask_path_handler` ``(parent, purpose, suggested) -> path | None``
  (purpose ``save_as`` | ``patch``). Headless (``app.headless``) without a handler: the layer is kept for recovery
  and ``on_done(True)``. ``on_done`` is called at once when no question is needed, else after the answer and the
  chosen action (False: cancelled or failed).
* :meth:`EditController.detach` (``timeout`` seconds): stop accepting, cancel the controller's tasks, interrupt a
  command that waits for the record-key index (``EditSession.interrupt``), let the running worker command finish and
  close the edit session (a layer with steps is kept for recovery); callbacks waiting for the controller to become
  idle (``when_idle``, the close answers) are called once, seeing ``closed``. The worker thread and the tasks are
  returned by :meth:`EditController.waiters` so the session closes the document only after them.
* Object lifetime: the worker holds the controller only weakly and posts plain objects through a process-wide poster
  ``QObject`` that is never deleted, so no QObject ever loses its last reference in the worker thread; callbacks given
  to commands stay in the GUI thread.

Fix round 3 (review3; documented decisions)
==========================================
* **A result that cannot be handled** (an exception in the GUI-thread apply path, a bug) never leaves the controller
  busy: the job ends (``on_done(None)``), the error is reported and the session shows the edit session's state again
  through a reset ChangeSet (``DocSession._apply_change`` itself falls back to a reset when following a step fails),
  so close / discard / save keep working (CL-5).
* **Expected refusals of user commands** (``EditError`` ``edits.error.*`` -- a key that exists, an impossible
  conversion, invalid JSON, a node gone meanwhile ...) are told on the status bar (or given to the command's
  ``on_error(exc)`` callback, which then replaces the status message), never in an error window; real failures (I/O,
  a damaged layer, ``edits.error.internal``, unexpected exceptions) keep the error window (L8).
* **Closing / reopening / quitting while a long operation runs** (a save or Save As, a patch import, a records task,
  a patch export, an export of a pending layer): :meth:`EditController.ask_close` asks "wait / stop / cancel" (default
  wait) when the document is clean or the operation is a save -- "wait" asks again once the controller is idle (the
  unsaved changes question follows when there are changes then), "stop" cancels the operation first. A dirty document
  with another long operation gets the usual Save / Don't save / Cancel question (the answer waits for the operation).
  :attr:`EditController.needs_close_answer` tells whether ``ask_close`` has something to ask (CL-4). Headless without
  a handler: a running save refuses the close (``gui.edit.save_wait``) as before, anything else closes (the task is
  cancelled, the layer kept).
* **In-place save while tasks read the document** (exports, file tools, searches, statistics: the tasks of
  ``DocSession.keep_document_for``; they would fail with "file changed" once the file is replaced): the save asks
  "wait, then save / stop them and save / cancel" (default wait; headless: wait). Waiting is a small cancellable task
  (kind ``save``, not heavy -- it never holds a heavy-task slot while it waits) that ends when every reader ended; the
  controller is busy (``saving``) meanwhile, then the save starts. The controller's own bulk previews are cancelled
  when a save or a discard starts, and a preview that fails because the layer was closed / discarded counts as
  cancelled (CL-3, CL-7).
* **Save As onto a file another tab shows** (:func:`target_conflict`, also used by the export dialog): refused with
  an error when that tab has unsaved changes, pending changes or a running change / save, or when another program
  holds the file's edit lock; asked (yes / no) when the tab is unchanged -- it is reloaded after the save. A replaced
  Save As target gets no ``.bak`` (decision: the save dialog confirms the overwrite and open tabs are protected; the
  saver is unchanged) (S3). A file whose unsaved changes are kept for recovery (no tab, no lock:
  ``writer.edited_targets``) is refused too (verdict ``pending``; fix round L2).
* **An interrupted save that the saver calls finished** while the file is still the version the layer was made for
  (``PendingInfo.matches`` and the file's identity unchanged since the document was opened): the layer is never
  deleted -- the result becomes ``"kept"`` (reported; the layer stays to be restored and saved again) (S1, GUI half).
* Test seam :data:`ask_choice_handler` ``(parent, purpose, name, choices) -> choice`` answers the questions above
  (purpose ``busy`` | ``readers``; choices ``wait`` | ``stop`` | ``cancel``); :data:`ask_save_handler` keeps answering
  only the unsaved-changes question. :meth:`EditController.when_idle` (``fn()`` once nothing is in flight) and
  :func:`settle_edits` (``root, edit, then``: commit the open inline editors -- ``editors.InlineEditor`` children of
  ``root`` -- and the structure builder's rename, then ``then()`` once the steps were applied; an invalid value
  refuses with a status message; :func:`editors_ready` checks without committing) let the window commit what is
  being typed before it saves, closes or reopens (M3).

API additions to §12.9 (documented decisions)
=============================================
* Every command method takes a keyword-only ``on_done(ChangeSet | None)`` (called once in the GUI thread after the
  change was applied, ``None`` when it was refused, failed or changed nothing) and returns ``True`` when it was
  accepted. ``set_path``, ``paste``, ``insert(..., key=)``, ``delete_records`` / ``duplicate_records`` (tasks),
  ``export_patch(path, *, diff=None)`` (``None`` = ``diff=False`` while the file changed on disk).
* ``bulk_preview(spec, scope, on_done, *, on_failed=None)``: ``on_done(preview)`` only on success; ``on_failed(exc |
  None)`` on failure / cancel. ``bulk_apply(preview, *, on_done=None)``.
* ``reason_text``, ``pending``, ``layer_key``, ``closed``, ``roll_pending(info)``, ``ignore_pending(info)`` (hide a
  stale / damaged / locked info until the next open), ``waiters()``, ``save``/``save_as`` call ``on_done(SaveReport |
  None)``, :data:`EDIT_SELECTION_MAX`.
* ``remap_ref(ref, cs, *, single=False)``: ``single=True`` for single documents (whose core NodeChanges use record
  0 while GUI refs use ``None``); ``DocSession`` passes it. Refs without a complete ``ipath``: array-index
  segments are known by value, object members only by key; when a NodeChange's container may or may not lie on such a
  ref's path (an unknown member index before it) and the ref continues below it with an index, the ref is cut to that
  container (it never points at a shifted element). A reset ChangeSet maps every ref to ``None``.
"""

from __future__ import annotations

import itertools
import logging
import os
import queue
import threading
import time
import weakref
from dataclasses import dataclass, field
from typing import Any, Callable, Iterable, Sequence

import shiboken6
from PySide6.QtCore import QObject, Qt, QTimer, Signal

from jsonreshape.errors import Cancelled, EditError, FileChangedError, JsonReshapeError, UserInputError
from jsonreshape.gui.session import NodeRef, _as_reset, _close_sets, _end_waiter, _same_file, file_users, \
    remap_view_sets
from jsonreshape.i18n import t
from jsonreshape.recordset import RecordSet

log = logging.getLogger(__name__)

__all__ = [
    "EDIT_SELECTION_MAX",
    "LOCK_RETRY_MS",
    "REASONS",
    "EditController",
    "ask_choice_handler",
    "ask_path_handler",
    "ask_save_handler",
    "editors_ready",
    "from_edit_ref",
    "open_inline_editors",
    "reason_text",
    "reload_sessions",
    "remap_ref",
    "settle_edits",
    "target_conflict",
    "to_edit_ref",
    "user_mistake",
]

#: Node commands take at most this many nodes (above: Bulk operations; = ``edits.MAX_ITEMS``).
EDIT_SELECTION_MAX = 10_000
#: While another tab / process holds the layer lock, opening is retried this often (ms).
LOCK_RETRY_MS = 2500
#: ``EditController.reason`` values ("" = available).
REASONS = ("indexing", "partial", "locked", "huge_single", "recovery_pending", "file_changed", "file_grown",
           "saving", "applying", "closed", "unavailable")

#: Test seam / automation: ``fn(parent, name, choices) -> choice`` answers :meth:`EditController.ask_close`
#: (choices: ``save`` | ``save_as`` | ``patch`` | ``discard`` | ``cancel``).
ask_save_handler: Callable[[Any, str, tuple], str] | None = None
#: Test seam: ``fn(parent, purpose, suggested) -> path | None`` replaces the file dialog of ask_close's
#: "Save As…" (purpose ``save_as``) and "Save changes as JSON Patch…" (purpose ``patch``).
ask_path_handler: Callable[[Any, str, str], str | None] | None = None
#: Test seam / automation: ``fn(parent, purpose, name, choices) -> choice`` answers the "a long operation runs"
#: question of ask_close (purpose ``busy``) and the "tasks still read the file" question of an in-place save
#: (purpose ``readers``); choices ``wait`` | ``stop`` | ``cancel`` (module docstring, "Fix round 3").
ask_choice_handler: Callable[[Any, str, str, tuple], str] | None = None

#: ``EditError`` keys of real failures: shown in an error window even when a user command caused them.
_FAILURE_KEYS = frozenset({"edits.error.internal", "edits.error.layer_corrupt", "edits.error.closed",
                           "edits.error.partial", "edits.error.pending", "edits.error.locked"})
#: Errors of a bulk preview whose layer was closed / discarded meanwhile (a save, a discard): the preview is void.
_STORE_GONE_KEYS = frozenset({"editstore.error.closed", "editstore.error.discarded", "edits.error.closed"})


def user_mistake(exc: BaseException) -> bool:
    """``exc`` is an expected refusal of a user command (a key that exists, an impossible conversion, invalid JSON,
    a node that went away ...): told on the status bar, not in an error window (review L8)."""
    if isinstance(exc, (Cancelled, FileChangedError)) or not isinstance(exc, JsonReshapeError):
        return False
    if isinstance(exc, EditError):
        key = str(getattr(exc, "key", "") or "")
        return key.startswith("edits.error.") and key not in _FAILURE_KEYS
    return isinstance(exc, UserInputError)


def _has_unsaved_work(session: Any) -> bool:
    """``session`` holds changes a replaced file would strand: unsaved changes, pending changes of this file version
    waiting for Restore, or a change / save in flight."""
    if bool(getattr(session, "dirty", False)):
        return True
    edit = getattr(session, "edit", None)
    if edit is None:
        return False
    return bool(getattr(edit, "dirty", False) or getattr(edit, "busy", False)
                or _blocking(getattr(edit, "pending", None) or ()))


def target_conflict(path: Any, *, exclude: Any = None) -> tuple[str, list[Any]] | None:
    """Who else uses the file ``path`` that a Save As or an export would replace (review S3), ``exclude`` (the
    document being saved / exported) left out:

    * ``("unsaved", [sessions])`` -- tabs of this process show it with unsaved or pending changes or a running
      change / save (or with an edit layer the core counts as unsaved edits, e.g. changes undone): refuse;
    * ``("locked", [])`` -- no tab here, but its edit lock is held (another window of the program edits it): refuse;
    * ``("pending", [])`` -- no tab here and no lock held, but the file has unsaved changes of this program kept
      for recovery (an edit layer with steps, also one of an older version of the file:
      :func:`jsonreshape.writer.edited_targets`, the rule the writer and the file tools refuse by): replacing
      the file would strand them: refuse (fix round L2);
    * ``("open", [sessions])`` -- tabs show it without changes: ask, and reload them afterwards;
    * ``None`` -- nobody (or ``path`` is ``exclude``'s own file).

    A few stat calls, a lock probe and the edit-layer index (read once; a layer is looked at only when the index
    names the file or a ``<file>.jredit`` folder exists) -- GUI thread, like the export dialog's own target
    checks."""
    target = os.fspath(path)
    if exclude is not None and _same_file(target, getattr(exclude, "path", "") or ""):
        return None
    users = file_users(target, exclude=exclude)
    busy = [s for s in users if _has_unsaved_work(s)]
    if busy:
        return "unsaved", busy
    if not users:
        try:
            from jsonreshape.editstore import LayerLock, file_lock_key
            from jsonreshape.fileio import long_path

            if os.path.exists(long_path(target)):
                key = file_lock_key(target)
                if key is not None and LayerLock.is_locked(key):
                    return "locked", []
        except Exception:  # an unusable lock folder: nothing can be told
            log.debug("probing the edit lock of %s failed", target, exc_info=True)
    try:
        from jsonreshape.writer import edited_targets

        kept = bool(edited_targets([target]))
    except Exception:  # an unusable edit-layer folder: no layer can be found (the writer cannot see one either)
        log.debug("looking for edit layers of %s failed", target, exc_info=True)
        kept = False
    if kept:
        # a tab here shows it: its own layer (e.g. undone changes) -- that tab is the one to save or close
        return ("unsaved", users) if users else ("pending", [])
    if users:
        return "open", users
    return None


def open_inline_editors(root: Any) -> list[Any]:
    """The inline editors (``editors.InlineEditor``) open under the widget ``root`` -- found as children, so no
    view needs a method for it (review M3)."""
    try:
        from PySide6.QtWidgets import QLineEdit

        from jsonreshape.gui.editors import InlineEditor
    except ImportError:  # pragma: no cover - part of the package
        return []
    if root is None or not shiboken6.isValid(root):
        return []
    return [w for w in root.findChildren(QLineEdit)
            if isinstance(w, InlineEditor) and shiboken6.isValid(w) and not w.done]


def settle_edits(root: Any, edit: Any, then: Callable[[], Any], *, views: Iterable[Any] = (),
                 show_status: Callable[[str, int], Any] | None = None) -> Any:
    """Commit what the user is typing under ``root`` (a document tab) before a save / Save As / close / reopen
    (review M3): the structure builder's rename (``finish_rename()`` of ``views``), then every open inline editor
    (``InlineEditor.commit()``); ``then()`` runs once their steps were applied (``edit.when_idle``) -- at once when
    no editor is open (its result is returned then; ``True`` when it runs later). ``False`` -- nothing done, the
    editor stays open, ``show_status(text, ms)`` says why -- while an editor holds an invalid value or checks its
    key, or ``edit`` is still busy with an earlier step."""
    def status(text: str, ms: int) -> None:
        if show_status is not None:
            show_status(text, ms)

    for view in views or ():
        finish = getattr(view, "finish_rename", None)
        if callable(finish):
            try:
                finish()
            except Exception:
                log.exception("committing the structure builder's rename failed")
    editors = open_inline_editors(root)
    if not editors:
        return then()
    for ed in editors:
        if ed.checking:
            status(t("gui.edit.editor.checking"), 6000)
            ed.setFocus()
            return False
        if not ed.is_valid():
            status(t("gui.edit.editor.invalid", reason=ed.error_text()), 8000)
            ed.setFocus()
            return False
    if edit is not None and bool(getattr(edit, "busy", False)):
        status(t("gui.edit.editor.busy"), 6000)
        return False
    for ed in editors:
        if not ed.commit():
            # a new key is checked now: the editor commits by itself when the answer comes
            status(t("gui.edit.editor.checking"), 6000)
            return False
    when_idle = getattr(edit, "when_idle", None) if edit is not None else None
    if not callable(when_idle):
        return then()
    result: list[Any] = []
    when_idle(lambda: result.append(then()))
    return result[0] if result else True


def editors_ready(root: Any, edit: Any, *, show_status: Callable[[str, int], Any] | None = None) -> bool:
    """The open inline editors under ``root`` could be committed now (valid, no key check on its way, ``edit`` not
    busy); else ``show_status`` says why (M3: Save As checks it before it asks for the file name)."""
    editors = open_inline_editors(root)
    for ed in editors:
        if ed.checking:
            if show_status is not None:
                show_status(t("gui.edit.editor.checking"), 6000)
            return False
        if not ed.is_valid():
            if show_status is not None:
                show_status(t("gui.edit.editor.invalid", reason=ed.error_text()), 8000)
            ed.setFocus()
            return False
    if editors and edit is not None and bool(getattr(edit, "busy", False)):
        if show_status is not None:
            show_status(t("gui.edit.editor.busy"), 6000)
        return False
    return True


def reload_sessions(refs: Iterable[Any]) -> None:
    """Open the files of the sessions in ``refs`` (weak references) again after this program replaced them (a Save
    As / an export onto a file another tab showed unchanged; review S3). A session that got changes meanwhile is left
    alone (its own change check reports the new file)."""
    for ref in refs or ():
        other = ref() if callable(ref) else ref
        if other is None or getattr(other, "closed", True):
            continue
        if _has_unsaved_work(other):
            continue
        try:
            other.reopen()
        except Exception:
            log.warning("reloading %s after its file was replaced failed", getattr(other, "name", other),
                        exc_info=True)


# ----------------------------------------------------------------------------------
# refs
# ----------------------------------------------------------------------------------
def _is_single(session: Any) -> bool:
    doc = getattr(session, "doc", None)
    return doc is not None and getattr(doc, "kind", None) == "single"


def _unpack(ref: Any) -> tuple[Any, tuple, tuple | None]:
    if ref is None:
        raise TypeError("a ref is required")
    seq = list(ref)
    if not 1 <= len(seq) <= 3:
        raise TypeError(f"expected a ref (record, path, ipath), got {ref!r}")
    seq += [(), None][len(seq) - 1:]
    record, path, ipath = seq[0], seq[1], seq[2]
    return record, tuple(path or ()), (None if ipath is None else tuple(ipath))


def to_edit_ref(session: Any, ref: Any) -> Any:
    """The core :class:`~jsonreshape.edits.Ref` of a GUI NodeRef (single documents: ``record None`` -> 0; an
    incomplete ``ipath`` is dropped -- the core follows the path by key then)."""
    from jsonreshape.edits import Ref

    record, path, ipath = _unpack(ref)
    if record is None and _is_single(session):
        record = 0
    if ipath is not None and len(ipath) != len(path):
        ipath = None
    return Ref(record, path, ipath)


def from_edit_ref(session: Any, ref: Any) -> NodeRef | None:
    """The GUI NodeRef of a core ref / ``ChangeSet.select`` tuple (single documents: record 0 -> ``None``)."""
    if ref is None:
        return None
    record, path, ipath = _unpack(ref)
    if _is_single(session):
        record = None
    return NodeRef(record, path, ipath)


def remap_ref(ref: Any, cs: Any, *, single: bool = False) -> NodeRef | None:
    """``ref`` (a NodeRef of the state before ``cs``) in the coordinates after it: the record remap, then the
    record's NodeChanges in list order (array-index segments of ``path`` and ``ipath`` rewritten at the container's
    depth); ``None`` when the node was deleted (or ``cs`` is a reset). See the module docstring for refs without a
    complete ``ipath`` and for ``single``."""
    if ref is None:
        return None
    if getattr(cs, "reset", False):
        return None
    record, path_t, ipath_t = _unpack(ref)
    remap = getattr(cs, "remap", None)
    if record is not None and remap is not None:
        try:
            record = remap.map(record)
        except (IndexError, ValueError):
            return None
        if record is None:
            return None
    nc_record = 0 if (single and record is None) else record
    path = list(path_t)
    complete = ipath_t is not None and len(ipath_t) == len(path)
    ipath = list(ipath_t) if ipath_t is not None else None
    for nc in getattr(cs, "nodes", ()) or ():
        if nc.record != nc_record:
            continue
        cip = list(nc.ipath)
        k = len(cip)
        if k >= len(path):
            continue        # the container is the node itself, below it or elsewhere: the node did not move
        if complete:
            assert ipath is not None
            if ipath[:k] != cip:
                continue
            j = nc.remap.map(ipath[k])
            if j is None:
                return None
            ipath[k] = j
            if type(path[k]) is int:
                path[k] = j
            continue
        known = [ipath[i] if ipath is not None and i < len(ipath)
                 else (path[i] if type(path[i]) is int else None) for i in range(len(path))]
        if any(p is not None and p != c for p, c in zip(known[:k], cip)):
            continue        # certainly another container
        level_known = known[k] is not None
        if all(p is not None for p in known[:k]):
            if not level_known:
                continue    # an object member addressed by its key: the key does not move
            j = nc.remap.map(known[k])
            if j is None:
                return None
            if ipath is not None and k < len(ipath):
                ipath[k] = j
            if type(path[k]) is int:
                path[k] = j
            continue
        if level_known:
            # may or may not be this container: never point at a shifted element -> cut to the container
            path = path[:k]
            if ipath is not None:
                ipath = ipath[:k]
    return NodeRef(record, tuple(path), None if ipath is None else tuple(ipath))


def reason_text(reason: str) -> str:
    """The localized sentence of an ``EditController.reason`` ("" for "")."""
    texts: dict[str, Callable[[], str]] = {
        "indexing": lambda: t("gui.edit.reason.indexing"),
        "partial": lambda: t("gui.edit.reason.partial"),
        "locked": lambda: t("gui.edit.reason.locked"),
        "huge_single": lambda: t("gui.edit.reason.huge_single"),
        "recovery_pending": lambda: t("gui.edit.reason.recovery_pending"),
        "file_changed": lambda: t("gui.edit.reason.file_changed"),
        "file_grown": lambda: t("gui.edit.reason.file_grown"),
        "saving": lambda: t("gui.edit.reason.saving"),
        "applying": lambda: t("gui.edit.reason.applying"),
        "closed": lambda: t("gui.edit.reason.closed"),
        "unavailable": lambda: t("gui.edit.reason.unavailable"),
    }
    fn = texts.get(reason)
    return fn() if fn is not None else ""


# ----------------------------------------------------------------------------------
# worker -> GUI thread
# ----------------------------------------------------------------------------------
class _Poster(QObject):
    """Lives in the GUI thread for the whole process (never deleted): workers emit :attr:`posted` (queued)."""

    posted = Signal(object)


_poster: _Poster | None = None


def _get_poster() -> _Poster:
    """The process-wide poster (created by the first controller, in the GUI thread)."""
    global _poster
    poster = _poster
    if poster is None or not shiboken6.isValid(poster):
        poster = _Poster()
        poster.posted.connect(_deliver, Qt.ConnectionType.QueuedConnection)
        _poster = poster
    return poster


def _deliver(item: _Item) -> None:     # GUI thread (queued slot)
    target = item.target
    ctl = target() if target is not None else None
    if ctl is None or not shiboken6.isValid(ctl):
        item.drop()
        return
    try:
        ctl._on_item(item)
    except Exception as exc:
        log.exception("handling an edit result failed")
        try:
            ctl._item_crashed(item, exc)     # the job ends, the views follow the state again (review CL-5)
        except Exception:  # pragma: no cover - last resort
            log.exception("recovering from a failed edit result failed")
            item.drop()


class _Job:
    """A unit of work for the worker (plain data; callbacks stay in the controller)."""

    __slots__ = ("id", "kind", "args", "sets")

    def __init__(self, job_id: int, kind: str, args: Any = None, sets: Sequence[RecordSet] = ()) -> None:
        self.id = job_id
        self.kind = kind
        self.args = args
        self.sets = list(sets)


class _Item:
    """A worker result posted to the GUI thread."""

    __slots__ = ("target", "job", "kind", "cs", "remapped", "status", "error", "data")

    def __init__(self, job: _Job | None, kind: str, *, cs: Any = None, remapped: dict | None = None,
                 status: dict | None = None, error: BaseException | None = None, **data: Any) -> None:
        self.target: Any = None
        self.job = job
        self.kind = kind
        self.cs = cs
        self.remapped = remapped
        self.status = status
        self.error = error
        self.data = data

    def drop(self) -> None:
        """Nobody takes the result: free the remapped record sets."""
        remapped, self.remapped = self.remapped, None
        _close_sets(remapped)


@dataclass(eq=False)
class _SaveRequest:
    """One Save / Save As on its way through the questions to the save task (GUI thread)."""

    target: str | None                  # None: in place
    on_done: Any                        # on_done(SaveReport | None)
    reopen: bool
    parent: Any
    drop_broken: bool = False
    reload: list = field(default_factory=list)   # weak refs: tabs showing the Save As target (reloaded after it)


def _status_of(es: Any) -> dict[str, Any]:
    """The mirrored properties of an edit session (any thread)."""
    if es is None or getattr(es, "closed", True):
        return {"dirty": False, "can_undo": False, "can_redo": False, "undo_text": "", "redo_text": "", "steps": 0}
    try:
        return {"dirty": bool(es.dirty), "can_undo": bool(es.can_undo), "can_redo": bool(es.can_redo),
                "undo_text": es.undo_label, "redo_text": es.redo_label, "steps": int(es.state.version)}
    except Exception:  # pragma: no cover - a store closed meanwhile
        log.debug("reading the edit session state failed", exc_info=True)
        return {"dirty": bool(getattr(es, "dirty", False)), "can_undo": False, "can_redo": False, "undo_text": "",
                "redo_text": "", "steps": 0}


def _file_is_old_version(journal: Any) -> bool:
    """The file at the journal's source is still the version before the interrupted save: its size, modification
    time and content hashes are the journal's ``old_identity`` (the check ``saver.roll_journal_at`` makes; review
    S1). ``True`` when it cannot be told (the layer is kept then)."""
    try:
        from jsonreshape import fileio
        from jsonreshape.fileio import FileIdentity

        old = FileIdentity.from_dict(journal["old_identity"])
        now = fileio.file_identity(journal["source"])
    except (KeyError, TypeError, ValueError, OSError, JsonReshapeError):
        return True
    return now.size == old.size and now.mtime_ns == old.mtime_ns and now.hashes == old.hashes


def _blocking(infos: Iterable[Any]) -> list[Any]:
    """The pending layers that must be answered before editing (this file version, not held elsewhere)."""
    return [p for p in infos or () if getattr(p, "matches", False) and not getattr(p, "locked", False)]


class _EditWorker:
    """The edit worker: a thread, a FIFO of jobs and a weak reference to the controller (``loader._State`` pattern).
    It owns the :class:`~jsonreshape.edits.EditSession` and, until a session takes it, the layer lock."""

    kind = "edit"     # read by DocSession._close_after (a "task" that must end before the document closes)

    def __init__(self, controller: EditController, doc: Any, settings: Any) -> None:
        self.target = weakref.ref(controller)
        self.doc = doc
        self.settings = settings
        self.session: Any = None          # EditSession
        self.lock: Any = None             # LayerLock (held by the worker or, after open, owned by the session)
        self.warned = 0                   # EditSession.warnings already shown
        self.queue: queue.SimpleQueue = queue.SimpleQueue()
        self.poster = _get_poster()       # created in the GUI thread (this constructor runs there)
        self.thread = threading.Thread(target=self._run, name=f"jsonreshape-edit-{doc.source_path.name}",
                                       daemon=True)

    # -- GUI thread --------------------------------------------------------------------
    def start(self) -> None:
        self.thread.start()

    def submit(self, job: _Job) -> None:
        self.queue.put(job)

    def close(self) -> None:
        # a command waiting for the record-key index (a member-record document) stops now instead of keeping the
        # worker -- and the document -- busy until the index is built (EditSession.interrupt: thread-safe, never
        # blocks; review CL-12)
        es = self.session
        if es is not None:
            try:
                es.interrupt()
            except Exception:  # pragma: no cover - defensive
                log.debug("interrupting the edit session failed", exc_info=True)
        self.queue.put(None)

    @property
    def thread_alive(self) -> bool:
        return self.thread.is_alive()

    def join(self, timeout: float | None = None) -> bool:
        if self.thread is threading.current_thread():
            return False
        self.thread.join(timeout)
        return not self.thread.is_alive()

    # -- worker thread -----------------------------------------------------------------
    def _post(self, item: _Item) -> None:
        item.target = self.target
        try:
            self.poster.posted.emit(item)
        except RuntimeError:  # pragma: no cover - the application is being torn down
            item.drop()

    def _run(self) -> None:
        try:
            while True:
                job = self.queue.get()
                if job is None:
                    return
                try:
                    item = self._execute(job)
                except BaseException as exc:  # noqa: BLE001 - handed to the GUI thread
                    item = _Item(job, "error", error=exc, status=_status_of(self.session))
                if item is not None:
                    item.data.setdefault("warnings", self._new_warnings())
                    self._post(item)
                item = None
                job = None
        finally:
            self._shutdown()

    def _shutdown(self) -> None:
        session, self.session = self.session, None
        lock, self.lock = self.lock, None
        try:
            if session is not None:
                session.close()      # keeps a layer with steps for recovery; releases the lock
            elif lock is not None:
                lock.release()
        except Exception:
            log.exception("closing the edit session failed")
        self.doc = None

    def _new_warnings(self) -> list[str]:
        """Texts of store notices (beside-folder fallback) not shown yet."""
        out: list[str] = []
        try:
            from jsonreshape.editstore import notice_texts

            out.extend(notice_texts())
        except Exception:  # pragma: no cover - defensive
            log.debug("edit notices unavailable", exc_info=True)
        es = self.session
        warnings = list(getattr(es, "warnings", None) or []) if es is not None else []
        if len(warnings) > self.warned:
            for key, params in warnings[self.warned:]:
                text = t(key, **dict(params or {}))
                if text not in out:
                    out.append(text)
            self.warned = len(warnings)
        return out

    def _held_lock(self) -> Any:
        """The document's layer lock while this process holds it (the worker keeps the LayerLock object it gave
        the edit session; the session releases it when it closes or saves)."""
        lock = self.lock
        return lock if lock is not None and lock.held else None

    def _execute(self, job: _Job) -> _Item | None:
        kind = job.kind
        if kind in ("open", "retry"):
            return self._open(job, retry=kind == "retry")
        if kind == "command":
            return self._command(job)
        if kind == "undo":
            return self._change(job, self._require().undo())
        if kind == "redo":
            return self._change(job, self._require().redo())
        if kind == "discard":
            return self._change(job, self._require().discard())
        if kind == "bulk_apply":
            return self._change(job, self._require().bulk_apply(job.args))
        if kind == "restore":
            return self._restore(job)
        if kind == "discard_pending":
            return self._discard_pending(job)
        if kind == "roll":
            return self._roll(job)
        raise ValueError(f"unknown edit job {kind!r}")   # pragma: no cover - programming error

    def _require(self) -> Any:
        es = self.session
        if es is None or es.closed:
            raise EditError("edits.error.closed")
        return es

    def _change(self, job: _Job, cs: Any) -> _Item:
        es = self.session
        if cs is None:
            return _Item(job, "noop", status=_status_of(es))
        remapped = None
        if cs.remap is not None and job.sets:
            try:
                remapped = remap_view_sets(job.sets, cs.remap, cs.gen)
            except Exception:  # never lose a committed step because a view could not be remapped
                log.warning("remapping the view after an edit failed", exc_info=True)
                remapped = None
        return _Item(job, "change", cs=cs, remapped=remapped, status=_status_of(es))

    def _command(self, job: _Job) -> _Item:
        name, args, kwargs, gen = job.args
        es = self._require()
        cs = getattr(es, name)(*args, expect_gen=gen, **kwargs)
        return self._change(job, cs)

    # -- opening -----------------------------------------------------------------------------
    def _open(self, job: _Job, *, retry: bool) -> _Item:
        from jsonreshape.editstore import LayerLock, find_pending

        doc = self.doc
        if doc is None or doc.closed:
            return _Item(job, "failed", error=EditError("edits.error.closed"))
        if self.session is not None and not self.session.closed:
            return _Item(job, "opened", state=self.session.state, layer_key=self._held_key(),
                         pending=None, status=_status_of(self.session))
        if doc.error is not None:
            return _Item(job, "partial")
        try:
            if doc.kind == "single" and doc.record_count == 1 and doc.is_huge(0):
                return _Item(job, "huge_single")
            lock = self.lock
            if lock is None or not lock.held:
                lock = LayerLock.for_doc(doc)
                if not lock.try_acquire():
                    infos = [] if retry else find_pending(doc, settings=self.settings)
                    return _Item(job, "locked", pending=infos)
                self.lock = lock
            infos = find_pending(doc, settings=self.settings, lock=lock)
            if _blocking(infos):
                return _Item(job, "pending", pending=infos, layer_key=lock.key)
            return self._open_session(job, restore=False, pending=infos)
        except FileChangedError as exc:
            return _Item(job, "file_changed", error=exc)
        except EditError as exc:
            if exc.key == "edits.error.partial":
                return _Item(job, "partial")
            if exc.key == "edits.error.locked":
                return _Item(job, "locked", pending=[])
            return _Item(job, "failed", error=exc)

    def _held_key(self) -> str | None:
        lock = self._held_lock()
        return getattr(lock, "key", None) if lock is not None else None

    def _open_session(self, job: _Job, *, restore: bool, pending: list | None, upto: int | None = None) -> _Item:
        from jsonreshape.edits import EditSession

        lock = self.lock
        es = EditSession.open(self.doc, settings=self.settings, lock=lock, restore=restore, auto_apply=False,
                              upto=upto)
        self.session = es
        self.warned = 0
        return _Item(job, "opened", state=es.state, layer_key=getattr(lock, "key", None), pending=pending,
                     status=_status_of(es))

    def _restore(self, job: _Job) -> _Item:
        from jsonreshape.editstore import LayerDamagedError
        from jsonreshape.overlay import ChangeSet

        info, upto, roll_first = job.args
        if self.session is not None and not self.session.closed:
            raise EditError("edits.error.pending")      # pragma: no cover - the controller prevents it
        roll_text = None
        if roll_first and getattr(info, "saving", None):
            result = self._roll_journal(info)
            roll_text = str(result)
            if result.action not in ("back", "invalid"):
                from jsonreshape.editstore import find_pending

                infos = find_pending(self.doc, settings=self.settings, lock=self._held_lock())
                return _Item(job, "pending_done", pending=infos, roll_text=roll_text, roll_action=result.action)
        try:
            item = self._open_session(job, restore=True, pending=None, upto=upto)
        except LayerDamagedError as exc:
            return _Item(job, "damaged", error=exc, info=info, roll_text=roll_text)
        es = self.session
        cs = ChangeSet(es.state, "restore", "", None, None, reset=True)
        return _Item(job, "restored", cs=cs, status=item.status, layer_key=item.data.get("layer_key"), info=info,
                     roll_text=roll_text)

    def _store_for(self, info: Any, *, writable: bool) -> Any:
        from jsonreshape.editstore import EditStore

        lock = self._held_lock()
        if writable and info.matches:
            return EditStore.open(self.doc, settings=self.settings, dir=info.dir, lock=lock)
        return EditStore.open(self.doc, settings=self.settings, dir=info.dir, lock=lock, read_only=True,
                              allow_stale=True)

    def _delete_layer(self, info: Any) -> None:
        from jsonreshape.editstore import LayerDamagedError, delete_layer

        if not getattr(info, "damaged", None):
            try:
                store = self._store_for(info, writable=True)
            except LayerDamagedError:
                store = None
            except EditError as exc:
                if exc.key not in ("editstore.error.damaged", "editstore.error.stale", "edits.error.layer_corrupt"):
                    raise
                store = None
            if store is not None:
                store.discard()
                return
        delete_layer(info.dir, key=info.key, lock=self._held_lock())

    def _after_pending_action(self, job: _Job, **data: Any) -> _Item:
        from jsonreshape.editstore import find_pending

        lock = self._held_lock()
        infos = find_pending(self.doc, settings=self.settings, lock=lock)
        if self.session is None and lock is not None and not _blocking(infos):
            item = self._open_session(job, restore=False, pending=infos)
            item.data.update(data)
            return item
        return _Item(job, "pending_done", pending=infos, status=_status_of(self.session), **data)

    def _discard_pending(self, job: _Job) -> _Item:
        (info,) = job.args
        self._delete_layer(info)
        return self._after_pending_action(job, discarded=info)

    def _roll_journal(self, info: Any) -> Any:
        from jsonreshape import saver

        if not getattr(info, "matches", False) and callable(getattr(saver, "roll_journal_at", None)):
            # a layer of another version / format of the file: this controller could only open it read-only (its
            # journal would stay and the bar come back), so it is rolled under its own lock -- which also keeps a
            # layer the file still matches (saver.roll_journal_at)
            return saver.roll_journal_at(info, settings=self.settings)
        store = self._store_for(info, writable=True)
        if store is None:
            raise UserInputError("gui.edit.pending.gone")
        try:
            result = saver.roll_journal(info.saving, store=store)
        except BaseException:
            store.close()
            raise
        if result.action in ("forward", "completed") and _file_is_old_version(info.saving):
            # the file is still the version the layer was made for (its size, modification time and hashes are
            # the journal's old_identity): the replace never happened, whatever the roll concluded -- the layer may
            # hold the only copy of the changes and is never deleted (review S1). The journal is gone (the roll
            # cleared it): the layer is offered for Restore again.
            store.close()
            source = (info.saving or {}).get("source") or getattr(self.doc, "source_path", "?")
            log.error("interrupted save of %s: the roll said %r, but the file is unchanged; the layer is kept",
                      source, result.action)
            return saver.RollResult(t("gui.edit.pending.roll_kept", path=os.path.normpath(str(source))), "kept")
        if result.action in ("forward", "completed"):
            store.discard()            # the save went through: the layer's changes are in the file
        else:
            store.close()
        return result

    def _roll(self, job: _Job) -> _Item:
        (info,) = job.args
        result = self._roll_journal(info)
        # roll_pending's on_done gets the RollResult (its text and action: the tab's conflict bar shows the text)
        return self._after_pending_action(job, roll_text=str(result), roll_action=result.action, roll_result=result)


# ----------------------------------------------------------------------------------
# the controller
# ----------------------------------------------------------------------------------
class EditController(QObject):
    """Editing of one opened document (see the module docstring). GUI thread only."""

    changed = Signal(object)          # ChangeSet, emitted AFTER the session applied it
    state_changed = Signal()          # a mirrored property below changed
    pending_found = Signal(object)    # list[PendingInfo] (recovery bar); [] when nothing is pending

    def __init__(self, session: Any, parent: QObject | None = None) -> None:
        super().__init__(parent)
        doc = session.doc
        if doc is None:
            raise ValueError("the session has no document")
        self._session_ref = weakref.ref(session)
        self.app = session.app
        self.doc = doc
        self._closed = False
        self._open_state = "opening"     # opening | open | pending | locked | partial | huge_single | file_changed
        #                                  | failed | saved
        self._file_state = getattr(session, "file_state", None)
        self._long: str | None = None    # saving | applying | patch | export
        self._status = _status_of(None)
        self._applied_gen = int(getattr(session, "data_gen", 0) or 0)
        self._pending: list[Any] = []
        self._ignored: set[str] = set()
        self.layer_key: str | None = None
        self._ids = itertools.count(1)
        # job id -> (kind, on_done, lease, on_error)
        self._jobs: dict[int, tuple[str, Callable[[Any], Any] | None, Any, Callable[[Any], Any] | None]] = {}
        self._busy_job: int | None = None
        self._busy_task: Any = None
        self._idle_waiters: list[Callable[[], Any]] = []
        self._saving = False
        self._lazy_state: Any = None     # the edit session's initial state, not installed on the document yet
        settings = getattr(self.app, "settings", None) or getattr(doc, "settings", None)
        self._worker = _EditWorker(self, doc, settings)
        self._lock_timer = QTimer(self)
        self._lock_timer.setSingleShot(True)
        self._lock_timer.setInterval(LOCK_RETRY_MS)
        self._lock_timer.timeout.connect(self._retry_lock)
        session.file_changed.connect(self._on_file_changed)
        self._worker.start()
        self._submit(_Job(next(self._ids), "open"), None, None, busy=False)

    def __repr__(self) -> str:
        return f"<EditController {self.doc.source_path.name!r} {self.reason or 'available'}>"

    # ================================================================================
    # mirrored state
    # ================================================================================
    @property
    def closed(self) -> bool:
        return self._closed

    @property
    def reason(self) -> str:
        """Why editing is not possible now ("" = available); one of :data:`REASONS`."""
        if self._closed or self._open_state == "saved":
            return "closed"
        st = self._open_state
        if st in ("partial", "huge_single", "locked"):
            return st
        if st == "failed":
            return "unavailable"
        if st == "file_changed" or self._file_state == "changed":
            return "file_changed"
        if self._file_state == "grown":
            return "file_grown"
        if st == "pending":
            return "recovery_pending"
        if st == "opening":
            return "indexing"
        if self._long == "saving":
            return "saving"
        if self._long == "applying":
            return "applying"
        return ""

    @property
    def reason_text(self) -> str:
        return reason_text(self.reason)

    @property
    def available(self) -> bool:
        return self.reason == ""

    @property
    def busy(self) -> bool:
        """A command, save, patch or pending action is in flight."""
        return self._busy_job is not None or self._busy_task is not None

    @property
    def needs_close_answer(self) -> bool:
        """:meth:`ask_close` has something to ask: unsaved changes, or a long operation (save / Save As, patch
        import or export, records task, export of pending changes) whose result closing would drop (review CL-4)."""
        return not self._closed and (self.dirty or self._busy_task is not None)

    @property
    def dirty(self) -> bool:
        return bool(self._status.get("dirty"))

    @property
    def can_undo(self) -> bool:
        return bool(self._status.get("can_undo")) and self.available

    @property
    def can_redo(self) -> bool:
        return bool(self._status.get("can_redo")) and self.available

    @property
    def undo_text(self) -> str:
        """Label of the step Undo would undo ("" when there is none)."""
        return str(self._status.get("undo_text") or "") if self.can_undo else ""

    @property
    def redo_text(self) -> str:
        return str(self._status.get("redo_text") or "") if self.can_redo else ""

    @property
    def steps(self) -> int:
        """Steps in effect (the layer's head): the "N changes" of the status bar."""
        return int(self._status.get("steps") or 0)

    @property
    def applied_gen(self) -> int:
        """Gen of the overlay state the GUI has installed (refs of commands are positions of it)."""
        return self._applied_gen

    @property
    def pending(self) -> list[Any]:
        """The pending layers found for the document (the recovery bar), ignored ones left out."""
        return [p for p in self._pending if p.key not in self._ignored]

    def _emit_state(self) -> None:
        if not shiboken6.isValid(self):
            return
        self.state_changed.emit()

    def _session(self) -> Any:
        return self._session_ref()

    def waiters(self) -> list[Any]:
        """Task-like objects (``thread_alive``, ``join``, ``kind``) whose threads must end before the document is
        closed: the worker and the controller's running tasks."""
        out: list[Any] = [task for task in self.app.tasks.tasks(owner=self, include_silent=True)]
        out.append(self._worker)
        return out

    # ================================================================================
    # job plumbing
    # ================================================================================
    def _submit(self, job: _Job, on_done: Callable[[Any], Any] | None, lease: Any, *, busy: bool = True,
                on_error: Callable[[Any], Any] | None = None) -> None:
        self._jobs[job.id] = (job.kind, on_done, lease, on_error)
        if busy:
            self._busy_job = job.id
        self._worker.submit(job)
        self._emit_state()

    def _job_entry(self, job: _Job | None) -> tuple[str, Any, Any, Any]:
        """``(kind, on_done, lease, on_error)`` of a job still in flight (``("", None, None, None)`` else)."""
        if job is None:
            return "", None, None, None
        return self._jobs.get(job.id, ("", None, None, None))

    def _finish_job(self, job: _Job | None, result: Any) -> None:
        if job is None:
            return
        _kind, on_done, lease, _on_error = self._jobs.pop(job.id, (None, None, None, None))
        if self._busy_job == job.id:
            self._busy_job = None
        if lease is not None:
            lease.release()
        if on_done is not None:
            _call(on_done, result)
        self._emit_state()
        self._run_idle_waiters()

    def _when_idle(self, fn: Callable[[], Any]) -> None:
        if not self.busy:
            fn()
        else:
            self._idle_waiters.append(fn)

    def when_idle(self, fn: Callable[[], Any]) -> None:
        """Call ``fn()`` (GUI thread) once no command, save or long task is in flight -- at once when idle. After
        :meth:`detach` it is called once all the same (it sees ``closed``). The window commits an open inline
        editor and saves / closes after its step was applied (review M3)."""
        if self._closed:
            _call(fn)
            return
        self._when_idle(fn)

    def _run_idle_waiters(self) -> None:
        while self._idle_waiters and not self.busy and not self._closed:
            fn = self._idle_waiters.pop(0)
            _call(fn)

    def _status_message(self, text: str, timeout: int = 6000) -> None:
        show = getattr(self.app, "show_status", None)
        if callable(show):
            show(text, timeout)

    def _check(self, *, allowed: tuple[str, ...] = ("",), need_session: bool = True) -> bool:
        """May an action run now? Otherwise a status message says why."""
        if self._closed:
            self._status_message(reason_text("closed"))
            return False
        reason = self.reason
        if reason not in allowed:
            self._status_message(t("gui.edit.unavailable", reason=reason_text(reason)))
            return False
        if self.busy:
            self._status_message(t("gui.edit.busy"))
            return False
        if need_session and self._worker.session is None:
            self._status_message(t("gui.edit.unavailable", reason=reason_text("indexing")))
            return False
        return True

    def _refs(self, refs: Any) -> list[Any]:
        session = self._session()
        if refs is None:
            return []
        if isinstance(refs, NodeRef) or (isinstance(refs, tuple) and 1 <= len(refs) <= 3 and (
                refs[0] is None or (isinstance(refs[0], int) and not isinstance(refs[0], bool)))):
            refs = [refs]
        return [to_edit_ref(session, r) for r in refs]

    def _command(self, name: str, args: tuple, kwargs: dict | None = None, *, on_done: Any = None,
                 count: int = 1, on_error: Any = None) -> bool:
        if not self._check():
            _call(on_done, None)
            return False
        if count > EDIT_SELECTION_MAX:
            self._status_message(t("gui.edit.too_many", count=count, max=EDIT_SELECTION_MAX), 10000)
            _call(on_done, None)
            return False
        session = self._session()
        sets, lease = session._edit_sets()
        job = _Job(next(self._ids), "command", (name, args, dict(kwargs or {}), self._applied_gen), sets)
        self._submit(job, on_done, lease, on_error=on_error)
        return True

    # ================================================================================
    # worker results (GUI thread)
    # ================================================================================
    def _on_item(self, item: _Item) -> None:
        session = self._session()
        if self._closed or session is None or session.closed:
            item.drop()
            self._finish_job(item.job, None)
            return
        warnings = item.data.get("warnings") or []
        if warnings:
            session._add_edit_warnings(warnings)
        if item.status is not None:
            self._status = item.status
        kind = item.kind
        handler = getattr(self, f"_item_{kind}", None)
        if handler is None:  # pragma: no cover - programming error
            log.error("unknown edit result %r", kind)
            item.drop()
            self._finish_job(item.job, None)
            return
        handler(item, session)

    def _set_pending(self, infos: Any) -> None:
        if infos is None:
            return
        self._pending = list(infos)
        self.pending_found.emit(self.pending)

    def _item_opened(self, item: _Item, session: Any) -> None:
        from jsonreshape.reader import _edited_state

        state = item.data["state"]
        self._open_state = "open"
        self._lock_timer.stop()
        self.layer_key = item.data.get("layer_key") or self.layer_key
        discarded = item.data.get("discarded")
        if discarded is not None and getattr(discarded, "matches", False):
            session._drop_layer_bookmarks(discarded.key)
        self._applied_gen = int(state.gen)
        if state.version > 0 or _edited_state(state):
            self._lazy_state = None
            if not session._install_state(state):
                self._open_state = "failed"
        else:
            # the initial state reads exactly like the file the document shows: it is installed with the first
            # change (or when a record set of the plain file becomes an edit scope), so opening a document never
            # changes data_gen (caches and record sets made so far stay valid)
            self._lazy_state = state
        if item.data.get("pending") is not None:
            self._set_pending(item.data["pending"])
        self._after_pending_texts(item)
        self._finish_job(item.job, item.data.get("roll_result", state))

    def _ensure_state(self) -> bool:
        """Install the edit session's (unedited) state on the document now (see :meth:`_item_opened`)."""
        state, self._lazy_state = self._lazy_state, None
        session = self._session()
        if state is None or session is None or state.gen != self._applied_gen:
            return state is None
        return bool(session._install_state(state))

    def _item_pending(self, item: _Item, session: Any) -> None:
        self._open_state = "pending"
        self.layer_key = item.data.get("layer_key") or self.layer_key
        self._set_pending(item.data.get("pending") or [])
        self._finish_job(item.job, None)

    def _item_locked(self, item: _Item, session: Any) -> None:
        self._open_state = "locked"
        infos = item.data.get("pending")
        if infos:
            self._set_pending(infos)
        if not self._closed:
            self._lock_timer.start()
        self._finish_job(item.job, None)

    def _item_partial(self, item: _Item, session: Any) -> None:
        self._open_state = "partial"
        self._finish_job(item.job, None)

    def _item_huge_single(self, item: _Item, session: Any) -> None:
        self._open_state = "huge_single"
        self._finish_job(item.job, None)

    def _item_file_changed(self, item: _Item, session: Any) -> None:
        self._open_state = "file_changed"
        self._finish_job(item.job, None)

    def _item_failed(self, item: _Item, session: Any) -> None:
        self._open_state = "failed"
        if item.error is not None:
            self.app.report_error(item.error)
        self._finish_job(item.job, None)

    def _item_noop(self, item: _Item, session: Any) -> None:
        kind = self._job_entry(item.job)[0]
        if kind == "undo":
            self._status_message(t("gui.edit.nothing_undo"))
        elif kind == "redo":
            self._status_message(t("gui.edit.nothing_redo"))
        self._finish_job(item.job, None)

    def _item_change(self, item: _Item, session: Any) -> None:
        cs = item.cs
        remapped, item.remapped = item.remapped, None
        try:
            applied = self._apply(cs, remapped, session)
        except Exception:
            item.remapped = remapped          # _item_crashed closes the copies the session did not take
            raise
        self._finish_job(item.job, cs if applied else None)

    def _apply(self, cs: Any, remapped: dict | None, session: Any) -> bool:
        """Install ``cs`` through the session (one GUI-thread call), then emit :attr:`changed`."""
        if cs.gen == self._applied_gen and not cs.reset:
            _close_sets(remapped)       # nothing changed (a command that was a no-op)
            return False
        self._lazy_state = None         # the ChangeSet installs its own state
        previous = self._applied_gen
        self._applied_gen = int(cs.gen)
        if not session._apply_change(cs, remapped):
            self._applied_gen = previous
            self._open_state = "failed"
            return False
        self.changed.emit(cs)
        return True

    def _item_restored(self, item: _Item, session: Any) -> None:
        self._open_state = "open"
        self.layer_key = item.data.get("layer_key") or self.layer_key
        info = item.data.get("info")
        if info is not None:
            self._pending = [p for p in self._pending if p.key != info.key]
            self.pending_found.emit(self.pending)
        self._after_pending_texts(item)
        cs = item.cs
        applied = self._apply(cs, None, session)
        if applied:
            self._status_message(t("gui.edit.restored", steps=self.steps), 8000)
        self._finish_job(item.job, cs if applied else None)

    def _item_damaged(self, item: _Item, session: Any) -> None:
        exc = item.error
        info = item.data.get("info")
        last_good = getattr(exc, "last_good", None)
        self._after_pending_texts(item)
        self._finish_job(item.job, None)
        if info is None or not last_good:
            self.app.report_error(exc)
            return
        text = t("gui.edit.ask.damaged", seq=getattr(exc, "seq", "?"), last_good=last_good)

        def answer(yes: bool) -> None:
            if yes and not self._closed:
                self._restore(info, upto=int(last_good), roll_first=False)

        _ask(None, text, answer, title=t("gui.edit.ask.title.damaged"))

    def _item_pending_done(self, item: _Item, session: Any) -> None:
        discarded = item.data.get("discarded")
        if discarded is not None and getattr(discarded, "matches", False):
            session._drop_layer_bookmarks(discarded.key)
        self._set_pending(item.data.get("pending") or [])
        self._after_pending_texts(item)
        self._finish_job(item.job, item.data.get("roll_result"))

    def _after_pending_texts(self, item: _Item) -> None:
        text = item.data.get("roll_text")
        if not text:
            return
        action = item.data.get("roll_action")
        if action == "conflict":
            self.app.report_error(UserInputError("gui.edit.pending.roll_conflict", message=str(text)))
        elif action == "kept":
            self.app.report_error(UserInputError("gui.edit.pending.roll_unconfirmed", message=str(text)))
        else:
            self._status_message(str(text), 15000)

    def _item_error(self, item: _Item, session: Any) -> None:
        exc = item.error
        job = item.job
        kind, _on_done, _lease, on_error = self._job_entry(job)
        if kind in ("open", "retry"):
            self._open_state = "failed"
        if isinstance(exc, EditError) and exc.key == "edits.error.stale_refs":
            self._status_message(t("gui.edit.stale"), 8000)
        elif isinstance(exc, EditError) and exc.key == "edits.error.closed" and self._open_state == "saved":
            self._status_message(reason_text("closed"))
        elif isinstance(exc, FileChangedError):
            self._file_state = "changed"
            self.app.report_error(exc)
        elif kind == "command" and exc is not None and user_mistake(exc):
            self._refused(exc, on_error)
        elif not isinstance(exc, Cancelled):
            self.app.report_error(exc)
        self._finish_job(job, None)

    def _refused(self, exc: BaseException, on_error: Any = None) -> None:
        """An expected refusal of a user command: the command's ``on_error`` gets it, else the status bar says why
        (never an error window: review L8)."""
        if on_error is not None:
            _call(on_error, exc)
            return
        message = getattr(exc, "message", None) or str(exc)
        self._status_message(t("gui.edit.refused", reason=message), 10000)

    def _item_crashed(self, item: _Item, exc: BaseException) -> None:
        """Handling a worker result raised (a bug, ``_deliver``): the job still ends and the session shows the edit
        session's state again, so the controller is never left busy and close / discard / save keep working
        (review CL-5)."""
        remapped, item.remapped = item.remapped, None
        if self._closed:
            _close_sets(remapped)
            self._finish_job(item.job, None)
            return
        cs = item.cs
        if cs is not None:
            self._recover(cs, exc, remapped)
        else:
            _close_sets(remapped)
            if self._open_state == "opening":
                self._open_state = "failed"
            self._report(exc)
        job = item.job
        if job is not None and job.id in self._jobs:
            self._finish_job(job, None)
        elif job is not None and self._busy_job == job.id:
            self._busy_job = None
            self._run_idle_waiters()
        self._emit_state()

    def _recover(self, cs: Any, exc: BaseException, remapped: dict | None = None) -> None:
        """Applying ``cs`` (a step the edit session has made) raised: install its state again as a reset (every
        view reloads, nothing is remapped) and report the error; editing stops only when even that fails.
        ``remapped``: the worker's copies of the view sets -- those the session did not take are closed."""
        self._report(exc)
        session = self._session()
        if session is None or session.closed or self._closed:
            _close_sets(remapped)
            return
        try:
            session._close_unused(dict(remapped or {}))
        except Exception:  # pragma: no cover - defensive
            log.debug("closing the unused remapped sets failed", exc_info=True)
        reset = _as_reset(cs)
        self._lazy_state = None
        try:
            ok = bool(session._apply_change(reset, None))
        except Exception:
            log.exception("resetting the views of %s after a failed edit result failed", self._name())
            ok = False
        if ok:
            self._applied_gen = int(cs.gen)
            self.changed.emit(reset)
        else:
            self._open_state = "failed"

    def _report(self, exc: BaseException) -> None:
        try:
            self.app.report_error(exc)
        except Exception:  # pragma: no cover - defensive
            log.exception("reporting an edit error failed")

    # ================================================================================
    # file state
    # ================================================================================
    def _on_file_changed(self, kind: str) -> None:
        if kind in ("changed", "grown") and self._file_state != "changed":
            self._file_state = kind
            self._emit_state()

    def _retry_lock(self) -> None:
        if self._closed or self._open_state != "locked" or self.busy:
            return
        self._submit(_Job(next(self._ids), "retry"), None, None, busy=False)

    # ================================================================================
    # commands (refs: GUI NodeRefs in positions of applied_gen)
    # ================================================================================
    # every command: ``on_done(ChangeSet | None)``; ``on_error(exc)``: an expected refusal (``user_mistake``, e.g. a
    # key taken between an editor's check and the commit) goes to it instead of the status bar (review L8)
    def set_value(self, ref: Any, json_text: str, *, on_done: Any = None, on_error: Any = None) -> bool:
        return self._command("set_value", (to_edit_ref(self._session(), ref), json_text), on_done=on_done,
                             on_error=on_error)

    def set_path(self, ref: Any, json_text: str, *, on_done: Any = None, on_error: Any = None) -> bool:
        """Like :meth:`set_value`, creating missing object members (table cells of missing fields)."""
        return self._command("set_path", (to_edit_ref(self._session(), ref), json_text), on_done=on_done,
                             on_error=on_error)

    def rename_key(self, ref: Any, key: str, *, on_done: Any = None, on_error: Any = None) -> bool:
        return self._command("rename_key", (to_edit_ref(self._session(), ref), key), on_done=on_done,
                             on_error=on_error)

    def insert(self, parent: Any, index: int | None, value: str, *, key: str | None = None,
               on_done: Any = None, on_error: Any = None) -> bool:
        """A new child of ``parent`` (``None``: a new record) at ``index`` (``None`` = the end)."""
        p = None if parent is None else to_edit_ref(self._session(), parent)
        return self._command("insert", (p, index, value), {"key": key}, on_done=on_done, on_error=on_error)

    def paste(self, parent: Any, text: str, *, index: int | None = None, key: str | None = None,
              as_records: bool = False, on_done: Any = None, on_error: Any = None) -> bool:
        p = None if parent is None else to_edit_ref(self._session(), parent)
        return self._command("paste", (p, text), {"index": index, "key": key, "as_records": bool(as_records)},
                             on_done=on_done, on_error=on_error)

    def delete(self, refs: Any, *, on_done: Any = None, on_error: Any = None) -> bool:
        items = self._refs(refs)
        return self._command("delete", (items,), on_done=on_done, count=len(items), on_error=on_error)

    def duplicate(self, refs: Any, *, on_done: Any = None, on_error: Any = None) -> bool:
        items = self._refs(refs)
        return self._command("duplicate", (items,), on_done=on_done, count=len(items), on_error=on_error)

    def move(self, refs: Any, parent: Any, index: int | None, *, on_done: Any = None, on_error: Any = None) -> bool:
        items = self._refs(refs)
        p = None if parent is None else to_edit_ref(self._session(), parent)
        return self._command("move", (items, p, index), on_done=on_done, count=len(items), on_error=on_error)

    def move_by(self, refs: Any, delta: int, *, on_done: Any = None, on_error: Any = None) -> bool:
        items = self._refs(refs)
        return self._command("move_by", (items, int(delta)), on_done=on_done, count=len(items), on_error=on_error)

    def change_type(self, refs: Any, to: str, *, on_done: Any = None, on_error: Any = None) -> bool:
        items = self._refs(refs)
        return self._command("change_type", (items, to), on_done=on_done, count=len(items), on_error=on_error)

    def delete_records(self, positions: Any, *, on_done: Any = None, on_error: Any = None) -> bool:
        """Delete the records at ``positions`` (a RecordSet of the state shown, an array, a range, a list; any size)
        in a task."""
        return self._records_task("delete_records", positions, t("gui.edit.task.delete_records", name=self._name()),
                                  on_done, on_error)

    def duplicate_records(self, positions: Any, *, on_done: Any = None, on_error: Any = None) -> bool:
        return self._records_task("duplicate_records", positions,
                                  t("gui.edit.task.duplicate_records", name=self._name()), on_done, on_error)

    def undo(self, *, on_done: Any = None) -> bool:
        if not self._check():
            _call(on_done, None)
            return False
        sets, lease = self._session()._edit_sets()
        self._submit(_Job(next(self._ids), "undo", None, sets), on_done, lease)
        return True

    def redo(self, *, on_done: Any = None) -> bool:
        if not self._check():
            _call(on_done, None)
            return False
        sets, lease = self._session()._edit_sets()
        self._submit(_Job(next(self._ids), "redo", None, sets), on_done, lease)
        return True

    def _name(self) -> str:
        session = self._session()
        return session.name if session is not None else self.doc.source_path.name

    def _scope_ok(self, rs: Any) -> bool:
        """A RecordSet of positions of the state shown (gen None / applied / an equivalent plain-file set)."""
        if not isinstance(rs, RecordSet):
            return True
        gen = rs.gen
        if gen is None or gen == self._applied_gen:
            return True
        if gen == 0 and not self.doc.edited:
            # a set of the plain file while the document shows the unedited state: the same positions. The
            # document now shows the edit session's state (the session re-tags its own sets with it).
            if not self._ensure_state():
                return False
            if rs.gen == 0:
                rs.gen = self._applied_gen
            return True
        return False

    def _change_task(self, title: str, fn: Callable[[Any, Any], Any], on_done: Any, *, kind: str = "edit",
                     heavy: bool = False, long: str | None = "applying", sets_extra: Sequence[Any] = (),
                     on_error: Any = None) -> Any:
        """Run ``fn(edit_session, ctx) -> ChangeSet`` in a task; the view sets are remapped in the task thread and the
        change is applied when it ends (``on_error``: see the commands)."""
        session = self._session()
        es = self._worker.session
        sets, lease = session._edit_sets()
        for rs in sets_extra:
            if isinstance(rs, RecordSet) and not any(rs is x for x in sets):
                session._held.append((lease, rs))
        snapshot = self.doc.pinned()

        def work(ctx: Any) -> tuple[Any, dict | None, dict]:
            cs = fn(es, ctx)
            remapped = None
            if cs is not None and cs.remap is not None and sets:
                try:
                    remapped = remap_view_sets(sets, cs.remap, cs.gen)     # never cancelled: the step is committed
                except Exception:
                    log.warning("remapping the view after an edit failed", exc_info=True)
            return cs, remapped, _status_of(es)

        def done(result: tuple[Any, dict | None, dict]) -> None:
            cs, remapped, status = result
            sess = self._session()
            if self._closed or sess is None or sess.closed:
                _close_sets(remapped)
                self._end_task(lease)
                return
            self._status = status
            self._long = None          # available again while the views take the change; still busy until applied
            applied = False
            try:
                applied = self._apply(cs, remapped, sess) if cs is not None else False
            except Exception as exc:   # a bug: the task still ends and the views follow the state (review CL-5)
                log.exception("applying the result of %s failed", title)
                self._recover(cs, exc, remapped)
            finally:
                self._end_task(lease)
            _call(on_done, cs if applied else None)

        def failed(exc: BaseException) -> None:
            self._end_task(lease)
            if isinstance(exc, EditError) and exc.key == "edits.error.stale_refs":
                self._status_message(t("gui.edit.stale"), 8000)
            elif kind == "edit" and user_mistake(exc):
                self._refused(exc, on_error)
            elif not isinstance(exc, Cancelled):
                self.app.report_error(exc)
            _call(on_done, None)

        def cancelled() -> None:
            self._end_task(lease)
            _call(on_done, None)

        self._long = long
        task = self.app.tasks.run(work, title=title, kind=kind, owner=self, heavy=heavy, on_done=done,
                                  on_error=failed, on_cancel=cancelled)
        self._busy_task = task
        session.keep_document_for(task, sets=sets, doc=snapshot)
        self._emit_state()
        return task

    def _end_task(self, lease: Any = None) -> None:
        if lease is not None:
            lease.release()
        self._busy_task = None
        self._long = None
        self._emit_state()
        self._run_idle_waiters()

    def _records_task(self, name: str, positions: Any, title: str, on_done: Any, on_error: Any = None) -> bool:
        if not self._check():
            _call(on_done, None)
            return False
        if not self._scope_ok(positions):
            self._status_message(t("gui.edit.stale"), 8000)
            _call(on_done, None)
            return False
        gen = self._applied_gen

        def fn(es: Any, ctx: Any) -> Any:
            return getattr(es, name)(positions, expect_gen=gen, progress=ctx.progress, cancel=ctx.cancel)

        self._change_task(title, fn, on_done, sets_extra=[positions], on_error=on_error)
        return True

    # ================================================================================
    # bulk operations
    # ================================================================================
    def bulk_preview(self, spec: Any, scope: Any, on_done: Callable[[Any], Any], *,
                     on_failed: Callable[[BaseException | None], Any] | None = None) -> Any:
        """Compute ``spec`` over ``scope`` (``None`` = all records, a RecordSet of the state shown, positions) in a
        heavy task; ``on_done(BulkPreview)`` in the GUI thread. Returns the GuiTask (``None`` when refused)."""
        if self._closed or not self.available or self._worker.session is None:
            self._status_message(t("gui.edit.unavailable", reason=reason_text(self.reason or "indexing")))
            _call(on_failed, None)
            return None
        if not self._scope_ok(scope):
            self._status_message(t("gui.edit.stale"), 8000)
            _call(on_failed, None)
            return None
        session = self._session()
        es = self._worker.session
        snapshot = self.doc.pinned()

        def work(ctx: Any) -> Any:
            return es.bulk_preview(spec, scope, progress=ctx.progress, cancel=ctx.cancel)

        def done(preview: Any) -> None:
            if self._closed:
                _discard_preview(preview)
                return
            _call(on_done, preview)

        def failed(exc: BaseException) -> None:
            if isinstance(exc, EditError) and exc.key in _STORE_GONE_KEYS:
                # the layer was saved or discarded while the preview ran: the preview is void, not an error
                _call(on_failed, None)
                return
            if not isinstance(exc, Cancelled):
                self.app.report_error(exc)
            _call(on_failed, exc)

        def cancelled() -> None:
            _call(on_failed, None)

        task = self.app.tasks.run(work, title=t("gui.edit.task.bulk_preview", name=self._name()), kind="bulk",
                                  owner=self, heavy=True, on_done=done, on_error=failed, on_cancel=cancelled)
        session.keep_document_for(task, sets=[scope] if isinstance(scope, RecordSet) else (), doc=snapshot)
        return task

    def _cancel_previews(self) -> None:
        """A save / discard closes the layer the running bulk previews read: they are cancelled first (review CL-7)."""
        try:
            tasks = self.app.tasks.tasks(owner=self, include_silent=True)
        except Exception:  # pragma: no cover - defensive
            return
        for task in tasks:
            if getattr(task, "kind", "") == "bulk":
                task.cancel()

    def bulk_apply(self, preview: Any, *, on_done: Any = None) -> bool:
        """Apply a preview computed on the state shown (``preview.gen == applied_gen``) as one step."""
        if getattr(preview, "gen", None) != self._applied_gen:
            self._status_message(t("gui.edit.preview_stale"), 8000)
            _call(on_done, None)
            return False
        if not self._check():
            _call(on_done, None)
            return False
        sets, lease = self._session()._edit_sets()
        self._submit(_Job(next(self._ids), "bulk_apply", preview, sets), on_done, lease)
        return True

    # ================================================================================
    # JSON Patch
    # ================================================================================
    def import_patch(self, path: Any, *, on_done: Any = None) -> bool:
        """Apply the JSON Patch file ``path`` as one step (heavy task kind ``patch``)."""
        if not self._check():
            _call(on_done, None)
            return False
        source = os.fspath(path)

        def fn(es: Any, ctx: Any) -> Any:
            return es.import_patch(source, progress=ctx.progress, cancel=ctx.cancel)

        self._change_task(t("gui.edit.task.import_patch", name=self._name()), fn, on_done, kind="patch",
                          heavy=True)
        return True

    def export_patch(self, path: Any, *, diff: bool | None = None, on_done: Any = None) -> bool:
        """Write the changes as a JSON Patch (task kind ``export``); ``diff=None``: ``False`` while the file changed on
        disk (whole-record replaces, no reads of the changed file). ``on_done(operation count | None)``."""
        if not self._check(allowed=("", "file_changed", "file_grown")):
            _call(on_done, None)
            return False
        target = os.fspath(path)
        if diff is None:
            diff = self.reason != "file_changed"
        es = self._worker.session
        session = self._session()
        snapshot = self.doc.pinned()
        notes: list[str] = []

        def work(ctx: Any) -> int:
            from jsonreshape import jsonpatch

            # the session's export (it takes the edit session's lock) with the exporter's warnings
            return int(jsonpatch.export_patch(es, target, diff=bool(diff), progress=ctx.progress, cancel=ctx.cancel,
                                              warnings=notes))

        def done(count: int) -> None:
            self._end_task()
            text = t("gui.edit.patch_exported", count=count, path=os.path.normpath(target))
            self._status_message(" ".join([text] + notes), 10000)
            _call(on_done, count)

        def failed(exc: BaseException) -> None:
            self._end_task()
            if not isinstance(exc, Cancelled):
                self.app.report_error(exc)
            _call(on_done, None)

        def cancelled() -> None:
            self._end_task()
            _call(on_done, None)

        task = self.app.tasks.run(work, title=t("gui.edit.task.export_patch", name=self._name()), kind="export",
                                  owner=self, on_done=done, on_error=failed, on_cancel=cancelled)
        self._busy_task = task
        session.keep_document_for(task, doc=snapshot)
        self._emit_state()
        return True

    # ================================================================================
    # discard
    # ================================================================================
    def discard(self, parent: Any = None, *, on_done: Any = None) -> None:
        """"Discard all changes": asks first, then deletes the layer (never offered for recovery)."""
        if not self._check(allowed=("", "file_changed", "file_grown")):
            _call(on_done, None)
            return
        if not self.dirty:
            self._status_message(t("gui.edit.nothing_to_discard"))
            _call(on_done, None)
            return

        def answer(yes: bool) -> None:
            if yes:
                self._discard_now(on_done)
            else:
                _call(on_done, None)

        _ask(parent, t("gui.edit.ask.discard", steps=self.steps), answer)

    def _discard_now(self, on_done: Any = None) -> None:
        if self._closed or self._worker.session is None:
            _call(on_done, None)
            return

        def run() -> None:
            if self._closed:
                _call(on_done, None)
                return
            self._cancel_previews()
            self._submit(_Job(next(self._ids), "discard"), lambda cs: self._discarded(cs, on_done), None)

        self._when_idle(run)

    def _discarded(self, cs: Any, on_done: Any) -> None:
        if cs is not None:
            self._status_message(t("gui.edit.discarded"), 6000)
        _call(on_done, cs)

    # ================================================================================
    # pending layers (recovery bar)
    # ================================================================================
    def restore_pending(self, info: Any, *, on_done: Any = None) -> None:
        """Restore the layer ``info`` (an interrupted save is rolled first). Only a layer of this file version can be
        restored (an older one: Export / Delete)."""
        if not self._pending_check(info):
            _call(on_done, None)
            return
        if not getattr(info, "matches", False) or getattr(info, "damaged", None):
            self._status_message(t("gui.edit.pending.not_restorable"), 10000)
            _call(on_done, None)
            return
        if self._worker.session is not None:
            self._status_message(t("gui.edit.busy"))
            _call(on_done, None)
            return
        self._restore(info, upto=None, roll_first=True, on_done=on_done)

    def _restore(self, info: Any, *, upto: int | None, roll_first: bool, on_done: Any = None) -> None:
        self._submit(_Job(next(self._ids), "restore", (info, upto, roll_first)), on_done, None)

    def _pending_check(self, info: Any) -> bool:
        if self._closed:
            return False
        if self.busy:
            self._status_message(t("gui.edit.busy"))
            return False
        if getattr(info, "locked", False):
            self._status_message(reason_text("locked"))
            return False
        if self._worker._held_lock() is None and self._open_state != "pending":
            # we do not hold the layer lock (another tab edits): nothing may touch the layers
            self._status_message(reason_text(self.reason or "locked"))
            return False
        return True

    def discard_pending(self, info: Any, parent: Any = None, *, on_done: Any = None) -> None:
        """Delete the pending layer ``info`` (asks first; damaged layers are removed file by file)."""
        if not self._pending_check(info):
            _call(on_done, None)
            return
        when = time.strftime("%Y-%m-%d %H:%M", time.localtime(info.updated)) if info.updated else "?"

        def answer(yes: bool) -> None:
            if yes and not self._closed and not self.busy:
                self._submit(_Job(next(self._ids), "discard_pending", (info,)), on_done, None)
            else:
                _call(on_done, None)

        _ask(parent, t("gui.edit.ask.discard_pending", time=when, steps=info.steps), answer)

    def roll_pending(self, info: Any, *, on_done: Any = None) -> None:
        """Finish or undo the interrupted save recorded in ``info.saving`` (the result is shown)."""
        if not self._pending_check(info) or not getattr(info, "saving", None):
            _call(on_done, None)
            return
        self._submit(_Job(next(self._ids), "roll", (info,)), on_done, None)

    def ignore_pending(self, info: Any) -> None:
        """Hide a pending layer that does not block editing (stale, damaged of another version, locked) until the
        next open."""
        if info in _blocking([info]):
            return
        self._ignored.add(info.key)
        self.pending_found.emit(self.pending)
        self._emit_state()

    def export_pending(self, info: Any, path: Any, *, on_done: Any = None) -> None:
        """Write the changes of the pending layer ``info`` as a JSON Patch to ``path`` (task kind ``export``); the
        layer stays (Discard deletes it). ``on_done(operation count | None)``."""
        if self._closed or self.busy:
            self._status_message(t("gui.edit.busy") if self.busy else reason_text("closed"))
            _call(on_done, None)
            return
        target = os.fspath(path)
        doc = self.doc
        settings = self._worker.settings
        lock = self._worker._held_lock()
        base = doc if getattr(info, "matches", False) else None
        session = self._session()
        notes: list[str] = []

        def work(ctx: Any) -> int:
            from jsonreshape import jsonpatch
            from jsonreshape.editstore import EditStore

            store = EditStore.open(doc, settings=settings, dir=info.dir, lock=lock, read_only=True, allow_stale=True)
            if store is None:
                raise UserInputError("gui.edit.pending.gone")
            try:
                return int(jsonpatch.export_layer(store, target, base_doc=base, progress=ctx.progress,
                                                  cancel=ctx.cancel, warnings=notes))
            finally:
                store.close(close_reader=True)

        def done(count: int) -> None:
            self._end_task()
            text = t("gui.edit.patch_exported", count=count, path=os.path.normpath(target))
            self._status_message(" ".join([text] + notes), 10000)
            _call(on_done, count)

        def failed(exc: BaseException) -> None:
            self._end_task()
            if not isinstance(exc, Cancelled):
                self.app.report_error(exc)
            _call(on_done, None)

        def cancelled() -> None:
            self._end_task()
            _call(on_done, None)

        task = self.app.tasks.run(work, title=t("gui.edit.task.export_pending", name=self._name()), kind="export",
                                  owner=self, on_done=done, on_error=failed, on_cancel=cancelled)
        self._busy_task = task
        if session is not None:
            session.keep_document_for(task)
        self._emit_state()

    # ================================================================================
    # save
    # ================================================================================
    def save(self, *, on_done: Any = None) -> None:
        """Save in place (questions first; then a heavy task); ``on_done(SaveReport | None)``."""
        self._save(None, on_done=on_done, reopen=True, parent=None)

    def save_as(self, path: Any, *, on_done: Any = None) -> None:
        """Save to ``path`` (same kind and encoding; ``.gz`` compresses); the session then shows that file."""
        self._save(os.fspath(path), on_done=on_done, reopen=True, parent=None)

    def _save(self, target: str | None, *, on_done: Any, reopen: bool, parent: Any) -> None:
        allowed = ("",) if target is None else ("", "file_grown")
        if not self._check(allowed=allowed):
            if target is None and self.reason == "file_grown":
                self._offer_save_as(parent, on_done, reopen)
                return
            _call(on_done, None)
            return
        if target is None and not self.dirty:
            self._status_message(t("gui.edit.nothing_to_save"))
            _call(on_done, None)
            return
        req = _SaveRequest(target, on_done, reopen, parent)
        if target is not None and not self._target_checked(req):
            return
        self._ask_save_questions(req)

    def _target_checked(self, req: _SaveRequest) -> bool:
        """Save As onto a file another tab shows (review S3): refused when that tab has changes (or another
        program edits the file), asked when it has none (it is reloaded after the save). True: go on now."""
        conflict = target_conflict(req.target, exclude=self._session())
        if conflict is None:
            return True
        what, sessions = conflict
        path = os.path.normpath(str(req.target))
        if what == "unsaved":
            self.app.report_error(UserInputError("gui.edit.target.unsaved", path=path))
            _call(req.on_done, None)
            return False
        if what == "locked":
            self.app.report_error(UserInputError("gui.edit.target.locked", path=path))
            _call(req.on_done, None)
            return False
        if what == "pending":           # unsaved changes kept for recovery (fix round L2): never asked about
            self.app.report_error(UserInputError("gui.edit.target.pending", path=path))
            _call(req.on_done, None)
            return False
        req.reload = [weakref.ref(s) for s in sessions]

        def answer(yes: bool) -> None:
            if yes and not self._closed:
                self._ask_save_questions(req)
            else:
                _call(req.on_done, None)

        _ask(req.parent, t("gui.edit.target.open", path=path), answer, title=t("gui.edit.ask.title.target"))
        return False

    def _ask_save_questions(self, req: _SaveRequest) -> None:
        """The questions every save asks first (``saver.save_questions``: brace_list, broken lines, gzip data after
        the stream of an in-place save without a backup -- no disk access), then the readers' check."""
        from jsonreshape import saver

        try:
            asked = list(saver.save_questions(self.doc, req.target, settings=self._worker.settings))
        except Exception as exc:  # pragma: no cover - defensive: the save itself refuses what it cannot do
            log.warning("the save questions of %s could not be built", self._name(), exc_info=True)
            self.app.report_error(exc)
            _call(req.on_done, None)
            return
        questions = [q.text for q in asked]
        titles = [t(_SAVE_QUESTION_TITLES.get(str(q.key), "gui.edit.ask.title")) for q in asked]
        req.drop_broken = any(q.key == "broken_lines" for q in asked)

        def ask_next(i: int) -> None:
            if i >= len(questions):
                self._before_save(req)
                return

            def answer(yes: bool) -> None:
                if yes and not self._closed:
                    ask_next(i + 1)
                else:
                    _call(req.on_done, None)

            _ask(req.parent, questions[i], answer, default_yes=True, title=titles[i])

        ask_next(0)

    def _readers(self) -> list[Any]:
        """Tasks reading the document that replacing the file would make fail (``keep_document_for``: exports, file
        tools, searches, statistics ...); the controller's own bulk previews are cancelled by the save instead."""
        session = self._session()
        fn = getattr(session, "document_users", None) if session is not None else None
        users = fn() if callable(fn) else []
        return [x for x in users if getattr(x, "owner", None) is not self
                and not bool(getattr(x, "cancel_requested", False))]

    def _before_save(self, req: _SaveRequest) -> None:
        """An in-place save while tasks still read the file (review CL-3): wait for them (default), stop them, or do
        not save. A Save As never touches the file they read."""
        if self._closed:
            _call(req.on_done, None)
            return
        readers = self._readers() if req.target is None else []
        if not readers:
            self._start_save(req)
            return
        session = self._session()
        name = session.name if session is not None else self.doc.source_path.name
        titles = [str(getattr(x, "title", "") or "") for x in readers]
        shown = ", ".join(x for x in titles[:3] if x) + ("\N{HORIZONTAL ELLIPSIS}" if len(titles) > 3 else "")
        text = t("gui.edit.ask.readers", name=name, count=len(readers), tasks=shown or "?")

        def answered(choice: str) -> None:
            if self._closed:
                _call(req.on_done, None)
            elif choice == "wait":
                self._wait_for_readers(readers, req)
            elif choice == "stop":
                for task in readers:
                    try:
                        task.cancel()
                    except Exception:  # pragma: no cover - defensive
                        log.debug("cancelling %r failed", task, exc_info=True)
                self._start_save(req)
            else:
                _call(req.on_done, None)

        self._question(req.parent, "readers", name, text, ("wait", "stop", "cancel"), answered, headless="wait",
                       labels={"wait": t("gui.edit.button.wait_save"), "stop": t("gui.edit.button.stop_save")})

    def _wait_for_readers(self, readers: list[Any], req: _SaveRequest) -> None:
        """A small task (kind ``save``, NOT heavy: it never holds a heavy-task slot a reader may wait for) that ends
        when every reader's thread ended; the controller is busy ("saving") meanwhile, then the save goes on. Its
        thread only sees the readers' plain end events (review CL-8)."""
        waits = [_end_waiter(x)[0] for x in readers]

        def work(ctx: Any) -> bool:
            for wait in waits:
                while not wait(0.1):
                    ctx.check()
            return True

        def done(_result: Any) -> None:
            # straight on to the save: the idle waiters (close questions) must not see the moment in between
            self._busy_task = None
            self._long = None
            self._saving = False
            if self._closed:
                _call(req.on_done, None)
                return
            allowed = ("",) if req.target is None else ("", "file_grown")
            if self._check(allowed=allowed):
                self._before_save(req)
            else:
                _call(req.on_done, None)
            if not self.busy:
                self._emit_state()
                self._run_idle_waiters()

        def failed(exc: BaseException) -> None:
            self._saving = False
            self._end_task()
            self.app.report_error(exc)
            _call(req.on_done, None)

        def cancelled() -> None:
            self._saving = False
            self._end_task()
            self._status_message(t("gui.edit.save_cancelled"), 6000)
            _call(req.on_done, None)

        self._long = "saving"
        self._saving = True
        task = self.app.tasks.run(work, title=t("gui.edit.task.save_wait", name=self._name()), kind="save",
                                  owner=self, on_done=done, on_error=failed, on_cancel=cancelled)
        self._busy_task = task
        self._status_message(t("gui.edit.save_waits", count=len(readers)), 8000)
        self._emit_state()

    def _start_save(self, req: _SaveRequest) -> None:
        if self._closed or self.busy or self._worker.session is None:
            self._status_message(t("gui.edit.busy") if self.busy else reason_text(self.reason or "closed"))
            _call(req.on_done, None)
            return
        session = self._session()
        es = self._worker.session
        doc = self.doc
        state = doc.overlay
        rids = session.bookmark_rids()
        snapshot = doc.pinned()
        name = self._name()
        target = req.target
        drop_broken = req.drop_broken

        def work(ctx: Any) -> tuple[Any, dict[int, int | None]]:
            report = es.save(target, drop_broken=drop_broken, progress=ctx.progress, cancel=ctx.cancel)
            positions: dict[int, int | None] = {}
            if rids:
                if state is not None and getattr(snapshot, "edited", False):
                    pieces = state.pieces
                    for rid in rids:
                        positions[rid] = pieces.pos_of(rid)
                else:
                    positions = {rid: rid for rid in rids}
            return report, positions

        def done(result: tuple[Any, dict]) -> None:
            # the saved state first, then the idle waiters: a close question waiting for the save must see "saved"
            self._saving = False
            self._busy_task = None
            self._long = None
            report, positions = result
            try:
                self._saved(report, positions, req)
            finally:
                self._end_task()

        def failed(exc: BaseException) -> None:
            self._saving = False
            self._end_task()
            sess = self._session()
            if sess is not None and not sess.closed:
                sess.resume_change_check()
            self._save_failed(exc, req)

        def cancelled() -> None:
            self._saving = False
            self._end_task()
            sess = self._session()
            if sess is not None and not sess.closed:
                sess.resume_change_check()
            self._status_message(t("gui.edit.save_cancelled"), 6000)
            _call(req.on_done, None)

        title = t("gui.edit.task.save", name=name) if target is None else \
            t("gui.edit.task.save_as", name=name, target=os.path.basename(target))
        self._cancel_previews()           # the save closes the layer they read (review CL-7)
        session.pause_change_check()
        self._long = "saving"
        self._saving = True
        task = self.app.tasks.run(work, title=title, kind="save", owner=self, heavy=True, on_done=done,
                                  on_error=failed, on_cancel=cancelled)
        self._busy_task = task
        session.keep_document_for(task, doc=snapshot)
        self._emit_state()

    def _saved(self, report: Any, positions: dict[int, int | None], req: _SaveRequest) -> None:
        """The save succeeded: the core closed the edit session (its layer is discarded)."""
        self._open_state = "saved"
        self._status = _status_of(None)
        session = self._session()
        in_place = bool(getattr(report, "in_place", req.target is None))
        if session is not None and not session.closed:
            try:
                session._bookmarks_saved(positions, self.layer_key,
                                         target=None if in_place else str(report.target))
            except Exception:
                log.exception("converting the bookmarks after saving failed")
            session._set_dirty(False)
        self._emit_state()
        # warnings of a successful save (e.g. edits.warn.layer_left: the layer could not be deleted afterwards)
        # are told with it -- they never turn it into a failed save
        warnings = list(getattr(report, "warnings", []) or [])
        text = t("gui.edit.saved", path=os.path.normpath(str(report.target)), records=report.records)
        self._status_message(" ".join([text] + warnings), 15000 if warnings else 10000)
        if req.reopen and session is not None and not session.closed:
            opts = dict(getattr(report, "reopen_options", {}) or {})
            try:
                if not in_place:
                    session.switch_path(str(report.target))
                session.reopen(discard_edits=True, keep_view=True, format=opts.get("format"),
                               record_path=opts.get("record_path"))
            except Exception as exc:
                log.exception("reopening after saving failed")
                self.app.report_error(exc)
        if req.reload:
            reload_sessions(req.reload)       # the other tabs of the replaced Save As target show the new file
        _call(req.on_done, report)

    def _save_failed(self, exc: BaseException, req: _SaveRequest) -> None:
        key = getattr(exc, "key", "")
        if isinstance(exc, Cancelled):
            _call(req.on_done, None)
            return
        if key == "saver.error.broken_lines" and not req.drop_broken:
            def answer(yes: bool) -> None:
                if yes and not self._closed:
                    req.drop_broken = True
                    self._before_save(req)
                else:
                    _call(req.on_done, None)

            _ask(req.parent, t("gui.edit.ask.broken_lines", count=exc.params.get("count", "?")), answer,  # type: ignore[attr-defined]
                 default_yes=True, title=t("gui.edit.ask.title.broken"))
            return
        self.app.report_error(exc)
        if req.target is None and key in ("saver.error.grown", "fileio.error.read_only"):
            self._offer_save_as(req.parent, req.on_done, req.reopen)
            return
        _call(req.on_done, None)

    def _offer_save_as(self, parent: Any, on_done: Any, reopen: bool) -> None:
        def answer(yes: bool) -> None:
            if not yes or self._closed:
                _call(on_done, None)
                return

            def picked(path: str | None) -> None:
                if not path or self._closed:
                    _call(on_done, None)
                    return
                self._save(path, on_done=on_done, reopen=reopen, parent=parent)

            self._ask_path(parent, "save_as", picked)

        _ask(parent, t("gui.edit.ask.save_as_instead"), answer, default_yes=True, title=t("gui.edit.ask.title.save_as"))

    def _suggested(self, purpose: str) -> str:
        src = str(self.doc.source_path)
        if purpose == "patch":
            return os.path.splitext(src)[0] + ".patch.json"
        stem, ext = os.path.splitext(src)
        return f"{stem}{t('gui.edit.path.copy_suffix')}{ext}"

    def _ask_path(self, parent: Any, purpose: str, callback: Callable[[str | None], Any]) -> None:
        """Ask for a target file (non-blocking): ``callback(path | None)``."""
        suggested = self._suggested(purpose)
        handler = ask_path_handler
        if handler is not None:
            path = handler(parent, purpose, suggested)
            QTimer.singleShot(0, lambda: callback(path))
            return
        if getattr(self.app, "headless", False):
            QTimer.singleShot(0, lambda: callback(None))
            return
        from PySide6.QtWidgets import QFileDialog

        from jsonreshape.gui.widgets import keep_alive

        title = t("gui.edit.path.patch") if purpose == "patch" else t("gui.edit.path.save_as")
        dialog = QFileDialog(parent, title, suggested)
        dialog.setAcceptMode(QFileDialog.AcceptMode.AcceptSave)
        dialog.setFileMode(QFileDialog.FileMode.AnyFile)
        if purpose == "patch":
            dialog.setNameFilters([t("gui.edit.path.filter_patch"), t("gui.edit.path.filter_all")])
            dialog.setDefaultSuffix("json")
        keep_alive(dialog)
        answered: list[bool] = []

        def finished(result: int) -> None:
            if answered:
                return
            answered.append(True)
            files = dialog.selectedFiles() if result else []
            callback(files[0] if files else None)

        dialog.finished.connect(finished)
        dialog.open()

    # ================================================================================
    # closing
    # ================================================================================
    def ask_close(self, parent: Any, on_done: Callable[[bool], Any], *, why: str = "close") -> None:
        """Before closing / reopening (``why``): a non-blocking Save / Don't save / Cancel question when there are
        unsaved changes, a "wait / stop / cancel" question while a long operation runs (module docstring).
        ``on_done(True)``: go on (saved, discarded, nothing to save, or headless keeping the layer);
        ``on_done(False)``: stop."""
        session = self._session()
        if self._closed or session is None or session.closed:
            _call(on_done, True)
            return
        task = self._busy_task
        if task is not None and (self._saving or not self.dirty):
            # a save, or a long operation of a document without changes: closing would drop its result
            # silently (review CL-4)
            self._ask_busy(parent, on_done, why=why, task=task)
            return
        if not self.dirty:
            _call(on_done, True)
            return
        state = self._file_state
        if state == "changed":
            choices: tuple[str, ...] = ("patch", "discard", "cancel")
            text = t("gui.edit.ask.close_changed", name=session.name, steps=self.steps)
        elif state == "grown":
            choices = ("save_as", "discard", "cancel")
            text = t("gui.edit.ask.close_grown", name=session.name, steps=self.steps)
        else:
            choices = ("save", "discard", "cancel")
            text = t("gui.edit.ask.close", name=session.name, steps=self.steps)
        handler = ask_save_handler
        if handler is None and getattr(self.app, "headless", False):
            _call(on_done, True)          # headless: the layer stays for recovery
            return

        def answered(choice: str) -> None:
            if self._closed:
                _call(on_done, choice != "cancel")
                return
            self._close_choice(choice, parent, on_done)

        if handler is not None:
            choice = str(handler(parent, session.name, choices))
            QTimer.singleShot(0, lambda: answered(choice if choice in choices else "cancel"))
            return
        _ask_choice(parent, text, choices, answered)

    def _ask_busy(self, parent: Any, on_done: Callable[[bool], Any], *, why: str, task: Any) -> None:
        """"<operation> has not finished: wait / stop / cancel" (default wait). Wait: ask again once idle (the
        unsaved changes question follows when the operation left changes); stop: cancel it, then the same."""
        session = self._session()
        name = session.name if session is not None else self.doc.source_path.name
        if ask_choice_handler is None and getattr(self.app, "headless", False):
            # nobody can answer: as before -- a running save refuses, anything else goes on (the task is
            # cancelled by detach, a layer with steps is kept for recovery)
            if self._saving:
                self._status_message(t("gui.edit.save_wait"), 6000)
                _call(on_done, False)
            else:
                _call(on_done, True)
            return
        title = str(getattr(task, "title", "") or "")
        text = t("gui.edit.ask.busy", name=name, task=title or "?")

        def again() -> None:
            self.ask_close(parent, on_done, why=why)

        def answered(choice: str) -> None:
            if self._closed:
                _call(on_done, choice != "cancel")
            elif choice == "wait":
                self._when_idle(again)
            elif choice == "stop":
                busy = self._busy_task
                if busy is not None:
                    busy.cancel()
                self._when_idle(again)
            else:
                _call(on_done, False)

        self._question(parent, "busy", name, text, ("wait", "stop", "cancel"), answered, headless="cancel",
                       labels={"wait": t("gui.edit.button.wait"), "stop": t("gui.edit.button.stop")},
                       title=t("gui.edit.ask.busy_title"))

    def _question(self, parent: Any, purpose: str, name: str, text: str, choices: tuple[str, ...],
                  callback: Callable[[str], Any], *, headless: str, labels: dict[str, str] | None = None,
                  title: str | None = None) -> None:
        """A non-blocking question with ``choices`` (the first is the default): :data:`ask_choice_handler` answers
        when set; headless without it: ``headless``; else a window-modal message box."""
        handler = ask_choice_handler
        if handler is not None:
            choice = str(handler(parent, purpose, name, choices))
            QTimer.singleShot(0, lambda: callback(choice if choice in choices else "cancel"))
            return
        if getattr(self.app, "headless", False):
            QTimer.singleShot(0, lambda: callback(headless))
            return
        _ask_choice(parent, text, choices, callback, labels=labels, title=title)

    def _close_choice(self, choice: str, parent: Any, on_done: Callable[[bool], Any]) -> None:
        if choice == "save":
            self._when_idle(lambda: self._save(None, on_done=lambda rep: _call(on_done, rep is not None),
                                               reopen=False, parent=parent))
        elif choice == "save_as":
            def picked(path: str | None) -> None:
                if not path or self._closed:
                    _call(on_done, False)
                    return
                self._when_idle(lambda: self._save(path, on_done=lambda rep: _call(on_done, rep is not None),
                                                   reopen=False, parent=parent))

            self._ask_path(parent, "save_as", picked)
        elif choice == "patch":
            def picked_patch(path: str | None) -> None:
                if not path or self._closed:
                    _call(on_done, False)
                    return
                self._when_idle(lambda: self.export_patch(path, diff=False,
                                                          on_done=lambda n: _call(on_done, n is not None)))

            self._ask_path(parent, "patch", picked_patch)
        elif choice == "discard":
            self._discard_now(lambda cs: _call(on_done, cs is not None))
        else:
            _call(on_done, False)

    def detach(self, timeout: float = 2.0) -> None:
        """Stop accepting commands, cancel the controller's tasks, let the running worker command finish (up to
        ``timeout`` seconds) and close the edit session (a layer with steps is kept for recovery)."""
        if self._closed:
            return
        self._closed = True
        self._lock_timer.stop()
        try:
            self.app.tasks.detach(self)
        except Exception:  # pragma: no cover - defensive
            log.debug("detaching the edit tasks failed", exc_info=True)
        for _kind, _cb, lease, _on_error in list(self._jobs.values()):
            if lease is not None:
                lease.release()
        self._jobs.clear()
        self._busy_job = None
        self._busy_task = None
        self._saving = False
        self._long = None
        waiters, self._idle_waiters = self._idle_waiters, []
        session = self._session()
        if session is not None:
            try:
                session.file_changed.disconnect(self._on_file_changed)
            except (RuntimeError, TypeError):
                pass
        self._worker.close()          # interrupts a command waiting for the key index first
        if not self._worker.join(max(0.0, float(timeout))):
            log.info("the edit worker of %s is still busy; it closes the edit session when its command ends",
                     self.doc.source_path.name)
        self._emit_state()
        if waiters:
            # answers waiting for this controller to become idle (a close question's "wait", "Save" of the close
            # question, an inline editor's commit before a save) are given once, from the event loop -- never
            # inside the reopen / close that detaches us -- and see ``closed``: a close flow never hangs
            QTimer.singleShot(0, lambda fns=waiters: [_call(fn) for fn in fns])


# ----------------------------------------------------------------------------------
# helpers
# ----------------------------------------------------------------------------------
def _call(fn: Callable[..., Any] | None, *args: Any) -> None:
    """Call a GUI callback; its exceptions are logged, never raised into the controller."""
    if fn is None:
        return
    try:
        fn(*args)
    except Exception:
        log.exception("an edit callback failed")


def _discard_preview(preview: Any) -> None:
    discard = getattr(preview, "discard", None)
    if callable(discard):
        try:
            discard()
        except Exception:  # pragma: no cover - best effort
            log.debug("discarding a bulk preview failed", exc_info=True)


def _ask(parent: Any, text: str, callback: Callable[[bool], Any], *, default_yes: bool = False,
         title: str | None = None) -> None:
    """A non-blocking yes/no question (``widgets.ask``; its ``confirm_handler`` answers in tests). ``title``: the
    window title (default "Saqlanmagan o'zgarishlar"; the questions about something else have their own: review
    L6)."""
    from jsonreshape.gui import widgets

    if parent is None:
        parent = _active_window()
    widgets.ask(parent, text, callback, title=title or t("gui.edit.ask.title"), default_yes=default_yes)


#: Window titles of the save questions (``saver.save_questions`` keys; review L6: not all "unsaved changes").
_SAVE_QUESTION_TITLES = {"brace_list": "gui.edit.ask.title.brace_list", "broken_lines": "gui.edit.ask.title.broken",
                         "gzip_trailing": "gui.edit.ask.title.gzip"}


def _active_window() -> Any:
    try:
        from PySide6.QtWidgets import QApplication

        return QApplication.activeWindow()
    except Exception:  # pragma: no cover - no widgets application
        return None


def _ask_choice(parent: Any, text: str, choices: tuple[str, ...], callback: Callable[[str], Any], *,
                labels: dict[str, str] | None = None, title: str | None = None) -> None:
    """A 3-way question as a window-modal message box opened without a nested event loop (the first choice is the
    default, ``cancel`` the escape button; ``labels`` overrides button texts)."""
    from PySide6.QtWidgets import QMessageBox

    from jsonreshape.gui.widgets import keep_alive

    if parent is None:
        parent = _active_window()
    box = QMessageBox(QMessageBox.Icon.Warning, title or t("gui.edit.ask.title"), text,
                      QMessageBox.StandardButton.NoButton, parent)
    box.setTextFormat(Qt.TextFormat.PlainText)
    roles = {
        "save": (lambda: t("gui.edit.button.save"), QMessageBox.ButtonRole.AcceptRole),
        "save_as": (lambda: t("gui.edit.button.save_as"), QMessageBox.ButtonRole.AcceptRole),
        "patch": (lambda: t("gui.edit.button.patch"), QMessageBox.ButtonRole.AcceptRole),
        "discard": (lambda: t("gui.edit.button.discard"), QMessageBox.ButtonRole.DestructiveRole),
        "wait": (lambda: t("gui.edit.button.wait"), QMessageBox.ButtonRole.AcceptRole),
        "stop": (lambda: t("gui.edit.button.stop"), QMessageBox.ButtonRole.DestructiveRole),
        "cancel": (lambda: t("gui.edit.button.cancel"), QMessageBox.ButtonRole.RejectRole),
    }
    buttons = {}
    for choice in choices:
        label, role = roles[choice]
        text_of = (labels or {}).get(choice)
        buttons[choice] = box.addButton(text_of if text_of else label(), role)
    box.setDefaultButton(buttons[choices[0]])
    box.setEscapeButton(buttons["cancel"])
    keep_alive(box)
    answered: list[str] = []

    def finished(_result: int) -> None:
        if answered:
            return
        clicked = box.clickedButton()
        choice = next((c for c, b in buttons.items() if b is clicked), "cancel")
        answered.append(choice)
        callback(choice)

    box.finished.connect(finished)
    box.open()
