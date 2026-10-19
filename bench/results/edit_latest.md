# jsonreshape: tahrirlar benchmarki (AGENT_PROMPT §12, 3-bosqich)

Sana: 2026-10-19 19:48 · fayl: `syn_2g.json` (2.1 GB, 6 513 235 ta yozuv) · to'liq natijalar: `edit-20261019-194807-final.json`, `edit-20261019-203414-final-rest.json` · oldingi o'lchov: `edit-20261019-112336-before.json`

## §12 maqsadi: tahrirlar bilan eksport va saqlash

| Amal | Maqsad | Oldin | Keyin | Holat |
|---|---|---|---|:---:|
| Eksport JSON (indent 2), 100 ta nuqtaviy tahrir | ≥ 50 MB/s, RAM < 300 MB | 106 MB/s, RSS 69.7 MB, private 58.2 MB (20.2 s; fsync 3.52 s, fsync'siz 129 MB/s) | 147 MB/s, RSS 73.2 MB, private 63.3 MB (14.6 s; fsync 7.14 s, fsync'siz 289 MB/s) | ✅ |
| Eksport JSON Lines, 100 ta nuqtaviy tahrir | ≥ 50 MB/s, RAM < 300 MB | 128 MB/s, RSS 84.9 MB, private 76.7 MB (16.7 s; fsync 1.91 s, fsync'siz 145 MB/s) | 227 MB/s, RSS 91.0 MB, private 83.2 MB (9.45 s; fsync 1.44 s, fsync'siz 268 MB/s) | ✅ |
| Eksport CSV (9 ta skalyar ustun), 100 ta nuqtaviy tahrir | ≥ 50 MB/s, RAM < 300 MB | 37.4 MB/s, RSS 104 MB, private 95.1 MB (57.4 s; fsync 357 ms, fsync'siz 37.6 MB/s) | 231 MB/s, RSS 222 MB, private 175 MB (9.31 s; fsync 579 ms, fsync'siz 246 MB/s; 4 ta ishchi jarayon) | ✅ |
| Boshqacha saqlash (Save As), 100 ta nuqtaviy tahrir | ≥ 50 MB/s, RAM < 300 MB | 282 MB/s, RSS 53.4 MB, private 42.3 MB (7.62 s; fsync 3.76 s, fsync'siz 556 MB/s) | 458 MB/s, RSS 57.2 MB, private 45.5 MB (4.69 s; fsync 2.56 s, fsync'siz 1 007 MB/s) | ✅ |
| Eksport JSON (indent 2), har 3-yozuv o'zgargan (bulk) | ≥ 50 MB/s, RAM < 300 MB | 55.7 MB/s, RSS 179 MB, private 117 MB (39.0 s; fsync 1.85 s, fsync'siz 58.5 MB/s) | 84.5 MB/s, RSS 122 MB, private 106 MB (25.7 s; fsync 3.35 s, fsync'siz 97.1 MB/s) | ✅ |
| Eksport JSON Lines, har 3-yozuv o'zgargan (bulk) | ≥ 50 MB/s, RAM < 300 MB | 66.9 MB/s, RSS 162 MB, private 102 MB (32.4 s; fsync 2.30 s, fsync'siz 72.0 MB/s) | 105 MB/s, RSS 112 MB, private 97.2 MB (20.6 s; fsync 1.55 s, fsync'siz 114 MB/s) | ✅ |
| Eksport CSV (9 ta skalyar ustun), har 3-yozuv o'zgargan (bulk) | ≥ 50 MB/s, RAM < 300 MB | 28.0 MB/s, RSS 185 MB, private 123 MB (77.4 s) | 187 MB/s, RSS 237 MB, private 187 MB (11.6 s; fsync 588 ms, fsync'siz 197 MB/s; 4 ta ishchi jarayon) | ✅ |
| Boshqacha saqlash (Save As), har 3-yozuv o'zgargan (bulk) | ≥ 50 MB/s, RAM < 300 MB | 85.6 MB/s, RSS 153 MB, private 90.1 MB (25.3 s; fsync 3.73 s, fsync'siz 100 MB/s) | 111 MB/s, RSS 111 MB, private 96.5 MB (19.6 s; fsync 3.54 s, fsync'siz 135 MB/s) | ✅ |
| Eksport JSON (indent 2), hamma yozuv o'zgargan (bulk) | ≥ 50 MB/s, RAM < 300 MB | 73.8 MB/s, RSS 267 MB, private 101 MB (30.3 s; fsync 1.27 s, fsync'siz 77.0 MB/s) | 118 MB/s, RSS 122 MB, private 105 MB (19.0 s; fsync 2.17 s, fsync'siz 133 MB/s) | ✅ |
| Eksport JSON Lines, hamma yozuv o'zgargan (bulk) | ≥ 50 MB/s, RAM < 300 MB | 87.5 MB/s, RSS 255 MB, private 90.3 MB (25.5 s; fsync 2.72 s, fsync'siz 97.9 MB/s) | 139 MB/s, RSS 111 MB, private 95.7 MB (16.0 s; fsync 1.59 s, fsync'siz 155 MB/s) | ✅ |
| Eksport CSV (9 ta skalyar ustun), hamma yozuv o'zgargan (bulk) | ≥ 50 MB/s, RAM < 300 MB | 31.3 MB/s, RSS 279 MB, private 113 MB (71.3 s; fsync 286 ms, fsync'siz 31.4 MB/s) | 199 MB/s, RSS 228 MB, private 172 MB (11.2 s; fsync 555 ms, fsync'siz 209 MB/s; 4 ta ishchi jarayon) | ✅ |
| Boshqacha saqlash (Save As), hamma yozuv o'zgargan (bulk) | ≥ 50 MB/s, RAM < 300 MB | 119 MB/s, RSS 211 MB, private 44.6 MB (18.7 s; fsync 3.16 s, fsync'siz 143 MB/s) | 272 MB/s, RSS 65.8 MB, private 49.9 MB (8.22 s; fsync 766 ms, fsync'siz 300 MB/s) | ✅ |
| Eksport JSON (indent 2), 10% yozuv o'chirilgan + 1 000 ta qo'shilgan | ≥ 50 MB/s, RAM < 300 MB | 78.9 MB/s, RSS 134 MB, private 123 MB (24.5 s; fsync 1.66 s, fsync'siz 84.6 MB/s) | 101 MB/s, RSS 135 MB, private 125 MB (19.1 s; fsync 1.33 s, fsync'siz 109 MB/s) | ✅ |
| Eksport JSON Lines, 10% yozuv o'chirilgan + 1 000 ta qo'shilgan | ≥ 50 MB/s, RAM < 300 MB | 88.3 MB/s, RSS 121 MB, private 114 MB (21.9 s; fsync 1.38 s, fsync'siz 94.2 MB/s) | 115 MB/s, RSS 126 MB, private 117 MB (16.8 s; fsync 1.53 s, fsync'siz 127 MB/s) | ✅ |
| Eksport CSV (9 ta skalyar ustun), 10% yozuv o'chirilgan + 1 000 ta qo'shilgan | ≥ 50 MB/s, RAM < 300 MB | 28.6 MB/s, RSS 145 MB, private 136 MB (67.5 s; fsync 254 ms, fsync'siz 28.7 MB/s) | 186 MB/s, RSS 244 MB, private 197 MB (10.4 s; fsync 540 ms, fsync'siz 196 MB/s; 4 ta ishchi jarayon) | ✅ |
| Boshqacha saqlash (Save As), 10% yozuv o'chirilgan + 1 000 ta qo'shilgan | ≥ 50 MB/s, RAM < 300 MB | 147 MB/s, RSS 90.2 MB, private 80.7 MB (13.2 s; fsync 1.46 s, fsync'siz 165 MB/s) | 309 MB/s, RSS 93.0 MB, private 83.6 MB (6.25 s; fsync 1.71 s, fsync'siz 426 MB/s) | ✅ |
| Dasturning umumiy xotirasi (barcha amallarning eng katta peak RSS) | < 500 MB | 2 642 MB RSS (eng kattasi: patch_import all (op)), private 2 534 MB | 422 MB RSS (eng kattasi: patch_import all (op)), private 306 MB | ✅ |

✅ maqsadga yetildi · ❌ yetilmadi (sababi va variantlari pastda) · — maqsad belgilanmagan. Vaqt — eng yaxshi takror, xotira — barcha takrorlarning eng kattasi (RSS = ishchi to'plam, private = shaxsiy baytlar; ishchi jarayonlar bilan birga); MB = 10⁶ bayt; tezlik = tahrirlangan hujjat yozuvlarining baytlari / vaqt. Holat «Keyin» ustuniga qarab.

## Tahrirlash amallari (qo'shimcha o'lchovlar)

| Amal | Maqsad | Oldin | Keyin | Holat |
|---|---|---|---|:---:|
| Eksport JSON (indent 2), tahrirsiz fayl (nazorat) | ≥ 50 MB/s, RAM < 300 MB | 119 MB/s, RSS 67.3 MB, private 57.0 MB (18.0 s; fsync 2.75 s, fsync'siz 140 MB/s) | 166 MB/s, RSS 70.9 MB, private 60.9 MB (12.9 s; fsync 6.06 s, fsync'siz 313 MB/s) | ✅ |
| Eksport JSON Lines, tahrirsiz fayl (nazorat) | ≥ 50 MB/s, RAM < 300 MB | 137 MB/s, RSS 80.0 MB, private 74.6 MB (15.7 s; fsync 541 ms, fsync'siz 142 MB/s) | 233 MB/s, RSS 87.8 MB, private 79.9 MB (9.20 s; fsync 1.90 s, fsync'siz 294 MB/s) | ✅ |
| Eksport CSV (9 ta skalyar ustun), tahrirsiz fayl (nazorat) | ≥ 50 MB/s, RAM < 300 MB | 34.7 MB/s, RSS 101 MB, private 93.3 MB (61.9 s; fsync 341 ms, fsync'siz 34.9 MB/s) | 253 MB/s, RSS 217 MB, private 169 MB (8.48 s; fsync 624 ms, fsync'siz 273 MB/s; 4 ta ishchi jarayon) | ✅ |
| Boshqacha saqlash (Save As), tahrirsiz fayl (nazorat) | ≥ 50 MB/s, RAM < 300 MB | 252 MB/s, RSS 50.4 MB, private 40.4 MB (8.52 s; fsync 4.58 s, fsync'siz 546 MB/s) | 313 MB/s, RSS 54.2 MB, private 44.4 MB (6.85 s; fsync 4.72 s, fsync'siz 1 008 MB/s) | ✅ |
| Bulk ko'rib chiqish set_field: hamma yozuv o'zgargan (bulk) | ≥ 50 MB/s (hamma yozuv o'zgarganda), dastur < 500 MB | 17.9 MB/s, RSS 291 MB, private 281 MB (120 s; fsync 1.03 s, fsync'siz 18.0 MB/s) | 151 MB/s, RSS 303 MB, private 255 MB (14.2 s; fsync 2.52 s, fsync'siz 184 MB/s; 4 ta ishchi jarayon) | ✅ |
| Bulk ko'rib chiqish set_field: har 3-yozuv o'zgargan (bulk) | ≥ 50 MB/s, dastur < 500 MB | 50.3 MB/s, RSS 244 MB, private 129 MB (42.7 s; fsync 685 ms, fsync'siz 51.2 MB/s) | 411 MB/s, RSS 367 MB, private 219 MB (5.23 s; fsync 817 ms, fsync'siz 487 MB/s; 4 ta ishchi jarayon) | ✅ |
| Bulk ko'rib chiqish «set_field», hamma yozuvlar (har yozuvga maydon) | ≥ 50 MB/s (hamma yozuv o'zgarganda), dastur < 500 MB | 19.5 MB/s, RSS 325 MB, private 319 MB (110 s; fsync 300 ms, fsync'siz 19.6 MB/s) | 136 MB/s, RSS 295 MB, private 246 MB (15.8 s; fsync 3.24 s, fsync'siz 171 MB/s; 4 ta ishchi jarayon) | ✅ |
| Bulk ko'rib chiqish «replace», hamma yozuvlar («+998» → «998», har yozuvda) | ≥ 50 MB/s (hamma yozuv o'zgarganda), dastur < 500 MB | 10.3 MB/s, RSS 328 MB, private 322 MB (209 s; fsync 1.07 s, fsync'siz 10.3 MB/s) — bir marta o'lchandi (juda sekin) | 102 MB/s, RSS 298 MB, private 244 MB (21.1 s; fsync 3.03 s, fsync'siz 119 MB/s; 4 ta ishchi jarayon) | ✅ |
| Bulk ko'rib chiqish «rename_key», hamma yozuvlar (active → is_active) | ≥ 50 MB/s (hamma yozuv o'zgarganda), dastur < 500 MB | 12.7 MB/s, RSS 328 MB, private 320 MB (169 s; fsync 1.71 s, fsync'siz 12.8 MB/s) — bir marta o'lchandi (juda sekin) | 112 MB/s, RSS 303 MB, private 247 MB (19.1 s; fsync 1.70 s, fsync'siz 123 MB/s; 4 ta ishchi jarayon) | ✅ |
| Bulk ko'rib chiqish «delete_field», hamma yozuvlar (tags o'chiriladi) | ≥ 50 MB/s (hamma yozuv o'zgarganda), dastur < 500 MB | 18.4 MB/s, RSS 326 MB, private 320 MB (117 s; fsync 393 ms, fsync'siz 18.4 MB/s) | 142 MB/s, RSS 302 MB, private 248 MB (15.1 s; fsync 1.82 s, fsync'siz 162 MB/s; 4 ta ishchi jarayon) | ✅ |
| Bulk ko'rib chiqish «clean_strings», hamma yozuvlar (trim + bo'sh joylar, hech narsa o'zgarmaydi) | dastur < 500 MB | 22.2 MB/s, RSS 65.7 MB, private 54.2 MB (96.5 s) | 193 MB/s, RSS 234 MB, private 178 MB (11.1 s; 4 ta ishchi jarayon) | ✅ |
| Bulk ko'rib chiqish «convert_type», hamma yozuvlar (score → satr) | ≥ 50 MB/s (hamma yozuv o'zgarganda), dastur < 500 MB | 14.8 MB/s, RSS 334 MB, private 325 MB (145 s; fsync 1.98 s, fsync'siz 15.0 MB/s) | 127 MB/s, RSS 294 MB, private 238 MB (16.9 s; fsync 1.51 s, fsync'siz 139 MB/s; 4 ta ishchi jarayon) | ✅ |
| Bulk ko'rib chiqish «dedupe», hamma yozuvlar (id bo'yicha, takror yo'q) | dastur < 500 MB | 80.5 MB/s, RSS 233 MB, private 225 MB (26.7 s) | 162 MB/s, RSS 238 MB, private 229 MB (13.3 s) | ✅ |
| JSON Patch eksporti, 100 ta nuqtaviy tahrir | dastur < 500 MB | 26.0 ms, RSS 41.1 MB, private 29.6 MB; 100 ta amal | 21.0 ms, RSS 47.0 MB, private 35.2 MB; 100 ta amal | ✅ |
| JSON Patch eksporti, har 3-yozuv o'zgargan (bulk) | dastur < 500 MB | 14.7 MB/s, RSS 212 MB, private 44.8 MB (147 s; 2 171 079 ta amal) | 183 MB/s, RSS 402 MB, private 204 MB (11.8 s; fsync 56.0 ms, fsync'siz 184 MB/s; 2 171 079 ta amal; 4 ta ishchi jarayon) | ✅ |
| JSON Patch importi (tahrirsiz faylga, yangi sessiya), har 3-yozuv o'zgargan (bulk) | dastur < 500 MB | 197 s, RSS 1 042 MB, private 930 MB — bir marta o'lchandi (juda sekin); 2 171 079 ta amal, 11 009 amal/s | 60.4 s, RSS 343 MB, private 231 MB; 2 171 079 ta amal, 35 932 amal/s | ✅ |
| JSON Patch eksporti, hamma yozuv o'zgargan (bulk) | dastur < 500 MB | 5.3 MB/s, RSS 303 MB, private 31.9 MB (418 s; 6 513 235 ta amal) — bir marta o'lchandi (juda sekin) | 121 MB/s, RSS 358 MB, private 201 MB (18.4 s; fsync 431 ms, fsync'siz 124 MB/s; 6 513 235 ta amal; 4 ta ishchi jarayon) | ✅ |
| JSON Patch importi (tahrirsiz faylga, yangi sessiya), hamma yozuv o'zgargan (bulk) | dastur < 500 MB | 549 s, RSS 2 642 MB, private 2 534 MB — bir marta o'lchandi (juda sekin); 6 513 235 ta amal, 11 874 amal/s | 132 s, RSS 422 MB, private 306 MB; 6 513 235 ta amal, 49 197 amal/s | ✅ |
| JSON Patch eksporti, 10% yozuv o'chirilgan + 1 000 ta qo'shilgan | dastur < 500 MB | 3.08 s, RSS 273 MB, private 263 MB; 652 324 ta amal | 1.90 s, RSS 158 MB, private 148 MB; 652 324 ta amal | ✅ |
| JSON Patch importi (tahrirsiz faylga, yangi sessiya), 10% yozuv o'chirilgan + 1 000 ta qo'shilgan | dastur < 500 MB | 18.9 s, RSS 140 MB, private 130 MB; 652 324 ta amal, 34 518 amal/s | 9.68 s, RSS 157 MB, private 147 MB; 652 324 ta amal, 67 417 amal/s | ✅ |
| Ochish + qatlamni tiklash (keshdagi indeks), 100 ta nuqtaviy tahrir | — (§12 da yo'q) | 16.0 ms, RSS 40.3 MB, private 28.8 MB | 22.0 ms, RSS 44.2 MB, private 32.6 MB | — |
| Oxirgi qadamni bekor qilish / qaytarish, 100 ta nuqtaviy tahrir | — (§12 da yo'q) | bekor qilish 21.0 ms, qaytarish 15.0 ms; RSS 40.5 MB | bekor qilish 8.0 ms, qaytarish 3.0 ms; RSS 44.2 MB | — |
| Ochish + qatlamni tiklash (keshdagi indeks), har 3-yozuv o'zgargan (bulk) | — (§12 da yo'q) | 591 ms, RSS 42.8 MB, private 31.4 MB | 155 ms, RSS 48.3 MB, private 37.0 MB | — |
| Oxirgi qadamni bekor qilish / qaytarish, har 3-yozuv o'zgargan (bulk) | — (§12 da yo'q) | bekor qilish 6.0 ms, qaytarish 3.0 ms; RSS 102 MB | bekor qilish 6.0 ms, qaytarish 3.0 ms; RSS 49.4 MB | — |
| Ochish + qatlamni tiklash (keshdagi indeks), hamma yozuv o'zgargan (bulk) | — (§12 da yo'q) | 1.85 s, RSS 42.9 MB, private 31.3 MB | 423 ms, RSS 48.2 MB, private 37.0 MB | — |
| Oxirgi qadamni bekor qilish / qaytarish, hamma yozuv o'zgargan (bulk) | — (§12 da yo'q) | bekor qilish 20.0 ms, qaytarish 15.0 ms; RSS 197 MB | bekor qilish 6.0 ms, qaytarish 3.0 ms; RSS 50.9 MB | — |
| Ochish + qatlamni tiklash (keshdagi indeks), 10% yozuv o'chirilgan + 1 000 ta qo'shilgan | — (§12 da yo'q) | 549 ms, RSS 126 MB, private 114 MB | 397 ms, RSS 130 MB, private 120 MB | — |
| Oxirgi qadamni bekor qilish / qaytarish, 10% yozuv o'chirilgan + 1 000 ta qo'shilgan | — (§12 da yo'q) | bekor qilish 13.0 ms, qaytarish 3.0 ms; RSS 81.7 MB | bekor qilish 9.0 ms, qaytarish 3.0 ms; RSS 67.2 MB | — |

## Maqsadga yetilmagan joylar

—

## Kompyuter

- Protsessor: Intel(R) Core(TM) Ultra 9 285, 24 yadro / 24 mantiqiy protsessor
- RAM: 205.6 GB
- Disk, ma'lumot fayli: E: SAMSUNG MZVL81T0HELB-00BH1 (NVMe, SSD, NTFS), bo'sh joy 376 GB
- Disk, chiqishlar va qatlamlar: C: SAMSUNG MZVL81T0HELB-00BH1 (NVMe, SSD, NTFS), bo'sh joy 100 GB
- OS: Windows-11-10.0.26200-SP0; Python 3.14.3; yadro: rust

## O'lchash shartlari

- Har bir amal alohida yangi jarayonda (`bench/edit_bench.py`), har ssenariy o'z JSONRESHAPE_HOME'ida; ochish va qatlamni tiklash alohida o'lchanadi, jadvalda faqat amalning o'zi. Takrorlar: 2 (juda sekin amal — 150.0 s dan ortig'i — bir marta), eng yaxshisi olinadi.
- Xotira: jarayonning o'zi va uning bola jarayonlari (ishchi jarayonlar) har 20 ms da o'lchandi (RSS va private yig'indisi); RSS ga jarayon tegib o'tgan mmap sahifalari ham kiradi.
- Chiqish fayli fsync qilinadi; fsync vaqti alohida o'lchandi (bu diskda u katta ulush) va fsync'siz tezlik ham ko'rsatilgan. Chiqishlar va qatlamlar C: diskida (agentning vaqtinchalik papkasi).
- Har o'lchovdan oldin tizim CPU yuklamasi ≤ 35.0% bo'lishi kutildi (ko'pi bilan 60.0 s); o'lchovlar paytidagi yuklama (o'lchanayotgan amalning o'zi ham kiradi): o'rtacha 12.4%, eng ko'pi 100%.
- «Oldin» — `--baseline` dagi o'lchov (boshqa paket nusxasi, `--pkg`, yoki oldingi o'lchov): o'rtada boshqa modullar ham o'zgargan (writer, editstore, index, fileio, ...); tahrirsiz fayl qatorlari ularning ta'sirini ko'rsatuvchi nazorat.
- Ma'lumot fayli OS keshida (iliq kesh); 205 GB RAM.
- Yakuniy o'lchov (6-bosqich, S6-D): kompyuterda boshqa agent yoki og'ir ish yo'q edi; «Keyin» — yakuniy kod. «Oldin» — 3-bosqichning unumdorlik ishidan (P3) oldingi paket nusxasi, 2026-10-19 da kompyuter boshqa agentlar bilan band paytida o'lchangan (`edit-20261019-112336-before.json`); tahrirsiz fayl qatorlari ikki o'lchov sharoitining farqini ko'rsatadi.

## Nimani o'lchab bo'lmadi

- GUI'dagi vaqtlar (kutubxona darajasida o'lchandi; GUI shu amallarni fon vazifalarida chaqiradi).
- §12 dagi 5 GB holat: 2,1 GB fayl ishlatildi (tezlik fayl hajmiga bog'liq emas; xotira o'zgargan yozuvlar soniga ~24 bayt / yozuv bilan o'sadigan joylar ayrim aytilgan).

Qayta o'lchash: `.venv/Scripts/python.exe bench/edit_bench.py --work DIR [--pkg PAKET] [--baseline OLDINGI.json]`; faqat hisobot: `--report NATIJA.json --baseline OLDINGI.json`.
