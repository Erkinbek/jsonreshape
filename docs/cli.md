---
title: CLI reference
description: jsonreshape command-line reference — inspect, search, filter, sort, convert, flatten, stats, dupes, schema, validate, split, merge, extract, repair, diff and import for huge JSON files.
---

# CLI reference

Every command streams the file and never loads it fully into memory. Run
`jsonreshape <command> --help` for all options. Use `-` as the path to read from **stdin**:

```bash
curl -s https://example.com/data.json | jsonreshape convert - data.jsonl
jsonreshape inspect - < data.json.gz
```

## Read and explore

```bash
jsonreshape inspect data.json --sample 10000      # record count, fields, types, sizes
jsonreshape search data.json "Toshkent"           # full-text search
jsonreshape search data.json "^Tosh.*t$" --regex  # regex search
jsonreshape search data.json "> 1000" --number    # numeric search
```

## Filter and sort

```bash
jsonreshape filter data.json out.json --where "contact.address.city == 'Toshkent' and len(children) > 2"
jsonreshape filter data.json out.json --jmespath "active && score > to_number('100')"
jsonreshape sort data.json sorted.json --by name --by -id
```

## Convert and export

```bash
jsonreshape convert data.json data.jsonl                 # JSON array <-> JSON Lines
jsonreshape convert data.json table.csv --columns "id,name,contact.address.city"
jsonreshape convert data.json table.xlsx --array-mode rows --explode orders
jsonreshape convert data.json db.sqlite                  # one table
jsonreshape convert data.json data.parquet               # needs jsonreshape[parquet]
jsonreshape convert data.json data.feather               # or .arrow (Arrow IPC); same extra
jsonreshape convert data.json data.arrow --feather-compression lz4
```

See [Convert JSON to CSV, Excel, SQLite, Parquet, Feather](guides/convert-json-to-csv-excel.md).

## Reshape

```bash
jsonreshape flatten data.json flat.jsonl --children-key children
jsonreshape apply structure.mapping.json data.json new.json
jsonreshape extract big.json items.json --path data.items
```

## Analyse

```bash
jsonreshape stats data.json --field score --histogram --bins 20
jsonreshape dupes data.json --key id --top 10 -o dupes.jsonl
jsonreshape schema data.json data.schema.json --strict
jsonreshape validate data.json --schema data.schema.json --export errors.csv
```

## Split, merge, repair, compare

```bash
jsonreshape split data.json --parts 10
jsonreshape merge "part_*.json" -o all.jsonl --dedupe-by id
jsonreshape repair broken.json repaired.json --report fixes.csv
jsonreshape diff old.json new.json --key id --exit-code
```

## Import other formats

```bash
jsonreshape import data.csv data.jsonl
jsonreshape import export.csv -o export.jsonl --delimiter ";" --encoding cp1251 --skip-bad
jsonreshape import catalog.xml -o catalog.jsonl --record-tag item
jsonreshape import config.yaml config.json      # needs jsonreshape[import]
```

The package is also a Python library — see the [Python API](API.md).
