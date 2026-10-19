"""Editing widgets and dialogs (docs/ARCHITECTURE.md §12.9, ``editors.py``).

Everything here only collects and validates the user's input; the edit itself is made by the caller
(``session.edit.set_value(ref, json_text)``, ``rename_key``, ``insert`` ...). Values are exact: numbers keep
the token the user typed, strings are encoded with :func:`jsonreshape.editops.encode_string`, nothing is ever
taken from the abbreviated *display* text of a cell.

Public API
==========
``InlineEditor(grid, row, col, kind, text, on_commit, on_cancel=None, *, commit_on_focus_out=True,
taken=None, inset=None, select_all=True, show=True, key_check=None)``
    A line edit placed over the cell ``(row, col)`` of a :class:`~jsonreshape.gui.vgrid.VirtualGrid`
    (``grid.cell_rect``; a child of ``grid.viewport()``, following scrolling and column changes). ``kind``:

    * ``"string"`` -- ``text`` is the DECODED string; ``on_commit`` receives the JSON string token
      (``encode_string(new)`` as ``str``);
    * ``"number"`` -- ``text`` is the exact number token; live validation with the JSON number grammar;
      ``on_commit`` receives the token exactly as typed (outer whitespace removed: ``1.10`` stays ``1.10``);
    * ``"key"`` -- ``text`` is the current key; ``on_commit`` receives the new key itself (not JSON); it must
      be non-empty and, when ``taken`` (a container of the sibling keys) is given, not one of them;
    * ``"json"`` -- any one JSON value (``editops.check_json``); ``on_commit`` receives the validated text.

    Enter commits, Esc cancels, focus-out commits (``commit_on_focus_out=False``: cancels; an invalid text is
    discarded on focus-out; a context menu or a window switch is not a focus-out). Every other key stays with the
    editor (a key the line edit does not use never reaches the grid's commands). Committing an UNCHANGED
    value is a cancel (``on_cancel`` -- no edit, no step). Strings / keys that a line edit cannot keep exactly
    (Qt drops unpaired surrogates) or that hold line breaks are shown ESCAPED (the content of a JSON string
    token: ``\\n``, ``\\"``, ``\\ud800`` ...) and parsed back on commit. Signals ``committed(str)``,
    ``cancelled()``, ``validity_changed(bool)``; methods ``commit() -> bool``, ``cancel()``, ``is_valid()``,
    ``error_text()``, ``value_text()``; attributes ``escaped``, ``original``. The editor deletes itself when it
    is done and gives the focus back to the grid (unless the focus went elsewhere). ``inset``: pixels to skip
    at the left of the cell (default: the indentation and badge of the tree column are skipped).

``StringDialog(parent=None, text="", *, title=None, on_accept=None, loading=False)``
    Multi-line plain-text editor for a DECODED string up to :data:`STRING_DIALOG_MAX` (4 MiB of UTF-8; a
    bigger text puts the dialog in the *refused* state: a message pointing to Bulk replace / JSON Patch and a
    Close button). Wrap toggle, "show special characters as escapes" toggle, character / line count, OK /
    Cancel. Line endings are kept: a text whose line breaks are all ``\\r\\n`` (or all ``\\n``, or all ``\\r``)
    is edited as lines and written back with the same line ending; a text the plain editor cannot keep
    exactly (mixed line endings, unpaired surrogates, U+2029 / U+FDD0 / U+FDD1, which Qt turns into
    paragraph breaks) is shown escaped (with a note). On OK with a CHANGED value: ``value_accepted(str)``
    (the JSON string token) and ``on_accept(token)``. ``value()`` (decoded; ``text()`` is the same),
    ``json_text()``, ``set_text(text)``, ``set_escaped(on)``, ``set_wrap(on)``, ``discard()`` (close without
    asking; Cancel / Esc ask before dropping changes), ``refused``, ``loading``, ``loaded_gen``.
    ``StringDialog.for_node(session, ref, *, parent=None, title=None, on_accept=None, show=True)`` -- the
    *dialog loader*: opens at once in the loading state and reads the string of ``ref`` in a silent task
    (pinned document; refused above the limit without reading it).

``JsonEditDialog(parent=None, text="", *, title=None, indent=2, tasks=None, on_accept=None, loading=False)``
    QPlainTextEdit with live validation, debounced :data:`VALIDATE_DELAY_MS`: in the GUI thread up to
    :data:`JSON_SYNC_MAX` (256 KiB of UTF-8), above in a background task of ``tasks`` (a
    :class:`~jsonreshape.gui.qtasks.TaskManager`; default: a private one) tagged with the text generation --
    stale results are dropped; at most one check runs at a time (the newest text is checked when it ends).
    The status line shows the error line / column / reason and a red wavy underline marks the error
    position (``EditError.char_offset`` code points -> UTF-16 positions, correct with Cyrillic and emoji).
    OK (and Ctrl+Enter) only while the current text is valid; indent combo (2 spaces / 4 spaces / tab) and
    Format (token-level ``core.Reformatter``: numbers and strings byte-identical; in a task above 256 KiB). Texts up to :data:`JSON_DIALOG_MAX` (4 MiB; bigger: refused). On OK with a CHANGED text:
    ``value_accepted(str)`` and ``on_accept(text)`` with the JSON text as typed. ``text()``, ``is_valid()``,
    ``indent()``, ``set_indent()``, ``format_text()``, ``set_text()``, ``discard()``, ``validation_path``
    (``"gui"`` | ``"task"``, the last validation), ``validated(bool)`` signal.
    ``JsonEditDialog.for_node(session, ref, *, parent=None, title=None, indent=2, on_accept=None, show=True)``
    loads the node's EFFECTIVE text (``doc.node_chunks`` of a pinned document, composite nodes too)
    pretty-printed with ``indent``, refusing nodes whose pretty text exceeds 4 MiB.

``KeyDialog(parent=None, key="", *, title=None, taken=None, on_accept=None, label=None, key_check=None)``
    The new name of an object member / record key: non-empty, not in ``taken``; escaped like the inline
    editor when needed. On OK with a changed key: ``key_accepted(str)`` / ``on_accept(key)``; ``key()``
    (``text()`` is the same).

``InsertDialog(parent=None, *, title=None, positions=INSERT_POSITIONS, position=None, key_needed=False,
taken=None, key=None, value_type="string", value=None, on_accept=None, key_check=None)``
    Position radio (a subset of ``before`` / ``after`` / ``child``), a key field shown when the position needs
    one (``key_needed``: a bool or ``{position: bool}`` -- object parents, the record level of keyed
    documents), ``taken`` (a container of existing keys, or ``{position: container}``), a type combo
    (:data:`INSERT_TYPES`: string, number, bool, null, object, array, json) with a value editor per type and the
    same validation as above. On OK: ``insert_accepted(InsertResult)`` / ``on_accept(result)`` --
    :class:`InsertResult` ``(position, key, value, type)``, ``value`` being JSON text (``{}`` / ``[]`` for the
    container types). ``result()`` returns it (``EditError`` while invalid).

Key checks (``key_check``; review3 L1)
    ``taken`` is a synchronous container: the views pass what they know without I/O (the loaded siblings).
    ``key_check`` (:data:`KeyCheck`: ``check(key, reply)``; for :class:`InsertDialog` also
    ``{position: check}``) asks the view whether a key exists in the container the new key goes into -- the view
    looks it up in a background task and calls ``reply(taken)`` in the GUI thread. The editors ask only when a
    changed key is committed (Enter / focus-out / OK) and its answer is not known yet: the editor stays open
    meanwhile (the inline editor's tooltip, the dialogs' note say "checking", OK is disabled), then commits a
    free key, or shows "this key exists already" in place (no error dialog). Answers are kept per key until
    ``forget_key_checks()`` (the view calls it when the data changed; a running lookup is asked again).
    ``checking`` tells whether a lookup is running; a check that raises counts as "free" (the edit session
    still refuses a duplicate).

``type_menu(parent, current_kind, on_pick, *, title=None) -> QMenu``
    "Change type" menu: the six JSON types; the current type (a single kind) is checked and disabled;
    conversions that §12.4 always refuses (object/array -> number/bool; number/bool/array -> object) are
    disabled with the reason when ANY of the given kinds (``str`` or an iterable of kinds) cannot be
    converted (``EditSession.change_type`` refuses the whole command then). ``on_pick(kind)``.
    ``type_refusal(current_kind, target) -> str | None`` is that rule for one target (the node menus of
    :mod:`jsonreshape.gui.actions` use it too).

``load_edit_value(session, ref, on_done, *, owner=None, on_error=None, max_bytes=EDIT_VALUE_MAX) -> GuiTask``
    Silent task reading ``EditValue(kind, token, decoded, size, gen)`` of a node for an editor (scalars up to
    ``max_bytes``: the exact token and, for strings, the decoded text; containers and bigger values:
    ``token``/``decoded`` ``None``). Errors go to ``on_error`` (default: ``app.report_error``).

Helpers: :func:`inline_editable` (the views' rule: no line break, at most :data:`INLINE_MAX_CHARS`
characters), :func:`is_json_number`, :func:`escape_text` / :func:`unescape_text`, :func:`utf16_offset`,
:func:`error_span`.

Decisions (where §12.9 leaves room)
===================================
* Committing an unchanged value is a cancel (no step, no dirty flag); the dialogs emit their
  ``*_accepted`` signals only for a changed value (``QDialog.accepted`` still fires on OK). Besides saving a
  round trip, this keeps untouched values byte-identical: re-encoding an unchanged decoded string would
  normalize its escapes (a token written with ``\\u00e9`` would become ``é``) and create a step.
* An :class:`InlineEditor` asked for a cell that does not exist (row gone, column hidden) is cancelled from
  the event loop right after the constructor returned (the caller holds the editor before ``on_cancel``).
* Exactness over convenience: anything a Qt text widget cannot hold exactly (unpaired surrogates are
  silently dropped by PySide; ``QPlainTextEdit`` turns CR / CRLF / U+2029 / U+FDD0 / U+FDD1 into paragraph
  breaks and ``toPlainText()`` also turns U+2028 into a line feed and NBSP into a space) is edited in the
  escaped form; plain texts are read with ``toRawText()``. JSON texts given to :class:`JsonEditDialog` have
  raw U+2028 / U+2029 / U+FDD0 / U+FDD1 (and unpaired surrogates) replaced by their ``\\uXXXX`` escapes
  (the same JSON value).
* A new line typed into a string that had no line break is ``\\n``.
* An invalid inline text is discarded on focus-out (never committed); Cancel / Esc / the window's close
  button ask before dropping changes made in the multi-line dialogs (``widgets.ask``; tests answer through
  ``widgets.confirm_handler``); :meth:`discard` closes without asking.
* The 4 MiB limits count UTF-8 bytes (of the decoded string, of the pretty JSON text). The string loader
  refuses a token larger than the limit without reading it (its decoded text can only be smaller, so an
  escape-heavy string slightly above the limit is refused too).
* The dialogs are window-modal when opened with ``open()`` (the loaders do that); nothing here runs a nested
  event loop.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from typing import Any, Callable, Container, Iterable, Mapping, Sequence

import shiboken6
from PySide6.QtCore import QEvent, QPoint, Qt, QTimer, Signal
from PySide6.QtGui import QColor, QFocusEvent, QKeyEvent, QTextCharFormat, QTextCursor
from PySide6.QtWidgets import (
    QButtonGroup,
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMenu,
    QPlainTextEdit,
    QPushButton,
    QRadioButton,
    QStackedWidget,
    QTextEdit,
    QToolTip,
    QVBoxLayout,
    QWidget,
)

from jsonreshape import codec, core, editops
from jsonreshape.errors import EditError, JsonReshapeError, UserInputError
from jsonreshape.gui import theme
from jsonreshape.i18n import t
from jsonreshape.tasks import format_bytes

log = logging.getLogger(__name__)

__all__ = [
    "EDITOR_KINDS",
    "EDIT_VALUE_MAX",
    "INDENTS",
    "INLINE_MAX_CHARS",
    "INSERT_POSITIONS",
    "INSERT_TYPES",
    "JSON_DIALOG_MAX",
    "JSON_SYNC_MAX",
    "MENU_TYPES",
    "STRING_DIALOG_MAX",
    "VALIDATE_DELAY_MS",
    "EditValue",
    "InlineEditor",
    "InsertDialog",
    "InsertResult",
    "JsonEditDialog",
    "KeyCheck",
    "KeyDialog",
    "StringDialog",
    "error_span",
    "escape_text",
    "inline_editable",
    "is_json_number",
    "load_edit_value",
    "type_menu",
    "type_refusal",
    "unescape_text",
    "utf16_offset",
]

#: ``check(key, reply)``: the view looks ``key`` up in the container the new key goes into (in a background task)
#: and calls ``reply(taken: bool)`` in the GUI thread (module docstring, "Key checks").
KeyCheck = Callable[[str, Callable[[bool], None]], Any]

KiB = 1 << 10
MiB = 1 << 20

STRING_DIALOG_MAX = 4 * MiB      # UTF-8 bytes of a string edited in StringDialog
JSON_DIALOG_MAX = 4 * MiB        # UTF-8 bytes of the (pretty) text edited in JsonEditDialog
JSON_SYNC_MAX = 256 * KiB        # JsonEditDialog: validated / formatted in the GUI thread up to this size
VALIDATE_DELAY_MS = 200          # debounce of the live validation
INLINE_MAX_CHARS = 200           # the views use the line editor up to this many characters (no line break)
EDIT_VALUE_MAX = 64 * KiB        # load_edit_value: scalar tokens read up to this size
EDITOR_KINDS = ("string", "number", "key", "json")
INDENTS: tuple[int | str, ...] = (2, 4, "\t")
INSERT_POSITIONS = ("before", "after", "child")
INSERT_TYPES = ("string", "number", "bool", "null", "object", "array", "json")
MENU_TYPES = ("string", "number", "bool", "null", "object", "array")

_LINE_MAX = 1 << 30              # QLineEdit.maxLength: the default 32767 would silently cut longer texts
_NUMBER_RE = re.compile(r"-?(?:0|[1-9][0-9]*)(?:\.[0-9]+)?(?:[eE][+-]?[0-9]+)?")
_JSON_WS = " \t\r\n"
# line boundaries of str.splitlines(): a line edit hides them, so such texts are edited escaped
_LINE_BREAK_RE = re.compile("[\n\r\v\f\x1c\x1d\x1e\x85\U00002028\U00002029]")
_SURROGATE_RE = re.compile("[\ud800-\udfff]")
# characters QPlainTextEdit turns into paragraph breaks (besides CR and LF)
_BLOCK_CHAR_RE = re.compile("[\U00002029\U0000fdd0\U0000fdd1]")
# invisible or special characters written as \uXXXX in the escaped form (all valid JSON escapes)
_INVISIBLE_RE = re.compile("[\x7f-\x9f\xad\U0000061c\U0000180e\U0000200b-\U0000200f\U00002028-\U0000202e"
                           "\U00002060-\U0000206f\U0000fdd0-\U0000fdef\U0000feff\U0000fff0-\U0000ffff]")
# raw characters of a JSON text that QPlainTextEdit cannot keep (escaped: same JSON value inside strings)
_JSON_SANITIZE_RE = re.compile("[\U00002028\U00002029\U0000fdd0\U0000fdd1\ud800-\udfff]")
_ASTRAL_RE = re.compile("[\U00010000-\U0010ffff]")
_STRING_BODY_RE = re.compile(r'(?:[^"\\]+|\\.)*', re.S)    # a JSON string body up to an unescaped quote
_PARAGRAPH = "\N{PARAGRAPH SEPARATOR}"      # QTextDocument.toRawText() block separator

# conversions that editops.convert_value always refuses (source, target)
_IMPOSSIBLE = frozenset({
    ("object", "number"), ("array", "number"), ("object", "bool"), ("array", "bool"),
    ("number", "object"), ("bool", "object"), ("array", "object"),
})


# ======================================================================================
# text helpers (pure)
# ======================================================================================
def is_json_number(text: str) -> bool:
    """``text`` (outer JSON whitespace ignored) is one JSON number token (RFC 8259 grammar)."""
    return _NUMBER_RE.fullmatch(str(text).strip(_JSON_WS)) is not None


def has_line_break(text: str) -> bool:
    """``text`` holds a line boundary (any of ``str.splitlines()``)."""
    return _LINE_BREAK_RE.search(text) is not None


def inline_editable(text: str) -> bool:
    """The views' rule (§12.9): a decoded string is edited with :class:`InlineEditor` when it has no line
    break and at most :data:`INLINE_MAX_CHARS` characters, else with :class:`StringDialog`."""
    return len(text) <= INLINE_MAX_CHARS and not has_line_break(text)


def _needs_escape_line(value: str) -> bool:
    """A one-line editor cannot show ``value`` exactly (unpaired surrogates, line breaks)."""
    return _SURROGATE_RE.search(value) is not None or has_line_break(value)


def _u_escape(m: re.Match[str]) -> str:
    return f"\\u{ord(m.group()):04x}"


def escape_text(s: str) -> str:
    """``s`` as the content of a JSON string token (no quotes): control characters, quotes, backslashes and
    unpaired surrogates escaped (codec rules), plus invisible / separator characters as ``\\uXXXX``."""
    inner = editops.encode_string(str(s)).decode("utf-8")[1:-1]
    return _INVISIBLE_RE.sub(_u_escape, inner)


def unescape_text(text: str) -> str:
    """The string whose JSON token content is ``text`` (inverse of :func:`escape_text`).

    ``EditError("gui.editors.error.escaped", column, reason)`` when ``text`` is not a valid string body; its
    ``char_offset`` is the code point of ``text`` where the problem is."""
    text = str(text)
    m = _STRING_BODY_RE.match(text)
    end = m.end() if m is not None else 0
    if end < len(text) and text[end] == '"':
        raise EditError("gui.editors.error.escaped", column=end + 1, reason=t("gui.editors.error.quote"),
                        char_offset=end)
    try:
        token = editops.check_json('"' + text + '"')
    except EditError as exc:
        off = max(0, min(len(text), (exc.char_offset or 0) - 1))
        raise EditError("gui.editors.error.escaped", column=off + 1, reason=exc.params.get("reason", ""),
                        char_offset=off) from None
    value = codec.loads(token)
    if not isinstance(value, str):  # pragma: no cover - a quoted body is always a string
        raise EditError("gui.editors.error.escaped", column=1, reason="", char_offset=0)
    return value


def utf16_offset(text: str, index: int) -> int:
    """The UTF-16 position (what ``QTextCursor`` / ``QString`` count) of code point ``index`` of ``text``."""
    index = max(0, min(len(text), int(index)))
    return index + len(_ASTRAL_RE.findall(text, 0, index))


def error_span(text: str, char_offset: int) -> tuple[int, int]:
    """UTF-16 ``[start, end)`` of the character to underline for an error at code point ``char_offset`` of
    ``text``: that character, or -- at a line end / the end of the text -- the last character before it
    (an underline on a line break would be invisible). ``(0, 0)`` when there is nothing to underline."""
    n = len(text)
    i = max(0, min(n, int(char_offset)))
    if i < n and text[i] != "\n":
        j = i + 1
    else:
        while i > 0 and text[i - 1] == "\n":
            i -= 1
        if i == 0:
            return (0, 0)
        j = i
        i -= 1
    return utf16_offset(text, i), utf16_offset(text, j)


def _utf8_len(text: str) -> int:
    return len(text.encode("utf-8", "surrogatepass"))


def _newline_of(value: str) -> str | None:
    """The one line ending ``value`` uses (``"\\n"`` when it has none); ``None`` when it mixes them."""
    crlf = value.count("\r\n")
    cr = value.count("\r") - crlf
    lf = value.count("\n") - crlf
    styles = [s for s, n in (("\r\n", crlf), ("\r", cr), ("\n", lf)) if n]
    if len(styles) > 1:
        return None
    return styles[0] if styles else "\n"


def _plain_problems(value: str) -> list[str]:
    """Why the multi-line plain editor cannot keep ``value`` exactly (localized; empty = it can)."""
    out = []
    if _SURROGATE_RE.search(value) is not None:
        out.append(t("gui.editors.special.surrogates"))
    if _BLOCK_CHAR_RE.search(value) is not None:
        out.append(t("gui.editors.special.separators"))
    if _newline_of(value) is None:
        out.append(t("gui.editors.special.mixed_newlines"))
    return out


def _line_count(value: str) -> int:
    return value.count("\n") + value.count("\r") - value.count("\r\n") + 1


def _sanitize_json_text(text: str) -> str:
    return _JSON_SANITIZE_RE.sub(_u_escape, text)


def _kind_of_token(data: bytes) -> str:
    """JSON type of a validated value (its first byte)."""
    if not data:
        return "null"
    return {0x7B: "object", 0x5B: "array", 0x22: "string", 0x74: "bool", 0x66: "bool", 0x6E: "null"}.get(
        data[0], "number")


def _indent_arg(indent: Any) -> int | str:
    if indent == "\t" or indent == "tab":
        return "\t"
    try:
        value = int(indent)
    except (TypeError, ValueError):
        return 2
    return value if 0 <= value <= 16 else 2


def _reformat(data: bytes, indent: int | str | None) -> bytes:
    """Token-level reformatting (``core.Reformatter``): strings, numbers and literals byte-identical."""
    fmt = core.Reformatter(indent=indent)
    if len(data) <= MiB:
        return fmt.feed(data) + fmt.finish()
    view = memoryview(data)
    parts = [fmt.feed(view[i:i + MiB]) for i in range(0, len(data), MiB)]
    parts.append(fmt.finish())
    return b"".join(parts)


def _type_label(kind: str | None) -> str:
    return t(f"gui.editors.type.{kind}") if kind in INSERT_TYPES else str(kind or "")


def _danger() -> QColor:
    return QColor(theme.color("danger"))


def _mono(widget: QWidget | None) -> Any:
    from jsonreshape.gui.widgets import mono_font

    return mono_font(getattr(widget, "session", None))


def _alive(obj: Any) -> bool:
    try:
        return obj is not None and shiboken6.isValid(obj)
    except Exception:  # pragma: no cover
        return False


def _button_box(dialog: QDialog) -> tuple[QDialogButtonBox, QPushButton, QPushButton]:
    box = QDialogButtonBox(dialog)
    ok = box.addButton(t("gui.common.ok"), QDialogButtonBox.ButtonRole.AcceptRole)
    cancel = box.addButton(t("gui.common.cancel"), QDialogButtonBox.ButtonRole.RejectRole)
    ok.setDefault(True)
    box.accepted.connect(dialog.accept)
    box.rejected.connect(dialog.reject)
    return box, ok, cancel


def _error_label(parent: QWidget) -> QLabel:
    label = QLabel(parent)
    label.setWordWrap(True)
    label.setTextFormat(Qt.TextFormat.PlainText)
    label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
    theme.bind_style(label, "color: $danger;")
    label.setVisible(False)
    return label


def _set_error(label: QLabel, text: str) -> None:
    from jsonreshape.gui import copyfmt

    label.setText(copyfmt.escape_surrogates(text or ""))
    label.setVisible(bool(text))


def _message(exc: BaseException) -> str:
    if isinstance(exc, JsonReshapeError):
        hint = exc.hint
        return f"{exc.message}\n{hint}" if hint else exc.message
    return t("gui.editors.load_failed", error=str(exc) or type(exc).__name__)


class _LineValue:
    """How a one-line edit shows a string exactly: plain, or escaped (:func:`escape_text`) when the plain
    text could not be kept (unpaired surrogates) or would hide line breaks."""

    __slots__ = ("escaped",)

    def __init__(self, value: str, *, force: bool = False) -> None:
        self.escaped = force or _needs_escape_line(value)

    def shown(self, value: str) -> str:
        return escape_text(value) if self.escaped else value

    def decode(self, text: str) -> str:
        return unescape_text(text) if self.escaped else text


def _key_problem(key: str, original: str | None, taken: Container[str] | None) -> str:
    if key == "":
        return t("gui.editors.error.key_empty")
    if taken is not None and key != original:
        try:
            exists = key in taken
        except TypeError:  # pragma: no cover - an odd container
            exists = False
        if exists:
            return t("gui.editors.error.key_exists", key=key)
    return ""


class _KeyAnswers:
    """The answers of an editor's asynchronous key check (module docstring, "Key checks"): ``taken(key)`` -> True /
    False / ``None`` (not asked, or on its way); :meth:`ask` runs ONE lookup at a time -- ``done(key)`` follows an
    answer that arrives later (a synchronous answer is simply there when :meth:`ask` returns); :meth:`forget` drops
    the answers (the data changed) and returns the key whose lookup was running."""

    __slots__ = ("_answers", "_closed", "_serial", "asking")

    def __init__(self, closed: Callable[[], bool]) -> None:
        self._answers: dict[Any, bool] = {}
        self._closed = closed
        self._serial = 0
        self.asking: Any = None          # the key (answer key) being looked up

    def taken(self, key: Any) -> bool | None:
        return self._answers.get(key)

    def ask(self, check: KeyCheck, key: Any, text: str, done: Callable[[Any], Any]) -> None:
        if self.asking == key:
            return
        self.asking = key
        self._serial += 1
        serial = self._serial
        sync = [True]

        def reply(taken: bool) -> None:
            if serial != self._serial or self._closed():
                return
            self.asking = None
            self._answers[key] = bool(taken)
            if not sync[0]:
                done(key)

        try:
            check(text, reply)
        except Exception:  # noqa: BLE001 - a failing lookup: the edit session still checks the key
            log.exception("key check failed")
            if self.asking == key and serial == self._serial:
                reply(False)
        finally:
            sync[0] = False

    def forget(self) -> Any:
        self._answers.clear()
        self._serial += 1
        key, self.asking = self.asking, None
        return key


class _TakenOrFound:
    """``taken`` of an editor plus the keys its key check found."""

    __slots__ = ("answers", "taken", "wrap")

    def __init__(self, taken: Container[str] | None, answers: _KeyAnswers, wrap: Callable[[str], Any] = lambda k: k):
        self.taken = taken
        self.answers = answers
        self.wrap = wrap

    def __contains__(self, key: object) -> bool:
        if self.answers.taken(self.wrap(key)):     # type: ignore[arg-type]
            return True
        try:
            return self.taken is not None and key in self.taken
        except TypeError:  # pragma: no cover - an odd container
            return False


# ======================================================================================
# InlineEditor
# ======================================================================================
class InlineEditor(QLineEdit):
    """A line edit over one cell of a VirtualGrid (module docstring)."""

    committed = Signal(str)
    cancelled = Signal()
    validity_changed = Signal(bool)

    def __init__(self, grid: Any, row: int, col: int, kind: str, text: str,
                 on_commit: Callable[[str], Any] | None = None, on_cancel: Callable[[], Any] | None = None, *,
                 commit_on_focus_out: bool = True, taken: Container[str] | None = None, inset: int | None = None,
                 select_all: bool = True, show: bool = True, key_check: KeyCheck | None = None) -> None:
        if kind not in EDITOR_KINDS:
            raise ValueError(f"unknown editor kind: {kind!r}")
        super().__init__(grid.viewport())
        self.setObjectName("InlineEditor")
        self.grid = grid
        self.row = int(row)
        self.col = int(col)
        self.kind = kind
        self.original = str(text)
        self.commit_on_focus_out = bool(commit_on_focus_out)
        self._on_commit = on_commit
        self._on_cancel = on_cancel
        self._key_check = key_check if kind == "key" else None
        self._keys = _KeyAnswers(lambda: self._done or not _alive(self))
        self._taken = _TakenOrFound(taken, self._keys)
        self._inset = inset
        self._done = False
        self._valid = True
        self._error = ""
        self._codec = _LineValue(self.original) if kind in ("string", "key") else None
        self.escaped = bool(self._codec is not None and self._codec.escaped)
        self.setMaxLength(_LINE_MAX)
        self.setFrame(False)
        try:
            self.setFont(grid.grid_font())
        except Exception:
            log.debug("grid font unavailable", exc_info=True)
        self.setText(self._codec.shown(self.original) if self._codec is not None else self.original)
        self._hint = t("gui.editors.escaped.tip") if self.escaped else ""
        self.setToolTip(self._hint)
        self.textChanged.connect(self._validate)
        self._connected: list[Any] = []
        for name in ("viewport_changed", "header_geometry_changed", "font_changed", "columns_changed"):
            signal = getattr(grid, name, None)
            if signal is not None:
                try:
                    signal.connect(self._place)
                    self._connected.append(signal)
                except (RuntimeError, TypeError):  # pragma: no cover - a fake grid
                    pass
        self._validate()
        self._apply_style()
        if not self._place(initial=True):
            # no such cell (any more): cancelled from the event loop, so the caller has the editor first
            QTimer.singleShot(0, self.cancel)
            return
        if show:
            self.show()
            self.raise_()
            self.setFocus(Qt.FocusReason.OtherFocusReason)
            if select_all:
                self.selectAll()

    # -- state ------------------------------------------------------------------------------
    def is_valid(self) -> bool:
        return self._valid

    def error_text(self) -> str:
        """The current validation message (``""`` when valid)."""
        return self._error

    @property
    def done(self) -> bool:
        """Committed or cancelled (the widget is being deleted)."""
        return self._done

    def _decoded(self) -> str:
        assert self._codec is not None
        return self._codec.decode(self.text())

    def _check(self) -> str:
        """The validation message of the current text (``""`` = valid)."""
        text = self.text()
        kind = self.kind
        if kind == "number":
            return "" if is_json_number(text) else t("gui.editors.error.number")
        if kind == "json":
            try:
                editops.check_json(text)
            except EditError as exc:
                return exc.message
            return ""
        try:
            value = self._decoded()
        except EditError as exc:
            return exc.message
        if kind == "key":
            return _key_problem(value, self.original, self._taken)
        return ""

    def _validate(self, *_args: Any) -> None:
        if self._done:
            return
        error = self._check()
        valid = not error
        self._error = error
        changed = valid != self._valid
        self._valid = valid
        self._apply_style()
        if valid:
            self.setToolTip(self._hint)
            if changed:
                QToolTip.hideText()
        else:
            self.setToolTip(error)
            if self.isVisible():
                QToolTip.showText(self.mapToGlobal(QPoint(0, self.height())), error, self)
        if changed:
            self.validity_changed.emit(valid)

    def _apply_style(self) -> None:
        border = "1px solid $accent" if self._valid else "2px solid $danger"
        theme.bind_style(self, "QLineEdit#InlineEditor { border: " + border + "; padding: 0px 2px; }")

    def is_unchanged(self) -> bool:
        """The text means the original value (committing it is a cancel)."""
        text = self.text()
        if self.kind in ("number", "json"):
            return text.strip(_JSON_WS) == self.original.strip(_JSON_WS)
        try:
            return self._decoded() == self.original
        except EditError:
            return False

    def value_text(self) -> str:
        """What committing delivers (module docstring); ``EditError`` when the text is not valid."""
        text = self.text()
        kind = self.kind
        if kind == "number":
            token = text.strip(_JSON_WS)
            if not is_json_number(token):
                raise EditError("gui.editors.error.number")
            return token
        if kind == "json":
            return editops.check_json(text).decode("utf-8", "surrogatepass")
        value = self._decoded()
        if kind == "key":
            problem = _key_problem(value, self.original, self._taken)
            if problem:
                raise EditError("gui.editors.error.key_invalid", reason=problem)
            return value
        return editops.encode_string(value).decode("utf-8")

    @property
    def checking(self) -> bool:
        """A key check (``key_check``) is on its way; the commit follows its answer."""
        return self._keys.asking is not None

    def _tip(self, text: str) -> None:
        if self.isVisible() and text:
            QToolTip.showText(self.mapToGlobal(QPoint(0, self.height())), text, self)

    # -- commit / cancel ----------------------------------------------------------------------
    def commit(self) -> bool:
        """Commit the text (Enter): False (the editor stays open) when it is not valid -- or while the key
        check (``key_check``) of a new key is on its way: the commit follows a "free" answer, a "taken" one
        shows the error in place. An unchanged value is a cancel."""
        if self._done:
            return False
        self._validate()
        if not self._valid:
            self._tip(self._error)
            return False
        if self.is_unchanged():
            self._finish(None)
            return True
        try:
            value = self.value_text()
        except EditError as exc:  # pragma: no cover - _validate checked the same
            self._error = exc.message
            return False
        if self._key_check is not None:
            answer = self._keys.taken(value)
            if answer is None:
                self._keys.ask(self._key_check, value, value, self._key_answered)
                answer = self._keys.taken(value)       # a synchronous answer
            if answer is None:
                self.setToolTip(t("gui.editors.key.checking"))
                self._tip(t("gui.editors.key.checking"))
                return False
            if answer:
                self._validate()                      # "exists" now
                self._tip(self._error)
                return False
        self._finish(value)
        return True

    def _key_answered(self, key: Any) -> None:
        """A key check answered after :meth:`commit` asked: commit a free key that is still typed."""
        if self._done:
            return
        self._validate()
        try:
            current = self._decoded()
        except EditError:
            return
        if current != key:
            return                                    # typed on meanwhile: the user commits again
        if self._keys.taken(key):
            self._tip(self._error)
        else:
            self.commit()

    def forget_key_checks(self) -> None:
        """The data changed: drop the key check answers (a lookup on its way is asked again)."""
        key = self._keys.forget()
        if self._done:
            return
        self._validate()
        if key is not None and self._key_check is not None:
            self._keys.ask(self._key_check, key, key, self._key_answered)
            if self._keys.taken(key) is not None:
                self._key_answered(key)

    def cancel(self) -> None:
        """Close without a change (Esc)."""
        if not self._done:
            self._finish(None)

    def _finish(self, value: str | None) -> None:
        self._done = True
        refocus = self.hasFocus()
        for signal in self._connected:
            try:
                signal.disconnect(self._place)
            except (RuntimeError, TypeError):
                pass
        self._connected = []
        QToolTip.hideText()
        self.hide()
        grid = self.grid
        if refocus and _alive(grid):
            grid.setFocus(Qt.FocusReason.OtherFocusReason)
        try:
            if value is None:
                self.cancelled.emit()
                if self._on_cancel is not None:
                    self._on_cancel()
            else:
                self.committed.emit(value)
                if self._on_commit is not None:
                    self._on_commit(value)
        finally:
            self._on_commit = self._on_cancel = None
            self.deleteLater()

    # -- geometry -------------------------------------------------------------------------------
    def _left_inset(self, rect: Any) -> int:
        if self._inset is not None:
            return max(0, int(self._inset))
        grid = self.grid
        try:
            if self.col != grid.tree_column():
                return 0
            # the public text area of the cell: right of the indentation, the expander, the badge, a swatch
            tr = grid.text_rect(self.row, self.col)
            if tr.isNull():
                return 0
            return max(0, tr.x() - rect.x() - 3)
        except Exception:
            return 0

    def _place(self, *_args: Any, initial: bool = False) -> bool:
        """Put the editor over its cell; a row that went away / a hidden column cancels the edit (False)."""
        if self._done:
            return False
        grid = self.grid
        if not _alive(grid):
            return False
        try:
            rows = grid.row_count()
        except Exception:
            rows = self.row + 1
        rect = grid.cell_rect(self.row, self.col) if self.row < rows else None
        if rect is None or rect.isNull():
            if not initial:
                self.cancel()
            return False
        left = rect.x() + self._left_inset(rect)
        vw = grid.viewport().width()
        want = self.fontMetrics().horizontalAdvance(self.text()[:500]) + 24
        width = max(rect.right() + 1 - left, min(want, max(0, vw - left)), 48)
        self.setGeometry(left, rect.y(), width, rect.height())
        return True

    # -- events ---------------------------------------------------------------------------------
    def event(self, event: QEvent) -> bool:  # noqa: D401 - Qt name
        if event.type() == QEvent.Type.ShortcutOverride and isinstance(event, QKeyEvent):
            if event.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter, Qt.Key.Key_Escape):
                event.accept()      # no window shortcut takes Enter / Esc from the editor
                return True
        return super().event(event)

    def keyPressEvent(self, event: QKeyEvent) -> None:  # noqa: N802 - Qt name
        key = event.key()
        if key in (Qt.Key.Key_Return, Qt.Key.Key_Enter):
            event.accept()
            self.commit()
            return
        if key == Qt.Key.Key_Escape:
            event.accept()
            self.cancel()
            return
        super().keyPressEvent(event)
        # the editor keeps every key: one the line edit does not use would bubble up to the grid
        # and run its commands (F2 / Ins / Ctrl+D / Alt+arrows ...) or move its current row
        event.accept()

    def focusOutEvent(self, event: QFocusEvent) -> None:  # noqa: N802 - Qt name
        super().focusOutEvent(event)
        if self._done:
            return
        if event.reason() in (Qt.FocusReason.PopupFocusReason, Qt.FocusReason.ActiveWindowFocusReason):
            return                  # the line edit's context menu / another window: still editing
        if self.commit_on_focus_out and self._valid:
            self.commit()
        else:
            self.cancel()


# ======================================================================================
# shared dialog machinery
# ======================================================================================
@dataclass(frozen=True)
class _Check:
    ok: bool
    kind: str | None = None
    error: EditError | None = None
    span: tuple[int, int] | None = None


def _check_json_text(text: str, data: bytes) -> _Check:
    """Validate ``data`` (``text`` encoded); the error span is computed on ``text`` (worker-safe)."""
    try:
        value = editops.check_json(data)
    except EditError as exc:
        return _Check(False, None, exc, error_span(text, exc.char_offset or 0))
    return _Check(True, _kind_of_token(value))


class _DialogBase(QDialog):
    """Common parts of the editing dialogs: refused / loading states, task ownership, the "discard
    changes?" question on Cancel."""

    def __init__(self, parent: QWidget | None, title: str) -> None:
        super().__init__(parent)
        self.setWindowTitle(title)
        self.setSizeGripEnabled(True)
        self.refused = False
        self.loading = False
        self.loaded_gen: int | None = None
        self._asking = False
        self._closed_flag = False          # done() ran (accepted / rejected)
        self._managers: list[Any] = []
        self._own_tasks: Any = None
        self.note = QLabel(self)
        self.note.setObjectName("editor_note")
        self.note.setWordWrap(True)
        self.note.setTextFormat(Qt.TextFormat.PlainText)
        self.note.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        self.note.setVisible(False)

    # -- tasks ----------------------------------------------------------------------------------
    def _task_manager(self, tasks: Any = None) -> Any:
        if tasks is None:
            if self._own_tasks is None:
                from jsonreshape.gui.qtasks import TaskManager

                self._own_tasks = TaskManager(self)
            tasks = self._own_tasks
        if tasks not in self._managers:
            self._managers.append(tasks)
        return tasks

    def _detach_tasks(self) -> None:
        for manager in self._managers:
            try:
                manager.detach(self)
            except Exception:  # pragma: no cover - a manager being destroyed
                log.debug("detach failed", exc_info=True)

    # -- states ---------------------------------------------------------------------------------
    def _show_note(self, text: str, kind: str = "info") -> None:
        from jsonreshape.gui import copyfmt

        self.note.setText(copyfmt.escape_surrogates(text or ""))
        role = {"error": "danger", "warning": "warn"}.get(kind)
        theme.bind_style(self.note, f"color: ${role};" if role else "")
        self.note.setVisible(bool(text))

    def _content_widgets(self) -> list[QWidget]:
        return []

    def _ok_button(self) -> QPushButton | None:
        return None

    def _cancel_button(self) -> QPushButton | None:
        return None

    def _set_loading(self, on: bool) -> None:
        self.loading = bool(on)
        for w in self._content_widgets():
            w.setEnabled(not on)
        ok = self._ok_button()
        if ok is not None and on:
            ok.setEnabled(False)
        if on:
            self._show_note(t("gui.editors.loading"))
        else:
            self._show_note("")

    def _refuse(self, exc: BaseException) -> None:
        """The refused state: the reason, no editor, one Close button."""
        self.refused = True
        self.loading = False
        self._show_note(_message(exc), "error")
        for w in self._content_widgets():
            w.setVisible(False)
        ok = self._ok_button()
        if ok is not None:
            ok.setVisible(False)
        cancel = self._cancel_button()
        if cancel is not None:
            cancel.setText(t("gui.common.close"))
        self.adjustSize()

    def _load_failed(self, exc: BaseException) -> None:
        if not _alive(self):
            return
        from jsonreshape.errors import Cancelled

        if isinstance(exc, Cancelled):
            return
        self._refuse(exc)

    # -- closing ----------------------------------------------------------------------------------
    def is_modified(self) -> bool:
        return False

    def reject(self) -> None:  # noqa: D401 - Qt name
        """Cancel / Esc / close button: asks before dropping changes (non-blocking)."""
        if self._asking:
            return
        modified = False
        if not self.refused and not self.loading:
            try:
                modified = self.is_modified()
            except Exception:  # pragma: no cover - be safe: ask
                modified = True
        if not modified:
            super().reject()
            return
        from jsonreshape.gui.widgets import ask

        self._asking = True

        def answered(yes: bool) -> None:
            self._asking = False
            if yes and _alive(self):
                QDialog.reject(self)

        ask(self, t("gui.editors.discard.question"), answered, title=self.windowTitle())

    def discard(self) -> None:
        """Close without asking (the node went away, the session closed ...)."""
        QDialog.reject(self)

    def _is_closed(self) -> bool:
        return self._closed_flag or not _alive(self)

    def done(self, result: int) -> None:  # noqa: D401 - Qt name
        self._closed_flag = True
        self._detach_tasks()
        super().done(result)


# ======================================================================================
# StringDialog
# ======================================================================================
class StringDialog(_DialogBase):
    """Multi-line editor of one string value (module docstring)."""

    value_accepted = Signal(str)

    def __init__(self, parent: QWidget | None = None, text: str = "", *, title: str | None = None,
                 on_accept: Callable[[str], Any] | None = None, loading: bool = False) -> None:
        super().__init__(parent, title or t("gui.editors.string.title"))
        self.setObjectName("StringDialog")
        self._on_accept = on_accept
        self.original = ""
        self._escaped = False
        self._newline = "\n"

        layout = QVBoxLayout(self)
        layout.addWidget(self.note)
        self.editor = QPlainTextEdit(self)
        self.editor.setObjectName("string_editor")
        self.editor.setFont(_mono(parent))
        self.editor.setLineWrapMode(QPlainTextEdit.LineWrapMode.WidgetWidth)
        self.editor.setTabChangesFocus(False)
        layout.addWidget(self.editor, 1)
        row = QHBoxLayout()
        self.wrap_check = QCheckBox(t("gui.editors.string.wrap"), self)
        self.wrap_check.setChecked(True)
        self.wrap_check.toggled.connect(self.set_wrap)
        row.addWidget(self.wrap_check)
        self.escape_check = QCheckBox(t("gui.editors.string.escapes"), self)
        self.escape_check.setToolTip(t("gui.editors.escaped.tip"))
        self.escape_check.toggled.connect(self._on_escape_toggled)
        row.addWidget(self.escape_check)
        row.addStretch(1)
        self.count_label = QLabel(self)
        self.count_label.setObjectName("count_label")
        row.addWidget(self.count_label)
        layout.addLayout(row)
        self.error_label = _error_label(self)
        layout.addWidget(self.error_label)
        self.buttons, self.ok_button, self.cancel_button = _button_box(self)
        layout.addWidget(self.buttons)

        self._timer = QTimer(self)
        self._timer.setSingleShot(True)
        self._timer.setInterval(VALIDATE_DELAY_MS)
        self._timer.timeout.connect(self._update)
        self.editor.textChanged.connect(self._timer.start)
        self.resize(640, 420)
        if loading:
            self._set_loading(True)
        else:
            self.set_text(text)

    # -- dialog base hooks ----------------------------------------------------------------------
    def _content_widgets(self) -> list[QWidget]:
        return [self.editor, self.wrap_check, self.escape_check, self.count_label]

    def _ok_button(self) -> QPushButton | None:
        return self.ok_button

    def _cancel_button(self) -> QPushButton | None:
        return self.cancel_button

    # -- content --------------------------------------------------------------------------------
    @property
    def escaped(self) -> bool:
        """The editor shows the escaped form."""
        return self._escaped

    def set_text(self, text: str) -> None:
        """(Re)load the dialog with ``text`` (the decoded original string)."""
        value = str(text)
        self._set_loading(False)
        size = _utf8_len(value)
        if size > STRING_DIALOG_MAX:
            self._refuse(UserInputError("gui.editors.string.too_large", size=format_bytes(size),
                                        limit=format_bytes(STRING_DIALOG_MAX)))
            return
        self.original = value
        problems = _plain_problems(value)
        if problems:
            self._show_note(t("gui.editors.string.forced_escapes", what=", ".join(problems)), "warning")
            self._show(value, True)
        else:
            self._newline = _newline_of(value) or "\n"
            self._show(value, False)
        self.editor.document().setModified(False)
        self._update()

    def _show(self, value: str, escaped: bool) -> None:
        self._escaped = escaped
        self.escape_check.blockSignals(True)
        self.escape_check.setChecked(escaped)
        self.escape_check.blockSignals(False)
        self.editor.blockSignals(True)
        self.editor.setPlainText(escape_text(value) if escaped else value)
        self.editor.blockSignals(False)
        self.editor.moveCursor(QTextCursor.MoveOperation.Start)

    def _editor_text(self) -> str:
        raw = self.editor.document().toRawText()
        return raw.replace(_PARAGRAPH, "\n" if self._escaped else self._newline)

    def value(self) -> str:
        """The current (decoded) value; ``EditError`` when the escaped text is not valid."""
        text = self._editor_text()
        return unescape_text(text) if self._escaped else text

    def json_text(self) -> str:
        """The JSON string token of :meth:`value`."""
        return editops.encode_string(self.value()).decode("utf-8")

    def text(self) -> str:
        """Same as :meth:`value` (the decoded string)."""
        return self.value()

    def is_modified(self) -> bool:
        try:
            return self.value() != self.original
        except EditError:
            return True

    def set_wrap(self, on: bool) -> None:
        self.wrap_check.blockSignals(True)
        self.wrap_check.setChecked(bool(on))
        self.wrap_check.blockSignals(False)
        mode = QPlainTextEdit.LineWrapMode.WidgetWidth if on else QPlainTextEdit.LineWrapMode.NoWrap
        self.editor.setLineWrapMode(mode)

    def set_escaped(self, on: bool) -> bool:
        """Switch between the plain and the escaped form; False (nothing changes, the reason is shown) when
        the current text is not valid or cannot be shown plainly."""
        self._timer.stop()
        on = bool(on)
        if on == self._escaped:
            return True
        try:
            value = self.value()
        except EditError as exc:
            _set_error(self.error_label, exc.message)
            self._show_check()
            return False
        if not on:
            problems = _plain_problems(value)
            if problems:
                _set_error(self.error_label, t("gui.editors.string.plain_refused", what=", ".join(problems)))
                self._show_check()
                return False
            self._newline = _newline_of(value) or "\n"
        self._show(value, on)
        self._update()
        return True

    def _show_check(self) -> None:
        self.escape_check.blockSignals(True)
        self.escape_check.setChecked(self._escaped)
        self.escape_check.blockSignals(False)

    def _on_escape_toggled(self, on: bool) -> None:
        self.set_escaped(on)

    def _update(self) -> None:
        """Validation and counts (debounced while typing)."""
        self._timer.stop()
        if self.refused or self.loading:
            self.ok_button.setEnabled(False)
            return
        error = ""
        try:
            value = self.value()
        except EditError as exc:
            value = None
            error = exc.message
        if value is not None:
            size = _utf8_len(value)
            if size > STRING_DIALOG_MAX:
                error = t("gui.editors.error.too_long", size=format_bytes(size), limit=format_bytes(STRING_DIALOG_MAX))
        _set_error(self.error_label, error)
        if value is None:
            self.count_label.setText(t("gui.editors.string.count_invalid"))
        else:
            from jsonreshape.gui.widgets import format_count

            self.count_label.setText(t("gui.editors.string.count", count=format_count(len(value)),
                                       lines=format_count(_line_count(value))))
        self.ok_button.setEnabled(not error)

    # -- closing --------------------------------------------------------------------------------
    def accept(self) -> None:  # noqa: D401 - Qt name
        """OK: emits ``value_accepted`` with the JSON token when the value changed."""
        if self.refused or self.loading:
            return
        self._update()
        if not self.ok_button.isEnabled():
            return
        value = self.value()
        if value == self.original:
            super().accept()
            return
        token = editops.encode_string(value).decode("utf-8")
        callback = self._on_accept
        super().accept()
        self.value_accepted.emit(token)
        if callback is not None:
            callback(token)

    # -- loader -----------------------------------------------------------------------------------
    @classmethod
    def for_node(cls, session: Any, ref: Any, *, parent: QWidget | None = None, title: str | None = None,
                 on_accept: Callable[[str], Any] | None = None, show: bool = True) -> StringDialog:
        """Open the dialog for the string of ``ref`` (read in a silent task; refused above the limit)."""
        dlg = cls(parent, "", title=title, on_accept=on_accept, loading=True)
        tasks = dlg._task_manager(session.app.tasks)

        def work(ctx: Any) -> tuple[str, int]:
            doc = _pinned(session)
            node = session.node(ref, ctx.cancel, doc=doc)
            if node.kind != "string":
                raise UserInputError("gui.editors.not_string")
            size = int(node.size)
            if size - 2 > STRING_DIALOG_MAX:
                raise UserInputError("gui.editors.string.too_large", size=format_bytes(size),
                                     limit=format_bytes(STRING_DIALOG_MAX))
            raw = b"".join(doc.node_chunks(node, cancel=ctx.cancel))
            ctx.check()
            value = codec.loads(raw)
            if not isinstance(value, str):  # pragma: no cover - checked above
                raise UserInputError("gui.editors.not_string")
            return value, int(getattr(doc, "gen", 0) or 0)

        def loaded(result: tuple[str, int]) -> None:
            if _alive(dlg):
                dlg.loaded_gen = result[1]
                dlg.set_text(result[0])

        tasks.run(work, title=t("gui.editors.task.load"), kind="value", owner=dlg, silent=True,
                  on_done=loaded, on_error=dlg._load_failed)
        if show:
            dlg.open()
        return dlg


def _pinned(session: Any) -> Any:
    doc = session.doc
    if doc is None:
        raise EditError("edits.error.closed")
    pinned = getattr(doc, "pinned", None)
    return pinned() if callable(pinned) else doc


# ======================================================================================
# JsonEditDialog
# ======================================================================================
class JsonEditDialog(_DialogBase):
    """JSON text editor with live validation (module docstring)."""

    value_accepted = Signal(str)
    validated = Signal(bool)

    def __init__(self, parent: QWidget | None = None, text: str = "", *, title: str | None = None,
                 indent: int | str = 2, tasks: Any = None, on_accept: Callable[[str], Any] | None = None,
                 loading: bool = False) -> None:
        super().__init__(parent, title or t("gui.editors.json.title"))
        self.setObjectName("JsonEditDialog")
        self._tasks = tasks
        self._on_accept = on_accept
        self.original = ""
        self._gen = 0                 # text generation: bumped on every change
        self._result_gen = -1         # generation of the last validation result
        self._valid = False
        self._kind: str | None = None
        self._error: EditError | None = None
        self._error_span: tuple[int, int] | None = None
        self.validation_path: str | None = None
        self._check_task: Any = None
        self._recheck = False
        self._format_task: Any = None

        layout = QVBoxLayout(self)
        layout.addWidget(self.note)
        self.editor = QPlainTextEdit(self)
        self.editor.setObjectName("json_editor")
        self.editor.setFont(_mono(parent))
        self.editor.setLineWrapMode(QPlainTextEdit.LineWrapMode.NoWrap)
        layout.addWidget(self.editor, 1)
        self.status_label = QLabel(self)
        self.status_label.setObjectName("json_status")
        self.status_label.setWordWrap(True)
        self.status_label.setTextFormat(Qt.TextFormat.PlainText)
        self.status_label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        layout.addWidget(self.status_label)
        row = QHBoxLayout()
        self.indent_label = QLabel(t("gui.editors.json.indent"), self)
        row.addWidget(self.indent_label)
        self.indent_combo = QComboBox(self)
        self.indent_combo.setObjectName("indent_combo")
        for value, key in ((2, "gui.editors.json.indent.2"), (4, "gui.editors.json.indent.4"),
                           ("\t", "gui.editors.json.indent.tab")):
            self.indent_combo.addItem(t(key), value)
        row.addWidget(self.indent_combo)
        self.format_button = QPushButton(t("gui.editors.json.format"), self)
        self.format_button.setObjectName("format_button")
        self.format_button.setToolTip(t("gui.editors.json.format.tip"))
        self.format_button.setAutoDefault(False)
        self.format_button.clicked.connect(self.format_text)
        row.addWidget(self.format_button)
        row.addStretch(1)
        self.buttons, self.ok_button, self.cancel_button = _button_box(self)
        self.ok_button.setToolTip(t("gui.editors.json.ok.tip"))
        row.addWidget(self.buttons)
        layout.addLayout(row)
        self.set_indent(indent)
        self.editor.installEventFilter(self)     # Ctrl+Enter = OK (a QShortcut needs an active window)

        self._timer = QTimer(self)
        self._timer.setSingleShot(True)
        self._timer.setInterval(VALIDATE_DELAY_MS)
        self._timer.timeout.connect(self._validate_now)
        self.editor.textChanged.connect(self._on_text_changed)
        self.resize(720, 520)
        if loading:
            self._set_loading(True)
            self._update_buttons()
        else:
            self.set_text(text)

    def eventFilter(self, obj: Any, event: QEvent) -> bool:  # noqa: N802 - Qt name
        if obj is self.editor and event.type() in (QEvent.Type.KeyPress, QEvent.Type.ShortcutOverride) \
                and isinstance(event, QKeyEvent) and event.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter) \
                and event.modifiers() & Qt.KeyboardModifier.ControlModifier:
            event.accept()
            if event.type() == QEvent.Type.KeyPress:
                self.accept()
            return True
        return super().eventFilter(obj, event)

    # -- dialog base hooks ----------------------------------------------------------------------
    def _content_widgets(self) -> list[QWidget]:
        return [self.editor, self.status_label, self.indent_label, self.indent_combo, self.format_button]

    def _ok_button(self) -> QPushButton | None:
        return self.ok_button

    def _cancel_button(self) -> QPushButton | None:
        return self.cancel_button

    # -- content ----------------------------------------------------------------------------------
    def indent(self) -> int | str:
        """The chosen indent: 2, 4 or ``"\\t"``."""
        value = self.indent_combo.currentData()
        return value if value is not None else 2

    def set_indent(self, indent: int | str) -> None:
        arg = _indent_arg(indent)
        index = self.indent_combo.findData(arg)
        self.indent_combo.setCurrentIndex(index if index >= 0 else 0)

    def set_text(self, text: str) -> None:
        """(Re)load the dialog with ``text`` (the original JSON text)."""
        text = _sanitize_json_text(str(text))
        self._set_loading(False)
        size = _utf8_len(text)
        if size > JSON_DIALOG_MAX:
            self._refuse(UserInputError("gui.editors.json.too_large", size=format_bytes(size),
                                        limit=format_bytes(JSON_DIALOG_MAX)))
            return
        self.editor.setPlainText(text)          # -> _on_text_changed
        self.original = self.text()
        self.editor.document().setModified(False)
        self.editor.moveCursor(QTextCursor.MoveOperation.Start)
        self._validate_now()

    def text(self) -> str:
        """The current text (paragraph breaks as ``\\n``; NBSP and U+2028 kept)."""
        return self.editor.document().toRawText().replace(_PARAGRAPH, "\n")

    def is_modified(self) -> bool:
        return self.text() != self.original

    def is_valid(self) -> bool:
        """The CURRENT text was validated and is valid JSON."""
        return self._valid and self._result_gen == self._gen

    @property
    def error(self) -> EditError | None:
        """The validation error of the current text (``None`` when valid or not checked yet)."""
        return self._error if self._result_gen == self._gen else None

    @property
    def value_kind(self) -> str | None:
        """JSON type of the current (valid) text."""
        return self._kind if self.is_valid() else None

    def error_selection(self) -> tuple[int, int] | None:
        """UTF-16 ``[start, end)`` of the underlined error character (``None``: no underline)."""
        sels = self.editor.extraSelections()
        if not sels:
            return None
        cur = sels[0].cursor
        return cur.selectionStart(), cur.selectionEnd()

    # -- validation -------------------------------------------------------------------------------
    def _on_text_changed(self) -> None:
        self._gen += 1
        if self._format_task is not None:
            self._format_task = None
        self.editor.setExtraSelections([])
        if not (self.refused or self.loading):
            self.status_label.setText(t("gui.editors.json.checking"))
            theme.bind_style(self.status_label, "color: $muted;")
        self._update_buttons()
        self._timer.start()

    def _validate_now(self) -> None:
        """Validate the current text now: in the GUI thread up to JSON_SYNC_MAX, else in a task."""
        self._timer.stop()
        if self.refused or self.loading:
            return
        gen = self._gen
        text = self.text()
        data = text.encode("utf-8", "surrogatepass")
        if len(data) <= JSON_SYNC_MAX:
            self.validation_path = "gui"
            self._apply_result(gen, _check_json_text(text, data))
            return
        self.validation_path = "task"
        if self._check_task is not None and self._check_task.state == "running":
            self._recheck = True                 # one check at a time: the newest text follows this one
            return
        self._recheck = False
        tasks = self._task_manager(self._tasks)
        self._check_task = tasks.run(
            lambda _ctx: _check_json_text(text, data), title=t("gui.editors.task.validate"), kind="validate",
            owner=self, silent=True,
            on_done=lambda result, g=gen: self._check_ended(g, result),
            on_error=lambda exc, g=gen: self._check_ended(g, None, exc))

    def _check_ended(self, gen: int, result: _Check | None, exc: BaseException | None = None) -> None:
        """A background check ended: its result is applied when the text is unchanged (else dropped), and
        a text changed meanwhile is checked now."""
        if not _alive(self):
            return
        self._check_task = None
        if exc is not None:
            if gen == self._gen:
                log.warning("JSON validation failed: %r", exc)
                result = _Check(False, None, EditError("edits.error.generic", detail=str(exc)), None)
        if result is not None:
            self._apply_result(gen, result)
        if self._recheck or (gen != self._gen and not self._timer.isActive()):
            self._recheck = False
            self._validate_now()

    def _apply_result(self, gen: int, result: _Check) -> None:
        if not _alive(self) or gen != self._gen:
            return                               # a result for an older text: dropped
        self._result_gen = gen
        self._valid = result.ok
        self._kind = result.kind
        self._error = result.error
        self._error_span = result.span
        if result.ok:
            self.editor.setExtraSelections([])
            self.status_label.setText(t("gui.editors.json.valid", type=_type_label(result.kind)))
            theme.bind_style(self.status_label, "color: $ok;")
        else:
            exc = result.error
            params = exc.params if exc is not None else {}
            if exc is not None and exc.key == "edits.error.invalid_json":
                text = t("gui.editors.json.invalid", line=params.get("line", "?"), column=params.get("column", "?"),
                         reason=params.get("reason", ""))
            else:
                text = exc.message if exc is not None else ""
            self.status_label.setText(text)
            theme.bind_style(self.status_label, "color: $danger;")
            self._underline(result.span)
        self._update_buttons()
        self.validated.emit(result.ok)

    def _underline(self, span: tuple[int, int] | None) -> None:
        if span is None or span[1] <= span[0]:
            self.editor.setExtraSelections([])
            return
        doc = self.editor.document()
        end_limit = max(0, doc.characterCount() - 1)
        start, end = min(span[0], end_limit), min(span[1], end_limit)
        if end <= start:
            self.editor.setExtraSelections([])
            return
        sel = QTextEdit.ExtraSelection()
        fmt = QTextCharFormat()
        fmt.setUnderlineStyle(QTextCharFormat.UnderlineStyle.WaveUnderline)
        fmt.setUnderlineColor(_danger())
        sel.format = fmt
        cursor = QTextCursor(doc)
        cursor.setPosition(start)
        cursor.setPosition(end, QTextCursor.MoveMode.KeepAnchor)
        sel.cursor = cursor
        self.editor.setExtraSelections([sel])

    def _update_buttons(self) -> None:
        usable = not (self.refused or self.loading)
        valid = usable and self.is_valid()
        self.ok_button.setEnabled(valid)
        self.format_button.setEnabled(valid and self._format_task is None)

    # -- formatting -------------------------------------------------------------------------------
    def format_text(self) -> None:
        """Re-indent the (valid) text with :meth:`indent`; values stay byte-identical. Undoable."""
        if not self.is_valid():
            return
        gen = self._gen
        data = self.text().encode("utf-8", "surrogatepass")
        indent = self.indent()
        if len(data) <= JSON_SYNC_MAX:
            self._apply_format(gen, _reformat(data, indent))
            return
        tasks = self._task_manager(self._tasks)
        self._format_task = tasks.run(
            lambda _ctx: _reformat(data, indent), title=t("gui.editors.task.format"), kind="format", owner=self,
            silent=True, on_done=lambda out, g=gen: self._apply_format(g, out),
            on_error=lambda exc: self._format_failed(exc))
        self._update_buttons()

    def _format_failed(self, exc: BaseException) -> None:
        if not _alive(self):
            return
        self._format_task = None
        log.warning("formatting failed: %r", exc)
        self._update_buttons()

    def _apply_format(self, gen: int, out: bytes) -> None:
        if not _alive(self):
            return
        self._format_task = None
        if gen != self._gen:
            self._update_buttons()
            return                               # the text changed meanwhile: dropped
        new = out.decode("utf-8", "surrogatepass")
        if new == self.text():
            self._update_buttons()
            return
        kind = self._kind
        cursor = QTextCursor(self.editor.document())
        cursor.beginEditBlock()
        cursor.select(QTextCursor.SelectionType.Document)
        cursor.insertText(new)
        cursor.endEditBlock()
        self.editor.moveCursor(QTextCursor.MoveOperation.Start)
        # the Reformatter changed whitespace only: the new text is valid (no second validation pass)
        self._timer.stop()
        self._apply_result(self._gen, _Check(True, kind))

    # -- closing ----------------------------------------------------------------------------------
    def accept(self) -> None:  # noqa: D401 - Qt name
        """OK / Ctrl+Enter: only for a valid text; emits ``value_accepted`` when it changed."""
        if self.refused or self.loading:
            return
        if self._result_gen != self._gen:
            if _utf8_len(self.text()) <= JSON_SYNC_MAX:
                self._validate_now()             # small: check now instead of waiting for the debounce
            else:
                return                           # a big text: wait for its validation
        if not self.is_valid():
            return
        text = self.text()
        if text == self.original:
            super().accept()
            return
        callback = self._on_accept
        super().accept()
        self.value_accepted.emit(text)
        if callback is not None:
            callback(text)

    def done(self, result: int) -> None:  # noqa: D401 - Qt name
        self._timer.stop()
        super().done(result)

    # -- loader -----------------------------------------------------------------------------------
    @classmethod
    def for_node(cls, session: Any, ref: Any, *, parent: QWidget | None = None, title: str | None = None,
                 indent: int | str = 2, on_accept: Callable[[str], Any] | None = None,
                 show: bool = True) -> JsonEditDialog:
        """Open the dialog for the effective text of ``ref`` pretty-printed with ``indent`` (read in a silent
        task from a pinned document; refused when the pretty text exceeds :data:`JSON_DIALOG_MAX`)."""
        tasks = session.app.tasks
        dlg = cls(parent, "", title=title, indent=indent, tasks=tasks, on_accept=on_accept, loading=True)
        dlg._task_manager(tasks)
        arg = _indent_arg(indent)

        def work(ctx: Any) -> tuple[str, int]:
            doc = _pinned(session)
            node = session.node(ref, ctx.cancel, doc=doc)
            fmt = core.Reformatter(indent=arg)
            out = bytearray()

            def too_large() -> UserInputError:
                return UserInputError("gui.editors.json.too_large", size=format_bytes(max(int(node.size), len(out))),
                                      limit=format_bytes(JSON_DIALOG_MAX))

            chunks = doc.node_chunks(node, chunk=MiB, cancel=ctx.cancel)
            try:
                for piece in chunks:
                    ctx.check()
                    out += fmt.feed(piece)
                    if len(out) > JSON_DIALOG_MAX:
                        raise too_large()
            finally:
                close = getattr(chunks, "close", None)
                if callable(close):
                    close()
            out += fmt.finish()
            if len(out) > JSON_DIALOG_MAX:
                raise too_large()
            return bytes(out).decode("utf-8", "surrogatepass"), int(getattr(doc, "gen", 0) or 0)

        def loaded(result: tuple[str, int]) -> None:
            if _alive(dlg):
                dlg.loaded_gen = result[1]
                dlg.set_text(result[0])

        tasks.run(work, title=t("gui.editors.task.load"), kind="value", owner=dlg, silent=True,
                  on_done=loaded, on_error=dlg._load_failed)
        if show:
            dlg.open()
        return dlg


# ======================================================================================
# KeyDialog
# ======================================================================================
class KeyDialog(_DialogBase):
    """Rename an object member or a record key (module docstring)."""

    key_accepted = Signal(str)

    def __init__(self, parent: QWidget | None = None, key: str = "", *, title: str | None = None,
                 taken: Container[str] | None = None, on_accept: Callable[[str], Any] | None = None,
                 label: str | None = None, key_check: KeyCheck | None = None) -> None:
        super().__init__(parent, title or t("gui.editors.key.title"))
        self.setObjectName("KeyDialog")
        self.original = str(key)
        self._key_check = key_check
        self._keys = _KeyAnswers(self._is_closed)
        self._taken = _TakenOrFound(taken, self._keys)
        self._on_accept = on_accept
        self._codec = _LineValue(self.original)
        layout = QVBoxLayout(self)
        form = QFormLayout()
        self.edit = QLineEdit(self)
        self.edit.setObjectName("key_edit")
        self.edit.setMaxLength(_LINE_MAX)
        self.edit.setText(self._codec.shown(self.original))
        if self._codec.escaped:
            self.edit.setToolTip(t("gui.editors.escaped.tip"))
        self.edit.selectAll()
        form.addRow(label or t("gui.editors.key.label"), self.edit)
        layout.addLayout(form)
        self.error_label = _error_label(self)
        layout.addWidget(self.error_label)
        layout.addWidget(self.note)                   # "checking the key ..."
        self.buttons, self.ok_button, self.cancel_button = _button_box(self)
        layout.addWidget(self.buttons)
        self.edit.textChanged.connect(self._validate)
        self._validate()
        self.resize(420, self.sizeHint().height())

    @property
    def escaped(self) -> bool:
        return self._codec.escaped

    def key(self) -> str:
        """The new key; ``EditError`` when the escaped text is not valid."""
        return self._codec.decode(self.edit.text())

    def text(self) -> str:
        """Same as :meth:`key`."""
        return self.key()

    def _problem(self) -> str:
        try:
            value = self.key()
        except EditError as exc:
            return exc.message
        return _key_problem(value, self.original, self._taken)

    def _validate(self, *_args: Any) -> None:
        problem = self._problem()
        _set_error(self.error_label, problem)
        self.ok_button.setEnabled(not problem and not self.checking)

    def is_modified(self) -> bool:
        try:
            return self.key() != self.original
        except EditError:
            return True

    @property
    def checking(self) -> bool:
        """A key check (``key_check``) is on its way; OK follows its answer."""
        return self._keys.asking is not None

    def accept(self) -> None:  # noqa: D401 - Qt name
        if self._problem():
            self._validate()
            return
        key = self.key()
        if key == self.original:
            super().accept()
            return
        if self._key_check is not None:
            answer = self._keys.taken(key)
            if answer is None:
                self._keys.ask(self._key_check, key, key, self._key_answered)
                answer = self._keys.taken(key)
            if answer is None:
                self._show_note(t("gui.editors.key.checking"))
                self._validate()                      # OK disabled while checking
                return
            if answer:
                self._validate()                      # "exists" in place
                return
        callback = self._on_accept
        super().accept()
        self.key_accepted.emit(key)
        if callback is not None:
            callback(key)

    def _key_answered(self, key: Any) -> None:
        self._show_note("")
        self._validate()
        try:
            current = self.key()
        except EditError:
            return
        if current == key and not self._keys.taken(key):
            self.accept()

    def forget_key_checks(self) -> None:
        """The data changed: drop the key check answers (a lookup on its way is asked again)."""
        key = self._keys.forget()
        if self._is_closed():
            return
        self._show_note("")
        self._validate()
        try:
            current = self.key()
        except EditError:
            return
        if key is not None and key == current and self._key_check is not None:
            self.accept()                             # OK was pressed: ask again on the new data


# ======================================================================================
# InsertDialog
# ======================================================================================
@dataclass(frozen=True)
class InsertResult:
    """What :class:`InsertDialog` collected: ``position`` (before | after | child), ``key`` (``None`` where
    no key is needed), ``value`` (JSON text) and ``type`` (one of :data:`INSERT_TYPES`)."""

    position: str
    key: str | None
    value: str
    type: str


class InsertDialog(_DialogBase):
    """Insert a new node: position, key, type and value (module docstring)."""

    insert_accepted = Signal(object)

    def __init__(self, parent: QWidget | None = None, *, title: str | None = None,
                 positions: Sequence[str] = INSERT_POSITIONS, position: str | None = None,
                 key_needed: bool | Mapping[str, bool] = False,
                 taken: Container[str] | Mapping[str, Container[str] | None] | None = None, key: str | None = None,
                 value_type: str = "string", value: str | None = None,
                 on_accept: Callable[[InsertResult], Any] | None = None,
                 key_check: KeyCheck | Mapping[str, KeyCheck | None] | None = None) -> None:
        super().__init__(parent, title or t("gui.editors.insert.title"))
        self.setObjectName("InsertDialog")
        positions = tuple(p for p in INSERT_POSITIONS if p in tuple(positions))
        if not positions:
            raise ValueError("InsertDialog needs at least one position")
        if value_type not in INSERT_TYPES:
            raise ValueError(f"unknown value type: {value_type!r}")
        self.positions = positions
        self._key_needed = key_needed
        self._taken = taken
        self._key_check = key_check
        self._keys = _KeyAnswers(self._is_closed)     # answers per (position, key)
        self._on_accept = on_accept
        self._json_valid: bool | None = None
        self._json_error = ""

        layout = QVBoxLayout(self)
        form = QFormLayout()
        pos_row = QHBoxLayout()
        self.position_group = QButtonGroup(self)
        self.position_buttons: dict[str, QRadioButton] = {}
        for i, pos in enumerate(positions):
            button = QRadioButton(t(f"gui.editors.insert.{pos}"), self)
            button.setObjectName(f"position_{pos}")
            self.position_group.addButton(button, i)
            self.position_buttons[pos] = button
            pos_row.addWidget(button)
        pos_row.addStretch(1)
        self.position_label = QLabel(t("gui.editors.insert.position"), self)
        form.addRow(self.position_label, pos_row)
        self.key_label = QLabel(t("gui.editors.insert.key"), self)
        self.key_edit = QLineEdit(self)
        self.key_edit.setObjectName("insert_key")
        self.key_edit.setMaxLength(_LINE_MAX)
        self.key_edit.setPlaceholderText(t("gui.editors.insert.key.placeholder"))
        if key is not None:
            self.key_edit.setText(str(key))
        form.addRow(self.key_label, self.key_edit)
        self.type_combo = QComboBox(self)
        self.type_combo.setObjectName("insert_type")
        for kind in INSERT_TYPES:
            self.type_combo.addItem(_type_label(kind), kind)
        form.addRow(t("gui.editors.insert.type"), self.type_combo)

        self.value_stack = QStackedWidget(self)
        self.string_edit = QLineEdit(self)
        self.string_edit.setObjectName("insert_string")
        self.string_edit.setMaxLength(_LINE_MAX)
        self.number_edit = QLineEdit(self)
        self.number_edit.setObjectName("insert_number")
        self.number_edit.setText("0")
        self.bool_combo = QComboBox(self)
        self.bool_combo.setObjectName("insert_bool")
        self.bool_combo.addItem("true", "true")
        self.bool_combo.addItem("false", "false")
        self.json_edit = QPlainTextEdit(self)
        self.json_edit.setObjectName("insert_json")
        self.json_edit.setFont(_mono(parent))
        self._pages: dict[str, int] = {}
        for kind, widget in (("string", self.string_edit), ("number", self.number_edit), ("bool", self.bool_combo),
                             ("null", QLabel(t("gui.editors.insert.null"), self)),
                             ("object", QLabel(t("gui.editors.insert.object"), self)),
                             ("array", QLabel(t("gui.editors.insert.array"), self)), ("json", self.json_edit)):
            self._pages[kind] = self.value_stack.addWidget(widget)
        form.addRow(t("gui.editors.insert.value"), self.value_stack)
        layout.addLayout(form)
        self.error_label = _error_label(self)
        layout.addWidget(self.error_label)
        layout.addWidget(self.note)                   # "checking the key ..."
        self.buttons, self.ok_button, self.cancel_button = _button_box(self)
        layout.addWidget(self.buttons)

        if value is not None:
            if value_type == "string":
                self.string_edit.setText(str(value))
            elif value_type == "number":
                self.number_edit.setText(str(value))
            elif value_type == "bool":
                self.bool_combo.setCurrentIndex(0 if str(value).strip() == "true" else 1)
            elif value_type == "json":
                self.json_edit.setPlainText(_sanitize_json_text(str(value)))
        self._json_timer = QTimer(self)
        self._json_timer.setSingleShot(True)
        self._json_timer.setInterval(VALIDATE_DELAY_MS)
        self._json_timer.timeout.connect(self._check_json_now)
        self.position_group.idToggled.connect(lambda _id, on: on and self._update())
        self.key_edit.textChanged.connect(self._update)
        self.type_combo.currentIndexChanged.connect(self._on_type)
        self.string_edit.textChanged.connect(self._update)
        self.number_edit.textChanged.connect(self._update)
        self.json_edit.textChanged.connect(self._on_json_changed)
        start = position if position in positions else ("after" if "after" in positions else positions[0])
        self.position_buttons[start].setChecked(True)
        self.type_combo.setCurrentIndex(INSERT_TYPES.index(value_type))
        self._on_type()
        self.resize(460, self.sizeHint().height())

    # -- state ------------------------------------------------------------------------------------
    def position(self) -> str:
        for pos, button in self.position_buttons.items():
            if button.isChecked():
                return pos
        return self.positions[0]

    def set_position(self, position: str) -> None:
        button = self.position_buttons.get(position)
        if button is None:
            raise ValueError(f"position not offered: {position!r}")
        button.setChecked(True)

    def value_type(self) -> str:
        return str(self.type_combo.currentData())

    def set_value_type(self, kind: str) -> None:
        self.type_combo.setCurrentIndex(INSERT_TYPES.index(kind))

    def key_needed(self, position: str | None = None) -> bool:
        pos = position or self.position()
        need = self._key_needed
        if isinstance(need, Mapping):
            return bool(need.get(pos, False))
        return bool(need)

    def _taken_for(self, position: str) -> Container[str] | None:
        taken = self._taken
        if isinstance(taken, Mapping):
            taken = taken.get(position)
        return _TakenOrFound(taken, self._keys, lambda key, pos=position: (pos, key))

    def _key_check_for(self, position: str) -> KeyCheck | None:
        check = self._key_check
        if isinstance(check, Mapping):
            return check.get(position)
        return check

    @property
    def checking(self) -> bool:
        """A key check (``key_check``) is on its way; OK follows its answer."""
        return self._keys.asking is not None

    def _on_type(self, *_args: Any) -> None:
        kind = self.value_type()
        self.value_stack.setCurrentIndex(self._pages[kind])
        if kind == "json":
            self._check_json_now()
        self._update()

    def _on_json_changed(self) -> None:
        self._json_valid = None
        self._json_timer.start()
        self._update()

    def _check_json_now(self) -> None:
        self._json_timer.stop()
        try:
            editops.check_json(self._json_text())
        except EditError as exc:
            self._json_valid, self._json_error = False, exc.message
        else:
            self._json_valid, self._json_error = True, ""
        self._update()

    def _json_text(self) -> str:
        return self.json_edit.document().toRawText().replace(_PARAGRAPH, "\n")

    def _problem(self) -> str:
        pos = self.position()
        if self.key_needed(pos):
            key = self.key_edit.text()
            if key == "":
                return t("gui.editors.error.key_required")
            problem = _key_problem(key, None, self._taken_for(pos))
            if problem:
                return problem
        kind = self.value_type()
        if kind == "number" and not is_json_number(self.number_edit.text()):
            return t("gui.editors.error.number")
        if kind == "json":
            if self._json_valid is None:
                return " "                       # waiting for the debounced check (OK disabled, no text)
            if not self._json_valid:
                return self._json_error
        return ""

    def _update(self, *_args: Any) -> None:
        need = self.key_needed()
        self.key_label.setVisible(need)
        self.key_edit.setVisible(need)
        problem = self._problem()
        _set_error(self.error_label, problem.strip())
        self.ok_button.setEnabled(not problem and not self.checking)

    def value_text(self) -> str:
        """The JSON text of the new value; ``EditError`` when invalid."""
        kind = self.value_type()
        if kind == "string":
            return editops.encode_string(self.string_edit.text()).decode("utf-8")
        if kind == "number":
            token = self.number_edit.text().strip(_JSON_WS)
            if not is_json_number(token):
                raise EditError("gui.editors.error.number")
            return token
        if kind == "bool":
            return str(self.bool_combo.currentData())
        if kind == "null":
            return "null"
        if kind == "object":
            return "{}"
        if kind == "array":
            return "[]"
        return editops.check_json(self._json_text()).decode("utf-8", "surrogatepass")

    def result(self) -> InsertResult:
        """The collected insert (``EditError`` while invalid)."""
        if self.value_type() == "json" and self._json_valid is None:
            self._check_json_now()
        problem = self._problem()
        if problem:
            raise EditError("gui.editors.error.insert_invalid", reason=problem.strip())
        pos = self.position()
        key = self.key_edit.text() if self.key_needed(pos) else None
        return InsertResult(pos, key, self.value_text(), self.value_type())

    def is_modified(self) -> bool:
        return False                             # nothing to lose: the dialog creates a new node

    def accept(self) -> None:  # noqa: D401 - Qt name
        try:
            res = self.result()
        except EditError:
            self._update()
            return
        check = self._key_check_for(res.position) if res.key is not None else None
        if check is not None:
            akey = (res.position, res.key)
            answer = self._keys.taken(akey)
            if answer is None:
                self._keys.ask(check, akey, str(res.key), self._key_answered)
                answer = self._keys.taken(akey)
            if answer is None:
                self._show_note(t("gui.editors.key.checking"))
                self._update()                        # OK disabled while checking
                return
            if answer:
                self._update()                        # "exists" in place
                return
        callback = self._on_accept
        super().accept()
        self.insert_accepted.emit(res)
        if callback is not None:
            callback(res)

    def _key_answered(self, akey: Any) -> None:
        self._show_note("")
        self._update()
        pos = self.position()
        if akey == (pos, self.key_edit.text()) and self.key_needed(pos) and not self._keys.taken(akey):
            self.accept()

    def forget_key_checks(self) -> None:
        """The data changed: drop the key check answers (a lookup on its way is asked again)."""
        akey = self._keys.forget()
        if self._is_closed():
            return
        self._show_note("")
        self._update()
        if akey is not None and akey == (self.position(), self.key_edit.text()):
            self.accept()                             # OK was pressed: ask again on the new data


# ======================================================================================
# type menu
# ======================================================================================
def _kinds(current: str | Iterable[str] | None) -> tuple[str, ...]:
    if current is None:
        return ()
    items = (current,) if isinstance(current, str) else tuple(current)
    return tuple(dict.fromkeys("bool" if k == "boolean" else str(k) for k in items))


def type_refusal(current_kind: str | Iterable[str] | None, target: str) -> str | None:
    """Why converting nodes of the kind(s) ``current_kind`` to ``target`` is not offered (localized), or
    ``None``: "already this type" for a single kind equal to ``target``, else the first conversion §12.4 always
    refuses (``EditSession.change_type`` refuses the whole command then). Unknown kinds (``None`` / empty):
    ``None``. The rule of :func:`type_menu` and of the views' "change type" menus."""
    kinds = _kinds(current_kind)
    if len(kinds) == 1 and kinds[0] == target:
        return t("gui.editors.type.same")
    bad = [k for k in kinds if (k, target) in _IMPOSSIBLE]
    if bad:
        return t("gui.editors.type.impossible", source=t(f"edits.type.{bad[0]}"), target=t(f"edits.type.{target}"))
    return None


def type_menu(parent: QWidget | None, current_kind: str | Iterable[str] | None,
              on_pick: Callable[[str], Any], *, title: str | None = None) -> QMenu:
    """The "change type" menu (module docstring); actions are named ``type_<kind>``."""
    kinds = _kinds(current_kind)
    menu = QMenu(title or t("gui.editors.type.menu"), parent)
    menu.setObjectName("type_menu")
    menu.setToolTipsVisible(True)
    for target in MENU_TYPES:
        action = menu.addAction(_type_label(target))
        action.setObjectName(f"type_{target}")
        action.setData(target)
        if len(kinds) == 1 and kinds[0] == target:
            action.setCheckable(True)
            action.setChecked(True)
        why = type_refusal(kinds, target)
        if why is not None:
            action.setEnabled(False)
            action.setToolTip(why)
        action.triggered.connect(lambda _checked=False, kind=target: on_pick(kind))
    return menu


# ======================================================================================
# value loader for the views
# ======================================================================================
@dataclass(frozen=True)
class EditValue:
    """A node's value as an editor needs it (:func:`load_edit_value`)."""

    kind: str                 # object | array | string | number | bool | null
    token: str | None         # exact JSON token of a scalar up to max_bytes (None: container / bigger)
    decoded: str | None       # strings: the decoded text (None when not read)
    size: int                 # bytes of the node's text
    gen: int                  # overlay gen of the document the value was read from


def load_edit_value(session: Any, ref: Any, on_done: Callable[[EditValue], Any], *, owner: Any = None,
                    on_error: Callable[[BaseException], Any] | None = None,
                    max_bytes: int = EDIT_VALUE_MAX) -> Any:
    """Read ``EditValue`` of ``ref`` in a silent task (pinned document); returns the task."""

    def work(ctx: Any) -> EditValue:
        doc = _pinned(session)
        node = session.node(ref, ctx.cancel, doc=doc)
        kind = node.kind
        size = int(node.size)
        token = decoded = None
        if kind not in ("object", "array") and size <= max_bytes:
            raw = b"".join(doc.node_chunks(node, cancel=ctx.cancel))
            token = raw.decode("utf-8", "surrogatepass")
            if kind == "string":
                decoded = codec.loads(raw)
        return EditValue(kind, token, decoded, size, int(getattr(doc, "gen", 0) or 0))

    def failed(exc: BaseException) -> None:
        from jsonreshape.gui.widgets import report_task_error

        report_task_error(session, exc, purpose="view")

    return session.app.tasks.run(work, title=t("gui.editors.task.load"), kind="value",
                                 owner=owner if owner is not None else session, on_done=on_done,
                                 on_error=on_error if on_error is not None else failed, silent=True)
