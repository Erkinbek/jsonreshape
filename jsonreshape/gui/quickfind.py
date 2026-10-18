"""Quick find bar (Ctrl+F): search in the part of a view that is already loaded.

AGENT_PROMPT §6.1: typing finds at once and highlights the matches; F3 / Shift+F3 move to
the next / previous match. The bar only collects the text and options; the owning view
searches its loaded rows with :func:`make_matcher` (the same casefold and apostrophe
folding as the full-file search) and reports back with :meth:`QuickFindBar.set_status`.
"""

from __future__ import annotations

import logging
from typing import Callable

from PySide6.QtCore import Qt, QTimer, Signal
from PySide6.QtGui import QKeyEvent
from PySide6.QtWidgets import QCheckBox, QFrame, QHBoxLayout, QLabel, QLineEdit, QToolButton, QWidget

from jsonreshape.errors import JsonReshapeError
from jsonreshape.i18n import t
from jsonreshape.textmatch import TextQuery

log = logging.getLogger(__name__)

#: Delay between the last keystroke and ``search_changed``.
DEBOUNCE_MS = 150

Matcher = Callable[[str], "list[tuple[int, int]]"]


def make_matcher(text: str, case_sensitive: bool = False) -> Matcher | None:
    """A function returning the match spans of ``text`` in a string (plain mode of
    :class:`~jsonreshape.textmatch.TextQuery`: casefold unless ``case_sensitive``, all
    apostrophe variants equal). ``None`` for an empty or unusable text."""
    if not text:
        return None
    try:
        query = TextQuery(text, mode="plain", case_sensitive=case_sensitive)
    except JsonReshapeError:
        return None
    return query.find_spans


class _FindEdit(QLineEdit):
    """Line edit that turns Enter / Shift+Enter / Escape into bar actions."""

    def __init__(self, bar: QuickFindBar) -> None:
        super().__init__(bar)
        self._bar = bar

    def keyPressEvent(self, event: QKeyEvent) -> None:  # noqa: N802 (Qt name)
        key = event.key()
        if key in (Qt.Key.Key_Return, Qt.Key.Key_Enter):
            self._bar.flush()
            if event.modifiers() & Qt.KeyboardModifier.ShiftModifier:
                self._bar.prev_requested.emit()
            else:
                self._bar.next_requested.emit()
            event.accept()
            return
        if key == Qt.Key.Key_Escape:
            self._bar.close_bar()
            event.accept()
            return
        if key == Qt.Key.Key_F3:
            self._bar.flush()
            if event.modifiers() & Qt.KeyboardModifier.ShiftModifier:
                self._bar.prev_requested.emit()
            else:
                self._bar.next_requested.emit()
            event.accept()
            return
        super().keyPressEvent(event)


class QuickFindBar(QFrame):
    """A slim find bar placed under a view (hidden until :meth:`open_bar`).

    Signals: ``search_changed(text, case_sensitive)`` (debounced; empty text = clear the
    highlights), ``next_requested()``, ``prev_requested()``, ``closed()``.
    """

    search_changed = Signal(str, bool)
    next_requested = Signal()
    prev_requested = Signal()
    closed = Signal()

    def __init__(self, parent: QWidget | None = None, *, hint: str | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("QuickFindBar")
        self.setFrameShape(QFrame.Shape.StyledPanel)
        lay = QHBoxLayout(self)
        lay.setContentsMargins(6, 3, 6, 3)
        lay.setSpacing(6)
        self._label = QLabel(t("gui.quickfind.label"), self)
        self._edit = _FindEdit(self)
        self._edit.setPlaceholderText(t("gui.quickfind.placeholder"))
        self._edit.setClearButtonEnabled(True)
        self._edit.setMinimumWidth(220)
        self._case = QCheckBox(t("gui.quickfind.case"), self)
        self._case.setToolTip(t("gui.quickfind.case.tip"))
        self._prev = QToolButton(self)
        self._prev.setText("▲")
        self._prev.setToolTip(t("gui.quickfind.prev.tip"))
        self._next = QToolButton(self)
        self._next.setText("▼")
        self._next.setToolTip(t("gui.quickfind.next.tip"))
        self._status = QLabel("", self)
        self._status.setMinimumWidth(90)
        self._hint = QLabel(hint if hint is not None else t("gui.quickfind.hint"), self)
        self._hint.setEnabled(False)  # muted
        close = QToolButton(self)
        close.setText("✕")
        close.setToolTip(t("gui.quickfind.close.tip"))
        close.setAutoRaise(True)
        for w in (self._label, self._edit, self._case, self._prev, self._next, self._status):
            lay.addWidget(w)
        lay.addWidget(self._hint, 1)
        lay.addWidget(close)
        self._timer = QTimer(self)
        self._timer.setSingleShot(True)
        self._timer.setInterval(DEBOUNCE_MS)
        self._timer.timeout.connect(self._emit_search)
        self._edit.textChanged.connect(lambda _text: self._timer.start())
        self._case.toggled.connect(lambda _on: self._timer.start())
        self._prev.clicked.connect(self._on_prev)
        self._next.clicked.connect(self._on_next)
        close.clicked.connect(self.close_bar)
        self._last: tuple[str, bool] | None = None
        self.hide()

    # -- public API --------------------------------------------------------------------
    def open_bar(self, text: str | None = None) -> None:
        """Show the bar and focus the text (optionally replacing it), all selected."""
        if text:
            self._edit.setText(text)
        self.show()
        self._edit.setFocus(Qt.FocusReason.ShortcutFocusReason)
        self._edit.selectAll()
        if self._edit.text():
            self.flush()

    def close_bar(self) -> None:
        """Hide the bar and clear the highlights (``search_changed("")`` + ``closed``)."""
        if not self.isVisible():
            return
        self._timer.stop()
        self.hide()
        self._last = None
        self.search_changed.emit("", self.case_sensitive())
        self.closed.emit()

    def text(self) -> str:
        return self._edit.text()

    def case_sensitive(self) -> bool:
        return self._case.isChecked()

    def matcher(self) -> Matcher | None:
        """:func:`make_matcher` for the current text and options."""
        return make_matcher(self.text(), self.case_sensitive())

    def set_status(self, text: str, state: str = "neutral") -> None:
        """Show ``text`` next to the arrows; ``state``: ``neutral`` | ``found`` | ``notfound``
        (``notfound`` also tints the text field)."""
        self._status.setText(text)
        self._edit.setProperty("notfound", state == "notfound")
        self._edit.setStyleSheet("QLineEdit { background: #f5c6c6; color: #17202c; }"
                                 if state == "notfound" else "")

    def status_text(self) -> str:
        return self._status.text()

    def flush(self) -> None:
        """Emit a pending ``search_changed`` now (Enter must not wait for the debounce)."""
        if self._timer.isActive():
            self._timer.stop()
            self._emit_search()
        elif self._last != (self.text(), self.case_sensitive()):
            self._emit_search()

    # -- internals ---------------------------------------------------------------------
    def _emit_search(self) -> None:
        state = (self.text(), self.case_sensitive())
        if state == self._last:
            return
        self._last = state
        if not state[0]:
            self.set_status("")
        self.search_changed.emit(*state)

    def _on_prev(self) -> None:
        self.flush()
        self.prev_requested.emit()

    def _on_next(self) -> None:
        self.flush()
        self.next_requested.emit()


def status_text(index: int | None, total: int, *, capped: bool = False) -> str:
    """Localized counter text for :meth:`QuickFindBar.set_status`: ``"3 / 17"``,
    ``"17 ta"`` (no current match), ``"topilmadi"``; ``capped`` adds a "+" (more matches
    may exist beyond the scanned rows)."""
    if total <= 0:
        return t("gui.quickfind.none")
    plus = "+" if capped else ""
    if index is None:
        return t("gui.quickfind.count", total=f"{total}{plus}")
    return t("gui.quickfind.position", index=index + 1, total=f"{total}{plus}")
