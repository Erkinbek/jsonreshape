"""jsonreshape — viewer, editor and reshaper for huge JSON files.

The package is also a library (``docs/API.md``). The names in :data:`__all__` are the
stable top-level API; each one is an alias of a class or function defined in a submodule
(``jsonreshape.Document`` is ``jsonreshape.reader.Document``, ``jsonreshape.apply_mapping``
is ``jsonreshape.engine.apply``). They are resolved lazily (PEP 562 ``__getattr__``): the
defining module is imported on first use, so ``import jsonreshape`` stays as cheap as the
version number (the CLI and the GUI import only what they need). Everything that starts
with an underscore is private.
"""

from __future__ import annotations

__version__ = "0.1.0"

# public name -> (defining module, name there); documented in docs/API.md "Stability"
_LAZY: dict[str, tuple[str, str]] = {
    # reading
    "Document": ("reader", "Document"),
    "Node": ("reader", "Node"),
    "FormatInfo": ("reader", "FormatInfo"),
    # record sets, filter, sort
    "RecordSet": ("recordset", "RecordSet"),
    "filter_records": ("recordset", "filter_records"),
    "sort_records": ("recordset", "sort_records"),
    "SortKey": ("sortkeys", "SortKey"),
    "compile_filter": ("query", "compile_filter"),
    "SearchOptions": ("search", "SearchOptions"),
    # export
    "ExportOptions": ("writer", "ExportOptions"),
    "ExportReport": ("writer", "ExportReport"),
    "export": ("writer", "export"),
    "discover_columns": ("writer", "discover_columns"),
    # editing
    "EditSession": ("edits", "EditSession"),
    "Ref": ("edits", "Ref"),
    # mapping
    "Designer": ("mapping", "Designer"),
    "infer_document": ("mapping", "infer_document"),
    "load_mapping": ("mapping", "load_mapping"),
    "save_mapping": ("mapping", "save_mapping"),
    "ApplyOptions": ("engine", "ApplyOptions"),
    "apply_mapping": ("engine", "apply"),
    # tools
    "field_stats": ("stats", "field_stats"),
    "find_duplicates": ("dupes", "find_duplicates"),
    "generate_schema": ("jschema", "generate_schema"),
    "compile_schema": ("jschema", "compile_schema"),
    "load_schema": ("jschema", "load_schema"),
    "repair_file": ("repair", "repair_file"),
    "diff_files": ("diff", "diff_files"),
    # settings, tasks, errors, language
    "Settings": ("settings", "Settings"),
    "get_settings": ("settings", "get_settings"),
    "set_settings": ("settings", "set_settings"),
    "Progress": ("tasks", "Progress"),
    "ProgressInfo": ("tasks", "ProgressInfo"),
    "CancelToken": ("tasks", "CancelToken"),
    "BackgroundTask": ("tasks", "BackgroundTask"),
    "JsonReshapeError": ("errors", "JsonReshapeError"),
    "Cancelled": ("errors", "Cancelled"),
    "UserInputError": ("errors", "UserInputError"),
    "QuerySyntaxError": ("errors", "QuerySyntaxError"),
    "PathSyntaxError": ("errors", "PathSyntaxError"),
    "JsonSyntaxError": ("errors", "JsonSyntaxError"),
    "FileFormatError": ("errors", "FileFormatError"),
    "FileChangedError": ("errors", "FileChangedError"),
    "TooLargeError": ("errors", "TooLargeError"),
    "ExportError": ("errors", "ExportError"),
    "EditError": ("errors", "EditError"),
    "set_language": ("i18n", "set_language"),
    "get_language": ("i18n", "get_language"),
}

__all__ = ["__version__", *_LAZY]


def __getattr__(name: str) -> object:
    try:
        module_name, attr = _LAZY[name]
    except KeyError:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}") from None
    import importlib  # noqa: WPS433 -- only when a lazy name is first used

    value = getattr(importlib.import_module(f"{__name__}.{module_name}"), attr)
    globals()[name] = value  # resolved once; later accesses are plain attribute reads
    return value


def __dir__() -> list[str]:
    return sorted(set(globals()) | set(_LAZY))


TYPE_CHECKING = False  # ``typing`` costs ~6 ms to import; type checkers know this constant
if TYPE_CHECKING:  # the same names for type checkers and IDEs (no runtime cost)
    from .diff import diff_files
    from .dupes import find_duplicates
    from .edits import EditSession, Ref
    from .engine import ApplyOptions
    from .engine import apply as apply_mapping
    from .errors import (
        Cancelled,
        EditError,
        ExportError,
        FileChangedError,
        FileFormatError,
        JsonReshapeError,
        JsonSyntaxError,
        PathSyntaxError,
        QuerySyntaxError,
        TooLargeError,
        UserInputError,
    )
    from .i18n import get_language, set_language
    from .jschema import compile_schema, generate_schema, load_schema
    from .mapping import Designer, infer_document, load_mapping, save_mapping
    from .query import compile_filter
    from .reader import Document, FormatInfo, Node
    from .recordset import RecordSet, filter_records, sort_records
    from .repair import repair_file
    from .search import SearchOptions
    from .settings import Settings, get_settings, set_settings
    from .sortkeys import SortKey
    from .stats import field_stats
    from .tasks import BackgroundTask, CancelToken, Progress, ProgressInfo
    from .writer import ExportOptions, ExportReport, discover_columns, export
