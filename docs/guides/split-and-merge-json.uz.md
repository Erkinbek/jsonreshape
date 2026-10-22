---
title: JSON fayllarni bo'lish va birlashtirish
description: Katta JSON yoki JSON Lines faylni qismlarga bo'ling yoki ko'p JSON faylni takrorlarni olib tashlab bittaga birlashtiring — GB miqyosida, buyruqlar qatoridan.
---

# Katta JSON faylni bo'lish va ko'p faylni birlashtirish

## Bo'lish

Katta faylni bir nechta kichikroq fayllarga bo'ling:

```bash
jsonreshape split data.json --parts 10
```

Bu taxminan teng sondagi yozuvlar bilan o'nta fayl yozadi. Buning o'rniga hajm bo'yicha yoki
belgilangan yozuvlar soni bo'yicha bo'lish uchun `jsonreshape split --help` ni ishga tushiring.

## Birlashtirish

Ko'p faylni bittaga birlashtiring, kerak bo'lsa takrorlarni olib tashlang:

```bash
jsonreshape merge "part_*.json" -o all.jsonl --dedupe-by id
```

- `"part_*.json"` glob andozasi har bir qismga mos keladi — uni qobiq avval ochib yubormasligi uchun tirnoq ichiga oling.
- `--dedupe-by id` har bir `id` uchun bitta yozuvni saqlab qoladi.

## Qism ajratib olish

Katta hujjatdan bitta shoxni ajratib olish uchun:

```bash
jsonreshape extract big.json items.json --path data.items
jsonreshape extract data.json orders.jsonl --gather "orders[*]"
```

## Aloqador

- [JSON'ni CSV, Excel, SQLite, Parquet ga konvertatsiya qilish](convert-json-to-csv-excel.md)
- [Ikki JSON faylni taqqoslash](compare-json-files.md)
