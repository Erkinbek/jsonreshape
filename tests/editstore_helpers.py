"""Shared helpers of the editstore tests (tests/test_editstore*.py).

* :func:`effective` -- the effective record bytes of an OverlayState over a plain Document (the oracle:
  base records are read from the document, edited ones through the state's reader / the data file);
* :func:`signature` -- a comparable description of a state (piece table, layers, wrapper, flags, ...);
* :class:`Driver` -- deterministic random edits on an EditStore (point edits, inserts, deletes, moves,
  duplicates, bulk array layers, wrapper edits, multi-op and "pieces" steps, undo/redo, trimming) with an
  exact model of the effective records per seq. The same seed gives the same actions in any process, so
  the crash tests replay a killed subprocess's run in the parent to know every state it passed through.
"""

from __future__ import annotations

import random
from array import array
from pathlib import Path
from typing import Any

from jsonreshape import editstore as es
from jsonreshape.overlay import EDIT_BASE, ArrayLayer, DictLayer, NodeChange, OverlayState, PieceTable, Remap, \
    write_array_layer

Model = tuple[tuple[bytes, ...], bytes | None, int]     # (records, wrapper text, flags)

KINDS = ("set", "insert", "delete", "move", "dup", "bulk", "wrapper", "multi", "pieces", "flags")


def base_records(doc: Any) -> list[bytes]:
    return [doc.record_bytes(i) for i in range(doc.record_count)]


def effective(state: OverlayState, base: list[bytes], raw: bytes) -> list[bytes]:
    """Effective record bytes of ``state`` (``base``: the plain document's records, ``raw``: its data file)."""
    out = []
    for pos in range(state.count):
        rid = state.rid_at(pos)
        span = state.content(rid)
        if span is None:
            assert rid < state.base_count, f"rid {rid} has no content"
            out.append(base[rid])
        elif span[0] >= EDIT_BASE:
            out.append(state.read(*span))
        else:
            out.append(raw[span[0]:span[1]])
    return out


def wrapper_text(state: OverlayState) -> bytes | None:
    return None if state.wrapper is None else state.read(*state.wrapper)


def model_of(state: OverlayState, base: list[bytes], raw: bytes) -> Model:
    return tuple(effective(state, base, raw)), wrapper_text(state), state.flags


def signature(state: OverlayState) -> tuple:
    layers = []
    for layer in state.layers:
        if isinstance(layer, ArrayLayer):
            layers.append(("array", layer.name, tuple(layer.items())))
        else:
            layers.append(("dict", tuple(layer.items())))
    return (state.base_count, state.pieces.to_bytes(), tuple(layers), state.wrapper, state.wrapper_hole, state.flags,
            state.next_rid, state.version)


def base_span(doc: Any, rid: int) -> tuple[int, int]:
    return doc.record_span(rid)


class Driver:
    """Random, reproducible edits on ``store`` (see the module docstring)."""

    def __init__(self, doc: Any, store: es.EditStore, seed: int, *, limit: int | None = None,
                 kinds: tuple[str, ...] = KINDS, undo: float = 0.12, redo: float = 0.08, check_undo: bool = False,
                 max_records: int = 60) -> None:
        self.doc = doc
        self.store = store
        self.raw = Path(doc.data_path).read_bytes()
        self.base = base_records(doc)
        self.rng = random.Random(seed)
        self.state = store.load_state()
        self.models: dict[int, Model] = {store.head: model_of(self.state, self.base, self.raw)}
        self.limit = limit
        self.kinds = kinds
        self.p_undo = undo
        self.p_redo = redo
        self.check_undo = check_undo
        self.max_records = max_records
        self.n = 0
        self.log: list[str] = []

    # -- model --------------------------------------------------------------------------------
    @property
    def model(self) -> Model:
        return self.models[self.store.head]

    def current(self) -> Model:
        return model_of(self.state, self.base, self.raw)

    # -- actions --------------------------------------------------------------------------------
    def action(self) -> str:
        st = self.store
        r = self.rng.random()
        if r < self.p_undo and st.head > st.checkpoint_seq:
            step = st.step(st.head)
            st.set_head(st.head - 1)
            self.state = es.undo_step(self.state, step, layers=st)
            self.log.append("undo")
            return "undo"
        if r < self.p_undo + self.p_redo and st.can_redo():
            step = st.step(st.head + 1)
            st.set_head(st.head + 1)
            self.state = es.apply_step(self.state, step, layers=st)
            self.log.append("redo")
            return "redo"
        count = self.state.count
        kinds = [k for k in self.kinds if self._possible(k, count)] or ["insert"]
        kind = self.rng.choice(kinds)
        self.n += 1
        step, model = getattr(self, "_make_" + kind)()
        before = self.state
        seq = st.commit(step, state=self.state)
        self.state = es.apply_step(self.state, step, layers=st)
        if self.check_undo:
            back = es.undo_step(self.state, step, layers=st)
            assert signature(back)[:6] == signature(before)[:6], kind
            assert back.next_rid == self.state.next_rid
        for s in [s for s in self.models if s >= seq]:
            del self.models[s]
        self.models[seq] = model
        if self.limit is not None:
            st.maybe_trim(self.limit)
        self.log.append(kind)
        return kind

    def _possible(self, kind: str, count: int) -> bool:
        if kind in ("set", "delete", "bulk", "multi"):
            return count > 0
        if kind == "move":
            return count > 1
        if kind in ("insert", "dup", "pieces"):
            return count < self.max_records and (kind == "insert" or count > 0)
        return True    # wrapper, flags

    def _unit(self, tag: str) -> bytes:
        return b'{"%s":%d,"t":"O\'zbekiston \xd0\xa2\xd0\xbe\xd1\x88\xd0\xba\xd0\xb5\xd0\xbd\xd1\x82"}' % (
            tag.encode(), self.n)

    def _flags(self) -> tuple[int, int]:
        old = self.state.flags
        return old, (old | (1 << self.rng.randrange(3)) if self.rng.random() < 0.3 else old)

    def _extras(self, step_kw: dict[str, Any], count_after: int) -> None:
        """Random ChangeSet data (round-tripped by the store; unused by the state)."""
        rng = self.rng
        if rng.random() < 0.3 and count_after:
            step_kw["records"] = sorted(rng.sample(range(count_after), min(count_after, rng.randint(1, 3))))
        if rng.random() < 0.2 and count_after:
            step_kw["select"] = (rng.randrange(count_after), ("a", 1, "kalit bo'shliq"), (0, 1))
        if rng.random() < 0.2:
            step_kw["nodes"] = [NodeChange(0 if count_after else None, (1, 0), Remap([("ins", 0, 1)]), 2)]
        if rng.random() < 0.2:
            step_kw["keys"] = [(0, "id\ud800", "Тошкент")]

    def _set_model(self, records: list[bytes], wrapper: bytes | None = None, flags: int | None = None,
                   keep_wrapper: bool = True) -> Model:
        cur = self.model
        return (tuple(records), cur[1] if keep_wrapper and wrapper is None else wrapper,
                cur[2] if flags is None else flags)

    def _make_set(self) -> tuple[es.Step, Model]:
        st, state = self.store, self.state
        k = min(state.count, self.rng.randint(1, 3))
        positions = sorted(self.rng.sample(range(state.count), k))
        records = list(self.model[0])
        content = []
        for pos in positions:
            rid = state.rid_at(pos)
            data = self._unit(f"set{pos}")
            span = st.append(data)
            content.append((rid, state.top.get(rid), span))
            records[pos] = data
        flags = self._flags()
        kw: dict[str, Any] = {}
        self._extras(kw, state.count)
        step = es.Step("edit", "edits.step.set_value", {}, content=content, flags=flags, **kw)
        return step, self._set_model(records, flags=flags[1])

    def _make_insert(self) -> tuple[es.Step, Model]:
        st, state = self.store, self.state
        pos = self.rng.randrange(state.count + 1)
        k = self.rng.randint(1, 3)
        rid0 = state.next_rid
        records = list(self.model[0])
        content = []
        for i in range(k):
            data = self._unit(f"ins{i}")
            content.append((rid0 + i, None, st.append(data)))
            records.insert(pos + i, data)
        step = es.Step("edit", "edits.step.insert_record", {"count": k}, content=content, struct=[("ins", pos, k)],
                       new_rids=[(pos, rid0, k)])
        return step, self._set_model(records)

    def _make_delete(self) -> tuple[es.Step, Model]:
        state = self.state
        k = self.rng.randint(1, min(4, state.count))
        positions = sorted(self.rng.sample(range(state.count), k))
        records = [r for i, r in enumerate(self.model[0]) if i not in set(positions)]
        if self.rng.random() < 0.5 and positions[-1] - positions[0] + 1 == k:
            struct = [("del", positions[0], k)]
        else:
            struct = [("dels", array("Q", positions))]
        step = es.Step("edit", "edits.step.delete_records", {"count": k}, struct=struct)
        return step, self._set_model(records)

    def _make_move(self) -> tuple[es.Step, Model]:
        state = self.state
        k = self.rng.randint(1, min(3, state.count - 1))
        positions = sorted(self.rng.sample(range(state.count), k))
        to = self.rng.randrange(state.count - k + 1)
        recs = list(self.model[0])
        moved = [recs[p] for p in positions]
        rest = [r for i, r in enumerate(recs) if i not in set(positions)]
        records = rest[:to] + moved + rest[to:]
        step = es.Step("edit", "edits.step.move_records", {"count": k}, struct=[("move", positions, to)])
        return step, self._set_model(records)

    def _make_dup(self) -> tuple[es.Step, Model]:
        st, state = self.store, self.state
        pos = self.rng.randrange(state.count)
        rid = state.rid_at(pos)
        span = state.content(rid)
        if span is None:
            span = base_span(self.doc, rid)
        new = state.next_rid
        records = list(self.model[0])
        records.insert(pos + 1, records[pos])
        step = es.Step("edit", "edits.step.duplicate_records", {"count": 1}, content=[(new, None, span)],
                       struct=[("ins", pos + 1, 1)], new_rids=[(pos + 1, new, 1)])
        return step, self._set_model(records)

    def _make_bulk(self) -> tuple[es.Step, Model]:
        st, state = self.store, self.state
        seg = st.new_segment()
        every = self.rng.randint(1, 3)
        records = list(self.model[0])
        entries = []
        for pos in range(0, state.count, every):
            data = self._unit(f"bulk{pos}")
            entries.append((state.rid_at(pos), *st.append(data, seg=seg)))
            records[pos] = data
        st.sync([seg])
        path = st.new_layer_path()
        self.rng.shuffle(entries)
        write_array_layer(path, entries, flags=0, segments=[seg], work_dir=None)
        step = es.Step("bulk", "edits.step.bulk.replace", {"count": len(entries)}, push_layer=path.name,
                       segments=[seg], flags=self._flags())
        return step, self._set_model(records, flags=step.flags[1])

    def _make_wrapper(self) -> tuple[es.Step, Model]:
        st, state = self.store, self.state
        data = b'{"meta":%d,"data":[]}' % self.n
        span = st.append(data)
        start = span[0] + data.index(b"[")
        hole = (start, start + 2)
        step = es.Step("edit", "edits.step.set_value", {}, wrapper=(state.wrapper, span, state.wrapper_hole, hole))
        return step, self._set_model(list(self.model[0]), wrapper=data, keep_wrapper=False)

    def _make_multi(self) -> tuple[es.Step, Model]:
        st, state = self.store, self.state
        recs = list(self.model[0])
        k = self.rng.randint(1, min(3, state.count))
        positions = sorted(self.rng.sample(range(state.count), k))
        rest = [r for i, r in enumerate(recs) if i not in set(positions)]
        pos = self.rng.randrange(len(rest) + 1)
        n = self.rng.randint(1, 2)
        rid0 = state.next_rid
        content = []
        for i in range(n):
            data = self._unit(f"multi{i}")
            content.append((rid0 + i, None, st.append(data)))
            rest.insert(pos + i, data)
        step = es.Step("edit", "edits.step.paste", {}, content=content,
                       struct=[("dels", positions), ("ins", pos, n)], new_rids=[(pos, rid0, n)])
        return step, self._set_model(rest)

    def _make_pieces(self) -> tuple[es.Step, Model]:
        st, state = self.store, self.state
        rids = list(state.rids(0, state.count))
        recs = list(self.model[0])
        order = list(range(len(rids)))
        a = self.rng.randrange(len(order))
        b = self.rng.randrange(a, len(order)) + 1
        order[a:b] = reversed(order[a:b])
        new_rid = state.next_rid
        data = self._unit("pieces")
        span = st.append(data)
        at = self.rng.randrange(len(order) + 1)
        new_rids_list = [rids[i] for i in order]
        new_recs = [recs[i] for i in order]
        new_rids_list.insert(at, new_rid)
        new_recs.insert(at, data)
        new_table = PieceTable.from_pairs(new_rids_list, [r + 1 for r in new_rids_list])
        step = es.Step("patch", "edits.step.patch", {"count": 2}, content=[(new_rid, None, span)],
                       struct=[("pieces", state.pieces, new_table)])
        return step, self._set_model(new_recs)

    def _make_flags(self) -> tuple[es.Step, Model]:
        old = self.state.flags
        new = old ^ (1 << self.rng.randrange(3))
        step = es.Step("edit", "edits.step.change_type", {"count": 0}, flags=(old, new))
        return step, self._set_model(list(self.model[0]), flags=new)


def empty_dict_layer() -> DictLayer:
    return DictLayer()
