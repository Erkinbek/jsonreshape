# jsonreshape: katta fayl benchmarki (AGENT_PROMPT §12)

Sana: 2026-10-19 19:09:28 · davomiyligi: 25 daq 52 s · rejim: full · to'liq natijalar: `20261019-190928.json`

## §12 maqsadlari

| Amal | Maqsad | Natija | Holat |
|---|---|---|:---:|
| Ochish, birinchi 50 ta yozuv ko'rinishi (syn_2g.json, 2.1 GB) | < 1 s | 11 ms (Document.open 7.9 ms; jarayon ishga tushganidan 128 ms) | ✅ |
| To'liq indeks, Rust yadro (syn_2g.json) | ≥ 150 MB/s | 725 MB/s (2.1 GB, 2.96 s, 6 513 235 ta yozuv; birinchi yozuv 2.0 ms; yadro: rust) | ✅ |
| 1 000 000 bolali tugunni ochish (node_1m.json, ichki massiv) | < 200 ms (virtual) | 95 ms (bolalar skaneri 41.7 MB; fayl ochilishidan 172 ms; oxirgi sahifa 0.2 ms; qayta ochish keshdan 4.3 ms) | ✅ |
| To'liq matn qidiruvi, bayt rejimi: kam uchraydigan so'z «Qoraqalpog'iston» (0.00101% yozuvda) | ≥ 100 MB/s, natijalar darhol chiqa boshlaydi (birinchi natija < 1 s) | 745 MB/s (2.88 s; 66 ta moslik, 66 ta yozuvda; birinchi natija 11 ms; rejim: bayt rejimi) | ✅ |
| To'liq matn qidiruvi, bayt rejimi: ko'p uchraydigan so'z «ko'chasi» (60% yozuvda) | ≥ 100 MB/s, natijalar darhol chiqa boshlaydi (birinchi natija < 1 s) | 272 MB/s (7.91 s; 3 907 316 ta moslik, 3 907 316 ta yozuvda; birinchi natija 14 ms; rejim: bayt rejimi) | ✅ |
| 10 000 000 ta yozuvni bitta maydon (`name`, o'zbekcha tartib) bo'yicha tartiblash va natijani JSONL faylga yozish (syn_10m.jsonl): ochishdan natija fayli to'liq yozilguncha, `jsonreshape sort` kabi | < 2 daqiqa, RAM < 1 GB | 77.6 s, peak RSS 378.5 MB (indeks 5.05 s; tartiblash 22.9 s — tashqi, diskda, 3 ta run; tartiblangan eksport 49.6 s — 66.3 MB/s) | ✅ |
| Eksport JSON → JSONL (syn_2g.json; mapping/tahrirlarsiz) | ≥ 50 MB/s, RAM < 300 MB | 180 MB/s, peak RSS 88.7 MB (11.9 s; chiqish 2.1 GB) | ✅ |
| Eksport JSON → JSON, indent 2 (syn_2g.json; mapping/tahrirlarsiz) | ≥ 50 MB/s, RAM < 300 MB | 214 MB/s, peak RSS 70.9 MB (10.1 s; chiqish 3.7 GB) | ✅ |
| Mapping bilan eksport: `jsonreshape apply` (6 qadamli mapping), syn_2g.json → JSON (indent 2); ishchi jarayonlar: avtomatik (3 ta) | ≥ 50 MB/s, RAM < 300 MB | 97.5 MB/s, RAM 263.7 MB (4 ta jarayon birga, private 242.2 MB; bittasining eng kattasi 90.7 MB) (22.0 s; 6 513 235 ta element, chiqish 3.3 GB; apply bosqichi 97.9 MB/s; jami 24.7 s, shundan indeks 2.71 s) | ✅ |
| Dasturning umumiy xotirasi (tartiblashdan boshqa amallarning eng katta peak RSS, ishchi jarayonlari bilan birga) | < 500 MB | 298.9 MB (eng kattasi: flatten_2g; 31 ta amal) | ✅ |

✅ maqsadga yetildi · ❌ yetilmadi (sababi va variantlari pastda) · — maqsad belgilanmagan. Vaqt — eng yaxshi takror, peak RSS — barcha takrorlarning eng kattasi; MB = 10⁶ bayt.

## Qo'shimcha o'lchovlar

| Amal | Maqsad | Natija | Holat |
|---|---|---|:---:|
| Mapping bilan eksport: `jsonreshape apply` (6 qadamli mapping), syn_2g.json → JSON (indent 2); ishchi jarayonlar: 1 (--workers 1) | ≥ 50 MB/s, RAM < 300 MB | 47.5 MB/s, RAM 101.2 MB (45.2 s; 6 513 235 ta element, chiqish 3.3 GB; apply bosqichi 47.6 MB/s; jami 48.1 s, shundan indeks 2.94 s) | ❌ |
| Mapping bilan eksport: `jsonreshape apply` (6 qadamli mapping), syn_2g.json → JSON (indent 2); ishchi jarayonlar: 3 (--workers 3) | ≥ 50 MB/s, RAM < 300 MB | 92.5 MB/s, RAM 265.0 MB (4 ta jarayon birga, private 242.0 MB; bittasining eng kattasi 91.6 MB) (23.2 s; 6 513 235 ta element, chiqish 3.3 GB; apply bosqichi 92.9 MB/s; jami 26.1 s, shundan indeks 2.88 s) | ✅ |
| Mapping bilan eksport: `jsonreshape apply` (6 qadamli mapping), syn_2g.json → JSONL; ishchi jarayonlar: avtomatik (3 ta) | ≥ 50 MB/s, RAM < 300 MB | 101 MB/s, RAM 257.8 MB (4 ta jarayon birga, private 238.4 MB; bittasining eng kattasi 84.4 MB) (21.2 s; 6 513 235 ta element, chiqish 2.0 GB; apply bosqichi 102 MB/s; jami 23.9 s, shundan indeks 2.77 s) | ✅ |
| Rekursiv tekislash bilan eksport: `jsonreshape flatten --children-key children`, syn_2g.json → JSON (indent 2); ishchi jarayonlar: avtomatik (3 ta) | ≥ 50 MB/s, RAM < 300 MB | 129 MB/s, RAM 298.9 MB (4 ta jarayon birga, private 246.4 MB; bittasining eng kattasi 133.2 MB) (16.6 s; 6 513 235 ta element, chiqish 3.7 GB; apply bosqichi 132 MB/s; jami 19.6 s, shundan indeks 3.04 s) | ✅ |
| Rekursiv tekislash bilan eksport: `jsonreshape flatten --children-key children`, syn_2g.json → JSON (indent 2); ishchi jarayonlar: 1 (--workers 1) | ≥ 50 MB/s, RAM < 300 MB | 46.0 MB/s, RAM 141.3 MB (46.7 s; 6 513 235 ta element, chiqish 3.7 GB; apply bosqichi 46.3 MB/s; jami 49.7 s, shundan indeks 2.94 s) | ❌ |
| Rekursiv tekislash bilan eksport: `jsonreshape flatten --children-key children`, syn_2g.json → JSON (indent 2); ishchi jarayonlar: 3 (--workers 3) | ≥ 50 MB/s, RAM < 300 MB | 127 MB/s, RAM 293.4 MB (4 ta jarayon birga, private 239.9 MB; bittasining eng kattasi 129.3 MB) (17.0 s; 6 513 235 ta element, chiqish 3.7 GB; apply bosqichi 129 MB/s; jami 19.8 s, shundan indeks 2.88 s) | ✅ |
| Ochish, birinchi 50 ta yozuv ko'rinishi (syn_5g.json, 5.4 GB) | < 1 s | 12 ms (Document.open 8.1 ms; jarayon ishga tushganidan 141 ms) | ✅ |
| To'liq indeks, Rust yadro (syn_5g.json) | ≥ 150 MB/s | 634 MB/s (5.4 GB, 8.47 s, 16 222 716 ta yozuv; birinchi yozuv 1.5 ms; yadro: rust) | ✅ |
| To'liq matn qidiruvi, bayt rejimi, syn_5g.json: «Qoraqalpog'iston» (0.001% yozuvda) | ≥ 100 MB/s, natijalar darhol chiqa boshlaydi (birinchi natija < 1 s) | 854 MB/s (6.28 s; 163 ta moslik, 163 ta yozuvda; birinchi natija 12 ms; rejim: bayt rejimi) | ✅ |
| Keshdagi indeks bilan qayta ochish, birinchi 50 ta yozuv | < 1 s | 9.2 ms (Document.open 8.6 ms; 6 513 235 ta yozuv keshdan: ha) | ✅ |
| Xuddi shu fayl avtomatik format bilan (items = yozuvlar yo'li): ochishdan tugun bolalari ko'ringuncha | < 1 s | 279 ms (Document.open 180 ms — bosh qism ijson bilan tahlil qilinadi; ildiz 99 ms — asosiy indeks bilan; tugun 0.2 ms) | ✅ |
| Qidiruv, bayt rejimi: o'rtacha uchraydigan so'z «Toshkent» (5.87% yozuvda) | ≥ 100 MB/s, natijalar darhol chiqa boshlaydi (birinchi natija < 1 s) | 621 MB/s (3.46 s; 382 422 ta moslik, 382 422 ta yozuvda; birinchi natija 12 ms; rejim: bayt rejimi) | ✅ |
| Fayl ochilishi bilan darhol qidiruv «Qoraqalpog'iston» (indeks fonda quriladi) | birinchi natija < 1 s | birinchi natija 12 ms (ochishdan); hammasi 5.74 s; rejim: bayt rejimi; parse qilingan yozuvlar: 58 999 | ✅ |
| 6 513 235 ta yozuvni bitta maydon (`name`, o'zbekcha tartib) bo'yicha tartiblash va natijani JSONL faylga yozish (syn_2g.json): ochishdan natija fayli to'liq yozilguncha, `jsonreshape sort` kabi | < 2 daqiqa, RAM < 1 GB | 54.9 s, peak RSS 377.4 MB (indeks 2.99 s; tartiblash 15.3 s — tashqi, diskda, 2 ta run; tartiblangan eksport 36.6 s — 58.5 MB/s) | ✅ |
| Faqat tartiblash bosqichi (sort_records, natija yozilmaydi): 10 000 000 ta yozuv, `name` bo'yicha (syn_10m.jsonl) | — (amalning bir bosqichi; §12 maqsadi butun amal uchun) | 20.9 s, peak RSS 375.1 MB (157 MB/s; tashqi, diskda, 3 ta run) | — |
| 10 000 000 ta yozuvni son maydon (`created_at`) bo'yicha tartiblash (faqat sort_records, natija yozilmaydi) | — (amalning bir bosqichi; §12 maqsadi butun amal uchun) | 26.0 s, peak RSS 400.7 MB (127 MB/s; tashqi, diskda, 2 ta run) | — |
| 10 000 000 ta yozuvni deyarli takrorlanmaydigan satr maydoni (`contact.phone`) bo'yicha tartiblash (faqat sort_records, natija yozilmaydi) | — (amalning bir bosqichi; §12 maqsadi butun amal uchun) | 35.3 s, peak RSS 376.0 MB (93.3 MB/s; tashqi, diskda, 3 ta run) | — |
| To'liq indeks, Rust yadro, JSON Lines (syn_10m.jsonl) | ≥ 150 MB/s | 666 MB/s (3.3 GB, 4.94 s, 10 000 000 ta yozuv; birinchi yozuv 1.7 ms; yadro: rust) | ✅ |
| Filtr `contact.address.city == "Toshkent"` (strategiya: extract) | — (§12 da yo'q) | 207 MB/s (10.4 s; 382 422 / 6 513 235 ta yozuv) | — |
| Filtr `contact.address.city == "Toshkent"` (strategiya: parse) | — (§12 da yo'q) | 102 MB/s (21.0 s; 382 422 / 6 513 235 ta yozuv) | — |
| Filtr `contact.address.city == "Toshkent"` va mos yozuvlarni JSONL faylga yozish (syn_2g.json): ochishdan natija fayli to'liq yozilguncha, `jsonreshape filter` kabi | — (§12 da yo'q) | 16.9 s, kirish bo'yicha 127 MB/s, peak RSS 189.3 MB (indeks 2.97 s; filtr 12.2 s — extract; eksport 1.70 s — 382 422 / 6 513 235 ta yozuv, chiqish 126.1 MB) | — |
| Filtr `contact.address.city == "Toshkent"` va mos yozuvlarni JSONL faylga yozish (syn_10m.jsonl): ochishdan natija fayli to'liq yozilguncha, `jsonreshape filter` kabi | — (§12 da yo'q) | 26.4 s, kirish bo'yicha 125 MB/s, peak RSS 251.8 MB (indeks 5.15 s; filtr 18.5 s — extract; eksport 2.70 s — 587 025 / 10 000 000 ta yozuv, chiqish 193.7 MB) | — |
| Eksport JSON → CSV, avtomatik ustunlar (massivlar katakda JSON matni) | ≥ 50 MB/s, RAM < 300 MB | 105 MB/s, peak RSS 226.4 MB (9 ta jarayon birga, private 175.4 MB; bittasining eng kattasi 65.8 MB) (20.5 s; chiqish 1.4 GB) | ✅ |
| Eksport JSON → CSV, faqat 9 ta skalyar ustun | ≥ 50 MB/s, RAM < 300 MB | 224 MB/s, peak RSS 221.7 MB (5 ta jarayon birga, private 167.4 MB; bittasining eng kattasi 64.8 MB) (9.59 s; chiqish 659.1 MB) | ✅ |
| To'liq indeks, sof Python yadro (zaxira) | ≥ 15 MB/s (ARCHITECTURE §1) | 70.8 MB/s (2.1 GB, 30.3 s, 6 513 235 ta yozuv; birinchi yozuv 51 ms; yadro: python) | ✅ |
| Tuzilma (schema), 10 000 ta yozuvli namuna | — (§12 da yo'q) | 176 ms (10 000 / 6 513 235 ta yozuv, 25 ta maydon) | — |
| Sovuq fayl (OS keshida yo'q): ochish, birinchi 50 ta yozuv | < 1 s | 36 ms (Document.open 32 ms); ochish + indeks paytida diskdan o'qildi: 2.1 GB | ✅ |
| Sovuq fayl: to'liq indeks (Rust) | ≥ 150 MB/s | 805 MB/s (2.1 GB, 2.67 s); diskdan o'qildi: 2.1 GB | ✅ |
| Disk: buferlanmagan ketma-ket o'qish / o'qish + Scanner | — (§12 da yo'q) | 3 037 MB/s xom o'qish; 782 MB/s o'qish + Scanner (diskdan 2.1 GB) | — |

## Maqsadga yetilmagan joylar

- **Mapping bilan eksport: `jsonreshape apply` (6 qadamli mapping), syn_2g.json → JSON (indent 2); ishchi jarayonlar: 1 (--workers 1)** — 47.5 MB/s, RAM 101.2 MB (45.2 s; 6 513 235 ta element, chiqish 3.3 GB; apply bosqichi 47.6 MB/s; jami 48.1 s, shundan indeks 2.94 s) (maqsad: ≥ 50 MB/s, RAM < 300 MB).
  - Sabab: bitta jarayonda har yozuv Python'da parse qilinadi (codec.loads), kompilyatsiya qilingan mapping funksiyalari bilan qayta quriladi va codec.dumps bilan yoziladi: 47.5 MB/s. Shu sababli 64 MB dan katta fayllarda `apply` / `flatten` o'zi ishchi jarayonlarni ishlatadi (standart: 97.5 MB/s, 3 ta ishchi jarayon bilan).
  - Variantlar: 1) standart avtomatik ishchilar (yuqoridagi qator: maqsaddan tez); 2) mapping baholashni Rust yadroga o'tkazish (yozuvlar baytlaridan to'g'ridan-to'g'ri) — eng katta yutuq, katta ish; 3) oddiy ko'chirish/o'chirish/nomlash mapping'larida yozuvni parse qilmasdan bayt darajasida qayta yozish.
- **Rekursiv tekislash bilan eksport: `jsonreshape flatten --children-key children`, syn_2g.json → JSON (indent 2); ishchi jarayonlar: 1 (--workers 1)** — 46.0 MB/s, RAM 141.3 MB (46.7 s; 6 513 235 ta element, chiqish 3.7 GB; apply bosqichi 46.3 MB/s; jami 49.7 s, shundan indeks 2.94 s) (maqsad: ≥ 50 MB/s, RAM < 300 MB).
  - Sabab: bitta jarayonda har yozuv Python'da parse qilinadi (codec.loads), kompilyatsiya qilingan mapping funksiyalari bilan qayta quriladi va codec.dumps bilan yoziladi: 46.0 MB/s. Shu sababli 64 MB dan katta fayllarda `apply` / `flatten` o'zi ishchi jarayonlarni ishlatadi (standart: 129 MB/s, 3 ta ishchi jarayon bilan).
  - Variantlar: 1) standart avtomatik ishchilar (yuqoridagi qator: maqsaddan tez); 2) mapping baholashni Rust yadroga o'tkazish (yozuvlar baytlaridan to'g'ridan-to'g'ri) — eng katta yutuq, katta ish; 3) oddiy ko'chirish/o'chirish/nomlash mapping'larida yozuvni parse qilmasdan bayt darajasida qayta yozish.

## Ma'lumot fayllari (bench/make_synthetic.py, deterministik)

- `node_1m.json`: 41.7 MB, 1 000 000 ta yozuv (avval yaratilgan)
- `syn_10m.jsonl`: 3.3 GB, 10 000 000 ta yozuv (avval yaratilgan)
- `syn_2g.json`: 2.1 GB, 6 513 235 ta yozuv (avval yaratilgan)
- `syn_5g.json`: 5.4 GB, 16 222 716 ta yozuv (avval yaratilgan)

## Kompyuter

- Protsessor: Intel(R) Core(TM) Ultra 9 285, 24 yadro / 24 mantiqiy protsessor (8 ta P + 16 ta E yadro)
- RAM: 205.6 GB
- Ma'lumotlar diski: SAMSUNG MZVL81T0HELB-00BH1 (NVMe, SSD), E: NTFS, bo'sh joy 376.3 GB
- OS: Windows-11-10.0.26200-SP0; quvvat rejasi: Balanced
- Python 3.14.3; jsonreshape 0.1.0, yadro: rust; orjson 3.13.0, ijson 3.6.0 (yajl2_c)

## O'lchash shartlari

- Har bir amal alohida yangi jarayonda, yangi bo'sh JSONRESHAPE_HOME bilan bajarildi (kesh indeksi yo'q; faqat «qayta ochish» oldingi indeksni ishlatadi). Qidiruv, filtr, tartiblash, eksport va tuzilma o'sha jarayonda avval indeks quradi; jadvaldagi vaqt — faqat amalning o'zi (indeks vaqti JSON'da alohida).
- Istisno — foydalanuvchi oladigan natijani o'lchaydigan qatorlar: §12 dagi tartiblash qatori va «filtr … va yozish» qatorlari `jsonreshape sort` / `jsonreshape filter` kabi butun yo'lni o'lchaydi — Document.open, indeks, sort_records / filter_records va natija RecordSet'ini JSONL faylga yozish, fayl to'liq yozilguncha (AGENT_PROMPT §6.4: tartiblash natijasi shu tartibda eksport qilinadi). Tartiblangan eksport yozuvlarni kalit tartibida, ya'ni faylning tasodifiy joylaridan o'qiydi, shuning uchun fayl tartibidagi eksport uning o'rnini bosa olmaydi; faqat sort_records vaqti qo'shimcha jadvalda.
- Peak RSS — jarayonning eng katta ishchi to'plami (Windows peak_wset). Windows'da venv'ning python.exe fayli faqat ishga tushiruvchi, haqiqiy interpretator uning bola jarayoni, shuning uchun butun jarayonlar daraxti psutil bilan har 20 ms kuzatildi va har jarayonning handle'i ochiq tutilib, tugagach aniq qiymat olindi; amal jarayoni ham o'z peak'ini xabar qiladi. Ikki mustaqil o'lchov farqi: ko'pi bilan 0.0%. Jarayon tegib o'tgan mmap indeks sahifalari ham kiradi (Vazifalar dispetcheridagidek).
- Takrorlar: amalga qarab 1/2/3/5 marta; jadvalda eng yaxshi takror.
- Har o'lchovdan oldin tizim CPU yuklamasi tekshirildi: ≤ 35.0% bo'lguncha, ko'pi bilan 60.0 s kutildi. Ko'rilgan yuklama: 2.2–16.7% (o'rtacha 6.6%); 0 marta kutish muddati tugab, o'lchov baribir bajarildi; jami kutish 84.0 s.
- O'lchovlar paytida boshqa jarayonlar band qilgan protsessor: o'rtacha 1.7, ko'pi bilan 4.9 yadro (24 tadan).
- apply_2g_w3, flatten_2g_w3 — alohida o'lchov (2026-10-19 19:46:13, shu sharoitda; `20261019-194613.json`), natijalarga qo'shilgan.
- apply_2g, apply_2g_jsonl, apply_2g_w1, apply_2g_w3 — alohida o'lchov (2026-10-19 20:42:19, shu sharoitda; `20261019-204219.json`), natijalarga qo'shilgan.
- apply_2g, apply_2g_jsonl, flatten_2g — alohida o'lchov (2026-10-19 20:58:57, shu sharoitda; `20261019-205857.json`), natijalarga qo'shilgan.
- OS fayl keshi: kompyuterda 205.6 GB RAM bor, ma'lumot fayllari keshda turadi — jadvaldagi natijalar iliq keshda olingan (faylni o'qish diskka bormaydi).
- Bitta sovuq o'qish o'lchandi: fayl FILE_FLAG_NO_BUFFERING bilan nusxalandi (nusxa OS keshiga tushmaydi), shu nusxa ochildi va indekslandi; disk hisoblagichi 2.1 GB o'qilganini ko'rsatdi (fayl 2.1 GB) — ma'lumot haqiqatan diskdan kelgan.
- §12 maqsadlari 5 GB massiv fayl uchun; bu yerda 2.1 GB massiv (6 513 235 ta yozuv) va 10 000 000 ta yozuvli 3.3 GB JSONL ishlatildi. Tezlik (MB/s) streaming tufayli fayl hajmiga bog'liq emas, ochish faqat faylning boshini o'qiydi, xotira esa hajmga deyarli bog'liq emas (indeks diskda, mmap).
- §12 dagi 5 GB holatni to'g'ridan-to'g'ri tekshirish uchun qo'shimcha jadvalda 5.4 GB massivda (16 222 716 ta yozuv) ochish, indeks va qidiruv ham o'lchandi.
- Tartiblash: `name` maydonida atigi ~286 xil qiymat bor (make_synthetic: 26 ism × 11 familiya), shuning uchun kalitlarni kodlash keshi ko'p ishlaydi; deyarli takrorlanmaydigan kalitlar uchun qo'shimcha jadvaldagi `created_at` (son) va `contact.phone` (satr) qatorlariga qarang.
- §12 dagi «mapping yoki tahrirlar bilan eksport» maqsadi: mapping bilan eksport shu jadvalda (`jsonreshape apply` / `flatten`, buyruq foydalanuvchi ishga tushirgandek, jarayon ichida `jsonreshape.cli.main`), tahrirlar bilan eksport `edit_latest.md` va `export_latest.md` da. Oddiy JSON → JSONL va JSON → JSON (indent 2) eksporti — taqqoslash uchun.
- Mapping: `bench/mapping_syn.json` — 10 000 yozuvli namunadan olingan daraxt + 6 qadam: `contact.address.city` yozuv darajasiga (`name` oldiga), `contact` ochildi (unwrap), `orders[].oid` yig'ildi (gather), `tags` o'chirildi, `score` → `ball`, yangi `holat` guruhi ichiga `active`. Vaqt — buyruqning indeksdan keyingi qismi (GUI'da indeks fayl ochilganda quriladi); jami vaqt va «apply bosqichi» (engine.apply'ning o'zi) ham ko'rsatilgan.
- Ishchi jarayonli amallarda (CSV eksport, mapping) xotira — butun dastur: asosiy jarayon va ishchi jarayonlar ishchi to'plamlarining bir paytdagi yig'indisi (har 20 ms dagi namunalarning eng kattasi; venv ishga tushiruvchisi kirmaydi) yoki bitta jarayonning aniq peak'i — qaysi biri katta bo'lsa.
- Gibrid protsessor (8 ta P + 16 ta E yadro): affinity o'rnatilmadi, yadroni Windows tanlaydi; E yadroga tushgan takror sekinroq bo'ladi, shuning uchun eng yaxshi takror olinadi.

## Nimani o'lchab bo'lmadi

- §12 «oddiy noutbuk» uchun yozilgan; o'lchov ish stoli protsessorida (Intel(R) Core(TM) Ultra 9 285) bajarildi — noutbukda CPU'ga bog'liq amallar sekinroq bo'lishi kutiladi.
- Hamma amallarni sovuq keshda o'lchab bo'lmadi: butun tizim keshini tozalash administrator huquqini talab qiladi va boshqa ishlayotgan dasturlarga ta'sir qiladi; faqat bitta sovuq ochish va indekslash o'lchandi (yuqorida).
- Bu jadvalda «birinchi yozuvlar ko'rinishi» kutubxona darajasida o'lchandi (Document.open + daraxt tugunlari + qiymatlar), Qt chizish vaqti kirmaydi; haqiqiy oynadagi o'lchovlar — `gui_latest.md`.

Qayta o'lchash: `.venv/Scripts/python.exe bench/run_bench.py` (to'liq) yoki `--subset 2g` (§14 25-test to'plami); bitta amal: `--only NOM` (`--list`).
