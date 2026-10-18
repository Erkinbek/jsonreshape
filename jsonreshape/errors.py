"""User-facing exception hierarchy.

Every exception carries an i18n key and parameters, so the message can be shown in the
current UI language (``str(exc)`` / ``exc.message``) together with an optional hint
(``exc.hint``) telling the user how to fix the problem.
"""

from __future__ import annotations

from typing import Any

from .i18n import has, t


class JsonReshapeError(Exception):
    """Base class for all expected (user-facing) errors."""

    key: str = "error.generic"

    def __init__(self, key: str | None = None, /, **params: Any) -> None:
        if key is not None:
            self.key = key
        self.params: dict[str, Any] = params
        super().__init__(self.key)

    @property
    def message(self) -> str:
        return t(self.key, **self.params)

    @property
    def hint(self) -> str | None:
        hint_key = self.key + ".hint"
        return t(hint_key, **self.params) if has(hint_key) else None

    def __str__(self) -> str:
        return self.message

    def __repr__(self) -> str:
        return f"{type(self).__name__}({self.key!r}, {self.params!r})"


class Cancelled(JsonReshapeError):
    key = "error.cancelled"


class UserInputError(JsonReshapeError):
    key = "error.user_input"


class QuerySyntaxError(UserInputError):
    """Filter/search expression error; ``pos`` is the 0-based character offset."""

    key = "error.query_syntax"

    def __init__(self, key: str | None = None, /, *, pos: int = 0, text: str = "", detail: str = "", **params: Any):
        super().__init__(key, pos=pos, text=text, detail=detail, **params)
        self.pos = pos
        self.text = text

    @property
    def message(self) -> str:
        params = dict(self.params)
        params["pos"] = self.pos + 1  # shown 1-based
        return t(self.key, **params)

    def caret(self) -> str:
        """The expression with a caret under the error position (for CLI output)."""
        if not self.text:
            return ""
        return f"{self.text}\n{' ' * max(0, self.pos)}^"


class PathSyntaxError(QuerySyntaxError):
    key = "error.path_syntax"


class JsonSyntaxError(JsonReshapeError):
    """A JSON syntax error at a precise location in a file."""

    key = "error.json_syntax"

    def __init__(
        self,
        *,
        code: str,
        offset: int,
        line: int,
        column: int,
        expected: str = "none",
        context_before: str = "",
        context_after: str = "",
        source: str = "",
    ) -> None:
        self.code = code
        self.offset = offset
        self.line = line  # 1-based
        self.column = column  # 1-based, in characters
        self.expected = expected
        self.context_before = context_before
        self.context_after = context_after
        self.source = source
        super().__init__(None, code=code, offset=offset, line=line, column=column, expected=expected)

    @property
    def reason(self) -> str:
        text = t(f"json.error.{self.code}")
        if self.expected and self.expected != "none":
            exp = t(f"json.expected.{self.expected}")
            if exp:
                text = f"{text}; {exp}"
        return text

    @property
    def message(self) -> str:
        return t(
            self.key,
            reason=self.reason,
            line=self.line,
            column=self.column,
            offset=self.offset,
        )

    def context(self) -> str:
        """Two-line context: surrounding text and a caret under the error position."""
        before = self.context_before.replace("\r", " ").replace("\n", " ").replace("\t", " ")
        after = self.context_after.replace("\r", " ").replace("\n", " ").replace("\t", " ")
        return f"{before}{after}\n{' ' * len(before)}^"

    def to_dict(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "offset": self.offset,
            "line": self.line,
            "column": self.column,
            "expected": self.expected,
            "context_before": self.context_before,
            "context_after": self.context_after,
            "source": self.source,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> JsonSyntaxError:
        return cls(
            code=data["code"],
            offset=int(data["offset"]),
            line=int(data["line"]),
            column=int(data["column"]),
            expected=data.get("expected", "none"),
            context_before=data.get("context_before", ""),
            context_after=data.get("context_after", ""),
            source=data.get("source", ""),
        )


class JsonDecodeError(JsonReshapeError):
    """Invalid JSON text given to codec.loads (not file-positioned)."""

    key = "error.json_decode"

    def __init__(self, detail: str = "", pos: int = -1) -> None:
        super().__init__(None, detail=detail, pos=pos)
        self.pos = pos


class FileFormatError(JsonReshapeError):
    key = "error.file_format"


class TooLargeError(JsonReshapeError):
    key = "error.too_large"


class FileChangedError(JsonReshapeError):
    key = "error.file_changed"


class ExportError(JsonReshapeError):
    key = "error.export"


class IndexBuildError(JsonReshapeError):
    key = "error.index_build"
