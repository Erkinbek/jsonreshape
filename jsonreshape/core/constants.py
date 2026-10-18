"""Constants shared by both core backends (mirrored in rust/src/lib.rs).

A test checks that the Rust module exports the same values.
"""

from __future__ import annotations

# Scanner modes
MODE_CONTAINER = 1
MODE_BRACE_LIST = 2
MODE_LINES = 3
MODE_STREAM = 4
MODE_SINGLE = 5

MODES = (MODE_CONTAINER, MODE_BRACE_LIST, MODE_LINES, MODE_STREAM, MODE_SINGLE)

# Scanner flags
FLAG_ESCAPES = 1
FLAG_U_ESCAPES = 2
FLAG_NON_ASCII = 4

DEFAULT_MAX_DEPTH = 100000

# Error codes
ERR_UNEXPECTED_CHAR = "unexpected_char"
ERR_UNEXPECTED_EOF = "unexpected_eof"
ERR_INVALID_NUMBER = "invalid_number"
ERR_INVALID_LITERAL = "invalid_literal"
ERR_INVALID_ESCAPE = "invalid_escape"
ERR_INVALID_HEX = "invalid_hex"
ERR_CONTROL_CHAR = "control_char"
ERR_INVALID_UTF8 = "invalid_utf8"
ERR_TRAILING_DATA = "trailing_data"
ERR_DEPTH_LIMIT = "depth_limit"
ERR_TARGET_NOT_FOUND = "target_not_found"
ERR_TARGET_NOT_CONTAINER = "target_not_container"

ERROR_CODES = (
    ERR_UNEXPECTED_CHAR,
    ERR_UNEXPECTED_EOF,
    ERR_INVALID_NUMBER,
    ERR_INVALID_LITERAL,
    ERR_INVALID_ESCAPE,
    ERR_INVALID_HEX,
    ERR_CONTROL_CHAR,
    ERR_INVALID_UTF8,
    ERR_TRAILING_DATA,
    ERR_DEPTH_LIMIT,
    ERR_TARGET_NOT_FOUND,
    ERR_TARGET_NOT_CONTAINER,
)

# "expected" descriptors reported with errors
EXPECTED_CODES = (
    "value",
    "value_or_end",
    "key_or_end",
    "key",
    "colon",
    "comma_or_bracket",
    "comma_or_brace",
    "open_brace",
    "end",
    "digit",
    "hex",
    "escape",
    "string_end",
    "literal",
    "none",
)

# PathExtractor segment tags
SEG_KEY = "key"
SEG_INDEX = "index"
SEG_ANY_INDEX = "any_index"
SEG_ANY_KEY = "any_key"
