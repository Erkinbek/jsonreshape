"""File tools dialogs: split one file into parts, merge several files into one (docs/ARCHITECTURE.md §14.9
"File tools", §14.4; design ``s5/map_convert.md`` §6; AGENT_PROMPT §10 "Bo'lish va birlashtirish").

:class:`SplitDialog` -- "Faylni bo'lish"
=======================================
Cuts the records of one :class:`~jsonreshape.gui.session.DocSession` into parts written as separate files
(:func:`jsonreshape.filetools.split`):

* **source**: the whole document (the pinned snapshot: unsaved edits included), the current view (filter /
  sort / shown records -- positions of the same snapshot; written through the writer, engine B) or one
  array / object node (``source=NodeRef``: the file is read again with that node as the record container,
  :func:`node_document`);
* **mode**: a number of parts (balanced by bytes or by records), records per part, or a part size
  (``100 MB``; binary units like the CLI's ``--size``: 1 MB = 1024 KB, a plain number means MB);
* **output**: folder (default: the file's folder), name pattern (``{stem}_{n}{ext}``), format ("the
  original format" = the byte copy of engine A, or JSON / JSON Lines / CSV / Excel / SQLite through the
  writer), the JSON indentation, "keep the wrapper in every part" (unchecked = ``--no-wrapper``), "leave
  out the broken lines" (``--drop-broken``), "Eski qismlarni o'chirish" (``SplitOptions.remove_stale``:
  the rest of an earlier split into the same name pattern is deleted in the same all-or-nothing commit;
  off by default and not remembered -- it deletes files; the tooltip says what is removed and what never
  is; the report lists the removed files);
* a **live plan**: every change of the inputs (debounced, :data:`PLAN_DELAY_MS`) starts a silent,
  cancellable task running :func:`jsonreshape.filetools.plan_split` on a snapshot taken at that moment
  (``session.snapshot()``; the view set and the pinned state are leased with ``keep_document_for``). The
  dialog shows the number of parts, their names, record ranges and estimated sizes, the files that would be
  replaced and every refusal (bad numbers, more parts than records, too many files, partial / single
  documents, broken lines, a folder that does not exist or is not a folder, a part that would be the source
  file, a read-only part). A newer request cancels the running plan task; results of older requests are
  dropped (a serial number). An edit of the document (``data_changed``), a new view, the end of indexing or
  a finished split make the plan stale: it is computed again;
* **Bo'lish** is enabled only with a current plan; it asks before replacing existing files, then runs the
  split as a heavy task through :class:`FileToolsRunner` (progress, "To'xtatish", the tasks panel) on the
  snapshot taken at the click. A part that another tab shows is refused while that tab has unsaved,
  pending or running changes, or when another window of the program edits it (its edit lock); tabs that
  show parts unchanged are named in the question (instead of the plain overwrite question) and open the
  new files when the split is done (review S3). The report window lists every part with its size, the old
  parts removed and the warnings, with "open the folder" / "open the first part".

:class:`MergeDialog` -- "Fayllarni birlashtirish"
================================================
Merges several documents into ONE JSON or JSON Lines file (:func:`jsonreshape.filetools.merge`):

* **inputs**, in order: files from disk (added with the file dialog or dropped on the dialog; each one is
  opened and indexed by a silent task, one at a time, to show its kind, records, size and JSON errors and
  to suggest deduplication keys) and open tabs (``open_documents()`` lists them). An input whose file is
  open in a tab -- also one added as a file -- is read through that tab's pinned snapshot taken when the
  merge starts, so unsaved edits are merged as they are shown; a tab closed meanwhile becomes a file input
  again. Remove (Delete), move up / down (Alt+Up / Alt+Down);
* **output** file (JSON / JSON Lines follows the extension, ``.gz`` compresses), indentation, sorted keys,
  "keep the first file's wrapper", "take the records before the error from files with a JSON error";
* **deduplication**: off, or a key (``$`` = the whole record, ``$key`` = the member key, a field path;
  suggestions from the first input's sampled schema; validated with :func:`jsonreshape.dupes.parse_key`)
  keeping the first (earliest input) or the last copy;
* the output may not be an input (also under another name: ``samefile``); an existing output is replaced
  only after a question; an output that another tab shows is refused while that tab has changes (or
  another window of the program edits it) and asked about when it shows the file unchanged -- that tab
  opens the merged file afterwards (review S3). The merge runs as a heavy task (progress, stop); the
  report lists the inputs with the records written of each, the duplicates removed and the warnings
  ("open the folder" / "open the result").

Runner and report
=================
:class:`FileToolsRunner` (one per application: :func:`filetools_runner`) runs the split / merge tasks
(``heavy=True``) independently of the dialog that started them: closing the dialog lets the task go on in
the tasks panel and its outcome is still reported -- :class:`FileToolsReportDialog` (non-modal), errors
through ``app.report_error``, the result in the status bar. The documents a task reads stay open until it
ended (``DocSession.keep_document_for`` with the pinned state and the view set it reads).

Threading: the GUI thread never reads a data file and never computes a plan; it only reads cheap document
attributes (kind, record count, broken-line count), stats a few paths when a merge starts (the source
check, "replace the existing file?") and lists nothing. Workers never touch widgets: plan / probe / run
results come back through task callbacks.

Integration (round B): :func:`show_split_dialog` / :func:`show_merge_dialog` (one split dialog per
session, one merge dialog per application, both non-modal), :func:`node_document` = :func:`node_path` +
:func:`open_at` (the node re-opened as a record container -- also what the export dialog needs for CSV /
XLSX / SQLite of a node), and :func:`filetools_runner` (``jobs(session)`` for "exports still running").
The plan resolves a node source once (``SplitPlan.node_path``); the split task reopens the file at that
path and never calls into the session from its thread.

Decisions (where the spec leaves room; chosen to keep the user's data safe)
==========================================================================
1. A node is split by reading the file again with the node's path as the record path (one indexing scan,
   cached per record path). That cannot show unsaved edits and JSON Lines / concatenated values /
   brace-list files cannot be reopened that way: the node source is refused for an edited document ("save
   first") and for those kinds (split the whole file or the view, or extract the node first).
2. Sizes are binary in these dialogs (input like the CLI's ``--size``; the plan and the report show
   ``1 MB = 1024 KB`` too, so a part never looks bigger than the size asked for).
3. Part names in the plan are the core's :func:`jsonreshape.filetools.part_names` (decided by the code
   the split uses); the split report always lists the files actually written.
4. Planned sizes are estimates ("≈"): the record extents of an unedited document read in file order, else
   the average record size times the records of the part.
5. The split may only start from a current plan: the overwrite question names the files of that plan.
6. A merge input open in a tab uses that tab's snapshot, also when it was added as a file (design §6);
   tabs that are still indexing must finish first.
7. A runner of its own instead of ``exportdialog.ExportRunner``: that one always shows the export report
   window, whose "open folder" cannot be injected (tests would start Explorer) and which has no merge
   summary (inputs, duplicates) and no "open the result".
8. ``filetools.error.part_open_edited`` (the core refuses to replace or remove a file that has unsaved edits
   in this program, also edits kept for recovery or made in another window) is the user's to resolve: the
   status bar and the dialog say so with the core's hint, no error window.
9. Outputs shown in other tabs (review S3) are checked in the GUI thread with
   :func:`~jsonreshape.gui.editing.target_conflict` -- for a split only the parts a tab shows or whose
   edit lock the plan task found held (``SplitPlan.locked``), never a probe per part. Old parts that
   "Eski qismlarni o'chirish" removes are not checked here (which ones the core removes is decided in the
   split): the core never removes one with unsaved edits, and a tab showing one unchanged sees its file
   gone (known limit).
"""

from __future__ import annotations

import dataclasses
import functools
import logging
import os
import re
import weakref
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterable

import shiboken6
from PySide6.QtCore import QEvent, QObject, Qt, QTimer, Signal
from PySide6.QtWidgets import (
    QAbstractItemView,
    QButtonGroup,
    QCheckBox,
    QComboBox,
    QDialog,
    QFileDialog,
    QFormLayout,
    QGridLayout,
    QGroupBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMenu,
    QPlainTextEdit,
    QProgressBar,
    QPushButton,
    QRadioButton,
    QSpinBox,
    QToolButton,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)

from jsonreshape import dupes, fileio, filetools, writer
from jsonreshape.errors import Cancelled, JsonReshapeError, UserInputError
from jsonreshape.fileio import long_path, normalize_path
from jsonreshape.filetools import MergeOptions, SplitOptions
from jsonreshape.gui import copyfmt, theme
from jsonreshape.gui.searchpanel import check_not_source, keep_for, open_target_check, reload_tabs, target_conflict_text
from jsonreshape.gui.widgets import confirm, format_count, keep_alive, open_local_path
from jsonreshape.i18n import has, t
from jsonreshape.reader import Document
from jsonreshape.recordset import RecordSet
from jsonreshape.schema import field_paths, infer_schema
from jsonreshape.tasks import CancelToken, check, format_duration, parse_size
from jsonreshape.writer import ExportOptions

log = logging.getLogger(__name__)

__all__ = [
    "FileToolsJob",
    "FileToolsReportDialog",
    "FileToolsRunner",
    "MergeDialog",
    "MergeInput",
    "MergeOutcome",
    "SplitDialog",
    "SplitOutcome",
    "SplitPlan",
    "check_node_source",
    "error_text",
    "filetools_runner",
    "node_document",
    "node_path",
    "open_at",
    "parse_part_size",
    "show_merge_dialog",
    "show_split_dialog",
    "size_text",
]

PLAN_DELAY_MS = 200            # the plan is computed this long after the last change of the inputs
VALIDATE_DELAY_MS = 0          # the merge inputs are validated on the next event-loop turn
PREVIEW_MAX = 500              # parts listed in the plan (the summary counts all of them)
REPORT_FILES_MAX = 2000        # files listed in the report window
FIELDS_MAX = 300               # deduplication key suggestions
FIELDS_SAMPLE = 200            # records sampled for the suggestions
NAMES_SHOWN = 5                # file names quoted in a question / a warning
STATE_SPLIT = "filetools_split"   # GuiState: the last split options
STATE_MERGE = "filetools_merge"   # GuiState: the last merge options and folder
SOURCES = ("document", "view", "node")
MODES = ("parts", "records", "size")
SPLIT_FORMATS = ("same", "json", "jsonl", "csv", "xlsx", "sqlite")
MERGE_FORMATS = ("json", "jsonl")
DEFAULT_PARTS = 2
DEFAULT_RECORDS = 100_000
DEFAULT_SIZE_TEXT = "100 MB"
RECORDS_MAX = 2_000_000_000    # records per part a spin box offers
OPEN_PATTERNS = "*.json *.jsonl *.ndjson *.geojson *.json.gz *.jsonl.gz *.ndjson.gz"
MERGED_SUFFIX = "_merged"      # default output name: <first input>_merged.json
WHOLE_RECORD = "$"             # deduplication key: the whole record (dupes.parse_key)
_INDENTS: tuple[tuple[str, Any], ...] = (("2", 2), ("4", 4), ("tab", "\t"), ("compact", None))
_SORTS: tuple[tuple[str, Any], ...] = (("none", False), ("alpha", "alpha"), ("codepoint", "codepoint"))
_EXT = {"json": ".json", "jsonl": ".jsonl", "csv": ".csv", "xlsx": ".xlsx", "sqlite": ".sqlite"}
_LINE_KINDS = ("jsonl", "stream")
_NODE_REFUSED = ("jsonl", "stream", "brace_list")
_NUMBER_RE = re.compile(r"\d+(?:\.\d+)?")
#: core errors whose hint names a CLI option: the dialogs give a hint of their own instead
_GUI_HINTS = {
    "filetools.error.single": "gui.filetools.hint.single",
    "filetools.error.partial": "gui.filetools.hint.partial",
    "filetools.error.broken_lines": "gui.filetools.hint.broken",
    "filetools.error.partial_input": "gui.filetools.hint.partial_input",
    "filetools.error.merge_format": "gui.filetools.hint.merge_format",
}
#: core errors whose own hint suits the GUI as it is
_CORE_HINTS = frozenset({"filetools.error.too_many_parts", "filetools.error.too_many_files",
                         "filetools.error.name_pattern", "filetools.error.disk_space",
                         "filetools.error.part_open_edited"})
#: refusals of a started split / merge that are the user's to resolve: told in the status bar and the dialog,
#: never in an error window (a part / an output with unsaved edits in this program: decision 8)
_USER_ERROR_KEYS = frozenset({"filetools.error.part_open_edited"})


# ==================================================================================
# small helpers
# ==================================================================================
def _alive(obj: Any) -> bool:
    """``obj`` is a Qt object whose C++ side still exists (any other object: True)."""
    if obj is None:
        return False
    try:
        return bool(shiboken6.isValid(obj))
    except Exception:  # pragma: no cover - not a Qt object
        return True


def _session_ok(session: Any) -> bool:
    return _alive(session) and not getattr(session, "closed", False)


def _amp(text: str) -> str:
    """A text for a button / radio label (``&`` is a mnemonic marker there)."""
    return copyfmt.escape_surrogates(text).replace("&", "&&")


def _plain(text: str) -> str:
    return copyfmt.escape_surrogates(text)


def error_text(exc: BaseException) -> str:
    """The localized text of ``exc`` for the dialogs: the message plus a hint that suits the GUI (the
    core's hints of some errors name CLI options)."""
    if not isinstance(exc, JsonReshapeError):
        return t("gui.filetools.error.unexpected", error=str(exc) or type(exc).__name__)
    text = exc.message
    hint_key = _GUI_HINTS.get(exc.key)
    hint = t(hint_key) if hint_key is not None else (exc.hint if exc.key in _CORE_HINTS else None)
    return f"{text}\n{hint}" if hint else text


def size_text(n: float) -> str:
    """A size in binary units (1 KB = 1024 B), as the size inputs of these dialogs read them."""
    value = float(n)
    if value != value or value in (float("inf"), float("-inf")):
        return "? B"
    if abs(value) < 1024:
        return f"{int(value)} B"
    for unit in ("KB", "MB", "GB", "TB"):
        value /= 1024.0
        if abs(round(value, 1)) < 1024 or unit == "TB":
            return f"{value:.1f} {unit}"
    return f"{value:.1f} TB"  # pragma: no cover


def parse_part_size(text: str) -> int:
    """Bytes of a part size text: ``100`` (a plain number means MB, like the CLI's ``--size``), ``1.5 GB``,
    ``1,5 GB`` (decimal comma), ``500KB``, ``2 GiB`` -- binary units (1 MB = 1024 KB).
    ``UserInputError("gui.filetools.error.bad_size")`` for anything else, zero included."""
    raw = str(text)
    compact = raw.strip().replace(" ", "").replace("\u00a0", "").replace(",", ".").upper().replace("IB", "B")
    if not compact:
        raise UserInputError("gui.filetools.error.bad_size", text=raw.strip())
    try:
        value = parse_size(compact + "MB" if _NUMBER_RE.fullmatch(compact) else compact)
    except ValueError:
        raise UserInputError("gui.filetools.error.bad_size", text=raw.strip()) from None
    if value < 1:
        raise UserInputError("gui.filetools.error.bad_size", text=raw.strip())
    return value


def _stem_ext(name: str) -> tuple[str, str]:
    """``("x", ".jsonl.gz")`` for ``x.jsonl.gz`` (as :mod:`jsonreshape.filetools` names parts)."""
    if name.lower().endswith(".gz") and len(name) > 3:
        stem, ext = os.path.splitext(name[:-3])
        return stem, ext + name[-3:]
    return os.path.splitext(name)


def _engine(opts: SplitOptions, source: Any) -> str:
    """``"same"`` (engine A, byte copy) or ``"export"`` (the writer), as :func:`filetools.split` decides."""
    if opts.export is None and (source is None or (isinstance(source, range) and source.step == 1)):
        return "same"
    return "export"


def _parts_fmt(doc: Any, opts: SplitOptions, engine: str) -> str:
    """The parts' format: the document's kind (engine A), else the writer format."""
    if engine == "same":
        return str(doc.kind)
    if opts.export is not None:
        return opts.export.fmt
    return "jsonl" if doc.kind in _LINE_KINDS else "json"


def _user_error(exc: BaseException | None) -> bool:
    """``exc`` is a refusal the user resolves (``_USER_ERROR_KEYS``): no error window for it."""
    return isinstance(exc, UserInputError) and getattr(exc, "key", None) in _USER_ERROR_KEYS


def _lock_held(path: Path) -> bool:
    """The edit lock of the file at ``path`` is held: a tab of this program or another window of it edits the
    file (``editstore.LayerLock``; an edit session holds it also without changes). Worker threads (a stat and
    a lock probe); ``False`` when it cannot be told."""
    try:
        from jsonreshape.editstore import LayerLock, file_lock_key

        key = file_lock_key(path)
        return key is not None and LayerLock.is_locked(key)
    except Exception:  # noqa: BLE001 - an unusable lock folder: nothing can be told (the split checks again)
        log.debug("probing the edit lock of %s failed", path, exc_info=True)
        return False


def _kind_text(kind: Any) -> str:
    key = f"reader.kind.{kind}"
    return t(key) if has(key) else str(kind)


def _format_text(fmt: str) -> str:
    key = f"gui.filetools.format.{fmt}"
    return t(key) if has(key) else str(fmt)


def _names_text(paths: Iterable[Any], shown: int = NAMES_SHOWN) -> str:
    names = [Path(p).name for p in paths]
    text = ", ".join(names[:shown])
    if len(names) > shown:
        text += " " + t("gui.filetools.more_names", count=format_count(len(names) - shown))
    return _plain(text)


def _copy_split_options(opts: SplitOptions) -> SplitOptions:
    """A private copy (a task must not share the options object the dialog may build again)."""
    export = dataclasses.replace(opts.export) if opts.export is not None else None
    return dataclasses.replace(opts, export=export)


def _file_size(path: Any) -> int:
    try:
        return int(os.stat(long_path(path)).st_size)
    except OSError:  # pragma: no cover - just written
        return 0


# -- default file dialogs (tests inject their own askers) ----------------------------
def _ask_folder(parent: QWidget | None, title: str, start: str) -> str | None:
    path = QFileDialog.getExistingDirectory(parent, title, start)
    return path or None


def _ask_files(parent: QWidget | None, title: str, start: str) -> list[str]:
    paths, _chosen = QFileDialog.getOpenFileNames(parent, title, start,
                                                  t("gui.filetools.merge.files_filter", patterns=OPEN_PATTERNS))
    return [p for p in paths if p]


def _ask_output(parent: QWidget | None, title: str, start: str) -> str | None:
    path, _chosen = QFileDialog.getSaveFileName(parent, title, start, t("gui.filetools.merge.output_filter"))
    return path or None


def _default_open_file(app: Any) -> Callable[[str], Any]:
    def open_file(path: Any) -> Any:
        request = getattr(app, "request_open", None)
        if callable(request):
            return request(str(path), {})
        return app.open_requested.emit(str(path), {})
    return open_file


# ==================================================================================
# a node as a record container (worker threads)
# ==================================================================================
def check_node_source(doc: Any) -> None:
    """The cheap refusals of a node source (any thread): a JSON Lines / concatenated / brace-list document
    cannot be reopened with a record path; an edited document would lose its unsaved changes."""
    if doc.kind in _NODE_REFUSED:
        raise UserInputError("gui.filetools.error.node_kind", kind=_kind_text(doc.kind))
    if bool(getattr(doc, "edited", False)):
        raise UserInputError("gui.filetools.error.node_edited")


def node_path(session: Any, ref: Any, doc: Any, *, where: str | None = None,
              cancel: CancelToken | None = None) -> tuple:
    """The absolute path of the array / object node ``ref`` resolved in the pinned document ``doc``
    (worker threads only). ``UserInputError`` for a refused document (:func:`check_node_source`), a node
    that does not exist or a scalar node."""
    check_node_source(doc)
    node = session.node(ref, cancel, doc=doc)
    if node.kind not in ("array", "object"):
        raise UserInputError("gui.filetools.error.node_scalar", path=where or str(getattr(ref, "path", "")))
    return tuple(node.path)


def open_at(doc: Any, record_path: tuple, *, settings: Any = None, where: str = "", progress: Any = None,
            cancel: CancelToken | None = None) -> Document:
    """``doc``'s file opened again with ``record_path`` as the record container and indexed (worker
    threads only; the caller closes it). The file must be the version ``doc`` read
    (``FileChangedError`` otherwise); ``UserInputError("gui.filetools.error.node_missing")`` when the
    reopened file has no container there."""
    doc.ensure_unchanged()
    reopened = Document.open(doc.source_path, record_path=tuple(record_path), settings=settings or doc.settings,
                             cancel=cancel)
    try:
        reopened.build_index(progress=progress, cancel=cancel)
        err = reopened.error
        if err is not None and getattr(err, "code", None) == "target_not_found":
            raise UserInputError("gui.filetools.error.node_missing", path=where or str(record_path))
    except BaseException:
        reopened.close()
        raise
    return reopened


def node_document(session: Any, ref: Any, doc: Any, *, settings: Any = None, where: str | None = None,
                  progress: Any = None, cancel: CancelToken | None = None) -> Document:
    """The file of ``session`` opened again with the array / object node ``ref`` as the record container
    and indexed (worker threads only; the caller closes it): :func:`node_path` + :func:`open_at`. ``doc``:
    the pinned document the node is resolved in. Refused (``UserInputError``) for an edited document, JSON
    Lines / concatenated values / brace-list files, a scalar node, a node the reopened file does not have;
    ``FileChangedError`` for a file changed since it was opened."""
    path = node_path(session, ref, doc, where=where, cancel=cancel)
    return open_at(doc, path, settings=settings, where=where or "", progress=progress, cancel=cancel)


# ==================================================================================
# the split plan (worker thread)
# ==================================================================================
@dataclass
class SplitPlan:
    """The parts a split would write, as the dialog shows them (made by a silent task).

    ``ranges``: half-open selection positions per part; ``names``: the part files; ``sizes``: estimated
    bytes per part; ``engine``: ``"same"`` | ``"export"``; ``fmt``: the parts' format (the document's kind
    for engine A); ``existing``: part files that exist (replaced by the split); ``warnings``: localized;
    ``gen`` / ``view_serial``: the state planned; ``opts``: the options planned (the split uses a copy);
    ``node`` / ``node_path`` / ``where``: a node source -- its ref, its absolute path (the record path the
    split opens the file with) and its location text; ``locked``: the existing parts whose edit lock was
    held when planned (a tab of this program or another window edits them: checked again when the split
    starts); ``name_keys``: ``{normalize_path(part): part}`` (made by the plan task, so the GUI thread looks
    up the open tabs' files without normalizing every part name)."""

    ranges: list[tuple[int, int]]
    names: list[Path]
    sizes: list[int]
    engine: str
    fmt: str
    records: int
    existing: list[Path]
    warnings: list[str]
    gen: int
    source: str
    opts: SplitOptions
    view_serial: int = 0
    node: Any = None
    node_path: tuple | None = None
    where: str = ""
    locked: list[Path] = field(default_factory=list)
    name_keys: dict[str, Path] = field(default_factory=dict)

    @property
    def count(self) -> int:
        return len(self.ranges)

    @property
    def total_size(self) -> int:
        return sum(self.sizes)


@dataclass
class _PlanRequest:
    doc: Any                 # the pinned document (state shown when the plan was requested)
    source: Any              # the view's RecordSet, or None (all records)
    opts: SplitOptions
    source_kind: str
    session_path: str
    settings: Any
    view_serial: int
    gen: int
    session: Any = None      # node plans only (session.node resolves the node)
    node: Any = None
    where: str = ""


def _existing(out_dir: Path, names: list[Path]) -> list[Path]:
    """The part names that exist in ``out_dir`` (one directory listing)."""
    try:
        listing = os.listdir(long_path(out_dir))
    except OSError:
        return []
    keys = {os.path.normcase(n) for n in listing}
    return [p for p in names if os.path.normcase(p.name) in keys]


def _part_sizes(doc: Any, source: Any, ranges: list[tuple[int, int]], cancel: CancelToken | None) -> list[int]:
    """Estimated bytes of every part: the record extents of an unedited document read in file order,
    else the average record size times the records of the part."""
    total = int(doc.record_count)
    if source is None and total and not bool(getattr(doc, "edited", False)):
        span = doc.record_span
        out: list[int] = []
        for k, (a, b) in enumerate(ranges):
            if k & 1023 == 0:
                check(cancel)
            out.append(max(0, int(span(b - 1)[1]) - int(span(a)[0])))
        return out
    if bool(getattr(doc, "edited", False)):
        full = int(doc.effective_bytes())
    else:
        full = max(0, int(doc.data_size) - int(doc.info.data_start))
    avg = full / max(1, total)
    return [int(avg * (b - a)) for a, b in ranges]


def _make_plan(req: _PlanRequest, cancel: CancelToken | None) -> SplitPlan:
    """Plan a split (worker thread): :func:`filetools.plan_split`, the part names
    (:func:`filetools.part_names`: decided by the code the split uses), the folder and source checks, the
    existing parts and the ones another editor holds, the estimated sizes."""
    node_doc = None
    path: tuple | None = None
    try:
        if req.node is not None:
            path = node_path(req.session, req.node, req.doc, where=req.where, cancel=cancel)
            node_doc = open_at(req.doc, path, settings=req.settings, where=req.where, cancel=cancel)
            target, source = node_doc, None
        else:
            target, source = req.doc, req.source
        out_dir = Path(os.fspath(req.opts.out_dir))
        lp = long_path(out_dir)
        if not os.path.isdir(lp):
            if os.path.exists(lp):
                raise UserInputError("gui.filetools.error.not_dir", path=str(out_dir))
            raise UserInputError("filetools.error.no_dir", path=str(out_dir))
        ranges = filetools.plan_split(target, req.opts, source, cancel=cancel)
        engine = _engine(req.opts, source)
        names = filetools.part_names(target, req.opts, source, count=len(ranges))
        check(cancel)
        existing = _existing(out_dir, names)
        sources = (target.source_path, target.data_path, req.session_path)
        locked: list[Path] = []
        for k, p in enumerate(existing):
            if k & 255 == 0:
                check(cancel)
            check_not_source(p, sources)
            if fileio.is_read_only(p):
                raise UserInputError("fileio.error.read_only", path=str(p))
            if _lock_held(p):
                locked.append(p)
        name_keys: dict[str, Path] = {}
        for k, p in enumerate(names):
            if k & 4095 == 0:
                check(cancel)
            name_keys[normalize_path(p)] = p
        sizes = _part_sizes(target, source, ranges, cancel)
        warnings: list[str] = []
        free = fileio.free_space(out_dir)
        need = sum(sizes)
        if free is not None and free < need + filetools.DISK_RESERVE:
            warnings.append(t("gui.filetools.split.plan.space", need=size_text(need), free=size_text(free)))
        return SplitPlan(ranges=ranges, names=names, sizes=sizes, engine=engine,
                         fmt=_parts_fmt(target, req.opts, engine), records=sum(b - a for a, b in ranges),
                         existing=existing, warnings=warnings, gen=req.gen, source=req.source_kind, opts=req.opts,
                         view_serial=req.view_serial, node=req.node, node_path=path, where=req.where,
                         locked=locked, name_keys=name_keys)
    finally:
        if node_doc is not None:
            node_doc.close()


# ==================================================================================
# outcomes and the report window
# ==================================================================================
@dataclass
class SplitOutcome:
    """What a split task returns: the core's :class:`~jsonreshape.filetools.SplitReport`, whether unsaved
    edits were written, what was split (localized)."""

    report: Any
    edited: bool = False
    source: str = ""


@dataclass
class MergeOutcome:
    """What a merge task returns: the core's :class:`~jsonreshape.filetools.MergeReport`, the output's size,
    the names of the open tabs whose unsaved edits were merged."""

    report: Any
    size: int = 0
    edited: list[str] = field(default_factory=list)


class FileToolsReportDialog(QDialog):
    """The report of a finished split / merge (non-modal): summary, inputs (merge), files with sizes,
    warnings; "open the folder", "open the first part" / "open the result" (injected callbacks)."""

    def __init__(self, outcome: SplitOutcome | MergeOutcome, parent: QWidget | None = None, *,
                 open_folder: Callable[[str], Any] | None = None,
                 open_file: Callable[[str], Any] | None = None) -> None:
        super().__init__(parent)
        self.outcome = outcome
        self._open_folder = open_folder or open_local_path
        self._open_file = open_file
        split = self.is_split
        self.setWindowTitle(t("gui.filetools.report.split_title" if split else "gui.filetools.report.merge_title"))
        self.setSizeGripEnabled(True)
        layout = QVBoxLayout(self)
        self.summary_label = QLabel(self.summary_text(), self)
        self.summary_label.setObjectName("filetools_report_summary")
        self.summary_label.setTextFormat(Qt.TextFormat.PlainText)
        self.summary_label.setWordWrap(True)
        self.summary_label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        layout.addWidget(self.summary_label)
        self.inputs_view: QPlainTextEdit | None = None
        if not split:
            inputs = list(getattr(outcome.report, "inputs", []) or [])
            layout.addWidget(QLabel(t("gui.filetools.report.inputs", count=format_count(len(inputs))), self))
            self.inputs_view = self._text_view(self.inputs_text(), min(6, max(2, len(inputs))))
            layout.addWidget(self.inputs_view)
        files = self.files()
        layout.addWidget(QLabel(t("gui.filetools.report.files", count=format_count(len(files))), self))
        self.files_view = self._text_view(self.files_text(), min(8, max(2, len(files))))
        layout.addWidget(self.files_view)
        self.removed_view: QPlainTextEdit | None = None
        removed = self.removed()
        if removed:
            layout.addWidget(QLabel(t("gui.filetools.report.removed", count=format_count(len(removed))), self))
            self.removed_view = self._text_view(self.removed_text(), min(6, max(2, len(removed))))
            self.removed_view.setObjectName("filetools_report_removed")
            layout.addWidget(self.removed_view)
        self.warnings_view: QPlainTextEdit | None = None
        warnings = self.warnings()
        if warnings:
            label = QLabel(t("gui.filetools.report.warnings", count=format_count(len(warnings))), self)
            label.setStyleSheet(f"color: {theme.color('warn').name()};")
            layout.addWidget(label)
            self.warnings_view = QPlainTextEdit(self)
            self.warnings_view.setReadOnly(True)
            self.warnings_view.setPlainText(_plain("\n".join(warnings)))
            layout.addWidget(self.warnings_view, 1)
        buttons = QHBoxLayout()
        self.open_folder_button = QPushButton(t("gui.filetools.report.open_folder"), self)
        self.open_folder_button.setAutoDefault(False)
        self.open_folder_button.setEnabled(bool(files))
        self.open_folder_button.clicked.connect(lambda _checked=False: self.open_folder())
        buttons.addWidget(self.open_folder_button)
        self.open_result_button = QPushButton(
            t("gui.filetools.report.open_first" if split else "gui.filetools.report.open_result"), self)
        self.open_result_button.setAutoDefault(False)
        self.open_result_button.setEnabled(bool(files) and self._open_file is not None)
        self.open_result_button.clicked.connect(lambda _checked=False: self.open_result())
        buttons.addWidget(self.open_result_button)
        buttons.addStretch(1)
        self.close_button = QPushButton(t("gui.common.close"), self)
        self.close_button.setDefault(True)
        self.close_button.clicked.connect(self.accept)
        buttons.addWidget(self.close_button)
        layout.addLayout(buttons)
        self.resize(620, self.sizeHint().height())

    def _text_view(self, text: str, lines: int) -> QPlainTextEdit:
        view = QPlainTextEdit(self)
        view.setReadOnly(True)
        view.setLineWrapMode(QPlainTextEdit.LineWrapMode.NoWrap)
        view.setPlainText(text)
        view.setMaximumHeight(view.fontMetrics().lineSpacing() * lines + 16)
        return view

    @property
    def is_split(self) -> bool:
        return isinstance(self.outcome, SplitOutcome)

    def files(self) -> list[Path]:
        report = self.outcome.report
        if self.is_split:
            return [Path(p) for p in report.files]
        return [Path(p) for p in report.report.files]

    def sizes(self) -> list[int]:
        if self.is_split:
            return [int(p.bytes) for p in self.outcome.report.parts]
        return [int(self.outcome.size)]

    def removed(self) -> list[Path]:
        """The old parts of an earlier split that the split deleted (``SplitReport.removed``,
        ``SplitOptions.remove_stale``); empty for a merge."""
        if not self.is_split:
            return []
        return [Path(p) for p in (getattr(self.outcome.report, "removed", None) or [])]

    def removed_text(self) -> str:
        files = self.removed()
        lines = [str(p) for p in files[:REPORT_FILES_MAX]]
        if len(files) > REPORT_FILES_MAX:
            lines.append(t("gui.filetools.report.more_files", count=format_count(len(files) - REPORT_FILES_MAX)))
        return _plain("\n".join(lines))

    def warnings(self) -> list[str]:
        return [str(w) for w in (self.outcome.report.warnings or [])]

    def summary_text(self) -> str:
        outcome = self.outcome
        report = outcome.report
        lines: list[str] = []
        if isinstance(outcome, SplitOutcome):
            lines.append(t("gui.filetools.report.split", parts=format_count(len(report.parts)),
                           records=format_count(report.records), size=size_text(report.bytes_written),
                           seconds=format_duration(report.elapsed), rate=f"{report.mb_per_s:.1f}"))
            if outcome.source:
                lines.append(t("gui.filetools.report.source", source=outcome.source))
            if report.engine == "same":
                lines.append(t("gui.filetools.report.engine.same"))
            else:
                lines.append(t("gui.filetools.report.engine.export", fmt=_format_text(report.fmt)))
            if outcome.edited:
                lines.append(t("gui.filetools.report.edited"))
        else:
            inner = report.report
            lines.append(t("gui.filetools.report.merge", records=format_count(report.records),
                           size=size_text(outcome.size or inner.bytes_written), seconds=format_duration(report.elapsed),
                           rate=f"{inner.mb_per_s:.1f}"))
            if report.duplicates or report.groups:
                lines.append(t("gui.filetools.report.dupes", duplicates=format_count(report.duplicates),
                               groups=format_count(report.groups)))
            if outcome.edited:
                lines.append(t("gui.filetools.report.edited_tabs", names=", ".join(outcome.edited)))
        return _plain("\n".join(lines))

    def inputs_text(self) -> str:
        lines = []
        for path, kind, records, written in getattr(self.outcome.report, "inputs", []) or []:
            lines.append(t("gui.filetools.report.input_line", name=Path(path).name, kind=_kind_text(kind),
                           records=format_count(records), written=format_count(written)))
        return _plain("\n".join(lines))

    def files_text(self) -> str:
        files = self.files()
        sizes = self.sizes()
        lines = []
        for i, p in enumerate(files[:REPORT_FILES_MAX]):
            size = sizes[i] if i < len(sizes) else None
            lines.append(f"{p}  —  {size_text(size)}" if size is not None else str(p))
        if len(files) > REPORT_FILES_MAX:
            lines.append(t("gui.filetools.report.more_files", count=format_count(len(files) - REPORT_FILES_MAX)))
        return _plain("\n".join(lines))

    def report_text(self) -> str:
        """Everything the report says, as plain text."""
        parts = [self.summary_text()]
        if not self.is_split:
            parts.append(self.inputs_text())
        parts.append(self.files_text())
        removed = self.removed()
        if removed:
            parts.append(t("gui.filetools.report.removed", count=format_count(len(removed))))
            parts.append(self.removed_text())
        parts.extend(self.warnings())
        return "\n".join(parts)

    def folder(self) -> str | None:
        files = self.files()
        return str(files[0].parent) if files else None

    def result_path(self) -> str | None:
        files = self.files()
        return str(files[0]) if files else None

    def open_folder(self) -> Any:
        folder = self.folder()
        return self._open_folder(folder) if folder else None

    def open_result(self) -> Any:
        path = self.result_path()
        if path is None or self._open_file is None:
            return None
        return self._open_file(path)


# ==================================================================================
# the runner (outlives the dialogs)
# ==================================================================================
@dataclass(eq=False)
class FileToolsJob:
    """One split / merge started through :class:`FileToolsRunner`."""

    tool: str                                     # "split" | "merge"
    path: str                                     # the output folder (split) / file (merge)
    sessions: list = field(default_factory=list)  # sessions whose documents the job reads
    task: Any = None
    state: str = "running"                        # running | done | failed | cancelled
    outcome: Any = None
    error: BaseException | None = None
    parents: list = field(default_factory=list)   # report window parent candidates (first alive & visible)
    report_dialog: Any = None
    open_folder: Callable[[str], Any] | None = None
    open_file: Callable[[str], Any] | None = None
    reload: list = field(default_factory=list)    # weak refs: tabs showing an output file (reopened when done)


class FileToolsRunner(QObject):
    """Runs split / merge tasks and reports their outcome independently of the dialog that started them
    (one per application: :func:`filetools_runner`). Signals ``job_started(job)``, ``job_finished(job)``
    (``job.state`` done | failed | cancelled; emitted after the report window is shown / the error is
    reported). A refusal the user resolves (``filetools.error.part_open_edited``: a part or the output has
    unsaved edits in this program) is told in the status bar and in the dialog, not in an error window."""

    job_started = Signal(object)
    job_finished = Signal(object)

    def __init__(self, app: Any, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self.app = app
        self._jobs: list[FileToolsJob] = []

    def jobs(self, session: Any = None) -> list[FileToolsJob]:
        """Running jobs (reading a document of ``session``)."""
        return [job for job in self._jobs if session is None or any(s is session for s in job.sessions)]

    def start(self, work: Callable[[Any], Any], *, tool: str, title: str, path: str, holds: Iterable = (),
              parents: Iterable = (), open_folder: Callable[[str], Any] | None = None,
              open_file: Callable[[str], Any] | None = None, reload: Iterable = ()) -> FileToolsJob:
        """Run ``work(ctx) -> SplitOutcome | MergeOutcome`` as a heavy task. ``holds``: ``(session, record
        sets, pinned document)`` triples the task reads -- each session keeps them open until the task
        ended (``keep_document_for``), also while it waits in the heavy-task queue. ``reload``: weak
        references of the tabs that show an output file (:func:`~jsonreshape.gui.searchpanel.open_target_check`),
        opened again when the job is done."""
        holds = [(s, tuple(sets or ()), doc) for s, sets, doc in holds]
        job = FileToolsJob(tool=tool, path=path, sessions=[s for s, _sets, _doc in holds], parents=list(parents),
                           open_folder=open_folder, open_file=open_file, reload=list(reload or ()))
        job.task = self.app.tasks.run(work, title=title, kind=tool, owner=job, heavy=True,
                                      on_done=lambda outcome, j=job: self._done(j, outcome),
                                      on_error=lambda exc, j=job: self._failed(j, exc),
                                      on_cancel=lambda j=job: self._cancelled(j))
        for session, sets, doc in holds:
            if _session_ok(session):
                keep_for(session, job.task, sets=tuple(rs for rs in sets if isinstance(rs, RecordSet)), doc=doc)
        self._jobs.append(job)
        self.job_started.emit(job)
        return job

    def cancel_all(self, session: Any = None) -> int:
        """Cancel the running jobs (reading ``session``); the number cancelled."""
        jobs = self.jobs(session)
        for job in jobs:
            job.task.cancel()
        return len(jobs)

    def _parent_for(self, job: FileToolsJob) -> Any:
        alive = [w for w in job.parents if _alive(w)]
        for widget in alive:
            if widget.isVisible():
                return widget
        return alive[-1] if alive else None

    def _end(self, job: FileToolsJob, state: str) -> None:
        job.state = state
        if job in self._jobs:
            self._jobs.remove(job)

    def _status(self, text: str) -> None:
        show = getattr(self.app, "show_status", None)
        if callable(show):
            show(text, 8000)

    def _done(self, job: FileToolsJob, outcome: Any) -> None:
        self._end(job, "done")
        job.outcome = outcome
        reload, job.reload = job.reload, []
        reload_tabs(reload)          # tabs that showed a replaced output file open the new one (review S3)
        try:
            job.report_dialog = self.show_report(job)
        except Exception as exc:  # pragma: no cover - defensive: the outcome is still told
            log.exception("showing the file tools report failed")
            self.app.report_error(exc)
        if isinstance(outcome, SplitOutcome):
            rep = outcome.report
            self._status(t("gui.filetools.status.split_done_path", parts=format_count(len(rep.parts)),
                           size=size_text(rep.bytes_written), path=job.path))
        elif isinstance(outcome, MergeOutcome):
            self._status(t("gui.filetools.status.merge_done_path", records=format_count(outcome.report.records),
                           size=size_text(outcome.size), path=job.path))
        self.job_finished.emit(job)

    def _failed(self, job: FileToolsJob, exc: BaseException) -> None:
        self._end(job, "failed")
        job.error = exc
        job.reload = []
        self.job_finished.emit(job)
        if isinstance(exc, Cancelled):
            return
        if _user_error(exc):
            # the user's to resolve (save / discard the edits of that file, or choose other names): the status
            # bar and the dialog say so, no error window (fix round R5 decision 12 seen from the GUI)
            log.info("%s refused: %s", job.tool, exc)
            self._status(t("gui.filetools.status.refused", error=_plain(getattr(exc, "message", str(exc)))))
            return
        self.app.report_error(exc, self._parent_for(job))

    def _cancelled(self, job: FileToolsJob) -> None:
        self._end(job, "cancelled")
        self._status(t("gui.filetools.status.cancelled_path", path=job.path))
        self.job_finished.emit(job)

    def show_report(self, job: FileToolsJob) -> FileToolsReportDialog:
        """The report window of a finished job (non-modal)."""
        parent = self._parent_for(job)
        dialog = FileToolsReportDialog(job.outcome, parent if _alive(parent) else None,
                                       open_folder=job.open_folder, open_file=job.open_file)
        keep_alive(dialog)
        dialog.show()
        return dialog


def filetools_runner(app: Any) -> FileToolsRunner:
    """The application's :class:`FileToolsRunner` (created on first use, a child of ``app``)."""
    runner = getattr(app, "_filetools_runner", None)
    if runner is None or not _alive(runner):
        runner = FileToolsRunner(app, app if isinstance(app, QObject) else None)
        try:
            app._filetools_runner = runner
        except AttributeError:  # pragma: no cover - a context that takes no attributes
            pass
    return runner


# ==================================================================================
# shared dialog parts
# ==================================================================================
class _ToolDialog(QDialog):
    """What both dialogs share: the runner job and its progress, the message labels, the buttons."""

    def __init__(self, app: Any, parent: QWidget | None, *, open_folder: Callable[[str], Any] | None,
                 open_file: Callable[[str], Any] | None) -> None:
        super().__init__(parent)
        self.app = app
        self.runner = filetools_runner(app)
        self.job: FileToolsJob | None = None
        self.task: Any = None
        self.report_dialog: FileToolsReportDialog | None = None
        self.outcome: Any = None
        self.error: BaseException | None = None
        self._closing = False
        self._queued_shown = False
        self._connections: list[tuple[Any, Any, Any]] = []      # (tag, signal, slot)
        self._open_folder = open_folder or open_local_path
        self._open_file = open_file or _default_open_file(app)
        self.setModal(False)
        self.setWindowModality(Qt.WindowModality.NonModal)
        self.setSizeGripEnabled(True)
        self._connect(None, self.runner.job_finished, self._job_finished)
        self._connect(None, app.tasks.task_updated, self._on_task_updated)
        self._connect(None, theme.notifier().theme_changed, self._style_labels)

    # -- connections -----------------------------------------------------------------------
    def _connect(self, tag: Any, signal: Any, slot: Any) -> None:
        try:
            signal.connect(slot)
        except (RuntimeError, TypeError):  # pragma: no cover - a fake / deleted sender
            return
        self._connections.append((tag, signal, slot))

    def _disconnect(self, tag: Any = None, *, every: bool = False) -> None:
        keep = []
        for entry in self._connections:
            entry_tag, signal, slot = entry
            if every or entry_tag is tag:
                try:
                    signal.disconnect(slot)
                except (RuntimeError, TypeError):
                    pass
            else:
                keep.append(entry)
        self._connections = keep

    # -- widgets -----------------------------------------------------------------------------
    def _add_messages(self, layout: QVBoxLayout) -> None:
        self.error_label = QLabel(self)
        self.error_label.setObjectName("filetools_error")
        self.warning_label = QLabel(self)
        self.warning_label.setObjectName("filetools_warning")
        self.note_label = QLabel(self)
        self.note_label.setObjectName("filetools_note")
        for label in (self.error_label, self.warning_label, self.note_label):
            label.setWordWrap(True)
            label.setTextFormat(Qt.TextFormat.PlainText)
            label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
            label.setVisible(False)
            layout.addWidget(label)
        self.progress_bar = QProgressBar(self)
        self.progress_bar.setRange(0, 1000)
        self.progress_bar.setVisible(False)
        layout.addWidget(self.progress_bar)
        self.status_label = QLabel(self)
        self.status_label.setObjectName("filetools_status")
        self.status_label.setWordWrap(True)
        self.status_label.setTextFormat(Qt.TextFormat.PlainText)
        layout.addWidget(self.status_label)

    def _add_buttons(self, layout: QVBoxLayout, run_text: str, run: Callable[[], Any]) -> None:
        row = QHBoxLayout()
        self.stop_button = QPushButton(t("gui.filetools.stop"), self)
        self.stop_button.setToolTip(t("gui.filetools.stop.tip"))
        self.stop_button.setAutoDefault(False)
        self.stop_button.setVisible(False)
        self.stop_button.clicked.connect(lambda _checked=False: self.cancel_task())
        row.addWidget(self.stop_button)
        row.addStretch(1)
        self.run_button = QPushButton(run_text, self)
        self.run_button.setDefault(True)
        self.run_button.clicked.connect(lambda _checked=False: run())
        row.addWidget(self.run_button)
        self.close_button = QPushButton(t("gui.common.close"), self)
        self.close_button.setAutoDefault(False)
        self.close_button.setToolTip(t("gui.filetools.close.tip"))
        self.close_button.clicked.connect(self.reject)
        row.addWidget(self.close_button)
        layout.addLayout(row)

    def _style_labels(self) -> None:
        self.error_label.setStyleSheet(f"color: {theme.color('danger').name()};")
        self.warning_label.setStyleSheet(f"color: {theme.color('warn').name()};")
        self.note_label.setStyleSheet(f"color: {theme.color('muted').name()};")

    def _set_messages(self, errors: list[str], warnings: list[str], notes: list[str] | None = None) -> None:
        for label, texts in ((self.error_label, errors), (self.warning_label, warnings),
                             (self.note_label, notes or [])):
            label.setText(_plain("\n".join(texts)))
            label.setVisible(bool(texts))

    def error_text(self) -> str:
        return self.error_label.text() if not self.error_label.isHidden() else ""

    def warning_text(self) -> str:
        return self.warning_label.text() if not self.warning_label.isHidden() else ""

    def note_text(self) -> str:
        return self.note_label.text() if not self.note_label.isHidden() else ""

    # -- the running job ------------------------------------------------------------------------
    @property
    def running(self) -> bool:
        """The job started with this dialog has not ended."""
        return self.task is not None and self.task.state == "running"

    @property
    def queued(self) -> bool:
        """The job waits for its turn in the heavy-task queue."""
        return self.running and bool(getattr(self.task, "queued", False))

    def cancel_task(self) -> None:
        """Stop the running job (nothing is written: the outputs are committed all or nothing)."""
        if self.running:
            self.task.cancel()

    def _show_running(self) -> None:
        self._queued_shown = False
        self.progress_bar.setRange(0, 0)
        self.progress_bar.setVisible(True)
        self.stop_button.setVisible(True)
        self.status_label.setText(t("gui.filetools.status.running"))
        self._show_task_state()
        self._update_run_button()

    def _show_task_state(self) -> None:
        task = self.task
        if task is None or task.state != "running":
            return
        if getattr(task, "queued", False):
            self._queued_shown = True
            self.progress_bar.setRange(0, 0)
            self.status_label.setText(t("gui.filetools.status.queued"))
            return
        if self._queued_shown:
            self.status_label.setText(t("gui.filetools.status.running"))
        self._queued_shown = False
        info = task.info()
        if info is None or info.fraction is None:
            self.progress_bar.setRange(0, 0)
        else:
            self.progress_bar.setRange(0, 1000)
            self.progress_bar.setValue(int(max(0.0, min(1.0, info.fraction)) * 1000))
        if info is not None and info.message:
            self.status_label.setText(_plain(info.message))

    def _on_task_updated(self, task: Any) -> None:
        if task is self.task and not self._closing:
            self._show_task_state()

    def _finish_ui(self) -> None:
        self.progress_bar.setVisible(False)
        self.stop_button.setVisible(False)
        self._update_run_button()

    def _job_finished(self, job: FileToolsJob) -> None:
        """The runner finished a job (it already showed the report / reported the error)."""
        if job is not self.job or self._closing:
            return
        self.task = None
        if job.state == "done":
            self.outcome = job.outcome
            self.report_dialog = job.report_dialog
            self._finish_ui()
            self._job_done(job)
        elif job.state == "failed" and job.error is not None:
            self.error = job.error
            self._finish_ui()
            key = "gui.filetools.status.refused" if _user_error(job.error) else "gui.filetools.status.failed"
            self.status_label.setText(t(key, error=_plain(error_text(job.error))))
            self._job_failed(job)
        else:
            self._finish_ui()
            self.status_label.setText(t("gui.filetools.status.cancelled"))
            self._job_cancelled(job)

    def _job_done(self, job: FileToolsJob) -> None:  # pragma: no cover - overridden
        pass

    def _job_failed(self, job: FileToolsJob) -> None:  # pragma: no cover - overridden
        pass

    def _job_cancelled(self, job: FileToolsJob) -> None:
        pass

    def _update_run_button(self) -> None:  # pragma: no cover - overridden
        pass

    def _cleanup(self) -> None:
        """Closing: stop the timers and the helper tasks (the job goes on: the runner reports it)."""

    def done(self, result: int) -> None:  # noqa: D401 - Qt name
        """Close: the helper tasks (plan, probes) are cancelled and forgotten; a running split / merge goes
        on (tasks panel) and its outcome is still reported by the runner."""
        if not self._closing:
            self._closing = True
            try:
                self._cleanup()
            finally:
                try:
                    self.app.tasks.detach(self)
                except Exception:  # pragma: no cover
                    log.debug("detach failed", exc_info=True)
                self._disconnect(every=True)
        super().done(result)


def _indent_combo(parent: QWidget, settings: Any) -> QComboBox:
    combo = QComboBox(parent)
    for key, value in _INDENTS:
        combo.addItem(t(f"gui.filetools.indent.{key}"), value)
    default = getattr(settings, "export_indent", 2)
    _select_data(combo, default, label=t("gui.filetools.indent.n", n=default))
    return combo


def _indent_ok(value: Any) -> bool:
    """A remembered indentation the writer accepts (``None``, a tab, 0..16)."""
    return value is None or value == chr(9) or (isinstance(value, int) and not isinstance(value, bool)
                                                and 0 <= value <= 16)


def _select_data(combo: QComboBox, value: Any, label: str | None = None) -> bool:
    for i in range(combo.count()):
        data = combo.itemData(i)
        if data == value and type(data) is type(value):
            combo.setCurrentIndex(i)
            return True
    if label is not None:
        combo.addItem(label, value)
        combo.setCurrentIndex(combo.count() - 1)
        return True
    return False


# ==================================================================================
# SplitDialog
# ==================================================================================
_SPLIT_OPEN: weakref.WeakKeyDictionary[Any, SplitDialog] = weakref.WeakKeyDictionary()


class SplitDialog(_ToolDialog):
    """Split a session's document into parts (module docstring "SplitDialog").

    ``source``: ``"document"``, ``"view"``, a :class:`~jsonreshape.gui.session.NodeRef` (an array / object
    node) or ``None`` (the view when it is filtered / sorted, else the document); ``ask_folder(parent,
    title, start) -> str | None``: the folder chooser; ``open_folder(path)`` / ``open_file(path)``: the
    report's "open the folder" / "open the first part" (defaults: the system file manager / a new tab).
    Signals: ``plan_ready(SplitPlan)``, ``plan_failed(exc)``, ``split_started(task)``,
    ``split_finished(SplitOutcome)``, ``split_failed(exc)``."""

    plan_ready = Signal(object)
    plan_failed = Signal(object)
    split_started = Signal(object)
    split_finished = Signal(object)
    split_failed = Signal(object)

    def __init__(self, session: Any, parent: QWidget | None = None, *, source: Any = None,
                 ask_folder: Callable[[QWidget | None, str, str], str | None] | None = None,
                 open_folder: Callable[[str], Any] | None = None,
                 open_file: Callable[[str], Any] | None = None) -> None:
        super().__init__(session.app, parent, open_folder=open_folder, open_file=open_file)
        self.session = session
        self.node_ref: Any = source if source is not None and not isinstance(source, str) else None
        self._ask_folder = ask_folder or _ask_folder
        self.plan: SplitPlan | None = None
        self.plan_error: str | None = None
        self.plan_task: Any = None
        self._plan_serial = 0
        self._plan_pending = False
        self._plan_stale = False
        self._local_errors: list[str] = []
        self._start_errors: list[str] = []       # why the last "Bo'lish" was refused (until the inputs change)
        self._syncing = False
        self._wrapper_touched = False
        self._plan_timer = QTimer(self)
        self._plan_timer.setSingleShot(True)
        self._plan_timer.setInterval(PLAN_DELAY_MS)
        self._plan_timer.timeout.connect(self._start_plan)
        self.setWindowTitle(t("gui.filetools.split.title", name=session.name))
        self._build()
        self._style_labels()
        self._syncing = True
        try:
            self._restore_last()
            self.folder_edit.setText(os.path.dirname(os.path.abspath(session.path)))
            self.set_source(source if source is not None else self._default_source(), replan=False)
        finally:
            self._syncing = False
        for name, slot in (("view_changed", self._on_view_changed), ("count_changed", self._on_counts),
                           ("view_busy_changed", self._on_view_busy), ("state_changed", self._on_session_state),
                           ("data_changed", self._on_data_changed), ("index_finished", self._on_index_finished)):
            signal = getattr(session, name, None)
            if signal is not None:
                self._connect(session, signal, slot)
        try:
            _SPLIT_OPEN[session] = self
        except TypeError:  # pragma: no cover - a session that cannot be weakly referenced
            pass
        self._update_texts()
        self._inputs_changed()
        self.resize(680, 720)

    # ------------------------------------------------------------------------------- registry
    @classmethod
    def instance(cls, session: Any) -> SplitDialog | None:
        """The open split dialog of ``session`` (``None`` when there is none)."""
        try:
            dlg = _SPLIT_OPEN.get(session)
        except (TypeError, RuntimeError):  # pragma: no cover
            return None
        return dlg if dlg is not None and _alive(dlg) and not dlg._closing else None

    def _default_source(self) -> str:
        session = self.session
        if _session_ok(session) and session.doc is not None and not session.view_is_base:
            return "view"
        return "document"

    # ------------------------------------------------------------------------------- build
    def _build(self) -> None:
        layout = QVBoxLayout(self)

        self.source_box = QGroupBox(t("gui.filetools.split.source.title"), self)
        source_layout = QVBoxLayout(self.source_box)
        self.source_group = QButtonGroup(self)
        self.source_buttons: dict[str, QRadioButton] = {}
        for i, key in enumerate(SOURCES):
            button = QRadioButton(self.source_box)
            button.setObjectName(f"split_source_{key}")
            self.source_group.addButton(button, i)
            self.source_buttons[key] = button
            source_layout.addWidget(button)
        self.source_buttons["node"].setToolTip(t("gui.filetools.split.source.node.tip"))
        self.source_buttons["node"].setVisible(self.node_ref is not None)
        self.source_group.idToggled.connect(self._on_source_toggled)
        layout.addWidget(self.source_box)

        self.mode_box = QGroupBox(t("gui.filetools.split.mode.title"), self)
        grid = QGridLayout(self.mode_box)
        self.mode_group = QButtonGroup(self)
        self.mode_buttons: dict[str, QRadioButton] = {}
        for i, key in enumerate(MODES):
            button = QRadioButton(t(f"gui.filetools.split.mode.{key}"), self.mode_box)
            button.setObjectName(f"split_mode_{key}")
            self.mode_group.addButton(button, i)
            self.mode_buttons[key] = button
            grid.addWidget(button, i, 0)
        self.parts_spin = QSpinBox(self.mode_box)
        self.parts_spin.setObjectName("split_parts")
        self.parts_spin.setRange(1, filetools.MAX_PARTS)
        self.parts_spin.setGroupSeparatorShown(True)
        self.parts_spin.setValue(DEFAULT_PARTS)
        self.balance_combo = QComboBox(self.mode_box)
        self.balance_combo.addItem(t("gui.filetools.split.balance.bytes"), "bytes")
        self.balance_combo.addItem(t("gui.filetools.split.balance.records"), "records")
        self.balance_combo.setToolTip(t("gui.filetools.split.balance.tip"))
        row = QHBoxLayout()
        row.addWidget(self.parts_spin)
        row.addWidget(self.balance_combo)
        row.addStretch(1)
        grid.addLayout(row, 0, 1)
        self.records_spin = QSpinBox(self.mode_box)
        self.records_spin.setObjectName("split_records")
        self.records_spin.setRange(1, RECORDS_MAX)
        self.records_spin.setGroupSeparatorShown(True)
        self.records_spin.setValue(DEFAULT_RECORDS)
        grid.addWidget(self.records_spin, 1, 1)
        self.size_edit = QLineEdit(self.mode_box)
        self.size_edit.setObjectName("split_size")
        self.size_edit.setPlaceholderText(t("gui.filetools.split.size.placeholder"))
        self.size_edit.setToolTip(t("gui.filetools.split.size.tip"))
        self.size_edit.setText(DEFAULT_SIZE_TEXT)
        grid.addWidget(self.size_edit, 2, 1)
        grid.setColumnStretch(1, 1)
        self.mode_group.idToggled.connect(self._on_mode_toggled)
        self.parts_spin.valueChanged.connect(self._inputs_changed)
        self.balance_combo.currentIndexChanged.connect(self._inputs_changed)
        self.records_spin.valueChanged.connect(self._inputs_changed)
        self.size_edit.textChanged.connect(self._inputs_changed)
        layout.addWidget(self.mode_box)

        self.output_box = QGroupBox(t("gui.filetools.split.output.title"), self)
        form = QFormLayout(self.output_box)
        row = QHBoxLayout()
        self.folder_edit = QLineEdit(self.output_box)
        self.folder_edit.setObjectName("split_folder")
        self.folder_edit.textChanged.connect(self._inputs_changed)
        row.addWidget(self.folder_edit, 1)
        self.browse_button = QPushButton(t("gui.filetools.choose"), self.output_box)
        self.browse_button.setAutoDefault(False)
        self.browse_button.clicked.connect(lambda _checked=False: self.choose_folder())
        row.addWidget(self.browse_button)
        folder_label = QLabel(t("gui.filetools.split.folder"), self.output_box)
        folder_label.setBuddy(self.folder_edit)
        form.addRow(folder_label, row)
        self.name_edit = QLineEdit(self.output_box)
        self.name_edit.setObjectName("split_name")
        self.name_edit.setText(filetools.DEFAULT_NAME)
        self.name_edit.setToolTip(t("gui.filetools.split.name.tip"))
        self.name_edit.textChanged.connect(self._inputs_changed)
        form.addRow(t("gui.filetools.split.name"), self.name_edit)
        self.format_combo = QComboBox(self.output_box)
        self.format_combo.setObjectName("split_format")
        for fmt in SPLIT_FORMATS:
            self.format_combo.addItem(_format_text(fmt), fmt)
        self.format_combo.setItemData(0, t("gui.filetools.format.same.tip"), Qt.ItemDataRole.ToolTipRole)
        self.format_combo.currentIndexChanged.connect(self._inputs_changed)
        form.addRow(t("gui.filetools.split.format"), self.format_combo)
        self.indent_combo = _indent_combo(self.output_box, self.app.settings)
        self.indent_combo.currentIndexChanged.connect(self._inputs_changed)
        form.addRow(t("gui.filetools.indent"), self.indent_combo)
        self.keep_wrapper_check = QCheckBox(t("gui.filetools.split.keep_wrapper"), self.output_box)
        self.keep_wrapper_check.setObjectName("split_keep_wrapper")
        self.keep_wrapper_check.setChecked(True)
        self.keep_wrapper_check.toggled.connect(self._on_wrapper_toggled)
        form.addRow("", self.keep_wrapper_check)
        self.drop_broken_check = QCheckBox(t("gui.filetools.split.drop_broken"), self.output_box)
        self.drop_broken_check.setObjectName("split_drop_broken")
        self.drop_broken_check.toggled.connect(self._inputs_changed)
        form.addRow("", self.drop_broken_check)
        # deletes files: off by default and never remembered between dialogs (SplitOptions.remove_stale)
        self.remove_stale_check = QCheckBox(t("gui.filetools.split.remove_stale"), self.output_box)
        self.remove_stale_check.setObjectName("split_remove_stale")
        self.remove_stale_check.setToolTip(t("gui.filetools.split.remove_stale.tip"))
        self.remove_stale_check.setChecked(False)
        self.remove_stale_check.toggled.connect(self._inputs_changed)
        form.addRow("", self.remove_stale_check)
        self.output_form = form
        layout.addWidget(self.output_box)

        self.plan_box = QGroupBox(t("gui.filetools.split.plan.title"), self)
        plan_layout = QVBoxLayout(self.plan_box)
        self.plan_label = QLabel(self.plan_box)
        self.plan_label.setObjectName("split_plan_summary")
        self.plan_label.setWordWrap(True)
        self.plan_label.setTextFormat(Qt.TextFormat.PlainText)
        plan_layout.addWidget(self.plan_label)
        self.plan_tree = QTreeWidget(self.plan_box)
        self.plan_tree.setObjectName("split_plan")
        self.plan_tree.setColumnCount(5)
        self.plan_tree.setHeaderLabels([t("gui.filetools.split.plan.col.n"), t("gui.filetools.split.plan.col.name"),
                                        t("gui.filetools.split.plan.col.range"),
                                        t("gui.filetools.split.plan.col.records"),
                                        t("gui.filetools.split.plan.col.size")])
        self.plan_tree.headerItem().setToolTip(2, t("gui.filetools.split.plan.col.range.tip"))
        self.plan_tree.setRootIsDecorated(False)
        self.plan_tree.setUniformRowHeights(True)
        self.plan_tree.setAlternatingRowColors(True)
        self.plan_tree.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        header = self.plan_tree.header()
        header.setStretchLastSection(False)
        header.setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        for col in (0, 2, 3, 4):
            header.setSectionResizeMode(col, QHeaderView.ResizeMode.ResizeToContents)
        plan_layout.addWidget(self.plan_tree, 1)
        layout.addWidget(self.plan_box, 1)

        self._add_messages(layout)
        self._add_buttons(layout, t("gui.filetools.split.run"), self.start_split)

    # ------------------------------------------------------------------------------- state
    def _restore_last(self) -> None:
        last = self.app.state.get(STATE_SPLIT)
        last = last if isinstance(last, dict) else {}
        mode = last.get("mode") if last.get("mode") in MODES else "parts"
        self.mode_buttons[mode].setChecked(True)
        for spin, key in ((self.parts_spin, "parts"), (self.records_spin, "records")):
            value = last.get(key)
            if isinstance(value, int) and not isinstance(value, bool) and spin.minimum() <= value <= spin.maximum():
                spin.setValue(value)
        if isinstance(last.get("size"), str) and last["size"].strip():
            self.size_edit.setText(last["size"])
        if last.get("balance") in filetools.BALANCES:
            _select_data(self.balance_combo, last["balance"])
        if last.get("fmt") in SPLIT_FORMATS:
            _select_data(self.format_combo, last["fmt"])
        if "indent" in last and _indent_ok(last["indent"]):
            _select_data(self.indent_combo, last["indent"])
        if isinstance(last.get("name"), str) and last["name"].strip():
            self.name_edit.setText(last["name"])
        self._update_widgets()

    def _remember(self) -> None:
        data = {"mode": self.mode(), "parts": self.parts_spin.value(), "records": self.records_spin.value(),
                "size": self.size_edit.text().strip(), "balance": self.balance_combo.currentData(),
                "fmt": self.format(), "indent": self.indent_combo.currentData(), "name": self.name_edit.text().strip()}
        try:
            self.app.state.set(STATE_SPLIT, data)
        except (TypeError, ValueError):  # pragma: no cover - plain JSON values
            return
        save = getattr(self.app, "save_state", None)
        if callable(save):
            save()

    # ------------------------------------------------------------------------------- accessors
    def source(self) -> str:
        """``"document"`` | ``"view"`` | ``"node"``."""
        for key, button in self.source_buttons.items():
            if button.isChecked():
                return key
        return "document"

    def set_source(self, source: Any, *, replan: bool = True) -> None:
        """Choose ``"document"`` / ``"view"`` / a node (a NodeRef: it becomes the node source)."""
        if source is not None and not isinstance(source, str):
            self.node_ref = source
            self.source_buttons["node"].setVisible(True)
            key = "node"
        else:
            key = source if source in SOURCES else "document"
            if key == "node" and self.node_ref is None:
                key = "document"
        self._update_texts()
        self.source_buttons[key].setChecked(True)
        if replan:
            self._inputs_changed()

    def mode(self) -> str:
        """``"parts"`` | ``"records"`` | ``"size"``."""
        for key, button in self.mode_buttons.items():
            if button.isChecked():
                return key
        return "parts"

    def set_mode(self, mode: str) -> None:
        if mode in MODES:
            self.mode_buttons[mode].setChecked(True)

    def set_parts(self, n: int, balance: str | None = None) -> None:
        self.set_mode("parts")
        self.parts_spin.setValue(int(n))
        if balance is not None:
            _select_data(self.balance_combo, balance)

    def set_records(self, n: int) -> None:
        self.set_mode("records")
        self.records_spin.setValue(int(n))

    def set_size_text(self, text: str) -> None:
        self.set_mode("size")
        self.size_edit.setText(text)

    def format(self) -> str:
        """``"same"`` (the byte copy in the document's own format) or a writer format."""
        return str(self.format_combo.currentData() or "same")

    def set_format(self, fmt: str) -> None:
        _select_data(self.format_combo, fmt)

    def out_dir(self) -> str:
        """The absolute output folder (a relative text is taken relative to the file's folder)."""
        text = os.path.expanduser(self.folder_edit.text().strip())
        if not text:
            return ""
        if not os.path.isabs(text):
            text = os.path.join(os.path.dirname(os.path.abspath(self.session.path)), text)
        return os.path.abspath(text)

    def set_out_dir(self, path: str | os.PathLike[str]) -> None:
        self.folder_edit.setText(os.fspath(path))

    def set_name_pattern(self, pattern: str) -> None:
        self.name_edit.setText(pattern)

    def split_options(self) -> SplitOptions:
        """The options described by the dialog (``UserInputError`` when invalid)."""
        mode = self.mode()
        kw: dict[str, Any] = {}
        if mode == "parts":
            kw["parts"] = self.parts_spin.value()
            kw["balance"] = self.balance_combo.currentData() or "bytes"
        elif mode == "records":
            kw["records"] = self.records_spin.value()
        else:
            kw["size"] = parse_part_size(self.size_edit.text())
        folder = self.out_dir()
        if not folder:
            raise UserInputError("gui.filetools.error.need_folder")
        keep = self.keep_wrapper_check.isChecked()
        fmt = self.format()
        export = None
        if fmt != "same":
            ekw: dict[str, Any] = {"keep_wrapper": keep}
            if fmt == "json":
                ekw["indent"] = self.indent_combo.currentData()
            export = ExportOptions.from_settings(fmt, self.app.settings, **ekw)
        name = self.name_edit.text().strip() or None
        return SplitOptions(out_dir=folder, name=name, keep_wrapper=keep,
                            drop_broken=self.drop_broken_check.isEnabled() and self.drop_broken_check.isChecked(),
                            export=export, remove_stale=self.remove_stale_check.isChecked(), **kw)

    @property
    def plan_pending(self) -> bool:
        """A plan is scheduled or being computed."""
        return self._plan_pending

    # ------------------------------------------------------------------------------- reactions
    def _on_source_toggled(self, _id: int, checked: bool) -> None:
        if not checked:
            return
        if self.source() == "node" and not self._wrapper_touched:
            self._syncing_wrapper(False)       # a node: its elements alone, unless asked otherwise
        elif self.source() != "node" and not self._wrapper_touched:
            self._syncing_wrapper(True)
        self._inputs_changed()

    def _syncing_wrapper(self, value: bool) -> None:
        old = self._syncing
        self._syncing = True
        try:
            self.keep_wrapper_check.setChecked(value)
        finally:
            self._syncing = old

    def _on_wrapper_toggled(self, _on: bool) -> None:
        if not self._syncing:
            self._wrapper_touched = True
        self._inputs_changed()

    def _on_mode_toggled(self, _id: int, checked: bool) -> None:
        if checked:
            self._inputs_changed()

    def _on_view_changed(self) -> None:
        self._update_texts()
        if self.source() == "view":
            self._invalidate(stale=True)
            self._schedule()

    def _on_counts(self) -> None:
        self._update_texts()
        if self.source() == "view" and self.plan is not None:
            self._invalidate(stale=True)
            self._schedule()

    def _on_view_busy(self, _busy: bool) -> None:
        if self.source() == "view":
            self._inputs_changed()

    def _on_session_state(self, state: str) -> None:
        if state == "closed":
            self._invalidate()
            self._plan_pending = False
            self._local_errors = [t("gui.filetools.error.closed")]
            self._show_plan()
            self._update_run_button()
            return
        self._update_texts()
        self._inputs_changed()

    def _on_index_finished(self) -> None:
        self._update_texts()
        self._inputs_changed()

    def _on_data_changed(self, _cs: Any = None) -> None:
        """An edit: the plan was made for another state -- stale, planned again."""
        self._update_texts()
        self._invalidate(stale=True)
        self._update_widgets()
        self._schedule()

    def _inputs_changed(self, *_args: Any) -> None:
        if self._syncing or self._closing:
            return
        self._update_widgets()
        self._invalidate()
        self._schedule()

    def _schedule(self) -> None:
        if self._closing:
            return
        self._plan_pending = True
        self._plan_timer.start()
        self._show_plan()
        self._update_run_button()

    def _invalidate(self, *, stale: bool = False) -> None:
        """Forget the plan: a running plan task is cancelled, its late result dropped."""
        self._plan_serial += 1
        task = self.plan_task
        if task is not None and task.state == "running":
            task.cancel()
        self.plan_task = None
        self._plan_stale = stale and (self.plan is not None or self._plan_stale)
        self.plan = None
        self.plan_error = None
        self._local_errors = []
        self._start_errors = []

    def _update_widgets(self) -> None:
        mode = self.mode()
        self.parts_spin.setEnabled(mode == "parts")
        self.balance_combo.setEnabled(mode == "parts")
        self.records_spin.setEnabled(mode == "records")
        self.size_edit.setEnabled(mode == "size")
        fmt = self.format()
        self.indent_combo.setEnabled(fmt == "json")
        self.output_form.setRowVisible(self.indent_combo, fmt == "json")
        session = self.session
        doc = session.doc if _session_ok(session) else None
        source = self.source()
        wrapped = doc is not None and doc.kind == "wrapped"
        self.keep_wrapper_check.setEnabled(source == "node" or wrapped)
        self.keep_wrapper_check.setToolTip(t("gui.filetools.split.keep_wrapper.tip") if source == "node" or wrapped
                                           else t("gui.filetools.split.keep_wrapper.only_wrapped"))
        broken = int(getattr(doc, "broken_count", 0) or 0) if doc is not None and doc.kind == "jsonl" else 0
        self.drop_broken_check.setEnabled(broken > 0 and source != "node")
        self.drop_broken_check.setToolTip(t("gui.filetools.split.drop_broken.tip", count=format_count(broken))
                                          if broken else t("gui.filetools.split.drop_broken.none"))

    def _update_texts(self) -> None:
        session = self.session
        if not _session_ok(session):
            return
        doc = session.doc
        count = format_count(doc.record_count) if doc is not None else "?"
        self.source_buttons["document"].setText(_amp(t("gui.filetools.split.source.document", count=count)))
        view_len = format_count(session.view_len())
        if doc is None or session.view_is_base:
            view_text = t("gui.filetools.split.source.view_all", count=view_len)
        else:
            view_text = t("gui.filetools.split.source.view", label=session.view_label(), count=view_len)
        self.source_buttons["view"].setText(_amp(view_text))
        if self.node_ref is not None:
            self.source_buttons["node"].setText(_amp(t("gui.filetools.split.source.node",
                                                       path=self._where())))

    def _where(self) -> str:
        try:
            return str(self.session.location_text(self.node_ref))
        except Exception:  # pragma: no cover - defensive
            return str(getattr(self.node_ref, "path", ""))

    def _source_label(self) -> str:
        source = self.source()
        button = self.source_buttons.get(source)
        return button.text().replace("&&", "&") if button is not None else ""

    # ------------------------------------------------------------------------------- planning
    def _check_inputs(self) -> tuple[list[str], SplitOptions | None]:
        """The refusals the GUI thread can tell at once, and the options (``None`` when invalid)."""
        session = self.session
        if not _session_ok(session):
            return [t("gui.filetools.error.closed")], None
        doc = session.doc
        if doc is None or session.state != "ready" or not doc.index_complete:
            return [t("gui.filetools.error.not_ready")], None
        errors: list[str] = []
        source = self.source()
        if source == "view" and getattr(session, "view_busy", False):
            errors.append(t("gui.filetools.error.view_busy"))
        if source == "node":
            try:
                check_node_source(doc)
            except UserInputError as exc:
                errors.append(exc.message)
        opts: SplitOptions | None = None
        try:
            opts = self.split_options()
        except JsonReshapeError as exc:
            errors.append(error_text(exc))
        broken = int(getattr(doc, "broken_count", 0) or 0) if doc.kind == "jsonl" else 0
        if source != "node" and broken and opts is not None and not opts.drop_broken:
            errors.append(error_text(UserInputError("filetools.error.broken_lines", count=format_count(broken))))
        return errors, (opts if not errors else None)

    def _start_plan(self) -> Any:
        """Start the plan task for the inputs as they are now (a snapshot of the session)."""
        if self._closing:
            return None
        self._plan_timer.stop()
        errors, opts = self._check_inputs()
        if errors or opts is None:
            self._plan_pending = False
            self._plan_stale = False
            self._local_errors = errors
            self._show_plan()
            self._update_run_button()
            return None
        session = self.session
        doc, view = session.snapshot()
        if doc is None:   # pragma: no cover - checked above
            return None
        source_kind = self.source()
        source = view if source_kind == "view" and not session.view_is_base else None
        node = self.node_ref if source_kind == "node" else None
        req = _PlanRequest(doc=doc, source=source, opts=_copy_split_options(opts), source_kind=source_kind,
                           session_path=str(session.path), settings=self.app.settings, view_serial=session.view_serial,
                           gen=int(getattr(doc, "gen", 0)), session=session if node is not None else None, node=node,
                           where=self._where() if node is not None else "")
        serial = self._plan_serial
        self._plan_pending = True
        task = self.app.tasks.run(lambda ctx, r=req: _make_plan(r, ctx.cancel),
                                  title=t("gui.filetools.split.plan_task", name=session.name), kind="plan", owner=self,
                                  silent=True, on_done=lambda plan, s=serial: self._plan_done(s, plan),
                                  on_error=lambda exc, s=serial: self._plan_failed(s, exc),
                                  on_cancel=lambda s=serial: self._plan_cancelled(s))
        keep_for(session, task, sets=(source,) if source is not None else (), doc=doc)
        self.plan_task = task
        self._show_plan()
        return task

    def plan_now(self) -> Any:
        """Plan at once (no debounce); returns the plan task (``None`` when the inputs are refused)."""
        self._invalidate()
        return self._start_plan()

    def _plan_done(self, serial: int, plan: SplitPlan) -> None:
        if serial != self._plan_serial or self._closing:
            return                     # an older request: dropped
        self.plan_task = None
        self.plan = plan
        self.plan_error = None
        self._plan_pending = False
        self._plan_stale = False
        self._show_plan()
        self._update_run_button()
        self.plan_ready.emit(plan)

    def _plan_failed(self, serial: int, exc: BaseException) -> None:
        if serial != self._plan_serial or self._closing:
            return
        if not isinstance(exc, JsonReshapeError):
            log.warning("planning a split failed", exc_info=(type(exc), exc, exc.__traceback__))
        self.plan_task = None
        self.plan = None
        self.plan_error = error_text(exc)
        self._plan_pending = False
        self._plan_stale = False
        self._show_plan()
        self._update_run_button()
        self.plan_failed.emit(exc)

    def _plan_cancelled(self, serial: int) -> None:
        if serial != self._plan_serial or self._closing:
            return
        self.plan_task = None
        self._plan_pending = False
        self._show_plan()
        self._update_run_button()

    def plan_errors(self) -> list[str]:
        """The refusals shown now (the inputs' and the plan's)."""
        out = list(self._local_errors)
        if self.plan_error:
            out.append(self.plan_error)
        return out

    def _show_plan(self) -> None:
        plan = self.plan
        notes: list[str] = []
        warnings: list[str] = []
        session = self.session
        doc = session.doc if _session_ok(session) else None
        if doc is not None and bool(getattr(doc, "edited", False)) and self.source() != "node":
            notes.append(t("gui.filetools.split.edited_note"))
        self.plan_tree.clear()
        if self._plan_pending:
            self.plan_label.setText(t("gui.filetools.split.plan.stale") if self._plan_stale
                                    else t("gui.filetools.split.plan.pending"))
        elif plan is None:
            self.plan_label.setText("")
        else:
            total = plan.total_size
            lines = [t("gui.filetools.split.plan.summary", parts=format_count(plan.count),
                       records=format_count(plan.records), size=size_text(total))]
            if plan.engine == "same":
                lines.append(t("gui.filetools.report.engine.same"))
            else:
                lines.append(t("gui.filetools.report.engine.export", fmt=_format_text(plan.fmt)))
            self.plan_label.setText(_plain("\n".join(lines)))
            self._fill_tree(plan)
            if plan.existing:
                warnings.append(t("gui.filetools.split.plan.existing", count=format_count(len(plan.existing)),
                                  names=_names_text(plan.existing)))
            warnings.extend(plan.warnings)
        errors = self.plan_errors() + self._start_errors if not self._plan_pending else []
        self._set_messages(errors, warnings, notes)

    def _fill_tree(self, plan: SplitPlan) -> None:
        tree = self.plan_tree
        items = []
        right = Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter
        for k in range(min(plan.count, PREVIEW_MAX)):
            a, b = plan.ranges[k]
            item = QTreeWidgetItem([format_count(k + 1), _plain(plan.names[k].name),
                                    t("gui.filetools.split.plan.range", first=a, last=b - 1),
                                    format_count(b - a), "≈ " + size_text(plan.sizes[k])])
            item.setToolTip(1, _plain(str(plan.names[k])))
            for col in (0, 3, 4):
                item.setTextAlignment(col, right)
            items.append(item)
        if plan.count > PREVIEW_MAX:
            more = QTreeWidgetItem(["", t("gui.filetools.split.plan.more",
                                          count=format_count(plan.count - PREVIEW_MAX))])
            more.setFlags(Qt.ItemFlag.NoItemFlags)
            items.append(more)
        tree.addTopLevelItems(items)

    def plan_rows(self) -> list[list[str]]:
        """The plan as listed (tests): one list of column texts per row."""
        tree = self.plan_tree
        return [[tree.topLevelItem(i).text(c) for c in range(tree.columnCount())]
                for i in range(tree.topLevelItemCount())]

    def _own_running(self) -> bool:
        return self.running

    def _update_run_button(self) -> None:
        ok = (self.plan is not None and not self._plan_pending and not self.running and _session_ok(self.session)
              and not self._closing)
        self.run_button.setEnabled(ok)

    # ------------------------------------------------------------------------------- folder chooser
    def choose_folder(self) -> str | None:
        """The folder chooser (injected ``ask_folder``)."""
        start = self.out_dir() or os.path.dirname(os.path.abspath(self.session.path))
        path = self._ask_folder(self, t("gui.filetools.split.folder.title"), start)
        if path:
            self.set_out_dir(path)
        return path

    # ------------------------------------------------------------------------------- split
    def start_split(self) -> Any:
        """Start the split of the current plan (heavy task); returns it (``None`` when there is no current
        plan, the plan is stale or the user declined to replace existing files)."""
        if self.running or self._closing:
            return None
        plan = self.plan
        session = self.session
        if plan is None or self._plan_pending or not _session_ok(session) or session.doc is None:
            return None
        doc, view = session.snapshot()
        if doc is None or int(getattr(doc, "gen", 0)) != plan.gen or (
                plan.source == "view" and session.view_serial != plan.view_serial):
            self._invalidate(stale=True)       # (a change the signals have not told yet)
            self._schedule()
            return None
        if self._start_errors:
            self._start_errors = []
            self._show_plan()
        reload = self._parts_target_check(plan)
        if reload is None:
            return None
        if plan.existing and not reload:
            question = t("gui.filetools.split.overwrite", count=format_count(len(plan.existing)),
                         names=_names_text(plan.existing, 10))
            if not confirm(self, question):
                return None
        self._remember()
        source = view if plan.source == "view" and not session.view_is_base else None
        record_path, where = plan.node_path, plan.where
        opts = _copy_split_options(plan.opts)
        edited = bool(getattr(doc, "edited", False)) and record_path is None
        settings = self.app.settings
        label = self._source_label()

        def work(ctx: Any) -> SplitOutcome:
            if record_path is not None:
                # (the plan resolved the node: the session is not needed in this thread)
                ctx.progress.set_message(t("gui.filetools.split.progress.node", path=where))
                target = open_at(doc, record_path, settings=settings, where=where, cancel=ctx.cancel,
                                 progress=ctx.progress)
                try:
                    report = filetools.split(target, opts, None, progress=ctx.progress, cancel=ctx.cancel)
                finally:
                    target.close()
            else:
                if source is not None and source.closed:   # pragma: no cover - the lease prevents it
                    raise UserInputError("gui.filetools.error.view_changed")
                report = filetools.split(doc, opts, source, progress=ctx.progress, cancel=ctx.cancel)
            return SplitOutcome(report=report, edited=edited, source=label)

        self.outcome = None
        self.error = None
        self.report_dialog = None
        self.job = self.runner.start(work, tool="split", title=t("gui.filetools.split.task", name=session.name),
                                     path=str(opts.out_dir), holds=[(session, (source,) if source is not None else (),
                                                                     doc)],
                                     parents=(self.parentWidget(), self), open_folder=self._open_folder,
                                     open_file=self._open_file, reload=reload)
        self.task = self.job.task
        self._show_running()
        self.split_started.emit(self.task)
        return self.task

    def _parts_target_check(self, plan: SplitPlan) -> list | None:
        """Parts that tabs of this program show (review S3; per part the rule of
        :func:`~jsonreshape.gui.searchpanel.open_target_check`): ``None`` -- refused (a tab shows a part with
        unsaved, pending or running changes, or another window of the program edits one: the error line says
        why) or not confirmed; else the weak references of the tabs to reopen once the parts were written
        (``[]``: no tab shows a part -- the usual overwrite question follows). Asked once for all the parts;
        that question replaces the overwrite question. GUI thread: one path lookup per open tab, then the
        stat calls and the lock probe of :func:`~jsonreshape.gui.editing.target_conflict` only for the parts
        a tab shows or whose edit lock was held when the plan was made (``SplitPlan.locked``)."""
        from jsonreshape.gui.editing import target_conflict
        from jsonreshape.gui.session import open_sessions

        by_name = plan.name_keys
        candidates: list[Path] = []
        for other in open_sessions():
            if other is self.session:
                continue
            try:
                part = by_name.get(normalize_path(other.path))
            except (OSError, TypeError, ValueError):
                part = None
            if part is not None and part not in candidates:
                candidates.append(part)
        candidates.extend(p for p in plan.locked if p not in candidates)
        shown: list[Path] = []
        users: list[Any] = []
        for part in candidates:
            found = target_conflict(part, exclude=self.session)
            if found is None:
                continue
            what, sessions = found
            if what != "open":
                self._start_errors = [target_conflict_text(what, part)]
                self._show_plan()
                return None
            shown.append(part)
            users.extend(s for s in sessions if not any(s is u for u in users))
        if not shown:
            return []
        replaced = list(plan.existing) + [p for p in shown if p not in plan.existing]
        question = t("gui.filetools.split.overwrite_open", count=format_count(len(replaced)),
                     names=_names_text(replaced, 10), open=format_count(len(shown)), tabs=_names_text(shown, 10))
        if not confirm(self, question):
            return None
        return [weakref.ref(s) for s in users]

    def _job_done(self, job: FileToolsJob) -> None:
        report = job.outcome.report
        self.status_label.setText(t("gui.filetools.status.split_done", parts=format_count(len(report.parts)),
                                    size=size_text(report.bytes_written)))
        self.split_finished.emit(job.outcome)
        self._inputs_changed()               # the parts exist now: the plan names them as existing

    def _job_failed(self, job: FileToolsJob) -> None:
        self.split_failed.emit(job.error)
        self._inputs_changed()

    def _cleanup(self) -> None:
        self._plan_timer.stop()
        self._plan_serial += 1
        task = self.plan_task
        if task is not None and task.state == "running":
            task.cancel()
        self.plan_task = None
        try:
            if _SPLIT_OPEN.get(self.session) is self:
                del _SPLIT_OPEN[self.session]
        except (TypeError, KeyError, RuntimeError):
            pass


# ==================================================================================
# MergeDialog
# ==================================================================================
@dataclass(eq=False)
class MergeInput:
    """One input of a merge: a file read from disk, or an open tab (``session``) read through its pinned
    snapshot. ``info``: what the probe found out about a file (kind, records, size, partial, line, column,
    broken, fields); ``error``: why it could not be opened."""

    path: str
    session: Any = None
    info: dict[str, Any] | None = None
    error: str | None = None
    task: Any = None

    @property
    def is_tab(self) -> bool:
        return _session_ok(self.session)

    @property
    def name(self) -> str:
        return os.path.basename(self.path)


def _probe_file(path: str, settings: Any, cancel: CancelToken | None) -> dict[str, Any]:
    """Open and index a merge input (worker thread): its kind, records, size, JSON error, broken lines and
    the field paths of a sample (deduplication key suggestions)."""
    doc = Document.open(path, settings=settings, cancel=cancel)
    try:
        doc.build_index(cancel=cancel)
        err = doc.error
        info: dict[str, Any] = {
            "kind": str(doc.kind), "records": int(doc.record_count), "size": int(doc.size),
            "partial": err is not None, "line": int(getattr(err, "line", 0) or 0),
            "column": int(getattr(err, "column", 0) or 0), "broken": int(doc.broken_count or 0), "fields": [],
        }
        info["fields"] = _fields_of(doc, cancel)
        return info
    finally:
        doc.close()


def _fields_of(doc: Any, cancel: CancelToken | None) -> list[str]:
    """Concrete field paths of a sample of ``doc``'s records (deduplication key suggestions)."""
    try:
        schema = infer_schema(doc, sample=FIELDS_SAMPLE, cancel=cancel)
    except Cancelled:
        raise
    except JsonReshapeError:
        return []
    return [f for f in field_paths(schema, inside_arrays=False) if f != dupes.KEY_FIELD][:FIELDS_MAX]


_MERGE_OPEN: weakref.WeakKeyDictionary[Any, MergeDialog] = weakref.WeakKeyDictionary()


class _ListKeys(QObject):
    """The keys of the inputs list: Delete removes, Alt+Up / Alt+Down move the selected input (consumed
    before the list moves its current row)."""

    def __init__(self, dialog: MergeDialog) -> None:
        super().__init__(dialog)
        self._dialog = weakref.ref(dialog)

    def eventFilter(self, obj: QObject, event: QEvent) -> bool:  # noqa: N802 - Qt name
        if event.type() != QEvent.Type.KeyPress:
            return False
        dialog = self._dialog()
        if dialog is None or dialog._closing:
            return False
        key = event.key()
        mods = event.modifiers() & ~Qt.KeyboardModifier.KeypadModifier
        if key == Qt.Key.Key_Delete and mods == Qt.KeyboardModifier.NoModifier:
            dialog.remove_selected()
            return True
        if key in (Qt.Key.Key_Up, Qt.Key.Key_Down) and mods == Qt.KeyboardModifier.AltModifier:
            dialog.move_selected(-1 if key == Qt.Key.Key_Up else 1)
            return True
        return False


class MergeDialog(_ToolDialog):
    """Merge files and open tabs into one JSON / JSON Lines file (module docstring "MergeDialog").

    ``open_documents()``: the open tabs (sessions, or objects with a ``session``); ``inputs``: initial
    inputs (sessions or paths); ``ask_files(parent, title, start) -> list[str]`` / ``ask_output(parent,
    title, start) -> str | None``: the file choosers; ``open_folder(path)`` / ``open_file(path)``: the
    report's "open the folder" / "open the result" (defaults: the system file manager / a new tab).
    Signals: ``merge_started(task)``, ``merge_finished(MergeOutcome)``, ``merge_failed(exc)``,
    ``inputs_changed()``."""

    merge_started = Signal(object)
    merge_finished = Signal(object)
    merge_failed = Signal(object)
    inputs_changed = Signal()

    def __init__(self, app: Any, open_documents: Callable[[], Iterable[Any]] | None = None,
                 parent: QWidget | None = None, *, inputs: Iterable[Any] = (),
                 ask_files: Callable[[QWidget | None, str, str], list[str]] | None = None,
                 ask_output: Callable[[QWidget | None, str, str], str | None] | None = None,
                 open_folder: Callable[[str], Any] | None = None,
                 open_file: Callable[[str], Any] | None = None) -> None:
        super().__init__(app, parent, open_folder=open_folder, open_file=open_file)
        self.open_documents = open_documents or (lambda: [])
        self._ask_files = ask_files or _ask_files
        self._ask_output = ask_output or _ask_output
        self._inputs: list[MergeInput] = []
        self._probe_task: Any = None
        self._fields_task: Any = None
        self._fields: list[str] = []
        self._fields_for: Any = None
        self._path_edited = False
        self._syncing = False
        self._valid = False
        self._validate_timer = QTimer(self)
        self._validate_timer.setSingleShot(True)
        self._validate_timer.setInterval(VALIDATE_DELAY_MS)
        self._validate_timer.timeout.connect(self._validate)
        self.setWindowTitle(t("gui.filetools.merge.title"))
        self.setAcceptDrops(True)
        self._build()
        self._style_labels()
        self._restore_last()
        try:
            _MERGE_OPEN[app] = self
        except TypeError:  # pragma: no cover
            pass
        for item in inputs:
            self.add_input(item, validate=False)
        self._after_inputs_changed()
        self._validate()
        self.resize(700, 680)

    # ------------------------------------------------------------------------------- registry
    @classmethod
    def instance(cls, app: Any) -> MergeDialog | None:
        """The open merge dialog of ``app`` (``None`` when there is none)."""
        try:
            dlg = _MERGE_OPEN.get(app)
        except (TypeError, RuntimeError):  # pragma: no cover
            return None
        return dlg if dlg is not None and _alive(dlg) and not dlg._closing else None

    # ------------------------------------------------------------------------------- build
    def _build(self) -> None:
        layout = QVBoxLayout(self)
        inputs_box = QGroupBox(t("gui.filetools.merge.inputs"), self)
        box = QVBoxLayout(inputs_box)
        self.list_widget = QListWidget(inputs_box)
        self.list_widget.setObjectName("merge_inputs")
        self.list_widget.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.list_widget.currentRowChanged.connect(lambda _row: self._update_list_buttons())
        box.addWidget(self.list_widget, 1)
        self._list_keys = _ListKeys(self)
        self.list_widget.installEventFilter(self._list_keys)
        buttons = QHBoxLayout()
        self.add_files_button = QPushButton(t("gui.filetools.merge.add_files"), inputs_box)
        self.add_files_button.setAutoDefault(False)
        self.add_files_button.clicked.connect(lambda _checked=False: self.choose_files())
        buttons.addWidget(self.add_files_button)
        self.tabs_button = QToolButton(inputs_box)
        self.tabs_button.setText(t("gui.filetools.merge.add_tabs"))
        self.tabs_button.setPopupMode(QToolButton.ToolButtonPopupMode.InstantPopup)
        self.tabs_menu = QMenu(self.tabs_button)
        self.tabs_menu.aboutToShow.connect(self._fill_tabs_menu)
        self.tabs_button.setMenu(self.tabs_menu)
        buttons.addWidget(self.tabs_button)
        buttons.addStretch(1)
        self.remove_button = QPushButton(t("gui.filetools.merge.remove"), inputs_box)
        self.remove_button.setAutoDefault(False)
        self.remove_button.setToolTip(t("gui.filetools.merge.remove.tip"))
        self.remove_button.clicked.connect(lambda _checked=False: self.remove_selected())
        buttons.addWidget(self.remove_button)
        self.up_button = QPushButton(t("gui.filetools.merge.up"), inputs_box)
        self.up_button.setAutoDefault(False)
        self.up_button.setToolTip(t("gui.filetools.merge.up.tip"))
        self.up_button.clicked.connect(lambda _checked=False: self.move_selected(-1))
        buttons.addWidget(self.up_button)
        self.down_button = QPushButton(t("gui.filetools.merge.down"), inputs_box)
        self.down_button.setAutoDefault(False)
        self.down_button.setToolTip(t("gui.filetools.merge.down.tip"))
        self.down_button.clicked.connect(lambda _checked=False: self.move_selected(1))
        buttons.addWidget(self.down_button)
        box.addLayout(buttons)
        layout.addWidget(inputs_box, 1)

        output_box = QGroupBox(t("gui.filetools.merge.output.title"), self)
        form = QFormLayout(output_box)
        row = QHBoxLayout()
        self.path_edit = QLineEdit(output_box)
        self.path_edit.setObjectName("merge_output")
        self.path_edit.textChanged.connect(self._on_path_changed)
        row.addWidget(self.path_edit, 1)
        self.browse_button = QPushButton(t("gui.filetools.choose"), output_box)
        self.browse_button.setAutoDefault(False)
        self.browse_button.clicked.connect(lambda _checked=False: self.choose_output())
        row.addWidget(self.browse_button)
        path_label = QLabel(t("gui.filetools.merge.output"), output_box)
        path_label.setBuddy(self.path_edit)
        form.addRow(path_label, row)
        fmt_row = QHBoxLayout()
        self.format_group = QButtonGroup(self)
        self.format_buttons: dict[str, QRadioButton] = {}
        for i, fmt in enumerate(MERGE_FORMATS):
            button = QRadioButton(_format_text(fmt), output_box)
            button.setObjectName(f"merge_format_{fmt}")
            self.format_group.addButton(button, i)
            self.format_buttons[fmt] = button
            fmt_row.addWidget(button)
        fmt_row.addStretch(1)
        self.format_buttons["json"].setChecked(True)
        self.format_group.idToggled.connect(self._on_format_toggled)
        form.addRow(t("gui.filetools.merge.format"), fmt_row)
        self.indent_combo = _indent_combo(output_box, self.app.settings)
        self.indent_combo.currentIndexChanged.connect(self._schedule_validate)
        form.addRow(t("gui.filetools.indent"), self.indent_combo)
        self.sort_combo = QComboBox(output_box)
        for key, value in _SORTS:
            self.sort_combo.addItem(t(f"gui.filetools.sort.{key}"), value)
        self.sort_combo.setToolTip(t("gui.filetools.sort.tip"))
        self.sort_combo.currentIndexChanged.connect(self._schedule_validate)
        form.addRow(t("gui.filetools.sort"), self.sort_combo)
        self.keep_wrapper_check = QCheckBox(t("gui.filetools.merge.keep_wrapper"), output_box)
        self.keep_wrapper_check.setObjectName("merge_keep_wrapper")
        self.keep_wrapper_check.setToolTip(t("gui.filetools.merge.keep_wrapper.tip"))
        self.keep_wrapper_check.setChecked(True)
        self.keep_wrapper_check.toggled.connect(self._schedule_validate)
        form.addRow("", self.keep_wrapper_check)
        self.allow_partial_check = QCheckBox(t("gui.filetools.merge.allow_partial"), output_box)
        self.allow_partial_check.setObjectName("merge_allow_partial")
        self.allow_partial_check.setToolTip(t("gui.filetools.merge.allow_partial.tip"))
        self.allow_partial_check.toggled.connect(self._schedule_validate)
        form.addRow("", self.allow_partial_check)
        self.output_form = form
        layout.addWidget(output_box)

        self.dedupe_box = QGroupBox(t("gui.filetools.merge.dedupe"), self)
        self.dedupe_box.setObjectName("merge_dedupe")
        self.dedupe_box.setCheckable(True)
        self.dedupe_box.setChecked(False)
        self.dedupe_box.toggled.connect(self._schedule_validate)
        dform = QFormLayout(self.dedupe_box)
        self.dedupe_combo = QComboBox(self.dedupe_box)
        self.dedupe_combo.setObjectName("merge_dedupe_key")
        self.dedupe_combo.setEditable(True)
        self.dedupe_combo.setInsertPolicy(QComboBox.InsertPolicy.NoInsert)
        self.dedupe_combo.setToolTip(t("gui.filetools.merge.dedupe.key.tip"))
        self._set_key_items([])
        self.dedupe_combo.setEditText(WHOLE_RECORD)
        self.dedupe_combo.editTextChanged.connect(self._schedule_validate)
        dform.addRow(t("gui.filetools.merge.dedupe.key"), self.dedupe_combo)
        keep_row = QHBoxLayout()
        self.keep_group = QButtonGroup(self)
        self.keep_first_radio = QRadioButton(t("gui.filetools.merge.keep.first"), self.dedupe_box)
        self.keep_last_radio = QRadioButton(t("gui.filetools.merge.keep.last"), self.dedupe_box)
        self.keep_group.addButton(self.keep_first_radio, 0)
        self.keep_group.addButton(self.keep_last_radio, 1)
        self.keep_first_radio.setChecked(True)
        self.keep_group.idToggled.connect(lambda _id, _on: self._schedule_validate())
        keep_row.addWidget(self.keep_first_radio)
        keep_row.addWidget(self.keep_last_radio)
        keep_row.addStretch(1)
        dform.addRow(t("gui.filetools.merge.keep"), keep_row)
        layout.addWidget(self.dedupe_box)

        self._add_messages(layout)
        self._add_buttons(layout, t("gui.filetools.merge.run"), self.start_merge)
        self._update_list_buttons()

    def _set_key_items(self, fields: list[str]) -> None:
        combo = self.dedupe_combo
        text = combo.currentText()
        blocked = combo.blockSignals(True)
        try:
            combo.clear()
            combo.addItem(WHOLE_RECORD)
            combo.setItemData(0, t("gui.filetools.merge.dedupe.whole"), Qt.ItemDataRole.ToolTipRole)
            combo.addItem(dupes.KEY_FIELD)
            combo.setItemData(1, t("gui.filetools.merge.dedupe.member_key"), Qt.ItemDataRole.ToolTipRole)
            for f in fields:
                if f not in (WHOLE_RECORD, dupes.KEY_FIELD):
                    combo.addItem(_plain(f))
            combo.setEditText(text)
        finally:
            combo.blockSignals(blocked)

    # ------------------------------------------------------------------------------- state
    def _restore_last(self) -> None:
        last = self.app.state.get(STATE_MERGE)
        last = last if isinstance(last, dict) else {}
        self._syncing = True
        try:
            if last.get("fmt") in MERGE_FORMATS:
                self.format_buttons[last["fmt"]].setChecked(True)
            if "indent" in last and _indent_ok(last["indent"]):
                _select_data(self.indent_combo, last["indent"])
            if last.get("sort") in (False, "alpha", "codepoint"):
                _select_data(self.sort_combo, last["sort"])
            if isinstance(last.get("keep_wrapper"), bool):
                self.keep_wrapper_check.setChecked(last["keep_wrapper"])
            if isinstance(last.get("key"), str) and last["key"].strip():
                self.dedupe_combo.setEditText(last["key"])
            if last.get("keep") == "last":
                self.keep_last_radio.setChecked(True)
        finally:
            self._syncing = False
        self._update_format_widgets()

    def _remember(self) -> None:
        data = {"fmt": self.format(), "indent": self.indent_combo.currentData(), "sort": self.sort_combo.currentData(),
                "keep_wrapper": self.keep_wrapper_check.isChecked(), "key": self.dedupe_combo.currentText().strip(),
                "keep": "last" if self.keep_last_radio.isChecked() else "first",
                "dir": os.path.dirname(self.output_path())}
        try:
            self.app.state.set(STATE_MERGE, data)
        except (TypeError, ValueError):  # pragma: no cover
            return
        save = getattr(self.app, "save_state", None)
        if callable(save):
            save()

    # ------------------------------------------------------------------------------- inputs
    def inputs(self) -> list[MergeInput]:
        return list(self._inputs)

    def _open_tabs(self) -> list[Any]:
        out = []
        try:
            docs = list(self.open_documents() or [])
        except Exception:  # pragma: no cover - a broken callback must not break the dialog
            log.exception("open_documents failed")
            docs = []
        for item in docs:
            session = getattr(item, "session", item)
            if _session_ok(session) and not any(s is session for s in out):
                out.append(session)
        return out

    def _tab_for(self, path: str) -> Any:
        """The open tab showing ``path`` (a ready one first), or ``None``."""
        key = normalize_path(path)
        found = None
        for session in self._open_tabs():
            try:
                same = normalize_path(session.path) == key
            except Exception:  # pragma: no cover
                same = False
            if same:
                if session.doc is not None and session.state == "ready":
                    return session
                found = found or session
        return found

    def add_input(self, item: Any, *, validate: bool = True) -> MergeInput | None:
        """Add a session (an open tab) or a path."""
        session = getattr(item, "session", item)
        if _session_ok(session) and hasattr(session, "snapshot"):
            return self.add_session(session, validate=validate)
        if isinstance(item, (str, os.PathLike)):
            added = self.add_files([item], validate=validate)
            return added[0] if added else None
        return None

    def add_files(self, paths: Iterable[Any], *, validate: bool = True) -> list[MergeInput]:
        """Add files (a file open in a tab is read through that tab: its snapshot)."""
        added = []
        for p in paths:
            path = os.path.abspath(os.fspath(p))
            tab = self._tab_for(path)
            inp = MergeInput(path=path, session=tab)
            self._inputs.append(inp)
            if tab is not None:
                self._watch_session(tab)
            added.append(inp)
        if added and validate:
            self._after_inputs_changed()
        return added

    def add_session(self, session: Any, *, validate: bool = True) -> MergeInput | None:
        """Add an open tab (read through its pinned snapshot when the merge starts)."""
        if not _session_ok(session):
            return None
        inp = MergeInput(path=os.path.abspath(str(session.path)), session=session)
        self._inputs.append(inp)
        self._watch_session(session)
        if validate:
            self._after_inputs_changed()
        return inp

    def add_open_tabs(self) -> int:
        """Add every open tab not listed yet; the number added."""
        listed = [inp.session for inp in self._inputs if inp.is_tab]
        n = 0
        for session in self._open_tabs():
            if not any(s is session for s in listed):
                self.add_session(session, validate=False)
                n += 1
        if n:
            self._after_inputs_changed()
        return n

    def remove_input(self, index: int) -> None:
        if not 0 <= index < len(self._inputs):
            return
        inp = self._inputs.pop(index)
        task = inp.task
        if task is not None and task.state == "running":
            task.cancel()
        inp.task = None
        session = inp.session
        if session is not None and not any(i.session is session for i in self._inputs):
            self._disconnect(session)
        self._after_inputs_changed(current=min(index, len(self._inputs) - 1))

    def remove_selected(self) -> None:
        row = self.list_widget.currentRow()
        if row >= 0:
            self.remove_input(row)

    def move_input(self, index: int, delta: int) -> int:
        """Move input ``index`` by ``delta`` places; its new index."""
        if not 0 <= index < len(self._inputs):
            return index
        new = max(0, min(len(self._inputs) - 1, index + delta))
        if new != index:
            self._inputs.insert(new, self._inputs.pop(index))
            self._after_inputs_changed(current=new)
        return new

    def move_selected(self, delta: int) -> None:
        row = self.list_widget.currentRow()
        if row >= 0:
            self.move_input(row, delta)

    def choose_files(self) -> list[MergeInput]:
        """The file chooser (injected ``ask_files``)."""
        start = os.path.dirname(self._inputs[-1].path) if self._inputs else str(self.app.state.get("last_dir") or "")
        paths = self._ask_files(self, t("gui.filetools.merge.add_files.title"), start)
        return self.add_files(paths) if paths else []

    def _fill_tabs_menu(self) -> None:
        menu = self.tabs_menu
        menu.clear()
        tabs = self._open_tabs()
        listed = [inp.session for inp in self._inputs if inp.is_tab]
        if not tabs:
            action = menu.addAction(t("gui.filetools.merge.add_tabs.none"))
            action.setEnabled(False)
            return
        for session in tabs:
            text = session.name + (" *" if getattr(session, "dirty", False) else "")
            action = menu.addAction(_amp(text))
            action.setToolTip(_plain(str(session.path)))
            action.setEnabled(not any(s is session for s in listed))
            action.triggered.connect(lambda _checked=False, s=session: self.add_session(s))
        menu.addSeparator()
        all_action = menu.addAction(t("gui.filetools.merge.add_tabs.all"))
        all_action.triggered.connect(lambda _checked=False: self.add_open_tabs())

    def _watch_session(self, session: Any) -> None:
        if any(tag is session for tag, _s, _slot in self._connections):
            return
        slot = functools.partial(self._on_tab_changed, session)
        for name in ("state_changed", "dirty_changed", "data_changed", "index_finished", "count_changed",
                     "path_changed"):
            signal = getattr(session, name, None)
            if signal is not None:
                self._connect(session, signal, slot)

    def _on_tab_changed(self, session: Any, *_args: Any) -> None:
        if self._closing:
            return
        if not _session_ok(session):
            # the tab was closed: its file is read from disk again (what the user saved or kept)
            for inp in self._inputs:
                if inp.session is session:
                    inp.session = None
                    inp.info = None
                    inp.error = None
            self._disconnect(session)
            self._after_inputs_changed(keep_fields=False)
            return
        for inp in self._inputs:
            if inp.session is session:
                inp.path = os.path.abspath(str(session.path))      # (Save As: the tab shows another file)
        self._refresh_list()
        self._schedule_validate()
        if self._inputs and self._inputs[0].session is session:
            self._update_fields()

    def _after_inputs_changed(self, *, current: int | None = None, keep_fields: bool = True) -> None:
        self._refresh_list(current)
        self._probe_next()
        self._update_fields(force=not keep_fields)
        self._update_default_path()
        self._schedule_validate()
        self.inputs_changed.emit()

    def _input_text(self, inp: MergeInput) -> str:
        name = inp.name
        session = inp.session if inp.is_tab else None
        if session is not None:
            doc = session.doc
            if doc is None or session.state != "ready":
                return t("gui.filetools.merge.input.tab_wait", name=name)
            key = "gui.filetools.merge.input.tab_dirty" if getattr(session, "dirty", False) \
                else "gui.filetools.merge.input.tab"
            return t(key, name=name, kind=_kind_text(doc.kind), records=format_count(doc.record_count))
        if inp.error:
            return t("gui.filetools.merge.input.failed", name=name, error=inp.error)
        info = inp.info
        if info is None:
            return t("gui.filetools.merge.input.probing", name=name)
        if info.get("partial"):
            return t("gui.filetools.merge.input.partial", name=name, line=info.get("line", 0),
                     records=format_count(info.get("records", 0)))
        return t("gui.filetools.merge.input.file", name=name, kind=_kind_text(info.get("kind")),
                 records=format_count(info.get("records", 0)), size=size_text(info.get("size", 0)))

    def _refresh_list(self, current: int | None = None) -> None:
        widget = self.list_widget
        if current is None:
            current = widget.currentRow()
        blocked = widget.blockSignals(True)
        try:
            widget.clear()
            for i, inp in enumerate(self._inputs):
                item = QListWidgetItem(_plain(f"{i + 1}. {self._input_text(inp)}"))
                item.setToolTip(_plain(inp.path))
                widget.addItem(item)
            if self._inputs:
                widget.setCurrentRow(max(0, min(len(self._inputs) - 1, current if current is not None else 0)))
        finally:
            widget.blockSignals(blocked)
        self._update_list_buttons()

    def input_texts(self) -> list[str]:
        """The inputs as listed (tests)."""
        return [self.list_widget.item(i).text() for i in range(self.list_widget.count())]

    def _update_list_buttons(self) -> None:
        row = self.list_widget.currentRow()
        n = len(self._inputs)
        self.remove_button.setEnabled(0 <= row < n)
        self.up_button.setEnabled(0 < row < n)
        self.down_button.setEnabled(0 <= row < n - 1)

    # -- probes: one silent task at a time ----------------------------------------------------
    def _probe_next(self) -> Any:
        if self._closing:
            return None
        task = self._probe_task
        if task is not None and task.state == "running":
            return task
        self._probe_task = None
        for inp in self._inputs:
            if inp.is_tab or inp.info is not None or inp.error is not None:
                continue
            path = inp.path
            settings = self.app.settings
            task = self.app.tasks.run(lambda ctx, p=path: _probe_file(p, settings, ctx.cancel),
                                      title=t("gui.filetools.merge.probe_task", name=inp.name), kind="probe",
                                      owner=self, silent=True,
                                      on_done=lambda info, i=inp: self._probe_done(i, info),
                                      on_error=lambda exc, i=inp: self._probe_failed(i, exc),
                                      on_cancel=lambda i=inp: self._probe_cancelled(i))
            inp.task = task
            self._probe_task = task
            return task
        return None

    @property
    def probing(self) -> bool:
        """A probe or a key-suggestion task runs."""
        return any(task is not None and task.state == "running" for task in (self._probe_task, self._fields_task))

    def _probe_done(self, inp: MergeInput, info: dict[str, Any]) -> None:
        inp.task = None
        if self._closing:
            return
        if any(i is inp for i in self._inputs) and not inp.is_tab:
            inp.info = info
            self._refresh_list()
            self._update_fields()
            self._schedule_validate()
        self._probe_task = None
        self._probe_next()

    def _probe_failed(self, inp: MergeInput, exc: BaseException) -> None:
        inp.task = None
        if self._closing:
            return
        if not isinstance(exc, JsonReshapeError):
            log.warning("probing %s failed", inp.path, exc_info=(type(exc), exc, exc.__traceback__))
        if any(i is inp for i in self._inputs) and not inp.is_tab:
            inp.error = error_text(exc).split("\n")[0]
            self._refresh_list()
            self._schedule_validate()
        self._probe_task = None
        self._probe_next()

    def _probe_cancelled(self, inp: MergeInput) -> None:
        inp.task = None
        if self._closing:
            return
        self._probe_task = None
        self._probe_next()

    # -- deduplication key suggestions ----------------------------------------------------------
    def _update_fields(self, force: bool = False) -> None:
        """Suggestions from the first input: a probed file's sample, or a sample of a tab's snapshot."""
        first = self._inputs[0] if self._inputs else None
        if first is None:
            self._fields_for = None
            self._apply_fields([])
            return
        if not first.is_tab:
            if first.info is not None:
                self._fields_for = first
                self._apply_fields(list(first.info.get("fields") or []))
            return
        session = first.session
        if self._fields_for is session and not force:
            return
        if session.doc is None or session.state != "ready":
            return
        task = self._fields_task
        if task is not None and task.state == "running":
            task.cancel()
        doc, _view = session.snapshot()
        self._fields_for = session
        self._fields_task = self.app.tasks.run(lambda ctx, d=doc: _fields_of(d, ctx.cancel),
                                               title=t("gui.filetools.merge.fields_task", name=session.name),
                                               kind="probe", owner=self, silent=True,
                                               on_done=lambda fields, s=session: self._fields_done(s, fields),
                                               on_error=lambda exc: log.debug("key suggestions failed: %r", exc))
        keep_for(session, self._fields_task, doc=doc)

    def _fields_done(self, session: Any, fields: list[str]) -> None:
        if self._closing or self._fields_for is not session:
            return
        self._apply_fields(fields)

    def _apply_fields(self, fields: list[str]) -> None:
        if fields == self._fields:
            return
        self._fields = list(fields)
        self._set_key_items(self._fields)

    def key_suggestions(self) -> list[str]:
        return [self.dedupe_combo.itemText(i) for i in range(self.dedupe_combo.count())]

    # ------------------------------------------------------------------------------- output
    def _base_dir(self) -> str:
        if self._inputs:
            return os.path.dirname(self._inputs[0].path)
        last = self.app.state.get(STATE_MERGE)
        if isinstance(last, dict) and isinstance(last.get("dir"), str) and last["dir"]:
            return last["dir"]
        return os.path.expanduser("~")

    def _default_path(self) -> str:
        if not self._inputs:
            return ""
        stem = _stem_ext(self._inputs[0].name)[0] or "merged"
        if stem.lower().endswith(".json"):
            stem = stem[:-5]
        return os.path.join(self._base_dir(), f"{stem}{MERGED_SUFFIX}{_EXT[self.format()]}")

    def _update_default_path(self) -> None:
        if self._path_edited:
            return
        self._syncing = True
        try:
            self.path_edit.setText(self._default_path())
        finally:
            self._syncing = False

    def output_path(self) -> str:
        """The absolute output path (a relative text is taken relative to the first input's folder)."""
        text = os.path.expanduser(self.path_edit.text().strip())
        if not text:
            return ""
        if not os.path.isabs(text):
            text = os.path.join(self._base_dir(), text)
        return os.path.abspath(text)

    def set_output_path(self, path: str | os.PathLike[str]) -> None:
        self.path_edit.setText(os.fspath(path))

    def choose_output(self) -> str | None:
        """The save chooser (injected ``ask_output``)."""
        path = self._ask_output(self, t("gui.filetools.merge.output.choose_title"), self.output_path())
        if path:
            if writer.format_for_path(path) is None:
                path += _EXT[self.format()]
            self.set_output_path(path)
        return path

    def format(self) -> str:
        """``"json"`` | ``"jsonl"``."""
        for fmt, button in self.format_buttons.items():
            if button.isChecked():
                return fmt
        return "json"

    def set_format(self, fmt: str) -> None:
        if fmt in self.format_buttons:
            self.format_buttons[fmt].setChecked(True)

    def _on_path_changed(self, text: str) -> None:
        if self._syncing:
            self._schedule_validate()
            return
        self._path_edited = True
        fmt = writer.format_for_path(text.strip()) if text.strip() else None
        if fmt in MERGE_FORMATS and fmt != self.format():
            self._syncing = True
            try:
                self.format_buttons[fmt].setChecked(True)
            finally:
                self._syncing = False
            self._update_format_widgets()
        self._schedule_validate()

    def _on_format_toggled(self, _id: int, checked: bool) -> None:
        if not checked:
            return
        self._update_format_widgets()
        if not self._syncing:
            fmt = self.format()
            text = self.path_edit.text().strip()
            if text and writer.format_for_path(text) != fmt:
                gz = text.lower().endswith(".gz")
                base = text[:-3] if gz else text
                stem, ext = os.path.splitext(base)
                new = (stem if writer.format_for_path(base) is not None or not ext else base) + _EXT[fmt] + \
                    (".gz" if gz else "")
                self._syncing = True
                try:
                    self.path_edit.setText(new)
                finally:
                    self._syncing = False
        self._schedule_validate()

    def _update_format_widgets(self) -> None:
        json_out = self.format() == "json"
        self.indent_combo.setEnabled(json_out)
        self.keep_wrapper_check.setEnabled(json_out)
        self.keep_wrapper_check.setToolTip(t("gui.filetools.merge.keep_wrapper.tip") if json_out
                                           else t("gui.filetools.merge.keep_wrapper.jsonl"))

    # ------------------------------------------------------------------------------- options
    def dedupe_key(self) -> str | None:
        """The deduplication key (``None`` when deduplication is off)."""
        if not self.dedupe_box.isChecked():
            return None
        return self.dedupe_combo.currentText().strip()

    def set_dedupe(self, key: str | None, keep: str = "first") -> None:
        self.dedupe_box.setChecked(key is not None)
        if key is not None:
            self.dedupe_combo.setEditText(key)
        (self.keep_last_radio if keep == "last" else self.keep_first_radio).setChecked(True)

    def merge_options(self) -> MergeOptions:
        """The options described by the dialog (``UserInputError`` / ``PathSyntaxError`` when invalid)."""
        fmt = self.format()
        kw: dict[str, Any] = {"sort_keys": self.sort_combo.currentData(),
                              "keep_wrapper": self.keep_wrapper_check.isChecked()}
        kw["indent"] = self.indent_combo.currentData() if fmt == "json" else None
        export = ExportOptions.from_settings(fmt, self.app.settings, **kw)
        key = self.dedupe_key()
        if key is not None:
            if not key:
                raise UserInputError("gui.filetools.merge.error.need_key")
            dupes.parse_key(key)
        return MergeOptions(export, dedupe_by=key, keep="last" if self.keep_last_radio.isChecked() else "first",
                            allow_partial=self.allow_partial_check.isChecked())

    # ------------------------------------------------------------------------------- validation
    def _schedule_validate(self, *_args: Any) -> None:
        if not self._closing:
            self._validate_timer.start()

    def _validate(self) -> bool:
        if self._closing:
            return False
        self._validate_timer.stop()
        errors: list[str] = []
        warnings: list[str] = []
        notes: list[str] = []
        if not self._inputs:
            errors.append(t("gui.filetools.merge.error.no_inputs"))
        allow = self.allow_partial_check.isChecked()
        seen: set[str] = set()
        warned: set[str] = set()
        edited: list[str] = []
        for inp in self._inputs:
            key = normalize_path(inp.path)
            if key in seen and key not in warned:
                warned.add(key)
                warnings.append(t("gui.filetools.merge.warn.twice", name=inp.name))
            seen.add(key)
            session = inp.session if inp.is_tab else self._tab_for(inp.path)
            if session is not None:
                doc = session.doc
                if doc is None or session.state != "ready":
                    errors.append(t("gui.filetools.merge.error.tab_not_ready", name=session.name))
                    continue
                if doc.partial and not allow:
                    err = doc.error
                    errors.append(error_text(UserInputError("filetools.error.partial_input", path=str(session.path),
                                                            line=getattr(err, "line", 0),
                                                            column=getattr(err, "column", 0))))
                if bool(getattr(doc, "edited", False)) and session.name not in edited:
                    edited.append(session.name)
                continue
            if inp.error:
                errors.append(t("gui.filetools.merge.error.input_failed", name=inp.name, error=inp.error))
            elif inp.info is not None and inp.info.get("partial") and not allow:
                errors.append(error_text(UserInputError("filetools.error.partial_input", path=inp.path,
                                                        line=inp.info.get("line", 0),
                                                        column=inp.info.get("column", 0))))
        out = self.output_path()
        if not out:
            errors.append(t("gui.filetools.merge.error.need_output"))
        else:
            ext_fmt = writer.format_for_path(out)
            if ext_fmt is not None and ext_fmt not in MERGE_FORMATS:
                errors.append(error_text(UserInputError("filetools.error.merge_format",
                                                        fmt=writer.FORMAT_NAMES[ext_fmt])))
            target = normalize_path(out)
            for inp in self._inputs:
                if target == normalize_path(inp.path):
                    errors.append(t("gui.filetools.merge.error.output_is_input", path=out))
                    break
        try:
            self.merge_options()
        except JsonReshapeError as exc:
            errors.append(error_text(exc))
        if edited:
            notes.append(t("gui.filetools.merge.edited_note", names=", ".join(edited)))
        self._set_messages(errors, warnings, notes)
        self._valid = not errors
        self._update_run_button()
        return self._valid

    def _update_run_button(self) -> None:
        self.run_button.setEnabled(self._valid and not self.running and not self._closing)

    # ------------------------------------------------------------------------------- merge
    def _resolve_inputs(self) -> tuple[list[tuple[str, Any]], list[tuple[Any, tuple, Any]], list[str]]:
        """``(entries, holds, edited)`` at request time: every input as ``(path, pinned document or None)``
        -- an input open in a ready tab is that tab's snapshot now --, the sessions to hold, the names of
        the tabs with unsaved edits."""
        entries: list[tuple[str, Any]] = []
        holds: list[tuple[Any, tuple, Any]] = []
        edited: list[str] = []
        for inp in self._inputs:
            session = inp.session if inp.is_tab else self._tab_for(inp.path)
            if session is not None and session.doc is not None and session.state == "ready":
                doc, _view = session.snapshot()
                entries.append((str(session.path), doc))
                holds.append((session, (), doc))
                if bool(getattr(doc, "edited", False)) and session.name not in edited:
                    edited.append(session.name)
            else:
                entries.append((inp.path, None))
        return entries, holds, edited

    def start_merge(self) -> Any:
        """Validate and start the merge (heavy task); returns it (``None`` when invalid or the user declined
        to replace the existing output)."""
        if self.running or self._closing or not self._validate():
            return None
        out = self.output_path()
        try:
            opts = self.merge_options()
        except JsonReshapeError as exc:  # pragma: no cover - validated above
            self._set_messages([error_text(exc)], [])
            return None
        entries, holds, edited = self._resolve_inputs()
        sources: list[Any] = []
        for path, doc in entries:
            sources.append(path)
            if doc is not None:
                sources.extend((doc.source_path, doc.data_path))
        try:
            check_not_source(out, sources)       # also another name of an input (samefile)
        except UserInputError:
            self._set_messages([t("gui.filetools.merge.error.output_is_input", path=out)], [])
            self.run_button.setEnabled(False)
            return None
        lp = long_path(out)
        if os.path.isdir(lp):
            self._set_messages([UserInputError("fileio.error.is_directory", path=out).message], [])
            return None
        # an output another tab shows: refused while it has changes, else asked -- instead of the overwrite
        # question -- and that tab opens the merged file (review S3)
        reload = open_target_check(self, out, on_refused=lambda text: self._set_messages([text], []))
        if reload is None:
            return None
        if not reload and os.path.exists(lp) and not confirm(self, t("gui.filetools.merge.overwrite", path=out)):
            return None
        self._remember()
        settings = self.app.settings

        def work(ctx: Any) -> MergeOutcome:
            cancel = ctx.cancel
            progress = ctx.progress
            opened: list[Any] = []
            docs: list[Any] = []
            try:
                for path, doc in entries:
                    check(cancel)
                    if doc is None:
                        progress.set_message(t("gui.filetools.merge.progress.open", name=os.path.basename(path)))
                        doc = Document.open(path, settings=settings, cancel=cancel)
                        opened.append(doc)
                    docs.append(doc)
                todo = [d for d in opened if not d.index_complete]
                if todo:
                    total = sum(max(1, int(d.data_size)) for d in todo)
                    progress.set_total(total)
                    progress.update(0)
                    base = 0
                    for d in todo:
                        size = max(1, int(d.data_size))
                        progress.set_message(t("gui.filetools.merge.progress.index", name=d.source_path.name))
                        sub = progress.child(base / total, (base + size) / total, total=size)
                        d.build_index(progress=sub, cancel=cancel)
                        base += size
                report = filetools.merge(docs, out, opts, progress=progress, cancel=cancel)
                return MergeOutcome(report=report, size=_file_size(out), edited=list(edited))
            finally:
                for d in opened:
                    d.close()

        self.outcome = None
        self.error = None
        self.report_dialog = None
        title = t("gui.filetools.merge.task", name=os.path.basename(out))
        self.job = self.runner.start(work, tool="merge", title=title,
                                     path=out, holds=holds, parents=(self.parentWidget(), self),
                                     open_folder=self._open_folder, open_file=self._open_file, reload=reload)
        self.task = self.job.task
        self._show_running()
        self.merge_started.emit(self.task)
        return self.task

    def _job_done(self, job: FileToolsJob) -> None:
        outcome = job.outcome
        self.status_label.setText(t("gui.filetools.status.merge_done", records=format_count(outcome.report.records),
                                    size=size_text(outcome.size)))
        self.merge_finished.emit(outcome)
        self._schedule_validate()

    def _job_failed(self, job: FileToolsJob) -> None:
        self.merge_failed.emit(job.error)
        self._schedule_validate()

    # ------------------------------------------------------------------------------- drag & drop
    def dragEnterEvent(self, event: Any) -> None:  # noqa: N802 - Qt name
        if self._local_files(event.mimeData()):
            event.acceptProposedAction()
        else:
            event.ignore()

    def dropEvent(self, event: Any) -> None:  # noqa: N802 - Qt name
        files = self._local_files(event.mimeData())
        if files:
            self.add_files(files)
            event.acceptProposedAction()
        else:
            event.ignore()

    @staticmethod
    def _local_files(mime: Any) -> list[str]:
        if mime is None or not mime.hasUrls():
            return []
        out = []
        for url in mime.urls():
            if url.isLocalFile():
                path = url.toLocalFile()
                if path and os.path.isfile(long_path(path)):
                    out.append(path)
        return out

    def _cleanup(self) -> None:
        self._validate_timer.stop()
        for task in [self._probe_task, self._fields_task] + [inp.task for inp in self._inputs]:
            if task is not None and task.state == "running":
                task.cancel()
        self._probe_task = None
        self._fields_task = None
        for inp in self._inputs:
            inp.task = None
        try:
            if _MERGE_OPEN.get(self.app) is self:
                del _MERGE_OPEN[self.app]
        except (TypeError, KeyError, RuntimeError):
            pass


# ==================================================================================
# integration API (round B wires these into the menus)
# ==================================================================================
def show_split_dialog(window: QWidget | None, session: Any, *, source: Any = None, **kwargs: Any) -> SplitDialog:
    """Show the split dialog of ``session`` (one per session: an open one is raised and, when ``source`` is
    given, switched to it). ``source``: ``"document"`` / ``"view"`` / a NodeRef / ``None`` (the view when
    it is filtered or sorted, else the document); ``kwargs``: ``ask_folder``, ``open_folder``,
    ``open_file``. Non-modal."""
    dlg = SplitDialog.instance(session)
    if dlg is None:
        dlg = SplitDialog(session, window, source=source, **kwargs)
        dlg.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
    elif source is not None:
        dlg.set_source(source)
    dlg.show()
    dlg.raise_()
    dlg.activateWindow()
    return dlg


def show_merge_dialog(window: QWidget | None, app: Any, open_documents: Callable[[], Iterable[Any]] | None = None,
                      *, inputs: Iterable[Any] = (), **kwargs: Any) -> MergeDialog:
    """Show the merge dialog of the application (one per application: an open one is raised and gets
    ``inputs`` added -- sessions or paths not listed yet). ``open_documents()``: the open tabs;
    ``kwargs``: ``ask_files``, ``ask_output``, ``open_folder``, ``open_file``. Non-modal."""
    dlg = MergeDialog.instance(app)
    if dlg is None:
        dlg = MergeDialog(app, open_documents, window, inputs=inputs, **kwargs)
        dlg.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
    else:
        listed = {normalize_path(inp.path) for inp in dlg.inputs()}
        for item in inputs:
            session = getattr(item, "session", item)
            path = session.path if _session_ok(session) and hasattr(session, "snapshot") else item
            try:
                key = normalize_path(path)
            except (TypeError, ValueError):
                continue
            if key not in listed:
                dlg.add_input(item)
                listed.add(key)
    dlg.show()
    dlg.raise_()
    dlg.activateWindow()
    return dlg
