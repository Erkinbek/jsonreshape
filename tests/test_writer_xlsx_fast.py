"""Stage 7: jsonreshape's own XLSX writer (``jsonreshape.writer_xlsx``) against the xlsxwriter exporter of stages 1-6.

* Values: every workbook (sheets, ``files`` parts and their names) holds exactly the cells the xlsxwriter exporter
  wrote -- read back with openpyxl: the same sheets, frozen header rows, bold header cells, and every cell with its
  type and value (numbers keep their exact value: the same ``%.16G`` text; booleans, blanks, long texts cut to
  32 767 UTF-16 units, control characters, ``_xHHHH_`` look-alikes, lone surrogates, XML specials, whitespace at the
  ends) -- and the same report (rows, records, columns, warnings with their parameters). The reference is the old
  exporter itself (:func:`ref_export_xlsx`, xlsxwriter in ``constant_memory`` mode, one thread). Every document
  kind, edited documents, selections, rows mode, splits, tiny jobs, ``workers=1`` and pooled jobs.
* One deliberate difference: a text that starts with ``<r>`` and ends with ``</r>`` is a text; xlsxwriter wrote it
  as raw rich-text XML (a workbook Excel refuses to open).
* The package: content types and relationships name every part, every part is well-formed XML, the sheet XML has
  no position attributes and blank cells inside a row only; zip64 end records and offsets (forced) read back; a
  sheet member of 4 GiB or more is refused (``writer.error.xlsx_too_large``) leaving no file; the CRC-32 of
  concatenated pieces.
* Real worker processes: cancellation leaves no output and no temporary file.
"""

from __future__ import annotations

import os
import random
import tempfile
import xml.etree.ElementTree as ET
import zipfile
import zlib
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

import jsongen
from jsonreshape import codec, writer, writer_xlsx
from jsonreshape.errors import Cancelled, ExportError
from jsonreshape.tasks import CancelToken, Progress
from jsonreshape.writer import ExportOptions, export
from overlay_helpers import make_edited
from test_perf6_csv_pool import (  # noqa: F401 - fixtures
    KINDS,
    SCALARS,
    SPECIAL,
    backend,
    docs,
    fake_pool,
    files_in,
    mixed_array,
    open_kind,
    open_raw,
)

openpyxl = pytest.importorskip("openpyxl")


# ----------------------------------------------------------------------------------
# the reference: the XLSX exporter of stages 1-6 (xlsxwriter, constant_memory, one thread)
# ----------------------------------------------------------------------------------
def ref_export_xlsx(ctx: Any, out: Path) -> list[Path]:
    """The cells, splits and warnings exactly as ``writer._export_xlsx`` wrote them before stage 7 (its
    ``_XlsxBook``: strings through ``write_string`` after the surrogate escape and the cut to Excel's limit,
    booleans ``write_boolean``, numbers ``write_number``; blanks skipped; a bold header on every sheet)."""
    import xlsxwriter

    opts = ctx.opts
    cells = writer._XlsxCells(opts.join_sep)
    builder = writer._builder(ctx, cells)
    header = [c.text for c in ctx.columns]
    files_mode = opts.xlsx_split == "files"
    tmpdir = tempfile.mkdtemp(prefix="xlsxref-", dir=str(out.parent))
    st = SimpleNamespace(wb=None, ws=None, bold=None, row=0, sheets=0, truncated=0, staged=[])

    def clean(s: str) -> str:
        if ctx.check_surrogates and not s.isascii() and writer._SURROGATE_RE.search(s):
            s = writer._escape_surrogates(s)
        if len(s) > writer._XLSX_SAFE_CHARS:
            data = s.encode("utf-16-le", "surrogatepass")
            if len(data) > 2 * writer.XLSX_MAX_STRING:
                data = data[:2 * writer.XLSX_MAX_STRING]
                if 0xD8 <= data[-1] <= 0xDB:
                    data = data[:-2]
                s = data.decode("utf-16-le", "surrogatepass")
                st.truncated += 1
        return s

    def new_sheet() -> None:
        ws = st.wb.add_worksheet()
        ws.freeze_panes(1, 0)
        for c, text in enumerate(header):
            ws.write_string(0, c, clean(text), st.bold)
        st.ws, st.row = ws, 1
        st.sheets += 1

    def open_book() -> None:
        path = out.with_name(f".ref{len(st.staged)}{out.suffix}") if files_mode else out
        st.staged.append(path)
        st.wb = xlsxwriter.Workbook(str(path), {"constant_memory": True, "strings_to_numbers": False,
                                                "strings_to_formulas": False, "strings_to_urls": False,
                                                "tmpdir": tmpdir})
        st.bold = st.wb.add_format({"bold": True})
        new_sheet()

    open_book()
    for batch in writer._iter_row_batches(ctx, builder):
        for row in batch:
            if st.row >= opts.xlsx_max_rows:
                if files_mode:
                    st.wb.close()
                    open_book()
                else:
                    new_sheet()
            ws, r = st.ws, st.row
            for c, v in enumerate(row):
                if v is None:
                    continue
                tv = type(v)
                if tv is str:
                    if not v:
                        continue
                    if ctx.check_surrogates or len(v) > writer._XLSX_SAFE_CHARS:
                        v = clean(v)
                    ws.write_string(r, c, v)
                elif tv is bool:
                    ws.write_boolean(r, c, v)
                else:
                    ws.write_number(r, c, v)
            st.row = r + 1
        ctx.rows += len(batch)
    st.wb.close()
    for p in Path(tmpdir).iterdir():
        p.unlink()
    os.rmdir(tmpdir)
    if files_mode:
        n = len(st.staged)
        files = [out] if n == 1 else [out.with_name(f"{out.stem}_{k}{out.suffix}") for k in range(1, n + 1)]
        for src, dst in zip(st.staged, files):
            os.replace(src, dst)
        if n > 1:
            ctx.warn("writer.warn.xlsx_files", add=n)
    else:
        files = [out]
        if st.sheets > 1:
            ctx.warn("writer.warn.xlsx_sheets", add=st.sheets)
    if cells.numbers_as_text:
        ctx.warn("writer.warn.xlsx_number_text", add=cells.numbers_as_text)
    if st.truncated:
        ctx.warn("writer.warn.xlsx_truncated", add=st.truncated, max=writer.XLSX_MAX_STRING)
    writer._warn_unfound(ctx, builder)
    return files


# ----------------------------------------------------------------------------------
# helpers
# ----------------------------------------------------------------------------------
def book_view(path: Path) -> list[tuple]:
    """Every sheet as openpyxl reads it: name, frozen panes, rows of (type, Python type, value, bold) without the
    trailing blanks."""
    wb = openpyxl.load_workbook(path)
    out = []
    for ws in wb.worksheets:
        rows = []
        for row in ws.iter_rows():
            cells = [(c.data_type, type(c.value).__name__, c.value, bool(c.font and c.font.b)) for c in row]
            while cells and cells[-1][2] is None:
                cells.pop()
            rows.append(cells)
        while rows and not rows[-1]:
            rows.pop()
        out.append((ws.title, ws.freeze_panes, rows))
    return out


def view(rep: writer.ExportReport) -> tuple:
    return (rep.records, rep.rows, rep.skipped, rep.columns, rep.warning_keys, rep.warning_params, rep.warnings,
            [p.name for p in rep.files])


def same_values(doc: Any, opts: ExportOptions, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, workers: int = 3,
                name: str = "o", **kw: Any) -> tuple[writer.ExportReport, writer.ExportReport]:
    """This program's workbook(s) hold exactly the cells of the xlsxwriter exporter's (and the reports match)."""
    ref_dir, new_dir = tmp_path / f"{name}_ref", tmp_path / f"{name}_new"
    ref_dir.mkdir()
    new_dir.mkdir()
    with monkeypatch.context() as m:
        m.setitem(writer._EXPORTERS, "xlsx", ref_export_xlsx)
        ref = export(doc, ref_dir / "book.xlsx", opts, workers=1, **kw)
    rep = export(doc, new_dir / "book.xlsx", opts, workers=workers, **kw)
    assert view(rep) == view(ref)
    for a, b in zip(ref.files, rep.files):
        assert book_view(b) == book_view(a), (a.name, b.name)
    assert files_in(new_dir) == sorted(p.name for p in rep.files)
    return ref, rep


def xlsx_records() -> list[bytes]:
    """Raw records for the XLSX rules (no ``<r>...</r>`` text: the deliberate difference has its own test)."""
    raws = [
        '{"id": 1e400, "big": 123456789012345678901234567890, "f": 1.10, "z": -0, "nz": -0.0, "e": 1E3, "x": 0.1,'
        ' "pi": 3.141592653589793, "i15": 999999999999999, "i16": 1234567890123456, "e20": 1e20, "sub": 5e-324,'
        ' "neg": -12.5e-3, "tiny": 1e-400, "e307": 1e307, "e308": 1.5e308, "b": true, "n": null}',
        '{"id": 2, "s": " lead", "t": "trail ", "u": "\\n nl", "c": "ctl\\u0001\\u001f\\r\\t", "x": "_x0041_ lit",'
        ' "X": "_X00ff_ and _x12345_", "nc": "\\ufffe\\uffff", "amp": "a & b < c > d \\" q \' &amp;",'
        ' "nb": "\\u00a0nbsp\\u2028", "e": "", "ws": "   ", "nl": "\\n", "bt": true, "bf": false}',
        '{"id": 3, "long": "' + "Ўз" * 17000 + '", "longast": "' + "a" * 32766 + '\\ud83d\\ude00tail",'
        ' "fits": "' + "b" * 32767 + '", "lone": "x\\ud800y", "pair": "\\ud83d\\ude00", "\\udc00key": 1}',
        '{"id": 4, "tags": ["a;b", "c", 1, null, true, {"k": [1, "\\u0002"]}], "orders": [{"oid": 1, "sum": 1.10}],'
        ' "emptyarr": [], "emptyobj": {}, "one": [7], "onestr": ["q"], "nested": [[1, 2], []]}',
        '[1, [2, 3], {"in": "array record"}]',
        '"a scalar record"',
        "null",
        "12345678901234567890",
        '{"id": 5, "toodeep": ' + "[" * 20000 + "0" + "]" * 20000 + "}",
        '{"id": 6, "brackets": [' + ",".join(["[]"] * (writer.POOL_SAFE_BRACKETS + 10)) + "]}",
    ]
    return [r.encode("utf-8") for r in raws]


def xlsx_doc(tmp_path: Path, docs: list, seed: int, n: int = 60, name: str = "x.json") -> Any:
    recs = [jsongen.encode(r, "compact", seed + i) for i, r in enumerate(jsongen.records(n, seed=seed))]
    recs += [jsongen.encode(v, "compact", seed) for v in jsongen.records(12, seed=seed, kind="value")]
    rng = random.Random(seed)
    for raw in xlsx_records():
        recs.insert(rng.randrange(len(recs) + 1), raw)
    return open_raw(tmp_path, docs, b"[" + b",\n".join(recs) + b"]", name)


OPTIONS = [
    lambda: ExportOptions("xlsx"),
    lambda: ExportOptions("xlsx", columns=SCALARS + SPECIAL + ["s", "t", "long", "longast", "big", "f", "z", "nz"]),
    lambda: ExportOptions("xlsx", array_mode="rows"),
    lambda: ExportOptions("xlsx", explode="orders", join_sep=" | "),
    lambda: ExportOptions("xlsx", xlsx_max_rows=7),
    lambda: ExportOptions("xlsx", columns=["id", "orders[*].oid", "name", "tags"], array_mode="rows", xlsx_max_rows=9,
                          xlsx_split="files"),
]


# ----------------------------------------------------------------------------------
# values: equal to the xlsxwriter workbooks
# ----------------------------------------------------------------------------------
def test_special_values_both_backends(tmp_path, docs, fake_pool, backend, monkeypatch) -> None:
    doc = xlsx_doc(tmp_path, docs, 3)
    for i, make in enumerate(OPTIONS):
        monkeypatch.setattr(writer, "POOL_BLOCK_BYTES", [1, 700, 1 << 20][i % 3])
        ref, rep = same_values(doc, make(), tmp_path, monkeypatch, name=f"o{i}")
        assert rep.workers == 3
    keys = set(ref.warning_keys)
    ref, _ = same_values(doc, OPTIONS[1](), tmp_path, monkeypatch, workers=1, name="w1")
    keys |= set(ref.warning_keys)
    assert {"writer.warn.xlsx_number_text", "writer.warn.xlsx_truncated", "writer.warn.xlsx_files"} <= keys


@pytest.mark.parametrize("kind", KINDS)
def test_every_kind(tmp_path, docs, fake_pool, monkeypatch, kind) -> None:
    rng = random.Random(KINDS.index(kind) + 50)
    doc = open_kind(tmp_path, docs, kind, jsongen.records(45, seed=KINDS.index(kind)), seed=2,
                    style=rng.choice(["compact", "pretty2", "messy"]))
    monkeypatch.setattr(writer, "POOL_BLOCK_BYTES", rng.choice([300, 2000]))
    for i in (0, 2, 4):
        same_values(doc, OPTIONS[i](), tmp_path, monkeypatch, name=f"{kind}{i}")


def test_sheet_limit_inside_a_job(tmp_path, docs, fake_pool, monkeypatch) -> None:
    """Big jobs cut at the sheet limit (a piece inflated, cut at the row and deflated again), sheets and files."""
    doc = open_kind(tmp_path, docs, "array", jsongen.records(120, seed=8))
    monkeypatch.setattr(writer, "POOL_BLOCK_BYTES", 1 << 20)                 # one job: every row
    for split, sizes in (("sheets", (2, 60, 120, 121, 122)), ("files", (3, 121))):
        for rows in sizes:                    # (121: the header and every row exactly)
            same_values(doc, ExportOptions("xlsx", columns=SCALARS, xlsx_max_rows=rows, xlsx_split=split), tmp_path,
                        monkeypatch, name=f"{split}{rows}")


@pytest.mark.parametrize("kind,scenario", [("array", "structure"), ("keyed", "mixed"), ("wrapped", "wrapper"),
                                           ("jsonl", "every3")])
def test_edited_documents(tmp_path, fake_pool, monkeypatch, kind, scenario) -> None:
    doc, _ob, exp = make_edited(tmp_path / "e", kind, scenario, n=40, seed=5)
    try:
        monkeypatch.setattr(writer, "POOL_BLOCK_BYTES", 600)
        _, rep = same_values(doc, ExportOptions("xlsx"), tmp_path, monkeypatch, name="ed")
        plain = export(exp, tmp_path / "exp.xlsx", ExportOptions("xlsx"), workers=1)
        assert book_view(plain.files[0]) == book_view(rep.files[0])          # the edits are exported
    finally:
        doc.close()
        exp.close()


def test_selections(tmp_path, docs, fake_pool, monkeypatch) -> None:
    doc = xlsx_doc(tmp_path, docs, 4, n=50)
    monkeypatch.setattr(writer, "POOL_BLOCK_BYTES", 900)
    n = doc.record_count
    for i, source in enumerate(([5, 3, 3, 40, 0, 41, 5], range(10, n, 7), list(range(n - 1, -1, -1)))):
        same_values(doc, ExportOptions("xlsx", columns=SCALARS + ["$index", "s", "long"]), tmp_path, monkeypatch,
                    name=f"s{i}", source=source)
        same_values(doc, ExportOptions("xlsx"), tmp_path, monkeypatch, name=f"a{i}", source=source)


def test_rich_text_lookalike_is_a_text(tmp_path, docs) -> None:
    """xlsxwriter wrote a text "<r>...</r>" as raw rich-text XML (Excel refuses that workbook); it is a text now."""
    texts = ["<r>raw</r>", "<r><t>x</t></r>", "<r>", "a</r>"]
    doc = open_kind(tmp_path, docs, "array", [{"id": i, "s": s} for i, s in enumerate(texts)])
    rep = export(doc, tmp_path / "r.xlsx", ExportOptions("xlsx"))
    rows = list(openpyxl.load_workbook(rep.files[0]).active.iter_rows(values_only=True))
    assert rows == [("id", "s")] + [(i, s) for i, s in enumerate(texts)]
    with zipfile.ZipFile(rep.files[0]) as z:
        assert b"&lt;r&gt;raw&lt;/r&gt;" in z.read("xl/worksheets/sheet1.xml")


# ----------------------------------------------------------------------------------
# the package
# ----------------------------------------------------------------------------------
NS = {"m": "http://schemas.openxmlformats.org/spreadsheetml/2006/main",
      "ct": "http://schemas.openxmlformats.org/package/2006/content-types",
      "rel": "http://schemas.openxmlformats.org/package/2006/relationships"}


def check_package(path: Path) -> dict[str, bytes]:
    """A valid SpreadsheetML package: returns its members."""
    with zipfile.ZipFile(path) as z:
        assert z.testzip() is None                                    # every CRC
        parts = {n: z.read(n) for n in z.namelist()}
        assert all(i.compress_type == zipfile.ZIP_DEFLATED for i in z.infolist())
    for name, data in parts.items():
        ET.fromstring(data)                                           # well-formed
    types = ET.fromstring(parts["[Content_Types].xml"])
    overrides = {e.get("PartName") for e in types.findall("ct:Override", NS)}
    assert {e.get("Extension") for e in types.findall("ct:Default", NS)} == {"rels", "xml"}
    assert overrides == {"/" + n for n in parts if not n.endswith(".rels") and n != "[Content_Types].xml"}
    rels = ET.fromstring(parts["_rels/.rels"])
    assert {e.get("Target") for e in rels.findall("rel:Relationship", NS)} <= set(parts)
    wb_rels = ET.fromstring(parts["xl/_rels/workbook.xml.rels"])
    targets = {e.get("Id"): "xl/" + e.get("Target") for e in wb_rels.findall("rel:Relationship", NS)}
    assert set(targets.values()) <= set(parts)
    wb = ET.fromstring(parts["xl/workbook.xml"])
    sheets = wb.findall("m:sheets/m:sheet", NS)
    rid = "{http://schemas.openxmlformats.org/officeDocument/2006/relationships}id"
    for k, sheet in enumerate(sheets, 1):
        assert sheet.get("name") == f"Sheet{k}"
        xml = ET.fromstring(parts[targets[sheet.get(rid)]])
        pane = xml.find("m:sheetViews/m:sheetView/m:pane", NS)
        assert pane.get("state") == "frozen" and pane.get("ySplit") == "1"
        for row in xml.iter(f"{{{NS['m']}}}row"):
            assert row.get("r") is None
            cells = list(row)
            assert all(c.get("r") is None for c in cells)
            assert not cells or len(cells[-1]) or cells[-1].attrib    # no trailing blank cell
    return parts


def test_package_parts_are_valid(tmp_path, docs) -> None:
    doc = xlsx_doc(tmp_path, docs, 6, n=30)
    rep = export(doc, tmp_path / "p.xlsx", ExportOptions("xlsx", xlsx_max_rows=10))
    assert len(openpyxl.load_workbook(rep.files[0]).sheetnames) > 3
    parts = check_package(rep.files[0])
    sheet = parts["xl/worksheets/sheet1.xml"].decode("utf-8")
    assert '<c s="1" t="inlineStr"><is><t>id</t></is></c>' in sheet          # the bold header (style 1)
    assert "<c/>" in sheet and "<dimension" not in sheet
    rep = export(doc, tmp_path / "f.xlsx", ExportOptions("xlsx", columns=["id"], xlsx_max_rows=20,
                                                         xlsx_split="files"))
    assert len(rep.files) > 2
    for p in rep.files:
        check_package(p)
    empty = open_kind(tmp_path, docs, "array", [], name="empty.json")
    rep = export(empty, tmp_path / "e.xlsx", ExportOptions("xlsx"))
    check_package(rep.files[0])
    assert list(openpyxl.load_workbook(rep.files[0]).active.iter_rows(values_only=True)) in ([], [()])


def test_zip64_structures(tmp_path, docs, monkeypatch) -> None:
    """Every offset written as zip64 (the archive over 4 GiB, forced): zipfile and openpyxl read it the same."""
    doc = xlsx_doc(tmp_path, docs, 7, n=40)
    plain = export(doc, tmp_path / "plain.xlsx", ExportOptions("xlsx", xlsx_max_rows=12))
    monkeypatch.setattr(writer_xlsx, "ZIP64_LIMIT", 0)
    rep = export(doc, tmp_path / "z64.xlsx", ExportOptions("xlsx", xlsx_max_rows=12))
    data = rep.files[0].read_bytes()
    assert b"PK\x06\x06" in data and b"PK\x06\x07" in data
    parts = check_package(rep.files[0])
    with zipfile.ZipFile(plain.files[0]) as z:
        assert {n: z.read(n) for n in z.namelist() if n != "docProps/core.xml"} == \
            {n: d for n, d in parts.items() if n != "docProps/core.xml"}
    assert book_view(rep.files[0]) == book_view(plain.files[0])


@pytest.mark.parametrize("split", ["sheets", "files"])
def test_sheet_member_too_large(tmp_path, docs, monkeypatch, split) -> None:
    doc = open_kind(tmp_path, docs, "array", [{"id": i, "name": f"name {i}"} for i in range(400)])
    out = tmp_path / "out"
    out.mkdir()
    (out / "o.xlsx").write_bytes(b"OLD")
    monkeypatch.setattr(writer_xlsx, "MEMBER_LIMIT", 8000)                  # a sheet member of 8000 bytes or more
    with pytest.raises(ExportError) as ei:
        export(doc, out / "o.xlsx", ExportOptions("xlsx", xlsx_split=split))
    assert ei.value.key == "writer.error.xlsx_too_large"
    assert files_in(out) == ["o.xlsx"] and (out / "o.xlsx").read_bytes() == b"OLD"
    rep = export(doc, out / "o.xlsx", ExportOptions("xlsx", xlsx_split=split, xlsx_max_rows=50))   # (the hint)
    assert len(rep.files) == (1 if split == "sheets" else 9) and rep.rows == 400


def test_crc32_combine() -> None:
    rng = random.Random(3)
    for _ in range(200):
        a = rng.randbytes(rng.randrange(0, 3000))
        b = rng.randbytes(rng.choice([0, 1, 7, rng.randrange(0, 70000)]))
        assert writer_xlsx.crc32_combine(zlib.crc32(a), zlib.crc32(b), len(b)) == zlib.crc32(a + b)


def test_row_xml_rules() -> None:
    xml = writer_xlsx._RowXml()
    rows = [[None, "", "a", None, 1, -0.0, 1e20, 0.1, True, False, None, ""], [], [" x", "y\n", "_x0041_", "\x01<&>"]]
    assert xml.rows(rows).decode("utf-8") == (
        '<row><c/><c/><c t="inlineStr"><is><t>a</t></is></c><c/><c><v>1</v></c><c><v>-0</v></c><c><v>1E+20</v></c>'
        '<c><v>0.1</v></c><c t="b"><v>1</v></c><c t="b"><v>0</v></c></row><row></row>'
        '<row><c t="inlineStr"><is><t xml:space="preserve"> x</t></is></c>'
        '<c t="inlineStr"><is><t xml:space="preserve">y\n</t></is></c>'
        '<c t="inlineStr"><is><t>_x005F_x0041_</t></is></c><c t="inlineStr"><is><t>_x0001_&lt;&amp;&gt;</t></is></c>'
        "</row>")
    assert xml.truncated == 0
    cut = xml.rows([["\U0001F600" * 20000]]).decode("utf-8")
    assert xml.truncated == 1 and cut.count("\U0001F600") == writer.XLSX_MAX_STRING // 2
    piece = writer_xlsx._piece(b"<row></row>" * 3, 3)
    assert zlib.decompressobj(-15).decompress(piece[0] + b"\x03\x00") == b"<row></row>" * 3
    assert piece[1:4] == (zlib.crc32(b"<row></row>" * 3), 33, 3)


# ----------------------------------------------------------------------------------
# real worker processes
# ----------------------------------------------------------------------------------
def test_real_workers_values_and_cancel(tmp_path, docs, monkeypatch) -> None:
    doc = xlsx_doc(tmp_path, docs, 9, n=150)
    monkeypatch.setattr(writer, "POOL_BLOCK_BYTES", 3000)
    _, rep = same_values(doc, ExportOptions("xlsx", xlsx_max_rows=40), tmp_path, monkeypatch, workers=2)
    assert rep.workers == 2
    big = open_raw(tmp_path, docs, mixed_array(23, 700), "m.json")
    out = tmp_path / "c"
    out.mkdir()
    (out / "o.xlsx").write_bytes(b"OLD")
    token = CancelToken()

    def cb(info: Any) -> None:
        if info.total and info.done > info.total // 3:
            token.cancel()

    for split in ("sheets", "files"):
        with pytest.raises(Cancelled):
            export(big, out / "o.xlsx", ExportOptions("xlsx", xlsx_split=split, xlsx_max_rows=50), workers=2,
                   cancel=token, progress=Progress(callback=cb, min_interval=0))
        assert files_in(out) == ["o.xlsx"] and (out / "o.xlsx").read_bytes() == b"OLD"
        token = CancelToken()
    assert codec is not None
