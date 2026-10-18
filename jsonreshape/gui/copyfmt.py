"""Copy formats of the viewer (AGENT_PROMPT §5 "Yo'lni nusxalash formatlari", "Qiymatni
nusxalash"; docs/ARCHITECTURE.md §11.7). Pure Python: no Qt import.

Paths (:func:`path_text`), styles of :data:`PATH_STYLES`, rendered with
:func:`jsonreshape.paths.format_path`:

=========  ======================  =========================================
style      ``("a", 0, "x y")``      empty path (the document root)
=========  ======================  =========================================
dotted     ``a[0]["x y"]``          ``$`` (what Ctrl+G / ``parse_path`` read as the root)
jsonpath   ``$.a[0]['x y']``        ``$``
pointer    ``/a/0/x y``             ``""`` (RFC 6901: the whole document)
python     ``["a"][0]["x y"]``      ``""`` (an accessor suffix: nothing to apply)
js         ``a[0]["x y"]``          ``""``
=========  ======================  =========================================

A key holding a lone surrogate (only possible from a ``\\ud800`` escape in the file)
cannot travel through the clipboard (Qt drops it), so it is written in its quoted, escaped
form (``["\\ud800"]``; jsonpath ``['\\ud800']``), which :func:`jsonreshape.paths.parse_path`
reads back to the same key. JSON Pointer has no escapes: there the surrogate becomes the
six characters ``\\ud800`` (documented loss).

Values (:func:`value_text`), formats of :data:`VALUE_FORMATS` (plus ``"csv_header"``):

* ``json``: pretty JSON (``indent`` 2, 4, ``"\\t"``; ``None`` = compact) and
  ``json_compact``: :func:`jsonreshape.codec.dumps` -- every number keeps its exact source
  text (``1.10``, ``1e400``, ``-0.0``, ``12345678901234567890``), non-ASCII is never
  ``\\u``-escaped, lone surrogates are written as ``\\udXXX`` escapes.
* ``python``: a Python literal (``True``/``False``/``None``, ``repr()`` of strings, dicts
  and lists laid out like the pretty JSON with ``indent``, on one line when ``indent`` is
  ``None``). Numbers are their exact JSON text, which is always a valid Python literal.
  When Python would read that literal as a different value -- a float overflowing to
  infinity (``1e400``) or a non-zero float underflowing to zero (``1e-400``) -- the number
  is written as ``float("1e400")``: it still evaluates without imports (to the same value
  Python's own ``json`` module would produce), keeps the exact text visible, and never
  shows ``inf``. Integers are always exact literals (Python ints are unbounded; above
  4300 digits Python needs ``sys.set_int_max_str_digits(0)`` first).
* ``csv``: ONE CSV line with the value's leaves flattened like the CSV writer's ``join``
  mode (:mod:`jsonreshape.writer`): objects are descended (dotted column names, objects
  nested deeper than :data:`jsonreshape.writer.MAX_DEPTH` levels and empty objects are
  one compact-JSON cell), an array is one cell (scalar elements joined with
  ``Settings.array_join_sep``, container elements as compact JSON), strings as they are,
  numbers as their exact text, ``true``/``false``, null empty. A value that is not a
  non-empty object is the single column ``$``. RFC 4180 quoting with
  ``Settings.csv_delimiter``; :func:`csv_header_and_row` / ``"csv_header"`` adds the header
  line. Lines are separated by ``"\\n"`` (Qt turns it into CRLF on the Windows clipboard).

:func:`tsv_text` builds Excel paste text; :func:`scalar_cell_text` the display text of a
table cell.
"""

from __future__ import annotations

import math
import re
from decimal import Decimal
from typing import Any, Iterable

from jsonreshape import codec
from jsonreshape.i18n import t
from jsonreshape.paths import format_path

__all__ = [
    "COPY_FORMATS",
    "PATH_STYLES",
    "VALUE_FORMATS",
    "csv_header_and_row",
    "csv_line",
    "escape_surrogates",
    "flatten_row",
    "format_label",
    "key_text",
    "path_text",
    "scalar_cell_text",
    "style_label",
    "tsv_text",
    "value_text",
]

PATH_STYLES: tuple[str, ...] = ("dotted", "jsonpath", "pointer", "python", "js")
VALUE_FORMATS: tuple[str, ...] = ("json", "json_compact", "python", "csv")
#: Every format :func:`value_text` accepts (the menus offer all of them).
COPY_FORMATS: tuple[str, ...] = VALUE_FORMATS + ("csv_header",)

#: Column of a value that is not a non-empty object (the writer's ``RECORD_COLUMN``).
ROOT_COLUMN = "$"
#: Objects nested deeper than this are one compact-JSON cell (the writer's ``MAX_DEPTH``).
MAX_DEPTH = 64
ELLIPSIS = "…"
LINE_BREAK_MARK = "↵"   # "↵": a line break shown on one line

_SURROGATE_RE = re.compile("[\ud800-\udfff]")
_LINE_BREAK_RE = re.compile("\r\n|\r|\n")


# ----------------------------------------------------------------------------------
# labels
# ----------------------------------------------------------------------------------
def style_label(style: str) -> str:
    """Localized name of a path style (menus)."""
    labels = {
        "dotted": t("gui.copy.style.dotted"),
        "jsonpath": t("gui.copy.style.jsonpath"),
        "pointer": t("gui.copy.style.pointer"),
        "python": t("gui.copy.style.python"),
        "js": t("gui.copy.style.js"),
    }
    try:
        return labels[style]
    except KeyError:
        raise ValueError(f"unknown path style: {style!r}") from None


def format_label(fmt: str) -> str:
    """Localized name of a value format (menus)."""
    labels = {
        "json": t("gui.copy.format.json"),
        "json_compact": t("gui.copy.format.json_compact"),
        "python": t("gui.copy.format.python"),
        "csv": t("gui.copy.format.csv"),
        "csv_header": t("gui.copy.format.csv_header"),
    }
    try:
        return labels[fmt]
    except KeyError:
        raise ValueError(f"unknown value format: {fmt!r}") from None


# ----------------------------------------------------------------------------------
# helpers
# ----------------------------------------------------------------------------------
def escape_surrogates(text: str) -> str:
    """Lone surrogates -> the six characters ``\\udXXX`` (Qt drops them on the clipboard
    and in widgets)."""
    if _SURROGATE_RE.search(text) is None:
        return text
    return _SURROGATE_RE.sub(lambda m: "\\u%04x" % ord(m.group()), text)


def _has_surrogate(seg: Any) -> bool:
    return isinstance(seg, str) and _SURROGATE_RE.search(seg) is not None


# ----------------------------------------------------------------------------------
# paths
# ----------------------------------------------------------------------------------
def _escaped_segment(seg: str, style: str) -> str:
    """Quoted segment text with lone surrogates as ``\\u`` escapes (see module doc)."""
    if style == "pointer":
        return "/" + escape_surrogates(seg).replace("~", "~0").replace("/", "~1")
    if style == "jsonpath":
        body = escape_surrogates(seg.replace("\\", "\\\\").replace("'", "\\'"))
        return f"['{body}']"
    # dotted / js / python: the JSON string form (codec escapes lone surrogates)
    return "[" + codec.dumps(seg).decode("utf-8") + "]"


def path_text(path: Iterable[Any], style: str = "dotted") -> str:
    """The text of a concrete path in one of :data:`PATH_STYLES` (see the module doc for
    the empty path)."""
    if style not in PATH_STYLES:
        raise ValueError(f"unknown path style: {style!r}")
    path = tuple(path)
    if not path:
        return "$" if style in ("dotted", "jsonpath") else ""
    if not any(_has_surrogate(seg) for seg in path):
        return format_path(path, style)
    # Segment by segment: for concrete paths format_path is a concatenation of segment
    # texts that depend only on the segment and on whether it comes first, so a plain
    # segment keeps exactly the text format_path gives it.
    out: list[str] = ["$"] if style == "jsonpath" else []
    head = format_path(("x",), style)
    for i, seg in enumerate(path):
        if _has_surrogate(seg):
            out.append(_escaped_segment(seg, style))
        elif i == 0:
            piece = format_path((seg,), style)
            out.append(piece[1:] if style == "jsonpath" else piece)
        else:
            out.append(format_path(("x", seg), style)[len(head):])
    return "".join(out)


def key_text(path: Iterable[Any]) -> str | None:
    """The last segment of a path as text (a member key, or an array index in decimal);
    ``None`` for the empty path (the root has no key)."""
    path = tuple(path)
    if not path:
        return None
    seg = path[-1]
    if isinstance(seg, int) and not isinstance(seg, bool):
        return str(seg)
    return escape_surrogates(str(seg))


# ----------------------------------------------------------------------------------
# scalar texts
# ----------------------------------------------------------------------------------
def _number_text(v: Any) -> str:
    return codec.number_text(v)


def _scalar_csv(v: Any) -> str:
    """CSV/TSV cell text of a scalar (strings raw, numbers exact, null empty)."""
    if v is None:
        return ""
    if v is True:
        return "true"
    if v is False:
        return "false"
    if isinstance(v, str):
        return escape_surrogates(v)
    return _number_text(v)


def _compact_json(v: Any) -> str:
    return codec.dumps(v).decode("utf-8")


def scalar_cell_text(value: Any, max_chars: int | None = None, *, single_line: bool = False) -> str:
    """Display text of a table cell: strings raw (not quoted), numbers their exact text,
    ``true``/``false``/``null``, arrays ``[n]`` and objects ``{n}`` (``n`` = child count).

    ``max_chars``: longer texts are cut to ``max_chars`` characters ending with ``…``.
    ``single_line``: line breaks are shown as ``↵`` and tabs as spaces (painting in one
    row). Lone surrogates are always shown escaped (``\\ud800``).
    """
    if isinstance(value, dict):
        text = "{" + str(len(value)) + "}"
    elif isinstance(value, (list, tuple)):
        text = "[" + str(len(value)) + "]"
    elif value is None:
        text = "null"
    elif value is True:
        text = "true"
    elif value is False:
        text = "false"
    elif isinstance(value, str):
        text = escape_surrogates(value)
    elif isinstance(value, (int, float, Decimal)):
        text = _number_text(value)
    else:
        text = escape_surrogates(str(value))
    if max_chars is not None and len(text) > max_chars:
        # cut before the line-break replacement so the result stays within max_chars
        text = text[: max(0, max_chars - 1)] + ELLIPSIS if max_chars > 0 else ""
    if single_line:
        text = _LINE_BREAK_RE.sub(LINE_BREAK_MARK, text).replace("\t", " ")
    return text


# ----------------------------------------------------------------------------------
# python literal
# ----------------------------------------------------------------------------------
_EXP_SPLIT_RE = re.compile(r"[eE]")
_INT_TOKEN_RE = re.compile(r"-?(?:0|[1-9][0-9]*)")


def _python_number(v: Any) -> str:
    text = _number_text(v)
    if isinstance(v, int):
        return text  # exact; Python ints are unbounded
    if _INT_TOKEN_RE.fullmatch(text):
        # an integer token the codec keeps as JsonFloat (more than 4000 digits): the exact
        # integer, not float("...") = inf
        return text
    as_float = float(v)  # JsonFloat: the nearest double; Decimal: never raises
    if math.isnan(as_float) or math.isinf(as_float):
        return f'float("{text}")'
    if as_float == 0.0:
        mantissa = _EXP_SPLIT_RE.split(text, 1)[0]
        if any(c in "123456789" for c in mantissa):
            return f'float("{text}")'  # a non-zero number Python would read as 0.0
    return text


def _python_scalar(v: Any) -> str:
    if v is None:
        return "None"
    if v is True:
        return "True"
    if v is False:
        return "False"
    if isinstance(v, str):
        return repr(str(v))
    if isinstance(v, (int, float, Decimal)):
        return _python_number(v)
    raise TypeError(f"not a JSON value: {type(v).__name__}")


def _indent_unit(indent: int | str | None) -> str | None:
    if indent is None:
        return None
    if indent == "\t":
        return "\t"
    if isinstance(indent, int) and not isinstance(indent, bool) and 0 <= indent <= 16:
        return " " * indent
    raise ValueError(f"invalid indent: {indent!r}")


def _python_text(value: Any, indent: int | str | None) -> str:
    """Python literal of ``value`` (iterative: no recursion limit on deep values)."""
    unit = _indent_unit(indent)
    out: list[str] = []
    # frames: [iterator, is_dict, first, level]
    stack: list[list[Any]] = []

    def newline(level: int) -> None:
        if unit is not None:
            out.append("\n" + unit * level)

    def emit(v: Any, level: int) -> None:
        if isinstance(v, dict):
            if not v:
                out.append("{}")
                return
            out.append("{")
            stack.append([iter(v.items()), True, True, level + 1])
        elif isinstance(v, (list, tuple)):
            if not v:
                out.append("[]")
                return
            out.append("[")
            stack.append([iter(v), False, True, level + 1])
        else:
            out.append(_python_scalar(v))

    emit(value, 0)
    while stack:
        frame = stack[-1]
        it, is_dict, first, level = frame
        try:
            item = next(it)
        except StopIteration:
            stack.pop()
            newline(level - 1)
            out.append("}" if is_dict else "]")
            continue
        if not first:
            out.append("," if unit is not None else ", ")
        frame[2] = False
        newline(level)
        if is_dict:
            key, v = item
            out.append(repr(str(key)))
            out.append(": ")
            emit(v, level)
        else:
            emit(item, level)
    return "".join(out)


# ----------------------------------------------------------------------------------
# CSV
# ----------------------------------------------------------------------------------
def _join_cell(items: list[Any], sep: str) -> str:
    parts = []
    for e in items:
        if isinstance(e, (dict, list, tuple)):
            parts.append(_compact_json(e))
        else:
            parts.append(_scalar_csv(e))
    return sep.join(parts)


def _cell(v: Any, sep: str) -> str:
    if isinstance(v, dict):
        return _compact_json(v)
    if isinstance(v, (list, tuple)):
        return _join_cell(list(v), sep)
    return _scalar_csv(v)


def flatten_row(value: Any, *, sep: str | None = None) -> tuple[list[str], list[str]]:
    """``(column names, cell texts)`` of one value, like a CSV export in ``join`` mode
    (see the module doc). ``sep`` defaults to ``Settings.array_join_sep``."""
    if sep is None:
        from jsonreshape.settings import get_settings

        sep = get_settings().array_join_sep
    if not isinstance(value, dict) or not value:
        return [ROOT_COLUMN], [_cell(value, sep)]
    names: list[str] = []
    cells: list[str] = []
    # iterative pre-order walk of the objects, keys in document order
    stack: list[tuple[tuple, Any]] = [((), iter(value.items()))]
    while stack:
        base, it = stack[-1]
        try:
            key, v = next(it)
        except StopIteration:
            stack.pop()
            continue
        path = base + (key,)
        if isinstance(v, dict) and v and len(path) < MAX_DEPTH:
            stack.append((path, iter(v.items())))
            continue
        names.append(path_text(path, "dotted"))
        cells.append(_cell(v, sep))
    return names, cells


def csv_line(cells: Iterable[str], delimiter: str | None = None) -> str:
    """One RFC 4180 line (no line terminator): a cell containing the delimiter, a quote,
    CR or LF is quoted, quotes doubled."""
    if delimiter is None:
        from jsonreshape.settings import get_settings

        delimiter = get_settings().csv_delimiter
    specials = (delimiter, '"', "\r", "\n")
    out = []
    for cell in cells:
        cell = escape_surrogates(str(cell))
        if any(s in cell for s in specials):
            cell = '"' + cell.replace('"', '""') + '"'
        out.append(cell)
    return delimiter.join(out)


def csv_header_and_row(value: Any, *, sep: str | None = None, delimiter: str | None = None) -> str:
    """Header line + value line (``"\\n"`` between them, none at the end)."""
    names, cells = flatten_row(value, sep=sep)
    return csv_line(names, delimiter) + "\n" + csv_line(cells, delimiter)


# ----------------------------------------------------------------------------------
# value text
# ----------------------------------------------------------------------------------
def value_text(value: Any, fmt: str = "json", *, indent: int | str | None = 2, sep: str | None = None,
               delimiter: str | None = None) -> str:
    """The clipboard text of a decoded value (:func:`jsonreshape.codec.loads` result) in
    one of :data:`COPY_FORMATS` (see the module doc). ``indent`` applies to ``json`` and
    ``python``; ``sep`` / ``delimiter`` to the CSV formats (default: the settings)."""
    if fmt == "json":
        _indent_unit(indent)  # validate
        return codec.dumps(value, indent=indent).decode("utf-8")
    if fmt == "json_compact":
        return codec.dumps(value).decode("utf-8")
    if fmt == "python":
        return _python_text(value, indent)
    if fmt == "csv":
        return csv_line(flatten_row(value, sep=sep)[1], delimiter)
    if fmt == "csv_header":
        return csv_header_and_row(value, sep=sep, delimiter=delimiter)
    raise ValueError(f"unknown value format: {fmt!r}")


# ----------------------------------------------------------------------------------
# TSV (Excel)
# ----------------------------------------------------------------------------------
def tsv_text(rows: Iterable[Iterable[Any]]) -> str:
    """Rows of cell texts as Excel paste text: cells separated by tabs, rows by ``"\\n"``
    (no trailing line break); a cell containing a tab, CR, LF or a quote is quoted with
    its quotes doubled (what Excel writes and reads)."""
    lines = []
    for row in rows:
        cells = []
        for cell in row:
            text = escape_surrogates("" if cell is None else str(cell))
            if "\t" in text or "\n" in text or "\r" in text or '"' in text:
                text = '"' + text.replace('"', '""') + '"'
            cells.append(text)
        lines.append("\t".join(cells))
    return "\n".join(lines)
