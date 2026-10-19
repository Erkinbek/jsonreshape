"""KeyIndex: record keys of member-record documents for the edit session (§12.5).

A 200 000-record keyed file (unique keys, repeated keys -- the last one wins --, escaped keys, Uzbek keys with
apostrophes, a lone-surrogate escape): ``find`` against a plain dict built from the file, uniqueness errors of
record-level key commands, free keys of duplicates, renames, deletes, undo/redo, the reader hook
(``doc.key_index`` used only for the state it describes), sorted runs spilled to the work folder (tiny memory
budget) and a mapped table file (removed by ``close``). Foreign steps (``commit_step``) without ``Step.keys`` are
followed by reading the changed records; a big step whose keys are unknown resets the index (rebuilt at the next
need); discard resets it too; a build error is raised once and the next need builds again.
"""

from __future__ import annotations

import json
import threading
import time
from pathlib import Path
from typing import Any

import pytest

from jsonreshape import edits
from jsonreshape.appdirs import work_dir
from jsonreshape.edits import EditSession, KeyIndex, Ref, key_hash
from jsonreshape.editstore import Step
from jsonreshape.errors import EditError
from jsonreshape.overlay import PieceTable
from jsonreshape.reader import Document
from jsonreshape.settings import Settings

N = 200_000


def special_key(i: int) -> str:
    if i % 5000 == 1:
        return f"esc\\u0041{i}"              # an escaped key token (decoded: "escA<i>")
    if i % 5000 == 2:
        return f"o‘zbek ko'cha {i}"
    if i % 50000 == 3:
        return "dup"                         # repeated: the last one wins
    if i == 7:
        return "lone\\ud800"                 # a lone surrogate escape
    return f"k{i}"


def decoded(i: int) -> str:
    return json.loads('"' + special_key(i).replace('"', '\\"') + '"')


@pytest.fixture(scope="module")
def big_file(tmp_path_factory) -> Path:
    path = tmp_path_factory.mktemp("keyindex") / "big.json"
    with open(path, "w", encoding="utf-8") as f:
        f.write("{\n")
        for i in range(N):
            f.write(f'  "{special_key(i)}": {{"v": {i}, "s": "q{i}"}}' + (",\n" if i < N - 1 else "\n"))
        f.write("}\n")
    return path


@pytest.fixture
def docs():
    opened: list[Any] = []
    yield opened
    for d in reversed(opened):
        d.close()


def expected_positions(keys: list[str]) -> dict[str, int]:
    out: dict[str, int] = {}
    for i, k in enumerate(keys):
        out[k] = i
    return out


def test_key_index_on_200k_records(big_file, docs, monkeypatch) -> None:
    monkeypatch.setattr(edits, "KEY_TABLE_MEMORY", 1 << 16)          # a mapped table file
    doc = Document.open(big_file, format="keyed")
    doc.build_index()
    docs.append(doc)
    s = EditSession.open(doc, settings=Settings(memory_limit_bytes=1))   # sorted runs spilled (65 536 entries each)
    docs.append(s)
    keys = [decoded(i) for i in range(N)]
    want = expected_positions(keys)
    t0 = time.perf_counter()
    ki = s.key_index
    assert isinstance(ki, KeyIndex) and not ki.started and not s.key_index_ready
    assert ki.find("k12345") == 12345
    build = time.perf_counter() - t0
    assert s.key_index_ready and doc.key_index is ki.view() and doc.key_index.gen == doc.gen
    table = ki.view()._table
    tpath = table.path
    assert tpath is not None and tpath.exists() and tpath.parent == work_dir(None)
    for k in ("k0", "k199999", "escA1", "escA195001", "o‘zbek ko'cha 2", "dup", "lone\ud800", "k8"):
        assert ki.find(k) == want[k], k
    assert ki.find("dup") == want["dup"] == max(i for i, k in enumerate(keys) if k == "dup")
    assert ki.find("missing") is None and ki.find("esc\\u0041") is None
    # the reader uses the hook for the state it describes (no scan)
    orig = Document._find_record_by_key

    def no_scan(self, key, cancel):
        raise AssertionError("scanned although the key index describes this state")

    monkeypatch.setattr(Document, "_find_record_by_key", no_scan)
    assert doc.node(("k777",)).value()["v"] == 777
    monkeypatch.setattr(Document, "_find_record_by_key", orig)
    # record-level key commands
    with pytest.raises(EditError) as exc:
        s.insert(None, 0, "{}", key="k5")
    assert exc.value.key == "edits.error.key_exists"
    s.insert(None, 0, '{"v": -1}', key="yangi")
    assert ki.find("yangi") == 0 and ki.find("k5") == 6 and doc.key_index.gen == doc.gen
    s.duplicate([Ref(8, ())])                       # position 8 = record 7 (the lone surrogate key)
    assert ki.find(keys[7] + "_2") == 9 and ki.find(keys[7]) == 8
    with pytest.raises(EditError) as exc:
        s.rename_key(Ref(11, ()), "k12")
    assert exc.value.key == "edits.error.key_exists"
    s.rename_key(Ref(11, ()), "qayta nomlandi")
    assert ki.find("qayta nomlandi") == 11 and ki.find(keys[9]) is None
    last_dup = want["dup"] + 2
    s.delete([Ref(last_dup, ())])
    previous = max(i for i, k in enumerate(keys) if k == "dup" and i != want["dup"])
    assert ki.find("dup") == previous + 2
    s.move([Ref(3, ())], None, 0)                   # a record move keeps the keys
    assert ki.find(keys[2]) == 0
    for _ in range(5):
        s.undo()
    for k in ("k0", "dup", "escA1", "yangi", "k5"):
        assert ki.find(k) == want.get(k), k
    s.redo()
    assert ki.find("yangi") == 0
    s.close()
    assert not tpath.exists() and doc.key_index is None
    assert build < 60


def test_free_keys_and_uniqueness_on_a_small_document(tmp_path, docs) -> None:
    p = tmp_path / "k.json"
    p.write_bytes(b'{"a": 1, "a_2": 2, "b": 3, "a": 4}')
    doc = Document.open(p, format="keyed")
    doc.build_index()
    docs.append(doc)
    s = EditSession.open(doc)
    docs.append(s)
    s.duplicate([Ref(0, ())])
    assert [doc.record_key(i) for i in range(5)] == ["a", "a_3", "a_2", "b", "a"]
    s.duplicate_records([1, 3])
    assert [doc.record_key(i) for i in range(7)] == ["a", "a_3", "a_3_2", "a_2", "b", "b_2", "a"]
    s.paste(None, '{"x": 1, "y": 2}', as_records=True, index=0)
    with pytest.raises(EditError):
        s.paste(None, '{"x": 1}', as_records=True)
    assert s.key_index.find("a") == 8 and s.key_index.find("x") == 0


def test_foreign_steps_and_resets(tmp_path, docs, monkeypatch) -> None:
    p = tmp_path / "k.json"
    p.write_bytes(b'{' + b",".join(b'"k%d": {"v": %d}' % (i, i) for i in range(20)) + b'}')
    doc = Document.open(p, format="keyed")
    doc.build_index()
    docs.append(doc)
    s = EditSession.open(doc)
    docs.append(s)
    ki = s.key_index
    assert ki.find("k3") == 3
    # a foreign step without Step.keys that renames record 3 (small: the session reads the new keys)
    store = s.ensure_store()
    span = store.append(b'"zzz": {"v": 3}')
    rid = s.state.rid_at(3)
    s.commit_step(Step("patch", "edits.step.patch", {"count": 1}, content=[(rid, s.state.top.get(rid), span)],
                       records=[3]))
    assert ki.find("zzz") == 3 and ki.find("k3") is None and doc.node(("zzz",)).value() == {"v": 3}
    s.undo()
    assert ki.find("k3") == 3 and ki.find("zzz") is None
    # a big "pieces" step without keys: the index is reset and rebuilt
    old = s.state.pieces
    new = PieceTable.from_pairs([10, 0], [20, 10])
    s.commit_step(Step("patch", "edits.step.patch", {"count": 1}, struct=[("pieces", old, new)]))
    assert not ki.started and not s.key_index_ready and doc.key_index is None
    assert ki.find("k0") == 10 and ki.find("k19") == 9 and s.key_index_ready
    s.undo()
    assert ki.find("k0") == 0 or not ki.started
    assert ki.find("k0") == 0
    # discard resets as well
    s.set_value(Ref(0, ()), '{"edited": 1}')
    s.discard()
    assert not ki.started and doc.key_index is None
    assert ki.find("k19") == 19


def test_build_errors_are_raised_once(tmp_path, docs, monkeypatch) -> None:
    p = tmp_path / "k.json"
    p.write_bytes(b'{"a": 1, "b": 2}')
    doc = Document.open(p, format="keyed")
    doc.build_index()
    docs.append(doc)
    s = EditSession.open(doc)
    docs.append(s)
    real = edits._KeyTable.build
    calls = [0]

    def failing(cls, *a, **kw):
        calls[0] += 1
        if calls[0] == 1:
            raise OSError("disk trouble")
        return real.__func__(cls, *a, **kw)

    monkeypatch.setattr(edits._KeyTable, "build", classmethod(failing))
    with pytest.raises(OSError):
        s.insert(None, 0, "1", key="c")
    assert doc.record_count == 2
    s.insert(None, 0, "1", key="c")
    assert s.key_index.find("c") == 0 and calls[0] == 2


def test_lookups_from_another_thread_while_editing(tmp_path, docs) -> None:
    p = tmp_path / "k.json"
    p.write_bytes(b'{' + b",".join(b'"k%d": %d' % (i, i) for i in range(500)) + b'}')
    doc = Document.open(p, format="keyed")
    doc.build_index()
    docs.append(doc)
    s = EditSession.open(doc)
    docs.append(s)
    s.key_index.wait()
    stop = threading.Event()
    errors: list[BaseException] = []

    def reader() -> None:
        try:
            while not stop.is_set():
                view = s.key_index.view()
                if view is None:
                    continue
                pos = view.find("k250")
                pinned = doc.pinned(view.state)
                assert pos is not None and pinned.record_key(pos) == "k250"
        except BaseException as exc:  # pragma: no cover - reported below
            errors.append(exc)

    th = threading.Thread(target=reader)
    th.start()
    try:
        for i in range(30):
            s.insert(None, 0, str(i), key=f"new{i}")
            if i % 3 == 0:
                s.undo()
                s.redo()
    finally:
        stop.set()
        th.join(30)
    assert not errors
    assert s.key_index.find("k250") == 280


def test_key_hash_matches_raw_token_hash() -> None:
    for key in ("", "k1", "O‘zbekiston", "Ўзбек", "emoji 😀", "e x"):
        raw = key.encode("utf-8")
        assert edits._raw_key_hash(raw) == key_hash(key)
        data = json.dumps(key, ensure_ascii=False).encode("utf-8") + b": 1"
        assert edits._member_hash(data, 0, len(data)) == key_hash(key)
        esc = json.dumps(key, ensure_ascii=True).encode("ascii") + b": 1"
        assert edits._member_hash(esc, 0, len(esc)) == key_hash(key)
    lone = b'"a\\ud800": 1'
    assert edits._member_hash(lone, 0, len(lone)) == key_hash("a\ud800")
