"""ExportDialog: the Parquet format (jsonreshape/gui/exportdialog.py; stage 7).

The dialog lists Parquet (``FORMATS``, ``EXTENSIONS``, the file filter), shows the table page for it (columns,
array mode; the CSV / XLSX-only widgets hidden), writes exactly what ``writer.export`` writes for the same options,
follows the ``.parquet`` extension and back; the node scope treats Parquet like CSV / XLSX / SQLite
(``TABLE_NODE_FORMATS``: an array / object node re-opened as the record container, refused for a JSON Lines source);
the exact number texts of a scalar array survive as a string column. Skipped when pyarrow is not installed.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from jsonreshape import writer
from jsonreshape.gui import exportdialog as exportdialog_mod
from jsonreshape.gui.session import NodeRef
from jsonreshape.i18n import t
from jsonreshape.reader import Document
from jsonreshape.writer import ExportOptions
from test_gui_exportdialog import app, dialog, opened, record_text, run_export, write_array  # noqa: F401 - fixtures

pytestmark = pytest.mark.gui

pq = pytest.importorskip("pyarrow.parquet")


def test_dialog_lists_parquet_and_writes_the_file(qtbot, app, opened, dialog, tmp_path: Path) -> None:
    assert "parquet" in exportdialog_mod.FORMATS and exportdialog_mod.EXTENSIONS["parquet"] == ".parquet"
    assert "parquet" in exportdialog_mod.TABLE_NODE_FORMATS and "parquet" not in exportdialog_mod.NODE_FORMATS
    s = opened(write_array(tmp_path / "people.json"))
    dlg = dialog(s, scope="all")
    combo = dlg.format_combo
    index = exportdialog_mod.FORMATS.index("parquet")
    assert combo.count() == len(exportdialog_mod.FORMATS) == 8
    assert combo.itemText(index) == t("gui.export.format.parquet") and combo.itemData(index) == "parquet"
    out = tmp_path / "out.parquet"
    dlg.set_output_path(out)
    assert dlg.format() == "parquet" and dlg.scope() == "all" and dlg.error_text() == ""
    # the table page: columns and array mode; no delimiter / BOM / sheet split
    assert dlg.pages.currentIndex() == dlg._page_index("csv") == 2
    form = dlg.table_form
    assert form.isRowVisible(dlg.array_mode_combo) and form.isRowVisible(dlg.explode_edit)
    for widget in (dlg.delimiter_combo, dlg.bom_check, dlg.split_combo, dlg.max_rows_spin):
        assert not form.isRowVisible(widget)
    outcome = run_export(qtbot, dlg)
    opts = dlg.export_options()
    assert opts == ExportOptions.from_settings("parquet") and opts.parquet_compression == "zstd"
    want = tmp_path / "want.parquet"
    report = writer.export(s.doc, want, opts, None)
    assert out.read_bytes() == want.read_bytes()
    assert outcome.report.records == report.records == 120 and outcome.report.rows == 120
    assert outcome.report.files == [out] and outcome.file_sizes == [out.stat().st_size]
    table = pq.read_table(out)
    assert table.num_rows == 120 and table.column("id").to_pylist() == list(range(120))
    assert str(table.schema.field("id").type) == "int64" and str(table.schema.field("name").type) == "string"
    assert table.column("nums").to_pylist()[0] == "1.10;1e400;12345678901234567890;-0.0;0.1"    # joined, exact
    # explicit columns and rows mode through the widgets
    dlg.set_columns(["id", "tags", "nested.y"])
    dlg.array_mode_combo.setCurrentIndex(dlg.array_mode_combo.findData("rows"))
    dlg.set_output_path(tmp_path / "rows.parquet")
    run_export(qtbot, dlg)
    opts = dlg.export_options()
    assert opts.columns == ["id", "tags", "nested.y"] and opts.array_mode == "rows" and opts.fmt == "parquet"
    writer.export(s.doc, tmp_path / "want_rows.parquet", opts, None)
    assert (tmp_path / "rows.parquet").read_bytes() == (tmp_path / "want_rows.parquet").read_bytes()
    assert pq.read_table(tmp_path / "rows.parquet").num_rows == 240


def test_format_follows_the_parquet_extension_and_back(qtbot, app, opened, dialog, tmp_path: Path) -> None:
    s = opened(write_array(tmp_path / "people.json"))
    dlg = dialog(s, scope="all")
    dlg.set_output_path(tmp_path / "x.parquet")
    assert dlg.format() == "parquet"
    dlg.set_format("csv")
    assert dlg.path_edit.text().endswith("x.csv") and dlg.format() == "csv"
    dlg.set_format("parquet")
    assert dlg.path_edit.text().endswith("x.parquet")
    assert "parquet" in t("gui.export.filter.parquet") and "*.parquet" in t("gui.export.filter.parquet")
    # remembered as the last format
    dlg.set_output_path(tmp_path / "y.parquet")
    run_export(qtbot, dlg)
    dlg2 = dialog(s, scope="all")
    assert dlg2.format() == "parquet"


def test_node_scope_treats_parquet_as_a_table_format(qtbot, app, opened, dialog, tmp_path: Path) -> None:
    path = write_array(tmp_path / "people.json")
    s = opened(path)
    s.select(NodeRef(7, ("nums",)))
    dlg = dialog(s, scope="node")
    model = dlg.format_combo.model()
    assert [model.item(i).isEnabled() for i in range(6)] == [True] * 6
    out = tmp_path / "nums.parquet"
    dlg.set_output_path(out)
    assert dlg.format() == "parquet" and dlg.error_text() == "" and dlg.scope() == "node"
    outcome = run_export(qtbot, dlg)
    assert outcome.scope == "node" and outcome.report.records == 5
    # the elements are the records: one column "$" of the exact number texts (no type keeps them all)
    table = pq.read_table(out)
    assert table.column_names == ["$"] and str(table.schema.field("$").type) == "string"
    assert table.column("$").to_pylist() == ["1.10", "1e400", "12345678901234567890", "-0.0", "0.1"]
    assert "writer.warn.parquet_number_text" in outcome.report.warning_keys
    ref_doc = Document.open(path, record_path=(7, "nums"), use_cache=False)
    try:
        ref_doc.build_index()
        writer.export(ref_doc, tmp_path / "want.parquet", ExportOptions.from_settings("parquet", keep_wrapper=False))
    finally:
        ref_doc.close()
    assert out.read_bytes() == (tmp_path / "want.parquet").read_bytes()
    # an object node: its members are the records ($key + the leaves)
    s.select(NodeRef(3, ("nested",)))
    dlg.set_output_path(tmp_path / "nested.parquet")
    outcome = run_export(qtbot, dlg)
    table = pq.read_table(tmp_path / "nested.parquet")
    assert outcome.report.records == 2 and table.column_names[0] == "$key"
    assert table.column("$key").to_pylist() == ["x", "y"]


def test_node_scope_refuses_parquet_for_a_json_lines_source(qtbot, app, opened, dialog, tmp_path: Path) -> None:
    lines = tmp_path / "l.jsonl"
    lines.write_text("".join(record_text(i) + "\n" for i in range(4)), encoding="utf-8")
    s = opened(lines)
    s.select(NodeRef(1, ("tags",)))
    dlg = dialog(s, scope="node")
    model = dlg.format_combo.model()
    enabled = [model.item(i).isEnabled() for i in range(6)]
    assert enabled == [True, True, False, False, False, False]
    assert model.item(5).toolTip() == model.item(2).toolTip() != ""        # the same refusal as CSV
    dlg.set_output_path(tmp_path / "t.parquet")
    assert dlg.format() == "json"                                           # a refused format is not taken
