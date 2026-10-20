# jsonreshape — Python API

jsonreshape is a command-line tool and a desktop application, and the same package is a library:
`pip install jsonreshape` (the GUI needs `pip install "jsonreshape[gui]"`), then `import jsonreshape`.
This document is the reference of the **public, stable** Python API. Every example below is executed by
`tests/test_api_docs.py`, in order, in one namespace, from an empty working directory — so the examples
are short and correct, and they build on each other (`data.json` from §1 is used everywhere).

What the library guarantees, whatever you call:

* **Streaming.** No function loads a whole file into memory. A `Document` keeps an on-disk index of the
  byte range of every record and reads records in 8 MiB blocks; filter, sort, search, export, statistics,
  mapping, split/merge, repair and diff all work in bounded memory (`Settings.memory_limit_bytes`,
  512 MiB by default; sorts spill to disk above `Settings.sort_memory_bytes`). A record larger than
  `Settings.large_node_bytes` (50 MiB) is *huge*: it is streamed where possible and skipped (counted in the
  report) where not; huge records are read-only.
* **Exact numbers.** JSON number text is never re-serialized through `float`: `1.10`, `1e400`,
  `12345678901234567890` and `-0` survive every export, edit and mapping unchanged (`jsonreshape.codec`, §16).
  Equality in filters, duplicates, statistics and diffs compares exact decimal values (`1 == 1.0 == 1e0`).
* **Inputs are never modified.** Every output goes to a temporary file in the target folder and is renamed
  over the target only when everything succeeded; on an error or a cancellation nothing is left behind and
  an existing target is untouched. An output path may never be the input (`UserInputError`), and a file
  that has unsaved edits in a running jsonreshape is never overwritten. A source file that another program
  changed while it was being read raises `FileChangedError` before any result is committed; a file that
  only *grew* (an active log) is read as the snapshot of open time.
* **Offline.** Nothing in the package touches the network.
* **Threads.** A `Document` may be read from several threads at once (every thread gets its own file
  handle); the index can be built in a background thread (`Document.start_index`) and a `RecordSet` can be
  read while it is still being filled. An `EditSession` serializes its commands with an internal lock.
  `Designer` (the mapping designer) and `Progress` callbacks are *not* thread-safe: callbacks run on the
  thread doing the work.
* **Processes.** CSV/XLSX exports, bulk operations and mapping application can use worker processes
  (`multiprocessing`, spawn) for inputs of at least 64 MiB. They are used automatically only when the
  caller's main module is safe to import again (`python -m ...`, an interactive interpreter, jsonreshape's
  own entry points). A plain script gets them by asking (`workers=`, `ApplyOptions(workers=...)`) and must
  guard its code with `if __name__ == "__main__":`.
* **Record numbers are 0-based** everywhere (`#0` is the first record, like `data[0]`).
* **Messages are localized.** Every error message, warning and report text comes from
  `jsonreshape.i18n` in Uzbek (the default) or English: `jsonreshape.set_language("en")` (§13).

## 1. Open a file and read records

`Document.open()` is cheap: it detects the format from the first 4 MiB and reuses a cached index when one
exists; `build_index()` scans the file once (or `start_index()` does it in a daemon thread and
`wait_index()` waits). Records are reachable while the index grows (`record_count` is live).

The examples need a file; this creates `data.json` — 200 records with nested objects and arrays:

```python
import json
import random
from pathlib import Path

from jsonreshape import Document, set_language

set_language("en")                    # messages and report texts in English (the default is Uzbek)

rng = random.Random(7)
cities = ["Toshkent", "Samarqand", "Buxoro", "Тошкент", None]
records = [
    {"id": i, "name": f"Name {i}", "active": i % 3 == 0, "score": round(rng.random() * 100, 2),
     "contact": {"city": rng.choice(cities), "phone": f"+99890{rng.randrange(10**6, 10**7)}"},
     "orders": [{"oid": i * 10 + j, "sum": rng.randrange(1, 500)} for j in range(rng.randrange(0, 3))]}
    for i in range(200)
]
Path("data.json").write_text(json.dumps(records, ensure_ascii=False, indent=2), encoding="utf-8")
```

```python
with Document.open("data.json") as doc:
    doc.build_index()                       # blocking; JSON errors land in doc.error, nothing is raised
    assert doc.info.kind == "array"         # array | jsonl | brace_list | wrapped | keyed | single | stream
    assert doc.record_count == 200 and doc.index_complete and doc.error is None

    first = doc.record(0)                   # the parsed value of record #0 (exact numbers, §16)
    assert first["id"] == 0
    raw = doc.record_bytes(0)               # its bytes, exactly as they are in the file
    start, end = doc.record_span(0)         # absolute byte offsets, end exclusive
    assert raw.startswith(b"{") and end - start == len(raw)

    for i, rec in doc.iter_records(range(0, 5)):      # (index, value); None = every record
        assert rec["id"] == i
    assert sum(1 for _ in doc.iter_records()) == 200

    node = doc.node("[3].contact.city")     # the lazy node tree: object / array / string / number / bool / null
    assert node.kind == "string" and node.value() == doc.record(3)["contact"]["city"]
    assert doc.root().kind == "array" and doc.root().child_count() == 200
```

Formats: `Document.open(path, record_path="data.items")` selects the record array of a wrapped document
(`{"meta": ..., "data": {"items": [...]}}`); `format="jsonl"` forces a kind. `.gz` files and UTF-16 files
are prepared into a UTF-8 copy in the cache first. For a *keyed* document (`{"id1": {...}, "id2": {...}}`)
every member is a record: `doc.record(i)` is the member value and `doc.record_key(i)` its key. A document
with a JSON syntax error keeps the records before the error usable (`doc.error` is a `JsonSyntaxError` with
line, column and context); JSON Lines files report broken lines in `doc.broken_lines`.

Huge records (`doc.is_huge(i)`) raise `TooLargeError` from `record()`/`record_bytes()`; stream them with
`jsonreshape.stream.stream_values` or `Node.child()`.

## 2. Search

`search()` finds every key and scalar value that matches a text query (plain, regex or number mode;
case-insensitive, apostrophe-tolerant; `translit=True` matches Latin and Cyrillic Uzbek as one text). The
hits of a run form a `RecordSet` (`result.records`); close the result when you are done.

```python
from jsonreshape.search import SearchOptions, iter_hits, search

with Document.open("data.json") as doc:
    result = search(doc, SearchOptions.build("toshkent"))
    assert result.hit_count == result.record_count > 0
    hit = result.hits[0]
    assert (hit.record, hit.path_text, hit.where, hit.text) == (1, "contact.city", "value", "Toshkent")
    assert hit.spans == ((0, 8),)           # the matched character spans inside hit.text
    result.close()

    latin_and_cyrillic = search(doc, SearchOptions.build("toshkent", translit=True))
    assert latin_and_cyrillic.hit_count > result.hit_count
    latin_and_cyrillic.close()

    opts = SearchOptions.build(r"^Name 1\d$", mode="regex", scope="values", path="name")
    assert sum(1 for _ in iter_hits(doc, opts)) == 10      # iter_hits streams every hit, bounded memory
```

`SearchOptions.build(text, mode="plain"|"regex"|"number", case_sensitive=False, whole_word=False,
translit=False, scope="both"|"keys"|"values", path=None)`; `search(doc, opts, source=None, max_hits=100000,
progress=None, cancel=None, on_hits=None)`; `export_hits(hits, "hits.csv")` writes hits as CSV or JSON.

## 3. Filter: the expression language and JMESPath

`compile_filter(text)` compiles a filter expression (the grammar is in `docs/ARCHITECTURE.md` §9: paths,
`== != < <= > >=`, `and or not`, `between`, `in`, `is null|empty|missing`, `contains startswith endswith
matches`, `len() exists() missing() lower() ...`; `$` is the record, `$key` the member key, `$index` the
record number). `compile_filter(text, lang="jmespath")` compiles a JMESPath expression. `filter_records()`
returns a `RecordSet` — an ordered, on-disk list of record numbers — with a `FilterReport`.

```python
from jsonreshape import compile_filter, filter_records

with Document.open("data.json") as doc:
    flt = compile_filter('active == true and contact.city == "Toshkent" and len(orders) > 0')
    matched = filter_records(doc, flt)
    assert len(matched) == matched.report.matched > 0
    for pos in matched:                     # record numbers in file order
        rec = doc.record(pos)
        assert rec["active"] and rec["contact"]["city"] == "Toshkent" and rec["orders"]
    matched.close()

    jm = compile_filter("contact.city == 'Toshkent' && score > `50`", lang="jmespath")
    with filter_records(doc, jm) as rs:     # a RecordSet is a context manager
        assert len(rs) == sum(1 for r in records if r["contact"]["city"] == "Toshkent" and r["score"] > 50)

    with filter_records(doc, "score between 10 and 20 or orders[*].sum >= 490") as rs:   # text is accepted too
        assert len(rs) > 0

    assert flt.matches({"active": True, "contact": {"city": "Toshkent"}, "orders": [1]})   # one value
```

A runtime error in one record (JMESPath on an unexpected type) is "no match" for that record and is counted
in `report.eval_errors`. `jsonreshape.query.FilterPresets` stores named filters in the app folder, like the
CLI `--save-preset` / `--preset`.

## 4. Sort to a RecordSet and export it

`sort_records()` is a stable external merge sort: typed (numbers < strings < booleans < arrays < objects),
exact numbers, natural digit runs, Uzbek collation by default, `null` and missing values first or last.
A `RecordSet` is accepted as the `source` of every other operation, so "filter, then sort, then export" is
three calls.

```python
from jsonreshape import ExportOptions, SortKey, export, sort_records

with Document.open("data.json") as doc:
    by_city = sort_records(doc, [SortKey("contact.city"), SortKey("score", descending=True)])
    assert len(by_city) == 200 and by_city.report.strategy == "memory"
    assert doc.record(by_city[0])["contact"]["city"] == "Buxoro"       # ... and null cities last
    assert doc.record(by_city[-1])["contact"]["city"] is None

    same = sort_records(doc, ["contact.city", "-score"])               # the CLI spelling of the keys
    assert same.to_list() == by_city.to_list()
    same.close()

    report = export(doc, "sorted.jsonl", ExportOptions("jsonl"), source=by_city)
    assert report.records == 200 and report.warnings == []
    by_city.close()
assert len(Path("sorted.jsonl").read_text(encoding="utf-8").splitlines()) == 200
```

`SortKey(path, descending=False, nulls="last", natural=True, collation="uz"|"simple"|"unicode",
case_sensitive=False)`; `sort_records(doc, keys, source=None, memory_limit=None, progress=None, cancel=None)`.

## 5. Export to JSON, JSON Lines, CSV, XLSX and SQLite

`export(doc, out_path, ExportOptions(fmt, ...), source=None)` writes all records or a selection. JSON and
JSON Lines are re-formatted at the token level (numbers and strings stay byte-identical, non-ASCII text is
never `\u`-escaped). CSV/XLSX columns are nested paths (`contact.city`); arrays are joined (`join_sep`) or
expanded into one row per element (`array_mode="rows"` / `explode="orders"`). SQLite gets one table per
array with parent-child links.

```python
from jsonreshape import discover_columns
from jsonreshape.writer import export_node, format_for_path

with Document.open("data.json") as doc:
    assert discover_columns(doc) == ["id", "name", "active", "score", "contact.city", "contact.phone", "orders"]

    csv = export(doc, "data.csv", ExportOptions("csv", columns=["id", "name", "contact.city", "orders[*].sum"]))
    assert csv.rows == 200
    lines = Path("data.csv").read_text(encoding="utf-8-sig").splitlines()     # csv_bom=True by default
    assert lines[0] == "id,name,contact.city,orders[*].sum" and lines[1] == "0,Name 0,Samarqand,25;38"

    xlsx = export(doc, "orders.xlsx", ExportOptions("xlsx", explode="orders"))  # one row per order
    assert xlsx.rows == sum(max(1, len(r["orders"])) for r in records)

    sqlite = export(doc, "data.sqlite", ExportOptions("sqlite"))
    assert sqlite.tables == ["records", "orders"]

    pretty = export(doc, "first3.json", ExportOptions("json", indent=4, sort_keys=True), source=range(0, 3))
    assert pretty.records == 3

    one_node = export_node(doc, doc.node("[0].orders"), "orders0.json")       # one node, re-indented
    assert json.loads(Path("orders0.json").read_text(encoding="utf-8")) == records[0]["orders"]

assert format_for_path("x.csv.gz") == "csv" and format_for_path("x.ndjson") == "jsonl"
```

`ExportOptions(fmt, indent=2, sort_keys=False, keep_wrapper=True, columns=None, array_mode="join",
explode=None, join_sep=";", csv_delimiter=",", csv_bom=True, xlsx_max_rows=1048576, xlsx_split="sheets",
as_keyed=False)`; `ExportOptions.from_settings(fmt)` takes the defaults from the user settings. A name
ending in `.gz` gives a gzip-compressed JSON / JSON Lines / CSV file. `ExportReport` has `records`, `rows`,
`bytes_written`, `files`, `columns`, `tables`, `warnings` (localized texts) and `warning_keys`.
`jsonreshape.writer.export_many([(doc_a, None), (doc_b, rs)], "all.json", opts)` writes several documents
into one file; `jsonreshape.filetools.extract(doc, "cities.jsonl", opts, path="contact.city")` writes the
values at a path.

## 6. Edit two values and save a copy

`EditSession` is the editor: every command is one undo step, persisted at once in an *edit layer* on disk
(continuous autosave; a crashed session is recovered on the next open). Values are given as JSON text. The
source file never changes until `save()`; `save(target)` writes a copy ("Save As"), `save()` replaces the
file atomically keeping `<file>.bak`. After a save the layer is discarded and the session is closed.

```python
from jsonreshape import EditSession, Ref

original = Path("data.json").read_bytes()
with Document.open("data.json") as doc:
    doc.build_index()                                       # a session needs the complete index
    with EditSession.open(doc) as session:
        session.set_value(Ref(0, ("name",)), '"Alisher Navoiy"')          # record #0, path inside it
        session.set_value(Ref(1, ("contact", "city")), '"Xiva"')
        assert session.dirty and session.can_undo and session.undo_label == "Value changed"
        assert doc.record(0)["name"] == "Alisher Navoiy"                   # the document shows the edits
        report = session.save("edited.json")                               # Save As; closes the session
        assert report.records == 200 and report.edited_records == 2 and not report.in_place
assert Path("data.json").read_bytes() == original                          # the input is untouched

with Document.open("edited.json") as copy:
    copy.build_index()
    assert copy.record(0)["name"] == "Alisher Navoiy" and copy.record(1)["contact"]["city"] == "Xiva"
    assert copy.record(2) == records[2]
```

`Ref(record, path, ipath=None)`: `record` is the record position (`None` = the wrapper of a wrapped
document), `path` a tuple of keys and indices relative to the record value. Other commands: `set_path`
(creates missing object members), `rename_key`, `change_type`, `insert`, `paste`, `delete`, `duplicate`,
`move`, `delete_records`, `duplicate_records`, `undo`, `redo`, `discard`; `bulk_preview(BulkSpec(...))` /
`bulk_apply(preview)` run a bulk operation with exact counts first (`jsonreshape.bulk`);
`import_patch("changes.json")` / `export_patch("changes.json")` are RFC 6902 JSON Patch
(`jsonreshape.jsonpatch`). The edit layer lives under the app folder (`JSONRESHAPE_HOME`, §15) or next to
the file (`Settings.edits_location="beside"`); a second process opening a session on the same file gets
`EditError("edits.error.locked")`.

## 7. Apply a mapping (the structure builder)

A *mapping* is a tree describing the output structure: `infer_document(doc)` builds the default tree (every
field as it is), `Designer` edits it with the same operations as the GUI (move, delete, rename, group, merge,
unwrap, flatten; each one undo step), `save_mapping` / `load_mapping` read and write the mapping file, and
`apply_mapping` (`jsonreshape.engine.apply`) streams the whole document through it. The semantics are
exactly those of the `json-tuzilma.html` reference engine.

```python
from jsonreshape import ApplyOptions, Designer, apply_mapping, infer_document, load_mapping, save_mapping
from jsonreshape.engine import preview
from jsonreshape.mapping import make_input_info

with Document.open("data.json") as doc:
    doc.build_index()
    tree = infer_document(doc).tree                 # root = the record array, "[]" = one record
    assert [c.key for c in tree.find("[]").children] == ["id", "name", "active", "score", "contact", "orders"]

    designer = Designer(tree)
    assert designer.move(tree.find("[].contact.city"), tree.find("[]"), "inside").applied   # city -> record level
    assert designer.delete(tree.find("[].contact")).applied                                 # drop the rest of contact
    assert designer.rename(tree.find("[].city"), "shahar").applied
    assert designer.steps == 3 and designer.can_undo
    assert preview(tree, doc.record(0)) == {"id": 0, "name": "Name 0", "active": True, "score": 32.38,
                                            "orders": records[0]["orders"], "shahar": "Samarqand"}

    save_mapping(tree, "mapping.json", input_info=make_input_info(doc))
    tree2, info = load_mapping("mapping.json")
    assert info["kind"] == "array" and info["records"] == 200

    report = apply_mapping(doc, tree2, "reshaped.json", ApplyOptions(indent=2))
    assert report.records_in == 200 and report.items_out == 200 and report.warnings == []

with Document.open("reshaped.json") as out:
    out.build_index()
    assert list(out.record(5)) == ["id", "name", "active", "score", "orders", "shahar"]
```

`MappingTree.find(ref)` accepts a REF string (`""` the root, `"[]"` the record element,
`"[].contact.city"`, `"orders[1]"`) or a node. `Designer` operations return an `OpResult(applied, changed,
message, ...)` instead of raising. `ApplyOptions(jsonl=False, indent=2, skip_huge=False, workers=None)`;
`apply_mapping(doc, tree, out_path, opts=None, source=None, progress=None, cancel=None)` returns an
`ApplyReport` (`records_in`, `items_out`, `passes`, `skipped_huge`, `unmapped_records`, `warnings`). A
mapping made for another kind of document raises `MappingError("engine.error.kind_mismatch")`.

## 8. Statistics and duplicates

`field_stats()` computes, in one pass, the presence, types, distinct values, most frequent values and exact
number statistics of any number of fields (path patterns: `contact.city`, `orders[*].sum`, `..name`, `$key`);
`exact=True` disables the HyperLogLog / Misra-Gries sketches that bound memory on huge files.
`find_duplicates()` groups records by the exact canonical value of a key.

```python
from jsonreshape import field_stats, find_duplicates
from jsonreshape.stats import format_stats

with Document.open("data.json") as doc:
    city, score = field_stats(doc, ["contact.city", "score"], top=3)
    assert (city.present, city.missing, city.nulls, city.distinct) == (200, 0, 36, 5)
    assert [(v.text, v.count) for v in city.top][0] == ('"Samarqand"', 46)
    assert (score.number.min_text, score.number.max_text, score.number.mean_text) == ("0.02", "99.45", "48.6108")
    text = format_stats([city, score], records=doc.record_count)        # the CLI's report text
    assert text.startswith("Field: contact.city")

    dupes = find_duplicates(doc, "contact.city")         # keep="first": the first record of a group is not a duplicate
    assert (dupes.groups, dupes.duplicates, len(dupes.records)) == (5, 195, 195)
    dupes.close()                                        # releases the RecordSet of duplicate positions
```

## 9. Validate against a JSON Schema

`jsonreshape.schema.infer_schema(doc)` collects the structure (types, presence, examples);
`generate_schema()` turns it into a JSON Schema (draft 2020-12; `strict=True` adds lengths, ranges, enums,
formats; `document=False` describes one record instead of the whole file). `validate()` checks every record
of a document with the package's own validator (exact numbers, ECMA regular expressions) and returns a
`ValidationReport`; `compile_schema(value).is_valid(instance)` checks one value in memory.

```python
from jsonreshape import compile_schema, generate_schema, load_schema
from jsonreshape.jschema import dump_schema, validate
from jsonreshape.schema import infer_schema

with Document.open("data.json") as doc:
    structure = infer_schema(doc, sample=None)                     # None = every record (default: a sample)
    Path("data.schema.json").write_bytes(dump_schema(generate_schema(structure, doc=doc)))

    compiled = compile_schema(load_schema("data.schema.json"))
    report = validate(doc, compiled)
    assert report.ok and report.scanned == 200 and report.invalid == 0 and report.scope == "document"
    report.close()

    record_schema = compile_schema(generate_schema(structure, doc=doc, document=False))
    assert record_schema.is_valid(doc.record(0)) and not record_schema.is_valid({"id": "x"})
    issue = record_schema.errors({"id": "x"})[-1]
    assert issue.path == ("id",) and issue.message == "Wrong type: expected integer, got string."
```

`validate(doc, compiled, source=None, scope="auto"|"record"|"document", max_per_record=10,
max_issues=1000, formats=False, on_issues=None)`; the report has `issues` (`Issue.record`, `path`,
`keyword`, `message`), `counts` by keyword, `records` (a `RecordSet` of the invalid records) and `ok`.

## 10. Split and merge

`split()` cuts a document into parts (by number of parts, bytes per part or records per part) written byte
for byte in the document's own kind and encoding — or through the writer when `export=ExportOptions(...)`
is given. `merge()` writes several opened documents into one JSON / JSON Lines file, optionally
deduplicated by a key. The output folder must exist.

```python
from jsonreshape.filetools import MergeOptions, SplitOptions, merge, split

Path("parts").mkdir(exist_ok=True)
with Document.open("data.json") as doc:
    parts = split(doc, SplitOptions(parts=4, out_dir="parts"))        # balanced by bytes
    assert [p.path.name for p in parts.parts] == ["data_1.json", "data_2.json", "data_3.json", "data_4.json"]
    assert sum(p.records for p in parts.parts) == 200 and parts.engine == "same"

docs = [Document.open(p.path) for p in parts.parts]
try:
    merged = merge(docs, "merged.json", MergeOptions(export=ExportOptions("json", indent=None)))
    assert merged.records == 200 and merged.duplicates == 0
finally:
    for d in docs:
        d.close()
assert json.loads(Path("merged.json").read_text(encoding="utf-8")) == records
```

`SplitOptions(parts=None, size=None, records=None, balance="bytes", out_dir=None, name="{stem}_{n}{ext}",
keep_wrapper=True, drop_broken=False, export=None)`; `MergeOptions(export, dedupe_by=None, keep="first",
allow_partial=False)`.

## 11. Repair

`repair_file()` reads a broken JSON / JSON Lines file and writes a strictly valid one, listing every change
(`RepairReport.fixes`: code, line, column, before, after); data it cannot interpret stops it with
`UnrepairableError` instead of a guess. `check_only=True` only reports.

```python
from jsonreshape import repair_file

Path("broken.json").write_text('[{"id": 1, "name": \'Ali\', score: NaN,}, // comment\n {"id": 2}]', encoding="utf-8")
repaired = repair_file("broken.json", "fixed.json")
assert repaired.written and repaired.records == 2
assert repaired.counts == {"single_quote": 1, "unquoted_key": 1, "nan": 1, "trailing_comma": 1, "comment": 1}
assert [(f.code, f.line, f.column, f.before, f.after) for f in repaired.fixes][0] == ("single_quote", 1, 20, "'Ali'", '"Ali"')
repaired.close()
assert json.loads(Path("fixed.json").read_text(encoding="utf-8")) == [{"id": 1, "name": "Ali", "score": None}, {"id": 2}]

check = repair_file("broken.json", None, check_only=True)        # nothing is written
assert not check.written and check.counts == repaired.counts
check.close()
```

`RepairOptions(nan="null"|"string", fixes=None, format=None, record_path=None, output_kind=None,
allow_partial=False)`; `report.write("fixes.csv")` writes the full list of fixes.

## 12. Diff

`diff_files()` compares two documents record by record — by a key (`key="id"`), by position, or aligned by
content (`mode="auto"` chooses) — and returns a `DiffReport` with the counts; `iter_record_diffs()` yields
one `RecordDiff` per differing record (`status`, `a_index`, `b_index`, `key`, `diff.changes`) and
`write_diff()` streams them to JSON, JSON Lines or CSV. `diff_values(a, b)` compares two values in memory.

```python
from jsonreshape import diff_files
from jsonreshape.diff import diff_values, iter_record_diffs, write_diff

other = json.loads(Path("data.json").read_text(encoding="utf-8"))
other[3]["score"] = 1000
del other[5]
Path("other.json").write_text(json.dumps(other, ensure_ascii=False), encoding="utf-8")

with Document.open("data.json") as a, Document.open("other.json") as b:
    report = diff_files(a, b, key="id")
    assert (report.changed, report.removed, report.added, report.equal) == (1, 1, 0, 198)
    report.close()

    changed = [rd for rd in iter_record_diffs(a, b, key="id") if rd.status == "changed"]
    assert (changed[0].key, changed[0].a_index, changed[0].b_index) == (3, 3, 3)
    assert [(c.op, c.path, c.a, c.b) for c in changed[0].diff.changes] == [("changed", ("score",), 6.99, 1000)]

    assert write_diff(iter_record_diffs(a, b, key="id"), "diff.json") == 2

assert [(c.op, c.path) for c in diff_values({"a": 1, "b": [1, 2]}, {"a": 1.0, "b": [1, 3]}).changes] == [("changed", ("b", 1))]
```

## 13. Errors and languages

Every expected error is a `JsonReshapeError`: it carries an i18n `key`, `params`, the localized `message`
(`str(exc)`) and an optional `hint`. Unexpected errors (bugs, OS failures outside the known cases) are
ordinary Python exceptions.

```
JsonReshapeError                       key "error.generic"
├── Cancelled                          cancel.cancel() was called
├── UserInputError                     bad arguments, options, paths (CLI exit 1)
│   ├── QuerySyntaxError               a filter / JMESPath expression; .pos, .text, .caret()
│   │   └── PathSyntaxError            a path pattern
│   ├── query.FilterEvalError
│   ├── jsonpatch.PatchError
│   └── mapping.MappingError
├── JsonSyntaxError                    the input is not valid JSON; .line, .column, .offset, .context()
├── FileFormatError                    empty file, wrong forced format or record path
├── TooLargeError                      a value over Settings.large_node_bytes was requested whole
├── FileChangedError                   the input changed on disk during the operation (nothing saved)
├── ExportError                        a record cannot be written in the requested form
├── IndexBuildError
├── EditError                          editing: locked layer, stale scope, huge record, ...
│   └── ConversionError
└── repair.UnrepairableError           .report holds the partial RepairReport
```

Messages are Uzbek by default; `set_language("en")` switches the whole process to English (the CLI reads
`--lang` and `JSONRESHAPE_LANG`, the library does not — call `set_language` yourself).
`jsonreshape.i18n.t(key, **params)` renders any key.

```python
from jsonreshape import FileFormatError, JsonReshapeError, QuerySyntaxError, UserInputError, get_language

try:
    compile_filter("score >")
except QuerySyntaxError as exc:
    assert isinstance(exc, UserInputError) and isinstance(exc, JsonReshapeError)
    assert exc.key == "error.query_syntax" and exc.pos == 7
    assert str(exc).startswith("Expression error at character 8: incomplete expression")
    assert exc.hint.startswith("Example:")

Path("empty.json").write_text("", encoding="utf-8")
try:
    Document.open("empty.json")
except FileFormatError as exc:
    assert exc.key == "reader.empty_file" and exc.params["path"].endswith("empty.json")

set_language("uz")
try:
    compile_filter("score >")
except QuerySyntaxError as exc:
    assert exc.message.startswith("Ifodada xato (8-belgi)")
set_language("en")
assert get_language() == "en"
```

## 14. Progress and cancellation

Every long operation takes `progress: Progress | None` and `cancel: CancelToken | None`. `Progress`
counts units of work (bytes of the data file by default) and calls `callback(info)` at most every
`min_interval` seconds and once more when it finishes, on the thread doing the work; `info` is a
`ProgressInfo(done, total, fraction, rate, eta, elapsed, message, unit, finished)`. `CancelToken.cancel()`
makes the operation raise `Cancelled` at its next check (within ~50 ms of work); a cancelled export leaves
no file behind.

```python
from jsonreshape import CancelToken, Cancelled, Progress

seen = []
with Document.open("data.json") as doc:
    export(doc, "copy.json", ExportOptions("json"), progress=Progress(callback=seen.append, min_interval=0.0))
assert seen[-1].finished and seen[-1].fraction == 1.0 and seen[-1].unit == "bytes"

cancel = CancelToken()
cancel.cancel()                                   # from any thread, e.g. a GUI button
try:
    with Document.open("data.json") as doc:
        export(doc, "never.json", ExportOptions("json"), cancel=cancel)
    raise AssertionError("not cancelled")
except Cancelled as exc:
    assert str(exc) == "The operation was cancelled."
assert not Path("never.json").exists()
```

`jsonreshape.BackgroundTask(fn, *args, progress=, cancel=)` runs `fn(*args, progress=..., cancel=...)` in a
daemon thread (`.start()`, `.join()`, `.result()`, `.cancel()`); `Document.start_index()` returns one.

## 15. Settings

`Settings` is a frozen-by-convention dataclass persisted as JSON in the app folder. `get_settings()` is the
process-wide instance (loaded once; a missing or corrupt file gives the defaults); build a changed one with
`dataclasses.replace(...)` — values are validated (`UserInputError("settings.invalid")`), byte sizes accept
`"256MB"` — and either pass it to `Document.open(path, settings=...)` or install it with `set_settings()`.

```python
import dataclasses

from jsonreshape import Settings, get_settings, set_settings

defaults = get_settings()
assert defaults.memory_limit_bytes == 512 * 2**20 and defaults.large_node_bytes == 50 * 2**20

custom = dataclasses.replace(defaults, memory_limit_bytes="256MB", sort_memory_bytes="64MB")
assert custom.memory_limit_bytes == 256 * 2**20
with Document.open("data.json", settings=custom) as doc:
    doc.build_index()
    assert doc.settings is custom

try:
    Settings(memory_limit_bytes=-1)
except UserInputError as exc:
    assert exc.key == "settings.invalid"
set_settings(defaults)
```

The fields and their defaults: `memory_limit_bytes` 512 MiB (the budget of filter, sort, statistics,
duplicates, diff), `sort_memory_bytes` 256 MiB, `large_node_bytes` 50 MiB (bigger records are huge),
`large_node_children` 10000, `head_bytes` 4 MiB (format detection), `sample_size` 10000 (schema samples),
`cache_dir` / `cache_limit_bytes` 20 GiB (index files and prepared copies of gzip / UTF-16 inputs, least
recently used first), `export_indent` 2, `csv_bom` True, `csv_delimiter` `,`, `array_join_sep` `;`,
`xlsx_max_rows` 1048576, `edits_location` `appdata` | `beside`, `backup_on_save` True, `undo_limit` 1000,
`diff_align_max_bytes` 200 MiB, `language` `uz` | `en` (applied by the CLI and the GUI; a library caller
uses `set_language`).

Environment variables: `JSONRESHAPE_HOME` — the app folder (settings, index cache, edit layers, record
sets, logs; default `%LOCALAPPDATA%\jsonreshape` on Windows, `~/.local/share/jsonreshape` elsewhere);
`JSONRESHAPE_BACKEND` — `rust` | `python` | `auto` (the pure-Python core gives identical results, slower);
`JSONRESHAPE_LANG` — read by the CLI and the GUI only.

## 16. Exact numbers: `jsonreshape.codec`

`codec.loads(data)` parses JSON like `json.loads`, but keeps every number token exact: plain tokens become
`int` / `float`, the others `JsonFloat` (a `float` carrying its `raw` text) or `JsonInt` (`-0`, integers
beyond 64 bits). `codec.dumps(value, indent=None, sort_keys=False)` writes them back as their raw text —
compact `bytes`, never `\u`-escaped. The values a `Document` returns (`record()`, `Node.value()`,
`iter_records()`) are these types.

```python
from jsonreshape import codec

value = codec.loads(b'{"a": 1.10, "b": 1e400, "c": 12345678901234567890, "d": -0}')
assert codec.dumps(value) == b'{"a":1.10,"b":1e400,"c":12345678901234567890,"d":-0}'
assert value["a"] == 1.1 and codec.number_text(value["a"]) == "1.10"
```

## 17. Stability policy

Public API, kept backwards compatible within a major version (deprecations are announced in
`CHANGELOG.md` one minor version ahead):

* every name exported from the package, `jsonreshape.__all__` — each is an alias of the module-level
  class or function listed below, resolved lazily on first use so that `import jsonreshape` stays cheap
  (the CLI starts in about 0.1 s);
* the module-level functions and classes named in this document (`jsonreshape.search.search`,
  `jsonreshape.jschema.validate`, `jsonreshape.filetools.split`, ... — the modules' `__all__` lists);
* the dataclass fields, report attributes and keyword arguments shown here; the i18n keys of errors
  (`exc.key`) and the fix codes of the repair report;
* the file formats: the index cache and the edit layer are private, the mapping file, the JSON Patch
  files, the filter presets and the settings file are public and versioned.

Everything that starts with an underscore is private, as are the GUI package (`jsonreshape.gui`), the
core backends (`jsonreshape.core`), `overlay`, `editstore`, `editops`, `index`, `stream` and `cli`
internals: they may change in any release.

| `jsonreshape.` | defined in | | `jsonreshape.` | defined in |
|---|---|---|---|---|
| `Document`, `Node`, `FormatInfo` | `reader` | | `field_stats` | `stats` |
| `RecordSet`, `filter_records`, `sort_records` | `recordset` | | `find_duplicates` | `dupes` |
| `SortKey` | `sortkeys` | | `generate_schema`, `compile_schema`, `load_schema` | `jschema` |
| `compile_filter` | `query` | | `repair_file` | `repair` |
| `SearchOptions` | `search` | | `diff_files` | `diff` |
| `ExportOptions`, `ExportReport`, `export`, `discover_columns` | `writer` | | `Settings`, `get_settings`, `set_settings` | `settings` |
| `EditSession`, `Ref` | `edits` | | `Progress`, `ProgressInfo`, `CancelToken`, `BackgroundTask` | `tasks` |
| `Designer`, `infer_document`, `load_mapping`, `save_mapping` | `mapping` | | `JsonReshapeError`, `Cancelled`, `UserInputError`, `QuerySyntaxError`, `PathSyntaxError`, `JsonSyntaxError`, `FileFormatError`, `FileChangedError`, `TooLargeError`, `ExportError`, `EditError` | `errors` |
| `ApplyOptions`, `apply_mapping` (= `engine.apply`) | `engine` | | `set_language`, `get_language` | `i18n` |

Functions whose name is also a module name are not re-exported, to keep `jsonreshape.search` the module:
use `from jsonreshape.search import search`, `from jsonreshape.jschema import validate`,
`from jsonreshape.filetools import split, merge, extract`, `from jsonreshape.repair import repair_file`.

```python
import jsonreshape

assert jsonreshape.Document is jsonreshape.reader.Document
assert jsonreshape.apply_mapping is jsonreshape.engine.apply
assert set(jsonreshape.__all__) <= set(dir(jsonreshape)) and jsonreshape.__version__
```
