"""ExportDialog: the Feather / Arrow IPC formats (jsonreshape/gui/exportdialog.py; stage 7).

The dialog lists both names after Parquet (``feather``: ``.feather``, ``arrow``: ``.arrow`` -- one Arrow IPC file
under two names, as ``--to feather`` / ``--to arrow``), shows the table page for them (columns, array mode; the
CSV / XLSX-only widgets hidden), writes exactly what ``writer.export`` writes for the same options (the two names
give the same bytes), follows the ``.feather`` / ``.arrow`` extension and back (a typed ``.arrow`` name selects
``arrow``: ``writer.format_for_path`` returns it, so the dialog must list it), and treats both as table formats
in the node scope (refused for a JSON Lines source, like CSV / XLSX / SQLite / Parquet).
Skipped when pyarrow is not installed.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from jsonreshape import writer
from jsonreshape.gui import exportdialog as exportdialog_mod
from jsonreshape.gui.session import NodeRef
from jsonreshape.i18n import t
from jsonreshape.writer import ExportOptions
from test_gui_exportdialog import app, dialog, opened, record_text, run_export, write_array  # noqa: F401 - fixtures

pytestmark = pytest.mark.gui

pf = pytest.importorskip("pyarrow.feather")


def test_dialog_lists_feather_and_arrow_and_writes_the_file(qtbot, app, opened, dialog, tmp_path: Path) -> None:
    assert exportdialog_mod.FORMATS[-2:] == ("feather", "arrow")
    assert exportdialog_mod.EXTENSIONS["feather"] == ".feather" and exportdialog_mod.EXTENSIONS["arrow"] == ".arrow"
    for fmt in ("feather", "arrow"):
        assert fmt in exportdialog_mod.TABLE_NODE_FORMATS and fmt not in exportdialog_mod.NODE_FORMATS
    s = opened(write_array(tmp_path / "people.json"))
    dlg = dialog(s, scope="all")
    combo = dlg.format_combo
    assert combo.count() == len(exportdialog_mod.FORMATS) == 8
    for fmt in ("feather", "arrow"):
        index = exportdialog_mod.FORMATS.index(fmt)
        assert combo.itemText(index) == t(f"gui.export.format.{fmt}") and combo.itemData(index) == fmt
    out = tmp_path / "out.feather"
    dlg.set_output_path(out)
    assert dlg.format() == "feather" and dlg.scope() == "all" and dlg.error_text() == ""
    # the table page: columns and array mode; no delimiter / BOM / sheet split
    assert dlg.pages.currentIndex() == dlg._page_index("feather") == dlg._page_index("arrow") == 2
    form = dlg.table_form
    assert form.isRowVisible(dlg.array_mode_combo) and form.isRowVisible(dlg.explode_edit)
    for widget in (dlg.delimiter_combo, dlg.bom_check, dlg.split_combo, dlg.max_rows_spin):
        assert not form.isRowVisible(widget)
    outcome = run_export(qtbot, dlg)
    opts = dlg.export_options()
    assert opts == ExportOptions.from_settings("feather") and opts.feather_compression == "zstd"
    want = tmp_path / "want.feather"
    report = writer.export(s.doc, want, opts, None)
    assert out.read_bytes() == want.read_bytes()
    assert outcome.report.records == report.records == 120 and outcome.report.files == [out]
    table = pf.read_table(out)
    assert table.num_rows == 120 and table.column("id").to_pylist() == list(range(120))
    assert str(table.schema.field("id").type) == "int64" and str(table.schema.field("name").type) == "string"
    # the "arrow" entry: the extension follows, and the file is the same Arrow IPC bytes
    dlg.set_format("arrow")
    assert dlg.path_edit.text().endswith("out.arrow") and dlg.format() == "arrow"
    run_export(qtbot, dlg)
    assert (tmp_path / "out.arrow").read_bytes() == out.read_bytes()


def test_format_follows_the_feather_and_arrow_extensions(qtbot, app, opened, dialog, tmp_path: Path) -> None:
    s = opened(write_array(tmp_path / "people.json"))
    dlg = dialog(s, scope="all")
    dlg.set_output_path(tmp_path / "x.arrow")
    assert dlg.format() == "arrow" and dlg.error_text() == ""
    dlg.set_output_path(tmp_path / "x.feather")
    assert dlg.format() == "feather"
    dlg.set_format("csv")
    assert dlg.path_edit.text().endswith("x.csv") and dlg.format() == "csv"
    dlg.set_format("feather")
    assert dlg.path_edit.text().endswith("x.feather")
    assert "*.feather" in t("gui.export.filter.feather") and "*.arrow" in t("gui.export.filter.arrow")
    # remembered as the last format
    dlg.set_output_path(tmp_path / "y.arrow")
    run_export(qtbot, dlg)
    assert dialog(s, scope="all").format() == "arrow"


def test_node_scope_treats_feather_as_a_table_format(qtbot, app, opened, dialog, tmp_path: Path) -> None:
    s = opened(write_array(tmp_path / "people.json"))
    s.select(NodeRef(3, ("nested",)))
    dlg = dialog(s, scope="node")
    model = dlg.format_combo.model()
    assert all(model.item(i).isEnabled() for i in range(dlg.format_combo.count()))
    dlg.set_output_path(tmp_path / "nested.feather")
    assert dlg.format() == "feather" and dlg.error_text() == "" and dlg.scope() == "node"
    outcome = run_export(qtbot, dlg)
    table = pf.read_table(tmp_path / "nested.feather")
    assert outcome.scope == "node" and outcome.report.records == 2
    assert table.column_names[0] == "$key" and table.column("$key").to_pylist() == ["x", "y"]


def test_node_scope_refuses_feather_for_a_json_lines_source(qtbot, app, opened, dialog, tmp_path: Path) -> None:
    lines = tmp_path / "l.jsonl"
    lines.write_text("".join(record_text(i) + "\n" for i in range(4)), encoding="utf-8")
    s = opened(lines)
    s.select(NodeRef(1, ("tags",)))
    dlg = dialog(s, scope="node")
    model = dlg.format_combo.model()
    for fmt in ("feather", "arrow"):
        i = exportdialog_mod.FORMATS.index(fmt)
        assert not model.item(i).isEnabled()
        assert model.item(i).toolTip() == model.item(2).toolTip() != ""      # the same refusal as CSV
    dlg.set_output_path(tmp_path / "t.arrow")
    assert dlg.format() == "json"                                           # a refused format is not taken
