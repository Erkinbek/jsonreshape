"""EditSession across processes: crash recovery and the layer lock (§12.5, acceptance test 21 core part).

A SUBPROCESS opens a document, runs a deterministic random sequence of commands (values, record inserts and
deletes, duplicates, moves between records, undo, redo) and prints, after every command, the head and a digest of
the effective document (every record + the root text). Then it dies with ``os._exit`` -- after the last command,
or INSIDE a durability point of the edit store (``editstore._crash_point``: before the COMMIT of a step, right after
it, inside ``set_head``). The parent reopens the document and the session (``restore=True``): the state must be
exactly the one after the last committed step (the digest the child printed for that head; a step whose COMMIT
happened just before the death is one head further), and undo must walk back through every earlier head with the
exact digests the child printed. A live child holding a session makes the parent's session ``edits.error.locked``;
when the child closes it, the parent can edit.
"""

from __future__ import annotations

import hashlib
import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

import pytest

from edits_helpers import effective, open_doc, write_unique_doc
from jsonreshape import editstore
from jsonreshape.edits import EditSession, Ref
from jsonreshape.errors import EditError
from jsonreshape.reader import Document

ROOT = Path(__file__).resolve().parents[1]
TESTS = Path(__file__).resolve().parent

CHILD = r'''
import hashlib, os, random, sys
sys.path[:0] = [{root!r}, {tests!r}]
from jsonreshape.reader import Document
from jsonreshape import editstore as es
from jsonreshape.edits import EditSession, Ref
from edits_helpers import Model, effective

path, fmt, seed, n, point, nth = sys.argv[1], sys.argv[2], int(sys.argv[3]), int(sys.argv[4]), sys.argv[5], \
    int(sys.argv[6])
doc = Document.open(path, format=fmt, record_path="data" if fmt == "wrapped" else None)
doc.build_index()
s = EditSession.open(doc)


def digest():
    recs, root = effective(doc)
    h = hashlib.sha256()
    for r in recs:
        h.update(len(r).to_bytes(8, "little") + r)
    h.update(root)
    return h.hexdigest()


if point != "-":
    hits = [0]

    def crash(name):
        if name == point:
            hits[0] += 1
            if hits[0] == nth:
                os._exit(3)

    es._crash_point = crash
rng = random.Random(seed)
print("ready", s.state.version, digest(), flush=True)
for i in range(n):
    md = Model.of(doc)
    act = rng.random()
    nrec = len(md.records)
    if act < 0.12 and s.can_undo:
        s.undo()
    elif act < 0.2 and s.can_redo:
        s.redo()
    elif act < 0.45 or nrec < 2:
        refs = md.refs()
        r = refs[rng.randrange(len(refs))]
        s.set_value(r, '{{"v": %d, "t": "O\'zbekiston %d"}}' % (i, rng.randrange(10 ** 6)))
    elif act < 0.6:
        if md.member:
            s.insert(None, rng.randrange(nrec + 1), '[%d]' % i, key="k%d_%d" % (seed, i))
        else:
            s.insert(None, rng.randrange(nrec + 1), '[%d]' % i)
    elif act < 0.7:
        s.delete_records([rng.randrange(nrec)])
    elif act < 0.8:
        refs = md.refs(with_roots=False)
        if refs:
            s.duplicate([refs[rng.randrange(len(refs))]])
        else:
            s.duplicate_records([0])
    else:
        a, b = rng.sample(range(nrec), 2)
        refs = [r for r in md.refs(with_roots=False) if r.record == a]
        targets = [r for r in md.refs(containers=True) if r.record == b]
        if refs and targets:
            s.move([refs[0]], targets[rng.randrange(len(targets))], None)
        else:
            s.move([Ref(a, ())], None, rng.randrange(nrec))
    print("done", i, s.state.version, digest(), flush=True)
print("finished", flush=True)
os._exit(0)
'''

LOCK_CHILD = r'''
import os, sys
sys.path[:0] = [{root!r}, {tests!r}]
from jsonreshape.reader import Document
from jsonreshape.edits import EditSession, Ref
doc = Document.open(sys.argv[1])
doc.build_index()
s = EditSession.open(doc)
s.set_value(Ref(0, ("id",)), "777")
print("held", flush=True)
sys.stdin.readline()
s.close()
print("closed", flush=True)
sys.stdin.readline()
os._exit(0)
'''


def script(tmp_path: Path, text: str, name: str) -> Path:
    p = tmp_path / name
    p.write_text(text.format(root=str(ROOT), tests=str(TESTS)), encoding="utf-8")
    return p


def run_child(script_path: Path, *args: Any, timeout: float = 120) -> list[str]:
    proc = subprocess.run([sys.executable, "-I", str(script_path), *map(str, args)], capture_output=True, text=True,
                          timeout=timeout, env=dict(os.environ), stdin=subprocess.DEVNULL)
    lines = [line.strip() for line in proc.stdout.splitlines()]
    assert lines and lines[0].startswith("ready"), (proc.returncode, proc.stderr[-3000:])
    if proc.returncode not in (0, 3):
        raise AssertionError(proc.stderr[-3000:])
    return lines


def digest_of(doc: Document) -> str:
    recs, root = effective(doc)
    h = hashlib.sha256()
    for r in recs:
        h.update(len(r).to_bytes(8, "little") + r)
    h.update(root)
    return h.hexdigest()


def heads(lines: list[str]) -> tuple[dict[int, str], int, str]:
    """``(digest per head on the final undo stack, last head, last digest)`` from the child's lines."""
    _ready, v0, d0 = lines[0].split()
    per: dict[int, str] = {int(v0): d0}
    last = (int(v0), d0)
    for line in lines[1:]:
        if line.startswith("done"):
            _d, _i, v, d = line.split()
            per[int(v)] = d
            last = (int(v), d)
    return per, last[0], last[1]


def check_restored(path: Path, fmt: str, lines: list[str], *, extra: int = 0) -> None:
    per, head, dig = heads(lines)
    doc = Document.open(path, format=fmt, record_path="data" if fmt == "wrapped" else None)
    try:
        doc.build_index()
        s = EditSession.open(doc)
        try:
            v = s.state.version
            assert v == head + extra, (v, head, extra)
            if v == head:
                assert digest_of(doc) == dig
            else:
                s.undo()
                assert digest_of(doc) == dig      # the step committed just before the death
            while s.can_undo:
                s.undo()
                assert digest_of(doc) == per[s.state.version], s.state.version
            assert s.state.version == 0 and digest_of(doc) == per[0]
            while s.can_redo:
                s.redo()
            top = digest_of(doc)
            if doc.record_count:               # editing goes on after the recovery
                s.set_value(Ref(0, ()), '{"after": "recovery"}')
                assert digest_of(doc) != top
                s.undo()
                assert digest_of(doc) == top
        finally:
            s.close()
    finally:
        doc.close()


@pytest.mark.parametrize("fmt", ["array", "keyed", "jsonl", "wrapped"])
def test_recovery_after_os_exit(tmp_path, fmt) -> None:
    path = tmp_path / "src.json"
    write_unique_doc(path, fmt, n=5, seed=3)
    child = script(tmp_path, CHILD, "child.py")
    lines = run_child(child, path, fmt, 11, 25, "-", 0)
    assert lines[-1] == "finished"
    check_restored(path, fmt, lines)


@pytest.mark.parametrize("point, nth, extra", [("commit", 5, 0), ("commit.done", 4, 1), ("set_head", 2, 0),
                                               ("append", 6, 0)])
def test_recovery_after_a_crash_inside_the_store(tmp_path, point, nth, extra) -> None:
    path = tmp_path / "src.json"
    write_unique_doc(path, "array", n=6, seed=8)
    child = script(tmp_path, CHILD, "child.py")
    lines = run_child(child, path, "array", 21, 40, point, nth)
    assert lines[-1] != "finished", "the crash point was not reached"
    # a set_head crash: the undo/redo did not happen, the head is the previous one
    check_restored(path, "array", lines, extra=extra)


def test_lock_is_held_across_processes(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(editstore, "LOCK_RETRY", 0.3)
    doc = open_doc(tmp_path / "a.json", "array")
    try:
        child = script(tmp_path, LOCK_CHILD, "lock_child.py")
        proc = subprocess.Popen([sys.executable, "-I", str(child), str(doc.source_path)], stdin=subprocess.PIPE,
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, env=dict(os.environ))
        try:
            assert proc.stdout.readline().strip() == "held", proc.stderr.read() if proc.poll() is not None else ""
            with pytest.raises(EditError) as exc:
                EditSession.open(doc)
            assert exc.value.key == "edits.error.locked"
            proc.stdin.write("\n")
            proc.stdin.flush()
            assert proc.stdout.readline().strip() == "closed"
            s = EditSession.open(doc)          # the child's layer (one step) is restored
            assert s.dirty and s.state.version == 1 and doc.record(0)["id"] == 777
            s.undo()
            s.close()
        finally:
            if proc.poll() is None:
                proc.stdin.write("\n")
                proc.stdin.flush()
            proc.wait(30)
    finally:
        doc.close()


def test_lock_of_a_killed_process_is_free(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(editstore, "LOCK_RETRY", 0.3)
    doc = open_doc(tmp_path / "a.json", "jsonl")
    try:
        child = script(tmp_path, LOCK_CHILD, "lock_child.py")
        proc = subprocess.Popen([sys.executable, "-I", str(child), str(doc.source_path), "jsonl"], stdin=subprocess.PIPE,
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, env=dict(os.environ))
        assert proc.stdout.readline().strip() == "held"
        proc.kill()
        proc.wait(30)
        deadline = time.monotonic() + 10
        while True:
            try:
                s = EditSession.open(doc)
                break
            except EditError as exc:
                if exc.key != "edits.error.locked" or time.monotonic() > deadline:
                    raise
        assert s.dirty and doc.record(0)["id"] == 777
        s.close()
    finally:
        doc.close()
