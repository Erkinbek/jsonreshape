"""Helpers of the JSON Patch tests (tests/test_jsonpatch*.py, tests/test_cli_patch.py).

* :func:`ref_apply` -- an independent, pure-Python RFC 6902 / RFC 6901 applier over a parsed document value
  (no byte-level code, no jsonreshape module but ``codec`` for exact number tokens): the oracle of the
  import tests. Test equality is by exact decimal value (``Decimal``), deep, no type coercion.
* :func:`doc_value` / :func:`original_value` -- the patched value of a document as RFC 6902 sees it (§12.8
  "Pointers"): a list of records (array / jsonl / stream / brace_list), a dict of records (keyed), the root
  value with the records inside (wrapped), the root value (single); effective (``doc.pinned()``) or original
  (``doc.pinned(None)``).
* :func:`canon` -- a canonical, order-free form for comparisons (objects by key: JSON Patch does not represent
  member order; arrays ordered; numbers by their exact token; strings exactly).
* :func:`random_edits` -- random EditSession commands (every command kind, record level included, undo/redo)
  driven by ``edits_helpers.Model``; refused commands are skipped.
"""

from __future__ import annotations

import copy
import json
import random
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Iterable

from edits_helpers import Model, N, O, dump, unique_value, _Counter
from jsonreshape import codec
from jsonreshape.edits import EditSession, Ref
from jsonreshape.errors import EditError, JsonReshapeError
from jsonreshape.reader import Document, split_member


class RefPatchError(Exception):
    """The reference applier refuses the patch."""


# ----------------------------------------------------------------------------------
# the patched value of a document
# ----------------------------------------------------------------------------------
def _records(view: Document) -> list[tuple[str | None, Any]]:
    out = []
    member = bool(view.member_records)
    for _pos, b in view.iter_record_bytes(include_huge=True):
        assert b is not None
        if member:
            key, vo = split_member(b)
            out.append((key, codec.loads(b[vo:])))
        else:
            out.append((None, codec.loads(b)))
    return out


def _value_of(view: Document) -> Any:
    kind = view.kind
    if kind == "single":
        return _records(view)[0][1]
    if kind == "wrapped":
        return codec.loads(b"".join(view.node_chunks(view.root())))
    recs = _records(view)
    if view.member_records:
        out: dict = {}
        for key, v in recs:
            out[key] = v
        return out
    return [v for _k, v in recs]


def doc_value(doc: Document) -> Any:
    """The effective value of the state the document shows."""
    return _value_of(doc.pinned())


def original_value(doc: Document) -> Any:
    """The value of the file as it is (no edits)."""
    return _value_of(doc.pinned(None))


# ----------------------------------------------------------------------------------
# canonical form
# ----------------------------------------------------------------------------------
def canon(v: Any) -> Any:
    if isinstance(v, dict):
        return ("o", tuple(sorted((k, canon(x)) for k, x in v.items())))
    if isinstance(v, list):
        return ("a", tuple(canon(x) for x in v))
    if isinstance(v, bool):
        return ("b", v)
    if v is None:
        return ("z",)
    if isinstance(v, str):
        return ("s", v)
    return ("n", codec.number_text(v))


# ----------------------------------------------------------------------------------
# the reference applier (RFC 6902, RFC 6901)
# ----------------------------------------------------------------------------------
def ref_pointer(text: str) -> list[str]:
    if text == "":
        return []
    if not text.startswith("/"):
        raise RefPatchError(f"bad pointer {text!r}")
    out = []
    for part in text[1:].split("/"):
        i = part.find("~")
        while i >= 0:
            if i + 1 >= len(part) or part[i + 1] not in "01":
                raise RefPatchError(f"bad escape in {text!r}")
            i = part.find("~", i + 2)
        out.append(part.replace("~1", "/").replace("~0", "~"))
    return out


def _index(seg: str, n: int, *, end: bool) -> int:
    if seg == "-" and end:
        return n
    if not seg or not seg.isdigit() or not seg.isascii() or (len(seg) > 1 and seg[0] == "0"):
        raise RefPatchError(f"bad index {seg!r}")
    i = int(seg)
    if i > n or (i == n and not end):
        raise RefPatchError(f"index {i} out of range {n}")
    return i


def _walk(doc: Any, segs: list[str]) -> Any:
    cur = doc
    for seg in segs:
        if isinstance(cur, dict):
            if seg not in cur:
                raise RefPatchError(f"missing {seg!r}")
            cur = cur[seg]
        elif isinstance(cur, list):
            cur = cur[_index(seg, len(cur), end=False)]
        else:
            raise RefPatchError("not a container")
    return cur


def _num(v: Any) -> Decimal:
    try:
        return Decimal(codec.number_text(v))
    except InvalidOperation:   # pragma: no cover
        return Decimal("NaN")


def ref_equal(a: Any, b: Any) -> bool:
    if isinstance(a, dict) or isinstance(b, dict):
        return (isinstance(a, dict) and isinstance(b, dict) and a.keys() == b.keys()
                and all(ref_equal(a[k], b[k]) for k in a))
    if isinstance(a, list) or isinstance(b, list):
        return (isinstance(a, list) and isinstance(b, list) and len(a) == len(b)
                and all(ref_equal(x, y) for x, y in zip(a, b)))
    if isinstance(a, bool) or isinstance(b, bool):
        return type(a) is type(b) and a == b
    if a is None or b is None:
        return a is None and b is None
    if isinstance(a, str) or isinstance(b, str):
        return isinstance(a, str) and isinstance(b, str) and a == b
    return _num(a) == _num(b)


def _add(doc: Any, segs: list[str], value: Any) -> Any:
    if not segs:
        return value
    parent = _walk(doc, segs[:-1])
    last = segs[-1]
    if isinstance(parent, dict):
        parent[last] = value
    elif isinstance(parent, list):
        parent.insert(_index(last, len(parent), end=True), value)
    else:
        raise RefPatchError("add to a scalar")
    return doc


def _remove(doc: Any, segs: list[str]) -> tuple[Any, Any]:
    if not segs:
        raise RefPatchError("remove the root")
    parent = _walk(doc, segs[:-1])
    last = segs[-1]
    if isinstance(parent, dict):
        if last not in parent:
            raise RefPatchError("remove: missing")
        return doc, parent.pop(last)
    if isinstance(parent, list):
        return doc, parent.pop(_index(last, len(parent), end=False))
    raise RefPatchError("remove from a scalar")


def ref_apply(doc: Any, ops: Iterable[dict]) -> Any:
    """Apply ``ops`` (parsed operation objects) to a deep copy of ``doc``; :class:`RefPatchError` on failure."""
    doc = copy.deepcopy(doc)
    for op in ops:
        name = op["op"]
        path = ref_pointer(op["path"])
        if name == "add":
            doc = _add(doc, path, copy.deepcopy(op["value"]))
        elif name == "remove":
            doc, _v = _remove(doc, path)
        elif name == "replace":
            _walk(doc, path)
            if not path:
                doc = copy.deepcopy(op["value"])
            else:
                doc, _v = _remove(doc, path)
                doc = _add(doc, path, copy.deepcopy(op["value"]))
        elif name == "move":
            frm = ref_pointer(op["from"])
            if len(frm) < len(path) and path[:len(frm)] == frm:
                raise RefPatchError("move into itself")
            if frm == path:
                _walk(doc, frm)
                continue
            doc, v = _remove(doc, frm)
            doc = _add(doc, path, v)
        elif name == "copy":
            v = copy.deepcopy(_walk(doc, ref_pointer(op["from"])))
            doc = _add(doc, path, v)
        elif name == "test":
            if not ref_equal(_walk(doc, path), op["value"]):
                raise RefPatchError("test failed")
        else:
            raise RefPatchError(f"unknown op {name!r}")
    return doc


def load_patch(path: Path) -> list[dict]:
    return codec.loads(Path(path).read_bytes())


# ----------------------------------------------------------------------------------
# random edits
# ----------------------------------------------------------------------------------
_KEYS = ["a", "b", "x/y", "t~1", "", "kalit o'zbek", "ключ", "e "]


def _rand_json(rng: random.Random, c: _Counter) -> str:
    return dump(unique_value(rng, c, rng.randint(1, 3))).decode("ascii")


def random_edits(s: EditSession, doc: Document, rng: random.Random, steps: int, *,
                 records: bool = True) -> int:
    """``steps`` random commands (refused ones are skipped); returns the number that changed something."""
    c = _Counter()
    c.n = 10_000 + rng.randint(0, 1000) * 100
    done = 0
    for _ in range(steps):
        md = Model.of(doc)
        refs = md.refs(wrapper=md.kind == "wrapped")
        conts = [r for r in refs if isinstance(md_node(md, r), (O, list))]
        nrec = len(md.records)
        kinds = ["set", "set", "rename", "insert", "delete", "duplicate", "move", "move_by", "type", "undo",
                 "redo"]
        if records and md.kind != "single":
            kinds += ["ins_rec", "del_rec", "dup_rec", "move_rec", "del_many", "dup_many"]
            if md.member:
                kinds += ["rename_rec", "rename_rec"]
        kind = rng.choice(kinds)
        version = s.state.version
        try:
            if kind == "set" and refs:
                s.set_value(rng.choice(refs), _rand_json(rng, c))
            elif kind == "rename":
                cands = [r for r in refs if r.ipath and isinstance(md_node(md, Ref(r.record, r.path[:-1],
                                                                                  r.ipath[:-1])), O)]
                if cands:
                    s.rename_key(rng.choice(cands), rng.choice(_KEYS) + str(rng.randint(0, 50)))
            elif kind == "insert" and conts:
                parent = rng.choice(conts)
                node = md_node(md, parent)
                n = len(node.pairs) if isinstance(node, O) else len(node)
                key = (rng.choice(_KEYS) + str(rng.randint(0, 99))) if isinstance(node, O) else None
                s.insert(parent, rng.randint(0, n), _rand_json(rng, c), key=key)
            elif kind == "delete":
                cands = [r for r in refs if r.ipath]
                if cands:
                    s.delete(rng.sample(cands, min(len(cands), rng.randint(1, 3))))
            elif kind == "duplicate":
                cands = [r for r in refs if r.ipath]
                if cands:
                    s.duplicate([rng.choice(cands)])
            elif kind == "move" and conts:
                cands = [r for r in refs if r.ipath]
                if cands:
                    parent = rng.choice(conts)
                    node = md_node(md, parent)
                    n = len(node.pairs) if isinstance(node, O) else len(node)
                    s.move([rng.choice(cands)], parent, rng.randint(0, n))
            elif kind == "move_by":
                cands = [r for r in refs if r.ipath]
                if cands:
                    s.move_by([rng.choice(cands)], rng.choice([-1, 1]))
            elif kind == "type":
                cands = [r for r in refs if r.ipath]
                if cands:
                    s.change_type([rng.choice(cands)], rng.choice(["string", "array", "null", "number", "bool"]))
            elif kind == "undo":
                s.undo()
            elif kind == "redo":
                s.redo()
            elif kind == "ins_rec":
                key = (rng.choice(_KEYS) + str(rng.randint(0, 999))) if md.member else None
                s.insert(None, rng.randint(0, nrec), _rand_json(rng, c), key=key)
            elif kind == "del_rec" and nrec:
                s.delete([Ref(rng.randrange(nrec), ())])
            elif kind == "dup_rec" and nrec:
                s.duplicate([Ref(rng.randrange(nrec), ())])
            elif kind == "move_rec" and nrec:
                p = rng.randrange(nrec)
                s.move([Ref(p, ())], None, rng.randint(0, nrec))
            elif kind == "del_many" and nrec:
                s.delete_records(sorted(rng.sample(range(nrec), min(nrec, rng.randint(1, 3)))))
            elif kind == "dup_many" and nrec:
                s.duplicate_records(sorted(rng.sample(range(nrec), min(nrec, rng.randint(1, 3)))))
            elif kind == "rename_rec" and nrec:
                s.rename_key(Ref(rng.randrange(nrec), ()), "k" + rng.choice(_KEYS) + str(rng.randint(0, 999)))
        except (EditError, JsonReshapeError):
            continue
        if s.state.version != version:
            done += 1
    return done


def md_node(md: Model, ref: Ref) -> Any:
    return md.node(ref.record, ref.ipath)


def op_texts(path: Path) -> list[str]:
    """The operation lines of a written patch file (for readable assertions)."""
    return [line.rstrip(",") for line in Path(path).read_text(encoding="utf-8").splitlines()[1:-1]]


def ops_of(path: Path) -> list[dict]:
    return json.loads(Path(path).read_text(encoding="utf-8"))
