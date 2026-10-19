"""Exact JSON decoding/encoding.

orjson is fast but loses information for some number tokens (``1.10`` -> ``1.1``,
``1e400`` -> error, integers beyond 64 bits -> float, ``-0`` -> ``0``). This module keeps
every number token exactly:

* *plain* tokens (those orjson reproduces byte-for-byte, see :func:`is_plain_int_token`
  and :func:`is_plain_float_token`) become ordinary ``int``/``float``;
* other floats become :class:`JsonFloat` (a ``float`` carrying the original text);
* ``-0`` and integers outside ``[-2**63, 2**64-1]`` become :class:`JsonInt`.

:func:`dumps` writes ``JsonFloat``/``JsonInt``/``Decimal`` back as their raw text, so
``dumps(loads(x))`` never changes a number and never turns it into a string. Its result is a compact
``bytes`` object (an ``orjson.dumps`` result keeps orjson's whole output buffer -- at least 4 KiB, several
times the text for bigger results -- so callers that store many results would use far more memory than
the texts); :func:`dumps_transient` skips that copy for results written, concatenated or decoded at once.
"""

from __future__ import annotations

import json
import math
import re
from decimal import Decimal
from typing import Any

import orjson

from .errors import JsonDecodeError

__all__ = [
    "JsonDecodeError",
    "JsonFloat",
    "JsonInt",
    "dumps",
    "dumps_transient",
    "is_number",
    "is_plain_float_token",
    "is_plain_int_token",
    "json_type",
    "loads",
    "loads_precise",
    "number_text",
    "value_text",
]

_INT64_MIN = -(2**63)
_UINT64_MAX = 2**64 - 1
_MAX_INT_DIGITS = 4000  # Python's int() refuses > 4300 digits by default


class JsonFloat(float):
    """A JSON number whose exact text must be preserved (value = nearest double)."""

    __slots__ = ("raw",)
    raw: str

    def __new__(cls, raw: str) -> JsonFloat:
        obj = float.__new__(cls, raw)
        obj.raw = raw
        return obj

    def __repr__(self) -> str:
        return self.raw

    __str__ = __repr__

    def __reduce__(self):
        return (JsonFloat, (self.raw,))

    def __copy__(self) -> JsonFloat:
        return self

    def __deepcopy__(self, memo) -> JsonFloat:
        return self


class JsonInt(int):
    """``-0`` or an integer outside the 64-bit range (value exact, text preserved)."""

    raw: str

    def __new__(cls, raw: str) -> JsonInt:
        obj = int.__new__(cls, raw)
        obj.raw = raw
        return obj

    def __repr__(self) -> str:
        return self.raw

    __str__ = __repr__

    def __reduce__(self):
        return (JsonInt, (self.raw,))

    def __copy__(self) -> JsonInt:
        return self

    def __deepcopy__(self, memo) -> JsonInt:
        return self


# -- plain-token predicates (must match core.numbers_plain exactly) ------------------
_PLAIN_INT_RE = re.compile(r"-?(?:0|[1-9][0-9]{0,17})")
_FLOAT_RE = re.compile(r"-?(0|[1-9][0-9]*)\.([0-9]+)")


def is_plain_int_token(tok: str) -> bool:
    """Integer token that orjson parses and re-serialises unchanged."""
    return tok != "-0" and _PLAIN_INT_RE.fullmatch(tok) is not None


def is_plain_float_token(tok: str) -> bool:
    """Float token (no exponent) that orjson parses and re-serialises unchanged."""
    m = _FLOAT_RE.fullmatch(tok)
    if m is None:
        return False
    ip, fp = m.group(1), m.group(2)
    if fp != "0" and fp.endswith("0"):
        return False
    if ip != "0":
        if len(ip) > 16:
            return False
        digits = ip.rstrip("0") if fp == "0" else ip + fp
        return len(digits) <= 15
    if fp == "0":
        return True
    stripped = fp.lstrip("0")
    if len(fp) - len(stripped) + 1 > 5:
        return False
    return len(stripped) <= 15


def _parse_float(tok: str) -> float:
    if is_plain_float_token(tok):
        return float(tok)
    return JsonFloat(tok)


def _parse_int(tok: str) -> int | float:
    if len(tok) <= 18 and tok != "-0":
        return int(tok)
    if len(tok) > _MAX_INT_DIGITS:
        return JsonFloat(tok)  # value: +-inf, text preserved
    value = int(tok)
    if tok == "-0" or value < _INT64_MIN or value > _UINT64_MAX:
        return JsonInt(tok)
    return value


def _reject_constant(name: str) -> Any:
    raise ValueError(f"{name} is not valid JSON")


_DECODER = json.JSONDecoder(parse_float=_parse_float, parse_int=_parse_int, parse_constant=_reject_constant)

_fast_checker = None  # core.numbers_plain when the Rust backend is active


def _init_fast() -> None:
    global _fast_checker
    try:
        from . import core

        if core.BACKEND == "rust":
            _fast_checker = core.numbers_plain
        else:
            _fast_checker = False
    except Exception:  # pragma: no cover - core unavailable
        _fast_checker = False


def loads_precise(data: bytes | bytearray | memoryview | str) -> Any:
    """Decode JSON keeping every number token exactly (pure stdlib path)."""
    if isinstance(data, str):
        text = data
    else:
        try:
            text = bytes(data).decode("utf-8")
        except UnicodeDecodeError as exc:
            raise JsonDecodeError(f"invalid UTF-8 at byte {exc.start}", exc.start) from None
    try:
        return _DECODER.decode(text)
    except json.JSONDecodeError as exc:
        raise JsonDecodeError(exc.msg, exc.pos) from None
    except (ValueError, RecursionError) as exc:
        raise JsonDecodeError(str(exc)) from None


def loads(data: bytes | bytearray | memoryview | str) -> Any:
    """Decode JSON exactly; uses orjson when all number tokens are plain (Rust backend)."""
    if _fast_checker is None:
        _init_fast()
    if _fast_checker:
        if isinstance(data, str):
            try:
                buf = data.encode("utf-8")
            except UnicodeEncodeError:
                # raw lone surrogates: not encodable, so only the precise path can decode
                # them (identically to the pure-Python backend)
                return loads_precise(data)
        else:
            buf = bytes(data)
        try:
            plain = _fast_checker(buf)
        except Exception:
            plain = False
        if plain:
            try:
                return orjson.loads(buf)
            except orjson.JSONDecodeError:
                pass  # e.g. lone surrogate escapes or depth > 1024: precise path decides
        return loads_precise(buf)
    return loads_precise(data)


# -- encoding --------------------------------------------------------------------------
def _default(obj: Any) -> Any:
    if isinstance(obj, (JsonFloat, JsonInt)):
        return orjson.Fragment(obj.raw)
    if isinstance(obj, Decimal):
        if not obj.is_finite():
            raise TypeError("non-finite Decimal is not valid JSON")
        return orjson.Fragment(str(obj))
    if isinstance(obj, bool):  # pragma: no cover - bool cannot be subclassed
        return bool(obj)
    if isinstance(obj, int):
        return int(obj)
    if isinstance(obj, float):
        return float(obj)
    if isinstance(obj, str):
        return str(obj)
    if isinstance(obj, dict):
        return dict(obj)
    if isinstance(obj, (list, tuple)):
        return list(obj)
    raise TypeError(f"Type is not JSON serializable: {type(obj).__name__}")


_OPT_BASE = orjson.OPT_PASSTHROUGH_SUBCLASS
_SURROGATE_RE = re.compile("[\ud800-\udfff]")


def _encode_str(s: str) -> str:
    text = json.dumps(s, ensure_ascii=False)
    if _SURROGATE_RE.search(text):
        text = _SURROGATE_RE.sub(lambda m: "\\u%04x" % ord(m.group()), text)
    return text


def _scalar_text(obj: Any) -> str:
    if obj is None:
        return "null"
    if obj is True:
        return "true"
    if obj is False:
        return "false"
    if isinstance(obj, str):
        return _encode_str(obj)
    if isinstance(obj, (int, float, Decimal)):
        if isinstance(obj, float) and not isinstance(obj, JsonFloat) and not math.isfinite(obj):
            return "null"  # same as orjson
        return number_text(obj)
    raise TypeError(f"Type is not JSON serializable: {type(obj).__name__}")


def _py_dumps(obj: Any, sort_keys: bool = False) -> bytes:
    """Compact pure-Python encoder (iterative) for what orjson refuses."""
    out: list[str] = []
    # stack of (iterator, is_dict, first_flag_holder)
    stack: list[list[Any]] = []

    def open_container(value: Any) -> None:
        if isinstance(value, dict):
            items = value.items()
            if sort_keys:
                items = sorted(items, key=lambda kv: kv[0])
            out.append("{")
            stack.append([iter(items), True, True])
        else:
            out.append("[")
            stack.append([iter(value), False, True])

    def emit(value: Any) -> None:
        if isinstance(value, (dict, list, tuple)):
            open_container(value)
        else:
            out.append(_scalar_text(value))

    emit(obj)
    while stack:
        frame = stack[-1]
        it, is_dict, first = frame
        try:
            item = next(it)
        except StopIteration:
            out.append("}" if is_dict else "]")
            stack.pop()
            continue
        if not first:
            out.append(",")
        frame[2] = False
        if is_dict:
            key, value = item
            if not isinstance(key, str):
                raise TypeError("dict keys must be str")
            out.append(_encode_str(key))
            out.append(":")
            emit(value)
        else:
            emit(item)
    return "".join(out).encode("utf-8")


_REINDENT_SLICE = 1 << 20


def _reindent(compact: bytes, indent: int | str) -> bytes:
    from .core import Reformatter

    fmt = Reformatter(indent=indent)
    if len(compact) <= _REINDENT_SLICE:
        return fmt.feed(compact) + fmt.finish()
    # bounded feeds: the pure-Python Reformatter's memory grows with the feed size
    view = memoryview(compact)
    parts = [fmt.feed(view[i:i + _REINDENT_SLICE]) for i in range(0, len(compact), _REINDENT_SLICE)]
    parts.append(fmt.finish())
    return b"".join(parts)


def dumps(obj: Any, *, indent: int | str | None = None, sort_keys: bool = False) -> bytes:
    """Serialize to UTF-8 JSON bytes; numbers keep their exact text; non-ASCII stays raw.

    ``indent``: ``None`` (compact), ``2``, ``4``, any int 0..16 or ``"\\t"``.

    The result is a compact ``bytes`` object that may be kept: an ``orjson.dumps`` result occupies
    its whole output buffer -- at least 4 KiB, ~8x the text for strings and up to ~30x for big
    containers (orjson 3.13; ``sys.getsizeof`` shows only the text) -- so it is copied (~35 ns for
    a small result, a memcpy for a big one). A caller that writes, concatenates or decodes the result
    right away uses :func:`dumps_transient` (no copy).
    """
    option = _OPT_BASE
    if sort_keys:
        option |= orjson.OPT_SORT_KEYS
    if indent == 2:
        option |= orjson.OPT_INDENT_2
    try:
        out = orjson.dumps(obj, default=_default, option=option)
    except (orjson.JSONEncodeError, TypeError) as exc:
        return _fallback_dumps(obj, exc, indent, sort_keys)
    if indent is None or indent == 2:
        # a compact copy, the cheapest way measured (~35 ns for a record; memoryview(out).tobytes() ~75 ns);
        # bytes(out), out[:] and b"" + out return out itself
        return b"%b" % out
    return _reindent(out, indent)            # the reformatter's output is compact already


def dumps_transient(obj: Any, *, indent: int | str | None = None, sort_keys: bool = False) -> bytes:
    """:func:`dumps` without the compacting copy, for a result that is written, concatenated or
    decoded right away (export and bulk hot paths): it may occupy orjson's whole output buffer
    (at least 4 KiB, often several times the text) for as long as it is referenced -- never keep
    many of them (keep ``dumps`` results, or the text concatenated into something else)."""
    option = _OPT_BASE
    if sort_keys:
        option |= orjson.OPT_SORT_KEYS
    if indent == 2:
        option |= orjson.OPT_INDENT_2
    try:
        out = orjson.dumps(obj, default=_default, option=option)
    except (orjson.JSONEncodeError, TypeError) as exc:
        return _fallback_dumps(obj, exc, indent, sort_keys)
    if indent is None or indent == 2:
        return out
    return _reindent(out, indent)


def _fallback_dumps(obj: Any, exc: Exception, indent: int | str | None, sort_keys: bool) -> bytes:
    """What orjson refuses (lone surrogates, nesting deeper than 255, integers beyond 64 bits): the
    pure-Python encoder (compact output); a type that is not JSON at all re-raises ``exc``."""
    if isinstance(exc, TypeError) and "not JSON serializable" in str(exc):
        raise exc
    compact = _py_dumps(obj, sort_keys=sort_keys)
    if indent is None:
        return compact
    return _reindent(compact, indent)


# -- helpers ---------------------------------------------------------------------------
def is_number(v: Any) -> bool:
    return isinstance(v, (int, float, Decimal)) and not isinstance(v, bool)


def number_text(v: Any) -> str:
    """Exact JSON text of a number value."""
    if isinstance(v, (JsonFloat, JsonInt)):
        return v.raw
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, int):
        return int.__repr__(v)
    if isinstance(v, float):
        if math.isnan(v):
            return "NaN"
        if math.isinf(v):
            return "Infinity" if v > 0 else "-Infinity"
        return orjson.dumps(float(v)).decode("ascii")
    if isinstance(v, Decimal):
        return str(v)
    raise TypeError(f"not a number: {type(v).__name__}")


def value_text(v: Any) -> str | None:
    """Text used for searching/displaying a scalar: strings as-is, numbers exact,
    ``true``/``false``/``null``. Returns ``None`` for containers."""
    if isinstance(v, str):
        return v
    if v is None:
        return "null"
    if v is True:
        return "true"
    if v is False:
        return "false"
    if isinstance(v, (int, float, Decimal)):
        return number_text(v)
    return None


def json_type(v: Any) -> str:
    """``object``, ``array``, ``string``, ``number``, ``bool`` or ``null``."""
    if isinstance(v, dict):
        return "object"
    if isinstance(v, (list, tuple)):
        return "array"
    if isinstance(v, str):
        return "string"
    if v is None:
        return "null"
    if isinstance(v, bool):
        return "bool"
    if isinstance(v, (int, float, Decimal)):
        return "number"
    raise TypeError(f"not a JSON value: {type(v).__name__}")
