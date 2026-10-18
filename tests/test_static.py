"""Static safety checks of the package source and the environment check.

* AST scan of ``jsonreshape/**/*.py`` (AGENT_PROMPT §1.6 offline, §6.3 / §14 test 18 "eval
  ishlatilmasin"; docs/ARCHITECTURE.md §0): no ``eval``/``exec``/builtin ``compile``,
  no ``__import__``, no ``pickle``/``marshal`` loading, no imports of network modules
  (directly, via ``from x import y`` or ``importlib.import_module("...")``), no ``print``
  in library code (CLI entry points excepted).
* ``jsonreshape.env.check_environment`` (AGENT_PROMPT §2: the ijson ``yajl2_c`` backend is
  checked at start-up) and the guarantee that it does not import network modules.
"""

from __future__ import annotations

import ast
import json
import logging
import os
import subprocess
import sys
from pathlib import Path

import pytest

import jsonreshape
from jsonreshape import env

PKG = Path(jsonreshape.__file__).resolve().parent
ROOT = PKG.parent

FORBIDDEN_BUILTINS = {"eval", "exec", "compile", "__import__"}
NETWORK_MODULES = {
    "socket", "ssl", "urllib.request", "http.client", "http.server", "requests", "ftplib", "smtplib",
    "poplib", "imaplib", "nntplib", "telnetlib", "socketserver", "xmlrpc", "urllib3", "httpx", "aiohttp",
    # Qt modules that talk to the network (the GUI is offline, AGENT_PROMPT §1.6)
    "PySide6.QtNetwork", "PySide6.QtNetworkAuth", "PySide6.QtWebEngineCore", "PySide6.QtWebEngineWidgets",
    "PySide6.QtWebEngineQuick", "PySide6.QtWebSockets", "PySide6.QtHttpServer", "PySide6.QtWebChannel",
    "PySide6.QtWebView",
}
UNSAFE_LOADS = {("pickle", "load"), ("pickle", "loads"), ("marshal", "load"), ("marshal", "loads"),
                ("pickle", "Unpickler"), ("shelve", "open")}
PRINT_ALLOWED = {"cli.py", "__main__.py"}  # command-line entry points write to stdout


def package_sources() -> list[Path]:
    return sorted(p for p in PKG.rglob("*.py") if "__pycache__" not in p.parts)


def _is_network(module: str) -> bool:
    return any(module == m or module.startswith(m + ".") for m in NETWORK_MODULES)


def _locally_defined(tree: ast.AST) -> set[str]:
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            names.add(node.name)
        elif isinstance(node, ast.Name) and isinstance(node.ctx, ast.Store):
            names.add(node.id)
        elif isinstance(node, ast.arg):
            names.add(node.arg)
        elif isinstance(node, ast.alias):
            names.add((node.asname or node.name).split(".")[0])
    return names


def scan(path: Path) -> list[str]:
    """Violations in one source file (``"file:line: what"``)."""
    rel = path.relative_to(ROOT).as_posix()
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    local = _locally_defined(tree)
    problems: list[str] = []

    def bad(node: ast.AST, what: str) -> None:
        problems.append(f"{rel}:{getattr(node, 'lineno', '?')}: {what}")

    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if _is_network(alias.name):
                    bad(node, f"imports network module {alias.name}")
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            if _is_network(node.module):
                bad(node, f"imports from network module {node.module}")
            for alias in node.names:
                if _is_network(f"{node.module}.{alias.name}"):
                    bad(node, f"imports network module {node.module}.{alias.name}")
                if (node.module, alias.name) in UNSAFE_LOADS:
                    bad(node, f"imports {node.module}.{alias.name}")
        elif isinstance(node, ast.Name) and isinstance(node.ctx, ast.Load):
            if node.id in FORBIDDEN_BUILTINS and node.id not in local:
                bad(node, f"uses builtin {node.id}")
        elif isinstance(node, ast.Attribute):
            if isinstance(node.value, ast.Name):
                if node.value.id == "builtins" and node.attr in FORBIDDEN_BUILTINS:
                    bad(node, f"uses builtins.{node.attr}")
                if (node.value.id, node.attr) in UNSAFE_LOADS:
                    bad(node, f"uses {node.value.id}.{node.attr}")
            if node.attr == "__import__":
                bad(node, "uses __import__")
        elif isinstance(node, ast.Call):
            func = node.func
            name = func.attr if isinstance(func, ast.Attribute) else func.id if isinstance(func, ast.Name) else ""
            if name in ("import_module", "__import__") and node.args:
                arg = node.args[0]
                if isinstance(arg, ast.Constant) and isinstance(arg.value, str) and _is_network(arg.value):
                    bad(node, f"dynamically imports network module {arg.value}")
            if name == "getattr" and len(node.args) >= 2:
                arg = node.args[1]
                if isinstance(arg, ast.Constant) and arg.value in FORBIDDEN_BUILTINS:
                    bad(node, f"getattr(..., {arg.value!r})")
            if isinstance(func, ast.Name) and func.id == "print" and "print" not in local \
                    and path.name not in PRINT_ALLOWED:
                bad(node, "print() in library code (use logging)")
    return problems


def test_package_sources_exist() -> None:
    names = {p.name for p in package_sources()}
    assert {"__init__.py", "codec.py", "paths.py", "fileio.py", "settings.py", "log.py", "env.py"} <= names


def test_no_eval_exec_import_tricks_or_network_imports() -> None:
    problems = [p for path in package_sources() for p in scan(path)]
    assert not problems, "\n".join(problems)


@pytest.mark.parametrize(
    "source, expected",
    [
        ("eval('1')", "uses builtin eval"),
        ("x = exec", "uses builtin exec"),
        ("compile('1', 'f', 'eval')", "uses builtin compile"),
        ("__import__('os')", "uses builtin __import__"),
        ("import builtins\nbuiltins.eval('1')", "uses builtins.eval"),
        ("getattr(__builtins__, 'eval')", "getattr(..., 'eval')"),
        ("import socket", "imports network module socket"),
        ("import urllib.request as r", "imports network module urllib.request"),
        ("from urllib import request", "imports network module urllib.request"),
        ("from http.client import HTTPConnection", "imports from network module http.client"),
        ("from http import client", "imports network module http.client"),
        ("import requests", "imports network module requests"),
        ("import ssl", "imports network module ssl"),
        ("import importlib\nimportlib.import_module('smtplib')", "dynamically imports network module smtplib"),
        ("import pickle\npickle.loads(b'')", "uses pickle.loads"),
        ("from marshal import loads", "imports marshal.loads"),
        ("print('x')", "print() in library code"),
    ],
)
def test_scanner_detects_violations(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, source: str, expected: str) -> None:
    fake_pkg = tmp_path / "jsonreshape"
    fake_pkg.mkdir()
    f = fake_pkg / "mod.py"
    f.write_text(source, encoding="utf-8")
    monkeypatch.setattr(sys.modules[__name__], "ROOT", tmp_path)
    problems = scan(f)
    assert any(expected in p for p in problems), problems


@pytest.mark.parametrize(
    "source",
    [
        "import re\nre.compile('x')",
        "import urllib.parse",
        "from urllib.parse import quote",
        "import http",
        "def compile(x):\n    return x\ncompile(1)",
        "import json\njson.loads('1')",
        "import logging\nlogging.getLogger(__name__).info('x')",
        "class A:\n    def eval(self):\n        return self.eval",
    ],
)
def test_scanner_allows_safe_code(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, source: str) -> None:
    fake_pkg = tmp_path / "jsonreshape"
    fake_pkg.mkdir()
    f = fake_pkg / "mod.py"
    f.write_text(source, encoding="utf-8")
    monkeypatch.setattr(sys.modules[__name__], "ROOT", tmp_path)
    assert scan(f) == []


# ----------------------------------------------------------------------------------
# environment check (env.py)
# ----------------------------------------------------------------------------------
def test_check_environment_reports_everything() -> None:
    info = env.check_environment()
    for key in ("app_version", "python", "python_ok", "implementation", "bits", "byteorder", "platform",
                "core_backend", "core_backends", "core_error", "rust_error", "backend_env", "ijson_backend",
                "ijson_ok", "ijson_error", "versions", "warnings", "warning_params"):
        assert key in info, key
    json.dumps(info)  # JSON-compatible
    assert info["app_version"] == jsonreshape.__version__
    assert info["python_ok"] is True and info["bits"] == 64
    assert set(info["versions"]) == set(env.PACKAGES)
    for name in env.REQUIRED_PACKAGES:
        assert info["versions"][name], name
    assert info["ijson_backend"] == "yajl2_c", "AGENT_PROMPT §2: ijson must use the yajl2_c backend"
    assert info["ijson_ok"] is True
    assert info["core_backend"] in ("rust", "python", "none")
    assert len(info["warnings"]) == len(info["warning_params"])
    if info["core_backend"] == "none":
        assert info["core_error"] and "env.warn.core_unavailable" in info["warnings"]
    else:
        assert info["core_backend"] in info["core_backends"]
    if os.environ.get("JSONRESHAPE_BACKEND", "").lower() == "python" and info["core_backend"] == "python":
        assert "disabled" in (info["rust_error"] or "")
        assert "env.warn.rust_unavailable" not in info["warnings"]  # forced, not a problem


def test_wrong_ijson_backend_is_warned(monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture) -> None:
    import ijson

    monkeypatch.setattr(ijson, "backend", "python")
    with caplog.at_level(logging.WARNING, logger="jsonreshape.env"):
        info = env.check_environment()
    assert info["ijson_ok"] is False and info["ijson_backend"] == "python"
    assert "env.warn.ijson_backend" in info["warnings"]
    assert any("yajl2_c" in r.getMessage() for r in caplog.records if r.levelno == logging.WARNING)


def test_missing_package_is_reported(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setitem(env.PACKAGES, "jmespath", ("no-such-dist-xyz", "no_such_module_xyz"))
    info = env.check_environment(log_result=False)
    assert info["versions"]["jmespath"] is None
    idx = info["warnings"].index("env.warn.missing_package")
    assert info["warning_params"][idx] == {"package": "jmespath"}


def test_format_environment_is_localized(lang_uz) -> None:
    info = env.check_environment(log_result=False)
    text_uz = env.format_environment(info)
    assert "Dastur muhiti" in text_uz and info["versions"]["orjson"] in text_uz
    from jsonreshape.i18n import set_language

    set_language("en")
    text_en = env.format_environment(info)
    assert "Program environment" in text_en and "ijson backend: yajl2_c" in text_en
    assert env.format_environment().startswith("Program environment")


def test_environment_check_imports_no_network_modules() -> None:
    code = (
        "import sys, json\n"
        "from jsonreshape.env import check_environment, format_environment\n"
        "info = check_environment()\n"
        "format_environment(info)\n"
        "bad = sorted(m for m in sys.modules if m.split('.')[0] in "
        "('socket', 'ssl', 'requests', 'urllib3', 'httpx') or m in ('urllib.request', 'http.client'))\n"
        "print(json.dumps(bad))\n"
    )
    environ = dict(os.environ, JSONRESHAPE_BACKEND="python")
    out = subprocess.run([sys.executable, "-c", code], cwd=str(ROOT), env=environ, capture_output=True,
                         text=True, timeout=120, check=True)
    assert json.loads(out.stdout.strip().splitlines()[-1]) == []
