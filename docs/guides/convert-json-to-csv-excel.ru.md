---
title: Преобразование JSON в CSV, Excel, SQLite, Parquet и Feather
description: Преобразуйте большие файлы JSON или JSON Lines в CSV, Excel (XLSX), SQLite, Parquet или Feather / Arrow IPC — потоком, в масштабе GB, 100–220 MB/s, не загружая файл в память.
---

# Преобразование больших файлов JSON / JSON Lines в CSV, Excel, SQLite, Parquet или Feather

jsonreshape преобразует между JSON, JSON Lines и табличными форматами **потоком**, поэтому размер
файла не ограничен вашей оперативной памятью. Экспорт выполняется примерно со скоростью
**100–220 MB/s**.

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

Вложенные поля адресуются путями с точками. Чтобы превратить вложенный массив в строки,
используйте `--explode` (см. [Развёртывание вложенного JSON](flatten-nested-json.md)).

## → Excel (XLSX)

```bash
jsonreshape convert data.json table.xlsx --array-mode rows --explode orders
```

У jsonreshape есть собственный потоковый модуль записи XLSX, поэтому даже многогигабайтные файлы
экспортируются, не исчерпывая память.

## → SQLite

```bash
jsonreshape convert data.json db.sqlite
```

Записи становятся строками в одной таблице — готовыми к запросам на SQL.

## → Parquet

```bash
pip install "jsonreshape[parquet]"
jsonreshape convert data.json data.parquet
```

Parquet — это столбцовый формат, используемый в инженерии данных (pandas, DuckDB, Spark).

## → Feather / Arrow IPC

```bash
jsonreshape convert data.json data.feather          # or data.arrow: the same file
jsonreshape convert data.json data.arrow --feather-compression lz4
```

Feather (файловый формат Arrow IPC) загружается напрямую в pandas (`pd.read_feather`) и Polars
(`pl.read_ipc`). Столбцы и их типы — те же, что и в экспорте Parquet; сжатие — `zstd` (по
умолчанию), `lz4` или `none`. Нужен тот же `pip install "jsonreshape[parquet]"`.

## В настольном приложении

Откройте файл, при желании сузьте его поиском / фильтром / сортировкой, затем **Файл → Экспорт** и
выберите формат. Вы можете экспортировать весь файл или только текущее выделение.

## Преобразование из CSV, YAML или XML

Обратное направление — это `import`:

```bash
jsonreshape import data.csv data.jsonl
jsonreshape import catalog.xml -o catalog.jsonl --record-tag item
```
