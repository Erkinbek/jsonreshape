"""Helpers of the EditSession tests (tests/test_edits*.py): an independent oracle and checks.

* :class:`Model` -- the expected document as plain Python values (objects keep their member ORDER and
  duplicate keys: :class:`O`; numbers keep their exact token: :class:`N`) with the same command semantics as
  ``jsonreshape.edits`` (§12.4/§12.5 key rules, document order of moved items, free keys ...), written without any
  byte-level code: it is compared with the parsed effective document after every command.
* :func:`write_unique_doc` -- documents of every kind whose string leaves are unique (so a wrongly remapped
  reference shows a different value).
* :func:`effective` -- the effective bytes of a document (every record + the root's text through
  ``node_chunks``) for the "undo restores the exact bytes" checks.
* :func:`check_changeset` -- ChangeSet fields against the documents before/after: ``records`` (unlisted positions
  with a preimage keep their bytes), NodeChange counts, ``select`` resolving, refs remapped like a holder does
  (:func:`remap_ref`) pointing at the same leaf values.
* :class:`Harness` -- runs a command and its model counterpart with all the checks above, then undo (exact previous
  bytes, reverse ChangeSet) and redo (exact bytes again); :meth:`Harness.finish` undoes everything (original bytes)
  and redoes everything (final bytes). :func:`ref` / :func:`pos` / :func:`others` build refs from the model.
"""

from __future__ import annotations

import copy
import json
import random
from bisect import bisect_left
from pathlib import Path
from typing import Any, Callable, Iterable, Sequence

import pytest

from jsonreshape import codec, editops
from jsonreshape.edits import EditSession, Ref
from jsonreshape.errors import EditError
from jsonreshape.overlay import ChangeSet
from jsonreshape.paths import canonical_index
from jsonreshape.reader import Document, split_member

KINDS = ("array", "jsonl", "brace_list", "wrapped", "wrapped_obj", "keyed", "single", "stream")


# ----------------------------------------------------------------------------------
# model values
# ----------------------------------------------------------------------------------
class N:
    """A JSON number by its exact token."""

    __slots__ = ("tok",)

    def __init__(self, tok: str) -> None:
        self.tok = tok

    def __eq__(self, other: object) -> bool:
        return isinstance(other, N) and other.tok == self.tok

    def __hash__(self) -> int:
        return hash(("N", self.tok))

    def __repr__(self) -> str:
        return f"N({self.tok})"


class O:
    """A JSON object as an ordered list of ``[key, value]`` pairs (duplicates allowed)."""

    __slots__ = ("pairs",)

    def __init__(self, pairs: Iterable[Any] = ()) -> None:
        self.pairs = [[k, v] for k, v in pairs]

    def __eq__(self, other: object) -> bool:
        return isinstance(other, O) and other.pairs == self.pairs

    def __repr__(self) -> str:
        return f"O({self.pairs!r})"

    def keys(self) -> list[str]:
        return [k for k, _ in self.pairs]

    def find(self, key: str) -> int | None:
        """Index of the LAST member ``key`` (dict semantics)."""
        found = None
        for i, (k, _v) in enumerate(self.pairs):
            if k == key:
                found = i
        return found

    def find_first(self, key: str) -> int | None:
        for i, (k, _v) in enumerate(self.pairs):
            if k == key:
                return i
        return None


class _Placeholder:
    def __eq__(self, other: object) -> bool:
        return isinstance(other, _Placeholder)

    def __repr__(self) -> str:
        return "PH"


PH = _Placeholder()


def parse(data: bytes | str) -> Any:
    text = data.decode("utf-8") if isinstance(data, (bytes, bytearray)) else data
    return json.loads(text, object_pairs_hook=O, parse_int=N, parse_float=N)


def dump(value: Any) -> bytes:
    """Compact JSON text of a model value (non-ASCII escaped, so lone surrogates survive)."""
    return _dump(value).encode("ascii")


def _dump(v: Any) -> str:
    if isinstance(v, O):
        return "{" + ",".join(json.dumps(k) + ":" + _dump(x) for k, x in v.pairs) + "}"
    if isinstance(v, list):
        return "[" + ",".join(_dump(x) for x in v) + "]"
    if isinstance(v, N):
        return v.tok
    return json.dumps(v)


def to_plain(v: Any) -> Any:
    """Model value -> Python value for ``jsongen`` writers (objects: dicts; numbers: exact codec numbers)."""
    if isinstance(v, O):
        return {k: to_plain(x) for k, x in v.pairs}
    if isinstance(v, list):
        return [to_plain(x) for x in v]
    if isinstance(v, N):
        return codec.loads(v.tok)
    return v


def is_container(v: Any) -> bool:
    return isinstance(v, (O, list))


def count(v: Any) -> int:
    return len(v.pairs) if isinstance(v, O) else len(v)


def child(v: Any, i: int) -> Any:
    return v.pairs[i][1] if isinstance(v, O) else v[i]


def set_child(v: Any, i: int, x: Any) -> None:
    if isinstance(v, O):
        v.pairs[i][1] = x
    else:
        v[i] = x


def child_key(v: Any, i: int) -> Any:
    return v.pairs[i][0] if isinstance(v, O) else i


def insert_child(v: Any, i: int, x: Any, key: str | None) -> None:
    if isinstance(v, O):
        v.pairs.insert(i, [key, x])
    else:
        v.insert(i, x)


def delete_children(v: Any, indices: Iterable[int]) -> None:
    for i in sorted(set(indices), reverse=True):
        if isinstance(v, O):
            del v.pairs[i]
        else:
            del v[i]


def free_key(taken: Any, base: Any) -> str:
    base = str(base)
    if base not in taken:
        return base
    n = 2
    while f"{base}_{n}" in taken:
        n += 1
    return f"{base}_{n}"


# ----------------------------------------------------------------------------------
# the model
# ----------------------------------------------------------------------------------
class ModelError(Exception):
    """The model refuses a command (the session must raise ``EditError(key)``)."""

    def __init__(self, key: str) -> None:
        super().__init__(key)
        self.key = key


class Model:
    """The expected edited document (module docstring). ``records``: values (value documents) or ``[key,
    value]`` (member documents); ``wrapper``: the root value with :data:`PH` at the record container."""

    def __init__(self, kind: str, member: bool, records: list, wrapper: Any, record_path: tuple) -> None:
        self.kind = kind
        self.member = member
        self.records = records
        self.wrapper = wrapper
        self.rp = tuple(record_path)

    def copy(self) -> Model:
        return copy.deepcopy(self)

    def __eq__(self, other: object) -> bool:
        return (isinstance(other, Model) and self.records == other.records and self.wrapper == other.wrapper)

    def __repr__(self) -> str:
        return f"Model({self.records!r}, wrapper={self.wrapper!r})"

    # -- building from a document -----------------------------------------------------------------
    @classmethod
    def of(cls, doc: Document) -> Model:
        v = doc.pinned()
        member = bool(v.member_records)
        recs: list = []
        for _pos, b in v.iter_record_bytes(include_huge=True):
            assert b is not None
            if member:
                key, vo = split_member(b)
                recs.append([key, parse(b[vo:])])
            else:
                recs.append(parse(b))
        wrapper = None
        rp = tuple(v.record_path or ()) if v.kind == "wrapped" else ()
        if v.kind == "wrapped":
            full = parse(b"".join(v.node_chunks(v.root())))
            wrapper = _replace_first(full, rp, PH)
        return cls(v.kind, member, recs, wrapper, rp)

    def full(self) -> Any:
        """The whole effective root value (wrapped: the wrapper around the records)."""
        container = O(self.records) if self.member else list(self.records)
        if self.kind == "wrapped":
            return _replace_first(copy.deepcopy(self.wrapper), self.rp, container)
        if self.kind == "single":
            return self.records[0]
        return container

    # -- navigation ------------------------------------------------------------------------------
    def root(self, record: int | None) -> Any:
        if record is None:
            return self.wrapper
        r = self.records[record]
        return r[1] if self.member else r

    def set_root(self, record: int | None, value: Any) -> None:
        if record is None:
            self.wrapper = value
        elif self.member:
            self.records[record][1] = value
        else:
            self.records[record] = value

    def node(self, record: int | None, ipath: Sequence[int]) -> Any:
        v = self.root(record)
        for i in ipath:
            v = child(v, i)
        return v

    def hole_ipath(self) -> list[int]:
        ip = _first_ipath(self.wrapper, self.rp)
        assert ip is not None
        return ip

    def refs(self, *, containers: bool = False, with_roots: bool = True, wrapper: bool = True) -> list[Ref]:
        """Every node as a complete Ref (wrapper nodes only off the record path)."""
        out: list[Ref] = []
        for r in range(len(self.records)):
            for path, ipath, v in _walk(self.root(r)):
                if (with_roots or ipath) and (not containers or is_container(v)):
                    out.append(Ref(r, tuple(path), tuple(ipath)))
        if self.kind == "wrapped" and wrapper:
            hole = self.hole_ipath()
            for path, ipath, v in _walk(self.wrapper):
                if list(ipath) == hole[:len(ipath)]:
                    continue
                if not containers or is_container(v):
                    out.append(Ref(None, tuple(path), tuple(ipath)))
        return out

    # -- commands (the same semantics as EditSession) -------------------------------------------------
    def set_value(self, ref: Ref, value: Any) -> None:
        if not ref.ipath:
            self.set_root(ref.record, value)
            return
        parent = self.node(ref.record, ref.ipath[:-1])
        set_child(parent, ref.ipath[-1], value)

    def rename(self, ref: Ref, key: str) -> None:
        if not ref.ipath:
            if ref.record is None or not self.member:
                raise ModelError("edits.error.not_member")
            if key != self.records[ref.record][0] and any(
                    k == key for i, (k, _v) in enumerate(self.records) if i != ref.record):
                raise ModelError("edits.error.key_exists")
            self.records[ref.record][0] = key
            return
        parent = self.node(ref.record, ref.ipath[:-1])
        if not isinstance(parent, O):
            raise ModelError("edits.error.not_member")
        i = ref.ipath[-1]
        if key != parent.pairs[i][0] and any(k == key for j, (k, _v) in enumerate(parent.pairs) if j != i):
            raise ModelError("edits.error.key_exists")
        parent.pairs[i][0] = key

    def insert(self, parent: Ref, index: int | None, value: Any, key: str | None) -> None:
        c = self.node(parent.record, parent.ipath)
        if not is_container(c):
            raise ModelError("edits.error.not_container")
        n = count(c)
        idx = n if index is None else index
        if isinstance(c, O):
            if key is None:
                raise ModelError("edits.error.key_required")
            if key in c.keys():
                raise ModelError("edits.error.key_exists")
        insert_child(c, idx, value, key if isinstance(c, O) else None)

    def paste(self, parent: Ref, index: int | None, value: Any, key: str | None) -> None:
        c = self.node(parent.record, parent.ipath)
        if isinstance(c, O) and key is None:
            key = free_key(set(c.keys()), "pasted")
        self.insert(parent, index, value, key)

    def insert_records(self, index: int | None, items: list[tuple[Any, str | None]], base: str = "pasted") -> None:
        if self.kind == "single":
            raise ModelError("edits.error.single_root")
        idx = len(self.records) if index is None else index
        new = []
        if self.member:
            taken = {k for k, _v in self.records}
            for value, key in items:
                if key is None:
                    key = free_key(taken, base)
                elif key in taken:
                    raise ModelError("edits.error.key_exists")
                taken.add(key)
                new.append([key, value])
        else:
            new = [value for value, _key in items]
        self.records[idx:idx] = new

    def _normalize(self, refs: list[Ref]) -> list[Ref]:
        out = []
        kept: set[tuple] = set()
        for ref in sorted(refs, key=lambda r: len(r.ipath)):
            host = ("w",) if ref.record is None else ("r", ref.record)
            ip = tuple(ref.ipath)
            if any((host, ip[:k]) in kept for k in range(len(ip) + 1)):
                continue
            kept.add((host, ip))
            out.append(ref)
        keep = {id(r) for r in out}
        return [r for r in refs if id(r) in keep]

    def delete(self, refs: list[Ref]) -> None:
        refs = self._normalize(refs)
        records = sorted({r.record for r in refs if r.record is not None and not r.ipath})
        if records and self.kind == "single":
            raise ModelError("edits.error.single_root")
        groups: dict[int, tuple[Any, list[int]]] = {}
        for r in refs:
            if r.record is not None and not r.ipath:
                continue
            c = self.node(r.record, r.ipath[:-1])
            groups.setdefault(id(c), (c, []))[1].append(r.ipath[-1])
        for c, idxs in groups.values():
            delete_children(c, idxs)
        for p in reversed(records):
            del self.records[p]

    def duplicate(self, refs: list[Ref]) -> None:
        refs = self._normalize(refs)
        records = sorted({r.record for r in refs if r.record is not None and not r.ipath})
        if records and self.kind == "single":
            raise ModelError("edits.error.single_root")
        groups: dict[int, tuple[Any, list[int]]] = {}
        for r in refs:
            if r.record is not None and not r.ipath:
                continue
            c = self.node(r.record, r.ipath[:-1])
            groups.setdefault(id(c), (c, []))[1].append(r.ipath[-1])
        for c, idxs in groups.values():
            plan = []
            taken = set(c.keys()) if isinstance(c, O) else set()
            for i in sorted(idxs):
                key = None
                if isinstance(c, O):
                    key = free_key(taken, c.pairs[i][0])
                    taken.add(key)
                plan.append((i, copy.deepcopy(child(c, i)), key))
            for i, v, key in reversed(plan):
                insert_child(c, i + 1, v, key)
        if records:
            taken = {k for k, _v in self.records} if self.member else set()
            copies = []
            for p in records:
                if self.member:
                    key = free_key(taken, self.records[p][0])
                    taken.add(key)
                    copies.append([key, copy.deepcopy(self.records[p][1])])
                else:
                    copies.append(copy.deepcopy(self.records[p]))
            for p, c in reversed(list(zip(records, copies))):
                self.records.insert(p + 1, c)

    def change_type(self, refs: list[Ref], to: str) -> int:
        refs = self._normalize(refs)
        n = 0
        new_values = []
        unchanged = object()
        for r in refs:
            v = self.node(r.record, r.ipath)
            cv = codec.loads(dump(v))
            nv = editops.convert_value(cv, to)   # ConversionError propagates
            new_values.append(unchanged if nv is cv else parse(codec.dumps(nv)))
        for r, nv in zip(refs, new_values):
            if nv is not unchanged:
                self.set_value(r, nv)
                n += 1
        return n

    def _order(self, r: Ref) -> tuple:
        return (-1 if r.record is None else r.record, tuple(r.ipath))

    def _value_of(self, r: Ref) -> tuple[Any, str]:
        if r.record is not None and not r.ipath:
            if self.member:
                return self.records[r.record][1], self.records[r.record][0]
            return self.records[r.record], str(r.record)
        parent = self.node(r.record, r.ipath[:-1])
        i = r.ipath[-1]
        return child(parent, i), (parent.pairs[i][0] if isinstance(parent, O) else str(i))

    def move(self, refs: list[Ref], parent: Ref | None, index: int | None) -> None:
        refs = self._normalize(refs)
        if parent is None:
            self._move_records(refs, index)
            return
        for r in refs:
            if r.record == parent.record and tuple(parent.ipath[:len(r.ipath)]) == tuple(r.ipath):
                raise ModelError("edits.error.move_into_self")
        T = self.node(parent.record, parent.ipath)
        if not is_container(T):
            raise ModelError("edits.error.not_container")
        for r in refs:
            if r.record is not None and not r.ipath and self.kind == "single":
                raise ModelError("edits.error.single_root")
        n = count(T)
        own_refs = [r for r in refs if r.ipath and r.record == parent.record
                    and tuple(r.ipath[:-1]) == tuple(parent.ipath)]
        own = sorted(r.ipath[-1] for r in own_refs)
        idx = n if index is None else index
        to = idx - bisect_left(own, idx)
        items = sorted(refs, key=self._order)
        is_obj = isinstance(T, O)
        taken: set[str] = set()
        own_keys: dict[int, str] = {}
        if is_obj:
            taken = {T.pairs[i][0] for i in range(n) if i not in set(own)}
            for i in own:
                k0 = T.pairs[i][0]
                k1 = free_key(taken, k0) if k0 in taken else k0
                taken.add(k1)
                own_keys[i] = k1
        own_ids = {id(r) for r in own_refs}
        specs = []
        for r in items:
            if id(r) in own_ids:
                i = r.ipath[-1]
                specs.append((child(T, i), own_keys.get(i)))
            else:
                v, base = self._value_of(r)
                key = None
                if is_obj:
                    key = free_key(taken, base)
                    taken.add(key)
                specs.append((v, key))
        # remove the sources (containers by identity), then place the items
        groups: dict[int, tuple[Any, list[int]]] = {}
        for r in refs:
            if r.record is not None and not r.ipath:
                continue
            c = self.node(r.record, r.ipath[:-1])
            groups.setdefault(id(c), (c, []))[1].append(r.ipath[-1])
        for c, idxs in groups.values():
            delete_children(c, idxs)
        for j, (v, key) in enumerate(specs):
            insert_child(T, to + j, v, key)
        records = sorted({r.record for r in refs if r.record is not None and not r.ipath})
        for p in reversed(records):
            del self.records[p]

    def _move_records(self, refs: list[Ref], index: int | None) -> None:
        if self.kind == "single":
            raise ModelError("edits.error.single_root")
        count_ = len(self.records)
        idx = count_ if index is None else index
        P = sorted({r.record for r in refs if r.record is not None and not r.ipath})
        to = idx - bisect_left(P, idx)
        items = sorted(refs, key=self._order)
        taken = {k for k, _v in self.records} if self.member else set()
        seq = []
        for r in items:
            if r.record is not None and not r.ipath:
                seq.append(self.records[r.record])
            else:
                v, base = self._value_of(r)
                if self.member:
                    key = free_key(taken, base)
                    taken.add(key)
                    seq.append([key, v])
                else:
                    seq.append(v)
        groups: dict[int, tuple[Any, list[int]]] = {}
        for r in refs:
            if r.record is not None and not r.ipath:
                continue
            c = self.node(r.record, r.ipath[:-1])
            groups.setdefault(id(c), (c, []))[1].append(r.ipath[-1])
        for c, idxs in groups.values():
            delete_children(c, idxs)
        rest = [rec for i, rec in enumerate(self.records) if i not in set(P)]
        self.records = rest[:to] + seq + rest[to:]

    def set_path(self, record: int | None, path: Sequence[Any], value: Any) -> None:
        v = self.root(record)
        for level, seg in enumerate(path):
            if isinstance(v, O):
                i = v.find(seg) if isinstance(seg, str) else None
            elif isinstance(v, list):
                i = seg if isinstance(seg, int) and 0 <= seg < len(v) else (
                    canonical_index(seg) if isinstance(seg, str) else None)
                if i is not None and i >= len(v):
                    i = None
            else:
                raise ModelError("edits.error.not_object")
            if i is None:
                if not isinstance(v, O) or any(not isinstance(s, str) for s in path[level:]):
                    raise ModelError("edits.error.not_object")
                nested = value
                for s in reversed(path[level + 1:]):
                    nested = O([[s, nested]])
                v.pairs.append([seg, nested])
                return
            if level == len(path) - 1:
                set_child(v, i, value)
                return
            v = child(v, i)
        self.set_root(record, value)


def _walk(v: Any, path: tuple = (), ipath: tuple = ()) -> Iterable[tuple[tuple, tuple, Any]]:
    yield path, ipath, v
    if isinstance(v, O):
        for i, (k, x) in enumerate(v.pairs):
            yield from _walk(x, path + (k,), ipath + (i,))
    elif isinstance(v, list):
        for i, x in enumerate(v):
            yield from _walk(x, path + (i,), ipath + (i,))


def _first_ipath(v: Any, path: Sequence[Any]) -> list[int] | None:
    ip = []
    for seg in path:
        if isinstance(v, O):
            i = v.find_first(seg) if isinstance(seg, str) else None
        elif isinstance(v, list):
            i = seg if isinstance(seg, int) and 0 <= seg < len(v) else None
        else:
            return None
        if i is None:
            return None
        ip.append(i)
        v = child(v, i)
    return ip


def _replace_first(v: Any, path: Sequence[Any], new: Any) -> Any:
    if not path:
        return new
    ip = _first_ipath(v, path)
    assert ip is not None, (v, path)
    parent = v
    for i in ip[:-1]:
        parent = child(parent, i)
    set_child(parent, ip[-1], new)
    return v


# ----------------------------------------------------------------------------------
# documents
# ----------------------------------------------------------------------------------
_EDGE = ["Toshkent", "Тошкент", "O'zbekiston", "Oʻzbekiston", "quote\"in", "back\\slash", "new\nline",
         "emoji 😀", "line sep  ", "Straße", "G'ulom"]
_NUMS = ["0.1", "1e400", "12345678901234567890", "-0.0", "1.10", "-0", "2.5E+3"]


class _Counter:
    def __init__(self) -> None:
        self.n = 0

    def next(self) -> int:
        self.n += 1
        return self.n


def unique_value(rng: random.Random, c: _Counter, depth: int = 0) -> Any:
    """A random model value whose string leaves are unique (every leaf string carries a counter)."""
    r = rng.random()
    if depth >= 3 or r < 0.35:
        k = rng.random()
        if k < 0.6:
            return f"{rng.choice(_EDGE)} s{c.next()}"
        if k < 0.8:
            return N(rng.choice(_NUMS))
        return rng.choice([True, False, None])
    if r < 0.65:
        return [unique_value(rng, c, depth + 1) for _ in range(rng.randint(0, 4))]
    keys = []
    for _ in range(rng.randint(0, 4)):
        keys.append(rng.choice(["a", "b", "name", "kalit o'zbek", "ключ", "x/y", "e "]) + f"{c.next()}")
    return O([[k, unique_value(rng, c, depth + 1)] for k in keys])


def unique_record(rng: random.Random, c: _Counter, i: int) -> O:
    return O([["id", N(str(i))], ["name", f"{rng.choice(_EDGE)} r{c.next()}"],
              ["tags", [f"t{c.next()}" for _ in range(rng.randint(1, 3))]],
              ["nested", unique_value(rng, c, 1)], ["num", N(rng.choice(_NUMS))]])


def write_unique_doc(path: Path, kind: str, *, n: int = 6, seed: int = 0, style: str = "compact"
                     ) -> dict[str, Any]:
    """Write a document of ``kind`` with ``n`` unique-leaf records; returns ``Document.open`` kwargs."""
    import jsongen

    rng = random.Random(seed)
    c = _Counter()
    recs = [unique_record(rng, c, i) for i in range(n)]
    plain = [to_plain(r) for r in recs]
    if kind == "wrapped_obj":
        members = b",".join(codec.dumps(f"r{i}") + b":" + jsongen.encode(r, style, seed + i) for i, r in enumerate(plain))
        path.write_bytes(b'{"meta": {"v": 1, "who": "Ali s0"}, "data": {' + members + b'}, "tail": [1, 2]}\n')
        return {"format": "wrapped", "record_path": "data"}
    if kind == "single":
        jsongen.write_file(path, plain, "single", style=style, seed=seed)
        return {"format": "single"}
    keys = [f"k{i}" for i in range(n)] if kind == "keyed" else None
    jsongen.write_file(path, plain, kind, style=style, seed=seed, keys=keys)
    if kind == "wrapped":
        return {"format": "wrapped", "record_path": "data"}
    return {"format": kind}


def open_doc(path: Path, kind: str, *, n: int = 6, seed: int = 0, style: str = "compact", settings: Any = None
             ) -> Document:
    kw = write_unique_doc(path, kind, n=n, seed=seed, style=style)
    doc = Document.open(path, settings=settings, **kw)
    doc.build_index()
    assert doc.error is None, doc.error
    return doc


# ----------------------------------------------------------------------------------
# checks
# ----------------------------------------------------------------------------------
def effective(doc: Document) -> tuple[tuple[bytes | None, ...], bytes]:
    """Every effective record's bytes + the root's text (``node_chunks``) of the state the document shows."""
    v = doc.pinned()
    recs = tuple(b for _p, b in v.iter_record_bytes(include_huge=True))
    root = b"".join(v.node_chunks(v.root()))
    return recs, root


def model_of(doc: Document) -> Model:
    return Model.of(doc)


def remap_ref(ref: Sequence[Any], cs: Any) -> tuple | None:
    """Apply a ChangeSet to a ``(record, path, ipath)`` like a holder (§12.1, §12.9 ``remap_ref``)."""
    rec, path, ipath = ref
    path, ipath = list(path), list(ipath)
    if rec is not None and cs.remap is not None:
        rec = cs.remap.map(rec)
        if rec is None:
            return None
    for nc in cs.nodes:
        if nc.record != rec:
            continue
        k = len(nc.ipath)
        if len(ipath) > k and ipath[:k] == list(nc.ipath):
            j = nc.remap.map(ipath[k])
            if j is None:
                return None
            if isinstance(path[k], int):
                path[k] = j
            ipath[k] = j
    return rec, tuple(path), tuple(ipath)


def node_at(model: Model, ref: Sequence[Any]) -> Any:
    """The model node at a complete ref, verifying the path against the ipath; ``KeyError`` if absent."""
    rec, path, ipath = ref
    if rec is not None and not 0 <= rec < len(model.records):
        raise KeyError(ref)
    v = model.root(rec)
    for seg, i in zip(path, ipath):
        if not is_container(v) or not 0 <= i < count(v):
            raise KeyError(ref)
        if isinstance(v, O):
            if v.pairs[i][0] != seg:
                raise KeyError(ref)
        elif seg != i:
            raise KeyError(ref)
        v = child(v, i)
    if len(path) != len(ipath):
        raise KeyError(ref)
    return v


def leaf_counts(model: Model) -> dict[str, int]:
    out: dict[str, int] = {}
    for ref in model.refs(wrapper=model.kind == "wrapped"):
        v = node_at(model, ref)
        if isinstance(v, str):
            out[v] = out.get(v, 0) + 1
    return out


def check_changeset(cs: Any, before_doc: tuple, before: Model, after: Model, after_doc: tuple, *,
                    targets: Iterable[Sequence[Any]] = (), leaves: bool = True) -> None:
    """ChangeSet fields against the documents before/after (module docstring). ``targets``: refs (before the
    step) whose subtree legitimately changes value; ``leaves=False`` skips the leaf-following check."""
    old_recs, new_recs = before_doc[0], after_doc[0]
    assert len(new_recs) == len(after.records)
    inv = cs.remap.inverse() if cs.remap is not None else None
    if cs.records is not None:
        for p, data in enumerate(new_recs):
            q = inv.map(p) if inv is not None else p
            if q is None:
                continue
            if p not in cs.records:
                assert data == old_recs[q], f"record {p} (was {q}) changed but is not in ChangeSet.records"
        for p in cs.records:
            assert 0 <= p < len(new_recs)
    # NodeChange counts: each container followed through the LATER entries (sequential semantics: an entry's
    # ipath is valid after the previous ones -- the reversed list of an undo is descendants first), the last count
    nodes = list(cs.nodes)
    last: dict[tuple, Any] = {}
    for k, nc in enumerate(nodes):
        ip: list[int] | None = list(nc.ipath)
        for later in nodes[k + 1:]:
            if ip is None or later.record != nc.record:
                continue
            d = len(later.ipath)
            if len(ip) > d and ip[:d] == list(later.ipath):
                j = later.remap.map(ip[d])
                ip = None if j is None else ip[:d] + [j] + ip[d + 1:]
        if ip is not None:
            last[(nc.record, tuple(ip))] = nc
    for (rec, ip), nc in last.items():
        v = after.node(rec, ip)
        assert (count(v) if is_container(v) else 0) == nc.count, (nc, v)   # a replaced container may be a scalar
    # the selection resolves
    if cs.select is not None:
        node_at(after, cs.select)
    # leaves followed through the ChangeSet keep their (unique) values
    if not leaves:
        return
    skip = {tuple(map(tuple, [t[1], t[2]])) + (t[0],) for t in targets}
    counts = leaf_counts(before)
    for ref in before.refs(wrapper=before.kind == "wrapped"):
        v = node_at(before, ref)
        if not isinstance(v, str) or counts.get(v) != 1:
            continue
        if (tuple(ref.path), tuple(ref.ipath), ref.record) in skip or _inside_target(ref, targets):
            continue
        new = remap_ref(ref, cs)
        if new is None:
            continue
        try:
            got = node_at(after, new)
        except KeyError:
            raise AssertionError(f"{ref} -> {new}: no such node after the step") from None
        assert got == v, f"{ref} -> {new}: {got!r} != {v!r}"


def _inside_target(ref: Ref, targets: Iterable[Sequence[Any]]) -> bool:
    for t in targets:
        if t[0] == ref.record and tuple(ref.ipath[:len(t[2])]) == tuple(t[2]):
            return True
    return False


# ----------------------------------------------------------------------------------
# the command harness (tests/test_edits*.py)
# ----------------------------------------------------------------------------------
def ref(model: Model, record: int | None, *path: Any) -> Ref:
    """A complete Ref to ``path`` of ``record`` (single documents: the records are the root array's elements)."""
    if model.kind == "single" and record is not None:
        path = (record,) + path
        record = 0
    v = model.root(record)
    ipath = []
    canon = []
    for seg in path:
        if isinstance(v, O):
            i = v.find(seg)
            assert i is not None, (seg, v)
            canon.append(seg)
        else:
            i = seg
            canon.append(i)
        ipath.append(i)
        v = v.pairs[i][1] if isinstance(v, O) else v[i]
    return Ref(record, tuple(canon), tuple(ipath))


def pos(md: Model, key: str) -> int:
    """The (last) record -- single documents: element of the root array -- holding the member ``key``."""
    items = md.records[0] if md.kind == "single" else [md.root(i) for i in range(len(md.records))]
    found = [i for i, v in enumerate(items) if isinstance(v, O) and key in v.keys()]
    assert found, key
    return found[-1]


def others(md: Model, *anchors: str) -> list[int]:
    """Records holding none of the ``anchors`` members."""
    items = md.records[0] if md.kind == "single" else [md.root(i) for i in range(len(md.records))]
    return [i for i, v in enumerate(items) if not (isinstance(v, O) and any(a in v.keys() for a in anchors))]


class Harness:
    """Runs commands with every check of the module docstring."""

    def __init__(self, doc: Document, session: EditSession) -> None:
        self.doc = doc
        self.s = session
        self.model = Model.of(doc)
        self.original = effective(doc)
        self.steps = 0

    def run(self, fn: Callable[[EditSession], ChangeSet], model_fn: Callable[[Model], Any], *,
            targets: tuple = (), undo: bool = True) -> ChangeSet:
        before_doc = effective(self.doc)
        before = self.model
        version = self.s.state.version
        cs = fn(self.s)
        assert isinstance(cs, ChangeSet) and cs.state is self.s.state and self.doc.gen == cs.gen
        expected = before.copy()
        model_fn(expected)
        got = Model.of(self.doc)
        assert got == expected, f"\nexpected {expected}\n     got {got}"
        after_doc = effective(self.doc)
        check_changeset(cs, before_doc, before, got, after_doc, targets=targets)
        self.model = got
        if self.s.state.version != version:
            self.steps += 1
            assert self.s.dirty and self.s.can_undo and cs.label
            if undo:
                u = self.s.undo()
                assert u is not None and u.kind == "undo" and u.label == cs.label
                assert effective(self.doc) == before_doc
                assert Model.of(self.doc) == before
                back = tuple(targets) + ((cs.select,) if cs.select is not None and targets else ())
                check_changeset(u, after_doc, got, before, before_doc, targets=back)
                r = self.s.redo()
                assert r is not None and r.kind == "redo" and r.label == cs.label
                assert effective(self.doc) == after_doc
                assert r.records == cs.records and r.select == cs.select and len(r.nodes) == len(cs.nodes)
        else:
            assert cs.records == frozenset() and cs.remap is None and not cs.nodes
        return cs

    def fails(self, key: str, fn: Callable[[EditSession], Any]) -> None:
        before = effective(self.doc)
        gen = self.s.state.gen
        with pytest.raises(EditError) as exc:
            fn(self.s)
        assert exc.value.key == key, exc.value.key
        assert self.s.state.gen == gen and effective(self.doc) == before

    def finish(self) -> None:
        final = effective(self.doc)
        n = 0
        while self.s.can_undo:
            assert self.s.undo() is not None
            n += 1
        assert n == self.steps
        assert effective(self.doc) == self.original
        assert not self.s.dirty and self.s.undo() is None
        while self.s.can_redo:
            self.s.redo()
        assert effective(self.doc) == final
