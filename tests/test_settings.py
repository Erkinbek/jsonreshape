"""jsonreshape.settings (AGENT_PROMPT §11 "Sozlamalar") and jsonreshape.log (§11 "Loglar").

Settings: defaults exactly as docs/ARCHITECTURE.md §4, validation with localized errors,
to_dict/from_dict (unknown keys ignored), load() never failing (missing/corrupt file ->
defaults + logged warning), atomic save(), process-wide get_settings(); the stage-3 editing
settings (``edits_location``, ``backup_on_save``, ``undo_limit``, §12.9) including settings
files written before they existed.

The edit-layer folders live here as well (``appdirs.edits_dir()`` / ``edits_locks_dir()``,
§12.2): under ``JSONRESHAPE_HOME``, created on demand, the fallback with its notice, no
silent fallback for the lock folder, and -- the user's unsaved changes -- never touched by
cache eviction (``index.evict`` / ``fileio.evict_entries``, ``prune_prepared``) or the
stale-file purges (``recordset.purge_stale_files``, the XLSX staging purge), whatever the
cache folder is.

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
import tempfile
import threading
import time
from pathlib import Path

import pytest

from jsonreshape import appdirs, fileio, recordset, writer
from jsonreshape import index as index_mod
from jsonreshape import log as jlog
from jsonreshape import settings as settings_mod
from jsonreshape.errors import JsonReshapeError, UserInputError
from jsonreshape.i18n import MESSAGES, has, set_language, t
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
    "edits_location": "appdata",
    "backup_on_save": True,
    "undo_limit": 1000,
    "diff_align_max_bytes": 200 * MiB,      # stage 5 (docs/ARCHITECTURE.md §14.0)
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
        ("edits_location", "beside", "beside"),
        ("edits_location", " AppData ", "appdata"),
        ("edits_location", "\tBESIDE\r\n", "beside"),
        ("backup_on_save", False, False),
        ("backup_on_save", True, True),
        ("undo_limit", 200, 200),                      # AGENT_PROMPT §1.5: at least 200 steps
        ("undo_limit", 1000.0, 1000),
        ("undo_limit", " 5000 ", 5000),
        ("undo_limit", 12345678901234567890, 12345678901234567890),   # no upper bound in the core
        ("diff_align_max_bytes", 1, 1),
        ("diff_align_max_bytes", "500MB", 500 * MiB),               # binary units like the other sizes
        ("diff_align_max_bytes", 2.0 * GiB, 2 * GiB),
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
        ("edits_location", ""),
        ("edits_location", "next to the file"),
        ("edits_location", "app data"),
        ("edits_location", "аppdata"),          # Cyrillic "а": looks the same, is not accepted
        ("edits_location", "beside.jredit"),
        ("edits_location", None),
        ("edits_location", 1),
        ("edits_location", ["beside"]),
        ("backup_on_save", "yes"),
        ("backup_on_save", "false"),                   # a string that looks false must not switch it
        ("backup_on_save", 1),
        ("backup_on_save", 0),
        ("backup_on_save", None),
        ("undo_limit", 199),
        ("undo_limit", 0),
        ("undo_limit", -1000),
        ("undo_limit", True),
        ("undo_limit", 200.5),
        ("undo_limit", float("inf")),
        ("undo_limit", float("nan")),
        ("undo_limit", "1e3"),
        ("undo_limit", "many"),
        ("undo_limit", None),
        ("diff_align_max_bytes", 0),
        ("diff_align_max_bytes", -1),
        ("diff_align_max_bytes", True),
        ("diff_align_max_bytes", "lots"),
        ("diff_align_max_bytes", None),
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


def test_editing_settings_error_wording(lang_uz) -> None:
    with pytest.raises(UserInputError) as info:
        Settings(edits_location="yonida")
    assert str(info.value) == ("Sozlama noto'g'ri: «Saqlanmagan o'zgarishlar joyi» = 'yonida'. Kutilgan qiymat: "
                               "appdata (dastur papkasida) yoki beside (fayl yonida).")
    assert info.value.hint == "Sozlamalar oynasida qiymatni tuzating."
    with pytest.raises(UserInputError) as info:
        Settings(undo_limit=150)
    assert str(info.value) == ("Sozlama noto'g'ri: «Ortga qaytarish (undo) qadamlari» = 150. Kutilgan qiymat: "
                               "200 yoki undan katta butun son.")
    with pytest.raises(UserInputError) as info:
        Settings(backup_on_save="ha")
    assert str(info.value) == ("Sozlama noto'g'ri: «Saqlashda .bak zaxira nusxasini qoldirish» = 'ha'. "
                               "Kutilgan qiymat: ha yoki yo'q (true/false).")
    set_language("en")
    with pytest.raises(UserInputError) as info:
        Settings(edits_location="next")
    assert str(info.value) == ('Invalid setting: "Where unsaved changes are kept" = \'next\'. Expected: appdata '
                               '(in the app folder) or beside (next to the file).')
    with pytest.raises(UserInputError) as info:
        Settings(undo_limit=-1)
    assert str(info.value) == 'Invalid setting: "Undo steps" = -1. Expected: a whole number of at least 200.'
    with pytest.raises(UserInputError) as info:
        Settings(backup_on_save=None)
    assert str(info.value) == ('Invalid setting: "Keep a .bak copy when saving" = None. '
                               'Expected: yes or no (true/false).')


def test_editing_settings_constants() -> None:
    assert settings_mod.EDITS_LOCATIONS == ("appdata", "beside")
    assert settings_mod.UNDO_LIMIT_MIN == 200
    assert Settings(undo_limit=settings_mod.UNDO_LIMIT_MIN).undo_limit == 200


def test_diff_align_setting(tmp_path: Path, caplog: pytest.LogCaptureFixture, lang_uz) -> None:
    """Stage 5 (§14.0): the full-diff size limit; a settings file of stage 3 gets the default
    quietly, a bad value in the file falls back alone, the error is localized."""
    p = tmp_path / "settings.json"
    old = {"version": 1, **{k: v for k, v in SPEC_DEFAULTS.items() if k != "diff_align_max_bytes"}}
    p.write_text(json.dumps(old), encoding="utf-8")
    with caplog.at_level(logging.WARNING, logger="jsonreshape.settings"):
        assert Settings.load(p).diff_align_max_bytes == 200 * MiB
    assert not caplog.records
    p.write_text(json.dumps({**old, "diff_align_max_bytes": "1GB", "sample_size": 7}), encoding="utf-8")
    s = Settings.load(p)
    assert (s.diff_align_max_bytes, s.sample_size) == (GiB, 7)
    p.write_text(json.dumps({**old, "diff_align_max_bytes": -5, "sample_size": 7}), encoding="utf-8")
    with caplog.at_level(logging.WARNING, logger="jsonreshape.settings"):
        s = Settings.load(p)
    assert (s.diff_align_max_bytes, s.sample_size) == (200 * MiB, 7)
    assert "diff_align_max_bytes" in caplog.text
    with pytest.raises(UserInputError) as info:
        Settings(diff_align_max_bytes=0)
    assert str(info.value) == ("Sozlama noto'g'ri: «Kalitsiz to'liq taqqoslanadigan fayl hajmi chegarasi» = 0. "
                               "Kutilgan qiymat: 1 yoki undan katta butun son.")
    set_language("en")
    with pytest.raises(UserInputError) as info:
        Settings(diff_align_max_bytes="huge")
    assert str(info.value) == ("Invalid setting: \"Largest file compared in full without a key\" = 'huge'. "
                               "Expected: a whole number of at least 1.")


def test_settings_file_from_before_the_editing_fields(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    """A stage-2 settings.json (no editing keys) keeps its values and gets the editing
    defaults, quietly; saving adds the new keys; invalid editing values fall back alone."""
    p = tmp_path / "settings.json"
    old = {"version": 1, **{k: v for k, v in SPEC_DEFAULTS.items()
                            if k not in ("edits_location", "backup_on_save", "undo_limit", "diff_align_max_bytes")}}
    old.update(sample_size=4321, language="en", csv_delimiter="|")
    p.write_text(json.dumps(old, ensure_ascii=False, indent=2), encoding="utf-8")
    with caplog.at_level(logging.WARNING, logger="jsonreshape.settings"):
        s = Settings.load(p)
    assert not caplog.records
    assert (s.sample_size, s.language, s.csv_delimiter) == (4321, "en", "|")
    assert (s.edits_location, s.backup_on_save, s.undo_limit) == ("appdata", True, 1000)
    assert s.diff_align_max_bytes == 200 * MiB
    s.save(p)
    raw = json.loads(p.read_text(encoding="utf-8"))
    assert list(raw)[-4:] == ["edits_location", "backup_on_save", "undo_limit", "diff_align_max_bytes"]
    assert (raw["edits_location"], raw["backup_on_save"], raw["undo_limit"]) == ("appdata", True, 1000)
    # a hand-edited file: each bad editing value falls back to its own default, the rest is kept
    raw.update(edits_location="Beside", backup_on_save="no", undo_limit=50)
    p.write_text(json.dumps(raw), encoding="utf-8")
    with caplog.at_level(logging.WARNING, logger="jsonreshape.settings"):
        s2 = Settings.load(p)
    assert (s2.edits_location, s2.backup_on_save, s2.undo_limit) == ("beside", True, 1000)
    assert s2.sample_size == 4321
    warned = " ".join(r.getMessage() for r in caplog.records)
    assert "backup_on_save" in warned and "undo_limit" in warned and "edits_location" not in warned
    s3 = dataclasses.replace(s2, backup_on_save=False, undo_limit=100000)
    s3.save(p)
    assert Settings.load(p) == s3


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
# edit-layer folders (appdirs.edits_dir / edits_locks_dir, ARCHITECTURE §12.2)
# ----------------------------------------------------------------------------------
@pytest.fixture
def private_temp(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """A private system temp folder (the fallback home goes there) and fresh appdirs state."""
    temp = tmp_path / "system_temp"
    temp.mkdir()
    monkeypatch.setattr(tempfile, "tempdir", str(temp))
    appdirs._reset_for_tests()
    yield temp
    appdirs._reset_for_tests()


def test_edits_dir_lives_under_the_app_home(_isolated_app_home: Path) -> None:
    edits = appdirs.edits_dir()
    assert edits == _isolated_app_home / "edits" and edits.is_dir()
    assert os.listdir(edits) == []                    # created on demand, no probe file left behind
    locks = appdirs.edits_locks_dir()
    assert locks == edits / "locks" and locks.is_dir()
    assert os.listdir(edits) == ["locks"] and os.listdir(locks) == []
    assert appdirs.edits_dir() == edits and appdirs.edits_locks_dir() == locks   # stable
    # removed while the program runs (e.g. by the user): created again on the next use
    os.rmdir(locks)
    os.rmdir(edits)
    assert appdirs.edits_locks_dir() == locks and locks.is_dir() and edits.is_dir()
    assert appdirs.take_notices() == []               # no fallback happened


def test_edits_dir_follows_jsonreshape_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    home = tmp_path / "Ma'lumotlar Тошкент" / "jr home"
    monkeypatch.setenv("JSONRESHAPE_HOME", str(home))
    assert appdirs.edits_dir() == home / "edits" and appdirs.edits_locks_dir() == home / "edits" / "locks"
    assert (home / "edits" / "locks").is_dir()


def test_unusable_edits_dir_falls_back_with_one_notice(_isolated_app_home: Path, private_temp: Path,
                                                         lang_en) -> None:
    blocker = _isolated_app_home / "edits"
    blocker.write_bytes(b"not a folder")              # a file in the way
    fallback = private_temp / "jsonreshape" / "edits"
    assert appdirs.edits_dir() == fallback and fallback.is_dir()
    assert appdirs.edits_locks_dir() == fallback / "locks" and (fallback / "locks").is_dir()
    notices = appdirs.take_notices()
    assert notices == [("fileio.warning.app_dir_fallback",
                        {"path": str(blocker), "fallback": str(fallback), "detail": t("fileio.reason.not_a_folder")})]
    assert has("fileio.warning.app_dir_fallback")     # notice keys are data: not seen by the i18n scan
    text = t(notices[0][0], **notices[0][1])
    assert str(fallback) in text and "a file of that name is in the way" in text
    assert appdirs.edits_dir() == fallback and appdirs.take_notices() == []   # reported once per process
    assert blocker.read_bytes() == b"not a folder"    # the file in the way is left alone


def test_edits_locks_dir_never_falls_back_silently(_isolated_app_home: Path, private_temp: Path, lang_en) -> None:
    """Two instances must agree on where a layer's lock is: no fallback for the lock folder."""
    edits = appdirs.edits_dir()
    blocker = edits / "locks"
    blocker.write_bytes(b"x")
    with pytest.raises(JsonReshapeError) as info:
        appdirs.edits_locks_dir()
    assert info.value.key == "fileio.error.no_work_dir"
    assert info.value.params == {"path": str(blocker), "detail": "a file of that name is in the way"}
    assert str(blocker) in str(info.value) and info.value.hint
    assert appdirs.take_notices() == []               # nothing was used instead
    assert not (private_temp / "jsonreshape").exists()
    assert blocker.read_bytes() == b"x"
    blocker.unlink()                                  # fixed: works now (a failure is not remembered)
    assert appdirs.edits_locks_dir() == edits / "locks" and (edits / "locks").is_dir()


_DEAD = 999999        # pid of a process that no longer runs (_pid_alive is patched to say so)
_OLD = 3 * 86400.0    # older than every age limit of the cleaners (one day, one hour)


def _make_files(root: Path, files: dict[str, bytes]) -> None:
    for rel, data in files.items():
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(data)


def _age_tree(root: Path) -> None:
    then = time.time() - _OLD
    for dirpath, dirnames, filenames in os.walk(root):
        for name in filenames + dirnames:
            os.utime(os.path.join(dirpath, name), (then, then))
    os.utime(root, (then, then))


def _snapshot(*roots: Path) -> dict[str, bytes | None]:
    """Every file (-> bytes) and folder (-> None) under ``roots``."""
    out: dict[str, bytes | None] = {}
    for root in roots:
        for dirpath, dirnames, filenames in os.walk(root):
            for name in dirnames:
                out[os.path.join(dirpath, name)] = None
            for name in filenames:
                p = os.path.join(dirpath, name)
                with open(p, "rb") as f:
                    out[p] = f.read()
    return out


#: what an edit layer folder holds (§12.2)
_LAYER = {
    "layer.sqlite": b"SQLite format 3\x00" + bytes(84),
    "layer.sqlite-wal": b"\x37\x7f\x06\x82" + bytes(28),
    "seg-00000.dat": '{"shahar": "Toshkent", "nom": "O\u2018zbekiston", "v": 1e400}'.encode(),
    "seg-00001.dat": b'{"n": 12345678901234567890, "x": -0.0, "y": 1.10}\r\n',
    "layer-00001.bin": bytes(range(16)),
}
#: files named like the ones each cleaner removes from its own folders (an index, temp
#: files, prepared copies, record sets, sort runs, XLSX staging): a cleaner that ever looked
#: into an edits folder would take them
_DECOYS = {
    "0123abcd.jri": b"index",
    ".seg.0badf00d.tmp": b"temp",
    "c0ffee.json": b"[]",
    "c0ffee.part": b"part",
    "d00d.json": b"[1]",
    "d00d.meta": b"{}",
    f"rs-{_DEAD}-0a1b.u64": bytes(8),
    f"sort-{_DEAD}-0a1b/run-0.bin": b"run",
    f"xlsx-{_DEAD}-0a1b/sheet1.xml": b"<x/>",
    "xlsx-abcd1234/sheet1.xml": b"<x/>",
}


@pytest.mark.parametrize("cache", ["default", "app_home", "edits", "data_folder"])
def test_cache_eviction_and_purges_never_touch_edit_layers(cache: str, tmp_path: Path, _isolated_app_home: Path,
                                                             monkeypatch: pytest.MonkeyPatch) -> None:
    """Cache eviction (``index.evict`` -> ``fileio.evict_entries``, ``prune_prepared``) and the
    stale-file purges (``recordset.purge_stale_files``, the XLSX staging purge) run with
    everything expired (limit 0, files three days old, every pid dead) and leave every edit
    layer intact -- in the app folder and next to a data file -- whatever the cache folder
    is: none, the app home itself, the edits folder itself, the data folder."""
    key = "5f2b" * 8                                  # a layer key: 32 hex digits
    edits = appdirs.edits_dir()
    locks = appdirs.edits_locks_dir()
    data = tmp_path / "ma'lumot"
    beside = data / "Тошкент.json.jredit"
    settings = {
        "default": Settings(),
        "app_home": Settings(cache_dir=str(_isolated_app_home)),
        "edits": Settings(cache_dir=str(edits)),
        "data_folder": Settings(cache_dir=str(data)),
    }[cache]
    # the user's things: edit layers in both places, their locks, the source and its backup
    _make_files(edits / key, {**_LAYER, **_DECOYS})
    _make_files(beside, {**_LAYER, **_DECOYS})
    _make_files(locks, {f"{key}.lock": b"", f"{'9' * 32}.lock": b""})
    _make_files(edits, {"index.json": b'{"c:\\\\data\\\\a.json": ["%s"]}' % key.encode(), "index.json.lock": b""})
    # decoys at the top of the edits folder too -- except index files and temp files when the
    # user made the edits folder itself the cache folder: there they ARE cache files
    _make_files(edits, {k: v for k, v in _DECOYS.items()
                        if cache != "edits" or not k.endswith((".jri", ".tmp"))})
    _make_files(data, {"Тошкент.json": b'[{"a": 1}]', "Тошкент.json.bak": b'[{"a": 0}]'})
    _age_tree(edits)
    _age_tree(data)
    before = _snapshot(edits, data)
    assert len(before) > 40
    # expired cache files where the cleaners look: they must go (so the cleaners did run)
    index_dir = appdirs.index_dir(settings.cache_dir)
    prepared = fileio.prepared_dir(settings.cache_dir)
    work = appdirs.work_dir(settings.cache_dir)
    sentinels = [index_dir / "feedface.jri", index_dir / ".feedface.0badf00d.tmp", prepared / "beef.json",
                 prepared / "beef.meta", prepared / "cafe.json", appdirs.recordsets_dir() / f"rs-{_DEAD}-0f.u64",
                 appdirs.tmp_dir() / f"sort-{_DEAD}-0f" / "run.bin", work / f"sort-{_DEAD}-1f" / "run.bin",
                 appdirs.tmp_dir() / f"xlsx-{_DEAD}-0f" / "s.xml", work / f"xlsx-{_DEAD}-1f" / "s.xml"]
    then = time.time() - _OLD
    for p in sentinels:
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(b"cache")
        os.utime(p, (then, then))
        os.utime(p.parent, (then, then))
    # every entry handed to the eviction (index files, prepared copies) is recorded
    entries: list[Path] = []
    real_evict = fileio.evict_entries

    def recording(items, limit_bytes):
        items = list(items)
        entries.extend(p for item in items for p in item.paths)
        return real_evict(items, limit_bytes)

    monkeypatch.setattr(fileio, "evict_entries", recording)
    monkeypatch.setattr(index_mod, "evict_entries", recording)
    monkeypatch.setattr(recordset, "_pid_alive", lambda pid: False)
    monkeypatch.setattr(writer, "_pid_alive", lambda pid: False)
    set_settings(settings)

    index_mod.evict(0, directory=index_dir, prepared_dir=prepared)
    fileio.prune_prepared(0, min_age=0, directory=prepared)
    assert recordset.purge_stale_files() >= 3
    assert writer._purge_staging(settings) >= 2

    assert not [p for p in sentinels if p.exists()]
    assert {p for p in entries} >= {index_dir / "feedface.jri", prepared / "beef.json"}
    protected = [os.path.normcase(str(p)) for p in (edits / key, locks, beside, edits / "index.json")]
    assert not [p for p in entries if any(os.path.normcase(str(p)).startswith(q) for q in protected)]
    after = _snapshot(edits, data)
    assert {p: v for p, v in before.items() if after.get(p, b"<gone>") != v} == {}


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
