"""The background tasks panel (docs/ARCHITECTURE.md §11.8; AGENT_PROMPT §11 "Fon vazifalari
paneli").

:class:`TasksPanel` lists the running (non-silent) tasks of the
:class:`~jsonreshape.gui.qtasks.TaskManager`: title (+ the task's progress message), a
progress bar (indeterminate while the total is unknown), done / total in the task's unit,
the rate (``MB/s`` for bytes), the remaining time, the state and a cancel button per task.
"Hammasini bekor qilish" cancels every listed task. A finished task stays listed for
:data:`KEEP_FINISHED_MS` with its final state (a failed one in red with the error text as
tooltip), then disappears -- unless "keep finished tasks" is checked; "Tozalash" removes
the finished rows at once. The task list is small (one row per running task), so a normal
:class:`QTreeWidget` is used.

Updates are cheap: a row's texts and its bar value are set only when they changed, so
the 100 ms task polls cause no repaint or layout work while nothing changes.
"""

from __future__ import annotations

import logging
import time
from typing import Any

from PySide6.QtCore import QTimer, Signal
from PySide6.QtGui import QBrush, QColor
from PySide6.QtWidgets import (
    QAbstractItemView,
    QCheckBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QProgressBar,
    QPushButton,
    QToolButton,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)

from jsonreshape.errors import JsonReshapeError
from jsonreshape.gui import theme
from jsonreshape.gui.qtasks import state_text
from jsonreshape.i18n import t
from jsonreshape.tasks import ProgressInfo, format_bytes, format_duration

log = logging.getLogger(__name__)

__all__ = ["KEEP_FINISHED_MS", "TasksPanel", "bar_value", "error_text", "eta_text", "progress_texts",
           "title_text"]

#: Finished tasks stay listed this long (unless "keep finished" is on).
KEEP_FINISHED_MS = 4000
_PRUNE_MS = 500
_BAR_STEPS = 1000

COL_TITLE, COL_PROGRESS, COL_DONE, COL_RATE, COL_ETA, COL_STATE, COL_CANCEL = range(7)


def _count(n: float) -> str:
    from jsonreshape.gui.widgets import format_count

    return format_count(int(n))


def _rate_count(n: float) -> str:
    """A rate of items per second: one decimal below 10 (``0.4``), else grouped."""
    if 0 < n < 10:
        return f"{n:.1f}"
    return _count(n)


def eta_text(seconds: float | None) -> str:
    """The remaining time: ``format_duration`` below a day, ``3 kun 4 soat`` above;
    empty when unknown (``None`` / NaN / infinite)."""
    if seconds is None or seconds != seconds or seconds in (float("inf"), float("-inf")):
        return ""
    seconds = max(0.0, float(seconds))
    if seconds < 86400:
        return format_duration(seconds)
    days, rest = divmod(int(seconds), 86400)
    return t("gui.taskspanel.eta_days", days=_count(days), hours=rest // 3600)


def title_text(title: str, message: str | None) -> str:
    """The task title with its progress message, without saying the same thing twice
    (``Indekslanmoqda: a.json`` + the same message = the title alone)."""
    message = (message or "").strip()
    if not message or message == title or message in title:
        return title
    if title and title in message:
        return message
    return t("gui.taskspanel.title_message", title=title, message=message)


def bar_value(fraction: float | None, finished: bool = False) -> int:
    """Progress bar steps for ``fraction``: rounded down and below the end until the
    task finished (99.96 % is not shown as 100 %)."""
    if fraction is None or fraction != fraction:
        return 0
    value = int(max(0.0, min(1.0, float(fraction))) * _BAR_STEPS)
    return value if finished else min(value, _BAR_STEPS - 1)


def progress_texts(info: ProgressInfo | None) -> tuple[str, str, str]:
    """``(done / total, rate, eta)`` texts of a progress report (empty when unknown).

    Bytes are shown with :func:`~jsonreshape.tasks.format_bytes` (``12.3 MB / 1.2 GB``,
    ``45.0 MB/s``), other units as grouped counts."""
    if info is None:
        return "", "", ""
    is_bytes = info.unit == "bytes"
    fmt = format_bytes if is_bytes else _count
    done = fmt(info.done)
    if info.total:
        done = t("gui.taskspanel.done_total", done=done, total=fmt(info.total))
    rate = ""
    if info.rate > 0 and info.done > 0:
        rate = t("gui.taskspanel.rate", rate=format_bytes(info.rate) if is_bytes else _rate_count(info.rate))
    eta = ""
    if info.total and info.eta is not None and info.done < info.total and not info.finished:
        eta = eta_text(info.eta)
    return done, rate, eta


def error_text(exc: BaseException | None) -> str:
    """The localized text of a task error (the message of expected errors)."""
    if exc is None:
        return ""
    if isinstance(exc, JsonReshapeError):
        return exc.message
    text = str(exc)
    name = type(exc).__name__
    return f"{name}: {text}" if text else name


class _Row:
    __slots__ = ("task", "item", "bar", "cancel", "finished_at", "texts")

    def __init__(self, task: Any, item: QTreeWidgetItem, bar: QProgressBar, cancel: QToolButton) -> None:
        self.task = task
        self.item = item
        self.bar = bar
        self.cancel = cancel
        self.finished_at: float | None = None
        self.texts: dict[tuple[int, int], str] = {}   # (column, 0 text / 1 tooltip) -> what was set last

    def set_text(self, column: int, text: str) -> None:
        if self.texts.get((column, 0)) != text:
            self.texts[(column, 0)] = text
            self.item.setText(column, text)

    def set_tip(self, column: int, text: str) -> None:
        if self.texts.get((column, 1)) != text:
            self.texts[(column, 1)] = text
            self.item.setToolTip(column, text)


class TasksPanel(QWidget):
    """The tasks dock widget (see the module docstring)."""

    count_changed = Signal(int)       # running tasks listed

    def __init__(self, app: Any, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.app = app
        self.manager = app.tasks
        self._rows: dict[int, _Row] = {}
        self._keep = False
        try:
            self._keep = bool(app.state.get("tasks_keep_finished", False))
        except Exception:
            log.debug("could not read tasks_keep_finished", exc_info=True)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(4, 4, 4, 4)
        layout.setSpacing(4)
        self.tree = QTreeWidget(self)
        self.tree.setObjectName("tasks_tree")
        self.tree.setColumnCount(7)
        self.tree.setHeaderLabels([
            t("gui.taskspanel.col.title"), t("gui.taskspanel.col.progress"), t("gui.taskspanel.col.done"),
            t("gui.taskspanel.col.rate"), t("gui.taskspanel.col.eta"), t("gui.taskspanel.col.state"), "",
        ])
        self.tree.setRootIsDecorated(False)
        self.tree.setUniformRowHeights(True)
        self.tree.setSelectionMode(QAbstractItemView.SelectionMode.NoSelection)
        header = self.tree.header()
        header.setStretchLastSection(False)
        header.setSectionResizeMode(COL_TITLE, QHeaderView.ResizeMode.Stretch)
        for col, width in ((COL_PROGRESS, 140), (COL_DONE, 150), (COL_RATE, 90), (COL_ETA, 70),
                           (COL_STATE, 120), (COL_CANCEL, 32)):
            header.setSectionResizeMode(col, QHeaderView.ResizeMode.Interactive)
            self.tree.setColumnWidth(col, width)
        layout.addWidget(self.tree, 1)

        self.empty_label = QLabel(t("gui.taskspanel.empty"), self)
        self.empty_label.setEnabled(False)
        layout.addWidget(self.empty_label)

        bottom = QHBoxLayout()
        self.keep_check = QCheckBox(t("gui.taskspanel.keep"), self)
        self.keep_check.setToolTip(t("gui.taskspanel.keep.tip"))
        self.keep_check.setChecked(self._keep)
        self.keep_check.toggled.connect(self.set_keep_finished)
        bottom.addWidget(self.keep_check)
        bottom.addStretch(1)
        self.clear_button = QPushButton(t("gui.taskspanel.clear"), self)
        self.clear_button.setToolTip(t("gui.taskspanel.clear.tip"))
        self.clear_button.setAutoDefault(False)
        self.clear_button.clicked.connect(self.clear_finished)
        bottom.addWidget(self.clear_button)
        self.cancel_all_button = QPushButton(t("gui.taskspanel.cancel_all"), self)
        self.cancel_all_button.setAutoDefault(False)
        self.cancel_all_button.clicked.connect(self.cancel_all)
        bottom.addWidget(self.cancel_all_button)
        layout.addLayout(bottom)

        self._prune_timer = QTimer(self)
        self._prune_timer.setInterval(_PRUNE_MS)
        self._prune_timer.timeout.connect(self._prune)

        self.manager.task_added.connect(self._on_added)
        self.manager.task_updated.connect(self._on_updated)
        self.manager.task_finished.connect(self._on_finished)
        for task in self.manager.tasks():
            self._add(task)
        self._sync()

    # ------------------------------------------------------------------ public
    def row_count(self) -> int:
        """Rows listed (running and recently finished tasks)."""
        return len(self._rows)

    def running_count(self) -> int:
        return sum(1 for row in self._rows.values() if row.finished_at is None)

    def task_ids(self) -> list[int]:
        return list(self._rows)

    def item_for(self, task: Any) -> QTreeWidgetItem | None:
        row = self._rows.get(task.id)
        return row.item if row is not None else None

    def progress_bar(self, task: Any) -> QProgressBar | None:
        row = self._rows.get(task.id)
        return row.bar if row is not None else None

    def cancel_button(self, task: Any) -> QToolButton | None:
        row = self._rows.get(task.id)
        return row.cancel if row is not None else None

    def keep_finished(self) -> bool:
        return self._keep

    def set_keep_finished(self, on: bool) -> None:
        """Keep finished tasks listed (until "Tozalash") instead of removing them after
        :data:`KEEP_FINISHED_MS`. Remembered in the GUI state."""
        on = bool(on)
        if self.keep_check.isChecked() != on:
            self.keep_check.setChecked(on)
            return
        self._keep = on
        try:
            self.app.state.set("tasks_keep_finished", on)
        except Exception:
            log.debug("could not store tasks_keep_finished", exc_info=True)
        self._sync()

    def cancel_all(self) -> None:
        """Cancel every listed running task (silent helper tasks are not touched)."""
        for row in list(self._rows.values()):
            if row.finished_at is None:
                self._cancel(row)

    def clear_finished(self) -> None:
        for task_id, row in list(self._rows.items()):
            if row.finished_at is not None:
                self._remove(task_id)
        self._sync()

    # ------------------------------------------------------------------ rows
    def _add(self, task: Any) -> _Row:
        row = self._rows.get(task.id)
        if row is not None:
            return row
        item = QTreeWidgetItem(self.tree)
        bar = QProgressBar(self.tree)
        bar.setTextVisible(True)
        bar.setMaximumHeight(max(12, self.tree.fontMetrics().height() + 2))
        cancel = QToolButton(self.tree)
        cancel.setText("✕")
        cancel.setAutoRaise(True)
        cancel.setToolTip(t("gui.taskspanel.cancel.tip"))
        cancel.setAccessibleName(t("gui.taskspanel.cancel.tip"))
        self.tree.setItemWidget(item, COL_PROGRESS, bar)
        self.tree.setItemWidget(item, COL_CANCEL, cancel)
        row = _Row(task, item, bar, cancel)
        row.set_text(COL_TITLE, task.title)
        row.set_tip(COL_TITLE, task.title)
        cancel.clicked.connect(lambda _checked=False, r=row: self._cancel(r))
        self._rows[task.id] = row
        self._update(row)
        return row

    def _remove(self, task_id: int) -> None:
        row = self._rows.pop(task_id, None)
        if row is None:
            return
        index = self.tree.indexOfTopLevelItem(row.item)
        if index >= 0:
            self.tree.takeTopLevelItem(index)
        row.bar.deleteLater()
        row.cancel.deleteLater()

    def _cancel(self, row: _Row) -> None:
        if row.finished_at is not None:
            return
        try:
            row.task.cancel()
        except Exception:
            log.exception("cancelling %r failed", row.task)
        row.cancel.setEnabled(False)
        row.set_text(COL_STATE, t("gui.taskspanel.cancelling"))

    def _update(self, row: _Row) -> None:
        task = row.task
        if task is None:          # finished: the row keeps its final texts
            return
        info = task.info()
        title = title_text(task.title, info.message if info is not None else None)
        row.set_text(COL_TITLE, title)
        row.set_tip(COL_TITLE, title)
        done, rate, eta = progress_texts(info)
        row.set_text(COL_DONE, done)
        row.set_text(COL_RATE, rate)
        row.set_text(COL_ETA, eta)
        state = task.state
        if state == "running":
            if task.cancel_requested:
                row.set_text(COL_STATE, t("gui.taskspanel.cancelling"))
                row.cancel.setEnabled(False)
            else:
                row.set_text(COL_STATE, state_text(state))
            if info is None or info.fraction is None:
                if row.bar.maximum() != 0:
                    row.bar.setRange(0, 0)      # indeterminate
            else:
                if row.bar.maximum() != _BAR_STEPS:
                    row.bar.setRange(0, _BAR_STEPS)
                value = bar_value(info.fraction, bool(info.finished))
                if row.bar.value() != value:
                    row.bar.setValue(value)
            return
        # finished
        row.set_text(COL_STATE, state_text(state))
        row.set_text(COL_ETA, "")
        row.cancel.setEnabled(False)
        row.cancel.setVisible(False)
        if row.bar.maximum() == 0:
            row.bar.setRange(0, _BAR_STEPS)
            row.bar.setValue(_BAR_STEPS if state == "done" else 0)
        elif state == "done":
            row.bar.setValue(_BAR_STEPS)
        if state == "failed":
            text = error_text(task.error)
            red = QBrush(self._danger_color())
            for col in (COL_TITLE, COL_STATE):
                row.item.setForeground(col, red)
            row.set_tip(COL_STATE, text)
            row.set_tip(COL_TITLE, f"{title}\n{text}" if text else title)
        elif state == "cancelled":
            muted = QBrush(self._muted_color())
            row.item.setForeground(COL_STATE, muted)

    @staticmethod
    def _danger_color() -> QColor:
        return QColor(theme.color("danger"))

    @staticmethod
    def _muted_color() -> QColor:
        return QColor(theme.color("muted"))

    # ------------------------------------------------------------------ manager signals
    def _on_added(self, task: Any) -> None:
        if getattr(task, "silent", False):
            return
        self._add(task)
        self._sync()

    def _on_updated(self, task: Any) -> None:
        row = self._rows.get(task.id)
        if row is None:
            if getattr(task, "silent", False) or task.state != "running":
                return
            row = self._add(task)
            self._sync()
            return
        self._update(row)

    def _on_finished(self, task: Any) -> None:
        if getattr(task, "silent", False):
            return
        row = self._rows.get(task.id) or self._add(task)
        row.finished_at = time.monotonic()
        self._update(row)
        # the row shows the final texts from now on: holding the task would keep its owner
        # (a closed tab's session, a dialog) alive while the row stays listed
        row.task = None
        self._sync()

    def _prune(self) -> None:
        if self._keep:
            self._prune_timer.stop()
            return
        now = time.monotonic()
        limit = KEEP_FINISHED_MS / 1000.0
        for task_id, row in list(self._rows.items()):
            if row.finished_at is not None and now - row.finished_at >= limit:
                self._remove(task_id)
        self._sync()

    def _sync(self) -> None:
        finished = any(row.finished_at is not None for row in self._rows.values())
        if finished and not self._keep:
            if not self._prune_timer.isActive():
                self._prune_timer.start()
        elif self._prune_timer.isActive():
            self._prune_timer.stop()
        running = self.running_count()
        self.cancel_all_button.setEnabled(running > 0)
        self.clear_button.setEnabled(finished)
        self.empty_label.setVisible(not self._rows)
        self.count_changed.emit(running)
