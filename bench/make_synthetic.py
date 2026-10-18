"""Generate large deterministic synthetic JSON files for benchmarks (AGENT_PROMPT §12, §14 test 25).

Examples (from the project root)::

    .venv/Scripts/python.exe bench/make_synthetic.py --size 2GB --out bench/data/syn_2g.json
    .venv/Scripts/python.exe bench/make_synthetic.py --records 10000000 --format jsonl --out bench/data/syn_10m.jsonl
    .venv/Scripts/python.exe bench/make_synthetic.py --node-items 1000000 --out bench/data/node_1m.json

Records look like real export data: ids, Uzbek Latin/Cyrillic names and cities, nested ``contact.address``,
``orders[]``, a ``children`` tree (children inside children), tags, timestamps, floats with exact decimal text.
Every ``MARKER_EVERY``-th record carries a rare marker string ("Qoraqalpog'iston-maxsus") for search benchmarks.
"""

from __future__ import annotations

import argparse
import os
import random
import sys
import time

import orjson

MARKER_EVERY = 100_000

FIRST = ["Abdulla", "Bobur", "Gulnora", "Nodira", "Olim", "Sardor", "Zafar", "Oʻtkir", "Gʻani", "Shahzod",
         "Chori", "Dilnoza", "Jasur", "Kamola", "Laylo", "Madina", "Nilufar", "Otabek", "Rustam", "Sevara",
         "Тимур", "Шаҳноза", "Ўткир", "Ғайрат", "Феруза", "Жавлон"]
LAST = ["Karimov", "Rahimova", "Yusupov", "Toshmatov", "Qodirova", "Ergashev", "Xolmatov", "Nazarova",
        "Каримов", "Юсупова", "Тошматов"]
CITIES = ["Toshkent", "Тошкент", "Samarqand", "Buxoro", "Andijon", "Namangan", "Farg'ona", "Fargʻona",
          "Xiva", "Nukus", "Qarshi", "Termiz", "Jizzax", "Navoiy", "Guliston", "Urganch", None]
STREETS = ["Amir Temur ko'chasi", "Navoiy ko'chasi", "Bobur ko'chasi", "Мустақиллик кўчаси", "Chilonzor 9-kvartal"]
TAGS = ["vip", "yangi", "doimiy", "ulgurji", "chakana", "онлайн", "qarzdor", "aksiya"]


def make_record(i: int, rng: random.Random) -> dict:
    name = f"{rng.choice(FIRST)} {rng.choice(LAST)}"
    rec: dict = {
        "id": i,
        "name": name,
        "active": rng.random() < 0.7,
        "score": round(rng.random() * 1000, 2),
        "created_at": 1_600_000_000 + rng.randrange(150_000_000),
        "contact": {
            "phone": f"+99890{rng.randrange(1_000_000, 9_999_999)}",
            "address": {"city": rng.choice(CITIES), "street": rng.choice(STREETS), "zip": rng.randrange(100000, 999999)},
        },
        "tags": rng.sample(TAGS, rng.randrange(0, 4)),
        "orders": [
            {"oid": i * 10 + j, "sum": round(rng.random() * 500, 2), "items": rng.randrange(1, 10)}
            for j in range(rng.randrange(0, 4))
        ],
    }
    if rng.random() < 0.3:
        rec["children"] = [
            {"name": f"child{k}", "children": [{"name": f"child{k}.{m}"} for m in range(rng.randrange(0, 3))]}
            for k in range(1, rng.randrange(2, 4))
        ]
    if rng.random() < 0.1:
        rec["note"] = None
    if i % MARKER_EVERY == 0:
        rec["note"] = f"Qoraqalpog'iston-maxsus-{i}"
    return rec


def _parse_size(text: str) -> int:
    s = text.strip().upper()
    for suffix, mult in (("GB", 1 << 30), ("MB", 1 << 20), ("KB", 1 << 10), ("G", 1 << 30), ("M", 1 << 20), ("K", 1 << 10)):
        if s.endswith(suffix):
            return int(float(s[: -len(suffix)]) * mult)
    return int(s)


def generate(out: str, *, size: int | None, records: int | None, fmt: str, seed: int, quiet: bool) -> dict:
    rng = random.Random(seed)
    os.makedirs(os.path.dirname(os.path.abspath(out)) or ".", exist_ok=True)
    tmp = out + ".part"
    written = 0
    count = 0
    t0 = time.perf_counter()
    last_report = t0
    with open(tmp, "wb", buffering=16 << 20) as f:
        if fmt == "array":
            f.write(b"[\n")
            written += 2
        elif fmt == "wrapped":
            head = b'{"meta":{"source":"synthetic","version":1,"generated":"2026-10-16"},"data":[\n'
            f.write(head)
            written += len(head)
        batch: list[bytes] = []
        while True:
            if records is not None and count >= records:
                break
            if size is not None and written >= size:
                break
            data = orjson.dumps(make_record(count, rng))
            if fmt in ("array", "wrapped"):
                piece = (b",\n" if count else b"") + data
            else:
                piece = data + b"\n"
            batch.append(piece)
            written += len(piece)
            count += 1
            if len(batch) >= 4096:
                f.write(b"".join(batch))
                batch.clear()
                now = time.perf_counter()
                if not quiet and now - last_report > 2:
                    last_report = now
                    print(f"  {written / 1e9:.2f} GB, {count:,} records, {written / 1e6 / (now - t0):.0f} MB/s",
                          file=sys.stderr)
        if batch:
            f.write(b"".join(batch))
        if fmt == "array":
            f.write(b"\n]\n")
            written += 3
        elif fmt == "wrapped":
            tail = b'\n],"total":' + str(count).encode() + b"}\n"
            f.write(tail)
            written += len(tail)
    os.replace(tmp, out)
    return {"path": out, "bytes": os.path.getsize(out), "records": count, "seconds": time.perf_counter() - t0}


def generate_node_file(out: str, items: int, seed: int) -> dict:
    """A single object whose "items" member is an array with ``items`` small objects (§14 test 14)."""
    rng = random.Random(seed)
    t0 = time.perf_counter()
    tmp = out + ".part"
    with open(tmp, "wb", buffering=16 << 20) as f:
        f.write(b'{"meta":{"kind":"node-benchmark","items":' + str(items).encode() + b'},"items":[')
        batch = []
        for i in range(items):
            batch.append((b"," if i else b"") + orjson.dumps({"i": i, "v": rng.randrange(1000), "s": f"element {i}"}))
            if len(batch) >= 8192:
                f.write(b"".join(batch))
                batch.clear()
        f.write(b"".join(batch))
        f.write(b'],"tail":{"ok":true}}\n')
    os.replace(tmp, out)
    return {"path": out, "bytes": os.path.getsize(out), "records": items, "seconds": time.perf_counter() - t0}


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--out", required=True)
    ap.add_argument("--size", help="target size, e.g. 2GB (stops after the record that crosses it)")
    ap.add_argument("--records", type=int, help="number of records")
    ap.add_argument("--format", default="array", choices=["array", "jsonl", "wrapped"])
    ap.add_argument("--node-items", type=int, help="write a single-object file with an N-element array instead")
    ap.add_argument("--seed", type=int, default=2026)
    ap.add_argument("--force", action="store_true", help="regenerate even if the file exists")
    ap.add_argument("-q", "--quiet", action="store_true")
    args = ap.parse_args(argv)
    if os.path.exists(args.out) and not args.force:
        print(orjson.dumps({"path": args.out, "bytes": os.path.getsize(args.out), "cached": True}).decode())
        return 0
    if args.node_items:
        info = generate_node_file(args.out, args.node_items, args.seed)
    else:
        if args.size is None and args.records is None:
            ap.error("--size or --records is required")
        info = generate(args.out, size=_parse_size(args.size) if args.size else None, records=args.records,
                        fmt=args.format, seed=args.seed, quiet=args.quiet)
    print(orjson.dumps(info).decode())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
