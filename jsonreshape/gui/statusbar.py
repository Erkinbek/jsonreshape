"""The main window's status bar (docs/ARCHITECTURE.md §11.8; AGENT_PROMPT §11 "Status qatori").

:class:`StatusBar` follows one :class:`~jsonreshape.gui.doctab.DocumentTab` at a time
(:meth:`StatusBar.set_tab`, called when the current tab changes) and shows:

* the current node's location (``#12.contact.address``; elided, tooltip = the full
  absolute path) -- a normal widget, hidden while a temporary message is shown;
* the selection size of the active view (``view.selection_count()`` when > 1);
* the byte range of the current node (``start–end``), resolved in a silent task after a
  short debounce (never blocks: the node may be inside a record not indexed yet); an edited
  node -- its bytes live in the edit layer (an edit offset), or a node built from edited
  records (the root of an edited array) -- shows "tahrirlangan" instead (it has no place in
  the file);
* the record count -- live while indexing (``… (indekslanmoqda 45 %)``), ``shown / total``
  for a filtered / sorted / search-result view; with edits these are the counts of the
  effective (edited) document, refreshed on ``session.data_changed``;
* "● N o'zgarish" next to it while the document has unsaved changes (N = the controller's
  ``steps``; tooltip: the step Undo would take back), following the tab's
  ``edit_state_changed``;
* the format (``doc.info.label`` + record path), the encoding (+ gzip), the file size;
* the process memory (RSS through psutil every 2 s; tooltip with the memory limit; psutil
  is imported in a silent task, not on the GUI thread at start-up);
* the running background tasks (count + the first task's percentage); clicking emits
  :attr:`StatusBar.tasks_clicked` (the window shows the tasks dock).

``AppContext.status_message`` (``app.show_status("Nusxalandi")``) is shown as a temporary
message.

Layout cost: the labels have **no Python size-hint overrides** (``QStatusBar`` asks every
item for its size hint on each layout request; a Python override waited for the GIL while
workers ran). A :class:`StatusLabel`'s size comes from Qt's ``QLabel`` size hint of a
*sizing text* that changes only when the needed width changes noticeably, so a new count /
memory text of a similar length only repaints the label (no layout request); the tasks
button has a fixed width following its text in steps. Texts are set only when they changed,
and task progress refreshes are coalesced (:attr:`StatusBar.TASKS_REFRESH_MS`).
"""

from __future__ import annotations

import logging
from typing import Any

import shiboken6
from PySide6.QtCore import Qt, QTimer, Signal
from PySide6.QtGui import QPainter
from PySide6.QtWidgets import QLabel, QSizePolicy, QStatusBar, QToolButton, QWidget

from jsonreshape.errors import Cancelled
from jsonreshape.gui import copyfmt
from jsonreshape.gui.widgets import format_count, format_size
from jsonreshape.i18n import t
from jsonreshape.overlay import is_edit_offset

log = logging.getLogger(__name__)

__all__ = ["StatusBar", "StatusLabel", "encoding_text", "sticky_width"]

#: The ``psutil.Process`` of this process once a status bar imported psutil (a silent task).
_PROCESS_CACHE: dict[str, Any] = {}

_ENCODINGS = {"utf-8": "UTF-8", "utf-8-bom": "UTF-8 BOM", "utf-16-le": "UTF-16 LE", "utf-16-be": "UTF-16 BE"}


def encoding_text(encoding: str | None, compression: str | None = None) -> str:
    """``UTF-8`` / ``UTF-16 LE`` ... plus `` · gzip`` for compressed files."""
    text = _ENCODINGS.get((encoding or "").lower(), (encoding or "").upper())
    if compression:
        text = t("gui.status.encoding_compressed", encoding=text, compression=compression)
    return text


def sticky_width(current: int, need: int, step: int, cap: int | None = None) -> int:
    """The new fixed width of a widget whose content needs ``need`` pixels: unchanged
    while ``need`` fits and is not much smaller (two steps), else ``need`` rounded up to
    ``step`` (capped at ``cap``). Keeps live texts ("1 234 yozuv" -> "1 240 yozuv") from
    resizing their widget on every change."""
    step = max(1, int(step))
    if cap is not None:
        need = min(need, cap)
    if current > 0 and current - 2 * step < need <= current:
        return current
    new = -(-max(0, need) // step) * step
    if cap is not None:
        new = min(new, cap)
    return max(1, new)


class StatusLabel(QLabel):
    """A one-line label that elides its text (``…``) itself, so a long text never widens
    the window; :meth:`full_text` is the whole text and the tooltip is the one given with
    :meth:`set_tip` (plus the full text while elided).

    No size hint is computed in Python: Qt's own ``QLabel`` size hint comes from a *sizing
    text* that is replaced only when the width needed changes noticeably
    (:func:`sticky_width` rule), so a new text of a similar length ("1 234 yozuv" ->
    "1 240 yozuv") only repaints the label -- no layout request reaches the window. The
    shown text is painted (elided to the current width) by :meth:`paintEvent`.

    ``fixed=True`` (status bar permanent labels): the preferred width follows the text
    (at most ``max_chars`` average characters), the minimum is two ellipses (a narrow
    window elides instead of growing). ``fixed=False``: the layout decides the width
    (horizontal policy ``Ignored``); the text never causes a relayout.
    """

    def __init__(self, parent: QWidget | None = None, *,
                 mode: Qt.TextElideMode = Qt.TextElideMode.ElideRight, fixed: bool = True,
                 max_chars: int = 60) -> None:
        super().__init__(parent)
        self._full = ""
        self._tip = ""
        self._mode = mode
        self._fixed = bool(fixed)
        self._max_chars = max(4, int(max_chars))
        self._sizing_width = -1
        self._shown_tip: str | None = None
        self.setTextFormat(Qt.TextFormat.PlainText)
        self.setContentsMargins(6, 0, 6, 0)
        if self._fixed:
            self.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Preferred)
        else:
            self.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        self.update_metrics()

    # -- sizing ------------------------------------------------------------------------
    def update_metrics(self) -> None:
        """Recompute the size limits and the sizing text from the current font."""
        fm = self.fontMetrics()
        margins = self.contentsMargins()
        extra = margins.left() + margins.right()
        self.setMinimumWidth(fm.horizontalAdvance("…") * 2 + extra)
        if self._fixed:
            self.setMaximumWidth(fm.averageCharWidth() * self._max_chars + extra + 2)
        self._sizing_width = -1
        self._resize_for_text(force=True)
        self._update_tip()

    def _resize_for_text(self, force: bool = False) -> None:
        """Replace the sizing text when the width the full text needs changed
        noticeably (that is the only time this label asks for a new layout)."""
        if not self._fixed:
            if force:
                # one line high (a space: nothing for a screen reader to read before the
                # first text); the width comes from the layout
                QLabel.setText(self, " ")
            return
        fm = self.fontMetrics()
        need = fm.horizontalAdvance(self._full)
        step = max(4, fm.averageCharWidth() * 3)
        width = sticky_width(self._sizing_width, need, step) if self._full else 0
        if force or width != self._sizing_width:
            self._sizing_width = width
            # padded with figure spaces to the step-rounded width: the next texts of a
            # similar length fit without a new layout
            pad = max(0, width - need)
            space = max(1, fm.horizontalAdvance("\N{FIGURE SPACE}"))
            QLabel.setText(self, self._full + "\N{FIGURE SPACE}" * (-(-pad // space)))

    # -- text ----------------------------------------------------------------------------
    def setText(self, text: str) -> None:  # noqa: N802 (Qt name)
        full = copyfmt.escape_surrogates(str(text or ""))
        if full == self._full:
            return
        self._full = full
        self._resize_for_text()
        # Qt's own label text is the (padded, possibly older) sizing text: screen readers
        # get the text the label shows instead
        self.setAccessibleName(full)
        self._update_tip()
        self.update()

    def full_text(self) -> str:
        return self._full

    def text(self) -> str:  # noqa: D401 - Qt name
        """The full text (Qt's own ``QLabel`` text is only the sizing text)."""
        return self._full

    def shown_text(self) -> str:
        """The text as painted now (elided to the current width)."""
        width = max(0, self.contentsRect().width())
        return self.fontMetrics().elidedText(self._full, self._mode, width) if width else self._full

    def set_tip(self, tip: str | None) -> None:
        tip = copyfmt.escape_surrogates(tip or "")
        if tip == self._tip:
            return
        self._tip = tip
        self._update_tip()

    def tip(self) -> str:
        return self._tip

    def _update_tip(self) -> None:
        elided = self.shown_text() != self._full
        tips = [x for x in (self._full if elided else "", self._tip) if x]
        tip = "\n".join(dict.fromkeys(tips))
        if tip != self._shown_tip:
            self._shown_tip = tip
            self.setToolTip(tip)

    # -- events --------------------------------------------------------------------------
    def resizeEvent(self, event: Any) -> None:  # noqa: N802 (Qt name)
        super().resizeEvent(event)
        self._update_tip()

    def paintEvent(self, event: Any) -> None:  # noqa: N802 (Qt name)
        painter = QPainter(self)
        try:
            self.style().drawItemText(painter, self.contentsRect(), int(self.alignment()), self.palette(),
                                      self.isEnabled(), self.shown_text(), self.foregroundRole())
        finally:
            painter.end()


class _TasksButton(QToolButton):
    """The status bar's tasks button: a fixed size following its text in steps (see the
    module docstring)."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._width = 0
        self.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Fixed)
        self.update_metrics()

    def update_metrics(self) -> None:
        self.setFixedHeight(QToolButton.sizeHint(self).height())
        self._width = 0
        self._fit()

    def set_label(self, text: str) -> None:
        if text == self.text():
            return
        self.setText(text)
        self._fit()

    def _fit(self) -> None:
        need = QToolButton.sizeHint(self).width()
        width = sticky_width(self._width, need, max(4, self.fontMetrics().averageCharWidth() * 3))
        if width != self._width:
            self._width = width
            self.setFixedWidth(width)


class StatusBar(QStatusBar):
    """See the module docstring."""

    tasks_clicked = Signal()

    MEMORY_INTERVAL_MS = 2000
    OFFSET_DELAY_MS = 150
    TASKS_REFRESH_MS = 250     # task progress updates are coalesced to this interval

    def __init__(self, app: Any, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("status_bar")
        self.app = app
        self._tab: Any = None
        self._session: Any = None
        self._conns: list[tuple[Any, Any]] = []
        self._offset_serial = 0
        self._offset_task: Any = None
        self._abs_path_text: str | None = None
        self._span: tuple[int, int] | None = None
        self._edited_offset = False
        self._shut = False

        self.path_label = StatusLabel(self, mode=Qt.TextElideMode.ElideMiddle, fixed=False)
        self.path_label.setObjectName("status_path")
        self.addWidget(self.path_label, 1)

        self.selection_label = self._label("status_selection")
        self.offset_label = self._label("status_offset")
        self.count_label = self._label("status_count")
        self.dirty_label = self._label("status_dirty")
        self.dirty_label.setVisible(False)
        self.format_label = self._label("status_format")
        self.encoding_label = self._label("status_encoding")
        self.size_label = self._label("status_size")
        self.memory_label = self._label("status_memory")
        for label in (self.selection_label, self.offset_label, self.count_label, self.dirty_label,
                      self.format_label, self.encoding_label, self.size_label, self.memory_label):
            self.addPermanentWidget(label)
        self.tasks_button = _TasksButton(self)
        self.tasks_button.setObjectName("status_tasks")
        self.tasks_button.setAutoRaise(True)
        self.tasks_button.setToolTip(t("gui.status.tasks.tip"))
        self.tasks_button.clicked.connect(self.tasks_clicked)
        self.tasks_button.setVisible(False)
        self.addPermanentWidget(self.tasks_button)

        self._offset_timer = QTimer(self)
        self._offset_timer.setSingleShot(True)
        self._offset_timer.setInterval(self.OFFSET_DELAY_MS)
        self._offset_timer.timeout.connect(self._resolve_offset)
        self._tasks_timer = QTimer(self)
        self._tasks_timer.setSingleShot(True)
        self._tasks_timer.setInterval(self.TASKS_REFRESH_MS)
        self._tasks_timer.timeout.connect(self._refresh_task_texts)

        self._process: Any = None
        self._memory_timer = QTimer(self)
        self._memory_timer.setInterval(self.MEMORY_INTERVAL_MS)
        self._memory_timer.timeout.connect(self.refresh_memory)
        self.memory_label.setVisible(False)
        self._start_memory_probe()

        tasks = app.tasks
        tasks.task_added.connect(self._on_task_changed)
        tasks.task_updated.connect(self._on_task)
        tasks.task_finished.connect(self._on_task_changed)
        app.status_message.connect(self._show_message)
        settings_changed = getattr(app, "settings_changed", None)
        if settings_changed is not None:
            settings_changed.connect(self.update_metrics)
        self.refresh()

    def _start_memory_probe(self) -> None:
        """Import ``psutil`` in a silent task (15-25 ms at start-up, not on the GUI
        thread); the memory label appears once the process object is there."""
        cached = _PROCESS_CACHE.get("process")
        if cached is not None:
            self._memory_ready(cached)
            return
        tasks = getattr(self.app, "tasks", None)
        if tasks is None:   # pragma: no cover - a bare context
            return

        def work(_ctx: Any) -> Any:
            import psutil

            return psutil.Process()

        tasks.run(work, title=t("gui.status.task.memory"), kind="memory", owner=self, silent=True,
                  on_done=self._memory_ready, on_error=self._memory_unavailable)

    def _memory_ready(self, process: Any) -> None:
        _PROCESS_CACHE["process"] = process
        if self._shut or not shiboken6.isValid(self):
            return
        self._process = process
        self._memory_timer.start()
        self.refresh_memory()

    def _memory_unavailable(self, exc: BaseException) -> None:
        """psutil missing or unusable: no memory display."""
        log.info("process memory display unavailable: %r", exc)
        if not self._shut and shiboken6.isValid(self):
            self.memory_label.setVisible(False)

    def _label(self, name: str) -> StatusLabel:
        label = StatusLabel(self)
        label.setObjectName(name)
        return label

    def labels(self) -> list[StatusLabel]:
        return [self.path_label, self.selection_label, self.offset_label, self.count_label, self.dirty_label,
                self.format_label, self.encoding_label, self.size_label, self.memory_label]

    def update_metrics(self) -> None:
        """The font may have changed (settings): recompute the fixed sizes."""
        for label in self.labels():
            label.update_metrics()
        self.tasks_button.update_metrics()

    # ------------------------------------------------------------------ binding
    def tab(self) -> Any:
        return self._tab

    def set_tab(self, tab: Any) -> None:
        """Follow ``tab`` (a DocumentTab or ``None``)."""
        if tab is self._tab:
            self.refresh()
            return
        self._disconnect()
        self._cancel_offset()
        self._tab = tab
        self._session = getattr(tab, "session", None) if tab is not None else None
        s = self._session
        if s is not None:
            self._connect(s.state_changed, self._on_doc_changed)
            self._connect(s.opened, self._on_doc_changed)
            self._connect(s.index_finished, self._on_doc_changed)
            self._connect(s.warnings_changed, self._on_doc_changed)
            self._connect(s.count_changed, self.refresh_count)
            self._connect(s.view_changed, self.refresh_count)
            self._connect(s.view_busy_changed, self.refresh_count)
            self._connect(s.current_changed, self._on_current_changed)
            data_changed = getattr(s, "data_changed", None)
            if data_changed is not None and hasattr(data_changed, "connect"):
                self._connect(data_changed, self._on_data_changed)
        if tab is not None:
            for name, slot in (("active_view_changed", self.refresh_selection),
                               ("selection_changed", self.refresh_selection),
                               ("edit_state_changed", self.refresh_dirty)):
                signal = getattr(tab, name, None)
                if signal is not None:
                    self._connect(signal, slot)
        self.refresh()

    def _connect(self, signal: Any, slot: Any) -> None:
        signal.connect(slot)
        self._conns.append((signal, slot))

    def _disconnect(self) -> None:
        for signal, slot in self._conns:
            try:
                signal.disconnect(slot)
            except (RuntimeError, TypeError):   # the sender is gone already
                pass
        self._conns.clear()

    def shutdown(self) -> None:
        """Stop the timers and forget the tab (window closing)."""
        self._shut = True
        self._disconnect()
        self._cancel_offset()
        tasks = getattr(self.app, "tasks", None)
        if tasks is not None:
            tasks.detach(self)       # the memory probe: nothing calls back after closing
        self._memory_timer.stop()
        self._offset_timer.stop()
        self._tasks_timer.stop()
        self._tab = self._session = None

    # ------------------------------------------------------------------ refresh
    def refresh(self) -> None:
        self.refresh_document()
        self.refresh_count()
        self.refresh_dirty()
        self.refresh_path()
        self.refresh_selection()
        self.refresh_tasks()
        self.refresh_memory()
        self._schedule_offset()

    def _on_doc_changed(self, *_args: Any) -> None:
        self.refresh_document()
        self.refresh_count()
        self.refresh_dirty()

    def _on_data_changed(self, *_args: Any) -> None:
        """An edit was applied: counts, the dirty mark and the current node's place may differ."""
        self.refresh_count()
        self.refresh_dirty()
        self._abs_path_text = None
        self.refresh_path()
        self._schedule_offset()

    def _edit(self) -> Any:
        s = self._session
        if s is None or getattr(s, "closed", False):
            return None
        return getattr(s, "edit", None)

    def dirty_text(self) -> str:
        """"● N o'zgarish" while the document has unsaved changes, else ``""``."""
        edit = self._edit()
        if edit is None or not bool(getattr(edit, "dirty", False)):
            return ""
        steps = int(getattr(edit, "steps", 0) or 0)
        return t("gui.status.dirty", count=format_count(steps))

    def refresh_dirty(self, *_args: Any) -> None:
        text = self.dirty_text()
        self.dirty_label.setText(text)
        if text:
            edit = self._edit()
            undo = str(getattr(edit, "undo_text", "") or "") if edit is not None else ""
            tip = t("gui.status.dirty.tip")
            if undo:
                tip += "\n" + t("gui.status.dirty.undo_tip", label=undo)
            self.dirty_label.set_tip(tip)
        else:
            self.dirty_label.set_tip("")
        if self.dirty_label.isHidden() == bool(text):
            self.dirty_label.setVisible(bool(text))

    def _on_current_changed(self, *_args: Any) -> None:
        self._abs_path_text = None
        self.refresh_path()
        self.refresh_selection()
        self._schedule_offset()

    def refresh_document(self) -> None:
        """Size, format and encoding of the document."""
        s = self._session
        doc = getattr(s, "doc", None) if s is not None else None
        if doc is None:
            for label in (self.size_label, self.format_label, self.encoding_label):
                label.setText("")
                label.set_tip("")
                label.setVisible(False)
            return
        size = int(doc.size)
        self.size_label.setText(format_size(size))
        tip = t("gui.status.size.tip", size=format_count(size))
        data_size = int(getattr(doc, "data_size", size) or size)
        if data_size != size:
            tip += "\n" + t("gui.status.size.data_tip", size=format_size(data_size))
        appended = int(getattr(doc, "appended_bytes", 0) or 0)
        if appended:
            tip += "\n" + t("gui.status.size.appended_tip", size=format_size(appended))
        self.size_label.set_tip(tip)
        self.size_label.setVisible(True)

        info = doc.info
        text = info.label
        if info.record_path:
            text = t("gui.status.format_path", format=text, path=info.record_path_text)
        self.format_label.setText(text)
        tips = [t("gui.status.format.auto") if info.auto else t("gui.status.format.forced")]
        broken = int(getattr(doc, "broken_count", 0) or 0)
        if broken:
            tips.append(t("gui.status.format.broken", count=format_count(broken)))
        self.format_label.set_tip(text + "\n" + "\n".join(tips))
        self.format_label.setVisible(True)

        self.encoding_label.setText(encoding_text(info.encoding, info.compression))
        enc_tip = ""
        try:
            if str(doc.data_path) != str(doc.source_path):
                enc_tip = t("gui.status.encoding.converted_tip", path=str(doc.data_path))
        except Exception:
            log.debug("data path unavailable", exc_info=True)
        self.encoding_label.set_tip(enc_tip)
        self.encoding_label.setVisible(True)

    def count_text(self) -> str:
        """The record count text (see the module docstring)."""
        s = self._session
        if s is None:
            return ""
        doc = s.doc
        if doc is None:
            if s.state in ("new", "opening"):
                return t("gui.status.opening")
            if s.state == "failed":
                return t("gui.status.failed")
            return ""
        if s.state == "failed":
            # the document opened but its indexing was cancelled or failed: the count is incomplete
            text = t("gui.status.records", count=format_count(int(doc.record_count)))
            key = "gui.status.index_cancelled" if isinstance(s.open_error, Cancelled) else "gui.status.index_failed"
            return t(key, text=text)
        if doc.kind == "single":
            text = t("gui.status.single")
        else:
            total = int(doc.record_count)
            if s.view_is_base:
                text = t("gui.status.records", count=format_count(total))
            else:
                text = t("gui.status.records_view", shown=format_count(s.view_len()), total=format_count(total))
        if s.state == "indexing":
            percent = self._index_percent()
            if percent is None:
                text = t("gui.status.indexing_unknown", text=text)
            else:
                text = t("gui.status.indexing", text=text, percent=percent)
        elif doc.error is not None:
            text = t("gui.status.partial", text=text)
        if s.view_busy:
            text = t("gui.status.view_busy", text=text)
        return text

    def refresh_count(self, *_args: Any) -> None:
        s = self._session
        text = self.count_text()
        self.count_label.setText(text)
        self.count_label.setVisible(bool(text))
        tip = ""
        if s is not None and s.doc is not None:
            try:
                tip = s.view_label()
            except Exception:
                log.debug("view_label failed", exc_info=True)
        self.count_label.set_tip(tip)

    def _index_percent(self) -> int | None:
        s = self._session
        if s is None:
            return None
        for task in self.app.tasks.tasks(owner=s):
            if task.kind == "index":
                info = task.info()
                if info is not None and info.fraction is not None:
                    return int(info.fraction * 100)
        return None

    def refresh_path(self) -> None:
        s = self._session
        ref = getattr(s, "current", None) if s is not None else None
        if ref is None:
            self.path_label.setText("")
            self.path_label.set_tip("")
            return
        try:
            text = s.location_text(ref)
        except Exception:
            log.debug("location_text failed", exc_info=True)
            text = ""
        self.path_label.setText(text)
        full = self._abs_path_text
        self.path_label.set_tip(copyfmt.escape_surrogates(full if full else text))

    def refresh_selection(self, *_args: Any) -> None:
        view = None
        if self._tab is not None:
            try:
                view = self._tab.active_view()
            except Exception:
                log.debug("active_view failed", exc_info=True)
        count = 0
        fn = getattr(view, "selection_count", None)
        if callable(fn):
            try:
                count = int(fn() or 0)
            except Exception:
                log.debug("selection_count failed", exc_info=True)
        if count > 1:
            self.selection_label.setText(t("gui.status.selection", count=format_count(count)))
            self.selection_label.setVisible(True)
        else:
            self.selection_label.setText("")
            self.selection_label.setVisible(False)

    # ------------------------------------------------------------------ offset
    def _schedule_offset(self) -> None:
        s = self._session
        if s is None or getattr(s, "current", None) is None or s.doc is None:
            self._cancel_offset()
            self._set_offset(None)
            return
        self._offset_timer.start()

    def _cancel_offset(self) -> None:
        self._offset_timer.stop()
        self._offset_serial += 1
        if self._offset_task is not None:
            try:
                self.app.tasks.detach(self)
            except Exception:
                log.debug("detach failed", exc_info=True)
            self._offset_task = None

    def _resolve_offset(self) -> None:
        s = self._session
        ref = getattr(s, "current", None) if s is not None else None
        if s is None or ref is None or s.doc is None:
            self._set_offset(None)
            return
        if self._offset_task is not None:
            self.app.tasks.detach(self)
        self._offset_serial += 1
        serial = self._offset_serial
        session = s
        doc = s.doc

        def work(ctx: Any) -> tuple[int, int, str | None, bool]:
            node = session.node(ref, ctx.cancel)
            try:
                path = copyfmt.path_text(session.absolute_path(ref), "dotted") or "$"
            except Exception:
                path = None
            start, end = int(node.start), int(node.end)
            # bytes of the edit layer, or text built from edited records: no place in the file
            edited = is_edit_offset(start) or (bool(getattr(node, "is_composite", False))
                                               and bool(getattr(doc, "edited", False)))
            return start, end, path, edited

        self._offset_task = self.app.tasks.run(
            work, title=t("gui.status.task.offset"), kind="offset", owner=self, silent=True,
            on_done=lambda result, n=serial: self._offset_done(n, result),
            on_error=lambda exc, n=serial: self._offset_failed(n, exc),
            on_cancel=lambda n=serial: self._offset_cancelled(n))

    def _offset_done(self, serial: int, result: tuple) -> None:
        if serial != self._offset_serial:
            return
        self._offset_task = None
        start, end, path = result[:3]
        edited = bool(result[3]) if len(result) > 3 else False
        self._abs_path_text = path
        self._set_offset((start, end), edited=edited)
        self.refresh_path()

    def _offset_failed(self, serial: int, exc: BaseException) -> None:
        if serial != self._offset_serial:
            return
        self._offset_task = None
        log.debug("resolving the current node failed: %r", exc)
        self._set_offset(None)

    def _offset_cancelled(self, serial: int) -> None:
        if serial == self._offset_serial:
            self._offset_task = None

    def _set_offset(self, span: tuple[int, int] | None, *, edited: bool = False) -> None:
        self._span = None if edited else span
        self._edited_offset = bool(edited and span is not None)
        if span is None:
            self.offset_label.setText("")
            self.offset_label.set_tip("")
            self.offset_label.setVisible(False)
            return
        if edited:
            self.offset_label.setText(t("gui.status.offset.edited"))
            self.offset_label.set_tip(t("gui.status.offset.edited.tip"))
            self.offset_label.setVisible(True)
            return
        start, end = span
        self.offset_label.setText(t("gui.status.offset", start=format_count(start), end=format_count(end)))
        self.offset_label.set_tip(t("gui.status.offset.tip", start=start, end=end,
                                       size=format_size(max(0, end - start))))
        self.offset_label.setVisible(True)

    def offset_span(self) -> tuple[int, int] | None:
        """The byte range shown, ``None`` when none is (also for an edited node)."""
        return self._span

    def offset_edited(self) -> bool:
        """The current node is edited: "tahrirlangan" is shown instead of a byte range."""
        return self._edited_offset

    # ------------------------------------------------------------------ memory / tasks
    def refresh_memory(self) -> None:
        proc = self._process
        if proc is None:
            return
        try:
            rss = int(proc.memory_info().rss)
        except Exception:
            log.debug("memory_info failed", exc_info=True)
            self.memory_label.setVisible(False)
            return
        self.memory_label.setText(t("gui.status.memory", size=format_size(rss)))
        limit = int(getattr(getattr(self.app, "settings", None), "memory_limit_bytes", 0) or 0)
        self.memory_label.set_tip(t("gui.status.memory.tip", size=format_size(rss), limit=format_size(limit)))
        self.memory_label.setVisible(True)

    def _on_task_changed(self, task: Any) -> None:
        """A task started or finished: the button at once."""
        self.refresh_tasks()
        if self._is_index_task(task):
            self.refresh_count()

    def _on_task(self, task: Any) -> None:
        """Progress of a running task (every poll of every task): coalesced."""
        if not self._tasks_timer.isActive():
            self._tasks_timer.start()

    def _is_index_task(self, task: Any) -> bool:
        return self._session is not None and getattr(task, "owner", None) is self._session \
            and getattr(task, "kind", "") == "index"

    def _refresh_task_texts(self) -> None:
        self.refresh_tasks()
        if self._session is not None and self._session.state == "indexing":
            self.refresh_count()

    def refresh_tasks(self) -> None:
        running = [task for task in self.app.tasks.tasks() if task.state == "running"]
        if not running:
            self.tasks_button.set_label("")
            self.tasks_button.setVisible(False)
            return
        text = t("gui.status.tasks", count=len(running))
        info = running[0].info()
        if info is not None and info.fraction is not None:
            text = t("gui.status.tasks_percent", text=text, percent=int(info.fraction * 100))
        self.tasks_button.set_label(text)
        self.tasks_button.setVisible(True)

    def _show_message(self, text: str, timeout: int) -> None:
        self.showMessage(text, max(0, int(timeout)))
