"""JSON Patch round trips (docs/ARCHITECTURE.md §12.8): random edit sequences made with the EditSession (every
command, record level, keyed renames, moves between records and levels, duplicates, wrapper edits, undo/redo)
-> ``export_patch`` -> the patch applied to a fresh copy of the ORIGINAL file with ``apply_patch`` AND with the
independent reference applier of ``jsonpatch_helpers`` over the parsed original -> both equal the edited
document (exact numbers, Unicode; objects compared by key: JSON Patch does not represent member order).
``diff=False`` exports too (the wrapper's deleted members are the documented gap: compared against the
reference instead). Both core backends.
"""

from __future__ import annotations

import logging
import random
import shutil
from pathlib import Path
from typing import Any

import pytest

from edits_helpers import write_unique_doc
from jsonpatch_helpers import canon, doc_value, load_patch, original_value, random_edits, ref_apply
from jsonreshape import editops, jsonpatch, reader
from jsonreshape.edits import EditSession
from jsonreshape.reader import Document

KINDS = ("array", "jsonl", "stream", "brace_list", "keyed", "wrapped", "wrapped_obj", "single")


@pytest.fixture
def backend(core_backend):
    name = "rust" if core_backend.__name__.endswith("_rscore") else "python"
    old = reader.set_scanner_backend(name)
    old_ops = editops.set_core_backend(name)
    old_jp = jsonpatch.set_core_backend(name)
    yield name
    reader.set_scanner_backend(old)
    editops.set_core_backend(old_ops)
    jsonpatch.set_core_backend(old_jp)


@pytest.fixture(autouse=True)
def _quiet_export_warnings():
    logging.disable(logging.WARNING)       # the diff=False gap warnings are logged; asserted where they matter
    yield
    logging.disable(logging.NOTSET)


def run_round_trip(tmp_path: Path, kind: str, seed: int, *, steps: int = 14, n: int = 5) -> None:
    rng = random.Random(seed * 7919 + len(kind))
    style = rng.choice(["compact", "pretty2", "pretty4", "tab"])
    src = tmp_path / "src.json"
    kw = write_unique_doc(src, kind, n=n, seed=seed, style=style)
    doc = Document.open(src, **kw)
    doc.build_index()
    s = EditSession.open(doc)
    opened: list[Any] = []
    try:
        random_edits(s, doc, rng, steps)
        final = doc_value(doc)
        orig = original_value(doc)
        for diff in (True, False):
            patch = tmp_path / f"patch-{diff}.json"
            s.export_patch(patch, diff=diff)
            ops = load_patch(patch)
            ref = ref_apply(orig, ops)
            gap = not diff and kind.startswith("wrapped")
            if not gap:
                assert canon(ref) == canon(final), f"reference applier, diff={diff}"
            copy = tmp_path / f"copy-{diff}.json"
            shutil.copy(src, copy)
            d2 = Document.open(copy, **kw)
            opened.append(d2)
            d2.build_index()
            s2 = EditSession.open(d2)
            opened.append(s2)
            s2.import_patch(patch)
            got = doc_value(d2)
            assert canon(got) == canon(ref), f"apply_patch vs reference, diff={diff}"
            if gap:
                records = "data"
                assert canon(got[records]) == canon(final[records])
            # undo of the imported patch gives the original back
            s2.undo()
            assert canon(doc_value(d2)) == canon(orig)
    finally:
        for obj in reversed(opened):
            obj.close()
        s.close()
        doc.close()


@pytest.mark.parametrize("kind", KINDS)
@pytest.mark.parametrize("seed", range(3))
def test_random_edits_round_trip(tmp_path: Path, backend: str, kind: str, seed: int) -> None:
    run_round_trip(tmp_path, kind, seed)


@pytest.mark.slow
@pytest.mark.parametrize("kind", KINDS)
@pytest.mark.parametrize("seed", range(3, 15))
def test_random_edits_round_trip_more(tmp_path: Path, kind: str, seed: int) -> None:
    run_round_trip(tmp_path, kind, seed, steps=24, n=8)
