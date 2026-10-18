# Vazifa: katta JSON fayllar uchun ko'ruvchi, muharrir va tuzilma quruvchi (Python + PySide6)

## 0. Kontekst

Men katta JSON fayllar bilan ishlayman: 100 MB dan bir necha GB gacha, Windows 10/11.
Fayllar odatda ko'p yozuvdan iborat: bir xil tuzilishdagi obyektlar, ichida ichma-ich obyektlar va massivlar (masalan, `children` ichida yana `children`).

Brauzerda ishlaydigan prototip bor: `json-tuzilma.html` (loyiha ildizida). U fayl tuzilmasini daraxt qilib chizadi. Maydonlarni sichqoncha bilan surib qayta joylash mumkin, eksportda yangi JSON yaratiladi. Mantiqi to'g'ri va testlangan, lekin butun faylni xotiraga oladi:
- 100 MB ≈ 5 soniya, ≈ 1.4 GB RAM;
- 300 MB ≈ 15 soniya, ≈ 4 GB RAM (brauzer qulaydi);
- 500 MB+ umuman ochilmaydi.

`/*ENGINE-START*/ … /*ENGINE-END*/` orasidagi JavaScript **tuzilmani qayta qurish semantikasi uchun etalon**. Pythondagi natija u bilan aynan bir xil bo'lishi kerak. Prototipdagi UI g'oyalaridan ham foydalan: badge'lar, tashlash zonalari, jonli ko'rinish.

**Maqsad:** JSON bilan ishlash uchun maksimal qulay desktop dastur. Ko'rish, qidirish, filtrlash, tartiblash, tahrirlash, tuzilmani qayta qurish, konvertatsiya. Fayl hajmidan qat'i nazar dastur qotmasligi va xotirani to'ldirmasligi shart.

Belgilar: **[M]** majburiy, **[K]** kerak (iloji bo'lsa albatta), **[I]** ixtiyoriy (vaqt qolsa).

---

## 1. Asosiy tamoyillar [M]

1. **Butun fayl hech qachon xotiraga yuklanmaydi.** Fayl bir marta indekslanadi (yozuvlar chegaralari va bayt offsetlari). Keyin faqat kerakli bo'lak o'qiladi va parse qilinadi.
2. **UI hech qachon qotmaydi.** Uzoq amallar fonda bajariladi: indekslash, qidiruv, tartiblash, filtr, statistika, eksport. Har birida progress (%, MB/s, qolgan vaqt) va "Bekor qilish" bo'ladi.
3. **Hamma ro'yxatlar virtual.** Daraxt ham, jadval ham, natijalar ham faqat ekrandagi qatorlarni chizadi. 100 million yozuv ham silliq aylansin.
4. **Asl fayl hech qachon o'zgarmaydi.** Tahrirlar alohida qatlamda (patch) saqlanadi. "Saqlash" yangi faylni streaming bilan yozadi va oxirida atomik almashtiradi (ixtiyoriy `.bak` zaxira bilan). "Boshqacha saqlash" ham bor.
5. **Har bir amal uchun Undo/Redo** (kamida 200 qadam). Ommaviy amallar ham kiradi.
6. **Offline.** Dastur tarmoqqa hech narsa yubormaydi: telemetriya ham, yangilanish tekshiruvi ham yo'q.
7. **Windows birinchi darajali.**
   - uzun yo'llar, tarmoq disklari;
   - faylni eksklyuziv bloklamaslik (shared read);
   - fayl tashqaridan o'zgarsa, aniqlab qayta indekslashni taklif qilish;
   - High-DPI.

---

## 2. Texnologiya

- Python 3.11+, GUI: **PySide6** (Qt 6). `QAbstractItemModel` asosidagi o'z modellar: `canFetchMore`/`fetchMore` yoki virtual oyna.
- Kutubxonalar:
  - `orjson`: yozuv/tugun parse va dump;
  - `ijson` (`yajl2_c` backend, ishga tushganda tekshirilsin);
  - `xlsxwriter` (`constant_memory` rejimi);
  - `jmespath`;
  - `sqlite3` (standart);
  - `pytest`, `pytest-qt`.
- **Tezkor yadro [K]:** bayt darajasidagi skanerlash Rust + PyO3 (`maturin`) modulida bo'lsin, Windows wheel bilan: yozuv chegaralari, konteyner bolalari offsetlari, bayt qidiruvi (`memchr`), tartiblash kalitlarini ajratish.
  - Har funksiyaning sof Python fallback'i bo'lsin. Interfeys bir xil, natija aynan bir xil (test bilan tekshiriladi).
  - Rust GIL'ni qo'yib yuborsin.
- `pyproject.toml`, buyruqlar: `jsonreshape` (CLI) va `jsonreshape-gui`. PyInstaller bilan bitta `.exe` [K].
- Hamma UI matnlari bitta lug'at faylida. O'zbekcha (lotin) asosiy, inglizcha zaxira.

---

## 3. Ochish, formatlar va indeks

### Formatlar [M]
Avtomatik aniqlanadi va status qatorida ko'rsatiladi:
1. Massiv: `[ {...}, ... ]`.
2. JSON Lines.
3. Buzuq `{ {...}, {...} }`: obyektlar ketma-ket o'qiladi, ogohlantirish chiqadi.
4. Ichida katta massiv bor obyekt: `{"meta": {...}, "data": [ ... ]}`.
   - Faylning boshini ko'rib, katta massiv yo'llarini taklif qil.
   - Foydalanuvchi "record path"ni tanlaydi (`data`).
   - Tashqi o'rama alohida ko'rsatiladi va saqlanadi.
5. Kalitli ildiz: `{"id1": {...}, "id2": {...}}`. Yozuvlar = yuqori darajadagi a'zolar (kalit + qiymat).
6. Bitta ulkan obyekt yoki qiymat: ichki konteynerlar talab bo'yicha indekslanadi (pastda).
7. UTF-8 (BOM bilan yoki BOMsiz), UTF-16 aniqlash [K], `.json.gz` [K].

### Indeks [M]
- Fonda quriladi: har yozuv uchun `(start, end)` bayt offsetlari.
- Birinchi yozuvlar **darhol** ko'rinadi (1 soniyadan kam), indeks esa orqada to'lib boradi. Yozuvlar soni jonli yangilanadi.
- Indeks diskda keshlanadi: `%LOCALAPPDATA%\jsonreshape\index\`, kaliti = yo'l + hajm + mtime + boshidagi bloklar hash'i.
  - Fayl o'zgarsa, indeks yaroqsiz.
  - Format: binar, `mmap` bilan o'qiladi, RAM'ga yuklanmaydi.
- **Ichki konteynerlarni talab bo'yicha indekslash:** 50 MB dan katta yoki 10 000 dan ko'p bolali tugun to'liq parse qilinmaydi. Ochilganda faqat bolalarining offsetlari skanerlanadi va keshlanadi. Bolalar virtual ro'yxatda chiqadi.
- JSON xato bo'lsa:
  - bayt offseti, qator va ustun hamda atrofdagi matn ko'rsatiladi;
  - "Xatogacha bo'lgan yozuvlarni ochish" imkoni bo'ladi;
  - JSONL'da buzuq qatorlar o'tkazib yuboriladi va ro'yxati chiqariladi.

### Yozuvlar to'plami (RecordSet) [M]
Markaziy abstraksiya: yozuv indekslari ro'yxati (diskdagi `uint64` massiv).
- Bazaviy to'plam = fayl tartibidagi barcha yozuvlar.
- Filtr, tartiblash va qidiruv natijalari = yangi RecordSet.
- Ko'rinishlar, statistika va eksportning hammasi RecordSet bilan ishlaydi. Natijada "filtrlangan + tartiblangan qismni eksport qilish" bepul keladi.

---

## 4. Ko'rish rejimlari

### 4.1 Daraxt [M]
- Har qatorda:
  - kalit (yoki `[indeks]`);
  - tur belgisi va rangi (object / array / string / number / bool / null);
  - qiymatning qisqa ko'rinishi;
  - bolalar soni;
  - tugun hajmi (bayt).
- Ochish/yopish:
  - bitta tugun;
  - tugun ichini hammasini ochish (bolalar 1 000 dan ko'p bo'lsa, oldin ogohlantirish);
  - hammasini yopish;
  - "N-darajagacha ochish" (1–9).
- Uzun satrlar qisqartiriladi, "to'liq ko'rish" alohida oynada.
- Chiziqli yo'l (breadcrumb) tepada doim ko'rinadi, har bo'lagi bosiladigan.
- Indent chiziqlari bor, juft/toq qatorlar ajralib turadi.
- Ochilgan holat eslab qolinadi. Filtr yoki tartib o'zgarsa ham yo'qolmaydi.

### 4.2 Jadval [M]
- Yozuvlar qator bo'lib chiqadi. Ustunlar tuzilmadan olinadi, ichma-ich maydonlar ham ustun bo'la oladi (`contact.address.city`).
- Ustunlar ustida: tanlash, yashirish, joyini almashtirish, kengligi, birinchi ustunni qotirish.
- Massiv va obyekt kataklari sonini ko'rsatadi (`[3]`, `{5}`). Bosilsa, daraxtda ochiladi.
- Ustun sarlavhasini bosish tartiblaydi (Shift bilan ko'p ustunli). Har ustun ostida tezkor filtr.
- Kataklarni nusxalash, ko'p katak tanlab TSV qilib nusxalash (Excel'ga qo'yish uchun).

### 4.3 Matn [M]
- Tanlangan tugun yoki yozuv sintaksis ranglari bilan, chiroyli formatda chiqadi: qator raqamlari, qavslarni yig'ish (folding), qator o'rash, 2/4/tab indent tanlovi.
- Faylning xom baytlari ko'rinishi: istalgan offset yoki qator atrofi sahifalab ko'rsatiladi (butun fayl o'qilmaydi). Buzuq JSON'ni tekshirish uchun "offsetga o'tish" va "qatorga o'tish".

### 4.4 Tuzilma (schema) [M]
- Barcha yozuvlarning umumiy tuzilmasi chiqadi:
  - turlar taqsimoti;
  - mavjudlik foizi;
  - misol qiymatlar.
- Namuna (default 10 000 yozuv) yoki to'liq skaner (fonda) asosida.
- Shu yerdan 7-bo'limdagi tuzilma quruvchiga o'tiladi.

### 4.5 Yonma-yon va taqqoslash [K]
- Ikki yozuv yoki tugunni yonma-yon ko'rish.
- Tuzilmaviy diff: qo'shilgan, o'chirilgan va o'zgargan yo'llar ranglanadi. Massiv elementlarini kalit bo'yicha (`id`) moslash tanlovi bor.
- Ikki faylni diff qilish:
  - 200 MB gacha to'liq;
  - kattaroqlarini kalit bo'yicha streaming sort-merge bilan [I].

### 4.6 Umumiy
- Bir nechta fayl tablarda ochiladi. Bitta faylning ikki ko'rinishini yonma-yon qo'yish mumkin (masalan, daraxt + jadval).
- Daraxt, jadval va matn bir-biriga bog'langan: birida tanlangan narsa boshqasida ham tanlanadi.

---

## 5. Navigatsiya [M]

- Yo'lga o'tish (Ctrl+G):
  - `data[120].contact.address`;
  - `$.data[120]`;
  - JSON Pointer `/data/120`;
  - yozuv raqami `#120`.
- Orqaga va oldinga (Alt+← / Alt+→), tanlash tarixi.
- Xatcho'plar (bookmark): nomlanadi va saqlanadi.
- Yo'lni nusxalash formatlari:
  - nuqtali: `a.b[0].c`;
  - JSONPath;
  - JSON Pointer;
  - Python: `["a"]["b"][0]`;
  - JavaScript.
- Qiymatni nusxalash: JSON (chiroyli yoki ixcham), Python dict, CSV qatori.
- "Daraxtda ko'rsatish": istalgan qidiruv natijasi, jadval katagi yoki statistika qatoridan.
- So'nggi fayllar, oxirgi sessiyani tiklash: ochiq fayllar, joylashuv, ochilgan tugunlar, filtrlar.

---

## 6. Qidiruv, filtr, tartiblash

### 6.1 Tezkor qidiruv (Ctrl+F) [M]
- Hozir ko'rinib turgan yoki yuklangan qismda yoziladi va darhol topadi, mosliklar ajratib ko'rsatiladi.
- F3 / Shift+F3 bilan keyingi yoki oldingi moslikka o'tiladi.

### 6.2 To'liq fayl bo'yicha qidiruv (Ctrl+Shift+F) [M]
- Qayerda: kalitlarda / qiymatlarda / ikkalasida; faqat belgilangan maydon yoki yo'lda (masalan, faqat `name` ichida).
- Rejimlar:
  - oddiy matn;
  - katta-kichik harfga sezgir / sezgir emas;
  - butun so'z;
  - regex;
  - raqamli taqqoslash (`> 1000`).
- **O'zbekcha qulaylik:**
  - Unicode casefold;
  - apostrof turlari bir xil hisoblanadi: `o'`, `o‘`, `oʻ`, `o’`, `g'`, `gʻ`;
  - **lotin ↔ kirill sezgir emas rejimi**: "Toshkent" qidirilsa "Тошкент" ham topiladi.
- Tezlik:
  - avval baytlar bo'yicha tez oldindan saralash;
  - keyin topilgan yozuv parse qilinib, aniq yo'l va moslik tasdiqlanadi.
  - Fayl `\uXXXX` escape'lar bilan yozilgan bo'lsa yoki lotin↔kirill rejimi tanlangan bo'lsa, parse asosidagi qidiruvga o'tiladi.
- Natijalar topilishi bilan ro'yxatda paydo bo'ladi: yozuv raqami, yo'l, ajratilgan parcha, jami soni.
  - Bekor qilish mumkin.
  - Natijani bosish o'sha tugunga olib boradi.
  - Natijalar ro'yxati RecordSet sifatida ham ochiladi.
  - Natijalarni CSV yoki JSON'ga eksport qilish mumkin.
- Qidiruv tarixi saqlanadi.

### 6.3 Filtr [M]
- **Vizual quruvchi:**
  - maydon + operator + qiymat;
  - operatorlar: `=`, `≠`, ichida bor, bilan boshlanadi, regex, `>`, `<`, oraliq, mavjud, mavjud emas, `null`, bo'sh, ro'yxatda bor, uzunlik;
  - VA/YOKI guruhlari.
- **Ifoda qatori:**
  - oddiy xavfsiz til: `contact.address.city == "Toshkent" and len(children) > 2` (`eval` ishlatilmasin);
  - mutaxassislar uchun JMESPath.
- Filtr fonda, streaming bilan qo'llanadi. Natija (RecordSet) to'lib borishi bilan ko'rinadi.
- Filtrlar nom bilan saqlanadi (preset). Statistikada qiymatni bosish o'sha qiymat bo'yicha filtr qo'yadi.

### 6.4 Tartiblash [M]
- Bir yoki bir nechta maydon bo'yicha, o'sish yoki kamayish tartibida.
- Turga mos taqqoslash:
  - sonlar son sifatida;
  - satrlar o'zbek/locale qoidasi bilan;
  - "tabiiy" tartib: `child2 < child10`;
  - `null` va yo'q qiymatlar oxirida (sozlanadi).
- Barqaror (stable) bo'lsin.
- Katta fayl uchun:
  - `(kalit, yozuv_indeksi)` juftliklari streaming bilan ajratiladi;
  - xotiraga sig'sa, xotirada tartiblanadi; sig'masa, diskda tashqi merge sort;
  - natija RecordSet bo'ladi va shu tartibda eksport qilinadi.
- Tugun ichida tartiblash:
  - massiv elementlarini maydon bo'yicha;
  - obyekt kalitlarini alifbo bo'yicha (ko'rinish va eksport uchun alohida tanlov).

---

## 7. Tuzilmani qayta qurish (mapping) [M]

Etalon: `json-tuzilma.html` engine.

### Ikki qism
1. **GUI quruvchi:** namuna asosidagi tuzilma daraxti. Maydonlar sichqoncha bilan suriladi.
2. **Engine:** mapping butun faylga streaming bilan qo'llanadi.

### Tugun modeli
`key`, `kind` (object/array/value), manba yo'li `src` (segmentlar, massiv elementi uchun `[]`), bayroqlar (`synthetic` yangi guruh, `computed` = `_depth`, `recKey` rekursiv tekislash, `isElem` massiv elementi), `children`.

### Hisoblash qoidalari
- **Bog'lanish:** massiv aylanayotganda har element o'sha `[]` prefiksiga bog'lanadi. Yo'l eng uzun bog'langan prefiksdan hal qilinadi.
- **Gather:** bog'lanmagan `[]` barcha elementlarga yoyiladi va natija bitta ro'yxatga yig'iladi. Masalan, `orders[].id` yozuv darajasiga ko'chirilsa → `[7, 8]`.
- **Broadcast:** massivdan tashqaridagi maydon element ichiga ko'chirilsa, har elementga takrorlanadi.
- **Array tuguni:** barcha element segmentlari ketma-ket ulanadi. Massivni massiv ustiga tashlash shu orqali ishlaydi.
- **recKey:** pre-order aylanish (child1, child1.2, child1.2.1, child1.2.3, child2), `_depth` = 1, 2, 3…, iterativ stek bilan.
- **Yo'q qiymat:** manbada qiymat yo'q bo'lsa, maydon yozilmaydi.
- **Turi mos kelmasa:** object tugunining manbasi obyekt bo'lmasa, qiymat o'zgarmasdan ko'chiriladi.
- **Kalit to'qnashuvi:** bir ota ichida takrorlansa, `_2`, `_3` qo'shiladi.

### Amallar
- ko'chirish: oldiga, ichiga, ortiga;
- nomini o'zgartirish, o'chirish;
- yangi guruh;
- unwrap (ichidagilarni bir pog'ona yuqoriga chiqarish);
- massivlarni birlashtirish;
- rekursiv tekislash (element tuzilmasi barcha darajalardan qayta aniqlanadi va `_depth` qo'shiladi);
- undo/redo, asl holat.

### Drag & drop qoidalari
- Qatorning tepa 28% qismi = oldiga, o'rtasi = ichiga, pastki 28% = ortiga.
- Qiymat qatorlariga "ichiga" tashlab bo'lmaydi.
- Tugunni o'z avlodi ichiga tashlab bo'lmaydi.
- Massiv ustiga faqat massiv tashlanadi.

### UI
- Badge'lar: "ko'chirildi", "asl nomi: …", "ro'yxatga yig'iladi", "har elementga takrorlanadi", "rekursiv · children", "manba: …".
- Jonli ko'rinish: tanlangan yozuv natijada qanday chiqishi, "Asl" bilan almashtiriladi.

### Mapping fayli
`{"version": 1, "input": {...}, "tree": {...}}` ko'rinishida saqlanadi, ochiladi va CLI'da ishlatiladi.

---

## 8. Tahrirlash

### 8.1 Tugun darajasida [M]
- Qiymatni joyida tahrirlash, turiga mos muharrir bilan:
  - satr: ko'p qatorli oyna;
  - son: tekshiruv bilan;
  - bool: almashtirgich;
  - `null`.
- Kalit nomini o'zgartirish (F2).
- Kalit/qiymat qo'shish, massivga element qo'shish (oldiga yoki ortiga).
- O'chirish (Del), nusxa ko'paytirish (Ctrl+D), yuqoriga/pastga surish.
- Data daraxtida drag & drop bilan ko'chirish.
- Turini almashtirish (satr ↔ son va h.k.). Imkonsiz bo'lsa, tushunarli xato chiqadi.
- Bufer: JSON'ni bola sifatida qo'yish (Ctrl+V), tugunni JSON qilib nusxalash.
- Ko'p tanlash (Ctrl/Shift + bosish): ommaviy o'chirish, ko'chirish, tur almashtirish.
- Tugunni matn muharririda tahrirlash: jonli JSON tekshiruvi va xato joyi belgisi bilan.

### 8.2 Ommaviy amallar [M]
Ommaviy amallar butun fayl yoki RecordSet ustida ishlaydi:
- qiymatlarda topish va almashtirish (matn yoki regex);
- kalitni hamma joyda qayta nomlash;
- maydonni hamma joydan o'chirish;
- filtrlangan yozuvlarda maydonga qiymat berish;
- satrlarni tozalash (trim, bo'sh joylarni yig'ish);
- turini ommaviy o'zgartirish;
- `id` bo'yicha dublikatlarni o'chirish.

Har bir ommaviy amaldan oldin "nechta yozuv va qiymat o'zgaradi" hisobi va 5 ta misol (oldin → keyin) ko'rsatiladi. Keyin tasdiqlanadi.

### 8.3 Saqlash modeli [M]
- **Tahrir qatlami** sidecar SQLite'da saqlanadi:
  - nuqtaviy tahrirlar yozuv darajasida (o'zgargan yozuvning yangi JSON'i);
  - qo'shilgan va o'chirilgan yozuvlar;
  - ommaviy amallar "qoida" sifatida yoziladi va saqlashda streaming bilan qo'llanadi.
- Saqlanmagan o'zgarishlar belgisi bor (sarlavhada `*`).
- Tahrir qatlami doimiy avtosaqlanadi. Dastur qulasa, keyingi ochilishda tiklashni taklif qiladi.
- Saqlash va boshqacha saqlash streaming bilan. Natija yaroqli JSON ekani tekshiriladi.
- Patch'ni RFC 6902 JSON Patch sifatida eksport va import qilish [K].

---

## 9. Statistika va tekshiruv

- **Maydon statistikasi [M]:**
  - mavjud / `null` / yo'q soni;
  - turlar taqsimoti;
  - noyob qiymatlar soni (katta faylda taxminiy: HyperLogLog);
  - eng ko'p uchragan 20 qiymat va soni;
  - sonlar uchun min / max / o'rtacha;
  - satrlar uchun uzunlik min / max.
  - Qiymat bosilsa, filtr qo'yiladi.
- **Dublikatlar [M]:** tanlangan kalit (masalan `id`) bo'yicha soni va ro'yxati.
- **JSON Schema [K]:** ma'lumotdan schema yasash (draft 2020-12). Faylni schema bo'yicha tekshirish, xatolar yozuv raqami va yo'li bilan chiqadi.
- **Aqlli ko'rinishlar [K]:**
  - unix vaqt (s/ms) va ISO sanalar ustiga borilganda odam o'qiydigan sana (Asia/Tashkent);
  - URL'lar bosiladigan;
  - rang kodlari namunasi;
  - base64 rasm ko'rinishi [I].

---

## 10. Konvertatsiya va fayl amallari

- **JSON ↔ JSONL [M]:**
  - chiroyli yoki ixcham;
  - indent 2 / 4 / tab;
  - kalitlarni tartiblash tanlovi;
  - Unicode `\u` ga aylantirilmaydi.
- **CSV va Excel [M]:**
  - tanlangan ustunlar, ichma-ich maydonlar `a.b.c` ustun bo'ladi;
  - massivlar uchun tanlov: birlashtirib yozish (`;`) yoki har elementga alohida qator;
  - xlsx 1 048 576 qatordan oshsa, avtomatik varaqlarga yoki fayllarga bo'linadi.
- **SQLite'ga eksport [K]:** har massiv uchun jadval, ota-bola bog'lanishi bilan. Keyin SQL bilan so'rov qilish uchun.
- **Faqat tanlangan qismni eksport qilish [M]:** RecordSet (filtr va tartib), tanlangan yozuvlar yoki bitta tugun.
- **Bo'lish va birlashtirish [M]:**
  - katta faylni N qismga bo'lish (soni yoki hajmi bo'yicha);
  - bir nechta faylni bittaga birlashtirish (ixtiyoriy dublikat tozalash bilan);
  - yo'ldagi massivni alohida faylga ajratish.
- **Ta'mirlash (repair) [M]:**
  - `{ {...} }`;
  - oxirgi vergullar;
  - `//` va `/* */` izohlar;
  - bitta tirnoqlar;
  - `NaN` / `Infinity`;
  - BOM;
  - ajratuvchisiz ketma-ket obyektlar;
  - kesilib qolgan fayl: oxirgi to'liq yozuvgacha tiklanadi.
  - Har doim hisobot chiqadi: nima, qayerda, nechta tuzatildi. Hech narsa jimgina tuzatilmaydi.

Barcha eksportlar fonda, progress bilan ishlaydi. Natija vaqtinchalik faylga yozilib, keyin atomik nomlanadi.

---

## 11. Qulaylik va interfeys

- **Buyruqlar palitrasi (Ctrl+Shift+P) [M]:** barcha amallar nomi bo'yicha qidiriladi.
- **To'liq klaviatura boshqaruvi [M]:**
  - ↑↓ yurish, → ochish, ← yopish, `*` hammasini ochish;
  - Enter tahrirlash, F2 nomlash, Del o'chirish;
  - Ctrl+C qiymat, Ctrl+Shift+C yo'l;
  - Ctrl+F, Ctrl+Shift+F, F3, Ctrl+G;
  - Ctrl+Z / Ctrl+Y, Ctrl+S;
  - Alt+← / Alt+→;
  - Ctrl+ +/- shrift o'lchami.
  - Tugmalar sozlamalarda o'zgartiriladi [K].
- **Kontekst menyu [M]:** har tugunda nusxalash (qiymat, yo'l, JSON), "shu qiymat bo'yicha filtr", "shu maydon bo'yicha tartiblash", statistika, hammasini ochish, tahrirlash, o'chirish, yangi tabda ochish.
- **Fayllarni ochish [M]:** oynaga sudrab tashlash, buyruq qatoridan ochish. Windows'da `.json` uchun "Open with" [K].
- **Mavzu [M]:** qorong'i va yorug' (tizimga ergashadi). Monospace shriftni tanlash mumkin.
- **Status qatori [M]:** fayl hajmi, yozuvlar soni, format, kodirovka, joriy yo'l, tanlov, bayt offseti, xotira sarfi, fon vazifalari.
- **Fon vazifalari paneli [M]:** indeks, qidiruv, tartib, eksport. Har birida progress va bekor qilish.
- **Loyiha (workspace) faylini saqlash [K]:** fayl yo'li, mapping, filtrlar, tahrir qatlami, ochiq tablar, joylashuv.
- **Sozlamalar [M]:**
  - namuna hajmi;
  - kesh papkasi va hajm chegarasi;
  - xotira chegarasi;
  - eksport standartlari;
  - til.
- **Loglar [M]:** `%LOCALAPPDATA%\jsonreshape\logs`. Xato oynasida "logni ochish" tugmasi bor.
- **Xato xabarlari [M]:** nima bo'ldi va qanday tuzatish kerak, oddiy tilda.

---

## 12. Tezlik va xotira maqsadlari (5 GB massiv fayl, oddiy noutbuk, NVMe)

| Amal | Maqsad |
|---|---|
| Ochish, birinchi yozuvlar ko'rinishi | < 1 s |
| To'liq indeks (Rust yadro bilan) | ≥ 150 MB/s |
| 1 million bolali tugunni ochish | < 200 ms (virtual) |
| To'liq matn qidiruvi (bayt rejimi) | ≥ 100 MB/s, natijalar darhol chiqa boshlaydi |
| 10 million yozuvni bitta maydon bo'yicha tartiblash | < 2 daqiqa, RAM < 1 GB |
| Mapping yoki tahrirlar bilan eksport | ≥ 50 MB/s, RAM < 300 MB |
| Dasturning umumiy xotirasi | < 500 MB (kesh sozlamaga bog'liq) |

Hammasini o'lchab, README'ga jadval qilib yoz. Maqsadga yetmagan joyini ochiq ayt.

Raqamlar aniqligi yo'qolmasin. Test qiymatlari: `0.1`, `1e400`, `12345678901234567890`, `-0.0`, `1.10`. Natija satrga aylanib qolmasin.

---

## 13. CLI [M]

```
jsonreshape inspect  input.json [--sample 10000]
jsonreshape stats    input.json --field contact.address.city
jsonreshape search   input.json "Toshkent" [--keys|--values] [--regex] [--translit] [--path name]
jsonreshape filter   input.json output.json --where 'contact.address.city == "Toshkent"'
jsonreshape sort     input.json output.json --by name --by -id
jsonreshape apply    mapping.json input.json output.json [--jsonl] [--compact] [--record-path data]
jsonreshape flatten  input.json output.json --children-key children [--depth-field _depth]
jsonreshape convert  input.json output.(jsonl|csv|xlsx|sqlite) [--columns a,b.c]
jsonreshape split    input.json --parts 10 | --size 500MB
jsonreshape merge    a.json b.json -o out.json [--dedupe-by id]
jsonreshape repair   broken.json fixed.json
jsonreshape diff     a.json b.json [--key id]
```

---

## 14. Testlar (qabul mezonlari)

`pytest` va `pytest-qt` bilan. Har bosqich oxirida hammasi yashil bo'lishi shart.

### Mapping (etalon bilan bir xil natija)
1. **Identity:** o'zgarishsiz mapping kirish bilan bir xil natija beradi (massiv ildiz, obyekt ildiz, kalitli daraxt `"child1": {"child1.2": {...}}`).
2. **Rekursiv tekislash:** `children` → `child1, child1.2, child1.2.1, child1.2.3, child2`, `_depth` = `1,2,3,3,1`. Ichkarida `children` qolmaydi.
3. **Ko'chirish:** `contact.address.city` yozuv darajasiga (`name`dan oldin) chiqadi va `address` ichidan yo'qoladi.
4. **Unwrap:** `contact` ochilganda `phone` va `address` yuqoriga chiqadi.
5. **Gather:** `orders[].oid` yozuv darajasida `[7, 8]` bo'ladi. `orders` yo'q yozuvda maydon umuman bo'lmaydi.
6. **Broadcast:** `name` `orders[]` ichiga ko'chirilsa, har order'da `name` bo'ladi.
7. **Guruh:** yangi guruh yaratib, `statusComment`ni ichiga ko'chirish va guruhni `holat` deb nomlash.
8. **Birlashtirish:** `children[].children` → `children` ustiga tashlanganda elementlar ketma-ket ulanadi.
9. **Drag & drop qoidalari:** 7-bo'limdagi 4 ta cheklov.
10. **Etalon bilan solishtirish:** `node` orqali `json-tuzilma.html` engine'i ishga tushirilib, golden natijalar yasaladi. Python natijasi ular bilan aynan bir xil bo'lishi shart.

### Indeks va o'qish
11. Har bir `(start, end)` bo'lagi parse qilinganda asl yozuv bilan bir xil chiqadi. Barcha formatlar (1–7) uchun.
12. Rust yadro va Python fallback bir xil natija beradi.
13. Xato joyi to'g'ri ko'rsatiladi (masalan, 2-qator, 5-ustun). Kesilgan fayldan oxirgi to'liq yozuvgacha ochiladi.
14. Ichki konteynerni talab bo'yicha indekslash: 1 million elementli massiv tugunini ochish xotirani to'ldirmaydi.

### Qidiruv, filtr, tartib
15. Qidiruv natijalari to'liq parse qilib, oddiy tekshirish bilan topilgan natija bilan aynan bir xil (barcha rejimlar).
16. Apostrof variantlari va lotin↔kirill: "Toshkent" ↔ "Тошкент", "O'zbekiston" ↔ "Oʻzbekiston" ↔ "Ўзбекистон".
17. Tartib barqaror, turlarga to'g'ri ishlaydi, tabiiy tartib to'g'ri. Tashqi sort xotirada tartiblangan natija bilan bir xil.
18. Filtr ifodalari va vizual quruvchi bir xil natija beradi. `eval` yo'qligi tekshiriladi.

### Tahrir va saqlash
19. Tahrirlar bilan saqlash, keyin qayta ochish: kutilgan natija chiqadi. Asl fayl o'zgarmaydi. Undo/redo to'liq qaytaradi.
20. Ommaviy amal oldidan berilgan hisob haqiqiy o'zgarishlar soniga teng.
21. Avtosaqlash va qulashdan keyin tiklash ishlaydi.

### Konvertatsiya va boshqalar
22. JSON → JSONL → JSON aylanmasi ma'lumotni o'zgartirmaydi. CSV va xlsx ustunlari to'g'ri. xlsx bo'linishi to'g'ri.
23. Repair: har bir buzilish turi uchun test bor va hisobot to'g'ri.
24. Raqam aniqligi va Unicode testlari (12-bo'lim).

### Katta fayl
25. 2 GB sintetik fayl uchun: indeks, qidiruv, tartib va eksport vaqti hamda peak RSS o'lchanadi va README'ga yoziladi. Xotira chegaralari (12-bo'lim) buzilmaydi.

### GUI (pytest-qt)
26. Faylni ochish, daraxtni ochish/yopish, qidiruv natijasiga o'tish, jadvalda tartiblash, tahrir va undo, eksport tugmasi.

---

## 15. Loyiha tuzilishi (taklif)

```
jsonreshape/
  core/          # Rust (PyO3) yadro + python fallback: scan, search, keys
  reader.py      # formatlarni aniqlash, indeks, yozuvlarni o'qish, xato joyi
  recordset.py   # RecordSet, filtr, tartib (tashqi sort)
  schema.py      # tuzilmani aniqlash, statistika
  query.py       # filtr tili, JMESPath, qidiruv rejimlari, translit
  mapping.py     # mapping modeli, amallar, undo, saqlash
  engine.py      # mapping hisoblash (binding, gather, broadcast, recKey)
  edits.py       # tahrir qatlami (SQLite), ommaviy qoidalar, patch
  writer.py      # streaming yozuvchilar: JSON, JSONL, CSV, XLSX, SQLite
  tools.py       # split, merge, repair, diff
  cli.py
  gui/           # app, tree_view, table_view, text_view, schema_view,
                 # mapping_designer, search_panel, filter_builder,
                 # stats_panel, tasks_panel, command_palette, settings
tests/
README.md
```

---

## 16. Ish tartibi (bosqichlar)

Har bosqich oxirida:
- testlarni ishga tushir;
- README'ni yangila;
- menga qisqa hisobot ber: nima qilindi, nima qolmadi, o'lchovlar.

Keyingi bosqichga o'tishdan oldin mendan tasdiq so'ra.

1. **Yadro:** reader + indeks (Rust yadro va fallback), RecordSet, qidiruv, filtr, tartib, writer, CLI (`inspect`, `search`, `filter`, `sort`, `convert`). Testlar 11–18, 22, 24, 25.
2. **Ko'ruvchi GUI:** daraxt, jadval, matn, tuzilma ko'rinishlari, navigatsiya, qidiruv paneli, filtr quruvchi, tartiblash, fon vazifalari, status qatori.
3. **Tahrirlash:** tugun tahriri, ommaviy amallar, tahrir qatlami, saqlash, avtosaqlash. Testlar 19–21.
4. **Tuzilma quruvchi:** mapping engine + GUI + CLI (`apply`, `flatten`). Testlar 1–10.
5. **Asboblar:** statistika, dublikatlar, JSON Schema, CSV/XLSX/SQLite, split/merge, repair, diff. Testlar 22–23.
6. **Silliqlash:** buyruqlar palitrasi, klaviatura sozlamalari, sessiyani tiklash, workspace, mavzular, PyInstaller `.exe`. Testlar 26 va to'liq benchmark.

Qoidalar:
- Biror xatti-harakat noaniq bo'lsa: mapping uchun `json-tuzilma.html` engine'iga qara. Boshqa hollarda taxmin qilma, menga savol ber.
- Xatoni yashirma, testni o'chirib qo'yma, sababini topib tuzat.
- Maqsadli tezlikka yetib bo'lmasa, o'lchovni ko'rsatib, sababini va variantlarini ayt.
