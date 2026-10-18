# jsonreshape

Katta JSON fayllar (100 MB dan bir necha GB gacha) bilan ishlash uchun desktop dastur va CLI:
ko'rish, qidirish, filtrlash, tartiblash, konvertatsiya, keyinchalik tahrirlash va tuzilmani qayta qurish.
**Fayl hech qachon to'liq xotiraga yuklanmaydi**: u bir marta indekslanadi (har yozuvning bayt chegaralari),
keyin faqat kerakli bo'laklar o'qiladi.

> **Holat: 2-bosqich (ko'ruvchi GUI) tayyor.** 3-bosqich (tahrirlash) tasdiqdan keyin boshlanadi.

| # | Bosqich | Holat |
|---|---|---|
| 1 | **Yadro**: reader + indeks (Rust yadro va Python fallback), RecordSet, qidiruv, filtr, tartib, writer, CLI (`inspect`, `search`, `filter`, `sort`, `convert`) | ✅ tayyor |
| 2 | **Ko'ruvchi GUI** (PySide6): daraxt, jadval, matn, xom baytlar, tuzilma, navigatsiya, qidiruv paneli, filtr quruvchi, tartiblash, eksport, fon vazifalari, holat qatori | ✅ tayyor |
| 3 | Tahrirlash: tugun tahriri, ommaviy amallar, tahrir qatlami (SQLite), saqlash, avtosaqlash | ⏳ |
| 4 | Tuzilma quruvchi (mapping engine + GUI + `apply`, `flatten`) — etalon: `json-tuzilma.html` | ⏳ |
| 5 | Asboblar: statistika, dublikatlar, JSON Schema, split/merge, repair, diff | ⏳ |
| 6 | Silliqlash: buyruqlar palitrasi, sessiya, workspace, mavzular, PyInstaller `.exe` | ⏳ |

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

Asl fayl **hech qachon o'zgartirilmaydi**: barcha natijalar alohida faylga vaqtinchalik nom bilan yoziladi
va oxirida atomik almashtiriladi. Agar kirish fayli ish davomida o'zgarsa (boshqa dastur qayta yozsa,
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
* **Alt+← / Alt+→** — tanlash tarixi; **Ctrl+D** — xatcho'p (nomlanadi, saqlanadi), **Ctrl+Shift+D** —
  xatcho'plar ro'yxati.
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
tiklash), JSONL, CSV, XLSX, SQLite. Doira: joriy ko'rinish (filtr + tartib), barcha yozuvlar yoki joriy tugun.
Manba faylning o'ziga (boshqa nom yoki havola orqali ham) yozish taqiqlangan. Eksport fonda ishlaydi: dialogni
yopsangiz ham, faylni qayta ochsangiz ham davom etadi; oxirida hisobot (yozuvlar, fayllar, ogohlantirishlar).

### Sozlamalar va loglar

**Asboblar → Sozlamalar**: namuna hajmi, kesh papkasi va hajm chegarasi, xotira chegarasi, tartiblash xotirasi,
katta tugun chegaralari, eksport standartlari, til, mavzu (tizimga ergashish / yorug' / qorong'i), monospace
shrift va o'lchami. Loglar: **Asboblar → Loglar papkasi**; har xato oynasida «Logni ochish».

### Klaviatura

| Tugma | Amal |
|---|---|
| Ctrl+O / Ctrl+W / F5 / Ctrl+Q | ochish / tabni yopish / qayta ochish / chiqish |
| Ctrl+1 … Ctrl+5 | faol panelda: Daraxt, Jadval, Matn, Xom, Tuzilma |
| ↑ ↓, PgUp PgDn, Home End | yurish |
| → / ← / `*` | tugunni ochish / yopish / hammasini ochish (daraxt) |
| Space | to'liq qiymat oynasi |
| Ctrl+C / Ctrl+Shift+C | qiymatni (jadvalda tanlangan kataklarni TSV) / yo'lni nusxalash |
| Ctrl+F, F3, Shift+F3 | tezkor qidiruv, keyingi / oldingi moslik |
| Ctrl+Shift+F / Ctrl+Shift+L | to'liq qidiruv / filtr paneli |
| Ctrl+G | yo'lga o'tish |
| Alt+← / Alt+→ | orqaga / oldinga |
| Ctrl+D / Ctrl+Shift+D | xatcho'p qo'shish / xatcho'plar |
| Ctrl+E | eksport |
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
* Faqat tanlangan qism: `filter`/`sort` natijasi yoki `convert --where ... --sort-by ...`.
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
  writer.py           oqimli yozuvchilar: JSON, JSONL, CSV, XLSX, SQLite
  cli.py              buyruq qatori
  i18n.py             barcha UI matnlari (o'zbekcha asosiy, inglizcha zaxira) — bitta lug'at fayli
  gui/                PySide6 ko'ruvchi: virtual grid (100 mln+ qator), daraxt/jadval/matn/xom/tuzilma
                      ko'rinishlari, sessiya (filtr -> tartib -> ko'rinish), fon vazifalari, panellar
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
```

To'liq to'plam: 5431 ta test (2-bosqich oxirida, Rust yadro bilan, ~13 daqiqa). GUI testlari ekransiz
(`QT_QPA_PLATFORM=offscreen`) ishlaydi.

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
| 26 (2-bosqich qismi) | faylni ochish (buyruq qatori, sudrab tashlash), daraxtni ochish/yopish, qidiruv natijasiga o'tish, jadvalda tartiblash, filtr, yo'lga o'tish va tarix, eksport tugmasi, yopish — haqiqiy oyna orqali (tahrir va undo 3-bosqichda) | `test_gui_e2e.py` |
| — | har ko'rinish va panel: daraxt, jadval, matn, xom, tuzilma, qidiruv, filtr, tartib, eksport, sozlamalar, navigatsiya, holat qatori, fon vazifalari | `test_gui_*.py` (37 ta fayl) |
| — | virtual grid (10¹² qator), FlatTree (hypothesis model testlari), yuklovchi, sessiya | `test_gui_vgrid.py`, `test_flattree.py`, `test_gui_loader.py`, `test_gui_session.py` |
| — | ko'rib chiqishda topilgan xatolar uchun regressiya testlari (qulash, xotira oqishi, GIL, katta matn) | `test_gui_fix_*.py`, `test_schema_walk.py`, `test_recordset_locks.py` |
| 25 (GUI) | katta fayllarda GUI: birinchi yozuvlar, indeks, aylantirish, 1 mln bolali tugun, qidiruv, filtr, tartib, eksport, RAM | `test_bench_gui.py` (`-m bench`), `bench/gui_bench.py` |

<a id="olchovlar"></a>
## 14. O'lchovlar (benchmark)

To'liq natijalar, o'lchash shartlari va kompyuter tavsifi: yadro va CLI —
[`bench/results/latest.md`](bench/results/latest.md), GUI — [`bench/results/gui_latest.md`](bench/results/gui_latest.md)
(2026-10-17; qayta o'lchash: `.venv\Scripts\python bench\run_bench.py` va `bench\gui_bench.py`). Kompyuter:
Intel Core Ultra 9 285 (24 yadro), 205 GB RAM, NVMe SSD, Windows 11, Python 3.14, Rust yadro. Fayllar OS
keshida (iliq kesh); bitta sovuq o'qish alohida o'lchandi. 2-bosqich o'lchovlari paytida kompyuterda begona
jarayonlar ~30–35 % CPU band qilib turgan, shuning uchun ba'zi raqamlar 1-bosqichdagidan 10–20 % pastroq.
§12 maqsadlari "oddiy noutbuk" uchun yozilgan — noutbukda CPU'ga bog'liq amallar sekinroq bo'ladi.

### Yadro va CLI

| Amal (AGENT_PROMPT §12) | Maqsad | Natija | Holat |
|---|---|---|:---:|
| Ochish, birinchi yozuvlar ko'rinishi (2.1 GB; 5.4 GB) | < 1 s | 11 ms; 12 ms | ✅ |
| To'liq indeks, Rust yadro (2.1 GB massiv; 5.4 GB; 3.3 GB JSONL) | ≥ 150 MB/s | 741; 668; 676 MB/s (sovuq disk: 777 MB/s) | ✅ |
| 1 000 000 bolali tugunni ochish (virtual) | < 200 ms | 79 ms (keshdan qayta: 3.9 ms) | ✅ |
| To'liq matn qidiruvi, bayt rejimi: kam uchraydigan so'z | ≥ 100 MB/s, natijalar darhol | 913 MB/s, birinchi natija 16 ms | ✅ |
| … o'rtacha so'z «Toshkent» (6 % yozuvda) | ≥ 100 MB/s | 680 MB/s, birinchi natija 12 ms | ✅ |
| … ko'p uchraydigan so'z «ko'chasi» (60 % yozuvda) | ≥ 100 MB/s | 287 MB/s, birinchi natija 14 ms | ✅ |
| 10 000 000 yozuvni bitta maydon bo'yicha tartiblash **va natijani yozish** (`jsonreshape sort` kabi, ochishdan oxirigacha) | < 2 daqiqa, RAM < 1 GB | 76.2 s, peak 372 MB (tartiblash 21.7 s, tartiblangan eksport 66.5 MB/s) | ✅ |
| Eksport JSON → JSONL / JSON (indent 2), 2.1 GB | ≥ 50 MB/s, RAM < 300 MB | 225 / 246 MB/s, peak 81 / 63 MB | ✅ |
| Dasturning umumiy xotirasi (tartiblashdan boshqa amallarning eng katta peak RSS) | < 500 MB | 248 MB | ✅ |

Qo'shimcha: filtr (extract) 200 MB/s; tuzilma namunasi (10 000 yozuv) 167 ms; keshdagi indeks bilan qayta
ochish 8.7 ms; sof Python yadro bilan indeks 72 MB/s. 2-bosqichda tartiblash GUI'ni qotirmasligi uchun kalitlar
kichik bo'laklarda tartiblanib birlashtiriladigan bo'ldi (bitta uzun `list.sort` GIL'ni 0.5 s ushlab turardi):
tartiblash bosqichi ~16 % sekinlashdi, butun amal maqsad ichida.

**Maqsadga yetilmagan joy**: CSV eksport **avtomatik ustunlar** bilan — 26.3 MB/s (peak 107 MB); faqat
tanlangan skalyar ustunlar bilan — 52.7 MB/s ✅. (§12 dagi ≥ 50 MB/s eksport maqsadi mapping/tahrirli eksport
uchun yozilgan; mapping va tahrirlar 3–4-bosqichlarda.) Sabab: CSV har bir yozuvni parse qiladi, har katakni
Python'da quradi va massivlarni JSON matniga qayta yozadi; avtomatik ustunlar uchun barcha yozuvlar oldindan
bir marta o'qiladi (hech bir maydon yo'qolmasligi uchun). Variantlar: 1) kerakli ustunlarni `--columns` bilan
tanlash; 2) qator qurishni Rust yadroga o'tkazish (Python obyektlari yaratilmaydi); 3) bloklarni parallel
jarayonlarda tayyorlab, tartib bilan yozish.

### GUI (2-bosqich)

Har o'lchov alohida jarayonda, haqiqiy oyna (`jsonreshape.gui.app.main`) bilan, Qt offscreen platformasida;
«UI javob berishi» — GUI oqimidagi 5 ms taymer urishlari orasidagi eng uzun oraliq (foydalanuvchi bosgan tugma
shuncha kutadi).

| Amal | Maqsad | Natija | Holat |
|---|---|---|:---:|
| Jarayon ishga tushganidan daraxtda birinchi yozuvlar chizilguncha (2.1 GB; 5.4 GB; 3.3 GB JSONL) | < 1 s | 627; 621; 622 ms (`main()` dan: 454; 441; 447 ms) | ✅ |
| To'liq indeks GUI ichida, UI javob berib turadi (2.1 GB; 5.4 GB; 3.3 GB JSONL) | ≥ 150 MB/s | 718; 666; 656 MB/s; UI max oraliq 16–26 ms | ✅ |
| Skrollbarni oxiriga sakratish (6.5 mln; 16 mln; 10 mln qator) | — | 6.7; 7.2; 6.7 ms | — |
| 1 000 000 bolali tugunni ochish | < 200 ms | birinchi marta 89 ms (bolalar indeksi 82 + chizish 6); qayta 2.8 ms | ✅ |
| To'liq qidiruv (qidiruv paneli): kam uchraydigan so'z; «Toshkent» | ≥ 100 MB/s, birinchi natija < 1 s | 921 MB/s, 56 ms; 663 MB/s, 25 ms | ✅ |
| Filtr 10 mln yozuvda (filtr paneli) | — | birinchi qisman natijalar 117 ms; hammasi 17.0 s (193 MB/s); UI max 25 ms | — |
| 10 mln yozuvni `name` bo'yicha tartiblash (GUI orqali) | < 2 daqiqa, RAM < 1 GB | 21.7 s, peak RSS 454 MB; UI max 59 ms | ✅ |
| Tartiblangan ko'rinishni JSONL'ga eksport (eksport oynasi) | ≥ 50 MB/s, RAM < 300 MB | 66.3 MB/s; RSS 413 MB, shundan xususiy xotira 112 MB | ❌ (RSS) |
| 1 mln elementli tugunni matn panelida ko'rsatish | — | 0.82 s (4 MB gacha), RAM +77 MB | — |
| Butun GUI sessiyasi, tartiblashsiz (ochish, indeks, skroll, jadval, qidiruv, filtr, tuzilma, matn, eksport) | < 500 MB | peak RSS 276 MB; tab yopilgach 132 MB (ochishdan oldin 99 MB) | ✅ |

**Tartiblangan eksportdagi RSS** maqsaddan oshdi: 413 MB ning 112 MB i jarayonning xususiy xotirasi, qolgani —
tartib bo'yicha o'qilgan indeks (`.jri`, 160 MB) va RecordSet (`.u64`, 80 MB) fayllarining xotiraga
proyeksiyalangan sahifalari (OS kerak bo'lsa ularni darhol bo'shatadi). Variantlar: 1) maqsadni xususiy xotira
bo'yicha hisoblash; 2) indeks oynalarini o'qilgandan keyin ochib-yopib turish (sekinroq); 3) eksportdan oldin
indeksni tartib bo'yicha qayta yozish (qo'shimcha disk). Qaror kerak bo'lsa — ochiq savollarda.

Ko'rib chiqishdan keyingi tuzatishlar natijasi (oldin → keyin): ikki JMESPath filtri parallel ishlaganda UI
p95 3.9 s → 11 ms; 10 mln tartiblashda UI max 457 → ~40 ms; katta tugunni yig'ish (folding) 4.2 s / +1.5 GB →
30 ms; 45 MB'lik bitta obyektli fayl 1.28 GB → 108 MB; har yopilgan tab +25 MB oqish → 0.

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

### Ma'lum cheklovlar

* gzip va UTF-16 fayllar birinchi yozuv ko'rinishidan oldin to'liq UTF-8 nusxaga o'giriladi (oqim bilan,
  xotira cheklangan; 200 MB ~0.5 s).
* Fayl dasturda ochiq turganda boshqa dastur uni `MoveFileEx`/`os.replace` bilan **ustidan almashtira olmaydi**
  (Windows qoidasi); oddiy yozish, qo'shish, nomini o'zgartirish va o'chirish ishlaydi va aniqlanadi.
* Hajmi va mtime'i saqlangan, faqat hash qilinmagan o'rta baytlarga tegadigan tahrir keshdagi indeks bilan
  sezilmasligi mumkin (`--no-cache` bilan qayta indekslang).
* Bitta juda uzun satr qiymati (yuzlab MB) tekshirilayotganda xotirada ~2 baravar joy oladi.
* CSV eksport avtomatik ustunlar bilan ~25–30 MB/s (ustunlar uchun to'liq oldindan o'qish + Python'da
  katak qurish); `--columns` bilan tezroq (≈ 60 MB/s).
* `stats`, `split`, `merge`, `repair`, `diff`, `apply`, `flatten` buyruqlari, tahrirlash, statistika paneli,
  buyruqlar palitrasi, sessiyani tiklash — keyingi bosqichlarda.
* GUI tilini almashtirish to'liq kuchga dastur qayta ishga tushirilgandan keyin kiradi (ochiq oynalardagi
  ba'zi matnlar eski tilda qoladi; sozlamalar oynasi buni aytadi).
* Tezkor qidiruv (Ctrl+F) faqat yuklangan qismda qidiradi (talab bo'yicha); butun fayl uchun Ctrl+Shift+F.
* GUI orqali 10 mln yozuvni tartiblash va tartiblangan ko'rinishni eksport qilishda RSS ~500 MB gacha
  ko'tariladi; buning katta qismi indeks va RecordSet fayllarining xotiraga proyeksiyalangan sahifalari (OS
  kerak bo'lsa bo'shatadi); jarayonning xususiy xotirasi ~110–420 MB.

### Javob berilgan savollar (2026-10-17)

1. `json-tuzilma.html` loyiha ildizida — 4-bosqich (mapping) etaloni.
2. O'zbek kollatsiyasi: rasmiy alifbo tartibi standart bo'lib qoladi (`simple` — tanlov sifatida).
3. Yozuv raqamlari GUI'da ham 0 dan (`#0`).
4. JSONL → JSON aylanmasida kalitli obyektni tiklash kerak — `--as-keyed` (CLI) va eksport oynasidagi
   «Kalitli obyektni tiklash» sifatida qilindi (9-bo'lim; API: `ExportOptions(as_keyed=True)`).

### Ochiq savollar (3-bosqich — tahrirlash — uchun)

1. **Tahrir qatlami (SQLite) qayerda saqlansin?** a) fayl yonida (`fayl.json.jredit`) — fayl bilan birga
   ko'chadi, lekin faqat o'qiladigan/tarmoq papkalarida ishlamaydi; b) dastur papkasida
   (`%LOCALAPPDATA%\jsonreshape\edits\`) — har doim ishlaydi. Tavsiya: b), "fayl yonida saqlash" tanlovi bilan.
2. **Saqlashda `.bak` zaxira** standart bo'yicha yoqilgan bo'lsinmi (katta faylda diskda ikki baravar joy
   oladi) yoki o'chirilgan bo'lsinmi?
3. **Enter tugmasi**: hozir daraxtda tugunni ochadi/yopadi; 3-bosqichda §11 bo'yicha "tahrirlash" bo'ladi
   (ochish/yopish → va ← da qoladi). Shunday bo'lsinmi?
4. **Tartiblangan eksport xotirasi** (14-bo'lim): "RAM < 300 MB" maqsadini jarayonning xususiy xotirasi
   bo'yicha hisoblaymizmi (112 MB ✅) yoki indeks fayllari sahifalari ham kirgan RSS bo'yicha (413 MB ❌ — u
   holda indeksni ochib-yopib o'qish kerak, eksport sekinlashadi)?
