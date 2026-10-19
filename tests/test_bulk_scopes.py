"""jsonreshape.bulk over RecordSet scopes (docs/ARCHITECTURE.md §12.6, §12.3; test 20).

Every rule of ``tests/test_bulk_matrix.py`` on an edited state, with the scope a ``filter_records``
result (ascending, ``gen`` of the pinned state) and a ``sort_records`` result (not ascending: examined in
position order through a bitmap): preview == naive reference over the scope's positions, the applied
change == the preview, undo restores every byte.
"""

from __future__ import annotations

import pytest

from jsonreshape.bulk import BulkSpec
from jsonreshape.recordset import filter_records, sort_records
from test_bulk import Rig, backend, check_rule, rigs  # noqa: F401 - fixtures
from test_bulk_matrix import RULES


@pytest.mark.parametrize("kind", ["array", "keyed", "jsonl", "wrapped_obj"])
def test_rules_over_recordset_scopes(rigs, backend, kind) -> None:
    rig: Rig = rigs(kind, n=40, seed=17)
    rig.edit_some(seed=3)
    for rule, params in RULES:
        for make in (lambda: filter_records(rig.doc.pinned(), "score > 0 or active == true"),
                     lambda: sort_records(rig.doc.pinned(), ["-score"])):
            scope = make()            # made on the state the preview reads (every apply/undo makes a new one)
            assert scope.gen == rig.state.gen and 0 < len(scope) <= rig.state.count
            check_rule(rig, BulkSpec(rule, params), scope)
            scope.close()
