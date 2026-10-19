"""jsonreshape.jschema: JSON Schema generation (docs/ARCHITECTURE.md §14.3; map_stats.md §8.1).

* the rules per field (types in frequency order, ``integer`` from the schema's integer counter,
  nullable, ``properties`` order, ``required`` incl. under arrays, pooled maps, ``items``,
  ``examples``, the ``$key`` pseudo-field);
* the document-level shapes of every kind (array-like, JSON Lines, single, keyed, wrapped with an
  array or object container, a wrapper too big to parse) and the header (``$schema``, ``title``,
  ``description``, the ``$comment`` scope marker);
* strict mode: lengths, exact number ranges, item counts, ``additionalProperties: false`` (not for
  pooled maps or truncated schemas), enums from field statistics, formats confirmed over the
  whole document;
* the round trip "every document validates against its own generated schema" (full scan) for
  every kind, strict or not, document or record schema, both core backends, records walked by the
  schema builder, and the portability of the output (official meta-schema via the dev oracle);
* ``dump_schema`` / ``load_schema`` (exact numbers, BOM, UTF-16, errors).
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

import jsongen
import overlay_helpers as oh
from jsonreshape import codec, reader
from jsonreshape import jschema as js
from jsonreshape import schema as schema_mod
from jsonreshape.errors import Cancelled, JsonReshapeError, UserInputError
from jsonreshape.i18n import set_language
from jsonreshape.jschema import DIALECT, compile_schema, dump_schema, generate_schema, load_schema, validate
from jsonreshape.reader import Document
from jsonreshape.schema import SchemaBuilder, infer_schema
from jsonreshape.settings import Settings
from jsonreshape.tasks import CancelToken, Progress

KINDS = ("array", "jsonl", "brace_list", "wrapped", "wrapped_obj", "keyed", "single", "stream")


@pytest.fixture
def backend(core_backend, monkeypatch):
    name = "rust" if core_backend.__name__.endswith("_rscore") else "python"
    old = reader.set_scanner_backend(name)
    monkeypatch.setattr(codec, "_fast_checker", core_backend.numbers_plain if name == "rust" else False)
    yield name
    reader.set_scanner_backend(old)


@pytest.fixture
def opened(tmp_path: Path):
    docs: list[Document] = []
    counter = [0]

    def make(kind: str, records: list | None = None, *, n: int = 40, seed: int = 0, style: str = "pretty2",
             settings: Settings | None = None, keys: list[str] | None = None) -> Document:
        counter[0] += 1
        path = tmp_path / f"g{counter[0]}.json"
        if records is None:
            kw = oh.write_source(path, kind, n=n, seed=seed, style=style)
        else:
            fmt = "wrapped" if kind == "wrapped_obj" else kind
            jsongen.write_file(path, records, fmt, style=style, keys=keys)
            kw = {"format": fmt}
            if fmt == "wrapped":
                kw["record_path"] = "data"
        doc = Document.open(path, settings=settings, **kw)
        doc.build_index()
        docs.append(doc)
        return doc

    yield make
    for d in docs:
        d.close()


def built(records: list, **limits) -> schema_mod.Schema:
    b = SchemaBuilder(**limits)
    for r in records:
        b.add(r)
    return b.build()


# ----------------------------------------------------------------------------------
# rules per field
# ----------------------------------------------------------------------------------
def test_field_rules() -> None:
    recs = [
        {"id": 1, "name": "Ali", "score": 1.5, "n": 1, "flag": True, "note": None,
         "orders": [{"oid": 1, "sum": 2}, {"oid": 2}], "tags": ["a"]},
        {"id": 2, "name": None, "score": 2, "n": codec.loads("2.0"), "flag": False,
         "orders": [{"oid": 3, "sum": codec.loads("1e400")}], "tags": []},
        {"id": 3, "name": "Vali", "score": 3, "n": codec.loads("1e400"), "flag": 1,
         "orders": [], "extra": {"x": 1}},
    ]
    rec = generate_schema(built(recs), document=False)
    assert rec["$schema"] == DIALECT and rec["$comment"] == js.SCOPE_RECORD
    assert "title" not in rec
    assert list(rec["properties"]) == ["id", "name", "score", "n", "flag", "note", "orders", "tags", "extra"]
    props = rec["properties"]
    assert rec["type"] == "object"
    assert rec["required"] == ["id", "name", "score", "n", "flag", "orders"]
    assert props["id"]["type"] == "integer"
    assert props["score"]["type"] == "number"
    assert props["n"]["type"] == "integer"                    # 1, 2.0, 1e400: all integral
    assert props["name"]["type"] == ["string", "null"]
    assert props["flag"]["type"] == ["boolean", "integer"]
    assert props["note"]["type"] == "null"
    orders = props["orders"]
    assert orders["type"] == "array"
    assert orders["items"]["required"] == ["oid"]             # sum is missing in one order
    assert orders["items"]["properties"]["sum"]["type"] == "integer"
    assert props["tags"]["items"] == {"type": "string", "examples": ["a"]}
    assert props["name"]["examples"] == ["Ali", None, "Vali"]   # null is an example value too
    assert "minimum" not in props["score"] and "additionalProperties" not in rec   # permissive by default


def test_integer_needs_the_counter() -> None:
    sch = built([{"n": 1}, {"n": 2}])
    assert generate_schema(sch, document=False)["properties"]["n"]["type"] == "integer"
    # a schema read from a file written before stage 5 has no counter: "number"
    old = sch.to_dict()
    for f in old["fields"]:
        f.pop("integers", None)
    old["root"].pop("integers", None)
    again = schema_mod.Schema.from_dict(old)
    assert generate_schema(again, document=False)["properties"]["n"]["type"] == "number"
    assert generate_schema(built([{"n": codec.loads("1e-400")}]), document=False)["properties"]["n"]["type"] == "number"


def test_examples_are_complete_values_only() -> None:
    long = "x" * 100
    rec = generate_schema(built([{"s": long}, {"s": "short"}, {"s": "Тошкент"}, {"s": "a b"}]), document=False)
    assert rec["properties"]["s"]["examples"] == ["short", "Тошкент", "a b"]
    nums = generate_schema(built([{"v": codec.loads(t)} for t in ("1e400", "1.10", "12345678901234567890")]),
                           document=False)
    assert dump_schema(nums["properties"]["v"]["examples"]) == b"[1e400,1.10,12345678901234567890]\n" or \
        b"1e400" in dump_schema(nums)


def test_pooled_maps_and_truncated_schemas() -> None:
    recs = [{"m": {f"k{i}": i for i in range(6)}}]
    sch = built(recs, max_keys=3)
    rec = generate_schema(sch, document=False, strict=True)
    m = rec["properties"]["m"]
    assert list(m["properties"]) == ["k0", "k1", "k2"]
    assert m["additionalProperties"] == {"type": "integer", "examples": [3, 4, 5], "minimum": 3, "maximum": 5}
    assert rec["additionalProperties"] is False
    truncated = built([{"a": {"b": {"c": 1}}}], max_depth=2)
    assert truncated.truncated
    out = generate_schema(truncated, document=False, strict=True)
    assert "additionalProperties" not in out and "additionalProperties" not in out["properties"]["a"]


def test_member_key_is_not_a_property(opened) -> None:
    doc = opened("keyed", [{"a": 1}, {"a": 2}], keys=["x1", "x22"])
    sch = infer_schema(doc, sample=None)
    out = generate_schema(sch, doc=doc, strict=True)
    assert out["type"] == "object" and out["$comment"] == js.SCOPE_DOCUMENT
    assert "$key" not in out["additionalProperties"]["properties"]
    assert out["propertyNames"] == {"minLength": 2, "maxLength": 3}
    assert validate(doc, out).ok


# ----------------------------------------------------------------------------------
# document shapes and header
# ----------------------------------------------------------------------------------
def test_document_shapes(opened, lang_en) -> None:
    recs = [{"id": i} for i in range(3)]
    for kind in ("array", "brace_list", "stream"):
        doc = opened(kind, recs)
        out = generate_schema(infer_schema(doc, sample=None), doc=doc)
        assert out["type"] == "array" and out["items"]["properties"]["id"]["type"] == "integer"
        assert out["$comment"] == js.SCOPE_DOCUMENT and out["title"] == doc.source_path.name
        assert out["description"].startswith("Generated from all 3 records (")
    for kind in ("jsonl", "single"):
        doc = opened(kind, recs)
        out = generate_schema(infer_schema(doc, sample=None), doc=doc, title="Mening schemam")
        assert out["$comment"] == js.SCOPE_RECORD and out["title"] == "Mening schemam"
        assert out["type"] == ("object" if kind == "jsonl" else "array")
    doc = opened("wrapped", recs)
    out = generate_schema(infer_schema(doc, sample=None), doc=doc)
    assert out["type"] == "object" and out["required"] == ["meta", "data", "tail"]
    assert out["properties"]["data"] == {"type": "array", "items": out["properties"]["data"]["items"]}
    assert out["properties"]["meta"]["properties"]["version"]["type"] == "integer"
    doc = opened("wrapped_obj")
    out = generate_schema(infer_schema(doc, sample=None), doc=doc)
    assert out["properties"]["data"]["type"] == "object" and "additionalProperties" in out["properties"]["data"]
    assert generate_schema(infer_schema(doc, sample=None), doc=doc, document=False)["$comment"] == js.SCOPE_RECORD


def test_sample_description_is_localized(opened, lang_uz) -> None:
    doc = opened("array", n=30)
    sch = infer_schema(doc, sample=5)
    out = generate_schema(sch, doc=doc)
    assert "30 ta yozuvdan 5 tasi (namuna)" in out["description"]
    set_language("en")
    assert "a sample of 5 of 30 records" in generate_schema(sch, doc=doc)["description"]


def test_wrapper_too_big_gets_a_minimal_wrapper(opened, monkeypatch) -> None:
    doc = opened("wrapped", [{"id": 1}, {"id": 2}])
    monkeypatch.setattr(js, "VALIDATE_PARSE_BYTES", 10)
    out = generate_schema(infer_schema(doc, sample=None), doc=doc)
    assert out["properties"] == {"data": {"type": "array", "items": out["properties"]["data"]["items"]}}
    assert out["required"] == ["data"]


def test_wrapper_record_path_through_an_array(tmp_path: Path) -> None:
    path = tmp_path / "deep.json"
    path.write_bytes(b'{"pages": [{"rows": [{"a": 1}, {"a": 2}], "n": 1}, {"rows": [], "n": 2}], "v": "x"}')
    with Document.open(path, format="wrapped", record_path="pages.0.rows") as doc:
        doc.build_index()
        out = generate_schema(infer_schema(doc, sample=None), doc=doc, strict=True)
        pages = out["properties"]["pages"]
        assert pages["prefixItems"][0]["properties"]["rows"]["items"]["properties"]["a"]["type"] == "integer"
        rep = validate(doc, out)
        assert rep.scope == "document" and rep.ok, [i.message for i in rep.issues]
        rep.close()


# ----------------------------------------------------------------------------------
# strict mode
# ----------------------------------------------------------------------------------
def test_strict_constraints_keep_exact_numbers() -> None:
    recs = [{"v": codec.loads(t), "s": s, "a": a} for t, s, a in
            (("-1e400", "ab", []), ("12345678901234567890", "Oʻzbekiston", [1, 2, 3]), ("0.10000000000000000001", "x", [1]))]
    out = generate_schema(built(recs), document=False, strict=True)
    v = out["properties"]["v"]
    assert codec.number_text(v["minimum"]) == "-1e400" and codec.number_text(v["maximum"]) == "12345678901234567890"
    assert out["properties"]["s"]["minLength"] == 1 and out["properties"]["s"]["maxLength"] == 11
    assert (out["properties"]["a"]["minItems"], out["properties"]["a"]["maxItems"]) == (0, 3)
    assert out["additionalProperties"] is False
    text = dump_schema(out)
    assert b'"minimum": -1e400' in text and b'"maximum": 12345678901234567890' in text
    cs = compile_schema(codec.loads(text))
    for r in recs:
        assert cs.is_valid(r)
    assert not cs.is_valid({"v": codec.loads("12345678901234567891"), "s": "ab", "a": []})
    assert not cs.is_valid({"v": 1, "s": "ab", "a": [], "other": 1})


def _stat(field: str, values: list, counts: list[int], *, exact: bool = True, **extra) -> SimpleNamespace:
    return SimpleNamespace(field=field, distinct=len(values), distinct_exact=exact, top_exact=exact,
                           top=[SimpleNamespace(value=v, count=c) for v, c in zip(values, counts)],
                           values=sum(counts), skipped=0, uncounted=0, **extra)


def test_strict_enums_from_field_statistics() -> None:
    recs = [{"city": c, "n": n, "id": i, "o": {"k": 1}} for i, (c, n) in
            enumerate([("Toshkent", 1), ("Buxoro", 2), ("Toshkent", 1), (None, 2)])]
    sch = built(recs)
    stats = [
        _stat("city", ["Toshkent", "Buxoro", None], [2, 1, 1]),
        _stat("n", [1, 2], [2, 2]),
        _stat("id", [0, 1, 2, 3], [1, 1, 1, 1]),                       # no repetition: not categorical
        _stat("o.k", [1], [4], exact=False),                          # approximate: no enum
    ]
    out = generate_schema(sch, document=False, strict=True, stats=stats)
    props = out["properties"]
    assert props["city"]["enum"] == ["Toshkent", "Buxoro", None]
    assert props["n"]["enum"] == [1, 2]
    assert "enum" not in props["id"] and "enum" not in props["o"]["properties"]["k"]
    assert "enum" not in generate_schema(sch, document=False, strict=False, stats=stats)["properties"]["city"]
    cs = compile_schema(out)
    assert all(cs.is_valid(r) for r in recs)
    assert not cs.is_valid({"city": "Samarqand", "n": 1, "id": 9, "o": {"k": 1}})


def test_strict_formats_are_confirmed_over_the_whole_document(opened) -> None:
    recs = []
    for i in range(30):
        recs.append({"email": f"user{i}@misol.uz", "when": f"2024-01-{i % 28 + 1:02d}",
                     "site": f"https://misol.uz/{i}", "uid": f"2eb8aa08-aa98-11ea-b4aa-73b441d1{i:04d}",
                     "ip": f"10.0.0.{i}", "stamp": f"2024-01-01T10:00:{i % 60:02d}Z"})
    recs[25]["when"] = "2024-02-30"          # not one of the first examples: found by the confirmation pass
    recs[27]["ip"] = "10.0.0.256"
    doc = opened("array", recs)
    sch = infer_schema(doc, sample=None)
    out = generate_schema(sch, doc=doc, strict=True)
    items = out["items"]["properties"]
    assert items["email"]["format"] == "email" and items["site"]["format"] == "uri"
    assert items["uid"]["format"] == "uuid" and items["stamp"]["format"] == "date-time"
    assert "format" not in items["when"] and "format" not in items["ip"]
    rep = validate(doc, out, formats=True)
    assert rep.ok, [i.message for i in rep.issues]
    rep.close()
    assert all("format" not in p for p in generate_schema(sch, strict=True)["properties"].values())   # no document


def test_strict_generation_reports_progress_and_can_be_cancelled(opened) -> None:
    doc = opened("array", [{"e": f"a{i}@b.uz"} for i in range(50)])
    sch = infer_schema(doc, sample=None)
    progress = Progress(min_interval=0)
    generate_schema(sch, doc=doc, strict=True, progress=progress)
    assert progress.finished and progress.done == progress.total
    token = CancelToken()
    token.cancel()
    with pytest.raises(Cancelled):
        generate_schema(sch, doc=doc, strict=True, cancel=token)


# ----------------------------------------------------------------------------------
# the round trip: every document validates against its own generated schema
# ----------------------------------------------------------------------------------
@pytest.mark.parametrize("kind", KINDS)
@pytest.mark.parametrize("strict", [False, True])
def test_documents_validate_against_their_own_schema(kind: str, strict: bool, opened, backend) -> None:
    doc = opened(kind, n=60, seed=11, style="messy")
    sch = infer_schema(doc, sample=None)
    assert sch.complete
    for document in (True, False):
        generated = generate_schema(sch, doc=doc, strict=strict, document=document)
        back = codec.loads(dump_schema(generated))
        rep = validate(doc, back, formats=strict)
        assert rep.ok and rep.skipped == 0, (kind, document, [(i.record, i.path_text, i.message) for i in rep.issues])
        expect_scope = "document" if document and kind not in ("jsonl", "single") else "record"
        assert rep.scope == expect_scope
        assert rep.scanned == doc.record_count
        rep.close()


@pytest.mark.parametrize("kind", ["array", "keyed", "jsonl"])
def test_round_trip_with_random_values_and_walked_records(kind: str, opened, monkeypatch) -> None:
    """Records analysed by the schema builder's walker (no codec.loads of the whole record)
    must still validate: the integer counter and lengths agree with the validator."""
    monkeypatch.setattr(schema_mod, "STREAM_RECORD_BYTES", 300)
    recs = jsongen.records(80, seed=4, kind="value")
    recs = [r if isinstance(r, dict) else {"v": r} for r in recs]
    keys = [f"k{i}" for i in range(len(recs))] if kind == "keyed" else None
    doc = opened(kind, recs, keys=keys)
    sch = infer_schema(doc, sample=None)
    for strict in (False, True):
        rep = validate(doc, generate_schema(sch, doc=doc, strict=strict))
        assert rep.ok, [(i.record, i.path_text, i.message) for i in rep.issues]
        rep.close()


def test_edited_document_round_trip(tmp_path: Path) -> None:
    doc, ob, exp = oh.make_edited(tmp_path, "wrapped", "wrapper", n=24, seed=3)
    try:
        sch = infer_schema(doc, sample=None)
        out = generate_schema(sch, doc=doc, strict=True)
        rep = validate(doc, out)
        assert rep.ok and rep.scanned == len(ob.records), [i.message for i in rep.issues]
        assert rep.gen == doc.gen
        rep.close()
    finally:
        doc.close()
        exp.close()


def test_generated_schemas_are_portable(opened) -> None:
    jsonschema = pytest.importorskip("jsonschema")
    from jsonschema_specifications import REGISTRY

    import json

    doc = opened("array", [{"id": i, "name": f"n{i}", "tags": ["a", "b"][: i % 3], "ok": i % 2 == 0}
                           for i in range(20)])
    out = generate_schema(infer_schema(doc, sample=None), doc=doc, strict=True)
    plain = json.loads(dump_schema(out))
    jsonschema.Draft202012Validator.check_schema(plain)
    oracle = jsonschema.Draft202012Validator(plain, registry=REGISTRY)
    assert oracle.is_valid([doc.record(i) for i in range(doc.record_count)])


# ----------------------------------------------------------------------------------
# schema files
# ----------------------------------------------------------------------------------
def test_dump_and_load_schema_files(tmp_path: Path, lang_uz) -> None:
    value = {"title": "Oʻzbekiston", "maximum": codec.loads("1e400"), "const": codec.loads("1.10"),
             "enum": [codec.loads("12345678901234567890"), codec.loads("-0.0")]}
    text = dump_schema(value)
    assert text.endswith(b"\n") and "Oʻzbekiston".encode() in text
    assert b"1e400" in text and b"1.10" in text and b"12345678901234567890" in text and b"-0.0" in text
    for name, data in (("plain.json", text), ("bom.json", b"\xef\xbb\xbf" + text),
                       ("u16.json", text.decode("utf-8").encode("utf-16"))):
        path = tmp_path / name
        path.write_bytes(data)
        assert dump_schema(load_schema(path)) == text
    with pytest.raises(JsonReshapeError) as ei:
        load_schema(tmp_path / "missing.json")         # fileio's localized error names the file
    assert ei.value.key == "error.file_not_found" and "missing.json" in str(ei.value)
    bad = tmp_path / "bad.json"
    bad.write_bytes(b"{nope")
    with pytest.raises(UserInputError) as ei2:
        load_schema(bad)
    assert ei2.value.key == "jschema.error.schema_file" and "o'qib bo'lmadi" in str(ei2.value)
