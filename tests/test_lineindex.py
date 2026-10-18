"""Qt-free logic of the raw view (jsonreshape/gui/rawdata.py; ARCHITECTURE §11.7 "Raw").

* LineIndex: line_of / line_start against a naive count on random files (hypothesis, tiny
  checkpoints and blocks so every boundary case is hit), partial (cancelled) builds that
  answer for the counted part and resume, files without / with a trailing newline, the
  per-document cache;
* the row rule: rows found by walking from the file start equal ``row_start_at`` for every
  offset, walking from any row start reproduces the same rows (scrolling up and down
  agree), rows are bounded, end only at newlines inside, and never split a valid UTF-8
  character; real constants on a long single line;
* decode_row / display_text: exact char -> byte maps, invalid bytes as U+FFFD, controls
  visible.
"""

from __future__ import annotations

import bisect
import gc
import io
import random

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from jsonreshape.errors import Cancelled
from jsonreshape.gui import rawdata
from jsonreshape.gui.rawdata import (
    LineIndex,
    RowRule,
    decode_row,
    display_text,
    is_control,
    line_index_for,
    row_start_at,
    rows_back,
    walk_rows,
)
from jsonreshape.tasks import CancelToken, Progress


# ----------------------------------------------------------------------------------
# helpers
# ----------------------------------------------------------------------------------
def naive_line_starts(data: bytes) -> list[int]:
    return [0] + [i + 1 for i, b in enumerate(data) if b == 0x0A]


def naive_line_of(data: bytes, offset: int) -> int:
    return data.count(b"\n", 0, offset) + 1


class CountingFile(io.BytesIO):
    """BytesIO that records the bytes read."""

    def __init__(self, data: bytes) -> None:
        super().__init__(data)
        self.reads: list[int] = []

    def read(self, n: int = -1) -> bytes:  # type: ignore[override]
        out = super().read(n)
        self.reads.append(len(out))
        return out


chunks = st.lists(st.sampled_from([b"a", b"bc", b"\n", b"\n\n", b"x" * 13, "Ж".encode(), b"{\"k\": 1}\n"]),
                  max_size=120)


# ----------------------------------------------------------------------------------
# LineIndex
# ----------------------------------------------------------------------------------
@settings(max_examples=150, deadline=None, suppress_health_check=[HealthCheck.too_slow])
@given(parts=chunks, step=st.integers(1, 17), block=st.integers(1, 40))
def test_line_index_matches_naive_count(parts, step, block) -> None:
    data = b"".join(parts)
    idx = LineIndex(len(data), checkpoint_bytes=step)
    f = io.BytesIO(data)
    idx.build(f, block_bytes=block)
    assert idx.complete and not idx.building
    starts = naive_line_starts(data)
    assert idx.line_count == len(starts)
    for line, start in enumerate(starts, start=1):
        assert idx.line_start(line, f) == start
    for offset in range(len(data) + 1):
        assert idx.line_of(offset, f) == naive_line_of(data, offset)
    with pytest.raises(ValueError):
        idx.line_start(len(starts) + 1, f)
    with pytest.raises(ValueError):
        idx.line_start(0, f)
    # checkpoints: (k * step, line containing that byte)
    for off, line in idx.checkpoints():
        assert line == naive_line_of(data, off)


def test_line_index_reads_are_bounded_by_one_checkpoint() -> None:
    rng = random.Random(5)
    data = b"".join(rng.choice([b"abc", b"\n", b"defgh\n", b"q" * 50]) for _ in range(20000))
    idx = LineIndex(len(data), checkpoint_bytes=1024)
    idx.build(io.BytesIO(data), block_bytes=8192)
    starts = naive_line_starts(data)
    for line in rng.sample(range(1, len(starts) + 1), 200):
        f = CountingFile(data)
        assert idx.line_start(line, f) == starts[line - 1]
        assert sum(f.reads) <= 1024
    for offset in rng.sample(range(len(data)), 200):
        f = CountingFile(data)
        assert idx.line_of(offset, f) == naive_line_of(data, offset)
        assert sum(f.reads) <= 1024


def test_line_index_partial_build_answers_for_the_counted_part_and_resumes() -> None:
    data = b"".join(b"line %d\n" % i for i in range(5000))
    starts = naive_line_starts(data)
    idx = LineIndex(len(data), checkpoint_bytes=500)
    token = CancelToken()
    seen: list[tuple[int, int]] = []

    def on_block(built: int, lines: int) -> None:
        seen.append((built, lines))
        if len(seen) == 3:
            token.cancel()

    progress = Progress()
    with pytest.raises(Cancelled):
        idx.build(io.BytesIO(data), block_bytes=2000, cancel=token, on_block=on_block, progress=progress)
    assert not idx.complete and not idx.building
    assert idx.built == 6000 and idx.line_count is None
    known = idx.known_lines()
    assert known == naive_line_of(data, 6000)
    f = io.BytesIO(data)
    for line in range(1, known + 1):
        assert idx.line_start(line, f) == starts[line - 1]
    assert idx.covers_offset(6000) and not idx.covers_offset(6001)
    with pytest.raises(ValueError):
        idx.line_start(known + 1, f)
    with pytest.raises(ValueError):
        idx.line_of(7000, f)
    assert 0 < idx.fraction() < 1
    # resume from the last checkpoint
    idx.build(io.BytesIO(data), block_bytes=2000)
    assert idx.complete and idx.line_count == len(starts)
    for line in range(1, len(starts) + 1, 97):
        assert idx.line_start(line, f) == starts[line - 1]


def test_line_index_edge_files() -> None:
    for data, lines in ((b"", 1), (b"\n", 2), (b"abc", 1), (b"a\nb", 2), (b"a\nb\n", 3), (b"\n\n\n", 4)):
        idx = LineIndex(len(data), checkpoint_bytes=2)
        f = io.BytesIO(data)
        idx.build(f)
        assert idx.complete and idx.line_count == lines, data
        assert idx.line_start(lines, f) == naive_line_starts(data)[-1]
        assert idx.line_of(len(data), f) == lines


def test_line_index_single_build_at_a_time() -> None:
    idx = LineIndex(10)
    assert idx.try_begin()
    assert not idx.try_begin()
    with pytest.raises(RuntimeError):
        idx.build(io.BytesIO(b"0123456789"))
    idx.end()
    idx.build(io.BytesIO(b"0123456789"))
    assert idx.complete and not idx.try_begin()


def test_line_index_short_file_is_reported_as_changed() -> None:
    from jsonreshape.errors import FileChangedError

    idx = LineIndex(100, checkpoint_bytes=10)
    with pytest.raises(FileChangedError):
        idx.build(io.BytesIO(b"x" * 50))
    assert not idx.building


def test_line_index_cache_is_per_document() -> None:
    class Doc:
        def __init__(self, size: int) -> None:
            self.data_size = size

    a, b = Doc(10), Doc(20)
    ia = line_index_for(a)
    assert line_index_for(a) is ia
    assert line_index_for(b) is not ia and line_index_for(b).size == 20
    del a
    gc.collect()
    assert all(i is not ia for i in rawdata._INDEXES.values())


# ----------------------------------------------------------------------------------
# the row rule
# ----------------------------------------------------------------------------------
SMALL = RowRule(8, 32)       # W = 8, A = 32: every case of the rule appears in short inputs


def check_rows(data: bytes, rule: RowRule, *, sample: int = 25) -> list[int]:
    size = len(data)
    w = rule.w
    rows = walk_rows(data, 0, 0, size, 10 ** 9, rule)
    if size == 0:
        assert rows == [0]
        return rows
    assert rows[0] == 0 and rows[-1] == size
    for a, b in zip(rows, rows[1:]):
        assert a < b <= a + w + 3
        nl = data.find(b"\n", a, b)
        assert nl in (-1, b - 1)                 # a newline only ends a row
        if b < size and data[b - 1] == 0x0A:
            assert b in rows
    # every line start is a row start
    for s in naive_line_starts(data):
        if s < size:
            assert s in rows
    # row_start_at agrees with the walk for every offset
    for x in range(size):
        assert row_start_at(data, 0, x, size, rule) == rows[bisect.bisect_right(rows, x) - 1]
    # walking from any row start continues identically; stepping back returns the rows
    rng = random.Random(size)
    for i in rng.sample(range(len(rows) - 1), min(sample, len(rows) - 1)):
        part = walk_rows(data, 0, rows[i], size, 40, rule)
        assert part == rows[i:i + len(part)]
        k = min(i, 7)
        assert rows_back(data, 0, rows[i], size, k, rule) == rows[i - k]
    return rows


utf8_parts = st.lists(st.sampled_from([
    b"a", b"\n", b"\r\n", b"\t", "oʻ".encode(), "Жук".encode(), "\U0001F600".encode(),
    b"z" * 9, b"{\"k\": [1, 2]}", "é".encode()]), max_size=150)


@settings(max_examples=200, deadline=None)
@given(parts=utf8_parts, w=st.sampled_from([4, 5, 8, 13]))
def test_rows_on_valid_utf8_never_split_a_character(parts, w) -> None:
    data = b"".join(parts)
    rows = check_rows(data, RowRule(w, 4 * w))
    for r in rows[:-1]:
        assert not 0x80 <= data[r] < 0xC0         # a row starts at a character start
    text = "".join(decode_row(data[a:b])[0] for a, b in zip(rows, rows[1:]))
    assert text == data.decode("utf-8") and "\ufffd" not in text


@settings(max_examples=150, deadline=None)
@given(data=st.binary(max_size=300))
def test_rows_on_arbitrary_bytes(data) -> None:
    check_rows(data, SMALL)


def test_long_lines_switch_to_the_absolute_grid() -> None:
    data = b"x" * 5 + b"\n" + b"y" * 200 + b"\n" + b"z" * 3
    rows = check_rows(data, SMALL)
    line2 = 6
    # measured from the line start up to A = 32 bytes, then multiples of 8
    assert [r for r in rows if line2 <= r <= line2 + 32] == list(range(line2, line2 + 33, 8))
    assert all(r % 8 == 0 for r in rows if line2 + 32 < r < 207)


def test_row_rule_for_width() -> None:
    assert RowRule.for_width(100) == RowRule(100, 6400)
    assert RowRule.for_width(3).w == rawdata.MIN_ROW_BYTES
    assert RowRule.for_width(10 ** 6).w == rawdata.MAX_ROW_BYTES
    assert rawdata.DEFAULT_RULE == RowRule(rawdata.ROW_BYTES, rawdata.ANCHOR_BYTES)
    assert rawdata.DEFAULT_RULE.lookback == rawdata.LOOKBACK


@pytest.mark.parametrize("w", [32, 77, 256, 1024])
def test_window_widths_on_a_minified_line(w) -> None:
    rule = RowRule.for_width(w)
    rng = random.Random(w)
    pieces = ['{"k":"oʻzbek","v":[1,2.5e10],"t":"Жук \U0001F600"}', ",", "123",
              '"été"', "\n"]
    text = "[" + "".join(rng.choice(pieces) for _ in range(20000)) + "]"
    data = text.encode()
    size = len(data)
    rows = walk_rows(data, 0, 0, size, 10 ** 9, rule)
    assert rows[-1] == size
    assert max(b - a for a, b in zip(rows, rows[1:])) <= rule.w + 3
    assert all(not 0x80 <= data[r] < 0xC0 for r in rows[:-1])
    for x in rng.sample(range(size), 300):
        assert row_start_at(data, 0, x, size, rule) == rows[bisect.bisect_right(rows, x) - 1]
    # a window buffer (as the view uses it) gives the same answers as the whole file
    for x in rng.sample(range(size), 100):
        lo = max(0, x - rule.lookback - 8)
        assert row_start_at(data[lo:x + 8], lo, x, size, rule) == row_start_at(data, 0, x, size, rule)
        r = row_start_at(data, 0, x, size, rule)
        lo = max(0, r - rawdata.need_back(5, rule))
        hi = min(size, r + rawdata.need_forward(5, rule))
        assert walk_rows(data[lo:hi], lo, r, size, 5, rule) == walk_rows(data, 0, r, size, 5, rule)
        assert rows_back(data[lo:r + 8], lo, r, size, 5, rule) == rows_back(data, 0, r, size, 5, rule)


# ----------------------------------------------------------------------------------
# decoding
# ----------------------------------------------------------------------------------
def test_decode_row_maps_characters_to_bytes() -> None:
    text, starts = decode_row(b"abc")
    assert text == "abc" and starts is None
    data = "oʻЖ\U0001F600x".encode()
    text, starts = decode_row(data)
    assert text == "oʻЖ\U0001F600x"
    assert starts == [0, 1, 3, 5, 9, 10]
    text, starts = decode_row(b"a\xff\xe2\x82b\xc3\xa9")
    assert text == "a\ufffd\ufffd\ufffdbé"
    assert starts == [0, 1, 2, 3, 4, 5, 7]
    # a truncated character at a row end (invalid data) stays one U+FFFD per byte
    text, starts = decode_row("Ж".encode()[:1])
    assert text == "\ufffd" and starts == [0, 1]


def test_display_text_keeps_length_and_shows_controls() -> None:
    raw = "a\tb\rc\x00\x7f\u2028\ufeffd"
    shown = display_text(raw)
    assert len(shown) == len(raw)
    assert shown[1] == "→" and shown[0] == "a" and shown[-1] == "d"
    assert all(ch == "·" for ch in shown[3:4] + shown[5:9])
    assert is_control("\t") and is_control("\x1b") and not is_control("a") and not is_control("Ж")
