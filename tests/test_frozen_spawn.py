"""Automatic worker processes are allowed in the frozen executable (PyInstaller): its entry scripts call
``multiprocessing.freeze_support()`` first, so a spawned worker never runs the program a second time."""

from __future__ import annotations

import sys
import types

import pytest

from jsonreshape import bulk, engine


@pytest.mark.parametrize("module", [engine, bulk])
def test_a_frozen_executable_may_spawn_workers(module, monkeypatch) -> None:
    script = types.ModuleType("__main__")
    script.__file__ = r"C:\app\_internal\entry_gui.py"      # a plain script: not safe when not frozen
    script.__spec__ = None
    monkeypatch.setitem(sys.modules, "__main__", script)
    monkeypatch.delattr(sys, "frozen", raising=False)
    assert module._spawn_safe() is False
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    assert module._spawn_safe() is True
