# jsonreshape: jadval eksportlari benchmarki (CSV, XLSX, SQLite; AGENT_PROMPT §12)

Sana: 2026-10-21 17:13 · fayl: `syn_2g.json` (2.1 GB, 6 513 235 ta yozuv) · to'liq natijalar: `edit-20261021-171332-s7x-after.json`, `edit-20261021-171550-s7x-after-all.json`, `edit-20261021-172858-s7x-csv-after-1.json`, `edit-20261021-173133-s7x-csv-after-2.json` · oldingi o'lchov: `edit-20261021-170441-s7x-before.json`, `edit-20261021-172004-s7x-before-all.json`, `edit-20261021-173017-s7x-csv-before-1.json`, `edit-20261021-173249-s7x-csv-before-2.json` · 6-bosqich o'lchovi: `edit-20261019-194807-final.json`

## §12 maqsadi: tahrirlar bilan (va tahrirsiz) jadval eksporti

| Amal | Maqsad | Oldin | Keyin | Holat |
|---|---|---|---|:---:|
| Eksport CSV (9 ta skalyar ustun), tahrirsiz fayl (nazorat) | ≥ 50 MB/s, RAM < 300 MB | 226 MB/s, RSS 263 MB, private 211 MB (9.51 s; 8 ta takrordan eng yaxshisi, mediana 203 MB/s; 4 ta ishchi jarayon) | 224 MB/s, RSS 261 MB, private 210 MB (9.59 s; 8 ta takrordan eng yaxshisi, mediana 211 MB/s; 4 ta ishchi jarayon) | ✅ |
| Eksport XLSX (9 ta skalyar ustun), tahrirsiz fayl (nazorat) | ≥ 50 MB/s, RAM < 300 MB | 7.8 MB/s, RSS 158 MB, private 125 MB (274 s; fsync 310 ms; 2 ta ishchi jarayon; fayl 399 MB) — bir marta o'lchandi (juda sekin) | 116 MB/s, RSS 250 MB, private 204 MB (18.5 s; fsync 169 ms; 4 ta ishchi jarayon; fayl 217 MB) | ✅ |
| Eksport SQLite, tahrirsiz fayl (nazorat) | ≥ 50 MB/s, RAM < 300 MB | 10.6 MB/s, RSS 140 MB, private 128 MB (203 s; bitta oqim) — bir marta o'lchandi (juda sekin) | 79.1 MB/s, RSS 270 MB, private 213 MB (27.2 s; 4 ta ishchi jarayon; 31 913 465 qator) | ✅ |
| Eksport CSV (9 ta skalyar ustun), hamma yozuv o'zgargan (bulk) | ≥ 50 MB/s, RAM < 300 MB | 211 MB/s, RSS 271 MB, private 213 MB (10.6 s; 8 ta takrordan eng yaxshisi, mediana 194 MB/s; 4 ta ishchi jarayon) | 213 MB/s, RSS 270 MB, private 212 MB (10.5 s; 8 ta takrordan eng yaxshisi, mediana 194 MB/s; 4 ta ishchi jarayon) | ✅ |
| Eksport XLSX (9 ta skalyar ustun), hamma yozuv o'zgargan (bulk) | ≥ 50 MB/s, RAM < 300 MB | 8.0 MB/s, RSS 173 MB, private 133 MB (281 s; fsync 1.82 s; 2 ta ishchi jarayon) — bir marta o'lchandi (juda sekin) | 116 MB/s, RSS 265 MB, private 208 MB (19.3 s; fsync 209 ms; 4 ta ishchi jarayon) | ✅ |
| Eksport SQLite, hamma yozuv o'zgargan (bulk) | ≥ 50 MB/s, RAM < 300 MB | 11.7 MB/s, RSS 150 MB, private 149 MB (192 s; bitta oqim) — bir marta o'lchandi (juda sekin) | 78.6 MB/s, RSS 282 MB, private 218 MB (28.4 s; 4 ta ishchi jarayon) | ✅ |

✅ maqsadga yetildi · ❌ yetilmadi · — maqsad belgilanmagan. Vaqt — eng yaxshi takror, xotira — barcha takrorlarning eng kattasi (RSS = ishchi to'plam, private = shaxsiy baytlar; ishchi jarayonlar bilan birga); MB = 10⁶ bayt; tezlik = tahrirlangan hujjat yozuvlarining baytlari / vaqt. Holat «Keyin» ustuniga qarab.

7-bosqich o'zgarishlari: **XLSX** — o'z oqimli yozuvchimiz (`jsonreshape/writer_xlsx.py`, xlsxwriter o'rniga): ishchi jarayonlar kataklarni tayyorlaydi, varaq XML'ini quradi (inline satrlar, `r` manzil atributlarisiz — shu sabab har blokning XML'i oldingi qatorlar sonini bilmasdan quriladi) va uni deflate bilan siqadi (har bo'lak `Z_SYNC_FLUSH` bilan tugaydi); ota jarayon siqilgan bo'laklarni tartib bilan zip'ga qo'shadi va CRC-32'larni birlashtiradi (`crc32_combine`) — oxirida sekin «zip yig'ish» bosqichi yo'q, fayl ikki baravar kichik (217 MB / 399 MB). **SQLite** — `jsonreshape/writer_sqlite.py`: bitta optimistik o'tish (avval map'larni topish o'tishi yo'q: ishchilar har yo'ldagi kalitlarni sanaydi, biror yo'lda 100 dan ortiq kalit bo'lsa — eksport eski usulda, avval skaner bilan qayta bajariladi); ishchilar yozuvlarni jadvallarga bo'lib, xotiradagi SQLite bazasiga yozadi va uni `serialize` qiladi; ota jarayon har bo'lakni biriktirilgan `w` bazasiga `deserialize` qiladi va har jadvalni bitta `INSERT … SELECT` bilan ko'chiradi (`_id`/`_parent` siljitiladi) — Python'dan qator o'tmaydi. CSV kodi o'zgarmagan (nazorat).

Qiymatlar bir xilligi: XLSX — har katak (turi va qiymati), varaqlar, bo'linish, ogohlantirishlar xlsxwriter'li eski eksport bilan solishtirildi (`tests/test_writer_xlsx_fast.py`; bundan tashqari `main` paketi bilan 70 ta XLSX eksporti, 266 136 katak — farq faqat `<r>…</r>` ko'rinishidagi matnda: xlsxwriter uni xom XML qilib yozardi va Excel bunday faylni ochmasdi); Excel'ning o'zi (COM orqali) 7 ta yangi faylni ochdi, 22 833 katakni yozilgandek o'qidi (zip64 yozuvli fayl ham). SQLite — har jadval, `CREATE` matni, indekslar, har qiymat va uning saqlash sinfi bir oqimli eski eksport bilan bir xil (`tests/test_writer_sqlite_pool.py`; `main` paketi bilan 10 ta eksport ham).

«Oldin» — `main` (3742d2f) paket nusxasi (`--pkg`), «Keyin» — shu tarmoq; ikkalasi 2026-10-21 da, bir xil skript va sharoitda, ketma-ket o'lchandi (kompyuter boshqa ishlar bilan qisman band — 6-bosqich o'lchovidan sekinroq: o'shanda XLSX 12.0, SQLite 16.4, CSV 253 MB/s). CSV ikki marta navbatma-navbat (keyin / oldin / keyin / oldin, har biri 3 takror) qo'shimcha o'lchandi: farq shovqin chegarasida. Takrorlar: 2 ta (CSV: 8 ta), eng yaxshisi; 150 s dan uzoq amal bir marta.

## Maqsadga yetilmagan joylar

Yo'q — barcha jadval eksportlari maqsadga yetdi.

- Qolgan cheklov (SQLite): ota jarayonning ishi — bo'laklarni ko'chirish (`INSERT … SELECT`, 2,1 GB faylda ~14 s, ~2,2 mln qator/s) va oxirida `_parent` indekslarini qurish (~4 s) — endi asosiy chegara; ishchilar ~7 s kutiladi. Xotira maqsad ichida, lekin chegaraga yaqin (tahrirlangan faylda 282 MB): ishchi jarayon ~40 MB interpretator va modullar + blok; ota jarayonda 16 MiB SQLite keshi.
- XLSX: ishchilar chegarasi (bitta ishchi ~40 MB/s: JSON o'qish, kataklar, XML, deflate) — 4 ta ishchi bilan ~115 MB/s.

## Kompyuter

- Protsessor: Intel(R) Core(TM) Ultra 9 285, 24 yadro / 24 mantiqiy protsessor
- RAM: 205.6 GB
- Disk, ma'lumot fayli: E: SAMSUNG MZVL81T0HELB-00BH1 (NVMe, SSD, NTFS)
- Disk, chiqishlar va qatlamlar: C: SAMSUNG MZVL81T0HELB-00BH1 (NVMe, SSD, NTFS)
- OS: Windows-11-10.0.26200-SP0; Python 3.14.3; yadro: rust

## O'lchash shartlari

- Har bir amal alohida yangi jarayonda (`bench/edit_bench.py`), har ssenariy o'z JSONRESHAPE_HOME'ida; ochish va qatlamni tiklash alohida o'lchanadi, jadvalda faqat amalning o'zi. Takrorlar: 2 (CSV: 8; juda sekin amal — 150.0 s dan ortig'i — bir marta), eng yaxshisi olinadi.
- Xotira: jarayonning o'zi va uning bola jarayonlari (ishchi jarayonlar) har 20 ms da o'lchandi (RSS va private yig'indisi; bola jarayonlar ro'yxati har 0,5 s da yangilanadi); RSS ga jarayon tegib o'tgan mmap sahifalari ham kiradi.
- Chiqish fayli fsync qilinadi; fsync vaqti alohida o'lchandi (XLSX, CSV). Chiqishlar va qatlamlar C: diskida (agentning vaqtinchalik papkasi).
- Har o'lchovdan oldin tizim CPU yuklamasi ≤ 35.0% bo'lishi kutildi (ko'pi bilan 60.0 s); o'lchovlar paytidagi yuklama (o'lchanayotgan amalning o'zi ham kiradi): o'rtacha 22.1%, eng ko'pi 57.9%.
- «Oldin» — `main` (3742d2f) paketining `git archive` nusxasi (`--pkg`, o'sha Rust yadrosi bilan); «Keyin» — `wip/xlsx-sqlite-speed` tarmog'i.
- Ma'lumot fayli OS keshida (iliq kesh); 205 GB RAM.

## Nimani o'lchab bo'lmadi

- GUI'dagi vaqtlar (kutubxona darajasida o'lchandi; GUI shu amallarni fon vazifalarida chaqiradi).
- §12 dagi 5 GB holat: 2,1 GB fayl ishlatildi (tezlik fayl hajmiga bog'liq emas). 4 GiB dan katta XLSX arxivi (zip64): kichik faylda zip64 yozuvlari majburan yozilib tekshirildi (Python `zipfile`, openpyxl va Excel ochdi).
- Qolgan ssenariylar (100 ta nuqtaviy tahrir, har 3-yozuv, 10% o'chirilgan; CSV avtomatik ustunlar bilan; qatlamni tiklash) qayta o'lchanmadi — ularning kodi o'zgarmagan, raqamlari 6-bosqich hisobotida (`edit-20261019-194807-final.json`, README §14).

Qayta o'lchash: `.venv/Scripts/python.exe bench/edit_bench.py --work DIR --only export_csv:base,export_xlsx:base,export_sqlite:base,export_csv:all,export_xlsx:all,export_sqlite:all [--pkg PAKET] [--label NOM]`.
