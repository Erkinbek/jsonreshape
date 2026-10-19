"""Entry script of ``jsonreshape.exe`` -- the console CLI -- in the PyInstaller build (``packaging/jsonreshape.spec``).

``multiprocessing.freeze_support()`` runs first: a worker process of the frozen program is this exe started again
with ``--multiprocessing-fork``; the call runs the worker and exits (the spawn start method never re-imports the
main module of a frozen Windows program).
"""

from __future__ import annotations

import multiprocessing
import sys

if __name__ == "__main__":
    multiprocessing.freeze_support()
    from jsonreshape.cli import main

    sys.exit(main())
