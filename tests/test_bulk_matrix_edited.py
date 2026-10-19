"""jsonreshape.bulk over the full matrix, starting from an EDITED state (docs/ARCHITECTURE.md §12.6, test 20).

The same rules and checks as ``tests/test_bulk_matrix.py`` (preview == naive reference; the applied step
changes exactly what the preview counted; undo restores every byte), on a state with point edits,
inserted / deleted / moved records and an array layer under a dict layer: positions differ from record
ids, records are read from the data file and from segments, and pure runs alternate with edited ones.
"""

from __future__ import annotations

import pytest

from test_bulk import backend, rigs  # noqa: F401 - fixtures
from test_bulk_matrix import KINDS, run_matrix


@pytest.mark.parametrize("style", ["compact", "pretty2"])
@pytest.mark.parametrize("kind", KINDS)
def test_every_rule_on_an_edited_state(rigs, backend, kind, style) -> None:
    run_matrix(rigs, kind, style, edited=True)
