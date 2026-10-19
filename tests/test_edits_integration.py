"""EditSession with the real bulk operations (§12.6) and Save / Save As (§12.7): the delegation of §12.5.

Bulk previews are computed on the session's state without its command lock and applied as ONE step (undo restores
the exact bytes; a preview of an older state is refused); save writes the effective document of the session's
state (in place with ``.bak``, or Save As leaving the source untouched), discards the layer and closes the session;
the saved file reopens (``SaveReport.reopen_options``) to exactly the edited content.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from edits_helpers import Model, effective, open_doc
from jsonreshape.edits import EditSession, Ref
from jsonreshape.editstore import find_pending
from jsonreshape.errors import EditError
from jsonreshape.reader import Document

bulk = pytest.importorskip("jsonreshape.bulk")
saver = pytest.importorskip("jsonreshape.saver")


@pytest.fixture
def closing():
    objs: list[Any] = []
    yield objs
    for obj in reversed(objs):
        try:
            obj.close()
        except Exception:
            pass


@pytest.mark.parametrize("kind", ["array", "keyed", "jsonl", "wrapped", "wrapped_obj", "stream"])
def test_bulk_preview_apply_undo(tmp_path, closing, kind) -> None:
    doc = open_doc(tmp_path / "src.json", kind, n=10, seed=11, style="pretty2")
    closing.append(doc)
    s = EditSession.open(doc)
    closing.append(s)
    original = effective(doc)
    s.set_value(Ref(2, ("name",)), '"Toshkent shahri"')
    edited = effective(doc)
    spec = bulk.BulkSpec("replace", {"find": "Toshkent", "replacement": "Samarqand", "mode": "text"})
    pv = s.bulk_preview(spec)
    assert pv.gen == s.state.gen and pv.records_changed >= 1
    s.set_value(Ref(0, ("id",)), "1000")
    with pytest.raises(EditError) as exc:
        s.bulk_apply(pv)                                  # computed on an older state
    assert exc.value.key == "edits.error.stale_refs"
    pv.discard()
    s.undo()
    pv = s.bulk_preview(spec)
    cs = s.bulk_apply(pv)
    assert cs.kind == "bulk" and s.undo_label and s.state.version == 2
    names = [Model.of(doc).root(i).pairs[1][1] for i in range(doc.record_count)]
    assert not any("Toshkent" in n for n in names) and names[2] == "Samarqand shahri"
    if pv.layer is not None:
        assert (s.store.dir / pv.layer).exists()          # the applied preview's files belong to the layer
    u = s.undo()
    assert u.kind == "undo" and effective(doc) == edited
    s.redo()
    s.undo()
    s.undo()
    assert effective(doc) == original


def test_bulk_dedupe_is_a_record_structure_step(tmp_path, closing) -> None:
    doc = open_doc(tmp_path / "src.json", "keyed", n=12, seed=2)
    closing.append(doc)
    s = EditSession.open(doc)
    closing.append(s)
    before = Model.of(doc)
    pv = s.bulk_preview(bulk.BulkSpec("dedupe", {"key": "num", "keep": "first"}))
    if not pv.records_changed:
        pytest.skip("no duplicates in this sample")
    cs = s.bulk_apply(pv)
    assert cs.remap is not None and doc.record_count == len(before.records) - pv.records_changed
    keys = [k for k, _v in Model.of(doc).records]
    assert s.key_index.find(keys[-1]) == len(keys) - 1
    s.undo()
    assert Model.of(doc) == before


@pytest.mark.parametrize("kind", ["array", "keyed", "jsonl", "wrapped", "brace_list", "single"])
def test_save_as_and_in_place(tmp_path, closing, kind) -> None:
    doc = open_doc(tmp_path / "src.json", kind, n=6, seed=9, style="pretty2")
    closing.append(doc)
    src_bytes = Path(doc.source_path).read_bytes()
    s = EditSession.open(doc)
    rec = 0
    s.set_value(Ref(rec, (0, "name") if kind == "single" else ("name",)), '"saqlandi ‘ok’"')
    if kind != "single":
        s.insert(None, 1, '{"yangi": [1e400, 0.1]}', key="yangi" if doc.member_records else None)
        s.delete_records([3])
    if kind == "wrapped":
        s.set_value(Ref(None, ("meta",)), '{"saved": true}')
    want = Model.of(doc)
    target = tmp_path / "copy.json"
    report = s.save(target)
    assert s.closed and find_pending(doc) == []
    assert Path(doc.source_path).read_bytes() == src_bytes        # Save As leaves the source alone
    saved = Document.open(target, **report.reopen_options)
    closing.append(saved)
    saved.build_index()
    got = Model.of(saved)
    assert got.records == want.records and got.wrapper == want.wrapper
    # in place: a new session on the copy
    s2 = EditSession.open(saved)
    s2.set_value(Ref(0, (0, "id") if kind == "single" else ("id",)), "42")
    expected2 = Model.of(saved)
    rep2 = s2.save()
    assert s2.closed and rep2.backup is not None and Path(rep2.backup).exists()
    reopened = Document.open(target, **rep2.reopen_options)
    closing.append(reopened)
    reopened.build_index()
    assert Model.of(reopened).records == expected2.records
