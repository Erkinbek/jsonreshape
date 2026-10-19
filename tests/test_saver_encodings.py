"""saver.save: the same encoding and compression as the source (docs/ARCHITECTURE.md §12.7).

UTF-8 with and without BOM, UTF-16 LE/BE with and without BOM, gzip (with a UTF-8 BOM or UTF-16 inside):
an in-place save keeps all of it (the BOM is written outside the validated stream, gzip with level 6 and
header mtime 0); Save As compresses iff the target name ends with ``.gz`` (with a warning when that
differs from the source); data after the source's gzip stream and a truncated stream are reported. The
saved text is exactly the oracle of ``tests/saver_helpers.py`` and reopens (``reopen_options``) to the
expected records.
"""

from __future__ import annotations

import gzip
import os
from pathlib import Path

import pytest

from jsonreshape import saver
from jsonreshape.reader import Document
from overlay_helpers import OverlayBuilder
from saver_helpers import ENCODINGS, apply_pattern, decode_saved, make_doc, oracle_text, reopen, source_traits

KINDS = ("array", "jsonl", "brace_list", "wrapped", "keyed", "single", "stream")


@pytest.fixture
def docs():
    opened: list[Document] = []
    yield opened
    for d in opened:
        d.close()


def edited(directory: Path, docs: list, kind: str, encoding: str, pattern: str = "mixed", **kw):
    name = "src.json.gz" if encoding.startswith("gzip") else "src.json"
    doc = make_doc(directory / "src", kind, kw.pop("style", "pretty2"), n=kw.pop("n", 16), encoding=encoding,
                   name=name, **kw)
    docs.append(doc)
    ob = OverlayBuilder(doc, directory / "layer")
    apply_pattern(ob, doc, pattern)
    doc.set_overlay(ob.state())
    return doc, ob


def check_file(path: Path, report: saver.SaveReport, want_text: bytes, records: list[bytes], *, encoding: str,
               bom: bool, gz: bool) -> None:
    raw = path.read_bytes()
    text, enc, has_bom, is_gz = decode_saved(raw)
    assert text == want_text
    assert (enc.replace("-bom", "") if enc == "utf-8-bom" else enc) == encoding.replace("-bom", "")
    assert has_bom == bom and is_gz == gz
    if gz:
        assert raw[3] == 0 and raw[4:8] == b"\0\0\0\0", "gzip header: no flags, mtime 0"
        assert gzip.decompress(raw)  # one valid member
    assert report.compression == ("gzip" if gz else None)
    new = reopen(path, report)
    try:
        assert new.error is None
        assert new.info.encoding == (("utf-8-bom" if bom else "utf-8") if encoding.startswith("utf-8") else encoding)
        assert new.info.compression == report.compression
        assert [new.record_bytes(i) for i in range(new.record_count)] == records
    finally:
        new.close()


@pytest.mark.parametrize("encoding", ENCODINGS)
@pytest.mark.parametrize("kind", KINDS)
def test_in_place_keeps_encoding_and_compression(tmp_path, docs, kind, encoding) -> None:
    doc, ob = edited(tmp_path, docs, kind, encoding)
    info_enc, bom, gz = source_traits(encoding)
    assert doc.info.encoding == info_enc and (doc.info.compression == "gzip") == gz
    src = doc.source_path
    before = src.read_bytes()
    want = oracle_text(doc, ob)
    report = saver.save(doc)
    assert report.in_place and report.encoding == info_enc
    assert report.backup is not None and report.backup.read_bytes() == before
    check_file(src, report, want, list(ob.records), encoding=info_enc, bom=bom, gz=gz)
    assert not [w for w in report.warning_keys if w in ("saver.warn.compressed", "saver.warn.uncompressed")]


@pytest.mark.parametrize("encoding", ENCODINGS)
@pytest.mark.parametrize("kind", ["array", "jsonl", "wrapped"])
def test_save_as_compression_follows_the_name(tmp_path, docs, kind, encoding, lang_en) -> None:
    doc, ob = edited(tmp_path, docs, kind, encoding)
    info_enc, bom, gz_src = source_traits(encoding)
    before = doc.source_path.read_bytes()
    want = oracle_text(doc, ob)
    for name, gz in (("plain.json", False), ("packed.json.gz", True), ("PACKED.JSON.GZ", True)):
        out = tmp_path / name
        report = saver.save(doc, out)
        check_file(out, report, want, list(ob.records), encoding=info_enc, bom=bom, gz=gz)
        changed = [w for w in report.warning_keys if w in ("saver.warn.compressed", "saver.warn.uncompressed")]
        if gz == gz_src:
            assert changed == []
        else:
            assert changed == ["saver.warn.compressed" if gz else "saver.warn.uncompressed"]
            assert (".gz" in report.warnings[report.warning_keys.index(changed[0])])
        out.unlink()
    assert doc.source_path.read_bytes() == before


def test_gzip_trailing_data_is_reported_and_dropped(tmp_path, docs, lang_en) -> None:
    text = b'[{"a": 1}, {"a": 2}]\n'
    p = tmp_path / "t.json.gz"
    p.write_bytes(gzip.compress(text, mtime=0) + b"GARBAGE")
    doc = Document.open(p)
    docs.append(doc)
    doc.build_index()
    assert "fileio.warning.gzip_trailing" in doc.prepared.warnings
    report = saver.save(doc)
    assert "saver.warn.gzip_trailing" in report.warning_keys
    assert gzip.decompress(p.read_bytes()) == text
    assert report.backup.read_bytes().endswith(b"GARBAGE")


def test_truncated_gzip_jsonl_is_saved_whole(tmp_path, docs, lang_en) -> None:
    lines = b"".join(b'{"i": %d, "pad": "%s"}\n' % (i, b"x" * 50) for i in range(400))
    packed = gzip.compress(lines, mtime=0)
    p = tmp_path / "t.jsonl.gz"
    p.write_bytes(packed[:len(packed) * 2 // 3])
    doc = Document.open(p)
    docs.append(doc)
    doc.build_index()
    assert "fileio.warning.gzip_truncated" in doc.prepared.warnings
    assert doc.error is None and doc.broken_count == 1          # the cut last line
    n = doc.record_count
    report = saver.save(doc, drop_broken=True)
    assert {"saver.warn.gzip_truncated", "saver.warn.broken_dropped"} <= set(report.warning_keys)
    out = gzip.decompress(p.read_bytes())
    assert out.count(b"\n") == n and out.endswith(b"}\n")


def test_multi_member_gzip_becomes_one_member(tmp_path, docs) -> None:
    p = tmp_path / "m.jsonl.gz"
    p.write_bytes(gzip.compress(b'{"a":1}\n', mtime=0) + gzip.compress(b'{"a":2}\n', mtime=0))
    doc = Document.open(p)
    docs.append(doc)
    doc.build_index()
    ob = OverlayBuilder(doc, tmp_path / "layer")
    ob.insert(1, b'{"a":1.5}')
    doc.set_overlay(ob.state())
    saver.save(doc)
    assert gzip.decompress(p.read_bytes()) == b'{"a":1}\n{"a":1.5}\n{"a":2}\n'
    import zlib

    d = zlib.decompressobj(31)
    d.decompress(p.read_bytes())
    assert d.eof and d.unused_data == b"", "a single member"


def test_utf16_non_bmp_and_cyrillic_round_trip(tmp_path, docs) -> None:
    text = '[{"s": "Ўзбекистон 😀 o\'zbek"}, {"s": "ok"}]'.encode("utf-8")
    for enc, bom in (("utf-16-le", True), ("utf-16-be", False)):
        d = tmp_path / enc
        doc = make_doc(d, "array", text=text, open_kw={"format": "array"},
                       encoding=enc + ("-bom" if bom else ""))
        docs.append(doc)
        ob = OverlayBuilder(doc, d / "layer")
        ob.set(1, '{"s": "Тошкент 𝄞"}'.encode("utf-8"))
        doc.set_overlay(ob.state())
        saver.save(doc)
        raw = doc.source_path.read_bytes()
        if bom:
            assert raw.startswith("\ufeff".encode(enc))
            body = raw[2:].decode(enc)
        else:
            assert not raw.startswith(b"\xfe\xff")
            body = raw.decode(enc)
        assert body == '[{"s": "Ўзбекистон 😀 o\'zbek"}, {"s": "Тошкент 𝄞"}]'


def test_bom_only_output_reopens_empty(tmp_path, docs) -> None:
    """A UTF-8-BOM JSON Lines file whose every record is deleted: the BOM alone; it reopens with 0 records."""
    doc = make_doc(tmp_path / "s", "jsonl", n=3, encoding="utf-8-bom")
    docs.append(doc)
    ob = OverlayBuilder(doc, tmp_path / "layer")
    ob.delete(0, 1, 2)
    doc.set_overlay(ob.state())
    report = saver.save(doc)
    assert doc.source_path.read_bytes() == b"\xef\xbb\xbf"
    new = reopen(doc.source_path, report)
    try:
        assert new.record_count == 0 and new.error is None and new.info.encoding == "utf-8-bom"
    finally:
        new.close()
    # gzip: an empty member
    doc2 = make_doc(tmp_path / "g", "jsonl", n=2, encoding="gzip")
    docs.append(doc2)
    ob2 = OverlayBuilder(doc2, tmp_path / "layer2")
    ob2.delete(0, 1)
    doc2.set_overlay(ob2.state())
    report = saver.save(doc2)
    assert gzip.decompress(doc2.source_path.read_bytes()) == b""
    new = reopen(doc2.source_path, report)
    try:
        assert new.record_count == 0
    finally:
        new.close()
    assert os.path.exists(report.backup)
