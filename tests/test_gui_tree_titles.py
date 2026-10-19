"""Tree edit dialogs use the GUI's one title format ``#12.name — …`` (review L6)."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from jsonreshape.gui.session import DocSession, NodeRef
from jsonreshape.gui.treeview import TreeView
from jsonreshape.i18n import set_language, t

pytestmark = pytest.mark.gui


@pytest.fixture(autouse=True)
def _uz():
    set_language("uz")
    yield


def _title(ref, path=("x",)):
    me = SimpleNamespace(session=SimpleNamespace(location_text=lambda r: DocSession.location_text(None, r)))
    return TreeView._title(me, SimpleNamespace(path=path), "gui.tree.edit.title.value", ref)


def test_record_nodes_are_written_like_the_rest_of_the_gui() -> None:
    assert _title(NodeRef(1, ("name",), ())).startswith("#1.name")
    assert _title(NodeRef(12, (), ())).startswith("#12")
    assert _title(NodeRef(3, ("tags", 0), ())).startswith("#3.tags[0]")
    assert _title(NodeRef(None, ("meta", "id"), ())).startswith("meta.id")


def test_without_a_ref_the_path_label_is_used() -> None:
    me = SimpleNamespace(session=None)
    text = TreeView._title(me, SimpleNamespace(path=("data", 1, "name")), "gui.tree.edit.title.value")
    assert text == t("gui.tree.edit.title.value", path="data[1].name")
