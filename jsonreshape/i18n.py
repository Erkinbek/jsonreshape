"""The single dictionary of user-facing texts (Uzbek Latin primary, English fallback).

Usage: ``t("error.cancelled")`` or ``t("json.error.at", line=2, column=5)``.

Rules:
* every user-visible string in the package goes through :func:`t`;
* each module adds keys only inside its own ``# ---- <section> ----`` block and prefixes
  them with the section name;
* every key has both ``uz`` and ``en`` texts (a test enforces this);
* parameters use ``str.format`` syntax; a missing parameter leaves the placeholder visible
  instead of raising.
"""

from __future__ import annotations

import os
import string
import threading

SUPPORTED_LANGUAGES = ("uz", "en")
DEFAULT_LANGUAGE = "uz"

MESSAGES: dict[str, dict[str, str]] = {
    # ---- common ----
    "common.app_name": {"uz": "JSON Reshape", "en": "JSON Reshape"},
    "common.yes": {"uz": "ha", "en": "yes"},
    "common.no": {"uz": "yo'q", "en": "no"},
    "common.unknown": {"uz": "noma'lum", "en": "unknown"},
    "common.records": {"uz": "yozuvlar", "en": "records"},
    "common.bytes": {"uz": "bayt", "en": "bytes"},
    "common.seconds": {"uz": "soniya", "en": "seconds"},
    # ---- errors ----
    "error.generic": {"uz": "Xatolik yuz berdi: {detail}", "en": "An error occurred: {detail}"},
    "error.generic.hint": {
        "uz": "Batafsil ma'lumot log faylida: {log}",
        "en": "Details are in the log file: {log}",
    },
    "error.cancelled": {"uz": "Amal bekor qilindi.", "en": "The operation was cancelled."},
    "error.user_input": {"uz": "Noto'g'ri kiritish: {detail}", "en": "Invalid input: {detail}"},
    "error.query_syntax": {
        "uz": "Ifodada xato ({pos}-belgi): {detail}",
        "en": "Expression error at character {pos}: {detail}",
    },
    "error.query_syntax.hint": {
        "uz": "Misol: contact.address.city == 'Toshkent' and len(children) > 2",
        "en": "Example: contact.address.city == 'Toshkent' and len(children) > 2",
    },
    "error.path_syntax": {
        "uz": "Yo'l noto'g'ri yozilgan ({pos}-belgi): {detail}",
        "en": "Invalid path at character {pos}: {detail}",
    },
    "error.path_syntax.hint": {
        "uz": "Misollar: data[120].contact.address, $.data[120], /data/120, #120",
        "en": "Examples: data[120].contact.address, $.data[120], /data/120, #120",
    },
    "error.json_syntax": {
        "uz": "JSON xatosi: {reason} ({line}-qator, {column}-ustun, {offset}-bayt).",
        "en": "JSON error: {reason} (line {line}, column {column}, byte {offset}).",
    },
    "error.json_syntax.hint": {
        "uz": "Xatogacha bo'lgan yozuvlar bilan ishlash mumkin; faylning o'zini matn muharririda tuzating.",
        "en": "You can work with the records before the error; fix the file itself in a text editor.",
    },
    "error.json_decode": {
        "uz": "JSON matnini o'qib bo'lmadi: {detail}",
        "en": "Could not decode JSON text: {detail}",
    },
    "error.file_format": {"uz": "Fayl formati muammosi: {detail}", "en": "File format problem: {detail}"},
    "error.too_large": {
        "uz": "Tugun juda katta ({size}); to'liq o'qilmaydi. Daraxtda bo'laklab oching.",
        "en": "The node is too large ({size}) to load fully. Expand it in the tree instead.",
    },
    "error.file_changed": {
        "uz": "Fayl ish davomida boshqa dastur tomonidan o'zgartirildi: {path}",
        "en": "The file was changed by another program while it was being read: {path}",
    },
    "error.file_changed.hint": {
        "uz": "Natija saqlanmadi. Fayl o'zgarishi tugagach, buyruqni qayta ishga tushiring (indeks qaytadan quriladi).",
        "en": "Nothing was saved. Run the command again once the file has stopped changing (it will be re-indexed).",
    },
    "error.export": {"uz": "Eksport muvaffaqiyatsiz: {detail}", "en": "Export failed: {detail}"},
    "error.index_build": {
        "uz": "Indeksni qurib bo'lmadi: {detail}",
        "en": "Could not build the index: {detail}",
    },
    "error.file_not_found": {"uz": "Fayl topilmadi: {path}", "en": "File not found: {path}"},
    "error.permission": {
        "uz": "Faylga kirish huquqi yo'q: {path}",
        "en": "Permission denied: {path}",
    },
    # ---- json (scanner error codes; used by reader) ----
    "json.error.unexpected_char": {"uz": "kutilmagan belgi", "en": "unexpected character"},
    "json.error.unexpected_eof": {"uz": "fayl kutilmaganda tugadi", "en": "unexpected end of data"},
    "json.error.invalid_number": {"uz": "noto'g'ri son", "en": "invalid number"},
    "json.error.invalid_literal": {
        "uz": "noto'g'ri qiymat (true/false/null kutilgan)",
        "en": "invalid literal (expected true/false/null)",
    },
    "json.error.invalid_escape": {"uz": "\\ dan keyin noto'g'ri belgi", "en": "invalid escape sequence"},
    "json.error.invalid_hex": {"uz": "\\u dan keyin noto'g'ri o'n oltilik raqam", "en": "invalid hex digit in \\u escape"},
    "json.error.control_char": {
        "uz": "satr ichida boshqaruv belgisi (masalan, qator tashlash)",
        "en": "control character inside a string (e.g. a raw newline)",
    },
    "json.error.invalid_utf8": {"uz": "noto'g'ri UTF-8 bayt", "en": "invalid UTF-8 byte"},
    "json.error.trailing_data": {
        "uz": "qiymatdan keyin ortiqcha ma'lumot",
        "en": "extra data after the value",
    },
    "json.error.depth_limit": {"uz": "ichma-ichlik juda chuqur", "en": "nesting is too deep"},
    "json.error.target_not_found": {
        "uz": "tanlangan yozuvlar yo'li (--record-path) topilmadi",
        "en": "the selected record path was not found",
    },
    "json.error.target_not_container": {
        "uz": "tanlangan yo'ldagi qiymat massiv yoki obyekt emas",
        "en": "the value at the selected path is not an array or object",
    },
    "json.expected.value": {"uz": "qiymat kutilgan", "en": "a value was expected"},
    "json.expected.value_or_end": {"uz": "qiymat yoki ']' kutilgan", "en": "a value or ']' was expected"},
    "json.expected.key_or_end": {
        "uz": "kalit (\"...\") yoki '}' kutilgan",
        "en": "a key (\"...\") or '}' was expected",
    },
    "json.expected.key": {"uz": "kalit (\"...\") kutilgan", "en": "a key (\"...\") was expected"},
    "json.expected.colon": {"uz": "':' kutilgan", "en": "':' was expected"},
    "json.expected.comma_or_bracket": {"uz": "',' yoki ']' kutilgan", "en": "',' or ']' was expected"},
    "json.expected.comma_or_brace": {"uz": "',' yoki '}' kutilgan", "en": "',' or '}' was expected"},
    "json.expected.open_brace": {"uz": "'{' kutilgan", "en": "'{' was expected"},
    "json.expected.end": {"uz": "fayl oxiri kutilgan", "en": "end of data was expected"},
    "json.expected.digit": {"uz": "raqam kutilgan", "en": "a digit was expected"},
    "json.expected.hex": {"uz": "o'n oltilik raqam (0-9, a-f) kutilgan", "en": "a hex digit was expected"},
    "json.expected.escape": {
        "uz": "\\ dan keyin \", \\, /, b, f, n, r, t yoki u kutilgan",
        "en": "an escape character was expected",
    },
    "json.expected.string_end": {"uz": "satrni yopuvchi '\"' kutilgan", "en": "a closing '\"' was expected"},
    "json.expected.literal": {"uz": "true, false yoki null kutilgan", "en": "true, false or null was expected"},
    "json.expected.none": {"uz": "", "en": ""},
    # line-oriented variants for broken JSON Lines lines (the texts above speak of the whole file)
    "json.line_error.unexpected_eof": {"uz": "qator kutilmaganda tugadi", "en": "the line ended unexpectedly"},
    "json.line_expected.end": {
        "uz": "qator oxiri kutilgan (bir qatorda bitta qiymat bo'ladi)",
        "en": "end of the line was expected (one value per line)",
    },
    # ---- tasks ----
    "tasks.progress": {
        "uz": "{message} {percent} {done}/{total} {rate}/s, qoldi {eta}",
        "en": "{message} {percent} {done}/{total} {rate}/s, {eta} left",
    },
    # ---- codec ----
    # ---- paths ----
    "paths.err.wildcard_not_allowed": {"uz": "bu yerda * yoki [*] ishlatib bo'lmaydi", "en": "wildcards are not allowed here"},
    "paths.err.expected_dot_after_root": {"uz": "'$' dan keyin '.' yoki '[' kutilgan", "en": "expected '.' or '[' after '$'"},
    "paths.err.key_after_descendants": {"uz": "'..' dan keyin kalit kutilgan", "en": "expected a key after '..'"},
    "paths.err.key_after_dot": {"uz": "'.' dan keyin kalit kutilgan", "en": "expected a key after '.'"},
    "paths.err.bracket_after_dot": {"uz": "'.' dan keyin '[' kelishi mumkin emas", "en": "unexpected '[' after '.'"},
    "paths.err.unexpected_char": {"uz": "kutilmagan belgi {char}", "en": "unexpected character {char}"},
    "paths.err.empty_key": {"uz": "bo'sh kalit", "en": "empty key"},
    "paths.err.unterminated_bracket": {"uz": "'[' yopilmagan", "en": "unterminated '['"},
    "paths.err.bad_bracket": {"uz": "'[...]' ichida indeks, '*' yoki qo'shtirnoqli kalit kutilgan", "en": "expected an index, '*' or a quoted key inside '[...]'"},
    "paths.err.negative_index": {"uz": "manfiy indeks qo'llab-quvvatlanmaydi", "en": "negative indices are not supported"},
    "paths.err.expected_close": {"uz": "']' kutilgan", "en": "expected ']'"},
    "paths.err.unterminated_quote": {"uz": "qo'shtirnoqli kalit yopilmagan", "en": "unterminated quoted key"},
    "paths.err.unterminated_escape": {"uz": "escape (\\) tugallanmagan", "en": "unterminated escape"},
    "paths.err.bad_u_escape": {"uz": "noto'g'ri \\u escape", "en": "invalid \\u escape"},
    "paths.err.bad_escape": {"uz": "noto'g'ri escape \\{char}", "en": "invalid escape \\{char}"},
    "paths.err.pointer_start": {"uz": "JSON Pointer '/' bilan boshlanishi kerak", "en": "a JSON Pointer must start with '/'"},
    "paths.err.pointer_tilde": {"uz": "noto'g'ri '~' escape (~0 yoki ~1 ishlating)", "en": "invalid '~' escape (use ~0 or ~1)"},
    "paths.err.record_number": {"uz": "'#' dan keyin yozuv raqami kutilgan", "en": "expected a record number after '#'"},
    "paths.err.after_record": {"uz": "yozuv raqamidan keyin '.', '[' yoki '/' kutilgan", "en": "expected '.', '[' or '/' after the record number"},
    # ---- fileio ----
    "fileio.error.is_directory": {"uz": "Bu fayl emas, papka: {path}", "en": "This is a folder, not a file: {path}"},
    "fileio.error.disk_full": {"uz": "Diskda bo'sh joy yetarli emas: {path}", "en": "Not enough free disk space: {path}"},
    "fileio.error.disk_full.hint": {
        "uz": "Diskda joy bo'shatib, qayta urinib ko'ring.",
        "en": "Free up some disk space and try again.",
    },
    "fileio.error.open": {"uz": "Faylni ochib bo'lmadi: {path} ({detail})", "en": "Could not open the file: {path} ({detail})"},
    "fileio.error.replace": {
        "uz": "Faylni yozib bo'lmadi: {path}. U boshqa dasturda (masalan, Excel'da) ochiq bo'lishi mumkin.",
        "en": "Could not write the file: {path}. It may be open in another program (for example Excel).",
    },
    "fileio.error.replace.hint": {
        "uz": "Faylni boshqa dasturda yoping va qayta urinib ko'ring.",
        "en": "Close the file in the other program and try again.",
    },
    "fileio.error.utf32": {
        "uz": "Fayl {encoding} kodirovkasida saqlangan, bu kodirovka qo'llab-quvvatlanmaydi: {path}",
        "en": "The file is encoded in {encoding}, which is not supported: {path}",
    },
    "fileio.error.utf32.hint": {
        "uz": "Faylni UTF-8 yoki UTF-16 kodirovkasida qayta saqlang (masalan, Notepad'da: «Boshqacha saqlash» → «Kodirovka: UTF-8»).",
        "en": "Save the file again as UTF-8 or UTF-16 (for example in Notepad: \"Save As\" → \"Encoding: UTF-8\").",
    },
    "fileio.error.decode": {
        "uz": "Faylni {encoding} sifatida o'qib bo'lmadi: {offset}-baytda noto'g'ri ma'lumot ({path})",
        "en": "The file could not be read as {encoding}: invalid data at byte {offset} ({path})",
    },
    "fileio.error.decode.hint": {
        "uz": "Fayl buzilgan yoki boshqa kodirovkada saqlangan bo'lishi mumkin.",
        "en": "The file may be damaged or saved in a different encoding.",
    },
    "fileio.error.gzip": {"uz": "Gzip arxivi buzilgan: {path} ({detail})", "en": "The gzip archive is damaged: {path} ({detail})"},
    "fileio.warning.gzip_truncated": {
        "uz": "Gzip arxivi oxirigacha yo'q (kesilgan): faqat ochilgan qismi ko'rsatiladi.",
        "en": "The gzip archive is truncated: only the part that could be decompressed is shown.",
    },
    "fileio.warning.gzip_trailing": {
        "uz": "Gzip arxividan keyin ortiqcha ma'lumot bor; u e'tiborga olinmadi.",
        "en": "There is extra data after the gzip archive; it was ignored.",
    },
    "fileio.progress.decompress": {"uz": "Arxiv ochilmoqda…", "en": "Decompressing…"},
    "fileio.progress.transcode": {"uz": "UTF-16 dan UTF-8 ga o'girilmoqda…", "en": "Converting UTF-16 to UTF-8…"},
    "fileio.error.in_use": {
        "uz": "Fayl boshqa dastur tomonidan band qilingan: {path}",
        "en": "The file is in use by another program: {path}",
    },
    "fileio.error.in_use.hint": {
        "uz": "Faylni ishlatayotgan dasturni yoping (yoki u ishini tugatishini kuting) va qayta urinib ko'ring.",
        "en": "Close the program that uses the file (or wait until it finishes) and try again.",
    },
    "fileio.error.read_only": {
        "uz": "Faylni qayta yozib bo'lmaydi: u faqat o'qish uchun (read-only): {path}",
        "en": "The file cannot be overwritten because it is read-only: {path}",
    },
    "fileio.error.read_only.hint": {
        "uz": "Fayl xususiyatlarida «Faqat o'qish uchun» belgisini olib tashlang yoki boshqa fayl nomini tanlang.",
        "en": "Clear the \"Read-only\" attribute in the file's properties or choose another file name.",
    },
    "fileio.error.write": {"uz": "Faylga yozib bo'lmadi: {path} ({detail})", "en": "Could not write the file: {path} ({detail})"},
    "fileio.error.write.hint": {
        "uz": "Diskda bo'sh joy borligini va disk (masalan, tarmoq yoki USB disk) hali ulanganligini tekshiring.",
        "en": "Check that the drive has free space and is still connected (for example a network or USB drive).",
    },
    "fileio.error.no_work_dir": {
        "uz": "Dastur fayllari uchun papka yaratib bo'lmadi: {path} ({detail})",
        "en": "No folder for the program's files could be created: {path} ({detail})",
    },
    "fileio.error.no_work_dir.hint": {
        "uz": "Diskda joy va yozish huquqi borligini tekshiring yoki JSONRESHAPE_HOME muhit o'zgaruvchisida boshqa papkani ko'rsating.",
        "en": "Check the free space and write permission, or set the JSONRESHAPE_HOME environment variable to another folder.",
    },
    "fileio.reason.permission": {"uz": "u yerga yozish huquqi yo'q", "en": "no permission to write there"},
    "fileio.reason.not_a_folder": {"uz": "bu nomda papka emas, fayl turibdi", "en": "a file of that name is in the way"},
    "fileio.reason.not_found": {
        "uz": "bunday yo'l yo'q (disk ulanganmi?)",
        "en": "the path does not exist (is the drive connected?)",
    },
    "fileio.reason.disk_full": {"uz": "diskda joy qolmagan", "en": "the disk is full"},
    "fileio.warning.app_dir_fallback": {
        "uz": "Dastur ma'lumotlari papkasidan foydalanib bo'lmadi ({path}: {detail}). Vaqtinchalik papka "
              "ishlatilmoqda: {fallback}. Boshqa papkani JSONRESHAPE_HOME muhit o'zgaruvchisida ko'rsatish mumkin.",
        "en": "The app data folder cannot be used ({path}: {detail}). A temporary folder is used instead: "
              "{fallback}. Another folder can be set with the JSONRESHAPE_HOME environment variable.",
    },
    "fileio.warning.cache_dir_fallback": {
        "uz": "Kesh papkasidan foydalanib bo'lmadi ({path}: {detail}). Uning o'rniga {fallback} ishlatilmoqda "
              "(Sozlamalar: kesh papkasi).",
        "en": "The cache folder cannot be used ({path}: {detail}). {fallback} is used instead (Settings: cache folder).",
    },
    # ---- settings ----
    "settings.invalid": {
        "uz": "Sozlama noto'g'ri: «{label}» = {value}. Kutilgan qiymat: {expected}.",
        "en": "Invalid setting: \"{label}\" = {value}. Expected: {expected}.",
    },
    "settings.invalid.hint": {
        "uz": "Sozlamalar oynasida qiymatni tuzating.",
        "en": "Correct the value in the settings window.",
    },
    "settings.expected.int_min": {"uz": "{min} yoki undan katta butun son", "en": "a whole number of at least {min}"},
    "settings.expected.int_range": {"uz": "{min} dan {max} gacha butun son", "en": "a whole number from {min} to {max}"},
    "settings.expected.path": {"uz": "papka yo'li yoki bo'sh qiymat", "en": "a folder path or nothing"},
    "settings.expected.language": {"uz": "uz (o'zbekcha) yoki en (inglizcha)", "en": "uz (Uzbek) or en (English)"},
    "settings.expected.indent": {
        "uz": "bo'sh (ixcham), 0 dan 16 gacha son yoki tab",
        "en": "nothing (compact), a number from 0 to 16, or tab",
    },
    "settings.expected.bool": {"uz": "ha yoki yo'q (true/false)", "en": "yes or no (true/false)"},
    "settings.expected.delimiter": {
        "uz": "bitta belgi (qo'shtirnoq yoki qator tashlash bo'lmasin)",
        "en": "a single character (not a double quote or a line break)",
    },
    "settings.expected.separator": {
        "uz": "1–16 belgidan iborat matn, qator tashlashsiz",
        "en": "1 to 16 characters without line breaks",
    },
    "settings.field.sample_size": {"uz": "Namuna hajmi (yozuvlar soni)", "en": "Sample size (records)"},
    "settings.field.cache_dir": {"uz": "Kesh papkasi", "en": "Cache folder"},
    "settings.field.cache_limit_bytes": {"uz": "Kesh hajmi chegarasi", "en": "Cache size limit"},
    "settings.field.memory_limit_bytes": {"uz": "Xotira chegarasi", "en": "Memory limit"},
    "settings.field.sort_memory_bytes": {"uz": "Tartiblash uchun xotira", "en": "Memory for sorting"},
    "settings.field.large_node_bytes": {"uz": "Katta tugun chegarasi (hajm)", "en": "Large node threshold (size)"},
    "settings.field.large_node_children": {
        "uz": "Katta tugun chegarasi (bolalar soni)",
        "en": "Large node threshold (children)",
    },
    "settings.field.head_bytes": {
        "uz": "Formatni aniqlash uchun o'qiladigan qism",
        "en": "Bytes read to detect the format",
    },
    "settings.field.language": {"uz": "Til", "en": "Language"},
    "settings.field.export_indent": {"uz": "Eksportdagi chekinish", "en": "Export indentation"},
    "settings.field.csv_bom": {"uz": "CSV boshida BOM (Excel uchun)", "en": "BOM at the start of CSV (for Excel)"},
    "settings.field.csv_delimiter": {"uz": "CSV ajratuvchisi", "en": "CSV delimiter"},
    "settings.field.array_join_sep": {
        "uz": "Massiv elementlarini birlashtiruvchi belgi",
        "en": "Array join separator",
    },
    "settings.field.xlsx_max_rows": {
        "uz": "Bitta Excel varag'idagi eng ko'p qatorlar soni",
        "en": "Maximum rows per Excel sheet",
    },
    # ---- env ----
    "env.report.title": {"uz": "Dastur muhiti", "en": "Program environment"},
    "env.report.version": {"uz": "Versiya: {version}", "en": "Version: {version}"},
    "env.report.python": {
        "uz": "Python: {version} ({implementation}, {bits}-bit)",
        "en": "Python: {version} ({implementation}, {bits}-bit)",
    },
    "env.report.platform": {"uz": "Tizim: {platform}", "en": "System: {platform}"},
    "env.report.core": {"uz": "Yadro: {backend}", "en": "Core: {backend}"},
    "env.report.ijson": {"uz": "ijson backend: {backend}", "en": "ijson backend: {backend}"},
    "env.report.package": {"uz": "{name}: {version}", "en": "{name}: {version}"},
    "env.report.warning": {"uz": "Ogohlantirish: {text}", "en": "Warning: {text}"},
    "env.report.missing": {"uz": "o'rnatilmagan", "en": "not installed"},
    "env.backend.rust": {"uz": "Rust (tezkor)", "en": "Rust (fast)"},
    "env.backend.python": {"uz": "sof Python (sekin)", "en": "pure Python (slow)"},
    "env.backend.none": {"uz": "yuklanmadi", "en": "not loaded"},
    "env.warn.python_version": {
        "uz": "Python {version} eskirgan: {required} yoki undan yangi versiya kerak.",
        "en": "Python {version} is too old: version {required} or newer is required.",
    },
    "env.warn.32bit": {
        "uz": "32-bitli Python ishlatilmoqda: katta fayllar uchun 64-bitli Python o'rnating.",
        "en": "A 32-bit Python is in use: install a 64-bit Python for large files.",
    },
    "env.warn.big_endian": {
        "uz": "Bu protsessor arxitekturasi (big-endian) qo'llab-quvvatlanmaydi.",
        "en": "This processor architecture (big-endian) is not supported.",
    },
    "env.warn.core_unavailable": {
        "uz": "Yadro modulini yuklab bo'lmadi: {error}",
        "en": "The core module could not be loaded: {error}",
    },
    "env.warn.rust_unavailable": {
        "uz": "Tezkor Rust yadrosi yuklanmadi ({error}); sekinroq sof Python yadrosi ishlatilmoqda.",
        "en": "The fast Rust core could not be loaded ({error}); the slower pure-Python core is used.",
    },
    "env.warn.ijson_backend": {
        "uz": "ijson {backend} backend'ida ishlayapti, {expected} topilmadi: katta tugunlarni o'qish sekinroq bo'ladi.",
        "en": "ijson uses the {backend} backend because {expected} is not available: reading large nodes will be slower.",
    },
    "env.warn.missing_package": {
        "uz": "{package} kutubxonasi o'rnatilmagan.",
        "en": "The {package} package is not installed.",
    },
    # ---- core ----
    # ---- reader ----
    "reader.empty_file": {
        "uz": "Fayl bo'sh (yoki faqat bo'sh joylardan iborat): {path}",
        "en": "The file is empty (or contains only whitespace): {path}",
    },
    "reader.warn_brace_list": {
        "uz": "Fayl standart JSON emas: obyektlar {example} ko'rinishida yozilgan. Ular ketma-ket yozuvlar sifatida o'qiladi.",
        "en": "The file is not standard JSON: the objects are written as {example}. They are read as consecutive records.",
    },
    "reader.record_path_auto": {
        "uz": "Yozuvlar yo'li avtomatik tanlandi: {path}. Kerak bo'lsa, boshqa yo'lni tanlang.",
        "en": "The record path was chosen automatically: {path}. Choose another path if needed.",
    },
    "reader.kind.array": {"uz": "JSON massiv", "en": "JSON array"},
    "reader.kind.jsonl": {"uz": "JSON Lines", "en": "JSON Lines"},
    "reader.kind.brace_list": {"uz": "Nostandart obyektlar ro'yxati", "en": "Non-standard object list"},
    "reader.kind.wrapped": {"uz": "Obyekt ichidagi massiv", "en": "Array inside an object"},
    "reader.kind.keyed": {"uz": "Kalitli obyekt", "en": "Keyed object"},
    "reader.kind.single": {"uz": "Bitta qiymat", "en": "Single value"},
    "reader.kind.stream": {"uz": "Ketma-ket JSON qiymatlar", "en": "Concatenated JSON values"},
    "reader.bad_record_path": {"uz": "Yozuvlar yo'li noto'g'ri: {path}", "en": "Invalid record path: {path}"},
    "reader.record_path_conflict": {
        "uz": "Yozuvlar yo'li ({path}) {format} formati bilan birga ishlatilmaydi.",
        "en": "The record path {path} cannot be combined with the {format} format.",
    },
    "reader.root_not_container": {
        "uz": "Faylning ildizi massiv yoki obyekt emas, shuning uchun yozuvlar yo'li '$' bo'la olmaydi: {path}",
        "en": "The root of the file is not an array or object, so the record path cannot be '$': {path}",
    },
    "reader.format_mismatch": {
        "uz": "Fayl {format} formatiga mos kelmaydi: {path}",
        "en": "The file does not match the {format} format: {path}",
    },
    "reader.no_record_path": {
        "uz": "Faylda yozuvlar massivi topilmadi: {path}",
        "en": "No record array was found in the file: {path}",
    },
    "reader.no_record_path.hint": {
        "uz": "Yozuvlar yo'lini qo'lda ko'rsating (masalan, data yoki result.items).",
        "en": "Specify the record path manually (for example data or result.items).",
    },
    "reader.bad_format": {
        "uz": "Noma'lum format: {format}. Mumkin bo'lganlari: auto, {kinds}",
        "en": "Unknown format: {format}. Allowed: auto, {kinds}",
    },
    "reader.progress.index": {"uz": "Indekslanmoqda: {name}", "en": "Indexing {name}"},
    "reader.progress.children": {"uz": "Tugun bolalari indekslanmoqda…", "en": "Indexing the node's children…"},
    "reader.broken_line": {
        "uz": "{line}-qator, {column}-ustun: {reason}",
        "en": "line {line}, column {column}: {reason}",
    },
    "reader.path_not_found": {"uz": "Bunday yo'l topilmadi: {path}", "en": "Path not found: {path}"},
    "reader.error.json_syntax_decompressed": {
        "uz": "JSON xatosi: {reason} ({line}-qator, {column}-ustun, arxivdan ochilgan ma'lumotning {offset}-bayti).",
        "en": "JSON error: {reason} (line {line}, column {column}, byte {offset} of the decompressed data).",
    },
    "reader.error.json_syntax_decompressed.hint": {
        "uz": "Xatogacha bo'lgan yozuvlarni ochish mumkin yoki faylni ta'mirlang (repair).",
        "en": "You can open the records before the error, or repair the file.",
    },
    "reader.progress.wait_index": {
        "uz": "Boshqa jarayon indekslamoqda, kutilmoqda: {name}",
        "en": "Waiting for another process that is indexing {name}",
    },
    "reader.warn.appended": {
        "uz": "Fayl ochilgandan keyin unga yana {bytes} ma'lumot yozildi (fayl hali yozilmoqda). Qo'shilgan qism "
              "natijaga kirmadi: fayl ochilgan paytdagi holatida ishlandi.",
        "en": "The file grew by {bytes} after it was opened (it is still being written). The added data is not "
              "included: the file was processed as it was when it was opened.",
    },
    # ---- index ----
    "index.error.write": {
        "uz": "Indeks faylini yozib bo'lmadi: {path} ({detail})",
        "en": "Could not write the index file: {path} ({detail})",
    },
    "index.error.write.hint": {
        "uz": "Kesh papkasida bo'sh joy va yozish huquqi borligini tekshiring (Sozlamalar: kesh papkasi).",
        "en": "Check that the cache folder has free space and write permission (Settings: cache folder).",
    },
    # ---- stream ----
    "stream.error.parse": {
        "uz": "Katta yozuvni oqim bilan o'qishda xato: {detail}",
        "en": "Error while streaming a large record: {detail}",
    },
    "stream.error.capture_limit": {
        "uz": "Yozuv bu ifodani xotirada hisoblash uchun juda katta: kerakli qiymatlar {limit} dan oshadi.",
        "en": "The record is too large to evaluate this expression in memory: the values it needs exceed {limit}.",
    },
    "stream.error.capture_limit.hint": {
        "uz": "Sozlamalarda xotira chegarasini oshiring yoki ifodada butun massiv o'rniga aniqroq yo'l ishlating.",
        "en": "Raise the memory limit in the settings, or use a more specific path instead of a whole array.",
    },
    # ---- recordset ----
    "recordset.desc.base": {"uz": "Barcha yozuvlar", "en": "All records"},
    "recordset.desc.filter": {"uz": "Filtr: {expr}", "en": "Filter: {expr}"},
    "recordset.desc.sort": {"uz": "Tartib: {keys}", "en": "Sorted by {keys}"},
    "recordset.progress.filter": {"uz": "Filtrlanmoqda: {name}", "en": "Filtering {name}"},
    "recordset.progress.sort_keys": {
        "uz": "Tartiblash uchun qiymatlar o'qilmoqda: {name}",
        "en": "Reading sort values from {name}",
    },
    "recordset.progress.sort_merge": {
        "uz": "Tartiblangan qismlar birlashtirilmoqda: {name}",
        "en": "Merging sorted parts of {name}",
    },
    "recordset.warn.skipped_huge": {
        "uz": "{count} ta juda katta yozuv ({size} dan katta) tekshirilmadi va natijaga kirmadi: "
              "JMESPath yoki butun yozuv ($) bilan ishlaydigan filtr bunday yozuvni to'liq xotiraga yuklashni talab qiladi.",
        "en": "{count} very large records (over {size}) were not checked and are not in the result: "
              "a JMESPath filter or a filter on the whole record ($) would have to load such a record into memory.",
    },
    "recordset.warn.skipped_huge.one": {
        "uz": "{count} ta juda katta yozuv ({size} dan katta) tekshirilmadi va natijaga kirmadi: "
              "JMESPath yoki butun yozuv ($) bilan ishlaydigan filtr bunday yozuvni to'liq xotiraga yuklashni talab qiladi.",
        "en": "{count} very large record (over {size}) was not checked and is not in the result: "
              "a JMESPath filter or a filter on the whole record ($) would have to load such a record into memory.",
    },
    "recordset.warn.unparseable": {
        "uz": "{count} ta yozuvni o'qib bo'lmadi va ular o'tkazib yuborildi (tafsilotlar log faylida).",
        "en": "{count} records could not be read and were skipped (details are in the log file).",
    },
    "recordset.warn.unparseable.one": {
        "uz": "{count} ta yozuvni o'qib bo'lmadi va u o'tkazib yuborildi (tafsilotlar log faylida).",
        "en": "{count} record could not be read and was skipped (details are in the log file).",
    },
    "recordset.warn.approximate": {
        "uz": "{count} ta juda katta yozuv ({size} dan katta; birinchisi #{record}) tartiblash qiymatining faqat boshi "
              "asosida tartiblandi: qiymat xotiraga sig'maydigan darajada katta.",
        "en": "{count} very large records (over {size}; the first is #{record}) were sorted using only the "
              "beginning of their sort value: the value is too large to hold in memory.",
    },
    "recordset.warn.approximate.one": {
        "uz": "{count} ta juda katta yozuv ({size} dan katta; #{record}) tartiblash qiymatining faqat boshi "
              "asosida tartiblandi: qiymat xotiraga sig'maydigan darajada katta.",
        "en": "{count} very large record (over {size}; #{record}) was sorted using only the beginning of its "
              "sort value: the value is too large to hold in memory.",
    },
    "recordset.warn.too_large": {
        "uz": "{count} ta yozuv (birinchisi #{record}) bu ifodani xotirada hisoblash uchun juda katta va natijaga "
              "kirmadi: kerakli qiymatlar {size} dan oshadi.",
        "en": "{count} records (the first is #{record}) are too large to evaluate this expression in memory and "
              "are not in the result: the values they need exceed {size}.",
    },
    "recordset.warn.too_large.one": {
        "uz": "{count} ta yozuv (#{record}) bu ifodani xotirada hisoblash uchun juda katta va natijaga "
              "kirmadi: kerakli qiymatlar {size} dan oshadi.",
        "en": "{count} record (#{record}) is too large to evaluate this expression in memory and "
              "is not in the result: the values it needs exceed {size}.",
    },
    "recordset.warn.eval_errors": {
        "uz": "{count} ta yozuvda filtrni hisoblab bo'lmadi, ular natijaga kirmadi. Birinchisi #{record}: {error}",
        "en": "The filter could not be evaluated on {count} records; they are not in the result. "
              "The first is #{record}: {error}",
    },
    "recordset.warn.eval_errors.one": {
        "uz": "{count} ta yozuvda (#{record}) filtrni hisoblab bo'lmadi, u natijaga kirmadi: {error}",
        "en": "The filter could not be evaluated on {count} record (#{record}); it is not in the result: {error}",
    },
    "recordset.warn.sort_key_absent": {
        "uz": "Tartiblash maydoni {field} birorta ham yozuvda topilmadi ({total} ta yozuv), shuning uchun tartibga "
              "ta'sir qilmadi. Maydon nomi to'g'ri yozilganini tekshiring.",
        "en": "The sort field {field} was not found in any of the {total} records, so it had no effect on the "
              "order. Check the spelling of the field name.",
    },
    "recordset.warn.sort_key_absent.one": {
        "uz": "Tartiblash maydoni {field} birorta ham yozuvda topilmadi ({total} ta yozuv), shuning uchun tartibga "
              "ta'sir qilmadi. Maydon nomi to'g'ri yozilganini tekshiring.",
        "en": "The sort field {field} was not found in the {total} record, so it had no effect on the "
              "order. Check the spelling of the field name.",
    },
    "recordset.warn.sort_key_rare": {
        "uz": "Tartiblash maydoni {field} {total} ta yozuvdan faqat {found} tasida bor.",
        "en": "The sort field {field} was found in only {found} of {total} records.",
    },
    "recordset.error.write": {
        "uz": "Yozuvlar to'plamining vaqtinchalik faylini yozib bo'lmadi: {path} ({detail})",
        "en": "Could not write a temporary record set file: {path} ({detail})",
    },
    "recordset.error.write.hint": {
        "uz": "Ma'lumotlar papkasida bo'sh joy va yozish huquqi borligini tekshiring.",
        "en": "Check that the application data folder has free space and write permission.",
    },
    "recordset.error.memory_limit": {
        "uz": "Tartiblash uchun xotira chegarasi musbat butun son (baytlarda) bo'lishi kerak, berilgan: {value}",
        "en": "The sort memory limit must be a positive whole number of bytes, got: {value}",
    },
    "recordset.error.memory_limit.hint": {
        "uz": "Misollar: 256MB, 1GB.",
        "en": "Examples: 256MB, 1GB.",
    },
    "recordset.error.eval_failed": {
        "uz": "Filtrni birorta ham yozuvda hisoblab bo'lmadi (tekshirilgan yozuvlar: {count}), shuning uchun u "
              "to'xtatildi. Birinchisi #{record}: {error}",
        "en": "The filter could not be evaluated on any record (records tried: {count}), so it was stopped. "
              "The first is #{record}: {error}",
    },
    "recordset.error.eval_failed.hint": {
        "uz": "Ifodadagi maydon nomlari va ular kutgan qiymat turlarini tekshiring. Asl xabar: {detail}",
        "en": "Check the field names in the expression and the value types it expects. Original message: {detail}",
    },
    # ---- sortkeys ----
    "sortkeys.err.path_type": {
        "uz": "Tartiblash maydoni matn ko'rinishida yozilishi kerak, berilgan: {value}",
        "en": "The sort field must be given as text, got: {value}",
    },
    "sortkeys.err.empty_field": {
        "uz": "Tartiblash maydoni ko'rsatilmagan.",
        "en": "No sort field was given.",
    },
    "sortkeys.err.empty_field.hint": {
        "uz": "Misollar: name, -id (kamayish tartibida), contact.address.city, $ (yozuvning o'zi)",
        "en": "Examples: name, -id (descending), contact.address.city, $ (the record itself)",
    },
    "sortkeys.err.descendants": {
        "uz": "Tartiblash maydonida '**' yoki '..' (barcha ichki darajalar) ishlatib bo'lmaydi: {path}",
        "en": "A sort field cannot use '**' or '..' (all nested levels): {path}",
    },
    "sortkeys.err.bad_nulls": {
        "uz": "null va yo'q qiymatlar o'rni 'first' (boshida) yoki 'last' (oxirida) bo'lishi kerak, berilgan: {value}",
        "en": "The place of null and missing values must be 'first' or 'last', got: {value}",
    },
    "sortkeys.err.bad_collation": {
        "uz": "Satrlarni solishtirish qoidasi 'uz', 'simple' yoki 'unicode' bo'lishi kerak, berilgan: {value}",
        "en": "The string collation must be 'uz', 'simple' or 'unicode', got: {value}",
    },
    "sortkeys.err.bad_collation.hint": {
        "uz": "uz - o'zbek alifbosi tartibi (oʻ, gʻ, sh, ch, ng alohida harf); simple - harfma-harf; "
              "unicode - belgi kodlari bo'yicha.",
        "en": "uz - Uzbek alphabet order (oʻ, gʻ, sh, ch, ng are letters); simple - letter by letter; "
              "unicode - by character code.",
    },
    "sortkeys.err.bad_flag": {
        "uz": "'{name}' parametri true yoki false bo'lishi kerak, berilgan: {value}",
        "en": "The '{name}' option must be true or false, got: {value}",
    },
    "sortkeys.dir.asc": {"uz": "o'sish tartibida", "en": "ascending"},
    "sortkeys.dir.desc": {"uz": "kamayish tartibida", "en": "descending"},
    "sortkeys.describe": {"uz": "{path} ({direction})", "en": "{path} ({direction})"},
    # ---- query ----
    "query.token.end": {"uz": "ifoda oxiri", "en": "the end of the expression"},
    "query.syntax.empty": {"uz": "ifoda bo'sh", "en": "the expression is empty"},
    "query.syntax.unexpected_char": {"uz": "kutilmagan belgi {char}", "en": "unexpected character {char}"},
    "query.syntax.curly_quote": {
        "uz": "kutilmagan belgi {char}: matn uchun oddiy qo'shtirnoq ishlating: \"...\" yoki '...'",
        "en": "unexpected character {char}: use straight quotes for text: \"...\" or '...'",
    },
    "query.syntax.unterminated_string": {
        "uz": "matn yopilmagan: oxiridagi {quote} yetishmayapti",
        "en": "unterminated string: the closing {quote} is missing",
    },
    "query.syntax.unterminated_name": {
        "uz": "` bilan boshlangan nom yopilmagan: oxiridagi ` yetishmayapti",
        "en": "unterminated `name`: the closing ` is missing",
    },
    "query.syntax.bad_escape": {
        "uz": "noto'g'ri escape ketma-ketligi: \\{char}",
        "en": "invalid escape sequence: \\{char}",
    },
    "query.syntax.bad_unicode_escape": {
        "uz": "noto'g'ri \\u escape: undan keyin 4 ta hex raqam kelishi kerak",
        "en": "invalid \\u escape: it must be followed by 4 hex digits",
    },
    "query.syntax.invalid_number": {"uz": "noto'g'ri son: {token}", "en": "invalid number: {token}"},
    "query.syntax.special_no_path": {
        "uz": "{name} dan keyin yo'l davom ettirilmaydi",
        "en": "{name} cannot be followed by a path",
    },
    "query.syntax.unknown_special": {
        "uz": "noma'lum maxsus nom {name}: $, $key yoki $index ishlating",
        "en": "unknown special name {name}: use $, $key or $index",
    },
    "query.syntax.expected_key": {
        "uz": "'{sep}' dan keyin kalit nomi kutilgan",
        "en": "a key name was expected after '{sep}'",
    },
    "query.syntax.unterminated_bracket": {"uz": "'[' yopilmagan", "en": "unterminated '['"},
    "query.syntax.bad_bracket": {
        "uz": "'[...]' ichida indeks, '*' yoki qo'shtirnoqli kalit kutilgan",
        "en": "an index, '*' or a quoted key was expected inside '[...]'",
    },
    "query.syntax.negative_index": {
        "uz": "manfiy indeks qo'llab-quvvatlanmaydi",
        "en": "negative indices are not supported",
    },
    "query.syntax.expected_bracket_close": {"uz": "']' kutilgan", "en": "']' was expected"},
    "query.syntax.too_deep": {
        "uz": "ifoda juda chuqur ichma-ich ({limit} darajadan ko'p)",
        "en": "the expression is nested too deeply (more than {limit} levels)",
    },
    "query.syntax.chained_comparison": {
        "uz": "taqqoslashlarni zanjir qilib yozib bo'lmaydi; ularni 'and' bilan ajrating (masalan: a > 1 and a < 5)",
        "en": "comparisons cannot be chained; join them with 'and' (e.g. a > 1 and a < 5)",
    },
    "query.syntax.missing_operator": {
        "uz": "kutilmagan {token}: shartlar orasida 'and' yoki 'or' tushib qolmaganmi?",
        "en": "unexpected {token}: is an 'and' or 'or' missing between the conditions?",
    },
    "query.syntax.unexpected_token": {"uz": "kutilmagan {token}", "en": "unexpected {token}"},
    "query.syntax.expected_between_and": {
        "uz": "'between' dan keyin '... and ...' kutilgan (masalan: x between 1 and 5), lekin {token} keldi",
        "en": "'between' needs '... and ...' (e.g. x between 1 and 5), found {token}",
    },
    "query.syntax.expected_in": {
        "uz": "'not' dan keyin 'in' kutilgan (masalan: x not in [1, 2]), lekin {token} keldi",
        "en": "'in' was expected after 'not' (e.g. x not in [1, 2]), found {token}",
    },
    "query.syntax.expected_is_target": {
        "uz": "'is' dan keyin null, empty yoki missing kutilgan, lekin {token} keldi",
        "en": "null, empty or missing was expected after 'is', found {token}",
    },
    "query.syntax.expected_close_paren": {
        "uz": "')' kutilgan, lekin {token} keldi",
        "en": "')' was expected, found {token}",
    },
    "query.syntax.expected_operand_end": {
        "uz": "ifoda tugallanmagan: qiymat yoki maydon yo'li kutilgan",
        "en": "incomplete expression: a value or a field path was expected",
    },
    "query.syntax.expected_operand": {
        "uz": "qiymat, maydon yo'li yoki '(' kutilgan, lekin {token} keldi",
        "en": "a value, a field path or '(' was expected, found {token}",
    },
    "query.syntax.unknown_function": {
        "uz": "noma'lum funksiya {name}(); mavjud funksiyalar: {names}",
        "en": "unknown function {name}(); available functions: {names}",
    },
    "query.syntax.expected_args_end": {
        "uz": "',' yoki ')' kutilgan, lekin {token} keldi",
        "en": "',' or ')' was expected, found {token}",
    },
    "query.syntax.arg_count": {
        "uz": "{name}() funksiyasi {expected} ta argument oladi, lekin {got} ta berildi",
        "en": "{name}() takes {expected} argument(s); arguments given: {got}",
    },
    "query.syntax.expected_list_end": {
        "uz": "',' yoki ']' kutilgan, lekin {token} keldi",
        "en": "',' or ']' was expected, found {token}",
    },
    "query.syntax.expected_object_key": {
        "uz": "obyekt kaliti qo'shtirnoqli matn bo'lishi kerak, lekin {token} keldi",
        "en": "an object key must be a quoted string, found {token}",
    },
    "query.syntax.expected_colon": {"uz": "':' kutilgan, lekin {token} keldi", "en": "':' was expected, found {token}"},
    "query.syntax.object_constant": {
        "uz": "obyekt ichidagi qiymat o'zgarmas bo'lishi kerak (matn, son, true, false, null, ro'yxat yoki obyekt)",
        "en": "object member values must be constants (string, number, true, false, null, list or object)",
    },
    "query.syntax.expected_object_end": {
        "uz": "',' yoki '}}' kutilgan, lekin {token} keldi",
        "en": "',' or '}}' was expected, found {token}",
    },
    "query.syntax.regex_not_string": {
        "uz": "regex andozasi qo'shtirnoqli matn bo'lishi kerak",
        "en": "the regex pattern must be a quoted string",
    },
    "query.syntax.bad_regex": {"uz": "noto'g'ri regex: {error}", "en": "invalid regular expression: {error}"},
    "query.warn.bare_word": {
        "uz": "'{word}' maydon nomi deb tushunildi; matn nazarda tutilgan bo'lsa, qo'shtirnoqqa oling: \"{word}\"",
        "en": "'{word}' is read as a field name; if you meant text, put it in quotes: \"{word}\"",
    },
    "query.jmespath.syntax": {"uz": "JMESPath xatosi: {error}", "en": "JMESPath error: {error}"},
    "query.jmespath.incomplete": {
        "uz": "JMESPath ifodasi tugallanmagan",
        "en": "the JMESPath expression is incomplete",
    },
    "query.jmespath.unclosed_name": {
        "uz": "qo'shtirnoqli maydon nomi yopilmagan: oxiridagi {quote} yetishmayapti (JMESPath'da matn '...' "
              "ichida yoziladi)",
        "en": "unterminated quoted field name: the closing {quote} is missing (in JMESPath, text is written "
              "in '...')",
    },
    "query.jmespath.unclosed_literal": {
        "uz": "` bilan boshlangan qiymat yopilmagan: oxiridagi {quote} yetishmayapti",
        "en": "unterminated `value`: the closing {quote} is missing",
    },
    "query.jmespath.bad_literal": {
        "uz": "` ` orasidagi qiymat to'g'ri JSON emas: {token}",
        "en": "the value between backticks is not valid JSON: {token}",
    },
    "query.jmespath.quoted_function": {
        "uz": "funksiya nomi qo'shtirnoqsiz yoziladi (masalan: length(name))",
        "en": "a function name is written without quotes (for example: length(name))",
    },
    "query.jmespath.bad_name": {
        "uz": "qo'shtirnoqli maydon nomi noto'g'ri: {name} (undagi \\ bilan yozilgan belgilarni tekshiring)",
        "en": "invalid quoted field name: {name} (check the characters written with \\ in it)",
    },
    "query.jmespath.unknown_function": {
        "uz": "noma'lum JMESPath funksiyasi {name}(); mavjudlari: {names}",
        "en": "unknown JMESPath function {name}(); available: {names}",
    },
    "query.jmespath.arg_count": {
        "uz": "JMESPath funksiyasi {name}() {expected} ta argument oladi, lekin {got} ta berildi",
        "en": "JMESPath function {name}() takes {expected} argument(s); arguments given: {got}",
    },
    "query.jmespath.runtime": {
        "uz": "JMESPath ifodasini hisoblab bo'lmadi: {reason}",
        "en": "The JMESPath expression could not be evaluated: {reason}",
    },
    "query.jmespath.runtime.hint": {
        "uz": "JMESPath xabari: {detail}",
        "en": "JMESPath message: {detail}",
    },
    "query.jmespath.err.type": {
        "uz": "{function}() funksiyasiga {received} turidagi qiymat berildi (kutilgan tur: {expected})",
        "en": "function {function}() got a value of type {received} (expected: {expected})",
    },
    "query.jmespath.err.compare": {
        "uz": "{left} va {right} turidagi qiymatlarni '{op}' bilan solishtirib bo'lmaydi",
        "en": "a value of type {left} cannot be compared with a value of type {right} using '{op}'",
    },
    "query.jmespath.err.number": {
        "uz": "sonni hisoblab bo'lmadi (masalan, son juda katta)",
        "en": "a number could not be computed (for example, it is too large)",
    },
    "query.jmespath.err.too_deep": {
        "uz": "qiymat juda chuqur ichma-ich joylashgan",
        "en": "the value is nested too deeply",
    },
    "query.jmespath.err.other": {"uz": "kutilmagan xato", "en": "unexpected error"},
    "query.preset.not_found": {
        "uz": "\"{name}\" nomli saqlangan filtr topilmadi.",
        "en": "There is no saved filter named \"{name}\".",
    },
    "query.preset.not_found.hint": {
        "uz": "Saqlangan filtrlar ro'yxatini ko'ring yoki filtrni avval shu nom bilan saqlang.",
        "en": "Look at the list of saved filters, or save the filter under this name first.",
    },
    "query.preset.bad_name": {
        "uz": "Filtr nomi noto'g'ri: {name}. Nom bo'sh bo'lmasligi, {limit} belgidan oshmasligi va boshqaruv "
              "belgilarisiz bo'lishi kerak.",
        "en": "Invalid filter name: {name}. A name must not be empty, must have at most {limit} characters and "
              "no control characters.",
    },
    "query.preset.bad_lang": {
        "uz": "Filtr tili noto'g'ri: {lang}. Mumkin bo'lganlari: {langs}.",
        "en": "Invalid filter language: {lang}. Possible values: {langs}.",
    },
    "query.preset.empty": {
        "uz": "\"{name}\" filtri uchun ifoda bo'sh.",
        "en": "The expression of the filter \"{name}\" is empty.",
    },
    "query.preset.busy": {
        "uz": "Saqlangan filtrlar faylini boshqa jarayon uzoq vaqtdan beri o'zgartirmoqda: {path}",
        "en": "Another process has been changing the saved filters file for too long: {path}",
    },
    "query.preset.busy.hint": {
        "uz": "Bir ozdan keyin qayta urinib ko'ring; muammo takrorlansa, jsonreshape ishlatayotgan boshqa "
              "oynalarni yoping.",
        "en": "Try again in a moment; if it happens again, close the other windows that run jsonreshape.",
    },
    "query.builder.error": {"uz": "Filtr shartida xato: {detail}", "en": "Error in a filter condition: {detail}"},
    "query.builder.unknown_op": {"uz": "noma'lum filtr operatori: {op}", "en": "unknown filter operator: {op}"},
    "query.builder.bad_group_op": {
        "uz": "guruh turi 'and' (VA) yoki 'or' (YOKI) bo'lishi kerak, lekin {op} berildi",
        "en": "a group must be 'and' or 'or', got {op}",
    },
    "query.builder.bad_item": {
        "uz": "guruh elementi shart yoki guruh bo'lishi kerak, lekin {type} berildi",
        "en": "a group item must be a condition or a group, got {type}",
    },
    "query.builder.bad_field": {
        "uz": "maydon yo'li matn bo'lishi kerak, lekin {type} berildi",
        "en": "the field must be a path string, got {type}",
    },
    "query.builder.bad_value": {
        "uz": "bu qiymatni filtrda ishlatib bo'lmaydi ({type}); JSON qiymati kerak",
        "en": "this value cannot be used in a filter ({type}); a JSON value is required",
    },
    "query.builder.non_finite": {
        "uz": "son chekli bo'lishi kerak (NaN yoki cheksizlikni ishlatib bo'lmaydi)",
        "en": "numbers must be finite (NaN or infinity cannot be used)",
    },
    "query.op.eq": {"uz": "=", "en": "="},
    "query.op.ne": {"uz": "≠", "en": "≠"},
    "query.op.contains": {"uz": "ichida bor", "en": "contains"},
    "query.op.startswith": {"uz": "bilan boshlanadi", "en": "starts with"},
    "query.op.regex": {"uz": "regex", "en": "matches regex"},
    "query.op.gt": {"uz": ">", "en": ">"},
    "query.op.lt": {"uz": "<", "en": "<"},
    "query.op.ge": {"uz": "≥", "en": "≥"},
    "query.op.le": {"uz": "≤", "en": "≤"},
    "query.op.between": {"uz": "oraliq", "en": "between"},
    "query.op.exists": {"uz": "mavjud", "en": "exists"},
    "query.op.not_exists": {"uz": "mavjud emas", "en": "does not exist"},
    "query.op.is_null": {"uz": "null", "en": "is null"},
    "query.op.is_empty": {"uz": "bo'sh", "en": "is empty"},
    "query.op.in": {"uz": "ro'yxatda bor", "en": "is in list"},
    "query.op.len_eq": {"uz": "uzunlik =", "en": "length ="},
    "query.op.len_ne": {"uz": "uzunlik ≠", "en": "length ≠"},
    "query.op.len_gt": {"uz": "uzunlik >", "en": "length >"},
    "query.op.len_lt": {"uz": "uzunlik <", "en": "length <"},
    "query.op.len_ge": {"uz": "uzunlik ≥", "en": "length ≥"},
    "query.op.len_le": {"uz": "uzunlik ≤", "en": "length ≤"},
    # ---- textmatch ----
    "textmatch.empty_query": {"uz": "Qidiruv matni bo'sh.", "en": "The search text is empty."},
    "textmatch.empty_query.hint": {
        "uz": "Qidiriladigan so'z, regex yoki son shartini kiriting.",
        "en": "Enter a word, a regular expression or a number condition to search for.",
    },
    "textmatch.regex_error": {
        "uz": "Regex ifodasida xato ({pos}-belgi): {detail}",
        "en": "Error in the regular expression at character {pos}: {detail}",
    },
    "textmatch.regex_error.hint": {
        "uz": "( ) [ ] . * + ? | ^ $ \\ belgilarini oddiy belgi sifatida qidirish uchun oldiga \\ qo'ying "
        "yoki oddiy matn rejimini tanlang.",
        "en": "To search for the characters ( ) [ ] . * + ? | ^ $ \\ literally, put a \\ before them "
        "or use plain-text mode.",
    },
    "textmatch.regex.unterminated_group": {
        "uz": "'(' yopilmagan: ')' yetishmaydi",
        "en": "missing ')': a group is not closed",
    },
    "textmatch.regex.unbalanced_paren": {
        "uz": "ortiqcha ')': unga mos '(' yo'q",
        "en": "unbalanced ')': there is no matching '('",
    },
    "textmatch.regex.unterminated_set": {
        "uz": "'[' yopilmagan: ']' yetishmaydi",
        "en": "missing ']': a character set is not closed",
    },
    "textmatch.regex.nothing_to_repeat": {
        "uz": "takrorlash belgisidan (*, + yoki ?) oldin hech narsa yo'q",
        "en": "nothing to repeat before *, + or ?",
    },
    "textmatch.regex.multiple_repeat": {
        "uz": "takrorlash belgilari ketma-ket yozilgan",
        "en": "repetition operators follow each other",
    },
    "textmatch.regex.bad_repeat_range": {
        "uz": "takrorlash oralig'ida eng kichik son eng kattasidan katta",
        "en": "the minimum repeat count is greater than the maximum",
    },
    "textmatch.regex.repeat_too_large": {"uz": "takrorlash soni juda katta", "en": "the repeat count is too large"},
    "textmatch.regex.bad_range": {
        "uz": "belgilar oralig'i noto'g'ri (masalan, z-a)",
        "en": "invalid character range (for example z-a)",
    },
    "textmatch.regex.bad_escape": {
        "uz": "\\ dan keyin noto'g'ri belgi",
        "en": "invalid escape: unexpected character after \\",
    },
    "textmatch.regex.lookbehind": {
        "uz": "look-behind ichidagi ifoda aniq uzunlikda bo'lishi kerak",
        "en": "a look-behind must have a fixed width",
    },
    "textmatch.regex.unknown_group": {
        "uz": "bunday nomli yoki raqamli guruh yo'q",
        "en": "there is no group with this name or number",
    },
    "textmatch.regex.unexpected_end": {"uz": "ifoda kutilmaganda tugadi", "en": "unexpected end of the expression"},
    "textmatch.regex.global_flags": {
        "uz": "(?i) kabi bayroqlar faqat ifoda boshida yozilishi mumkin",
        "en": "flags such as (?i) are only allowed at the start of the expression",
    },
    "textmatch.regex.bad_construct": {"uz": "noto'g'ri '(?...)' tuzilmasi", "en": "invalid '(?...)' construct"},
    "textmatch.regex.bad_group_name": {
        "uz": "guruh nomi noto'g'ri yoki yopilmagan: (?P<nom>...) ko'rinishida yozing",
        "en": "invalid or unterminated group name: write (?P<name>...)",
    },
    "textmatch.regex.duplicate_group": {
        "uz": "bir xil nomli ikkita guruh bor",
        "en": "two groups have the same name",
    },
    "textmatch.regex.open_group": {
        "uz": "guruhga uning ichidan murojaat qilib bo'lmaydi",
        "en": "a group cannot be referred to from inside itself",
    },
    "textmatch.regex.too_many_groups": {"uz": "guruhlar juda ko'p", "en": "too many groups"},
    "textmatch.regex.too_complex": {
        "uz": "ifoda juda murakkab (ichma-ich qavslar juda ko'p)",
        "en": "the expression is too complex (too many nested parentheses)",
    },
    "textmatch.regex.other": {"uz": "{message}", "en": "{message}"},
    "textmatch.number_error": {
        "uz": "Son shartida xato ({pos}-belgi): {detail}",
        "en": "Error in the number condition at character {pos}: {detail}",
    },
    "textmatch.number_error.hint": {
        "uz": "Misollar: > 1000, >= 5, < -2.5, = 3, != 0, 1000..2000 yoki shunchaki 42.",
        "en": "Examples: > 1000, >= 5, < -2.5, = 3, != 0, 1000..2000 or just 42.",
    },
    "textmatch.number.expected_number": {"uz": "son kutilgan", "en": "a number was expected"},
    "textmatch.number.trailing": {"uz": "ortiqcha belgilar: {extra}", "en": "unexpected characters: {extra}"},
    # ---- search ----
    "search.progress.search": {"uz": "Qidirilmoqda: {name}", "en": "Searching: {name}"},
    "search.progress.wait_index": {
        "uz": "Qidiruv indeks tugashini kutmoqda (keyin tezroq davom etadi): {name}",
        "en": "The search is waiting for the index to finish (it then continues faster): {name}",
    },
    "search.progress.export": {
        "uz": "Qidiruv natijalari eksport qilinmoqda: {name}",
        "en": "Exporting search results: {name}",
    },
    "search.recordset_description": {
        "uz": "Qidiruv natijasi: “{text}”",
        "en": "Search results: “{text}”",
    },
    "search.export.bad_format": {
        "uz": "Qidiruv natijalarini bu formatda saqlab bo'lmaydi: {fmt}",
        "en": "Search results cannot be saved in this format: {fmt}",
    },
    "search.export.bad_format.hint": {
        "uz": "CSV (.csv) yoki JSON (.json) formatini tanlang.",
        "en": "Choose CSV (.csv) or JSON (.json).",
    },
    "search.summary": {
        "uz": "{hits} ta moslik, {records} ta yozuvda; {size} {seconds} soniyada ko'rib chiqildi "
        "({rate} MB/s, {mode}).",
        "en": "Matches: {hits}, records: {records}; {size} searched in {seconds} s ({rate} MB/s, {mode}).",
    },
    "search.summary.truncated": {
        "uz": "Ro'yxatda faqat dastlabki {shown} ta moslik bor (jami {total} ta).",
        "en": "Only the first {shown} matches are listed (of {total}).",
    },
    "search.summary.truncated.one": {
        "uz": "Ro'yxatda faqat dastlabki {shown} ta moslik bor (jami {total} ta).",
        "en": "Only the first match is listed ({shown} of {total}).",
    },
    "search.history_busy": {
        "uz": "Qidiruv tarixi faylini boshqa jarayon uzoq vaqtdan beri o'zgartirmoqda: {path}",
        "en": "Another process has been changing the search history file for too long: {path}",
    },
    "search.mode.bytes": {"uz": "bayt rejimi", "en": "byte mode"},
    "search.mode.parse": {"uz": "har bir yozuvni o'qish rejimi", "en": "parse mode"},
    "search.where.key": {"uz": "kalit", "en": "key"},
    "search.where.value": {"uz": "qiymat", "en": "value"},
    "search.scope.both": {"uz": "kalitlarda va qiymatlarda", "en": "keys and values"},
    "search.scope.keys": {"uz": "faqat kalitlarda", "en": "keys only"},
    "search.scope.values": {"uz": "faqat qiymatlarda", "en": "values only"},
    # ---- schema ----
    "schema.progress": {"uz": "Tuzilma aniqlanmoqda: {name}", "en": "Inferring the structure: {name}"},
    "schema.bad_sample": {
        "uz": "Namuna hajmi musbat butun son bo'lishi kerak, berilgan: {value}",
        "en": "The sample size must be a positive whole number, got: {value}",
    },
    "schema.bad_sample.hint": {"uz": "Masalan: 10000.", "en": "For example: 10000."},
    "schema.summary.full": {
        "uz": "Barcha yozuvlar tahlil qilindi: {scanned} ta ({seconds} s).",
        "en": "All records were analysed: {scanned} ({seconds} s).",
    },
    "schema.summary.sample": {
        "uz": "Namuna: {total} ta yozuvdan {scanned} tasi tahlil qilindi ({seconds} s).",
        "en": "Sample: {scanned} of {total} records analysed ({seconds} s).",
    },
    "schema.summary.sample.one": {
        "uz": "Namuna: {total} ta yozuvdan {scanned} tasi tahlil qilindi ({seconds} s).",
        "en": "Sample: {scanned} of {total} record analysed ({seconds} s).",
    },
    "schema.summary.skipped": {
        "uz": "{count} ta yozuvni tahlil qilib bo'lmadi (tafsilotlar log faylida).",
        "en": "{count} records could not be analysed (details in the log file).",
    },
    "schema.summary.skipped.one": {
        "uz": "{count} ta yozuvni tahlil qilib bo'lmadi (tafsilotlar log faylida).",
        "en": "{count} record could not be analysed (details in the log file).",
    },
    "schema.summary.truncated": {
        "uz": "Tuzilma juda katta: {fields} ta maydondan keyingilari va juda chuqur darajalar hisobga olinmadi.",
        "en": "The structure is very large: fields beyond {fields} and very deep levels were not analysed.",
    },
    "schema.col.path": {"uz": "Yo'l", "en": "Path"},
    "schema.col.types": {"uz": "Turlar", "en": "Types"},
    "schema.col.presence": {"uz": "Mavjudlik", "en": "Presence"},
    "schema.col.examples": {"uz": "Misollar", "en": "Examples"},
    "schema.type.object": {"uz": "obyekt", "en": "object"},
    "schema.type.array": {"uz": "massiv", "en": "array"},
    "schema.type.string": {"uz": "satr", "en": "string"},
    "schema.type.number": {"uz": "son", "en": "number"},
    "schema.type.bool": {"uz": "mantiqiy", "en": "bool"},
    "schema.type.null": {"uz": "null", "en": "null"},
    "schema.no_records": {"uz": "Tahlil qilingan yozuvlar yo'q.", "en": "No records were analysed."},
    "schema.no_fields": {"uz": "Yozuvlarda maydonlar yo'q.", "en": "The records have no fields."},
    # ---- writer ----
    "writer.invalid_option": {
        "uz": "Eksport sozlamasi noto'g'ri: {name} = {value}. Kutilgan qiymat: {expected}.",
        "en": "Invalid export option: {name} = {value}. Expected: {expected}.",
    },
    "writer.expected.fmt": {"uz": "json, jsonl, csv, xlsx yoki sqlite", "en": "json, jsonl, csv, xlsx or sqlite"},
    "writer.expected.indent": {
        "uz": "bo'sh (ixcham), 0 dan 16 gacha son yoki tab",
        "en": "nothing (compact), a number from 0 to 16, or tab",
    },
    "writer.expected.bool": {"uz": "ha yoki yo'q (true/false)", "en": "yes or no (true/false)"},
    "writer.expected.columns": {
        "uz": "ustun yo'llari ro'yxati (kamida bitta, masalan: id, name, contact.address.city)",
        "en": "a list of column paths (at least one, for example: id, name, contact.address.city)",
    },
    "writer.expected.array_mode": {
        "uz": "join (elementlarni bitta katakka birlashtirish) yoki rows (har elementga alohida qator)",
        "en": "join (elements joined in one cell) or rows (one row per element)",
    },
    "writer.expected.explode": {
        "uz": "massiv yo'li, yulduzchasiz (masalan: orders)",
        "en": "an array path without wildcards (for example: orders)",
    },
    "writer.expected.separator": {
        "uz": "1–16 belgidan iborat matn, qator tashlashsiz",
        "en": "1 to 16 characters without line breaks",
    },
    "writer.expected.delimiter": {
        "uz": "bitta belgi (qo'shtirnoq yoki qator tashlash bo'lmasin)",
        "en": "a single character (not a double quote or a line break)",
    },
    "writer.expected.max_rows": {"uz": "{min} dan {max} gacha butun son", "en": "a whole number from {min} to {max}"},
    "writer.expected.split": {
        "uz": "sheets (yangi varaqlarga) yoki files (yangi fayllarga)",
        "en": "sheets (new sheets) or files (new files)",
    },
    "writer.expected.sample": {"uz": "1 yoki undan katta butun son", "en": "a whole number of at least 1"},
    "writer.expected.sort_keys": {
        "uz": "true/false, alpha (alifbo bo'yicha) yoki codepoint (belgi kodlari bo'yicha)",
        "en": "true/false, alpha (alphabetical) or codepoint (by character code)",
    },
    "writer.bad_column": {
        "uz": "Ustun yo'li noto'g'ri: {column} ({detail})",
        "en": "Invalid column path: {column} ({detail})",
    },
    "writer.bad_column.hint": {
        "uz": "Misollar: id, contact.address.city, orders[*].oid, tags, $key (kalitli yozuvning kaliti), "
              "$index (yozuv raqami).",
        "en": "Examples: id, contact.address.city, orders[*].oid, tags, $key (key of a keyed record), "
              "$index (record number).",
    },
    "writer.bad_explode": {
        "uz": "Qatorlarga yoyiladigan massiv yo'li noto'g'ri: {path} ({detail})",
        "en": "Invalid path of the array to expand into rows: {path} ({detail})",
    },
    "writer.bad_explode.hint": {
        "uz": "Massiv yo'lini yulduzchasiz yozing, masalan: orders yoki contact.phones",
        "en": "Write the array path without wildcards, for example: orders or contact.phones",
    },
    "writer.error.same_file": {
        "uz": "Natija manba faylning o'ziga yozilmaydi: {path}",
        "en": "The result cannot be written over the source file: {path}",
    },
    "writer.error.same_file.hint": {
        "uz": "Asl fayl hech qachon o'zgartirilmaydi: natija uchun boshqa nom tanlang.",
        "en": "The original file is never modified: choose another name for the result.",
    },
    "writer.error.no_dir": {"uz": "Natija papkasi topilmadi: {path}", "en": "The output folder does not exist: {path}"},
    "writer.error.bad_record": {
        "uz": "Yozuv raqami noto'g'ri: {index} (faylda {count} ta yozuv bor, raqamlar 0 dan boshlanadi)",
        "en": "Invalid record number: {index} (records in the file: {count}, numbered from 0)",
    },
    "writer.error.no_columns": {
        "uz": "Jadvalga chiqarish uchun birorta ham ustun topilmadi.",
        "en": "No columns were found for the table export.",
    },
    "writer.error.no_columns.hint": {
        "uz": "Ustunlarni qo'lda ko'rsating, masalan: id,name,contact.address.city",
        "en": "Specify the columns, for example: id,name,contact.address.city",
    },
    "writer.error.too_many_columns": {
        "uz": "Excel varag'ida ko'pi bilan {max} ta ustun bo'ladi, bu eksportda esa {count} ta ustun bor.",
        "en": "An Excel sheet holds at most {max} columns, but this export has {count}.",
    },
    "writer.error.too_many_columns.hint": {
        "uz": "Kerakli ustunlarni tanlang yoki CSV formatidan foydalaning.",
        "en": "Choose the columns you need or use the CSV format.",
    },
    "writer.error.xlsx_too_large": {
        "uz": "Excel fayli 4 GB chegarasidan oshib ketadi: {path}",
        "en": "The Excel file would exceed the 4 GB limit: {path}",
    },
    "writer.error.xlsx_too_large.hint": {
        "uz": "Natijani fayllarga bo'ling (--xlsx-split files) yoki varaqdagi qatorlar chegarasini "
              "(xlsx_max_rows sozlamasi) kamaytiring.",
        "en": "Split the result into files (--xlsx-split files) or lower the number of rows per sheet "
              "(the xlsx_max_rows setting).",
    },
    "writer.error.xlsx_part_locked": {
        "uz": "Qism faylni almashtirib bo'lmadi: {path}. Hech qaysi qism fayl o'zgartirilmadi.",
        "en": "Could not replace the part file {path}. No part file was changed.",
    },
    "writer.error.xlsx_part_locked.hint": {
        "uz": "Fayl boshqa dasturda (masalan, Excel'da) ochiq bo'lishi mumkin: uni yopib, qaytadan urinib ko'ring.",
        "en": "The file may be open in another program (for example Excel): close it and try again.",
    },
    "writer.error.sqlite": {
        "uz": "SQLite bazasiga yozishda xato: {detail}",
        "en": "Error while writing the SQLite database: {detail}",
    },
    "writer.warn.partial": {
        "uz": "Manba faylda JSON xatosi bor: faqat xatogacha bo'lgan yozuvlar eksport qilindi ({count} ta).",
        "en": "The source file has a JSON error: only the records before the error were exported ({count}).",
    },
    "writer.warn.broken_lines": {
        "uz": "Manbadagi {count} ta buzuq qator o'tkazib yuborildi.",
        "en": "{count} broken lines of the source were skipped.",
    },
    "writer.warn.broken_lines.one": {
        "uz": "Manbadagi {count} ta buzuq qator o'tkazib yuborildi.",
        "en": "{count} broken line of the source was skipped.",
    },
    "writer.warn.huge_skipped": {
        "uz": "{count} ta juda katta yozuv ({limit} dan katta) o'tkazib yuborildi. Ularni ham chiqarish uchun "
              "ustunlarni aniq ko'rsating.",
        "en": "{count} very large records (over {limit}) were skipped. Specify the columns explicitly to export "
              "them too.",
    },
    "writer.warn.huge_skipped.one": {
        "uz": "{count} ta juda katta yozuv ({limit} dan katta) o'tkazib yuborildi. Uni ham chiqarish uchun "
              "ustunlarni aniq ko'rsating.",
        "en": "{count} very large record (over {limit}) was skipped. Specify the columns explicitly to export "
              "it too.",
    },
    "writer.warn.huge_skipped_sqlite": {
        "uz": "{count} ta juda katta yozuv ({limit} dan katta) SQLite bazasiga yozilmadi. Ular uchun JSON yoki "
              "JSONL formatidan foydalaning.",
        "en": "{count} very large records (over {limit}) were not written to the SQLite database. Use the JSON "
              "or JSONL format for them.",
    },
    "writer.warn.huge_skipped_sqlite.one": {
        "uz": "{count} ta juda katta yozuv ({limit} dan katta) SQLite bazasiga yozilmadi. U uchun JSON yoki "
              "JSONL formatidan foydalaning.",
        "en": "{count} very large record (over {limit}) was not written to the SQLite database. Use the JSON "
              "or JSONL format for it.",
    },
    "writer.warn.huge_unsorted": {
        "uz": "{count} ta juda katta yozuvning kalitlari tartiblanmasdan yozildi.",
        "en": "The keys of {count} very large records were written unsorted.",
    },
    "writer.warn.huge_unsorted.one": {
        "uz": "{count} ta juda katta yozuvning kalitlari tartiblanmasdan yozildi.",
        "en": "The keys of {count} very large record were written unsorted.",
    },
    "writer.warn.wrapper_dropped": {
        "uz": "Hujjat to'liq emas, shuning uchun tashqi o'rama saqlanmadi: faqat yozuvlar yozildi.",
        "en": "The document is incomplete, so the outer wrapper was not kept: only the records were written.",
    },
    "writer.warn.xlsx_number_text": {
        "uz": "{count} ta son Excel'da aniq saqlanmaydi (15 tadan ko'p raqam yoki juda katta/kichik), shuning "
              "uchun matn sifatida yozildi.",
        "en": "{count} numbers cannot be stored exactly in Excel (more than 15 digits, or too large/small), so "
              "they were written as text.",
    },
    "writer.warn.xlsx_number_text.one": {
        "uz": "{count} ta son Excel'da aniq saqlanmaydi (15 tadan ko'p raqam yoki juda katta/kichik), shuning "
              "uchun matn sifatida yozildi.",
        "en": "{count} number cannot be stored exactly in Excel (more than 15 digits, or too large/small), so "
              "it was written as text.",
    },
    "writer.warn.xlsx_truncated": {
        "uz": "{count} ta matn Excel chegarasidan ({max} belgi) uzun edi va qisqartirildi.",
        "en": "{count} texts were longer than the Excel limit ({max} characters) and were truncated.",
    },
    "writer.warn.xlsx_truncated.one": {
        "uz": "{count} ta matn Excel chegarasidan ({max} belgi) uzun edi va qisqartirildi.",
        "en": "{count} text was longer than the Excel limit ({max} characters) and was truncated.",
    },
    "writer.warn.xlsx_sheets": {
        "uz": "Qatorlar bitta varaqqa sig'madi: ma'lumot {count} ta varaqqa bo'lindi.",
        "en": "The rows did not fit on one sheet: the data was split into {count} sheets.",
    },
    "writer.warn.xlsx_files": {
        "uz": "Qatorlar bitta faylga sig'madi: ma'lumot {count} ta faylga bo'lindi.",
        "en": "The rows did not fit into one file: the data was split into {count} files.",
    },
    "writer.warn.duplicates": {
        "uz": "{count} ta takroriy yozuv SQLite bazasiga qayta yozilmadi.",
        "en": "{count} repeated records were written to the SQLite database only once.",
    },
    "writer.warn.duplicates.one": {
        "uz": "{count} ta takroriy yozuv SQLite bazasiga qayta yozilmadi.",
        "en": "{count} repeated record was written to the SQLite database only once.",
    },
    "writer.warn.map_columns": {
        "uz": "{limit} tadan ko'p turli kalitli obyektlar lug'at sifatida ishlatilgan: ularning kam uchraydigan "
              "kalitlari (va eng ko'p uchraydigan {max} tadan keyingilari) har bir obyekt uchun bitta ustunga "
              "ixcham JSON qilib yozildi ({count} ta obyekt): {paths}.",
        "en": "Objects with more than {limit} different keys are used as maps: their rarely used keys (and those "
              "beyond the {max} most frequent) were written as compact JSON in one column per object "
              "(objects: {count}): {paths}.",
    },
    "writer.warn.map_columns.item": {
        "uz": "{column} ({keys} ta kalit)",
        "en": "{column} (keys: {keys})",
    },
    "writer.warn.deep_columns": {
        "uz": "{limit} darajadan chuqur joylashgan obyektlar ixcham JSON ustun qilib yozildi ({count} ta ustun).",
        "en": "Objects nested deeper than {limit} levels were written as compact JSON columns (columns: {count}).",
    },
    "writer.warn.columns_capped": {
        "uz": "Yozuvlarda {max} tadan ko'p maydon bor: {depth}-darajadan chuqurroq obyektlar ixcham JSON ustun "
              "qilib yozildi (jami {count} ta ustun). Kerakli ustunlarni o'zingiz ham ko'rsatishingiz mumkin.",
        "en": "The records have more than {max} fields: objects deeper than level {depth} were written as compact "
              "JSON columns (columns in all: {count}). You can also specify the columns you need.",
    },
    "writer.warn.deep_skipped": {
        "uz": "{count} ta yozuv juda chuqur ichma-ich joylashgani uchun xotirada o'qilmadi va o'tkazib "
              "yuborildi. Ularni ham chiqarish uchun ustunlarni aniq ko'rsating.",
        "en": "Records nested too deeply to be read in memory were skipped: {count}. Specify the columns "
              "explicitly to export them too.",
    },
    "writer.warn.deep_skipped_sqlite": {
        "uz": "{count} ta yozuv juda chuqur ichma-ich joylashgani uchun SQLite bazasiga yozilmadi. Ular uchun "
              "JSON yoki JSONL formatidan foydalaning.",
        "en": "Records nested too deeply to be read in memory were not written to the SQLite database: {count}. "
              "Use the JSON or JSONL format for them.",
    },
    "writer.warn.deep_unsorted": {
        "uz": "{count} ta yozuv juda chuqur ichma-ich joylashgani uchun kalitlari tartiblanmasdan yozildi.",
        "en": "Records nested too deeply to be read in memory were written with unsorted keys: {count}.",
    },
    "writer.warn.cell_truncated": {
        "uz": "Juda katta yozuvlarning {count} ta katagi {limit} belgida qisqartirildi; birinchisi: #{index} "
              "yozuvning {column} ustuni. Bu qatorlarning boshqa kataklari to'liq yozildi.",
        "en": "Cells of very large records were cut at {limit} characters (cells: {count}); the first is in "
              "record #{index}, column {column}. The other cells of these rows are complete.",
    },
    "writer.warn.columns_not_found": {
        "uz": "Quyidagi ustunlar uchun eksport qilingan birorta ham yozuvda qiymat topilmadi (yo'l to'g'ri "
              "yozilganini tekshiring): {columns}.",
        "en": "No exported record has a value for these columns (check the spelling of the paths): {columns}.",
    },
    "writer.warn.sqlite_number_text": {
        "uz": "{count} ta son SQLite'da son sifatida aniq saqlanmaydi (double sig'dira olmaydigan raqamlar, "
              "64 bitdan katta butun son yoki juda katta/kichik qiymat), shuning uchun aniq matni saqlandi.",
        "en": "Numbers that no SQLite number holds exactly (more digits than a double keeps, integers beyond "
              "64 bits, or too large/small) were stored as their exact text: {count}.",
    },
    "writer.warn.sqlite_extra": {
        "uz": "Jadval(lar)dagi ustunlar soni {max} ga yetdi ({tables}): {count} ta qatorning qolgan maydonlari "
              "{column} ustuniga ixcham JSON qilib yozildi.",
        "en": "The table(s) {tables} reached {max} columns: the remaining fields were stored as compact JSON "
              "in the column {column} (rows: {count}).",
    },
    "writer.warn.sqlite_tables": {
        "uz": "Bazada {max} ta bola jadval yaratildi: keyingi yo'llardagi massivlar ota qatorning ustuniga "
              "ixcham JSON matn qilib yozildi ({count} ta massiv): {paths}.",
        "en": "The database reached {max} child tables: arrays at further paths were stored as compact JSON "
              "text in a column of their parent row (arrays: {count}): {paths}.",
    },
    "writer.warn.sqlite_deep": {
        "uz": "{limit} darajadan chuqur joylashgan {count} ta qiymat JSON matni sifatida saqlandi.",
        "en": "Values nested deeper than {limit} levels were stored as JSON text: {count}.",
    },
    "writer.warn.xlsx_stale_parts": {
        "uz": "Oldingi eksportdan qolgan {count} ta qism fayl ({names}) bu natijaga kirmaydi; ular "
              "o'zgartirilmadi.",
        "en": "Part files of an earlier export are not part of this result and were left unchanged "
              "({count}): {names}.",
    },
    "writer.expected.as_keyed": {
        "uz": "false — kalitli obyektni tiklash faqat JSON formatida ishlaydi (tanlangan format: {fmt})",
        "en": "false — rebuilding the keyed object works only for the JSON format (the chosen format: {fmt})",
    },
    "writer.error.not_single_member": {
        "uz": "Kalitli obyektni tiklab bo'lmaydi: #{index} yozuv bitta a'zoli obyekt emas, u — {what}. "
              "Hech narsa saqlanmadi.",
        "en": "The keyed object cannot be rebuilt: record #{index} is not an object with exactly one member, "
              "it is {what}. Nothing was saved.",
    },
    "writer.error.not_single_member.hint": {
        "uz": "Kalitli obyektni tiklash uchun har bir yozuv {{\"kalit\": qiymat}} ko'rinishida bo'lishi kerak "
              "(kalitli faylni JSONL'ga eksport qilganda shunday qatorlar hosil bo'ladi). Yozuvlarni massiv "
              "sifatida saqlash uchun bu opsiyani (--as-keyed) olib tashlang.",
        "en": "To rebuild a keyed object every record must look like {{\"key\": value}} (exporting a keyed file "
              "to JSONL gives such lines). Remove this option (--as-keyed) to save the records as an array.",
    },
    "writer.shape.array": {"uz": "massiv", "en": "an array"},
    "writer.shape.string": {"uz": "matn (satr)", "en": "a string"},
    "writer.shape.number": {"uz": "son", "en": "a number"},
    "writer.shape.bool": {"uz": "mantiqiy qiymat (true/false)", "en": "a boolean (true/false)"},
    "writer.shape.null": {"uz": "null", "en": "null"},
    "writer.shape.empty": {"uz": "bo'sh obyekt", "en": "an empty object"},
    "writer.shape.members": {"uz": "{count} a'zoli obyekt", "en": "an object with {count} members"},
    "writer.warn.duplicate_keys": {
        "uz": "{count} ta yozuvning kaliti oldingi yozuvdagi kalit bilan bir xil (birinchisi: #{index} yozuv, "
              "kalit {key}): natijadagi obyektda takroriy kalitlar bor. Ular o'zgarishsiz yozildi — JSON bunga "
              "yo'l qo'yadi, lekin ko'p dasturlar takroriy kalitning faqat oxirgi qiymatini oladi.{note}",
        "en": "{count} records repeat the key of an earlier record (the first: record #{index}, key {key}): the "
              "result object has repeated keys. They were written unchanged — JSON allows this, but many programs "
              "keep only the last value of a repeated key.{note}",
    },
    "writer.warn.duplicate_keys.one": {
        "uz": "{count} ta yozuvning kaliti oldingi yozuvdagi kalit bilan bir xil (#{index} yozuv, kalit {key}): "
              "natijadagi obyektda takroriy kalit bor. U o'zgarishsiz yozildi — JSON bunga yo'l qo'yadi, lekin "
              "ko'p dasturlar takroriy kalitning faqat oxirgi qiymatini oladi.{note}",
        "en": "{count} record repeats the key of an earlier record (record #{index}, key {key}): the result "
              "object has a repeated key. It was written unchanged — JSON allows this, but many programs keep "
              "only the last value of a repeated key.{note}",
    },
    "writer.note.duplicate_keys_limited": {
        "uz": " Xotirani tejash uchun faqat birinchi {checked} ta kalit tekshirildi.",
        "en": " To save memory, only the first {checked} keys were checked.",
    },
    "writer.progress.index": {"uz": "Indekslanmoqda: {name}", "en": "Indexing {name}"},
    "writer.progress.columns": {"uz": "Ustunlar aniqlanmoqda…", "en": "Finding the columns…"},
    "writer.progress.export": {"uz": "Eksport qilinmoqda: {name}", "en": "Exporting {name}"},
    "writer.progress.finish": {"uz": "Fayl yakunlanmoqda: {name}", "en": "Finishing {name}"},
    # ---- cli ----
    # argparse's own phrases (cli.ARGPARSE_MESSAGES maps argparse msgids to these keys)
    "cli.argparse.usage": {"uz": "foydalanish: ", "en": "usage: "},
    "cli.argparse.heading": {"uz": "%(heading)s:", "en": "%(heading)s:"},
    "cli.argparse.default": {"uz": " (standart: %(default)s)", "en": " (default: %(default)s)"},
    "cli.argparse.argument": {
        "uz": "%(argument_name)s argumenti: %(message)s",
        "en": "argument %(argument_name)s: %(message)s",
    },
    "cli.argparse.version": {
        "uz": "dastur versiyasini ko'rsatish va chiqish",
        "en": "show program's version number and exit",
    },
    "cli.argparse.unknown_parser": {
        "uz": "noma'lum buyruq %(parser_name)r (mavjudlari: %(choices)s)",
        "en": "unknown parser %(parser_name)r (choices: %(choices)s)",
    },
    "cli.argparse.deprecated_command": {
        "uz": "'%(parser_name)s' buyrug'i eskirgan",
        "en": "command '%(parser_name)s' is deprecated",
    },
    "cli.argparse.cant_open": {
        "uz": "'%(filename)s' faylini ochib bo'lmadi: %(error)s",
        "en": "can't open '%(filename)s': %(error)s",
    },
    "cli.argparse.positionals": {"uz": "argumentlar", "en": "positional arguments"},
    "cli.argparse.options": {"uz": "parametrlar", "en": "options"},
    "cli.argparse.help": {"uz": "shu yordamni ko'rsatish va chiqish", "en": "show this help message and exit"},
    "cli.argparse.subcommands": {"uz": "buyruqlar", "en": "subcommands"},
    "cli.argparse.unrecognized": {"uz": "tanilmagan argumentlar: %s", "en": "unrecognized arguments: %s"},
    "cli.argparse.not_allowed": {
        "uz": "%s argumenti bilan birga ishlatib bo'lmaydi",
        "en": "not allowed with argument %s",
    },
    "cli.argparse.ambiguous": {
        "uz": "noaniq parametr: %(option)s quyidagilardan biri bo'lishi mumkin: %(matches)s",
        "en": "ambiguous option: %(option)s could match %(matches)s",
    },
    "cli.argparse.ignored_explicit": {
        "uz": "ortiqcha qiymat e'tiborga olinmadi: %r",
        "en": "ignored explicit argument %r",
    },
    "cli.argparse.deprecated_option": {
        "uz": "'%(option)s' parametri eskirgan",
        "en": "option '%(option)s' is deprecated",
    },
    "cli.argparse.deprecated_argument": {
        "uz": "'%(argument_name)s' argumenti eskirgan",
        "en": "argument '%(argument_name)s' is deprecated",
    },
    "cli.argparse.required": {
        "uz": "quyidagi argumentlar majburiy: %s",
        "en": "the following arguments are required: %s",
    },
    "cli.argparse.one_required": {
        "uz": "%s argumentlaridan bittasi majburiy",
        "en": "one of the arguments %s is required",
    },
    "cli.argparse.expected_one": {"uz": "bitta qiymat kutilgan", "en": "expected one argument"},
    "cli.argparse.expected_at_most_one": {
        "uz": "ko'pi bilan bitta qiymat kutilgan",
        "en": "expected at most one argument",
    },
    "cli.argparse.expected_at_least_one": {
        "uz": "kamida bitta qiymat kutilgan",
        "en": "expected at least one argument",
    },
    "cli.argparse.expected_n": {"uz": "%s ta qiymat kutilgan", "en": "expected %s arguments"},
    "cli.argparse.expected_n.one": {"uz": "%s ta qiymat kutilgan", "en": "expected %s argument"},
    "cli.argparse.unexpected_option": {"uz": "kutilmagan parametr: %s", "en": "unexpected option string: %s"},
    "cli.argparse.invalid_value": {
        "uz": "noto'g'ri %(type)s qiymati: %(value)r",
        "en": "invalid %(type)s value: %(value)r",
    },
    "cli.argparse.invalid_choice": {
        "uz": "noto'g'ri tanlov: %(value)r (mumkin bo'lganlari: %(choices)s)",
        "en": "invalid choice: %(value)r (choose from %(choices)s)",
    },
    "cli.argparse.invalid_choice_suggest": {
        "uz": "noto'g'ri tanlov: %(value)r, %(closest)r nazarda tutilganmi? (mumkin bo'lganlari: %(choices)s)",
        "en": "invalid choice: %(value)r, maybe you meant %(closest)r? (choose from %(choices)s)",
    },
    "cli.argparse.error": {"uz": "%(prog)s: xato: %(message)s\n", "en": "%(prog)s: error: %(message)s\n"},
    "cli.argparse.warning": {
        "uz": "%(prog)s: ogohlantirish: %(message)s\n",
        "en": "%(prog)s: warning: %(message)s\n",
    },
    # argument value errors
    "cli.argerr.positive_int": {
        "uz": "musbat butun son bo'lishi kerak, berilgan: {value}",
        "en": "must be a positive whole number, got: {value}",
    },
    "cli.argerr.non_negative_int": {
        "uz": "0 yoki undan katta butun son bo'lishi kerak, berilgan: {value}",
        "en": "must be a whole number of at least 0, got: {value}",
    },
    "cli.argerr.indent": {
        "uz": "0 dan 16 gacha son (masalan, 2 yoki 4) yoki tab bo'lishi kerak, berilgan: {value}",
        "en": "must be a number from 0 to 16 (such as 2 or 4) or tab, got: {value}",
    },
    "cli.argerr.size": {
        "uz": "hajm noto'g'ri: {value} (misollar: 256MB, 1GB; birliksiz son megabayt: 512 = 512MB)",
        "en": "invalid size: {value} (examples: 256MB, 1GB; a number without a unit is in megabytes: 512 = 512MB)",
    },
    "cli.argerr.no_command": {
        "uz": "buyruq ko'rsatilmagan: inspect, search, filter, sort, convert yoki presets",
        "en": "no command given: inspect, search, filter, sort, convert or presets",
    },
    # help texts (argument help strings must not contain the percent sign)
    "cli.help.description": {
        "uz": "Katta JSON fayllar bilan ishlash vositasi: tuzilmani ko'rish, qidirish, filtrlash, tartiblash va\n"
              "boshqa formatlarga o'tkazish. Fayl hech qachon to'liq xotiraga yuklanmaydi, asl fayl o'zgarmaydi.",
        "en": "A tool for huge JSON files: inspect the structure, search, filter, sort and convert to other\n"
              "formats. The file is never loaded into memory as a whole and the original is never modified.",
    },
    "cli.help.epilog": {
        "uz": "Misollar:\n"
              "  jsonreshape inspect data.json\n"
              "  jsonreshape search data.json Toshkent --translit\n"
              "  jsonreshape filter data.json natija.json --where \"contact.address.city == 'Toshkent'\"\n"
              "  jsonreshape sort data.json natija.json --by name --by -id\n"
              "  jsonreshape convert data.json natija.csv --columns \"id,name,contact.address.city\"\n"
              "\n"
              "Ifodani (va vergulli ro'yxatni) qo'shtirnoq (\"...\") ichida, undagi matnni bitta tirnoq ('...')\n"
              "ichida yozing: misollar cmd.exe, PowerShell va bash'da bir xil ishlaydi.\n"
              "\n"
              "Chiqish kodlari: 0 - muvaffaqiyatli; 1 - noto'g'ri buyruq, parametr, ifoda yoki yo'l; 2 - kirish\n"
              "faylida JSON xatosi yoki fayl ish paytida o'zgardi; 3 - bekor qilindi (Ctrl+C); 4 - ichki xato\n"
              "(tafsilotlar log faylida).\n"
              "Har bir buyruq haqida batafsil: jsonreshape BUYRUQ --help",
        "en": "Examples:\n"
              "  jsonreshape inspect data.json\n"
              "  jsonreshape search data.json Toshkent --translit\n"
              "  jsonreshape filter data.json result.json --where \"contact.address.city == 'Toshkent'\"\n"
              "  jsonreshape sort data.json result.json --by name --by -id\n"
              "  jsonreshape convert data.json result.csv --columns \"id,name,contact.address.city\"\n"
              "\n"
              "Put an expression (and a comma list) in double quotes (\"...\") and the texts inside it in single\n"
              "quotes ('...'): the examples then work the same in cmd.exe, PowerShell and bash.\n"
              "\n"
              "Exit codes: 0 - success; 1 - invalid command, option, expression or path; 2 - JSON error in the\n"
              "input file, or the file changed while it was read; 3 - cancelled (Ctrl+C); 4 - internal error\n"
              "(details in the log file).\n"
              "More about a command: jsonreshape COMMAND --help",
    },
    "cli.help.commands": {"uz": "buyruqlar", "en": "commands"},
    "cli.help.version": {
        "uz": "versiyalarni ko'rsatish (jsonreshape, yadro, Python) va chiqish",
        "en": "show the versions (jsonreshape, core, Python) and exit",
    },
    "cli.help.lang": {
        "uz": "til: uz (o'zbekcha, standart) yoki en (inglizcha)",
        "en": "language: uz (Uzbek, default) or en (English)",
    },
    "cli.help.backend": {
        "uz": "yadro: auto (Rust bo'lsa Rust), rust yoki python (sekin etalon)",
        "en": "core: auto (Rust when available), rust or python (slow reference)",
    },
    "cli.help.quiet": {
        "uz": "jarayon qatori va xulosalarni chiqarmaslik (ogohlantirish va xatolar baribir chiqadi)",
        "en": "no progress line and no summaries (warnings and errors are still shown)",
    },
    "cli.help.verbose": {
        "uz": "log yozuvlarini ham ko'rsatish: -v (INFO), -vv (DEBUG va xato tafsilotlari)",
        "en": "also show log records: -v (INFO), -vv (DEBUG and error tracebacks)",
    },
    "cli.help.input": {
        "uz": "kirish JSON fayli (.json, .jsonl, .json.gz; UTF-8 yoki UTF-16)",
        "en": "input JSON file (.json, .jsonl, .json.gz; UTF-8 or UTF-16)",
    },
    "cli.help.output": {
        "uz": "natija fayli; formati kengaytmadan aniqlanadi (.json, .jsonl, .csv, .xlsx, .sqlite) yoki --to bilan "
              "beriladi",
        "en": "output file; the format comes from the extension (.json, .jsonl, .csv, .xlsx, .sqlite) or from --to",
    },
    "cli.help.sample": {
        "uz": "tuzilma uchun namuna hajmi, yozuvlar soni (standart: sozlamalardagi qiymat, odatda 10000)",
        "en": "sample size for the structure, in records (default: the setting, usually 10000)",
    },
    "cli.help.json": {
        "uz": "hisobotni bitta JSON obyekt sifatida chiqarish (aniq sonlar bilan)",
        "en": "print the report as one JSON object (exact numbers)",
    },
    "cli.help.text": {
        "uz": "qidiriladigan matn (--regex bilan regex, --number bilan son sharti, masalan '> 1000')",
        "en": "text to search for (a regex with --regex, a number condition such as '> 1000' with --number)",
    },
    "cli.help.keys": {"uz": "faqat kalitlarda qidirish", "en": "search keys only"},
    "cli.help.values": {"uz": "faqat qiymatlarda qidirish", "en": "search values only"},
    "cli.help.regex": {"uz": "TEXT - regex ifodasi", "en": "TEXT is a regular expression"},
    "cli.help.number": {
        "uz": "TEXT - son sharti: > 1000, <= 5, 10..20 yoki 42",
        "en": "TEXT is a number condition: > 1000, <= 5, 10..20 or 42",
    },
    "cli.help.case_sensitive": {"uz": "katta-kichik harfni farqlash", "en": "match upper and lower case exactly"},
    "cli.help.whole_word": {"uz": "faqat butun so'zlarni topish", "en": "match whole words only"},
    "cli.help.translit": {
        "uz": "lotin va kirill yozuvini farqlamaslik (Toshkent = Тошкент)",
        "en": "treat Latin and Cyrillic spelling as equal (Toshkent = Тошкент)",
    },
    "cli.help.path": {
        "uz": "faqat shu yo'l ichida qidirish (masalan: name, contact.address.city, ..name)",
        "en": "search only inside this path (e.g. name, contact.address.city, ..name)",
    },
    "cli.help.limit": {
        "uz": "chiqariladigan mosliklar soni (standart 50; 0 - ro'yxatsiz)",
        "en": "number of hits to print (default 50; 0 = none)",
    },
    "cli.help.count": {
        "uz": "faqat mosliklar va yozuvlar sonini chiqarish",
        "en": "print only the number of hits and records",
    },
    "cli.help.export": {
        "uz": "barcha mosliklarni CSV yoki JSON faylga yozish (kengaytma: .csv yoki .json)",
        "en": "write every hit to a CSV or JSON file (extension .csv or .json)",
    },
    "cli.help.max_hits": {
        "uz": "ro'yxat uchun yig'iladigan mosliklar chegarasi (standart 100000; sanash davom etadi)",
        "en": "maximum hits collected for the listing (default 100000; counting goes on)",
    },
    "cli.help.where": {
        "uz": "filtr ifodasi, masalan: contact.address.city == 'Toshkent' and len(children) > 2",
        "en": "filter expression, e.g. contact.address.city == 'Toshkent' and len(children) > 2",
    },
    "cli.help.jmespath": {
        "uz": "JMESPath filtri, masalan: active && contains(tags, 'x') (natijasi rost bo'lgan yozuvlar qoladi)",
        "en": "JMESPath filter, e.g. active && contains(tags, 'x') (records with a truthy result are kept)",
    },
    "cli.help.preset": {
        "uz": "saqlangan filtr nomi (--where yoki --jmespath o'rniga); ro'yxati: jsonreshape presets",
        "en": "name of a saved filter (instead of --where or --jmespath); list them with: jsonreshape presets",
    },
    "cli.help.save_preset": {
        "uz": "shu filtrni NAME nomi bilan saqlash (keyin --preset NAME bilan ishlatiladi)",
        "en": "save this filter as NAME (use it later with --preset NAME)",
    },
    "cli.help.by": {
        "uz": "tartiblash maydoni; kamayish tartibi uchun oldiga - qo'ying (--by -id); bir necha marta berish mumkin",
        "en": "sort field; prefix it with - for descending order (--by -id); may be repeated",
    },
    "cli.help.sort_by": {
        "uz": "avval shu maydon bo'yicha tartiblash; kamayish uchun oldiga - (--sort-by -id); bir necha marta "
              "berish mumkin",
        "en": "sort by this field first; prefix it with - for descending order (--sort-by -id); may be repeated",
    },
    "cli.help.nulls": {
        "uz": "null va yo'q qiymatlar o'rni: first (boshida) yoki last (oxirida, standart)",
        "en": "where null and missing values go: first or last (default)",
    },
    "cli.help.collation": {
        "uz": "satrlarni solishtirish: uz (o'zbek alifbosi, standart), simple yoki unicode",
        "en": "string order: uz (Uzbek alphabet, default), simple or unicode",
    },
    "cli.help.no_natural": {
        "uz": "tabiiy tartibni o'chirish (child2 < child10 qoidasi)",
        "en": "turn off natural order (child2 < child10)",
    },
    "cli.help.sort_case_sensitive": {
        "uz": "katta-kichik harf tartibga ta'sir qiladi",
        "en": "upper and lower case sort differently",
    },
    "cli.help.memory_limit": {
        "uz": "tartiblash uchun xotira, masalan 256MB yoki 1GB; birliksiz son megabayt (512 = 512MB); kamida 16MB, "
              "ko'pi bilan «Xotira chegarasi» sozlamasining yarmi (oshsa, vaqtinchalik fayllar bilan tartiblanadi)",
        "en": "memory for sorting, e.g. 256MB or 1GB; a number without a unit is in megabytes (512 = 512MB); at "
              "least 16MB, at most half of the \"Memory limit\" setting (beyond it the sort uses temporary files)",
    },
    "cli.help.record_path": {
        "uz": "yozuvlar massivining yo'li (masalan: data yoki result.items; $ - ildiz)",
        "en": "path of the record array (e.g. data or result.items; $ = the root)",
    },
    "cli.help.format": {
        "uz": "kirish formati (standart: auto - avtomatik aniqlash)",
        "en": "input format (default: auto - detect it)",
    },
    "cli.help.allow_partial": {
        "uz": "JSON xatosi bo'lsa, xatogacha bo'lgan yozuvlar bilan davom etish",
        "en": "on a JSON error, go on with the records before the error",
    },
    "cli.help.no_cache": {
        "uz": "keshdagi indeksni ishlatmaslik (fayl qayta indekslanadi)",
        "en": "ignore a cached index (index the file again)",
    },
    "cli.help.to": {
        "uz": "natija formati (standart: OUTPUT kengaytmasidan)",
        "en": "output format (default: from the OUTPUT extension)",
    },
    "cli.help.jsonl": {
        "uz": "JSON Lines formatida yozish (--to jsonl bilan bir xil)",
        "en": "write JSON Lines (same as --to jsonl)",
    },
    "cli.help.compact": {"uz": "ixcham JSON (bo'sh joylarsiz)", "en": "compact JSON (no whitespace)"},
    "cli.help.indent": {
        "uz": "JSON chekinishi: 2, 4 yoki tab (standart: sozlamalardan, odatda 2)",
        "en": "JSON indentation: 2, 4 or tab (default: the setting, usually 2)",
    },
    "cli.help.sort_keys": {
        "uz": "har bir yozuv ichidagi kalitlarni tartiblash: alpha - alifbo bo'yicha (o'zbek alifbosi, tabiiy "
              "tartib, katta-kichik harf farqsiz; standart) yoki codepoint - Unicode kodlari tartibida",
        "en": "sort the keys inside every record: alpha - alphabetically (Uzbek alphabet, natural order, "
              "case-insensitive; the default) or codepoint - by Unicode code point",
    },
    "cli.help.no_wrapper": {
        "uz": "tashqi o'ramani tashlab, faqat yozuvlar massivini yozish (obyekt ichidagi massiv formati uchun)",
        "en": "drop the outer wrapper and write only the record array (array-inside-an-object files)",
    },
    "cli.help.as_keyed": {
        "uz": "JSON: har bir yozuvi bitta a'zoli obyekt (\"kalit\": qiymat) bo'lgan fayldan (masalan, kalitli "
              "fayldan olingan JSONL) bitta kalitli obyektni qayta yig'ish; boshqa ko'rinishdagi yozuv bo'lsa, "
              "xato chiqadi va hech narsa saqlanmaydi",
        "en": "JSON: rebuild one keyed object from records that are all one-member objects (\"key\": value), "
              "e.g. JSONL made from a keyed file; any other record is an error and nothing is saved",
    },
    "cli.help.columns": {
        "uz": "CSV/XLSX ustunlari, vergul bilan: id,name,contact.address.city",
        "en": "CSV/XLSX columns, comma separated: id,name,contact.address.city",
    },
    "cli.help.array_mode": {
        "uz": "massivlar: join (bitta katakka birlashtirish) yoki rows (har elementga alohida qator)",
        "en": "arrays: join (one cell) or rows (one row per element)",
    },
    "cli.help.explode": {
        "uz": "qatorlarga yoyiladigan massiv yo'li (masalan: orders; rows rejimini yoqadi)",
        "en": "path of the array expanded into rows (e.g. orders; implies rows)",
    },
    "cli.help.sep": {
        "uz": "massiv elementlarini birlashtiruvchi matn (standart: ;)",
        "en": "separator of joined array elements (default: ;)",
    },
    "cli.help.delimiter": {
        "uz": "CSV ajratuvchisi: bitta belgi yoki tab (standart: ,)",
        "en": "CSV delimiter: one character or tab (default: ,)",
    },
    "cli.help.no_bom": {
        "uz": "CSV boshiga BOM yozmaslik (Excel uchun BOM odatda yoziladi)",
        "en": "no BOM at the start of the CSV (written by default for Excel)",
    },
    "cli.help.xlsx_split": {
        "uz": "1 048 576 qatordan oshsa: sheets (yangi varaqlar) yoki files (yangi fayllar)",
        "en": "above 1,048,576 rows: sheets (new sheets) or files (new files)",
    },
    "cli.group.global": {"uz": "umumiy parametrlar", "en": "general options"},
    "cli.group.input": {"uz": "kirish fayli parametrlari", "en": "input options"},
    "cli.group.output": {"uz": "natija fayli parametrlari", "en": "output options"},
    "cli.group.filter": {"uz": "filtr", "en": "filter"},
    "cli.group.sort": {"uz": "tartiblash", "en": "sorting"},
    "cli.group.inspect": {"uz": "hisobot parametrlari", "en": "report options"},
    "cli.group.search": {"uz": "qidiruv parametrlari", "en": "search options"},
    "cli.group.presets": {"uz": "saqlangan filtrlar", "en": "saved filters"},
    "cli.help.delete_preset": {"uz": "shu nomdagi saqlangan filtrni o'chirish", "en": "delete the saved filter NAME"},
    "cli.help.presets_json": {
        "uz": "ro'yxatni JSON sifatida chiqarish",
        "en": "print the list as JSON",
    },
    # commands
    "cli.cmd.inspect.help": {"uz": "fayl haqida ma'lumot va uning tuzilmasi", "en": "file information and structure"},
    "cli.cmd.inspect.description": {
        "uz": "Faylni indekslaydi va hisobot chiqaradi: hajm, kodirovka, format, yozuvlar soni, indekslash\n"
              "tezligi, JSON xatolari va namunadagi yozuvlar tuzilmasi (maydonlar, turlar, mavjudlik, misollar).",
        "en": "Indexes the file and prints a report: size, encoding, format, number of records, indexing\n"
              "speed, JSON errors and the structure of a sample of records (fields, types, presence, examples).",
    },
    "cli.cmd.inspect.epilog": {
        "uz": "Misollar:\n"
              "  jsonreshape inspect data.json\n"
              "  jsonreshape inspect data.json --sample 50000\n"
              "  jsonreshape inspect data.json --json",
        "en": "Examples:\n"
              "  jsonreshape inspect data.json\n"
              "  jsonreshape inspect data.json --sample 50000\n"
              "  jsonreshape inspect data.json --json",
    },
    "cli.cmd.search.help": {"uz": "butun fayl bo'yicha qidiruv", "en": "search the whole file"},
    "cli.cmd.search.description": {
        "uz": "Kalit va qiymatlarda matn, regex yoki son shartini qidiradi. Mosliklar topilishi bilan chiqariladi:\n"
              "#yozuv  yo'l  [kalit|qiymat]  parcha. Katta-kichik harf va apostrof turlari farqlanmaydi.",
        "en": "Searches keys and values for text, a regular expression or a number condition. Hits are printed\n"
              "as they are found: #record  path  [key|value]  snippet. Case and apostrophe variants are ignored.",
    },
    "cli.cmd.search.epilog": {
        "uz": "Misollar:\n"
              "  jsonreshape search data.json Toshkent\n"
              "  jsonreshape search data.json Toshkent --translit --path contact.address.city\n"
              "  jsonreshape search data.json \"^child[0-9]+$\" --regex --keys\n"
              "  jsonreshape search data.json \"> 1000\" --number --count\n"
              "  jsonreshape search data.json Toshkent --export natijalar.csv",
        "en": "Examples:\n"
              "  jsonreshape search data.json Toshkent\n"
              "  jsonreshape search data.json Toshkent --translit --path contact.address.city\n"
              "  jsonreshape search data.json \"^child[0-9]+$\" --regex --keys\n"
              "  jsonreshape search data.json \"> 1000\" --number --count\n"
              "  jsonreshape search data.json Toshkent --export hits.csv",
    },
    "cli.cmd.filter.help": {
        "uz": "shartga mos yozuvlarni yangi faylga yozish",
        "en": "write the records matching a condition to a new file",
    },
    "cli.cmd.filter.description": {
        "uz": "Ifoda (--where) yoki JMESPath (--jmespath) bo'yicha mos yozuvlarni tanlab, natija fayliga yozadi.\n"
              "Asl fayl o'zgarmaydi; natija avval vaqtinchalik faylga yoziladi.",
        "en": "Selects the records matching an expression (--where) or JMESPath (--jmespath) and writes them\n"
              "to the output file. The original is never modified; the result is written to a temporary file first.",
    },
    "cli.cmd.filter.epilog": {
        "uz": "Misollar:\n"
              "  jsonreshape filter data.json toshkent.json --where \"contact.address.city == 'Toshkent'\"\n"
              "  jsonreshape filter data.json katta.jsonl --where \"len(orders) > 2 and score >= 100\"\n"
              "  jsonreshape filter data.json faol.csv --jmespath active --columns \"id,name\"\n"
              "  jsonreshape filter data.json yuqori.json --jmespath \"active && score > to_number('100')\"\n"
              "  jsonreshape filter data.json toshkent.json --where \"contact.address.city == 'Toshkent'\" "
              "--save-preset toshkent\n"
              "  jsonreshape filter data.json toshkent.csv --preset toshkent\n"
              "\n"
              "Tirnoqlar: ifodani qo'shtirnoq (\"...\") ichiga, undagi matnni bitta tirnoq ('...') ichiga yozing -\n"
              "bu cmd.exe, Windows PowerShell 5.1, PowerShell 7 va bash'da bir xil ishlaydi. JMESPath'dagi `100`\n"
              "kabi qiymatlarni PowerShell'da bitta tirnoqli satrda yozing: --jmespath 'score > `100`'\n"
              "(cmd.exe'da: --jmespath \"score > `100`\").\n"
              "\n"
              "Ifoda tili: == != < <= > >= and or not in between contains startswith endswith matches,\n"
              "is null, is empty, is missing; funksiyalar: len exists missing empty lower upper trim type\n"
              "number string.",
        "en": "Examples:\n"
              "  jsonreshape filter data.json tashkent.json --where \"contact.address.city == 'Toshkent'\"\n"
              "  jsonreshape filter data.json big.jsonl --where \"len(orders) > 2 and score >= 100\"\n"
              "  jsonreshape filter data.json active.csv --jmespath active --columns \"id,name\"\n"
              "  jsonreshape filter data.json top.json --jmespath \"active && score > to_number('100')\"\n"
              "  jsonreshape filter data.json tashkent.json --where \"contact.address.city == 'Toshkent'\" "
              "--save-preset tashkent\n"
              "  jsonreshape filter data.json tashkent.csv --preset tashkent\n"
              "\n"
              "Quoting: put the expression in double quotes (\"...\") and the texts inside it in single quotes\n"
              "('...'); this works the same in cmd.exe, Windows PowerShell 5.1, PowerShell 7 and bash. In\n"
              "PowerShell, a JMESPath value in backticks such as `100` needs single quotes around the expression:\n"
              "--jmespath 'score > `100`' (cmd.exe: --jmespath \"score > `100`\").\n"
              "\n"
              "Expression language: == != < <= > >= and or not in between contains startswith endswith matches,\n"
              "is null, is empty, is missing; functions: len exists missing empty lower upper trim type number\n"
              "string.",
    },
    "cli.cmd.sort.help": {
        "uz": "yozuvlarni maydon(lar) bo'yicha tartiblash",
        "en": "sort the records by one or more fields",
    },
    "cli.cmd.sort.description": {
        "uz": "Yozuvlarni bir yoki bir nechta maydon bo'yicha barqaror tartiblaydi: sonlar son sifatida, satrlar\n"
              "o'zbek alifbosi va tabiiy tartib bilan (child2 < child10). Katta fayllar diskda tartiblanadi.",
        "en": "Sorts the records by one or more fields (stable): numbers as numbers, strings by the Uzbek\n"
              "alphabet with natural order (child2 < child10). Big files are sorted on disk.",
    },
    "cli.cmd.sort.epilog": {
        "uz": "Misollar:\n"
              "  jsonreshape sort data.json saralangan.json --by name --by -id\n"
              "  jsonreshape sort data.json saralangan.jsonl --by contact.address.city --nulls first\n"
              "  jsonreshape sort data.json saralangan.json --by score --memory-limit 128MB",
        "en": "Examples:\n"
              "  jsonreshape sort data.json sorted.json --by name --by -id\n"
              "  jsonreshape sort data.json sorted.jsonl --by contact.address.city --nulls first\n"
              "  jsonreshape sort data.json sorted.json --by score --memory-limit 128MB",
    },
    "cli.cmd.convert.help": {
        "uz": "boshqa formatga o'tkazish (JSON, JSONL, CSV, XLSX, SQLite)",
        "en": "convert to another format (JSON, JSONL, CSV, XLSX, SQLite)",
    },
    "cli.cmd.convert.description": {
        "uz": "Yozuvlarni boshqa formatga yozadi; xohlasangiz avval filtrlaydi (--where) va tartiblaydi\n"
              "(--sort-by). Sonlar aniqligi saqlanadi, Unicode matn o'zgarmaydi.",
        "en": "Writes the records in another format, optionally filtered (--where) and sorted (--sort-by)\n"
              "first. Numbers keep their exact value and Unicode text is not escaped.",
    },
    "cli.cmd.convert.epilog": {
        "uz": "Misollar:\n"
              "  jsonreshape convert data.json data.jsonl\n"
              "  jsonreshape convert data.jsonl data.json --indent 4 --sort-keys\n"
              "  jsonreshape convert data.json jadval.csv --columns \"id,name,orders[*].oid\" --array-mode rows\n"
              "  jsonreshape convert data.json jadval.xlsx --where \"active == true\" --sort-by -score\n"
              "  jsonreshape convert data.json baza.sqlite",
        "en": "Examples:\n"
              "  jsonreshape convert data.json data.jsonl\n"
              "  jsonreshape convert data.jsonl data.json --indent 4 --sort-keys\n"
              "  jsonreshape convert data.json table.csv --columns \"id,name,orders[*].oid\" --array-mode rows\n"
              "  jsonreshape convert data.json table.xlsx --where \"active == true\" --sort-by -score\n"
              "  jsonreshape convert data.json data.sqlite",
    },
    "cli.cmd.presets.help": {"uz": "saqlangan filtrlar ro'yxati", "en": "list the saved filters"},
    "cli.cmd.presets.description": {
        "uz": "filter va convert buyruqlarida --save-preset NAME bilan saqlangan filtrlarni ko'rsatadi;\n"
              "--delete NAME filtrni o'chiradi.",
        "en": "Lists the filters saved with --save-preset NAME of the filter and convert commands;\n"
              "--delete NAME deletes one.",
    },
    "cli.cmd.presets.epilog": {
        "uz": "Misollar:\n"
              "  jsonreshape filter data.json faol.json --where \"active == true\" --save-preset faol\n"
              "  jsonreshape convert data.json faol.csv --preset faol\n"
              "  jsonreshape presets\n"
              "  jsonreshape presets --delete faol",
        "en": "Examples:\n"
              "  jsonreshape filter data.json active.json --where \"active == true\" --save-preset active\n"
              "  jsonreshape convert data.json active.csv --preset active\n"
              "  jsonreshape presets\n"
              "  jsonreshape presets --delete active",
    },
    # messages
    "cli.version": {"uz": "jsonreshape {version}", "en": "jsonreshape {version}"},
    "cli.warning": {"uz": "Ogohlantirish: {message}", "en": "Warning: {message}"},
    "cli.error.prefix": {"uz": "Xato: {message}", "en": "Error: {message}"},
    "cli.error.hint": {"uz": "Maslahat: {hint}", "en": "Hint: {hint}"},
    "cli.usage.help_hint": {"uz": "Yordam: {prog} --help", "en": "Help: {prog} --help"},
    "cli.error.json_input": {
        "uz": "faylda JSON xatosi bor (tafsilotlar hisobotda).",
        "en": "the file has a JSON error (see the report).",
    },
    "cli.error.jsonl_conflict": {
        "uz": "--jsonl va --to {fmt} birga ishlatilmaydi.",
        "en": "--jsonl cannot be combined with --to {fmt}.",
    },
    "cli.error.as_keyed_format": {
        "uz": "--as-keyed faqat JSON natija uchun ishlaydi (tanlangan format: {fmt}).",
        "en": "--as-keyed works only for JSON output (the chosen format: {fmt}).",
    },
    "cli.error.as_keyed_format.hint": {
        "uz": "Natija faylini .json bilan nomlang yoki --to json bering; JSONL, CSV, XLSX va SQLite uchun "
              "--as-keyed'ni olib tashlang.",
        "en": "Name the output file .json or give --to json; remove --as-keyed for JSONL, CSV, XLSX and SQLite.",
    },
    "cli.error.output_format": {
        "uz": "Natija formatini fayl nomidan aniqlab bo'lmadi: {path}",
        "en": "The output format cannot be told from the file name: {path}",
    },
    "cli.error.output_format.hint": {
        "uz": "--to json, jsonl, csv, xlsx yoki sqlite qo'shing yoki .json, .jsonl, .csv, .xlsx, .sqlite "
              "kengaytmasini ishlating.",
        "en": "Add --to json, jsonl, csv, xlsx or sqlite, or use a .json, .jsonl, .csv, .xlsx or .sqlite name.",
    },
    "cli.error.backend": {
        "uz": "Tanlangan yadroni ({backend}) yuklab bo'lmadi: {detail}",
        "en": "The selected core ({backend}) could not be loaded: {detail}",
    },
    "cli.error.backend.hint": {
        "uz": "--backend auto yoki --backend python bilan ishga tushiring.",
        "en": "Run with --backend auto or --backend python.",
    },
    "cli.error.internal": {"uz": "Ichki xato: {detail}", "en": "Internal error: {detail}"},
    "cli.error.see_log": {
        "uz": "Bu dasturdagi xato. Tafsilotlar log faylida: {path}",
        "en": "This is a bug in the program. Details are in the log file: {path}",
    },
    "cli.error.os": {"uz": "fayl tizimi xatosi: {detail}", "en": "file system error: {detail}"},
    "cli.error.os_detail": {"uz": "tizim xabari: {detail}", "en": "system message: {detail}"},
    "cli.error.os.not_found": {"uz": "Fayl yoki papka topilmadi: {path}", "en": "File or folder not found: {path}"},
    "cli.error.os.not_found.hint": {
        "uz": "Yo'lni tekshiring; u turgan disk (masalan, USB yoki tarmoq diski) ulanganiga ishonch hosil qiling.",
        "en": "Check the path and make sure its drive (for example a USB or network drive) is connected.",
    },
    "cli.error.os.access_denied": {"uz": "Ruxsat yo'q: {path}", "en": "Access denied: {path}"},
    "cli.error.os.access_denied.hint": {
        "uz": "Bu fayl yoki papkani o'qish va unga yozish huquqingiz borligini tekshiring yoki boshqa joyni tanlang.",
        "en": "Check that you may read and write this file or folder, or choose another location.",
    },
    "cli.error.os.exists": {
        "uz": "Bu nomda fayl yoki papka allaqachon bor: {path}",
        "en": "A file or folder with this name already exists: {path}",
    },
    "cli.error.os.exists.hint": {
        "uz": "Uni boshqa joyga ko'chiring yoki boshqa nom yoki papka tanlang (dastur ma'lumotlari papkasi: "
              "JSONRESHAPE_HOME; «Kesh papkasi» sozlamasi).",
        "en": "Move it away or choose another name or folder (the program's data folder: JSONRESHAPE_HOME; the "
              "\"Cache folder\" setting).",
    },
    "cli.error.record_path_not_found": {
        "uz": "Yozuvlar yo'li topilmadi: {path}",
        "en": "Record path not found: {path}",
    },
    "cli.error.record_path_not_container": {
        "uz": "Yozuvlar yo'lidagi qiymat massiv yoki obyekt emas: {path}",
        "en": "The value at the record path is not an array or object: {path}",
    },
    "cli.error.record_path_values": {
        "uz": "Yozuvlar yo'li ({path}) faqat bitta JSON hujjat ichida ishlaydi, bu fayl esa {format} deb aniqlandi "
              "(bir nechta JSON qiymat).",
        "en": "A record path ({path}) only works inside a single JSON document, but this file was detected as "
              "{format} (several JSON values).",
    },
    "cli.error.record_path_values.hint": {
        "uz": "--record-path ni olib tashlang: har bir qiymat alohida yozuv bo'ladi. Fayl aslida bitta JSON hujjat "
              "bo'lsa, --format wrapped qo'shing.",
        "en": "Remove --record-path: each value is then a record. If the file is in fact a single JSON document, "
              "add --format wrapped.",
    },
    "cli.error.presets_unavailable": {
        "uz": "Filtrlarni saqlash bu versiyada mavjud emas.",
        "en": "Saved filters are not available in this version.",
    },
    "cli.error.preset_not_found": {
        "uz": "Bunday nomli saqlangan filtr yo'q: {name}",
        "en": "There is no saved filter named {name}",
    },
    "cli.error.preset_not_found.hint": {
        "uz": "Saqlangan filtrlar ro'yxati: jsonreshape presets",
        "en": "List the saved filters with: jsonreshape presets",
    },
    "cli.error.save_preset_needs_filter": {
        "uz": "--save-preset uchun --where, --jmespath yoki --preset kerak.",
        "en": "--save-preset needs --where, --jmespath or --preset.",
    },
    "cli.hint.allow_partial": {
        "uz": "Xatogacha bo'lgan {count} ta yozuv bilan ishlash uchun --allow-partial qo'shing.",
        "en": "Add --allow-partial to work with the {count} records before the error.",
    },
    "cli.hint.allow_partial.one": {
        "uz": "Xatogacha bo'lgan {count} ta yozuv bilan ishlash uchun --allow-partial qo'shing.",
        "en": "Add --allow-partial to work with the {count} record before the error.",
    },
    "cli.hint.no_records_before_error": {
        "uz": "Xatogacha birorta ham yozuv o'qilmadi: fayl JSON ekanini va uning boshi buzilmaganini tekshiring.",
        "en": "Not a single record could be read before the error: check that this is a JSON file and that its "
              "beginning is intact.",
    },
    "cli.hint.format_jsonl": {
        "uz": "Faylda har qatorda bitta JSON qiymat bor shekilli (JSON Lines): buyruqni --format jsonl bilan "
              "qayta ishga tushiring.",
        "en": "The file seems to hold one JSON value per line (JSON Lines): run the command again with "
              "--format jsonl.",
    },
    "cli.hint.format_stream": {
        "uz": "Faylda bir nechta JSON qiymat ketma-ket yozilgan shekilli: buyruqni --format stream bilan qayta "
              "ishga tushiring.",
        "en": "The file seems to hold several JSON values one after another: run the command again with "
              "--format stream.",
    },
    "cli.hint.record_path_values": {
        "uz": "Faylda bir nechta JSON qiymat bor shekilli (masalan, har qatorda bittadan); --record-path faqat "
              "bitta JSON hujjat ichida ishlaydi: uni olib tashlang (har bir qiymat yozuv bo'ladi).",
        "en": "The file seems to hold several JSON values (for example one per line); --record-path only works "
              "inside a single JSON document: remove it (each value is then a record).",
    },
    "cli.hint.record_path_candidates": {
        "uz": "Fayl boshida topilgan massivlar: {candidates}. Masalan: --record-path {example}",
        "en": "Arrays found at the start of the file: {candidates}. For example: --record-path {example}",
    },
    "cli.hint.record_path_none": {
        "uz": "Fayl boshida yozuvlar massivi topilmadi. --record-path ni olib tashlang (format avtomatik "
              "aniqlanadi) yoki to'g'ri yo'lni yozing (masalan: data yoki result.items).",
        "en": "No record array was found at the start of the file. Remove --record-path (the format is then "
              "detected automatically) or give the right path (for example data or result.items).",
    },
    "cli.hint.record_path_root_array": {
        "uz": "Faylning o'zi yozuvlar massivi: --record-path ni olib tashlang.",
        "en": "The file itself is an array of records: remove --record-path.",
    },
    "cli.hint.record_path_syntax": {
        "uz": "Yozuvlar yo'li misollari: data, result.items, /data/0, data[0] ($ - ildiz).",
        "en": "Record path examples: data, result.items, /data/0, data[0] ($ = the root).",
    },
    "cli.hint.path_syntax": {
        "uz": "Misollar: contact.address.city, data[120].contact.address, $.data[120], /data/120",
        "en": "Examples: contact.address.city, data[120].contact.address, $.data[120], /data/120",
    },
    "cli.hint.candidate": {"uz": "{path} ({items} ta element)", "en": "{path} ({items} items)"},
    "cli.hint.candidate.one": {"uz": "{path} ({items} ta element)", "en": "{path} ({items} item)"},
    "cli.hint.jmespath": {
        "uz": "JMESPath misoli: contact.address.city == 'Toshkent' && length(children) > `2` (matn '...' ichida, "
              "son `...` ichida; PowerShell'da butun ifodani bitta tirnoqqa oling).",
        "en": "JMESPath example: contact.address.city == 'Toshkent' && length(children) > `2` (texts in '...', "
              "numbers in `...`; in PowerShell put the whole expression in single quotes).",
    },
    "cli.warn.partial": {
        "uz": "Faylda JSON xatosi bor ({line}-qator, {column}-ustun): faqat xatogacha bo'lgan {count} ta yozuv "
              "ishlatiladi.",
        "en": "The file has a JSON error (line {line}, column {column}): only the {count} records before it will "
              "be used.",
    },
    "cli.warn.partial.one": {
        "uz": "Faylda JSON xatosi bor ({line}-qator, {column}-ustun): faqat xatogacha bo'lgan {count} ta yozuv "
              "ishlatiladi.",
        "en": "The file has a JSON error (line {line}, column {column}): only the {count} record before it will "
              "be used.",
    },
    "cli.warn.broken_lines": {
        "uz": "{count} ta buzuq qator o'tkazib yuborildi (qatorlar: {lines}).",
        "en": "{count} broken lines were skipped (lines {lines}).",
    },
    "cli.warn.broken_lines.one": {
        "uz": "{count} ta buzuq qator o'tkazib yuborildi ({lines}-qator).",
        "en": "{count} broken line was skipped (line {lines}).",
    },
    "cli.warn.memory_limit_min": {
        "uz": "--memory-limit {value} juda kichik: {minimum} ishlatiladi.",
        "en": "--memory-limit {value} is too small: {minimum} is used instead.",
    },
    "cli.warn.memory_limit_cap": {
        "uz": "--memory-limit {value}: «{setting}» sozlamasi ({limit}) tartiblashga ko'pi bilan {cap} beradi; "
              "{cap} ishlatiladi.",
        "en": "--memory-limit {value}: the \"{setting}\" setting ({limit}) allows at most {cap} for sorting; "
              "{cap} is used.",
    },
    "cli.warn.memory_peak": {
        "uz": "Dastur {used} gacha xotira ishlatdi: bu «{setting}» sozlamasidan ({limit}) ko'p. Kamroq xotira uchun "
              "«{large_setting}» sozlamasini ({large}) kamaytiring yoki «{setting}» qiymatini oshiring.",
        "en": "The program used up to {used} of memory, more than the \"{setting}\" setting ({limit}). To use less, "
              "lower the \"{large_setting}\" setting ({large}), or raise \"{setting}\".",
    },
    "cli.warn.memory_peak_sort": {
        "uz": "Dastur {used} gacha xotira ishlatdi: bu «{setting}» sozlamasidan ({limit}) ko'p. Kamroq xotira uchun "
              "--memory-limit qiymatini yoki «{large_setting}» sozlamasini ({large}) kamaytiring yoki «{setting}» "
              "qiymatini oshiring.",
        "en": "The program used up to {used} of memory, more than the \"{setting}\" setting ({limit}). To use less, "
              "lower --memory-limit or the \"{large_setting}\" setting ({large}), or raise \"{setting}\".",
    },
    "cli.warn.jmespath_quoted": {
        "uz": "JMESPath'da \"{name}\" maydon nomi deb o'qiladi; matn bo'lsa, bitta tirnoqda yozing: '{name}'.",
        "en": "In JMESPath \"{name}\" is read as a field name; for a text use single quotes: '{name}'.",
    },
    "cli.preset.saved": {"uz": "Filtr saqlandi: {name}", "en": "Filter saved: {name}"},
    "cli.preset.deleted": {"uz": "Filtr o'chirildi: {name}", "en": "Filter deleted: {name}"},
    "cli.preset.empty": {
        "uz": "Saqlangan filtrlar yo'q. Saqlash uchun: jsonreshape filter INPUT OUTPUT --where \"...\" "
              "--save-preset NAME",
        "en": "There are no saved filters. To save one: jsonreshape filter INPUT OUTPUT --where \"...\" "
              "--save-preset NAME",
    },
    "cli.preset.line": {"uz": "{name}  [{lang}]  {text}", "en": "{name}  [{lang}]  {text}"},
    "cli.warn.ignored_options": {
        "uz": "{options}: {format} formatida ishlatilmaydi, e'tiborga olinmadi.",
        "en": "{options}: not used by the {format} format, ignored.",
    },
    "cli.warn.backend_loaded": {
        "uz": "Bu jarayonda yadro allaqachon {active} bilan yuklangan; --backend {requested} faqat yangi "
              "ishga tushirishda ta'sir qiladi.",
        "en": "The core of this process is already loaded with {active}; --backend {requested} only takes effect "
              "in a new process.",
    },
    # progress and summaries
    "cli.stage.preparing": {"uz": "Tayyorlanmoqda", "en": "Preparing"},
    "cli.stage.indexing": {"uz": "Indekslanmoqda", "en": "Indexing"},
    "cli.stage.filtering": {"uz": "Filtrlanmoqda", "en": "Filtering"},
    "cli.stage.sorting": {"uz": "Tartiblanmoqda", "en": "Sorting"},
    "cli.stage.writing": {"uz": "Yozilmoqda", "en": "Writing"},
    "cli.stage.searching": {"uz": "Qidirilmoqda", "en": "Searching"},
    "cli.stage.schema": {"uz": "Tuzilma aniqlanmoqda", "en": "Analysing the structure"},
    "cli.progress.line": {
        "uz": "{stage}: {percent}  {done} / {total}  {rate}/s  qoldi {eta}",
        "en": "{stage}: {percent}  {done} / {total}  {rate}/s  {eta} left",
    },
    "cli.progress.line_unknown": {"uz": "{stage}: {done}  {rate}/s", "en": "{stage}: {done}  {rate}/s"},
    "cli.summary.export": {
        "uz": "{selected} / {total} yozuv -> {output} ({size}, {seconds})",
        "en": "{selected} / {total} records -> {output} ({size}, {seconds})",
    },
    "cli.summary.export.one": {
        "uz": "{selected} / {total} yozuv -> {output} ({size}, {seconds})",
        "en": "{selected} / {total} record -> {output} ({size}, {seconds})",
    },
    "cli.summary.rows": {"uz": "{rows} ta qator", "en": "{rows} rows"},
    "cli.summary.rows.one": {"uz": "{rows} ta qator", "en": "{rows} row"},
    "cli.summary.files": {"uz": "{count} ta fayl", "en": "{count} files"},
    "cli.summary.files.one": {"uz": "{count} ta fayl", "en": "{count} file"},
    "cli.count.hits": {"uz": "{count} ta moslik", "en": "{count} hits"},
    "cli.count.hits.one": {"uz": "{count} ta moslik", "en": "{count} hit"},
    "cli.count.records": {"uz": "{count} ta yozuv", "en": "{count} records"},
    "cli.count.records.one": {"uz": "{count} ta yozuv", "en": "{count} record"},
    "cli.search.count": {"uz": "{hits}, {records}da", "en": "{hits} in {records}"},
    "cli.search.summary": {
        "uz": "{hits}, {records}da; {size} ko'rib chiqildi: {seconds}, {rate} MB/s, {mode}.",
        "en": "{hits} in {records}; {size} searched in {seconds} ({rate} MB/s, {mode}).",
    },
    "cli.search.more": {
        "uz": "Faqat dastlabki {shown} ta moslik ko'rsatildi (jami {total} ta); ko'proq uchun {option} yoki "
              "--export FAYL.",
        "en": "Only the first {shown} hits are shown (of {total}); use {option} or --export FILE for more.",
    },
    "cli.search.more.one": {
        "uz": "Faqat birinchi moslik ko'rsatildi (jami {total} ta); ko'proq uchun {option} yoki --export FAYL.",
        "en": "Only the first hit is shown (of {total}); use {option} or --export FILE for more.",
    },
    "cli.search.more_export": {
        "uz": "Faqat dastlabki {shown} ta moslik ko'rsatildi (jami {total} ta); ko'proq ko'rsatish uchun {option}.",
        "en": "Only the first {shown} hits are shown (of {total}); use {option} to show more.",
    },
    "cli.search.more_export.one": {
        "uz": "Faqat birinchi moslik ko'rsatildi (jami {total} ta); ko'proq ko'rsatish uchun {option}.",
        "en": "Only the first hit is shown (of {total}); use {option} to show more.",
    },
    "cli.search.exported": {"uz": "{count} ta moslik yozildi: {path}", "en": "{count} hits written to {path}"},
    "cli.search.exported.one": {"uz": "{count} ta moslik yozildi: {path}", "en": "{count} hit written to {path}"},
    # inspect report
    "cli.inspect.file": {"uz": "Fayl", "en": "File"},
    "cli.inspect.size": {"uz": "Hajm", "en": "Size"},
    "cli.inspect.size_value": {"uz": "{size} ({bytes} bayt)", "en": "{size} ({bytes} bytes)"},
    "cli.inspect.size_value.one": {"uz": "{size} ({bytes} bayt)", "en": "{size} ({bytes} byte)"},
    "cli.inspect.data_size": {"uz": "ochilgandan keyin {size}", "en": "{size} decoded"},
    "cli.inspect.encoding": {"uz": "Kodirovka", "en": "Encoding"},
    "cli.inspect.format": {"uz": "Format", "en": "Format"},
    "cli.inspect.format_value": {"uz": "{label} ({mode})", "en": "{label} ({mode})"},
    "cli.inspect.auto": {"uz": "avtomatik", "en": "detected"},
    "cli.inspect.forced": {"uz": "majburiy", "en": "forced"},
    "cli.inspect.record_path": {"uz": "Yozuvlar yo'li", "en": "Record path"},
    "cli.inspect.candidates": {"uz": "Nomzod massivlar", "en": "Candidate arrays"},
    "cli.inspect.wrapper": {"uz": "Tashqi o'rama", "en": "Wrapper"},
    "cli.inspect.wrapper_records": {"uz": "[yozuvlar: {count} ta]", "en": "[records: {count}]"},
    "cli.inspect.wrapper_unread": {
        "uz": "yozuvlardan oldin {before}, keyin {after} (ko'rsatilmadi)",
        "en": "{before} before the records, {after} after them (not shown)",
    },
    "cli.inspect.candidate": {"uz": "{path}: {items} ta element ({kind})", "en": "{path}: {items} items ({kind})"},
    "cli.inspect.candidate.one": {"uz": "{path}: {items} ta element ({kind})", "en": "{path}: {items} item ({kind})"},
    "cli.inspect.items.objects": {"uz": "obyektlar", "en": "objects"},
    "cli.inspect.items.arrays": {"uz": "massivlar", "en": "arrays"},
    "cli.inspect.items.mixed": {"uz": "aralash", "en": "mixed"},
    "cli.inspect.items.values": {"uz": "oddiy qiymatlar", "en": "plain values"},
    "cli.inspect.items.empty": {"uz": "bo'sh", "en": "empty"},
    "cli.inspect.records": {"uz": "Yozuvlar soni", "en": "Records"},
    "cli.inspect.records_partial": {"uz": "{count} (xatogacha)", "en": "{count} (before the error)"},
    "cli.inspect.index": {"uz": "Indeks", "en": "Index"},
    "cli.inspect.index_built": {"uz": "{seconds} s, {rate} MB/s", "en": "{seconds} s, {rate} MB/s"},
    "cli.inspect.index_cached": {"uz": "keshdan", "en": "from cache"},
    "cli.inspect.backend": {"uz": "Yadro", "en": "Core"},
    "cli.inspect.strings": {"uz": "Satrlarda", "en": "Strings"},
    "cli.inspect.strings_value": {
        "uz": "\\ bilan yozilgan belgilar: {escapes}; \\uXXXX kodlari: {u_escapes}; ASCII bo'lmagan matn: {non_ascii}",
        "en": "escapes: {escapes}; unicode escapes: {u_escapes}; non-ASCII text: {non_ascii}",
    },
    "cli.inspect.error": {"uz": "Xato", "en": "Error"},
    "cli.inspect.error_records": {
        "uz": "Xatogacha {count} ta yozuv o'qildi.",
        "en": "{count} records were read before the error.",
    },
    "cli.inspect.error_records.one": {
        "uz": "Xatogacha {count} ta yozuv o'qildi.",
        "en": "{count} record was read before the error.",
    },
    "cli.inspect.broken": {"uz": "Buzuq qatorlar", "en": "Broken lines"},
    "cli.inspect.broken_value": {"uz": "{count} ta", "en": "{count}"},
    "cli.inspect.broken_line": {"uz": "{message}: {preview}", "en": "{message}: {preview}"},
    "cli.inspect.warnings": {"uz": "Ogohlantirishlar", "en": "Warnings"},
    # ---- gui.common ----
    "gui.common.ok": {"uz": "OK", "en": "OK"},
    "gui.common.cancel": {"uz": "Bekor qilish", "en": "Cancel"},
    "gui.common.close": {"uz": "Yopish", "en": "Close"},
    "gui.common.copy": {"uz": "Nusxalash", "en": "Copy"},
    "gui.common.yes": {"uz": "Ha", "en": "Yes"},
    "gui.common.no": {"uz": "Yo'q", "en": "No"},
    "gui.common.loading": {"uz": "Yuklanmoqda…", "en": "Loading…"},
    "gui.common.confirm_title": {"uz": "Tasdiqlash", "en": "Confirm"},
    # ---- gui.app ----
    "gui.app.no_qt": {
        "uz": "Grafik interfeys uchun PySide6 o'rnatilmagan.",
        "en": "PySide6 is not installed, so the graphical interface cannot start.",
    },
    "gui.app.no_qt.hint": {
        "uz": "O'rnatish: pip install \"jsonreshape[gui]\" (yoki pip install PySide6).",
        "en": "Install it with: pip install \"jsonreshape[gui]\" (or pip install PySide6).",
    },
    # ---- gui.grid ----
    "gui.grid.empty": {"uz": "Ko'rsatadigan ma'lumot yo'q", "en": "Nothing to show"},
    "gui.grid.sort.asc": {"uz": "O'sish bo'yicha tartiblangan", "en": "Sorted ascending"},
    "gui.grid.sort.desc": {"uz": "Kamayish bo'yicha tartiblangan", "en": "Sorted descending"},
    "gui.grid.sort.key": {"uz": "{n}-kalit", "en": "key {n}"},
    # ---- gui.tasks ----
    "gui.tasks.state.running": {"uz": "Bajarilmoqda", "en": "Running"},
    "gui.tasks.state.done": {"uz": "Tugadi", "en": "Finished"},
    "gui.tasks.state.failed": {"uz": "Xato bilan tugadi", "en": "Failed"},
    "gui.tasks.state.cancelled": {"uz": "Bekor qilindi", "en": "Cancelled"},
    "gui.tasks.queued": {
        "uz": "Navbatda: boshqa og'ir vazifa tugashini kutmoqda",
        "en": "Queued: waiting for another heavy task to finish",
    },
    # ---- gui.context ----
    "gui.context.task.state": {"uz": "Interfeys holati saqlanmoqda", "en": "Saving the interface state"},
    # ---- gui.session ----
    "gui.session.task.open": {"uz": "Ochilmoqda: {name}", "en": "Opening {name}"},
    "gui.session.task.index": {"uz": "Indekslanmoqda: {name}", "en": "Indexing {name}"},
    "gui.session.task.filter": {"uz": "Filtrlanmoqda: {name}", "en": "Filtering {name}"},
    "gui.session.task.sort": {"uz": "Tartiblanmoqda: {name}", "en": "Sorting {name}"},
    "gui.session.task.locate": {"uz": "Yozuv o'rni topilmoqda: {name}", "en": "Locating a record in {name}"},
    "gui.session.task.check": {
        "uz": "Fayl o'zgargani tekshirilmoqda: {name}",
        "en": "Checking {name} for changes",
    },
    "gui.session.label.all": {"uz": "Barcha yozuvlar", "en": "All records"},
    "gui.session.label.filter": {"uz": "Filtr: {expr}", "en": "Filter: {expr}"},
    "gui.session.label.sort": {"uz": "Tartib: {keys}", "en": "Sorted by {keys}"},
    "gui.session.single_no_view": {
        "uz": "Bu fayl bitta JSON qiymatidan iborat, unda yozuvlar ro'yxati yo'q: filtr va tartiblash qo'llanmaydi.",
        "en": "This file is a single JSON value without a list of records, so filtering and sorting do not apply.",
    },
    "gui.session.single_no_view.hint": {
        "uz": "Ichidagi massivni yozuvlar sifatida ko'rish uchun yozuvlar yo'lini tanlang.",
        "en": "Choose a record path to view an array inside it as records.",
    },
    "gui.session.not_open": {"uz": "{name} fayli hali ochilmagan.", "en": "{name} is not open yet."},
    "gui.session.record_not_found": {
        "uz": "#{record} yozuv yo'q: faylda {count} ta yozuv bor (raqamlar #0 dan boshlanadi).",
        "en": "There is no record #{record}: the file has {count} records (numbers start at #0).",
    },
    "gui.session.record_not_found_partial": {
        "uz": "#{record} yozuv hozircha yo'q: fayl hali indekslanmoqda, hozircha {count} ta yozuv topildi "
        "(raqamlar #0 dan boshlanadi). Indekslash tugagach yana urinib ko'ring.",
        "en": "There is no record #{record} yet: the file is still being indexed, {count} records were found so "
        "far (numbers start at #0). Try again when indexing has finished.",
    },
    "gui.session.record_not_found_stopped": {
        "uz": "#{record} yozuv topilmadi: indekslash to'xtatilgan, faqat {count} ta yozuv ma'lum (raqamlar #0 dan "
        "boshlanadi). Barcha yozuvlarni ko'rish uchun faylni qayta oching (F5).",
        "en": "Record #{record} was not found: indexing was stopped and only {count} records are known (numbers "
        "start at #0). Reopen the file (F5) to see all records.",
    },
    "gui.session.view_cancelled": {
        "uz": "Filtr yoki tartiblash bekor qilindi: avvalgi ko'rinish qaytarildi ({label}).",
        "en": "The filter or sort was cancelled: the previous view is shown again ({label}).",
    },
    # ---- gui.state ----
    "gui.state.busy": {
        "uz": "Interfeys holati fayli ({path}) boshqa oyna tomonidan band qilingan.",
        "en": "The interface state file ({path}) is in use by another window.",
    },
    "gui.state.busy.hint": {
        "uz": "Birozdan keyin yana urinib ko'ring.",
        "en": "Try again in a moment.",
    },
    # ---- gui.copy ----
    "gui.copy.style.dotted": {"uz": "Nuqtali", "en": "Dotted"},
    "gui.copy.style.jsonpath": {"uz": "JSONPath", "en": "JSONPath"},
    "gui.copy.style.pointer": {"uz": "JSON Pointer", "en": "JSON Pointer"},
    "gui.copy.style.python": {"uz": "Python", "en": "Python"},
    "gui.copy.style.js": {"uz": "JavaScript", "en": "JavaScript"},
    "gui.copy.format.json": {"uz": "JSON (chiroyli)", "en": "JSON (pretty)"},
    "gui.copy.format.json_compact": {"uz": "JSON (ixcham)", "en": "JSON (compact)"},
    "gui.copy.format.python": {"uz": "Python", "en": "Python"},
    "gui.copy.format.csv": {"uz": "CSV qatori", "en": "CSV row"},
    "gui.copy.format.csv_header": {"uz": "CSV (sarlavha bilan)", "en": "CSV (with header)"},
    "gui.copy.task": {"uz": "Nusxalash: {format}", "en": "Copying: {format}"},
    "gui.copy.too_large": {
        "uz": "Qiymat nusxalash uchun juda katta ({size}).",
        "en": "The value is too large to copy ({size}).",
    },
    "gui.copy.too_large.hint": {
        "uz": "Uni eksport qiling (Ctrl+E) yoki daraxtda ochib, kichikroq qismlarini nusxalang.",
        "en": "Export it (Ctrl+E), or expand it in the tree and copy smaller parts.",
    },
    # ---- gui.fields ----
    "gui.fields.task.sample": {"uz": "Tuzilma aniqlanmoqda (namuna): {name}", "en": "Inferring the structure (sample): {name}"},
    "gui.fields.task.full": {"uz": "Tuzilma aniqlanmoqda (to'liq): {name}", "en": "Inferring the structure (full scan): {name}"},
    # ---- gui.actions ----
    "gui.actions.copy": {"uz": "Nusxalash", "en": "Copy"},
    "gui.actions.copy_value": {"uz": "Qiymat: {format}", "en": "Value: {format}"},
    "gui.actions.copy_path": {"uz": "Yo'l ({style}): {path}", "en": "Path ({style}): {path}"},
    "gui.actions.copy_path_style": {"uz": "Yo'l: {style}", "en": "Path: {style}"},
    "gui.actions.copy_key": {"uz": "Kalit: {key}", "en": "Key: {key}"},
    "gui.actions.copy_key_plain": {"uz": "Kalit", "en": "Key"},
    "gui.actions.root": {"uz": "(ildiz)", "en": "(root)"},
    "gui.actions.no_key": {"uz": "Ildiz tugunning kaliti yo'q.", "en": "The root node has no key."},
    "gui.actions.no_path": {
        "uz": "Bu tugunning yo'lini aniqlab bo'lmadi.",
        "en": "The path of this node could not be determined.",
    },
    "gui.actions.no_file": {"uz": "Hujjat fayli yo'q.", "en": "The document has no file."},
    "gui.actions.filter": {"uz": "Shu qiymat bo'yicha filtr", "en": "Filter by this value"},
    "gui.actions.filter.need_record": {
        "uz": "Faqat yozuvlar ichidagi tugunlar uchun ishlaydi.",
        "en": "Works only for nodes inside the records.",
    },
    "gui.actions.filter.too_large_tip": {
        "uz": "Qiymat filtr uchun juda katta ({size}).",
        "en": "The value is too large for a filter ({size}).",
    },
    "gui.actions.filter_too_large": {
        "uz": "Qiymat filtr uchun juda katta ({size}).",
        "en": "The value is too large for a filter ({size}).",
    },
    "gui.actions.filter_too_large.hint": {
        "uz": "Ichidagi kichikroq maydonni tanlang yoki filtr panelida ifoda yozing (Ctrl+Shift+L).",
        "en": "Choose a smaller field inside it, or write an expression in the filter panel (Ctrl+Shift+L).",
    },
    "gui.actions.task.filter": {"uz": "Filtr uchun qiymat o'qilmoqda", "en": "Reading the value for the filter"},
    "gui.actions.sort": {"uz": "Shu maydon bo'yicha tartiblash", "en": "Sort by this field"},
    "gui.actions.sort.asc": {"uz": "O'sish tartibida ↑", "en": "Ascending ↑"},
    "gui.actions.sort.desc": {"uz": "Kamayish tartibida ↓", "en": "Descending ↓"},
    "gui.actions.sort.need_field": {
        "uz": "Faqat yozuvlar ichidagi maydonlar uchun (yozuvning o'zi uchun emas).",
        "en": "Works only for fields inside the records (not for a whole record).",
    },
    "gui.actions.full_value": {"uz": "To'liq ko'rish", "en": "Show the full value"},
    "gui.actions.show_in_tree": {"uz": "Daraxtda ko'rsatish", "en": "Show in the tree"},
    "gui.actions.show_in_tree.already": {
        "uz": "Bu tugun daraxtda allaqachon tanlangan.",
        "en": "This node is already selected in the tree.",
    },
    "gui.actions.open_new_tab": {"uz": "Yangi tabda ochish", "en": "Open in a new tab"},
    # ---- gui.widgets ----
    "gui.widgets.task.load_value": {"uz": "Qiymat o'qilmoqda", "en": "Reading a value"},
    "gui.widgets.error.title": {"uz": "Xato", "en": "Error"},
    "gui.widgets.error.unexpected": {"uz": "Kutilmagan xato yuz berdi.", "en": "An unexpected error occurred."},
    "gui.widgets.error.unexpected.hint": {
        "uz": "Bu dasturdagi nosozlik bo'lishi mumkin. Tafsilotlar log faylida: {log}",
        "en": "This may be a bug in the program. The details are in the log file: {log}",
    },
    "gui.widgets.error.location": {
        "uz": "Joyi: {line}-qator, {column}-ustun, {offset}-bayt",
        "en": "Location: line {line}, column {column}, byte {offset}",
    },
    "gui.widgets.error.source": {"uz": "Fayl: {path}", "en": "File: {path}"},
    "gui.widgets.error.context": {"uz": "Xato atrofidagi matn:", "en": "Text around the error:"},
    "gui.widgets.error.details": {"uz": "Tafsilotlar", "en": "Details"},
    "gui.widgets.error.open_log": {"uz": "Logni ochish", "en": "Open log"},
    "gui.widgets.error.open_log.tip": {"uz": "Log fayllari papkasini ochadi", "en": "Opens the folder of the log files"},
    "gui.widgets.error.copy.tip": {
        "uz": "Xato haqidagi hamma ma'lumotni nusxalaydi",
        "en": "Copies everything known about the error",
    },
    "gui.widgets.full.title": {"uz": "{path} — to'liq qiymat", "en": "{path} — full value"},
    "gui.widgets.full.info": {"uz": "{path} · {size}", "en": "{path} · {size}"},
    "gui.widgets.full.truncated": {
        "uz": "Qiymat juda katta: faqat boshidagi {shown} ko'rsatildi (tugun hajmi {total}).",
        "en": "The value is too large: only the first {shown} are shown (node size {total}).",
    },
    "gui.widgets.full.wrap": {"uz": "Qatorlarni o'rash", "en": "Wrap lines"},
    "gui.widgets.full.copy.tip": {
        "uz": "Yuklangan matnni nusxalaydi (qisqartirib ko'rsatilgan uzun qatorlar to'liq)",
        "en": "Copies the loaded text (long lines in full, even where they are shown shortened)",
    },
    "gui.widgets.full.long_lines": {
        "uz": "{count} ta juda uzun qator {chars} belgigacha qisqartirib ko'rsatildi; nusxalash to'liq matnni oladi.",
        "en": "{count} very long lines are shown shortened to {chars} characters; copying takes the full text.",
    },
    "gui.widgets.error.more.title": {"uz": "Boshqa xatolar", "en": "More errors"},
    "gui.widgets.error.more": {
        "uz": "Yana {count} ta xato oyna ochilmasdan logga yozildi. Tafsilotlar log faylida.",
        "en": "{count} more errors were written to the log without opening a window. See the log for details.",
    },
    # ---- gui.tree ----
    "gui.tree.col.key": {"uz": "Kalit", "en": "Key"},
    "gui.tree.col.value": {"uz": "Qiymat", "en": "Value"},
    "gui.tree.col.children": {"uz": "Bolalar", "en": "Children"},
    "gui.tree.col.size": {"uz": "Hajm", "en": "Size"},
    "gui.tree.col.offset": {"uz": "Bayt o'rni", "en": "Offset"},
    "gui.tree.wrapper": {"uz": "(o'rama)", "en": "(wrapper)"},
    "gui.tree.wrapper.tip": {
        "uz": "Yozuvlar massivini o'rab turgan tashqi obyekt. Yozuvlarning o'zi undan keyingi qatorlarda.",
        "en": "The outer object around the record array. The records themselves are the rows after it.",
    },
    "gui.tree.record.tip": {"uz": "Yozuv {record}", "en": "Record {record}"},
    "gui.tree.records_below": {"uz": "yozuvlar quyida", "en": "records are shown below"},
    "gui.tree.records.tip": {
        "uz": "Bu massiv yozuvlarni saqlaydi: ular daraxtda o'rama ostidagi alohida qatorlar bo'lib ko'rsatiladi.",
        "en": "This array holds the records: the tree shows them as separate rows below the wrapper.",
    },
    "gui.tree.count.unknown.tip": {
        "uz": "Bolalar soni hali noma'lum: tugun ochilganda sanaladi.",
        "en": "The number of children is not known yet: it is counted when the node is expanded.",
    },
    "gui.tree.size.tip": {
        "uz": "{bytes} bayt\nFayldagi joyi: {start} – {end} bayt",
        "en": "{bytes} bytes\nPosition in the file: bytes {start} – {end}",
    },
    "gui.tree.load_failed": {"uz": "O'qib bo'lmadi: {error}", "en": "Could not read: {error}"},
    "gui.tree.placeholder.opening": {"uz": "Fayl ochilmoqda…", "en": "Opening the file…"},
    "gui.tree.placeholder.empty": {"uz": "Bu ko'rinishda yozuv yo'q", "en": "There are no records in this view"},
    "gui.tree.placeholder.failed": {"uz": "Faylni ochib bo'lmadi", "en": "The file could not be opened"},
    "gui.tree.task.children": {"uz": "Bolalar indekslanmoqda: {path}", "en": "Indexing children: {path}"},
    "gui.tree.task.sort_keys": {"uz": "Kalitlar tartiblanmoqda: {path}", "en": "Sorting keys: {path}"},
    "gui.tree.task.sort_elements": {"uz": "Elementlar tartiblanmoqda: {path}", "en": "Sorting elements: {path}"},
    "gui.tree.task.sorting": {"uz": "Tartiblanmoqda…", "en": "Sorting…"},
    "gui.tree.task.expand": {"uz": "Ochiladigan qatorlar sanalmoqda: {path}", "en": "Counting the rows to expand: {path}"},
    "gui.tree.task.reveal": {"uz": "Tugun daraxtda qidirilmoqda", "en": "Finding the node in the tree"},
    "gui.tree.task.locate": {"uz": "Yozuvlarning ko'rinishdagi o'rni aniqlanmoqda",
                             "en": "Finding the records in the view"},
    "gui.tree.task.find": {"uz": "Yuklangan qatorlardan qidirilmoqda", "en": "Searching the loaded rows"},
    "gui.tree.task.fields": {"uz": "Maydon nomlari o'qilmoqda", "en": "Reading the field names"},
    "gui.tree.all_objects": {"uz": "ochiq obyektlar", "en": "expanded objects"},
    "gui.tree.top_level": {"uz": "yuqori daraja", "en": "top level"},
    "gui.tree.expand_confirm": {
        "uz": "{rows} ta qator ochiladi. Davom etilsinmi?",
        "en": "{rows} rows will be expanded. Continue?",
    },
    "gui.tree.expand_confirm.title": {"uz": "Ko'p qator", "en": "Many rows"},
    "gui.tree.too_many": {
        "uz": "Ochib bo'lmaydi: {rows} tadan ortiq qator chiqadi. Kichikroq tugunni yoki kamroq darajani tanlang.",
        "en": "Cannot expand: it would show more than {rows} rows. Choose a smaller node or fewer levels.",
    },
    "gui.tree.not_in_view": {"uz": "Bu yozuv joriy ko'rinishda yo'q", "en": "This record is not in the current view"},
    "gui.tree.reveal_failed": {"uz": "Tugunni daraxtda ko'rsatib bo'lmadi", "en": "Could not show the node in the tree"},
    "gui.tree.path_copied": {"uz": "Yo'l nusxalandi", "en": "Path copied"},
    "gui.tree.crumb.copy": {"uz": "Nusxalash", "en": "Copy"},
    "gui.tree.crumb.copy.tip": {"uz": "Joriy tugun yo'lini nusxalash (Ctrl+Shift+C)",
                                "en": "Copy the path of the current node (Ctrl+Shift+C)"},
    "gui.tree.crumb.hidden": {"uz": "Yo'lning yashirilgan qismlari", "en": "Hidden parts of the path"},
    "gui.tree.menu.expand_all": {"uz": "Hammasini ochish", "en": "Expand all"},
    "gui.tree.menu.collapse_all": {"uz": "Hammasini yopish", "en": "Collapse all"},
    "gui.tree.menu.expand_level": {"uz": "N-darajagacha ochish", "en": "Expand to level"},
    "gui.tree.menu.level": {"uz": "{n}-darajagacha", "en": "Level {n}"},
    "gui.tree.menu.sort_children": {"uz": "Bolalarni tartiblash", "en": "Sort children"},
    "gui.tree.menu.sort_keys": {"uz": "Kalitlarni alifbo bo'yicha", "en": "Keys alphabetically"},
    "gui.tree.menu.sort_keys.tip": {
        "uz": "Hamma obyektlarda kalitlar alifbo tartibida ko'rsatiladi. Faqat ko'rinish: fayl o'zgarmaydi.",
        "en": "Every object shows its keys in alphabetical order. View only: the file is not changed.",
    },
    "gui.tree.menu.sort_elements": {"uz": "Elementlarni maydon bo'yicha tartiblash…",
                                    "en": "Sort the elements by a field…"},
    "gui.tree.menu.original_order": {"uz": "Asl tartib", "en": "Original order"},
    "gui.tree.menu.need_container": {"uz": "Faqat obyekt yoki massiv uchun", "en": "Only for objects and arrays"},
    "gui.tree.menu.need_array": {"uz": "Faqat massiv uchun", "en": "Only for arrays"},
    "gui.tree.menu.not_sorted": {"uz": "Bu massiv tartiblanmagan", "en": "This array is not sorted"},
    "gui.tree.menu.records_inline": {
        "uz": "Yozuvlar o'rama ostidagi qatorlarda ko'rsatiladi",
        "en": "The records are shown as the rows below the wrapper",
    },
    "gui.tree.sortdlg.title": {"uz": "Elementlarni tartiblash", "en": "Sort elements"},
    "gui.tree.sortdlg.field": {"uz": "Maydon:", "en": "Field:"},
    "gui.tree.sortdlg.field.tip": {
        "uz": "Har bir element ichidagi yo'l, masalan name yoki contact.city; $ — elementning o'zi",
        "en": "A path inside each element, e.g. name or contact.city; $ is the element itself",
    },
    "gui.tree.sortdlg.asc": {"uz": "O'sish tartibida", "en": "Ascending"},
    "gui.tree.sortdlg.desc": {"uz": "Kamayish tartibida", "en": "Descending"},
    "gui.tree.sortdlg.note": {
        "uz": "Faqat ko'rinish uchun: fayl va elementlarning [indeks]lari o'zgarmaydi. "
              "null va yo'q qiymatlar oxirida turadi.",
        "en": "View only: the file and the [index] of each element stay the same. "
              "null and missing values come last.",
    },
    # ---- gui.quickfind ----
    "gui.quickfind.label": {"uz": "Topish:", "en": "Find:"},
    "gui.quickfind.placeholder": {"uz": "Yuklangan qismdan topish", "en": "Find in the loaded part"},
    "gui.quickfind.case": {"uz": "Aa", "en": "Aa"},
    "gui.quickfind.case.tip": {"uz": "Katta-kichik harfni farqlash", "en": "Match case"},
    "gui.quickfind.prev.tip": {"uz": "Oldingi moslik (Shift+F3)", "en": "Previous match (Shift+F3)"},
    "gui.quickfind.next.tip": {"uz": "Keyingi moslik (F3)", "en": "Next match (F3)"},
    "gui.quickfind.close.tip": {"uz": "Yopish (Esc)", "en": "Close (Esc)"},
    "gui.quickfind.hint": {
        "uz": "Faqat yuklangan qismda. Butun fayl bo'yicha: Ctrl+Shift+F",
        "en": "Loaded part only. Whole file: Ctrl+Shift+F",
    },
    "gui.quickfind.none": {"uz": "Topilmadi", "en": "Not found"},
    "gui.quickfind.count": {"uz": "{total} ta", "en": "{total} found"},
    "gui.quickfind.position": {"uz": "{index} / {total}", "en": "{index} of {total}"},
    # ---- gui.table ----
    "gui.table.busy": {"uz": "⟳ Ko'rinish yangilanmoqda…", "en": "⟳ Updating the view…"},
    "gui.table.col.key": {"uz": "Kalit", "en": "Key"},
    "gui.table.col.index": {"uz": "Raqam", "en": "Number"},
    "gui.table.col.record": {"uz": "Yozuv", "en": "Record"},
    "gui.table.columns": {"uz": "Ustunlar…", "en": "Columns…"},
    "gui.table.columns.tip": {
        "uz": "Ustunlarni tanlash, tartiblash va istalgan yo'lni ustun qilib qo'shish",
        "en": "Choose and order the columns, add any path as a column",
    },
    "gui.table.columns.loading": {
        "uz": "Ustunlar fayl tuzilmasidan olinmoqda…",
        "en": "Taking the columns from the file's structure…",
    },
    "gui.table.load_failed": {"uz": "Yozuvni o'qib bo'lmadi ({error})", "en": "The record could not be read ({error})"},
    "gui.table.placeholder.opening": {"uz": "Fayl ochilmoqda…", "en": "Opening the file…"},
    "gui.table.placeholder.failed": {"uz": "Fayl ochilmadi", "en": "The file could not be opened"},
    "gui.table.placeholder.indexing": {"uz": "Yozuvlar o'qilmoqda…", "en": "Reading the records…"},
    "gui.table.placeholder.busy": {
        "uz": "Yozuvlar tartiblanmoqda yoki filtrlanmoqda…",
        "en": "Sorting or filtering the records…",
    },
    "gui.table.placeholder.no_match": {"uz": "Filtrga mos yozuv topilmadi", "en": "No record matches the filter"},
    "gui.table.placeholder.empty": {"uz": "Ko'rsatadigan yozuv yo'q", "en": "No records to show"},
    "gui.table.single_no_view": {
        "uz": "Fayl bitta JSON qiymatdan iborat: tartiblash va filtr yozuvlar ro'yxatida ishlaydi",
        "en": "The file is a single JSON value: sorting and filtering work on lists of records",
    },
    "gui.table.sort.tip": {
        "uz": "Bosing — tartiblash (yana bosing — kamayish bo'yicha, uchinchi marta — bekor). "
              "Shift+bosish — qo'shimcha tartiblash kaliti",
        "en": "Click: sort (again: descending, a third time: off). Shift+click: an additional sort key",
    },
    "gui.table.sort.no_descendants": {
        "uz": "Bu ustun bo'yicha tartiblab bo'lmaydi: ** (barcha darajalar) yo'li tartiblash kaliti bo'la "
              "olmaydi",
        "en": "This column cannot be sorted: a ** (all levels) path cannot be a sort key",
    },
    "gui.table.find.hint": {
        "uz": "Faqat yuklangan qatorlarda. Ustun bo'yicha filtr: Alt+↓. Butun fayl bo'yicha: Ctrl+Shift+F",
        "en": "Loaded rows only. Filter by a column: Alt+↓. Whole file: Ctrl+Shift+F",
    },
    "gui.table.find.task": {"uz": "Jadvaldan topilmoqda", "en": "Finding in the table"},
    "gui.table.qf.row": {"uz": "Tezkor filtr", "en": "Quick filter"},
    "gui.table.qf.row.tip": {
        "uz": "Ustunlar ostidagi filtr qatorini ko'rsatish yoki yashirish (Alt+↓ — joriy ustun filtriga o'tish)",
        "en": "Show or hide the filter row under the columns (Alt+↓ jumps to the current column's filter)",
    },
    "gui.table.qf.placeholder": {"uz": "filtr", "en": "filter"},
    "gui.table.qf.accessible": {"uz": "{column} ustunining tezkor filtri", "en": "Quick filter of the column {column}"},
    "gui.table.qf.clear_all": {"uz": "Filtrlarni tozalash", "en": "Clear filters"},
    "gui.table.qf.clear_all_n": {"uz": "Filtrlarni tozalash ({count})", "en": "Clear filters ({count})"},
    "gui.table.qf.clear_all.tip": {
        "uz": "Barcha ustunlarning tezkor filtrlarini olib tashlaydi",
        "en": "Removes the quick filters of all columns",
    },
    "gui.table.qf.help": {
        "uz": "Tezkor filtr (Enter bosilganda yoki yozib bo'lgach qo'llanadi):\n"
              "matn — shu matn bor (katta-kichik harf farqsiz);  !matn — yo'q\n"
              "=qiymat, !=qiymat — teng / teng emas (=12, =\"12\", =true, =null, =Toshkent)\n"
              ">5  >=5  <5  <=5 — son bilan solishtirish (matn qo'shtirnoqda: >=\"2020-01-01\")\n"
              "5..10 — oraliq (5.. va ..10 ham);  ~regex — muntazam ifoda, !~regex — mos emas\n"
              "null, !null, exists, missing, empty, !empty\n"
              "Esc — tozalash",
        "en": "Quick filter (applied on Enter or after you stop typing):\n"
              "text — contains the text (case-insensitive);  !text — does not contain it\n"
              "=value, !=value — equals / differs (=12, =\"12\", =true, =null, =Tashkent)\n"
              ">5  >=5  <5  <=5 — compares with a number (texts in quotes: >=\"2020-01-01\")\n"
              "5..10 — range (also 5.. and ..10);  ~regex — regular expression, !~regex — no match\n"
              "null, !null, exists, missing, empty, !empty\n"
              "Esc — clear",
    },
    "gui.table.qf.bad_column": {"uz": "Ustun yo'li noto'g'ri: {path}", "en": "Invalid column path: {path}"},
    "gui.table.qf.need_value": {"uz": "«{op}» dan keyin qiymat yozing", "en": "Type a value after “{op}”"},
    "gui.table.qf.need_number": {
        "uz": "«{op}» dan keyin son yoki qo'shtirnoq ichidagi matn yozing ({op}5 yoki {op}\"abc\"), «{value}» emas",
        "en": "After “{op}” type a number or a quoted text ({op}5 or {op}\"abc\"), not “{value}”",
    },
    "gui.table.qf.no_objects": {
        "uz": "Obyekt bilan solishtirib bo'lmaydi: son, matn, true/false/null yoki massiv yozing",
        "en": "Cannot compare with an object: type a number, a text, true/false/null or an array",
    },
    "gui.table.qf.range_types": {
        "uz": "Oraliqning ikkala chegarasi bir turda bo'lsin: ikkalasi son yoki ikkalasi qo'shtirnoqdagi matn",
        "en": "Both bounds of a range must have the same type: two numbers or two quoted texts",
    },
    "gui.table.qf.invalid": {"uz": "Tezkor filtr noto'g'ri: {detail}", "en": "Invalid quick filter: {detail}"},
    "gui.table.copy.done": {
        "uz": "Nusxalandi: {rows} ta qator, {cells} ta katak",
        "en": "Copied {rows} rows, {cells} cells",
    },
    "gui.table.copy.task": {"uz": "Jadvaldan nusxalash ({count} ta qator)", "en": "Copying from the table ({count} rows)"},
    "gui.table.copy.no_value": {"uz": "Bu katakda qiymat yo'q", "en": "This cell has no value"},
    "gui.table.copy.confirm": {
        "uz": "{count} ta qator nusxalanadi. Bu ko'p vaqt va xotira olishi mumkin. Davom etilsinmi?",
        "en": "{count} rows will be copied. This may take a lot of time and memory. Continue?",
    },
    "gui.table.copy.too_many": {
        "uz": "Bir martada ko'pi bilan {limit} ta qator nusxalanadi, tanlangani esa {count} ta.",
        "en": "At most {limit} rows can be copied at once; {count} are selected.",
    },
    "gui.table.copy.too_many.hint": {
        "uz": "Katta qismlarni eksport qiling (Ctrl+E).",
        "en": "Export large parts instead (Ctrl+E).",
    },
    "gui.table.menu.copy_cell": {"uz": "Katakni nusxalash", "en": "Copy cell"},
    "gui.table.menu.copy_tsv": {
        "uz": "Tanlanganlarni nusxalash (TSV, Excel uchun)",
        "en": "Copy selection (TSV, for Excel)",
    },
    "gui.table.menu.copy_headers": {"uz": "Sarlavhalar bilan nusxalash", "en": "Copy with headers"},
    "gui.table.menu.freeze": {"uz": "Birinchi ustunni qotirish", "en": "Freeze first column"},
    "gui.table.menu.filter_row": {"uz": "Tezkor filtr qatori", "en": "Quick filter row"},
    "gui.table.menu.reset_columns": {"uz": "Standart ustunlar", "en": "Default columns"},
    "gui.table.menu.sort_asc": {"uz": "O'sish bo'yicha tartiblash", "en": "Sort ascending"},
    "gui.table.menu.sort_desc": {"uz": "Kamayish bo'yicha tartiblash", "en": "Sort descending"},
    "gui.table.menu.sort_clear": {"uz": "Tartiblashni bekor qilish", "en": "Remove sorting"},
    "gui.table.menu.hide": {"uz": "Ustunni yashirish", "en": "Hide column"},
    "gui.table.menu.remove": {"uz": "Ustunni olib tashlash", "en": "Remove column"},
    "gui.table.menu.fit": {"uz": "Kenglikni moslash", "en": "Fit width"},
    "gui.table.menu.show_hidden": {"uz": "Yashirilgan ustunlar", "en": "Hidden columns"},
    "gui.table.chooser.title": {"uz": "Jadval ustunlari", "en": "Table columns"},
    "gui.table.chooser.search": {"uz": "Maydonni qidirish", "en": "Search fields"},
    "gui.table.chooser.up": {"uz": "Yuqoriga", "en": "Move up"},
    "gui.table.chooser.down": {"uz": "Pastga", "en": "Move down"},
    "gui.table.chooser.check_all": {"uz": "Hammasini belgilash", "en": "Check all"},
    "gui.table.chooser.check_none": {"uz": "Belgilarni olib tashlash", "en": "Uncheck all"},
    "gui.table.chooser.reset": {"uz": "Standart", "en": "Default"},
    "gui.table.chooser.reset.tip": {
        "uz": "Fayl tuzilmasidan olingan standart ustunlar",
        "en": "The default columns taken from the file's structure",
    },
    "gui.table.chooser.custom.placeholder": {
        "uz": "Yo'l yozing, masalan contact.address.city yoki orders[*].id",
        "en": "Type a path, e.g. contact.address.city or orders[*].id",
    },
    "gui.table.chooser.custom.tip": {
        "uz": "Istalgan yo'lni ustun qilib qo'shish ($key — kalit, $index — yozuv raqami, $ — butun yozuv)",
        "en": "Add any path as a column ($key — the key, $index — the record number, $ — the whole record)",
    },
    "gui.table.chooser.custom.add": {"uz": "Qo'shish", "en": "Add"},
    "gui.table.chooser.empty_path": {"uz": "Yo'l yozilmagan", "en": "No path given"},
    "gui.table.chooser.loading": {"uz": "Maydonlar ro'yxati tayyorlanmoqda…", "en": "Collecting the fields…"},
    "gui.table.chooser.no_schema": {
        "uz": "Maydonlar ro'yxatini tayyorlab bo'lmadi: yo'lni pastda o'zingiz yozing",
        "en": "The field list could not be collected: type a path below",
    },
    "gui.table.chooser.not_in_schema": {"uz": "namunada uchramadi", "en": "not seen in the sample"},
    "gui.table.chooser.note": {
        "uz": "{count} ta maydon ({records} ta yozuv tahlil qilingan). Belgilanganlar jadvalda ko'rinadi; "
              "tartibni sudrab yoki tugmalar bilan o'zgartiring.",
        "en": "{count} fields ({records} records analysed). Checked fields are shown; "
              "drag them or use the buttons to change the order.",
    },
    # ---- gui.text ----
    "gui.text.indent": {"uz": "Chekinish:", "en": "Indent:"},
    "gui.text.indent.tip": {
        "uz": "Chiroyli formatdagi bir pog'ona surilish kengligi",
        "en": "Indentation of one nesting level in the pretty text",
    },
    "gui.text.indent.tab": {"uz": "Tab", "en": "Tab"},
    "gui.text.wrap": {"uz": "Qatorlarni o'rash", "en": "Wrap lines"},
    "gui.text.wrap.tip": {
        "uz": "Uzun qatorlarni oyna kengligida keyingi qatorga o'tkazish",
        "en": "Continue long lines on the next row at the window width",
    },
    "gui.text.fold": {"uz": "Yig'ish", "en": "Fold"},
    "gui.text.fold.tip": {
        "uz": "Obyekt va massivlarni yig'ish yoki yoyish (qator raqami yonidagi ▸ / ▾ ham bosiladi)",
        "en": "Fold or unfold objects and arrays (the ▸ / ▾ next to the line numbers can be clicked too)",
    },
    "gui.text.fold_all": {"uz": "Hammasini yig'ish", "en": "Fold all"},
    "gui.text.unfold_all": {"uz": "Hammasini yoyish", "en": "Unfold all"},
    "gui.text.fold_level": {"uz": "{level}-darajagacha yig'ish", "en": "Fold to level {level}"},
    "gui.text.copy.tip": {
        "uz": "Belgilangan matnni, belgilanmagan bo'lsa butun matnni nusxalaydi",
        "en": "Copies the selected text, or the whole text when nothing is selected",
    },
    "gui.text.menu.copy": {"uz": "Nusxalash", "en": "Copy"},
    "gui.text.menu.copy_all": {"uz": "Butun matnni nusxalash", "en": "Copy the whole text"},
    "gui.text.menu.select_all": {"uz": "Hammasini belgilash", "en": "Select all"},
    "gui.text.menu.node": {"uz": "Tugun", "en": "Node"},
    "gui.text.task.format": {"uz": "Tugun matni tayyorlanmoqda", "en": "Preparing the node text"},
    "gui.text.task.find": {"uz": "Matndan qidirilmoqda", "en": "Searching the text"},
    "gui.text.empty": {"uz": "Daraxt yoki jadvalda tugun tanlang", "en": "Select a node in the tree or the table"},
    "gui.text.load_failed": {"uz": "Tugunni ko'rsatib bo'lmadi: {message}", "en": "The node cannot be shown: {message}"},
    "gui.text.virtual": {
        "uz": "Bu butun fayl ({count} ta yozuv): matn ko'rinishi uchun juda katta. Yozuvni tanlang yoki xom "
              "ko'rinishda oching.",
        "en": "This is the whole file ({count} records), too large for the text view. Select a record, or open "
              "it in the raw view.",
    },
    "gui.text.open_raw": {"uz": "Xom ko'rinishda ochish", "en": "Open in the raw view"},
    "gui.text.too_large": {
        "uz": "Tugun juda katta ({size}): matnda faqat boshidagi {shown} ko'rsatildi.",
        "en": "The node is too large ({size}): only the first {shown} are shown as text.",
    },
    "gui.text.json_error": {
        "uz": "Tugun ichida JSON xatosi bor, xatogacha bo'lgan qism ko'rsatildi: {message}",
        "en": "The node contains a JSON error; the part before the error is shown: {message}",
    },
    "gui.text.goto_error": {"uz": "Xatoni xom ko'rinishda ochish", "en": "Show the error in the raw view"},
    "gui.text.long_lines": {
        "uz": "{count} ta juda uzun qator {chars} belgidan keyin qisqartirib ko'rsatildi (nusxalashda to'liq "
              "olinadi).",
        "en": "{count} very long lines are shortened after {chars} characters on screen (copying takes them in "
              "full).",
    },
    "gui.text.size": {"uz": "{kind} · {size}", "en": "{kind} · {size}"},
    "gui.text.kind.object": {"uz": "obyekt", "en": "object"},
    "gui.text.kind.array": {"uz": "massiv", "en": "array"},
    "gui.text.kind.string": {"uz": "matn", "en": "string"},
    "gui.text.kind.number": {"uz": "son", "en": "number"},
    "gui.text.kind.bool": {"uz": "mantiqiy", "en": "boolean"},
    "gui.text.kind.null": {"uz": "null", "en": "null"},
    "gui.text.loading_progress": {"uz": "Matn yuklanmoqda… {percent}%", "en": "Loading the text… {percent}%"},
    "gui.text.status": {
        "uz": "Qator {line}, ustun {column} · jami {lines} qator",
        "en": "Line {line}, column {column} · {lines} lines",
    },
    # ---- gui.raw ----
    "gui.raw.offset.label": {"uz": "Bayt o'rni:", "en": "Offset:"},
    "gui.raw.offset.placeholder": {"uz": "123456 yoki 0x1E240", "en": "123456 or 0x1E240"},
    "gui.raw.offset.tip": {
        "uz": "Bayt o'rniga o'tish: baytning fayldagi o'rni, o'nlik yoki 0x bilan 16-lik son (Enter)",
        "en": "Go to offset: the byte position in the file, decimal or hexadecimal with 0x (Enter)",
    },
    "gui.raw.line.label": {"uz": "Qator:", "en": "Line:"},
    "gui.raw.line.placeholder": {"uz": "qator raqami", "en": "line number"},
    "gui.raw.line.tip": {
        "uz": "Qatorga o'tish (1 dan boshlanadi; kerak bo'lsa avval qatorlar sanaladi) (Enter)",
        "en": "Go to line (starts at 1; the lines are counted first when needed) (Enter)",
    },
    "gui.raw.hex": {"uz": "16-lik (hex)", "en": "Hex"},
    "gui.raw.hex.tip": {"uz": "Bayt o'rinlarini 16-lik sanoqda ko'rsatish", "en": "Show offsets in hexadecimal"},
    "gui.raw.lines": {"uz": "Qatorlarni sanash", "en": "Count lines"},
    "gui.raw.lines.tip": {
        "uz": "Fayldagi qatorlarni fonda sanaydi: shundan keyin qator raqamlari ko'rinadi va qatorga tez o'tiladi",
        "en": "Counts the lines of the file in the background: then line numbers are shown and going to a line "
              "is fast",
    },
    "gui.raw.goto_error": {"uz": "Xatoga o'tish", "en": "Go to the error"},
    "gui.raw.goto_error.tip": {"uz": "JSON xatosi joylashgan baytga o'tish", "en": "Go to the byte of the JSON error"},
    "gui.raw.prepared": {
        "uz": "Ko'rsatilayotgan: tayyorlangan UTF-8 nusxa",
        "en": "Showing: the prepared UTF-8 copy",
    },
    "gui.raw.prepared.tip": {
        "uz": "Manba fayl ({encoding}, siqish: {compression}) o'qish uchun UTF-8 nusxaga o'girilgan; bayt "
              "o'rinlari va qatorlar shu nusxaga tegishli.",
        "en": "The source file ({encoding}, compression: {compression}) was converted to a UTF-8 copy for reading; "
              "offsets and lines refer to that copy.",
    },
    "gui.raw.no_doc": {"uz": "Fayl hali ochilmagan", "en": "The file is not open yet"},
    "gui.raw.empty": {"uz": "Fayl bo'sh", "en": "The file is empty"},
    "gui.raw.read_failed.short": {"uz": "Faylni o'qib bo'lmadi", "en": "The file could not be read"},
    "gui.raw.read_failed": {"uz": "Faylni o'qib bo'lmadi: {message}", "en": "The file could not be read: {message}"},
    "gui.raw.bad_offset": {
        "uz": "Bayt o'rni noto'g'ri: «{text}». O'nlik (123456) yoki 16-lik (0x1E240) son yozing.",
        "en": "Invalid offset: \"{text}\". Type a decimal (123456) or hexadecimal (0x1E240) number.",
    },
    "gui.raw.offset_past_end": {
        "uz": "Bayt o'rni {offset} fayl oxiridan keyin keladi (fayl hajmi {size} bayt): oxirgi baytga o'tildi.",
        "en": "{offset} is past the end of the file ({size} bytes): went to the last byte.",
    },
    "gui.raw.bad_line": {
        "uz": "Qator raqami noto'g'ri: «{text}». 1 yoki undan katta butun son yozing.",
        "en": "Invalid line number: \"{text}\". Type a whole number of 1 or more.",
    },
    "gui.raw.line_past_end": {
        "uz": "Faylda {count} ta qator bor: {line}-qator yo'q.",
        "en": "The file has {count} lines: there is no line {line}.",
    },
    "gui.raw.counting": {
        "uz": "{line}-qatorni topish uchun qatorlar sanalmoqda… {percent}%",
        "en": "Counting lines to find line {line}… {percent}%",
    },
    "gui.raw.task.read": {"uz": "Xom baytlar o'qilmoqda", "en": "Reading raw bytes"},
    "gui.raw.task.node": {"uz": "Tugun o'rni aniqlanmoqda", "en": "Locating the node"},
    "gui.raw.task.copy": {"uz": "Belgilangan baytlar nusxalanmoqda", "en": "Copying the selected bytes"},
    "gui.raw.task.lineindex": {"uz": "Qatorlar sanalmoqda: {name}", "en": "Counting lines: {name}"},
    "gui.raw.position": {"uz": "Bayt o'rni {offset} / {size} ({percent}%)", "en": "Offset {offset} / {size} ({percent}%)"},
    "gui.raw.position.line": {"uz": "{line}-qator", "en": "line {line}"},
    "gui.raw.copy_capped": {
        "uz": "Belgilangan qism juda katta: faqat boshidagi {size} nusxalandi.",
        "en": "The selection is too large: only its first {size} were copied.",
    },
    "gui.raw.menu.copy": {"uz": "Nusxalash", "en": "Copy"},
    "gui.raw.menu.copy_offset": {"uz": "Bayt o'rnini nusxalash: {offset}", "en": "Copy the offset: {offset}"},
    # ---- gui.schema ----
    "gui.schema.col.field": {"uz": "Maydon", "en": "Field"},
    "gui.schema.col.types": {"uz": "Turlar", "en": "Types"},
    "gui.schema.col.presence": {"uz": "Mavjudlik", "en": "Presence"},
    "gui.schema.col.count": {"uz": "Soni", "en": "Count"},
    "gui.schema.col.examples": {"uz": "Misollar", "en": "Examples"},
    "gui.schema.col.details": {"uz": "Tafsilotlar", "en": "Details"},
    "gui.schema.detail.str_len": {"uz": "uzunligi {range}", "en": "length {range}"},
    "gui.schema.detail.num_range": {"uz": "qiymatlar {range}", "en": "values {range}"},
    "gui.schema.detail.arr_len": {"uz": "elementlar {range}", "en": "items {range}"},
    "gui.schema.detail.obj_keys": {"uz": "kalitlar {range}", "en": "keys {range}"},
    "gui.schema.tip.pooled": {
        "uz": "Bu obyektning kalitlari juda ko'p va xilma-xil (masalan, id yoki sanalar), shuning uchun ular bitta * maydoniga birlashtirildi.",
        "en": "This object has many different keys (e.g. ids or dates), so they are pooled into one * field.",
    },
    "gui.schema.tip.key": {
        "uz": "Yozuvning kaliti (kalitli obyektdagi a'zo nomi).",
        "en": "The record's key (the member name in a keyed object).",
    },
    "gui.schema.tip.type": {"uz": "{type}: {count} ta ({pct})", "en": "{type}: {count} ({pct})"},
    "gui.schema.tip.context_records": {"uz": "yozuvlar", "en": "records"},
    "gui.schema.tip.context_elements": {"uz": "massiv elementlari", "en": "array elements"},
    "gui.schema.tip.presence": {"uz": "Bor: {present} / {base} ({context})", "en": "Present: {present} of {base} ({context})"},
    "gui.schema.tip.count": {"uz": "{count} ta qiymat, {records} ta yozuvda", "en": "{count} values in {records} records"},
    "gui.schema.tip.sort_first": {
        "uz": "Massiv ichidagi maydon: har bir yozuv birinchi elementdagi qiymati bo'yicha tartiblanadi.",
        "en": "A field inside an array: each record is sorted by the value in its first element.",
    },
    "gui.schema.not_loaded": {"uz": "Tuzilma hali aniqlanmagan.", "en": "The structure has not been inferred yet."},
    "gui.schema.base_note": {
        "uz": "Tuzilma hujjatning barcha yozuvlari bo'yicha aniqlanadi: joriy filtr va tartib hisobga olinmaydi.",
        "en": "The structure describes all records of the document; the current filter and sort are not taken into account.",
    },
    "gui.schema.full_scan": {"uz": "To'liq skanerlash", "en": "Full scan"},
    "gui.schema.full_scan.tip": {
        "uz": "Barcha yozuvlarni fonda tahlil qiladi (vazifalar panelida bekor qilish mumkin).",
        "en": "Analyses every record in the background (it can be cancelled in the tasks panel).",
    },
    "gui.schema.full_scan.running": {"uz": "Skanerlanmoqda…", "en": "Scanning…"},
    "gui.schema.full_scan.done_tip": {
        "uz": "Barcha yozuvlar allaqachon tahlil qilingan.",
        "en": "Every record has already been analysed.",
    },
    "gui.schema.search.placeholder": {"uz": "Maydonni qidirish", "en": "Find a field"},
    "gui.schema.act.add_column": {"uz": "Jadvalga ustun qilib qo'shish", "en": "Add as a table column"},
    "gui.schema.act.filter_exists": {"uz": "Filtr: mavjud", "en": "Filter: exists"},
    "gui.schema.act.filter_missing": {"uz": "Filtr: mavjud emas", "en": "Filter: missing"},
    "gui.schema.act.sort_asc": {"uz": "Tartiblash ↑", "en": "Sort ↑"},
    "gui.schema.act.sort_desc": {"uz": "Tartiblash ↓", "en": "Sort ↓"},
    "gui.schema.act.copy_path": {"uz": "Yo'lni nusxalash", "en": "Copy path"},
    "gui.schema.act.filter_builder": {"uz": "Filtr quruvchiga qo'shish", "en": "Add to the filter builder"},
    "gui.schema.retry": {"uz": "Qayta urinish", "en": "Try again"},
    "gui.schema.waiting_open": {"uz": "Fayl ochilmoqda…", "en": "Opening the file…"},
    "gui.schema.unavailable": {
        "uz": "Fayl ochilmadi, shuning uchun tuzilmani aniqlab bo'lmaydi.",
        "en": "The file could not be opened, so its structure is unavailable.",
    },
    "gui.schema.waiting_index": {
        "uz": "Fayl indekslanmoqda; tuzilma u tugagach aniqlanadi.",
        "en": "The file is being indexed; the structure will be inferred when it is done.",
    },
    "gui.schema.loading": {
        "uz": "Tuzilma aniqlanmoqda ({count} ta yozuvdan namuna)…",
        "en": "Inferring the structure (a sample of {count} records)…",
    },
    "gui.schema.filling": {"uz": "Tuzilma ko'rsatilmoqda… {percent}%", "en": "Showing the structure… {percent}%"},
    "gui.schema.cancelled": {"uz": "Tuzilmani aniqlash bekor qilindi.", "en": "Inferring the structure was cancelled."},
    "gui.schema.failed": {"uz": "Tuzilmani aniqlab bo'lmadi.", "en": "The structure could not be inferred."},
    "gui.schema.no_fields": {"uz": "Yozuvlarda maydonlar yo'q.", "en": "The records have no fields."},
    "gui.schema.header.full": {
        "uz": "To'liq: {scanned} ta yozuv · {fields} ta maydon · {seconds} s",
        "en": "Full scan: {scanned} records · {fields} fields · {seconds} s",
    },
    "gui.schema.header.sample": {
        "uz": "{scanned} ta yozuvdan namuna (jami {total}) · {fields} ta maydon · {seconds} s",
        "en": "Sample of {scanned} records (of {total}) · {fields} fields · {seconds} s",
    },
    "gui.schema.header.skipped": {"uz": "{count} ta yozuv tahlil qilinmadi", "en": "{count} records skipped"},
    "gui.schema.header.truncated": {
        "uz": "maydonlar chegarasiga yetildi: hammasi ko'rsatilmagan",
        "en": "field limit reached: not every field is shown",
    },
    "gui.schema.header.scanning": {"uz": "to'liq skanerlash davom etmoqda", "en": "full scan in progress"},
    "gui.schema.why.no_field": {"uz": "Avval maydonni tanlang.", "en": "Select a field first."},
    "gui.schema.why.pooled_column": {
        "uz": "Birlashtirilgan kalitlar (*) ustun bo'la olmaydi.",
        "en": "Pooled keys (*) cannot be a column.",
    },
    "gui.schema.why.always_present": {
        "uz": "Bu maydon har bir yozuvda bor, bunday filtrning ma'nosi yo'q.",
        "en": "Every record has this field, so such a filter would be meaningless.",
    },
    "gui.schema.why.pooled_sort": {
        "uz": "Birlashtirilgan kalitlar (*) bo'yicha tartiblashning ma'nosi yo'q.",
        "en": "Sorting by pooled keys (*) is meaningless.",
    },
    "gui.schema.original_order": {"uz": "Asl tartib", "en": "Original order"},
    # ---- gui.filter ----
    "gui.filter.builder": {"uz": "Quruvchi", "en": "Builder"},
    "gui.filter.expression": {"uz": "Ifoda", "en": "Expression"},
    "gui.filter.lang.expr": {"uz": "Ifoda", "en": "Expression"},
    "gui.filter.lang.jmespath": {"uz": "JMESPath", "en": "JMESPath"},
    "gui.filter.lang.tip": {
        "uz": "Filtr tili: oddiy ifoda yoki mutaxassislar uchun JMESPath",
        "en": "Filter language: a simple expression, or JMESPath for experts",
    },
    "gui.filter.placeholder.expr": {
        "uz": "Masalan: contact.address.city == \"Toshkent\" and len(children) > 2",
        "en": "For example: contact.address.city == \"Toshkent\" and len(children) > 2",
    },
    "gui.filter.placeholder.jmespath": {
        "uz": "Masalan: age > `30` && contains(tags, 'vip')",
        "en": "For example: age > `30` && contains(tags, 'vip')",
    },
    "gui.filter.value.placeholder": {"uz": "qiymat", "en": "value"},
    "gui.filter.value.force_string": {
        "uz": "Qiymatni har doim matn (satr) deb olish: 12 emas, \"12\"",
        "en": "Always read the value as text (a string): \"12\" instead of 12",
    },
    "gui.filter.value.read_as": {"uz": "Turi: {type} — {value}", "en": "Read as {type}: {value}"},
    "gui.filter.field.placeholder": {"uz": "maydon", "en": "field"},
    "gui.filter.field.tip": {
        "uz": "Maydon yo'li: contact.city, orders[*].id, $key (yozuv kaliti), $index (yozuv raqami)",
        "en": "Field path: contact.city, orders[*].id, $key (record key), $index (record number)",
    },
    "gui.filter.between_and": {"uz": "va", "en": "and"},
    "gui.filter.remove_condition": {"uz": "Shartni olib tashlash", "en": "Remove the condition"},
    "gui.filter.remove_group": {"uz": "Guruhni olib tashlash", "en": "Remove the group"},
    "gui.filter.add_condition": {"uz": "+ Shart", "en": "+ Condition"},
    "gui.filter.add_group": {"uz": "+ Guruh", "en": "+ Group"},
    "gui.filter.group.and": {"uz": "VA (hammasi)", "en": "AND (all)"},
    "gui.filter.group.or": {"uz": "YOKI (birortasi)", "en": "OR (any)"},
    "gui.filter.group.op_tip": {
        "uz": "VA: barcha shartlar bajarilsin; YOKI: kamida bittasi bajarilsin",
        "en": "AND: every condition must hold; OR: at least one must hold",
    },
    "gui.filter.group.not": {"uz": "EMAS", "en": "NOT"},
    "gui.filter.group.not_tip": {"uz": "Guruh natijasini teskarisiga aylantiradi", "en": "Inverts the result of the group"},
    "gui.filter.group.empty": {
        "uz": "Shartlar yo'q: barcha yozuvlar mos keladi.",
        "en": "No conditions: every record matches.",
    },
    "gui.filter.builder_error": {"uz": "Quruvchida xato: {detail}", "en": "Builder error: {detail}"},
    "gui.filter.error_at": {"uz": "{pos}-belgida xato: {detail}", "en": "Error at character {pos}: {detail}"},
    "gui.filter.notice.unrepresentable": {
        "uz": "Bu ifodani quruvchida ko'rsatib bo'lmaydi. Ifodaning o'zi qo'llanadi; quruvchidan foydalanish uchun ifodani tozalang.",
        "en": "This expression cannot be shown in the builder. The expression itself is applied; clear it to use the builder.",
    },
    "gui.filter.notice.invalid": {
        "uz": "Ifodada xato bor: u tuzatilgach quruvchi yangilanadi.",
        "en": "The expression has an error; the builder will follow once it is fixed.",
    },
    "gui.filter.notice.jmespath": {
        "uz": "JMESPath ifodasini quruvchida ko'rsatib bo'lmaydi.",
        "en": "A JMESPath expression cannot be shown in the builder.",
    },
    "gui.filter.presets": {"uz": "Saqlanganlar:", "en": "Saved:"},
    "gui.filter.preset.none": {"uz": "Saqlangan filtrni tanlang", "en": "Choose a saved filter"},
    "gui.filter.preset.load": {"uz": "Ochish", "en": "Load"},
    "gui.filter.preset.load_tip": {"uz": "Tanlangan filtrni muharrirga yuklash", "en": "Load the chosen filter into the editor"},
    "gui.filter.preset.save": {"uz": "Saqlash…", "en": "Save…"},
    "gui.filter.preset.save_tip": {"uz": "Joriy ifodani nom bilan saqlash", "en": "Save the current expression under a name"},
    "gui.filter.preset.delete": {"uz": "O'chirish", "en": "Delete"},
    "gui.filter.preset.delete_tip": {"uz": "Tanlangan saqlangan filtrni o'chirish", "en": "Delete the chosen saved filter"},
    "gui.filter.preset.save_title": {"uz": "Filtrni saqlash", "en": "Save the filter"},
    "gui.filter.preset.name": {"uz": "Nomi:", "en": "Name:"},
    "gui.filter.preset.overwrite": {
        "uz": "\"{name}\" nomli filtr bor. Uni almashtirasizmi?",
        "en": "A filter named \"{name}\" already exists. Replace it?",
    },
    "gui.filter.preset.delete_confirm": {"uz": "\"{name}\" filtri o'chirilsinmi?", "en": "Delete the filter \"{name}\"?"},
    "gui.filter.preset.loaded": {"uz": "\"{name}\" yuklandi (hali qo'llanmagan).", "en": "\"{name}\" loaded (not applied yet)."},
    "gui.filter.preset.saved": {"uz": "\"{name}\" saqlandi.", "en": "\"{name}\" saved."},
    "gui.filter.preset.deleted": {"uz": "\"{name}\" o'chirildi.", "en": "\"{name}\" deleted."},
    "gui.filter.preset.working": {
        "uz": "Saqlangan filtrlar fayli bilan ishlanmoqda…",
        "en": "Working with the saved filters file…",
    },
    "gui.filter.task.presets": {"uz": "Saqlangan filtrlar", "en": "Saved filters"},
    "gui.filter.apply": {"uz": "Qo'llash", "en": "Apply"},
    "gui.filter.apply_tip": {"uz": "Filtrni qo'llash (Enter, Ctrl+Enter)", "en": "Apply the filter (Enter, Ctrl+Enter)"},
    "gui.filter.clear": {"uz": "Tozalash", "en": "Clear"},
    "gui.filter.clear_tip": {
        "uz": "Quruvchi va ifodani tozalab, filtrni olib tashlash",
        "en": "Clear the builder and the expression and remove the filter",
    },
    "gui.filter.load_applied": {"uz": "Qo'llanganini yuklash", "en": "Load the applied filter"},
    "gui.filter.load_applied_tip": {
        "uz": "Muharrirga hozir qo'llanib turgan filtrni qaytarish",
        "en": "Put the filter that is applied now back into the editor",
    },
    "gui.filter.no_file": {
        "uz": "Fayl ochilmagan. Filtrlash uchun avval fayl oching.",
        "en": "No file is open. Open a file to filter it.",
    },
    "gui.filter.single": {
        "uz": "Bu fayl bitta JSON qiymatidan iborat: filtrlanadigan yozuvlar yo'q. Ichidagi massivni yozuvlar sifatida ko'rish uchun yozuvlar yo'lini tanlang.",
        "en": "This file is a single JSON value, so there are no records to filter. Choose a record path to view an array inside it as records.",
    },
    "gui.filter.status.running": {
        "uz": "Filtrlanmoqda… hozircha {count} ta yozuv topildi",
        "en": "Filtering… {count} records found so far",
    },
    "gui.filter.status.result": {"uz": "Natija: {total} ta yozuvdan {count} tasi", "en": "Result: {count} of {total} records"},
    "gui.filter.status.none": {"uz": "Filtr yo'q: {total} ta yozuv", "en": "No filter: {total} records"},
    "gui.filter.status.result_shown": {
        "uz": "Natija: {source} — {total} ta yozuvdan {count} tasi",
        "en": "Result: {count} of the {total} records of {source}",
    },
    "gui.filter.status.none_shown": {
        "uz": "Filtr yo'q: {source} — {total} ta yozuv",
        "en": "No filter: {source}, {total} records",
    },
    "gui.filter.status.shown_records": {"uz": "ko'rsatilgan yozuvlar", "en": "the shown records"},
    "gui.filter.status.pending": {"uz": "o'zgarishlar hali qo'llanmagan", "en": "changes not applied yet"},
    "gui.filter.status.builder_error": {
        "uz": "Quruvchidagi xatoni tuzating (qizil bilan belgilangan shart).",
        "en": "Fix the error in the builder (the condition marked in red).",
    },
    "gui.filter.status.invalid": {
        "uz": "Ifodada xato bor: filtr qo'llanmadi.",
        "en": "The expression has an error; the filter was not applied.",
    },
    "gui.filter.cannot_add": {
        "uz": "Shartni qo'shib bo'lmadi: quruvchi hozir faqat o'qish uchun (ifodani tahrirlang).",
        "en": "The condition could not be added: the builder is read-only now (edit the expression).",
    },
    # ---- gui.sort ----
    "gui.sort.title": {"uz": "Tartiblash", "en": "Sort"},
    "gui.sort.info": {
        "uz": "Yozuvlar birinchi kalit bo'yicha tartiblanadi, qiymatlari teng bo'lsa keyingi kalit bo'yicha. Tartib barqaror: to'liq teng yozuvlar fayldagi tartibida qoladi.",
        "en": "Records are sorted by the first key, equal values by the next key. The sort is stable: records that are fully equal keep their file order.",
    },
    "gui.sort.single": {
        "uz": "Bu fayl bitta JSON qiymatidan iborat: tartiblanadigan yozuvlar yo'q.",
        "en": "This file is a single JSON value, so there are no records to sort.",
    },
    "gui.sort.col.field": {"uz": "Maydon", "en": "Field"},
    "gui.sort.col.direction": {"uz": "Yo'nalish", "en": "Direction"},
    "gui.sort.col.nulls": {"uz": "null va yo'q qiymatlar", "en": "Null and missing"},
    "gui.sort.col.natural": {"uz": "Sonlar", "en": "Numbers"},
    "gui.sort.col.collation": {"uz": "Alifbo", "en": "Collation"},
    "gui.sort.col.case": {"uz": "Harflar", "en": "Case"},
    "gui.sort.field.placeholder": {"uz": "maydon", "en": "field"},
    "gui.sort.field_tip": {
        "uz": "Maydon yo'li: name, contact.city, orders[*].sum (birinchi element), $key (yozuv kaliti), $index (yozuv raqami)",
        "en": "Field path: name, contact.city, orders[*].sum (the first element), $key (record key), $index (record number)",
    },
    "gui.sort.asc": {"uz": "O'sish ↑", "en": "Ascending ↑"},
    "gui.sort.desc": {"uz": "Kamayish ↓", "en": "Descending ↓"},
    "gui.sort.nulls.last": {"uz": "Oxirida", "en": "Last"},
    "gui.sort.nulls.first": {"uz": "Boshida", "en": "First"},
    "gui.sort.nulls_tip": {
        "uz": "null va maydoni yo'q yozuvlar yo'nalishdan qat'i nazar oxirida yoki boshida turadi",
        "en": "Records with null or without the field go last or first, whatever the direction",
    },
    "gui.sort.natural": {"uz": "Tabiiy (2 < 10)", "en": "Natural (2 < 10)"},
    "gui.sort.natural_tip": {
        "uz": "Matn ichidagi sonlar son sifatida taqqoslanadi: child2 < child10",
        "en": "Numbers inside text compare as numbers: child2 < child10",
    },
    "gui.sort.collation.uz": {"uz": "O'zbek alifbosi", "en": "Uzbek alphabet"},
    "gui.sort.collation.simple": {"uz": "Oddiy (harfma-harf)", "en": "Simple (letter by letter)"},
    "gui.sort.collation.unicode": {"uz": "Unicode kodlari", "en": "Unicode code points"},
    "gui.sort.collation_tip": {
        "uz": "O'zbek alifbosi: a b d e … x y z oʻ gʻ sh ch ng (rasmiy tartib); oddiy: oʻ, sh kabilar alohida harf emas; Unicode: belgi kodlari bo'yicha",
        "en": "Uzbek alphabet: a b d e … x y z oʻ gʻ sh ch ng (the official order); simple: oʻ, sh and the like are not separate letters; Unicode: by code point",
    },
    "gui.sort.case": {"uz": "Katta-kichik", "en": "Match case"},
    "gui.sort.case_tip": {
        "uz": "Katta va kichik harflarni farqlash (a < A < b < B)",
        "en": "Distinguish upper and lower case (a < A < b < B)",
    },
    "gui.sort.up": {"uz": "Yuqoriga (muhimroq kalit)", "en": "Move up (a more important key)"},
    "gui.sort.down": {"uz": "Pastga", "en": "Move down"},
    "gui.sort.remove": {"uz": "Kalitni olib tashlash", "en": "Remove the key"},
    "gui.sort.add": {"uz": "+ Kalit qo'shish", "en": "+ Add a key"},
    "gui.sort.ok": {"uz": "Tartiblash", "en": "Sort"},
    "gui.sort.clear": {"uz": "Tartibni olib tashlash", "en": "Remove the sort"},
    "gui.sort.clear_tip": {"uz": "Yozuvlarni fayldagi tartibiga qaytarish", "en": "Back to the file order"},
    # ---- gui.search ----
    "gui.search.query.name": {"uz": "Qidiruv matni", "en": "Search text"},
    "gui.search.query.tip": {
        "uz": "Butun fayl bo'yicha qidiriladi. Enter — qidirish, Esc — to'xtatish. Ro'yxatda avvalgi qidiruvlar.",
        "en": "Searches the whole file. Enter searches, Esc stops. The list holds earlier searches.",
    },
    "gui.search.query.placeholder.plain": {"uz": "Nimani qidiramiz? (masalan: Toshkent)", "en": "Search for… (e.g. Toshkent)"},
    "gui.search.query.placeholder.regex": {
        "uz": "Regex (masalan: ^AB\\d+$ yoki Tosh(kent|kand))",
        "en": "Regex (e.g. ^AB\\d+$ or Tosh(kent|kand))",
    },
    "gui.search.query.placeholder.number": {
        "uz": "Son sharti: > 1000, <= 5, = 3, 10..20",
        "en": "Number condition: > 1000, <= 5, = 3, 10..20",
    },
    "gui.search.run": {"uz": "Qidirish", "en": "Search"},
    "gui.search.run.tip": {"uz": "Butun faylda qidirish (Enter)", "en": "Search the whole file (Enter)"},
    "gui.search.cancel": {"uz": "To'xtatish", "en": "Stop"},
    "gui.search.cancel.tip": {
        "uz": "Qidiruvni to'xtatish (Esc). Topilganlari ro'yxatda qoladi.",
        "en": "Stop the search (Esc). The matches found so far stay in the list.",
    },
    "gui.search.mode.label": {"uz": "Rejim:", "en": "Mode:"},
    "gui.search.mode.plain": {"uz": "Oddiy matn", "en": "Plain text"},
    "gui.search.mode.regex": {"uz": "Regex", "en": "Regex"},
    "gui.search.mode.number": {"uz": "Son (> 1000)", "en": "Number (> 1000)"},
    "gui.search.mode.tip": {
        "uz": "Oddiy matn: harflar va apostroflar farqlanmaydi. Regex: muntazam ifoda. Son: faqat sonli "
        "qiymatlar shart bo'yicha solishtiriladi.",
        "en": "Plain text: case and apostrophe variants are ignored. Regex: a regular expression. Number: only "
        "number values are compared with the condition.",
    },
    "gui.search.case": {"uz": "Katta-kichik harf", "en": "Match case"},
    "gui.search.case.tip": {
        "uz": "Katta va kichik harflarni farqlash (apostrof turlari baribir bir xil hisoblanadi)",
        "en": "Tell upper and lower case apart (apostrophe variants still count as equal)",
    },
    "gui.search.word": {"uz": "Butun so'z", "en": "Whole word"},
    "gui.search.word.tip": {
        "uz": "Faqat alohida so'z sifatida uchraganini topish",
        "en": "Find it only as a separate word",
    },
    "gui.search.translit": {"uz": "Lotin ↔ kirill", "en": "Latin ↔ Cyrillic"},
    "gui.search.translit.tip": {
        "uz": "\"Toshkent\" qidirilsa \"Тошкент\" ham topiladi (va aksincha)",
        "en": "Searching \"Toshkent\" also finds \"Тошкент\" (and the other way round)",
    },
    "gui.search.translit.plain_only": {
        "uz": "Faqat \"Oddiy matn\" rejimida ishlaydi",
        "en": "Works only in the \"Plain text\" mode",
    },
    "gui.search.number_ignores": {
        "uz": "Son rejimida qo'llanmaydi: sonlar qiymati bo'yicha solishtiriladi",
        "en": "Not used in the number mode: numbers are compared by value",
    },
    "gui.search.scope.label": {"uz": "Qayerda:", "en": "Where:"},
    "gui.search.scope.keys": {"uz": "Kalitlarda", "en": "Keys"},
    "gui.search.scope.values": {"uz": "Qiymatlarda", "en": "Values"},
    "gui.search.scope.both": {"uz": "Ikkalasida", "en": "Both"},
    "gui.search.path.label": {"uz": "Faqat shu yo'lda:", "en": "Only in path:"},
    "gui.search.path.placeholder": {
        "uz": "ixtiyoriy: name, contact.city, **.name, orders[*].sum",
        "en": "optional: name, contact.city, **.name, orders[*].sum",
    },
    "gui.search.path.tip": {
        "uz": "Faqat shu maydon (va uning ichidagilar) qidiriladi. \"**.name\" — istalgan chuqurlikdagi name; "
        "\"orders[*].sum\" — har bir buyurtmaning sum maydoni.",
        "en": "Only this field (and what is inside it) is searched. \"**.name\" is a name at any depth; "
        "\"orders[*].sum\" the sum of every order.",
    },
    "gui.search.path.tip_value": {"uz": "yo'l: {path}", "en": "path: {path}"},
    "gui.search.col.record": {"uz": "Yozuv", "en": "Record"},
    "gui.search.col.path": {"uz": "Yo'l", "en": "Path"},
    "gui.search.col.where": {"uz": "Qayerda", "en": "Where"},
    "gui.search.col.text": {"uz": "Topilgan joy", "en": "Match"},
    "gui.search.empty": {"uz": "Natijalar shu yerda chiqadi", "en": "Results appear here"},
    "gui.search.no_document": {
        "uz": "Qidirish uchun avval fayl oching.",
        "en": "Open a file to search it.",
    },
    "gui.search.view_changed": {
        "uz": "Ko'rinish shu vaqtda o'zgardi (filtr yoki tartib yangilandi).",
        "en": "The view changed meanwhile (the filter or the sort was updated).",
    },
    "gui.search.view_changed.hint": {"uz": "Amalni qaytadan bajaring.", "en": "Run it again."},
    "gui.search.stale": {
        "uz": "Fayl qayta ochildi: qidiruvni qaytadan bajaring.",
        "en": "The file was reopened: run the search again.",
    },
    "gui.search.task": {"uz": "Qidiruv: “{text}” — {name}", "en": "Search: “{text}” in {name}"},
    "gui.search.task.history": {"uz": "Qidiruv tarixi", "en": "Search history"},
    "gui.search.task.open": {
        "uz": "Qidiruv natijasi yozuvlar to'plamiga aylantirilmoqda",
        "en": "Turning the search results into a record set",
    },
    "gui.search.task.export": {"uz": "Qidiruv natijalari saqlanmoqda: {name}", "en": "Saving search results: {name}"},
    "gui.search.progress.snapshot": {"uz": "Joriy ko'rinish nusxalanmoqda", "en": "Copying the current view"},
    "gui.search.source": {"uz": "Qidiruv doirasi: {scope}", "en": "Searching: {scope}"},
    "gui.search.source.changed": {
        "uz": "Qidirilgan: {searched} · ko'rinish o'zgardi, keyingi qidiruv: {live}",
        "en": "Searched: {searched} · the view changed, the next search covers: {live}",
    },
    "gui.search.domain.all": {"uz": "barcha yozuvlar ({count} ta)", "en": "all records ({count})"},
    "gui.search.domain.view": {
        "uz": "joriy ko'rinish — {label} ({count} ta yozuv)",
        "en": "the current view — {label} ({count} records)",
    },
    "gui.search.view_busy": {
        "uz": "Filtr yoki tartiblash hali tugamagan: ko'rinish to'liq bo'lgach qidiring (yoki uni Vazifalar "
        "panelida to'xtating).",
        "en": "The filter or sort has not finished yet: search once the view is complete (or stop it in the "
        "Tasks panel).",
    },
    "gui.search.scope.keys_number": {
        "uz": "Son rejimida faqat qiymatlar solishtiriladi: kalitlarda qidirilmaydi.",
        "en": "The number mode compares values only: keys are not searched.",
    },
    "gui.search.live": {
        "uz": "Qidirilmoqda… {hits} ta moslik, {records} ta yozuvda · {rate} MB/s",
        "en": "Searching… {hits} matches in {records} records · {rate} MB/s",
    },
    "gui.search.cancelled": {
        "uz": "To'xtatildi: hozirgacha {hits} ta moslik, {records} ta yozuvda topildi (to'liq emas).",
        "en": "Stopped: {hits} matches in {records} records found so far (incomplete).",
    },
    "gui.search.failed": {"uz": "Qidiruv xato bilan tugadi: {error}", "en": "The search failed: {error}"},
    "gui.search.truncated_note": {
        "uz": "Ro'yxatda dastlabki {shown} ta moslik ko'rsatiladi; qolganlari faqat sanaladi (eksport hammasini saqlaydi).",
        "en": "The list shows the first {shown} matches; the rest are only counted (export saves all of them).",
    },
    "gui.search.records_label": {
        "uz": "Qidiruv natijasi: “{text}”",
        "en": "Search results: “{text}”",
    },
    "gui.search.open_records.single": {
        "uz": "Fayl bitta JSON qiymatidan iborat: unda yozuvlar to'plami bo'lmaydi.",
        "en": "The file is a single JSON value: it has no records to show as a set.",
    },
    "gui.search.export.rerun_tip": {
        "uz": "Qidiruv to'xtatilgan edi: eksport uni butun manba bo'yicha qaytadan bajaradi va barcha "
        "mosliklarni saqlaydi.",
        "en": "The search was stopped: the export runs it again over the whole source and saves every match.",
    },
    "gui.search.export.rerun_confirm": {
        "uz": "Qidiruv oxirigacha bajarilmagan (to'xtatilgan). Eksport qidiruvni butun manba bo'yicha qaytadan "
        "bajaradi va barcha mosliklarni saqlaydi — bu vaqt olishi mumkin. Davom etilsinmi?",
        "en": "The search did not run to the end (it was stopped). The export runs it again over the whole "
        "source and saves every match, which may take a while. Continue?",
    },
    "gui.search.open_records": {"uz": "Yozuvlar to'plami sifatida ochish", "en": "Open as a record set"},
    "gui.search.open_records.tip": {
        "uz": "Moslik topilgan yozuvlarni ko'rinish sifatida ko'rsatadi (filtr va tartib olib tashlanadi)",
        "en": "Shows the records with matches as the view (the filter and the sort are cleared)",
    },
    "gui.search.open_records.need_search": {"uz": "Avval qidiruvni bajaring.", "en": "Run a search first."},
    "gui.search.open_records.running": {
        "uz": "Qidiruv tugashini kuting (yoki uni to'xtating).",
        "en": "Wait for the search to finish (or stop it).",
    },
    "gui.search.open_records.truncated": {
        "uz": "Qidiruv to'xtatilgan va ro'yxat to'liq emas: qidiruvni oxirigacha bajaring.",
        "en": "The search was stopped and the list is incomplete: run the search to the end.",
    },
    "gui.search.open_records.no_hits": {"uz": "Hech narsa topilmadi.", "en": "Nothing was found."},
    "gui.search.export": {"uz": "Eksport…", "en": "Export…"},
    "gui.search.export.tip": {
        "uz": "Barcha mosliklarni CSV yoki JSON faylga saqlash (ro'yxatda ko'rinmaganlari ham)",
        "en": "Save every match to a CSV or JSON file (also the ones not listed)",
    },
    "gui.search.export.title": {"uz": "Qidiruv natijalarini saqlash", "en": "Save search results"},
    "gui.search.export.filter": {"uz": "CSV (*.csv);;JSON (*.json)", "en": "CSV (*.csv);;JSON (*.json)"},
    "gui.search.export.need_search": {"uz": "Avval qidiruvni bajaring.", "en": "Run a search first."},
    "gui.search.export.no_source": {
        "uz": "Qidiruv ko'rinish nusxalanmasdan oldin to'xtatilgan: qidiruvni qaytadan bajaring.",
        "en": "The search was stopped before the view was copied: run the search again.",
    },
    "gui.search.export.done": {"uz": "{count} ta moslik saqlandi: {path}", "en": "{count} matches saved to {path}"},
    "gui.search.menu.show": {"uz": "Daraxtda ko'rsatish", "en": "Show in the tree"},
    "gui.search.menu.copy_path": {"uz": "Yo'lni nusxalash", "en": "Copy the path"},
    "gui.search.menu.copy_text": {"uz": "Topilgan matnni nusxalash", "en": "Copy the matched text"},
    "gui.search.menu.copy_record": {"uz": "Yozuv raqamini nusxalash", "en": "Copy the record number"},
    "gui.search.menu.copy_rows": {"uz": "Tanlangan qatorlarni nusxalash", "en": "Copy the selected rows"},
    # ---- gui.export ----
    "gui.export.title": {"uz": "Eksport — {name}", "en": "Export — {name}"},
    "gui.export.path.label": {"uz": "Fayl:", "en": "File:"},
    "gui.export.path.placeholder": {"uz": "Natija fayli yo'li", "en": "Path of the result file"},
    "gui.export.browse": {"uz": "Tanlash…", "en": "Browse…"},
    "gui.export.choose.title": {"uz": "Natijani qayerga saqlash", "en": "Where to save the result"},
    "gui.export.format.label": {"uz": "Format:", "en": "Format:"},
    "gui.export.format.json": {"uz": "JSON", "en": "JSON"},
    "gui.export.format.jsonl": {"uz": "JSON Lines (har qatorda bitta yozuv)", "en": "JSON Lines (one record per line)"},
    "gui.export.format.csv": {"uz": "CSV (jadval)", "en": "CSV (table)"},
    "gui.export.format.xlsx": {"uz": "Excel (XLSX)", "en": "Excel (XLSX)"},
    "gui.export.format.sqlite": {"uz": "SQLite ma'lumotlar bazasi", "en": "SQLite database"},
    "gui.export.filter.json": {"uz": "JSON (*.json)", "en": "JSON (*.json)"},
    "gui.export.filter.jsonl": {"uz": "JSON Lines (*.jsonl *.ndjson)", "en": "JSON Lines (*.jsonl *.ndjson)"},
    "gui.export.filter.csv": {"uz": "CSV (*.csv)", "en": "CSV (*.csv)"},
    "gui.export.filter.xlsx": {"uz": "Excel (*.xlsx)", "en": "Excel (*.xlsx)"},
    "gui.export.filter.sqlite": {"uz": "SQLite (*.sqlite *.sqlite3 *.db)", "en": "SQLite (*.sqlite *.sqlite3 *.db)"},
    "gui.export.scope.title": {"uz": "Nimani eksport qilamiz", "en": "What to export"},
    "gui.export.scope.view": {
        "uz": "Joriy ko'rinish: {label} ({count} ta yozuv)",
        "en": "Current view: {label} ({count} records)",
    },
    "gui.export.scope.view_all": {
        "uz": "Joriy ko'rinish: filtr va tartibsiz ({count} ta yozuv)",
        "en": "Current view: no filter or sort ({count} records)",
    },
    "gui.export.scope.all": {"uz": "Barcha yozuvlar ({count} ta)", "en": "All records ({count})"},
    "gui.export.scope.node": {"uz": "Joriy tugun: {path}", "en": "Current node: {path}"},
    "gui.export.scope.node_none": {"uz": "Joriy tugun (tanlanmagan)", "en": "Current node (none selected)"},
    "gui.export.scope.node_none.tip": {
        "uz": "Daraxtda yoki jadvalda tugunni tanlang.",
        "en": "Select a node in the tree or the table.",
    },
    "gui.export.scope.node.tip": {
        "uz": "Faqat tanlangan tugun yoziladi (JSON yoki JSON Lines). Sonlar va matnlar baytma-bayt o'zgarmaydi.",
        "en": "Only the selected node is written (JSON or JSON Lines). Numbers and strings stay byte for byte.",
    },
    "gui.export.scope.single.tip": {
        "uz": "Bu fayl bitta JSON qiymati: u butunligicha yoziladi.",
        "en": "This file is a single JSON value: it is written as a whole.",
    },
    "gui.export.options.title": {"uz": "Sozlamalar", "en": "Options"},
    "gui.export.indent.label": {"uz": "Chekinish:", "en": "Indentation:"},
    "gui.export.indent.2": {"uz": "2 bo'shliq", "en": "2 spaces"},
    "gui.export.indent.4": {"uz": "4 bo'shliq", "en": "4 spaces"},
    "gui.export.indent.tab": {"uz": "Tab", "en": "Tab"},
    "gui.export.indent.compact": {"uz": "Ixcham (bo'shliqlarsiz)", "en": "Compact (no whitespace)"},
    "gui.export.indent.n": {"uz": "{n} bo'shliq", "en": "{n} spaces"},
    "gui.export.sort.label": {"uz": "Kalitlarni tartiblash:", "en": "Sort keys:"},
    "gui.export.sort.none": {"uz": "Yo'q (fayldagi tartib)", "en": "No (file order)"},
    "gui.export.sort.alpha": {"uz": "Alifbo bo'yicha", "en": "Alphabetically"},
    "gui.export.sort.codepoint": {"uz": "Kod nuqtasi bo'yicha", "en": "By code point"},
    "gui.export.sort.tip": {
        "uz": "Har bir obyekt kalitlarini tartiblaydi (yozuvlar tartibi o'zgarmaydi). Alifbo: o'zbekcha, "
        "item2 < item10, harf kattaligiga qaramay.",
        "en": "Sorts the keys of every object (the order of the records is kept). Alphabetical: Uzbek order, "
        "item2 < item10, ignoring case.",
    },
    "gui.export.node_no_sort": {
        "uz": "Joriy tugun eksportida kalitlar fayldagi tartibda qoladi.",
        "en": "A node export keeps the keys in file order.",
    },
    "gui.export.keep_wrapper": {"uz": "Tashqi o'ramani saqlash", "en": "Keep the wrapper"},
    "gui.export.keep_wrapper.tip": {
        "uz": "Yozuvlar massivi atrofidagi tashqi o'rama maydonlari (masalan, meta) ham yoziladi.",
        "en": "The wrapper's fields around the record array (e.g. meta) are written too.",
    },
    "gui.export.keep_wrapper.only_wrapped": {
        "uz": "Faqat yozuvlari boshqa obyekt ichidagi massivda turgan hujjatlar uchun.",
        "en": "Only for documents whose records sit in an array inside another object.",
    },
    "gui.export.as_keyed": {"uz": "Kalitli obyektni tiklash", "en": "Rebuild the keyed object"},
    "gui.export.as_keyed.tip": {
        "uz": "{line} ko'rinishidagi yozuvlardan (har qatorda bittadan) bitta umumiy obyekt yasaydi: {result}. "
        "Har bir yozuv aynan bitta a'zoli obyekt bo'lishi kerak.",
        "en": "Builds one object from records like {line} (one per line): {result}. "
        "Every record must be an object with exactly one member.",
    },
    "gui.export.jsonl.note": {
        "uz": "Har bir yozuv bitta qatorga ixcham yoziladi. Kalitli yozuvlar {line} qatoriga aylanadi.",
        "en": "Every record is written compactly on one line. Keyed records become {line} lines.",
    },
    "gui.export.columns.label": {"uz": "Ustunlar:", "en": "Columns:"},
    "gui.export.columns.auto": {"uz": "Avtomatik (barcha maydonlar)", "en": "Automatic (every field)"},
    "gui.export.columns.auto.tip": {
        "uz": "Ustunlar barcha yozuvlarni oldindan ko'rib chiqib aniqlanadi: hech bir maydon yo'qolmaydi.",
        "en": "The columns are found by a scan of every record first: no field is lost.",
    },
    "gui.export.columns.placeholder": {
        "uz": "Har qatorda bitta ustun: name, contact.city, tags[0], $key, $index",
        "en": "One column per line: name, contact.city, tags[0], $key, $index",
    },
    "gui.export.columns.tip": {
        "uz": "Ichma-ich maydonlar a.b.c ko'rinishida. $key — kalitli yozuv kaliti, $index — yozuv raqami.",
        "en": "Nested fields as a.b.c. $key is the key of a keyed record, $index the record number.",
    },
    "gui.export.no_columns": {
        "uz": "Ustunlar ro'yxati bo'sh: ustun yozing yoki \"Avtomatik\"ni belgilang.",
        "en": "The column list is empty: enter columns or tick \"Automatic\".",
    },
    "gui.export.array.label": {"uz": "Massivlar:", "en": "Arrays:"},
    "gui.export.array.join": {"uz": "Bitta katakka birlashtirish", "en": "Join into one cell"},
    "gui.export.array.rows": {"uz": "Har bir element alohida qatorda", "en": "One row per element"},
    "gui.export.array.tip": {
        "uz": "Massiv elementlari ajratuvchi bilan bitta katakka yoziladi yoki har biri o'z qatoriga chiqadi.",
        "en": "Array elements are joined into one cell with the separator, or each gets a row of its own.",
    },
    "gui.export.explode.label": {"uz": "Qatorlarga yoyiladigan massiv:", "en": "Array expanded into rows:"},
    "gui.export.explode.placeholder": {"uz": "ixtiyoriy, masalan: orders", "en": "optional, e.g. orders"},
    "gui.export.explode.tip": {
        "uz": "Shu yo'ldagi massivning har bir elementi alohida qator bo'ladi (boshqa massivlar birlashtiriladi).",
        "en": "Every element of the array at this path gets its own row (other arrays are joined).",
    },
    "gui.export.join_sep.label": {"uz": "Birlashtiruvchi belgi:", "en": "Join separator:"},
    "gui.export.join_sep.tip": {
        "uz": "Massiv elementlari orasiga qo'yiladi (1–16 belgi).",
        "en": "Put between array elements (1 to 16 characters).",
    },
    "gui.export.delimiter.label": {"uz": "CSV ajratuvchisi:", "en": "CSV delimiter:"},
    "gui.export.delimiter.tip": {
        "uz": "Ustunlar orasidagi belgi. Excel o'zbek/rus sozlamalarida ko'pincha nuqtali vergul (;) kutadi.",
        "en": "The character between columns. Excel with some regional settings expects a semicolon (;).",
    },
    "gui.export.bom": {"uz": "Boshida BOM (Excel uchun)", "en": "BOM at the start (for Excel)"},
    "gui.export.bom.tip": {
        "uz": "Excel faylni UTF-8 deb to'g'ri ochishi uchun.",
        "en": "So that Excel opens the file as UTF-8.",
    },
    "gui.export.split.label": {"uz": "Qatorlar ko'p bo'lsa:", "en": "Too many rows:"},
    "gui.export.split.sheets": {"uz": "Yangi varaqlarga bo'lish", "en": "Split into sheets"},
    "gui.export.split.files": {"uz": "Yangi fayllarga bo'lish", "en": "Split into files"},
    "gui.export.split.tip": {
        "uz": "Bitta varaqqa sig'magan qatorlar keyingi varaqqa yoki faylga yoziladi.",
        "en": "Rows that do not fit one sheet go to the next sheet or file.",
    },
    "gui.export.max_rows.label": {"uz": "Varaqdagi eng ko'p qator:", "en": "Maximum rows per sheet:"},
    "gui.export.max_rows.tip": {
        "uz": "Sarlavha qatori bilan birga. Excel chegarasi: 1 048 576.",
        "en": "Including the header row. Excel's limit is 1,048,576.",
    },
    "gui.export.sqlite.note": {
        "uz": "Yozuvlar asosiy jadvalga, ichidagi massivlar esa ota yozuvga bog'langan alohida jadvallarga "
        "yoziladi. Keyin SQL bilan so'rov qilish mumkin.",
        "en": "The records go to a main table, the arrays inside them to separate tables linked to their "
        "parent record. You can then query them with SQL.",
    },
    "gui.export.node_formats": {
        "uz": "Joriy tugunni faqat JSON yoki JSON Lines formatida eksport qilish mumkin.",
        "en": "The current node can be exported only as JSON or JSON Lines.",
    },
    "gui.export.node_truncated": {
        "uz": "Tugun ma'lumotini oxirigacha o'qib bo'lmadi: fayl qisqargan.",
        "en": "The node's data could not be read to the end: the file got shorter.",
    },
    "gui.export.node_truncated.hint": {
        "uz": "Faylni qayta oching (F5) va yana urinib ko'ring.",
        "en": "Reopen the file (F5) and try again.",
    },
    "gui.export.progress.node": {"uz": "Tugun yozilmoqda: {name}", "en": "Writing the node: {name}"},
    "gui.export.need_path": {"uz": "Natija faylini tanlang.", "en": "Choose the result file."},
    "gui.export.same_file.hint": {
        "uz": "Boshqa nom yoki papka tanlang: asl fayl hech qachon o'zgarmaydi.",
        "en": "Choose another name or folder: the source file is never changed.",
    },
    "gui.export.view_busy": {
        "uz": "Filtr yoki tartiblash hali tugamagan: ko'rinish to'liq bo'lgach eksport qiling (yoki \"Barcha "
        "yozuvlar\"ni tanlang).",
        "en": "The filter or sort has not finished yet: export once the view is complete (or choose \"All "
        "records\").",
    },
    "gui.export.status.done_path": {
        "uz": "Eksport tayyor: {records} ta yozuv, {size} — {path}",
        "en": "Export finished: {records} records, {size} — {path}",
    },
    "gui.export.status.cancelled_path": {
        "uz": "Eksport bekor qilindi (fayl yozilmadi): {path}",
        "en": "The export was cancelled (no file was written): {path}",
    },
    "gui.export.partial": {
        "uz": "Faylda JSON xatosi bor: faqat xatogacha bo'lgan yozuvlar eksport qilinadi.",
        "en": "The file has a JSON error: only the records before the error are exported.",
    },
    "gui.export.ext_mismatch": {
        "uz": "Fayl kengaytmasi ({ext}) tanlangan formatga ({fmt}) mos emas.",
        "en": "The file extension ({ext}) does not match the chosen format ({fmt}).",
    },
    "gui.export.overwrite": {
        "uz": "Bu fayl allaqachon bor:\n{path}\n\nUning ustiga yozilsinmi?",
        "en": "This file already exists:\n{path}\n\nReplace it?",
    },
    "gui.export.run": {"uz": "Eksport qilish", "en": "Export"},
    "gui.export.stop": {"uz": "To'xtatish", "en": "Stop"},
    "gui.export.stop.tip": {
        "uz": "Eksportni to'xtatish (chala fayl qolmaydi)",
        "en": "Stop the export (no partial file is left)",
    },
    "gui.export.close.tip": {
        "uz": "Oynani yopish. Boshlangan eksport fon vazifalari panelida davom etadi.",
        "en": "Close the window. A running export goes on in the tasks panel.",
    },
    "gui.export.task": {"uz": "Eksport: {name}", "en": "Export: {name}"},
    "gui.export.task.state": {"uz": "Eksport sozlamalari eslab qolinmoqda", "en": "Remembering the export options"},
    "gui.export.status.running": {"uz": "Yozilmoqda: {path}", "en": "Writing {path}"},
    "gui.export.status.queued": {
        "uz": "Navbatda: boshqa og'ir vazifa tugashi bilan yozish boshlanadi ({path})",
        "en": "Queued: writing starts when another heavy task has finished ({path})",
    },
    "gui.export.status.done": {
        "uz": "Tayyor: {records} ta yozuv, {size}",
        "en": "Done: {records} records, {size}",
    },
    "gui.export.status.failed": {"uz": "Xato: {error}", "en": "Error: {error}"},
    "gui.export.status.cancelled": {
        "uz": "To'xtatildi: hech qanday fayl yozilmadi.",
        "en": "Stopped: no file was written.",
    },
    "gui.export.report.title": {"uz": "Eksport tugadi", "en": "Export finished"},
    "gui.export.report.summary": {
        "uz": "{records} ta yozuv ({rows} ta qator), {size}; {seconds} vaqt ketdi ({rate} MB/s).",
        "en": "{records} records ({rows} rows), {size}; took {seconds} ({rate} MB/s).",
    },
    "gui.export.report.skipped": {
        "uz": "{count} ta yozuv o'tkazib yuborildi (ogohlantirishlarga qarang).",
        "en": "{count} records were skipped (see the warnings).",
    },
    "gui.export.report.tables": {"uz": "Jadvallar: {tables}", "en": "Tables: {tables}"},
    "gui.export.report.files": {"uz": "Fayllar ({count}):", "en": "Files ({count}):"},
    "gui.export.report.warnings": {"uz": "Ogohlantirishlar ({count}):", "en": "Warnings ({count}):"},
    "gui.export.report.open_folder": {"uz": "Papkani ochish", "en": "Open folder"},
    # ---- gui.settings ----
    "gui.settings.title": {"uz": "Sozlamalar", "en": "Settings"},
    "gui.settings.tab.general": {"uz": "Umumiy", "en": "General"},
    "gui.settings.tab.export": {"uz": "Eksport", "en": "Export"},
    "gui.settings.tab.view": {"uz": "Ko'rinish", "en": "Appearance"},
    "gui.settings.tab.advanced": {"uz": "Qo'shimcha", "en": "Advanced"},
    "gui.settings.save": {"uz": "Saqlash", "en": "Save"},
    "gui.settings.defaults": {"uz": "Standartga qaytarish", "en": "Restore defaults"},
    "gui.settings.defaults.tip": {
        "uz": "Barcha maydonlarga standart qiymatlarni qo'yadi (\"Saqlash\" bosilgandagina saqlanadi).",
        "en": "Puts the default values into every field (saved only when you press \"Save\").",
    },
    "gui.settings.browse": {"uz": "Tanlash…", "en": "Browse…"},
    "gui.settings.size.placeholder": {"uz": "masalan: 512MB, 20GB", "en": "e.g. 512MB, 20GB"},
    "gui.settings.size.exact": {"uz": "= {bytes} bayt", "en": "= {bytes} bytes"},
    "gui.settings.sample_size.tip": {
        "uz": "Tuzilma (schema) va jadval ustunlari shuncha yozuvdan aniqlanadi.",
        "en": "The structure (schema) and the table columns are inferred from this many records.",
    },
    "gui.settings.cache_dir.default": {"uz": "Standart papka", "en": "Default folder"},
    "gui.settings.cache_dir.default_path": {"uz": "Standart: {path}", "en": "Default: {path}"},
    "gui.settings.cache_dir.tip": {
        "uz": "Indeks fayllari va katta vaqtinchalik fayllar shu yerda saqlanadi. Bo'sh — standart papka.",
        "en": "Index files and big temporary files are kept here. Empty means the default folder.",
    },
    "gui.settings.cache_dir.clear": {"uz": "Standart", "en": "Default"},
    "gui.settings.cache_dir.clear.tip": {"uz": "Standart papkaga qaytish", "en": "Go back to the default folder"},
    "gui.settings.cache_dir.choose": {"uz": "Kesh papkasini tanlang", "en": "Choose the cache folder"},
    "gui.settings.cache_dir.relative": {
        "uz": "to'liq yo'l kerak (diskdan boshlab, masalan D:\\kesh)",
        "en": "a full path is needed (starting with the drive, e.g. D:\\cache)",
    },
    "gui.settings.cache_dir.no_drive": {
        "uz": "{drive} disk topilmadi (ulanmaganmi?)",
        "en": "drive {drive} was not found (is it connected?)",
    },
    "gui.settings.cache_dir.is_file": {
        "uz": "bu yo'l papka emas, fayl",
        "en": "this path is a file, not a folder",
    },
    "gui.settings.cache_dir.system": {
        "uz": "tizim papkasi ({folder}) yoki uning ichidagi papka bo'lmasin: o'zingizning papkangizni tanlang "
        "(masalan, D:\\kesh)",
        "en": "must not be a system folder ({folder}) or a folder inside it: choose a folder of your own "
        "(e.g. D:\\cache)",
    },
    "gui.settings.cache_dir.root": {
        "uz": "diskning ildiz papkasi ({path}) bo'lmasin: unda alohida papka yarating va o'shani tanlang "
        "(masalan, D:\\kesh)",
        "en": "must not be the root of a drive ({path}): create a folder in it and choose that one "
        "(e.g. D:\\cache)",
    },
    "gui.settings.language.env": {
        "uz": "Interfeys tilini JSONRESHAPE_LANG muhit o'zgaruvchisi belgilaydi ({lang}): u bu sozlamadan "
              "ustun. Bu yerda tanlangan til o'zgaruvchisiz ishga tushirilganda ishlatiladi.",
        "en": "The JSONRESHAPE_LANG environment variable chooses the interface language ({lang}): it wins over "
              "this setting. The language chosen here is used when the program starts without the variable.",
    },
    "gui.settings.cache.measuring": {"uz": "Kesh hajmi hisoblanmoqda…", "en": "Measuring the cache…"},
    "gui.settings.cache.usage": {"uz": "Kesh hajmi: {size}", "en": "Cache size: {size}"},
    "gui.settings.cache.unknown": {"uz": "Kesh hajmini aniqlab bo'lmadi.", "en": "The cache size is unknown."},
    "gui.settings.cache.open": {"uz": "Kesh papkasini ochish", "en": "Open the cache folder"},
    "gui.settings.task.cache": {"uz": "Kesh hajmi hisoblanmoqda", "en": "Measuring the cache"},
    "gui.settings.cache_limit.tip": {
        "uz": "Indekslar va tayyorlangan nusxalar uchun umumiy joy. Oshsa, eng eski ishlatilganlari o'chiriladi.",
        "en": "Room for indexes and prepared copies together. Beyond it the least recently used ones are removed.",
    },
    "gui.settings.memory_limit.tip": {
        "uz": "Dastur ishlatadigan umumiy xotira chegarasi: tartiblash yarmini, qidiruv natijalari choragini oladi.",
        "en": "The overall memory budget: sorting uses at most half of it, stored search results a quarter.",
    },
    "gui.settings.sort_memory.tip": {
        "uz": "Shundan ko'p bo'lsa, tartiblash diskdagi vaqtinchalik fayllar bilan ishlaydi.",
        "en": "Beyond this, sorting works with temporary files on disk.",
    },
    "gui.settings.language.tip": {"uz": "Interfeys tili", "en": "The interface language"},
    "gui.settings.language.restart": {
        "uz": "Til saqlangandan keyin almashadi; ba'zi matnlar (menyular, ochiq oynalar) dastur qayta "
        "ishga tushirilgandan keyin o'zgaradi.",
        "en": "The language changes after saving; some texts (menus, open windows) change only after a restart.",
    },
    "gui.settings.theme.label": {"uz": "Mavzu:", "en": "Theme:"},
    "gui.settings.theme.system": {"uz": "Tizimga ergashish", "en": "Follow the system"},
    "gui.settings.theme.light": {"uz": "Yorug'", "en": "Light"},
    "gui.settings.theme.dark": {"uz": "Qorong'i", "en": "Dark"},
    "gui.settings.theme.tip": {
        "uz": "Darhol qo'llanadi; \"Bekor qilish\" avvalgisini qaytaradi.",
        "en": "Applied at once; \"Cancel\" brings the previous one back.",
    },
    "gui.settings.mono.label": {"uz": "Ma'lumotlar shrifti:", "en": "Data font:"},
    "gui.settings.mono.tip": {
        "uz": "Daraxt, jadval va matn ko'rinishlaridagi bir xil kenglikdagi shrift",
        "en": "The fixed-width font of the tree, table and text views",
    },
    "gui.settings.mono.default": {"uz": "Standart", "en": "Default"},
    "gui.settings.mono.default.tip": {"uz": "Standart shrift: {family}", "en": "Default font: {family}"},
    "gui.settings.mono_size.label": {"uz": "Ma'lumotlar shrifti o'lchami:", "en": "Data font size:"},
    "gui.settings.ui_size.label": {"uz": "Interfeys shrifti o'lchami:", "en": "Interface font size:"},
    "gui.settings.ui_size.tip": {
        "uz": "Menyular, tugmalar va yozuvlar uchun",
        "en": "For menus, buttons and labels",
    },
    "gui.settings.size_default": {"uz": "Standart", "en": "Default"},
    "gui.settings.advanced.note": {
        "uz": "Bu chegaralar katta fayllar bilan ishlash tezligi va xotirasiga ta'sir qiladi. Ishonchingiz komil "
        "bo'lmasa, o'zgartirmang.",
        "en": "These limits affect the speed and memory use with big files. Change them only if you are sure.",
    },
    "gui.settings.large_node_bytes.tip": {
        "uz": "Bundan katta tugun xotiraga to'liq o'qilmaydi: oqim bilan ishlanadi.",
        "en": "A node larger than this is never read into memory whole: it is streamed.",
    },
    "gui.settings.large_node_children.tip": {
        "uz": "Bundan ko'p bolali tugunning bolalari indeksi diskda saqlanadi.",
        "en": "The child index of a node with more children than this is kept on disk.",
    },
    "gui.settings.head_bytes.tip": {
        "uz": "Fayl formatini aniqlash uchun boshidan shuncha bayt o'qiladi.",
        "en": "This many bytes from the start are read to detect the file format.",
    },
    # ---- gui.nav ----
    "gui.nav.unexpected": {"uz": "Kutilmagan xato: {error}", "en": "Unexpected error: {error}"},
    "gui.nav.goto.title": {"uz": "Yo'lga o'tish", "en": "Go to"},
    "gui.nav.goto.help": {
        "uz": "Yo'l yoki yozuv raqamini yozing: data[120].contact.address, $.data[120], /data/120, #120 yoki #120.contact (yozuvlar #0 dan boshlanadi).",
        "en": "Type a path or a record number: data[120].contact.address, $.data[120], /data/120, #120 or #120.contact (records start at #0).",
    },
    "gui.nav.goto.placeholder": {"uz": "masalan: #120.contact.address", "en": "e.g. #120.contact.address"},
    "gui.nav.goto.busy": {"uz": "Qidirilmoqda…", "en": "Looking it up…"},
    "gui.nav.goto.go": {"uz": "O'tish", "en": "Go"},
    "gui.nav.goto.task": {"uz": "Tugun qidirilmoqda", "en": "Looking up the node"},
    "gui.nav.bookmarks.title": {"uz": "Xatcho'plar — {name}", "en": "Bookmarks — {name}"},
    "gui.nav.bookmarks.col.name": {"uz": "Nomi", "en": "Name"},
    "gui.nav.bookmarks.col.location": {"uz": "Joyi", "en": "Location"},
    "gui.nav.bookmarks.col.created": {"uz": "Qo'shilgan", "en": "Added"},
    "gui.nav.bookmarks.empty": {
        "uz": "Bu faylda xatcho'p yo'q. Tugunni tanlab Ctrl+D bosing.",
        "en": "This file has no bookmarks. Select a node and press Ctrl+D.",
    },
    "gui.nav.bookmarks.go": {"uz": "O'tish", "en": "Go to"},
    "gui.nav.bookmarks.rename": {"uz": "Nomini o'zgartirish…", "en": "Rename…"},
    "gui.nav.bookmarks.delete": {"uz": "O'chirish", "en": "Delete"},
    "gui.nav.bookmarks.rename.title": {"uz": "Xatcho'p nomi", "en": "Bookmark name"},
    "gui.nav.bookmarks.rename.label": {"uz": "Yangi nom:", "en": "New name:"},
    "gui.nav.rp.title": {"uz": "Yozuvlar yo'li — {name}", "en": "Record path — {name}"},
    "gui.nav.rp.intro": {
        "uz": "Fayl obyekt bilan boshlanadi. Yozuvlar qaysi massivda (yoki obyektda) turganini tanlang. Ro'yxatda faylning boshidan topilgan yo'llar bor; boshqa yo'lni pastda yozishingiz mumkin.",
        "en": "The file starts with an object. Choose the array (or object) that holds the records. The list shows the paths found at the beginning of the file; you can type another path below.",
    },
    "gui.nav.rp.col.path": {"uz": "Yo'l", "en": "Path"},
    "gui.nav.rp.col.items": {"uz": "Elementlar", "en": "Items"},
    "gui.nav.rp.col.kind": {"uz": "Ichida", "en": "Contains"},
    "gui.nav.rp.auto": {"uz": "Avtomatik aniqlash", "en": "Detect automatically"},
    "gui.nav.rp.items.more": {"uz": "{count}+", "en": "{count}+"},
    "gui.nav.rp.items.objects": {"uz": "obyektlar", "en": "objects"},
    "gui.nav.rp.items.arrays": {"uz": "massivlar", "en": "arrays"},
    "gui.nav.rp.items.mixed": {"uz": "aralash", "en": "mixed"},
    "gui.nav.rp.items.values": {"uz": "oddiy qiymatlar", "en": "plain values"},
    "gui.nav.rp.items.empty": {"uz": "bo'sh", "en": "empty"},
    "gui.nav.rp.path": {"uz": "Yozuvlar yo'li:", "en": "Record path:"},
    "gui.nav.rp.path.placeholder": {
        "uz": "bo'sh = avtomatik; masalan: data yoki result.items",
        "en": "empty = automatic; e.g. data or result.items",
    },
    "gui.nav.rp.format": {"uz": "Format:", "en": "Format:"},
    "gui.nav.rp.format.auto": {"uz": "Avtomatik", "en": "Automatic"},
    "gui.nav.rp.conflict": {
        "uz": "\"{format}\" formati yozuvlar yo'li bilan birga ishlamaydi: yo'lni o'chiring yoki formatni \"Avtomatik\" qiling.",
        "en": "The \"{format}\" format cannot be combined with a record path: clear the path or set the format to \"Automatic\".",
    },
    "gui.nav.rp.reopen": {"uz": "Qayta ochish", "en": "Reopen"},
    "gui.nav.broken.title": {"uz": "Buzuq qatorlar — {name}", "en": "Broken lines — {name}"},
    "gui.nav.broken.col.line": {"uz": "Qator", "en": "Line"},
    "gui.nav.broken.col.column": {"uz": "Ustun", "en": "Column"},
    "gui.nav.broken.col.offset": {"uz": "Bayt", "en": "Byte"},
    "gui.nav.broken.col.reason": {"uz": "Sabab", "en": "Reason"},
    "gui.nav.broken.col.text": {"uz": "Matn", "en": "Text"},
    "gui.nav.broken.summary": {
        "uz": "{count} ta buzuq qator o'tkazib yuborildi. Qatorni ikki marta bossangiz, u xom ko'rinishda ochiladi.",
        "en": "{count} broken lines were skipped. Double-click a line to show it in the raw view.",
    },
    "gui.nav.broken.summary_capped": {
        "uz": "{count} ta buzuq qator o'tkazib yuborildi; ro'yxatda birinchi {shown} tasi bor. Qatorni ikki marta bossangiz, u xom ko'rinishda ochiladi.",
        "en": "{count} broken lines were skipped; the first {shown} are listed. Double-click a line to show it in the raw view.",
    },
    "gui.nav.broken.go": {"uz": "Xom ko'rinishda ko'rsatish", "en": "Show in the raw view"},
    "gui.nav.broken.go.tip": {
        "uz": "Tanlangan qatorni xom baytlar ko'rinishida ochadi",
        "en": "Shows the selected line in the raw byte view",
    },
    "gui.nav.broken.task": {"uz": "Buzuq qatorlar ro'yxati o'qilmoqda", "en": "Reading the list of broken lines"},
    # ---- gui.doctab ----
    "gui.doctab.view_banner": {
        "uz": "Ko'rinish: {label} · {shown} / {total} yozuv",
        "en": "View: {label} · {shown} / {total} records",
    },
    "gui.doctab.view_banner.busy": {"uz": "{text} · hisoblanmoqda…", "en": "{text} · updating…"},
    "gui.doctab.view_banner.clear": {"uz": "Hammasini ko'rsatish", "en": "Show all"},
    "gui.doctab.view_banner.clear.tip": {
        "uz": "Filtr, tartib va qidiruv natijasini olib tashlab, barcha yozuvlarni fayldagi tartibda ko'rsatish",
        "en": "Remove the filter, sort and search results and show all records in file order",
    },
    "gui.doctab.view.tree": {"uz": "Daraxt", "en": "Tree"},
    "gui.doctab.view.table": {"uz": "Jadval", "en": "Table"},
    "gui.doctab.view.text": {"uz": "Matn", "en": "Text"},
    "gui.doctab.view.raw": {"uz": "Xom", "en": "Raw"},
    "gui.doctab.view.schema": {"uz": "Tuzilma", "en": "Structure"},
    "gui.doctab.view.tip": {"uz": "{view} ({key})", "en": "{view} ({key})"},
    "gui.doctab.no_views": {
        "uz": "Ko'rinishlarni yuklab bo'lmadi (tafsilotlar logda).",
        "en": "No views could be loaded (the details are in the log).",
    },
    "gui.doctab.retry": {"uz": "Qayta urinish", "en": "Try again"},
    "gui.doctab.open_log": {"uz": "Logni ochish", "en": "Open log"},
    "gui.doctab.open_cancelled": {"uz": "{name} faylini ochish bekor qilindi.", "en": "Opening {name} was cancelled."},
    "gui.doctab.open_failed": {
        "uz": "{name} faylini ochib bo'lmadi: {error}",
        "en": "{name} could not be opened: {error}",
    },
    "gui.doctab.open_auto": {"uz": "Avtomatik aniqlash bilan ochish", "en": "Open with automatic detection"},
    "gui.doctab.error.partial": {
        "uz": "Xatogacha bo'lgan {count} ta yozuv ochildi.",
        "en": "The {count} records before the error were opened.",
    },
    "gui.doctab.error.goto": {"uz": "Xatoga o'tish", "en": "Go to the error"},
    "gui.doctab.error.hint_jsonl": {
        "uz": "Bu fayl JSON Lines (har qatorda bitta qiymat) bo'lishi mumkin.",
        "en": "This file may be JSON Lines (one value per line).",
    },
    "gui.doctab.error.hint_stream": {
        "uz": "Bu faylda bir nechta JSON qiymat ketma-ket yozilgan bo'lishi mumkin.",
        "en": "This file may hold several JSON values one after another.",
    },
    "gui.doctab.error.open_jsonl": {"uz": "JSON Lines sifatida ochish", "en": "Open as JSON Lines"},
    "gui.doctab.error.open_stream": {"uz": "Ketma-ket qiymatlar sifatida ochish", "en": "Open as concatenated values"},
    "gui.doctab.broken": {
        "uz": "{count} ta buzuq qator o'tkazib yuborildi.",
        "en": "{count} broken lines were skipped.",
    },
    "gui.doctab.broken.list": {"uz": "Ro'yxat", "en": "List"},
    "gui.doctab.record_path.wrapped": {"uz": "Yozuvlar yo'li: {path} ({how})", "en": "Record path: {path} ({how})"},
    "gui.doctab.record_path.keyed": {
        "uz": "Yozuvlar: ildiz obyektning a'zolari ({how})",
        "en": "Records: the members of the root object ({how})",
    },
    "gui.doctab.record_path.single": {
        "uz": "Fayl bitta qiymat sifatida ochildi ({how})",
        "en": "The file was opened as a single value ({how})",
    },
    "gui.doctab.record_path.auto": {"uz": "avtomatik", "en": "automatic"},
    "gui.doctab.record_path.chosen": {"uz": "tanlangan", "en": "chosen"},
    "gui.doctab.record_path.candidates": {
        "uz": "Topilgan yo'llar ({count}): {paths}",
        "en": "Paths found ({count}): {paths}",
    },
    "gui.doctab.record_path.change": {"uz": "O'zgartirish…", "en": "Change…"},
    "gui.doctab.changed": {"uz": "Fayl tashqaridan o'zgartirildi.", "en": "The file was changed by another program."},
    "gui.doctab.changed.detail": {
        "uz": "Ko'rsatilayotgan ma'lumot eskirgan bo'lishi mumkin. Yangi holatini ko'rish uchun faylni qayta oching.",
        "en": "The data shown may be out of date. Reopen the file to see its new content.",
    },
    "gui.doctab.grown": {
        "uz": "Fayl o'sdi (+{size}); ochilgan paytdagi holati ko'rsatilmoqda.",
        "en": "The file grew (+{size}); its state at opening time is shown.",
    },
    "gui.doctab.reopen": {"uz": "Qayta ochish", "en": "Reopen"},
    "gui.doctab.ignore": {"uz": "E'tiborsiz qoldirish", "en": "Ignore"},
    # ---- gui.status ----
    "gui.status.index_cancelled": {
        "uz": "{text} · indekslash to'xtatildi (to'liq emas)",
        "en": "{text} · indexing stopped (incomplete)",
    },
    "gui.status.index_failed": {
        "uz": "{text} · indekslash xato bilan tugadi (to'liq emas)",
        "en": "{text} · indexing failed (incomplete)",
    },
    "gui.status.opening": {"uz": "Ochilmoqda…", "en": "Opening…"},
    "gui.status.failed": {"uz": "Ochilmadi", "en": "Not opened"},
    "gui.status.single": {"uz": "Bitta qiymat", "en": "Single value"},
    "gui.status.records": {"uz": "{count} yozuv", "en": "{count} records"},
    "gui.status.records_view": {"uz": "{shown} / {total} yozuv", "en": "{shown} / {total} records"},
    "gui.status.indexing": {"uz": "{text} (indekslanmoqda {percent} %)", "en": "{text} (indexing {percent}%)"},
    "gui.status.indexing_unknown": {"uz": "{text} (indekslanmoqda…)", "en": "{text} (indexing…)"},
    "gui.status.partial": {"uz": "{text} (xatogacha)", "en": "{text} (up to the error)"},
    "gui.status.view_busy": {"uz": "{text} · hisoblanmoqda…", "en": "{text} · updating…"},
    "gui.status.size.tip": {"uz": "Fayl hajmi: {size} bayt", "en": "File size: {size} bytes"},
    "gui.status.size.data_tip": {
        "uz": "O'qilayotgan (o'girilgan) ma'lumot: {size}",
        "en": "Data being read (converted): {size}",
    },
    "gui.status.size.appended_tip": {
        "uz": "Ochilgandan keyin qo'shilgan: {size}",
        "en": "Appended since opening: {size}",
    },
    "gui.status.format_path": {"uz": "{format} · {path}", "en": "{format} · {path}"},
    "gui.status.format.auto": {"uz": "Format avtomatik aniqlangan", "en": "Format detected automatically"},
    "gui.status.format.forced": {"uz": "Format qo'lda tanlangan", "en": "Format chosen by hand"},
    "gui.status.format.broken": {"uz": "Buzuq qatorlar: {count}", "en": "Broken lines: {count}"},
    "gui.status.encoding_compressed": {"uz": "{encoding} · {compression}", "en": "{encoding} · {compression}"},
    "gui.status.encoding.converted_tip": {
        "uz": "Ishlash uchun vaqtinchalik nusxa: {path}",
        "en": "Working copy: {path}",
    },
    "gui.status.selection": {"uz": "{count} ta tanlangan", "en": "{count} selected"},
    "gui.status.offset": {"uz": "Bayt {start}–{end}", "en": "Bytes {start}–{end}"},
    "gui.status.offset.tip": {
        "uz": "Joriy tugun fayldagi {start}–{end} baytlarda ({size})",
        "en": "The current node is at bytes {start}–{end} of the file ({size})",
    },
    "gui.status.task.offset": {"uz": "Tugun joyi aniqlanmoqda", "en": "Locating the node"},
    "gui.status.task.memory": {"uz": "Xotira ko'rsatkichi tayyorlanmoqda", "en": "Preparing the memory display"},
    "gui.status.memory": {"uz": "RAM {size}", "en": "RAM {size}"},
    "gui.status.memory.tip": {
        "uz": "Dastur ishlatayotgan xotira: {size}\nXotira chegarasi (sozlamalar): {limit}",
        "en": "Memory used by the program: {size}\nMemory limit (settings): {limit}",
    },
    "gui.status.tasks": {"uz": "{count} ta vazifa", "en": "{count} tasks"},
    "gui.status.tasks_percent": {"uz": "{text} · {percent} %", "en": "{text} · {percent}%"},
    "gui.status.tasks.tip": {"uz": "Fon vazifalarini ko'rsatish", "en": "Show the background tasks"},
    # ---- gui.taskspanel ----
    "gui.taskspanel.eta_days": {"uz": "{days} kun {hours} soat", "en": "{days} d {hours} h"},
    "gui.taskspanel.col.title": {"uz": "Vazifa", "en": "Task"},
    "gui.taskspanel.col.progress": {"uz": "Jarayon", "en": "Progress"},
    "gui.taskspanel.col.done": {"uz": "Bajarildi", "en": "Done"},
    "gui.taskspanel.col.rate": {"uz": "Tezlik", "en": "Speed"},
    "gui.taskspanel.col.eta": {"uz": "Qoldi", "en": "Left"},
    "gui.taskspanel.col.state": {"uz": "Holat", "en": "State"},
    "gui.taskspanel.done_total": {"uz": "{done} / {total}", "en": "{done} / {total}"},
    "gui.taskspanel.rate": {"uz": "{rate}/s", "en": "{rate}/s"},
    "gui.taskspanel.empty": {"uz": "Fon vazifalari yo'q.", "en": "No background tasks."},
    "gui.taskspanel.keep": {"uz": "Tugaganlarini ro'yxatda qoldirish", "en": "Keep finished tasks listed"},
    "gui.taskspanel.keep.tip": {
        "uz": "Belgilansa, tugagan vazifalar \"Tozalash\" bosilguncha ro'yxatda turadi",
        "en": "When checked, finished tasks stay listed until you press \"Clear\"",
    },
    "gui.taskspanel.clear": {"uz": "Tozalash", "en": "Clear"},
    "gui.taskspanel.clear.tip": {
        "uz": "Tugagan vazifalarni ro'yxatdan olib tashlaydi",
        "en": "Removes the finished tasks from the list",
    },
    "gui.taskspanel.cancel_all": {"uz": "Hammasini bekor qilish", "en": "Cancel all"},
    "gui.taskspanel.cancel.tip": {"uz": "Bekor qilish", "en": "Cancel"},
    "gui.taskspanel.cancelling": {"uz": "Bekor qilinmoqda…", "en": "Cancelling…"},
    "gui.taskspanel.title_message": {"uz": "{title} — {message}", "en": "{title} — {message}"},
    # ---- gui.main ----
    "gui.main.folder_ignored": {
        "uz": "Papka ochilmaydi, faqat fayllar: {path}",
        "en": "Folders are not opened, only files: {path}",
    },
    "gui.main.export_continues": {
        "uz": "{name} yopildi; uning eksporti fonda davom etmoqda ({count} ta), tugagach hisobot chiqadi.",
        "en": "{name} was closed; its export goes on in the background ({count}), a report appears when "
        "it is done.",
    },
    "gui.main.exports_running.title": {"uz": "Eksport tugamagan", "en": "Export not finished"},
    "gui.main.exports_running": {
        "uz": "{count} ta eksport hali tugamagan. Dastur yopilsa ular to'xtatiladi va fayllar yozilmaydi. "
        "Baribir yopilsinmi?",
        "en": "{count} exports have not finished yet. Closing the program stops them and their files are not "
        "written. Close anyway?",
    },
    "gui.main.app_name": {"uz": "JSON Reshape", "en": "JSON Reshape"},
    "gui.main.window_title": {"uz": "{name} — JSON Reshape", "en": "{name} — JSON Reshape"},
    "gui.main.welcome": {
        "uz": "Faylni ochish uchun Ctrl+O bosing yoki uni shu oynaga sudrab tashlang.\nJSON, JSON Lines (.jsonl, .ndjson), .json.gz va GeoJSON fayllar ochiladi.",
        "en": "Press Ctrl+O to open a file, or drag it onto this window.\nJSON, JSON Lines (.jsonl, .ndjson), .json.gz and GeoJSON files can be opened.",
    },
    "gui.main.menu.file": {"uz": "&Fayl", "en": "&File"},
    "gui.main.menu.recent": {"uz": "&So'nggi fayllar", "en": "&Recent files"},
    "gui.main.menu.edit": {"uz": "&Tahrir", "en": "&Edit"},
    "gui.main.menu.copy_more": {"uz": "Boshqa &formatda nusxalash", "en": "Copy &as"},
    "gui.main.menu.view": {"uz": "&Ko'rinish", "en": "&View"},
    "gui.main.menu.expand_level": {"uz": "N-&darajagacha ochish", "en": "Expand to &level"},
    "gui.main.menu.theme": {"uz": "&Mavzu", "en": "&Theme"},
    "gui.main.menu.panels": {"uz": "&Panellar", "en": "&Panels"},
    "gui.main.menu.navigate": {"uz": "&Navigatsiya", "en": "&Navigate"},
    "gui.main.menu.search": {"uz": "&Qidiruv", "en": "&Search"},
    "gui.main.menu.tools": {"uz": "&Asboblar", "en": "&Tools"},
    "gui.main.menu.help": {"uz": "&Yordam", "en": "&Help"},
    "gui.main.toolbar": {"uz": "Asboblar paneli", "en": "Toolbar"},
    "gui.main.act.open": {"uz": "&Ochish…", "en": "&Open…"},
    "gui.main.act.open.tip": {"uz": "JSON faylni ochish (Ctrl+O)", "en": "Open a JSON file (Ctrl+O)"},
    "gui.main.act.reopen": {"uz": "&Qayta ochish", "en": "&Reopen"},
    "gui.main.act.reopen.tip": {"uz": "Faylni qaytadan o'qish (F5)", "en": "Read the file again (F5)"},
    "gui.main.act.record_path": {"uz": "&Yozuvlar yo'lini tanlash…", "en": "Choose the &record path…"},
    "gui.main.act.export": {"uz": "&Eksport…", "en": "&Export…"},
    "gui.main.act.export.tip": {
        "uz": "Yozuvlarni JSON, JSONL, CSV, Excel yoki SQLite faylga saqlash (Ctrl+E)",
        "en": "Save the records as JSON, JSONL, CSV, Excel or SQLite (Ctrl+E)",
    },
    "gui.main.act.close_tab": {"uz": "&Yopish", "en": "&Close"},
    "gui.main.act.quit": {"uz": "&Chiqish", "en": "E&xit"},
    "gui.main.act.copy_value": {"uz": "&Qiymatni nusxalash", "en": "Copy &value"},
    "gui.main.act.copy_path": {"uz": "&Yo'lni nusxalash", "en": "Copy &path"},
    "gui.main.act.second_pane": {"uz": "Ikkinchi panelni ko'rsatish", "en": "Show the second pane"},
    "gui.main.act.split_vertical": {
        "uz": "Vertikal bo'lish (ustma-ust)",
        "en": "Split vertically (one above the other)",
    },
    "gui.main.act.zoom_in": {"uz": "Shriftni kattalashtirish", "en": "Larger font"},
    "gui.main.act.zoom_out": {"uz": "Shriftni kichraytirish", "en": "Smaller font"},
    "gui.main.act.zoom_reset": {"uz": "Shrift o'lchamini tiklash", "en": "Reset the font size"},
    "gui.main.theme.system": {"uz": "Tizim bo'yicha", "en": "Follow the system"},
    "gui.main.theme.light": {"uz": "Yorug'", "en": "Light"},
    "gui.main.theme.dark": {"uz": "Qorong'i", "en": "Dark"},
    "gui.main.act.expand_all": {"uz": "Hammasini &ochish", "en": "&Expand all"},
    "gui.main.act.collapse_all": {"uz": "Hammasini &yopish", "en": "&Collapse all"},
    "gui.main.act.expand_level": {"uz": "{level}-darajagacha", "en": "Level {level}"},
    "gui.main.act.sort_keys": {"uz": "Kalitlarni alifbo tartibida", "en": "Sort keys alphabetically"},
    "gui.main.act.sort_keys.tip": {
        "uz": "Daraxtda obyekt kalitlarini alifbo bo'yicha ko'rsatish (faylga ta'sir qilmaydi)",
        "en": "Show object keys in alphabetical order in the tree (the file is not changed)",
    },
    "gui.main.act.goto": {"uz": "Yo'lga &o'tish…", "en": "&Go to…"},
    "gui.main.act.goto.tip": {
        "uz": "Yo'l yoki yozuv raqamiga o'tish (Ctrl+G)",
        "en": "Go to a path or a record number (Ctrl+G)",
    },
    "gui.main.act.back": {"uz": "&Orqaga", "en": "&Back"},
    "gui.main.act.forward": {"uz": "O&ldinga", "en": "&Forward"},
    "gui.main.act.bookmark_add": {"uz": "Xatcho'p &qo'shish…", "en": "Add &bookmark…"},
    "gui.main.act.bookmarks": {"uz": "&Xatcho'plar…", "en": "Book&marks…"},
    "gui.main.act.goto_offset": {"uz": "&Offsetga o'tish…", "en": "Go to &offset…"},
    "gui.main.act.quick_find": {"uz": "&Tezkor qidiruv", "en": "&Quick find"},
    "gui.main.act.find_next": {"uz": "&Keyingisi", "en": "Find &next"},
    "gui.main.act.find_prev": {"uz": "O&ldingisi", "en": "Find &previous"},
    "gui.main.act.search": {"uz": "To'liq &qidiruv", "en": "&Search the file"},
    "gui.main.act.search.tip": {
        "uz": "Butun fayl bo'yicha qidirish (Ctrl+Shift+F)",
        "en": "Search the whole file (Ctrl+Shift+F)",
    },
    "gui.main.act.filter": {"uz": "&Filtr", "en": "&Filter"},
    "gui.main.act.filter.tip": {
        "uz": "Yozuvlarni shart bo'yicha filtrlash (Ctrl+Shift+L)",
        "en": "Filter the records by a condition (Ctrl+Shift+L)",
    },
    "gui.main.act.sort": {"uz": "&Tartiblash…", "en": "&Sort…"},
    "gui.main.act.sort.tip": {"uz": "Yozuvlarni maydonlar bo'yicha tartiblash", "en": "Sort the records by fields"},
    "gui.main.act.clear_view": {"uz": "Filtr va tartibni &tozalash", "en": "&Clear filter and sort"},
    "gui.main.act.settings": {"uz": "&Sozlamalar…", "en": "&Settings…"},
    "gui.main.act.open_logs": {"uz": "&Loglar papkasini ochish", "en": "Open the &logs folder"},
    "gui.main.act.environment": {"uz": "&Muhit haqida", "en": "&Environment"},
    "gui.main.act.about": {"uz": "Dastur &haqida", "en": "&About"},
    "gui.main.dock.tasks": {"uz": "Fon vazifalari", "en": "Background tasks"},
    "gui.main.dock.search": {"uz": "Qidiruv", "en": "Search"},
    "gui.main.dock.filter": {"uz": "Filtr", "en": "Filter"},
    "gui.main.filter.json": {"uz": "JSON fayllar ({patterns})", "en": "JSON files ({patterns})"},
    "gui.main.filter.all": {"uz": "Barcha fayllar (*)", "en": "All files (*)"},
    "gui.main.open.title": {"uz": "Faylni ochish", "en": "Open a file"},
    "gui.main.recent.empty": {"uz": "Ro'yxat bo'sh", "en": "No recent files"},
    "gui.main.recent.clear": {"uz": "Ro'yxatni tozalash", "en": "Clear the list"},
    "gui.main.recent.missing": {
        "uz": "Fayl topilmadi va so'nggi fayllar ro'yxatidan olib tashlandi: {path}",
        "en": "The file was not found and was removed from the recent files: {path}",
    },
    "gui.main.bookmark.title": {"uz": "Xatcho'p qo'shish", "en": "Add a bookmark"},
    "gui.main.bookmark.label": {"uz": "Xatcho'p nomi:", "en": "Bookmark name:"},
    "gui.main.bookmark.no_node": {"uz": "Avval tugunni tanlang.", "en": "Select a node first."},
    "gui.main.bookmark.added": {"uz": "Xatcho'p qo'shildi: {name}", "en": "Bookmark added: {name}"},
    "gui.main.offset.title": {"uz": "Offsetga o'tish", "en": "Go to offset"},
    "gui.main.offset.label": {
        "uz": "Bayt offseti (o'nlik son yoki 0x bilan o'n oltilik):",
        "en": "Byte offset (decimal, or hexadecimal with 0x):",
    },
    "gui.main.offset.invalid": {"uz": "Noto'g'ri offset: {text}", "en": "Not a valid offset: {text}"},
    "gui.main.offset.invalid.hint": {
        "uz": "Manfiy bo'lmagan butun son yozing, masalan 1048576 yoki 0x100000.",
        "en": "Type a whole number that is not negative, e.g. 1048576 or 0x100000.",
    },
    "gui.main.env.task": {"uz": "Muhit tekshirilmoqda", "en": "Checking the environment"},
    "gui.main.env.title": {"uz": "Muhit haqida", "en": "Environment"},
    "gui.main.about.title": {"uz": "Dastur haqida", "en": "About"},
    "gui.main.about.text": {
        "uz": "JSON Reshape {version}\nKatta JSON fayllarni ko'rish, qidirish, filtrlash va eksport qilish uchun.\n\nTezkor yadro: {backend}\n\nDastur to'liq oflayn ishlaydi: hech qanday ma'lumot tarmoqqa yuborilmaydi, telemetriya ham, yangilanish tekshiruvi ham yo'q.",
        "en": "JSON Reshape {version}\nView, search, filter and export huge JSON files.\n\nFast core: {backend}\n\nThe program works fully offline: nothing is sent over the network, there is no telemetry and no update check.",
    },
}

_lock = threading.Lock()
_language = (os.environ.get("JSONRESHAPE_LANG") or DEFAULT_LANGUAGE).strip().lower()
if _language not in SUPPORTED_LANGUAGES:
    _language = DEFAULT_LANGUAGE


class _SafeDict(dict):
    def __missing__(self, key: str) -> str:
        return "{" + key + "}"


_formatter = string.Formatter()


def set_language(lang: str) -> None:
    """Switch the UI language (``uz`` or ``en``)."""
    global _language
    lang = (lang or DEFAULT_LANGUAGE).strip().lower()
    if lang not in SUPPORTED_LANGUAGES:
        raise ValueError(f"unsupported language: {lang!r}")
    with _lock:
        _language = lang


def get_language() -> str:
    return _language


def has(key: str) -> bool:
    return key in MESSAGES


def t(key: str, /, **params: object) -> str:
    """Translate ``key`` into the current language and format it with ``params``."""
    entry = MESSAGES.get(key)
    if entry is None:
        text = key
    else:
        text = entry.get(_language) or entry.get("en") or key
    if not params:
        return text
    try:
        return _formatter.vformat(text, (), _SafeDict(params))
    except (ValueError, IndexError, AttributeError, KeyError):
        return text
