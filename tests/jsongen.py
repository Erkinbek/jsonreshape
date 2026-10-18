"""Deterministic JSON generators and file writers shared by the test-suite.

* :func:`rand_value` / :func:`rand_record` — random JSON values (seeded ``random.Random``)
  including edge cases: Uzbek Latin/Cyrillic text, apostrophe variants, escapes, emoji,
  lone-surrogate-free unicode, exact number tokens (``1.10``, ``1e400``, big ints, ``-0``).
* :func:`encode` — serialise a value in several textual styles (compact, pretty, tabs,
  messy whitespace, ``\\u``-escaped) while keeping number tokens exact.
* :func:`write_file` — write a list of records in any supported file format.
"""

from __future__ import annotations

import gzip
import json
import random
from pathlib import Path
from typing import Any

from jsonreshape.codec import JsonFloat, JsonInt, number_text

EDGE_STRINGS = [
    "",
    "Toshkent",
    "Тошкент",
    "toshkent",
    "TOSHKENT",
    "O'zbekiston",
    "Oʻzbekiston",
    "O‘zbekiston",
    "O’zbekiston",
    "Ўзбекистон",
    "G'ulom",
    "Gʻulom",
    "Ғулом",
    "Shahrisabz",
    "Шаҳрисабз",
    "Chorsu",
    "Чорсу",
    "Amir Temur ko'chasi",
    'quote"inside',
    "back\\slash",
    "slash/inside",
    "tab\there",
    "new\nline",
    "cr\rlf",
    "nul\u0000char",
    "bell\u0007",
    "emoji 😀 test",
    "line sep \u2028 para \u2029",
    "null",
    "true",
    "123",
    "1.10",
    "Straße",
    "ǅemal",
    "ﬁle",
    "İstanbul",
    "child2",
    "child10",
    "a" * 300,
]

EDGE_NUMBER_TOKENS = [
    "0",
    "-0",
    "1",
    "-1",
    "0.1",
    "1.10",
    "-0.0",
    "0.0",
    "100.0",
    "1e400",
    "-1e400",
    "1e-400",
    "1E5",
    "2.5E+3",
    "12345678901234567890",
    "-9223372036854775808",
    "-9223372036854775809",
    "18446744073709551615",
    "18446744073709551616",
    "123456789012345678901234567890",
    "123456789.123456789",
    "5e-324",
    "0.00001",
    "0.000001",
    "9007199254740993",
    "3.141592653589793",
    "1000000000000000.0",
    "9999999999999999.0",
]

KEYS = [
    "id",
    "name",
    "ism",
    "city",
    "shahar",
    "contact",
    "address",
    "phone",
    "children",
    "orders",
    "oid",
    "sum",
    "tags",
    "active",
    "score",
    "note",
    "kalit bo'shliq bilan",
    "dot.key",
    "Тошкент",
    "",
    "0",
    "a/b",
    "x~y",
]


def number_from_token(tok: str) -> Any:
    """Python value for a number token exactly as codec.loads would produce it."""
    from jsonreshape.codec import loads

    return loads(tok)


def rand_string(rng: random.Random) -> str:
    r = rng.random()
    if r < 0.45:
        return rng.choice(EDGE_STRINGS)
    if r < 0.75:
        alphabet = "abcdefghijklmnopqrstuvwxyz ABCXYZ0123456789-_'ʻ"
        return "".join(rng.choice(alphabet) for _ in range(rng.randint(0, 20)))
    if r < 0.9:
        alphabet = "абвгдеёжзийклмнопрстуфхцчшъьэюяўқғҳ АБВЎҚҒҲ"
        return "".join(rng.choice(alphabet) for _ in range(rng.randint(1, 15)))
    # random BMP + astral chars (no surrogates)
    chars = []
    for _ in range(rng.randint(1, 8)):
        cp = rng.choice([rng.randint(0x20, 0x7E), rng.randint(0xA0, 0xD7FF), rng.randint(0xE000, 0xFFFD),
                         rng.randint(0x10000, 0x10FFFF), rng.randint(0, 0x1F)])
        chars.append(chr(cp))
    return "".join(chars)


def rand_number(rng: random.Random) -> Any:
    r = rng.random()
    if r < 0.3:
        return number_from_token(rng.choice(EDGE_NUMBER_TOKENS))
    if r < 0.6:
        return rng.randint(-10**6, 10**6)
    if r < 0.85:
        return round(rng.uniform(-1e4, 1e4), rng.randint(0, 6))
    return rng.choice([rng.randint(-2**70, 2**70), rng.random() * 10 ** rng.randint(-30, 30)])


def rand_scalar(rng: random.Random) -> Any:
    r = rng.random()
    if r < 0.4:
        return rand_string(rng)
    if r < 0.75:
        return rand_number(rng)
    if r < 0.85:
        return rng.choice([True, False])
    return None


def rand_value(rng: random.Random, depth: int = 0, max_depth: int = 4, max_items: int = 5) -> Any:
    if depth >= max_depth or rng.random() < 0.35:
        return rand_scalar(rng)
    if rng.random() < 0.5:
        return [rand_value(rng, depth + 1, max_depth, max_items) for _ in range(rng.randint(0, max_items))]
    obj: dict[str, Any] = {}
    for _ in range(rng.randint(0, max_items)):
        key = rng.choice(KEYS) if rng.random() < 0.6 else rand_string(rng)
        obj[key] = rand_value(rng, depth + 1, max_depth, max_items)
    return obj


def rand_record(rng: random.Random, i: int) -> dict[str, Any]:
    """A realistic record: id, name, nested contact.address, orders[], children tree."""
    rec: dict[str, Any] = {"id": i, "name": rng.choice(EDGE_STRINGS[:20]) + f" {i}"}
    if rng.random() < 0.8:
        rec["contact"] = {
            "phone": f"+99890{rng.randint(1000000, 9999999)}",
            "address": {"city": rng.choice(["Toshkent", "Тошкент", "Samarqand", "Buxoro", None]),
                        "zip": rng.randint(100000, 999999)},
        }
    if rng.random() < 0.7:
        rec["orders"] = [{"oid": rng.randint(1, 99), "sum": rand_number(rng)} for _ in range(rng.randint(0, 3))]
    if rng.random() < 0.5:
        rec["tags"] = [rand_string(rng) for _ in range(rng.randint(0, 3))]
    rec["score"] = rand_number(rng)
    rec["active"] = rng.random() < 0.5
    if rng.random() < 0.3:
        rec["note"] = None
    if rng.random() < 0.3:
        rec["extra"] = rand_value(rng, 1, 4)
    return rec


def records(n: int, seed: int = 0, kind: str = "record") -> list[Any]:
    rng = random.Random(seed)
    if kind == "record":
        return [rand_record(rng, i) for i in range(n)]
    return [rand_value(rng, 0, 4) for _ in range(n)]


# ---------------------------------------------------------------------------------
# encoding with styles
# ---------------------------------------------------------------------------------
def _string_token(s: str, escaped: bool, rng: random.Random | None) -> str:
    if escaped:
        text = json.dumps(s, ensure_ascii=True)  # \uXXXX for non-ASCII (surrogate pairs for astral)
        return text.replace("/", "\\/")
    return json.dumps(s, ensure_ascii=False)


def _scalar_token(v: Any, escaped: bool, rng: random.Random | None) -> str:
    if v is None:
        return "null"
    if v is True:
        return "true"
    if v is False:
        return "false"
    if isinstance(v, str):
        return _string_token(v, escaped, rng)
    if isinstance(v, (int, float)):
        if isinstance(v, (JsonFloat, JsonInt)):
            return v.raw
        return number_text(v)
    raise TypeError(type(v))


def encode(value: Any, style: str = "compact", seed: int = 0) -> bytes:
    """Serialise ``value``.

    Styles: ``compact``, ``pretty2``, ``pretty4``, ``tab``, ``messy`` (random whitespace
    including ``\\r\\n`` and tabs between every token), ``escaped`` (compact with all
    non-ASCII as ``\\uXXXX`` and ``/`` as ``\\/``), ``messy_escaped``.
    """
    rng = random.Random(seed)
    escaped = style in ("escaped", "messy_escaped")
    messy = style in ("messy", "messy_escaped")
    indent: str | None = None
    if style == "pretty2":
        indent = "  "
    elif style == "pretty4":
        indent = "    "
    elif style == "tab":
        indent = "\t"

    def ws() -> str:
        if not messy:
            return ""
        return "".join(rng.choice([" ", "\t", "\n", "\r\n", ""]) for _ in range(rng.randint(0, 2)))

    out: list[str] = []

    def emit(v: Any, level: int) -> None:
        if isinstance(v, dict):
            if not v:
                out.append("{" + ws() + "}")
                return
            out.append("{")
            first = True
            for k, item in v.items():
                if not first:
                    out.append(ws() + ",")
                first = False
                if indent is not None:
                    out.append("\n" + indent * (level + 1))
                out.append(ws() + _string_token(k, escaped, rng) + ws() + ":" + (" " if indent is not None else "") + ws())
                emit(item, level + 1)
            if indent is not None:
                out.append("\n" + indent * level)
            out.append(ws() + "}")
        elif isinstance(v, list):
            if not v:
                out.append("[" + ws() + "]")
                return
            out.append("[")
            first = True
            for item in v:
                if not first:
                    out.append(ws() + ",")
                first = False
                if indent is not None:
                    out.append("\n" + indent * (level + 1))
                out.append(ws())
                emit(item, level + 1)
            if indent is not None:
                out.append("\n" + indent * level)
            out.append(ws() + "]")
        else:
            out.append(_scalar_token(v, escaped, rng))

    emit(value, 0)
    return "".join(out).encode("utf-8")


# ---------------------------------------------------------------------------------
# file writers
# ---------------------------------------------------------------------------------
FORMATS = ("array", "jsonl", "brace_list", "wrapped", "keyed", "single", "stream")


def build_document(recs: list[Any], fmt: str, *, style: str = "compact", newline: str = "\n",
                   keys: list[str] | None = None, wrapper_path: tuple[str, ...] = ("data",),
                   seed: int = 0) -> bytes:
    """Bytes of a document holding ``recs`` in format ``fmt``.

    * ``keyed``: records must be dicts' values; ``keys`` default to ``id0, id1, ...``
    * ``wrapped``: ``{"meta": {...}, <wrapper_path...>: [records], "tail": {...}}``
    * ``single``: the list of records as one value
    """
    sep = b"," + (newline.encode() if style != "compact" else b"")
    enc = [encode(r, style, seed + i) for i, r in enumerate(recs)]
    if fmt == "array":
        return b"[" + sep.join(enc) + b"]"
    if fmt == "jsonl":
        return b"".join(encode(r, "compact" if style not in ("escaped",) else style, seed + i) + newline.encode()
                        for i, r in enumerate(recs))
    if fmt == "stream":
        return newline.encode().join(enc) + newline.encode()
    if fmt == "brace_list":
        return b"{" + sep.join(enc) + b"}"
    if fmt == "keyed":
        ks = keys or [f"id{i}" for i in range(len(recs))]
        parts = [json.dumps(k, ensure_ascii=False).encode() + b":" + e for k, e in zip(ks, enc)]
        return b"{" + sep.join(parts) + b"}"
    if fmt == "wrapped":
        inner = b"[" + sep.join(enc) + b"]"
        for key in reversed(wrapper_path[1:]):
            inner = b"{" + json.dumps(key).encode() + b":" + inner + b"}"
        meta = encode({"version": 1, "source": "test", "list": [1, 2, 3]}, style, seed)
        tail = encode({"count": len(recs), "ok": True}, style, seed + 1)
        return (b'{"meta":' + meta + b"," + json.dumps(wrapper_path[0]).encode() + b":" + inner
                + b',"tail":' + tail + b"}")
    if fmt == "single":
        return encode(recs, style, seed)
    raise ValueError(fmt)


def write_file(path: str | Path, recs: list[Any], fmt: str, *, style: str = "compact", encoding: str = "utf-8",
               bom: bool = False, gz: bool = False, newline: str = "\n", keys: list[str] | None = None,
               wrapper_path: tuple[str, ...] = ("data",), seed: int = 0) -> bytes:
    """Write a document; returns the UTF-8 bytes of the JSON text (before re-encoding)."""
    data = build_document(recs, fmt, style=style, newline=newline, keys=keys, wrapper_path=wrapper_path, seed=seed)
    raw = data
    if encoding in ("utf-16-le", "utf-16-be"):
        text = data.decode("utf-8")
        raw = text.encode(encoding)
        if bom:
            raw = ("\ufeff".encode(encoding)) + raw
    elif bom:
        raw = b"\xef\xbb\xbf" + data
    if gz:
        raw = gzip.compress(raw)
    Path(path).write_bytes(raw)
    return data
