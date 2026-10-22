---
title: Ichma-ich JSON'ni tekislash
description: Chuqur ichma-ich JSON'ni tekis jadval yoki JSON Lines'ga tekislang — ichma-ich massivlarni qatorlarga, ichma-ich obyektlarni nuqtali ustunlarga yoying — GB miqyosida, CLI'dan yoki desktop tuzilma quruvchidan.
---

# Ichma-ich JSON'ni jadvalga tekislash

Ichma-ich obyektlar va massivlar CSV yoki ma'lumotlar bazasi jadvaliga toza sig'maydi. `flatten`
ichma-ich tuzilmani tekis yozuvlarga aylantiradi: ichma-ich obyektlar **nuqtali ustunlar**ga
(`contact.address.city`) aylanadi, ichma-ich massivni esa har bir element o'z qatoriga aylanadigan
qilib yoyish mumkin.

```bash
jsonreshape flatten data.json flat.jsonl --children-key children
```

So'ng tekis natijani istalgan jadval formatiga konvertatsiya qiling:

```bash
jsonreshape convert flat.jsonl flat.csv
```

## Ichma-ich massivni qatorlarga yoyish

To'g'ridan-to'g'ri jadvalga konvertatsiya qilayotganda bitta massivni yoyishingiz (explode) mumkin:

```bash
jsonreshape convert data.json table.xlsx --array-mode rows --explode orders
```

## Tuzilma quruvchi (desktop)

To'liq nazorat uchun **Tuzilma quruvchi** manba maydonlarini chiqish shakliga sudrab tashlash va
jonli ko'rinishni ko'rish, so'ng mapping'ni saqlab butun faylga qo'llash imkonini beradi:

```bash
jsonreshape apply structure.mapping.json data.json new.json
```

![Tuzilma quruvchi](../img/structure-builder.png){ loading=lazy }

## Aloqador

- [JSON'ni CSV, Excel, SQLite, Parquet ga konvertatsiya qilish](convert-json-to-csv-excel.md)
- [Katta JSON faylni ochish](open-large-json-file.md)
