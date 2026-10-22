---
title: Buzuq JSON'ni ta'mirlash
description: Yaroqsiz yoki buzuq JSON'ni avtomatik tuzating — oxirgi vergullar, yetishmayotgan qavslar, tirnoqsiz kalitlar, yakka tirnoqlar, kesilgan fayllar — buyruqlar qatoridan yoki desktop dasturdan.
---

# Buzuq / yaroqsiz JSON'ni ta'mirlash

`repair` JSON faylni yaroqsiz qiladigan keng tarqalgan xatolarni tuzatadi, shunda u o'qiladigan
va ochiladigan bo'ladi: oxirgi vergullar, yetishmayotgan yoki ortiqcha qavslar, tirnoqsiz kalitlar,
yakka tirnoqlar, izohlar va kesilgan oxirlar.

```bash
jsonreshape repair broken.json repaired.json --report fixes.csv
```

- `repaired.json` — tuzatilgan fayl; asl fayl o'zgarmaydi.
- `--report fixes.csv` qilingan har bir o'zgarish ro'yxatini yozadi, shunday qilib hech narsa sassiz tuzatilmaydi.

## Desktop dasturda

Fayl o'qilmaganda, xato paneli **Ta'mirlash…** tugmasini taklif qiladi. U tuzatilgan faylni
yozishdan oldin nima o'zgarishini ko'rsatadi.

## Maslahatlar

- Faylning endi yaroqli ekanligiga ishonch hosil qilish uchun keyin [`inspect`](../cli.md) yoki
  [`validate`](../cli.md) ni ishga tushiring.
- Yaroqli, lekin noto'g'ri shaklga ega fayllar uchun
  [Ichma-ich JSON'ni tekislash](flatten-nested-json.md) va
  [JSON'ni konvertatsiya qilish](convert-json-to-csv-excel.md) ga qarang.
