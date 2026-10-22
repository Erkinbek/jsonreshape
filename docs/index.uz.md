---
title: Katta JSON fayllarni oching va qayta quring
description: jsonreshape 100 MB dan bir necha GB gacha bo'lgan JSON va JSON Lines fayllarni xotiraga yuklamasdan ochadi, qidiradi, tahrirlaydi va konvertatsiya qiladi. Windows, Linux va macOS uchun bepul, oflayn va ochiq kodli.
---

# Katta JSON fayllarni oching va qayta quring

**jsonreshape** — oddiy muharrirda ochib bo'lmaydigan darajada katta JSON va
JSON Lines (NDJSON) fayllar — 100 MB dan bir necha gigabaytgacha — uchun bepul, ochiq
kodli **desktop dastur va buyruqlar qatori vositasi**.

[O'rnatish](install.md){ .md-button .md-button--primary }
[GitHub'da ko'rish](https://github.com/Erkinbek/jsonreshape){ .md-button }

![Katta JSON massiv daraxtini ko'rsatayotgan jsonreshape ko'ruvchisi](img/main-light.png){ loading=lazy }

## Nimasi bilan farqlanadi

- **Hech narsa butunlay xotiraga yuklanmaydi.** Ko'p gigabaytli fayl bir marta indekslanadi
  (har bir yozuvning bayt chegaralari), keyin faqat siz ko'rayotgan yozuvlar o'qiladi. 2 GB fayl
  taxminan **3 soniyada** tayyor bo'ladi; 10 million yozuv 400 MB dan kam RAM bilan **77 soniyada** tartiblanadi.
- **Asl fayl hech qachon o'zgartirilmaydi.** Tahrirlar alohida qatlamda turadi; saqlash faylni
  atomik almashtiradi va standart bo'yicha `.bak` zaxira nusxasini saqlab qoladi.
- **Oflayn va maxfiy.** Telemetriya ham, tarmoqqa kirish ham yo'q — ma'lumotlaringiz hech qachon qurilmangizdan chiqmaydi.
- **Tez yadro, qulay asboblar.** Sof Python fallback bilan Rust yadro, PySide6 desktop
  oynasi va skript yoziladigan CLI.

## Nimalar qila olasiz

| Vazifa | Qo'llanma |
|---|---|
| 2–5 GB JSON / JSON Lines faylni ochish va ko'rish | [Katta JSON faylni ochish](guides/open-large-json-file.md) |
| CSV, Excel, SQLite, Parquet yoki Feather ga konvertatsiya | [JSON'ni konvertatsiya qilish](guides/convert-json-to-csv-excel.md) |
| Ikki JSON faylni taqqoslash | [JSON fayllarni taqqoslash](guides/compare-json-files.md) |
| Yaroqsiz / buzuq JSON faylni ta'mirlash | [Buzuq JSON'ni ta'mirlash](guides/repair-broken-json.md) |
| Ichma-ich JSON'ni jadvalga tekislash | [Ichma-ich JSON'ni tekislash](guides/flatten-nested-json.md) |
| Bitta katta faylni bo'lish / ko'p faylni birlashtirish | [JSON'ni bo'lish va birlashtirish](guides/split-and-merge-json.md) |

Shuningdek: to'liq matnli va regex **qidiruv**, **filtr** tili, ko'p kalitli **tartiblash**,
gistogrammali maydon **statistikasi**, **dublikatlar**ni aniqlash, **JSON Schema** yaratish va
tekshirish, hamda sudrab tashlanadigan **tuzilma quruvchi**. [CLI ma'lumotnomasi](cli.md) va
[Python API](API.md) ga qarang.

## Bir qatorda o'rnatish

Birinchi nashrdan boshlab:

```bash
pip install jsonreshape          # CLI + Python library
pip install "jsonreshape[gui]"   # add the desktop window
```

Tayyor Windows yuklamalari, manbadan qurish va ixtiyoriy Parquet / import qo'shimchalari uchun
[O'rnatish](install.md) bo'limiga qarang.
