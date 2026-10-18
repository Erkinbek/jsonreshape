"""session.same_node: duplicate member keys share a path, so complete ipaths decide."""

from __future__ import annotations

import pytest

pytest.importorskip("PySide6")

from jsonreshape.gui.session import NodeRef, same_node  # noqa: E402

pytestmark = pytest.mark.gui


def test_same_node_compares_complete_ipaths() -> None:
    a = NodeRef(2, ("contact", "x"), (1, 0))
    b = NodeRef(2, ("contact", "x"), (1, 3))   # a later duplicate "x"
    assert not same_node(a, b)
    assert same_node(a, NodeRef(2, ("contact", "x"), (1, 0)))
    # one side without (complete) indices: the path decides
    assert same_node(a, NodeRef(2, ("contact", "x")))
    assert same_node(NodeRef(2, ("contact", "x"), (1,)), b)
    assert not same_node(a, NodeRef(3, ("contact", "x"), (1, 0)))
    assert not same_node(a, None) and same_node(None, None)
