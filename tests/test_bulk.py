"""jsonreshape.bulk: bulk operations, preview -> exact counts -> apply (docs/ARCHITECTURE.md §12.6).

This module holds the INDEPENDENT naive reference (every record parsed, the rule applied in plain
recursive Python with ``PathPattern.matches`` / ``matches_prefix_of`` on full paths, duplicate keys found
by collecting every object's keys) and the "test 20" checker (AGENT_PROMPT §14: the pre-count equals the
real number of changes): a preview is compared with the reference (counts, examples, problems), then its
step is committed to a real :class:`~jsonreshape.editstore.EditStore` and applied with
``editstore.apply_step``; the effective records before and after are diffed (records that differ, values
that differ by the kind's own notion of a value), every changed record must parse to the reference's
value and be written in the document's style, every other record must keep its bytes, and undo must
restore the exact previous records. ``tests/test_bulk_matrix.py`` runs that over every rule x all
document kinds x both core backends x styles x plain/edited states.

The tests here: parameter validation and labels (uz/en), Uzbek apostrophe variants and ``ß`` in text
replace, literal replacements with backslashes, regex groups and templates, whole words with ``oʻ`` /
``o'``, path filters, each rule's corner cases (conflicts, failures, outermost matches, creation of
intermediate objects, equality), duplicate-key records, huge records and results over the size limit,
exact numbers, escapes, scopes (RecordSet subsets, unordered / repeated positions, ranges, ``gen``
mismatch -> stale scope), the byte prefilter (used only where sound), the dedupe rule, ``make_step`` /
``discard`` (files kept after a commit, deleted otherwise), cancellation cleanup, disk-space refusal
before and during the pass (monkeypatched ``shutil.disk_usage``), the entry spill, progress and
encodings (CRLF, BOM, UTF-16, gzip).
"""

from __future__ import annotations

import copy
import json
import random
import re
import shutil
from array import array
from collections import namedtuple
from decimal import Decimal
from pathlib import Path
from typing import Any, Iterable

import pytest

import jsongen
from jsonreshape import bulk, codec, editops, reader
from jsonreshape.bulk import BulkExample, BulkPreview, BulkSpec, make_step, preview
from jsonreshape.codec import JsonFloat, JsonInt
from jsonreshape.editstore import EditStore, Step, apply_step, undo_step
from jsonreshape.errors import (
    Cancelled,
    ConversionError,
    EditError,
    FileChangedError,
    PathSyntaxError,
    QuerySyntaxError,
    UserInputError,
)
from jsonreshape.fileio import read_range
from jsonreshape.i18n import t
from jsonreshape.overlay import EDIT_BASE, write_array_layer
from jsonreshape.paths import MISSING, canonical_index, format_path, get as path_get, parse_path, parse_pattern
from jsonreshape.reader import Document, split_member
from jsonreshape.recordset import RecordSet, filter_records
from jsonreshape.settings import Settings
from jsonreshape.tasks import CancelToken, Progress
from jsonreshape.textmatch import TextQuery
from overlay_helpers import make_source, new_record, write_source

ELLIPSIS = "\N{HORIZONTAL ELLIPSIS}"
DELETED_M = object()


# ----------------------------------------------------------------------------------
# fixtures
# ----------------------------------------------------------------------------------
@pytest.fixture
def backend(core_backend):
    """Reader scanner, editops (Scanner / Reformatter) and the bulk prefilter on one core backend."""
    name = "rust" if core_backend.__name__.endswith("_rscore") else "python"
    old = reader.set_scanner_backend(name)
    old_ops = editops.set_core_backend(name)
    old_bulk = bulk.set_core_backend(name)
    yield name
    reader.set_scanner_backend(old)
    editops.set_core_backend(old_ops)
    bulk.set_core_backend(old_bulk)


@pytest.fixture
def rigs(tmp_path: Path):
    """Factory of :class:`Rig` objects (document + edit store); everything is closed at teardown."""
    made: list[Rig] = []
    counter = [0]

    def make(source: Any, fmt: str | None = None, *, settings: Settings | None = None, open_kw: dict | None = None,
             **write_kw: Any) -> Rig:
        counter[0] += 1
        folder = tmp_path / f"rig{counter[0]}"
        folder.mkdir()
        path = folder / write_kw.pop("name", "src.json")
        kw: dict[str, Any] = dict(open_kw or {})
        if isinstance(source, (bytes, bytearray)):
            path.write_bytes(bytes(source))
        elif isinstance(source, str):        # an overlay_helpers kind
            kw.update(write_source(path, source, write_kw.pop("n", 24), write_kw.pop("seed", 0),
                                   write_kw.pop("style", "compact")))
        else:
            jsongen.write_file(path, source, fmt, **write_kw)
            if fmt == "wrapped":
                kw.setdefault("record_path", "data")
            kw.setdefault("format", fmt)
        doc = Document.open(path, settings=settings, **kw)
        doc.build_index()
        assert doc.error is None
        rig = Rig(doc, folder / "layer")
        made.append(rig)
        return rig

    yield make
    for rig in made:
        rig.close()


# ----------------------------------------------------------------------------------
# a document with its edit store
# ----------------------------------------------------------------------------------
class Rig:
    """A document, its EditStore and the state the "session" shows (the doc's overlay)."""

    def __init__(self, doc: Document, layer_dir: Path) -> None:
        self.doc = doc
        self.store = EditStore.open(doc, dir=layer_dir)
        self.state = self.store.load_state()
        self.member = bool(doc.member_records)

    def close(self) -> None:
        self.store.close()
        self.doc.close()

    # -- records ------------------------------------------------------------------------
    def records(self, state: Any = None) -> list[bytes]:
        """The raw bytes of every effective record (huge ones included)."""
        view = self.doc.pinned(self.state if state is None else state)
        with view.open_data() as f:
            return [read_range(f, *view.record_span(i)) for i in range(view.record_count)]

    def value_of(self, raw: bytes) -> Any:
        return codec.loads(raw[split_member(raw)[1]:] if self.member else raw)

    # -- edits through the store (the session's protocol) --------------------------------
    def commit(self, step: Step) -> Any:
        self.store.commit(step, state=self.state)
        self.state = apply_step(self.state, step, layers=self.store)
        self.doc.set_overlay(self.state)
        return self.state

    def _flags(self, *datas: bytes) -> tuple[int, int] | None:
        new = self.state.flags
        for d in datas:
            new |= editops.check_unit(d, member=self.member)
        return None if new == self.state.flags else (self.state.flags, new)

    def set(self, pos: int, data: bytes) -> Any:
        rid = self.state.rid_at(pos)
        span = self.store.append(data)
        return self.commit(Step("edit", "edits.step.set_value", {}, content=[(rid, self.state.top.get(rid), span)],
                                flags=self._flags(data)))

    def insert(self, pos: int, datas: list[bytes]) -> Any:
        rid0 = self.state.next_rid
        content = [(rid0 + i, None, self.store.append(d)) for i, d in enumerate(datas)]
        return self.commit(Step("edit", "edits.step.insert_record", {"count": len(datas)}, content=content,
                                struct=[("ins", pos, len(datas))], new_rids=[(pos, rid0, len(datas))],
                                flags=self._flags(*datas)))

    def delete(self, positions: Iterable[int]) -> Any:
        ps = array("Q", sorted(set(positions)))
        return self.commit(Step("edit", "edits.step.delete_records", {"count": len(ps)}, struct=[("dels", ps)]))

    def move(self, positions: Iterable[int], to: int) -> Any:
        ps = sorted(set(positions))
        return self.commit(Step("edit", "edits.step.move_records", {"count": len(ps)}, struct=[("move", ps, to)]))

    def array_layer(self, changes: dict[int, bytes]) -> Any:
        seg = self.store.new_segment()
        entries = []
        for pos, data in changes.items():
            entries.append((self.state.rid_at(pos), *self.store.append(data, seg=seg)))
        self.store.sync([seg])
        path = self.store.new_layer_path()
        write_array_layer(path, entries, flags=0, segments=[seg], work_dir=None)
        return self.commit(Step("bulk", "edits.step.bulk.replace", {"count": len(entries)}, push_layer=path.name,
                                segments=[seg], flags=self._flags(*changes.values())))

    def edit_some(self, seed: int = 5) -> None:
        """Point edits, inserts, deletes, a move and an array layer (positions != rids, units in segments)."""
        rng = random.Random(seed)
        doc = self.doc
        rec = lambda i: new_record(doc, rng, i, f"new{i}")  # noqa: E731
        if doc.kind == "single":
            self.set(0, rec(1))
            return
        n = self.state.count
        self.set(0, rec(1))
        self.set(n // 2, rec(2))
        self.insert(1, [rec(3), rec(4)])
        self.delete([3, self.state.count // 3])
        self.move([0, 2], self.state.count - 2)
        self.array_layer({p: rec(10 + p) for p in range(2, self.state.count, 5)})
        self.set(4, rec(5))

    # -- bulk -------------------------------------------------------------------------------
    def preview(self, spec: BulkSpec, scope: Any = None, **kw: Any) -> BulkPreview:
        return preview(self.doc.pinned(), self.state, self.store, spec, scope, **kw)

    def apply(self, pv: BulkPreview) -> tuple[Step, Any]:
        step = make_step(pv)
        before = self.state
        self.store.commit(step, state=before)
        new = apply_step(before, step, layers=self.store)
        self.doc.set_overlay(new)
        self.state = new
        return step, before

    def undo(self, step: Step) -> Any:
        self.store.set_head(self.store.head - 1)
        self.state = undo_step(self.state, step, layers=self.store)
        self.doc.set_overlay(self.state)
        return self.state


# ----------------------------------------------------------------------------------
# the naive reference
# ----------------------------------------------------------------------------------
def cut(text: str) -> str:
    return text if len(text) <= bulk.TEXT_CHARS else text[:bulk.TEXT_CHARS - 1] + ELLIPSIS


def vtext(v: Any) -> str:
    if v is DELETED_M:
        return t("bulk.example.deleted")
    if v is MISSING:
        return t("bulk.example.missing")
    return cut(codec.dumps(v).decode("utf-8"))


def ptext(path: tuple) -> str:
    return format_path(tuple(path)) if path else "$"


def norm(v: Any) -> Any:
    """Exact JSON value model: numbers as Decimal, objects without member order."""
    if v is None:
        return ("null",)
    if v is True or v is False:
        return ("bool", v)
    if isinstance(v, str):
        return ("str", str(v))
    if isinstance(v, (JsonFloat, JsonInt)):
        return ("num", Decimal(v.raw))
    if isinstance(v, int):
        return ("num", Decimal(int(v)))
    if isinstance(v, float):
        return ("num", Decimal(repr(float(v))))
    if isinstance(v, dict):
        return ("obj", frozenset((k, norm(x)) for k, x in v.items()))
    if isinstance(v, list):
        return ("arr", tuple(norm(x) for x in v))
    raise TypeError(type(v))


def ordered(v: Any) -> Any:
    """Like :func:`norm` but member order matters (a rename keeps positions)."""
    if isinstance(v, dict):
        return ("obj", tuple((k, ordered(x)) for k, x in v.items()))
    if isinstance(v, list):
        return ("arr", tuple(ordered(x) for x in v))
    return norm(v)


class Events:
    def __init__(self) -> None:
        self.changes = 0
        self.conflicts = 0
        self.failures = 0
        self.ev: list[tuple[str, str, str, str]] = []

    def ch(self, path: tuple, before: Any, after: Any) -> None:
        self.changes += 1
        self.ev.append(("c", ptext(path), vtext(before), vtext(after)))

    def cf(self, path: tuple, before: Any, reason: str) -> None:
        self.conflicts += 1
        self.ev.append(("p", ptext(path), vtext(before), reason))

    def fl(self, path: tuple, before: Any, reason: str) -> None:
        self.failures += 1
        self.ev.append(("p", ptext(path), vtext(before), reason))


class _Stop(Exception):
    pass


def collapse(s: str) -> str:
    out: list[str] = []
    in_ws = False
    for ch in s:
        if ch.isspace():
            if not in_ws:
                out.append(" ")
            in_ws = True
        else:
            out.append(ch)
            in_ws = False
    return "".join(out)


def ref_rule(kind: str, p: dict[str, Any]):
    """``fn(value) -> (new value, Events)`` of the naive reference (recursive, full paths)."""
    path_text = p.get("path")
    pat = parse_pattern(path_text) if isinstance(path_text, str) and path_text.strip() else None

    def strings(fn):
        def walk(v: Any, path: tuple, r: Events) -> Any:
            if isinstance(v, str):
                if pat is None or pat.matches_prefix_of(path):
                    n = fn(v)
                    if n != v:
                        r.ch(path, v, n)
                        return n
                return v
            if isinstance(v, dict):
                return {k: walk(x, path + (k,), r) for k, x in v.items()}
            if isinstance(v, list):
                return [walk(x, path + (i,), r) for i, x in enumerate(v)]
            return v
        return walk

    if kind == "replace":
        regex = p.get("mode") == "regex"
        cs, ww = bool(p.get("case_sensitive")), bool(p.get("whole_word"))
        repl = p.get("replacement", "")
        q = TextQuery(p["find"], "regex" if regex else "plain", cs, ww)
        rx = (q._regex if ww else re.compile(p["find"], 0 if cs else re.IGNORECASE)) if regex else None

        def sub(s: str) -> str:
            if rx is not None:
                return rx.sub(repl, s)
            for a, b in reversed(q.find_spans(s)):      # right to left, literal text
                s = s[:a] + repl + s[b:]
            return s
        walk = strings(sub)
    elif kind == "clean_strings":
        trim, coll = bool(p.get("trim")), bool(p.get("collapse_spaces"))

        def clean(s: str) -> str:
            if trim:
                s = s.strip()
            if coll:
                s = collapse(s)
            return s
        walk = strings(clean)
    elif kind == "rename_key":
        old, new = p["old"], p["new"]

        def walk(v: Any, path: tuple, r: Events) -> Any:
            if isinstance(v, dict):
                do = False
                if (pat is None or pat.matches(path)) and old in v:
                    if new in v:
                        r.cf(path, old, t("bulk.problem.key_exists", key=new))
                    else:
                        do = True
                        r.ch(path, old, new)
                out = {}
                for k, x in v.items():
                    out[new if do and k == old else k] = walk(x, path + (k,), r)
                return out
            if isinstance(v, list):
                return [walk(x, path + (i,), r) for i, x in enumerate(v)]
            return v
    elif kind == "delete_field":
        def walk(v: Any, path: tuple, r: Events) -> Any:
            if isinstance(v, dict):
                out = {}
                for k, x in v.items():
                    if pat.matches(path + (k,)):
                        r.ch(path + (k,), x, DELETED_M)
                        continue
                    out[k] = walk(x, path + (k,), r)
                return out
            if isinstance(v, list):
                return [walk(x, path + (i,), r) for i, x in enumerate(v)]
            return v
    elif kind == "convert_type":
        to = p["to"]

        def walk(v: Any, path: tuple, r: Events) -> Any:
            if pat.matches(path):
                if codec.json_type(v) == to:
                    return v
                try:
                    n = editops.convert_value(v, to)
                except ConversionError as exc:
                    r.fl(path, v, exc.message)
                    return v
                r.ch(path, v, n)
                return n
            if isinstance(v, dict):
                return {k: walk(x, path + (k,), r) for k, x in v.items()}
            if isinstance(v, list):
                return [walk(x, path + (i,), r) for i, x in enumerate(v)]
            return v
    elif kind == "set_field":
        segs = parse_path(p["path"])
        newv = codec.loads(p["value"])
        mode = p.get("mode", "always")

        def block(r: Events, i: int, node: Any, key: str, **kw: Any) -> None:
            if mode != "if_exists":
                r.cf(segs[:i], node, t(key, path=ptext(segs[:i]), **kw))
            raise _Stop

        def put(v: Any, i: int, r: Events) -> Any:
            seg = segs[i]
            last = i == len(segs) - 1
            if isinstance(v, dict):
                if isinstance(seg, int):
                    block(r, i, v, "bulk.problem.not_array")
                if seg in v:
                    out = dict(v)
                    if last:
                        if mode == "if_missing" or norm(v[seg]) == norm(newv):
                            raise _Stop
                        r.ch(segs, v[seg], newv)
                        out[seg] = newv
                    else:
                        out[seg] = put(v[seg], i + 1, r)
                    return out
                if mode == "if_exists":
                    raise _Stop
                rest = segs[i:]
                for j in range(1, len(rest)):
                    if isinstance(rest[j], int):
                        block(r, i + j, MISSING, "bulk.problem.missing_array")
                node = newv
                for s in reversed(rest[1:]):
                    node = {s: node}
                r.ch(segs, MISSING, newv)
                out = dict(v)
                out[seg] = node
                return out
            if isinstance(v, list):
                idx = seg if isinstance(seg, int) else canonical_index(seg)
                if idx is None:
                    block(r, i, v, "bulk.problem.not_object")
                if idx >= len(v):
                    block(r, i, v, "bulk.problem.no_element", index=idx)
                out = list(v)
                if last:
                    if mode == "if_missing" or norm(v[idx]) == norm(newv):
                        raise _Stop
                    r.ch(segs, v[idx], newv)
                    out[idx] = newv
                else:
                    out[idx] = put(v[idx], i + 1, r)
                return out
            block(r, i, v, "bulk.problem.not_object")

        def walk(v: Any, path: tuple, r: Events) -> Any:
            try:
                return put(v, 0, r)
            except _Stop:
                return v
    else:
        raise ValueError(kind)

    def run(value: Any) -> tuple[Any, Events]:
        r = Events()
        return walk(copy.deepcopy(value), (), r), r
    return run


def has_dup_keys(vb: bytes) -> bool:
    found: list[bool] = []

    def hook(pairs: list) -> dict:
        keys = [k for k, _ in pairs]
        if len(keys) != len(set(keys)):
            found.append(True)
        return dict(pairs)

    json.loads(vb.decode("utf-8"), object_pairs_hook=hook, parse_int=str, parse_float=str)
    return bool(found)


def positions_of(scope: Any, count: int) -> list[int]:
    if scope is None or (isinstance(scope, RecordSet) and scope.kind == "base"):
        return list(range(count))
    return sorted(set(int(p) for p in scope))


def reference(rig: Rig, spec: BulkSpec, scope: Any = None, state: Any = None) -> dict[str, Any]:
    """The expected preview of ``spec`` (content kinds) on the rig's state."""
    view = rig.doc.pinned(rig.state if state is None else state)
    p = spec.normalized()
    run = ref_rule(spec.kind, p)
    large = view.settings.large_node_bytes
    style = editops.detect_style(view)
    out = dict(scanned=0, records_changed=0, values_changed=0, conflicts=0, failures=0, skipped_huge=0,
               bytes_new=0, examples=[], problems=[], new={})
    for pos in positions_of(scope, view.record_count):
        out["scanned"] += 1
        s, e = view.record_span(pos)
        if e - s > large:
            out["skipped_huge"] += 1
            continue
        raw = view.record_bytes(pos)
        head, vb = (raw[:split_member(raw)[1]], raw[split_member(raw)[1]:]) if rig.member else (None, raw)
        new, r = run(codec.loads(vb))
        if not r.changes:
            out["conflicts"] += r.conflicts
            out["failures"] += r.failures
            out["problems"] += [(pos, a, b, c) for kind, a, b, c in r.ev if kind == "p"]
            continue
        if has_dup_keys(vb):
            out["conflicts"] += 1
            out["problems"].append((pos, "$", cut(vb.decode("utf-8")), t("bulk.problem.duplicate_keys")))
            continue
        unit = editops.format_record(codec.dumps(new), style, member_head=head)
        if len(unit) > large:
            out["skipped_huge"] += 1
            continue
        out["records_changed"] += 1
        out["values_changed"] += r.changes
        out["conflicts"] += r.conflicts
        out["failures"] += r.failures
        out["bytes_new"] += len(unit)
        out["new"][pos] = new
        out["examples"] += [(pos, a, b, c) for kind, a, b, c in r.ev if kind == "c"]
        out["problems"] += [(pos, a, b, c) for kind, a, b, c in r.ev if kind == "p"]
    out["examples"] = out["examples"][:bulk.EXAMPLE_COUNT]
    out["problems"] = out["problems"][:bulk.PROBLEM_COUNT]
    return out


def ref_dedupe(rig: Rig, spec: BulkSpec, scope: Any = None) -> list[int]:
    """Positions the dedupe rule deletes (naive canonical grouping)."""
    raws = rig.records()
    p = spec.normalized()
    key = p["key"].strip()
    groups: dict[Any, list[int]] = {}
    for pos in positions_of(scope, len(raws)):
        raw = raws[pos]
        if key == "$key":
            if not rig.member:
                continue
            value = split_member(raw)[0]
        else:
            value = path_get(rig.value_of(raw), parse_path(key))
            if value is MISSING:
                continue
        groups.setdefault(norm(value), []).append(pos)
    keep_last = p["keep"].strip().lower() == "last"
    return sorted(q for g in groups.values() if len(g) > 1 for q in (g[:-1] if keep_last else g[1:]))


# ----------------------------------------------------------------------------------
# value diffs (the "real" number of changed values, kind by kind)
# ----------------------------------------------------------------------------------
def diff_values(kind: str, a: Any, b: Any) -> int:
    if kind == "set_field":
        return int(norm(a) != norm(b))
    if kind in ("replace", "clean_strings"):
        if isinstance(a, str):
            return int(a != b)
        if isinstance(a, dict):
            assert list(a) == list(b)
            return sum(diff_values(kind, a[k], b[k]) for k in a)
        if isinstance(a, list):
            assert len(a) == len(b)
            return sum(diff_values(kind, x, y) for x, y in zip(a, b))
        assert norm(a) == norm(b)
        return 0
    if kind == "rename_key":
        if isinstance(a, dict):
            ia, ib = list(a.items()), list(b.items())
            assert len(ia) == len(ib)
            return sum(int(ka != kb) + diff_values(kind, va, vb) for (ka, va), (kb, vb) in zip(ia, ib))
        if isinstance(a, list):
            return sum(diff_values(kind, x, y) for x, y in zip(a, b))
        return 0
    if kind == "delete_field":
        if isinstance(a, dict):
            return sum(1 if k not in b else diff_values(kind, v, b[k]) for k, v in a.items())
        if isinstance(a, list):
            assert len(a) == len(b)
            return sum(diff_values(kind, x, y) for x, y in zip(a, b))
        return 0
    if kind == "convert_type":
        if codec.json_type(a) != codec.json_type(b):
            return 1
        if isinstance(a, dict):
            return sum(diff_values(kind, v, b[k]) for k, v in a.items())
        if isinstance(a, list):
            return sum(diff_values(kind, x, y) for x, y in zip(a, b))
        return 0
    raise ValueError(kind)


def example_tuples(items: list[BulkExample]) -> list[tuple]:
    return [(e.position, e.path, e.before, e.after) for e in items]


def check_rule(rig: Rig, spec: BulkSpec, scope: Any = None, *, undo: bool = True) -> BulkPreview:
    """Preview vs the naive reference, then apply (test 20) and undo; returns the preview."""
    if spec.kind == "dedupe":
        return check_dedupe(rig, spec, scope, undo=undo)
    before_state = rig.state
    before = rig.records()
    ref = reference(rig, spec, scope)
    pv = rig.preview(spec, scope)
    label = f"{spec.kind} {spec.params}"
    got = {k: getattr(pv, k) for k in ("scanned", "records_changed", "values_changed", "conflicts", "failures",
                                        "skipped_huge", "bytes_new")}
    want = {k: ref[k] for k in got}
    assert got == want, label
    assert example_tuples(pv.examples) == ref["examples"], label
    assert example_tuples(pv.problems) == ref["problems"], label
    assert pv.gen == before_state.gen
    if not pv.records_changed:
        assert pv.layer is None and pv.segment is None
        with pytest.raises(UserInputError) as ei:
            make_step(pv)
        assert ei.value.key == "bulk.error.nothing"
        pv.discard()
        return pv
    seg_file = rig.store.segment_path(pv.segment)
    assert seg_file.exists() and (rig.store.dir / pv.layer).exists()
    step, _ = rig.apply(pv)
    assert step.kind == "bulk" and step.label_key == f"edits.step.bulk.{spec.kind}"
    after = rig.records()
    # -- test 20: the pre-count is the real change ---------------------------------------
    assert len(after) == len(before)
    changed = [i for i in range(len(before)) if before[i] != after[i]]
    assert len(changed) == pv.records_changed == len(ref["new"]), label
    assert sorted(ref["new"]) == changed, label
    style = editops.detect_style(rig.doc)
    values = 0
    for i in changed:
        old_v, new_v = rig.value_of(before[i]), rig.value_of(after[i])
        values += diff_values(spec.kind, old_v, new_v)
        assert ordered(new_v) == ordered(ref["new"][i]), (label, i)
        head = before[i][:split_member(before[i])[1]] if rig.member else None
        assert after[i] == editops.format_record(codec.dumps(ref["new"][i]), style, member_head=head)
        span = rig.doc.pinned(rig.state).record_span(i)
        assert span[0] >= EDIT_BASE
    assert values == pv.values_changed, label
    assert step.records == (changed if len(changed) <= bulk.RECORDS_MAX else None)
    pv.discard()                                     # after the commit: nothing is deleted
    assert seg_file.exists() and (rig.store.dir / pv.layer).exists()
    assert rig.records() == after
    if undo:
        rig.undo(step)
        assert rig.records() == before
    return pv


def check_dedupe(rig: Rig, spec: BulkSpec, scope: Any = None, *, undo: bool = True) -> BulkPreview:
    before = rig.records()
    dels = ref_dedupe(rig, spec, scope)
    pv = rig.preview(spec, scope)
    assert pv.records_changed == len(dels) and pv.values_changed == 0
    assert list(pv.positions or []) == dels
    assert [e.position for e in pv.examples] == dels[:bulk.EXAMPLE_COUNT]
    assert all(e.after == t("bulk.example.deleted") for e in pv.examples)
    if not dels:
        with pytest.raises(UserInputError):
            make_step(pv)
        pv.discard()
        return pv
    if rig.member:
        assert [k for _r, k, _n in pv.keys] == [split_member(before[p])[0] for p in dels]
        assert [r for r, _k, _n in pv.keys] == list(rig.state.rids_at(array("Q", dels)))
    else:
        assert pv.keys is None
    step, _ = rig.apply(pv)
    assert step.struct == [("dels", array("Q", dels))] and step.records == [] and step.push_layer is None
    assert step.label_params["count"] == len(dels)
    assert step.keys == (pv.keys if rig.member else None)
    after = rig.records()
    gone = set(dels)
    assert after == [r for i, r in enumerate(before) if i not in gone]
    assert len(before) - len(after) == pv.records_changed
    if undo:
        rig.undo(step)
        assert rig.records() == before
    return pv


# ----------------------------------------------------------------------------------
# small documents
# ----------------------------------------------------------------------------------
def values_doc(rigs, values: list[Any], fmt: str = "jsonl", **kw: Any) -> Rig:
    return rigs(values, fmt, **kw)


def strings_of(rig: Rig) -> list[Any]:
    return [rig.value_of(r) for r in rig.records()]


# ----------------------------------------------------------------------------------
# validation and labels
# ----------------------------------------------------------------------------------
@pytest.mark.parametrize("spec,key", [
    (BulkSpec("nope", {}), "bulk.error.kind"),
    (BulkSpec("replace", "x"), "bulk.error.params"),        # type: ignore[arg-type]
    (BulkSpec("replace", {"find": "a", "colour": 1}), "bulk.error.unknown_param"),
    (BulkSpec("replace", {}), "bulk.error.missing_param"),
    (BulkSpec("replace", {"find": ""}), "bulk.error.find_empty"),
    (BulkSpec("replace", {"find": "a", "mode": "glob"}), "bulk.error.mode"),
    (BulkSpec("replace", {"find": "a", "case_sensitive": 1}), "bulk.error.param_type"),
    (BulkSpec("replace", {"find": 5}), "bulk.error.param_type"),
    (BulkSpec("replace", {"find": "a", "path": 3}), "bulk.error.param_type"),
    (BulkSpec("replace", {"find": "(a)", "mode": "regex", "replacement": "\\2"}), "bulk.error.replacement"),
    (BulkSpec("replace", {"find": "a", "mode": "regex", "replacement": "\\q"}), "bulk.error.replacement"),
    (BulkSpec("replace", {"find": "a", "mode": "regex", "replacement": "\\g<x>"}), "bulk.error.replacement"),
    (BulkSpec("replace", {"find": "a", "mode": "regex", "replacement": "\\"}), "bulk.error.replacement"),
    (BulkSpec("rename_key", {"old": "a", "new": "a"}), "bulk.error.same_key"),
    (BulkSpec("rename_key", {"old": "a"}), "bulk.error.missing_param"),
    (BulkSpec("delete_field", {"path": "  "}), "bulk.error.path_required"),
    (BulkSpec("delete_field", {"path": "$"}), "bulk.error.path_root"),
    (BulkSpec("set_field", {"path": "", "value": "1"}), "bulk.error.path_required"),
    (BulkSpec("set_field", {"path": "$", "value": "1"}), "bulk.error.path_root"),
    (BulkSpec("set_field", {"path": "a", "value": "{"}), "bulk.error.value"),
    (BulkSpec("set_field", {"path": "a", "value": "Toshkent"}), "bulk.error.value"),
    (BulkSpec("set_field", {"path": "a", "value": "1", "mode": "sometimes"}), "bulk.error.set_mode"),
    (BulkSpec("clean_strings", {}), "bulk.error.clean_nothing"),
    (BulkSpec("convert_type", {"path": "a", "to": "date"}), "bulk.error.type"),
    (BulkSpec("convert_type", {"path": "", "to": "string"}), "bulk.error.path_required"),
    (BulkSpec("dedupe", {"key": ""}), "dupes.error.key"),
    (BulkSpec("dedupe", {"key": "a[*]"}), "dupes.error.wildcard"),
    (BulkSpec("dedupe", {"key": "id", "keep": "middle"}), "dupes.error.keep"),
])
def test_validate_refuses_bad_specs(spec: BulkSpec, key: str, lang_uz) -> None:
    with pytest.raises(UserInputError) as ei:
        spec.validate()
    assert ei.value.key == key
    assert ei.value.message and "{" not in ei.value.message


def test_validate_syntax_errors_and_good_specs() -> None:
    with pytest.raises(QuerySyntaxError):
        BulkSpec("replace", {"find": "(", "mode": "regex"}).validate()
    with pytest.raises(PathSyntaxError):
        BulkSpec("replace", {"find": "a", "path": "a[x"}).validate()
    with pytest.raises(PathSyntaxError):
        BulkSpec("set_field", {"path": "a[*]", "value": "1"}).validate()     # concrete paths only
    good = [
        BulkSpec("replace", {"find": "a", "replacement": "\\1", "mode": "text"}),   # literal in text mode
        BulkSpec("replace", {"find": "(?P<w>a)", "replacement": "\\g<w>\\1\\\\", "mode": "regex"}),
        BulkSpec("rename_key", {"old": "", "new": "x", "path": None}),
        BulkSpec("delete_field", {"path": "..zip"}),
        BulkSpec("set_field", {"path": "a.b[0]", "value": " 1e400 ", "mode": "if_exists"}),
        BulkSpec("clean_strings", {"collapse_spaces": True, "path": " "}),
        BulkSpec("convert_type", {"path": "$", "to": "array"}),
        BulkSpec("dedupe", {"key": "$key", "keep": "last"}),
    ]
    for spec in good:
        spec.validate()
        assert spec.label()
    assert BulkSpec("clean_strings", {"trim": True}).normalized() == {"trim": True, "collapse_spaces": False,
                                                                     "path": None}


def test_labels_and_kind_titles(lang_en) -> None:
    assert bulk.kind_title("replace") == "Find and replace"
    assert bulk.kind_title("dedupe") == "Remove duplicates"
    assert BulkSpec("replace", {"find": "o'zbek", "replacement": "UZ", "mode": "regex", "whole_word": True,
                                "path": "name"}).label() == \
        'Find and replace: "o\'zbek" → "UZ" (regex, whole word) (in name)'
    assert BulkSpec("rename_key", {"old": "city", "new": "shahar"}).label() == 'Rename key: "city" → "shahar"'
    assert BulkSpec("delete_field", {"path": "..zip"}).label() == "Delete field: ..zip"
    assert BulkSpec("set_field", {"path": "a.b", "value": "1", "mode": "if_missing"}).label() == \
        "Set field: a.b = 1 (only where it is missing)"
    assert BulkSpec("clean_strings", {"trim": True, "collapse_spaces": True}).label() == \
        "Clean strings: trim, collapse spaces"
    assert BulkSpec("convert_type", {"path": "id", "to": "string"}).label() == "Convert type: id → string"
    assert BulkSpec("dedupe", {"key": "$key"}).label() == "Remove duplicates (by the record key, keeping the first)"
    assert BulkSpec("dedupe", {"key": "id", "keep": "last"}).label() == "Remove duplicates (by id, keeping the last)"
    # never raises, also for nonsense
    assert BulkSpec("nope", {}).label() == "nope"
    assert BulkSpec("replace", {"find": 3}).label()
    long = BulkSpec("replace", {"find": "x" * 500})
    assert len(long.label()) < 120


def test_labels_uz(lang_uz) -> None:
    assert BulkSpec("rename_key", {"old": "city", "new": "shahar"}).label() == "Kalitni qayta nomlash: «city» → «shahar»"
    assert BulkSpec("convert_type", {"path": "id", "to": "bool"}).label() == "Turni o'zgartirish: id → mantiqiy qiymat"
    assert bulk.kind_title("clean_strings") == "Matnlarni tozalash"


# ----------------------------------------------------------------------------------
# replace: Uzbek text, literal and template replacements, whole words
# ----------------------------------------------------------------------------------
def test_text_replace_apostrophes_case_and_cyrillic(rigs, backend) -> None:
    vals = [{"s": v} for v in ["O'zbekiston", "Oʻzbekiston", "O‘zbekiston", "O’zbekiston", "Oʼzbekiston",
                               "O`zbekiston", "O´zbekiston", "Ozbekiston", "O'ZBEKISTON", "Ўзбекистон",
                               "Toshkent", "toshkent", "TOSHKENT", "Тошкент", "ToShKeNt"]]
    rig = values_doc(rigs, vals)
    pv = check_rule(rig, BulkSpec("replace", {"find": "o'zbekiston", "replacement": "UZ"}), undo=False)
    got = [v["s"] for v in strings_of(rig)]
    assert got[:7] == ["UZ"] * 7 and got[7] == "Ozbekiston" and got[8] == "UZ" and got[9] == "Ўзбекистон"
    assert pv.records_changed == 8
    pv2 = check_rule(rig, BulkSpec("replace", {"find": "Toshkent", "replacement": "Tashkent",
                                                "case_sensitive": True}), undo=False)
    assert pv2.records_changed == 1
    assert [v["s"] for v in strings_of(rig)][10:] == ["Tashkent", "toshkent", "TOSHKENT", "Тошкент", "ToShKeNt"]
    check_rule(rig, BulkSpec("replace", {"find": "тошкент", "replacement": "Тошкент шаҳри"}))


def test_text_replace_sharp_s_and_expanding_folds(rigs, backend) -> None:
    vals = [{"s": v} for v in ["Straße", "STRASSE", "strasse", "Strasse ß", "ﬁle", "File", "İstanbul"]]
    rig = values_doc(rigs, vals)
    check_rule(rig, BulkSpec("replace", {"find": "strasse", "replacement": "ko'cha"}), undo=False)
    assert [v["s"] for v in strings_of(rig)][:4] == ["ko'cha", "ko'cha", "ko'cha", "ko'cha ß"]
    check_rule(rig, BulkSpec("replace", {"find": "ss", "replacement": "_"}), undo=False)
    assert [v["s"] for v in strings_of(rig)][3] == "ko'cha _"            # ß alone folds to "ss"
    check_rule(rig, BulkSpec("replace", {"find": "file", "replacement": "fayl"}), undo=False)
    assert [v["s"] for v in strings_of(rig)][4:6] == ["fayl", "fayl"]
    check_rule(rig, BulkSpec("replace", {"find": "i̇stanbul", "replacement": "Istanbul"}))


def test_text_replacement_is_literal(rigs, backend) -> None:
    rig = values_doc(rigs, [{"p": "C:/data/a.txt"}, {"p": "nothing"}, ["a.b", "AB"]])
    check_rule(rig, BulkSpec("replace", {"find": "a", "replacement": "\\1\\\\$0\\g<0>\\n",
                                          "case_sensitive": True}), undo=False)
    assert strings_of(rig) == [{"p": "C:/d\\1\\\\$0\\g<0>\\nt\\1\\\\$0\\g<0>\\n/\\1\\\\$0\\g<0>\\n.txt"},
                               {"p": "nothing"}, ["\\1\\\\$0\\g<0>\\n.b", "AB"]]
    raw = rig.records()[0]
    assert b'"C:/d\\\\1\\\\\\\\$0' in raw                   # JSON-escaped backslashes in the unit


def test_regex_groups_templates_and_flags(rigs, backend) -> None:
    rig = values_doc(rigs, [{"d": "10-20 and 3-4"}, {"d": "a-b"}, {"d": "Ali Valiyev"}, {"d": "ALI"}])
    check_rule(rig, BulkSpec("replace", {"find": r"(\d+)-(\d+)", "replacement": r"\2-\1", "mode": "regex"}),
               undo=False)
    check_rule(rig, BulkSpec("replace", {"find": r"(?P<ism>ali)\b", "replacement": r"[\g<ism>]\n",
                                          "mode": "regex"}), undo=False)
    assert [v["d"] for v in strings_of(rig)] == ["20-10 and 4-3", "a-b", "[Ali]\n Valiyev", "[ALI]\n"]
    check_rule(rig, BulkSpec("replace", {"find": "ali", "replacement": "X", "mode": "regex", "case_sensitive": True}))
    # an empty match: Python's re.sub semantics
    check_rule(rig, BulkSpec("replace", {"find": "x*", "replacement": "-", "mode": "regex", "path": "d"}))


def test_whole_word_with_uzbek_apostrophes(rigs, backend) -> None:
    vals = [{"s": v} for v in ["o'zbek tili", "oʻzbekcha", "O‘ZBEK", "yangi oʻzbek", "o'zbek'", "'o'zbek'",
                               "bo'zbek", "o'zbek_x", "o'zbek-tili"]]
    rig = values_doc(rigs, vals)
    pv = check_rule(rig, BulkSpec("replace", {"find": "o'zbek", "replacement": "UZ", "whole_word": True}),
                    undo=False)
    assert [v["s"] for v in strings_of(rig)] == ["UZ tili", "oʻzbekcha", "UZ", "yangi UZ", "UZ'", "'UZ'",
                                                 "bo'zbek", "o'zbek_x", "UZ-tili"]
    assert pv.values_changed == 6
    # regex mode: the same whole-word guards
    rig2 = values_doc(rigs, vals)
    check_rule(rig2, BulkSpec("replace", {"find": "o.zbek", "replacement": "UZ", "whole_word": True,
                                           "mode": "regex"}))


def test_replace_only_values_and_path_filters(rigs, backend) -> None:
    vals = [{"Toshkent": "Toshkent", "a": {"Toshkent": ["Toshkent", {"b": "Toshkent"}]}, "n": 1},
            ["Toshkent", {"a": "Toshkent"}], "Toshkent", 5, None]
    rig = values_doc(rigs, vals)
    for path in (None, "a", "a.Toshkent[1]", "..b", "[*]", "$", "**", "*", "a.*[0]", "/a/Toshkent/0"):
        check_rule(rig, BulkSpec("replace", {"find": "toshkent", "replacement": "T", "path": path}))
    check_rule(rig, BulkSpec("replace", {"find": "toshkent", "replacement": "T"}), undo=False)
    assert strings_of(rig)[0] == {"Toshkent": "T", "a": {"Toshkent": ["T", {"b": "T"}]}, "n": 1}  # keys untouched
    assert strings_of(rig)[2] == "T"


def test_replace_never_touches_record_keys(rigs, backend) -> None:
    data = b'{"Toshkent": {"city": "Toshkent"}, "k2": {"city": "Buxoro"}, "Toshkent 2": "Toshkent"}'
    rig = rigs(data, open_kw={"format": "keyed"})
    pv = check_rule(rig, BulkSpec("replace", {"find": "toshkent", "replacement": "X"}), undo=False)
    assert pv.records_changed == 2
    keys = [split_member(r)[0] for r in rig.records()]
    assert keys == ["Toshkent", "k2", "Toshkent 2"]
    assert rig.records()[0].startswith(b'"Toshkent": ')


# ----------------------------------------------------------------------------------
# the other rules
# ----------------------------------------------------------------------------------
def test_rename_key_positions_conflicts_and_paths(rigs, backend) -> None:
    vals = [{"a": 1, "x": {"a": 2, "b": 3}, "z": 0},
            {"x": {"a": 1, "b": 2}, "a": {"a": {"a": 0}}},
            [{"a": 1}, {"a": 1, "b": 0}, {"b": 1}],
            {"b": 1, "a": 2},
            "a"]
    rig = values_doc(rigs, vals)
    for path in (None, "x", "**", "*", "[*]", "a.a", "..a", "$"):
        check_rule(rig, BulkSpec("rename_key", {"old": "a", "new": "b", "path": path}))
    pv = check_rule(rig, BulkSpec("rename_key", {"old": "a", "new": "ä"}), undo=False)
    assert strings_of(rig)[0] == {"ä": 1, "x": {"ä": 2, "b": 3}, "z": 0}          # member positions kept
    assert pv.conflicts == 0
    pv = check_rule(rig, BulkSpec("rename_key", {"old": "b", "new": "ä"}), undo=False)
    assert pv.conflicts == 4 and pv.problems[0].after == t("bulk.problem.key_exists", key="ä")
    # an object selected by its ORIGINAL path: "**.a.*" sees the inner objects under the old key
    rig2 = values_doc(rigs, [{"a": {"k": {"a": {"z": {"a": 1}}}}}])
    check_rule(rig2, BulkSpec("rename_key", {"old": "a", "new": "b", "path": "**.a.*"}), undo=False)
    assert strings_of(rig2) == [{"a": {"k": {"b": {"z": {"b": 1}}}}}]


def test_delete_field_outermost_and_array_elements(rigs, backend) -> None:
    vals = [{"zip": 1, "a": {"zip": {"zip": 2}, "b": [{"zip": 3}, 4]}},
            [{"zip": 1}, {"x": {"zip": 0}}],
            {"0": "key zero", "arr": ["e0", "e1"]},
            {"x": 1}]
    rig = values_doc(rigs, vals)
    for path in ("..zip", "zip", "a.zip", "a.b[0].zip", "*", "**", "[*].zip", "arr.0", "arr[0]", "0", "a.**",
                 "**.zip.zip", "/a/b/0/zip"):
        check_rule(rig, BulkSpec("delete_field", {"path": path}))
    pv = check_rule(rig, BulkSpec("delete_field", {"path": "..zip"}), undo=False)
    assert strings_of(rig)[0] == {"a": {"b": [{}, 4]}}
    assert pv.values_changed == 5                   # outermost only: zip.zip is not counted
    pv = check_rule(rig, BulkSpec("delete_field", {"path": "arr[0]"}), undo=False)  # elements are not fields
    assert pv.records_changed == 0


def test_set_field_creation_modes_equality_and_conflicts(rigs, backend) -> None:
    vals = [{"a": {"b": 1}},                     # exists
            {"a": {"c": 1}},                     # missing member
            {},                                  # missing intermediate
            {"a": 5},                            # scalar intermediate -> conflict
            {"a": None},                         # null intermediate -> conflict
            {"a": [{"b": 0}]},                   # array where a key follows -> conflict
            {"a": {"b": 1.0}},                   # equal value (1 == 1.0)
            [1, 2],                              # array record
            "text",                              # scalar record
            {"a": {"b": {"deep": [1]}}}]
    rig = values_doc(rigs, vals)
    for mode in ("always", "if_missing", "if_exists"):
        for value in ("1", '"x"', '{"n": 1e400}', "null", "[1, 2, 3]"):
            check_rule(rig, BulkSpec("set_field", {"path": "a.b", "value": value, "mode": mode}))
    pv = check_rule(rig, BulkSpec("set_field", {"path": "a.b", "value": " 1 "}), undo=False)
    assert [str(p.after) for p in pv.problems] == [
        t("bulk.problem.not_object", path="a"), t("bulk.problem.not_object", path="a"),
        t("bulk.problem.not_object", path="a"), t("bulk.problem.not_object", path="$"),
        t("bulk.problem.not_object", path="$")]
    assert pv.conflicts == 5 and pv.records_changed == 3
    got = strings_of(rig)
    assert got[1] == {"a": {"c": 1, "b": 1}} and got[2] == {"a": {"b": 1}} and got[6] == {"a": {"b": 1.0}}
    # arrays: elements are addressed, never created
    rig2 = values_doc(rigs, [{"o": [{"id": 1}]}, {"o": []}, {}, {"o": {"0": {"id": 2}}}, {"o": [5]}])
    for path in ("o[0].id", "o.0.id", "o[1].id", "o[0]", "x[0].y", "o.x"):
        for mode in ("always", "if_missing", "if_exists"):
            check_rule(rig2, BulkSpec("set_field", {"path": path, "value": "12345678901234567890", "mode": mode}))


def test_clean_strings(rigs, backend) -> None:
    vals = [{"s": "  a  b  ", "t": "\t\n x \u00a0 y\u2003", "k  k": "ok", "n": 1}, " lead", "trail ", "a\u2028\u2029b",
            ["no", "change"], {"x": {"s": "  deep  "}}]
    rig = values_doc(rigs, vals)
    for trim, coll in ((True, False), (False, True), (True, True)):
        for path in (None, "s", "x", "..s"):
            check_rule(rig, BulkSpec("clean_strings", {"trim": trim, "collapse_spaces": coll, "path": path}))
    check_rule(rig, BulkSpec("clean_strings", {"trim": True, "collapse_spaces": True}), undo=False)
    got = strings_of(rig)
    assert got[0] == {"s": "a b", "t": "x y", "k  k": "ok", "n": 1}       # keys untouched
    assert got[1:4] == ["lead", "trail", "a b"]


def test_convert_type_outermost_failures_and_root(rigs, backend) -> None:
    vals = [{"v": "12"}, {"v": "1e400"}, {"v": " 1.10 "}, {"v": "abc"}, {"v": True}, {"v": None},
            {"v": [1, {"v": "2"}]}, {"v": {"v": "3"}}, {"v": 7}, {"w": 1}, "yo'q", "ha", "0"]
    rig = values_doc(rigs, vals)
    for to in editops.JSON_TYPES:
        for path in ("v", "..v", "$", "v[*]", "**"):
            check_rule(rig, BulkSpec("convert_type", {"path": path, "to": to}))
    pv = check_rule(rig, BulkSpec("convert_type", {"path": "v", "to": "number"}), undo=False)
    assert pv.failures == 3 and pv.records_changed == 5
    got = strings_of(rig)
    assert codec.dumps(got[1]) == b'{"v":1e400}' and codec.dumps(got[2]) == b'{"v":1.10}'
    assert got[4] == {"v": 1} and got[5] == {"v": 0}
    rig2 = values_doc(rigs, vals[10:] + [{"v": "Ha"}])
    pv = check_rule(rig2, BulkSpec("convert_type", {"path": "$", "to": "bool"}))
    assert pv.records_changed == 3 and pv.failures == 1


def test_exact_numbers_survive_a_change(rigs, backend) -> None:
    rec = b'{"old": 1, "nums": [0.1, 1e400, 12345678901234567890, -0.0, 1.10, -0, 1E5], "big": 1e-400}'
    rig = rigs(b"[" + rec + b", " + rec + b"]", open_kw={"format": "array"})
    check_rule(rig, BulkSpec("rename_key", {"old": "old", "new": "new"}), undo=False)
    for raw in rig.records():               # the file's single-line style (": " and ", ") is kept as well
        assert raw == rec.replace(b'"old"', b'"new"')
    compact = rec.replace(b": ", b":").replace(b", ", b",")
    rig = rigs(b"[" + compact + b"," + compact + b"]", open_kw={"format": "array"})
    check_rule(rig, BulkSpec("rename_key", {"old": "old", "new": "new"}), undo=False)
    assert rig.records() == [compact.replace(b'"old"', b'"new"')] * 2


def test_escapes_are_normalized_in_changed_records_only(rigs, backend) -> None:
    recs = [b'{"a":"\\u00e9\\u0041\\/","b":1,"s":"\\ud83d\\ude00 \\ud800"}', b'{"a":"\\u00e9","c":1}']
    rig = rigs(b"\n".join(recs) + b"\n", open_kw={"format": "jsonl"})
    pv = check_rule(rig, BulkSpec("rename_key", {"old": "b", "new": "B"}), undo=False)
    assert pv.records_changed == 1
    assert rig.records() == ['{"a":"éA/","B":1,"s":"😀 \\ud800"}'.encode("utf-8"), recs[1]]
    from jsonreshape.core import FLAG_U_ESCAPES
    assert pv.flags[1] & FLAG_U_ESCAPES and rig.doc.flags & FLAG_U_ESCAPES


# ----------------------------------------------------------------------------------
# duplicate keys, huge records, results over the limit, unreadable records
# ----------------------------------------------------------------------------------
def test_duplicate_key_records_are_conflicts_only_when_changed(rigs, backend) -> None:
    lines = [b'{"a": "Toshkent", "a": "x"}',           # dict: a = "x" -> changed -> duplicate -> conflict
             b'{"a": "x", "a": "Toshkent"}',           # dict: a = "Toshkent" -> no change, not checked
             b'{"b": {"k": 1, "k": 2}, "s": "x"}',     # nested duplicate
             b'{"a": 1, "\\u0061": 2, "s": "x"}',      # escaped duplicate
             b'{"s": "x", "t": {"u": "x"}}',
             b'[{"k": 1}, {"k": 1}, "x"]']             # equal keys in DIFFERENT objects: fine
    rig = rigs(b"\n".join(lines) + b"\n", open_kw={"format": "jsonl"})
    pv = check_rule(rig, BulkSpec("replace", {"find": "x", "replacement": "y"}), undo=False)
    assert pv.conflicts == 3 and pv.records_changed == 2
    assert [p.position for p in pv.problems] == [0, 2, 3]
    assert all(p.after == t("bulk.problem.duplicate_keys") and p.path == "$" for p in pv.problems)
    assert rig.records()[:4] == lines[:4]


def test_huge_records_and_results_over_the_limit(rigs, backend) -> None:
    vals = [{"i": i, "s": "Toshkent " + "x" * (i * 40)} for i in range(12)]
    rig = values_doc(rigs, vals, settings=Settings(large_node_bytes=300))
    sizes = [rig.doc.record_span(i)[1] - rig.doc.record_span(i)[0] for i in range(12)]
    huge = sum(1 for s in sizes if s > 300)
    assert 0 < huge < 12
    pv = check_rule(rig, BulkSpec("replace", {"find": "toshkent", "replacement": "T"}))
    assert pv.skipped_huge == huge and pv.records_changed == 12 - huge
    assert pv.warnings and str(huge) in pv.warnings[0]
    first = min(i for i, s in enumerate(sizes) if s > 300)
    assert f"#{first}" in pv.warnings[0]
    # a result that would become huge is not written (and counted as skipped)
    pv = check_rule(rig, BulkSpec("set_field", {"path": "pad", "value": json.dumps("p" * 150)}))
    assert pv.skipped_huge > huge and pv.records_changed == 12 - pv.skipped_huge
    # dedupe: huge records take part (their keys are streamed)
    check_dedupe(rig, BulkSpec("dedupe", {"key": "s"}))


def test_unreadable_record_is_a_failure(rigs, backend, monkeypatch) -> None:
    rig = values_doc(rigs, [{"s": "x"}, {"s": "y"}])
    real = codec.loads
    calls = [0]

    def flaky(data: Any) -> Any:
        calls[0] += 1
        if calls[0] == 1:
            raise codec.JsonDecodeError("nested too deeply")
        return real(data)

    monkeypatch.setattr(codec, "loads", flaky)
    pv = rig.preview(BulkSpec("replace", {"find": "y", "replacement": "z", "mode": "regex"}))
    assert pv.failures == 1 and pv.records_changed == 1
    assert pv.problems == [BulkExample(0, "$", "", t("bulk.problem.unreadable"))]
    pv.discard()


# ----------------------------------------------------------------------------------
# scopes
# ----------------------------------------------------------------------------------
def test_scopes_subsets_order_and_repeats(rigs, backend) -> None:
    rig = rigs("array", n=40, seed=3)
    rig.edit_some()
    spec = BulkSpec("replace", {"find": "o", "replacement": "0"})
    n = rig.state.count
    sub = filter_records(rig.doc.pinned(), "id >= 5")
    assert sub.gen == rig.state.gen and 0 < len(sub) < n
    check_rule(rig, spec, sub)
    # (gen-tagged scopes are made right before use: every apply/undo makes a new state)
    scopes = [lambda: [n - 1, 3, 3, 0, 17, 3], lambda: array("Q", [5, 2, 9]), lambda: (1, 2, 3),
              lambda: range(n - 1, 0, -3), lambda: range(2, 9), lambda: range(0), lambda: [],
              lambda: RecordSet.from_indices([9, 1, 1, 30]), lambda: RecordSet.base(rig.doc),
              lambda: RecordSet.base(rig.doc.pinned(None)),
              lambda: RecordSet.from_indices([0, 1, 2], gen=rig.state.gen), lambda: sort_scope(rig)]
    for make_scope in scopes:
        scope = make_scope()
        pv = check_rule(rig, spec, scope)
        assert pv.scanned == len(positions_of(scope, n))
        assert [e.position for e in pv.examples] == sorted(e.position for e in pv.examples)
    # a plain iterator (read once) with repeats
    want = reference(rig, spec, [1, 4])
    pv = rig.preview(spec, iter([4, 4, 1]))
    assert (pv.scanned, pv.records_changed, pv.values_changed) == (2, want["records_changed"],
                                                                   want["values_changed"])
    pv.discard()


def sort_scope(rig: Rig) -> RecordSet:
    from jsonreshape.recordset import sort_records

    rs = sort_records(rig.doc.pinned(), ["-id"])
    assert rs.gen == rig.state.gen and not rs.is_ascending()
    return rs


def test_stale_scopes_are_refused(rigs, backend) -> None:
    rig = rigs("jsonl", n=20)
    spec = BulkSpec("replace", {"find": "a", "replacement": "b"})
    plain = filter_records(rig.doc.pinned(), "id >= 0")      # made on the plain file: gen 0
    assert plain.gen == 0
    pv = rig.preview(spec, plain)                             # the initial state shows the file: accepted
    assert pv.scanned == 20
    pv.discard()
    rig.edit_some()
    files = sorted(p.name for p in rig.store.dir.iterdir())
    for scope in (plain, RecordSet.from_indices([1], gen=rig.state.gen + 1000), [0, rig.state.count],
                  RecordSet.from_indices([rig.state.count + 5]), [-1], (3, -2), range(rig.state.count + 1)):
        with pytest.raises(EditError) as ei:
            rig.preview(spec, scope)
        assert ei.value.key == "bulk.error.stale_scope"
    with pytest.raises(EditError) as ei:
        rig.preview(BulkSpec("dedupe", {"key": "id"}), plain)
    assert ei.value.key == "bulk.error.stale_scope"
    assert sorted(p.name for p in rig.store.dir.iterdir()) == files      # nothing created


# ----------------------------------------------------------------------------------
# the byte prefilter
# ----------------------------------------------------------------------------------
def clean_records(n: int, seed: int) -> list[dict[str, Any]]:
    rng = random.Random(seed)
    words = ["Toshkent", "Samarqand", "O'zbekiston", "Oʻzbekiston", "Buxoro", "shahar", "ko'cha", "Ўзбек", "zip"]
    out = []
    for i in range(n):
        rec: dict[str, Any] = {"id": i, "name": " ".join(rng.choice(words) for _ in range(3))}
        if rng.random() < 0.5:
            rec["contact"] = {"city": rng.choice(words), "zip": rng.randint(1, 9)}
        if rng.random() < 0.3:
            rec["city"] = rng.choice(words)
        out.append(rec)
    return out


@pytest.mark.parametrize("escapes", [False, True])
def test_prefilter_is_used_only_where_sound(rigs, backend, monkeypatch, escapes) -> None:
    recs = clean_records(60, 4)
    if escapes:
        recs[7]["q"] = 'quote " back \\ slash'        # FLAG_ESCAPES: the key needle is not sound any more
    rig = rigs(recs, "array")
    from jsonreshape.core import FLAG_ESCAPES, FLAG_U_ESCAPES
    assert bool(rig.doc.flags & FLAG_ESCAPES) == escapes and not rig.doc.flags & FLAG_U_ESCAPES
    real = codec.loads
    parsed = [0]

    def counting(data: Any) -> Any:
        parsed[0] += 1
        return real(data)

    monkeypatch.setattr(codec, "loads", counting)
    cases = [(BulkSpec("replace", {"find": "toshkent", "replacement": "T"}), True),
             (BulkSpec("replace", {"find": "o'zbekiston", "replacement": "UZ", "whole_word": True}), True),
             (BulkSpec("replace", {"find": "toshkent", "replacement": "T", "mode": "regex"}), False),
             (BulkSpec("rename_key", {"old": "city", "new": "shahar"}), not escapes),
             (BulkSpec("delete_field", {"path": "..zip"}), not escapes),
             (BulkSpec("delete_field", {"path": "contact.*"}), False),
             (BulkSpec("set_field", {"path": "city", "value": "1"}), False)]
    for spec, filtered in cases:
        parsed[0] = 0
        pv = rig.preview(spec)
        pv.discard()
        assert (parsed[0] < 60) == filtered, spec
        monkeypatch.setattr(codec, "loads", real)
        check_rule(rig, spec)
        monkeypatch.setattr(codec, "loads", counting)


# ----------------------------------------------------------------------------------
# dedupe
# ----------------------------------------------------------------------------------
def test_dedupe_rule(rigs, backend) -> None:
    vals = [{"id": 1, "v": "a"}, {"id": 1.0, "v": "b"}, {"id": "1"}, {"v": "no id"}, {"id": None}, {"id": None},
            {"id": {"x": 1, "y": [1]}}, {"id": {"y": [1], "x": 1.00}}, {"id": 2}]
    rig = values_doc(rigs, vals)
    for keep in ("first", "last"):
        check_dedupe(rig, BulkSpec("dedupe", {"key": "id", "keep": keep}))
    check_dedupe(rig, BulkSpec("dedupe", {"key": "id"}), [8, 7, 6, 1, 0])
    pv = check_dedupe(rig, BulkSpec("dedupe", {"key": "$key"}))
    assert pv.records_changed == 0 and pv.warnings
    keyed = rigs(b'{"k1": {"id": 1}, "k2": {"id": 1}, "k1": {"id": 2}, "k3": {"id": 1}}', open_kw={"format": "keyed"})
    pv = check_dedupe(keyed, BulkSpec("dedupe", {"key": "$key"}))
    assert pv.keys == [(keyed.state.rid_at(2), "k1", None)]
    check_dedupe(keyed, BulkSpec("dedupe", {"key": "id", "keep": "last"}))


# ----------------------------------------------------------------------------------
# make_step / discard / cleanup
# ----------------------------------------------------------------------------------
def test_make_step_fields_and_flags(rigs, backend, monkeypatch) -> None:
    rig = values_doc(rigs, [{"s": "a"}, {"s": "b"}, {"s": "a"}])
    pv = rig.preview(BulkSpec("replace", {"find": "a", "replacement": "Ўзбек \"q\""}))
    assert pv.flags[0] == rig.state.flags and pv.flags[1] != pv.flags[0]
    step = make_step(pv)
    assert step.push_layer == pv.layer and step.segments == [pv.segment] and step.flags == pv.flags
    assert step.label_params["count"] == 2 and step.label_params["kind"] == "replace"
    assert step.label_params["rule"]["find"] == "a"
    assert step.records == [0, 2] and step.struct is None and step.content == []
    assert step.label == t("edits.step.bulk.replace", count=2)
    pv.discard()
    with pytest.raises(EditError) as ei:
        make_step(pv)
    assert ei.value.key == "bulk.error.discarded"
    # unchanged flags -> None; more than RECORDS_MAX records -> None
    monkeypatch.setattr(bulk, "RECORDS_MAX", 1)
    pv = rig.preview(BulkSpec("replace", {"find": "b", "replacement": "c"}))
    assert pv.records == [1]
    pv.discard()
    pv = rig.preview(BulkSpec("replace", {"find": "a", "replacement": "c"}))
    assert pv.records is None
    step = make_step(pv)
    assert step.flags is None and step.records is None
    pv.discard()


def test_discard_removes_files_unless_committed(rigs, backend, monkeypatch) -> None:
    rig = values_doc(rigs, [{"s": "a"}] * 5)
    spec = BulkSpec("replace", {"find": "a", "replacement": "b"})
    pv = rig.preview(spec)
    seg, layer = rig.store.segment_path(pv.segment), rig.store.dir / pv.layer
    assert seg.exists() and layer.exists()
    pv.discard()
    pv.discard()                                   # idempotent
    assert not seg.exists() and not layer.exists()
    # a failed commit: the files are deleted by discard
    pv = rig.preview(spec)
    seg, layer = rig.store.segment_path(pv.segment), rig.store.dir / pv.layer
    step = make_step(pv)

    def boom(*a: Any, **k: Any) -> int:
        raise EditError("editstore.error.write", path="x", detail="test")

    monkeypatch.setattr(rig.store, "commit", boom)
    with pytest.raises(EditError):
        rig.store.commit(step, state=rig.state)
    monkeypatch.undo()
    assert step.seq == 0
    pv.discard()
    assert not seg.exists() and not layer.exists()
    # committed, undone and truncated by the next step: the store deletes the files, discard never does
    pv = rig.preview(spec)
    step, _ = rig.apply(pv)
    seg, layer = rig.store.segment_path(pv.segment), rig.store.dir / pv.layer
    rig.undo(step)
    rig.set(0, b'{"s":"c"}')
    pv.discard()
    assert not seg.exists() and not layer.exists()
    assert [rig.value_of(r) for r in rig.records()] == [{"s": "c"}] + [{"s": "a"}] * 4


def test_cancellation_deletes_everything(rigs, backend, monkeypatch) -> None:
    rig = rigs("jsonl", n=300, seed=2)
    monkeypatch.setattr(bulk, "APPEND_BYTES", 256)
    monkeypatch.setattr(bulk, "ENTRY_BATCH", 16)
    spec = BulkSpec("set_field", {"path": "x", "value": "1"})
    for phase in ("scan", "layer"):
        tok = CancelToken()
        seen: list[str] = []

        layer_msg = t("bulk.progress.layer", name=rig.doc.source_path.name)

        def cb(info: Any, phase: str = phase, tok: CancelToken = tok, layer_msg: str = layer_msg) -> None:
            seen.append(info.message)
            if phase == "scan" and info.done > 0:
                tok.cancel()
            if phase == "layer" and info.message == layer_msg:
                tok.cancel()

        prog = Progress(callback=cb, min_interval=0.0)
        with pytest.raises(Cancelled):
            rig.preview(spec, progress=prog, cancel=tok)
        assert (layer_msg in seen) == (phase == "layer")
        files = sorted(p.name for p in rig.store.dir.iterdir())
        assert not [f for f in files if f.startswith(("layer-", "bulk-")) or f.endswith(".tmp")], files
        assert [f for f in files if f.startswith("seg-")] in ([], ["seg-00000.dat"]), files
    pv = rig.preview(spec)
    assert pv.records_changed == 300
    pv.discard()


def test_dedupe_progress_and_cancellation(rigs, backend) -> None:
    rig = rigs("keyed", n=200, seed=4)
    name = rig.doc.source_path.name
    infos: list[Any] = []
    prog = Progress(callback=infos.append, min_interval=0.0)
    pv = rig.preview(BulkSpec("dedupe", {"key": "active"}), progress=prog)
    assert pv.records_changed == 198 and len(pv.keys) == 198
    messages = [i.message for i in infos]
    assert t("bulk.progress.scan", name=name) in messages and t("bulk.progress.keys", name=name) in messages
    assert prog.finished and prog.done == prog.total
    pv.discard()
    tok = CancelToken()
    tok.cancel()
    with pytest.raises(Cancelled):
        rig.preview(BulkSpec("dedupe", {"key": "active"}), cancel=tok)


def test_preview_in_a_thread_while_steps_are_committed(rigs, backend, monkeypatch) -> None:
    import threading

    rig = rigs("jsonl", n=400, seed=6)
    monkeypatch.setattr(bulk, "APPEND_BYTES", 300)          # many appends to the preview's segment
    spec = BulkSpec("set_field", {"path": "x", "value": '"Oʻzbekiston"'})
    state0 = rig.state
    ref = reference(rig, spec, state=state0)
    out: list[Any] = []
    th = threading.Thread(target=lambda: out.append(preview(rig.doc.pinned(state0), state0, rig.store, spec, None)))
    th.start()
    for i in range(20):                                     # point edits appended to segment 0 meanwhile
        rig.set(i, b'{"i":%d}' % i)
    th.join(60)
    pv = out[0]
    assert pv.gen == state0.gen != rig.state.gen
    assert (pv.records_changed, pv.values_changed, pv.bytes_new) == (ref["records_changed"], ref["values_changed"],
                                                                     ref["bytes_new"])
    from jsonreshape.overlay import ArrayLayer
    layer = ArrayLayer.open(rig.store.dir / pv.layer)       # the units read back exactly
    try:
        style = editops.detect_style(rig.doc)
        for pos, value in list(ref["new"].items())[:50]:
            span = layer.get(state0.rid_at(pos))
            assert rig.state.read(*span) == editops.format_record(codec.dumps(value), style)
    finally:
        layer.close()
    pv.discard()
    assert [rig.value_of(r) for r in rig.records()][:20] == [{"i": i} for i in range(20)]


def test_entry_spill_gives_the_same_layer(rigs, backend, monkeypatch) -> None:
    rig = rigs("array", n=120, seed=8)
    rig.edit_some()
    spec = BulkSpec("clean_strings", {"trim": True, "collapse_spaces": True})
    ref = check_rule(rig, spec)
    monkeypatch.setattr(bulk, "ENTRY_BATCH", 7)
    monkeypatch.setattr(bulk, "APPEND_BYTES", 100)
    pv = check_rule(rig, spec)
    assert (pv.records_changed, pv.values_changed, pv.bytes_new) == (ref.records_changed, ref.values_changed,
                                                                     ref.bytes_new)
    assert not [p for p in rig.store.dir.iterdir() if p.name.endswith(".tmp")]


DiskUsage = namedtuple("DiskUsage", "total used free")


def test_disk_space_refused_before_the_pass(rigs, backend, monkeypatch, lang_en) -> None:
    rig = rigs("array", n=30)
    monkeypatch.setattr(shutil, "disk_usage", lambda p: DiskUsage(10 << 30, 0, bulk.DISK_RESERVE + 10))
    with pytest.raises(EditError) as ei:
        rig.preview(BulkSpec("replace", {"find": "a", "replacement": "b"}))
    assert ei.value.key == "bulk.error.disk_space"
    assert "GB" in ei.value.message and ei.value.hint
    assert not rig.store.dir.exists() or not list(rig.store.dir.glob("seg-0000[1-9].dat"))
    # dedupe writes no units: not checked
    pv = rig.preview(BulkSpec("dedupe", {"key": "active"}))
    pv.discard()


def test_disk_space_checked_while_writing(rigs, backend, monkeypatch) -> None:
    rig = rigs("jsonl", n=200, seed=1)
    monkeypatch.setattr(bulk, "DISK_CHECK_BYTES", 2000)
    monkeypatch.setattr(bulk, "APPEND_BYTES", 500)
    free = [100 << 30]

    def usage(p: Any) -> DiskUsage:
        free[0] //= 4                        # 25 GiB, 6 GiB, 1.5 GiB, 0.4 GiB (< the 1 GiB reserve)
        return DiskUsage(1 << 40, 0, free[0])

    monkeypatch.setattr(shutil, "disk_usage", usage)
    with pytest.raises(EditError) as ei:
        rig.preview(BulkSpec("set_field", {"path": "x", "value": "1"}))
    assert ei.value.key == "bulk.error.disk_space"
    assert not list(rig.store.dir.glob("seg-0000[1-9].dat")) and not list(rig.store.dir.glob("layer-*"))


def test_file_changed_during_the_pass(rigs, backend) -> None:
    rig = rigs("jsonl", n=50)
    src = rig.doc.source_path
    data = src.read_bytes()
    src.write_bytes(data[:-10] + b"\n")
    with pytest.raises(FileChangedError):
        rig.preview(BulkSpec("set_field", {"path": "x", "value": "1"}))
    assert not list(rig.store.dir.glob("seg-0000[1-9].dat")) and not list(rig.store.dir.glob("layer-*"))


def test_progress_reaches_the_end(rigs, backend) -> None:
    rig = rigs("array", n=80)
    infos: list[Any] = []
    prog = Progress(callback=infos.append, min_interval=0.0)
    pv = rig.preview(BulkSpec("replace", {"find": "o", "replacement": "0"}), progress=prog)
    assert prog.finished and prog.done == prog.total > 0
    assert infos[0].message == t("bulk.progress.scan", name=rig.doc.source_path.name)
    assert any(i.message == t("bulk.progress.layer", name=rig.doc.source_path.name) for i in infos)
    dones = [i.done for i in infos]
    assert dones == sorted(dones)
    pv.discard()
    prog = Progress(callback=None)
    pv = rig.preview(BulkSpec("dedupe", {"key": "active"}), progress=prog)
    assert prog.finished and prog.done == prog.total
    pv.discard()


def test_preview_arguments(rigs, backend) -> None:
    rig = rigs("array", n=5)
    with pytest.raises(TypeError):
        preview(rig.doc, rig.state, rig.store, {"kind": "replace"}, None)      # type: ignore[arg-type]
    with pytest.raises(TypeError):
        preview(rig.doc, None, rig.store, BulkSpec("replace", {"find": "a"}), None)
    with pytest.raises(UserInputError):
        preview(rig.doc, rig.state, rig.store, BulkSpec("replace", {"find": ""}), None)
    # a live document or any view of it: re-pinned to the state given
    pv = preview(rig.doc, rig.state, rig.store, BulkSpec("replace", {"find": "o", "replacement": "0"}), None)
    assert pv.gen == rig.state.gen
    pv.discard()


def test_partial_documents_are_refused(tmp_path, backend) -> None:
    p = tmp_path / "bad.json"
    p.write_bytes(b'[{"a": 1}, {"a": 2}, {"a": ')
    doc = Document.open(p)
    doc.build_index()
    assert doc.error is not None
    store = EditStore.open(doc, dir=tmp_path / "layer")
    try:
        with pytest.raises(EditError) as ei:
            preview(doc, store.load_state(), store, BulkSpec("set_field", {"path": "a", "value": "1"}), None)
        assert ei.value.key == "bulk.error.partial"
    finally:
        store.close()
        doc.close()


def test_a_bad_unit_fails_the_check_and_leaves_nothing(rigs, backend, monkeypatch) -> None:
    rig = values_doc(rigs, [{"s": "a"}, {"s": "b"}, {"s": "a"}])
    def breaking(real: Any) -> Any:
        def broken(value: Any, **kw: Any) -> bytes:
            out = real(value, **kw)
            return out + b"}" if value == {"s": "c"} and not kw else out     # the changed record: invalid JSON
        return broken

    # the bulk runner serializes with codec.dumps_transient (results copied into the block at once)
    monkeypatch.setattr(codec, "dumps", breaking(codec.dumps))
    monkeypatch.setattr(codec, "dumps_transient", breaking(codec.dumps_transient))
    calls: list[int] = []
    real_check = editops.check_unit
    monkeypatch.setattr(editops, "check_unit", lambda u, *, member: calls.append(1) or real_check(u, member=member))
    with pytest.raises(EditError) as ei:
        rig.preview(BulkSpec("replace", {"find": "b", "replacement": "c"}))
    assert ei.value.key == "edits.error.internal"
    assert calls                                      # the batch check failed -> unit by unit
    assert not list(rig.store.dir.glob("seg-0000[1-9].dat")) and not list(rig.store.dir.glob("layer-*"))
    monkeypatch.undo()
    pv = check_rule(rig, BulkSpec("replace", {"find": "a", "replacement": "c"}))  # one pass, no fallback
    assert pv.records_changed == 2


def test_an_edited_wrapper_is_left_alone(rigs, backend) -> None:
    rig = rigs(jsongen.records(10, seed=5), "wrapped")
    w = '{"meta":{"v":2,"who":"Oʻzbekiston"},"data":[],"extra":"Toshkent"}'.encode("utf-8")
    span = rig.store.append(w)
    hole_start = span[0] + w.index(b"[]")
    rig.commit(Step("edit", "edits.step.set_value", {}, wrapper=(rig.state.wrapper, span, rig.state.wrapper_hole,
                                                                  (hole_start, hole_start + 2))))
    prefix, suffix = rig.doc.pinned().wrapper_spans()
    pv = check_rule(rig, BulkSpec("replace", {"find": "toshkent", "replacement": "T"}), undo=False)
    assert pv.records_changed > 0
    assert rig.doc.pinned().wrapper_spans() == (prefix, suffix)
    assert rig.state.read(*rig.state.wrapper) == w


def test_dynamic_message_keys_exist() -> None:
    from jsonreshape.i18n import MESSAGES

    keys = [f"bulk.kind.{k}" for k in bulk.KINDS] + [f"edits.step.bulk.{k}" for k in bulk.KINDS]
    keys += [f"bulk.label.mode.{m}" for m in ("always", "if_missing", "if_exists")]
    keys += [f"bulk.label.keep.{k}" for k in ("first", "last")] + [f"edits.type.{x}" for x in editops.JSON_TYPES]
    for key in keys:
        assert MESSAGES[key]["uz"] and MESSAGES[key]["en"], key


@pytest.mark.parametrize("kind", ["array", "keyed"])
def test_an_applied_bulk_step_survives_a_reopen_and_undo(rigs, backend, kind) -> None:
    rig = rigs(kind, n=30, seed=9)
    rig.edit_some()
    pv = rig.preview(BulkSpec("set_field", {"path": "contact.address.country", "value": '"UZ"'}))
    step, before_state = rig.apply(pv)
    after = rig.records()
    dd = rig.preview(BulkSpec("dedupe", {"key": "active"}))
    step2, _ = rig.apply(dd)
    final = rig.records()
    assert len(final) == len(after) - dd.records_changed
    layer_dir = rig.store.dir
    rig.store.close()                                  # the "crash": the layer is reopened from disk
    rig.store = EditStore.open(rig.doc, dir=layer_dir)
    state = rig.store.load_state()
    assert rig.records(state) == final
    rig.state = state
    rig.doc.set_overlay(state)
    rig.undo(rig.store.step(rig.store.head))
    assert rig.records() == after
    rig.undo(rig.store.step(rig.store.head))
    assert rig.records() == rig.records(before_state)


@pytest.mark.parametrize("kind", ["array", "keyed", "jsonl"])
def test_through_the_edit_session(tmp_path, backend, kind) -> None:
    """The session's protocol (§12.5): bulk_preview -> bulk_apply -> undo / redo."""
    edits = pytest.importorskip("jsonreshape.edits")
    doc = make_source(tmp_path / "src.json", kind, n=20)
    session = edits.EditSession.open(doc)
    try:
        def records() -> list[bytes]:
            view = doc.pinned()
            return [view.record_bytes(i) for i in range(view.record_count)]

        before = records()
        scope = filter_records(doc.pinned(), "id >= 3")
        pv = session.bulk_preview(BulkSpec("replace", {"find": "toshkent", "replacement": "T"}), scope)
        cs = session.bulk_apply(pv)
        assert cs.kind == "bulk" and cs.label == t("edits.step.bulk.replace", count=pv.values_changed)
        after = records()
        changed = sorted(i for i in range(len(before)) if before[i] != after[i])
        assert len(changed) == pv.records_changed > 0 and sorted(cs.records) == changed
        pv.discard()                                   # files of the applied step are kept
        assert records() == after
        stale = session.bulk_preview(BulkSpec("set_field", {"path": "x", "value": "1"}))
        dd = session.bulk_preview(BulkSpec("dedupe", {"key": "active"}))
        cs = session.bulk_apply(dd)
        assert cs.remap is not None and doc.pinned().record_count == len(before) - dd.records_changed
        with pytest.raises(EditError):
            session.bulk_apply(stale)                  # computed on an older state
        stale.discard()
        session.undo()
        session.undo()
        assert records() == before
        session.redo()
        assert records() == after
    finally:
        session.close()
        doc.close()


def test_plain_scope_and_dedupe_on_an_unedited_state(rigs, backend) -> None:
    rig = rigs(b'{"a": {"id": 1}, "b": {"id": 1}, "c": {"id": 2}, "d": {"id": 2}, "e": {"id": 3}}',
               open_kw={"format": "keyed"})
    plain = filter_records(rig.doc.pinned(), "id <= 2")
    assert plain.gen == 0 and rig.state.gen != 0
    pv = check_dedupe(rig, BulkSpec("dedupe", {"key": "id", "keep": "last"}), plain)
    assert list(pv.positions) == [0, 2] and [k for _r, k, _n in pv.keys] == ["a", "c"]
    plain2 = filter_records(rig.doc.pinned(None), "id <= 2")
    pv = rig.preview(BulkSpec("replace", {"find": "x", "replacement": "y", "mode": "regex"}), plain2)
    assert pv.scanned == 4 and pv.records_changed == 0
    pv.discard()


# ----------------------------------------------------------------------------------
# encodings and styles
# ----------------------------------------------------------------------------------
@pytest.mark.parametrize("enc", [{"encoding": "utf-8", "bom": True}, {"encoding": "utf-16-le", "bom": True},
                                 {"encoding": "utf-16-be"}, {"gz": True}, {"crlf": True}, {"style": "pretty4"},
                                 {"style": "tab"}, {"style": "escaped"}, {"style": "messy"}])
def test_encodings_and_styles(rigs, backend, enc) -> None:
    kw = dict(enc)
    recs = jsongen.records(30, seed=11)
    if kw.pop("crlf", False):    # a pretty file written with CRLF line ends throughout
        data = jsongen.build_document(recs, "array", style="pretty2", newline="\n").replace(b"\n", b"\r\n")
        rig = rigs(data, open_kw={"format": "array"})
    else:
        name = "src.json.gz" if kw.get("gz") else "src.json"
        rig = rigs(recs, "array", name=name, **kw)
    style = editops.detect_style(rig.doc)
    check_rule(rig, BulkSpec("replace", {"find": "toshkent", "replacement": "Tashkent"}))
    pv = check_rule(rig, BulkSpec("set_field", {"path": "contact.address.country", "value": '"UZ"'}), undo=False)
    raw = rig.records()[pv.examples[0].position]
    if "crlf" in enc:
        assert style.newline == b"\r\n" and style.unit == "  "
        assert b"\r\n" in raw and b"\n" not in raw.replace(b"\r\n", b"")
    if style.unit is None:
        assert b"\n" not in raw
    else:
        assert raw.count(style.newline) > 3
