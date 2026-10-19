"""L3 item 1 (critical): reading a mapping file killed the process.

``mapping.load_mapping`` decodes a mapping file with ``codec.loads``; when that refuses the text (a broken
file, or one nested deeper than the JSON decoders allow) it fell back to ``_parse_iterative``, which ran
``ijson.basic_parse`` (yajl2_c, ``use_float=False``) over the bytes. For an integer token of more than
``sys.get_int_max_str_digits()`` (4300) digits inside an array followed by more members, yajl2_c leaves a
pending error and emits ``("number", NULL)``: reading that event killed the process (Windows access
violation, exit code 139 / 0xC0000005). The fallback now uses ``stream.exact_events`` (pure Python, exact
``codec.loads`` values). Every load here runs in a SUBPROCESS, so a crash fails the test instead of killing
pytest: deep valid files load with their numbers exact, broken ones fail with ``mapping.error.json``.
"""

from __future__ import annotations

import ast
import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

from jsonreshape import codec
from jsonreshape import mapping as M
from jsonreshape.mapping import MapNode, MappingTree

ROOT = Path(__file__).resolve().parents[1]
DIGITS = (4301, 10_000, 100_000)
DEEP_LEVELS = 100_000           # nested arrays: beyond every JSON decoder's stack, so codec.loads refuses

_DRIVER = r'''
import json, sys
sys.path.insert(0, sys.argv[1])
from jsonreshape import codec, mapping

spec = json.loads(open(sys.argv[2], encoding="utf-8").read())
out = []
for item in spec:
    sys.stderr.write("load %s\n" % item["path"])
    sys.stderr.flush()
    src = open(item["path"], "rb").read() if item["as_bytes"] else item["path"]
    try:
        tree, info = mapping.load_mapping(src)
    except mapping.MappingError as exc:
        out.append({"error": exc.key})
        continue
    out.append({"nodes": tree.node_count(), "root_kind": tree.root_kind,
                "info": codec.dumps(info).decode("utf-8")})
sys.stdout.write(json.dumps(out))
'''


def big(n: int) -> str:
    """An integer token of ``n`` digits."""
    return ("1234567890" * (n // 10 + 1))[:n]


def small_tree_file(input_text: str) -> bytes:
    """A valid one-node mapping file whose ``input`` member is ``input_text`` (raw JSON)."""
    data = M.dumps_mapping(MappingTree(MapNode(1, kind="object", is_root=True), root_kind="value"), input_info={})
    assert data.count(b' "input": {}') == 1
    return data.replace(b' "input": {}', b' "input": ' + input_text.encode("utf-8"))


def deep_tree_file(input_text: str, depth: int = 30000) -> bytes:
    """A valid mapping file whose TREE is too deep for the decoders (nested groups, as in test_mapping)."""
    root = MapNode(1, kind="object", is_root=True)
    cur = root
    for i in range(depth):
        g = MapNode(i + 2, key=f"g{i}", kind="object", src=None, synthetic=True, types=("object",))
        cur.children.append(g)
        cur = g
    data = M.dumps_mapping(MappingTree(root, root_kind="value"), input_info={})
    return data.replace(b' "input": {}', b' "input": ' + input_text.encode("utf-8"), 1)


def run_driver(tmp_path: Path, spec: list[dict[str, Any]]) -> list[dict[str, Any]]:
    driver = tmp_path / "driver.py"
    driver.write_text(_DRIVER, encoding="utf-8")
    spec_path = tmp_path / "spec.json"
    spec_path.write_text(json.dumps(spec), encoding="utf-8")
    env = dict(os.environ, PYTHONIOENCODING="utf-8", PYTHONFAULTHANDLER="1")
    proc = subprocess.run([sys.executable, str(driver), str(ROOT), str(spec_path)], cwd=str(ROOT), env=env,
                          capture_output=True, timeout=600)
    err = proc.stderr.decode("utf-8", "replace")
    assert proc.returncode == 0, f"the subprocess died (exit {proc.returncode}):\n{err[-3000:]}"
    return json.loads(proc.stdout.decode("utf-8"))


def _refused(data: bytes) -> bool:
    try:
        codec.loads(data[3:] if data.startswith(b"\xef\xbb\xbf") else data)
    except codec.JsonDecodeError:
        return True
    return False


@pytest.mark.parametrize("digits", DIGITS)
def test_mapping_files_with_huge_integers_never_crash(tmp_path: Path, digits: int) -> None:
    b = big(digits)
    deep = "[" * DEEP_LEVELS + "]" * DEEP_LEVELS
    # the review's shape: the integer inside an array, followed by more members
    info = '{"grid":[%s],"arr":[1],"neg":[-%s,2],"x":1.10,"e":1e400,"u":"Oʻzbek \\ud800","deep":%s}' % (b, b, deep)
    cases: list[tuple[str, bytes, Any]] = [
        # deep enough for the fallback, valid: loads, every number exact
        ("deep_input.mapping.json", small_tree_file(info), ("ok", 1)),
        ("deep_input_bom.mapping.json", b"\xef\xbb\xbf" + small_tree_file(info), ("ok", 1)),
        # broken: refused with the localized error
        ("cut.mapping.json", small_tree_file(info)[:-5], "mapping.error.json"),
        ("trailing.mapping.json", small_tree_file(info) + b" x", "mapping.error.json"),
        ("review.mapping.json", ('{"id17": {"grid": [%s], "arr": [1], "orders": [1], "k": 282.0, "id": 17}' % b)
         .encode(), "mapping.error.json"),
        ("review_trailing.mapping.json",
         ('{"id17": {"grid": [%s], "arr": [1], "orders": [1], "k": 282.0, "id": 17}} x' % b).encode(),
         "mapping.error.json"),
        ("version.mapping.json", ('{"version": 1, "x": {"grid": [%s], "arr": [1], "k": 2}, "tree": ' % b).encode(),
         "mapping.error.json"),
        # a complete document followed by invalid UTF-8: refused (the fallback decodes strictly)
        ("bad_utf8.mapping.json", small_tree_file(info) + b"\xff", "mapping.error.json"),
    ]
    spec = []
    for name, data, _want in cases:
        assert _refused(data), name                     # the fallback really runs
        p = tmp_path / name
        p.write_bytes(data)
        for as_bytes in (False, True):
            spec.append({"path": str(p), "as_bytes": as_bytes})
    got = run_driver(tmp_path, spec)
    assert len(got) == len(spec)
    want_info = codec.dumps(codec.loads(info.replace(deep, "0"))).decode("utf-8").replace(':0}', ':' + deep + '}')
    for (name, _data, want), res in zip([c for c in cases for _ in (0, 1)], got):
        if isinstance(want, str):
            assert res == {"error": want}, name
        else:
            assert "error" not in res, (name, res)
            assert res["nodes"] == want[1] and res["root_kind"] == "value", name
            assert res["info"] == want_info, name   # exact: the digits, 1.10, 1e400, the lone surrogate


def test_deep_tree_mapping_file_with_a_huge_integer_loads(tmp_path: Path) -> None:
    """The realistic case: a tree nested beyond the decoders (read by the fallback) and an integer of more
    than 4300 digits inside an array followed by more members."""
    b = big(5000)
    info = '{"grid":[%s],"arr":[1],"orders":[1],"k":282.0,"id":17}' % b
    data = deep_tree_file(info)
    assert _refused(data)
    p = tmp_path / "deep_tree.mapping.json"
    p.write_bytes(data)
    cut = tmp_path / "deep_tree_cut.mapping.json"
    cut.write_bytes(data[:len(data) // 2])
    got = run_driver(tmp_path, [{"path": str(p), "as_bytes": False}, {"path": str(cut), "as_bytes": True}])
    assert "error" not in got[0], got[0]
    assert got[0]["nodes"] == 30001
    assert got[0]["info"] == info                                   # the integer's text is kept
    assert got[1] == {"error": "mapping.error.json"}


def test_fallback_values_equal_codec_loads() -> None:
    """In process (no huge integers): the iterative fallback gives what codec.loads gives -- exact numbers,
    lone surrogates, duplicate keys (last value, first position), any depth."""
    text = ('{"a":[1,-0,1.10,1e400,12345678901234567890,-0.0,0.1,true,false,null],"s":"x\\ud800y\\u00e9",'
            '"d":{"k":1,"j":2,"k":3},"e":{},"f":[],"g":[[[{"h":[{}]}]]]}')
    want = codec.loads(text)
    got = M._parse_iterative(text.encode("utf-8"))
    assert codec.dumps(got) == codec.dumps(want)
    assert list(got["d"]) == ["k", "j"] and got["d"]["k"] == 3
    deep = b"[" * 50_000 + b"7" + b"]" * 50_000
    v = M._parse_iterative(deep)
    for _ in range(50_000):
        v = v[0]
    assert v == 7
    for bad in (b"[1,", b"{\"a\" 1}", b"[1] x", b"\xff", b"[\"\xc3\"]", b""):
        with pytest.raises(Exception):
            M._parse_iterative(bad)


def test_no_direct_ijson_parsing_outside_the_protected_modules() -> None:
    """``ijson.basic_parse`` & co. on raw bytes may crash the process (R5A-X1): only ``stream.py`` (careful
    mode) and ``reader.py`` (``needs_exact`` / ``exact_events``) may import ijson; ``env.py`` only checks the
    backend. (A module importing ijson elsewhere must be protected the same way and added here.)"""
    allowed = {"stream.py", "reader.py", "env.py"}
    pkg = ROOT / "jsonreshape"
    offenders = []
    for path in sorted(pkg.rglob("*.py")):
        if "__pycache__" in path.parts or path.name in allowed:
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import) and any(a.name.split(".")[0] == "ijson" for a in node.names):
                offenders.append(f"{path.name}:{node.lineno}: import ijson")
            elif isinstance(node, ast.ImportFrom) and (node.module or "").split(".")[0] == "ijson":
                offenders.append(f"{path.name}:{node.lineno}: from ijson")
            elif isinstance(node, ast.Call) and "importlib" in ast.unparse(node.func) \
                    and "ijson" in ast.unparse(node):
                offenders.append(f"{path.name}:{node.lineno}: importlib ijson")
    assert offenders == []
