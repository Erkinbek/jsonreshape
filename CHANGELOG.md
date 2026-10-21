# Changelog

All notable changes to jsonreshape are recorded here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/); versions follow [Semantic Versioning](https://semver.org/).

## [Unreleased]

### Added
- Apache License 2.0 (`LICENSE`, `NOTICE`, `THIRD-PARTY-NOTICES.md`), `CONTRIBUTING.md`, `SECURITY.md`, issue and
  pull request templates.
- Python API: `docs/API.md` documents the public, stable library interface with one runnable example per area
  (executed by `tests/test_api_docs.py`); `jsonreshape.__all__` exports the main classes and functions
  (`Document`, `export`, `ExportOptions`, `EditSession`, `apply_mapping`, `Settings`, `Progress`, `CancelToken`,
  the error classes, `set_language`, ...) as lazily resolved aliases, so `import jsonreshape` stays as cheap as
  before (~1 ms more; the CLI start time is unchanged).
- CI and release pipeline: GitHub Actions `ci.yml` (tests on Windows with the Rust core and on Linux with the
  pure-Python core, the Windows `.exe` as an artifact) and `release.yml` (a `v*` tag builds the Windows zip and the
  Inno Setup installer, Linux / macOS bundles, abi3 wheels and the sdist, `SHA256SUMS.txt`, release notes from this
  file and the winget manifests, and opens a draft GitHub release; optional PyPI trusted publishing);
  `scripts/build_exe.ps1 -Installer` / `-Sign`, `packaging/installer.iss`, `packaging/winget/`,
  `scripts/release_assets.py`, `scripts/winget_manifest.py`, `tests/test_packaging_pipeline.py`.
- **Parquet export** (`jsonreshape convert in.json out.parquet`, `--to parquet`, the export dialog; optional
  `pyarrow`: `pip install "jsonreshape[parquet]"`): the CSV/XLSX columns and rows with typed columns (`int64`,
  `double` only for exactly representable numbers, `bool`, `string` with the exact text of everything else —
  digits are never lost), types decided from the first 65 536 rows with an automatic two-pass restart when a later
  value does not fit, row groups of ~64 MiB, `--parquet-compression zstd|snappy|gzip|none`, rows built in worker
  processes like CSV (byte-identical to one thread), atomic output, progress and cancellation
  (`jsonreshape/writer_parquet.py`, `ExportOptions.parquet_compression`, `writer.TABLE_FORMATS`).
- **Import of CSV / TSV, YAML and XML** (stage 7): `jsonreshape import INPUT OUTPUT` and File → "Import (CSV, YAML,
  XML)…" convert such files into JSON Lines or a JSON array with exact numbers (`1.10` stays `1.10`), detected
  encodings and delimiters, nested `a.b.c` columns, a safe YAML loader (optional `pyyaml`, extra
  `jsonreshape[import]`), streaming XML with `@attribute` / `#text` conventions and record selection by tag or path;
  the result opens in a new tab (`jsonreshape/importers.py`, `jsonreshape/gui/importdialog.py`).
- **Standard input on the command line** (stage 7): `-` in place of INPUT reads a pipe
  (`curl ... | jsonreshape convert - out.jsonl`, `jsonreshape inspect - < data.json.gz`; also one of the inputs of
  `merge` / `diff`). The data is spooled in 1 MiB pieces to a temporary file in the work folder (progress line,
  cancellation, a free-space check; removed when the command ends, also after an error) and then handled like a named
  file (`--format`, `--record-path`, gzip detection). `-` is refused as an output, twice, when standard input is a
  terminal, for `patch --in-place`, for `split` without `-o DIR` and for `import` without `--from`
  (`jsonreshape/stdin_input.py`, `cli._stdin_inputs`, `tests/test_cli_stdin.py`).
- Screenshots of the viewer in `docs/img/` (main window in the light and dark theme, structure builder, compare
  window, field statistics with the histogram, import window, command palette) and `scripts/make_screenshots.py`,
  which regenerates them offscreen from synthetic data (`bench/make_synthetic.py`).
- Field statistics: a chart area in the statistics dock — a histogram of the numbers (32 nice bins, exact up
  to 65 536 distinct numbers, else a reservoir sample scaled to the total and marked "≈"), the distribution of
  the string lengths, and the top 20 values as bars; hovering shows range, count and share, clicking a bar
  filters the view by the range or the value. `FieldStats.histogram(bins)` / `length_histogram(bins)` and
  `stats.format_histogram` in the Python API; the CLI prints them with `stats --histogram` (`--bins N`; with
  `--json` as `histogram` / `length_histogram` with exact edges per field).
- Russian interface: `--lang ru`, `JSONRESHAPE_LANG=ru` or Tools → Settings → Language → «Русский»; the texts
  are in `jsonreshape/i18n_ru.py` (every key translated; a missing text falls back to English), numbers are
  written `1 234 567,5` as in Uzbek (`i18n.space_grouped()`), `tests/test_i18n_ru.py` checks the coverage and
  the placeholders.

### Changed
- **XLSX export ~15× faster** (7.8–12 → 116 MB/s on a 2.1 GB file, 4 worker processes, peak memory 250–265 MB):
  jsonreshape's own streaming XLSX writer (`jsonreshape/writer_xlsx.py`) replaces `xlsxwriter` — worker processes
  build the worksheet XML (inline strings, no position attributes) and deflate it in pieces, the parent appends
  them to the zip in order and combines the CRC-32s; no staging folder, no slow "finishing" step, files half the
  size. Every cell has the value and type of the xlsxwriter workbook; the workbook is byte-identical whatever the
  number of workers. Archives over 4 GB get zip64 records (xlsxwriter refused them); only a single sheet over 4 GB
  is refused (`writer.error.xlsx_too_large`, raised as soon as it is reached).
- **SQLite export ~7× faster** (10.6–16.4 → 79 MB/s, peak memory 270–282 MB): `jsonreshape/writer_sqlite.py` —
  one optimistic pass instead of two (the workers count the keys per path; data where a path may be a map is
  redone the old way), the records split into tables in worker processes (in-memory databases, serialized) and
  merged by the parent with `INSERT … SELECT`. The database is value for value the one of the one-thread exporter
  (kept as `writer._export_sqlite_onethread`, the reference of the tests and the fallback without
  `sqlite3.Connection.serialize`). `export(..., workers=)` now applies to XLSX and SQLite too.

### Fixed
- XLSX: a text that starts with `<r>` and ends with `</r>` was written by xlsxwriter as raw rich-text XML, producing a
  workbook Excel refused to open; it is now written as the text it is.

## [0.1.0] — 2026-10-20

The first complete version: the six development stages (README §12 "Architecture", `docs/ARCHITECTURE.md`).

### Added
- **Core and CLI** (stage 1): reader with a byte-range index of every record (Rust core, pure-Python fallback with
  identical results), RecordSet, full-text search (Latin ↔ Cyrillic, apostrophe variants), filter language and
  JMESPath, stable external sort with Uzbek collation, streaming writers (JSON, JSON Lines, CSV, XLSX, SQLite),
  CLI `inspect`, `search`, `filter`, `sort`, `convert`.
- **Viewer GUI** (stage 2, PySide6): tree, table, text, raw bytes and Schema views in two linked panes, navigation
  and bookmarks, search and filter panes, sorting, export dialog, background tasks, status bar; files of several GB
  open in well under a second.
- **Editing** (stage 3): node edits (value, key, type, insert, delete, duplicate, move, drag and drop), bulk
  operations with an exact preview, JSON Patch (RFC 6902) export / import, undo / redo, an edit layer on disk with
  continuous autosave and crash recovery, atomic saving with a `.bak` copy, CLI `patch`.
- **Structure builder** (stage 4): mapping engine identical to the `json-tuzilma.html` reference, drag-and-drop
  designer with a live preview, mapping files, streaming apply in worker processes, CLI `apply` and `flatten`.
- **Tools** (stage 5): field statistics (exact or sketched), duplicates, JSON Schema generation and validation
  (draft 2020-12), split / merge / extract, repair of broken JSON with a full report, compare (diff) by key,
  position or alignment, "smart" values (dates, URLs, colours, images); CLI `stats`, `dupes`, `schema`, `validate`,
  `split`, `merge`, `extract`, `repair`, `diff`.
- **Polish** (stage 6): command palette (Ctrl+Shift+P), shortcut settings, session restore, `.jrworkspace` files,
  themes with a dark-mode audit, Windows "Open with", PyInstaller `.exe` (`scripts/build_exe.ps1`), worker
  processes for CSV / XLSX exports and layer checksums, the final benchmark (README §14).
- Documentation in English (`README.md`) and Uzbek (`README.uz.md`); the UI is Uzbek with English fallback
  (`JSONRESHAPE_LANG=en`).

### Known limitations
- XLSX (~12 MB/s) and SQLite (~16 MB/s) exports are below the 50 MB/s target; CSV, JSON and JSON Lines are above it.
- A mapping applied in a single process runs at ~47 MB/s; the default uses worker processes (≥ 97 MB/s).
- Records larger than 50 MB are read-only.

[Unreleased]: https://github.com/Erkinbek/jsonreshape/compare/v0.1.0...HEAD
[0.1.0]: https://github.com/Erkinbek/jsonreshape/releases/tag/v0.1.0
