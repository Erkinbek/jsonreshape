"""JSON paths: parsing, formatting, patterns with wildcards, navigation.

Concrete paths are tuples of segments: ``str`` (object key) or ``int`` (array index).
Patterns may also contain :class:`Wild` segments (any key, any index, descendants).

Accepted syntaxes (see docs/ARCHITECTURE.md §3):

* dotted ``a.b[0].c``, quoted keys ``a["x.y"]`` / ``a['x']``, optional ``$`` / ``$.`` prefix
* wildcards ``*`` (any key), ``[*]`` / ``[]`` (any index), ``**`` or ``..name`` (descendants)
* JSON Pointer ``/a/0/c`` (``~0`` = ``~``, ``~1`` = ``/``)
* record reference ``#120`` (only via :func:`parse_location`)

A ``str`` segment applied to a list acts as an index when it is a canonical non-negative
decimal (``"0"`` or ``[1-9][0-9]*``) — the same rule as the core ``PathExtractor``.

:func:`parse_pointer` is the STRICT RFC 6901 parser of JSON Patch (§12.8): the text is used as it
is (no stripping), ``""`` is the whole document, every segment is a ``str``; :func:`format_pointer`
is its inverse.
"""

from __future__ import annotations

import enum
import json
import re
from dataclasses import dataclass
from typing import Any, Iterator, Union

from .errors import PathSyntaxError
from .i18n import t


class _Missing:
    _instance: _Missing | None = None

    def __new__(cls) -> _Missing:
        if cls._instance is None:
            cls._instance = super().__new__(cls)
        return cls._instance

    def __repr__(self) -> str:
        return "MISSING"

    def __bool__(self) -> bool:
        return False

    def __reduce__(self):
        return (_Missing, ())


MISSING: Any = _Missing()


class Wild(enum.Enum):
    ANY_INDEX = "[*]"
    ANY_KEY = "*"
    DESCENDANTS = "**"

    def __repr__(self) -> str:
        return f"Wild.{self.name}"


Segment = Union[str, int]
Path = tuple[Segment, ...]
PatternSegment = Union[str, int, Wild]

_CANON_INDEX_RE = re.compile(r"0|[1-9][0-9]*")


def canonical_index(key: str) -> int | None:
    """``"12"`` -> 12 (canonical non-negative decimal), else ``None``."""
    if _CANON_INDEX_RE.fullmatch(key):
        return int(key)
    return None


# ----------------------------------------------------------------------------------
# parsing
# ----------------------------------------------------------------------------------
_KEY_STOP = set(".[]\"'") | {" ", "\t", "\r", "\n"}


class _Parser:
    def __init__(self, text: str, allow_wild: bool) -> None:
        self.text = text
        self.pos = 0
        self.allow_wild = allow_wild
        self.segments: list[PatternSegment] = []

    def error(self, detail: str, pos: int | None = None) -> PathSyntaxError:
        return PathSyntaxError(pos=self.pos if pos is None else pos, text=self.text, detail=detail)

    def peek(self) -> str:
        return self.text[self.pos] if self.pos < len(self.text) else ""

    def add_wild(self, wild: Wild, pos: int) -> None:
        if not self.allow_wild:
            raise self.error(t("paths.err.wildcard_not_allowed"), pos)
        self.segments.append(wild)

    def parse(self) -> list[PatternSegment]:
        text = self.text
        n = len(text)
        if text.startswith("$"):
            self.pos = 1
            if self.pos == n:
                return self.segments
            if text[self.pos] not in ".[":
                raise self.error(t("paths.err.expected_dot_after_root"))
        if self.pos < n and text[self.pos] not in ".[" and self.pos == 0:
            self.read_key()
        while self.pos < n:
            c = text[self.pos]
            if c == ".":
                start = self.pos
                self.pos += 1
                if self.pos < n and text[self.pos] == ".":
                    # JSONPath recursive descent: '..name' / '..[0]' / '..*'
                    self.pos += 1
                    self.add_wild(Wild.DESCENDANTS, start)
                    if self.pos < n and text[self.pos] == "[":
                        continue
                    if self.pos >= n:
                        raise self.error(t("paths.err.key_after_descendants"))
                    self.read_key()
                    continue
                if self.pos >= n:
                    raise self.error(t("paths.err.key_after_dot"))
                if text[self.pos] == "[":
                    raise self.error(t("paths.err.bracket_after_dot"))
                self.read_key()
            elif c == "[":
                self.read_bracket()
            else:
                raise self.error(t("paths.err.unexpected_char", char=repr(c)))
        return self.segments

    def read_key(self) -> None:
        text = self.text
        start = self.pos
        while self.pos < len(text) and text[self.pos] not in _KEY_STOP:
            self.pos += 1
        key = text[start : self.pos]
        if not key:
            raise self.error(t("paths.err.empty_key"), start)
        if key == "*":
            self.add_wild(Wild.ANY_KEY, start)
        elif key == "**":
            self.add_wild(Wild.DESCENDANTS, start)
        else:
            self.segments.append(key)

    def read_bracket(self) -> None:
        text = self.text
        start = self.pos
        self.pos += 1  # '['
        n = len(text)
        while self.pos < n and text[self.pos] == " ":
            self.pos += 1
        if self.pos >= n:
            raise self.error(t("paths.err.unterminated_bracket"), start)
        c = text[self.pos]
        if c == "]":
            self.pos += 1
            self.add_wild(Wild.ANY_INDEX, start)
            return
        if c == "*":
            self.pos += 1
            self.expect_close(start)
            self.add_wild(Wild.ANY_INDEX, start)
            return
        if c in "\"'":
            key = self.read_quoted(c)
            self.expect_close(start)
            self.segments.append(key)
            return
        m = re.compile(r"-?[0-9]+").match(text, self.pos)
        if m is None:
            raise self.error(t("paths.err.bad_bracket"))
        token = m.group()
        if token.startswith("-"):
            raise self.error(t("paths.err.negative_index"), self.pos)
        self.pos = m.end()
        self.expect_close(start)
        self.segments.append(int(token))

    def expect_close(self, start: int) -> None:
        text = self.text
        while self.pos < len(text) and text[self.pos] == " ":
            self.pos += 1
        if self.pos >= len(text) or text[self.pos] != "]":
            raise self.error(t("paths.err.expected_close"), self.pos if self.pos < len(text) else start)
        self.pos += 1

    def read_quoted(self, quote: str) -> str:
        text = self.text
        start = self.pos
        self.pos += 1
        buf: list[str] = []
        while True:
            if self.pos >= len(text):
                raise self.error(t("paths.err.unterminated_quote"), start)
            c = text[self.pos]
            if c == quote:
                self.pos += 1
                break
            if c == "\\":
                if self.pos + 1 >= len(text):
                    raise self.error(t("paths.err.unterminated_escape"), self.pos)
                nxt = text[self.pos + 1]
                if nxt == "u":
                    hexpart = text[self.pos + 2 : self.pos + 6]
                    if len(hexpart) != 4 or not all(h in "0123456789abcdefABCDEF" for h in hexpart):
                        raise self.error(t("paths.err.bad_u_escape"), self.pos)
                    buf.append(chr(int(hexpart, 16)))
                    self.pos += 6
                    continue
                mapping = {"n": "\n", "t": "\t", "r": "\r", "b": "\b", "f": "\f", "/": "/", "\\": "\\",
                           '"': '"', "'": "'"}
                if nxt not in mapping:
                    raise self.error(t("paths.err.bad_escape", char=nxt), self.pos)
                buf.append(mapping[nxt])
                self.pos += 2
                continue
            buf.append(c)
            self.pos += 1
        return "".join(buf)


def _parse_pointer(text: str) -> list[PatternSegment]:
    if text == "":
        return []
    if not text.startswith("/"):
        raise PathSyntaxError(pos=0, text=text, detail=t("paths.err.pointer_start"))
    out: list[PatternSegment] = []
    pos = 1
    for raw in text[1:].split("/"):
        if re.search(r"~(?![01])", raw):
            raise PathSyntaxError(pos=pos, text=text, detail=t("paths.err.pointer_tilde"))
        out.append(raw.replace("~1", "/").replace("~0", "~"))
        pos += len(raw) + 1
    return out


def parse_pointer(text: str) -> tuple[str, ...]:
    """Parse a JSON Pointer STRICTLY (RFC 6901; JSON Patch, docs/ARCHITECTURE.md §12.8).

    ``""`` is the whole document; any other pointer must start with ``/``. Every segment is a
    ``str`` (also array indices: whether ``"12"`` is an index or a key depends on the value it is
    applied to); ``~1`` becomes ``/`` and then ``~0`` becomes ``~`` (so ``~01`` is the key ``~1``);
    any other ``~`` is an error. Nothing is stripped: whitespace belongs to the keys (``"/ a"`` is
    the key ``" a"``) and ``"/"`` is the empty key. :class:`PathSyntaxError` otherwise.

    The lenient path parser (:func:`parse_path`, which accepts pointers next to the other
    syntaxes and ignores leading whitespace) is unchanged."""
    if not isinstance(text, str):
        raise TypeError(f"a JSON Pointer is a str, got {type(text).__name__}")
    return tuple(_parse_pointer(text))  # type: ignore[arg-type]


def format_pointer(path: tuple[Segment, ...] | list[Segment]) -> str:
    """The JSON Pointer of a concrete path (``~`` -> ``~0``, ``/`` -> ``~1``; ``()`` -> ``""``);
    the inverse of :func:`parse_pointer` (ints become their decimal text)."""
    return format_path(tuple(path), "pointer")


def _parse_segments(text: str, allow_wild: bool) -> list[PatternSegment]:
    lead = text.lstrip()
    if lead.startswith("/"):
        # whitespace inside and at the end of a JSON Pointer is part of the keys
        return _parse_pointer(lead)
    text = text.strip()
    if text in ("", "$"):
        return []
    return _Parser(text, allow_wild).parse()


def parse_path(text: str) -> Path:
    """Parse a concrete path (no wildcards)."""
    return tuple(_parse_segments(text, allow_wild=False))  # type: ignore[return-value]


def parse_pattern(text: str) -> PathPattern:
    """Parse a path pattern (wildcards allowed)."""
    lead = text.lstrip()
    shown = lead if lead.startswith("/") else text.strip()  # pointer: trailing spaces are key text
    return PathPattern(tuple(_parse_segments(text, allow_wild=True)), shown)


@dataclass(frozen=True)
class Location:
    """Result of :func:`parse_location` (Ctrl+G).

    ``record`` is set for ``#N`` references; ``path`` is relative to that record when
    ``record`` is not ``None``, otherwise absolute in the document.
    """

    record: int | None
    path: Path
    text: str


def parse_location(text: str) -> Location:
    """Parse a Ctrl+G location: ``#120``, ``#120.a``, ``#120/a``, ``data[120].x``,
    ``$.data[120]``, ``/data/120``. Error positions refer to the stripped ``text``."""
    raw = text.strip()
    if raw.startswith("#"):
        m = re.compile(r"#\s*([0-9]+)").match(raw)
        if m is None:
            raise PathSyntaxError(pos=1, text=raw, detail=t("paths.err.record_number"))
        record = int(m.group(1))
        rest_start = m.end()
        while rest_start < len(raw) and raw[rest_start].isspace():
            rest_start += 1
        rest = raw[rest_start:]
        if rest == "":
            return Location(record, (), raw)
        if rest.startswith("/"):
            sub, shift = rest, rest_start
        elif rest.startswith((".", "[")):
            sub, shift = "$" + rest, rest_start - 1
        else:
            raise PathSyntaxError(pos=rest_start, text=raw, detail=t("paths.err.after_record"))
        try:
            path = parse_path(sub)
        except PathSyntaxError as exc:
            # report the position in the text the user typed, not in the helper string
            raise PathSyntaxError(pos=max(0, exc.pos + shift), text=raw,
                                  detail=exc.params.get("detail", "")) from None
        return Location(record, path, raw)
    return Location(None, parse_path(raw), raw)


# ----------------------------------------------------------------------------------
# formatting
# ----------------------------------------------------------------------------------
_DOTTED_KEY_RE = re.compile(r"[^\x00-\x1f\x7f\s.\[\]\"'\\*#/0-9$][^\x00-\x1f\x7f\s.\[\]\"'\\]*")
_ASCII_IDENT_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
_JS_IDENT_RE = re.compile(r"[A-Za-z_$][A-Za-z0-9_$]*")


def _json_str(s: str) -> str:
    return json.dumps(s, ensure_ascii=False)


def _wild_text(seg: Wild, style: str) -> str:
    # every style uses the parseable forms: "[*]" is ANY_INDEX, so ANY_KEY is ".*" even
    # in the python style; jsonpath renders "**" + segment as "..segment" (format_path)
    if seg is Wild.ANY_INDEX:
        return "[*]"
    if seg is Wild.ANY_KEY:
        return ".*"
    return ".**"


def _jsonpath_segment(seg: PatternSegment) -> str:
    if isinstance(seg, Wild):
        return _wild_text(seg, "jsonpath")
    if isinstance(seg, int):
        return f"[{seg}]"
    if _ASCII_IDENT_RE.fullmatch(seg):
        return "." + seg
    escaped = seg.replace("\\", "\\\\").replace("'", "\\'")
    return f"['{escaped}']"


def format_path(path: tuple[PatternSegment, ...], style: str = "dotted") -> str:
    """Render a path in one of the copy formats: dotted, jsonpath, pointer, python, js."""
    if style == "pointer":
        parts = []
        for seg in path:
            if isinstance(seg, Wild):
                raise ValueError("JSON Pointer cannot express wildcards")
            parts.append(str(seg).replace("~", "~0").replace("/", "~1"))
        return "".join("/" + p for p in parts)
    if style == "python":
        out = []
        for seg in path:
            if isinstance(seg, Wild):
                out.append(_wild_text(seg, style))
            elif isinstance(seg, int):
                out.append(f"[{seg}]")
            else:
                out.append(f"[{_json_str(seg)}]")
        return "".join(out)
    if style == "jsonpath":
        out = ["$"]
        i = 0
        while i < len(path):
            seg = path[i]
            nxt = path[i + 1] if i + 1 < len(path) else None
            if seg is Wild.DESCENDANTS and nxt is not None and nxt is not Wild.DESCENDANTS:
                # JSONPath recursive descent: "$..name", "$..[0]", "$..*", "$..['x y']"
                txt = _jsonpath_segment(nxt)
                out.append(".." + (txt[1:] if txt.startswith(".") else txt))
                i += 2
                continue
            out.append(_jsonpath_segment(seg))  # a trailing "**" stays ".**"
            i += 1
        return "".join(out)
    if style in ("dotted", "js"):
        key_re = _DOTTED_KEY_RE if style == "dotted" else _JS_IDENT_RE
        out = []
        for i, seg in enumerate(path):
            if isinstance(seg, Wild):
                txt = _wild_text(seg, style)
                if i == 0 and txt.startswith("."):
                    txt = txt[1:]
                out.append(txt)
            elif isinstance(seg, int):
                out.append(f"[{seg}]")
            elif key_re.fullmatch(seg) and seg not in ("*", "**") and not (i == 0 and seg.startswith("$")):
                # a leading "$..." would be read back as the root marker
                out.append(seg if i == 0 else "." + seg)
            else:
                out.append(f"[{_json_str(seg)}]")
        return "".join(out)
    raise ValueError(f"unknown path style: {style!r}")


# ----------------------------------------------------------------------------------
# navigation
# ----------------------------------------------------------------------------------
def _step(value: Any, seg: Segment) -> Any:
    if isinstance(value, dict):
        if isinstance(seg, str):
            return value.get(seg, MISSING)
        return MISSING
    if isinstance(value, list):
        if isinstance(seg, int):
            idx = seg
        else:
            idx = canonical_index(seg)
            if idx is None:
                return MISSING
        if 0 <= idx < len(value):
            return value[idx]
        return MISSING
    return MISSING


def get(value: Any, path: Path, default: Any = MISSING) -> Any:
    """Navigate a concrete path; returns ``default`` when any step is missing."""
    cur = value
    for seg in path:
        cur = _step(cur, seg)
        if cur is MISSING:
            return default
    return cur


def _walk_descendants(value: Any, path: Path) -> Iterator[tuple[Path, Any]]:
    """The value itself and every descendant, pre-order (document order)."""
    stack: list[tuple[Path, Any]] = [(path, value)]
    while stack:
        p, v = stack.pop()
        yield p, v
        if isinstance(v, dict):
            items = list(v.items())
            for k, child in reversed(items):
                stack.append((p + (k,), child))
        elif isinstance(v, list):
            for i in range(len(v) - 1, -1, -1):
                stack.append((p + (i,), v[i]))


class PathPattern:
    """A path that may contain wildcards."""

    __slots__ = ("segments", "text", "is_concrete")

    def __init__(self, segments: tuple[PatternSegment, ...], text: str | None = None) -> None:
        self.segments = tuple(segments)
        self.text = text if text is not None else format_path(self.segments)
        self.is_concrete = not any(isinstance(s, Wild) for s in self.segments)

    def __repr__(self) -> str:
        return f"PathPattern({self.text!r})"

    def __eq__(self, other: object) -> bool:
        return isinstance(other, PathPattern) and other.segments == self.segments

    def __hash__(self) -> int:
        return hash(self.segments)

    def to_path(self) -> Path:
        if not self.is_concrete:
            raise ValueError("pattern contains wildcards")
        return self.segments  # type: ignore[return-value]

    # -- matching paths ----------------------------------------------------------------
    @staticmethod
    def _seg_matches(pseg: PatternSegment, seg: Segment) -> bool:
        # Path segments are typed: int = array position, str = object key. An int
        # (index) pattern segment therefore never matches an object key, even "0" --
        # exactly like resolve()/get() and the core PathExtractor. A str pattern segment
        # matches an equal key, or an array position when it is a canonical index.
        if pseg is Wild.ANY_INDEX:
            return isinstance(seg, int)
        if pseg is Wild.ANY_KEY:
            return isinstance(seg, str)
        if isinstance(pseg, int):
            return isinstance(seg, int) and seg == pseg
        # str pattern segment
        if isinstance(seg, int):
            return canonical_index(pseg) == seg
        return seg == pseg

    def _match(self, path: Path, prefix: bool) -> bool:
        segs = self.segments

        # memoised recursive matcher (patterns are short)
        memo: dict[tuple[int, int], bool] = {}

        def rec(i: int, j: int) -> bool:
            key = (i, j)
            if key in memo:
                return memo[key]
            if i == len(segs):
                res = prefix or j == len(path)
            elif segs[i] is Wild.DESCENDANTS:
                res = any(rec(i + 1, k) for k in range(j, len(path) + 1))
            elif j == len(path):
                res = False
            else:
                res = self._seg_matches(segs[i], path[j]) and rec(i + 1, j + 1)
            memo[key] = res
            return res

        return rec(0, 0)

    def matches(self, path: Path) -> bool:
        return self._match(tuple(path), prefix=False)

    def matches_prefix_of(self, path: Path) -> bool:
        """True when ``path`` equals a match of this pattern or lies below one."""
        return self._match(tuple(path), prefix=True)

    # -- resolving against values -----------------------------------------------------
    def resolve(self, value: Any, base: Path = ()) -> list[tuple[Path, Any]]:
        """All ``(path, value)`` matches in document order (dict semantics).

        Result paths are typed like the document (int for array positions, str for
        object keys), each node appears at most once, and a node precedes its
        descendants -- also for ``**`` patterns such as ``**.*``.
        """
        if any(s is Wild.DESCENDANTS for s in self.segments):
            return self._resolve_walk(value, base)
        # fixed depth: expanding segment by segment keeps document order
        results: list[tuple[Path, Any]] = [(base, value)]
        for seg in self.segments:
            nxt: list[tuple[Path, Any]] = []
            for p, v in results:
                if seg is Wild.ANY_INDEX:
                    if isinstance(v, list):
                        nxt.extend((p + (i,), item) for i, item in enumerate(v))
                elif seg is Wild.ANY_KEY:
                    if isinstance(v, dict):
                        nxt.extend((p + (k,), item) for k, item in v.items())
                else:
                    child = _step(v, seg)  # type: ignore[arg-type]
                    if child is not MISSING:
                        step_seg: Segment = seg  # type: ignore[assignment]
                        if isinstance(v, list) and isinstance(seg, str):
                            step_seg = int(seg)
                        nxt.append((p + (step_seg,), child))
            results = nxt
            if not results:
                break
        return results

    def _resolve_walk(self, value: Any, base: Path) -> list[tuple[Path, Any]]:
        """Pre-order walk carrying the set of pattern positions reached (an NFA); only
        subtrees that can still match are visited."""
        segs = self.segments
        n = len(segs)
        closures: dict[frozenset[int], frozenset[int]] = {}

        def closure(states: frozenset[int]) -> frozenset[int]:
            # '**' may match zero segments: reaching position i also reaches i + 1
            cached = closures.get(states)
            if cached is None:
                out = set(states)
                for i in sorted(states):
                    j = i
                    while j < n and segs[j] is Wild.DESCENDANTS:
                        j += 1
                        out.add(j)
                cached = closures[states] = frozenset(out)
            return cached

        results: list[tuple[Path, Any]] = []
        stack: list[tuple[Path, Any, frozenset[int]]] = [(base, value, closure(frozenset({0})))]
        while stack:
            path, v, states = stack.pop()
            if n in states:
                results.append((path, v))
            if isinstance(v, dict):
                children: Iterator[tuple[Segment, Any]] = iter(v.items())
            elif isinstance(v, list):
                children = enumerate(v)
            else:
                continue
            pushes = []
            for seg, child in children:
                nxt = set()
                for i in states:
                    if i == n:
                        continue
                    pseg = segs[i]
                    if pseg is Wild.DESCENDANTS:
                        nxt.add(i)
                    elif self._seg_matches(pseg, seg):
                        nxt.add(i + 1)
                if nxt:
                    pushes.append((path + (seg,), child, closure(frozenset(nxt))))
            stack.extend(reversed(pushes))
        return results

    def values(self, value: Any) -> list[Any]:
        if self.is_concrete:
            v = get(value, self.segments)  # type: ignore[arg-type]
            return [] if v is MISSING else [v]
        return [v for _, v in self.resolve(value)]

    def to_core(self) -> list[tuple]:
        """Segments for :class:`jsonreshape.core.PathExtractor`."""
        out: list[tuple] = []
        for seg in self.segments:
            if seg is Wild.ANY_INDEX:
                out.append(("any_index",))
            elif seg is Wild.ANY_KEY:
                out.append(("any_key",))
            elif seg is Wild.DESCENDANTS:
                raise ValueError("descendant wildcards are not supported by the core extractor")
            elif isinstance(seg, int):
                out.append(("index", seg))
            else:
                out.append(("key", seg))
        return out


def iter_leaves(value: Any, base: Path = ()) -> Iterator[tuple[Path, Any]]:
    """Pre-order walk yielding every node ``(path, value)`` (containers included)."""
    yield from _walk_descendants(value, base)
