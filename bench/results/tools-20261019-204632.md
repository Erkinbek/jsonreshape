# jsonreshape: 5-bosqich asboblari benchmarki (AGENT_PROMPT §9–§10, §12)

Sana: 2026-10-19 20:46:32 · davomiyligi: 12 daq 8 s · rejim: full · to'liq natijalar: `tools-20261019-204632.json`

## Asboblar (syn_2g.json, 2.1 GB)

| Amal | Maqsad | Natija | Holat |
|---|---|---|:---:|
| Statistika, 1 ta maydon: `stats --field contact.address.city` | dastur < 500 MB (§12); tezlik — §12 da yo'q | 556 MB/s (3.87 s; jami 6.56 s, shundan indeks 2.69 s); RAM 80.6 MB; contact.address.city: 17 xil, 6 513 235 ta qiymat | ✅ |
| Statistika, 4 ta maydon: `stats --field name --field score --field contact.address.city --field "orders[*].sum"` | dastur < 500 MB (§12); tezlik — §12 da yo'q | 124 MB/s (17.3 s; jami 20.0 s, shundan indeks 2.65 s); RAM 104.6 MB; name: 286 xil, 6 513 235 ta qiymat; score: 100 001 xil, 6 513 235 ta qiymat; contact.address.city: 17 xil, 6 513 235 ta qiymat; orders[*].sum: 50 001 xil, 9 774 338 ta qiymat | ✅ |
| Statistika, aniq hisoblash (deyarli takrorlanmaydigan maydon): `stats --field contact.phone --exact` | dastur < 500 MB (§12); tezlik — §12 da yo'q | 108 MB/s (19.9 s; jami 22.6 s, shundan indeks 2.67 s); RAM 196.8 MB; contact.phone: 4 636 210 xil, 6 513 235 ta qiymat | ✅ |
| Dublikatlar: `dupes --key contact.phone` | dastur < 500 MB (§12); tezlik — §12 da yo'q | 170 MB/s (12.7 s; jami 15.4 s, shundan indeks 2.70 s); RAM 262.8 MB; 1 476 797 ta guruh, 1 877 025 ta dublikat | ✅ |
| JSON Schema yaratish, barcha yozuvlardan: `schema IN OUT` | dastur < 500 MB (§12); tezlik — §12 da yo'q | 42.5 MB/s (50.5 s; jami 53.1 s, shundan indeks 2.63 s); RAM 80.9 MB; sxema 5.3 KB | ✅ |
| JSON Schema bo'yicha tekshirish: `validate --schema syn_2g.schema.json` | dastur < 500 MB (§12); tezlik — §12 da yo'q | 74.0 MB/s (29.0 s; jami 31.7 s, shundan indeks 2.67 s); RAM 80.9 MB; 6 513 235 ta yozuv tekshirildi, 0 ta xato | ✅ |
| Bo'lish, 4 qism, bayt nusxa (o'sha format): `split --parts 4` | dastur < 500 MB (§12); tezlik — §12 da yo'q | 758 MB/s (2.83 s; jami 5.50 s, shundan indeks 2.67 s); RAM 64.8 MB; 4 ta qism, 6 513 235 ta yozuv, 2.1 GB | ✅ |
| Bo'lish, 4 qism, JSON Lines'ga: `split --parts 4 --to jsonl` | dastur < 500 MB (§12); tezlik — §12 da yo'q | 288 MB/s (7.46 s; jami 10.2 s, shundan indeks 2.72 s); RAM 101.8 MB; 4 ta qism, 6 513 235 ta yozuv, 2.1 GB | ✅ |
| Birlashtirish, dublikatlarni tozalash bilan (syn_2g.json + uning yangi versiyasi, 4.3 GB): `merge A B -o OUT --dedupe-by id` | dastur < 500 MB (§12); tezlik — §12 da yo'q | 117 MB/s (36.7 s; jami 42.3 s, shundan indeks 5.58 s); RAM 264.3 MB; 6 519 748 ta yozuv yozildi, 6 506 702 ta dublikat tashlandi, 3.7 GB | ✅ |
| Ajratish, har yozuvdan yig'ish: `extract IN OUT.jsonl --gather "orders[*]"` | dastur < 500 MB (§12); tezlik — §12 da yo'q | 383 MB/s (5.61 s; jami 8.28 s, shundan indeks 2.67 s); RAM 87.0 MB; 9 774 338 ta qiymat, 386.2 MB | ✅ |
| Ta'mirlash, buzilmagan fayl: `repair IN OUT` | dastur < 500 MB (§12); tezlik — §12 da yo'q | 335 MB/s (6.41 s; indeks kerak emas); RAM 101.6 MB; 6 513 235 ta yozuv, 0 ta tuzatish, 2.1 GB | ✅ |
| Ta'mirlash, yozuvlarning 1 % i buzilgan (NaN, ortiqcha vergul, bitta tirnoq, tirnoqsiz kalit, izoh): `repair IN OUT` | dastur < 500 MB (§12); tezlik — §12 da yo'q | 217 MB/s (9.92 s; indeks kerak emas); RAM 68.7 MB; 6 513 235 ta yozuv, 78 160 ta tuzatish, 2.1 GB | ✅ |
| Solishtirish kalit bo'yicha (2.1 GB va uning yangi versiyasi): `diff A B --key id --summary` | dastur < 500 MB (§12); tezlik — §12 da yo'q | 99.6 MB/s (43.1 s; jami 48.7 s, shundan indeks 5.55 s); RAM 402.4 MB; 6 441 635 ta bir xil, 65 067 ta o'zgargan, 6 513 ta qo'shilgan, 6 533 ta o'chirilgan | ✅ |
| Solishtirish o'rin bo'yicha (2.1 GB, 1 % yozuv o'zgargan): `diff A B --by-position --summary` | dastur < 500 MB (§12); tezlik — §12 da yo'q | 203 MB/s (21.2 s; jami 26.7 s, shundan indeks 5.49 s); RAM 96.7 MB; 6 448 102 ta bir xil, 65 133 ta o'zgargan, 0 ta qo'shilgan, 0 ta o'chirilgan | ✅ |
| Solishtirish mazmun bo'yicha tekislab (~190 MiB fayllar: kalitsiz tekislash chegarasi 200 MiB): `diff A B --align --summary` | dastur < 500 MB (§12); tezlik — §12 da yo'q | 178 MB/s (2.24 s; jami 2.81 s, shundan indeks 570 ms); RAM 169.4 MB; 602 333 ta bir xil, 6 085 ta o'zgargan, 608 ta qo'shilgan, 610 ta o'chirilgan | ✅ |

✅ maqsadga yetildi · ❌ yetilmadi (sababi va variantlari pastda) · — maqsad belgilanmagan. Vaqt — eng yaxshi takror: buyruqning indeksdan keyingi qismi (tezlik = kirish fayl(lar)i baytlari / shu vaqt); «jami» — buyruq butunligicha, indeks bilan. RAM — barcha takrorlarning eng kattasi, butun dastur (ishchi jarayonlar bilan); MB = 10⁶ bayt.

## Maqsadga yetilmagan joylar

Barcha maqsadlarga yetildi.

## Ma'lumot fayllari (bench/make_synthetic.py, deterministik)

- `syn_2g.json`: 2.1 GB, 6 513 235 ta yozuv (avval yaratilgan)
- `syn_2g_changed.json`: 2.1 GB
- `syn_2g_inplace.json`: 2.1 GB
- `syn_2g_broken1.json`: 2.1 GB
- `syn_190m_a.json`: 199.2 MB
- `syn_190m_b.json`: 199.2 MB

## Kompyuter

- Protsessor: Intel(R) Core(TM) Ultra 9 285, 24 yadro / 24 mantiqiy protsessor (8 ta P + 16 ta E yadro)
- RAM: 205.6 GB
- Ma'lumotlar diski: SAMSUNG MZVL81T0HELB-00BH1 (NVMe, SSD), E: NTFS, bo'sh joy 376.3 GB
- OS: Windows-11-10.0.26200-SP0; quvvat rejasi: Balanced
- Python 3.14.3; jsonreshape 0.1.0, yadro: rust; orjson 3.13.0, ijson 3.6.0 (yajl2_c)

## O'lchash shartlari

- Har bir buyruq alohida yangi jarayonda, yangi bo'sh JSONRESHAPE_HOME bilan (kesh indeksi yo'q), foydalanuvchi ishga tushirgandek: `bench/ops.py cli` jarayon ichida `jsonreshape.cli.main` ni chaqiradi (Document.open, indeks, amal, xulosa; ishchi jarayonlarni dastur o'zi tanlaydi). Indeks vaqti `Document.build_index` dan o'lchanadi (diff va merge'da ikkala fayl).
- RAM — butun dastur: har jarayonning aniq peak ishchi to'plami va jarayonlar daraxtining bir paytdagi yig'indisi (har 20 ms namuna; venv ishga tushiruvchisi kirmaydi) — qaysi biri katta bo'lsa (run_bench.program_peak). Jarayon tegib o'tgan mmap sahifalari (indeks, fayl) ham kiradi.
- Takrorlar: 2 (bir martalik yugurish 400 s dan oshsa — bir marta); eng yaxshisi olinadi.
- Har o'lchovdan oldin tizim CPU yuklamasi ≤ 35.0% bo'lguncha kutildi (ko'pi bilan 60.0 s); ko'rilgan yuklama: 1.5–8.6%. O'lchovlar paytida kompyuterda boshqa og'ir ish yo'q edi.
- Tayyorlangan kirishlar (o'lchanmaydi, bir marta, ish papkasida): yangi versiya — har 100-yozuvning `score` qiymati o'zgargan, har 997-yozuv o'chirilgan, har 1000-yozuv oldiga yangi yozuv qo'shilgan; o'rin bo'yicha solishtirish uchun — faqat `score` o'zgargan; ta'mirlash uchun — har 100-yozuv buzilgan (5 xil buzilish navbat bilan); tekislab solishtirish uchun — syn_2g.json'ning birinchi ~190 MiB i va uning yangi versiyasi.
- Ma'lumot fayllari OS keshida (iliq kesh; 205.6 GB RAM). Chiqishlar va tayyorlangan kirishlar — E: diskida.

## Nimani o'lchab bo'lmadi

- §12 «oddiy noutbuk» uchun yozilgan; o'lchov ish stoli protsessorida (Intel(R) Core(TM) Ultra 9 285) bajarildi — noutbukda CPU'ga bog'liq amallar sekinroq bo'lishi kutiladi.
- GUI'dagi vaqtlar (asboblar oynalari shu funksiyalarni fon vazifalarida chaqiradi).

Qayta o'lchash: `.venv/Scripts/python.exe bench/tools_bench.py --work-dir PAPKA` (to'liq) yoki `--only NOM` (`--list`); faqat hisobot: `--report NATIJA.json`.
