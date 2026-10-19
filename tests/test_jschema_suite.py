"""Conformance of :mod:`jsonreshape.jschema` with the official JSON-Schema-Test-Suite, draft 2020-12
(vendored with its MIT LICENSE in ``tests/data/json-schema-test-suite``; docs/ARCHITECTURE.md §14.3).

Every test of every file runs. The remote documents of the suite (``http://localhost:1234/...``) are
served offline through ``compile_schema(..., resources=...)``, so ``refRemote`` runs too. What the
validator refuses on purpose (fail closed, never ignored) is asserted to be refused with the
expected localized error -- these are the documented skips:

* ``$dynamicRef`` / ``$dynamicAnchor`` (``dynamicRef.json``, ``optional/dynamicRef.json`` and the two
  ``unevaluated*`` groups that use them): ``jschema.error.schema.dynamic``;
* custom meta-schemas / other dialects (``vocabulary.json``, ``optional/format-assertion.json``,
  ``optional/cross-draft.json``): ``jschema.error.schema.dialect``;
* references to the official meta-schema (``https://json-schema.org/draft/2020-12/schema``, which
  itself needs ``$dynamicRef``): ``jschema.error.schema.remote_ref``;
* ``optional/format/*``: the formats asserted with ``formats=True`` (date-time, date, time, email,
  uri, uuid, ipv4, ipv6) must pass completely; the other formats are annotations only, so their
  "invalid" cases are skipped ("optional/format assertions when off") and the valid ones must pass.

For every test the boolean answer (``is_valid``) and the error report (``errors``) must agree.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from jsonreshape import codec
from jsonreshape.errors import UserInputError
from jsonreshape.jschema import compile_schema

SUITE = Path(__file__).resolve().parent / "data" / "json-schema-test-suite"
TESTS = SUITE / "tests" / "draft2020-12"
REMOTES = SUITE / "remotes"
ASSERTED_FORMATS = {"date-time", "date", "time", "email", "uri", "uuid", "ipv4", "ipv6"}

# (file, group description) -> the error key the refusal must carry
REFUSED_GROUPS = {
    ("unevaluatedItems.json", "unevaluatedItems with $dynamicRef"): "jschema.error.schema.dynamic",
    ("unevaluatedProperties.json", "unevaluatedProperties with $dynamicRef"): "jschema.error.schema.dynamic",
    ("ref.json", "remote ref, containing refs itself"): "jschema.error.schema.remote_ref",
    ("defs.json", "validate definition against metaschema"): "jschema.error.schema.remote_ref",
}
REFUSED_FILES = {
    "dynamicRef.json": "jschema.error.schema.dynamic",
    "optional/dynamicRef.json": "jschema.error.schema.dynamic",
    "vocabulary.json": "jschema.error.schema.dialect",
    "optional/format-assertion.json": "jschema.error.schema.dialect",
    "optional/cross-draft.json": "jschema.error.schema.dialect",
}


def _load(path: Path):
    return codec.loads(path.read_bytes())


def _remotes() -> dict:
    out = {}
    for dirpath, _dirs, files in os.walk(REMOTES):
        for name in files:
            if name.endswith(".json"):
                full = Path(dirpath) / name
                rel = full.relative_to(REMOTES).as_posix()
                out["http://localhost:1234/" + rel] = _load(full)
    return out


RESOURCES = _remotes()


def _suite_files() -> list[str]:
    out = []
    for path in sorted(TESTS.rglob("*.json")):
        out.append(path.relative_to(TESTS).as_posix())
    return out


FILES = _suite_files()


def _run_file(rel: str) -> dict[str, int]:
    """Run one suite file; returns counts and raises AssertionError listing every failure."""
    fmt_file = rel.startswith("optional/format/")
    fmt_name = rel[len("optional/format/"):-len(".json")] if fmt_file else ""
    formats = fmt_file and fmt_name in ASSERTED_FORMATS
    counts = {"passed": 0, "refused": 0, "skipped": 0}
    failures = []
    for group in _load(TESTS / rel):
        desc = group["description"]
        expected_refusal = REFUSED_FILES.get(rel) or REFUSED_GROUPS.get((rel, desc))
        try:
            cs = compile_schema(group["schema"], formats=formats, resources=RESOURCES)
        except UserInputError as exc:
            if expected_refusal is None:
                failures.append(f"{desc}: refused unexpectedly: {exc.key}: {exc}")
            elif exc.key != expected_refusal:
                failures.append(f"{desc}: refused with {exc.key}, expected {expected_refusal}")
            else:
                counts["refused"] += len(group["tests"])
            continue
        if expected_refusal is not None:
            failures.append(f"{desc}: compiled although it must be refused ({expected_refusal})")
            continue
        for case in group["tests"]:
            if fmt_file and not formats and not case["valid"]:
                counts["skipped"] += 1        # format assertion is off for this format
                continue
            data = case["data"]
            got = cs.is_valid(data)
            errors = cs.errors(data)
            if got == bool(errors):
                failures.append(f"{desc} / {case['description']}: is_valid={got} but errors={errors}")
            elif got != case["valid"]:
                failures.append(f"{desc} / {case['description']}: expected valid={case['valid']}, got {got}")
            else:
                counts["passed"] += 1
    assert not failures, f"{rel}:\n  " + "\n  ".join(failures)
    return counts


def test_suite_is_vendored_with_its_license() -> None:
    assert (SUITE / "LICENSE").read_text(encoding="utf-8").startswith("Copyright (c)")
    assert "MIT" in (SUITE / "README.md").read_text(encoding="utf-8") or "Permission is hereby granted" in \
        (SUITE / "LICENSE").read_text(encoding="utf-8")
    assert len(FILES) == 80
    assert "refRemote.json" in FILES and "optional/format/date-time.json" in FILES


@pytest.mark.parametrize("rel", FILES)
def test_official_suite_file(rel: str) -> None:
    counts = _run_file(rel)
    assert counts["passed"] + counts["refused"] + counts["skipped"] > 0


def test_official_suite_totals() -> None:
    """The overall numbers (documented in the report): everything that is not refused on purpose
    or a disabled format assertion passes."""
    total = {"passed": 0, "refused": 0, "skipped": 0}
    for rel in FILES:
        for k, v in _run_file(rel).items():
            total[k] += v
    # 1 906 tests in 80 files: 1 756 pass, 62 refused on purpose ($dynamicRef, custom dialects,
    # the official meta-schema), 88 "invalid" cases of annotation-only formats skipped
    assert total == {"passed": 1756, "refused": 62, "skipped": 88}, total


def test_refused_categories_are_exactly_the_documented_ones() -> None:
    files = {rel for rel in FILES}
    for rel in REFUSED_FILES:
        assert rel in files, rel
    for rel, _desc in REFUSED_GROUPS:
        assert rel in files, rel
