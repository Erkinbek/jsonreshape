# jsonreshape — architecture & interface specification

This document is the **source of truth** for module boundaries, public interfaces and
exact semantics. Code must follow it; when code and spec disagree, fix one of them
explicitly (never silently). The user-facing requirements are in `AGENT_PROMPT.md`
(Uzbek). Section numbers like "§6.2" refer to that file.

Language conventions: code, comments, docstrings and this document are English.
Every user-facing text (CLI output, error messages, GUI) goes through
`jsonreshape.i18n.t()` — Uzbek (Latin) primary, English fallback.

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
  * `JSONRESHAPE_LANG` — `uz` | `en`.
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
  cli.py                  argparse CLI
  gui/                    PySide6 viewer (stage 2; see §11)
tests/                    pytest suite (+ tests/jsongen.py shared generators)
bench/                    synthetic data generator + benchmark runner -> README table
docs/ARCHITECTURE.md      this file
```

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
  `array_join_sep=";"`, `xlsx_max_rows=1048576`), `Settings.load()`, `.save()`,
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
    fmt: str                 # json|jsonl|csv|xlsx|sqlite
    indent: int|str|None=2; sort_keys: bool|str=False   # True == "alpha" (uz collation) | "codepoint"
    keep_wrapper=True
    columns: list[str]|None=None; array_mode="join"|"rows"; explode: str|None=None
    join_sep=";"; csv_delimiter=","; csv_bom=True
    xlsx_max_rows=1048576; xlsx_split="sheets"|"files"
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
* XLSX: numbers typed when they have ≤ 15 significant digits within the double range, else
  exact text + warning; strings over 32,767 characters truncated + warning; split into
  sheets/files over `xlsx_max_rows`; cancellation checked before the zip is assembled.
* SQLite: two passes (map detection, then rows); numbers stored as INTEGER (64-bit) / REAL
  (when the double holds the same decimal value) / TEXT (otherwise, with a warning);
  ≤ 1500 columns per table plus an overflow JSON column (`_extra`, renamed when taken);
  map-like objects pooled as JSON text; ≤ 200 child tables (overflow as JSON text).
* Every export calls `doc.ensure_unchanged()` right before the atomic commit.

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
  Ctrl+Shift+C, Ctrl+ +/-/0, Ctrl+E export, Ctrl+Shift+L filter, F5 reopen, Ctrl+D bookmark),
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
