"""Document with an overlay (docs/ARCHITECTURE.md §12.3): the edit-aware reader.

The oracle: an edited document must behave exactly like a plain document of the expected content
(``tests/overlay_helpers.py`` builds the overlay state from Python-level edits AND the plain file).
For every document kind (the 7 kinds + a wrapped document with an object container), both core
backends and several edit scenarios (point edits, inserts, deletes, moves, duplicates, an array
layer over every 3rd record, renamed record keys, an edited wrapper): record APIs, ``iter_blocks``
for every kind of source (indices, data, spans, ``contiguous`` only over consecutive pure base
records, huge blocks with a tiny ``large_node_bytes``), ``iter_records`` / ``iter_record_bytes``,
Node trees (values, children, keys, ``member_span``, composite nodes, ``node_chunks``), path lookups
(positions, keys -- last match wins --, the ``key_index`` hook). Plus pinned views (frozen states,
several threads while the live state is swapped), the ``VirtualReader``, damaged layers
(``edits.error.layer_corrupt``, never ``FileChangedError``), ``set_overlay`` checks, child caches of
edited nodes (never persisted), flags, and forced jsonl/stream on a whitespace-only file.
"""

from __future__ import annotations

import io
import os
import random
import threading
from array import array
from pathlib import Path
from typing import Any

import pytest

from jsonreshape import codec, editops, reader
from jsonreshape.core import FLAG_ESCAPES, FLAG_U_ESCAPES
from jsonreshape.errors import EditError, FileChangedError, FileFormatError, TooLargeError
from jsonreshape.fileio import read_range
from jsonreshape.overlay import EDIT_BASE, OverlayState, PieceTable, edit_offset, is_edit_offset
from jsonreshape.reader import Document, Node, PinnedDocument, VirtualReader
from jsonreshape.recordset import RecordSet
from jsonreshape.settings import Settings
from jsonreshape.stream import stream_values
from jsonreshape.paths import parse_pattern
from overlay_helpers import (
    KINDS,
    SCENARIOS,
    OverlayBuilder,
    base_records,
    expected_text,
    make_edited,
    make_source,
    new_record,
    open_expected,
)

# ----------------------------------------------------------------------------------
# fixtures and builders
# ----------------------------------------------------------------------------------
@pytest.fixture
def backend(core_backend):
    name = "rust" if core_backend.__name__.endswith("_rscore") else "python"
    old = reader.set_scanner_backend(name)
    old_ops = editops.set_core_backend(name)
    yield name
    reader.set_scanner_backend(old)
    editops.set_core_backend(old_ops)


@pytest.fixture
def docs():
    opened: list[Document] = []
    yield opened
    for d in opened:
        d.close()


def open_source(tmp_path: Path, docs: list, kind: str, n: int = 24, seed: int = 0, style: str = "compact",
                settings: Settings | None = None, name: str = "src.json") -> Document:
    doc = make_source(tmp_path / name, kind, n=n, seed=seed, style=style, settings=settings)
    docs.append(doc)
    return doc


def edited(tmp_path: Path, docs: list, kind: str, scenario: str, *, n: int = 24, seed: int = 0,
           style: str = "compact", settings: Settings | None = None) -> tuple[Document, OverlayBuilder, Document]:
    """(edited document, its builder, the plain document of the expected content)."""
    doc, ob, exp = make_edited(tmp_path, kind, scenario, n=n, seed=seed, style=style, settings=settings)
    docs.extend((doc, exp))
    return doc, ob, exp


def raw_at(doc: Document, span: tuple[int, int]) -> bytes:
    with doc.open_data() as f:
        return read_range(f, *span)


def block_records(doc: Document, blocks: Any) -> list[tuple[int, bytes, bool]]:
    """(index, record bytes, huge) of every record of the blocks, checking the block invariants."""
    out = []
    for blk in blocks:
        sp = array("Q")
        sp.frombytes(blk.spans)
        assert len(sp) == 2 * len(blk.indices)
        if blk.huge:
            assert len(blk.indices) == 1 and blk.data == b"" and not blk.contiguous
            out.append((int(blk.indices[0]), raw_at(doc, (sp[0], sp[1])), True))
            continue
        assert all(sp[2 * k] <= sp[2 * k + 1] for k in range(len(blk.indices)))
        assert all(sp[2 * k + 1] <= sp[2 * k + 2] for k in range(len(blk.indices) - 1)), "spans sorted"
        if blk.contiguous:
            assert blk.base >= 0 and not is_edit_offset(blk.base)
            if reader._edited_state(doc.overlay):   # (the file's own blocks may skip unselected records)
                rids = [doc.record_rid(int(i)) for i in blk.indices]
                assert not any(over for _r, over in rids), "contiguous blocks hold pure base records only"
                order = sorted(set(r for r, _o in rids))
                assert order == list(range(order[0], order[0] + len(order))) if order else True
        for k, i in enumerate(blk.indices):
            out.append((int(i), blk.data[sp[2 * k]:sp[2 * k + 1]], False))
    return out


class FakeSeq:
    """Sequence-like source without iter_chunks."""

    def __init__(self, items: list[int]) -> None:
        self.items = items

    def __len__(self) -> int:
        return len(self.items)

    def __getitem__(self, i: int) -> int:
        return self.items[i]


# ----------------------------------------------------------------------------------
# the oracle
# ----------------------------------------------------------------------------------
def assert_records_equal(doc: Document, exp: Document, expected: list[bytes]) -> None:
    n = doc.record_count
    assert n == exp.record_count == len(expected)
    large = doc.settings.large_node_bytes
    for i in range(n):
        span = doc.record_span(i)
        assert raw_at(doc, span) == expected[i]
        assert doc.is_huge(i) == exp.is_huge(i) == (len(expected[i]) > large)
        if len(expected[i]) <= large:
            assert doc.record_bytes(i) == exp.record_bytes(i) == expected[i]
            assert doc.record(i) == exp.record(i)
        else:
            with pytest.raises(TooLargeError):
                doc.record_bytes(i)
        assert doc.record_key(i) == exp.record_key(i)
        assert raw_at(doc, doc.record_value_span(i)) == raw_at(exp, exp.record_value_span(i))
    with pytest.raises(IndexError):
        doc.record_span(n)
    with pytest.raises(IndexError):
        doc.record_span(-1)


def assert_blocks_equal(doc: Document, exp: Document, rng: random.Random) -> None:
    n = doc.record_count
    for block_bytes in (64, 700, 1 << 20):
        got = block_records(doc, doc.iter_blocks(None, block_bytes=block_bytes))
        want = block_records(exp, exp.iter_blocks(None, block_bytes=block_bytes))
        assert [(i, b) for i, b, _h in got] == [(i, b) for i, b, _h in want]
        assert [h for *_x, h in got] == [h for *_x, h in want]
        if n:
            sources: list[Any] = [
                range(1, max(1, n - 1)),
                [rng.randrange(n) for _ in range(2 * n)],
                list(range(n - 1, -1, -2)),
                RecordSet.from_indices([rng.randrange(n) for _ in range(n)]),
                FakeSeq(sorted(rng.sample(range(n), n // 2))),
                iter(list(range(0, n, 3))),
            ]
            for src in sources:
                if isinstance(src, type(iter([]))):
                    items = list(src)
                    a, b = iter(items), iter(items)
                else:
                    a = b = src
                g = block_records(doc, doc.iter_blocks(a, block_bytes=block_bytes))
                w = block_records(exp, exp.iter_blocks(b, block_bytes=block_bytes))
                assert [(i, x) for i, x, _h in g] == [(i, x) for i, x, _h in w]
    assert list(doc.iter_record_bytes(include_huge=True)) == list(exp.iter_record_bytes(include_huge=True))
    assert list(doc.iter_records()) == list(exp.iter_records())


def node_sig(node: Node, depth: int) -> list[Any]:
    doc = node.doc
    large = doc.settings.large_node_bytes
    sig: list[Any] = [node.path, node.kind, node.key, node.is_composite, node.role]
    if node.mstart is not None:
        head = raw_at(doc, (node.mstart, node.start))   # '"key": ' (the record container's text may differ in
        sig.append(head.split(b":")[0])                 # layout between the files: compared by value below)
        if not node.is_composite:
            sig.append(raw_at(doc, node.member_span()))
    if node.is_container:
        cc = node.child_count()
        sig.append(cc)
        if depth > 0 and cc:
            idx = sorted({*range(min(cc, 5)), *range(max(0, cc - 2), cc)})
            kids = [node.child(i) for i in idx]
            sig.append([node_sig(k, depth - 1) for k in kids])
            assert [k.key for k in node.children(0, min(cc, 5))] == [k.key for k in kids[:min(cc, 5)]]
    text = b"".join(doc.node_chunks(node, chunk=97))
    if node.is_composite:
        sig.append(codec.loads(text) if len(text) <= 1 << 20 else len(text))
    else:
        assert text == raw_at(doc, (node.start, node.end))
        sig.append(text)
        if node.size <= large:
            sig.append(node.value())
            sig.append(node.scalar_text(40))
            assert node.raw() == text
    return sig


def assert_nodes_equal(doc: Document, exp: Document) -> None:
    root, eroot = doc.root(), exp.root()
    assert node_sig(root, 3) == node_sig(eroot, 3)
    if doc.kind != "single" and root.is_composite and doc.effective_bytes() < doc.settings.large_node_bytes:
        assert root.value() == eroot.value()
        assert codec.loads(root.raw()) == codec.loads(eroot.raw()) if doc.kind not in ("jsonl", "stream",
                                                                                          "brace_list") else True
        assert root.scalar_text(30).startswith(("[", "{"))
    n = doc.record_count
    if doc.kind == "single" or not n:
        return
    rp = tuple(doc.record_path or ())
    for i in sorted({0, n // 2, n - 1}):
        a, b = doc.record_node(i), exp.record_node(i)
        assert node_sig(a, 2) == node_sig(b, 2)
        seg = a.key
        assert node_sig(doc.node(rp + (seg,)), 1) == node_sig(exp.node(rp + (seg,)), 1)
        if a.is_container and a.child_count():
            first = a.child(0)
            assert node_sig(doc.node(rp + (seg, first.key)), 0) == node_sig(exp.node(rp + (seg, first.key)), 0)
    records = doc.records_node()
    assert records is not None and records.child_count() == n
    assert [k.key for k in records.children(0, 4)] == [k.key for k in exp.records_node().children(0, 4)]


# ----------------------------------------------------------------------------------
# every kind, both backends, every scenario
# ----------------------------------------------------------------------------------
@pytest.mark.parametrize("scenario", SCENARIOS)
@pytest.mark.parametrize("kind", KINDS)
def test_edited_document_equals_plain_document(tmp_path, docs, backend, kind, scenario) -> None:
    seed = KINDS.index(kind) * 10 + SCENARIOS.index(scenario)
    style = "pretty2" if seed % 2 else "compact"
    doc, ob, exp = edited(tmp_path, docs, kind, scenario, seed=seed, style=style)
    assert doc.edited == (ob.steps > 0)
    assert doc.gen == doc.overlay.gen and doc.base_record_count == ob.base_count
    assert_records_equal(doc, exp, ob.records)
    assert_blocks_equal(doc, exp, random.Random(seed))
    assert_nodes_equal(doc, exp)
    if kind == "wrapped" or kind == "wrapped_obj":
        prefix, suffix = doc.wrapper_parts()
        eprefix, esuffix = exp.wrapper_parts()
        assert prefix.strip() == eprefix.strip() and suffix.strip() == esuffix.strip()


@pytest.mark.parametrize("kind", KINDS)
def test_huge_records_in_both_spaces(tmp_path, docs, backend, kind) -> None:
    settings = Settings(large_node_bytes=50 if kind == "single" else 230)
    doc, ob, exp = edited(tmp_path, docs, kind, "mixed", seed=5, settings=settings)
    large = settings.large_node_bytes
    assert any(len(r) > large and is_edit_offset(doc.record_span(i)[0]) for i, r in enumerate(ob.records))
    if kind != "single":
        assert any(len(r) <= large for r in ob.records)
        assert any(len(r) > large and not is_edit_offset(doc.record_span(i)[0]) for i, r in enumerate(ob.records))
    assert_records_equal(doc, exp, ob.records)
    assert_blocks_equal(doc, exp, random.Random(5))
    member = doc.member_records
    patterns = [parse_pattern(p) for p in ("id", "name", "orders[*].oid", "$")]
    for i, rec in enumerate(ob.records):
        if len(rec) > large:
            got = stream_values(doc, doc.record_span(i), patterns, member=member)
            want = stream_values(exp, exp.record_span(i), patterns, member=member)
            assert got == want


def test_every_third_record_in_an_array_layer_stays_block_based(tmp_path, docs, backend) -> None:
    doc = open_source(tmp_path, docs, "array", n=3000, seed=3)
    ob = OverlayBuilder(doc, tmp_path / "layer")
    ob.bulk_every(3, lambda p, old: old.replace(b'"id":', b'"id" :', 1))
    doc.set_overlay(ob.state())
    blocks = list(doc.iter_blocks(None, block_bytes=64 * 1024))
    assert len(blocks) < 3000 // 50, "no per-record blocks"
    assert sum(len(b.indices) for b in blocks) == 3000
    assert [b for _i, b, _h in block_records(doc, blocks)] == ob.records
    # a few edits: long pure runs are contiguous file ranges
    doc2 = open_source(tmp_path, docs, "array", n=3000, seed=3, name="src2.json")
    ob2 = OverlayBuilder(doc2, tmp_path / "layer2")
    ob2.set(1500, b'{"id": 1500}')
    doc2.set_overlay(ob2.state())
    blocks2 = list(doc2.iter_blocks(None))
    assert [len(b.indices) for b in blocks2 if not b.contiguous] == [1]
    assert sum(b.contiguous for b in blocks2) >= 2


@pytest.mark.parametrize("kind", [k for k in KINDS if k != "single"])
def test_every_record_deleted_or_none_left_then_inserted(tmp_path, docs, backend, kind) -> None:
    doc = open_source(tmp_path, docs, kind, n=6, seed=4)
    ob = OverlayBuilder(doc, tmp_path / "layer")
    ob.delete(*range(6))
    doc.set_overlay(ob.state())
    assert doc.record_count == 0 and doc.base_record_count == 6 and doc.edited
    assert list(doc.iter_blocks()) == [] and list(doc.iter_blocks(range(0))) == []
    with pytest.raises(IndexError):
        list(doc.iter_blocks([0]))
    exp = open_expected(doc, [], tmp_path / "expected.json", wrapper=None)
    docs.append(exp)
    assert exp.record_count == 0
    root = doc.root()
    records = doc.records_node()
    assert records.child_count() == 0 and records.children() == []
    assert codec.loads(b"".join(doc.node_chunks(root))) == exp.root().value()
    assert root.value() == exp.root().value() and records.value() == exp.records_node().value()
    assert doc.effective_bytes() == 0
    # then a new record into the empty document
    one = new_record(doc, random.Random(1), 3)
    ob.insert(0, one)
    doc.set_overlay(ob.state())
    assert doc.record_count == 1 and doc.record_bytes(0) == one and doc.record_rid(0) == (6, True)
    assert [b for _i, b in doc.iter_record_bytes()] == [one]


def test_progress_and_composite_size_of_an_edited_document(tmp_path, docs, monkeypatch) -> None:
    doc = open_source(tmp_path, docs, "array", n=300, seed=8)
    ob = OverlayBuilder(doc, tmp_path / "layer")
    ob.bulk_every(4, lambda p, old: b'{"p": %d}' % p)
    ob.insert(10, b'{"new": true}')
    state = ob.state()
    doc.set_overlay(state)
    infos: list[Any] = []
    from jsonreshape.tasks import Progress

    progress = Progress(callback=infos.append, min_interval=0)
    n = sum(len(b.indices) for b in doc.iter_blocks(None, block_bytes=4096, progress=progress))
    assert n == 301 and progress.finished
    assert progress.total == doc.effective_bytes() and all(i.done <= i.total for i in infos if i.total)
    # a big state that was not measured yet: Node.size scales the file's extent (no pass on the GUI thread)
    monkeypatch.setattr(reader, "EFF_EXACT_MAX", 10)
    doc2 = open_source(tmp_path, docs, "array", n=300, seed=8, name="src2.json")
    ob2 = OverlayBuilder(doc2, tmp_path / "layer2")
    ob2.bulk_every(4, lambda p, old: b'{"p": %d}' % p)
    doc2.set_overlay(ob2.state())
    root = doc2.root()
    estimate = root.size
    assert estimate == doc2._base_extent() + 2
    exact = doc2.effective_bytes()   # measured (and cached) now
    assert root.size == exact + 2 and exact < doc2._base_extent()
    assert root.value() == [codec.loads(r) for r in ob2.records]


def test_effective_bytes_rid_and_state_properties(tmp_path, docs) -> None:
    doc = open_source(tmp_path, docs, "array", n=10)
    assert doc.overlay is None and doc.gen == 0 and not doc.edited and doc.origin is doc
    assert doc.effective_bytes() == doc.record_span(9)[1] - doc.record_span(0)[0]
    assert doc.record_rid(3) == (3, False)
    with pytest.raises(IndexError):
        doc.record_rid(10)
    ob = OverlayBuilder(doc, tmp_path / "layer")
    base = base_records(doc)
    ob.set(2, b'{"x": 1}').insert(0, b'{"y": 22}').delete(9)
    state = ob.state()
    doc.set_overlay(state)
    assert doc.overlay is state and doc.gen == state.gen and doc.edited
    assert doc.base_record_count == 10 and doc.record_count == 10
    assert doc.record_rid(0) == (10, True) and doc.record_rid(1) == (0, False) and doc.record_rid(3) == (2, True)
    pure = [b for i, b in enumerate(base) if i not in (2, 8)]
    s = [doc.record_span(i) for i in range(10)]
    extent = sum(len(b) for b in [b'{"x": 1}', b'{"y": 22}'])
    assert doc.effective_bytes() >= extent + sum(len(b) for b in pure)
    # an initial (unedited) state reads like the file
    doc.set_overlay(OverlayState.initial(10, None))
    assert not doc.edited and doc.record_count == 10 and doc.record_span(4) == s[5]
    doc.set_overlay(None)
    assert doc.overlay is None and doc.record_span(4) == s[5]


def test_flags_cover_edited_content(tmp_path, docs) -> None:
    p = tmp_path / "plain.json"
    p.write_bytes(b'[{"a": 1}, {"a": "o\xca\xbbzbek"}, {"a": 3}, {"a": 4}, {"a": 5}]')
    doc = Document.open(p)
    docs.append(doc)
    doc.build_index()
    assert not doc.flags & (FLAG_ESCAPES | FLAG_U_ESCAPES)
    base_flags = doc.flags
    ob = OverlayBuilder(doc, tmp_path / "layer")
    ob.set(1, b'{"s": "\\u00e9t\\u00e9"}')
    state = ob.state()
    assert state.flags & FLAG_U_ESCAPES
    doc.set_overlay(state)
    assert doc.flags == base_flags | state.flags and doc.flags & FLAG_ESCAPES
    assert doc.pinned(None).flags == base_flags   # the file's own flags
    doc.flags = base_flags                       # the index build's writer keeps working
    assert doc.flags == base_flags | state.flags


# ----------------------------------------------------------------------------------
# keyed lookups and the key_index hook
# ----------------------------------------------------------------------------------
class FakeKeyIndex:
    def __init__(self, mapping: dict[str, int], gen: int | None = None) -> None:
        self.mapping = mapping
        self.calls: list[str] = []
        if gen is not None:
            self.gen = gen

    def find(self, key: str) -> int | None:
        self.calls.append(key)
        return self.mapping.get(key)


def test_key_lookup_scans_effective_keys_and_uses_the_key_index(tmp_path, docs, backend) -> None:
    doc, ob, exp = edited(tmp_path, docs, "keyed", "keys", seed=3)
    keys = [doc.record_key(i) for i in range(doc.record_count)]
    for key in set(keys):
        last = max(i for i, k in enumerate(keys) if k == key)
        node = doc.node((key,))
        assert node.start == doc.record_node(last).start and node.mstart == doc.record_span(last)[0]
        assert node_sig(node, 1) == node_sig(exp.node((key,)), 1)
    assert doc.root().find_child("missing!") is None
    # the hook: used for the state it describes
    ki = FakeKeyIndex({"zzz": 2})
    doc.key_index = ki
    assert doc.node(("zzz",)).start == doc.record_node(2).start and ki.calls == ["zzz"]
    view = doc.pinned()
    assert view.node(("zzz",)).start == doc.record_node(2).start and ki.calls == ["zzz", "zzz"]
    old = doc.pinned(None)   # another state: the keys are scanned
    assert old.root().find_child_index("zzz") is None and ki.calls == ["zzz", "zzz"]
    doc.key_index = FakeKeyIndex({"zzz": 4}, gen=doc.gen)
    assert doc.root().find_child_index("zzz") == 4
    doc.key_index = FakeKeyIndex({"zzz": 4}, gen=doc.gen + 1000)   # another state's index
    assert doc.root().find_child_index("zzz") is None


def test_wrapped_with_edited_wrapper(tmp_path, docs, backend) -> None:
    doc, ob, exp = edited(tmp_path, docs, "wrapped", "wrapper", seed=4)
    root = doc.root()
    assert root.is_edited and root.role == "ancestor" and root.start == ob.wrapper[0]
    (ps, pe), (ss, se) = doc.wrapper_spans()
    assert (ps, se) == ob.wrapper and (pe, ss) == ob.wrapper_hole
    assert doc._container_bounds() == ob.wrapper_hole
    kids = root.children()
    assert [k.key for k in kids] == ["meta", "data", "extra", "tail"]
    records = kids[1]
    assert records.role == "records" and records.is_composite and records.child_count() == doc.record_count
    assert [c.key for c in records.children(0, 3)] == [0, 1, 2]
    assert doc.node(("extra", "added")).value() is True
    assert doc.node(("meta", "esc")).value() == "A"
    assert doc.node(("data", 1)).start == doc.record_span(1)[0]
    assert root.value() == exp.root().value()
    text = b"".join(doc.node_chunks(root))
    assert codec.loads(text) == codec.loads(expected_text(doc, ob.records, wrapper=ob.wrapper_text))
    # the file's wrapper is still there for the base view
    base = doc.pinned(None)
    assert not base.root().is_edited and base.wrapper_spans()[0][0] == base.info.data_start


# ----------------------------------------------------------------------------------
# pinned views
# ----------------------------------------------------------------------------------
def test_pinned_views_keep_their_state(tmp_path, docs, backend) -> None:
    doc = open_source(tmp_path, docs, "array", n=12, seed=9)
    base = base_records(doc)
    v0 = doc.pinned()
    assert isinstance(v0, PinnedDocument) and v0.overlay is None and v0.origin is doc and v0.gen == 0
    assert v0.pinned() is v0 and v0.pinned(None) is v0
    ob = OverlayBuilder(doc, tmp_path / "layer")
    ob.set(0, b'{"a": 1}').delete(5)
    s1 = ob.state()
    doc.set_overlay(s1)
    v1 = doc.pinned()
    assert v1.overlay is s1 and v1.record_count == 11 and v1.pinned() is v1
    ob.insert(0, b'{"b": 2}')
    s2 = ob.state()
    doc.set_overlay(s2)
    assert doc.record_count == 12 and v1.record_count == 11 and v0.record_count == 12
    assert [v1.record_bytes(i) for i in range(11)] == [b'{"a": 1}'] + base[1:5] + base[6:]
    assert [v0.record_bytes(i) for i in range(12)] == base
    assert [b for _i, b in v1.iter_record_bytes()] == [v1.record_bytes(i) for i in range(11)]
    assert doc.record_bytes(0) == b'{"b": 2}'
    assert v1.pinned(s2).overlay is s2 and v1.pinned(None).overlay is None
    # shared live state: index, closed, settings, caches
    assert v1.index_complete and v1.settings is doc.settings and v1.info is doc.info
    v1.close()   # a no-op
    assert not doc.closed and v1.record_bytes(0) == b'{"a": 1}'
    with pytest.raises(TypeError):
        v1.set_overlay(None)
    with pytest.raises(TypeError):
        PinnedDocument()
    assert "PinnedDocument" in repr(v1) and f"gen={s1.gen}" in repr(v1)
    # writes go to the live document
    v1._trimmed_end_cache = None
    assert "_trimmed_end_cache" not in vars(v1)
    doc.close()
    with pytest.raises(ValueError):
        v1.record_bytes(1)


def test_pinned_views_across_overlay_swaps_from_several_threads(tmp_path, docs, backend) -> None:
    doc = open_source(tmp_path, docs, "jsonl", n=400, seed=2)
    obs = []
    states = []
    for k in range(4):
        ob = OverlayBuilder(doc, tmp_path / f"layer{k}")
        ob.bulk_every(k + 2, lambda p, old, k=k: b'{"v": %d, "p": %d}' % (k, p))
        for _ in range(k):
            ob.delete(0)
        obs.append(ob)
        states.append(ob.state())
    errors: list[BaseException] = []
    stop = threading.Event()

    def reader_thread(k: int) -> None:
        try:
            while not stop.is_set():
                view = doc.pinned(states[k])
                got = [b for _i, b in view.iter_record_bytes(block_bytes=4096)]
                assert got == obs[k].records
                assert view.record_count == len(obs[k].records)
        except BaseException as exc:  # pragma: no cover - reported below
            errors.append(exc)

    threads = [threading.Thread(target=reader_thread, args=(k,)) for k in range(4)]
    for th in threads:
        th.start()
    rng = random.Random(1)
    for _ in range(200):
        doc.set_overlay(rng.choice(states + [None]))
        live = doc.pinned()
        want = obs[states.index(live.overlay)].records if live.overlay is not None else base_records(doc)
        assert [b for _i, b in live.iter_record_bytes()] == want
    stop.set()
    for th in threads:
        th.join(30)
    assert not errors, errors[:1]


# ----------------------------------------------------------------------------------
# the virtual reader, damaged layers, set_overlay checks
# ----------------------------------------------------------------------------------
def test_virtual_reader(tmp_path, docs) -> None:
    doc = open_source(tmp_path, docs, "array", n=6)
    ob = OverlayBuilder(doc, tmp_path / "layer")
    ob.set(1, b'{"edited": true}')
    doc.set_overlay(ob.state())
    s, e = doc.record_span(1)
    assert is_edit_offset(s)
    with doc.open_data() as f:
        assert isinstance(f, VirtualReader) and f.readable() and f.seekable() and not f.writable()
        with pytest.raises(io.UnsupportedOperation):
            f.fileno()
        assert f.read(1) == b"["
        assert f.tell() == 1
        f.seek(s)
        assert f.read(e - s) == b'{"edited": true}' and f.tell() == e
        f.seek(s)
        buf = bytearray(4)
        assert f.readinto(buf) == 4 and bytes(buf) == b'{"ed'
        f.seek(s)
        assert f.read() == b'{"edited": true}'   # read(): to the published end of the segment
        f.seek(e)
        with pytest.raises(EditError) as ei:
            f.read(1)                            # beyond the published end
        assert ei.value.key == "edits.error.layer_corrupt"
        assert f.seek(0, 2) == doc.data_size and f.read(10) == b""
        f.seek(doc.data_size + 5)
        assert f.read(3) == b""
        with pytest.raises(ValueError):
            f.seek(-1)
        assert f.name == str(doc.data_path) and "VirtualReader" in repr(f)
    assert f.closed
    with pytest.raises(ValueError):
        f.read(1)


def test_damaged_layer_raises_layer_corrupt(tmp_path, docs) -> None:
    doc = open_source(tmp_path, docs, "array", n=6)
    ob = OverlayBuilder(doc, tmp_path / "layer")
    ob.set(1, b'{"ok": 1}')
    bad = ob.raw_unit(b"xx not json")
    ob.top[ob.rids[2]] = bad
    doc.set_overlay(ob.state())
    for call in (lambda: doc.record_bytes(2), lambda: list(doc.iter_blocks()),
                 lambda: list(doc.iter_blocks([2, 0])), lambda: list(doc.iter_blocks([0, 2]))):
        with pytest.raises(EditError) as ei:
            call()
        assert ei.value.key == "edits.error.layer_corrupt"
        assert not isinstance(ei.value, FileChangedError)
    # a span beyond the published end of its segment
    ob.top[ob.rids[2]] = (bad[0], bad[1] + 100)
    doc.set_overlay(ob.state())
    with pytest.raises(EditError):
        doc.record_bytes(2)
    with pytest.raises(EditError):
        list(doc.iter_blocks())
    # a record id without content
    st = ob.state(pieces=PieceTable.from_pairs([0, 99], [6, 100]), next_rid=100)
    doc.set_overlay(st)
    with pytest.raises(EditError):
        list(doc.iter_blocks([6]))
    with pytest.raises(EditError):
        doc.record_bytes(6)
    # an offset of an earlier state stays readable through the layer the document registered
    doc.set_overlay(None)
    assert doc._read(*ob.top[ob.rids[1]]) == b'{"ok": 1}'
    with pytest.raises(EditError):
        doc._read(doc.data_size - 2, EDIT_BASE + 2)   # a span across the spaces
    # a document that never showed an edit layer: no segment reader at all
    other = open_source(tmp_path, docs, "array", n=6, name="other.json")
    with pytest.raises(EditError) as ei:
        other._read(edit_offset(0, 0), edit_offset(0, 3))
    assert ei.value.key == "edits.error.layer_corrupt"
    with other.open_data() as f:
        f.seek(edit_offset(0, 0))
        with pytest.raises(EditError):
            f.read(3)


def test_set_overlay_checks(tmp_path, docs) -> None:
    doc = open_source(tmp_path, docs, "array", n=6)
    with pytest.raises(ValueError):
        doc.set_overlay(OverlayState.initial(5, None))
    with pytest.raises(TypeError):
        doc.set_overlay("state")   # type: ignore[arg-type]
    with pytest.raises(ValueError):
        doc.pinned(OverlayState.initial(7, None))
    with pytest.raises(TypeError):
        doc.pinned(42)
    p = tmp_path / "late.json"
    p.write_bytes(b"[1, 2, 3]")
    late = Document.open(p)
    docs.append(late)
    with pytest.raises(ValueError):
        late.set_overlay(OverlayState.initial(3, None))   # the index is not complete
    late.build_index()
    late.set_overlay(OverlayState.initial(3, None))
    late.close()
    with pytest.raises(ValueError):
        late.set_overlay(None)


def test_close_releases_the_layer(tmp_path, docs) -> None:
    doc = open_source(tmp_path, docs, "array", n=6)
    ob = OverlayBuilder(doc, tmp_path / "layer")
    ob.bulk({1: b'{"b": 1}'})
    state = ob.state()
    doc.set_overlay(state)
    doc.close()
    assert state.reader.closed and all(getattr(layer, "closed", False) for layer in state.layers[1:2])
    assert doc.overlay is None and doc.record_count == 0


# ----------------------------------------------------------------------------------
# node children of edited nodes: never in the persistent cache
# ----------------------------------------------------------------------------------
def test_child_indexes_of_edited_nodes_are_not_persisted(tmp_path, docs, backend) -> None:
    settings = Settings(large_node_children=1000)
    doc = open_source(tmp_path, docs, "array", n=4, settings=settings)
    big = b"[" + b",".join(b"%d" % i for i in range(5000)) + b"]"
    obj = b"{" + b",".join(b'"k%d": %d' % (i, i) for i in range(1500)) + b"}"
    ob = OverlayBuilder(doc, tmp_path / "layer")
    ob.set(1, b'{"big": ' + big + b', "obj": ' + obj + b"}")
    doc.set_overlay(ob.state())
    idx_dir = doc._index_dir()
    before = set(os.listdir(idx_dir))
    node = doc.node((1, "big"))
    assert node.is_edited and node.child_count() == 5000
    assert node.child(4321).value() == 4321 and [c.value() for c in node.children(10, 13)] == [10, 11, 12]
    onode = doc.node((1, "obj"))
    assert onode.child_count() == 1500 and onode.find_child("k1499").value() == 1499
    assert onode.child(7).member_span() == (onode.child(7).mstart, onode.child(7).end)
    assert raw_at(doc, onode.child(7).member_span()) == b'"k7": 7'
    created = set(os.listdir(idx_dir)) - before
    assert created and all(name.startswith("enode-") for name in created)   # temporary, not the node cache
    assert not any(doc._node_index_key(node.start) in name for name in os.listdir(idx_dir))
    # the base node of the same document is cached as usual
    doc.close()
    assert not (set(os.listdir(idx_dir)) - before), "temporary child indexes are removed by close()"


# ----------------------------------------------------------------------------------
# forced jsonl / stream on a whitespace-only file
# ----------------------------------------------------------------------------------
@pytest.mark.parametrize("content", [b"", b"  \r\n\t\n", b"\xef\xbb\xbf \n"])
@pytest.mark.parametrize("fmt", ["jsonl", "stream"])
def test_forced_jsonl_or_stream_on_a_whitespace_only_file(tmp_path, docs, fmt, content) -> None:
    p = tmp_path / "empty.json"
    p.write_bytes(content)
    doc = Document.open(p, format=fmt)
    docs.append(doc)
    doc.build_index()
    assert doc.kind == fmt and doc.record_count == 0 and doc.error is None
    assert list(doc.iter_blocks()) == [] and doc.root().child_count() == 0
    assert b"".join(doc.node_chunks(doc.root())) == b"[]"
    with pytest.raises(FileFormatError) as ei:
        Document.open(p)
    assert ei.value.key == "reader.empty_file"
    with pytest.raises(FileFormatError):
        Document.open(p, format="array")


def test_node_member_span_and_mstart_on_base_documents(tmp_path, docs) -> None:
    p = tmp_path / "k.json"
    p.write_bytes(b'{"a": {"x": 1, "y": [1, 2]}, "b": 2}')
    doc = Document.open(p, format="keyed")
    docs.append(doc)
    doc.build_index()
    a = doc.record_node(0)
    assert a.mstart == 1 and raw_at(doc, a.member_span()) == b'"a": {"x": 1, "y": [1, 2]}'
    y = doc.node(("a", "y"))
    assert raw_at(doc, y.member_span()) == b'"y": [1, 2]'
    first = y.child(0)
    assert first.mstart is None and first.member_span() == (first.start, first.end)
    assert not a.is_edited and doc.root().is_composite and not a.is_composite
