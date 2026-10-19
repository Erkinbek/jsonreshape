"""jsonreshape.jschema: the validator (docs/ARCHITECTURE.md §14.3; design: stage-5 map_stats.md §8).

* exactness probes (map_stats §8.3) and exact numbers (big integers, ``1e400``, ``-0``, ``1.10``,
  ``0.10000000000000000001``, a 5000-digit integer, huge exponents);
* ECMA-262 regular expressions (``\\d``/``\\w`` ASCII, the ECMA ``\\s`` set, ``$``, ``.``, ``\\cX``,
  ``\\u{...}``, surrogate pairs, named groups, ``\\p{...}``, classes with ``\\S``) incl. Uzbek text;
* formats off / on; schema value checks with localized messages; refusals (remote ``$ref``,
  ``$dynamicRef``, other dialects, custom vocabularies, endless ``$ref`` cycles) and the
  guarantee that no network module is ever imported (subprocess);
* differential test against the ``jsonschema`` package (dev-only oracle) on hypothesis-generated
  schema / instance pairs without float edge cases;
* documents: record numbers (0-based), member keys and paths of the issues for every kind and
  both core backends, the RecordSet of invalid records, counts, top paths, limits and warnings,
  sources (RecordSets, sorted views, stale scopes), edited (pinned) documents, the document
  scope decomposition per kind (array-like, keyed, wrapped with array / object containers,
  JSON Lines with an array schema), the whole-document fallback and its refusal, the huge-record
  policy, too deep records, cancellation, progress and file changes.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from pathlib import Path

import pytest

import jsongen
import overlay_helpers as oh
from jsonreshape import codec, reader
from jsonreshape import jschema as js
from jsonreshape.errors import Cancelled, EditError, FileChangedError, UserInputError
from jsonreshape.i18n import set_language
from jsonreshape.jschema import Issue, compile_schema, ecma_regex, iter_issues, validate
from jsonreshape.reader import Document
from jsonreshape.recordset import RecordSet, sort_records
from jsonreshape.settings import Settings
from jsonreshape.tasks import CancelToken, Progress

ROOT = Path(__file__).resolve().parents[1]


# ----------------------------------------------------------------------------------
# helpers
# ----------------------------------------------------------------------------------
def S(text: str) -> object:
    """A schema / instance from JSON text (exact numbers)."""
    return codec.loads(text)


def ok(schema: object, instance_text: str, *, formats: bool = False) -> bool:
    cs = compile_schema(schema, formats=formats)
    value = codec.loads(instance_text)
    valid = cs.is_valid(value)
    assert valid == (not cs.errors(value)), "is_valid and errors() disagree"
    return valid


@pytest.fixture
def backend(core_backend, monkeypatch):
    """Documents use this core backend's Scanner; codec.loads its plain-number check."""
    name = "rust" if core_backend.__name__.endswith("_rscore") else "python"
    old = reader.set_scanner_backend(name)
    monkeypatch.setattr(codec, "_fast_checker", core_backend.numbers_plain if name == "rust" else False)
    yield name
    reader.set_scanner_backend(old)


@pytest.fixture
def make_doc(tmp_path: Path):
    docs: list[Document] = []
    counter = [0]

    def make(records: list, fmt: str = "array", *, style: str = "compact", keys: list[str] | None = None,
             settings: Settings | None = None, raw: bytes | None = None, **write_kw) -> Document:
        counter[0] += 1
        path = tmp_path / f"doc{counter[0]}.json"
        if raw is not None:
            path.write_bytes(raw)
        else:
            jsongen.write_file(path, records, fmt, style=style, keys=keys, **write_kw)
        kw: dict = {"format": fmt}
        if fmt == "wrapped":
            kw["record_path"] = "data"
        doc = Document.open(path, settings=settings, **kw)
        doc.build_index()
        docs.append(doc)
        return doc

    yield make
    for d in docs:
        d.close()


def issue_keys(issues) -> list[tuple]:
    return [(i.record, i.key, i.path, i.keyword) for i in issues]


PERSON = S('{"type": "object", "properties": {"id": {"type": "integer"}, "name": {"type": "string"},'
           ' "tags": {"type": "array", "items": {"type": "string"}}}, "required": ["id", "name"]}')
PEOPLE = [{"id": 0, "name": "Ali"}, {"id": "x", "name": "Vali"}, {"id": 2}, {"id": 3, "name": 5, "tags": [1, "t"]},
          {"id": 4, "name": "Oʻtkir", "tags": ["ok"]}]
EXPECTED_PEOPLE = [(1, ("id",), "type"), (2, (), "required"), (3, ("name",), "type"), (3, ("tags", 0), "type")]


# ----------------------------------------------------------------------------------
# exact numbers (map_stats §8.3 probes and more)
# ----------------------------------------------------------------------------------
@pytest.mark.parametrize("schema, token, valid", [
    ('{"multipleOf": 0.01}', "0.07", True),
    ('{"multipleOf": 0.1}', "0.3", True),
    ('{"multipleOf": 0.1}', "0.35", False),
    ('{"multipleOf": 0.01}', "1.10", True),
    ('{"type": "integer"}', "1.0", True),
    ('{"type": "integer"}', "1e400", True),
    ('{"type": "integer"}', "1.5e400", True),
    ('{"type": "integer"}', "1e-400", False),
    ('{"type": "integer"}', "-0.0", True),
    ('{"type": "integer"}', "-0", True),
    ('{"type": "integer"}', "12345678901234567890.000", True),
    ('{"maximum": 1e308}', "1e400", False),
    ('{"minimum": 1e308}', "1e400", True),
    ('{"exclusiveMinimum": 0.1}', "0.10000000000000000001", True),
    ('{"exclusiveMaximum": 0.1}', "0.10000000000000000001", False),
    ('{"minimum": 0.1}', "0.09999999999999999999", False),
    ('{"maximum": 0.1}', "0.1", True),
    ('{"exclusiveMaximum": 0.1}', "0.1", False),
    ('{"const": 12345678901234567890}', "12345678901234567890.0", True),
    ('{"const": 12345678901234567890}', "12345678901234567891", False),
    ('{"enum": [1]}', "true", False),
    ('{"enum": [true]}', "1", False),
    ('{"enum": [null]}', "0", False),
    ('{"const": 0}', "-0", True),
    ('{"const": 0}', "-0.0", True),
    ('{"const": 1.1}', "1.10", True),
    ('{"const": 1E5}', "100000", True),
    ('{"const": {"a": 1, "b": [1.0, "x"]}}', '{"b": [1, "x"], "a": 1.0}', True),
    ('{"uniqueItems": true}', "[1, 1.0]", False),
    ('{"uniqueItems": true}', "[1, true]", True),
    ('{"uniqueItems": true}', '[0, false, null, "0", []]', True),
    ('{"uniqueItems": true}', '[{"a": 1, "b": 2}, {"b": 2, "a": 1}]', False),
    ('{"minimum": 18446744073709551615}', "18446744073709551616", True),
    ('{"maximum": 9007199254740992}', "9007199254740993", False),
    ('{"maximum": 9007199254740993}', "9007199254740992.0", True),
    ('{"type": "integer", "multipleOf": 0.5}', "1e308", True),
    ('{"multipleOf": 1e-400}', "1e-399", True),
    ('{"multipleOf": 0.0001}', "1e999999999", True),
    ('{"multipleOf": 3}', "1e999999999", False),
    ('{"multipleOf": 7}', "-0", True),
    ('{"minimum": -1e400}', "-1e401", False),
])
def test_exact_numbers(schema: str, token: str, valid: bool, backend) -> None:
    assert ok(S(schema), token) is valid


def test_number_types_from_python_and_decimal() -> None:
    from decimal import Decimal

    cs = compile_schema({"minimum": 0.1, "multipleOf": 0.05, "type": "number"})
    assert cs.is_valid(0.15) and cs.is_valid(Decimal("0.15")) and cs.is_valid(1)
    assert not cs.is_valid(0.12) and not cs.is_valid(Decimal("0.0999999999999999999999"))
    assert not cs.is_valid(True) and not cs.is_valid("0.2") and not cs.is_valid(float("nan"))
    big = 3 * 10 ** 5000
    assert compile_schema({"multipleOf": 3}).is_valid(big)
    for delta in range(8):
        assert compile_schema({"multipleOf": 7}).is_valid(big + delta) is ((big + delta) % 7 == 0)
    # coefficients longer than the Decimal context precision (28 digits) stay exact
    long_token = "1" * 40 + "e5"
    assert compile_schema({"multipleOf": 10 ** 5}).is_valid(codec.loads(long_token))
    assert compile_schema({"multipleOf": 7}).is_valid(codec.loads(long_token)) is (int("1" * 40) % 7 == 0)
    assert compile_schema({"multipleOf": int("1" * 40)}).is_valid(codec.loads("1" * 40 + "0.0"))
    assert compile_schema({"type": "integer"}).is_valid(codec.loads("1" + "0" * 5000))   # JsonFloat raw text
    assert compile_schema({"maximum": 10 ** 5000}).is_valid(codec.loads("1" + "0" * 5000))
    assert not compile_schema({"exclusiveMaximum": 10 ** 5000}).is_valid(codec.loads("1" + "0" * 5000))


def test_error_parameters_keep_exact_number_texts(lang_en) -> None:
    cs = compile_schema(S('{"maximum": 1e308, "multipleOf": 0.01}'))
    issues = cs.errors(codec.loads("1e400"))
    assert [i.keyword for i in issues] == ["maximum"]
    assert issues[0].params == {"value": "1e400", "limit": "1e308"}
    assert "1e400" in issues[0].message and "1e308" in issues[0].message
    issue = compile_schema(S('{"multipleOf": 0.01}')).errors(codec.loads("1.001"))[0]
    assert issue.params == {"value": "1.001", "divisor": "0.01"} and issue.schema_path == "/multipleOf"


# ----------------------------------------------------------------------------------
# ECMA-262 regular expressions
# ----------------------------------------------------------------------------------
@pytest.mark.parametrize("pattern, text, match", [
    ("^\\d+$", "123", True),
    ("^\\d+$", "١٢٣", False),          # Arabic-Indic digits (Python \d would match)
    ("^\\w+$", "é", False),
    ("^\\w+$", "abc_09", True),
    ("^\\s$", " ", True),
    ("^\\s$", "﻿", True),
    ("^\\s$", " ", True),
    ("^\\s$", " ", True),
    ("^\\s$", "\u0001", False),
    ("^\\S$", " ", False),
    ("^\\S$", "x", True),
    ("^[\\S]$", "a", True),
    ("^[\\S]$", " ", False),
    ("^[^\\S]$", "\t", True),
    ("^[^\\S]$", "a", False),
    ("^[a\\s]$", " ", True),
    ("^[a\\s]$", "b", False),
    ("^[^a\\s]$", "b", True),
    ("^[^a\\s]$", "a", False),
    ("^[^a\\s]$", " ", False),
    ("^abc$", "abc\n", False),
    ("^abc$", "abc", True),
    ("^.$", "\n", False),
    ("^.$", "\r", False),
    ("^.$", " ", False),
    ("^.$", "😀", True),
    ("^\\cJ$", "\n", True),
    ("^\\cj$", "\n", True),
    ("^\\u{1F600}$", "😀", True),
    ("^\\ud83d\\ude00$", "😀", True),
    ("^\\x41\\u0042$", "AB", True),
    ("^\\0$", "\x00", True),
    ("^(?<y>\\d{4})-\\k<y>$", "2024-2024", True),
    ("^(?<y>\\d{4})-\\k<y>$", "2024-2025", False),
    ("^[]$", "", False),
    ("^[^]$", "\n", True),
    ("^\\p{L}+$", "Oʻzbekiston", True),             # U+02BB is a modifier letter (Lm)
    ("^\\p{L}+$", "O'zbekiston", False),            # ASCII apostrophe is punctuation
    ("^\\p{Letter}+$", "Тошкент", True),
    ("^[\\p{Lu}\\p{Ll}]+$", "Ўзбекистон", True),
    ("^\\P{L}+$", "12-3", True),
    ("^[\\P{L}]+$", "a", False),
    ("^\\p{digit}+$", "৪২", True),
    ("\\bfoo\\b", "a foo b", True),
    ("\\bfoo\\b", "éfooé", True),                      # \b is ASCII-based
    ("^[\\d\\-]+$", "1-2", True),
    ("^[a-z&&]+$", "a&", True),
    ("^a{2}$", "aa", True),
    ("^\\/\\.$", "/.", True),
])
def test_ecma_regex(pattern: str, text: str, match: bool) -> None:
    assert (re.compile(ecma_regex(pattern), re.ASCII).search(text) is not None) is match
    # the same through the schema keyword
    assert compile_schema({"pattern": pattern}).is_valid(text) is match


@pytest.mark.parametrize("pattern", ["\\a", "\\q", "(", "[a", "\\cÉ", "\\u12", "a\\", "\\p{Script=Latin}", "\\k<n"])
def test_bad_regex_is_a_localized_schema_error(pattern: str, lang_uz) -> None:
    with pytest.raises(UserInputError) as ei:
        compile_schema({"properties": {"a": {"pattern": pattern}}})
    assert ei.value.key == "jschema.error.schema.regex"
    assert ei.value.params["location"] == "/properties/a/pattern"
    assert "Muntazam ifoda" in str(ei.value)


def test_pattern_properties_with_uzbek_keys() -> None:
    cs = compile_schema({"patternProperties": {"^[\\p{L}ʻ' ]+$": {"type": "string"}}, "additionalProperties": False})
    assert cs.is_valid({"Oʻzbekiston": "ha", "Тошкент": "ha", "kalit bo'shliq": "x"})
    assert not cs.is_valid({"Oʻzbekiston": 1})
    assert not cs.is_valid({"a1": "x"})
    issues = cs.errors({"a1": "x"})
    assert [(i.path, i.keyword, i.params) for i in issues] == [(("a1",), "additionalProperties", {"property": "a1"})]


# ----------------------------------------------------------------------------------
# formats
# ----------------------------------------------------------------------------------
@pytest.mark.parametrize("fmt, good, bad", [
    ("date-time", "2024-02-29T23:59:60Z", "2023-02-29T10:00:00Z"),
    ("date", "2020-02-29", "2021-02-29"),
    ("time", "15:59:60-08:00", "15:59:60+00:00"),
    ("email", "ali.valiyev@misol.uz", "ali..valiyev@misol.uz"),
    ("uri", "https://misol.uz/yo'l?q=1#a", "misol.uz/yo'l"),
    ("uuid", "2eb8aa08-AA98-11ea-B4Aa-73B441D16380", "2eb8aa08aa9811eab4aa73b441d16380"),
    ("ipv4", "192.168.0.1", "192.168.0.01"),
    ("ipv6", "::ffff:192.168.0.1", "fe80::a%eth1"),
])
def test_formats_are_annotations_unless_asserted(fmt: str, good: str, bad: str) -> None:
    schema = {"type": "string", "format": fmt}
    assert compile_schema(schema).is_valid(bad)                         # annotation only
    asserted = compile_schema(schema, formats=True)
    assert asserted.is_valid(good) and not asserted.is_valid(bad)
    assert compile_schema({"format": fmt}, formats=True).is_valid(12)   # formats apply to strings only
    issue = asserted.errors(bad)[0]
    assert issue.keyword == "format" and issue.params == {"format": fmt}
    assert compile_schema(schema).with_formats(True).is_valid(bad) is False
    assert compile_schema({"format": "duration"}, formats=True).is_valid("not a duration")   # unknown: annotation


def test_format_option_of_validate(make_doc) -> None:
    doc = make_doc([{"e": "a@b.uz"}, {"e": "not-an-email"}])
    schema = {"properties": {"e": {"format": "email"}}}
    assert validate(doc, schema).ok
    rep = validate(doc, schema, formats=True)
    assert issue_keys(rep.issues) == [(1, None, ("e",), "format")] and rep.formats is True


# ----------------------------------------------------------------------------------
# schema checks and refusals
# ----------------------------------------------------------------------------------
@pytest.mark.parametrize("schema, keyword, location", [
    ({"minimum": "1"}, "minimum", "/minimum"),
    ({"properties": {"a": {"maxLength": -1}}}, "maxLength", "/properties/a/maxLength"),
    ({"minItems": 1.5}, "minItems", "/minItems"),
    ({"required": ["a", "a"]}, "required", "/required"),
    ({"required": "a"}, "required", "/required"),
    ({"type": "int"}, "type", "/type"),
    ({"type": ["string", "string"]}, "type", "/type"),
    ({"multipleOf": 0}, "multipleOf", "/multipleOf"),
    ({"multipleOf": -2}, "multipleOf", "/multipleOf"),
    ({"items": [{}]}, "items", "/items"),
    ({"properties": {"a": 1}}, "properties", "/properties"),
    ({"allOf": []}, "allOf", "/allOf"),
    ({"anyOf": {}}, "anyOf", "/anyOf"),
    ({"uniqueItems": "yes"}, "uniqueItems", "/uniqueItems"),
    ({"$defs": {"x": {"$anchor": "1x"}}}, "$anchor", "/$defs/x"),
    ({"dependentRequired": {"a": [1]}}, "dependentRequired", "/dependentRequired"),
    ({"enum": "a"}, "enum", "/enum"),
    ({"format": 5}, "format", "/format"),
    ({"$ref": 5}, "$ref", "/$ref"),
])
def test_schema_values_are_checked(schema: dict, keyword: str, location: str, lang_uz) -> None:
    with pytest.raises(UserInputError) as ei:
        compile_schema(schema)
    err = ei.value
    assert err.key == "jschema.error.schema.keyword"
    assert err.params["keyword"] == keyword and err.params["location"].startswith(location)
    assert "noto'g'ri" in err.message and err.hint
    set_language("en")
    assert "invalid" in err.message


@pytest.mark.parametrize("schema, key", [
    ({"$ref": "https://example.invalid/other.json"}, "jschema.error.schema.remote_ref"),
    ({"$ref": "other.json#/a"}, "jschema.error.schema.remote_ref"),
    ({"$id": "urn:x:y", "$ref": "z.json"}, "jschema.error.schema.remote_ref"),
    ({"$ref": "#/$defs/missing"}, "jschema.error.schema.unresolved_ref"),
    ({"$ref": "#nowhere"}, "jschema.error.schema.unresolved_ref"),
    ({"$dynamicRef": "#meta"}, "jschema.error.schema.dynamic"),
    ({"$defs": {"a": {"$dynamicAnchor": "x"}}}, "jschema.error.schema.dynamic"),
    ({"$schema": "http://json-schema.org/draft-07/schema#"}, "jschema.error.schema.dialect"),
    ({"$defs": {"a": {"$id": "a.json", "$schema": "https://json-schema.org/draft/2019-09/schema"}}},
     "jschema.error.schema.dialect"),
    ({"$vocabulary": {"https://example.invalid/vocab/mine": True}}, "jschema.error.schema.vocabulary"),
    ({"$ref": "#"}, "jschema.error.schema.ref_cycle"),
    ({"$defs": {"a": {"allOf": [{"$ref": "#/$defs/b"}]}, "b": {"anyOf": [{"$ref": "#/$defs/a"}]}},
      "$ref": "#/$defs/a"}, "jschema.error.schema.ref_cycle"),
    ({"$id": "http://x.test/a.json#frag"}, "jschema.error.schema.id"),
    ({"properties": {"a": 5}}, "jschema.error.schema.keyword"),
])
def test_unsupported_schemas_are_refused(schema: dict, key: str, lang_en) -> None:
    with pytest.raises(UserInputError) as ei:
        compile_schema(schema)
    assert ei.value.key == key
    assert str(ei.value) and "{" not in str(ei.value)       # every placeholder filled


def test_refusal_messages_are_localized(lang_uz) -> None:
    with pytest.raises(UserInputError) as ei:
        compile_schema({"properties": {"a": {"$ref": "https://example.invalid/s.json"}}})
    err = ei.value
    assert "Tashqi havola" in err.message and "/properties/a/$ref" in err.message
    assert "$defs" in err.hint
    with pytest.raises(UserInputError) as ei2:
        compile_schema({"$schema": "http://json-schema.org/draft-07/schema#"})
    assert "2020-12" in ei2.value.message and ei2.value.hint


def test_accepted_schema_shapes() -> None:
    assert compile_schema(True).is_valid({"a": 1}) and not compile_schema(False).is_valid(None)
    assert compile_schema({"$schema": "https://json-schema.org/draft/2020-12/schema#"}).is_valid(1)
    assert compile_schema('{"type": "integer", "maximum": 1e400}').is_valid(codec.loads("1e399"))
    with pytest.raises(UserInputError) as ei:
        compile_schema("{not json")
    assert ei.value.key == "jschema.error.schema.json"
    with pytest.raises(UserInputError):
        compile_schema([{"type": "string"}])
    recursive = compile_schema({"properties": {"children": {"items": {"$ref": "#"}}}, "required": ["name"]})
    assert recursive.is_valid({"name": "a", "children": [{"name": "b", "children": []}]})
    assert not recursive.is_valid({"name": "a", "children": [{"children": []}]})
    assert {"$vocabulary": {"https://json-schema.org/draft/2020-12/vocab/core": True}} is not None
    compile_schema({"$vocabulary": {"https://json-schema.org/draft/2020-12/vocab/core": True,
                                    "https://example.invalid/optional": False}})


def test_local_resources_resolve_offline() -> None:
    other = {"$id": "http://x.test/types.json", "$defs": {"n": {"type": "integer", "minimum": 0}}}
    cs = compile_schema({"$ref": "http://x.test/types.json#/$defs/n"}, resources={"http://x.test/types.json": other})
    assert cs.is_valid(3) and not cs.is_valid(-1)
    issue = cs.errors(-1)[0]
    assert issue.schema_path == "http://x.test/types.json#/$defs/n/minimum"


def test_no_network_module_is_ever_imported() -> None:
    code = (
        "import sys, json, tempfile, os\n"
        "os.environ['JSONRESHAPE_HOME'] = tempfile.mkdtemp()\n"
        "from jsonreshape.jschema import compile_schema, validate, generate_schema\n"
        "from jsonreshape.errors import UserInputError\n"
        "from jsonreshape.reader import Document\n"
        "from jsonreshape.schema import infer_schema\n"
        "for ref in ('https://example.com/s.json', 'http://localhost:1234/x.json', 'other.json'):\n"
        "    try:\n"
        "        compile_schema({'$ref': ref})\n"
        "    except UserInputError:\n"
        "        pass\n"
        "p = os.path.join(tempfile.mkdtemp(), 'd.json')\n"
        "open(p, 'w').write('[{\"u\": \"https://example.com\", \"e\": \"a@b.uz\"}]')\n"
        "d = Document.open(p); d.build_index()\n"
        "s = generate_schema(infer_schema(d), doc=d, strict=True)\n"
        "r = validate(d, s, formats=True); r.close(); d.close()\n"
        "bad = sorted(m for m in sys.modules if m.split('.')[0] in "
        "('socket', 'ssl', 'requests', 'urllib3', 'httpx', '_socket', '_ssl') or "
        "m in ('urllib.request', 'http.client'))\n"
        "print(json.dumps(bad))\n"
    )
    environ = dict(os.environ, JSONRESHAPE_BACKEND="python")
    out = subprocess.run([sys.executable, "-c", code], cwd=str(ROOT), env=environ, capture_output=True, text=True,
                         timeout=120, check=True)
    assert json.loads(out.stdout.strip().splitlines()[-1]) == []
    source = (ROOT / "jsonreshape" / "jschema.py").read_text(encoding="utf-8")
    assert "import jsonschema" not in source and "from jsonschema" not in source


# ----------------------------------------------------------------------------------
# errors: paths, keywords, messages
# ----------------------------------------------------------------------------------
def test_error_details_and_messages(lang_uz) -> None:
    cs = compile_schema(PERSON)
    issues = cs.errors({"id": 1.5, "tags": ["a", 2]})
    assert [(i.path, i.keyword, i.schema_path) for i in issues] == [
        ((), "required", "/required"),
        (("id",), "type", "/properties/id/type"),
        (("tags", 1), "type", "/properties/tags/items/type"),
    ]
    assert issues[0].params == {"property": "name"} and issues[0].message == 'Majburiy maydon yo\'q: "name".'
    assert issues[1].message == "Turi noto'g'ri: butun son kutilgan, son berilgan."
    assert issues[2].path_text == "tags[1]" and issues[0].path_text == "$"
    set_language("en")
    assert issues[1].message == "Wrong type: expected integer, got number."
    assert issues[0].to_dict() == {"record": None, "key": None, "path": "$", "keyword": "required",
                                   "schema_path": "/required", "message": 'A required property is missing: "name".',
                                   "params": {"property": "name"}}


@pytest.mark.parametrize("schema, instance, keyword", [
    ({"enum": ["Toshkent", "Samarqand"]}, '"Тошкент"', "enum"),
    ({"const": "O'zbekiston"}, '"Oʻzbekiston"', "const"),
    ({"minLength": 3}, '"ab"', "minLength"),
    ({"maxLength": 2}, '"😀😀😀"', "maxLength"),
    ({"minItems": 2}, "[1]", "minItems"),
    ({"maxItems": 0}, "[1]", "maxItems"),
    ({"uniqueItems": True}, "[1, 2, 1.0]", "uniqueItems"),
    ({"prefixItems": [False]}, "[1]", "prefixItems"),
    ({"items": False}, "[1]", "items"),
    ({"contains": {"type": "string"}}, "[1]", "contains"),
    ({"contains": {"type": "string"}, "maxContains": 1}, '["a", "b"]', "maxContains"),
    ({"minProperties": 1}, "{}", "minProperties"),
    ({"maxProperties": 0}, '{"a": 1}', "maxProperties"),
    ({"dependentRequired": {"a": ["b"]}}, '{"a": 1}', "dependentRequired"),
    ({"dependentSchemas": {"a": False}}, '{"a": 1}', "dependentSchemas"),
    ({"properties": {"a": False}}, '{"a": 1}', "properties"),
    ({"patternProperties": {"^a": False}}, '{"ab": 1}', "patternProperties"),
    ({"additionalProperties": False}, '{"x": 1}', "additionalProperties"),
    ({"unevaluatedProperties": False}, '{"x": 1}', "unevaluatedProperties"),
    ({"unevaluatedItems": False}, "[1]", "unevaluatedItems"),
    ({"propertyNames": {"maxLength": 1}}, '{"ab": 1}', "propertyNames"),
    ({"not": {}}, "1", "not"),
    ({"anyOf": [{"type": "string"}, {"type": "boolean"}]}, "1", "anyOf"),
    ({"oneOf": [{"type": "integer"}, {"minimum": 0}]}, "1", "oneOf"),
    ({"exclusiveMinimum": 1}, "1", "exclusiveMinimum"),
    (False, "1", "false"),
])
def test_every_keyword_has_a_localized_message(schema, instance, keyword, lang_uz) -> None:
    issues = compile_schema(schema).errors(codec.loads(instance))
    assert [i.keyword for i in issues] == [keyword]
    for lang in ("uz", "en"):
        set_language(lang)
        text = issues[0].message
        assert text and "{" not in text and not text.startswith("jschema."), (lang, text)


def test_every_message_key_exists() -> None:
    from jsonreshape.i18n import has

    keys = set(js._MESSAGE_KEYS.values()) | set(js._DOC_KEYS.values()) | set(js._TYPE_KEYS.values())
    source = (ROOT / "jsonreshape" / "jschema.py").read_text(encoding="utf-8")
    keys |= set(re.findall(r'"(jschema\.[a-zA-Z_.]+[a-zA-Z_])"', source))
    missing = sorted(k for k in keys if not has(k))
    assert not missing, missing


# ----------------------------------------------------------------------------------
# documents: record numbers, keys, paths
# ----------------------------------------------------------------------------------
@pytest.mark.parametrize("fmt", ["array", "jsonl", "brace_list", "stream", "wrapped", "keyed"])
def test_issue_record_numbers_keys_and_paths(fmt: str, make_doc, backend) -> None:
    keys = [f"k{i}" for i in range(len(PEOPLE))] if fmt == "keyed" else None
    doc = make_doc(PEOPLE, fmt, style="pretty2", keys=keys)
    rep = validate(doc, PERSON, scope="record")
    expected = [(r, keys[r] if keys else None, p, kw) for r, p, kw in EXPECTED_PEOPLE]
    assert issue_keys(rep.issues) == expected
    assert rep.scope == "record" and rep.scanned == len(PEOPLE) and rep.invalid == 3 and rep.issue_count == 4
    assert rep.records.to_list() == [1, 2, 3] and rep.records.gen == doc.gen and rep.records.kind == "invalid"
    assert rep.counts == {"type": 3, "required": 1}
    assert dict(rep.top_paths) == {"$": 1, "id": 1, "name": 1, "tags[*]": 1}
    assert not rep.ok and not rep.warnings
    assert issue_keys(iter_issues(doc, PERSON, scope="record")) == expected
    rep.close()


def test_single_document_is_one_record(make_doc) -> None:
    doc = make_doc(PEOPLE, "single")
    rep = validate(doc, {"type": "array", "items": PERSON})
    assert rep.scope == "record" and rep.scanned == 1
    assert issue_keys(rep.issues) == [(0, None, (r,) + p, kw) for r, p, kw in EXPECTED_PEOPLE]
    rep.close()


def test_encodings_and_compression(tmp_path: Path) -> None:
    for enc, bom, gz in (("utf-8", True, False), ("utf-16-le", True, False), ("utf-8", False, True),
                         ("utf-16-be", True, True)):
        path = tmp_path / f"d_{enc}_{bom}_{gz}.json"
        jsongen.write_file(path, PEOPLE, "array", style="pretty4", newline="\r\n", encoding=enc, bom=bom, gz=gz)
        with Document.open(path) as doc:
            doc.build_index()
            rep = validate(doc, PERSON)
            assert [(i.record, i.path, i.keyword) for i in rep.issues] == EXPECTED_PEOPLE, (enc, bom, gz)
            rep.close()


def test_limits_truncation_and_warnings(make_doc, lang_en) -> None:
    doc = make_doc(PEOPLE)
    rep = validate(doc, PERSON, max_per_record=1, max_issues=2)
    assert [(i.record, i.keyword) for i in rep.issues] == [(1, "type"), (2, "required")]
    assert rep.issue_count == 3 and rep.truncated and rep.limited_records == 3
    assert rep.warnings == ("jschema.warn.per_record", "jschema.warn.issues_truncated")
    texts = rep.warning_texts()
    assert texts[0] == "3 records have many issues: only the first 1 per record are listed."
    assert texts[1] == "Only the first 2 issues were kept (of 3)."
    rep.close()
    rep = validate(doc, PERSON, max_per_record=None, max_issues=None)
    assert rep.issue_count == 4 and len(rep.issues) == 4 and not rep.warnings
    rep.close()
    rep = validate(doc, PERSON, max_issues=0)
    assert rep.issues == [] and rep.issue_count == 4 and rep.truncated and rep.invalid == 3
    rep.close()
    for bad in ({"max_per_record": 0}, {"max_issues": -1}, {"scope": "all"}, {"max_per_record": True}):
        with pytest.raises(UserInputError):
            validate(doc, PERSON, **bad)


def test_sources_sorted_views_and_stale_scopes(make_doc) -> None:
    doc = make_doc(PEOPLE)
    rep = validate(doc, PERSON, source=[3, 0, 1])
    assert [(i.record, i.keyword) for i in rep.issues] == [(3, "type"), (3, "type"), (1, "type")]
    assert rep.records.to_list() == [3, 1] and rep.scanned == 3
    rep.close()
    view = sort_records(doc, ["-id"])
    order = view.to_list()
    rep = validate(doc, PERSON, source=view)
    per_record = {1: 1, 2: 1, 3: 2}
    assert [i.record for i in rep.issues] == [r for r in order for _ in range(per_record.get(r, 0))]
    assert rep.records.to_list() == [r for r in order if r in per_record]     # scope order
    rep.close()
    view.close()
    # a source naming a record twice: validated once, like stats and dupes examine each position
    # once (review R5A-8; it was validated twice: its issues and the scanned count doubled)
    rep = validate(doc, PERSON, source=[3, 3, 1, 0, 3])
    assert [i.record for i in rep.issues] == [3, 3, 1]
    assert rep.records.to_list() == [3, 1] and rep.invalid == 2 and rep.scanned == 3 and rep.issue_count == 3
    rep.close()
    # arguments are checked when iter_issues is called, before any iteration
    with pytest.raises(UserInputError):
        iter_issues(doc, PERSON, scope="everything")
    with pytest.raises(UserInputError):
        iter_issues(doc, {"$ref": "https://example.invalid/x.json"})
    stale = RecordSet.from_indices([0, 1], gen=doc.gen + 12345)
    with pytest.raises(EditError) as ei:
        validate(doc, PERSON, source=stale)
    assert ei.value.key == "edits.error.stale_scope"
    base = RecordSet.base(doc)
    rep = validate(doc, PERSON, source=base)
    assert rep.scanned == len(PEOPLE)
    rep.close()


@pytest.mark.parametrize("kind", ["array", "jsonl", "keyed", "wrapped", "wrapped_obj", "stream"])
@pytest.mark.parametrize("scenario", ["point", "structure", "mixed", "keys"])
def test_edited_documents_validate_like_their_content(kind: str, scenario: str, tmp_path: Path) -> None:
    doc, ob, exp = oh.make_edited(tmp_path, kind, scenario, n=20, seed=5)
    try:
        schema = {"type": "object", "properties": {"id": {"type": "integer", "maximum": 1005},
                                                   "name": {"type": "string", "maxLength": 12}},
                  "required": ["active"]}
        got = validate(doc, schema, scope="record")
        want = validate(exp, schema, scope="record")
        assert issue_keys(got.issues) == issue_keys(want.issues)
        assert got.records.to_list() == want.records.to_list() and got.gen == doc.gen
        assert got.scanned == want.scanned == len(ob.records)
        got.close()
        want.close()
    finally:
        doc.close()
        exp.close()


# ----------------------------------------------------------------------------------
# document scope
# ----------------------------------------------------------------------------------
RECS = [{"id": i, "name": f"r{i}"} for i in range(6)]


def test_array_document_decomposition(make_doc, lang_en) -> None:
    doc = make_doc(RECS + [{"id": 1, "name": "r1"}])
    schema = {"type": "array",
              "prefixItems": [{"properties": {"id": {"const": 0}}}, {"required": ["name"]}],
              "items": {"properties": {"id": {"maximum": 4}}},
              "minItems": 2, "maxItems": 5, "uniqueItems": True,
              "contains": {"properties": {"name": {"const": "r9"}}}}
    rep = validate(doc, schema)
    assert rep.scope == "document"
    got = [(i.record, i.path, i.keyword) for i in rep.issues]
    assert got == [(5, ("id",), "maximum"), (6, (), "uniqueItems"), (None, (), "maxItems"), (None, (), "contains")]
    msgs = [i.message for i in rep.issues]
    assert msgs[1] == "This record is equal to record #1 (the records must be unique)."
    assert msgs[2] == "The document has 7 records, at most 5 allowed."
    assert msgs[3] == "0 records match the condition, at least 1 required."
    assert rep.document_issues == 2 and rep.invalid == 2 and rep.records.to_list() == [5, 6]
    rep.close()
    # a filtered scope: container keywords are not checked (warning)
    rep = validate(doc, schema, source=[5, 6])
    assert [(i.record, i.keyword) for i in rep.issues] == [(5, "maximum")]
    assert rep.warnings == ("jschema.warn.aggregates_skipped",)
    rep.close()


def test_unevaluated_items_and_contains_in_document_scope(make_doc) -> None:
    doc = make_doc([{"t": "a"}, {"t": "b"}, {"t": "c"}, 5])
    schema = {"prefixItems": [{"type": "object"}], "contains": {"type": "object", "properties": {"t": {"const": "c"}}},
              "unevaluatedItems": {"type": "object", "properties": {"t": {"enum": ["b"]}}}}
    rep = validate(doc, schema, scope="document")
    assert [(i.record, i.path, i.keyword) for i in rep.issues] == [(3, (), "type")]
    rep.close()
    rep = validate(doc, {"type": "array", "prefixItems": [True], "unevaluatedItems": False})
    assert [(i.record, i.keyword) for i in rep.issues] == [(1, "unevaluatedItems"), (2, "unevaluatedItems"),
                                                           (3, "unevaluatedItems")]
    rep.close()


def test_keyed_document_decomposition(make_doc, lang_en) -> None:
    recs = [{"x": 1}, {"y": 2}, {"x": 3}, {"x": 4}, 7]
    keys = ["k0", "k1", "k2", "k2", "m"]
    doc = make_doc(recs, "keyed", keys=keys)
    schema = {"type": "object", "properties": {"k1": {"required": ["x"]}},
              "patternProperties": {"^k": {"type": "object"}}, "additionalProperties": False,
              "propertyNames": {"pattern": "^k"}, "required": ["k0", "zz"], "dependentRequired": {"k1": ["k9"]},
              "minProperties": 2, "maxProperties": 3}
    rep = validate(doc, schema, scope="document")   # auto: "properties"/"required" read as a record schema
    assert rep.scope == "document"
    got = [(i.record, i.key, i.path, i.keyword) for i in rep.issues]
    assert got == [(1, "k1", (), "required"), (4, "m", (), "propertyNames"), (4, "m", (), "additionalProperties"),
                   (None, None, (), "required"), (None, None, (), "dependentRequired"),
                   (None, None, (), "maxProperties")]
    assert rep.issues[-1].message == "The document has 4 distinct keys, at most 3 allowed."
    assert rep.issues[3].message == 'A record with a required key is missing: "zz".'
    rep.close()


@pytest.mark.parametrize("member", [False, True])
def test_wrapped_document_decomposition(member: bool, make_doc, tmp_path: Path, lang_en) -> None:
    if member:
        kw = oh.write_source(tmp_path / "w.json", "wrapped_obj", n=4)
        doc = Document.open(tmp_path / "w.json", **kw)
        doc.build_index()
        container = {"type": "object", "additionalProperties": {"required": ["nope"]}, "minProperties": 10}
    else:
        doc = make_doc(RECS, "wrapped")
        container = {"type": "array", "items": {"required": ["nope"]}, "minItems": 10}
    try:
        schema = {"type": "object",
                  "properties": {"meta": {"type": "string"}, "data": container},
                  "required": ["meta", "data", "extra"]}
        rep = validate(doc, schema)
        assert rep.scope == "document"
        n = doc.record_count
        doc_issues = [(i.path, i.keyword) for i in rep.issues if i.record is None]
        kw = "minProperties" if member else "minItems"
        assert doc_issues == [((), "required"), (("meta",), "type"), (("data",), kw)]
        rec_issues = [(i.record, i.key, i.path, i.keyword) for i in rep.issues if i.record is not None]
        assert rec_issues == [(r, doc.record_key(r) if member else None, (), "required") for r in range(n)]
        assert [i for i in rep.issues if i.record is None][2].message.startswith("The document has ")
        rep.close()
        # the same schema in record scope applies the root to every record
        rep = validate(doc, schema, scope="record")
        assert rep.scope == "record" and rep.invalid == n
        rep.close()
    finally:
        doc.close()


def test_jsonl_with_an_array_schema_is_a_sequence(make_doc) -> None:
    doc = make_doc(RECS, "jsonl")
    rep = validate(doc, {"type": "array", "prefixItems": [{"properties": {"id": {"const": 99}}}], "maxItems": 3})
    assert rep.scope == "document"
    assert [(i.record, i.keyword) for i in rep.issues] == [(0, "const"), (None, "maxItems")]
    rep.close()
    rep = validate(doc, {"type": "object", "required": ["id"]})       # each line is a document
    assert rep.scope == "record" and rep.ok
    rep.close()
    rep = validate(doc, {"type": "object", "required": ["id"]}, scope="document")
    assert rep.scope == "document" and rep.ok
    rep.close()


def test_auto_scope_decisions(make_doc) -> None:
    arr = make_doc(RECS)
    keyed = make_doc(RECS, "keyed")
    wrapped = make_doc(RECS, "wrapped")
    cases = [
        (arr, {"type": "object"}, "record"),
        (arr, {"items": {"type": "object"}}, "document"),
        (arr, {"type": ["array", "null"]}, "document"),
        (arr, {"$comment": js.SCOPE_RECORD, "type": "array"}, "record"),
        (arr, {"$comment": js.SCOPE_DOCUMENT}, "document"),
        (arr, {"$defs": {"d": {"type": "array"}}, "$ref": "#/$defs/d"}, "document"),
        (keyed, {"type": "object", "properties": {"id": {}}}, "record"),
        (keyed, {"type": "object", "additionalProperties": {"type": "object"}}, "document"),
        (wrapped, {"properties": {"data": {"items": {}}}}, "document"),
        (wrapped, {"properties": {"id": {}}}, "record"),
    ]
    for doc, schema, scope in cases:
        rep = validate(doc, schema)
        assert rep.scope == scope, (doc.kind, schema)
        rep.close()


def test_whole_document_fallback_and_its_limit(make_doc, monkeypatch, lang_en) -> None:
    doc = make_doc(RECS)
    schema = {"type": "array", "anyOf": [{"maxItems": 1}, {"items": {"type": "integer"}}],
              "items": {"properties": {"id": {"maximum": 3}}}}
    rep = validate(doc, schema)
    assert rep.scope == "document"
    assert [(i.record, i.path, i.keyword) for i in rep.issues] == [
        (None, (), "anyOf"), (4, ("id",), "maximum"), (5, ("id",), "maximum")]
    assert rep.warnings == ("jschema.warn.whole_document",)
    assert "anyOf" in rep.warning_texts()[0]
    rep.close()
    keyed = make_doc(RECS, "keyed", keys=[f"k{i}" for i in range(6)])
    rep = validate(keyed, {"type": "object", "not": {"required": ["k3"]}}, scope="document")
    assert [(i.record, i.keyword) for i in rep.issues] == [(None, "not")]
    rep.close()
    monkeypatch.setattr(js, "DOCUMENT_PARSE_BYTES", 10)
    with pytest.raises(UserInputError) as ei:
        validate(doc, schema)
    assert ei.value.key == "jschema.error.document_too_large" and ei.value.params["keyword"] == "anyOf"
    assert ei.value.hint and "record" in ei.value.hint


# ----------------------------------------------------------------------------------
# big, broken and deep records; cancellation; progress; file changes
# ----------------------------------------------------------------------------------
def test_records_over_the_parse_limit_are_skipped_and_counted(make_doc, monkeypatch, lang_en) -> None:
    recs = [{"id": i, "pad": "x" * (10 if i % 2 else 400)} for i in range(6)]
    doc = make_doc(recs)
    monkeypatch.setattr(js, "VALIDATE_PARSE_BYTES", 200)
    rep = validate(doc, {"properties": {"id": {"maximum": 2}}})
    assert rep.scanned == 3 and rep.too_large == 3 and rep.skipped == 3
    assert [i.record for i in rep.issues] == [3, 5]
    assert rep.warnings == ("jschema.warn.too_large",)
    assert rep.warning_texts()[0] == "3 records are too large (limit 200 B) and were not checked; the first one: #0."
    rep.close()


def test_huge_records_are_skipped(make_doc) -> None:
    settings = Settings()
    settings.large_node_bytes = 100
    recs = [{"id": i, "pad": "y" * (5 if i != 2 else 300)} for i in range(4)]
    doc = make_doc(recs, settings=settings)
    rep = validate(doc, {"required": ["id"]})
    assert rep.scanned == 3 and rep.too_large == 1 and rep.ok is True
    assert rep.warning_params[0]["record"] == 2
    rep.close()


def test_too_deep_records_are_skipped(make_doc) -> None:
    deep = b"[" * 600 + b"1" + b"]" * 600
    raw = b"[[1]," + deep + b",[2]]"
    doc = make_doc([], raw=raw)
    # "too deep" is what the interpreter's recursion limit allows; the limit is pinned to the default
    # for the test's duration (the mapping engine raised it for good before its scoped
    # engine._RecursionRoom; anything else changing it must not make this test order-dependent)
    old = sys.getrecursionlimit()
    sys.setrecursionlimit(1000)
    try:
        rep = validate(doc, {"items": {"$ref": "#"}}, scope="record")
    finally:
        sys.setrecursionlimit(old)
    assert rep.scanned == 2 and rep.too_deep == 1 and rep.ok
    assert not rep.complete and rep.skipped == 1      # ok, but not every record was validated
    assert rep.warnings == ("jschema.warn.too_deep",)
    rep.close()


def test_cancellation_progress_and_file_changes(make_doc, tmp_path: Path) -> None:
    recs = jsongen.records(300, seed=2)
    doc = make_doc(recs, "jsonl")
    token = CancelToken()
    token.cancel()
    with pytest.raises(Cancelled):
        validate(doc, {"type": "object"}, cancel=token)
    seen: list[int] = []
    progress = Progress(callback=lambda info: seen.append(info.done), min_interval=0)
    rep = validate(doc, {"type": "object"}, progress=progress)
    rep.close()
    assert seen == sorted(seen) and progress.finished and progress.done == progress.total
    assert progress.total == doc.data_size - doc.info.data_start
    # the file changes while it is open: nothing is reported
    path = Path(doc.source_path)
    data = path.read_bytes()
    path.write_bytes(data.replace(b'"id":1,', b'"id":7,', 1))
    with pytest.raises(FileChangedError):
        validate(doc, {"type": "object"})


def test_unreadable_records_are_counted(make_doc, monkeypatch) -> None:
    doc = make_doc(RECS)
    real = codec.loads

    def flaky(data):
        if b'"r2"' in bytes(data):
            raise codec.JsonDecodeError("simulated")
        return real(data)

    monkeypatch.setattr(codec, "loads", flaky)
    rep = validate(doc, {"type": "object"})
    assert rep.unreadable == 1 and rep.scanned == 5 and rep.warning_params[0]["record"] == 2
    rep.close()


# ----------------------------------------------------------------------------------
# throughput on a 200 MB file
# ----------------------------------------------------------------------------------
@pytest.mark.slow
@pytest.mark.parametrize("scope", ["record", "document"])
def test_throughput_on_a_200mb_file(scope: str, tmp_path: Path, capsys) -> None:
    import time

    sys.path.insert(0, str(ROOT / "bench"))
    try:
        import make_synthetic
    finally:
        sys.path.remove(str(ROOT / "bench"))
    from jsonreshape.jschema import generate_schema
    from jsonreshape.schema import infer_schema

    path = tmp_path / "big.json"
    info = make_synthetic.generate(str(path), size=200 << 20, records=None, fmt="array", seed=7, quiet=True)
    with Document.open(path) as doc:
        doc.build_index()
        schema = generate_schema(infer_schema(doc, sample=None), doc=doc, document=scope == "document")
        for _ in doc.iter_blocks(None):     # warm the OS cache like a second run of the user would
            pass
        started = time.perf_counter()
        rep = validate(doc, schema, scope=scope)
        elapsed = time.perf_counter() - started
        rate = doc.data_size / 1e6 / elapsed
        with capsys.disabled():
            print(f"\n[jschema] validate {doc.data_size / 1e6:.0f} MB, {info['records']} records, {scope} scope: "
                  f"{elapsed:.2f} s = {rate:.1f} MB/s ({elapsed * 1e6 / rep.scanned:.2f} us/record)")
        assert rep.ok and rep.scanned == info["records"] and rep.scope == scope
        rep.close()
    assert rate > 10, f"{rate:.1f} MB/s"


# ----------------------------------------------------------------------------------
# differential test against the jsonschema package (dev-only oracle)
# ----------------------------------------------------------------------------------
jsonschema = pytest.importorskip("jsonschema")
from hypothesis import HealthCheck, given, settings as hsettings, strategies as st  # noqa: E402

_scalars = st.one_of(st.none(), st.booleans(), st.integers(-50, 50), st.sampled_from([0.5, -1.5, 2.25, 10.0]),
                     st.sampled_from(["", "a", "ab", "abc", "Toshkent", "Тошкент", "Oʻzbekiston", "12", "x-1"]))
_values = st.recursive(_scalars, lambda inner: st.one_of(
    st.lists(inner, max_size=4),
    st.dictionaries(st.sampled_from(["a", "b", "c", "id", "ism"]), inner, max_size=4)), max_leaves=12)
_type_names = st.sampled_from(["object", "array", "string", "number", "integer", "boolean", "null"])
# patterns whose ECMA-262 and Python ``re`` meanings agree on the strings above (``\w``, ``\s`` and
# non-ASCII ``\d`` differ on purpose: the ECMA tests above and the official suite cover them)
_patterns = st.sampled_from(["^a", "b$", "^[a-c]+$", "\\d", "^[A-Za-z]*$", "^.{2}$", "Т", "^x-\\d$", "ʻ"])


def _schemas(depth: int = 0) -> st.SearchStrategy:
    leaf = st.one_of(
        st.booleans(),
        st.fixed_dictionaries({"type": st.one_of(_type_names, st.lists(_type_names, min_size=1, max_size=3,
                                                                        unique=True))}),
        st.fixed_dictionaries({"minimum": st.integers(-5, 5)}),
        st.fixed_dictionaries({"exclusiveMaximum": st.sampled_from([0, 2, 2.5, 10])}),
        st.fixed_dictionaries({"multipleOf": st.sampled_from([1, 2, 3, 5])}),
        st.fixed_dictionaries({"minLength": st.integers(0, 3)}),
        st.fixed_dictionaries({"maxLength": st.integers(0, 3)}),
        st.fixed_dictionaries({"pattern": _patterns}),
        st.fixed_dictionaries({"enum": st.lists(_scalars, min_size=1, max_size=3)}),
        st.fixed_dictionaries({"const": _values}),
        st.fixed_dictionaries({"minItems": st.integers(0, 3)}),
        st.fixed_dictionaries({"maxProperties": st.integers(0, 3)}),
        st.fixed_dictionaries({"uniqueItems": st.booleans()}),
        st.fixed_dictionaries({"required": st.lists(st.sampled_from(["a", "b", "id"]), max_size=2, unique=True)}),
        st.fixed_dictionaries({"dependentRequired": st.dictionaries(st.sampled_from(["a", "b"]),
                                                                    st.lists(st.sampled_from(["c", "id"]),
                                                                             max_size=2, unique=True),
                                                                    max_size=2)}),
    )
    if depth >= 2:
        return leaf
    sub = st.deferred(lambda: _schemas(depth + 1))
    return st.one_of(
        leaf,
        st.fixed_dictionaries({"properties": st.dictionaries(st.sampled_from(["a", "b", "id"]), sub, max_size=2)},
                              optional={"additionalProperties": sub, "required": st.lists(
                                  st.sampled_from(["a", "b"]), max_size=2, unique=True)}),
        st.fixed_dictionaries({"patternProperties": st.dictionaries(_patterns, sub, max_size=2)},
                              optional={"unevaluatedProperties": sub}),
        st.fixed_dictionaries({"items": sub}, optional={"prefixItems": st.lists(sub, min_size=1, max_size=2)}),
        st.fixed_dictionaries({"prefixItems": st.lists(sub, min_size=1, max_size=2)},
                              optional={"unevaluatedItems": sub}),
        st.fixed_dictionaries({"contains": sub}, optional={"minContains": st.integers(0, 2),
                                                           "maxContains": st.integers(0, 2)}),
        st.fixed_dictionaries({"allOf": st.lists(sub, min_size=1, max_size=2)}),
        st.fixed_dictionaries({"anyOf": st.lists(sub, min_size=1, max_size=2)}),
        st.fixed_dictionaries({"oneOf": st.lists(sub, min_size=1, max_size=2)}),
        st.fixed_dictionaries({"not": sub}),
        st.fixed_dictionaries({"if": sub}, optional={"then": sub, "else": sub}),
        st.fixed_dictionaries({"propertyNames": sub}),
        st.fixed_dictionaries({"dependentSchemas": st.dictionaries(st.sampled_from(["a", "b"]), sub, max_size=1)}),
        st.fixed_dictionaries({"type": st.just("object"), "properties": st.dictionaries(
            st.sampled_from(["a", "b"]), sub, max_size=2)}, optional={"unevaluatedProperties": sub,
                                                                      "allOf": st.lists(sub, min_size=1, max_size=2)}),
    )


@hsettings(max_examples=400, deadline=None, suppress_health_check=[HealthCheck.too_slow])
@given(schema=_schemas(), instance=_values)
def test_differential_against_jsonschema(schema, instance) -> None:
    from jsonschema import Draft202012Validator
    from jsonschema_specifications import REGISTRY

    text = json.dumps(schema)
    inst_text = json.dumps(instance, ensure_ascii=False)
    expected = Draft202012Validator(json.loads(text), registry=REGISTRY).is_valid(json.loads(inst_text))
    mine = compile_schema(codec.loads(text))
    value = codec.loads(inst_text)
    assert mine.is_valid(value) is expected, (text, inst_text)
    assert bool(mine.errors(value)) is (not expected)


# ----------------------------------------------------------------------------------
# validate(on_issues=): issues handed over while the records are read (GUI streaming)
# ----------------------------------------------------------------------------------
N_INT = {"type": "object", "properties": {"n": {"type": "integer"}}}


def test_on_issues_hands_over_every_issue_in_report_order(make_doc, backend) -> None:
    doc = make_doc([{"id": i, "n": "x" if i % 3 == 0 else i} for i in range(700)])
    schema = compile_schema(N_INT)
    batches: list[list] = []
    rep = validate(doc, schema, max_issues=5, on_issues=batches.append)
    got = [i for b in batches for i in b]
    assert issue_keys(got) == issue_keys(iter_issues(doc, schema))       # also the ones beyond max_issues
    assert len(got) == rep.issue_count == 234 and rep.truncated and issue_keys(rep.issues) == issue_keys(got[:5])
    assert batches and all(0 < len(b) <= js.ISSUE_BATCH for b in batches)


def test_on_issues_batches_are_bounded(make_doc, backend) -> None:
    doc = make_doc([{"n": "x"} for _ in range(600)])
    batches: list[list] = []
    rep = validate(doc, compile_schema(N_INT), max_issues=None, on_issues=batches.append)
    assert [len(b) for b in batches][:2] == [js.ISSUE_BATCH, js.ISSUE_BATCH]
    assert sum(len(b) for b in batches) == rep.issue_count == 600 and max(len(b) for b in batches) <= js.ISSUE_BATCH


def test_on_issues_hands_over_a_waiting_batch_while_reading(make_doc, backend, monkeypatch) -> None:
    monkeypatch.setattr(js, "ISSUE_FLUSH_SECONDS", 0.0)
    recs = [{"n": 1} for _ in range(1100)]
    recs[0]["n"] = "x"
    recs[-1]["n"] = "y"
    doc = make_doc(recs)
    batches: list[list] = []
    validate(doc, compile_schema(N_INT), on_issues=batches.append)
    # the first issue is handed over at the next check point (512 records later), not with the last one
    assert [[i.record for i in b] for b in batches] == [[0], [1099]]


def test_on_issues_is_not_called_after_a_cancel(make_doc, backend, monkeypatch) -> None:
    monkeypatch.setattr(js, "ISSUE_FLUSH_SECONDS", 0.0)
    recs = [{"n": 1} for _ in range(1100)]
    recs[0]["n"] = "x"
    recs[-1]["n"] = "y"
    doc = make_doc(recs)
    token = CancelToken()
    batches: list[list] = []

    def take(items: list) -> None:
        batches.append(items)
        token.cancel()

    with pytest.raises(Cancelled):
        validate(doc, compile_schema(N_INT), on_issues=take, cancel=token)
    assert [[i.record for i in b] for b in batches] == [[0]]           # nothing after the cancel
