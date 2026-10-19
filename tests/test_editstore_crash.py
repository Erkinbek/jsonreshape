"""jsonreshape.editstore across processes: crash consistency and inter-process locks (§12.2, test 21 core part).

A SUBPROCESS runs a deterministic random edit sequence (``tests/editstore_helpers.Driver``: point edits,
inserts, deletes, moves, duplicates, bulk array layers in their own segments, wrapper edits, multi-op and
"pieces" steps, undo/redo, trimming into checkpoints) and dies

* by ``TerminateProcess`` / ``kill`` at a random moment, or
* by ``os._exit`` at a chosen durability point (``editstore._crash_point``: after an append, inside the
  commit transaction before COMMIT, right after it, inside set_head / trim / discard).

The parent replays the same seed in a scratch layer to know every state the child passed through. The
reopened layer must equal the state after the last action the child reported or the next one (whose
commit may have become durable just before the death), never reference missing bytes (``load_state`` and
reading every record succeed; the whole history ``checkpoint .. head`` loads), keep working (undo + a new
commit), and contain no garbage files afterwards.
"""

from __future__ import annotations

import os
import random
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

import pytest

import jsongen
from editstore_helpers import Driver, model_of, signature
from jsonreshape import editstore as es
from jsonreshape.editstore import DB_NAME, EditStore, LayerLock, Step, apply_step, find_pending, layer_key, undo_step
from jsonreshape.errors import EditError
from jsonreshape.overlay import segment_name
from jsonreshape.reader import Document

ROOT = Path(__file__).resolve().parents[1]
TESTS = Path(__file__).resolve().parent

CHILD = r'''
import os, sys
sys.path[:0] = [{root!r}, {tests!r}]
from jsonreshape.reader import Document
from jsonreshape import editstore as es
from editstore_helpers import Driver

path, seed, n, limit, point, nth = sys.argv[1], int(sys.argv[2]), int(sys.argv[3]), int(sys.argv[4]), \
    sys.argv[5], int(sys.argv[6])
doc = Document.open(path)
doc.build_index()
store = es.EditStore.open(doc)
if point != "-":
    hits = [0]

    def crash(name):
        if name == point:
            hits[0] += 1
            if hits[0] == nth:
                os._exit(3)

    es._crash_point = crash
drv = Driver(doc, store, seed, limit=limit if limit >= 0 else None)
print("ready", flush=True)
for i in range(n):
    drv.action()
    print("done", i, flush=True)
if point.startswith("discard"):
    store.discard()
print("finished", flush=True)
sys.stdin.readline()
os._exit(0)
'''

LOCK_CHILD = r'''
import os, sys
sys.path[:0] = [{root!r}, {tests!r}]
from jsonreshape.reader import Document
from jsonreshape import editstore as es

doc = Document.open(sys.argv[1])
doc.build_index()
store = es.EditStore.open(doc)
if sys.argv[2] == "commit":
    state = store.load_state()
    span = store.append(b'{{"child":1}}')
    step = es.Step("edit", "edits.step.set_value", {{}}, content=[(0, None, span)])
    store.commit(step, state=state)
print("held", flush=True)
line = sys.stdin.readline().strip()
if line == "close":
    store.close()
    print("closed", flush=True)
    sys.stdin.readline()
os._exit(0)
'''


def write_script(tmp_path: Path, text: str, name: str) -> Path:
    p = tmp_path / name
    p.write_text(text.format(root=str(ROOT), tests=str(TESTS)), encoding="utf-8")
    return p


def start(script: Path, *args: Any) -> subprocess.Popen:
    return subprocess.Popen([sys.executable, "-I", str(script), *map(str, args)], stdin=subprocess.PIPE,
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, env=dict(os.environ))


def read_until(proc: subprocess.Popen, wanted: str, timeout: float = 60.0) -> list[str]:
    lines = []
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        line = proc.stdout.readline()     # type: ignore[union-attr]
        if not line:
            break
        lines.append(line.strip())
        if line.strip() == wanted:
            return lines
    err = proc.stderr.read() if proc.poll() is not None else ""  # type: ignore[union-attr]
    raise AssertionError(f"child did not print {wanted!r}: {lines[-5:]} {err[-2000:]}")


def finish(proc: subprocess.Popen) -> list[str]:
    """Wait for a dead child; the lines it printed before it died."""
    out, err = proc.communicate(timeout=60)
    return [line.strip() for line in out.splitlines()] + ([f"stderr: {err[-500:]}"] if err.strip() else [])


def last_done(lines: list[str]) -> int:
    done = [int(line.split()[1]) for line in lines if line.startswith("done ")]
    return done[-1] if done else -1


class Replay:
    """The parent's replay of a child's run: the state after every action."""

    def __init__(self, doc: Document, layer_dir: Path, seed: int, n: int, limit: int) -> None:
        store = EditStore.open(doc, dir=layer_dir)
        drv = Driver(doc, store, seed, limit=limit if limit >= 0 else None)
        self.snapshots = [(store.head, drv.model, dict(drv.models), store.checkpoint_seq)]
        for _ in range(n):
            drv.action()
            self.snapshots.append((store.head, drv.model, dict(drv.models), store.checkpoint_seq))
        self.base, self.raw = drv.base, drv.raw
        store.discard()

    def after(self, k: int) -> tuple[int, Any, dict, int]:
        """State after action ``k`` (``-1`` = before the first)."""
        return self.snapshots[k + 1]


def check_recovered(doc: Document, replay: Replay, k: int, n: int, *, discarded: bool = False) -> int:
    """The reopened layer equals the state after action k or k+1; returns which one."""
    if discarded:
        assert find_pending(doc) == []
        assert EditStore.open(doc, create=False) is None
        return k
    infos = find_pending(doc)
    store = EditStore.open(doc, create=False)
    try:
        if store is None:      # nothing durable yet, or every step undone: no pending layer
            assert infos == []
            assert any(replay.after(j)[0] == 0 for j in (k, min(k + 1, n - 1)))
            return k
        assert len(infos) <= 1
        loaded = store.load_state()
        got = model_of(loaded, replay.base, replay.raw)
        cands = [j for j in (k, k + 1) if j < n and replay.after(j)[0] == store.head and replay.after(j)[1] == got]
        assert cands, f"recovered head {store.head} matches neither action {k} nor {k + 1}"
        j = cands[0]
        if store.head:
            assert infos and infos[0].matches and infos[0].steps == store.head and infos[0].damaged is None
        models = replay.after(j)[2]
        for seq in range(store.checkpoint_seq, store.head + 1):
            assert model_of(store.load_state(upto=seq), replay.base, replay.raw) == models[seq], seq
        names = set(os.listdir(store.dir))
        allowed = {DB_NAME, DB_NAME + "-wal", segment_name(0)} | set(store._files)
        assert names <= allowed, names - allowed
        # the layer keeps working: undo, then a new step
        if store.head > store.checkpoint_seq:
            step = store.step(store.head)
            store.set_head(store.head - 1)
            loaded = undo_step(loaded, step, layers=store)
            assert model_of(loaded, replay.base, replay.raw) == models[store.head]
        span = store.append(b'{"after_crash":1}')
        if loaded.count:
            rid = loaded.rid_at(0)
            step = Step("edit", "edits.step.set_value", {}, content=[(rid, loaded.top.get(rid), span)])
            store.commit(step, state=loaded)
            after = apply_step(loaded, step, layers=store)
            store.close()
            store = EditStore.open(doc, create=False)
            assert store is not None and signature(store.load_state()) == signature(after)
        return j
    finally:
        if store is not None:
            store.close()


@pytest.fixture
def crash_doc(tmp_path: Path):
    p = tmp_path / "crash.json"
    jsongen.write_file(p, jsongen.records(16, seed=8), "array", style="pretty")
    doc = Document.open(p)
    doc.build_index()
    yield doc
    doc.close()


N_ACTIONS = 40


@pytest.mark.parametrize("seed", [1, 2, 3, 4, 5, 6])
def test_terminate_at_random_moments(tmp_path: Path, crash_doc: Document, seed: int) -> None:
    rng = random.Random(seed)
    n = N_ACTIONS if seed <= 4 else 120    # the long runs fold steps into checkpoints (limit 0)
    limit = 0 if seed > 4 else -1
    script = write_script(tmp_path, CHILD, "child.py")
    proc = start(script, crash_doc.source_path, seed, n, limit, "-", 0)
    read_until(proc, "ready")
    stop_after = rng.randrange(n // 2, n - 5) if seed > 4 else rng.randrange(n - 5)
    read_until(proc, f"done {stop_after}")
    time.sleep(rng.random() * 0.02)       # land somewhere inside a later action
    proc.kill()
    lines = finish(proc)
    k = max(stop_after, last_done(lines))
    replay = Replay(crash_doc, tmp_path / "replay", seed, n, limit)
    if seed > 4:
        assert replay.after(k)[3] > 0, "the run must have crossed a checkpoint before the kill"
    check_recovered(crash_doc, replay, k, n)


CRASH_POINTS = [
    ("append", 7),
    ("append", 23),
    ("commit", 5),
    ("commit", 17),
    ("commit.done", 9),
    ("commit.done", 26),
    ("set_head", 2),
    ("trim", 1),
    ("discard", 1),
    ("discard.marked", 1),
]


@pytest.mark.parametrize("point,nth", CRASH_POINTS)
def test_exit_at_durability_points(tmp_path: Path, crash_doc: Document, point: str, nth: int) -> None:
    seed = 100 + nth
    n = 110 if point == "trim" else N_ACTIONS
    limit = 0 if point == "trim" else -1
    script = write_script(tmp_path, CHILD, "child.py")
    proc = start(script, crash_doc.source_path, seed, n, limit, point, nth)
    lines = finish(proc)
    assert proc.returncode == 3, lines[-3:]          # it died at the crash point
    k = last_done(lines)
    replay = Replay(crash_doc, tmp_path / "replay", seed, n, limit)
    if point == "discard.marked":
        # the tombstone is durable: the layer is never offered again and is purged
        assert k == n - 1
        layer = es._edits_root() / layer_key(crash_doc)
        assert (layer / DB_NAME).exists()
        check_recovered(crash_doc, replay, k, n, discarded=True)
        assert not layer.exists()
        return
    j = check_recovered(crash_doc, replay, k, n)
    if point in ("append", "commit", "set_head"):
        assert j == k                                 # nothing of the interrupted action is durable
    if point == "commit.done":
        assert j == k + 1                             # the committed step survived


# ----------------------------------------------------------------------------------
# locks across processes
# ----------------------------------------------------------------------------------
def test_lock_held_by_another_process(tmp_path: Path, crash_doc: Document) -> None:
    key = layer_key(crash_doc)
    script = write_script(tmp_path, LOCK_CHILD, "lock_child.py")
    proc = start(script, crash_doc.source_path, "commit")
    try:
        read_until(proc, "held")
        assert LayerLock.is_locked(key)
        t0 = time.monotonic()
        with pytest.raises(EditError) as ei:
            EditStore.open(crash_doc)                   # retries LOCK_RETRY (2 s), then gives up
        assert ei.value.key == "edits.error.locked"
        assert time.monotonic() - t0 >= es.LOCK_RETRY - 0.05
        infos = find_pending(crash_doc)
        assert len(infos) == 1 and infos[0].locked and infos[0].matches and infos[0].damaged is None
        with pytest.raises(EditError):
            es.delete_layer(infos[0].dir)
        # the child closes normally: the lock is free again, its step is pending
        proc.stdin.write("close\n")                     # type: ignore[union-attr]
        proc.stdin.flush()                              # type: ignore[union-attr]
        read_until(proc, "closed")
        assert not LayerLock.is_locked(key)
        infos = find_pending(crash_doc)
        assert len(infos) == 1 and not infos[0].locked and infos[0].steps == 1
    finally:
        proc.kill()
        finish(proc)


def test_lock_released_when_the_holder_dies(tmp_path: Path, crash_doc: Document) -> None:
    key = layer_key(crash_doc)
    script = write_script(tmp_path, LOCK_CHILD, "lock_child.py")
    proc = start(script, crash_doc.source_path, "commit")
    read_until(proc, "held")
    lock = LayerLock(key)
    assert not lock.try_acquire()
    proc.kill()
    finish(proc)
    lock.acquire()                                      # the OS released the dead holder's lock
    try:
        infos = find_pending(crash_doc, lock=lock)
        assert len(infos) == 1 and infos[0].steps == 1 and not infos[0].locked
        store = EditStore.open(crash_doc, lock=lock)
        assert store.head == 1
        state = store.load_state()
        assert state.read(*state.content(0)) == b'{"child":1}'
        store.close()
    finally:
        lock.release()


def test_lock_only_child_creates_no_layer(tmp_path: Path, crash_doc: Document) -> None:
    script = write_script(tmp_path, LOCK_CHILD, "lock_child.py")
    proc = start(script, crash_doc.source_path, "lock-only")
    try:
        read_until(proc, "held")
        assert find_pending(crash_doc) == []           # nothing written: nothing pending, nothing created
        assert not (es._edits_root() / layer_key(crash_doc)).exists()
        assert not es.beside_dir(crash_doc).exists()
        with pytest.raises(EditError):
            LayerLock(layer_key(crash_doc)).acquire(timeout=0.2)
    finally:
        proc.kill()
        finish(proc)
