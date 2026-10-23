"""Properties of jsonreshape.repair (s5/map_repair_diff.md §3.9), with hypothesis and fault injection:

(a) valid JSON of every standard kind and style is copied byte for byte, with no fix;
(b) the output is always strictly valid (the core Scanner in the output kind's mode, ``codec.loads``) and
    numbers stay exact (``1e400``, ``-0.0``, ``1.10``, ``12345678901234567890``);
(c) ``accelerate=True`` == ``accelerate=False`` -- output AND fix list -- for window sizes 1, 7, 4096 and
    8 MiB, with both core backends;
(d) idempotence: repairing the output again changes nothing;
(e) every byte difference between input and output is covered by a reported fix (the fix spans rebuild the
    output from the input);
(f) line / column of every fix equal ``reader.Document._make_error`` on the same offset.

Plus realistic broken files (JSON5-ish, NaN everywhere, a Python ``repr`` dump) with exact counts and the
data preserved, and a big sparse-fault file (``slow``): MB/s and peak memory (< 200 MB).
"""

from __future__ import annotations

import difflib
import functools
import io
import json
import os
import random
import re
import subprocess
import sys
import textwrap
import tokenize
from pathlib import Path

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

import jsongen
from jsonreshape import codec, core, fileio, repair
from jsonreshape.reader import Document
from repair_helpers import repair_bytes, strict_records

MiB = 1 << 20
WINDOWS = (None, 1, 7, 4096, 8 * MiB)

PROPS = settings(max_examples=int(os.environ.get("RP_EXAMPLES", "25")), deadline=None, derandomize=True, database=None,
                 suppress_health_check=[HealthCheck.too_slow, HealthCheck.function_scoped_fixture,
                                        HealthCheck.data_too_large])


@pytest.fixture
def backend(core_backend, monkeypatch: pytest.MonkeyPatch):
    """Repairs with the parametrized core backend (the accelerator and the output check); no fsync."""
    name = "rust" if core_backend.__name__.endswith("_rscore") else "python"
    old = repair.set_scanner_backend(name)
    monkeypatch.setattr(repair, "atomic_write", functools.partial(fileio.atomic_write, fsync=False))
    yield name
    repair.set_scanner_backend(old)


# ----------------------------------------------------------------------------------------------------
# documents and faults
# ----------------------------------------------------------------------------------------------------
_TOKEN = re.compile(rb'"(?:[^"\\]|\\.)*"|-?\d+(?:\.\d+)?(?:[eE][+-]?\d+)?|true|false|null|[{}\[\]:,]|[ \t\r\n]+', re.S)
_IDENT = re.compile(rb'"[A-Za-z_][A-Za-z0-9_]*"')
KINDS = ("array", "jsonl", "wrapped", "keyed", "single")              # valid as they are
BROKEN_KINDS = KINDS + ("brace_list", "stream")                      # + the kinds repair converts
STYLES = ("compact", "pretty2", "messy", "escaped")


def make_document(seed: int, kind: str, style: str, n: int) -> bytes:
    recs = jsongen.records(n, seed=seed)
    return jsongen.build_document(recs, kind, style=style, seed=seed)


def tokens(data: bytes) -> list[tuple[int, int, bytes]]:
    out = []
    pos = 0
    while pos < len(data):
        m = _TOKEN.match(data, pos)
        assert m is not None, data[pos:pos + 40]
        out.append((m.start(), m.end(), m.group()))
        pos = m.end()
    return out


def inject(data: bytes, rng: random.Random, kind: str, k: int) -> bytes:
    """Up to ``k`` random faults the repair must fix (never an unrepairable one)."""
    toks = [tk for tk in tokens(data) if not tk[2].isspace()]
    edits: list[tuple[int, int, bytes]] = []    # (start, end, replacement)
    used: set[int] = set()

    def free(*idx: int) -> bool:
        return not any(i in used for i in idx)

    for _ in range(k):
        what = rng.choice(("trailing", "comment", "nan", "quote", "literal", "missing", "control", "escape",
                           "key"))
        i = rng.randrange(len(toks))
        s, e, tok = toks[i]
        prev = toks[i - 1][2] if i else b""
        nxt = toks[i + 1][2] if i + 1 < len(toks) else b""
        if what == "trailing" and tok in (b"]", b"}") and prev not in (b"[", b"{", b",") and free(i, i - 1):
            edits.append((s, s, b","))
        elif what == "comment" and tok in (b",", b"[", b"{") and free(i, i + 1):
            edits.append((e, e, b"/* c\xc3\xa9 */" if kind == "jsonl" or rng.random() < 0.5 else b"// c\n"))
        elif what == "nan" and tok[:1] in b"-0123456789" and free(i):
            edits.append((s, e, rng.choice((b"NaN", b"Infinity", b"-Infinity", b"+Infinity"))))
        elif what == "quote" and tok[:1] == b'"' and b"'" not in tok and b"\\" not in tok and free(i):
            edits.append((s, e, b"'" + tok[1:-1] + b"'"))
        elif what == "literal" and tok in (b"true", b"false", b"null") and free(i):
            edits.append((s, e, {b"true": b"True", b"false": b"False", b"null": b"None"}[tok]))
        elif what == "missing" and tok == b"," and prev[-1:] in (b"}", b"]", b'"') and nxt[:1] in (b"{", b"[", b'"') \
                and free(i, i - 1, i + 1):
            edits.append((s, e, b" "))
        elif what == "control" and tok[:1] == b'"' and len(tok) > 2 and nxt != b":" and free(i):
            edits.append((s + 1, s + 1, b"\t"))
        elif what == "escape" and tok[:1] == b'"' and nxt != b":" and free(i):
            edits.append((s + 1, s + 1, b"\\x41"))
        elif what == "key" and nxt == b":" and _IDENT.fullmatch(tok) and free(i):
            edits.append((s, e, tok[1:-1]))
        else:
            continue
        used.add(i)
    for s, e, rep in sorted(edits, key=lambda x: (x[0], x[1]), reverse=True):
        data = data[:s] + rep + data[e:]
    if rng.random() < 0.15:
        data = b"\xef\xbb\xbf" + data
    if kind != "single" and rng.random() < 0.3:
        data = data[:rng.randrange(len(data) // 2, len(data))]
    if rng.random() < 0.1:
        data = data + b"\x00" * rng.randrange(1, 5)
    return data


def output_mode(rep) -> tuple[int, list]:
    ok = rep.output_kind
    if ok == "jsonl":
        return core.MODE_LINES, []
    if ok == "single":
        return core.MODE_SINGLE, []
    if ok == "wrapped":
        return core.MODE_CONTAINER, list(rep.record_path or ())
    return core.MODE_CONTAINER, []


def check_valid(out: bytes, rep) -> None:
    """(b): strictly valid in the output kind's mode, with the reported record count; parseable."""
    mode, target = output_mode(rep)
    if rep.output_kind == "wrapped" and rep.records == 0:
        sc = core.Scanner(core.MODE_SINGLE, [], 0)
        sc.feed(out)
        sc.finish()
        assert sc.error is None
    else:
        assert strict_records(out, mode, target) == rep.records
    if rep.output_kind == "jsonl":
        for line in out.splitlines():
            if line.strip():
                codec.loads(line)
    else:
        codec.loads(out)


def check_coverage(src: bytes, out: bytes, rep) -> None:
    """(e): the input with every top-level fix span replaced by its output span IS the output (a brace list's
    root closer and a stream's separators belong to their one ``brace_list`` / ``concatenated`` entry)."""
    fixes = sorted(rep.iter_fixes(), key=lambda f: (f.offset, f.length))
    brace = any(f.code == "brace_list" for f in fixes)
    concat = any(f.code == "concatenated" for f in fixes)
    if brace or concat:
        if not (concat and rep.output_kind == "jsonl"):
            check_coverage_grouped(src, out, fixes, brace, concat)
        return
    i = j = 0
    end_nested = -1
    for f in fixes:
        if f.offset < end_nested:   # inside a fix already applied (a single-quoted string's inner fixes)
            continue
        a = f.offset
        assert a >= i, (f, i)
        assert src[i:a] == out[j:j + (a - i)], (f, src[i:a], out[j:j + (a - i)])
        j += a - i
        assert f.out_offset == j, (f, j)
        i = a + f.length
        j = f.out_offset + f.out_length
        end_nested = max(end_nested, i)
    assert src[i:] == out[j:], (src[i:i + 60], out[j:j + 60])


def check_coverage_grouped(src: bytes, out: bytes, fixes, brace: bool, concat: bool) -> None:
    """(e) for brace lists and streams: after the listed fixes, the only differences left are the root closer
    ``}`` -> ``]`` (once) and inserted ``,`` / ``[`` / ``]`` separators."""
    parts = []
    i = 0
    end_nested = -1
    for f in fixes:
        if f.offset < end_nested:
            continue
        parts.append(src[i:f.offset])
        parts.append(out[f.out_offset:f.out_offset + f.out_length])
        i = f.offset + f.length
        end_nested = max(end_nested, i)
    parts.append(src[i:])
    rebuilt = b"".join(parts)
    closers = 0
    for op, a1, a2, b1, b2 in difflib.SequenceMatcher(None, rebuilt, out, autojunk=False).get_opcodes():
        old, new = rebuilt[a1:a2], out[b1:b2]
        if op == "equal":
            continue
        if brace and op == "replace" and old == b"}" and new == b"]" and not closers:
            closers += 1
            continue
        if concat and op == "insert" and set(new) <= set(b",[]"):
            continue
        raise AssertionError((op, old[:60], new[:60]))


def check_locations(path: Path, rep) -> None:
    """(f): line / column of every fix (UTF-8 source: user offset = data offset) like the reader's."""
    try:
        doc = Document.open(path)
    except Exception:   # noqa: BLE001 - a source the reader cannot even open (empty): nothing to compare
        return
    with doc:
        data = path.read_bytes()
        for f in rep.iter_fixes():
            if f.offset < doc.info.data_start:
                assert (f.line, f.column) == (1, 1)
                continue
            line0 = data.count(b"\n", 0, f.offset)
            ls = max(doc.info.data_start, data.rfind(b"\n", 0, f.offset) + 1)
            err = doc._make_error((f.offset, "x", "none", line0, ls))
            assert (f.line, f.column) == (err.line, err.column), f


# ----------------------------------------------------------------------------------------------------
# (a) valid input: unchanged
# ----------------------------------------------------------------------------------------------------
@PROPS
@given(seed=st.integers(0, 10 ** 6), kind=st.sampled_from(KINDS), style=st.sampled_from(STYLES),
       n=st.integers(0, 12))
def test_valid_documents_are_copied_unchanged(tmp_path_factory, backend, seed: int, kind: str, style: str,
                                              n: int) -> None:
    if n == 0 and kind in ("jsonl", "keyed"):
        n = 1
    data = make_document(seed, kind, style, n)
    tmp = tmp_path_factory.mktemp("valid")
    name = "in.jsonl" if kind == "jsonl" else "in.json"
    out, rep = repair_bytes(tmp, data, name, windows=(None, 7))
    assert out == data
    assert rep.total == 0 and rep.problem is None and rep.truncated is None


# ----------------------------------------------------------------------------------------------------
# (b)-(f) broken input
# ----------------------------------------------------------------------------------------------------
@PROPS
@given(seed=st.integers(0, 10 ** 6), kind=st.sampled_from(BROKEN_KINDS), style=st.sampled_from(STYLES),
       n=st.integers(1, 10), faults=st.integers(1, 8))
def test_broken_documents(tmp_path_factory, backend, seed: int, kind: str, style: str, n: int,
                          faults: int) -> None:
    rng = random.Random(seed)
    valid = make_document(seed, kind, style, n)
    data = inject(valid, rng, kind, faults)
    tmp = tmp_path_factory.mktemp("broken")
    name = "in.jsonl" if kind == "jsonl" else "in.json"
    out, rep = repair_bytes(tmp, data, name, windows=WINDOWS, max_items=3)
    if out is None:   # never expected: the injected faults are all repairable
        pytest.fail(f"unrepairable: {rep.problem} in {data[:300]!r}")
    check_valid(out, rep)                                                      # (b)
    src = tmp / name
    rep = repair.repair_file(src, tmp / "again_src.json", repair.RepairOptions(max_items=3))
    check_coverage(data, out, rep)                                             # (e)
    check_locations(src, rep)                                                  # (f)
    rep.close()
    if out.strip(b" \t\r\n"):                                                  # (d)
        again = tmp / ("out2.jsonl" if rep.output_kind == "jsonl" else "out2.json")
        again.write_bytes(out)
        rep2 = repair.repair_file(again, tmp / "out3.json")
        assert rep2.total == 0 and (tmp / "out3.json").read_bytes() == out


def test_exact_numbers_survive(tmp_path: Path) -> None:
    nums = b"[1e400, -0.0, 1.10, 12345678901234567890, 0.1, -1E5, 5e-324]"
    data = nums[:-1] + b", NaN,]"
    out, rep = repair_bytes(tmp_path, data, windows=WINDOWS)
    assert out == nums[:-1] + b", null]"
    vals = codec.loads(out)
    assert [codec.number_text(v) for v in vals[:-1]] == ["1e400", "-0.0", "1.10", "12345678901234567890", "0.1",
                                                         "-1E5", "5e-324"]


# ----------------------------------------------------------------------------------------------------
# realistic broken files (the variants of s5/make_broken.py, built from jsongen records)
# ----------------------------------------------------------------------------------------------------
def realistic_records(n: int) -> list[dict]:
    rng = random.Random(7)
    out = []
    for i in range(n):
        out.append({
            "id": i,
            "name": jsongen.EDGE_STRINGS[i % 20] + f" {i}",
            "active": rng.random() < 0.5,
            "score": round(rng.uniform(-100, 100), 2),
            "tags": [jsongen.EDGE_STRINGS[(i + k) % 30] for k in range(i % 3)],
            "contact": {"city": rng.choice(["Toshkent", "Тошкент", "Samarqand", None])},
            "note": None if i % 4 else "O'zbekiston",
        })
    return out


def test_json5_style_file(tmp_path: Path) -> None:
    recs = realistic_records(120)
    parts = [b"[\n"]
    for i, r in enumerate(recs):
        if i % 50 == 0:
            parts.append(b"  // batch %d\n" % (i // 50))
        pretty = codec.dumps(r, indent=2)
        pretty = pretty[:-2] + b",\n}"   # a trailing comma inside every record
        parts.append(b"  " + pretty.replace(b"\n", b"\n  ") + b",\n")
    parts.append(b"]\n")
    data = b"".join(parts)
    out, rep = repair_bytes(tmp_path, data, windows=(None, 4096))
    assert rep.counts == {"comment": 3, "trailing_comma": 121} and rep.records == 120
    assert codec.loads(out) == recs
    assert out.count(b"\n") == data.count(b"\n")     # line numbers kept


def test_nan_everywhere_file(tmp_path: Path) -> None:
    recs = realistic_records(200)
    data = b"[\n" + b",\n".join(codec.dumps(r) for r in recs) + b"\n]\n"
    data = re.sub(rb'"score":-?[0-9.]+', b'"score":NaN', data)
    out, rep = repair_bytes(tmp_path, data, windows=(None, 4096))
    assert rep.counts == {"nan": 200} and rep.records == 200
    expected = [dict(r, score=None) for r in recs]
    assert codec.loads(out) == expected
    out, rep = repair_bytes(tmp_path, data, nan="string", out_name="s.json")
    assert [r["score"] for r in codec.loads(out)] == ["NaN"] * 200


def _python_string_tokens(text: str) -> tuple[int, int]:
    """(single-quoted, double-quoted) string literals of Python source ``text`` (Python's own tokenizer)."""
    single = double = 0
    for tok in tokenize.generate_tokens(io.StringIO(text).readline):
        if tok.type == tokenize.STRING:
            if tok.string.startswith("'"):
                single += 1
            else:
                double += 1
    return single, double


def test_python_repr_dump(tmp_path: Path) -> None:
    recs = realistic_records(150)
    text = repr(recs)
    data = text.encode("utf-8")
    out, rep = repair_bytes(tmp_path, data, windows=(None, 4096))
    assert codec.loads(out) == recs           # every value survives the conversion
    singles, doubles = _python_string_tokens(text)
    assert singles > 1000 and doubles > 10    # repr uses "..." for strings holding an apostrophe
    assert rep.counts["single_quote"] == singles
    assert rep.counts["python_literal"] == text.count("True") + text.count("False") + text.count("None")
    assert set(rep.counts) <= {"single_quote", "python_literal", "invalid_escape"}
    # a dict at the root with the records in a list is detected as wrapped
    data = repr({"meta": {"count": 150}, "data": recs}).encode("utf-8")
    out, rep = repair_bytes(tmp_path, data, out_name="w.json")
    assert rep.kind == "wrapped" and rep.records == 150 and codec.loads(out)["data"] == recs


# ----------------------------------------------------------------------------------------------------
# a big file with sparse faults: speed and memory (README)
# ----------------------------------------------------------------------------------------------------
_BIG_RECORD = (b'{"id":%d,"name":"Toshkent \xd1\x82\xd0\xb5\xd1\x81\xd1\x82 %d","active":true,'
               b'"score":%s,"contact":{"phone":"+998901234567","address":{"city":"Samarqand","zip":140100}},'
               b'"orders":[{"oid":1,"sum":12.5},{"oid":2,"sum":1.10}],"tags":["a","b"]}')

#: the repair in a fresh process: its own peak memory (the data file is written by the test beforehand)
_BIG_SCRIPT = textwrap.dedent(r'''
    import json, os, sys, time
    from jsonreshape import repair
    src, dst = sys.argv[1], sys.argv[2]
    t0 = time.perf_counter()
    rep = repair.repair_file(src, dst)
    dt = time.perf_counter() - t0
    from jsonreshape.cli import _peak_memory   # peak working set / VmHWM: this process only (not the forking parent)
    peak = _peak_memory()
    print(json.dumps({"bytes": os.path.getsize(src), "seconds": dt, "peak": peak, "records": rep.records,
                      "counts": rep.counts, "out": os.path.getsize(dst)}))
''')


def _write_big_sparse(path: Path, mb: int) -> tuple[int, int]:
    """A record array of about ``mb`` MB with ``NaN`` in 1 % of the records; ``(records, NaNs)``."""
    n = nans = size = 0
    with open(path, "wb") as f:
        f.write(b"[\n")
        batch = []
        while size < mb * 1_000_000:
            bad = n % 100 == 37
            nans += bad
            r = _BIG_RECORD % (n, n, b"NaN" if bad else b"%d.25" % n)
            batch.append(r)
            size += len(r) + 2
            n += 1
            if len(batch) == 4096:
                f.write(b",\n".join(batch) + b",\n")
                batch = []
        f.write(b",\n".join(batch) + b"\n]\n")
    return n, nans


@pytest.mark.slow
def test_big_sparse_file_speed_and_memory(tmp_path: Path) -> None:
    """120 MB with sparse faults: exact counts, peak memory of the repairing process < 200 MB; the speed
    (wall time of ``repair_file`` with format detection, regex compilation and the fsync) is printed."""
    _big_sparse(tmp_path, 120)


@pytest.mark.bench
def test_big_sparse_file_2gb(tmp_path: Path) -> None:
    """The README measurement (``-m bench``): 2 GB with a NaN in 1 % of the records."""
    _big_sparse(tmp_path, 2000)


def _big_sparse(tmp_path: Path, mb: int) -> None:
    src = tmp_path / "big.json"
    n, nans = _write_big_sparse(src, mb)
    env = dict(os.environ)
    env["PYTHONPATH"] = str(Path(__file__).resolve().parents[1])
    res = subprocess.run([sys.executable, "-c", _BIG_SCRIPT, str(src), str(tmp_path / "out.json")],
                         capture_output=True, text=True, timeout=600, env=env, check=True)
    info = json.loads(res.stdout.strip().splitlines()[-1])
    print(f"\nrepair of {info['bytes'] / 1e6:.0f} MB with 1 % faults: {info['bytes'] / info['seconds'] / 1e6:.0f} MB/s,"
          f" peak memory {info['peak'] / 1e6:.0f} MB")
    assert info["records"] == n and info["counts"] == {"nan": nans}
    assert info["out"] == info["bytes"] + nans     # NaN -> null
    assert info["peak"] < 200 * 1e6
