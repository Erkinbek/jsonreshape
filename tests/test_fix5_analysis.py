"""Stage-5 review "analysis": schema / jschema / smartvalues findings.

* R5A-X2: ``infer_schema`` crashed (``decimal.InvalidOperation``) on ``1e99999999999999999999``.
* R5A-3: jschema compared numbers through ``Decimal(raw)``: such a token made ``validate`` raise
  and a schema bound with it fail with a raw error.
* R5A-4: the whole-document fallback stopped silently at 100 000 errors (wrong invalid counts).
* R5A-8: a source naming a position twice validated it twice.
* R5A-9: Python-only regex syntax was accepted with Python's meaning.
* R5A-12: the signature alone (or a corrupt header) was "an image of unknown size".
* observation: ``ValidationReport.complete`` tells callers that records were skipped.
"""

from __future__ import annotations

import base64
import random
import struct
from decimal import Decimal
from pathlib import Path

import pytest

from jsonreshape import codec, schema
from jsonreshape import jschema as js
from jsonreshape.errors import UserInputError
from jsonreshape.jschema import compile_schema, ecma_regex, validate
from jsonreshape.reader import Document
from jsonreshape.schema import infer_schema, is_integral
from jsonreshape.smartvalues import detect, image_bytes

HUGE = "1e99999999999999999999"


@pytest.fixture
def make_doc(tmp_path: Path):
    docs: list[Document] = []

    def make(data: bytes, name: str = "doc.json") -> Document:
        p = tmp_path / f"{len(docs)}-{name}"
        p.write_bytes(data)
        doc = Document.open(p)
        doc.build_index()
        docs.append(doc)
        return doc

    yield make
    for d in docs:
        d.close()


# ==================================================================================
# R5A-X2: schema
# ==================================================================================
def test_infer_schema_with_numbers_beyond_decimal(make_doc) -> None:
    vals = [HUGE, "5", "-" + HUGE, "1e-99999999999999999999", "2e99999999999999999999", "0e99999999999999999999",
            "1e400", "-0.5"]
    doc = make_doc(("[" + ",".join('{"n": %s}' % v for v in vals) + "]").encode())
    sch = infer_schema(doc, sample=None)
    fi = sch.find("n")
    assert fi.num_range == ("-" + HUGE, "2e99999999999999999999")
    # integral: HUGE, 5, -HUGE, 2e.., 0e.., 1e400 (not 1e-.., not -0.5)
    assert fi.integers == 6


@pytest.mark.parametrize("tok, want", [
    (HUGE, True), ("-" + HUGE, True), ("1.5e99999999999999999999", True), ("1e-99999999999999999999", False),
    ("0e-99999999999999999999", True), ("0.0e99999999999999999999", True), ("1e" + "7" * 5000, True),
    ("1e-" + "7" * 5000, False), ("120e-99999999999999999999", False),
])
def test_is_integral_beyond_decimal(tok: str, want: bool) -> None:
    assert is_integral(codec.loads(tok)) is want


def _random_token(rng: random.Random) -> str:
    sign = rng.choice(["", "-"])
    ip = rng.choice(["0", "1", "7", "12", "100", "3" * rng.randint(1, 30)])
    fp = rng.choice(["", "." + "".join(rng.choice("0123456789") for _ in range(rng.randint(1, 12)))])
    ep = rng.choice(["", "e%d" % rng.randint(-40, 40), "E+%d" % rng.randint(0, 40), "e-0%d" % rng.randint(0, 9)])
    return sign + ip + fp + ep


def test_beyond_decimal_key_compares_exactly() -> None:
    """The comparison key of tokens Decimal cannot hold, checked on tokens it can: the same
    order as Decimal against int, float, Decimal and its own kind."""
    rng = random.Random(4)
    for _ in range(3000):
        a, b = _random_token(rng), _random_token(rng)
        ka, da, db = schema._BeyondDecimal(a), Decimal(a), Decimal(b)
        for other, exact in ((schema._BeyondDecimal(b), db), (db, db), (int(db) if db == db.to_integral_value() else db, db)):
            assert (ka < other) == (da < exact) and (ka > other) == (da > exact) and (ka == other) == (da == exact), (a, b)
        f = float(db)
        if f == f and abs(f) != float("inf"):
            fd = Decimal(repr(f))
            assert (ka < f) == (da < fd) and (ka > f) == (da > fd), (a, f)
    huge, big = schema._BeyondDecimal(HUGE), schema._BeyondDecimal("2e99999999999999999999")
    assert Decimal("1e999999999999999999") < huge < big and not huge < 0
    assert schema._BeyondDecimal("-" + HUGE) < -(10 ** 4000) < 0 < schema._BeyondDecimal("1e-99999999999999999999") < 1e-300


# ==================================================================================
# R5A-3: jschema exact numbers
# ==================================================================================
@pytest.mark.parametrize("keyword, bound, tok, valid", [
    ("minimum", 0, HUGE, True), ("minimum", 0, "-" + HUGE, False), ("maximum", 10, HUGE, False),
    ("maximum", 1e308, HUGE, False), ("exclusiveMinimum", 0, "1e-99999999999999999999", True),
    ("exclusiveMaximum", 1e-300, "1e-99999999999999999999", True), ("minimum", 1e-300, "1e-99999999999999999999", False),
    ("multipleOf", 2, HUGE, True), ("multipleOf", 3, HUGE, False), ("multipleOf", 0.5, "1.5e-99999999999999999999", False),
    ("multipleOf", 5, "5e99999999999999999999", True), ("type", "integer", HUGE, True),
    ("type", "integer", "1.5e-99999999999999999999", False), ("type", "integer", "0e-99999999999999999999", True),
])
def test_jschema_numbers_beyond_decimal(keyword: str, bound: object, tok: str, valid: bool) -> None:
    c = compile_schema({keyword: bound})
    v = codec.loads(tok)
    assert c.is_valid(v) is valid
    assert (not c.errors(v)) is valid


def test_jschema_bounds_beyond_decimal() -> None:
    c = compile_schema({"maximum": codec.loads(HUGE), "minimum": codec.loads("-" + HUGE)})
    assert c.is_valid(5) and c.is_valid(codec.loads("1e999999999999999999"))
    assert not c.is_valid(codec.loads("2e99999999999999999999"))
    c = compile_schema({"exclusiveMinimum": codec.loads(HUGE)})
    assert not c.is_valid(codec.loads(HUGE)) and c.is_valid(codec.loads("1.1e99999999999999999999"))
    c = compile_schema({"multipleOf": codec.loads("1e-99999999999999999999")})
    assert c.is_valid(7) and c.is_valid(codec.loads("3e-99999999999999999999"))
    # counts: an absurd maxLength is "no limit", minLength nothing reaches
    assert compile_schema({"maxLength": codec.loads(HUGE)}).is_valid("abc")
    assert not compile_schema({"minItems": codec.loads("1e999999999999999999")}).is_valid([1, 2])
    with pytest.raises(UserInputError):
        compile_schema({"minLength": codec.loads("-" + HUGE)})


def test_validate_document_with_numbers_beyond_decimal(make_doc) -> None:
    doc = make_doc(('[{"n": %s}, {"n": 5}, {"n": 2.5}]' % HUGE).encode())
    for sch, invalid in (({"items": {"properties": {"n": {"minimum": 0}}}}, 0),
                         ({"items": {"properties": {"n": {"type": "integer"}}}}, 1),
                         ({"items": {"properties": {"n": {"multipleOf": 2}}}}, 2),
                         ({"items": {"properties": {"n": {"maximum": codec.loads(HUGE)}}}}, 0)):
        rep = validate(doc, sch, scope="document")
        assert rep.invalid == invalid, sch
        rep.close()


# ==================================================================================
# R5A-4: whole-document fallback
# ==================================================================================
def test_whole_document_fallback_counts_every_record(make_doc, monkeypatch) -> None:
    n = 3000
    doc = make_doc(("[" + ",".join("%d" % i for i in range(n)) + "]").encode())
    sch = {"type": "array", "items": {"type": "string"}, "not": {"maxItems": 0}}   # `not`: not decomposable
    monkeypatch.setattr(js, "WHOLE_DOCUMENT_ERRORS", 1000)                      # the memory guard
    seen: list[int] = []
    rep = validate(doc, sch, scope="document", max_issues=None, on_issues=lambda b: seen.extend(i.record for i in b))
    assert rep.invalid == n and rep.records.to_list() == list(range(n))         # every invalid record
    assert rep.issue_count == 1000 and len(seen) == 1000                         # the issues kept
    assert "jschema.warn.whole_dropped" in rep.warnings
    i = rep.warnings.index("jschema.warn.whole_dropped")
    assert rep.warning_params[i] == {"count": n - 1000, "kept": 1000}
    rep.close()
    monkeypatch.setattr(js, "WHOLE_DOCUMENT_ERRORS", 500_000)
    rep = validate(doc, sch, scope="document", max_issues=10)
    assert rep.invalid == n and rep.issue_count == n and "jschema.warn.whole_dropped" not in rep.warnings
    rep.close()


def test_whole_document_fallback_keeps_max_per_record(make_doc) -> None:
    doc = make_doc(b'[[1, 2, 3, 4, 5], ["a"], [6, 7]]')
    sch = {"type": "array", "items": {"type": "array", "items": {"type": "string"}}, "not": {"maxItems": 0}}
    rep = validate(doc, sch, scope="document", max_per_record=3, max_issues=None)
    assert [(i.record, i.path) for i in rep.issues] == [(0, (0,)), (0, (1,)), (0, (2,)), (2, (0,)), (2, (1,))]
    assert rep.invalid == 2 and rep.limited_records == 1
    rep.close()


# ==================================================================================
# R5A-8: repeated positions
# ==================================================================================
def test_repeated_positions_are_validated_once(make_doc) -> None:
    doc = make_doc(b'{"a": 1}\n{"a": "x"}\n{"a": "y"}\n', "r.jsonl")
    sch = {"properties": {"a": {"type": "integer"}}}
    rep = validate(doc, sch, source=[1, 1, 2, 1, 0, 2])
    assert (rep.scanned, rep.issue_count, rep.invalid) == (3, 2, 2)
    assert [i.record for i in rep.issues] == [1, 2] and rep.records.to_list() == [1, 2]
    rep.close()
    rep = validate(doc, sch, source=range(2, -1, -1))         # descending: no repeats, scope order
    assert [i.record for i in rep.issues] == [2, 1] and rep.scanned == 3
    rep.close()


# ==================================================================================
# R5A-9: ECMA-262 regular expressions
# ==================================================================================
@pytest.mark.parametrize("pattern", ["(?i)abc", "a*+", "a++", "a?+", "a{1,2}+", "(?>a)", "(?P<n>a)", "(?P=n)",
                                     "(?#c)x", "(?(1)a|b)", "(?s:a)", "a*?+"])
def test_python_only_regex_syntax_is_refused(pattern: str) -> None:
    with pytest.raises(ValueError):
        ecma_regex(pattern)
    with pytest.raises(UserInputError) as ei:
        compile_schema({"pattern": pattern})
    assert ei.value.key == "jschema.error.schema.regex"


@pytest.mark.parametrize("pattern, yes, no", [
    ("^a{,2}$", ["a{,2}"], ["aa", "a", ""]),           # Annex B: no quantifier, the characters
    ("^a{2}$", ["aa"], ["a", "aaa"]), ("^a{2,}$", ["aaa"], ["a"]), ("^a{1,2}?b$", ["ab", "aab"], ["b"]),
    ("^a*?b$", ["b", "aab"], ["c"]), ("^a??b$", ["b", "ab"], ["aab"]), ("^(?:ab)+$", ["abab"], ["aba"]),
    ("^(?=a)\\w+$", ["ab"], ["ba"]), ("^(?!a)\\w+$", ["ba"], ["ab"]), ("(?<=a)b", ["ab"], ["cb"]),
    ("(?<!a)b", ["cb"], ["ab"]), ("^(?<x>a)\\k<x>$", ["aa"], ["ab"]), ("^{}$", ["{}"], ["x"]),
    ("^a{$", ["a{"], ["a"]), ("^}$", ["}"], ["{"]),
])
def test_ecma_regex_semantics(pattern: str, yes: list, no: list) -> None:
    c = compile_schema({"pattern": pattern})
    for s in yes:
        assert c.is_valid(s), (pattern, s)
    for s in no:
        assert not c.is_valid(s), (pattern, s)


# ==================================================================================
# observation: ok vs complete
# ==================================================================================
def test_report_complete_flags_skipped_records(make_doc, monkeypatch) -> None:
    doc = make_doc(b'[{"a": 1}, {"a": 2}, {"a": 3}]')
    rep = validate(doc, {"type": "object"})
    assert rep.ok and rep.complete
    rep.close()
    monkeypatch.setattr(js, "VALIDATE_PARSE_BYTES", 5)                # every record is "too large"
    rep = validate(doc, {"type": "object"})
    assert rep.ok and not rep.complete and rep.skipped == 3 and rep.too_large == 3
    rep.close()


# ==================================================================================
# R5A-12: images
# ==================================================================================
def b64(data: bytes) -> str:
    return base64.b64encode(data).decode()


def jpeg(w: int, h: int, app: bytes = b"") -> bytes:
    sof = b"\xff\xc0" + struct.pack(">HBHHB", 11, 8, h, w, 1) + b"\x01\x11\x00"
    return b"\xff\xd8" + app + b"\xff\xe0" + struct.pack(">H", 16) + b"JFIF\0" + b"\0" * 9 + sof + b"\xff\xda" + b"\0" * 12


@pytest.mark.parametrize("data", [
    b"\x89PNG\r\n\x1a\n",                                            # the signature only
    b"\x89PNG\r\n\x1a\n" + struct.pack(">I", 13) + b"IHDR" + b"\0" * 8,   # size 0 x 0
    b"\x89PNG\r\n\x1a\n" + b"\0\0\0\x0dIHDX" + b"\0" * 9,           # no IHDR
    b"GIF89a\x01", b"GIF89a\x10\x00\x10\x00",                      # screen descriptor cut
    b"\xff\xd8\xff\xe0\x00\x00", b"\xff\xd8\xff\xe0\x00\x01" + b"\0" * 10, b"\xff\xd8" + b"\xff" * 6000,
    b"\xff\xd8\xff\xe1\xff\xff" + b"\0" * 100, b"\xff\xd8\xff\xc0\x00\x11\x08\x00", b"\xff\xd8" + b"\xff\xd0" * 3000,
    b"\xff\xd8\xff\xda\x00\x0c" + b"\0" * 20, b"\xff\xd8\xff\xd9",
    b"RIFF\0\0\0\0WEBPVP8L\0\0\0\0\x2f", b"RIFF\0\0\0\0WEBPVP8 \0\0\0\0\0\0\0\x9d\x01\x2a",
])
def test_no_readable_size_is_no_image(data: bytes) -> None:
    for text in (b64(data), "data:image/png;base64," + b64(data)):
        sv = detect(text)
        assert sv is None or sv.kind != "image", data[:20]
        assert image_bytes(text) is None


def test_images_with_a_size_and_big_jpeg_metadata() -> None:
    sv = detect(b64(jpeg(30, 20)))
    assert sv.kind == "image" and (sv.image.width, sv.image.height) == (30, 20)
    # metadata segments beyond the 64 KiB read: a JPEG of unknown size (its header is sound so far)
    app = b"".join(b"\xff\xe2" + struct.pack(">H", 65000) + b"\0" * 64998 for _ in range(2))
    big = jpeg(640, 480, app)
    sv = detect(b64(big))
    assert sv.kind == "image" and sv.image.format == "JPEG" and sv.image.width is None
    assert image_bytes(b64(big)) == big
    # the same metadata in an image that ends before its frame header: detect() reads 64 KiB (the
    # header is sound so far), but image_bytes() checks every segment and gives no thumbnail
    cut = big[:2 + len(app)] + b"\xff\xd9"
    assert image_bytes(b64(cut)) is None
