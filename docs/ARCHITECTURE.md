# jsonreshape — architecture & interface specification

This document is the **source of truth** for module boundaries, public interfaces and
exact semantics. Code must follow it; when code and spec disagree, fix one of them
explicitly (never silently). The user-facing requirements are in `AGENT_PROMPT.md`
(Uzbek). Section numbers like "§6.2" refer to that file.

Language conventions: code, comments, docstrings and this document are English.
Every user-facing text (CLI output, error messages, GUI) goes through
`jsonreshape.i18n.t()` — Uzbek (Latin) primary, English fallback, Russian (`i18n_ru.py`).

---------------------------------------------------------------------------------

## 0. Global conventions

* Python ≥ 3.11 (dev venv uses 3.14: `.venv\Scripts\python.exe`). `from __future__ import annotations`
  in every module. Type hints everywhere public.
* No `eval`, `exec`, builtin `compile`, `pickle.loads` of untrusted data, or `__import__`
  tricks anywhere in `jsonreshape/` (a test scans the AST). `re.compile` is fine.
* No networking: never import `socket`, `urllib.request`, `http.client`, `requests`,
  `ssl` in `jsonreshape/` (a test scans imports).
* No `print` in library code. Use `log = logging.getLogger(__name__)`.
* Source data files are opened only through `jsonreshape.fileio.open_shared()` (Windows:
  share read/write/delete, long paths). Never load a whole data file into memory.
* Every long operation takes keyword args `progress: Progress | None = None,
  cancel: CancelToken | None = None` (see `tasks.py`), calls `cancel.check()` at least
  every ~50 ms of work, and reports progress in **bytes** of the data file processed
  (unit `"bytes"`) unless stated otherwise.
* Offsets are absolute byte offsets into the *data file* (the UTF-8 file actually read; see
  §5.1 for gzip/UTF-16 inputs). A *span* is `(start, end)` with `end` exclusive.
* "Span arrays" crossing module/core boundaries are `bytes` objects holding little-endian
  `uint64` values (`array('Q').tobytes()` on little-endian hosts). Pairs are interleaved
  `start0, end0, start1, end1, ...`.
* Record numbers are **0-based** everywhere (`#0` is the first record), consistent with
  JSON array indices (`data[0]`).
* Environment variables:
  * `JSONRESHAPE_HOME` — overrides the app data dir (tests set it to a temp dir).
  * `JSONRESHAPE_BACKEND` — `rust` | `python` | `auto` (default auto).
  * `JSONRESHAPE_LANG` — `uz` | `en` | `ru`.
* Tests: `tests/test_*.py`, pytest. Markers: `slow` (> ~5 s), `bench` (big-file
  benchmark, excluded by default), `gui` (pytest-qt). Default run: `pytest -m "not bench"`.
  Tests never write outside `tmp_path` / `JSONRESHAPE_HOME` (autouse fixture in conftest).
* Each module owns its section in `jsonreshape/i18n.py` (`# ---- <section> ----`).
  Add message keys only inside your section; key prefix = section name
  (e.g. `query.syntax.unexpected_token`). A test checks that every `t("...")` literal key
  used in the package exists in both `uz` and `en`.

### 0.1 Package layout

```
pyproject.toml            maturin build (mixed Rust/Python); entry point jsonreshape = jsonreshape.cli:main
rust/                     Rust crate "jsonreshape-core" -> Python module jsonreshape.core._rscore
  Cargo.toml
  src/lib.rs              PyO3 bindings only (thin)
  src/scanner.rs ...      pure-Rust logic (no PyO3 types) so it is unit-testable
jsonreshape/
  __init__.py             __version__
  __main__.py             python -m jsonreshape -> cli.main()
  i18n.py                 MESSAGES dict + t()   (the single dictionary file)
  errors.py               exception hierarchy (user-facing, localized)
  appdirs.py              %LOCALAPPDATA%\jsonreshape\{index,logs,tmp,recordsets}
  tasks.py                CancelToken, Progress, BackgroundTask
  log.py                  logging setup (rotating file in logs dir)
  settings.py             Settings dataclass (JSON file)
  fileio.py               shared-read open, long paths, identity, gzip/UTF-16 preparation, atomic write
  env.py                  environment check (rust backend, ijson yajl2_c, versions)
  codec.py                exact JSON loads/dumps (orjson + precise fallback), JsonFloat/JsonInt
  paths.py                path parsing/formatting, PathPattern, resolve/get
  core/__init__.py        backend selection; re-exports the core API
  core/constants.py       modes, flags, error codes (mirrored in Rust)
  core/_pycore.py         pure-Python reference implementation of the core API
  core/_rscore.pyd        built Rust extension (same API)
  index.py                IndexStore: on-disk binary span index (mmap), cache keys, LRU eviction
  reader.py               Document: format detection, indexing, record access, Node tree, errors
  stream.py               ijson-based streaming access for huge records (> large_node_bytes)
  recordset.py            RecordSet (disk uint64 array), filter_records, sort_records (external sort)
  sortkeys.py             typed order-preserving sort keys, Uzbek collation, natural order
  query.py                filter language, builder model, JMESPath adapter, number predicates
  textmatch.py            text search matching: casefold, apostrophes, Latin<->Cyrillic, prefilter needles
  search.py               full-file search runner (byte prefilter + parse confirmation)
  schema.py               structure inference (types, presence, examples) from a sample
  writer.py               streaming writers: JSON, JSONL, CSV, XLSX, SQLite
  writer_parquet.py       Parquet and Feather / Arrow IPC export: one columnar engine (typed columns, type decision +
                          two-pass restart, row groups) with two write backends (§8; optional pyarrow)
  overlay.py              edit state: piece table, content layers, virtual address space, Remap/ChangeSet (§12.1)
  editstore.py            edit layer on disk: SQLite steps + units.dat, recovery, locks (§12.2)
  editops.py              byte-level node edits, style detection, formatting, type conversion (§12.4)
  edits.py                EditSession: edit commands, undo/redo, bulk/save/patch entry points (§12.5)
  bulk.py, dupes.py       bulk operations (preview -> exact counts -> apply); duplicate finder (§12.6)
  saver.py                Save / Save As in the original kind and encoding, validated, .bak (§12.7)
  jsonpatch.py            RFC 6902 export/import of the edits (§12.8)
  mapping.py              mapping tree (reference node model), designer operations, undo/redo, mapping files (§13.1)
  engine.py               mapping evaluation (exact json-tuzilma.html semantics, compiled), streaming apply (§13.2-13.3)
  stats.py, smartvalues.py  per-field statistics (exact / HLL / Misra-Gries); smart value detection (§14.1, §14.7)
  jschema.py              JSON Schema 2020-12 generation + own validator (§14.3)
  filetools.py            split / merge / extract (§14.4)
  repair.py, diff.py      tolerant repair with a report; structural diff (§14.5, §14.6)
  cli.py                  argparse CLI
  gui/                    PySide6 viewer (stage 2; see §11) + editing (stage 3; see §12.9)
tests/                    pytest suite (+ tests/jsongen.py shared generators)
bench/                    synthetic data generator + benchmark runner -> README table
docs/ARCHITECTURE.md      this file
```

### 0.2 Public Python API (stage 7, as built)

`docs/API.md` is the reference of the library interface; `tests/test_api_docs.py` executes every 

---------------------------------------------------------------------------------

## 1. Core API (`jsonreshape.core`) — Rust + pure-Python, identical results

`jsonreshape/core/__init__.py` selects the backend (`BACKEND` = `"rust"` or `"python"`)
and re-exports: `Scanner`, `Reformatter`, `MultiFinder`, `PathExtractor`,
`numbers_plain`, plus constants. `get_backend(name)` returns a specific backend module
(tests run every core test against every available backend and compare them).

Both backends implement **exactly** the same classes/functions with the same signatures,
the same results and the same exceptions (`ValueError` for bad arguments). Results are
compared byte-for-byte by the test-suite (§14 test 12). The Python backend is the
*reference*; when in doubt, this spec decides.

Rust specifics: heavy work runs inside `py.detach(...)` (PyO3 0.30 name for releasing
the GIL). Pure logic lives in plain Rust modules; `lib.rs` only converts types.
Performance targets (Rust, typical record-shaped JSON, warm cache):
Scanner ≥ 400 MB/s, Reformatter ≥ 300 MB/s, MultiFinder ≥ 1 GB/s (few needles),
PathExtractor ≥ 300 MB/s, numbers_plain ≥ 800 MB/s.
Python reference: correctness first; aim for ≥ 15 MB/s on the Scanner using regex
fast paths (strings, whitespace runs, numbers), never per-byte Python loops for the
common case.

### 1.1 JSON grammar used by the core (RFC 8259, strict)

* whitespace: `0x20 0x09 0x0A 0x0D`
* values: object, array, string, number, `true`, `false`, `null`
* number: `-?(0|[1-9][0-9]*)(\.[0-9]+)?([eE][+-]?[0-9]+)?`
* string: `"` … `"`; escapes `\" \\ \/ \b \f \n \r \t \uXXXX` (hex any case); raw bytes
  `< 0x20` are errors; content must be valid UTF-8 (no overlongs, no UTF-8-encoded
  surrogates, max U+10FFFF). Lone surrogate *escapes* such as `\ud800` are accepted.
* a byte `>= 0x80` outside a string is `unexpected_char`.
* nesting depth limit: `max_depth` (default 100000) → `depth_limit`.

### 1.2 `Scanner` — validating, resumable record indexer

```python
class Scanner:
    def __init__(self, mode: int, target_path: list[str | int] = [], base_offset: int = 0,
                 max_depth: int = 100000) -> None: ...
    def feed(self, chunk: bytes) -> bytes: ...     # returns newly completed record spans (u64 LE pairs, absolute)
    def finish(self) -> bytes: ...                 # signals EOF; returns remaining spans
    # read-only properties
    error: tuple[int, str, str, int, int] | None   # (offset, code, expected, line, line_start)
    record_count: int        # spans emitted so far
    consumed: int            # absolute offset of the next byte expected by feed()
    lines: int               # number of 0x0A bytes consumed so far
    flags: int               # FLAG_* bits observed so far (see below)
    target_found: bool       # MODE_CONTAINER: target container has started
    container_span: tuple[int, int] | None   # MODE_CONTAINER: span of target container once closed
    finished: bool           # finish() called, or error (non-LINES modes)
    def take_broken(self) -> list[tuple[int, int, int, int, str, str]]:
        # MODE_LINES: broken lines since last call: (line_no, line_start, line_end, err_offset, code, expected)
```

`base_offset` is the absolute file offset of the first byte that will be fed (e.g. 3 after
a UTF-8 BOM, or a node's start when scanning a sub-range). All offsets in results are
`base_offset + position`.

Modes (`core.constants`):

| constant | value | document shape | records emitted |
|---|---|---|---|
| `MODE_CONTAINER` | 1 | one JSON value | children of the container at `target_path`: array → element value spans; object → member spans (`start` = opening quote of key, `end` = end of value) |
| `MODE_BRACE_LIST` | 2 | `{` value (`,` value)* `}` or `{}` (non-standard) | the values |
| `MODE_LINES` | 3 | JSON Lines | one value per line (span trimmed of whitespace) |
| `MODE_STREAM` | 4 | values separated by optional whitespace (concatenated JSON) | each top-level value |
| `MODE_SINGLE` | 5 | one JSON value | the root value (exactly one span) |

`target_path` (MODE_CONTAINER only): list of segments; `str` = object key (compared to the
*decoded* key), `int` = array index (0-based). `[]` = the root. A value is "on the target
path" if its parent is on the path and its key/index equals the next segment. A `str`
segment never matches an array element and an `int` never matches an object member.
With duplicate keys only the **first** matching value becomes the target. If the value at
the full path is not an array/object → error `target_not_container` at its start. If the
document ends without the target → error `target_not_found` at the end offset.
Children are emitted only for the direct children of the target container. The whole
document (prefix and suffix around the target) is still fully validated.

Flags: `FLAG_ESCAPES = 1` (any backslash escape inside any string, keys included),
`FLAG_U_ESCAPES = 2` (any `\u` escape), `FLAG_NON_ASCII = 4` (any byte ≥ 0x80 inside a string).

Error handling (all modes except LINES): at the first error the scanner stops; `error` is
set; further `feed()` returns `b""`. Records completed before the error stay emitted.
`error = (offset, code, expected, line, line_start)`: `offset` = first byte that cannot be
consumed (or the end offset for EOF errors), `line` = number of `\n` bytes before `offset`
(0-based line index), `line_start` = offset just after that last `\n` (or `base_offset`).
Codes (`core.constants.ERR_*`, strings):

* `unexpected_char` – byte cannot start/continue anything valid here
* `unexpected_eof` – input ended inside a value / before the root value / container unclosed
* `invalid_number` – after `-`, `.`, `e`/`E`, or exponent sign a digit was required (offset = offending byte; at EOF it is `unexpected_eof` instead)
* `invalid_literal` – `true`/`false`/`null` mismatch (offset = first mismatching byte)
* `invalid_escape` – byte after `\` not one of `"\/bfnrtu` (offset = that byte)
* `invalid_hex` – non-hex byte inside `\uXXXX` (offset = that byte)
* `control_char` – raw byte < 0x20 inside a string (offset = that byte)
* `invalid_utf8` – invalid UTF-8 inside a string (offset = first byte of the invalid sequence, i.e. Python's `UnicodeDecodeError.start` / Rust `valid_up_to`)
* `trailing_data` – non-whitespace after the root value (MODE_CONTAINER/SINGLE/BRACE_LIST), or after the value on a JSONL line
* `depth_limit`, `target_not_found`, `target_not_container`

`expected` (string) describes what the state machine was waiting for when the error
happened: `value`, `value_or_end` (after `[`, or after `{` in BRACE_LIST), `key_or_end`
(after `{`), `key`, `colon`, `comma_or_bracket` (after an array element),
`comma_or_brace` (after an object member / brace-list value), `open_brace` (BRACE_LIST
root must start with `{`), `end` (only whitespace allowed), `digit`, `hex`, `escape`,
`string_end`, `literal`, `none` (for target_* / depth errors). Number/literal tokens end
at the first byte that cannot continue them; that byte is then judged by the next state
(so `[01]` → `unexpected_char`/`comma_or_bracket` at `1`; `[1x]` likewise; `[-]` →
`invalid_number`/`digit` at `]`; `[tru]` → `invalid_literal`/`literal` at `]`).
Empty input (only whitespace): CONTAINER/SINGLE/BRACE_LIST → `unexpected_eof`/`value`
(BRACE_LIST: `open_brace`); LINES/STREAM → no records, no error.

MODE_LINES: each line (bytes up to and excluding `\n`; a trailing `\r` is ordinary
whitespace) is an independent document holding exactly one value. Whitespace-only lines
are skipped silently. A line with an error (including an unterminated value at the line
end → `unexpected_eof` at the `\n` offset or EOF offset, and a second value on the same
line → `trailing_data`) is reported through `take_broken()` and scanning continues with
the next line; `error` stays `None` in LINES mode. `line_no` is the 0-based line index;
`line_end` excludes `\n` but includes a trailing `\r`.

### 1.3 `numbers_plain(buf: bytes) -> bool`

`buf` holds valid JSON text (a value, or an object member `"k": v`, possibly with
surrounding whitespace). Returns `True` iff **every** number token outside strings is
*plain*; plain tokens are exactly those for which `orjson.dumps(orjson.loads(tok)) == tok`
is guaranteed (verified empirically for orjson 3.13):

* plain int: matches `-?(0|[1-9][0-9]{0,17})` and is not `-0`;
* plain float: matches `-?(0|[1-9][0-9]*)\.([0-9]+)` (no exponent) and
  * fraction does not end with `0` unless the fraction is exactly `0`;
  * if the integer part ≠ `0`: integer part has ≤ 16 digits, and the significant digits
    (integer part + fraction, fraction dropped when it is exactly `0`, then trailing zeros
    stripped) number ≤ 15;
  * if the integer part = `0`: either the fraction is exactly `0`, or (leading zeros of
    the fraction + 1) ≤ 5 and the fraction without leading zeros has ≤ 15 digits.
* everything else (exponents, `-0`, ≥ 19-digit ints, …) is not plain.

Behaviour on invalid JSON is unspecified (may return anything or raise `ValueError`) but
must not crash the process.

### 1.4 `Reformatter` — streaming whitespace normaliser

```python
class Reformatter:
    def __init__(self, indent: int | str | None = None, base_level: int = 0) -> None: ...
    def feed(self, chunk: bytes) -> bytes: ...
    def finish(self) -> bytes: ...
```

Input: a stream of JSON tokens (valid JSON, possibly fragments such as a wrapper prefix
`{"meta":{…},"data":` fed in pieces; tokens and strings may be split across chunks).
Output: the same tokens byte-for-byte (strings, numbers and literals are never altered),
with whitespace outside strings replaced:

* `indent=None` (compact): no whitespace at all: `{"a":1,"b":[1,2]}`.
* `indent=n` (spaces) or `indent="\t"`: orjson/`json.dumps(indent=…)` style:
  after `{`/`[` a newline + indentation of the new level, unless the container is empty
  (then `{}`/`[]` with nothing inside); after `,` newline + indentation; before a
  closing bracket of a non-empty container newline + indentation of the outer level;
  after `:` exactly one space. Level 0 = no indentation; every emitted newline is
  followed by `(base_level + level) * indent`.
* Whitespace between top-level tokens/values is dropped (the caller adds separators).
* `finish()` flushes pending output (e.g. a `{` still waiting to know if it is empty is
  emitted as just `{`).
* `indent` must be `None`, an int 0..16, or `"\t"`; otherwise `ValueError`.
  `indent=0` means newlines without indentation.

### 1.5 `MultiFinder` — multi-literal byte search

```python
class MultiFinder:
    def __init__(self, needles: list[bytes]) -> None: ...   # ≥1 needle, none empty, else ValueError
    def find_in_spans(self, buf: bytes, spans: bytes) -> bytes: ...
    def find_first(self, buf: bytes, start: int = 0, end: int | None = None) -> int: ...
    def locate_tokens(self, buf: bytes, spans: bytes, member_spans: bool = False) -> list[tuple]: ...
```

* `find_in_spans`: `spans` are u64 LE pairs relative to `buf`, sorted by start and
  non-overlapping (caller guarantees; `ValueError` if a span is out of bounds). Returns
  u64 LE indices (positions in the span list) of every span that contains at least one
  occurrence of any needle lying **entirely inside** the span (`start ≤ p` and
  `p + len(needle) ≤ end`), in increasing order.
* `find_first`: smallest `p` in `[start, end)` such that some needle occurs at `p` and
  ends `≤ end`; `-1` if none.
* Pure byte semantics (no case folding); case/apostrophe variants are generated by the
  caller (`textmatch.py`). Rust: a needle set that is exactly every ASCII case variant
  of some patterns (plus needles without ASCII letters) — what a case-insensitive query
  produces, often 256+ needles that defeat the SIMD prefilters — is searched as those
  few lowercase patterns in the ASCII-lowercased haystack; same results.
* `locate_tokens` (the byte-mode search's confirm step, `search.py` "The locator"):
  `spans` as for `find_in_spans`, every span one JSON value — with `member_spans` an
  object member `"key": value` — optionally surrounded by whitespace, else `ValueError`
  (the rule of `PathExtractor`, also for spans without any occurrence; out-of-bounds
  spans and a span array whose length is not a multiple of 16 likewise). For every span,
  every occurrence of any needle lying entirely inside the span is considered; the
  token containing the occurrence's **first** byte — a string (object key or string
  value, quotes included), a number or `true`/`false`/`null` — is *located*; an
  occurrence starting on whitespace or structure (`{ } [ ] , :`) is ignored, one
  starting in token A and ending in token B locates A only. Each located token is
  reported once, in document order, as `(span_idx, kind, token_start, token_end, path)`:
  `kind` 0 string value, 1 object key, 2 number, 3 literal, 4 the member's own key of a
  member span; `token_start`/`token_end` absolute offsets in `buf`; `path` a tuple of
  decoded `str` keys (Python `json` semantics: an escaped surrogate pair is one code
  point, a lone surrogate escape stays one) and `int` indices — relative to the record
  value; for a key the path of its member value (parent path + `(key,)`); `()` for kind
  4 and for a scalar record. A span with at least one located token whose record has
  duplicate keys in any object (compared after decoding escapes; exact) gets one more
  tuple `(span_idx, 5, 0, 0, ())` right after its tokens ("confirm this record by
  parsing"). For a record without duplicate keys the located tokens are exactly its
  keys/scalars (in the document-order walk of the parsed value) whose raw bytes hold a
  needle occurrence that starts inside them. Every span is handled on its own (unsorted
  or overlapping spans are fine). Rust: one validating pass per span (≈ 1 GB/s); the
  occurrences are searched lazily, resuming after each located token.

### 1.6 `PathExtractor` — value spans at paths, without parsing

```python
class PathExtractor:
    def __init__(self, paths: list[list[tuple]], member_spans: bool = False) -> None: ...
    def extract(self, buf: bytes, spans: bytes) -> bytes: ...
```

Path segments: `("key", str)`, `("index", int ≥ 0)`, `("any_index",)`, `("any_key",)`.
`[]` (empty path) = the record value itself. With `member_spans=True` every span is an
object member `"k": v` and paths apply to `v`.

Result: u64 LE quads `(span_idx, path_idx, start, end)` (absolute positions within `buf`),
ordered by span, then path, then document order. Semantics must equal "parse the record
with Python's json/orjson (dict semantics) and navigate":

* `("key", k)` on an object → the value of the **last** member whose decoded key equals
  `k` (duplicate keys: last wins, like a Python dict); on an array → if `k` is a canonical
  non-negative decimal (`0` or `[1-9][0-9]*`) it acts as an index; otherwise no match.
* `("index", i)` on an array → element `i` if it exists; on anything else → no match.
* `("any_index",)` on an array → every element in order; otherwise no match.
* `("any_key",)` on an object → one value per *distinct* key, keys in order of first
  appearance, value of the last occurrence (Python dict semantics); otherwise no match.
* Invalid JSON inside a span → `ValueError`.

---------------------------------------------------------------------------------

## 2. `codec.py` — exact JSON numbers

```python
class JsonFloat(float):   # number token that orjson would not reproduce exactly (1.10, 1e400, 1E5, …)
    raw: str               # original token text; str()/repr() return raw
class JsonInt(int):       # "-0" and integers outside [-2**63, 2**64-1]
    raw: str
def is_plain_int_token(tok: str) -> bool
def is_plain_float_token(tok: str) -> bool
def loads(data: bytes | bytearray | memoryview | str) -> Any
def loads_precise(data) -> Any
def dumps(obj: Any, *, indent: int | str | None = None, sort_keys: bool = False) -> bytes
def number_text(v) -> str          # JSON text of a number (raw for Json*, orjson format otherwise)
def json_type(v) -> str            # object|array|string|number|bool|null
class JsonDecodeError(JsonReshapeError)   # raised by loads* on invalid JSON (msg, pos)
```

* Parsed numbers: plain tokens → `int`/`float` (identical to orjson); non-plain floats →
  `JsonFloat`; ints → `int` unless `-0` or out of 64-bit range → `JsonInt`.
* `loads`: if the Rust backend is active and `core.numbers_plain(data)` → `orjson.loads`
  (falls back to `loads_precise` on any orjson error); otherwise `loads_precise`
  (`json.JSONDecoder` with `parse_float`/`parse_int` hooks, NaN/Infinity rejected).
  Both paths return **identical** objects for every valid input.
* `dumps`: orjson with `OPT_PASSTHROUGH_SUBCLASS` and `default` turning `JsonFloat`,
  `JsonInt`, `Decimal` into `orjson.Fragment(raw)`; `indent=2` → `OPT_INDENT_2`; other
  indents → compact orjson output passed through `core.Reformatter`. Fallback pure-Python
  encoder for what orjson refuses (big plain ints, lone surrogates → `\udXXX` escapes).
  Output is UTF-8, non-ASCII is never `\u`-escaped (§10).
* Round-trip guarantee: `dumps(loads(x))` equals the compact form of `x` for every number
  token (`0.1`, `1e400`, `12345678901234567890`, `-0.0`, `1.10`, `-0`, `1E5` …).

## 3. `paths.py`

```python
MISSING                      # sentinel (falsy, singleton)
Path = tuple[str | int, ...]
class Wild(Enum): ANY_INDEX, ANY_KEY, DESCENDANTS
class PathPattern:
    segments: tuple[str | int | Wild, ...]; text: str
    is_concrete: bool
    def matches(self, path: Path) -> bool              # exact match (wildcards honoured)
    def matches_prefix_of(self, path: Path) -> bool    # path equals or is below a match
    def resolve(self, value) -> list[tuple[Path, Any]] # all matches, document order, dict semantics
    def values(self, value) -> list[Any]
    def to_core(self) -> list[tuple]                   # PathExtractor segments; ValueError for DESCENDANTS
def parse_path(text: str) -> Path                      # concrete; raises PathSyntaxError
def parse_pattern(text: str) -> PathPattern
def parse_location(text: str) -> Location              # Ctrl+G: "#120", "#120.a", "data[120].x", "$.data[120]", "/data/120"
def format_path(path: Path, style: str = "dotted") -> str   # dotted|jsonpath|pointer|python|js
def get(value, path: Path, default=MISSING) -> Any
```

Accepted syntaxes: dotted `a.b[0].c`, quoted keys `a["x.y"]` / `a['x']`, optional leading
`$` / `$.`, JSONPath recursive `..name` (DESCENDANTS), `*` (any key), `[*]` or `[]` (any
index), `**` (descendants), JSON Pointer `/a/0` (`~0`, `~1`; segments are `str`). Unquoted
key = run of characters other than `. [ ] " '` and whitespace. A `str` segment applied to
a list acts as an index when it is a canonical non-negative decimal (same rule as the
core). Formatting styles: dotted `a.b[0]["x y"]`; jsonpath `$.a.b[0]['x y']`; pointer
`/a/b/0/x y`; python `["a"]["b"][0]["x y"]`; js `a.b[0]["x y"]`.

## 4. Infrastructure

* `i18n.py`: `MESSAGES: dict[str, dict[str, str]]`, `t(key, /, **params)`,
  `set_language()`, `get_language()`, `has(key)`. Missing language → `en` → key itself.
  `SUPPORTED_LANGUAGES = ("uz", "en", "ru")`; `MESSAGES` keeps exactly `uz` + `en` per key, the Russian texts are
  `i18n_ru.MESSAGES_RU: dict[str, str]` (imported by `t()` on first use with `ru`; a key missing there → `en`).
  `space_grouped()` — True for `uz` / `ru` (numbers `1 234 567,5`), False for `en` (`1,234,567.5`); every count /
  decimal formatter asks it instead of comparing the language code.
* `errors.py`: `JsonReshapeError(key, **params)` (`.message`, `.hint`, `str()` localized);
  subclasses `Cancelled`, `UserInputError`, `QuerySyntaxError(pos=…)`, `PathSyntaxError`,
  `JsonSyntaxError(code, offset, line, column, expected, context_before, context_after,
  source)`, `FileFormatError`, `TooLargeError`, `FileChangedError`, `ExportError`,
  `IndexBuildError`.
* `tasks.py`: `CancelToken` (`cancel()`, `.cancelled`, `check()` raises `Cancelled`),
  `Progress(total=None, callback=None, unit="bytes", message="", min_interval=0.1)` with
  `update(done)`, `advance(n)`, `set_total()`, `set_message()`, `finish()`, `info()` →
  `ProgressInfo(done, total, fraction, rate, eta, elapsed, message, unit)`;
  `Progress.child(start_fraction, end_fraction)` maps a sub-task into a range of the parent;
  `BackgroundTask(fn, *args, name=…, progress=…, cancel=…, **kwargs)`: `start()`,
  `join()`, `done`, `result()`, `exception`, `cancel()`; `fn` receives `progress=` and
  `cancel=` keyword args.
* `appdirs.py`: `app_home()`, `index_dir(override)`, `logs_dir()`, `tmp_dir()`,
  `work_dir(override)` (big temporary files — prepared copies, sort runs, XLSX staging:
  `<Settings.cache_dir>/tmp` when a cache folder is configured, else `tmp_dir()`),
  `recordsets_dir()`, `settings_file()` (all `pathlib.Path`, created on demand with
  long-path support and probed once per process for write access). An unusable folder
  falls back (app home → `<system temp>/jsonreshape`, cache folder → the default) with
  one recorded notice (`take_notices()` / `notice_texts()`; the reader shows them as
  document warnings); only when nothing works: `JsonReshapeError("fileio.error.no_work_dir")`.
* `log.py`: `setup_logging(level="INFO", console=False)` → rotating
  `logs_dir()/jsonreshape.log` (5 MB × 3); never fails (a log file that cannot be opened
  disables file logging once; a `NullHandler` keeps `logging.lastResort` silent);
  `log_path()`, `install_excepthooks()`, `shutdown_logging()`.
* `settings.py`: `Settings` dataclass (`sample_size=10000`, `cache_dir=None`,
  `cache_limit_bytes=20 GiB`, `memory_limit_bytes=512 MiB`, `sort_memory_bytes=256 MiB`,
  `large_node_bytes=50 MiB`, `large_node_children=10000`, `head_bytes=4 MiB`,
  `language="uz"`, `export_indent=2`, `csv_bom=True`, `csv_delimiter=","`,
  `array_join_sep=";"`, `xlsx_max_rows=1048576`, `edits_location="appdata"` (`"appdata"` |
  `"beside"`, §12.2), `backup_on_save=True` (§12.7), `undo_limit=1000` (≥ 200, §12.2)),
  `Settings.load()`, `.save()`,
  `get_settings()` (process-wide cached instance). `memory_limit_bytes` is the overall
  memory budget: sorting uses at most half of it, stored search hits and huge-record value
  capture at most a quarter, writer batches are sized well under it, and the CLI warns at
  the end of a command when the process peak exceeded it.
* `fileio.py`: `long_path(p)`, `make_dirs(p)`, `open_shared(p, mode="rb")` (Windows
  share read/write/delete), `FileIdentity(path, size, mtime_ns, head_hash, tail_hash,
  sample_hash)` (sha256 of the first MiB, of the last MiB, and of 16 page-aligned 4 KiB
  blocks spread between them) + `file_identity(p)`, `grown_from(...)` /
  `appended_bytes(...)` (append-only growth: longer file, original head/tail/sample hashes
  unchanged), `sniff(head: bytes)` → `Sniff(encoding, bom_len, gzip: bool)`,
  `prepare_data_file(path, *, settings=None, progress, cancel)` → `PreparedFile(data_path,
  encoding, compression, bom_len, identity, warnings)` (gunzip / UTF-16→UTF-8 into
  `work_dir(cache_dir)/prepared`; reused when the identity matches; a truncated gzip keeps
  its prefix with a warning), `prune_prepared`, `evict_entries` (one LRU budget
  `cache_limit_bytes` for index files and prepared copies; protected (in use / recently
  used) and in-use entries are kept and not counted), `atomic_write(path)` /
  `atomic_path(path)` (temp file in the same dir → `os.replace`; temp removed on error;
  OS errors while writing become localized errors naming the target), `os_error(exc, path,
  writing=False)` (not found, permission, in use (winerror 32/33), read-only, disk full),
  `read_range(f, start, end)`.

## 5. Reader (`reader.py`, `index.py`, `stream.py`)

### 5.1 Opening and format detection

```python
class FormatInfo:   # dataclass
    kind: str            # array|jsonl|brace_list|wrapped|keyed|single|stream
    encoding: str        # utf-8|utf-8-bom|utf-16-le|utf-16-be
    compression: str | None   # gzip
    record_path: tuple | None # wrapped: path of the record array
    candidates: list[RecordPathCandidate]   # (path, closed_in_head, items_in_head, items_kind)
    auto: bool
    warnings: list[str]  # i18n keys
    data_start: int      # first byte after a BOM
class Document:
    @classmethod
    def open(cls, path, *, record_path=None, format=None, settings=None, use_cache=True,
             progress=None, cancel=None) -> Document
```

Detection (on the first `head_bytes` of the data file): gzip magic → decompress to cache;
UTF-16 BOM or NUL pattern → transcode to UTF-8 in cache; UTF-8 BOM skipped. Forced
`format`/`record_path` win. Otherwise: `.jsonl`/`.ndjson` extension → jsonl; a first
value that ends on its line followed by another value on a later line → jsonl (a
multi-line first value followed by more values → stream); root `[` → array; root `{`
whose first member is a value instead of `"key":` → brace_list (warning); root `{`
otherwise: candidate record arrays (depth ≤ 3 through objects) are collected with
`ijson.basic_parse` on the head; an array still open at the end of the head → wrapped
(auto); a fully-read small file → wrapped if the largest candidate has ≥ 2 elements that
are mostly objects and holds the majority of elements, else keyed if ≥ 2 members and
≥ 80 % of member values are objects, else single. Any other root → single (or jsonl/stream
if several values). A first value that does not end inside the head is scanned further
(up to 256 MiB) when the file's last line is a complete value by itself, so JSON Lines
files with records larger than the head are detected correctly. Forced record paths:
canonical decimal segments (`/data/0`, `data.0`) select array elements unless the head
analysis shows an object at that place. A wrong record path (not found / not a container)
is reported by the CLI as a user error (exit 1) with the detected candidates.

### 5.2 Index

`index.py` stores spans in `index_dir()/<key>.jri` (binary, mmap-read). Cache key =
sha256 of (the file identity — normalized absolute path, size, mtime_ns, head/tail/sample
hashes —, format kind, record path, index format version). Header 4096 bytes (magic
`JRIDX\0\1\0`, flags, count, metadata JSON offset/length), then `count` u64 LE pairs, then
UTF-8 JSON metadata (format info, error, broken lines, flags, container span, format hint,
timings). Written under a temp name and renamed when complete; a per-key `BuildLock`
(`<key>.lock`, OS file lock) makes concurrent processes wait for one build and reuse it;
`finalize` never deletes an index installed by another process and reuses an equivalent
canonical index instead of keeping duplicates. LRU eviction with the shared cache budget
(see `fileio.evict_entries`). While building, readers see records already scanned (live
count); after completion the file is mmapped (never read into RAM).

### 5.3 Document API (used by every other module)

```python
doc.source_path, doc.data_path, doc.size, doc.data_size, doc.info: FormatInfo
doc.build_index(progress=None, cancel=None) -> None     # blocking; idempotent; JSON errors -> doc.error (not raised)
doc.start_index() -> BackgroundTask
doc.index_complete: bool; doc.record_count: int (live); doc.error: JsonSyntaxError | None
doc.broken_lines: list[BrokenLine]; doc.broken_count: int  # jsonl
doc.flags: int                                           # core FLAG_* of the whole file
doc.member_records: bool                                 # True for keyed (record = object member)
doc.record_span(i) -> tuple[int, int]
doc.record_bytes(i) -> bytes                             # raises TooLargeError over large_node_bytes
doc.record(i) -> Any                                     # parsed value (keyed: the member VALUE)
doc.record_key(i) -> str | None                          # keyed: the member key
doc.split_member(buf: bytes) -> tuple[str, int]          # (decoded key, offset of value in buf)
doc.iter_blocks(source=None, *, block_bytes=8 MiB, progress=None, cancel=None) -> Iterator[Block]
doc.iter_records(source=None, ...) -> Iterator[tuple[int, Any]]   # (record index, value) skipping huge
doc.wrapper_parts() -> tuple[bytes, bytes] | None       # wrapped: (prefix, suffix) raw bytes around the record array
doc.root() -> Node; doc.node(path) -> Node
doc.ensure_unchanged() -> None      # FileChangedError unless the source is unchanged or only grew (append-only)
doc.check_changed() -> bool
doc.appended_bytes: int             # bytes appended after open; the snapshot of open time is used + warning reader.warn.appended
doc.format_hint: str | None         # "jsonl" | "stream": suggestion when an auto-detected document stopped with trailing data
doc.error_data_offset: int | None   # error offset in the data file (JsonSyntaxError.offset is a byte of the USER's file:
                                    # UTF-16 mapped back; gzip = offset in the decompressed data, said in the message)
doc.open_data(buffering=-1)         # an open_shared handle verified against the identity
doc.close()
```

Change detection (AGENT_PROMPT §1.7): every new handle is fstat-checked against the
identity, block reads re-check it and sanity-check span boundaries, builds verify the full
identity before installing an index, and every operation calls `ensure_unchanged()` before
committing output or reporting results; a change raises `FileChangedError` (CLI exit 2,
nothing saved). Append-only growth (an active log) is not a change: the document keeps
using the snapshot of open time and records a warning. Remaining limitation (documented in
reader.py): an edit that keeps size and mtime and touches neither the first/last MiB nor a
sampled page can go unnoticed.

`Block`: `data: bytes`, `indices: array('Q')` (record indices, iteration order),
`spans: bytes` (u64 LE pairs relative to `data`, same order, sorted & non-overlapping),
`contiguous: bool`, `huge: bool`, `selected_bytes` (bytes of the selected records — the
progress unit). Records in ascending file order are read as one contiguous range while
the gaps are small (≤ 8 KiB) and, once the source revisits bytes it already passed, only
while the selection covers ≥ 50 % of the range; other selections are scattered blocks
(records sorted by offset and sliced from a transient read-only mmap that never lives
across a `yield`, or exact unbuffered reads), concatenated with `b"\n"`.
A record larger than `large_node_bytes` is yielded alone in a `huge=True` block with
`data=b""` and `spans` holding its **absolute** span; consumers use `stream.py`.
`source` may be `None` (all records), a `RecordSet`, a `range` or an iterable of indices.

`Node`: `path`, `start`, `end`, `kind` (object|array|string|number|bool|null), `key`,
`size`, `child_count()` (on-demand child indexing; large nodes — > `large_node_bytes`
or > `large_node_children` children — store child spans on disk and never parse),
`child(i)`, `children(start, stop)`, `value()` (TooLargeError if large),
`scalar_text()`. The root of array/keyed/jsonl/stream/brace_list documents has the
records as children (main index); wrapped root = the wrapper object whose record-array
child uses the main index.

`stream.py`: `stream_values(doc, span, patterns, member=False, *, max_bytes=None,
first_only=False, cancel=None) -> dict[int, list]` (values of each `PathPattern` inside a
huge record via ijson, numbers converted to codec types; raises `CaptureLimitExceeded`
when the captured values would exceed `max_bytes`; `capture_budget(settings)` =
min(large_node_bytes, memory_limit_bytes // 4), ≥ 1 MiB), `stream_walk(doc, span,
member=False)` → `(path, "key"|"value", key_or_scalar)` events, `stream_text_prefix`,
`stream_probe`. ijson is fed adaptive chunks (64 KiB, doubling up to 16 MiB while a
string/number token stays open) so long string values stay linear. Documented ijson
deviations on this path only: `-0` reads as `0`, exponent text is normalized
(`1e400` → `1E+400`, same value), duplicate keys are reported per occurrence; a single
huge string value is held in memory once while it is examined.

Errors: a JSON error becomes `doc.error` (a `JsonSyntaxError` with 1-based line/column
in characters, ±60 chars of context). Records before the error remain usable ("open
records before the error"); callers decide (CLI: fail unless `--allow-partial`).

## 6. RecordSet, filter, sort (`recordset.py`, `sortkeys.py`, `query.py`)

```python
class RecordSet:              # ordered record indices; base set is virtual (identity)
    kind: str; description: str
    def __len__(self) -> int; def __getitem__(self, i: int) -> int
    complete: bool
    def iter_chunks(self, size: int = 65536) -> Iterator[array]
    def is_ascending(self) -> bool
    @classmethod base(cls, doc) -> RecordSet
    @classmethod from_indices(cls, indices, kind="manual", description="") -> RecordSet
    def close(self) -> None
class RecordSetBuilder: append(i); extend(arr); finish() -> RecordSet   # file in recordsets_dir()
def filter_records(doc, flt: Filter | str, source=None, *, progress=None, cancel=None, on_batch=None,
                   strategy="auto"|"extract"|"parse") -> RecordSet   # .report: FilterReport
def sort_records(doc, keys: list[SortKey | str], source=None, *, memory_limit=None, progress=None,
                 cancel=None) -> RecordSet                         # .report: SortReport
```

* Filter strategies give identical results: "extract" (Rust `PathExtractor` pulls only the
  referenced values; default with the Rust core) and "parse" (`codec.loads` per record).
  Huge records are evaluated on the ijson stream within the capture budget (too large →
  skipped + warning). `FilterReport(scanned, matched, skipped_huge, unparseable,
  eval_errors, first_error_record, first_error, too_large, strategy, elapsed, ...)`.
  A JMESPath/expression runtime error in one record = no match for that record (counted,
  localized warning naming the first record); when the first 1000 evaluated records
  (`ALL_FAILED_LIMIT`) all fail, the filter aborts with `UserInputError
  ("recordset.error.eval_failed")`.
* Sort: keys extracted by the Rust `PathExtractor` + `sortkeys.token_encoder`; in memory
  while under the limit, else sorted runs in `work_dir(cache_dir)` merged with
  `heapq.merge` (run count capped by growing the run size). Effective limit =
  min(`memory_limit` or `sort_memory_bytes`, `memory_limit_bytes // 2`).
  `SortReport(records, runs, elapsed, peak_bytes_estimate, strategy, missing,
  memory_limit, approximate, ...)`; warnings when a key is missing in every record (or
  almost every record). Progress: one monotone bar (the merge phase has its own message).
* `query.FilterPresets` (`app_home()/filter_presets.json`, inter-process `file_lock`):
  `save(name, text, lang="expr")`, `get(name)`, `list()`, `delete(name)`, `names()`.

`sortkeys.SortKey(path, descending=False, nulls="last", natural=True, collation="uz",
case_sensitive=False)`; `sortkeys.encode_token(raw: bytes, spec)` /
`encode_value(value, spec)` produce prefix-free, order-preserving byte keys
(equal for equal JSON values: `encode_token(t) == encode_value(codec.loads(t))`);
`compose(fields, specs)`; order of types: numbers < strings < booleans < arrays <
objects; null and missing placed first/last per `nulls`, independent of direction;
numbers compared by exact decimal value; strings by the chosen collation with natural
digit runs; ties → original position (stable).

`query.py`: `parse_filter(text) -> Expr`; `compile_filter(text, lang="expr"|"jmespath")
-> Filter`; `Filter.matches(value, key=None, index=None) -> bool`;
`Filter.required_patterns`; builder model `Condition(field, op, value=None,
value2=None)`, `Group(op="and"|"or", items=[], negate=False)` with `to_text()` and
`Filter.from_builder(group)`; `builder_from_text(text) -> Group | None` (inverse of `Group.to_text()` for
expressions the builder can show: and/or/not of `path OP literal`, between, in, is null/empty/missing,
exists/missing, len; `None` otherwise; used by the GUI filter panel to keep builder and text in sync).
Grammar & semantics: §9.

## 7. Search (`textmatch.py`, `search.py`)

```python
class TextQuery:  # textmatch.py
    def __init__(self, text, mode="plain"|"regex"|"number", case_sensitive=False,
                 whole_word=False, translit=False) -> None
    def find_spans(self, s: str) -> list[tuple[int, int]]   # match spans in the ORIGINAL s
    def match_number(self, number_text: str) -> bool
    def prefilter_needles(self, flags: int) -> list[bytes] | None   # None => parse-based search
def fold(s, *, casefold=True, apostrophes=True, translit=False) -> tuple[str, list[int]]
class SearchOptions: query: TextQuery; scope="both"|"keys"|"values"; path: str|None
class SearchHit: record, path, where ("key"|"value"), text, spans, key
class SearchResult: hits, hit_count, record_count, records: RecordSet, truncated, mode_used
def search(doc, opts, source=None, *, max_hits=100000, progress=None, cancel=None, on_hits=None) -> SearchResult
def iter_hits(doc, opts, source=None, *, progress=None, cancel=None) -> Iterator[SearchHit]   # every hit, bounded memory
def reference_search(doc, opts, source=None) -> list[SearchHit]   # brute force (tests)
def export_hits(hits, path, fmt="csv"|"json") -> None
class SearchHistory    # app_home()/search_history.json, deduplicated, ≤ 200 entries
```

Byte mode (Rust core): `MultiFinder.find_in_spans` selects candidate records, then
`MultiFinder.locate_tokens` (§1.5) returns only the keys/scalars that contain a needle
occurrence, with their paths; only those tokens are decoded and confirmed (records with
duplicate keys, very big candidates and huge records use the parse/stream confirm path).
Results equal `reference_search` exactly (order included). The search follows a growing
index (results appear while indexing). Stored hits are capped (`max_hits`, and
`memory_limit_bytes // 4`); hits of big records are produced incrementally.
Translit (`--translit`): Cyrillic folded to Uzbek Latin (`е` → `ye` at word start/after a
vowel, `ц` → `s`, `ъ` after `ў/ғ/қ` dropped, `сҳ` → `s'h`), then in both needle and haystack
every `ts` is merged into `s` and an apostrophe between `s` and `h` is ignored — a fuzzy
mode that makes `tsirk`/`sirk` ~ `цирк`, `militsiya`/`milisiya` ~ `милиция`.

## 8. Writer (`writer.py`)

```python
class ExportOptions:  # dataclass
    fmt: str                 # json|jsonl|csv|xlsx|sqlite|parquet|feather|arrow
    indent: int|str|None=2; sort_keys: bool|str=False   # True == "alpha" (uz collation) | "codepoint"
    keep_wrapper=True
    columns: list[str]|None=None; array_mode="join"|"rows"; explode: str|None=None
    join_sep=";"; csv_delimiter=","; csv_bom=True
    xlsx_max_rows=1048576; xlsx_split="sheets"|"files"
    parquet_compression="zstd"|"snappy"|"gzip"|"none"   # stage 7, writer_parquet.py
    feather_compression="zstd"|"lz4"|"none"             # Feather / Arrow IPC (same module)
    as_keyed: bool = False   # JSON only (else UserInputError writer.invalid_option): one keyed object from {"k": v} records
def discover_columns(doc, source=None, *, sample=None, array_mode="join", explode=None,
                     progress=None, cancel=None) -> list[str]   # sample=None: every record
def export(doc, out_path, opts, source=None, *, progress=None, cancel=None) -> ExportReport
```

* JSON/JSONL: records are re-formatted at token level (never parsed unless `sort_keys`), so
  numbers/strings stay byte-identical and non-ASCII is never `\u`-escaped. JSONL cannot
  hold the keyed/wrapped structure: keyed records become `{"key": value}` lines.
* `as_keyed` (JSON; CLI `--as-keyed`) rebuilds the keyed object from such lines: ONE object whose
  members are the members of the records, in selection order, written through the same
  token-level Reformatter (keys and values never re-encoded; `indent`/`sort_keys` give exactly
  the bytes of the JSON export of the keyed document; no records → `{}`). Every record must be an
  object with exactly one member; any other record (array, scalar, `{}`, two or more members,
  `{"a":1,"a":2}` included) → `ExportError("writer.error.not_single_member", index=<0-based record
  number>, what=<localized: "massiv", "2 a'zoli obyekt", …>, kind, members)` for the first one in
  selection order; nothing is saved. Check: per block one `core.Scanner` (`MODE_CONTAINER`) pass
  over `{` + the records without their braces joined with `,` + `}` — a valid object with one
  member per record exactly when every record has one member (no parsing; with the Rust core it
  runs in a helper thread beside the Reformatter, so the export speed stays); huge records are
  counted while they are streamed. The output root is the keyed object: `keep_wrapper` has no
  effect. Member-record sources (keyed; wrapped with an object container) are written as their
  normal JSON export (no wrapper; no check). Member keys repeated across records are written as
  they are and counted in `writer.warn.duplicate_keys` (exact, escapes decoded) while ≤ 1 000 000
  keys taking ≤ `memory_limit_bytes // 4` bytes are remembered; later keys are not checked (the
  warning then says how many were).
* CSV/XLSX automatic columns come from a full pre-scan of the exported records (no field is
  lost; `--columns` skips the scan). Map-like objects (many distinct, rare keys): keys found
  in ≥ 1 % of the scanned records keep their columns (≤ 1000 per map), the rare tail is
  pooled into one compact-JSON column `<path>.*` with a warning; the record root is never
  collapsed. Explicit columns that matched nothing are named in a warning. Rows mode: the
  plain column at the exploded path is filled only for records whose value there is not an
  array. Huge records: cells built incrementally from the ijson stream with a size cap
  (truncated cells are named in a warning; rows are never dropped).
* XLSX: numbers typed when they have ≤ 15 significant digits within the double range, else exact text + warning;
  strings over 32,767 UTF-16 units truncated + warning; split into sheets/files over `xlsx_max_rows`; written by
  the own streaming writer (`writer_xlsx.py`, sheet XML deflated in worker processes; cancellation checked for
  every block, job result and write).
* SQLite: one optimistic pass in worker processes (`writer_sqlite.py`; two passes — map detection, then rows —
  only when a path may be a map); numbers stored as INTEGER (64-bit) / REAL (when the double holds the same decimal
  value) / TEXT (otherwise, with a warning); ≤ 1500 columns per table plus an overflow JSON column (`_extra`,
  renamed when taken); map-like objects pooled as JSON text; ≤ 200 child tables (overflow as JSON text).
* Every export calls `doc.ensure_unchanged()` right before the atomic commit.

**Parquet (stage 7, as built; `writer_parquet.py` is authoritative).** `fmt="parquet"` is the third table format
(`writer.TABLE_FORMATS = ("csv", "xlsx", "parquet", "feather", "arrow")`): `_prepare_columns`, the column scan, `plan_columns`, the
wrapper notice and the option/format tables of the CLI and the export dialog treat it exactly as CSV/XLSX, so the
three formats of one export share header and rows. `_EXPORTERS["parquet"]` imports the module lazily (pyarrow is
an optional extra `parquet = ["pyarrow>=17"]`; `export()` checks it before any work: `writer.error.parquet_missing`
names `pip install "jsonreshape[parquet]"`). Cells: `_ParquetCells` keeps the decoded scalars (`int`/`JsonInt`,
`float`/`JsonFloat`, `bool`, `str`, `None`) and gives arrays / objects their CSV texts; `_RowsJob` builds such rows
in the worker processes (the XLSX path of `_RowsPool`), the calling thread converts them to Arrow arrays in block
order. Types per column: `int64` (all integers within int64; `-0` → 0), `double` (every value exactly a double —
`writer._exact_double`'s rule — and integers ≤ 2**53 among them), `bool`, else `string` with the exact JSON text of
non-strings; number-only columns that became text are named in `writer.warn.parquet_number_text`; an all-null
column is `string`. Decision: the first `TYPE_ROWS` = 65 536 rows (bounded by `2 × cell budget` cells, at least
1024) are buffered, the schema is fixed, writing starts; a later misfit raises `_Restart`: the attempt's temporary
file, counters and warnings are dropped, a types-only pass over every row (same row machinery, workers included)
decides the schema, a second pass writes it (`writer.warn.parquet_restarted` with column and record number;
progress total grows by two passes). The prefix rule is monotone (a string-forcing mix stays string; an all-null
prefix is provisional and any later non-string value restarts), so the final schema is always the all-rows
decision and the file is a function of the rows alone. Writing: `pq.ParquetWriter` on a cancel-checking file
wrapper inside `atomic_path`, compression from `ExportOptions.parquet_compression`; Arrow record batches are
accumulated and a row group is written every `G` rows, `G` = ~64 MiB estimated from the first rows (1024 …
1 048 576), each group `combine_chunks()`-ed into one contiguous chunk (Arrow's page splitting depends on the
chunks it is given) — hence byte-identical files for `workers=1` and `workers=N`; only > 512 MiB of pending batches
cuts a group early (memory bound; data unchanged). Lone surrogates become the text `\ud800` (as CSV/XLSX).

**Feather / Arrow IPC (as built).** `fmt="feather"` and `fmt="arrow"` (`.feather` / `.arrow`) are one format under
two names -- the Arrow IPC file format, which Feather v2 is -- and both map to `_EXPORTERS` → `export_feather`. It is
the Parquet engine above, not a copy: `_Collector` takes a write backend instead of `pyarrow.parquet`
(`_ParquetBackend.open` = `ParquetWriter(..., compression=parquet_compression)`, `write_group` =
`write_table(part, row_group_size=G)`; `_ArrowBackend.open` = `pyarrow.ipc.new_file(sink, schema,
IpcWriteOptions(compression=zstd|lz4|None))`, `write_group` = `write_table(part)` -- one record batch per row group,
so the batch boundaries are the data-determined row groups and `workers=1` / `workers=N` stay byte-identical), and
`_export_columnar(ctx, out, fmt)` takes a small `_Fmt` descriptor with the backend factory and the message keys
(`writer.*.parquet*` / `writer.*.feather*`). The Parquet bytes did not change with the refactor. `export()` checks
`require_pyarrow_ipc()` before any work (`writer.error.feather_missing`, the same `jsonreshape[parquet]` extra);
`.feather.gz` / `.arrow.gz` are refused (`writer.error.gzip_format`). The export dialog lists both names, because
`writer.format_for_path` returns either for a typed name and the dialog looks the format up in its own list.

**XLSX and SQLite (stage 7, as built; `writer_xlsx.py` and `writer_sqlite.py` are authoritative).** *XLSX*:
`writer._export_xlsx` calls `writer_xlsx.export_xlsx`, jsonreshape's own streaming SpreadsheetML writer (it replaced
`xlsxwriter`). Rows come from `_RowsPool` exactly as for CSV (the CSV worker rule; `workers=1` runs the same jobs in
the calling thread through `_NoPool`): a `_RowsJob` with `fmt="xlsx"` builds the typed cells of `_XlsxCells`, turns
them into `<row>` XML (`_RowXml`: inline strings, no `r` attributes, blanks inside a row as `<c/>`, xlsxwriter's
escaping — `_xHHHH_`, `_x005F_`, `U+FFFE/FFFF`, entities, `xml:space="preserve"` — and its `%.16G` numbers) and
deflates it as one raw deflate piece ended with `Z_SYNC_FLUSH`, returning `(bytes, crc32, raw length, rows,
truncated texts)`. The calling thread (`_XlsxBook`) appends pieces to the current sheet member of `_ZipWriter`
(own zip writer: local header patched with the CRC — `crc32_combine` — and sizes when the sheet ends; a final empty
deflate block closes the stream), cuts a piece that straddles `xlsx_max_rows` (inflate, cut at the `<row>`,
deflate both parts), opens new sheets or staged `files` parts, and writes the small parts (workbook, rels, styles
with a bold style 1 for the header, docProps, content types) at the end; the central directory gets zip64 records
when an offset or the entry count needs them; a member reaching `MEMBER_LIMIT` (4 GiB) raises
`writer.error.xlsx_too_large`. The workbook is byte-identical for any number of workers (but `docProps/core.xml`);
the cells equal the xlsxwriter workbook's (`tests/test_writer_xlsx_fast.py` keeps the old exporter as the
reference; Excel itself opened the files in the check), except that a text like `<r>…</r>` is a text. No staging
folder; stale `xlsx-<pid>-<hex>` folders of older versions are still purged. *SQLite*: `writer._export_sqlite`
calls `writer_sqlite.export_sqlite`: one optimistic pass whose jobs (`_SqliteJob`, worker processes by the CSV rule)
decode a block, split it with the `_SqliteWriter` row engine — `_LocalRows`, whose object walk `_walk` does exactly
what `_SqliteWriter._fill` does (pre-order, same depth rules, map plans) while counting the keys per path — into
local tables of an in-memory database (created at their first batch of rows: an `ALTER TABLE` reparses the whole
schema), and return the serialized image plus a manifest (array path, parent, leaf paths, rows per local table).
The calling thread (`_SqliteTarget`) merges the key counts (`_MapGuard`: any path with more than `MAP_MIN_KEYS`
keys → the attempt is dropped and redone as in stages 1–6, `_sqlite_scan` then a rows pass with its plans, the
progress total growing by two passes), creates missing tables/columns through the one-thread registry (or redoes
the segment's records with `_SqliteWriter.add` when the table / column caps are reached), loads the image into the
attached `w` — with a new schema cookie per image, because SQLite keeps the parsed schema of an attached database
across `deserialize` when the cookies match — and copies each table with one `INSERT INTO main.t (…) SELECT c0 +
offset, c1 + parent offset, … FROM w.tN`. Records with many brackets, blocks needing more columns or child tables
than allowed, failed jobs and broken pools fall back to the one-thread code in place. Database: page size 32 KiB,
journal off, 16 MiB cache while appending, 64 MiB for the `_parent` indexes at the end (the workers have exited).
The result is value for value the database of `writer._export_sqlite_onethread` (kept: the tests' reference and the
fallback without `Connection.serialize`; `tests/test_writer_sqlite_pool.py`). Measured on `bench/data/syn_2g.json`
(2.1 GB, 4 workers): XLSX 116 MB/s (RSS 250 MB), SQLite 79 MB/s (RSS 270 MB), CSV unchanged (`bench/results/export_latest.md`).
Measured on `bench/data/syn_2g.json` (2.1 GB, 6.5 M records): see "Measured speed" below.

## 9. Filter language

```
expr     := or
or       := and (("or" | "||") and)*
and      := not (("and" | "&&") not)*
not      := ("not" | "!") not | cmp
cmp      := operand ( cmpop operand
                    | "between" operand "and" operand
                    | ["not"] "in" operand
                    | "is" ["not"] ("null" | "empty" | "missing")
                    | ("contains" | "startswith" | "endswith" | "matches" | "~") operand )?
cmpop    := "==" | "=" | "!=" | "≠" | "<" | "<=" | "≤" | ">" | ">=" | "≥"
operand  := literal | list | call | path | "(" expr ")"
literal  := string ("…" or '…', JSON escapes) | number (exact) | true | false | null
list     := "[" [expr ("," expr)*] "]"
call     := name "(" [expr ("," expr)*] ")"   # len exists missing empty lower upper trim type number string
path     := path syntax of §3 inside expressions; `$` = record value, `$key` = keyed record key, `$index` = record number;
            keys colliding with keywords or containing spaces: `["and"]`, `` `my key` ``
```

Semantics: a path yields the list of values it resolves to (empty when missing;
wildcards give several). `a OP b` (==, <, <=, >, >=, contains, startswith, endswith,
matches, in, between) is true iff **some** value of `a` satisfies it; `a != b` ≡
`not (a == b)`; `is not X` ≡ `not (is X)`. Equality: numbers by their exact decimal
value (`1 == 1.0`, `1.10 == 1.1`, `1e-400 != 0`, `1e400 < 2e400`, big ints exact — the same
order as `search --number` and the sort), strings exactly, containers deeply, no type
coercion. `between` with reversed bounds swaps them (builder too).
Ordering: number–number or string–string only, else false. `contains`: substring for
strings, element for arrays, key for objects. `is null`: present and null. `is empty`:
present and `""`/`[]`/`{}`. `is missing` / `missing(p)`: no value. `exists(p)`: ≥1 value.
`len(p)`: length of the first value (string chars, array/object size). A bare operand
used as a condition is truthy when it has a value that is not false/null/""/0/[]/{}.
`matches` uses Python `re.search`. Builder operators map 1:1:
`eq ne contains startswith regex gt lt ge le between exists not_exists is_null is_empty in
len_eq len_ne len_gt len_lt len_ge len_le`.

## 10. CLI

```
jsonreshape inspect  INPUT [--sample N] [--json]
jsonreshape search   INPUT TEXT [--keys|--values] [--regex|--number] [--case-sensitive] [--whole-word]
                     [--translit] [--path P] [--limit N] [--count] [--max-hits N] [--export FILE.csv|FILE.json]
jsonreshape filter   INPUT OUTPUT (--where EXPR | --jmespath EXPR | --preset NAME) [--save-preset NAME] [output options]
jsonreshape sort     INPUT OUTPUT --by FIELD [--by -FIELD ...] [--nulls first|last] [--collation uz|simple|unicode]
                     [--no-natural] [--case-sensitive] [--memory-limit SIZE] [output options]
jsonreshape convert  INPUT OUTPUT.(json|jsonl|csv|xlsx|sqlite) [--where EXPR | --jmespath EXPR | --preset NAME]
                     [--save-preset NAME] [--sort-by FIELD ...] [sort options] [output options]
jsonreshape presets  [--delete NAME]
output options: [--to FMT] [--jsonl] [--compact] [--indent 2|4|tab] [--sort-keys [alpha|codepoint]] [--no-wrapper]
                [--as-keyed] [--columns a,b.c] [--array-mode join|rows] [--explode PATH] [--sep ";"] [--delimiter ","]
                [--no-bom] [--xlsx-split sheets|files]
common: [--record-path P] [--format auto|array|jsonl|brace_list|wrapped|keyed|single|stream]
        [--allow-partial] [--no-cache] [--backend auto|rust|python] [--lang uz|en] [-q] [-v|-vv] [--version]
```

`--as-keyed` (`ExportOptions.as_keyed`; `jsonreshape convert keyed.jsonl keyed.json --as-keyed`) works
for JSON output only: with another output format it is a user error (exit 1,
`cli.error.as_keyed_format`), not an option "ignored with a warning" like the others; a record
that is not a one-member object is a user error too (exit 1, nothing saved).
`--memory-limit`: a number without a unit means MB; minimum 16 MiB; capped at half of
`memory_limit_bytes`. `search` without `--export/--count` prints hits while the index is
still being built. Warnings collected during a command (e.g. "the file grew after it was
opened", "peak memory exceeded the memory limit") are printed once at the end.

Exit codes: 0 ok; 1 usage/user error (incl. a record path that does not exist or is not a
container, `--record-path` on JSON Lines/stream input, OUTPUT == INPUT); 2 JSON error in
the input, or the input was changed by another program while it was read (nothing saved);
3 cancelled (Ctrl+C / Ctrl+Break); 4 internal error (message + log path).

**Standard input (stage 7, as built; `jsonreshape/stdin_input.py` and the "Standard input" decision in the `cli`
docstring are authoritative).** An INPUT given as `-` — and only `-` — is standard input: one INPUT per command
(`inspect` … `import`, `patch` with an OUTPUT), at most one of the inputs of `merge` / `diff`. `cli._stdin_inputs`, a
context manager around every command in `_run`, first refuses `-` as any output (`OUTPUT_DESTS`:
`cli.error.stdout_output`), two `-` (`cli.error.stdin_twice`), a terminal or missing stdin (`stdin_is_tty`:
`cli.error.stdin_tty`), `patch --in-place -`, `split -` without `-o DIR` (the parts would land in the work folder)
and `import -` without `--from` (no file name tells the type); then `stdin_input.spool_stdin` copies
`sys.stdin.buffer` (binary: no newline translation on Windows) in 1 MiB pieces (`read1`, constant memory) to
`tempfile.mkstemp("stdin-<pid>-", ".json")` in `appdirs.work_dir(settings.cache_dir)` (mode 0600 where modes exist),
with a `Progress` in bytes and no total (the CLI line shows bytes read and the rate), `cancel.check()` per piece and
the free-space rule (at 0 and at every 256 MiB: `fileio.free_space(folder) >= bytes read + 64 MiB`, else
`fileio.error.disk_full`); every failure removes the partial file. The `-` in `args` is then replaced by the spool
path, so `_check_input`, the same-file check of `_check_output` (an output may not be the spool file),
`Document.open` with `--format` / `--record-path` (gzip by its magic bytes through `prepare_data_file`),
`repair_file`, `import_file` and the messages that name the input work unchanged on a named file; one info line
(`cli.stdin.summary`, hidden by `-q`) tells the bytes read and the path. The spool file is removed in a `finally`
after the command (retried briefly on Windows while a scanner holds it); its index and prepared copy stay in the
cache for the LRU eviction like those of any deleted file (the spool path is new every run, so they are never
reused). A `-` for PATCH, MAPPING or `--schema` reaches `_check_input` unspooled and is
`cli.error.stdin_unsupported`. Measured: a 50 MB pipe peaks at ~90 MB for the whole process tree, as the file run
does (+2.5 MB of read/write buffers); `tests/test_cli_stdin.py` (25 tests) pins the byte-identical result, the
refusals, the clean-up after success / JSON error / cancellation, the free-space rule and the memory bound. Shell
note (verified): cmd.exe and bash pipes and `<` redirections carry bytes; Windows PowerShell 5.1 re-encodes
pipelines as text (gzip and non-ASCII data break: 48 bytes of Cyrillic JSON arrive as 53) and has no `<`.

---------------------------------------------------------------------------------

## 11. GUI (`jsonreshape/gui/`, PySide6) — stage 2: viewer

AGENT_PROMPT §4–§6, §11 (viewer parts). Editing (stage 3), the mapping designer (stage 4),
statistics/diff tools (stage 5), command palette / key settings / session restore / workspace /
PyInstaller (stage 6) are out of scope; leave clean extension points, no dead UI for them.

### 11.0 Rules

* **The GUI thread never blocks.** It never reads more than ~64 KiB of the data file, never
  parses a record larger than 1 MiB, never waits on a `Document` lock held by indexing and
  never calls `Node.child_count()` / `children()` / `find_child()` / `doc.node()` on a node
  whose children are not indexed yet. Everything else runs in `qtasks.TaskManager` (long,
  visible in the tasks panel, cancellable) or a `loader.RowLoader` (short reads for visible
  rows, invisible). Worker threads never touch widgets; they hand results over through
  `TaskContext.post()` / Qt signals emitted on bridge `QObject`s living in the GUI thread.
* Every user-visible text goes through `t()`; sections `gui.<module>` in `i18n.py`
  (key prefix = section name, e.g. `gui.tree.col.key`).
* Record numbers are shown 0-based (`#0` = first record; user decision 2026-10-17).
* Offline: never import `PySide6.QtNetwork`, `QtWebEngine*`, `QtWebSockets`, `QtHttpServer`
  (test_static checks it). No `QDesktopServices.openUrl` on non-`file:` URLs.
* All lists/tables/trees with unbounded row counts are custom virtual views
  (`vgrid.VirtualGrid`): Qt item views keep per-row state (`QHeaderView` sections,
  `QTreeView` view items) and their scroll ranges are `int`, so they cannot show 100 M rows.
  Small lists (≤ 10 000 items: schema fields, presets, bookmarks, tasks) may use normal Qt
  widgets.
* Memory: every GUI cache is bounded (row caches ≤ 4096 rows, record cache ≤ 64 MiB, pretty
  text ≤ 4 MiB (≈ 80 MB of QTextDocument), raw pages ≤ 1 MiB). The GUI adds < 150 MiB to the core's budgets.
* Windows / High-DPI: Qt 6 scales automatically; geometry in device-independent pixels, row
  height from font metrics. Paths shown with native separators.
* Tests: `tests/test_gui_*.py`, `pytestmark = pytest.mark.gui`, pytest-qt `qtbot`,
  `QT_QPA_PLATFORM=offscreen` (set in conftest). Pure logic (flattree, copyfmt, ...) is also
  tested without Qt. GUI modules must be importable without a display.

### 11.1 Modules

```
gui/__init__.py       require_qt(): friendly UserInputError when PySide6 is missing
gui/__main__.py       python -m jsonreshape.gui -> app.main()
gui/app.py            main(argv): QApplication, theme, logging, excepthook, MainWindow, open argv files
gui/theme.py          color roles (light/dark), type colors, badge glyphs, fonts, apply_theme()
gui/vgrid.py          VirtualGrid (+ GridHeader, Cell, Column, GridSource): virtual list/table/tree widget
gui/flattree.py       FlatTree: visible-row flattening of a lazily expanded tree (pure Python)
gui/loader.py         RowLoader: background fetch + bounded cache for visible rows
gui/qtasks.py         TaskManager / GuiTask / TaskContext: background tasks with progress & cancel
gui/context.py        AppContext: task manager, gui state, settings, error reporting, app-level signals
gui/session.py        DocSession (one open file), NodeRef, FilterSpec, history, view pipeline
gui/guistate.py       GuiState (gui_state.json: recent files, bookmarks, theme, fonts, layouts)
gui/copyfmt.py        copy formats (value: JSON pretty/compact, Python, CSV row; path styles)
gui/actions.py        node context-menu builder shared by the views
gui/widgets.py        ViewBase, InfoBar, ErrorDialog (open log), show_error(), small helpers
gui/treeview.py       TreeView (+ breadcrumb, full value dialog, expand operations, node sorting)
gui/quickfind.py      QuickFindBar (Ctrl+F in the loaded part, F3 / Shift+F3)
gui/tableview.py      TableView (+ column chooser, quick filter row, TSV copy)
gui/textview.py       TextView: pretty JSON of the selected node (colors, line numbers, folding, wrap, indent)
gui/rawview.py        RawView: paged raw bytes around an offset/line (+ LineIndex)
gui/schemaview.py     SchemaView: types, presence, examples (sample / full scan)
gui/filterpanel.py    FilterPanel: visual builder + expression line (expr / JMESPath) + presets
gui/sortdialog.py     SortDialog: multi-key sort
gui/searchpanel.py    SearchPanel: full-file search (Ctrl+Shift+F), live results, export, history
gui/exportdialog.py   ExportDialog: JSON/JSONL/CSV/XLSX/SQLite of the view / all / a node
gui/settingsdialog.py SettingsDialog: core Settings + GUI settings
gui/navigation.py     GoToDialog (Ctrl+G), BookmarksDialog, RecordPathDialog, BrokenLinesDialog
gui/doctab.py         DocumentTab (info bars + splitter of two ViewPanes), ViewPane (view selector)
gui/statusbar.py      StatusBar (size, records, format, encoding, path, selection, offset, memory, tasks)
gui/taskspanel.py     TasksPanel dock (progress, rate, ETA, cancel)
gui/mainwindow.py     MainWindow: tabs, menus, toolbar, docks, drag & drop, recent files, shortcuts
```
Entry point: `jsonreshape-gui = "jsonreshape.gui.app:main"` (`[project.gui-scripts]`).

### 11.2 `qtasks.py`

```python
class TaskContext:            # given to the task function (worker thread)
    progress: Progress        # unit "bytes" by default; fn may set_total / set_message
    cancel: CancelToken
    def post(self, item) -> None          # thread-safe; delivered in order to on_item (GUI thread)
class GuiTask(QObject):
    id: int; title: str; kind: str; owner: object | None; silent: bool
    state: str                # "running" | "done" | "failed" | "cancelled"
    result: Any; error: BaseException | None
    def info(self) -> ProgressInfo | None
    def cancel(self) -> None
class TaskManager(QObject):
    task_added = Signal(object); task_updated = Signal(object); task_finished = Signal(object)
    error = Signal(object, object)        # (exception, task) for failed tasks without on_error
    def run(self, fn: Callable[[TaskContext], Any], *, title: str, kind: str = "other", owner=None,
            on_done=None, on_error=None, on_item=None, on_cancel=None, silent: bool = False,
            unit: str = "bytes") -> GuiTask
    def tasks(self, owner=None) -> list[GuiTask]        # running tasks
    def cancel_all(self, owner=None) -> None
    def detach(self, owner) -> None       # cancel + never call back into owner again (tab closed)
    def wait_idle(self, timeout: float) -> bool         # tests / shutdown (processes events)
```
One worker thread per task (`threading.Thread`, daemon). A single 100 ms `QTimer` (running only
while tasks exist) polls: drains posted items (`on_item` in order), emits `task_updated`, and
after the thread ended calls exactly one of `on_done(result)`, `on_error(exc)` (else the
`error` signal), `on_cancel()` — always after the last `on_item`. `Cancelled` is never an
error. `silent` tasks are not listed in the tasks panel (short helper jobs).

### 11.3 `loader.py`

```python
class LoadFailed:  error: BaseException; text: str      # cached value of a failed fetch
class RowLoader(QObject):
    loaded = Signal()                     # coalesced: some requested values became available
    def __init__(self, fetch: Callable[[list, CancelToken], dict], *, capacity: int = 4096,
                 batch: int = 64, max_bytes: int | None = None, sizeof=None, name: str = "", parent=None)
    def get(self, key) -> Any | None      # cached value (LRU touch) or None (and enqueue key)
    def peek(self, key) -> Any | None     # cached value, no enqueue
    def request(self, keys) -> None       # enqueue missing keys; newest requests first
    def prefetch(self, keys) -> None      # lower priority than request()
    def invalidate(self, keys=None) -> None   # forget all/some; results of older requests are dropped
    def pending(self) -> int
    def close(self) -> None
```
One daemon worker per loader. Requests are served newest-first in batches of ≤ `batch`
keys; keys no longer wanted (a newer `request()` replaced the visible window) are dropped
before fetching. `fetch(keys, cancel)` returns `{key: value}` for the keys it could load;
an exception becomes `LoadFailed` for the batch's keys (logged once). Cache bounded by
`capacity` entries and optionally `max_bytes` (`sizeof(value)`).

### 11.4 `flattree.py` (pure Python, no Qt)

Visible rows of a tree whose top level has `top_count` items and whose expanded nodes have
known child counts. Node identity = **key path** `(top_key, c1, c2, ...)`: `top_key`
identifies a top-level item independently of its position (record number, or a string such
as `"wrapper"`), `ci` are child indices in file order. A node may carry a display order
(sorted view of its children).

```python
class RowInfo(NamedTuple): row: int; depth: int; keypath: tuple; pos: int   # pos: display position among siblings
class FlatTree:
    def __init__(self, top_count: int = 0, *, top_key=None, top_pos=None)   # default: identity
    def set_top(self, top_count, *, top_key=None, top_pos=None) -> None     # view changed / grew; expansion kept
    def row_count(self) -> int
    def locate(self, row: int) -> RowInfo                 # O(depth * log k)
    def row_of(self, keypath) -> int | None               # None: an ancestor collapsed or top item not in view
    def expand(self, keypath, child_count: int, order=None) -> int   # rows added
    def collapse(self, keypath) -> int                    # rows removed; the subtree's expansion is remembered
    def is_expanded(self, keypath) -> bool
    def set_order(self, keypath, order) -> None           # display permutation (pos -> child index) or None
    def set_child_count(self, keypath, n) -> None
    def collapse_all(self) -> None; def expanded_keypaths(self) -> list[tuple]
    def parent_row(self, row) -> int | None
```
Collapsing keeps the expanded descendants (re-expanding restores them); `collapse_all`
forgets everything. Expanded top items whose `top_pos(key)` is `None` (not in the current
view) are remembered but contribute no rows — expansion survives filter and sort changes
(AGENT_PROMPT §4.1). Per expanded node: sorted display positions of its expanded children
with prefix sums of their visible subtree sizes; updates O(k) per ancestor, lookups bisect.

### 11.5 `vgrid.py`

```python
@dataclass(slots=True)
class Cell:
    text: str = ""
    role: str | None = None   # theme color role: key string number bool null object array muted error link
    badge: str | None = None  # type badge before the text: object array string number bool null
    depth: int = 0            # tree indent level (tree column only)
    expander: int = 0         # 0 none, 1 collapsed, 2 expanded, 3 loading
    spans: tuple = ()         # (start, end) highlight ranges in text (search / quick find)
    italic: bool = False; bold: bool = False; align: str = "left"   # left | right | center
    tooltip: str | None = None
@dataclass
class Column:
    id: str; title: str; width: int = 120; visible: bool = True; align: str = "left"
    sortable: bool = False; sort_order: int = 0      # 0 none, 1 ascending, -1 descending
    sort_priority: int = 0                           # 1-based when several sort keys
class GridSource(Protocol):
    def row_count(self) -> int
    def cell(self, row: int, column: Column) -> Cell | None    # None: not loaded yet (placeholder painted)
    def rows_shown(self, first: int, last: int) -> None         # visible window changed (request data)
    def row_label(self, row: int) -> str | None                 # optional gutter text ("#12")
class VirtualGrid(QAbstractScrollArea):
    current_changed = Signal(int, int)        # row, column index (-1, -1 when none)
    activated = Signal(int, int)              # double click / Enter
    expander_clicked = Signal(int)            # click on the expander of a tree row
    context_menu_requested = Signal(int, int, QPoint)
    header_clicked = Signal(int, object)      # column index, Qt.KeyboardModifiers
    header_context_menu = Signal(int, QPoint)
    selection_changed = Signal(); columns_changed = Signal(); viewport_changed = Signal()
    def set_source(self, source) -> None; def reset(self) -> None        # new rows (keeps nothing)
    def rows_changed(self, keep_current: bool = True) -> None            # row count changed (live growth)
    def set_columns(self, cols: list[Column]) -> None; def columns(self) -> list[Column]
    def set_tree_column(self, index: int | None) -> None   # column painted with indent/expander/badge
    def set_frozen_columns(self, n: int) -> None           # 0 or 1: first visible column stays while scrolling
    def set_selection_mode(self, mode: str) -> None        # "single" | "rows" | "cells"
    def set_row_labels(self, on: bool) -> None             # gutter with source.row_label()
    def set_current(self, row: int, col: int | None = None, *, select: bool = True, scroll: str = "ensure") -> None
    def current(self) -> tuple[int, int]
    def selected_ranges(self) -> list[tuple[int, int, int, int]]   # (row0, row1, col0, col1) inclusive
    def clear_selection(self) -> None; def select_all(self) -> None
    def scroll_to(self, row: int, where: str = "ensure") -> None    # ensure | center | top
    def visible_rows(self) -> tuple[int, int]
    def row_at(self, y: int) -> int; def column_at(self, x: int) -> int; def cell_rect(self, row, col) -> QRect
    def set_key_handler(self, fn) -> None   # fn(QKeyEvent) -> bool: the owner consumes keys first
    def zoom(self, steps: int) -> None; def set_font(self, font: QFont) -> None
    def set_placeholder_text(self, text: str) -> None      # shown when row_count() == 0
```
Uniform row height (font metrics + padding). Vertical scroll by rows; the scrollbar range is
`rows - page` while it fits in a C `int`, otherwise a scaled 0..2^30 range mapped to rows
(100 M+ rows scroll smoothly; keys/wheel move exact rows). Own header widget (resize by
dragging edges, reorder by dragging, double-click edge = fit visible rows, sort indicators
with priority numbers, context menu signal); frozen first column painted on top while
scrolling horizontally. Alternate row colors, hover, focus rectangle, palette selection
colors, elided text with `…`, highlighted `spans`, tooltips (full text when elided). Keys:
arrows, PgUp/PgDn, Home/End (Ctrl: first/last row), Shift extends, Ctrl+A, Enter →
`activated`; the key handler runs first (tree: →/←/`*`). Ctrl+wheel / Ctrl +/- zoom. The
grid asks the source only for visible rows (`rows_shown` covers the window + 1 page).

### 11.6 `session.py`, `context.py`, `guistate.py`

```python
class NodeRef(NamedTuple):
    record: int | None        # record number (document numbering); None = outside the records
                              # (single-document root, wrapper members of a wrapped document)
    path: tuple = ()          # JSON path relative to the record value (record None: absolute)
    ipath: tuple | None = None    # child indices along path when known (tree identity)
@dataclass
class FilterSpec: text: str; lang: str = "expr"; builder: dict | None = None   # builder = Group.to_dict()
class AppContext(QObject):
    tasks: TaskManager; state: GuiState; settings: Settings (core, get_settings())
    open_requested = Signal(str, dict)     # path, options (record_path, format, ref)
    settings_changed = Signal()
    def report_error(self, exc, parent=None) -> None   # ErrorDialog (message, hint, details, "open log")
class DocSession(QObject):
    state_changed = Signal(str)   # opening | indexing | ready | failed | closed
    opened = Signal(); open_failed = Signal(object); index_finished = Signal()
    count_changed = Signal()      # record count / view length changed (throttled to <= 5/s)
    view_changed = Signal()       # view RecordSet replaced (views reset; expansion is kept)
    view_busy_changed = Signal(bool)
    current_changed = Signal(object, object)   # (NodeRef | None, origin view or None)
    reveal_requested = Signal(object)          # NodeRef the user wants to see (tree expands to it)
    file_changed = Signal(str)                 # "changed" | "grown"
    warnings_changed = Signal()
    def __init__(self, app: AppContext, path, *, record_path=None, format=None, use_cache=True, parent=None)
    path; name; doc: Document | None; state: str; open_error: BaseException | None
    def start(self) -> None                    # open (background) then index (background task, live count)
    def reopen(self, *, record_path=None, format=None, use_cache=True) -> None
    # view = sort(filter(base)); filter = FilterSpec AND quick filters (column -> expression)
    filter: FilterSpec | None; quick_filters: dict[str, str]; sort_keys: list[SortKey]; view_busy: bool
    def view_len(self) -> int; def record_at(self, pos: int) -> int; def view_label(self) -> str
    def view_recordset(self) -> RecordSet      # for export / search source
    def set_filter(self, spec: FilterSpec | None) -> None
    def set_quick_filter(self, column_path: str, expr_text: str | None) -> None
    def set_sort(self, keys: list[SortKey] | None) -> None
    def show_records(self, rs: RecordSet, label: str) -> None   # e.g. search results; filter/sort cleared
    def reset_view(self) -> None
    def position_of(self, record: int, callback) -> None        # async; callback(pos | None) on the GUI thread
    current: NodeRef | None
    def select(self, ref, *, origin=None, history: bool = True, reveal: bool = False) -> None
    def back(self) -> None; def forward(self) -> None; can_back: bool; can_forward: bool
    def absolute_path(self, ref) -> tuple; def ref_for_path(self, abs_path) -> NodeRef
    def ref_for_location(self, loc: Location) -> NodeRef
    def node(self, ref, cancel=None) -> Node                    # blocking: worker threads only
    def resolve(self, ref, cancel=None) -> NodeRef              # blocking: fills ipath
    def filter_by_value(self, ref, value) -> None               # AND `path == value` to the filter
    def sort_by_field(self, path, descending: bool = False) -> None
    bookmarks: list[Bookmark]; def add_bookmark(name, ref); def remove_bookmark(i); def rename_bookmark(i, name)
    def close(self) -> None
```
Open runs `Document.open` in a task, then `build_index` in a task (kind `index`); `opened`
fires as soon as the document exists so the first records show while indexing (the live
`record_count` is polled ≤ 5/s → `count_changed`). Filter: `filter_records(..., on_batch=post)`
→ the growing RecordSet becomes the view at once (partial results visible); a JMESPath main
filter plus quick filters = two passes. Sort runs after the filter finished. A new
filter/sort cancels the running one. `position_of`: base view = identity; other views: scan
of the RecordSet (`bytes.find` with 8-byte alignment) in a silent task, results cached. A
3 s timer runs `doc.check_changed()` in a silent task → `file_changed`. History: up to 200
NodeRefs; `select(history=False)` for programmatic moves. `GuiState` (JSON in
`app_home()/gui_state.json`, atomic write + `query.file_lock`): recent files (≤ 20),
bookmarks per normalized source path, theme (`system|light|dark`), mono font family/size,
UI font size, main window geometry/state, last directories, per-file table column layouts.

### 11.7 Views (`ViewBase` in widgets.py)

```python
class ViewBase(QWidget):
    view_id: str              # "tree" | "table" | "text" | "raw" | "schema"
    def __init__(self, session: DocSession, parent=None)
    def activate(self) -> None            # became visible: load lazily (hidden views do no I/O)
    def zoom(self, steps: int) -> None    # Ctrl +/- (0 = reset)
    def quick_find(self) -> None          # Ctrl+F (no-op where meaningless)
    def find_next(self, backwards: bool = False) -> None    # F3 / Shift+F3
    def copy_value(self, fmt: str = "json") -> None         # json | json_compact | python | csv
    def copy_path(self, style: str = "dotted") -> None      # dotted | jsonpath | pointer | python | js
    def shutdown(self) -> None            # tab closing: stop loaders, detach tasks
```
Views follow `session.current_changed` (ignoring their own `origin`) and set the current node
with `session.select(ref, origin=self)`: tree, table, text and raw stay linked
(AGENT_PROMPT §4.6). The node context menu (`actions.node_menu`) offers: copy value (JSON
pretty/compact, Python, CSV row), copy path (5 styles), copy key, filter by this value, sort
by this field (asc/desc), expand all / collapse all / expand to level 1–9 (tree), full
value, show in tree, open in a new tab.

* **Tree**: top level = the records of the view (`#n`; keyed: the key) — wrapped documents get
  a first row for the wrapper object (its record-array member shows the count and is not
  expanded inline), single documents one root row (expanded). Columns: key (indent guides,
  expander, type badge), value preview (type colored, long text elided; "full value" window
  loads lazily), children, size (bytes; tooltip exact size + offset). Expanding a container
  whose children are not indexed runs a task (progress, cancel) when the node is large
  (> 1 MiB or `is_large`), else in the loader. Expand-all under a node warns above 1000
  rows; expand to level N; collapse all. Breadcrumb (clickable segments) above the grid.
  View-only node sorting: object keys alphabetically (toggle), array elements by a field
  (per node, task). Quick find (Ctrl+F) searches the loaded rows (expanded nodes + cached
  records), highlights matches, F3/Shift+F3 move.
* **Table**: rows = view positions (gutter `#record`), columns = schema fields (default: scalar
  leaves up to depth 3, at most 50) — chooser dialog (search, check, reorder), hide, drag to
  reorder, resize, freeze first column, layout remembered per file. Containers show `[3]` /
  `{5}`; activating one reveals it in the tree. Header click sorts (Shift adds a key) →
  `session.set_sort`. Quick filter row under the header (`text` = contains, `=x`, `!=x`,
  `>5`, `<=5`, `~regex`, `null`, `!null`/`exists`) → `session.set_quick_filter`. Ctrl+C
  copies the selected cells as TSV (Excel).
* **Text**: pretty JSON of the current node (token-level `core.Reformatter`: exact numbers,
  no `\u` escaping), syntax colors, line numbers, folding markers, wrap toggle, indent
  2/4/tab; nodes over 4 MiB of text show the beginning + a notice + "open in raw view".
* **Raw**: pages of the data file (64 KiB) around an offset with an offset gutter, the current
  node's span highlighted; go to offset / go to line (`LineIndex`: newline checkpoints every
  1 MiB, built in a task, cached per document); the JSON error position is reachable from
  the error info bar.
* **Schema**: `infer_schema` on `Settings.sample_size` records (default) or every record
  (button, task); tree of fields: types distribution, presence %, examples, ranges; actions:
  add as table column, filter "exists", sort by, copy path.

### 11.8 Panels, dialogs, window

* **SearchPanel** (dock, Ctrl+Shift+F): text (history combo, `SearchHistory`), scope
  keys/values/both, path, mode plain/regex/number, case sensitive, whole word, translit;
  Search/Cancel; results stream in (`search(..., on_hits=post)`) into a VirtualGrid (record,
  path, where, snippet with highlighted spans) with the running total; activating a hit
  reveals it; "open as record set" → `session.show_records`; export CSV/JSON (`iter_hits` +
  `export_hits` task). Source = the current view.
* **FilterPanel** (dock): builder (groups AND/OR/NOT, condition rows: field combo from the
  schema, operator `op_label`, value/value2 editors), expression line (expr | JMESPath) with
  live syntax check (error position underlined), Apply / Clear, presets (save, load,
  delete). Builder → text always; text → builder when the expression is representable.
* **SortDialog**: key rows (field, asc/desc, nulls first/last, natural, collation
  uz/simple/unicode, case sensitive) → `session.set_sort`.
* **ExportDialog**: format by extension or choice; JSON (indent 2/4/tab/compact, sort keys,
  keep wrapper, rebuild keyed object), JSONL, CSV/XLSX (columns from the table / automatic,
  array mode, explode, separators, BOM, split), SQLite; scope: current view / all records /
  current node; refuses the source file as output; runs `writer.export` as a task and shows
  the report (records, files, warnings).
* **SettingsDialog**: core settings (sample size, cache folder & limit, memory limit, sort
  memory, large node thresholds, export defaults, language) + theme, monospace font, sizes.
* **MainWindow**: tabs of DocumentTabs (close, move), menus File/View/Navigate/Search/Tools/
  Help, toolbar, docks (search, filter, tasks), drag & drop files, recent files, shortcuts
  (Ctrl+O, Ctrl+W, Ctrl+F, Ctrl+Shift+F, F3, Shift+F3, Ctrl+G, Alt+←/→, Ctrl+C,
  Ctrl+Shift+C, Ctrl+ +/-/0, Ctrl+E export, Ctrl+Shift+L filter, F5 reopen, Ctrl+D bookmark — Ctrl+B since stage 3, §12),
  status bar, error dialogs. **DocumentTab**: info bars (JSON error with "go to error",
  broken JSONL lines list, format warnings, record path choice, file changed → reopen) above
  a splitter of two ViewPanes (default: tree | text); each pane chooses its view (tree,
  table, text, raw, schema); one view instance per kind per pane.

### 11.9 Implementation decisions (stage 2, after review)

* **Threads & GIL.** `app.main` installs a GUI-thread-only GC policy (`install_gc_policy`: `gc.disable()`,
  `gc.freeze()`, young collections from a 50 ms GUI timer, rare full collections — also ~3 s after a tab
  closes — deferred while the user is typing/clicking): a cyclic collection triggered in a worker thread
  could destroy Qt objects off their thread (access violation, found by review). GUI tests use the same rule
  (conftest fixture). `sys.setswitchinterval(0.0005)` keeps the GUI responsive while Python-heavy workers run
  (2 JMESPath filters: GUI p95 3.9 s → 11 ms). `TaskManager.run(heavy=True)` (filter, sort, export, full schema
  scan): at most `MAX_HEAVY_TASKS = 2` run at once, the rest wait ("Navbatda…"). Workers wake the GUI thread
  through a queued signal (result latency ~1–5 ms instead of the 100 ms poll); a modal loop in one task's
  callback no longer stalls the others. No Python `QLayout` subclasses or Python `sizeHint` overrides on hot
  layout paths.
* **VirtualGrid** signals carrying rows use `object` (rows may exceed 2^31); the current/selected row stays
  visible without focus (accent outline). `set_header_extra()` reserves space under the header (table quick
  filter row).
* **RowLoader** never evicts keys of the current window for their size (a budget smaller than the visible rows
  would fetch and evict them forever); everything else stays within `max_bytes`.
* **FlatTree** accepts a worker-built `DisplayOrder` (permutation + inverse) in O(1); element sorts use
  `treemodel.stable_argsort` (16 K runs + merges: no GIL hold above a few ms).
* **Session.** After `show_records` (search results) filter/sort run on the shown set; `reopen` with another
  record path/format clears filter, quick filters and sort; `set_quick_filters(mapping)` (one pipeline run);
  `positions_of(records, cb)`; `keep_document_for(task, sets)` keeps the old document and the leased view sets
  alive until an export ends (exports are owned by `exportdialog.ExportRunner`, not by the session, and survive
  F5 / tab close). Search is refused while a filter/sort is still running (the view is not final).
* **Table.** Parsed record values are kept only for records ≤ 16 KiB; bigger records keep their cells only
  (Rust `PathExtractor`), records > 1 MiB are streamed; the cache counts estimated Python memory. Layouts are
  stored per (file, kind, record path) (layout version 2). Ctrl+F = quick find over the loaded cells; the quick
  filter row is under Alt+↓ / its button.
* **Text.** `TEXT_CAP = 4 MiB` of pretty text (a QTextDocument costs ~20× its text); folding removes the folded
  lines from the document (bounded steps), loading is chunked (32 K characters), the last 2 pretty texts per
  session are cached, copy always takes the complete source text, the caret selects the node under it
  (debounced, exact `ipath` for duplicate keys).
* **Raw.** Pages come from a dedicated `PageFetcher` thread (no task latency); rows follow the pane width.
* **Schema.** Records > 1 MiB are walked by the core scanner into the same `SchemaBuilder` (statistics equal to
  parsing them; never parsed whole); the filter panel / sort dialog request fields lazily (never for single
  documents); the schema view fills its rows in timed batches.


---------------------------------------------------------------------------------

## 12. Editing (stage 3)

AGENT_PROMPT §1.4, §1.5, §8, §11 (Enter/F2/Del/Ctrl+Z/Y/Ctrl+S), tests 19–21, §12 ("export with edits ≥ 50 MB/s,
RAM < 300 MB"). Decisions for the stage (README §15): the edit layer lives in
`%LOCALAPPDATA%\jsonreshape\edits\<key>\` (setting `edits_location = "beside"` → `<file>.jredit\` next to the file,
falling back to the app folder with a notice); `.bak` is ON by default (`backup_on_save`); Enter = edit (→/← keep
expand/collapse); the bookmark shortcut moves from Ctrl+D to **Ctrl+B** (Ctrl+D = duplicate, §8.1). This section was
reviewed adversarially before implementation (4 lenses); every "must" below closes a found hole.

### 12.0 Model (read this first)

* **External record numbers are effective positions.** Outside the edit layer a record number is the record's
  position in the *edited* document: `0 .. record_count-1`, dense, effective order. `Block.indices`, RecordSets,
  `$index`, `#n`, `NodeRef.record`, search hits, filter/sort results and writer sources all use positions, so every
  stage-1/2 "base set = identity 0..count-1" assumption stays true for the edited document. After an
  insert/delete/move, holders of positions are remapped with the step's `Remap` (§12.1) or reset (`ChangeSet.reset`).
* **Inside the edit layer records have stable ids (rid).** Base records keep their original index (`0..N-1`,
  `N = base_count`); records created by edits get `N, N+1, …` from a monotonic `next_rid` (never lowered by undo;
  persisted as a high-water mark). The effective order is a **piece table** of rid ranges.
* **Edited content is bytes in append-only segment files** (`seg-00000.dat` for point edits, one `seg-<n>.dat` per
  bulk preview / big patch). They are addressed through a **virtual address space**: offsets `< data_size` are the
  data file, offsets `≥ EDIT_BASE = 1 << 48` are segments (`EDIT_BASE + (seg << 40) + offset`; segment numbers are
  never reused, so virtual offsets are unique and can key caches). An edited record has a normal span (a *virtual
  span*), so every code path that reads spans through the document (`_read`, `open_data()`, Node, stream.py, schema
  walker, writer chunks) works on it.
* **Record content = the whole record.** A point edit produces the record's new bytes by a **byte splice** of its
  current bytes (everything else byte-identical; the new value formatted like its surroundings) and stores them as one
  *unit*. A record is **huge** when its content span (in either space) is longer than `large_node_bytes` (50 MiB):
  huge records cannot be edited (`EditError("edits.error.huge_record")`), no producer ever writes a unit above the
  limit (bulk operations count `skipped_huge`; paste/insert/set/patch raise), and huge records are yielded as huge
  blocks with their span (virtual or base). Single documents and wrapped wrappers over the limit are therefore
  read-only (documented limitation; the bulk dialog says so before Preview).
* **Content layers** map `rid → virtual span`, newest last: dict layers (point edits; the top layer is always a dict
  layer) and array layers (file-backed, sorted; produced by bulk operations, big selections and big patches). A rid's
  content is the span of the newest layer that has it, else (base rid) its original file span.
* **Steps.** Every user action is one *step* (one undo unit) with a symmetric delta. Units are appended to a segment,
  validated with the core Scanner, fsynced, then the step is committed to SQLite (`layer.sqlite`, WAL,
  `synchronous=FULL`, `locking_mode=EXCLUSIVE`): this is the continuous autosave. After a crash the state is rebuilt
  by replaying the steps from the last checkpoint.
* **States are immutable and identified.** `OverlayState.gen` is unique in the process and increasing (a new state
  for every apply, undo, redo, restore, discard); `OverlayState.version` is the persisted head seq (used only for
  persistence and `dirty`). Everything that must not mix states (pending commands, bulk previews, view sets, GUI
  caches) compares `gen`.
* **Snapshots.** `doc.pinned()` ALWAYS returns a read-only view bound to one state (also when there is no overlay
  yet, so a task started before the first edit never sees it mid-run). Long-running readers use a pinned document;
  callers that capture positions pin **when they capture them** (`DocSession.snapshot()`).
* **Editing preconditions.** Index complete; no JSON error (partial documents are read-only; the repair tool is
  stage 5); the layer lock is held (taken when the session opens, §12.2); the source unchanged on disk. All 7 kinds
  are editable (`brace_list` is saved as a JSON array, §12.7). Single documents: the one record is the root (no
  record insert/delete). Wrapped documents: wrapper members outside the record container are editable; the record
  container, its ancestors and its place (the "hole", §12.5) are protected.
* **Bulk rules** are steps whose label/params describe the rule. Their result is **computed at preview time** (the
  pass that yields the exact count and the 5 examples) into the preview's own segment and kept as an array layer on
  Apply (discarded with its segment otherwise). This keeps the pre-count exactly equal to the applied change (test 20)
  and lets views/search/filter see the result at full speed. Deviation from AP §8.3 wording ("rules applied while
  saving"; "point edits in the SQLite sidecar") — documented in README §15 with its disk cost (≈ size of the changed
  records per applied rule, checked against free space before and during the preview).

### 12.1 `overlay.py` — immutable edit state

```python
EDIT_BASE = 1 << 48; SEG_SHIFT = 40
def is_edit_offset(offset: int) -> bool                 # offset >= EDIT_BASE (NOT Node.is_virtual, which is the jsonl root)
def edit_offset(seg: int, off: int) -> int; def split_edit_offset(v: int) -> tuple[int, int]   # (seg, off)

class PieceTable:                                       # immutable, persistent: positions -> rids
    # pieces = rid ranges [lo, hi); stored in chunks of <= 4096 pieces with per-chunk counts and a chunk prefix-sum
    # array; operations copy the chunk list and rebuild only the touched chunks (O(P/4096 + 4096)); adjacent
    # contiguous pieces are merged.
    @classmethod
    def identity(cls, n: int) -> PieceTable
    @classmethod
    def from_pairs(cls, lo: array, hi: array) -> PieceTable
    count: int; npieces: int
    def rid_at(self, pos: int) -> int                   # IndexError outside 0..count-1
    def rids(self, pos: int, n: int) -> array           # array('Q'), clipped
    def rids_at(self, positions: array) -> array        # identity fast path when npieces <= 1
    def pos_of(self, rid: int) -> int | None            # lazily built rid-sorted index, cached per table (O(P log P) once)
    def runs(self, pos0: int = 0, pos1: int | None = None) -> Iterator[tuple[int, int, int]]   # (pos, rid_lo, n)
    def insert(self, pos: int, rid_lo: int, n: int) -> PieceTable           # 0 <= pos <= count
    def delete(self, pos: int, n: int) -> PieceTable
    def delete_positions(self, positions: array) -> PieceTable            # ascending, unique
    def move(self, positions: array, to: int) -> PieceTable
        # takes the records at `positions` (ascending, unique) out and re-inserts them in their relative order so that
        # the first one ends at `to`, where `to` counts positions of the table WITHOUT the moved records
        # (0 <= to <= count - len(positions)). The GUI/session convert "drop before row r" (pre-step coordinates)
        # to this with Remap(("dels", positions)).map(r).
    def apply(self, remap: Remap, *, new_rids: Sequence[tuple[int, int, int]] = ()) -> PieceTable
        # replays structural Remap ops; "ins"/"inss" take their rids from `new_rids` (pos, rid_lo, n) in op order
    def to_bytes(self) -> bytes; @classmethod from_bytes(cls, b: bytes) -> PieceTable   # u64 LE (lo, hi) pairs
    def __eq__(self, other) -> bool

class DictLayer:                                        # immutable, persistent mapping rid -> (vstart, vend)
    # buckets keyed by rid >> 12 (small dicts); with_changes copies only the touched buckets
    def get(self, rid) -> tuple[int, int] | None; def __contains__(self, rid) -> bool; def __len__(self) -> int
    def keys_in(self, lo: int, hi: int) -> array        # ascending rids in [lo, hi) as array('Q')
    def with_changes(self, changes: Mapping[int, tuple[int, int] | None]) -> DictLayer   # None removes
    def items(self) -> Iterator[tuple[int, tuple[int, int]]]
class ArrayLayer:                                       # immutable, file-backed (mmap through an open_shared handle)
    @classmethod
    def open(cls, path: Path) -> ArrayLayer             # validates header, count vs size and crc32 -> EditError damaged
    name: str; count: int; flags: int; segments: tuple[int, ...]
    def get(self, rid) -> tuple[int, int] | None        # bisect on the mapped rid array (no RAM bitmap)
    def __contains__(self, rid) -> bool
    def keys_in(self, lo, hi) -> array                  # slice of the mapped array
    def spans_in(self, lo, hi) -> tuple[array, array]   # (rids, flat start/end pairs) slices
    def close(self) -> None
def write_array_layer(path: Path, entries: Iterable[tuple[int, int, int]], *, flags: int, segments, work_dir) -> int
    # entries (rid, vstart, vend) in ANY order -> sorted file: naturally sorted runs are merged with heapq.merge,
    # spilled to temp run files beyond 4 M entries; written as <path>.tmp, fsynced, renamed. File: header
    # b"JRLAYER\0" + u32 version + u32 flags + u64 count + u32 crc32(of the arrays) + segment list, padded to 64 B,
    # then count u64 rids, then count u64 (vstart, vend) pairs. Returns the count.

class DatReader:                                        # thread-safe reads of the layer's segments
    def __init__(self, dir: Path)
    def read(self, vstart: int, vend: int) -> bytes     # per-thread open_shared handles; a span beyond the published
                                                        # end of its segment -> EditError("edits.error.layer_corrupt")
    def open_segment(self, seg: int) -> BinaryIO
    def publish(self, seg: int, end: int) -> None       # written size made visible before any state references it
    def close(self) -> None

@dataclass(frozen=True)
class OverlayState:
    base_count: int
    pieces: PieceTable
    layers: tuple                                       # (DictLayer | ArrayLayer, ...), oldest first, last = DictLayer
    wrapper: tuple[int, int] | None = None              # wrapped docs: virtual span of the edited wrapper text W'
    wrapper_hole: tuple[int, int] | None = None         # virtual span of the record-container placeholder inside W'
    flags: int = 0                                      # FLAG_* bits of all edited content (OR'ed into doc.flags)
    next_rid: int = 0
    version: int = 0                                    # persisted head seq (0 = no step)
    gen: int = 0                                        # process-unique, increasing; set by the factory functions
    reader: DatReader | None = None
    count: int                                          # property = pieces.count
    def rid_at(self, pos) -> int; def rids(self, pos, n) -> array; def rids_at(self, positions) -> array
    def content(self, rid: int) -> tuple[int, int] | None   # newest layer's span, None = base content
    def runs(self, pos0: int = 0, pos1: int | None = None) -> Iterator[tuple[int, int, int, bool]]
        # (pos, rid_lo, n, pure), n <= 65536: maximal runs of consecutive rids that are either all PURE (base rids that
        # no layer overrides: raw file ranges may be copied) or all IMPURE (overridden or new rids). Cost
        # O(runs + overrides in range): one keys_in() per layer per window, never O(records) for pure stretches
    def contents(self, rid_lo: int, n: int) -> array    # flat (start, end) array('Q') of an IMPURE run
    def read(self, vstart: int, vend: int) -> bytes
    @property
    def edited(self) -> bool                            # version > 0 (dirty); also true after a restore
    @classmethod
    def initial(cls, base_count: int, reader: DatReader | None) -> OverlayState
def next_gen() -> int                                   # itertools.count based, thread-safe
```

```python
class Remap:                                            # old position -> new position (records, or children)
    # ops applied in order, each in the coordinates left by the previous one:
    #   ("ins", pos, n)      n new items at pos
    #   ("del", pos, n)      n items removed at pos
    #   ("inss", positions)  new items at these ascending NEW positions (array('Q'))
    #   ("dels", positions)  items at these ascending OLD positions removed
    #   ("move", positions, to)   PieceTable.move semantics (positions ascending old, `to` without them)
    #   ("unmove", to, positions) inverse of "move"
    #   ("pieces", old: PieceTable, new: PieceTable)  general: map(p) = new.pos_of(old.rid_at(p)) (big patches, reset-free)
    # map() is O(log k) per op; map_array() merges an ascending input in O(n + k); arrays > 4096 entries are stored
    # in a segment by the store (to_json writes {"blob": [vstart, vend]}).
    def __init__(self, ops: Sequence[tuple] = ())
    ops: tuple; identity: bool
    def map(self, pos: int) -> int | None               # None: deleted (new items have no preimage)
    def map_array(self, positions: array) -> array      # order kept, deleted dropped
    def map_chunks(self, chunks: Iterable[array], cancel=None) -> Iterator[array]   # streaming (RecordSets)
    def inverse(self) -> Remap
    def then(self, other: Remap) -> Remap
    def delta(self) -> int                              # count after - count before
@dataclass(frozen=True)
class NodeChange:                                       # children of one container changed structure
    record: int | None                                  # record position AFTER the step; None = wrapper
    ipath: tuple[int, ...]                              # child-index path of the container AFTER the step (() = root)
    remap: Remap                                        # old child index -> new child index
    count: int                                          # child count after the step
    # A ChangeSet lists NodeChanges ancestors first; holders apply the record remap, then the NodeChanges in list order
    # (each ipath is valid after the previous entries). The inverse is the reversed list with inverse remaps, the
    # same ipaths, and count - remap.delta().
@dataclass(frozen=True)
class ChangeSet:
    state: OverlayState
    kind: str            # "edit" | "undo" | "redo" | "bulk" | "patch" | "restore" | "discard"
    label: str           # localized step label ("Qiymat o'zgartirildi"); "" for restore/discard
    records: frozenset[int] | None    # positions (after) whose CONTENT changed; None = potentially every record
                                      # (always None when > 10 000 records changed)
    remap: Remap | None               # record positions moved (insert/delete/move); None = none
    nodes: tuple[NodeChange, ...] = ()
    wrapper: bool = False             # wrapper text changed
    reset: bool = False               # restore/discard/import of a layer: positions are NOT remappable; holders drop
                                      # every position-based state (views -> base view, current/history cleared)
    select: tuple | None = None       # (record, path, ipath) to select after the step, or None
    gen: int                          # property = state.gen
```
`ChangeSet.records` never lists positions that only moved (covered by `remap`).

### 12.2 `editstore.py` — the layer on disk

```python
FORMAT_VERSION = 1
def layer_key(doc) -> str               # sha256 hex[:32] of (identity.digest(), kind, record_path text, base_count)
def layer_dir(doc, *, settings=None) -> Path    # appdata: appdirs.edits_dir()/<key>; beside: <src dir>/<name>.jredit
                                        # (beside not writable -> appdata + warning "editstore.warn.beside_fallback")
def lock_path(doc) -> Path              # appdirs.edits_dir()/locks/<key>.lock (never inside a beside folder)
@dataclass
class PendingInfo:
    dir: Path; key: str; steps: int; created: float; updated: float; label: str   # last step label (localized)
    matches: bool                       # False: a layer of an OLDER version of this file (identity differs)
    locked: bool                        # held by another tab/process right now (non-blocking probe) -> info only
    damaged: str | None                 # reason when the layer cannot be loaded -> only Delete / Export offered
    saving: dict | None                 # an interrupted save journal (§12.7) -> roll forward/back is offered first
    source: str
def find_pending(doc, *, settings=None) -> list[PendingInfo]
    # both locations; layers with head >= 1 only; tombstoned layers are purged silently; matching layer first, then
    # stale ones of the same normalized source path (edits_dir()/index.json, written under query.file_lock)
class LayerLock:                        # msvcrt.locking on lock_path; process-wide registry of held keys; acquire()
    ...                                 # retries for 2 s (a key held by THIS process: waits for its release)
class EditStore:
    @classmethod
    def open(cls, doc, *, settings=None, create: bool = True, dir: Path | None = None, lock: LayerLock | None = None,
             read_only: bool = False, allow_stale: bool = False) -> EditStore | None
        # create=False and no layer -> None. The SQLite file and segments are created lazily at the first commit (the
        # lock alone never creates a .jredit folder). Meta mismatch (identity/kind/base_count) ->
        # EditError("editstore.error.stale") unless allow_stale (read-only export of an old layer).
    dir: Path; key: str; base_count: int; head: int; reader: DatReader; read_only: bool
    def append(self, data: bytes, *, seg: int = 0) -> tuple[int, int]
        # unbuffered fd, seek to the published end, write loop; on OSError the segment is truncated back and the error
        # re-raised (disk full -> "editstore.error.disk_full"); returns the virtual span (published after sync)
    def new_segment(self) -> int                          # for previews / big patches (seg-<n>.dat)
    def drop_segment(self, seg: int) -> None              # discarded previews
    def sync(self, segs=None) -> None                     # fsync segments, then publish their ends
    def commit(self, step: Step) -> int                   # sync(); one transaction: delete steps > head (their private
                                                          # segments/layer files are tombstoned and deleted), insert the
                                                          # step (seq = head + 1), meta head/updated/next_rid; returns seq
    def set_head(self, seq: int) -> None                  # durable before the new state is published (undo/redo)
    def step(self, seq: int) -> Step; def steps(self) -> list[StepInfo]   # StepInfo(seq, ts, kind, label)
    def can_redo(self) -> bool
    def load_state(self) -> OverlayState                  # checkpoint + replay up to head; validates every span ->
                                                          # EditError("editstore.error.damaged", seq) with .last_good
    def new_layer_path(self) -> Path
    def maybe_trim(self, limit: int) -> None              # when steps > limit + 64: replay the oldest 64 forward from the
                                                          # in-memory checkpoint state, write the new checkpoint, delete them
    def journal(self, data: dict | None) -> None          # save journal (§12.7), durable
    def mark_discarded(self) -> None; def discard(self) -> None   # tombstone (meta state="discarded", index.json entry
                                                          # removed) then best-effort delete; leftovers purged later
    def close(self) -> None                               # releases the lock (keeps files); a head-0 layer is deleted
```
Files: `layer.sqlite` (+`-wal`), `seg-*.dat`, `layer-*.bin`, all opened with `fileio.open_shared` (FILE_SHARE_DELETE;
mmaps made from those handles) so a discarded layer can be deleted while a reader still maps it. Layers are never
touched by cache eviction or `purge_stale_files`. The live `Document` owns the readers of the overlay it shows
(`set_overlay` registers `state.reader` and the array layers; `Document.close()` closes them after leases end);
`EditStore.close()` never closes readers that a document still uses.

SQLite: `connect(long_path, isolation_level=None, check_same_thread=False)` + the store's lock; `PRAGMA
locking_mode=EXCLUSIVE; PRAGMA journal_mode=WAL; PRAGMA synchronous=FULL`. Tables: `meta(key TEXT PRIMARY KEY, value
TEXT NOT NULL)` — `format_version, source, identity (JSON), kind, record_path, base_count, created, updated, head,
next_rid, state ("live"|"discarded"), saving (journal JSON or ""), app_version`; `steps(seq INTEGER PRIMARY KEY, ts
REAL NOT NULL, kind TEXT NOT NULL, data TEXT NOT NULL)`; `checkpoint(id INTEGER PRIMARY KEY CHECK (id = 1), seq
INTEGER NOT NULL, data TEXT NOT NULL)`.

```python
@dataclass
class Step:                     # symmetric delta; spans are virtual
    kind: str; label_key: str; label_params: dict
    content: list[tuple[int, tuple | None, tuple | None]]   # (rid, old top-layer entry, new top-layer entry), <= 10 000
    struct: list | None         # forward Remap ops of the record structure + new_rids (pos, rid_lo, n) for "ins"/"inss"
    pieces_blob: tuple | None   # full PieceTable snapshot span, written on every 64th structural step only
    push_layer: str | None      # array layer file pushed on top (then a new empty DictLayer)
    segments: list[int]         # segments created by this step (deleted when the step is truncated as redo)
    wrapper: tuple | None       # (old span|None, new span|None, old hole|None, new hole|None)
    flags: tuple[int, int]      # (old, new) state flags
    nodes: list | None          # forward NodeChange list
    records: list | None        # changed positions after the step (None = many)
    keys: list | None           # member-record docs: [(rid, old key, new key)] for record key changes/deletions
    select: list | None
    seq: int = 0; ts: float = 0.0
def apply_step(state, step, *, layers) -> OverlayState; def undo_step(state, step, *, layers) -> OverlayState
```
`push_layer` steps are undone by popping the array layer and the empty dict layer above it (LIFO guarantees it is
empty). Undo never lowers `next_rid`. Checkpoints hold the full state (piece-table blob span, the layer list with dict
layers as blobs in seg 0, wrapper, flags, next_rid). `undo_limit` (Settings, default 1000, minimum 200) bounds the
undoable steps.

### 12.3 Document with an overlay (`reader.py`)

```python
doc.set_overlay(state: OverlayState | None) -> None    # live document only; registers state.reader/array layers
doc.overlay -> OverlayState | None                      # the state this object reads (pinned: its frozen state)
doc.pinned(state=_CURRENT) -> Document
    # ALWAYS a PinnedDocument view (for a live document; a pinned view with the same state returns itself): a Document
    # subclass made with object.__new__ holding only `_origin` and `_pin` (the frozen state, or None = base file).
    # __getattr__/__setattr__ forward everything else to the origin, so index state, `closed`, caches, counters and
    # growth notes stay shared and live. All overlay-dependent code reads the state once per public call through one
    # accessor (`self._ov()`), which the view overrides. close() of a view is a no-op; views of a closed document
    # raise like the document. Views are cheap (two attributes).
doc.origin -> Document; doc.gen -> int                  # overlay gen (0 = no overlay)
doc.edited -> bool
doc.base_record_count -> int                            # records of the file (index count)
doc.flags -> int                                        # property: base flags | overlay.flags
doc.record_rid(pos) -> tuple[int, bool]                 # (rid, content overridden)
doc.effective_bytes() -> int                            # bytes of the effective records (pure runs + unit sizes)
doc.node_chunks(node, *, chunk: int = 1 << 20, cancel=None) -> Iterator[bytes]
    # the effective JSON text of ANY node: plain nodes stream [start, end) through the virtual reader; composite nodes
    # (role records / ancestor / virtual, `Node.is_composite`) stream wrapper prefix + records joined with the
    # document's record separator + suffix (jsonl/stream virtual root: as a JSON array)
```
With an overlay (live or pinned), every public API works on **positions**:
* `record_count` = state count; `record_span(pos)` = content span or base span; `record_value_span`, `record_bytes`,
  `record`, `record_key`, `is_huge` (span length), `record_node`, `iter_record_bytes`, `iter_records` follow.
* Reads dispatch on the address space (`is_edit_offset`); a read never crosses spaces; sanity-check failures or short
  reads on edit offsets raise `EditError("edits.error.layer_corrupt")` (never `FileChangedError`). `open_data()`
  returns a `VirtualReader` (`seek/tell/read/readinto/close/name/closed`, context manager; `fileno()` raises
  `io.UnsupportedOperation`) that reads the data file (verified handle) or the segments.
* `iter_blocks(source)`: positions in, positions out (`Block.indices` = positions in source order). Blocks are built
  **by size**, not by run: `runs()` is walked in windows of ≤ 65 536 positions; a pure run of ≥ 256 KiB becomes one
  or more contiguous blocks through the existing `_range_blocks` (raw file range incl. separators,
  `contiguous=True`, indices rewritten); shorter pure runs and impure records accumulate into one scattered block up
  to `block_bytes` (base pieces read with `_read_pieces`/`_map_pieces` — never with an edit offset — and unit pieces
  coalesced into one `DatReader` read per neighbouring group; `contiguous=False`, joined with `b"\n"`); huge records
  (by span length, either space) are huge blocks. The handle is verified once per block. Arbitrary sources
  (RecordSets) map positions with `rids_at`; a block is `contiguous=True` only when its records are consecutive
  pure base rids. Progress counts the bytes of the selected records.
* Composite nodes (`is_composite`: role records/ancestor/virtual) on an edited document: `value()`, `raw()`,
  `scalar_text()`, `size` are built from effective content (size guard = `effective_bytes()`); children of the
  records/virtual roles are read per child (kind and key from each child's own bytes, never from one
  `[first.start, last.end)` window). `Node.is_edited` = its span is an edit offset.
* Keyed documents: `_find_record_index` uses the session's `KeyIndex` when one is attached (`doc.key_index`), else
  scans effective keys (last match wins, as before).
* Wrapped documents with an edited wrapper: `wrapper_spans()` returns the virtual prefix `(W'.start, hole.start)` and
  suffix `(hole.end, W'.end)` (brackets excluded as before; W' covers `[root_start, trimmed_end)` only — the original
  outer whitespace stays outside); `root()` is the ancestor node over W'; `_container_bounds()` returns the hole, so
  the record-array child is classified `records` by bounds; `records_node()` keeps its records-role children.
* `ensure_unchanged()` / `check_changed()` check the source file. Node child caches are keyed by `node.start`
  (edit offsets are unique); persisted child indexes are never written for edit-offset nodes. New Node slot
  `mstart: int | None` (member start, `"key"` quote) for object members and `Node.member_span()`.
* Forced format `jsonl`/`stream` on a whitespace-only file opens it with 0 records (needed to reopen a saved empty
  document) instead of `reader.empty_file`.

RecordSets carry `gen: int | None` (the state gen their positions belong to; `None` = valid for any state, e.g. the
base set). Producers (filter, sort, search, find_duplicates, the session's remaps) set it from the pinned document.
Consumers that take a `source` (filter, sort, search, iter_hits, infer_schema, export, discover_columns, bulk preview,
find_duplicates) require `source.gen in (None, doc.gen)` else `EditError("edits.error.stale_scope")`.
`RecordSet.base(doc)` binds the given document (a pinned view gives a frozen count); `_is_base_of(doc)` compares
origins (the base set of the live document means "all records" for any pinned state).

Consumers (small changes, all in this stage): `stream.iter_events`, `search._huge_contains`,
`search.reference_search`, `recordset._KeyCollector._huge_root_key`, `writer._Ctx.chunks`, `schema._RecordWalker.read`
read through `doc.open_data()`. Progress estimates that used `record_span(0)`/`record_span(count-1)` (search
`_source_total`, schema `_records_extent`, writer `_estimate_total`) use `data_size`-based totals when edited.
Entry points pin first: `filter_records`, `sort_records`, `search`, `iter_hits`, `reference_search`, `infer_schema`,
`writer.export`, `discover_columns` begin with `doc = doc.pinned()` (a no-op for an already pinned view).

### 12.4 `editops.py` — byte-level edits and formatting (pure functions)

```python
@dataclass(frozen=True)
class DocStyle:
    newline: bytes            # b"\r\n" iff the first b"\n" in the first 64 KiB is preceded by b"\r", else b"\n"
    record_sep: bytes         # comma kinds: the first record gap if any, else b"," + newline + indent (compact: b",");
                              # jsonl: newline; stream: the first gap if non-empty, else newline
    record_level: int         # indentation level of the record lines (pretty `[\n  {` -> 1; jsonl/stream -> 0)
    unit: str | None          # indent unit inside records ("  ", "    ", "\t"); None = compact
    colon: bytes              # b": " or b":" (first member seen)
    empty: bytes              # record container without records: b"[]" (array, brace_list, wrapped array),
                              # b"{}" (keyed, wrapped object), b"" (jsonl, stream)
def detect_style(doc) -> DocStyle      # reads <= 64 KiB (head, first gaps, first records); cached per document

@dataclass(frozen=True)
class Loc:                    # a node inside a record VALUE buffer (offsets relative to the buffer)
    start: int; end: int      # value span
    mstart: int | None        # member start (key quote) for object members
    kend: int | None          # end of the key token
    key: str | int | None     # member key / element index / None for the root
    kind: str; index: int | None; depth: int
def locate(buf: bytes, path: tuple, ipath: tuple | None = None) -> Loc
    # ipath (complete) selects duplicate keys exactly; else path segments, last duplicate wins;
    # EditError("edits.error.path_not_found")
def child_locs(buf: bytes, container: Loc) -> list[Loc]     # core Scanner(MODE_CONTAINER) over the container
def line_level(buf: bytes, off: int, unit: str | None, base_level: int) -> int
    # indentation level of the line holding `off`; offsets on the buffer's first line get base_level
def check_json(text: str | bytes) -> bytes
    # one JSON value (core Scanner MODE_SINGLE); returns UTF-8 bytes without outer whitespace;
    # EditError("edits.error.invalid_json", line, column, reason) with .char_offset (code points of the ORIGINAL
    # input, so editors can convert to UTF-16 positions), .line, .column
def check_unit(unit: bytes, *, member: bool) -> int
    # validates a unit before commit (value: MODE_SINGLE; member: b"{" + unit + b"}" with MODE_CONTAINER and exactly
    # one record); returns the scanner flags; EditError("edits.error.internal") on failure (a bug: logged)
def encode_string(s: str) -> bytes; def encode_key(s: str) -> bytes   # JSON string tokens (codec rules)
def format_value(value: bytes, level: int, style: DocStyle, *, compact: bool = False) -> bytes
def set_value(buf: bytes, loc: Loc, value: bytes, style: DocStyle, *, base_level: int) -> bytes
def rename_key(buf: bytes, loc: Loc, new_key: str) -> bytes
def insert_child(buf: bytes, container: Loc, index: int, value: bytes, style: DocStyle, *, key: str | None = None,
                 base_level: int) -> bytes
def delete_children(buf: bytes, container: Loc, indices: Sequence[int]) -> bytes
def move_child(buf: bytes, container: Loc, src: int, dst: int, style: DocStyle, *, base_level: int) -> bytes
def convert_value(value: Any, to: str) -> Any                             # ConversionError(EditError)
def format_record(value: bytes, style: DocStyle, *, key: str | None = None, member_head: bytes | None = None) -> bytes
    # a NEW record's unit: value at style.record_level (+ b'"key"' + colon). Point edits of keyed records keep the
    # original member head `[span.start, value_start)` and splice only inside the value (no re-indent of the record)
```
Formatting rules (splices keep every other byte):
* A new/replaced value is formatted at the level of the line where the node starts (`line_level`) when the container
  spans several lines; inside a single-line container it is compact.
* Separators for an inserted child are copied from the container: the bytes between its first two children; with one
  child `b","` + the whitespace between the opening bracket and that child; an empty container in a pretty record
  becomes `{` + newline + indent(level+1) + child + newline + indent(level) + `}`, else `{child}`. The member colon
  comes from the container's own members when it has any, else `style.colon`.
* Delete: sorted unique indices are grouped into maximal runs `[a, b]`; `b < n-1` → remove `[start_a, start_{b+1})`
  (member starts for objects); `b == n-1` and `a > 0` → remove `[end_{a-1}, end_{n-1})`; all children → the container
  becomes empty (`{}`/`[]`). All ranges are computed on the original buffer and the result is built in one pass.
* Move within a container = one rebuild with the container's separators; `dst` is the FINAL index.
* Keys must be unique: insert/rename/paste with an explicit key that exists → `EditError("edits.error.key_exists")`;
  duplicate and move generate free keys `key_2`, `key_3`, … ignoring the moved member itself; moving an array element
  into an object uses its old index as the key (`"3"`, `"3_2"`); moving a member into an array drops its key.

Type conversion (`convert_value`; also the bulk type change; string → bool folds apostrophes with `textmatch.fold`):

| to \ from | string | number | bool | null | object / array |
|---|---|---|---|---|---|
| string | — | exact token text | `"true"`/`"false"` | `""` | compact JSON text |
| number | trimmed text that is a JSON number (exact token kept) else error | — | `1` / `0` | `0` | error |
| bool | `true/false/1/0/yes/no/ha/yo'q` (trimmed, case-insensitive) else error | `0` → false, other → true | — | `false` | error |
| null | `null` | `null` | `null` | — | `null` |
| object | text holding a JSON object, `""` → `{}`, else error | error | error | `{}` | array → error |
| array | text holding a JSON array, `""` → `[]`, else `[value]` | `[value]` | `[value]` | `[]` | object → `[object]` |

### 12.5 `edits.py` — `EditSession` (core, Qt-free, thread-agnostic)

```python
class Ref(NamedTuple):            # positions; core convention: single documents use record=0, the wrapper record=None
    record: int | None; path: tuple = (); ipath: tuple | None = None
class EditSession:
    @classmethod
    def open(cls, doc, *, settings=None, lock: LayerLock | None = None, restore: bool = True) -> EditSession
        # preconditions (§12.0, EditError with the reason key); takes/uses the lock; loads an existing layer when
        # restore=True (a pending layer with restore=False -> EditError("edits.error.pending"))
    doc: Document; state: OverlayState; store: EditStore | None (created at the first commit); style: DocStyle
    auto_apply: bool              # True (CLI/tests): doc.set_overlay after every step; GUI: False (applies in GUI thread)
    key_index: KeyIndex | None    # member-record documents (built lazily, §below)
    dirty: bool; can_undo: bool; can_redo: bool; undo_label: str; redo_label: str; closed: bool
    def history(self) -> list[StepInfo]
    # every command is ONE step and takes expect_gen (the gen the caller's refs belong to; None = current);
    # expect_gen != state.gen -> EditError("edits.error.stale_refs") and nothing changes
    def set_value(self, ref, value, *, expect_gen=None) -> ChangeSet             # value = JSON text
    def set_path(self, ref, value, *, expect_gen=None) -> ChangeSet
        # like set_value but creates missing intermediate OBJECTS along ref.path; a non-object intermediate ->
        # EditError("edits.error.not_object"); used by table cells of missing fields
    def rename_key(self, ref, key, *, expect_gen=None) -> ChangeSet             # object member or keyed record (path ())
    def insert(self, parent, index, value, *, key=None, expect_gen=None) -> ChangeSet
        # parent None = the record level (record at position `index`, None = end; keyed docs need `key`);
        # index = child position BEFORE the step
    def delete(self, refs, *, expect_gen=None) -> ChangeSet                     # nodes and/or whole records (<= 10 000)
    def duplicate(self, refs, *, expect_gen=None) -> ChangeSet                  # copy right after each node/record
    def delete_records(self, positions, *, expect_gen=None, progress=None, cancel=None) -> ChangeSet
    def duplicate_records(self, positions, *, expect_gen=None, progress=None, cancel=None) -> ChangeSet
        # positions: RecordSet/array of any size (Ctrl+A in the table/tree); big ones -> array layers
    def move(self, refs, parent, index, *, expect_gen=None) -> ChangeSet
        # into `parent` (None = record level) before the child at `index` (pre-step coordinates; None = end);
        # moving a node into itself or its subtree -> EditError("edits.error.move_into_self")
    def move_by(self, refs, delta, *, expect_gen=None) -> ChangeSet             # -1 up / +1 down among siblings
    def change_type(self, refs, to, *, expect_gen=None) -> ChangeSet
    def paste(self, parent, text, *, index=None, key=None, as_records=False, expect_gen=None) -> ChangeSet
        # ONE new child of `parent` (object parents: `key`, else a free key "pasted", "pasted_2", …) or, with parent
        # None, one new record; as_records=True with parent None and an array value inserts every element as a record
    def undo(self) -> ChangeSet | None; def redo(self) -> ChangeSet | None
    def bulk_preview(self, spec, scope=None, *, progress=None, cancel=None) -> BulkPreview   # §12.6 (no command lock)
    def bulk_apply(self, preview) -> ChangeSet                                   # preview.gen must equal state.gen
    def import_patch(self, source, *, progress=None, cancel=None) -> ChangeSet   # §12.8
    def export_patch(self, path, *, diff=True, progress=None, cancel=None) -> int
    def save(self, target=None, *, backup=None, drop_broken=False, progress=None, cancel=None) -> SaveReport
        # §12.7; afterwards the session is CLOSED (layer discarded; every command -> EditError("edits.error.closed"))
    def discard(self) -> ChangeSet       # deletes the layer (tombstone first); state = initial; reset ChangeSet
    def close(self) -> None              # releases the lock; keeps a layer with steps (recovery); deletes a head-0 layer
```
* Node-level commands read the record's current bytes (`doc.pinned(state).record_bytes`; keyed: member split),
  locate the node(s), splice, validate the unit (`check_unit`; its flags go into the step), append, commit. Several
  records → one step; more than 10 000 records (or units) in one command → the session refuses it from the
  synchronous path (`edits.error.too_many`) — callers use `delete_records`/`duplicate_records`/bulk operations, which
  run as tasks and write array layers.
* The wrapper (`record=None`, wrapped documents) is edited on W = prefix + placeholder + suffix (placeholder `[]`/`{}`
  at the record container, the hole); refused with `EditError("edits.error.container_locked")`: any command whose
  target span equals or contains the hole, any insert/delete/move of children of an ancestor array at an index ≤ the
  container's index, renaming a key on the record path. After every wrapper splice the hole is located again
  (`locate(W', record_path)`) and verified to be the untouched placeholder before commit. W over `large_node_bytes`
  → `edits.error.huge_record`.
* Record-level commands change the piece table (insert: new rids with units; delete; move; duplicate: a new rid whose
  content span is the source record's current span — member-record documents always write a new unit with a free key).
  Single documents refuse record-level structure changes (`edits.error.single_root`).
* `KeyIndex` (member-record documents: keyed, wrapped object): base keys hashed (blake2b-64 of the decoded key) with
  their rids into a sorted array file built once in the background (spilled runs, like dupes.py) plus a delta for
  changed/inserted/deleted rids; a hash hit is confirmed by decoding the candidate keys. It serves record-level key
  uniqueness (`edits.error.key_exists`; duplicates already in the file are left alone), free-key generation, patch
  pointer resolution and `doc.key_index` for `_find_record_index`. Record-level key commands wait for it (the GUI
  shows busy).
* Every ChangeSet carries `records`, `remap`, `nodes` (ancestors first), `select` (inserted/moved node, or the
  neighbour of a deleted one), `wrapper`, `reset`. Undo/redo derive them from the stored step (inverse remap, reversed
  inverse NodeChanges).
* Labels (`edits.step.*`): set_value, rename_key, insert, insert_record, delete, delete_records, duplicate,
  duplicate_records, move, move_records, change_type, paste, bulk.<kind>, patch.

### 12.6 `bulk.py` (+ `dupes.py`) — bulk operations

```python
@dataclass
class BulkSpec:
    kind: str                     # replace | rename_key | delete_field | set_field | clean_strings | convert_type | dedupe
    params: dict
    def validate(self) -> None    # UserInputError("bulk.error.*", param=...)
    def label(self) -> str        # localized one-line description
@dataclass
class BulkExample:  position: int; path: str; before: str; after: str      # texts cut to 120 chars
@dataclass
class BulkPreview:
    spec: BulkSpec; gen: int      # state gen it was computed on (bulk_apply refuses another gen)
    scanned: int; records_changed: int; values_changed: int; conflicts: int; failures: int; skipped_huge: int
    examples: list[BulkExample]   # the first 5 changes in position order
    problems: list[BulkExample]   # the first 5 conflicts/failures, `after` = the reason
    bytes_new: int; elapsed: float
    def discard(self) -> None     # deletes the preview's segment and pending layer file
def preview(doc_pinned, state, store, spec, scope, *, progress=None, cancel=None) -> BulkPreview
```
Scope: `None` = all records, or a RecordSet/sequence of positions with `gen in (None, state.gen)`. The pass reads the
pinned document in blocks, prefilters where sound (`TextQuery.prefilter_needles(doc.flags)`; the key token for
rename/delete), parses candidates (`codec` exact numbers), applies the rule; every changed record gets
`format_record(new)` (keyed: original member head kept) validated with `check_unit` and appended to the preview's own
segment (`store.new_segment()`), `(rid, span)` going to the pending array layer. Free space is checked before the pass
(estimate: scope bytes × 1.2 + 1 GiB) and every 256 MiB written (`bulk.error.disk_space`). Records whose parse shows
duplicate keys (checked with the `json` C scanner and `object_pairs_hook` on changed records only) are skipped as
conflicts. Huge records → `skipped_huge`. Changed records are re-serialized in the file's style (values exact; escapes
normalized; documented).

| kind | params | semantics | values_changed counts |
|---|---|---|---|
| `replace` | `find`, `replacement`, `mode` text\|regex, `case_sensitive`, `whole_word`, `path` (pattern, optional) | string VALUES only (not keys) whose path matches (`pattern.matches_prefix_of(value_path)`, the search rule; no path = all). Text mode: `TextQuery(find, "plain", case_sensitive, whole_word)` (casefold + apostrophe equivalence + Uzbek whole-word rule; no translit); every span of `find_spans(s)` replaced right to left by the replacement as LITERAL text. Regex mode: the TextQuery's compiled regex (same whole-word guards) `.sub(replacement, s)` — Python template syntax (`\1`, `\g<name>`), documented | strings whose text changed |
| `rename_key` | `old`, `new` (≠ old), `path` (pattern of the objects, optional) | member `old` → `new` keeping its position; an object that already has `new` is a conflict (counted per object; the other objects of the record are still renamed); record keys of keyed documents are not touched | renamed members |
| `delete_field` | `path` (pattern; `..name` any depth, `*`/`[*]`) | removes every object member matched; only outermost matches (descendants of a removed member are not visited); array elements are not fields | removed members |
| `set_field` | `path` (concrete), `value` (JSON text), `mode` always\|if_missing\|if_exists | sets the value creating missing intermediate objects; a non-object intermediate is a conflict; an equal value (`query` equality: exact numbers, deep) is no change | records where the value changed |
| `clean_strings` | `trim`, `collapse_spaces`, `path` (optional) | trim = `str.strip()`; collapse = `re.sub(r"\s+", " ", s)` (`str.isspace` whitespace) | strings changed |
| `convert_type` | `path` (pattern), `to` string\|number\|bool\|null\|object\|array | `editops.convert_value` on outermost matches; values already of the target type untouched; impossible → failure (value unchanged) | values converted |
| `dedupe` | `key` (concrete path or `$key`), `keep` first\|last | among records whose key exists (null counts as a value), all but the first (last) of each equal key are deleted; equality = canonical JSON (numbers by exact decimal value, objects with sorted keys, strings exact) | — (records_changed = deleted) |

`dupes.py` (shared with stage 5 statistics):
```python
@dataclass
class DuplicateReport:
    groups: int; duplicates: int; records_with_key: int; missing: int
    records: RecordSet            # positions of the duplicates (all but the kept one per group), ascending, gen set
    examples: list[tuple[str, list[int]]]   # (key text, positions) of the first 20 groups
def find_duplicates(doc, key: str, source=None, *, keep="first", memory_limit=None, progress=None, cancel=None) -> DuplicateReport
```
Keys come from `core.PathExtractor` (huge records: `stream_values`), are canonicalized, hashed (blake2b-128) and stored
as one Python int `(hash << 64) | position` (sized as ~52 B each); batches are sorted in memory and spilled as runs to
`work_dir` beyond `memory_limit` (default `memory_limit_bytes // 4`), merged with `heapq.merge`; duplicate positions
are collected in a record_count-bit bitmap to produce the ascending RecordSet.

### 12.7 `saver.py` — Save and Save As

```python
@dataclass
class SaveReport:
    target: Path; records: int; bytes_written: int; copied_bytes: int; edited_records: int
    backup: Path | None; elapsed: float; warnings: list[str]; kind: str; encoding: str; compression: str | None
    reopen_options: dict          # {"format": kind (brace_list -> "array"), "record_path": doc.record_path}: the caller
                                  # MUST reopen with these (detection could pick another kind for 0-1 records)
def save(doc, target=None, *, backup: bool = True, drop_broken: bool = False, settings=None,
         progress=None, cancel=None) -> SaveReport          # target None = in place; save pins the document
```
* Output = the **same kind**: array → array, keyed → object, wrapped → the (edited) wrapper around the record
  container, jsonl → lines, stream → values, single → the value; `brace_list` → a JSON array (the head's last `{`
  written as `[`, the tail's first `}` as `]`, every other byte kept; warning `saver.warn.brace_list`, asked first in
  the GUI). Same encoding/compression: the BOM is written OUTSIDE the validated stream (UTF-8 BOM iff
  `info.encoding == "utf-8-bom"` or `prepared.source_bom == 3` inside gzip; UTF-16 BOM iff `prepared.source_bom ==
  2`), UTF-16 LE/BE re-encoded, gzip with `zlib.compressobj(6, DEFLATED, 31)` and `mtime=0` (trailing non-gzip data of
  the source is dropped with `saver.warn.gzip_trailing`). Save As: compression follows the target name (`.gz`).
* Bytes, in order: **head**; the records in effective order; **tail**.
  * comma kinds and single: head = `[data_start, first record start)`, tail = `[last record end, data_size)`;
  * wrapped: head = effective prefix (`wrapper_spans()[0]`) + original `[container_span[0], first record start)`;
    tail = original `[last record end, container_span[1])` + effective suffix (`wrapper_spans()[1]`);
  * zero records: comma kinds = head's leading whitespace + `style.empty` + tail's trailing whitespace; wrapped =
    effective prefix + `style.empty` + effective suffix; jsonl/stream = empty output (opened again with 0 records);
  * a file WITHOUT base records (`[]`, forced keyed `{}`, a forced wrapped empty container, jsonl of broken lines
    only): head = up to and including the opening bracket (+ newline + indentation when the file is pretty), tail =
    from the closing bracket;
  * between output items a and b: if both are base records and `rid_b == rid_a + 1`, the original gap is copied
    (pure runs are copied as one range `[span(lo).start, span(hi-1).end)`); otherwise `style.record_sep`;
  * JSONL: head, tail and every gap are filtered — broken lines (from `iter_broken_lines()`; when `broken_count`
    exceeds what is listed, any gap holding a non-whitespace byte) are removed with their line end and the run is
    rejoined with `style.newline`; `drop_broken=False` with `broken_count > 0` → `EditError("saver.error.broken_lines",
    count)` (the GUI asks, then passes True).
  Adjacent content spans are merged into reads of ≤ 8 MiB; pure runs are copied in 1 MiB chunks.
* Validation: the UTF-8 stream (without BOM) is fed through a bounded queue (8 × 1 MiB) to a helper thread running a
  core `Scanner` in the kind's mode (wrapped: MODE_CONTAINER with the record path; keyed/array/brace_list:
  MODE_CONTAINER `[]`; jsonl: MODE_LINES, where any `take_broken()` entry also fails; stream: MODE_STREAM; single:
  MODE_SINGLE); an error or a record count ≠ `doc.record_count` → `ExportError("saver.error.invalid_output", detail)`,
  nothing replaced, details logged.
* Preconditions: index complete, `doc.error is None`, the source not read-only for an in-place save
  (`UserInputError("fileio.error.read_only")` — the GUI offers Save As).
* Commit (in place), after the temp file (`.<name>.<hex>.tmp` next to the source) is written, fsynced and closed:
  1. growth check NOW: `fileio.appended_bytes(source, identity)` must be exactly 0 (`None` → `FileChangedError`,
     `> 0` → `EditError("saver.error.grown", bytes)`); `ensure_unchanged()`;
  2. journal `{source, temp, backup, old_identity}` into the layer (`store.journal`), durable;
  3. backup on: an existing `<source>.bak` (read-only bit cleared) is renamed to `.<name>.<hex>.old`; then
     `os.link(source, <source>.bak)` (hard link = instant backup, works while our handles are open);
  4. `fileio.replace_posix(temp, source)` (new helper: `SetFileInformationByHandle(FileRenameInfoEx,
     REPLACE_IF_EXISTS | POSIX_SEMANTICS)` with `long_path`; atomic, works while our own handles/mmaps are open);
  5. fallback when the link or the POSIX rename is not supported (FAT/exFAT, some SMB, old Windows): rename the
     source to `<source>.bak` (backup on) or `.<name>.<hex>.old` (backup off), then `os.replace(temp, source)`; on
     failure the first rename is undone; the error names where the original is;
  6. on success: delete `.old` files of this save, clear the journal. At the next open a journal with a missing
     source rolls forward (temp complete → rename) or back (aside → source) and tells the user.
  Save As: `os.path.samefile(target, source)` → in-place path; otherwise temp next to the target + `replace_posix`
  (works even when another tab holds the target), the source is untouched; growth during Save As adds
  `reader.warn.appended` (the snapshot is written).
* Speed: pure runs at disk speed; validation beside (Rust Scanner ≈ 700 MB/s). Optional (if time permits): write the
  new index from the validator's spans under the new identity so the reopen is instant.

### 12.8 `jsonpatch.py` (RFC 6902 / RFC 6901) and `jsonreshape patch`

```python
def iter_patch(source) -> Iterator[PatchOp]        # streams the top-level array with the core Scanner (MODE_CONTAINER);
                                                    # each op parsed with codec (exact numbers); structure errors ->
                                                    # UserInputError("patch.error.*", index=<op number>)
def export_patch(session, path, *, diff: bool = True, progress=None, cancel=None) -> int
def export_layer(store: EditStore, path, *, base_doc=None, progress=None, cancel=None) -> int   # pending/stale layers
def apply_patch(session, source, *, progress=None, cancel=None) -> ChangeSet
```
* Pointers (`paths.parse_pointer`, strict: no stripping; `~0`/`~1`; `-` only as the last segment of an array target
  of add/move/copy): array/jsonl/stream/brace_list documents = an array of records (`/12/name`); wrapped = the full path
  (`/data/12/name`); keyed = records by key (`/id42/name`, via KeyIndex); single = the root.
* Import: pass 1 validates the ops and collects keyed record keys; pass 2 applies them in order on a working copy:
  touched records parsed with exact numbers into an LRU (≤ `memory_limit_bytes // 4`, evicted records serialized into
  the patch's segment), structural ops on a working piece table; at the end ONE step (kind "patch"; > 10 000 changed
  records → array layer; record structure as a `("pieces", old, new)` Remap). `add` onto an existing member replaces
  it (RFC 6902 §4.1); `test` uses query equality (exact numbers, deep); a touched record with duplicate keys →
  `UserInputError("patch.error.duplicate_keys", index, path)`; any failure → nothing changes (segment deleted).
* Export (net effect against the original file): kept base records = the maximum-weight increasing subsequence of the
  piece runs (weight = run length); (1) `remove` of deleted base records in descending original position; (2) `move`
  of the other surviving base records to their final positions (ascending final position; current positions from a
  Fenwick tree); (3) `copy` for duplicates of base records whose content is still the source span; (4) `add` of new
  records ascending; (5) for kept records with changed content a structural diff at FINAL positions (objects:
  remove/add/replace per key; arrays of equal length per index, else `replace`; numbers compared by exact token);
  (6) wrapper member changes. Keyed: `remove`/`add` by key, renames as `move` only when the target key is free at that
  point (cycles through a temporary key); keys of deleted/renamed base records come from `Step.keys` (no base read).
  `diff=False` (the source changed on disk) writes whole-record `replace`s from edited content and needs no base
  reads. Object member order is not represented (RFC 6902).
* CLI: `jsonreshape patch INPUT PATCH OUTPUT [--in-place [--no-backup]] [input options]` applies a JSON Patch and
  writes OUTPUT with `saver.save` (same kind/encoding as INPUT); `--in-place` replaces INPUT (OUTPUT omitted; `.bak`
  unless `--no-backup`). The edit layer of a CLI run lives in a temporary directory, deleted at the end. Exit codes as
  §10 (a failed `test` op = 1).

### 12.9 GUI (`gui/editing.py`, `gui/editors.py`, `gui/bulkdialog.py` + integration)

```python
class EditController(QObject):        # gui/editing.py, one per DocSession (session.edit)
    changed = Signal(object)          # ChangeSet, emitted in the GUI thread AFTER the session applied it
    state_changed = Signal()          # mirrors below changed
    pending_found = Signal(object)    # list[PendingInfo] (recovery bar)
    available: bool; reason: str      # editing possible now? reasons: indexing, partial, locked, huge_single,
                                      # recovery_pending, file_changed, file_grown, saving, applying, closed
    busy: bool                        # a command/bulk/patch/save in flight
    dirty: bool; can_undo: bool; can_redo: bool; undo_text: str; redo_text: str; steps: int
    applied_gen: int                  # gen of the state the GUI has installed
    def set_value(self, ref, json_text) -> None ...  # every EditSession command; refs are GUI NodeRefs (positions of
                                      # applied_gen); at most ONE command in flight — a command while busy is refused
                                      # with a status message (e.g. holding Alt+↓ moves as fast as steps commit)
    def undo(self) -> None; def redo(self) -> None
    def save(self, *, on_done=None) -> None; def save_as(self, path, *, on_done=None) -> None
    def discard(self, parent) -> None                       # asks first
    def restore_pending(self, info) -> None; def discard_pending(self, info, parent) -> None
    def export_pending(self, info, path) -> None
    def bulk_preview(self, spec, scope, on_done) -> GuiTask; def bulk_apply(self, preview) -> None
    def import_patch(self, path) -> None; def export_patch(self, path) -> None
    def ask_close(self, parent, on_done: Callable[[bool], None], *, why: str) -> None
        # non-blocking 3-way question when dirty (Save / Don't save / Cancel); buttons adapt: file changed on disk ->
        # "Save changes as JSON Patch…/Discard/Cancel"; file grew -> "Save As…/Discard/Cancel". "Don't save" =
        # discard (the layer is deleted, never offered for recovery). Test seam: editing.ask_save_handler(parent,
        # name, choices) -> str; headless without a handler: keep the layer for recovery and close.
    def detach(self, timeout: float = 2.0) -> None          # stop accepting, finish the running command, close session
def remap_ref(ref, cs: ChangeSet) -> NodeRef | None          # record remap, then the record's NodeChanges (array-index
                                                             # segments of path and ipath rewritten); None = deleted
def to_edit_ref(session, ref) -> Ref; def from_edit_ref(session, ref) -> NodeRef   # single docs: None <-> 0
```
* A plain `_EditWorker` (thread + queue + weak reference to the controller, like `loader._State`) owns the
  `EditSession` (`auto_apply=False`): opening (preconditions, lock, `find_pending`, restore), commands, undo/redo,
  discard, pending-layer actions. The GUI thread never calls `EditSession`/`EditStore`; the controller mirrors
  `available, reason, busy, dirty, can_undo, can_redo, undo_text, redo_text, steps` from worker posts. Commands carry
  `expect_gen = applied_gen`. The lock is taken when the session's index finishes (layer files are created at the
  first step): a second tab/instance on the same file gets `available=False, reason="locked"`.
* Applying a ChangeSet happens in ONE GUI-thread slot (`DocSession._apply_change`): `doc.set_overlay(cs.state)`;
  `session.data_gen = cs.gen`; for `remap`: view RecordSets up to `REMAP_INLINE_MAX = 200 000` positions were remapped
  by the worker before posting and are installed now (bigger ones: the session switches to the base view, keeps the
  specs and shows "view outdated — Refresh"); `current`, history, `_pos_cache` remapped with `remap_ref`; a running
  filter/sort pipeline restarted; for `reset`: base view, current/history/position caches cleared; then
  `data_changed(cs)` and `dirty_changed`. Every view invalidates synchronously in its `data_changed` slot.
* Pinning & tags: `DocSession.snapshot() -> (pinned_doc, view_set)` is used by every task that takes positions
  (export, search, iter_hits, schema, bulk preview scope, patch export); `keep_document_for(task, sets, doc=pinned)`
  keeps that state's readers open; RecordSets carry `gen`. The table's fetch closure pins once per batch; tree
  `NodeSource` has a `gen` (results of an older gen are dropped); text cache keys and `_stable.filtered_key` include
  `data_gen`; `fields.forget(session)` on bulk/patch/restore/discard (schema marked stale otherwise).
* Invalidation: `cs.remap` or `cs.records is None` → tree/table `loader.invalidate()` (all); else the positions in
  `cs.records` (+ ancestors in the tree). One helper `is_edit_offset(start)` makes the tree offset column/tooltips, the
  text view's "open raw", the raw highlight and the status bar show "tahrirlangan" (edited) and never jump.
* Tree (`TreeView._apply_change(cs)`): bump its data gen, detach its tasks; `flat.remap_tops(cs.remap)` (every view:
  top keys are positions in filtered/sorted views too); `flat.remap_children(keypath, remap, count)` for each
  NodeChange in list order (record None → WRAPPER key, single document → ROOT); FlatTree re-keys `by_idx`, rewrites
  the keypaths of recorded descendants, drops children mapped to None; a node with a display order falls back to file
  order (its order task runs again); one `_rekey(fn)` helper remaps or clears every keypath-keyed map (`_known,
  _orders, _kinds, _elem_sorts, _busy, _pending_expand, _qf_*, _cur_kp, _pending_select, _crumb_refs, _pos_*`); the
  records-member row is invalidated; scrolling is anchored on the top visible node; the selected keypaths are
  re-applied. Record-level moves/drags only in the base view; sibling moves only when the parent shows file order
  (else disabled with a tooltip).
* Keys: Enter, Shift+Enter, F2, Insert, Del, Ctrl+D, Ctrl+V, Alt+↑/↓ are handled in the grids' key handlers
  (`TreeView._on_key` before its Ctrl/Alt early return; `TableView._grid_key`), never as window/central shortcuts
  (they would steal Enter/F2/Ins/Alt+↓ from line edits in the document area: find bars, filter row, inline editors);
  menus show them for display only (like actions.py). Window-level: Ctrl+Z, Ctrl+Y, Ctrl+Shift+Z, Ctrl+S,
  Ctrl+Shift+S, Ctrl+H (bulk), Ctrl+B (bookmark). Tree: Return = edit; double-click (`activated`) = toggle for
  containers, inline edit for scalars. Pinned tests change: test_gui_mainwindow (bookmark Ctrl+D → Ctrl+B),
  test_gui_e2e, test_gui_treeview ("Enter toggles").
* Editing values: Enter/F2 first loads `(kind, exact_token, decoded)` of the node in a silent task (token ≤ 64 KiB;
  bigger strings use the dialog loader); the editor opens when the result arrives if that node is still current: a
  line editor when `decoded` has no line break and ≤ 200 characters, else `StringDialog`; numbers prefilled with the
  exact token; bool toggles; null and containers open `JsonEditDialog`. Table: editable cells = a concrete column
  path, or a wildcard column with exactly one match (its concrete path); `$index` read-only; `$key` = `rename_key`
  (keyed documents); container cells keep Enter = reveal; a missing field uses `set_path`; Del on cells = delete the
  fields of concrete paths only (wildcards → "use Bulk operations").
* Selection & drag: `VirtualGrid` gets `set_selected_ranges(ranges)`, `set_row_drag(handler)` (`can_start(rows) ->
  bool`, `target(row, frac) -> (row, zone) | None`, `drop(rows, row, zone)`), a pure `drop_zone(frac)` (< 0.28
  before, > 0.72 after, else inside), an internal drag (no QDrag): start after `startDragDistance`, a press on an
  already-selected row changes the selection on release only, the grid paints the indicator, shows `ForbiddenCursor`
  when `target` is None, auto-scrolls, Esc cancels. The tree keeps normalized selected keypaths (no descendants of a
  selected ancestor, no wrapper/records rows), re-applied after structure changes and remapped on data changes. Node
  commands accept at most `EDIT_SELECTION_MAX = 10 000` nodes (above: a message pointing to Bulk operations); record
  delete/duplicate over row ranges use `delete_records`/`duplicate_records` (tasks); the "selected records" bulk
  scope is built in a background task.
* Save sequence: refused while busy; brace_list / broken-lines questions first (`widgets.ask`, non-blocking); then
  `busy`, `session.pause_change_check()`, a heavy task kind "save" with `keep_document_for` (`_close_after` waits for
  "save" like "export"); on success: the edit session is closed (layer discarded when unused), bookmarks converted,
  in place → `session.reopen(discard_edits=True, format=…, record_path=…)` with `reopen_options`; Save As →
  `session.switch_path(target)` (path, name, bookmark/layout keys, recent files, title, tooltip) then reopen; on
  failure: change check resumed, busy cleared, error reported. `DocSession.reopen(discard_edits=False)` raises
  `EditError("edits.error.dirty")` when dirty; the UI entry points (`DocumentTab.reopen`, the info bars' Reopen
  buttons, `RecordPathDialog.accept`, `MainWindow.close_tab` → bool, `closeEvent` → ignore, ask tab by tab, close
  again) call `ask_close` first. "Discard all changes" and the recovery bar's Discard ask first.
* Bookmarks: while a layer is active they store rids (persisted: base rids as original positions, new rids as
  `{"rid": n, "layer": key}`); on save they become positions of the new file (deleted ones dropped); Save As copies
  them to the new path; discard drops new rids.
* `editors.py`: `InlineEditor` (line edit over a grid cell: Enter commit, Esc cancel, live number validation),
  `StringDialog` (multi-line; strings up to 4 MiB — larger: "use Bulk replace or JSON Patch"), `JsonEditDialog`
  (QPlainTextEdit; live validation debounced 200 ms in the GUI thread below 256 KiB, in a background task above (results
  tagged, stale ones dropped); error line/column + red wavy underline at `char_offset` converted to UTF-16; OK only
  when valid; indent 2/4/tab; Format; nodes up to 4 MiB of pretty text), `KeyDialog`, `InsertDialog` (key + type +
  value), `type_menu()`.
* `bulkdialog.py`: `BulkDialog(session, scope_default)`: operation combo, parameter form per kind, scope (all
  records / current view / selected records), Preview (heavy task, progress) → counts + 5 examples (before → after)
  + problems → Apply (enabled only while `preview.gen == applied_gen`; it listens to `data_changed`) / Close
  (discards the preview). States the huge-record limitation when the document has huge records.
* Main window: File ▸ Save (Ctrl+S), Save As (Ctrl+Shift+S); Edit ▸ Undo, Redo, ─, edit commands of the current view
  (display shortcuts), ─, Bulk operations… (Ctrl+H), Import JSON Patch…, Export changes as JSON Patch…, Discard all
  changes; Navigate ▸ Add bookmark (Ctrl+B). Tab and window titles show `*` when dirty.
* `DocumentTab`: recovery bar ("Unsaved changes from <time> (N steps) — Restore / Discard / Save as JSON Patch…";
  editing unavailable until answered; locked layers shown as info only; stale/damaged layers offer Export/Delete; an
  interrupted save journal offers roll forward/back first), the "view outdated — Refresh" bar, a read-only note when
  editing is unavailable; when the source changes on disk while dirty, the "file changed" bar offers "Save changes as
  JSON Patch…" (`diff=False`).
* Status bar: "● N o'zgarish" (dirty) next to the record count; "tahrirlangan" instead of a byte offset for edited
  nodes.
* Settings (core `Settings`, General tab): `edits_location` ("appdata" | "beside", default "appdata"),
  `backup_on_save` (True), `undo_limit` (1000, ≥ 200).


### 12.10 As built (rounds A–B; the module docstrings are authoritative for details)

* **overlay.py:** `OverlayState` is `frozen=True, eq=False` (identity comparison); derived states only through
  `state.derive(**changes)` / `state.with_content(changes, **more)` (fresh gen; `dataclasses.replace` raises);
  `PieceTable.count`/`npieces` are properties; `apply(remap, new_rids=[(pos, rid_lo, n), …])` consumes the entries in op
  order; `invert_node_changes(nodes, record_remap)`; ArrayLayer files: header + segment list padded to 64 B, crc32 of
  the arrays, any damage → `edits.error.layer_corrupt`; `runs()` issues one `keys_in` per layer per group of nearby rids
  (`CLUSTER_GAP = 256`); `ChangeSet.records` above 10 000 → None. Measured (1 M pieces): insert/delete 0.3 ms, move
  1.2 ms, `rid_at` 0.56 µs, `delete_positions` 1 M of 10 M 0.26 s.
* **editops.py:** extra `DocStyle.comma` (item separator of single-line output), `Loc.pstart/pend` (parent span),
  `member_loc(buf)` (a keyed record as a member: `set_value` replaces the value, `rename_key` renames the record key),
  `free_key(taken, base)`, `insert_child(..., verbatim=True)` for duplicates; extra error keys
  `edits.error.{not_member, not_container, bad_index, key_required}`; `detect_style` caches per origin document and
  reads ≤ 64 KiB; `locate` with a complete ipath also verifies keys/indices (stale refs → `path_not_found`).
* **dupes.py:** keep follows position order; entries ~60 B; examples 20 groups × 20 positions; public
  `canonical_bytes`, `canonical_token`, `key_hash`; equal 128-bit hashes are equal keys (no confirmation pass).
* **editstore.py:** `Step.struct` = canonical Remap ops (+ `new_rids`, `old_rids` filled by `commit`, explicit
  `next_rid`); `flags=None` = unchanged; `LayerDamagedError(.seq, .last_good)`; `delete_layer`, `step_label`,
  `notice_texts`; a busy lock raises `edits.error.locked`, an unusable lock folder `editstore.error.unavailable`;
  truncated redo files are deleted right after the transaction (leftovers collected at the next open); the store reads
  its own blobs through a private reader. Measured (1 M records, 300 k scattered deletes): commit 0.22 s, undo 0.49 s,
  reopen + load 0.2 s.
* **reader.py:** `PinnedDocument` (own attributes `_origin`, `_pin` only); `VirtualReader` (offsets between
  `data_size` and `EDIT_BASE` read as EOF; reads past a segment's published end → `layer_corrupt`); `Node.mstart`,
  `member_span()`, `is_composite`, `is_edited`; `node_chunks` joins records with `,` (original separators inside
  unchanged runs); a composite node's `size` on an edited document is an estimate (`effective_bytes() + 2` …; the
  `value()`/`raw()` limits use the exact figure); `doc.key_index` is used only when its `gen` matches the state read;
  `set_overlay` requires a complete index and the same base count; edited nodes with more than `large_node_children`
  children spill to a temporary `enode-<pid>-<hex>.jri` deleted at close; `RecordSet(gen=…)`, `RecordSetBuilder(gen=…)`,
  `from_indices(gen=…)`. Measured (200 MB, 837 k records): no overlay unchanged (0.15 s `iter_blocks(None)`), 10 edits
  +2 %, every 3rd record edited 1.61 s (block-based; filter 3.1 s).

### 12.11 As built after the stage-3 review (fix round 3; supersedes the matching lines above)

The stage-3 code was reviewed adversarially (lenses: concurrency, safety, GUI; every finding reproduced before it was
fixed, each fix with a regression test that failed before it). Changes to the contract:

* **Layer files are never reused or deleted under a reader** (`editstore.py`, `overlay.py`, `reader.py`): segment and
  layer-file numbers are never reused for a folder within the process (a per-folder counter raised from the files
  present; after a discard the new point segment is `seg-<n>.dat`, n > 0, meta `point_seg`,
  `EditStore.point_segment`; `append(data, *, seg=None)` writes to it). `discard`, the close of a head-0 layer and redo
  truncation delete files only when no document-owned state reads them: a process-wide registry of retained files,
  weakref callbacks and a closed-reader hook feed a `SimpleQueue` drained by the daemon thread
  `jsonreshape-layer-reaper` (no I/O in the callbacks; the GUI GC policy ignores that thread); a file that is no longer
  the same `(dev, ino, size, mtime)` is never deleted; leftovers of ended processes are collected at the next open and
  by an atexit sweep. Edit-offset child caches are keyed by `(DatReader.uid, start)`. `EditSession.discard()` order:
  tombstone → reset → delete.
* **Integrity**: every step row carries `crc = [[seg, start, end, crc32], …]` of the bytes it appended (bulk segments
  too); the checkpoint keeps the folded steps' crcs merged per segment (`crc32_combine`); `load_state` verifies them
  (`LayerDamagedError(seq, last_good)`); rows without crcs still load.
* **One editor per physical file**: `LayerLock.for_doc` also takes the lock of `file_lock_key(path)` (volume serial +
  file id; fallback realpath), so two spellings of one file (junction, 8.3 name, case) cannot be edited at once;
  `LayerLock.key` stays the layer key (a pending layer made under one spelling is not offered under another — known
  limit).
* **Interruptible**: `EditSession.interrupt()` / `cancel_token`, `KeyIndex.interrupt()`; `EditSession.close()` and the
  GUI worker's close interrupt a command waiting for the key index.
* **Save** (`saver.py`): the temp file gets the source's attributes before the journal (hidden, system,
  not-content-indexed copied; archive set; NTFS compression / EFS applied to the empty temp, failure →
  `saver.warn.compression_lost` / `.encryption_lost`). **Journal v2** (`JOURNAL_VERSION = 2`): `{v:2, op, source,
  temp, backup, had_backup, bak_read_only, old_backup, aside, old_identity, old_file:{dev,ino},
  new:{size,hashes,mtime_ns,dev,ino}, ids, layer:{key,kind,record_path,base_count}, time}` (ids as strings; `ids` true
  only for non-zero distinct same-volume ids on NTFS/ReFS, `fileio.stable_file_ids`). **Roll by evidence** a rename
  keeps and a content edit cannot fake: temp present → "back" if the source is the old file, else "conflict"; temp gone
  → "completed" iff the source is the new file and not the old one, "back" iff it is the old file and not the new one;
  source missing → "forward" if the temp is the new file, else "back" from the `.old`/`.bak` copy that is the old file;
  any doubt → "conflict" (nothing touched, files named); foreign / out-of-folder journal paths → "invalid"; v1 journals
  are never "completed" while the temp exists; "back" never overwrites a `.bak` that is not our link of the original;
  undo and roll "back" restore a read-only `.bak` with its bit. A missing source: `find_journals(path)`,
  `roll_journal_at(info)`, `layer_store_at(info)` (the tab shows "Saqlashni yakunlash" and reopens the file).
  `sweep_leftovers(source, …) -> SweepResult` removes orphaned `.<name>.<hex>.jrsave` / `.old` files (not named by a
  live journal, nothing locked elsewhere, not in use, ≥ `SWEEP_MIN_AGE` 180 s, `.old` only with a newer `.bak`) at every
  save and in the GUI after an open / roll / restore. `save_questions(doc, target=None, *, backup=None, settings=None)
  -> [SaveQuestion(key, text, params)]` (`brace_list`, `broken_lines`, `gzip_trailing`) lists what the GUI asks; a close
  error while another error propagates is logged, never raised over it. `EditSession.save` reports a cleanup failure
  after a successful save as the warning `edits.warn.layer_left`.
* **GUI controller** (`gui/editing.py`, `session.py`, `mainwindow.py`, `qtasks.py`): an in-place save first checks the
  document's readers (`DocSession.document_users()`): "wait, then save (default) / stop them and save / cancel" (a small
  non-heavy cancellable wait task); the controller's own bulk previews are cancelled on save / discard and a preview
  failing on a closed layer counts as cancelled. `ask_close` also asks "wait / stop / cancel" while a save runs or a
  clean document has a long task (`needs_close_answer`); close tab / F5 / quit ask such tabs too. Open inline editors
  (and the structure builder's rename) are committed before save / save as / close / reopen
  (`editing.settle_edits`; an invalid value refuses with a status message). Save As / export onto a file another tab
  shows: refused when that tab has unsaved / pending / running changes or another program holds the file lock, asked
  when it is unchanged (that tab reloads) — `editing.target_conflict`; no `.bak` of a replaced Save As target. Expected
  user mistakes (`editing.user_mistake`) go to the status bar (every command takes `on_error=`); I/O and internal
  errors keep the error dialog. `DocSession._apply_change` falls back to a full reset on any exception (the controller
  finishes the job: `_item_crashed`); the step's selection is emitted last, after `data_changed`. The close thread
  waits on plain events only (`GuiTask.done_event`); finished tasks drop `owner`; released leases leave `_held`. A
  forward/completed roll that finds the file still equal to the journal's old identity keeps the layer (roll action
  "kept"). The Export action has its own theme-aware icon; parentless error windows close with the main window.
* **Views** (`treeview.py`, `tableview.py`, `textview.py`, `vgrid.py`, `loader.py`, `editors.py`, `actions.py`): a
  reopened document never caches indexing-time `IndexError`s (`RowLoader.clear()`, `invalidate_failed()` on
  `index_finished`; `NodeSource.fetch` leaves out records not indexed yet); `VirtualGrid` ignores key events while a
  child editor has focus; new keys are checked asynchronously before a commit (`key_check(key, reply)` in
  InlineEditor / KeyDialog / InsertDialog; record keys in a silent task); dialogs and inline editors follow their node
  through other edits (`gui.tree.edit.gone` when it was deleted); the type menu offers only possible conversions
  (`editors.type_refusal`, `actions.change_type_refusal`); menus show each view's own keys (`actions.command_keys`);
  in the TABLE, F2 renames (record key on `$key`, else the member key of the cell) and Enter edits the value;
  protected rows (wrapper, record container, single root) say why a command does nothing.
* **Bookkeeping**: `tests/conftest.py` isolates `JSONRESHAPE_HOME` with a private `MonkeyPatch` plus a session-level
  temp home (a test's own `monkeypatch.undo()` or a worker thread outliving its test can no longer write into the real
  `%LOCALAPPDATA%`).

### 12.12 As built: performance round (P3; numbers in `bench/results/edit_latest.md`)

* **bulk.py (P3):** `preview(..., workers=None)` -- scopes of at least `POOL_MIN_BYTES` (64 MiB) use `auto_workers`
  worker processes (spawn; `min(cpu_count - 1, 8)` and at most `memory_limit_bytes / 2` at 64 MiB each: 4 by default;
  only when `_spawn_safe()`, the rule of `engine._spawn_safe`), jobs of `POOL_BLOCK_BYTES` (2 MiB) of records, at most
  `2 * workers` in flight, results merged in position order (counts, first 5 examples / problems, units appended in
  `APPEND_BYTES` batches with the free-space checks, layer entries): byte-identical to one thread; `BrokenProcessPool`
  -> the pass is repeated in the calling thread; `BulkPreview.workers`. One `_Runner` per block does the per-record work
  in both modes; the duplicate-key check of a changed record compares `codec.dumps(value before the rule)` with the
  record text and falls back to the `json` decoder (records with >= 1000 brackets always decode); recursive rule walks
  (iterative below depth 48); text `replace` pre-checks each string with `prefilter_needles(0)`. Measured (2.1 GB,
  6.5 M records, shared machine): set_field on every record 120 s -> 19 s (111 MB/s, 4 workers, <= 300 MB incl.
  workers), every 3rd record 43 s -> 12 s.
* **jsonpatch.py (P3):** export: changed records read in chunks of 4096 with coalesced reads (the spans of nearby
  originals from one index read, `Document._spans_bytes`), the diff skips subtrees with equal `codec.dumps` texts
  (scalars by type + value; plain floats also by the sign of 0), pointer texts carried, array-based run bookkeeping
  (no sort when no record moved); many changed records: the diffs of the chunks run in worker processes
  (`export_patch(..., workers=None)` / `export_layer(..., workers=None)`; automatic once `POOL_MIN_BYTES` (64 MiB) of
  changed records were diffed in the calling thread and `bulk._spawn_safe()`, `bulk.auto_workers` of them; a chunk's
  records travel as one packed blob; the calling thread reads the records and writes the results in order, at most
  `2 * workers` chunks in flight: byte-identical; a chunk a worker cannot do is redone in the calling thread, so an
  error is the one-thread error; `BrokenProcessPool` -> the rest in the calling thread). Import: `_load_checked`
  (codec.loads + dump comparison, else the exact decoder), tilde-free pointers split directly, one-pass validation
  of each append batch, `_SpanLog` (compact rid -> span log, 24 B per write, spills beyond 2 M entries to
  `patch-*.spans.tmp` next to the layer with a sparse index; `write_array_layer` keeps a record's last write),
  `LRU_FACTOR` 6 -> 9 (measured); `finish` frees the record cache before the step's layer is built and, with the
  state's own record table, keeps the written records that existed without a position lookup per record.
  Measured (2.1 GB, 6.5 M records): bench/results/edit_latest.md.
* **reader.py (P3):** `_EffWindow` computes the spans and pure/impure marks of a window of `OV_WINDOW` positions at
  once (index reads per rid cluster, one `spans_in` per layer and cluster, contiguous / evenly spaced overrides as
  slice assignments), used by `_ov_run_blocks` and `_eff_bytes` (same blocks, same totals; the per-window run split
  of `effective_bytes` is unchanged); scattered blocks check their records' first / last bytes at once
  (`_first_unlike`) and group segment reads without per-record Python (`_unit_pieces`). Measured (200 MB, every 3rd
  record edited): `iter_blocks(None)` 0.95 s -> 0.71 s, JSON export 2.27 s -> 2.0 s.
* **overlay.py (P3):** `ArrayLayer.keys_in` / `spans_in` / `items` of `FILE_READ_MIN` (4096) values or more read the
  file through a second `open_shared` handle (smaller ranges and `get` use the mapping): a pass over a layer of
  millions of entries no longer adds the layer file to the working set (export of the 2 GB "every record edited"
  state: RSS 267 MB -> ~120 MB). `write_array_layer` writes the natural runs of a spilled batch straight from the
  batch arrays (no copy of a run: a 4 M-entry batch no longer doubles while it is spilled).
* After P6 (§14.10 writer): CSV export 86–226 MB/s in every edit scenario (rows in worker processes); restore: checksummed ranges ≥ 64 MiB verified by 4 threads (6.5 M-record bulk layer 1.04 → 0.41 s); numbers in `bench/results/export_latest.md` / `edit_latest.md`.

---------------------------------------------------------------------------------

## 13. Structure builder / mapping (stage 4)

AGENT_PROMPT §7, §13 (`apply`, `flatten`), §14 tests 1–10, §12 ("export with mapping ≥ 50 MB/s, RAM < 300 MB").
The reference is the engine of `json-tuzilma.html` (`/*ENGINE-START*/ … /*ENGINE-END*/`, HTML lines 219–536).
A complete analysis with a verified line-by-line Python transliteration exists (0 mismatches on 82 targeted and 4 000
random cases with 25 028 edit operations): scratchpad `s4/engine_map.md` (semantics, examples, node harness),
`s4/ui_map.md` (UI, badges, drag & drop, integration points), `s4/engine_sketch.py`, `s4/engine_sketch_ops.py`,
`s4/engine_harness.js`. Those files are part of this spec: where this section is silent, the reference behaviour
described there (and the code itself) decides.

### 13.0 Decisions

* **Semantics = the reference engine**, node for node: tree inference (`inferInto`/`fromAcc`: array beats object
  beats value, first-seen key order, an elem for every array even when always empty), binding by compiled source
  prefix with "deepest bound `[]` wins", gather, broadcast, array-node concatenation (element-node order, then source
  order), `recKey` pre-order with `_depth` = 1, 2, 3 … (iterative), MISSING ≠ null (missing values are not written),
  type mismatches pass through unchanged, synthetic groups always written (`{}`), output key order = tree order,
  key collisions → `_2`, `_3` on the whole key, every edit operation with its exact guards, effects and messages.
* **Deliberate deviations (JS artefacts, not semantics; documented in README §15):** numbers stay exact (codec:
  `1.10`, `1e400`, `12345678901234567890`, `-0.0` are not turned into JS doubles); object keys keep their insertion
  order (JS moves integer-like keys such as `"10"` first); there is redo (the reference has only undo, max 100) and
  the history keeps `Settings.undo_limit` steps (≥ 200); record numbers in the preview are 0-based (`#0`). Golden tests
  compare modulo exactly these artefacts (`canon`: numbers as doubles, objects in JS key order on both sides).
* **Documents:** record kinds (array, jsonl, stream, brace_list) are the reference's root array of records. Wrapped
  documents are the reference's object root (wrapper members + the record array). Single documents are one value
  (up to `large_node_bytes`; larger → `MappingError("mapping.error.too_large")`, choose a record path). **Keyed
  documents** (one member per record, possibly millions of keys) are mapped per record: the tree describes the member
  VALUE (root kind = the record elem's kind under a synthetic "har bir a'zo" root), the output keeps the member keys
  in order (deviation: the reference builds one node per member key, which cannot scale; documented).
* **Tree source:** the default tree is inferred from a sample (`Settings.sample_size` evenly spread records, the same
  positions as `schema.sample_positions`) or from every record ("Full scan", streamed through the accumulator in a
  task). `flatten` re-inference reads the same population. Fields never seen in the sample are dropped by a mapping
  (reference semantics); `apply` reports how many records contained such fields (`mapping.warn.unmapped`) so the
  user can rerun with a full scan: for every source path S the compiled mapping knows the keys seen at inference
  (`known(S)` = child keys of S in the ORIGINAL inferred tree ∪ in the current tree); whenever an object node is built
  from a source object at S, `source.keys() - known(S)` non-empty marks the record (a C-level set difference; keys the
  user deleted on purpose are known and never counted; objects copied raw by value nodes lose nothing).
* **Huge records** (> `large_node_bytes`) cannot be transformed in memory: `apply` stops with
  `MappingError("mapping.error.huge_record", position, size)` unless `skip_huge=True` (CLI `--skip-huge`: skipped with
  a counted warning).

### 13.1 `mapping.py` — the mapping tree, operations, undo/redo, file format

```python
WILD = None                      # the array-step segment in `src` (JSON null in the mapping file; "[]" is a real key)
@dataclass(eq=False)
class MapNode:                   # the reference node (engine_map §3); field order = snapshot order
    id: int; key: str = ""; kind: str = "value"          # "object" | "array" | "value"
    src: tuple | None = ()       # absolute source path: str keys and WILD; None for synthetic / computed
    children: list[MapNode]      # array nodes: element nodes only (at least one)
    types: tuple = (); count: int = 0; pcount: int | None = None; samples: tuple = ()   # UI statistics
    synthetic: bool = False; rec_key: str | None = None; computed: str | None = None    # "depth"
    ctx: tuple | None = None     # computed nodes: src of the flattened element node
    open: bool = False; is_elem: bool = False; is_root: bool = False
class MappingTree:
    root: MapNode
    def find(self, ref) -> MapNode | None          # by id, or by display path ("[].contact.address.city")
    def parent(self, node) -> MapNode | None
    def snapshot(self) -> bytes; @classmethod restore(cls, data) -> MappingTree   # whole tree incl. open/ids
def infer_tree(values: Iterable, *, root_kind="records"|"value"|"wrapped"|"keyed", progress=None, cancel=None) -> MappingTree
    # the reference inferInto/fromAcc (iterative; insertion key order), plus setOpen for the UI
class Designer:                  # the reference operations on a tree (engine_map §8), each one undo step
    def __init__(self, tree: MappingTree, *, population=None, undo_limit=1000)
    tree: MappingTree; can_undo: bool; can_redo: bool; steps: int; changes: int   # header "N o'zgarish"
    def zone_for(self, target, node, frac: float) -> str | None   # before | inside | after | None (28 % rule)
    def move(self, node, target, zone) -> OpResult                # moveNode incl. array merge
    def delete(self, node) -> OpResult; def unwrap(self, node) -> OpResult
    def add_group(self, target, name: str | None = None) -> OpResult    # name given: the rename is a 2nd step
    def rename(self, node, name: str) -> OpResult                 # ECMAScript whitespace trim set
    def can_flatten(self, node) -> bool; def flatten(self, node, *, progress=None, cancel=None) -> OpResult
        # re-infers from the population (iterator factory over the records' values; full data or the sample)
    def undo(self) -> OpResult; def redo(self) -> OpResult; def reset(self) -> OpResult   # reset is undoable
@dataclass
class OpResult: applied: bool; changed: bool; message: str; node: MapNode | None   # message = localized toast
def badges(tree, node, parent, ctx) -> list[tuple[str, str, str]]   # (text, tooltip, theme role) per §13.5
def load_mapping(path_or_bytes) -> tuple[MappingTree, dict]          # (tree, input info); MappingError on problems
def save_mapping(tree, path, *, input_info: dict) -> None            # atomic write
```
Mapping file (UTF-8 JSON, written atomically):
```json
{"version": 1,
 "input": {"source": "C:\\data\\x.json", "kind": "array", "record_path": null, "sample": 10000,
           "full_scan": false, "records": 1234567, "created": "2026-10-18T15:00:00+05:00", "app": "0.1.0"},
 "tree": {"key": "", "kind": "array", "src": [], "is_root": true, "children": [
          {"key": "", "kind": "object", "src": [null], "is_elem": true, "children": [ ... ]}]}}
```
`src` and `ctx` are segment arrays with `null` for `[]`; `rec_key`, `synthetic`, `computed`, `is_elem`, `is_root`
only when set; UI fields (`types`, `count`, `pcount`, `samples`, `open`, `id`) are optional (ids are reassigned on
load). Optional `"known_paths": [[seg, …], …]` = the `src` of every node of the originally inferred tree (used for
the unmapped-fields count; without it only the current tree counts as known). `"designer"` (optional) may hold the
original tree (`"orig"`) so "Asl holat" works after reopening a saved mapping. Loading validates the structure (`mapping.error.*` with the JSON pointer of the bad node): known kinds, array
nodes holding ≥ 1 element node and nothing else, element nodes only under arrays, `src` prefix rules (an element
node's `src` = its array's source + `[null]`), computed nodes with a `ctx`, unique keys per parent are NOT required
(the reference tolerates duplicates after flatten). `apply` checks the tree's root kind against the input document
(`mapping.error.kind_mismatch` with both kinds).

### 13.2 `engine.py` — evaluation (exact reference semantics, compiled for speed)

```python
MISSING = <sentinel>
class CompiledMapping:
    def __init__(self, tree: MappingTree, *, limit: int | None = None)   # limit = preview (21)
    root_elems: int               # element nodes of the root array (record documents)
    def transform_value(self, value: Any) -> Any                   # the reference transform(DATA): whole document value
    def transform_record(self, record: Any, *, elem: int = 0) -> list   # items(E_elem, r) of one record (§13.3)
    def transform_wrapper(self, wrapper: Any, ...) -> ...          # wrapped documents, see §13.3
def preview(tree, record_value, *, limit=21) -> Any                   # the prototype preview (1-element unwrap)
def apply(doc, tree, out_path, opts: ApplyOptions, source=None, *, progress=None, cancel=None) -> ApplyReport
@dataclass
class ApplyOptions: jsonl: bool = False; indent: int | str | None = 2; skip_huge: bool = False
@dataclass
class ApplyReport: records_in: int; items_out: int; bytes_written: int; files: list[Path]; elapsed: float;
                   skipped_huge: int; unmapped_records: int; warnings: list[str]; passes: int
```
* The evaluator is a port of `engine_sketch.py` (`resolve`, `ev`, `ev_array`, `ev_elem`, `build_obj`, iterative
  `preorder`), then **compiled**: the tree is turned once into per-node closures with precompiled paths (tuples),
  binding keys as small integers (one slot per element node's compiled prefix and its `#d` depth), fast paths for
  nodes whose source is inside the current element (no gather/broadcast), and plain-copy shortcuts for value nodes.
  The compiled evaluator must give byte-identical results to the plain port on every test (both run in the tests).
* Values come from `codec.loads` (exact numbers) and are written with `codec.dumps` / the token Reformatter (indent
  2/4/tab/compact; non-ASCII raw; exact number tokens). Lone surrogates as in codec.
* Performance target ≥ 50 MB/s on a 2 GB array file (README table). If one thread is below the target, `apply` uses
  a process pool (`concurrent.futures.ProcessPoolExecutor`, `spawn`; workers receive the mapping file bytes once and
  blocks of record bytes, return the output bytes; the parent writes in order; `cancel` stops the pool); the number of
  workers = `min(os.cpu_count() - 1, 8)` and 1 when the file is small (< 64 MiB); memory stays within
  `memory_limit_bytes` (bounded in-flight blocks). Measured numbers go into the README.
  As built (stage 6): `engine.auto_workers` also caps the count at half the memory budget / `WORKER_MEMORY` (80 MiB:
  a worker measures ~62–66 MB, plus its share of the parent's blocks in flight) — 3 with the default 512 MiB, which
  keeps the whole program under the §12 300 MB (2 GB benchmark: `apply` 264 MB / 97.5 MB/s, `flatten` 299 MB /
  129 MB/s; with 4 workers it was 321–355 MB for at most 20 % more speed).

### 13.3 Streaming decomposition (engine_map §14, verified on 4 000 cases)

* **Record documents** (array, jsonl, stream, brace_list; the root is an array node): `output = [ items(E_j, r) for
  E_j in the root's element nodes (tree order) for r in records (position order) ]`; `items(E_j, r)` evaluates `E_j`
  with its array source bound to record `r` alone; a record whose source path does not reach an array contributes
  nothing. The common case (one root element node, `src = [WILD]`) is ONE streaming pass over `doc.iter_blocks`;
  several root element nodes (an array merged into the root) need one pass per element node (`ApplyReport.passes`).
  When no `(E_j, r)` reaches an array at all, the export is the first non-array value reached (raw) or `null`
  (reference rule; this needs a full pass and is reported with `mapping.warn.not_array`). Output: a JSON array
  (`.json`) or JSON Lines (`jsonl=True`, one item per line).
* **Wrapped documents** (object root = the wrapper): the wrapper value is parsed with the record array replaced by
  the records stream. Wrapper members are evaluated in tree order; nodes whose value does not depend on the records
  are computed first (small) and written in order; the record array member streams its items (`items` rule above with
  the record array as source); a wrapper-level node that gathers from the records (`data[].x` moved to the wrapper)
  streams its own pass. Records bound inside the record element see the wrapper as ROOT for unbound paths (broadcast
  of wrapper fields into records works).
* **Keyed documents:** per record: the member value is transformed by the tree (root = the member-value tree), the
  output is `{key: transformed, …}` in record order (`jsonl=True`: `{"key": value}` lines). Records whose transform is
  MISSING are not written.
* **Single documents:** `transform_value` on the parsed value (≤ `large_node_bytes`).
* Every pass reads a pinned document (`doc.pinned()`; edited documents are mapped with their edits); progress in bytes
  over all passes; `doc.ensure_unchanged()` before the atomic commit; the source is never the target.

### 13.4 CLI

```
jsonreshape apply    MAPPING INPUT OUTPUT [--jsonl] [--compact | --indent 2|4|tab] [--record-path P] [--skip-huge]
                     [input options]
jsonreshape flatten  INPUT OUTPUT --children-key KEY [--depth-field NAME] [--mode nested|records] [--jsonl]
                     [--compact | --indent N] [--sample N | --full-scan] [input options]
```
* `apply` loads the mapping (`mapping.error.*` → exit 1), opens INPUT (forced `--record-path` overrides the mapping's
  recorded one; a kind mismatch is exit 1), runs `engine.apply`, prints the report (warnings incl. unmapped records and
  passes) — exit codes as §10.
* `flatten` builds the default tree from the sample (or every record with `--full-scan`) and applies one recursive
  flatten, then `apply`: `--mode nested` (default) flattens the record-level array field `KEY` of every record
  (AGENT_PROMPT test 2: `children` → child1, child1.2, child1.2.1, child1.2.3, child2 with `_depth` 1, 2, 3, 3, 1);
  `--mode records` flattens the record list itself (records + all descendants, engine Ex 18). `--depth-field`
  renames the computed node (default `_depth`; a collision with a data field gives `_depth_2`, reported as a warning).
  A `KEY` that is not a flattenable array (`can_flatten` false) → exit 1 with the reason.

### 13.5 GUI: the structure builder (`gui/mapview.py` + `gui/mapmodel.py`)

* A new view kind **`mapping`** ("Tuzilma quruvchi", Ctrl+6) in `doctab.VIEW_KINDS` / `VIEW_MODULES` / `view_label`
  and the main window's view menu; the schema view gets an action "Tuzilma quruvchi…" that opens it (signal
  `mapping_requested`, wired in `DocumentTab._hook_view`). One designer per session (the tree survives switching
  views; it is dropped when the document is reopened with another kind/record path).
* Layout (prototype A6): left the structure tree, right the live preview; toolbar: Open mapping…, Save mapping…,
  Undo (Ctrl+Z), Redo (Ctrl+Y / Ctrl+Shift+Z), Reset ("Asl holat"), Expand all / Collapse, Full scan, Compact
  toggle, **Export with mapping…** (Ctrl+E inside the view → export dialog in mapping mode: JSON / JSONL, indent,
  target; runs `engine.apply` as a heavy task through `ExportRunner`); a hint line (the three drag rules); the stats
  line `N maydon · M o'zgarish`.
* Tree rows (prototype A7, exact texts through i18n): caret, label (root = file name; elems `har bir yozuv` /
  `har bir element` / keyed `har bir a'zo`), type chips (`{ }`, `[ ]`, `abc`, `123`, `t/f`, `null`, `bo'sh` for an
  always-empty elem, `123` for computed), meta (`NN%` when present in part of the objects, `N ta` for elems,
  `N ta yozuv` for the root), badges in order: `yangi guruh`, `daraja raqami`, `rekursiv · KEY`, `manba: PATH`,
  `ko'chirildi` (tooltip `Asl joyi: PATH`), `asl nomi: NAME`, `ro'yxatga yig'iladi` / `har elementga takrorlanadi`
  (tooltips as the prototype), samples, and hover action buttons (group, unwrap, flat, rename, delete) with the
  prototype's enabling rules. Theme roles: accent, muted, ok, array, warn, bool (prototype colours = theme.py).
  Implementation: `QTreeView` over a small model (`mapmodel.py`; the tree is bounded by the schema limits) with a
  delegate painting chips, pills, samples and action buttons; 400 children per node then a "yana N ta maydonni
  ko'rsatish" row.
* Drag & drop exactly as `zoneFor` (28 % zones; root/elem rows cannot be dragged; forbidden cursor when the zone is
  None; indicator: line before/after at the row indent, box for inside; auto-scroll; Esc cancels) implemented with
  mouse events on the view (no QDrag; the pure `zone_for` is unit-tested). Keyboard alternatives (AGENT_PROMPT §11):
  F2 rename (inline editor), Del delete, Ctrl+X then Ctrl+V on a target (paste = move "inside" when allowed, else
  "after"), Alt+↑/↓ move among siblings, Insert = new group, Ctrl+Shift+U unwrap, context menu with every operation
  and its disabled reason. Messages (toasts) go to the status bar / a transient label.
* Preview (prototype A10): "Natija" / "Asl" toggle, record navigator `‹ #N ›` (0-based, follows the session's current
  record when it changes, own navigation otherwise), 60 ms debounce, `limit = 21` (first 20 items + `… davomi
  eksportda`), strings cut at 300 characters, 200 keys; pretty JSON with the text view's colours; the record value is
  read in a silent task (never parsed on the GUI thread above 1 MiB); errors shown inline.
* The tree comes from `fields.get_schema`-independent inference: a silent task reads the sample records
  (`schema.sample_positions` over the current document, or every record for Full scan in a heavy task with progress)
  and runs `infer_tree`; wrapped documents add the wrapper members (parsed W with the records placeholder); keyed
  documents use the member values. The designer is rebuilt when the document is reopened; edits (stage 3
  `data_changed`) only refresh the preview (the tree stays; Full scan again on demand).
* Undo/redo of the designer is its own history (not the document's edit layer); Ctrl+Z/Ctrl+Y inside the mapping
  view act on the designer (the window-level edit undo is disabled while the mapping view has focus).

### 13.6 Tests (acceptance 1–10)

* `tests/golden/engine_harness.js` (copied from the analysis) + `tests/golden/mapping/*.json` committed goldens
  generated from the reference for tests 1–10 and the engine_map examples (identity for array / object root / keyed
  tree `"child1": {"child1.2": {...}}`, recursive flatten, move, unwrap, gather incl. missing `orders`, broadcast,
  group + rename, merge, the drag & drop matrix incl. the four §7 rules, collisions, type mismatch, `_depth` rules).
  A test compares the Python port with every golden (`canon`), and, when `node` is on PATH, regenerates the goldens
  from `json-tuzilma.html` and compares again (skipped without node).
* Differential fuzzing (hypothesis + the fuzz driver): random records and random edit scripts give identical outputs,
  trees (ignoring ids) and op results (applied/changed/message) for the plain port, the compiled evaluator and (with
  node) the reference.
* Streaming: `engine.apply` over every document kind equals `transform_value` of the whole parsed value (small files),
  multi-pass root merges, wrapped gathers/broadcasts, keyed records, huge records (error / skip), cancellation, both
  core backends, exact numbers and Unicode.
* CLI `apply`/`flatten` end to end; GUI (pytest-qt): building the tree, every operation through buttons, keys, context
  menu and drag & drop (QTest mouse events, zones), undo/redo, preview updates, save/open mapping, export with mapping.

### 13.7 As built (module docstrings are authoritative)

* **One inference for CLI and GUI**: `mapping.infer_document(doc, *, sample, progress, cancel) -> DocumentTree`
  (`.tree`, `.wrapper`, `.record_path`, `.source`, `.full`) with `document_wrapper`, `sample_source`,
  `document_values`; `cli flatten` and the builder both use it.
* **`gui/mapmodel.py`**: `MappingState` per session (`state_for` / `forget_state`; signal `replaced` when a new tree
  (full scan, opened mapping, reopen) replaces the old one — open menus close, drags cancel, a typed rename is dropped;
  one parsed wrapper shared through `wrapper_cache`), `MapModel`, preview helpers (`PREVIEW_LIMIT` 21,
  `PREVIEW_MAX_BYTES` 8 MiB, `VALUE_KEEP_MAX` 1 MiB), `MORE_STEP` 400.
  **`gui/mapview.py`**: `MapView` (`view_id` "mapping"), `MapTree`, `MapDelegate`, `PreviewPane`, `RecordSpin` (a 64-bit
  record navigator: documents with more than 2³¹−1 records).
* **Window**: Ctrl+6 comes from `VIEW_KINDS`; Ctrl+Z / Ctrl+Y and Edit ▸ Undo / Redo go to an active view that has its
  own `undo()` / `redo()`; Ctrl+E in the builder = export with mapping (`MainWindow.show_export(*, mapping=True)`);
  export dialog mapping mode: JSON / JSON Lines only (`.gz` refused), indent, "skip huge", scope view / all,
  `engine.apply` through `ExportRunner`, default name `<name>_yangi.json`.
* **Deviations** from §13.5: a single click selects (caret, double click or Enter toggles); an unedited sample tree is
  inferred again on reopen (edited, file and full-scan trees are kept; dropped on another kind / record path);
  operations are refused while a full scan runs or a flatten is planned; "Asl holat" is also enabled right after
  opening a saved mapping whose tree differs from the stored original; the mapping file's `input` describes what the
  tree was made from; indentation 18 px per level up to 35 % of the row width, then 6 px per level up to 60 % (no
  horizontal scrolling); records over 8 MiB are not previewed; huge records are left out of the inferred tree and
  counted in the header; wrapped documents whose records are object members are refused (as the engine); the header
  "Namunadan: N / M" keeps the numbers from when the tree was built.
* **Fixed after the GUI round**: `engine._Compiler._rec_items` returned `list.append` for an identity element node
  (scalar / null / list records raised TypeError or corrupted a list record) → a proper `(rec, out)` function
  (regression `test_engine_apply.py::test_scalar_and_mixed_records_with_the_default_tree`).

---------------------------------------------------------------------------------

## 14. Tools (stage 5): statistics, duplicates, JSON Schema, file tools, repair, diff, smart values

AGENT_PROMPT §4.5, §5 ("show in tree" from statistics), §9, §10, §11 (context menu "statistika"), §13 (`stats`,
`split`, `merge`, `repair`, `diff`, `convert`), tests 22–23. The analyses in scratchpad `s5/` are part of this spec
(real line numbers, measurements, prototypes): `s5/map_stats.md`, `s5/map_convert.md`, `s5/map_repair_diff.md`.
Where this section is short, the design written there decides.

### 14.0 Decisions (README §15)

* Statistics: exact in-memory counting up to a budget (`memory_limit_bytes // 8`), then HyperLogLog (p=14,
  ±0.8 %) for distinct counts and Misra–Gries (10 000 counters) for the top values, both marked "≈" with their error
  bound; "Aniq hisoblash" / `--exact` reruns exactly with spilled hash runs (dupes machinery). The stats panel's
  default scope is the current view (filter/sort/search result); "Barcha yozuvlar" is an option.
* JSON Schema: an OWN validator for draft 2020-12 (`jschema.py`) — exact numbers, ECMA-262 regex semantics, offline by
  construction, ~10× faster than the `jsonschema` package; `$dynamicRef`/`$dynamicAnchor`, remote/absolute `$ref` and
  custom `$vocabulary` are refused with a clear error (never silently ignored); the official JSON-Schema-Test-Suite
  (draft2020-12, MIT, vendored with its LICENSE under `tests/data/json-schema-test-suite/`) is the conformance test
  (documented skips: dynamicRef, refRemote, vocabulary, optional/format assertions). `jsonschema` is a dev-only
  differential oracle (`[project.optional-dependencies].dev`). Generated schemas are document-level and permissive by
  default; strict constraints (min/max, enum, `additionalProperties: false`, formats) are opt-in.
* New CLI commands beyond §13: `dupes`, `schema`, `validate`, `extract`; new exit code **5** = "data does not match"
  (`validate` with issues, `diff --exit-code` with differences). Codes 0–4 keep their meaning.
* Split keeps the kind, encoding, compression and the wrapper in every part (`--no-wrapper` drops it); `--parts N`
  balances by bytes (`--balance records` alternative); `--records N` = records per part; `--size` uses binary units
  like `--memory-limit` (`500MB` = 500 MiB); names `<stem>_<k><ext>` with `k` 1-based, zero-padded to the digits of N;
  all-or-nothing commit; partial documents refused (repair first); JSONL broken lines need `--drop-broken`.
* Merge v1 writes JSON/JSONL through the writer (re-formatted; exact tokens): an object root only when every input
  has member records; the first input's wrapper is kept; `--dedupe-by KEY` (`$` = whole record, `$key` = member key)
  with keep first (earliest input) by default.
* Repair: the fix catalogue and decisions D1–D10 of `s5/map_repair_diff.md` §3.1/§3.6 (NaN/Infinity → null by
  default, `--nan string`; missing commas only between self-delimited tokens; unquoted keys / Python literals /
  `undefined` repaired, unquoted string values unrepairable; mismatched closers unrepairable; invalid UTF-8 → U+FFFD,
  stop above 1 000 occurrences; output kind = input kind with brace_list/stream → array, JSONL stays JSONL; never in
  place; unrepairable → nothing written without `--allow-partial`).
* Diff: canonical equality (numbers by exact value, member order ignored; `--numbers text` option), arrays aligned by
  content by default (or by index, or by an element key reporting moves); files up to `diff_align_max_bytes`
  (new setting, default 200 MB) are diffed in full without a key; bigger files need `--key` (streaming hash
  sort-merge, any size) or `--by-position`; duplicate keys pair i-th with i-th and warn.
* Smart values: unix seconds/milliseconds and ISO dates get a tooltip in Asia/Tashkent time as a fixed UTC+05:00
  (exact since 1992, no DST; no `tzdata` dependency; noted in the tooltip for older dates); URLs are clickable after a
  confirmation ("open in the browser?") — the program itself sends nothing (§1.6 offline rule unchanged; §11.0 is
  amended: `QDesktopServices.openUrl` on `http(s)` only after an explicit user confirmation); `#rgb`/`#rrggbb` show a
  colour swatch; base64 images (PNG/JPEG/GIF/WebP magic) show a thumbnail in the full-value window [I].
* XLSX export keeps `xlsxwriter` (measured 7–10 MB/s; documented in README §14); a faster own sheet writer is an
  optional stage-6 item (superseded in stage 7: own writer `writer_xlsx.py`, see §8).

### 14.1 `stats.py` — per-field statistics

API, semantics, algorithm and sketches exactly as `s5/map_stats.md` §4: `ValueCount`, `NumberStats`, `StringStats`,
`FieldStats`, `field_stats(doc, fields, source=None, *, top=20, exact=False, sample=None, memory_limit=None,
progress=None, cancel=None, on_partial=None) -> list[FieldStats]`. Pinned document, `source.gen` check
(`edits.error.stale_scope`), presence semantics of `SchemaBuilder`, canonical equality from `dupes`, exact numbers
(int + unbounded `Decimal`, the 4 300-digit `int()` guard), PathExtractor fast path (parse strategy for `..`/`**`),
huge records via `stream_probe`, deterministic blake2b hashing (never Python `hash()`), progress in bytes, cancel per
4096-record chunk, `ensure_unchanged()` before returning. `schema.py` gets an integer counter in `_Acc`/`FieldInfo`
(`to_dict` key read with `.get`) and exposes `default_columns`/`field_paths` as core helpers.

**Histograms (stage 7, as built).** Every numeric field keeps, besides its exact extremes, the material of a
histogram in its `_Agg` (`_Sample`): the exact `value → count` of its distinct finite numbers while they are at
most `k`, else a uniform reservoir sample of `k` occurrences — Li's Algorithm L with weighted insertions (a
dict entry folded at the end of the exact phase, or at a spill, counts as `count` occurrences; the skip
computation costs O(k·log(n/k)) random numbers per field, nothing per value) and merges (streamed records'
accumulators) that draw the union's sample and re-seat the acceptance threshold from `Beta(k, n − k + 1)`.
`k = max(HIST_MIN = 1 024, min(HIST_SAMPLE = 65 536, (budget / 4) / (fields × HIST_ENTRY_COST = 200)))` — with
the default budget (`memory_limit_bytes / 8` = 64 MiB) one or two fields get 65 536 / 41 943 samples. The
generator is seeded with blake2b of the field text, so a run is reproducible. Numbers beyond the Decimal range
are left out (`_Sample.skipped`); `NumberStats` is unchanged. String lengths: exact counts per length up to
`LEN_COUNTS` = 4 096 distinct lengths (`_LenCounts`), then grouped by a doubling width. `FieldStats.histogram
(bins=32) → Histogram(edges, counts, exact, sample_size)` with `nice_edges` (steps 1, 2, 2.5, 5 × 10ᵏ aligned
to multiples of the step, ≤ `bins` bins; integral data: integer edges, half-open bins, the last edge beyond the
maximum so every integer has its own bin while they fit; one distinct value: one bin `[v, v]`; one bin over a
range with 0 inside: the exact extremes) — exact Decimal / int arithmetic, never floats (`1E+400`, 5 000-digit
integers); counts of a sampled field are scaled to the total. Live snapshots carry the material (the current
dict is folded into a copy of it). Memory: `_AccSet.total()` includes the histogram bytes (`hist_bytes()`), the
sketch bytes that resize Misra–Gries do not — every earlier result (`to_dict()`, the CLI text, the sketch
bounds) is unchanged; the stats tests compare against the same references as before. `format_histogram`
renders a text block (`stats.hist.*` keys); the CLI prints it per field with `stats --histogram` / `--bins N`
(`cli._stats_histogram_text`: "Histogram of the numbers" / "of the string lengths", only the kinds the field
has) and `--json` adds `histogram` / `length_histogram` per field (`cli._histogram_data`: `bins`, `edges` as
exact JSON numbers — `int` or `Decimal(number_text(e))` written raw by `codec.dumps` —, `counts`, `exact`,
`sample_size`; `null` without numbers / strings). `cli.HIST_BINS` / `HIST_BINS_MAX` mirror the stats constants
so that building the parser does not import `stats` (a test keeps them equal).

**Memory cost per field**: at most `k × 200` accounted bytes for the numbers (65 536 × 200 = 12.5 MiB at the
default budget, a quarter of the budget shared by the fields of a call) plus at most 4 096 × 100 = 400 KiB for
the string lengths; the real objects are ≤ 65 536 ints / Decimals (28–104 bytes each) in a list or dict.

### 14.2 `dupes.py` extensions

Backward compatible (s5/map_stats.md §7.2, s5/map_convert.md §4.3, s5/map_repair_diff.md §4.4a): `keep="none"` (mark
every member of a group of ≥ 2), optional `top` largest groups, a public generic group iterator (`iter_groups`),
public `KeyHasher` / `HashRuns` helpers (used by stats exact mode and diff key mode), and
`find_duplicates_many(docs, key, *, keep="first", memory_limit=None, progress=None, cancel=None) ->
MultiDuplicateReport` with `keep_source(i)` (ascending kept positions of input i, with `__len__`/`__getitem__`/
`iter_chunks`).

### 14.3 `jschema.py` — JSON Schema generation and validation

`generate_schema(schema: Schema, *, doc=None, document=True, strict=False, title=None) -> dict`,
`compile_schema(schema_value) -> CompiledSchema` (closures, cached per subschema, `$ref`/`$defs`/`$id`/`$anchor` in the
same document, `unevaluated*` via annotations where needed, `format` annotation by default with optional assertion),
`validate(doc, compiled, source=None, *, scope="auto"|"record"|"document", max_per_record=10, max_issues=1000,
formats=False, progress=None, cancel=None) -> ValidationReport` (issues `Issue(record, key, path, keyword,
schema_path, params)` with localized messages `jschema.error.<keyword>`, invalid records as a RecordSet, counts per
keyword, top paths), `iter_issues(...)` (streaming for export), document decomposition per kind and the huge-record
policy (records ≤ 8 MiB parsed; larger skipped with a counted warning in v1) — all as `s5/map_stats.md` §8.

### 14.4 File tools: `filetools.py` + writer/saver extensions

As `s5/map_convert.md` §3–§5: `saver._Planner` gains a position range (`p0, p1, first, last, wrapper`) and a public
`write_part(...)`; `filetools.SplitOptions`, `SplitPart`, `SplitReport`, `plan_split(doc, opts)`, `split(doc, opts,
source=None, *, progress, cancel)` (engine A byte copy for "same kind", engine B = `writer.export` for another format,
a view or a filter; CSV/XLSX parts share the columns of one `discover_columns`); `writer.export_many(inputs, out,
opts)` and `filetools.MergeOptions` / `MergeReport` / `merge(docs, out, opts, *, progress, cancel)` with
`find_duplicates_many`; `filetools.extract(doc, out, opts, *, path=None, node=None, source=None, ...)` (the record
path form, one node, or a record-relative pattern gathered from every record with PathExtractor byte copies);
`export_node` (+ `_stream_value`, `_write_lines`, `_reformat`) moves from `gui/exportdialog.py` to `writer.py`;
`fileio.commit_files(staged, finals)` (from `writer._commit_parts`, with `replace_posix`) and `fileio.free_space`;
writer gaps G4/G5/G7 of map_convert §1.7: SQLite `_jsonreshape` metadata table + `REFERENCES` + `_wrapper` table +
`_ord` for unsorted sources, the `writer.note.wrapper_not_written` notice for jsonl/csv/xlsx/sqlite, `.json.gz` /
`.jsonl.gz` / `.csv.gz` outputs; CLI `--select SPEC` (0-based `N`, `A:B`, `A:`, `:B`, comma lists) on
filter/sort/convert and `--node LOCATION` on convert.

### 14.5 `repair.py` — tolerant repair with a report

As `s5/map_repair_diff.md` §3: the H2 hybrid (strict core Scanner copies clean records verbatim; a Python tolerant
`Tokenizer` re-reads broken records from the last record end with a known sync state; output committed only at record
ends; truncation = commit up to the last record end + closers), the fix catalogue with stable codes and categories
(syntax / data / dropped), `RepairOptions`, `RepairFix`, `RepairProblem`, `Truncation`, `RepairReport` (+ `write` of
every fix from a temp JSONL), `UnrepairableError`, `repair_file(source, target, opts, *, check_only=False, settings,
progress, cancel)`, output validation by the core Scanner, atomic write, user-file offsets and 1-based line/column
equal to the reader's error computation. The identity property `accelerate=True` ≡ `accelerate=False` (output AND fix
list, any window size) is tested. The optional core addition `Scanner.error_context` (H1) and a Rust tokenizer are
NOT part of stage 5 (documented options).

### 14.6 `diff.py` — structural diff

As `s5/map_repair_diff.md` §4: `Change`, `DiffOptions`, `ValueDiff`, `diff_values`, `RecordDiff`, `DiffReport`,
`diff_files(doc_a, doc_b, *, key=None, mode="auto"|"key"|"align"|"position"|"document", ...)`, `iter_record_diffs`,
`diff_nodes` (memory-bounded over the Node API), `write_diff(...)`; compact fingerprints (Reformatter + blake2b-64) skip
equal records without parsing; key mode = streaming hash sort-merge on `dupes.KeyHasher`/`HashRuns`; align mode =
patience/LIS + SequenceMatcher in gaps; pinned documents (edited documents compare by positions).

### 14.7 Smart values (`smartvalues.py`, Qt-free)

`detect(text_or_token, kind) -> SmartValue | None` for: unix seconds (1e9–4e9 range) and milliseconds (1e12–4e12),
ISO 8601 dates/date-times, URLs (`http(s)://…`, `mailto:`), colours (`#rgb`, `#rrggbb`, `#rrggbbaa`), base64 images
(by magic bytes after decoding a ≤ 64 KiB prefix) — each with a display tooltip (Asia/Tashkent UTC+05:00 time with
the weekday in Uzbek, colour hex + swatch spec, image size). Pure, unit-tested, used by the GUI views.

### 14.8 CLI

New commands (usage lines of `s5/map_stats.md` §6–§8 and `s5/map_convert.md` §7.3 / `s5/map_repair_diff.md`
§3.7, §4.6): `stats`, `dupes`, `schema`, `validate`, `split`, `merge`, `extract`, `repair`, `diff`; `--select` /
`--node` on the existing export commands; `merge` expands `* ? [` globs itself (cmd.exe/PowerShell do not) sorted
naturally; `_document(ctx, *, build=True, path=None)` for multi-input commands; exit codes as §14.0. All texts in the
`cli` section (uz + en); one agent owns cli.py.

### 14.9 GUI

* **Statistics dock** `gui/statspanel.py` (`DOCK_PANELS` "stats"): field combo (completer from the schema + `$key`),
  scope (current view / all records), compute / cancel / exact, live partial results (~1 s), presence block, types
  bar, distinct (exact or ≈ with the error), numbers (min / max / mean / sum, exact texts), string lengths, top-20
  table (value, count, records for multi-valued fields, %, bar); double-click/menu on a value → filter
  (`DocSession.filter_by_condition(Condition)` — a refactor of `filter_by_value`; texts verified in map_stats §5.3),
  "Daraxtda ko'rsatish" (first position of the value), stale marker after `data_changed`/view changes. Entry points:
  "Statistika" in `actions.node_menu` (§11 [M]), the table header menu, the schema view (action + double-click), Tools
  menu.
* **Duplicates dialog** `gui/dupesdialog.py` (map_stats §7.1): key, keep, scope; counts; 20 example groups; show the
  duplicates as a record set; export; "delete duplicates…" → the stage-3 bulk dedupe preview.
* **JSON Schema**: Tools ▸ "Schema yaratish…" (generate from the cached full schema or a full scan; document/record,
  strict) and "Schema bo'yicha tekshirish…" (`gui/validatepanel.py`: schema file, scope, options; issues stream into a
  VirtualGrid `#record | path | message | keyword`; activation reveals the node; invalid records as a record set;
  export CSV/JSONL).
* **File tools** `gui/filetoolsdialog.py`: `SplitDialog` (source whole document / current view, mode with a live plan,
  output folder and name pattern, format), `MergeDialog` (file list incl. open tabs via `session.snapshot()`, output,
  JSON/JSONL, indent, sort keys, keep wrapper, dedupe); both through `ExportRunner`; ExportDialog gets the
  "selected records" scope and CSV/XLSX/SQLite for array/object nodes; node menu "Faylga ajratish…".
* **Repair** `gui/repairdialog.py`: entry from the JSON-error and broken-lines bars ("Ta'mirlash…"), the brace-list
  warning bar, and Tools; target, options, Check / Repair (heavy task with progress); the report (summary by fix with
  first location links to the raw view, truncation and unrepairable boxes, a VirtualGrid of fixes, save report,
  "open the repaired file", "save partial result" when a problem stopped it).
* **Compare** `gui/difftree.py` (pure model), `gui/diffview.py` (one VirtualGrid tree `Kalit | A | B`, tinted rows by
  status, character highlights, F7/Shift+F7, array matching options, swap, text mode with two synchronized editors),
  `gui/comparewindow.py` (non-modal; file mode with a results grid read from `results_file` + a DiffView of the
  selected pair; node mode via "Taqqoslash uchun belgilash" / "Belgilangan bilan taqqoslash" in the node menu and
  "compare the two selected"); `vgrid` gets `GridSource.row_background(row)` + `Cell.bg`; `theme` roles
  `diff_added/removed/changed/moved` (light and dark).
* **Smart values** in the tree value column, table cells and the full-value window: tooltips for times/dates,
  underlined URLs (Ctrl+click → confirm → open), colour swatches, image thumbnails in the full-value window.
* **Schema view**: show nulls / missing (column or tooltip) and the integer share.

**Chart area (stage 7).** Between the summary and the top values the dock holds a `_Chart` (QPainter; QtCharts
is excluded from the executable) with a kind combo: `hist` (the number histogram, `HIST_BINS` = 32), `lens`
(string lengths) and `top` (the top 20 values as horizontal bars with their share); the default follows the
most frequent type, a chosen kind is kept while the following results offer it. Bars hit-test by column / row;
hovering updates the tooltip, the info line and the accessible description; a click applies the bar's filter —
a histogram bin becomes the quick filter keyed by the field (`f >= lo and f < hi`, the last bin `<=`, integral
bins inclusive, one value `f == v`; lengths through `len(f)` with the type guard of the length rows, none for
multi-valued fields), a top value its `==` builder condition. The "≈" title and a tooltip mark a sampled
histogram / grouped lengths. Colours are roles only (`accent`, `hover`, `fg`, `muted`, `line`, `panel`);
the chart hides when the field has no values; the splitter lets the user drag it shut. Deviation from the
brief: a range is two comparisons and `DocSession.filter_by_condition` takes one builder `Condition`, so bins
go through the existing expression-text path (`set_quick_filter`) like the type and mixed-type length filters.

### 14.10 As built (stage 5 after two reviews and the follow-ups; module docstrings are authoritative)

* **stats.py**: the budget is checked inside a chunk (`FEED_SLICE` 16 384 values; mid-record spill with markers so a
  record counts once per value), zero-copy quads, `array('Q')` buckets, records with more commas than
  `memory_limit_bytes / 256` streamed for multi-valued fields, lazy `_resolve` (each record's tokens read once per
  field; cancel checked); one token cache shared by all fields (≤ 65 536 entries and budget / 8); Misra–Gries counters
  per field `k = (budget/2) / (sketched fields × 500 B)` within `[max(1000, 2·top), 10 000]` (`MG_MIN`), bound
  `D ≤ values/(k+1)` reported as `top_bound`; HyperLogLog with Ertl's improved estimator (bias ≤ 0.24 %); spilled
  fields keep a small HLL + MG for monotone live snapshots (`distinct_rse`, `top_bound`); `NumberStats.sum_skipped` +
  `stats.warn.sum_skipped` (numbers beyond any exact sum are counted, never make the rest approximate); the sum is
  written with the finest scale of every spelling it saw, in every mode and on both cores.
* **jschema.py**: `validate(..., on_issues=)` streams issue batches (≤ `ISSUE_BATCH` 256, flushed every
  `ISSUE_FLUSH_SECONDS` 0.25 s at the check points, never after an exception); exact numbers beyond Decimal
  (`_BeyondDecimal`); count keywords clamped to `sys.maxsize`; whole-document fallback `_WholeCtx` (per-record caps,
  every invalid record counted, ≤ 500 000 errors kept, `jschema.warn.whole_dropped`); repeated source positions
  validated once; stricter ECMA-262 regex (Python-only `(?…)` groups and possessive quantifiers refused, `{,n}`
  literal); `ValidationReport.complete` (`skipped == 0`; `ok` unchanged).
* **stream.py / reader.py / schema.py / mapping.py (ijson safety)**: ijson 3.6 yajl2_c leaves a pending ValueError for
  an integer over 4300 digits and emits `("number", NULL)` → reading it is an access violation. Heads flagged by
  `stream.needs_exact()` (a digit run > 640 or a surrogate escape not in a plain pair) are parsed by the exact
  pure-Python `stream.exact_events` (no ijson, no recursion; 5–10 MB/s, heads ≤ 4 MiB); huge records use a careful
  mode (long ints exact, lone low surrogates → `?`); `mapping._parse_iterative` uses `exact_events`; an AST test
  forbids direct ijson parsing outside the protected modules. `schema._num_key` / `is_integral` exact beyond Decimal.
* **codec.py**: `dumps` returns a compact copy (an orjson result holds its ≥ 4 KiB output buffer: kept results
  ballooned memory); `dumps_transient` keeps the old no-copy behaviour for results written / concatenated / decoded at
  once (writer, engine, sortkeys, query, bulk runner); `diff._token` and `stats._dump` keep compact copies.
* **engine.py**: `_RecursionRoom(depth)` raises the recursion limit for an evaluation and restores it (re-entrant,
  counter under a lock); `_spawn_safe()` (and `bulk._spawn_safe()`) is True in a frozen executable.
* **repair.py**: extra fix code `line_split`; `dropped_tail` (category `dropped`) for every partial cut;
  `RepairReport.data_path`, `check_report_path` (a report is never written over the source, its prepared copy or the
  repaired output); `fixes_file` None when all fixes fit; `check_only` never raises; wrapped documents: every child of
  a container on the record path is a cut point; top-level streams split like the strict stream scanner.
* **filetools.py**: `part_names(doc, opts, source=None, *, count) -> list[Path]` (exactly the files split writes),
  `SplitOptions.remove_stale` (default False; plausible earlier parts of the same pattern removed in the same commit,
  never the source / read-only / edited files), `SplitReport.removed`, stale-parts warning naming the files; split /
  merge / extract (and every writer output, `writer.edited_targets` / `refuse_edited_targets`) never replace a file
  with unsaved edits in a tab or a pending layer (`filetools.error.part_open_edited`).
* **diff.py**: `DiffReport.sources`, `write_diff(..., sources=)` refuses the inputs up front; public `align`,
  `OP_EQ / OP_PAIRS / OP_DEL / OP_INS`; SequenceMatcher limited to 4 M element pairs (above: patience, then Myers capped
  at 1 000 edits, then position pairing); sampling above `PATIENCE_MAX` (1 M per side).
* **fileio.py / index.py**: `commit_files(remove=)`, `fileio.error.restore_failed` names every failed restore,
  `.jrold` asides older than 600 s swept; `sniff` needs the UTF-16 NUL pattern in two code units; a UTF-16 file cut
  inside a character drops the 1–3 byte tail (`fileio.warning.utf16_truncated`); `same_file`,
  `scan_prepared_entries`; index eviction keeps a per-process tally and rescans at most every `SCAN_INTERVAL` 600 s.
* **editstore.py**: `layer_sources()` (one cheap index read); checksummed ranges ≥ 64 MiB are verified by up to 4
  threads at restore (same check, still before restore returns).
* **writer.py** (P6): `export(..., workers=None)`, `discover_columns(..., workers=None)`, `ExportReport.workers`: CSV
  rows and the CSV/XLSX column scan run in worker processes (the `bulk.auto_workers` / `_spawn_safe` rule; 1 MiB jobs,
  ≤ 2 × workers in flight, ordered, byte-identical to one thread); huge, undecodable and > 4 096-bracket records are
  done in the calling thread; BrokenProcessPool → the calling thread; the scan merge is exact unless map-like paths,
  more than `SCAN_MAX_PATHS` paths or inferred rows mode (then the one-thread scan); XLSX value preparation in ≤ 2
  workers with xlsxwriter in the parent; gzip outputs feed zlib 1 MiB pieces (pooled `.csv.gz` byte-identical).
  (Superseded in stage 7: XLSX has its own worker-pool writer and SQLite a worker-pool export — §8.)
* **CLI**: every output refuses an input (any spelling / hard link) and a file with unsaved edits before any work;
  `split --remove-stale`; `diff -o` checked up front; `validate` exit 5 when records could not be checked (`--json`
  `ok` = the exit verdict).
* **GUI round A/B + leftovers**: the tools are wired into Tools ▸ (compare / stats, dupes, JSON Schema ▸, repair /
  split, merge), the stats dock, the node menu (Statistika, split node, compare mark / with marked; two selected nodes
  in tree/table), the tab bars ("Ta'mirlash…"), the export dialog (scopes view / all / selected / node / records,
  `writer.export_node`, node CSV/XLSX/SQLite, keep wrapper for every format); smart values in tree / table / full-value
  window (`vgrid` `Cell.link` / `link_span` / `swatch`, `link_activated`, `widgets.open_url` asks first); schema view
  null / yo'q column, integer share, stats / dupes signals; `fields.store`; `DocSession.filter_by_condition`; every
  tool output checked with `editing.target_conflict` (verdicts unsaved / locked / pending / open) through
  `searchpanel.open_target_check` / `reload_tabs` / `target_conflict_text`.

---------------------------------------------------------------------------------

## 15. Polish (stage 6): command palette, key settings, session, workspace, themes, "Open with", `.exe`

AGENT_PROMPT §5 ("oxirgi sessiyani tiklash: ochiq fayllar, joylashuv, ochilgan tugunlar, filtrlar"), §11 (command
palette [M], shortcuts in settings [K], "Open with" [K], themes [M], workspace [K]), §2 (PyInstaller `.exe` [K]),
§14 test 26, §12 (full benchmark), §16 item 6. Decisions (README §15, taken by recommendation):

### 15.0 Decisions

* **Command palette** (`gui/palette.py`, **Ctrl+Shift+P**): a popup over the window (line edit + list); entries =
  every main-window `QAction` that has a text (menus, submenus, docks, view switches — walked from the menu bar, so
  new actions appear automatically), the current view's edit commands and the current node's context-menu commands
  (as `actions.node_menu` builds them), "Ochish: <recent file>", "Tab: <open tab>". Each row: text, menu path
  ("Fayl ▸ Saqlash"), shortcut. Matching: case- and apostrophe-insensitive, Latin ↔ Cyrillic
  (`textmatch` folding), word-prefix and subsequence scoring (exact prefix > word start > subsequence); the last 20
  used commands first when the query is empty (`gui_state["palette_recent"]`). Disabled commands are listed greyed
  with their reason (tooltip / status) and cannot run. Enter runs, Esc closes, ↑↓/PgUp/PgDn move; the command runs
  after the popup closed (focus back to the view that was active).
* **Key settings** (`gui/keymap.py` + Settings ▸ "Klaviatura"): every window-level action has a stable id (the
  `_act` key) and default keys; the user's overrides live in `gui_state["shortcuts"] = {action_id: ["Ctrl+…", …]}`
  (GUI state, not core settings: the CLI has no use for them) and are applied at start and live. The page: filter
  box, table (action, menu path, default, current) with a `QKeySequenceEdit`, "Asl holiga" per row and for all;
  conflicts (a sequence used by another action or by a fixed key) are refused with the other action's name. The
  grids' own keys (↑↓→←, `*`, Space, Enter, Shift+Enter, F2, Ins, Del, Ctrl+D, Alt+↑/↓, Ctrl+V, Ctrl+C, Alt+↓ in the
  table) are fixed and listed read-only (they are handled in the views' key handlers, §12.9). The palette and the
  menus show the current keys.
* **Session restore** (`gui/sessionstore.py`, Qt-free model + a small Qt part in the main window): on close (and
  every 60 s while the window lives, for crash safety) the session is written to `gui_state["session"]`: per tab the
  path, forced format / record path, the two panes' view kinds, the current node (location text), the expanded tree
  nodes (location texts, at most 2 000 per tab), the filter (expression text or builder spec) and the sort spec, the
  active tab; window geometry and dock state as today. At start WITHOUT files on the command line the last session is
  restored (setting `restore_session`: "always" (default) | "ask" | "never", in Settings ▸ General); with files on
  the command line only those open and the saved session is kept for the next start. Missing files are skipped with
  one status line; changed files reopen normally; filters / sorts re-run in the background; expanded nodes are
  re-expanded once the tree has loaded (best effort, bounded); unsaved edit layers are handled by the existing
  recovery bars (nothing is restored silently). A search result view is not restored (the search is).
* **Workspace file** (`gui/workspace.py`, File ▸ "Workspace'ni ochish…" / "Workspace'ni saqlash…", recent workspaces):
  `*.jrworkspace` (UTF-8 JSON, atomic write) `{"version": 1, "app": …, "saved": …, "tabs": [ {"path": absolute,
  "relpath": relative to the workspace file, "format", "record_path", "views", "current", "expanded", "filter",
  "sort", "table_layout", "bookmarks", "mapping": <embedded mapping file JSON or null>, "edits": "none" | "pending"}
  ], "active": n, "geometry": …}` — the session model plus bookmarks, table layouts and the structure builder's tree.
  Opening resolves `relpath` first (a moved workspace folder still works), then `path`; edit layers are NOT copied:
  a tab saved with unsaved changes reopens its file and the recovery bar offers them (`"edits": "pending"` only
  informs). Loading validates the structure (`workspace.error.*` with the JSON pointer of the bad part).
* **Themes**: system / light / dark stay; stage-6 work is a dark-mode audit of every window and dialog added in
  stages 3–5 (theme roles only, no hard-coded colours outside `theme.py` — a static test enforces it) and a contrast
  test over the theme roles used for text on tinted backgrounds. Font zoom (Ctrl+= / Ctrl+- / Ctrl+0) and the
  monospace font choice exist (stage 2).
* **"Open with" on Windows** (`gui/osintegration.py`, Settings ▸ General: "Windows: .json fayllarni «Open with»
  ro'yxatiga qo'shish" / "olib tashlash"): writes ONLY per-user keys under `HKCU\Software\Classes` —
  `Applications\jsonreshape-gui.exe` (`FriendlyAppName`, `shell\open\command` = `"<exe>" "%1"`, `SupportedTypes`:
  `.json .jsonl .ndjson .geojson .gz`) and a ProgID `jsonreshape.document` listed in `OpenWithProgids` of those
  extensions; never changes the default program (Windows does not allow it silently; the user picks it in "Open
  with"). Enabled when running from the packaged `jsonreshape-gui.exe` (or, for development, with
  `pythonw.exe -m jsonreshape.gui`); "remove" deletes exactly the keys it wrote. `SHChangeNotify(SHCNE_ASSOCCHANGED)`
  after a change. Tests use a fake registry backend (no real registry writes).
* **`.exe`** (`packaging/jsonreshape.spec`, `scripts/build_exe.ps1`): PyInstaller ONEDIR build (fast start; a onefile
  exe unpacks ~150 MB to a temp folder at every start) → `dist\jsonreshape\` with `jsonreshape.exe` (console: the
  CLI) and `jsonreshape-gui.exe` (windowed) sharing one `_internal` folder; includes the Rust core `.pyd`, the i18n
  table, PySide6 `platforms`, `styles`, `imageformats` plugins; excludes QtWebEngine, QtQml/Quick, Qt3D, multimedia,
  tkinter and tests; a version resource from `jsonreshape.__version__`. The build script installs a pinned PyInstaller
  into the venv when missing (developer step; the app itself stays offline), builds, and runs the smoke tests
  (`tests/test_packaging.py`, marker `packaging`, skipped unless `dist\jsonreshape` exists): `--version`, `inspect`
  of a small file, `convert`, the GUI exe started offscreen with a file and closed, and start time.
* **Test 26 + benchmark**: `tests/test_gui_e2e.py` covers the whole scenario through the real window (open, expand /
  collapse, search hit → node, table sort, edit + undo + redo + save, export button, Tools menu pass); the full
  benchmark re-runs `bench/run_bench.py`, `bench/gui_bench.py`, `bench/edit_bench.py` and the mapping / stage-5 tool
  benchmarks on the 2 GB files and fills README §14 against every §12 target.

### 15.1 Ownership plan (agents)

* S6-A: `gui/palette.py`, `gui/keymap.py`, the Settings "Klaviatura" page (`gui/settingsdialog.py`), hooks in
  `gui/mainwindow.py` (Ctrl+Shift+P action, keymap applied after `_create_actions`), tests.
* S6-B: `gui/sessionstore.py`, `gui/workspace.py`, hooks in `gui/mainwindow.py` (File menu items, save on close +
  timer, restore at start) and `gui/app.py` (start-up decision), `restore_session` in the Settings General page,
  tests.
* S6-C: `gui/osintegration.py` (+ its Settings General rows), `packaging/`, `scripts/build_exe.ps1`,
  `tests/test_packaging.py`, the static colour test + dark-mode audit fixes in the stage-3–5 GUI modules.
* S6-D (last, alone on the machine): test 26 completion and the full benchmark + README §14.
  Shared files (`mainwindow.py`, `settingsdialog.py`, `i18n.py`) take small anchored edits only.

### 15.2 As built (stage 6; module docstrings are authoritative)

* **`gui/palette.py`** (`show_palette(window)` → one `CommandPalette` per window; Ctrl+Shift+P; View menu, first item):
  entries = every menu-bar QAction (the menus' `aboutToShow` are fired first so dynamic menus are current — they must
  stay cheap), the current node's context-menu commands ("Tugun ▸ …", without the Edit-menu duplicates), "Tab: …",
  "Ochish: …"; ranking exact prefix > word start > word initials > inside a word > subsequence, then the menu path;
  ties: enabled, earliest match, recently used, shorter, menu order; `gui_state["palette_recent"]` (20); disabled
  entries greyed with their reason; the document area is found through `window.central`. Measured with 330 entries:
  first open 29 ms (one-time imports), then ~6 ms; filtering ~0.7 ms per keystroke.
* **`gui/keymap.py`** (`KeyMap` attached as `window.keymap`; Settings ▸ "Klaviatura" `KeymapPage`): stable ids = the
  `_act` keys; overrides in `gui_state["shortcuts"]` (cleaned on load: invalid / fixed / taken keys dropped and
  logged); one key combination per sequence (no chords); the grids' keys (`FIXED_KEYS`: arrows, `*`, `+`/`-`, Space,
  Enter, Shift+Enter, F2, Ins, Shift+Ins, Del, Ctrl+D, Alt+↑/↓, Ctrl+V, Ctrl+C, Ctrl+Shift+C, Ctrl+A, Ctrl+Space, Esc,
  Tab, PgUp/PgDn/Home/End with modifiers, Alt+menu letters) are refused and listed read-only; the page edits a working
  copy applied on "Saqlash".
* **`gui/sessionstore.py`** (Qt-free) + **`gui/workspace.py`** (`window.workspaces` = `WorkspaceController`):
  `gui_state["session"]` written at close and every `AUTOSAVE_MS` (60 s) after `at_start`; `at_start(tabs)` →
  "command_line" | "restore" | "ask" (a non-modal bar) | "never" | "nothing"; `TreeView.expanded_locations()` /
  `restore_expanded()` (loaded rows only; one silent task); search result views are not restored (their inputs are);
  `.jrworkspace` v1 written one member per line (a deep embedded mapping stays small); `MappingState.install_tree`
  installs an embedded mapping; recent workspaces ≤ 10.
* **`gui/osintegration.py`** ("Open with", Settings ▸ Umumiy): `WinregBackend` confined to `Software\Classes` of HKCU,
  `MemoryRegistry` fake (also the default under pytest); the ProgID `jsonreshape.document` gets its own command and
  type name; a `JsonReshapeCreatedKeys` (REG_MULTI_SZ) record makes the removal exact (only own values, then own empty
  keys, then the recorded shared keys if empty); adding and removing ask first; `SHChangeNotify(SHCNE_ASSOCCHANGED)`.
* **Packaging** (`packaging/jsonreshape.spec`, `scripts/build_exe.ps1`, `tests/test_packaging.py` marker `packaging`):
  PyInstaller 6.22.3 onedir `dist\jsonreshape\` (78 MB, 160 files: Qt 44 MB) with `jsonreshape-gui.exe` (windowed) and
  `jsonreshape.exe` (console) sharing `_internal\`; both entry scripts call `multiprocessing.freeze_support()` first
  (automatic worker pools work frozen); excluded: QtWebEngine / Qml / Quick / 3D / multimedia, TUIO and virtual-keyboard
  plugins, the PDF plugin, `opengl32sw.dll`, Qt translations, tkinter, `ssl`, tests. Start (final build): CLI `--version`
  ~0.10 s, GUI window ~0.28 s, a file opened and indexed ~0.31 s.
* **Themes**: `theme.role_style(template)` / `bind_style(widget, template)` (style sheets re-applied on theme
  change), role `danger_soft`; a static test forbids hard-coded colours outside `theme.py` (allow-listed: the app icon,
  transparency checkerboards, `Qt.GlobalColor.transparent`); a contrast test and a dark-mode smoke over every stage 3–5
  window.
* **`fileio.read_limited(f, limit)`** (found by the final benchmark): every bounded read of a small file — mapping
  (128 MiB limit), JSON Schema and workspace (64 MiB), GUI state and the edit-layer index (16 MiB), search history and
  filter presets (8 MiB), settings (1 MiB) — asks for the file's size + 1 instead of `read(limit + 1)`, which
  allocated (and on Windows put in the working set) the whole limit at once: `jsonreshape apply` peaked at 186 MB in
  the parent alone, 94 MB after. A file that grew after the size check is read on in 1 MiB pieces up to the limit.

---------------------------------------------------------------------------------

## 16. Distribution (stage 7): CI, releases, installer, winget

### 16.1 As built (module docstrings and workflow headers are authoritative)

* **CI** (`.github/workflows/ci.yml`; push / pull request to `main`; superseded runs cancelled): `tests-windows`
  (windows-latest, Python 3.12, Rust stable, venv, `maturin develop --release`, `pip install -e ".[dev,gui]"`,
  asserts `core.BACKEND == "rust"`, `pytest -m "not gui and not bench and not packaging" --timeout=600`),
  `tests-linux-python-core` (ubuntu-latest, `JSONRESHAPE_BACKEND=python`, the same selection; Rust is installed
  there only because the build backend (maturin) compiles the extension while installing — the tests assert
  `core.BACKEND == "python"`), `build-exe` (after `tests-windows`: `scripts/build_exe.ps1 -SkipTests -NoInstall`,
  `pytest -m packaging`, `dist/jsonreshape` as a 14-day artifact). Both test jobs need the `gui` extra: pytest-qt
  refuses to start without a Qt binding and 57 GUI test modules import PySide6 at collection (the marker excludes
  the GUI tests themselves). pip and cargo caches (`actions/cache@v4`) keyed on `pyproject.toml` / `rust/Cargo.lock`;
  every action pinned to a major version (`checkout@v4`, `setup-python@v5`, `cache@v4`, `upload-artifact@v4`,
  `dtolnay/rust-toolchain@stable`); `JSONRESHAPE_HOME` under the runner's temp, `QT_QPA_PLATFORM=offscreen`.
* **Release** (`.github/workflows/release.yml`; tag `v*` or manual): `version` (the tag, `jsonreshape/__init__.py`,
  `pyproject.toml` and `rust/Cargo.toml` must agree — `scripts/release_assets.py check-version`), `windows-exe`
  (`build_exe.ps1 -SkipTests -NoInstall -Installer [-Sign]`, smoke tests, `jsonreshape-<v>-windows-x64.zip` +
  `jsonreshape-<v>-setup-x64.exe`; signing only when the secrets `CODE_SIGN_PFX_BASE64` / `CODE_SIGN_PFX_PASSWORD`
  exist), `unix-bundle` (ubuntu-22.04 → `-linux-x86_64.tar.gz`, macos-14 → `-macos-arm64.tar.gz`; PyInstaller with
  the same spec, smoke: `--version`, `inspect --json`, the GUI with the smoke hook offscreen), `wheels`
  (`PyO3/maturin-action@v1`: one abi3 wheel per platform — win_amd64, manylinux x86_64, macOS universal2 — for
  Python ≥ 3.11) and `sdist`, then `release`: `SHA256SUMS.txt` (sha256sum format), release notes from the
  CHANGELOG.md section of the version, the winget manifests (artifact `winget-manifests`), and
  `softprops/action-gh-release@v2` with `draft: true` — the author reviews and publishes by hand. `publish-pypi` runs
  only with the repository variable `PYPI_PUBLISH=true`, for tags, through the `pypi` environment with trusted
  publishing (`id-token: write`, no token).
* **Spec off Windows** (`packaging/jsonreshape.spec`): the version resource and the icon only on `win32`
  (`PyInstaller.utils.win32.versioninfo` needs `pefile`, a Windows-only dependency); the Qt drop list matches
  `Qt6X.dll`, `libQt6X.so.N` and `QtX.framework/`, keeping `QtDBus` (the Linux `QtGui` links it) and `QtOpenGL`
  there; `gui/osintegration.py` imports `winreg` lazily and reports "not available" off Windows.
* **Installer** (`packaging/installer.iss`, Inno Setup 6, `scripts/build_exe.ps1 -Installer`): `dist\jsonreshape\*`
  → `{autopf}\jsonreshape` (`PrivilegesRequired=lowest` + `PrivilegesRequiredOverridesAllowed=dialog`: per user
  without elevation or for all users; `/CURRENTUSER` / `/ALLUSERS` silently), Start menu shortcut, optional desktop
  shortcut, optional user PATH entry (HKCU `Environment\Path`, removed by the uninstaller's `[Code]`), uninstaller;
  `ArchitecturesInstallIn64BitMode=x64compatible`; fixed `AppId {FA95975A-1001-43C5-8BE8-53ADDEF4E518}`; the
  version, its four-number form and the copyright come from `packaging/version_info.py` as `/D` defines (the script
  reads them through JSON with `ensure_ascii`, so the `©` survives any console code page; the `.iss` is ASCII and
  `#error`s without `/DAppVersion`). `ISCC.exe` is looked up on the PATH, then in `%ProgramFiles(x86)%`,
  `%ProgramFiles%` and `%LOCALAPPDATA%\Programs` (`Inno Setup 6\`); missing → a message with the install command
  and the step is skipped. Measured: 27.9 MB, 14 s (lzma2/max) from the 78 MB folder.
* **Signing** (`-Sign <thumbprint>`): `signtool sign /sha1 <thumbprint> /fd SHA256 /tr http://timestamp.digicert.com
  /td SHA256` on both executables (before the smoke tests) and on the installer; signtool from the PATH or the newest
  `Windows Kits\10\bin\<version>\x64`; missing → a message and the files stay unsigned; a thumbprint that matches no
  certificate fails the build (signtool exit 1).
* **winget** (`packaging/winget/Erkinbek.jsonreshape/<version>/*.yaml`, schema 1.6.0, `scripts/winget_manifest.py`):
  templates with `{{VERSION}}`, `{{RELEASE_DATE}}`, `{{INSTALLER_URL}}`, `{{INSTALLER_SHA256}}`; the installer
  manifest lists the same `.exe` twice (`Scope: user` with `/CURRENTUSER`, `Scope: machine` with `/ALLUSERS`),
  `ProductCode {AppId}_is1` (the Inno Setup uninstall key, so winget finds upgrades), `Commands: [jsonreshape]`,
  `FileExtensions`; the script hashes the installer (1 MiB pieces), fills the newest template folder, drops the
  template header and writes `<out>/manifests/e/Erkinbek/jsonreshape/<version>/` (the winget-pkgs layout).
* **`scripts/release_assets.py`** (standard library only; never imports the package): `version`, `check-version`,
  `sums` (streaming SHA-256, temporary file + rename), `notes` (CHANGELOG section → installation lines per asset
  kind, asset table with sizes and digests, checksum instructions; the "[Unreleased]" section with a TODO when the
  version has no section yet).
* **Packaging metadata**: `maturin build` → `jsonreshape-<v>-cp311-abi3-<platform>.whl` with Name / Version /
  `License: Apache-2.0` / classifiers / `License-File: LICENSE, NOTICE`; the sdist now includes LICENSE, NOTICE and
  CHANGELOG.md (`[tool.maturin] include`). `tests/test_packaging_pipeline.py` (17 tests) checks all of the above
  statically — workflows parsed with PyYAML, actions pinned, jobs / markers / timeouts, the `.iss` sections and
  defines, the build script's switches and its PowerShell parse, the winget fill, the release helpers on temporary
  folders, the spec guards, and the built wheel / sdist metadata when `build/wheels` exists.

### 16.2 Not verified here (first CI run)

The Linux / macOS jobs could not be run on this machine: Windows-only assumptions in tests (paths, locking) may
surface in `tests-linux-python-core`; the Linux PyInstaller bundle depends on the runner's Qt system libraries
(`libegl1 libgl1 libopengl0 libxkbcommon0 libdbus-1-3 libfontconfig1 libglib2.0-0 libxcb-cursor0` are installed);
the macOS bundle is unsigned and not notarized (Gatekeeper asks once). The Windows test job may need more than the
45-minute limit on a slow runner (the fast selection takes ~20 min locally) — raise `timeout-minutes` or add
`pytest-xdist` then.

### 16.3 Screenshots (`scripts/make_screenshots.py`)

`scripts/make_screenshots.py` renders the README pictures without a display: it starts the real window with
`gui.app.main([file], run=False)` on the offscreen platform (English UI, a private `JSONRESHAPE_HOME`), drives it
through the same public methods the GUI tests use (`DocumentTab.show_mapping`, `MapView.run_action`,
`MainWindow.show_compare_files` / `show_stats` / `show_import` / `show_palette`, `TreeView.expand_node`), and saves
`QWidget.grab()` of each whole window as PNG (zlib level 9; an 8-bit median-cut fallback keeps every file under
350 KB). On Windows the offscreen platform's FreeType font database needs `QT_QPA_FONTDIR` (set to `%WINDIR%\Fonts`)
and the UI font is set to Segoe UI 9 pt before the window exists. The data (12 000 synthetic records, a changed copy
for the comparison, a CSV) is written once into `%TEMP%\jsonreshape-screenshots`.

### 16.4 Demo GIF (`scripts/make_demo_gif.py`)

`scripts/make_demo_gif.py` records `docs/img/demo.gif` the same way (it imports `make_screenshots` for the offscreen
environment and its wait helpers) on `bench/data/syn_2g.json` (2.1 GB, 6.5 M records; generated when missing). Five
scenes — open, browse, search ("Toshkent", byte mode), filter (`score > 990 and contact.address.city == "Samarqand"`),
CSV export of the filtered view — each frame `QWidget.grab()` of the main window (an open dialog drawn centred over
it) plus a caption strip with a clock. The clock and the frame durations are real time: during a running task a
frame is grabbed every 90 ms and stays on screen until the next one; only the filter (~25 s, it parses every record)
is played 8× faster, which the strip says under the clock. A fresh `JSONRESHAPE_HOME` per run means no cached index,
so the opening shows the real first-time indexing. The PNG frames go through `ffmpeg` (concat demuxer with
per-frame durations; one `palettegen` palette, `paletteuse=dither=none:diff_mode=rectangle`): ~125 frames, ~33 s,
~0.8 MB. `--mp4` writes an H.264 copy for posts. The export lands next to the data file under a relative name (no
user folder on screen) and is deleted afterwards.

---------------------------------------------------------------------------------

## 17. Importers (stage 7): CSV / TSV, YAML and XML -> JSON / JSON Lines

Module `jsonreshape/importers.py` (its docstring is the detailed specification); CLI command `import`
(`cli._cmd_import`, `cli._add_import_command`); GUI `jsonreshape/gui/importdialog.py` (`ImportDialog`,
`show_import_dialog`; File ▸ "Import (CSV, YAML, XML)…" → `MainWindow.show_import`, action id `import_file`).
Tests: `tests/test_importers.py`, `tests/test_cli_import.py`, `tests/test_gui_importdialog.py`.

### 17.0 Interface

```python
FORMATS = ("csv", "yaml", "xml"); OUTPUT_FORMATS = ("json", "jsonl")
TYPE_MODES = ("auto", "string"); EMPTY_MODES = ("null", "string")

@dataclass
class ImportOptions:
    fmt: str | None = None          # None: detect_format(source) by extension (.csv .tsv .yaml .yml .xml)
    output: str | None = None       # None: output_format_for(target) (.json / .jsonl / .ndjson), else jsonl
    indent: int | str | None = 2    # JSON array layout: 0..16, "\t", None = compact (JSON Lines is always compact)
    types: str = "auto"; empty: str = "null"
    delimiter: str | None = None; header: bool = True; nest: bool = True; encoding: str | None = None
    skip_bad: bool = False
    record_tag: str | None = None; record_path: str | None = None       # XML; not both

def import_file(source, target, opts=None, *, settings=None, progress=None, cancel=None) -> ImportReport
def detect_format(path) -> str | None; def output_format_for(path) -> str | None
def number_value(token: str) -> int | float | JsonFloat | JsonInt      # the exact value of a JSON number token
```

`ImportReport`: `fmt`, `output_fmt`, `source`, `output`, `records`, `bytes_read`, `bytes_written`, `elapsed`,
`encoding`, `delimiter`, `columns` (CSV header), `numbers`, `literals`, `empty_cells`, `skipped`, `skipped_lines`
(first 5), `dates`, `binaries`, `yaml_values`, `nonfinite`, `root_tag` (XML), `warnings` / `warning_keys` (skipped
rows, big YAML, non-finite YAML values, no records), `notes` / `note_keys` (conversion counts); `to_dict()` for
`--json`. Errors: `UserInputError` (options, a missing source, a refused target — `writer.error.same_file`,
`fileio.error.is_directory`, `writer.error.no_dir`, `filetools.error.part_open_edited` through
`writer.refuse_edited_targets` — `importers.error.unknown_format`, `importers.error.gzip`,
`importers.error.yaml_missing`, `importers.error.yaml_too_big`, `importers.error.yaml_recursive`,
`importers.error.yaml_expanded`, `importers.error.xml_entities`); `FileFormatError` for a malformed input
(`importers.error.csv_row_width`, `csv_malformed`, `csv_duplicate_header`, `csv_nest_conflict`, `decode`, `utf32`,
`yaml`, `xml`: the CLI exits 2). `Cancelled` leaves nothing behind (`fileio.atomic_write`).

### 17.1 Streaming and memory

* CSV: `open_shared` → a counting `RawIOBase` (`_Counter`: bytes handed out = progress; raises `Cancelled` at a
  cancellation) → `io.BufferedReader` (1 MiB) → `io.TextIOWrapper(encoding, newline="")` → `csv.reader(strict=True)`;
  one row is parsed, typed (`_Cells`: the value rule with counters) and written at a time (`codec.dumps_transient`
  + `\n`, or fed to one `core.Reformatter(indent)` between `[` `,` `]` for the JSON array — the exact bytes of
  `codec.dumps(records, indent)`). `csv.field_size_limit` is raised to `FIELD_LIMIT` (64 MiB) once: an unbalanced
  quote cannot swallow the file into memory. Measured: a 100 MB CSV (1 M records) converts in ~7 s with a 35 MB
  peak working set (`test_100mb_csv_converts_with_bounded_memory`, a subprocess measured with psutil).
* Encoding: `fileio.sniff` on the first 64 KiB (UTF-8 BOM → `utf-8-sig`, UTF-16 BOM → `utf-16`, the BOM-less
  UTF-16 pattern → `utf-16-le` / `-be`, UTF-32 refused, gzip refused) or the given codec (`utf-8` → `utf-8-sig`).
  A decode error is reported with its line found by a second pass (`_decode_error_line`: the text wrapper decodes
  whole buffers, so the row reader's line is not the place).
* Delimiter: `_sniff_delimiter` over the sample's complete lines (≤ 20): the first of `, ; \t |` (tab first for
  `.tsv`) that gives every row the same field count > 1, else the one with the most fields.
* YAML: the loader (`yaml.CSafeLoader` when libyaml is present, else `SafeLoader`, subclassed once per process with
  constructors for `!!int` / `!!float` — exact `number_value` for JSON tokens, by value otherwise — `!!timestamp`
  → `isoformat()`, `!!binary` → the base64 text) reads the file through the same counting stream (progress = bytes
  consumed; a cancellation ends the stream — `soft` — and is reported as `Cancelled` after the parser stops), but
  constructs every document whole: `YAML_BIG_BYTES` (256 MiB) → `importers.warn.yaml_big`,
  `Settings.memory_limit_bytes × 2` → refused. `_jsonify` walks each document iteratively: keys → text, tuples →
  lists, sets → objects, a container met twice on the current path → `yaml_recursive`, more than
  `2 × size + 1 000 000` values → `yaml_expanded` (the alias bomb). `check_data()` / `get_data()` document by
  document decide JSON (one) vs JSON Lines (several) when the target's extension does not.
* XML: `xml.etree.ElementTree.XMLPullParser(events=("start", "end", "start-ns"))` fed 8 MiB chunks; every chunk
  before the root element is scanned for `<!ENTITY` (with an 8-byte carry over the chunk boundary) and the file is
  refused when found — expat expands internal entities; external ones are never fetched. A stack of open elements
  with their qualified names selects the records (`_Selector`: depth 2 by default, `record_tag` — not inside another
  record —, or the `record_path` from the root); a record ending is converted (`_element_value`, iterative
  post-order: no recursion limit) and written, then removed from its parent (`parent.remove(elem)`), so the tree
  holds one record at a time plus the non-record shells. Namespaces: `start-ns` events build `uri → prefix` (first
  declaration wins; `xml` prefix built in).
* Output: `fileio.atomic_write(target, "wb")`; the target is checked before any work (`_check_target`: same file in
  any spelling, folder, missing folder, `writer.refuse_edited_targets`). No trailing newline after a JSON array
  (like the writer); JSON Lines end with `\n`.

### 17.2 CLI

`import INPUT [OUTPUT | -o OUTPUT] [--from csv|yaml|xml] [--to json|jsonl | --jsonl] [--compact | --indent N]
[--types auto|string] [--empty null|string] [--delimiter D] [--no-header] [--no-nest] [--encoding ENC] [--skip-bad]
[--record-tag TAG | --record-path a/b/c] [--json]`. `_check_input` / `_one_output(required=True)` / `_check_output`
as every command; the default indent is `Settings.export_indent`; the summary line `cli.summary.import` ("{records}
ta yozuv: {input} ({fmt}) -> {output} ({size}, {seconds})"), then the report's notes as info lines and its warnings
as "Ogohlantirish:" lines; `--json` prints `ImportReport.to_dict()` on stdout. Exit codes: 0, 1 (user errors incl.
a missing pyyaml), 2 (malformed input: `FileFormatError`), 3 (cancelled). Help texts `cli.cmd.import.*`,
`cli.help.import_*`, groups `cli.group.import`, `cli.group.import_csv`, `cli.group.import_xml`.

### 17.3 GUI

`ImportDialog(app, source=None, parent=None, *, target=None, open_result=None, ask_open_path=None,
ask_save_path=None)`: source + "Tanlash…" (filters per type), the detected type label and a format combo (override),
target + "Tanlash…" (default `default_target(source, fmt)`: `<stem>.jsonl`, `<stem>.json` for YAML; follows the
source until edited), options (values, indent — enabled for a `.json` target —, the CSV box: delimiter / encoding
(editable combo) / empty cell / header / nest / skip bad; the XML box: record tag / record path; boxes shown for the
chosen type), "Import" (default button) / "To'xtatish", progress bar + status (bytes, rate, ETA; "Navbatda" while
queued), inline error label (`$danger`), the report box (headline `gui.importdialog.report.summary`, notes `$muted`,
warnings `$warn`, "Natijani ochish"). The run is one heavy task (kind `import`) owned by a small `_ImportJob`
(weak reference to the dialog: a late result is dropped); target checks before the run: `check_not_source`, folder,
`open_target_check` (review S3: refused with changes, asked when shown unchanged → those tabs reload), the overwrite
question; closing while running asks and cancels. "Natijani ochish" → `open_result(path, options)` or
`app.request_open(path, {"format": "jsonl"} | {})`. `show_import_dialog(app, source, parent, **kw)` reuses the open
window (and gives it the new source). `MainWindow.show_import(path=None)` (File menu, after "Record path…"; keymap
id `import_file`, no default shortcut) stores it in `dialogs["import"]`.

### 17.4 Decisions

See README §15 (import items): the value rule, CSV columns and rows, YAML (PyYAML 1.1 semantics, limits, aliases,
keys), XML conventions and record selection, output format and defaults. Not done: `.gz` import, CSV type hints
per column, XML attributes/text naming options (`--attr-prefix`), an "always array" list for XML tags, streaming
YAML (impossible with PyYAML).
