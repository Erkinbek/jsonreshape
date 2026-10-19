"""Text matching for the full-file search (docs/ARCHITECTURE.md §7, AGENT_PROMPT.md §6.2).

:class:`TextQuery` decides whether, and where, a string matches the user's search text.
``search.py`` calls it for object keys, string values and the JSON text of numbers /
``true`` / ``false`` / ``null`` (the Uzbek conveniences apply to keys as well), and asks
it for byte *needles* for the fast prefilter (:class:`jsonreshape.core.MultiFinder`).

Modes
=====

``plain``
    Substring search after folding the needle and the haystack with :func:`fold`:

    * full Unicode case folding (:meth:`str.casefold`) unless ``case_sensitive``;
    * the apostrophe variants U+0027 ``'``, U+2018 ``‘``, U+2019 ``’``, U+02BB ``ʻ``,
      U+02BC ``ʼ``, U+0060 (grave accent) and U+00B4 ``´`` are always equal, also when
      case-sensitive (they all fold to ``'``);
    * ``translit=True`` additionally folds Uzbek Cyrillic to Uzbek Latin (rules below), so
      "Toshkent" ~ "Тошкент" and "O'zbekiston" ~ "Oʻzbekiston" ~ "Ўзбекистон".

    Matches are ``(start, end)`` spans of the *original* string (``end`` exclusive),
    non-overlapping, found left to right (leftmost first; on a tie between two needle
    alternatives the longer one). A match must begin and end on boundaries of original
    characters, i.e. it never starts or ends inside the folded expansion of one character:
    ``"ss"`` and ``"strasse"`` match ``"ß"`` / ``"Straße"`` but ``"s"`` does not match
    ``"ß"``; with ``translit`` ``"sh"`` matches ``"ш"`` but ``"s"`` does not. Case folding
    is exactly :meth:`str.casefold`: ``"İ"`` (U+0130) folds to ``"i̇"`` (``i`` + U+0307),
    so ``"İstanbul"`` matches ``"İSTANBUL"`` and ``"i̇stanbul"`` but ``"istanbul"`` does
    not match ``"İstanbul"``; Greek final sigma folds to ``σ``. No Unicode normalisation
    (NFC/NFD) is applied.

``regex``
    :func:`re.finditer` on the original string (``re.IGNORECASE`` unless case-sensitive),
    no folding at all. Empty matches are reported as empty spans (a pattern that matches
    the empty string matches every string, as with :func:`re.search`). An invalid pattern
    raises :class:`~jsonreshape.errors.QuerySyntaxError` (``pos`` = 0-based offset).

``number``
    The text is a comparison: ``> 1000``, ``>= 5``, ``< -2.5``, ``<= 7``, ``= 3`` (or
    ``== 3``), ``!= 0``, ``1000..2000`` (inclusive range; reversed bounds are swapped) or a
    bare number (equality). ``≥ ≤ ≠`` and ``=> =< <>`` are accepted as well; numbers may be
    written as ``+5``, ``.5``, ``5.``, ``1e3`` and with the Unicode minus ``−`` (a dot
    directly followed by another dot never belongs to a number, so ``1...2`` reads as
    ``1 .. .2``; whitespace around ``..`` is allowed).
    :meth:`TextQuery.match_number` compares the exact decimal value of a number text
    (``1.10 == 1.1``, ``-0 == 0``, ``1e400``, ``12345678901234567890``; also exponents far
    beyond :mod:`decimal`'s range). :meth:`TextQuery.find_spans` returns ``[(0, len(s))]``
    when ``s`` is a number text that satisfies the comparison, else ``[]``. The same exact
    comparison is public as :func:`number_key` / :func:`compare_numbers` (the filter
    language of :mod:`jsonreshape.query` uses it, so filter, search and sort agree).

``case_sensitive`` and ``whole_word`` apply to plain and regex mode, ``translit`` only to
plain mode; number mode ignores all three. An empty search text raises
:class:`~jsonreshape.errors.UserInputError` in every mode.

``find_spans(s, reach=N)`` returns only the spans from the first match up to the first
span that starts more than ``N`` characters after the first match's start (the spans a
snippet around the first match can show; the full-file search uses it so that a string
with millions of matches costs no memory). Strings longer than :data:`HUGE_TEXT`
characters are matched window by window (plain mode folds :data:`WINDOW_CHARS` characters
at a time instead of building folded copies of the whole string; regex mode reads matches
lazily); the result is the same.

Whole word
==========
With ``whole_word=True`` (plain and regex mode) a match counts only when the character
before it and the character after it (in the original string) are not *word characters*.
Word character at index ``i``:

* an apostrophe variant is a word character only when it sits between two *letters*
  (``s[i-1]`` and ``s[i+1]``), where a letter is an alphanumeric character that is not a
  decimal digit and not an apostrophe variant (regex ``[^\\W\\d_]``);
* any other character is a word character when ``str.isalnum()`` or it is ``"_"``
  (exactly the regex class ``\\w``).

So ``"O"`` is not a whole word in ``"O'zbekiston"`` while ``"Toshkent"`` is one in
``"'Toshkent'"`` and ``"ʻToshkentʼ"``. In regex mode the same rule is compiled into
look-around guards around the pattern, so alternatives are retried correctly (a pattern
the guards cannot wrap is matched as it is and its matches are filtered instead:
:attr:`TextQuery.regex_postfilter`). The rule is public as :func:`is_whole_word` /
:func:`is_word_char`, the pattern of a regex query as :attr:`TextQuery.regex`.

Latin <-> Cyrillic folding (``translit=True``)
===============================================
Uzbek Cyrillic letters become Uzbek Latin:

    а a, б b, в v, г g, д d, е e/ye, ё yo, ж j, з z, и i, й y, к k, л l, м m, н n, о o,
    п p, р r, с s, т t, у u, ф f, х x, ц s, ч ch, ш sh, щ sh, ъ ʼ, ы i, ь (dropped),
    э e, ю yu, я ya, ў oʻ, қ q, ғ gʻ, ҳ h.

* ``е`` becomes ``ye`` at the start of a word (no previous character, or the previous
  character is not a letter as defined above, or it is an apostrophe variant) and after a
  Cyrillic vowel (а е ё и о у ы э ю я ў) or ``ъ``/``ь``; otherwise ``e``.
* ``ts`` folds to ``s`` (a fuzzy rule): after transliteration, every ``t`` directly
  followed by ``s`` is merged with it into one ``s`` -- Latin ``ts`` and ``тс`` alike.
  The official Latin spelling writes ``ц`` as ``s`` at the start of a word and after
  consonants and as ``ts`` after vowels, and the Russian-style ``ts`` spellings are
  common as well; with ``ц`` -> ``s`` and this rule both spellings match in both
  directions: ``sirk`` / ``tsirk`` ~ ``цирк``, ``konsert`` / ``kontsert`` ~ ``концерт``,
  ``stansiya`` / ``stantsiya`` ~ ``станция``, ``milisiya`` / ``militsiya`` ~ ``милиция``.
  Latin text is folded the same way (``sex`` also finds ``tsex``, ``bos`` finds
  ``bots``). The merged ``s`` maps to its ``t`` (the ``s`` has no entry of its own) and
  a match containing it covers both characters; a needle ending in ``t`` also matches the
  ``t`` of a merged pair (``kont`` and ``Toshkent`` are found in ``kontsert`` and
  ``Toshkentskaya``; the match ends after the ``t``). Case-sensitive, only ``ts`` -> ``s``
  and ``TS`` -> ``S`` are merged: a pair of mixed case such as ``Ts`` stays (so there
  ``Tsirk`` does not find ``Цирк``, while ``Sirk`` and ``TSIRK`` / ``ЦИРК`` do).
* ``ъ`` directly after ``ў``, ``ғ`` or ``қ`` is dropped (the Latin letter already carries
  the sign): ``мўъжиза`` -> ``moʻjiza``, ``мўътадил`` -> ``moʻtadil``.
* An apostrophe between ``s`` and ``h`` (Latin ``s``/``h`` or Cyrillic ``с``/``ҳ``, any
  case) is dropped, so the separator of the official spelling is optional: ``Isʼhoq``,
  ``Ishoq`` and ``Исҳоқ`` all fold to ``ishoq`` (``сҳ`` is ``sh`` like ``ш``).
* The produced apostrophes (ʼ for ``ъ``, ʻ for ``ў``/``ғ``) fold to ``'`` like every
  other apostrophe when apostrophe folding is on (always, in plain mode).
* All context rules look at the text after case folding and apostrophe folding, and only
  at the directly neighbouring characters.
* Uppercase Cyrillic (only present when case-sensitive) keeps its case: a one-letter
  result is uppercased; a multi-letter result is all caps (``"SH"``) when the next
  character is an uppercase letter, or when the next character is not a cased letter and
  the previous character is uppercase; otherwise it is title case (``"Sh"``). So
  ``"ШАҲРИСАБЗ"`` folds to ``"SHAHRISABZ"`` and ``"Шаҳрисабз"`` to ``"Shahrisabz"``.
* Characters outside the table (Latin, digits, other Cyrillic letters, ...) are kept.

Because the needle's first and last characters have no context of their own, the needle
is folded in every relevant context (start of word, after a vowel, after a consonant,
before upper/lower case letters, ...) and all distinct results ("alternatives") are
searched; e.g. ``"ер"`` is searched as ``"yer"`` and ``"er"``. A needle that folds to
nothing (only ``ь``) is searched without transliteration. Characters that fold to nothing (``ь``, a dropped ``ъ`` or apostrophe)
are never part of a match on their own: a needle consisting only of ``ъ`` does not find
the ``ъ`` of ``мўъжиза``.

:func:`fold` returns the folded text and an index map (``index_map[i]`` = index in ``s``
of the character that produced ``folded[i]``, plus a final sentinel ``len(s)``).

Prefilter needles
=================
:meth:`TextQuery.prefilter_needles` returns byte strings such that **every** string for
which :meth:`TextQuery.find_spans` is non-empty contains at least one of them in its JSON
encoding, for any standard JSON writer that uses no escapes other than the mandatory ones
(``\\"``, ``\\\\``, ``\\b \\f \\n \\r \\t``, ``\\u00XX`` for other control characters) when
``flags`` lacks ``FLAG_ESCAPES``, may additionally write ``/`` as ``\\/`` when ``flags`` has
``FLAG_ESCAPES``, and uses no ``\\u`` escapes when ``flags`` lacks ``FLAG_U_ESCAPES``.
Number / literal tokens appear verbatim in the bytes, so the same needles cover them.

It returns ``None`` (the caller parses every record) for regex and number mode, with
``translit``, when ``flags`` has ``FLAG_U_ESCAPES``, and when no needle can be built
(e.g. the needle consists only of lone surrogates). Construction:

1. Fold the needle (``n``). Split it at apostrophes (and lone surrogates).
2. For each character ``c`` the *case variants* are every code point ``x`` with
   ``x.casefold() == c`` (lazily built inverse map over all code points); when
   case-sensitive only ``c`` itself. The variants ``c`` and ``c.upper()`` are *product*
   variants; the other (exotic) ones, e.g. ``ſ`` for ``s`` or the Kelvin sign for ``k``,
   become *standalone* needles.
3. Every variant is JSON-encoded the way a writer may write it (above), so ``"`` becomes
   ``\\"`` and, with ``FLAG_ESCAPES``, ``/`` gives both ``/`` and ``\\/``.
4. The anchor is the longest apostrophe-free substring of ``n`` whose number of encoded
   product combinations is at most :data:`MAX_ANCHOR_VARIANTS` (256); ties prefer fewer
   combinations, then the leftmost.
5. Needles = all product combinations of the anchor + the standalone variants of its
   characters + (case-insensitive only) the encoding of every *expanding* character ``x``
   (``len(x.casefold()) > 1``) whose folding contains an anchor character (``ß`` for an
   ``s``, ``ﬁ`` for an ``f`` or ``i``, ...).
6. A needle made only of apostrophes uses the apostrophe variants themselves (plus ``ŉ``,
   whose folding contains ``ʼ``, when case-insensitive).

Soundness: an aligned match of ``n`` puts every anchor character in the folded haystack;
each comes from one original character, which is either expanding (then it is a needle
itself), an exotic variant (a standalone needle) or a product variant; if all are product
variants they are consecutive original characters and their encoding is one of the
product needles.
"""

from __future__ import annotations

import bisect
import itertools
import logging
import re
import threading
from typing import Iterator, NamedTuple

from .errors import QuerySyntaxError, UserInputError
from .i18n import t

try:  # the flag values live in core.constants (mirrored by the Rust extension)
    from .core.constants import FLAG_ESCAPES, FLAG_U_ESCAPES
except ImportError:  # pragma: no cover - core package not importable (no backend built yet)
    FLAG_ESCAPES = 1
    FLAG_U_ESCAPES = 2

log = logging.getLogger(__name__)

__all__ = [
    "APOSTROPHES",
    "CANONICAL_APOSTROPHE",
    "HUGE_TEXT",
    "MAX_ANCHOR_VARIANTS",
    "MODES",
    "NumKey",
    "TextQuery",
    "WINDOW_CHARS",
    "compare_numbers",
    "fold",
    "is_whole_word",
    "is_word_char",
    "number_key",
    "regex_error_text",
    "translit_fold_ascii",
]

MODES = ("plain", "regex", "number")

HUGE_TEXT = 1 << 20
"""Strings longer than this (characters) are matched window by window (module docstring)."""

WINDOW_CHARS = 1 << 18
"""Characters folded at a time when matching a huge string in plain mode."""

APOSTROPHES = (
    "'"
    "\N{LEFT SINGLE QUOTATION MARK}"
    "\N{RIGHT SINGLE QUOTATION MARK}"
    "\N{MODIFIER LETTER TURNED COMMA}"
    "\N{MODIFIER LETTER APOSTROPHE}"
    "`"
    "\N{ACUTE ACCENT}"
)
"""Apostrophe variants that are always equal in plain mode: ' ‘ ’ ʻ ʼ ` ´
(U+0027 U+2018 U+2019 U+02BB U+02BC U+0060 U+00B4)."""

CANONICAL_APOSTROPHE = "'"
"""What every apostrophe variant folds to."""

MAX_ANCHOR_VARIANTS = 256
"""Upper bound on the number of encoded case combinations of the prefilter anchor."""

_APOS_SET = frozenset(APOSTROPHES)
_APOS_TABLE = str.maketrans({c: CANONICAL_APOSTROPHE for c in APOSTROPHES if c != CANONICAL_APOSTROPHE})
_APOS_CLASS = "[" + re.escape(APOSTROPHES) + "]"

# ----------------------------------------------------------------------------------
# Latin <-> Cyrillic
# ----------------------------------------------------------------------------------
_OKINA = "\N{MODIFIER LETTER TURNED COMMA}"  # oʻ, gʻ
_TUTUQ = "\N{MODIFIER LETTER APOSTROPHE}"  # tutuq belgisi = Cyrillic ъ
_CYR_E = "\N{CYRILLIC SMALL LETTER IE}"
_CYR_HARD = "\N{CYRILLIC SMALL LETTER HARD SIGN}"

_CYRILLIC_TO_LATIN: dict[str, str] = {
    "а": "a",
    "б": "b",
    "в": "v",
    "г": "g",
    "д": "d",
    "е": "e",  # "ye" in the contexts handled by _ye_context()
    "ё": "yo",
    "ж": "j",
    "з": "z",
    "и": "i",
    "й": "y",
    "к": "k",
    "л": "l",
    "м": "m",
    "н": "n",
    "о": "o",
    "п": "p",
    "р": "r",
    "с": "s",
    "т": "t",
    "у": "u",
    "ф": "f",
    "х": "x",
    "ц": "s",  # official "s" / "ts" (after a vowel): Latin "ts" folds to "s" as well (_merge_ts)
    "ч": "ch",
    "ш": "sh",
    "щ": "sh",
    "ъ": _TUTUQ,
    "ы": "i",
    "ь": "",
    "э": "e",
    "ю": "yu",
    "я": "ya",
    "ў": "o" + _OKINA,
    "қ": "q",
    "ғ": "g" + _OKINA,
    "ҳ": "h",
}
_YE_CONTEXT_LOWER = "аеёиоуыэюяўъь"
_YE_CONTEXT = frozenset(_YE_CONTEXT_LOWER + _YE_CONTEXT_LOWER.upper())
_HARD_DROP_AFTER = frozenset("ўғқЎҒҚ")  # "ъ" after these is dropped
_SH_BEFORE = frozenset("sSсС")  # an apostrophe between these ...
_SH_AFTER = frozenset("hHҳҲ")  # ... and these is dropped
_UPPER_TO_LOWER: dict[str, str] = {k.upper(): k for k in _CYRILLIC_TO_LATIN}


def _translit_map(apostrophes: bool) -> dict[str, str]:
    if apostrophes:
        return {k: v.translate(_APOS_TABLE) for k, v in _CYRILLIC_TO_LATIN.items()}
    return dict(_CYRILLIC_TO_LATIN)


_TR_MAP: dict[bool, dict[str, str]] = {True: _translit_map(True), False: _translit_map(False)}
_TR_TRANSLATE = {flag: str.maketrans({k: (v or None) for k, v in m.items()}) for flag, m in _TR_MAP.items()}
# text that transliteration changes: Cyrillic letters, or an apostrophe between s and h
_TR_CHARS_RE = re.compile(
    "[" + "".join(_CYRILLIC_TO_LATIN) + "".join(_UPPER_TO_LOWER) + "]"
    "|(?<=[" + "".join(sorted(_SH_BEFORE)) + "])" + _APOS_CLASS + "(?=[" + "".join(sorted(_SH_AFTER)) + "])"
)
# the context rules on case-folded text (only lower-case Cyrillic left), one pass:
# "е" in a "ye" context (_ye_context), "ъ" after ў/ғ/қ, and an apostrophe between s and h
_CTX_LOWER_RE = re.compile(
    "(?:(?<![^\\W\\d_])|(?<=[" + re.escape(APOSTROPHES) + "".join(sorted(_YE_CONTEXT)) + "]))" + _CYR_E
    + "|(?<=[ўғқ])" + _CYR_HARD
    + "|(?<=[sс])" + _APOS_CLASS + "(?=[hҳ])"
)
_CTX_LOWER_REPL = {_CYR_E: "ye"}  # anything else matched is dropped


def _ctx_lower_sub(m: re.Match[str]) -> str:
    return _CTX_LOWER_REPL.get(m.group(), "")


# characters that may need a context rule (stage-1 text with apostrophe folding only
# holds the canonical apostrophe)
_CTX_LOWER_CHARS = {True: (_CYR_E, _CYR_HARD, CANONICAL_APOSTROPHE),
                    False: (_CYR_E, _CYR_HARD) + tuple(APOSTROPHES)}

# Latin "ts" folds to "s" after transliteration (module docstring): case-folded text only
# holds "ts"; case-sensitive text merges "ts" and "TS" (a mixed pair such as "Ts" stays:
# its merged letter could not keep the case of both letters)
_TS_PAIR = {True: re.compile("ts"), False: re.compile("ts|TS")}


def _ts_merged(m: re.Match[str]) -> str:
    return "S" if m.group()[0] == "T" else "s"


def _merge_ts_text(text: str, casefold: bool) -> str:
    """Every ``t`` directly followed by ``s`` (of the same case) is merged with it into one
    ``s``."""
    if casefold:
        return text.replace("ts", "s") if "ts" in text else text
    return _TS_PAIR[False].sub(_ts_merged, text)


def _merge_ts(text: str, imap: list[int] | None, casefold: bool) -> tuple[str, list[int] | None,
                                                                          dict[int, int] | None]:
    """:func:`_merge_ts_text` with the index map (``None``: the identity; no sentinel yet).
    The merged ``s`` maps to the source of its ``t``; ``merged`` (``None`` when nothing was
    merged) maps the folded index of every merged ``s`` to the source of the ``s``."""
    starts = [m.start() for m in _TS_PAIR[casefold].finditer(text)]
    if not starts:
        return text, imap, None
    if imap is None:
        imap = list(range(len(text)))
    pieces: list[str] = []
    out_map: list[int] = []
    merged: dict[int, int] = {}
    pos = 0
    for i in starts:
        pieces.append(text[pos:i])
        out_map.extend(imap[pos:i])
        merged[len(out_map)] = imap[i + 1]
        out_map.append(imap[i])
        pieces.append("S" if text[i] == "T" else "s")
        pos = i + 2
    pieces.append(text[pos:])
    out_map.extend(imap[pos:])
    return "".join(pieces), out_map, merged

# needle contexts used to enumerate the foldings of the needle's first / last character
_CONTEXT_BEFORE = ("", " ", "'", "1", "a", "а", "А", "б", "Б", "S", "ў")
_CONTEXT_AFTER = ("", " ", "'", "1", "б", "Б", "h", "H")


def _is_letter(c: str) -> bool:
    """Alphanumeric, not a decimal digit, not an apostrophe variant (regex ``[^\\W\\d_]``)."""
    return c not in _APOS_SET and c.isalnum() and not c.isdecimal()


def _ye_context(text: str, j: int) -> bool:
    """True when the Cyrillic ``е`` at ``text[j]`` is pronounced/written ``ye``."""
    if j == 0:
        return True
    p = text[j - 1]
    return p in _YE_CONTEXT or not _is_letter(p)


def _sh_apostrophe(text: str, j: int) -> bool:
    """True when the apostrophe at ``text[j]`` sits between ``s`` and ``h`` (dropped)."""
    return 0 < j < len(text) - 1 and text[j - 1] in _SH_BEFORE and text[j + 1] in _SH_AFTER


def _cyrillic_lower(text: str, j: int, lower: str, table: dict[str, str]) -> str:
    """Latin text of the lower-case Cyrillic letter ``lower`` found at ``text[j]``
    (context rules applied)."""
    if lower == _CYR_E:
        return "ye" if _ye_context(text, j) else table[lower]
    if lower == _CYR_HARD and j > 0 and text[j - 1] in _HARD_DROP_AFTER:
        return ""
    return table[lower]


def _all_caps(text: str, j: int, n: int) -> bool:
    if j + 1 < n:
        q = text[j + 1]
        if q.isupper():
            return True
        if q.islower():
            return False
    return j > 0 and text[j - 1].isupper()


def _translit_char(text: str, j: int, n: int, table: dict[str, str]) -> str:
    ch = text[j]
    if ch in table:  # lower-case Cyrillic
        return _cyrillic_lower(text, j, ch, table)
    lower = _UPPER_TO_LOWER.get(ch)
    if lower is None:
        return ch
    lat = _cyrillic_lower(text, j, lower, table)
    if len(lat) <= 1:
        return lat.upper()
    tail = lat[1:]
    if _all_caps(text, j, n):
        tail = tail.upper()
    return lat[0].upper() + tail


def _translit_with_map(text: str, apostrophes: bool) -> tuple[str, list[int] | None]:
    """Transliterate stage-1 text; the map is ``None`` when nothing changed."""
    if _TR_CHARS_RE.search(text) is None:
        return text, None
    table = _TR_MAP[apostrophes]
    n = len(text)
    out: list[str] = []
    imap: list[int] = []
    for j, ch in enumerate(text):
        piece = table.get(ch)
        if piece is None:  # not lower-case Cyrillic: upper-case letters need context
            if ch in _UPPER_TO_LOWER:
                piece = _translit_char(text, j, n, table)
            elif ch in _APOS_SET and _sh_apostrophe(text, j):
                piece = ""
            else:
                piece = ch
        elif ch == _CYR_E or ch == _CYR_HARD:
            piece = _cyrillic_lower(text, j, ch, table)
        k = len(piece)
        if k == 1:
            out.append(piece)
            imap.append(j)
        elif k:
            out.append(piece)
            imap.extend([j] * k)
    return "".join(out), imap


def _translit_text_lower(text: str, apostrophes: bool) -> str:
    """Fast transliteration of *case-folded* stage-1 text (no upper-case Cyrillic left)."""
    for c in _CTX_LOWER_CHARS[apostrophes]:
        if c in text:
            text = _CTX_LOWER_RE.sub(_ctx_lower_sub, text)
            break
    return text.translate(_TR_TRANSLATE[apostrophes])


_ASCII_SH_RE = re.compile("(?<=[sS])['`](?=[hH])")


def translit_fold_ascii(s: str, casefold: bool = True) -> str:
    """``fold(s, casefold=casefold, translit=True)[0]`` for an ASCII string ``s`` (a
    cheaper path: ASCII text has no Cyrillic letters, so only case folding, apostrophe
    folding, the dropped apostrophe between ``s`` and ``h`` and ``ts`` -> ``s`` apply)."""
    t = s.lower() if casefold else s
    if "'" in t or "`" in t:
        t = _ASCII_SH_RE.sub("", t).replace("`", "'")
    return _merge_ts_text(t, casefold)


# ----------------------------------------------------------------------------------
# folding
# ----------------------------------------------------------------------------------
def _expansion_map(s: str) -> list[int]:
    imap: list[int] = []
    for i, ch in enumerate(s):
        k = len(ch.casefold())
        if k == 1:
            imap.append(i)
        else:
            imap.extend([i] * k)
    return imap


def _fold_text(s: str, casefold: bool, apostrophes: bool, translit: bool) -> str:
    """The folded text only (fast path; equal to ``_fold_map(...)[0]``)."""
    text = s.casefold() if casefold else s
    if apostrophes:
        text = text.translate(_APOS_TABLE)
    if translit:
        if casefold:
            text = _translit_text_lower(text, apostrophes)
        else:
            text = _translit_with_map(text, apostrophes)[0]
        return _merge_ts_text(text, casefold)
    return text


def _fold_map(s: str, casefold: bool, apostrophes: bool,
              translit: bool) -> tuple[str, list[int] | None, dict[int, int] | None]:
    """Folded text, index map *with* sentinel (``None`` for the identity map) and the
    merged ``ts`` (``None`` when there are none): folded index of the ``s`` -> index in
    ``s`` of the original ``s`` (see :func:`_merge_ts`; the map holds its ``t``)."""
    if casefold:
        text = s.casefold()
        m0 = None if len(text) == len(s) else _expansion_map(s)
    else:
        text, m0 = s, None
    if apostrophes:
        text = text.translate(_APOS_TABLE)
    m: list[int] | None = m0
    merged: dict[int, int] | None = None
    if translit:
        text, m1 = _translit_with_map(text, apostrophes)
        if m1 is not None:
            m = m1 if m0 is None else [m0[j] for j in m1]
        text, m, merged = _merge_ts(text, m, casefold)
    if m is None:
        return text, None, None
    m.append(len(s))
    return text, m, merged


def fold(s: str, *, casefold: bool = True, apostrophes: bool = True, translit: bool = False) -> tuple[str, list[int]]:
    """Fold ``s`` for matching; returns ``(folded, index_map)``.

    * ``casefold``: :meth:`str.casefold` (per character, may expand: ``ß`` -> ``ss``);
    * ``apostrophes``: every variant in :data:`APOSTROPHES` becomes ``'``;
    * ``translit``: Uzbek Cyrillic -> Uzbek Latin (module docstring); with
      ``apostrophes=False`` the official ``ʻ`` (oʻ, gʻ) and ``ʼ`` (ъ) are produced.

    ``index_map[i]`` is the index in ``s`` of the character that produced ``folded[i]``;
    ``index_map[len(folded)] == len(s)`` is a sentinel. The map is non-decreasing; a
    character that folds to nothing (``ь`` with ``translit``) has no entry. With
    ``translit`` a ``t`` directly followed by ``s`` folds to one ``s`` (``tsirk`` ->
    ``sirk``) that maps to the ``t``: that ``s`` has no entry either.
    """
    folded, imap, _ = _fold_map(s, casefold, apostrophes, translit)
    if imap is None:
        imap = list(range(len(s) + 1))
    return folded, imap


def _needle_alternatives(text: str, casefold: bool, translit: bool) -> tuple[str, ...]:
    """Distinct foldings of the needle in every relevant context (first one: no context)."""
    if not translit:
        return (_fold_text(text, casefold, True, False),)
    alts: list[str] = []
    for before in _CONTEXT_BEFORE:
        for after in _CONTEXT_AFTER:
            # (no context ends with "t" or starts with "s": the needle never merges with one)
            folded, imap, _ = _fold_map(before + text + after, casefold, True, True)
            lo, hi = len(before), len(before) + len(text)
            if imap is None:
                piece = folded[lo:hi]
            else:
                piece = "".join(ch for ch, src in zip(folded, imap) if lo <= src < hi)
            if piece not in alts:
                alts.append(piece)
    return tuple(alts)


# ----------------------------------------------------------------------------------
# whole words
# ----------------------------------------------------------------------------------
def is_word_char(s: str, i: int) -> bool:
    """Whether ``s[i]`` is a word character for the whole-word option (module docstring)."""
    c = s[i]
    if c in _APOS_SET:
        return 0 < i < len(s) - 1 and _is_letter(s[i - 1]) and _is_letter(s[i + 1])
    return c.isalnum() or c == "_"


def is_whole_word(s: str, start: int, end: int) -> bool:
    """Whether the match ``s[start:end]`` is a whole word (module docstring "Whole word"): the
    characters before and after it are not word characters (:func:`is_word_char`). Public since fix
    round L1 (the filter of :attr:`TextQuery.regex_postfilter`); ``_is_whole_word`` stays as an alias."""
    return (start == 0 or not is_word_char(s, start - 1)) and (end >= len(s) or not is_word_char(s, end))


_is_whole_word = is_whole_word


_RX_LETTER = "(?!" + _APOS_CLASS + ")[^\\W\\d_]"
_RX_WORD_NO_APOS = "(?!" + _APOS_CLASS + ")\\w"
# zero-width guards equivalent to "not is_word_char(s, start - 1)" / "not is_word_char(s, end)";
# "(?u-ix:...)" keeps the user's IGNORECASE / ASCII / VERBOSE flags from changing their meaning
_RX_WORD_BEFORE = (
    "(?u-ix:(?<!" + _RX_WORD_NO_APOS + ")(?:(?<!" + _RX_LETTER + _APOS_CLASS + ")|(?!" + _RX_LETTER + ")))"
)
_RX_WORD_AFTER = (
    "(?u-ix:(?!" + _RX_WORD_NO_APOS + ")(?:(?<!" + _RX_LETTER + ")|(?!" + _APOS_CLASS + _RX_LETTER + ")))"
)
_RX_GLOBAL_FLAGS = re.compile(r"\(\?[aiLmsux]+\)")
_RX_VERBOSE_GAP = re.compile(r"(?:\s+|#[^\n]*(?:\n|\Z))*")


def _whole_word_pattern(pattern: str, flags: int) -> str:
    """Wrap a valid pattern in whole-word guards (leading global flags stay in front;
    in verbose mode whitespace and comments between them are skipped like ``re`` does)."""
    verbose = bool(flags & re.VERBOSE)
    head_end = pos = 0
    while True:
        if verbose:
            pos = _RX_VERBOSE_GAP.match(pattern, pos).end()  # type: ignore[union-attr]
        m = _RX_GLOBAL_FLAGS.match(pattern, pos)
        if m is None:
            break
        head_end = pos = m.end()
    body = pattern[head_end:]
    if verbose:
        body += "\n"  # end a trailing "# comment" before the closing guard
    return pattern[:head_end] + "(?:" + _RX_WORD_BEFORE + ")(?:" + body + ")(?:" + _RX_WORD_AFTER + ")"


# ----------------------------------------------------------------------------------
# regex errors
# ----------------------------------------------------------------------------------
_REGEX_MESSAGES: tuple[tuple[str, str], ...] = (
    ("missing ), unterminated subpattern", "textmatch.regex.unterminated_group"),
    ("unbalanced parenthesis", "textmatch.regex.unbalanced_paren"),
    ("unterminated character set", "textmatch.regex.unterminated_set"),
    ("nothing to repeat", "textmatch.regex.nothing_to_repeat"),
    ("multiple repeat", "textmatch.regex.multiple_repeat"),
    ("min repeat greater than max repeat", "textmatch.regex.bad_repeat_range"),
    ("the repetition number is too large", "textmatch.regex.repeat_too_large"),
    ("bad character range", "textmatch.regex.bad_range"),
    ("bad escape", "textmatch.regex.bad_escape"),
    ("incomplete escape", "textmatch.regex.bad_escape"),
    ("look-behind requires fixed-width pattern", "textmatch.regex.lookbehind"),
    ("unknown group name", "textmatch.regex.unknown_group"),
    ("invalid group reference", "textmatch.regex.unknown_group"),
    ("unexpected end of pattern", "textmatch.regex.unexpected_end"),
    ("global flags not at the start", "textmatch.regex.global_flags"),
    ("unknown extension", "textmatch.regex.bad_construct"),
    ("unknown flag", "textmatch.regex.bad_construct"),
    ("bad inline flags", "textmatch.regex.bad_construct"),
    ("missing -, : or )", "textmatch.regex.bad_construct"),
    ("missing ), unterminated comment", "textmatch.regex.unterminated_group"),
    ("missing group name", "textmatch.regex.bad_group_name"),
    ("bad character in group name", "textmatch.regex.bad_group_name"),
    ("missing >, unterminated name", "textmatch.regex.bad_group_name"),
    ("missing ), unterminated name", "textmatch.regex.bad_group_name"),
    ("redefinition of group name", "textmatch.regex.duplicate_group"),
    ("cannot refer to an open group", "textmatch.regex.open_group"),
    ("bad group number", "textmatch.regex.unknown_group"),
    ("too many groups", "textmatch.regex.too_many_groups"),
    ("undefined character name", "textmatch.regex.bad_escape"),
    ("octal escape value", "textmatch.regex.bad_escape"),
    ("missing :", "textmatch.regex.bad_construct"),
    ("missing <", "textmatch.regex.bad_construct"),
    ("missing {", "textmatch.regex.bad_construct"),
    ("missing flag", "textmatch.regex.bad_construct"),
    ("conditional backref", "textmatch.regex.bad_construct"),
    ("unsupported", "textmatch.regex.bad_construct"),
)


def _regex_message_key(exc: BaseException) -> tuple[str, str]:
    """(i18n key, raw message) of a regular-expression compile error."""
    if isinstance(exc, RecursionError):
        return "textmatch.regex.too_complex", str(exc)
    if isinstance(exc, OverflowError):
        return "textmatch.regex.repeat_too_large", str(exc)
    message = getattr(exc, "msg", None) or str(exc) or type(exc).__name__
    for prefix, k in _REGEX_MESSAGES:
        if message.startswith(prefix):
            return k, message
    return "textmatch.regex.other", message


def regex_error_text(exc: BaseException) -> str:
    """Localized description of an error raised by :func:`re.compile` (``re.error``, or
    the ``RecursionError`` / ``OverflowError`` it may raise for absurd patterns), e.g.
    ``"'(' yopilmagan: ')' yetishmaydi"``. Python's own (English) text is used only for
    messages without a translation. Shared by the search (regex mode) and the filter
    language (``matches``)."""
    key, message = _regex_message_key(exc)
    return t(key, message=message)


def _regex_syntax_error(pattern: str, exc: BaseException) -> QuerySyntaxError:
    key, _ = _regex_message_key(exc)
    pos = getattr(exc, "pos", None)
    if not isinstance(pos, int):
        pos = 0
        if key == "textmatch.regex.lookbehind":
            found = [i for i in (pattern.find("(?<="), pattern.find("(?<!")) if i >= 0]
            pos = min(found) if found else 0
    return QuerySyntaxError("textmatch.regex_error", pos=pos, text=pattern, detail=regex_error_text(exc))


# ----------------------------------------------------------------------------------
# numbers
# ----------------------------------------------------------------------------------
_NUMBER_RE = re.compile(
    r"(?P<sign>[+\-\N{MINUS SIGN}]?)(?:(?P<int>[0-9]+)(?:\.(?!\.)(?P<frac>[0-9]*))?|\.(?P<frac2>[0-9]+))"
    r"(?:[eE](?P<exp>[+\-]?[0-9]+))?"
)
_OP_RE = re.compile("==|!=|<>|<=|>=|=<|=>|[=<>≤≥≠]")
_OP_ALIASES = {
    "=": "=",
    "==": "=",
    "!=": "!=",
    "<>": "!=",
    "≠": "!=",
    "<": "<",
    "<=": "<=",
    "=<": "<=",
    "≤": "<=",
    ">": ">",
    ">=": ">=",
    "=>": ">=",
    "≥": ">=",
}
_OP_TESTS = {
    "=": lambda c: c == 0,
    "!=": lambda c: c != 0,
    "<": lambda c: c < 0,
    "<=": lambda c: c <= 0,
    ">": lambda c: c > 0,
    ">=": lambda c: c >= 0,
}


class _NumKey(NamedTuple):
    """Exact decimal value: ``sign * 0.DIGITS * 10**exp`` (``digits`` without edge zeros)."""

    sign: int
    exp: int
    digits: str


_ZERO = _NumKey(0, 0, "")


def _num_key_from_match(m: re.Match[str]) -> _NumKey | None:
    int_part = m.group("int") or ""
    frac = m.group("frac")
    if frac is None:
        frac = m.group("frac2") or ""
    digits = (int_part + frac).lstrip("0")
    if not digits:
        return _ZERO
    exp_text = m.group("exp")
    try:
        exp = int(exp_text) if exp_text else 0
    except ValueError:  # exponent longer than int() accepts (> 4300 digits)
        return None
    sign = -1 if m.group("sign") in ("-", "\N{MINUS SIGN}") else 1
    return _NumKey(sign, exp - len(frac) + len(digits), digits.rstrip("0"))


def _num_key(text: str) -> _NumKey | None:
    m = _NUMBER_RE.fullmatch(text)
    return None if m is None else _num_key_from_match(m)


def _num_cmp(a: _NumKey, b: _NumKey) -> int:
    """Exact comparison (same result as comparing ``decimal.Decimal`` values)."""
    if a.sign != b.sign:
        return -1 if a.sign < b.sign else 1
    if a.sign == 0:
        return 0
    if a.exp != b.exp:
        mag = -1 if a.exp < b.exp else 1
    elif a.digits != b.digits:
        mag = -1 if a.digits < b.digits else 1
    else:
        return 0
    return mag * a.sign


NumKey = _NumKey
"""Exact decimal value of a number text (see :func:`number_key`); equal numbers have equal
(hashable) keys: ``number_key("1.10") == number_key("1.1")``."""


def number_key(text: str) -> NumKey | None:
    """Exact decimal value of a number text -- JSON number syntax, also ``+5``, ``.5``,
    ``5.`` and the Unicode minus -- as a key for :func:`compare_numbers` (any exponent,
    no float rounding); ``None`` when ``text`` is not a number."""
    return _num_key(text)


def compare_numbers(a: NumKey, b: NumKey) -> int:
    """``-1``, ``0`` or ``1``: exact comparison of two :func:`number_key` values (the
    comparison of number mode, ``search --number``)."""
    return _num_cmp(a, b)


def _skip_ws(text: str, pos: int) -> int:
    n = len(text)
    while pos < n and text[pos].isspace():
        pos += 1
    return pos


def _number_error(text: str, pos: int, detail: str) -> QuerySyntaxError:
    return QuerySyntaxError("textmatch.number_error", pos=pos, text=text, detail=detail)


def _expect_number(text: str, pos: int) -> tuple[_NumKey, int]:
    m = _NUMBER_RE.match(text, pos)
    key = None if m is None else _num_key_from_match(m)
    if m is None or key is None:
        raise _number_error(text, pos, t("textmatch.number.expected_number"))
    return key, m.end()


def _expect_end(text: str, pos: int) -> None:
    pos = _skip_ws(text, pos)
    if pos != len(text):
        raise _number_error(text, pos, t("textmatch.number.trailing", extra=text[pos:]))


def _parse_number_query(text: str) -> tuple[tuple[str, _NumKey], ...]:
    """``"> 5"`` -> ``((">", key),)``; ``"1..2"`` -> ``((">=", k1), ("<=", k2))``."""
    pos = _skip_ws(text, 0)
    m = _OP_RE.match(text, pos)
    if m is not None:
        key, pos = _expect_number(text, _skip_ws(text, m.end()))
        _expect_end(text, pos)
        return ((_OP_ALIASES[m.group()], key),)
    low, pos = _expect_number(text, pos)
    pos = _skip_ws(text, pos)
    if text.startswith("..", pos):
        high, pos = _expect_number(text, _skip_ws(text, pos + 2))
        _expect_end(text, pos)
        if _num_cmp(low, high) > 0:
            low, high = high, low
        return ((">=", low), ("<=", high))
    _expect_end(text, pos)
    return (("=", low),)


# ----------------------------------------------------------------------------------
# prefilter needles
# ----------------------------------------------------------------------------------
class _CaseTables:
    """Inverse of :meth:`str.casefold` over all code points (built once, lazily)."""

    __slots__ = ("inverse", "expanding", "apostrophe_producers")

    def __init__(self) -> None:
        inverse: dict[str, list[str]] = {}
        expanding: list[tuple[str, str]] = []
        producers: list[str] = []
        block = 0x800  # 0xD800 and 0xE000 are multiples: surrogate blocks are skipped whole
        for base in range(0, 0x110000, block):
            if 0xD800 <= base < 0xE000:
                continue
            chars = "".join(map(chr, range(base, base + block)))
            if chars.casefold() == chars:
                continue
            for ch in chars:
                f = ch.casefold()
                if f == ch:
                    continue
                if len(f) == 1:
                    inverse.setdefault(f, []).append(ch)
                else:
                    expanding.append((ch, f))
                if ch not in _APOS_SET and any(a in f for a in APOSTROPHES):
                    producers.append(ch)
        self.inverse: dict[str, tuple[str, ...]] = {k: tuple(v) for k, v in inverse.items()}
        self.expanding: tuple[tuple[str, str], ...] = tuple(expanding)
        self.apostrophe_producers: tuple[str, ...] = tuple(producers)

    def variants(self, c: str) -> tuple[str, ...]:
        """Every code point whose casefold is exactly ``c`` (``c`` itself first)."""
        return (c,) + self.inverse.get(c, ())


_tables: _CaseTables | None = None
_tables_lock = threading.Lock()


def _case_tables() -> _CaseTables:
    global _tables
    if _tables is None:
        with _tables_lock:
            if _tables is None:
                _tables = _CaseTables()
    return _tables


_SHORT_ESCAPES = {
    '"': b'\\"',
    "\\": b"\\\\",
    "\b": b"\\b",
    "\f": b"\\f",
    "\n": b"\\n",
    "\r": b"\\r",
    "\t": b"\\t",
}


def _json_forms(ch: str, escapes: bool) -> tuple[bytes, ...]:
    """Every way a writer (see module docstring) may encode ``ch`` inside a JSON string."""
    short = _SHORT_ESCAPES.get(ch)
    if short is not None:
        return (short,)
    cp = ord(ch)
    if cp < 0x20:
        return (b"\\u%04x" % cp,)
    if 0xD800 <= cp <= 0xDFFF:
        return ()  # a lone surrogate cannot appear in UTF-8 without \u escapes
    if ch == "/" and escapes:
        return (b"/", b"\\/")
    return (ch.encode("utf-8"),)


_Unit = tuple[tuple[bytes, ...], tuple[bytes, ...]]  # (product forms, standalone forms)


def _needle_units(folded: str, case_sensitive: bool, escapes: bool) -> list[_Unit | None]:
    tables = None if case_sensitive else _case_tables()
    units: list[_Unit | None] = []
    for c in folded:
        if c in _APOS_SET:
            units.append(None)
            continue
        if tables is None:
            product: tuple[str, ...] = (c,)
            standalone: tuple[str, ...] = ()
        else:
            every = tables.variants(c)
            up = c.upper()
            product = (c, up) if up != c and up in every else (c,)
            standalone = tuple(x for x in every if x not in product)
        pforms = tuple(sorted({f for x in product for f in _json_forms(x, escapes)}))
        if not pforms:
            units.append(None)
            continue
        sforms = tuple(sorted({f for x in standalone for f in _json_forms(x, escapes)}))
        units.append((pforms, sforms))
    return units


def _choose_anchor(units: list[_Unit | None]) -> tuple[int, int] | None:
    """``(start, length)`` of the longest run whose product count <= MAX_ANCHOR_VARIANTS."""
    best: tuple[int, int, int] | None = None  # (length, count, start)
    n = len(units)
    i = 0
    while i < n:
        if units[i] is None:
            i += 1
            continue
        j = i
        while j < n and units[j] is not None:
            j += 1
        left = i
        count = 1
        for right in range(i, j):
            count *= len(units[right][0])  # type: ignore[index]
            while count > MAX_ANCHOR_VARIANTS:
                count //= len(units[left][0])  # type: ignore[index]
                left += 1
            length = right + 1 - left
            if best is None or length > best[0] or (length == best[0] and count < best[1]):
                best = (length, count, left)
        i = j
    return None if best is None else (best[2], best[0])


def _build_needles(folded: str, case_sensitive: bool, escapes: bool) -> list[bytes] | None:
    units = _needle_units(folded, case_sensitive, escapes)
    anchor = _choose_anchor(units)
    needles: set[bytes] = set()
    if anchor is None:
        if not any(c in _APOS_SET for c in folded):
            return None  # e.g. only lone surrogates
        needles.update(a.encode("utf-8") for a in APOSTROPHES)
        if not case_sensitive:
            needles.update(c.encode("utf-8") for c in _case_tables().apostrophe_producers)
        return sorted(needles)
    start, length = anchor
    chosen = [u for u in units[start : start + length] if u is not None]
    needles.update(b"".join(combo) for combo in itertools.product(*(u[0] for u in chosen)))
    for u in chosen:
        needles.update(u[1])
    if not case_sensitive:
        anchor_chars = set(folded[start : start + length])
        for x, fx in _case_tables().expanding:
            if any(c in fx for c in anchor_chars):
                needles.update(_json_forms(x, escapes))
    return sorted(needles)


# ----------------------------------------------------------------------------------
# TextQuery
# ----------------------------------------------------------------------------------
_VANISHING = tuple(sorted(set("ьЬъЪ") | _APOS_SET))  # characters that may fold to nothing (translit)


def _extend_over_vanishing(s: str, start: int, hi: int) -> int:
    """Move ``hi`` right by the number of characters in ``s[start:hi]`` that may fold to
    nothing (repeatedly), so a window keeps its margin of characters that do fold."""
    n = len(s)
    while hi < n:
        piece = s[start:hi]
        k = sum(piece.count(c) for c in _VANISHING)
        if not k:
            break
        start, hi = hi, min(n, hi + k)
    return hi


def _next_hit(
    s: str, folded: str, imap: list[int] | None, alt: str, pos: int, whole_word: bool,
    merged: dict[int, int] | None = None, t_end: bool = False,
) -> tuple[int, int, int, int] | None:
    """First valid occurrence of ``alt`` at folded position >= ``pos``:
    ``(folded_start, folded_end, start, end)``.

    ``merged``: the merged ``ts`` of the folding (:func:`_fold_map`); a merged ``s`` covers
    its ``t`` and its ``s``. ``t_end``: ``alt`` is a needle alternative ending in ``t`` with
    that ``t`` replaced by ``s`` -- it matches only when its last character is a ``ts``
    merged from two original characters, and the match ends after the ``t``."""
    j = folded.find(alt, pos)
    while j >= 0:
        e = j + len(alt)
        if imap is None:
            start, end, aligned = j, e, not t_end
        elif merged:
            start = imap[j]
            # the previous folded character must end before this one starts, and the next
            # one must start after this match ends (a merged "s" ends at its own source)
            aligned = j == 0 or imap[j] > merged.get(j - 1, imap[j - 1])
            last = e - 1
            s_src = merged.get(last)
            if t_end:
                aligned = aligned and s_src is not None and s_src != imap[last]
                end = imap[last] + 1
            else:
                end = (imap[last] if s_src is None else s_src) + 1
                aligned = aligned and imap[e] >= end
        else:
            aligned = not t_end and (j == 0 or imap[j] != imap[j - 1]) and imap[e] != imap[e - 1]
            start, end = imap[j], imap[e - 1] + 1
        if aligned and (not whole_word or _is_whole_word(s, start, end)):
            return j, e, start, end
        j = folded.find(alt, j + 1)
    return None


class TextQuery:
    """A compiled search text; see the module docstring for the exact semantics.

    ``mode``: ``"plain"`` | ``"regex"`` | ``"number"``. Raises
    :class:`~jsonreshape.errors.UserInputError` for an empty text and
    :class:`~jsonreshape.errors.QuerySyntaxError` for an invalid regex / number condition.
    """

    __slots__ = (
        "text",
        "mode",
        "case_sensitive",
        "whole_word",
        "translit",
        "_casefold",
        "_translit_active",
        "_alts",
        "_t_alts",
        "_quick_alts",
        "_margin",
        "_regex",
        "_regex_postfilter",
        "_conditions",
        "_needle_cache",
    )

    def __init__(
        self,
        text: str,
        mode: str = "plain",
        case_sensitive: bool = False,
        whole_word: bool = False,
        translit: bool = False,
    ) -> None:
        if not isinstance(text, str):
            raise TypeError(f"search text must be str, not {type(text).__name__}")
        if mode not in MODES:
            raise ValueError(f"unknown search mode {mode!r} (expected one of {', '.join(MODES)})")
        self.text = text
        self.mode = mode
        self.case_sensitive = bool(case_sensitive)
        self.whole_word = bool(whole_word)
        self.translit = bool(translit)
        self._casefold = not self.case_sensitive
        self._translit_active = False
        self._alts: tuple[str, ...] = ()
        self._t_alts: tuple[str, ...] = ()       # translit: alternatives ending in "t" (see _next_hit)
        self._quick_alts: tuple[str, ...] = ()   # both: what a folded text must contain
        self._margin = 64
        self._regex: re.Pattern[str] | None = None
        self._regex_postfilter = False
        self._conditions: tuple[tuple[str, _NumKey], ...] = ()
        self._needle_cache: dict[bool, list[bytes] | None] = {}
        if not text:
            raise UserInputError("textmatch.empty_query")
        if mode == "plain":
            self._init_plain()
        elif mode == "regex":
            self._init_regex()
        else:
            self._conditions = _parse_number_query(text)

    # -- construction ---------------------------------------------------------------
    def _init_plain(self) -> None:
        translit = self.translit
        alts = _needle_alternatives(self.text, self._casefold, translit)
        if translit and not any(alts):  # e.g. only "ь": search it literally
            translit = False
            alts = _needle_alternatives(self.text, self._casefold, False)
        self._translit_active = translit
        self._alts = tuple(a for a in alts if a)
        if translit:
            # the haystack merges a "t" with a following "s": a needle ending in "t" also
            # matches the "t" of such a merged "s" ("kont" finds "kontsert")
            t_alts: list[str] = []
            for a in self._alts:
                if a[-1] in "tT":
                    b = a[:-1] + ("S" if a[-1] == "T" else "s")
                    if b not in t_alts:
                        t_alts.append(b)
            self._t_alts = tuple(t_alts)
        self._quick_alts = self._alts + self._t_alts
        # characters kept around a window of a huge string: a match starting in the window
        # (original length <= folded length, plus characters that fold to nothing) and the
        # neighbours the context rules and the whole-word test look at
        self._margin = 4 * max((len(a) for a in self._alts), default=1) + 64

    def _init_regex(self) -> None:
        flags = 0 if self.case_sensitive else re.IGNORECASE
        try:
            compiled = re.compile(self.text, flags)
        except (re.error, OverflowError, RecursionError) as exc:
            raise _regex_syntax_error(self.text, exc) from None
        if self.whole_word:
            try:
                compiled = re.compile(_whole_word_pattern(self.text, compiled.flags), flags)
            except (re.error, OverflowError, RecursionError):
                log.info("whole-word wrapping failed for %r; filtering matches instead", self.text)
                self._regex_postfilter = True
        self._regex = compiled

    # -- accessors (public since fix round L1; the private names stay) ----------------
    @property
    def regex(self) -> re.Pattern[str] | None:
        """Regex mode: the compiled pattern :meth:`find_spans` runs -- ``re.IGNORECASE`` unless
        case-sensitive and, with ``whole_word``, the look-around guards of the whole-word rule
        compiled into it -- for callers that must use exactly the search's pattern (the bulk
        "replace" uses its ``sub``). ``None`` in plain and number mode. When the guards could not be
        compiled into the pattern, :attr:`regex_postfilter` is true and the pattern is the user's
        own: keep only the matches :func:`is_whole_word` accepts."""
        return self._regex

    @property
    def regex_postfilter(self) -> bool:
        """Regex mode with ``whole_word``: ``True`` when the whole-word rule is applied by filtering
        the matches of :attr:`regex` (:func:`is_whole_word`) instead of by guards inside the pattern
        (a pattern the guards cannot wrap); ``False`` otherwise."""
        return self._regex_postfilter

    # -- matching -------------------------------------------------------------------
    def find_spans(self, s: str, reach: int | None = None) -> list[tuple[int, int]]:
        """Non-overlapping match spans in ``s`` (left to right); ``[]`` when no match.

        ``reach``: stop at the first span that starts more than ``reach`` characters after
        the start of the first match (the result is a prefix of the full list: the spans a
        snippet around the first match can show)."""
        if self.mode == "plain":
            if len(s) > HUGE_TEXT:
                return self._plain_spans_windowed(s, reach)
            return self._plain_spans(s, reach)
        if self.mode == "regex":
            return self._regex_spans(s, reach)
        return [(0, len(s))] if self.match_number(s) else []

    def _regex_spans(self, s: str, reach: int | None) -> list[tuple[int, int]]:
        assert self._regex is not None
        found: Iterator[re.Match[str]] = self._regex.finditer(s)
        if self._regex_postfilter:
            found = (m for m in found if _is_whole_word(s, *m.span()))
        if reach is None:
            return [m.span() for m in found]
        spans: list[tuple[int, int]] = []
        limit = -1
        for m in found:
            a = m.start()
            if not spans:
                limit = a + reach
            elif a > limit:
                break
            spans.append(m.span())
        return spans

    def matches(self, s: str) -> bool:
        """``bool(self.find_spans(s))``."""
        return bool(self.find_spans(s))

    def match_number(self, number_text: str) -> bool:
        """Number mode: whether the number text satisfies the comparison (exact decimal
        values; a text that is not a number gives ``False``). Other modes: whether the
        number's JSON text matches as text (``bool(find_spans(number_text))``)."""
        if self.mode != "number":
            return bool(self.find_spans(number_text))
        key = _num_key(number_text)
        if key is None:
            return False
        return all(_OP_TESTS[op](_num_cmp(key, ref)) for op, ref in self._conditions)

    def _plain_spans(self, s: str, reach: int | None = None) -> list[tuple[int, int]]:
        casefold, translit = self._casefold, self._translit_active
        quick = _fold_text(s, casefold, True, translit)
        for alt in self._quick_alts:
            if alt in quick:
                break
        else:
            return []
        if not translit and len(quick) == len(s):  # 1:1 folding: identity index map
            folded, imap, merged = quick, None, None
        else:
            folded, imap, merged = _fold_map(s, casefold, True, translit)
        spans: list[tuple[int, int]] = []
        pos = 0
        limit = -1
        whole_word = self.whole_word
        while True:
            best = self._best_hit(s, folded, imap, pos, whole_word, merged)
            if best is None:
                return spans
            if not spans:
                limit = best[2] + reach if reach is not None else -1
            elif reach is not None and best[2] > limit:
                return spans
            spans.append((best[2], best[3]))
            pos = best[1]

    def _best_hit(self, s: str, folded: str, imap: list[int] | None, pos: int,
                  whole_word: bool, merged: dict[int, int] | None = None) -> tuple[int, int, int, int] | None:
        """Leftmost valid occurrence of any alternative at folded position >= ``pos`` (the
        longer one on a tie)."""
        best: tuple[int, int, int, int] | None = None
        for alt in self._alts:
            hit = _next_hit(s, folded, imap, alt, pos, whole_word, merged)
            if hit is not None and (best is None or hit[0] < best[0] or (hit[0] == best[0] and hit[1] > best[1])):
                best = hit
        if merged:
            for alt in self._t_alts:
                hit = _next_hit(s, folded, imap, alt, pos, whole_word, merged, True)
                if hit is not None and (best is None or hit[0] < best[0]
                                        or (hit[0] == best[0] and hit[1] > best[1])):
                    best = hit
        return best

    def _plain_spans_windowed(self, s: str, reach: int | None) -> list[tuple[int, int]]:
        """:meth:`_plain_spans` for a huge string: folds :data:`WINDOW_CHARS` characters
        (plus context) at a time, so no folded copy or index map of the whole string is
        built; a window without any occurrence costs one fast fold. Same result."""
        casefold, translit = self._casefold, self._translit_active
        whole_word = self.whole_word
        n = len(s)
        spans: list[tuple[int, int]] = []
        limit = -1
        pos = 0  # the next match starts at or after this original position
        while pos < n:
            w_end = min(n, pos + WINDOW_CHARS)
            lo = max(0, pos - 16)  # left context: context rules and the whole-word test
            hi = min(n, w_end + self._margin)
            if translit:
                hi = _extend_over_vanishing(s, w_end, hi)
            seg = s[lo:hi]
            quick = _fold_text(seg, casefold, True, translit)
            if not any(alt in quick for alt in self._quick_alts):
                pos = w_end
                continue
            if not translit and len(quick) == len(seg):
                folded, imap, merged = quick, None, None
                fpos = pos - lo
            else:
                folded, imap, merged = _fold_map(seg, casefold, True, translit)
                fpos = pos - lo if imap is None else bisect.bisect_left(imap, pos - lo)
            next_pos = w_end
            while True:
                best = self._best_hit(seg, folded, imap, fpos, whole_word, merged)
                if best is None or best[2] + lo >= w_end:
                    break  # the next match (if any) starts in a later window
                start, end = best[2] + lo, best[3] + lo
                if not spans:
                    limit = start + reach if reach is not None else -1
                elif reach is not None and start > limit:
                    return spans
                spans.append((start, end))
                fpos = best[1]
                next_pos = max(w_end, end)
            pos = next_pos
        return spans

    # -- prefilter ------------------------------------------------------------------
    def prefilter_needles(self, flags: int) -> list[bytes] | None:
        """Byte needles for ``core.MultiFinder`` (sound: no false negatives), or ``None``
        when the caller must parse every record (module docstring)."""
        if self.mode != "plain" or self.translit or flags & FLAG_U_ESCAPES:
            return None
        escapes = bool(flags & FLAG_ESCAPES)
        if escapes not in self._needle_cache:
            folded = self._alts[0] if self._alts else ""
            self._needle_cache[escapes] = _build_needles(folded, self.case_sensitive, escapes) if folded else None
        cached = self._needle_cache[escapes]
        return None if cached is None else list(cached)

    def __repr__(self) -> str:
        return (
            f"TextQuery({self.text!r}, mode={self.mode!r}, case_sensitive={self.case_sensitive}, "
            f"whole_word={self.whole_word}, translit={self.translit})"
        )
