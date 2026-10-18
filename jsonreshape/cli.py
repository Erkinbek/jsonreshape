"""Command-line interface (AGENT_PROMPT §13, §11, §1.4; docs/ARCHITECTURE.md §10).

::

    jsonreshape [--lang uz|en] [--backend auto|rust|python] [-q] [-v|-vv] [--version] COMMAND ...

    inspect  INPUT [--sample N] [--json]
    search   INPUT TEXT [--keys|--values] [--regex|--number] [--case-sensitive] [--whole-word]
             [--translit] [--path P] [--limit N] [--count] [--export FILE.csv|FILE.json] [--max-hits N]
    filter   INPUT OUTPUT (--where EXPR | --jmespath EXPR | --preset NAME) [--save-preset NAME]
             [output options]
    sort     INPUT OUTPUT --by FIELD [--by -FIELD ...] [--nulls first|last]
             [--collation uz|simple|unicode] [--no-natural] [--case-sensitive] [--memory-limit SIZE]
             [output options]
    convert  INPUT OUTPUT [--where EXPR | --jmespath EXPR | --preset NAME] [--save-preset NAME]
             [--sort-by FIELD ...] [sort options] [output options]
    presets  [--delete NAME] [--json]

    input options (every command but presets): --record-path P,
        --format auto|array|jsonl|brace_list|wrapped|keyed|single|stream, --allow-partial, --no-cache
    output options: --to json|jsonl|csv|xlsx|sqlite (default: from the OUTPUT extension, see
        writer.format_for_path), --jsonl (= --to jsonl), --compact, --indent 2|4|tab,
        --sort-keys [alpha|codepoint], --no-wrapper, --as-keyed, --columns a,b.c,
        --array-mode join|rows, --explode PATH, --sep ";", --delimiter "," (also "tab"), --no-bom,
        --xlsx-split sheets|files

The global options may also be given after the command (``jsonreshape inspect a.json --lang en``).
:func:`main` is the entry point of ``python -m jsonreshape`` and of the ``jsonreshape`` console
script; it returns the exit code:

* 0 -- success;
* 1 -- user error: bad arguments (with the usage line), an invalid filter/search expression,
  sort field or path (printed with a caret line under the error position; a JMESPath
  syntax error gets a JMESPath example as its hint; a record path with a syntax error gets
  record-path examples), a missing input file, a record path that does not exist or does
  not lead to an array/object (with the record arrays found at the start of the file --
  ``--allow-partial`` never turns it into an empty success), a record path on an input
  detected as JSON Lines or as several JSON values (see "Decisions"), OUTPUT equal to
  INPUT, a read-only output file, an unwritable output folder, an unknown filter preset, a
  filter that could not be evaluated on any record (the filter runner's
  :class:`~jsonreshape.errors.UserInputError`, with its message and hint), file-system
  errors (see "OS errors" below), ...;
* 2 -- JSON error in the input: message, the text around the error with a caret, and a hint:
  ``--allow-partial`` (work with the records before the error), or ``--format jsonl`` /
  ``--format stream`` when the error is a second JSON value after a complete first one (or
  the reader's ``format_hint`` says so); also an input that is not usable at all (empty
  file, damaged gzip archive, a forced format that does not match) and an input that
  another program changed while the command read it
  (:class:`~jsonreshape.errors.FileChangedError`: nothing is saved, run it again);
* 3 -- cancelled (Ctrl+C or Ctrl+Break, or an injected :class:`~jsonreshape.tasks.CancelToken`);
* 4 -- internal error: a short localized message and the log file path; the traceback is
  written to the log (and to stderr with ``-vv``).

Start-up order
==============
1. stdout/stderr are reconfigured to UTF-8 when they are not a console (redirected output
   keeps Uzbek/Cyrillic text; lone surrogates become ``\\udXXX`` text instead of crashing);
   file logging starts (:func:`jsonreshape.log.setup_logging`).
2. The language is chosen: ``--lang`` > ``JSONRESHAPE_LANG`` > ``settings.language`` >
   ``uz`` (``--lang`` is looked up in the raw arguments first, because the parser itself
   is localized).
3. The arguments are parsed. Every argparse text is localized: our help texts come from
   :func:`jsonreshape.i18n.t`, and argparse's own phrases ("usage:", "error:", "options", "the
   following arguments are required", "invalid choice", ...) are translated by routing
   argparse's gettext hooks (``argparse._`` / ``argparse.ngettext``) through
   :data:`ARGPARSE_MESSAGES` while the parser is built and used (:func:`localized_argparse`).
   Usage errors exit with 1 (argparse would use 2, which is our "JSON error" code).
4. ``JSONRESHAPE_BACKEND`` is set from ``--backend``; only then are the modules that load
   :mod:`jsonreshape.core` imported (every such import is inside a function), so
   ``--backend`` really selects the core. When the core was already loaded by the calling
   process with another backend (in-process use, tests), a warning says so: the backend of
   a running process cannot change.
5. ``-v`` / ``-vv`` add console logging (INFO / DEBUG) on stderr; the uncaught-exception
   hooks are installed; :func:`jsonreshape.env.check_environment` runs once (its warnings,
   e.g. a missing ``yajl2_c`` ijson backend, go to the log and, with ``-v``, to stderr).
6. SIGINT (Ctrl+C) and, on Windows, SIGBREAK (Ctrl+Break) set the CancelToken shared by every
   operation: the running operation stops at its next check (``errors.Cancelled``), nothing
   partial is left behind (indexes, record sets and exports are written atomically) and
   the exit code is 3. A second Ctrl+C raises ``KeyboardInterrupt`` at once (still exit 3).

Output
======
* Results go to stdout, status lines (summaries, warnings, errors) to stderr. ``-q``
  suppresses the progress line and the summaries; warnings and errors are always shown.
* Progress: only when stderr is a terminal and ``-q`` is not given -- one line refreshed in
  place with the stage, percent, done / total bytes, rate and remaining time, cleared when
  the stage ends. The stage label is the operation's own ``Progress.info().message`` when it
  sets one (the progress of a stage starts with the CLI's label -- preparing, indexing,
  filtering, sorting, writing, searching, schema -- as its message, so an operation that
  announces a phase replaces it: "merging the sorted parts" of a sort, "finding the
  columns" / "exporting" / "finishing" of the writer, waiting for another process's index);
  a trailing ``…`` or ``:`` of that message is dropped, and a label too long for the
  terminal is shortened so that the numbers stay visible.
* Numbers in summaries are grouped with spaces in Uzbek (``12 345``) and commas in English.
  English texts with a count use their singular form for 1 (``1 hit in 1 record``,
  ``1 / 1 record -> ...``, ``(1 byte)``): the key ``<key>.one`` is used when it exists
  (:func:`_tn`; argparse's own plural phrases likewise); Uzbek keeps the noun after a
  number in the singular anyway (``3 ta yozuv``).
* Document warnings (``doc.info.warnings``): those known when the work starts are printed
  then (``inspect``: in the report); those the document gains while the command runs (e.g.
  a cache-folder fallback during indexing) and the :data:`END_WARNINGS` -- data appended to
  the input while it was read, of which only the snapshot taken at open is used: never
  shown earlier, as its size keeps changing -- are printed once after the command's own
  output (also after an error), with their latest values. A warning is identified by its
  key and ``path`` (added again, e.g. with a newer byte count, it appears once) and is
  never repeated; one the document had at open is not printed at the end when the command
  failed before showing it. Then the memory check (see "Decisions").
* ``search`` prints hits while they are found (``on_hits``), at most ``--limit``:
  ``#record  path  [kalit|qiymat]  snippet`` with the matched parts in bold yellow when
  stdout is a terminal and ``NO_COLOR`` is not set, otherwise between ``«`` and ``»``;
  control characters in keys and texts are shown escaped (``\\n``, ``\\u0007``), so one hit
  is always one line.
* Broken JSON Lines lines are described in line-oriented words: the keys
  ``json.line_error.<code>`` / ``json.line_expected.<code>`` replace ``json.error.<code>`` /
  ``json.expected.<code>`` where those speak of the end of the *file* (``unexpected_eof``:
  "the line ended unexpectedly"; ``end``: "end of the line was expected").

Decisions (documented deviations / interpretations)
====================================================
* ``--max-hits N`` caps the hits the search collects for listing (default 100000) and
  ``--limit N`` the hits printed (default 50; 0 = none): at most ``min(limit, max_hits)``
  hits are printed. Counting always covers every hit. ``--export`` writes every hit
  (``search.iter_hits`` + ``search.export_hits``, bounded memory) in the same single pass
  that prints the first ``--limit`` hits; its "mode" in the summary is derived the same way
  as :func:`jsonreshape.search.search` decides it (byte prefilter possible for the query and
  the file's string flags). The "only the first N hits are shown" note advises ``--limit``
  (or ``--max-hits`` when that was the bound) and ``--export`` only when no export was given.
* ``search`` without ``--export``/``--count`` does not build the index first (AGENT_PROMPT
  §6.2/§12 "results appear at once"): :func:`jsonreshape.search.search` starts the index in
  the background and follows it, so the first hits are printed while a fresh multi-GB file
  is still being indexed. Warnings about the index (broken lines, a JSON error with
  ``--allow-partial``) and a JSON error without ``--allow-partial`` (exit 2) are reported
  when it is complete, after the hits of the records before it. With ``--export`` or
  ``--count`` (and when a complete cached index exists) the index comes first, as for every
  other command.
* ``--count`` prints one line with the counts to stdout (plain digits, for scripts).
* ``inspect`` of a file with a JSON error prints the whole report (including the error with
  context) and exits with 2 unless ``--allow-partial`` is given; the schema table needs
  ``--allow-partial`` then (it describes the records before the error). For a wrapped
  document the report shows the outer wrapper (AGENT_PROMPT §3.4 "Tashqi o'rama alohida
  ko'rsatiladi"): its members with short compact-JSON values, the record container as a
  placeholder; ``--json`` adds a ``wrapper`` object (``bytes_before``, ``bytes_after``,
  ``members``: ``key``, ``records``, ``json``, ``truncated``; ``members`` is ``null`` when a
  part is larger than :data:`WRAPPER_READ_MAX` or cannot be parsed).
* A record path that matches nothing (``target_not_found``) or a scalar
  (``target_not_container``) is not a JSON error of the file: exit 1 with the record arrays
  found in the head (``doc.info.candidates``; the closest one as the example), also with
  ``--allow-partial``.
* A record path only works inside a single JSON document. With ``--format auto`` and
  ``--record-path`` the input is first detected without the record path
  (:func:`jsonreshape.reader.detect_format`: the head only; a gzip/UTF-16 input is prepared
  once, Document.open reuses that copy); JSON Lines or several JSON values one after
  another (``jsonl`` / ``stream``) are refused before any work -- exit 1 with
  ``cli.error.record_path_values`` and its hint (remove ``--record-path``, or force
  ``--format wrapped`` for a single document with a misleading name), as a forced
  ``--format jsonl --record-path`` is. Several values found only while reading (a first
  value larger than the detection head) remain a JSON error (exit 2) with the hint to
  remove ``--record-path``.
* A syntax error in ``--record-path`` gets record-path examples as its hint
  (``cli.hint.record_path_syntax``: ``data``, ``result.items``, ``/data/0``, ``data[0]``)
  instead of the general path examples, whose ``#120`` is a record number (a Ctrl+G
  location), not a path; any other :class:`~jsonreshape.errors.PathSyntaxError` that reaches
  the command line (``--path``, ``--by`` / ``--sort-by``) gets the general examples without
  it (``cli.hint.path_syntax``).
* Memory (AGENT_PROMPT §11 "xotira chegarasi"): at the end of every command the peak
  memory of the process (Windows: the peak working set, via psutil; elsewhere the maximum
  resident set size) is compared with ``Settings.memory_limit_bytes``. Above it, one
  warning gives both numbers and what to lower (``--memory-limit`` for a sort, the "Large
  node threshold" setting) or to raise; the command is not aborted. Only a peak reached
  during the command counts: the process's peak must have grown since :func:`main` started
  (an in-process caller's earlier peak is not this command's).
* File changes (AGENT_PROMPT §1.7): ``doc.ensure_unchanged()`` runs after indexing, before
  the inspect report, after a search (inside the hit stream of ``--export``, so the export
  is never committed) and before an export starts (the writer checks again right before its
  atomic rename). Any other error raised while the input is open becomes the
  :class:`~jsonreshape.errors.FileChangedError` when the file changed meanwhile (the real
  cause, not the garbled data it produced). Exit code 2 with ``error.file_changed`` and its
  hint.
* ``--memory-limit SIZE`` (sort keys in memory): a number without a unit means megabytes
  (``512`` = 512 MB; ``parse_size`` units are binary); below :data:`MIN_MEMORY_LIMIT` it is
  raised to that minimum with a warning (a tiny limit wrote one temporary run file per few
  keys). The sort memory never exceeds half of ``Settings.memory_limit_bytes`` (the overall
  memory budget, "Xotira chegarasi"): a larger ``--memory-limit`` is lowered with a warning;
  without the option ``min(Settings.sort_memory_bytes, memory_limit_bytes // 2)`` is passed.
* ``--sort-keys [alpha|codepoint]``: ``alpha`` (the default when the flag is given) sorts the
  keys alphabetically (Uzbek collation, natural, case-insensitive), ``codepoint`` by code
  point; ``--sort-keys`` followed by any other argument that is not an option (e.g. INPUT)
  means ``alpha`` and does not swallow that argument.
* Filter presets (AGENT_PROMPT §6.3 "Filtrlar nom bilan saqlanadi"): ``--save-preset NAME``
  stores the filter of ``filter``/``convert`` (after it compiled, before the work starts),
  ``--preset NAME`` uses one, ``presets`` lists them and ``presets --delete NAME`` deletes
  one (:class:`jsonreshape.query.FilterPresets`).
* OS errors: common ``winerror``/``errno`` values (2, 3: not found; 5: access denied, or the
  read-only attribute; 32, 33: in use by another program; 39, 112: disk full; 80, 183:
  already exists) get a localized message and hint -- the ``fileio.error.*`` texts of
  :func:`jsonreshape.fileio.os_error` for "in use", "read-only" and "disk full", ``cli.error.os.*``
  for the others; the raw OS text (often in the Windows display language) is only a
  detail line. An existing read-only OUTPUT is refused before any work
  (``fileio.error.read_only``).
* ``--by -id``: argparse would read ``-id`` as an option, so a value starting with a single
  ``-`` right after ``--by`` / ``--sort-by`` is joined to it (``--by=-id``) before parsing.
* ``convert`` also accepts the sort options of ``sort`` for its ``--sort-by`` keys.
* Output options that do not apply to the chosen format (``--columns`` for JSON, ``--indent``
  for CSV, ...) are ignored with a warning -- except ``--as-keyed`` (rebuild one keyed object
  from ``{"key": value}`` records, ``writer.ExportOptions.as_keyed``): it changes what the
  result is, so with a non-JSON output it is a user error (exit 1, ``cli.error.as_keyed_format``).
  A record that is not a one-member object is a user error too (exit 1,
  ``writer.error.not_single_member``; nothing is saved).
* Broken pipes on stdout (``jsonreshape search ... | head``) end the command quietly (exit 0).
* The examples of ``--help`` work unchanged in cmd.exe, Windows PowerShell 5.1, PowerShell 7
  and POSIX shells: expressions in double quotes, texts inside them in single quotes, no
  backtick inside double quotes (PowerShell's escape character), no double quote inside a
  single-quoted argument (Windows PowerShell 5.1 drops it).
"""

from __future__ import annotations

import argparse
import contextlib
import difflib
import errno
import inspect
import json
import logging
import os
import platform
import re
import shutil
import signal
import stat
import struct
import sys
import threading
import time
import traceback
from typing import Any, Callable, Iterable, Iterator, Sequence

from . import __version__
from .errors import (
    Cancelled,
    FileChangedError,
    FileFormatError,
    JsonReshapeError,
    JsonSyntaxError,
    PathSyntaxError,
    QuerySyntaxError,
    TooLargeError,
    UserInputError,
)
from .i18n import SUPPORTED_LANGUAGES, get_language, has, set_language, t
from .tasks import CancelToken, Progress, ProgressInfo, format_bytes, format_duration, parse_size

log = logging.getLogger(__name__)

__all__ = [
    "ARGPARSE_MESSAGES",
    "EXIT_CANCELLED",
    "EXIT_INTERNAL",
    "EXIT_JSON",
    "EXIT_OK",
    "EXIT_USER",
    "build_parser",
    "localized_argparse",
    "main",
]

EXIT_OK = 0
EXIT_USER = 1
EXIT_JSON = 2
EXIT_CANCELLED = 3
EXIT_INTERNAL = 4

PROG = "jsonreshape"
BACKENDS = ("auto", "rust", "python")
INPUT_FORMATS = ("auto", "array", "jsonl", "brace_list", "wrapped", "keyed", "single", "stream")  # reader.KINDS
OUTPUT_FORMATS = ("json", "jsonl", "csv", "xlsx", "sqlite")                                       # writer.FORMATS
HIT_EXPORT_FORMATS = ("csv", "json")                                                           # search.EXPORT_FORMATS
SORT_KEY_ORDERS = ("alpha", "codepoint")                                       # writer.ExportOptions.sort_keys
DEFAULT_LIMIT = 50
DEFAULT_MAX_HITS = 100000
BROKEN_SHOWN = 5          # broken JSON Lines lines listed in warnings and in the inspect report
BROKEN_JSON_MAX = 100     # broken lines listed by inspect --json
CANDIDATES_SHOWN = 5      # record-path candidates listed in the inspect text report (--json: all)
DASH_VALUE_OPTIONS = ("--by", "--sort-by")
MiB = 1 << 20
MIN_MEMORY_LIMIT = 16 * MiB   # smallest sort memory (--memory-limit below it is raised to it)
WRAPPER_READ_MAX = MiB        # each part of a wrapper (before/after the records) read by inspect
WRAPPER_TEXT_CHARS = 100      # characters of a wrapper value in the inspect text report
WRAPPER_JSON_CHARS = 2000     # characters of a wrapper value in inspect --json
WRAPPER_MEMBERS_SHOWN = 10    # wrapper members listed in the inspect text report (--json: all)
MIN_PROGRESS_LABEL = 12       # a progress label is shortened to fit the terminal, not below this
#: JSON error codes of a record path that does not lead to an array/object (core constants)
RECORD_PATH_CODES = ("target_not_found", "target_not_container")
#: attribute of a JsonReshapeError carrying a hint the CLI chose instead of the key's own
CLI_HINT_ATTR = "cli_hint"
#: document warnings shown only at the end of a command, with their latest values: notes
#: about this run that keep changing while it runs (reader.APPENDED_WARNING: the input grew
#: after it was opened -- only the snapshot taken at open is used)
END_WARNINGS = ("reader.warn.appended",)

HIGHLIGHT_ON = "\x1b[1;33m"     # bold yellow
HIGHLIGHT_OFF = "\x1b[0m"
MARK_OPEN = chr(0xAB)           # «
MARK_CLOSE = chr(0xBB)          # »
ELLIPSIS = "\N{HORIZONTAL ELLIPSIS}"

# Characters shown escaped in one-line output (controls, line/paragraph separators, bidi
# controls, BOM, lone surrogates).
_CONTROL_RANGES = ((0x00, 0x1F), (0x7F, 0x9F), (0x200E, 0x200F), (0x2028, 0x202E), (0x2066, 0x2069),
                   (0xFEFF, 0xFEFF), (0xD800, 0xDFFF))
_CONTROL_RE = re.compile("[" + "".join(re.escape(chr(a)) + ("-" + re.escape(chr(b)) if b != a else "")
                                       for a, b in _CONTROL_RANGES) + "]")
_ESCAPE_NAMES = {"\n": "\\n", "\r": "\\r", "\t": "\\t"}
_PLAIN_NUMBER_RE = re.compile(r"[0-9]+(?:\.[0-9]+)?")


# ==================================================================================
# argparse localization
# ==================================================================================
#: argparse's own message ids (gettext msgids) -> i18n keys of the "cli" section.
ARGPARSE_MESSAGES: dict[str, str] = {
    "usage: ": "cli.argparse.usage",
    "%(heading)s:": "cli.argparse.heading",
    " (default: %(default)s)": "cli.argparse.default",
    "argument %(argument_name)s: %(message)s": "cli.argparse.argument",
    "show program's version number and exit": "cli.argparse.version",
    "unknown parser %(parser_name)r (choices: %(choices)s)": "cli.argparse.unknown_parser",
    "command '%(parser_name)s' is deprecated": "cli.argparse.deprecated_command",
    "can't open '%(filename)s': %(error)s": "cli.argparse.cant_open",
    "positional arguments": "cli.argparse.positionals",
    "options": "cli.argparse.options",
    "optional arguments": "cli.argparse.options",
    "show this help message and exit": "cli.argparse.help",
    "subcommands": "cli.argparse.subcommands",
    "unrecognized arguments: %s": "cli.argparse.unrecognized",
    "not allowed with argument %s": "cli.argparse.not_allowed",
    "ambiguous option: %(option)s could match %(matches)s": "cli.argparse.ambiguous",
    "ignored explicit argument %r": "cli.argparse.ignored_explicit",
    "option '%(option)s' is deprecated": "cli.argparse.deprecated_option",
    "argument '%(argument_name)s' is deprecated": "cli.argparse.deprecated_argument",
    "the following arguments are required: %s": "cli.argparse.required",
    "one of the arguments %s is required": "cli.argparse.one_required",
    "expected one argument": "cli.argparse.expected_one",
    "expected at most one argument": "cli.argparse.expected_at_most_one",
    "expected at least one argument": "cli.argparse.expected_at_least_one",
    "expected %s argument": "cli.argparse.expected_n",
    "expected %s arguments": "cli.argparse.expected_n",
    "unexpected option string: %s": "cli.argparse.unexpected_option",
    "invalid %(type)s value: %(value)r": "cli.argparse.invalid_value",
    "invalid choice: %(value)r (choose from %(choices)s)": "cli.argparse.invalid_choice",
    "invalid choice: %(value)r, maybe you meant %(closest)r? (choose from %(choices)s)":
        "cli.argparse.invalid_choice_suggest",
    "%(prog)s: error: %(message)s\n": "cli.argparse.error",
    "%(prog)s: warning: %(message)s\n": "cli.argparse.warning",
}


def _argparse_gettext(message: str) -> str:
    key = ARGPARSE_MESSAGES.get(message)
    return t(key) if key is not None else message


def _argparse_ngettext(singular: str, plural: str, n: int) -> str:
    key = ARGPARSE_MESSAGES.get(singular)
    if key is not None:
        return _tn(key, n)
    return singular if n == 1 else plural


@contextlib.contextmanager
def localized_argparse() -> Iterator[None]:
    """Route argparse's gettext hooks through :func:`t` while the block runs (building the
    parser, parsing, formatting help/usage), so its own phrases appear in the current
    language. The previous hooks are restored afterwards."""
    saved = {name: getattr(argparse, name) for name in ("_", "ngettext") if hasattr(argparse, name)}
    if "_" in saved:
        argparse._ = _argparse_gettext  # type: ignore[attr-defined]
    if "ngettext" in saved:
        argparse.ngettext = _argparse_ngettext  # type: ignore[attr-defined]
    try:
        yield
    finally:
        for name, value in saved.items():
            setattr(argparse, name, value)


class _UsageError(Exception):
    """A usage error found by argparse (reported with the usage line, exit code 1)."""

    def __init__(self, parser: argparse.ArgumentParser, message: str) -> None:
        super().__init__(message)
        self.parser = parser
        self.message = message


class _ParserExit(Exception):
    """argparse wanted to exit (``--help``): the status to return."""

    def __init__(self, status: int) -> None:
        super().__init__(status)
        self.status = status


class _ArgumentParser(argparse.ArgumentParser):
    """ArgumentParser that never calls ``sys.exit`` (``main()`` returns exit codes).
    ``commands`` maps command names to their parsers (main parser only)."""

    commands: dict[str, argparse.ArgumentParser]

    def error(self, message: str):  # type: ignore[override]
        raise _UsageError(self, message)

    def exit(self, status: int = 0, message: str | None = None):  # type: ignore[override]
        if message:
            self._print_message(message, sys.stderr)
        raise _ParserExit(status)


def _parser_extra() -> dict[str, Any]:
    """Keyword arguments supported by this Python's argparse (3.14: plain help without
    colours -- our own output decides about colours -- and "maybe you meant" hints)."""
    extra: dict[str, Any] = {}
    try:
        params = inspect.signature(argparse.ArgumentParser.__init__).parameters
    except (TypeError, ValueError):  # pragma: no cover
        return extra
    if "color" in params:
        extra["color"] = False
    if "suggest_on_error" in params:
        extra["suggest_on_error"] = True
    return extra


_PARSER_EXTRA = _parser_extra()


def _formatter(prog: str) -> argparse.HelpFormatter:
    try:
        columns = shutil.get_terminal_size((100, 24)).columns
    except (OSError, ValueError):  # pragma: no cover
        columns = 100
    width = max(60, min(120, columns) - 2)
    return argparse.RawDescriptionHelpFormatter(prog, max_help_position=30, width=width)


# -- argument value types -------------------------------------------------------------
def _int_value(text: str) -> int | None:
    s = text.strip()
    if s.isascii() and (s.isdigit() or (s[:1] in "+-" and s[1:].isdigit())):
        return int(s)
    return None


def _positive_int(text: str) -> int:
    n = _int_value(text)
    if n is None or n < 1:
        raise argparse.ArgumentTypeError(t("cli.argerr.positive_int", value=text))
    return n


def _non_negative_int(text: str) -> int:
    n = _int_value(text)
    if n is None or n < 0:
        raise argparse.ArgumentTypeError(t("cli.argerr.non_negative_int", value=text))
    return n


def _indent_value(text: str) -> int | str:
    s = text.strip().lower()
    if text == "\t" or s in ("tab", "\\t"):
        return "\t"
    n = _int_value(s)
    if n is None or not 0 <= n <= 16:
        raise argparse.ArgumentTypeError(t("cli.argerr.indent", value=text))
    return n


def _memory_size_value(text: str) -> int:
    """``--memory-limit``: a size in bytes. ``256MB``, ``1GB``, ``65536B`` as
    :func:`~jsonreshape.tasks.parse_size` reads them (binary units); a number without a
    unit means megabytes (``512`` = 512 MB) -- nobody means 512 bytes of sort memory."""
    s = text.strip()
    try:
        if _PLAIN_NUMBER_RE.fullmatch(s):
            n = int(float(s) * MiB)
        else:
            n = parse_size(s)
    except (ValueError, OverflowError):
        n = 0
    if n < 1:
        raise argparse.ArgumentTypeError(t("cli.argerr.size", value=text))
    return n


def _delimiter_value(text: str) -> str:
    if text.strip().lower() in ("tab", "\\t"):
        return "\t"
    return text


# -- parser -------------------------------------------------------------------------------
def _add_global_options(parser: argparse.ArgumentParser, *, repeated: bool) -> None:
    """``--lang --backend -q -v``: on the main parser with real defaults; repeated on every
    command with ``SUPPRESS`` defaults (a value given after the command overrides the one
    given before it, an absent one leaves it alone)."""
    if repeated:
        target: Any = parser.add_argument_group(t("cli.group.global"))
        none, false, zero = argparse.SUPPRESS, argparse.SUPPRESS, argparse.SUPPRESS
    else:
        target = parser
        none, false, zero = None, False, 0
    target.add_argument("--lang", choices=SUPPORTED_LANGUAGES, default=none, help=t("cli.help.lang"))
    target.add_argument("--backend", choices=BACKENDS, default=none, help=t("cli.help.backend"))
    target.add_argument("-q", "--quiet", action="store_true", default=false, help=t("cli.help.quiet"))
    target.add_argument("-v", "--verbose", action="count", default=zero, help=t("cli.help.verbose"))


def _add_input_options(parser: argparse.ArgumentParser) -> None:
    g = parser.add_argument_group(t("cli.group.input"))
    g.add_argument("--record-path", metavar="P", default=None, help=t("cli.help.record_path"))
    g.add_argument("--format", choices=INPUT_FORMATS, default="auto", help=t("cli.help.format"))
    g.add_argument("--allow-partial", action="store_true", help=t("cli.help.allow_partial"))
    g.add_argument("--no-cache", action="store_true", help=t("cli.help.no_cache"))


def _add_output_options(parser: argparse.ArgumentParser) -> None:
    g = parser.add_argument_group(t("cli.group.output"))
    g.add_argument("--to", choices=OUTPUT_FORMATS, default=None, help=t("cli.help.to"))
    g.add_argument("--jsonl", action="store_true", help=t("cli.help.jsonl"))
    mx = g.add_mutually_exclusive_group()
    mx.add_argument("--compact", action="store_true", help=t("cli.help.compact"))
    mx.add_argument("--indent", metavar="N", type=_indent_value, default=None, help=t("cli.help.indent"))
    g.add_argument("--sort-keys", nargs="?", const="alpha", choices=SORT_KEY_ORDERS, default=None,
                   metavar="alpha|codepoint", help=t("cli.help.sort_keys"))
    g.add_argument("--no-wrapper", action="store_true", help=t("cli.help.no_wrapper"))
    g.add_argument("--as-keyed", action="store_true", help=t("cli.help.as_keyed"))
    g.add_argument("--columns", metavar="LIST", default=None, help=t("cli.help.columns"))
    g.add_argument("--array-mode", choices=("join", "rows"), default=None, help=t("cli.help.array_mode"))
    g.add_argument("--explode", metavar="PATH", default=None, help=t("cli.help.explode"))
    g.add_argument("--sep", metavar="TEXT", default=None, help=t("cli.help.sep"))
    g.add_argument("--delimiter", metavar="CHAR", type=_delimiter_value, default=None, help=t("cli.help.delimiter"))
    g.add_argument("--no-bom", action="store_true", help=t("cli.help.no_bom"))
    g.add_argument("--xlsx-split", choices=("sheets", "files"), default=None, help=t("cli.help.xlsx_split"))


def _add_filter_options(parser: argparse.ArgumentParser, *, required: bool) -> None:
    g = parser.add_argument_group(t("cli.group.filter"))
    mx = g.add_mutually_exclusive_group(required=required)
    mx.add_argument("--where", metavar="EXPR", default=None, help=t("cli.help.where"))
    mx.add_argument("--jmespath", metavar="EXPR", default=None, help=t("cli.help.jmespath"))
    mx.add_argument("--preset", metavar="NAME", default=None, help=t("cli.help.preset"))
    g.add_argument("--save-preset", metavar="NAME", default=None, help=t("cli.help.save_preset"))


def _add_sort_options(parser: argparse.ArgumentParser, flag: str, *, required: bool) -> None:
    g = parser.add_argument_group(t("cli.group.sort"))
    g.add_argument(flag, dest="sort_fields", action="append", metavar="FIELD", required=required, default=None,
                   help=t("cli.help.by") if flag == "--by" else t("cli.help.sort_by"))
    g.add_argument("--nulls", choices=("first", "last"), default="last", help=t("cli.help.nulls"))
    g.add_argument("--collation", choices=("uz", "simple", "unicode"), default="uz", help=t("cli.help.collation"))
    g.add_argument("--no-natural", action="store_true", help=t("cli.help.no_natural"))
    g.add_argument("--case-sensitive", action="store_true", help=t("cli.help.sort_case_sensitive"))
    g.add_argument("--memory-limit", metavar="SIZE", type=_memory_size_value, default=None,
                   help=t("cli.help.memory_limit"))


def _add_command(sub: Any, name: str, short_help: str, description: str, epilog: str) -> argparse.ArgumentParser:
    return sub.add_parser(name, help=short_help, description=description, epilog=epilog, formatter_class=_formatter,
                          allow_abbrev=False, **_PARSER_EXTRA)


def build_parser() -> _ArgumentParser:
    """The localized argument parser (texts in the current language). Build and use it
    inside :func:`localized_argparse` so argparse's own phrases are translated as well."""
    parser = _ArgumentParser(prog=PROG, description=t("cli.help.description"), epilog=t("cli.help.epilog"),
                             formatter_class=_formatter, allow_abbrev=False, **_PARSER_EXTRA)
    parser.add_argument("--version", action="store_true", help=t("cli.help.version"))
    _add_global_options(parser, repeated=False)
    sub = parser.add_subparsers(dest="command", metavar="COMMAND", title=t("cli.help.commands"))

    p = _add_command(sub, "inspect", t("cli.cmd.inspect.help"), t("cli.cmd.inspect.description"),
                     t("cli.cmd.inspect.epilog"))
    p.add_argument("input", metavar="INPUT", help=t("cli.help.input"))
    g = p.add_argument_group(t("cli.group.inspect"))
    g.add_argument("--sample", metavar="N", type=_positive_int, default=None, help=t("cli.help.sample"))
    g.add_argument("--json", action="store_true", help=t("cli.help.json"))
    _add_input_options(p)
    _add_global_options(p, repeated=True)

    p = _add_command(sub, "search", t("cli.cmd.search.help"), t("cli.cmd.search.description"),
                     t("cli.cmd.search.epilog"))
    p.add_argument("input", metavar="INPUT", help=t("cli.help.input"))
    p.add_argument("text", metavar="TEXT", help=t("cli.help.text"))
    g = p.add_argument_group(t("cli.group.search"))
    mx = g.add_mutually_exclusive_group()
    mx.add_argument("--keys", action="store_true", help=t("cli.help.keys"))
    mx.add_argument("--values", action="store_true", help=t("cli.help.values"))
    mx = g.add_mutually_exclusive_group()
    mx.add_argument("--regex", action="store_true", help=t("cli.help.regex"))
    mx.add_argument("--number", action="store_true", help=t("cli.help.number"))
    g.add_argument("--case-sensitive", action="store_true", help=t("cli.help.case_sensitive"))
    g.add_argument("--whole-word", action="store_true", help=t("cli.help.whole_word"))
    g.add_argument("--translit", action="store_true", help=t("cli.help.translit"))
    g.add_argument("--path", metavar="P", default=None, help=t("cli.help.path"))
    g.add_argument("--limit", metavar="N", type=_non_negative_int, default=DEFAULT_LIMIT, help=t("cli.help.limit"))
    g.add_argument("--count", action="store_true", help=t("cli.help.count"))
    g.add_argument("--export", metavar="FILE", default=None, help=t("cli.help.export"))
    g.add_argument("--max-hits", metavar="N", type=_non_negative_int, default=DEFAULT_MAX_HITS,
                   help=t("cli.help.max_hits"))
    _add_input_options(p)
    _add_global_options(p, repeated=True)

    p = _add_command(sub, "filter", t("cli.cmd.filter.help"), t("cli.cmd.filter.description"),
                     t("cli.cmd.filter.epilog"))
    p.add_argument("input", metavar="INPUT", help=t("cli.help.input"))
    p.add_argument("output", metavar="OUTPUT", help=t("cli.help.output"))
    _add_filter_options(p, required=True)
    _add_output_options(p)
    _add_input_options(p)
    _add_global_options(p, repeated=True)

    p = _add_command(sub, "sort", t("cli.cmd.sort.help"), t("cli.cmd.sort.description"), t("cli.cmd.sort.epilog"))
    p.add_argument("input", metavar="INPUT", help=t("cli.help.input"))
    p.add_argument("output", metavar="OUTPUT", help=t("cli.help.output"))
    _add_sort_options(p, "--by", required=True)
    _add_output_options(p)
    _add_input_options(p)
    _add_global_options(p, repeated=True)

    p = _add_command(sub, "convert", t("cli.cmd.convert.help"), t("cli.cmd.convert.description"),
                     t("cli.cmd.convert.epilog"))
    p.add_argument("input", metavar="INPUT", help=t("cli.help.input"))
    p.add_argument("output", metavar="OUTPUT", help=t("cli.help.output"))
    _add_filter_options(p, required=False)
    _add_sort_options(p, "--sort-by", required=False)
    _add_output_options(p)
    _add_input_options(p)
    _add_global_options(p, repeated=True)

    p = _add_command(sub, "presets", t("cli.cmd.presets.help"), t("cli.cmd.presets.description"),
                     t("cli.cmd.presets.epilog"))
    g = p.add_argument_group(t("cli.group.presets"))
    g.add_argument("--delete", metavar="NAME", default=None, help=t("cli.help.delete_preset"))
    g.add_argument("--json", action="store_true", help=t("cli.help.presets_json"))
    _add_global_options(p, repeated=True)
    parser.commands = dict(sub.choices)
    return parser


def _parse(parser: _ArgumentParser, argv: Sequence[str]) -> argparse.Namespace:
    """Parse; arguments nobody recognised are reported by the command's own parser (its
    usage line is the useful one)."""
    args, extras = parser.parse_known_args(argv)
    if extras:
        target = parser.commands.get(args.command or "", parser)
        target.error(t("cli.argparse.unrecognized") % " ".join(extras))
    if args.command is None and not args.version:
        parser.error(t("cli.argerr.no_command"))
    return args


def _scan_option(argv: Sequence[str], name: str) -> str | None:
    """Value of ``name`` (``--lang x`` / ``--lang=x``) in raw arguments (last one wins)."""
    value = None
    for i, tok in enumerate(argv):
        if tok == "--":
            break
        if tok == name and i + 1 < len(argv):
            value = argv[i + 1]
        elif tok.startswith(name + "="):
            value = tok[len(name) + 1:]
    return value


def _join_dash_values(argv: Sequence[str]) -> list[str]:
    """``--by -id`` -> ``--by=-id`` (argparse reads a lone ``-id`` as an unknown option);
    ``--sort-keys`` followed by an argument that is neither ``alpha``/``codepoint`` nor an
    option -> ``--sort-keys=alpha`` (its optional value must never swallow a positional
    argument: ``--sort-keys in.json out.json``)."""
    out: list[str] = []
    i = 0
    n = len(argv)
    while i < n:
        tok = argv[i]
        if tok == "--":
            out.extend(argv[i:])
            break
        if tok in DASH_VALUE_OPTIONS and i + 1 < n:
            nxt = argv[i + 1]
            if nxt.startswith("-") and len(nxt) > 1 and not nxt.startswith("--"):
                out.append(f"{tok}={nxt}")
                i += 2
                continue
        if tok == "--sort-keys" and i + 1 < n and argv[i + 1] not in SORT_KEY_ORDERS \
                and not argv[i + 1].startswith("-"):
            out.append("--sort-keys=alpha")
            i += 1
            continue
        out.append(tok)
        i += 1
    return out


def _choose_language(cli_value: str | None) -> str:
    """``--lang`` > ``JSONRESHAPE_LANG`` > ``settings.language`` > ``uz``."""
    for candidate in (cli_value, os.environ.get("JSONRESHAPE_LANG")):
        if candidate and candidate.strip().lower() in SUPPORTED_LANGUAGES:
            return candidate.strip().lower()
    try:
        from .settings import get_settings

        language = get_settings().language
        if language in SUPPORTED_LANGUAGES:
            return language
    except Exception:  # pragma: no cover - settings never raise, but the language must not fail
        log.warning("could not read the language setting", exc_info=True)
    return "uz"


# ==================================================================================
# localized texts with counts
# ==================================================================================
def _tn(key: str, count: Any, /, **params: Any) -> str:
    """:func:`t` with the singular variant ``<key>.one`` when ``count`` is 1 and that key
    exists (English "1 record" / "2 records"; the Uzbek texts are the same in both)."""
    try:
        one = int(count) == 1
    except (TypeError, ValueError):
        one = False
    if one and has(key + ".one"):
        return t(key + ".one", **params)
    return t(key, **params)


def _hits_text(n: int, shown: str) -> str:
    return _tn("cli.count.hits", n, count=shown)


def _records_text(n: int, shown: str) -> str:
    return _tn("cli.count.records", n, count=shown)


# ==================================================================================
# console output
# ==================================================================================
class _OutputClosed(Exception):
    """stdout was closed by the reader (broken pipe)."""


def _isatty(stream: Any) -> bool:
    try:
        return bool(stream is not None and stream.isatty())
    except (AttributeError, ValueError, OSError):
        return False


def _setup_streams() -> None:
    """UTF-8 for redirected stdout/stderr; never crash on unencodable characters."""
    for name in ("stdout", "stderr"):
        stream = getattr(sys, name, None)
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is None:
            continue
        try:
            if _isatty(stream):
                reconfigure(errors="backslashreplace")
            else:
                reconfigure(encoding="utf-8", errors="backslashreplace")
        except (AttributeError, ValueError, OSError, LookupError):  # pragma: no cover - exotic streams
            pass


def _enable_ansi() -> bool:
    """Make sure the console of stdout understands ANSI colours (Windows: VT mode)."""
    if sys.platform != "win32":
        return True
    try:
        import ctypes
        from ctypes import wintypes

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)  # private instance: own argtypes
        kernel32.GetStdHandle.argtypes = (wintypes.DWORD,)
        kernel32.GetStdHandle.restype = wintypes.HANDLE
        kernel32.GetConsoleMode.argtypes = (wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD))
        kernel32.GetConsoleMode.restype = wintypes.BOOL
        kernel32.SetConsoleMode.argtypes = (wintypes.HANDLE, wintypes.DWORD)
        kernel32.SetConsoleMode.restype = wintypes.BOOL
        handle = kernel32.GetStdHandle(wintypes.DWORD(-11 & 0xFFFFFFFF))  # STD_OUTPUT_HANDLE
        mode = wintypes.DWORD()
        if not kernel32.GetConsoleMode(handle, ctypes.byref(mode)):
            return False
        vt = 0x0004  # ENABLE_VIRTUAL_TERMINAL_PROCESSING
        if mode.value & vt:
            return True
        return bool(kernel32.SetConsoleMode(handle, mode.value | vt))
    except Exception:  # pragma: no cover - no console API
        return False


def _closed_pipe(exc: OSError) -> bool:
    # Windows reports a pipe closed by the reader as EINVAL as often as EPIPE
    return isinstance(exc, BrokenPipeError) or exc.errno in (errno.EPIPE, errno.EINVAL)


def _write(stream: Any, text: str, *, flush: bool = False) -> None:
    """Write (and flush) ``text``; a reader that went away raises :class:`_OutputClosed`."""
    if stream is None:
        return
    try:
        stream.write(text)
        if flush:
            stream.flush()
    except OSError as exc:
        if _closed_pipe(exc):
            raise _OutputClosed() from None
        raise
    except ValueError:  # the stream was closed
        raise _OutputClosed() from None


def _flush(stream: Any) -> None:
    """Best-effort flush (status output, end of run)."""
    try:
        if stream is not None:
            stream.flush()
    except (OSError, ValueError):
        pass


def _terminal_columns() -> int:
    for stream in (sys.stderr, sys.stdout):
        try:
            return max(20, os.get_terminal_size(stream.fileno()).columns)
        except (AttributeError, ValueError, OSError):
            continue
    return 80


def _num(n: int) -> str:
    """Grouped digits: ``12 345`` (Uzbek), ``12,345`` (English)."""
    text = f"{int(n):,}"
    return text.replace(",", " ") if get_language() == "uz" else text


def _mem_text(n: int) -> str:
    """A memory size in the binary units ``--memory-limit`` is read in (``16 MB`` = 16 MiB)."""
    for unit, factor in (("GB", 1 << 30), ("MB", MiB), ("KB", 1 << 10)):
        if n >= factor:
            value = n / factor
            return f"{value:.0f} {unit}" if value == int(value) or value >= 100 else f"{value:.1f} {unit}"
    return f"{int(n)} B"


def _display_text(s: str) -> str:
    """One-line display form: control/format characters escaped (``\\n``, ``\\u0007``)."""
    if s.isascii() and s.isprintable():
        return s
    return _CONTROL_RE.sub(_escape_match, s)


def _escape_match(m: re.Match[str]) -> str:
    ch = m.group()
    return _ESCAPE_NAMES.get(ch) or "\\" + "u" + f"{ord(ch):04x}"


def _same_width_text(s: str) -> str:
    """Control characters replaced one-for-one (keeps a caret line aligned)."""
    return _CONTROL_RE.sub(lambda m: " " if m.group() in "\t\n\r" else "?", s)


def _caret_lines(text: str, pos: int) -> list[str]:
    shown = _same_width_text(text)
    pos = max(0, min(pos, len(shown)))
    return [shown, " " * pos + "^"]


def _shorten(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[:max(0, limit - 1)] + ELLIPSIS


def _stage_label(message: str) -> str:
    """A progress message as the label of the progress line: one line, without a trailing
    ellipsis or colon (the line adds ``": "``)."""
    text = _display_text(message or "").strip()
    while text:
        stripped = text.rstrip(":" + ELLIPSIS).rstrip()
        if stripped.endswith("..."):
            stripped = stripped[:-3].rstrip()
        if stripped == text:
            break
        text = stripped
    return text


def _display_path(path: Any) -> str:
    """A path for messages: ``str`` without the ``\\\\?\\`` long-path prefix."""
    s = os.fsdecode(path) if isinstance(path, bytes) else str(path)
    if s.startswith("\\\\?\\UNC\\"):
        return "\\\\" + s[8:]
    if s.startswith("\\\\?\\"):
        return s[4:]
    return s


class _ProgressLine:
    """One progress line on stderr, refreshed in place (``\\r``), cleared at the end."""

    def __init__(self, enabled: bool) -> None:
        self.enabled = enabled
        self._lock = threading.RLock()
        self._label = ""
        self._shown = 0

    def stage(self, label: str) -> Progress | None:
        """A :class:`~jsonreshape.tasks.Progress` drawing this line (``None`` when progress
        is disabled). ``label`` is its initial message: operations keep a message they
        find, and replace it to announce a phase of their own (sort: merging), which then
        labels the line."""
        if not self.enabled:
            return None
        self.clear()
        with self._lock:
            self._label = label
        return Progress(callback=self._render, min_interval=0.1, message=label)

    def format(self, info: ProgressInfo, width: int | None = None) -> str:
        """The line for ``info``; longer than ``width``: the label is shortened first, so
        the numbers stay visible."""
        label = _stage_label(info.message) or self._label
        text = self._line(info, label)
        if width is not None and len(text) > width:
            room = width - (len(text) - len(label))
            if room >= MIN_PROGRESS_LABEL:
                text = self._line(info, _shorten(label, room))
        return text

    @staticmethod
    def _line(info: ProgressInfo, label: str) -> str:
        if info.total and info.fraction is not None:
            return t("cli.progress.line", stage=label, percent=f"{info.fraction * 100:.1f}%",
                     done=format_bytes(info.done), total=format_bytes(info.total), rate=format_bytes(info.rate),
                     eta=format_duration(info.eta))
        return t("cli.progress.line_unknown", stage=label, done=format_bytes(info.done), rate=format_bytes(info.rate))

    def _render(self, info: ProgressInfo) -> None:
        if info.finished:
            self.clear()
            return
        limit = _terminal_columns() - 1
        text = self.format(info, limit)
        if len(text) > limit:
            text = text[:max(0, limit - 1)] + ELLIPSIS
        with self._lock:
            pad = max(0, self._shown - len(text))
            try:
                _write(sys.stderr, "\r" + text + " " * pad)
                _flush(sys.stderr)
            except _OutputClosed:
                self.enabled = False
                return
            self._shown = len(text)

    def clear(self) -> None:
        with self._lock:
            if self._shown:
                try:
                    _write(sys.stderr, "\r" + " " * self._shown + "\r")
                    _flush(sys.stderr)
                except _OutputClosed:
                    self.enabled = False
                self._shown = 0


class _ClearProgressFilter(logging.Filter):
    """Clears the progress line before a console log record is written."""

    def __init__(self, line: _ProgressLine) -> None:
        super().__init__()
        self.line = line

    def filter(self, record: logging.LogRecord) -> bool:
        self.line.clear()
        return True


class _Console:
    """stdout/stderr writer of one command run (looked up at call time, so captured
    streams of tests work)."""

    def __init__(self, *, quiet: bool, verbose: int) -> None:
        self.quiet = quiet
        self.verbose = verbose
        self.progress = _ProgressLine(enabled=not quiet and _isatty(sys.stderr))
        self.color = _isatty(sys.stdout) and "NO_COLOR" not in os.environ and _enable_ansi()
        self._filtered: list[logging.Handler] = []
        self._filter = _ClearProgressFilter(self.progress)

    # -- plain output -----------------------------------------------------------------
    def out(self, text: str = "") -> None:
        self.progress.clear()
        _write(sys.stdout, text + "\n")

    def out_lines(self, lines: Iterable[str]) -> None:
        self.progress.clear()
        _write(sys.stdout, "".join(line + "\n" for line in lines), flush=True)

    def err(self, text: str) -> None:
        self.progress.clear()
        try:
            _write(sys.stderr, text + "\n")
            _flush(sys.stderr)
        except _OutputClosed:
            pass

    def info(self, text: str) -> None:
        if not self.quiet:
            self.err(text)

    def warn(self, text: str) -> None:
        self.err(t("cli.warning", message=text))

    def hint(self, text: str) -> None:
        self.err(t("cli.error.hint", hint=text))

    def error(self, exc: JsonReshapeError, *, hint: str | None = None) -> None:
        lines = [t("cli.error.prefix", message=exc.message)]
        lines.extend("  " + line for line in _detail_lines(exc))
        shown_hint = hint if hint is not None else _hint_of(exc)
        if shown_hint:
            lines.append(t("cli.error.hint", hint=shown_hint))
        self.err("\n".join(lines))

    # -- log records on the console never break the progress line ----------------------
    def attach_log_filter(self) -> None:
        if not self.progress.enabled:
            return
        for handler in logging.getLogger().handlers:
            if isinstance(handler, logging.StreamHandler) and not isinstance(handler, logging.FileHandler):
                handler.addFilter(self._filter)
                self._filtered.append(handler)

    def finish(self) -> None:
        self.progress.clear()
        for handler in self._filtered:
            handler.removeFilter(self._filter)
        self._filtered.clear()
        _flush(sys.stdout)
        _flush(sys.stderr)


# ==================================================================================
# errors shown by the CLI
# ==================================================================================
class _HintedError(UserInputError):
    """A user error whose hint the CLI computes (e.g. the record arrays found in the file)."""

    def __init__(self, key: str, /, *, hint: str | None = None, **params: Any) -> None:
        super().__init__(key, **params)
        self._hint = hint

    @property
    def hint(self) -> str | None:
        return self._hint if self._hint is not None else super().hint


class _OsError(UserInputError):
    """An :class:`OSError` with a localized message and hint; the raw OS text (often in the
    Windows display language) is shown as a detail line only."""

    def __init__(self, key: str, /, *, os_text: str = "", **params: Any) -> None:
        super().__init__(key, **params)
        self.os_text = os_text


def _hint_of(exc: JsonReshapeError) -> str | None:
    override = getattr(exc, CLI_HINT_ATTR, None)
    return override if override is not None else exc.hint


def _set_hint(exc: JsonReshapeError, hint: str) -> None:
    try:
        setattr(exc, CLI_HINT_ATTR, hint)
    except AttributeError:  # pragma: no cover - exceptions accept attributes
        pass


def _detail_lines(exc: JsonReshapeError) -> list[str]:
    """Caret lines of syntax errors (expression / path / JSON); the OS text of OS errors."""
    if isinstance(exc, QuerySyntaxError) and exc.text:
        return _caret_lines(exc.text, exc.pos)
    if isinstance(exc, JsonSyntaxError) and (exc.context_before or exc.context_after):
        before = _same_width_text(exc.context_before)
        after = _same_width_text(exc.context_after)
        return [before + after, " " * len(before) + "^"]
    if isinstance(exc, _OsError) and exc.os_text:
        return [t("cli.error.os_detail", detail=_display_text(exc.os_text))]
    return []


#: Windows error codes (``OSError.winerror``) of the common file-system failures
_WIN_NOT_FOUND = frozenset({2, 3, 53, 67, 161})   # file / path not found, bad network path / name, bad pathname
_WIN_ACCESS_DENIED = frozenset({5})
_WIN_IN_USE = frozenset({32, 33})                 # sharing violation, lock violation
_WIN_DISK_FULL = frozenset({39, 112})             # handle disk full, disk full
_WIN_EXISTS = frozenset({80, 183})                # file exists, already exists


def _read_only_file(path: Any) -> bool:
    """``path`` is an existing file with the read-only attribute (Windows) / not writable."""
    from .fileio import long_path

    try:
        lp = long_path(path)
        st = os.stat(lp)
    except (OSError, ValueError):
        return False
    if not stat.S_ISREG(st.st_mode):
        return False
    attributes = getattr(st, "st_file_attributes", None)
    if attributes is not None:
        return bool(attributes & getattr(stat, "FILE_ATTRIBUTE_READONLY", 1))
    try:
        return not os.access(lp, os.W_OK)
    except (OSError, ValueError):  # pragma: no cover
        return False


def _os_error(exc: OSError) -> JsonReshapeError:
    """A localized error for an :class:`OSError` that reached the command line (the
    messages of :func:`jsonreshape.fileio.os_error` where it has them: file in use,
    read-only file, disk full)."""
    winerror = getattr(exc, "winerror", None)
    code = exc.errno
    raw = exc.strerror or str(exc)
    path = exc.filename if exc.filename is not None else exc.filename2
    if path is None:
        return UserInputError("cli.error.os", detail=raw)
    shown = _display_path(path)
    if winerror in _WIN_IN_USE:
        return _OsError("fileio.error.in_use", path=shown, os_text=raw)
    if winerror in _WIN_NOT_FOUND or isinstance(exc, FileNotFoundError) or code == errno.ENOENT:
        return _OsError("cli.error.os.not_found", path=shown, os_text=raw)
    if winerror in _WIN_DISK_FULL or code == errno.ENOSPC:
        return _OsError("fileio.error.disk_full", path=shown, os_text=raw)
    if winerror in _WIN_EXISTS or isinstance(exc, FileExistsError) or code == errno.EEXIST:
        return _OsError("cli.error.os.exists", path=shown, os_text=raw)
    if winerror in _WIN_ACCESS_DENIED or isinstance(exc, PermissionError) or code in (errno.EACCES, errno.EPERM):
        if _read_only_file(path):
            return _OsError("fileio.error.read_only", path=shown, os_text=raw)
        return _OsError("cli.error.os.access_denied", path=shown, os_text=raw)
    return UserInputError("cli.error.os", detail=f"{raw}: {shown}")


# ==================================================================================
# Ctrl+C / Ctrl+Break
# ==================================================================================
class _Interrupts:
    """SIGINT/SIGBREAK handlers that cancel ``token``; the second signal raises
    ``KeyboardInterrupt`` at once. Installed only in the main thread; restored afterwards."""

    def __init__(self, token: CancelToken) -> None:
        self.token = token
        self._saved: list[tuple[int, Any]] = []

    def install(self) -> None:
        if threading.current_thread() is not threading.main_thread():
            return
        for name in ("SIGINT", "SIGBREAK"):
            signum = getattr(signal, name, None)
            if signum is None:
                continue
            try:
                previous = signal.signal(signum, self._handle)
            except (OSError, ValueError, RuntimeError):  # pragma: no cover
                continue
            self._saved.append((signum, previous))

    def _handle(self, signum: int, frame: Any) -> None:
        if self.token.cancelled:
            raise KeyboardInterrupt
        self.token.cancel()

    def restore(self) -> None:
        while self._saved:
            signum, previous = self._saved.pop()
            try:
                signal.signal(signum, previous if previous is not None else signal.SIG_DFL)
            except (OSError, ValueError, RuntimeError):  # pragma: no cover
                pass


# ==================================================================================
# command context and shared steps
# ==================================================================================
class _Ctx:
    """Everything a command needs: arguments, console, cancel token, settings."""

    def __init__(self, args: argparse.Namespace, console: _Console, cancel: CancelToken) -> None:
        from .settings import get_settings

        self.args = args
        self.console = console
        self.cancel = cancel
        self.settings = get_settings()
        self.started = time.perf_counter()
        # warnings of the input document (doc.info.warnings, see _doc_warnings): printed
        # already, and those it had when it was opened (printed by the command or never)
        self.warnings_printed: set[tuple[str, str]] = set()
        self.warnings_at_open: set[tuple[str, str]] = set()

    def stage(self, label: str) -> Progress | None:
        return self.console.progress.stage(label)

    def end_stage(self) -> None:
        self.console.progress.clear()

    def warn(self, text: str) -> None:
        self.console.warn(text)

    def info(self, text: str) -> None:
        self.console.info(text)

    def elapsed(self) -> float:
        return time.perf_counter() - self.started


def _check_input(path: str) -> None:
    from .fileio import long_path

    lp = long_path(path)
    if os.path.isdir(lp):
        raise UserInputError("fileio.error.is_directory", path=path)
    if not os.path.exists(lp):
        raise UserInputError("error.file_not_found", path=path)


def _same_file(a: str, b: str) -> bool:
    from .fileio import long_path, normalize_path

    try:
        if normalize_path(a) == normalize_path(b):
            return True
    except (OSError, ValueError):  # pragma: no cover
        pass
    try:
        return os.path.samefile(long_path(a), long_path(b))
    except (OSError, ValueError):
        return False


def _check_output(input_path: str, output_path: str) -> None:
    """Refuse an output that is the input itself (AGENT_PROMPT §1.4: the source is never
    modified), a folder, an existing read-only file, or a path in a folder that does not
    exist -- before any work."""
    from .fileio import long_path

    if _same_file(input_path, output_path):
        raise UserInputError("writer.error.same_file", path=output_path)
    if os.path.isdir(long_path(output_path)):
        raise UserInputError("fileio.error.is_directory", path=output_path)
    if _read_only_file(output_path):
        raise UserInputError("fileio.error.read_only", path=output_path)
    parent = os.path.dirname(os.path.abspath(output_path))
    if parent and not os.path.isdir(long_path(parent)):
        raise UserInputError("writer.error.no_dir", path=parent)


def _input_changed(doc: Any) -> bool:
    """The (still open) input changed since it was opened; never raises."""
    try:
        return bool(doc.check_changed())
    except Exception:  # pragma: no cover - a failing check must not hide the real error
        log.debug("check_changed failed", exc_info=True)
        return False


def _refuse_record_path_on_values(ctx: _Ctx) -> None:
    """``--record-path`` selects the records inside ONE JSON document: an input detected
    (``--format auto``, without the record path) as JSON Lines or as several JSON values
    one after another is refused before any work -- exit 1, like a forced
    ``--format jsonl --record-path``."""
    from .reader import detect_format

    args = ctx.args
    info = detect_format(args.input, settings=ctx.settings, progress=ctx.stage(t("cli.stage.preparing")),
                         cancel=ctx.cancel)
    ctx.end_stage()
    if info.kind in ("jsonl", "stream"):
        raise UserInputError("cli.error.record_path_values", path=_display_text(str(args.record_path)),
                             format=info.label)


def _doc_warnings(doc: Any) -> list[tuple[tuple[str, str], str]]:
    """``(identity, text)`` of the document's warnings in order. A warning is identified by
    its key and ``path`` parameter; one added more than once (e.g. with a newer byte count)
    appears once, at its first position, with its latest parameters."""
    info = doc.info
    keys = list(info.warnings)
    params = list(info.warning_params)
    texts = info.warning_texts()   # never shorter than keys (warnings are only appended)
    latest: dict[tuple[str, str], int] = {}
    for i, key in enumerate(keys):
        p = params[i] if i < len(params) and isinstance(params[i], dict) else {}
        latest[(key, str(p.get("path", "")))] = i
    return [(ident, texts[i]) for ident, i in latest.items()]


def _shown_now(ctx: _Ctx, doc: Any) -> list[str]:
    """The document's warnings to show at this point -- not printed yet, not one of
    :data:`END_WARNINGS` -- marked as printed."""
    out = []
    for ident, text in _doc_warnings(doc):
        if ident[0] in END_WARNINGS or ident in ctx.warnings_printed:
            continue
        ctx.warnings_printed.add(ident)
        out.append(text)
    return out


def _late_warnings(ctx: _Ctx, doc: Any) -> None:
    """At the end of the command: print the warnings the document gained while it ran and
    the :data:`END_WARNINGS` (with their latest values), each once; a warning it had when
    it was opened but that the command never showed (it failed before) stays unprinted."""
    for ident, text in _doc_warnings(doc):
        if ident in ctx.warnings_printed or ident in ctx.warnings_at_open:
            continue
        ctx.warnings_printed.add(ident)
        ctx.warn(text)


@contextlib.contextmanager
def _document(ctx: _Ctx, *, build: bool = True) -> Iterator[Any]:
    """INPUT opened (and indexed unless ``build`` is false or a complete cached index
    exists), closed when the block ends. An error other than a cancellation raised while
    the document is open becomes :class:`FileChangedError` when another program changed the
    file meanwhile: that is the real cause, not the garbled data it produced. Warnings the
    document gains while the block runs are printed when it ends (:func:`_late_warnings`).

    ``--record-path``: refused on an input detected as JSON Lines / several values
    (:func:`_refuse_record_path_on_values`); a syntax error in it gets record-path
    examples as its hint."""
    from .reader import Document

    args = ctx.args
    _check_input(args.input)
    fmt = None if args.format in (None, "auto") else args.format
    try:
        if args.record_path is not None and fmt is None:
            _refuse_record_path_on_values(ctx)
        doc = Document.open(args.input, record_path=args.record_path, format=fmt, settings=ctx.settings,
                            use_cache=not args.no_cache, progress=ctx.stage(t("cli.stage.preparing")),
                            cancel=ctx.cancel)
    except QuerySyntaxError as exc:
        if args.record_path is not None:  # the only path parsed while opening
            _set_hint(exc, t("cli.hint.record_path_syntax"))
        raise
    try:
        ctx.warnings_at_open = {ident for ident, _ in _doc_warnings(doc) if ident[0] not in END_WARNINGS}
        ctx.end_stage()
        if build and not doc.index_complete:
            doc.build_index(progress=ctx.stage(t("cli.stage.indexing")), cancel=ctx.cancel)
            ctx.end_stage()
        yield doc
    except (Cancelled, KeyboardInterrupt, FileChangedError, _OutputClosed, GeneratorExit):
        raise
    except BaseException as exc:
        if _input_changed(doc):
            log.warning("the input changed while it was read; reported instead of %s: %s", type(exc).__name__, exc)
            raise FileChangedError(path=str(doc.source_path)) from exc
        raise
    finally:
        try:
            _late_warnings(ctx, doc)
        except Exception:  # pragma: no cover - a warning must never hide the command's own result
            log.debug("could not show the document's late warnings", exc_info=True)
        finally:
            doc.close()


def _line_reason(code: str, expected: str) -> str:
    """Why a JSON Lines line is broken, in line-oriented words: ``json.line_error.<code>``
    / ``json.line_expected.<code>`` where they exist (the general texts speak of the end of
    the whole file), else ``json.error.<code>`` / ``json.expected.<code>``."""
    key = f"json.line_error.{code}"
    text = t(key) if has(key) else t(f"json.error.{code}")
    if expected and expected != "none":
        key = f"json.line_expected.{expected}"
        exp = t(key) if has(key) else t(f"json.expected.{expected}")
        if exp:
            text = f"{text}; {exp}"
    return text


def _broken_line_message(b: Any) -> str:
    return t("reader.broken_line", line=b.line, column=b.column, reason=_line_reason(b.code, b.expected))


def _broken_lines_text(doc: Any) -> str:
    numbers = [str(b.line) for b in doc.broken_lines[:BROKEN_SHOWN]]
    if doc.broken_count > len(numbers):
        numbers.append(ELLIPSIS)
    return _tn("cli.warn.broken_lines", doc.broken_count, count=_num(doc.broken_count), lines=", ".join(numbers))


def _partial_hint(doc: Any) -> str:
    n = doc.record_count
    if n == 0:
        return t("cli.hint.no_records_before_error")
    return _tn("cli.hint.allow_partial", n, count=_num(n))


_VALUE_START = frozenset('{["-0123456789tfn')


def _several_values(doc: Any, err: JsonSyntaxError) -> str | None:
    """``"jsonl"`` / ``"stream"`` when the JSON error shows that the input holds several
    JSON values (or the reader's ``format_hint`` says so), else ``None``.

    The error must be ``trailing_data`` where the trailing data starts a JSON value: after
    a line break following a root value that ended on line 1, one value per line
    (``jsonl``); otherwise values one after another (``stream``)."""
    hint = getattr(doc, "format_hint", None)
    if callable(hint):
        try:
            hint = hint()
        except Exception:  # pragma: no cover - an optional reader helper
            hint = None
    if isinstance(hint, str) and hint in ("jsonl", "stream"):
        return hint
    if err.code != "trailing_data" or not err.context_after or err.context_after[0] not in _VALUE_START:
        return None
    before = err.context_before or ""
    value_part = before.rstrip()
    gap = before[len(value_part):]
    if "\n" in gap and value_part and err.line - gap.count("\n") == 1:
        return "jsonl"
    return "stream"


def _format_hint(ctx: _Ctx, doc: Any, err: JsonSyntaxError) -> str | None:
    """A ``--format jsonl`` / ``--format stream`` hint (instead of ``--allow-partial``) for
    an input that evidently holds several JSON values (blackbox F2: e.g. a JSON Lines file
    named ``.json`` whose first line is larger than the head used for format detection)."""
    kind = _several_values(doc, err)
    if kind is None:
        return None
    if ctx.args.record_path is not None:
        return t("cli.hint.record_path_values")
    return t("cli.hint.format_jsonl") if kind == "jsonl" else t("cli.hint.format_stream")


def _candidate_list(cands: Sequence[Any]) -> str:
    parts = []
    for c in cands[:CANDIDATES_SHOWN]:
        items = _num(c.items_in_head) + ("" if c.closed_in_head else "+")
        parts.append(_tn("cli.hint.candidate", c.items_in_head if c.closed_in_head else 2,
                         path=_display_text(c.text), items=items))
    if len(cands) > CANDIDATES_SHOWN:
        parts.append(ELLIPSIS)
    return ", ".join(parts)


def _record_path_hint(doc: Any, shown: str) -> str:
    cands = list(doc.info.candidates)
    if not cands:
        if doc.info.root_char == "[":
            return t("cli.hint.record_path_root_array")
        return t("cli.hint.record_path_none")
    texts = [c.text for c in cands]
    close = difflib.get_close_matches(shown, texts, n=1, cutoff=0.5)
    example = close[0] if close else texts[0]
    return t("cli.hint.record_path_candidates", candidates=_candidate_list(cands), example=_display_text(example))


def _record_path_error(ctx: _Ctx, doc: Any, err: JsonSyntaxError) -> JsonReshapeError:
    """A record path that matches nothing / a scalar: a user error (exit 1) listing the
    record arrays found at the start of the file -- not a JSON error of the file."""
    shown = ctx.args.record_path if ctx.args.record_path is not None else (doc.info.record_path_text or "$")
    shown = _display_text(str(shown))
    hint = _record_path_hint(doc, shown)
    if err.code == "target_not_found":
        return _HintedError("cli.error.record_path_not_found", hint=hint, path=shown)
    return _HintedError("cli.error.record_path_not_container", hint=hint, path=shown)


def _start_warnings(ctx: _Ctx, doc: Any) -> None:
    """Format notes of the opened document (automatic record path, non-standard JSON, ...);
    the ones it gains later and the :data:`END_WARNINGS` are printed at the end
    (:func:`_late_warnings`)."""
    for text in _shown_now(ctx, doc):
        ctx.warn(text)


def _check_index(ctx: _Ctx, doc: Any) -> int | None:
    """After indexing: the file must be unchanged; a record path that leads nowhere is a
    user error; a JSON error gives exit code 2 unless ``--allow-partial`` (then a warning);
    broken JSON Lines lines give a warning."""
    doc.ensure_unchanged()
    err = doc.error
    if err is not None:
        if err.code in RECORD_PATH_CODES:
            raise _record_path_error(ctx, doc, err)
        hint = _format_hint(ctx, doc, err)
        if not ctx.args.allow_partial:
            ctx.console.error(err, hint=hint or _partial_hint(doc))
            return EXIT_JSON
        ctx.warn(_tn("cli.warn.partial", doc.record_count, line=err.line, column=err.column,
                     count=_num(doc.record_count)))
        if hint:
            ctx.console.hint(hint)
    if doc.broken_count:
        ctx.warn(_broken_lines_text(doc))
    return None


def _check_document(ctx: _Ctx, doc: Any) -> int | None:
    _start_warnings(ctx, doc)
    return _check_index(ctx, doc)


# ==================================================================================
# inspect
# ==================================================================================
_RECORDS_MARK = "\x00jsonreshape:records\x00"   # stands for the record container inside the wrapper


def _wrapper_data(doc: Any) -> dict[str, Any] | None:
    """The outer wrapper of a wrapped document (AGENT_PROMPT §3.4): sizes of the parts
    before/after the record container and its members as compact JSON (the container
    itself, at any depth, shown as a placeholder). ``members`` is ``None`` when a part is
    larger than :data:`WRAPPER_READ_MAX` or the wrapper cannot be parsed; ``None`` for
    other documents and for a record container that never closed."""
    from . import codec

    if doc.info.kind != "wrapped" or not hasattr(doc, "wrapper_spans"):
        return None
    spans = doc.wrapper_spans()
    if spans is None:
        return None
    (ps, pe), (ss, se) = spans
    data: dict[str, Any] = {"bytes_before": int(pe - ps), "bytes_after": int(se - ss), "members": None}
    if pe - ps > WRAPPER_READ_MAX or se - ss > WRAPPER_READ_MAX:
        return data
    try:
        parts = doc.wrapper_parts()
        if parts is None:
            return data
        mark = codec.dumps(_RECORDS_MARK)
        value = codec.loads(parts[0] + mark + parts[1])
    except (TooLargeError, ValueError, RecursionError) as exc:
        log.info("inspect: the wrapper of %s is not shown: %s", doc.source_path, exc)
        return data
    except JsonReshapeError as exc:
        if isinstance(exc, FileChangedError):
            raise
        log.info("inspect: the wrapper of %s is not shown: %s", doc.source_path, exc)
        return data
    placeholder = "{" + ELLIPSIS + "}" if doc.member_records else "[" + ELLIPSIS + "]"
    mark_text = mark.decode("utf-8")
    if isinstance(value, dict):
        items: Iterable[tuple[Any, Any]] = value.items()
    elif isinstance(value, list):
        items = enumerate(value)
    else:
        return data
    members = []
    for key, member in items:
        if isinstance(member, str) and member == _RECORDS_MARK:
            members.append({"key": key, "records": True, "json": None, "truncated": False})
            continue
        text = codec.dumps(member).decode("utf-8", "replace").replace(mark_text, placeholder)
        cut = len(text) > WRAPPER_JSON_CHARS
        members.append({"key": key, "records": False, "json": _shorten(text, WRAPPER_JSON_CHARS),
                        "truncated": cut})
    data["members"] = members
    return data


def _wrapper_lines(wrapper: dict[str, Any], records: int) -> list[str]:
    members = wrapper["members"]
    if members is None:
        return [t("cli.inspect.wrapper_unread", before=format_bytes(wrapper["bytes_before"]),
                  after=format_bytes(wrapper["bytes_after"]))]
    lines = []
    for m in members[:WRAPPER_MEMBERS_SHOWN]:
        key = m["key"]
        name = f"[{key}]" if isinstance(key, int) else _display_text(str(key))
        if m["records"]:
            value = t("cli.inspect.wrapper_records", count=_num(records))
        else:
            value = _display_text(_shorten(m["json"] or "", WRAPPER_TEXT_CHARS))
        lines.append(f"{name}: {value}")
    if len(members) > WRAPPER_MEMBERS_SHOWN:
        lines.append(ELLIPSIS)
    return lines


def _inspect_data(doc: Any, warnings: list[str] | None = None) -> dict[str, Any]:
    """Everything ``inspect`` reports, with exact values (also the ``--json`` output).
    ``warnings``: the document's warnings shown in the report (default: all of them)."""
    from . import core
    from .fileio import encoding_label

    info = doc.info
    stats = dict(doc.index_stats or {})
    flags = int(doc.flags)
    err = doc.error
    error = None
    if err is not None:
        error = err.to_dict()
        error["message"] = err.message
        error["records_before"] = doc.record_count
    broken = [{"line": b.line, "column": b.column, "code": b.code, "expected": b.expected,
               "message": _broken_line_message(b), "preview": b.preview} for b in doc.broken_lines[:BROKEN_JSON_MAX]]
    return {
        "file": str(doc.source_path),
        "size": int(doc.size),
        "data_size": int(doc.data_size),
        "encoding": info.encoding,
        "encoding_label": encoding_label(info.encoding),
        "compression": info.compression,
        "format": {
            "kind": info.kind,
            "label": info.label,
            "auto": bool(info.auto),
            "record_path": None if info.record_path is None else list(info.record_path),
            "record_path_text": info.record_path_text,
            "member_records": bool(doc.member_records),
            "candidates": [dict(c.to_dict(), text=c.text, items_kind=c.items_kind) for c in info.candidates],
        },
        "wrapper": _wrapper_data(doc),
        "records": int(doc.record_count),
        "index": {
            "complete": bool(doc.index_complete),
            "from_cache": bool(doc.index_from_cache),
            "seconds": stats.get("seconds"),
            "bytes": stats.get("bytes"),
            "mb_per_s": stats.get("mb_per_s"),
            "first_record_seconds": stats.get("first_record_seconds"),
            "backend": stats.get("backend"),
        },
        "core_backend": str(core.BACKEND),
        "flags": {
            "escapes": bool(flags & core.FLAG_ESCAPES),
            "u_escapes": bool(flags & core.FLAG_U_ESCAPES),
            "non_ascii": bool(flags & core.FLAG_NON_ASCII),
        },
        "error": error,
        "broken_lines": {"count": int(doc.broken_count), "first": broken},
        "warnings": info.warning_texts() if warnings is None else list(warnings),
    }


def _yes_no(flag: bool) -> str:
    return t("common.yes") if flag else t("common.no")


def _backend_label(name: str) -> str:
    if name == "rust":
        return t("env.backend.rust")
    if name == "python":
        return t("env.backend.python")
    return t("env.backend.none")


def _items_kind_label(kind: str) -> str:
    return {
        "objects": t("cli.inspect.items.objects"),
        "arrays": t("cli.inspect.items.arrays"),
        "mixed": t("cli.inspect.items.mixed"),
        "values": t("cli.inspect.items.values"),
    }.get(kind, t("cli.inspect.items.empty"))


def _inspect_lines(data: dict[str, Any], doc: Any) -> list[str]:
    rows: list[tuple[str, str]] = [(t("cli.inspect.file"), _display_text(data["file"]))]
    size = _tn("cli.inspect.size_value", data["size"], size=format_bytes(data["size"]), bytes=_num(data["size"]))
    if data["data_size"] != data["size"] or data["compression"]:
        size += "; " + t("cli.inspect.data_size", size=format_bytes(data["data_size"]))
    rows.append((t("cli.inspect.size"), size))
    encoding = data["encoding_label"] + (f", {data['compression']}" if data["compression"] else "")
    rows.append((t("cli.inspect.encoding"), encoding))
    fmt = data["format"]
    mode = t("cli.inspect.auto") if fmt["auto"] else t("cli.inspect.forced")
    rows.append((t("cli.inspect.format"), t("cli.inspect.format_value", label=fmt["label"], mode=mode)))
    if fmt["record_path_text"] is not None:
        rows.append((t("cli.inspect.record_path"), _display_text(fmt["record_path_text"])))
    if data.get("wrapper") is not None:
        rows.append((t("cli.inspect.wrapper"), "\n".join(_wrapper_lines(data["wrapper"], data["records"]))))
    if fmt["candidates"]:
        cands = []
        for c in fmt["candidates"][:CANDIDATES_SHOWN]:
            more = "" if c["closed_in_head"] else "+"
            count = c["items_in_head"] if c["closed_in_head"] else 2
            cands.append(_tn("cli.inspect.candidate", count, path=_display_text(c["text"]),
                             items=_num(c["items_in_head"]) + more, kind=_items_kind_label(c["items_kind"])))
        if len(fmt["candidates"]) > CANDIDATES_SHOWN:
            cands.append(ELLIPSIS)
        rows.append((t("cli.inspect.candidates"), "\n".join(cands)))
    records = _num(data["records"])
    if data["error"] is not None:
        records = t("cli.inspect.records_partial", count=records)
    rows.append((t("cli.inspect.records"), records))
    idx = data["index"]
    if idx["from_cache"]:
        index_text = t("cli.inspect.index_cached")
    elif idx["seconds"] is not None:
        index_text = t("cli.inspect.index_built", seconds=f"{float(idx['seconds']):.2f}",
                       rate=f"{float(idx['mb_per_s'] or 0.0):.1f}")
    else:  # pragma: no cover - the index is always built before inspect reports
        index_text = t("common.unknown")
    rows.append((t("cli.inspect.index"), index_text))
    rows.append((t("cli.inspect.backend"), _backend_label(data["core_backend"])))
    flags = data["flags"]
    rows.append((t("cli.inspect.strings"), t("cli.inspect.strings_value", escapes=_yes_no(flags["escapes"]),
                                                u_escapes=_yes_no(flags["u_escapes"]),
                                                non_ascii=_yes_no(flags["non_ascii"]))))
    err = doc.error
    if err is not None:
        lines = [err.message]
        lines.extend(_detail_lines(err))
        lines.append(_tn("cli.inspect.error_records", doc.record_count, count=_num(doc.record_count)))
        rows.append((t("cli.inspect.error"), "\n".join(lines)))
    if doc.broken_count:
        lines = [t("cli.inspect.broken_value", count=_num(doc.broken_count))]
        for b in doc.broken_lines[:BROKEN_SHOWN]:
            lines.append(t("cli.inspect.broken_line", message=_broken_line_message(b), preview=_display_text(b.preview)))
        if doc.broken_count > BROKEN_SHOWN:
            lines.append(ELLIPSIS)
        rows.append((t("cli.inspect.broken"), "\n".join(lines)))
    if data["warnings"]:
        rows.append((t("cli.inspect.warnings"), "\n".join(data["warnings"])))
    width = max(len(label) for label, _ in rows) + 2
    out: list[str] = []
    for label, value in rows:
        parts = value.split("\n")
        out.append((label + ":").ljust(width) + parts[0])
        out.extend(" " * width + part for part in parts[1:])
    return out


def _cmd_inspect(ctx: _Ctx) -> int:
    from .schema import infer_schema

    args = ctx.args
    sample = args.sample if args.sample is not None else ctx.settings.sample_size
    with _document(ctx) as doc:
        doc.ensure_unchanged()
        err = doc.error
        if err is not None and err.code in RECORD_PATH_CODES:
            raise _record_path_error(ctx, doc, err)
        blocked = err is not None and not args.allow_partial
        data = _inspect_data(doc, _shown_now(ctx, doc))   # later ones and END_WARNINGS: at the end
        schema = None
        if not blocked:
            schema = infer_schema(doc, sample=sample, progress=ctx.stage(t("cli.stage.schema")), cancel=ctx.cancel)
            ctx.end_stage()
        doc.ensure_unchanged()  # the report describes the file as it still is
        if args.json:
            data["schema"] = schema.to_dict() if schema is not None else None
            data["schema_summary"] = schema.summary() if schema is not None else None
            ctx.console.out(json.dumps(data, ensure_ascii=False, indent=2))
        else:
            lines = _inspect_lines(data, doc)
            if schema is not None:
                width = _terminal_columns() - 1 if _isatty(sys.stdout) else None
                lines.extend(["", schema.format_table(width=width), "", schema.summary()])
            ctx.console.out_lines(lines)
        hint = _format_hint(ctx, doc, err) if err is not None else None
        if blocked:
            ctx.console.err(t("cli.error.prefix", message=t("cli.error.json_input")))
            ctx.console.hint(hint or _partial_hint(doc))
            return EXIT_JSON
        if hint:
            ctx.console.hint(hint)
    return EXIT_OK


# ==================================================================================
# search
# ==================================================================================
def _highlight(text: str, spans: Iterable[tuple[int, int]], color: bool) -> str:
    on, off = (HIGHLIGHT_ON, HIGHLIGHT_OFF) if color else (MARK_OPEN, MARK_CLOSE)
    out: list[str] = []
    pos = 0
    n = len(text)
    for s, e in sorted(spans):
        s = max(pos, min(int(s), n))
        e = max(s, min(int(e), n))
        if e <= s:
            continue
        out.append(_display_text(text[pos:s]))
        out.append(on + _display_text(text[s:e]) + off)
        pos = e
    out.append(_display_text(text[pos:]))
    return "".join(out)


class _HitPrinter:
    """Prints hits (``#record  path  [kalit|qiymat]  snippet``) up to ``limit``."""

    def __init__(self, console: _Console, limit: int, *, stop_on_close: bool = True) -> None:
        self.console = console
        self.limit = limit
        self.printed = 0
        self.stop_on_close = stop_on_close
        self.closed = False

    def line(self, hit: Any) -> str:
        where = t("search.where.key") if hit.where == "key" else t("search.where.value")
        return (f"#{hit.record}  {_display_text(hit.path_text)}  [{where}]  "
                f"{_highlight(hit.text, hit.spans, self.console.color)}")

    def add_many(self, hits: Iterable[Any]) -> None:
        if self.closed:
            return
        lines = []
        for hit in hits:
            if self.printed >= self.limit:
                break
            lines.append(self.line(hit))
            self.printed += 1
        if lines:
            try:
                self.console.out_lines(lines)
            except _OutputClosed:
                if self.stop_on_close:
                    raise
                self.closed = True


class _HitCounter:
    """Counts the hits (and distinct records) streamed to an export, printing the first
    ones; ``on_end`` runs when the hits are exhausted -- before the export is committed."""

    def __init__(self, printer: _HitPrinter, on_end: Callable[[], Any] | None = None) -> None:
        self.printer = printer
        self.on_end = on_end
        self.hits = 0
        self.records = 0
        self._last: int | None = None

    def wrap(self, hits: Iterable[Any]) -> Iterator[Any]:
        for hit in hits:
            self.hits += 1
            if hit.record != self._last:
                self.records += 1
                self._last = hit.record
            if self.printer.printed < self.printer.limit:
                self.printer.add_many((hit,))
            yield hit
        if self.on_end is not None:
            self.on_end()


def _hits_export_format(path: str) -> str:
    ext = os.path.splitext(path)[1].lower().lstrip(".")
    if ext not in HIT_EXPORT_FORMATS:
        raise UserInputError("search.export.bad_format", fmt=ext or path)
    return ext


def _bytes_mode_possible(doc: Any, opts: Any) -> bool:
    """Would :func:`jsonreshape.search.search` use its byte prefilter (complete index)?"""
    q = opts.query
    if q.mode != "plain" or q.translit or doc.record_count == 0:
        return False
    return bool(q.prefilter_needles(doc.flags))


def _cmd_search(ctx: _Ctx) -> int:
    from .search import SearchHistory, SearchOptions, export_hits, iter_hits, search

    args = ctx.args
    mode = "regex" if args.regex else "number" if args.number else "plain"
    scope = "keys" if args.keys else "values" if args.values else "both"
    opts = SearchOptions.build(args.text, mode=mode, case_sensitive=args.case_sensitive, whole_word=args.whole_word,
                               translit=args.translit, scope=scope, path=args.path)
    export_fmt = None
    if args.export is not None:
        _check_output(args.input, args.export)
        export_fmt = _hits_export_format(args.export)
    show = 0 if args.count else min(args.limit, args.max_hits)
    # printing hits does not wait for the index: search() follows it while it is built
    follow = export_fmt is None and not args.count
    with _document(ctx, build=not follow) as doc:
        _start_warnings(ctx, doc)
        checked = False
        if doc.index_complete:
            rc = _check_index(ctx, doc)
            if rc is not None:
                return rc
            checked = True
        exported = None
        if export_fmt is None:
            printer = _HitPrinter(ctx.console, show)
            result = search(doc, opts, max_hits=show, progress=ctx.stage(t("cli.stage.searching")), cancel=ctx.cancel,
                            on_hits=printer.add_many)
            ctx.end_stage()
            result.close()
            hits, records = result.hit_count, result.record_count
            mode_used, scanned, elapsed = result.mode_used, result.bytes_scanned, result.elapsed
        else:
            printer = _HitPrinter(ctx.console, show, stop_on_close=False)
            counter = _HitCounter(printer, on_end=doc.ensure_unchanged)
            started = time.perf_counter()
            stream = iter_hits(doc, opts, progress=ctx.stage(t("cli.stage.searching")), cancel=ctx.cancel)
            exported = export_hits(counter.wrap(stream), args.export, fmt=export_fmt, cancel=ctx.cancel)
            ctx.end_stage()
            elapsed = time.perf_counter() - started
            hits, records = counter.hits, counter.records
            mode_used = "bytes" if _bytes_mode_possible(doc, opts) else "parse"
            scanned = max(0, int(doc.data_size) - int(doc.info.data_start))
        if not checked:
            # the search followed the index while it was built: its warnings and errors now
            doc.build_index(cancel=ctx.cancel)  # normally complete already; waits for a running build
            rc = _check_index(ctx, doc)
            if rc is not None:
                return rc
        if exported is None:  # an export was checked right before its commit (_HitCounter.on_end)
            doc.ensure_unchanged()
        if args.count:
            ctx.console.out(t("cli.search.count", hits=_hits_text(hits, str(hits)),
                              records=_records_text(records, str(records))))
        rate = scanned / 1e6 / elapsed if elapsed > 0 else 0.0
        ctx.info(t("cli.search.summary", hits=_hits_text(hits, _num(hits)), records=_records_text(records, _num(records)),
                   size=format_bytes(scanned), seconds=format_duration(elapsed), rate=f"{rate:.0f}",
                   mode=t(f"search.mode.{mode_used}")))
        if not args.count and printer.limit > 0 and hits > printer.printed:
            option = "--max-hits N" if args.max_hits < args.limit else "--limit N"
            key = "cli.search.more_export" if exported is not None else "cli.search.more"
            ctx.info(_tn(key, printer.printed, shown=_num(printer.printed), total=_num(hits), option=option))
        if exported is not None:
            ctx.info(_tn("cli.search.exported", exported, count=_num(exported), path=args.export))
    try:
        SearchHistory().add(args.text, opts.to_dict())
    except Exception:  # the history is a convenience: never fail the search because of it
        log.warning("could not save the search history", exc_info=True)
    return EXIT_OK


# ==================================================================================
# filter / sort / convert
# ==================================================================================
#: output option -> the formats it applies to
_FORMAT_OPTIONS: dict[str, tuple[str, ...]] = {
    "--compact": ("json",),
    "--indent": ("json",),
    "--sort-keys": ("json", "jsonl"),
    "--no-wrapper": ("json",),
    "--as-keyed": ("json",),
    "--columns": ("csv", "xlsx"),
    "--array-mode": ("csv", "xlsx"),
    "--explode": ("csv", "xlsx"),
    "--sep": ("csv", "xlsx"),
    "--delimiter": ("csv",),
    "--no-bom": ("csv",),
    "--xlsx-split": ("xlsx",),
}


def _output_format(args: argparse.Namespace) -> str:
    from .writer import format_for_path

    if args.jsonl:
        if args.to not in (None, "jsonl"):
            raise UserInputError("cli.error.jsonl_conflict", fmt=args.to)
        return "jsonl"
    if args.to is not None:
        return args.to
    fmt = format_for_path(args.output)
    if fmt is None:
        raise UserInputError("cli.error.output_format", path=args.output)
    return fmt


def _split_columns(text: str) -> list[str]:
    """``a,b.c,d["x,y"]`` -> ``["a", "b.c", 'd["x,y"]']`` (commas inside brackets or
    quotes do not split)."""
    parts: list[str] = []
    buf: list[str] = []
    depth = 0
    quote: str | None = None
    i = 0
    while i < len(text):
        ch = text[i]
        if quote is not None:
            buf.append(ch)
            if ch == "\\" and i + 1 < len(text):
                buf.append(text[i + 1])
                i += 2
                continue
            if ch == quote:
                quote = None
        elif ch in "\"'`":
            quote = ch
            buf.append(ch)
        elif ch == "[":
            depth += 1
            buf.append(ch)
        elif ch == "]":
            depth = max(0, depth - 1)
            buf.append(ch)
        elif ch == "," and depth == 0:
            parts.append("".join(buf).strip())
            buf = []
        else:
            buf.append(ch)
        i += 1
    parts.append("".join(buf).strip())
    return [p for p in parts if p]


def _given_output_options(args: argparse.Namespace) -> list[str]:
    given = {
        "--compact": args.compact,
        "--indent": args.indent is not None,
        "--sort-keys": args.sort_keys is not None,
        "--no-wrapper": args.no_wrapper,
        "--as-keyed": args.as_keyed,
        "--columns": args.columns is not None,
        "--array-mode": args.array_mode is not None,
        "--explode": args.explode is not None,
        "--sep": args.sep is not None,
        "--delimiter": args.delimiter is not None,
        "--no-bom": args.no_bom,
        "--xlsx-split": args.xlsx_split is not None,
    }
    return [name for name, on in given.items() if on]


def _export_options(ctx: _Ctx, fmt: str) -> Any:
    from .writer import ExportOptions

    args = ctx.args
    overrides: dict[str, Any] = {}
    if args.compact:
        overrides["indent"] = None
    elif args.indent is not None:
        overrides["indent"] = args.indent
    if args.sort_keys is not None:
        overrides["sort_keys"] = args.sort_keys
    if args.no_wrapper:
        overrides["keep_wrapper"] = False
    if args.as_keyed:
        if fmt != "json":   # a user error, not an option that is ignored with a warning
            raise UserInputError("cli.error.as_keyed_format", fmt=fmt)
        overrides["as_keyed"] = True
    if args.columns is not None:
        overrides["columns"] = _split_columns(args.columns)
    if args.array_mode is not None:
        overrides["array_mode"] = args.array_mode
    if args.explode is not None:
        overrides["explode"] = args.explode
    if args.sep is not None:
        overrides["join_sep"] = args.sep
    if args.delimiter is not None:
        overrides["csv_delimiter"] = args.delimiter
    if args.no_bom:
        overrides["csv_bom"] = False
    if args.xlsx_split is not None:
        overrides["xlsx_split"] = args.xlsx_split
    try:
        return ExportOptions.from_settings(fmt, ctx.settings, **overrides)
    except UserInputError as exc:
        # a writer without key orders takes only a bool (its one order: code points)
        if exc.key != "writer.invalid_option" or exc.params.get("name") != "sort_keys" \
                or not isinstance(overrides.get("sort_keys"), str):
            raise
        log.warning("this writer has no key orders: --sort-keys %s sorts by code point", overrides["sort_keys"])
        overrides["sort_keys"] = True
        return ExportOptions.from_settings(fmt, ctx.settings, **overrides)


def _presets() -> Any:
    """The filter preset store (:class:`jsonreshape.query.FilterPresets`)."""
    from . import query

    store_cls = getattr(query, "FilterPresets", None)
    if store_cls is None:
        raise UserInputError("cli.error.presets_unavailable")
    return store_cls()


def _filter_source(args: argparse.Namespace) -> tuple[str, str] | None:
    """``(text, lang)`` of the filter given by ``--where`` / ``--jmespath`` / ``--preset``."""
    if getattr(args, "jmespath", None) is not None:
        return args.jmespath, "jmespath"
    if getattr(args, "where", None) is not None:
        return args.where, "expr"
    name = getattr(args, "preset", None)
    if name is not None:
        try:
            preset = _presets().get(name)
        except UserInputError as exc:
            if exc.key != "query.preset.not_found":
                raise
            raise UserInputError("cli.error.preset_not_found", name=name) from exc
        return str(preset["text"]), str(preset.get("lang") or "expr")
    return None


_JMES_COMPARATORS = frozenset({"eq", "ne", "lt", "lte", "gt", "gte"})


def _jmespath_warnings(text: str) -> list[str]:
    """A double-quoted name right after ``==``, ``<`` etc. is a *field* in JMESPath, not a
    text (``city == "Toshkent"`` compares with the field Toshkent and matches nothing). On
    the left side (``"first name" == 'Ali'``) a quoted name is the usual way to write a field."""
    try:
        from jmespath import lexer

        tokens = list(lexer.Lexer().tokenize(text))
    except Exception:  # the expression compiled: never fail on this extra check
        return []
    out = []
    for i, tok in enumerate(tokens):
        if i > 0 and tok.get("type") == "quoted_identifier" and tokens[i - 1].get("type") in _JMES_COMPARATORS:
            out.append(t("cli.warn.jmespath_quoted", name=_display_text(str(tok.get("value")))))
    return out


def _compile_filter(ctx: _Ctx) -> Any:
    """The filter of ``--where`` / ``--jmespath`` / ``--preset`` (``None`` without one);
    ``--save-preset NAME`` stores it once it compiled."""
    from .query import compile_filter

    args = ctx.args
    source = _filter_source(args)
    save_as = getattr(args, "save_preset", None)
    if source is None:
        if save_as is not None:
            raise UserInputError("cli.error.save_preset_needs_filter")
        return None
    text, lang = source
    try:
        flt = compile_filter(text, lang=lang)
    except QuerySyntaxError as exc:
        if lang == "jmespath":
            _set_hint(exc, t("cli.hint.jmespath"))
        raise
    if lang == "jmespath":
        for warning in _jmespath_warnings(text):
            ctx.warn(warning)
    if save_as is not None:
        _presets().save(save_as, text, lang=lang)
        ctx.info(t("cli.preset.saved", name=save_as))
    return flt


def _sort_keys(args: argparse.Namespace) -> list[Any]:
    from .sortkeys import SortKey

    fields = getattr(args, "sort_fields", None) or []
    options = {"nulls": args.nulls, "collation": args.collation, "natural": not args.no_natural,
               "case_sensitive": bool(args.case_sensitive)}
    return [SortKey.parse(text, **options) for text in fields]


def _sort_memory(ctx: _Ctx) -> int:
    """Bytes of sort keys kept in memory: ``--memory-limit`` (raised to at least
    :data:`MIN_MEMORY_LIMIT`) or ``Settings.sort_memory_bytes``, and never more than half
    of ``Settings.memory_limit_bytes`` -- the overall memory budget."""
    settings = ctx.settings
    limit = int(settings.memory_limit_bytes)
    budget = max(MIN_MEMORY_LIMIT, limit // 2)
    given = getattr(ctx.args, "memory_limit", None)
    if given is None:
        return min(int(settings.sort_memory_bytes), budget)
    if given < MIN_MEMORY_LIMIT:
        ctx.warn(t("cli.warn.memory_limit_min", value=_mem_text(given), minimum=_mem_text(MIN_MEMORY_LIMIT)))
        given = MIN_MEMORY_LIMIT
    if given > budget:
        ctx.warn(t("cli.warn.memory_limit_cap", value=_mem_text(given), cap=_mem_text(budget),
                   limit=_mem_text(limit), setting=t("settings.field.memory_limit_bytes")))
        given = budget
    return given


def _cmd_export(ctx: _Ctx) -> int:
    """filter / sort / convert: [filter] -> [sort] -> export, with a summary line."""
    from .recordset import filter_records, sort_records
    from .writer import export

    args = ctx.args
    _check_output(args.input, args.output)
    fmt = _output_format(args)
    opts = _export_options(ctx, fmt)
    ignored = [name for name in _given_output_options(args) if fmt not in _FORMAT_OPTIONS[name]]
    flt = _compile_filter(ctx)
    keys = _sort_keys(args) if args.command in ("sort", "convert") else []
    memory = _sort_memory(ctx) if keys else None
    if ignored:
        ctx.warn(t("cli.warn.ignored_options", options=", ".join(ignored), format=fmt))
    if flt is not None:
        for text in flt.warnings:
            ctx.warn(text)
    with _document(ctx) as doc:
        rc = _check_document(ctx, doc)
        if rc is not None:
            return rc
        total = doc.record_count
        selection: Any = None
        sets: list[Any] = []
        try:
            if flt is not None:
                selection = filter_records(doc, flt, None, progress=ctx.stage(t("cli.stage.filtering")),
                                           cancel=ctx.cancel)
                sets.append(selection)
                ctx.end_stage()
                for text in selection.report.warning_texts():
                    ctx.warn(text)
            if keys:
                selection = sort_records(doc, keys, selection, memory_limit=memory,
                                         progress=ctx.stage(t("cli.stage.sorting")), cancel=ctx.cancel)
                sets.append(selection)
                ctx.end_stage()
                for text in selection.report.warning_texts():
                    ctx.warn(text)
            doc.ensure_unchanged()  # nothing is written from a file that changed meanwhile
            report = export(doc, args.output, opts, source=selection, progress=ctx.stage(t("cli.stage.writing")),
                            cancel=ctx.cancel)
            ctx.end_stage()
        finally:
            for rs in sets:
                rs.close()
        # the partial/broken-line warnings were already given after indexing
        for key, text in zip(report.warning_keys, report.warnings):
            if key not in ("writer.warn.partial", "writer.warn.broken_lines"):
                ctx.warn(text)
        output = str(report.files[0]) if len(report.files) > 1 else args.output
        # "{selected} / {total} records": the noun goes with the total
        summary = _tn("cli.summary.export", total, selected=_num(report.records), total=_num(total), output=output,
                      size=format_bytes(report.bytes_written), seconds=format_duration(ctx.elapsed()))
        extra = []
        if fmt in ("csv", "xlsx", "sqlite") and report.rows != report.records:
            extra.append(_tn("cli.summary.rows", report.rows, rows=_num(report.rows)))
        if len(report.files) > 1:
            extra.append(_tn("cli.summary.files", len(report.files), count=_num(len(report.files))))
        if extra:
            summary += "; " + ", ".join(extra)
        ctx.info(summary)
        log.info("%s: %d of %d records -> %s (%s, %d bytes) in %.2f s", args.command, report.records, total, output,
                 fmt, report.bytes_written, ctx.elapsed())
    return EXIT_OK


# ==================================================================================
# presets
# ==================================================================================
def _cmd_presets(ctx: _Ctx) -> int:
    """List the saved filters, or delete one (``--delete NAME``)."""
    args = ctx.args
    store = _presets()
    if args.delete is not None:
        if not store.delete(args.delete):
            raise UserInputError("cli.error.preset_not_found", name=args.delete)
        ctx.info(t("cli.preset.deleted", name=args.delete))
        return EXIT_OK
    items = list(store.list())
    if args.json:
        ctx.console.out(json.dumps(items, ensure_ascii=False, indent=2, default=str))
        return EXIT_OK
    if not items:
        ctx.info(t("cli.preset.empty"))
        return EXIT_OK
    names = [_display_text(str(item.get("name", ""))) for item in items]
    width = max(len(name) for name in names)
    lines = []
    for name, item in zip(names, items):
        lines.append(t("cli.preset.line", name=name.ljust(width), lang=str(item.get("lang") or "expr"),
                       text=_display_text(str(item.get("text", "")))))
    ctx.console.out_lines(lines)
    return EXIT_OK


# ==================================================================================
# --version
# ==================================================================================
def _version_lines(backend: str | None, detail: str | None = None) -> list[str]:
    label = _backend_label(backend or "none")
    if detail:
        label = f"{label} ({detail})"
    return [
        t("cli.version", version=__version__),
        t("env.report.core", backend=label),
        t("env.report.python", version=platform.python_version(), implementation=platform.python_implementation(),
          bits=struct.calcsize("P") * 8),
    ]


# ==================================================================================
# main
# ==================================================================================
_COMMANDS: dict[str, Callable[[_Ctx], int]] = {
    "inspect": _cmd_inspect,
    "search": _cmd_search,
    "filter": _cmd_export,
    "sort": _cmd_export,
    "convert": _cmd_export,
    "presets": _cmd_presets,
}


def _start_logging(verbose: int) -> str | None:
    """File logging always; ``-v``/``-vv`` add the console (INFO/DEBUG). Returns the log path."""
    from . import log as applog

    try:
        path = applog.setup_logging("DEBUG" if verbose >= 2 else "INFO", console=verbose > 0)
    except Exception:  # an unwritable data folder must not stop the command
        return None
    return str(path)


def _apply_backend(choice: str | None) -> str | None:
    """Set ``JSONRESHAPE_BACKEND`` from ``--backend``; a note when the core of this process
    is already loaded with another backend (in-process use)."""
    if choice is None:
        return None
    os.environ["JSONRESHAPE_BACKEND"] = choice
    loaded = sys.modules.get("jsonreshape.core")
    active = getattr(loaded, "BACKEND", None) if loaded is not None else None
    if active is not None and choice != "auto" and active != choice:
        log.warning("--backend %s ignored: the core of this process is already loaded with the %s backend",
                    choice, active)
        return t("cli.warn.backend_loaded", requested=choice, active=active)
    return None


def _load_core() -> Any:
    try:
        from . import core
    except ImportError as exc:
        raise UserInputError("cli.error.backend", backend=os.environ.get("JSONRESHAPE_BACKEND", "auto"),
                             detail=str(exc)) from exc
    if getattr(core, "BACKEND", "none") == "none":
        raise UserInputError("cli.error.backend", backend=os.environ.get("JSONRESHAPE_BACKEND", "auto"),
                             detail=t("env.backend.none"))
    return core


def _check_environment() -> None:
    try:
        from .env import check_environment

        check_environment(log_result=True)
    except Exception:  # pragma: no cover - diagnostics must never stop the command
        log.warning("environment check failed", exc_info=True)


def _peak_memory() -> int | None:
    """Peak memory of this process so far, in bytes: the peak working set on Windows
    (psutil), the maximum resident set size elsewhere; ``None`` when it cannot be told."""
    try:
        import psutil

        peak = getattr(psutil.Process().memory_info(), "peak_wset", None)
        if peak:
            return int(peak)
    except Exception:  # psutil missing or failing: the next source
        log.debug("psutil could not tell the peak memory", exc_info=True)
    try:
        import resource  # POSIX
    except ImportError:  # pragma: no cover - Windows without psutil
        return None
    try:  # pragma: no cover - POSIX only
        maxrss = int(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss)
    except (OSError, ValueError, AttributeError):  # pragma: no cover
        return None
    return maxrss if sys.platform == "darwin" else maxrss * 1024  # pragma: no cover - KiB on Linux


def _memory_warning(console: _Console, args: argparse.Namespace, peak_before: int | None) -> None:
    """One warning when the peak memory of the process, reached during this command,
    exceeds ``Settings.memory_limit_bytes`` (both numbers and what to change); the command
    is not aborted. Never raises."""
    try:
        from .settings import get_settings

        peak = _peak_memory()
        if peak is None or (peak_before is not None and peak <= peak_before):
            return  # unknown, or an earlier peak of an in-process caller: not this command's
        settings = get_settings()
        limit = int(settings.memory_limit_bytes)
        if limit <= 0 or peak <= limit:
            return
        sorting = args.command == "sort" or bool(getattr(args, "sort_fields", None))
        log.info("peak memory %d bytes is above the memory limit (%d bytes)", peak, limit)
        console.warn(t("cli.warn.memory_peak_sort" if sorting else "cli.warn.memory_peak",
                       used=_mem_text(peak), limit=_mem_text(limit), setting=t("settings.field.memory_limit_bytes"),
                       large_setting=t("settings.field.large_node_bytes"),
                       large=_mem_text(int(settings.large_node_bytes))))
    except Exception:  # pragma: no cover - a diagnostic must never change the result
        log.debug("memory check failed", exc_info=True)


def _print_usage_error(exc: _UsageError) -> None:
    try:
        _write(sys.stderr, exc.parser.format_usage())
        _write(sys.stderr, t("cli.argparse.error") % {"prog": exc.parser.prog, "message": exc.message})
        _write(sys.stderr, t("cli.usage.help_hint", prog=exc.parser.prog) + "\n")
    except _OutputClosed:  # pragma: no cover
        pass
    _flush(sys.stderr)


def _internal_error(console: _Console, exc: BaseException, log_file: str | None) -> None:
    log.critical("internal error", exc_info=(type(exc), exc, exc.__traceback__))
    lines = [t("cli.error.internal", detail=f"{type(exc).__name__}: {exc}")]
    if log_file:
        lines.append(t("cli.error.see_log", path=log_file))
    if console.verbose >= 2:
        lines.append("".join(traceback.format_exception(type(exc), exc, exc.__traceback__)).rstrip())
    console.err("\n".join(lines))


def _run(args: argparse.Namespace, console: _Console, cancel: CancelToken, log_file: str | None,
         backend_note: str | None) -> int:
    try:
        if args.version:
            try:
                core = _load_core()
            except UserInputError as exc:
                console.out_lines(_version_lines(None, str(exc.params.get("detail") or "")))
                return EXIT_USER
            console.out_lines(_version_lines(str(core.BACKEND)))
            return EXIT_OK
        _load_core()
        _check_environment()
        if backend_note:
            console.warn(backend_note)
        return _COMMANDS[args.command](_Ctx(args, console, cancel))
    except _OutputClosed:
        return EXIT_OK
    except (Cancelled, KeyboardInterrupt):
        log.info("cancelled")
        console.err(t("error.cancelled"))
        return EXIT_CANCELLED
    except FileChangedError as exc:
        log.info("input changed: %s", exc.message)
        console.error(exc)
        return EXIT_JSON
    except (JsonSyntaxError, FileFormatError) as exc:
        log.info("input error: %s", exc.message)
        console.error(exc)
        return EXIT_JSON
    except JsonReshapeError as exc:
        log.info("user error: %s", exc.message)
        if isinstance(exc, PathSyntaxError) and getattr(exc, CLI_HINT_ATTR, None) is None:
            # the general examples end with "#120", a record number (Ctrl+G), not a path
            _set_hint(exc, t("cli.hint.path_syntax"))
        console.error(exc)
        return EXIT_USER
    except OSError as exc:
        log.warning("file system error", exc_info=True)
        console.error(_os_error(exc))
        return EXIT_USER
    except Exception as exc:
        _internal_error(console, exc, log_file)
        return EXIT_INTERNAL


def main(argv: Sequence[str] | None = None, *, cancel: CancelToken | None = None) -> int:
    """Run the command line ``argv`` (default ``sys.argv[1:]``) and return the exit code.

    ``cancel``: a :class:`~jsonreshape.tasks.CancelToken` shared by every operation (a new one
    by default); Ctrl+C / Ctrl+Break cancel it.
    """
    started = time.perf_counter()
    peak_before = _peak_memory()  # the memory check counts only a peak reached by this command
    raw = [str(a) for a in (sys.argv[1:] if argv is None else argv)]
    _setup_streams()
    log_file = _start_logging(0)
    set_language(_choose_language(_scan_option(raw, "--lang")))
    try:
        with localized_argparse():
            args = _parse(build_parser(), _join_dash_values(raw))
    except _ParserExit as exc:
        _flush(sys.stdout)
        return exc.status
    except _UsageError as exc:
        with localized_argparse():
            _print_usage_error(exc)
        log.info("usage error: %s (arguments: %s)", exc.message, raw)
        return EXIT_USER
    if args.lang:
        set_language(args.lang)
    quiet = bool(args.quiet)
    verbose = int(args.verbose or 0)
    if verbose:
        log_file = _start_logging(verbose) or log_file
    try:
        from . import log as applog

        applog.install_excepthooks()
    except Exception:  # pragma: no cover
        pass
    backend_note = _apply_backend(args.backend)
    token = cancel if cancel is not None else CancelToken()
    console = _Console(quiet=quiet, verbose=verbose)
    console.attach_log_filter()
    interrupts = _Interrupts(token)
    log.info("jsonreshape %s: %s", __version__, " ".join(raw))
    rc = EXIT_INTERNAL
    try:
        interrupts.install()
        rc = _run(args, console, token, log_file, backend_note)
    except KeyboardInterrupt:  # a second Ctrl+C outside the command itself
        console.err(t("error.cancelled"))
        rc = EXIT_CANCELLED
    finally:
        interrupts.restore()
        if args.command is not None and not args.version:
            _memory_warning(console, args, peak_before)
        console.finish()
        log.info("exit code %d after %.2f s", rc, time.perf_counter() - started)
        if verbose:
            _start_logging(0)  # remove the console handler again (in-process callers)
    if sys.stdout is not None and sys.stdout is sys.__stdout__:
        try:
            sys.stdout.flush()
        except (OSError, ValueError):
            # the reader went away: silence the flush at interpreter exit as well
            try:
                devnull = os.open(os.devnull, os.O_WRONLY)
                os.dup2(devnull, sys.stdout.fileno())
            except (OSError, ValueError):  # pragma: no cover
                pass
    return rc
