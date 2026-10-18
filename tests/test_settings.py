"""jsonreshape.settings (AGENT_PROMPT §11 "Sozlamalar") and jsonreshape.log (§11 "Loglar").

Settings: defaults exactly as docs/ARCHITECTURE.md §4, validation with localized errors,
to_dict/from_dict (unknown keys ignored), load() never failing (missing/corrupt file ->
defaults + logged warning), atomic save(), process-wide get_settings().

Logging lives here as well (no separate test module is owned by the infra role): rotating
UTF-8 log file in logs_dir(), idempotent setup, console handler, rollover that survives a
locked backup, uncaught-exception hooks for the main thread and worker threads.
"""

from __future__ import annotations

import dataclasses
import json
import logging
import os
import sys
import threading
from pathlib import Path

import pytest

from jsonreshape import appdirs
from jsonreshape import log as jlog
from jsonreshape import settings as settings_mod
from jsonreshape.errors import UserInputError
from jsonreshape.i18n import MESSAGES, set_language
from jsonreshape.settings import SETTINGS_VERSION, Settings, get_settings, set_settings

GiB = 1 << 30
MiB = 1 << 20

SPEC_DEFAULTS = {
    "sample_size": 10000,
    "cache_dir": None,
    "cache_limit_bytes": 20 * GiB,
    "memory_limit_bytes": 512 * MiB,
    "sort_memory_bytes": 256 * MiB,
    "large_node_bytes": 50 * MiB,
    "large_node_children": 10000,
    "head_bytes": 4 * MiB,
    "language": "uz",
    "export_indent": 2,
    "csv_bom": True,
    "csv_delimiter": ",",
    "array_join_sep": ";",
    "xlsx_max_rows": 1048576,
}


# ----------------------------------------------------------------------------------
# Settings
# ----------------------------------------------------------------------------------
def test_defaults_match_the_spec() -> None:
    s = Settings()
    assert s.to_dict() == SPEC_DEFAULTS
    assert [f.name for f in dataclasses.fields(Settings)] == list(SPEC_DEFAULTS)


def test_every_field_has_a_localized_label() -> None:
    for f in dataclasses.fields(Settings):
        entry = MESSAGES.get(f"settings.field.{f.name}")
        assert entry and entry.get("uz") and entry.get("en"), f.name


@pytest.mark.parametrize(
    "field, value, normalized",
    [
        ("sample_size", 1, 1),
        ("sample_size", 5.0, 5),
        ("sample_size", "250", 250),
        ("cache_limit_bytes", 0, 0),
        ("cache_limit_bytes", "20GB", 20 * GiB),
        ("memory_limit_bytes", "512 MB", 512 * MiB),
        ("large_node_bytes", 100, 100),  # tiny values are allowed (tests use them)
        ("head_bytes", "64k", 64 * 1024),
        ("cache_dir", "  ", None),
        ("cache_dir", Path("C:/cache"), str(Path("C:/cache"))),
        ("language", " EN ", "en"),
        ("export_indent", None, None),
        ("export_indent", 0, 0),
        ("export_indent", 16, 16),
        ("export_indent", "\t", "\t"),
        ("export_indent", "tab", "\t"),
        ("export_indent", "4", 4),
        ("export_indent", "compact", None),
        ("csv_bom", False, False),
        ("csv_delimiter", "\t", "\t"),
        ("csv_delimiter", ";", ";"),
        ("array_join_sep", ", ", ", "),
        ("xlsx_max_rows", 2, 2),
        ("xlsx_max_rows", 1048576, 1048576),
    ],
)
def test_valid_values_are_normalized(field: str, value, normalized) -> None:
    s = Settings(**{field: value})
    assert getattr(s, field) == normalized
    assert Settings.from_dict({field: value}, strict=True).to_dict() == s.to_dict()


@pytest.mark.parametrize(
    "field, value",
    [
        ("sample_size", 0),
        ("sample_size", -5),
        ("sample_size", True),
        ("sample_size", 1.5),
        ("sample_size", "many"),
        ("sample_size", None),
        ("cache_limit_bytes", -1),
        ("cache_limit_bytes", "lots"),
        ("memory_limit_bytes", 0),
        ("sort_memory_bytes", "inf"),
        ("large_node_children", 0),
        ("head_bytes", [1]),
        ("cache_dir", 5),
        ("language", "fr"),
        ("language", None),
        ("export_indent", 17),
        ("export_indent", -1),
        ("export_indent", True),
        ("export_indent", "  x"),
        ("csv_bom", "yes"),
        ("csv_bom", 1),
        ("csv_delimiter", ""),
        ("csv_delimiter", ";;"),
        ("csv_delimiter", '"'),
        ("csv_delimiter", "\n"),
        ("array_join_sep", ""),
        ("array_join_sep", "x" * 17),
        ("array_join_sep", "a\nb"),
        ("xlsx_max_rows", 1),
        ("xlsx_max_rows", 1048577),
    ],
)
def test_invalid_values_raise_localized_errors(field: str, value) -> None:
    with pytest.raises(UserInputError) as info:
        Settings(**{field: value})
    exc = info.value
    assert exc.key == "settings.invalid" and exc.params["name"] == field
    with pytest.raises(UserInputError):
        Settings.from_dict({field: value}, strict=True)
    assert getattr(Settings.from_dict({field: value}), field) == SPEC_DEFAULTS[field]  # lenient: default


def test_error_message_wording(lang_uz) -> None:
    with pytest.raises(UserInputError) as info:
        Settings(sample_size=0)
    assert str(info.value) == "Sozlama noto'g'ri: «Namuna hajmi (yozuvlar soni)» = 0. Kutilgan qiymat: 1 yoki undan katta butun son."
    assert info.value.hint == "Sozlamalar oynasida qiymatni tuzating."
    set_language("en")
    with pytest.raises(UserInputError) as info:
        Settings(xlsx_max_rows=5_000_000)
    assert str(info.value) == 'Invalid setting: "Maximum rows per Excel sheet" = 5000000. Expected: a whole number from 2 to 1048576.'


def test_replace_validates_and_validate_rechecks() -> None:
    s = Settings()
    t = dataclasses.replace(s, sample_size=500, language="en")
    assert t.sample_size == 500 and t.language == "en" and s.sample_size == 10000
    with pytest.raises(UserInputError):
        dataclasses.replace(s, head_bytes=0)
    s.head_bytes = -1  # direct assignment is not checked ...
    with pytest.raises(UserInputError):
        s.validate()  # ... until validate()


def test_to_dict_from_dict_round_trip(caplog: pytest.LogCaptureFixture) -> None:
    s = Settings(sample_size=123, cache_dir="D:/cache", export_indent="\t", csv_bom=False, language="en")
    d = s.to_dict()
    assert json.loads(json.dumps(d)) == d
    assert Settings.from_dict(d) == s
    with caplog.at_level(logging.DEBUG, logger="jsonreshape.settings"):
        t = Settings.from_dict({**d, "unknown_key": 1, "version": 1, "future_option": {"x": 1}})
    assert t == s
    with pytest.raises(TypeError):
        Settings.from_dict(["not", "a", "dict"])  # type: ignore[arg-type]


def test_load_missing_file_gives_defaults(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    with caplog.at_level(logging.INFO, logger="jsonreshape.settings"):
        s = Settings.load(tmp_path / "nope.json")
    assert s == Settings()
    assert any("not found" in r.getMessage() for r in caplog.records)
    assert not any(r.levelno >= logging.WARNING for r in caplog.records)  # normal on first start


@pytest.mark.parametrize(
    "content",
    [b"{not json", b"[1, 2]", b"\"text\"", b"\xff\xfe garbage", b"", b"null"],
)
def test_load_corrupt_file_gives_defaults_and_warns(tmp_path: Path, caplog: pytest.LogCaptureFixture,
                                                    content: bytes) -> None:
    p = tmp_path / "settings.json"
    p.write_bytes(content)
    with caplog.at_level(logging.WARNING, logger="jsonreshape.settings"):
        s = Settings.load(p)
    assert s == Settings()
    assert any(r.levelno == logging.WARNING for r in caplog.records)
    assert p.read_bytes() == content  # the user's file is not touched by loading


def test_load_keeps_valid_values_and_drops_invalid_ones(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    p = tmp_path / "settings.json"
    p.write_text(json.dumps({"version": 1, "sample_size": 777, "language": "xx", "csv_delimiter": "|",
                             "head_bytes": -4, "something_new": True}), encoding="utf-8")
    with caplog.at_level(logging.WARNING, logger="jsonreshape.settings"):
        s = Settings.load(p)
    assert s.sample_size == 777 and s.csv_delimiter == "|"
    assert s.language == "uz" and s.head_bytes == 4 * MiB
    warned = " ".join(r.getMessage() for r in caplog.records)
    assert "language" in warned and "head_bytes" in warned


def test_load_too_large_file(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    p = tmp_path / "settings.json"
    p.write_bytes(b'{"sample_size": 5' + b" " * (settings_mod._MAX_SETTINGS_FILE + 10) + b"}")
    with caplog.at_level(logging.WARNING, logger="jsonreshape.settings"):
        assert Settings.load(p) == Settings()
    assert caplog.records


def test_load_utf8_bom_file(tmp_path: Path) -> None:
    p = tmp_path / "settings.json"
    p.write_bytes(b"\xef\xbb\xbf" + json.dumps({"array_join_sep": " | "}).encode())
    assert Settings.load(p).array_join_sep == " | "


def test_save_is_atomic_and_round_trips(tmp_path: Path) -> None:
    s = Settings(sample_size=42, cache_dir="C:\\Кеш", export_indent=None, array_join_sep="; ")
    target = tmp_path / "sub" / "settings.json"
    assert s.save(target) == target
    assert sorted(os.listdir(target.parent)) == ["settings.json"]  # no temp file left
    raw = json.loads(target.read_text(encoding="utf-8"))
    assert raw["version"] == SETTINGS_VERSION and raw["cache_dir"] == "C:\\Кеш"
    assert "Кеш" in target.read_text(encoding="utf-8")  # stored readable, not \u-escaped
    assert Settings.load(target) == s
    s2 = dataclasses.replace(s, sample_size=43)
    s2.save(target)
    assert Settings.load(target).sample_size == 43


def test_default_location_and_process_wide_instance(_isolated_app_home: Path) -> None:
    a = get_settings()
    assert a == Settings() and get_settings() is a  # cached
    custom = dataclasses.replace(a, sample_size=999)
    path = custom.save()
    assert path == appdirs.settings_file() and path.parent == _isolated_app_home
    assert get_settings() is a  # saving does not swap the cached instance
    settings_mod._reset_for_tests()
    b = get_settings()
    assert b is not a and b.sample_size == 999
    set_settings(dataclasses.replace(b, language="en"))
    assert get_settings().language == "en"
    with pytest.raises(TypeError):
        set_settings({"language": "en"})  # type: ignore[arg-type]
    bad = Settings()
    bad.sample_size = 0
    with pytest.raises(UserInputError):
        set_settings(bad)


def test_get_settings_is_thread_safe() -> None:
    settings_mod._reset_for_tests()
    seen: list[Settings] = []
    barrier = threading.Barrier(8)

    def worker() -> None:
        barrier.wait()
        seen.append(get_settings())

    threads = [threading.Thread(target=worker) for _ in range(8)]
    for th in threads:
        th.start()
    for th in threads:
        th.join(10)
    assert len(seen) == 8 and all(s is seen[0] for s in seen)


# ----------------------------------------------------------------------------------
# logging (jsonreshape.log)
# ----------------------------------------------------------------------------------
@pytest.fixture
def clean_logging():
    root = logging.getLogger()
    level = root.level
    handlers = list(root.handlers)
    excepthook, thread_hook = sys.excepthook, threading.excepthook
    yield
    jlog.shutdown_logging()
    root.setLevel(level)
    for h in list(root.handlers):
        if h not in handlers:
            root.removeHandler(h)
    sys.excepthook, threading.excepthook = excepthook, thread_hook


def _ours(root: logging.Logger) -> list[logging.Handler]:
    return [h for h in root.handlers if getattr(h, "_jsonreshape_handler", False)]


def _flush() -> None:
    for h in logging.getLogger().handlers:
        h.flush()


def test_setup_logging_writes_rotating_utf8_file(clean_logging, _isolated_app_home: Path) -> None:
    path = jlog.setup_logging("DEBUG")
    assert path == jlog.log_path() == appdirs.logs_dir() / "jsonreshape.log"
    assert path.parent == _isolated_app_home / "logs"
    log = logging.getLogger("jsonreshape.test")
    log.debug("debug message Тошкент 😀")
    log.warning("lone surrogate \udc80 survives")

    def in_thread() -> None:
        logging.getLogger("jsonreshape.worker").info("from worker")

    th = threading.Thread(target=in_thread, name="worker-7")
    th.start()
    th.join()
    _flush()
    text = path.read_text(encoding="utf-8")
    assert "logging started" in text
    assert "DEBUG" in text and "[MainThread] jsonreshape.test: debug message Тошкент 😀" in text
    assert "\\udc80" in text  # backslashreplace instead of a logging error
    assert "[worker-7] jsonreshape.worker: from worker" in text
    first = text.splitlines()[0]
    assert first[:4].isdigit() and first[4] == "-" and str(os.getpid()) in first  # timestamp, pid
    handler = _ours(logging.getLogger())[0]
    assert handler.maxBytes == 5 * 1024 * 1024 and handler.backupCount == 3
    assert handler.encoding == "utf-8"


def test_setup_logging_is_idempotent(clean_logging, capsys: pytest.CaptureFixture[str]) -> None:
    root = logging.getLogger()
    jlog.setup_logging("INFO")
    jlog.setup_logging("INFO")
    assert len(_ours(root)) == 1
    jlog.setup_logging("WARNING", console=True)
    assert len(_ours(root)) == 2 and root.level == logging.WARNING
    jlog.setup_logging(logging.DEBUG, console=True)
    assert len(_ours(root)) == 2 and root.level == logging.DEBUG
    logging.getLogger("jsonreshape.console").error("to stderr")
    assert "ERROR jsonreshape.console: to stderr" in capsys.readouterr().err
    jlog.setup_logging("INFO", console=False)
    assert len(_ours(root)) == 1
    with pytest.raises(ValueError):
        jlog.setup_logging("LOUD")
    jlog.shutdown_logging()
    jlog.shutdown_logging()
    assert _ours(root) == []


def test_setup_logging_follows_a_new_home(clean_logging, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    first = jlog.setup_logging()
    monkeypatch.setenv("JSONRESHAPE_HOME", str(tmp_path / "other_home"))
    second = jlog.setup_logging()
    assert second != first and second.parent == tmp_path / "other_home" / "logs"
    assert len(_ours(logging.getLogger())) == 1
    logging.getLogger("jsonreshape.x").info("goes to the new file")
    _flush()
    assert "goes to the new file" in second.read_text(encoding="utf-8")


def test_rotation_keeps_three_backups(clean_logging, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(jlog, "MAX_BYTES", 2000)
    path = jlog.setup_logging("INFO")
    log = logging.getLogger("jsonreshape.rot")
    for i in range(400):
        log.info("record %04d %s", i, "x" * 40)
    _flush()
    names = sorted(os.listdir(path.parent))
    assert names == ["jsonreshape.log", "jsonreshape.log.1", "jsonreshape.log.2", "jsonreshape.log.3"]
    for name in names:
        assert os.path.getsize(path.parent / name) <= 2200
    assert "record 0399" in path.read_text(encoding="utf-8")


def test_failed_rollover_does_not_break_logging(clean_logging, monkeypatch: pytest.MonkeyPatch,
                                                capsys: pytest.CaptureFixture[str]) -> None:
    monkeypatch.setattr(jlog, "MAX_BYTES", 500)
    path = jlog.setup_logging("INFO")

    def locked(source: str, dest: str) -> None:
        raise PermissionError(13, "file is used by another process")

    handler = _ours(logging.getLogger())[0]
    monkeypatch.setattr(handler, "rotate", locked)
    log = logging.getLogger("jsonreshape.locked")
    for i in range(50):
        log.info("still logging %d %s", i, "y" * 30)
    _flush()
    assert "still logging 49" in path.read_text(encoding="utf-8")
    assert "Logging error" not in capsys.readouterr().err
    assert not (path.parent / "jsonreshape.log.1").exists()


@pytest.mark.skipif(sys.platform != "win32", reason="Windows sharing semantics")
def test_open_log_file_can_be_renamed_by_another_process(clean_logging) -> None:
    path = jlog.setup_logging("INFO")
    logging.getLogger("jsonreshape.x").info("first")
    _flush()
    moved = path.with_name("renamed.log")
    os.rename(path, moved)  # e.g. a second instance rotating the shared log
    logging.getLogger("jsonreshape.x").info("second")
    _flush()
    assert "second" in moved.read_text(encoding="utf-8")


def test_excepthooks_log_and_chain(clean_logging, monkeypatch: pytest.MonkeyPatch) -> None:
    path = jlog.setup_logging("INFO")
    calls: list[str] = []
    monkeypatch.setattr(sys, "excepthook", lambda t, e, tb: calls.append(f"main:{t.__name__}"))
    monkeypatch.setattr(threading, "excepthook", lambda args: calls.append(f"thread:{args.exc_type.__name__}"))
    jlog.install_excepthooks()
    hook = sys.excepthook
    jlog.install_excepthooks()  # idempotent
    assert sys.excepthook is hook

    try:
        raise ValueError("boom in main")
    except ValueError as exc:
        sys.excepthook(type(exc), exc, exc.__traceback__)
    sys.excepthook(KeyboardInterrupt, KeyboardInterrupt(), None)

    def worker() -> None:
        raise RuntimeError("boom in worker")

    th = threading.Thread(target=worker, name="bg-task")
    th.start()
    th.join()
    th2 = threading.Thread(target=lambda: sys.exit(0), name="exiting")
    th2.start()
    th2.join()
    _flush()
    text = path.read_text(encoding="utf-8")
    assert "CRITICAL" in text and "uncaught exception" in text and "ValueError: boom in main" in text
    assert "uncaught exception in thread bg-task" in text and "RuntimeError: boom in worker" in text
    assert "KeyboardInterrupt" not in text and "SystemExit" not in text
    assert calls == ["main:ValueError", "main:KeyboardInterrupt", "thread:RuntimeError", "thread:SystemExit"]
