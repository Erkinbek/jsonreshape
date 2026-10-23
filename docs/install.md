---
title: Installation
description: Install jsonreshape on Windows, Linux or macOS — from PyPI with pip, as a prebuilt Windows executable, or from source. Python 3.11+.
---

# Installation

jsonreshape runs on **Windows, Linux and macOS** and needs **Python 3.11+**. The prebuilt
Windows executable bundles everything and needs nothing installed.

## From PyPI (pip)

```bash
pip install jsonreshape             # CLI + Python library
pip install "jsonreshape[gui]"      # add the PySide6 desktop window
pip install "jsonreshape[parquet]"  # add Parquet export (pyarrow)
pip install "jsonreshape[import]"   # add YAML import (pyyaml)
```

Then run `jsonreshape --help` (CLI) or `jsonreshape-gui` (desktop window).

!!! note
    The Windows installer and portable `.zip`, the Linux and macOS builds and the wheels of
    every version are on the [GitHub Releases] page; PyPI has jsonreshape from **v0.1.1** on.
    A winget package is not available yet.

## Prebuilt executables (Windows, Linux, macOS)

Download the Windows installer or `.zip`, or the Linux / macOS archive, from the [GitHub Releases]
page, check it against `SHA256SUMS.txt`, and run `jsonreshape-gui` (the window) or `jsonreshape`
(the command line) — no Python required.

[GitHub Releases]: https://github.com/Erkinbek/jsonreshape/releases

## From source

```bash
git clone https://github.com/Erkinbek/jsonreshape
cd jsonreshape
python -m venv .venv
.venv\Scripts\python -m pip install -U pip maturin
.venv\Scripts\python -m pip install -e ".[gui]"   # drop [gui] for CLI only
```

maturin builds the Rust core automatically; if Rust is unavailable, jsonreshape falls back to
a pure-Python implementation (same results, slower). Build a standalone `.exe` with:

```bash
powershell -ExecutionPolicy Bypass -File scripts\build_exe.ps1
```

## Verify

```bash
jsonreshape --version
jsonreshape inspect yourfile.json
```

Next: [open a large JSON file](guides/open-large-json-file.md).
