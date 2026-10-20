# Changelog

All notable changes to jsonreshape are recorded here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/); versions follow [Semantic Versioning](https://semver.org/).

## [Unreleased]

### Added
- Apache License 2.0 (`LICENSE`, `NOTICE`, `THIRD-PARTY-NOTICES.md`), `CONTRIBUTING.md`, `SECURITY.md`, issue and
  pull request templates.

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
