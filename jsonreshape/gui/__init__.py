"""PySide6 desktop viewer (docs/ARCHITECTURE.md §11).

Importing this package does not import Qt; :func:`require_qt` checks that PySide6 is
available and raises a localized :class:`~jsonreshape.errors.UserInputError` otherwise.
"""

from __future__ import annotations

import importlib.util

from jsonreshape.errors import UserInputError

MIN_QT = (6, 5)


def require_qt() -> None:
    """Raise ``UserInputError("gui.app.no_qt")`` when PySide6 cannot be imported."""
    if importlib.util.find_spec("PySide6") is None:
        raise UserInputError("gui.app.no_qt")
