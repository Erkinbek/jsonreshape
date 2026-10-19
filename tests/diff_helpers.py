"""Helpers of the diff tests -- also for other suites: acceptance test 22 ("JSON -> JSONL -> JSON
does not change the data") checks a conversion with :func:`assert_same_data`.

* :func:`open_doc` -- ``Document.open`` + ``build_index``;
* :func:`diff_paths` -- open two files, :func:`jsonreshape.diff.diff_files`, close them; returns
  ``(record diffs, report)``;
* :func:`assert_same_data` -- the two files hold the same data (records equal in order, canonical
  equality: numbers by exact value, member order ignored; wrappers too), with a readable message;
* :func:`shape` -- record diffs as plain tuples (for comparing two runs).
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from jsonreshape import codec
from jsonreshape.diff import DiffReport, Omitted, RecordDiff, diff_files
from jsonreshape.paths import MISSING
from jsonreshape.reader import Document

__all__ = ["assert_same_data", "diff_paths", "open_doc", "shape"]


def open_doc(path: str | Path, **open_kw: Any) -> Document:
    doc = Document.open(path, **open_kw)
    doc.build_index()
    return doc


def diff_paths(path_a: str | Path, path_b: str | Path, *, open_a: dict | None = None, open_b: dict | None = None,
               **diff_kw: Any) -> tuple[list[RecordDiff], DiffReport]:
    """Diff two files (opened with ``open_a`` / ``open_b`` keyword arguments)."""
    a = open_doc(path_a, **(open_a or {}))
    b = open_doc(path_b, **(open_b or {}))
    try:
        rds: list[RecordDiff] = []
        rep = diff_files(a, b, on_record=rds.append, **diff_kw)
        return rds, rep
    finally:
        a.close()
        b.close()


def _text(v: Any) -> Any:
    if v is MISSING:
        return None
    if isinstance(v, Omitted):
        return ("omitted", v.kind, v.size)
    return codec.dumps(v).decode("utf-8")


def shape(rds: list[RecordDiff]) -> list[tuple]:
    """Record diffs as comparable tuples (values as exact compact JSON text)."""
    out = []
    for rd in rds:
        changes = None
        if rd.diff is not None:
            changes = (tuple((c.op, c.a_path, c.b_path, _text(c.a), _text(c.b)) for c in rd.diff.changes),
                       rd.diff.truncated, tuple(sorted(rd.diff.counts.items())))
        out.append((rd.status, rd.scope, rd.a_index, rd.b_index, _text(rd.key), changes))
    return out


def assert_same_data(path_a: str | Path, path_b: str | Path, *, open_a: dict | None = None,
                     open_b: dict | None = None, **diff_kw: Any) -> DiffReport:
    """The two files hold the same data (in record order; ``mode="position"`` by default, so a
    reordered record counts as a difference)."""
    diff_kw.setdefault("mode", "position")
    rds, rep = diff_paths(path_a, path_b, open_a=open_a, open_b=open_b, **diff_kw)
    assert rep.identical and not rds, (
        f"{Path(path_a).name} and {Path(path_b).name} differ: {rep.changed} changed, {rep.added} added, "
        f"{rep.removed} removed records; first: {shape(rds[:3])}")
    assert rep.a_records == rep.b_records == rep.equal, (rep.a_records, rep.b_records, rep.equal)
    return rep
