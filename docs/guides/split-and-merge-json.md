---
title: Split and merge JSON files
description: Split a large JSON or JSON Lines file into parts, or merge many JSON files into one with de-duplication — at GB scale, from the command line.
---

# Split a large JSON file and merge many files

## Split

Break a big file into several smaller ones:

```bash
jsonreshape split data.json --parts 10
```

This writes ten files with roughly equal numbers of records. Run `jsonreshape split --help`
for splitting by size or by a fixed record count instead.

## Merge

Combine many files into one, optionally removing duplicates:

```bash
jsonreshape merge "part_*.json" -o all.jsonl --dedupe-by id
```

- The glob `"part_*.json"` matches every part — quote it so the shell does not expand it first.
- `--dedupe-by id` keeps a single record per `id`.

## Extract a sub-part

To pull one branch out of a large document:

```bash
jsonreshape extract big.json items.json --path data.items
jsonreshape extract data.json orders.jsonl --gather "orders[*]"
```

## Related

- [Convert JSON to CSV, Excel, SQLite, Parquet](convert-json-to-csv-excel.md)
- [Compare two JSON files](compare-json-files.md)
