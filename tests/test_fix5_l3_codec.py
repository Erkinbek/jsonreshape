"""L3 item 3: ``codec.dumps`` results kept orjson's whole output buffer.

orjson 3.13 returns a ``bytes`` object that occupies its whole output buffer: 4 097 bytes for a 5-byte
result, ~8x the text for strings (a 1 MB string: 8.3 MB) and up to ~30x for containers -- ``sys.getsizeof``
shows only the text. Callers that keep many results (a block of sorted records in the writer, the values
gathered by ``filetools.extract``, the JSON Lines items of a mapped document) used far more memory than the
texts. ``codec.dumps`` now returns a compact copy; ``codec.dumps_transient`` (no copy) serves the hot paths
that write, concatenate or decode the result at once. Memory is measured with ``tracemalloc`` (the bytes
really requested from the allocator).
"""

from __future__ import annotations

import gc
import tracemalloc
from pathlib import Path
from typing import Any, Callable

import pytest

from jsonreshape import codec, engine, filetools, mapping, writer
from jsonreshape.codec import JsonFloat, JsonInt
from jsonreshape.reader import Document


def traced_per_object(make: Callable[[int], Any], n: int) -> float:
    """Bytes allocated per kept object (tracemalloc)."""
    gc.collect()
    tracemalloc.start()
    try:
        keep = [make(i) for i in range(n)]
        current = tracemalloc.get_traced_memory()[0]
    finally:
        tracemalloc.stop()
    assert len(keep) == n
    return current / n


def peak_of(fn: Callable[[], Any]) -> int:
    gc.collect()
    tracemalloc.start()
    try:
        fn()
        return tracemalloc.get_traced_memory()[1]
    finally:
        tracemalloc.stop()


VALUES = [
    0, -1, 12345, 2**64 - 1, 10**30, -0.0, 0.1, 1.5, JsonFloat("1.10"), JsonFloat("1e400"),
    JsonInt("12345678901234567890"), JsonInt("-0"), "", "Oʻzbekiston", "Тошкент", "x\ud800y", "😀", "a\"b\\c\n",
    True, False, None, [], {}, [1, [2, [3]]], {"b": 1, "a": {"d": [1, 2], "c": "ʻ"}}, list(range(300)),
    {"k%d" % i: "v" * i for i in range(50)},
]


@pytest.mark.parametrize("kw", [{}, {"indent": 2}, {"indent": 4}, {"indent": "\t"}, {"sort_keys": True},
                                {"indent": 2, "sort_keys": True}])
def test_dumps_transient_writes_the_same_bytes(kw: dict) -> None:
    deep: Any = 1
    for _ in range(600):                                  # beyond orjson's 255 levels: the Python encoder
        deep = [deep]
    for v in VALUES + [deep]:
        a, b = codec.dumps(v, **kw), codec.dumps_transient(v, **kw)
        assert type(a) is bytes and type(b) is bytes
        assert a == b, v
    with pytest.raises(TypeError, match="not JSON serializable"):
        codec.dumps(object())
    with pytest.raises(TypeError, match="not JSON serializable"):
        codec.dumps_transient({"a": object()})


@pytest.mark.parametrize("value", [12345, "Toshkent shahri", {"id": 1, "name": "Alisher Navoiy", "tags": ["a", "b"]},
                                   {"k%d" % i: i * 1.5 for i in range(50)}, list(range(1000))])
@pytest.mark.parametrize("kw", [{}, {"indent": 2}, {"sort_keys": True}])
def test_kept_results_are_compact(value: Any, kw: dict) -> None:
    size = len(codec.dumps(value, **kw))
    per = traced_per_object(lambda i: codec.dumps(value, **kw), 500)
    assert per < size + 100, (size, per)                 # orjson's result alone: >= 4 097 bytes, often ~10x
    raw = traced_per_object(lambda i: codec.dumps_transient(value, **kw), 50)
    assert raw >= size                                    # (the transient result is what orjson returned)


def test_a_big_result_is_compact_too() -> None:
    s = "x" * 1_000_000
    per = traced_per_object(lambda i: codec.dumps(s), 3)
    assert per < 1_000_000 + 1000                         # orjson's result: ~8.3 MB


def test_writer_sorted_export_keeps_compact_pieces(tmp_path: Path) -> None:
    """A block's sorted records are kept until the block is written: 4 KiB each before."""
    n = 20_000
    p = tmp_path / "recs.jsonl"
    p.write_bytes(b"".join(b'{"z":%d,"name":"user %d","a":[1,2]}\n' % (i, i) for i in range(n)))
    doc = Document.open(p)
    try:
        doc.build_index()
        for fmt, sort in (("json", "codepoint"), ("jsonl", "alpha")):
            out = tmp_path / f"out.{fmt}"
            peak = peak_of(lambda: writer.export(doc, out, writer.ExportOptions(fmt=fmt, indent=None, sort_keys=sort)))
            assert peak < 24 << 20, (fmt, peak)           # before: > 80 MiB (20 000 x 4 KiB)
            first = out.read_bytes().splitlines()[0]          # (compact JSON: one line)
            assert b'{"a":[1,2],"name":"user 0","z":0}' in first
    finally:
        doc.close()


def test_extract_keeps_compact_values(tmp_path: Path) -> None:
    """``filetools.extract`` with a ``**`` pattern decodes the records and keeps a block's values."""
    n = 20_000
    p = tmp_path / "recs.jsonl"
    p.write_bytes(b"".join(b'{"id":%d,"z":{"q":1,"a":[1,%d]}}\n' % (i, i) for i in range(n)))
    doc = Document.open(p)
    try:
        doc.build_index()
        out = tmp_path / "values.json"
        peak = peak_of(lambda: filetools.extract(doc, out, writer.ExportOptions(fmt="json", indent=None), path="**.a"))
        assert peak < 24 << 20, peak                       # before: > 80 MiB
        assert codec.loads(out.read_bytes())[:2] == [[1, 0], [1, 1]]
    finally:
        doc.close()


def test_mapped_json_lines_items_are_compact_pieces() -> None:
    """``engine._format_items`` (JSON Lines): the items of a worker process's block or of a single
    document's array -- no bound on their number -- were joined from orjson results kept until the join
    ended (4 KiB each); each item is a compact piece now."""
    items = [{"id": i, "name": "user %d" % i, "tags": ["a", "b"]} for i in range(20_000)]
    want = b"".join(codec.dumps(x) + b"\n" for x in items)
    out: list[bytes] = []
    peak = peak_of(lambda: out.append(engine._format_items(items, None, 0, True)))
    assert out[0] == want
    assert peak < 8 * len(want) + (1 << 20), (peak, len(want))     # ~4.4 MB; before: > 80 MiB
    assert engine._format_items([], None, 0, True) == b""
    assert engine._format_items(["x\ud800", 10**30, JsonFloat("1.10")], None, 0, True) == b'"x\\ud800"\n' \
        b"1000000000000000000000000000000\n1.10\n"


def test_mapping_snapshot_is_compact() -> None:
    recs = [{"f%d" % k: k for k in range(400)}]
    tree = mapping.infer_tree(recs, root_kind="records")
    size = len(tree.snapshot())
    per = traced_per_object(lambda i: tree.snapshot(), 5)
    assert per < size * 1.5 + 4096, (size, per)
