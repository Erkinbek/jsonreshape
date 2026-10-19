# jsonreshape: tahrirlar benchmarki (AGENT_PROMPT §12, 3-bosqich)

Sana: 2026-10-19 12:50 · fayl: `syn_2g.json` (2.1 GB, 6 513 235 ta yozuv) · to'liq natijalar: `edit-20261019-125031-after.json`, `edit-20261019-135520-final.json`, `edit-20261019-143619-final-patch.json`, `edit-20261019-145502-final2.json` · oldingi o'lchov: `edit-20261019-112336-before.json`

## §12 maqsadi: tahrirlar bilan eksport va saqlash

| Amal | Maqsad | Oldin | Keyin | Holat |
|---|---|---|---|:---:|
| Eksport JSON (indent 2), 100 ta nuqtaviy tahrir | ≥ 50 MB/s, RAM < 300 MB | 106 MB/s, RSS 69.7 MB, private 58.2 MB (20.2 s; fsync 3.52 s, fsync'siz 129 MB/s) | 89.4 MB/s, RSS 72.7 MB, private 62.2 MB (24.0 s; fsync 2.18 s, fsync'siz 98.4 MB/s) | ✅ |
| Eksport JSON Lines, 100 ta nuqtaviy tahrir | ≥ 50 MB/s, RAM < 300 MB | 128 MB/s, RSS 84.9 MB, private 76.7 MB (16.7 s; fsync 1.91 s, fsync'siz 145 MB/s) | 111 MB/s, RSS 87.6 MB, private 80.4 MB (19.3 s; fsync 2.34 s, fsync'siz 127 MB/s) | ✅ |
| Eksport CSV (9 ta skalyar ustun), 100 ta nuqtaviy tahrir | ≥ 50 MB/s, RAM < 300 MB | 37.4 MB/s, RSS 104 MB, private 95.1 MB (57.4 s; fsync 357 ms, fsync'siz 37.6 MB/s) | 30.2 MB/s, RSS 107 MB, private 98.4 MB (71.0 s; fsync 247 ms, fsync'siz 30.3 MB/s) | ❌ |
| Boshqacha saqlash (Save As), 100 ta nuqtaviy tahrir | ≥ 50 MB/s, RAM < 300 MB | 282 MB/s, RSS 53.4 MB, private 42.3 MB (7.62 s; fsync 3.76 s, fsync'siz 556 MB/s) | 320 MB/s, RSS 56.6 MB, private 45.3 MB (6.71 s; fsync 2.15 s, fsync'siz 472 MB/s) | ✅ |
| Eksport JSON (indent 2), har 3-yozuv o'zgargan (bulk) | ≥ 50 MB/s, RAM < 300 MB | 55.7 MB/s, RSS 179 MB, private 117 MB (39.0 s; fsync 1.85 s, fsync'siz 58.5 MB/s) | 68.1 MB/s, RSS 120 MB, private 104 MB (31.8 s; fsync 1.93 s, fsync'siz 72.5 MB/s) | ✅ |
| Eksport JSON Lines, har 3-yozuv o'zgargan (bulk) | ≥ 50 MB/s, RAM < 300 MB | 66.9 MB/s, RSS 162 MB, private 102 MB (32.4 s; fsync 2.30 s, fsync'siz 72.0 MB/s) | 73.9 MB/s, RSS 109 MB, private 95.9 MB (29.3 s; fsync 2.75 s, fsync'siz 81.6 MB/s) | ✅ |
| Eksport CSV (9 ta skalyar ustun), har 3-yozuv o'zgargan (bulk) | ≥ 50 MB/s, RAM < 300 MB | 28.0 MB/s, RSS 185 MB, private 123 MB (77.4 s) | 28.2 MB/s, RSS 134 MB, private 118 MB (77.0 s; fsync 923 ms, fsync'siz 28.5 MB/s) | ❌ |
| Boshqacha saqlash (Save As), har 3-yozuv o'zgargan (bulk) | ≥ 50 MB/s, RAM < 300 MB | 85.6 MB/s, RSS 153 MB, private 90.1 MB (25.3 s; fsync 3.73 s, fsync'siz 100 MB/s) | 86.0 MB/s, RSS 110 MB, private 95.6 MB (25.2 s; fsync 1.15 s, fsync'siz 90.1 MB/s) | ✅ |
| Eksport JSON (indent 2), hamma yozuv o'zgargan (bulk) | ≥ 50 MB/s, RAM < 300 MB | 73.8 MB/s, RSS 267 MB, private 101 MB (30.3 s; fsync 1.27 s, fsync'siz 77.0 MB/s) | 93.8 MB/s, RSS 119 MB, private 103 MB (23.8 s; fsync 1.89 s, fsync'siz 102 MB/s) | ✅ |
| Eksport JSON Lines, hamma yozuv o'zgargan (bulk) | ≥ 50 MB/s, RAM < 300 MB | 87.5 MB/s, RSS 255 MB, private 90.3 MB (25.5 s; fsync 2.72 s, fsync'siz 97.9 MB/s) | 121 MB/s, RSS 110 MB, private 93.4 MB (18.5 s; fsync 1.34 s, fsync'siz 130 MB/s) | ✅ |
| Eksport CSV (9 ta skalyar ustun), hamma yozuv o'zgargan (bulk) | ≥ 50 MB/s, RAM < 300 MB | 31.3 MB/s, RSS 279 MB, private 113 MB (71.3 s; fsync 286 ms, fsync'siz 31.4 MB/s) | 40.6 MB/s, RSS 132 MB, private 115 MB (55.0 s; fsync 489 ms, fsync'siz 40.9 MB/s) | ❌ |
| Boshqacha saqlash (Save As), hamma yozuv o'zgargan (bulk) | ≥ 50 MB/s, RAM < 300 MB | 119 MB/s, RSS 211 MB, private 44.6 MB (18.7 s; fsync 3.16 s, fsync'siz 143 MB/s) | 198 MB/s, RSS 64.0 MB, private 47.0 MB (11.3 s; fsync 1.95 s, fsync'siz 239 MB/s) | ✅ |
| Eksport JSON (indent 2), 10% yozuv o'chirilgan + 1 000 ta qo'shilgan | ≥ 50 MB/s, RAM < 300 MB | 78.9 MB/s, RSS 134 MB, private 123 MB (24.5 s; fsync 1.66 s, fsync'siz 84.6 MB/s) | 67.9 MB/s, RSS 133 MB, private 125 MB (28.4 s; fsync 1.51 s, fsync'siz 71.8 MB/s) | ✅ |
| Eksport JSON Lines, 10% yozuv o'chirilgan + 1 000 ta qo'shilgan | ≥ 50 MB/s, RAM < 300 MB | 88.3 MB/s, RSS 121 MB, private 114 MB (21.9 s; fsync 1.38 s, fsync'siz 94.2 MB/s) | 84.1 MB/s, RSS 125 MB, private 115 MB (23.0 s; fsync 2.54 s, fsync'siz 94.5 MB/s) | ✅ |
| Eksport CSV (9 ta skalyar ustun), 10% yozuv o'chirilgan + 1 000 ta qo'shilgan | ≥ 50 MB/s, RAM < 300 MB | 28.6 MB/s, RSS 145 MB, private 136 MB (67.5 s; fsync 254 ms, fsync'siz 28.7 MB/s) | 28.3 MB/s, RSS 147 MB, private 138 MB (68.2 s) | ❌ |
| Boshqacha saqlash (Save As), 10% yozuv o'chirilgan + 1 000 ta qo'shilgan | ≥ 50 MB/s, RAM < 300 MB | 147 MB/s, RSS 90.2 MB, private 80.7 MB (13.2 s; fsync 1.46 s, fsync'siz 165 MB/s) | 157 MB/s, RSS 108 MB, private 102 MB (12.3 s; fsync 1.81 s, fsync'siz 184 MB/s) | ✅ |
| Dasturning umumiy xotirasi (barcha amallarning eng katta peak RSS) | < 500 MB | 2 642 MB RSS (eng kattasi: patch_import all (op)), private 2 534 MB | 406 MB RSS (eng kattasi: patch_import all (op)), private 291 MB | ✅ |

✅ maqsadga yetildi · ❌ yetilmadi (sababi va variantlari pastda) · — maqsad belgilanmagan. Vaqt — eng yaxshi takror, xotira — barcha takrorlarning eng kattasi (RSS = ishchi to'plam, private = shaxsiy baytlar; ishchi jarayonlar bilan birga); MB = 10⁶ bayt; tezlik = tahrirlangan hujjat yozuvlarining baytlari / vaqt. Holat «Keyin» ustuniga qarab.

## Tahrirlash amallari (qo'shimcha o'lchovlar)

| Amal | Maqsad | Oldin | Keyin | Holat |
|---|---|---|---|:---:|
| Eksport JSON (indent 2), tahrirsiz fayl (nazorat) | ≥ 50 MB/s, RAM < 300 MB | 119 MB/s, RSS 67.3 MB, private 57.0 MB (18.0 s; fsync 2.75 s, fsync'siz 140 MB/s) | 99.9 MB/s, RSS 71.3 MB, private 61.1 MB (21.5 s; fsync 3.11 s, fsync'siz 117 MB/s) | ✅ |
| Eksport JSON Lines, tahrirsiz fayl (nazorat) | ≥ 50 MB/s, RAM < 300 MB | 137 MB/s, RSS 80.0 MB, private 74.6 MB (15.7 s; fsync 541 ms, fsync'siz 142 MB/s) | 115 MB/s, RSS 86.1 MB, private 78.9 MB (18.7 s; fsync 1.33 s, fsync'siz 124 MB/s) | ✅ |
| Eksport CSV (9 ta skalyar ustun), tahrirsiz fayl (nazorat) | ≥ 50 MB/s, RAM < 300 MB | 34.7 MB/s, RSS 101 MB, private 93.3 MB (61.9 s; fsync 341 ms, fsync'siz 34.9 MB/s) | 31.7 MB/s, RSS 103 MB, private 94.1 MB (67.7 s; fsync 254 ms, fsync'siz 31.8 MB/s) | ❌ |
| Boshqacha saqlash (Save As), tahrirsiz fayl (nazorat) | ≥ 50 MB/s, RAM < 300 MB | 252 MB/s, RSS 50.4 MB, private 40.4 MB (8.52 s; fsync 4.58 s, fsync'siz 546 MB/s) | 351 MB/s, RSS 55.6 MB, private 46.5 MB (6.12 s; fsync 2.30 s, fsync'siz 562 MB/s) | ✅ |
| Bulk ko'rib chiqish set_field: hamma yozuv o'zgargan (bulk) | ≥ 50 MB/s (hamma yozuv o'zgarganda), dastur < 500 MB | 17.9 MB/s, RSS 291 MB, private 281 MB (120 s; fsync 1.03 s, fsync'siz 18.0 MB/s) | 111 MB/s, RSS 300 MB, private 274 MB (19.3 s; fsync 3.05 s, fsync'siz 132 MB/s; 4 ta ishchi jarayon) | ✅ |
| Bulk ko'rib chiqish set_field: har 3-yozuv o'zgargan (bulk) | ≥ 50 MB/s, dastur < 500 MB | 50.3 MB/s, RSS 244 MB, private 129 MB (42.7 s; fsync 685 ms, fsync'siz 51.2 MB/s) | 179 MB/s, RSS 371 MB, private 224 MB (12.0 s; fsync 3.39 s, fsync'siz 249 MB/s; 4 ta ishchi jarayon) | ✅ |
| Bulk ko'rib chiqish «set_field», hamma yozuvlar (har yozuvga maydon) | ≥ 50 MB/s (hamma yozuv o'zgarganda), dastur < 500 MB | 19.5 MB/s, RSS 325 MB, private 319 MB (110 s; fsync 300 ms, fsync'siz 19.6 MB/s) | 106 MB/s, RSS 303 MB, private 256 MB (20.2 s; fsync 2.61 s, fsync'siz 122 MB/s; 4 ta ishchi jarayon) | ✅ |
| Bulk ko'rib chiqish «replace», hamma yozuvlar («+998» → «998», har yozuvda) | ≥ 50 MB/s (hamma yozuv o'zgarganda), dastur < 500 MB | 10.3 MB/s, RSS 328 MB, private 322 MB (209 s; fsync 1.07 s, fsync'siz 10.3 MB/s) — bir marta o'lchandi (juda sekin) | 77.4 MB/s, RSS 294 MB, private 249 MB (27.7 s; fsync 2.77 s, fsync'siz 86.0 MB/s; 4 ta ishchi jarayon) | ✅ |
| Bulk ko'rib chiqish «rename_key», hamma yozuvlar (active → is_active) | ≥ 50 MB/s (hamma yozuv o'zgarganda), dastur < 500 MB | 12.7 MB/s, RSS 328 MB, private 320 MB (169 s; fsync 1.71 s, fsync'siz 12.8 MB/s) — bir marta o'lchandi (juda sekin) | 82.0 MB/s, RSS 299 MB, private 250 MB (26.2 s; fsync 3.99 s, fsync'siz 96.8 MB/s; 4 ta ishchi jarayon) | ✅ |
| Bulk ko'rib chiqish «delete_field», hamma yozuvlar (tags o'chiriladi) | ≥ 50 MB/s (hamma yozuv o'zgarganda), dastur < 500 MB | 18.4 MB/s, RSS 326 MB, private 320 MB (117 s; fsync 393 ms, fsync'siz 18.4 MB/s) | 98.2 MB/s, RSS 290 MB, private 242 MB (21.9 s; fsync 3.64 s, fsync'siz 118 MB/s; 4 ta ishchi jarayon) | ✅ |
| Bulk ko'rib chiqish «clean_strings», hamma yozuvlar (trim + bo'sh joylar, hech narsa o'zgarmaydi) | dastur < 500 MB | 22.2 MB/s, RSS 65.7 MB, private 54.2 MB (96.5 s) | 178 MB/s, RSS 226 MB, private 176 MB (12.1 s; 4 ta ishchi jarayon) | ✅ |
| Bulk ko'rib chiqish «convert_type», hamma yozuvlar (score → satr) | ≥ 50 MB/s (hamma yozuv o'zgarganda), dastur < 500 MB | 14.8 MB/s, RSS 334 MB, private 325 MB (145 s; fsync 1.98 s, fsync'siz 15.0 MB/s) | 88.6 MB/s, RSS 298 MB, private 248 MB (24.2 s; fsync 4.05 s, fsync'siz 106 MB/s; 4 ta ishchi jarayon) | ✅ |
| Bulk ko'rib chiqish «dedupe», hamma yozuvlar (id bo'yicha, takror yo'q) | dastur < 500 MB | 80.5 MB/s, RSS 233 MB, private 225 MB (26.7 s) | 70.4 MB/s, RSS 235 MB, private 228 MB (30.5 s) | ✅ |
| JSON Patch eksporti, 100 ta nuqtaviy tahrir | dastur < 500 MB | 26.0 ms, RSS 41.1 MB, private 29.6 MB; 100 ta amal | 46.0 ms, RSS 52.1 MB, private 39.3 MB; 100 ta amal | ✅ |
| JSON Patch eksporti, har 3-yozuv o'zgargan (bulk) | dastur < 500 MB | 14.7 MB/s, RSS 212 MB, private 44.8 MB (147 s; 2 171 079 ta amal) | 99.2 MB/s, RSS 401 MB, private 200 MB (21.9 s; 2 171 079 ta amal; 4 ta ishchi jarayon) | ✅ |
| JSON Patch importi (tahrirsiz faylga, yangi sessiya), har 3-yozuv o'zgargan (bulk) | dastur < 500 MB | 197 s, RSS 1 042 MB, private 930 MB — bir marta o'lchandi (juda sekin); 2 171 079 ta amal, 11 009 amal/s | 115 s, RSS 348 MB, private 234 MB; 2 171 079 ta amal, 18 849 amal/s | ✅ |
| JSON Patch eksporti, hamma yozuv o'zgargan (bulk) | dastur < 500 MB | 5.3 MB/s, RSS 303 MB, private 31.9 MB (418 s; 6 513 235 ta amal) — bir marta o'lchandi (juda sekin) | 75.7 MB/s, RSS 352 MB, private 198 MB (29.5 s; fsync 280 ms, fsync'siz 76.4 MB/s; 6 513 235 ta amal; 4 ta ishchi jarayon) | ✅ |
| JSON Patch importi (tahrirsiz faylga, yangi sessiya), hamma yozuv o'zgargan (bulk) | dastur < 500 MB | 549 s, RSS 2 642 MB, private 2 534 MB — bir marta o'lchandi (juda sekin); 6 513 235 ta amal, 11 874 amal/s | 374 s, RSS 406 MB, private 291 MB — bir marta o'lchandi (juda sekin); 6 513 235 ta amal, 17 395 amal/s | ✅ |
| JSON Patch eksporti, 10% yozuv o'chirilgan + 1 000 ta qo'shilgan | dastur < 500 MB | 3.08 s, RSS 273 MB, private 263 MB; 652 324 ta amal | 3.00 s, RSS 158 MB, private 147 MB; 652 324 ta amal | ✅ |
| JSON Patch importi (tahrirsiz faylga, yangi sessiya), 10% yozuv o'chirilgan + 1 000 ta qo'shilgan | dastur < 500 MB | 18.9 s, RSS 140 MB, private 130 MB; 652 324 ta amal, 34 518 amal/s | 23.3 s, RSS 165 MB, private 156 MB; 652 324 ta amal, 28 016 amal/s | ✅ |
| Ochish + qatlamni tiklash (keshdagi indeks), 100 ta nuqtaviy tahrir | — (§12 da yo'q) | 16.0 ms, RSS 40.3 MB, private 28.8 MB | 24.0 ms, RSS 43.1 MB, private 31.4 MB | — |
| Oxirgi qadamni bekor qilish / qaytarish, 100 ta nuqtaviy tahrir | — (§12 da yo'q) | bekor qilish 21.0 ms, qaytarish 15.0 ms; RSS 40.5 MB | bekor qilish 16.0 ms, qaytarish 13.0 ms; RSS 43.1 MB | — |
| Ochish + qatlamni tiklash (keshdagi indeks), har 3-yozuv o'zgargan (bulk) | — (§12 da yo'q) | 591 ms, RSS 42.8 MB, private 31.4 MB | 497 ms, RSS 45.5 MB, private 34.1 MB | — |
| Oxirgi qadamni bekor qilish / qaytarish, har 3-yozuv o'zgargan (bulk) | — (§12 da yo'q) | bekor qilish 6.0 ms, qaytarish 3.0 ms; RSS 102 MB | bekor qilish 26.0 ms, qaytarish 3.0 ms; RSS 48.2 MB | — |
| Ochish + qatlamni tiklash (keshdagi indeks), hamma yozuv o'zgargan (bulk) | — (§12 da yo'q) | 1.85 s, RSS 42.9 MB, private 31.3 MB | 1.72 s, RSS 45.9 MB, private 34.5 MB | — |
| Oxirgi qadamni bekor qilish / qaytarish, hamma yozuv o'zgargan (bulk) | — (§12 da yo'q) | bekor qilish 20.0 ms, qaytarish 15.0 ms; RSS 197 MB | bekor qilish 19.0 ms, qaytarish 3.0 ms; RSS 49.7 MB | — |
| Ochish + qatlamni tiklash (keshdagi indeks), 10% yozuv o'chirilgan + 1 000 ta qo'shilgan | — (§12 da yo'q) | 549 ms, RSS 126 MB, private 114 MB | 499 ms, RSS 130 MB, private 118 MB | — |
| Oxirgi qadamni bekor qilish / qaytarish, 10% yozuv o'chirilgan + 1 000 ta qo'shilgan | — (§12 da yo'q) | bekor qilish 13.0 ms, qaytarish 3.0 ms; RSS 81.7 MB | bekor qilish 16.0 ms, qaytarish 16.0 ms; RSS 66.3 MB | — |

## Maqsadga yetilmagan joylar

- **Eksport CSV (9 ta skalyar ustun), 100 ta nuqtaviy tahrir** — 30.2 MB/s, RSS 107 MB, private 98.4 MB (71.0 s; fsync 247 ms, fsync'siz 30.3 MB/s) (maqsad: ≥ 50 MB/s, RAM < 300 MB).
  - Sabab: CSV yozuvchi (writer.py) har bir yozuvni parse qilib, kataklarni Python'da quradi; tahrirsiz faylning nazorat qatori ham shu tezlikda, ya'ni chegarani tahrirlar emas, CSV yozish belgilaydi (tahrirlangan yozuvlarni o'qish qo'shimchasi kichik).
  - Variantlar: 1) CSV qatorlarini bloklab ishchi jarayonlarda tayyorlash (engine.apply / bulk kabi; writer.py egasiga so'rov); 2) qator qurishni Rust yadroga o'tkazish (PathExtractor spanlari + xom tokenlar).
- **Eksport CSV (9 ta skalyar ustun), har 3-yozuv o'zgargan (bulk)** — 28.2 MB/s, RSS 134 MB, private 118 MB (77.0 s; fsync 923 ms, fsync'siz 28.5 MB/s) (maqsad: ≥ 50 MB/s, RAM < 300 MB).
- **Eksport CSV (9 ta skalyar ustun), hamma yozuv o'zgargan (bulk)** — 40.6 MB/s, RSS 132 MB, private 115 MB (55.0 s; fsync 489 ms, fsync'siz 40.9 MB/s) (maqsad: ≥ 50 MB/s, RAM < 300 MB).
- **Eksport CSV (9 ta skalyar ustun), 10% yozuv o'chirilgan + 1 000 ta qo'shilgan** — 28.3 MB/s, RSS 147 MB, private 138 MB (68.2 s) (maqsad: ≥ 50 MB/s, RAM < 300 MB).
- **Eksport CSV (9 ta skalyar ustun), tahrirsiz fayl (nazorat)** — 31.7 MB/s, RSS 103 MB, private 94.1 MB (67.7 s; fsync 254 ms, fsync'siz 31.8 MB/s) (maqsad: ≥ 50 MB/s, RAM < 300 MB).

## Kompyuter

- Protsessor: Intel(R) Core(TM) Ultra 9 285, 24 yadro / 24 mantiqiy protsessor
- RAM: 205.6 GB
- Disk, ma'lumot fayli: E: SAMSUNG MZVL81T0HELB-00BH1 (NVMe, SSD, NTFS), bo'sh joy 376 GB
- Disk, chiqishlar va qatlamlar: C: SAMSUNG MZVL81T0HELB-00BH1 (NVMe, SSD, NTFS), bo'sh joy 119 GB
- OS: Windows-11-10.0.26200-SP0; Python 3.14.3; yadro: rust

## O'lchash shartlari

- Har bir amal alohida yangi jarayonda (`bench/edit_bench.py`), har ssenariy o'z JSONRESHAPE_HOME'ida; ochish va qatlamni tiklash alohida o'lchanadi, jadvalda faqat amalning o'zi. Takrorlar: 2 (juda sekin amal — 150.0 s dan ortig'i — bir marta), eng yaxshisi olinadi.
- Xotira: jarayonning o'zi va uning bola jarayonlari (ishchi jarayonlar) har 20 ms da o'lchandi (RSS va private yig'indisi); RSS ga jarayon tegib o'tgan mmap sahifalari ham kiradi.
- Chiqish fayli fsync qilinadi; fsync vaqti alohida o'lchandi (bu diskda u katta ulush) va fsync'siz tezlik ham ko'rsatilgan. Chiqishlar va qatlamlar C: diskida (agentning vaqtinchalik papkasi).
- Kompyuter boshqa agentlar bilan umumiy: har o'lchovdan oldin yuklama ≤ 35.0% bo'lishi kutildi (ko'pi bilan 60.0 s); o'lchovlar paytidagi yuklama: o'rtacha 13.6%, eng ko'pi 66.5%.
- «Oldin» va «Keyin» jsonreshape paketining ikki nusxasida o'lchandi (`--pkg`): o'rtada boshqa agentlar ham o'z modullarini o'zgartirgan (writer, editstore, index, fileio, ...); tahrirsiz fayl qatorlari ularning ta'sirini ko'rsatuvchi nazorat.
- Ma'lumot fayli OS keshida (iliq kesh); 205 GB RAM.

## Nimani o'lchab bo'lmadi

- GUI'dagi vaqtlar (kutubxona darajasida o'lchandi; GUI shu amallarni fon vazifalarida chaqiradi).
- §12 dagi 5 GB holat: 2,1 GB fayl ishlatildi (tezlik fayl hajmiga bog'liq emas; xotira o'zgargan yozuvlar soniga ~24 bayt / yozuv bilan o'sadigan joylar ayrim aytilgan).

## Eslatma: «10% yozuv o'chirilgan» eksportlari (A/B)

«Keyin» ustunidagi bu qatorlar «Oldin»dan pastroq chiqdi; ularni bir xil sharoitda navbatma-navbat qayta o'lchadim
(P3 dan oldingi paket / yakuniy paket, 2 tadan, `edit-20261019-151207-ab-base-1.json` ... `edit-20261019-151515-ab-final2-2.json`):
JSON (indent 2) 28.4–33.0 s / 30.0–30.9 s, JSON Lines 24.3–24.5 s / 16.2–26.9 s — farq o'lchov shovqini ichida
(tahrirsiz faylning nazorat qatorlari ham shu o'lchovlarda 119 → 100 MB/s ga tushgan: kompyuter umumiy).

Qayta o'lchash: `.venv/Scripts/python.exe bench/edit_bench.py --work DIR [--pkg PAKET] [--baseline OLDINGI.json]`; faqat hisobot: `--report NATIJA.json --baseline OLDINGI.json`.
