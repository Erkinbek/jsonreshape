"""Application-wide GUI context (docs/ARCHITECTURE.md §11.6).

One :class:`AppContext` per application: the :class:`~jsonreshape.gui.qtasks.TaskManager`,
the :class:`~jsonreshape.gui.guistate.GuiState`, the core settings and the app-level
signals. Errors that nobody handled (failed tasks without ``on_error``, exceptions in task
callbacks) end in :meth:`AppContext.report_error`.

The GUI state is saved with :meth:`AppContext.save_state`: a silent background task (the
state file's inter-process lock may wait for another window), coalesced -- while one save
runs, further requests become one more save after it.
"""

from __future__ import annotations

import logging
from typing import Any

from PySide6.QtCore import QObject, Signal

from jsonreshape.errors import Cancelled, JsonReshapeError
from jsonreshape.gui.guistate import GuiState
from jsonreshape.gui.qtasks import TaskManager
from jsonreshape.i18n import t
from jsonreshape.settings import Settings, get_settings

log = logging.getLogger(__name__)

__all__ = ["AppContext"]


class AppContext(QObject):
    """Task manager, GUI state, core settings, error reporting, app-level signals.

    * ``tasks``: :class:`TaskManager` (its ``error`` signal is connected to
      :meth:`report_error`);
    * ``state``: :class:`GuiState` (loaded from ``app_home()/gui_state.json`` unless given);
    * ``settings``: the core :class:`~jsonreshape.settings.Settings`
      (``get_settings()``), re-read when ``settings_changed`` is emitted (whoever changes
      the settings calls ``set_settings()``/``save()`` first, then emits it);
    * ``headless``: when True (tests) :meth:`report_error` shows no dialog.
    """

    open_requested = Signal(str, dict)     # path, options (record_path, format, ref)
    settings_changed = Signal()
    error_reported = Signal(object)        # the exception given to report_error()
    status_message = Signal(str, int)      # text, timeout in ms (0: until replaced) -> status bar

    def __init__(self, *, state: GuiState | None = None, headless: bool = False,
                 parent: QObject | None = None) -> None:
        super().__init__(parent)
        self.headless = bool(headless)
        self.state: GuiState = state if state is not None else GuiState.load()
        self.settings: Settings = get_settings()
        self.tasks = TaskManager(self)
        self.tasks.error.connect(self._task_error)
        self._state_task: Any = None
        self._state_again = False
        # connected first: every other listener of settings_changed sees the new settings
        self.settings_changed.connect(self._reload_settings)

    # -- signals helpers -----------------------------------------------------------------
    def request_open(self, path: str, options: dict[str, Any] | None = None) -> None:
        """Emit ``open_requested(path, options)`` (options: ``record_path``, ``format``,
        ``ref`` -- a NodeRef to reveal after opening)."""
        self.open_requested.emit(str(path), dict(options or {}))

    def show_status(self, text: str, timeout_ms: int = 4000) -> None:
        """Show a short message in the main window's status bar (e.g. "Nusxalandi");
        ``timeout_ms`` 0 keeps it until the next message."""
        self.status_message.emit(str(text), max(0, int(timeout_ms)))

    def save_state(self) -> Any:
        """Save the GUI state in a silent background task (never blocks the GUI thread).
        Coalesced: while a save runs, any number of requests cause one more save after
        it. Returns the running save task."""
        if self._state_task is not None:
            self._state_again = True
            return self._state_task
        state = self.state
        self._state_again = False
        self._state_task = self.tasks.run(lambda _ctx: state.save(), title=t("gui.context.task.state"),
                                          kind="state", owner=self, silent=True, on_done=self._state_saved,
                                          on_error=self._state_save_failed, on_cancel=self._state_save_ended)
        return self._state_task

    def _state_saved(self, _ok: Any) -> None:
        self._state_save_ended()

    def _state_save_failed(self, exc: BaseException) -> None:
        log.warning("saving the GUI state failed: %r", exc)
        self._state_save_ended()

    def _state_save_ended(self) -> None:
        self._state_task = None
        if self._state_again:
            self.save_state()

    @property
    def state_saving(self) -> bool:
        """A background save of the GUI state is running or queued."""
        return self._state_task is not None

    def _reload_settings(self) -> None:
        self.settings = get_settings()

    def _task_error(self, exc: object, task: object) -> None:
        if isinstance(exc, BaseException):
            self.report_error(exc)

    # -- errors -----------------------------------------------------------------------------
    def report_error(self, exc: BaseException, parent: Any = None) -> None:
        """Log ``exc``, emit ``error_reported`` and (unless ``headless``) show the error
        dialog (message, hint, details, "open log"). ``Cancelled`` is not an error and is
        ignored."""
        if isinstance(exc, Cancelled):
            return
        if isinstance(exc, JsonReshapeError):
            log.warning("error reported: %s", exc)
        else:
            log.error("unexpected error reported", exc_info=(type(exc), exc, exc.__traceback__))
        try:
            self.error_reported.emit(exc)
        except Exception:  # pragma: no cover - slots normally do not raise into emit
            log.exception("error_reported handler failed")
        if self.headless:
            return
        try:
            from jsonreshape.gui.widgets import show_error

            show_error(parent, exc)
        except Exception:
            log.exception("could not show the error dialog")

    # -- lifetime -----------------------------------------------------------------------------
    def shutdown(self, timeout: float = 5.0) -> None:
        """Cancel the background tasks (waiting up to ``timeout`` s; a running state save
        finishes) and save the GUI state (the last time, in the calling thread)."""
        self._state_again = False
        self.tasks.shutdown(timeout)
        self._state_task = None
        self.state.save()
