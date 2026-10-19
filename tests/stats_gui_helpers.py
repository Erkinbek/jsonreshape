"""Shared fixtures / helpers of tests/test_gui_statspanel.py and tests/test_gui_dupesdialog.py: a headless
:class:`~jsonreshape.gui.context.AppContext`, real :class:`~jsonreshape.gui.session.DocSession` objects opened on
small test files (exact number tokens, Uzbek texts, missing / null / empty values, multi-valued fields), the core
backend switch (``backend``), and brute-force references (``count_matching``).
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from jsonreshape import dupes, reader, stats
from jsonreshape.gui.context import AppContext
from jsonreshape.gui.guistate import GuiState
from jsonreshape.gui.session import DocSession
from jsonreshape.query import compile_filter
from jsonreshape.recordset import filter_records

TIMEOUT = 30000

CITIES = ["Toshkent", "Samarqand", "Ўзбекистон", "O'zbekiston"]
SCORES = ["0.1", "1e400", "12345678901234567890", "-0.0", "1.10"]   # exact tokens, 12 records each
SUMS = ["44.83", "1.10", "7"]
NOTES = {1: '""', 2: '"x"', 3: '"xx"', 4: '"xxx"'}                      # i % 6: 0 -> null, 5 -> missing
MIXES = ['"abc"', "42", "[1, 2, 3]"]                                    # i % 3: string / number / array


def record_text(i: int) -> str:
    """Record ``i`` of :func:`write_records` as JSON text (number tokens exactly as written)."""
    parts = [f'"id": {i}', f'"city": {json.dumps(CITIES[i % 4], ensure_ascii=False)}', f'"score": {SCORES[i % 5]}']
    if i % 6 == 0:
        parts.append('"note": null')
    elif i % 6 != 5:
        parts.append(f'"note": {NOTES[i % 6]}')
    orders = ", ".join(f'{{"sum": {SUMS[(i + k) % 3]}}}' for k in range(i % 3))
    parts.append(f'"orders": [{orders}]')
    parts.append(f'"mix": {MIXES[i % 3]}')
    parts.append('"tags": ["a", "bb"]' if i % 2 else '"tags": ["ccc"]')
    parts.append(f'"code": {json.dumps("C" + str(i % 7))}')
    return "{" + ", ".join(parts) + "}"


def write_records(path: Path, n: int = 60) -> Path:
    """An array document of ``n`` records (``record_text``), pretty enough to have line breaks."""
    path.write_text("[\n" + ",\n".join("  " + record_text(i) for i in range(n)) + "\n]\n", encoding="utf-8")
    return path


def write_keyed(path: Path, n: int = 12) -> Path:
    """A keyed document ``{"k0": {...}, ...}`` whose values repeat every 4 records."""
    body = ", ".join(f'"k{i}": {{"grp": {i % 4}, "name": {json.dumps(CITIES[i % 4], ensure_ascii=False)}}}'
                     for i in range(n))
    path.write_text("{" + body + "}", encoding="utf-8")
    return path


@pytest.fixture
def app(qapp, tmp_path: Path):
    ctx = AppContext(state=GuiState.load(tmp_path / "gui_state.json"), headless=True)
    ctx.tasks.set_poll_interval(10)
    ctx.errors = []
    ctx.statuses = []
    ctx.error_reported.connect(ctx.errors.append)
    ctx.status_message.connect(lambda text, _ms: ctx.statuses.append(text))
    yield ctx
    assert ctx.tasks.shutdown(30)


@pytest.fixture
def opened(qtbot, app):
    """``opened(path, edit=False, **kw) -> DocSession`` (index finished; ``edit``: wait for the edit controller)."""
    made: list[DocSession] = []

    def open_file(path: Path, *, edit: bool = False, **kw: Any) -> DocSession:
        kw.setdefault("use_cache", False)
        s = DocSession(app, path, **kw)
        made.append(s)
        with qtbot.waitSignal(s.index_finished, timeout=TIMEOUT):
            s.start()
        if edit:
            assert s.edit is not None
            qtbot.waitUntil(lambda: s.edit.available, timeout=TIMEOUT)
        return s

    yield open_file
    for s in made:
        s.close()
    assert app.tasks.wait_idle(30)
    assert not app.errors, app.errors


@pytest.fixture
def backend(core_backend, monkeypatch):
    """Documents use this core backend's Scanner; the statistics and the duplicate finder its PathExtractor."""
    name = "rust" if core_backend.__name__.endswith("_rscore") else "python"
    old = reader.set_scanner_backend(name)
    monkeypatch.setattr(stats, "_EXTRACTOR", core_backend.PathExtractor)
    monkeypatch.setattr(dupes, "_EXTRACTOR", core_backend.PathExtractor)
    yield name
    reader.set_scanner_backend(old)


def count_matching(session: DocSession, expr: str) -> int:
    """Records of the session's document (the state shown) matching ``expr`` (brute force)."""
    doc = session.doc.pinned()
    rs = filter_records(doc, compile_filter(expr))
    try:
        return len(rs)
    finally:
        rs.close()


def wait_view(qtbot: Any, session: DocSession, count: int) -> None:
    """Wait until the session's filter pipeline finished with ``count`` records in the view."""
    qtbot.waitUntil(lambda: not session.view_busy and session.view_len() == count, timeout=TIMEOUT)


def reset(qtbot: Any, session: DocSession) -> None:
    session.reset_view()
    qtbot.waitUntil(lambda: not session.view_busy and session.view_is_base, timeout=TIMEOUT)


#: (format, write options, open options) of :func:`write_kind`: every document kind and the encodings
KIND_CASES = [
    ("jsonl", {"newline": "\r\n"}, {}),
    ("wrapped", {"style": "pretty"}, {}),
    ("stream", {"style": "escaped"}, {"format": "stream"}),
    ("array", {"encoding": "utf-16-le", "bom": True}, {}),
    ("jsonl", {"gz": True}, {}),
    ("brace_list", {}, {}),
    ("keyed", {}, {}),
]


def write_kind(tmp_path: Path, fmt: str, opts: dict, n: int = 24) -> Path:
    """A document of kind ``fmt`` holding ``{"id", "city", "v"}`` records (``v``: the exact score tokens)."""
    import jsongen

    recs = [{"id": i, "city": CITIES[i % 4], "v": jsongen.number_from_token(SCORES[i % 5])} for i in range(n)]
    path = tmp_path / ("doc.json.gz" if opts.get("gz") else "doc.json")
    jsongen.write_file(path, recs, fmt, **opts)
    return path
