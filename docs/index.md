---
title: Open and reshape huge JSON files
description: jsonreshape opens, searches, edits and converts JSON and JSON Lines files from 100 MB to several GB without loading them into memory. Free, offline and open source for Windows, Linux and macOS.
---

# Open and reshape huge JSON files

**jsonreshape** is a free, open-source **desktop app and command-line tool** for JSON and
JSON Lines (NDJSON) files that are too large to open in a normal editor — from 100 MB to
several gigabytes.

[Install](install.md){ .md-button .md-button--primary }
[View on GitHub](https://github.com/Erkinbek/jsonreshape){ .md-button }

![The jsonreshape viewer showing the tree of a large JSON array](img/main-light.png){ loading=lazy }

## Why it is different

- **Nothing is loaded into memory as a whole.** A multi-gigabyte file is indexed once (the
  byte boundaries of every record), then only the records you look at are read. A 2 GB file is
  ready in about **3 seconds**; 10 million records sort in **77 seconds** using under 400 MB of RAM.
- **The original file is never changed.** Edits live in a separate layer; saving atomically
  replaces the file, keeping a `.bak` backup by default.
- **Offline and private.** No telemetry and no network access — your data never leaves your machine.
- **Fast core, friendly tools.** A Rust core with a pure-Python fallback, a PySide6 desktop
  window, and a scriptable CLI.

## What you can do

| Task | Guide |
|---|---|
| Open and browse a 2–5 GB JSON / JSON Lines file | [Open a large JSON file](guides/open-large-json-file.md) |
| Convert to CSV, Excel, SQLite or Parquet | [Convert JSON](guides/convert-json-to-csv-excel.md) |
| Compare two JSON files | [Compare JSON files](guides/compare-json-files.md) |
| Repair an invalid / broken JSON file | [Repair broken JSON](guides/repair-broken-json.md) |
| Flatten nested JSON into a table | [Flatten nested JSON](guides/flatten-nested-json.md) |
| Split one big file / merge many files | [Split and merge JSON](guides/split-and-merge-json.md) |

Also included: full-text and regex **search**, a **filter** language, multi-key **sort**,
field **statistics** with histograms, **duplicate** detection, **JSON Schema** generation and
validation, and a drag-and-drop **structure builder**. See the [CLI reference](cli.md) and the
[Python API](API.md).

## Install in one line

From the first release on:

```bash
pip install jsonreshape          # CLI + Python library
pip install "jsonreshape[gui]"   # add the desktop window
```

See [Installation](install.md) for prebuilt Windows binaries, building from source, and the
optional Parquet / import extras.
