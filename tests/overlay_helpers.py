"""Edited documents for tests, without an EditSession (docs/ARCHITECTURE.md §12.0, §12.1, §12.3).

Shared by the stage-3 test modules (reader, saver, edits, GUI views...). An
:class:`OverlayBuilder` turns Python-level edits of a REAL, indexed :class:`~jsonreshape.reader.Document`
into an :class:`~jsonreshape.overlay.OverlayState` exactly as the edit layer stores them -- unit
bytes appended to segment files ``seg-%05d.dat`` of a temporary layer directory (read through a
:class:`~jsonreshape.overlay.DatReader`), a :class:`~jsonreshape.overlay.PieceTable` of record ids,
:class:`~jsonreshape.overlay.DictLayer` content (point edits) and file-backed
:class:`~jsonreshape.overlay.ArrayLayer` content (bulk steps) -- and keeps the *expected* result:
the effective record bytes in position order (:attr:`OverlayBuilder.records`) and, for wrapped
documents with an edited wrapper, the wrapper text W' (:attr:`OverlayBuilder.wrapper_text`).

:func:`write_expected_file` / :func:`open_expected` write a PLAIN file of the same kind holding
exactly those records (and that wrapper), so a test can compare "the edited document" with "a plain
document of the expected content" through any API (the oracle of the stage-3 reader tests).

Example::

    doc = Document.open(path); doc.build_index()
    ob = OverlayBuilder(doc, tmp_path / "layer")
    ob.set(3, b'{"id": 3, "name": "new"}')      # replace the record at position 3 (positions: BEFORE the call)
    ob.insert(0, b'{"id": -1}')                 # a new record at position 0 (len(ob) = append)
    ob.delete(5, 7)                             # remove positions 5 and 7
    ob.move([1, 2], 0)                          # PieceTable.move semantics (`to` counts without the moved)
    ob.duplicate(4)                             # a new record after #4 sharing its content span
    ob.bulk({2: b'{"x": 1}', 9: b'{"x": 2}'})   # content changes as ONE array layer (like a bulk step)
    ob.set_wrapper(b'{"meta": 2, "data": []}')  # wrapped: W' (the placeholder [] / {} at the record path)
    doc.set_overlay(ob.state())                 # a fresh OverlayState (new gen) every call
    assert [doc.record_bytes(i) for i in range(doc.record_count)] == ob.records
    plain = open_expected(doc, ob.records, tmp_path / "expected.json", wrapper=ob.wrapper_text)

Notes:

* Records are given as BYTES without outer whitespace: values for value documents, members
  ``b'"key": value'`` for member-record documents (keyed, wrapped with an object container).
  Every unit is validated with :func:`jsonreshape.editops.check_unit` (its scanner flags go into
  the state's ``flags``, as the session does).
* ``duplicate`` shares the content span (a data-file span for an unchanged base record), which is
  what the session does for value documents; for member documents a test may use it too (the
  reader must cope with two records sharing a span), but the session writes a new unit there.
* The DatReader and array layers belong to the document once ``doc.set_overlay(state)`` was called
  (``doc.close()`` closes them); otherwise call :meth:`OverlayBuilder.close`.
* Huge records (tests with a tiny ``Settings.large_node_bytes``) are read raw for the expected list.

Ready-made test documents and edit scenarios (used by ``test_overlay_reader.py`` /
``test_overlay_consumers.py``; reuse them): :data:`KINDS` (the 7 kinds + ``"wrapped_obj"``, a wrapped
document whose record container is an object), :func:`make_source` (a plain document of a kind,
opened and indexed), :data:`SCENARIOS` / :func:`apply_scenario` (point edits, inserts/deletes/moves/
duplicates, an array layer over every 3rd record, a mix of all with two array layers, renamed and
duplicate record keys, an edited wrapper), :func:`make_edited` (source + scenario + overlay set +
the expected plain document; the caller closes both documents).
"""

from __future__ import annotations

import random
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping, Sequence

import jsongen
from jsonreshape import codec, editops
from jsonreshape.fileio import read_range
from jsonreshape.overlay import (
    ArrayLayer,
    DatReader,
    DictLayer,
    OverlayState,
    PieceTable,
    edit_offset,
    segment_name,
    write_array_layer,
)
from jsonreshape.reader import Document
from jsonreshape.settings import Settings

__all__ = [
    "KINDS",
    "SCENARIOS",
    "OverlayBuilder",
    "apply_scenario",
    "base_records",
    "expected_text",
    "make_edited",
    "make_source",
    "new_record",
    "new_value",
    "open_expected",
    "write_expected_file",
    "write_source",
]

KINDS = ("array", "jsonl", "brace_list", "wrapped", "wrapped_obj", "keyed", "single", "stream")
SCENARIOS = ("none", "point", "structure", "every3", "mixed", "keys", "wrapper")


def base_records(doc: Document) -> list[bytes]:
    """The raw bytes of every record of the FILE (``doc.pinned(None)``), huge ones included."""
    base = doc.pinned(None)
    n = base.base_record_count
    if not n:
        return []
    with base.open_data() as f:
        return [read_range(f, *base.record_span(i)) for i in range(n)]


def _pieces_of(rids: Sequence[int]) -> tuple[list[int], list[int]]:
    """Maximal ranges of consecutive rids as (lo, hi) lists."""
    lo: list[int] = []
    hi: list[int] = []
    for r in rids:
        if hi and hi[-1] == r:
            hi[-1] = r + 1
        else:
            lo.append(r)
            hi.append(r + 1)
    return lo, hi


class OverlayBuilder:
    """Python-level edits of ``doc`` -> an :class:`OverlayState` + the expected records (module
    docstring). Positions are those of the state built so far (before each call)."""

    def __init__(self, doc: Document, layer_dir: str | Path) -> None:
        if not doc.index_complete:
            doc.build_index()
        self.doc = doc
        self.member = bool(doc.member_records)
        self.dir = Path(layer_dir)
        self.dir.mkdir(parents=True, exist_ok=True)
        self.reader = DatReader(self.dir)
        self.base_count = doc.base_record_count
        self.rids: list[int] = list(range(self.base_count))
        self.records: list[bytes] = base_records(doc)
        self.top: dict[int, tuple[int, int]] = {}      # the top DictLayer (point edits)
        self.lower: list[Any] = []                      # older layers (DictLayer / ArrayLayer), oldest first
        self.next_rid = self.base_count
        self.flags = 0
        self.steps = 0
        self.wrapper: tuple[int, int] | None = None
        self.wrapper_hole: tuple[int, int] | None = None
        self.wrapper_text: bytes | None = None
        self._ends: dict[int, int] = {}
        self._next_seg = 1                               # segment 0 holds the point edits
        self._layers = 0
        self._base_spans: dict[int, tuple[int, int]] = {}

    # -- bytes ----------------------------------------------------------------------------
    def __len__(self) -> int:
        return len(self.rids)

    def unit(self, data: bytes, seg: int = 0, *, member: bool | None = None) -> tuple[int, int]:
        """Append ``data`` to segment ``seg`` (validated as a unit; its flags are collected) and
        return its virtual span."""
        return self.units([data], seg, member=member)[0]

    def units(self, datas: Sequence[bytes], seg: int = 0, *, member: bool | None = None) -> list[tuple[int, int]]:
        """:meth:`unit` for many units at once (one write)."""
        m = self.member if member is None else member
        datas = [bytes(d) for d in datas]
        for d in datas:
            self.flags |= editops.check_unit(d, member=m)
        return self.raw_units(datas, seg)

    def raw_unit(self, data: bytes, seg: int = 0) -> tuple[int, int]:
        """Append ``data`` to segment ``seg`` without any check (damaged layers in tests)."""
        return self.raw_units([bytes(data)], seg)[0]

    def raw_units(self, datas: Sequence[bytes], seg: int = 0) -> list[tuple[int, int]]:
        path = self.dir / segment_name(seg)
        off = self._ends.get(seg, 0)
        spans = []
        pos = off
        for d in datas:
            spans.append((edit_offset(seg, pos), edit_offset(seg, pos + len(d))))
            pos += len(d)
        with open(path, "r+b" if path.exists() else "wb") as f:
            f.seek(off)
            f.write(b"".join(datas))
        self._ends[seg] = pos
        self.reader.publish(seg, pos)
        return spans

    def content(self, rid: int) -> tuple[int, int] | None:
        """The content span of ``rid`` in the state built so far (``None`` = its file span)."""
        span = self.top.get(rid)
        if span is not None:
            return span
        for layer in reversed(self.lower):
            span = layer.get(rid)
            if span is not None:
                return span
        return None

    def _span_of(self, rid: int) -> tuple[int, int]:
        span = self.content(rid)
        if span is not None:
            return span
        hit = self._base_spans.get(rid)
        if hit is None:
            hit = self._base_spans[rid] = self.doc.pinned(None).record_span(rid)
        return hit

    # -- edits (positions before the call) ---------------------------------------------------
    def set(self, pos: int, data: bytes) -> OverlayBuilder:
        """New content of the record at ``pos``."""
        rid = self.rids[pos]
        self.top[rid] = self.unit(data)
        self.records[pos] = bytes(data)
        self.steps += 1
        return self

    def insert(self, pos: int, data: bytes) -> OverlayBuilder:
        """A new record at ``pos`` (``len(self)`` appends)."""
        if not 0 <= pos <= len(self.rids):
            raise IndexError(pos)
        rid = self.next_rid
        self.next_rid += 1
        self.top[rid] = self.unit(data)
        self.rids.insert(pos, rid)
        self.records.insert(pos, bytes(data))
        self.steps += 1
        return self

    def delete(self, *positions: int) -> OverlayBuilder:
        """Remove the records at ``positions``."""
        for pos in sorted(set(positions), reverse=True):
            del self.rids[pos]
            del self.records[pos]
        self.steps += 1
        return self

    def move(self, positions: Iterable[int], to: int) -> OverlayBuilder:
        """``PieceTable.move``: the records at ``positions`` (ascending, unique) are taken out and
        put back in their order so that the first one is at ``to`` (counted without them)."""
        ps = sorted(set(positions))
        moved_r = [self.rids[p] for p in ps]
        moved_b = [self.records[p] for p in ps]
        for p in reversed(ps):
            del self.rids[p]
            del self.records[p]
        if not 0 <= to <= len(self.rids):
            raise IndexError(to)
        self.rids[to:to] = moved_r
        self.records[to:to] = moved_b
        self.steps += 1
        return self

    def duplicate(self, pos: int) -> OverlayBuilder:
        """A new record right after ``pos`` whose content span is the current span of ``pos``."""
        rid = self.next_rid
        self.next_rid += 1
        self.top[rid] = self._span_of(self.rids[pos])
        self.rids.insert(pos + 1, rid)
        self.records.insert(pos + 1, self.records[pos])
        self.steps += 1
        return self

    def bulk(self, changes: Mapping[int, bytes] | Iterable[tuple[int, bytes]]) -> OverlayBuilder:
        """Content changes of many records as ONE array layer (units in a new segment), pushed like
        a bulk step: the current top DictLayer goes below the array layer, a new empty one on top."""
        items = list(changes.items()) if isinstance(changes, Mapping) else list(changes)
        seg = self._next_seg
        self._next_seg += 1
        spans = self.units([data for _pos, data in items], seg)
        entries = []
        for (pos, data), (s, e) in zip(items, spans):
            entries.append((self.rids[pos], s, e))
            self.records[pos] = bytes(data)
        self._layers += 1
        path = self.dir / f"layer-{self._layers:05d}.bin"
        write_array_layer(path, entries, flags=self.flags, segments=(seg,), work_dir=self.dir)
        self.lower.append(DictLayer.from_items(self.top.items()))
        self.lower.append(ArrayLayer.open(path))
        self.top = {}
        self.steps += 1
        return self

    def bulk_every(self, step: int, fn: Callable[[int, bytes], bytes], start: int = 0) -> OverlayBuilder:
        """:meth:`bulk` of every ``step``-th record from ``start``: ``fn(position, old bytes)``."""
        return self.bulk({p: fn(p, self.records[p]) for p in range(start, len(self.records), step)})

    def set_wrapper(self, text: bytes) -> OverlayBuilder:
        """Wrapped documents: the edited wrapper text W' (no outer whitespace), holding the placeholder
        ``[]`` / ``{}`` of the record container at the record path (the hole)."""
        if self.doc.kind != "wrapped":
            raise ValueError("set_wrapper() is for wrapped documents")
        text = bytes(text)
        loc = editops.locate(text, tuple(self.doc.record_path or ()))
        if text[loc.start:loc.end] not in (b"[]", b"{}"):
            raise ValueError(f"no placeholder at the record path: {text[loc.start:loc.end]!r}")
        ws, we = self.unit(text, member=False)
        self.wrapper = (ws, we)
        self.wrapper_hole = (ws + loc.start, ws + loc.end)
        self.wrapper_text = text
        self.steps += 1
        return self

    # -- results ----------------------------------------------------------------------------
    def pieces(self) -> PieceTable:
        lo, hi = _pieces_of(self.rids)
        return PieceTable.from_pairs(lo, hi)

    def state(self, **overrides: Any) -> OverlayState:
        """A new :class:`OverlayState` of the edits so far (fresh gen; ``version`` = number of
        edit calls, so a builder with edits gives an ``edited`` state)."""
        kw: dict[str, Any] = dict(
            base_count=self.base_count, pieces=self.pieces(),
            layers=tuple(self.lower) + (DictLayer.from_items(self.top.items()),),
            wrapper=self.wrapper, wrapper_hole=self.wrapper_hole, flags=self.flags,
            next_rid=self.next_rid, version=self.steps, reader=self.reader)
        kw.update(overrides)
        return OverlayState(**kw)

    def close(self) -> None:
        """Close the reader and the array layers (when no document owns them)."""
        for layer in self.lower:
            if isinstance(layer, ArrayLayer):
                layer.close()
        self.reader.close()


# ----------------------------------------------------------------------------------
# the expected plain file
# ----------------------------------------------------------------------------------
def expected_text(doc: Document, records: Sequence[bytes], *, wrapper: bytes | None = None) -> bytes:
    """The bytes of a plain file of ``doc``'s kind holding exactly ``records`` (and the wrapper
    text ``wrapper`` = W' for wrapped documents with an edited wrapper; else the file's own
    wrapper)."""
    kind = doc.kind
    records = [bytes(r) for r in records]
    if kind == "array":
        return b"[" + b",\n".join(records) + b"]\n"
    if kind in ("keyed", "brace_list"):
        return b"{" + b",\n".join(records) + b"}\n"
    if kind == "jsonl":
        return b"".join(r + b"\n" for r in records)
    if kind == "stream":
        return b"\n".join(records) + (b"\n" if records else b"")
    if kind == "single":
        if len(records) != 1:
            raise ValueError("a single document has exactly one record")
        return records[0] + b"\n"
    if kind != "wrapped":
        raise ValueError(kind)
    open_b, close_b = (b"{", b"}") if doc.member_records else (b"[", b"]")
    body = open_b + b",\n".join(records) + close_b
    if wrapper is not None:
        loc = editops.locate(wrapper, tuple(doc.record_path or ()))
        return wrapper[:loc.start] + body + wrapper[loc.end:] + b"\n"
    base = doc.pinned(None)
    (ps, pe), (ss, se) = base.wrapper_spans()
    with base.open_data() as f:
        prefix = read_range(f, ps, pe)
        suffix = read_range(f, ss, se)
    return prefix + body + suffix


def write_expected_file(doc: Document, expected_records: Sequence[bytes], path: str | Path, *,
                        wrapper: bytes | None = None) -> dict[str, Any]:
    """Write :func:`expected_text` to ``path`` (UTF-8, no BOM); returns the ``Document.open``
    keyword arguments that open it as the same kind (``format`` and, wrapped, ``record_path``)."""
    Path(path).write_bytes(expected_text(doc, expected_records, wrapper=wrapper))
    kw: dict[str, Any] = {"format": doc.kind}
    if doc.kind == "wrapped":
        kw["record_path"] = tuple(doc.record_path or ())
    return kw


def open_expected(doc: Document, expected_records: Sequence[bytes], path: str | Path, *,
                  wrapper: bytes | None = None, **open_kw: Any) -> Document:
    """:func:`write_expected_file` + ``Document.open`` (same settings) + ``build_index``."""
    kw = write_expected_file(doc, expected_records, path, wrapper=wrapper)
    kw.setdefault("settings", doc.settings)
    kw.update(open_kw)
    out = Document.open(path, **kw)
    out.build_index()
    return out


# ----------------------------------------------------------------------------------
# test documents and edit scenarios
# ----------------------------------------------------------------------------------
def write_source(path: str | Path, kind: str, n: int = 24, seed: int = 0, style: str = "compact") -> dict[str, Any]:
    """Write a plain document of ``kind`` (one of :data:`KINDS`) with ``n`` records of
    ``jsongen.records`` (keyed: keys ``k0..`` with two duplicates; single: one array of n // 4 records);
    returns the ``Document.open`` keyword arguments that open it as that kind."""
    path = Path(path)
    rng = random.Random(seed)
    recs = jsongen.records(n, seed=seed)
    if kind == "wrapped_obj":
        members = b",".join(codec.dumps(f"r{i}") + b":" + jsongen.encode(r, style, seed + i)
                            for i, r in enumerate(recs))
        path.write_bytes(b'{"meta": {"v": 1, "who": "Ali"}, "data": {' + members + b'}, "tail": [1, 2]}\n')
        return {"format": "wrapped", "record_path": "data"}
    if kind == "single":
        jsongen.write_file(path, recs[:max(1, n // 4)], "single", style=style, seed=seed)
        return {"format": "single"}
    keys = None
    if kind == "keyed":
        keys = [f"k{i % max(1, n - 2)}" for i in range(n)]   # two duplicate keys (the last match wins)
        rng.shuffle(keys)
    jsongen.write_file(path, recs, kind, style=style, seed=seed, keys=keys)
    if kind == "wrapped":
        return {"format": "wrapped", "record_path": "data"}
    return {"format": kind}


def make_source(path: str | Path, kind: str, *, n: int = 24, seed: int = 0, style: str = "compact",
                settings: Settings | None = None) -> Document:
    """:func:`write_source` + ``Document.open`` + ``build_index`` (the caller closes it)."""
    kw = write_source(path, kind, n, seed, style)
    doc = Document.open(path, settings=settings, **kw)
    doc.build_index()
    assert doc.error is None
    return doc


def new_value(rng: random.Random, i: int) -> bytes:
    """A compact record value (one line); every 3rd one with escapes (``\\"``, ``\\n``, U+2028,
    Uzbek Cyrillic/Latin) and exact numbers (``0.1``, ``1e400``, ``12345678901234567890``, ``-0.0``,
    ``1.10``)."""
    rec = jsongen.rand_record(rng, 1000 + i)
    if i % 3 == 0:
        rec["esc"] = "A\"\\/\n\t" + chr(0x2028) + "Ўзбек o‘zbek"
        rec["nums"] = [codec.loads(t) for t in (b"0.1", b"1e400", b"12345678901234567890", b"-0.0", b"1.10")]
    return jsongen.encode(rec, "compact", seed=i)


def new_record(doc: Document, rng: random.Random, i: int, key: str | None = None) -> bytes:
    """A new record for ``doc``: a value, a member ``"key": value`` (member records), or the whole
    root (single documents)."""
    value = new_value(rng, i)
    if doc.kind == "single":
        return b"[" + value + b"]"
    if doc.member_records:
        return editops.encode_key(key if key is not None else f"new{i}") + b": " + value
    return value


def apply_scenario(ob: OverlayBuilder, doc: Document, scenario: str, rng: random.Random) -> None:
    """The Python-level edits of one of :data:`SCENARIOS` (single documents: the root is replaced)."""
    rec = lambda i, key=None: new_record(doc, rng, i, key)  # noqa: E731
    if scenario == "none":
        return
    if doc.kind == "single":
        ob.set(0, rec(1))
        if scenario == "mixed":
            ob.set(0, rec(2))
        return
    n = len(ob)
    if scenario == "point":
        for p in sorted({0, n // 2, n - 1}):
            if p < len(ob):
                ob.set(p, rec(p))
    elif scenario == "structure":
        ob.insert(0, rec(10))
        ob.insert(len(ob), rec(11))
        ob.insert(len(ob) // 2, rec(12))
        ob.delete(1, 3, len(ob) - 2)
        ob.move([2, 4], 0)
        ob.move([0], len(ob) - 1)
        ob.duplicate(1)
        ob.duplicate(len(ob) - 1)
    elif scenario == "every3":
        ob.bulk_every(3, lambda p, old: rec(100 + p, f"bulk{p}"))
    elif scenario == "mixed":
        ob.set(1, rec(20))
        ob.insert(2, rec(21))
        ob.bulk_every(3, lambda p, old: rec(200 + p, f"m{p}"), start=1)
        ob.set(4, rec(22))                 # the top dict layer over the array layer
        ob.delete(0, 7)
        ob.move([1, 2, 3], len(ob) - 3)
        ob.insert(len(ob), rec(23))
        ob.bulk({0: rec(24), len(ob) - 1: rec(25)})   # a second array layer
        ob.duplicate(3)
    elif scenario == "keys":
        if doc.member_records:
            base = base_records(doc)
            key0, _ = Document.split_member(base[0])
            ob.set(1, editops.encode_key(key0) + b": " + new_value(rng, 30))   # a duplicate key (the last wins)
            for p in (0, n - 1):
                key, vo = Document.split_member(ob.records[p])
                ob.set(p, editops.encode_key(key + "_renamed") + b": " + ob.records[p][vo:])
            ob.insert(3, editops.encode_key("ключ o'zbek \u2028") + b": " + new_value(rng, 31))
        else:
            ob.set(n // 3, rec(32))
    elif scenario == "wrapper":
        if doc.kind != "wrapped":
            ob.set(n - 1, rec(40))
            return
        hole = b"{}" if doc.member_records else b"[]"
        ob.set_wrapper(b'{"meta": {"v": 2, "who": "Vali", "esc": "\\u0041"}, "data": ' + hole
                       + b', "extra": {"added": true}, "tail": [3]}')
        ob.set(0, rec(41))
        ob.insert(len(ob), rec(42))
    else:
        raise ValueError(scenario)


def make_edited(directory: str | Path, kind: str, scenario: str, *, n: int = 24, seed: int = 0,
                style: str = "compact", settings: Settings | None = None
                ) -> tuple[Document, OverlayBuilder, Document]:
    """``(edited document, its builder, the plain document of the expected content)`` in ``directory``
    (``src.json``, ``layer/``, ``expected.json``); the overlay state is set. The caller closes both
    documents."""
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    doc = make_source(directory / "src.json", kind, n=n, seed=seed, style=style, settings=settings)
    ob = OverlayBuilder(doc, directory / "layer")
    apply_scenario(ob, doc, scenario, random.Random(seed + 7))
    doc.set_overlay(ob.state())
    exp = open_expected(doc, ob.records, directory / "expected.json", wrapper=ob.wrapper_text)
    assert exp.error is None and exp.record_count == len(ob.records)
    return doc, ob, exp
