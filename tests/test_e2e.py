"""End-to-end runs of the real command line in subprocesses (AGENT_PROMPT §13, §11, §1.4).

``python -m jsonreshape`` and the ``jsonreshape`` console script (when installed): real exit
codes 0/1/2/3, UTF-8 output when stdout/stderr are redirected (the Windows code page cannot
encode Uzbek and Cyrillic text), non-ASCII paths, the log file, ``-v`` console logging,
``--backend`` selecting the core of a fresh process (and ``import jsonreshape.cli`` not
loading the core at all), a closed stdout pipe, and Ctrl+Break (Windows) / SIGINT (POSIX)
cancelling a running command without leaving partial output.
"""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

import pytest

import jsongen
import jsonreshape

ROOT = Path(__file__).resolve().parents[1]
VERSION = jsonreshape.__version__
CONSOLE_SCRIPT = Path(sys.executable).parent / ("jsonreshape.exe" if sys.platform == "win32" else "jsonreshape")
TOSHKENT_CYR = "".join(map(chr, (0x0422, 0x043E, 0x0448, 0x043A, 0x0435, 0x043D, 0x0442)))
OKINA = chr(0x02BB)
UZ_TEXT = "O" + OKINA + "zbekiston"


def child_env(home: Path, **extra: str) -> dict[str, str]:
    """The test environment without anything that would make Python use UTF-8 by itself
    (the CLI must do it); JSONRESHAPE_BACKEND is inherited (both backend test runs)."""
    env = {k: v for k, v in os.environ.items()
           if k not in ("PYTHONIOENCODING", "PYTHONUTF8", "JSONRESHAPE_LANG", "NO_COLOR", "PYTHONLEGACYWINDOWSSTDIO")}
    env["JSONRESHAPE_HOME"] = str(home)
    env.update(extra)
    return env


def jr(home: Path, *args: Any, script: bool = False, timeout: float = 180, **env: str) -> subprocess.CompletedProcess:
    cmd = [str(CONSOLE_SCRIPT)] if script else [sys.executable, "-m", "jsonreshape"]
    return subprocess.run(cmd + [str(a) for a in args], cwd=str(ROOT), env=child_env(home, **env),
                          capture_output=True, timeout=timeout)


def text(data: bytes) -> str:
    return data.decode("utf-8")  # strict: the output must be valid UTF-8


@pytest.fixture
def home(tmp_path: Path) -> Path:
    h = tmp_path / "home"
    h.mkdir()
    return h


@pytest.fixture
def data(tmp_path: Path) -> Path:
    recs = [
        {"id": 1, "name": "Anvar", "city": "Toshkent", "note": UZ_TEXT, "score": codec_loads("1.10")},
        {"id": 2, "name": "Bobur", "city": TOSHKENT_CYR, "note": "Chorsu", "score": codec_loads("1e400")},
        {"id": 3, "name": "Dilnoza", "city": "Samarqand", "note": None, "score": codec_loads("-0.0")},
    ]
    p = tmp_path / "data.json"
    jsongen.write_file(p, recs, "array", style="pretty2")
    return p


def codec_loads(tok: str) -> Any:
    from jsonreshape import codec

    return codec.loads(tok)


# ----------------------------------------------------------------------------------
# entry points and exit codes
# ----------------------------------------------------------------------------------
def test_python_m_jsonreshape_version(home: Path) -> None:
    r = jr(home, "--version")
    assert r.returncode == 0, r.stderr
    lines = text(r.stdout).splitlines()
    assert lines[0] == f"jsonreshape {VERSION}"
    assert lines[1].startswith("Yadro: ") and lines[2].startswith("Python: ")


def test_console_script(home: Path, data: Path) -> None:
    if not CONSOLE_SCRIPT.exists():
        pytest.skip("the jsonreshape console script is not installed in this environment")
    r = jr(home, "--lang", "en", "--version", script=True)
    assert r.returncode == 0, r.stderr
    assert text(r.stdout).splitlines()[0] == f"jsonreshape {VERSION}"
    r = jr(home, "inspect", data, "--json", script=True)
    assert r.returncode == 0 and json.loads(text(r.stdout))["records"] == 3
    r = jr(home, "inspect", data.with_name("missing.json"), script=True)
    assert r.returncode == 1


def test_real_exit_codes(home: Path, data: Path, tmp_path: Path) -> None:
    assert jr(home, "inspect", data).returncode == 0
    r = jr(home)
    assert r.returncode == 1 and "foydalanish: jsonreshape" in text(r.stderr)
    r = jr(home, "inspect", tmp_path / "missing.json")
    assert r.returncode == 1 and text(r.stderr).startswith("Xato: Fayl topilmadi")
    r = jr(home, "filter", data, tmp_path / "o.json", "--where", "id <")
    assert r.returncode == 1 and text(r.stderr).splitlines()[1:3] == ["  id <", "      ^"]
    r = jr(home, "convert", data, data)
    assert r.returncode == 1 and "Natija manba faylning o'ziga yozilmaydi" in text(r.stderr)
    bad = tmp_path / "bad.json"
    bad.write_bytes(b'[{"a": 1},\n {"a": 2}\n {"a": 3}]')
    r = jr(home, "filter", bad, tmp_path / "o.json", "--where", "a > 0")
    assert r.returncode == 2
    err = text(r.stderr)
    assert "(3-qator, 2-ustun, 22-bayt)" in err and "--allow-partial" in err
    assert not (tmp_path / "o.json").exists()
    r = jr(home, "filter", bad, tmp_path / "o.json", "--where", "a > 0", "--allow-partial")
    assert r.returncode == 0 and (tmp_path / "o.json").exists()


# ----------------------------------------------------------------------------------
# UTF-8 when redirected, non-ASCII paths, the log file
# ----------------------------------------------------------------------------------
def test_redirected_output_is_utf8(home: Path, data: Path) -> None:
    r = jr(home, "search", data, "toshkent", "--translit")
    assert r.returncode == 0, r.stderr
    out = text(r.stdout)
    assert out.splitlines() == [
        "#0  city  [qiymat]  " + chr(0xAB) + "Toshkent" + chr(0xBB),
        "#1  city  [qiymat]  " + chr(0xAB) + TOSHKENT_CYR + chr(0xBB),
    ]
    assert "2 ta moslik, 2 ta yozuvda" in text(r.stderr)
    r = jr(home, "inspect", data)
    assert r.returncode == 0 and UZ_TEXT in text(r.stdout) and TOSHKENT_CYR in text(r.stdout)
    r = jr(home, "search", data, UZ_TEXT, "--count")
    assert r.returncode == 0 and text(r.stdout) in ("1 ta moslik, 1 ta yozuvda\n", "1 ta moslik, 1 ta yozuvda\r\n")


def test_non_ascii_paths(home: Path, data: Path, tmp_path: Path) -> None:
    folder = tmp_path / TOSHKENT_CYR
    folder.mkdir()
    src = folder / (UZ_TEXT + ".json")
    src.write_bytes(data.read_bytes())
    out = folder / "natija.jsonl"
    r = jr(home, "convert", src, out)
    assert r.returncode == 0, text(r.stderr)
    assert str(out) in text(r.stderr) and out.exists()
    r = jr(home, "inspect", src)
    assert r.returncode == 0 and str(src) in text(r.stdout)
    r = jr(home, "--lang", "en", "inspect", folder / "yoq.json")
    assert r.returncode == 1 and TOSHKENT_CYR in text(r.stderr)


def test_log_file_and_verbose(home: Path, data: Path) -> None:
    r = jr(home, "inspect", data, "--sample", "2")
    assert r.returncode == 0
    assert "INFO" not in text(r.stderr)
    log = home / "logs" / "jsonreshape.log"
    assert log.exists()
    content = log.read_text(encoding="utf-8")
    assert f"jsonreshape {VERSION}: inspect {data} --sample 2" in content
    assert "exit code 0 after" in content and "environment: jsonreshape" in content
    r = jr(home, "-v", "inspect", data)
    assert r.returncode == 0
    err = text(r.stderr)
    assert "INFO jsonreshape.cli: jsonreshape" in err and "INFO jsonreshape.reader: opened" in err


def test_internal_error_points_to_the_log(home: Path, data: Path) -> None:
    code = ("import sys\n"
            "from jsonreshape import cli, schema\n"
            "def boom(*a, **k):\n"
            "    raise RuntimeError('ichki')\n"
            "schema.infer_schema = boom\n"
            "sys.exit(cli.main(sys.argv[1:]))\n")
    r = subprocess.run([sys.executable, "-c", code, "inspect", str(data)], cwd=str(ROOT), env=child_env(home),
                       capture_output=True, timeout=120)
    assert r.returncode == 4
    err = text(r.stderr)
    assert "Ichki xato: RuntimeError: ichki" in err and str(home / "logs" / "jsonreshape.log") in err
    assert "Traceback" not in err
    assert "Traceback" in (home / "logs" / "jsonreshape.log").read_text(encoding="utf-8")


# ----------------------------------------------------------------------------------
# backend selection in a fresh process
# ----------------------------------------------------------------------------------
def rust_extension_built() -> bool:
    import importlib.util

    return importlib.util.find_spec("jsonreshape.core._rscore") is not None


def test_importing_the_cli_does_not_load_the_core(home: Path) -> None:
    code = ("import sys\n"
            "from jsonreshape import cli\n"
            "print('jsonreshape.core' in sys.modules)\n"
            "rc = cli.main(['--help'])\n"
            "print(rc, 'jsonreshape.core' in sys.modules)\n")
    r = subprocess.run([sys.executable, "-c", code], cwd=str(ROOT), env=child_env(home), capture_output=True,
                       timeout=120)
    assert r.returncode == 0, r.stderr
    lines = text(r.stdout).splitlines()
    assert lines[0] == "False" and lines[-1] == "0 False"


@pytest.mark.parametrize("backend", ["python", "rust", "auto"])
def test_backend_flag_selects_the_core(home: Path, data: Path, backend: str) -> None:
    if backend != "python" and not rust_extension_built():
        pytest.skip("the Rust core is not built")
    env = {"JSONRESHAPE_BACKEND": "python"} if backend == "rust" else {}  # the flag wins over the variable
    r = jr(home, "--backend", backend, "inspect", data, "--json", **env)
    assert r.returncode == 0, text(r.stderr)
    expected = "python" if backend == "python" else "rust"
    assert json.loads(text(r.stdout))["core_backend"] == expected
    r = jr(home, "inspect", data, "--json", "--backend", backend, **env)  # also after the command
    assert json.loads(text(r.stdout))["core_backend"] == expected


# ----------------------------------------------------------------------------------
# a reader that goes away, cancellation
# ----------------------------------------------------------------------------------
def write_big(path: Path, records: int) -> None:
    rec = (b'{"id": %d, "name": "Toshkent %d", "contact": {"city": "Samarqand", "zip": 123456}, '
           b'"tags": ["a", "b", "c"], "score": 1.10}')
    with open(path, "wb") as f:
        f.write(b"[")
        for i in range(records):
            if i:
                f.write(b",\n")
            f.write(rec % (i, i))
        f.write(b"]")


def test_closed_stdout_pipe_ends_quietly(home: Path, tmp_path: Path) -> None:
    big = tmp_path / "many.json"
    write_big(big, 20000)
    proc = subprocess.Popen([sys.executable, "-m", "jsonreshape", "search", str(big), "Toshkent", "--limit", "100000"],
                            cwd=str(ROOT), env=child_env(home), stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    first = proc.stdout.readline()
    assert first.startswith(b"#0  name  [qiymat]")
    proc.stdout.close()
    err = proc.stderr.read()
    proc.wait(timeout=120)
    assert proc.returncode == 0, err
    assert b"Traceback" not in err and b"Exception ignored" not in err


def _wait_for_log(log: Path, needle: str, proc: subprocess.Popen, timeout: float = 60) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline and proc.poll() is None:
        try:
            if needle in log.read_text(encoding="utf-8", errors="replace"):
                return True
        except OSError:
            pass
        time.sleep(0.02)
    return False


@pytest.mark.slow
@pytest.mark.parametrize("stage", ["indexed", "opened"])
def test_ctrl_break_cancels_without_partial_output(home: Path, tmp_path: Path, stage: str) -> None:
    """Ctrl+Break (a new process group on Windows) / SIGINT (POSIX) while the command is
    indexing ("opened") or exporting ("indexed"): exit code 3, the cancellation message,
    and nothing but the input left in the folder."""
    work = tmp_path / "work"
    work.mkdir()
    big = work / "big.json"
    write_big(big, 250000)  # ~33 MB: the pure-Python core needs a second or two
    out = work / "out.csv"
    cmd = [sys.executable, "-m", "jsonreshape", "--backend", "python", "convert", str(big), str(out), "--no-cache"]
    kwargs: dict[str, Any] = {}
    if sys.platform == "win32":
        kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
    proc = subprocess.Popen(cmd, cwd=str(ROOT), env=child_env(home), stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                            **kwargs)
    try:
        seen = _wait_for_log(home / "logs" / "jsonreshape.log", f"{stage} " if stage == "opened" else
                             f"indexed {big.name}", proc)
        if not seen:
            proc.communicate(timeout=120)
            pytest.skip(f"the command finished before the {stage!r} stage could be interrupted")
        try:
            proc.send_signal(signal.CTRL_BREAK_EVENT if sys.platform == "win32" else signal.SIGINT)
        except (OSError, ValueError) as exc:  # pragma: no cover - no console to deliver the event
            proc.kill()
            proc.communicate(timeout=60)
            pytest.skip(f"cannot deliver a console control event here: {exc}")
        stdout, stderr = proc.communicate(timeout=120)
    finally:
        if proc.poll() is None:  # pragma: no cover
            proc.kill()
            proc.communicate(timeout=60)
    if proc.returncode == 0:
        pytest.skip("the command completed before the signal arrived")
    assert proc.returncode == 3, text(stderr)
    assert text(stderr).strip().endswith("Amal bekor qilindi.")
    assert sorted(p.name for p in work.iterdir()) == ["big.json"]
    log = (home / "logs" / "jsonreshape.log").read_text(encoding="utf-8")
    assert "cancelled" in log and "exit code 3" in log
