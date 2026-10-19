import os


def test_undo_keeps_the_isolated_home(monkeypatch, _isolated_app_home):
    monkeypatch.setenv("X_TEST", "1")
    monkeypatch.undo()
    assert os.environ["JSONRESHAPE_HOME"] == str(_isolated_app_home)


def test_delenv_then_undo(monkeypatch, _isolated_app_home):
    monkeypatch.delenv("JSONRESHAPE_HOME")
    assert "JSONRESHAPE_HOME" not in os.environ
    monkeypatch.undo()
    assert os.environ["JSONRESHAPE_HOME"] == str(_isolated_app_home)
