# jsonreshape: katta fayl benchmarki (AGENT_PROMPT §12)

Sana: 2026-10-17 16:51:47 · davomiyligi: 21 daq 29 s · rejim: full · to'liq natijalar: `20261017-165147.json`

## §12 maqsadlari

| Amal | Maqsad | Natija | Holat |
|---|---|---|:---:|
| Ochish, birinchi 50 ta yozuv ko'rinishi (syn_2g.json, 2.1 GB) | < 1 s | 11 ms (Document.open 7.6 ms; jarayon ishga tushganidan 131 ms) | ✅ |
| To'liq indeks, Rust yadro (syn_2g.json) | ≥ 150 MB/s | 741 MB/s (2.1 GB, 2.90 s, 6 513 235 ta yozuv; birinchi yozuv 1.5 ms; yadro: rust) | ✅ |
| 1 000 000 bolali tugunni ochish (node_1m.json, ichki massiv) | < 200 ms (virtual) | 79 ms (bolalar skaneri 41.7 MB; fayl ochilishidan 148 ms; oxirgi sahifa 0.2 ms; qayta ochish keshdan 3.9 ms) | ✅ |
| To'liq matn qidiruvi, bayt rejimi: kam uchraydigan so'z «Qoraqalpog'iston» (0.00101% yozuvda) | ≥ 100 MB/s, natijalar darhol chiqa boshlaydi (birinchi natija < 1 s) | 913 MB/s (2.35 s; 66 ta moslik, 66 ta yozuvda; birinchi natija 16 ms; rejim: bayt rejimi) | ✅ |
| To'liq matn qidiruvi, bayt rejimi: ko'p uchraydigan so'z «ko'chasi» (60% yozuvda) | ≥ 100 MB/s, natijalar darhol chiqa boshlaydi (birinchi natija < 1 s) | 287 MB/s (7.49 s; 3 907 316 ta moslik, 3 907 316 ta yozuvda; birinchi natija 14 ms; rejim: bayt rejimi) | ✅ |
| 10 000 000 ta yozuvni bitta maydon (`name`, o'zbekcha tartib) bo'yicha tartiblash va natijani JSONL faylga yozish (syn_10m.jsonl): ochishdan natija fayli to'liq yozilguncha, `jsonreshape sort` kabi | < 2 daqiqa, RAM < 1 GB | 76.2 s, peak RSS 371.6 MB (indeks 5.00 s; tartiblash 21.7 s — tashqi, diskda, 3 ta run; tartiblangan eksport 49.5 s — 66.5 MB/s) | ✅ |
| Eksport JSON → JSONL (syn_2g.json; mapping/tahrirlarsiz) | ≥ 50 MB/s, RAM < 300 MB | 225 MB/s, peak RSS 80.7 MB (9.54 s; chiqish 2.1 GB) | ✅ |
| Eksport JSON → JSON, indent 2 (syn_2g.json; mapping/tahrirlarsiz) | ≥ 50 MB/s, RAM < 300 MB | 246 MB/s, peak RSS 63.3 MB (8.74 s; chiqish 3.7 GB) | ✅ |
| Dasturning umumiy xotirasi (tartiblashdan boshqa amallarning eng katta peak RSS) | < 500 MB | 247.5 MB (eng kattasi: filter_export_10m; 24 ta amal) | ✅ |

✅ maqsadga yetildi · ❌ yetilmadi (sababi va variantlari pastda) · — maqsad belgilanmagan. Vaqt — eng yaxshi takror, peak RSS — barcha takrorlarning eng kattasi; MB = 10⁶ bayt.

## Qo'shimcha o'lchovlar

| Amal | Maqsad | Natija | Holat |
|---|---|---|:---:|
| Ochish, birinchi 50 ta yozuv ko'rinishi (syn_5g.json, 5.4 GB) | < 1 s | 12 ms (Document.open 7.9 ms; jarayon ishga tushganidan 134 ms) | ✅ |
| To'liq indeks, Rust yadro (syn_5g.json) | ≥ 150 MB/s | 668 MB/s (5.4 GB, 8.03 s, 16 222 716 ta yozuv; birinchi yozuv 1.7 ms; yadro: rust) | ✅ |
| To'liq matn qidiruvi, bayt rejimi, syn_5g.json: «Qoraqalpog'iston» (0.001% yozuvda) | ≥ 100 MB/s, natijalar darhol chiqa boshlaydi (birinchi natija < 1 s) | 884 MB/s (6.08 s; 163 ta moslik, 163 ta yozuvda; birinchi natija 12 ms; rejim: bayt rejimi) | ✅ |
| Keshdagi indeks bilan qayta ochish, birinchi 50 ta yozuv | < 1 s | 8.7 ms (Document.open 8.1 ms; 6 513 235 ta yozuv keshdan: ha) | ✅ |
| Xuddi shu fayl avtomatik format bilan (items = yozuvlar yo'li): ochishdan tugun bolalari ko'ringuncha | < 1 s | 271 ms (Document.open 186 ms — bosh qism ijson bilan tahlil qilinadi; ildiz 85 ms — asosiy indeks bilan; tugun 0.2 ms) | ✅ |
| Qidiruv, bayt rejimi: o'rtacha uchraydigan so'z «Toshkent» (5.87% yozuvda) | ≥ 100 MB/s, natijalar darhol chiqa boshlaydi (birinchi natija < 1 s) | 680 MB/s (3.16 s; 382 422 ta moslik, 382 422 ta yozuvda; birinchi natija 12 ms; rejim: bayt rejimi) | ✅ |
| Fayl ochilishi bilan darhol qidiruv «Qoraqalpog'iston» (indeks fonda quriladi) | birinchi natija < 1 s | birinchi natija 12 ms (ochishdan); hammasi 5.63 s; rejim: bayt rejimi; parse qilingan yozuvlar: 49 325 | ✅ |
| 6 513 235 ta yozuvni bitta maydon (`name`, o'zbekcha tartib) bo'yicha tartiblash va natijani JSONL faylga yozish (syn_2g.json): ochishdan natija fayli to'liq yozilguncha, `jsonreshape sort` kabi | < 2 daqiqa, RAM < 1 GB | 47.9 s, peak RSS 372.3 MB (indeks 2.95 s; tartiblash 13.9 s — tashqi, diskda, 2 ta run; tartiblangan eksport 31.1 s — 68.9 MB/s) | ✅ |
| Faqat tartiblash bosqichi (sort_records, natija yozilmaydi): 10 000 000 ta yozuv, `name` bo'yicha (syn_10m.jsonl) | — (amalning bir bosqichi; §12 maqsadi butun amal uchun) | 20.9 s, peak RSS 371.1 MB (157 MB/s; tashqi, diskda, 3 ta run) | — |
| 10 000 000 ta yozuvni son maydon (`created_at`) bo'yicha tartiblash (faqat sort_records, natija yozilmaydi) | — (amalning bir bosqichi; §12 maqsadi butun amal uchun) | 25.1 s, peak RSS 397.5 MB (131 MB/s; tashqi, diskda, 2 ta run) | — |
| 10 000 000 ta yozuvni deyarli takrorlanmaydigan satr maydoni (`contact.phone`) bo'yicha tartiblash (faqat sort_records, natija yozilmaydi) | — (amalning bir bosqichi; §12 maqsadi butun amal uchun) | 34.4 s, peak RSS 373.6 MB (95.7 MB/s; tashqi, diskda, 3 ta run) | — |
| To'liq indeks, Rust yadro, JSON Lines (syn_10m.jsonl) | ≥ 150 MB/s | 676 MB/s (3.3 GB, 4.87 s, 10 000 000 ta yozuv; birinchi yozuv 1.5 ms; yadro: rust) | ✅ |
| Filtr `contact.address.city == "Toshkent"` (strategiya: extract) | — (§12 da yo'q) | 200 MB/s (10.7 s; 382 422 / 6 513 235 ta yozuv) | — |
| Filtr `contact.address.city == "Toshkent"` (strategiya: parse) | — (§12 da yo'q) | 114 MB/s (18.9 s; 382 422 / 6 513 235 ta yozuv) | — |
| Filtr `contact.address.city == "Toshkent"` va mos yozuvlarni JSONL faylga yozish (syn_2g.json): ochishdan natija fayli to'liq yozilguncha, `jsonreshape filter` kabi | — (§12 da yo'q) | 15.1 s, kirish bo'yicha 142 MB/s, peak RSS 185.1 MB (indeks 2.93 s; filtr 10.7 s — extract; eksport 1.47 s — 382 422 / 6 513 235 ta yozuv, chiqish 126.1 MB) | — |
| Filtr `contact.address.city == "Toshkent"` va mos yozuvlarni JSONL faylga yozish (syn_10m.jsonl): ochishdan natija fayli to'liq yozilguncha, `jsonreshape filter` kabi | — (§12 da yo'q) | 24.8 s, kirish bo'yicha 133 MB/s, peak RSS 247.5 MB (indeks 5.01 s; filtr 17.3 s — extract; eksport 2.52 s — 587 025 / 10 000 000 ta yozuv, chiqish 193.7 MB) | — |
| Eksport JSON → CSV, avtomatik ustunlar (massivlar katakda JSON matni) | ≥ 50 MB/s, RAM < 300 MB | 26.3 MB/s, peak RSS 107.1 MB (81.5 s; chiqish 1.4 GB) | ❌ |
| Eksport JSON → CSV, faqat 9 ta skalyar ustun | ≥ 50 MB/s, RAM < 300 MB | 52.7 MB/s, peak RSS 100.1 MB (40.7 s; chiqish 659.1 MB) | ✅ |
| To'liq indeks, sof Python yadro (zaxira) | ≥ 15 MB/s (ARCHITECTURE §1) | 72.2 MB/s (2.1 GB, 29.7 s, 6 513 235 ta yozuv; birinchi yozuv 150 ms; yadro: python) | ✅ |
| Tuzilma (schema), 10 000 ta yozuvli namuna | — (§12 da yo'q) | 167 ms (10 000 / 6 513 235 ta yozuv, 25 ta maydon) | — |
| Sovuq fayl (OS keshida yo'q): ochish, birinchi 50 ta yozuv | < 1 s | 31 ms (Document.open 27 ms); ochish + indeks paytida diskdan o'qildi: 2.1 GB | ✅ |
| Sovuq fayl: to'liq indeks (Rust) | ≥ 150 MB/s | 777 MB/s (2.1 GB, 2.76 s); diskdan o'qildi: 2.1 GB | ✅ |
| Disk: buferlanmagan ketma-ket o'qish / o'qish + Scanner | — (§12 da yo'q) | 2 980 MB/s xom o'qish; 787 MB/s o'qish + Scanner (diskdan 2.1 GB) | — |

## Maqsadga yetilmagan joylar

- **Eksport JSON → CSV, avtomatik ustunlar (massivlar katakda JSON matni)** — 26.3 MB/s, peak RSS 107.1 MB (81.5 s; chiqish 1.4 GB) (maqsad: ≥ 50 MB/s, RAM < 300 MB).
  - Sabab: CSV eksport har bir yozuvni parse qiladi (codec.loads) va har bir katakni Python'da hosil qiladi; avtomatik ustunlarda orders/children kabi obyektlar massivi har katakda ixcham JSON matniga qayta yoziladi, so'ng csv moduli yozadi (cProfile: vaqtning katta qismi katak qurishda, keyin csv.writerows va parse). Faqat skalyar ustunlar bilan: 52.7 MB/s.
  - Variantlar: 1) faqat kerakli skalyar ustunlarni tanlash (52.7 MB/s, o'lchandi); 2) qator qurishni Rust yadroga o'tkazish (PathExtractor spanlari + xom tokenlarni nusxalash, konteynerlar uchun Reformatter) — Python obyektlari yaratilmaydi; 3) bloklarni parallel jarayonlarda tayyorlab, tartib bilan yozish.

## Ma'lumot fayllari (bench/make_synthetic.py, deterministik)

- `node_1m.json`: 41.7 MB, 1 000 000 ta yozuv (avval yaratilgan)
- `syn_10m.jsonl`: 3.3 GB, 10 000 000 ta yozuv (avval yaratilgan)
- `syn_2g.json`: 2.1 GB, 6 513 235 ta yozuv (avval yaratilgan)
- `syn_5g.json`: 5.4 GB, 16 222 716 ta yozuv (avval yaratilgan)

## Kompyuter

- Protsessor: Intel(R) Core(TM) Ultra 9 285, 24 yadro / 24 mantiqiy protsessor (8 ta P + 16 ta E yadro)
- RAM: 205.6 GB
- Ma'lumotlar diski: SAMSUNG MZVL81T0HELB-00BH1 (NVMe, SSD), E: NTFS, bo'sh joy 343.5 GB
- OS: Windows-11-10.0.26200-SP0; quvvat rejasi: Balanced
- Python 3.14.3; jsonreshape 0.1.0, yadro: rust; orjson 3.13.0, ijson 3.6.0 (yajl2_c)

## O'lchash shartlari

- Har bir amal alohida yangi jarayonda, yangi bo'sh JSONRESHAPE_HOME bilan bajarildi (kesh indeksi yo'q; faqat «qayta ochish» oldingi indeksni ishlatadi). Qidiruv, filtr, tartiblash, eksport va tuzilma o'sha jarayonda avval indeks quradi; jadvaldagi vaqt — faqat amalning o'zi (indeks vaqti JSON'da alohida).
- Istisno — foydalanuvchi oladigan natijani o'lchaydigan qatorlar: §12 dagi tartiblash qatori va «filtr … va yozish» qatorlari `jsonreshape sort` / `jsonreshape filter` kabi butun yo'lni o'lchaydi — Document.open, indeks, sort_records / filter_records va natija RecordSet'ini JSONL faylga yozish, fayl to'liq yozilguncha (AGENT_PROMPT §6.4: tartiblash natijasi shu tartibda eksport qilinadi). Tartiblangan eksport yozuvlarni kalit tartibida, ya'ni faylning tasodifiy joylaridan o'qiydi, shuning uchun fayl tartibidagi eksport uning o'rnini bosa olmaydi; faqat sort_records vaqti qo'shimcha jadvalda.
- Peak RSS — jarayonning eng katta ishchi to'plami (Windows peak_wset). Windows'da venv'ning python.exe fayli faqat ishga tushiruvchi, haqiqiy interpretator uning bola jarayoni, shuning uchun butun jarayonlar daraxti psutil bilan har 20 ms kuzatildi va har jarayonning handle'i ochiq tutilib, tugagach aniq qiymat olindi; amal jarayoni ham o'z peak'ini xabar qiladi. Ikki mustaqil o'lchov farqi: ko'pi bilan 0.0%. Jarayon tegib o'tgan mmap indeks sahifalari ham kiradi (Vazifalar dispetcheridagidek).
- Takrorlar: amalga qarab 1/2/3/5 marta; jadvalda eng yaxshi takror.
- Har o'lchovdan oldin tizim CPU yuklamasi tekshirildi: ≤ 35.0% bo'lguncha, ko'pi bilan 60.0 s kutildi. Ko'rilgan yuklama: 27.8–34.9% (o'rtacha 31.3%); 0 marta kutish muddati tugab, o'lchov baribir bajarildi; jami kutish 101.0 s.
- O'lchovlar paytida boshqa jarayonlar band qilgan protsessor: o'rtacha 7.7, ko'pi bilan 10.5 yadro (24 tadan).
- OS fayl keshi: kompyuterda 205.6 GB RAM bor, ma'lumot fayllari keshda turadi — jadvaldagi natijalar iliq keshda olingan (faylni o'qish diskka bormaydi).
- Bitta sovuq o'qish o'lchandi: fayl FILE_FLAG_NO_BUFFERING bilan nusxalandi (nusxa OS keshiga tushmaydi), shu nusxa ochildi va indekslandi; disk hisoblagichi 2.1 GB o'qilganini ko'rsatdi (fayl 2.1 GB) — ma'lumot haqiqatan diskdan kelgan.
- §12 maqsadlari 5 GB massiv fayl uchun; bu yerda 2.1 GB massiv (6 513 235 ta yozuv) va 10 000 000 ta yozuvli 3.3 GB JSONL ishlatildi. Tezlik (MB/s) streaming tufayli fayl hajmiga bog'liq emas, ochish faqat faylning boshini o'qiydi, xotira esa hajmga deyarli bog'liq emas (indeks diskda, mmap).
- §12 dagi 5 GB holatni to'g'ridan-to'g'ri tekshirish uchun qo'shimcha jadvalda 5.4 GB massivda (16 222 716 ta yozuv) ochish, indeks va qidiruv ham o'lchandi.
- Tartiblash: `name` maydonida atigi ~286 xil qiymat bor (make_synthetic: 26 ism × 11 familiya), shuning uchun kalitlarni kodlash keshi ko'p ishlaydi; deyarli takrorlanmaydigan kalitlar uchun qo'shimcha jadvaldagi `created_at` (son) va `contact.phone` (satr) qatorlariga qarang.
- §12 dagi eksport maqsadi mapping yoki tahrirlar bilan eksport uchun; ular keyingi bosqichlarda qo'shiladi, shuning uchun hozir oddiy JSON → JSONL va JSON → JSON (indent 2) o'lchandi.
- Gibrid protsessor (8 ta P + 16 ta E yadro): affinity o'rnatilmadi, yadroni Windows tanlaydi; E yadroga tushgan takror sekinroq bo'ladi, shuning uchun eng yaxshi takror olinadi.

## Nimani o'lchab bo'lmadi

- §12 «oddiy noutbuk» uchun yozilgan; o'lchov ish stoli protsessorida (Intel(R) Core(TM) Ultra 9 285) bajarildi — noutbukda CPU'ga bog'liq amallar sekinroq bo'lishi kutiladi.
- Hamma amallarni sovuq keshda o'lchab bo'lmadi: butun tizim keshini tozalash administrator huquqini talab qiladi va boshqa ishlayotgan dasturlarga ta'sir qiladi; faqat bitta sovuq ochish va indekslash o'lchandi (yuqorida).
- GUI hali yo'q (2-bosqich): «birinchi yozuvlar ko'rinishi» kutubxona darajasida o'lchandi (Document.open + daraxt tugunlari + qiymatlar), Qt chizish vaqti kirmaydi.
- Mapping yoki tahrirlar bilan eksport (§12 dagi 6-qator) hali o'lchab bo'lmaydi: bu imkoniyatlar 3- va 4-bosqichlarda.

Qayta o'lchash: `.venv/Scripts/python.exe bench/run_bench.py` (to'liq) yoki `--subset 2g` (§14 25-test to'plami); bitta amal: `--only NOM` (`--list`).
