"""Check one Russian translation part: `python check_ru.py ru_in_N.json ru_part_N.py`.

The part file is a Python module defining `MESSAGES_RU_PART: dict[str, str]`. Checks: every key of the input is
present (missing ones listed), no unknown keys, every value is a non-empty str, the `{name}` and `%(name)s`
placeholders of each value are exactly those of the English text, `\n` counts match the English text, no value is
identical to the English one unless the English one has no letters (pure symbols / codes) or is a proper name.
Exit code 1 on any problem; prints a coverage line.
"""
from __future__ import annotations

import importlib.util
import json
import re
import sys
from pathlib import Path

ALLOWED_EMPTY = {"json.expected.none"}   # intentionally empty in every language (tests/test_i18n.py)
PLACEHOLDER = re.compile(r"\{[a-zA-Z_][a-zA-Z0-9_]*(?:![rs])?(?::[^{}]*)?\}|%\([a-zA-Z_][a-zA-Z0-9_]*\)[sdr]")
BRACES = re.compile(r"\{\{|\}\}")


def placeholders(text: str) -> list[str]:
    cleaned = BRACES.sub("", text)
    return sorted(re.sub(r"(:[^{}]*)?\}$", "}", p) for p in PLACEHOLDER.findall(cleaned))


def main(inp: str, part: str) -> int:
    items = json.loads(Path(inp).read_text(encoding="utf-8"))
    spec = importlib.util.spec_from_file_location("ru_part", part)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)  # type: ignore[union-attr]
    ru: dict[str, str] = mod.MESSAGES_RU_PART
    keys = [i["key"] for i in items]
    en = {i["key"]: i["en"] for i in items}
    problems: list[str] = []
    missing = [k for k in keys if k not in ru]
    unknown = [k for k in ru if k not in en]
    if missing:
        problems.append(f"missing {len(missing)} keys, first: {missing[:5]}")
    if unknown:
        problems.append(f"unknown keys: {unknown[:5]}")
    same = 0
    for k, v in ru.items():
        if k not in en:
            continue
        if not isinstance(v, str) or (not v.strip() and k not in ALLOWED_EMPTY):
            problems.append(f"{k}: empty or not a string")
            continue
        if placeholders(v) != placeholders(en[k]):
            problems.append(f"{k}: placeholders {placeholders(v)} != en {placeholders(en[k])}")
        if v.count("\n") != en[k].count("\n"):
            problems.append(f"{k}: line breaks {v.count(chr(10))} != en {en[k].count(chr(10))}")
        if v == en[k] and re.search(r"[A-Za-z]{3,}", en[k]) and not re.fullmatch(r"[A-Za-z0-9 ._\-/:+*%()]+", en[k]):
            same += 1
    if same:
        problems.append(f"{same} values identical to English (allowed only for codes / names) — review them")
    done = sum(1 for k in keys if k in ru)
    print(f"coverage {done}/{len(keys)} ({100 * done / max(1, len(keys)):.1f} %)")
    for p in problems:
        print("PROBLEM:", p)
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1], sys.argv[2]))
