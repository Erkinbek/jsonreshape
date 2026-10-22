---
title: Convert JSON to CSV, Excel, SQLite and Parquet
description: Convert large JSON or JSON Lines files to CSV, Excel (XLSX), SQLite or Parquet — streaming, GB-scale, 100–250 MB/s, without loading the file into memory.
---

# Convert large JSON / JSON Lines to CSV, Excel, SQLite or Parquet

jsonreshape converts between JSON, JSON Lines and tabular formats by **streaming**, so the
file size is not limited by your RAM. Exports run at roughly **100–250 MB/s**.

## JSON ↔ JSON Lines

```bash
jsonreshape convert data.json data.jsonl     # array -> one record per line
jsonreshape convert data.jsonl data.json     # back to an array
```

## JSON / JSON Lines → CSV

```bash
jsonreshape convert data.jsonl table.csv
jsonreshape convert data.json table.csv --columns "id,name,contact.address.city"
```

Nested fields are addressed with dotted paths. To turn a nested array into rows, use
`--explode` (see [Flatten nested JSON](flatten-nested-json.md)).

## → Excel (XLSX)

```bash
jsonreshape convert data.json table.xlsx --array-mode rows --explode orders
```

jsonreshape has its own streaming XLSX writer, so even multi-gigabyte files export without
running out of memory.

## → SQLite

```bash
jsonreshape convert data.json db.sqlite
```

The records become rows in one table — ready to query with SQL.

## → Parquet

```bash
pip install "jsonreshape[parquet]"
jsonreshape convert data.json data.parquet
```

Parquet is the columnar format used across data engineering (pandas, DuckDB, Spark).

## In the desktop app

Open the file, optionally search / filter / sort to narrow it, then **File → Export** and
choose the format. You can export the whole file or just the current selection.

## Convert from CSV, YAML or XML

The reverse direction is `import`:

```bash
jsonreshape import data.csv data.jsonl
jsonreshape import catalog.xml -o catalog.jsonl --record-tag item
```
