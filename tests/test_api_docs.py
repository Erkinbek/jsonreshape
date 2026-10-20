"""``docs/API.md`` is executable documentation, and ``import jsonreshape`` stays cheap.

* every ```python block of ``docs/API.md`` runs, in order, in one namespace, from an empty working
  directory (the examples create ``data.json`` themselves and assert their own results);
* every name of ``jsonreshape.__all__`` resolves (lazily, PEP 562) to the object of its defining module,
  ``dir(jsonreshape)`` lists it, no name shadows a submodule, and the table of docs/API.md §17 agrees;
* ``import jsonreshape`` imports neither PySide6 nor the heavy modules (checked in a subprocess) and costs
  only a few milliseconds (``python -X importtime``).
"""

from __future__ import annotations

import importlib
import json
import re
import subprocess
import sys
from pathlib import Path

import pytest

import jsonreshape
from jsonreshape import i18n

ROOT = Path(__file__).resolve().parents[1]
PKG = ROOT / "jsonreshape"
API_MD = ROOT / "docs" / "API.md"

# modules that ``import jsonreshape`` must not load (the CLI imports what each command needs)
HEAVY_PREFIXES = ("PySide6", "orjson", "ijson", "xlsxwriter", "jmespath", "psutil", "sqlite3", "jsonreshape.")
IMPORT_TIME_LIMIT_US = 10_000       # measured ~1.7 ms (the old 3-line __init__: ~0.7 ms); generous for a busy machine


def python_blocks(text: str) -> list[tuple[int, str]]:
    """``(first line number, code)`` of every ```python block of ``text``, in document order."""
    blocks: list[tuple[int, str]] = []
    for m in re.finditer(r"^```python\n(.*?)^```", text, flags=re.S | re.M):
        blocks.append((text.count("\n", 0, m.start()) + 2, m.group(1)))
    return blocks


@pytest.fixture
def _keep_language():
    """The examples switch the UI language; the other tests expect the default afterwards."""
    before = i18n.get_language()
    try:
        yield
    finally:
        i18n.set_language(before)


def test_api_md_examples_run_in_order(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, _keep_language) -> None:
    text = API_MD.read_text(encoding="utf-8")
    blocks = python_blocks(text)
    assert len(blocks) >= 17, "docs/API.md has one example per section"
    monkeypatch.chdir(tmp_path)                 # the examples write into the current directory
    namespace: dict[str, object] = {"__name__": "api_docs_examples"}
    for line, code in blocks:
        try:
            exec(compile(code, f"{API_MD.name}:{line}", "exec"), namespace)      # noqa: S102 -- documentation test
        except Exception as exc:                                                   # pragma: no cover - diagnostics
            raise AssertionError(f"docs/API.md example starting at line {line} failed: {exc!r}") from exc
    # the files the examples promise exist, the cancelled export left nothing behind, the input is intact
    for name in ("sorted.jsonl", "data.csv", "orders.xlsx", "data.sqlite", "first3.json", "orders0.json",
                 "edited.json", "mapping.json", "reshaped.json", "data.schema.json", "merged.json", "fixed.json",
                 "diff.json", "copy.json"):
        assert (tmp_path / name).is_file(), name
    assert not (tmp_path / "never.json").exists()
    assert json.loads((tmp_path / "data.json").read_text(encoding="utf-8")) == namespace["records"]


def test_api_md_mentions_only_names_that_exist() -> None:
    """Every ``jsonreshape.<module>.<name>`` written in the document resolves."""
    text = API_MD.read_text(encoding="utf-8")
    seen = set(re.findall(r"`jsonreshape\.([a-z_]+)\.([A-Za-z_]+)", text))
    assert seen
    for module, name in sorted(seen):
        mod = importlib.import_module(f"jsonreshape.{module}")
        assert hasattr(mod, name), f"jsonreshape.{module}.{name}"


def test_all_names_resolve_lazily_to_their_modules() -> None:
    assert "__version__" in jsonreshape.__all__ and jsonreshape.__version__
    assert len(jsonreshape.__all__) == len(set(jsonreshape.__all__))
    for name in jsonreshape.__all__:
        assert getattr(jsonreshape, name) is not None, name
    assert set(jsonreshape.__all__) <= set(dir(jsonreshape))
    with pytest.raises(AttributeError):
        jsonreshape.no_such_name  # noqa: B018
    submodules = {p.stem for p in PKG.glob("*.py")} | {p.name for p in PKG.iterdir() if p.is_dir()}
    for name, (module, attr) in jsonreshape._LAZY.items():
        mod = importlib.import_module(f"jsonreshape.{module}")
        assert getattr(jsonreshape, name) is getattr(mod, attr), name
        exported = getattr(mod, "__all__", None)
        assert exported is None or attr in exported, f"{module}.{attr} is not in {module}.__all__"
        # a top-level name never shadows a submodule (jsonreshape.search stays the module)
        assert name not in submodules, name
    assert jsonreshape.apply_mapping is jsonreshape.engine.apply


def test_api_md_stability_table_matches_all() -> None:
    text = API_MD.read_text(encoding="utf-8")
    table = text.split("## 17. Stability policy", 1)[1]
    in_table = set(re.findall(r"`([A-Za-z_]+)`", table))
    public = set(jsonreshape.__all__) - {"__version__"}
    assert public <= in_table, sorted(public - in_table)
    modules = set(re.findall(r"\| `([a-z0-9]+)` \|", table))
    assert modules == {m for m, _ in jsonreshape._LAZY.values()}, modules


def _import_time_us() -> int:
    proc = subprocess.run([sys.executable, "-X", "importtime", "-c", "import jsonreshape"], capture_output=True,
                          text=True, check=True, cwd=str(ROOT))
    for line in proc.stderr.splitlines():
        parts = [p.strip() for p in line.split("|")]
        if len(parts) == 3 and parts[2] == "jsonreshape":
            return int(parts[1])
    raise AssertionError(proc.stderr)


def test_import_jsonreshape_is_lazy_and_cheap() -> None:
    code = ("import sys, json; before = set(sys.modules); import jsonreshape; "
            "print(json.dumps([jsonreshape.__version__, sorted(set(sys.modules) - before)]))")
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, check=True, cwd=str(ROOT))
    version, loaded = json.loads(out.stdout)
    assert version == jsonreshape.__version__ and "jsonreshape" in loaded
    heavy = [m for m in loaded if m.startswith(HEAVY_PREFIXES)]
    assert heavy == [], heavy
    assert min(_import_time_us() for _ in range(3)) < IMPORT_TIME_LIMIT_US
