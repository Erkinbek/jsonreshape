"""jsonreshape.diff: the value diff (docs/ARCHITECTURE.md §14.6, s5/map_repair_diff.md §4.1-§4.3).

* objects (added / removed / changed, order of the changes, nested paths), type changes,
  empty containers, duplicate keys (dict semantics), very deep values (no recursion);
* arrays by index, by content (``auto``: a front insertion does not cascade, deletions,
  replacements paired position-wise, duplicates, long arrays through the patience path) and
  by key (moves on a longest increasing subsequence, nested changes of moved elements,
  duplicate element keys, fallback to ``auto``, ``PATH=FIELD`` rules with wildcards);
* number semantics: exact values (``1 == 1.0``, ``-0 == 0``, ``1e400 != 2e400``, big ints)
  and ``numbers="text"`` (``1.10`` vs ``1.1`` reported); ``1 != true``; strings with escapes,
  Uzbek Latin/Cyrillic and apostrophe variants;
* ``max_changes`` truncation with complete counts; :class:`DiffOptions` checks;
* the alignment itself (``_align``): every op sequence covers both sides in order, equal runs
  are equal, minimal edits for simple cases, SequenceMatcher and patience agree on the edit
  counts; properties over random values (equal iff canonically equal, re-serialized values
  are equal).
"""

from __future__ import annotations

import random
from collections import Counter
from typing import Any

import pytest

import jsongen
from jsonreshape import codec, diff
from jsonreshape.codec import JsonFloat, JsonInt
from jsonreshape.diff import Change, DiffOptions, Omitted, ValueDiff, diff_values
from jsonreshape.dupes import canonical_bytes
from jsonreshape.errors import PathSyntaxError, UserInputError
from jsonreshape.i18n import has, t
from jsonreshape.paths import MISSING, PathPattern
from jsonreshape.tasks import format_bytes


# ----------------------------------------------------------------------------------
# helpers
# ----------------------------------------------------------------------------------
def J(text: str) -> Any:
    return codec.loads(text)


def txt(v: Any) -> str | None:
    if v is MISSING:
        return None
    if isinstance(v, Omitted):
        return f"<omitted {v.kind} {v.size}>"
    return codec.dumps(v).decode("utf-8")


def show(vd: ValueDiff) -> list[tuple]:
    return [(c.op, c.a_path, c.b_path, txt(c.a), txt(c.b)) for c in vd.changes]


def d(a: str, b: str, **kw: Any) -> list[tuple]:
    return show(diff_values(J(a), J(b), DiffOptions(**kw) if kw else None))


# ----------------------------------------------------------------------------------
# objects and scalars
# ----------------------------------------------------------------------------------
def test_objects_added_removed_changed_in_document_order() -> None:
    a = '{"id": 1, "name": "Ali", "fax": "123", "contact": {"city": "Toshkent", "zip": 100}, "z": true}'
    b = '{"id": 1, "contact": {"zip": 100, "city": "Samarqand", "street": "Amir Temur"}, "name": "Vali", ' \
        '"z": true, "tags": []}'
    assert d(a, b) == [
        ("changed", ("name",), ("name",), '"Ali"', '"Vali"'),
        ("removed", ("fax",), None, '"123"', None),
        ("changed", ("contact", "city"), ("contact", "city"), '"Toshkent"', '"Samarqand"'),
        ("added", None, ("contact", "street"), None, '"Amir Temur"'),
        ("added", None, ("tags",), None, "[]"),
    ]
    vd = diff_values(J(a), J(b))
    assert vd.counts == {"added": 2, "removed": 1, "changed": 2, "moved": 0}
    assert vd.total == 5 and not vd.equal and not vd.truncated
    assert diff_values(J(a), J(a)).equal and diff_values(J(a), J(a)).changes == []


def test_member_order_and_duplicate_keys_do_not_matter() -> None:
    assert d('{"a": 1, "b": {"x": [1, {"p": 1, "q": 2}]}}', '{"b": {"x": [1, {"q": 2, "p": 1}]}, "a": 1}') == []
    assert d('{"a": 1, "a": 2}', '{"a": 2}') == []                     # the last duplicate wins
    assert d('{"a": 1, "a": 2}', '{"a": 1}') == [("changed", ("a",), ("a",), "2", "1")]


def test_type_changes_are_changes_of_the_whole_value() -> None:
    assert d('{"v": {"a": 1}}', '{"v": [1]}') == [("changed", ("v",), ("v",), '{"a":1}', "[1]")]
    assert d('[1, "1", true, null, 0, false, ""]', '[true, 1, 1, false, false, null, null]', array_match="index") == [
        ("changed", (0,), (0,), "1", "true"),
        ("changed", (1,), (1,), '"1"', "1"),
        ("changed", (2,), (2,), "true", "1"),
        ("changed", (3,), (3,), "null", "false"),
        ("changed", (4,), (4,), "0", "false"),
        ("changed", (5,), (5,), "false", "null"),
        ("changed", (6,), (6,), '""', "null"),
    ]
    assert d('"x"', '{"x": 1}') == [("changed", (), (), '"x"', '{"x":1}')]


def test_empty_containers() -> None:
    assert d("{}", "[]") == [("changed", (), (), "{}", "[]")]
    assert d("[]", "[]") == [] and d("{}", "{}") == []
    assert d("[]", "[1, {}]") == [("added", None, (0,), None, "1"), ("added", None, (1,), None, "{}")]
    assert d('{"a": {}}', '{"a": {"b": []}}') == [("added", None, ("a", "b"), None, "[]")]
    assert d('{"a": [[]]}', '{"a": [[], []]}') == [("added", None, ("a", 1), None, "[]")]


def test_deep_values_need_no_recursion() -> None:
    a: Any = 1
    b: Any = 2
    for _ in range(50000):
        a = {"k": [a]}
        b = {"k": [b]}
    vd = diff_values(a, b)
    assert len(vd.changes) == 1 and vd.changes[0].op == "changed"
    assert len(vd.changes[0].a_path) == 100000 and vd.changes[0].a_path[:4] == ("k", 0, "k", 0)
    assert diff_values(a, a).equal


# ----------------------------------------------------------------------------------
# numbers and strings
# ----------------------------------------------------------------------------------
EQUAL_VALUES = [("1", "1.0"), ("1", "1e0"), ("100", "1E+2"), ("-0", "0"), ("-0.0", "0"), ("0e5", "0"),
                ("1.10", "1.1"), ("12345678901234567890", "12345678901234567890.0"),
                ("12345678901234567890", "1.2345678901234567890e19"), ("1e400", "10E399"), ("0.10", "0.1"),
                ("-1e-400", "-10e-401")]
UNEQUAL_VALUES = [("1e400", "2e400"), ("0.1", "0.10000000000000001"), ("12345678901234567890",
                  "12345678901234567891"), ("9007199254740993", "9007199254740992"), ("1", "-1"), ("1e-400", "0")]


@pytest.mark.parametrize("x, y", EQUAL_VALUES)
def test_numbers_by_exact_value(x: str, y: str) -> None:
    assert d(f"[{x}]", f"[{y}]") == []
    assert d(f'{{"n": {x}}}', f'{{"n": {y}}}') == []
    # numbers="text": compared as written
    want = [] if x == y else [("changed", ("n",), ("n",), x, y)]
    assert d(f'{{"n": {x}}}', f'{{"n": {y}}}', numbers="text") == want


@pytest.mark.parametrize("x, y", UNEQUAL_VALUES)
def test_unequal_numbers(x: str, y: str) -> None:
    for kw in ({}, {"numbers": "text"}):
        assert d(f'{{"n": {x}}}', f'{{"n": {y}}}', **kw) == [("changed", ("n",), ("n",), x, y)]


def test_numbers_text_mode_and_exact_values_in_changes() -> None:
    assert d('{"a": 1.10, "b": 1e400, "c": -0}', '{"a": 1.1, "b": 1E+400, "c": 0}', numbers="text") == [
        ("changed", ("a",), ("a",), "1.10", "1.1"),
        ("changed", ("b",), ("b",), "1e400", "1E+400"),
        ("changed", ("c",), ("c",), "-0", "0"),
    ]
    vd = diff_values(J('{"x": 123456789012345678901234567890}'), J('{"x": 0.1}'))
    c = vd.changes[0]
    assert isinstance(c.a, JsonInt) and c.a.raw == "123456789012345678901234567890" and c.b == 0.1
    vd = diff_values(J('{"x": 12345678901234567890}'), J('{"x": 12345678901234567891}'))
    assert (vd.changes[0].a, vd.changes[0].b) == (12345678901234567890, 12345678901234567891)
    vd = diff_values(J("[1e400]"), J("[-0.0]"), DiffOptions(array_match="index"))
    assert isinstance(vd.changes[0].a, JsonFloat) and vd.changes[0].a.raw == "1e400"
    assert codec.dumps(vd.changes[0].b) == b"-0.0"


def test_numbers_and_literals_are_distinct() -> None:
    for x, y in (("1", "true"), ("0", "false"), ("0", "null"), ('"1"', "1"), ('"null"', "null"), ('"true"', "true")):
        assert d(f"[{x}]", f"[{y}]") != [], (x, y)


def test_strings_exactly() -> None:
    assert d('["A", "\\u0041", "a\\/b", "\\ud83d\\ude00"]', '["\\u0041", "A", "a/b", "😀"]') == []
    pairs = [("Toshkent", "Тошкент"), ("O'zbekiston", "Oʻzbekiston"), ("O'zbekiston", "O‘zbekiston"),
             ("Toshkent", "toshkent"), ("Straße", "STRASSE"), ("é", "é"), ("line sep", "line sep")]
    for x, y in pairs:
        a, b = codec.dumps({"s": x}).decode(), codec.dumps({"s": y}).decode()
        assert d(a, b) == [("changed", ("s",), ("s",), codec.dumps(x).decode(), codec.dumps(y).decode())]
    assert d('{"s": "\\ud800"}', '{"s": "\\ud800"}') == []                # lone surrogates survive
    assert len(d('{"s": "\\ud800"}', '{"s": "\\udc00"}')) == 1


# ----------------------------------------------------------------------------------
# arrays
# ----------------------------------------------------------------------------------
def records(n: int, seed: int) -> list[Any]:
    return [codec.loads(codec.dumps(r)) for r in jsongen.records(n, seed=seed)]


def test_array_index_mode() -> None:
    assert d("[1, 2, 3, 4]", "[1, 3, 4]", array_match="index") == [
        ("changed", (1,), (1,), "2", "3"), ("changed", (2,), (2,), "3", "4"), ("removed", (3,), None, "4", None)]
    assert d("[1]", "[1, 2, 3]", array_match="index") == [("added", None, (1,), None, "2"),
                                                         ("added", None, (2,), None, "3")]


def test_front_insertion_does_not_cascade() -> None:
    recs = records(120, seed=1)
    new = {"id": -1, "name": "yangi"}
    vd = diff_values(recs, [new] + recs)
    assert show(vd) == [("added", None, (0,), None, txt(new))]
    vd = diff_values(recs, recs[:60] + [new, new] + recs[60:])
    assert show(vd) == [("added", None, (60,), None, txt(new)), ("added", None, (61,), None, txt(new))]
    vd = diff_values(recs, recs[1:])
    assert show(vd) == [("removed", (0,), None, txt(recs[0]), None)]
    # by index every element would differ
    assert diff_values(recs, [new] + recs, DiffOptions(array_match="index")).total > 100


def test_replacements_are_paired_and_compared_inside() -> None:
    a = [{"id": i, "v": i} for i in range(10)]
    b = [dict(x) for x in a]
    b[3]["v"] = 33
    b[4] = {"id": 4, "v": 4, "new": True}
    del b[7]
    assert show(diff_values(a, b)) == [
        ("changed", (3, "v"), (3, "v"), "3", "33"),
        ("added", None, (4, "new"), None, "true"),
        ("removed", (7,), None, txt(a[7]), None),
    ]
    # elements aligned to other indices keep their own paths on each side
    b = [{"id": 99}] + [dict(x) for x in a]
    b[6]["v"] = -5
    vd = diff_values(a, b)
    assert show(vd) == [("added", None, (0,), None, '{"id":99}'), ("changed", (5, "v"), (6, "v"), "5", "-5")]


def test_duplicates_in_arrays() -> None:
    assert d("[1, 1, 1]", "[1, 1]") == [("removed", (2,), None, "1", None)]
    assert d("[1, 2, 1, 2]", "[1, 2, 1, 2, 1, 2]") == [("added", None, (4,), None, "1"),
                                                       ("added", None, (5,), None, "2")]
    assert d('["a", "b", "a"]', '["a", "a"]') == [("removed", (1,), None, '"b"', None)]
    # equal values spelled differently are still equal (they only anchor less well)
    assert d("[1, 2, 3]", "[1.0, 2.0, 3.0]") == []


def test_long_arrays_use_patience_and_stay_minimal(monkeypatch) -> None:
    n = 6000                                           # 6000 x 6000 > SM_PRODUCT_MAX: patience
    a = [{"i": i} for i in range(n)]
    b = [dict(x) for x in a]
    b.insert(0, {"i": "first"})
    del b[3001]
    b[4500]["i"] = "changed"
    b.append({"i": "last"})
    vd = diff_values(a, b)
    assert show(vd) == [
        ("added", None, (0,), None, '{"i":"first"}'),
        ("removed", (3000,), None, '{"i":3000}', None),
        ("changed", (4500, "i"), (4500, "i"), "4500", '"changed"'),
        ("added", None, (6000,), None, '{"i":"last"}'),
    ]
    # the same through SequenceMatcher only
    monkeypatch.setattr(diff, "SM_PRODUCT_MAX", 10**12)
    assert show(diff_values(a, b)) == show(vd)
    # beyond PATIENCE_MAX a content-defined sample of the elements anchors: still the minimal edits
    monkeypatch.setattr(diff, "SM_PRODUCT_MAX", 10)
    monkeypatch.setattr(diff, "PATIENCE_MAX", 100)
    assert diff._sample_mask(6001) == 63
    assert show(diff_values(a, b)) == show(vd)
    # nothing in common at all: paired by position
    c = [{"j": i} for i in range(300)]
    vd = diff_values(a[:300], c)
    assert vd.counts == {"added": 300, "removed": 300, "changed": 0, "moved": 0}     # {"i"} vs {"j"} members
    assert [ch.a_path for ch in vd.changes[:2]] == [(0, "i"), None]


def test_array_key_matching_reports_moves() -> None:
    a = [{"id": i, "v": i} for i in range(6)]
    b = [a[0], a[1], {"id": 4, "v": 44}, a[2], a[3], a[5], {"id": 9, "v": 9}]
    opts = DiffOptions(array_match="key", array_keys=("id",))
    assert show(diff_values(a, b, opts)) == [
        ("moved", (4,), (2,), None, None),
        ("changed", (4, "v"), (2, "v"), "4", "44"),
        ("added", None, (6,), None, '{"id":9,"v":9}'),
    ]
    vd = diff_values(a, b, opts)
    assert vd.counts == {"added": 1, "removed": 0, "changed": 1, "moved": 1}
    # a reversed array: everything but one element moved (the LIS keeps one)
    vd = diff_values(a, list(reversed(a)), opts)
    assert vd.counts == {"added": 0, "removed": 0, "changed": 0, "moved": 5}
    # auto would see deletions and insertions instead
    assert diff_values(a, list(reversed(a))).counts["moved"] == 0


def test_array_key_duplicates_and_fallbacks() -> None:
    a = [{"id": 1, "n": "a"}, {"id": 1, "n": "b"}, {"id": 2, "n": "c"}]
    b = [{"id": 2, "n": "c"}, {"id": 1, "n": "b2"}, {"id": 1.0, "n": "a"}, {"id": 1, "n": "x"}]
    opts = DiffOptions(array_keys=("id",))
    assert show(diff_values(a, b, opts)) == [
        ("changed", (0, "n"), (1, "n"), '"a"', '"b2"'),        # the i-th 1 with the i-th 1
        ("changed", (1, "n"), (2, "n"), '"b"', '"a"'),
        ("moved", (2,), (0,), None, None),
        ("added", None, (3,), None, '{"id":1,"n":"x"}'),
    ]
    # an element without the field: the array is aligned by content instead
    b2 = [{"id": 1, "n": "a"}, {"n": "no id"}, {"id": 2, "n": "c"}]
    assert diff_values(a, b2, opts).counts["moved"] == 0
    assert show(diff_values(a, b2, opts)) == show(diff_values(a, b2))
    # non-object elements: same fallback
    assert show(diff_values([1, 2], [2, 1], opts)) == show(diff_values([1, 2], [2, 1]))


def test_array_key_rules_by_path() -> None:
    a = {"orders": [{"oid": 1, "s": 1}, {"oid": 2, "s": 2}], "tags": [{"oid": 1}, {"oid": 2}],
         "groups": [{"items": [{"sku": "x", "q": 1}, {"sku": "y", "q": 2}]}]}
    b = {"orders": [{"oid": 2, "s": 2}, {"oid": 1, "s": 1}], "tags": [{"oid": 2}, {"oid": 1}],
         "groups": [{"items": [{"sku": "y", "q": 2}, {"sku": "x", "q": 3}]}]}
    opts = DiffOptions(array_keys=("orders=oid", ("groups[*].items", "sku")))
    got = show(diff_values(a, b, opts))
    # two swapped elements: one of them is off the longest increasing subsequence (deterministic)
    assert [c for c in got if c[1] and c[1][0] == "orders"] == [("moved", ("orders", 0), ("orders", 1), None, None)]
    assert ("moved", ("groups", 0, "items", 0), ("groups", 0, "items", 1), None, None) in got
    assert ("changed", ("groups", 0, "items", 0, "q"), ("groups", 0, "items", 1, "q"), "1", "3") in got
    assert not any(c[0] == "moved" and (c[1] or c[2])[0] == "tags" for c in got)        # tags: auto
    assert any(c[0] in ("added", "removed") and (c[1] or c[2])[0] == "tags" for c in got)
    assert opts.array_keys[0][0] == PathPattern(("orders",)) and opts.array_keys[0][1] == "oid"
    # a nested key field
    a = [{"m": {"id": 1}, "v": 1}, {"m": {"id": 2}, "v": 2}]
    b = [a[1], {"m": {"id": 1}, "v": 9}]
    assert show(diff_values(a, b, DiffOptions(array_keys=("m.id",)))) == [
        ("moved", (0,), (1,), None, None), ("changed", (0, "v"), (1, "v"), "1", "9")]


# ----------------------------------------------------------------------------------
# truncation, options
# ----------------------------------------------------------------------------------
def test_max_changes_truncates_but_counts_everything() -> None:
    a = {f"k{i}": i for i in range(50)}
    b = {f"k{i}": i + 1 for i in range(50)}
    vd = diff_values(a, b, DiffOptions(max_changes=10))
    assert len(vd.changes) == 10 and vd.truncated and vd.counts["changed"] == 50
    assert [c.a_path for c in vd.changes] == [(f"k{i}",) for i in range(10)]
    vd = diff_values(a, b, DiffOptions(max_changes=0))
    assert vd.changes == [] and vd.truncated and vd.total == 50
    vd = diff_values(a, b, DiffOptions(max_changes=None))
    assert len(vd.changes) == 50 and not vd.truncated
    assert DiffOptions().max_changes == 10000


def test_options_are_checked(lang_en) -> None:
    bad = [({"array_match": "fuzzy"}, "diff.error.array_match"), ({"numbers": "float"}, "diff.error.numbers"),
           ({"max_changes": -1}, "diff.error.max_changes"), ({"max_changes": True}, "diff.error.max_changes"),
           ({"array_match": "key"}, "diff.error.array_key_missing"),
           ({"array_keys": ("",)}, "diff.error.array_key"), ({"array_keys": ("a=",)}, "diff.error.array_key"),
           ({"array_keys": ("id[*]",)}, "diff.error.array_key_wildcard"), ({"array_keys": (5,)}, "diff.error.array_key")]
    for kw, key in bad:
        with pytest.raises(UserInputError) as ei:
            DiffOptions(**kw)
        assert ei.value.key == key, kw
    with pytest.raises(PathSyntaxError):
        DiffOptions(array_keys=("a[=b",))
    o = DiffOptions(array_match=" KEY ", numbers="Text", array_keys="id")
    assert (o.array_match, o.numbers, o.array_keys) == ("key", "text", ((None, "id"),))
    assert DiffOptions(numbers=" Exact").numbers == "value"          # an alias (CLI --numbers exact)
    o = DiffOptions(array_keys=('a["x=y"]=id', "orders = oid", (None, "z")))
    assert [(p.segments if p else None, f) for p, f in o.array_keys] == [(("a", "x=y"), "id"), (("orders",), "oid"),
                                                                        (None, "z")]
    with pytest.raises(TypeError):
        diff_values(1, 2, {"numbers": "text"})  # type: ignore[arg-type]
    assert "auto, key, align" in t("diff.error.mode", value="'x'")


# ----------------------------------------------------------------------------------
# the alignment
# ----------------------------------------------------------------------------------
def check_ops(a: list, b: list, ops: list[tuple]) -> dict[str, int]:
    """The ops cover a and b in order exactly once; equal runs are equal."""
    i = j = 0
    counts = {"eq": 0, "pairs": 0, "del": 0, "ins": 0}
    for op in ops:
        tag = op[0]
        if tag == diff._EQ:
            _, x, y, n = op
            assert (x, y) == (i, j) and a[x:x + n] == b[y:y + n]
            i, j = i + n, j + n
            counts["eq"] += n
        elif tag == diff._PAIRS:
            _, x, y, n = op
            assert (x, y) == (i, j)
            i, j = i + n, j + n
            counts["pairs"] += n
        elif tag == diff._DEL:
            _, x, n = op
            assert x == i
            i += n
            counts["del"] += n
        else:
            _, y, n = op
            assert y == j
            j += n
            counts["ins"] += n
    assert (i, j) == (len(a), len(b))
    return counts


@pytest.mark.parametrize("seed", range(40))
def test_alignment_covers_both_sides(seed: int, monkeypatch) -> None:
    rng = random.Random(seed)
    n = rng.choice([0, 1, 5, 50, 400])
    alphabet = rng.choice([3, 30, 10**9])
    a = [rng.randrange(alphabet) for _ in range(n)]
    b = list(a)
    for _ in range(rng.randrange(0, 12)):
        op = rng.random()
        if op < 0.4 and b:
            del b[rng.randrange(len(b))]
        elif op < 0.8:
            b.insert(rng.randrange(len(b) + 1), rng.randrange(alphabet))
        elif b:
            b[rng.randrange(len(b))] = rng.randrange(alphabet)
    sm = check_ops(a, b, diff._align(a, b))
    monkeypatch.setattr(diff, "SM_PRODUCT_MAX", 0)              # patience everywhere it can
    pt = check_ops(a, b, diff._align(a, b))
    if alphabet == 10**9:                                       # unique elements: both find every match
        assert sm["eq"] == pt["eq"]


def test_alignment_of_arrays_and_bytes() -> None:
    from array import array

    a = array("Q", range(100000))
    b = array("Q", [7] + list(range(100000)))
    assert diff._align(a, b) == [(diff._INS, 0, 1), (diff._EQ, 0, 1, 100000)]
    assert diff._align([b"x", b"y"], [b"y"]) == [(diff._DEL, 0, 1), (diff._EQ, 1, 0, 1)]
    assert diff._lis([3, 1, 2, 5, 4, 6]) in ([1, 2, 3, 5], [1, 2, 4, 5])


def lis_reference(values: list[int]) -> list[int]:
    """The element-wise patience LIS (diff._lis) as values."""
    return [values[k] for k in diff._lis(values)]


@pytest.mark.parametrize("seed", range(30))
def test_patience_anchors_are_the_unique_items_on_the_lis(seed: int) -> None:
    """``_patience`` (C-level, run by run) against a plain reference: the items unique in both
    segments, the element-wise LIS of their B indices in A order, consecutive anchors as runs; items
    repeated on either side never anchor."""
    rng = random.Random(seed)
    n = rng.choice([5, 40, 300, 2000])
    alphabet = rng.choice([4, 60, 10**9])
    a = [rng.randrange(alphabet) for _ in range(n)]
    b = list(a)
    for _ in range(rng.randrange(0, 30)):
        r = rng.random()
        if r < 0.3 and b:
            del b[rng.randrange(len(b))]
        elif r < 0.6:
            b.insert(rng.randrange(len(b) + 1), rng.randrange(alphabet))
        elif r < 0.8 and b:
            b[rng.randrange(len(b))] = rng.randrange(alphabet)
        elif b:                                      # a block move
            k = rng.randrange(len(b))
            block = b[k:k + rng.randrange(1, 20)]
            del b[k:k + len(block)]
            p = rng.randrange(len(b) + 1)
            b[p:p] = block
    alo, blo = rng.randrange(0, 3), rng.randrange(0, 3)
    ahi, bhi = max(alo + 1, len(a) - rng.randrange(0, 3)), max(blo + 1, len(b) - rng.randrange(0, 3))
    a += [10**12] * 3                               # outside the segments: never seen
    b += [10**12 + 1] * 3
    for items_a, items_b in ((a, b), ([b"%d" % x for x in a], [b"%d" % x for x in b])):
        seg_a, seg_b = items_a[alo:ahi], items_b[blo:bhi]
        ca, cb = Counter(seg_a), Counter(seg_b)
        anchors = [(alo + i, blo + seg_b.index(x)) for i, x in enumerate(seg_a) if ca[x] == 1 and cb.get(x) == 1]
        keep = set(lis_reference([j for _, j in anchors]))
        want = [(i, j) for i, j in anchors if j in keep]
        runs = diff._patience(items_a, items_b, alo, ahi, blo, bhi, None)
        got = []
        for i, j, c in runs:
            assert items_a[i:i + c] == items_b[j:j + c]
            got += [(i + t, j + t) for t in range(c)]
        # every LIS anchor is inside a run; joined gaps hold equal items only
        assert set(want) <= set(got)
        assert all(x[0] < y[0] and x[1] < y[1] for x, y in zip(got, got[1:]))


def test_patience_ignores_items_repeated_on_one_side() -> None:
    #       0    1    2    3    4
    a = [b"u", b"d", b"v", b"d", b"w"]          # d twice in A: anchors u, v, w; the gap (d, d) is equal
    b = [b"u", b"v", b"d", b"w", b"x"]
    assert diff._patience(a, b, 0, 5, 0, 5, None) == [(0, 0, 1), (2, 1, 3)]
    assert diff._patience(a, [b"u", b"v", b"q", b"w"], 0, 5, 0, 4, None) == [(0, 0, 1), (2, 1, 1), (4, 3, 1)]
    b2 = [b"x", b"v", b"w", b"v"]               # v twice in B: only w anchors
    assert diff._patience(a, b2, 0, 5, 0, 4, None) == [(4, 2, 1)]
    assert diff._patience([1, 1], [1, 1], 0, 2, 0, 2, None) == []
    assert diff._patience([1, 2, 3], [4, 5], 0, 3, 0, 2, None) == []
    # runs on one diagonal around an equal gap are joined: (u, d, v) == (u, d, v)
    assert diff._patience([b"u", b"d", b"v", b"d"], [b"u", b"d", b"v", b"q"], 0, 4, 0, 4, None) == [(0, 0, 3)]


def lcs_length(a: list, b: list) -> int:
    prev = [0] * (len(b) + 1)
    for x in a:
        cur = [0] * (len(b) + 1)
        for j, y in enumerate(b):
            cur[j + 1] = prev[j] + 1 if x == y else max(prev[j + 1], cur[j])
        prev = cur
    return prev[-1]


@pytest.mark.parametrize("seed", range(20))
def test_myers_gives_a_shortest_edit_script(seed: int) -> None:
    """The fallback without patience anchors (items repeated on both sides): the equal runs are a
    longest common subsequence; over ``max_d`` edits it gives up (None)."""
    rng = random.Random(seed)
    for _ in range(60):
        alphabet = rng.choice([1, 2, 3, 8])
        a = [rng.randrange(alphabet) for _ in range(rng.randrange(1, 60))]
        b = list(a)
        for _ in range(rng.randrange(0, 10)):
            if rng.random() < 0.5 and len(b) > 1:
                del b[rng.randrange(len(b))]
            else:
                b.insert(rng.randrange(len(b) + 1), rng.randrange(alphabet))
        alo, blo = rng.randrange(0, 2), rng.randrange(0, min(2, len(b)))
        ahi, bhi = len(a), len(b)
        if ahi <= alo:
            continue
        lcs = lcs_length(a[alo:ahi], b[blo:bhi])
        ops = diff._myers(a, b, alo, ahi, blo, bhi, 1000, None)
        counts = check_ops(a[alo:], b[blo:], [_rebase(op, alo, blo) for op in ops])
        assert counts["eq"] == lcs
        d_min = (ahi - alo) + (bhi - blo) - 2 * lcs
        assert diff._myers(a, b, alo, ahi, blo, bhi, d_min, None) is not None
        if d_min:
            assert diff._myers(a, b, alo, ahi, blo, bhi, d_min - 1, None) is None


def _rebase(op: tuple, alo: int, blo: int) -> tuple:
    if op[0] in (diff._EQ, diff._PAIRS):
        return (op[0], op[1] - alo, op[2] - blo, op[3])
    return (op[0], op[1] - (alo if op[0] == diff._DEL else blo), op[2])


def test_arrays_of_repeated_items_are_aligned_without_a_cascade(monkeypatch) -> None:
    """No element occurs once (booleans): patience has no anchor, the shortest edit script aligns
    them; beyond MYERS_MAX_D edits they are paired by position."""
    a = [i % 3 == 0 for i in range(5000)]
    b = list(a)
    b.insert(10, True)
    del b[2500]
    b[-3] = not b[-3]
    vd = diff_values(a, b)
    assert vd.counts == {"added": 1, "removed": 1, "changed": 1, "moved": 0}
    assert [(c.op, c.a_path, c.b_path) for c in vd.changes] == [
        ("added", None, (10,)), ("removed", (2499,), None), ("changed", (4997,), (4997,))]
    monkeypatch.setattr(diff, "MYERS_MAX_D", 2)
    assert diff_values(a, b).counts["changed"] > 100
    """Over PATIENCE_MAX items per side a content-defined sample anchors: ints by their low bits,
    other items by their blake2b hash; the same items on both sides; deterministic."""
    from array import array

    assert [diff._sample_mask(n) for n in (0, 10, diff.PATIENCE_MAX, diff.PATIENCE_MAX + 1, 3 * diff.PATIENCE_MAX,
                                           4 * diff.PATIENCE_MAX, 4 * diff.PATIENCE_MAX + 1)] == [0, 0, 0, 1, 3, 3, 7]
    rng = random.Random(7)
    a = [rng.getrandbits(64) for _ in range(5000)]
    b = list(a)
    for _ in range(25):
        b.insert(rng.randrange(len(b) + 1), rng.getrandbits(64))
        del b[rng.randrange(len(b))]
    full = diff._align(a, b)
    monkeypatch.setattr(diff, "SM_PRODUCT_MAX", 64)
    monkeypatch.setattr(diff, "PATIENCE_MAX", 400)
    assert diff._sample_mask(5000) == 15
    for items_a, items_b in ((a, b), (array("Q", a), array("Q", b)), ([b"%x" % x for x in a], [b"%x" % x for x in b])):
        ops = diff._align(items_a, items_b)
        assert ops == diff._align(items_a, items_b)                      # deterministic
        assert check_ops(list(items_a), list(items_b), ops) == check_ops(a, b, full)   # as good as the full one
    # the candidates: low bits zero (ints) / of the blake2b hash (bytes)
    got = [x for x, _ in diff._candidates(a, 0, 5000, 15, True)]
    assert got == [x for x in a if x & 15 == 0] and 250 < len(got) < 400
    tokens = [b"%x" % x for x in a]
    got = [x for x, _ in diff._candidates(tokens, 10, 5000, 15, False)]
    assert got == [x for x in tokens[10:] if diff._hash64(x) & 15 == 0]
    assert [i for _, i in diff._candidates(tokens, 10, 5000, 0, False)] == list(range(10, 5000))


# ----------------------------------------------------------------------------------
# properties over random values
# ----------------------------------------------------------------------------------
def respell(v: Any, rng: random.Random) -> Any:
    """An equal value: other key order, other number spellings."""
    if isinstance(v, dict):
        items = list(v.items())
        rng.shuffle(items)
        return {k: respell(x, rng) for k, x in items}
    if isinstance(v, list):
        return [respell(x, rng) for x in v]
    if isinstance(v, bool) or v is None or isinstance(v, str):
        return v
    if isinstance(v, int) and not isinstance(v, JsonInt) and rng.random() < 0.5:
        return JsonFloat(f"{int(v)}.0")
    return v


@pytest.mark.parametrize("seed", range(25))
def test_random_values(seed: int) -> None:
    rng = random.Random(seed)
    for _ in range(40):
        a = jsongen.rand_value(rng, 0, 4)
        b = jsongen.rand_value(rng, 0, 4) if rng.random() < 0.5 else respell(a, rng)
        vd = diff_values(a, b)
        assert vd.equal == (canonical_bytes(a) == canonical_bytes(b))
        assert diff_values(a, respell(a, rng)).equal
        assert diff_values(b, a).total == vd.total or not vd.equal   # symmetric for equality
        # every change's path exists on its side(s)
        for c in vd.changes:
            if c.a_path is not None:
                assert _get(a, c.a_path) is not MISSING
            if c.b_path is not None:
                assert _get(b, c.b_path) is not MISSING


def _get(v: Any, path: tuple) -> Any:
    for seg in path:
        if isinstance(v, dict) and seg in v:
            v = v[seg]
        elif isinstance(v, list) and isinstance(seg, int) and 0 <= seg < len(v):
            v = v[seg]
        else:
            return MISSING
    return v


def test_change_and_omitted_basics(lang_en) -> None:
    c = Change("added", None, ("a", 0), MISSING, 5)
    assert c.path == ("a", 0) and Change("removed", ("x",), None).path == ("x",)
    with pytest.raises(AttributeError):
        c.op = "removed"  # type: ignore[misc]
    o = Omitted("array", 3 << 20, "[1, 2…")
    assert o.text() == f"[array, {format_bytes(3 << 20)}: too large to show]"


def test_messages_exist_in_both_languages() -> None:
    keys = ["diff.value.omitted", "diff.error.mode", "diff.error.array_match", "diff.error.numbers",
            "diff.error.max_changes", "diff.error.max_details", "diff.error.memory_limit", "diff.error.array_key",
            "diff.error.array_key_wildcard", "diff.error.array_key_missing", "diff.error.key_mode",
            "diff.error.key_required", "diff.error.too_big_no_key", "diff.error.too_big_no_key.hint",
            "diff.error.too_many_keys", "diff.error.too_many_keys.hint", "diff.error.format", "diff.progress.scan",
            "diff.progress.match", "diff.progress.compare", "diff.warn.wrapper_too_large", "diff.warn.wrapper_one_side"]
    for w in ("partial", "broken_lines", "duplicate_keys", "no_record_keys", "missing_keys", "key_too_large",
              "key_unreadable"):
        keys += [f"diff.warn.{w}", f"diff.warn.{w}.one"]
    for key in keys:
        assert has(key), key
