"""Tests for jsonreshape.textmatch: search text matching (ARCHITECTURE §7, AGENT_PROMPT §6.2).

Covers AGENT_PROMPT §14 test 16 (apostrophe variants, Latin <-> Cyrillic in both
directions, case variants), Unicode casefold edge cases, whole-word matching, regex and
number modes, the fold() index maps and the soundness of the byte prefilter needles.
"""

from __future__ import annotations

import ast
import json
import random
import re
import runpy
from decimal import Decimal, localcontext
from pathlib import Path

import orjson
import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from jsonreshape import i18n
from jsonreshape import textmatch as tm
from jsonreshape.errors import QuerySyntaxError, UserInputError
from jsonreshape.textmatch import APOSTROPHES, MAX_ANCHOR_VARIANTS, TextQuery, fold, is_word_char

FLAG_ESCAPES = tm.FLAG_ESCAPES
FLAG_U_ESCAPES = tm.FLAG_U_ESCAPES
ROOT = Path(__file__).resolve().parents[1]

APOS_VARIANTS = list(APOSTROPHES)  # ' ‘ ’ ʻ ʼ ` ´


def hyp(max_examples: int):
    """Deterministic hypothesis settings: fixed seed, no example database on disk."""
    return settings(
        max_examples=max_examples,
        deadline=None,
        derandomize=True,
        database=None,
        suppress_health_check=[HealthCheck.too_slow],
    )


def spans(needle: str, hay: str, **kw) -> list[tuple[int, int]]:
    return TextQuery(needle, **kw).find_spans(hay)


# ======================================================================================
# §14 test 16 — apostrophe variants and Latin <-> Cyrillic
# ======================================================================================
def test_apostrophe_set_is_exactly_the_specified_code_points():
    assert [hex(ord(c)) for c in APOSTROPHES] == ["0x27", "0x2018", "0x2019", "0x2bb", "0x2bc", "0x60", "0xb4"]


@pytest.mark.parametrize("case_sensitive", [False, True])
@pytest.mark.parametrize("b", APOS_VARIANTS)
@pytest.mark.parametrize("a", APOS_VARIANTS)
def test_16_apostrophe_variants_are_equal(a, b, case_sensitive):
    hay = f"Men O{b}zbekistonda, G{b}ulomda yashayman"
    assert spans(f"O{a}zbekiston", hay, case_sensitive=case_sensitive) == [(4, 15)]
    assert spans(f"G{a}ulom", hay, case_sensitive=case_sensitive) == [(19, 25)]


def test_16_apostrophes_inside_keys_and_phrases():
    assert spans("kalit bo‘shliq", "kalit bo'shliq bilan") == [(0, 14)]
    assert spans("Amir Temur ko'chasi", "AMIR TEMUR KOʻCHASI 5") == [(0, 19)]


def test_16_case_variants():
    hay = "Toshkent TOSHKENT toshkent tOsHkEnT"
    assert spans("toshkent", hay) == [(0, 8), (9, 17), (18, 26), (27, 35)]
    assert spans("TOSHKENT", hay) == [(0, 8), (9, 17), (18, 26), (27, 35)]
    assert spans("Toshkent", hay, case_sensitive=True) == [(0, 8)]
    assert spans("тошкент", "ТОШКЕНТ Тошкент") == [(0, 7), (8, 15)]
    assert spans("тошкент", "ТОШКЕНТ Тошкент", case_sensitive=True) == []


TRANSLIT_PAIRS = [
    ("Toshkent", "Тошкент"),
    ("O'zbekiston", "Ўзбекистон"),
    ("Oʻzbekiston", "Ўзбекистон"),
    ("O‘zbekiston", "Ўзбекистон"),
    ("G'ulom", "Ғулом"),
    ("Gʻulom", "Ғулом"),
    ("Shahrisabz", "Шаҳрисабз"),
    ("Chorsu", "Чорсу"),
    ("Yerevan", "Ереван"),
    ("poyezd", "поезд"),
    ("s'yezd", "съезд"),
    ("ma'no", "маъно"),
    ("Xiva", "Хива"),
    ("Qarshi", "Қарши"),
    ("yo'l", "йўл"),
    ("yosh", "ёш"),
    ("yulduz", "юлдуз"),
    ("energiya", "энергия"),
    ("sirk", "цирк"),  # ц: "s" at the start of a word and after consonants ...
    ("konsert", "концерт"),
    ("stansiya", "станция"),
    ("militsiya", "милиция"),  # ... "ts" after vowels (Latin "ts" folds to "s": both match)
    ("tsirk", "цирк"),
    ("kontsert", "концерт"),
    ("Moʻjiza", "Мўъжиза"),  # ъ after ў/ғ/қ is dropped
    ("moʻtadil", "мўътадил"),
    ("Isʼhoq", "Исҳоқ"),  # the apostrophe between s and h is optional
    ("Ishoq", "Исҳоқ"),
    ("Asʼhob", "Асҳоб"),
    ("Jizzax", "Жиззах"),
    ("kecha", "кеча"),
    ("pyesa", "пьеса"),
    ("Samarqand", "Самарқанд"),
    ("Farg'ona", "Фарғона"),
]


@pytest.mark.parametrize("latin,cyrillic", TRANSLIT_PAIRS)
def test_16_latin_cyrillic_both_directions(latin, cyrillic):
    for needle, hay in ((latin, cyrillic), (cyrillic, latin)):
        for variant in (hay, hay.upper(), hay.lower(), hay.title()):
            text = f"« {variant} »"
            assert TextQuery(needle, translit=True).find_spans(text) == [(2, 2 + len(variant))], (needle, variant)
        # translit off: the scripts stay different
        assert TextQuery(needle).find_spans(hay) == []


def test_16_translit_spans_map_back_to_the_original():
    hay = "Manzil: Тошкент шаҳри, Ўзбекистон"
    assert spans("toshkent", hay, translit=True) == [(8, 15)]
    assert spans("shahri", hay, translit=True) == [(16, 21)]
    assert spans("o'zbekiston", hay, translit=True) == [(23, 33)]
    assert spans("Oʻzbekiston", "Oʻzbekiston va Ўзбекистон", translit=True) == [(0, 11), (15, 25)]


def test_16_translit_mixed_scripts_and_homoglyphs():
    # a Cyrillic "е" typed inside a Latin word (common typo) is found
    typo = "Toshk\N{CYRILLIC SMALL LETTER IE}nt"
    assert spans("Toshkent", typo, translit=True) == [(0, 8)]
    assert spans("Toshkent", typo) == []


def test_translit_case_sensitive():
    assert spans("Toshkent", "Тошкент тошкент ТОШКЕНТ", translit=True, case_sensitive=True) == [(0, 7)]
    assert spans("SHAHRISABZ", "Шаҳрисабз ШАҲРИСАБЗ", translit=True, case_sensitive=True) == [(10, 19)]
    assert spans("Shahrisabz", "Шаҳрисабз ШАҲРИСАБЗ", translit=True, case_sensitive=True) == [(0, 9)]
    assert spans("Шаҳрисабз", "Shahrisabz SHAHRISABZ", translit=True, case_sensitive=True) == [(0, 10)]
    assert spans("ШАҲРИСАБЗ", "Shahrisabz SHAHRISABZ", translit=True, case_sensitive=True) == [(11, 21)]
    # a lone upper-case digraph letter in the needle may be "Sh" or "SH"
    assert spans("Ш", "Shahar SHAHAR shahar", translit=True, case_sensitive=True) == [(0, 2), (7, 9)]
    # "Е" in the needle: word start ("Ye"/"YE") or inside a word ("E")
    assert spans("Е", "Yer YER bEk", translit=True, case_sensitive=True) == [(0, 2), (4, 6), (9, 10)]


def test_translit_ye_rules():
    folded = fold("Ереван ер поезд съезд пьеса кеча Ёш Юлдуз 'ер 5ер", translit=True)[0]
    assert folded == "yerevan yer poyezd s'yezd pyesa kecha yosh yulduz 'yer 5yer"
    # a Cyrillic needle starting with "е" is searched as "ye..." and "e..."
    assert spans("ер", "Ереван ер тер yer er", translit=True) == [(0, 2), (7, 9), (11, 13), (14, 17), (18, 20)]
    # but "e" never matches inside the "ye" of a word-initial "е"
    assert spans("e", "ер", translit=True) == []
    assert spans("ye", "ер", translit=True) == [(0, 1)]


def test_translit_digraphs_are_atomic():
    # "s" is not a prefix of "ш" (-> "sh"): matches never end inside one character's expansion
    assert spans("s", "ш", translit=True) == []
    assert spans("sh", "ш", translit=True) == [(0, 1)]
    assert spans("o", "ў", translit=True) == []
    assert spans("o'", "ў", translit=True) == [(0, 1)]
    assert spans("Tos", "Тошкент", translit=True) == []
    assert spans("Tosh", "Тошкент", translit=True) == [(0, 3)]


def test_translit_soft_sign_needle_falls_back_to_literal_search():
    q = TextQuery("ь", translit=True)
    assert q.find_spans("пьеса") == [(1, 2)]
    assert q.find_spans("pyesa") == []
    assert q.prefilter_needles(0) is None  # translit requested -> parse-based search


def test_translit_produces_official_apostrophes_when_not_folding():
    assert fold("Ўзбекистон съезд ғалаба", casefold=False, apostrophes=False, translit=True)[0] == (
        "Oʻzbekiston sʼyezd gʻalaba"
    )
    assert fold("ЎЗБЕКИСТОН", casefold=False, translit=True)[0] == "O'ZBEKISTON"
    assert fold("ШАҲАР Шаҳар ЧОРСУ Ш.", casefold=False, translit=True)[0] == "SHAHAR Shahar CHORSU Sh."


def test_translit_table_is_cyrillic():
    for k, v in tm._CYRILLIC_TO_LATIN.items():
        assert 0x400 <= ord(k) <= 0x4FF, hex(ord(k))
        assert all(ord(c) < 0x80 or c in APOSTROPHES for c in v)
    assert len(tm._CYRILLIC_TO_LATIN) == 33 + 4  # Russian alphabet + ў қ ғ ҳ


# ======================================================================================
# casefold edge cases
# ======================================================================================
def test_casefold_sharp_s():
    assert spans("STRASSE", "Straße") == [(0, 6)]
    assert spans("Straße", "STRASSE") == [(0, 7)]
    assert spans("strasse", "STRAẞE") == [(0, 6)]
    assert spans("ẞ", "strasse") == [(4, 6)]
    assert spans("ss", "ß") == [(0, 1)]
    assert spans("ss", "sß") == [(1, 2)]
    # a match never begins/ends inside the expansion of one character
    assert spans("s", "ß") == []
    assert spans("s", "Straße") == [(0, 1)]
    assert spans("straße", "STRASSE", case_sensitive=True) == []
    assert spans("s", "ſ") == [(0, 1)]  # long s folds to "s"


def test_casefold_fi_ligature():
    assert spans("file", "ﬁle") == [(0, 3)]
    assert spans("ﬁ", "FI fi") == [(0, 2), (3, 5)]
    assert spans("f", "ﬁ") == []
    assert spans("if", "ﬁﬁ") == []


def test_casefold_dotted_capital_i():
    # str.casefold: "İ" -> "i" + U+0307 (combining dot above)
    dotted = "\N{LATIN CAPITAL LETTER I WITH DOT ABOVE}"
    i_dot = "i\N{COMBINING DOT ABOVE}"
    assert spans(f"{dotted}stanbul", f"{dotted}STANBUL") == [(0, 8)]
    assert spans(f"{dotted}stanbul", f"{i_dot}stanbul") == [(0, 9)]
    assert spans(i_dot, dotted) == [(0, 1)]
    assert spans("istanbul", f"{dotted}stanbul") == []  # documented: exact casefold semantics


def test_casefold_greek_final_sigma():
    assert spans("ΟΔΟΣ", "οδος") == [(0, 4)]
    assert spans("οδος", "ΟΔΟΣ") == [(0, 4)]
    assert spans("οδοσ", "οδος") == [(0, 4)]
    assert spans("σ", "ς Σ σ") == [(0, 1), (2, 3), (4, 5)]


def test_casefold_kelvin_and_micro():
    assert spans("k", "\N{KELVIN SIGN}") == [(0, 1)]
    assert spans("\N{GREEK SMALL LETTER MU}m", "\N{MICRO SIGN}m") == [(0, 2)]


# ======================================================================================
# whole word
# ======================================================================================
def test_whole_word_basic():
    assert spans("Toshkent", "Toshkent shahri", whole_word=True) == [(0, 8)]
    assert spans("Toshkent", "Toshkentda", whole_word=True) == []
    assert spans("kent", "Toshkent kent", whole_word=True) == [(9, 13)]
    assert spans("Toshkent", "'Toshkent'", whole_word=True) == [(1, 9)]
    assert spans("Toshkent", "ʻToshkentʼ", whole_word=True) == [(1, 9)]
    assert spans("Toshkent", "(Toshkent).", whole_word=True) == [(1, 9)]
    assert spans("foo", "foo_bar foo1 1foo foo-bar", whole_word=True) == [(18, 21)]


def test_whole_word_apostrophe_between_letters():
    assert spans("O", "O'zbekiston", whole_word=True) == []
    assert spans("zbekiston", "O‘zbekiston", whole_word=True) == []
    assert spans("O'zbekiston", "O‘zbekiston.", whole_word=True) == [(0, 11)]
    assert spans("ko'cha", "Amir Temur ko`chasi", whole_word=True) == []
    assert spans("ko'chasi", "Amir Temur ko`chasi", whole_word=True) == [(11, 19)]


def test_whole_word_with_translit():
    assert spans("Toshkent", "Тошкент шаҳри", whole_word=True, translit=True) == [(0, 7)]
    assert spans("Toshkent", "Тошкентда", whole_word=True, translit=True) == []
    assert spans("yesa", "пьеса", whole_word=True, translit=True) == []
    assert spans("yesa", "пьеса", translit=True) == [(2, 5)]


@pytest.mark.parametrize(
    "s,i,expected",
    [
        ("a'b", 1, True),
        ("aʻb", 1, True),
        ("a' b", 1, False),
        ("'ab", 0, False),
        ("ab'", 2, False),
        ("5'a", 1, False),
        ("a'5", 1, False),
        ("²'a", 1, True),  # "²" is alphanumeric and not a decimal digit -> a letter
        ("a_b", 1, True),
        ("é", 0, True),
        ("ʻ", 0, False),  # U+02BB is alphanumeric for str, but apostrophes follow the rule
        ("ab", 1, True),
        ("a b", 1, False),
        ("a-b", 1, False),
        ("7", 0, True),
    ],
)
def test_is_word_char(s, i, expected):
    assert is_word_char(s, i) is expected


def _brute_whole_word(needle: str, hay: str) -> list[tuple[int, int]]:
    out, p, n = [], 0, len(needle)
    while p + n <= len(hay):
        if hay.startswith(needle, p) and tm._is_whole_word(hay, p, p + n):
            out.append((p, p + n))
            p += n
        else:
            p += 1
    return out


@hyp(300)
@given(needle=st.text("ab1_é²", min_size=1, max_size=3), hay=st.text("ab_1² 'ʻ’`-.éЖ", max_size=24))
def test_whole_word_regex_guards_equal_python_rule(needle, hay):
    expected = _brute_whole_word(needle, hay)
    assert spans(needle, hay, case_sensitive=True, whole_word=True) == expected
    assert spans(re.escape(needle), hay, mode="regex", case_sensitive=True, whole_word=True) == expected
    assert spans(re.escape(needle), hay, mode="regex", whole_word=True) == expected  # IGNORECASE: no case pairs here
    assert spans("(?a)" + re.escape(needle), hay, mode="regex", case_sensitive=True, whole_word=True) == expected


# ======================================================================================
# regex mode
# ======================================================================================
def test_regex_basic():
    assert spans(r"tosh\w+", "Toshkent va toshbaqa", mode="regex") == [(0, 8), (12, 20)]
    assert spans(r"tosh\w+", "Toshkent va toshbaqa", mode="regex", case_sensitive=True) == [(12, 20)]
    assert spans(r"\d{3}", "a123456b", mode="regex") == [(1, 4), (4, 7)]
    assert spans("^Tosh", "Toshkent Toshkent", mode="regex") == [(0, 4)]


def test_regex_does_not_fold():
    assert spans("o'z", "Oʻzbekiston", mode="regex") == []
    assert spans("o'z", "O'zbekiston", mode="regex") == [(0, 3)]
    assert spans("toshkent", "Тошкент", mode="regex", translit=True) == []
    assert spans("strasse", "Straße", mode="regex") == []


def test_regex_empty_matches_count_as_matches():
    assert spans("x*", "ab", mode="regex") == [(0, 0), (1, 1), (2, 2)]
    assert TextQuery("^", mode="regex").matches("anything")


def test_regex_whole_word():
    assert spans("x|xy", "xyz xy", mode="regex", whole_word=True) == [(4, 6)]
    assert spans("(?i)TOSH", "tosh toshkent", mode="regex", case_sensitive=True, whole_word=True) == [(0, 4)]
    pattern = "(?x) x | xy   # a comment at the end"
    assert spans(pattern, "xyz xy", mode="regex", whole_word=True) == [(4, 6)]
    assert spans(r"(\w)\1", "aa bba aa", mode="regex", whole_word=True) == [(0, 2), (7, 9)]
    assert spans("O", "O'zbekiston O", mode="regex", whole_word=True) == [(12, 13)]


@pytest.mark.parametrize("pattern", ["(?x) (?i)abc", "(?x)\n(?i)abc", "(?x)#c\n(?i) a b c  # tail"])
def test_regex_whole_word_verbose_flag_groups(pattern):
    q = TextQuery(pattern, mode="regex", case_sensitive=True, whole_word=True)
    assert not q._regex_postfilter
    assert q.find_spans("ABC abcd abc") == [(0, 3), (9, 12)]


def test_regex_whole_word_fallback_filters_matches(monkeypatch):
    monkeypatch.setattr(tm, "_whole_word_pattern", lambda pattern, flags: "(")  # force the fallback
    q = TextQuery("x|xy", mode="regex", whole_word=True)
    assert q._regex_postfilter
    assert q.find_spans("x xy xz") == [(0, 1)]  # matches are filtered, alternatives are not retried


@pytest.mark.parametrize(
    "pattern,pos",
    [
        ("abc(", 3),
        ("[a", 0),
        ("x**", 2),
        (")", 0),
        ("*abc", 0),
        ("a{2,1}", 2),
        ("[z-a]", 1),
        ("tosh\\", 4),
        (r"\q", 0),
        ("(?<=a+)b", 0),
        ("x(?<!a*)b", 1),
        (r"(?P=nope)", 4),
        ("(?z)", 1),
        ("a{99999999999}", 0),
        ("abc(?i)", 3),
    ],
)
def test_regex_invalid_raises_query_syntax_error(pattern, pos, lang_en):
    with pytest.raises(QuerySyntaxError) as info:
        TextQuery(pattern, mode="regex")
    err = info.value
    assert err.pos == pos
    assert err.text == pattern
    assert err.key == "textmatch.regex_error"
    assert f"character {pos + 1}" in err.message
    assert err.hint and "plain-text mode" in err.hint
    assert err.caret().endswith(" " * pos + "^")


def test_regex_error_messages_are_localized(lang_uz):
    # like paths.py, the detail text is rendered in the language active when raising
    with pytest.raises(QuerySyntaxError) as info:
        TextQuery("abc(", mode="regex")
    assert info.value.message == "Regex ifodasida xato (4-belgi): '(' yopilmagan: ')' yetishmaydi"
    assert info.value.hint.endswith("oddiy matn rejimini tanlang.")
    i18n.set_language("en")
    with pytest.raises(QuerySyntaxError) as info:
        TextQuery("abc(", mode="regex")
    assert info.value.message == "Error in the regular expression at character 4: missing ')': a group is not closed"
    with pytest.raises(QuerySyntaxError) as info:
        TextQuery("(?P<1>x)", mode="regex")
    assert info.value.message == ("Error in the regular expression at character 5: invalid or unterminated group "
                                  "name: write (?P<name>...)")
    # a message without a translation: Python's own text is shown as the detail
    assert tm.regex_error_text(re.error("brand new problem")) == "brand new problem"


# ======================================================================================
# number mode
# ======================================================================================
NUMBER_CASES = [
    ("> 1000", "1000.5", True),
    ("> 1000", "1000", False),
    (">1000", "1e400", True),
    (">= 5", "5.0", True),
    (">= 5", "4.999", False),
    ("≥ 5", "5", True),
    ("=> 5", "6", True),
    ("< -2.5", "-3", True),
    ("< -2.5", "-2.5", False),
    ("< -2.5", "-2.4", False),
    ("<= 7", "7.00", True),
    ("≤ 7", "7.01", False),
    ("=< 7", "-7", True),
    ("= 3", "3.0", True),
    ("== 3", "3e0", True),
    ("= 3", "3.000001", False),
    ("!= 0", "0.0", False),
    ("!= 0", "-0", False),
    ("≠ 0", "1e-400", True),
    ("<> 0", "5", True),
    ("= 0", "-0.0", True),
    ("= 0", "0e10", True),
    ("1.1", "1.10", True),
    ("1.10", "1.1", True),
    ("= 1.1", "1.100000000000000000000001", False),
    ("12345678901234567890", "12345678901234567890", True),
    ("12345678901234567890", "12345678901234567891", False),  # distinct although float64 would merge them
    ("> 12345678901234567889", "12345678901234567890", True),
    ("= 1e400", "10e399", True),
    ("> 1e399", "1e400", True),
    ("< 1e400", "1e400", False),
    ("< -1e400", "-1e401", True),
    ("> 1e1000000000000000000", "1e1000000000000000001", True),  # beyond decimal's exponent range
    ("> 1e1000000000000000000", "1e999999999999999999", False),
    ("1000..2000", "1000", True),
    ("1000..2000", "2000", True),
    ("1000..2000", "1500.5", True),
    ("1000..2000", "999.999", False),
    ("1000..2000", "2000.0001", False),
    ("2000..1000", "1500", True),  # reversed bounds are swapped
    ("-5..-1", "-3", True),
    ("-5 .. -1", "0", False),
    ("1e3..2e3", "1500", True),
    (".5..1.", "0.75", True),
    ("42", "42.0", True),
    ("42", "-42", False),
    ("−5", "-5", True),  # Unicode minus
    ("+5", "5", True),
]


@pytest.mark.parametrize("query,number,expected", NUMBER_CASES)
def test_number_mode(query, number, expected):
    q = TextQuery(query, mode="number")
    assert q.match_number(number) is expected
    assert q.find_spans(number) == ([(0, len(number))] if expected else [])


@pytest.mark.parametrize("text", ["abc", "", " 5", "5 ", "1e", "--5", "0x10", "NaN", "Infinity", "1,5", "١٢"])
def test_number_mode_non_number_text_never_matches(text):
    for query in ("!= 0", "> -1e400", "= 5"):
        q = TextQuery(query, mode="number")
        assert q.match_number(text) is False
        assert q.find_spans(text) == []


@pytest.mark.parametrize(
    "query,pos",
    [
        ("> abc", 2),
        ("1..", 3),
        ("5 6", 2),
        (">=", 2),
        ("1,5", 1),
        ("..5", 0),
        ("> 5..6", 3),
        ("   ", 3),
        ("5 > 3", 2),
        ("= =", 2),
    ],
)
def test_number_mode_invalid_condition(query, pos, lang_en):
    with pytest.raises(QuerySyntaxError) as info:
        TextQuery(query, mode="number")
    err = info.value
    assert err.pos == pos
    assert err.key == "textmatch.number_error"
    assert f"character {pos + 1}" in err.message
    assert err.hint and "1000..2000" in err.hint


def test_number_error_in_uzbek(lang_uz):
    with pytest.raises(QuerySyntaxError) as info:
        TextQuery("> abc", mode="number")
    assert info.value.message == "Son shartida xato (3-belgi): son kutilgan"
    assert info.value.hint.startswith("Misollar:")


def test_match_number_in_text_modes_matches_the_number_text():
    assert TextQuery("1.1").match_number("1.10") is True
    assert TextQuery("1.10").match_number("1.1") is False
    assert TextQuery(r"^1\.1\d*$", mode="regex").match_number("1.10") is True
    assert TextQuery("1e5").match_number("1E5") is True
    assert TextQuery("1e5", case_sensitive=True).match_number("1E5") is False


def _number_texts():
    def build(sign, int_digits, frac, exp):
        text = sign + int_digits
        if frac is not None:
            text += "." + frac
        if exp is not None:
            text += exp
        return text

    digits = st.text("0123456789", max_size=22)
    return st.builds(
        build,
        st.sampled_from(["", "-", "+"]),
        st.text("0123456789", min_size=1, max_size=22),
        st.none() | digits,
        st.none() | st.builds(lambda e, s: "e" + s + str(e), st.integers(0, 60), st.sampled_from(["", "+", "-"])),
    )


@hyp(400)
@given(a=_number_texts(), b=_number_texts())
def test_number_comparison_equals_decimal(a, b):
    da, db = Decimal(a), Decimal(b)
    expected = (da > db) - (da < db)
    assert tm._num_cmp(tm._num_key(a), tm._num_key(b)) == expected
    assert TextQuery(f"= {b}", mode="number").match_number(a) is (da == db)
    assert TextQuery(f"> {b}", mode="number").match_number(a) is (da > db)
    assert TextQuery(f"<={b}", mode="number").match_number(a) is (da <= db)
    lo, hi = sorted([da, db])
    between = TextQuery(f"{a} .. {b}", mode="number")  # spaces: "0." + ".." + "1" would read as 0 .. .1
    assert between.match_number(str(lo)) is True
    assert between.match_number(str(hi)) is True
    with localcontext() as ctx:
        ctx.prec = 400  # exact: inputs have <= 44 digits and |exponent| <= 60
        below, above = lo - 1, hi + 1
    assert between.match_number(str(below)) is False
    assert between.match_number(str(above)) is False


# ======================================================================================
# constructor / API contract
# ======================================================================================
def test_constructor_validation(lang_en):
    with pytest.raises(ValueError):
        TextQuery("x", mode="fuzzy")
    with pytest.raises(TypeError):
        TextQuery(None)  # type: ignore[arg-type]
    for mode in ("plain", "regex", "number"):
        with pytest.raises(UserInputError) as info:
            TextQuery("", mode=mode)
        assert info.value.message == "The search text is empty."
        assert info.value.hint


def test_repr_and_attributes():
    q = TextQuery("Toshkent", case_sensitive=True, whole_word=True, translit=True)
    assert (q.text, q.mode, q.case_sensitive, q.whole_word, q.translit) == ("Toshkent", "plain", True, True, True)
    assert "Toshkent" in repr(q)


def test_matches_helper_and_non_overlap():
    q = TextQuery("aa")
    assert q.find_spans("aaaaa") == [(0, 2), (2, 4)]
    assert q.matches("xaax") and not q.matches("xax")
    assert spans(" ", "a b  c") == [(1, 2), (3, 4), (4, 5)]


def test_number_and_literal_texts_in_plain_mode():
    # search.py feeds the JSON text of numbers / literals in plain mode
    assert spans("true", "true") == [(0, 4)]
    assert spans("TRUE", "true") == [(0, 4)]
    assert spans("1e400", "1E400") == [(0, 5)]
    assert spans("0.1", "10.15") == [(1, 4)]
    assert spans("null", "null", whole_word=True) == [(0, 4)]


# ======================================================================================
# fold() and index maps
# ======================================================================================
def test_fold_examples():
    assert fold("") == ("", [0])
    assert fold("Straße") == ("strasse", [0, 1, 2, 3, 4, 4, 5, 6])
    assert fold("O‘zbekiston") == ("o'zbekiston", list(range(12)))
    assert fold("O‘zbekiston", apostrophes=False) == ("o‘zbekiston", list(range(12)))
    assert fold("ABC", casefold=False) == ("ABC", [0, 1, 2, 3])
    assert fold("Тошкент", translit=True) == ("toshkent", [0, 1, 2, 2, 3, 4, 5, 6, 7])
    assert fold("пьеса", translit=True) == ("pyesa", [0, 2, 2, 3, 4, 5])
    assert fold("ﬁŉ") == ("fi'n", [0, 0, 1, 1, 2])
    assert fold("ﬁŉ", apostrophes=False) == ("fiʼn", [0, 0, 1, 1, 2])


FOLD_ALPHABET = (
    "abcSsHhzZ019 _-.'‘’ʻʼ`´"
    "абвгдеёжзийклмнопрстуфхцчшщъыьэюяўқғҳ"
    "АБВГДЕЁЖЗИЙКЛМНОПРСТУФХЦЧШЩЪЫЬЭЮЯЎҚҒҲ"
    "ßẞﬁﬀİıſ\N{KELVIN SIGN}Σσςŉ\N{COMBINING DOT ABOVE}é😀\N{CYRILLIC SMALL LETTER ROUNDED VE}²"
)


@hyp(300)
@given(s=st.text(FOLD_ALPHABET, max_size=30), casefold=st.booleans(), apostrophes=st.booleans(), translit=st.booleans())
def test_fold_index_map_properties(s, casefold, apostrophes, translit):
    folded, imap = fold(s, casefold=casefold, apostrophes=apostrophes, translit=translit)
    assert len(imap) == len(folded) + 1
    assert imap[-1] == len(s)
    assert all(0 <= imap[i] < len(s) for i in range(len(folded)))
    assert all(imap[i] <= imap[i + 1] for i in range(len(imap) - 1))
    # the fast text-only path gives exactly the same text
    assert tm._fold_text(s, casefold, apostrophes, translit) == folded
    if not translit:
        # every original character contributes exactly its own (case/apostrophe) folding
        for j, ch in enumerate(s):
            piece = "".join(folded[i] for i in range(len(folded)) if imap[i] == j)
            expected = ch.casefold() if casefold else ch
            if apostrophes:
                expected = expected.translate(tm._APOS_TABLE)
            assert piece == expected
        assert folded == "".join(
            (ch.casefold() if casefold else ch).translate(tm._APOS_TABLE if apostrophes else {}) for ch in s
        )
    else:
        stage1, _ = fold(s, casefold=casefold, apostrophes=apostrophes)
        dropped = set(range(len(s))) - set(imap[:-1])

        def alone(ch: str) -> str:
            return fold(ch, casefold=casefold, apostrophes=apostrophes, translit=True)[0]

        for j in dropped:
            # only the soft sign, a hard sign after ў/ғ/қ, an apostrophe between s and h and
            # the s of a merged "ts" (it maps to its t) disappear
            c = s[j].casefold() if casefold else s[j]
            prev = s[j - 1].casefold() if casefold and j else (s[j - 1] if j else "")
            nxt = s[j + 1].casefold() if casefold and j + 1 < len(s) else (s[j + 1] if j + 1 < len(s) else "")
            merged_s = j > 0 and alone(s[j]) in ("s", "S") and alone(s[j - 1])[-1:] in ("t", "T")
            assert (c in ("ь", "Ь") or (c in ("ъ", "Ъ") and prev in "ўғқЎҒҚ")
                    or (c in APOSTROPHES and prev in "sSсС" and nxt in "hHҳҲ") or merged_s), (s, j)
        # no transliterated Cyrillic letter survives
        assert not any(c in tm._CYRILLIC_TO_LATIN or c in tm._UPPER_TO_LOWER for c in folded)
        if not any(c in tm._CYRILLIC_TO_LATIN or c in tm._UPPER_TO_LOWER for c in stage1):
            # Latin-only text is unchanged by translit, except the optional apostrophe
            # between s and h and "ts" -> "s"
            assert folded == tm._merge_ts_text(re.sub("(?<=[sS])" + tm._APOS_CLASS + "(?=[hH])", "", stage1),
                                               casefold)


@hyp(200)
@given(s=st.text(FOLD_ALPHABET, max_size=20), needle=st.text(FOLD_ALPHABET, min_size=1, max_size=4))
def test_plain_spans_are_aligned_and_fold_equal(s, needle):
    for kw in ({}, {"case_sensitive": True}, {"translit": True}, {"translit": True, "case_sensitive": True}):
        q = TextQuery(needle, **kw)
        found = q.find_spans(s)
        last = 0
        for a, b in found:
            assert last <= a < b <= len(s)
            last = b
            piece = fold(s[a:b], casefold=not q.case_sensitive, translit=q._translit_active)[0]
            if not q._translit_active:
                # context-free folding: the matched text folds exactly to the needle
                assert piece == q._alts[0]


def test_literal_occurrences_are_always_found():
    rng = random.Random(7)
    alphabet = FOLD_ALPHABET
    for _ in range(2000):
        needle = "".join(rng.choice(alphabet) for _ in range(rng.randint(1, 4)))
        hay = "".join(rng.choice(alphabet) for _ in range(rng.randint(0, 6)))
        hay = hay + needle + "".join(rng.choice(alphabet) for _ in range(rng.randint(0, 6)))
        for kw in ({}, {"case_sensitive": True}, {"translit": True}, {"translit": True, "case_sensitive": True}):
            if kw.get("translit") and all(c in VANISH for c in needle):
                continue  # documented: a needle made only of characters that may fold to nothing
            assert TextQuery(needle, **kw).find_spans(hay), (needle, hay, kw)


#: characters that fold to nothing in some contexts with translit (ь, ъ after ў/ғ/қ, an
#: apostrophe between s and h)
VANISH = "ьЬъЪ" + APOSTROPHES


# ======================================================================================
# prefilter needles
# ======================================================================================
NEEDLE_ALPHABET = (
    "toshkentTOSHKENTabcfiszOGq"
    "тошкентТОШКЕНТўғқҳЎҒ"
    "'‘’ʻʼ`´"
    "ßẞﬁſ\N{KELVIN SIGN}Σςŉ😀é"
    ' "\\/\n\x07_1.'
)


def _mutate(text: str, rng: random.Random) -> str:
    """A string that matches ``text`` case-/apostrophe-insensitively (often)."""
    tables = tm._case_tables()
    out = []
    for c in text:
        if c in APOSTROPHES:
            out.append(rng.choice(APOSTROPHES))
            continue
        options = [c, c.upper(), c.lower()]
        f = c.casefold()
        if len(f) == 1:
            options.extend(tables.variants(f))
        else:
            options.append(f)
        out.append(rng.choice(options))
    s = "".join(out)
    if rng.random() < 0.3:
        s = s.replace("ss", rng.choice(["ß", "ẞ"]), 1)
    if rng.random() < 0.3:
        s = s.replace("fi", "ﬁ", 1)
    return s


def _encodings(hay: str, flags: int) -> list[bytes]:
    base = json.dumps(hay, ensure_ascii=False)
    out = [base.encode("utf-8"), orjson.dumps(hay)]
    if flags & FLAG_ESCAPES:
        out.append(base.replace("/", "\\/").encode("utf-8"))  # writers that escape "/"
    return out


def _check_sound(q: TextQuery, hay: str) -> bool:
    if not q.find_spans(hay):
        return False
    for flags in (0, FLAG_ESCAPES):
        needles = q.prefilter_needles(flags)
        assert needles is not None, (q, flags)
        for enc in _encodings(hay, flags):
            assert any(n in enc for n in needles), (q, hay, flags, enc)
    return True


@st.composite
def _needle_and_haystack(draw):
    needle = draw(st.text(NEEDLE_ALPHABET, min_size=1, max_size=6))
    if draw(st.booleans()):
        middle = _mutate(needle, draw(st.randoms(use_true_random=False)))
    else:
        middle = draw(st.text(NEEDLE_ALPHABET, max_size=8))
    prefix = draw(st.text(NEEDLE_ALPHABET, max_size=4))
    suffix = draw(st.text(NEEDLE_ALPHABET, max_size=4))
    return needle, prefix + middle + suffix


@hyp(400)
@given(data=_needle_and_haystack(), case_sensitive=st.booleans(), whole_word=st.booleans())
def test_prefilter_needles_are_sound(data, case_sensitive, whole_word):
    needle, hay = data
    _check_sound(TextQuery(needle, case_sensitive=case_sensitive, whole_word=whole_word), hay)


def test_prefilter_needles_are_sound_seeded():
    rng = random.Random(16)
    positives = checked = 0
    for _ in range(2500):
        needle = "".join(rng.choice(NEEDLE_ALPHABET) for _ in range(rng.randint(1, 6)))
        middle = _mutate(needle, rng) if rng.random() < 0.7 else needle
        pre = "".join(rng.choice(NEEDLE_ALPHABET) for _ in range(rng.randint(0, 4)))
        post = "".join(rng.choice(NEEDLE_ALPHABET) for _ in range(rng.randint(0, 4)))
        for cs in (False, True):
            checked += 1
            positives += _check_sound(TextQuery(needle, case_sensitive=cs), pre + middle + post)
    assert positives > checked // 2  # the generator really exercises matching strings


def test_prefilter_none_cases():
    assert TextQuery("abc", mode="regex").prefilter_needles(0) is None
    assert TextQuery("> 5", mode="number").prefilter_needles(0) is None
    assert TextQuery("Toshkent", translit=True).prefilter_needles(0) is None
    assert TextQuery("Toshkent").prefilter_needles(FLAG_U_ESCAPES) is None
    assert TextQuery("Toshkent").prefilter_needles(FLAG_U_ESCAPES | FLAG_ESCAPES) is None
    assert TextQuery("\ud800").prefilter_needles(0) is None  # a lone surrogate cannot occur in UTF-8


def test_prefilter_exact_needles():
    assert TextQuery("Toshkent", case_sensitive=True).prefilter_needles(0) == [b"Toshkent"]
    assert TextQuery("a/b", case_sensitive=True).prefilter_needles(0) == [b"a/b"]
    assert TextQuery("a/b", case_sensitive=True).prefilter_needles(FLAG_ESCAPES) == [b"a/b", b"a\\/b"]
    assert TextQuery('say "hi"', case_sensitive=True).prefilter_needles(0) == [b'say \\"hi\\"']
    assert TextQuery("tab\there", case_sensitive=True).prefilter_needles(FLAG_ESCAPES) == [b"tab\\there"]
    assert TextQuery("bell\x07", case_sensitive=True).prefilter_needles(0) == [b"bell\\u0007"]
    # apostrophes split the needle: the anchor is the longest apostrophe-free part
    assert TextQuery("O'zbekiston", case_sensitive=True).prefilter_needles(0) == [b"zbekiston"]


def test_prefilter_case_insensitive_needles():
    needles = TextQuery("Straße").prefilter_needles(0)
    assert b"strasse" in needles and b"STRASSE" in needles and b"StraSSe" in needles
    assert "ß".encode() in needles and "ẞ".encode() in needles  # expanding characters
    assert "ſ".encode() in needles  # exotic case variant of "s" as a standalone needle
    assert needles == sorted(set(needles))
    assert all(needles)
    big = TextQuery("Toshkent").prefilter_needles(0)
    assert b"toshkent" in big and b"TOSHKENT" in big
    assert len([n for n in big if n.isascii() and len(n) == 8]) == MAX_ANCHOR_VARIANTS
    assert "\N{KELVIN SIGN}".encode() in big  # Kelvin sign folds to "k"
    cyr = TextQuery("Тошкент").prefilter_needles(0)
    assert "тошкент".encode() in cyr and "ТОШКЕНТ".encode() in cyr
    assert "\N{CYRILLIC SMALL LETTER TALL TE}".encode() in cyr  # exotic variant of "т": standalone
    assert TextQuery("'").prefilter_needles(0) == sorted(
        {a.encode() for a in APOSTROPHES} | {"ŉ".encode()}
    )
    assert TextQuery("ʻ", case_sensitive=True).prefilter_needles(0) == sorted({a.encode() for a in APOSTROPHES})


def test_prefilter_needles_with_multifinder():
    try:
        from jsonreshape import core
    except ImportError as exc:  # pragma: no cover - depends on the parallel build state
        pytest.skip(f"core backend not importable yet: {exc}")
    q = TextQuery("O'zbekiston")
    needles = q.prefilter_needles(0)
    finder = core.MultiFinder(needles)
    buf = json.dumps({"city": "OʻZBEKISTON"}, ensure_ascii=False).encode()
    assert finder.find_first(buf) >= 0
    assert finder.find_first(b'{"city": "Toshkent"}') == -1


def test_case_tables_match_bruteforce():
    tables = tm._case_tables()
    inverse: dict[str, list[str]] = {}
    expanding = []
    for cp in range(0x110000):
        if 0xD800 <= cp <= 0xDFFF:
            continue
        ch = chr(cp)
        f = ch.casefold()
        if f != ch:
            if len(f) == 1:
                inverse.setdefault(f, []).append(ch)
            else:
                expanding.append((ch, f))
    assert {k: tuple(v) for k, v in inverse.items()} == tables.inverse
    assert tuple(expanding) == tables.expanding
    assert tables.apostrophe_producers == ("ŉ",)


# ======================================================================================
# integration with shared modules
# ======================================================================================
def test_flag_constants_match_core_constants():
    values = runpy.run_path(str(ROOT / "jsonreshape" / "core" / "constants.py"))
    assert tm.FLAG_ESCAPES == values["FLAG_ESCAPES"]
    assert tm.FLAG_U_ESCAPES == values["FLAG_U_ESCAPES"]


def test_all_textmatch_message_keys_exist_in_both_languages():
    source = (ROOT / "jsonreshape" / "textmatch.py").read_text(encoding="utf-8")
    keys = {
        node.value
        for node in ast.walk(ast.parse(source))
        if isinstance(node, ast.Constant) and isinstance(node.value, str) and node.value.startswith("textmatch.")
    }
    assert "textmatch.regex_error" in keys and "textmatch.number_error" in keys
    for key in keys:
        entry = i18n.MESSAGES.get(key)
        assert entry is not None, key
        assert entry.get("uz") and entry.get("en"), key
    for key in ("textmatch.regex_error.hint", "textmatch.number_error.hint", "textmatch.empty_query.hint"):
        assert i18n.MESSAGES[key]["uz"] and i18n.MESSAGES[key]["en"]
    own = [k for k in i18n.MESSAGES if k.startswith("textmatch.")]
    assert all(i18n.MESSAGES[k].get("uz") and i18n.MESSAGES[k].get("en") for k in own)
