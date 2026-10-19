# jsonreshape: jadval eksportlari benchmarki (CSV, XLSX, SQLite; AGENT_PROMPT §12)

Sana: 2026-10-19 19:48 · fayl: `syn_2g.json` (2.1 GB, 6 513 235 ta yozuv) · to'liq natijalar: `edit-20261019-194807-final.json` · oldingi o'lchov: `edit-20261019-173524-ab-before-base.json`, `edit-20261019-174029-ab-before-point.json`, `edit-20261019-174534-ab-before-third.json`, `edit-20261019-175217-ab-before-all.json`, `edit-20261019-175933-ab-before-dels.json`, `edit-20261019-181706-ab-before-tab-base.json`, `edit-20261019-182810-ab-before-tab-all.json`, `edit-20261019-180909-ab-before-restore-1.json`, `edit-20261019-180957-ab-before-restore-2.json`

## §12 maqsadi: tahrirlar bilan (va tahrirsiz) jadval eksporti

| Amal | Maqsad | Oldin | Keyin | Holat |
|---|---|---|---|:---:|
| Eksport CSV (9 ta skalyar ustun), tahrirsiz fayl (nazorat) | ≥ 50 MB/s, RAM < 300 MB | 57.8 MB/s, RSS 105 MB, private 97.6 MB (37.1 s; fsync 698 ms, fsync'siz 58.9 MB/s) | 253 MB/s, RSS 217 MB, private 169 MB (8.48 s; fsync 624 ms, fsync'siz 273 MB/s; 4 ta ishchi jarayon) | ✅ |
| Eksport CSV (avtomatik ustunlar), tahrirsiz fayl (nazorat) | ≥ 50 MB/s, RAM < 300 MB | 28.1 MB/s, RSS 112 MB, private 104 MB (76.4 s; fsync 584 ms, fsync'siz 28.3 MB/s) | 117 MB/s, RSS 219 MB, private 170 MB (18.3 s; fsync 1.23 s, fsync'siz 126 MB/s; 4 ta ishchi jarayon) | ✅ |
| Eksport XLSX (9 ta skalyar ustun), tahrirsiz fayl (nazorat) | ≥ 50 MB/s, RAM < 300 MB | 10.2 MB/s, RSS 83.4 MB, private 72.8 MB (211 s; fsync 147 ms, fsync'siz 10.2 MB/s) — bir marta o'lchandi (juda sekin) | 12.0 MB/s, RSS 144 MB, private 115 MB (179 s; fsync 890 ms, fsync'siz 12.1 MB/s; 2 ta ishchi jarayon) — bir marta o'lchandi (juda sekin) | ❌ |
| Eksport SQLite, tahrirsiz fayl (nazorat) | ≥ 50 MB/s, RAM < 300 MB | 17.0 MB/s, RSS 138 MB, private 143 MB (127 s) | 16.4 MB/s, RSS 138 MB, private 143 MB (131 s) | ❌ |
| Ochish + qatlamni tiklash (keshdagi indeks), tahrirsiz fayl (nazorat) | — (§12 da yo'q) | o'lchanmagan | 3.0 ms, RSS 43.2 MB, private 32.5 MB | — |
| Eksport CSV (9 ta skalyar ustun), 100 ta nuqtaviy tahrir | ≥ 50 MB/s, RAM < 300 MB | 61.7 MB/s, RSS 107 MB, private 97.6 MB (34.8 s; fsync 353 ms, fsync'siz 62.3 MB/s) | 231 MB/s, RSS 222 MB, private 175 MB (9.31 s; fsync 579 ms, fsync'siz 246 MB/s; 4 ta ishchi jarayon) | ✅ |
| Eksport CSV (avtomatik ustunlar), 100 ta nuqtaviy tahrir | ≥ 50 MB/s, RAM < 300 MB | 28.4 MB/s, RSS 116 MB, private 108 MB (75.7 s; fsync 562 ms, fsync'siz 28.6 MB/s) | 102 MB/s, RSS 225 MB, private 175 MB (21.1 s; fsync 1.13 s, fsync'siz 107 MB/s; 4 ta ishchi jarayon) | ✅ |
| Ochish + qatlamni tiklash (keshdagi indeks), 100 ta nuqtaviy tahrir | — (§12 da yo'q) | o'lchanmagan | 22.0 ms, RSS 44.2 MB, private 32.6 MB | — |
| Eksport CSV (9 ta skalyar ustun), har 3-yozuv o'zgargan (bulk) | ≥ 50 MB/s, RAM < 300 MB | 49.2 MB/s, RSS 135 MB, private 120 MB (44.1 s; fsync 332 ms, fsync'siz 49.5 MB/s) | 187 MB/s, RSS 237 MB, private 187 MB (11.6 s; fsync 588 ms, fsync'siz 197 MB/s; 4 ta ishchi jarayon) | ✅ |
| Eksport CSV (avtomatik ustunlar), har 3-yozuv o'zgargan (bulk) | ≥ 50 MB/s, RAM < 300 MB | 20.7 MB/s, RSS 142 MB, private 127 MB (105 s; fsync 427 ms, fsync'siz 20.8 MB/s) | 83.7 MB/s, RSS 235 MB, private 182 MB (25.9 s; fsync 1.34 s, fsync'siz 88.2 MB/s; 4 ta ishchi jarayon) | ✅ |
| Ochish + qatlamni tiklash (keshdagi indeks), har 3-yozuv o'zgargan (bulk) | — (§12 da yo'q) | 343 ms, RSS 45.8 MB, private 34.8 MB | 155 ms, RSS 48.3 MB, private 37.0 MB | — |
| Eksport CSV (9 ta skalyar ustun), hamma yozuv o'zgargan (bulk) | ≥ 50 MB/s, RAM < 300 MB | 51.0 MB/s, RSS 134 MB, private 117 MB (43.7 s; fsync 358 ms, fsync'siz 51.5 MB/s) | 199 MB/s, RSS 228 MB, private 172 MB (11.2 s; fsync 555 ms, fsync'siz 209 MB/s; 4 ta ishchi jarayon) | ✅ |
| Eksport CSV (avtomatik ustunlar), hamma yozuv o'zgargan (bulk) | ≥ 50 MB/s, RAM < 300 MB | 19.2 MB/s, RSS 140 MB, private 124 MB (116 s; fsync 1.19 s, fsync'siz 19.4 MB/s) | 90.1 MB/s, RSS 235 MB, private 178 MB (24.8 s; fsync 1.37 s, fsync'siz 95.3 MB/s; 4 ta ishchi jarayon) | ✅ |
| Eksport XLSX (9 ta skalyar ustun), hamma yozuv o'zgargan (bulk) | ≥ 50 MB/s, RAM < 300 MB | 10.8 MB/s, RSS 112 MB, private 95.5 MB (206 s; fsync 327 ms, fsync'siz 10.8 MB/s) — bir marta o'lchandi (juda sekin) | 7.3 MB/s, RSS 159 MB, private 122 MB (307 s; fsync 156 ms, fsync'siz 7.3 MB/s; 2 ta ishchi jarayon) — bir marta o'lchandi (juda sekin) | ❌ |
| Eksport SQLite, hamma yozuv o'zgargan (bulk) | ≥ 50 MB/s, RAM < 300 MB | 14.9 MB/s, RSS 146 MB, private 128 MB (150 s) | 8.9 MB/s, RSS 153 MB, private 152 MB (250 s) — bir marta o'lchandi (juda sekin) | ❌ |
| Ochish + qatlamni tiklash (keshdagi indeks), hamma yozuv o'zgargan (bulk) | — (§12 da yo'q) | 1.04 s, RSS 46.7 MB, private 35.4 MB | 423 ms, RSS 48.2 MB, private 37.0 MB | — |
| Eksport CSV (9 ta skalyar ustun), 10% yozuv o'chirilgan + 1 000 ta qo'shilgan | ≥ 50 MB/s, RAM < 300 MB | 49.2 MB/s, RSS 148 MB, private 139 MB (39.3 s; fsync 387 ms, fsync'siz 49.7 MB/s) | 186 MB/s, RSS 244 MB, private 197 MB (10.4 s; fsync 540 ms, fsync'siz 196 MB/s; 4 ta ishchi jarayon) | ✅ |
| Eksport CSV (avtomatik ustunlar), 10% yozuv o'chirilgan + 1 000 ta qo'shilgan | ≥ 50 MB/s, RAM < 300 MB | 23.6 MB/s, RSS 154 MB, private 145 MB (82.0 s; fsync 526 ms, fsync'siz 23.7 MB/s) | 96.7 MB/s, RSS 247 MB, private 198 MB (20.0 s; fsync 1.11 s, fsync'siz 102 MB/s; 4 ta ishchi jarayon) | ✅ |
| Ochish + qatlamni tiklash (keshdagi indeks), 10% yozuv o'chirilgan + 1 000 ta qo'shilgan | — (§12 da yo'q) | 392 ms, RSS 131 MB, private 120 MB | 397 ms, RSS 130 MB, private 120 MB | — |

✅ maqsadga yetildi · ❌ yetilmadi (sababi va variantlari pastda) · — maqsad belgilanmagan. Vaqt — eng yaxshi takror, xotira — barcha takrorlarning eng kattasi (RSS = ishchi to'plam, private = shaxsiy baytlar; ishchi jarayonlar bilan birga); MB = 10⁶ bayt; tezlik = tahrirlangan hujjat yozuvlarining baytlari / vaqt. Holat «Keyin» ustuniga qarab.

P6 o'zgarishlari (`jsonreshape/writer.py`): CSV qatorlari ishchi jarayonlarda tayyorlanadi (`writer.export(..., workers=None)`: 64 MiB dan katta tanlovda avtomatik, `bulk.auto_workers` — standart xotira chegarasi (512 MiB) bilan 4 ta); ota jarayon bloklarni o'qiydi va natijalarni tartib bilan yozadi — fayl bir oqimli yozuvchinikidan bayt-bayt farq qilmaydi (2,1 GB faylda SHA-256 bilan tekshirildi: tahrirsiz, «hamma yozuv o'zgargan» va «10% o'chirilgan» holatlarida, 9 ta ustun, avtomatik ustunlar va qatorlar rejimi). Avtomatik ustunlarni topish skaneri ham ishchi jarayonlarda (har blok o'z skaneri, natijalar tartib bilan birlashtiriladi; map-ga o'xshash obyekt yoki juda ko'p yo'l bo'lsa — bir oqimli skaner). XLSX: 2 ta ishchi kataklarni tayyorlaydi, xlsxwriter ota jarayonda (varaq bir xil). SQLite o'zgarmagan (nazorat). Qatlamni tiklash (`editstore.py`): 64 MiB va undan katta nazorat yig'indisi oralig'i 4 ta oqimda bo'laklab o'qiladi (tekshiruv o'sha, holat qaytarilishidan oldin tugaydi).

«Oldin» — P6 dan oldingi paket nusxasi (`--pkg`), 2026-10-19 da kompyuter boshqa agentlar bilan band paytida o'lchangan; «Keyin» — yakuniy kod (6-bosqich), kompyuterda boshqa agent yoki og'ir ish yo'q paytda (S6-D), o'sha skript va o'sha xotira o'lchagich bilan (bola jarayonlar ro'yxati har 0,5 s da yangilanadi). Takrorlar: 2 ta, eng yaxshisi; 150 s dan uzoq amal bir marta.

## Maqsadga yetilmagan joylar

- **Eksport XLSX (9 ta skalyar ustun), tahrirsiz fayl (nazorat)** — 12.0 MB/s, RSS 144 MB, private 115 MB (179 s; fsync 890 ms, fsync'siz 12.1 MB/s; 2 ta ishchi jarayon) — bir marta o'lchandi (juda sekin) (maqsad: ≥ 50 MB/s, RAM < 300 MB).
  - Sabab: xlsxwriter har katakni Python'da XML ga yozadi (~2,5 mks / katak) va oxirida zip quradi (README qaror 39): cProfile bo'yicha vaqtning ~80% i xlsxwriter'ning katak yozishi, ~5% zip; qiymatlarni tayyorlash (~15%) endi ishchi jarayonlarda, lekin xlsxwriter ota jarayonda bitta oqimda qoladi.
  - Variantlar: o'z XLSX yozuvchimiz: varaq XML'ini ishchi jarayonlarda tayyorlash (qatorlar tartib bilan), zip alohida oqimda — 6-bosqichda ixtiyoriy (qaror 39); hozircha katta jadvallar uchun CSV.
- **Eksport SQLite, tahrirsiz fayl (nazorat)** — 16.4 MB/s, RSS 138 MB, private 143 MB (131 s) (maqsad: ≥ 50 MB/s, RAM < 300 MB).
  - Sabab: ikki o'tish (map-larni topish, so'ng qatorlar); har yozuv Python'da jadvallarga bo'linadi (har katak uchun tur tekshiruvi, ustun/jadval reestri) va sqlite3 executemany bilan bitta oqimda yoziladi.
  - Variantlar: 1) birinchi o'tishni (map-lar) ishchi jarayonlarda bajarish (CSV skaneri kabi); 2) qatorlarni ishchi jarayonlarda tayyorlash (jadval/ustun reestri va _id/_parent ota jarayonda) — keyingi ish.
- **Eksport XLSX (9 ta skalyar ustun), hamma yozuv o'zgargan (bulk)** — 7.3 MB/s, RSS 159 MB, private 122 MB (307 s; fsync 156 ms, fsync'siz 7.3 MB/s; 2 ta ishchi jarayon) — bir marta o'lchandi (juda sekin) (maqsad: ≥ 50 MB/s, RAM < 300 MB).
- **Eksport SQLite, hamma yozuv o'zgargan (bulk)** — 8.9 MB/s, RSS 153 MB, private 152 MB (250 s) — bir marta o'lchandi (juda sekin) (maqsad: ≥ 50 MB/s, RAM < 300 MB).

## Kompyuter

- Protsessor: Intel(R) Core(TM) Ultra 9 285, 24 yadro / 24 mantiqiy protsessor
- RAM: 205.6 GB
- Disk, ma'lumot fayli: E: SAMSUNG MZVL81T0HELB-00BH1 (NVMe, SSD, NTFS), bo'sh joy 376 GB
- Disk, chiqishlar va qatlamlar: C: SAMSUNG MZVL81T0HELB-00BH1 (NVMe, SSD, NTFS), bo'sh joy 99 GB
- OS: Windows-11-10.0.26200-SP0; Python 3.14.3; yadro: rust

## O'lchash shartlari

- Har bir amal alohida yangi jarayonda (`bench/edit_bench.py`), har ssenariy o'z JSONRESHAPE_HOME'ida; ochish va qatlamni tiklash alohida o'lchanadi, jadvalda faqat amalning o'zi. Takrorlar: 2 (juda sekin amal — 150.0 s dan ortig'i — bir marta), eng yaxshisi olinadi.
- Xotira: jarayonning o'zi va uning bola jarayonlari (ishchi jarayonlar) har 20 ms da o'lchandi (RSS va private yig'indisi); RSS ga jarayon tegib o'tgan mmap sahifalari ham kiradi.
- Chiqish fayli fsync qilinadi; fsync vaqti alohida o'lchandi (bu diskda u katta ulush) va fsync'siz tezlik ham ko'rsatilgan. Chiqishlar va qatlamlar C: diskida (agentning vaqtinchalik papkasi).
- Har o'lchovdan oldin tizim CPU yuklamasi ≤ 35.0% bo'lishi kutildi (ko'pi bilan 60.0 s); o'lchovlar paytidagi yuklama (o'lchanayotgan amalning o'zi ham kiradi): o'rtacha 11.5%, eng ko'pi 100%.
- «Oldin» — `--baseline` dagi o'lchov (boshqa paket nusxasi, `--pkg`, yoki oldingi o'lchov): o'rtada boshqa modullar ham o'zgargan (writer, editstore, index, fileio, ...); tahrirsiz fayl qatorlari ularning ta'sirini ko'rsatuvchi nazorat.
- Ma'lumot fayli OS keshida (iliq kesh); 205 GB RAM.

## Nimani o'lchab bo'lmadi

- GUI'dagi vaqtlar (kutubxona darajasida o'lchandi; GUI shu amallarni fon vazifalarida chaqiradi).
- §12 dagi 5 GB holat: 2,1 GB fayl ishlatildi (tezlik fayl hajmiga bog'liq emas; xotira o'zgargan yozuvlar soniga ~24 bayt / yozuv bilan o'sadigan joylar ayrim aytilgan).

Qayta o'lchash: `.venv/Scripts/python.exe bench/edit_bench.py --work DIR --only export_csv,export_csv_auto,export_xlsx,export_sqlite [--pkg PAKET] [--workers N]`; faqat hisobot: `--report NATIJA.json --export-report --baseline OLDINGI.json`.
