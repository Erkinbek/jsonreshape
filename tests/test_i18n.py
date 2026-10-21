"""jsonreshape.i18n: the single dictionary (AGENT_PROMPT §2 "Hamma UI matnlari bitta lug'at
faylida", §11 error messages).

* every ``t("literal")`` key used anywhere in ``jsonreshape/`` (AST scan of all .py files),
  every literal key passed to an error class and every ``key = "..."`` of an error class
  exists in ``MESSAGES`` with non-empty ``uz`` and ``en`` texts;
* the dynamic key families (``json.error.<code>``, ``json.expected.<code>``) are complete;
* placeholders agree between the languages, keys follow the section prefixes, no key is
  defined twice;
* ``t()`` never raises on missing/odd parameters; ``set_language``/``get_language``.
"""

from __future__ import annotations

import ast
import os
import runpy
import string
import subprocess
import sys
from pathlib import Path

import pytest

import jsonreshape
from jsonreshape import i18n
from jsonreshape.i18n import MESSAGES, get_language, has, set_language, t

PKG = Path(jsonreshape.__file__).resolve().parent
ROOT = PKG.parent
ALLOWED_EMPTY = {"json.expected.none"}  # intentionally empty (no "expected" text)

# error classes whose first positional argument is an i18n key (errors.py)
KEY_FIRST_ERRORS = {
    "JsonReshapeError", "Cancelled", "UserInputError", "QuerySyntaxError", "PathSyntaxError",
    "FileFormatError", "TooLargeError", "FileChangedError", "ExportError", "IndexBuildError",
}
# error classes whose first positional argument is NOT a key
NON_KEY_ERRORS = {"JsonSyntaxError", "JsonDecodeError"}


def package_sources() -> list[Path]:
    return sorted(p for p in PKG.rglob("*.py") if "__pycache__" not in p.parts)


def _parse(path: Path) -> ast.AST:
    return ast.parse(path.read_text(encoding="utf-8"), filename=str(path))


def _base_names(node: ast.ClassDef) -> set[str]:
    names = set()
    for b in node.bases:
        if isinstance(b, ast.Name):
            names.add(b.id)
        elif isinstance(b, ast.Attribute):
            names.add(b.attr)
    return names


def _error_classes(trees: dict[Path, ast.AST]) -> tuple[set[str], set[str]]:
    """(key-first error classes, non-key error classes) incl. subclasses defined anywhere."""
    key_first, non_key = set(KEY_FIRST_ERRORS), set(NON_KEY_ERRORS)
    changed = True
    while changed:
        changed = False
        for tree in trees.values():
            for node in ast.walk(tree):
                if isinstance(node, ast.ClassDef):
                    bases = _base_names(node)
                    if bases & non_key and node.name not in non_key:
                        non_key.add(node.name)
                        changed = True
                    elif bases & key_first and node.name not in key_first and node.name not in non_key:
                        key_first.add(node.name)
                        changed = True
    return key_first - non_key, non_key


def _str_const(node: ast.AST | None) -> str | None:
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    return None


def used_keys() -> dict[str, list[str]]:
    """Literal i18n keys used in the package -> where they are used."""
    trees = {p: _parse(p) for p in package_sources()}
    key_first, _ = _error_classes(trees)
    found: dict[str, list[str]] = {}

    def add(key: str, path: Path, node: ast.AST) -> None:
        found.setdefault(key, []).append(f"{path.relative_to(ROOT)}:{getattr(node, 'lineno', '?')}")

    for path, tree in trees.items():
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and node.args:
                func = node.func
                first = _str_const(node.args[0])
                if first is None:
                    continue
                if isinstance(func, ast.Name) and func.id == "t":
                    add(first, path, node)
                elif isinstance(func, ast.Attribute) and func.attr == "t" and isinstance(func.value, ast.Name) \
                        and func.value.id == "i18n":
                    add(first, path, node)
                elif isinstance(func, ast.Name) and func.id in key_first:
                    add(first, path, node)
                elif isinstance(func, ast.Attribute) and func.attr in key_first:
                    add(first, path, node)
            elif isinstance(node, ast.ClassDef) and node.name in key_first:
                for stmt in node.body:
                    value = None
                    if isinstance(stmt, ast.Assign) and any(isinstance(tg, ast.Name) and tg.id == "key"
                                                            for tg in stmt.targets):
                        value = _str_const(stmt.value)
                    elif isinstance(stmt, ast.AnnAssign) and isinstance(stmt.target, ast.Name) \
                            and stmt.target.id == "key":
                        value = _str_const(stmt.value)
                    if value is not None:
                        add(value, path, stmt)
    return found


def test_scan_finds_known_keys() -> None:
    keys = used_keys()
    # sanity check of the scanner itself
    for key in ("error.generic", "error.cancelled", "paths.err.key_after_dot", "error.file_not_found",
                "fileio.error.utf32", "settings.invalid", "env.report.title"):
        assert key in keys, key


def test_every_used_key_exists_in_both_languages() -> None:
    problems = []
    for key, where in sorted(used_keys().items()):
        entry = MESSAGES.get(key)
        if entry is None:
            problems.append(f"missing key {key!r} (used at {', '.join(where[:3])})")
            continue
        if key in ALLOWED_EMPTY:
            continue
        for lang in ("uz", "en"):
            if not isinstance(entry.get(lang), str) or not entry[lang].strip():
                problems.append(f"{key!r} has no {lang!r} text (used at {where[0]})")
    assert not problems, "\n".join(problems)


def test_every_message_has_uz_and_en() -> None:
    problems = []
    for key, entry in MESSAGES.items():
        if not isinstance(entry, dict):
            problems.append(f"{key}: not a dict")
            continue
        extra = set(entry) - {"uz", "en"}
        if extra:
            problems.append(f"{key}: unknown languages {sorted(extra)}")
        for lang in ("uz", "en"):
            text = entry.get(lang)
            if not isinstance(text, str):
                problems.append(f"{key}: {lang} missing")
            elif not text.strip() and key not in ALLOWED_EMPTY:
                problems.append(f"{key}: {lang} empty")
    assert not problems, "\n".join(problems)


def _fields(text: str) -> set[str] | None:
    """Placeholder names, or None when the text is not a valid format string (a literal
    such as "'{' kutilgan", which t() returns unchanged)."""
    try:
        return {name.split(".")[0].split("[")[0] for _, name, _, _ in string.Formatter().parse(text) if name}
    except ValueError:
        return None


def test_placeholders_agree_between_languages() -> None:
    problems = []
    for key, entry in MESSAGES.items():
        uz, en = _fields(entry.get("uz", "")), _fields(entry.get("en", ""))
        if uz is None and en is None:
            continue  # literal texts in both languages (used without parameters)
        if uz != en:
            problems.append(f"{key}: uz {sorted(uz) if uz is not None else 'literal'} != "
                            f"en {sorted(en) if en is not None else 'literal'}")
    assert not problems, "\n".join(problems)


def test_dynamic_key_families_are_complete() -> None:
    constants = runpy.run_path(str(PKG / "core" / "constants.py"))
    for code in constants["ERROR_CODES"]:
        assert has(f"json.error.{code}"), code
    for code in constants["EXPECTED_CODES"]:
        assert has(f"json.expected.{code}"), code


def _messages_dict_node() -> ast.Dict:
    tree = _parse(PKG / "i18n.py")
    for node in ast.walk(tree):
        target = None
        if isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            target = node.target.id
        elif isinstance(node, ast.Assign) and len(node.targets) == 1 and isinstance(node.targets[0], ast.Name):
            target = node.targets[0].id
        if target == "MESSAGES" and isinstance(node.value, ast.Dict):
            return node.value
    raise AssertionError("MESSAGES dict literal not found")


def test_no_key_is_defined_twice() -> None:
    seen: dict[str, int] = {}
    dupes = []
    for k in _messages_dict_node().keys:
        key = _str_const(k)
        if key is None:
            continue
        if key in seen:
            dupes.append(f"{key!r} at lines {seen[key]} and {k.lineno}")
        seen[key] = k.lineno
    assert not dupes, "duplicate keys (the later one silently wins):\n" + "\n".join(dupes)


def test_keys_carry_their_section_prefix() -> None:
    """Each module adds keys inside its own ``# ---- <section> ----`` block, prefixed with
    the section name (docs/ARCHITECTURE.md §0); "errors" uses "error.", "json (...)" "json."."""
    lines = (PKG / "i18n.py").read_text(encoding="utf-8").splitlines()
    key_lines = {k.lineno: _str_const(k) for k in _messages_dict_node().keys}
    section = None
    problems = []
    for lineno, line in enumerate(lines, start=1):
        stripped = line.strip()
        if stripped.startswith("# ---- ") and stripped.endswith(" ----"):
            section = stripped[len("# ---- "):-len(" ----")].split()[0]
            continue
        key = key_lines.get(lineno)
        if key is None:
            continue
        prefix = {"errors": "error"}.get(section or "", section or "")
        if not (key == prefix or key.startswith(prefix + ".")):
            problems.append(f"line {lineno}: {key!r} is in section {section!r}")
    assert not problems, "\n".join(problems)


# ----------------------------------------------------------------------------------
# t() behaviour
# ----------------------------------------------------------------------------------
@pytest.fixture
def scratch_messages(monkeypatch: pytest.MonkeyPatch):
    entries = {
        "test.both": {"uz": "Salom, {name}!", "en": "Hello, {name}!"},
        "test.only_en": {"en": "English only {x}"},
        "test.empty_uz": {"uz": "", "en": "fallback"},
        "test.odd": {"uz": "a {b} {c:>5} {d.e} {f[0]} { {g!z}", "en": "x"},
        "test.unbalanced": {"uz": "brace { here {x}", "en": "y"},
        "test.nothing": {},
    }
    for k, v in entries.items():
        monkeypatch.setitem(MESSAGES, k, v)
    return entries


def test_t_formats_and_falls_back(scratch_messages, lang_uz) -> None:
    assert t("test.both", name="Ali") == "Salom, Ali!"
    assert t("test.both") == "Salom, {name}!"  # no params: raw text
    assert t("test.both", other=1) == "Salom, {name}!"  # missing param stays visible
    assert t("test.only_en", x=1) == "English only 1"  # uz missing -> en
    assert t("test.empty_uz") == "fallback"  # empty uz -> en
    assert t("test.nothing") == "test.nothing"  # no text at all -> key
    assert t("no.such.key", a=1) == "no.such.key"
    set_language("en")
    assert t("test.both", name="Ali") == "Hello, Ali!"


def test_t_never_raises_on_odd_format_strings(scratch_messages, lang_uz) -> None:
    for key in ("test.odd", "test.unbalanced"):
        for params in ({}, {"b": 1}, {"x": 2, "c": "q"}, {"d": object(), "f": [1]}):
            out = t(key, **params)
            assert isinstance(out, str) and out
    assert t("test.unbalanced", x=1) == "brace { here {x}"  # invalid format: text unchanged


def test_t_with_real_keys(lang_en) -> None:
    assert t("error.file_not_found", path="C:\\a.json") == "File not found: C:\\a.json"
    assert t("error.file_not_found") == "File not found: {path}"
    set_language("uz")
    assert t("error.file_not_found", path="a.json") == "Fayl topilmadi: a.json"
    assert t("error.json_syntax", reason="x", line=2, column=5, offset=10).count("2") >= 1


def test_set_and_get_language() -> None:
    old = get_language()
    try:
        set_language(" EN ")
        assert get_language() == "en"
        set_language("uz")
        assert get_language() == "uz"
        with pytest.raises(ValueError):
            set_language("fr")
        assert get_language() == "uz"  # unchanged after a rejected value
        set_language("")  # empty -> default
        assert get_language() == i18n.DEFAULT_LANGUAGE
    finally:
        set_language(old)
    assert has("error.generic") and not has("no.such.key")


@pytest.mark.parametrize("env_value, expected", [("en", "en"), ("UZ", "uz"), ("ru", "ru"), ("fr", "uz"), ("", "uz")])
def test_language_from_environment(env_value: str, expected: str) -> None:
    env = dict(os.environ, JSONRESHAPE_LANG=env_value, JSONRESHAPE_BACKEND="python")
    out = subprocess.run(
        [sys.executable, "-c", "from jsonreshape.i18n import get_language; print(get_language())"],
        cwd=str(ROOT), env=env, capture_output=True, text=True, timeout=60, check=True,
    )
    assert out.stdout.strip() == expected
