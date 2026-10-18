"""Throughput benchmark of the byte-level core (docs/ARCHITECTURE.md §1).

Generates ~200 MB of record-array JSON (``orjson.dumps`` of ``tests/jsongen.rand_record``
records, ~240 bytes per record, Uzbek Latin/Cyrillic text, nested objects/arrays) once
into ``bench/data/`` and measures MB/s of every core function (best of N runs, warm OS
cache, data already in memory):

* ``Scanner``            MODE_CONTAINER over the root array, 4 MiB and 16 MiB feeds
* ``Reformatter``        compact / indent=2 / tab, 8 MiB feeds
* ``MultiFinder``        ``find_in_spans`` with 1, 4 and 16 needles over 8 MiB record blocks
* ``PathExtractor``      1 and 3 key paths over 8 MiB record blocks
* ``numbers_plain``      a whole buffer whose numbers are all plain, and one call per record

Usage::

    python bench/core_bench.py [--mb 200] [--backend rust|python] [--repeat 3] [--json OUT]

The Python backend is much slower; use e.g. ``--backend python --mb 20``.
"""

from __future__ import annotations

import argparse
import json
import os
import random
import sys
import time
from array import array
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))

import orjson  # noqa: E402

DATA_DIR = ROOT / "bench" / "data"
MiB = 1 << 20

#: MB/s targets for the Rust backend (ARCHITECTURE.md §1)
TARGETS = {
    "Scanner": 400.0,
    "Reformatter": 300.0,
    "MultiFinder": 1000.0,
    "PathExtractor": 300.0,
    "numbers_plain": 800.0,
}


def _fragment(obj):
    raw = getattr(obj, "raw", None)  # codec.JsonFloat / JsonInt keep their exact token
    if raw is None:
        raise TypeError(type(obj).__name__)
    return orjson.Fragment(raw)


def _dump(rec) -> bytes:
    try:
        return orjson.dumps(rec, option=orjson.OPT_PASSTHROUGH_SUBCLASS, default=_fragment)
    except (orjson.JSONEncodeError, TypeError):
        from jsonreshape import codec

        return codec.dumps(rec)  # integers beyond 64 bits: exact pure-Python encoder


def _plain_numbers(v):
    """Copy of ``v`` whose numbers are all plain (for the numbers_plain full-scan run)."""
    from jsonreshape.codec import number_text, is_plain_float_token, is_plain_int_token

    if isinstance(v, dict):
        return {k: _plain_numbers(x) for k, x in v.items()}
    if isinstance(v, list):
        return [_plain_numbers(x) for x in v]
    if isinstance(v, bool) or v is None or isinstance(v, str):
        return v
    tok = number_text(v)
    if is_plain_int_token(tok) or is_plain_float_token(tok):
        return v
    return round(float(len(tok)) + 0.25, 2)


def generate(path: Path, mb: int, *, plain: bool = False, seed: int = 1) -> None:
    import jsongen

    rng = random.Random(seed)
    target = mb * 1_000_000
    tmp = path.with_suffix(".tmp")
    size = 0
    i = 0
    t0 = time.perf_counter()
    with open(tmp, "wb") as f:
        f.write(b"[")
        while size < target:
            rec = jsongen.rand_record(rng, i)
            if plain:
                rec = _plain_numbers(rec)
            b = _dump(rec)
            if i:
                f.write(b",")
            f.write(b)
            size += len(b) + 1
            i += 1
        f.write(b"]")
    os.replace(tmp, path)
    print(f"generated {path.name}: {size / 1e6:.1f} MB, {i} records in {time.perf_counter() - t0:.1f} s")


def load(mb: int, plain: bool = False) -> bytes:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    path = DATA_DIR / f"core_bench_{mb}mb{'_plain' if plain else ''}.json"
    if not path.exists():
        generate(path, mb, plain=plain)
    return path.read_bytes()


def best_of(fn, repeat: int) -> tuple[float, object]:
    best, res = float("inf"), None
    for _ in range(repeat):
        t = time.perf_counter()
        res = fn()
        best = min(best, time.perf_counter() - t)
    return best, res


def make_blocks(data: bytes, spans: array, block_bytes: int = 8 * MiB) -> list[tuple[bytes, bytes]]:
    """Group consecutive records into ~block_bytes blocks: (block data, relative spans)."""
    blocks = []
    n = len(spans) // 2
    i = 0
    while i < n:
        start = spans[2 * i]
        j = i
        while j < n and spans[2 * j + 1] - start <= block_bytes:
            j += 1
        j = max(j, i + 1)
        end = spans[2 * j - 1]
        rel = array("Q", (v - start for v in spans[2 * i:2 * j]))
        blocks.append((data[start:end], rel.tobytes()))
        i = j
    return blocks


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--mb", type=int, default=200, help="size of the generated data (MB)")
    ap.add_argument("--backend", choices=("rust", "python"), default="rust")
    ap.add_argument("--repeat", type=int, default=3)
    ap.add_argument("--json", type=Path, help="write the results as JSON")
    args = ap.parse_args(argv)

    from jsonreshape import core

    be = core.get_backend(args.backend)
    data = load(args.mb)
    nbytes = len(data)
    print(f"backend={args.backend} data={nbytes / 1e6:.1f} MB repeat={args.repeat}")
    results: list[dict] = []

    def report(group: str, name: str, nb: int, secs: float) -> None:
        mbs = nb / secs / 1e6
        target = TARGETS.get(group) if args.backend == "rust" else None
        status = "" if target is None else ("ok" if mbs >= target else "BELOW TARGET")
        print(f"  {name:<46} {mbs:9.1f} MB/s  {'' if target is None else f'(target {target:.0f}) {status}'}")
        results.append({"group": group, "name": name, "mb_per_s": round(mbs, 1), "target": target,
                        "bytes": nb, "seconds": secs})

    # ---- Scanner -----------------------------------------------------------------------
    spans_raw = b""
    for feed_mib in (4, 16):
        ch = feed_mib * MiB
        chunks = [data[k:k + ch] for k in range(0, nbytes, ch)]

        def scan(chunks=chunks):
            s = be.Scanner(core.MODE_CONTAINER)
            out = [s.feed(c) for c in chunks]
            out.append(s.finish())
            assert s.error is None, s.error
            return b"".join(out)

        secs, spans_raw = best_of(scan, args.repeat)
        report("Scanner", f"Scanner MODE_CONTAINER, {feed_mib} MiB feeds", nbytes, secs)
    spans = array("Q", spans_raw)
    nrec = len(spans) // 2
    print(f"  ({nrec} records, {nbytes / nrec:.0f} bytes/record)")

    # ---- Reformatter ---------------------------------------------------------------------
    ch8 = [data[k:k + 8 * MiB] for k in range(0, nbytes, 8 * MiB)]
    for indent in (None, 2, "\t"):
        def ref(indent=indent):
            f = be.Reformatter(indent=indent)
            n = sum(len(f.feed(c)) for c in ch8)
            return n + len(f.finish())

        secs, _ = best_of(ref, args.repeat)
        report("Reformatter", f"Reformatter indent={indent!r}", nbytes, secs)

    # ---- MultiFinder / PathExtractor over 8 MiB record blocks -------------------------------
    blocks = make_blocks(data, spans)
    block_bytes = sum(len(b) for b, _ in blocks)
    needle_sets = {
        1: [b"Toshkent"],
        4: [b"Toshkent", "Тошкент".encode(), b"Samarqand", b"Buxoro 9"],
        16: [w.encode() for w in ("Toshkent", "Тошкент", "Samarqand", "Buxoro", "Chorsu", "Чорсу", "child10",
                                  "Shahrisabz", "G'ulom", "Ғулом", "zzqx", "qqzz", "xyzzy", "Straße", "İstanbul",
                                  "emoji")],
    }
    for k, needles in needle_sets.items():
        mf = be.MultiFinder(needles)
        secs, hits = best_of(lambda mf=mf: sum(len(mf.find_in_spans(b, s)) // 8 for b, s in blocks), args.repeat)
        report("MultiFinder", f"MultiFinder.find_in_spans {k} needle(s) ({hits} hits)", block_bytes, secs)

    path_sets = {
        1: [[("key", "id")]],
        3: [[("key", "name")], [("key", "contact"), ("key", "address"), ("key", "city")], [("key", "score")]],
    }
    for k, paths in path_sets.items():
        px = be.PathExtractor(paths)
        secs, _ = best_of(lambda px=px: sum(len(px.extract(b, s)) for b, s in blocks), args.repeat)
        report("PathExtractor", f"PathExtractor.extract {k} path(s)", block_bytes, secs)

    # ---- numbers_plain ----------------------------------------------------------------------
    plain = load(args.mb, plain=True)
    assert be.numbers_plain(plain) is True
    secs, _ = best_of(lambda: be.numbers_plain(plain), args.repeat)
    report("numbers_plain", "numbers_plain whole buffer (all plain)", len(plain), secs)
    s = be.Scanner(core.MODE_CONTAINER)
    psp = array("Q", s.feed(plain) + s.finish())
    recs = [plain[psp[2 * i]:psp[2 * i + 1]] for i in range(len(psp) // 2)]
    del psp
    fn = be.numbers_plain
    secs, _ = best_of(lambda: sum(1 for r in recs if fn(r)), args.repeat)
    report("numbers_plain", f"numbers_plain per record ({len(recs)} calls)", sum(map(len, recs)), secs)

    if args.json:
        args.json.parent.mkdir(parents=True, exist_ok=True)
        args.json.write_text(json.dumps({"backend": args.backend, "data_bytes": nbytes, "records": nrec,
                                         "results": results}, indent=2), encoding="utf-8")
    below = [r for r in results if r["target"] is not None and r["mb_per_s"] < r["target"]]
    return 1 if below else 0


if __name__ == "__main__":
    raise SystemExit(main())
