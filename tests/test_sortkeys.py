"""jsonreshape.sortkeys: typed, order-preserving sort keys (docs/ARCHITECTURE.md §6,
AGENT_PROMPT §6.4, part of §14 test 17).

* exhaustive ordering tables: exact decimal numbers, mixed types, null/missing placement for
  ascending/descending and nulls first/last;
* Uzbek Latin alphabet (oʻ gʻ sh ch ng, apostrophe variants), Uzbek Cyrillic, Latin before
  Cyrillic before other scripts, punctuation < digits < letters, natural digit runs, case ties;
* prefix-freeness, multi-field ``compose`` with mixed directions;
* ``encode_token(t) == encode_value(codec.loads(t))`` over random JSON (tests/jsongen) and
  number tokens;
* hypothesis: sorting by the keys equals sorting with a straightforward reference comparator
  (implemented independently below) on random strings, numbers and mixed JSON values.
"""

from __future__ import annotations

import copy
import functools
import pickle
import random
import time
import unicodedata
from decimal import Decimal

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

import jsongen
from jsonreshape import codec, sortkeys
from jsonreshape.codec import JsonFloat, JsonInt
from jsonreshape.errors import PathSyntaxError, UserInputError
from jsonreshape.i18n import set_language
from jsonreshape.paths import MISSING
from jsonreshape.sortkeys import (
    SortKey,
    collation_key,
    compose,
    describe_keys,
    encode_token,
    encode_value,
    record_key,
    token_encoder,
    value_encoder,
)

# hypothesis: deterministic (derandomize) and no example database on disk
HYP = dict(deadline=None, derandomize=True, database=None, suppress_health_check=[HealthCheck.too_slow])

COMBOS = [
    dict(collation=c, natural=n, case_sensitive=cs)
    for c in ("uz", "simple", "unicode") for n in (True, False) for cs in (False, True)
]
ALL_SPECS = [
    SortKey("x", descending=d, nulls=nl, **combo)
    for combo in COMBOS for d in (False, True) for nl in ("last", "first")
]

ACUTE, BREVE, ZWJ, SHY = chr(0x301), chr(0x306), chr(0x200D), chr(0xAD)
APOSTROPHE_VARIANTS = ["'", chr(0x2018), chr(0x2019), chr(0x2BB), chr(0x2BC), "`"]
OKINA = chr(0x2BB)  # ʻ


def kv(value, **options):
    return encode_value(value, SortKey("x", **options))


def kt(token: str, **options):
    return encode_token(token.encode("utf-8"), SortKey("x", **options))


def sort_values(values, **options):
    spec = SortKey("x", **options)
    return sorted(values, key=spec.encode_value)


def assert_strictly_increasing(keys, labels):
    for (k1, l1), (k2, l2) in zip(zip(keys, labels), zip(keys[1:], labels[1:])):
        assert k1 < k2, f"{l1!r} should sort before {l2!r}"


# =======================================================================================
# numbers
# =======================================================================================
NUMBER_GROUPS = [  # ascending; tokens inside a group are equal values
    ["-1e400", "-1E+400", "-10e399"],
    ["-12345678901234567890", "-1.2345678901234567890e19"],
    ["-5", "-5.0", "-0.5e1", "-50e-1"],
    ["-1.5", "-15e-1"],
    ["-1", "-1.0", "-1e0", "-10e-1"],
    ["-0.5", "-5e-1", "-0.50"],
    ["-1e-400"],
    ["0", "-0", "0.0", "-0.0", "0e10", "0E-5", "0.000", "-0e-0"],
    ["1e-400", "0.1e-399"],
    ["5e-324"],
    ["0.00001", "1e-5", "1E-5", "0.000010"],
    ["0.1", "1e-1", "0.10", "10e-2"],
    ["1", "1.0", "1e0", "10e-1", "0.1e1"],
    ["1.1", "1.10", "11e-1", "1.100"],
    ["2"],
    ["100", "1e2", "1E+2", "100.0", "1.00e2"],
    ["9007199254740993"],
    ["12345678901234567890", "1.2345678901234567890e19"],
    ["18446744073709551616"],
    ["123456789012345678901234567890"],
    ["1e400", "10e399"],
]


@pytest.mark.parametrize("descending", [False, True])
def test_number_order_table(descending: bool) -> None:
    spec = SortKey("n", descending=descending)
    reps = []
    for group in NUMBER_GROUPS:
        token_keys = {t: encode_token(t.encode(), spec) for t in group}
        value_keys = {t: encode_value(codec.loads(t), spec) for t in group}
        assert len(set(token_keys.values())) == 1, f"equal values must give identical keys: {group}"
        assert token_keys == value_keys, group
        reps.append((token_keys[group[0]], group[0]))
    keys = [k for k, _ in reps]
    labels = [label for _, label in reps]
    if descending:
        keys, labels = keys[::-1], labels[::-1]
    assert_strictly_increasing(keys, labels)


def test_numbers_are_exact_not_float_rounded() -> None:
    # all of these collapse to the same double but are different decimals
    tokens = ["9007199254740992", "9007199254740993", "9007199254740994"]
    keys = [kt(t) for t in tokens]
    assert keys[0] < keys[1] < keys[2]
    assert kt("0.1") != kt("0.1000000000000000055511151231257827")
    assert kt("0.1") < kt("0.1000000000000000055511151231257827")
    assert kt("1e400") > kt("1e399") > kt("179769313486231570000000")


def test_python_number_values() -> None:
    order = [
        float("-inf"), Decimal("-Infinity"), Decimal("-1E+400"), -(2**70), -5, -1.5, Decimal("-0.5"),
        -0.0, 0, Decimal("0E-10"), JsonInt("-0"), 5e-324, Decimal("0.1"), 0.1, 1, 1.0, Decimal("1.10"),
        1.5, 2**70, 1e300, JsonFloat("1e400"), Decimal("Infinity"), float("inf"),
        float("nan"), Decimal("NaN"),
    ]
    keys = [kv(v) for v in order]
    assert keys == sorted(keys)
    # equal values -> identical keys, whatever the Python type
    assert kv(0) == kv(-0.0) == kv(JsonInt("-0")) == kv(Decimal("-0")) == kv(Decimal("0E-10"))
    assert kv(0.1) == kv(Decimal("0.1")) == kt("0.1") == kt("1e-1")
    assert kv(1.5) == kv(Decimal("1.50")) == kt("1.50")
    assert kv(1e16) == kt("10000000000000000") == kv(10**16)
    assert kv(2**70) == kt(str(2**70)) == kv(Decimal(2**70))
    assert kv(float("inf")) == kv(Decimal("Infinity")) and kv(float("-inf")) == kv(Decimal("-Infinity"))
    assert kv(float("nan")) == kv(Decimal("NaN")) == kv(Decimal("sNaN"))
    # bool is not a number
    assert kv(True) != kv(1) and kv(False) != kv(0)


def test_absurd_exponents_are_clamped_monotonically() -> None:
    limit = 2**62
    big = [f"1e{limit - 3}", f"1e{limit - 1}", f"1e{limit}", f"1e{limit + 10}", "1e" + "9" * 40, "1e" + "9" * 5000]
    keys = [kt(t) for t in big]
    assert keys == sorted(keys)  # never out of order
    assert keys[0] < keys[1]
    assert keys[1] == keys[2] == keys[3] == keys[4] == keys[5]  # beyond the clamp: equal (documented)
    small = ["1e-" + "9" * 5000, f"1e-{limit + 10}", f"1e-{limit}", f"1e-{limit - 5}"]
    keys = [kt(t) for t in small]
    assert keys == sorted(keys) and keys[0] == keys[1] and keys[2] < keys[3]
    assert kt("-1e" + "9" * 5000) < kt("-1e400") < kt("0") < kt("1e-" + "9" * 5000)
    # thousands of digits
    huge = "9" * 5000
    assert kt(huge) > kt("1e4999") and kt(huge) < kt("1e5000") and kt("-" + huge) < kt("-1e4999")


@settings(max_examples=400, **HYP)
@given(st.lists(st.from_regex(r"-?(0|[1-9][0-9]{0,25})(\.[0-9]{1,20})?([eE][+-]?[0-9]{1,4})?", fullmatch=True),
                min_size=2, max_size=8),
       st.booleans())
def test_number_tokens_sort_like_decimal(tokens: list[str], descending: bool) -> None:
    spec = SortKey("n", descending=descending)
    by_key = sorted(range(len(tokens)), key=lambda i: (encode_token(tokens[i].encode(), spec), i))
    sign = -1 if descending else 1
    by_decimal = sorted(range(len(tokens)), key=lambda i: (sign * Decimal(tokens[i]), i))
    assert by_key == by_decimal
    for t in tokens:
        assert encode_token(t.encode(), spec) == encode_value(codec.loads(t), spec)


# =======================================================================================
# types, null and missing
# =======================================================================================
MIXED = [None, MISSING, "b", "a", 2, -1, True, False, [1, 2], [1], {"a": 1}, {}]


@pytest.mark.parametrize(
    "descending, nulls, expected",
    [
        # containers compare by compact JSON text: "[1,2]" < "[1]" and '{"a":1}' < "{}"
        (False, "last", [-1, 2, "a", "b", False, True, [1, 2], [1], {"a": 1}, {}, None, MISSING]),
        (True, "last", [{}, {"a": 1}, [1], [1, 2], True, False, "b", "a", 2, -1, None, MISSING]),
        (False, "first", [MISSING, None, -1, 2, "a", "b", False, True, [1, 2], [1], {"a": 1}, {}]),
        (True, "first", [MISSING, None, {}, {"a": 1}, [1], [1, 2], True, False, "b", "a", 2, -1]),
    ],
)
def test_type_order_and_null_placement(descending: bool, nulls: str, expected: list) -> None:
    assert sort_values(MIXED, descending=descending, nulls=nulls) == expected
    # same through raw tokens (MISSING = no token)
    spec = SortKey("x", descending=descending, nulls=nulls)
    raw = {id(v): (None if v is MISSING else codec.dumps(v)) for v in MIXED}
    assert sorted(MIXED, key=lambda v: encode_token(raw[id(v)], spec)) == expected


def test_containers_order_by_compact_json_text() -> None:
    values = [[], [1], [1, 2], [2], ["a"], [[]], {}, {"a": 1}, {"a": 2}, {"b": 0}]
    keys = [kv(v) for v in values]
    arrays = [v for v in values if isinstance(v, list)]
    assert sorted(arrays, key=kv) == sorted(arrays, key=codec.dumps)
    assert kt('[ 1 ,\n 2 ]') == kv([1, 2]) and kt('{"a" : 1}') == kv({"a": 1})
    assert kt('{"a":1,"a":2}') == kv({"a": 2})  # duplicate keys: last wins (dict semantics)
    assert kt("[1.10]") == kv([JsonFloat("1.10")]) != kv([1.1])  # exact number text
    assert kt('["\\u00e9"]') == kv(["é"])
    assert len(set(keys)) == len(keys)


# =======================================================================================
# strings: Uzbek Latin, Cyrillic, scripts, natural order, case, apostrophes
# =======================================================================================
def test_uzbek_latin_alphabet_names() -> None:
    names = ["Abdulla", "Zafar", f"O{OKINA}tkir", f"G{OKINA}ani", "Shahzod", "Chori", "Sardor", "Gulnora",
             "Olim", "Nodira", "Ngoz", "Bobur"]
    expected = ["Abdulla", "Bobur", "Gulnora", "Nodira", "Olim", "Sardor", "Zafar", f"O{OKINA}tkir",
                f"G{OKINA}ani", "Shahzod", "Chori", "Ngoz"]
    assert sort_values(names) == expected
    assert sort_values(names, descending=True) == expected[::-1]
    # the full alphabet: a..z with c and w at English positions, then oʻ gʻ sh ch ng
    letters = list("abcdefghijklmnopqrstuvwxyz") + [f"o{OKINA}", f"g{OKINA}", "sh", "ch", "ng"]
    shuffled = letters[:]
    random.Random(3).shuffle(shuffled)
    assert sort_values(shuffled) == letters
    # "simple": letter by letter, apostrophes ignorable
    assert sort_values(names, collation="simple") == ["Abdulla", "Bobur", "Chori", f"G{OKINA}ani", "Gulnora",
                                                      "Ngoz", "Nodira", "Olim", f"O{OKINA}tkir", "Sardor",
                                                      "Shahzod", "Zafar"]


@pytest.mark.parametrize("variant", APOSTROPHE_VARIANTS)
def test_every_apostrophe_variant_forms_the_letter(variant: str) -> None:
    names = ["Zafar", f"O{variant}tkir", f"G{variant}ani", "Olim", "Gulnora", "Shahzod"]
    assert sort_values(names) == ["Gulnora", "Olim", "Zafar", f"O{variant}tkir", f"G{variant}ani", "Shahzod"]


def _levels_without_original(s: str, **options) -> bytes:
    """Key of ``s`` minus level 4 (the original string), to compare collation levels 1-3."""
    payload = collation_key(s, **options)
    original = s.encode("utf-8", "surrogatepass").replace(b"\x01", b"\x01\x02").replace(b"\x00", b"\x01\x01")
    assert payload.endswith(original + b"\x00")
    return payload[: len(payload) - len(original) - 1]


def test_apostrophe_variants_are_equal_at_collation_levels() -> None:
    for word in ("O{}tkir", "g{}ani", "ma{}no", "Is{}hoq", "To{}g{}ri"):
        variants = [word.replace("{}", a) for a in APOSTROPHE_VARIANTS]
        for collation in ("uz", "simple"):
            levels = {_levels_without_original(v, collation=collation) for v in variants}
            assert len(levels) == 1, (word, collation)
        # ... but the order is still total and deterministic (level 4: original text)
        keys = [kv(v) for v in variants]
        assert len(set(keys)) == len(variants)
    # outside oʻ/gʻ an apostrophe is ignorable (tutuq belgisi)
    assert _levels_without_original("ma'no") == _levels_without_original("mano")
    assert _levels_without_original("O'tkir", collation="simple") == _levels_without_original("Otkir", collation="simple")
    assert _levels_without_original("O'tkir") != _levels_without_original("Otkir")


def test_uzbek_merge_rules() -> None:
    # s'h stays s + h ("Is'hoq"), sh is one letter after z
    assert sort_values(["Ishoq", "Is'hoq", "Isxoq"]) == ["Is'hoq", "Isxoq", "Ishoq"]
    # ngʻ is n + gʻ (oʻ/gʻ merge first): t o n gʻ  vs  t o n z  vs  t o ng
    assert sort_values(["Tong", f"Tong{OKINA}", "Tonz"]) == ["Tonz", f"Tong{OKINA}", "Tong"]
    # accented / compatibility letters take part in merges through their base letters
    assert _levels_without_original("şh") == _levels_without_original("sh")
    assert _levels_without_original("ŋ") == _levels_without_original("ng")
    assert _levels_without_original("ğ'") == _levels_without_original(f"g{OKINA}")
    # merged letters keep the case flags of both letters: sh < sH < Sh < SH
    assert sort_values(["SH", "Sh", "sH", "sh"]) == ["sh", "sH", "Sh", "SH"]


def test_uzbek_cyrillic_order() -> None:
    alphabet = "абвгдеёжзийклмнопрстуфхцчшщъыьэюяўқғҳ"
    letters = list(alphabet)
    shuffled = letters[:]
    random.Random(5).shuffle(shuffled)
    assert sort_values(shuffled) == letters
    names = ["Ҳасан", "Ғани", "Қобил", "Ўткир", "Шаҳзод", "Чори", "Олим", "Нодира", "Зафар", "Ёқуб",
             "Гулнора", "Бобур", "Абдулла", "Щука", "Ыа", "Ъа", "Ьа", "Эркин", "Юсуф", "Ярослав"]
    expected = ["Абдулла", "Бобур", "Гулнора", "Ёқуб", "Зафар", "Нодира", "Олим", "Чори", "Шаҳзод", "Щука",
                "Ъа", "Ыа", "Ьа", "Эркин", "Юсуф", "Ярослав", "Ўткир", "Қобил", "Ғани", "Ҳасан"]
    assert sort_values(names) == expected
    # case-insensitive at the primary level, lowercase first on ties
    assert sort_values(["Ўткир", "ўткир", "ЎТКИР"]) == ["ўткир", "Ўткир", "ЎТКИР"]
    # other Cyrillic letters (і ґ ө ...) after ҳ, by code point
    assert sort_values(["ө", "ҳ", "і", "ґ", "я"]) == ["я", "ҳ", "і", "ґ", "ө"]
    # decomposed й / ў equal the precomposed letters at the collation levels
    assert _levels_without_original("и" + BREVE) == _levels_without_original("й")
    assert _levels_without_original("У" + BREVE + "ткир") == _levels_without_original("Ўткир")


def test_scripts_and_character_classes() -> None:
    ordered = ["\x07", " ", "!", "-", "~", chr(0x2014), chr(0x1F600), "0", "9", "a", "z", f"o{OKINA}", "ng",
               chr(0x259), "а", "ҳ", "і", "α", "中", chr(0x10428)]
    shuffled = ordered[:]
    random.Random(1).shuffle(shuffled)
    assert sort_values(shuffled) == ordered
    assert sort_values(shuffled, descending=True) == ordered[::-1]
    assert sort_values(["Zafar", "Абдулла", "Ali", "Борис"]) == ["Ali", "Zafar", "Абдулла", "Борис"]
    # whitespace characters share one weight; ignorables vanish
    assert _levels_without_original("a b") == _levels_without_original("a\tb") == \
        _levels_without_original("a" + chr(0xA0) + "b")
    assert _levels_without_original("a" + ZWJ + "b") == _levels_without_original("ab") == \
        _levels_without_original("a" + SHY + "b")


def test_accents_and_compatibility_letters_sort_with_base_letters() -> None:
    assert sort_values(["Ezoza", "Émile", "Elena", "Fotima", "Davron"]) == ["Davron", "Elena", "Émile", "Ezoza",
                                                                           "Fotima"]
    assert _levels_without_original("émile") == _levels_without_original("e" + ACUTE + "mile")
    assert _levels_without_original("ﬁle") == _levels_without_original("file")
    assert _levels_without_original("Ａli") == _levels_without_original("Ali")
    assert _levels_without_original("Straße") == _levels_without_original("Strasse")
    assert sort_values(["Strat", "Straße", "Strasse", "Strap"]) == ["Strap", "Strasse", "Straße", "Strat"]
    # level 4 decides between canonically different spellings
    assert kv("é") != kv("e" + ACUTE)


def test_natural_order() -> None:
    assert sort_values(["child10", "child2", "child1", "child20", "child3"]) == \
        ["child1", "child2", "child3", "child10", "child20"]
    assert sort_values(["file10.txt", "file9.txt", "file9a.txt"]) == ["file9.txt", "file9a.txt", "file10.txt"]
    assert sort_values(["v1.10", "v1.9", "v1.10.1", "v2"]) == ["v1.9", "v1.10", "v1.10.1", "v2"]
    assert sort_values(["x10y10", "x10y2", "x9y99"]) == ["x9y99", "x10y2", "x10y10"]
    # ties: fewer leading zeros first
    assert sort_values(["a001", "a1", "a01", "a2", "a0", "a00"]) == ["a0", "a00", "a1", "a01", "a001", "a2"]
    # very long runs (digit count >= 94 uses the long length form)
    long_runs = ["a" + "9" * 93, "a" + "1" + "0" * 93, "a" + "9" * 100, "a" + "1" + "0" * 100, "a" + "2" * 1000]
    assert sort_values(long_runs) == long_runs
    # digits before letters, after punctuation; not natural -> character by character
    assert sort_values(["a", "10", "9", "-"]) == ["-", "9", "10", "a"]
    assert sort_values(["child10", "child2"], natural=False) == ["child10", "child2"]
    assert sort_values(["child10", "child2"], collation="unicode") == ["child2", "child10"]
    assert sort_values(["child10", "child2"], collation="unicode", natural=False) == ["child10", "child2"]
    # an apostrophe between digits is ignorable (uz/simple): 1'000 == 1000 at the collation levels
    assert _levels_without_original("1'000") == _levels_without_original("1000")
    # other Unicode decimal digits count as digits in uz/simple
    assert sort_values(["a" + chr(0x663), "a2", "a4"]) == ["a2", "a" + chr(0x663), "a4"]


def test_case_ties_and_case_sensitive_mode() -> None:
    words = ["ABDULLA", "Abdulla", "abdulla", "aBdulla"]
    assert sort_values(words) == ["abdulla", "aBdulla", "Abdulla", "ABDULLA"]
    mixed = ["b", "B", "a", "A", "ab", "aB", "Ab", "AB"]
    assert sort_values(mixed) == ["a", "A", "ab", "aB", "Ab", "AB", "b", "B"]
    assert sort_values(mixed, case_sensitive=True) == ["a", "ab", "aB", "A", "Ab", "AB", "b", "B"]
    assert sort_values(["aC", "Ab"], case_sensitive=True) == ["aC", "Ab"]
    assert sort_values(["aC", "Ab"]) == ["Ab", "aC"]
    # s < S < ... < sh < Sh = SH (merged letters take the first letter's case; then level 4)
    assert sort_values(["Sh", "sh", "SH", "sa", "Sz"], case_sensitive=True) == ["sa", "Sz", "sh", "SH", "Sh"]
    assert sort_values(["Ё", "ё", "Е", "е"], case_sensitive=True) == ["е", "Е", "ё", "Ё"]
    assert sort_values(["Ω", "ω"], case_sensitive=True) == ["Ω", "ω"]  # other scripts: code point order
    assert sort_values(["Ω", "ω"]) == ["ω", "Ω"]
    assert sort_values(["B", "a", "A", "b"], collation="unicode") == ["a", "A", "b", "B"]
    assert sort_values(["B", "a", "A", "b"], collation="unicode", case_sensitive=True) == ["A", "B", "a", "b"]


def test_unicode_collation_is_casefolded_code_point_order() -> None:
    words = ["b", "Á", "a", "z", "é", "ß", "st", "SS", "Ω", "中", "_", "1"]
    expected = sorted(words, key=lambda w: (w.casefold(), w))
    natural_free = sort_values(words, collation="unicode", natural=False)
    assert [w.casefold() for w in natural_free] == [w.casefold() for w in expected]


def test_strings_with_control_and_surrogate_characters() -> None:
    values = ["a\x00", "a", "a\x01", "a\x00b", "a\x01b", "a\x02", "\x00", "", chr(0xD800), chr(0xDFFF) + "x",
              "a" + chr(0xD800)]
    for spec in ALL_SPECS:
        keys = [encode_value(v, spec) for v in values]
        assert len(set(keys)) == len(values)
        for v, k in zip(values, keys):
            raw = codec.dumps(v)
            assert encode_token(raw, spec) == k, (v, spec)
    assert sort_values(["a\x01", "a\x00", "a"]) == ["a", "a\x00", "a\x01"]


# =======================================================================================
# prefix-freeness, compose
# =======================================================================================
def _assert_prefix_free(keys: list[bytes]) -> None:
    uniq = sorted(set(keys))
    for k1, k2 in zip(uniq, uniq[1:]):
        assert not k2.startswith(k1), (k1, k2)  # sorted: a proper prefix would be adjacent


def test_keys_are_prefix_free_and_distinct() -> None:
    rng = random.Random(11)
    strings = list(dict.fromkeys(
        [""] + jsongen.EDGE_STRINGS + [jsongen.rand_string(rng) for _ in range(300)]
        + ["sh", "s", "s'h", "shh", "a1", "a01", "a10", "a" + "1" * 100, "1'000", "e" + ACUTE, "é", "\x00", "\x01"]
    ))
    numbers = list(dict.fromkeys(jsongen.EDGE_NUMBER_TOKENS + [str(rng.randint(-10**30, 10**30)) for _ in range(100)]
                                 + [repr(rng.uniform(-1e6, 1e6)) for _ in range(100)]))
    for spec in ALL_SPECS:
        skeys = [encode_value(s, spec) for s in strings]
        assert len(set(skeys)) == len(strings), spec  # distinct strings -> distinct keys
        nkeys = [encode_token(t.encode(), spec) for t in numbers]
        others = [encode_value(v, spec) for v in (None, MISSING, True, False, [], {}, [1], {"a": [None]})]
        _assert_prefix_free(skeys + nkeys + others)


def test_compose_multi_field_mixed_directions() -> None:
    rng = random.Random(21)
    cities = ["Toshkent", "Тошкент", "Samarqand", "Buxoro", None, MISSING]
    records = []
    for i in range(400):
        rec = {"name": rng.choice(["Ali", "ali", "Vali", "G'ani", "Shahzod", "child2", "child10"])}
        city = rng.choice(cities)
        if city is not MISSING:
            rec["city"] = city
        if rng.random() < 0.85:
            rec["age"] = rng.choice([rng.randint(18, 30), rng.randint(18, 30) + 0.5, JsonFloat("1e400"), None])
        records.append(rec)
    specs = [SortKey("city", nulls="first"), SortKey("age", descending=True), SortKey("name")]

    composed = [compose([encode_value(s.extract(r), s) for s in specs], specs) for r in records]
    assert composed == [record_key(r, specs) for r in records]
    by_key = sorted(range(len(records)), key=lambda i: (composed[i], i))

    def ref(i: int, j: int) -> int:
        for s in specs:
            c = ref_value_cmp(s.extract(records[i]), s.extract(records[j]), s)
            if c:
                return c
        return (i > j) - (i < j)

    assert by_key == sorted(range(len(records)), key=functools.cmp_to_key(ref))
    # raw-token path gives the same composed keys
    raw_records = [codec.dumps(r) for r in records]
    for r, raw in zip(records, raw_records):
        parsed = codec.loads(raw)
        toks = []
        for s in specs:
            v = s.extract(parsed)
            toks.append(encode_token(None if v is MISSING else codec.dumps(v), s))
        assert compose(toks, specs) == record_key(r, specs)
    with pytest.raises(ValueError):
        compose([b"\x10"], specs)


# =======================================================================================
# encode_token == encode_value
# =======================================================================================
@pytest.mark.parametrize("style", ["compact", "pretty2", "tab", "messy", "escaped", "messy_escaped"])
def test_token_equals_value_over_random_json(style: str) -> None:
    rng = random.Random(sum(map(ord, style)))
    specs = ALL_SPECS[::5]
    for i in range(250):
        value = jsongen.rand_value(rng, 0, 3)
        raw = jsongen.encode(value, style, seed=i)
        parsed = codec.loads(raw)
        for spec in specs:
            key = encode_token(raw, spec)
            assert key == encode_value(parsed, spec), (raw, spec)
            assert key == encode_value(value, spec), (raw, spec)


def test_token_equals_value_for_edge_tokens() -> None:
    tokens = jsongen.EDGE_NUMBER_TOKENS + [codec.dumps(s).decode() for s in jsongen.EDGE_STRINGS] + [
        '"\\u0041\\u00e9\\ud83d\\ude00"', '"\\ud800"', '"\\/\\b\\f\\n\\r\\t\\"\\\\"', "true", "false", "null",
        "[]", "{}", '[1, "a", null]', '{"k": {"n": 1.10}}',
    ]
    for spec in ALL_SPECS:
        for t in tokens:
            assert encode_token(t.encode(), spec) == encode_value(codec.loads(t), spec), (t, spec)
        assert encode_token(b"  12 \n", spec) == encode_token(b"12", spec)
        assert encode_token(bytearray(b'"x"'), spec) == encode_token(memoryview(b'"x"'), spec) == encode_value("x", spec)
        assert encode_token(None, spec) == encode_value(MISSING, spec)


@pytest.mark.parametrize("bad", [b"", b"   ", b"tru", b"nul", b"01", b"1.", b".5", b"-", b"+1", b"1e", b'"abc',
                                 b'"a\\x"', b'"a"b"', b'"', b"[1,", b"{1}", b"x", b"NaN", b"Infinity",
                                 b"1 2", b'"a" "b"'])
def test_invalid_tokens_raise_value_error(bad: bytes) -> None:
    with pytest.raises(ValueError):
        encode_token(bad, SortKey("x"))


def test_unsupported_python_values_raise_type_error() -> None:
    for bad in ({1, 2}, object(), b"bytes"):
        with pytest.raises(TypeError):
            encode_value(bad, SortKey("x"))


@settings(max_examples=300, **HYP)
@given(st.text(alphabet=st.characters(codec="ascii"), max_size=30), st.sampled_from(COMBOS))
def test_ascii_fast_path_equals_general_path(text: str, combo: dict) -> None:
    payload, payload_ascii = sortkeys._string_payload_fns(combo["collation"], combo["natural"],
                                                          combo["case_sensitive"])
    general = payload(text, False)
    assert payload(text) == general
    if "\x00" not in text and "\x01" not in text:
        assert payload_ascii(text.encode("ascii")) == general


# =======================================================================================
# independent reference implementation of the rules (module docstring)
# =======================================================================================
REF_CYR = "абвгдеёжзийклмнопрстуфхцчшщъыьэюяўқғҳ"
REF_PUNCT = [c for c in map(chr, range(0x21, 0x7F)) if not c.isalnum() and c not in "'`"]
CTRL, WS, PUNCT, PUNCT_OTHER, DIGIT, LATIN, LATIN_OTHER, CYR, CYR_OTHER, OTHER = range(10)
APOS, COMB = "apos", "comb"
L_O, L_G, L_S, L_C, L_H, L_N = (ord(ch) - 97 for ch in "ogschn")
L_OG, L_GG, L_SH, L_CH, L_NG = 26, 27, 28, 29, 30


def ref_units(c: str) -> list[tuple]:
    """(group, value, upper) of one character of the test alphabet (uz/simple collations)."""
    if c in APOSTROPHE_VARIANTS:
        return [(APOS, 0, 0)]
    if c in (ACUTE, BREVE):
        return [(COMB, 0, 0)]
    if c in (ZWJ, SHY):
        return []
    if c in (" ", "\t", chr(0xA0)):
        return [(WS, 0, 0)]
    if c in ("\x00", "\x01", "\x07"):
        return [(CTRL, 0, 0)]
    if c in REF_PUNCT:
        return [(PUNCT, REF_PUNCT.index(c), 0)]
    if c in (chr(0x2014), chr(0xAB), chr(0x1F600), chr(0xBD)):
        return [(PUNCT_OTHER, 0, 0)]
    if c in "0123456789":
        return [(DIGIT, int(c), 0)]
    if c == chr(0x663):
        return [(DIGIT, 3, 0)]
    if c == "ß":
        return [(LATIN, L_S, 0), (LATIN, L_S, 0)]
    if c == chr(0xFB01):  # ﬁ
        return [(LATIN, ord("f") - 97, 0), (LATIN, ord("i") - 97, 0)]
    if c == chr(0x1C5):  # ǅ
        return [(LATIN, ord("d") - 97, 1), (LATIN, ord("z") - 97, 0)]
    if c == chr(0xFF21):  # fullwidth A
        return [(LATIN, 0, 1)]
    if c in (chr(0x259), chr(0x18F)):  # schwa
        return [(LATIN_OTHER, 0, int(c == chr(0x18F)))]
    base = unicodedata.normalize("NFD", c)[0]
    if base.isascii() and base.isalpha():
        return [(LATIN, ord(base.lower()) - 97, int(base.isupper()))]
    low = c.lower()
    up = int(c != low)
    if 0x400 <= ord(low) <= 0x52F:
        return [(CYR, REF_CYR.index(low), up)] if low in REF_CYR else [(CYR_OTHER, ord(low), up)]
    if unicodedata.category(c).startswith("L"):
        return [(OTHER, (ord(low), ord(c)), up)]
    raise AssertionError(f"character outside the test alphabet: {c!r}")


def _merge(units: list[list], firsts: dict[int, int], second) -> list[list]:
    out, i = [], 0
    while i < len(units):
        u = units[i]
        if i + 1 < len(units) and u[0] == LATIN and u[1] in firsts and second(units[i + 1]):
            nxt = units[i + 1]
            out.append([LATIN, firsts[u[1]], u[2], u[3] + nxt[3]])
            i += 2
        else:
            out.append(u)
            i += 1
    return out


def ref_string_key(s: str, collation: str, natural: bool, cs: bool) -> tuple:
    """(primary, leading zeros, case flags, original) - compared as Python sequences."""
    units: list[list] = []  # [group, value, upper, flags]
    if collation == "unicode":
        for c in s:
            up = 0 if cs else int(unicodedata.category(c) in ("Lu", "Lt"))
            for d in (c if cs else c.casefold()):
                if "0" <= d <= "9":
                    units.append([DIGIT, int(d), 0, [3]])
                else:
                    units.append(["cp", ord(d), up, [up + 1]])
    else:
        if any(unicodedata.category(c) in ("Mn", "Me") for c in s):
            s_units = unicodedata.normalize("NFC", s)
        else:
            s_units = s
        for c in s_units:
            for group, value, up in ref_units(c):
                if group == COMB:
                    continue
                if group == APOS:
                    if collation == "uz":
                        units.append([APOS, 0, 0, []])
                    continue
                units.append([group, value, up, [3] if group == DIGIT else [up + 1]])
        if collation == "uz":
            units = _merge(units, {L_O: L_OG, L_G: L_GG}, lambda u: u[0] == APOS)
            units = _merge(units, {L_S: L_SH, L_C: L_CH}, lambda u: u[0] == LATIN and u[1] == L_H)
            units = _merge(units, {L_N: L_NG}, lambda u: u[0] == LATIN and u[1] == L_G)
            units = [u for u in units if u[0] != APOS]
    zeros: list[int] = []
    if natural:
        merged, i = [], 0
        while i < len(units):
            if units[i][0] == DIGIT:
                j = i
                while j < len(units) and units[j][0] == DIGIT:
                    j += 1
                digits = "".join(str(units[k][1]) for k in range(i, j))
                sig = digits.lstrip("0")
                zeros.append(len(digits) - len(sig))
                merged.append(["run", (len(sig), sig), 0, sum((units[k][3] for k in range(i, j)), [])])
                i = j
            else:
                merged.append(units[i])
                i += 1
        units = merged
    primary = []
    for group, value, up, _flags in units:
        if collation == "unicode":
            if group == "run":
                primary.append((0x30,) + value)
            elif group == DIGIT:
                primary.append((0x30 + value,))
            else:
                primary.append((value,))
            continue
        if group == "run":
            primary.append((DIGIT, value))
        elif group in (LATIN, CYR):
            primary.append((group, 2 * value + up if cs else value))
        elif group == CYR_OTHER:
            primary.append((group, 2 * value + up if cs else value))
        elif group == LATIN_OTHER:
            primary.append((group, up if cs else 0))
        elif group == OTHER:
            primary.append((group, value[1] if cs else value[0]))
        else:
            primary.append((group, value))
    flags = [] if cs else [f for u in units for f in u[3]]
    return (primary, zeros, flags, s)


TYPE_RANK = {"number": 0, "string": 1, "bool": 2, "array": 3, "object": 4}


def _cmp(a, b) -> int:
    return (a > b) - (a < b)


def _decimal(v) -> Decimal:
    if isinstance(v, (JsonFloat, JsonInt)):
        return Decimal(v.raw)
    if isinstance(v, Decimal):
        return v
    if isinstance(v, int):
        return Decimal(v)
    return Decimal(repr(float(v)))


def ref_value_cmp(a, b, spec: SortKey) -> int:
    """Reference comparator of two field values (MISSING allowed) under ``spec``."""

    def placement(v) -> int:
        if spec.nulls == "last":
            return 2 if v is MISSING else 1 if v is None else 0
        return 0 if v is MISSING else 1 if v is None else 2

    pa, pb = placement(a), placement(b)
    if pa != pb:
        return _cmp(pa, pb)
    if a is MISSING or a is None:
        return 0
    ta, tb = codec.json_type(a), codec.json_type(b)
    if ta != tb:
        c = _cmp(TYPE_RANK[ta], TYPE_RANK[tb])
    elif ta == "number":
        c = _cmp(_decimal(a), _decimal(b))
    elif ta == "string":
        c = _cmp(ref_string_key(a, spec.collation, spec.natural, spec.case_sensitive),
                 ref_string_key(b, spec.collation, spec.natural, spec.case_sensitive))
    elif ta == "bool":
        c = _cmp(a, b)
    else:
        c = _cmp(codec.dumps(a), codec.dumps(b))
    return -c if spec.descending else c


def _sorted_indices_by_key(values, spec):
    keys = [encode_value(v, spec) for v in values]
    return sorted(range(len(values)), key=lambda i: (keys[i], i))


def _sorted_indices_by_ref(values, spec):
    def cmp(i, j):
        return ref_value_cmp(values[i], values[j], spec) or _cmp(i, j)

    return sorted(range(len(values)), key=functools.cmp_to_key(cmp))


FRAGMENTS = (
    list("abcdghnosABCDGHNOSzZeEiIklmtuxy") + list("0123456789") * 2
    + [" ", " ", "-", ".", "_", "~", "\t", chr(0xA0)] + APOSTROPHE_VARIANTS
    + list("éÉöÖşŞğçáñ") + ["ß", chr(0xFB01), chr(0xFF21), chr(0x1C5), chr(0x259), chr(0x18F)]
    + list("абвгдеёжзийклмнопрстуфхцчшщъыьэюяўқғҳ") + list("АЁЎҚҒҲШЧИЙ")
    + [chr(0x456), chr(0x406), chr(0x491), chr(0x4E9), chr(0x4E8)]
    + [chr(0x3B1), chr(0x3A9), chr(0x4E2D), chr(0x1F600), chr(0x10400), chr(0x10428)]
    + [chr(0x2014), chr(0xAB), chr(0xBD), "\x00", "\x01", "\x07"]
    + [ACUTE, BREVE, ZWJ, SHY, chr(0x663)]
    + ["sh", "Sh", "SH", "sH", "ch", "Ch", "ng", "NG", "o'", "O'", f"o{OKINA}", "g'", f"G{OKINA}", "s'h", "n'g",
       f"ng{OKINA}", "00", "007", "12", "e" + ACUTE, "и" + BREVE, "у" + BREVE, "1'000", "child", "Child"]
)
strings_st = st.lists(st.sampled_from(FRAGMENTS), max_size=7).map("".join)


@settings(max_examples=500, **HYP)
@given(st.lists(strings_st, min_size=2, max_size=9), st.sampled_from(COMBOS), st.booleans())
def test_strings_sort_like_reference_comparator(strings: list[str], combo: dict, descending: bool) -> None:
    spec = SortKey("s", descending=descending, **combo)
    assert _sorted_indices_by_key(strings, spec) == _sorted_indices_by_ref(strings, spec)
    for s in strings:  # the raw-token path agrees
        assert encode_token(codec.dumps(s), spec) == encode_value(s, spec)


NUMBER_TOKENS_ST = st.from_regex(r"-?(0|[1-9][0-9]{0,22})(\.[0-9]{1,12})?([eE][+-]?[0-9]{1,3})?", fullmatch=True)
scalar_st = st.one_of(
    st.none(),
    st.just(MISSING),
    st.booleans(),
    st.integers(min_value=-(2**80), max_value=2**80),
    st.floats(allow_nan=False, allow_infinity=False),
    NUMBER_TOKENS_ST.map(codec.loads),
    strings_st,
)
json_st = st.recursive(
    scalar_st.filter(lambda v: v is not MISSING),
    lambda children: st.lists(children, max_size=3) | st.dictionaries(st.text("abc", max_size=2), children,
                                                                      max_size=3),
    max_leaves=6,
)
field_st = st.one_of(st.just(MISSING), json_st)


@settings(max_examples=400, **HYP)
@given(st.lists(field_st, min_size=2, max_size=10), st.sampled_from(COMBOS), st.booleans(),
       st.sampled_from(["last", "first"]))
def test_mixed_values_sort_like_reference_comparator(values, combo, descending, nulls) -> None:
    spec = SortKey("v", descending=descending, nulls=nulls, **combo)
    assert _sorted_indices_by_key(values, spec) == _sorted_indices_by_ref(values, spec)
    for v in values:
        token = None if v is MISSING else codec.dumps(v)
        assert encode_token(token, spec) == encode_value(v, spec)


@settings(max_examples=150, **HYP)
@given(st.lists(st.fixed_dictionaries({}, optional={"a": field_st.filter(lambda v: v is not MISSING),
                                                     "b": field_st.filter(lambda v: v is not MISSING)}),
                min_size=2, max_size=10),
       st.booleans(), st.booleans(), st.sampled_from(["last", "first"]))
def test_composed_keys_sort_like_reference_comparator(records, desc_a, desc_b, nulls) -> None:
    specs = [SortKey("a", descending=desc_a, nulls=nulls), SortKey("b", descending=desc_b, collation="simple")]
    keys = [record_key(r, specs) for r in records]
    by_key = sorted(range(len(records)), key=lambda i: (keys[i], i))

    def cmp(i, j):
        for s in specs:
            c = ref_value_cmp(s.extract(records[i]), s.extract(records[j]), s)
            if c:
                return c
        return _cmp(i, j)

    assert by_key == sorted(range(len(records)), key=functools.cmp_to_key(cmp))


def test_reference_alphabet_classification_matches_module() -> None:
    """Sanity check of the test oracle against ``char_units`` on its alphabet."""
    kinds = {"apos": APOS, "comb": COMB, "ctrl": CTRL, "ws": WS, "punct": PUNCT, "punct_other": PUNCT_OTHER,
             "digit": DIGIT, "latin": LATIN, "latin_other": LATIN_OTHER, "cyr": CYR, "cyr_other": CYR_OTHER,
             "other": OTHER}
    for c in {c for frag in FRAGMENTS for c in frag}:
        ref = ref_units(c)
        mod = sortkeys.char_units(c)
        assert len(ref) == len(mod), c
        for (group, value, up), (kind, mvalue, mup, _cp) in zip(ref, mod):
            assert kinds[kind] == group, c
            if group in (LATIN, LATIN_OTHER, CYR, CYR_OTHER, OTHER):
                assert int(mup) == up, c
            if group in (PUNCT, DIGIT, LATIN, CYR, CYR_OTHER):
                assert mvalue == value, c
            if group == OTHER:
                assert mvalue == value[0], c


# =======================================================================================
# SortKey API
# =======================================================================================
def test_sortkey_parse_and_text() -> None:
    k = SortKey.parse("-id")
    assert k.path == "id" and k.descending and k.kind == "value"
    assert SortKey.parse(" + name ").descending is False and SortKey.parse("+name").path == "name"
    assert SortKey.parse("contact.address.city", nulls="first").nulls == "first"
    assert SortKey.parse("-x", descending=True).descending
    assert SortKey.parse("$key").kind == "key" and SortKey.parse("-$index").kind == "index"
    for text in ("id", "-id", "contact.address.city", "-tags[0]", "$", "$key", '-["-x"]', '["+y"]'):
        assert SortKey.parse(SortKey.parse(text).to_text()) == SortKey.parse(text), text
    assert SortKey('["-x"]').path == '["-x"]'
    assert SortKey("-x").to_text() == '["-x"]'  # a key really named "-x"


def test_sortkey_validation_errors(lang_en) -> None:
    with pytest.raises(UserInputError) as exc:
        SortKey("x", nulls="middle")
    assert exc.value.key == "sortkeys.err.bad_nulls" and "'first' or 'last'" in str(exc.value)
    with pytest.raises(UserInputError) as exc:
        SortKey("x", collation="tr")
    assert exc.value.key == "sortkeys.err.bad_collation" and exc.value.hint
    with pytest.raises(UserInputError) as exc:
        SortKey("x", descending="yes")
    assert exc.value.key == "sortkeys.err.bad_flag" and "descending" in str(exc.value)
    for bad in ("", "   "):
        with pytest.raises(UserInputError) as exc:
            SortKey(bad)
        assert exc.value.key == "sortkeys.err.empty_field"
    with pytest.raises(UserInputError) as exc:
        SortKey.parse("-")
    assert exc.value.key == "sortkeys.err.empty_field"
    with pytest.raises(UserInputError) as exc:
        SortKey.parse("x", descending="yes")
    assert exc.value.key == "sortkeys.err.bad_flag"
    with pytest.raises(UserInputError) as exc:
        SortKey("x", natural=1)  # type: ignore[arg-type]
    assert exc.value.key == "sortkeys.err.bad_flag"
    with pytest.raises(UserInputError) as exc:
        SortKey(123)  # type: ignore[arg-type]
    assert exc.value.key == "sortkeys.err.path_type"
    with pytest.raises(UserInputError) as exc:
        SortKey("a..b")
    assert exc.value.key == "sortkeys.err.descendants"
    with pytest.raises(UserInputError):
        SortKey("**.id")
    with pytest.raises(PathSyntaxError):
        SortKey("a[")
    with pytest.raises(UserInputError):
        collation_key("x", collation="xx")
    # normalisation of case and whitespace
    k = SortKey(" name ", nulls="FIRST", collation="Unicode")
    assert (k.path, k.nulls, k.collation) == ("name", "first", "unicode")


def test_sortkey_messages_are_localized(lang_uz) -> None:
    with pytest.raises(UserInputError) as exc:
        SortKey("x", nulls="middle")
    text_uz = str(exc.value)
    assert "null" in text_uz and "bo'lishi kerak" in text_uz
    assert SortKey("name").describe() == "name (o'sish tartibida)"
    assert describe_keys([SortKey("name"), SortKey("id", descending=True)]) == \
        "name (o'sish tartibida), id (kamayish tartibida)"
    set_language("en")
    assert str(exc.value).startswith("The place of null and missing values")
    assert SortKey("id", descending=True).describe() == "id (descending)"


def test_sortkey_value_access_and_core_path() -> None:
    rec = {"a": {"b": [10, 20]}, "tags": ["x", "y"], "items": [{"p": 3}, {"p": 1}], "$key": "literal"}
    assert SortKey("a.b[1]").extract(rec) == 20
    assert SortKey("tags[*]").extract(rec) == "x"  # wildcard: first match in document order
    assert SortKey("items[*].p").extract(rec) == 3
    assert SortKey("items[*].q").extract(rec) is MISSING
    assert SortKey("missing.path").extract(rec) is MISSING
    assert SortKey("$").extract(rec) is rec
    assert SortKey('["$key"]').extract(rec) == "literal"
    assert SortKey("$key").extract(rec, key="id7") == "id7" and SortKey("$key").extract(rec) is MISSING
    assert SortKey("$index").extract(rec, index=5) == 5 and SortKey("$index").extract(rec) is MISSING
    assert SortKey("a.b[1]").core_path() == [("key", "a"), ("key", "b"), ("index", 1)]
    assert SortKey("items[*].p").core_path() == [("key", "items"), ("any_index",), ("key", "p")]
    assert SortKey("$key").core_path() is None and SortKey("$index").pattern is None
    keys = [record_key(rec, [SortKey("$key"), SortKey("$index", descending=True)], key=k, index=i)
            for k, i in (("b", 1), ("a", 2), ("a", 1))]
    assert sorted(range(3), key=lambda i: keys[i]) == [1, 2, 0]


def test_sortkey_is_immutable_hashable_and_picklable() -> None:
    k = SortKey("name", descending=True, nulls="first", natural=False, collation="simple", case_sensitive=True)
    assert pickle.loads(pickle.dumps(k)) == k and copy.deepcopy(k) == k and copy.copy(k) == k
    assert hash(k) == hash(SortKey("name", True, "first", False, "simple", True))
    with pytest.raises(AttributeError):
        k.path = "other"  # type: ignore[misc]
    clone = pickle.loads(pickle.dumps(k))
    assert clone.encode_value("Ab") == k.encode_value("Ab") == encode_value("Ab", k)
    assert token_encoder(k)(b'"Ab"') == value_encoder(k)("Ab") == k.encode_token(b'"Ab"')


def test_collation_key_for_object_keys() -> None:
    keys = ["name", "Name", "id", "child10", "child2", f"o{OKINA}qituvchi", "shahar", "zip", "Ёш", "_meta"]
    # "ch" is one letter after "sh" in the uz collation
    assert sorted(keys, key=collation_key) == ["_meta", "id", "name", "Name", "zip", f"o{OKINA}qituvchi", "shahar",
                                               "child2", "child10", "Ёш"]
    assert sorted(keys, key=lambda k: collation_key(k, "simple")) == ["_meta", "child2", "child10", "id", "name",
                                                                       "Name", f"o{OKINA}qituvchi", "shahar",
                                                                       "zip", "Ёш"]
    assert collation_key("a", "unicode", False, True) == sortkeys._string_payload_fns("unicode", False, True)[0]("a")


def test_translate_table_size_is_bounded(monkeypatch: pytest.MonkeyPatch) -> None:
    spec = SortKey("x", collation="simple")
    table = sortkeys._table("simple", False, True)
    encode_value("warm up", spec)
    size = len(table)
    monkeypatch.setattr(sortkeys, "_TABLE_MAX", size)
    text = "".join(chr(cp) for cp in range(0x5000, 0x5000 + 300)) + "Ўзбек"
    sortkeys.clear_caches()
    capped = encode_value(text, spec)
    assert len(table) == size  # nothing new was cached ...
    monkeypatch.undo()
    sortkeys.clear_caches()
    assert encode_value(text, spec) == capped  # ... and the result is the same when cached
    assert len(table) > size


def test_clear_caches_keeps_results() -> None:
    spec = SortKey("x")
    before = [encode_token(b'"Toshkent"', spec), encode_value("Samarqand", spec)]
    sortkeys.clear_caches()
    assert [encode_token(b'"Toshkent"', spec), encode_value("Samarqand", spec)] == before


def test_performance_smoke() -> None:
    """Loose bound only (precise numbers are reported by the benchmark, not asserted)."""
    rng = random.Random(4)
    words = ["Abdulla", "Karimov", "Toshkent", "Shahzod", f"O{OKINA}tkir", "Тошкент", "child", "Nodira"]
    tokens = [codec.dumps(f"{rng.choice(words)} {rng.choice(words)} {i}") for i in range(20000)]
    numbers = [str(rng.randint(-10**9, 10**9)).encode() for _ in range(20000)]
    enc = token_encoder(SortKey("x"))
    sortkeys.clear_caches()
    t0 = time.perf_counter()
    for t in tokens:
        enc(t)
    for t in numbers:
        enc(t)
    per_key = (time.perf_counter() - t0) / 40000
    assert per_key < 25e-6, f"{per_key * 1e6:.1f} us per key"
