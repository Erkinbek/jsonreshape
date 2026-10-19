"""FlatTree structure changes (jsonreshape/gui/flattree.py; docs/ARCHITECTURE.md §12.9 "Tree").

``remap_tops`` (record-level inserts / deletes / moves) and ``remap_children`` (children of one
container inserted / deleted / moved) are checked against a naive model that keeps the whole
tree as nested lists of node OBJECTS: expansion state and display orders belong to a node,
key paths are computed from the structure. After every structural edit the model applies
the change to its lists (through ``Remap.map``) and the FlatTree is told with the remap;
then every visible row, ``row_of`` of every node, the expanded key paths, the recorded
nodes (white box) and ``top_runs`` must agree. Identity top levels and filtered / sorted top
views (custom ``top_key`` / ``top_pos`` re-set after the remap, like the tree view does) are
covered; remaps use every ``Remap`` op kind (``ins``, ``del``, ``inss``, ``dels``, ``move``,
``unmove``, ``pieces`` and sequences of them).

Pure logic: no Qt needed.
"""

from __future__ import annotations

import itertools
import random
from array import array

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from jsonreshape.gui.flattree import DisplayOrder, FlatTree, RowInfo
from jsonreshape.overlay import PieceTable, Remap

_ids = itertools.count()


class MNode:
    """A node of the model: children in file order, expansion and display order."""

    __slots__ = ("id", "kids", "open", "order")

    def __init__(self, nkids: int = 0) -> None:
        self.id = next(_ids)
        self.kids: list[MNode] = [MNode() for _ in range(nkids)] if nkids else []
        self.open = False
        self.order: list[int] | None = None

    def __repr__(self) -> str:
        return f"N{self.id}"


class Model:
    """The whole tree: ``tops`` (records in file order); ``view`` = top keys in display order
    (``None``: identity, every top in file order)."""

    def __init__(self, tops: list[MNode], view: list[int] | None = None) -> None:
        self.tops = tops
        self.view = view

    def shown(self) -> list[int]:
        return list(range(len(self.tops))) if self.view is None else list(self.view)

    def nodes(self) -> list[tuple[tuple, MNode]]:
        """Every node with its key path (depth first, file order)."""
        out: list[tuple[tuple, MNode]] = []
        stack = [((k,), n) for k, n in reversed(list(enumerate(self.tops)))]
        while stack:
            kp, node = stack.pop()
            out.append((kp, node))
            for c in reversed(range(len(node.kids))):
                stack.append((kp + (c,), node.kids[c]))
        return out

    def rows(self) -> list[RowInfo]:
        rows: list[RowInfo] = []
        stack = [((key,), p, 0, self.tops[key]) for p, key in reversed(list(enumerate(self.shown())))]
        while stack:
            kp, pos, depth, node = stack.pop()
            rows.append(RowInfo(len(rows), depth, kp, pos))
            if node.open:
                n = len(node.kids)
                for q in reversed(range(n)):
                    c = q if node.order is None else node.order[q]
                    stack.append((kp + (c,), q, depth + 1, node.kids[c]))
        return rows

    def open_keypaths(self) -> list[tuple]:
        return [kp for kp, node in self.nodes() if node.open]


def apply_remap(items: list, remap: Remap, make) -> list:
    """The list after ``remap`` (surviving items at their new index, new ones made)."""
    n = len(items) + remap.delta()
    out: list = [None] * n
    for i, item in enumerate(items):
        j = remap.map(i)
        if j is not None:
            assert out[j] is None, "remap is not injective"
            out[j] = item
    return [make() if x is None else x for x in out]


def recorded(tree: FlatTree) -> list[tuple]:
    out = []
    stack = list(tree._root.by_idx.values())
    while stack:
        rec = stack.pop()
        out.append(rec.keypath)
        stack.extend(rec.by_idx.values())
    return out


def check(tree: FlatTree, model: Model) -> None:
    rows = model.rows()
    assert tree.row_count() == len(rows)
    assert list(tree.iter_rows(0, len(rows))) == rows
    for info in rows[:: max(1, len(rows) // 7)]:
        assert tree.locate(info.row) == info
    visible = {info.keypath: info.row for info in rows}
    for kp, node in model.nodes():
        assert tree.row_of(kp) == visible.get(kp), kp
        assert tree.is_expanded(kp) == node.open, kp
    opened = model.open_keypaths()
    assert sorted(tree.expanded_keypaths(), key=repr) == sorted(opened, key=repr)
    # white box: only open nodes and their ancestors are recorded, with consistent key paths
    want = {kp[:k] for kp in opened for k in range(1, len(kp) + 1)}
    assert sorted(recorded(tree), key=repr) == sorted(want, key=repr)
    stack = list(tree._root.by_idx.items())
    while stack:
        key, rec = stack.pop()
        assert rec.key == key and rec.keypath[-1] == key
        for c, child in rec.by_idx.items():
            assert child.keypath == rec.keypath + (c,)
            stack.append((c, child))
    # top_runs over a few windows
    for a, b in ((0, len(rows) - 1), (len(rows) // 3, 2 * len(rows) // 3), (1, len(rows) - 2)):
        assert list(tree.top_runs(a, b)) == naive_top_runs(rows, a, b)


def naive_top_runs(rows: list[RowInfo], a: int, b: int) -> list[tuple[int, int, int]]:
    out: list[list[int]] = []
    for info in rows[max(0, a):b + 1]:
        if info.depth:
            continue
        if out and out[-1][0] + out[-1][2] == info.row and out[-1][1] + out[-1][2] == info.pos:
            out[-1][2] += 1
        else:
            out.append([info.row, info.pos, 1])
    return [tuple(x) for x in out]


# --------------------------------------------------------------------------- remap generation
def random_remap(rng: random.Random, n: int) -> Remap:
    """A random Remap over ``n`` items (one or two ops of any kind, or a "pieces" remap)."""
    kinds = ["ins", "del", "inss", "dels", "move", "unmove", "pieces", "two"]
    kind = rng.choice(kinds)
    if kind == "two":
        first = random_remap_op(rng, n, rng.choice(kinds[:6]))
        m = n + Remap([first]).delta()
        second = random_remap_op(rng, m, rng.choice(kinds[:6]))
        return Remap([first, second])
    if kind == "pieces":
        old = PieceTable.identity(n)
        new = old
        if n and rng.random() < 0.5:
            k = rng.randint(1, max(1, n // 2))
            pos = array("Q", sorted(rng.sample(range(n), k)))
            new = new.delete_positions(pos)
        if rng.random() < 0.5 and new.count:
            k = rng.randint(1, new.count)
            pos = array("Q", sorted(rng.sample(range(new.count), k)))
            new = new.move(pos, rng.randint(0, new.count - k))
        if rng.random() < 0.6:
            new = new.insert(rng.randint(0, new.count), n + 1000, rng.randint(1, 3))
        return Remap([("pieces", old, new)])
    return Remap([random_remap_op(rng, n, kind)])


def random_remap_op(rng: random.Random, n: int, kind: str) -> tuple:
    if kind == "ins" or n == 0:
        return ("ins", rng.randint(0, n), rng.randint(1, 3))
    if kind == "del":
        pos = rng.randrange(n)
        return ("del", pos, rng.randint(1, min(2, n - pos)))
    if kind == "inss":
        m = rng.randint(1, 3)
        return ("inss", array("Q", sorted(rng.sample(range(n + m), m))))
    if kind == "dels":
        k = rng.randint(1, max(1, n // 2))
        return ("dels", array("Q", sorted(rng.sample(range(n), k))))
    k = rng.randint(1, n)
    pos = array("Q", sorted(rng.sample(range(n), k)))
    to = rng.randint(0, n - k)
    if kind == "move":
        return ("move", pos, to)
    # "unmove": the inverse of a move (positions are the FINAL positions of the moved items)
    inv = Remap([("move", pos, to)]).inverse()
    return inv.ops[0] if inv.ops else ("move", pos, to)    # a block moved onto itself: no-op


# --------------------------------------------------------------------------- driver
class Driver:
    """Applies random operations to a FlatTree and the model."""

    def __init__(self, rng: random.Random, *, view_mode: bool) -> None:
        self.rng = rng
        tops = [self.make_node(depth=0) for _ in range(rng.randint(0, 9))]
        self.model = Model(tops)
        self.tree = FlatTree(len(tops))
        self.view_mode = view_mode
        if view_mode:
            self.new_view()

    def make_node(self, depth: int = 1) -> MNode:
        rng = self.rng
        node = MNode()
        if depth < 3 and rng.random() < 0.7:
            node.kids = [self.make_node(depth + 1) for _ in range(rng.randint(0, 4))]
        return node

    # -- top level ------------------------------------------------------------------------
    def new_view(self) -> None:
        """A filtered / sorted view: a random subset of the tops in a random order."""
        keys = list(range(len(self.model.tops)))
        self.rng.shuffle(keys)
        self.model.view = keys[: self.rng.randint(0, len(keys))]
        self.set_view()

    def set_view(self) -> None:
        view = list(self.model.view or [])
        positions = {k: p for p, k in enumerate(view)}
        self.tree.set_top(len(view), top_key=view.__getitem__, top_pos=positions.get)

    # -- node choice ------------------------------------------------------------------------
    def pick(self, *, containers: bool = False, visible: bool = False) -> tuple[tuple, MNode] | None:
        if visible:
            rows = self.model.rows()
            nodes = dict(self.model.nodes())
            cands = [(r.keypath, nodes[r.keypath]) for r in rows]
        else:
            cands = self.model.nodes()
        if containers:
            cands = [c for c in cands if c[1].kids]
        return self.rng.choice(cands) if cands else None

    def pick_recorded(self) -> tuple[tuple, MNode] | None:
        """An open node or an ancestor of one (the nodes the FlatTree records)."""
        nodes = self.model.nodes()
        opened = {kp for kp, n in nodes if n.open}
        want = {kp[:k] for kp in opened for k in range(1, len(kp) + 1)}
        cands = [(kp, n) for kp, n in nodes if kp in want]
        return self.rng.choice(cands) if cands else None

    def order_for(self, node: MNode) -> object:
        n = len(node.kids)
        r = self.rng.random()
        if r < 0.5:
            return None
        perm = self.rng.sample(range(n), n)
        return DisplayOrder(perm) if r < 0.75 else perm

    # -- operations ---------------------------------------------------------------------------
    def step(self) -> None:
        rng = self.rng
        op = rng.random()
        if op < 0.30:
            got = self.pick(visible=rng.random() < 0.7)
            if got is None:
                return
            kp, node = got
            order = self.order_for(node)
            self.tree.expand(kp, len(node.kids), order)
            node.open = True
            node.order = None if order is None else list(order)
        elif op < 0.42:
            opened = [(kp, n) for kp, n in self.model.nodes() if n.open]
            if not opened:
                return
            kp, node = rng.choice(opened)
            self.tree.collapse(kp)
            node.open = False
            node.order = None
        elif op < 0.48:
            opened = [(kp, n) for kp, n in self.model.nodes() if n.open]
            if not opened:
                return
            kp, node = rng.choice(opened)
            order = self.order_for(node)
            self.tree.set_order(kp, order)
            node.order = None if order is None else list(order)
        elif op < 0.80:
            got = self.pick_recorded() if rng.random() < 0.6 else self.pick(containers=rng.random() < 0.8)
            if got is None:
                return
            kp, node = got
            remap = random_remap(rng, len(node.kids))
            node.kids = apply_remap(node.kids, remap, self.make_node)
            if remap.identity:
                return
            self.tree.remap_children(kp, remap, len(node.kids))
            node.order = None    # a remapped node shows file order (open or not)
        elif op < 0.95:
            remap = random_remap(rng, len(self.model.tops))
            self.model.tops = apply_remap(self.model.tops, remap, lambda: self.make_node(depth=0))
            self.tree.remap_tops(remap)
            if self.view_mode:
                # the session remaps the view set: deleted records leave it, order kept
                kept = []
                for k in self.model.view or []:
                    j = remap.map(k)
                    if j is not None:
                        kept.append(j)
                self.model.view = kept
                self.set_view()
        elif self.view_mode:
            self.new_view()


def run(seed: int, steps: int, *, view_mode: bool, every: int = 1) -> None:
    driver = Driver(random.Random(seed), view_mode=view_mode)
    check(driver.tree, driver.model)
    for k in range(steps):
        driver.step()
        assert driver.tree.row_count() == len(driver.model.rows())
        if k % every == 0:
            check(driver.tree, driver.model)
    check(driver.tree, driver.model)


@settings(max_examples=120, deadline=None, derandomize=True, database=None,
          suppress_health_check=[HealthCheck.too_slow])
@given(seed=st.integers(0, 2**32 - 1), view_mode=st.booleans())
def test_remaps_match_the_model_hypothesis(seed: int, view_mode: bool) -> None:
    run(seed, 40, view_mode=view_mode)


@pytest.mark.parametrize("seed", range(12))
def test_remaps_match_the_model_long_walks(seed: int) -> None:
    """Long walks, checks every few steps (several changes accumulate between lookups)."""
    run(1000 + seed, 400, view_mode=bool(seed % 2), every=7)


# --------------------------------------------------------------------------- explicit cases
def keypaths(tree: FlatTree) -> list[tuple]:
    return [info.keypath for info in tree.iter_rows(0, tree.row_count())]


def test_remap_children_shifts_expansion_after_a_delete() -> None:
    tree = FlatTree(2)
    tree.expand((0,), 4)
    tree.expand((0, 3), 2)          # child 3 expanded
    tree.expand((0, 1), 1)          # child 1 expanded
    tree.remap_children((0,), Remap([("del", 1, 1)]), 3)    # child 1 deleted: 3 -> 2
    assert tree.is_expanded((0, 2)) and not tree.is_expanded((0, 1)) and not tree.is_expanded((0, 3))
    assert keypaths(tree) == [(0,), (0, 0), (0, 1), (0, 2), (0, 2, 0), (0, 2, 1), (1,)]


def test_remap_children_insert_and_move_follow_the_nodes() -> None:
    tree = FlatTree(1)
    tree.expand((0,), 3)
    tree.expand((0, 0), 1)
    tree.expand((0, 0, 0), 2)       # a deep expansion below child 0
    tree.remap_children((0,), Remap([("ins", 0, 2)]), 5)     # two new children in front
    assert tree.expanded_keypaths() == [(0,), (0, 2), (0, 2, 0)]
    tree.remap_children((0,), Remap([("move", array("Q", [2]), 4)]), 5)   # child 2 to the end
    assert tree.expanded_keypaths() == [(0,), (0, 4), (0, 4, 0)]
    assert keypaths(tree)[-4:] == [(0, 4), (0, 4, 0), (0, 4, 0, 0), (0, 4, 0, 1)]


def test_remap_children_drops_the_display_order_and_subtrees() -> None:
    tree = FlatTree(1)
    tree.expand((0,), 3, [2, 0, 1])
    tree.expand((0, 2), 2)
    assert keypaths(tree) == [(0,), (0, 2), (0, 2, 0), (0, 2, 1), (0, 0), (0, 1)]
    # replacing the value of the container: every old child is gone (del all, ins new)
    tree.remap_children((0,), Remap([("del", 0, 3), ("ins", 0, 2)]), 2)
    assert keypaths(tree) == [(0,), (0, 0), (0, 1)]
    assert tree.expanded_keypaths() == [(0,)]


def test_remap_children_of_a_collapsed_node_keeps_remembered_expansions() -> None:
    tree = FlatTree(1)
    tree.expand((0,), 4)
    tree.expand((0, 3), 1)
    tree.collapse((0,))
    tree.remap_children((0,), Remap([("dels", array("Q", [0, 1]))]), 2)
    assert tree.expanded_keypaths() == [(0, 1)]           # remembered, re-keyed
    tree.expand((0,), 2)
    assert keypaths(tree) == [(0,), (0, 0), (0, 1), (0, 1, 0)]
    # deleting the only remembered child forgets the collapsed record too
    tree.collapse((0,))
    tree.remap_children((0,), Remap([("del", 1, 1)]), 1)
    assert tree.expanded_keypaths() == [] and recorded(tree) == []


def test_remap_children_of_an_unrecorded_node_is_a_no_op() -> None:
    tree = FlatTree(3)
    tree.expand((1,), 2)
    tree.remap_children((0,), Remap([("ins", 0, 1)]), 9)
    tree.remap_children((1, 0), Remap([("del", 0, 1)]), 0)
    assert tree.expanded_keypaths() == [(1,)] and tree.row_count() == 5


def test_remap_children_validates_before_changing() -> None:
    tree = FlatTree(1)
    tree.expand((0,), 2)
    with pytest.raises(ValueError):
        tree.remap_children((0,), Remap([("ins", 0, 1)]), -1)
    assert tree.row_count() == 3


def test_remap_children_of_a_hidden_open_node_propagates_when_shown() -> None:
    tree = FlatTree(1)
    tree.expand((0,), 2)
    tree.expand((0, 1), 3)
    tree.collapse((0,))
    tree.remap_children((0, 1), Remap([("ins", 3, 4)]), 7)
    assert tree.row_count() == 1
    tree.expand((0,), 2)
    assert tree.row_count() == 1 + 2 + 7


def test_remap_tops_identity_mode() -> None:
    tree = FlatTree(5)
    tree.expand((1,), 2)
    tree.expand((3,), 1)
    tree.expand((3, 0), 1)
    tree.remap_tops(Remap([("del", 0, 2)]))              # records 0 and 1 deleted
    assert tree.top_count == 3
    assert tree.expanded_keypaths() == [(1,), (1, 0)]
    assert keypaths(tree) == [(0,), (1,), (1, 0), (1, 0, 0), (2,)]
    tree.remap_tops(Remap([("ins", 0, 2)]))
    assert tree.top_count == 5 and tree.expanded_keypaths() == [(3,), (3, 0)]
    tree.remap_tops(Remap([("move", array("Q", [3]), 0)]))
    assert tree.expanded_keypaths() == [(0,), (0, 0)] and tree.row_of((0, 0, 0)) == 2
    tree.remap_tops(None)
    tree.remap_tops(Remap())
    assert tree.top_count == 5


def test_remap_tops_keeps_non_integer_keys() -> None:
    from jsonreshape.gui.treemodel import WRAPPER

    keys = [WRAPPER, 0, 1, 2]
    tree = FlatTree(4, top_key=keys.__getitem__, top_pos={k: p for p, k in enumerate(keys)}.get)
    tree.expand((WRAPPER,), 2)
    tree.expand((2,), 1)
    tree.remap_tops(Remap([("del", 0, 1)]))
    keys = [WRAPPER, 0, 1]
    tree.set_top(3, top_key=keys.__getitem__, top_pos={k: p for p, k in enumerate(keys)}.get)
    assert tree.expanded_keypaths() == [(1,), (WRAPPER,)] or tree.expanded_keypaths() == [(WRAPPER,), (1,)]
    assert keypaths(tree) == [(WRAPPER,), (WRAPPER, 0), (WRAPPER, 1), (0,), (1,), (1, 0)]


def test_remap_tops_with_view_functions_hides_conflicts_until_set_top() -> None:
    view = [4, 2, 0]
    pos = {k: p for p, k in enumerate(view)}
    tree = FlatTree(3, top_key=view.__getitem__, top_pos=pos.get)
    tree.expand((4,), 1)
    tree.expand((2,), 1)
    # record 3 deleted: 4 -> 3; the old top_pos does not know 3 yet -> hidden for now
    tree.remap_tops(Remap([("del", 3, 1)]))
    assert tree.row_count() == 3 + 1
    view = [3, 2, 0]
    pos = {k: p for p, k in enumerate(view)}
    tree.set_top(3, top_key=view.__getitem__, top_pos=pos.get)
    assert keypaths(tree) == [(3,), (3, 0), (2,), (2, 0), (0,)]


def test_top_runs_skip_expanded_subtrees() -> None:
    tree = FlatTree(10)
    tree.expand((2,), 3)
    tree.expand((5,), 0)            # expanded but empty: no rows below, runs continue
    tree.expand((7,), 2)
    # rows: 0 1 2 [3 4 5] 6(=3) 7(=4) 8(=5) 9(=6) 10(=7) [11 12] 13(=8) 14(=9)
    assert list(tree.top_runs(0, tree.row_count() - 1)) == [(0, 0, 3), (6, 3, 5), (13, 8, 2)]
    assert list(tree.top_runs(4, 11)) == [(6, 3, 5)]
    assert list(tree.top_runs(12, 12)) == []
    assert list(tree.top_runs(5, 6)) == [(6, 3, 1)]
    assert list(tree.top_runs(-5, 1)) == [(0, 0, 2)]
    assert list(tree.top_runs(20, 30)) == []
    big = FlatTree(10**12)
    big.expand((5,), 10)
    assert list(big.top_runs(0, 10**12 + 9)) == [(0, 0, 6), (16, 6, 10**12 - 6)]


def test_remap_is_fast_with_many_recorded_nodes() -> None:
    import time

    tree = FlatTree(100_000)
    for k in range(0, 100_000, 20):
        tree.expand((k,), 3)
        tree.expand((k, 1), 2)
    t0 = time.perf_counter()
    tree.remap_tops(Remap([("del", 0, 1)]))
    assert tree.is_expanded((19, 1)) and tree.top_count == 99_999
    tree.remap_children((19,), Remap([("ins", 0, 1)]), 4)
    assert tree.is_expanded((19, 2))
    assert time.perf_counter() - t0 < 2.0
