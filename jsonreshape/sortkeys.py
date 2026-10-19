"""Typed, order-preserving sort keys (docs/ARCHITECTURE.md §6, AGENT_PROMPT §6.4).

The external sort (``recordset.sort_records``) compares keys as plain ``bytes``.  This module
turns the value of one sort field into such a key so that ``key(a) < key(b)`` (bytewise) is
exactly the order the user asked for.  Keys are built either from the raw JSON token bytes of
the value (:func:`encode_token`, fed by ``core.PathExtractor``) or from a Python value
(:func:`encode_value`); for every JSON text ``t`` and every spec ``s``::

    encode_token(t, s) == encode_value(codec.loads(t), s)

Field keys
----------
Every field key is one *tag* byte followed by a type specific *payload*:

====================================  ===========  ============
value                                 ascending    descending
====================================  ===========  ============
missing (``nulls="first"``)           ``01``       ``01``
null    (``nulls="first"``)           ``02``       ``02``
number                                ``10``       ``14``
string                                ``11``       ``13``
boolean (false < true)                ``12``       ``12``
array                                 ``13``       ``11``
object                                ``14``       ``10``
null    (``nulls="last"``)            ``20``       ``20``
missing (``nulls="last"``)            ``21``       ``21``
====================================  ===========  ============

So present values are ordered numbers < strings < booleans < arrays < objects.  A descending
field inverts every payload byte (``b -> 0xFF - b``) and mirrors the type tags; null and
missing keys are never inverted, so their placement does not depend on the direction:
``nulls="last"`` puts null, then missing after every value, ``nulls="first"`` puts missing,
then null before every value (also for descending fields).

Every payload is *prefix-free* (no payload is a proper prefix of another one), therefore
inversion exactly reverses the order and field keys can simply be concatenated:
:func:`compose` joins the field keys of one record and the joined keys compare field by
field.  Equal values give identical keys; different values of the same type give different
keys (numbers: different decimal values; strings: different strings), with the one
documented exception of absurd exponents (below).  To make a sort stable, append the record
number (e.g. 8 bytes big-endian) after the composed key.

Numbers
-------
Numbers compare by their exact decimal value, read from the token text (no float rounding):
``-1e400 < -5 < -0.5 < 0 == -0.0 == -0 < 1e-400 < 0.1 < 1.1 == 1.10 < 2 <
12345678901234567890 < 1e400``.  A non-zero number ``x`` is normalised to
``x = ±0.d1d2...dn × 10**E`` (``d1 != 0``, no trailing zeros) and encoded as a sign byte
(``01`` negative, ``02`` zero, ``03`` positive), then ``E`` (one byte for ``-64 <= E < 64``,
otherwise a length-prefixed big-endian form), then the digits ``d1..dn`` and a ``00``
terminator; negative numbers invert the bytes after the sign.  ``E`` is clamped to
``±2**62``: numbers with more than ~4.6·10**18 decimal digits before or after the point
compare as if their exponent were ``±2**62`` (such tokens cannot occur in real data).
Python-only values: ``float('inf')`` / ``Decimal('Infinity')`` sort after every finite
number, ``-inf`` before; NaN sorts after ``+inf``.  Python floats are encoded through their
shortest round-trip text (``repr``), exactly like they would be written to JSON.

Booleans, arrays, objects
-------------------------
``false < true``.  Arrays and objects are ordered by their compact JSON text
(:func:`codec.dumps`, UTF-8 bytes, document key order, exact number text), as the spec
requires: a plain byte order that is deterministic but not element-wise (``[1,2] < [1]``
because ``,`` < ``]``; ``{"a":1} < {}``; ``[1.10]`` and ``[1.1]`` are different arrays).

Strings
-------
A string key has four levels, compared one after the other:

1. **Primary** - a sequence of collation units (below), case-insensitive unless
   ``case_sensitive=True``.
2. **Leading zeros** (only ``natural=True``) - for the digit runs in order, the number of
   leading zeros; fewer leading zeros first (``a1 < a01 < a001``).
3. **Case** (only ``case_sensitive=False``) - one flag per character that has a primary
   weight (an expansion such as ``ß`` -> ``ss`` gives one flag per letter, a merged letter
   keeps the flags of both its letters; ignorable characters and, in ``uz``/``simple``,
   apostrophes have none), compared in order, lowercase/uncased before uppercase/titlecase:
   ``abdulla < Abdulla < ABDULLA``, ``aB < Ab``, ``Sh < SH``.  (When levels 1 and 2 tie,
   both strings have the same number of flags.)
4. **Original string** - code point order of the original text, so the order is total and
   deterministic (e.g. apostrophe variants and accents only differ here).

Collation ``"uz"`` (default) primary units, in this order of groups:

* ignorable (removed): format characters (``Cf``: ZWJ, ZWNJ, soft hyphen, BOM, ...) and
  combining marks (``Mn``/``Me``).  A string containing combining marks is NFC-normalised
  first (so ``e`` + U+0301 equals ``é`` and ``и`` + U+0306 equals ``й``); marks that do not
  compose are then ignored.
* control characters (one shared weight), then whitespace (all whitespace characters share
  one weight: space, tab, NBSP, ...);
* ASCII punctuation and symbols, by code point; then every other punctuation/symbol
  character (``P*``, ``S*``, ``No``: dashes, quotes, ``«»``, ``½``, emoji, ...; also
  non-Latin modifier letters below U+0370 such as ``ˈ``) sharing one weight;
* digits: ASCII ``0-9``; other Unicode decimal digits (``Nd``) count as the ASCII digit of
  the same value;
* Latin letters in the official Uzbek order ``a b d e f g h i j k l m n o p q r s t u v x y
  z oʻ gʻ sh ch ng`` with ``c`` and ``w`` at their English positions; ``o``/``g`` followed by
  any apostrophe variant (U+0027 ', U+2018 ‘, U+2019 ’, U+02BB ʻ, U+02BC ʼ, U+0060 `)
  form the letters ``oʻ``/``gʻ``; ``sh``, ``ch``, ``ng`` are single letters (merging order:
  ``oʻ``/``gʻ`` first, then ``sh``/``ch``, then ``ng``, so ``ngʻ`` is ``n`` + ``gʻ`` and
  ``s'h`` stays ``s`` + ``h``).  Any other apostrophe is ignorable.  Accented and
  compatibility Latin letters sort with their base letters (NFKD: ``é ö ñ ş ğ ç`` -> ``e o n
  s g c``, fullwidth ``Ａ`` -> ``a``, ``ﬁ`` -> ``fi``); ``ß`` -> ``ss``, ``æ`` -> ``ae``,
  ``œ`` -> ``oe``, ``ø`` -> ``o``, ``ł`` -> ``l``, ``đ``/``ð`` -> ``d``, ``þ`` -> ``th``,
  ``ı`` -> ``i``, ``ŋ`` -> ``ng`` (the base letters then take part in the merges above,
  e.g. ``ş`` + ``h`` is ``sh``).  Remaining Latin letters (IPA ``ə``, ``ʔ``, ...) share one
  weight after ``ng``.
* Cyrillic letters in the Uzbek order ``а б в г д е ё ж з и й к л м н о п р с т у ф х ц ч ш
  щ ъ ы ь э ю я ў қ ғ ҳ`` (Russian ``щ`` after ``ш``, ``ы`` after ``ъ``); other Cyrillic
  letters (``і є ї ґ ң ө`` ...) follow, by the code point of their lowercase form.
* every other script (Greek, Armenian, Arabic, CJK, private use, lone surrogates, ...):
  by the code point of the lowercase form (case-sensitive mode: the original code point).

Collation ``"simple"``: the same, but without the letters ``oʻ gʻ sh ch ng`` (letter by
letter) and every apostrophe variant is ignorable.

Collation ``"unicode"``: casefolded code point order (``str.casefold``); nothing is
ignorable, no merges, no normalisation; with ``case_sensitive=True`` the original code
point order.  Natural digit runs (ASCII digits only) apply as well.

``natural=True`` (default): a maximal run of digits (ASCII; for ``uz``/``simple`` also other
``Nd`` digits) is one unit that compares by numeric value (``child2 < child10``,
``v1.9 < v1.10``) and sorts where digits sort; signs and decimal points are not part of a
run.  Ties are broken by fewer leading zeros first (level 2).

``case_sensitive=True``: case is significant at the primary level, letter by letter with
lowercase before uppercase: ``a < A < b < B ... < ng < Ng`` (so ``aC < Ab``); merged
letters take the case of their first letter.  Other scripts then use their original code
points (``Α`` U+0391 before ``α`` U+03B1).  Level 3 is omitted.

Performance: a string key is built with one ``str.translate`` pass over a lazily filled
table that maps each character to a (primary weight, case flag) pair, two slices and a few
C-level ``replace`` calls (merges and digit runs only touch the primary weights); pure-ASCII
strings use two ``bytes.translate`` passes instead.  Field keys are memoised in small
bounded caches (repeated values such as city names cost one dict lookup).  Measured on the
development machine (Python 3.14, unique values, ``uz`` collation): ~0.75 µs per ASCII
string of ~20 characters, ~1.2 µs for Uzbek Latin with ``ʻ``, ~1.5 µs for Cyrillic,
~1.3 µs with digit runs, ~0.4 µs per integer token, ~0.5 µs per decimal token (``123.45``),
~0.85 µs with an exponent; ~0.06 µs for a repeated value.  Keys and tables are safe to use
from several threads.
"""

from __future__ import annotations

import json
import logging
import math
import re
import unicodedata
from dataclasses import dataclass
from decimal import Decimal
from typing import Any, Callable, Sequence

from . import codec
from .codec import JsonFloat, JsonInt
from .errors import UserInputError
from .i18n import t
from .paths import MISSING, PathPattern, Wild, format_path, parse_pattern
from .paths import get as path_get

log = logging.getLogger(__name__)

__all__ = [
    "APOSTROPHES",
    "COLLATIONS",
    "CYRILLIC_ALPHABET",
    "INDEX_FIELD",
    "KEY_FIELD",
    "LATIN_ALPHABET",
    "NULLS_POSITIONS",
    "SortKey",
    "char_units",
    "clear_caches",
    "collation_key",
    "compose",
    "describe_keys",
    "encode_token",
    "encode_value",
    "record_key",
    "token_encoder",
    "value_encoder",
]

COLLATIONS = ("uz", "simple", "unicode")
NULLS_POSITIONS = ("last", "first")
KEY_FIELD = "$key"      # keyed documents: the member key of the record
INDEX_FIELD = "$index"  # the record number (0-based)

_CACHE_MAX = 8192  # entries per bounded memo dict (cleared when full)

# ---------------------------------------------------------------------------------------
# type tags
# ---------------------------------------------------------------------------------------
_RANK_NUMBER, _RANK_STRING, _RANK_BOOL, _RANK_ARRAY, _RANK_OBJECT = range(5)
_MISSING_FIRST = b"\x01"
_NULL_FIRST = b"\x02"
_NULL_LAST = b"\x20"
_MISSING_LAST = b"\x21"

_INV = bytes(range(255, -1, -1))  # bytes.translate table: b -> 0xFF - b


def _type_tag(rank: int, descending: bool) -> bytes:
    return bytes((0x14 - rank if descending else 0x10 + rank,))


# ---------------------------------------------------------------------------------------
# numbers
# ---------------------------------------------------------------------------------------
_NUM_ZERO = b"\x02"
_NUM_NAN = b"\x04"
_NUM_POS_INF = b"\x03\xff"  # 0xFF is above every exponent lead byte (<= 0xC8)
_NUM_NEG_INF = b"\x01\x00"
_E_LIMIT = 1 << 62
_E_HUGE = 1 << 63  # stands for "beyond the clamp" before clamping

_NUM_BODY_RE = re.compile(rb"(0|[1-9][0-9]*)(?:\.([0-9]+))?(?:[eE]([+-]?[0-9]+))?")


def _exp_bytes(e: int) -> bytes:
    """Order-preserving, prefix-free encoding of a (clamped) decimal exponent.

    ``-64 <= e < 64``: one byte ``0x80 + e`` (0x40..0xBF); larger: ``0xC0 + n`` then ``e`` in
    ``n`` big-endian bytes (lead 0xC1..0xC8); smaller: ``0x40 - n`` then ``2**(8n) - 1 + e``
    in ``n`` bytes (lead 0x38..0x3F).
    """
    if -64 <= e < 64:
        return bytes((0x80 + e,))
    if e > 0:
        n = (e.bit_length() + 7) // 8
        return bytes((0xC0 + n,)) + e.to_bytes(n, "big")
    m = -e
    n = (m.bit_length() + 7) // 8
    return bytes((0x40 - n,)) + ((1 << (8 * n)) - 1 - m).to_bytes(n, "big")


_POS_HEAD = tuple(b"\x03" + _exp_bytes(e) for e in range(-64, 64))
_NEG_HEAD = tuple(b"\x01" + _exp_bytes(e).translate(_INV) for e in range(-64, 64))


def _magnitude(neg: bool, e: int, sig: bytes) -> bytes:
    """Payload of ``±0.<sig> × 10**e`` (``sig``: ASCII digits, no leading/trailing zeros)."""
    if -64 <= e < 64:
        if neg:
            return _NEG_HEAD[e + 64] + sig.translate(_INV) + b"\xff"
        return _POS_HEAD[e + 64] + sig + b"\x00"
    if e > _E_LIMIT:
        e = _E_LIMIT
    elif e < -_E_LIMIT:
        e = -_E_LIMIT
    head = _exp_bytes(e)
    if neg:
        return b"\x01" + head.translate(_INV) + sig.translate(_INV) + b"\xff"
    return b"\x03" + head + sig + b"\x00"


def _exponent_value(ep: bytes) -> int:
    if len(ep) <= 21:
        return int(ep)
    neg = ep[:1] == b"-"
    digits = ep.lstrip(b"+-").lstrip(b"0")
    if len(digits) > 20:  # >= 1e20 > 2**63: beyond the clamp (and maybe beyond int()'s digit limit)
        return -_E_HUGE if neg else _E_HUGE
    value = int(digits) if digits else 0
    return -value if neg else value


def _number_payload(tok: bytes) -> bytes:
    """Ascending payload of a JSON number token (``ValueError`` if it is not one)."""
    neg = tok[:1] == b"-"
    body = tok[1:] if neg else tok
    if body.isdigit():  # plain integer (the common case)
        if body[0] == 0x30:
            if len(body) == 1:
                return _NUM_ZERO
            raise ValueError(f"invalid JSON number token: {tok[:40]!r}")
        return _magnitude(neg, len(body), body.rstrip(b"0"))
    dot = body.find(b".")
    ip, fp = body[:dot], body[dot + 1:]
    if dot > 0 and ip.isdigit() and fp.isdigit() and (ip[0] != 0x30 or dot == 1):
        ep = None  # plain decimal fraction without exponent (fast path)
    else:
        m = _NUM_BODY_RE.fullmatch(body)
        if m is None:
            raise ValueError(f"invalid JSON number token: {tok[:40]!r}")
        ip, fp, ep = m.groups()
    if ip != b"0":
        e = len(ip)
        sig = (ip + fp).rstrip(b"0") if fp else ip.rstrip(b"0")
    else:
        if not fp:
            return _NUM_ZERO
        fs = fp.lstrip(b"0")
        if not fs:
            return _NUM_ZERO
        e = len(fs) - len(fp)
        sig = fs.rstrip(b"0")
    if ep:
        e += _exponent_value(ep)
    return _magnitude(neg, e, sig)


def _int_payload(v: int) -> bytes:
    if v == 0:
        return _NUM_ZERO
    neg = v < 0
    digits = int.__repr__(-v if neg else v).encode("ascii")
    return _magnitude(neg, len(digits), digits.rstrip(b"0"))


def _float_payload(v: float) -> bytes:
    if v != v:
        return _NUM_NAN
    if v == math.inf:
        return _NUM_POS_INF
    if v == -math.inf:
        return _NUM_NEG_INF
    return _number_payload(float.__repr__(v).encode("ascii"))


def _decimal_payload(v: Decimal) -> bytes:
    sign, digits, exp = v.as_tuple()
    if exp == "F":
        return _NUM_NEG_INF if sign else _NUM_POS_INF
    if not isinstance(exp, int):  # 'n' / 'N': NaN
        return _NUM_NAN
    text = "".join(map(str, digits)).lstrip("0")
    if not text:
        return _NUM_ZERO
    return _magnitude(bool(sign), len(text) + exp, text.rstrip("0").encode("ascii"))


# ---------------------------------------------------------------------------------------
# strings: character classification
# ---------------------------------------------------------------------------------------
# apostrophe variants: U+0027 ' U+2018 ‘ U+2019 ’ U+02BB ʻ U+02BC ʼ U+0060 `
APOSTROPHES = "".join(map(chr, (0x27, 0x2018, 0x2019, 0x2BB, 0x2BC, 0x60)))
_ASCII_PUNCT = "".join(ch for ch in map(chr, range(0x21, 0x7F)) if not ch.isalnum() and ch not in "'`")
LATIN_ALPHABET = tuple("abcdefghijklmnopqrstuvwxyz") + ("oʻ", "gʻ", "sh", "ch", "ng")
CYRILLIC_ALPHABET = "абвгдеёжзийклмнопрстуфхцчшщъыьэюяўқғҳ"
_LATIN_INDEX = {letter: i for i, letter in enumerate(LATIN_ALPHABET)}
_CYR_INDEX = {letter: i for i, letter in enumerate(CYRILLIC_ALPHABET)}
_ASCII_LETTERS = frozenset("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ")
# Latin letters without a usable decomposition -> ASCII letters (case preserved)
_LATIN_EXTRA = {
    "ß": "ss", "ẞ": "SS", "æ": "ae", "Æ": "AE", "œ": "oe", "Œ": "OE", "ø": "o", "Ø": "O",
    "ł": "l", "Ł": "L", "đ": "d", "Đ": "D", "ð": "d", "Ð": "D", "þ": "th", "Þ": "TH",
    "ı": "i", "ŋ": "ng", "Ŋ": "NG", "ħ": "h", "Ħ": "H", "ŧ": "t", "Ŧ": "T", "ŀ": "l",
    "Ŀ": "L", "ƒ": "f",
}
_LETTER_KINDS = frozenset(("latin", "latin_other", "cyr", "cyr_other", "cyr_other_last", "other"))

# primary weights (code points of the weight characters; see module docstring for groups)
_W_CTRL = 0x03
_W_WS = 0x04
_W_PUNCT = 0x05          # + index in _ASCII_PUNCT (30 characters) -> 0x05..0x22
_W_PUNCT_OTHER = 0x23
_W_COMB = 0x24           # transient marker: combining mark present -> NFC path
_W_APOS = 0x25           # transient marker: apostrophe (uz), merged or removed
_W_DIGIT = 0x30          # '0'..'9'; a natural digit run starts with 0x30 as well
_W_LATIN = 0x3A          # CI: + index (31 letters) -> 0x3A..0x58; CS: + 2*index + upper -> ..0x77
_W_LATIN_OTHER_CI = 0x59
_W_LATIN_OTHER_CS = 0x78  # + upper
_W_CYR_CI = 0x5A         # + index (37 letters) -> 0x5A..0x7E
_W_CYR_CS = 0x80         # + 2*index + upper -> 0x80..0xC9
_W_CYR_OTHER_CI = 0x80   # + (lowercase code point - 0x400), U+0400..U+052F -> 0x80..0x1AF
_W_CYR_OTHER_LAST_CI = 0x1B0
_W_CYR_OTHER_CS = 0x100  # + 2*(lowercase code point - 0x400) + upper -> 0x100..0x35F
_W_CYR_OTHER_LAST_CS = 0x360  # + upper
_W_OTHER_MIN = 0x370     # other scripts use their own code points (>= U+0370)

# case flags (second character of every (weight, flag) pair; the case level is the
# sequence of flags of all letters/characters, see _make_string_payload)
_F_LOWER = "\x01"  # lowercase or uncased
_F_UPPER = "\x02"  # uppercase or titlecase
_F_DIGIT = "\x03"  # digit (marks natural runs)
_F_APOS = "\x04"   # apostrophe (uz): removed from the case level
_COMB = chr(_W_COMB)
_APOS = chr(_W_APOS)
_COMB_PAIR = _COMB + _F_LOWER
_APOS_PAIR = _APOS + _F_APOS

Unit = tuple  # (kind, value, upper, code point)


def _latin_letters(c: str) -> str | None:
    """ASCII letters (case preserved) a Latin(-like) letter sorts as, or ``None``."""
    extra = _LATIN_EXTRA.get(c)
    if extra is not None:
        return extra
    if c in _ASCII_LETTERS:
        return c
    decomposed = unicodedata.normalize("NFKD", c)
    base = "".join(ch for ch in decomposed if unicodedata.category(ch) not in ("Mn", "Me"))
    if base and all(ch in _ASCII_LETTERS for ch in base):
        return base
    return None


def _other_weight(c: str) -> int | None:
    """Primary weight of a letter of another script (lowercase code point, >= U+0370)."""
    low = c.lower()
    if len(low) == 1 and ord(low) >= _W_OTHER_MIN:
        return ord(low)
    if ord(c) >= _W_OTHER_MIN:
        return ord(c)
    decomposed = unicodedata.normalize("NFKD", c).lower()
    if len(decomposed) == 1 and ord(decomposed) >= _W_OTHER_MIN:
        return ord(decomposed)  # e.g. MICRO SIGN -> Greek mu
    return None


def char_units(c: str) -> list[Unit]:
    """Collation units of one character for the ``uz``/``simple`` collations.

    Returns a list of ``(kind, value, upper, code_point)``; kinds: ``apos``, ``comb``,
    ``ctrl``, ``ws``, ``punct`` (value = index among ASCII punctuation), ``punct_other``,
    ``digit`` (value = digit value), ``latin`` (value = index in :data:`LATIN_ALPHABET`),
    ``latin_other``, ``cyr`` (index in :data:`CYRILLIC_ALPHABET`), ``cyr_other`` (value =
    lowercase code point), ``cyr_other_last``, ``other`` (value = lowercase code point).
    Ignorable characters give ``[]``.  Public for tests and tools; not a hot path.
    """
    cp = ord(c)
    if c in APOSTROPHES:
        return [("apos", 0, 0, cp)]
    cat = unicodedata.category(c)
    if cat in ("Mn", "Me"):
        return [("comb", 0, 0, cp)]
    if cat == "Cf":
        return []
    if c.isspace():
        return [("ws", 0, 0, cp)]
    if cat == "Cc":
        return [("ctrl", 0, 0, cp)]
    i = _ASCII_PUNCT.find(c)
    if i >= 0:
        return [("punct", i, 0, cp)]
    if cat == "Nd":
        return [("digit", unicodedata.decimal(c), 0, cp)]
    if cat[0] == "L" or cat == "Nl":
        letters = _latin_letters(c)
        if letters is not None:
            return [("latin", _LATIN_INDEX[ch.lower()], int(ch.isupper()), cp) for ch in letters]
        upper = int(cat in ("Lu", "Lt"))
        name = unicodedata.name(c, "")
        if name.startswith("LATIN "):
            return [("latin_other", 0, upper, cp)]
        if name.startswith("CYRILLIC "):
            low = c.lower()
            if len(low) == 1:
                j = _CYR_INDEX.get(low)
                if j is not None:
                    return [("cyr", j, upper, cp)]
                if 0x400 <= ord(low) <= 0x52F:
                    return [("cyr_other", ord(low), upper, cp)]
            return [("cyr_other_last", 0, upper, cp)]
        weight = _other_weight(c)
        if weight is not None:
            return [("other", weight, upper, cp)]
        return [("punct_other", 0, 0, cp)]
    if cat[0] in "PS" or cat == "No":
        return [("punct_other", 0, 0, cp)]
    if cp >= _W_OTHER_MIN:  # Co, Cs, Cn, other Nl ...: other scripts by code point
        return [("other", cp, 0, cp)]
    return [("punct_other", 0, 0, cp)]


def _uz_weight(kind: str, value: int, upper: int, cp: int, cs: bool) -> int:
    if kind == "latin":
        return _W_LATIN + (2 * value + upper if cs else value)
    if kind == "digit":
        return _W_DIGIT + value
    if kind == "punct":
        return _W_PUNCT + value
    if kind == "ws":
        return _W_WS
    if kind == "ctrl":
        return _W_CTRL
    if kind == "punct_other":
        return _W_PUNCT_OTHER
    if kind == "cyr":
        return _W_CYR_CS + 2 * value + upper if cs else _W_CYR_CI + value
    if kind == "latin_other":
        return _W_LATIN_OTHER_CS + upper if cs else _W_LATIN_OTHER_CI
    if kind == "cyr_other":
        return _W_CYR_OTHER_CS + 2 * (value - 0x400) + upper if cs else _W_CYR_OTHER_CI + value - 0x400
    if kind == "cyr_other_last":
        return _W_CYR_OTHER_LAST_CS + upper if cs else _W_CYR_OTHER_LAST_CI
    if kind == "other":
        return cp if cs and cp >= _W_OTHER_MIN else value
    raise AssertionError(kind)


def _uz_entry(cp: int, cs: bool, contract: bool, comb_marker: bool) -> str:
    """translate() replacement of one character: concatenated (weight, flag) pairs."""
    parts = []
    for kind, value, upper, ucp in char_units(chr(cp)):
        if kind == "apos":
            if contract:
                parts.append(_APOS_PAIR)
        elif kind == "comb":
            if comb_marker:
                parts.append(_COMB_PAIR)
        else:
            if kind == "digit":
                flag = _F_DIGIT
            elif not cs and upper and kind in _LETTER_KINDS:
                flag = _F_UPPER
            else:
                flag = _F_LOWER
            parts.append(chr(_uz_weight(kind, value, upper, ucp, cs)) + flag)
    return "".join(parts)


def _unicode_entry(cp: int, cs: bool) -> str:
    c = chr(cp)
    if cs:
        chars, flag = c, _F_LOWER
    else:
        chars = c.casefold()
        flag = _F_UPPER if unicodedata.category(c) in ("Lu", "Lt") else _F_LOWER
    parts = []
    for d in chars:
        if "0" <= d <= "9":
            parts.append(d + _F_DIGIT)
        elif d == "\x00":  # weights must never contain 0x00 (the level terminator)
            parts.append("\x01" + flag + "\x01" + flag)
        elif d == "\x01":
            parts.append("\x01" + flag + "\x02" + flag)
        else:
            parts.append(d + flag)
    return "".join(parts)


_TABLE_MAX = 32768  # cached code points per table (memory bound for exotic data)


class _LazyTable(dict):
    """``str.translate`` table filled on demand (one entry per distinct code point).

    At most ``_TABLE_MAX`` entries are kept; further characters are computed on every use
    (same result, slower), so a file containing all of Unicode cannot grow it unboundedly.
    """

    __slots__ = ("_build",)

    def __init__(self, build: Callable[[int], str]) -> None:
        super().__init__()
        self._build = build

    def __missing__(self, cp: int) -> str:
        value = self._build(cp)
        if len(self) < _TABLE_MAX:
            self[cp] = value
        return value


_TABLES: dict[tuple[str, bool, bool], _LazyTable] = {}


def _table(collation: str, cs: bool, comb_marker: bool) -> _LazyTable:
    key = (collation, cs, comb_marker)
    table = _TABLES.get(key)
    if table is None:
        if collation == "unicode":
            def build(cp: int, _cs: bool = cs) -> str:
                return _unicode_entry(cp, _cs)
        else:
            def build(cp: int, _cs: bool = cs, _contract: bool = collation == "uz",
                      _comb: bool = comb_marker) -> str:
                return _uz_entry(cp, _cs, _contract, _comb)
        table = _TABLES.setdefault(key, _LazyTable(build))
    return table


# ---------------------------------------------------------------------------------------
# strings: merges, natural runs, levels
# ---------------------------------------------------------------------------------------
def _run_head(n: int) -> str:
    """Primary weights that start a natural digit run with ``n`` significant digits.

    The run marker sorts like the digit ``0`` (between punctuation and letters); the next
    weight is the digit count (one character up to 93 digits, otherwise ``0x7F`` + the count
    of digits of the count + its digits), then the significant digits: numeric order.
    """
    if n < 94:
        return "0" + chr(0x21 + n)
    text = str(n)
    return "0\x7f" + chr(0x21 + len(text)) + text


def _zeros_level(zeros: list[int]) -> bytes:
    """Level 2: leading-zero counts of the digit runs (equal run count when level 1 ties)."""
    if not any(zeros):
        return b"\x01"
    out = [b"\x02"]
    for z in zeros:
        out.append(bytes((z + 1,)) if z < 0xEF else b"\xf0" + z.to_bytes(8, "big"))
    return b"".join(out)


class _PrimaryKit:
    """Constants for merges and digit runs on primary weights, as ``str`` or ``bytes``.

    The ``*_chk`` members are membership needles: a 1-character ``str``, or for ``bytes`` an
    ``int`` (``int in bytes`` is a plain memchr; ``bytes in bytes`` is much slower in CPython
    because it first tries the needle as an integer and handles the resulting exception).
    """

    __slots__ = ("empty", "apos", "apos_chk", "apos_rules", "h_rules", "g_rules", "h_chk", "g_chk",
                 "zero", "run_split", "run_heads", "long_head")

    def __init__(self, cs: bool, as_bytes: bool) -> None:
        def conv(text: str) -> Any:
            return text.encode("latin-1") if as_bytes else text

        def chk(char: str) -> Any:
            return ord(char) if as_bytes else char

        def w(letter: str, upper: int = 0) -> str:
            idx = _LATIN_INDEX[letter]
            return chr(_W_LATIN + (2 * idx + upper if cs else idx))

        cases = (0, 1) if cs else (0,)  # merged letters take the case of their first letter
        self.empty = conv("")
        self.apos = conv(_APOS)
        self.apos_chk = chk(_APOS)
        self.apos_rules = tuple((conv(w(first, u) + _APOS), conv(w(merged, u)))
                                for first, merged in (("o", "oʻ"), ("g", "gʻ")) for u in cases)
        self.h_rules = tuple((conv(w(first, u1) + w("h", u2)), conv(w(merged, u1)))
                             for first, merged in (("s", "sh"), ("c", "ch")) for u1 in cases for u2 in cases)
        self.g_rules = tuple((conv(w("n", u1) + w("g", u2)), conv(w("ng", u1))) for u1 in cases for u2 in cases)
        self.h_chk = tuple(chk(w("h", u)) for u in cases)
        self.g_chk = tuple(chk(w("g", u)) for u in cases)
        self.zero = conv("0")
        self.run_split = re.compile(conv("([0-9]+)")).split
        self.run_heads = tuple(conv(_run_head(n)) for n in range(94))
        self.long_head = lambda n: conv(_run_head(n))


def _make_merger(kit: _PrimaryKit) -> Callable[[Any], Any]:
    """``oʻ gʻ`` first, then ``sh ch``, then ``ng``; finally drop the other apostrophes."""
    apos, apos_chk, empty = kit.apos, kit.apos_chk, kit.empty
    apos_rules, h_rules, g_rules = kit.apos_rules, kit.h_rules, kit.g_rules
    h_chk, g_chk = kit.h_chk, kit.g_chk

    def merge(p: Any) -> Any:
        if apos_chk in p:
            for old, new in apos_rules:
                p = p.replace(old, new)
        for c in h_chk:
            if c in p:
                for old, new in h_rules:
                    p = p.replace(old, new)
                break
        for c in g_chk:
            if c in p:
                for old, new in g_rules:
                    p = p.replace(old, new)
                break
        if apos_chk in p:  # apostrophes that did not form oʻ/gʻ are ignorable
            p = p.replace(apos, empty)
        return p

    return merge


def _make_runs(kit: _PrimaryKit) -> Callable[[Any], tuple[Any, bytes]]:
    """Natural digit runs on primary weights; returns ``(primary, zeros_level)``."""
    run_split, run_heads, long_head = kit.run_split, kit.run_heads, kit.long_head
    zero, empty = kit.zero, kit.empty

    def runs(p: Any) -> tuple[Any, bytes]:
        parts = run_split(p)
        if len(parts) == 1:
            return p, b"\x01"
        zeros = []
        for i in range(1, len(parts), 2):
            run = parts[i]
            stripped = run.lstrip(zero)
            zeros.append(len(run) - len(stripped))
            n = len(stripped)
            parts[i] = (run_heads[n] if n < 94 else long_head(n)) + stripped
        return empty.join(parts), _zeros_level(zeros)

    return runs


def _make_string_payload(collation: str, natural: bool, cs: bool) -> tuple[Callable[[str], bytes],
                                                                           Callable[[bytes], bytes]]:
    """Build the (ascending) string payload functions of one collation configuration.

    Returns ``(payload, payload_ascii)``: ``payload(s)`` for any ``str``; ``payload_ascii(b)``
    for the UTF-8 bytes of a pure-ASCII string without U+0000/U+0001 (same result, faster).
    Both map every character to a primary weight and a case flag (``str.translate`` to
    (weight, flag) pairs, resp. two ``bytes.translate`` passes), then merge letters and
    encode digit runs on the primary weights only.
    """
    table = _table(collation, cs, True)
    uzlike = collation != "unicode"
    nocomb = _table(collation, cs, False) if uzlike else None
    contract = collation == "uz"
    kit_s, kit_b = _PrimaryKit(cs, False), _PrimaryKit(cs, True)
    merge_s, merge_b = _make_merger(kit_s), _make_merger(kit_b)
    runs_s, runs_b = _make_runs(kit_s), _make_runs(kit_b)
    split_case = not cs
    normalize = unicodedata.normalize
    no_zeros = b"\x01" if natural else b""
    apos_s, apos_i = kit_s.apos_chk, kit_b.apos_chk
    digit_i, upper_i = ord(_F_DIGIT), ord(_F_UPPER)
    aposflag_b = _F_APOS.encode("latin-1")

    # ASCII translate tables derived from the very same per-character entries
    wtab, ftab, deleted = bytearray(range(256)), bytearray(256), bytearray()
    for b in range(128):
        entry = table[b]
        if entry == "":
            deleted.append(b)
        elif len(entry) == 2:
            wtab[b], ftab[b] = ord(entry[0]), ord(entry[1])
        # longer entries (unicode U+0000/U+0001) never reach the ASCII path
    wtab_b, ftab_b, deleted_b = bytes(wtab), bytes(ftab), bytes(deleted)

    def payload_ascii(b: bytes) -> bytes:
        w = b.translate(wtab_b, deleted_b)
        f = b.translate(ftab_b, deleted_b)
        if contract:
            if apos_i in w:
                f = f.replace(aposflag_b, b"")
            w = merge_b(w)
        if natural:
            if digit_i in f:
                w, zeros_level = runs_b(w)
            else:
                zeros_level = b"\x01"
        else:
            zeros_level = b""
        if split_case:
            case_level = b"\x02" + f if upper_i in f else b"\x01"
        else:
            case_level = b""
        return b"".join((w, b"\x00", zeros_level, case_level, b, b"\x00"))

    def payload(s: str, fast: bool = True) -> bytes:
        if fast and s.isascii():  # fast=False forces the general path (tests compare both)
            b = s.encode("ascii")
            if 0 not in b and 1 not in b:
                return payload_ascii(b)
        x = s.translate(table)
        if uzlike and _COMB in x:
            x = normalize("NFC", s).translate(nocomb)
        p = x[0::2]
        f = x[1::2]
        if contract:
            if apos_s in p:
                f = f.replace(_F_APOS, "")
            p = merge_s(p)
        if natural:
            if _F_DIGIT in f:
                p, zeros_level = runs_s(p)
            else:
                zeros_level = b"\x01"
        else:
            zeros_level = b""
        if split_case:
            case_level = b"\x02" + f.encode("latin-1") if _F_UPPER in f else b"\x01"
        else:
            case_level = b""
        try:
            pb = p.encode()
        except UnicodeEncodeError:
            pb = p.encode("utf-8", "surrogatepass")
        try:
            ob = s.encode()
        except UnicodeEncodeError:
            ob = s.encode("utf-8", "surrogatepass")
        if 0 in ob or 1 in ob:  # keep the 0x00 terminator unique
            ob = ob.replace(b"\x01", b"\x01\x02").replace(b"\x00", b"\x01\x01")
        return b"".join((pb, b"\x00", zeros_level, case_level, ob, b"\x00"))

    return payload, payload_ascii


_STRING_PAYLOADS: dict[tuple[str, bool, bool], tuple[Callable[[str], bytes], Callable[[bytes], bytes]]] = {}


def _string_payload_fns(collation: str, natural: bool, cs: bool) -> tuple[Callable[[str], bytes],
                                                                          Callable[[bytes], bytes]]:
    key = (collation, natural, cs)
    fns = _STRING_PAYLOADS.get(key)
    if fns is None:
        fns = _STRING_PAYLOADS.setdefault(key, _make_string_payload(collation, natural, cs))
    return fns


def collation_key(s: str, collation: str = "uz", natural: bool = True, case_sensitive: bool = False) -> bytes:
    """Ascending, prefix-free collation key of a string (bytes compare = string order).

    Same rules as string sort keys (module docstring) without the type tag; useful to sort
    object keys alphabetically (AGENT_PROMPT §6.4).
    """
    if collation not in COLLATIONS:
        raise UserInputError("sortkeys.err.bad_collation", value=repr(collation))
    return _string_payload_fns(collation, bool(natural), bool(case_sensitive))[0](s)


# ---------------------------------------------------------------------------------------
# compiled field encoders
# ---------------------------------------------------------------------------------------
_scanstring = json.decoder.scanstring
_WS_BYTES = b" \t\r\n"


def _decode_escaped_string(raw: bytes) -> str:
    """Value of a JSON string token containing escapes (same result as ``codec.loads``)."""
    text = raw.decode("utf-8")
    value, end = _scanstring(text, 1)
    if end != len(text):
        raise ValueError(f"invalid JSON string token: {raw[:40]!r}")
    return value


def _container_payload(value: Any) -> bytes:
    return codec.dumps_transient(value) + b"\x00"     # the concatenation is the compact copy that is kept


class _FieldEncoder:
    """Encoders of one (direction, nulls, collation, natural, case) configuration."""

    __slots__ = ("token", "value", "string", "clear")

    def __init__(self, descending: bool, nulls: str, collation: str, natural: bool, cs: bool) -> None:
        string_payload, ascii_payload = _string_payload_fns(collation, natural, cs)
        desc = descending
        inv = _INV
        if nulls == "last":
            missing_key, null_key = _MISSING_LAST, _NULL_LAST
        else:
            missing_key, null_key = _MISSING_FIRST, _NULL_FIRST
        num_tag = _type_tag(_RANK_NUMBER, desc)
        str_tag = _type_tag(_RANK_STRING, desc)
        bool_tag = _type_tag(_RANK_BOOL, desc)
        arr_tag = _type_tag(_RANK_ARRAY, desc)
        obj_tag = _type_tag(_RANK_OBJECT, desc)
        false_key = bool_tag + (b"\x01".translate(inv) if desc else b"\x01")
        true_key = bool_tag + (b"\x02".translate(inv) if desc else b"\x02")
        tcache: dict[bytes, bytes] = {}
        scache: dict[str, bytes] = {}
        tget = tcache.get
        sget = scache.get
        cache_max = _CACHE_MAX

        def number_key(payload: bytes) -> bytes:
            return num_tag + (payload.translate(inv) if desc else payload)

        def container_key(value: Any) -> bytes:
            payload = _container_payload(value)
            tag = obj_tag if isinstance(value, dict) else arr_tag
            return tag + (payload.translate(inv) if desc else payload)

        def string_key(s: str) -> bytes:
            key = sget(s)
            if key is None:
                payload = string_payload(s)
                key = str_tag + (payload.translate(inv) if desc else payload)
                if len(scache) >= cache_max:
                    scache.clear()
                scache[s] = key
            return key

        def token_uncached(raw: bytes) -> bytes:
            if not raw:
                raise ValueError("empty JSON token")
            c = raw[0]
            if c == 0x22:
                if 0x5C in raw:  # backslash: JSON escapes
                    payload = string_payload(_decode_escaped_string(raw))
                else:
                    inner = raw[1:-1]
                    if len(raw) < 2 or raw[-1] != 0x22 or 0x22 in inner:
                        raise ValueError(f"invalid JSON string token: {raw[:40]!r}")
                    if inner.isascii() and 0 not in inner and 1 not in inner:
                        payload = ascii_payload(inner)
                    else:
                        payload = string_payload(inner.decode("utf-8"))
                return str_tag + (payload.translate(inv) if desc else payload)
            if c == 0x2D or 0x30 <= c <= 0x39:
                payload = _number_payload(raw)
                return num_tag + (payload.translate(inv) if desc else payload)
            if raw == b"true":
                return true_key
            if raw == b"false":
                return false_key
            if raw == b"null":
                return null_key
            if c == 0x5B or c == 0x7B:
                try:
                    value = codec.loads(raw)
                except codec.JsonDecodeError as exc:
                    raise ValueError(f"invalid JSON container token: {exc}") from None
                if not isinstance(value, (list, dict)):
                    raise ValueError(f"invalid JSON container token: {raw[:40]!r}")
                return container_key(value)
            if c in _WS_BYTES:
                stripped = raw.strip(_WS_BYTES)
                if stripped:
                    return token_uncached(stripped)
            raise ValueError(f"invalid JSON token: {raw[:40]!r}")

        def token(raw: bytes | None) -> bytes:
            if raw is None:
                return missing_key
            if raw.__class__ is not bytes:
                raw = bytes(raw)
            key = tget(raw)
            if key is None:
                key = token_uncached(raw)
                if len(tcache) >= cache_max:
                    tcache.clear()
                tcache[raw] = key
            return key

        def value_slow(v: Any) -> bytes:
            if v is MISSING:
                return missing_key
            if isinstance(v, bool):
                return true_key if v else false_key
            if isinstance(v, (JsonFloat, JsonInt)):
                return number_key(_number_payload(v.raw.encode("ascii")))
            if isinstance(v, str):
                return string_key(str.__str__(v))
            if isinstance(v, int):
                return number_key(_int_payload(int(v)))
            if isinstance(v, float):
                return number_key(_float_payload(float(v)))
            if isinstance(v, Decimal):
                return number_key(_decimal_payload(v))
            if isinstance(v, (list, tuple, dict)):
                return container_key(v)
            raise TypeError(f"cannot build a sort key from {type(v).__name__}")

        def value(v: Any) -> bytes:
            cls = v.__class__
            if cls is str:
                return string_key(v)
            if v is None:
                return null_key
            if cls is int:
                return number_key(_int_payload(v))
            if cls is float:
                return number_key(_float_payload(v))
            if cls is bool:
                return true_key if v else false_key
            if cls is JsonFloat or cls is JsonInt:
                return number_key(_number_payload(v.raw.encode("ascii")))
            return value_slow(v)

        def clear() -> None:
            tcache.clear()
            scache.clear()

        self.token = token
        self.value = value
        self.string = string_key
        self.clear = clear


_ENCODERS: dict[tuple[bool, str, str, bool, bool], _FieldEncoder] = {}


def _encoder(descending: bool, nulls: str, collation: str, natural: bool, cs: bool) -> _FieldEncoder:
    key = (descending, nulls, collation, natural, cs)
    enc = _ENCODERS.get(key)
    if enc is None:
        enc = _ENCODERS.setdefault(key, _FieldEncoder(descending, nulls, collation, natural, cs))
    return enc


def clear_caches() -> None:
    """Drop the memoised field keys of every encoder (character tables are kept)."""
    for enc in list(_ENCODERS.values()):
        enc.clear()


# ---------------------------------------------------------------------------------------
# SortKey
# ---------------------------------------------------------------------------------------
@dataclass(frozen=True)
class SortKey:
    """One sort field.

    * ``path``: a :mod:`jsonreshape.paths` pattern (``name``, ``contact.address.city``,
      ``tags[0]``, ``orders[*].sum``, ``$`` = the record value itself), or ``"$key"`` (the
      member key of a keyed record; missing for other formats) or ``"$index"`` (the record
      number).  A field literally named ``$key``/``$index`` is written ``["$key"]``.
      Wildcard patterns use the **first** matching value in document order (missing when
      nothing matches); ``**``/``..`` (all nested levels) are not allowed.
    * ``descending``: reverse order of present values (null/missing placement unchanged).
    * ``nulls``: ``"last"`` (default: values, null, missing) or ``"first"`` (missing, null,
      values).
    * ``natural``: digit runs inside strings compare by numeric value.
    * ``collation``: ``"uz"`` (default), ``"simple"`` or ``"unicode"`` (module docstring).
    * ``case_sensitive``: case significant at the primary level (``a < A < b < B``).

    Instances are immutable and hashable; invalid arguments raise :class:`UserInputError`
    (a bad path raises :class:`~jsonreshape.errors.PathSyntaxError`).
    """

    path: str
    descending: bool = False
    nulls: str = "last"
    natural: bool = True
    collation: str = "uz"
    case_sensitive: bool = False

    def __post_init__(self) -> None:
        if not isinstance(self.path, str):
            raise UserInputError("sortkeys.err.path_type", value=repr(self.path))
        path = self.path.strip()
        if not path:
            raise UserInputError("sortkeys.err.empty_field")
        for name in ("descending", "natural", "case_sensitive"):
            flag = getattr(self, name)
            if not isinstance(flag, bool):
                raise UserInputError("sortkeys.err.bad_flag", name=name, value=repr(flag))
        nulls = self.nulls.strip().lower() if isinstance(self.nulls, str) else None
        if nulls not in NULLS_POSITIONS:
            raise UserInputError("sortkeys.err.bad_nulls", value=repr(self.nulls))
        collation = self.collation.strip().lower() if isinstance(self.collation, str) else None
        if collation not in COLLATIONS:
            raise UserInputError("sortkeys.err.bad_collation", value=repr(self.collation))
        if path == KEY_FIELD:
            kind, pattern = "key", None
        elif path == INDEX_FIELD:
            kind, pattern = "index", None
        else:
            kind = "value"
            pattern = parse_pattern(path)
            if Wild.DESCENDANTS in pattern.segments:
                raise UserInputError("sortkeys.err.descendants", path=path)
        setattr_ = object.__setattr__
        setattr_(self, "path", path)
        setattr_(self, "nulls", nulls)
        setattr_(self, "collation", collation)
        setattr_(self, "_kind", kind)
        setattr_(self, "_pattern", pattern)
        setattr_(self, "_enc", _encoder(self.descending, nulls, collation, self.natural, self.case_sensitive))

    def __reduce__(self):
        return (type(self), (self.path, self.descending, self.nulls, self.natural, self.collation,
                             self.case_sensitive))

    # -- construction from CLI/GUI text ------------------------------------------------
    @classmethod
    def parse(cls, text: str, **options: Any) -> SortKey:
        """``"name"`` / ``"+name"`` ascending, ``"-name"`` descending (CLI ``--by -id``).

        ``options`` are the other fields (``nulls=``, ``collation=``, ...); ``descending=True``
        in ``options`` makes the field descending as well.  A field whose name starts with
        ``-``/``+`` must be quoted: ``["-x"]``.
        """
        if not isinstance(text, str):
            raise UserInputError("sortkeys.err.path_type", value=repr(text))
        body = text.strip()
        descending = options.pop("descending", False)  # validated by __post_init__
        if body[:1] == "-":
            descending = True
            body = body[1:].strip()
        elif body[:1] == "+":
            body = body[1:].strip()
        if not body:
            raise UserInputError("sortkeys.err.empty_field")
        return cls(body, descending=descending, **options)

    def to_text(self) -> str:
        """Inverse of :meth:`parse` (``"-id"`` for a descending ``id``)."""
        path = self.path
        if path[:1] in ("-", "+") and self._pattern is not None:
            path = format_path(self._pattern.segments, "python")
        return "-" + path if self.descending else path

    def describe(self) -> str:
        """Localized one-line description, e.g. ``name (o'sish tartibida)``."""
        direction = t("sortkeys.dir.desc" if self.descending else "sortkeys.dir.asc")
        return t("sortkeys.describe", path=self.path, direction=direction)

    # -- where the value comes from ------------------------------------------------------
    @property
    def kind(self) -> str:
        """``"value"`` (a path inside the record), ``"key"`` (``$key``) or ``"index"``."""
        return self._kind  # type: ignore[attr-defined]

    @property
    def pattern(self) -> PathPattern | None:
        """The parsed path (``None`` for ``$key`` / ``$index``)."""
        return self._pattern  # type: ignore[attr-defined]

    def core_path(self) -> list[tuple] | None:
        """Segments for ``core.PathExtractor`` (``None`` for ``$key`` / ``$index``)."""
        pattern = self._pattern  # type: ignore[attr-defined]
        return None if pattern is None else pattern.to_core()

    def extract(self, value: Any, key: str | None = None, index: int | None = None) -> Any:
        """The field value of a parsed record (``paths.MISSING`` when absent).

        ``key``: the member key of a keyed record; ``index``: the record number.
        """
        kind = self._kind  # type: ignore[attr-defined]
        if kind == "key":
            return MISSING if key is None else key
        if kind == "index":
            return MISSING if index is None else index
        pattern: PathPattern = self._pattern  # type: ignore[attr-defined]
        if pattern.is_concrete:
            return path_get(value, pattern.segments)  # type: ignore[arg-type]
        values = pattern.values(value)
        return values[0] if values else MISSING

    # -- keys -------------------------------------------------------------------------
    def encode_token(self, raw: bytes | None) -> bytes:
        """Same as :func:`encode_token` ``(raw, self)``."""
        return self._enc.token(raw)  # type: ignore[attr-defined]

    def encode_value(self, value: Any) -> bytes:
        """Same as :func:`encode_value` ``(value, self)``."""
        return self._enc.value(value)  # type: ignore[attr-defined]


def encode_token(raw: bytes | bytearray | memoryview | None, spec: SortKey) -> bytes:
    """Field key from the raw JSON token bytes of the value (``None`` = missing).

    ``raw`` is one complete JSON value as produced by ``core.PathExtractor`` (surrounding
    whitespace is tolerated).  Raises ``ValueError`` when it is not valid JSON.
    """
    return spec._enc.token(raw)  # type: ignore[attr-defined]


def encode_value(value: Any, spec: SortKey) -> bytes:
    """Field key from a Python value (codec types, ``Decimal``; ``paths.MISSING`` = missing).

    Raises ``TypeError`` for values that are not JSON values.
    """
    return spec._enc.value(value)  # type: ignore[attr-defined]


def token_encoder(spec: SortKey) -> Callable[[bytes | None], bytes]:
    """The fast ``raw -> key`` function of ``spec`` (avoids a call level in hot loops)."""
    return spec._enc.token  # type: ignore[attr-defined]


def value_encoder(spec: SortKey) -> Callable[[Any], bytes]:
    """The fast ``value -> key`` function of ``spec``."""
    return spec._enc.value  # type: ignore[attr-defined]


def compose(field_keys: Sequence[bytes], specs: Sequence[SortKey]) -> bytes:
    """Concatenate the field keys of one record (one key per spec, in priority order).

    Field keys from :func:`encode_token`/:func:`encode_value` already carry direction and
    null placement and are prefix-free, so bytewise comparison of the composed keys gives
    the multi-field order.  Append the record number (8 bytes big-endian) for stability.
    """
    if len(field_keys) != len(specs):
        raise ValueError(f"compose: {len(field_keys)} field keys for {len(specs)} sort keys")
    if len(field_keys) == 1:
        return bytes(field_keys[0])
    return b"".join(field_keys)


def record_key(value: Any, specs: Sequence[SortKey], *, key: str | None = None, index: int | None = None) -> bytes:
    """Composed key of a parsed record (``key``: keyed-record key, ``index``: record number)."""
    return b"".join(spec._enc.value(spec.extract(value, key, index)) for spec in specs)  # type: ignore[attr-defined]


def describe_keys(specs: Sequence[SortKey]) -> str:
    """Localized description of several sort keys, e.g. ``name (o'sish tartibida), ...``."""
    return ", ".join(spec.describe() for spec in specs)
