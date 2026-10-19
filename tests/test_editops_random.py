"""jsonreshape.editops under random edits (docs/ARCHITECTURE.md §12.4).

Random records (``tests/jsongen.py``: Uzbek Latin/Cyrillic, apostrophes, escapes, emoji,
U+2028, exact numbers, empty containers) are encoded in every text style (compact,
pretty2, pretty4, tab, messy, escaped; LF and CRLF) and edited by random operations
(set / rename / insert / delete runs / move / convert) through the byte splices; a plain
Python value is edited the same way. After every operation the bytes must be valid JSON
on every core backend, parse (exact numbers) to the model, and keep every byte outside
the edited region. Duplicate keys are addressed exactly through ipaths; both core
backends locate identically.
"""

from __future__ import annotations

import copy
import random

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

import jsongen
from jsonreshape import codec, core, editops, paths
from jsonreshape.core.constants import MODE_SINGLE
from jsonreshape.editops import DocStyle
from jsonreshape.errors import ConversionError, EditError

HYP = settings(deadline=None, max_examples=200, derandomize=True, database=None,
               suppress_health_check=[HealthCheck.function_scoped_fixture, HealthCheck.too_slow])

TEXT_STYLES = ("compact", "pretty2", "pretty4", "tab", "messy", "escaped", "messy_escaped")
DOC_STYLES = (
    DocStyle(),
    DocStyle(newline=b"\n", record_sep=b"\n", unit=None, colon=b": ", empty=b"", comma=b", "),
    DocStyle(newline=b"\n", record_sep=b",\n  ", record_level=1, unit="  ", colon=b": ", comma=b", "),
    DocStyle(newline=b"\n", record_sep=b",\n    ", record_level=1, unit="    ", colon=b": ", comma=b", "),
    DocStyle(newline=b"\n", record_sep=b",\n\t", record_level=1, unit="\t", colon=b": ", comma=b", "),
    DocStyle(newline=b"\r\n", record_sep=b",\r\n  ", record_level=1, unit="  ", colon=b": ", comma=b", "),
    DocStyle(newline=b"\n", record_sep=b",", record_level=0, unit="  ", colon=b":", comma=b","),
)
TYPES = editops.JSON_TYPES


@pytest.fixture
def backend(core_backend):
    """Run the test on each core backend."""
    old = editops.set_core_backend(core_backend)
    yield core_backend
    editops.set_core_backend(old)


# ----------------------------------------------------------------------------------
# helpers
# ----------------------------------------------------------------------------------
def canon(data: bytes) -> bytes:
    return codec.dumps(codec.loads(data))


def scan_ok(data: bytes) -> None:
    """``data`` is one JSON value without outer whitespace on every core backend."""
    for name in core.available_backends():
        sc = core.get_backend(name).Scanner(MODE_SINGLE, [], 0)
        out = sc.feed(data) + sc.finish()
        assert sc.error is None, (name, sc.error, data[:200])
        assert out == (0).to_bytes(8, "little") + len(data).to_bytes(8, "little"), name


def all_paths(value, path=()):
    yield path, value
    if isinstance(value, dict):
        for k, v in value.items():
            yield from all_paths(v, path + (k,))
    elif isinstance(value, list):
        for i, v in enumerate(value):
            yield from all_paths(v, path + (i,))


def get_at(value, path):
    for seg in path:
        value = value[seg]
    return value


def set_at(root, path, value):
    if not path:
        return value
    get_at(root, path[:-1])[path[-1]] = value
    return root


def fresh_key(rng: random.Random, taken) -> str:
    while True:
        key = jsongen.rand_string(rng) if rng.random() < 0.7 else rng.choice(jsongen.KEYS)
        if key not in taken:
            return key


def texts(buf: bytes, loc) -> list[bytes]:
    return [buf[k.node_start:k.end] for k in editops.child_locs(buf, loc)]


def encode(value, rng: random.Random, crlf: bool) -> bytes:
    data = jsongen.encode(value, rng.choice(TEXT_STYLES), rng.randrange(1 << 30))
    return data.replace(b"\n", b"\r\n") if crlf else data


# ----------------------------------------------------------------------------------
# one random operation: returns (new bytes, new model)
# ----------------------------------------------------------------------------------
def random_op(rng: random.Random, buf: bytes, model, style: DocStyle, crlf: bool):
    nodes = list(all_paths(model))
    level = rng.randint(0, 3)
    op = rng.choice(("set", "set", "rename", "insert", "insert", "delete", "move", "convert"))
    if op == "set":
        path, _ = rng.choice(nodes)
        new = jsongen.rand_value(rng, 0, 3)
        loc = editops.locate(buf, path)
        out = editops.set_value(buf, loc, encode(new, rng, crlf), style, base_level=level)
        assert out[:loc.start] == buf[:loc.start] and out[len(out) - (len(buf) - loc.end):] == buf[loc.end:]
        return out, set_at(model, path, new)
    if op == "convert":
        path, value = rng.choice(nodes)
        to = rng.choice(TYPES)
        try:
            new = editops.convert_value(value, to)
        except ConversionError:
            src = codec.json_type(value)
            assert (src, to) in {("object", "number"), ("array", "number"), ("object", "bool"), ("array", "bool"),
                                 ("number", "object"), ("bool", "object"), ("array", "object"),
                                 ("string", "number"), ("string", "bool"), ("string", "object")}
            return buf, model
        loc = editops.locate(buf, path)
        out = editops.set_value(buf, loc, codec.dumps(new), style, base_level=level)
        return out, set_at(model, path, new)
    members = [p for p, _ in nodes if p and isinstance(get_at(model, p[:-1]), dict)]
    containers = [(p, v) for p, v in nodes if isinstance(v, (dict, list))]
    if op == "rename" and members:
        path = rng.choice(members)
        parent = get_at(model, path[:-1])
        loc = editops.locate(buf, path)
        others = [k for k in parent if k != path[-1]]
        if others and rng.random() < 0.2:
            with pytest.raises(EditError) as ei:
                editops.rename_key(buf, loc, rng.choice(others))
            assert ei.value.key == "edits.error.key_exists"
            return buf, model
        new_key = fresh_key(rng, parent)
        out = editops.rename_key(buf, loc, new_key)
        assert out[:loc.mstart] == buf[:loc.mstart] and out[len(out) - (len(buf) - loc.kend):] == buf[loc.kend:]
        items = [(new_key if k == path[-1] else k, v) for k, v in parent.items()]
        parent.clear()
        parent.update(items)
        return out, model
    if op == "insert" and containers:
        path, cont = rng.choice(containers)
        n = len(cont)
        index = rng.randint(0, n)
        new = jsongen.rand_value(rng, 0, 3)
        loc = editops.locate(buf, path)
        key = fresh_key(rng, cont) if isinstance(cont, dict) else None
        if key is not None and cont and rng.random() < 0.15:
            with pytest.raises(EditError) as ei:
                editops.insert_child(buf, loc, index, b"1", style, key=next(iter(cont)), base_level=level)
            assert ei.value.key == "edits.error.key_exists"
        out = editops.insert_child(buf, loc, index, encode(new, rng, crlf), style, key=key, base_level=level)
        if n == 0:
            assert out[:loc.start + 1] == buf[:loc.start + 1] and out.endswith(buf[loc.end - 1:])
        else:
            kids = editops.child_locs(buf, loc)
            pos = kids[n - 1].end if index == n else kids[index].node_start
            assert out[:pos] == buf[:pos] and out.endswith(buf[pos:])
        if isinstance(cont, dict):
            items = list(cont.items())
            items.insert(index, (key, new))
            cont.clear()
            cont.update(items)
        else:
            cont.insert(index, new)
        return out, model
    filled = [(p, v) for p, v in containers if len(v) >= 1]
    if op == "delete" and filled:
        path, cont = rng.choice(filled)
        n = len(cont)
        chosen = sorted(rng.sample(range(n), rng.randint(1, n)))
        loc = editops.locate(buf, path)
        before = texts(buf, loc)
        shuffled = chosen[:]
        rng.shuffle(shuffled)
        out = editops.delete_children(buf, loc, shuffled + shuffled[:1])  # any order, repeats allowed
        assert out[:loc.start] == buf[:loc.start] and out.endswith(buf[loc.end:])
        assert texts(out, editops.locate(out, path)) == [t for i, t in enumerate(before) if i not in chosen]
        if isinstance(cont, dict):
            items = [kv for i, kv in enumerate(cont.items()) if i not in chosen]
            cont.clear()
            cont.update(items)
        else:
            cont[:] = [v for i, v in enumerate(cont) if i not in chosen]
        return out, model
    movable = [(p, v) for p, v in containers if len(v) >= 2]
    if op == "move" and movable:
        path, cont = rng.choice(movable)
        n = len(cont)
        src, dst = rng.randrange(n), rng.randrange(n)
        loc = editops.locate(buf, path)
        before = texts(buf, loc)
        out = editops.move_child(buf, loc, src, dst, style, base_level=level)
        assert out[:loc.start] == buf[:loc.start] and out.endswith(buf[loc.end:])
        order = list(range(n))
        order.insert(dst, order.pop(src))
        assert texts(out, editops.locate(out, path)) == [before[i] for i in order]
        if isinstance(cont, dict):
            items = list(cont.items())
            items.insert(dst, items.pop(src))
            cont.clear()
            cont.update(items)
        else:
            cont.insert(dst, cont.pop(src))
        return out, model
    return buf, model


# ----------------------------------------------------------------------------------
# tests
# ----------------------------------------------------------------------------------
@HYP
@given(seed=st.integers(0, 2**32 - 1))
def test_random_edits_match_the_model(seed, backend) -> None:
    rng = random.Random(seed)
    model = jsongen.rand_record(rng, rng.randrange(1000)) if rng.random() < 0.5 else jsongen.rand_value(rng, 0, 4)
    crlf = rng.random() < 0.3
    style = rng.choice(DOC_STYLES)
    buf = encode(model, rng, crlf)
    buf = editops.check_json(buf)  # no outer whitespace, like a record span
    model = copy.deepcopy(model)
    single_line = b"\n" not in buf
    no_cr = b"\r" not in buf
    for _ in range(8):
        buf, model = random_op(rng, buf, model, style, crlf)
        scan_ok(buf)
        assert canon(buf) == codec.dumps(model)
        if style.unit is None and single_line:
            assert b"\n" not in buf  # compact documents (JSON Lines) stay on one line
        if style.newline == b"\n" and no_cr:
            assert b"\r" not in buf  # new text uses the document's newline


@HYP
@given(seed=st.integers(0, 2**32 - 1))
def test_member_units(seed, backend) -> None:
    """Keyed records: the unit is ``"key": value``; splices inside it keep the member head."""
    rng = random.Random(seed)
    key = jsongen.rand_string(rng)
    value = jsongen.rand_value(rng, 0, 3)
    head = editops.encode_key(key) + rng.choice((b":", b": ", b" :\t"))
    unit = head + jsongen.encode(value, rng.choice(TEXT_STYLES), seed)
    assert editops.check_unit(unit, member=True) >= 0
    loc = editops.member_loc(unit)
    assert loc.key == key and canon(unit[loc.start:loc.end]) == codec.dumps(value)
    new = jsongen.rand_value(rng, 0, 3)
    out = editops.set_value(unit, loc, codec.dumps(new), rng.choice(DOC_STYLES), base_level=1)
    assert out.startswith(head)
    editops.check_unit(out, member=True)
    assert canon(out[len(head):]) == codec.dumps(new)
    new_key = jsongen.rand_string(rng)
    renamed = editops.rename_key(out, editops.member_loc(out), new_key)
    editops.check_unit(renamed, member=True)
    assert editops.member_loc(renamed).key == new_key
    unit2 = editops.format_record(codec.dumps(new), rng.choice(DOC_STYLES), key=new_key)
    editops.check_unit(unit2, member=True)
    assert editops.member_loc(unit2).key == new_key


def _walk(buf: bytes, loc, path=(), ipath=()):
    yield path, ipath, loc
    if loc.kind in ("object", "array"):
        for child in editops.child_locs(buf, loc):
            yield from _walk(buf, child, path + (child.key,), ipath + (child.index,))


def _with_duplicates(rng: random.Random, buf: bytes) -> bytes:
    """Repeat some object members (same key, another value) right after themselves."""
    for _ in range(rng.randint(1, 4)):
        members = [loc for _p, _i, loc in _walk(buf, editops.locate(buf)) if loc.mstart is not None]
        if not members:
            return buf
        m = rng.choice(members)
        value = jsongen.encode(jsongen.rand_value(rng, 0, 2), rng.choice(TEXT_STYLES), rng.randrange(99))
        buf = buf[:m.end] + b" ,\n" + buf[m.mstart:m.kend] + b":" + value + buf[m.end:]
    return buf


@HYP
@given(seed=st.integers(0, 2**32 - 1))
def test_duplicate_keys_ipath_exact_path_last_wins(seed, backend) -> None:
    rng = random.Random(seed)
    value = {"a": jsongen.rand_value(rng, 1, 4), "b": [jsongen.rand_value(rng, 1, 3)], "c": {"d": 1}}
    buf = _with_duplicates(rng, jsongen.encode(value, rng.choice(TEXT_STYLES), seed))
    parsed = codec.loads(buf)
    for path, ipath, loc in _walk(buf, editops.locate(buf)):
        assert editops.locate(buf, path, ipath) == loc
        want = paths.get(parsed, path, default=paths.MISSING)
        if want is paths.MISSING:  # a path through an earlier duplicate does not exist in dict semantics
            with pytest.raises(EditError) as ei:
                editops.locate(buf, path)
            assert ei.value.key == "edits.error.path_not_found"
        else:
            by_path = editops.locate(buf, path)
            assert canon(buf[by_path.start:by_path.end]) == codec.dumps(want)
    # editing an earlier duplicate through its ipath touches only that member
    dups = [(p, i, loc) for p, i, loc in _walk(buf, editops.locate(buf)) if loc.mstart is not None]
    path, ipath, loc = rng.choice(dups)
    out = editops.set_value(buf, loc, b'"EDITED"', DocStyle(), base_level=0)
    assert editops.locate(out, path, ipath).kind == "string"
    assert out[loc.start:loc.start + 8] == b'"EDITED"'
    assert out[:loc.start] == buf[:loc.start] and out[loc.start + 8:] == buf[loc.end:]


def _try_locate(buf: bytes, path, ipath=None):
    try:
        return editops.locate(buf, path, ipath)
    except EditError as exc:
        return exc.key, exc.params


@HYP
@given(seed=st.integers(0, 2**32 - 1))
def test_both_backends_locate_identically(seed, both_backends) -> None:
    rng = random.Random(seed)
    value = jsongen.rand_value(rng, 0, 4) if rng.random() < 0.5 else jsongen.rand_record(rng, 1)
    buf = jsongen.encode(value, rng.choice(TEXT_STYLES), seed)
    if isinstance(value, dict):
        buf = _with_duplicates(rng, buf)
    results = []
    old = editops.set_core_backend(None)
    try:
        for backend in both_backends:
            editops.set_core_backend(backend)
            walk = list(_walk(buf, editops.locate(buf)))
            located = [_try_locate(buf, p, i) for p, i, _l in walk] + [_try_locate(buf, p) for p, _i, _l in walk]
            results.append((walk, located))
    finally:
        editops.set_core_backend(old)
    assert results[0] == results[1]


@pytest.mark.parametrize("token", ["0.1", "1e400", "-1e400", "1e-400", "12345678901234567890", "-0.0", "-0", "1.10",
                                   "1E5", "2.5E+3", "123456789012345678901234567890", "5e-324"])
@pytest.mark.parametrize("style", DOC_STYLES[:3] + DOC_STYLES[5:6])
def test_exact_numbers_survive_every_splice(token, style, backend) -> None:
    rec = b'{\n  "n": 0,\n  "list": [1, 2],\n  "empty": {},\n  "deep": {"x": []}\n}'
    tok = token.encode()
    out = editops.set_value(rec, editops.locate(rec, ("n",)), tok, style, base_level=0)
    out = editops.insert_child(out, editops.locate(out, ("list",)), 1, b"[" + tok + b"]", style, base_level=0)
    out = editops.insert_child(out, editops.locate(out, ("empty",)), 0, tok, style, key="k", base_level=0)
    out = editops.insert_child(out, editops.locate(out, ("deep", "x")), 0, b'{"v":' + tok + b"}", style,
                               base_level=0)
    out = editops.move_child(out, editops.locate(out, ("list",)), 1, 0, style, base_level=0)
    scan_ok(out)
    value = codec.loads(out)
    for got in (value["n"], value["list"][0][0], value["empty"]["k"], value["deep"]["x"][0]["v"]):
        assert codec.number_text(got) == token
    assert out.count(tok) == 4
    converted = editops.convert_value(value["n"], "string")
    assert converted == token and editops.convert_value(converted, "number").__repr__() == repr(value["n"])
