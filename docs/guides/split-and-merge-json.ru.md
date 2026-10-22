---
title: Разделение и объединение файлов JSON
description: Разделите большой файл JSON или JSON Lines на части или объедините много файлов JSON в один с удалением дубликатов — в масштабе GB, из командной строки.
---

# Разделение большого файла JSON и объединение многих файлов

## Разделение

Разбейте большой файл на несколько меньших:

```bash
jsonreshape split data.json --parts 10
```

Это создаёт десять файлов с примерно равным числом записей. Выполните `jsonreshape split --help`
для разделения по размеру или по фиксированному числу записей.

## Объединение

Объедините много файлов в один, при желании удаляя дубликаты:

```bash
jsonreshape merge "part_*.json" -o all.jsonl --dedupe-by id
```

- Шаблон `"part_*.json"` соответствует каждой части — заключите его в кавычки, чтобы оболочка не
  раскрыла его первой.
- `--dedupe-by id` оставляет одну запись на каждый `id`.

## Извлечение части

Чтобы извлечь одну ветвь из большого документа:

```bash
jsonreshape extract big.json items.json --path data.items
jsonreshape extract data.json orders.jsonl --gather "orders[*]"
```

## См. также

- [Преобразование JSON в CSV, Excel, SQLite, Parquet](convert-json-to-csv-excel.md)
- [Сравнение двух файлов JSON](compare-json-files.md)
