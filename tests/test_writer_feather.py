"""jsonreshape.writer_parquet: the Arrow IPC / Feather export (formats ``feather`` / ``arrow``; stage 7).

Feather / Arrow IPC is the Parquet engine with another write backend (``_ArrowBackend``), so most tests check
that the table equals the Parquet export's -- the same column types (int64 / double / bool / string), the same
values row for row, the same counters and the same warnings (the ``feather`` keys for the ``parquet`` ones) --
for the table options and every document kind. The rest checks what is Feather's own:

* the registration (``feather`` and ``arrow``: ``FORMATS``, ``TABLE_FORMATS``, the extensions, the CLI lists);
* ``.feather`` / ``.arrow`` and ``fmt="feather"`` / ``fmt="arrow"`` give byte-identical Arrow IPC files (the
  ``ARROW1`` magic), read by ``pyarrow.feather`` and ``pyarrow.ipc`` alike;
* ``feather_compression`` (zstd / lz4 / none); one record batch per row group;
* the two-pass restart (``writer.warn.feather_restarted``) and real worker processes give the same bytes;
* ``.feather.gz`` / ``.arrow.gz`` refused, a pyarrow error, cancellation, pyarrow missing, the empty table;
* the CLI: ``convert in.json out.feather``, ``--to arrow``, ``--feather-compression``, option compatibility.

Every test is skipped when pyarrow is not installed.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

import pytest

import jsongen
from jsonreshape import cli, writer
from jsonreshape import writer_parquet as wp
from jsonreshape.errors import Cancelled, ExportError, UserInputError
from jsonreshape.settings import Settings
from jsonreshape.tasks import CancelToken, Progress
from jsonreshape.writer import ExportOptions, export, format_for_path, plan_columns
from test_writer import files_in, open_doc, out_dir  # noqa: F401 - fixtures
from test_writer_parquet import (  # noqa: F401 - shared data, helpers and fixtures
    SCALARS,
    SPECIAL,
    TYPES_JSON,
    conflict_records,
    run,
    small_types,
    src,
)

pa = pytest.importorskip("pyarrow")
pq = pytest.importorskip("pyarrow.parquet")
pf = pytest.importorskip("pyarrow.feather")
pytest.importorskip("pyarrow.ipc")

_KINDS = {"int64": "int64", "double": "double", "bool": "bool", "string": "string", "large_string": "string"}


# ----------------------------------------------------------------------------------
# helpers
# ----------------------------------------------------------------------------------
def read_ipc(path: Path) -> Any:
    """The table of an Arrow IPC file, read with ``pyarrow.ipc`` (the file is closed again)."""
    with pa.OSFile(str(path), "rb") as f:
        return pa.ipc.open_file(f).read_all()


def batch_rows(path: Path) -> list[int]:
    """The number of rows of every record batch of an Arrow IPC file."""
    with pa.OSFile(str(path), "rb") as f:
        reader = pa.ipc.open_file(f)
        return [reader.get_batch(i).num_rows for i in range(reader.num_record_batches)]


def fkinds(path: Path) -> dict[str, str]:
    return {f.name: _KINDS[str(f.type)] for f in read_ipc(path).schema}


def pkinds(path: Path) -> dict[str, str]:
    return {f.name: _KINDS[str(f.type)] for f in pq.read_schema(path)}


def fcolumns(path: Path) -> dict[str, list[Any]]:
    t = pf.read_table(path)
    return {name: t.column(name).to_pylist() for name in t.column_names}


def pcolumns(path: Path) -> dict[str, list[Any]]:
    t = pq.read_table(path)
    return {name: t.column(name).to_pylist() for name in t.column_names}


def same_as_parquet(doc: Any, out: Path, **kw: Any) -> tuple[writer.ExportReport, writer.ExportReport]:
    """The Feather export and the Parquet export with the same options: the same columns in the same order, the
    same types and values, the same counters, the same warnings (the Feather keys for the Parquet ones)."""
    p_out, f_out = out.with_suffix(".parquet"), out.with_suffix(".feather")
    rp = export(doc, p_out, ExportOptions("parquet", **kw), workers=1)
    rf = export(doc, f_out, ExportOptions("feather", **kw), workers=1)
    assert read_ipc(f_out).column_names == pq.read_table(p_out).column_names
    assert fkinds(f_out) == pkinds(p_out)
    assert fcolumns(f_out) == pcolumns(p_out)
    assert (rf.records, rf.rows, rf.columns, rf.skipped) == (rp.records, rp.rows, rp.columns, rp.skipped)
    assert rf.warning_keys == [k.replace("parquet", "feather") for k in rp.warning_keys]
    assert rf.fmt == "feather" and rf.files == [f_out] and rf.bytes_written > 0
    return rf, rp


# ----------------------------------------------------------------------------------
# registration and options
# ----------------------------------------------------------------------------------
def test_formats_are_registered() -> None:
    for fmt in ("feather", "arrow"):
        assert fmt in writer.FORMATS and fmt in writer.TABLE_FORMATS and fmt not in writer.GZIP_FORMATS
        assert writer._EXPORTERS[fmt] is writer._EXPORTERS["feather"]
    assert writer.FORMAT_NAMES["feather"] == "Feather" and writer.FORMAT_NAMES["arrow"] == "Arrow IPC"
    assert format_for_path("x.feather") == "feather" and format_for_path("X.FEATHER") == "feather"
    assert format_for_path("x.arrow") == "arrow" and format_for_path("x.Arrow") == "arrow"
    assert format_for_path("x.feather.gz") is None and format_for_path("x.arrow.gz") is None
    assert writer.FEATHER_COMPRESSIONS == wp.FEATHER_COMPRESSIONS == cli.FEATHER_COMPRESSIONS == ("zstd", "lz4", "none")
    assert set(cli.OUTPUT_FORMATS) == set(writer.FORMATS)
    assert cli._FORMAT_OPTIONS["--feather-compression"] == ("feather", "arrow")
    for opt in ("--columns", "--array-mode", "--explode", "--sep"):
        assert {"feather", "arrow"} <= set(cli._FORMAT_OPTIONS[opt]), opt
    assert wp.pyarrow_ipc_available()


def test_options(lang_en) -> None:
    assert ExportOptions("feather").feather_compression == "zstd"
    assert ExportOptions("ARROW", feather_compression=" LZ4 ").feather_compression == "lz4"
    assert ExportOptions("Feather").fmt == "feather" and ExportOptions("arrow").fmt == "arrow"
    for bad in ("snappy", "gzip", "", None, 1):
        with pytest.raises(UserInputError) as info:
            ExportOptions("feather", feather_compression=bad)
        assert info.value.key == "writer.invalid_option" and "zstd, lz4 or none" in info.value.message
    for fmt in ("feather", "arrow"):
        with pytest.raises(UserInputError) as info:
            ExportOptions(fmt, as_keyed=True)
        assert fmt in info.value.message
    assert ExportOptions.from_settings("feather").feather_compression == "zstd"
    # one ExportOptions for every format: the other format's option is kept, and ignored
    assert ExportOptions("feather", parquet_compression="snappy").parquet_compression == "snappy"
    assert ExportOptions("parquet", feather_compression="lz4").feather_compression == "lz4"


def test_gzip_names_are_refused(open_doc, out_dir: Path, lang_en) -> None:
    doc = open_doc(recs=[{"a": 1}], fmt="array")
    for fmt, name, shown in (("feather", "o.feather.gz", "Feather"), ("arrow", "o.arrow.gz", "Arrow IPC")):
        with pytest.raises(UserInputError) as info:
            export(doc, out_dir / name, ExportOptions(fmt))
        assert info.value.key == "writer.error.gzip_format"
        assert f"{shown} files cannot be gzip-compressed" in info.value.message
        with pytest.raises(UserInputError):
            export(doc, out_dir / name.removesuffix(".gz"), ExportOptions(fmt), gzip=True)
    assert files_in(out_dir) == []


# ----------------------------------------------------------------------------------
# the same table as Parquet
# ----------------------------------------------------------------------------------
@pytest.mark.parametrize("kw", [
    {},
    {"columns": SCALARS},
    {"columns": SCALARS + SPECIAL},
    {"array_mode": "rows"},
    {"explode": "orders"},
    {"columns": ["id", "orders[*].oid", "orders[*].sum", "name", "$index"], "array_mode": "rows"},
    {"columns": SCALARS, "join_sep": " | "},
])
def test_same_table_as_parquet(open_doc, out_dir: Path, kw: dict) -> None:
    recs = jsongen.records(80, seed=7)
    doc = open_doc(recs=recs, fmt="array")
    rf, _ = same_as_parquet(doc, out_dir / "o", **kw)
    assert rf.records == 80
    if kw.get("columns") == SCALARS:
        assert fcolumns(out_dir / "o.feather")["id"] == [r["id"] for r in recs]


@pytest.mark.parametrize("kind", ["jsonl", "keyed", "wrapped", "brace_list", "single", "stream"])
def test_every_document_kind(open_doc, out_dir: Path, kind: str) -> None:
    recs = jsongen.records(30, seed=11)
    kw: dict[str, Any] = {}
    if kind == "keyed":
        kw["keys"] = [f"k{i}" for i in range(30)]
    if kind == "wrapped":
        kw["record_path"] = "data"
    doc = open_doc(recs=recs, fmt=kind, **kw)
    rf, _ = same_as_parquet(doc, out_dir / "o")
    if kind == "keyed":
        assert rf.columns[0] == "$key" and fcolumns(out_dir / "o.feather")["$key"] == [f"k{i}" for i in range(30)]
    if kind == "wrapped":
        assert "writer.note.wrapper_not_written" in rf.warning_keys and "Feather" in rf.warnings[0]


def test_types_of_every_kind_of_value(open_doc, out_dir: Path, lang_en) -> None:
    doc = open_doc(data=TYPES_JSON)
    rf, _ = same_as_parquet(doc, out_dir / "t")
    out = out_dir / "t.feather"
    assert fkinds(out)["i"] == "int64" and fkinds(out)["f"] == "double" and fkinds(out)["b"] == "bool"
    assert fkinds(out)["big"] == "string" and fcolumns(out)["big"] == ["12345678901234567890", "2", None]
    assert str(fcolumns(out)["nz"][0]) == "-0.0"                    # the sign of -0.0 survives
    assert rf.warning_keys == ["writer.warn.feather_number_text"]
    assert rf.warning_params[0]["names"] == ["big", "inexact", "huge", "iw"] and rf.warning_params[0]["count"] == 4
    assert "4 columns hold numbers" in rf.warnings[0] and "big, inexact, huge, iw" in rf.warnings[0]


def test_unicode_and_odd_strings(open_doc, out_dir: Path) -> None:
    doc = open_doc(data=b'[{"s": "O\xca\xbbzbekiston \xd0\xa1\xd0\xb0\xd0\xbc\xd0\xb0\xd1\x80\xd2\x9b\xd0\xb0\xd0\xbd\xd0\xb4"},'
                        b' {"s": "lone \\ud800 and pair \\ud83d\\ude00"}, {"s": "q\\"uote, comma\\r\\n\\ttab \\u0000"}]')
    rf, _ = same_as_parquet(doc, out_dir / "u")
    data = fcolumns(out_dir / "u.feather")["s"]
    assert data[0] == "Oʻzbekiston Самарқанд" and data[1] == "lone \\ud800 and pair \U0001f600"
    assert data[2] == 'q"uote, comma\r\n\ttab \x00' and rf.records == 3


def test_huge_and_deep_records(open_doc, out_dir: Path, lang_en) -> None:
    recs = jsongen.records(20, seed=141)
    doc = open_doc(recs=recs, fmt="array", style="pretty2", settings=Settings(large_node_bytes=200))
    cols = ["id", "name", "contact.address.city", "orders[*].oid", "tags", "active", "$index"]
    same_as_parquet(doc, out_dir / "h", columns=cols)
    same_as_parquet(doc, out_dir / "r", columns=cols, explode="orders")
    rf, _ = same_as_parquet(doc, out_dir / "a")
    assert rf.skipped > 0 and rf.warning_keys == ["writer.warn.huge_skipped"]


# ----------------------------------------------------------------------------------
# the Arrow IPC file: one format, two names; compression; record batches
# ----------------------------------------------------------------------------------
def test_feather_and_arrow_are_one_file(open_doc, out_dir: Path) -> None:
    doc = open_doc(recs=jsongen.records(30, seed=4), fmt="array")
    rf = export(doc, out_dir / "x.feather", ExportOptions("feather"), workers=1)
    ra = export(doc, out_dir / "x.arrow", ExportOptions("arrow"), workers=1)
    export(doc, out_dir / "x.bin", ExportOptions("feather"), workers=1)          # any name, the format given
    data = (out_dir / "x.feather").read_bytes()
    assert data == (out_dir / "x.arrow").read_bytes() == (out_dir / "x.bin").read_bytes()
    assert data[:6] == b"ARROW1" and data[-6:] == b"ARROW1"                        # the Arrow IPC file format
    assert rf.fmt == "feather" and ra.fmt == "arrow" and rf.rows == ra.rows == 30
    assert read_ipc(out_dir / "x.arrow").equals(pf.read_table(out_dir / "x.feather"))


@pytest.mark.parametrize("compression", ["zstd", "lz4", "none"])
def test_compression_round_trip(open_doc, out_dir: Path, compression: str) -> None:
    recs = jsongen.records(40, seed=3)
    doc = open_doc(recs=recs, fmt="array")
    out = out_dir / f"{compression}.feather"
    export(doc, out, ExportOptions("feather", columns=["id", "name"], feather_compression=compression), workers=1)
    assert fcolumns(out) == {"id": [r["id"] for r in recs], "name": [r["name"] for r in recs]}


def test_compression_is_applied(open_doc, out_dir: Path) -> None:
    text = "jsonreshape " * 20
    doc = open_doc(recs=[{"id": i, "text": text} for i in range(2000)], fmt="array")
    sizes = {}
    for codec in ("zstd", "lz4", "none"):
        out = out_dir / f"{codec}.arrow"
        export(doc, out, ExportOptions("arrow", feather_compression=codec), workers=1)
        sizes[codec] = out.stat().st_size
        assert fcolumns(out)["text"][0] == text and fcolumns(out)["id"][-1] == 1999
    assert sizes["none"] > 2 * sizes["zstd"] and sizes["none"] > 2 * sizes["lz4"]
    assert len({(out_dir / f"{codec}.arrow").read_bytes() for codec in sizes}) == 3


def test_record_batches_follow_the_row_groups(open_doc, out_dir: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(wp, "ROW_GROUP_BYTES", 3000)
    monkeypatch.setattr(wp, "ROW_GROUP_MIN_ROWS", 10)
    monkeypatch.setattr(wp, "TYPE_ROWS", 25)
    monkeypatch.setattr(wp, "TYPE_MIN_ROWS", 25)
    doc = open_doc(recs=[{"id": i, "text": "x" * 100} for i in range(500)], fmt="array")
    export(doc, out_dir / "g.parquet", ExportOptions("parquet"), workers=1)
    rep = export(doc, out_dir / "g.feather", ExportOptions("feather"), workers=1)
    md = pq.ParquetFile(out_dir / "g.parquet").metadata
    groups = [md.row_group(i).num_rows for i in range(md.num_row_groups)]
    assert len(groups) > 3 and batch_rows(out_dir / "g.feather") == groups
    assert fcolumns(out_dir / "g.feather")["id"] == list(range(500)) and rep.rows == 500


def test_empty_selection_and_no_records(open_doc, out_dir: Path) -> None:
    doc = open_doc(recs=jsongen.records(5, seed=1), fmt="array")
    rep = export(doc, out_dir / "e.feather", ExportOptions("feather", columns=["id", "name"]), source=[])
    t = read_ipc(out_dir / "e.feather")
    assert t.num_rows == 0 and t.column_names == ["id", "name"] and rep.records == 0
    assert fkinds(out_dir / "e.feather") == {"id": "string", "name": "string"}    # nothing but null: string
    empty = open_doc(data=b"[]")
    rep = export(empty, out_dir / "n.arrow", ExportOptions("arrow"))
    t = read_ipc(out_dir / "n.arrow")
    assert t.num_rows == 0 and t.num_columns == 0 and rep.records == 0 and rep.columns == []


def test_plan_columns(open_doc, out_dir: Path) -> None:
    doc = open_doc(recs=jsongen.records(12, seed=5), fmt="array")
    plan = plan_columns(doc, ExportOptions("feather"))
    rep = export(doc, out_dir / "p.feather", ExportOptions("feather"), source=range(0, 6), column_plan=plan)
    assert rep.columns == plan.header and read_ipc(out_dir / "p.feather").column_names == plan.header


# ----------------------------------------------------------------------------------
# the two-pass restart; worker processes
# ----------------------------------------------------------------------------------
def test_conflict_restarts_in_two_passes(open_doc, out_dir: Path, small_types: int, lang_en) -> None:
    doc = open_doc(recs=conflict_records(), fmt="array")
    messages: list[str] = []
    progress = Progress(callback=lambda info: messages.append(info.message), min_interval=0)
    cols = ["id", "price", "late", "flag", "huge", "s", "arr", "nope"]
    rep = export(doc, out_dir / "c.feather", ExportOptions("feather", columns=cols), progress=progress, workers=1)
    export(doc, out_dir / "c.parquet", ExportOptions("parquet", columns=cols), workers=1)
    assert fkinds(out_dir / "c.feather") == pkinds(out_dir / "c.parquet")
    assert fcolumns(out_dir / "c.feather") == pcolumns(out_dir / "c.parquet")
    assert rep.records == rep.rows == 300 and rep.skipped == 0
    assert rep.warning_keys == ["writer.warn.feather_restarted", "writer.warn.feather_number_text",
                                "writer.warn.columns_not_found"]
    restarted = dict(zip(rep.warning_keys, rep.warning_params))["writer.warn.feather_restarted"]
    assert restarted["column"] == "flag" and restarted["index"] == 120 and restarted["rows"] == 50
    assert "Column flag" in rep.warnings[0] and "two passes" in rep.warnings[0]
    assert any(m.startswith("Deciding the column types") for m in messages)
    # the same bytes as the file written without a restart (the types decided from all rows)
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(wp, "TYPE_ROWS", 65536)
        mp.setattr(wp, "TYPE_MIN_ROWS", 1024)
        rep2 = export(doc, out_dir / "all.feather", ExportOptions("feather", columns=cols), workers=1)
    assert rep2.warning_keys == ["writer.warn.feather_number_text", "writer.warn.columns_not_found"]
    assert (out_dir / "all.feather").read_bytes() == (out_dir / "c.feather").read_bytes()
    assert files_in(out_dir) == ["all.feather", "c.feather", "c.parquet"]     # no temporary file of the attempt


def same_with_workers(doc: Any, opts: ExportOptions, out_dir: Path, workers: int = 2) -> writer.ExportReport:
    one = export(doc, out_dir / "one.feather", opts, workers=1)
    many = export(doc, out_dir / "many.feather", opts, workers=workers)
    assert (out_dir / "many.feather").read_bytes() == (out_dir / "one.feather").read_bytes()
    assert (many.records, many.rows, many.skipped, many.columns, many.warnings, many.warning_params) == \
        (one.records, one.rows, one.skipped, one.columns, one.warnings, one.warning_params)
    assert one.workers == 1
    return many


def test_real_workers_give_the_same_file(open_doc, out_dir: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(writer, "POOL_BLOCK_BYTES", 1500)
    doc = open_doc(recs=jsongen.records(90, seed=21), fmt="array")
    assert same_with_workers(doc, ExportOptions("feather", columns=SCALARS + SPECIAL), out_dir).workers == 2
    assert same_with_workers(doc, ExportOptions("arrow", explode="orders"), out_dir).workers == 2


def test_real_workers_with_a_restart(open_doc, out_dir: Path, monkeypatch: pytest.MonkeyPatch, small_types: int) -> None:
    monkeypatch.setattr(writer, "POOL_BLOCK_BYTES", 1200)
    doc = open_doc(recs=conflict_records(200), fmt="array")
    rep = same_with_workers(doc, ExportOptions("feather"), out_dir)
    assert rep.workers == 2 and "writer.warn.feather_restarted" in rep.warning_keys
    assert fkinds(out_dir / "many.feather")["late"] == "int64" and fkinds(out_dir / "many.feather")["flag"] == "string"


# ----------------------------------------------------------------------------------
# errors, cancellation, pyarrow missing
# ----------------------------------------------------------------------------------
def test_arrow_error_is_an_export_error(open_doc, out_dir: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    doc = open_doc(recs=jsongen.records(40, seed=191), fmt="array")
    target = out_dir / "y.feather"
    target.write_bytes(b"OLD")

    def boom(writer_: Any, part: Any, g: int) -> None:
        raise pa.ArrowInvalid("boom")

    monkeypatch.setattr(wp._ArrowBackend, "write_group", staticmethod(boom))
    with pytest.raises(ExportError) as info:
        export(doc, target, ExportOptions("feather", columns=["id"]), workers=1)
    assert info.value.key == "writer.error.feather" and "boom" in info.value.message
    assert target.read_bytes() == b"OLD" and files_in(out_dir) == ["y.feather"]


def test_cancel_keeps_the_old_target(open_doc, out_dir: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(writer, "BLOCK_BYTES", 2000)
    doc = open_doc(recs=jsongen.records(60, seed=181), fmt="array")
    target = out_dir / "c.feather"
    target.write_bytes(b"OLD CONTENT")
    for when in ("start", "middle", "finish"):
        token = CancelToken()

        def cb(info: Any, when: str = when, token: CancelToken = token) -> None:
            if when == "start" and info.message.startswith(("Eksport", "Export")):
                token.cancel()
            elif when == "middle" and info.done > 3000:
                token.cancel()

        progress = Progress(callback=cb, min_interval=0)
        if when == "finish":
            real = wp._Collector.finish

            def finish(self: Any) -> None:
                token.cancel()
                real(self)

            monkeypatch.setattr(wp._Collector, "finish", finish)
        with pytest.raises(Cancelled):
            export(doc, target, ExportOptions("feather", columns=["id", "name", "orders[*].oid"]), progress=progress,
                   cancel=token, workers=1)
        assert target.read_bytes() == b"OLD CONTENT" and files_in(out_dir) == ["c.feather"]
    monkeypatch.undo()


def test_pyarrow_missing(open_doc, out_dir: Path, monkeypatch: pytest.MonkeyPatch, lang_en) -> None:
    doc = open_doc(recs=[{"a": 1}], fmt="array")
    monkeypatch.setitem(sys.modules, "pyarrow", None)
    monkeypatch.setitem(sys.modules, "pyarrow.ipc", None)
    assert not wp.pyarrow_ipc_available()
    for fmt, name in (("feather", "o.feather"), ("arrow", "o.arrow")):
        with pytest.raises(ExportError) as info:
            export(doc, out_dir / name, ExportOptions(fmt))
        assert info.value.key == "writer.error.feather_missing"
        assert 'pip install "jsonreshape[parquet]"' in info.value.message
        assert 'pip install "jsonreshape[parquet]"' in info.value.hint
    assert files_in(out_dir) == []
    export(doc, out_dir / "o.csv", ExportOptions("csv"))       # the other formats still work
    assert files_in(out_dir) == ["o.csv"]


def test_docstrings_mention_feather() -> None:
    assert "Arrow IPC" in writer.__doc__ and "feather_compression" in writer.__doc__
    for word in ("Feather", "Arrow IPC", "_ArrowBackend", "feather_compression", "lz4", "two passes"):
        assert word in wp.__doc__, word


# ----------------------------------------------------------------------------------
# CLI
# ----------------------------------------------------------------------------------
def test_cli_convert_to_feather(run, src: Path, tmp_path: Path) -> None:
    out = tmp_path / "out.feather"
    code, _out, err = run("convert", src, out, "--columns", "id,name,orders[*].oid", "--sep", "|", "--lang", "en")
    assert code == 0, err
    assert fkinds(out) == {"id": "int64", "name": "string", "orders[*].oid": "string"}
    assert fcolumns(out)["id"] == [r["id"] for r in jsongen.records(12, seed=9)]
    assert "12 / 12 records" in err and "out.feather" in err
    # .arrow by its extension; --to arrow names the format for any extension: the same file
    code, _out, err = run("convert", src, tmp_path / "out.arrow", "--lang", "en")
    assert code == 0, err
    code, _out, err = run("convert", src, tmp_path / "out.bin", "--to", "arrow", "--lang", "en")
    assert code == 0, err
    assert (tmp_path / "out.bin").read_bytes() == (tmp_path / "out.arrow").read_bytes()
    # --feather-compression chooses the codec; exploded rows are reported
    code, _out, err = run("convert", src, tmp_path / "lz4.feather", "--feather-compression", "lz4", "--explode",
                          "orders", "--lang", "en")
    assert code == 0, err
    t = read_ipc(tmp_path / "lz4.feather")
    assert t.num_rows > 12 and f"{t.num_rows} rows" in err
    code, _out, err = run("convert", src, tmp_path / "bad.feather", "--feather-compression", "snappy", "--lang", "en")
    assert code == 1 and "snappy" in err and not (tmp_path / "bad.feather").exists()


def test_cli_option_compatibility(run, src: Path, tmp_path: Path) -> None:
    code, _out, err = run("convert", src, tmp_path / "o.feather", "--parquet-compression", "gzip", "--no-bom",
                          "--columns", "id", "--lang", "en")
    assert code == 0, err
    assert "--no-bom, --parquet-compression: not used by the feather format, ignored." in err
    code, _out, err = run("convert", src, tmp_path / "o.csv", "--feather-compression", "lz4", "--columns", "id",
                          "--lang", "en")
    assert code == 0 and "--feather-compression: not used by the csv format, ignored." in err
    code, _out, err = run("convert", src, tmp_path / "o.parquet", "--feather-compression", "lz4", "--columns", "id",
                          "--lang", "en")
    assert code == 0 and "--feather-compression: not used by the parquet format, ignored." in err
    code, _out, err = run("convert", src, tmp_path / "o.feather.gz", "--lang", "en")
    assert code == 1 and "--to json, jsonl, csv, xlsx, sqlite, parquet, feather or arrow" in err
    code, _out, err = run("convert", src, tmp_path / "o.arrow.gz", "--to", "arrow", "--lang", "en")
    assert code == 1 and "Arrow IPC files cannot be gzip-compressed" in err and not (tmp_path / "o.arrow.gz").exists()
    help_code, out, _err = run("convert", "--help", "--lang", "en")
    assert help_code == 0 and "--feather-compression" in out and "arrow" in out
