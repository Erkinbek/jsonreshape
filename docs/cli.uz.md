---
title: CLI ma'lumotnomasi
description: jsonreshape buyruqlar qatori ma'lumotnomasi — katta JSON fayllar uchun inspect, search, filter, sort, convert, flatten, stats, dupes, schema, validate, split, merge, extract, repair, diff va import.
---

# CLI ma'lumotnomasi

Har bir buyruq faylni oqim bilan o'qiydi va uni hech qachon butunlay xotiraga yuklamaydi.
Barcha parametrlar uchun `jsonreshape <command> --help` ni ishga tushiring. **Standart kirish**dan
(stdin) o'qish uchun yo'l sifatida `-` dan foydalaning:

```bash
curl -s https://example.com/data.json | jsonreshape convert - data.jsonl
jsonreshape inspect - < data.json.gz
```

## O'qish va ko'rish

```bash
jsonreshape inspect data.json --sample 10000      # record count, fields, types, sizes
jsonreshape search data.json "Toshkent"           # full-text search
jsonreshape search data.json "^Tosh.*t$" --regex  # regex search
jsonreshape search data.json "> 1000" --number    # numeric search
```

## Filtrlash va tartiblash

```bash
jsonreshape filter data.json out.json --where "contact.address.city == 'Toshkent' and len(children) > 2"
jsonreshape filter data.json out.json --jmespath "active && score > to_number('100')"
jsonreshape sort data.json sorted.json --by name --by -id
```

## Konvertatsiya va eksport

```bash
jsonreshape convert data.json data.jsonl                 # JSON array <-> JSON Lines
jsonreshape convert data.json table.csv --columns "id,name,contact.address.city"
jsonreshape convert data.json table.xlsx --array-mode rows --explode orders
jsonreshape convert data.json db.sqlite                  # one table
jsonreshape convert data.json data.parquet               # needs jsonreshape[parquet]
```

[JSON'ni CSV, Excel, SQLite, Parquet ga konvertatsiya qilish](guides/convert-json-to-csv-excel.md) ga qarang.

## Qayta qurish

```bash
jsonreshape flatten data.json flat.jsonl --children-key children
jsonreshape apply structure.mapping.json data.json new.json
jsonreshape extract big.json items.json --path data.items
```

## Tahlil qilish

```bash
jsonreshape stats data.json --field score --histogram --bins 20
jsonreshape dupes data.json --key id --top 10 -o dupes.jsonl
jsonreshape schema data.json data.schema.json --strict
jsonreshape validate data.json --schema data.schema.json --export errors.csv
```

## Bo'lish, birlashtirish, ta'mirlash, taqqoslash

```bash
jsonreshape split data.json --parts 10
jsonreshape merge "part_*.json" -o all.jsonl --dedupe-by id
jsonreshape repair broken.json repaired.json --report fixes.csv
jsonreshape diff old.json new.json --key id --exit-code
```

## Boshqa formatlarni import qilish

```bash
jsonreshape import data.csv data.jsonl
jsonreshape import export.csv -o export.jsonl --delimiter ";" --encoding cp1251 --skip-bad
jsonreshape import catalog.xml -o catalog.jsonl --record-tag item
jsonreshape import config.yaml config.json      # needs jsonreshape[import]
```

Paket shuningdek Python kutubxonasidir — [Python API](API.md) ga qarang.
