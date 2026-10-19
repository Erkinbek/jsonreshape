"""Random command sequences on EditSession (hypothesis stateful testing, §12.5).

Every kind of document (unique-leaf records, compact/pretty), random commands -- values, paths, keys, inserts and
pastes (nodes and records), deletes, duplicates, moves within a container / between containers / between records /
to and from the record level / into and out of the wrapper, move_by, type changes, delete_records /
duplicate_records with unsorted input -- interleaved with undo and redo. After every action: the parsed effective
document equals the independent model (``edits_helpers.Model``; a refused command must raise the error the model
predicts and change nothing), the ChangeSet is consistent with the documents before/after
(``edits_helpers.check_changeset``: records, NodeChange counts, the selection, refs remapped like a holder point at
the same unique leaves), and undo/redo reproduce the exact effective bytes recorded for every head.

The default run is derandomized (the same 60 + 20 sequences every time, ~20 s). For a longer exploration:
``JR_EDIT_RANDOM=1 JR_EDIT_EXAMPLES=2000 pytest --timeout=3400 tests/test_edits_random.py`` (new random sequences;
2 000 + 600 such sequences and 300 with the lowered limits ran clean during development).
"""

from __future__ import annotations

import os
import random
import uuid
from pathlib import Path
from typing import Any, Callable

import pytest
from hypothesis import HealthCheck, settings
from hypothesis import strategies as st
from hypothesis.stateful import RuleBasedStateMachine, initialize, invariant, precondition, rule, run_state_machine_as_test

from edits_helpers import (
    KINDS,
    Model,
    ModelError,
    O,
    _Counter,
    check_changeset,
    count,
    dump,
    effective,
    node_at,
    open_doc,
    unique_value,
)
from jsonreshape import edits
from jsonreshape.edits import EditSession, Ref
from jsonreshape.errors import ConversionError, EditError

TYPES = ("object", "array", "string", "number", "bool", "null")
_HEALTH = [HealthCheck.too_slow, HealthCheck.filter_too_much, HealthCheck.data_too_large]


class EditMachine(RuleBasedStateMachine):
    """One document + session; the model; the effective bytes and model of every head."""

    def __init__(self) -> None:
        super().__init__()
        self.doc: Any = None
        self.s: EditSession | None = None
        self.counter = _Counter()
        self.counter.n = 100_000

    @initialize(kind=st.sampled_from(KINDS), style=st.sampled_from(["compact", "pretty2"]),
                seed=st.integers(0, 10 ** 6), n=st.integers(1, 6))
    def setup(self, kind: str, style: str, seed: int, n: int) -> None:
        home = Path(os.environ["JSONRESHAPE_HOME"]) / "machines" / uuid.uuid4().hex
        home.mkdir(parents=True)
        self.doc = open_doc(home / "src.json", kind, n=n, seed=seed, style=style)
        self.s = EditSession.open(self.doc)
        self.model = Model.of(self.doc)
        self.history: list[tuple[Model, tuple]] = [(self.model, effective(self.doc))]
        self.rng = random.Random(seed)

    def teardown(self) -> None:
        if self.s is not None:
            self.s.close()
        if self.doc is not None:
            self.doc.close()

    # -- helpers ----------------------------------------------------------------------------------
    def value(self) -> Any:
        return unique_value(self.rng, self.counter)

    def apply(self, fn: Callable[[EditSession], Any], model_fn: Callable[[Model], Any], targets: tuple = ()) -> None:
        assert self.s is not None
        before_doc = effective(self.doc)
        before = self.model
        version = self.s.state.version
        expected = before.copy()
        err = None
        try:
            model_fn(expected)
        except ModelError as exc:
            err = exc.key
        except ConversionError:
            err = "edits.error.convert"
        if err is not None:
            with pytest.raises(EditError) as exc:
                fn(self.s)
            assert exc.value.key == err, (exc.value.key, err)
            assert effective(self.doc) == before_doc and self.s.state.version == version
            return
        try:
            cs = fn(self.s)
        except EditError as exc:
            # with the item limit lowered (big-path test) a command may legitimately need too many units
            if exc.key != "edits.error.too_many" or edits.MAX_ITEMS >= 100:
                raise
            assert effective(self.doc) == before_doc and self.s.state.version == version
            return
        got = Model.of(self.doc)
        assert got == expected, f"\nexpected {expected}\n     got {got}"
        after_doc = effective(self.doc)
        check_changeset(cs, before_doc, before, got, after_doc, targets=targets)
        self.model = got
        if self.s.state.version != version:
            head = self.s.state.version
            del self.history[head:]
            self.history.append((got, after_doc))
            assert len(self.history) == head + 1
        else:
            assert after_doc == before_doc

    def pick(self, data: Any, refs: list[Ref], k: int = 1) -> list[Ref]:
        k = min(k, edits.MAX_ITEMS)
        if not refs or k < 1:
            return []
        n = data.draw(st.integers(1, min(k, len(refs))))
        idx = data.draw(st.lists(st.integers(0, len(refs) - 1), min_size=n, max_size=n, unique=True))
        return [refs[i] for i in idx]

    def record_refs(self) -> list[Ref]:
        if self.model.kind == "single":
            return []
        return [Ref(i, (), ()) for i in range(len(self.model.records))]

    def containers(self) -> list[Ref]:
        """Insert/move targets: every container (the wrapper root too: an ancestor OBJECT takes members)."""
        refs = self.model.refs(containers=True)
        if self.model.kind == "wrapped":
            refs.append(Ref(None, (), ()))
        return refs

    # -- rules ------------------------------------------------------------------------------------
    @rule(data=st.data())
    def set_value(self, data: Any) -> None:
        picked = self.pick(data, self.model.refs())
        if not picked:
            return
        (r,) = picked
        v = self.value()
        self.apply(lambda s: s.set_value(r, dump(v)), lambda md: md.set_value(r, v), targets=(r,))

    @rule(data=st.data())
    def set_path(self, data: Any) -> None:
        refs = [r for r in self.model.refs(containers=True) if isinstance(node_at(self.model, r), O)]
        if not refs:
            return
        (r,) = self.pick(data, refs)
        extra = tuple(f"yo'l{self.counter.next()}" for _ in range(data.draw(st.integers(1, 3))))
        v = self.value()
        path = r.path + extra
        self.apply(lambda s: s.set_path(Ref(r.record, path), dump(v)), lambda md: md.set_path(r.record, path, v))

    @rule(data=st.data())
    def rename(self, data: Any) -> None:
        refs = [r for r in self.model.refs() if r.ipath] + (self.record_refs() if self.model.member else [])
        if not refs:
            return
        (r,) = self.pick(data, refs)
        existing = data.draw(st.booleans())
        key = f"nomi ‘{self.counter.next()}’"
        if existing and r.ipath:
            parent = self.model.node(r.record, r.ipath[:-1])
            if isinstance(parent, O) and parent.pairs:
                key = data.draw(st.sampled_from(parent.keys()))
        elif existing and not r.ipath and self.model.records:
            key = data.draw(st.sampled_from([k for k, _v in self.model.records]))
        self.apply(lambda s: s.rename_key(r, key), lambda md: md.rename(r, key), targets=(r,))

    @rule(data=st.data())
    def insert(self, data: Any) -> None:
        refs = self.containers()
        if not refs:
            return
        (r,) = self.pick(data, refs)
        c = node_at(self.model, r)
        index = data.draw(st.one_of(st.none(), st.integers(0, count(c))))
        key = None
        if isinstance(c, O):
            key = data.draw(st.sampled_from(c.keys() + [f"k{self.counter.next()}"])) if c.pairs else "k0"
        v = self.value()
        if data.draw(st.booleans()):
            given = key if data.draw(st.booleans()) else None
            self.apply(lambda s: s.paste(r, dump(v), index=index, key=given), lambda md: md.paste(r, index, v, given))
        else:
            self.apply(lambda s: s.insert(r, index, dump(v), key=key), lambda md: md.insert(r, index, v, key))

    @rule(data=st.data())
    def insert_records(self, data: Any) -> None:
        md = self.model
        index = data.draw(st.one_of(st.none(), st.integers(0, len(md.records))))
        k = data.draw(st.integers(1, min(3, edits.MAX_ITEMS)))
        values = [self.value() for _ in range(k)]
        if k == 1 and data.draw(st.booleans()):
            key = None
            if md.member and data.draw(st.booleans()):
                key = data.draw(st.sampled_from([kk for kk, _v in md.records] + ["yangi"])) if md.records else "yangi"

            def model(m: Model) -> None:
                if m.member and key is None:
                    raise ModelError("edits.error.key_required")
                m.insert_records(index, [(values[0], key)])

            self.apply(lambda s: s.insert(None, index, dump(values[0]), key=key), model)
            return
        text = "[" + ",".join(dump(v).decode("ascii") for v in values) + "]"
        self.apply(lambda s: s.paste(None, text, index=index, as_records=True),
                   lambda m: m.insert_records(index, [(v, None) for v in values]))

    @rule(data=st.data())
    def delete(self, data: Any) -> None:
        refs = self.pick(data, self.model.refs() + self.record_refs(), 3)
        if refs:
            self.apply(lambda s: s.delete(refs), lambda md: md.delete(list(refs)))

    @rule(data=st.data())
    def duplicate(self, data: Any) -> None:
        refs = self.pick(data, self.model.refs() + self.record_refs(), 3)
        if refs:
            self.apply(lambda s: s.duplicate(refs), lambda md: md.duplicate(list(refs)))

    @rule(data=st.data())
    def move(self, data: Any) -> None:
        md = self.model
        srcs = self.pick(data, md.refs(with_roots=False) + self.record_refs(), 2)
        if not srcs:
            return
        if data.draw(st.booleans()) and md.kind != "single":
            index = data.draw(st.one_of(st.none(), st.integers(0, len(md.records))))
            self.apply(lambda s: s.move(srcs, None, index), lambda m: m.move(list(srcs), None, index))
            return
        targets = self.containers()
        if not targets:
            return
        (t,) = self.pick(data, targets)
        index = data.draw(st.one_of(st.none(), st.integers(0, count(node_at(md, t)))))
        self.apply(lambda s: s.move(srcs, t, index), lambda m: m.move(list(srcs), t, index))

    @rule(data=st.data())
    def move_by(self, data: Any) -> None:
        md = self.model
        delta = data.draw(st.sampled_from([-2, -1, 1, 2]))
        if data.draw(st.booleans()) and md.kind != "single" and md.records:
            refs = self.pick(data, self.record_refs(), 2)
            P = sorted(r.record for r in refs)
            n = len(md.records)
            idx = max(0, P[0] + delta) if delta < 0 else min(n, P[-1] + 1 + delta)
            self.apply(lambda s: s.move_by(refs, delta), lambda m: m.move(list(refs), None, idx))
            return
        parents = [r for r in md.refs(containers=True) if count(node_at(md, r))]
        if not parents:
            return
        (p,) = self.pick(data, parents)
        c = node_at(md, p)
        n = count(c)
        kids = [Ref(p.record, p.path + ((c.pairs[i][0] if isinstance(c, O) else i),), p.ipath + (i,)) for i in range(n)]
        refs = self.pick(data, kids, 2)
        idxs = sorted(r.ipath[-1] for r in refs)
        idx = max(0, idxs[0] + delta) if delta < 0 else min(n, idxs[-1] + 1 + delta)
        self.apply(lambda s: s.move_by(refs, delta), lambda m: m.move(list(refs), p, idx))

    @rule(data=st.data())
    def change_type(self, data: Any) -> None:
        refs = self.pick(data, self.model.refs(), 2)
        to = data.draw(st.sampled_from(TYPES))
        if refs:
            self.apply(lambda s: s.change_type(refs, to), lambda md: md.change_type(list(refs), to), targets=tuple(refs))

    @rule(data=st.data())
    def record_tasks(self, data: Any) -> None:
        md = self.model
        if md.kind == "single" or not md.records:
            return
        n = len(md.records)
        positions = data.draw(st.lists(st.integers(0, n - 1), min_size=1, max_size=5))
        uniq = sorted(set(positions))
        if data.draw(st.booleans()):
            self.apply(lambda s: s.delete_records(positions), lambda m: m.delete([Ref(p, (), ()) for p in uniq]))
        else:
            self.apply(lambda s: s.duplicate_records(positions), lambda m: m.duplicate([Ref(p, (), ()) for p in uniq]))

    @precondition(lambda self: self.s is not None and self.s.can_undo)
    @rule()
    def undo(self) -> None:
        assert self.s is not None
        head = self.s.state.version
        before_doc = effective(self.doc)
        cs = self.s.undo()
        assert cs is not None and self.s.state.version == head - 1
        model, data = self.history[head - 1]
        assert effective(self.doc) == data
        got = Model.of(self.doc)
        assert got == model
        check_changeset(cs, before_doc, self.model, got, data, leaves=False)
        self.model = got

    @precondition(lambda self: self.s is not None and self.s.can_redo)
    @rule()
    def redo(self) -> None:
        assert self.s is not None
        head = self.s.state.version
        before_doc = effective(self.doc)
        cs = self.s.redo()
        assert cs is not None and self.s.state.version == head + 1
        model, data = self.history[head + 1]
        assert effective(self.doc) == data and Model.of(self.doc) == model
        check_changeset(cs, before_doc, self.model, model, data, leaves=False)
        self.model = model

    @invariant()
    def consistent(self) -> None:
        if self.s is None:
            return
        assert self.doc.gen == self.s.state.gen and self.s.dirty == (self.s.state.version > 0)


def test_random_command_sequences() -> None:
    run_state_machine_as_test(EditMachine, settings=settings(
        max_examples=int(os.environ.get("JR_EDIT_EXAMPLES", "60")), stateful_step_count=14, deadline=None,
        derandomize=os.environ.get("JR_EDIT_RANDOM") != "1", database=None, suppress_health_check=_HEALTH))


def test_random_with_big_paths(monkeypatch) -> None:
    """The array-layer / "pieces" paths of the record tasks and the item limit (limits lowered)."""
    monkeypatch.setattr(edits, "MAX_ITEMS", 2)
    monkeypatch.setattr(edits, "DELS_RUNS_MAX", 1)
    run_state_machine_as_test(EditMachine, settings=settings(
        max_examples=int(os.environ.get("JR_EDIT_EXAMPLES", "60")) // 3, stateful_step_count=12, deadline=None,
        derandomize=os.environ.get("JR_EDIT_RANDOM") != "1", database=None, suppress_health_check=_HEALTH))
