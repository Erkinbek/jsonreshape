"""jsonreshape.gui.osintegration: Windows "Open with" (docs/ARCHITECTURE.md §15.0; AGENT_PROMPT §11).

Only per-user keys under HKCU\\Software\\Classes, exactly the planned values, never the default program; "remove"
deletes exactly what registration wrote (foreign values / keys stay, also inside keys registration created);
failures at any write leave a state "remove" cleans up completely; the launch command (packaged exe, development
pythonw.exe); the state; SHChangeNotify after every change; the settings rows (question first, then the action,
Windows only). Every test uses the dict-based registry: nothing here writes the real registry (the real winreg
backend is only read).
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

pytest.importorskip("PySide6")

from jsonreshape import i18n  # noqa: E402
from jsonreshape.gui import osintegration as osi  # noqa: E402
from jsonreshape.gui.osintegration import (  # noqa: E402
    APP_KEY,
    CREATED_VALUE,
    EXTENSIONS,
    PROG_ID,
    REG_MULTI_SZ,
    REG_NONE,
    REG_SZ,
    MemoryRegistry,
    OpenWithError,
    RegValue,
    launch_command,
    plan,
    register,
    status,
    unavailable_reason,
    unregister,
)
from jsonreshape.i18n import t  # noqa: E402

CMD = '"C:\\Program Files\\JSON Reshape\\jsonreshape-gui.exe" "%1"'
CMD2 = '"D:\\Tools\\jsonreshape\\jsonreshape-gui.exe" "%1"'


class Notifier:
    def __init__(self) -> None:
        self.calls = 0

    def __call__(self) -> bool:
        self.calls += 1
        return True


def foreign_registry() -> MemoryRegistry:
    """A registry with other programs' associations (that must survive register + unregister)."""
    reg = MemoryRegistry()
    reg.ensure(".json\\OpenWithProgids")
    reg.set_value(".json", "", "VSCode.json", REG_SZ)
    reg.set_value(".json\\OpenWithProgids", "VSCode.json", None, REG_NONE)
    reg.ensure(".jsonl\\OpenWithProgids")                         # an empty key somebody else created
    reg.ensure(".gz")
    reg.set_value(".gz", "", "7-Zip.gz", REG_SZ)
    reg.set_value(".gz", "Content Type", "application/gzip", REG_SZ)
    reg.ensure("Applications\\notepad++.exe\\shell\\open\\command")
    reg.set_value("Applications\\notepad++.exe\\shell\\open\\command", "", '"C:\\npp\\notepad++.exe" "%1"', REG_SZ)
    reg.ensure("VSCode.json\\shell\\open\\command")
    reg.set_value("VSCode.json\\shell\\open\\command", "", '"C:\\Code\\Code.exe" "%1"', REG_SZ)
    reg.ensure(".txt")
    reg.set_value(".txt", "", "txtfile", REG_SZ)
    return reg


def expected_values(command: str) -> dict[tuple[str, str], tuple[object, int]]:
    out: dict[tuple[str, str], tuple[object, int]] = {
        (PROG_ID, ""): (t("gui.os.type_name"), REG_SZ),
        (PROG_ID + "\\shell\\open\\command", ""): (command, REG_SZ),
        (APP_KEY, "FriendlyAppName"): ("JSON Reshape", REG_SZ),
        (APP_KEY + "\\shell\\open\\command", ""): (command, REG_SZ),
    }
    for ext in EXTENSIONS:
        out[(APP_KEY + "\\SupportedTypes", ext)] = ("", REG_SZ)
        out[(ext + "\\OpenWithProgids", PROG_ID)] = (None, REG_NONE)
    return out


# ----------------------------------------------------------------------------- the plan
def test_plan_is_exactly_the_spec(lang_uz) -> None:
    entries = plan(CMD)
    got = {(e.key, e.name): (e.data, e.kind) for e in entries}
    assert got == expected_values(CMD)
    assert len(entries) == len(got) == 4 + 2 * len(EXTENSIONS)
    assert set(EXTENSIONS) == {".json", ".jsonl", ".ndjson", ".geojson", ".gz"}
    assert APP_KEY == "Applications\\jsonreshape-gui.exe" and PROG_ID == "jsonreshape.document"


def test_plan_never_touches_the_default_program() -> None:
    for e in plan(CMD):
        assert not e.key.upper().startswith(("HKEY", "SOFTWARE\\")), e    # relative to HKCU\Software\Classes
        parts = e.key.casefold().split("\\")
        assert "userchoice" not in parts and "openwithlist" not in parts and "fileexts" not in parts
        if e.key.startswith("."):
            # an extension key gets only an OpenWithProgids entry, never its default value (the default program)
            assert e.key.casefold().endswith("\\openwithprogids") and e.name == PROG_ID


def test_plan_type_name_follows_the_language(lang_en) -> None:
    assert plan(CMD)[0] == RegValue(PROG_ID, "", "JSON file (JSON Reshape)", REG_SZ)
    i18n.set_language("uz")
    assert plan(CMD)[0].data == "JSON fayl (JSON Reshape)"


# ----------------------------------------------------------------------------- register / unregister
def test_register_on_an_empty_registry_and_remove_everything() -> None:
    reg = MemoryRegistry()
    before = reg.dump()
    notify = Notifier()
    out = register(reg, CMD, notify=notify)
    assert notify.calls == 1 and out.notified
    dump = reg.dump()
    for (key, name), value in expected_values(CMD).items():
        assert dump[key][name] == value, (key, name)
    created = reg.get_value(PROG_ID, CREATED_VALUE)
    assert created is not None and created[1] == REG_MULTI_SZ
    assert created[0] == ["Applications"] + [k for ext in EXTENSIONS for k in (ext, ext + "\\OpenWithProgids")]
    # nothing but the planned values (and the record) was written
    written = {(k, n) for k, values in dump.items() for n in values}
    assert written == set(expected_values(CMD)) | {(PROG_ID, CREATED_VALUE)}
    assert status(reg, CMD).state == "current"

    out = unregister(reg, notify=notify)
    assert notify.calls == 2 and out.notified
    assert reg.dump() == before == {}
    assert status(reg, CMD).state == "none"


def test_foreign_associations_survive(lang_uz) -> None:
    reg = foreign_registry()
    before = reg.dump()
    register(reg, CMD, notify=Notifier())
    dump = reg.dump()
    # the default programs and other programs' entries are untouched
    assert dump[".json"][""] == ("VSCode.json", REG_SZ)
    assert dump[".gz"] == {"": ("7-Zip.gz", REG_SZ), "Content Type": ("application/gzip", REG_SZ)}
    assert dump[".json\\OpenWithProgids"]["VSCode.json"] == (None, REG_NONE)
    assert dump[".json\\OpenWithProgids"][PROG_ID] == (None, REG_NONE)
    for key, values in before.items():
        for name, value in values.items():
            assert dump[key][name] == value, (key, name)
    # only the keys that did not exist are recorded as created
    record = reg.get_value(PROG_ID, CREATED_VALUE)[0]
    assert record == [".ndjson", ".ndjson\\OpenWithProgids", ".geojson", ".geojson\\OpenWithProgids",
                      ".gz\\OpenWithProgids"]
    unregister(reg, notify=Notifier())
    assert reg.dump() == before        # also the empty .jsonl\OpenWithProgids somebody else created


def test_remove_keeps_foreign_values_inside_its_own_and_created_keys() -> None:
    reg = MemoryRegistry()
    before = reg.dump()
    register(reg, CMD)
    # other programs (or Windows) add to keys registration wrote
    reg.set_value(".geojson\\OpenWithProgids", "QGIS.geojson", None, REG_NONE)
    reg.set_value(APP_KEY + "\\shell\\open", "FriendlyAppName", "Mine", REG_SZ)
    reg.ensure(PROG_ID + "\\shell\\edit\\command")
    reg.set_value(PROG_ID + "\\shell\\edit\\command", "", "edit.exe %1", REG_SZ)
    unregister(reg)
    dump = reg.dump()
    assert dump[".geojson\\OpenWithProgids"] == {"QGIS.geojson": (None, REG_NONE)}
    assert ".geojson" in dump                                    # the parent of a kept key stays
    assert dump[APP_KEY + "\\shell\\open"] == {"FriendlyAppName": ("Mine", REG_SZ)}
    assert dump[PROG_ID + "\\shell\\edit\\command"] == {"": ("edit.exe %1", REG_SZ)}
    assert dump[PROG_ID] == {} and PROG_ID + "\\shell\\open\\command" not in dump
    # none of the registration's own values is left
    for (key, name) in expected_values(CMD):
        assert name not in dump.get(key, {}), (key, name)
    assert CREATED_VALUE not in dump[PROG_ID]
    # every other key registration created is gone
    for ext in (".json", ".jsonl", ".ndjson", ".gz"):
        assert ext not in dump and ext + "\\OpenWithProgids" not in dump
    assert set(dump) - set(before) == {
        ".geojson", ".geojson\\OpenWithProgids", "Applications", APP_KEY, APP_KEY + "\\shell",
        APP_KEY + "\\shell\\open", PROG_ID, PROG_ID + "\\shell", PROG_ID + "\\shell\\edit", PROG_ID + "\\shell\\edit\\command"}


def test_register_again_updates_the_command_without_duplicates() -> None:
    reg = foreign_registry()
    register(reg, CMD)
    record = reg.get_value(PROG_ID, CREATED_VALUE)[0]
    register(reg, CMD2)
    assert reg.get_value(PROG_ID, CREATED_VALUE)[0] == record
    assert reg.get_value(PROG_ID + "\\shell\\open\\command", "") == (CMD2, REG_SZ)
    assert reg.get_value(APP_KEY + "\\shell\\open\\command", "") == (CMD2, REG_SZ)
    assert status(reg, CMD2).state == "current"
    st = status(reg, CMD)
    assert st.state == "other" and st.command == CMD2 and st.extensions == EXTENSIONS
    # case-insensitive comparison (Windows paths)
    assert status(reg, CMD2.upper()).state == "current"


def test_status_partial_and_none() -> None:
    reg = MemoryRegistry()
    assert status(reg).state == "none" and status(reg).command is None
    register(reg, CMD)
    assert status(reg).state == "current"          # no command to compare with: complete is "current"
    reg.delete_value(APP_KEY + "\\SupportedTypes", ".gz")
    assert status(reg, CMD).state == "partial"
    register(reg, CMD)
    reg.set_value(APP_KEY + "\\shell\\open\\command", "", CMD2, REG_SZ)    # the two commands disagree
    assert status(reg, CMD).state == "partial"
    unregister(reg)
    reg.ensure(PROG_ID)                          # e.g. a registration that failed right after its record
    assert status(reg, CMD).state == "partial"
    unregister(reg)
    assert reg.dump() == {}


def test_remove_with_nothing_registered_changes_nothing() -> None:
    reg = foreign_registry()
    before = reg.dump()
    writes = reg.writes
    notify = Notifier()
    out = unregister(reg, notify=notify)
    assert out.values == [] and out.keys == [] and notify.calls == 0 and not out.notified
    assert reg.writes == writes and reg.dump() == before


def test_a_tampered_record_never_deletes_other_keys() -> None:
    reg = foreign_registry()
    reg.ensure(".xml")                              # an empty foreign key
    before = reg.dump()
    register(reg, CMD)
    reg.set_value(PROG_ID, CREATED_VALUE, [".xml", ".txt", "Software\\Microsoft", ".ndjson\\OpenWithProgids",
                                           ".ndjson"], REG_MULTI_SZ)
    unregister(reg)
    dump = reg.dump()
    assert ".xml" in dump and ".txt" in dump
    assert ".ndjson" not in dump and ".ndjson\\OpenWithProgids" not in dump     # listed and created: deleted
    # keys the record no longer lists stay (empty): they are never guessed
    assert ".geojson\\OpenWithProgids" in dump and dump[".geojson\\OpenWithProgids"] == {}
    for key in before:
        assert key in dump


class FailingRegistry(MemoryRegistry):
    """Refuses the ``fail_at``-th set_value (or every delete when ``fail_deletes``)."""

    def __init__(self, fail_at: int | None = None, *, fail_deletes: bool = False, keys=()) -> None:
        super().__init__(keys)
        self.fail_at = fail_at
        self.fail_deletes = fail_deletes
        self.sets = 0

    def set_value(self, path, name, data, kind):
        self.sets += 1
        if self.fail_at is not None and self.sets == self.fail_at:
            raise PermissionError(13, "Access is denied")
        super().set_value(path, name, data, kind)

    def delete_value(self, path, name):
        if self.fail_deletes:
            raise PermissionError(13, "Access is denied")
        return super().delete_value(path, name)


@pytest.mark.parametrize("fail_at", range(1, 2 + 4 + 2 * len(EXTENSIONS)))
def test_a_failure_at_any_write_is_reported_and_removable(fail_at: int, lang_uz) -> None:
    reg = FailingRegistry(fail_at)
    for key, values in foreign_registry().dump().items():
        reg.ensure(key)
        for name, (data, kind) in values.items():
            MemoryRegistry.set_value(reg, key, name, data, kind)
    before = reg.dump()
    notify = Notifier()
    with pytest.raises(OpenWithError) as info:
        register(reg, CMD, notify=notify)
    assert info.value.key == "gui.os.error.add" and "Access is denied" in info.value.message
    assert info.value.hint
    reg.fail_at = None
    if reg.dump() != before:
        assert status(reg, CMD).state in ("partial", "current")
    unregister(reg, notify=notify)
    assert reg.dump() == before


def test_remove_failure_is_reported() -> None:
    reg = FailingRegistry()
    register(reg, CMD)
    reg.fail_deletes = True
    with pytest.raises(OpenWithError) as info:
        unregister(reg)
    assert info.value.key == "gui.os.error.remove"
    reg.fail_deletes = False
    unregister(reg)
    assert reg.dump() == {}


def test_register_refuses_a_command_without_the_file_argument() -> None:
    with pytest.raises(ValueError):
        register(MemoryRegistry(), '"C:\\x.exe"')
    with pytest.raises(ValueError):
        register(MemoryRegistry(), "")


# ----------------------------------------------------------------------------- the memory registry
def test_memory_registry_semantics() -> None:
    reg = MemoryRegistry()
    with pytest.raises(FileNotFoundError):
        reg.create_key("A\\B")                       # the parent must exist (like the real one)
    reg.create_key("A")
    reg.create_key("a\\B")
    assert reg.key_exists("A\\b") and reg.subkeys("a") == ["B"] and reg.subkeys("") == ["A"]
    reg.set_value("A", "Name", 1, REG_SZ)
    reg.set_value("a", "NAME", 2, REG_SZ)            # case-insensitive: the same value, first spelling kept
    assert reg.value_names("A") == ["Name"] and reg.get_value("A", "name") == (2, REG_SZ)
    with pytest.raises(PermissionError):
        reg.delete_key("A")                          # has a subkey
    with pytest.raises(FileNotFoundError):
        reg.set_value("missing", "x", 1, REG_SZ)
    reg.delete_key("A\\B")
    reg.delete_key("A")                              # drops its values
    assert reg.dump() == {} and reg.get_value("A", "Name") is None
    for bad in ("..\\x", "a\\\\b", "\\a", "HKEY_LOCAL_MACHINE\\Software"):
        with pytest.raises(ValueError):
            reg.key_exists(bad)


@pytest.mark.skipif(sys.platform != "win32", reason="the real registry exists on Windows only")
def test_winreg_backend_reads_only_below_software_classes() -> None:
    backend = osi.WinregBackend()
    assert backend.root == "Software\\Classes"
    assert backend.key_exists("")
    assert not backend.key_exists("jsonreshape-test-no-such-key-5b8e1f")
    assert backend.get_value("", "jsonreshape-test-no-such-value-5b8e1f") is None
    assert backend.get_value("jsonreshape-test-no-such-key-5b8e1f", "") is None
    assert backend.subkeys("jsonreshape-test-no-such-key-5b8e1f") == []
    assert isinstance(backend.subkeys(""), list)
    with pytest.raises(ValueError):
        backend.key_exists("HKEY_LOCAL_MACHINE\\Software")
    with pytest.raises(ValueError):
        backend.get_value("..\\Microsoft", "")
    with pytest.raises(PermissionError):
        backend.delete_key("")


@pytest.fixture
def app_hive(tmp_path: Path):
    """A private application hive file (``RegLoadAppKeyW``) with a ``Software\\Classes`` key: the real winreg
    code paths run against it, never against the user's registry."""
    if sys.platform != "win32":
        pytest.skip("Windows only")
    import ctypes
    import winreg
    from ctypes import wintypes

    adv = ctypes.WinDLL("advapi32")
    adv.RegLoadAppKeyW.argtypes = [wintypes.LPCWSTR, ctypes.POINTER(wintypes.HKEY), wintypes.DWORD,
                                   wintypes.DWORD, wintypes.DWORD]
    adv.RegLoadAppKeyW.restype = wintypes.LONG
    adv.RegCloseKey.argtypes = [wintypes.HKEY]
    handle = wintypes.HKEY()
    rc = adv.RegLoadAppKeyW(str(tmp_path / "hive.dat"), ctypes.byref(handle), winreg.KEY_ALL_ACCESS, 0, 0)
    if rc != 0:  # pragma: no cover - e.g. a locked-down machine
        pytest.skip(f"RegLoadAppKeyW failed: {rc}")
    winreg.CreateKeyEx(handle.value, "Software\\Classes", 0, winreg.KEY_WRITE).Close()
    try:
        yield handle.value
    finally:
        adv.RegCloseKey(handle)


def _hive_dump(backend: osi.WinregBackend, path: str = "") -> dict[str, dict[str, tuple[object, int]]]:
    out: dict[str, dict[str, tuple[object, int]]] = {}
    for name in backend.subkeys(path):
        sub = f"{path}\\{name}" if path else name
        out[sub] = {v: backend.get_value(sub, v) for v in backend.value_names(sub)}
        out.update(_hive_dump(backend, sub))
    return out


def test_winreg_backend_full_cycle_in_a_private_hive(app_hive, lang_uz) -> None:
    backend = osi.WinregBackend(hive=app_hive)
    # other programs' associations
    for key in (".json", ".json\\OpenWithProgids", ".gz", "Applications", "Applications\\notepad++.exe"):
        backend.create_key(key)
    backend.set_value(".json", "", "VSCode.json", REG_SZ)
    backend.set_value(".json\\OpenWithProgids", "VSCode.json", None, REG_NONE)
    backend.set_value(".gz", "", "7-Zip.gz", REG_SZ)
    before = _hive_dump(backend)
    notify = Notifier()
    register(backend, CMD, notify=notify)
    dump = _hive_dump(backend)
    for (key, name), value in expected_values(CMD).items():
        assert dump[key][name] == value, (key, name)
    assert dump[".json"][""] == ("VSCode.json", REG_SZ)
    assert backend.get_value(PROG_ID, CREATED_VALUE) == (
        [".jsonl", ".jsonl\\OpenWithProgids", ".ndjson", ".ndjson\\OpenWithProgids", ".geojson",
         ".geojson\\OpenWithProgids", ".gz\\OpenWithProgids"], REG_MULTI_SZ)
    assert status(backend, CMD).state == "current" and status(backend, CMD2).state == "other"
    register(backend, CMD2)
    assert status(backend, CMD2).state == "current"
    unregister(backend, notify=notify)
    assert _hive_dump(backend) == before and notify.calls == 2
    assert status(backend).state == "none"


def test_winreg_backend_semantics_in_a_private_hive(app_hive) -> None:
    backend = osi.WinregBackend(hive=app_hive)
    with pytest.raises(FileNotFoundError):
        backend.create_key("A\\B")
    backend.create_key("A")
    backend.create_key("a\\B")
    assert backend.key_exists("A\\b") and backend.subkeys("a") == ["B"]
    backend.set_value("A", "Name", "x", REG_SZ)
    assert backend.get_value("a", "NAME") == ("x", REG_SZ) and backend.value_names("A") == ["Name"]
    assert backend.delete_value("A", "name") and not backend.delete_value("A", "name")
    assert not backend.delete_value("missing", "x")
    with pytest.raises(OSError):
        backend.delete_key("A")                      # has a subkey
    backend.delete_key("A\\B")
    backend.delete_key("A")
    assert not backend.key_exists("A") and backend.subkeys("") == []


def test_the_safety_net_under_pytest() -> None:
    first = osi.default_backend()
    assert isinstance(first, MemoryRegistry) and osi.default_backend() is first
    assert osi.default_notifier()() is False


# ----------------------------------------------------------------------------- launch command
def test_launch_command_of_the_packaged_exe(lang_uz) -> None:
    exe = "C:\\Program Files\\JSON Reshape\\jsonreshape-gui.exe"
    assert launch_command(frozen=True, executable=exe, platform="win32") == f'"{exe}" "%1"'
    assert unavailable_reason(frozen=True, executable=exe, platform="win32") is None
    # the console exe (or any other name) cannot register itself
    other = "C:\\Program Files\\JSON Reshape\\jsonreshape.exe"
    assert launch_command(frozen=True, executable=other, platform="win32") is None
    assert "jsonreshape-gui.exe" in unavailable_reason(frozen=True, executable=other, platform="win32")


def test_launch_command_in_development(tmp_path: Path, lang_en) -> None:
    folder = tmp_path / "venv o'zbek ўзбек" / "Scripts"
    folder.mkdir(parents=True)
    python = folder / "python.exe"
    python.write_bytes(b"")
    assert launch_command(frozen=False, executable=python, platform="win32") is None
    assert "pythonw.exe was not found" in unavailable_reason(frozen=False, executable=python, platform="win32")
    (folder / "pythonw.exe").write_bytes(b"")
    cmd = launch_command(frozen=False, executable=python, platform="win32")
    assert cmd == f'"{folder / "pythonw.exe"}" -m jsonreshape.gui "%1"'
    assert launch_command(frozen=False, executable=folder / "pythonw.exe", platform="win32") == cmd
    # an embedding program (not python.exe / pythonw.exe) cannot register
    assert launch_command(frozen=False, executable=folder / "host.exe", platform="win32") is None
    assert unavailable_reason(platform="linux") == "This works on Windows only."
    assert launch_command(platform="darwin") is None


@pytest.mark.skipif(sys.platform != "win32", reason="Windows only")
def test_launch_command_of_this_process() -> None:
    cmd = launch_command()
    if Path(sys.executable).with_name("pythonw.exe").is_file():
        assert cmd is not None and cmd.endswith(' -m jsonreshape.gui "%1"') and "pythonw.exe" in cmd
        register(MemoryRegistry(), cmd)              # accepted by the registration
    else:  # pragma: no cover - an interpreter without pythonw.exe
        assert cmd is None and unavailable_reason()


# ----------------------------------------------------------------------------- settings rows (Qt)
@pytest.fixture
def rows_env(qtbot, monkeypatch, lang_uz):
    from jsonreshape.gui import widgets

    asked: list[str] = []
    answers: list[bool] = []

    def handler(_parent, text, _title):
        asked.append(text)
        return answers.pop(0) if answers else False

    monkeypatch.setattr(widgets, "confirm_handler", handler)

    class App:
        def __init__(self) -> None:
            self.errors: list[BaseException] = []

        def report_error(self, exc, parent=None):
            self.errors.append(exc)

    return asked, answers, App()


@pytest.mark.gui
def test_rows_ask_first_then_register_and_remove(qtbot, rows_env) -> None:
    asked, answers, app = rows_env
    reg, notify = MemoryRegistry(), Notifier()
    rows = osi.OpenWithRows(app=app, backend=reg, notify=notify, command=CMD)
    qtbot.addWidget(rows)
    assert rows.status_label.text() == t("gui.os.status.none")
    assert rows.add_button.isEnabled() and not rows.remove_button.isEnabled()
    assert CMD in rows.add_button.toolTip()
    # "no": nothing is written
    answers.append(False)
    rows.add_button.click()
    qtbot.waitUntil(lambda: len(asked) == 1)
    qtbot.wait(20)
    assert reg.writes == 0 and notify.calls == 0
    assert CMD in asked[0] and ".geojson" in asked[0] and "HKCU\\Software\\Classes" in asked[0]
    # "yes": registered, Explorer told, state shown
    answers.append(True)
    with qtbot.waitSignal(rows.changed, timeout=5000):
        rows.add_button.click()
    assert status(reg, CMD).state == "current" and notify.calls == 1
    assert rows.status_label.text() == t("gui.os.done.add") and rows.remove_button.isEnabled()
    rows._message = None
    rows.refresh()
    assert rows.status_label.text() == t("gui.os.status.current")
    # remove: asked too
    answers.append(True)
    with qtbot.waitSignal(rows.changed, timeout=5000):
        rows.remove_button.click()
    assert asked[-1] == t("gui.os.confirm.remove")
    assert reg.dump() == {} and notify.calls == 2 and not rows.remove_button.isEnabled()
    assert app.errors == []


@pytest.mark.gui
def test_rows_report_a_failure(qtbot, rows_env) -> None:
    _asked, answers, app = rows_env
    reg = FailingRegistry(3)
    rows = osi.OpenWithRows(app=app, backend=reg, notify=Notifier(), command=CMD)
    qtbot.addWidget(rows)
    answers.append(True)
    with qtbot.waitSignal(rows.changed, timeout=5000):
        rows.add_button.click()
    assert len(app.errors) == 1 and isinstance(app.errors[0], OpenWithError)
    assert "Access is denied" in rows.status_label.text()
    assert rows.status_label.property("role") == "danger"
    assert rows.remove_button.isEnabled()            # what was written can be removed
    assert rows.remove() and reg.dump() == {}


@pytest.mark.gui
def test_rows_when_this_process_cannot_register(qtbot, rows_env) -> None:
    reason = t("gui.os.unavailable.exe", name="jsonreshape-gui.exe")
    reg = MemoryRegistry()
    register(reg, CMD2)
    rows = osi.OpenWithRows(backend=reg, notify=Notifier(), command=None, reason=reason)
    qtbot.addWidget(rows)
    assert not rows.add_button.isEnabled() and rows.add_button.toolTip() == reason
    assert rows.remove_button.isEnabled()            # removing another copy's registration is still possible
    assert rows.status_label.text() == t("gui.os.status.current")
    reg.delete_value(APP_KEY + "\\SupportedTypes", ".gz")
    rows.refresh()
    assert rows.status_label.text() == t("gui.os.status.partial") + "\n" + reason
    rows.ask_add()                                   # does nothing without a command
    assert status(reg).command == CMD2


@pytest.mark.gui
def test_rows_follow_the_theme(qtbot, rows_env, qapp) -> None:
    from PySide6.QtGui import QPalette

    from jsonreshape.gui import theme

    palette = QPalette(qapp.palette())
    style = qapp.style().name()
    rows = osi.OpenWithRows(backend=MemoryRegistry(), notify=Notifier(), command=CMD)
    qtbot.addWidget(rows)
    try:
        theme.apply_theme(qapp, "dark")
        assert theme.colors("dark")["muted"].name() in rows.status_label.styleSheet()
        theme.apply_theme(qapp, "light")
        assert theme.colors("light")["muted"].name() in rows.status_label.styleSheet()
    finally:
        theme._reset_for_tests()
        try:
            qapp.styleHints().unsetColorScheme()
        except AttributeError:  # pragma: no cover
            pass
        qapp.setStyle(style)
        qapp.setPalette(palette)
        theme.invalidate()


@pytest.mark.gui
def test_settings_rows_only_on_windows(qtbot) -> None:
    from PySide6.QtWidgets import QFormLayout, QWidget

    page = QWidget()
    qtbot.addWidget(page)
    form = QFormLayout(page)
    assert osi.add_settings_rows(page, form, platform="linux") is None and form.rowCount() == 0
    rows = osi.add_settings_rows(page, form, platform="win32")
    assert isinstance(rows, osi.OpenWithRows) and form.rowCount() == 1
    assert isinstance(rows.backend, MemoryRegistry)  # the pytest safety net, never the real registry


@pytest.mark.gui
@pytest.mark.skipif(sys.platform != "win32", reason="the rows exist on Windows only")
def test_settings_dialog_has_the_rows_and_cancel_keeps_the_registration(qtbot, rows_env, tmp_path) -> None:
    from jsonreshape.gui.context import AppContext
    from jsonreshape.gui.guistate import GuiState
    from jsonreshape.gui.settingsdialog import SettingsDialog

    _asked, answers, _app = rows_env
    ctx = AppContext(state=GuiState.load(tmp_path / "gui_state.json"), headless=True)
    try:
        dlg = SettingsDialog(ctx)
        qtbot.addWidget(dlg)
        rows = dlg.open_with_rows
        assert isinstance(rows, osi.OpenWithRows) and rows.parent() is not None
        assert isinstance(rows.backend, MemoryRegistry)
        unregister(rows.backend)
        rows.refresh()
        if rows.command is None:                     # pragma: no cover - no pythonw.exe beside the interpreter
            return
        answers.append(True)
        with qtbot.waitSignal(rows.changed, timeout=5000):
            rows.add_button.click()
        assert status(rows.backend, rows.command).state == "current"
        dlg.reject()                                 # "Bekor qilish" does not undo an action
        assert status(rows.backend, rows.command).state == "current"
        unregister(rows.backend)
    finally:
        assert ctx.tasks.shutdown(30)
