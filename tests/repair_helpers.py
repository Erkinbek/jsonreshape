"""Shared helpers of the repair tests (tests/test_repair*.py).

``repair_bytes`` runs :func:`jsonreshape.repair.repair_file` on bytes with the accelerator on and off (and
optionally several window sizes) and asserts that the output and the fix list are identical in every run
(docs/ARCHITECTURE.md §14.5: ``accelerate=True`` == ``accelerate=False`` for any window size).
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from jsonreshape import core
from jsonreshape.repair import RepairOptions, RepairReport, UnrepairableError, repair_file


def fix_tuple(fx: Any) -> tuple:
    """The comparable part of a fix: code, data offset, line, column, out offset, record, spans, texts."""
    return (fx.code, fx.offset, fx.line, fx.column, fx.out_offset, fx.record, fx.length, fx.out_length, fx.before,
            fx.after)


def short(fx: Any) -> tuple:
    """``(code, offset, line, column, out_offset, record)``."""
    return fx.code, fx.offset, fx.line, fx.column, fx.out_offset, fx.record


def result_key(out: bytes | None, rep: RepairReport) -> tuple:
    tr = rep.truncated
    p = rep.problem
    return (out, rep.kind, rep.output_kind, rep.records, dict(rep.counts), [fix_tuple(f) for f in rep.fixes],
            None if tr is None else tr.to_dict(), None if p is None else p.to_dict(), rep.written)


def repair_bytes(tmp_path: Path, data: bytes, name: str = "in.json", *, windows: tuple = (None,),
                 accelerate: tuple = (True, False), out_name: str = "out.json", settings: Any = None,
                 **opts: Any) -> tuple[bytes | None, RepairReport]:
    """Repair ``data`` (written to ``tmp_path/name``) with every combination of ``accelerate`` and
    ``windows``; all results must be identical. Returns ``(output bytes or None, report)`` of the first run
    (an unrepairable file without ``allow_partial`` gives ``None`` and the error's report)."""
    src = tmp_path / name
    src.write_bytes(data)
    first: tuple | None = None
    ret: tuple[bytes | None, RepairReport] | None = None
    k = 0
    for acc in accelerate:
        for w in windows:
            k += 1
            dst = tmp_path / f"r{k}_{out_name}"
            if dst.exists():
                dst.unlink()
            o = RepairOptions(accelerate=acc, window=w, **opts)
            try:
                rep = repair_file(src, dst, o, settings=settings)
                out = dst.read_bytes()
            except UnrepairableError as exc:
                rep = exc.report
                assert not dst.exists()
                out = None
            key = result_key(out, rep)
            if first is None:
                first = key
                ret = (out, rep)
            else:
                assert key == first, f"accelerate={acc} window={w} differs:\n{key}\n!=\n{first}"
            rep.close()
    assert ret is not None
    return ret


def strict_records(data: bytes, mode: int, target: list | None = None) -> int:
    """Records the strict core scanner finds in ``data`` (``AssertionError`` when it is not valid)."""
    sc = core.Scanner(mode, target or [], 0)
    sc.feed(data)
    sc.finish()
    assert sc.error is None, (sc.error, data[:200])
    if mode == core.MODE_LINES:
        assert not sc.take_broken(), data[:200]
    return sc.record_count
