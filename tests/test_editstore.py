"""jsonreshape.editstore: the edit layer on disk (docs/ARCHITECTURE.md §12.2).

* ``Step``: exact JSON round trip (hypothesis; lists > 4096 entries through segment blobs; lone
  surrogates, exact floats, 64-bit integers), validation;
* ``apply_step`` / ``undo_step``: symmetry for every step kind against an in-memory model (random driver,
  ``tests/editstore_helpers.py``), push_layer pops the array layer AND the empty dict layer, undo never
  lowers ``next_rid``, steps that do not fit a state are refused;
* replay equality: a state built step by step == ``load_state()`` after reopening, also across
  checkpoints (``maybe_trim``) and 64th-step piece-table snapshots; ``load_state(upto=)`` for the history;
* redo truncation deletes private segments and layer files; garbage collection at open;
* ``append``: partial writes and a full disk leave the segment consistent;
* lazy creation, the lock folder, both locations, the beside -> app folder fallback notice, custom
  folders, read-only / stale layers, format versions, damaged layers (``.last_good``);
* ``find_pending``: matching, stale, locked, damaged, tombstoned, head-0, both locations, save journal;
* ``discard`` while an ArrayLayer / DatReader of the layer is still open (Windows deletion semantics);
* ``LayerLock`` within one process (threads); every document kind and both core backends.

Subprocess crash consistency and cross-process locks: ``tests/test_editstore_crash.py``.
"""

from __future__ import annotations

import gc
import os
import random
import threading
import time
from array import array
from pathlib import Path
from typing import Any

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

import jsongen
from editstore_helpers import Driver, base_records, effective, model_of, signature
from jsonreshape import editstore as es
from jsonreshape import i18n, reader
from jsonreshape.editstore import (
    DB_NAME,
    EditStore,
    LayerDamagedError,
    LayerLock,
    PendingInfo,
    Step,
    apply_step,
    find_pending,
    layer_key,
    undo_step,
)
from jsonreshape.errors import EditError
from jsonreshape.fileio import long_path, make_dirs, open_shared
from jsonreshape.overlay import (
    EDIT_BASE,
    ArrayLayer,
    DatReader,
    DictLayer,
    NodeChange,
    OverlayState,
    PieceTable,
    Remap,
    segment_name,
    write_array_layer,
)
from jsonreshape.reader import Document
from jsonreshape.settings import Settings

HYP = settings(derandomize=True, database=None, deadline=None, max_examples=120,
               suppress_health_check=[HealthCheck.function_scoped_fixture, HealthCheck.too_slow])
U64 = (1 << 64) - 1


# ----------------------------------------------------------------------------------
# fixtures
# ----------------------------------------------------------------------------------
@pytest.fixture
def make_doc(tmp_path: Path):
    docs: list[Document] = []
    counter = [0]

    def make(recs: Any = None, fmt: str = "array", *, name: str | None = None, folder: Path | None = None,
             record_path: Any = None, format: str | None = None, **write_kw: Any) -> Document:
        counter[0] += 1
        d = folder or tmp_path
        make_dirs(d)
        p = d / (name or f"doc{counter[0]}.json")
        if not isinstance(recs, (bytes, bytearray)):
            tmp = tmp_path / f".gen{counter[0]}"
            jsongen.write_file(tmp, recs if recs is not None else jsongen.records(12, seed=counter[0]), fmt,
                               **write_kw)
            recs = tmp.read_bytes()
            tmp.unlink()
        with open(long_path(p), "wb") as f:
            f.write(bytes(recs))
        doc = Document.open(p, record_path=record_path, format=format)
        doc.build_index()
        docs.append(doc)
        return doc

    yield make
    for doc in docs:
        doc.close()


@pytest.fixture
def stores():
    """Stores opened by a test are closed at teardown (their locks released)."""
    opened: list[EditStore] = []

    def track(store: EditStore | None) -> EditStore | None:
        if store is not None:
            opened.append(store)
        return store

    yield track
    for s in opened:
        s.close()


def beside_settings() -> Settings:
    return Settings(edits_location="beside")


def commit_set(store: EditStore, state: OverlayState, pos: int, data: bytes, **kw: Any) -> OverlayState:
    rid = state.rid_at(pos)
    span = store.append(data)
    step = Step("edit", "edits.step.set_value", {}, content=[(rid, state.top.get(rid), span)], **kw)
    store.commit(step, state=state)
    return apply_step(state, step, layers=store)


def simulate_crash(store: EditStore) -> None:
    """Drop a store like a killed process would: handles closed, nothing cleaned up."""
    for w in store._writers.values():
        w.f.close()
    store._con.close()
    store._con = None
    store.closed = True
    store.lock.release()


def files_of(path: Path) -> set[str]:
    return set(os.listdir(path)) if path.exists() else set()


class Blobs:
    """In-memory blob space for Step round trips."""

    def __init__(self) -> None:
        self.data = bytearray()
        self.writes = 0

    def write(self, b: bytes) -> tuple[int, int]:
        self.writes += 1
        s = EDIT_BASE + len(self.data)
        self.data += b
        return s, s + len(b)

    def read(self, s: int, e: int) -> bytes:
        return bytes(self.data[s - EDIT_BASE:e - EDIT_BASE])


def roundtrip(step: Step, blobs: Blobs | None = None) -> Step:
    data = step.to_json(None if blobs is None else blobs.write)
    text = es._dumps(data)
    return Step.from_json(es._loads(text), None if blobs is None else blobs.read, seq=step.seq, ts=step.ts)


# ----------------------------------------------------------------------------------
# Step: JSON round trip and validation
# ----------------------------------------------------------------------------------
u64 = st.integers(0, U64)
small = st.integers(0, 50)
spans = st.tuples(u64, u64).map(lambda t: (min(t), max(t)))
opt_spans = st.one_of(st.none(), spans)
texts = st.one_of(st.text(max_size=12), st.sampled_from(["id\ud800", "Тошкент", "O'zbekiston", "Oʻzbekiston",
                                                          "\udfff\ud83d", "", "a/b~c"]))
json_scalars = st.one_of(st.none(), st.booleans(), st.integers(-(1 << 63), U64),
                         st.floats(allow_nan=False, allow_infinity=False), texts)
json_values = st.recursive(json_scalars, lambda c: st.one_of(st.lists(c, max_size=3),
                                                              st.dictionaries(texts, c, max_size=3)), max_leaves=8)
positions = st.lists(small, min_size=1, max_size=6, unique=True).map(sorted)


@st.composite
def tables(draw: Any) -> PieceTable:
    rids = draw(st.lists(st.integers(0, 60), max_size=12, unique=True))
    return PieceTable.from_pairs(rids, [r + 1 for r in rids])


@st.composite
def ops_lists(draw: Any) -> list[tuple]:
    out = []
    for _ in range(draw(st.integers(1, 4))):
        kind = draw(st.sampled_from(["ins", "del", "inss", "dels", "move", "unmove", "pieces"]))
        if kind in ("ins", "del"):
            out.append((kind, draw(small), draw(st.integers(1, 9))))
        elif kind in ("inss", "dels"):
            out.append((kind, draw(positions)))
        elif kind == "move":
            out.append((kind, draw(positions), draw(small)))
        elif kind == "unmove":
            out.append((kind, draw(small), draw(positions)))
        else:
            out.append((kind, draw(tables()), draw(tables())))
    return out


@st.composite
def steps(draw: Any) -> Step:
    struct = draw(st.one_of(st.none(), ops_lists()))
    new_rids: list[tuple[int, int, int]] = []
    old_rids = None
    if struct is not None:
        ops = Remap(struct).ops
        ins = sum(op[2] if op[0] == "ins" else len(op[1]) for op in ops if op[0] in ("ins", "inss"))
        rem = sum(op[2] if op[0] == "del" else len(op[1]) for op in ops if op[0] in ("del", "dels"))
        if ins:
            cut = draw(st.integers(1, ins))
            new_rids = [(draw(small), draw(st.integers(0, 1 << 40)), cut)]
            if ins - cut:
                new_rids.append((draw(small), draw(st.integers(0, 1 << 40)), ins - cut))
        if rem and draw(st.booleans()):
            old_rids = [(draw(small), draw(st.integers(0, 1 << 40)), rem)]
    content = draw(st.dictionaries(u64, st.tuples(opt_spans, opt_spans), max_size=6))
    nodes = draw(st.one_of(st.none(), st.lists(st.builds(
        lambda rec, ipath, ops, count: NodeChange(rec, tuple(ipath), Remap(ops), count),
        st.one_of(st.none(), small), st.lists(small, max_size=4), ops_lists(), small), max_size=3)))
    return Step(
        kind=draw(st.sampled_from(["edit", "bulk", "patch"])),
        label_key=draw(st.sampled_from(["edits.step.set_value", "edits.step.delete", "x.unknown"])),
        label_params=draw(st.dictionaries(st.text(max_size=6), json_values, max_size=3)),
        content=[(rid, old, new) for rid, (old, new) in content.items()],
        struct=struct,
        pieces_blob=draw(st.one_of(st.none(), spans)),
        push_layer=draw(st.one_of(st.none(), st.just("layer-00003.bin"))),
        segments=draw(st.lists(st.integers(1, 1000), max_size=3, unique=True)),
        wrapper=draw(st.one_of(st.none(), st.tuples(opt_spans, opt_spans, opt_spans, opt_spans))),
        flags=draw(st.one_of(st.none(), st.tuples(st.integers(0, 7), st.integers(0, 7)))),
        nodes=nodes,
        records=draw(st.one_of(st.none(), st.lists(u64, max_size=5))),
        keys=draw(st.one_of(st.none(), st.lists(st.tuples(u64, st.one_of(st.none(), texts),
                                                          st.one_of(st.none(), texts)), max_size=3))),
        select=draw(st.one_of(st.none(), st.tuples(st.one_of(st.none(), small), st.lists(st.one_of(texts, small),
                                                                                        max_size=3),
                                                   st.one_of(st.none(), st.lists(small, max_size=3))))),
        seq=draw(st.integers(0, 10 ** 6)),
        ts=draw(st.floats(0, 4e9)),
        new_rids=new_rids,
        old_rids=old_rids,
        next_rid=draw(st.integers(0, 1 << 40)),
    )


@HYP
@given(steps(), st.booleans())
def test_step_json_round_trip_is_exact(step: Step, with_blobs: bool) -> None:
    back = roundtrip(step, Blobs() if with_blobs else None)
    assert back == step
    assert back.label == step.label
    if step.struct is not None:
        assert back.remap is not None and back.remap.ops == step.remap.ops  # type: ignore[union-attr]
    again = roundtrip(back, Blobs())
    assert again == step


def test_long_lists_go_through_blobs_and_round_trip() -> None:
    n = 5000
    rng = random.Random(3)
    dels = array("Q", sorted(rng.sample(range(100_000), n)))
    step = Step(
        "patch", "edits.step.patch", {"count": n, "nested": [1, (2, 3)], "f": -0.0},
        content=[(i, None if i % 3 else (i, i + 1), (EDIT_BASE + i, EDIT_BASE + i + 9)) for i in range(n)],
        struct=[("dels", dels), ("inss", array("Q", range(0, 2 * n, 2)))],
        new_rids=[(0, 10 ** 6, n)], old_rids=[(int(p), int(p), 1) for p in dels],
        nodes=[NodeChange(1, (0,), Remap([("dels", array("Q", range(n)))]), 3)] * 2,
        records=list(range(n)), keys=[(i, f"k{i}", None if i % 2 else "Тошкент\ud800") for i in range(n)],
        select=(0, ("a", 0), None))
    blobs = Blobs()
    data = step.to_json(blobs.write)
    text = es._dumps(data)
    assert len(text) < 2000, "long lists must be blobs, not inline JSON"
    assert blobs.writes >= 8
    back = Step.from_json(es._loads(text), blobs.read)
    assert back == step
    assert back.label_params == {"count": n, "nested": [1, [2, 3]], "f": -0.0}
    with pytest.raises(ValueError):
        Step.from_json(es._loads(text), None)      # blobs need a reader


def test_step_validation() -> None:
    with pytest.raises(ValueError):
        Step("", "edits.step.set_value")
    with pytest.raises(ValueError):
        Step("edit", "k", content=[(1, None, (5, 4))])          # reversed span
    with pytest.raises(ValueError):
        Step("edit", "k", content=[(1, None, None), (1, None, None)])   # a rid twice
    with pytest.raises(ValueError):
        Step("edit", "k", label_params={"x": float("nan")})
    with pytest.raises(ValueError):
        Step("edit", "k", label_params={"x": 1 << 64})
    with pytest.raises(ValueError):
        Step("edit", "k", label_params={1: "x"})
    with pytest.raises(ValueError):
        Step("edit", "k", struct=[("ins", 0, 2)], new_rids=[(0, 5, 1)])     # 2 inserted, 1 rid
    with pytest.raises(ValueError):
        Step("edit", "k", struct=[("del", 0, 2)], old_rids=[(0, 5, 3)])
    with pytest.raises(ValueError):
        Step("edit", "k", new_rids=[(0, 5, 1)])                             # rids without structure
    with pytest.raises(ValueError):
        Step("edit", "k", push_layer="../evil.bin")
    with pytest.raises(ValueError):
        Step("edit", "k", segments=[0])                                      # seg 0 is never private
    with pytest.raises(ValueError):
        Step("edit", "k", keys=[(1, 5, None)])
    with pytest.raises(ValueError):
        Step("edit", "k", flags=(1,))
    # accepted alternative forms of the structure; identity remaps disappear
    r = Remap([("ins", 3, 2)])
    a = Step("edit", "k", struct=r, new_rids=[(3, 9, 2)])
    b = Step("edit", "k", struct=[r, [(3, 9, 2)]])
    assert a == b and a.struct == [("ins", 3, 2)] and a.old_rids == []
    assert Step("edit", "k", struct=[("ins", 0, 0)]).struct is None
    # select is kept as nested tuples
    assert Step("edit", "k", select=[1, ["a", 2], [0]]).select == (1, ("a", 2), (0,))


def test_step_label_and_unknown_keys(lang_en) -> None:
    assert Step("edit", "edits.step.delete", {"count": 3}).label == "Items deleted: 3"
    assert es.step_label("no.such.key") == "Change"
    assert es.step_label("") == "Change"
    i18n.set_language("uz")
    assert es.step_label("edits.step.delete", {"count": 3}) == "3 ta element o'chirildi"


# ----------------------------------------------------------------------------------
# apply / undo
# ----------------------------------------------------------------------------------
def test_removed_rids_feed_the_inverse() -> None:
    rng = random.Random(5)
    for _ in range(200):
        n = rng.randint(1, 30)
        rids = list(range(n))
        rng.shuffle(rids)
        tbl = PieceTable.from_pairs(rids, [r + 1 for r in rids])
        cur = list(rids)
        ops = []
        new_rids = []
        nxt = 100
        for _k in range(rng.randint(1, 4)):
            kind = rng.choice(["del", "dels", "ins", "inss", "move"]) if cur else "ins"
            if kind == "del":
                p = rng.randrange(len(cur))
                k = rng.randint(1, len(cur) - p)
                ops.append(("del", p, k))
                del cur[p:p + k]
            elif kind == "dels":
                ps = sorted(rng.sample(range(len(cur)), rng.randint(1, len(cur))))
                ops.append(("dels", ps))
                cur = [r for i, r in enumerate(cur) if i not in set(ps)]
            elif kind == "ins":
                p = rng.randint(0, len(cur))
                k = rng.randint(1, 3)
                ops.append(("ins", p, k))
                new_rids.append((p, nxt, k))
                cur[p:p] = list(range(nxt, nxt + k))
                nxt += k
            elif kind == "inss":
                k = rng.randint(1, 3)
                ps = sorted(rng.sample(range(len(cur) + k), k))
                ops.append(("inss", ps))
                new_rids.append((ps[0], nxt, k))
                for j, p in enumerate(ps):
                    cur.insert(p, nxt + j)
                nxt += k
            else:
                ps = sorted(rng.sample(range(len(cur)), rng.randint(1, len(cur))))
                to = rng.randint(0, len(cur) - len(ps))
                ops.append(("move", ps, to))
                moved = [cur[p] for p in ps]
                rest = [r for i, r in enumerate(cur) if i not in set(ps)]
                cur = rest[:to] + moved + rest[to:]
        remap = Remap(ops)
        after = tbl.apply(remap, new_rids=new_rids)
        assert list(after.rids(0, after.count)) == cur
        old = es.removed_rids(tbl, remap.ops, new_rids)
        assert after.apply(remap.inverse(), new_rids=old) == tbl


@pytest.mark.parametrize("kind", ["set", "insert", "delete", "move", "dup", "bulk", "wrapper", "multi", "pieces",
                                  "flags"])
def test_apply_undo_symmetry_per_kind(make_doc, stores, kind: str) -> None:
    doc = make_doc(jsongen.records(15, seed=2), "array", style="pretty")
    store = stores(EditStore.open(doc))
    drv = Driver(doc, store, seed=11, kinds=(kind,), undo=0.0, redo=0.0, check_undo=True)
    states = [drv.state]
    models = [drv.model]
    for _ in range(12):
        drv.action()
        assert drv.current() == drv.model
        states.append(drv.state)
        models.append(drv.model)
    # walk back through the store (undo) and forward again (redo) comparing every state
    state = drv.state
    for i in range(len(states) - 1, 0, -1):
        step = store.step(store.head)
        store.set_head(store.head - 1)
        state = undo_step(state, step, layers=store)
        assert signature(state)[:6] == signature(states[i - 1])[:6]
        assert model_of(state, drv.base, drv.raw) == models[i - 1]
        assert state.next_rid == drv.state.next_rid          # undo never lowers next_rid
        assert state.version == store.head
    for i in range(1, len(states)):
        step = store.step(store.head + 1)
        store.set_head(store.head + 1)
        state = apply_step(state, step, layers=store)
        assert signature(state)[:6] == signature(states[i])[:6]
        assert model_of(state, drv.base, drv.raw) == models[i]
    assert not store.can_redo()


def test_random_driver_matches_model_and_undo_everything(make_doc, stores) -> None:
    doc = make_doc(jsongen.records(20, seed=4), "jsonl")
    store = stores(EditStore.open(doc))
    drv = Driver(doc, store, seed=99, check_undo=True, undo=0.15, redo=0.15)
    original = drv.model
    for _ in range(150):
        drv.action()
        assert drv.current() == drv.model
    while store.head > store.checkpoint_seq:
        step = store.step(store.head)
        store.set_head(store.head - 1)
        drv.state = undo_step(drv.state, step, layers=store)
    assert drv.current() == original
    assert drv.state.pieces == PieceTable.identity(store.base_count)
    assert all(not len(layer) for layer in drv.state.layers if isinstance(layer, DictLayer))


def test_push_layer_undo_pops_array_and_empty_dict(make_doc, stores, tmp_path) -> None:
    doc = make_doc(jsongen.records(6, seed=1))
    store = stores(EditStore.open(doc))
    state = store.load_state()
    state = commit_set(store, state, 0, b'{"x":1}')
    seg = store.new_segment()
    span = store.append(b'{"bulk":1}', seg=seg)
    store.sync([seg])
    path = store.new_layer_path()
    write_array_layer(path, [(2, *span)], flags=0, segments=[seg], work_dir=None)
    step = Step("bulk", "edits.step.bulk.replace", {"count": 1}, push_layer=path.name, segments=[seg])
    store.commit(step, state=state)
    after = apply_step(state, step, layers=store)
    assert len(after.layers) == len(state.layers) + 2
    assert isinstance(after.layers[-2], ArrayLayer) and isinstance(after.layers[-1], DictLayer)
    assert after.layers[-2].name == path.name and not len(after.layers[-1])
    back = undo_step(after, step, layers=store)
    assert signature(back)[:6] == signature(state)[:6]
    # a non-empty top dict layer (LIFO violated) or another layer on top is refused
    bad = after.with_content({0: span})
    with pytest.raises(LayerDamagedError):
        undo_step(bad, step, layers=store)
    with pytest.raises(LayerDamagedError):
        undo_step(state, step, layers=store)


def test_steps_that_do_not_fit_are_refused(make_doc, stores) -> None:
    doc = make_doc(jsongen.records(5, seed=1))
    store = stores(EditStore.open(doc))
    state = store.load_state()
    span = store.append(b'{"a":1}')
    store.sync()
    wrong_old = Step("edit", "k", content=[(1, (EDIT_BASE, EDIT_BASE + 1), span)])
    with pytest.raises(LayerDamagedError) as ei:
        apply_step(state, wrong_old)
    assert ei.value.key == "editstore.error.damaged"
    with pytest.raises(LayerDamagedError):
        apply_step(state, Step("edit", "k", struct=[("del", 3, 9)]))
    with pytest.raises(LayerDamagedError):
        apply_step(state, Step("edit", "k", wrapper=((1, 2), (3, 4), None, None)))
    with pytest.raises(LayerDamagedError):
        apply_step(state, Step("bulk", "k", push_layer="layer-00001.bin"), layers={})
    with pytest.raises(LayerDamagedError):
        undo_step(state, Step("edit", "k", content=[(1, None, span)]))


def test_next_rid_high_water_mark(make_doc, stores) -> None:
    doc = make_doc(jsongen.records(4, seed=1))
    store = stores(EditStore.open(doc))
    state = store.load_state()
    assert state.next_rid == 4
    for rid in (4, 5):
        span = store.append(b"{}")
        step = Step("edit", "k", content=[(rid, None, span)], struct=[("ins", rid, 1)], new_rids=[(rid, rid, 1)])
        store.commit(step, state=state)
        state = apply_step(state, step, layers=store)
    assert state.next_rid == 6
    back = undo_step(state, store.step(2), layers=store)
    store.set_head(1)
    assert back.next_rid == 6 and back.count == 5
    # explicit next_rid and "pieces" tables count too
    assert es.step_next_rid(Step("edit", "k", next_rid=77)) == 77
    new = PieceTable.from_pairs([0, 1, 2, 3, 40], [1, 2, 3, 4, 41])
    assert es.step_next_rid(Step("patch", "k", struct=[("pieces", PieceTable.identity(4), new)])) == 41
    store.close()
    store = stores(EditStore.open(doc))
    assert store.head == 1
    assert store.load_state().next_rid == 6          # persisted high-water mark after undo


# ----------------------------------------------------------------------------------
# persistence: replay, checkpoints, snapshots
# ----------------------------------------------------------------------------------
@pytest.mark.parametrize("seed", [1, 2, 3])
def test_replay_equals_in_memory_state_with_trims(make_doc, stores, seed: int) -> None:
    doc = make_doc(jsongen.records(14, seed=seed), "array", style="pretty2" if seed == 2 else "compact")
    store = stores(EditStore.open(doc))
    drv = Driver(doc, store, seed=seed, limit=10, undo=0.1, redo=0.1)
    for i in range(160):
        drv.action()
        if i % 40 == 39:     # reopen in the middle of the run too
            mem = drv.state
            store.close()
            store = stores(EditStore.open(doc))
            drv.store = store
            loaded = store.load_state()
            assert signature(loaded) == signature(mem)
            drv.state = loaded
    assert store.checkpoint_seq > 0, "the run must have folded steps into the checkpoint"
    mem = drv.state
    store.close()
    store = stores(EditStore.open(doc))
    loaded = store.load_state()
    assert signature(loaded) == signature(mem)
    assert model_of(loaded, drv.base, drv.raw) == drv.model
    for seq in range(store.checkpoint_seq, store.head + 1):
        assert model_of(store.load_state(upto=seq), drv.base, drv.raw) == drv.models[seq]
    with pytest.raises(ValueError):
        store.set_head(store.checkpoint_seq - 1)
    with pytest.raises(ValueError):
        store.load_state(upto=store.checkpoint_seq - 1)


def test_trim_keeps_limit_plus_batch(make_doc, stores) -> None:
    doc = make_doc(jsongen.records(30, seed=7))
    store = stores(EditStore.open(doc))
    state = store.load_state()
    for i in range(140):
        state = commit_set(store, state, i % 30, b'{"i":%d}' % i)
        store.maybe_trim(10)
        assert store.head - store.checkpoint_seq <= 10 + es.TRIM_BATCH
    assert store.checkpoint_seq == 128
    assert [s.seq for s in store.steps()] == list(range(129, 141))
    size0 = store.published(0)
    store.maybe_trim(10)                     # nothing to fold: nothing written
    assert store.published(0) == size0
    store.close()
    store = stores(EditStore.open(doc))
    loaded = store.load_state()
    assert signature(loaded) == signature(state)
    assert store.checkpoint_seq == 128 and store.head == 140


def test_every_64th_structural_step_stores_a_snapshot(make_doc, stores, monkeypatch) -> None:
    doc = make_doc(jsongen.records(8, seed=1))
    store = stores(EditStore.open(doc))
    state = store.load_state()
    for i in range(200):
        if i % 3 == 2:
            state = commit_set(store, state, 0, b'{"c":%d}' % i)
            continue
        span = store.append(b'{"n":%d}' % i)
        rid = state.next_rid
        step = Step("edit", "k", content=[(rid, None, span)], struct=[("ins", i % (state.count + 1), 1)],
                    new_rids=[(i % (state.count + 1), rid, 1)])
        store.commit(step, state=state if i % 2 else None)       # also the store's own head table
        state = apply_step(state, step, layers=store)
    snaps = [s for s in range(1, store.head + 1) if store.step(s).pieces_blob is not None]
    structural = [s for s in range(1, store.head + 1) if store.step(s).struct is not None]
    assert snaps == [structural[63], structural[127]]
    blob = store.step(snaps[-1]).pieces_blob
    assert PieceTable.from_bytes(store.reader.read(*blob)) == apply_step(
        store.load_state(upto=snaps[-1] - 1), store.step(snaps[-1])).pieces
    # load_state starts from the nearest snapshot: no structural replay before it
    calls = []
    orig = PieceTable.apply

    def counting(self: PieceTable, remap: Remap, **kw: Any) -> PieceTable:
        calls.append(1)
        return orig(self, remap, **kw)

    store.close()
    store = stores(EditStore.open(doc))
    monkeypatch.setattr(PieceTable, "apply", counting)
    loaded = store.load_state()
    monkeypatch.setattr(PieceTable, "apply", orig)
    assert signature(loaded) == signature(state)
    assert len(calls) == len([s for s in structural if s > snaps[-1]])


def test_redo_truncation_deletes_private_files(make_doc, stores) -> None:
    doc = make_doc(jsongen.records(10, seed=3))
    store = stores(EditStore.open(doc))
    state = store.load_state()
    state = commit_set(store, state, 1, b'{"a":1}')
    seg = store.new_segment()
    entries = [(r, *store.append(b'{"b":%d}' % r, seg=seg)) for r in range(0, 10, 2)]
    store.sync([seg])
    lpath = store.new_layer_path()
    write_array_layer(lpath, entries, flags=0, segments=[seg], work_dir=None)
    bulk = Step("bulk", "edits.step.bulk.replace", {"count": 5}, push_layer=lpath.name, segments=[seg])
    store.commit(bulk, state=state)
    bulk_state = apply_step(state, bulk, layers=store)
    held = bulk_state.layers[-2]           # a pinned state still maps the layer
    assert {segment_name(seg), lpath.name} <= files_of(store.dir)
    store.set_head(1)
    state = undo_step(bulk_state, store.step(2), layers=store)
    assert store.can_redo()
    state = commit_set(store, state, 3, b'{"c":1}')      # truncates the redo branch
    assert not store.can_redo() and store.max_seq == 2
    names = files_of(store.dir)
    assert segment_name(seg) not in names and lpath.name not in names
    assert held.get(0) == entries[0][1:]                # the mapping still reads (deleted file)
    assert store.step(2).label_key == "edits.step.set_value"
    store.close()
    store = stores(EditStore.open(doc))
    assert signature(store.load_state()) == signature(state)
    assert files_of(store.dir) <= {DB_NAME, DB_NAME + "-wal", segment_name(0)}
    held.close()


def test_open_collects_garbage_and_cuts_uncommitted_tails(make_doc, stores) -> None:
    doc = make_doc(jsongen.records(6, seed=3))
    store = stores(EditStore.open(doc))
    state = commit_set(store, store.load_state(), 0, b'{"a":1}')
    end = store.published(0)
    store.append(b"garbage that was never committed")
    store.sync()
    seg = store.new_segment()
    store.append(b"preview", seg=seg)
    lp = store.new_layer_path()
    write_array_layer(lp, [], flags=0, segments=[seg], work_dir=None)
    d = store.dir
    (d / "layer-00042.bin.tmp").write_bytes(b"x")
    (d / "jrlayer-1-abc.run").write_bytes(b"x")
    (d / "notes.txt").write_bytes(b"user file")
    simulate_crash(store)
    store = stores(EditStore.open(doc))
    names = files_of(d)
    assert segment_name(seg) not in names and lp.name not in names
    assert "layer-00042.bin.tmp" not in names and "jrlayer-1-abc.run" not in names
    assert "notes.txt" in names                       # files of others are never touched
    assert (d / segment_name(0)).stat().st_size == end
    assert signature(store.load_state()) == signature(state)
    assert store.new_segment() > seg                  # numbers of this run are not reused


# ----------------------------------------------------------------------------------
# segments
# ----------------------------------------------------------------------------------
def test_append_sync_publish(make_doc, stores) -> None:
    doc = make_doc(jsongen.records(3, seed=1))
    store = stores(EditStore.open(doc))
    s1 = store.append(b"abc")
    s2 = store.append(memoryview(b"defgh"))
    s3 = store.append(b"")
    assert s1 == (EDIT_BASE, EDIT_BASE + 3) and s2 == (EDIT_BASE + 3, EDIT_BASE + 8) and s3 == (s2[1], s2[1])
    with pytest.raises(EditError):
        store.reader.read(*s1)                        # not published before sync
    store.sync()
    assert store.reader.read(*s2) == b"defgh" and store.published(0) == 8
    seg = store.new_segment()
    a = store.append(b"xyz", seg=seg)
    assert es.split_edit_offset(a[0]) == (seg, 0)
    with pytest.raises(ValueError):
        store.append(b"x", seg=seg + 5)
    store.sync([seg])
    assert store.reader.read(*a) == b"xyz"
    store.drop_segment(seg)
    assert not store.segment_path(seg).exists()
    with pytest.raises(EditError):
        store.reader.read(*a)
    with pytest.raises(ValueError):
        store.drop_segment(0)


def test_partial_write_and_disk_full_leave_the_segment_consistent(make_doc, stores, monkeypatch, lang_en) -> None:
    doc = make_doc(jsongen.records(4, seed=1))
    store = stores(EditStore.open(doc))
    state = commit_set(store, store.load_state(), 0, b'{"a":"first"}')
    end = store.published(0)
    real = es._raw_write
    calls = {"n": 0}

    def partial_then_full(f: Any, view: memoryview) -> int:
        calls["n"] += 1
        if calls["n"] == 1:
            return real(f, view[:5])                 # a short write: the loop continues
        if calls["n"] == 2:
            real(f, view[:3])
            raise OSError(28, "No space left on device")
        return real(f, view)

    monkeypatch.setattr(es, "_raw_write", partial_then_full)
    with pytest.raises(EditError) as ei:
        store.append(b'{"b":"second unit, long enough"}')
    assert ei.value.key == "editstore.error.disk_full"
    assert "disk is full" in str(ei.value)
    assert store.segment_path(0).stat().st_size == end           # truncated back
    monkeypatch.setattr(es, "_raw_write", real)
    span = store.append(b'{"c":3}')
    assert span[0] == EDIT_BASE + end                            # continues at the old end
    step = Step("edit", "k", content=[(1, None, span)])
    store.commit(step, state=state)
    state = apply_step(state, step, layers=store)
    store.close()
    store = stores(EditStore.open(doc))
    loaded = store.load_state()
    assert loaded.read(*loaded.content(1)) == b'{"c":3}'
    assert signature(loaded) == signature(state)

    def other_error(f: Any, view: memoryview) -> int:
        raise OSError(5, "Input/output error")

    monkeypatch.setattr(es, "_raw_write", other_error)
    with pytest.raises(EditError) as ei:
        store.append(b"x")
    assert ei.value.key == "editstore.error.write"


def test_commit_refuses_unsynced_or_foreign_bytes(make_doc, stores) -> None:
    doc = make_doc(jsongen.records(4, seed=1))
    store = stores(EditStore.open(doc))
    state = store.load_state()
    with pytest.raises(ValueError):
        store.commit(Step("edit", "k", content=[(1, None, (EDIT_BASE + 10, EDIT_BASE + 20))]), state=state)
    with pytest.raises(ValueError):
        store.commit(Step("edit", "k", segments=[7]), state=state)
    with pytest.raises(ValueError):
        store.commit(Step("edit", "k"), state=state.derive(version=5))
    seg = store.new_segment()
    span = store.append(b"{}", seg=seg)
    step = Step("edit", "k", content=[(1, None, span)])           # a new segment the step uses is its own
    store.commit(step, state=state)
    assert step.segments == [seg] and step.seq == 1
    assert segment_name(seg) in store._files
    # a base (data file) span is fine: duplicates of base records keep their span
    st2 = apply_step(state, step, layers=store)
    dup = Step("edit", "k", content=[(4, None, doc.record_span(0))], struct=[("ins", 4, 1)], new_rids=[(4, 4, 1)])
    store.commit(dup, state=st2)


# ----------------------------------------------------------------------------------
# locations, lazy creation, locks
# ----------------------------------------------------------------------------------
def test_lazy_creation_and_lock_location(make_doc, stores, _isolated_app_home) -> None:
    doc = make_doc(jsongen.records(4, seed=1))
    store = stores(EditStore.open(doc, settings=beside_settings()))
    key = layer_key(doc)
    assert store.key == key and store.lock.held
    assert es.lock_path(doc) == Path(_isolated_app_home) / "edits" / "locks" / f"{key}.lock"
    assert es.lock_path(doc).exists()
    bdir = es.beside_dir(doc)
    assert store.dir == bdir and not bdir.exists()             # the lock alone creates nothing beside
    assert store.head == 0 and store.steps() == [] and not store.can_redo()
    assert signature(store.load_state()) == signature(OverlayState.initial(4, None))
    commit_set(store, store.load_state(), 0, b'{"x":1}')
    assert (bdir / DB_NAME).exists() and (bdir / segment_name(0)).exists()
    assert not (bdir / f"{key}.lock").exists()
    assert es.layer_dir(doc, settings=beside_settings()) == bdir
    assert es.layer_dir(doc) == Path(_isolated_app_home) / "edits" / key
    assert not [n for n in os.listdir(bdir.parent) if ".probe-" in n]


def test_beside_folder_not_writable_falls_back_with_notice(make_doc, stores, monkeypatch, lang_en) -> None:
    doc = make_doc(jsongen.records(4, seed=1))
    bdir = es.beside_dir(doc)
    real = es.make_dirs

    def deny(p: Any) -> None:
        if str(p).endswith(es.BESIDE_SUFFIX):
            raise PermissionError(13, "Access is denied", str(p))
        real(p)

    monkeypatch.setattr(es, "make_dirs", deny)
    es.take_notices()
    store = stores(EditStore.open(doc, settings=beside_settings()))
    assert store.dir == bdir
    state = commit_set(store, store.load_state(), 0, b'{"x":1}')
    assert store.dir == es._edits_root() / store.key and store.location == "appdata"
    assert not bdir.exists()
    assert [k for k, _p in store.warnings] == ["editstore.warn.beside_fallback"]
    notices = es.take_notices()
    assert notices and notices[0][0] == "editstore.warn.beside_fallback"
    text = i18n.t(*notices[0][:1], **notices[0][1])
    assert str(bdir) in text and "program folder" in text
    assert state.read(*state.content(0)) == b'{"x":1}'
    # the probe of layer_dir() decides the same way, leaving nothing behind
    monkeypatch.setattr(es, "_probe_dir", lambda p: "denied")
    assert es.layer_dir(doc, settings=beside_settings()) == es._edits_root() / layer_key(doc)
    store.close()
    infos = find_pending(doc)
    assert len(infos) == 1 and infos[0].dir == es._edits_root() / layer_key(doc)


def test_beside_name_taken_by_a_file_falls_back(make_doc, stores, lang_uz) -> None:
    doc = make_doc(jsongen.records(4, seed=2))
    bdir = es.beside_dir(doc)
    bdir.write_bytes(b"a file, not a folder")
    es.take_notices()
    assert es.layer_dir(doc, settings=beside_settings()) == es._edits_root() / layer_key(doc)
    store = stores(EditStore.open(doc, settings=beside_settings()))
    commit_set(store, store.load_state(), 1, b'{"y":2}')
    assert store.location == "appdata" and store.dir == es._edits_root() / layer_key(doc)
    assert bdir.read_bytes() == b"a file, not a folder"
    notices = es.take_notices()
    assert [k for k, _p in notices] == ["editstore.warn.beside_fallback"]
    assert "dastur papkasida" in i18n.t(notices[0][0], **notices[0][1])
    store.close()
    assert [i.dir for i in find_pending(doc)] == [es._edits_root() / layer_key(doc)]


def test_existing_layer_found_in_either_location(make_doc, stores) -> None:
    doc = make_doc(jsongen.records(4, seed=1))
    store = stores(EditStore.open(doc, settings=beside_settings()))
    state = commit_set(store, store.load_state(), 2, b'{"beside":1}')
    store.close()
    store = stores(EditStore.open(doc))                         # setting now "appdata"
    assert store.dir == es.beside_dir(doc) and store.location == "beside" and store.head == 1
    assert signature(store.load_state()) == signature(state)
    store.close()
    infos = find_pending(doc)
    assert [i.dir for i in infos] == [es.beside_dir(doc)] and infos[0].matches


def test_custom_dir_is_not_registered(make_doc, stores, tmp_path) -> None:
    doc = make_doc(jsongen.records(4, seed=1))
    d = tmp_path / "cli-layer"
    store = stores(EditStore.open(doc, dir=d))
    commit_set(store, store.load_state(), 0, b"{}")
    assert (d / DB_NAME).exists()
    assert all(e["dir"] != str(d) for e in es._index_load())
    store.close()
    assert EditStore.open(doc, create=False) is None            # not in the standard locations
    again = stores(EditStore.open(doc, dir=d, create=False))
    assert again is not None and again.head == 1


def test_create_false_read_only_and_errors(make_doc, stores) -> None:
    doc = make_doc(jsongen.records(4, seed=1))
    assert EditStore.open(doc, create=False) is None
    assert EditStore.open(doc, read_only=True) is None
    assert not LayerLock.is_locked(layer_key(doc))               # a None result released its lock
    store = stores(EditStore.open(doc))
    state = commit_set(store, store.load_state(), 0, b'{"a":1}')
    store.close()
    ro = stores(EditStore.open(doc, read_only=True))
    assert ro.read_only and signature(ro.load_state()) == signature(state)
    for call in (lambda: ro.append(b"x"), lambda: ro.commit(Step("edit", "k")), lambda: ro.set_head(0),
                 lambda: ro.new_segment(), lambda: ro.journal({"a": 1})):
        with pytest.raises(EditError) as ei:
            call()
        assert ei.value.key == "editstore.error.read_only"
    ro.close()
    with pytest.raises(EditError) as ei:
        ro.step(1)
    assert ei.value.key == "editstore.error.closed"


def test_index_must_be_complete(tmp_path) -> None:
    p = tmp_path / "a.json"
    jsongen.write_file(p, jsongen.records(5), "array")
    doc = Document.open(p)
    try:
        with pytest.raises(EditError) as ei:
            layer_key(doc)
        assert ei.value.key == "editstore.error.not_indexed"
    finally:
        doc.close()


def test_layer_lock_within_one_process(make_doc, monkeypatch) -> None:
    doc = make_doc(jsongen.records(3, seed=1))
    key = layer_key(doc)
    a = LayerLock(key)
    a.acquire()
    assert a.held and LayerLock.is_locked(key)
    b = LayerLock(key)
    assert not b.try_acquire()
    t0 = time.monotonic()
    with pytest.raises(EditError) as ei:
        b.acquire(timeout=0.3)
    assert ei.value.key == "edits.error.locked" and time.monotonic() - t0 >= 0.29
    # a second store on the same document waits LOCK_RETRY, then fails
    monkeypatch.setattr(es, "LOCK_RETRY", 0.2)
    with pytest.raises(EditError):
        EditStore.open(doc)
    # a key held by this process is waited for until it is released
    threading.Timer(0.15, a.release).start()
    t0 = time.monotonic()
    b.acquire(timeout=2.0)
    assert b.held and not a.held and 0.1 <= time.monotonic() - t0 < 1.5
    a.release()                      # idempotent, does not touch b
    assert b.held
    # a lost LayerLock object is released by the garbage collector
    del b, ei                        # (the traceback of the failed acquire referenced b)
    gc.collect()
    c = LayerLock(key)
    assert c.try_acquire()
    c.release()
    assert not LayerLock.is_locked(key)
    with pytest.raises(ValueError):
        LayerLock("not-a-key")
    with LayerLock(key) as d:
        assert d.held
    assert not d.held


def test_unusable_lock_folder_is_a_clear_error(make_doc, monkeypatch, _isolated_app_home) -> None:
    doc = make_doc(jsongen.records(3, seed=1))
    from jsonreshape import appdirs

    appdirs._reset_for_tests()
    locks = Path(_isolated_app_home) / "edits" / "locks"
    locks.parent.mkdir(parents=True, exist_ok=True)
    if locks.is_dir():
        locks.rmdir()
    locks.write_bytes(b"a file in the way")
    try:
        with pytest.raises(EditError) as ei:
            EditStore.open(doc)
        assert ei.value.key == "editstore.error.unavailable"
        with pytest.raises(EditError) as ei:
            find_pending(doc)
        assert ei.value.key == "editstore.error.unavailable"
    finally:
        locks.unlink()
        appdirs._reset_for_tests()


def test_stores_on_other_threads(make_doc, stores) -> None:
    """The store's connection is used from several threads (check_same_thread=False + the store lock)."""
    doc = make_doc(jsongen.records(10, seed=1))
    store = stores(EditStore.open(doc))
    seg = store.new_segment()
    errors: list[BaseException] = []

    def preview() -> None:
        try:
            for i in range(300):
                store.append(b'{"preview":%d}' % i, seg=seg)
                if i % 50 == 0:
                    store.sync([seg])
        except BaseException as exc:  # pragma: no cover - reported below
            errors.append(exc)

    th = threading.Thread(target=preview)
    th.start()
    state = store.load_state()
    for i in range(40):
        state = commit_set(store, state, i % 10, b'{"edit":%d}' % i)
    th.join()
    assert not errors
    store.sync([seg])
    assert store.published(seg) == sum(len(b'{"preview":%d}' % i) for i in range(300))
    store.close()
    store = stores(EditStore.open(doc))
    assert signature(store.load_state()) == signature(state)


# ----------------------------------------------------------------------------------
# damage
# ----------------------------------------------------------------------------------
def _layer_with_steps(make_doc, stores, n: int = 6):
    doc = make_doc(jsongen.records(8, seed=5))
    store = stores(EditStore.open(doc))
    state = store.load_state()
    states = [state]
    for i in range(n):
        state = commit_set(store, state, i % 8, b'{"step":%d,"pad":"%s"}' % (i, b"x" * 40))
        states.append(state)
    return doc, store, states


def test_truncated_segment_is_damaged_with_last_good(make_doc, stores) -> None:
    doc, store, states = _layer_with_steps(make_doc, stores)
    span4 = states[4].content(3)
    d = store.dir
    store.close()
    with open_shared(d / segment_name(0), "r+b") as f:
        f.truncate(span4[0] - EDIT_BASE + 3)           # step 4's unit is cut
    store = stores(EditStore.open(doc, read_only=True))
    with pytest.raises(LayerDamagedError) as ei:
        store.load_state()
    err = ei.value
    assert err.key == "editstore.error.damaged" and err.seq == 4 and err.last_good == 3
    assert signature(store.load_state(upto=err.last_good)) == signature(states[3])
    infos = find_pending(doc)
    store.close()
    infos = find_pending(doc)
    assert len(infos) == 1 and infos[0].damaged and segment_name(0) in infos[0].damaged


def test_corrupt_step_row_and_missing_row(make_doc, stores) -> None:
    doc, store, states = _layer_with_steps(make_doc, stores)
    d = store.dir
    store.close()
    import sqlite3

    con = sqlite3.connect(d / DB_NAME, isolation_level=None)
    con.execute("UPDATE steps SET data = '{not json' WHERE seq = 5")
    con.close()
    store = stores(EditStore.open(doc))
    with pytest.raises(LayerDamagedError) as ei:
        store.load_state()
    assert (ei.value.seq, ei.value.last_good) == (5, 4)
    with pytest.raises(LayerDamagedError):
        store.step(5)
    store.close()
    con = sqlite3.connect(d / DB_NAME, isolation_level=None)
    con.execute("DELETE FROM steps WHERE seq = 3")
    con.close()
    infos = find_pending(doc)
    assert infos[0].damaged == i18n.t("editstore.damaged.steps")
    store = stores(EditStore.open(doc))
    with pytest.raises(LayerDamagedError) as ei:
        store.load_state()
    assert (ei.value.seq, ei.value.last_good) == (3, 2)


def test_missing_array_layer_file_is_damaged(make_doc, stores) -> None:
    doc = make_doc(jsongen.records(6, seed=1))
    store = stores(EditStore.open(doc))
    state = commit_set(store, store.load_state(), 0, b"{}")
    seg = store.new_segment()
    span = store.append(b'{"z":1}', seg=seg)
    store.sync([seg])
    lp = store.new_layer_path()
    write_array_layer(lp, [(1, *span)], flags=0, segments=[seg], work_dir=None)
    step = Step("bulk", "k", push_layer=lp.name, segments=[seg])
    store.commit(step, state=state)
    d = store.dir
    store.close()
    os.remove(d / lp.name)
    infos = find_pending(doc)
    assert infos[0].damaged and lp.name in infos[0].damaged
    store = stores(EditStore.open(doc, read_only=True))
    with pytest.raises(LayerDamagedError) as ei:
        store.load_state()
    assert (ei.value.seq, ei.value.last_good) == (2, 1)
    assert store.load_state(upto=1).count == 6


def test_unreadable_database_and_newer_format(make_doc, stores, lang_en) -> None:
    doc, store, _states = _layer_with_steps(make_doc, stores, n=2)
    d = store.dir
    store.close()
    import sqlite3

    con = sqlite3.connect(d / DB_NAME, isolation_level=None)
    con.execute("UPDATE meta SET value = '99' WHERE key = 'format_version'")
    con.close()
    with pytest.raises(EditError) as ei:
        EditStore.open(doc)
    assert ei.value.key == "editstore.error.format" and "99" in str(ei.value)
    infos = find_pending(doc)
    assert infos[0].damaged and "newer version" in infos[0].damaged
    for name in os.listdir(d):
        if name.startswith(DB_NAME):
            os.remove(d / name)
    (d / DB_NAME).write_bytes(b"this is not a database" * 100)
    infos = find_pending(doc)
    assert len(infos) == 1 and infos[0].damaged and infos[0].matches and not infos[0].locked
    with pytest.raises(LayerDamagedError):
        EditStore.open(doc)
    assert es.delete_layer(d)
    assert not d.exists() and find_pending(doc) == []
    stores(EditStore.open(doc))          # editing works again


# ----------------------------------------------------------------------------------
# find_pending
# ----------------------------------------------------------------------------------
def test_find_pending_matching_and_journal(make_doc, stores, lang_en) -> None:
    doc = make_doc(jsongen.records(5, seed=1))
    assert find_pending(doc) == []
    store = stores(EditStore.open(doc))
    state = commit_set(store, store.load_state(), 0, b"{}")
    state = commit_set(store, state, 1, b"[]")
    store.journal({"source": str(doc.source_path), "temp": "t.jrsave", "n": [1, 2]})
    assert store.saving == {"source": str(doc.source_path), "temp": "t.jrsave", "n": [1, 2]}
    store.close()
    infos = find_pending(doc)
    assert len(infos) == 1
    info = infos[0]
    assert isinstance(info, PendingInfo) and info.matches and not info.locked and info.damaged is None
    assert info.steps == 2 and info.key == layer_key(doc) and info.label == "Value changed"
    assert info.saving == {"source": str(doc.source_path), "temp": "t.jrsave", "n": [1, 2]}
    assert info.source == str(doc.source_path) and info.created <= info.updated
    # a layer with a journal is reported even at head 0
    store = stores(EditStore.open(doc))
    store.set_head(0)
    store.close()
    assert [i.steps for i in find_pending(doc)] == [0]
    store = stores(EditStore.open(doc))
    store.journal(None)
    store.close()                                   # head 0, no journal: deleted
    assert find_pending(doc) == [] and not (es._edits_root() / layer_key(doc)).exists()


def test_find_pending_stale_layers_of_older_versions(make_doc, stores, tmp_path) -> None:
    recs = jsongen.records(6, seed=1)
    doc = make_doc(recs, name="data.json")
    store = stores(EditStore.open(doc))
    commit_set(store, store.load_state(), 0, b'{"v1":1}')
    old_key = store.key
    store.close()
    doc.close()
    jsongen.write_file(Path(doc.source_path), recs + [{"new": 1}], "array")   # the file changed
    doc2 = make_doc(Path(doc.source_path).read_bytes(), name="data.json")
    assert layer_key(doc2) != old_key
    infos = find_pending(doc2)
    assert len(infos) == 1 and not infos[0].matches and infos[0].key == old_key and infos[0].steps == 1
    # a new layer of the new version goes next to it; the stale one stays listed (matching first)
    store = stores(EditStore.open(doc2))
    commit_set(store, store.load_state(), 0, b'{"v2":1}')
    store.close()
    infos = find_pending(doc2)
    assert [i.matches for i in infos] == [True, False]
    # export of the stale layer: read-only, allow_stale, by folder
    ro = stores(EditStore.open(doc2, dir=infos[1].dir, read_only=True, allow_stale=True))
    assert ro.stale and ro.read_only and ro.key == old_key and ro.base_count == 6
    st = ro.load_state()
    assert st.read(*st.content(0)) == b'{"v1":1}'
    ro.discard()                                    # delete after export (allowed read-only)
    assert [i.matches for i in find_pending(doc2)] == [True]
    assert not infos[1].dir.exists()


def test_stale_layer_beside_blocks_a_new_one(make_doc, stores) -> None:
    recs = jsongen.records(5, seed=2)
    doc = make_doc(recs, name="b.json")
    store = stores(EditStore.open(doc, settings=beside_settings()))
    commit_set(store, store.load_state(), 0, b'{"old":1}')
    store.close()
    doc.close()
    jsongen.write_file(Path(doc.source_path), recs[:-1], "array")
    doc2 = make_doc(Path(doc.source_path).read_bytes(), name="b.json")
    with pytest.raises(EditError) as ei:
        EditStore.open(doc2, settings=beside_settings())
    assert ei.value.key == "editstore.error.stale" and str(es.beside_dir(doc2)) in str(ei.value)
    other = EditStore.open(doc2)                    # the app folder is free
    assert other is not None and other.dir == es._edits_root() / layer_key(doc2)
    other.close()
    infos = find_pending(doc2)
    assert len(infos) == 1 and not infos[0].matches and infos[0].dir == es.beside_dir(doc2)
    ro = stores(EditStore.open(doc2, settings=beside_settings(), read_only=True, allow_stale=True))
    assert ro.stale and ro.read_only and ro.location == "beside"
    ro.discard()
    assert not es.beside_dir(doc2).exists()
    store = stores(EditStore.open(doc2, settings=beside_settings()))
    commit_set(store, store.load_state(), 0, b'{"new":1}')
    assert store.dir == es.beside_dir(doc2)


def test_find_pending_locked_layers(make_doc, stores, lang_en) -> None:
    doc = make_doc(jsongen.records(5, seed=1))
    store = stores(EditStore.open(doc))
    state = commit_set(store, store.load_state(), 0, b"{}")
    store.close()                                    # index.json now knows the step count and label
    store = stores(EditStore.open(doc))              # held by "another tab" of this process
    infos = find_pending(doc)
    assert len(infos) == 1 and infos[0].locked and infos[0].matches and infos[0].steps == 1
    assert infos[0].label == "Value changed" and infos[0].damaged is None
    assert store.lock.held                           # the probe did not disturb the holder
    commit_set(store, store.load_state(), 1, b"{}")
    # the holder itself may look (it passes its lock; before it opened its store)
    store.close()
    lock = LayerLock.for_doc(doc)
    lock.acquire()
    try:
        mine = find_pending(doc, lock=lock)
        assert len(mine) == 1 and not mine[0].locked and mine[0].steps == 2
        again = stores(EditStore.open(doc, lock=lock))
        assert again.head == 2 and again.lock is lock
        again.close()
        assert lock.held                             # a lock given by the caller stays held
    finally:
        lock.release()
    del state


def test_find_pending_purges_tombstoned_and_head0_layers(make_doc, stores) -> None:
    doc = make_doc(jsongen.records(5, seed=1))
    store = stores(EditStore.open(doc))
    commit_set(store, store.load_state(), 0, b"{}")
    d = store.dir
    store.mark_discarded()
    assert store.head == 1
    store.close()                                   # tombstoned, files kept (as after a crash)
    assert (d / DB_NAME).exists()
    assert find_pending(doc) == [] and not d.exists()
    # head 0 left behind by a crash (no close)
    store = stores(EditStore.open(doc))
    state = commit_set(store, store.load_state(), 0, b"{}")
    store.set_head(0)
    simulate_crash(store)
    assert d.exists()
    assert find_pending(doc) == [] and not d.exists()
    del state
    # a folder without database (an interrupted deletion) is purged too
    d.mkdir(parents=True)
    (d / segment_name(0)).write_bytes(b"left")
    assert find_pending(doc) == [] and not d.exists()


def test_find_pending_both_locations_and_index_cleanup(make_doc, stores) -> None:
    doc = make_doc(jsongen.records(5, seed=1))
    a = stores(EditStore.open(doc, settings=beside_settings()))
    commit_set(a, a.load_state(), 0, b'{"beside":1}')
    a.close()
    infos = find_pending(doc, settings=Settings())
    assert [i.dir for i in infos] == [es.beside_dir(doc)]
    # an index entry whose folder vanished is dropped
    import shutil

    shutil.rmtree(es.beside_dir(doc))
    assert any(e["key"] == layer_key(doc) for e in es._index_load())
    assert find_pending(doc) == []
    assert not any(e["key"] == layer_key(doc) for e in es._index_load())


def test_discard_while_readers_are_open(make_doc, stores) -> None:
    doc = make_doc(jsongen.records(8, seed=4))
    store = stores(EditStore.open(doc))
    state = commit_set(store, store.load_state(), 0, b'{"point":1}')
    seg = store.new_segment()
    entries = [(r, *store.append(b'{"bulk":%d}' % r, seg=seg)) for r in range(8)]
    store.sync([seg])
    lp = store.new_layer_path()
    write_array_layer(lp, entries, flags=0, segments=[seg], work_dir=None)
    step = Step("bulk", "edits.step.bulk.replace", {"count": 8}, push_layer=lp.name, segments=[seg])
    store.commit(step, state=state)
    state = apply_step(state, step, layers=store)
    base = base_records(doc)
    raw = Path(doc.data_path).read_bytes()
    before = effective(state, base, raw)            # opens the reader's handles
    other = DatReader(store.dir)                    # another reader of the same layer
    other.publish(seg, store.published(seg))
    assert other.read(*entries[1][1:]) == b'{"bulk":1}'
    layer = state.layers[-2]
    d = store.dir
    store.discard()
    assert store.closed and not store.lock.held
    assert not (d / DB_NAME).exists() and not (d / lp.name).exists() and not (d / segment_name(seg)).exists()
    assert effective(state, base, raw) == before    # open handles / mappings keep working
    assert layer.get(3) == entries[3][1:]
    assert other.read(*entries[2][1:]) == b'{"bulk":2}'
    other.close()
    assert find_pending(doc) == []
    fresh = stores(EditStore.open(doc))             # a new layer can be made right away
    commit_set(fresh, fresh.load_state(), 1, b'{"again":1}')
    assert fresh.head == 1 and fresh.dir == d
    layer.close()
    state.reader.close()


def test_close_deletes_unapplied_previews_and_head0_layers(make_doc, stores) -> None:
    doc = make_doc(jsongen.records(5, seed=1))
    store = stores(EditStore.open(doc))
    state = commit_set(store, store.load_state(), 0, b"{}")
    seg = store.new_segment()
    store.append(b"preview", seg=seg)
    lp = store.new_layer_path()
    write_array_layer(lp, [], flags=0, segments=[seg], work_dir=None)
    d = store.dir
    store.close()
    assert set(os.listdir(d)) <= {DB_NAME, DB_NAME + "-wal", segment_name(0)}
    store = stores(EditStore.open(doc))
    store.set_head(0)
    store.close()
    assert not d.exists()
    store = stores(EditStore.open(doc))       # nothing written: nothing created, nothing to delete
    store.close()
    assert not d.exists()
    del state


def test_big_steps_round_trip_through_the_store(make_doc, stores) -> None:
    doc = make_doc(b"[" + b",".join(b"%d" % i for i in range(20_000)) + b"]")
    store = stores(EditStore.open(doc))
    state = store.load_state()
    rng = random.Random(9)
    positions = array("Q", sorted(rng.sample(range(20_000), 6_000)))
    content = [(r, None, store.append(b'{"r":%d}' % r)) for r in range(0, 12_000, 2)]
    step = Step("patch", "edits.step.patch", {"count": 2}, content=content, struct=[("dels", positions)],
                records=list(range(5_000)), keys=[(r, "k%d" % r, None) for r in range(5_000)],
                nodes=[NodeChange(0, (), Remap([("dels", array("Q", range(5_000)))]), 1)])
    seq = store.commit(step, state=state)
    assert step.old_rids is not None and sum(n for _p, _r, n in step.old_rids) == 6_000
    after = apply_step(state, step, layers=store)
    store.close()
    store = stores(EditStore.open(doc))
    assert store.step(seq) == step
    loaded = store.load_state()
    assert signature(loaded) == signature(after)
    back = undo_step(loaded, store.step(seq), layers=store)
    assert back.pieces == PieceTable.identity(20_000)


def test_open_purges_a_tombstoned_layer_and_starts_fresh(make_doc, stores) -> None:
    doc = make_doc(jsongen.records(5, seed=6))
    store = stores(EditStore.open(doc))
    commit_set(store, store.load_state(), 0, b'{"old":1}')
    d = store.dir
    store.mark_discarded()
    with pytest.raises(EditError) as ei:
        store.append(b"x")
    assert ei.value.key == "editstore.error.discarded"
    store.close()
    assert (d / DB_NAME).exists()
    store = stores(EditStore.open(doc))
    assert not store.created and store.head == 0 and not (d / DB_NAME).exists()
    state = commit_set(store, store.load_state(), 1, b'{"new":1}')
    assert store.head == 1 and state.content(0) is None
    with pytest.raises(ValueError):
        store.set_head(2)
    with pytest.raises(ValueError):
        store.step(2)
    with pytest.raises(ValueError):
        store.step(0)


def test_steps_history_and_labels(make_doc, stores, lang_uz) -> None:
    doc = make_doc(jsongen.records(5, seed=1))
    store = stores(EditStore.open(doc))
    state = store.load_state()
    state = commit_set(store, state, 0, b"{}")
    span = store.append(b"{}")
    step = Step("edit", "edits.step.delete_records", {"count": 2}, struct=[("dels", [1, 2])])
    store.commit(step, state=state)
    hist = store.steps()
    assert [(h.seq, h.kind, h.label) for h in hist] == [(1, "edit", "Qiymat o'zgartirildi"),
                                                        (2, "edit", "2 ta yozuv o'chirildi")]
    assert all(isinstance(h, es.StepInfo) and h.ts > 0 for h in hist)
    store.set_head(1)
    assert [h.seq for h in store.steps()] == [1, 2] and store.can_redo()
    assert store.step(2) == step
    del span


# ----------------------------------------------------------------------------------
# every document kind, both core backends
# ----------------------------------------------------------------------------------
@pytest.fixture
def backend(core_backend):
    name = "rust" if core_backend.__name__.endswith("_rscore") else "python"
    old = reader.set_scanner_backend(name)
    yield name
    reader.set_scanner_backend(old)


KIND_CASES = [
    ("array", {}, {}),
    ("jsonl", {}, {}),
    ("brace_list", {}, {}),
    ("wrapped", {}, {}),
    ("keyed", {}, {}),
    ("single", {}, {"format": "single"}),
    ("stream", {"style": "pretty"}, {"format": "stream"}),
    ("array", {"encoding": "utf-16-le", "bom": True}, {}),
    ("jsonl", {"gz": True}, {}),
    ("array", {"bom": True, "style": "pretty", "newline": "\r\n"}, {}),
    ("keyed", {"encoding": "utf-16-be", "style": "pretty"}, {}),
]


@pytest.mark.parametrize("fmt,write_kw,open_kw", KIND_CASES)
def test_every_kind_and_encoding(make_doc, stores, backend, fmt, write_kw, open_kw, tmp_path) -> None:
    recs = [{"id": i, "ism": "Oʻzbekiston" if i % 2 else "Тошкент", "n": jsongen.number_from_token("1.10")}
            for i in range(9)]
    name = "doc.json.gz" if write_kw.get("gz") else ("doc.jsonl" if fmt == "jsonl" else "doc.json")
    doc = make_doc(recs, fmt, name=name, folder=tmp_path / fmt / "ünï code dir", **write_kw, **open_kw)
    assert doc.info.kind == fmt or (fmt == "single" and doc.info.kind == "single")
    key = layer_key(doc)
    assert key == layer_key(doc) and len(key) == 32
    store = stores(EditStore.open(doc, settings=beside_settings()))
    assert store.dir == Path(doc.source_path).parent / (Path(doc.source_path).name + ".jredit")
    drv = Driver(doc, store, seed=sum(map(ord, fmt)), kinds=("set", "insert", "delete", "move", "dup", "bulk")
                 if doc.record_count > 1 else ("set", "bulk"), undo=0.1, redo=0.1)
    for _ in range(25):
        drv.action()
    mem = drv.state
    store.close()
    store = stores(EditStore.open(doc))
    loaded = store.load_state()
    assert signature(loaded) == signature(mem)
    assert model_of(loaded, drv.base, drv.raw) == drv.model
    infos = find_pending(doc)
    assert len(infos) == 1 and infos[0].matches


def test_long_paths(make_doc, stores, tmp_path) -> None:
    deep = tmp_path
    for i in range(6):
        deep = deep / (f"{i}-" + "papka" * 9)
    doc = make_doc(jsongen.records(4, seed=1), folder=deep, name="juda uzun nomli fayl " + "x" * 40 + ".json")
    assert len(str(es.beside_dir(doc))) > 300
    store = stores(EditStore.open(doc, settings=beside_settings()))
    state = commit_set(store, store.load_state(), 0, b'{"long":1}')
    store.close()
    infos = find_pending(doc)
    assert infos and infos[0].dir == es.beside_dir(doc)
    store = stores(EditStore.open(doc))
    assert signature(store.load_state()) == signature(state)
    store.discard()
    assert find_pending(doc) == []
