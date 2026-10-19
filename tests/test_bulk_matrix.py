"""jsonreshape.bulk over the full matrix (docs/ARCHITECTURE.md §12.6; AGENT_PROMPT §14 test 20).

For every rule of the §12.6 table (several parameter sets each: :data:`RULES`) x all 7 document kinds
(+ a wrapped document whose record container is an object) x both core backends x compact / pretty
documents: the preview equals the independent naive reference of ``tests/test_bulk.py`` (records / values
changed, conflicts, failures, skipped huge records, new bytes, the first 5 examples and problems), and
after the step is committed to a real EditStore and applied, the effective records differ exactly where
and as much as the preview said (test 20); undo restores every byte. This file starts from the plain
file; ``test_bulk_matrix_edited.py`` from an edited state, ``test_bulk_scopes.py`` runs the rules over
RecordSet scopes.
"""

from __future__ import annotations

from typing import Any

import pytest

from jsonreshape.bulk import BulkSpec
from test_bulk import Rig, backend, check_rule, rigs  # noqa: F401 - fixtures

KINDS = ("array", "jsonl", "brace_list", "wrapped", "wrapped_obj", "keyed", "single", "stream")

RULES: list[tuple[str, dict[str, Any]]] = [
    ("replace", {"find": "toshkent", "replacement": "Tashkent"}),
    ("replace", {"find": "o'zbekiston", "replacement": "UZ", "whole_word": True, "path": "name"}),
    ("replace", {"find": "a", "replacement": "\\1\\\\", "case_sensitive": True, "path": "tags"}),
    ("replace", {"find": "ў", "replacement": "oʻ", "path": "..esc"}),
    ("replace", {"find": r"(\d)(\d)", "replacement": r"\2\1", "mode": "regex", "path": "contact"}),
    ("replace", {"find": "[a-z]+", "replacement": "w", "mode": "regex", "whole_word": True, "path": "**.extra"}),
    ("rename_key", {"old": "city", "new": "shahar"}),
    ("rename_key", {"old": "phone", "new": "address"}),
    ("rename_key", {"old": "oid", "new": "order_id", "path": "orders[*]"}),
    ("rename_key", {"old": "id", "new": "ID", "path": "$"}),
    ("delete_field", {"path": "..zip"}),
    ("delete_field", {"path": "orders[*].sum"}),
    ("delete_field", {"path": "extra.*"}),
    ("delete_field", {"path": "nums"}),
    ("set_field", {"path": "contact.address.country", "value": '"Oʻzbekiston"'}),
    ("set_field", {"path": "score", "value": "1.10", "mode": "if_exists"}),
    ("set_field", {"path": "note", "value": "null", "mode": "if_missing"}),
    ("set_field", {"path": "name.first", "value": "1"}),
    ("set_field", {"path": "orders[0].oid", "value": "12345678901234567890"}),
    ("set_field", {"path": "[1].id", "value": "-0.0"}),
    ("clean_strings", {"trim": True, "collapse_spaces": True}),
    ("clean_strings", {"collapse_spaces": True, "path": "tags"}),
    ("convert_type", {"path": "id", "to": "string"}),
    ("convert_type", {"path": "..city", "to": "bool"}),
    ("convert_type", {"path": "orders[*]", "to": "array"}),
    ("convert_type", {"path": "score", "to": "number"}),
    ("convert_type", {"path": "tags", "to": "string"}),
    ("convert_type", {"path": "nums[*]", "to": "string"}),
    ("convert_type", {"path": "**.name", "to": "number"}),
    ("dedupe", {"key": "active"}),
    ("dedupe", {"key": "contact.address.city", "keep": "last"}),
    ("dedupe", {"key": "$key"}),
]


def run_matrix(rigs, kind: str, style: str, *, edited: bool) -> None:
    rig: Rig = rigs(kind, n=24, seed=KINDS.index(kind) * 3 + len(style), style=style)
    if edited:
        rig.edit_some(seed=KINDS.index(kind) + 1)
    totals = dict(records_changed=0, conflicts=0, failures=0)
    for rule, params in RULES:
        pv = check_rule(rig, BulkSpec(rule, params))
        for k in totals:
            totals[k] += getattr(pv, k)
    assert all(totals.values()), totals          # changes, conflicts and failures were all exercised


@pytest.mark.parametrize("style", ["compact", "pretty2"])
@pytest.mark.parametrize("kind", KINDS)
def test_every_rule_on_the_plain_file(rigs, backend, kind, style) -> None:
    run_matrix(rigs, kind, style, edited=False)
