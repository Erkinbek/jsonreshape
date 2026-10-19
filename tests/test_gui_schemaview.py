"""SchemaView (jsonreshape/gui/schemaview.py; ARCHITECTURE §11.7 "Schema", AGENT_PROMPT §4.4).

Real sessions on generated files (array of objects with nested fields and arrays, keyed
object, JSONL, scalars, single document): the sample and the full scan (types, presence,
counts, examples, details, header), cancelling the full scan from the task, reopening,
the field actions (signals, quick filters, sorts, clipboard; enabled / disabled per field
kind), the search box, column sorting and the original order.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

pytest.importorskip("PySide6")

from PySide6.QtGui import QGuiApplication  # noqa: E402

from jsonreshape.gui import fields  # noqa: E402
from jsonreshape.gui.context import AppContext  # noqa: E402
from jsonreshape.gui.guistate import GuiState  # noqa: E402
from jsonreshape.gui.schemaview import (  # noqa: E402
    COL_COUNT,
    COL_DETAILS,
    COL_EXAMPLES,
    COL_FIELD,
    COL_PRESENCE,
    COL_TYPES,
    SchemaView,
    percent_text,
)
from jsonreshape.gui.session import DocSession  # noqa: E402
from jsonreshape.sortkeys import SortKey  # noqa: E402

pytestmark = pytest.mark.gui

TIMEOUT = 30000


# ----------------------------------------------------------------------------------
# fixtures
# ----------------------------------------------------------------------------------
@pytest.fixture
def app(qapp, tmp_path: Path):
    ctx = AppContext(state=GuiState.load(tmp_path / "gui_state.json"), headless=True)
    ctx.tasks.set_poll_interval(10)
    ctx.errors = []
    ctx.error_reported.connect(ctx.errors.append)
    yield ctx
    assert ctx.tasks.shutdown(30)


@pytest.fixture
def opened(qtbot, app):
    made: list = []

    def open_file(path: Path, **kw) -> DocSession:
        kw.setdefault("use_cache", False)
        s = DocSession(app, path, **kw)
        made.append(s)
        with qtbot.waitSignal(s.index_finished, timeout=TIMEOUT):
            s.start()
        return s

    yield open_file
    for s in made:
        s.close()
    assert app.tasks.wait_idle(30)


def make_view(qtbot, session: DocSession) -> SchemaView:
    view = SchemaView(session)
    qtbot.addWidget(view)
    view.resize(1000, 600)
    view.show()
    return view


def show_schema(qtbot, view: SchemaView) -> None:
    with qtbot.waitSignal(view.schema_shown, timeout=TIMEOUT):
        view.activate()


def records(n: int) -> list[dict]:
    out = []
    for i in range(n):
        rec = {"id": i, "name": f"user{i}", "age": i % 90 if i % 10 else None,
               "contact": {"city": ["Toshkent", "Samarqand", "Buxoro"][i % 3], "zip": f"{100000 + i}"},
               "orders": [{"sum": i + 0.5, "items": list(range(i % 4))} for _ in range(i % 3)],
               "tags": ["a", "b"][: i % 3]}
        if i % 3 == 0:
            rec["email"] = f"u{i}@x.uz"
        out.append(rec)
    return out


@pytest.fixture
def array_file(tmp_path: Path) -> Path:
    path = tmp_path / "people.json"
    path.write_text(json.dumps(records(300), ensure_ascii=False), encoding="utf-8")
    return path


def texts(view: SchemaView) -> list[str]:
    return [item.fi.text for item in view.items()]


# ----------------------------------------------------------------------------------
# sample, full scan, header, columns
# ----------------------------------------------------------------------------------
def test_sample_then_full_scan(qtbot, app, opened, array_file: Path, lang_uz) -> None:
    s = opened(array_file)
    app.settings.sample_size = 50
    view = make_view(qtbot, s)
    assert view.schema is None and view.stack.currentIndex() == 0   # nothing before activate()
    show_schema(qtbot, view)
    sch = view.schema
    assert sch is not None and not sch.complete and sch.scanned == 50 and sch.total_records == 300
    assert "50 ta yozuvdan namuna (jami 300)" in view.header_label.text()
    assert "ta maydon" in view.header_label.text()
    got = texts(view)
    for name in ("id", "name", "age", "contact", "contact.city", "orders", "orders[*]", "orders[*].sum",
                 "orders[*].items", "tags", "tags[*]", "email"):
        assert name in got, name
    assert got.index("contact") < got.index("contact.city")       # pre-order, children after parent
    assert view.full_button.isEnabled()

    # full scan: a visible background task (kind "schema"), header updated, button disabled
    view.full_scan()
    assert view.full_scan_running and not view.full_button.isEnabled()
    assert any(t.kind == "schema" and not t.silent for t in app.tasks.tasks(owner=s))
    qtbot.waitUntil(lambda: view.schema is not None and view.schema.complete, timeout=TIMEOUT)
    full = view.schema
    assert full.scanned == 300 and not view.full_scan_running
    assert view.header_label.text().startswith("To'liq: 300 ta yozuv")
    assert not view.full_button.isEnabled()
    # the cells of the full schema
    email = view.item_for("email")
    assert email.text(COL_PRESENCE) == percent_text(100 / 300) == "33.3%"
    assert email.text(COL_TYPES) == "satr 100%"
    assert email.text(COL_EXAMPLES).startswith('"u0@x.uz", "u3@x.uz"')
    assert "uzunligi" in email.text(COL_DETAILS)
    age = view.item_for("age")
    assert age.text(COL_TYPES) == "son 90% \N{MIDDLE DOT} null 10%"
    assert age.text(COL_PRESENCE) == "100%"
    assert "qiymatlar 1 \N{HORIZONTAL ELLIPSIS} 89" in age.text(COL_DETAILS)
    osum = view.item_for("orders[*].sum")
    assert osum.text(COL_COUNT) == f"{full.find('orders[*].sum').count} / {full.find('orders[*].sum').records}"
    assert osum.text(COL_PRESENCE) == "100%"     # present in every order element
    assert "elementlar 0\N{EN DASH}2" in view.item_for("orders").text(COL_DETAILS)
    assert view.item_for("contact").text(COL_FIELD) == "contact"
    assert view.item_for("contact.city").text(COL_FIELD) == "city"
    assert view.item_for("tags[*]").text(COL_FIELD) == "[*]"
    assert not view.grab().isNull()       # paints badges and bars (exceptions would fail the test)
    assert app.errors == []
    # a second activation reuses the cached schema (no new task)
    view.activate()
    assert view.schema is full
    assert not [t for t in app.tasks.tasks(owner=s) if t.kind == "schema"]


def test_cancelled_full_scan_restores_the_button(qtbot, app, opened, tmp_path: Path, monkeypatch) -> None:
    path = tmp_path / "big.jsonl"
    path.write_text("".join(json.dumps({"i": i, "s": "x" * (i % 7)}) + "\n" for i in range(3000)), encoding="utf-8")
    s = opened(path)
    app.settings.sample_size = 20
    view = make_view(qtbot, s)
    show_schema(qtbot, view)
    import jsonreshape.gui.fields as fields_mod

    real = fields_mod.infer_schema

    def slow(doc, source, *, sample=None, progress=None, cancel=None):
        if sample is None:           # the full scan waits for its cancellation
            while True:
                cancel.check()
                import time

                time.sleep(0.01)
        return real(doc, source, sample=sample, progress=progress, cancel=cancel)

    monkeypatch.setattr(fields_mod, "infer_schema", slow)
    view.full_scan()
    assert view.full_scan_running
    (task,) = [t for t in app.tasks.tasks(owner=s) if t.kind == "schema"]
    task.cancel()   # what the tasks panel does
    qtbot.waitUntil(lambda: not view.full_scan_running, timeout=TIMEOUT)
    assert view.full_button.isEnabled() and not view.schema.complete
    assert "skanerlash davom" not in view.header_label.text()
    assert app.errors == []


def test_cancelled_sample_offers_retry(qtbot, app, opened, array_file: Path, monkeypatch, lang_uz) -> None:
    s = opened(array_file)
    import jsonreshape.gui.fields as fields_mod

    real = fields_mod.infer_schema
    gate = {"block": True}

    def slow(doc, source, *, sample=None, progress=None, cancel=None):
        import time

        while gate["block"]:
            cancel.check()
            time.sleep(0.01)
        return real(doc, source, sample=sample, progress=progress, cancel=cancel)

    monkeypatch.setattr(fields_mod, "infer_schema", slow)
    view = make_view(qtbot, s)
    view.activate()
    assert view.loading and "aniqlanmoqda" in view.message.text()
    (task,) = [t for t in app.tasks.tasks(owner=s) if t.kind == "schema"]
    task.cancel()
    qtbot.waitUntil(lambda: not view.loading, timeout=TIMEOUT)
    assert view.retry_button.isVisibleTo(view) and view.schema is None
    gate["block"] = False
    with qtbot.waitSignal(view.schema_shown, timeout=TIMEOUT):
        view.retry_button.click()
    assert view.schema is not None


def test_keyed_document_shows_key_first(qtbot, app, opened, tmp_path: Path, lang_uz) -> None:
    path = tmp_path / "keyed.json"
    path.write_text(json.dumps({f"k{i}": {"x": i, "y": [i]} for i in range(40)}), encoding="utf-8")
    s = opened(path)
    view = make_view(qtbot, s)
    show_schema(qtbot, view)
    assert texts(view)[:2] == ["$key", "x"]
    key = view.item_for("$key")
    assert key.text(COL_FIELD) == "$key" and key.text(COL_TYPES) == "satr 100%"
    assert view.schema.complete    # 40 records <= sample size: complete at once
    assert view.header_label.text().startswith("To'liq: 40 ta yozuv")
    view.select_field("$key")
    assert view.act_add_column.isEnabled() and view.act_sort_asc.isEnabled()
    assert not view.act_filter_exists.isEnabled() and not view.act_filter_missing.isEnabled()
    with qtbot.waitSignal(view.add_column_requested, timeout=1000) as sig:
        view.act_add_column.trigger()
    assert sig.args == ["$key"]
    view.act_sort_desc.trigger()
    assert s.sort_keys == [SortKey("$key", descending=True)]
    qtbot.waitUntil(lambda: not s.view_busy, timeout=TIMEOUT)
    assert [s.record_at(i) for i in range(3)] == [39, 38, 37]     # natural order: k39 > k38 > ... > k10 > k9
    assert app.errors == []


def test_actions_on_jsonl(qtbot, app, opened, tmp_path: Path, lang_uz) -> None:
    path = tmp_path / "data.jsonl"
    recs = records(120)
    path.write_text("".join(json.dumps(r) + "\n" for r in recs), encoding="utf-8")
    s = opened(path)
    view = make_view(qtbot, s)
    show_schema(qtbot, view)
    assert all(not a.isEnabled() for a in view.actions_list())     # no field selected

    view.select_field("email")
    assert all(a.isEnabled() for a in view.actions_list())
    view.act_filter_exists.trigger()
    assert s.quick_filters == {"email": "exists(email)"}
    qtbot.waitUntil(lambda: not s.view_busy, timeout=TIMEOUT)
    assert s.view_len() == sum(1 for r in recs if "email" in r) == 40
    view.act_filter_missing.trigger()
    assert s.quick_filters == {"email": "missing(email)"}
    qtbot.waitUntil(lambda: not s.view_busy, timeout=TIMEOUT)
    assert s.view_len() == 80
    s.set_quick_filter("email", None)
    qtbot.waitUntil(lambda: not s.view_busy, timeout=TIMEOUT)

    # sort by a field inside an array: allowed (first element), keys built from the path
    view.select_field("orders[*].sum")
    assert view.act_sort_asc.isEnabled() and "birinchi" in view.act_sort_asc.toolTip()
    view.act_sort_desc.trigger()
    assert s.sort_keys == [SortKey("orders[*].sum", descending=True)]
    qtbot.waitUntil(lambda: not s.view_busy, timeout=TIMEOUT)
    first = [s.record_at(i) for i in range(3)]
    best = sorted((r for r in recs if r["orders"]), key=lambda r: -r["orders"][0]["sum"])[:3]
    assert first == [r["id"] for r in best]

    with qtbot.waitSignal(view.filter_field_requested, timeout=1000) as sig:
        view.act_filter_builder.trigger()
    assert sig.args == ["orders[*].sum"]
    view.select_field("contact.city")
    with qtbot.waitSignal(view.add_column_requested, timeout=1000) as sig:
        view.act_add_column.trigger()
    assert sig.args == ["contact.city"]
    view.act_copy_path.trigger()
    assert QGuiApplication.clipboard().text() == "contact.city"
    assert view.copy_path("jsonpath") == "$.contact.city"
    assert view.copy_key() == "city"
    assert view.copy_value() == "contact.city"
    menu = view.build_menu()
    names = [a.objectName() for a in menu.actions() if a.objectName()]
    assert names == ["schema_add_column", "schema_filter_exists", "schema_filter_missing", "schema_filter_builder",
                     "schema_sort_asc", "schema_sort_desc", "schema_stats", "schema_dupes", "schema_copy_path",
                     "schema_mapping"]
    assert app.errors == []


def test_pooled_map_keys(qtbot, app, opened, tmp_path: Path, lang_uz) -> None:
    path = tmp_path / "maps.jsonl"
    path.write_text("".join(json.dumps({"id": i, "scores": {f"d{i * 3 + j}": j for j in range(3)}}) + "\n"
                            for i in range(400)), encoding="utf-8")
    s = opened(path)
    view = make_view(qtbot, s)
    show_schema(qtbot, view)
    assert "scores.*" in texts(view)
    view.select_field("scores.*")
    assert view.item_for("scores.*").text(COL_FIELD) == "*"
    assert not view.act_add_column.isEnabled() and "(*)" in view.act_add_column.toolTip()
    assert not view.act_sort_asc.isEnabled()
    assert view.act_filter_exists.isEnabled()
    view.act_filter_exists.trigger()
    assert s.quick_filters == {"scores.*": "exists(scores.*)"}
    qtbot.waitUntil(lambda: not s.view_busy, timeout=TIMEOUT)
    assert s.view_len() == 400


def test_scalar_records_show_the_root_row(qtbot, app, opened, tmp_path: Path, lang_uz) -> None:
    path = tmp_path / "mixed.json"
    path.write_text(json.dumps([1, "a", None, {"k": 1}, 2.5]), encoding="utf-8")
    s = opened(path)
    view = make_view(qtbot, s)
    show_schema(qtbot, view)
    assert texts(view)[:2] == ["$", "k"]
    root = view.item_for("$")
    assert root.text(COL_FIELD) == "$" and "son 40%" in root.text(COL_TYPES)
    view.select_field("$")
    assert view.act_add_column.isEnabled() and view.act_sort_asc.isEnabled()
    assert not view.act_filter_exists.isEnabled()        # the record itself always exists
    view.act_sort_asc.trigger()
    assert s.sort_keys == [SortKey("$")]


def test_single_document_disables_filter_and_sort(qtbot, app, opened, tmp_path: Path, lang_uz) -> None:
    path = tmp_path / "one.json"
    path.write_text(json.dumps({"a": 1, "b": {"c": [1, 2]}}), encoding="utf-8")
    s = opened(path)
    assert s.doc.kind == "single"
    view = make_view(qtbot, s)
    show_schema(qtbot, view)
    view.select_field("b.c")
    assert view.act_add_column.isEnabled() and view.act_copy_path.isEnabled()
    for act in (view.act_filter_exists, view.act_filter_missing, view.act_sort_asc, view.act_sort_desc,
                view.act_filter_builder):
        assert not act.isEnabled()
        assert "bitta JSON" in act.toolTip()


def test_search_sorting_and_original_order(qtbot, app, opened, array_file: Path) -> None:
    s = opened(array_file)
    view = make_view(qtbot, s)
    show_schema(qtbot, view)
    order = texts(view)
    view.search_edit.setText("SUM")
    shown = [it.fi.text for it in view.items() if not it.isHidden()]
    assert shown == ["orders", "orders[*]", "orders[*].sum"]       # matches + their ancestors
    view.search_edit.clear()
    assert all(not it.isHidden() for it in view.items())
    view.tree.sortByColumn(COL_PRESENCE, Qt_Ascending())
    top = [view.tree.topLevelItem(i).fi.text for i in range(view.tree.topLevelItemCount())]
    assert top[0] == "email"          # the least present top-level field first
    view.restore_order()
    top = [view.tree.topLevelItem(i).fi.text for i in range(view.tree.topLevelItemCount())]
    assert top == [t for t in order if "." not in t and "[" not in t]
    view.quick_find()
    assert view.search_edit.hasFocus() or not view.isActiveWindow()


def Qt_Ascending():  # noqa: N802 - small helper
    from PySide6.QtCore import Qt

    return Qt.SortOrder.AscendingOrder


def test_reopen_reloads_the_schema(qtbot, app, opened, tmp_path: Path) -> None:
    path = tmp_path / "grow.jsonl"
    path.write_text("".join(json.dumps({"a": i}) + "\n" for i in range(10)), encoding="utf-8")
    s = opened(path)
    view = make_view(qtbot, s)
    show_schema(qtbot, view)
    assert texts(view) == ["a"]
    path.write_text("".join(json.dumps({"a": i, "b": "x"}) + "\n" for i in range(12)), encoding="utf-8")
    with qtbot.waitSignal(view.schema_shown, timeout=TIMEOUT):
        s.reopen(use_cache=False)
    assert texts(view) == ["a", "b"] and view.schema.scanned == 12
    assert fields.cached_schema(s) is view.schema


def test_wide_schema(qtbot, app, opened, tmp_path: Path) -> None:
    path = tmp_path / "wide.json"
    path.write_text(json.dumps([{f"f{j}": j for j in range(900)} for _ in range(3)] +
                               [{"nested": {f"g{j}": [j] for j in range(900)}}]), encoding="utf-8")
    s = opened(path)
    view = make_view(qtbot, s)
    show_schema(qtbot, view)
    assert len(view.items()) == 900 + 1 + 900 * 2
    assert view.item_for("nested.g899[*]") is not None
    view.zoom(2)
    view.zoom(0)
    view.shutdown()


def test_large_file_sample_then_full(qtbot, app, opened, tmp_path: Path, lang_uz) -> None:
    path = tmp_path / "large.jsonl"
    n = 200_000
    with open(path, "w", encoding="utf-8") as f:
        for i in range(n):
            f.write(json.dumps({"i": i, "k": f"v{i % 97}", "o": {"z": i % 3}} if i % 50 else {"i": i}) + "\n")
    s = opened(path)
    assert int(app.settings.sample_size) == 10000
    view = make_view(qtbot, s)
    show_schema(qtbot, view)
    assert view.schema.scanned == 10000 and view.schema.total_records == n
    assert view.header_label.text().startswith("10 000 ta yozuvdan namuna (jami 200 000)")
    with qtbot.waitSignal(view.schema_shown, timeout=TIMEOUT):
        view.full_scan()
    assert view.schema.complete and view.schema.scanned == n
    assert view.item_for("k").text(COL_PRESENCE) == "98%"
    assert view.item_for("o.z").text(COL_COUNT) == "196 000"
