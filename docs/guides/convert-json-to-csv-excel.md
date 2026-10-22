---
title: Convert JSON to CSV, Excel, SQLite, Parquet and Feather
description: Convert large JSON or JSON Lines files to CSV, Excel (XLSX), SQLite, Parquet or Feather / Arrow IPC — streaming, GB-scale, 100–220 MB/s, without loading the file into memory.
---

# Convert large JSON / JSON Lines to CSV, Excel, SQLite, Parquet or Feather

jsonreshape converts between JSON, JSON Lines and tabular formats by **streaming**, so the
file size is not limited by your RAM. Exports run at roughly **100–220 MB/s**.

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

## → Feather / Arrow IPC

```bash
jsonreshape convert data.json data.feather          # or data.arrow: the same file
jsonreshape convert data.json data.arrow --feather-compression lz4
```

Feather (the Arrow IPC file format) loads straight into pandas (`pd.read_feather`) and Polars
(`pl.read_ipc`). It has the same columns and column types as the Parquet export; compression is
`zstd` (default), `lz4` or `none`. It needs the same `pip install "jsonreshape[parquet]"`.

## In the desktop app

Open the file, optionally search / filter / sort to narrow it, then **File → Export** and
choose the format. You can export the whole file or just the current selection.

## Convert from CSV, YAML or XML

The reverse direction is `import`:

```bash
jsonreshape import data.csv data.jsonl
jsonreshape import catalog.xml -o catalog.jsonl --record-tag item
```
