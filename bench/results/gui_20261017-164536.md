# jsonreshape: GUI benchmarki (2-bosqich, AGENT_PROMPT §12)

Sana: 2026-10-17 16:45:36 · davomiyligi: 6 daq 11 s · rejim: full · to'liq natijalar: `gui_20261017-164536.json`

## §12 maqsadlari GUI ichida

| Amal | Maqsad | Natija | Holat |
|---|---|---|:---:|
| Ochish: jarayon ishga tushganidan daraxtda birinchi yozuvlar chizilguncha (syn_2g.json, 2.1 GB) | < 1 s | 627 ms (`main()` chaqirilganidan 454 ms; interpretator yaratilganidan 622 ms; ko'rinadigan qatorlar to'liq: 454 ms) | ✅ |
| To'liq indeks GUI ichida, UI javob berib turadi (syn_2g.json) | ≥ 150 MB/s; 5 ms taymer oralig'i max < 250 ms | 718 MB/s (2.1 GB, 2.99 s, 6 513 235 ta yozuv); taymer oraliqlari p95 8.3 ms, max 16 ms | ✅ |
| Ochish: jarayon ishga tushganidan daraxtda birinchi yozuvlar chizilguncha (syn_5g.json, 5.4 GB) | < 1 s | 621 ms (`main()` chaqirilganidan 441 ms; interpretator yaratilganidan 616 ms; ko'rinadigan qatorlar to'liq: 441 ms) | ✅ |
| To'liq indeks GUI ichida, UI javob berib turadi (syn_5g.json) | ≥ 150 MB/s; 5 ms taymer oralig'i max < 250 ms | 666 MB/s (5.4 GB, 8.06 s, 16 222 716 ta yozuv); taymer oraliqlari p95 16 ms, max 26 ms | ✅ |
| 1 000 000 bolali tugunni ochish (node_1m.json): bolalari indekslangandan keyin chizilguncha | < 200 ms (virtual) | qayta ochish 2.8 ms; birinchi marta: bolalar indeksi 82 ms + chizish 6.0 ms = 89 ms; oxirgi bolaga sakrash 4.8 ms; 1 000 000 ta bola | ✅ |
| To'liq qidiruv «Qoraqalpog'iston» (syn_2g.json), qidiruv panelidan: birinchi natija chizilguncha va tezlik | ≥ 100 MB/s, birinchi natija < 1 s | 921 MB/s (2.33 s; 66 ta moslik); birinchi natija chizildi: 56 ms; taymer p95 17 ms, max 22 ms | ✅ |
| 10 000 000 ta yozuvni `name` bo'yicha tartiblash, sessiya orqali (syn_10m.jsonl) | < 2 daqiqa, RAM < 1 GB | 21.7 s, peak RSS 454.3 MB (tartiblash bosqichida 439.4 MB); taymer p95 18 ms, max 59 ms; tartiblangan qatorlar chizildi 4.6 ms keyin | ✅ |
| Tartiblangan ko'rinishni JSONL ga eksport, eksport oynasi orqali (syn_10m.jsonl) | ≥ 50 MB/s, RAM < 300 MB | 66.3 MB/s (49.6 s; 10 000 000 ta yozuv, chiqish 3.3 GB); eksport bosqichida RSS 413.0 MB (bosqich boshida 138.2 MB); taymer max 22 ms | ❌ |
| Butun GUI sessiyasining xotirasi, tartiblashsiz (syn_2g.json: ochish, indeks, skroll, jadval, qidiruv, filtr, tuzilma, matn, eksport) | < 500 MB | peak RSS 276.2 MB (tartiblashsiz barcha o'lchovlarning eng kattasi: 276.2 MB — session_2g) | ✅ |

✅ maqsadga yetildi · ❌ yetilmadi (pastda) · — maqsad belgilanmagan. Vaqt — eng yaxshi takror, peak RSS — barcha takrorlarning eng kattasi; MB = 10⁶ bayt.

## Qo'shimcha o'lchovlar

| Amal | Maqsad | Natija | Holat |
|---|---|---|:---:|
| Skrollbarni oxiriga sakratish: ko'rinadigan qatorlar to'liq chizilguncha (syn_2g.json, 6 513 235 ta yozuv) | — (§12 da yo'q) | 6.7 ms (o'rtaga sakrash 8.9 ms) | — |
| Skrollbarni oxiriga sakratish: ko'rinadigan qatorlar to'liq chizilguncha (syn_5g.json, 16 222 716 ta yozuv) | — (§12 da yo'q) | 7.2 ms (o'rtaga sakrash 8.5 ms) | — |
| Ochish: jarayon ishga tushganidan daraxtda birinchi yozuvlar chizilguncha (syn_10m.jsonl, 3.3 GB) | < 1 s | 622 ms (`main()` chaqirilganidan 447 ms; interpretator yaratilganidan 616 ms; ko'rinadigan qatorlar to'liq: 447 ms) | ✅ |
| To'liq indeks GUI ichida, UI javob berib turadi (syn_10m.jsonl) | ≥ 150 MB/s; 5 ms taymer oralig'i max < 250 ms | 656 MB/s (3.3 GB, 5.01 s, 10 000 000 ta yozuv); taymer oraliqlari p95 16 ms, max 22 ms | ✅ |
| Skrollbarni oxiriga sakratish: ko'rinadigan qatorlar to'liq chizilguncha (syn_10m.jsonl, 10 000 000 ta yozuv) | — (§12 da yo'q) | 6.7 ms (o'rtaga sakrash 7.6 ms) | — |
| 1 000 000 elementli tugunni tanlash (node_1m.json): matn paneli uni formatlab ko'rsatguncha | — (§12 da yo'q) | 816 ms; RSS 112.8 MB → 189.9 MB; taymer p95 19 ms, max 24 ms | — |
| To'liq qidiruv «Toshkent» (syn_2g.json), qidiruv panelidan: birinchi natija chizilguncha va tezlik | ≥ 100 MB/s, birinchi natija < 1 s | 663 MB/s (3.24 s; 382 422 ta moslik); birinchi natija chizildi: 25 ms; taymer p95 18 ms, max 22 ms | ✅ |
| Filtr `contact.address.city == "Toshkent"` filtr panelidan (syn_10m.jsonl, 10 000 000 ta yozuv): qisman natijalar | — (§12 da yo'q) | birinchi qisman qatorlar chizildi: 117 ms; hammasi 17.0 s (193 MB/s; 587 025 ta mos yozuv); taymer p95 17 ms, max 25 ms | — |
| Tab yopilgandan keyin RSS (syn_2g.json) | — (§12 da yo'q) | 132.1 MB (fayl ochilishidan oldin 99.1 MB; sessiya davomida eng ko'pi 276.2 MB) | — |

## UI javob berishi (GUI oqimidagi 5 ms taymer)

| Bosqich | Davomiyligi | Oraliqlar p50 / p95 / p99 / max | > 100 ms | > 250 ms |
|---|---:|---|---:|---:|
| open_2g: ochish (birinchi chizishgacha) | 212 ms | 5.0 / 7.0 / 8.0 / 8.0 ms | 0 | 0 |
| open_2g: indeks | 3.00 s | 5.0 / 8.3 / 16.2 / 18.7 ms | 0 | 0 |
| open_2g: skroll | 638 ms | 8.7 / 16.7 / 24.8 / 24.8 ms | 0 | 0 |
| open_5g: ochish (birinchi chizishgacha) | 210 ms | 5.0 / 5.8 / 6.7 / 6.7 ms | 0 | 0 |
| open_5g: indeks | 8.13 s | 5.8 / 16.5 / 17.8 / 25.9 ms | 0 | 0 |
| open_5g: skroll | 625 ms | 8.4 / 16.9 / 17.6 / 17.6 ms | 0 | 0 |
| open_10m: ochish (birinchi chizishgacha) | 213 ms | 5.0 / 6.8 / 7.9 / 7.9 ms | 0 | 0 |
| open_10m: indeks | 5.01 s | 5.2 / 16.0 / 18.1 / 26.5 ms | 0 | 0 |
| open_10m: skroll | 642 ms | 8.3 / 16.9 / 17.6 / 17.6 ms | 0 | 0 |
| node_1m: tugunni tanlash (matn paneli) | 825 ms | 14.1 / 18.8 / 28.4 / 28.4 ms | 0 | 0 |
| node_1m: tugunni birinchi ochish | 90 ms | 5.0 / 7.9 / 7.9 / 7.9 ms | 0 | 0 |
| node_1m: tugunni qayta ochish | 7.0 ms | 7.2 / 7.2 / 7.2 / 7.2 ms | 0 | 0 |
| search_2g: qidiruv «Qoraqalpog'iston» | 2.34 s | 8.8 / 17.0 / 19.6 / 21.7 ms | 0 | 0 |
| search_2g: qidiruv «Toshkent» | 3.25 s | 8.6 / 17.5 / 19.6 / 26.8 ms | 0 | 0 |
| filter_10m: filtr | 17.0 s | 8.8 / 17.5 / 20.0 / 25.0 ms | 0 | 0 |
| sort_export_10m: tartiblash | 21.7 s | 8.6 / 18.1 / 25.1 / 58.8 ms | 0 | 0 |
| sort_export_10m: eksport | 49.8 s | 15.3 / 16.8 / 18.1 / 22.1 ms | 0 | 0 |

## Maqsadga yetilmagan joylar

- **Tartiblangan ko'rinishni JSONL ga eksport, eksport oynasi orqali (syn_10m.jsonl)** — 66.3 MB/s (49.6 s; 10 000 000 ta yozuv, chiqish 3.3 GB); eksport bosqichida RSS 413.0 MB (bosqich boshida 138.2 MB); taymer max 22 ms (maqsad: ≥ 50 MB/s, RAM < 300 MB).
  - Xotira bosqich oxirida: RSS 385.9 MB, xususiy (private) 112.0 MB; ishchi to'plamdagi eng katta fayl xaritalari: d3f7b98dc34e144c33af59ebff629e24354ad453c17ba68620f472b87a5dff18.jri 160.0 MB, rs-44132-cf60a5d008b5.u64 80.0 MB, Qt6Core.dll 10.5 MB, Qt6Gui.dll 9.8 MB, windows.storage.dll 9.1 MB.
  - Bosqichlar va har bir takror JSON faylda (`gui_20261017-164536.json`, `ops.sort_export_10m`); sababini tahlil qilish kerak.

## Ma'lumot fayllari

- `node_1m.json`: 41.7 MB, 1 000 000 ta yozuv (avval yaratilgan)
- `syn_10m.jsonl`: 3.3 GB, 10 000 000 ta yozuv (avval yaratilgan)
- `syn_2g.json`: 2.1 GB, 6 513 235 ta yozuv (avval yaratilgan)
- `syn_5g.json`: 5.4 GB, 16 222 716 ta yozuv (avval yaratilgan)

## Kompyuter

- Protsessor: Intel(R) Core(TM) Ultra 9 285, 24 yadro / 24 mantiqiy protsessor (8 ta P + 16 ta E yadro)
- RAM: 205.6 GB
- Ma'lumotlar diski: SAMSUNG MZVL81T0HELB-00BH1 (NVMe, SSD), E: NTFS, bo'sh joy 346.8 GB
- OS: Windows-11-10.0.26200-SP0; quvvat rejasi: Balanced
- Python 3.14.3; jsonreshape 0.1.0, yadro: rust; orjson 3.13.0, ijson 3.6.0 (yajl2_c); PySide6 6.12.0

## O'lchash shartlari

- Har bir o'lchov alohida yangi jarayonda (`gui_bench.py --child`), yangi bo'sh JSONRESHAPE_HOME bilan (kesh indeksi yo'q): ko'ruvchi foydalanuvchi ishga tushirgandek `jsonreshape.gui.app.main([fayl])` orqali ochiladi (haqiqiy MainWindow, ko'rinishlar, panellar, GC siyosati).
- Qt `offscreen` platformasi (`QT_QPA_FONTDIR=C:/Windows/Fonts`, haqiqiy shriftlar), oyna 1440×900. «Chizildi» — VirtualGrid paint hodisasi haqiqiy kataklar bilan tugadi (yuklanmagan qatorlar o'rnidagi bo'sh joylar sanaladi); «to'liq» — ko'rinadigan barcha kataklar yuklangan.
- «Jarayon ishga tushganidan» — runner jarayonni yaratgan paytdan (venv python.exe ishga tushiruvchisi va importlar ichida); interpretator jarayoni yaratilganidan va `main()` chaqirilganidan vaqtlar ham JSON'da.
- UI javob berishi: GUI oqimida 5 ms li aniq (PreciseTimer) taymer; ketma-ket ikki urish orasidagi vaqt — foydalanuvchi kiritishi kutadigan eng uzoq vaqt. 250 ms dan uzun oraliq — sezilarli qotish (o'z chegaramiz; §12 da raqam yo'q).
- Peak RSS — jarayonlar daraxtining eng katta ishchi to'plami (run_bench.TreeWatcher, Windows peak_wset; venv ishga tushiruvchisi va haqiqiy interpretator); bosqich RSS'i — jarayon ichida har 50 ms olingan namunalarning eng kattasi.
- Takrorlar: 2; har bir qiymatning eng yaxshisi olinadi (kompyuter umumiy).
- Har o'lchovdan oldin tizim CPU yuklamasi ≤ 35.0% bo'lguncha kutildi (ko'pi bilan 60.0 s); ko'rilgan yuklama: 30.3–34.6%.
- OS fayl keshi iliq (ma'lumot fayllari RAM'da); sovuq disk o'lchovlari `bench/results/latest.md` da.

## Nimani o'lchab bo'lmadi

- Haqiqiy ekranga chiqish (GPU, kompozitor, monitor yangilanishi) o'lchanmadi: offscreen platforma faqat Qt'ning o'z chizishini o'lchaydi.
- §12 «oddiy noutbuk» uchun yozilgan; o'lchov Intel(R) Core(TM) Ultra 9 285 da bajarildi.
- Tahrir, undo, mapping bilan eksport — 3- va 4-bosqichlarda.

Qayta o'lchash: `.venv/Scripts/python.exe bench/gui_bench.py` (to'liq) yoki `--quick` (tezkor); bitta o'lchov: `--only NOM` (`--list`). Kutubxona darajasidagi raqamlar: `bench/results/latest.md`.
