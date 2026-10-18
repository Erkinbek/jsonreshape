"""QuickFindBar (AGENT_PROMPT §6.1): debounced search signal, Enter / Shift+Enter / F3,
Escape closes and clears, status texts, matcher folding like the full search."""

from __future__ import annotations

import pytest

pytest.importorskip("PySide6")

from PySide6.QtCore import Qt  # noqa: E402

from jsonreshape.gui.quickfind import QuickFindBar, make_matcher, status_text  # noqa: E402

pytestmark = pytest.mark.gui


def test_matcher_folds_case_and_apostrophes() -> None:
    m = make_matcher("o'zbek")
    assert m is not None
    assert m("Oʻzbekiston") == [(0, 6)]
    assert m("O‘ZBEK tili") == [(0, 6)]
    assert make_matcher("O'zbek", case_sensitive=True)("o'zbek") == []
    assert make_matcher("") is None


def test_open_type_enter_and_escape(qtbot) -> None:
    bar = QuickFindBar()
    qtbot.addWidget(bar)
    bar.show()
    bar.open_bar()
    assert bar.isVisible()
    with qtbot.waitSignal(bar.search_changed, timeout=2000) as sig:
        qtbot.keyClicks(bar._edit, "Tosh")
    assert sig.args == ["Tosh", False]
    with qtbot.waitSignal(bar.next_requested, timeout=1000):
        qtbot.keyClick(bar._edit, Qt.Key.Key_Return)
    with qtbot.waitSignal(bar.prev_requested, timeout=1000):
        qtbot.keyClick(bar._edit, Qt.Key.Key_Return, Qt.KeyboardModifier.ShiftModifier)
    with qtbot.waitSignal(bar.next_requested, timeout=1000):
        qtbot.keyClick(bar._edit, Qt.Key.Key_F3)
    with qtbot.waitSignals([bar.search_changed, bar.closed], timeout=1000):
        qtbot.keyClick(bar._edit, Qt.Key.Key_Escape)
    assert not bar.isVisible()


def test_enter_flushes_pending_search_before_next(qtbot) -> None:
    bar = QuickFindBar()
    qtbot.addWidget(bar)
    bar.show()
    bar.open_bar()
    order: list[str] = []
    bar.search_changed.connect(lambda text, case: order.append("search:" + text))
    bar.next_requested.connect(lambda: order.append("next"))
    qtbot.keyClicks(bar._edit, "ab")
    qtbot.keyClick(bar._edit, Qt.Key.Key_Return)  # before the debounce fires
    assert order == ["search:ab", "next"]
    qtbot.wait(300)
    assert order == ["search:ab", "next"]  # no duplicate emission afterwards


def test_case_toggle_reemits(qtbot) -> None:
    bar = QuickFindBar()
    qtbot.addWidget(bar)
    bar.open_bar("x")
    with qtbot.waitSignal(bar.search_changed, timeout=2000) as sig:
        bar._case.setChecked(True)
    assert sig.args == ["x", True]
    assert bar.matcher() is not None


def test_status_texts(lang_uz) -> None:
    assert status_text(None, 0) == "Topilmadi"
    assert status_text(2, 17) == "3 / 17"
    assert status_text(None, 5, capped=True) == "5+ ta"


def test_set_status_notfound_tints(qtbot) -> None:
    bar = QuickFindBar()
    qtbot.addWidget(bar)
    bar.set_status("Topilmadi", "notfound")
    assert bar.status_text() == "Topilmadi"
    assert bar._edit.property("notfound") is True
    bar.set_status("1 / 2", "found")
    assert bar._edit.property("notfound") is False
