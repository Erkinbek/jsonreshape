# jsonreshape: GUI benchmarki (AGENT_PROMPT §12)

Sana: 2026-10-19 19:40:28 · davomiyligi: 5 daq 4 s · rejim: full · to'liq natijalar: `gui_20261019-194028.json`

## §12 maqsadlari GUI ichida

| Amal | Maqsad | Natija | Holat |
|---|---|---|:---:|
| Ochish: jarayon ishga tushganidan daraxtda birinchi yozuvlar chizilguncha (syn_2g.json, 2.1 GB) | < 1 s | 641 ms (`main()` chaqirilganidan 463 ms; interpretator yaratilganidan 635 ms; ko'rinadigan qatorlar to'liq: 463 ms) | ✅ |
| To'liq indeks GUI ichida, UI javob berib turadi (syn_2g.json) | ≥ 150 MB/s; 5 ms taymer oralig'i max < 250 ms | 772 MB/s (2.1 GB, 2.78 s, 6 513 235 ta yozuv); taymer oraliqlari p95 6.0 ms, max 8.9 ms | ✅ |
| Ochish: jarayon ishga tushganidan daraxtda birinchi yozuvlar chizilguncha (syn_5g.json, 5.4 GB) | < 1 s | 627 ms (`main()` chaqirilganidan 456 ms; interpretator yaratilganidan 622 ms; ko'rinadigan qatorlar to'liq: 456 ms) | ✅ |
| To'liq indeks GUI ichida, UI javob berib turadi (syn_5g.json) | ≥ 150 MB/s; 5 ms taymer oralig'i max < 250 ms | 749 MB/s (5.4 GB, 7.17 s, 16 222 716 ta yozuv); taymer oraliqlari p95 5.9 ms, max 9.5 ms | ✅ |
| 1 000 000 bolali tugunni ochish (node_1m.json): bolalari indekslangandan keyin chizilguncha | < 200 ms (virtual) | qayta ochish 3.0 ms; birinchi marta: bolalar indeksi 98 ms + chizish 6.0 ms = 104 ms; oxirgi bolaga sakrash 4.7 ms; 1 000 000 ta bola | ✅ |
| To'liq qidiruv «Qoraqalpog'iston» (syn_2g.json), qidiruv panelidan: birinchi natija chizilguncha va tezlik | ≥ 100 MB/s, birinchi natija < 1 s | 1 086 MB/s (1.98 s; 66 ta moslik); birinchi natija chizildi: 46 ms; taymer p95 5.9 ms, max 7.5 ms | ✅ |
| 10 000 000 ta yozuvni `name` bo'yicha tartiblash, sessiya orqali (syn_10m.jsonl) | < 2 daqiqa, RAM < 1 GB | 17.2 s, peak RSS 470.4 MB (tartiblash bosqichida 453.4 MB); taymer p95 6.8 ms, max 23 ms; tartiblangan qatorlar chizildi 3.3 ms keyin | ✅ |
| Tartiblangan ko'rinishni JSONL ga eksport, eksport oynasi orqali (syn_10m.jsonl) | ≥ 50 MB/s, RAM < 300 MB | 79.7 MB/s (41.3 s; 10 000 000 ta yozuv, chiqish 3.3 GB); eksport bosqichida RSS 429.3 MB (bosqich boshida 159.1 MB); taymer max 7.5 ms | ❌ |
| Butun GUI sessiyasining xotirasi, tartiblashsiz (syn_2g.json: ochish, indeks, skroll, jadval, qidiruv, filtr, tuzilma, matn, eksport) | < 500 MB | peak RSS 289.5 MB (tartiblashsiz barcha o'lchovlarning eng kattasi: 289.5 MB — session_2g) | ✅ |

✅ maqsadga yetildi · ❌ yetilmadi (pastda) · — maqsad belgilanmagan. Vaqt — eng yaxshi takror, peak RSS — barcha takrorlarning eng kattasi; MB = 10⁶ bayt.

## Qo'shimcha o'lchovlar

| Amal | Maqsad | Natija | Holat |
|---|---|---|:---:|
| Skrollbarni oxiriga sakratish: ko'rinadigan qatorlar to'liq chizilguncha (syn_2g.json, 6 513 235 ta yozuv) | — (§12 da yo'q) | 5.8 ms (o'rtaga sakrash 7.0 ms) | — |
| Skrollbarni oxiriga sakratish: ko'rinadigan qatorlar to'liq chizilguncha (syn_5g.json, 16 222 716 ta yozuv) | — (§12 da yo'q) | 5.6 ms (o'rtaga sakrash 7.0 ms) | — |
| Ochish: jarayon ishga tushganidan daraxtda birinchi yozuvlar chizilguncha (syn_10m.jsonl, 3.3 GB) | < 1 s | 621 ms (`main()` chaqirilganidan 457 ms; interpretator yaratilganidan 616 ms; ko'rinadigan qatorlar to'liq: 457 ms) | ✅ |
| To'liq indeks GUI ichida, UI javob berib turadi (syn_10m.jsonl) | ≥ 150 MB/s; 5 ms taymer oralig'i max < 250 ms | 722 MB/s (3.3 GB, 4.56 s, 10 000 000 ta yozuv); taymer oraliqlari p95 5.9 ms, max 7.4 ms | ✅ |
| Skrollbarni oxiriga sakratish: ko'rinadigan qatorlar to'liq chizilguncha (syn_10m.jsonl, 10 000 000 ta yozuv) | — (§12 da yo'q) | 5.6 ms (o'rtaga sakrash 7.4 ms) | — |
| 1 000 000 elementli tugunni tanlash (node_1m.json): matn paneli uni formatlab ko'rsatguncha | — (§12 da yo'q) | 780 ms; RSS 125.6 MB → 203.5 MB; taymer p95 19 ms, max 19 ms | — |
| To'liq qidiruv «Toshkent» (syn_2g.json), qidiruv panelidan: birinchi natija chizilguncha va tezlik | ≥ 100 MB/s, birinchi natija < 1 s | 818 MB/s (2.63 s; 382 422 ta moslik); birinchi natija chizildi: 20 ms; taymer p95 6.0 ms, max 15 ms | ✅ |
| Filtr `contact.address.city == "Toshkent"` filtr panelidan (syn_10m.jsonl, 10 000 000 ta yozuv): qisman natijalar | — (§12 da yo'q) | birinchi qisman qatorlar chizildi: 143 ms; hammasi 14.0 s (236 MB/s; 587 025 ta mos yozuv); taymer p95 6.3 ms, max 12 ms | — |
| Tab yopilgandan keyin RSS (syn_2g.json) | — (§12 da yo'q) | 146.5 MB (fayl ochilishidan oldin 115.5 MB; sessiya davomida eng ko'pi 289.5 MB) | — |

## UI javob berishi (GUI oqimidagi 5 ms taymer)

| Bosqich | Davomiyligi | Oraliqlar p50 / p95 / p99 / max | > 100 ms | > 250 ms |
|---|---:|---|---:|---:|
| open_2g: ochish (birinchi chizishgacha) | 220 ms | 5.0 / 6.1 / 8.4 / 8.4 ms | 0 | 0 |
| open_2g: indeks | 2.79 s | 5.0 / 6.0 / 6.4 / 8.9 ms | 0 | 0 |
| open_2g: skroll | 630 ms | 5.0 / 5.9 / 6.4 / 6.6 ms | 0 | 0 |
| open_5g: ochish (birinchi chizishgacha) | 216 ms | 4.9 / 6.0 / 6.8 / 6.8 ms | 0 | 0 |
| open_5g: indeks | 7.17 s | 5.0 / 5.9 / 6.5 / 9.5 ms | 0 | 0 |
| open_5g: skroll | 620 ms | 5.0 / 6.0 / 6.2 / 6.2 ms | 0 | 0 |
| open_10m: ochish (birinchi chizishgacha) | 212 ms | 5.1 / 6.0 / 6.8 / 6.8 ms | 0 | 0 |
| open_10m: indeks | 4.56 s | 5.0 / 5.9 / 7.7 / 9.0 ms | 0 | 0 |
| open_10m: skroll | 630 ms | 5.0 / 5.8 / 6.2 / 6.8 ms | 0 | 0 |
| node_1m: tugunni tanlash (matn paneli) | 797 ms | 13.1 / 19.4 / 24.1 / 24.1 ms | 0 | 0 |
| node_1m: tugunni birinchi ochish | 108 ms | 5.1 / 6.0 / 6.6 / 6.6 ms | 0 | 0 |
| node_1m: tugunni qayta ochish | 6.0 ms | 6.4 / 6.4 / 6.4 / 6.4 ms | 0 | 0 |
| search_2g: qidiruv «Qoraqalpog'iston» | 1.98 s | 5.0 / 6.1 / 7.1 / 10.4 ms | 0 | 0 |
| search_2g: qidiruv «Toshkent» | 2.65 s | 5.0 / 6.0 / 7.1 / 14.6 ms | 0 | 0 |
| filter_10m: filtr | 14.0 s | 5.0 / 6.4 / 9.2 / 12.5 ms | 0 | 0 |
| sort_export_10m: tartiblash | 17.2 s | 5.0 / 7.5 / 12.0 / 22.6 ms | 0 | 0 |
| sort_export_10m: eksport | 41.9 s | 5.0 / 6.0 / 6.3 / 8.4 ms | 0 | 0 |

## Maqsadga yetilmagan joylar

- **Tartiblangan ko'rinishni JSONL ga eksport, eksport oynasi orqali (syn_10m.jsonl)** — 79.7 MB/s (41.3 s; 10 000 000 ta yozuv, chiqish 3.3 GB); eksport bosqichida RSS 429.3 MB (bosqich boshida 159.1 MB); taymer max 7.5 ms (maqsad: ≥ 50 MB/s, RAM < 300 MB).
  - Xotira bosqich oxirida: RSS 399.3 MB, xususiy (private) 126.5 MB; ishchi to'plamdagi eng katta fayl xaritalari: d3f7b98dc34e144c33af59ebff629e24354ad453c17ba68620f472b87a5dff18.jri 160.0 MB, rs-41572-5b2f54d42172.u64 80.0 MB, Qt6Core.dll 10.5 MB, Qt6Gui.dll 9.8 MB, windows.storage.dll 9.1 MB.
  - Sabab: tartiblangan eksport yozuvlarni kalit tartibida o'qiydi: tartib bo'yicha o'qilgan indeks (`.jri`, 160.0 MB) va natija RecordSet'i (`.u64`, 80.0 MB) fayllarining xotiraga proyeksiyalangan sahifalari ishchi to'plamda qoladi (OS kerak bo'lsa ularni darhol bo'shatadi); jarayonning xususiy xotirasi — 126.5 MB.
  - Variantlar: 1) maqsadni xususiy xotira bo'yicha hisoblash (126.5 MB — maqsad ichida); 2) indeks oynalarini o'qilgandan keyin ochib-yopib turish yoki `VirtualUnlock`/`DiscardVirtualMemory` bilan bo'shatish (biroz sekinroq); 3) eksportdan oldin indeksni tartib bo'yicha qayta yozish (qo'shimcha disk).
  - Bosqichlar va har bir takror JSON faylda (`gui_20261019-194028.json`, `ops.sort_export_10m`).

## Ma'lumot fayllari

- `node_1m.json`: 41.7 MB, 1 000 000 ta yozuv (avval yaratilgan)
- `syn_10m.jsonl`: 3.3 GB, 10 000 000 ta yozuv (avval yaratilgan)
- `syn_2g.json`: 2.1 GB, 6 513 235 ta yozuv (avval yaratilgan)
- `syn_5g.json`: 5.4 GB, 16 222 716 ta yozuv (avval yaratilgan)

## Kompyuter

- Protsessor: Intel(R) Core(TM) Ultra 9 285, 24 yadro / 24 mantiqiy protsessor (8 ta P + 16 ta E yadro)
- RAM: 205.6 GB
- Ma'lumotlar diski: SAMSUNG MZVL81T0HELB-00BH1 (NVMe, SSD), E: NTFS, bo'sh joy 376.3 GB
- OS: Windows-11-10.0.26200-SP0; quvvat rejasi: Balanced
- Python 3.14.3; jsonreshape 0.1.0, yadro: rust; orjson 3.13.0, ijson 3.6.0 (yajl2_c); PySide6 6.12.0

## O'lchash shartlari

- Har bir o'lchov alohida yangi jarayonda (`gui_bench.py --child`), yangi bo'sh JSONRESHAPE_HOME bilan (kesh indeksi yo'q): ko'ruvchi foydalanuvchi ishga tushirgandek `jsonreshape.gui.app.main([fayl])` orqali ochiladi (haqiqiy MainWindow, ko'rinishlar, panellar, GC siyosati).
- Qt `offscreen` platformasi (`QT_QPA_FONTDIR=C:/Windows/Fonts`, haqiqiy shriftlar), oyna 1440×900. «Chizildi» — VirtualGrid paint hodisasi haqiqiy kataklar bilan tugadi (yuklanmagan qatorlar o'rnidagi bo'sh joylar sanaladi); «to'liq» — ko'rinadigan barcha kataklar yuklangan.
- «Jarayon ishga tushganidan» — runner jarayonni yaratgan paytdan (venv python.exe ishga tushiruvchisi va importlar ichida); interpretator jarayoni yaratilganidan va `main()` chaqirilganidan vaqtlar ham JSON'da.
- UI javob berishi: GUI oqimida 5 ms li aniq (PreciseTimer) taymer; ketma-ket ikki urish orasidagi vaqt — foydalanuvchi kiritishi kutadigan eng uzoq vaqt. 250 ms dan uzun oraliq — sezilarli qotish (o'z chegaramiz; §12 da raqam yo'q).
- Peak RSS — jarayonlar daraxtining eng katta ishchi to'plami (run_bench.TreeWatcher, Windows peak_wset; venv ishga tushiruvchisi va haqiqiy interpretator); bosqich RSS'i — jarayon ichida har 50 ms olingan namunalarning eng kattasi.
- Takrorlar: 2; har bir qiymatning eng yaxshisi olinadi (kompyuter umumiy).
- Har o'lchovdan oldin tizim CPU yuklamasi ≤ 35.0% bo'lguncha kutildi (ko'pi bilan 60.0 s); ko'rilgan yuklama: 2.4–14.5%.
- OS fayl keshi iliq (ma'lumot fayllari RAM'da); sovuq disk o'lchovlari `bench/results/latest.md` da.

## Nimani o'lchab bo'lmadi

- Haqiqiy ekranga chiqish (GPU, kompozitor, monitor yangilanishi) o'lchanmadi: offscreen platforma faqat Qt'ning o'z chizishini o'lchaydi.
- §12 «oddiy noutbuk» uchun yozilgan; o'lchov Intel(R) Core(TM) Ultra 9 285 da bajarildi.
- Tahrir, undo, mapping bilan eksport va 5-bosqich asboblari GUI ichida alohida o'lchanmadi: GUI ularni o'sha funksiyalar bilan fon vazifalarida bajaradi; kutubxona / buyruq darajasidagi raqamlar — `edit_latest.md`, `export_latest.md`, `latest.md` (mapping) va `tools_latest.md`. Tahrir, undo, saqlash va eksport tugmasi haqiqiy oynada `tests/test_gui_e2e.py` (14-bo'lim, 26-test) da tekshiriladi.

Qayta o'lchash: `.venv/Scripts/python.exe bench/gui_bench.py` (to'liq) yoki `--quick` (tezkor); bitta o'lchov: `--only NOM` (`--list`). Kutubxona darajasidagi raqamlar: `bench/results/latest.md`.
