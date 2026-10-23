"""File-system facts some tests depend on (imported as ``from fs_helpers import ...``, like ``guishell_helpers``).

The tests were written on Windows; the Linux CI job (pure-Python core) runs them on a case-sensitive file system,
where ``A.JSON`` is another file than ``a.json`` -- not another spelling of it.
"""

from __future__ import annotations

import functools
import tempfile
from pathlib import Path


@functools.lru_cache(maxsize=None)
def _case_folds_in(folder: str) -> bool:
    with tempfile.TemporaryDirectory(dir=folder) as probe_dir:
        probe = Path(probe_dir) / "case-probe"
        probe.write_bytes(b"")
        return (Path(probe_dir) / "CASE-PROBE").exists()


def case_folds(path: Path) -> bool:
    """True when the file system of ``path`` (a folder) treats ``A.JSON`` and ``a.json`` as one name (NTFS, APFS by
    default), False on a case-sensitive one (ext4, ...). The probe is made next to ``path``, not in it, so a test's
    own folder listing is not disturbed."""
    return _case_folds_in(str(Path(path).resolve().parent))
