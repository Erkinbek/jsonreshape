# jsonreshape

[English](README.md) | **O'zbekcha**

Katta JSON fayllar (100 MB dan bir necha GB gacha) bilan ishlash uchun desktop dastur va CLI:
ko'rish, qidirish, filtrlash, tartiblash, konvertatsiya, tahrirlash, tuzilmani qayta qurish, statistika,
dublikatlar, JSON Schema, bo'lish/birlashtirish, ta'mirlash va taqqoslash.
**Fayl hech qachon to'liq xotiraga yuklanmaydi**: u bir marta indekslanadi (har yozuvning bayt chegaralari),
keyin faqat kerakli bo'laklar o'qiladi. **Asl fayl hech qachon o'zgartirilmaydi**: tahrirlar alohida qatlamda
turadi, saqlash yangi faylni atomik almashtiradi (standart bo'yicha `.bak` zaxira nusxasi bilan).

> **Holat: barcha 6 bosqich tayyor** (2026-10-19). Muallif: Erkin Pardayev · Claude Opus 5.5 yordamida yaratilgan
> ([muallif, aloqa va mualliflik huquqi](#muallif)).

| # | Bosqich | Holat |
|---|---|---|
| 1 | **Yadro**: reader + indeks (Rust yadro va Python fallback), RecordSet, qidiruv, filtr, tartib, writer, CLI (`inspect`, `search`, `filter`, `sort`, `convert`) | ✅ tayyor |
| 2 | **Ko'ruvchi GUI** (PySide6): daraxt, jadval, matn, xom baytlar, tuzilma, navigatsiya, qidiruv paneli, filtr quruvchi, tartiblash, eksport, fon vazifalari, holat qatori | ✅ tayyor |
| 3 | **Tahrirlash**: tugun tahriri (qiymat, kalit, tur, qo'shish, o'chirish, nusxa, surish, sudrab tashlash), ommaviy amallar (oldindan aniq sanash bilan), JSON Patch, undo/redo, tahrir qatlami (SQLite + segment fayllar, uzluksiz avtosaqlash va tiklash), xavfsiz saqlash, CLI `patch` | ✅ tayyor |
| 4 | **Tuzilma quruvchi**: mapping engine (`json-tuzilma.html` bilan aynan bir xil natija), GUI (sudrab tashlash, jonli ko'rinish), mapping fayllari, CLI `apply`, `flatten` | ✅ tayyor |
| 5 | **Asboblar**: maydon statistikasi, dublikatlar, JSON Schema (yaratish va tekshirish), bo'lish / birlashtirish / ajratib olish, ta'mirlash, taqqoslash (diff), "aqlli" qiymatlar; CLI `stats`, `dupes`, `schema`, `validate`, `split`, `merge`, `extract`, `repair`, `diff` | ✅ tayyor |
| 6 | **Silliqlash**: buyruqlar palitrasi, tugmalarni sozlash, sessiyani tiklash, workspace fayllari, mavzular, Windows «Open with», PyInstaller `.exe`; og'ir eksportlar uchun ishchi jarayonlar; yakuniy benchmark | ✅ tayyor |

---

## Mundarija

1. [O'rnatish](#ornatish)
2. [Tez boshlash (CLI)](#tez-boshlash-cli)
3. [Grafik interfeys (GUI)](#gui)
4. [Qo'llab-quvvatlanadigan formatlar](#formatlar)
5. [Raqamlar aniqligi](#raqamlar-aniqligi)
6. [Qidiruv](#qidiruv)
7. [Filtr tili](#filtr-tili)
8. [Tartiblash](#tartiblash)
9. [Eksport va konvertatsiya](#eksport)
10. [Indeks, kesh va xotira](#indeks)
11. [Xatolar va loglar](#xatolar)
12. [Arxitektura](#arxitektura)
13. [Testlar](#testlar)
14. [O'lchovlar (benchmark)](#olchovlar)
15. [Qabul qilingan qarorlar va ochiq savollar](#qarorlar)
16. [Muallif, aloqa va mualliflik huquqi](#muallif)

---

<a id="ornatish"></a>
## 1. O'rnatish

Talablar: Windows 10/11 (Linux/macOS'da ham ishlaydi), **Python 3.11+** (3.14 da tekshirilgan),
tezkor yadro uchun **Rust** (stable) va Visual Studio Build Tools (MSVC).

```powershell
py -3.14 -m venv .venv
.venv\Scripts\python -m pip install -U pip maturin
.venv\Scripts\maturin develop --release          # Rust yadrosini quradi va paketni o'rnatadi
.venv\Scripts\python -m pip install -e ".[dev]"  # test vositalari (pytest, hypothesis, openpyxl ...)
```

Rust bo'lmasa ham dastur ishlaydi: yadroning sof Python nusxasi (fallback) bor, natijasi aynan bir xil
(testlar bilan tekshiriladi), faqat sekinroq. Majburan tanlash: `JSONRESHAPE_BACKEND=python`
(yoki CLI'da `--backend python`).

Grafik interfeys uchun: `.venv\Scripts\python -m pip install -e ".[gui]"` (PySide6).

Ishga tushirish: `jsonreshape ...` (CLI) yoki `python -m jsonreshape ...`; GUI — `jsonreshape-gui [fayl ...]`
yoki `python -m jsonreshape.gui` ([3-bo'lim](#gui)).

### Windows uchun `.exe` (Python o'rnatmasdan ishlatish)

```powershell
powershell -ExecutionPolicy Bypass -File scripts\build_exe.ps1     # -SkipTests: faqat qurish; -NoInstall: pip ishlatmaslik
```

Skript PyInstaller'ni (6.22.3) venv'ga o'rnatadi (faqat shu qadamda tarmoq kerak; dasturning o'zi oflayn), ikonani
yasaydi, `packaging\jsonreshape.spec` bo'yicha quradi va tayyor exe ustida smoke-testlarni ishga tushiradi
(`-m packaging`). Natija — `dist\jsonreshape\` papkasi (~78 MB): `jsonreshape-gui.exe` (oyna) va `jsonreshape.exe`
(CLI) bitta `_internal\` papkasini ishlatadi; foydalanuvchiga butun papka beriladi. Ishga tushish: CLI ~0,10 s,
GUI oynasi ~0,28 s. Qo'lda:

```powershell
.venv\Scripts\python -m pip install "pyinstaller==6.22.3" "pyinstaller-hooks-contrib==2026.8"
.venv\Scripts\python packaging\make_icon.py build\jsonreshape.ico
.venv\Scripts\python -m PyInstaller --noconfirm --clean --distpath dist --workpath build\pyinstaller packaging\jsonreshape.spec
.venv\Scripts\python -m pytest -q -m packaging tests\test_packaging.py
```
Dastur tarmoqqa hech narsa yubormaydi (telemetriya ham, yangilanish tekshiruvi ham yo'q).

<a id="tez-boshlash-cli"></a>
## 2. Tez boshlash (CLI)

Quyidagi buyruqlar **cmd.exe, Windows PowerShell 5.1, PowerShell 7 va bash'da bir xil** ishlaydi
(`#` bilan boshlangan qatorlar — izoh, ularni terminalga kiritmang).
Qoida: ifodani qo'shtirnoq (`"..."`) ichiga, undagi matnni bitta tirnoq (`'...'`) ichiga yozing.

```text
# Fayl haqida: format, kodirovka, yozuvlar soni, indeks tezligi, tuzilma (namuna: 10 000 yozuv)
jsonreshape inspect data.json --sample 10000

# Qidiruv: kalit va qiymatlarda, katta-kichik harfga sezgir emas, apostroflar bir xil
jsonreshape search data.json "O'zbekiston"
# lotin-kirill sezgir emas: Тошкент ham topiladi
jsonreshape search data.json Toshkent --translit
jsonreshape search data.json toshkent --path contact.address.city --values
jsonreshape search data.json "^Tosh.*t$" --regex
jsonreshape search data.json "> 1000" --number
# barcha mosliklar CSV faylga
jsonreshape search data.json maxsus --export natijalar.csv

# Filtr (xavfsiz ifoda tili yoki JMESPath); saqlangan filtrlar (preset)
jsonreshape filter data.json toshkent.json --where "contact.address.city == 'Toshkent' and len(children) > 2"
jsonreshape filter data.json yuqori.json --jmespath "active && score > to_number('100')"
jsonreshape filter data.json toshkent.json --where "contact.address.city == 'Toshkent'" --save-preset toshkent
jsonreshape filter data.json toshkent.csv --preset toshkent
# saqlangan filtrlar ro'yxati
jsonreshape presets

# Tartiblash (bir yoki bir nechta maydon; "-" kamayish tartibi)
jsonreshape sort data.json sorted.json --by name --by -id

# Konvertatsiya (+ ixtiyoriy filtr va tartib)
jsonreshape convert data.json data.jsonl
# keyed faylning JSONL ko'rinishidan ({"kalit": qiymat} qatorlari) kalitli obyektni qayta tiklash
jsonreshape convert keyed.jsonl keyed.json --as-keyed
jsonreshape convert data.json jadval.csv --columns "id,name,contact.address.city"
jsonreshape convert data.json jadval.xlsx --array-mode rows --explode orders
jsonreshape convert data.json baza.sqlite
jsonreshape convert data.json chiroyli.json --indent 4 --sort-keys --where "active == true" --sort-by -score
# faqat tanlangan yozuvlar (raqamlar 0 dan: 0:100 = 0..99) yoki bitta tugun
jsonreshape convert data.json tanlov.csv --select "0:100,500"
jsonreshape convert data.json buyurtmalar.json --node "#5.orders"

# JSON Patch (RFC 6902): natija yangi faylga, kirish fayli bilan bir xil format va kodirovkada
jsonreshape patch data.json tuzatish.patch.json natija.json
# avval data.json.bak; --no-backup
jsonreshape patch data.json tuzatish.patch.json --in-place

# Tuzilma quruvchida saqlangan mapping'ni butun faylga qo'llash (oqim bilan, parallel jarayonlarda)
jsonreshape apply tuzilma.mapping.json data.json yangi.json
# ichma-ich children ro'yxatini daraja raqami (_depth) bilan bitta ro'yxatga tekislash
jsonreshape flatten data.json tekis.jsonl --children-key children

# Statistika, dublikatlar, JSON Schema
jsonreshape stats data.json --field contact.address.city
jsonreshape stats data.json --where "active == true" --json
jsonreshape dupes data.json --key id --top 10 -o takroriy.jsonl
jsonreshape schema data.json data.schema.json --strict
jsonreshape validate data.json --schema data.schema.json --export xatolar.csv

# Bo'lish, birlashtirish, ajratib olish
# yoki --size 500MB / --records 100000; -o papka
jsonreshape split data.json --parts 10
# andozani dastur o'zi ochadi
jsonreshape merge "qism_*.json" -o hammasi.jsonl --dedupe-by id
jsonreshape extract katta.json elementlar.json --path data.items
jsonreshape extract data.json buyurtmalar.jsonl --gather "orders[*]"

# Ta'mirlash (asl fayl o'zgarmaydi) va taqqoslash
# --check: faqat tekshirish
jsonreshape repair buzuq.json tuzatilgan.json --report tuzatishlar.csv
jsonreshape diff eski.json yangi.json --key id --exit-code
```

JMESPath'da `` `100` `` kabi literal kerak bo'lsa: PowerShell'da bitta tirnoqli satr
(``--jmespath 'score > `100`'``), cmd.exe'da qo'shtirnoq (``--jmespath "score > `100`"``) ishlating
yoki literalsiz yozing: `to_number('100')`.

Umumiy kirish parametrlari: `--record-path data` (ichida katta massiv bor obyekt uchun; `data[0]`,
`/data/0`, `data.0` ham ishlaydi), `--format auto|array|jsonl|brace_list|wrapped|keyed|single|stream`,
`--allow-partial` (JSON xatosi bo'lsa, xatogacha bo'lgan yozuvlar bilan ishlash), `--no-cache`.
Global: `--lang uz|en`, `--backend auto|rust|python`, `-q`, `-v`/`-vv`, `--version`.

Chiqish kodlari:

| Kod | Ma'nosi |
|---|---|
| `0` | muvaffaqiyatli |
| `1` | foydalanuvchi xatosi: noto'g'ri buyruq/parametr/ifoda, fayl topilmadi, mavjud bo'lmagan `--record-path` (topilgan massiv yo'llari taklif qilinadi), chiqish fayli kirish fayli bilan bir xil |
| `2` | kirish faylida JSON xatosi (qator/ustun/bayt va atrofi ko'rsatiladi) **yoki** fayl ish paytida boshqa dastur tomonidan o'zgartirildi (natija saqlanmaydi) |
| `3` | bekor qilindi (Ctrl+C / Ctrl+Break) — chala chiqish fayli qolmaydi |
| `4` | ichki xato (qisqa xabar va log faylining yo'li) |
| `5` | ma'lumot mos emas: `validate` xato topdi yoki `diff --exit-code` farq topdi |

Har bir buyruqning to'liq parametrlari: `jsonreshape BUYRUQ --help` (o'zbekcha; `--lang en` — inglizcha).
PowerShell va bash'da `$key` ni bitta tirnoqda yozing: `'$key'`.

Asl fayl **o'z-o'zidan hech qachon o'zgartirilmaydi**: barcha natijalar alohida faylga vaqtinchalik nom bilan
yoziladi va oxirida atomik almashtiriladi; chiqish fayli kirish fayli bo'lsa (boshqa yozilishda, havola orqali
ham) — rad etiladi. Faylning o'zi faqat aniq so'ralganda almashtiriladi (`patch --in-place`, GUI'da **Saqlash**):
yangi nusxa tekshirilgandan keyin atomik va standart bo'yicha `.bak` zaxira bilan. Agar kirish fayli ish davomida o'zgarsa (boshqa dastur qayta yozsa,
o'chirib qayta yaratsa), bu aniqlanadi va buzuq natija yozilmaydi.

<a id="gui"></a>
## 3. Grafik interfeys (GUI)

```text
jsonreshape-gui
jsonreshape-gui katta.json boshqa.jsonl
python -m jsonreshape.gui katta.json
```

PySide6 kerak (`pip install -e ".[gui]"`). Fayllarni oynaga sudrab tashlash yoki **Fayl → Ochish** (Ctrl+O)
ham mumkin; **Fayl → So'nggi fayllar** ro'yxati saqlanadi. Birinchi yozuvlar fayl ochilishi bilan (indeks
fonda qurilayotganda) ko'rinadi, yozuvlar soni jonli yangilanadi.

### Oyna tuzilishi

* Har fayl alohida **tabda**. Tab ichida **ikki panel** yonma-yon (standart: Daraxt | Matn); har panelda
  ko'rinish tanlanadi: **Daraxt, Jadval, Matn, Xom, Tuzilma** (Ctrl+1…5). Ko'rinishlar bog'langan: birida
  tanlangan tugun boshqasida ham tanlanadi (jadval katagi, matndagi kursor, xom baytlar ham).
* Tab tepasidagi xabar panellari: JSON xatosi (qator, ustun, bayt, sababi; **«Xatoga o'tish»**, xatogacha
  bo'lgan yozuvlar ochiq qoladi), JSON Lines'dagi buzuq qatorlar ro'yxati, format ogohlantirishlari, yozuvlar
  yo'li (`data`) va uni o'zgartirish, fayl tashqaridan o'zgarganda **«Qayta ochish»**.
* Filtr, tartib yoki qidiruv natijasi ko'rsatilayotganda banner: «Ko'rinish: Filtr … · 22 / 300 yozuv» va
  **«Hammasini ko'rsatish»**.
* **Holat qatori**: fayl hajmi, yozuvlar soni (indekslash foizi bilan), format va yozuvlar yo'li, kodirovka,
  joriy yo'l, tanlov, tugunning bayt oralig'i, xotira (RAM), fon vazifalari.
* Yon panellar: **Qidiruv** (Ctrl+Shift+F), **Filtr** (Ctrl+Shift+L), **Fon vazifalari** (har vazifa: %, MB/s,
  qolgan vaqt, «Bekor qilish»).

### Ko'rinishlar

* **Daraxt.** Yozuvlar `#0`, `#1`, … (raqamlash 0 dan); kalitli faylda kalitlar; o'ralgan faylda
  (`{"meta": …, "data": […]}`) birinchi qator «(o'rama)». Ustunlar: kalit (tur belgisi va rangi), qiymat,
  bolalar soni, hajm (bayt). → ochadi, ← yopadi, `*` hammasini ochadi (1000 dan ko'p qator bo'lsa avval
  so'raydi), «N-darajagacha ochish» (1–9), «Hammasini yopish». Katta tugun (masalan, 1 mln bola) fonda
  indekslanadi va virtual ko'rsatiladi. Tepada bosiladigan yo'l (breadcrumb). Ochilgan tugunlar filtr yoki
  tartib o'zgarsa ham ochiq qoladi. Faqat ko'rinish uchun tartiblash: obyekt kalitlari alifbo bo'yicha,
  massiv elementlari maydon bo'yicha (indekslar asl holicha ko'rsatiladi). Space — to'liq qiymat oynasi.
* **Jadval.** Yozuvlar qator, maydonlar ustun (ichma-ich ham: `contact.address.city`). Ustunlar tuzilmadan
  avtomatik olinadi; «Ustunlar…» oynasida tanlash, tartib, o'z yo'lingizni qo'shish; sarlavha menyusida
  yashirish, birinchi ustunni qotirish. Joylashuv har fayl (va yozuvlar yo'li) uchun eslab qolinadi.
  Massiv/obyekt kataklari `[3]` / `{5}`, ikki marta bosilsa daraxtda ochiladi. Sarlavhani bosish — tartiblash
  (yana bosish — kamayish, Shift — qo'shimcha kalit). Sarlavha ostidagi tezkor filtrlar (Alt+↓):
  `Toshkent` (ichida bor), `=12`, `!=x`, `>5`, `<=5`, `5..10`, `~regex`, `null`, `!null`, `exists`, `missing`,
  `empty`. Ctrl+C — tanlangan kataklar TSV ko'rinishida (Excel'ga qo'yish uchun).
* **Matn.** Tanlangan tugun chiroyli formatda: ranglar, qator raqamlari, qavslarni yig'ish, qator o'rash,
  chekinish 2 / 4 / tab. Raqamlar va satrlar baytma-bayt asl holicha (`1e400`, `1.10`, `-0.0`). Kursor
  turgan tugun daraxtda ham tanlanadi. 4 MB dan katta matnning boshi ko'rsatiladi, butunini ko'rish uchun
  «Xom ko'rinishda ochish».
* **Xom.** Fayl baytlari sahifalab (butun fayl o'qilmaydi): bayt o'rni va qator raqami, «Bayt o'rniga o'tish»
  (`123456`, `0x1E240`), «Qatorga o'tish», JSON xatosi joyi belgilangan.
* **Tuzilma.** Maydonlar daraxti: turlar taqsimoti, mavjudlik %, misollar, uzunlik/son oraliqlari;
  10 000 yozuvli namuna yoki «To'liq skanerlash» (fonda). Menyu: jadvalga ustun qilib qo'shish, «mavjud» /
  «mavjud emas» filtri, tartiblash, filtr quruvchiga qo'shish.

### Navigatsiya

* **Ctrl+G**: `data[120].contact.address`, `$.data[120]`, `/data/120`, `#120`, `#120.contact`.
* **Alt+← / Alt+→** — tanlash tarixi; **Ctrl+B** — xatcho'p (nomlanadi, saqlanadi; 3-bosqichdan boshlab — Ctrl+D
  endi «nusxasini qo'shish»), **Ctrl+Shift+D** — xatcho'plar ro'yxati.
* Har tugunning kontekst menyusi: qiymatni nusxalash (JSON chiroyli / ixcham, Python, CSV qatori), yo'lni
  nusxalash (nuqtali `a.b[0].c`, JSONPath, JSON Pointer, Python `["a"]["b"][0]`, JavaScript), kalitni
  nusxalash; «Shu qiymat bo'yicha filtr», «Shu maydon bo'yicha tartiblash ↑/↓», «Daraxtda ko'rsatish»,
  «Yangi tabda ochish», to'liq qiymat.

### Qidiruv, filtr, tartib

* **Tezkor qidiruv** (Ctrl+F) daraxt, jadval, matn va xom ko'rinishda — yuklangan qismda; F3 / Shift+F3.
* **To'liq qidiruv** (Ctrl+Shift+F): kalitlarda / qiymatlarda / ikkalasida, faqat ko'rsatilgan yo'lda; oddiy
  matn, regex, son (`> 1000`); katta-kichik harf, butun so'z, lotin ↔ kirill; apostrof turlari doim bir xil.
  Natijalar topilishi bilan chiqadi; bosilsa tugunga o'tadi; «Yozuvlar to'plami sifatida ochish»; barcha
  natijalarni CSV/JSON'ga eksport; qidiruv tarixi. Filtr yoki tartib hali ishlayotganda qidiruv boshlanmaydi
  (ko'rinish tayyor bo'lishini kutish so'raladi).
* **Filtr** (Ctrl+Shift+L): vizual quruvchi (maydon + operator + qiymat; VA / YOKI / EMAS guruhlari) va ifoda
  qatori (ifoda tili yoki JMESPath) — biri o'zgarsa ikkinchisi yangilanadi; nomli presetlar. Natija fonda
  hisoblanadi va to'lib borishi bilan ko'rinadi.
* **Tartiblash** (Qidiruv → Tartiblash…): bir nechta maydon, o'sish / kamayish, `null` oxirida yoki boshida,
  tabiiy tartib (`child2 < child10`), o'zbek alifbosi (standart) / oddiy / Unicode.
* Filtr va tartib hamma ko'rinishlarga va eksportga birdek ta'sir qiladi; qidiruv natijasi ochilgan bo'lsa,
  filtr va tartib o'sha yozuvlar ustida ishlaydi.

### Eksport (Ctrl+E)

JSON (chekinish 2 / 4 / tab / ixcham, kalitlarni tartiblash, tashqi o'ramani saqlash, kalitli obyektni
tiklash), JSONL, CSV, XLSX, SQLite. Doira: joriy ko'rinish (filtr + tartib), barcha yozuvlar, tanlangan yozuvlar,
joriy tugun (massiv yoki obyekt tugun — CSV/XLSX/SQLite'ga ham) yoki boshqa asbob bergan yozuvlar to'plami
(masalan, takroriylar). «Tashqi o'ramani saqlash» har formatda ishlaydi; `.gz` nomli chiqish siqiladi.
Manba faylning o'ziga (boshqa nom yoki havola orqali ham) yozish taqiqlangan. Eksport fonda ishlaydi: dialogni
yopsangiz ham, faylni qayta ochsangiz ham davom etadi; oxirida hisobot (yozuvlar, fayllar, ogohlantirishlar).

### Tahrirlash (3-bosqich)

Asl fayl tahrir paytida **o'zgartirilmaydi**: har bir o'zgarish alohida *tahrir qatlamiga* yoziladi, fayl faqat
**Saqlash** bosilganda tekshirilgan yangi nusxa bilan atomik almashtiriladi.

* **Tugun ustida** (daraxt va jadvalda): **Enter** — qiymatni tahrirlash (qisqa qiymat katakning o'zida, uzun matn
  alohida oynada; son asl yozuvi bilan ochiladi — `1.10` `1.1` ga aylanmaydi); **Shift+Enter** — JSON matn sifatida
  (jonli tekshiruv, xato qatori va ustuni belgilanadi, «Formatlash»); **F2** — kalitni qayta nomlash; **Ins** —
  qo'shish (ichiga / oldiga / ortiga: kalit, tur, qiymat); **Del** — o'chirish; **Ctrl+D** — nusxasini qo'shish;
  **Alt+↑ / Alt+↓** — surish; **Ctrl+V** — buferdagi JSON'ni qo'yish; menyuda «Turini o'zgartirish» (faqat mumkin
  bo'lgan turlar). Mavjud kalit, noto'g'ri son yoki JSON kabi xatolar tahrir maydonining o'zida yoki holat qatorida
aytiladi (xato oynasi ochilmaydi). Daraxtda yozuv va
  elementlarni sichqoncha bilan sudrab ko'chirish mumkin (oldiga / ichiga / ortiga). Bir nechta tanlangan tugun
  (Shift / Ctrl) bilan o'chirish, nusxalash va surish — 10 000 tagacha, undan ko'pi uchun ommaviy amallar.
* **Jadvalda** aniq ustun katagi tahrirlanadi (yozuvda yo'q maydon qo'shiladi); `$key` ustunida F2 — kalitli faylda
  yozuv kalitini o'zgartiradi; `$index` faqat o'qish uchun.
* **Ortga qaytarish** Ctrl+Z, **qayta bajarish** Ctrl+Y / Ctrl+Shift+Z. Har bir amal (ommaviy amal ham) — bitta
  qadam, menyuda nomi bilan («Ortga qaytarish: …»); standart 1000 qadam (sozlamada, kamida 200).
* **Ommaviy amallar** (Ctrl+H): topish va almashtirish (oddiy matn yoki regex, ixtiyoriy yo'l bo'yicha), kalitni
  qayta nomlash, maydonni o'chirish, maydonga qiymat berish (har doim / yo'q bo'lsa / bor bo'lsa), matnlarni
  tozalash (chetdagi va ortiqcha bo'shliqlar), turni o'zgartirish, takrorlarni o'chirish (kalit bo'yicha,
  birinchisi yoki oxirgisi qoladi). Doira: barcha yozuvlar, joriy ko'rinish (filtr / tartib) yoki tanlangan
  yozuvlar. Avval **«Ko'rib chiqish»**: o'zgaradigan yozuvlarning aniq soni, 5 ta misol (oldin → keyin) va
  muammolar; keyin **«Qo'llash»** — natija sanalgan son bilan aynan bir xil.
* **JSON Patch** (RFC 6902): Tahrir → «JSON Patch'ni qo'llash…» (bitta qadam; biror amal bajarilmasa hech narsa
  o'zgarmaydi) va «O'zgarishlarni JSON Patch sifatida saqlash…» (asl faylga nisbatan sof farq).
* **Saqlash** (Ctrl+S) fonda ishlaydi. Fayl o'z formatida (massiv, kalitli obyekt, o'rama, JSONL, oqim, bitta
  qiymat), kodirovkasida (UTF-8, BOM, UTF-16), siqilishida (gzip) va atributlari bilan (yashirin, tizim, NTFS
  siqish) yoziladi; o'zgarmagan yozuvlar baytma-bayt ko'chiriladi. Yangi fayl avval vaqtinchalik faylga yoziladi va
  yadro skaneri bilan tekshiriladi (yozuvlar soni ham), keyin bitta atomik amal bilan asl faylning o'rniga
  qo'yiladi. Eski fayl standart bo'yicha `fayl.json.bak` bo'lib qoladi (sozlamada o'chiriladi). Ochiq tahrir
  maydonidagi yozilgan qiymat saqlashdan oldin qo'llanadi. Shu fayldan eksport yoki boshqa fon vazifasi ishlayotgan
  bo'lsa, saqlash ularni kutadi (yoki to'xtatadi — so'raladi). **Boshqacha saqlash** (Ctrl+Shift+S) — boshqa faylga
  (`.gz` nomi — gzip); boshqa tabda saqlanmagan o'zgarishlar bilan ochiq faylning ustiga yozish rad etiladi.
* **Avtosaqlash va tiklash.** Har bir qadam darhol diskka yoziladi (SQLite WAL + nazorat yig'indili segment
  fayllar, fsync): dastur yoki kompyuter o'chib qolsa ham o'zgarishlar yo'qolmaydi. Fayl keyingi ochilishda panel
  chiqadi: «Saqlanmagan o'zgarishlar (vaqt, N ta qadam)» — tiklash, o'chirish yoki JSON Patch sifatida saqlash.
  Saqlash o'rtasida uzilgan bo'lsa (hatto fayl yo'qolib qolgan bo'lsa ham) — **«Saqlashni yakunlash»**: qaror
  dalillarga (fayl identifikatori, vaqti, hajmi, hash'lari) asoslanadi; shubha bo'lsa hech narsa o'chirilmaydi va
  fayllar nomma-nom aytiladi. Fayl tashqaridan o'zgargan bo'lsa, o'zgarishlarni JSON Patch sifatida saqlash taklif
  qilinadi (ularni yangi faylga qayta qo'llash mumkin).
* Qatlam joyi: `%LOCALAPPDATA%\jsonreshape\edits\` (sozlama «fayl yonida» — `fayl.json.jredit\`). Bitta faylni
  bir vaqtda faqat bitta tab yoki oyna tahrirlaydi (boshqa yozilishdagi yo'l yoki havola orqali ham), qolganlari
  faqat ko'radi.
* Tabni yopish, qayta ochish (F5) va chiqishda saqlanmagan o'zgarishlar yoki ishlab turgan saqlash haqida so'raladi.
  Tab va oyna sarlavhasida `*`, holat qatorida «● N o'zgarish».
* **Cheklovlar**: 50 MB dan katta bitta yozuv tahrirlanmaydi (ommaviy amallar uni o'tkazib, sanab ko'rsatadi);
  JSON xatosi bor (qisman ochilgan) fayl faqat o'qish uchun — avval «Ta'mirlash»; `brace_list` fayl JSON massiv
  sifatida saqlanadi, JSONL'dagi buzuq qatorlar saqlashda olib tashlanadi, gzip oxiridagi ortiqcha ma'lumot
  (zaxirasiz saqlashda) tashlab yuboriladi — uchalasi ham avval so'raladi.

### Tuzilma quruvchi (4-bosqich, Ctrl+6)

Yozuvlar tuzilmasini sichqoncha bilan qayta quring va natijani butun faylga qo'llang. Etalon — `json-tuzilma.html`
prototipi: natija u bilan aynan bir xil (1–10 qabul testlari va 4 000 tasodifiy holatda tekshirilgan). Farqlar faqat
JavaScript cheklovlari bilan bog'liq: sonlar aniq saqlanadi (`1.10`, `1e400`, `12345678901234567890`), kalitlar
tartibi o'zgarmaydi, qayta bajarish (redo) bor, yozuv raqamlari 0 dan.

* Tuzilma ko'rinishidagi «Tuzilma quruvchi…» tugmasi yoki Ctrl+6. Chapda tuzilma daraxti (namunadan — standart
  10 000 yozuv — yoki «To'liq skanerlash» bilan barcha yozuvlardan), o'ngda jonli natija («Natija» / «Asl»,
  `‹ #N ›` yozuv tanlagich).
* **Sudrab tashlash**: qator o'rtasiga — ichiga, yuqori yoki pastki chetiga — oldiga yoki ortiga. Massiv ichidagi
  maydonni tashqariga olib chiqish — «ro'yxatga yig'iladi», tashqaridagi maydonni massiv elementi ichiga —
  «har elementga takrorlanadi». Yangi guruh (Ins), nomini o'zgartirish (F2), olib tashlash (Del), ichidagilarni bir
  pog'ona yuqoriga chiqarish (Ctrl+Shift+U), rekursiv tekislash (`children` → daraja raqami `_depth` bilan),
  surish (Alt+↑ / Alt+↓), ko'chirish (Ctrl+X, Ctrl+V); qator menyusida har amal va nega mumkin emasligi.
* Har o'zgarish nishon bilan ko'rinadi (`ko'chirildi`, `asl nomi`, `yangi guruh`, `rekursiv`, …); Ctrl+Z / Ctrl+Y —
  quruvchining o'z tarixi; «Asl holat».
* Mapping'ni faylga saqlash va ochish; **«Mapping bilan eksport»** (Ctrl+E): JSON yoki JSON Lines, butun fayl yoki
  joriy ko'rinish; fonda, katta fayllarda parallel jarayonlarda. CLI: `jsonreshape apply`, `jsonreshape flatten`.
* Namunada uchramagan maydonlar natijaga tushmaydi (etalon qoidasi) — eksport hisobotida bunday yozuvlar soni
  aytiladi; kerak bo'lsa «To'liq skanerlash».

<a id="asboblar"></a>
### Asboblar (5-bosqich)

Hammasi **Asboblar** menyusida, tugun menyusida va tegishli ko'rinishlarda. Og'ir ishlar fonda (progress va bekor
qilish bilan); tahrirlangan fayl ko'rsatilgan holatida (saqlanmagan o'zgarishlar bilan) ishlatiladi; natija fayllari
hech qachon manba fayl yoki boshqa tabda saqlanmagan o'zgarishlar bilan ochiq fayl ustiga yozilmaydi.

* **Maydon statistikasi** (Asboblar → «Maydon statistikasi…»; tugun menyusi, jadval sarlavhasi va Tuzilma
  ko'rinishida «Statistika»; o'ng panel): mavjudlik (bor / `null` / yo'q), turlar, noyob qiymatlar soni (aniq yoki
  «≈» — xatolik chegarasi bilan), sonlar uchun min / max / o'rtacha / yig'indi (aniq yozilishida), satr uzunliklari,
  eng ko'p uchragan 20 qiymat (ulushi; ko'p qiymatli maydonlarda yozuvlar soni ham). Hisoblanayotganda natija har
  ~1 soniyada yangilanadi. Qiymatni ikki marta bosish — shu qiymat bo'yicha filtr; menyuda `null`, yo'q, tur, uzunlik
  bo'yicha filtr va «Daraxtda ko'rsatish». Doira: joriy ko'rinish yoki barcha yozuvlar; «Aniq hisoblash».
* **Takroriy yozuvlar** (Asboblar → «Takroriy yozuvlar…», Tuzilma ko'rinishi): kalit maydon (`$key` — a'zo kaliti,
  `$` — butun yozuv), birinchisi yoki oxirgisi qoladi; takroriylar soni, 20 ta misol guruh (bosilsa — yozuvga
  o'tadi; guruh bo'yicha filtr); «Takroriylarni ko'rsatish» (yozuvlar to'plami sifatida), «Eksport…»,
  «Takroriylarni o'chirish…» (ommaviy amal: avval aniq sanab ko'rsatadi).
* **JSON Schema** (Asboblar → JSON Schema): «Schema yaratish…» — butun hujjat yoki bitta yozuv uchun, yumshoq yoki
  qat'iy, tuzilma keshidan yoki to'liq skanerlash bilan; «Schema bo'yicha tekshirish…» — schema fayli, doira,
  formatlar; xatolar topilishi bilan ro'yxatga tushadi (`#yozuv | yo'l | xabar | kalit so'z`), bosilsa tugunga
  o'tadi; «Xatoli yozuvlarni ko'rsatish», CSV/JSONL'ga eksport, xulosa (kalit so'zlar va yo'llar bo'yicha).
* **Ta'mirlash** (Asboblar → «Ta'mirlash…»; JSON xatosi, buzuq qatorlar va `brace_list` panellaridagi tugma):
  «Tekshirish» (hech narsa yozmaydi) va «Ta'mirlash» — natija yangi faylga; hisobot: tuzatishlar turi bo'yicha
  (birinchi joyi xom ko'rinishda ochiladi), kesilgan qism, tuzatib bo'lmaydigan joy (qator, ustun va `^` bilan),
  barcha tuzatishlar jadvali, hisobotni saqlash, «Ta'mirlangan faylni ochish», kerak bo'lsa «Qisman natijani
  saqlash».
* **Bo'lish va birlashtirish** (Asboblar menyusi; tugun menyusi → «Shu tugunni qismlarga bo'lish…»). Bo'lish: butun
  fayl, joriy ko'rinish yoki tugun; qismlar soni, har qismdagi yozuvlar yoki qism hajmi bo'yicha, **jonli reja**
  (qism nomlari, yozuvlar, hajm); format, nom andozasi, o'ramasiz, eski qismlarni o'chirish. Birlashtirish: fayllar
  va ochiq tablar (saqlanmagan tahrirlar bilan — ko'rsatilganidek), tartibni o'zgartirish, JSON/JSONL, chekinish,
  kalitlarni tartiblash, o'ramani saqlash, kalit bo'yicha takrorlarni olib tashlash.
* **Taqqoslash** (Asboblar → «Ikki faylni taqqoslash…»; tugun menyusi → «Taqqoslash uchun belgilash» / «Belgilangan
  bilan taqqoslash»; ikkita tanlangan tugun → «Tanlangan ikkitasini taqqoslash»). Fayllar kalit, tekislash yoki o'rin
  bo'yicha taqqoslanadi: natijalar jadvali (holat, kalit, A#, B#, o'zgarishlar soni, birinchi yo'l) va tanlangan
  juftning yonma-yon daraxti `Kalit | A | B` — qo'shilgan yashil, o'chirilgan qizil, o'zgargan sariq, ko'chgan ko'k;
  qisqa satrlarda o'zgargan harflar belgilanadi; **F7 / Shift+F7** — keyingi / oldingi farq; «Faqat farqlar»,
  massivlarni moslash (indeks / avto / kalit / tartibsiz), A ⇄ B, «Matn» rejimi; farqli yozuvlarni yozuvlar to'plami
  sifatida ochish; hisobotni eksport qilish.
* **«Aqlli» qiymatlar** (daraxt, jadval, to'liq qiymat oynasi): unix vaqt va ISO sanalar — Toshkent vaqti va hafta
  kuni bilan izoh; URL'lar tagiga chizilgan, **Ctrl+bosish** → «Brauzerda ochilsinmi?» → faqat tasdiqdan keyin
  ochiladi; `#rgb` / `#rrggbb` ranglar — kichik namuna; base64 rasmlar — to'liq qiymat oynasida kichik rasm.
* **Tuzilma ko'rinishi**: «null / yo'q» ustuni (namuna yoki to'liq skanerlash bo'yicha — sarlavhada aytiladi), butun
  sonlar ulushi; «Statistika» (ikki marta bosish ham) va «Takroriy yozuvlar…».

### Qulaylik (6-bosqich)

* **Buyruqlar palitrasi (Ctrl+Shift+P)** — oyna ustida ochiladigan qidiruv oynachasi: menyulardagi barcha buyruqlar
  («Fayl ▸ …», «Asboblar ▸ …»), joriy tugunning menyusi («Tugun ▸ …»), ochiq tablar («Tab: …») va so'nggi fayllar
  («Ochish: …»). Har qatorda buyruq nomi, menyu yo'li va tugmasi. Qidiruv katta-kichik harf, apostrof turlari
  (o' / oʻ / o‘) va lotin–kirill farqini hisobga olmaydi («сақлаш» ham «Saqlash»ni topadi); avval nomi shu so'z bilan
  boshlanadiganlar, keyin so'z boshidagilar, so'ng harflar ketma-ketligi. Qidiruv bo'sh bo'lsa — oxirgi ishlatilgan
  20 ta buyruq. Hozir ishlamaydigan buyruq kulrang, sababi bilan. Enter — bajarish, Esc — yopish.
* **Klaviatura** (Sozlamalar ▸ Klaviatura): har bir buyruqning tugmasini o'zgartirish, olib tashlash, asl holiga
  qaytarish; band tugma rad etiladi va qaysi buyruqqa berilgani aytiladi. Daraxt va jadvalning o'z tugmalari (↑↓→←,
  `*`, Space, Enter, F2, Ins, Del, Ctrl+D, Alt+↑/↓, Ctrl+V, Ctrl+C …) o'zgarmaydi va ro'yxat qilib ko'rsatiladi.
* **Oxirgi sessiyani tiklash.** Dastur faylsiz ishga tushirilganda oxirgi sessiya qayta ochiladi: fayllar (tanlangan
  format / yozuvlar yo'li bilan), ikki panelning ko'rinishlari, joriy tugun, ochilgan tugunlar (har tabda 2 000
  tagacha), filtr, tezkor filtrlar, tartib va qidiruv so'rovi. Sessiya oyna yopilganda va har daqiqada saqlanadi
  (kutilmagan to'xtashdan himoya). Sozlamalar ▸ Umumiy ▸ «Oxirgi sessiyani tiklash»: Har doim (standart) / Har safar
  so'rash / Hech qachon. Fayl buyruq qatorida yoki «Open with» orqali berilsa — faqat o'sha fayl ochiladi. Topilmagan
  fayllar o'tkazib yuboriladi; saqlanmagan o'zgarishlar faqat tiklash paneli orqali taklif qilinadi.
* **Workspace** (Fayl ▸ «Workspace'ni saqlash…» / «Workspace'ni ochish…» / «Oxirgi workspace'lar»): ochiq tablar
  `*.jrworkspace` fayliga yoziladi — sessiyadagi hamma narsa, xatcho'plar, jadval ustunlari va tuzilma quruvchi
  daraxti; yo'llar workspace fayliga nisbatan ham saqlanadi (papka ko'chirilsa ham ishlaydi); fayl qat'iy tekshiriladi
  (xato bo'lsa, buzilgan joyning JSON pointeri aytiladi). Tahrir qatlamlari workspace'ga ko'chirilmaydi.
* **Windows «Open with»** (Sozlamalar ▸ Umumiy): `.json`, `.jsonl`, `.ndjson`, `.geojson`, `.gz` fayllarning «Open
  with» ro'yxatiga jsonreshape'ni qo'shish yoki olib tashlash (avval so'raladi). Faqat joriy foydalanuvchi uchun
  (`HKCU\Software\Classes`) yoziladi, standart dastur o'zgartirilmaydi; olib tashlash faqat o'zi yozgan narsani
  o'chiradi.
* **Mavzular**: tizimga ergashish / yorug' / qorong'i — barcha oynalar (3–5-bosqich oynalari ham) faqat mavzu
  ranglaridan foydalanadi va mavzu almashganda darhol yangilanadi.

### Sozlamalar va loglar

**Asboblar → Sozlamalar**: namuna hajmi, kesh papkasi va hajm chegarasi, xotira chegarasi, tartiblash xotirasi,
katta tugun chegaralari, eksport standartlari, til, mavzu (tizimga ergashish / yorug' / qorong'i), monospace
shrift va o'lchami. Loglar: **Asboblar → Loglar papkasi**; har xato oynasida «Logni ochish».

### Klaviatura

| Tugma | Amal |
|---|---|
| Ctrl+O / Ctrl+W / F5 / Ctrl+Q | ochish / tabni yopish / qayta ochish / chiqish |
| Ctrl+S / Ctrl+Shift+S | saqlash / boshqacha saqlash |
| Ctrl+Z / Ctrl+Y (Ctrl+Shift+Z) | ortga qaytarish / qayta bajarish (tuzilma quruvchida — uning o'z tarixi) |
| Ctrl+1 … Ctrl+6 | faol panelda: Daraxt, Jadval, Matn, Xom, Tuzilma, Tuzilma quruvchi |
| ↑ ↓, PgUp PgDn, Home End | yurish |
| → / ← / `*` | tugunni ochish / yopish / hammasini ochish (daraxt) |
| Space | to'liq qiymat oynasi |
| Ctrl+C / Ctrl+Shift+C | qiymatni (jadvalda tanlangan kataklarni TSV) / yo'lni nusxalash |
| Ctrl+F, F3, Shift+F3 | tezkor qidiruv, keyingi / oldingi moslik |
| Ctrl+Shift+F / Ctrl+Shift+L | to'liq qidiruv / filtr paneli |
| Ctrl+G | yo'lga o'tish |
| Alt+← / Alt+→ | orqaga / oldinga |
| Enter / Shift+Enter / F2 | qiymatni tahrirlash / JSON sifatida tahrirlash / kalitni qayta nomlash |
| Ins / Del / Ctrl+D | qo'shish / o'chirish / nusxasini qo'shish |
| Alt+↑ / Alt+↓ | tugunni surish (daraxtda) |
| Ctrl+V | buferdagi JSON'ni qo'yish |
| Ctrl+H | ommaviy amallar |
| Ctrl+B / Ctrl+Shift+D | xatcho'p qo'shish / xatcho'plar |
| Ctrl+E | eksport (tuzilma quruvchida — mapping bilan eksport) |
| Ctrl+Shift+P | buyruqlar palitrasi |
| F7 / Shift+F7 | taqqoslash oynasida keyingi / oldingi farq |
| Ctrl+= / Ctrl+- / Ctrl+0 | shriftni kattalashtirish / kichraytirish / asl holiga |
| Alt+↓ | jadvalda tezkor filtr qatori |

### Qanday qilib qotmaydi

Indekslash, qidiruv, filtr, tartib, tuzilma, eksport — hammasi fonda (progress va bekor qilish bilan).
Ro'yxatlar, jadval va daraxt o'zimizning virtual vidjetimiz bilan chiziladi: faqat ekrandagi qatorlar
o'qiladi, 100 mln va undan ko'p qator ham silliq aylanadi (Qt'ning tayyor jadval va daraxtlari har qator uchun
xotira oladi). Ekrandagi qatorlar alohida oqimda yuklanadi, keshlar chegaralangan. Python'ning chiqindi
yig'uvchisi faqat GUI oqimida ishlaydi va GIL oqimlar orasida tez almashadi — og'ir fon vazifalari paytida ham
oyna javob beradi (o'lchovlar: [14-bo'lim](#olchovlar)).

<a id="formatlar"></a>
## 4. Qo'llab-quvvatlanadigan formatlar

Format avtomatik aniqlanadi (`inspect` ko'rsatadi) yoki `--format` / `--record-path` bilan beriladi.

| Format | Ko'rinishi | Yozuv (record) |
|---|---|---|
| `array` | `[ {...}, {...} ]` | massiv elementi |
| `jsonl` | har qatorda bitta JSON (`.jsonl`, `.ndjson`, yoki `.json` nomli JSON Lines) | qator; buzuq qatorlar o'tkazib yuboriladi va ro'yxati chiqadi |
| `brace_list` | buzuq `{ {...}, {...} }` | ichidagi qiymatlar (ogohlantirish bilan) |
| `wrapped` | `{"meta": {...}, "data": [ ... ]}` | `data` (record path) elementlari; tashqi o'rama saqlanadi va `inspect`da ko'rsatiladi |
| `keyed` | `{"id1": {...}, "id2": {...}}` | yuqori darajadagi a'zolar (kalit + qiymat, kalit `$key`) |
| `single` | bitta ulkan qiymat | ichki konteynerlar talab bo'yicha indekslanadi |
| `stream` | ketma-ket JSON qiymatlar (ajratuvchisiz yoki ko'p qatorli) | har bir qiymat |

Kodirovka: UTF-8 (BOM bilan yoki BOMsiz), UTF-16 LE/BE (BOM bilan yoki BOMsiz), `.json.gz` (va istalgan
gzip). UTF-16 va gzip fayllar oldin UTF-8 nusxaga o'giriladi (oqim bilan, xotira cheklangan) va keshlanadi;
nusxa tayyor bo'lgunicha birinchi yozuvlar ko'rinmaydi (200 MB'lik ma'lumot ~0.5 s). UTF-32 rad etiladi
(tushunarli xabar bilan).

Obyekt ildizli fayl uchun aniqlash qoidalari: faylning boshidagi (`head_bytes`, standart 4 MiB) qismida hali
yopilmagan katta massiv bo'lsa — `wrapped` (shu massiv yo'li avtomatik tanlanadi va ogohlantirish chiqadi);
kichik faylda ko'pchilik qiymati obyekt bo'lgan massiv faylning asosiy qismini egallasa — `wrapped`;
a'zolarning ≥ 80 % i obyekt bo'lsa — `keyed`; aks holda — `single`. Topilgan barcha massiv yo'llari
(`candidates`) `inspect` natijasida ko'rinadi. Birinchi yozuvi 4 MiB dan katta JSON Lines fayl ham to'g'ri
aniqlanadi (birinchi qiymat oxirigacha skanerlanadi, ko'pi bilan 256 MiB).

<a id="raqamlar-aniqligi"></a>
## 5. Raqamlar aniqligi

Raqamlar matni **aynan** saqlanadi: `0.1`, `1e400`, `12345678901234567890`, `-0.0`, `1.10`, `1E5`, `-0`,
`123456789012345678901234567890` filtr, tartib, konvertatsiya va JSONL aylanmasidan keyin ham aynan shu
ko'rinishda chiqadi va hech qachon satrga aylanmaydi. Ichki model: orjson aniq qayta yoza oladigan
("plain") raqamlar oddiy `int`/`float`; qolganlari `JsonFloat`/`JsonInt` (qiymat + asl matn).

Taqqoslash hamma joyda bir xil va aniq: filtr (`--where`), `search --number` va tartiblash sonlarni aniq
o'nlik qiymati bo'yicha solishtiradi (`1.10 == 1.1`, `1e-400 > 0`, `1e400 < 2e400`, katta butun sonlar aniq).
XLSX'da 15 dan ortiq ma'noli raqamli yoki `double` diapazonidan tashqari sonlar matn sifatida yoziladi
(ogohlantirish bilan), chunki Excel ularni aniq saqlay olmaydi. SQLite'da qiymati `double`ga aynan sig'adigan
sonlar `REAL`, 64-bitli butun sonlar `INTEGER`, sig'maydiganlari (`1e400`, juda uzun sonlar) `TEXT` sifatida
(ogohlantirish bilan) yoziladi.

<a id="qidiruv"></a>
## 6. Qidiruv

`jsonreshape search FILE MATN`: kalitlarda (`--keys`), qiymatlarda (`--values`) yoki ikkalasida; faqat
belgilangan yo'lda (`--path name`, `--path contact.address`, istalgan chuqurlik: `--path "**.name"`).

Rejimlar: oddiy matn (standart: katta-kichik harfga sezgir emas, to'liq Unicode casefold), `--case-sensitive`,
`--whole-word`, `--regex` (Python `re`), `--number` (`> 1000`, `>= 5`, `< -2.5`, `= 3`, `!= 0`, `1000..2000`).

O'zbekcha qulayliklar:
* apostrof turlari **doim** bir xil: `'` `‘` `’` `ʻ` `ʼ` `` ` `` `´` (`O'zbekiston` = `Oʻzbekiston` = `O’zbekiston`);
* `--translit`: lotin ↔ kirill sezgir emas (`Toshkent` ↔ `Тошкент`, `O'zbekiston` ↔ `Ўзбекистон`,
  `G'ulom` ↔ `Ғулом`, `Shahrisabz` ↔ `Шаҳрисабз`, `Moʻjiza` ↔ `Мўъжиза`, `Isʼhoq` ↔ `Исҳоқ`,
  `sirk`/`tsirk` ↔ `цирк`, `militsiya`/`milisiya` ↔ `милиция`). Qoidalar: `е` so'z boshida va unlidan keyin
  `ye`; `ў/ғ/қ` dan keyingi `ъ` tushib qoladi; `s` va `h` orasidagi tutuq belgisi e'tiborga olinmaydi;
  `ц` va lotincha `ts` ikkalasi ham `s` deb solishtiriladi (bu "yumshoq" rejim, shuning uchun `ts`/`s`
  farqi e'tiborga olinmaydi).

Tezlik: avval baytlar bo'yicha oldindan saralash (Rust: Aho-Corasick/memchr; katta-kichik harf va
apostrof variantlari oldindan hisoblanadi), keyin faqat nomzod yozuvlar tekshirilib aniq yo'l va moslik
tasdiqlanadi. Fayl `\uXXXX` escape'lar bilan yozilgan bo'lsa yoki `--translit`/`--regex`/`--number` tanlansa —
parse asosidagi qidiruv. Natijalar topilishi bilan chiqa boshlaydi (indeks hali qurilayotgan bo'lsa ham);
natija ro'yxati RecordSet sifatida ham mavjud; `--export` barcha mosliklarni CSV/JSON'ga yozadi (xotira
cheklangan); `--count` faqat sonini chiqaradi; qidiruv tarixi saqlanadi.

<a id="filtr-tili"></a>
## 7. Filtr tili

Xavfsiz ifoda tili (`eval` ishlatilmaydi — test bilan tekshiriladi). Matnlar bitta (`'...'`) yoki qo'sh
(`"..."`) tirnoqda yoziladi; buyruq qatorida ifodani qo'shtirnoqqa, ichidagi matnni bitta tirnoqqa oling:

```text
contact.address.city == 'Toshkent' and len(children) > 2
orders[*].sum > 100 or tags contains 'vip'
name ~ '^Tosh' and not (note is null)
score between 10 and 20
id in [1, 7, 9]
exists(contact.address) and type(score) == 'number'
$key startswith 'user'          # keyed formatda yozuv kaliti
$index < 1000                   # yozuv raqami (0 dan)
`kalit bo'shliq bilan` == 1     # noqulay kalitlar: `...` yoki $["and"]
```

* Mantiq: `and`/`&&`, `or`/`||`, `not`/`!`, qavslar (ustunlik: `not` > `and` > `or`).
* Taqqoslash: `==` `=` `!=` `≠` `<` `<=` `≤` `>` `>=` `≥`, `between … and …` (chegaralar teskari berilsa
  almashtiriladi), `in` / `not in`, `contains`, `startswith`, `endswith`, `matches` / `~` (regex),
  `is null`, `is empty`, `is missing` (va `is not …`).
* Funksiyalar: `len`, `exists`, `missing`, `empty`, `lower`, `upper`, `trim`, `type`, `number`, `string`.
* Yo'llar: `a.b[0].c`, `a["x.y"]`, `orders[*].oid` (`[]` ham), `*`, `**`; massiv yo'llari "kamida bittasi" ma'nosida.
* Semantika: `!=` — `not (==)`; turlar majburan o'zgartirilmaydi (`"5" == 5` — yolg'on); tartib taqqoslash
  faqat son–son yoki satr–satr; `is null` — maydon bor va `null`; `is missing` — maydon yo'q.
* JMESPath: `--jmespath` (mutaxassislar uchun; JMESPath haqiqiylik qoidalari). Bitta yozuvda ifodani hisoblab
  bo'lmasa (masalan, `null` maydonga funksiya), o'sha yozuv mos kelmagan deb olinadi va ogohlantirishda nechta
  yozuv va birinchisining raqami ko'rsatiladi — butun filtr to'xtamaydi.
* Saqlangan filtrlar (preset): `--save-preset NOM` bilan saqlanadi, `--preset NOM` bilan ishlatiladi,
  `jsonreshape presets` ro'yxatni ko'rsatadi (`--delete NOM`).
* Vizual quruvchi modeli (2-bosqich GUI uchun) `query.Condition`/`query.Group`: operatorlar `=`, `≠`, ichida bor,
  bilan boshlanadi, regex, `>`, `<`, `≥`, `≤`, oraliq, mavjud, mavjud emas, `null`, bo'sh, ro'yxatda bor, uzunlik.
  Quruvchi va ifoda qatori bir xil natija beradi (test 18).

Filtr fonda, oqim bilan ishlaydi. Agar ifoda faqat bir nechta maydonga murojaat qilsa, Rust yadrosi shu
maydonlarning baytlarini butun yozuvni parse qilmasdan ajratadi ("extract" strategiyasi, ~2 marta tezroq).
50 MB dan katta yozuvlar ijson oqimi bilan baholanadi: konteynerlar to'liq yig'ilmaydi va tanlangan
qiymatlar uchun xotira byudjeti bor (oshsa, yozuv ogohlantirish bilan o'tkazib yuboriladi). Cheklov: bitta
juda uzun satr qiymati (masalan, yuzlab MB'lik base64) tekshirilayotganda xotirada taxminan o'z hajmining
ikki baravari joy egallaydi.

<a id="tartiblash"></a>
## 8. Tartiblash

`jsonreshape sort IN OUT --by name --by -id` — bir yoki bir nechta maydon, o'sish/kamayish (`-`).
* Turlar tartibi: sonlar < satrlar < mantiqiy (false < true) < massivlar < obyektlar; `null` va yo'q
  qiymatlar standart bo'yicha oxirida (`--nulls first|last`, yo'nalishdan qat'i nazar).
* Sonlar aniq o'nlik qiymat bo'yicha (`1.1 == 1.10`, katta butun sonlar, `1e400`).
* Satrlar **o'zbek alifbosi** bo'yicha (`--collation uz`, standart): a b d e f g h i j k l m n o p q r s t u v
  x y z **oʻ gʻ sh ch ng**, keyin kirill: а б в г д е ё ж з и й к л м н о п р с т у ф х ц ч ш ъ ь э ю я ў қ ғ ҳ.
  Masalan: Abdulla, Bobur, Gulnora, Nodira, Olim, Sardor, Zafar, Oʻtkir, Gʻani, Shahzod, Chori.
  `--collation simple` — oʻ/gʻ/sh/ch/ng alohida harf hisoblanmaydi; `unicode` — oddiy kod nuqtasi tartibi.
* "Tabiiy" tartib: `child2 < child10` (`--no-natural` bilan o'chiriladi); katta-kichik harf ikkinchi darajada.
* Barqaror (stable): teng kalitli yozuvlar asl tartibini saqlaydi.
* Maydon hech bir yozuvda topilmasa (masalan, nomi xato yozilgan), ogohlantirish chiqadi.
* Katta fayl: kalitlar Rust yadrosi bilan (butun yozuvni parse qilmasdan) ajratiladi, xotiraga sig'sa
  xotirada, sig'masa diskda tashqi merge sort. Xotira: `--memory-limit` (birliksiz son = MB, kamida 16 MiB,
  standart 256 MiB, umumiy `xotira chegarasi`ning yarmidan oshmaydi). Natija — RecordSet, shu tartibda
  eksport qilinadi (tartiblangan eksport ham ≥ 50 MB/s).

<a id="eksport"></a>
## 9. Eksport va konvertatsiya

* **JSON / JSONL**: chiroyli yoki ixcham, indent 2 / 4 / tab; `--sort-keys` obyekt kalitlarini alifbo
  bo'yicha (o'zbek tartibi, `--sort-keys codepoint` — kod nuqtasi tartibi) tartiblaydi; Unicode `\u` ga
  aylantirilmaydi; yozuvlar parse qilinmasdan bayt darajasida qayta formatlanadi (raqam va satrlar
  baytma-bayt saqlanadi). `wrapped` faylda tashqi o'rama saqlanadi (`--no-wrapper` — faqat massiv);
  `keyed` fayl JSON'da kalitli obyekt bo'lib qoladi. JSONL bitta yozuv = bitta qator formati bo'lgani uchun
  o'rama va kalitli obyekt tuzilmasini saqlay olmaydi: `keyed` yozuvlar `{"kalit": qiymat}` qatorlari bo'ladi.
  `--as-keyed` (faqat JSON natija uchun) bunday qatorlardan kalitli obyektni qayta tiklaydi:
  `{"kalit1": ..., "kalit2": ...}` — kalit va qiymatlar baytma-bayt, `--indent`/`--sort-keys` kalitli faylning
  oddiy JSON eksporti bilan aynan bir xil natija beradi; `filter`/`sort`/`convert` bilan ishlaydi, tashqi o'rama
  yozilmaydi. Har bir yozuv bitta a'zoli obyekt bo'lishi shart: massiv, son, `{}` yoki 2 a'zoli obyekt
  uchraganda yozuv raqami bilan xato chiqadi (kod 1) va hech narsa saqlanmaydi. Takroriy kalitlar o'zgarishsiz
  yoziladi va ogohlantirishda sanaladi (xotirani tejash uchun birinchi 1 000 000 ta kalit tekshiriladi).
  Boshqa formatlar bilan `--as-keyed` — xato (kod 1).
* **CSV / XLSX**: tanlangan ustunlar (`--columns "a,b.c"`) yoki avtomatik — avtomatik ustunlar **barcha**
  tanlangan yozuvlarni oldindan ko'rib chiqib aniqlanadi (hech bir maydon yo'qolmaydi; bu qo'shimcha o'qish,
  `--columns` bilan o'tkazib yuboriladi). Ichma-ich maydonlar `a.b.c` ustun bo'ladi; massivlar `;` bilan
  birlashtiriladi (`--array-mode join`, `--sep`) yoki har elementga alohida qator (`--array-mode rows`,
  `--explode orders`). Kalitlari id/sana kabi "lug'at" obyektlarda (juda ko'p turli, kam uchraydigan
  kalitlar) yozuvlarning kamida 1 % ida uchraydigan kalitlar ustun bo'lib qoladi, kam uchraydigan "dumi"
  esa `yo'l.*` nomli bitta JSON ustuniga yig'iladi (ogohlantirish bilan); yozuvning yuqori darajasi hech
  qachon bitta ustunga yig'ilmaydi. `--columns`da berilgan, lekin hech bir yozuvda topilmagan ustunlar
  ogohlantirishda nomlanadi (yozuv xatosini topish uchun). 50 MB dan katta yozuvlarda katak juda katta
  bo'lsa, u qisqartiriladi (ogohlantirish bilan), qator esa baribir yoziladi. CSV: UTF-8 BOM (Excel uchun,
  `--no-bom`), ajratuvchi `--delimiter`. XLSX: 1 048 576 qatordan oshsa avtomatik yangi varaqlarga
  (`--xlsx-split sheets`) yoki fayllarga (`files`) bo'linadi.
* **SQLite**: har massiv uchun alohida jadval, ota-bola bog'lanishi bilan (`_id`, `_parent`, `_idx`);
  jadvalda 1500 dan ortiq ustun bo'lsa, qolgan maydonlar `_extra` (JSON matn) ustuniga yoziladi; "lug'at"
  obyektlar JSON matn sifatida saqlanadi, bola jadvallar soni 200 tagacha (qolgani JSON matn).
* Faqat tanlangan qism: `filter`/`sort` natijasi, `convert --where ... --sort-by ...`, yozuv raqamlari
  (`--select "0:100,500"`, raqamlar 0 dan) yoki bitta tugun (`convert --node "#5.orders"`).
* SQLite'da qo'shimcha jadvallar: `_jsonreshape` (jadvallar ro'yxati: nomi, yo'li, otasi, turi, qatorlar soni),
  `_wrapper` (tashqi o'rama JSON matni), har jadvalda `_ord` (asl tartib); `_parent` — `REFERENCES` bilan.
* `.json.gz`, `.jsonl.gz`, `.csv.gz` nomli chiqish fayllari gzip bilan siqiladi; `--no-wrapper` har formatda ishlaydi.
* Bo'lish, birlashtirish va ajratib olish — `split`, `merge`, `extract` ([2-bo'lim](#tez-boshlash-cli), [Asboblar](#asboblar)).
* Barcha eksportlar fonda, progress bilan; natija vaqtinchalik faylga yozilib, keyin atomik nomlanadi.
  Kirish fayli ish davomida o'zgarsa, natija saqlanmaydi (chiqish kodi 2).

<a id="indeks"></a>
## 10. Indeks, kesh va xotira

* Indeks fonda quriladi; birinchi yozuvlar darhol (millisekundlarda) ko'rinadi, yozuvlar soni jonli o'sadi.
* Indeks diskda keshlanadi: `%LOCALAPPDATA%\jsonreshape\index\<sha256>.jri` — binar, `mmap` bilan o'qiladi
  (RAM'ga yuklanmaydi). Kalit: yo'l + hajm + mtime + boshidagi va oxiridagi 1 MiB hash'i + fayl bo'ylab
  tarqalgan 16 ta 4 KiB blok hash'i + format + record path. Fayl o'zgarsa indeks yaroqsiz bo'ladi; o'qish
  paytida ham yozuv chegaralari tekshiriladi. Cheklov: hajmi va mtime'i saqlangan, faqat o'rtadagi (hash
  qilinmagan) baytlarga tegadigan tahrir sezilmasligi mumkin — bunday holatda `--no-cache` ishlating.
* Fayl faqat **oxiriga qo'shilsa** (masalan, yozilayotgan JSONL log), dastur ochilgan paytdagi holat bilan
  ishlashda davom etadi va "fayl ochilgandan keyin N bayt qo'shildi, ular kiritilmadi" ogohlantirishini
  chiqaradi. Boshqa har qanday o'zgarish (qayta yozish, qisqartirish, almashtirish) — xato (kod 2).
* Kesh hajmi `cache_limit_bytes` (standart 20 GiB) — indekslar va gzip/UTF-16 nusxalari uchun umumiy;
  oshsa, eng eskilari o'chiriladi (LRU). Hozir ishlatilayotgan yoki yaqinda (1 soat ichida) ishlatilgan
  nusxalar o'chirilmaydi va hisobga kirmaydi. `cache_dir` sozlamasi berilsa, indekslar, gzip/UTF-16 nusxalari,
  tartiblash va XLSX vaqtinchalik fayllari o'sha papkada bo'ladi. Papka ishlatib bo'lmasa (masalan, uzilgan
  disk), standart papka ishlatiladi va bitta ogohlantirish chiqadi.
* Bir nechta jarayon bir faylni bir vaqtda indekslasa, biri quradi, qolganlari kutib o'sha indeksni ishlatadi.
* Ichki konteynerlar talab bo'yicha: 50 MB dan katta yoki 10 000 dan ko'p bolali tugun parse qilinmaydi —
  faqat bolalarining offsetlari skanerlanadi va diskda keshlanadi (1 million bolali tugun ~80 ms, +6 MB RAM).
* Fayl eksklyuziv bloklanmaydi (Windows: `FILE_SHARE_READ|WRITE|DELETE`): boshqa dasturlar uni o'qishi,
  yozishi, nomini o'zgartirishi va o'chirishi mumkin. Cheklov: fayl ochiq turganda boshqa dastur uni
  `MoveFileEx`/`os.replace` bilan ustidan almashtira olmaydi (Windows qoidasi); o'zgarishlar esa aniqlanadi.
  Uzun yo'llar (`\\?\`, 260 belgidan uzun) qo'llab-quvvatlanadi.
* Sozlamalar: `%LOCALAPPDATA%\jsonreshape\settings.json` — namuna hajmi, kesh papkasi va chegarasi, xotira
  chegarasi (`memory_limit_bytes`, standart 512 MiB: tartiblash, eksport bloklari, qidiruv va ulkan yozuvlar
  uchun umumiy byudjet), tartiblash xotirasi, eksport standartlari, til. `JSONRESHAPE_HOME` bilan butun papkani
  o'zgartirish mumkin.
* **Tahrir qatlami** (3-bosqich): `%LOCALAPPDATA%\jsonreshape\edits\<kalit>\` — `layer.sqlite` (qadamlar, WAL),
  `seg-*.dat` (tahrirlangan yozuvlarning yangi baytlari, nazorat yig'indilari bilan), `layer-*.bin` (ommaviy
  amallar natijasi). Diskdagi hajmi ≈ o'zgargan yozuvlar hajmi (har qo'llangan ommaviy amal uchun); bo'sh joy
  ommaviy amaldan oldin va davomida tekshiriladi. Saqlash yoki bekor qilishdan keyin qatlam o'chiriladi (uni hali
  o'qiyotgan eksport tugaganidan keyin). Sozlamalar: `edits_location` (`appdata` | `beside` — fayl yonidagi
  `.jredit` papkasi), `backup_on_save` (standart: yoqiq), `undo_limit` (1000, kamida 200).
* 5-bosqich sozlamalari: `diff_align_max_bytes` (200 MB: shundan katta fayllar kalitsiz taqqoslanmaydi —
  `--key` yoki `--by-position` kerak); statistika va dublikatlar `memory_limit_bytes` ichida ishlaydi (oshsa —
  diskka yoki taxminiy hisobga o'tadi, natijada aytiladi).

<a id="xatolar"></a>
## 11. Xatolar va loglar

* JSON xatosi: bayt offseti (UTF-16 faylda — foydalanuvchi faylidagi bayt; gzip'da — ochilgan ma'lumotdagi
  bayt, bu xabarda aytiladi), **qator va ustun** (belgilarda), atrofdagi matn va `^` belgisi; xatogacha
  bo'lgan yozuvlar ishlatilishi mumkin (`--allow-partial`). Kesilgan fayl oxirgi to'liq yozuvgacha ochiladi.
  JSON Lines faylda format noto'g'ri aniqlangani sezilsa, `--format jsonl` taklif qilinadi.
* JSONL'da buzuq qatorlar o'tkazib yuboriladi va ro'yxati (qator raqami, sabab) chiqadi.
* Xabarlar oddiy tilda: nima bo'ldi va qanday tuzatish kerak (o'zbekcha, `--lang en` — inglizcha); OS
  xatolari (fayl band, faqat o'qish uchun, disk to'lgan, yo'l topilmadi) ham tarjima qilingan.
* Loglar: `%LOCALAPPDATA%\jsonreshape\logs\jsonreshape.log` (5 MB × 3 aylanma); ichki xatoda CLI log faylining
  yo'lini ko'rsatadi; log faylini ochib bo'lmasa, dastur ishlashda davom etadi.
* JSON xatosi bor faylni **ta'mirlash** mumkin (GUI: xato panelidagi «Ta'mirlash…», CLI: `jsonreshape repair`):
  natija yangi faylga yoziladi, har bir tuzatish joyi (qator, ustun, bayt) va sababi bilan hisobot beriladi
  ([Asboblar](#asboblar)).
* Tahrirlangan faylni saqlash uzilib qolsa (elektr o'chishi, jarayon o'ldirilishi), keyingi ochilishda
  «Saqlashni yakunlash» taklif qilinadi; vaqtinchalik `.jrsave` fayllar 3 daqiqadan eski va hech qaysi jurnal
  ularga ishora qilmasa, avtomatik tozalanadi (holat qatorida aytiladi).

<a id="arxitektura"></a>
## 12. Arxitektura

Batafsil interfeys spetsifikatsiyasi: [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md).

```text
rust/                 Rust yadro (PyO3 0.30, maturin): validatsiyali skaner, reformatter, Aho-Corasick qidiruv,
                      yo'l bo'yicha qiymat ajratish, raqamlar tekshiruvi — GIL qo'yib yuboriladi
jsonreshape/
  core/               yadro API'si: _rscore (Rust) yoki _pycore (sof Python etalon) — natija aynan bir xil
  codec.py            aniq raqamli JSON loads/dumps (orjson + aniq fallback)
  paths.py            yo'llar: nuqtali, JSONPath, JSON Pointer, Python, JS; wildcard'lar
  fileio.py           Windows shared-read, uzun yo'llar, gzip/UTF-16 tayyorlash, atomik yozish
  reader.py           format aniqlash, indeks, yozuvlarga murojaat, Node daraxti, xato joyi
  index.py            diskdagi binar indeks (mmap), kesh kaliti, LRU
  stream.py           ulkan yozuvlar uchun ijson (yajl2_c) oqimi
  recordset.py        RecordSet, filtr va tartib (tashqi merge sort)
  sortkeys.py         turga mos tartib kalitlari, o'zbek kollatsiyasi, tabiiy tartib
  query.py            filtr tili, vizual quruvchi modeli, JMESPath
  textmatch.py        qidiruv rejimlari, casefold, apostroflar, lotin↔kirill
  search.py           to'liq fayl bo'yicha qidiruv (bayt prefiltr + tasdiqlash), eksport, tarix
  schema.py           tuzilma: turlar taqsimoti, mavjudlik foizi, misollar
  writer.py           oqimli yozuvchilar: JSON, JSONL, CSV, XLSX, SQLite; bir nechta kirish, tugun eksporti
  overlay.py          tahrir holati (o'zgarmas): piece table, kontent qatlamlari, virtual manzillar, ChangeSet
  editstore.py        diskdagi tahrir qatlami: SQLite (WAL) qadamlar, segment fayllar, qulf, tiklash
  editops.py          bayt darajasidagi tahrirlar va formatlash (sof funksiyalar)
  edits.py            EditSession: barcha tahrir buyruqlari, undo/redo, kalit indeksi
  bulk.py             ommaviy amallar: oldindan aniq sanash (preview) -> qo'llash
  dupes.py            dublikatlar: kanonik kalit hash'lari, diskka chiqariladigan saralash
  saver.py            saqlash: tekshirilgan yangi nusxa, atomik almashtirish, .bak, jurnal va tiklash
  jsonpatch.py        RFC 6902 / 6901: qo'llash va sof farqni eksport qilish
  mapping.py          tuzilma quruvchi modeli: daraxt, amallar, undo/redo, mapping fayli
  engine.py           mapping'ni qo'llash (json-tuzilma.html bilan aynan bir xil, kompilyatsiya, jarayonlar havzasi)
  stats.py            maydon statistikasi (aniq, kerak bo'lsa HyperLogLog + Misra-Gries)
  jschema.py          JSON Schema 2020-12: yaratish va o'zimizning tekshiruvchimiz (aniq sonlar, ECMA regex)
  filetools.py        bo'lish (split), birlashtirish (merge), ajratib olish (extract)
  repair.py           buzuq JSON'ni ta'mirlash (gibrid: Rust skaner + tokenizator), hisobot
  diff.py             strukturali taqqoslash: kalit / tekislash / o'rin / hujjat rejimlari
  smartvalues.py      vaqt, sana, URL, rang, base64 rasmlarni aniqlash (Toshkent vaqti UTC+05:00)
  cli.py              buyruq qatori
  i18n.py             barcha UI matnlari (o'zbekcha asosiy, inglizcha zaxira) — bitta lug'at fayli
  gui/                PySide6: virtual grid (100 mln+ qator), daraxt/jadval/matn/xom/tuzilma ko'rinishlari,
                      sessiya (filtr -> tartib -> ko'rinish), fon vazifalari, panellar; tahrir boshqaruvchisi
                      (editing.py), tahrir oynalari (editors.py), ommaviy amallar (bulkdialog.py), tuzilma
                      quruvchi (mapview.py, mapmodel.py), statistika (statspanel.py), dublikatlar
                      (dupesdialog.py), JSON Schema (validatepanel.py), ta'mirlash (repairdialog.py),
                      bo'lish/birlashtirish (filetoolsdialog.py), taqqoslash (difftree.py, diffview.py,
                      comparewindow.py), buyruqlar palitrasi (palette.py), tugmalar (keymap.py), sessiya va
                      workspace (sessionstore.py, workspace.py), Windows «Open with» (osintegration.py)
packaging/            PyInstaller spec, ishga tushirish skriptlari, versiya ma'lumoti (scripts/build_exe.ps1)
tests/                pytest (+ hypothesis) testlari
bench/                sintetik katta fayllar generatori va benchmark
```

<a id="testlar"></a>
## 13. Testlar

```powershell
.venv\Scripts\python -m pytest -q                                  # Rust yadrosi bilan
$env:JSONRESHAPE_BACKEND="python"; .venv\Scripts\python -m pytest -q  # sof Python fallback bilan
.venv\Scripts\python -m pytest -q -m bench                         # 2 GB benchmark (test 25) + GUI benchmark
.venv\Scripts\python -m pytest -q -m gui                           # faqat GUI testlari (ekransiz, pytest-qt)
.venv\Scripts\python -m pytest -q -m packaging                     # tayyor .exe ustida (avval scripts\build_exe.ps1)
```

To'liq to'plam: **12 660 ta test** (6-bosqich oxirida, Rust yadro bilan, ~47 daqiqa; 5-bosqich oxirida 12 238 ta,
2-bosqich oxirida 5431 ta edi); sof Python yadro bilan GUI'siz qismi — 9 208 ta test (~20 daqiqa). `-m packaging`
testlari `dist\jsonreshape` bo'lmasa o'tkazib yuboriladi. GUI testlari ekransiz
(`QT_QPA_PLATFORM=offscreen`) ishlaydi. Qulash va xotira testlari alohida jarayonlarda bajariladi (qulash
pytest'ni to'xtatmaydi, test xato bo'ladi).

AGENT_PROMPT §14 bo'yicha 1-bosqich qabul testlari:

| № | Mazmun | Test fayllari |
|---|---|---|
| 11 | har `(start, end)` bo'lagi asl yozuv bilan bir xil, barcha formatlar | `test_reader_formats.py` |
| 12 | Rust yadro va Python fallback natijasi aynan bir xil | `test_core.py`, `test_core_fuzz.py`, `test_core_rust_smoke.py` |
| 13 | xato joyi (qator/ustun), kesilgan fayl | `test_reader_errors.py` |
| 14 | 1 mln elementli tugun xotirani to'ldirmaydi | `test_nodes.py` |
| 15 | qidiruv natijasi to'liq parse + oddiy tekshiruv bilan aynan bir xil | `test_search.py` |
| 16 | apostroflar va lotin↔kirill | `test_textmatch.py`, `test_search.py` |
| 17 | tartib barqaror, turlar, tabiiy tartib, tashqi sort = xotiradagi | `test_sortkeys.py`, `test_sort_runner.py` |
| 18 | filtr ifodasi = vizual quruvchi, `eval` yo'q | `test_query.py`, `test_filter_runner.py`, `test_static.py` |
| 22 | JSON → JSONL → JSON (`keyed` fayl `--as-keyed` bilan ham), CSV/XLSX ustunlari, XLSX bo'linishi | `test_writer.py`, `test_keyed_rebuild.py` |
| 24 | raqam aniqligi va Unicode | `test_codec.py`, `test_numbers_e2e.py`, `test_cli.py` |
| 25 | 2 GB fayl: indeks, qidiruv, tartib, eksport vaqti va peak RSS | `test_bench.py` (`-m bench`) |

2-bosqich (ko'ruvchi GUI) testlari:

| № | Mazmun | Test fayllari |
|---|---|---|
| 26 (2-bosqich qismi) | faylni ochish (buyruq qatori, sudrab tashlash), daraxtni ochish/yopish, qidiruv natijasiga o'tish, jadvalda tartiblash, filtr, yo'lga o'tish va tarix, eksport tugmasi, yopish — haqiqiy oyna orqali (to'liq ssenariy — 6-bosqich jadvalida) | `test_gui_e2e.py` |
| — | har ko'rinish va panel: daraxt, jadval, matn, xom, tuzilma, qidiruv, filtr, tartib, eksport, sozlamalar, navigatsiya, holat qatori, fon vazifalari | `test_gui_*.py` (37 ta fayl) |
| — | virtual grid (10¹² qator), FlatTree (hypothesis model testlari), yuklovchi, sessiya | `test_gui_vgrid.py`, `test_flattree.py`, `test_gui_loader.py`, `test_gui_session.py` |
| — | ko'rib chiqishda topilgan xatolar uchun regressiya testlari (qulash, xotira oqishi, GIL, katta matn) | `test_gui_fix_*.py`, `test_schema_walk.py`, `test_recordset_locks.py` |
| 25 (GUI) | katta fayllarda GUI: birinchi yozuvlar, indeks, aylantirish, 1 mln bolali tugun, qidiruv, filtr, tartib, eksport, RAM | `test_bench_gui.py` (`-m bench`), `bench/gui_bench.py` |

3-bosqich (tahrirlash) testlari:

| № | Mazmun | Test fayllari |
|---|---|---|
| 19 | tahrirlar bilan saqlash va qayta ochish (barcha 7 format, kodirovkalar, gzip, BOM), asl fayl o'zgarmaydi, undo/redo to'liq qaytaradi | `test_saver*.py` (8 ta fayl), `test_edits*.py`, `test_overlay*.py`, `test_jsonpatch*.py`, `test_cli_patch.py` |
| 20 | ommaviy amal oldidan berilgan hisob = haqiqiy o'zgarishlar (oddiy etalon bilan solishtirish, tahrirlangan hujjatlarda ham) | `test_bulk.py`, `test_bulk_matrix*.py`, `test_bulk_scopes.py` |
| 21 | avtosaqlash va qulashdan keyin tiklash: jarayonni saqlash/commit'ning har bosqichida haqiqatan o'ldirib, keyin tiklash; buzilgan qatlam aniqlanadi | `test_edits_crash.py`, `test_editstore_crash.py`, `test_fix3_journal.py`, `test_fix3_layer.py`, `test_gui_editing_recovery.py` |
| — | GUI tahriri: daraxt, jadval, matn, sudrab tashlash, tahrir oynalari, boshqaruvchi, ko'rib chiqishda topilgan xatolar | `test_gui_*edit*.py`, `test_gui_editors.py`, `test_gui_bulkdialog*.py`, `test_fix3_ctl*.py`, `test_fix3_gui.py` |

4-bosqich (tuzilma quruvchi) testlari:

| № | Mazmun | Test fayllari |
|---|---|---|
| 1–9 | identity, rekursiv tekislash, ko'chirish, unwrap, gather, broadcast, guruh, birlashtirish, sudrab tashlash qoidalari | `test_mapping.py`, `test_engine.py`, `test_mapping_golden.py` |
| 10 | etalon bilan solishtirish: `node` orqali `json-tuzilma.html` engine'i ishga tushirilib golden natijalar yasaladi; Python natijasi aynan bir xil (node bo'lmasa — saqlangan goldenlar bilan) | `test_mapping_golden.py`, `tests/golden/` |
| — | tasodifiy tahrir skriptlari bilan differensial fuzz (oddiy port, kompilyatsiya qilingan engine, etalon), oqimli qo'llash har format va jarayonlar havzasi bilan, CLI `apply`/`flatten`, GUI quruvchi | `test_mapping_random.py`, `test_engine_apply.py`, `test_mapping_document.py`, `test_cli_mapping.py`, `test_gui_mapview*.py`, `test_gui_mapmodel.py` |

5-bosqich (asboblar) testlari:

| № | Mazmun | Test fayllari |
|---|---|---|
| 22 | JSON → JSONL → JSON aylanmasi, CSV/XLSX ustunlari va bo'linishi; bo'lish → birlashtirish aylanmasi qiymatma-qiymat bir xil | `test_writer*.py`, `test_keyed_rebuild.py`, `test_filetools_*.py` |
| 23 | ta'mirlash: har bir buzilish turi uchun test, hisobot (joyi, sababi, kodlari) to'g'ri, natija doim to'g'ri JSON, har o'zgargan bayt hisobotda | `test_repair.py`, `test_repair_props.py`, `test_cli_repair.py` |
| — | statistika (oddiy etalon bilan aniqlik, sketch xatolik chegarasi), dublikatlar, JSON Schema (rasmiy test to'plami + `jsonschema` bilan differensial), diff (rejimlar, aniq sonlar), aqlli qiymatlar, CLI buyruqlari, GUI oynalari | `test_stats*.py`, `test_dupes*.py`, `test_jschema*.py`, `test_diff*.py`, `test_smartvalues.py`, `test_cli_*.py`, `test_gui_statspanel.py`, `test_gui_dupesdialog.py`, `test_gui_validatepanel.py`, `test_gui_repairdialog.py`, `test_gui_filetoolsdialog.py`, `test_gui_diffview.py`, `test_gui_comparewindow.py`, `test_fix5_*.py` |

6-bosqich (silliqlash) testlari:

| № | Mazmun | Test fayllari |
|---|---|---|
| 26 | butun ssenariy haqiqiy oynada klaviatura va sichqoncha bilan: ochish, daraxtni ochish/yopish, qidiruv natijasidan tugunga, jadval sarlavhasi bilan tartib, Enter bilan tahrir, Ctrl+Z / Ctrl+Y, Ctrl+S (disk baytma-bayt, `.bak`), eksport tugmasi, buyruqlar palitrasi, qayta ishga tushirishda sessiya tiklanishi | `test_gui_e2e.py::test_acceptance_26_the_whole_scenario` |
| — | buyruqlar palitrasi (yig'ish, lotin/kirill qidiruv, tartib, o'chiq buyruqlar), tugmalarni sozlash (band va qat'iy tugmalar rad etiladi) | `test_gui_palette.py`, `test_gui_keymap.py` |
| — | sessiyani tiklash va workspace (model, `.jrworkspace` tekshiruvi, nisbiy yo'llar, ochilgan tugunlar, qayta ishga tushirish) | `test_sessionstore.py`, `test_gui_session_restore.py`, `test_workspace.py`, `test_gui_workspace.py` |
| — | Windows «Open with» (soxta reestr: faqat HKCU, olib tashlash faqat o'zi yozganini o'chiradi) | `test_osintegration.py` |
| — | mavzular: mavzu tashqarisidagi ranglarni taqiqlovchi statik test, kontrast, qorong'i rejimda 3–5-bosqich oynalari | `test_theme_colours.py` |
| — | `.exe`: muzlatilgan dasturda ishchi jarayonlar, tayyor exe ustida smoke-testlar (`-m packaging`, `dist\jsonreshape` bo'lsa) | `test_frozen_spawn.py`, `test_packaging.py` |
| — | P6 unumdorlik: CSV ishchi jarayonlari (bitta oqim bilan baytma-bayt bir xil), qatlam nazorat yig'indilari parallel, benchmark skriptlari | `test_perf6_*.py`, `test_bench_tools.py`, `test_bench.py` |

<a id="olchovlar"></a>
## 14. O'lchovlar (benchmark)

Yakuniy o'lchov — 2026-10-19, 6-bosqich oxiridagi kod bilan; kompyuterda boshqa agent yoki og'ir ish yo'q edi
(o'lchovlardan oldingi tizim yuklamasi ~2–17 %, o'rtacha ~6 %). To'liq natijalar, har bir takror, o'lchash shartlari
va kompyuter tavsifi: yadro, CLI va mapping — [`bench/results/latest.md`](bench/results/latest.md); GUI —
[`gui_latest.md`](bench/results/gui_latest.md); tahrirlar — [`edit_latest.md`](bench/results/edit_latest.md);
jadval eksportlari (CSV, XLSX, SQLite) — [`export_latest.md`](bench/results/export_latest.md); 5-bosqich asboblari —
[`tools_latest.md`](bench/results/tools_latest.md). Qayta o'lchash: `.venv\Scripts\python bench\run_bench.py`,
`bench\gui_bench.py`, `bench\edit_bench.py --work PAPKA`, `bench\tools_bench.py --work-dir PAPKA`.

Kompyuter: Intel Core Ultra 9 285 (24 yadro: 8 P + 16 E), 205 GB RAM, ikkita NVMe SSD (ma'lumot fayllari E: da,
chiqish fayllari C: da — u 89 % to'la, katta fayl yozilganda `fsync` ~2–7 s), Windows 11, Python 3.14, Rust yadro.
Har amal alohida yangi jarayonda, yangi bo'sh `JSONRESHAPE_HOME` bilan; ma'lumot fayllari OS keshida (iliq kesh),
bitta sovuq o'qish alohida o'lchandi. **RAM — butun dastur**: ishchi jarayonli amallarda asosiy jarayon va ishchi
jarayonlar ishchi to'plamlarining bir paytdagi yig'indisi (har 20 ms namuna) yoki bitta jarayonning aniq peak'i —
qaysi biri katta bo'lsa. §12 maqsadlari 5 GB fayl va «oddiy noutbuk» uchun: tezliklar fayl hajmiga bog'liq emas
(5.4 GB massiv ham o'lchandi), noutbukda CPU'ga bog'liq amallar sekinroq bo'ladi.

O'lchovlar oxirida bitta tuzatish kiritildi: kichik fayllar (mapping, JSON Schema, workspace, sozlamalar, GUI holati,
tahrir qatlamlari ro'yxati) endi fayl hajmicha o'qiladi (`fileio.read_limited`; avval `read(chegara + 1)` har safar
chegara hajmidagi buferni — mapping uchun 128 MiB — ajratib, Windows'da ishchi to'plamni shuncha oshirardi).
`apply` qatorlari va asboblar benchmarki shu tuzatishdan keyin o'lchandi; qolgan o'lchovlarga u faqat xotirani
kamaytirishi mumkin (tahrir qatlamini tiklashda ≤ 17 MB).

### Yadro va CLI

| Amal (AGENT_PROMPT §12) | Maqsad | Natija | Holat |
|---|---|---|:---:|
| Ochish, birinchi yozuvlar ko'rinishi (2.1 GB; 5.4 GB; sovuq disk) | < 1 s | 11 ms; 12 ms; 36 ms | ✅ |
| To'liq indeks, Rust yadro (2.1 GB massiv; 5.4 GB; 3.3 GB JSONL) | ≥ 150 MB/s | 725; 634; 666 MB/s (sovuq disk: 805 MB/s) | ✅ |
| 1 000 000 bolali tugunni ochish (virtual) | < 200 ms | 95 ms (keshdan qayta: 4.3 ms) | ✅ |
| To'liq matn qidiruvi, bayt rejimi: kam uchraydigan so'z (2.1 GB; 5.4 GB) | ≥ 100 MB/s, natijalar darhol | 745; 854 MB/s, birinchi natija 11–12 ms | ✅ |
| … o'rtacha so'z «Toshkent» (6 % yozuvda) | ≥ 100 MB/s | 621 MB/s, birinchi natija 12 ms | ✅ |
| … ko'p uchraydigan so'z «ko'chasi» (60 % yozuvda) | ≥ 100 MB/s | 272 MB/s, birinchi natija 14 ms | ✅ |
| 10 000 000 yozuvni bitta maydon bo'yicha tartiblash **va natijani yozish** (`jsonreshape sort` kabi, ochishdan oxirigacha) | < 2 daqiqa, RAM < 1 GB | 77.6 s, peak 379 MB (indeks 5.1 s, tartiblash 22.9 s, tartiblangan eksport 66.3 MB/s) | ✅ |
| Eksport JSON → JSONL / JSON (indent 2), 2.1 GB, mapping va tahrirlarsiz | ≥ 50 MB/s, RAM < 300 MB | 180 / 214 MB/s, peak 89 / 71 MB | ✅ |
| Eksport CSV: avtomatik ustunlar / 9 ta skalyar ustun (4 ta ishchi jarayon) | ≥ 50 MB/s, RAM < 300 MB | 105 / 224 MB/s, RAM 226 / 222 MB (private 175 / 167 MB) | ✅ |
| Dasturning umumiy xotirasi (tartiblashdan boshqa barcha amallarning eng kattasi, ishchi jarayonlari bilan) | < 500 MB | 299 MB (`flatten`, 3 ta ishchi jarayon bilan; private 246 MB) | ✅ |

Qo'shimcha: filtr (extract) 207 MB/s; filtr + natijani yozish (2.1 GB, ochishdan oxirigacha) 16.9 s; tuzilma namunasi
(10 000 yozuv) 176 ms; keshdagi indeks bilan qayta ochish 9.2 ms; sof Python yadro bilan indeks 70.8 MB/s (zaxira
maqsadi ≥ 15 MB/s). 2-bosqichdagi «CSV avtomatik ustunlar 26 MB/s» kamchiligi 6-bosqichda yopildi: qatorlar va
ustunlarni aniqlash ishchi jarayonlarda (natija bitta oqimdagi bilan baytma-bayt bir xil).

### Tuzilma quruvchi: mapping bilan eksport (`jsonreshape apply` / `flatten`, 2.1 GB → 3.3–3.7 GB)

Mapping — `bench/mapping_syn.json`: 10 000 yozuvli namunadan olingan daraxt + 6 qadam (`contact.address.city`
yozuv darajasiga, `contact` ochildi, `orders[].oid` yig'ildi, `tags` o'chirildi, `score` → `ball`, yangi `holat`
guruhi). Tezlik — buyruqning indeksdan keyingi qismi (GUI'da indeks fayl ochilganda quriladi; indeks 2.8–3.2 s).

| Amal | Maqsad | Natija | Holat |
|---|---|---|:---:|
| `apply` → JSON (indent 2), ishchi jarayonlar avtomatik (3 ta) | ≥ 50 MB/s, RAM < 300 MB | 97.5 MB/s; RAM 264 MB (4 jarayon, private 242 MB) | ✅ |
| `apply` → JSONL, avtomatik (3 ta) | ≥ 50 MB/s, RAM < 300 MB | 101 MB/s; RAM 258 MB (private 238 MB) | ✅ |
| `apply --workers 1` (bitta jarayon) | ≥ 50 MB/s, RAM < 300 MB | 47.5 MB/s; RAM 101 MB | ❌ (tezlik) |
| `flatten --children-key children` → JSON, avtomatik (3 ta) | ≥ 50 MB/s, RAM < 300 MB | 129 MB/s; RAM 299 MB (private 246 MB) | ✅ * |
| `flatten --workers 1` | ≥ 50 MB/s, RAM < 300 MB | 46.0 MB/s; RAM 141 MB | ❌ (tezlik) |

Ishchi jarayonlar soni xotira chegarasidan hisoblanadi (standart 512 MiB bilan 3 ta; qaror 40): 4 ta bilan `apply`
13–20 % tezroq (113–127 MB/s), `flatten` esa o'sha tezlikda (127 MB/s) edi, lekin butun dastur 321–355 MB bo'lardi. \* `flatten`'ning
299 MB'idan ~55 MB — 10 000 yozuvli namunani o'qishda xotiraga proyeksiyalangan kirish fayli sahifalari (OS kerak
bo'lsa darhol bo'shatadi; asosiy jarayonning xususiy xotirasi `apply`'dagi bilan bir xil, ~77 MB).

### Tahrirlar bilan eksport va saqlash (3-bosqich; 2.1 GB, 6.5 mln yozuv)

To'rt holat: 100 ta nuqtaviy tahrir; har 3-yozuv o'zgargan; hamma yozuv o'zgargan; 10 % yozuv o'chirilgan + 1 000 ta
qo'shilgan (ommaviy amallar bilan yasalgan). «Oldin» ustuni (3-bosqichning unumdorlik ishidan oldingi kod) va har
qator — `edit_latest.md` da.

| Amal | Maqsad | Natija (to'rt holat) | Holat |
|---|---|---|:---:|
| Eksport JSON (indent 2) | ≥ 50 MB/s, RAM < 300 MB | 147; 84.5; 118; 101 MB/s; RAM 73–135 MB | ✅ |
| Eksport JSON Lines | ≥ 50 MB/s, RAM < 300 MB | 227; 105; 139; 115 MB/s; RAM 91–126 MB | ✅ |
| Eksport CSV (9 ta skalyar ustun; 4 ta ishchi jarayon) | ≥ 50 MB/s, RAM < 300 MB | 231; 187; 199; 186 MB/s; RAM 222–244 MB | ✅ |
| Boshqacha saqlash (Save As) | ≥ 50 MB/s, RAM < 300 MB | 458; 111; 272; 309 MB/s; RAM 57–111 MB | ✅ |
| Ommaviy amal ko'rib chiqishi, 6.5 mln yozuvning hammasi: set_field, replace, rename_key, delete_field, convert_type | ≥ 50 MB/s, dastur < 500 MB | 102–151 MB/s; RAM 294–303 MB | ✅ |
| … clean_strings (hech narsa o'zgarmaydi) / dedupe (id bo'yicha) | dastur < 500 MB | 193 / 162 MB/s; RAM 234 / 238 MB | ✅ |
| JSON Patch eksporti / importi (yangi sessiyaga), hamma yozuv o'zgargan — 6.5 mln amal | dastur < 500 MB | 18.4 s (121 MB/s) / 132 s (49 197 amal/s); RAM 358 / 422 MB | ✅ |
| Ochish + qatlamni tiklash (keshdagi indeks): 100 tahrir; hamma yozuv o'zgargan | — | 22 ms; 423 ms | — |
| Undo / redo (hamma yozuv o'zgargan holatda) | — | 6 / 3 ms | — |
| Dasturning umumiy xotirasi (barcha tahrir amallari) | < 500 MB | 422 MB (JSON Patch importi, 6.5 mln amal) | ✅ |

**Jadval eksportlari** (`export_latest.md`): CSV avtomatik ustunlar bilan — tahrirsiz 117 MB/s, tahrirlar bilan
84–102 MB/s ✅; **XLSX** (9 ta ustun) 12.0 MB/s (hamma yozuv o'zgarganda 7.3 MB/s) va **SQLite** 16.4 MB/s (8.9 MB/s),
RAM 138–159 MB — tezlik maqsadidan past ❌ (sabab va variantlar pastda).

### GUI

Har o'lchov alohida jarayonda, haqiqiy oyna (`jsonreshape.gui.app.main`) bilan, Qt offscreen platformasida;
«UI javob berishi» — GUI oqimidagi 5 ms taymer urishlari orasidagi eng uzun oraliq (foydalanuvchi bosgan tugma
shuncha kutadi).

| Amal | Maqsad | Natija | Holat |
|---|---|---|:---:|
| Jarayon ishga tushganidan daraxtda birinchi yozuvlar chizilguncha (2.1 GB; 5.4 GB; 3.3 GB JSONL) | < 1 s | 641; 627; 621 ms (`main()` dan: 463; 456; 457 ms) | ✅ |
| To'liq indeks GUI ichida, UI javob berib turadi (2.1 GB; 5.4 GB; 3.3 GB JSONL) | ≥ 150 MB/s | 772; 749; 722 MB/s; UI max oraliq 7–10 ms | ✅ |
| Skrollbarni oxiriga sakratish (6.5 mln; 16 mln; 10 mln qator) | — | 5.8; 5.6; 5.6 ms | — |
| 1 000 000 bolali tugunni ochish | < 200 ms | birinchi marta 104 ms (bolalar indeksi 98 + chizish 6); qayta 3.0 ms | ✅ |
| To'liq qidiruv (qidiruv paneli): kam uchraydigan so'z; «Toshkent» | ≥ 100 MB/s, birinchi natija < 1 s | 1 086 MB/s, 46 ms; 818 MB/s, 20 ms | ✅ |
| Filtr 10 mln yozuvda (filtr paneli) | — | birinchi qisman natijalar 143 ms; hammasi 14.0 s (236 MB/s); UI max 12 ms | — |
| 10 mln yozuvni `name` bo'yicha tartiblash (GUI orqali) | < 2 daqiqa, RAM < 1 GB | 17.2 s, peak RSS 470 MB; UI max 23 ms | ✅ |
| Tartiblangan ko'rinishni JSONL'ga eksport (eksport oynasi) | ≥ 50 MB/s, RAM < 300 MB | 79.7 MB/s; xususiy xotira 127 MB (RSS 429 MB) | ✅ * |
| 1 mln elementli tugunni matn panelida ko'rsatish | — | 0.78 s, RAM +78 MB | — |
| Butun GUI sessiyasi, tartiblashsiz (ochish, indeks, skroll, jadval, qidiruv, filtr, tuzilma, matn, eksport) | < 500 MB | peak RSS 290 MB; tab yopilgach 147 MB (ochishdan oldin 116 MB) | ✅ |

\* 2026-10-18 qarori bo'yicha tartiblangan eksportning «RAM < 300 MB» maqsadi jarayonning xususiy xotirasi bilan
o'lchanadi: RSS'ning qolgan qismi — tartib bo'yicha o'qilgan indeks (`.jri`, 160 MB) va RecordSet (`.u64`, 80 MB)
fayllarining xotiraga proyeksiyalangan sahifalari (OS kerak bo'lsa darhol bo'shatadi). `gui_latest.md` jadvali RSS
bo'yicha ❌ ko'rsatadi.

Test 26 (`tests/test_gui_e2e.py::test_acceptance_26_the_whole_scenario`) haqiqiy oynada butun ssenariyni
klaviatura va sichqoncha bilan bajaradi: faylni ochish, daraxtni ochish/yopish, qidiruv natijasidan tugunga o'tish,
jadval sarlavhasi bilan tartiblash, Enter + yozish + Enter bilan tahrir, Ctrl+Z / Ctrl+Y, Ctrl+S (diskdagi fayl
baytma-bayt tekshiriladi, `.bak` eski nusxa), eksport tugmasi, buyruqlar palitrasi (Ctrl+Shift+P, «saqlash», Enter)
va `main()` qayta ishga tushirilganda sessiyaning tiklanishi.

### Asboblar (5-bosqich; `jsonreshape <buyruq>`, 2.1 GB, 6.5 mln yozuv)

§12 asboblar uchun faqat umumiy xotira maqsadini qo'yadi; tezlik — buyruqning indeksdan keyingi qismi (indeks
2.6–2.7 s, ikki faylli buyruqlarda 5.5 s).

| Amal | Maqsad | Natija | Holat |
|---|---|---|:---:|
| Statistika: 1 maydon; 4 maydon; aniq hisoblash (4.6 mln xil qiymat) | dastur < 500 MB | 556; 124; 108 MB/s; RAM 81; 105; 197 MB | ✅ |
| Takroriy yozuvlar (`--key contact.phone`, 1.5 mln guruh) | dastur < 500 MB | 170 MB/s; RAM 263 MB | ✅ |
| JSON Schema: barcha yozuvlardan yaratish; tekshirish | dastur < 500 MB | 42.5; 74.0 MB/s; RAM 81 MB | ✅ |
| Bo'lish 4 qismga: bayt nusxa; JSON Lines'ga | dastur < 500 MB | 758; 288 MB/s; RAM 65; 102 MB | ✅ |
| Birlashtirish, `--dedupe-by id` (2 × 2.1 GB) | dastur < 500 MB | 117 MB/s; RAM 264 MB | ✅ |
| Ajratish (`--gather "orders[*]"`, 9.8 mln qiymat) | dastur < 500 MB | 383 MB/s; RAM 87 MB | ✅ |
| Ta'mirlash: buzilmagan fayl; 1 % yozuv buzilgan (78 160 tuzatish) | dastur < 500 MB | 335; 217 MB/s; RAM 102; 69 MB | ✅ |
| Taqqoslash: kalit bo'yicha; o'rin bo'yicha; mazmun bo'yicha tekislab (2 × 190 MiB) | dastur < 500 MB | 99.6; 203; 178 MB/s; RAM 402; 97; 169 MB | ✅ |

### Maqsadga yetilmagan joylar va variantlar

* **XLSX eksporti** 12.0 MB/s (hamma yozuv o'zgarganda 7.3 MB/s) va **SQLite eksporti** 16.4 MB/s (8.9 MB/s) — maqsad
  ≥ 50 MB/s; xotira maqsad ichida (138–159 MB). Sabab: XLSX kataklarini `xlsxwriter` bitta oqimda XML'ga yozadi
  (cProfile bo'yicha vaqtning ~80 %; qiymatlarni tayyorlash allaqachon ishchi jarayonlarda), SQLite'ga yozuvlar ikki
  o'tishda Python'da jadvallarga bo'linib, bitta oqimda kiritiladi. Variantlar: 1) o'z XLSX yozuvchimiz — varaq
  XML'i ishchi jarayonlarda, zip alohida oqimda (qaror 39); 2) SQLite uchun map'larni topish va qatorlarni
  tayyorlashni ishchi jarayonlarga o'tkazish; hozircha katta jadvallar uchun CSV (100–250 MB/s).
* **Mapping bitta jarayonda** (`--workers 1`): `apply` 47.5 MB/s, `flatten` 46.0 MB/s — maqsadga yaqin, lekin
  past. Standart rejimda 64 MB dan katta fayl parallel qayta ishlanadi (yuqoridagi jadval). Variant: mapping
  qoidalarini Rust yadroda bajarish.
* 6-bosqichda yopilganlar: CSV avtomatik ustunlar bilan (26 → 105 MB/s), tahrirli CSV eksporti (28–41 → 186–231
  MB/s), mapping bilan eksportning xotirasi (avtomatik 4 ta ishchi jarayon bilan 321–355 MB → 3 ta bilan
  258–299 MB; mapping faylini o'qishdagi 134 MB'lik sakrash olib tashlandi).

<a id="qarorlar"></a>
## 15. Qabul qilingan qarorlar va ochiq savollar

### Qabul qilingan qarorlar (o'zgartirish mumkin)

1. **Yozuv raqamlari 0 dan** boshlanadi (`#0` — birinchi yozuv, `$index`), JSON massiv indekslari bilan bir xil.
2. **`keyed` format**: yozuv = yuqori darajadagi a'zo; filtr, tartib va qidiruv yo'llari qiymatga nisbatan,
   a'zoning kaliti `$key`. JSON'ga eksportda kalitli obyekt saqlanadi, JSONL'da esa `{"kalit": qiymat}` qatorlari;
   bu qatorlardan kalitli obyekt `--as-keyed` bilan qayta tiklanadi.
3. **Format aniqlash** evristik (4-bo'lim); noto'g'ri bo'lsa `--format` / `--record-path` bilan tuzatiladi.
   `inspect` topilgan barcha massiv yo'llarini ko'rsatadi.
4. **O'zbek kollatsiyasi** (standart): rasmiy alifbo tartibi — `oʻ gʻ sh ch ng` alohida harflar, `z` dan
   keyin; lotin harflari kirilldan oldin; katta-kichik harf ikkinchi darajada; raqamlar "tabiiy" tartibda.
5. **Aralash turlarni tartiblash**: sonlar < satrlar < mantiqiy < massivlar < obyektlar (massiv va obyektlar
   ixcham JSON matni bo'yicha); `null` va yo'q qiymatlar standart bo'yicha oxirida.
6. **Filtr semantikasi**: massiv yo'llari "kamida bittasi"; `!=` = `not (==)`; turlar majburan
   o'zgartirilmaydi; `is null` — maydon bor va `null`; sonlar aniq qiymat bo'yicha.
7. **Qidiruv**: apostrof variantlari doim teng; sonlar, `true/false/null` ham JSON matni bo'yicha qidiriladi;
   `--number` faqat sonlarga qo'llanadi.
8. **Raqamlar**: JSON/JSONL'da baytma-bayt; XLSX'da 15 dan ortiq ma'noli raqamli sonlar matn; SQLite'da
   aniq saqlanmaydiganlari `TEXT` (har biri ogohlantirish bilan).
9. **CSV**: UTF-8 BOM bilan va `\r\n` qator oxiri (Excel uchun qulay; `--no-bom`).
10. **`--sort-keys`** standart bo'yicha alifbo (o'zbek) tartibida; `--sort-keys codepoint` — kod nuqtasi tartibi.
11. **Ulkan yozuvlar** (> 50 MB) ijson oqimi bilan ishlanadi; faqat shu yozuvlarda `-0` → `0` va eksponent
    ko'rinishi (`1e400` → `1E+400`) farq qilishi mumkin (qiymat bir xil).
12. **Kirish fayli o'zgarsa** — natija saqlanmaydi (kod 2); faqat oxiriga qo'shilsa — ochilgan paytdagi holat
    ishlatiladi va ogohlantirish chiqadi.
13. **GUI ro'yxatlari o'zimizning virtual vidjetimiz** (`gui/vgrid.py`): Qt'ning tayyor jadval/daraxtlari har
    qator uchun xotira oladi va 2³¹ dan ortiq qatorni ko'tarmaydi.
14. **GUI'da Python chiqindi yig'uvchisi faqat GUI oqimida** ishlaydi va GIL almashish oralig'i 0.5 ms —
    ko'rib chiqishda topilgan qulash (fon oqimidagi GC Qt obyektini yo'q qilishi) va "qotishlar" shu bilan
    tuzatildi. Og'ir fon vazifalari (filtr, tartib, eksport, to'liq tuzilma) bir vaqtda ko'pi bilan 2 ta,
    qolganlari navbatda kutadi.
15. **Matn ko'rinishi** 4 MB gacha chiroyli matn ko'rsatadi (Qt hujjati matndan ~20 baravar ko'p xotira oladi);
    kattaroq tugunlar uchun boshi va «Xom ko'rinishda ochish».
16. **Qidiruv natijasi ochilgan bo'lsa** filtr va tartib o'sha yozuvlar ustida ishlaydi; filtr/tartib hali
    ishlayotganda to'liq qidiruv boshlanmaydi (natija yarim ko'rinish bo'yicha bo'lib qolmasligi uchun).
17. **Eksport fonda** ishlaydi va dialog yopilsa ham, fayl qayta ochilsa ham oxirigacha davom etadi.

**3-bosqich — tahrirlash**

18. **Tahrir qatlami** dastur papkasida: `%LOCALAPPDATA%\jsonreshape\edits\` (har doim yozsa bo'ladi); sozlamada
    «fayl yonida» (`fayl.json.jredit\`) tanlovi bor — u yerga yozib bo'lmasa, dastur papkasi ishlatiladi va aytiladi.
19. **`.bak` zaxira standart bo'yicha yoqilgan.** U qattiq havola (hard link) bilan bir zumda yaratiladi; diskda
    eski va yangi nusxa birga turadi (har qanday atomik saqlashdagi kabi). Sozlamada o'chiriladi.
20. **Enter = tahrirlash** (ochish/yopish → va ← da qoladi); xatcho'p **Ctrl+B** ga ko'chdi, Ctrl+D — «nusxasini
    qo'shish» (AGENT_PROMPT §8.1).
21. **Tashqi yozuv raqamlari = tahrirlangan hujjatdagi o'rin** (`#0..N-1`, zich); qatlam ichida yozuvlarning
    o'zgarmas identifikatorlari bor. Qo'shish/o'chirish/surishdan keyin filtr, tartib, qidiruv natijalari va
    tanlov yangi o'rinlarga o'tkaziladi.
22. **Ommaviy amal natijasi «Ko'rib chiqish» paytida hisoblanadi** va «Qo'llash» aynan o'sha natijani qatlamga
    qo'shadi (AGENT_PROMPT §8.3 dagi «saqlashda qo'llanadi» o'rniga): oldindan sanalgan son qo'llangan o'zgarish
    bilan doim teng (test 20), ko'rinishlar, qidiruv va filtr natijani darhol ko'radi. Narxi: diskda ≈ o'zgargan
    yozuvlar hajmi (bo'sh joy oldindan tekshiriladi).
23. **Saqlash faylning o'z turi, kodirovkasi va siqilishini saqlaydi**; `brace_list` (`{...},{...}`) JSON massiv
    bo'lib saqlanadi, JSONL'dagi buzuq qatorlar olib tashlanadi, zaxirasiz saqlashda gzip oxiridagi ortiqcha
    ma'lumot tashlab yuboriladi — har biri avval so'raladi.
24. **Uzilgan saqlashni yakunlash dalilga asoslanadi** (fayl identifikatori NTFS/ReFS'da, o'zgarish vaqti, hajm,
    hash'lar); qaror aniq bo'lmasa — «ziddiyat»: hech narsa o'chirilmaydi, tegishli fayllar nomma-nom aytiladi.
25. **50 MB dan katta bitta yozuv tahrirlanmaydi** (xotirada tahrirlab bo'lmaydi); bunday bitta hujjat yoki
    o'rama faqat o'qish uchun. Ommaviy amallar bunday yozuvlarni o'tkazib yuborib sanaydi.
26. **Kutilgan foydalanuvchi xatolari** (mavjud kalit, mumkin bo'lmagan tur o'zgartirish, buferdagi noto'g'ri JSON)
    xato oynasida emas, tahrir maydonida yoki holat qatorida aytiladi; xato oynasi — haqiqiy nosozliklar uchun.

**4-bosqich — tuzilma quruvchi**

27. **Semantika `json-tuzilma.html` etaloni bilan aynan bir xil** (bog'lash, yig'ish, takrorlash, «yo'q» ≠ `null`,
    kalit to'qnashuvida `_2`, `_3`, `_depth` qoidalari, har bir amalning shartlari va xabarlari). Ataylab farqlar
    (JavaScript cheklovlari): sonlar aniq, kalitlar tartibi saqlanadi, redo bor, tarix 1000 qadam, yozuv raqamlari 0
    dan.
28. **Kalitli fayllar** (millionlab kalitli obyekt) har bir a'zo bo'yicha qayta quriladi (etalon har kalitga alohida
    tugun yaratadi — bu katta faylda ishlamaydi); natijada kalitlar tartib bilan saqlanadi.
29. **Tuzilma namunadan** (standart 10 000 yozuv) yoki «To'liq skanerlash» bilan quriladi; namunada uchramagan
    maydonlar natijaga tushmaydi (etalon qoidasi) — `apply` hisobotida bunday yozuvlar soni aytiladi.
30. **Qo'llash tezligi**: 64 MB dan katta fayllar parallel jarayonlarda (`--workers`), natija tartib bilan yoziladi.

**5-bosqich — asboblar**

31. **Statistika**: xotira byudjeti ichida aniq hisob, oshsa noyob qiymatlar soni HyperLogLog (p=14, ±0,8 %),
    eng ko'p uchraydiganlar Misra–Gries bilan — «≈» belgisi va xatolik chegarasi bilan; «Aniq hisoblash» /
    `--exact` diskka chiqarib aniq sanaydi. Panelning standart doirasi — joriy ko'rinish.
32. **JSON Schema**: o'zimizning draft 2020-12 tekshiruvchimiz (aniq sonlar, ECMA-262 regex, tarmoqsiz,
    `jsonschema` paketidan ~10 baravar tez); `$dynamicRef`, masofaviy `$ref`, maxsus `$vocabulary` aniq xato bilan
    rad etiladi (jimgina e'tiborsiz qoldirilmaydi). Rasmiy JSON-Schema-Test-Suite — muvofiqlik testi. Yaratilgan
    schema standart bo'yicha yumshoq; qat'iy cheklovlar (min/max, enum, `additionalProperties: false`, formatlar) —
    `--strict` bilan.
33. **Yangi chiqish kodi 5** = «ma'lumot mos emas» (`validate` xato topdi, `diff --exit-code` farq topdi);
    0–4 ma'nosi o'zgarmadi.
34. **Bo'lish (split)** har qismda tur, kodirovka, siqilish va o'ramani saqlaydi (`--no-wrapper` — o'ramasiz);
    `--parts N` hajm bo'yicha teng bo'ladi (`--balance records` — yozuvlar soni bo'yicha); `--size` ikkilik
    birliklarda (`500MB` = 500 MiB); nomlar `<nom>_<k><kengaytma>` (`k` 1 dan, N ning xonalari soniga qadar nol
    bilan); hammasi yoki hech narsa (bittasi yozilmasa, eski qismlar tegilmaydi); JSON xatosi bor fayl bo'linmaydi
    (avval ta'mirlang).
35. **Birlashtirish (merge)** yozuvchi orqali (qayta formatlanadi, aniq tokenlar): obyekt ildiz — faqat hamma
    kirishlar kalitli bo'lsa; birinchi kirishning o'ramasi saqlanadi; `--dedupe-by KEY` (`$` — butun yozuv,
    `$key` — a'zo kaliti), standart bo'yicha birinchisi qoladi.
36. **Ta'mirlash** hech qachon joyida emas; NaN/Infinity → `null` (`--nan string` — matn); vergul faqat o'zi
    chegaralangan tokenlar orasida qo'shiladi; qo'shtirnoqsiz kalitlar, Python literallari (`True`, `None`,
    `'...'`), `undefined` tuzatiladi; qo'shtirnoqsiz satr qiymatlari va mos kelmagan yopuvchi qavslar
    tuzatilmaydi; noto'g'ri UTF-8 → U+FFFD (1000 tadan oshsa to'xtaydi); natija turi kirish turi bilan bir xil
    (`brace_list`/oqim → massiv, JSONL — JSONL); tuzatib bo'lmasa hech narsa yozilmaydi (`--allow-partial` —
    xatogacha bo'lgan qism, tashlab yuborilgani hisobotda).
37. **Taqqoslash (diff)**: kanonik tenglik — sonlar aniq qiymat bo'yicha (`1` = `1.0`), kalitlar tartibi e'tiborga
    olinmaydi (`--numbers text` — yozilishi bo'yicha); massivlar standart bo'yicha mazmun bo'yicha tekislanadi (yoki
    indeks, yoki element kaliti bo'yicha — ko'chishlar bilan); `diff_align_max_bytes` (200 MB) gacha fayllar
    kalitsiz to'liq taqqoslanadi, kattaroqlariga `--key` (har qanday hajm) yoki `--by-position` kerak; takroriy
    kalitlar i-chisi i-chisi bilan juftlanadi va ogohlantiriladi.
38. **«Aqlli» qiymatlar**: unix soniya/millisekund va ISO sanalar Toshkent vaqtida (qat'iy UTC+05:00 — 1992 dan
    beri aniq, yozgi vaqt yo'q; `tzdata` qo'shilmadi; eski sanalar uchun izohda aytiladi); URL'lar faqat «Brauzerda
    ochilsinmi?» tasdig'idan keyin ochiladi — dasturning o'zi tarmoqqa hech narsa yubormaydi; `#rgb` / `#rrggbb`
    ranglar namunasi; base64 rasmlar (PNG/JPEG/GIF/WebP) to'liq qiymat oynasida kichik rasm sifatida.
39. **XLSX** eksporti `xlsxwriter` bilan qoldi (~11 MB/s; qiymatlar parallel jarayonlarda tayyorlanadi, kataklarni
    `xlsxwriter` bitta oqimda yozadi); tezroq o'z yozuvchimiz — keyinroq ixtiyoriy.

**6-bosqich — silliqlash**

40. **Parallel jarayonlar avtomatik**: 64 MB dan katta ishlarda mapping qo'llash, ommaviy amal ko'rib chiqishi, JSON
    Patch eksporti, CSV/XLSX qatorlari va ustunlarni aniqlash bir nechta jarayonda bajariladi (xotira chegarasiga
    qarab; standart 512 MiB bilan mapping qo'llashda 3 ta — butun dastur 300 MB dan kam qolishi uchun, qolganlarida
    4 ta), natija tartib bilan va bitta oqimdagi bilan baytma-bayt bir xil; `.exe` ichida ham. `--workers N` bilan
    o'zgartiriladi.
41. **Buyruqlar palitrasi** menyulardan avtomatik yig'iladi (yangi buyruqlar o'z-o'zidan qo'shiladi); qidiruv lotin /
    kirill / apostroflarga sezgir emas.
42. **Tugmalar** faqat oyna darajasidagi buyruqlar uchun o'zgartiriladi; daraxt va jadvalning o'z tugmalari qat'iy
    (ular ko'rinishlarning tugma ishlovchilarida); bitta buyruqqa bitta tugmalar birikmasi (Ctrl+K, Ctrl+S kabi
    ketma-ketliklar yo'q).
43. **Sessiya** standart bo'yicha har doim tiklanadi (fayl berilmasa); qidiruv natijasi ko'rinishi tiklanmaydi (so'rov
    tiklanadi); daraxtdagi faqat-ko'rinish tartiblari va panel o'lchamlari saqlanmaydi.
44. **Workspace** tahrir qatlamini ko'chirmaydi (saqlanmagan o'zgarishlar faylning o'z tiklash paneli orqali
    taklif qilinadi); yo'llar nisbiy va mutlaq ko'rinishda saqlanadi.
45. **«Open with»** faqat joriy foydalanuvchi uchun va standart dasturni o'zgartirmaydi (Windows buni jimgina qilishga
    ruxsat bermaydi; foydalanuvchi o'zi tanlaydi).
46. **`.exe` — bitta papka** (onedir): bitta fayl (onefile) har ishga tushishda ~150 MB ni vaqtinchalik papkaga
    ochadi va sekin ishga tushadi.

### Ma'lum cheklovlar

* gzip va UTF-16 fayllar birinchi yozuv ko'rinishidan oldin to'liq UTF-8 nusxaga o'giriladi (oqim bilan,
  xotira cheklangan; 200 MB ~0.5 s).
* Fayl dasturda ochiq turganda boshqa dastur uni `MoveFileEx`/`os.replace` bilan **ustidan almashtira olmaydi**
  (Windows qoidasi); oddiy yozish, qo'shish, nomini o'zgartirish va o'chirish ishlaydi va aniqlanadi.
* Hajmi va mtime'i saqlangan, faqat hash qilinmagan o'rta baytlarga tegadigan tahrir keshdagi indeks bilan
  sezilmasligi mumkin (`--no-cache` bilan qayta indekslang).
* Bitta juda uzun satr qiymati (yuzlab MB) tekshirilayotganda xotirada ~2 baravar joy oladi.
* Tahrirlash: 50 MB dan katta bitta yozuv va JSON xatosi bor (qisman ochilgan) fayl faqat o'qish uchun; bitta
  faylning saqlanmagan o'zgarishlari boshqa yozilishdagi yo'l (masalan, qisqa 8.3 nom) bilan ochilganda tiklash
  uchun taklif qilinmaydi (tahrir esa baribir bir vaqtda faqat bitta joyda mumkin).
* Tugunni bo'lish yoki tugunni CSV/XLSX/SQLite'ga eksport tahrirlangan va JSONL/oqim fayllarda mumkin emas (avval
  saqlang); qolgan formatlar va doiralar ishlaydi.
* `diff --ignore-order` kalitsiz ishlatilganda yozuvlar butun qiymati bo'yicha juftlanadi va bunda massivlar
  tartibi hisobga olinadi — faqat massiv tartibi farq qiladigan yozuvlar uchun `--key` kerak.
* Ta'mirlash xatolar zich fayllarda sekinroq (har tuzatish aniq joyi va bekor qilish ma'lumoti bilan yoziladi):
  1 % buzuq yozuv ~185 MB/s, 10 % ~47 MB/s, JSON5 uslubi ~35 MB/s, hamma satrlar bitta tirnoqda ~2 MB/s.
* XLSX (~11 MB/s) va SQLite (~15–17 MB/s) eksporti 50 MB/s dan sekin: XLSX kataklarini `xlsxwriter` bitta oqimda
  yozadi, SQLite'ga yozuvlar bitta oqimda kiritiladi (variantlar 14-bo'limda). CSV, JSON va JSONL maqsaddan tez.
* Sessiya va workspace daraxtdagi faqat-ko'rinish tartiblarini, panel o'lchamlarini va tab yo'nalishini saqlamaydi.
* GUI tilini almashtirish to'liq kuchga dastur qayta ishga tushirilgandan keyin kiradi (ochiq oynalardagi
  ba'zi matnlar eski tilda qoladi; sozlamalar oynasi buni aytadi).
* Tezkor qidiruv (Ctrl+F) faqat yuklangan qismda qidiradi (talab bo'yicha); butun fayl uchun Ctrl+Shift+F.
* GUI orqali 10 mln yozuvni tartiblash va tartiblangan ko'rinishni eksport qilishda RSS ~500 MB gacha
  ko'tariladi; buning katta qismi indeks va RecordSet fayllarining xotiraga proyeksiyalangan sahifalari (OS
  kerak bo'lsa bo'shatadi); jarayonning xususiy xotirasi ~110–420 MB.

### Ochiq savollar

Hozircha yo'q. 6-bosqichdagi tanlovlar ham tavsiya bo'yicha qilindi (qarorlar 40–46) va o'zgartirish mumkin.

<a id="muallif"></a>
## 16. Muallif, aloqa va mualliflik huquqi

**Muallif:** Erkin Pardayev

* GitHub: [github.com/Erkinbek](https://github.com/Erkinbek)
* Repozitoriy: [github.com/Erkinbek/jsonreshape](https://github.com/Erkinbek/jsonreshape)
* E-pochta: [erkin.pardayev@gmail.com](mailto:erkin.pardayev@gmail.com)

Xatolar va takliflar: [GitHub Issues](https://github.com/Erkinbek/jsonreshape/issues).

**Claude Opus 5.5 yordamida yaratilgan.** Kod, testlar, benchmarklar va hujjatlar Anthropic'ning Claude Opus 5.5
modeli (Claude Code'da) bilan, muallifning texnik topshirig'i (`AGENT_PROMPT.md`) va qarorlari asosida yozildi.

Mualliflik huquqi © 2026 Erkin Pardayev. Barcha huquqlar himoyalangan.
