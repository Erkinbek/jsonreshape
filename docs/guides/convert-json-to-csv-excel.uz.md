---
title: JSON'ni CSV, Excel, SQLite va Parquet ga konvertatsiya qilish
description: Katta JSON yoki JSON Lines fayllarni CSV, Excel (XLSX), SQLite yoki Parquet ga konvertatsiya qiling — oqim bilan, GB miqyosida, 100–250 MB/s, faylni xotiraga yuklamasdan.
---

# Katta JSON / JSON Lines'ni CSV, Excel, SQLite yoki Parquet ga konvertatsiya qilish

jsonreshape JSON, JSON Lines va jadval formatlari orasida **oqim bilan** konvertatsiya qiladi,
shuning uchun fayl hajmi RAM'ingiz bilan cheklanmaydi. Eksportlar taxminan **100–250 MB/s** tezlikda ishlaydi.

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

Ichma-ich maydonlar nuqtali yo'llar bilan ko'rsatiladi. Ichma-ich massivni qatorlarga aylantirish
uchun `--explode` dan foydalaning ([Ichma-ich JSON'ni tekislash](flatten-nested-json.md) ga qarang).

## → Excel (XLSX)

```bash
jsonreshape convert data.json table.xlsx --array-mode rows --explode orders
```

jsonreshape o'zining oqimli XLSX yozuvchisiga ega, shuning uchun hatto ko'p gigabaytli fayllar ham
xotira tugamasdan eksport qilinadi.

## → SQLite

```bash
jsonreshape convert data.json db.sqlite
```

Yozuvlar bitta jadvaldagi qatorlarga aylanadi — SQL bilan so'rov yuborishga tayyor.

## → Parquet

```bash
pip install "jsonreshape[parquet]"
jsonreshape convert data.json data.parquet
```

Parquet — ma'lumotlar muhandisligida (pandas, DuckDB, Spark) keng qo'llaniladigan ustunli format.

## Desktop dasturda

Faylni oching, kerak bo'lsa uni toraytirish uchun qidiring / filtrlang / tartiblang, so'ng
**Fayl → Eksport** ni tanlab formatni belgilang. Butun faylni yoki faqat joriy tanlovni eksport qilishingiz mumkin.

## CSV, YAML yoki XML'dan konvertatsiya qilish

Teskari yo'nalish — `import`:

```bash
jsonreshape import data.csv data.jsonl
jsonreshape import catalog.xml -o catalog.jsonl --record-tag item
```
