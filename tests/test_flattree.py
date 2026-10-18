"""FlatTree (jsonreshape/gui/flattree.py, docs/ARCHITECTURE.md §11.4).

* model-based (hypothesis stateful) tests against a naive reference that materializes every
  visible row of a small tree;
* explicit cases (remembered expansion, hidden ancestors, filtered top views, orders, growth);
* performance with 10**9..10**12 top items, thousands of expanded nodes and a node with
  10**6 children.

Pure logic: no Qt needed.
"""

from __future__ import annotations

import random
import subprocess
import sys
import time

import pytest
from hypothesis import HealthCheck, settings
from hypothesis import strategies as st
from hypothesis.stateful import RuleBasedStateMachine, initialize, invariant, precondition, rule

from jsonreshape.gui import flattree as flattree_mod
from jsonreshape.gui.flattree import DisplayOrder, FlatTree, RowInfo

# --------------------------------------------------------------------------- reference model
def maybe_prepared(data, order):
    """A drawn display order as a list or as a prepared :class:`DisplayOrder` (P-F11)."""
    if order is not None and data.draw(st.booleans()):
        return DisplayOrder(order)
    return order



class Reference:
    """Naive reference: expanded nodes in a dict, every visible row materialized."""

    def __init__(self) -> None:
        self.view: list = []  # top keys in display order
        self.nodes: dict[tuple, list] = {}  # keypath -> [child_count, order list | None]

    def rows(self) -> tuple[list[RowInfo], list[int | None], list[int]]:
        """(rows, parent row of each row, last descendant row of each row)."""
        rows: list[RowInfo] = []
        parents: list[int | None] = []
        last: list[int] = []
        # iterative DFS: (keypath, pos, depth, parent row)
        stack = [((key,), p, 0, None) for p, key in reversed(list(enumerate(self.view)))]
        open_rows: list[int] = []  # rows whose subtree is still being emitted
        while stack:
            kp, pos, depth, parent = stack.pop()
            while open_rows and rows[open_rows[-1]].depth >= depth:
                last[open_rows.pop()] = len(rows) - 1
            r = len(rows)
            rows.append(RowInfo(r, depth, kp, pos))
            parents.append(parent)
            last.append(r)
            open_rows.append(r)
            info = self.nodes.get(kp)
            if info is not None:
                count, order = info
                for q in reversed(range(count)):
                    c = q if order is None else order[q]
                    stack.append((kp + (c,), q, depth + 1, r))
        while open_rows:
            last[open_rows.pop()] = len(rows) - 1
        return rows, parents, last

    def check_path(self, kp: tuple) -> None:
        for n in range(1, len(kp)):
            info = self.nodes.get(kp[:n])
            if info is not None and kp[n] >= info[0]:
                raise IndexError(kp)

    def drop_below(self, kp: tuple, n: int) -> None:
        depth = len(kp)
        for k in [k for k in self.nodes if len(k) > depth and k[:depth] == kp and k[depth] >= n]:
            del self.nodes[k]

    def expand(self, kp: tuple, n: int, order) -> None:
        self.check_path(kp)
        self.drop_below(kp, n)
        self.nodes[kp] = [n, None if order is None else list(order)]

    def collapse(self, kp: tuple) -> None:
        self.nodes.pop(kp, None)

    def set_order(self, kp: tuple, order) -> None:
        if kp not in self.nodes:
            raise KeyError(kp)
        self.nodes[kp][1] = None if order is None else list(order)

    def set_child_count(self, kp: tuple, n: int) -> None:
        self.drop_below(kp, n)
        info = self.nodes.get(kp)
        if info is None:
            return
        old, order = info
        if order is not None:
            if n < old:
                info[1] = [c for c in order if c < n]
            else:
                info[1] = order + list(range(old, n))
        info[0] = n


TOP_KEYS = [0, 1, 2, 3, 4, 5, 6, 7, "a", "b"]
MAX_CHILDREN = 4


def check_against(tree: FlatTree, ref: Reference) -> None:
    rows, parents, last = ref.rows()
    n = len(rows)
    assert tree.row_count() == n
    for info in rows:
        r = info.row
        assert tree.locate(r) == info
        assert tree.row_of(info.keypath) == r
        assert tree.parent_row(r) == parents[r]
        assert tree.last_descendant_row(r) == last[r]
        nxt = last[r] + 1
        sibling = nxt if nxt < n and rows[nxt].depth == info.depth else None
        assert tree.next_sibling_row(r) == sibling
        assert tree.visible_count(info.keypath) == last[r] - r + 1
    assert list(tree.iter_rows(0, n)) == rows
    for a in range(0, n, 3):
        assert list(tree.iter_rows(a, a + 5)) == rows[a:a + 5]
    assert list(tree.iter_rows(-3, n + 3)) == rows
    for bad in (-1, n):
        with pytest.raises(IndexError):
            tree.locate(bad)
    expanded = tree.expanded_keypaths()
    assert sorted(expanded, key=repr) == sorted(ref.nodes, key=repr)
    seen: set = set()
    for kp in expanded:  # parents before descendants
        assert not any(kp[:k] in ref.nodes and kp[:k] not in seen for k in range(1, len(kp)))
        seen.add(kp)
    visible = {info.keypath for info in rows}
    for kp in ref.nodes:
        assert tree.is_expanded(kp)
        assert (tree.row_of(kp) is not None) == (kp in visible)
        if kp not in visible:
            assert tree.visible_count(kp) == 0
    # white box: only expanded nodes and their ancestors are recorded (bounded memory)
    recorded = []
    stack = list(tree._root.by_idx.values())
    while stack:
        rec = stack.pop()
        recorded.append(rec.keypath)
        stack.extend(rec.by_idx.values())
    assert sorted(recorded, key=repr) == sorted({kp[:k] for kp in ref.nodes for k in range(1, len(kp) + 1)}, key=repr)


class FlatTreeMachine(RuleBasedStateMachine):
    def __init__(self) -> None:
        super().__init__()
        self.tree = FlatTree(0)
        self.ref = Reference()

    # -- helpers
    def keypath(self, data) -> tuple:
        rows = self.ref.rows()[0]
        kinds = ["random"]
        if rows:
            kinds += ["visible", "visible"]
        if self.ref.nodes:
            kinds += ["child", "expanded"]
        kind = data.draw(st.sampled_from(kinds))
        if kind == "visible":
            return data.draw(st.sampled_from(rows)).keypath
        if kind == "expanded":
            return data.draw(st.sampled_from(sorted(self.ref.nodes, key=repr)))
        if kind == "child":
            kp = data.draw(st.sampled_from(sorted(self.ref.nodes, key=repr)))
            return kp + (data.draw(st.integers(0, MAX_CHILDREN)),)
        top = data.draw(st.sampled_from(TOP_KEYS))
        rest = data.draw(st.lists(st.integers(0, MAX_CHILDREN), max_size=3))
        return (top, *rest)

    def row_count(self) -> int:
        return len(self.ref.rows()[0])

    # -- rules
    @initialize(n=st.integers(1, 8))
    def start(self, n):
        self.tree.set_top(n)
        self.ref.view = list(range(n))

    @rule(n=st.integers(0, 8))
    def set_top_identity(self, n):
        self.tree.set_top(n)
        self.ref.view = list(range(n))

    @rule(view=st.lists(st.sampled_from(TOP_KEYS), unique=True, max_size=8))
    def set_top_view(self, view):
        view = list(view)
        positions = {key: p for p, key in enumerate(view)}
        self.tree.set_top(len(view), top_key=view.__getitem__, top_pos=positions.get)
        self.ref.view = view

    @rule(data=st.data())
    def expand(self, data):
        kp = self.keypath(data)
        n = data.draw(st.integers(0, MAX_CHILDREN))
        order = maybe_prepared(data, data.draw(st.none() | st.permutations(list(range(n)))))
        before = self.row_count()
        try:
            self.ref.expand(kp, n, order)
        except IndexError:
            with pytest.raises(IndexError):
                self.tree.expand(kp, n, order)
            return
        assert self.tree.expand(kp, n, order) == self.row_count() - before

    @precondition(lambda self: bool(self.ref.view))
    @rule(data=st.data())
    def expand_visible(self, data):
        """Grows the tree faster than ``expand`` (which also tries hidden/invalid paths)."""
        info = data.draw(st.sampled_from(self.ref.rows()[0]))
        n = data.draw(st.integers(1, MAX_CHILDREN))
        order = maybe_prepared(data, data.draw(st.none() | st.permutations(list(range(n)))))
        before = self.row_count()
        self.ref.expand(info.keypath, n, order)
        assert self.tree.expand(info.keypath, n, order) == self.row_count() - before

    @rule(data=st.data())
    def collapse(self, data):
        kp = self.keypath(data)
        before = self.row_count()
        removed = self.tree.collapse(kp)
        self.ref.collapse(kp)
        assert removed == before - self.row_count()

    @precondition(lambda self: bool(self.ref.nodes))
    @rule(data=st.data())
    def collapse_expanded(self, data):
        kp = data.draw(st.sampled_from(sorted(self.ref.nodes, key=repr)))
        before = self.row_count()
        removed = self.tree.collapse(kp)
        self.ref.collapse(kp)
        assert removed == before - self.row_count()

    @rule()
    def collapse_all(self):
        self.tree.collapse_all()
        self.ref.nodes.clear()

    @precondition(lambda self: bool(self.ref.nodes))
    @rule(data=st.data())
    def set_order(self, data):
        kp = data.draw(st.sampled_from(sorted(self.ref.nodes, key=repr)))
        n = self.ref.nodes[kp][0]
        order = maybe_prepared(data, data.draw(st.none() | st.permutations(list(range(n)))))
        self.tree.set_order(kp, order)
        self.ref.set_order(kp, order)

    @rule(data=st.data())
    def set_order_not_expanded(self, data):
        kp = self.keypath(data)
        if kp in self.ref.nodes:
            return
        with pytest.raises(KeyError):
            self.tree.set_order(kp, None)

    @rule(data=st.data())
    def set_child_count(self, data):
        kp = self.keypath(data)
        n = data.draw(st.integers(0, MAX_CHILDREN + 1))
        self.tree.set_child_count(kp, n)
        self.ref.set_child_count(kp, n)

    @invariant()
    def row_count_matches(self):  # cheap: does not refresh the lazy prefix sums
        assert self.tree.row_count() == self.row_count()


class CheckEveryStep(FlatTreeMachine):
    """Full comparison after every step (all prefix sums fresh before each update)."""

    @invariant()
    def matches_reference(self):
        check_against(self.tree, self.ref)


class CheckSometimes(FlatTreeMachine):
    """Full comparison as a rule: several updates accumulate between lookups, which exercises
    the lazy ``start`` bookkeeping of hidden and visible nodes."""

    @rule()
    def check(self):
        check_against(self.tree, self.ref)

    def teardown(self):
        check_against(self.tree, self.ref)


_MODEL_SETTINGS = settings(  # deterministic, no example database on disk
    max_examples=150, stateful_step_count=50, deadline=None, derandomize=True, database=None,
    suppress_health_check=[HealthCheck.too_slow, HealthCheck.data_too_large],
)
CheckEveryStep.TestCase.settings = _MODEL_SETTINGS
CheckSometimes.TestCase.settings = _MODEL_SETTINGS
TestModelCheckEveryStep = CheckEveryStep.TestCase
TestModelCheckSometimes = CheckSometimes.TestCase


def _random_keypath(rng: random.Random, ref: Reference) -> tuple:
    rows = ref.rows()[0]
    kind = rng.random()
    if rows and kind < 0.5:
        return rng.choice(rows).keypath
    if ref.nodes and kind < 0.8:
        kp = rng.choice(sorted(ref.nodes, key=repr))
        return kp if rng.random() < 0.5 else kp + (rng.randrange(MAX_CHILDREN + 1),)
    return (rng.choice(TOP_KEYS), *[rng.randrange(MAX_CHILDREN + 1) for _ in range(rng.randrange(4))])


@pytest.mark.parametrize("seed", range(8))
def test_random_walk_against_reference(seed):
    """Deterministic differential test: long operation sequences, checks at random intervals."""
    rng = random.Random(seed)
    tree, ref = FlatTree(6), Reference()
    ref.view = list(range(6))
    shared: dict[int, DisplayOrder] = {}
    snapshots: dict[int, list[int]] = {}

    def make_order(n: int, p: float):
        """None, a list, or a DisplayOrder reused across nodes (the tree must never modify it)."""
        if rng.random() >= p:
            return None
        if rng.random() < 0.5:
            return rng.sample(range(n), n)
        if n not in shared or rng.random() < 0.3:
            shared[n] = DisplayOrder(rng.sample(range(n), n))
            snapshots[n] = list(shared[n])
        return shared[n]

    for _step in range(800):
        op = rng.random()
        before = len(ref.rows()[0])
        if op < 0.45:
            kp = _random_keypath(rng, ref)
            n = rng.randrange(MAX_CHILDREN + 1)
            order = make_order(n, 0.4)
            try:
                ref.expand(kp, n, order)
            except IndexError:
                with pytest.raises(IndexError):
                    tree.expand(kp, n, order)
            else:
                assert tree.expand(kp, n, order) == len(ref.rows()[0]) - before
        elif op < 0.70:
            kp = _random_keypath(rng, ref)
            removed = tree.collapse(kp)
            ref.collapse(kp)
            assert removed == before - len(ref.rows()[0])
        elif op < 0.80 and ref.nodes:
            kp = rng.choice(sorted(ref.nodes, key=repr))
            n = ref.nodes[kp][0]
            order = make_order(n, 0.7)
            tree.set_order(kp, order)
            ref.set_order(kp, order)
        elif op < 0.88:
            kp = _random_keypath(rng, ref)
            n = rng.randrange(MAX_CHILDREN + 2)
            tree.set_child_count(kp, n)
            ref.set_child_count(kp, n)
        elif op < 0.97:
            if rng.random() < 0.5:
                n = rng.randrange(9)
                tree.set_top(n)
                ref.view = list(range(n))
            else:
                view = rng.sample(TOP_KEYS, rng.randrange(len(TOP_KEYS) + 1))
                positions = {key: p for p, key in enumerate(view)}
                tree.set_top(len(view), top_key=view.__getitem__, top_pos=positions.get)
                ref.view = view
        else:
            tree.collapse_all()
            ref.nodes.clear()
        assert tree.row_count() == len(ref.rows()[0])
        if rng.random() < 0.15:
            check_against(tree, ref)
    check_against(tree, ref)
    for n, order in shared.items():                 # shared orders were never modified
        assert list(order) == snapshots[n] and list(order.inv) == sorted(range(n), key=order.__getitem__)


# --------------------------------------------------------------------------- explicit cases


def keypaths(tree: FlatTree) -> list[tuple]:
    return [info.keypath for info in tree.iter_rows(0, tree.row_count())]


def test_identity_top_level_expand_collapse():
    tree = FlatTree(5)
    assert tree.row_count() == 5 and tree.top_count == 5
    assert tree.locate(3) == RowInfo(3, 0, (3,), 3)
    assert tree.expand((2,), 3) == 3
    assert keypaths(tree) == [(0,), (1,), (2,), (2, 0), (2, 1), (2, 2), (3,), (4,)]
    assert tree.locate(4) == RowInfo(4, 1, (2, 1), 1)
    assert tree.row_of((3,)) == 6
    assert tree.row_of((2, 2)) == 5
    assert tree.row_of((2, 3)) is None  # no such child
    assert tree.row_of((7,)) is None  # not in the view
    assert tree.parent_row(5) == 2 and tree.parent_row(2) is None
    assert tree.next_sibling_row(2) == 6 and tree.next_sibling_row(5) is None
    assert tree.next_sibling_row(7) is None
    assert tree.last_descendant_row(2) == 5 and tree.last_descendant_row(1) == 1
    assert tree.visible_count((2,)) == 4 and tree.visible_count((2, 0)) == 1
    assert tree.collapse((2,)) == 3
    assert tree.collapse((2,)) == 0
    assert tree.row_count() == 5
    assert tree.expanded_keypaths() == []


def test_display_order_uses_file_indices_in_keypaths():
    tree = FlatTree(1)
    tree.expand((0,), 4, order=[3, 1, 0, 2])
    assert keypaths(tree) == [(0,), (0, 3), (0, 1), (0, 0), (0, 2)]
    assert tree.locate(1) == RowInfo(1, 1, (0, 3), 0)
    tree.expand((0, 1), 2)
    assert keypaths(tree) == [(0,), (0, 3), (0, 1), (0, 1, 0), (0, 1, 1), (0, 0), (0, 2)]
    assert tree.row_of((0, 0)) == 5
    tree.set_order((0,), None)  # back to file order: the expanded child moves with it
    assert keypaths(tree) == [(0,), (0, 0), (0, 1), (0, 1, 0), (0, 1, 1), (0, 2), (0, 3)]
    tree.set_order((0,), range(3, -1, -1))
    assert keypaths(tree) == [(0,), (0, 3), (0, 2), (0, 1), (0, 1, 0), (0, 1, 1), (0, 0)]
    assert tree.locate(3).pos == 2


def test_display_order_validates_and_inverts():
    order = DisplayOrder([2, 0, 1])
    assert len(order) == 3 and list(order) == [2, 0, 1] and order[0] == 2
    assert list(order.inv) == [1, 2, 0] and order.order.typecode == "q"
    assert DisplayOrder(order, 3).order is order.order          # no copy of a prepared order
    assert len(DisplayOrder([])) == 0
    for bad in ([0, 0, 1], [0, 1, 3], [-1, 0, 1], [1, 1, 0]):
        with pytest.raises(ValueError):
            DisplayOrder(bad)
    with pytest.raises(ValueError):
        DisplayOrder([1, 0], 3)                                    # wrong length


def test_display_order_is_taken_in_constant_time(monkeypatch):
    """P-F11: expand / set_order with a prepared DisplayOrder never copies, validates or
    inverts the permutation (that work happens where the order is built: a worker)."""
    n = 1_000_000
    prepared = DisplayOrder(range(n - 1, -1, -1))
    calls: list[int] = []
    real_inverse = flattree_mod._inverse
    monkeypatch.setattr(flattree_mod, "_inverse", lambda order: calls.append(len(order)) or real_inverse(order))
    tree = FlatTree(3)
    t0 = time.perf_counter()
    assert tree.expand((1,), n, prepared) == n
    tree.collapse((1,))
    tree.expand((1,), n, prepared)
    tree.set_order((1,), None)
    tree.set_order((1,), prepared)
    elapsed = time.perf_counter() - t0
    assert calls == [] and elapsed < 0.05                      # a list of 1M children took ~60 ms each
    assert tree.locate(2) == RowInfo(2, 1, (1, n - 1), 0)
    assert tree.row_of((1, 0)) == 2 + n - 1
    with pytest.raises(ValueError):
        tree.expand((0,), n - 1, prepared)                       # length checked
    assert not tree.is_expanded((0,))
    with pytest.raises(ValueError):
        tree.set_order((1,), DisplayOrder([0, 1]))
    assert tree.locate(2).keypath == (1, n - 1)                 # unchanged


def test_display_order_is_never_modified_by_the_tree():
    prepared = DisplayOrder([2, 0, 1])
    tree = FlatTree(2)
    tree.expand((0,), 3, prepared)
    tree.expand((1,), 3, prepared)                               # shared by two nodes
    tree.set_child_count((0,), 5)                                # grows: new children at the end
    assert keypaths(tree)[1:6] == [(0, 2), (0, 0), (0, 1), (0, 3), (0, 4)]
    tree.set_child_count((1,), 2)                                # shrinks
    assert keypaths(tree)[6:] == [(1,), (1, 0), (1, 1)]
    assert list(prepared) == [2, 0, 1] and list(prepared.inv) == [1, 2, 0]


@pytest.mark.parametrize("order", [[0, 1], [0, 0, 1], [0, 1, 3], [-1, 0, 1], [1, 1, 0]])
def test_invalid_order_is_rejected_without_change(order):
    tree = FlatTree(2)
    with pytest.raises(ValueError):
        tree.expand((0,), 3, order)
    assert tree.row_count() == 2 and tree.expanded_keypaths() == []
    tree.expand((0,), 3)
    with pytest.raises(ValueError):
        tree.set_order((0,), order)
    assert keypaths(tree) == [(0,), (0, 0), (0, 1), (0, 2), (1,)]


def test_collapse_remembers_descendants():
    tree = FlatTree(3)
    tree.expand((1,), 3)
    tree.expand((1, 2), 2)
    tree.expand((1, 2, 1), 1)
    assert tree.row_count() == 3 + 3 + 2 + 1
    assert tree.collapse((1,)) == 6
    assert tree.row_count() == 3
    assert tree.is_expanded((1, 2)) and not tree.is_expanded((1,))
    assert tree.row_of((1, 2)) is None
    assert tree.expand((1,), 3) == 6  # descendants restored
    assert tree.row_of((1, 2, 1, 0)) == 7
    assert tree.expanded_keypaths() == [(1,), (1, 2), (1, 2, 1)]
    tree.collapse((1, 2))
    tree.collapse((1,))
    assert tree.expand((1,), 2) == 2  # fewer children: (1, 2) is forgotten
    assert tree.expanded_keypaths() == [(1,)]
    tree.collapse_all()
    assert tree.expanded_keypaths() == [] and tree.row_count() == 3


def test_expand_below_collapsed_ancestor_is_recorded():
    tree = FlatTree(2)
    assert tree.expand((1, 2, 0), 3) == 0
    assert tree.is_expanded((1, 2, 0)) and not tree.is_expanded((1, 2))
    assert tree.row_of((1, 2, 0)) is None
    assert tree.expand((1,), 4) == 4
    assert tree.row_of((1, 2)) == 4
    assert tree.expand((1, 2), 1) == 1 + 3
    assert keypaths(tree)[4:] == [(1, 2), (1, 2, 0), (1, 2, 0, 0), (1, 2, 0, 1), (1, 2, 0, 2), (1, 3)]
    # an out-of-range child below a collapsed ancestor is dropped when the ancestor expands
    tree.expand((0, 9), 2)
    assert tree.expand((0,), 3) == 3
    assert (0, 9) not in tree.expanded_keypaths()


def test_child_index_validated_against_expanded_ancestors():
    tree = FlatTree(2)
    tree.expand((0,), 3)
    before = (tree.row_count(), tree.expanded_keypaths())
    with pytest.raises(IndexError):
        tree.expand((0, 3), 1)
    with pytest.raises(IndexError):
        tree.expand((0, 3, 1), 1)
    with pytest.raises(IndexError):
        tree.expand((0, -1), 1)
    with pytest.raises(ValueError):
        tree.expand((0, 1), -1)
    with pytest.raises(ValueError):
        tree.expand((), 1)
    with pytest.raises(TypeError):
        tree.expand((0, "x"), 1)
    assert (tree.row_count(), tree.expanded_keypaths()) == before


def test_reexpand_open_node_replaces_count_and_order():
    tree = FlatTree(1)
    tree.expand((0,), 4)
    tree.expand((0, 3), 2)
    assert tree.row_count() == 1 + 4 + 2
    assert tree.expand((0,), 6, order=[5, 4, 3, 2, 1, 0]) == 2
    assert keypaths(tree)[:4] == [(0,), (0, 5), (0, 4), (0, 3)]
    assert tree.row_of((0, 3, 1)) == 5
    assert tree.expand((0,), 2) == -(6 + 2) + 2  # (0, 3) is forgotten
    assert tree.expanded_keypaths() == [(0,)]


def test_filtered_top_view_keeps_expansion_of_hidden_items():
    tree = FlatTree(10)
    tree.expand((3,), 2)
    tree.expand((7,), 1)
    tree.expand(("wrapper",), 5)  # not in the identity view: remembered, no rows
    assert tree.row_count() == 10 + 2 + 1
    view = [7, 1, 5]
    tree.set_top(3, top_key=view.__getitem__, top_pos={k: i for i, k in enumerate(view)}.get)
    assert keypaths(tree) == [(7,), (7, 0), (1,), (5,)]
    assert tree.row_of((3,)) is None and tree.row_of((3, 0)) is None
    assert tree.is_expanded((3,))
    assert tree.collapse((3,)) == 0  # hidden: no rows removed, but forgotten
    assert tree.expand((5,), 1) == 1
    view = ["wrapper", 3, 5, 7]
    tree.set_top(4, top_key=view.__getitem__, top_pos={k: i for i, k in enumerate(view)}.get)
    assert keypaths(tree) == [("wrapper",)] + [("wrapper", i) for i in range(5)] + [(3,), (5,), (5, 0), (7,), (7, 0)]
    tree.set_top(10)  # back to the identity view
    assert tree.row_count() == 10 + 1 + 1
    assert tree.row_of(("wrapper", 0)) is None


def test_set_top_rejects_inconsistent_positions():
    tree = FlatTree(4)
    tree.expand((1,), 1)
    tree.expand((2,), 1)
    with pytest.raises(ValueError):
        tree.set_top(4, top_key=lambda p: p, top_pos=lambda k: 0)  # two keys at position 0
    with pytest.raises(ValueError):
        tree.set_top(2, top_key=lambda p: p, top_pos=lambda k: 3)  # outside [0, 2)
    with pytest.raises(ValueError):
        tree.set_top(2, top_key=lambda p: p)  # top_pos missing
    with pytest.raises(ValueError):
        tree.set_top(-1)
    assert tree.row_count() == 6 and tree.top_count == 4
    tree.set_top(2)  # identity: key 2 is now outside the view
    assert tree.row_count() == 3 and tree.is_expanded((2,))
    tree.set_top(5)  # grew: back in view
    assert tree.row_count() == 7


def test_top_pos_only_called_for_expanded_keys_and_cached():
    n = 10**9
    calls = {"pos": 0, "key": 0}

    def top_key(p):
        calls["key"] += 1
        return n - 1 - p  # reversed view

    def top_pos(k):
        calls["pos"] += 1
        return n - 1 - k if isinstance(k, int) and 0 <= k < n else None

    tree = FlatTree(n, top_key=top_key, top_pos=top_pos)
    assert calls == {"pos": 0, "key": 0}
    for key in (5, 10, n - 1):
        tree.expand((key,), 2)
    assert calls["pos"] == 3
    calls["pos"] = 0
    tree.set_top(n, top_key=top_key, top_pos=top_pos)
    assert calls["pos"] == 3  # once per expanded key
    calls["pos"] = 0
    rows = tree.row_count()
    assert rows == n + 6
    assert tree.locate(0) == RowInfo(0, 0, (n - 1,), 0)  # a record: no top_key call
    assert tree.locate(1) == RowInfo(1, 1, (n - 1, 0), 0)
    assert calls["key"] == 0
    assert tree.locate(n) == RowInfo(n, 1, (5, 1), 1)  # key 5 is at position n - 6
    assert calls["key"] == 0
    assert tree.locate(rows - 1) == RowInfo(rows - 1, 0, (0,), n - 1) and calls["key"] == 1
    calls["key"] = 0
    assert [i.keypath for i in tree.iter_rows(rows - 10, rows)] == [
        (7,), (6,), (5,), (5, 0), (5, 1), (4,), (3,), (2,), (1,), (0,)]
    assert calls["key"] == 7  # only the plain top rows need their key
    assert calls["pos"] == 0  # locate / iter_rows never call top_pos
    assert tree.row_of((5, 1)) == n and calls["pos"] == 0
    assert tree.row_of((6,)) == n - 3 and calls["pos"] == 1  # not recorded: one call


def test_iter_rows_tail_of_reversed_view():
    n = 10**9
    tree = FlatTree(n, top_key=lambda p: n - 1 - p, top_pos=lambda k: n - 1 - k)
    tree.expand((10,), 2)
    tree.expand((5,), 2)
    rows = tree.row_count()
    start = tree.row_of((10, 0))
    got = [i.keypath for i in tree.iter_rows(start, rows)]
    assert got == [(10, 0), (10, 1), (9,), (8,), (7,), (6,), (5,), (5, 0), (5, 1), (4,), (3,), (2,), (1,), (0,)]
    assert start == rows - len(got)
    assert [i.pos for i in tree.iter_rows(rows - 3, rows)] == [n - 3, n - 2, n - 1]


def test_set_child_count_growth_and_shrink():
    tree = FlatTree(1)
    tree.expand((0,), 3, order=[2, 0, 1])
    tree.expand((0, 0), 1)
    tree.set_child_count((0,), 5)  # new children appended in file order
    assert keypaths(tree) == [(0,), (0, 2), (0, 0), (0, 0, 0), (0, 1), (0, 3), (0, 4)]
    tree.set_child_count((0,), 1)  # shrink: the order keeps the remaining children
    assert keypaths(tree) == [(0,), (0, 0), (0, 0, 0)]
    tree.set_child_count((0,), 0)
    assert keypaths(tree) == [(0,)]
    assert tree.expanded_keypaths() == [(0,)]
    tree.set_child_count((9, 9), 3)  # not recorded: no-op
    assert tree.expanded_keypaths() == [(0,)]
    # a collapsed record: remembered children beyond the new count are forgotten
    tree.expand((0,), 3)
    tree.expand((0, 2), 1)
    tree.collapse((0,))
    tree.set_child_count((0,), 2)
    assert tree.expanded_keypaths() == []


def test_lenient_queries_for_invalid_keypaths():
    tree = FlatTree(3)
    tree.expand((1,), 2)
    for kp in [(), ([1],), (1, "x"), (1, 5), (1, -1), (1, 0, 0), ("nope",)]:
        assert tree.row_of(kp) is None
        assert not tree.is_expanded(kp)
        assert tree.visible_count(kp) == 0
        assert tree.collapse(kp) == 0
    assert tree.row_of([1, 1]) == 3  # any iterable


def test_iter_rows_clamps_and_empty_tree():
    tree = FlatTree(0)
    assert tree.row_count() == 0
    assert list(tree.iter_rows(0, 10)) == []
    with pytest.raises(IndexError):
        tree.locate(0)
    tree.set_top(3)
    tree.expand((2,), 2)
    assert [i.row for i in tree.iter_rows(-5, 100)] == [0, 1, 2, 3, 4]
    assert list(tree.iter_rows(4, 2)) == []


def test_huge_top_count():
    n = 10**12
    tree = FlatTree(n)
    tree.expand((n - 1,), 3)
    tree.expand((0,), 2)
    tree.expand((n // 2,), 1)
    assert tree.row_count() == n + 6
    last = tree.row_count() - 1
    assert tree.locate(last) == RowInfo(last, 1, (n - 1, 2), 2)
    assert tree.row_of((n // 2, 0)) == n // 2 + 3
    assert tree.locate(n // 2 + 3) == RowInfo(n // 2 + 3, 1, (n // 2, 0), 0)
    assert [i.keypath for i in tree.iter_rows(last - 4, last + 1)] == [
        (n - 3,), (n - 2,), (n - 1,), (n - 1, 0), (n - 1, 1), (n - 1, 2)][-5:]
    assert tree.next_sibling_row(n // 2 + 2) == n // 2 + 4
    assert tree.parent_row(last) == last - 3


def test_deep_chain():
    tree = FlatTree(1)
    kp: tuple = (0,)
    for _ in range(300):
        tree.expand(kp, 2)
        kp = kp + (1,)
    assert tree.row_count() == 1 + 300 * 2
    last = tree.row_count() - 1
    info = tree.locate(last)
    assert info.depth == 300 and info.keypath == (0,) + (1,) * 300
    assert tree.row_of(info.keypath) == last
    assert tree.collapse((0,)) == 600
    assert tree.expand((0,), 2) == 600


def test_flattree_does_not_import_qt():
    code = "import sys, jsonreshape.gui.flattree; sys.exit(1 if 'PySide6' in sys.modules else 0)"
    assert subprocess.run([sys.executable, "-c", code], check=False).returncode == 0


# --------------------------------------------------------------------------- performance


def _check_roundtrips(tree: FlatTree, rng: random.Random, count: int) -> float:
    """``count`` locate + row_of round trips: half random rows, half inside expanded subtrees."""
    total = tree.row_count()
    rows = [rng.randrange(total) for _ in range(count // 2)]
    expanded = tree.expanded_keypaths()
    while len(rows) < count:
        kp = rng.choice(expanded)
        first = tree.row_of(kp)
        if first is not None:
            rows.append(first + rng.randrange(tree.visible_count(kp)))
    t0 = time.perf_counter()
    infos = [tree.locate(r) for r in rows]
    back = [tree.row_of(i.keypath) for i in infos]
    elapsed = time.perf_counter() - t0
    assert back == rows
    return elapsed


def test_performance_many_expanded_nodes():
    rng = random.Random(1)
    n = 10**9
    tree = FlatTree(n)
    tops = rng.sample(range(n), 1000)
    for key in tops:
        tree.expand((key,), 20)
    for key in rng.sample(tops, 500):  # 1000 nested expansions below expanded tops
        for c in rng.sample(range(20), 2):
            tree.expand((key, c), 5)
    assert len(tree.expanded_keypaths()) == 2000
    assert tree.row_count() == n + 1000 * 20 + 1000 * 5
    elapsed = _check_roundtrips(tree, rng, 10_000)
    assert elapsed < 1.0, f"10 000 locate + row_of took {elapsed:.2f} s"
    # rows inside the expanded subtrees too
    hits = [tree.row_of((key, c)) for key in tops[:200] for c in (0, 19)]
    assert all(tree.locate(r).keypath[0] in tops for r in hits)
    t0 = time.perf_counter()
    page = list(tree.iter_rows(tree.row_count() // 2, tree.row_count() // 2 + 2000))
    assert time.perf_counter() - t0 < 0.2
    assert [i.row for i in page] == list(range(page[0].row, page[0].row + 2000))
    # set_top keeps everything
    t0 = time.perf_counter()
    tree.set_top(n + 5)
    assert time.perf_counter() - t0 < 0.5
    assert tree.row_count() == n + 5 + 25_000


def test_performance_node_with_a_million_children():
    rng = random.Random(2)
    m = 10**6
    tree = FlatTree(3)
    order = list(range(m - 1, -1, -1))
    t0 = time.perf_counter()
    tree.expand((1,), m, order=order)
    for c in rng.sample(range(m), 5000):
        tree.expand((1, c), 3)
    build = time.perf_counter() - t0
    assert tree.row_count() == 3 + m + 15_000
    assert build < 3.0, f"building took {build:.2f} s"
    elapsed = _check_roundtrips(tree, rng, 10_000)
    assert elapsed < 1.0, f"10 000 locate + row_of took {elapsed:.2f} s"
    # interleaved updates and lookups stay cheap (lazy prefix sums)
    t0 = time.perf_counter()
    for c in rng.sample(range(m), 300):
        tree.expand((1, c), 2)
        tree.locate(rng.randrange(tree.row_count()))
    assert time.perf_counter() - t0 < 1.0
    t0 = time.perf_counter()
    tree.set_order((1,), None)  # re-sort: O(n + k log k)
    assert time.perf_counter() - t0 < 1.0
    elapsed = _check_roundtrips(tree, rng, 2000)
    assert elapsed < 0.5
