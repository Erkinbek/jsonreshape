"""jsonreshape.tasks: cancellation, progress (%, rate, ETA, throttling, child ranges),
background tasks and the size/duration helpers (AGENT_PROMPT §1.2: every long operation
runs in the background with progress and "Bekor qilish")."""

from __future__ import annotations

import logging
import threading
import time

import pytest

from jsonreshape.errors import Cancelled, JsonReshapeError
from jsonreshape.tasks import (
    BackgroundTask,
    CancelToken,
    Progress,
    ProgressInfo,
    check,
    format_bytes,
    format_duration,
    parse_size,
)


# ----------------------------------------------------------------------------------
# CancelToken
# ----------------------------------------------------------------------------------
def test_cancel_token() -> None:
    tok = CancelToken()
    assert not tok.cancelled
    tok.check()
    check(tok)
    check(None)  # None is allowed everywhere
    assert tok.wait(0.01) is False
    tok.cancel()
    assert tok.cancelled
    assert tok.wait(0) is True
    with pytest.raises(Cancelled) as info:
        tok.check()
    assert isinstance(info.value, JsonReshapeError)
    assert info.value.key == "error.cancelled"
    with pytest.raises(Cancelled):
        check(tok)
    tok.cancel()  # idempotent
    assert tok.cancelled


def test_cancel_from_another_thread() -> None:
    tok = CancelToken()
    started = threading.Event()

    def worker() -> str:
        started.set()
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            try:
                tok.check()
            except Cancelled:
                return "cancelled"
            time.sleep(0.001)
        return "timeout"

    result: list[str] = []
    th = threading.Thread(target=lambda: result.append(worker()))
    th.start()
    started.wait(5)
    tok.cancel()
    th.join(10)
    assert result == ["cancelled"]


def test_cancelled_message_is_localized(lang_uz) -> None:
    assert str(Cancelled()) == "Amal bekor qilindi."


# ----------------------------------------------------------------------------------
# Progress
# ----------------------------------------------------------------------------------
def test_progress_info_fraction_rate_eta() -> None:
    p = Progress(1000, unit="bytes", message="scan", min_interval=0)
    info = p.info()
    assert isinstance(info, ProgressInfo)
    assert info.done == 0 and info.total == 1000 and info.fraction == 0.0 and info.eta is None
    time.sleep(0.02)
    p.update(250)
    info = p.info()
    assert info.done == 250 and info.fraction == pytest.approx(0.25)
    assert info.rate > 0 and info.eta is not None and info.eta > 0
    assert info.message == "scan" and info.unit == "bytes" and not info.finished
    p.advance(250)
    assert p.info().done == 500
    p.set_total(2000)
    assert p.info().fraction == pytest.approx(0.25)
    p.set_message("sort")
    assert p.info().message == "sort"
    p.finish()
    info = p.info()
    assert info.finished and info.done == 2000 and info.fraction == 1.0 and info.eta == 0.0
    p.update(5000)  # overshoot is clamped in the fraction
    assert p.info().fraction == 1.0


def test_progress_without_total() -> None:
    p = Progress(min_interval=0)
    p.advance(10)
    info = p.info()
    assert info.total is None and info.fraction is None and info.eta is None and info.done == 10
    p.finish()
    assert p.info().finished and p.info().done == 10


def test_progress_callback_throttling() -> None:
    seen: list[ProgressInfo] = []
    p = Progress(100, callback=seen.append, min_interval=10.0)
    for i in range(1, 101):
        p.update(i)
    throttled = len(seen)
    assert throttled <= 1  # at most one callback within the interval
    p.set_message("x")  # forced
    p.finish()  # forced
    assert len(seen) == throttled + 2
    assert seen[-1].finished and seen[-1].done == 100

    unthrottled: list[ProgressInfo] = []
    q = Progress(10, callback=unthrottled.append, min_interval=0)
    for _ in range(10):
        q.advance(1)
    assert [x.done for x in unthrottled] == list(range(1, 11))


def test_progress_callback_errors_do_not_break_the_worker(caplog: pytest.LogCaptureFixture) -> None:
    def boom(info: ProgressInfo) -> None:
        raise RuntimeError("ui gone")

    p = Progress(10, callback=boom, min_interval=0)
    with caplog.at_level(logging.ERROR, logger="jsonreshape.tasks"):
        p.update(5)
        p.finish()
    assert any("progress callback failed" in r.getMessage() for r in caplog.records)


def test_progress_child_maps_into_parent_range() -> None:
    parent = Progress(1000, min_interval=0)
    first = parent.child(0.0, 0.5, total=200)
    first.update(100)
    assert parent.info().done == 250
    first.finish()
    assert parent.info().done == 500
    second = parent.child(0.5, 1.0, total=10)
    assert second.unit == parent.unit and second.message == parent.message
    second.update(5)
    assert parent.info().done == 750
    second.finish()
    assert parent.info().done == 1000
    # a child without a total does not move the parent
    third = parent.child(0.0, 0.1)
    third.advance(5)
    assert parent.info().done == 1000


def test_progress_thread_safety() -> None:
    p = Progress(min_interval=0)

    def work() -> None:
        for _ in range(10000):
            p.advance(1)

    threads = [threading.Thread(target=work) for _ in range(8)]
    for th in threads:
        th.start()
    for th in threads:
        th.join()
    assert p.info().done == 80000


def test_restart_clock() -> None:
    p = Progress(100, min_interval=0)
    time.sleep(0.02)
    before = p.info().elapsed
    p.restart_clock()
    assert p.info().elapsed < before


# ----------------------------------------------------------------------------------
# BackgroundTask
# ----------------------------------------------------------------------------------
def test_background_task_result_and_kwargs() -> None:
    received: dict[str, object] = {}

    def fn(a: int, b: int, *, scale: int, progress: Progress, cancel: CancelToken) -> int:
        received["progress"] = progress
        received["cancel"] = cancel
        received["thread"] = threading.current_thread().name
        progress.set_total(10)
        progress.update(10)
        return (a + b) * scale

    done_calls: list[BackgroundTask] = []
    task = BackgroundTask(fn, 2, 3, name="adder", scale=10)
    task.on_done = done_calls.append
    assert not task.done
    assert task.start() is task
    assert task.join(10)
    assert task.done and task.result() == 50 and task.exception is None
    assert received["progress"] is task.progress and received["cancel"] is task.cancel_token
    assert received["thread"] == "jsonreshape-adder"
    assert task.progress.info().done == 10
    deadline = time.monotonic() + 5
    while not done_calls and time.monotonic() < deadline:
        time.sleep(0.005)
    assert done_calls == [task]
    with pytest.raises(RuntimeError):
        task.start()


def test_background_task_uses_given_progress_and_token() -> None:
    progress = Progress(5)
    token = CancelToken()
    task = BackgroundTask(lambda progress, cancel: (progress, cancel), progress=progress, cancel=token).start()
    assert task.result(10) == (progress, token)


def test_background_task_exception(caplog: pytest.LogCaptureFixture) -> None:
    def fail(progress: Progress, cancel: CancelToken) -> None:
        raise ValueError("bad input")

    with caplog.at_level(logging.ERROR, logger="jsonreshape.tasks"):
        task = BackgroundTask(fail, name="failing").start()
        assert task.join(10)
    assert isinstance(task.exception, ValueError)
    with pytest.raises(ValueError, match="bad input"):
        task.result()
    assert any("failing" in r.getMessage() for r in caplog.records)


def test_background_task_cancel(caplog: pytest.LogCaptureFixture) -> None:
    started = threading.Event()

    def loop(progress: Progress, cancel: CancelToken) -> str:
        started.set()
        while True:
            cancel.check()
            time.sleep(0.001)

    with caplog.at_level(logging.ERROR, logger="jsonreshape.tasks"):
        task = BackgroundTask(loop, name="looping").start()
        assert started.wait(10)
        with pytest.raises(TimeoutError):
            task.result(timeout=0.01)
        task.cancel()
        assert task.join(10)
    assert isinstance(task.exception, Cancelled)
    with pytest.raises(Cancelled):
        task.result()
    assert not caplog.records  # cancellation is not an error


def test_on_done_errors_are_logged(caplog: pytest.LogCaptureFixture) -> None:
    task = BackgroundTask(lambda progress, cancel: 1)

    def bad_callback(_task: BackgroundTask) -> None:
        raise RuntimeError("callback failed")

    task.on_done = bad_callback
    with caplog.at_level(logging.ERROR, logger="jsonreshape.tasks"):
        task.start()
        assert task.join(10)
        deadline = time.monotonic() + 5
        while not caplog.records and time.monotonic() < deadline:
            time.sleep(0.005)
    assert task.result() == 1
    assert any("on_done" in r.getMessage() for r in caplog.records)


# ----------------------------------------------------------------------------------
# helpers
# ----------------------------------------------------------------------------------
@pytest.mark.parametrize(
    "n, text",
    [(0, "0 B"), (999, "999 B"), (1000, "1.0 KB"), (1500, "1.5 KB"), (2_000_000, "2.0 MB"),
     (5 * 10**9, "5.0 GB"), (10**12, "1.0 TB"), (10**16, "10000.0 TB"), (512.7, "512 B")],
)
def test_format_bytes(n: float, text: str) -> None:
    assert format_bytes(n) == text


@pytest.mark.parametrize(
    "seconds, text",
    [(None, "?"), (-3, "0.0 s"), (0, "0.0 s"), (5.25, "5.2 s"), (9.99, "10.0 s"), (42, "42 s"), (59.4, "59 s"),
     (60, "1:00"), (125, "2:05"), (3599, "59:59"), (3600, "1:00:00"), (3725, "1:02:05"), (90000, "25:00:00")],
)
def test_format_duration(seconds: float | None, text: str) -> None:
    assert format_duration(seconds) == text


@pytest.mark.parametrize(
    "text, value",
    [("1048576", 1048576), ("0", 0), ("500MB", 500 << 20), ("500 mb", 500 << 20), ("2 GB", 2 << 30),
     ("64k", 64 << 10), ("64KB", 64 << 10), ("1.5G", int(1.5 * (1 << 30))), ("1T", 1 << 40), ("10B", 10),
     (" 7 M ", 7 << 20), ("1e3", 1000)],
)
def test_parse_size(text: str, value: int) -> None:
    assert parse_size(text) == value


@pytest.mark.parametrize("text", ["", "   ", "abc", "MB", "-5", "-1GB", "inf", "nan", "1e400", "1e300TB", "5 XB"])
def test_parse_size_rejects_invalid(text: str) -> None:
    with pytest.raises(ValueError):
        parse_size(text)


def test_parse_size_is_exact_above_two_to_the_53() -> None:
    """Sizes are parsed with decimal arithmetic: no float rounding of big byte counts."""
    assert parse_size("9007199254740993") == 9007199254740993
    assert parse_size("8191.999999999999999TB") == int(8191999999999999999 * (1 << 40) // 10**15)
    assert parse_size("0.5K") == 512
