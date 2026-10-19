"""Qt-free parts of the tree view (jsonreshape/gui/treemodel.py).

Text helpers (keys, string tokens with escapes / cuts / surrogates, container summaries,
labels, quick-find matching) and :class:`NodeSource` on real documents: infos and child
counts (small vs large nodes), sibling ranges, Uzbek key order, element order by a field
(typed, nulls / missing last, stable, descending, huge elements streamed), field
candidates, the expand planner (levels, cap, orders) and the ancestor chain.
"""

from __future__ import annotations

import json
import random
import time
from pathlib import Path

import pytest

from jsonreshape.errors import Cancelled
from jsonreshape.tasks import CancelToken, Progress

from jsonreshape.gui.treemodel import (
    ROOT,
    WRAPPER,
    NodeInfo,
    NodeSource,
    _runs,
    container_text,
    display_key,
    find_matches,
    key_label,
    scalar_display,
    search_texts,
    stable_argsort,
    string_display,
    summarize_prefix,
)
from jsonreshape.reader import Document
from jsonreshape.settings import Settings
from jsonreshape.sortkeys import SortKey, collation_key


def open_doc(path: Path, value, **settings) -> Document:
    path.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")
    st = Settings()
    for k, v in settings.items():
        setattr(st, k, v)
    doc = Document.open(path, settings=st, use_cache=False)
    doc.build_index()
    return doc


@pytest.fixture
def docs():
    made: list[Document] = []

    def make(path: Path, value, **settings) -> Document:
        doc = open_doc(path, value, **settings)
        made.append(doc)
        return doc

    yield make
    for d in made:
        d.close()


# ----------------------------------------------------------------------------------
# text helpers
# ----------------------------------------------------------------------------------
def test_display_key() -> None:
    assert display_key("name") == "name"
    assert display_key("o'rik") == "o'rik"
    assert display_key("") == '""'
    assert display_key(" padded") == '" padded"'
    assert display_key("a\nb") == '"a\\nb"'
    assert display_key("tab\there") == '"tab\\there"'
    assert display_key("\ud800x") == "\\ud800x"          # lone surrogate made visible


def test_string_display() -> None:
    assert string_display('"Toshkent"') == '"Toshkent"'
    assert string_display('"\\u0422\\u043e\\u0448\\u043a\\u0435\\u043d\\u0442"') == '"Тошкент"'
    assert string_display('"a\\nb\\t\\"c\\" \\\\ /"') == '"a\\nb\\t\\"c\\" \\\\ /"'
    assert string_display('"\\ud83d\\ude00"') == '"😀"'
    assert string_display('"\\ud800"') == '"\\ud800"'
    # cut tokens (Node.scalar_text adds "…"): escapes decoded, an incomplete escape dropped
    assert string_display('"\\u0442\\u043e\\u04…') == '"то…'
    assert string_display('"abc\\…') == '"abc…'
    assert string_display('"line\\nnext…') == '"line\\nnext…'
    # a string that really ends with an ellipsis is complete
    assert string_display('"wait…"') == '"wait…"'
    assert scalar_display("number", "12345678901234567890.10") == "12345678901234567890.10"
    assert scalar_display("bool", "true") == "true" and scalar_display("null", "null") == "null"


def test_summarize_prefix_and_container_text() -> None:
    assert summarize_prefix("object", '{"id": 1, "name": "Ali", "tags": [1, 2], "c": {"x": 1}}') == \
        'id: 1, name: "Ali", tags: […], c: {…}'
    assert summarize_prefix("array", "[1, 2, 3]") == "1, 2, 3"
    assert summarize_prefix("array", '[{}, [], "x"]') == '{}, [], "x"'
    # cut prefixes: whatever is complete, then "…"
    assert summarize_prefix("object", '{"id": 1, "name": "Al…') == 'id: 1, name: "Al…, …'
    assert summarize_prefix("array", "[1, 2, 12…") == "1, 2, …"
    assert summarize_prefix("array", '[{"a": [1, 2…') == "{…}, …"
    assert summarize_prefix("array", "[1, 2, 3, 4, 5, 6, 7, 8]") == "1, 2, 3, 4, 5, 6, …"
    assert summarize_prefix("object", '{"a\\nb": "\\u0442"}') == '"a\\nb": "т"'
    assert summarize_prefix("object", "garbage") == "…"
    long = summarize_prefix("array", '["' + "x" * 100 + '"]')
    assert long.startswith('"xxx') and long.endswith("…") and len(long) <= 40
    assert container_text("object", 0, "{}") == "{}" and container_text("array", 0, "[]") == "[]"
    assert container_text("object", 5, '{"id": 1, "name": "Ali"}') == '{5} id: 1, name: "Ali"'
    assert container_text("array", None, "[1, 2…") == "[?] 1, …"


def info(kp, kind="object", key=None, text="", count=None) -> NodeInfo:
    return NodeInfo(keypath=kp, kind=kind, key=key, path=(), start=0, end=10, count=count, text=text)


def test_key_label_and_search_texts(lang_uz) -> None:
    assert key_label((5,), None) is None
    assert key_label((5,), info((5,), key=5)) == "#5"
    assert key_label((5,), info((5,), key="alpha")) == "alpha"
    assert key_label((WRAPPER,), None) == "(o'rama)"
    assert key_label((ROOT,), None) == "$"
    assert key_label((5, 3), info((5, 3), key=3)) == "[3]"
    assert key_label((5, 3), info((5, 3), key="a b")) == "a b"
    assert key_label((5, 3), info((5, 3), key="")) == '""'
    assert search_texts((5, 3), info((5, 3), kind="string", key="name", text='"Ali"')) == ("name", '"Ali"')
    # C4: a container row is findable by its preview (the value column as painted)
    assert search_texts((5, 3), info((5, 3), kind="object", key=3, text="{1} a: 1")) == (None, "{1} a: 1")
    assert search_texts((5,), info((5,), kind="object", key="rec", text='{2} name: "Nazarova", …')) ==         ("rec", '{2} name: "Nazarova", …')
    records = info((WRAPPER, 1), kind="array", key="data", text="[300]")
    records.records = True
    assert search_texts((WRAPPER, 1), records) == ("data", None)     # a label, not data
    entries = [((1,), "name", '"ali"'), ((2,), None, '"x"'), ((3,), "ALIM", None)]
    found = find_matches(entries, lambda text: [(i, i + 3) for i in range(len(text)) if text.lower()[i:i + 3] == "ali"])
    assert found[(1,)] == ((), ((1, 4),)) and found[(3,)] == (((0, 3),), ())
    assert len(find_matches(entries, lambda text: [(0, 1)], limit=2)) == 2
    assert list(_runs([1, 2, 3, 40, 41, 100], 16)) == [(0, 3), (3, 5), (5, 6)]


# ----------------------------------------------------------------------------------
# stable_argsort (P-F5: bounded GIL holds, same order as one sorted())
# ----------------------------------------------------------------------------------
@pytest.mark.parametrize("run,block", [(2, 1), (3, 1), (4, 2), (5, 3), (16, 4), (64, 1)])
def test_stable_argsort_matches_sorted_on_random_and_skewed_keys(run: int, block: int) -> None:
    rng = random.Random(run * 31 + block)
    for n in (0, 1, 2, 3, 7, 50, 333):
        cases = [
            [bytes([rng.randrange(4)]) for _ in range(n)],            # many ties
            [rng.randbytes(rng.randrange(3)) for _ in range(n)],       # prefixes, empty keys
            [b"x"] * n,                                                # all equal
            [i.to_bytes(4, "big") for i in range(n)],                  # ascending
            [(n - i).to_bytes(4, "big") for i in range(n)],            # descending
            [bytes([(i // 7) % 3]) for i in range(n)],                 # long equal runs
        ]
        for keys in cases:
            expected = sorted(range(n), key=keys.__getitem__)
            got = stable_argsort(keys, run=run, block=block)
            assert got.typecode == "q" and list(got) == expected, (n, run, block, keys)


def test_stable_argsort_bounded_steps_progress_and_cancel(monkeypatch) -> None:
    """A million keys: the same order as one sorted() call, but no single C-level sort gets
    more than 2 * SORT_RUN items (each keeps the GIL only a few ms; one sorted() of all
    keys kept it 0.2-0.6 s and froze the GUI thread)."""
    import jsonreshape.gui.treemodel as tm

    rng = random.Random(7)
    n = 1_000_000
    keys = [rng.randrange(1000).to_bytes(2, "big") for _ in range(n)]
    expected = tm.array("q", sorted(range(n), key=keys.__getitem__))
    sizes: list[int] = []

    def spy_sorted(items, *a, **k):
        out = sorted(items, *a, **k)
        sizes.append(len(out))
        return out

    monkeypatch.setattr(tm, "sorted", spy_sorted, raising=False)
    progress = Progress(n, unit="items")
    t0 = time.perf_counter()
    got = stable_argsort(keys, None, progress)
    elapsed = time.perf_counter() - t0
    monkeypatch.undo()
    assert got == expected
    assert len(sizes) > n // tm.SORT_RUN and max(sizes) <= 2 * tm.SORT_RUN
    assert progress.done == n
    assert elapsed < 20                                     # generous: ~0.5 s typical
    token = CancelToken()
    token.cancel()
    with pytest.raises(Cancelled):
        stable_argsort(keys, token)


@pytest.mark.parametrize("pattern", ["equal", "ascending", "descending"])
def test_stable_argsort_gallops_over_presorted_runs(monkeypatch, pattern: str) -> None:
    """Presorted / all-equal keys (e.g. sorting by a missing field) need no merge sorts."""
    import jsonreshape.gui.treemodel as tm

    n = 300_000
    if pattern == "equal":
        keys = [b"\x00"] * n
    elif pattern == "ascending":
        keys = [i.to_bytes(4, "big") for i in range(n)]
    else:
        keys = [(n - i).to_bytes(4, "big") for i in range(n)]
    sizes: list[int] = []

    def spy_sorted(items, *a, **k):
        out = sorted(items, *a, **k)
        sizes.append(len(out))
        return out

    monkeypatch.setattr(tm, "sorted", spy_sorted, raising=False)
    got = stable_argsort(keys)
    monkeypatch.undo()
    assert list(got) == sorted(range(n), key=keys.__getitem__)
    runs = -(-n // tm.SORT_RUN)
    assert len(sizes) == runs                                # only the run sorts, no merge steps


# ----------------------------------------------------------------------------------
# NodeSource
# ----------------------------------------------------------------------------------
def test_fetch_infos_counts_and_ranges(tmp_path: Path, docs) -> None:
    doc = docs(tmp_path / "a.json", [{"id": 1, "name": "Ali", "tags": [1, 2], "big": list(range(400))},
                                     {"x": None}], large_node_bytes=1000)
    src = NodeSource(doc)
    got = src.fetch([(0,), (1,), (0, 0), (0, 1), (0, 2), (0, 3), (0, 9), (7,)], None)
    rec = got[(0,)]
    assert rec.kind == "object" and rec.key == 0 and rec.path == (0,) and rec.large and rec.count is None
    assert rec.text.startswith("{?} id: 1, name: \"Ali\"")
    assert got[(1,)].count == 1 and not got[(1,)].large and got[(1,)].text == "{1} x: null"
    assert got[(0, 1)].text == '"Ali"' and got[(0, 1)].key == "name" and got[(0, 1)].path == (0, "name")
    assert got[(0, 2)].count == 2 and got[(0, 2)].text == "[2] 1, 2"
    big = got[(0, 3)]
    assert big.large and big.count is None             # > large_node_bytes: counted by the task
    assert isinstance(got[(0, 9)], IndexError) and isinstance(got[(7,)], Exception)
    assert src.child_count((0, 3)) == 400
    again = src.fetch([(0, 3)], None)[(0, 3)]
    assert again.count == 400                          # indexed now: counted at once
    assert (0,) in src.recent_tops


def test_wrapped_records_member_and_chain(tmp_path: Path, docs) -> None:
    doc = docs(tmp_path / "w.json", {"meta": {"v": {"deep": [1, 2]}}, "data": [{"a": 1}, {"a": 2}, {"a": 3}]})
    assert doc.kind == "wrapped"
    src = NodeSource(doc)
    assert src.record_base == ("data",) and src.record_depth == 2
    got = src.fetch([(WRAPPER,), (WRAPPER, 0), (WRAPPER, 1)], None)
    assert got[(WRAPPER,)].count == 2
    data = got[(WRAPPER, 1)]
    assert data.records and data.count == 3 and not data.expandable
    rec = src.fetch([(2,)], None)[(2,)]
    assert rec.path == ("data", 2) and rec.text == "{1} a: 3"
    chain = src.chain((WRAPPER, 0, 0, 0, 1))
    assert [i.keypath for i in chain] == [(WRAPPER,), (WRAPPER, 0), (WRAPPER, 0, 0), (WRAPPER, 0, 0, 0)]
    assert [i.count for i in chain] == [2, 1, 1, 2]


def test_key_order_uzbek(tmp_path: Path, docs) -> None:
    keys = ["z", "o'rik", "olma", "g'isht", "sh", "ch", "b", "child10", "child2", "ng", "n", "a"]
    doc = docs(tmp_path / "k.json", [{k: i for i, k in enumerate(keys)}, {}])
    src = NodeSource(doc)
    node = src.node((0,))
    order = src.key_order(node, len(keys))
    shown = [keys[i] for i in order]
    assert shown == sorted(keys, key=collation_key)
    assert shown == ["a", "b", "n", "olma", "z", "o'rik", "g'isht", "sh", "ch", "child2", "child10", "ng"]


def test_element_order_by_field(tmp_path: Path, docs) -> None:
    items = [{"n": 3}, {"n": None}, {"n": 1}, {}, {"n": 2}, {"n": "b"}, {"n": "a"}, {"n": 1}, {"n": [1]}]
    doc = docs(tmp_path / "e.json", [{"items": items, "nums": [5, 1, None, 4], "deep": [{"a": {"b": 2}},
                                                                                      {"a": {"b": 1}}]}, {}])
    src = NodeSource(doc)
    node = src.node((0, 0))
    n = len(items)
    asc = list(src.element_order(node, n, SortKey("n")))
    # numbers < strings < arrays, then null, then missing; equal values keep the file order
    assert asc == [2, 7, 4, 0, 6, 5, 8, 1, 3]
    desc = list(src.element_order(node, n, SortKey("n", descending=True)))
    assert desc == [8, 5, 6, 0, 4, 2, 7, 1, 3]
    nums = src.node((0, 1))
    assert list(src.element_order(nums, 4, SortKey("$"))) == [1, 3, 0, 2]
    assert list(src.element_order(nums, 4, SortKey("$index", descending=True))) == [3, 2, 1, 0]
    deep = src.node((0, 2))
    assert list(src.element_order(deep, 2, SortKey("a.b"))) == [1, 0]
    assert src.field_candidates((0, 0)) == ["n"]
    assert src.field_candidates((0, 1)) == ["$"]
    assert src.field_candidates((0, 2)) == ["a"]


def test_element_order_streams_huge_elements(tmp_path: Path, docs) -> None:
    items = [{"k": 2, "pad": "x" * 300}, {"k": 1, "pad": "y" * 300}, {"k": 3}]
    doc = docs(tmp_path / "h.json", [{"items": items}, {}], large_node_bytes=200)
    src = NodeSource(doc)
    node = src.node((0, 0))
    count = node.child_count()
    assert list(src.element_order(node, count, SortKey("k"))) == [1, 0, 2]


def test_plan_levels_cap_and_orders(tmp_path: Path, docs) -> None:
    rec = {"b": {"y": [1, 2], "x": 1}, "a": [{"q": 1}, {"q": 2}], "s": 1}
    doc = docs(tmp_path / "p.json", [rec, rec, {"z": 1}])
    src = NodeSource(doc)
    plan = src.plan([(0,)], levels=None, cap=1000)
    assert [(kp, c, k) for kp, c, _o, k in plan.items] == [
        ((0,), 3, "object"), ((0, 0), 2, "object"), ((0, 1), 2, "array"), ((0, 0, 0), 2, "array"),
        ((0, 1, 0), 1, "object"), ((0, 1, 1), 1, "object")]
    assert plan.rows == 3 + 2 + 2 + 2 + 1 + 1 and not plan.overflow
    two = src.plan([(0,)], levels=2, cap=1000)
    assert [kp for kp, *_ in two.items] == [(0,), (0, 0), (0, 1)]
    tops = src.plan(iter([(0,), (1,), (2,)]), levels=1, cap=1000)
    assert [kp for kp, *_ in tops.items] == [(0,), (1,), (2,)] and tops.rows == 7
    capped = src.plan(iter([(0,), (1,), (2,)]), levels=None, cap=12)
    assert capped.overflow and capped.rows > 12

    def order_for(kp, node, count, cancel):
        return ("keys", src.key_order(node, count, cancel)) if node.kind == "object" else None

    ordered = src.plan([(0,)], levels=1, cap=100, order_for=order_for)
    mode, arr = ordered.items[0][2]
    assert mode == "keys" and list(arr) == [1, 0, 2]          # a, b, s


def test_fetch_leaves_out_records_the_index_has_not_reached(tmp_path: Path) -> None:
    """While the index grows, records beyond it are "not loaded yet" (left out of the result), never errors: the
    tree's loader would keep an error after the index got there (review3 H1)."""
    path = tmp_path / "g.json"
    path.write_text(json.dumps([{"a": i} for i in range(5)]), encoding="utf-8")
    doc = Document.open(path, use_cache=False)
    try:
        assert not doc.index_complete and doc.record_count == 0
        src = NodeSource(doc.pinned())
        assert src.not_indexed_yet(0) and src.not_indexed_yet(4)
        assert not src.not_indexed_yet(WRAPPER) and not src.not_indexed_yet(ROOT) and not src.not_indexed_yet(True)
        assert src.fetch([(0,), (3,), (3, 0)], None) == {}
        doc.build_index()
        assert not src.not_indexed_yet(4) and not src.not_indexed_yet(9)
        got = src.fetch([(0,), (3,), (3, 0), (9,)], None)
        assert got[(3,)].text == "{1} a: 3" and got[(3, 0)].text == "3"
        assert isinstance(got[(9,)], IndexError)        # beyond the final count: a real error
    finally:
        doc.close()
