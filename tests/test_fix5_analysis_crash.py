"""R5A-X1 (stage-5 review "analysis", critical): opening a small valid JSON file killed the process.

ijson's yajl2_c backend builds a Python ``int`` for every integer token; for a token of more
digits than Python converts (``sys.get_int_max_str_digits()``, 4300) the conversion fails but the
backend still emits a ``("number", NULL)`` event, and the reader's head analysis -- reading that
event's value -- died with a Windows access violation (exit code 139 / 0xC0000005). Every open
here runs in a SUBPROCESS, so a crash fails the test instead of killing pytest: files whose heads
of every kind (single object, wrapped, keyed, array, JSON Lines, stream, brace list, a forced
record path, a head cut inside the number) hold integers of 4301, 10 000 and 100 000 digits, on
both core backends. Every record must read exactly (``codec.dumps`` of the value = the record's
compact text: the number text is kept), also when the records are huge (streamed through
``stream.py``, R5A-10) -- and stats / dupes must read the same values in both cases with nothing
skipped. The CLI ``inspect`` of the review's file must succeed as well.
"""

from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parents[1]
BACKENDS = ["python"] + (["rust"] if importlib.util.find_spec("jsonreshape.core._rscore") is not None else [])
DIGITS = (4301, 10_000, 100_000)

_DRIVER = r'''
import json, sys
sys.path.insert(0, sys.argv[1])
from jsonreshape import codec, dupes, stats, stream
from jsonreshape.reader import Document
from jsonreshape.settings import Settings

spec = json.loads(open(sys.argv[2], encoding="utf-8").read())
out = []
for item in spec:
    sys.stderr.write("open %s %s\n" % (item["path"], item.get("settings")))
    sys.stderr.flush()
    settings = Settings(**item.get("settings", {}))
    kw = {}
    if item.get("record_path") is not None:
        kw["record_path"] = item["record_path"]
    doc = Document.open(item["path"], settings=settings, **kw)
    try:
        doc.build_index()
        records, keys, huge = [], [], 0
        for i in range(doc.record_count):
            s, e = doc.record_span(i)
            if e - s > settings.large_node_bytes:
                huge += 1
                value = stream.stream_values(doc, (s, e), ["$"], member=doc.member_records)[0][0]
            else:
                value = doc.record(i)
            records.append(codec.dumps(value).decode("utf-8"))
            if doc.member_records:
                keys.append(doc.record_key(i))
        st = [[f.field, f.scanned, f.skipped, f.values, f.distinct, sorted(v.text for v in f.top)]
              for f in stats.field_stats(doc, item["fields"])]
        rep = dupes.find_duplicates(doc, item["dupes"])
        out.append({"kind": doc.kind, "record_path": list(doc.record_path) if doc.record_path else None,
                    "error": doc.error.key if doc.error is not None else None, "huge": huge,
                    "records": records, "keys": keys, "stats": st,
                    "dupes": [rep.records_with_key, rep.missing, rep.skipped, rep.groups]})
    finally:
        doc.close()
sys.stdout.write(json.dumps(out))
'''


def big(n: int) -> str:
    """An integer token of ``n`` digits."""
    return ("1234567890" * (n // 10 + 1))[:n]


def cases(n: int) -> list[dict[str, Any]]:
    """(file name, text, kind, record path, records' compact texts, keys, open options)."""
    b = big(n)
    wrapped = '{"meta":{"n":%s},"data":[{"id":1,"v":%s},{"id":2,"v":[-%s,1]},{"id":3,"v":"x"}]}' % (b, b, b)
    wrapped_recs = ['{"id":1,"v":%s}' % b, '{"id":2,"v":[-%s,1]}' % b, '{"id":3,"v":"x"}']
    single = '{"id17":{"grid":[%s],"arr":[1],"orders":[1],"k":282.0,"id":17}}' % b
    return [
        # the review's file: an integer inside an array followed by more members
        dict(name="single.json", text=single, kind="single", path=None, records=[single]),
        dict(name="wrapped.json", text=wrapped, kind="wrapped", path=["data"], records=wrapped_recs),
        dict(name="forced.json", text=wrapped, kind="wrapped", path=["data"], records=wrapped_recs,
             record_path="data"),
        dict(name="keyed.json", text='{"a":{"v":[%s],"w":1},"b":{"v":-%s},"c":{"v":3}}' % (b, b), kind="keyed",
             path=None, records=['{"v":[%s],"w":1}' % b, '{"v":-%s}' % b, '{"v":3}'], keys=["a", "b", "c"]),
        dict(name="array.json", text='[{"v":[%s]},%s,{"x":1},-%s]' % (b, b, b), kind="array", path=None,
             records=['{"v":[%s]}' % b, b, '{"x":1}', "-" + b]),
        dict(name="lines.jsonl", text='{"v":%s}\n[%s,1]\n-%s\n{"x":"s"}\n' % (b, b, b), kind="jsonl", path=None,
             records=['{"v":%s}' % b, "[%s,1]" % b, "-" + b, '{"x":"s"}']),
        dict(name="stream.json", text='{"v":%s} {"v":[1,%s]} -%s {"x":2}' % (b, b, b), kind="stream", path=None,
             records=['{"v":%s}' % b, '{"v":[1,%s]}' % b, "-" + b, '{"x":2}']),
        dict(name="brace.json", text='{ {"v":%s}, {"v":[1,%s]} }' % (b, b), kind="brace_list", path=None,
             records=['{"v":%s}' % b, '{"v":[1,%s]}' % b]),
        # the head (2000 bytes) ends inside the integer
        dict(name="cut.json", text='{"data":[{"v":%s},{"v":2}]}' % b, kind="wrapped", path=["data"],
             records=['{"v":%s}' % b, '{"v":2}'], settings={"head_bytes": 2000}),
    ]


def run_driver(tmp_path: Path, backend: str, spec: list[dict[str, Any]]) -> list[dict[str, Any]]:
    driver = tmp_path / "driver.py"
    driver.write_text(_DRIVER, encoding="utf-8")
    spec_path = tmp_path / "spec.json"
    spec_path.write_text(json.dumps(spec), encoding="utf-8")
    env = dict(os.environ, JSONRESHAPE_BACKEND=backend, PYTHONIOENCODING="utf-8", PYTHONFAULTHANDLER="1")
    proc = subprocess.run([sys.executable, str(driver), str(ROOT), str(spec_path)], cwd=str(ROOT), env=env,
                          capture_output=True, timeout=600)
    err = proc.stderr.decode("utf-8", "replace")
    assert proc.returncode == 0, f"the subprocess died (exit {proc.returncode}):\n{err[-3000:]}"
    return json.loads(proc.stdout.decode("utf-8"))


@pytest.mark.parametrize("digits", DIGITS)
@pytest.mark.parametrize("backend", BACKENDS)
def test_heads_with_huge_integers_open_and_read_exactly(tmp_path: Path, backend: str, digits: int) -> None:
    spec = []
    expected = []
    for case in cases(digits):
        p = tmp_path / case["name"]
        p.write_text(case["text"], encoding="utf-8")
        for variant in ({}, {"large_node_bytes": 1024}):        # in memory / huge records (streamed)
            settings = dict(case.get("settings", {}), **variant)
            spec.append({"path": str(p), "settings": settings, "record_path": case.get("record_path"),
                         "fields": ["v", "v[*]", "$"], "dupes": "v"})
            expected.append(case)
    got = run_driver(tmp_path, backend, spec)
    assert len(got) == len(spec)
    for case, item, res in zip(expected, spec, got):
        where = f"{case['name']} {item['settings']}"
        assert res["kind"] == case["kind"], where
        assert res["record_path"] == case["path"], where
        assert res["error"] is None, where
        assert res["records"] == case["records"], where                  # exact number texts
        assert res["keys"] == case.get("keys", []), where
        assert all(s[2] == 0 for s in res["stats"]), (where, res["stats"])   # nothing skipped
        assert res["dupes"][2] == 0, (where, res["dupes"])
        if item["settings"].get("large_node_bytes") == 1024:
            assert res["huge"] >= 1, where                                   # really streamed
    # the streamed reading gives the same statistics and duplicates as the in-memory one
    for k in range(0, len(got), 2):
        assert got[k]["stats"] == got[k + 1]["stats"], expected[k]["name"]
        assert got[k]["dupes"] == got[k + 1]["dupes"], expected[k]["name"]


@pytest.mark.parametrize("backend", BACKENDS)
def test_cli_inspect_of_the_reviewers_file(tmp_path: Path, backend: str) -> None:
    p = tmp_path / "reader_crash.json"
    p.write_text('{"id17": {"grid": [' + "7" * 5000 + '], "arr": [1], "orders": [1], "k": 282.0, "id": 17}}',
                 encoding="utf-8")
    env = dict(os.environ, JSONRESHAPE_BACKEND=backend, PYTHONIOENCODING="utf-8")
    proc = subprocess.run([sys.executable, "-m", "jsonreshape", "inspect", str(p)], cwd=str(ROOT), env=env,
                          capture_output=True, timeout=300)
    assert proc.returncode == 0, (proc.returncode, proc.stderr.decode("utf-8", "replace")[-2000:])
