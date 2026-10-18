"""gui.fields: one schema computation per opened document shared by every caller;
field lists and default table columns."""

from __future__ import annotations

import json

import pytest

pytest.importorskip("PySide6")

from jsonreshape.gui import fields  # noqa: E402
from jsonreshape.gui.context import AppContext  # noqa: E402
from jsonreshape.gui.session import DocSession  # noqa: E402

pytestmark = pytest.mark.gui


def _records(n: int) -> list[dict]:
    return [{"id": i, "name": f"n{i}", "contact": {"city": "Toshkent", "geo": {"lat": 1.5}},
             "orders": [{"oid": i, "sum": 1.10}], "tags": ["a"]} for i in range(n)]


def _open(qtbot, path, tmp_path):
    app = AppContext(headless=True)
    app.tasks.set_poll_interval(10)
    s = DocSession(app, path)
    with qtbot.waitSignal(s.index_finished, timeout=20000):
        s.start()
    return app, s


def test_schema_shared_and_default_columns(qtbot, tmp_path) -> None:
    path = tmp_path / "a.json"
    path.write_text(json.dumps(_records(50)), encoding="utf-8")
    app, s = _open(qtbot, path, tmp_path)
    got: list = []
    assert fields.get_schema(s, got.append) is None
    assert fields.get_schema(s, got.append) is None  # second caller queued, one task
    qtbot.waitUntil(lambda: len(got) == 2, timeout=20000)
    assert got[0] is got[1]
    assert fields.get_schema(s) is got[0]  # cached now, no callback
    cols = fields.default_columns(got[0])
    assert cols[:3] == ["id", "name", "contact.city"]
    assert "contact.geo.lat" in cols and "orders" in cols and "tags" in cols
    assert "orders[*].oid" not in cols and "contact" not in cols
    all_fields = fields.field_paths(got[0])
    assert "orders[*].oid" in all_fields and "contact" in all_fields
    assert "orders[*].oid" not in fields.field_paths(got[0], inside_arrays=False)
    assert "contact" not in fields.field_paths(got[0], containers=False)
    s.close()
    app.shutdown()


def test_full_scan_replaces_sample(qtbot, tmp_path) -> None:
    path = tmp_path / "b.jsonl"
    path.write_text("\n".join(json.dumps(r) for r in _records(30)) + "\n", encoding="utf-8")
    app, s = _open(qtbot, path, tmp_path)
    app.settings.sample_size = 5
    got: list = []
    fields.get_schema(s, got.append)
    qtbot.waitUntil(lambda: bool(got), timeout=20000)
    assert got[0].scanned == 5 and not got[0].complete
    full: list = []
    assert fields.get_schema(s, full.append, full=True) is None
    qtbot.waitUntil(lambda: bool(full), timeout=20000)
    assert full[0].complete and full[0].scanned == 30
    assert fields.cached_schema(s) is full[0]
    s.close()
    app.shutdown()


def test_keyed_and_scalar_records(qtbot, tmp_path) -> None:
    path = tmp_path / "k.json"
    path.write_text(json.dumps({"a": {"x": 1}, "b": {"x": 2}, "c": {"x": 3}}), encoding="utf-8")
    app, s = _open(qtbot, path, tmp_path)
    got: list = []
    fields.get_schema(s, got.append)
    qtbot.waitUntil(lambda: bool(got), timeout=20000)
    assert fields.default_columns(got[0]) == ["$key", "x"]
    s.close()
    path2 = tmp_path / "n.json"
    path2.write_text("[1, 2, 3]", encoding="utf-8")
    s2 = DocSession(app, path2)
    with qtbot.waitSignal(s2.index_finished, timeout=20000):
        s2.start()
    got2: list = []
    fields.get_schema(s2, got2.append)
    qtbot.waitUntil(lambda: bool(got2), timeout=20000)
    assert fields.default_columns(got2[0]) == ["$"]
    s2.close()
    app.shutdown()


# ----------------------------------------------------------------------------------------
# fix round (A-F7): waiters hear about failures and cancellations
# ----------------------------------------------------------------------------------------
def test_on_failed_is_called_when_the_computation_fails(qtbot, tmp_path, monkeypatch) -> None:
    path = tmp_path / "f.json"
    path.write_text(json.dumps(_records(10)), encoding="utf-8")
    app, s = _open(qtbot, path, tmp_path)
    errors: list = []
    app.error_reported.connect(errors.append)
    boom = OSError("disk gone")

    def failing(*args, **kwargs):
        raise boom

    monkeypatch.setattr(fields, "infer_schema", failing)
    got: list = []
    failed: list = []
    assert fields.get_schema(s, got.append, on_failed=failed.append) is None
    assert fields.get_schema(s, got.append) is None            # old-style caller, same task
    qtbot.waitUntil(lambda: bool(failed), timeout=20000)
    assert failed == [boom] and got == [] and errors == [boom]
    # a later request starts a new computation
    monkeypatch.undo()
    assert fields.get_schema(s, got.append, on_failed=failed.append) is None
    qtbot.waitUntil(lambda: bool(got), timeout=20000)
    assert failed == [boom]
    s.close()
    app.shutdown()


def test_on_failed_none_when_cancelled_or_forgotten(qtbot, tmp_path, monkeypatch) -> None:
    import threading

    path = tmp_path / "c.json"
    path.write_text(json.dumps(_records(10)), encoding="utf-8")
    app, s = _open(qtbot, path, tmp_path)
    release = threading.Event()
    real = fields.infer_schema

    def slow(*args, **kwargs):
        cancel = kwargs.get("cancel")
        while not release.is_set():
            cancel.check()
            release.wait(0.01)
        return real(*args, **kwargs)

    monkeypatch.setattr(fields, "infer_schema", slow)
    failed: list = []
    got: list = []
    fields.get_schema(s, got.append, on_failed=failed.append)
    task = [t for t in app.tasks.tasks(owner=s) if t.kind == "schema"][0]
    task.cancel()
    qtbot.waitUntil(lambda: failed == [None], timeout=20000)
    # forget() while computing: the waiters are told at once
    fields.get_schema(s, got.append, full=True, on_failed=failed.append)
    fields.forget(s)
    assert failed == [None, None]
    release.set()
    assert app.tasks.wait_idle(20)
    assert got == [] and failed == [None, None]
    s.close()
    app.shutdown()
