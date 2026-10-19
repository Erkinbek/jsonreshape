r"""JSON Schema (draft 2020-12): generation from structure statistics and an own validator
(docs/ARCHITECTURE.md §14.3; design: the stage-5 analysis ``map_stats.md`` §8).

Generation -- :func:`generate_schema`
=====================================
``generate_schema(schema, *, doc=None, document=True, strict=False, title=None)`` turns the
statistics of :func:`jsonreshape.schema.infer_schema` (a :class:`~jsonreshape.schema.Schema`)
into a JSON Schema document (a dict; write it with :func:`dump_schema`, which keeps every number
text exact -- the standard ``json`` module would not):

* per field: ``type`` (one name or a list in frequency order; ``bool`` -> ``boolean``;
  ``number`` becomes ``integer`` when the schema's integer counter (``FieldInfo.integers``,
  added in stage 5) says every number of the field is mathematically integral -- without that
  counter it stays ``number``); ``properties`` in first-seen order; ``required`` = the keys
  present in every object of the field (exact for complete scans; conservative under pooled
  ``*`` maps); pooled keys -> ``additionalProperties: <pooled schema>``; array elements ->
  ``items``; ``examples`` = the complete example texts (not cut with ``…``) parsed back exactly;
* the document level (``document=True``, the default): ``array`` / ``brace_list`` / ``stream``
  -> ``{"type": "array", "items": <record>}``; ``jsonl`` and ``single`` -> the record schema
  (each line is a document; the single record is the document); ``keyed`` -> ``{"type":
  "object", "additionalProperties": <record>}`` (+ ``propertyNames`` in strict mode); ``wrapped``
  -> the schema of the wrapper (parsed with the record container replaced by ``[]``/``{}``) with
  the container schema placed at the record path. A ``$comment`` "jsonreshape:scope=document" /
  "jsonreshape:scope=record" tells :func:`validate` (scope ``auto``) how the root applies;
  ``$comment`` is a standard annotation every validator ignores (a vendor keyword would break
  strict validators such as ajv);
* ``strict=True`` adds ``minLength``/``maxLength``, ``minimum``/``maximum`` (exact texts),
  ``minItems``/``maxItems``, ``additionalProperties: false`` (not for pooled maps and not when
  the schema was truncated), ``enum`` for low-cardinality fields (at most :data:`ENUM_MAX`
  distinct values, every value known and repeated) from :func:`jsonreshape.stats.field_stats`
  when that module is available, and ``format`` (date-time, date, time, email, uri, uuid, ipv4,
  ipv6): a candidate comes from the field's examples and is kept only when a validation pass
  over the whole document with format assertion finds no value that breaks it, so a strict
  schema made from a full scan always validates its own document. Strict extras that need the
  data run only when ``doc`` is given (``progress`` / ``cancel`` apply to those passes).

Validation -- :func:`compile_schema`, :func:`validate`, :func:`iter_issues`
==========================================================================
:func:`compile_schema` turns a schema value into a :class:`CompiledSchema`: every subschema
becomes a node holding its parsed keyword values and a closure that answers "valid?" for one
instance without allocating (the hot path). Two interpretive evaluators of the same node data
produce annotations (``unevaluatedProperties`` / ``unevaluatedItems``, only where a schema
uses them) and error details (only for invalid instances).

* Keywords: every applicator, validation, unevaluated and core keyword of 2020-12 except
  ``$dynamicRef``/``$dynamicAnchor``; ``dependencies`` (draft-07) is accepted as the
  compatibility alias of ``dependentRequired``/``dependentSchemas``; unknown keywords are
  annotations (ignored), like ``title``, ``default``, ``examples``, ``content*``.
* Exact numbers: instances and schema values are compared by their exact decimal value --
  ``int`` as is, a plain ``float`` by its shortest round-trip text (the token it came from),
  :class:`~jsonreshape.codec.JsonFloat` by its raw text, ``Decimal`` as is -- never through a
  float when that could change the answer: ``0.07`` is a multiple of ``0.01``,
  ``0.10000000000000000001 > 0.1``, ``1e400`` is an integer above every double,
  ``12345678901234567890.0 == 12345678901234567890``. ``multipleOf`` divides coefficient and
  exponent exactly (no power of ten of the size of an exponent is ever built). ``enum``,
  ``const`` and ``uniqueItems`` use the canonical form of :mod:`jsonreshape.dupes` (``1 == 1.0``,
  ``true != 1``, object member order ignored).
* Regular expressions (``pattern``, ``patternProperties``) follow ECMA-262 (:func:`ecma_regex`):
  ``\d``, ``\w``, ``\b`` are ASCII, ``\s`` is the ECMA white-space set, ``.`` excludes the line
  terminators, ``$`` matches only at the end, ``\cX``, ``\u{...}``, surrogate pairs, named
  groups ``(?<n>...)`` / ``\k<n>`` and the General_Category property escapes ``\p{...}`` /
  ``\P{...}`` are translated; an escape ECMA does not know is a schema error.
* ``$ref`` resolves JSON Pointers, ``$anchor`` names, ``$defs`` and embedded ``$id`` resources of
  the same document (base URIs per RFC 3986). Anything else is refused with a localized
  :class:`~jsonreshape.errors.UserInputError` -- never ignored, never fetched: a remote or
  absolute ``$ref`` that is not in the document (unless the caller passes the document in
  ``resources``: an offline URI -> schema mapping), ``$dynamicRef``/``$dynamicAnchor``, a
  ``$schema`` other than 2020-12, a required custom ``$vocabulary``, and ``$ref`` cycles that
  never consume any part of the instance (an evaluation that would never end).
* ``format`` is an annotation by default; ``formats=True`` asserts date-time, date, time, email,
  uri, uuid, ipv4 and ipv6 (RFC 3339 incl. leap seconds, RFC 5321 addresses, RFC 3986 absolute
  URIs; ASCII digits only). Other formats stay annotations.
* Keyword values are checked when compiling ("minimum must be a number", "required must be an
  array of unique strings", ...) with the schema location in the message
  (``jschema.error.schema.*``). The official meta-schema needs ``$dynamicRef`` and is not used.

:func:`validate` checks the records of a document (all, or a ``source`` RecordSet / range /
list of positions) and returns a :class:`ValidationReport`; :func:`iter_issues` streams every
:class:`Issue` (for exports). An issue carries the record number (0-based position), the member
key (keyed documents, wrapped documents with an object container), the path inside the record,
the failing keyword, its schema location (a JSON Pointer) and the message parameters; its text
is ``jschema.error.<keyword>`` in the UI language.

Scope
-----
* ``"record"``: every record is validated against the root schema.
* ``"document"``: the root describes the whole document and is *decomposed* so that records are
  still read one at a time: for record sequences (``array``, ``brace_list``, ``stream``,
  ``jsonl`` with an array schema) ``prefixItems[i]`` applies to record ``i``, ``items`` to the
  rest, ``unevaluatedItems`` to records no other keyword evaluated, ``contains`` /
  ``minContains`` / ``maxContains`` count the matching records, ``minItems`` / ``maxItems``
  compare with the record count and ``uniqueItems`` uses :func:`jsonreshape.dupes.find_duplicates`
  (a second pass); for keyed documents ``properties`` / ``patternProperties`` /
  ``additionalProperties`` / ``unevaluatedProperties`` pick the record schema by the member
  key, ``propertyNames`` checks the key, ``required`` / ``dependentRequired`` track the keys
  seen and ``minProperties`` / ``maxProperties`` compare with the distinct key count; for
  wrapped documents the wrapper (the record container replaced by ``[]`` / ``{}``) is validated
  once and every subschema that reaches the container is collected and decomposed the same way.
  ``$ref`` and ``allOf`` are followed. ``anyOf`` / ``oneOf`` / ``not`` / ``if`` / ``enum`` /
  ``const`` (and ``dependentSchemas`` of an object container) at the document level cannot be
  decomposed: the document is then validated as one value when it is at most
  :data:`DOCUMENT_PARSE_BYTES` (64 MiB), else refused with a clear message
  (``jschema.error.document_too_large``). Container-level keywords (counts, required keys,
  duplicates) are checked only when every record is in scope (warning otherwise). Document-level
  issues have ``record=None``.
* ``"auto"`` (default): ``jsonl`` and ``single`` are validated per record unless the root is an
  array schema (then a JSON Lines file is treated like an array of its lines); for the other
  kinds the generator's ``$comment`` decides, else the root's shape: an array-only root (type
  or keywords) for record sequences, a root with ``additionalProperties`` /
  ``patternProperties`` / ``propertyNames`` and without ``properties`` / ``required`` for keyed
  documents, a root whose ``properties`` lead along the record path to the container for wrapped
  documents. :attr:`ValidationReport.scope` says what was used.

Records and limits
------------------
* The document is pinned (``doc.pinned()``, §12.3); a ``source`` with a ``gen`` of another state
  raises ``EditError("edits.error.stale_scope")``; the invalid records come back as a RecordSet
  (kind ``"invalid"``, scope order) tagged with the pinned state's ``gen``.
* Records up to :data:`VALIDATE_PARSE_BYTES` (8 MiB) are parsed with ``codec.loads``; bigger
  ones (and huge records) are skipped and counted with a warning (v1 limitation: no streaming
  validation). Records the decoder refuses, and records nested so deeply that the validator's
  recursion would overflow, are skipped and counted the same way.
* ``max_per_record`` (default 10) issues are kept per record (further ones are not
  collected: the record is invalid anyway), ``max_issues`` (default 1000) are stored in the
  report; the counts per keyword and the top paths cover every collected issue.
* Progress is in bytes (index build, duplicate pre-passes, the records read); ``cancel`` is
  checked every :data:`CHECK_RECORDS` records; ``doc.ensure_unchanged()`` runs before the
  results are reported.

Decisions (documented deviations / extensions of §14.3)
-------------------------------------------------------
* :func:`compile_schema` also takes ``formats`` and ``resources``; :func:`validate` /
  :func:`iter_issues` accept a raw schema value and compile it; :meth:`CompiledSchema.with_formats`
  gives the variant with/without format assertion (cached).
* :func:`generate_schema` also takes ``stats`` (precomputed field statistics, for tests and
  callers that already ran them), ``progress`` and ``cancel`` (strict extras read the file).
* The document-level decomposition validates each record of a keyed document with duplicate
  keys (every duplicate is checked, not only the last one as a JSON parser would keep).
* The whole-document fallback (non-decomposable root) always validates every record, whatever
  ``source`` says (a warning tells it).
* :func:`dump_schema` / :func:`load_schema` read and write schema files with exact numbers.
* Strict ``format`` is not taken from the field statistics (they hold the most frequent values
  only): a candidate from the field's examples is kept only after a validation pass over every
  record finds no value that breaks it (the self-validation guarantee); enums come from
  :func:`jsonreshape.stats.field_stats` (at most :data:`ENUM_FIELDS` scalar leaf fields, one
  pass) when that module is present. Document-level ``minItems`` / ``maxItems`` are not
  generated for the record container (a schema should not fix the record count).
* The generator writes the scope as a ``$comment`` (``SCOPE_DOCUMENT`` / ``SCOPE_RECORD``), not
  as a vendor keyword: strict validators (ajv) reject unknown keywords; ``$comment`` is standard.
* Besides ``$dynamicRef`` and remote references, the validator refuses: other dialects and
  custom meta-schemas (``$schema``), required custom vocabularies, a ``$ref`` to the official
  meta-schema (it needs ``$dynamicRef``; never fetched), endless in-place ``$ref`` cycles, a
  ``$id`` with a fragment. ``dependencies`` (draft-07) is honoured as a compatibility alias.
* Wrapped documents in document scope: the wrapper (the record container replaced by ``[]`` /
  ``{}``) must be at most :data:`VALIDATE_PARSE_BYTES`; a ``$ref``/``allOf`` path to the container
  is followed, a conditional applicator (``anyOf``/``oneOf``/``not``/``if``), ``enum``/``const``,
  ``contains``/``uniqueItems`` or ``unevaluated*`` above the container makes it non-decomposable
  (whole-document fallback).
* Known limits: a ``pattern`` with catastrophic backtracking cannot be interrupted while one
  string is matched (Python ``re``); Unicode script properties (``\p{Script=...}``) are not
  supported (schema error); records over 8 MiB are not validated; the auto scope cannot tell a
  record schema from a document schema when the records themselves are arrays and the schema has
  no ``$comment`` marker (use ``scope``).
"""

from __future__ import annotations

import datetime
import ipaddress
import logging
import math
import re
import sys
import threading
import time
import unicodedata
from array import array
from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path
from typing import Any, Callable, Iterable, Iterator, Mapping, Sequence
from urllib.parse import unquote, urljoin

from . import codec
from .codec import JsonFloat, JsonInt
from .dupes import canonical_bytes
from .schema import _BeyondDecimal
from .errors import EditError, TooLargeError, UserInputError
from .i18n import get_language, has, t
from .paths import Wild, format_path
from .recordset import RecordSet, RecordSetBuilder
from .tasks import CancelToken, Progress, check, format_bytes

log = logging.getLogger(__name__)

__all__ = [
    "DIALECT",
    "DOCUMENT_PARSE_BYTES",
    "VALIDATE_PARSE_BYTES",
    "CompiledSchema",
    "Issue",
    "ValidationReport",
    "compile_schema",
    "dump_schema",
    "ecma_regex",
    "generate_schema",
    "iter_issues",
    "load_schema",
    "validate",
]

DIALECT = "https://json-schema.org/draft/2020-12/schema"
VALIDATE_PARSE_BYTES = 8 << 20       # records up to this size are parsed and validated
DOCUMENT_PARSE_BYTES = 64 << 20      # a non-decomposable document schema: whole-document limit
SCHEMA_FILE_BYTES = 64 << 20         # load_schema(): largest schema file read
CHECK_RECORDS = 512                  # cancellation checks (records)
TOP_PATHS = 50                       # ValidationReport.top_paths
PATHS_TRACKED = 10_000               # distinct normalized paths counted (further ones: not ranked)
ISSUE_BATCH = 256                    # validate(on_issues=): issues per batch at most
ISSUE_FLUSH_SECONDS = 0.25           # ... and a waiting batch is handed over this often while records are read
WHOLE_DOCUMENT_ERRORS = 500_000      # errors kept by a whole-document validation (memory; more: counted, warned)
KEY_CACHE = 4096                     # keyed documents: member keys whose subschemas are remembered
ENUM_MAX = 10                        # strict generation: enum for at most this many distinct values
SCOPE_DOCUMENT = "jsonreshape:scope=document"
SCOPE_RECORD = "jsonreshape:scope=record"

_DIALECTS = frozenset({DIALECT, DIALECT + "#"})
_ROOT_BASE = "https://jsonreshape.invalid/schema.json"   # base URI of a root without $id (never fetched)
_VOCAB = "https://json-schema.org/draft/2020-12/vocab/"
_STANDARD_VOCABULARIES = frozenset(_VOCAB + v for v in (
    "core", "applicator", "unevaluated", "validation", "meta-data", "format-annotation", "format-assertion",
    "content"))
_SCHEME_RE = re.compile(r"[A-Za-z][A-Za-z0-9+.\-]*:")
_ANCHOR_RE = re.compile(r"[A-Za-z_][A-Za-z0-9._\-]*\Z")
_MISSING: Any = object()
_NONE_T = type(None)
_P53 = 1 << 53
_FLOATISH = (float, JsonFloat, Decimal)
_NUM_TYPES = frozenset({int, float, JsonFloat, JsonInt, Decimal})
_TYPE_PY: dict[str, tuple[type, ...]] = {
    "object": (dict,),
    "array": (list,),
    "string": (str,),
    "boolean": (bool,),
    "null": (_NONE_T,),
    "number": (int, float, JsonFloat, JsonInt, Decimal),
    "integer": (int, JsonInt),
}
_TYPE_NAMES = ("array", "boolean", "integer", "null", "number", "object", "string")
_FORMATS_ASSERTED = ("date-time", "date", "time", "email", "uri", "uuid", "ipv4", "ipv6")


# ----------------------------------------------------------------------------------
# exact numbers
# ----------------------------------------------------------------------------------
def _is_number(v: Any) -> bool:
    return type(v) in _NUM_TYPES


def _token_exact(raw: str) -> Any:
    """``Decimal(raw)``, or -- for a token beyond Decimal's exponent range such as
    ``1e99999999999999999999``, where ``Decimal`` raised ``InvalidOperation`` (review R5A-3) --
    a :class:`~jsonreshape.schema._BeyondDecimal`: exact comparisons with ``int`` / ``Decimal``,
    integrality, ``coef_exp`` for ``multipleOf``."""
    try:
        return Decimal(raw)
    except ArithmeticError:
        return _BeyondDecimal(raw)


def _exact(v: Any) -> Any:
    """The exact value of a number: ``int``, ``Decimal`` or ``_BeyondDecimal`` (a plain float by
    its shortest round-trip text, a JsonFloat by its raw text)."""
    tp = type(v)
    if tp is int:
        return v
    if tp is float:
        return Decimal(float.__repr__(v))
    if tp is JsonFloat:
        return _token_exact(v.raw)
    if tp is JsonInt:
        return int(v)
    if tp is Decimal:
        return v
    if isinstance(v, bool):
        raise TypeError("a boolean is not a number")
    if isinstance(v, int):
        return int(v)
    if isinstance(v, float):
        return Decimal(float.__repr__(float(v)))
    raise TypeError(f"not a number: {type(v).__name__}")


def _dec_integral(d: Decimal) -> bool:
    if not d.is_finite():
        return False
    _sign, digits, exp = d.as_tuple()
    if exp >= 0:                       # type: ignore[operator]
        return True
    k = -exp                           # type: ignore[operator]
    if k >= len(digits):
        return not any(digits)
    return not any(digits[-k:])


def _integral(v: Any) -> bool:
    """``v`` is a number with a zero fractional part (exactly)."""
    tp = type(v)
    if tp is int or tp is JsonInt:
        return True
    if tp is float:
        return v.is_integer()
    if tp is JsonFloat:
        x = _token_exact(v.raw)
        return x.integral() if type(x) is _BeyondDecimal else _dec_integral(x)
    if tp is Decimal:
        return _dec_integral(v)
    return False


def _count_value(x: Any) -> int:
    """A non-negative integral exact value as an ``int`` for the count keywords (``minLength``
    ...): one beyond ``sys.maxsize`` counts as ``sys.maxsize`` (no length reaches it; building the
    ``int`` of ``1e999999999999999999`` would not end)."""
    if type(x) is int:
        return min(x, sys.maxsize)
    if x.adjusted() > 18:
        return sys.maxsize
    return int(x)


class _Bound:
    """A numeric keyword value: exact (``x``), as a float when that comparison is exact too
    (``f``), and its JSON text."""

    __slots__ = ("x", "f", "text")

    def __init__(self, value: Any) -> None:
        self.x = _exact(value)
        self.text = _num_text(value)
        f = None
        if type(self.x) is int:
            if -_P53 <= self.x <= _P53:
                f = float(self.x)
        elif self.x.is_finite():
            try:
                ff = float(self.x)
            except OverflowError:  # pragma: no cover - Decimal -> float never overflows (gives inf)
                ff = math.inf
            if math.isfinite(ff) and Decimal(float.__repr__(ff)) == self.x:
                f = ff
        self.f = f


def _cmp(v: Any, b: _Bound) -> int:
    """Sign of ``v - b`` by exact decimal value (``v`` a number, not NaN)."""
    tp = type(v)
    y = b.x
    if tp is int or tp is JsonInt:
        x = v
    elif tp is float:
        if b.f is not None:
            x, y = v, b.f
        elif type(y) is int and -_P53 < v < _P53:
            x = v
        else:
            x = Decimal(float.__repr__(v))
    elif tp is JsonFloat:
        x = _token_exact(v.raw)
    else:
        x = v
    return (x > y) - (x < y)


def _nan(v: Any) -> bool:
    return v != v


def _coef_exp(d: Any) -> tuple[int, int]:
    """``|d| = c * 10**e`` (``d`` nonzero, int, finite Decimal or ``_BeyondDecimal``)."""
    if type(d) is int:
        return abs(d), 0
    if type(d) is _BeyondDecimal:
        return d.coef_exp()
    _sign, digits, exp = d.as_tuple()
    # built from the digit tuple: Decimal arithmetic (abs, scaleb) would round to the context
    # precision or overflow; int(Decimal) itself is exact and has no digit limit
    return int(Decimal((0, digits, 0))), int(exp)


def _is_multiple(x: Any, m: Any) -> bool:
    """``x / m`` is an integer (exact; ``m > 0``; no huge power of ten is ever built)."""
    if x == 0:
        return True
    cx, ex = _coef_exp(x)
    cm, em = _coef_exp(m)
    e = ex - em
    if e >= 0:
        r = cm // math.gcd(cm, cx)
        if r == 1:
            return True
        twos = fives = 0
        while r % 2 == 0:
            r //= 2
            twos += 1
        while r % 5 == 0:
            r //= 5
            fives += 1
        return r == 1 and max(twos, fives) <= e
    k = -e
    if k > cx.bit_length():            # 10**k > cx: a nonzero cx cannot be a multiple
        return False
    return cx % (cm * 10 ** k) == 0


def _multiple_of(v: Any, m: _Bound) -> bool:
    if type(v) is int and type(m.x) is int:
        return v % m.x == 0
    if _nan(v):
        return False
    x = _exact(v)
    if isinstance(x, Decimal) and not x.is_finite():
        return False
    return _is_multiple(x, m.x)


def _canon(v: Any) -> bytes | None:
    try:
        return canonical_bytes(v)
    except (TypeError, ValueError):
        return None


# ----------------------------------------------------------------------------------
# ECMA-262 regular expressions
# ----------------------------------------------------------------------------------
_ECMA_WS = "\\t\\n\\x0b\\x0c\\r \\xa0\\u1680\\u2000-\\u200a\\u2028\\u2029\\u202f\\u205f\\u3000\\ufeff"
_ECMA_DOT = "[^\\n\\r\\u2028\\u2029]"
_ANY_CHAR = "[\\s\\S]"
_GC_ALIASES = {
    "L": ("Lu", "Ll", "Lt", "Lm", "Lo"), "Letter": "L", "LC": ("Lu", "Ll", "Lt"), "Cased_Letter": "LC",
    "Lu": ("Lu",), "Uppercase_Letter": "Lu", "Ll": ("Ll",), "Lowercase_Letter": "Ll", "Lt": ("Lt",),
    "Titlecase_Letter": "Lt", "Lm": ("Lm",), "Modifier_Letter": "Lm", "Lo": ("Lo",), "Other_Letter": "Lo",
    "M": ("Mn", "Mc", "Me"), "Mark": "M", "Combining_Mark": "M", "Mn": ("Mn",), "Nonspacing_Mark": "Mn",
    "Mc": ("Mc",), "Spacing_Mark": "Mc", "Me": ("Me",), "Enclosing_Mark": "Me",
    "N": ("Nd", "Nl", "No"), "Number": "N", "Nd": ("Nd",), "Decimal_Number": "Nd", "digit": "Nd",
    "Nl": ("Nl",), "Letter_Number": "Nl", "No": ("No",), "Other_Number": "No",
    "P": ("Pc", "Pd", "Ps", "Pe", "Pi", "Pf", "Po"), "Punctuation": "P", "punct": "P", "Pc": ("Pc",),
    "Connector_Punctuation": "Pc", "Pd": ("Pd",), "Dash_Punctuation": "Pd", "Ps": ("Ps",),
    "Open_Punctuation": "Ps", "Pe": ("Pe",), "Close_Punctuation": "Pe", "Pi": ("Pi",),
    "Initial_Punctuation": "Pi", "Pf": ("Pf",), "Final_Punctuation": "Pf", "Po": ("Po",),
    "Other_Punctuation": "Po",
    "S": ("Sm", "Sc", "Sk", "So"), "Symbol": "S", "Sm": ("Sm",), "Math_Symbol": "Sm", "Sc": ("Sc",),
    "Currency_Symbol": "Sc", "Sk": ("Sk",), "Modifier_Symbol": "Sk", "So": ("So",), "Other_Symbol": "So",
    "Z": ("Zs", "Zl", "Zp"), "Separator": "Z", "Zs": ("Zs",), "Space_Separator": "Zs", "Zl": ("Zl",),
    "Line_Separator": "Zl", "Zp": ("Zp",), "Paragraph_Separator": "Zp",
    "C": ("Cc", "Cf", "Cs", "Co", "Cn"), "Other": "C", "Cc": ("Cc",), "Control": "Cc", "cntrl": "Cc",
    "Cf": ("Cf",), "Format": "Cf", "Cs": ("Cs",), "Surrogate": "Cs", "Co": ("Co",), "Private_Use": "Co",
    "Cn": ("Cn",), "Unassigned": "Cn",
}
_gc_lock = threading.Lock()
_gc_ranges: dict[str, list[tuple[int, int]]] | None = None
_class_cache: dict[str, str] = {}


class _RegexError(ValueError):
    """An ECMA-262 pattern this module cannot translate (message: the reason)."""


def _gc_table() -> dict[str, list[tuple[int, int]]]:
    """Code point ranges per two-letter General_Category (built once, ~0.3 s)."""
    global _gc_ranges
    with _gc_lock:
        if _gc_ranges is None:
            table: dict[str, list[tuple[int, int]]] = {}
            category = unicodedata.category
            start = 0
            cur = category("\x00")
            for cp in range(1, 0x110000):
                c = category(chr(cp))
                if c != cur:
                    table.setdefault(cur, []).append((start, cp - 1))
                    start, cur = cp, c
            table.setdefault(cur, []).append((start, 0x10FFFF))
            _gc_ranges = table
        return _gc_ranges


def _cp(c: int) -> str:
    return "\\U%08x" % c


def _ranges_text(ranges: Iterable[tuple[int, int]]) -> str:
    return "".join(_cp(a) if a == b else f"{_cp(a)}-{_cp(b)}" for a, b in ranges)


def _property_class(name: str) -> str:
    """The body of a character class (no brackets) for ``\\p{name}``."""
    hit = _class_cache.get(name)
    if hit is not None:
        return hit
    value = name
    for prefix in ("General_Category=", "gc="):
        if value.startswith(prefix):
            value = value[len(prefix):]
    if value == "Any":
        body = "\\x00-\\U0010ffff"
    elif value == "ASCII":
        body = "\\x00-\\x7f"
    else:
        cats = _GC_ALIASES.get(value)
        while isinstance(cats, str):
            cats = _GC_ALIASES.get(cats)
        if value == "Assigned":
            cats = tuple(c for c in _gc_table() if c != "Cn")
        if not cats:
            raise _RegexError(f"unsupported Unicode property \\p{{{name}}}")
        table = _gc_table()
        merged: list[tuple[int, int]] = []
        for a, b in sorted(r for c in cats for r in table.get(c, ())):
            if merged and a <= merged[-1][1] + 1:
                merged[-1] = (merged[-1][0], max(b, merged[-1][1]))
            else:
                merged.append((a, b))
        body = _ranges_text(merged) if merged else ""
    _class_cache[name] = body
    return body


def _hex(src: str, i: int, n: int) -> int:
    text = src[i:i + n]
    if len(text) != n or any(c not in "0123456789abcdefABCDEF" for c in text):
        raise _RegexError(f"invalid escape at position {i - 1}")
    return int(text, 16)


def _escape(src: str, i: int, in_class: bool) -> tuple[str, str | None, int]:
    """Translate the escape at ``src[i] == '\\'``: ``(positive text, negated class body or None,
    next index)``; the negated body is set for ``\\S`` / ``\\P{..}`` inside a class."""
    n = len(src)
    if i + 1 >= n:
        raise _RegexError("pattern ends with a backslash")
    d = src[i + 1]
    j = i + 2
    if d in "dDwW":
        return "\\" + d, None, j
    if d in "bB":
        if in_class:
            return ("\\x08", None, j) if d == "b" else _raise(f"\\B in a class at position {i}")
        return "\\" + d, None, j
    if d == "s":
        return (_ECMA_WS if in_class else f"[{_ECMA_WS}]"), None, j
    if d == "S":
        return ("", _ECMA_WS, j) if in_class else (f"[^{_ECMA_WS}]", None, j)
    if d in "tnrfv":
        return "\\" + d, None, j
    if d == "c":
        if j < n and src[j].isascii() and src[j].isalpha():
            return "\\x%02x" % (ord(src[j]) % 32), None, j + 1
        raise _RegexError(f"invalid control escape at position {i}")
    if d == "x":
        return "\\x%02x" % _hex(src, j, 2), None, j + 2
    if d == "u":
        if j < n and src[j] == "{":
            end = src.find("}", j)
            if end < 0 or end == j + 1:
                raise _RegexError(f"invalid \\u{{...}} escape at position {i}")
            cp = _hex(src, j + 1, end - j - 1)
            if cp > 0x10FFFF:
                raise _RegexError(f"code point out of range at position {i}")
            return _cp(cp), None, end + 1
        cp = _hex(src, j, 4)
        j += 4
        if 0xD800 <= cp <= 0xDBFF and src.startswith("\\u", j) and j + 6 <= n:
            try:
                lo = _hex(src, j + 2, 4)
            except _RegexError:
                lo = -1
            if 0xDC00 <= lo <= 0xDFFF:
                return _cp(0x10000 + ((cp - 0xD800) << 10) + (lo - 0xDC00)), None, j + 6
        return _cp(cp), None, j
    if d == "0" and not (j < n and src[j].isdigit()):
        return "\\x00", None, j
    if d.isdigit():
        if in_class:
            raise _RegexError(f"back-reference in a class at position {i}")
        k = j
        while k < n and src[k].isdigit():
            k += 1
        return "\\" + src[i + 1:k], None, k
    if d == "k":
        if j < n and src[j] == "<":
            end = src.find(">", j)
            if end > j + 1:
                return f"(?P={src[j + 1:end]})", None, end + 1
        raise _RegexError(f"invalid \\k escape at position {i}")
    if d in "pP":
        if j < n and src[j] == "{":
            end = src.find("}", j)
            if end > j + 1:
                body = _property_class(src[j + 1:end])
                if d == "p":
                    return (body if in_class else f"[{body}]"), None, end + 1
                return ("", body, end + 1) if in_class else (f"[^{body}]", None, end + 1)
        raise _RegexError(f"invalid property escape at position {i}")
    if d.isascii() and d.isalnum():
        raise _RegexError(f"unknown escape \\{d} at position {i}")
    return re.escape(d), None, j


def _raise(msg: str) -> Any:
    raise _RegexError(msg)


def _char_class(src: str, i: int) -> tuple[str, int]:
    """Translate the class starting at ``src[i] == '['``."""
    n = len(src)
    j = i + 1
    neg = j < n and src[j] == "^"
    if neg:
        j += 1
    if j < n and src[j] == "]":       # [] matches nothing, [^] any character
        return (_ANY_CHAR if neg else "(?!)"), j + 1
    pos: list[str] = []
    negs: list[str] = []
    while True:
        if j >= n:
            raise _RegexError(f"unterminated character class at position {i}")
        c = src[j]
        if c == "]":
            j += 1
            break
        if c == "\\":
            text, negbody, j = _escape(src, j, True)
            if negbody is not None:
                negs.append(negbody)
            pos.append(text)
            continue
        if c in "[&~|":
            pos.append("\\" + c)
        else:
            pos.append(c)
        j += 1
    body = "".join(pos)
    if not negs:
        if not body:
            return (_ANY_CHAR if neg else "(?!)"), j
        return f"[{'^' if neg else ''}{body}]", j
    if not neg:
        alts = ([f"[{body}]"] if body else []) + [f"[^{nb}]" for nb in negs]
        return "(?:" + "|".join(alts) + ")", j
    looks = "".join(f"(?=[{nb}])" for nb in negs)
    if body:
        looks += f"(?![{body}])"
    return f"(?:{looks}{_ANY_CHAR})", j


_BRACE_QUANT = re.compile(r"\{[0-9]+(?:,[0-9]*)?\}")
_BRACE_NO_MIN = re.compile(r"\{,[0-9]*\}")
_ECMA_GROUPS = ("(?:", "(?=", "(?!", "(?<=", "(?<!")


def ecma_regex(pattern: str) -> str:
    """Translate an ECMA-262 pattern (JSON Schema ``pattern``) into Python ``re`` syntax with
    the same meaning when compiled with ``re.ASCII`` (module docstring). ``ValueError`` for a
    pattern that cannot be translated -- also for the syntax Python accepts with a meaning of its
    own where ECMA-262 has a SyntaxError (review R5A-9): a group ``(?`` other than ``(?:``,
    ``(?=``, ``(?!``, ``(?<=``, ``(?<!``, ``(?<name>`` (``(?i)``, ``(?P<n>``, ``(?#..)``,
    ``(?>..)``...) and a possessive quantifier (``a*+``). ``{,n}`` is no quantifier in ECMA-262:
    its characters are matched literally (Annex B; Python read it as ``{0,n}``), like the other
    braces that do not form a quantifier."""
    out: list[str] = []
    i = 0
    n = len(pattern)
    quant = 0          # the last token: 1 a quantifier, 2 a quantifier made lazy by "?"
    while i < n:
        c = pattern[i]
        prev, quant = quant, 0
        if c == "\\":
            text, _neg, i = _escape(pattern, i, False)
            out.append(text)
        elif c == "[":
            text, i = _char_class(pattern, i)
            out.append(text)
        elif c == ".":
            out.append(_ECMA_DOT)
            i += 1
        elif c == "$":
            out.append("\\Z")
            i += 1
        elif c == "(" and pattern.startswith("(?", i):
            if pattern.startswith(_ECMA_GROUPS, i):
                out.append("(?")
                i += 2
            elif pattern.startswith("(?<", i):
                out.append("(?P<")
                i += 3
            else:
                raise _RegexError(f"invalid group at position {i}: (?{pattern[i + 2:i + 3]} is not ECMA-262 syntax")
        elif c == "?" and prev == 1:
            out.append("?")                    # lazy
            i += 1
            quant = 2
        elif c == "+" and prev:
            raise _RegexError(f"nothing to repeat at position {i} (a possessive quantifier is not ECMA-262 syntax)")
        elif c in "*+?":
            out.append(c)
            i += 1
            quant = 1
        elif c == "{":
            m = _BRACE_QUANT.match(pattern, i)
            if m is not None:
                out.append(m.group())
                i = m.end()
                quant = 1
            else:
                m = _BRACE_NO_MIN.match(pattern, i)
                end = m.end() if m is not None else i + 1
                out.append(re.escape(pattern[i:end]))
                i = end
        elif c == "}":
            out.append("\\}")
            i += 1
        else:
            out.append(c)
            i += 1
    return "".join(out)


# ----------------------------------------------------------------------------------
# formats (assertion when asked)
# ----------------------------------------------------------------------------------
_DATE_RE = re.compile(r"([0-9]{4})-([0-9]{2})-([0-9]{2})")
_TIME_RE = re.compile(r"([0-9]{2}):([0-9]{2}):([0-9]{2})(?:\.[0-9]+)?(?:([Zz])|([+-])([0-9]{2}):([0-9]{2}))")
_UUID_RE = re.compile(r"[0-9A-Fa-f]{8}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{12}")
_IPV4_RE = re.compile(r"(?:25[0-5]|2[0-4][0-9]|1[0-9][0-9]|[1-9]?[0-9])(?:\.(?:25[0-5]|2[0-4][0-9]|1[0-9][0-9]|[1-9]?[0-9])){3}")
_IPV6_CHARS = re.compile(r"[0-9A-Fa-f:.]+")
_ATEXT = r"[A-Za-z0-9!#$%&'*+/=?^_`{|}~\-]+"
_LOCAL_RE = re.compile(rf'{_ATEXT}(?:\.{_ATEXT})*|"(?:[\x20\x21\x23-\x5b\x5d-\x7e]|\\[\x20-\x7e])*"')
_LABEL_RE = re.compile(r"[A-Za-z0-9](?:[A-Za-z0-9\-]{0,61}[A-Za-z0-9])?")
_URI_SCHEME_RE = re.compile(r"([A-Za-z][A-Za-z0-9+.\-]*):(.*)", re.S)
_URI_CHARS_RE = re.compile(r"(?:[A-Za-z0-9\-._~!$&'()*+,;=:@/?#\[\]]|%[0-9A-Fa-f]{2})*")
_DAYS = (31, 28, 31, 30, 31, 30, 31, 31, 30, 31, 30, 31)


def _date_ok(s: str) -> bool:
    m = _DATE_RE.fullmatch(s)
    if m is None:
        return False
    y, mo, d = int(m.group(1)), int(m.group(2)), int(m.group(3))
    if not 1 <= mo <= 12 or d < 1:
        return False
    leap = y % 4 == 0 and (y % 100 != 0 or y % 400 == 0)
    return d <= (29 if mo == 2 and leap else _DAYS[mo - 1])


def _time_ok(s: str) -> bool:
    m = _TIME_RE.fullmatch(s)
    if m is None:
        return False
    h, mi, sec = int(m.group(1)), int(m.group(2)), int(m.group(3))
    if h > 23 or mi > 59 or sec > 60:
        return False
    off = 0
    if m.group(4) is None:
        oh, om = int(m.group(6)), int(m.group(7))
        if oh > 23 or om > 59:
            return False
        off = (oh * 60 + om) * (1 if m.group(5) == "+" else -1)
    if sec == 60:
        return (h * 60 + mi - off) % 1440 == 23 * 60 + 59
    return True


def _date_time_ok(s: str) -> bool:
    if len(s) < 11 or s[10] not in "Tt":
        return False
    return _date_ok(s[:10]) and _time_ok(s[11:])


def _hostname_ok(s: str) -> bool:
    if not s or len(s) > 253:
        return False
    return all(_LABEL_RE.fullmatch(label) for label in s.split("."))


def _ipv4_ok(s: str) -> bool:
    return _IPV4_RE.fullmatch(s) is not None


def _ipv6_ok(s: str) -> bool:
    if _IPV6_CHARS.fullmatch(s) is None:
        return False
    try:
        ipaddress.IPv6Address(s)
    except ValueError:
        return False
    return True


def _email_ok(s: str) -> bool:
    at = s.rfind("@")
    if at <= 0:
        return False
    local, domain = s[:at], s[at + 1:]
    if _LOCAL_RE.fullmatch(local) is None:
        return False
    if domain.startswith("[") and domain.endswith("]"):
        inner = domain[1:-1]
        if inner[:5].lower() == "ipv6:":
            return _ipv6_ok(inner[5:])
        return _ipv4_ok(inner)
    return _hostname_ok(domain)


def _uri_ok(s: str) -> bool:
    m = _URI_SCHEME_RE.fullmatch(s)
    if m is None:
        return False
    rest = m.group(2)
    if _URI_CHARS_RE.fullmatch(rest) is None:
        return False
    rest, _, frag = rest.partition("#")
    if "#" in frag or "[" in frag or "]" in frag:
        return False
    rest, _, query = rest.partition("?")
    if "[" in query or "]" in query:
        return False
    if rest.startswith("//"):
        auth_end = len(rest)
        for k in range(2, len(rest)):
            if rest[k] == "/":
                auth_end = k
                break
        auth, path = rest[2:auth_end], rest[auth_end:]
        userinfo, at, hostport = auth.rpartition("@")
        if at and ("[" in userinfo or "]" in userinfo or "@" in userinfo):
            return False
        if hostport.startswith("["):
            close = hostport.find("]")
            if close < 0:
                return False
            literal, tail = hostport[1:close], hostport[close + 1:]
            if not (literal[:1] in "vV" and "." in literal) and not _ipv6_ok(literal):
                return False
            if tail and not (tail.startswith(":") and tail[1:].isdigit() or tail == ":"):
                return False
        else:
            host, _, port = hostport.partition(":")
            if "[" in host or "]" in host or (port and not (port.isascii() and port.isdigit())):
                return False
    else:
        path = rest
    return "[" not in path and "]" not in path


def _uuid_ok(s: str) -> bool:
    return _UUID_RE.fullmatch(s) is not None


_FORMAT_CHECKS: dict[str, Callable[[str], bool]] = {
    "date-time": _date_time_ok,
    "date": _date_ok,
    "time": _time_ok,
    "email": _email_ok,
    "uri": _uri_ok,
    "uuid": _uuid_ok,
    "ipv4": _ipv4_ok,
    "ipv6": _ipv6_ok,
}


# ----------------------------------------------------------------------------------
# compiled nodes
# ----------------------------------------------------------------------------------
class _NotDecomposable(Exception):
    """A document-level keyword that cannot be checked record by record."""

    def __init__(self, keyword: str) -> None:
        super().__init__(keyword)
        self.keyword = keyword


def _true(v: Any) -> bool:
    return True


def _false(v: Any) -> bool:
    return False


def _ptr(token: Any) -> str:
    return str(token).replace("~", "~0").replace("/", "~1")


class _Node:
    """One compiled subschema: parsed keyword values + the ``valid`` closure (hot path)."""

    __slots__ = (
        "value", "loc", "base", "boolean", "valid", "core",
        "types", "type_names", "intf", "enum", "enum_vals", "has_const", "const", "const_val",
        "minimum", "maximum", "xminimum", "xmaximum", "multiple",
        "min_len", "max_len", "pattern", "pattern_src", "fmt", "fmt_name",
        "prefix", "items", "contains", "min_contains", "max_contains", "min_items", "max_items", "unique",
        "props", "pprops", "addl", "pnames", "required", "dep_req", "dep_schemas", "min_props", "max_props",
        "ref", "all_of", "any_of", "one_of", "not_", "if_", "then", "else_", "uprops", "uitems", "needs_ann", "done",
    )

    def __init__(self, value: Any, loc: str, base: str) -> None:
        self.value = value
        self.loc = loc
        self.base = base
        self.boolean: bool | None = value if isinstance(value, bool) else None
        self.valid: Callable[[Any], bool] = _true
        self.core: Callable[[Any], bool] = _true
        self.types: tuple[type, ...] | None = None
        self.type_names: tuple[str, ...] = ()
        self.intf = False
        self.enum: Callable[[Any], bool] | None = None
        self.enum_vals: list[Any] = []
        self.has_const = False
        self.const: Callable[[Any], bool] | None = None
        self.const_val: Any = None
        self.minimum: _Bound | None = None
        self.maximum: _Bound | None = None
        self.xminimum: _Bound | None = None
        self.xmaximum: _Bound | None = None
        self.multiple: _Bound | None = None
        self.min_len: int | None = None
        self.max_len: int | None = None
        self.pattern: Callable[[str], Any] | None = None
        self.pattern_src = ""
        self.fmt: Callable[[str], bool] | None = None
        self.fmt_name = ""
        self.prefix: tuple[_Node, ...] = ()
        self.items: _Node | None = None
        self.contains: _Node | None = None
        self.min_contains = 1
        self.max_contains: int | None = None
        self.min_items: int | None = None
        self.max_items: int | None = None
        self.unique = False
        self.props: dict[str, _Node] = {}
        self.pprops: tuple[tuple[Callable[[str], Any], str, _Node], ...] = ()
        self.addl: _Node | None = None
        self.pnames: _Node | None = None
        self.required: tuple[str, ...] = ()
        self.dep_req: tuple[tuple[str, tuple[str, ...]], ...] = ()
        self.dep_schemas: tuple[tuple[str, _Node], ...] = ()
        self.min_props: int | None = None
        self.max_props: int | None = None
        self.ref: _Node | None = None
        self.all_of: tuple[_Node, ...] = ()
        self.any_of: tuple[_Node, ...] = ()
        self.one_of: tuple[_Node, ...] = ()
        self.not_: _Node | None = None
        self.if_: _Node | None = None
        self.then: _Node | None = None
        self.else_: _Node | None = None
        self.uprops: _Node | None = None
        self.uitems: _Node | None = None
        self.needs_ann = False
        self.done = False             # ``valid`` is final (closures may capture it directly)
        if self.boolean is not None:
            self.valid = self.core = _true if self.boolean else _false
            self.done = True

    def __repr__(self) -> str:
        return f"<schema node {self.loc or '/'}>"

    # -- type ------------------------------------------------------------------------------
    def type_ok(self, v: Any) -> bool:
        tp = type(v)
        types = self.types
        if types is None or tp in types:
            return True
        return self.intf and tp in _FLOATISH and _integral(v)

    def inplace(self) -> list[_Node]:
        """Subschemas applied to the same instance (graph of in-place applicators)."""
        out: list[_Node] = []
        if self.ref is not None:
            out.append(self.ref)
        out.extend(self.all_of)
        out.extend(self.any_of)
        out.extend(self.one_of)
        for n in (self.not_, self.if_, self.then, self.else_):
            if n is not None:
                out.append(n)
        out.extend(n for _k, n in self.dep_schemas)
        return out

    # -- annotations (unevaluated*) -----------------------------------------------------------
    def ann(self, v: Any) -> tuple[Any, Any] | None:
        """``None`` when ``v`` is invalid, else ``(evaluated property names, evaluated item
        indices)``, each ``None`` (nothing), a set, or ``True`` (all)."""
        if self.boolean is not None:
            return (None, None) if self.boolean else None
        if not self.core(v):
            return None
        P: Any = None
        I: Any = None
        tp = type(v)
        if tp is dict:
            props = self.props
            pp = self.pprops
            addl = self.addl
            if props or pp or addl is not None:
                got: set[str] = set()
                for k, x in v.items():
                    c = props.get(k)
                    matched = False
                    if c is not None:
                        if not c.valid(x):
                            return None
                        matched = True
                    for rx, _src, pn in pp:
                        if rx(k) is not None:
                            matched = True
                            if not pn.valid(x):
                                return None
                    if not matched and addl is not None:
                        if not addl.valid(x):
                            return None
                        matched = True
                    if matched:
                        got.add(k)
                P = got
            for k, dn in self.dep_schemas:
                if k in v:
                    a = dn.ann(v)
                    if a is None:
                        return None
                    P, I = _merge(P, I, a)
        elif tp is list:
            n = len(v)
            np_ = min(n, len(self.prefix))
            if np_:
                pre = self.prefix
                for i in range(np_):
                    if not pre[i].valid(v[i]):
                        return None
                I = set(range(np_))
            if self.items is not None:
                it = self.items
                for i in range(len(self.prefix), n):
                    if not it.valid(v[i]):
                        return None
                if n > len(self.prefix):
                    I = True
            if self.contains is not None:
                cn = self.contains
                hits = {i for i in range(n) if cn.valid(v[i])}
                if len(hits) < self.min_contains or (self.max_contains is not None and len(hits) > self.max_contains):
                    return None
                if hits and I is not True:
                    I = hits if I is None else (I | hits)
        if self.ref is not None:
            a = self.ref.ann(v)
            if a is None:
                return None
            P, I = _merge(P, I, a)
        for c in self.all_of:
            a = c.ann(v)
            if a is None:
                return None
            P, I = _merge(P, I, a)
        if self.any_of:
            ok = False
            for c in self.any_of:
                a = c.ann(v)
                if a is not None:
                    ok = True
                    P, I = _merge(P, I, a)
            if not ok:
                return None
        if self.one_of:
            found = None
            count = 0
            for c in self.one_of:
                a = c.ann(v)
                if a is not None:
                    count += 1
                    found = a
            if count != 1:
                return None
            P, I = _merge(P, I, found)
        if self.if_ is not None:
            a = self.if_.ann(v)
            if a is not None:
                P, I = _merge(P, I, a)
                if self.then is not None:
                    b = self.then.ann(v)
                    if b is None:
                        return None
                    P, I = _merge(P, I, b)
            elif self.else_ is not None:
                b = self.else_.ann(v)
                if b is None:
                    return None
                P, I = _merge(P, I, b)
        if self.uprops is not None and tp is dict:
            if P is not True:
                up = self.uprops
                for k, x in v.items():
                    if (P is None or k not in P) and not up.valid(x):
                        return None
            P = True
        if self.uitems is not None and tp is list:
            if I is not True:
                ui = self.uitems
                for i, x in enumerate(v):
                    if (I is None or i not in I) and not ui.valid(x):
                        return None
            I = True
        return P, I

    def evaluated(self, v: Any) -> tuple[Any, Any]:
        """Annotations for error reports: what this schema's own keywords matched (whatever the
        result of their subschemas) and what its valid in-place subschemas evaluated."""
        P: Any = None
        I: Any = None
        tp = type(v)
        if tp is dict:
            if self.addl is not None:
                P = True
            else:
                got = {k for k in v if k in self.props or any(rx(k) is not None for rx, _s, _n in self.pprops)}
                P = got or None
            for k, dn in self.dep_schemas:
                if k in v:
                    a = dn.ann(v)
                    if a is not None:
                        P, I = _merge(P, I, a)
        elif tp is list:
            n = len(v)
            if self.items is not None and n > len(self.prefix):
                I = True
            elif self.prefix:
                I = set(range(min(n, len(self.prefix))))
            if self.contains is not None and I is not True:
                hits = {i for i in range(n) if self.contains.valid(v[i])}
                if hits:
                    I = hits if I is None else (I | hits)
        subs: list[_Node] = []
        if self.ref is not None:
            subs.append(self.ref)
        subs.extend(self.all_of)
        subs.extend(self.any_of)
        subs.extend(self.one_of)
        for c in subs:
            a = c.ann(v)
            if a is not None:
                P, I = _merge(P, I, a)
        if self.if_ is not None:
            a = self.if_.ann(v)
            branch = self.then if a is not None else self.else_
            if a is not None:
                P, I = _merge(P, I, a)
            if branch is not None:
                b = branch.ann(v)
                if b is not None:
                    P, I = _merge(P, I, b)
        return P, I

    # -- errors ---------------------------------------------------------------------------------
    def errs(self, v: Any, path: tuple, ctx: _ErrCtx) -> None:
        """Append the errors of ``v`` (at instance ``path``) to ``ctx``."""
        if ctx.full():
            return
        anc = False
        hole = ctx.hole
        if hole is not None:
            rel = hole.relation(path)
            if rel == 2:
                hole.nodes.append(self)
                return
            anc = rel == 1
        if self.boolean is not None:
            if not self.boolean:
                ctx.add(path, "false", self.loc, {})
            return
        loc = self.loc
        tp = type(v)
        if self.types is not None and not self.type_ok(v):
            ctx.add(path, "type", loc + "/type", {"expected": list(self.type_names), "actual": _instance_type(v)})
        if self.enum is not None:
            if anc:
                raise _NotDecomposable("enum")
            if not self.enum(v):
                ctx.add(path, "enum", loc + "/enum", {"value": _show(v), "allowed": [_show(x) for x in self.enum_vals]})
        if self.has_const:
            if anc:
                raise _NotDecomposable("const")
            if not self.const(v):  # type: ignore[misc]
                ctx.add(path, "const", loc + "/const", {"value": _show(v), "expected": _show(self.const_val)})
        if tp in _NUM_TYPES:
            self._num_errs(v, path, ctx)
        elif tp is str:
            self._str_errs(v, path, ctx)
        elif tp is list:
            self._arr_errs(v, path, ctx, anc)
        elif tp is dict:
            self._obj_errs(v, path, ctx, anc)
        if self.ref is not None:
            if hole is not None or not self.ref.valid(v):
                self.ref.errs(v, path, ctx)
        for c in self.all_of:
            if hole is not None or not c.valid(v):
                c.errs(v, path, ctx)
        if self.any_of:
            if anc:
                raise _NotDecomposable("anyOf")
            if not any(c.valid(v) for c in self.any_of):
                ctx.add(path, "anyOf", loc + "/anyOf", {"count": len(self.any_of)})
        if self.one_of:
            if anc:
                raise _NotDecomposable("oneOf")
            matched = [i for i, c in enumerate(self.one_of) if c.valid(v)]
            if len(matched) != 1:
                ctx.add(path, "oneOf", loc + "/oneOf", {"count": len(self.one_of), "matched": len(matched)})
        if self.not_ is not None:
            if anc:
                raise _NotDecomposable("not")
            if self.not_.valid(v):
                ctx.add(path, "not", loc + "/not", {})
        if self.if_ is not None:
            if anc:
                raise _NotDecomposable("if")
            branch = self.then if self.if_.valid(v) else self.else_
            if branch is not None and not branch.valid(v):
                branch.errs(v, path, ctx)
        if (self.uprops is not None and tp is dict) or (self.uitems is not None and tp is list):
            if anc:
                raise _NotDecomposable("unevaluatedProperties" if tp is dict else "unevaluatedItems")
            P, I = self.evaluated(v)
            if tp is dict and self.uprops is not None and P is not True:
                up = self.uprops
                for k, x in v.items():
                    if (P is None or k not in P) and not up.valid(x):
                        if up.boolean is False:
                            ctx.add(path + (k,), "unevaluatedProperties", loc + "/unevaluatedProperties",
                                    {"property": k})
                        else:
                            up.errs(x, path + (k,), ctx)
            if tp is list and self.uitems is not None and I is not True:
                ui = self.uitems
                for i, x in enumerate(v):
                    if (I is None or i not in I) and not ui.valid(x):
                        if ui.boolean is False:
                            ctx.add(path + (i,), "unevaluatedItems", loc + "/unevaluatedItems", {"index": i})
                        else:
                            ui.errs(x, path + (i,), ctx)

    def _num_errs(self, v: Any, path: tuple, ctx: _ErrCtx) -> None:
        loc = self.loc
        if _nan(v):
            ctx.add(path, "type", loc, {"expected": list(_TYPE_NAMES), "actual": "number"})
            return
        text = _num_text(v)
        if self.minimum is not None and _cmp(v, self.minimum) < 0:
            ctx.add(path, "minimum", loc + "/minimum", {"value": text, "limit": self.minimum.text})
        if self.maximum is not None and _cmp(v, self.maximum) > 0:
            ctx.add(path, "maximum", loc + "/maximum", {"value": text, "limit": self.maximum.text})
        if self.xminimum is not None and _cmp(v, self.xminimum) <= 0:
            ctx.add(path, "exclusiveMinimum", loc + "/exclusiveMinimum", {"value": text, "limit": self.xminimum.text})
        if self.xmaximum is not None and _cmp(v, self.xmaximum) >= 0:
            ctx.add(path, "exclusiveMaximum", loc + "/exclusiveMaximum", {"value": text, "limit": self.xmaximum.text})
        if self.multiple is not None and not _multiple_of(v, self.multiple):
            ctx.add(path, "multipleOf", loc + "/multipleOf", {"value": text, "divisor": self.multiple.text})

    def _str_errs(self, v: str, path: tuple, ctx: _ErrCtx) -> None:
        loc = self.loc
        n = len(v)
        if self.min_len is not None and n < self.min_len:
            ctx.add(path, "minLength", loc + "/minLength", {"length": n, "limit": self.min_len})
        if self.max_len is not None and n > self.max_len:
            ctx.add(path, "maxLength", loc + "/maxLength", {"length": n, "limit": self.max_len})
        if self.pattern is not None and self.pattern(v) is None:
            ctx.add(path, "pattern", loc + "/pattern", {"pattern": self.pattern_src})
        if self.fmt is not None and not self.fmt(v):
            ctx.add(path, "format", loc + "/format", {"format": self.fmt_name})

    def _arr_errs(self, v: list, path: tuple, ctx: _ErrCtx, anc: bool) -> None:
        loc = self.loc
        n = len(v)
        if self.min_items is not None and n < self.min_items:
            ctx.add(path, "minItems", loc + "/minItems", {"count": n, "limit": self.min_items})
        if self.max_items is not None and n > self.max_items:
            ctx.add(path, "maxItems", loc + "/maxItems", {"count": n, "limit": self.max_items})
        if self.unique:
            if anc:
                raise _NotDecomposable("uniqueItems")
            pair = _duplicate_pair(v)
            if pair is not None:
                ctx.add(path, "uniqueItems", loc + "/uniqueItems", {"first": pair[0], "second": pair[1]})
        exact = ctx.hole is None
        pre = self.prefix
        for i in range(min(n, len(pre))):
            c = pre[i]
            if exact and c.valid(v[i]):
                continue
            if c.boolean is False:
                ctx.add(path + (i,), "prefixItems", loc + f"/prefixItems/{i}", {"index": i})
            else:
                c.errs(v[i], path + (i,), ctx)
        if self.items is not None:
            it = self.items
            for i in range(len(pre), n):
                if ctx.full():
                    return
                if exact and it.valid(v[i]):
                    continue
                if it.boolean is False:
                    ctx.add(path + (i,), "items", loc + "/items", {"index": i, "limit": len(pre)})
                else:
                    it.errs(v[i], path + (i,), ctx)
        if self.contains is not None:
            if anc:
                raise _NotDecomposable("contains")
            cn = self.contains
            hits = sum(1 for x in v if cn.valid(x))
            if hits < self.min_contains:
                ctx.add(path, "contains", loc + ("/minContains" if "minContains" in self.value else "/contains"),
                        {"count": hits, "limit": self.min_contains})
            if self.max_contains is not None and hits > self.max_contains:
                ctx.add(path, "maxContains", loc + "/maxContains", {"count": hits, "limit": self.max_contains})

    def _obj_errs(self, v: dict, path: tuple, ctx: _ErrCtx, anc: bool) -> None:
        loc = self.loc
        for k in self.required:
            if k not in v:
                ctx.add(path, "required", loc + "/required", {"property": k})
        for k, deps in self.dep_req:
            if k in v:
                for d in deps:
                    if d not in v:
                        ctx.add(path, "dependentRequired", loc + "/dependentRequired",
                                {"property": k, "required": d})
        n = len(v)
        if self.min_props is not None and n < self.min_props:
            ctx.add(path, "minProperties", loc + "/minProperties", {"count": n, "limit": self.min_props})
        if self.max_props is not None and n > self.max_props:
            ctx.add(path, "maxProperties", loc + "/maxProperties", {"count": n, "limit": self.max_props})
        if self.pnames is not None:
            pn = self.pnames
            for k in v:
                if not pn.valid(k):
                    ctx.add(path + (k,), "propertyNames", loc + "/propertyNames", {"property": k})
        exact = ctx.hole is None
        props = self.props
        pp = self.pprops
        addl = self.addl
        if props or pp or addl is not None:
            for k, x in v.items():
                if ctx.full():
                    return
                sub = path + (k,)
                c = props.get(k)
                matched = c is not None
                if c is not None and not (exact and c.valid(x)):
                    if c.boolean is False:
                        ctx.add(sub, "properties", loc + "/properties/" + _ptr(k), {"property": k})
                    else:
                        c.errs(x, sub, ctx)
                for rx, src, pn in pp:
                    if rx(k) is not None:
                        matched = True
                        if not (exact and pn.valid(x)):
                            if pn.boolean is False:
                                ctx.add(sub, "patternProperties", loc + "/patternProperties/" + _ptr(src),
                                        {"property": k, "pattern": src})
                            else:
                                pn.errs(x, sub, ctx)
                if not matched and addl is not None and not (exact and addl.valid(x)):
                    if addl.boolean is False:
                        ctx.add(sub, "additionalProperties", loc + "/additionalProperties", {"property": k})
                    else:
                        addl.errs(x, sub, ctx)
        for k, dn in self.dep_schemas:
            if k in v and (not exact or not dn.valid(v)):
                if dn.boolean is False:
                    ctx.add(path, "dependentSchemas", loc + "/dependentSchemas/" + _ptr(k), {"property": k})
                else:
                    dn.errs(v, path, ctx)


def _merge(P: Any, I: Any, a: tuple[Any, Any]) -> tuple[Any, Any]:
    p, i = a
    if p is not None:
        if P is True or p is True:
            P = True
        elif P is None:
            P = set(p)
        else:
            P = P | p
    if i is not None:
        if I is True or i is True:
            I = True
        elif I is None:
            I = set(i)
        else:
            I = I | i
    return P, I


def _duplicate_pair(v: list) -> tuple[int, int] | None:
    """Indices of the first two equal items (canonical equality), or ``None``."""
    seen: dict[Any, int] = {}
    for i, x in enumerate(v):
        key: Any = ("s", x) if type(x) is str else _canon(x)   # a str never equals another type
        if key is None:
            continue
        j = seen.get(key)
        if j is not None:
            return j, i
        seen[key] = i
    return None


def _unique(v: list) -> bool:
    n = len(v)
    if n < 2:
        return True
    if all(type(x) is str for x in v):
        return len(set(v)) == n
    return _duplicate_pair(v) is None


def _instance_type(v: Any) -> str:
    tp = type(v)
    if tp is dict:
        return "object"
    if tp is list:
        return "array"
    if tp is str:
        return "string"
    if tp is bool:
        return "boolean"
    if v is None:
        return "null"
    if tp in _NUM_TYPES:
        return "integer" if not _nan(v) and _integral(v) else "number"
    return type(v).__name__


def _num_text(v: Any) -> str:
    """Exact JSON text of a number (also a Python ``int`` beyond the 4300-digit ``str()``
    limit, through ``Decimal``, whose conversions have no such limit)."""
    if isinstance(v, Decimal):
        return str(v)
    try:
        return codec.number_text(v)
    except ValueError:
        return str(Decimal(int(v)))
    except TypeError:  # pragma: no cover - numbers only
        return repr(v)


_SHOW_CHARS = 80


def _show(v: Any) -> str:
    """Compact display text of a value (exact numbers, cut to 80 characters)."""
    try:
        text = codec.dumps(v).decode("utf-8", "replace")
    except Exception:
        text = repr(v)
    if len(text) > _SHOW_CHARS:
        text = text[:_SHOW_CHARS - 1] + "…"
    return text


class _Hole:
    """The record container inside a wrapper being validated (document scope)."""

    __slots__ = ("path", "prefixes", "nodes")

    def __init__(self, path: tuple) -> None:
        self.path = tuple(path)
        self.prefixes = {self.path[:k] for k in range(len(self.path))}
        self.nodes: list[_Node] = []

    def relation(self, path: tuple) -> int:
        """2: ``path`` is the hole; 1: an ancestor of it; 0: unrelated."""
        if path == self.path:
            return 2
        return 1 if path in self.prefixes else 0


class _ErrCtx:
    __slots__ = ("out", "limit", "hole")

    def __init__(self, limit: int | None, hole: _Hole | None = None) -> None:
        self.out: list[tuple[tuple, str, str, dict[str, Any]]] = []
        self.limit = limit if limit is not None else 1 << 62
        self.hole = hole

    def full(self) -> bool:
        return len(self.out) >= self.limit

    def add(self, path: tuple, keyword: str, loc: str, params: dict[str, Any]) -> None:
        if len(self.out) < self.limit:
            self.out.append((path, keyword, loc, params))


class _WholeCtx(_ErrCtx):
    """Errors of a whole-document validation (review R5A-4: one global cap of 100 000 errors made
    the invalid and issue counts silently wrong): the errors of each record -- instance paths
    below the record container ``cpath``, by the record's segment -- under their own limit
    (``per_record``: what a decomposed validation keeps), the document's own errors besides;
    every error is visited (``full()`` is never true), so ``seen`` knows every invalid record and
    how many errors it has. At most :data:`WHOLE_DOCUMENT_ERRORS` are kept in all (memory):
    further ones are counted in ``dropped``."""

    __slots__ = ("cpath", "n", "per_record", "seen", "dropped")

    def __init__(self, cpath: tuple, per_record: int | None) -> None:
        super().__init__(WHOLE_DOCUMENT_ERRORS)
        self.cpath = cpath
        self.n = len(cpath)
        self.per_record = per_record
        self.seen: dict[Any, int] = {}       # record segment -> errors found
        self.dropped = 0

    def full(self) -> bool:
        return False

    def add(self, path: tuple, keyword: str, loc: str, params: dict[str, Any]) -> None:
        n = self.n
        if len(path) > n and path[:n] == self.cpath:
            seg = path[n]
            if isinstance(seg, (int, str)) and not isinstance(seg, bool):
                c = self.seen.get(seg, 0)
                self.seen[seg] = c + 1
                if self.per_record is not None and c >= self.per_record:
                    return                   # beyond the record's limit (not collected, like a decomposed run)
        if len(self.out) >= self.limit:
            self.dropped += 1
            return
        self.out.append((path, keyword, loc, params))


# ----------------------------------------------------------------------------------
# the compiler
# ----------------------------------------------------------------------------------
_SUB_ONE = ("additionalProperties", "items", "contains", "not", "if", "then", "else", "propertyNames",
            "unevaluatedItems", "unevaluatedProperties", "contentSchema")
_SUB_LIST = ("prefixItems", "allOf", "anyOf", "oneOf")
_SUB_MAP = ("properties", "patternProperties", "$defs", "dependentSchemas")


def _schema_error(key: str, **params: Any) -> UserInputError:
    return UserInputError(key, **params)


def _where(loc: str) -> str:
    return loc or "/"


class _Compiler:
    """Registry of the schema resources (``$id``, ``$anchor``) and the node cache."""

    def __init__(self, root: Any, formats: bool, resources: Mapping[str, Any] | None) -> None:
        self.root = root
        self.formats = bool(formats)
        self.resources: dict[str, Any] = {}
        for uri, value in (resources or {}).items():
            self.resources[_strip_fragment(str(uri))] = value
        self.res: dict[str, Any] = {}                 # resource URI -> schema object
        self.anchors: dict[str, Any] = {}             # URI#name -> schema object
        self.base_of: dict[int, str] = {}             # id(schema object) -> base URI
        self.loc_of: dict[int, str] = {}              # id(schema object) -> display location
        self.nodes: dict[int, _Node] = {}
        self.keep: list[Any] = []                     # objects whose id() keys the dicts above
        self.regexes: dict[str, Callable[[str], Any]] = {}
        self.loaded: set[str] = set()
        self._walk(root, _ROOT_BASE, "", resource=_ROOT_BASE)

    # -- registry -----------------------------------------------------------------------------
    def _walk(self, obj: Any, base: str, loc: str, *, resource: str | None = None) -> None:
        stack: list[tuple[Any, str, str, str | None]] = [(obj, base, loc, resource)]
        while stack:
            s, b, where, res = stack.pop()
            if not isinstance(s, dict):
                continue
            if res is not None:
                self.res.setdefault(res, s)
            if "$schema" in s:
                dialect = s["$schema"]
                if not isinstance(dialect, str):
                    raise _schema_error("jschema.error.schema.keyword", keyword="$schema", location=_where(where),
                                        expected=t("jschema.expected.uri"))
                if dialect not in _DIALECTS:
                    raise _schema_error("jschema.error.schema.dialect", value=dialect, location=_where(where))
            if "$id" in s:
                ident = s["$id"]
                if not isinstance(ident, str):
                    raise _schema_error("jschema.error.schema.keyword", keyword="$id", location=_where(where),
                                        expected=t("jschema.expected.uri"))
                uri, frag = _split_uri(self._join(b, ident, where))
                if frag:
                    raise _schema_error("jschema.error.schema.id", value=ident, location=_where(where))
                b = uri
                self.res.setdefault(uri, s)
            if "$dynamicRef" in s or "$dynamicAnchor" in s:
                kw = "$dynamicRef" if "$dynamicRef" in s else "$dynamicAnchor"
                raise _schema_error("jschema.error.schema.dynamic", keyword=kw, location=_where(where))
            if "$vocabulary" in s:
                vocab = s["$vocabulary"]
                if not isinstance(vocab, dict):
                    raise _schema_error("jschema.error.schema.keyword", keyword="$vocabulary",
                                        location=_where(where), expected=t("jschema.expected.object"))
                for uri, required in vocab.items():
                    if required is True and uri not in _STANDARD_VOCABULARIES:
                        raise _schema_error("jschema.error.schema.vocabulary", vocabulary=uri)
            if "$anchor" in s:
                anchor = s["$anchor"]
                if not isinstance(anchor, str) or _ANCHOR_RE.match(anchor) is None:
                    raise _schema_error("jschema.error.schema.keyword", keyword="$anchor", location=_where(where),
                                        expected=t("jschema.expected.anchor"))
                self.anchors.setdefault(b + "#" + anchor, s)
            key = id(s)
            if key not in self.base_of:
                self.base_of[key] = b
                self.loc_of[key] = where
                self.keep.append(s)
            for kw in _SUB_ONE:
                if kw in s:
                    stack.append((s[kw], b, f"{where}/{kw}", None))
            for kw in _SUB_LIST:
                sub = s.get(kw)
                if isinstance(sub, list):
                    for i, x in enumerate(sub):
                        stack.append((x, b, f"{where}/{kw}/{i}", None))
            for kw in _SUB_MAP:
                sub = s.get(kw)
                if isinstance(sub, dict):
                    for k, x in sub.items():
                        stack.append((x, b, f"{where}/{kw}/{_ptr(k)}", None))
            deps = s.get("dependencies")
            if isinstance(deps, dict):
                for k, x in deps.items():
                    if isinstance(x, (dict, bool)):
                        stack.append((x, b, f"{where}/dependencies/{_ptr(k)}", None))

    def _join(self, base: str, ref: str, loc: str) -> str:
        if ref.startswith("#"):
            return base.split("#", 1)[0] + ref
        if _SCHEME_RE.match(ref):
            return ref
        joined = urljoin(base, ref)
        if not _SCHEME_RE.match(joined):
            raise _schema_error("jschema.error.schema.remote_ref", ref=ref, location=_where(loc))
        return joined

    def resolve(self, ref: str, base: str, loc: str) -> tuple[Any, str, str]:
        """``(schema object, its base URI, its display location)`` of ``$ref``."""
        uri, frag = _split_uri(self._join(base, ref, loc))
        doc = self.res.get(uri)
        if doc is None and uri in self.resources and uri not in self.loaded:
            self.loaded.add(uri)
            raw = self.resources[uri]
            self._walk(raw, uri, uri + "#", resource=uri)
            doc = self.res.get(uri)
        if doc is None:
            raise _schema_error("jschema.error.schema.remote_ref", ref=ref, location=_where(loc))
        if not frag:
            return doc, self.base_of.get(id(doc), uri), self.loc_of.get(id(doc), "")
        if frag.startswith("/"):
            target = doc
            tbase = self.base_of.get(id(doc), uri)
            for token in frag[1:].split("/"):
                token = token.replace("~1", "/").replace("~0", "~")
                if isinstance(target, dict) and token in target:
                    target = target[token]
                elif isinstance(target, list) and token.isdigit() and (token == "0" or token[0] != "0") \
                        and int(token) < len(target):
                    target = target[int(token)]
                else:
                    raise _schema_error("jschema.error.schema.unresolved_ref", ref=ref, location=_where(loc))
                tbase = self.base_of.get(id(target), tbase) if isinstance(target, dict) else tbase
            tloc = self.loc_of.get(id(target)) if isinstance(target, dict) else None
            if tloc is None:
                tloc = self.loc_of.get(id(doc), "") + frag
            return target, tbase, tloc
        hit = self.anchors.get(uri + "#" + frag)
        if hit is None:
            raise _schema_error("jschema.error.schema.unresolved_ref", ref=ref, location=_where(loc))
        return hit, self.base_of.get(id(hit), uri), self.loc_of.get(id(hit), "")

    # -- nodes ----------------------------------------------------------------------------------
    def node(self, obj: Any, base: str, loc: str) -> _Node:
        if obj is True or obj is False:
            return _Node(obj, loc, base)
        if not isinstance(obj, dict):
            raise _schema_error("jschema.error.schema.not_schema", location=_where(loc))
        key = id(obj)
        hit = self.nodes.get(key)
        if hit is not None:
            return hit
        n = _Node(obj, self.loc_of.get(key, loc), self.base_of.get(key, base))
        self.nodes[key] = n
        self.keep.append(obj)
        self._fill(n, obj)
        return n

    def _child(self, value: Any, base: str, loc: str) -> _Node:
        if isinstance(value, dict):
            b = self.base_of.get(id(value), base)
            return self.node(value, b, self.loc_of.get(id(value), loc))
        return self.node(value, base, loc)

    def _regex(self, pattern: Any, loc: str) -> Callable[[str], Any]:
        if not isinstance(pattern, str):
            raise _schema_error("jschema.error.schema.keyword", keyword="pattern", location=_where(loc),
                                expected=t("jschema.expected.regex"))
        hit = self.regexes.get(pattern)
        if hit is not None:
            return hit
        try:
            rx = re.compile(ecma_regex(pattern), re.ASCII)
        except (_RegexError, re.error, ValueError, OverflowError, RecursionError) as exc:
            raise _schema_error("jschema.error.schema.regex", pattern=pattern, location=_where(loc),
                                detail=str(exc)) from None
        self.regexes[pattern] = rx.search
        return rx.search

    def _fill(self, n: _Node, s: dict) -> None:
        loc = n.loc
        base = n.base

        def at(kw: str) -> str:
            return f"{loc}/{_ptr(kw)}"

        def bad(kw: str, expected: str) -> UserInputError:
            return _schema_error("jschema.error.schema.keyword", keyword=kw, location=_where(at(kw)),
                                 expected=t(expected))

        def count(kw: str) -> int | None:
            if kw not in s:
                return None
            v = s[kw]
            if not _is_number(v) or _nan(v) or not _integral(v) or _exact(v) < 0:
                raise bad(kw, "jschema.expected.count")
            return _count_value(_exact(v))

        def number(kw: str) -> _Bound | None:
            if kw not in s:
                return None
            v = s[kw]
            if not _is_number(v) or _nan(v):
                raise bad(kw, "jschema.expected.number")
            return _Bound(v)

        def schema_list(kw: str) -> tuple[_Node, ...]:
            if kw not in s:
                return ()
            v = s[kw]
            if not isinstance(v, list) or not v:
                raise bad(kw, "jschema.expected.schema_list")
            return tuple(self._child(x, base, f"{at(kw)}/{i}") for i, x in enumerate(v))

        def one(kw: str) -> _Node | None:
            if kw not in s:
                return None
            v = s[kw]
            if not isinstance(v, (dict, bool)):
                raise bad(kw, "jschema.expected.schema")
            return self._child(v, base, at(kw))

        def str_list(kw: str, v: Any) -> tuple[str, ...]:
            if not isinstance(v, list) or not all(isinstance(x, str) for x in v) or len(set(v)) != len(v):
                raise bad(kw, "jschema.expected.string_list")
            return tuple(v)

        if "$ref" in s:
            ref = s["$ref"]
            if not isinstance(ref, str):
                raise bad("$ref", "jschema.expected.uri")
            target, tbase, tloc = self.resolve(ref, base, at("$ref"))
            n.ref = self.node(target, tbase, tloc) if isinstance(target, (dict, bool)) else \
                _raise_schema("jschema.error.schema.not_schema", tloc)
        if "type" in s:
            tv = s["type"]
            names = [tv] if isinstance(tv, str) else tv
            if not isinstance(names, list) or not names or len(set(map(str, names))) != len(names) or \
                    any(not isinstance(x, str) or x not in _TYPE_PY for x in names):
                raise bad("type", "jschema.expected.type")
            n.type_names = tuple(names)
            pys: list[type] = []
            for name in names:
                pys.extend(_TYPE_PY[name])
            n.types = tuple(dict.fromkeys(pys))
            n.intf = "integer" in names and "number" not in names
        if "enum" in s:
            vals = s["enum"]
            if not isinstance(vals, list):
                raise bad("enum", "jschema.expected.array")
            n.enum_vals = list(vals)
            n.enum = _enum_check(vals)
        if "const" in s:
            n.has_const = True
            n.const_val = s["const"]
            n.const = _enum_check([s["const"]])
        n.minimum = number("minimum")
        n.maximum = number("maximum")
        n.xminimum = number("exclusiveMinimum")
        n.xmaximum = number("exclusiveMaximum")
        n.multiple = number("multipleOf")
        if n.multiple is not None and not n.multiple.x > 0:
            raise bad("multipleOf", "jschema.expected.positive")
        n.min_len = count("minLength")
        n.max_len = count("maxLength")
        if "pattern" in s:
            n.pattern_src = s["pattern"] if isinstance(s["pattern"], str) else ""
            n.pattern = self._regex(s["pattern"], at("pattern"))
        if "format" in s:
            fmt = s["format"]
            if not isinstance(fmt, str):
                raise bad("format", "jschema.expected.string")
            n.fmt_name = fmt
            if self.formats:
                n.fmt = _FORMAT_CHECKS.get(fmt)
        # arrays
        n.prefix = schema_list("prefixItems")
        if "items" in s and not isinstance(s["items"], (dict, bool)):
            raise bad("items", "jschema.expected.schema")
        n.items = one("items")
        n.contains = one("contains")
        mc = count("minContains")
        if mc is not None and n.contains is not None:
            n.min_contains = mc
        n.max_contains = count("maxContains") if n.contains is not None else None
        n.min_items = count("minItems")
        n.max_items = count("maxItems")
        if "uniqueItems" in s:
            if not isinstance(s["uniqueItems"], bool):
                raise bad("uniqueItems", "jschema.expected.boolean")
            n.unique = s["uniqueItems"]
        n.uitems = one("unevaluatedItems")
        # objects
        if "properties" in s:
            props = s["properties"]
            if not isinstance(props, dict) or not all(isinstance(x, (dict, bool)) for x in props.values()):
                raise bad("properties", "jschema.expected.schema_map")
            n.props = {k: self._child(x, base, f"{at('properties')}/{_ptr(k)}") for k, x in props.items()}
        if "patternProperties" in s:
            pp = s["patternProperties"]
            if not isinstance(pp, dict) or not all(isinstance(x, (dict, bool)) for x in pp.values()):
                raise bad("patternProperties", "jschema.expected.schema_map")
            n.pprops = tuple((self._regex(k, at("patternProperties")), k,
                              self._child(x, base, f"{at('patternProperties')}/{_ptr(k)}")) for k, x in pp.items())
        n.addl = one("additionalProperties")
        n.pnames = one("propertyNames")
        if "required" in s:
            n.required = str_list("required", s["required"])
        dep_req: list[tuple[str, tuple[str, ...]]] = []
        dep_schemas: list[tuple[str, _Node]] = []
        if "dependentRequired" in s:
            dr = s["dependentRequired"]
            if not isinstance(dr, dict):
                raise bad("dependentRequired", "jschema.expected.string_list_map")
            dep_req.extend((k, str_list("dependentRequired", x)) for k, x in dr.items())
        if "dependentSchemas" in s:
            ds = s["dependentSchemas"]
            if not isinstance(ds, dict):
                raise bad("dependentSchemas", "jschema.expected.schema_map")
            for k, x in ds.items():
                if not isinstance(x, (dict, bool)):
                    raise bad("dependentSchemas", "jschema.expected.schema_map")
                dep_schemas.append((k, self._child(x, base, f"{at('dependentSchemas')}/{_ptr(k)}")))
        deps = s.get("dependencies")
        if isinstance(deps, dict):              # draft-07 compatibility (optional in 2020-12)
            for k, x in deps.items():
                if isinstance(x, list):
                    dep_req.append((k, str_list("dependencies", x)))
                elif isinstance(x, (dict, bool)):
                    dep_schemas.append((k, self._child(x, base, f"{at('dependencies')}/{_ptr(k)}")))
                else:
                    raise bad("dependencies", "jschema.expected.schema_map")
        n.dep_req = tuple(dep_req)
        n.dep_schemas = tuple(dep_schemas)
        n.min_props = count("minProperties")
        n.max_props = count("maxProperties")
        n.uprops = one("unevaluatedProperties")
        # applicators
        n.all_of = schema_list("allOf")
        n.any_of = schema_list("anyOf")
        n.one_of = schema_list("oneOf")
        n.not_ = one("not")
        n.if_ = one("if")
        if n.if_ is not None:
            n.then = one("then")
            n.else_ = one("else")
        else:
            one("then")
            one("else")
        n.needs_ann = n.uprops is not None or n.uitems is not None
        _build_closures(n)

    # -- checks over the whole graph ------------------------------------------------------------------
    def check_cycles(self) -> None:
        """Refuse ``$ref`` (in-place) cycles: evaluating them would never end."""
        state: dict[int, int] = {}
        for start in list(self.nodes.values()):
            if state.get(id(start)):
                continue
            stack: list[tuple[_Node, Iterator[_Node]]] = [(start, iter(start.inplace()))]
            state[id(start)] = 1
            while stack:
                node, it = stack[-1]
                nxt = next(it, None)
                if nxt is None:
                    state[id(node)] = 2
                    stack.pop()
                    continue
                if nxt.boolean is not None:
                    continue
                st = state.get(id(nxt), 0)
                if st == 1:
                    raise _schema_error("jschema.error.schema.ref_cycle", location=_where(nxt.loc))
                if st == 0:
                    state[id(nxt)] = 1
                    stack.append((nxt, iter(nxt.inplace())))


def _raise_schema(key: str, loc: str) -> Any:
    raise _schema_error(key, location=_where(loc))


def _strip_fragment(uri: str) -> str:
    return uri[:-1] if uri.endswith("#") else uri


def _split_uri(uri: str) -> tuple[str, str]:
    base, sep, frag = uri.partition("#")
    return base, unquote(frag) if sep else ""


def _enum_check(vals: list[Any]) -> Callable[[Any], bool]:
    if all(type(x) is str for x in vals):
        allowed = frozenset(vals)
        return lambda v: type(v) is str and v in allowed
    canon = frozenset(c for c in (_canon(x) for x in vals) if c is not None)
    if all(x is None or x is True or x is False for x in vals):
        lits = tuple(vals)
        return lambda v: any(v is x for x in lits)

    def check(v: Any) -> bool:
        c = _canon(v)
        return c is not None and c in canon
    return check


# ----------------------------------------------------------------------------------
# the "valid" closures
# ----------------------------------------------------------------------------------
def _build_closures(n: _Node) -> None:
    n.core = _checks(n, annotations=False)
    if n.needs_ann:
        ann = n.ann
        n.valid = lambda v: ann(v) is not None
    else:
        n.valid = _checks(n, annotations=True)
    n.done = True


def _fn(c: _Node) -> Callable[[Any], bool]:
    """``c.valid`` itself when it is final, else a call through the node (a ``$ref`` cycle:
    the node is still being compiled)."""
    if c.done:
        return c.valid
    return lambda v: c.valid(v)


def _checks(n: _Node, *, annotations: bool) -> Callable[[Any], bool]:
    """The validity closure of ``n``: every keyword (``annotations=True``) or every keyword
    except the annotation-producing applicators (``False``: ``core`` for :meth:`_Node.ann`)."""
    obj: list[Callable[[dict], bool]] = []
    arr: list[Callable[[list], bool]] = []
    st: list[Callable[[str], bool]] = []
    num: list[Callable[[Any], bool]] = []
    anyc: list[Callable[[Any], bool]] = []
    # -- objects
    if n.required:
        req = n.required
        if len(req) == 1:
            r0 = req[0]
            obj.append(lambda v: r0 in v)
        else:
            def required(v: dict) -> bool:
                for k in req:
                    if k not in v:
                        return False
                return True
            obj.append(required)
    if n.min_props is not None:
        mp = n.min_props
        obj.append(lambda v: len(v) >= mp)
    if n.max_props is not None:
        xp = n.max_props
        obj.append(lambda v: len(v) <= xp)
    if n.dep_req:
        dr = n.dep_req

        def dependent_required(v: dict) -> bool:
            for k, deps in dr:
                if k in v:
                    for d in deps:
                        if d not in v:
                            return False
            return True
        obj.append(dependent_required)
    if n.pnames is not None:
        pv = _fn(n.pnames)

        def property_names(v: dict) -> bool:
            for k in v:
                if not pv(k):
                    return False
            return True
        obj.append(property_names)
    if annotations:
        props = n.props
        pp = tuple((rx, _fn(pnode)) for rx, _src, pnode in n.pprops)
        addl = _fn(n.addl) if n.addl is not None else None
        if props and not pp and addl is None:
            pget = {k: _fn(c) for k, c in props.items()}.get

            def properties(v: dict) -> bool:
                for k, x in v.items():
                    f = pget(k)
                    if f is not None and not f(x):
                        return False
                return True
            obj.append(properties)
        elif props or pp or addl is not None:
            pget = {k: _fn(c) for k, c in props.items()}.get

            def members(v: dict) -> bool:
                for k, x in v.items():
                    f = pget(k)
                    matched = False
                    if f is not None:
                        if not f(x):
                            return False
                        matched = True
                    for rx, pf in pp:
                        if rx(k) is not None:
                            matched = True
                            if not pf(x):
                                return False
                    if not matched and addl is not None and not addl(x):
                        return False
                return True
            obj.append(members)
        if n.dep_schemas:
            ds = tuple((k, _fn(dn)) for k, dn in n.dep_schemas)

            def dependent_schemas(v: dict) -> bool:
                for k, df in ds:
                    if k in v and not df(v):
                        return False
                return True
            obj.append(dependent_schemas)
    # -- arrays
    if n.min_items is not None:
        mi = n.min_items
        arr.append(lambda v: len(v) >= mi)
    if n.max_items is not None:
        xi = n.max_items
        arr.append(lambda v: len(v) <= xi)
    if n.unique:
        arr.append(_unique)
    if annotations:
        pre = tuple(_fn(c) for c in n.prefix)
        if pre:
            npre = len(pre)

            def prefix_items(v: list) -> bool:
                for i in range(min(len(v), npre)):
                    if not pre[i](v[i]):
                        return False
                return True
            arr.append(prefix_items)
        if n.items is not None:
            iv = _fn(n.items)
            start = len(pre)
            if start:
                def items_after(v: list) -> bool:
                    for i in range(start, len(v)):
                        if not iv(v[i]):
                            return False
                    return True
                arr.append(items_after)
            else:
                def items(v: list) -> bool:
                    for x in v:
                        if not iv(x):
                            return False
                    return True
                arr.append(items)
        if n.contains is not None:
            cv = _fn(n.contains)
            lo = n.min_contains
            hi = n.max_contains
            if lo == 0 and hi is None:
                pass                              # always satisfied
            else:
                def contains(v: list) -> bool:
                    hits = 0
                    for x in v:
                        if cv(x):
                            hits += 1
                            if hi is None and hits >= lo:
                                return True
                            if hi is not None and hits > hi:
                                return False
                    return hits >= lo
                arr.append(contains)
    # -- strings
    if n.min_len is not None:
        ml = n.min_len
        st.append(lambda v: len(v) >= ml)
    if n.max_len is not None:
        xl = n.max_len
        st.append(lambda v: len(v) <= xl)
    if n.pattern is not None:
        search = n.pattern
        st.append(lambda v: search(v) is not None)
    if n.fmt is not None:
        st.append(n.fmt)
    # -- numbers
    for bound, test in ((n.minimum, lambda c: c >= 0), (n.maximum, lambda c: c <= 0),
                        (n.xminimum, lambda c: c > 0), (n.xmaximum, lambda c: c < 0)):
        if bound is not None:
            num.append(_bound_check(bound, test))
    if n.multiple is not None:
        mb = n.multiple
        num.append(lambda v: _multiple_of(v, mb))
    # -- any type
    if n.enum is not None:
        anyc.append(n.enum)
    if n.has_const and n.const is not None:
        anyc.append(n.const)
    if annotations:
        if n.ref is not None:
            anyc.append(_fn(n.ref))
        for c in n.all_of:
            anyc.append(_fn(c))
        if n.any_of:
            alts = tuple(_fn(c) for c in n.any_of)

            def any_of(v: Any) -> bool:
                for f in alts:
                    if f(v):
                        return True
                return False
            anyc.append(any_of)
        if n.one_of:
            alts1 = tuple(_fn(c) for c in n.one_of)

            def one_of(v: Any) -> bool:
                found = 0
                for f in alts1:
                    if f(v):
                        found += 1
                        if found > 1:
                            return False
                return found == 1
            anyc.append(one_of)
        if n.if_ is not None:
            cond = _fn(n.if_)
            then = _fn(n.then) if n.then is not None else None
            els = _fn(n.else_) if n.else_ is not None else None

            def if_then_else(v: Any) -> bool:
                if cond(v):
                    return then is None or then(v)
                return els is None or els(v)
            anyc.append(if_then_else)
    if n.not_ is not None:
        neg = _fn(n.not_)
        anyc.append(lambda v: not neg(v))
    return _combine(n, obj, arr, st, num, anyc)


def _bound_check(b: _Bound, test: Callable[[int], bool]) -> Callable[[Any], bool]:
    bx = b.x
    if type(bx) is int:
        ge = test(1) and test(0) and not test(-1)      # minimum
        le = test(-1) and test(0) and not test(1)      # maximum
        gt = test(1) and not test(0)                   # exclusiveMinimum
        if ge:
            def check(v: Any) -> bool:
                if type(v) is int:
                    return v >= bx
                return not _nan(v) and _cmp(v, b) >= 0
        elif le:
            def check(v: Any) -> bool:
                if type(v) is int:
                    return v <= bx
                return not _nan(v) and _cmp(v, b) <= 0
        elif gt:
            def check(v: Any) -> bool:
                if type(v) is int:
                    return v > bx
                return not _nan(v) and _cmp(v, b) > 0
        else:
            def check(v: Any) -> bool:
                if type(v) is int:
                    return v < bx
                return not _nan(v) and _cmp(v, b) < 0
        return check

    def check_any(v: Any) -> bool:
        return not _nan(v) and test(_cmp(v, b))
    return check_any


def _type_check(types: tuple[type, ...] | None, intf: bool) -> Callable[[Any], bool] | None:
    """The ``type`` keyword alone (``None``: no type constraint)."""
    if types is None:
        return None
    if not intf:
        if len(types) == 1:
            only = types[0]
            return lambda v: type(v) is only
        tset = frozenset(types)
        return lambda v: type(v) in tset
    tset2 = frozenset(types)

    def type_ok(v: Any) -> bool:
        tp = type(v)
        return tp in tset2 or (tp in _FLOATISH and _integral(v))
    return type_ok


def _all_of_checks(checks: tuple[Callable[[Any], bool], ...]) -> Callable[[Any], bool]:
    if len(checks) == 1:
        return checks[0]
    if len(checks) == 2:
        c0, c1 = checks
        return lambda v: c0(v) and c1(v)

    def every(v: Any) -> bool:
        for c in checks:
            if not c(v):
                return False
        return True
    return every


def _combine(n: _Node, obj: list, arr: list, st: list, num: list, anyc: list) -> Callable[[Any], bool]:
    """One closure for the type gate and the per-type checks (specialized for the common
    shapes: type only, a single instance type, any-type checks only)."""
    types = n.types
    type_ok = _type_check(types, n.intf)
    groups = [(dict, obj), (list, arr), (str, st)]
    used = [(tp, _all_of_checks(tuple(cs))) for tp, cs in groups if cs]
    numc = _all_of_checks(tuple(num)) if num else None
    anyf = _all_of_checks(tuple(anyc)) if anyc else None
    if not used and numc is None:
        if type_ok is None:
            return anyf if anyf is not None else _true
        if anyf is None:
            return type_ok
        return lambda v: type_ok(v) and anyf(v)
    if len(used) == 1 and numc is None and anyf is None:
        only_tp, fn = used[0]
        if type_ok is None:
            def valid_one(v: Any) -> bool:
                return type(v) is not only_tp or fn(v)
            return valid_one
        if types == (only_tp,):
            def valid_only(v: Any) -> bool:
                return type(v) is only_tp and fn(v)
            return valid_only

        def valid_typed(v: Any) -> bool:
            if type(v) is only_tp:
                return type_ok(v) and fn(v)
            return type_ok(v)
        return valid_typed
    objf = next((f for tp, f in used if tp is dict), None)
    arrf = next((f for tp, f in used if tp is list), None)
    strf = next((f for tp, f in used if tp is str), None)

    def valid(v: Any) -> bool:
        if type_ok is not None and not type_ok(v):
            return False
        tp = type(v)
        if tp is dict:
            if objf is not None and not objf(v):
                return False
        elif tp is list:
            if arrf is not None and not arrf(v):
                return False
        elif tp is str:
            if strf is not None and not strf(v):
                return False
        elif numc is not None and tp in _NUM_TYPES:
            if not numc(v):
                return False
        return anyf is None or anyf(v)
    return valid


# ----------------------------------------------------------------------------------
# public: compiled schemas
# ----------------------------------------------------------------------------------
class CompiledSchema:
    """A schema compiled by :func:`compile_schema` (immutable; safe to share between
    threads). ``value`` is the schema, ``formats`` whether ``format`` is asserted."""

    def __init__(self, value: Any, *, formats: bool = False, resources: Mapping[str, Any] | None = None) -> None:
        if not isinstance(value, (dict, bool)):
            raise _schema_error("jschema.error.schema.not_schema", location="/")
        self.value = value
        self.formats = bool(formats)
        self._resources = dict(resources or {})
        compiler = _Compiler(value, self.formats, self._resources)
        self._root = compiler.node(value, compiler.base_of.get(id(value), _ROOT_BASE), "")
        compiler.check_cycles()
        self._compiler = compiler
        self._variants: dict[bool, CompiledSchema] = {self.formats: self}
        self._lock = threading.Lock()

    def __repr__(self) -> str:
        return f"<CompiledSchema {len(self._compiler.nodes)} subschemas{' formats' if self.formats else ''}>"

    @property
    def root(self) -> _Node:
        return self._root

    @property
    def size(self) -> int:
        """Number of compiled (object) subschemas."""
        return len(self._compiler.nodes)

    def with_formats(self, formats: bool) -> CompiledSchema:
        """The same schema compiled with/without format assertion (cached)."""
        formats = bool(formats)
        with self._lock:
            hit = self._variants.get(formats)
            if hit is None:
                hit = CompiledSchema(self.value, formats=formats, resources=self._resources)
                hit._variants = self._variants
                hit._lock = self._lock
                self._variants[formats] = hit
            return hit

    def is_valid(self, instance: Any) -> bool:
        """``instance`` (codec-typed Python data) is valid."""
        return self._root.valid(instance)

    def errors(self, instance: Any, *, limit: int | None = None) -> list[Issue]:
        """The issues of ``instance`` (``record`` / ``key`` are ``None``); at most ``limit``."""
        if self._root.valid(instance):
            return []
        ctx = _ErrCtx(limit)
        self._root.errs(instance, (), ctx)
        if not ctx.out:   # pragma: no cover - the two evaluators agree (tested on the official suite)
            log.error("jschema: invalid instance without error details (schema %r)", self)
            ctx.add((), "false", "", {})
        return [Issue(None, None, path, kw, loc, params) for path, kw, loc, params in ctx.out]

    def iter_errors(self, instance: Any) -> Iterator[Issue]:
        yield from self.errors(instance)


def compile_schema(value: Any, *, formats: bool = False, resources: Mapping[str, Any] | None = None) -> CompiledSchema:
    """Compile a JSON Schema (a dict or a boolean; JSON text as ``str``/``bytes`` is parsed
    exactly first). ``formats``: assert the supported formats; ``resources``: other schema
    documents by URI (offline; e.g. files the user chose) that ``$ref`` may point to.
    Raises a localized ``UserInputError`` for invalid or unsupported schemas."""
    if isinstance(value, CompiledSchema):
        return value.with_formats(formats)
    if isinstance(value, (str, bytes, bytearray, memoryview)):
        try:
            value = codec.loads(value)
        except codec.JsonDecodeError as exc:
            raise UserInputError("jschema.error.schema.json", detail=str(exc)) from None
    return CompiledSchema(value, formats=formats, resources=resources)


# ----------------------------------------------------------------------------------
# issues and messages
# ----------------------------------------------------------------------------------
_TYPE_KEYS = {
    "object": "jschema.type.object", "array": "jschema.type.array", "string": "jschema.type.string",
    "number": "jschema.type.number", "integer": "jschema.type.integer", "boolean": "jschema.type.boolean",
    "null": "jschema.type.null",
}
_MESSAGE_KEYS = {
    "false": "jschema.error.false",
    "type": "jschema.error.type",
    "enum": "jschema.error.enum",
    "const": "jschema.error.const",
    "multipleOf": "jschema.error.multipleOf",
    "minimum": "jschema.error.minimum",
    "maximum": "jschema.error.maximum",
    "exclusiveMinimum": "jschema.error.exclusiveMinimum",
    "exclusiveMaximum": "jschema.error.exclusiveMaximum",
    "minLength": "jschema.error.minLength",
    "maxLength": "jschema.error.maxLength",
    "pattern": "jschema.error.pattern",
    "format": "jschema.error.format",
    "minItems": "jschema.error.minItems",
    "maxItems": "jschema.error.maxItems",
    "uniqueItems": "jschema.error.uniqueItems",
    "prefixItems": "jschema.error.prefixItems",
    "items": "jschema.error.items",
    "contains": "jschema.error.contains",
    "maxContains": "jschema.error.maxContains",
    "minProperties": "jschema.error.minProperties",
    "maxProperties": "jschema.error.maxProperties",
    "required": "jschema.error.required",
    "dependentRequired": "jschema.error.dependentRequired",
    "dependentSchemas": "jschema.error.dependentSchemas",
    "properties": "jschema.error.properties",
    "patternProperties": "jschema.error.patternProperties",
    "additionalProperties": "jschema.error.additionalProperties",
    "unevaluatedProperties": "jschema.error.unevaluatedProperties",
    "unevaluatedItems": "jschema.error.unevaluatedItems",
    "propertyNames": "jschema.error.propertyNames",
    "not": "jschema.error.not",
    "anyOf": "jschema.error.anyOf",
    "oneOf": "jschema.error.oneOf",
}
# document-level variants (keywords of the record container, duplicate records)
_DOC_KEYS = {
    "false": "jschema.error.document.false",
    "type": "jschema.error.document.type",
    "minItems": "jschema.error.document.minItems",
    "maxItems": "jschema.error.document.maxItems",
    "contains": "jschema.error.document.contains",
    "maxContains": "jschema.error.document.maxContains",
    "minProperties": "jschema.error.document.minProperties",
    "maxProperties": "jschema.error.document.maxProperties",
    "required": "jschema.error.document.required",
    "dependentRequired": "jschema.error.document.dependentRequired",
    "uniqueItems": "jschema.error.document.uniqueItems",
    "uniqueItems.first": "jschema.error.document.uniqueItems.first",
}


def _type_text(name: str) -> str:
    key = _TYPE_KEYS.get(name)
    return t(key) if key is not None else name


def _count_text(n: Any) -> str:
    if isinstance(n, int) and not isinstance(n, bool):
        text = f"{n:,}"
        return text.replace(",", " ") if get_language() == "uz" else text
    return str(n)


def _display_params(keyword: str, params: Mapping[str, Any]) -> dict[str, Any]:
    out = dict(params)
    if isinstance(out.get("expected"), list):
        out["expected"] = " | ".join(_type_text(x) for x in out["expected"])
    if isinstance(out.get("actual"), str):
        out["actual"] = _type_text(out["actual"])
    if isinstance(out.get("allowed"), list):
        shown = out["allowed"][:10]
        text = ", ".join(shown)
        if len(out["allowed"]) > len(shown):
            text += ", …"
        out["allowed"] = text
    for name in ("property", "required", "pattern"):
        if isinstance(out.get(name), str):
            out[name] = _show(out[name])
    for name in ("count", "limit", "length", "matched"):
        if name in out:
            out[name] = _count_text(out[name])
    for name in ("first", "second", "index"):
        if isinstance(out.get(name), int):
            out[name] = f"#{out[name]}"
    return out


@dataclass(frozen=True)
class Issue:
    """One validation problem.

    * ``record``: the record number (0-based position; ``None`` = the document itself: the
      wrapper, or a keyword of the record container such as ``minItems``);
    * ``key``: the member key of the record (keyed documents, wrapped object containers);
    * ``path``: the path inside the record value (``()`` = the record itself; for a document
      issue: inside the document) -- :attr:`path_text` formats it;
    * ``keyword``: the failing keyword (``type``, ``required``, ...; ``false`` for a ``false``
      schema);
    * ``schema_path``: JSON Pointer of the keyword in the schema (``/properties/age/minimum``;
      for a schema of ``resources``: ``<uri>#<pointer>``);
    * ``params``: the message parameters (JSON-compatible); :attr:`message` is the localized
      text (``jschema.error.<keyword>``; ``message_key`` overrides it for document-level
      variants).
    """

    record: int | None
    key: str | None
    path: tuple
    keyword: str
    schema_path: str
    params: dict[str, Any] = field(default_factory=dict)
    message_key: str | None = None

    @property
    def message(self) -> str:
        key = self.message_key or _MESSAGE_KEYS.get(self.keyword, "jschema.error.generic")
        return t(key, **_display_params(self.keyword, self.params))

    @property
    def path_text(self) -> str:
        """The path in dotted syntax (``$`` for the record / document itself)."""
        return format_path(self.path) if self.path else "$"

    def to_dict(self) -> dict[str, Any]:
        """JSON-compatible form (export): record, key, path, keyword, schema_path, message, params."""
        return {
            "record": self.record,
            "key": self.key,
            "path": self.path_text,
            "keyword": self.keyword,
            "schema_path": self.schema_path,
            "message": self.message,
            "params": dict(self.params),
        }


def _normalized_path(path: tuple) -> str:
    """Path text with every array index as ``[*]`` (top-path statistics)."""
    if not path:
        return "$"
    return format_path(tuple(Wild.ANY_INDEX if isinstance(p, int) else p for p in path))


@dataclass
class ValidationReport:
    """Result of :func:`validate`.

    ``scope``: ``"record"`` or ``"document"`` (what was used); ``scanned``: records validated;
    ``invalid``: records with at least one issue; ``issue_count``: issues collected (at most
    ``max_per_record`` per record) incl. ``document_issues`` (issues with ``record=None``);
    ``issues``: the first ``max_issues`` of them (``truncated`` when more were found);
    ``counts``: issues per keyword, most frequent first; ``top_paths``: the most frequent paths
    (indices as ``[*]``) with their counts; ``records``: the invalid records (RecordSet, scope
    order, tagged with ``gen``); ``skipped``: records not validated (``too_large`` +
    ``unreadable`` + ``too_deep``; each with a warning; :attr:`ok` stays True for them,
    :attr:`complete` is False); ``limited_records``: records whose issues reached
    ``max_per_record``; ``warnings`` / ``warning_params`` (i18n keys; :meth:`warning_texts`).
    :meth:`close` releases the RecordSet.
    """

    scope: str
    scanned: int = 0
    invalid: int = 0
    issue_count: int = 0
    document_issues: int = 0
    issues: list[Issue] = field(default_factory=list)
    counts: dict[str, int] = field(default_factory=dict)
    top_paths: list[tuple[str, int]] = field(default_factory=list)
    records: RecordSet | None = None
    skipped: int = 0
    too_large: int = 0
    unreadable: int = 0
    too_deep: int = 0
    truncated: bool = False
    limited_records: int = 0
    gen: int | None = None
    formats: bool = False
    elapsed: float = 0.0
    warnings: tuple[str, ...] = ()
    warning_params: tuple[dict[str, Any], ...] = ()

    @property
    def ok(self) -> bool:
        """No issue was found (records that were not validated -- ``skipped`` -- are no issue:
        see :attr:`complete`)."""
        return self.issue_count == 0

    @property
    def complete(self) -> bool:
        """Every record of the scope was validated: nothing was ``skipped`` (too large,
        unreadable, too deep). ``ok and not complete``: no issue among the records that could be
        checked -- callers (the CLI's exit code) decide what that means."""
        return self.skipped == 0

    def warning_texts(self) -> list[str]:
        """The warnings in the current UI language (``<key>.one`` for a count of 1)."""
        out = []
        for i, key in enumerate(self.warnings):
            params = dict(self.warning_params[i]) if i < len(self.warning_params) else {}
            n = params.get("count")
            if isinstance(n, int) and not isinstance(n, bool) and n == 1 and has(key + ".one"):
                key += ".one"
            for name in ("count", "shown", "limit"):
                if isinstance(params.get(name), int) and not isinstance(params.get(name), bool):
                    params[name] = _count_text(params[name])
            if isinstance(params.get("record"), int):
                params["record"] = f"#{params['record']}"
            out.append(t(key, **params))
        return out

    def close(self) -> None:
        """Release the RecordSet of invalid records."""
        if self.records is not None:
            self.records.close()


# ----------------------------------------------------------------------------------
# validation: helpers
# ----------------------------------------------------------------------------------
def _unique_source(src: Any) -> bool:
    """A scope that never names a position twice (no seen-bitmap needed)."""
    if src is None:
        return True
    if isinstance(src, RecordSet):
        return bool(src.is_ascending())
    if isinstance(src, range):
        return src.step > 0
    return False


_BIG_ENDIAN = array("Q", [1]).tobytes()[0] == 0
_Raw = tuple  # (path, keyword, schema location, params)


def _u64(data: bytes) -> array:
    a = array("Q")
    a.frombytes(data)
    if _BIG_ENDIAN:  # pragma: no cover - little-endian hosts only
        a.byteswap()
    return a


def _expand(nodes: Iterable[_Node]) -> list[_Node]:
    """``nodes`` and every subschema they apply unconditionally to the same instance
    (``$ref``, ``allOf``), each once, in a stable order."""
    seen: set[int] = set()
    out: list[_Node] = []
    stack = list(reversed(list(nodes)))
    while stack:
        n = stack.pop()
        if id(n) in seen:
            continue
        seen.add(id(n))
        out.append(n)
        if n.boolean is not None:
            continue
        subs = ([n.ref] if n.ref is not None else []) + list(n.all_of)
        stack.extend(reversed(subs))
    return out


def _declared_types(nodes: list[_Node]) -> set[str] | None:
    """Intersection of the ``type`` sets the nodes declare (``None``: no declaration)."""
    out: set[str] | None = None
    for n in nodes:
        if n.boolean is None and n.types is not None:
            names = set(n.type_names)
            if "integer" in names:
                names.add("number")
            out = names if out is None else (out & names)
    return out


_ARRAY_KEYWORDS = ("prefixItems", "items", "contains", "minItems", "maxItems", "uniqueItems", "unevaluatedItems",
                   "minContains", "maxContains")
_OBJECT_KEYWORDS = ("properties", "patternProperties", "additionalProperties", "propertyNames", "required",
                    "dependentRequired", "dependentSchemas", "minProperties", "maxProperties",
                    "unevaluatedProperties")


def _has_any(nodes: list[_Node], keywords: Sequence[str]) -> bool:
    return any(n.boolean is None and any(k in n.value for k in keywords) for n in nodes)


def _describes_array(root: _Node) -> bool:
    """The root describes a sequence of records (an array-only schema)."""
    nodes = _expand([root])
    types = _declared_types(nodes)
    if types is not None:
        return "array" in types and "object" not in types
    return _has_any(nodes, _ARRAY_KEYWORDS) and not _has_any(nodes, _OBJECT_KEYWORDS)


def _describes_object_container(root: _Node) -> bool:
    """The root describes a keyed document (a map of records, not one record)."""
    nodes = _expand([root])
    types = _declared_types(nodes)
    if types is not None and "object" not in types:
        return False
    if _has_any(nodes, ("properties", "required")):
        return False
    return _has_any(nodes, ("additionalProperties", "patternProperties", "propertyNames", "minProperties",
                            "maxProperties"))


def _leads_to(root: _Node, path: tuple) -> bool:
    """The root's ``properties`` / ``prefixItems`` / ``items`` lead along ``path`` (a wrapper
    schema reaching the record container)."""
    nodes = _expand([root])
    for seg in path:
        nxt: list[_Node] = []
        for n in nodes:
            if n.boolean is not None:
                continue
            if isinstance(seg, str):
                c = n.props.get(seg)
                if c is not None:
                    nxt.append(c)
            elif seg < len(n.prefix):
                nxt.append(n.prefix[seg])
            elif n.items is not None:
                nxt.append(n.items)
        if not nxt:
            return False
        nodes = _expand(nxt)
    return bool(path)


class _Entry:
    """A subschema applied to a record in document scope (``kw``: the keyword that applied
    it, used for the message when the subschema is ``false``)."""

    __slots__ = ("node", "kw", "loc", "params")

    def __init__(self, node: _Node, kw: str, loc: str, params: dict[str, Any] | None = None) -> None:
        self.node = node
        self.kw = kw
        self.loc = loc
        self.params = params or {}


class _DocPlan:
    """Document scope: the subschemas of the record container, decomposed per record."""

    def __init__(self, hnodes: list[_Node], container: str) -> None:
        self.container = container            # "array" | "object"
        order = _expand(hnodes)
        self.false_nodes = [n for n in order if n.boolean is False]
        self.nodes = [n for n in order if n.boolean is None]
        self.type_fail: list[_Node] = []
        py = list if container == "array" else dict
        for n in self.nodes:
            for kw, present in (("enum", n.enum is not None), ("const", n.has_const), ("anyOf", bool(n.any_of)),
                                ("oneOf", bool(n.one_of)), ("not", n.not_ is not None), ("if", n.if_ is not None)):
                if present:
                    raise _NotDecomposable(kw)
            if container == "object" and n.dep_schemas:
                raise _NotDecomposable("dependentSchemas")
            if n.types is not None and py not in n.types:
                self.type_fail.append(n)
        self.subtree: dict[int, list[_Node]] = {}
        for n in self.nodes:
            if n.uitems is not None or n.uprops is not None:
                self.subtree[id(n)] = [d for d in _expand([n]) if d.boolean is None]
        if container == "array":
            self._init_array()
        else:
            self._init_object()

    # -- arrays ---------------------------------------------------------------------------------
    def _init_array(self) -> None:
        nodes = self.nodes
        self.max_prefix = max((len(n.prefix) for n in nodes), default=0)
        self.contains = [(n, n.contains) for n in nodes if n.contains is not None]
        self.counts = [0] * len(self.contains)
        self.unique = [n for n in nodes if n.unique]
        self.aggregates = bool(self.contains or self.unique or any(
            n.min_items is not None or n.max_items is not None for n in nodes))
        self.distinct_needed = False
        self._tail = self._array_entries(self.max_prefix)
        self.uneval: list[tuple[_Node, list[int], bool, int]] = []
        for n in nodes:
            if n.uitems is not None:
                sub = self.subtree[id(n)]
                own = [k for k, (owner, _c) in enumerate(self.contains) if owner in sub]
                always = any(d.items is not None or (d.uitems is not None and d is not n) for d in sub)
                upto = max((len(d.prefix) for d in sub), default=0)
                self.uneval.append((n, own, always, upto))

    def _array_entries(self, i: int) -> list[_Entry]:
        out = []
        for n in self.nodes:
            if i < len(n.prefix):
                out.append(_Entry(n.prefix[i], "prefixItems", f"{n.loc}/prefixItems/{i}", {"index": i}))
            elif n.items is not None:
                out.append(_Entry(n.items, "items", f"{n.loc}/items", {"index": i, "limit": len(n.prefix)}))
        return out

    def entries_at(self, i: int, value: Any) -> list[_Entry]:
        """The subschemas that apply to record ``i`` (updates the ``contains`` counts)."""
        out = self._tail if i >= self.max_prefix else self._array_entries(i)
        matched: list[bool] | None = None
        if self.contains:
            matched = []
            for k, (_owner, cn) in enumerate(self.contains):
                hit = cn.valid(value)
                matched.append(hit)
                if hit:
                    self.counts[k] += 1
        if self.uneval:
            extra = None
            for n, own, always, upto in self.uneval:
                if always or i < upto:
                    continue
                if matched is not None and any(matched[k] for k in own):
                    continue
                if extra is None:
                    extra = list(out)
                extra.append(_Entry(n.uitems, "unevaluatedItems", f"{n.loc}/unevaluatedItems",  # type: ignore[arg-type]
                                    {"index": i}))
            if extra is not None:
                out = extra
        return out

    def array_aggregates(self, count: int) -> list[_Raw]:
        out: list[_Raw] = []
        for n in self.nodes:
            if n.min_items is not None and count < n.min_items:
                out.append(((), "minItems", f"{n.loc}/minItems", {"count": count, "limit": n.min_items}))
            if n.max_items is not None and count > n.max_items:
                out.append(((), "maxItems", f"{n.loc}/maxItems", {"count": count, "limit": n.max_items}))
        for k, (n, _cn) in enumerate(self.contains):
            hits = self.counts[k]
            if hits < n.min_contains:
                where = "/minContains" if "minContains" in n.value else "/contains"
                out.append(((), "contains", n.loc + where, {"count": hits, "limit": n.min_contains}))
            if n.max_contains is not None and hits > n.max_contains:
                out.append(((), "maxContains", f"{n.loc}/maxContains", {"count": hits, "limit": n.max_contains}))
        return out

    # -- objects --------------------------------------------------------------------------------
    def _init_object(self) -> None:
        nodes = self.nodes
        self.pnames = [n for n in nodes if n.pnames is not None]
        names: set[str] = set()
        for n in nodes:
            names.update(n.required)
            for k, deps in n.dep_req:
                names.add(k)
                names.update(deps)
        self.tracked = names
        self.seen: set[str] = set()
        self.unique = []
        self.distinct_needed = any(n.min_props is not None or n.max_props is not None for n in nodes)
        self.aggregates = bool(names or self.distinct_needed)
        self.key_cache: dict[str, list[_Entry]] = {}

    @staticmethod
    def _evaluated_key(sub: list[_Node], owner: _Node, key: str) -> bool:
        for d in sub:
            if key in d.props or d.addl is not None or (d.uprops is not None and d is not owner):
                return True
            if any(rx(key) is not None for rx, _s, _n in d.pprops):
                return True
        return False

    def entries_for(self, key: str) -> list[_Entry]:
        """The subschemas that apply to the record with member key ``key``."""
        hit = self.key_cache.get(key)
        if hit is not None:
            return hit
        out = []
        for n in self.nodes:
            matched = False
            c = n.props.get(key)
            if c is not None:
                out.append(_Entry(c, "properties", f"{n.loc}/properties/{_ptr(key)}", {"property": key}))
                matched = True
            for rx, src, pn in n.pprops:
                if rx(key) is not None:
                    out.append(_Entry(pn, "patternProperties", f"{n.loc}/patternProperties/{_ptr(src)}",
                                      {"property": key, "pattern": src}))
                    matched = True
            if not matched and n.addl is not None:
                out.append(_Entry(n.addl, "additionalProperties", f"{n.loc}/additionalProperties", {"property": key}))
            if n.uprops is not None and not self._evaluated_key(self.subtree[id(n)], n, key):
                out.append(_Entry(n.uprops, "unevaluatedProperties", f"{n.loc}/unevaluatedProperties",
                                  {"property": key}))
        if len(self.key_cache) < KEY_CACHE:
            self.key_cache[key] = out
        return out

    def key_errors(self, key: str) -> list[_Raw]:
        """Record the key (``required`` tracking) and check it against ``propertyNames``."""
        if key in self.tracked:
            self.seen.add(key)
        out: list[_Raw] = []
        for n in self.pnames:
            if not n.pnames.valid(key):   # type: ignore[union-attr]
                out.append(((), "propertyNames", f"{n.loc}/propertyNames", {"property": key}))
        return out

    def object_aggregates(self, distinct: int | None) -> list[_Raw]:
        out: list[_Raw] = []
        for n in self.nodes:
            for k in n.required:
                if k not in self.seen:
                    out.append(((), "required", f"{n.loc}/required", {"property": k}))
            for k, deps in n.dep_req:
                if k in self.seen:
                    for d in deps:
                        if d not in self.seen:
                            out.append(((), "dependentRequired", f"{n.loc}/dependentRequired",
                                        {"property": k, "required": d}))
            if distinct is not None:
                if n.min_props is not None and distinct < n.min_props:
                    out.append(((), "minProperties", f"{n.loc}/minProperties",
                                {"count": distinct, "limit": n.min_props}))
                if n.max_props is not None and distinct > n.max_props:
                    out.append(((), "maxProperties", f"{n.loc}/maxProperties",
                                {"count": distinct, "limit": n.max_props}))
        return out

    def container_errors(self) -> list[_Raw]:
        out: list[_Raw] = [((), "false", n.loc, {}) for n in self.false_nodes]
        for n in self.type_fail:
            out.append(((), "type", f"{n.loc}/type", {"expected": list(n.type_names), "actual": self.container}))
        return out


def _check_source_gen(source: Any, gen: Any) -> None:
    src_gen = getattr(source, "gen", None) if source is not None else None
    if src_gen is not None and src_gen != gen:
        raise EditError("edits.error.stale_scope")


def _data_bytes(doc: Any) -> int:
    if getattr(doc, "edited", False):
        return max(1, int(doc.effective_bytes()))
    return max(1, int(doc.data_size) - int(doc.info.data_start))


def _check_limit(name: str, value: Any, *, zero: bool = False) -> int | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int) or value < (0 if zero else 1):
        raise UserInputError("jschema.error.limit", name=name, value=repr(value))
    return value


class _Validator:
    """One run of :func:`validate` / :func:`iter_issues` (module docstring)."""

    def __init__(self, doc: Any, compiled: Any, source: Any, scope: str, max_per_record: int | None, formats: bool,
                 progress: Progress | None, cancel: CancelToken | None) -> None:
        self.started = time.perf_counter()
        if scope not in ("auto", "record", "document"):
            raise UserInputError("jschema.error.scope", value=repr(scope))
        self.max_per_record = _check_limit("max_per_record", max_per_record)
        self.doc = doc.pinned()
        self.gen = self.doc.gen
        _check_source_gen(source, self.gen)
        if isinstance(source, RecordSet) and source._is_base_of(self.doc):
            source = None
        self.source = source
        # a source naming a position twice: each position is validated once (review R5A-8)
        self.unique = _unique_source(source)
        self.seen: bytearray | None = None
        self.compiled = compile_schema(compiled, formats=formats)
        self.formats = bool(formats)
        self.root = self.compiled.root
        self.requested = scope
        self.progress = progress
        self.cancel = cancel
        self.kind = self.doc.kind
        self.member = False
        self.scope = "record"
        self.scanned = 0
        self.too_large = 0
        self.unreadable = 0
        self.too_deep = 0
        self.first: dict[str, int] = {}
        self.limited = 0
        self.warnings: list[str] = []
        self.warning_params: list[dict[str, Any]] = []
        self.invalid_hook: Callable[[int], None] | None = None
        self.tick_hook: Callable[[], None] | None = None   # called every CHECK_RECORDS records (worker thread)

    # -- bookkeeping ----------------------------------------------------------------------------
    def _seen_bits(self) -> bytearray | None:
        """The bitmap of the positions examined (sources that may repeat a position), sized once
        the index is complete."""
        if self.unique:
            return None
        if self.seen is None:
            self.seen = bytearray((int(self.doc.record_count) + 7) >> 3)
        return self.seen

    @staticmethod
    def _repeated(seen: bytearray, pos: int) -> bool:
        b = pos >> 3
        bit = 1 << (pos & 7)
        if seen[b] & bit:
            return True
        seen[b] |= bit
        return False

    def _skip(self, what: str, pos: int, exc: BaseException | None = None) -> None:
        n = getattr(self, what) + 1
        setattr(self, what, n)
        self.first.setdefault(what, int(pos))
        if exc is not None:
            (log.warning if n <= 10 else log.debug)("jschema: record #%d not validated (%s): %s", pos, what, exc)

    def warn(self, key: str, **params: Any) -> None:
        self.warnings.append(key)
        self.warning_params.append(params)

    def _sub_progress(self, start: float, end: float, total: int | None = None) -> Progress | None:
        if self.progress is None:
            return None
        return self.progress.child(start, end, total=total)

    def _scan_progress(self, start: float) -> Progress | None:
        """Progress of the records pass (bytes of the records of ``source``)."""
        if self.progress is None:
            return None
        full = _data_bytes(self.doc)
        total: int | None = full
        if self.source is not None:
            try:
                n = len(self.source)
            except TypeError:
                total = None
            else:
                count = max(1, int(self.doc.record_count))
                total = max(1, int(full * min(1.0, n / count)))
        return self.progress.child(start, 1.0, total=total)

    # -- the run --------------------------------------------------------------------------------
    def run(self) -> Iterator[Issue]:
        doc = self.doc
        name = doc.source_path.name
        progress = self.progress
        if progress is not None:
            if not progress.message:
                progress.set_message(t("jschema.progress.validate", name=name))
            if progress.total is None:
                progress.set_total(_data_bytes(doc))
        share = 0.0
        if not doc.index_complete:
            share = 0.3
            doc.build_index(progress=self._sub_progress(0.0, share), cancel=self.cancel)
        check(self.cancel)
        if doc.partial:
            self.warn("jschema.warn.partial")
        if int(getattr(doc, "broken_count", 0) or 0):
            self.warn("jschema.warn.broken_lines", count=int(doc.broken_count))
        self.member = bool(doc.member_records)
        if self._mode() == "record":
            self.scope = "document" if self.requested == "document" else "record"
            yield from self._record_pass(self._scan_progress(share), [_Entry(self.root, "", self.root.loc)])
        else:
            self.scope = "document"
            yield from self._document(share)
        check(self.cancel)
        doc.ensure_unchanged()      # results of a file another program changed are not reported
        self._final_warnings()
        if progress is not None:
            progress.finish()

    def _mode(self) -> str:
        kind = self.kind
        root = self.root
        if kind == "single" or self.requested == "record":
            return "record"
        if self.requested == "auto":
            marker = root.value.get("$comment") if isinstance(root.value, dict) else None
            if marker == SCOPE_RECORD:
                return "record"
            if marker != SCOPE_DOCUMENT:
                if kind in ("array", "brace_list", "stream", "jsonl"):
                    return "document" if _describes_array(root) else "record"
                if kind == "keyed":
                    return "document" if _describes_object_container(root) else "record"
                if kind == "wrapped":
                    return "document" if _leads_to(root, tuple(self.doc.record_path or ())) else "record"
                return "record"
        if kind == "jsonl" and not _describes_array(root):
            return "record"           # each line is a document
        return "document"

    # -- records --------------------------------------------------------------------------------
    def _records(self, src: Any, progress: Progress | None) -> Iterator[tuple[int, str | None, Any]]:
        from .reader import split_member

        doc = self.doc
        member = self.member
        limit = VALIDATE_PARSE_BYTES
        loads = codec.loads
        cancel = self.cancel
        seen = self._seen_bits() if src is not None else None
        repeated = self._repeated
        blocks = doc.iter_blocks(src, progress=progress, cancel=cancel)
        try:
            for blk in blocks:
                check(cancel)
                idx = blk.indices
                if blk.huge:
                    if seen is None or not repeated(seen, int(idx[0])):
                        self._skip("too_large", int(idx[0]))
                    continue
                sp = _u64(blk.spans)
                data = blk.data
                for k in range(len(idx)):
                    if not k % CHECK_RECORDS:
                        check(cancel)
                        if self.tick_hook is not None:
                            self.tick_hook()
                    s = sp[2 * k]
                    e = sp[2 * k + 1]
                    pos = int(idx[k])
                    if seen is not None and repeated(seen, pos):
                        continue
                    if e - s > limit:
                        self._skip("too_large", pos)
                        continue
                    raw = data[s:e]
                    key = None
                    try:
                        if member:
                            key, vo = split_member(raw)
                            raw = raw[vo:]
                        value = loads(raw)
                    except (codec.JsonDecodeError, ValueError, RecursionError) as exc:
                        self._skip("unreadable", pos, exc)
                        continue
                    yield pos, key, value
        finally:
            blocks.close()

    def _record_pass(self, progress: Progress | None, entries: list[_Entry], plan: _DocPlan | None = None,
                     dups: bytearray | None = None, dup_first: dict[int, int] | None = None,
                     dup_loc: str = "") -> Iterator[Issue]:
        if plan is None and len(entries) == 1 and dups is None:
            yield from self._fast_pass(progress, entries)
            return
        array_plan = plan is not None and plan.container == "array"
        object_plan = plan is not None and plan.container == "object"
        limit = self.max_per_record
        for pos, key, value in self._records(self.source, progress):
            extra: list[_Raw] = []
            try:
                if array_plan:
                    ents = plan.entries_at(pos, value)  # type: ignore[union-attr]
                elif object_plan:
                    ents = plan.entries_for(key if key is not None else "")  # type: ignore[union-attr]
                    extra = plan.key_errors(key if key is not None else "")  # type: ignore[union-attr]
                else:
                    ents = entries
                if dups is not None and dups[pos >> 3] >> (pos & 7) & 1:
                    first = dup_first.get(pos) if dup_first else None
                    extra = extra + [((), "uniqueItems", dup_loc, {"first": first} if first is not None else {})]
                if not extra and all(en.node.valid(value) for en in ents):
                    self.scanned += 1
                    continue
                errors = self._errors(ents, value, limit, extra)
            except RecursionError as exc:
                self._skip("too_deep", pos, exc)
                continue
            self.scanned += 1
            yield from self._emit(pos, key, errors, dup_loc if dups is not None else None)

    def _fast_pass(self, progress: Progress | None, entries: list[_Entry]) -> Iterator[Issue]:
        """Record scope: every record against one schema (the hot loop, inlined)."""
        from .reader import split_member

        valid = entries[0].node.valid
        loads = codec.loads
        member = self.member
        limit = VALIDATE_PARSE_BYTES
        cancel = self.cancel
        per_record = self.max_per_record
        decode_errors = (codec.JsonDecodeError, ValueError, RecursionError)
        seen = self._seen_bits() if self.source is not None else None
        repeated = self._repeated
        blocks = self.doc.iter_blocks(self.source, progress=progress, cancel=cancel)
        try:
            for blk in blocks:
                check(cancel)
                idx = blk.indices
                if blk.huge:
                    if seen is None or not repeated(seen, int(idx[0])):
                        self._skip("too_large", int(idx[0]))
                    continue
                sp = _u64(blk.spans)
                data = blk.data
                ok = 0
                key = None
                for k in range(len(idx)):
                    if not k % CHECK_RECORDS:
                        check(cancel)
                        if self.tick_hook is not None:
                            self.tick_hook()
                    s = sp[2 * k]
                    e = sp[2 * k + 1]
                    if seen is not None and repeated(seen, int(idx[k])):
                        continue
                    if e - s > limit:
                        self._skip("too_large", int(idx[k]))
                        continue
                    try:
                        if member:
                            raw = data[s:e]
                            key, vo = split_member(raw)
                            value = loads(raw[vo:])
                        else:
                            value = loads(data[s:e])
                    except decode_errors as exc:
                        self._skip("unreadable", int(idx[k]), exc)
                        continue
                    try:
                        if valid(value):
                            ok += 1
                            continue
                        errors = self._errors(entries, value, per_record)
                    except RecursionError as exc:
                        self._skip("too_deep", int(idx[k]), exc)
                        continue
                    self.scanned += 1
                    yield from self._emit(int(idx[k]), key, errors, None)
                self.scanned += ok
        finally:
            blocks.close()

    def _emit(self, pos: int, key: str | None, errors: list[_Raw], dup_loc: str | None) -> Iterator[Issue]:
        if not errors:   # pragma: no cover - the two evaluators agree (tested on the official suite)
            log.error("jschema: record #%d is invalid but has no error details", pos)
            errors = [((), "false", self.root.loc, {})]
        if self.max_per_record is not None and len(errors) >= self.max_per_record:
            self.limited += 1
        if self.invalid_hook is not None:
            self.invalid_hook(pos)
        for path, kw, loc, params in errors:
            mk = None
            if kw == "uniqueItems" and dup_loc is not None and loc == dup_loc and not path:
                mk = _DOC_KEYS["uniqueItems.first" if "first" in params else "uniqueItems"]
            yield Issue(pos, key, path, kw, loc, params, mk)

    @staticmethod
    def _errors(entries: list[_Entry], value: Any, limit: int | None, extra: list[_Raw] | None = None) -> list[_Raw]:
        ctx = _ErrCtx(limit)
        for item in extra or ():
            ctx.add(*item)
        for en in entries:
            if ctx.full():
                break
            nd = en.node
            if nd.valid(value):
                continue
            if nd.boolean is False and en.kw:
                ctx.add((), en.kw, en.loc, dict(en.params))
            else:
                nd.errs(value, (), ctx)
        return ctx.out

    # -- document scope -------------------------------------------------------------------------
    def _document(self, share: float) -> Iterator[Issue]:
        doc = self.doc
        container_path: tuple = ()
        wrapper_issues: list[Issue] = []
        container = "object" if self.member else "array"
        try:
            if self.kind == "wrapped":
                container_path = tuple(doc.record_path or ())
                raw, hnodes = self._wrapper(container_path)
                wrapper_issues = [Issue(None, None, p, kw, loc, prm) for p, kw, loc, prm in raw]
                plan = _DocPlan(hnodes, container)
            else:
                plan = _DocPlan([self.root], container)
        except _NotDecomposable as exc:
            yield from self._whole(share, exc.keyword)
            return
        yield from wrapper_issues
        for p, kw, loc, prm in plan.container_errors():
            yield Issue(None, None, container_path + p, kw, loc, prm, _DOC_KEYS.get(kw))
        complete = self.source is None
        if plan.aggregates and not complete:
            self.warn("jschema.warn.aggregates_skipped")
        dups = None
        dup_first: dict[int, int] = {}
        dup_loc = ""
        distinct = None
        main = share
        if complete and plan.unique:
            main = (share + 1.0) / 2
            dup_loc = f"{plan.unique[0].loc}/uniqueItems"
            dups, dup_first = self._duplicates(share, main)
        elif complete and plan.distinct_needed:
            main = (share + 1.0) / 2
            distinct = self._distinct_keys(share, main)
        yield from self._record_pass(self._scan_progress(main), [], plan, dups, dup_first, dup_loc)
        if complete:
            check(self.cancel)
            if plan.container == "array":
                aggregates = plan.array_aggregates(int(doc.record_count))
            else:
                aggregates = plan.object_aggregates(distinct)
            for p, kw, loc, prm in aggregates:
                yield Issue(None, None, container_path + p, kw, loc, prm, _DOC_KEYS.get(kw))

    def _wrapper_value(self) -> Any:
        """The wrapper of a wrapped document with the record container replaced by ``[]`` / ``{}``."""
        try:
            parts = self.doc.wrapper_parts(cancel=self.cancel)
        except TooLargeError:
            raise UserInputError("jschema.error.wrapper_too_large", limit=format_bytes(VALIDATE_PARSE_BYTES)) from None
        if parts is None:
            raise UserInputError("jschema.error.wrapper_missing")
        prefix, suffix = parts
        if len(prefix) + len(suffix) > VALIDATE_PARSE_BYTES:
            raise UserInputError("jschema.error.wrapper_too_large", limit=format_bytes(VALIDATE_PARSE_BYTES))
        return codec.loads(prefix + (b"{}" if self.member else b"[]") + suffix)

    def _wrapper(self, path: tuple) -> tuple[list[_Raw], list[_Node]]:
        value = self._wrapper_value()
        hole = _Hole(path)
        ctx = _ErrCtx(10_000, hole)
        self.root.errs(value, (), ctx)
        return ctx.out, hole.nodes

    def _duplicates(self, start: float, end: float) -> tuple[bytearray, dict[int, int]]:
        from .dupes import find_duplicates

        doc = self.doc
        child = self._sub_progress(start, end)
        if child is not None:
            child.set_message(t("jschema.progress.duplicates", name=doc.source_path.name))
        rep = find_duplicates(doc, "$", None, keep="first", progress=child, cancel=self.cancel)
        try:
            bits = bytearray((int(doc.record_count) + 7) >> 3)
            for chunk in rep.records.iter_chunks():
                for pos in chunk:
                    bits[pos >> 3] |= 1 << (pos & 7)
            first: dict[int, int] = {}
            for _text, positions in rep.examples:
                for p in positions[1:]:
                    first[p] = positions[0]
            if rep.skipped:
                self.warn("jschema.warn.duplicates_skipped", count=int(rep.skipped))
        finally:
            rep.close()
        if self.progress is not None:
            self.progress.set_message(t("jschema.progress.validate", name=doc.source_path.name))
        return bits, first

    def _distinct_keys(self, start: float, end: float) -> int:
        from .dupes import find_duplicates

        rep = find_duplicates(self.doc, "$key", None, keep="first", progress=self._sub_progress(start, end),
                              cancel=self.cancel)
        try:
            return int(rep.records_with_key) - int(rep.duplicates)
        finally:
            rep.close()

    def _whole(self, share: float, keyword: str) -> Iterator[Issue]:
        """A document schema that cannot be decomposed: the document validated as one value."""
        doc = self.doc
        size = _data_bytes(doc)
        if size > DOCUMENT_PARSE_BYTES:
            raise UserInputError("jschema.error.document_too_large", keyword=keyword, size=format_bytes(size),
                                 limit=format_bytes(DOCUMENT_PARSE_BYTES))
        self.warn("jschema.warn.whole_document", keyword=keyword)
        member = self.member
        container: Any = {} if member else []
        positions: dict[str, int] = {}
        source, self.source = self.source, None
        try:
            for pos, key, value in self._records(None, self._scan_progress(share)):
                self.scanned += 1
                if member:
                    container[key] = value
                    positions[key] = pos       # type: ignore[index]
                else:
                    container.append(value)
        finally:
            self.source = source
        if self.too_large or self.unreadable:
            raise UserInputError("jschema.error.document_unreadable", count=self.too_large + self.unreadable)
        cpath: tuple = ()
        if self.kind == "wrapped":
            cpath = tuple(doc.record_path or ())
            whole = self._wrapper_value()
            holder = whole
            for seg in cpath[:-1]:
                holder = holder[seg]
            holder[cpath[-1]] = container
        else:
            whole = container
        limit = self.max_per_record
        ctx = _WholeCtx(cpath, limit)
        try:
            if not self.root.valid(whole):
                self.root.errs(whole, (), ctx)
        except RecursionError:
            raise UserInputError("jschema.error.document_too_deep") from None
        if ctx.dropped:
            self.warn("jschema.warn.whole_dropped", count=ctx.dropped, kept=WHOLE_DOCUMENT_ERRORS)
        per_record: dict[int, list[Issue]] = {}
        n = len(cpath)
        for path, kw, loc, params in ctx.out:
            if len(path) > n and path[:n] == cpath:
                seg = path[n]
                pos = positions.get(seg) if member else seg
                if isinstance(pos, int):
                    per_record.setdefault(pos, []).append(
                        Issue(pos, seg if member else None, path[n + 1:], kw, loc, params))
                    continue
            yield Issue(None, None, path, kw, loc, params, _DOC_KEYS.get(kw) if path == cpath else None)
        hook = self.invalid_hook
        invalid: dict[int, int] = {}          # every record with an error (also when its errors were dropped)
        for seg, count in ctx.seen.items():
            pos = positions.get(seg) if member else seg
            if isinstance(pos, int):
                invalid[pos] = count
        for pos in sorted(invalid):
            if limit is not None and invalid[pos] >= limit:
                self.limited += 1
            if hook is not None:
                hook(pos)
            yield from per_record.get(pos, ())

    def _final_warnings(self) -> None:
        if self.too_large:
            self.warn("jschema.warn.too_large", count=self.too_large, record=self.first["too_large"],
                      size=format_bytes(VALIDATE_PARSE_BYTES))
        if self.unreadable:
            self.warn("jschema.warn.unreadable", count=self.unreadable, record=self.first["unreadable"])
        if self.too_deep:
            self.warn("jschema.warn.too_deep", count=self.too_deep, record=self.first["too_deep"])
        if self.limited and self.max_per_record is not None:
            self.warn("jschema.warn.per_record", count=self.limited, limit=self.max_per_record)


# ----------------------------------------------------------------------------------
# validation: public entry points
# ----------------------------------------------------------------------------------
def iter_issues(doc: Any, compiled: Any, source: Any = None, *, scope: str = "auto", max_per_record: int | None = 10,
                formats: bool = False, progress: Progress | None = None,
                cancel: CancelToken | None = None) -> Iterator[Issue]:
    """Every issue of the records of ``source`` (``None`` = all) against ``compiled`` (a
    :class:`CompiledSchema` or a schema value): wrapper and container issues of a document
    scope, then the records' issues in scope order (bounded memory: issues are produced while
    the records are read). Raises like :func:`validate` (bad arguments and unsupported schemas
    at once, everything else while iterating)."""
    return _Validator(doc, compiled, source, scope, max_per_record, formats, progress, cancel).run()


def validate(doc: Any, compiled: Any, source: Any = None, *, scope: str = "auto", max_per_record: int | None = 10,
             max_issues: int | None = 1000, formats: bool = False, progress: Progress | None = None,
             cancel: CancelToken | None = None,
             on_issues: Callable[[list[Issue]], Any] | None = None) -> ValidationReport:
    """Validate the records of ``doc`` (all, or ``source``: a RecordSet, range or list of
    positions) against ``compiled`` (a :class:`CompiledSchema` or a schema value) and return
    a :class:`ValidationReport` (module docstring).

    ``scope``: ``"auto"``, ``"record"`` or ``"document"``; ``max_per_record``: issues kept
    per record (``None`` = all); ``max_issues``: issues stored in the report (``None`` = all;
    the counts cover every collected issue); ``formats``: assert the supported formats (it decides:
    a :class:`CompiledSchema` compiled the other way is recompiled once and cached).
    ``on_issues``: called in the calling (worker) thread with lists of the collected issues in
    report order -- every issue, also those beyond ``max_issues`` -- at most :data:`ISSUE_BATCH` at
    a time; a waiting batch is handed over at least every :data:`ISSUE_FLUSH_SECONDS` while records
    are read, the rest before the report is returned (never after an exception).
    Raises ``UserInputError`` (bad arguments, an unsupported schema, a non-decomposable
    document schema over :data:`DOCUMENT_PARSE_BYTES`), ``EditError("edits.error.stale_scope")``,
    ``Cancelled`` and ``FileChangedError``."""
    max_issues = _check_limit("max_issues", max_issues, zero=True)
    run = _Validator(doc, compiled, source, scope, max_per_record, formats, progress, cancel)
    builder = RecordSetBuilder("invalid", t("jschema.desc.invalid", name=run.doc.source_path.name), gen=run.gen)
    marks = bytearray()               # 1 bit per record (bounded memory); sized once the index is built
    invalid = [0]

    def on_invalid(pos: int) -> None:
        byte = pos >> 3
        if byte >= len(marks):
            marks.extend(bytes(max(byte + 1, (int(run.doc.record_count) + 7) >> 3) - len(marks)))
        bit = 1 << (pos & 7)
        if not marks[byte] & bit:     # a source may name a position twice: the set lists it once
            marks[byte] |= bit
            invalid[0] += 1
            builder.append(pos)

    run.invalid_hook = on_invalid
    batch: list[Issue] = []
    flushed = [time.perf_counter()]

    def flush() -> None:
        if batch and on_issues is not None:
            items = batch[:]
            batch.clear()
            flushed[0] = time.perf_counter()
            on_issues(items)

    if on_issues is not None:
        def tick() -> None:
            if batch and time.perf_counter() - flushed[0] >= ISSUE_FLUSH_SECONDS:
                flush()
        run.tick_hook = tick
    report = ValidationReport(scope="record", gen=run.gen, formats=run.formats)
    counts: dict[str, int] = {}
    paths: dict[str, int] = {}
    try:
        for issue in run.run():
            report.issue_count += 1
            counts[issue.keyword] = counts.get(issue.keyword, 0) + 1
            if issue.record is None:
                report.document_issues += 1
            norm = _normalized_path(issue.path)
            if norm in paths or len(paths) < PATHS_TRACKED:
                paths[norm] = paths.get(norm, 0) + 1
            if max_issues is None or len(report.issues) < max_issues:
                report.issues.append(issue)
            else:
                report.truncated = True
            if on_issues is not None:
                batch.append(issue)
                if len(batch) >= ISSUE_BATCH:
                    flush()
        flush()
        rs = builder.finish()
    except BaseException:
        builder.abort()
        raise
    report.records = rs
    report.scope = run.scope
    report.scanned = run.scanned
    report.invalid = invalid[0]
    report.counts = dict(sorted(counts.items(), key=lambda kv: (-kv[1], kv[0])))
    report.top_paths = sorted(paths.items(), key=lambda kv: (-kv[1], kv[0]))[:TOP_PATHS]
    report.too_large = run.too_large
    report.unreadable = run.unreadable
    report.too_deep = run.too_deep
    report.skipped = run.too_large + run.unreadable + run.too_deep
    report.limited_records = run.limited
    if report.truncated and max_issues is not None:
        run.warn("jschema.warn.issues_truncated", shown=max_issues, count=report.issue_count)
    report.warnings = tuple(run.warnings)
    report.warning_params = tuple(run.warning_params)
    report.elapsed = time.perf_counter() - run.started
    log.info("validated %s (%s scope): %d records, %d invalid, %d issues, %d skipped (%.2f s)",
             run.doc.source_path.name, report.scope, report.scanned, report.invalid, report.issue_count,
             report.skipped, report.elapsed)
    return report


# ----------------------------------------------------------------------------------
# generation
# ----------------------------------------------------------------------------------
_TYPE_OUT = {"object": "object", "array": "array", "string": "string", "number": "number", "bool": "boolean",
             "null": "null"}
_SCALAR_TYPES = frozenset({"string", "number", "bool", "null"})
ENUM_FIELDS = 64                      # strict generation: fields examined for an enum (one stats pass)


class _Gen:
    """Settings and collected state of one :func:`generate_schema` call."""

    def __init__(self, strict: bool, closed: bool, stats: Mapping[str, Any], formats: bool) -> None:
        self.strict = strict
        self.closed = closed              # additionalProperties: false allowed (not truncated)
        self.stats = stats                # field text -> FieldStats (enums)
        self.formats = formats            # format candidates wanted (a document to confirm them)
        self.candidates = 0


def _number_name(fi: Any) -> str:
    numbers = int(fi.types.get("number", 0))
    integers = getattr(fi, "integers", None)
    if numbers and isinstance(integers, int) and not isinstance(integers, bool) and integers >= numbers:
        return "integer"
    return "number"


def _examples(fi: Any) -> list[Any]:
    from .schema import ELLIPSIS

    out = []
    for text in fi.examples:
        if not isinstance(text, str) or text.endswith(ELLIPSIS):
            continue
        try:
            out.append(codec.loads(text))
        except (codec.JsonDecodeError, ValueError):
            continue
    return out


def _enum_values(st: Any, fi: Any) -> list[Any] | None:
    """The complete value list of a low-cardinality field from its statistics, or ``None``."""
    from .paths import MISSING

    if st is None or not set(fi.types) <= _SCALAR_TYPES:
        return None
    try:
        distinct = int(st.distinct)
        if not (st.distinct_exact and st.top_exact) or int(getattr(st, "skipped", 0) or 0) \
                or int(getattr(st, "uncounted", 0) or 0):
            return None
        top = list(st.top)
        values = int(st.values)
    except (AttributeError, TypeError, ValueError):
        return None
    if not 0 < distinct <= ENUM_MAX or len(top) != distinct or values <= distinct:
        return None
    out = [vc.value for vc in top]
    if any(v is MISSING or isinstance(v, (dict, list)) for v in out):
        return None
    return out


def _format_candidate(fi: Any) -> str | None:
    """A format every example of a string field has (confirmed later over the document)."""
    if "string" not in fi.types or not set(fi.types) <= {"string", "null"}:
        return None
    strings = [x for x in _examples(fi) if type(x) is str]
    if not strings:
        return None
    for name in _FORMATS_ASSERTED:
        check_fmt = _FORMAT_CHECKS[name]
        if all(check_fmt(s) for s in strings):
            return name
    return None


def _field_schema(fi: Any, g: _Gen) -> dict[str, Any]:
    out: dict[str, Any] = {}
    names = [_number_name(fi) if tn == "number" else _TYPE_OUT.get(tn, tn) for tn in fi.types]
    if len(names) == 1:
        out["type"] = names[0]
    elif names:
        out["type"] = names
    if g.strict:
        if g.formats:
            fmt = _format_candidate(fi)
            if fmt is not None:
                out["format"] = fmt
                g.candidates += 1
        enum = _enum_values(g.stats.get(fi.text), fi) if g.stats else None
        if enum is not None:
            out["enum"] = enum
        if "number" in fi.types and fi.num_range is not None:
            out["minimum"] = codec.loads(fi.num_range[0])
            out["maximum"] = codec.loads(fi.num_range[1])
        if "string" in fi.types and fi.str_len is not None:
            out["minLength"], out["maxLength"] = int(fi.str_len[0]), int(fi.str_len[1])
        if "array" in fi.types and fi.arr_len is not None:
            out["minItems"], out["maxItems"] = int(fi.arr_len[0]), int(fi.arr_len[1])
    objects = int(fi.types.get("object", 0))
    props: dict[str, Any] = {}
    required: list[str] = []
    pooled = None
    item = None
    for seg, child in fi.children.items():
        if getattr(child, "is_key", False):
            continue                       # the member key pseudo-field ($key)
        if seg is Wild.ANY_KEY:
            pooled = _field_schema(child, g)
        elif seg is Wild.ANY_INDEX:
            item = _field_schema(child, g)
        elif isinstance(seg, str):
            props[seg] = _field_schema(child, g)
            if objects and child.present == objects:
                required.append(seg)
    if props:
        out["properties"] = props
    if required:
        out["required"] = required
    if pooled is not None:
        out["additionalProperties"] = pooled
    elif g.strict and g.closed and objects:
        out["additionalProperties"] = False
    if item is not None:
        out["items"] = item
    examples = _examples(fi)
    if examples:
        out["examples"] = examples
    return out


def _format_probe(s: Any) -> Any:
    """The generated schema reduced to its navigation keywords and ``format`` (the same JSON
    Pointers lead to the same subschemas)."""
    if not isinstance(s, dict):
        return True
    out: dict[str, Any] = {}
    if "format" in s:
        out["format"] = s["format"]
    if isinstance(s.get("properties"), dict):
        out["properties"] = {k: _format_probe(v) for k, v in s["properties"].items()}
    if isinstance(s.get("additionalProperties"), dict):
        out["additionalProperties"] = _format_probe(s["additionalProperties"])
    if isinstance(s.get("items"), dict):
        out["items"] = _format_probe(s["items"])
    return out


def _pointer_get(value: Any, pointer: str) -> Any:
    node = value
    for token in pointer.split("/")[1:] if pointer else ():
        token = token.replace("~1", "/").replace("~0", "~")
        node = node[token]
    return node


def _confirm_formats(doc: Any, rec: dict[str, Any], progress: Progress | None, cancel: CancelToken | None) -> int:
    """Drop every generated ``format`` that some value of the document breaks (one validation
    pass with format assertion over every record). Returns the formats dropped."""
    probe = _format_probe(rec)
    failing: set[str] = set()
    for issue in iter_issues(doc, probe, scope="record", max_per_record=None, formats=True, progress=progress,
                             cancel=cancel):
        if issue.keyword == "format":
            failing.add(issue.schema_path)
    for loc in failing:
        holder = _pointer_get(rec, loc[:-len("/format")])
        holder.pop("format", None)
    return len(failing)


def _value_stats(doc: Any, schema: Any, progress: Progress | None, cancel: CancelToken | None) -> dict[str, Any]:
    """Statistics of the scalar leaf fields (enum candidates) from :mod:`jsonreshape.stats`
    when that module is available (else no enums)."""
    try:
        from .stats import field_stats  # type: ignore[attr-defined]
    except ImportError:
        log.info("schema generation: no statistics module, strict schema without enums")
        return {}
    fields = [fi.text for fi in schema.fields()
              if not fi.children and fi.types and set(fi.types) <= _SCALAR_TYPES][:ENUM_FIELDS]
    if not fields:
        return {}
    try:
        result = list(field_stats(doc, fields, top=ENUM_MAX, progress=progress, cancel=cancel))
    except UserInputError as exc:      # a field the statistics cannot address: no enums (logged)
        log.warning("schema generation: field statistics not available: %s", exc)
        return {}
    if len(result) == len(fields):     # one result per requested field, in order
        return dict(zip(fields, result))
    return {st.field: st for st in result}


def _place(ws: dict[str, Any], path: tuple, container: Any) -> bool:
    """Put ``container`` at ``path`` of the wrapper schema ``ws`` (array elements through
    ``prefixItems``, so that the other elements keep their schema)."""
    import copy

    node: Any = ws
    for i, seg in enumerate(path):
        last = i == len(path) - 1
        if not isinstance(node, dict):
            return False
        if isinstance(seg, str):
            props = node.get("properties")
            if not isinstance(props, dict) or seg not in props:
                return False
            if last:
                props[seg] = container
                return True
            node = props[seg]
        else:
            items = node.get("items", True)
            pre = node.setdefault("prefixItems", [])
            while len(pre) <= seg:
                pre.append(copy.deepcopy(items))
            if last:
                pre[seg] = container
                return True
            node = pre[seg]
    return False


def _minimal_wrapper(path: tuple, container: Any) -> Any:
    out = container
    for seg in reversed(path):
        if isinstance(seg, str):
            out = {"type": "object", "properties": {seg: out}, "required": [seg]}
        else:
            out = {"type": "array", "prefixItems": [True] * seg + [out], "minItems": seg + 1}
    return out


def _wrapper_schema(doc: Any, container: Any, g: _Gen) -> Any:
    from .schema import SchemaBuilder

    path = tuple(doc.record_path or ())
    try:
        parts = doc.wrapper_parts()
    except TooLargeError:
        parts = None
    if parts is not None and len(parts[0]) + len(parts[1]) <= VALIDATE_PARSE_BYTES:
        try:
            value = codec.loads(parts[0] + (b"{}" if doc.member_records else b"[]") + parts[1])
        except (codec.JsonDecodeError, ValueError, RecursionError):
            value = None
        if value is not None:
            builder = SchemaBuilder()
            builder.add(value)
            ws = builder.build()
            wg = _Gen(g.strict, not ws.truncated, {}, False)
            out = _field_schema(ws.root, wg)
            out.pop("examples", None)
            if _place(out, path, container):
                return out
    log.info("schema generation: wrapper of %s not available, minimal wrapper schema", doc.source_path.name)
    return _minimal_wrapper(path, container)


def generate_schema(schema: Any, *, doc: Any = None, document: bool = True, strict: bool = False,
                    title: str | None = None, stats: Any = None, progress: Progress | None = None,
                    cancel: CancelToken | None = None) -> dict[str, Any]:
    """A JSON Schema (draft 2020-12) of the data described by ``schema`` (an
    :func:`~jsonreshape.schema.infer_schema` result; module docstring for the rules).

    ``doc``: the document (its kind gives the document-level shape; strict extras read it);
    ``document``: a document schema (default) or a record schema; ``strict``: add the strict
    constraints; ``title``: default the document's file name; ``stats``: field statistics to
    use for enums (a mapping field text -> :class:`jsonreshape.stats.FieldStats` or a list of
    them; default: computed from ``doc`` in strict mode). Write the result with
    :func:`dump_schema` (exact numbers)."""
    from .schema import RECORD_KEY

    if doc is not None:
        doc = doc.pinned()
    stats_map: dict[str, Any] = {}
    p_stats = p_formats = None
    if strict and doc is not None and progress is not None:
        if progress.total is None:
            progress.set_total(_data_bytes(doc))
        p_stats = progress.child(0.0, 0.5)
        p_formats = progress.child(0.5, 1.0)
    if strict:
        if stats is not None:
            stats_map = dict(stats) if isinstance(stats, Mapping) else {st.field: st for st in stats}
        elif doc is not None:
            stats_map = _value_stats(doc, schema, p_stats, cancel)
    g = _Gen(bool(strict), not schema.truncated, stats_map, strict and doc is not None)
    rec = _field_schema(schema.root, g)
    if g.candidates:
        _confirm_formats(doc, rec, p_formats, cancel)
    kind = doc.kind if doc is not None else None
    scope = SCOPE_RECORD
    body: Any = rec
    if document and kind in ("array", "brace_list", "stream"):
        body = {"type": "array", "items": rec}
        scope = SCOPE_DOCUMENT
    elif document and kind == "keyed":
        body = {"type": "object", "additionalProperties": rec}
        key_field = schema.root.children.get(RECORD_KEY)
        if strict and key_field is not None:
            names: dict[str, Any] = {}
            enum = _enum_values(stats_map.get(key_field.text), key_field)
            if enum is not None:
                names["enum"] = enum
            if key_field.str_len is not None:
                names["minLength"], names["maxLength"] = int(key_field.str_len[0]), int(key_field.str_len[1])
            if names:
                body["propertyNames"] = names
        scope = SCOPE_DOCUMENT
    elif document and kind == "wrapped":
        container: dict[str, Any] = ({"type": "object", "additionalProperties": rec} if doc.member_records
                                     else {"type": "array", "items": rec})
        body = _wrapper_schema(doc, container, g)
        scope = SCOPE_DOCUMENT
    out: dict[str, Any] = {"$schema": DIALECT}
    if title is None and doc is not None:
        title = doc.source_path.name
    if title:
        out["title"] = title
    today = datetime.date.today().isoformat()
    if schema.complete:
        out["description"] = t("jschema.gen.description.full", records=_count_text(schema.scanned), date=today)
    else:
        out["description"] = t("jschema.gen.description.sample", records=_count_text(schema.scanned),
                               total=_count_text(schema.total_records), date=today)
    out["$comment"] = scope
    if isinstance(body, dict):
        for k, v in body.items():
            out.setdefault(k, v)
    if progress is not None and strict and doc is not None:
        progress.finish()
    return out


# ----------------------------------------------------------------------------------
# schema files
# ----------------------------------------------------------------------------------
def dump_schema(value: Any, *, indent: int | str | None = 2) -> bytes:
    """UTF-8 JSON text of a schema with every number exactly as written (``codec.dumps``;
    non-ASCII unescaped), ending with a newline."""
    return codec.dumps(value, indent=indent) + b"\n"


def load_schema(path: str | Path) -> Any:
    """Read a JSON Schema file (UTF-8 with or without BOM, UTF-16 with BOM) with exact numbers.
    Raises a localized ``UserInputError`` (``jschema.error.schema_file``) when the file cannot be
    read or is not JSON; a file that cannot be opened raises the localized error of
    :func:`jsonreshape.fileio.open_shared` (``error.file_not_found``, ``error.permission``, ...)."""
    from .fileio import open_shared

    p = Path(path)
    try:
        with open_shared(p, "rb") as f:
            data = f.read(SCHEMA_FILE_BYTES + 1)
    except OSError as exc:
        raise UserInputError("jschema.error.schema_file", path=str(p), detail=exc.strerror or str(exc)) from None
    if len(data) > SCHEMA_FILE_BYTES:
        raise UserInputError("jschema.error.schema_file", path=str(p),
                             detail=t("jschema.detail.too_big", limit=format_bytes(SCHEMA_FILE_BYTES)))
    try:
        if data.startswith(b"\xef\xbb\xbf"):
            text = data[3:].decode("utf-8")
        elif data.startswith((b"\xff\xfe", b"\xfe\xff")):
            text = data.decode("utf-16")
        else:
            text = data.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise UserInputError("jschema.error.schema_file", path=str(p), detail=str(exc)) from None
    try:
        return codec.loads(text)
    except codec.JsonDecodeError as exc:
        raise UserInputError("jschema.error.schema_file", path=str(p), detail=str(exc)) from None

