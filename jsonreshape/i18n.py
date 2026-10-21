"""The single dictionary of user-facing texts (Uzbek Latin primary, English fallback, Russian).

Usage: ``t("error.cancelled")`` or ``t("json.error.at", line=2, column=5)``.

Rules:
* every user-visible string in the package goes through :func:`t`;
* each module adds keys only inside its own ``# ---- <section> ----`` block and prefixes
  them with the section name;
* every key has both ``uz`` and ``en`` texts (a test enforces this);
* parameters use ``str.format`` syntax; a missing parameter leaves the placeholder visible
  instead of raising;
* the Russian texts live in :mod:`jsonreshape.i18n_ru` (``MESSAGES_RU``, one string per key,
  loaded on first use); a key without a Russian text falls back to English, so a new key may
  land before its translation (``tests/test_i18n_ru.py`` reports the coverage).
"""

from __future__ import annotations

import os
import string
import threading

SUPPORTED_LANGUAGES = ("uz", "en", "ru")
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
    "fileio.warning.utf16_truncated": {
        "uz": "UTF-16 fayl belgining o'rtasida tugaydi (kesilgan): oxiridagi chala baytlar tashlab yuborildi.",
        "en": "The UTF-16 file ends in the middle of a character (it is cut off): the incomplete bytes at its end "
              "were left out.",
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
    "fileio.error.restore_failed": {
        "uz": "Fayllarni almashtirib bo'lmadi, ba'zi eski fayllarni esa joyiga qaytarib ham bo'lmadi (fayl ← uning "
              "eski mazmuni saqlangan nusxa): {files}. Har bir nusxani o'z fayli nomiga qayta nomlang. Sabab: {reason}",
        "en": "The files could not be replaced, and some old files could not be put back either (file ← the copy "
              "that holds its old content): {files}. Rename each copy back to its file name. Cause: {reason}",
    },
    "fileio.error.restore_failed.hint": {
        "uz": "Nusxalar yashirin bo'lishi mumkin: ularning nomi nuqta bilan boshlanib, .jrold bilan tugaydi. Faylni "
              "ushlab turgan dastur (masalan, antivirus) ishini tugatgach qayta nomlang.",
        "en": "The copies may be hidden: their names start with a dot and end with .jrold. Rename them once the "
              "program holding the files (for example an antivirus scanner) has finished.",
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
    "settings.expected.language": {
        "uz": "uz (o'zbekcha), en (inglizcha) yoki ru (ruscha)",
        "en": "uz (Uzbek), en (English) or ru (Russian)",
    },
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
    "settings.expected.edits_location": {
        "uz": "appdata (dastur papkasida) yoki beside (fayl yonida)",
        "en": "appdata (in the app folder) or beside (next to the file)",
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
    "settings.field.edits_location": {"uz": "Saqlanmagan o'zgarishlar joyi", "en": "Where unsaved changes are kept"},
    "settings.field.backup_on_save": {
        "uz": "Saqlashda .bak zaxira nusxasini qoldirish",
        "en": "Keep a .bak copy when saving",
    },
    "settings.field.undo_limit": {"uz": "Ortga qaytarish (undo) qadamlari", "en": "Undo steps"},
    "settings.field.diff_align_max_bytes": {
        "uz": "Kalitsiz to'liq taqqoslanadigan fayl hajmi chegarasi",
        "en": "Largest file compared in full without a key",
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
    # ---- dupes ----
    "dupes.desc": {"uz": "Takroriy yozuvlar: {key}", "en": "Duplicate records: {key}"},
    "dupes.progress.scan": {
        "uz": "Takroriy qiymatlar qidirilmoqda: {name}",
        "en": "Looking for duplicate values in {name}",
    },
    "dupes.progress.merge": {
        "uz": "Bir xil qiymatlar guruhlanmoqda: {name}",
        "en": "Grouping equal values of {name}",
    },
    "dupes.error.key": {
        "uz": "Takrorlarni qaysi maydon bo'yicha qidirish kerakligini ko'rsating (yozuv ichidagi yo'l yoki $key), "
              "berilgan: {value}",
        "en": "Give the field to look for duplicates by (a path inside the record, or $key), got: {value}",
    },
    "dupes.error.key.hint": {
        "uz": "Misollar: id, email, contact.phone, $ (butun yozuv), $key (kalitli hujjatda yozuvning kaliti).",
        "en": "Examples: id, email, contact.phone, $ (the whole record), $key (the record key in a keyed document).",
    },
    "dupes.error.wildcard": {
        "uz": "Takrorlarni qidirish maydoni aniq yo'l bo'lishi kerak: unda '*', '[*]' yoki '..' ishlatib bo'lmaydi: "
              "{key}",
        "en": "The field to look for duplicates by must be an exact path; '*', '[*]' and '..' cannot be used in it: "
              "{key}",
    },
    "dupes.error.keep": {
        "uz": "Har bir takroriy guruhda qaysi yozuv qolishi {choices} bo'lishi kerak, berilgan: {value}",
        "en": "The record to keep in each group of duplicates must be {choices}, got: {value}",
    },
    "dupes.keep.choices2": {
        "uz": "'first' (birinchisi) yoki 'last' (oxirgisi)",
        "en": "'first' or 'last'",
    },
    "dupes.keep.choices3": {
        "uz": "'first' (birinchisi), 'last' (oxirgisi) yoki 'none' (hech biri: guruhning hamma yozuvlari "
              "belgilanadi)",
        "en": "'first', 'last' or 'none' (none: every record of the group is marked)",
    },
    "dupes.error.top": {
        "uz": "Eng katta guruhlar soni 0 dan {max} gacha butun son bo'lishi kerak, berilgan: {value}",
        "en": "The number of largest groups must be a whole number from 0 to {max}, got: {value}",
    },
    "dupes.error.examples": {
        "uz": "Ko'rsatiladigan misol guruhlar soni 0 dan {max} gacha butun son bo'lishi kerak, berilgan: {value}",
        "en": "The number of example groups must be a whole number from 0 to {max}, got: {value}",
    },
    "dupes.progress.scan_many": {
        "uz": "{count} ta fayldagi takroriy qiymatlar qidirilmoqda",
        "en": "Looking for duplicate values in {count} files",
    },
    "dupes.progress.merge_many": {
        "uz": "{count} ta faylning bir xil qiymatlari guruhlanmoqda",
        "en": "Grouping equal values of {count} files",
    },
    "dupes.warn.input_no_record_keys": {
        "uz": "{name}: bu faylda yozuvlarning kaliti yo'q ($key faqat kalitli hujjatlarda ishlaydi). {count} ta "
              "yozuv kalitsiz hisoblandi.",
        "en": "{name}: the records of this file have no keys ($key works only in keyed documents). {count} records "
              "were counted as having no key.",
    },
    "dupes.warn.input_no_record_keys.one": {
        "uz": "{name}: bu faylda yozuvlarning kaliti yo'q ($key faqat kalitli hujjatlarda ishlaydi). {count} ta "
              "yozuv kalitsiz hisoblandi.",
        "en": "{name}: the records of this file have no keys ($key works only in keyed documents). {count} record "
              "was counted as having no key.",
    },
    "dupes.warn.input_too_large": {
        "uz": "{name}: {count} ta yozuvning (birinchisi #{record}) kalit qiymati xotirada solishtirish uchun juda "
              "katta ({size} dan oshadi): bu yozuvlar takror deb belgilanmadi.",
        "en": "{name}: the key value of {count} records (the first is #{record}) is too large to compare in memory "
              "(over {size}): these records were not marked as duplicates.",
    },
    "dupes.warn.input_too_large.one": {
        "uz": "{name}: {count} ta yozuvning (#{record}) kalit qiymati xotirada solishtirish uchun juda katta "
              "({size} dan oshadi): bu yozuv takror deb belgilanmadi.",
        "en": "{name}: the key value of {count} record (#{record}) is too large to compare in memory "
              "(over {size}): this record was not marked as a duplicate.",
    },
    "dupes.warn.input_unreadable": {
        "uz": "{name}: {count} ta yozuvning (birinchisi #{record}) kalit qiymatini o'qib bo'lmadi (tafsilotlar "
              "log faylida): bu yozuvlar takror deb belgilanmadi.",
        "en": "{name}: the key value of {count} records (the first is #{record}) could not be read (details are "
              "in the log file): these records were not marked as duplicates.",
    },
    "dupes.warn.input_unreadable.one": {
        "uz": "{name}: {count} ta yozuvning (#{record}) kalit qiymatini o'qib bo'lmadi (tafsilotlar log "
              "faylida): bu yozuv takror deb belgilanmadi.",
        "en": "{name}: the key value of {count} record (#{record}) could not be read (details are in the log "
              "file): this record was not marked as a duplicate.",
    },
    "dupes.error.memory_limit": {
        "uz": "Takrorlarni qidirish uchun xotira chegarasi musbat butun son (baytlarda) bo'lishi kerak, "
              "berilgan: {value}",
        "en": "The memory limit for finding duplicates must be a positive whole number of bytes, got: {value}",
    },
    "dupes.warn.too_large": {
        "uz": "{count} ta yozuvning (birinchisi #{record}) kalit qiymati xotirada solishtirish uchun juda katta "
              "({size} dan oshadi): bu yozuvlar takror deb belgilanmadi.",
        "en": "The key value of {count} records (the first is #{record}) is too large to compare in memory "
              "(over {size}): these records were not marked as duplicates.",
    },
    "dupes.warn.too_large.one": {
        "uz": "{count} ta yozuvning (#{record}) kalit qiymati xotirada solishtirish uchun juda katta "
              "({size} dan oshadi): bu yozuv takror deb belgilanmadi.",
        "en": "The key value of {count} record (#{record}) is too large to compare in memory "
              "(over {size}): this record was not marked as a duplicate.",
    },
    "dupes.warn.unreadable": {
        "uz": "{count} ta yozuvning (birinchisi #{record}) kalit qiymatini o'qib bo'lmadi (tafsilotlar log "
              "faylida): bu yozuvlar takror deb belgilanmadi.",
        "en": "The key value of {count} records (the first is #{record}) could not be read (details are in "
              "the log file): these records were not marked as duplicates.",
    },
    "dupes.warn.unreadable.one": {
        "uz": "{count} ta yozuvning (#{record}) kalit qiymatini o'qib bo'lmadi (tafsilotlar log faylida): "
              "bu yozuv takror deb belgilanmadi.",
        "en": "The key value of {count} record (#{record}) could not be read (details are in the log file): "
              "this record was not marked as a duplicate.",
    },
    "dupes.warn.key_absent": {
        "uz": "{key} maydoni birorta ham yozuvda topilmadi ({count} ta yozuv tekshirildi). Maydon nomi to'g'ri "
              "yozilganini tekshiring.",
        "en": "The field {key} was not found in any of the {count} records checked. Check the spelling of the "
              "field name.",
    },
    "dupes.warn.key_absent.one": {
        "uz": "{key} maydoni birorta ham yozuvda topilmadi ({count} ta yozuv tekshirildi). Maydon nomi to'g'ri "
              "yozilganini tekshiring.",
        "en": "The field {key} was not found in the {count} record checked. Check the spelling of the field name.",
    },
    "dupes.warn.no_record_keys": {
        "uz": "Bu hujjatda yozuvlarning kaliti yo'q: $key faqat yozuvlari obyekt a'zolari bo'lgan (kalitli) "
              "hujjatlarda ishlaydi. {count} ta yozuv kalitsiz hisoblandi.",
        "en": "The records of this document have no keys: $key works only in documents whose records are object "
              "members (keyed documents). {count} records were counted as having no key.",
    },
    "dupes.warn.no_record_keys.one": {
        "uz": "Bu hujjatda yozuvlarning kaliti yo'q: $key faqat yozuvlari obyekt a'zolari bo'lgan (kalitli) "
              "hujjatlarda ishlaydi. {count} ta yozuv kalitsiz hisoblandi.",
        "en": "The records of this document have no keys: $key works only in documents whose records are object "
              "members (keyed documents). {count} record was counted as having no key.",
    },
    # ---- diff ----
    # structural diff (diff.py): values, nodes, files
    "diff.value.omitted": {
        "uz": "[{kind}, {size}: ko'rsatish uchun juda katta]",
        "en": "[{kind}, {size}: too large to show]",
    },
    "diff.error.mode": {
        "uz": "Taqqoslash usuli noma'lum: {value}. Mumkin bo'lganlari: auto, key, align, position, document.",
        "en": "Unknown comparison mode: {value}. Possible: auto, key, align, position, document.",
    },
    "diff.error.array_match": {
        "uz": "Massiv elementlarini moslash usuli 'auto', 'index', 'key' yoki 'unordered' (tartibsiz) bo'lishi "
              "kerak, berilgan: {value}",
        "en": "The array matching must be 'auto', 'index', 'key' or 'unordered', got: {value}",
    },
    "diff.error.numbers": {
        "uz": "Sonlar 'value' (qiymati bo'yicha) yoki 'text' (yozilishi bo'yicha) solishtirilishi kerak, "
              "berilgan: {value}",
        "en": "Numbers must be compared by 'value' or by 'text', got: {value}",
    },
    "diff.error.max_changes": {
        "uz": "Bitta yozuv uchun saqlanadigan o'zgarishlar soni 0 yoki musbat butun son bo'lishi kerak, "
              "berilgan: {value}",
        "en": "The number of changes kept per record must be 0 or a positive whole number, got: {value}",
    },
    "diff.error.max_details": {
        "uz": "Batafsil ko'rsatiladigan yozuvlar soni 0 yoki musbat butun son bo'lishi kerak, berilgan: {value}",
        "en": "The number of records shown in detail must be 0 or a positive whole number, got: {value}",
    },
    "diff.error.memory_limit": {
        "uz": "Taqqoslash uchun xotira chegarasi musbat butun son (baytlarda) bo'lishi kerak, berilgan: {value}",
        "en": "The memory limit for the comparison must be a positive whole number of bytes, got: {value}",
    },
    "diff.error.array_key": {
        "uz": "Massiv kaliti noto'g'ri: {value}. Ko'rinishi: MAYDON yoki YO'L=MAYDON (masalan id yoki orders=oid).",
        "en": "Invalid array key: {value}. Form: FIELD or PATH=FIELD (for example id or orders=oid).",
    },
    "diff.error.array_key_wildcard": {
        "uz": "Massiv elementlarini moslash maydoni aniq yo'l bo'lishi kerak ('*', '[*]' va '..' ishlatib "
              "bo'lmaydi): {value}",
        "en": "The field matching array elements must be an exact path ('*', '[*]' and '..' cannot be used): "
              "{value}",
    },
    "diff.error.array_key_missing": {
        "uz": "Massiv elementlarini kalit bo'yicha moslash uchun kalit maydonini ko'rsating (masalan id).",
        "en": "Give the key field to match array elements by (for example id).",
    },
    "diff.error.key_mode": {
        "uz": "Kalit (--key) faqat kalit bo'yicha taqqoslashda ishlatiladi; tanlangan usul: {mode}.",
        "en": "A key (--key) is used only when comparing by key; the mode chosen is {mode}.",
    },
    "diff.error.key_required": {
        "uz": "Kalit bo'yicha taqqoslash uchun kalitni ko'rsating (masalan --key id).",
        "en": "Give the key to compare by (for example --key id).",
    },
    "diff.error.too_big_no_key": {
        "uz": "Fayllar kalitsiz to'liq taqqoslash uchun juda katta ({size}; chegara {limit}).",
        "en": "The files are too large to compare in full without a key ({size}; the limit is {limit}).",
    },
    "diff.error.too_big_no_key.hint": {
        "uz": "Yozuvlarni kalit bo'yicha (--key id) yoki tartib raqami bo'yicha (--by-position) taqqoslang. "
              "Chegarani sozlamalarda o'zgartirish mumkin.",
        "en": "Compare the records by a key (--key id) or by position (--by-position). The limit can be "
              "changed in the settings.",
    },
    "diff.error.too_many_keys": {
        "uz": "Obyektda a'zolar juda ko'p ({count}; chegara {max}): uni bu usulda taqqoslab bo'lmaydi.",
        "en": "The object has too many members ({count}; the limit is {max}) to be compared this way.",
    },
    "diff.error.too_many_keys.hint": {
        "uz": "Bunday ma'lumotni kalit bo'yicha taqqoslang (masalan --key $key).",
        "en": "Compare such data by key (for example --key $key).",
    },
    "diff.error.format": {
        "uz": "Farqlar fayli formati noma'lum: {value}. Mumkin bo'lganlari: json, jsonl, csv.",
        "en": "Unknown format for the differences file: {value}. Possible: json, jsonl, csv.",
    },
    "diff.progress.scan": {"uz": "Taqqoslanmoqda: {a} va {b}", "en": "Comparing {a} and {b}"},
    "diff.progress.match": {
        "uz": "Yozuvlar juftlanmoqda: {a} va {b}",
        "en": "Matching the records of {a} and {b}",
    },
    "diff.progress.compare": {
        "uz": "Farqlar aniqlanmoqda: {a} va {b}",
        "en": "Finding the differences of {a} and {b}",
    },
    "diff.warn.partial": {
        "uz": "{name}: faylda JSON xatosi bor, faqat xatogacha bo'lgan {count} ta yozuv taqqoslandi.",
        "en": "{name}: the file has a JSON error; only the {count} records before it were compared.",
    },
    "diff.warn.partial.one": {
        "uz": "{name}: faylda JSON xatosi bor, faqat xatogacha bo'lgan {count} ta yozuv taqqoslandi.",
        "en": "{name}: the file has a JSON error; only the {count} record before it was compared.",
    },
    "diff.warn.broken_lines": {
        "uz": "{name}: {count} ta buzuq qator taqqoslanmadi.",
        "en": "{name}: {count} broken lines were not compared.",
    },
    "diff.warn.broken_lines.one": {
        "uz": "{name}: {count} ta buzuq qator taqqoslanmadi.",
        "en": "{name}: {count} broken line was not compared.",
    },
    "diff.warn.wrapper_too_large": {
        "uz": "Yozuvlar atrofidagi qism (o'rovchi) juda katta ({size}): u taqqoslanmadi.",
        "en": "The part around the records (the wrapper) is too large ({size}): it was not compared.",
    },
    "diff.warn.wrapper_one_side": {
        "uz": "{name}: yozuvlar atrofidagi qism (o'rovchi) taqqoslanmadi, chunki ikkinchi faylda u yo'q.",
        "en": "{name}: the part around the records (the wrapper) was not compared: the other file has none.",
    },
    "diff.warn.duplicate_keys": {
        "uz": "{name}: {key} ning {count} ta qiymati bir nechta yozuvda uchraydi; bunday yozuvlar tartib "
              "bo'yicha juftlandi (birinchisi birinchisi bilan, ikkinchisi ikkinchisi bilan).",
        "en": "{name}: {count} values of {key} occur in more than one record; such records were paired in "
              "order (the first with the first, the second with the second).",
    },
    "diff.warn.duplicate_keys.one": {
        "uz": "{name}: {key} ning {count} ta qiymati bir nechta yozuvda uchraydi; bunday yozuvlar tartib "
              "bo'yicha juftlandi (birinchisi birinchisi bilan, ikkinchisi ikkinchisi bilan).",
        "en": "{name}: {count} value of {key} occurs in more than one record; these records were paired in "
              "order (the first with the first, the second with the second).",
    },
    "diff.warn.no_record_keys": {
        "uz": "{name}: bu faylda yozuvlarning kaliti yo'q ($key faqat kalitli hujjatlarda ishlaydi): {count} ta "
              "yozuv taqqoslanmadi.",
        "en": "{name}: the records of this file have no keys ($key works only in keyed documents): {count} "
              "records were not compared.",
    },
    "diff.warn.no_record_keys.one": {
        "uz": "{name}: bu faylda yozuvlarning kaliti yo'q ($key faqat kalitli hujjatlarda ishlaydi): {count} ta "
              "yozuv taqqoslanmadi.",
        "en": "{name}: the records of this file have no keys ($key works only in keyed documents): {count} "
              "record was not compared.",
    },
    "diff.warn.missing_keys": {
        "uz": "{name}: {count} ta yozuvda {key} maydoni yo'q: ular taqqoslanmadi.",
        "en": "{name}: {count} records have no field {key}: they were not compared.",
    },
    "diff.warn.missing_keys.one": {
        "uz": "{name}: {count} ta yozuvda {key} maydoni yo'q: u taqqoslanmadi.",
        "en": "{name}: {count} record has no field {key}: it was not compared.",
    },
    "diff.warn.key_too_large": {
        "uz": "{name}: {count} ta yozuvning (birinchisi #{record}) kalit qiymati xotirada solishtirish uchun "
              "juda katta ({size} dan oshadi): ular taqqoslanmadi.",
        "en": "{name}: the key value of {count} records (the first is #{record}) is too large to compare in "
              "memory (over {size}): they were not compared.",
    },
    "diff.warn.key_too_large.one": {
        "uz": "{name}: {count} ta yozuvning (#{record}) kalit qiymati xotirada solishtirish uchun juda katta "
              "({size} dan oshadi): u taqqoslanmadi.",
        "en": "{name}: the key value of {count} record (#{record}) is too large to compare in memory "
              "(over {size}): it was not compared.",
    },
    "diff.warn.key_unreadable": {
        "uz": "{name}: {count} ta yozuvning (birinchisi #{record}) kalit qiymatini o'qib bo'lmadi (tafsilotlar "
              "log faylida): ular taqqoslanmadi.",
        "en": "{name}: the key value of {count} records (the first is #{record}) could not be read (details are "
              "in the log file): they were not compared.",
    },
    "diff.warn.key_unreadable.one": {
        "uz": "{name}: {count} ta yozuvning (#{record}) kalit qiymatini o'qib bo'lmadi (tafsilotlar log "
              "faylida): u taqqoslanmadi.",
        "en": "{name}: the key value of {count} record (#{record}) could not be read (details are in the log "
              "file): it was not compared.",
    },
    # ---- bulk ----
    # bulk operations (bulk.py): parameter checks, errors of a preview, problem reasons, examples, labels
    "bulk.error.kind": {"uz": "Noma'lum ommaviy amal: «{kind}».", "en": "Unknown bulk operation: \"{kind}\"."},
    "bulk.error.params": {
        "uz": "Ommaviy amal parametrlari noto'g'ri berilgan.",
        "en": "The parameters of the bulk operation are not valid.",
    },
    "bulk.error.unknown_param": {
        "uz": "Bu amalda «{param}» degan parametr yo'q.",
        "en": "This operation has no parameter \"{param}\".",
    },
    "bulk.error.missing_param": {
        "uz": "«{param}» parametri ko'rsatilmagan.",
        "en": "The parameter \"{param}\" is missing.",
    },
    "bulk.error.param_type": {
        "uz": "«{param}» parametri noto'g'ri: {expected} bo'lishi kerak.",
        "en": "The parameter \"{param}\" is not valid: {expected} is expected.",
    },
    "bulk.expect.text": {"uz": "matn", "en": "text"},
    "bulk.expect.bool": {"uz": "true yoki false", "en": "true or false"},
    "bulk.expect.path": {"uz": "yo'l (matn)", "en": "a path (text)"},
    "bulk.error.find_empty": {"uz": "Qidiriladigan matn bo'sh.", "en": "The text to find is empty."},
    "bulk.error.mode": {
        "uz": "Qidirish usuli noto'g'ri: «{value}» (text yoki regex bo'lishi kerak).",
        "en": "Invalid search mode \"{value}\" (it must be text or regex).",
    },
    "bulk.error.replacement": {
        "uz": "O'rniga qo'yiladigan matn noto'g'ri: {detail}.",
        "en": "The replacement text is not valid: {detail}.",
    },
    "bulk.error.replacement.hint": {
        "uz": "Regex rejimida \\1 yoki \\g<nom> topilgan guruhni qo'yadi; teskari chiziqning o'zi uchun \\\\ "
              "deb yozing.",
        "en": "In regex mode \\1 or \\g<name> inserts a matched group; write \\\\ for a backslash itself.",
    },
    "bulk.error.same_key": {
        "uz": "Yangi kalit eskisi bilan bir xil.",
        "en": "The new key is the same as the old one.",
    },
    "bulk.error.path_required": {"uz": "Yo'l ko'rsatilmagan.", "en": "No path was given."},
    "bulk.error.path_required.hint": {
        "uz": "Masalan: name, contact.city, orders[*].sum yoki ..id (istalgan chuqurlikda).",
        "en": "For example: name, contact.city, orders[*].sum or ..id (at any depth).",
    },
    "bulk.error.path_root": {
        "uz": "Bu amal uchun yozuvning o'zini emas, uning ichidagi maydon yo'lini ko'rsating.",
        "en": "Give the path of a field inside the records, not the record itself.",
    },
    "bulk.error.value": {
        "uz": "Qiymat JSON sifatida noto'g'ri ({line}-qator, {column}-ustun): {reason}.",
        "en": "The value is not valid JSON (line {line}, column {column}): {reason}.",
    },
    "bulk.error.value.hint": {
        "uz": "Matnni qo'shtirnoq ichida yozing, masalan \"Toshkent\". Son, true, false, null, massiv yoki obyekt "
              "ham bo'lishi mumkin.",
        "en": "Write text in double quotes, for example \"Tashkent\". A number, true, false, null, an array or an "
              "object is also possible.",
    },
    "bulk.error.set_mode": {
        "uz": "Qiymat berish rejimi noto'g'ri: «{value}» (always, if_missing yoki if_exists bo'lishi kerak).",
        "en": "Invalid mode \"{value}\" (it must be always, if_missing or if_exists).",
    },
    "bulk.error.clean_nothing": {
        "uz": "Tozalash turi tanlanmagan: chetdagi bo'sh joylarni olib tashlashni yoki ketma-ket bo'sh joylarni "
              "bittaga yig'ishni yoqing.",
        "en": "Nothing to clean: turn on trimming or collapsing runs of spaces.",
    },
    "bulk.error.type": {
        "uz": "Noma'lum tur: «{value}» (string, number, bool, null, object yoki array bo'lishi kerak).",
        "en": "Unknown type \"{value}\" (it must be string, number, bool, null, object or array).",
    },
    "bulk.error.partial": {
        "uz": "Faylda JSON xatosi bor: uni faqat ko'rish mumkin, ommaviy amalni bajarib bo'lmaydi.",
        "en": "The file has a JSON error: it can only be viewed, bulk operations are not possible.",
    },
    "bulk.error.stale_scope": {
        "uz": "Tanlangan yozuvlar ro'yxati eskirgan: ma'lumot u tuzilgandan keyin o'zgardi.",
        "en": "The selected records are out of date: the data changed after they were selected.",
    },
    "bulk.error.stale_scope.hint": {
        "uz": "Yozuvlarni qaytadan tanlang (filtr, saralash yoki qidiruvni yangilang) va amalni yana ko'rib chiqing.",
        "en": "Select the records again (refresh the filter, sort or search) and preview the operation again.",
    },
    "bulk.error.disk_space": {
        "uz": "Diskda joy yetarli emas: ommaviy amal uchun taxminan {need} kerak, bo'sh joy esa {free} ({path}).",
        "en": "Not enough disk space: the bulk operation needs about {need}, but only {free} is free ({path}).",
    },
    "bulk.error.disk_space.hint": {
        "uz": "Diskda joy bo'shating yoki kamroq yozuvni tanlang. Natija saqlanmagan o'zgarishlar papkasiga "
              "yoziladi.",
        "en": "Free some disk space or select fewer records. The result is written to the folder of unsaved "
              "changes.",
    },
    "bulk.error.nothing": {
        "uz": "Hech narsa o'zgarmaydi: qo'llash uchun o'zgarish yo'q.",
        "en": "Nothing would change: there is nothing to apply.",
    },
    "bulk.error.discarded": {
        "uz": "Bu oldindan ko'rish bekor qilingan, uni qo'llab bo'lmaydi.",
        "en": "This preview was discarded and cannot be applied.",
    },
    "bulk.error.discarded.hint": {"uz": "Amalni qaytadan ko'rib chiqing.", "en": "Preview the operation again."},
    # problem reasons (BulkPreview.problems[i].after)
    "bulk.problem.duplicate_keys": {
        "uz": "yozuvda takrorlangan kalitlar bor, u o'zgartirilmaydi",
        "en": "the record has duplicate keys, so it is not changed",
    },
    "bulk.problem.key_exists": {
        "uz": "obyektda «{key}» kaliti allaqachon bor",
        "en": "the object already has the key \"{key}\"",
    },
    "bulk.problem.not_object": {"uz": "{path} obyekt emas", "en": "{path} is not an object"},
    "bulk.problem.not_array": {"uz": "{path} massiv emas", "en": "{path} is not an array"},
    "bulk.problem.no_element": {
        "uz": "{path} massivida {index}-o'rinda element yo'q",
        "en": "the array {path} has no element at position {index}",
    },
    "bulk.problem.missing_array": {
        "uz": "{path} yo'q: massiv elementini yaratib bo'lmaydi",
        "en": "{path} does not exist: array elements cannot be created",
    },
    "bulk.problem.unreadable": {
        "uz": "yozuvni xotirada o'qib bo'lmadi (juda chuqur joylashgan)",
        "en": "the record could not be read in memory (it is nested too deeply)",
    },
    # example texts
    "bulk.example.deleted": {"uz": "(o'chiriladi)", "en": "(deleted)"},
    "bulk.example.missing": {"uz": "(yo'q)", "en": "(missing)"},
    "bulk.example.huge": {"uz": "(juda katta yozuv)", "en": "(very large record)"},
    "bulk.warn.huge": {
        "uz": "{count} ta yozuv {limit} dan katta, ular o'zgartirilmaydi (birinchisi: #{record}).",
        "en": "{count} records are larger than {limit} and are not changed (the first: #{record}).",
    },
    "bulk.progress.scan": {
        "uz": "Ommaviy amal oldindan ko'rilmoqda: {name}",
        "en": "Previewing the bulk operation: {name}",
    },
    "bulk.progress.layer": {"uz": "Natija yozilmoqda: {name}", "en": "Writing the result: {name}"},
    "bulk.progress.keys": {
        "uz": "O'chiriladigan yozuvlar o'qilmoqda: {name}",
        "en": "Reading the records to delete: {name}",
    },
    # operation names and one-line descriptions (BulkSpec.label)
    "bulk.kind.replace": {"uz": "Topish va almashtirish", "en": "Find and replace"},
    "bulk.kind.rename_key": {"uz": "Kalitni qayta nomlash", "en": "Rename key"},
    "bulk.kind.delete_field": {"uz": "Maydonni o'chirish", "en": "Delete field"},
    "bulk.kind.set_field": {"uz": "Maydonga qiymat berish", "en": "Set field"},
    "bulk.kind.clean_strings": {"uz": "Matnlarni tozalash", "en": "Clean strings"},
    "bulk.kind.convert_type": {"uz": "Turni o'zgartirish", "en": "Convert type"},
    "bulk.kind.dedupe": {"uz": "Takrorlarni o'chirish", "en": "Remove duplicates"},
    "bulk.label.replace": {
        "uz": "Topish va almashtirish: «{find}» → «{replacement}»{options}{where}",
        "en": "Find and replace: \"{find}\" → \"{replacement}\"{options}{where}",
    },
    "bulk.label.rename_key": {
        "uz": "Kalitni qayta nomlash: «{old}» → «{new}»{where}",
        "en": "Rename key: \"{old}\" → \"{new}\"{where}",
    },
    "bulk.label.delete_field": {"uz": "Maydonni o'chirish: {path}", "en": "Delete field: {path}"},
    "bulk.label.set_field": {
        "uz": "Maydonga qiymat berish: {path} = {value} ({mode})",
        "en": "Set field: {path} = {value} ({mode})",
    },
    "bulk.label.clean_strings": {"uz": "Matnlarni tozalash: {what}{where}", "en": "Clean strings: {what}{where}"},
    "bulk.label.convert_type": {"uz": "Turni o'zgartirish: {path} → {type}", "en": "Convert type: {path} → {type}"},
    "bulk.label.dedupe": {
        "uz": "Takrorlarni o'chirish ({key} bo'yicha, {keep})",
        "en": "Remove duplicates (by {key}, {keep})",
    },
    "bulk.label.where": {"uz": " ({path} ichida)", "en": " (in {path})"},
    "bulk.label.options": {"uz": " ({options})", "en": " ({options})"},
    "bulk.label.regex": {"uz": "regex", "en": "regex"},
    "bulk.label.case": {"uz": "katta-kichik harf farqlanadi", "en": "case-sensitive"},
    "bulk.label.whole_word": {"uz": "butun so'z", "en": "whole word"},
    "bulk.label.trim": {"uz": "chetdagi bo'sh joylar", "en": "trim"},
    "bulk.label.collapse": {"uz": "ketma-ket bo'sh joylar", "en": "collapse spaces"},
    "bulk.label.mode.always": {"uz": "har doim", "en": "always"},
    "bulk.label.mode.if_missing": {"uz": "faqat maydon yo'q bo'lsa", "en": "only where it is missing"},
    "bulk.label.mode.if_exists": {"uz": "faqat maydon bor bo'lsa", "en": "only where it exists"},
    "bulk.label.keep.first": {"uz": "birinchisi qoladi", "en": "keeping the first"},
    "bulk.label.keep.last": {"uz": "oxirgisi qoladi", "en": "keeping the last"},
    "bulk.label.record_key": {"uz": "yozuv kaliti", "en": "the record key"},
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
    # ---- stats ----
    # per-field statistics (stats.py): progress, argument errors, warnings, the CLI text block
    "stats.progress": {"uz": "Maydon statistikasi hisoblanmoqda: {name}", "en": "Computing field statistics: {name}"},
    "stats.progress.finish": {"uz": "Statistika yakunlanmoqda: {name}", "en": "Finishing the statistics: {name}"},
    "stats.error.fields": {
        "uz": "Statistika uchun maydonlar ro'yxatini bering, berilgan: {value}",
        "en": "Give a list of fields for the statistics, got: {value}",
    },
    "stats.error.no_fields": {
        "uz": "Statistika uchun kamida bitta maydonni ko'rsating.",
        "en": "Give at least one field for the statistics.",
    },
    "stats.error.no_fields.hint": {
        "uz": "Misollar: contact.address.city, orders[*].sum, ..name, $ (butun yozuv), $key (kalitli hujjatda "
              "yozuvning kaliti).",
        "en": "Examples: contact.address.city, orders[*].sum, ..name, $ (the whole record), $key (the record key in "
              "a keyed document).",
    },
    "stats.error.field": {
        "uz": "Maydon yo'li bo'sh bo'lmagan matn bo'lishi kerak, berilgan: {value}",
        "en": "A field must be a path given as text that is not empty, got: {value}",
    },
    "stats.error.top": {
        "uz": "Eng ko'p uchragan qiymatlar soni 0 dan {max} gacha butun son bo'lishi kerak, berilgan: {value}",
        "en": "The number of most frequent values must be a whole number from 0 to {max}, got: {value}",
    },
    "stats.error.sample": {
        "uz": "Namuna hajmi musbat butun son bo'lishi kerak, berilgan: {value}",
        "en": "The sample size must be a positive whole number, got: {value}",
    },
    "stats.error.memory_limit": {
        "uz": "Statistika uchun xotira chegarasi musbat butun son (baytlarda) bo'lishi kerak, berilgan: {value}",
        "en": "The memory limit for the statistics must be a positive whole number of bytes, got: {value}",
    },
    "stats.warn.no_record_keys": {
        "uz": "Bu hujjatda yozuvlarning kaliti yo'q: $key faqat yozuvlari obyekt a'zolari bo'lgan (kalitli) "
              "hujjatlarda ishlaydi. {count} ta yozuv kalitsiz hisoblandi.",
        "en": "The records of this document have no keys: $key works only in documents whose records are object "
              "members (keyed documents). {count} records were counted as having no key.",
    },
    "stats.warn.no_record_keys.one": {
        "uz": "Bu hujjatda yozuvlarning kaliti yo'q: $key faqat yozuvlari obyekt a'zolari bo'lgan (kalitli) "
              "hujjatlarda ishlaydi. {count} ta yozuv kalitsiz hisoblandi.",
        "en": "The records of this document have no keys: $key works only in documents whose records are object "
              "members (keyed documents). {count} record was counted as having no key.",
    },
    "stats.warn.field_absent": {
        "uz": "{field} maydoni birorta ham yozuvda topilmadi ({count} ta yozuv tekshirildi). Maydon nomi to'g'ri "
              "yozilganini tekshiring.",
        "en": "The field {field} was not found in any of the {count} records checked. Check the spelling of the "
              "field name.",
    },
    "stats.warn.field_absent.one": {
        "uz": "{field} maydoni birorta ham yozuvda topilmadi ({count} ta yozuv tekshirildi). Maydon nomi to'g'ri "
              "yozilganini tekshiring.",
        "en": "The field {field} was not found in the {count} record checked. Check the spelling of the field name.",
    },
    "stats.warn.too_large": {
        "uz": "{count} ta yozuvdagi (birinchisi #{record}) qiymatlar xotirada hisoblash uchun juda katta ({size} "
              "dan oshadi): bu yozuvlar statistikaga kirmadi.",
        "en": "The values in {count} records (the first is #{record}) are too large to count in memory (over "
              "{size}): these records are not in the statistics.",
    },
    "stats.warn.too_large.one": {
        "uz": "{count} ta yozuvdagi (#{record}) qiymatlar xotirada hisoblash uchun juda katta ({size} dan oshadi): "
              "bu yozuv statistikaga kirmadi.",
        "en": "The values in {count} record (#{record}) are too large to count in memory (over {size}): this "
              "record is not in the statistics.",
    },
    "stats.warn.unreadable": {
        "uz": "{count} ta yozuvning (birinchisi #{record}) qiymatlarini o'qib bo'lmadi (tafsilotlar log faylida): "
              "bu yozuvlar statistikaga kirmadi.",
        "en": "The values of {count} records (the first is #{record}) could not be read (details are in the log "
              "file): these records are not in the statistics.",
    },
    "stats.warn.unreadable.one": {
        "uz": "{count} ta yozuvning (#{record}) qiymatlarini o'qib bo'lmadi (tafsilotlar log faylida): bu yozuv "
              "statistikaga kirmadi.",
        "en": "The values of {count} record (#{record}) could not be read (details are in the log file): this "
              "record is not in the statistics.",
    },
    "stats.warn.uncounted": {
        "uz": "{count} ta obyekt yoki massiv qiymati (birinchisi #{record} yozuvda) solishtirish uchun juda katta "
              "({size} dan oshadi): ular turlar va uzunliklarda hisoblandi, lekin noyob qiymatlar va eng ko'p "
              "uchraganlar ro'yxatiga kirmadi.",
        "en": "{count} object or array values (the first in record #{record}) are too large to compare (over "
              "{size}): they are counted in the types and lengths, but not in the distinct values and the most "
              "frequent values.",
    },
    "stats.warn.uncounted.one": {
        "uz": "{count} ta obyekt yoki massiv qiymati (#{record} yozuvda) solishtirish uchun juda katta ({size} dan "
              "oshadi): u turlar va uzunliklarda hisoblandi, lekin noyob qiymatlar va eng ko'p uchraganlar "
              "ro'yxatiga kirmadi.",
        "en": "{count} object or array value (in record #{record}) is too large to compare (over {size}): it is "
              "counted in the types and lengths, but not in the distinct values and the most frequent values.",
    },
    "stats.warn.approximate": {
        "uz": "Noyob qiymatlar juda ko'p, shuning uchun ular taxminiy hisoblandi: noyob qiymatlar soni "
              "HyperLogLog bilan (±{rse} %), eng ko'p uchraganlarning har birining haqiqiy soni ko'rsatilganidan "
              "{bound} tagacha ko'p bo'lishi mumkin. Aniq natija uchun \"Aniq hisoblash\" (--exact) ni tanlang.",
        "en": "There are too many distinct values, so they were counted approximately: the number of distinct "
              "values with HyperLogLog (±{rse} %), and the true count of each most frequent value may be up to "
              "{bound} higher than shown. Choose \"Exact count\" (--exact) for exact results.",
    },
    "stats.warn.sum_approximate": {
        "uz": "Sonlar yig'indisini aniq hisoblash uchun {digits} dan ortiq raqam kerak bo'lardi (juda katta va "
              "juda kichik sonlar aralash): yig'indi va o'rtacha 50 ta muhim raqam bilan taxminiy berildi.",
        "en": "The exact sum of the numbers would need more than {digits} digits (very large and very small "
              "numbers mixed): the sum and the mean are given approximately, with 50 significant digits.",
    },
    "stats.warn.sum_skipped": {
        "uz": "{count} ta son yig'indi va o'rtachaga qo'shilmadi: ularning darajasi aniq qo'shish uchun juda "
              "katta (masalan, 1e99999999999999999999). Yig'indi va o'rtacha qolgan sonlar bo'yicha hisoblandi.",
        "en": "{count} numbers were left out of the sum and the mean: their exponent is too large to add them "
              "exactly (e.g. 1e99999999999999999999). The sum and the mean are over the other numbers.",
    },
    "stats.warn.sum_skipped.one": {
        "uz": "{count} ta son yig'indi va o'rtachaga qo'shilmadi: uning darajasi aniq qo'shish uchun juda katta "
              "(masalan, 1e99999999999999999999). Yig'indi va o'rtacha qolgan sonlar bo'yicha hisoblandi.",
        "en": "{count} number was left out of the sum and the mean: its exponent is too large to add it exactly "
              "(e.g. 1e99999999999999999999). The sum and the mean are over the other numbers.",
    },
    "stats.text.field": {
        "uz": "Maydon: {field}    {records} ta yozuv · {seconds} s",
        "en": "Field: {field}    {records} records · {seconds} s",
    },
    "stats.text.field.one": {
        "uz": "Maydon: {field}    {records} ta yozuv · {seconds} s",
        "en": "Field: {field}    {records} record · {seconds} s",
    },
    "stats.text.context": {
        "uz": "Kontekst: {context} ({count} ta element)",
        "en": "Context: {context} ({count} elements)",
    },
    "stats.text.context.one": {
        "uz": "Kontekst: {context} ({count} ta element)",
        "en": "Context: {context} ({count} element)",
    },
    "stats.text.presence": {
        "uz": "Mavjud: {present} ({present_pct})    null: {nulls} ({nulls_pct})    yo'q: {missing} ({missing_pct})",
        "en": "Present: {present} ({present_pct})    null: {nulls} ({nulls_pct})    missing: {missing} "
              "({missing_pct})",
    },
    "stats.text.types": {"uz": "Turlar: {types}", "en": "Types: {types}"},
    "stats.text.distinct_exact": {"uz": "Noyob qiymatlar: {count} (aniq)", "en": "Distinct values: {count} (exact)"},
    "stats.text.distinct_approx": {
        "uz": "Noyob qiymatlar: ≈ {count} (HyperLogLog, ±{rse} %)",
        "en": "Distinct values: ≈ {count} (HyperLogLog, ±{rse} %)",
    },
    "stats.text.numbers": {
        "uz": "Sonlar: {count} ta (butun: {integers}) · min {min} · max {max} · o'rtacha {mean} · yig'indi {sum}",
        "en": "Numbers: {count} (integers: {integers}) · min {min} · max {max} · mean {mean} · sum {sum}",
    },
    "stats.text.strings": {
        "uz": "Satr uzunligi: {min}–{max} (bo'sh satrlar: {empty})",
        "en": "String length: {min}–{max} (empty strings: {empty})",
    },
    "stats.text.arrays": {"uz": "Massiv uzunligi: {min}–{max}", "en": "Array length: {min}–{max}"},
    "stats.text.objects": {"uz": "Obyekt kalitlari soni: {min}–{max}", "en": "Object keys: {min}–{max}"},
    "stats.text.top_exact": {
        "uz": "Eng ko'p uchragan {count} ta qiymat:",
        "en": "The {count} most frequent values:",
    },
    "stats.text.top_exact.one": {
        "uz": "Eng ko'p uchragan qiymat:",
        "en": "The most frequent value:",
    },
    "stats.text.top_approx": {
        "uz": "Eng ko'p uchragan {count} ta qiymat (taxminiy: har birining haqiqiy soni {bound} tagacha ko'p "
              "bo'lishi mumkin):",
        "en": "The {count} most frequent values (approximate: each true count may be up to {bound} higher):",
    },
    "stats.text.top_approx.one": {
        "uz": "Eng ko'p uchragan qiymat (taxminiy: haqiqiy soni {bound} tagacha ko'p bo'lishi mumkin):",
        "en": "The most frequent value (approximate: the true count may be up to {bound} higher):",
    },
    "stats.text.records": {"uz": "({count} ta yozuvda)", "en": "(in {count} records)"},
    "stats.text.records.one": {"uz": "({count} ta yozuvda)", "en": "(in {count} record)"},
    # histograms (stage 7): bin labels and the text block of format_histogram
    "stats.hist.single": {"uz": "{value}", "en": "{value}"},
    "stats.hist.range_int": {"uz": "{lo} – {hi}", "en": "{lo} – {hi}"},
    "stats.hist.range": {"uz": "{lo} ≤ x < {hi}", "en": "{lo} ≤ x < {hi}"},
    "stats.hist.range_last": {"uz": "{lo} ≤ x ≤ {hi}", "en": "{lo} ≤ x ≤ {hi}"},
    "stats.hist.title": {
        "uz": "Taqsimot: {bins} ta oraliq, {count} ta qiymat (aniq):",
        "en": "Distribution: {bins} bins, {count} values (exact):",
    },
    "stats.hist.title.one": {
        "uz": "Taqsimot: bitta oraliq, {count} ta qiymat (aniq):",
        "en": "Distribution: one bin, {count} values (exact):",
    },
    "stats.hist.title_sampled": {
        "uz": "Taqsimot: {bins} ta oraliq, {count} ta qiymat (≈ {sample} ta qiymatdan iborat namuna bo'yicha):",
        "en": "Distribution: {bins} bins, {count} values (≈ from a sample of {sample} values):",
    },
    "stats.hist.title_sampled.one": {
        "uz": "Taqsimot: bitta oraliq, {count} ta qiymat (≈ {sample} ta qiymatdan iborat namuna bo'yicha):",
        "en": "Distribution: one bin, {count} values (≈ from a sample of {sample} values):",
    },
    # ---- smartvalues ----
    # smart values (smartvalues.py): tooltips of times, links, colours and images
    "smartvalues.unix_s": {"uz": "Unix vaqti (soniyalarda)", "en": "Unix time (seconds)"},
    "smartvalues.unix_ms": {"uz": "Unix vaqti (millisoniyalarda)", "en": "Unix time (milliseconds)"},
    "smartvalues.datetime": {"uz": "Sana va vaqt (ISO 8601)", "en": "Date and time (ISO 8601)"},
    "smartvalues.date": {"uz": "Sana (ISO 8601)", "en": "Date (ISO 8601)"},
    "smartvalues.tashkent": {
        "uz": "Toshkent vaqti: {date} {time}, {weekday} (UTC+05:00)",
        "en": "Tashkent time: {date} {time}, {weekday} (UTC+05:00)",
    },
    "smartvalues.utc": {"uz": "UTC: {date} {time}", "en": "UTC: {date} {time}"},
    "smartvalues.naive": {
        "uz": "{date} {time}, {weekday} (vaqt mintaqasi ko'rsatilmagan)",
        "en": "{date} {time}, {weekday} (no time zone given)",
    },
    "smartvalues.date_line": {"uz": "{date}, {weekday}", "en": "{date}, {weekday}"},
    "smartvalues.old_offset": {
        "uz": "{year} yilgacha Toshkentda boshqa vaqt qoidalari amal qilgan: vaqt UTC+05:00 bo'yicha ko'rsatildi.",
        "en": "Before {year} Tashkent used other time rules: the time is shown for UTC+05:00.",
    },
    "smartvalues.weekday.0": {"uz": "dushanba", "en": "Monday"},
    "smartvalues.weekday.1": {"uz": "seshanba", "en": "Tuesday"},
    "smartvalues.weekday.2": {"uz": "chorshanba", "en": "Wednesday"},
    "smartvalues.weekday.3": {"uz": "payshanba", "en": "Thursday"},
    "smartvalues.weekday.4": {"uz": "juma", "en": "Friday"},
    "smartvalues.weekday.5": {"uz": "shanba", "en": "Saturday"},
    "smartvalues.weekday.6": {"uz": "yakshanba", "en": "Sunday"},
    "smartvalues.url": {
        "uz": "Havola: {url}\nCtrl+bosish: brauzerda ochish (avval so'raladi; dasturning o'zi hech narsa "
              "yubormaydi)",
        "en": "Link: {url}\nCtrl+click: open it in the browser (you are asked first; the program itself sends "
              "nothing)",
    },
    "smartvalues.email": {
        "uz": "E-pochta manzili: {address}\nCtrl+bosish: pochta dasturida ochish (avval so'raladi)",
        "en": "E-mail address: {address}\nCtrl+click: open it in the mail program (you are asked first)",
    },
    "smartvalues.color": {"uz": "Rang: {hex} (R {r}, G {g}, B {b})", "en": "Colour: {hex} (R {r}, G {g}, B {b})"},
    "smartvalues.color_alpha": {
        "uz": "Rang: {hex} (R {r}, G {g}, B {b}, ko'rinish darajasi {alpha} %)",
        "en": "Colour: {hex} (R {r}, G {g}, B {b}, opacity {alpha} %)",
    },
    "smartvalues.image": {
        "uz": "Rasm: {format}, {width}×{height} piksel",
        "en": "Image: {format}, {width}×{height} pixels",
    },
    "smartvalues.image_unknown_size": {"uz": "Rasm: {format}", "en": "Image: {format}"},
    "smartvalues.image_hint": {
        "uz": "To'liq qiymat oynasida rasmning kichik nusxasi ko'rinadi.",
        "en": "The full-value window shows a small copy of the image.",
    },
    # ---- jschema ----
    "jschema.type.object": {"uz": "obyekt", "en": "object"},
    "jschema.type.array": {"uz": "massiv", "en": "array"},
    "jschema.type.string": {"uz": "satr", "en": "string"},
    "jschema.type.number": {"uz": "son", "en": "number"},
    "jschema.type.integer": {"uz": "butun son", "en": "integer"},
    "jschema.type.boolean": {"uz": "mantiqiy qiymat", "en": "boolean"},
    "jschema.type.null": {"uz": "null", "en": "null"},
    "jschema.error.generic": {"uz": "Qiymat schemaga mos emas.", "en": "The value does not match the schema."},
    "jschema.error.false": {
        "uz": "Bu joyda hech qanday qiymatga ruxsat yo'q (schemada false).",
        "en": "No value is allowed here (the schema is false).",
    },
    "jschema.error.type": {
        "uz": "Turi noto'g'ri: {expected} kutilgan, {actual} berilgan.",
        "en": "Wrong type: expected {expected}, got {actual}.",
    },
    "jschema.error.enum": {
        "uz": "Qiymat ruxsat etilganlar ro'yxatida yo'q: {value}. Ruxsat etilgan qiymatlar: {allowed}.",
        "en": "The value is not in the allowed list: {value}. Allowed values: {allowed}.",
    },
    "jschema.error.const": {
        "uz": "Qiymat aynan {expected} bo'lishi kerak, berilgan: {value}.",
        "en": "The value must be exactly {expected}, got: {value}.",
    },
    "jschema.error.multipleOf": {
        "uz": "Son {divisor} soniga karrali bo'lishi kerak, berilgan: {value}.",
        "en": "The number must be a multiple of {divisor}, got: {value}.",
    },
    "jschema.error.minimum": {
        "uz": "Son juda kichik: {value}. Eng kichik ruxsat etilgan qiymat: {limit}.",
        "en": "The number is too small: {value}. The minimum is {limit}.",
    },
    "jschema.error.maximum": {
        "uz": "Son juda katta: {value}. Eng katta ruxsat etilgan qiymat: {limit}.",
        "en": "The number is too large: {value}. The maximum is {limit}.",
    },
    "jschema.error.exclusiveMinimum": {
        "uz": "Son {limit} sonidan katta bo'lishi kerak, berilgan: {value}.",
        "en": "The number must be greater than {limit}, got: {value}.",
    },
    "jschema.error.exclusiveMaximum": {
        "uz": "Son {limit} sonidan kichik bo'lishi kerak, berilgan: {value}.",
        "en": "The number must be less than {limit}, got: {value}.",
    },
    "jschema.error.minLength": {
        "uz": "Satr juda qisqa: {length} ta belgi, kamida {limit} ta belgi kerak.",
        "en": "The string is too short: {length} characters, at least {limit} required.",
    },
    "jschema.error.maxLength": {
        "uz": "Satr juda uzun: {length} ta belgi, ko'pi bilan {limit} ta belgi bo'lishi mumkin.",
        "en": "The string is too long: {length} characters, at most {limit} allowed.",
    },
    "jschema.error.pattern": {
        "uz": "Satr andozaga mos kelmaydi: {pattern}.",
        "en": "The string does not match the pattern {pattern}.",
    },
    "jschema.error.format": {
        "uz": "Satr {format} formatiga mos emas.",
        "en": "The string is not a valid {format}.",
    },
    "jschema.error.minItems": {
        "uz": "Massivda {count} ta element bor, kamida {limit} ta kerak.",
        "en": "The array has {count} items, at least {limit} required.",
    },
    "jschema.error.maxItems": {
        "uz": "Massivda {count} ta element bor, ko'pi bilan {limit} ta bo'lishi mumkin.",
        "en": "The array has {count} items, at most {limit} allowed.",
    },
    "jschema.error.uniqueItems": {
        "uz": "Massiv elementlari takrorlanmasligi kerak: {first} va {second} elementlar bir xil.",
        "en": "The array items must be unique: items {first} and {second} are equal.",
    },
    "jschema.error.prefixItems": {
        "uz": "Massivning {index} elementiga ruxsat yo'q (schemada false).",
        "en": "Item {index} is not allowed (the schema is false).",
    },
    "jschema.error.items": {
        "uz": "Ortiqcha element {index}: massivda ko'pi bilan {limit} ta element bo'lishi mumkin.",
        "en": "Extra item {index}: the array may have at most {limit} items.",
    },
    "jschema.error.contains": {
        "uz": "Massivda shartga mos elementlar soni {count} ta, kamida {limit} ta kerak.",
        "en": "{count} items match the condition, at least {limit} required.",
    },
    "jschema.error.maxContains": {
        "uz": "Massivda shartga mos elementlar soni {count} ta, ko'pi bilan {limit} ta bo'lishi mumkin.",
        "en": "{count} items match the condition, at most {limit} allowed.",
    },
    "jschema.error.minProperties": {
        "uz": "Obyektda {count} ta maydon bor, kamida {limit} ta kerak.",
        "en": "The object has {count} properties, at least {limit} required.",
    },
    "jschema.error.maxProperties": {
        "uz": "Obyektda {count} ta maydon bor, ko'pi bilan {limit} ta bo'lishi mumkin.",
        "en": "The object has {count} properties, at most {limit} allowed.",
    },
    "jschema.error.required": {
        "uz": "Majburiy maydon yo'q: {property}.",
        "en": "A required property is missing: {property}.",
    },
    "jschema.error.dependentRequired": {
        "uz": "{property} maydoni bor, shuning uchun {required} maydoni ham bo'lishi kerak.",
        "en": "Property {property} is present, so {required} is required too.",
    },
    "jschema.error.dependentSchemas": {
        "uz": "{property} maydoni bor, bu holda schema obyektga ruxsat bermaydi (false).",
        "en": "Property {property} is present, and then the schema allows no object (false).",
    },
    "jschema.error.properties": {
        "uz": "Bu maydonga ruxsat yo'q: {property} (schemada false).",
        "en": "Property {property} is not allowed (the schema is false).",
    },
    "jschema.error.patternProperties": {
        "uz": "Bu maydonga ruxsat yo'q: {property} (nomi {pattern} andozasiga mos keladi).",
        "en": "Property {property} is not allowed (its name matches {pattern}).",
    },
    "jschema.error.additionalProperties": {
        "uz": "Ortiqcha maydon: {property}. Schema bunday maydonga ruxsat bermaydi.",
        "en": "Unexpected property: {property}. The schema does not allow it.",
    },
    "jschema.error.unevaluatedProperties": {
        "uz": "Ortiqcha maydon: {property}. Schemaning hech bir qismi unga ruxsat bermaydi.",
        "en": "Unexpected property: {property}. No part of the schema allows it.",
    },
    "jschema.error.unevaluatedItems": {
        "uz": "Ortiqcha element {index}: schemaning hech bir qismi unga ruxsat bermaydi.",
        "en": "Unexpected item {index}: no part of the schema allows it.",
    },
    "jschema.error.propertyNames": {
        "uz": "Maydon nomi qoidaga mos emas: {property}.",
        "en": "The property name is not allowed: {property}.",
    },
    "jschema.error.not": {
        "uz": "Qiymat taqiqlangan shartga mos keladi (not).",
        "en": "The value matches a forbidden condition (not).",
    },
    "jschema.error.anyOf": {
        "uz": "Qiymat {count} ta variantning birortasiga ham mos kelmaydi.",
        "en": "The value matches none of the {count} alternatives.",
    },
    "jschema.error.oneOf": {
        "uz": "Qiymat aniq bitta variantga mos kelishi kerak, lekin {matched} ta variantga mos keladi.",
        "en": "The value must match exactly one alternative, but it matches {matched}.",
    },
    "jschema.error.document.false": {
        "uz": "Schema bu hujjatning yozuvlariga ruxsat bermaydi (false).",
        "en": "The schema does not allow the records of this document (false).",
    },
    "jschema.error.document.type": {
        "uz": "Hujjat turi schemaga mos emas: {expected} kutilgan, yozuvlar esa {actual} ko'rinishida.",
        "en": "The document type does not match the schema: expected {expected}, the records form an {actual}.",
    },
    "jschema.error.document.minItems": {
        "uz": "Hujjatda {count} ta yozuv bor, kamida {limit} ta kerak.",
        "en": "The document has {count} records, at least {limit} required.",
    },
    "jschema.error.document.maxItems": {
        "uz": "Hujjatda {count} ta yozuv bor, ko'pi bilan {limit} ta bo'lishi mumkin.",
        "en": "The document has {count} records, at most {limit} allowed.",
    },
    "jschema.error.document.contains": {
        "uz": "Shartga mos yozuvlar soni {count} ta, kamida {limit} ta kerak.",
        "en": "{count} records match the condition, at least {limit} required.",
    },
    "jschema.error.document.maxContains": {
        "uz": "Shartga mos yozuvlar soni {count} ta, ko'pi bilan {limit} ta bo'lishi mumkin.",
        "en": "{count} records match the condition, at most {limit} allowed.",
    },
    "jschema.error.document.minProperties": {
        "uz": "Hujjatda {count} ta turli kalit bor, kamida {limit} ta kerak.",
        "en": "The document has {count} distinct keys, at least {limit} required.",
    },
    "jschema.error.document.maxProperties": {
        "uz": "Hujjatda {count} ta turli kalit bor, ko'pi bilan {limit} ta bo'lishi mumkin.",
        "en": "The document has {count} distinct keys, at most {limit} allowed.",
    },
    "jschema.error.document.required": {
        "uz": "Majburiy kalitli yozuv yo'q: {property}.",
        "en": "A record with a required key is missing: {property}.",
    },
    "jschema.error.document.dependentRequired": {
        "uz": "{property} kalitli yozuv bor, shuning uchun {required} kalitli yozuv ham bo'lishi kerak.",
        "en": "A record with key {property} exists, so a record with key {required} is required too.",
    },
    "jschema.error.document.uniqueItems": {
        "uz": "Bu yozuv avvalgi yozuvlardan biri bilan aynan bir xil (yozuvlar takrorlanmasligi kerak).",
        "en": "This record is equal to an earlier record (the records must be unique).",
    },
    "jschema.error.document.uniqueItems.first": {
        "uz": "Bu yozuv {first} yozuv bilan aynan bir xil (yozuvlar takrorlanmasligi kerak).",
        "en": "This record is equal to record {first} (the records must be unique).",
    },
    "jschema.error.schema.keyword": {
        "uz": "Schemadagi {keyword} qiymati noto'g'ri ({location}): {expected} bo'lishi kerak.",
        "en": "The schema's {keyword} value is invalid ({location}): it must be {expected}.",
    },
    "jschema.error.schema.keyword.hint": {
        "uz": "Schema faylini tuzatib, qaytadan urinib ko'ring.",
        "en": "Fix the schema file and try again.",
    },
    "jschema.error.schema.dialect": {
        "uz": "Faqat JSON Schema draft 2020-12 qo'llab-quvvatlanadi, schemada esa $schema = {value} ({location}).",
        "en": "Only JSON Schema draft 2020-12 is supported, but the schema says $schema = {value} ({location}).",
    },
    "jschema.error.schema.dialect.hint": {
        "uz": "Schema 2020-12 qoidalariga mos bo'lsa, $schema qiymatini "
              "https://json-schema.org/draft/2020-12/schema qiling yoki uni olib tashlang.",
        "en": "If the schema follows the 2020-12 rules, set $schema to "
              "https://json-schema.org/draft/2020-12/schema or remove it.",
    },
    "jschema.error.schema.id": {
        "uz": "$id qiymatida # belgisidan keyin hech narsa bo'lmasligi kerak: {value} ({location}).",
        "en": "$id must not have a fragment after #: {value} ({location}).",
    },
    "jschema.error.schema.dynamic": {
        "uz": "{keyword} qo'llab-quvvatlanmaydi ({location}).",
        "en": "{keyword} is not supported ({location}).",
    },
    "jschema.error.schema.dynamic.hint": {
        "uz": "Uning o'rniga oddiy $ref ishlating.",
        "en": "Use a plain $ref instead.",
    },
    "jschema.error.schema.vocabulary": {
        "uz": "Schema noma'lum lug'atni (vocabulary) talab qiladi: {vocabulary}.",
        "en": "The schema requires an unknown vocabulary: {vocabulary}.",
    },
    "jschema.error.schema.remote_ref": {
        "uz": "Tashqi havola qo'llab-quvvatlanmaydi: $ref = {ref} ({location}). Dastur internetdan yoki "
              "boshqa fayllardan hech narsa yuklamaydi.",
        "en": "External references are not supported: $ref = {ref} ({location}). The program never "
              "downloads or reads other schemas.",
    },
    "jschema.error.schema.remote_ref.hint": {
        "uz": "Havola qilingan schemani shu faylning $defs bo'limiga ko'chiring va $ref ni #/$defs/... "
              "ko'rinishida yozing.",
        "en": "Copy the referenced schema into this file's $defs and write the $ref as #/$defs/...",
    },
    "jschema.error.schema.unresolved_ref": {
        "uz": "$ref ko'rsatgan joy schemada topilmadi: {ref} ({location}).",
        "en": "The $ref target was not found in the schema: {ref} ({location}).",
    },
    "jschema.error.schema.not_schema": {
        "uz": "Bu joyda schema (obyekt yoki true/false) bo'lishi kerak ({location}).",
        "en": "A schema (an object or true/false) is expected here ({location}).",
    },
    "jschema.error.schema.regex": {
        "uz": "Muntazam ifoda noto'g'ri yoki qo'llab-quvvatlanmaydi: {pattern} ({location}): {detail}.",
        "en": "The regular expression is invalid or not supported: {pattern} ({location}): {detail}.",
    },
    "jschema.error.schema.ref_cycle": {
        "uz": "Schemada cheksiz $ref aylanasi bor: tekshirish hech qachon tugamaydi ({location}).",
        "en": "The schema has an endless $ref cycle: validation would never finish ({location}).",
    },
    "jschema.error.schema.json": {
        "uz": "Schema JSON sifatida o'qilmadi: {detail}.",
        "en": "The schema is not valid JSON: {detail}.",
    },
    "jschema.error.schema_file": {
        "uz": "Schema faylini o'qib bo'lmadi: {path}: {detail}.",
        "en": "The schema file could not be read: {path}: {detail}.",
    },
    "jschema.detail.too_big": {
        "uz": "fayl juda katta (chegara {limit})",
        "en": "the file is too large (limit {limit})",
    },
    "jschema.expected.uri": {"uz": "URI matni", "en": "a URI string"},
    "jschema.expected.object": {"uz": "obyekt", "en": "an object"},
    "jschema.expected.anchor": {
        "uz": "harf yoki _ bilan boshlanadigan nom (harflar, raqamlar, - . _)",
        "en": "a name that starts with a letter or _ (letters, digits, - . _)",
    },
    "jschema.expected.regex": {"uz": "muntazam ifoda (satr)", "en": "a regular expression (string)"},
    "jschema.expected.count": {"uz": "manfiy bo'lmagan butun son", "en": "a non-negative integer"},
    "jschema.expected.number": {"uz": "son", "en": "a number"},
    "jschema.expected.positive": {"uz": "noldan katta son", "en": "a number greater than zero"},
    "jschema.expected.schema": {"uz": "schema (obyekt yoki true/false)", "en": "a schema (an object or true/false)"},
    "jschema.expected.schema_list": {"uz": "schemalarning bo'sh bo'lmagan massivi", "en": "a non-empty array of schemas"},
    "jschema.expected.schema_map": {"uz": "qiymatlari schema bo'lgan obyekt", "en": "an object whose values are schemas"},
    "jschema.expected.string_list": {"uz": "takrorlanmaydigan satrlar massivi", "en": "an array of unique strings"},
    "jschema.expected.string_list_map": {
        "uz": "qiymatlari takrorlanmaydigan satrlar massivi bo'lgan obyekt",
        "en": "an object whose values are arrays of unique strings",
    },
    "jschema.expected.type": {
        "uz": "tur nomi yoki takrorlanmaydigan tur nomlari massivi (object, array, string, number, integer, "
              "boolean, null)",
        "en": "a type name or an array of unique type names (object, array, string, number, integer, boolean, null)",
    },
    "jschema.expected.array": {"uz": "massiv", "en": "an array"},
    "jschema.expected.string": {"uz": "satr", "en": "a string"},
    "jschema.expected.boolean": {"uz": "true yoki false", "en": "true or false"},
    "jschema.error.scope": {
        "uz": "Tekshirish doirasi noto'g'ri: {value}. Mumkin qiymatlar: auto, record, document.",
        "en": "Invalid validation scope: {value}. Possible values: auto, record, document.",
    },
    "jschema.error.limit": {
        "uz": "{name} qiymati noto'g'ri: {value}. Butun son kerak.",
        "en": "Invalid {name}: {value}. A whole number is required.",
    },
    "jschema.error.wrapper_too_large": {
        "uz": "Hujjatning yozuvlar atrofidagi qismi juda katta (chegara {limit}), uni hujjat darajasida "
              "tekshirib bo'lmaydi.",
        "en": "The part of the document around the records is too large (limit {limit}) to be checked "
              "at the document level.",
    },
    "jschema.error.wrapper_too_large.hint": {
        "uz": "Yozuvlarni alohida tekshiring (doira: record).",
        "en": "Validate the records one by one (scope: record).",
    },
    "jschema.error.wrapper_missing": {
        "uz": "Hujjatning yozuvlar atrofidagi qismi to'liq emas, uni hujjat darajasida tekshirib bo'lmaydi.",
        "en": "The part of the document around the records is incomplete, so it cannot be checked at the "
              "document level.",
    },
    "jschema.error.wrapper_missing.hint": {
        "uz": "Yozuvlarni alohida tekshiring (doira: record) yoki faylni avval ta'mirlang.",
        "en": "Validate the records one by one (scope: record) or repair the file first.",
    },
    "jschema.error.document_too_large": {
        "uz": "Schemaning hujjat darajasidagi {keyword} shartini yozuvma-yozuv tekshirib bo'lmaydi, hujjat esa "
              "butunligicha tekshirish uchun juda katta ({size}; chegara {limit}).",
        "en": "The schema's document-level {keyword} cannot be checked record by record, and the document is "
              "too large to be checked as a whole ({size}; limit {limit}).",
    },
    "jschema.error.document_too_large.hint": {
        "uz": "Yozuvlarni alohida tekshiring (doira: record) yoki schemani soddalashtiring.",
        "en": "Validate the records one by one (scope: record) or simplify the schema.",
    },
    "jschema.error.document_unreadable": {
        "uz": "Hujjatni butunligicha tekshirib bo'lmadi: {count} ta yozuvni o'qib bo'lmadi.",
        "en": "The document could not be checked as a whole: {count} records could not be read.",
    },
    "jschema.error.document_too_deep": {
        "uz": "Hujjat juda chuqur ichma-ich tuzilgan, uni butunligicha tekshirib bo'lmadi.",
        "en": "The document is nested too deeply to be checked as a whole.",
    },
    "jschema.warn.partial": {
        "uz": "Faylda JSON xatosi bor: faqat xatogacha bo'lgan yozuvlar tekshirildi.",
        "en": "The file has a JSON error: only the records before it were checked.",
    },
    "jschema.warn.broken_lines": {
        "uz": "{count} ta buzilgan qator JSON emas va tekshirilmadi.",
        "en": "{count} broken lines are not JSON and were not checked.",
    },
    "jschema.warn.broken_lines.one": {
        "uz": "{count} ta buzilgan qator JSON emas va tekshirilmadi.",
        "en": "{count} broken line is not JSON and was not checked.",
    },
    "jschema.warn.aggregates_skipped": {
        "uz": "Hujjat darajasidagi shartlar (yozuvlar soni, takrorlanish, majburiy kalitlar) faqat barcha "
              "yozuvlar tekshirilganda tekshiriladi.",
        "en": "Document-level conditions (record count, duplicates, required keys) are checked only when all "
              "records are checked.",
    },
    "jschema.warn.whole_document": {
        "uz": "Schemadagi {keyword} sharti tufayli hujjat butunligicha (barcha yozuvlar) tekshirildi.",
        "en": "Because of the schema's {keyword}, the document was checked as a whole (all records).",
    },
    "jschema.warn.too_large": {
        "uz": "{count} ta yozuv juda katta (chegara {size}) va tekshirilmadi; birinchisi: {record}.",
        "en": "{count} records are too large (limit {size}) and were not checked; the first one: {record}.",
    },
    "jschema.warn.too_large.one": {
        "uz": "{count} ta yozuv juda katta (chegara {size}) va tekshirilmadi: {record}.",
        "en": "{count} record is too large (limit {size}) and was not checked: {record}.",
    },
    "jschema.warn.unreadable": {
        "uz": "{count} ta yozuvni o'qib bo'lmadi va ular tekshirilmadi; birinchisi: {record}.",
        "en": "{count} records could not be read and were not checked; the first one: {record}.",
    },
    "jschema.warn.unreadable.one": {
        "uz": "{count} ta yozuvni o'qib bo'lmadi va u tekshirilmadi: {record}.",
        "en": "{count} record could not be read and was not checked: {record}.",
    },
    "jschema.warn.too_deep": {
        "uz": "{count} ta yozuv juda chuqur ichma-ich tuzilgan va tekshirilmadi; birinchisi: {record}.",
        "en": "{count} records are nested too deeply and were not checked; the first one: {record}.",
    },
    "jschema.warn.too_deep.one": {
        "uz": "{count} ta yozuv juda chuqur ichma-ich tuzilgan va tekshirilmadi: {record}.",
        "en": "{count} record is nested too deeply and was not checked: {record}.",
    },
    "jschema.warn.per_record": {
        "uz": "{count} ta yozuvda xatolar ko'p: har bir yozuv uchun faqat dastlabki {limit} tasi ko'rsatildi.",
        "en": "{count} records have many issues: only the first {limit} per record are listed.",
    },
    "jschema.warn.per_record.one": {
        "uz": "{count} ta yozuvda xatolar ko'p: faqat dastlabki {limit} tasi ko'rsatildi.",
        "en": "{count} record has many issues: only the first {limit} are listed.",
    },
    "jschema.warn.issues_truncated": {
        "uz": "Faqat dastlabki {shown} ta xato saqlandi (jami {count} ta).",
        "en": "Only the first {shown} issues were kept (of {count}).",
    },
    "jschema.warn.whole_dropped": {
        "uz": "Hujjat butunligicha tekshirilganda ko'pi bilan {kept} ta xato saqlanadi: yana {count} ta xato "
              "topildi, lekin ro'yxatga va xatolar soniga kirmadi. Noto'g'ri yozuvlar soni to'liq.",
        "en": "A whole-document check keeps at most {kept} issues: {count} more issues were found but are not "
              "listed or counted. The number of invalid records is complete.",
    },
    "jschema.warn.duplicates_skipped": {
        "uz": "{count} ta yozuvni takrorlanishga tekshirib bo'lmadi (juda katta yoki o'qilmaydi).",
        "en": "{count} records could not be checked for duplicates (too large or unreadable).",
    },
    "jschema.warn.duplicates_skipped.one": {
        "uz": "{count} ta yozuvni takrorlanishga tekshirib bo'lmadi (juda katta yoki o'qilmaydi).",
        "en": "{count} record could not be checked for duplicates (too large or unreadable).",
    },
    "jschema.progress.validate": {"uz": "Schema bo'yicha tekshirilmoqda: {name}", "en": "Validating: {name}"},
    "jschema.progress.duplicates": {
        "uz": "Takroriy yozuvlar qidirilmoqda: {name}",
        "en": "Looking for duplicate records: {name}",
    },
    "jschema.desc.invalid": {
        "uz": "Schemaga mos kelmagan yozuvlar ({name})",
        "en": "Records that do not match the schema ({name})",
    },
    "jschema.gen.description.full": {
        "uz": "{records} ta yozuvning barchasi tahlil qilinib yaratildi ({date}).",
        "en": "Generated from all {records} records ({date}).",
    },
    "jschema.gen.description.sample": {
        "uz": "{total} ta yozuvdan {records} tasi (namuna) tahlil qilinib yaratildi ({date}).",
        "en": "Generated from a sample of {records} of {total} records ({date}).",
    },
    # ---- writer ----
    "writer.invalid_option": {
        "uz": "Eksport sozlamasi noto'g'ri: {name} = {value}. Kutilgan qiymat: {expected}.",
        "en": "Invalid export option: {name} = {value}. Expected: {expected}.",
    },
    "writer.expected.fmt": {
        "uz": "json, jsonl, csv, xlsx, sqlite yoki parquet",
        "en": "json, jsonl, csv, xlsx, sqlite or parquet",
    },
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
    "writer.expected.workers": {
        "uz": "ishchi jarayonlar soni: 1 yoki undan katta butun son (1 — hammasi bitta oqimda)",
        "en": "the number of worker processes: a whole number of at least 1 (1: everything in one thread)",
    },
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
    # stage 5 (docs/ARCHITECTURE.md §14.4): wrapper notice, gzip outputs, merge (export_many), one node
    "writer.note.wrapper_not_written": {
        "uz": "Yozuvlar atrofidagi qism ({path} konteyneridan tashqaridagi maydonlar) {fmt} faylga yozilmadi: unda "
              "faqat yozuvlar bor. Bu qismni faqat JSON eksporti saqlaydi.",
        "en": "The part around the records (the fields outside the container {path}) was not written to the {fmt} "
              "file: it holds the records only. Only a JSON export keeps that part.",
    },
    "writer.error.gzip_format": {
        "uz": "{fmt} faylini gzip bilan siqib bo'lmaydi: {path}",
        "en": "A {fmt} file cannot be gzip-compressed: {path}",
    },
    "writer.error.gzip_format.hint": {
        "uz": "Fayl nomi oxiridagi .gz ni olib tashlang: gzip faqat JSON, JSON Lines va CSV fayllar uchun.",
        "en": "Remove the .gz at the end of the file name: gzip is available for JSON, JSON Lines and CSV files only.",
    },
    "writer.expected.table_fmt": {"uz": "csv, xlsx yoki parquet", "en": "csv, xlsx or parquet"},
    "writer.expected.as_keyed_many": {
        "uz": "false — bir nechta fayl birlashtirilganda kalitli obyekt tiklanmaydi",
        "en": "false — a keyed object is not rebuilt when several files are merged",
    },
    "writer.error.many_format": {
        "uz": "Bir nechta faylni faqat JSON yoki JSON Lines faylga birlashtirish mumkin ({fmt} emas).",
        "en": "Several files can be merged into a JSON or JSON Lines file only (not {fmt}).",
    },
    "writer.error.many_format.hint": {
        "uz": "Avval JSON Lines faylga birlashtiring, keyin natijani kerakli formatga o'giring.",
        "en": "Merge into a JSON Lines file first, then convert the result to the format you need.",
    },
    "writer.error.no_inputs": {
        "uz": "Birlashtirish uchun birorta ham fayl berilmadi.",
        "en": "No input files were given to merge.",
    },
    "writer.warn.members_as_objects": {
        "uz": "{count} ta kirish faylining yozuvlari «kalit: qiymat» juftlari edi. Natija massiv bo'lgani uchun "
              "ular {{\"kalit\": qiymat}} ko'rinishidagi obyektlar sifatida yozildi.",
        "en": "The records of {count} inputs were \"key: value\" members. Since the result is an array, they were "
              "written as objects of the form {{\"key\": value}}.",
    },
    "writer.warn.members_as_objects.one": {
        "uz": "{count} ta kirish faylining yozuvlari «kalit: qiymat» juftlari edi. Natija massiv bo'lgani uchun "
              "ular {{\"kalit\": qiymat}} ko'rinishidagi obyektlar sifatida yozildi.",
        "en": "The records of {count} input were \"key: value\" members. Since the result is an array, they were "
              "written as objects of the form {{\"key\": value}}.",
    },
    "writer.warn.wrappers_dropped": {
        "uz": "{count} ta kirish faylida yozuvlar atrofidagi qism (o'ram) natijaga kirmadi: {names}. Faqat "
              "birinchi faylning o'rami saqlanadi — uning yozuvlar konteyneri natija bilan bir xil turda bo'lsa.",
        "en": "The part around the records (the wrapper) of {count} inputs was not written: {names}. Only the "
              "first input's wrapper is kept — when its record container is of the same kind as the result.",
    },
    "writer.warn.wrappers_dropped.one": {
        "uz": "{count} ta kirish faylida yozuvlar atrofidagi qism (o'ram) natijaga kirmadi: {names}. Faqat "
              "birinchi faylning o'rami saqlanadi — uning yozuvlar konteyneri natija bilan bir xil turda bo'lsa.",
        "en": "The part around the records (the wrapper) of {count} input was not written: {names}. Only the "
              "first input's wrapper is kept — when its record container is of the same kind as the result.",
    },
    "writer.warn.merge_duplicate_keys": {
        "uz": "{count} ta yozuvning kaliti oldingi yozuvdagi kalit bilan bir xil (birinchisi: natijadagi #{index}, "
              "kalit {key}): birlashtirilgan obyektda takroriy kalitlar bor. Ular o'zgarishsiz yozildi. Har bir "
              "kalitdan bittasini qoldirish uchun $key bo'yicha dublikatlarni olib tashlab birlashtiring.{note}",
        "en": "{count} records repeat the key of an earlier record (the first: #{index} of the result, key {key}): "
              "the merged object has repeated keys. They were written unchanged. To keep one record per key, merge "
              "with the duplicates by $key removed.{note}",
    },
    "writer.warn.merge_duplicate_keys.one": {
        "uz": "{count} ta yozuvning kaliti oldingi yozuvdagi kalit bilan bir xil (natijadagi #{index}, kalit {key}): "
              "birlashtirilgan obyektda takroriy kalit bor. U o'zgarishsiz yozildi. Har bir kalitdan bittasini "
              "qoldirish uchun $key bo'yicha dublikatlarni olib tashlab birlashtiring.{note}",
        "en": "{count} record repeats the key of an earlier record (#{index} of the result, key {key}): the merged "
              "object has a repeated key. It was written unchanged. To keep one record per key, merge with the "
              "duplicates by $key removed.{note}",
    },
    "writer.error.node_format": {
        "uz": "Bitta tugunni faqat JSON yoki JSON Lines faylga yozish mumkin ({fmt} emas).",
        "en": "A single node can be written to a JSON or JSON Lines file only (not {fmt}).",
    },
    "writer.error.node_format.hint": {
        "uz": "Tugunni JSON Lines faylga yozing, keyin natijani kerakli formatga o'giring.",
        "en": "Write the node to a JSON Lines file, then convert the result to the format you need.",
    },
    "writer.error.node_truncated": {
        "uz": "Tugunning baytlarini o'qib bo'lmadi: fayl qisqarib qolgan.",
        "en": "The bytes of the node could not be read: the file got shorter.",
    },
    "writer.error.node_truncated.hint": {
        "uz": "Faylni qayta oching va yana urinib ko'ring.",
        "en": "Open the file again and try again.",
    },
    "writer.progress.node": {"uz": "Tugun yozilmoqda: {name}", "en": "Writing the node: {name}"},
    # Parquet (writer_parquet.py, stage 7)
    "writer.error.parquet_missing": {
        "uz": "Parquet eksporti uchun pyarrow paketi kerak: pip install \"jsonreshape[parquet]\" ({detail})",
        "en": "The Parquet export needs the pyarrow package: pip install \"jsonreshape[parquet]\" ({detail})",
    },
    "writer.error.parquet_missing.hint": {
        "uz": "Paketni o'rnating: pip install \"jsonreshape[parquet]\" — dasturning qolgan qismi usiz ham ishlaydi.",
        "en": "Install the package: pip install \"jsonreshape[parquet]\" — the rest of the program works without it.",
    },
    "writer.error.parquet": {
        "uz": "Parquet faylini yozib bo'lmadi: {detail}",
        "en": "The Parquet file could not be written: {detail}",
    },
    "writer.error.parquet_type": {
        "uz": "{column} ustuni: #{index} yozuvdagi qiymat turi barcha qatorlardan aniqlangan ustun turiga mos "
              "kelmaydi (eksport davomida asl fayl o'zgarganmi?).",
        "en": "Column {column}: the value at record #{index} does not fit the column type decided from all rows "
              "(did the source change during the export?).",
    },
    "writer.expected.parquet_compression": {"uz": "zstd, snappy, gzip yoki none", "en": "zstd, snappy, gzip or none"},
    "writer.warn.parquet_restarted": {
        "uz": "{column} ustuni: #{index} yozuvdagi qiymat turi ustun turlari aniqlangan dastlabki {rows} qatordan "
              "farq qiladi; eksport ikki o'tishda qayta bajarildi (turlar barcha qatorlardan aniqlandi).",
        "en": "Column {column}: record #{index} holds a value of another type than the first {rows} rows, from which "
              "the column types had been fixed; the export was done again in two passes (the types decided from "
              "every row).",
    },
    "writer.warn.parquet_number_text": {
        "uz": "{count} ta ustunda int64 yoki double aniq saqlay olmaydigan sonlar bor (64 bitdan katta butun sonlar, "
              "double sig'dirmaydigan raqamlar, 1e400): ular matn sifatida yozildi: {columns}",
        "en": "{count} columns hold numbers no int64 or double keeps exactly (integers beyond 64 bits, more digits "
              "than a double holds, 1e400): they were written as text: {columns}",
    },
    "writer.warn.parquet_number_text.one": {
        "uz": "{count} ta ustunda int64 yoki double aniq saqlay olmaydigan sonlar bor (64 bitdan katta butun sonlar, "
              "double sig'dirmaydigan raqamlar, 1e400): u matn sifatida yozildi: {columns}",
        "en": "{count} column holds numbers no int64 or double keeps exactly (integers beyond 64 bits, more digits "
              "than a double holds, 1e400): it was written as text: {columns}",
    },
    "writer.progress.parquet_types": {"uz": "Ustun turlari aniqlanmoqda: {name}", "en": "Deciding the column types: {name}"},
    # ---- filetools ----
    # split / merge / extract / --select (filetools.py, docs/ARCHITECTURE.md §14.4)
    "filetools.error.option": {
        "uz": "Fayl amali sozlamasi noto'g'ri: {name} = {value}",
        "en": "Invalid file tool option: {name} = {value}",
    },
    "filetools.error.split_mode": {
        "uz": "Faylni qanday bo'lishni bitta usulda ko'rsating: qismlar soni, qism hajmi yoki har qismdagi "
              "yozuvlar soni.",
        "en": "Say how to split the file in exactly one way: the number of parts, the size of a part or the "
              "records per part.",
    },
    "filetools.error.split_mode.hint": {
        "uz": "Masalan: --parts 10, --size 500MB yoki --records 100000.",
        "en": "For example: --parts 10, --size 500MB or --records 100000.",
    },
    "filetools.error.partial": {
        "uz": "Faylni bo'lib bo'lmaydi: unda JSON xatosi bor ({line}-qator, {column}-ustun) va faqat xatogacha "
              "bo'lgan yozuvlar ochilgan.",
        "en": "The file cannot be split: it has a JSON error (line {line}, column {column}) and only the records "
              "before the error are open.",
    },
    "filetools.error.partial.hint": {
        "uz": "Avval faylni ta'mirlang (jsonreshape repair), keyin ta'mirlangan faylni bo'ling.",
        "en": "Repair the file first (jsonreshape repair), then split the repaired file.",
    },
    "filetools.error.single": {
        "uz": "Fayl bitta JSON qiymatidan iborat: uni yozuvlarga bo'lib bo'lmaydi.",
        "en": "The file is a single JSON value: it cannot be split into records.",
    },
    "filetools.error.single.hint": {
        "uz": "Ichidagi massivni yozuvlar sifatida ochish uchun uning yo'lini ko'rsating (--record-path, "
              "masalan data).",
        "en": "Give the path of the array inside it to open its elements as records (--record-path, for example "
              "data).",
    },
    "filetools.error.no_records": {
        "uz": "Faylda bo'linadigan yozuv yo'q.",
        "en": "The file has no records to split.",
    },
    "filetools.error.too_many_parts": {
        "uz": "{parts} ta qismga bo'lib bo'lmaydi: faylda bor-yo'g'i {count} ta yozuv bor.",
        "en": "The file cannot be split into {parts} parts: it has only {count} records.",
    },
    "filetools.error.too_many_parts.hint": {
        "uz": "Qismlar sonini yozuvlar sonidan oshirmang.",
        "en": "Choose at most as many parts as there are records.",
    },
    "filetools.error.too_many_files": {
        "uz": "Bu bo'lish {parts} ta fayl hosil qilardi; ko'pi bilan {max} ta fayl mumkin.",
        "en": "This split would create {parts} files; at most {max} files are allowed.",
    },
    "filetools.error.too_many_files.hint": {
        "uz": "Kattaroq qism hajmini yoki har qismga ko'proq yozuv tanlang.",
        "en": "Choose a larger part size or more records per part.",
    },
    "filetools.error.broken_lines": {
        "uz": "Faylda {count} ta buzuq qator bor: ular qismlarga yozilmaydi.",
        "en": "The file has {count} broken lines: they would not be written to the parts.",
    },
    "filetools.error.broken_lines.hint": {
        "uz": "Buzuq qatorlarsiz bo'lishga rozi bo'lsangiz, --drop-broken bilan qayta ishga tushiring (yoki avval "
              "faylni ta'mirlang).",
        "en": "Run it again with --drop-broken to split without the broken lines (or repair the file first).",
    },
    "filetools.error.no_dir": {"uz": "Bunday papka yo'q: {path}", "en": "The folder does not exist: {path}"},
    "filetools.error.name_pattern": {
        "uz": "Qism fayllar nomi andozasi noto'g'ri: {pattern}",
        "en": "Invalid name pattern for the part files: {pattern}",
    },
    "filetools.error.name_pattern.hint": {
        "uz": "Andozada {{n}} (qism raqami) bo'lishi shart; {{stem}} (fayl nomi) va {{ext}} (kengaytma) ham "
              "ishlatilishi mumkin, papka ko'rsatilmaydi. Masalan: {{stem}}_qism{{n}}{{ext}}",
        "en": "The pattern must contain {{n}} (the part number); {{stem}} (the file name) and {{ext}} (the "
              "extension) may be used too, no folder. For example: {{stem}}_part{{n}}{{ext}}",
    },
    "filetools.error.disk_space": {
        "uz": "Diskda joy yetmaydi: qismlar uchun taxminan {need} kerak, bo'sh joy {free} ({path}).",
        "en": "Not enough disk space: the parts need about {need}, {free} is free ({path}).",
    },
    "filetools.error.disk_space.hint": {
        "uz": "Diskda joy bo'shating yoki qismlarni boshqa diskdagi papkaga yozing.",
        "en": "Free up disk space or write the parts to a folder on another drive.",
    },
    "filetools.error.part_open_edited": {
        "uz": "Bu faylda dasturda saqlanmagan o'zgarishlar bor (ochiq oynada tahrirlanmoqda yoki tiklash uchun "
              "saqlab qo'yilgan): {path}. Uni hozir almashtirib ham, o'chirib ham bo'lmaydi.",
        "en": "This file has unsaved changes in this program (it is being edited in a tab, or the changes are "
              "kept to be restored): {path}. It cannot be replaced or removed now.",
    },
    "filetools.error.part_open_edited.hint": {
        "uz": "Avval o'sha fayldagi o'zgarishlarni saqlang yoki bekor qiling (uni jsonreshape'da oching) yoki "
              "natijani boshqa papkaga yoki boshqa nomlar bilan yozing.",
        "en": "First save or discard the changes of that file (open it in jsonreshape), or write the result to "
              "another folder or under other names.",
    },
    "filetools.error.merge_format": {
        "uz": "Fayllarni faqat JSON yoki JSON Lines faylga birlashtirish mumkin ({fmt} emas).",
        "en": "Files can be merged into a JSON or JSON Lines file only (not {fmt}).",
    },
    "filetools.error.merge_format.hint": {
        "uz": "Avval .jsonl faylga birlashtiring, keyin natijani kerakli formatga o'giring (jsonreshape convert).",
        "en": "Merge into a .jsonl file first, then convert the result (jsonreshape convert).",
    },
    "filetools.error.partial_input": {
        "uz": "Birlashtiriladigan faylda JSON xatosi bor ({line}-qator, {column}-ustun): {path}",
        "en": "A file to merge has a JSON error (line {line}, column {column}): {path}",
    },
    "filetools.error.partial_input.hint": {
        "uz": "Faylni ta'mirlang (jsonreshape repair) yoki xatogacha bo'lgan yozuvlarni birlashtirishga ruxsat "
              "bering (--allow-partial).",
        "en": "Repair the file (jsonreshape repair) or allow merging the records before the error "
              "(--allow-partial).",
    },
    "filetools.error.dedupe_unavailable": {
        "uz": "Bir nechta fayldagi dublikatlarni qidirib bo'lmaydi: dasturning bu qismi o'rnatilmagan.",
        "en": "Duplicates across several files cannot be searched: this part of the program is not installed.",
    },
    "filetools.error.extract_args": {
        "uz": "Yo'l (--path) yoki tugun (--node) dan faqat bittasini ko'rsating.",
        "en": "Give either a path (--path) or a node (--node), not both.",
    },
    "filetools.error.extract_format": {
        "uz": "Tugun yoki yo'l bo'yicha yig'ilgan qiymatlarni faqat JSON yoki JSON Lines faylga yozish mumkin "
              "({fmt} emas).",
        "en": "A node or the values gathered by a path can be written to a JSON or JSON Lines file only "
              "(not {fmt}).",
    },
    "filetools.error.extract_format.hint": {
        "uz": "Avval .jsonl faylga ajrating, keyin natijani kerakli formatga o'giring (jsonreshape convert).",
        "en": "Extract into a .jsonl file first, then convert the result (jsonreshape convert).",
    },
    "filetools.error.select_syntax": {
        "uz": "Yozuvlar tanlovi noto'g'ri yozilgan: «{item}» ({spec}).",
        "en": "Invalid record selection: \"{item}\" ({spec}).",
    },
    "filetools.error.select_syntax.hint": {
        "uz": "Yozuv raqamlari 0 dan boshlanadi. Misollar: 5 (bitta yozuv), 10:20 (10 dan 19 gacha), 100: "
              "(100 dan oxirigacha), :50 (birinchi 50 ta), 1,5,10:20 (vergul bilan ro'yxat).",
        "en": "Record numbers start at 0. Examples: 5 (one record), 10:20 (10 to 19), 100: (from 100 to the end), "
              ":50 (the first 50), 1,5,10:20 (a comma-separated list).",
    },
    "filetools.error.select_range": {
        "uz": "Tanlovdagi «{item}» faylda yo'q: faylda {count} ta yozuv bor (#0 dan #{last} gacha).",
        "en": "\"{item}\" of the selection is not in the file: it has {count} records (#0 to #{last}).",
    },
    "filetools.error.select_empty": {
        "uz": "Tanlovdagi «{item}» oralig'ida birorta ham yozuv yo'q (boshi oxiridan kichik bo'lishi kerak).",
        "en": "The range \"{item}\" of the selection holds no record (its start must be below its end).",
    },
    "filetools.warn.oversized": {
        "uz": "{count} ta yozuvning o'zi {size} dan katta: har biri alohida qismga yozildi (birinchisi: "
              "#{first} yozuv).",
        "en": "{count} records are larger than {size} by themselves: each was written to a part of its own "
              "(the first: record #{first}).",
    },
    "filetools.warn.oversized.one": {
        "uz": "{count} ta yozuvning o'zi {size} dan katta: u alohida qismga yozildi (#{first} yozuv).",
        "en": "{count} record is larger than {size} by itself: it was written to a part of its own "
              "(record #{first}).",
    },
    "filetools.warn.stale_parts": {
        "uz": "Diqqat: papkada shu nom andozasiga mos, lekin bu natijaga kirmaydigan fayllar bor (masalan, oldingi "
              "bo'lishdan qolgan qismlar); ular o'zgartirilmadi ({count} ta): {names}. Ularni yangi qismlar bilan "
              "birga birlashtirmang yoki import qilmang: yozuvlar ikki marta hisoblanadi. Kerak bo'lmasa, ularni "
              "o'chiring.",
        "en": "Attention: the folder has files matching the name pattern that are not part of this result (for "
              "example parts of an earlier split); they were left unchanged ({count}): {names}. Do not merge or "
              "import them together with the new parts: their records would be counted twice. Delete them if "
              "they are not needed.",
    },
    "filetools.warn.stale_removed": {
        "uz": "Oldingi bo'lishdan qolgan eski qismlar o'chirildi ({count} ta): {names}",
        "en": "The old parts of an earlier split were removed ({count}): {names}",
    },
    "filetools.warn.leftover": {
        "uz": "Almashtirilgan eski qism faylning nusxasini o'chirib bo'lmadi: {path}",
        "en": "The copy of a replaced old part file could not be removed: {path}",
    },
    "filetools.warn.broken_dropped": {
        "uz": "{count} ta buzuq qator qismlarga yozilmadi.",
        "en": "{count} broken lines were not written to the parts.",
    },
    "filetools.warn.broken_dropped.one": {
        "uz": "{count} ta buzuq qator qismlarga yozilmadi.",
        "en": "{count} broken line was not written to the parts.",
    },
    "filetools.warn.brace_list": {
        "uz": "Qismlar JSON massivi sifatida yozildi: {{ {{...}}, {{...}} }} ko'rinishi standart JSON emas.",
        "en": "The parts were written as JSON arrays: the form {{ {{...}}, {{...}} }} is not standard JSON.",
    },
    "filetools.warn.extract_too_large": {
        "uz": "{count} ta katta yozuvdagi qiymatlar xotiraga sig'madi ({limit} chegarasi) va yozilmadi "
              "(birinchisi: #{first} yozuv).",
        "en": "The values of {count} large records did not fit the memory budget ({limit}) and were not written "
              "(the first: record #{first}).",
    },
    "filetools.warn.extract_too_large.one": {
        "uz": "{count} ta katta yozuvdagi qiymatlar xotiraga sig'madi ({limit} chegarasi) va yozilmadi "
              "(#{first} yozuv).",
        "en": "The values of {count} large record did not fit the memory budget ({limit}) and were not written "
              "(record #{first}).",
    },
    "filetools.progress.part": {"uz": "Qism {k}/{n} · {name}", "en": "Part {k}/{n} · {name}"},
    "filetools.progress.dedupe": {"uz": "Dublikatlar qidirilmoqda…", "en": "Looking for duplicates…"},
    "filetools.progress.merge": {"uz": "Birlashtirilmoqda: {name}", "en": "Merging into {name}"},
    "filetools.progress.extract": {"uz": "Qiymatlar yig'ilmoqda: {name}", "en": "Gathering the values: {name}"},
    # ---- edits ----
    # errors of the edit layer (EditError / ConversionError, editops/edits/editstore/saver/bulk/jsonpatch)
    "edits.error.generic": {"uz": "Tahrirlab bo'lmadi: {detail}", "en": "The edit could not be made: {detail}"},
    "edits.error.path_not_found": {
        "uz": "Tahrirlanadigan joy topilmadi: {path}",
        "en": "The place to edit was not found: {path}",
    },
    "edits.error.path_not_found.hint": {
        "uz": "Ma'lumot o'zgargan bo'lishi mumkin. Tugunni daraxt yoki jadvalda qaytadan tanlang.",
        "en": "The data may have changed. Select the node again in the tree or the table.",
    },
    "edits.error.key_exists": {
        "uz": "Bu obyektda \"{key}\" kaliti allaqachon bor.",
        "en": "This object already has the key \"{key}\".",
    },
    "edits.error.key_exists.hint": {
        "uz": "Boshqa nom tanlang: bitta obyektdagi kalitlar takrorlanmasligi kerak.",
        "en": "Choose another name: the keys of one object must be unique.",
    },
    "edits.error.invalid_json": {
        "uz": "JSON matni noto'g'ri ({line}-qator, {column}-ustun): {reason}.",
        "en": "The JSON text is not valid (line {line}, column {column}): {reason}.",
    },
    "edits.error.invalid_json.hint": {
        "uz": "Bitta JSON qiymatini kiriting, masalan: 42, \"matn\", true, null, [1, 2] yoki obyekt. "
              "Matn qo'shtirnoq ichida yoziladi.",
        "en": "Enter one JSON value, for example: 42, \"text\", true, null, [1, 2] or an object. "
              "Text goes inside double quotes.",
    },
    "edits.error.internal": {
        "uz": "Ichki xato: tahrir natijasi tekshiruvdan o'tmadi ({detail}). Hech narsa o'zgartirilmadi.",
        "en": "Internal error: the result of the edit failed validation ({detail}). Nothing was changed.",
    },
    "edits.error.internal.hint": {
        "uz": "Bu dasturdagi xato. Iltimos, log faylini dastur muallifiga yuboring.",
        "en": "This is a bug in the program. Please send the log file to the developer.",
    },
    "edits.error.convert": {
        "uz": "Turni o'zgartirib bo'lmaydi: {source} turidagi {value} qiymati {target} turiga aylanmaydi.",
        "en": "The {source} value {value} cannot be converted to {target}.",
    },
    "edits.error.convert.hint": {
        "uz": "Matn songa faqat JSON soni bo'lsa (masalan 12, -3.5, 1e3), mantiqiy qiymatga esa true/false, "
              "1/0, yes/no yoki ha/yo'q bo'lsa o'zgaradi. Obyekt va massivni songa yoki mantiqiy qiymatga "
              "o'zgartirib bo'lmaydi.",
        "en": "Text becomes a number only when it is a JSON number (e.g. 12, -3.5, 1e3), and a boolean only "
              "when it is true/false, 1/0, yes/no or ha/yo'q. Objects and arrays cannot become numbers or "
              "booleans.",
    },
    "edits.error.huge_record": {
        "uz": "Bu yozuv juda katta ({size}): {limit} dan katta yozuvlarni tahrirlab bo'lmaydi.",
        "en": "This record is too large ({size}): records larger than {limit} cannot be edited.",
    },
    "edits.error.huge_record.hint": {
        "uz": "Chegara — Sozlamalardagi «Katta tugun chegarasi (hajm)». Uni oshirsangiz, ko'proq xotira kerak bo'ladi.",
        "en": "The limit is the \"Large node threshold (size)\" setting. Raising it needs more memory.",
    },
    "edits.error.container_locked": {
        "uz": "Yozuvlar konteyneri ({path}) va unga olib boruvchi yo'lni o'zgartirib, o'chirib yoki "
              "ko'chirib bo'lmaydi.",
        "en": "The record container ({path}) and the path leading to it cannot be changed, deleted or moved.",
    },
    "edits.error.container_locked.hint": {
        "uz": "Yozuvlarning o'zini yoki o'rab turgan obyektning boshqa a'zolarini tahrirlang.",
        "en": "Edit the records themselves, or the other members of the wrapping object.",
    },
    "edits.error.single_root": {
        "uz": "Bu hujjat bitta JSON qiymatidan iborat: yozuv qo'shib, o'chirib, nusxalab yoki ko'chirib "
              "bo'lmaydi.",
        "en": "This document is one JSON value: records cannot be added, deleted, duplicated or moved.",
    },
    "edits.error.single_root.hint": {
        "uz": "Ildiz qiymatning ichidagi elementlar va a'zolarni tahrirlang.",
        "en": "Edit the elements and members inside the root value instead.",
    },
    "edits.error.pending": {
        "uz": "Bu fayl uchun avvalgi saqlanmagan o'zgarishlar topildi.",
        "en": "Unsaved changes from an earlier session were found for this file.",
    },
    "edits.error.pending.hint": {
        "uz": "Avval ularni tiklang, bekor qiling yoki JSON Patch sifatida saqlang.",
        "en": "Restore them, discard them or save them as a JSON Patch first.",
    },
    "edits.error.stale_refs": {
        "uz": "Ma'lumot shu orada o'zgardi, buyruq bajarilmadi.",
        "en": "The data changed in the meantime; the command was not carried out.",
    },
    "edits.error.stale_refs.hint": {
        "uz": "Tugunni qaytadan tanlab, buyruqni yana bajaring.",
        "en": "Select the node again and repeat the command.",
    },
    "edits.error.stale_scope": {
        "uz": "Yozuvlar ro'yxati eskirgan: ma'lumot u tuzilgandan keyin o'zgardi.",
        "en": "The record list is out of date: the data changed after it was made.",
    },
    "edits.error.stale_scope.hint": {
        "uz": "Filtr, saralash yoki qidiruvni qaytadan bajaring.",
        "en": "Run the filter, sort or search again.",
    },
    "edits.error.too_many": {
        "uz": "Bitta buyruq uchun juda ko'p element tanlangan ({count} ta; ko'pi bilan {limit} ta).",
        "en": "Too many items for one command ({count}; at most {limit}).",
    },
    "edits.error.too_many.hint": {
        "uz": "Ko'p yozuvlarni bir yo'la o'zgartirish uchun «Ommaviy amallar» (Ctrl+H) dan foydalaning.",
        "en": "Use Bulk operations (Ctrl+H) to change many records at once.",
    },
    "edits.error.not_object": {
        "uz": "{path} obyekt emas, shuning uchun unda kalitli a'zo bo'la olmaydi.",
        "en": "{path} is not an object, so it cannot have a member with a key.",
    },
    "edits.error.move_into_self": {
        "uz": "Tugunni o'zining ichiga yoki o'z bolalaridan biriga ko'chirib bo'lmaydi.",
        "en": "A node cannot be moved into itself or into one of its own children.",
    },
    "edits.error.closed": {
        "uz": "Tahrirlash sessiyasi yopilgan: fayl saqlangan yoki yopilgan.",
        "en": "The editing session is closed: the file was saved or closed.",
    },
    "edits.error.closed.hint": {
        "uz": "Tahrirlashni davom ettirish uchun faylni qayta oching.",
        "en": "Reopen the file to continue editing.",
    },
    "edits.error.dirty": {"uz": "Faylda saqlanmagan o'zgarishlar bor.", "en": "The file has unsaved changes."},
    "edits.error.dirty.hint": {
        "uz": "Avval o'zgarishlarni saqlang yoki bekor qiling.",
        "en": "Save or discard the changes first.",
    },
    "edits.error.layer_corrupt": {
        "uz": "Saqlanmagan o'zgarishlar fayli shikastlangan: uni o'qib bo'lmadi.",
        "en": "The file of unsaved changes is damaged and cannot be read.",
    },
    "edits.error.layer_corrupt.hint": {
        "uz": "O'zgarishlarni JSON Patch sifatida eksport qilib ko'ring yoki ularni bekor qiling.",
        "en": "Try to export the changes as a JSON Patch, or discard them.",
    },
    "edits.error.locked": {
        "uz": "Bu fayl boshqa oynada yoki dasturning boshqa nusxasida tahrirlanmoqda.",
        "en": "This file is being edited in another tab or in another instance of the program.",
    },
    "edits.error.locked.hint": {
        "uz": "Faylni o'sha yerda tahrirlang yoki avval u yerda yoping.",
        "en": "Edit it there, or close it there first.",
    },
    "edits.error.target_changed": {
        "uz": "Tahrirlanayotgan tugun o'zgargan yoki o'chirilgan.",
        "en": "The node being edited was changed or deleted.",
    },
    "edits.error.target_changed.hint": {"uz": "Tugunni qaytadan tanlang.", "en": "Select the node again."},
    "edits.error.not_member": {
        "uz": "Faqat obyekt a'zolarining kaliti bor: massiv elementini yoki ildiz qiymatni qayta nomlab bo'lmaydi.",
        "en": "Only object members have keys: an array element or the root value cannot be renamed.",
    },
    "edits.error.not_container": {
        "uz": "Bu qiymat massiv yoki obyekt emas: uning ichiga element qo'shib yoki ichidan o'chirib bo'lmaydi.",
        "en": "This value is not an array or an object: nothing can be added to it or removed from it.",
    },
    "edits.error.bad_index": {
        "uz": "{index}-o'rin mavjud emas (elementlar soni: {count}).",
        "en": "Position {index} does not exist (number of items: {count}).",
    },
    "edits.error.key_required": {
        "uz": "Obyektga qo'shiladigan a'zo uchun kalit kerak.",
        "en": "A new member of an object needs a key.",
    },
    "edits.error.partial": {
        "uz": "Faylda JSON xatosi bor: faqat xatogacha bo'lgan yozuvlar ko'rsatilgan, shuning uchun uni tahrirlab "
              "bo'lmaydi.",
        "en": "The file has a JSON error: only the records before it are shown, so it cannot be edited.",
    },
    "edits.error.partial.hint": {
        "uz": "Avval faylni tuzating yoki to'g'ri qismini boshqa faylga eksport qiling.",
        "en": "Repair the file first, or export the valid part to another file.",
    },
    "edits.error.not_siblings": {
        "uz": "Yuqoriga yoki pastga faqat bitta ota tugunning elementlarini birga surish mumkin.",
        "en": "Only items of the same parent can be moved up or down together.",
    },
    "edits.warn.layer_left": {
        "uz": "Fayl saqlandi, lekin saqlanmagan o'zgarishlar faylini o'chirib bo'lmadi ({detail}). Fayl keyingi "
              "safar ochilganda u ko'rsatiladi: o'sha yerda uni o'chirib tashlashingiz mumkin.",
        "en": "The file was saved, but the file of unsaved changes could not be deleted ({detail}). It is shown "
              "the next time the file is opened: you can delete it there.",
    },
    "edits.progress.keys": {"uz": "Yozuv kalitlari indekslanmoqda…", "en": "Indexing the record keys…"},
    "edits.progress.delete_records": {"uz": "Yozuvlar o'chirilmoqda…", "en": "Deleting records…"},
    "edits.progress.duplicate_records": {"uz": "Yozuvlar nusxalanmoqda…", "en": "Duplicating records…"},
    "edits.detail.shape": {"uz": "kutilmagan tuzilma", "en": "unexpected structure"},
    # check_json reasons that speak of "the text" (the json.* texts speak of a file)
    "edits.reason.empty": {"uz": "matn bo'sh, JSON qiymati kutilgan", "en": "the text is empty, a JSON value was expected"},
    "edits.reason.unexpected_eof": {"uz": "matn kutilmaganda tugadi", "en": "the text ended unexpectedly"},
    "edits.reason.expected_end": {"uz": "matn shu yerda tugashi kerak", "en": "the text should end here"},
    # undo/redo step labels
    "edits.step.set_value": {"uz": "Qiymat o'zgartirildi", "en": "Value changed"},
    "edits.step.rename_key": {"uz": "Kalit qayta nomlandi", "en": "Key renamed"},
    "edits.step.insert": {"uz": "Element qo'shildi", "en": "Item inserted"},
    "edits.step.insert_record": {"uz": "Yozuv qo'shildi", "en": "Record inserted"},
    "edits.step.delete": {"uz": "{count} ta element o'chirildi", "en": "Items deleted: {count}"},
    "edits.step.delete_records": {"uz": "{count} ta yozuv o'chirildi", "en": "Records deleted: {count}"},
    "edits.step.duplicate": {"uz": "{count} ta elementning nusxasi yaratildi", "en": "Items duplicated: {count}"},
    "edits.step.duplicate_records": {"uz": "{count} ta yozuvning nusxasi yaratildi", "en": "Records duplicated: {count}"},
    "edits.step.move": {"uz": "{count} ta element ko'chirildi", "en": "Items moved: {count}"},
    "edits.step.move_records": {"uz": "{count} ta yozuv ko'chirildi", "en": "Records moved: {count}"},
    "edits.step.change_type": {"uz": "{count} ta qiymatning turi o'zgartirildi", "en": "Types changed: {count}"},
    "edits.step.paste": {"uz": "Nusxa qo'yildi", "en": "Pasted"},
    "edits.step.patch": {"uz": "JSON Patch qo'llandi ({count} ta amal)", "en": "JSON Patch applied (operations: {count})"},
    "edits.step.restore": {"uz": "O'zgarishlar tiklandi", "en": "Changes restored"},
    "edits.step.bulk.replace": {"uz": "Topish va almashtirish ({count} ta qiymat)", "en": "Find and replace (values: {count})"},
    "edits.step.bulk.rename_key": {"uz": "Kalitni qayta nomlash ({count} ta a'zo)", "en": "Rename key (members: {count})"},
    "edits.step.bulk.delete_field": {"uz": "Maydonni o'chirish ({count} ta a'zo)", "en": "Delete field (members: {count})"},
    "edits.step.bulk.set_field": {"uz": "Maydonga qiymat berish ({count} ta yozuv)", "en": "Set field (records: {count})"},
    "edits.step.bulk.clean_strings": {"uz": "Matnlarni tozalash ({count} ta qiymat)", "en": "Clean strings (values: {count})"},
    "edits.step.bulk.convert_type": {"uz": "Turni o'zgartirish ({count} ta qiymat)", "en": "Convert type (values: {count})"},
    "edits.step.bulk.dedupe": {"uz": "Takrorlarni o'chirish ({count} ta yozuv)", "en": "Remove duplicates (records: {count})"},
    # JSON type names used in messages
    "edits.type.object": {"uz": "obyekt", "en": "object"},
    "edits.type.array": {"uz": "massiv", "en": "array"},
    "edits.type.string": {"uz": "satr", "en": "string"},
    "edits.type.number": {"uz": "son", "en": "number"},
    "edits.type.bool": {"uz": "mantiqiy qiymat", "en": "boolean"},
    "edits.type.null": {"uz": "null", "en": "null"},
    # ---- editstore ----
    # the edit layer on disk (editstore.py): errors, recovery reasons, notices
    "editstore.error.damaged": {
        "uz": "Saqlanmagan o'zgarishlar fayli shikastlangan: uni {seq}-qadamdan boshlab o'qib bo'lmadi.",
        "en": "The file of unsaved changes is damaged: it cannot be read from step {seq} on.",
    },
    "editstore.error.damaged.hint": {
        "uz": "Undan oldingi o'zgarishlarni JSON Patch sifatida saqlab olishingiz yoki o'zgarishlarni "
              "o'chirishingiz mumkin.",
        "en": "You can save the changes before it as a JSON Patch, or delete the changes.",
    },
    "editstore.error.stale": {
        "uz": "{path} dagi saqlanmagan o'zgarishlar bu faylning eski versiyasiga tegishli: fayl ular "
              "kiritilgandan keyin o'zgargan.",
        "en": "The unsaved changes in {path} belong to an older version of this file: the file changed after "
              "they were made.",
    },
    "editstore.error.stale.hint": {
        "uz": "Ularni JSON Patch sifatida saqlang yoki o'chiring, so'ng faylni qaytadan tahrirlang.",
        "en": "Save them as a JSON Patch or delete them, then edit the file again.",
    },
    "editstore.error.disk_full": {
        "uz": "Diskda joy qolmadi: o'zgarishni saqlab bo'lmadi ({path}).",
        "en": "The disk is full: the change could not be stored ({path}).",
    },
    "editstore.error.disk_full.hint": {
        "uz": "Diskda joy bo'shatib, amalni qaytadan bajaring. Avvalgi o'zgarishlar saqlanib qolgan.",
        "en": "Free some disk space and repeat the action. The earlier changes are kept.",
    },
    "editstore.error.write": {
        "uz": "Saqlanmagan o'zgarishlarni yozib bo'lmadi ({path}): {detail}",
        "en": "The unsaved changes could not be written ({path}): {detail}",
    },
    "editstore.error.format": {
        "uz": "Saqlanmagan o'zgarishlar dasturning yangiroq versiyasida yozilgan (format {version}): bu versiya "
              "ularni o'qiy olmaydi.",
        "en": "The unsaved changes were written by a newer version of the program (format {version}): this "
              "version cannot read them.",
    },
    "editstore.error.format.hint": {
        "uz": "Ularni yaratgan versiyada oching yoki dasturni yangilang.",
        "en": "Open them with the version that made them, or update the program.",
    },
    "editstore.error.unavailable": {
        "uz": "Tahrirlab bo'lmaydi: saqlanmagan o'zgarishlar papkasidan foydalanib bo'lmadi ({detail}).",
        "en": "Editing is not possible: the folder for unsaved changes cannot be used ({detail}).",
    },
    "editstore.error.unavailable.hint": {
        "uz": "Dastur papkasiga yozish mumkinligini tekshiring (JSONRESHAPE_HOME yoki %LOCALAPPDATA%\\jsonreshape).",
        "en": "Check that the program folder can be written (JSONRESHAPE_HOME or %LOCALAPPDATA%\\jsonreshape).",
    },
    "editstore.error.read_only": {
        "uz": "Bu saqlanmagan o'zgarishlar faqat o'qish uchun ochilgan: ularni o'zgartirib bo'lmaydi.",
        "en": "These unsaved changes are open for reading only: they cannot be changed.",
    },
    "editstore.error.not_indexed": {
        "uz": "Fayl hali indekslanmoqda: tahrirlash indekslash tugagach mumkin bo'ladi.",
        "en": "The file is still being indexed: editing becomes possible when indexing finishes.",
    },
    "editstore.error.closed": {
        "uz": "Saqlanmagan o'zgarishlar fayli yopilgan.",
        "en": "The file of unsaved changes is closed.",
    },
    "editstore.error.discarded": {
        "uz": "Bu o'zgarishlar bekor qilingan: ularga yangi qadam qo'shib bo'lmaydi.",
        "en": "These changes were discarded: no new step can be added to them.",
    },
    "editstore.error.index_busy": {
        "uz": "Saqlanmagan o'zgarishlar ro'yxati band ({path}).",
        "en": "The list of unsaved changes is busy ({path}).",
    },
    "editstore.warn.beside_fallback": {
        "uz": "O'zgarishlarni fayl yonida saqlab bo'lmadi ({path}): {detail}. Ular dastur papkasida saqlanadi: "
              "{fallback}.",
        "en": "The changes cannot be kept next to the file ({path}): {detail}. They are kept in the program "
              "folder instead: {fallback}.",
    },
    # reasons shown for a damaged pending layer (PendingInfo.damaged)
    "editstore.damaged.db": {
        "uz": "ma'lumotlar bazasini o'qib bo'lmadi ({detail})",
        "en": "the database cannot be read ({detail})",
    },
    "editstore.damaged.meta": {
        "uz": "o'zgarishlar tavsifida \"{key}\" yo'q",
        "en": "the description of the changes lacks \"{key}\"",
    },
    "editstore.damaged.meta_value": {
        "uz": "o'zgarishlar tavsifi noto'g'ri ({detail})",
        "en": "the description of the changes is invalid ({detail})",
    },
    "editstore.damaged.missing": {
        "uz": "{name} fayli yo'q yoki qisqarib qolgan",
        "en": "the file {name} is missing or too short",
    },
    "editstore.damaged.steps": {"uz": "ba'zi qadamlar yo'qolgan", "en": "some steps are missing"},
    # label of a step whose label key is unknown (a layer written by another version)
    "editstore.step.unknown": {"uz": "O'zgarish", "en": "Change"},
    # ---- saver ----
    # Save / Save As in the original kind and encoding (saver.py, docs/ARCHITECTURE.md §12.7)
    "saver.error.broken_lines": {
        "uz": "Faylda {count} ta buzuq qator bor. Saqlanganda ular fayldan o'chiriladi.",
        "en": "The file has {count} broken lines. Saving removes them from the file.",
    },
    "saver.error.broken_lines.hint": {
        "uz": "Buzuq qatorlarsiz saqlashga rozi bo'lsangiz, saqlashni tasdiqlang; aks holda avval ularni tuzating.",
        "en": "Confirm saving without the broken lines, or fix them first.",
    },
    "saver.error.invalid_output": {
        "uz": "Saqlanadigan fayl yaroqli JSON bo'lmay chiqdi ({detail}). Hech narsa saqlanmadi.",
        "en": "The file to be saved is not valid JSON ({detail}). Nothing was saved.",
    },
    "saver.error.invalid_output.hint": {
        "uz": "O'zgarishlaringiz saqlanib qoldi. Bu dasturdagi xato: iltimos, log faylini dasturchilarga yuboring.",
        "en": "Your changes are kept. This is a bug in the program: please send the log file to the developers.",
    },
    "saver.error.grown": {
        "uz": "Saqlash vaqtida faylga yana {bytes} ma'lumot yozildi (fayl hali yozilmoqda). Faylni almashtirish "
              "qo'shilgan ma'lumotni yo'qotardi, shuning uchun hech narsa saqlanmadi: {path}",
        "en": "{bytes} of data were added to the file while it was being saved (it is still being written). "
              "Replacing the file would lose the added data, so nothing was saved: {path}",
    },
    "saver.error.grown.hint": {
        "uz": "O'zgarishlarni «Boshqacha saqlash» bilan boshqa faylga saqlang yoki faylga yozayotgan dastur ishini "
              "tugatishini kuting.",
        "en": "Save the changes to another file with Save As, or wait until the program writing to the file has "
              "finished.",
    },
    "saver.error.partial": {
        "uz": "Faylni saqlab bo'lmaydi: unda JSON xatosi bor ({line}-qator, {column}-ustun) va faqat xatogacha "
              "bo'lgan yozuvlar ochilgan.",
        "en": "The file cannot be saved: it has a JSON error (line {line}, column {column}) and only the records "
              "before the error are open.",
    },
    "saver.error.partial.hint": {
        "uz": "Yozuvlarni eksport qilishingiz yoki avval faylni tuzatishingiz mumkin.",
        "en": "You can export the records, or repair the file first.",
    },
    "saver.error.no_dir": {"uz": "Bunday papka yo'q: {path}", "en": "The folder does not exist: {path}"},
    "saver.error.target_cache": {
        "uz": "{path} — dasturning bu fayl uchun ishchi nusxasi. Boshqa nom tanlang.",
        "en": "{path} is the program's working copy of this file. Choose another name.",
    },
    "saver.error.original_moved": {
        "uz": "Saqlash muvaffaqiyatsiz tugadi va asl faylni joyiga qaytarib bo'lmadi. U hozir shu yerda: {path}",
        "en": "Saving failed and the original file could not be put back. It is now here: {path}",
    },
    "saver.error.original_moved.hint": {
        "uz": "Faylni qo'lda {source} nomiga qaytaring.",
        "en": "Rename it back to {source} by hand.",
    },
    # details of saver.error.invalid_output
    "saver.detail.json": {"uz": "{reason}, {offset}-bayt", "en": "{reason}, at byte {offset}"},
    "saver.detail.count": {"uz": "{expected} ta yozuv o'rniga {found} ta", "en": "{found} records instead of {expected}"},
    "saver.detail.broken": {"uz": "{line}-qator buzuq: {reason}", "en": "line {line} is broken: {reason}"},
    "saver.detail.encoding": {
        "uz": "matnni {encoding} kodirovkasiga o'girib bo'lmadi",
        "en": "the text cannot be converted to {encoding}",
    },
    # SaveReport.warnings
    "saver.warn.brace_list": {
        "uz": "Fayl JSON massivi sifatida saqlandi: {example} ko'rinishi standart JSON emas.",
        "en": "The file was saved as a JSON array: the form {example} is not standard JSON.",
    },
    "saver.warn.gzip_trailing": {
        "uz": "Asl gzip arxividan keyingi ortiqcha ma'lumot saqlanmadi.",
        "en": "The extra data after the original gzip archive was not saved.",
    },
    "saver.warn.gzip_truncated": {
        "uz": "Asl gzip arxivi kesilgan edi: faylning faqat ochilgan qismi saqlandi.",
        "en": "The original gzip archive was truncated: only the part that could be decompressed was saved.",
    },
    "saver.warn.broken_dropped": {
        "uz": "{count} ta buzuq qator fayldan o'chirildi.",
        "en": "{count} broken lines were removed from the file.",
    },
    "saver.warn.broken_dropped.one": {
        "uz": "{count} ta buzuq qator fayldan o'chirildi.",
        "en": "{count} broken line was removed from the file.",
    },
    "saver.warn.compressed": {
        "uz": "Fayl gzip bilan siqib saqlandi: uning nomi .gz bilan tugaydi.",
        "en": "The file was saved compressed with gzip: its name ends with .gz.",
    },
    "saver.warn.uncompressed": {
        "uz": "Fayl siqilmasdan saqlandi: uning nomi .gz bilan tugamaydi.",
        "en": "The file was saved without compression: its name does not end with .gz.",
    },
    "saver.warn.leftover": {
        "uz": "Eski nusxani o'chirib bo'lmadi: {path}",
        "en": "The old copy could not be removed: {path}",
    },
    "saver.warn.compression_lost": {
        "uz": "Asl fayl NTFS bilan siqilgan edi, yangi faylni siqib bo'lmadi: u siqilmasdan saqlandi.",
        "en": "The original file was NTFS-compressed; the new file could not be compressed and was saved "
              "uncompressed.",
    },
    "saver.warn.encryption_lost": {
        "uz": "Asl fayl EFS bilan shifrlangan edi, yangi faylni shifrlab bo'lmadi: u shifrlanmasdan saqlandi.",
        "en": "The original file was encrypted with EFS; the new file could not be encrypted and was saved "
              "unencrypted.",
    },
    # sweep_leftovers(): leftovers of interrupted saves removed (status bar, SaveReport.warnings)
    "saver.sweep.removed": {
        "uz": "To'xtab qolgan saqlashlardan qolgan {count} ta ortiqcha fayl o'chirildi ({size} bo'shatildi).",
        "en": "Removed {count} leftover files of interrupted saves ({size} freed).",
    },
    "saver.sweep.removed.one": {
        "uz": "To'xtab qolgan saqlashdan qolgan {count} ta ortiqcha fayl o'chirildi ({size} bo'shatildi).",
        "en": "Removed {count} leftover file of an interrupted save ({size} freed).",
    },
    # save_questions(): asked before a save (yes = save, no = do not save)
    "saver.ask.brace_list": {
        "uz": "Bu fayl figurali qavslar ichidagi ro'yxat ({example}). U oddiy JSON massivi (kvadrat qavslar "
              "ichida) sifatida saqlanadi. Davom etilsinmi?",
        "en": "This file is a list inside curly braces ({example}). It will be saved as a regular JSON array "
              "(in square brackets). Continue?",
    },
    "saver.ask.broken_lines": {
        "uz": "Faylda {count} ta buzilgan qator bor. Ular saqlangan faylga yozilmaydi. Davom etilsinmi?",
        "en": "The file has {count} broken lines. They are not written to the saved file. Continue?",
    },
    "saver.ask.broken_lines.one": {
        "uz": "Faylda {count} ta buzilgan qator bor. U saqlangan faylga yozilmaydi. Davom etilsinmi?",
        "en": "The file has {count} broken line. It is not written to the saved file. Continue?",
    },
    "saver.ask.gzip_trailing": {
        "uz": "{name} faylida gzip arxividan keyin yana ma'lumot bor. U yangi faylga yozilmaydi va zaxira nusxa "
              "(.bak) saqlanmayotgani uchun butunlay yo'qoladi. Davom etilsinmi?",
        "en": "{name} has more data after its gzip archive. It is not written to the new file, and since no "
              "backup (.bak) is kept, it will be lost. Continue?",
    },
    "saver.progress.save": {"uz": "{name} saqlanmoqda…", "en": "Saving {name}…"},
    # roll_journal(): an interrupted save found at the next open (recovery bar)
    "saver.journal.forward": {
        "uz": "{path} faylini saqlash oxiriga yetmay to'xtagan edi. Saqlash yakunlandi: faylda saqlangan "
              "o'zgarishlar bor.",
        "en": "Saving {path} had stopped before it finished. The save was completed: the file holds the saved "
              "changes.",
    },
    "saver.journal.completed": {
        "uz": "{path} faylining oxirgi saqlanishi tugagan edi; undan qolgan ortiqcha fayllar o'chirildi.",
        "en": "The last save of {path} had finished; the files it left behind were removed.",
    },
    "saver.journal.back": {
        "uz": "{path} faylini saqlash oxiriga yetmay to'xtagan edi. Fayl saqlashdan oldingi holatiga qaytarildi, "
              "saqlanmagan o'zgarishlar esa saqlanib qoldi.",
        "en": "Saving {path} had stopped before it finished. The file was put back as it was before the save; the "
              "unsaved changes are kept.",
    },
    "saver.journal.conflict": {
        "uz": "{path} faylining to'xtab qolgan saqlanishini xavfsiz yakunlab ham, bekor qilib ham bo'lmadi: "
              "fayllardan saqlash qayergacha yetgani aniq bilinmaydi (fayl o'shandan beri o'zgargan bo'lishi "
              "mumkin). Hech narsa o'zgartirilmadi. Tegishli fayllar: {files}",
        "en": "The interrupted save of {path} could neither be completed nor undone safely: the files do not show "
              "how far it got (the file may have changed since). Nothing was changed. Files involved: {files}",
    },
    "saver.journal.invalid": {
        "uz": "To'xtab qolgan saqlash haqidagi yozuv buzilgan, undan foydalanib bo'lmadi.",
        "en": "The record of an interrupted save is damaged and cannot be used.",
    },
    "saver.journal.left": {
        "uz": "Bu fayllar joyida qoldirildi: {files}",
        "en": "These files were left in place: {files}",
    },
    "saver.journal.none": {
        "uz": "Yakunlanadigan narsa qolmagan: to'xtab qolgan saqlash allaqachon hal qilingan.",
        "en": "There is nothing left to finish: the interrupted save was already dealt with.",
    },
    "saver.error.layer_gone": {
        "uz": "Saqlanmagan o'zgarishlar qatlami endi mavjud emas: {path}",
        "en": "The layer of unsaved changes no longer exists: {path}",
    },
    # ---- patch ----
    # JSON Patch (RFC 6902) import / export (jsonpatch.py); operation numbers are 0-based
    "patch.error.generic": {"uz": "JSON Patch'ni qo'llab bo'lmadi.", "en": "The JSON Patch could not be applied."},
    "patch.error.json": {
        "uz": "JSON Patch fayli to'g'ri JSON emas ({line}-qator, {column}-ustun): {reason}.",
        "en": "The JSON Patch file is not valid JSON (line {line}, column {column}): {reason}.",
    },
    "patch.error.json.hint": {
        "uz": "JSON Patch (RFC 6902) amallar massividir: har bir amal \"op\" va \"path\" (kerak bo'lsa \"from\" "
              "yoki \"value\") a'zolari bor obyekt.",
        "en": "A JSON Patch (RFC 6902) is an array of operations: each one is an object with \"op\" and \"path\" "
              "members (and \"from\" or \"value\" where needed).",
    },
    "patch.error.empty": {"uz": "JSON Patch fayli bo'sh.", "en": "The JSON Patch file is empty."},
    "patch.error.not_array": {
        "uz": "JSON Patch amallar massivi bo'lishi kerak (fayl [ belgisi bilan boshlanadi).",
        "en": "A JSON Patch must be an array of operations (the file starts with [).",
    },
    "patch.error.not_array.hint": {
        "uz": "Har bir amal \"op\" va \"path\" (kerak bo'lsa \"from\" yoki \"value\") a'zolari bor obyekt bo'ladi.",
        "en": "Each operation is an object with \"op\" and \"path\" members (and \"from\" or \"value\" where needed).",
    },
    "patch.error.encoding": {
        "uz": "JSON Patch faylini o'qib bo'lmadi: kodlash yoki siqish buzilgan ({detail}).",
        "en": "The JSON Patch file cannot be read: its encoding or compression is damaged ({detail}).",
    },
    "patch.error.encoding.hint": {"uz": "Faylni UTF-8 kodlashda saqlang.", "en": "Save the file as UTF-8."},
    "patch.error.op_not_object": {
        "uz": "JSON Patch: #{index} amal obyekt emas (har bir amal \"op\" va \"path\" a'zolari bor obyekt bo'ladi).",
        "en": "JSON Patch: operation #{index} is not an object (every operation is an object with \"op\" and "
              "\"path\" members).",
    },
    "patch.error.member_missing": {
        "uz": "JSON Patch: #{index} amalda ({op}) \"{member}\" a'zosi yo'q.",
        "en": "JSON Patch: operation #{index} ({op}) has no \"{member}\" member.",
    },
    "patch.error.member_type": {
        "uz": "JSON Patch: #{index} amaldagi ({op}) \"{member}\" a'zosi matn bo'lishi kerak.",
        "en": "JSON Patch: the \"{member}\" member of operation #{index} ({op}) must be a string.",
    },
    "patch.error.op_unknown": {
        "uz": "JSON Patch: #{index} amalning turi noma'lum: \"{op}\" (mumkin: add, remove, replace, move, copy, test).",
        "en": "JSON Patch: operation #{index} has an unknown type \"{op}\" (allowed: add, remove, replace, move, "
              "copy, test).",
    },
    "patch.error.pointer": {
        "uz": "JSON Patch: #{index} amaldagi ({op}) \"{member}\" to'g'ri JSON Pointer emas: \"{pointer}\" ({detail}).",
        "en": "JSON Patch: the \"{member}\" of operation #{index} ({op}) is not a valid JSON Pointer: \"{pointer}\" "
              "({detail}).",
    },
    "patch.error.pointer.hint": {
        "uz": "JSON Pointer \"/\" bilan boshlanadi (butun hujjat uchun bo'sh matn); kalitdagi \"~\" belgisi \"~0\", "
              "\"/\" belgisi esa \"~1\" deb yoziladi.",
        "en": "A JSON Pointer starts with \"/\" (an empty string is the whole document); write \"~\" in a key as "
              "\"~0\" and \"/\" as \"~1\".",
    },
    "patch.error.op_duplicate_key": {
        "uz": "JSON Patch: #{index} amalda \"{key}\" kaliti ikki marta yozilgan; bunday amalning ma'nosi aniq emas.",
        "en": "JSON Patch: operation #{index} has the key \"{key}\" twice; such an operation has no clear meaning.",
    },
    "patch.error.op_unreadable": {
        "uz": "JSON Patch: #{index} amalni o'qib bo'lmadi: {detail}.",
        "en": "JSON Patch: operation #{index} cannot be read: {detail}.",
    },
    "patch.error.move_into_self": {
        "uz": "JSON Patch, #{index} amal ({op} \"{path}\"): qiymatni o'zining ichiga ko'chirib bo'lmaydi "
              "(\"from\": \"{pointer}\").",
        "en": "JSON Patch, operation #{index} ({op} \"{path}\"): a value cannot be moved into itself "
              "(\"from\": \"{pointer}\").",
    },
    "patch.error.path_not_found": {
        "uz": "JSON Patch, #{index} amal ({op} \"{path}\"): \"{pointer}\" mavjud emas.",
        "en": "JSON Patch, operation #{index} ({op} \"{path}\"): \"{pointer}\" does not exist.",
    },
    "patch.error.path_not_found.hint": {
        "uz": "Patch boshqa fayl yoki faylning boshqa versiyasi uchun yozilgan bo'lishi mumkin. Hech narsa "
              "o'zgartirilmadi.",
        "en": "The patch may have been written for another file or another version of it. Nothing was changed.",
    },
    "patch.error.dash": {
        "uz": "JSON Patch, #{index} amal ({op} \"{path}\"): \"{pointer}\" dagi \"-\" (massiv oxiri) faqat add "
              "amalining yo'lida yoki move/copy amalining manzilida, oxirgi qism sifatida ishlatiladi.",
        "en": "JSON Patch, operation #{index} ({op} \"{path}\"): \"-\" (the end of an array) in \"{pointer}\" is "
              "allowed only as the last part of an add path or of a move/copy target.",
    },
    "patch.error.bad_index": {
        "uz": "JSON Patch, #{index} amal ({op} \"{path}\"): \"{pointer}\" dagi \"{segment}\" massiv indeksi emas "
              "(0 yoki boshida nol bo'lmagan butun son bo'lishi kerak).",
        "en": "JSON Patch, operation #{index} ({op} \"{path}\"): \"{segment}\" in \"{pointer}\" is not an array index "
              "(0 or a whole number without a leading zero).",
    },
    "patch.error.index_range": {
        "uz": "JSON Patch, #{index} amal ({op} \"{path}\"): \"{pointer}\": {segment}-o'rin yo'q (elementlar soni: "
              "{count}).",
        "en": "JSON Patch, operation #{index} ({op} \"{path}\"): \"{pointer}\": position {segment} does not exist "
              "(number of items: {count}).",
    },
    "patch.error.not_container": {
        "uz": "JSON Patch, #{index} amal ({op} \"{path}\"): \"{pointer}\" massiv yoki obyekt emas, uning ichiga "
              "qiymat qo'shib bo'lmaydi.",
        "en": "JSON Patch, operation #{index} ({op} \"{path}\"): \"{pointer}\" is not an array or an object, so "
              "nothing can be added to it.",
    },
    "patch.error.test_failed": {
        "uz": "JSON Patch, #{index} amal (test \"{path}\"): qiymat kutilganiga teng emas. Patch qo'llanmadi, hech "
              "narsa o'zgartirilmadi.",
        "en": "JSON Patch, operation #{index} (test \"{path}\"): the value differs from the expected one. The patch "
              "was not applied; nothing was changed.",
    },
    "patch.error.test_failed.hint": {
        "uz": "test amali patch qaysi ma'lumot uchun yozilganini tekshiradi: fayl patch kutgan holatda emas.",
        "en": "A test operation checks the data the patch was written for: the file is not in the state the "
              "patch expects.",
    },
    "patch.error.duplicate_keys": {
        "uz": "JSON Patch, #{index} amal ({op} \"{path}\"): \"{pointer}\" da \"{key}\" kaliti takrorlangan, "
              "shuning uchun uni JSON Patch bilan xavfsiz o'zgartirib bo'lmaydi.",
        "en": "JSON Patch, operation #{index} ({op} \"{path}\"): the key \"{key}\" occurs more than once at "
              "\"{pointer}\", so it cannot be changed safely with a JSON Patch.",
    },
    "patch.error.duplicate_keys.hint": {
        "uz": "Takrorlangan kalitlarni avval qo'lda tuzating (masalan, daraxtda qayta nomlang).",
        "en": "Fix the repeated keys by hand first (for example, rename them in the tree).",
    },
    "patch.error.unreadable": {
        "uz": "JSON Patch, #{index} amal ({op} \"{path}\"): \"{pointer}\" dagi yozuvni o'qib bo'lmadi: {detail}.",
        "en": "JSON Patch, operation #{index} ({op} \"{path}\"): the record at \"{pointer}\" cannot be read: {detail}.",
    },
    "patch.error.container_locked": {
        "uz": "JSON Patch, #{index} amal ({op} \"{path}\"): yozuvlar konteyneri (\"{container}\") va unga olib "
              "boruvchi yo'lni o'chirib, ko'chirib yoki surib bo'lmaydi.",
        "en": "JSON Patch, operation #{index} ({op} \"{path}\"): the record container (\"{container}\") and the "
              "path leading to it cannot be removed, moved or shifted.",
    },
    "patch.error.container_locked.hint": {
        "uz": "Yozuvlarning o'zini yoki o'rab turgan obyektning boshqa a'zolarini o'zgartiring.",
        "en": "Change the records themselves, or the other members of the wrapping object.",
    },
    "patch.error.container_type": {
        "uz": "JSON Patch, #{index} amal ({op} \"{path}\"): yozuvlar konteyneri (\"{pointer}\") {expected} bo'lib "
              "qolishi kerak.",
        "en": "JSON Patch, operation #{index} ({op} \"{path}\"): the record container (\"{pointer}\") must stay an "
              "{expected}.",
    },
    "patch.error.root": {
        "uz": "JSON Patch, #{index} amal ({op} \"{path}\"): butun hujjatni o'chirib yoki ko'chirib bo'lmaydi.",
        "en": "JSON Patch, operation #{index} ({op} \"{path}\"): the whole document cannot be removed or moved.",
    },
    "patch.error.huge_record": {
        "uz": "JSON Patch, #{index} amal ({op} \"{path}\"): \"{pointer}\" dagi yozuv juda katta ({size}; chegara "
              "{limit}).",
        "en": "JSON Patch, operation #{index} ({op} \"{path}\"): the record at \"{pointer}\" is too large ({size}; "
              "the limit is {limit}).",
    },
    "patch.error.huge_record.hint": {
        "uz": "Chegara — Sozlamalardagi «Katta tugun chegarasi (hajm)». Uni oshirsangiz, ko'proq xotira kerak bo'ladi.",
        "en": "The limit is the \"Large node threshold (size)\" setting. Raising it needs more memory.",
    },
    "patch.error.huge_unit": {
        "uz": "JSON Patch qo'llangandan keyin \"{pointer}\" dagi yozuv juda katta bo'lib qoladi ({size}; chegara "
              "{limit}).",
        "en": "After the JSON Patch the record at \"{pointer}\" would be too large ({size}; the limit is {limit}).",
    },
    "patch.error.too_large": {
        "uz": "JSON Patch, #{index} amal ({op} \"{path}\"): \"{pointer}\" juda katta, uni nusxalab bo'lmaydi "
              "({size}; chegara {limit}).",
        "en": "JSON Patch, operation #{index} ({op} \"{path}\"): \"{pointer}\" is too large to be copied ({size}; "
              "the limit is {limit}).",
    },
    "patch.detail.too_deep": {"uz": "qiymat juda chuqur joylashgan", "en": "the value is nested too deeply"},
    "patch.warn.copies_unknown": {
        "uz": "{count} ta nusxalangan yozuv patchga kirmadi: ular faylning eski versiyasidagi yozuvlarning nusxasi.",
        "en": "{count} copied records are not in the patch: they copy records of an older version of the file.",
    },
    "patch.warn.keys_unknown": {
        "uz": "{count} ta o'chirilgan yozuvning kaliti noma'lum, ular patchda yo'q.",
        "en": "The keys of {count} deleted records are unknown; they are not in the patch.",
    },
    "patch.warn.history_trimmed": {
        "uz": "Tahrirlar tarixining eng eski qadamlari qisqartirilgan: o'shanda qayta nomlangan yozuvlar patchda "
              "yangi yozuv sifatida chiqishi mumkin.",
        "en": "The oldest steps of the edit history were trimmed: records renamed there may appear as new records "
              "in the patch.",
    },
    "patch.warn.wrapper_partial": {
        "uz": "O'rab turgan obyektdan o'chirilgan a'zolar patchda yo'q (asl fayl o'qilmadi).",
        "en": "Members deleted from the wrapping object are not in the patch (the original file was not read).",
    },
    "patch.warn.damaged": {
        "uz": "Tahrirlar fayli shikastlangan: faqat dastlabki {steps} ta qadam eksport qilindi ({seq}-qadamdan "
              "boshlab o'qib bo'lmadi).",
        "en": "The edit layer is damaged: only the first {steps} steps were exported (step {seq} and later could "
              "not be read).",
    },
    "patch.progress.reading": {"uz": "JSON Patch o'qilmoqda…", "en": "Reading the JSON Patch…"},
    "patch.progress.keys": {"uz": "Yozuv kalitlari qidirilmoqda…", "en": "Looking up the record keys…"},
    "patch.progress.applying": {"uz": "JSON Patch qo'llanmoqda…", "en": "Applying the JSON Patch…"},
    "patch.progress.writing": {"uz": "O'zgarishlar yozilmoqda…", "en": "Writing the changes…"},
    "patch.progress.export": {"uz": "JSON Patch tayyorlanmoqda…", "en": "Preparing the JSON Patch…"},
    # ---- repair ----
    "repair.fix.bom": {"uz": "BOM belgisi olib tashlandi", "en": "byte-order mark (BOM) removed"},
    "repair.fix.comment": {"uz": "izoh olib tashlandi", "en": "comment removed"},
    "repair.fix.trailing_comma": {"uz": "ortiqcha (oxirgi) vergul olib tashlandi", "en": "trailing comma removed"},
    "repair.fix.missing_comma": {"uz": "tushib qolgan vergul qo'shildi", "en": "missing comma added"},
    "repair.fix.single_quote": {
        "uz": "bitta tirnoq qo'sh tirnoqqa almashtirildi",
        "en": "single quotes replaced by double quotes",
    },
    "repair.fix.unquoted_key": {"uz": "kalit qo'sh tirnoqqa olindi", "en": "key put in double quotes"},
    "repair.fix.nan": {"uz": "NaN → {to}", "en": "NaN → {to}"},
    "repair.fix.infinity": {"uz": "Infinity → {to}", "en": "Infinity → {to}"},
    "repair.fix.python_literal": {
        "uz": "True/False/None → true/false/null",
        "en": "True/False/None → true/false/null",
    },
    "repair.fix.undefined": {"uz": "undefined → null", "en": "undefined → null"},
    "repair.fix.control_char": {
        "uz": "satr ichidagi boshqaruv belgisi ekranlandi",
        "en": "control character inside a string escaped",
    },
    "repair.fix.invalid_escape": {"uz": "noto'g'ri escape (\\) tuzatildi", "en": "invalid escape (\\) fixed"},
    "repair.fix.invalid_utf8": {
        "uz": "noto'g'ri UTF-8 bayt U+FFFD belgisiga almashtirildi",
        "en": "invalid UTF-8 bytes replaced by U+FFFD",
    },
    "repair.fix.brace_list": {
        "uz": "nostandart obyektlar ro'yxati massivga aylantirildi",
        "en": "non-standard object list turned into an array",
    },
    "repair.fix.concatenated": {
        "uz": "ketma-ket qiymatlar massivga o'raldi",
        "en": "values one after another wrapped into an array",
    },
    "repair.fix.concatenated_jsonl": {
        "uz": "ketma-ket qiymatlar JSON Lines qatorlariga ajratildi",
        "en": "values one after another written as JSON Lines",
    },
    "repair.fix.line_split": {
        "uz": "bir qatordagi bir nechta qiymat alohida qatorlarga ajratildi",
        "en": "several values on one line split into separate lines",
    },
    "repair.fix.truncated": {
        "uz": "kesilgan fayl oxirgi to'liq yozuvgacha tiklandi",
        "en": "cut-off file restored up to the last complete record",
    },
    "repair.fix.trailing_data": {
        "uz": "ildiz qiymatdan keyingi ortiqcha ma'lumot olib tashlandi",
        "en": "extra data after the root value removed",
    },
    "repair.fix.line_dropped": {
        "uz": "tuzatib bo'lmaydigan qator olib tashlandi",
        "en": "a line that could not be repaired was removed",
    },
    "repair.fix.dropped_tail": {
        "uz": "qisman natija: ta'mirlab bo'lmaydigan yozuvdan boshlab faylning qolgan qismi tashlab yuborildi",
        "en": "partial result: the rest of the file from the unrepairable record on dropped",
    },
    "repair.category.syntax": {"uz": "yozilish (qiymatlar o'zgarmadi)", "en": "syntax (no value changed)"},
    "repair.category.data": {"uz": "ma'lumot o'zgardi", "en": "data changed"},
    "repair.category.dropped": {"uz": "ma'lumot tashlab yuborildi", "en": "data dropped"},
    "repair.problem.at": {
        "uz": "{reason} ({line}-qator, {column}-ustun, {offset}-bayt)",
        "en": "{reason} (line {line}, column {column}, byte {offset})",
    },
    "repair.problem.unexpected_char": {"uz": "bu yerda kutilmagan belgi", "en": "unexpected character here"},
    "repair.problem.mismatched_closer": {
        "uz": "yopuvchi qavs ochilgan qavsga mos kelmaydi",
        "en": "the closing bracket does not match the open one",
    },
    "repair.problem.unquoted_value": {
        "uz": "qo'sh tirnoqsiz matn qiymati (true, false yoki null emas)",
        "en": "a text value without quotes (not true, false or null)",
    },
    "repair.problem.invalid_number": {
        "uz": "son noto'g'ri yozilgan (masalan, 01, 1., .5, 0x1F)",
        "en": "an invalid number (e.g. 01, 1., .5, 0x1F)",
    },
    "repair.problem.depth_limit": {"uz": "ichma-ichlik juda chuqur", "en": "nesting is too deep"},
    "repair.problem.not_utf8": {
        "uz": "noto'g'ri UTF-8 baytlar juda ko'p ({count} tadan ortiq): fayl UTF-8 emas shekilli "
              "(masalan, Windows-1251)",
        "en": "too many invalid UTF-8 bytes (more than {count}): the file is probably not UTF-8 "
              "(e.g. Windows-1251)",
    },
    "repair.problem.nothing_complete": {
        "uz": "saqlab qolinadigan birorta to'liq qiymat yo'q",
        "en": "there is no complete value to keep",
    },
    "repair.problem.extra_value": {
        "uz": "ildiz qiymatdan keyin yana qiymat bor (bir nechta qiymat bo'lsa, formatni stream yoki jsonl "
              "deb bering)",
        "en": "another value follows the root value (for several values give the format stream or jsonl)",
    },
    "repair.problem.disabled": {"uz": "kerakli tuzatish o'chirilgan: {fix}", "en": "a needed fix is turned off: {fix}"},
    "repair.error.unrepairable": {
        "uz": "JSON'ni ta'mirlab bo'lmadi: {reason} ({line}-qator, {column}-ustun, {offset}-bayt). "
              "Hech narsa yozilmadi.",
        "en": "The JSON cannot be repaired: {reason} (line {line}, column {column}, byte {offset}). "
              "Nothing was written.",
    },
    "repair.error.unrepairable.hint": {
        "uz": "Muammodan oldingi to'liq yozuvlarni qisman natija sifatida saqlash mumkin.",
        "en": "The complete records before the problem can be saved as a partial result.",
    },
    "repair.error.option": {
        "uz": "Ta'mirlash parametri noto'g'ri: {name} = {value}",
        "en": "Invalid repair option: {name} = {value}",
    },
    "repair.error.no_target": {"uz": "Natija fayli ko'rsatilmagan.", "en": "No output file was given."},
    "repair.error.report_target": {
        "uz": "Hisobot ta'mirlangan faylning o'ziga yozilmaydi: {path}",
        "en": "The report cannot be written over the repaired file: {path}",
    },
    "repair.error.report_target.hint": {
        "uz": "Hisobot uchun boshqa nom tanlang (masalan, .csv yoki .jsonl kengaytmali).",
        "en": "Choose another name for the report (for example with a .csv or .jsonl extension).",
    },
    "repair.error.output_kind": {
        "uz": "JSON Lines natija faqat ketma-ket qiymatlar yoki JSON Lines fayl uchun; bu fayl: {kind}. "
              "Boshqa formatga o'tkazish uchun convert buyrug'idan foydalaning.",
        "en": "JSON Lines output is only for concatenated values or JSON Lines input; this file is: {kind}. "
              "Use convert for other conversions.",
    },
    "repair.error.invalid_output": {
        "uz": "Ta'mirlangan natija tekshiruvdan o'tmadi ({detail}). Hech narsa yozilmadi; bu dastur xatosi, "
              "tafsilotlar logda.",
        "en": "The repaired output failed the check ({detail}). Nothing was written; this is a program error, "
              "details are in the log.",
    },
    "repair.warn.restarted": {
        "uz": "Fayl aslida {kind} ekan (ildiz qiymatdan keyin yana qiymatlar bor); ta'mirlash shu tur bo'yicha "
              "qaytadan bajarildi.",
        "en": "The file is really {kind} (more values follow the root value); the repair ran again as that "
              "kind.",
    },
    "repair.warn.transcoded": {
        "uz": "Fayl {encoding} kodlashda edi; natija UTF-8 da yozildi.",
        "en": "The file was {encoding}; the result is written as UTF-8.",
    },
    "repair.warn.uncompressed": {
        "uz": "Manba gzip bilan siqilgan edi; natija siqilmagan holda yozildi (siqish uchun nomi .gz bilan "
              "tugashi kerak).",
        "en": "The source was gzip-compressed; the result is written uncompressed (name it .gz to compress "
              "it).",
    },
    "repair.warn.compressed": {
        "uz": "Natija gzip bilan siqildi (nomi .gz bilan tugaydi).",
        "en": "The result is gzip-compressed (its name ends with .gz).",
    },
    "repair.warn.record_path_missing": {
        "uz": "Yozuvlar yo'li topilmadi yoki u massiv/obyekt emas: {path}",
        "en": "The record path was not found or is not an array/object: {path}",
    },
    "repair.progress.repair": {"uz": "Ta'mirlanmoqda: {name}", "en": "Repairing {name}"},
    "repair.progress.check": {"uz": "Tekshirilmoqda: {name}", "en": "Checking {name}"},
    "repair.summary.written": {
        "uz": "Ta'mirlandi: {source} → {target} ({size}, {records} ta yozuv, {seconds})",
        "en": "Repaired: {source} → {target} ({size}, {records} records, {seconds})",
    },
    "repair.summary.checked": {
        "uz": "Tekshirildi: {source} ({size}, {records} ta yozuv, {seconds})",
        "en": "Checked: {source} ({size}, {records} records, {seconds})",
    },
    "repair.summary.not_written": {
        "uz": "Ta'mirlab bo'lmadi: {source} (hech narsa yozilmadi)",
        "en": "Not repaired: {source} (nothing was written)",
    },
    "repair.summary.none": {
        "uz": "Muammo topilmadi; o'zgarishsiz nusxa yozildi.",
        "en": "No problems found; an unchanged copy was written.",
    },
    "repair.summary.none_check": {"uz": "Muammo topilmadi.", "en": "No problems found."},
    "repair.summary.fix": {
        "uz": "  {name}: {count} ta (birinchisi {line}-qator, {column}-ustun)",
        "en": "  {name}: {count} (first at line {line}, column {column})",
    },
    "repair.summary.fix_count": {"uz": "  {name}: {count} ta", "en": "  {name}: {count}"},
    "repair.summary.truncated": {
        "uz": "  kesilgan joy: {line}-qator, {column}-ustun; saqlangan yozuvlar: {kept}; tashlab yuborildi: "
              "{dropped}; qo'shildi: {closers}",
        "en": "  cut at line {line}, column {column}; records kept: {kept}; dropped: {dropped}; appended: "
              "{closers}",
    },
    "repair.summary.problem": {
        "uz": "Ta'mirlab bo'lmaydigan joy: {reason} ({line}-qator, {column}-ustun)",
        "en": "Unrepairable: {reason} (line {line}, column {column})",
    },
    "repair.summary.partial": {
        "uz": "Muammodan oldingi {kept} ta yozuvni qisman natija sifatida saqlash mumkin.",
        "en": "The {kept} records before the problem can be saved as a partial result.",
    },
    "repair.summary.partial_written": {
        "uz": "Qisman natija yozildi: muammodan oldingi {kept} ta yozuv.",
        "en": "Partial result written: the {kept} records before the problem.",
    },
    "repair.summary.data_changed": {
        "uz": "Ma'lumotni o'zgartirgan yoki tashlab yuborgan tuzatishlar: {items}.",
        "en": "Fixes that changed or dropped data: {items}.",
    },
    "repair.summary.data_item": {"uz": "{name} {count} ta", "en": "{name} {count}"},
    # ---- mapping ----
    # toasts of the designer operations (Uzbek = json-tuzilma.html verbatim)
    "mapping.toast.merged": {
        "uz": "«{node}» elementlari «{target}» massiviga qo'shildi",
        "en": "The elements of «{node}» were added to the «{target}» array",
    },
    "mapping.toast.merged_root": {
        "uz": "«{node}» elementlari «ildiz» massiviga qo'shildi",
        "en": "The elements of «{node}» were added to the root array",
    },
    "mapping.toast.moved_renamed": {
        "uz": "«{old}» nomi band edi, «{new}» deb qo'yildi",
        "en": "The name «{old}» was taken, so the field was placed as «{new}»",
    },
    "mapping.toast.deleted": {
        "uz": "«{name}» natijadan olib tashlandi",
        "en": "«{name}» was removed from the result",
    },
    "mapping.toast.deleted_elem": {
        "uz": "«element» natijadan olib tashlandi",
        "en": "These elements were removed from the result",
    },
    "mapping.toast.unwrapped": {
        "uz": "«{name}» ichidagi {count} ta maydon bir pog'ona yuqoriga chiqdi",
        "en": "{count} fields of «{name}» moved one level up",
    },
    "mapping.toast.unwrapped.one": {
        "uz": "«{name}» ichidagi {count} ta maydon bir pog'ona yuqoriga chiqdi",
        "en": "{count} field of «{name}» moved one level up",
    },
    "mapping.toast.unwrapped_renamed": {
        "uz": "«{name}» ichidagi {count} ta maydon bir pog'ona yuqoriga chiqdi, "
              "{renamed} tasining nomiga raqam qo'shildi",
        "en": "Fields moved one level up from «{name}»: {count} (renamed with a number: {renamed})",
    },
    "mapping.toast.renamed_unique": {
        "uz": "«{name}» band edi, «{new}» deb nomlandi",
        "en": "«{name}» was taken, so the field was named «{new}»",
    },
    "mapping.toast.flattened": {
        "uz": "«{key}» ichidagi barcha darajalar bitta massivga yig'ildi: {total} ta element",
        "en": "All levels of «{key}» were collected into one array: {total} elements",
    },
    "mapping.toast.flattened.one": {
        "uz": "«{key}» ichidagi barcha darajalar bitta massivga yig'ildi: {total} ta element",
        "en": "All levels of «{key}» were collected into one array: {total} element",
    },
    "mapping.toast.undone": {"uz": "Oxirgi o'zgarish bekor qilindi", "en": "The last change was undone"},
    "mapping.toast.redone": {
        "uz": "Bekor qilingan o'zgarish qayta bajarildi",
        "en": "The undone change was made again",
    },
    "mapping.toast.reset": {"uz": "Asl tuzilma tiklandi", "en": "The original structure was restored"},
    # tree rows: labels, type chips, meta, samples, header
    "mapping.label.each_record": {"uz": "har bir yozuv", "en": "each record"},
    "mapping.label.each_element": {"uz": "har bir element", "en": "each element"},
    "mapping.label.each_member": {"uz": "har bir a'zo", "en": "each member"},
    "mapping.label.root": {"uz": "ildiz", "en": "root"},
    "mapping.label.root_path": {"uz": "(ildiz)", "en": "(root)"},
    "mapping.default_group_key": {"uz": "yangi_guruh", "en": "new_group"},
    "mapping.type.object": {"uz": "obyekt", "en": "object"},
    "mapping.type.array": {"uz": "massiv", "en": "array"},
    "mapping.type.string": {"uz": "matn", "en": "text"},
    "mapping.type.number": {"uz": "son", "en": "number"},
    "mapping.type.boolean": {"uz": "mantiqiy", "en": "true/false"},
    "mapping.type.null": {"uz": "null", "en": "null"},
    "mapping.type.empty": {"uz": "bo'sh", "en": "empty"},
    "mapping.type.empty.tip": {"uz": "massiv doim bo'sh", "en": "the array is always empty"},
    "mapping.meta.pct.tip": {
        "uz": "{count} / {pcount} ta obyektda bor",
        "en": "present in {count} of {pcount} objects",
    },
    "mapping.meta.items": {"uz": "{count} ta", "en": "{count} items"},
    "mapping.meta.items.one": {"uz": "{count} ta", "en": "{count} item"},
    "mapping.meta.records": {"uz": "{count} ta yozuv", "en": "{count} records"},
    "mapping.meta.records.one": {"uz": "{count} ta yozuv", "en": "{count} record"},
    "mapping.meta.members": {"uz": "{count} ta a'zo", "en": "{count} members"},
    "mapping.meta.members.one": {"uz": "{count} ta a'zo", "en": "{count} member"},
    "mapping.stats.fields": {"uz": "{count} maydon", "en": "{count} fields"},
    "mapping.stats.fields.one": {"uz": "{count} maydon", "en": "{count} field"},
    "mapping.stats.changes": {"uz": "{count} o'zgarish", "en": "{count} changes"},
    "mapping.stats.changes.one": {"uz": "{count} o'zgarish", "en": "{count} change"},
    "mapping.more": {"uz": "yana {count} ta maydonni ko'rsatish", "en": "show {count} more fields"},
    # badges (order and colours of the prototype)
    "mapping.badge.group": {"uz": "yangi guruh", "en": "new group"},
    "mapping.badge.depth": {"uz": "daraja raqami", "en": "level number"},
    "mapping.badge.depth.tip": {
        "uz": "Element qaysi darajadan kelgani: 1 = bevosita, 2 = uning ichidagisi…",
        "en": "The level the element came from: 1 = direct, 2 = one inside it…",
    },
    "mapping.badge.recursive": {"uz": "rekursiv · {key}", "en": "recursive · {key}"},
    "mapping.badge.recursive.tip": {
        "uz": "«{key}» ichidagi barcha darajalar shu massivga tushadi",
        "en": "All levels of «{key}» go into this array",
    },
    "mapping.badge.source": {"uz": "manba: {path}", "en": "source: {path}"},
    "mapping.badge.source.tip": {
        "uz": "Bu elementlar boshqa massivdan qo'shilgan",
        "en": "These elements were added from another array",
    },
    "mapping.badge.moved": {"uz": "ko'chirildi", "en": "moved"},
    "mapping.badge.moved.tip": {"uz": "Asl joyi: {path}", "en": "Original place: {path}"},
    "mapping.badge.orig_name": {"uz": "asl nomi: {name}", "en": "original name: {name}"},
    "mapping.badge.gathered": {"uz": "ro'yxatga yig'iladi", "en": "collected into a list"},
    "mapping.badge.gathered.tip": {
        "uz": "Massiv ichidan olingan: har bir elementdagi qiymat bitta ro'yxatga yig'iladi",
        "en": "Taken from inside an array: the value of every element is collected into one list",
    },
    "mapping.badge.broadcast": {"uz": "har elementga takrorlanadi", "en": "repeated in every element"},
    "mapping.badge.broadcast.tip": {
        "uz": "Massivdan tashqaridan olingan: har bir elementga bir xil qiymat yoziladi",
        "en": "Taken from outside the array: the same value is written into every element",
    },
    "mapping.badge.opaque": {"uz": "butunicha", "en": "copied whole"},
    "mapping.badge.opaque.tip": {
        "uz": "Kalitlari juda ko'p yoki juda chuqur: qiymat o'zgarishsiz ko'chiriladi",
        "en": "Too many keys or too deep: the value is copied unchanged",
    },
    # row actions (tooltips and short labels)
    "mapping.action.group": {"uz": "Ichiga yangi guruh qo'shish", "en": "Add a new group inside"},
    "mapping.action.group.label": {"uz": "Yangi guruh", "en": "New group"},
    "mapping.action.unwrap": {
        "uz": "Ichidagilarni bir pog'ona yuqoriga chiqarish",
        "en": "Move the contents one level up",
    },
    "mapping.action.unwrap.label": {"uz": "Ichidagilarni chiqarish", "en": "Unwrap"},
    "mapping.action.flatten": {
        "uz": "Ichma-ich elementlarni (hamma darajasini) shu massivga tekislash",
        "en": "Flatten the nested elements (all levels) into this array",
    },
    "mapping.action.flatten.label": {"uz": "Rekursiv tekislash", "en": "Flatten recursively"},
    "mapping.action.rename": {"uz": "Nomini o'zgartirish", "en": "Rename"},
    "mapping.action.delete": {"uz": "Natijadan olib tashlash", "en": "Remove from the result"},
    "mapping.action.delete.label": {"uz": "Olib tashlash", "en": "Remove"},
    # why an operation is not possible (refused operations, disabled actions)
    "mapping.why.missing": {"uz": "Bu tugun endi tuzilmada yo'q.", "en": "This node is no longer in the structure."},
    "mapping.why.root": {
        "uz": "Ildiz qatorini ko'chirib, nomlab yoki o'chirib bo'lmaydi.",
        "en": "The root row cannot be moved, renamed or removed.",
    },
    "mapping.why.elem": {
        "uz": "«Har bir yozuv / element / a'zo» qatorini ko'chirib, nomini o'zgartirib yoki ichidagilarini chiqarib "
              "bo'lmaydi.",
        "en": "An «each record / element / member» row cannot be moved, renamed or unwrapped.",
    },
    "mapping.why.last_elem": {
        "uz": "Massivning yagona element manbasini o'chirib bo'lmaydi; massivning o'zini o'chiring.",
        "en": "The only element source of an array cannot be removed; remove the array itself.",
    },
    "mapping.why.not_object": {
        "uz": "Bu amal faqat obyekt (guruh) tugunlari uchun.",
        "en": "This works only on object (group) nodes.",
    },
    "mapping.why.into_itself": {
        "uz": "Tugunni o'zining ustiga yoki o'z ichidagi tugunga ko'chirib bo'lmaydi.",
        "en": "A node cannot be moved onto itself or into one of its own descendants.",
    },
    "mapping.why.array_only": {
        "uz": "Massiv ustiga faqat massiv tashlanadi.",
        "en": "Only an array can be dropped onto an array.",
    },
    "mapping.why.no_sibling": {
        "uz": "Ildiz yoki «har bir yozuv / element / a'zo» qatorining oldiga yoki ortiga qo'yib bo'lmaydi.",
        "en": "Nothing can be placed before or after the root or an «each record / element / member» row.",
    },
    "mapping.why.no_zone": {"uz": "Bu yerga tashlab bo'lmaydi.", "en": "It cannot be dropped here."},
    "mapping.why.first": {"uz": "Bu qator allaqachon birinchi.", "en": "This row is already the first one."},
    "mapping.why.last": {"uz": "Bu qator allaqachon oxirgi.", "en": "This row is already the last one."},
    "mapping.why.empty_name": {"uz": "Nom bo'sh bo'lmasligi kerak.", "en": "The name must not be empty."},
    "mapping.why.same_name": {"uz": "Nom o'zgarmadi.", "en": "The name did not change."},
    "mapping.why.not_flattenable": {
        "uz": "Bu massiv elementlarida xuddi shunday ichki massiv yo'q (masalan, children ichida children).",
        "en": "The elements of this array hold no nested array of the same kind (such as children "
              "inside children).",
    },
    "mapping.why.no_data": {
        "uz": "Tuzilmani qayta aniqlash uchun yozuvlar berilmagan.",
        "en": "No records were given to re-infer the structure from.",
    },
    "mapping.why.nothing_to_undo": {"uz": "Bekor qilinadigan o'zgarish yo'q.", "en": "There is nothing to undo."},
    "mapping.why.nothing_to_redo": {"uz": "Qaytariladigan o'zgarish yo'q.", "en": "There is nothing to redo."},
    "mapping.why.stale": {
        "uz": "Tuzilma shu orada o'zgardi; amalni qaytadan bajaring.",
        "en": "The structure changed in the meantime; run the operation again.",
    },
    # mapping files and mappings that do not fit the input
    "mapping.error.invalid": {"uz": "Mapping noto'g'ri: {detail}", "en": "Invalid mapping: {detail}"},
    "mapping.error.json": {
        "uz": "Mapping fayli to'g'ri JSON emas: {detail}",
        "en": "The mapping file is not valid JSON: {detail}",
    },
    "mapping.error.file_too_big": {
        "uz": "Mapping fayli juda katta: {path} ({size} bayt, chegara {limit}).",
        "en": "The mapping file is too big: {path} ({size} bytes, limit {limit}).",
    },
    "mapping.error.file_too_big.hint": {
        "uz": "Ma'lumot fayli tanlanmaganini tekshiring: mapping fayli tuzilma quruvchida saqlangan .json bo'ladi.",
        "en": "Check that this is not the data file: a mapping file is the .json saved by the structure builder.",
    },
    "mapping.error.not_object": {
        "uz": "Mapping fayli JSON obyekt bo'lishi kerak (version, input va tree maydonlari bilan).",
        "en": "A mapping file must be a JSON object (with the fields version, input and tree).",
    },
    "mapping.error.version": {
        "uz": "Mapping fayli versiyasi qo'llab-quvvatlanmaydi: {version} (kutilgan: 1).",
        "en": "Unsupported mapping file version: {version} (expected 1).",
    },
    "mapping.error.input": {
        "uz": "Mapping faylidagi «input» obyekt bo'lishi kerak.",
        "en": "«input» in the mapping file must be an object.",
    },
    "mapping.error.no_tree": {
        "uz": "Mapping faylida «tree» (tuzilma daraxti) yo'q.",
        "en": "The mapping file has no «tree» (structure tree).",
    },
    "mapping.error.node": {
        "uz": "Tuzilma tuguni JSON obyekt bo'lishi kerak ({pointer}).",
        "en": "A structure node must be a JSON object ({pointer}).",
    },
    "mapping.error.kind": {
        "uz": "Tugun turi noma'lum: {kind} ({pointer}); object, array yoki value bo'lishi kerak.",
        "en": "Unknown node kind {kind} ({pointer}); it must be object, array or value.",
    },
    "mapping.error.field": {
        "uz": "Tugunning «{field}» maydoni noto'g'ri ({pointer}).",
        "en": "The «{field}» field of the node is invalid ({pointer}).",
    },
    "mapping.error.src": {
        "uz": "Manba yo'li (src) noto'g'ri ({pointer}): kalitlar ro'yxati bo'lishi kerak, massiv qadami "
              "uchun null.",
        "en": "Invalid source path (src) ({pointer}): it must be a list of keys, with null for an array "
              "step.",
    },
    "mapping.error.ctx": {
        "uz": "ctx noto'g'ri ({pointer}): u faqat hisoblangan tugunda bo'ladi va yo'l ro'yxati bo'lishi kerak.",
        "en": "Invalid ctx ({pointer}): only computed nodes have it, and it must be a path list.",
    },
    "mapping.error.root": {
        "uz": "Ildiz faqat eng yuqori tugun bo'ladi va u element bo'lmaydi ({pointer}).",
        "en": "Only the top node is the root, and it is not an element ({pointer}).",
    },
    "mapping.error.root_src": {
        "uz": "Ildiz tugunining manbasi bo'sh ro'yxat bo'lishi kerak ({pointer}).",
        "en": "The source of the root node must be an empty list ({pointer}).",
    },
    "mapping.error.elem_src": {
        "uz": "Element tugunining manbasi massiv qadami (null) bilan tugashi kerak ({pointer}).",
        "en": "The source of an element node must end with an array step (null) ({pointer}).",
    },
    "mapping.error.field_src": {
        "uz": "Maydon tugunining manbasi kalit bilan tugashi kerak ({pointer}).",
        "en": "The source of a field node must end with a key ({pointer}).",
    },
    "mapping.error.synthetic_src": {
        "uz": "Yangi guruh va hisoblangan tugunlarning manbasi null bo'lishi kerak ({pointer}).",
        "en": "New groups and computed nodes must have the source null ({pointer}).",
    },
    "mapping.error.array_children": {
        "uz": "Massiv tugunida kamida bitta element tuguni bo'lishi va boshqa tugun bo'lmasligi kerak ({pointer}).",
        "en": "An array node must hold at least one element node and nothing else ({pointer}).",
    },
    "mapping.error.elem_outside_array": {
        "uz": "Element tuguni faqat massiv tuguni ichida bo'ladi ({pointer}).",
        "en": "An element node can only be inside an array node ({pointer}).",
    },
    "mapping.error.value_children": {
        "uz": "Qiymat tugunining ichida boshqa tugunlar bo'lmaydi ({pointer}).",
        "en": "A value node cannot hold other nodes ({pointer}).",
    },
    "mapping.error.synthetic": {
        "uz": "Yangi guruh (synthetic) obyekt turidagi oddiy tugun bo'lishi kerak ({pointer}).",
        "en": "A new group (synthetic) must be an ordinary node of kind object ({pointer}).",
    },
    "mapping.error.computed": {
        "uz": "Hisoblangan tugun noto'g'ri ({pointer}): computed faqat depth, turi value, ctx esa "
              "element manbasi bo'ladi.",
        "en": "Invalid computed node ({pointer}): computed can only be depth, the kind value, and ctx "
              "an element source.",
    },
    "mapping.error.rec_key": {
        "uz": "rec_key faqat obyekt turidagi element tugunida bo'ladi ({pointer}).",
        "en": "rec_key is allowed only on element nodes of kind object ({pointer}).",
    },
    "mapping.error.root_kind": {
        "uz": "Ildiz turi mos emas ({pointer}): {kind}, yozuvlar hujjati uchun esa massiv ildiz kerak "
              "({root_kind}).",
        "en": "The root kind does not fit ({pointer}): {kind}, but a records document needs an array "
              "root ({root_kind}).",
    },
    "mapping.error.root_kind_name": {
        "uz": "Noma'lum ildiz turi: {kind}; records, value, wrapped yoki keyed bo'lishi kerak.",
        "en": "Unknown root kind {kind}; it must be records, value, wrapped or keyed.",
    },
    "mapping.error.record_path": {
        "uz": "input.record_path noto'g'ri: kalitlar va indekslar ro'yxati bo'lishi kerak.",
        "en": "Invalid input.record_path: it must be a list of keys and indexes.",
    },
    "mapping.error.known_paths": {
        "uz": "known_paths noto'g'ri ({pointer}): manba yo'llari ro'yxati bo'lishi kerak.",
        "en": "Invalid known_paths ({pointer}): it must be a list of source paths.",
    },
    "mapping.error.designer": {
        "uz": "designer qismi noto'g'ri ({pointer}): obyekt bo'lishi kerak.",
        "en": "Invalid designer section ({pointer}): it must be an object.",
    },
    "mapping.warn.truncated": {
        "uz": "Tuzilma juda katta: 1000 tadan ko'p kalitli, juda chuqur yoki sig'magan qismlar butunicha "
              "ko'chiriladigan qiymat bo'lib qoldi.",
        "en": "The structure is very large: parts with more than 1000 keys, very deep parts or parts "
              "beyond the limit stay values that are copied whole.",
    },
    # ---- engine ----
    # mapping evaluation and the streaming apply (engine.py, docs/ARCHITECTURE.md §13.2-§13.3)
    "engine.error.indent": {
        "uz": "Chekinish qiymati noto'g'ri: {value} (2, 4, tab yoki ixcham bo'lishi mumkin).",
        "en": "Invalid indent: {value} (use 2, 4, tab or compact).",
    },
    "engine.error.workers": {
        "uz": "Jarayonlar soni noto'g'ri: {value} (1 yoki undan katta butun son bo'lishi kerak).",
        "en": "Invalid number of worker processes: {value} (a whole number of 1 or more).",
    },
    "engine.error.huge_record": {
        "uz": "#{record} yozuv juda katta ({size}, chegara {limit}): uni xotirada qayta qurib bo'lmaydi.",
        "en": "Record #{record} is too large ({size}, limit {limit}): it cannot be restructured in memory.",
    },
    "engine.error.huge_record.hint": {
        "uz": "Bunday yozuvlarni o'tkazib yuborish uchun --skip-huge ni qo'shing yoki sozlamalarda katta tugun "
              "chegarasini oshiring.",
        "en": "Add --skip-huge to skip such records, or raise the large node limit in the settings.",
    },
    "engine.error.unreadable": {
        "uz": "#{record} yozuvni o'qib bo'lmadi: {detail}",
        "en": "Record #{record} could not be read: {detail}",
    },
    "engine.error.kind_mismatch": {
        "uz": "Bu tuzilma {mapping} uchun tayyorlangan, kiruvchi fayl esa: {input}.",
        "en": "This mapping was made for {mapping}, but the input is: {input}.",
    },
    "engine.error.kind_mismatch.hint": {
        "uz": "Tuzilmani shu fayldan qayta yarating yoki yozuvlar yo'lini --record-path bilan ko'rsating.",
        "en": "Build the mapping from this file again, or give the record path with --record-path.",
    },
    "engine.error.partial_wrapper": {
        "uz": "Yozuvlar massivi yopilmagan (fayl oxirigacha to'liq emas): o'rovchi obyektga tuzilmani qo'llab "
              "bo'lmaydi.",
        "en": "The record array is never closed (the file is incomplete): the mapping cannot be applied to the "
              "wrapper.",
    },
    "engine.error.member_container": {
        "uz": "Yozuvlar o'rovchi ichidagi obyektning a'zolari: bunday faylga tuzilmani qo'llab bo'lmaydi.",
        "en": "The records are members of an object inside the wrapper: a mapping cannot be applied to such a "
              "file.",
    },
    "engine.error.member_container.hint": {
        "uz": "Massivga olib boradigan yozuvlar yo'lini tanlang.",
        "en": "Choose a record path that leads to an array.",
    },
    "engine.error.too_large": {
        "uz": "Fayl bitta qiymat sifatida qayta qurish uchun juda katta ({size}, chegara {limit}).",
        "en": "The document is too large to restructure as one value ({size}, limit {limit}).",
    },
    "engine.error.too_large.hint": {
        "uz": "Yozuvlar yo'lini ko'rsating (--record-path): shunda yozuvlar birma-bir qayta quriladi.",
        "en": "Give a record path (--record-path) so that the records are restructured one by one.",
    },
    "engine.root_kind.records": {"uz": "yozuvlar massivi", "en": "an array of records"},
    "engine.root_kind.value": {"uz": "bitta qiymat", "en": "a single value"},
    "engine.root_kind.wrapped": {"uz": "obyekt ichidagi massiv", "en": "an array inside an object"},
    "engine.root_kind.keyed": {"uz": "kalitli obyekt", "en": "a keyed object"},
    "engine.progress.index": {"uz": "Indekslanmoqda: {name}", "en": "Indexing {name}"},
    "engine.progress.apply": {"uz": "Tuzilma qo'llanmoqda: {name}", "en": "Applying the mapping: {name}"},
    "engine.progress.pass": {
        "uz": "Tuzilma qo'llanmoqda: {name} ({n}-o'tish)",
        "en": "Applying the mapping: {name} (pass {n})",
    },
    "engine.warn.partial": {
        "uz": "Manba faylda JSON xatosi bor: faqat xatogacha bo'lgan {count} ta yozuv qayta qurildi.",
        "en": "The source file has a JSON error: only the {count} records before the error were restructured.",
    },
    "engine.warn.partial.one": {
        "uz": "Manba faylda JSON xatosi bor: faqat xatogacha bo'lgan {count} ta yozuv qayta qurildi.",
        "en": "The source file has a JSON error: only the {count} record before the error was restructured.",
    },
    "engine.warn.broken_lines": {
        "uz": "Manbadagi {count} ta buzuq qator o'tkazib yuborildi.",
        "en": "{count} broken lines of the source were skipped.",
    },
    "engine.warn.broken_lines.one": {
        "uz": "Manbadagi {count} ta buzuq qator o'tkazib yuborildi.",
        "en": "{count} broken line of the source was skipped.",
    },
    "engine.warn.not_array": {
        "uz": "Hech bir yozuvda massiv topilmadi, natija massiv emas: etalon qoidasiga ko'ra birinchi topilgan "
              "qiymat (yoki null) yozildi.",
        "en": "No record reached an array, so the result is not an array: by the reference rule the first value "
              "found (or null) was written.",
    },
    "engine.warn.unmapped": {
        "uz": "{count} ta yozuvda tuzilmada yo'q maydonlar bor edi, ular natijaga yozilmadi. Tuzilmani to'liq "
              "skanerlash bilan qayta yarating.",
        "en": "{count} records had fields the mapping does not know; those fields were not written. Build the "
              "mapping again with a full scan.",
    },
    "engine.warn.unmapped.one": {
        "uz": "{count} ta yozuvda tuzilmada yo'q maydonlar bor edi, ular natijaga yozilmadi. Tuzilmani to'liq "
              "skanerlash bilan qayta yarating.",
        "en": "{count} record had fields the mapping does not know; those fields were not written. Build the "
              "mapping again with a full scan.",
    },
    "engine.warn.unmapped_wrapper": {
        "uz": "O'rovchi obyektda tuzilmada yo'q maydonlar bor edi, ular natijaga yozilmadi.",
        "en": "The wrapper had fields the mapping does not know; they were not written.",
    },
    "engine.warn.skipped_huge": {
        "uz": "{count} ta juda katta yozuv ({limit} dan katta) o'tkazib yuborildi.",
        "en": "{count} very large records (over {limit}) were skipped.",
    },
    "engine.warn.skipped_huge.one": {
        "uz": "{count} ta juda katta yozuv ({limit} dan katta) o'tkazib yuborildi.",
        "en": "{count} very large record (over {limit}) was skipped.",
    },
    "engine.warn.passes": {
        "uz": "Yozuvlar {count} marta o'qildi (birlashtirilgan massivlar uchun alohida o'tishlar).",
        "en": "The records were read {count} times (separate passes for merged arrays).",
    },
    "engine.warn.records_hidden": {
        "uz": "O'rovchida yozuvlar massivi bilan bir xil nomli keyingi kalit bor: JSON qoidasiga ko'ra keyingisi "
              "olinadi, shuning uchun yozuvlar natijaga kirmadi.",
        "en": "The wrapper repeats the key of the record array: by the JSON rules the later value wins, so the "
              "records are not in the result.",
    },
    "engine.warn.record_path": {
        "uz": "Tuzilma {mapping} dagi yozuvlar uchun tayyorlangan, bu faylning yozuvlari esa {input} da.",
        "en": "The mapping was made for records at {mapping}, but the records of this file are at {input}.",
    },
    "engine.warn.jsonl_wrapper": {
        "uz": "JSON Lines faqat yozuvlarni saqlaydi: o'rovchi obyektning boshqa maydonlari yozilmadi.",
        "en": "JSON Lines holds only the records: the other fields of the wrapper were not written.",
    },
    # ---- workspace ----
    "workspace.error.invalid": {
        "uz": "Workspace fayli noto'g'ri ({pointer}).",
        "en": "The workspace file is invalid ({pointer}).",
    },
    "workspace.error.json": {
        "uz": "Workspace fayli JSON sifatida o'qilmadi: {detail}",
        "en": "The workspace file is not valid JSON: {detail}",
    },
    "workspace.error.json.hint": {
        "uz": "Fayl buzilgan yoki boshqa turdagi fayl bo'lishi mumkin. Workspace'ni qaytadan saqlang.",
        "en": "The file may be damaged or of another kind. Save the workspace again.",
    },
    "workspace.error.not_object": {
        "uz": "Workspace fayli JSON obyekt bo'lishi kerak.",
        "en": "A workspace file must be a JSON object.",
    },
    "workspace.error.version": {
        "uz": "Workspace fayli versiyasi ({version}) qo'llab-quvvatlanmaydi: bu dastur {expected}-versiyani o'qiydi.",
        "en": "Workspace file version {version} is not supported: this program reads version {expected}.",
    },
    "workspace.error.missing": {
        "uz": "Workspace faylida {pointer} yo'q.",
        "en": "The workspace file has no {pointer}.",
    },
    "workspace.error.type": {
        "uz": "Workspace faylidagi {pointer} noto'g'ri: {expected} bo'lishi kerak.",
        "en": "{pointer} in the workspace file is wrong: it must be {expected}.",
    },
    "workspace.error.value": {
        "uz": "Workspace faylidagi {pointer} qiymati noto'g'ri: {detail}",
        "en": "{pointer} in the workspace file has an invalid value: {detail}",
    },
    "workspace.error.too_many": {
        "uz": "Workspace faylidagi {pointer} juda ko'p: {count} ta (ko'pi bilan {limit} ta).",
        "en": "{pointer} in the workspace file has too many items: {count} (at most {limit}).",
    },
    "workspace.error.mapping": {
        "uz": "Workspace'dagi tuzilma noto'g'ri ({pointer}): {detail}",
        "en": "The structure in the workspace is invalid ({pointer}): {detail}",
    },
    "workspace.error.too_big": {
        "uz": "Workspace fayli juda katta: {size} (ko'pi bilan {limit}): {path}",
        "en": "The workspace file is too large: {size} (at most {limit}): {path}",
    },
    "workspace.type.string": {"uz": "matn", "en": "a string"},
    "workspace.type.bool": {"uz": "true yoki false", "en": "true or false"},
    "workspace.type.int": {"uz": "butun son", "en": "a whole number"},
    "workspace.type.list": {"uz": "ro'yxat (JSON massiv)", "en": "a list (JSON array)"},
    "workspace.type.object": {"uz": "JSON obyekt", "en": "a JSON object"},
    "workspace.type.string_or_list": {"uz": "matn yoki ro'yxat", "en": "a string or a list"},
    # ---- importers ----
    # import of CSV / TSV, YAML and XML into JSON / JSON Lines (importers.py, docs/ARCHITECTURE.md §17)
    "importers.format.csv": {"uz": "CSV / TSV", "en": "CSV / TSV"},
    "importers.format.yaml": {"uz": "YAML", "en": "YAML"},
    "importers.format.xml": {"uz": "XML", "en": "XML"},
    "importers.progress.csv": {"uz": "CSV o'qilmoqda", "en": "Reading the CSV"},
    "importers.progress.yaml": {"uz": "YAML tahlil qilinmoqda", "en": "Parsing the YAML"},
    "importers.progress.xml": {"uz": "XML o'qilmoqda", "en": "Reading the XML"},
    "importers.error.option": {
        "uz": "Import parametri noto'g'ri: {name} = {value} (kutilgani: {expected})",
        "en": "Invalid import option: {name} = {value} (expected: {expected})",
    },
    "importers.error.encoding_unknown": {"uz": "Noma'lum kodirovka: {encoding}", "en": "Unknown encoding: {encoding}"},
    "importers.error.record_both": {
        "uz": "Yozuv tegi va yozuv yo'lini birga berib bo'lmaydi: bittasini tanlang.",
        "en": "A record tag and a record path cannot be given together: choose one.",
    },
    "importers.error.unknown_format": {
        "uz": "Kirish faylining turi kengaytmasidan aniqlanmadi: {path}",
        "en": "The type of the input file could not be told from its extension: {path}",
    },
    "importers.error.unknown_format.hint": {
        "uz": "Tanilgan kengaytmalar: {extensions}. Turini qo'lda bering: --from csv|yaml|xml.",
        "en": "Known extensions: {extensions}. Give the type by hand: --from csv|yaml|xml.",
    },
    "importers.error.gzip": {
        "uz": "Siqilgan (gzip) faylni import qilib bo'lmaydi: {path}",
        "en": "A gzip-compressed file cannot be imported: {path}",
    },
    "importers.error.gzip.hint": {
        "uz": "Avval faylni oching (gunzip / 7-Zip), keyin import qiling.",
        "en": "Unpack the file first (gunzip / 7-Zip), then import it.",
    },
    "importers.error.utf32": {
        "uz": "UTF-32 kodirovkali fayl import qilinmaydi: {path}. Uni UTF-8 ga o'giring.",
        "en": "A UTF-32 encoded file is not imported: {path}. Convert it to UTF-8 first.",
    },
    "importers.error.decode": {
        "uz": "Faylni {encoding} kodirovkasida o'qib bo'lmadi ({line}-qator atrofida): {detail}",
        "en": "The file could not be read as {encoding} (around line {line}): {detail}",
    },
    "importers.error.decode.hint": {
        "uz": "Kodirovkani qo'lda bering: --encoding cp1251 (yoki utf-16, latin-1, ...).",
        "en": "Give the encoding by hand: --encoding cp1251 (or utf-16, latin-1, ...).",
    },
    "importers.error.csv_duplicate_header": {
        "uz": "CSV sarlavhasida «{name}» nomi takrorlangan ({columns}-ustunlar).",
        "en": "The CSV header uses the name \"{name}\" more than once (columns {columns}).",
    },
    "importers.error.csv_duplicate_header.hint": {
        "uz": "Ustun nomlarini farqli qiling yoki sarlavhasiz import qiling (--no-header: ustunlar c1..cN).",
        "en": "Make the column names different, or import without a header (--no-header: columns c1..cN).",
    },
    "importers.error.csv_nest_conflict": {
        "uz": "«{first}» ustuni ham qiymat, ham «{second}» ustunining obyekti bo'lishi kerak: ichma-ich qilib "
              "bo'lmaydi.",
        "en": "The column \"{first}\" would be both a value and the object holding \"{second}\": the columns cannot "
              "be nested.",
    },
    "importers.error.csv_nest_conflict.hint": {
        "uz": "Nomlarni tekis kalit sifatida qoldirish uchun --no-nest qo'shing.",
        "en": "Add --no-nest to keep the names as flat keys.",
    },
    "importers.error.csv_malformed": {
        "uz": "{line}-qator: CSV xatosi: {detail}",
        "en": "Line {line}: CSV error: {detail}",
    },
    "importers.error.csv_malformed.hint": {
        "uz": "Buzuq qatorlarni o'tkazib yuborish uchun --skip-bad qo'shing (ular hisobotda sanaladi).",
        "en": "Add --skip-bad to skip malformed rows (they are counted in the report).",
    },
    "importers.error.csv_row_width": {
        "uz": "{line}-qator: {got} ta maydon, sarlavhada esa {expected} ta.",
        "en": "Line {line}: {got} fields, but the header has {expected}.",
    },
    "importers.error.csv_row_width.hint": {
        "uz": "Buzuq qatorlarni o'tkazib yuborish uchun --skip-bad qo'shing; ajratuvchi noto'g'ri aniqlangan "
              "bo'lsa, --delimiter bering.",
        "en": "Add --skip-bad to skip malformed rows; if the delimiter was detected wrongly, give --delimiter.",
    },
    "importers.error.yaml_missing": {
        "uz": "YAML import uchun «pyyaml» kutubxonasi o'rnatilmagan.",
        "en": "The \"pyyaml\" library needed for YAML import is not installed.",
    },
    "importers.error.yaml_missing.hint": {
        "uz": "O'rnatish: pip install pyyaml (yoki pip install jsonreshape[import]).",
        "en": "Install it: pip install pyyaml (or pip install jsonreshape[import]).",
    },
    "importers.error.yaml_too_big": {
        "uz": "YAML fayl juda katta ({size}): YAML butunlay xotiraga o'qiladi, ruxsat etilgani {limit} "
              "(«{setting}» sozlamasining ikki baravari).",
        "en": "The YAML file is too big ({size}): YAML is read into memory whole, and at most {limit} is allowed "
              "(twice the \"{setting}\" setting).",
    },
    "importers.error.yaml": {
        "uz": "YAML xatosi ({line}-qator, {column}-ustun): {reason}",
        "en": "YAML error (line {line}, column {column}): {reason}",
    },
    "importers.error.yaml_plain": {"uz": "YAML xatosi: {reason}", "en": "YAML error: {reason}"},
    "importers.error.yaml_key": {
        "uz": "YAML kalitini JSON matniga aylantirib bo'lmaydi: {key}",
        "en": "A YAML key cannot be turned into JSON text: {key}",
    },
    "importers.error.yaml_expanded": {
        "uz": "YAML alias'lari (takroriy ishoralar) juda ko'p ma'lumotga kengayadi ({count} dan ortiq qiymat); "
              "fayl import qilinmadi.",
        "en": "The YAML aliases expand to too much data (more than {count} values); the file was not imported.",
    },
    "importers.error.yaml_recursive": {
        "uz": "YAML hujjatda o'z-o'ziga ishora qiluvchi (rekursiv) alias bor; JSON bunday tuzilmani ifodalay "
              "olmaydi.",
        "en": "The YAML document has an alias that refers to itself (recursive); JSON cannot express that.",
    },
    "importers.error.yaml_deep": {
        "uz": "YAML hujjat juda chuqur joylashgan (ichma-ich darajalar ko'p); import qilinmadi.",
        "en": "The YAML document is nested too deeply; it was not imported.",
    },
    "importers.error.xml": {
        "uz": "XML xatosi ({line}-qator, {column}-ustun): {reason}",
        "en": "XML error (line {line}, column {column}): {reason}",
    },
    "importers.error.xml_entities": {
        "uz": "XML faylning DOCTYPE qismida ENTITY e'lonlari bor; xavfsizlik uchun bunday fayl import qilinmaydi: "
              "{path}",
        "en": "The DOCTYPE of the XML file declares entities (ENTITY); for safety such a file is not imported: "
              "{path}",
    },
    "importers.error.xml_entities.hint": {
        "uz": "ENTITY e'lonlarini olib tashlang yoki faylni ishonchli vosita bilan oddiy XML'ga aylantiring.",
        "en": "Remove the ENTITY declarations, or expand them with a trusted tool first.",
    },
    "importers.warn.skipped": {
        "uz": "{count} ta buzuq qator o'tkazib yuborildi (birinchilari: {lines}-qatorlar).",
        "en": "{count} malformed rows were skipped (the first ones end at lines {lines}).",
    },
    "importers.warn.skipped.one": {
        "uz": "{count} ta buzuq qator o'tkazib yuborildi ({lines}-qator).",
        "en": "{count} malformed row was skipped (it ends at line {lines}).",
    },
    "importers.warn.no_records": {"uz": "Birorta ham yozuv topilmadi.", "en": "No records were found."},
    "importers.warn.xml_no_records": {
        "uz": "Birorta ham yozuv topilmadi (ildiz elementi: <{root}>). Yozuv elementini --record-tag TEG yoki "
              "--record-path a/b/c bilan ko'rsating.",
        "en": "No records were found (the root element is <{root}>). Name the record element with --record-tag TAG "
              "or --record-path a/b/c.",
    },
    "importers.warn.yaml_nonfinite": {
        "uz": "{count} ta .inf / .nan qiymati null bo'ldi (JSON'da cheksizlik va NaN yo'q).",
        "en": "{count} .inf / .nan values became null (JSON has no infinity or NaN).",
    },
    "importers.warn.yaml_nonfinite.one": {
        "uz": "{count} ta .inf / .nan qiymati null bo'ldi (JSON'da cheksizlik va NaN yo'q).",
        "en": "{count} .inf / .nan value became null (JSON has no infinity or NaN).",
    },
    "importers.warn.yaml_big": {
        "uz": "YAML fayl katta ({size}): u butunlay xotiraga o'qiladi (PyYAML oqim bilan o'qiy olmaydi).",
        "en": "The YAML file is big ({size}): it is read into memory whole (PyYAML cannot stream).",
    },
    "importers.note.numbers": {"uz": "{count} ta katak son bo'ldi.", "en": "{count} cells became numbers."},
    "importers.note.numbers.one": {"uz": "{count} ta katak son bo'ldi.", "en": "{count} cell became a number."},
    "importers.note.literals": {
        "uz": "{count} ta katak true / false / null bo'ldi.",
        "en": "{count} cells became true / false / null.",
    },
    "importers.note.literals.one": {
        "uz": "{count} ta katak true / false / null bo'ldi.",
        "en": "{count} cell became true / false / null.",
    },
    "importers.note.empty_null": {"uz": "{count} ta bo'sh katak null bo'ldi.", "en": "{count} empty cells became null."},
    "importers.note.empty_null.one": {"uz": "{count} ta bo'sh katak null bo'ldi.", "en": "{count} empty cell became null."},
    "importers.note.empty_string": {
        "uz": "{count} ta bo'sh katak bo'sh matn (\"\") bo'ldi.",
        "en": "{count} empty cells became empty strings (\"\").",
    },
    "importers.note.empty_string.one": {
        "uz": "{count} ta bo'sh katak bo'sh matn (\"\") bo'ldi.",
        "en": "{count} empty cell became an empty string (\"\").",
    },
    "importers.note.yaml_values": {
        "uz": "{count} ta YAML son (0x1F, 017, 1_000, +1, 1:30, .5 ko'rinishidagi) qiymati bo'yicha yozildi.",
        "en": "{count} YAML numbers in a non-JSON form (0x1F, 017, 1_000, +1, 1:30, .5) were written by value.",
    },
    "importers.note.yaml_values.one": {
        "uz": "{count} ta YAML son (0x1F, 017, 1_000, +1, 1:30, .5 ko'rinishidagi) qiymati bo'yicha yozildi.",
        "en": "{count} YAML number in a non-JSON form (0x1F, 017, 1_000, +1, 1:30, .5) was written by value.",
    },
    "importers.note.dates": {
        "uz": "{count} ta YAML sana / vaqt ISO 8601 matn bo'ldi.",
        "en": "{count} YAML dates / times became ISO 8601 strings.",
    },
    "importers.note.dates.one": {
        "uz": "{count} ta YAML sana / vaqt ISO 8601 matn bo'ldi.",
        "en": "{count} YAML date / time became an ISO 8601 string.",
    },
    "importers.note.binary": {
        "uz": "{count} ta YAML binary qiymat base64 matn bo'ldi.",
        "en": "{count} YAML binary values became base64 strings.",
    },
    "importers.note.binary.one": {
        "uz": "{count} ta YAML binary qiymat base64 matn bo'ldi.",
        "en": "{count} YAML binary value became a base64 string.",
    },
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
        "uz": "buyruq ko'rsatilmagan: inspect, search, filter, sort, convert, patch, apply, flatten, stats, dupes, "
              "schema, validate, split, merge, extract, repair, diff yoki presets",
        "en": "no command given: inspect, search, filter, sort, convert, patch, apply, flatten, stats, dupes, "
              "schema, validate, split, merge, extract, repair, diff or presets",
    },
    "cli.argerr.split_size": {
        "uz": "qism hajmi kamida {minimum} bo'lishi kerak, berilgan: {value}",
        "en": "the part size must be at least {minimum}, got: {value}",
    },
    # help texts (argument help strings must not contain the percent sign)
    "cli.help.description": {
        "uz": "Katta JSON fayllar bilan ishlash vositasi: tuzilmani ko'rish, qidirish, filtrlash, tartiblash,\n"
              "boshqa formatlarga o'tkazish, statistika, dublikatlar, JSON Schema, bo'lish va birlashtirish,\n"
              "ta'mirlash va solishtirish. Fayl hech qachon to'liq xotiraga yuklanmaydi, asl fayl o'zgarmaydi.",
        "en": "A tool for huge JSON files: inspect the structure, search, filter, sort, convert to other formats,\n"
              "statistics, duplicates, JSON Schema, split and merge, repair and compare. The file is never loaded\n"
              "into memory as a whole and the original is never modified.",
    },
    "cli.help.epilog": {
        "uz": "Misollar:\n"
              "  jsonreshape inspect data.json\n"
              "  jsonreshape search data.json Toshkent --translit\n"
              "  jsonreshape filter data.json natija.json --where \"contact.address.city == 'Toshkent'\"\n"
              "  jsonreshape sort data.json natija.json --by name --by -id\n"
              "  jsonreshape convert data.json natija.csv --columns \"id,name,contact.address.city\"\n"
              "  jsonreshape stats data.json --field contact.address.city\n"
              "  jsonreshape split data.json --parts 10\n"
              "  jsonreshape merge a.json b.json -o hammasi.json --dedupe-by id\n"
              "  jsonreshape repair buzuq.json tuzatilgan.json\n"
              "  jsonreshape diff eski.json yangi.json --key id\n"
              "\n"
              "Ifodani (va vergulli ro'yxatni) qo'shtirnoq (\"...\") ichida, undagi matnni bitta tirnoq ('...')\n"
              "ichida yozing: misollar cmd.exe, PowerShell va bash'da bir xil ishlaydi.\n"
              "\n"
              "Chiqish kodlari: 0 - muvaffaqiyatli; 1 - noto'g'ri buyruq, parametr, ifoda yoki yo'l; 2 - kirish\n"
              "faylida JSON xatosi yoki fayl ish paytida o'zgardi; 3 - bekor qilindi (Ctrl+C); 4 - ichki xato\n"
              "(tafsilotlar log faylida); 5 - ma'lumot mos emas (validate xato topdi, diff --exit-code farq topdi).\n"
              "Har bir buyruq haqida batafsil: jsonreshape BUYRUQ --help",
        "en": "Examples:\n"
              "  jsonreshape inspect data.json\n"
              "  jsonreshape search data.json Toshkent --translit\n"
              "  jsonreshape filter data.json result.json --where \"contact.address.city == 'Toshkent'\"\n"
              "  jsonreshape sort data.json result.json --by name --by -id\n"
              "  jsonreshape convert data.json result.csv --columns \"id,name,contact.address.city\"\n"
              "  jsonreshape stats data.json --field contact.address.city\n"
              "  jsonreshape split data.json --parts 10\n"
              "  jsonreshape merge a.json b.json -o all.json --dedupe-by id\n"
              "  jsonreshape repair broken.json fixed.json\n"
              "  jsonreshape diff old.json new.json --key id\n"
              "\n"
              "Put an expression (and a comma list) in double quotes (\"...\") and the texts inside it in single\n"
              "quotes ('...'): the examples then work the same in cmd.exe, PowerShell and bash.\n"
              "\n"
              "Exit codes: 0 - success; 1 - invalid command, option, expression or path; 2 - JSON error in the\n"
              "input file, or the file changed while it was read; 3 - cancelled (Ctrl+C); 4 - internal error\n"
              "(details in the log file); 5 - the data does not match (validate found issues, diff --exit-code\n"
              "found differences).\n"
              "More about a command: jsonreshape COMMAND --help",
    },
    "cli.help.commands": {"uz": "buyruqlar", "en": "commands"},
    "cli.help.version": {
        "uz": "versiyalarni ko'rsatish (jsonreshape, yadro, Python) va chiqish",
        "en": "show the versions (jsonreshape, core, Python) and exit",
    },
    "cli.help.lang": {
        "uz": "til: uz (o'zbekcha, standart), en (inglizcha) yoki ru (ruscha)",
        "en": "language: uz (Uzbek, default), en (English) or ru (Russian)",
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
        "uz": "kirish JSON fayli (.json, .jsonl, .json.gz; UTF-8 yoki UTF-16) yoki - (standart kirish)",
        "en": "input JSON file (.json, .jsonl, .json.gz; UTF-8 or UTF-16), or - for standard input",
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
    "cli.help.parquet_compression": {
        "uz": "Parquet siqish usuli: zstd (standart), snappy, gzip yoki none",
        "en": "Parquet compression: zstd (default), snappy, gzip or none",
    },
    # import (CSV / YAML / XML -> JSON / JSON Lines; importers.py, docs/ARCHITECTURE.md §17)
    "cli.cmd.import.help": {
        "uz": "CSV, YAML yoki XML faylni JSON / JSON Lines'ga aylantirish",
        "en": "convert a CSV, YAML or XML file to JSON / JSON Lines",
    },
    "cli.cmd.import.description": {
        "uz": "CSV / TSV, YAML yoki XML faylni o'qib, yozuvlarini JSON Lines (standart) yoki bitta JSON massiv\n"
              "sifatida OUTPUT ga yozadi. Kirish turi kengaytmadan (.csv .tsv .yaml .yml .xml) yoki --from dan,\n"
              "natija formati OUTPUT kengaytmasidan (.json / .jsonl) yoki --to / --jsonl dan olinadi. Sonlar\n"
              "matni aynan saqlanadi (1.10 o'zgarmaydi, 007 matn qoladi). Asl fayl o'zgarmaydi; natija kirish\n"
              "fayli yoki saqlanmagan o'zgarishlari bor fayl bo'la olmaydi. CSV va XML oqim bilan o'qiladi; YAML\n"
              "butunlay xotiraga o'qiladi (256 MiB dan katta bo'lsa ogohlantiradi).",
        "en": "Reads a CSV / TSV, YAML or XML file and writes its records to OUTPUT as JSON Lines (default) or as\n"
              "one JSON array. The input type comes from the extension (.csv .tsv .yaml .yml .xml) or --from, the\n"
              "output format from the OUTPUT extension (.json / .jsonl) or --to / --jsonl. Number texts are kept\n"
              "exactly (1.10 never changes, 007 stays a string). The original file is never modified; the output\n"
              "may not be the input or a file with unsaved edits. CSV and XML are streamed; YAML is read into\n"
              "memory whole (a warning above 256 MiB).",
    },
    "cli.cmd.import.epilog": {
        "uz": "Misollar:\n"
              "  jsonreshape import data.csv data.jsonl\n"
              "  jsonreshape import data.csv data.json --indent 4\n"
              "  jsonreshape import jadval.tsv -o jadval.jsonl --no-header --types string\n"
              "  jsonreshape import eksport.csv -o eksport.jsonl --delimiter \";\" --encoding cp1251 --skip-bad\n"
              "  jsonreshape import config.yaml config.json\n"
              "  jsonreshape import katalog.xml -o katalog.jsonl --record-tag item\n"
              "  jsonreshape import katalog.xml -o katalog.jsonl --record-path catalog/books/book --json\n"
              "\n"
              "Qoidalar: CSV sarlavhasi ustun nomlari (a.b.c ichma-ich obyekt bo'ladi; --no-nest); bo'sh katak null\n"
              "(--empty string); XML'da atribut \"@nom\", matn \"#text\", takror elementlar massiv.\n"
              "Chiqish kodlari: 0 - yozildi; 1 - noto'g'ri parametr (OUTPUT kirish faylining o'zi, noma'lum tur,\n"
              "pyyaml o'rnatilmagan); 2 - kirish fayli buzuq (qator va sabab ko'rsatiladi); 3 - bekor qilindi.",
        "en": "Examples:\n"
              "  jsonreshape import data.csv data.jsonl\n"
              "  jsonreshape import data.csv data.json --indent 4\n"
              "  jsonreshape import table.tsv -o table.jsonl --no-header --types string\n"
              "  jsonreshape import export.csv -o export.jsonl --delimiter \";\" --encoding cp1251 --skip-bad\n"
              "  jsonreshape import config.yaml config.json\n"
              "  jsonreshape import catalog.xml -o catalog.jsonl --record-tag item\n"
              "  jsonreshape import catalog.xml -o catalog.jsonl --record-path catalog/books/book --json\n"
              "\n"
              "Rules: the CSV header names the columns (a.b.c becomes nested objects; --no-nest); an empty cell is\n"
              "null (--empty string); in XML attributes are \"@name\", text is \"#text\", repeated elements arrays.\n"
              "Exit codes: 0 - written; 1 - an invalid option (OUTPUT is the input itself, an unknown type, pyyaml\n"
              "not installed); 2 - the input is malformed (the line and the reason are shown); 3 - cancelled.",
    },
    "cli.group.import": {"uz": "import parametrlari", "en": "import options"},
    "cli.group.import_csv": {"uz": "CSV / TSV parametrlari", "en": "CSV / TSV options"},
    "cli.group.import_xml": {"uz": "XML parametrlari", "en": "XML options"},
    "cli.help.import_input": {
        "uz": "kirish fayli: .csv, .tsv, .yaml, .yml yoki .xml (boshqa kengaytma yoki - standart kirish uchun --from)",
        "en": "input file: .csv, .tsv, .yaml, .yml or .xml (--from for another extension or for - standard input)",
    },
    "cli.help.import_output": {
        "uz": "natija fayli: .jsonl (JSON Lines) yoki .json (bitta massiv); kirish fayli bo'lishi mumkin emas",
        "en": "output file: .jsonl (JSON Lines) or .json (one array); must not be the input",
    },
    "cli.help.import_from": {
        "uz": "kirish turi (standart: auto - kengaytmadan)",
        "en": "input type (default: auto - from the extension)",
    },
    "cli.help.import_to": {
        "uz": "natija formati (standart: OUTPUT kengaytmasidan; aks holda jsonl, YAML uchun json)",
        "en": "output format (default: from the OUTPUT extension; else jsonl, json for YAML)",
    },
    "cli.help.import_jsonl": {"uz": "JSON Lines yozish (--to jsonl bilan bir xil)", "en": "write JSON Lines (same as --to jsonl)"},
    "cli.help.import_types": {
        "uz": "qiymatlar: auto - JSON son ko'rinishidagi katak son, true/false/null - shunday qiymat, qolgani "
              "matn (standart); string - hammasi matn",
        "en": "values: auto - a cell that is a JSON number becomes a number, true/false/null those values, the "
              "rest strings (default); string - everything a string",
    },
    "cli.help.import_empty": {
        "uz": "bo'sh CSV katak: null (standart) yoki string (bo'sh matn)",
        "en": "an empty CSV cell: null (default) or string (an empty string)",
    },
    "cli.help.import_delimiter": {
        "uz": "CSV ajratuvchisi: bitta belgi yoki tab (standart: fayl boshidan aniqlanadi: , ; tab |)",
        "en": "CSV delimiter: one character or tab (default: detected from the start of the file: , ; tab |)",
    },
    "cli.help.import_no_header": {
        "uz": "birinchi qator sarlavha emas: ustunlar c1..cN deb nomlanadi",
        "en": "the first row is not a header: the columns are named c1..cN",
    },
    "cli.help.import_no_nest": {
        "uz": "a.b.c ko'rinishidagi ustun nomlarini ichma-ich obyekt qilmaslik (tekis kalit qoladi)",
        "en": "do not turn column names like a.b.c into nested objects (they stay flat keys)",
    },
    "cli.help.import_encoding": {
        "uz": "CSV kodirovkasi, masalan cp1251 (standart: aniqlanadi: UTF-8 BOM bilan/BOMsiz, UTF-16 BOM bilan)",
        "en": "CSV encoding, e.g. cp1251 (default: detected: UTF-8 with or without BOM, UTF-16 with a BOM)",
    },
    "cli.help.import_skip_bad": {
        "uz": "buzuq CSV qatorlarni (maydonlar soni boshqa, tirnoq xatosi) o'tkazib yuborish; aks holda xato",
        "en": "skip malformed CSV rows (another number of fields, a quoting error) instead of stopping",
    },
    "cli.help.import_record_tag": {
        "uz": "XML: shu tegli elementlar yozuv bo'ladi (standart: ildiz elementining bolalari)",
        "en": "XML: the elements with this tag become the records (default: the children of the root)",
    },
    "cli.help.import_record_path": {
        "uz": "XML: yozuv elementlarining yo'li ildizdan, masalan catalog/books/book",
        "en": "XML: the path of the record elements from the root, e.g. catalog/books/book",
    },
    "cli.help.import_json": {"uz": "hisobotni JSON sifatida chiqarish", "en": "print the report as JSON"},
    # standard input as INPUT ("-"): cli._stdin_inputs + jsonreshape.stdin_input
    "cli.stdin.stage": {"uz": "Standart kirish o'qilmoqda", "en": "Reading standard input"},
    "cli.stdin.summary": {
        "uz": "Standart kirishdan {size} o'qildi (vaqtinchalik fayl {path}, buyruq tugagach o'chiriladi)",
        "en": "{size} read from standard input (temporary file {path}, removed when the command ends)",
    },
    "cli.error.stdin_tty": {
        "uz": "Standart kirish terminal: - uchun quvur (pipe) orqali hech narsa berilmagan.",
        "en": "Standard input is the terminal: nothing is piped to -.",
    },
    "cli.error.stdin_tty.hint": {
        "uz": "Ma'lumotni quvur orqali bering (masalan curl ... | jsonreshape inspect -) yoki fayl nomini ko'rsating.",
        "en": "Pipe the data in (for example curl ... | jsonreshape inspect -) or give a file name.",
    },
    "cli.error.stdin_twice": {
        "uz": "- (standart kirish) faqat bitta INPUT o'rnida bo'la oladi.",
        "en": "Only one INPUT may be - (standard input).",
    },
    "cli.error.stdin_unsupported": {
        "uz": "- (standart kirish) faqat buyruqning INPUT argumenti bo'la oladi; boshqa fayllar nom bilan beriladi.",
        "en": "Only the INPUT of a command may be - (standard input); the other files are given by name.",
    },
    "cli.error.stdout_output": {
        "uz": "Natija - (standart chiqish) bo'la olmaydi: natijalar faylga yoziladi, fayl nomini ko'rsating.",
        "en": "The output cannot be - (standard output): results are written to files, give a file name.",
    },
    "cli.error.stdin_in_place": {
        "uz": "patch --in-place standart kirish (-) bilan ishlamaydi: OUTPUT faylini ko'rsating.",
        "en": "patch --in-place cannot work on standard input (-): give an OUTPUT file.",
    },
    "cli.error.stdin_split_dir": {
        "uz": "Standart kirishni (-) bo'lish uchun -o DIR (qismlar papkasi) kerak.",
        "en": "split from standard input (-) needs -o DIR (the folder of the parts).",
    },
    "cli.error.stdin_split_dir.hint": {
        "uz": "Qismlarni --name ANDOZA bilan nomlang: standart nom vaqtinchalik fayl nomidan olinadi.",
        "en": "Name the parts with --name PATTERN: the default name comes from the temporary file.",
    },
    "cli.error.stdin_import_format": {
        "uz": "Standart kirishdan (-) import uchun --from csv|yaml|xml kerak: turini fayl nomidan aniqlab bo'lmaydi.",
        "en": "import from standard input (-) needs --from csv|yaml|xml: the type cannot be told from a file name.",
    },
    "cli.stage.importing": {"uz": "Import qilinmoqda", "en": "Importing"},
    "cli.summary.import": {
        "uz": "{records} ta yozuv: {input} ({fmt}) -> {output} ({size}, {seconds})",
        "en": "{records} records: {input} ({fmt}) -> {output} ({size}, {seconds})",
    },
    "cli.summary.import.one": {
        "uz": "{records} ta yozuv: {input} ({fmt}) -> {output} ({size}, {seconds})",
        "en": "{records} record: {input} ({fmt}) -> {output} ({size}, {seconds})",
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
    # the patch command (JSON Patch, RFC 6902)
    "cli.cmd.patch.help": {"uz": "JSON Patch (RFC 6902) qo'llash", "en": "apply a JSON Patch (RFC 6902)"},
    "cli.cmd.patch.description": {
        "uz": "PATCH faylidagi amallarni (RFC 6902: add, remove, replace, move, copy, test) INPUT ga qo'llaydi va\n"
              "natijani INPUT bilan bir xil formatda va kodlashda OUTPUT ga yozadi; --in-place INPUT ning o'zini\n"
              "almashtiradi (avval INPUT.bak zaxira nusxasi qoladi). Biror amal bajarilmasa (masalan, test mos\n"
              "kelmasa), hech narsa yozilmaydi.",
        "en": "Applies the operations of the PATCH file (RFC 6902: add, remove, replace, move, copy, test) to INPUT\n"
              "and writes the result to OUTPUT in the same format and encoding as INPUT; --in-place replaces INPUT\n"
              "itself (INPUT.bak is kept first). When an operation fails (for example a test that does not match),\n"
              "nothing is written.",
    },
    "cli.cmd.patch.epilog": {
        "uz": "Misollar:\n"
              "  jsonreshape patch data.json tuzatish.json natija.json\n"
              "  jsonreshape patch data.json tuzatish.json --in-place\n"
              "  jsonreshape patch data.jsonl tuzatish.json --in-place --no-backup\n"
              "\n"
              "Yo'llar (JSON Pointer): massiv va JSON Lines fayllarda /12/name - 12-yozuvning name maydoni (yozuvlar\n"
              "0 dan sanaladi), /- - oxiriga qo'shish; kalitli obyektda /kalit/name; o'ralgan faylda to'liq yo'l,\n"
              "masalan /data/12/name; bitta qiymatli faylda ildizdan boshlab.",
        "en": "Examples:\n"
              "  jsonreshape patch data.json fix.json result.json\n"
              "  jsonreshape patch data.json fix.json --in-place\n"
              "  jsonreshape patch data.jsonl fix.json --in-place --no-backup\n"
              "\n"
              "Paths (JSON Pointer): in array and JSON Lines files /12/name is the name field of record 12 (records\n"
              "are counted from 0) and /- appends; in a keyed object /key/name; in a wrapped file the full path,\n"
              "for example /data/12/name; in a single-value file from the root.",
    },
    "cli.help.patch": {
        "uz": "JSON Patch fayli (RFC 6902; UTF-8, UTF-16 yoki .gz)",
        "en": "JSON Patch file (RFC 6902; UTF-8, UTF-16 or .gz)",
    },
    "cli.help.patch_output": {
        "uz": "natija fayli (INPUT bilan bir xil format va kodlash; .gz bilan tugasa siqiladi); --in-place bilan "
              "berilmaydi",
        "en": "output file (same format and encoding as INPUT; compressed when it ends in .gz); not given with "
              "--in-place",
    },
    "cli.help.in_place": {
        "uz": "INPUT faylining o'zini almashtirish (atomik; avval INPUT.bak zaxira nusxasi qoladi)",
        "en": "replace INPUT itself (atomically; INPUT.bak is kept first)",
    },
    "cli.help.no_backup": {
        "uz": "--in-place bilan .bak zaxira nusxasini qoldirmaslik",
        "en": "with --in-place: do not keep the .bak backup",
    },
    "cli.group.patch": {"uz": "patch parametrlari", "en": "patch options"},
    "cli.stage.patching": {"uz": "Patch qo'llanmoqda", "en": "Applying the patch"},
    "cli.error.patch_output": {
        "uz": "Natija faylini (OUTPUT) bering yoki --in-place parametrini ishlating (ikkalasini birga emas).",
        "en": "Give an OUTPUT file or use --in-place (not both).",
    },
    "cli.error.no_backup": {
        "uz": "--no-backup faqat --in-place bilan ishlatiladi.",
        "en": "--no-backup is only used together with --in-place.",
    },
    "cli.hint.patch_broken_lines": {
        "uz": "Buzuq qatorlarsiz saqlash uchun --allow-partial parametrini qo'shing.",
        "en": "Add --allow-partial to save without the broken lines.",
    },
    "cli.summary.patch": {
        "uz": "{ops} ta amal qo'llandi -> {output} ({size}, {seconds})",
        "en": "{ops} operations applied -> {output} ({size}, {seconds})",
    },
    "cli.summary.patch.one": {
        "uz": "{ops} ta amal qo'llandi -> {output} ({size}, {seconds})",
        "en": "{ops} operation applied -> {output} ({size}, {seconds})",
    },
    "cli.summary.patch_unchanged": {
        "uz": "Patch hech narsani o'zgartirmadi ({ops} ta amal): {output} o'zgartirilmadi.",
        "en": "The patch changed nothing ({ops} operations): {output} was not modified.",
    },
    "cli.summary.patch_unchanged.one": {
        "uz": "Patch hech narsani o'zgartirmadi ({ops} ta amal): {output} o'zgartirilmadi.",
        "en": "The patch changed nothing ({ops} operation): {output} was not modified.",
    },
    "cli.summary.backup": {"uz": "Zaxira nusxa: {path}", "en": "Backup: {path}"},
    # the apply and flatten commands (structure builder / mapping, docs/ARCHITECTURE.md §13.4)
    "cli.cmd.apply.help": {
        "uz": "tuzilma (mapping) faylini butun faylga qo'llash",
        "en": "apply a mapping (structure) file to a whole file",
    },
    "cli.cmd.apply.description": {
        "uz": "Tuzilma quruvchida saqlangan MAPPING faylidagi tuzilmani INPUT ning barcha yozuvlariga qo'llab,\n"
              "natijani OUTPUT ga JSON yoki JSON Lines sifatida yozadi. Fayl oqim bilan o'qiladi (xotiraga to'liq\n"
              "yuklanmaydi), sonlar aniq saqlanadi, asl fayl o'zgarmaydi; natija avval vaqtinchalik faylga yoziladi.",
        "en": "Applies the structure stored in the MAPPING file (saved by the structure builder) to every record\n"
              "of INPUT and writes the result to OUTPUT as JSON or JSON Lines. The file is streamed (never loaded\n"
              "into memory as a whole), numbers stay exact and the original is never modified; the result is\n"
              "written to a temporary file first.",
    },
    "cli.cmd.apply.epilog": {
        "uz": "Misollar:\n"
              "  jsonreshape apply tuzilma.json data.json natija.json\n"
              "  jsonreshape apply tuzilma.json data.json natija.jsonl\n"
              "  jsonreshape apply tuzilma.json data.json natija.json --compact --record-path data\n"
              "  jsonreshape apply tuzilma.json katta.json natija.json --skip-huge --workers 4\n"
              "\n"
              "Obyekt ichidagi massiv fayllarda mapping faylida saqlangan yozuvlar yo'li ishlatiladi; --record-path\n"
              "uni almashtiradi. Tuzilmada yo'q maydonlar natijaga yozilmaydi: shunday maydonli yozuvlar soni\n"
              "ogohlantirishda aytiladi (tuzilmani to'liq skanerlash bilan qayta yarating).",
        "en": "Examples:\n"
              "  jsonreshape apply mapping.json data.json result.json\n"
              "  jsonreshape apply mapping.json data.json result.jsonl\n"
              "  jsonreshape apply mapping.json data.json result.json --compact --record-path data\n"
              "  jsonreshape apply mapping.json big.json result.json --skip-huge --workers 4\n"
              "\n"
              "For an array inside an object, the record path stored in the mapping file is used; --record-path\n"
              "overrides it. Fields the mapping does not know are not written: a warning tells how many records\n"
              "had such fields (build the mapping again with a full scan).",
    },
    "cli.cmd.flatten.help": {
        "uz": "ichma-ich ro'yxatni (masalan, children) bitta ro'yxatga tekislash",
        "en": "flatten a nested list (such as children) into one list",
    },
    "cli.cmd.flatten.description": {
        "uz": "Har bir yozuvdagi ichma-ich KEY massivini (children ichida children ...) barcha darajalari bilan\n"
              "bitta ro'yxatga tekislaydi: elementlar yuqoridan pastga tartibda, har biri daraja raqami (_depth: 1,\n"
              "2, 3 ...) bilan va ichidagi KEY maydonisiz chiqadi. Tuzilma namunadan (--sample N) yoki barcha\n"
              "yozuvlardan (--full-scan) aniqlanadi, keyin butun faylga oqim bilan qo'llanadi. --mode records\n"
              "yozuvlar ro'yxatining o'zini (yozuvlar va ularning barcha avlodlarini) tekislaydi.",
        "en": "Flattens the nested KEY array of every record (children inside children ...), all levels, into one\n"
              "list: the items come in top-down order, each with its level number (_depth: 1, 2, 3 ...) and\n"
              "without the inner KEY field. The structure is inferred from a sample (--sample N) or from every\n"
              "record (--full-scan) and then applied to the whole file, streamed. --mode records flattens the list\n"
              "of records itself (the records and all their descendants).",
    },
    "cli.cmd.flatten.epilog": {
        "uz": "Misollar:\n"
              "  jsonreshape flatten data.json tekis.json --children-key children\n"
              "  jsonreshape flatten data.json tekis.json --children-key children --depth-field daraja\n"
              "  jsonreshape flatten data.json tekis.jsonl --children-key children --mode records\n"
              "  jsonreshape flatten data.json tekis.json --children-key items --full-scan\n"
              "\n"
              "Masalan, child1 ichida child1.2, uning ichida child1.2.1 va child1.2.3, keyin child2 bo'lsa,\n"
              "natijadagi children ro'yxati: child1, child1.2, child1.2.1, child1.2.3, child2; _depth = 1, 2, 3, 3, 1.",
        "en": "Examples:\n"
              "  jsonreshape flatten data.json flat.json --children-key children\n"
              "  jsonreshape flatten data.json flat.json --children-key children --depth-field level\n"
              "  jsonreshape flatten data.json flat.jsonl --children-key children --mode records\n"
              "  jsonreshape flatten data.json flat.json --children-key items --full-scan\n"
              "\n"
              "For example, with child1.2 inside child1, child1.2.1 and child1.2.3 inside child1.2, then child2,\n"
              "the children list of the result is: child1, child1.2, child1.2.1, child1.2.3, child2; _depth = 1, 2,\n"
              "3, 3, 1.",
    },
    "cli.help.mapping": {
        "uz": "tuzilma quruvchida saqlangan mapping fayli (.json)",
        "en": "mapping file saved by the structure builder (.json)",
    },
    "cli.help.mapping_output": {
        "uz": "natija fayli: JSON (.json) yoki JSON Lines (.jsonl, .ndjson yoki --jsonl)",
        "en": "output file: JSON (.json) or JSON Lines (.jsonl, .ndjson or --jsonl)",
    },
    "cli.help.mapping_jsonl": {
        "uz": "JSON Lines formatida yozish: har qatorda natijaning bitta elementi (.jsonl nomli OUTPUT uchun "
              "avtomatik)",
        "en": "write JSON Lines: one item of the result per line (automatic for an OUTPUT named .jsonl)",
    },
    "cli.help.skip_huge": {
        "uz": "«Katta tugun chegarasi» sozlamasidan katta yozuvlarni o'tkazib yuborish (soni ogohlantirishda "
              "aytiladi); standart: xato bilan to'xtash",
        "en": "skip the records larger than the \"Large node threshold\" setting (counted in a warning); "
              "default: stop with an error",
    },
    "cli.help.workers": {
        "uz": "parallel ishlaydigan jarayonlar soni (standart: 64 MB dan katta fayllarda avtomatik; 1 - faqat shu "
              "jarayon)",
        "en": "number of worker processes (default: automatic for files over 64 MB; 1 = this process only)",
    },
    "cli.help.children_key": {
        "uz": "tekislanadigan ichma-ich massiv kaliti (masalan: children)",
        "en": "key of the nested array to flatten (e.g. children)",
    },
    "cli.help.depth_field": {
        "uz": "daraja raqami yoziladigan maydon nomi (standart: _depth; ma'lumotda band bo'lsa NAME_2)",
        "en": "name of the level-number field (default: _depth; NAME_2 when the data already has it)",
    },
    "cli.help.flatten_mode": {
        "uz": "nested (standart) - har bir yozuvning KEY maydonini tekislash; records - yozuvlar ro'yxatining "
              "o'zini tekislash (yozuvlar va ularning barcha avlodlari bitta ro'yxatda)",
        "en": "nested (default) - flatten the KEY field of every record; records - flatten the list of records "
              "itself (the records and all their descendants in one list)",
    },
    "cli.help.mapping_sample": {
        "uz": "tuzilma aniqlanadigan yozuvlar soni: fayl bo'ylab teng taqsimlangan namuna (standart: «Namuna "
              "hajmi» sozlamasi, odatda 10000)",
        "en": "number of records the structure is inferred from: a sample spread evenly over the file "
              "(default: the \"Sample size\" setting, usually 10000)",
    },
    "cli.help.full_scan": {
        "uz": "tuzilmani barcha yozuvlardan aniqlash (sekinroq, lekin namunada uchramaydigan maydonlar ham "
              "saqlanadi)",
        "en": "infer the structure from every record (slower, but fields a sample would miss are kept)",
    },
    "cli.group.flatten": {"uz": "tekislash parametrlari", "en": "flatten options"},
    "cli.group.apply": {"uz": "qo'llash parametrlari", "en": "apply options"},
    "cli.stage.applying": {"uz": "Tuzilma qo'llanmoqda", "en": "Applying the mapping"},
    "cli.stage.inferring": {"uz": "Tuzilma aniqlanmoqda", "en": "Inferring the structure"},
    "cli.stage.flattening": {"uz": "Ichki darajalar o'qilmoqda", "en": "Reading the nested levels"},
    "cli.error.output_is_mapping": {
        "uz": "Natija fayli mapping faylining o'zi: {path}",
        "en": "The output file is the mapping file itself: {path}",
    },
    "cli.error.output_is_mapping.hint": {
        "uz": "Natija uchun boshqa fayl nomini bering.",
        "en": "Give the result another file name.",
    },
    "cli.error.mapping_gzip": {
        "uz": "Tuzilma natijasi siqilmagan JSON bo'lib yoziladi, nom esa .gz bilan tugaydi: {path}",
        "en": "The result of a mapping is written as uncompressed JSON, but the name ends in .gz: {path}",
    },
    "cli.error.mapping_gzip.hint": {
        "uz": "Natija nomini .json yoki .jsonl bilan tugating.",
        "en": "End the output name with .json or .jsonl.",
    },
    "cli.error.mapping_output_format": {
        "uz": "Tuzilma natijasi faqat JSON yoki JSON Lines bo'ladi, nom esa {fmt} formatini bildiradi: {path}",
        "en": "The result of a mapping is JSON or JSON Lines only, but the name means the {fmt} format: {path}",
    },
    "cli.error.mapping_output_format.hint": {
        "uz": "Natijani .json yoki .jsonl faylga yozing, keyin kerak bo'lsa uni jsonreshape convert bilan CSV, "
              "XLSX yoki SQLite ga o'tkazing.",
        "en": "Write the result to a .json or .jsonl file, then convert it to CSV, XLSX or SQLite with "
              "jsonreshape convert if needed.",
    },
    "cli.error.mapping_record_path": {
        "uz": "Mapping faylida saqlangan yozuvlar yo'li bu faylda topilmadi yoki massiv emas: {path}",
        "en": "The record path stored in the mapping file is not in this file or is not an array: {path}",
    },
    "cli.error.depth_field_empty": {
        "uz": "--depth-field qiymati bo'sh bo'lmasligi kerak.",
        "en": "--depth-field must not be empty.",
    },
    "cli.error.children_key_missing": {
        "uz": "Yozuvlarda «{key}» maydoni topilmadi.",
        "en": "The records have no «{key}» field.",
    },
    "cli.error.not_recursive": {
        "uz": "«{key}» bu yerda obyektlarning rekursiv massivi emas: {reason}.",
        "en": "«{key}» is not a recursive array of objects here: {reason}.",
    },
    "cli.error.flatten_records_mode": {
        "uz": "--mode records bu faylda ishlamaydi: yozuvlar massivda emas ({format}).",
        "en": "--mode records does not work for this file: its records are not in an array ({format}).",
    },
    "cli.error.flatten_records_mode.hint": {
        "uz": "Har bir yozuv ichidagi massivni tekislash uchun --mode nested dan foydalaning.",
        "en": "Use --mode nested to flatten the array inside every record.",
    },
    "cli.reason.not_array": {
        "uz": "bu maydon massiv emas (turi: {types})",
        "en": "the field is not an array (type: {types})",
    },
    "cli.reason.items_not_objects": {
        "uz": "uning elementlari obyekt emas (turi: {types})",
        "en": "its items are not objects (type: {types})",
    },
    "cli.reason.always_empty": {"uz": "bu massiv doim bo'sh", "en": "the array is always empty"},
    "cli.reason.no_nested": {
        "uz": "uning elementlari ichida yana «{key}» massivi (obyektlar bilan) yo'q",
        "en": "its items hold no nested «{key}» array of objects",
    },
    "cli.hint.record_arrays": {
        "uz": "Yozuvlardagi massiv maydonlar: {fields}.",
        "en": "Array fields of the records: {fields}.",
    },
    "cli.hint.record_no_arrays": {"uz": "Yozuvlarda massiv maydon yo'q.", "en": "The records have no array fields."},
    "cli.hint.records_not_objects": {"uz": "Yozuvlar obyekt emas.", "en": "The records are not objects."},
    "cli.hint.flatten_sample": {
        "uz": "Tuzilma {total} ta yozuvdan {count} tasi (namuna) bo'yicha aniqlandi: kerakli darajalar namunaga "
              "tushmagan bo'lsa, --full-scan bilan qayta urinib ko'ring.",
        "en": "The structure was inferred from a sample of {count} of {total} records: if the levels you need "
              "were not in the sample, try again with --full-scan.",
    },
    "cli.hint.flatten_sample.one": {
        "uz": "Tuzilma {total} ta yozuvdan {count} tasi (namuna) bo'yicha aniqlandi: kerakli darajalar namunaga "
              "tushmagan bo'lsa, --full-scan bilan qayta urinib ko'ring.",
        "en": "The structure was inferred from a sample of {count} of {total} record: if the levels you need "
              "were not in the sample, try again with --full-scan.",
    },
    "cli.hint.full_scan": {
        "uz": "Tuzilmani barcha yozuvlardan aniqlash uchun --full-scan ni qo'shing.",
        "en": "Add --full-scan to infer the structure from every record.",
    },
    "cli.hint.mapping_kind": {
        "uz": "Mapping «{label}» turidagi fayldan tuzilgan: kirish faylini ham shunday o'qing: {option}. Yoki "
              "tuzilmani shu fayldan qayta yarating.",
        "en": "The mapping was made from a file of the kind «{label}»: read the input the same way with "
              "{option}, or build the mapping again from this file.",
    },
    "cli.warn.depth_field_taken": {
        "uz": "«{name}» nomi ma'lumotda band: daraja raqami «{new}» maydoniga yoziladi.",
        "en": "The name «{name}» is taken in the data: the level number is written to «{new}».",
    },
    "cli.warn.flatten_no_records": {
        "uz": "Faylda yozuv yo'q: tekislanadigan narsa yo'q.",
        "en": "The file has no records: there is nothing to flatten.",
    },
    "cli.flatten.done_full": {
        "uz": "«{key}» rekursiv tekislandi (daraja raqami: «{field}»); tuzilma barcha yozuvlardan ({count} ta) "
              "aniqlandi.",
        "en": "«{key}» was flattened recursively (level number: «{field}»); the structure was inferred from every "
              "record ({count}).",
    },
    "cli.flatten.done_sample": {
        "uz": "«{key}» rekursiv tekislandi (daraja raqami: «{field}»); tuzilma {total} ta yozuvdan {count} tasi "
              "(namuna) bo'yicha aniqlandi.",
        "en": "«{key}» was flattened recursively (level number: «{field}»); the structure was inferred from a "
              "sample of {count} of {total} records.",
    },
    "cli.flatten.done_sample.one": {
        "uz": "«{key}» rekursiv tekislandi (daraja raqami: «{field}»); tuzilma {total} ta yozuvdan {count} tasi "
              "(namuna) bo'yicha aniqlandi.",
        "en": "«{key}» was flattened recursively (level number: «{field}»); the structure was inferred from a "
              "sample of {count} of {total} record.",
    },
    "cli.summary.apply": {
        "uz": "{records} -> {items} -> {output} ({size}, {seconds}, {rate} MB/s; {passes}, {workers})",
        "en": "{records} -> {items} -> {output} ({size}, {seconds}, {rate} MB/s; {passes}, {workers})",
    },
    "cli.count.items": {"uz": "{count} ta element", "en": "{count} items"},
    "cli.count.items.one": {"uz": "{count} ta element", "en": "{count} item"},
    "cli.count.passes": {"uz": "{count} ta o'tish", "en": "{count} passes"},
    "cli.count.passes.one": {"uz": "{count} ta o'tish", "en": "{count} pass"},
    "cli.count.workers": {"uz": "{count} ta jarayon", "en": "{count} processes"},
    "cli.count.workers.one": {"uz": "{count} ta jarayon", "en": "{count} process"},
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
        "uz": "--to json, jsonl, csv, xlsx, sqlite yoki parquet qo'shing yoki .json, .jsonl, .csv, .xlsx, .sqlite, "
              ".parquet kengaytmasini ishlating.",
        "en": "Add --to json, jsonl, csv, xlsx, sqlite or parquet, or use a .json, .jsonl, .csv, .xlsx, .sqlite or "
              ".parquet name.",
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
    # stage 5: the tool commands (docs/ARCHITECTURE.md §14.8)
    "cli.group.select": {"uz": "yozuvlar tanlovi", "en": "record selection"},
    "cli.help.select": {
        "uz": "faqat shu yozuvlar (raqamlar 0 dan): 5, 10:20 (10 dan 19 gacha), 100: (oxirigacha), :50 (birinchi 50 "
              "ta), vergul bilan ro'yxat (\"1,5,10:20\")",
        "en": "only these records (numbered from 0): 5, 10:20 (10 to 19), 100: (to the end), :50 (the first 50), a "
              "comma list (\"1,5,10:20\")",
    },
    "cli.group.node": {"uz": "bitta tugun", "en": "one node"},
    "cli.help.node": {
        "uz": "faqat shu tugunni yozish (JSON yoki JSON Lines): #5.orders, /data/5/orders, data[5].orders",
        "en": "write this node only (JSON or JSON Lines): #5.orders, /data/5/orders, data[5].orders",
    },
    "cli.help.output_opt": {"uz": "natija fayli (OUTPUT o'rniga)", "en": "output file (instead of OUTPUT)"},
    # stats
    "cli.cmd.stats.help": {
        "uz": "maydon statistikasi: mavjudlik, turlar, noyob va eng ko'p uchragan qiymatlar",
        "en": "field statistics: presence, types, distinct and most frequent values",
    },
    "cli.cmd.stats.description": {
        "uz": "Har bir maydon uchun bitta o'qishda hisoblaydi: mavjud / null / yo'q soni, turlar taqsimoti, noyob\n"
              "qiymatlar soni (katta faylda taxminiy, xatosi bilan; --exact - aniq), eng ko'p uchragan qiymatlar va\n"
              "ularning soni, sonlar uchun min / max / o'rtacha / yig'indi (aniq), satrlar uzunligi. --field\n"
              "berilmasa, tuzilmadagi asosiy maydonlar olinadi.",
        "en": "Computes in one pass for every field: the present / null / missing counts, the type distribution, the\n"
              "number of distinct values (approximate with its error on big files; --exact counts exactly), the most\n"
              "frequent values with their counts, min / max / mean / sum of the numbers (exact) and the string\n"
              "lengths. Without --field the main fields of the structure are used.",
    },
    "cli.cmd.stats.epilog": {
        "uz": "Misollar:\n"
              "  jsonreshape stats data.json --field contact.address.city\n"
              "  jsonreshape stats data.json --field name --field score --top 10\n"
              "  jsonreshape stats data.json --field \"orders[*].sum\" --exact\n"
              "  jsonreshape stats data.json --where \"active == true\" --json\n"
              "  jsonreshape stats data.json --field id --select 0:1000\n"
              "\n"
              "Maydon yo'llari: name, contact.address.city, orders[*].sum (massiv ichida), ..name (har qanday\n"
              "chuqurlikda), $key (kalitli faylda a'zo kaliti; PowerShell va bash'da uni bitta tirnoqqa oling).",
        "en": "Examples:\n"
              "  jsonreshape stats data.json --field contact.address.city\n"
              "  jsonreshape stats data.json --field name --field score --top 10\n"
              "  jsonreshape stats data.json --field \"orders[*].sum\" --exact\n"
              "  jsonreshape stats data.json --where \"active == true\" --json\n"
              "  jsonreshape stats data.json --field id --select 0:1000\n"
              "\n"
              "Field paths: name, contact.address.city, orders[*].sum (inside an array), ..name (at any depth),\n"
              "$key (the member key of a keyed file; put it in single quotes in PowerShell and bash).",
    },
    "cli.group.stats": {"uz": "statistika parametrlari", "en": "statistics options"},
    "cli.help.field": {
        "uz": "maydon yo'li (bir necha marta berish mumkin): name, contact.address.city, orders[*].sum, ..name, $key; "
              "berilmasa - asosiy maydonlar",
        "en": "field path (may be repeated): name, contact.address.city, orders[*].sum, ..name, $key; default: the "
              "main fields",
    },
    "cli.help.stats_top": {
        "uz": "eng ko'p uchragan nechta qiymat ko'rsatilsin (standart 20; 0 - ko'rsatilmaydi)",
        "en": "how many of the most frequent values to show (default 20; 0 = none)",
    },
    "cli.help.exact": {
        "uz": "katta fayllarda ham noyob qiymatlar va sonlarni aniq hisoblash (sekinroq, vaqtinchalik fayllar bilan)",
        "en": "count the distinct values and the top counts exactly on big files too (slower, uses temporary files)",
    },
    "cli.help.stats_sample": {
        "uz": "faqat fayl bo'ylab teng taqsimlangan N ta yozuvni ko'rib chiqish",
        "en": "look at N records spread evenly over the file only",
    },
    "cli.help.stats_memory": {
        "uz": "aniq sanash uchun xotira, masalan 256MB (standart: «Xotira chegarasi»ning 1/8 qismi); undan oshsa, "
              "taxminiy hisobga o'tiladi (--exact bilan diskdan foydalaniladi)",
        "en": "memory for exact counting, e.g. 256MB (default: 1/8 of the \"Memory limit\"); beyond it the counts "
              "become approximate (with --exact the disk is used)",
    },
    "cli.help.stats_json": {
        "uz": "natijani JSON sifatida chiqarish (aniq sonlar bilan)",
        "en": "print the result as JSON (exact numbers)",
    },
    "cli.stage.stats": {"uz": "Statistika hisoblanmoqda", "en": "Computing statistics"},
    "cli.stats.default_fields": {
        "uz": "--field berilmadi: tuzilmadagi {count} ta asosiy maydon olindi.",
        "en": "No --field given: the {count} main fields of the structure are used.",
    },
    "cli.stats.default_fields.one": {
        "uz": "--field berilmadi: tuzilmadagi {count} ta asosiy maydon olindi.",
        "en": "No --field given: the {count} main field of the structure is used.",
    },
    "cli.summary.stats": {
        "uz": "Statistika: {fields}, {records} ({seconds})",
        "en": "Statistics: {fields}, {records} ({seconds})",
    },
    "cli.count.fields": {"uz": "{count} ta maydon", "en": "{count} fields"},
    "cli.count.fields.one": {"uz": "{count} ta maydon", "en": "{count} field"},
    # dupes
    "cli.cmd.dupes.help": {
        "uz": "kalit bo'yicha takroriy yozuvlar: soni va ro'yxati",
        "en": "duplicate records by a key: how many and which",
    },
    "cli.cmd.dupes.description": {
        "uz": "KEY qiymati bir nechta yozuvda uchraydigan yozuvlarni topadi: guruhlar va takroriy yozuvlar soni,\n"
              "dastlabki guruhlar (yozuv raqamlari 0 dan) va --top bilan eng katta guruhlar. Sonlar qiymati bo'yicha\n"
              "solishtiriladi (1 = 1.0), satrlar aynan. -o FAYL takroriy deb belgilangan yozuvlarni faylga yozadi.",
        "en": "Finds the records whose KEY value occurs in more than one record: the number of groups and of\n"
              "duplicates, the first groups (record numbers from 0) and, with --top, the largest groups. Numbers\n"
              "compare by value (1 = 1.0), strings exactly. -o FILE writes the records marked as duplicates to a file.",
    },
    "cli.cmd.dupes.epilog": {
        "uz": "Misollar:\n"
              "  jsonreshape dupes data.json --key id\n"
              "  jsonreshape dupes data.json --key contact.phone --top 10\n"
              "  jsonreshape dupes data.json --key id --keep none -o takroriylar.json\n"
              "  jsonreshape dupes data.json --key id --keep last -o ortiqcha.jsonl --json\n"
              "\n"
              "--keep: first - har guruhning birinchisi takroriy hisoblanmaydi (standart), last - oxirgisi, none -\n"
              "guruhning barcha yozuvlari takroriy. Butun yozuv bo'yicha: --key $; kalitli faylda a'zo kaliti\n"
              "bo'yicha: --key $key (PowerShell va bash'da bitta tirnoqda).",
        "en": "Examples:\n"
              "  jsonreshape dupes data.json --key id\n"
              "  jsonreshape dupes data.json --key contact.phone --top 10\n"
              "  jsonreshape dupes data.json --key id --keep none -o duplicates.json\n"
              "  jsonreshape dupes data.json --key id --keep last -o extra.jsonl --json\n"
              "\n"
              "--keep: first - the first record of each group is not a duplicate (default), last - the last one,\n"
              "none - every record of a group is a duplicate. By the whole record: --key $; by the member key of a\n"
              "keyed file: --key $key (in single quotes in PowerShell and bash).",
    },
    "cli.group.dupes": {"uz": "dublikat parametrlari", "en": "duplicate options"},
    "cli.help.dupes_key": {
        "uz": "kalit: yozuv ichidagi yo'l (id, contact.phone, tags[0]), $ - butun yozuv, $key - kalitli fayldagi a'zo "
              "kaliti",
        "en": "key: a path inside the record (id, contact.phone, tags[0]), $ = the whole record, $key = the member key "
              "of a keyed file",
    },
    "cli.help.dupes_keep": {
        "uz": "takroriy hisoblanmaydigan yozuv: first - har guruhning birinchisi (standart), last - oxirgisi, none - "
              "hech biri (guruhning barcha yozuvlari takroriy)",
        "en": "the record that is not a duplicate: first - the first of each group (default), last - the last, none - "
              "none (every record of a group is a duplicate)",
    },
    "cli.help.dupes_examples": {
        "uz": "ko'rsatiladigan dastlabki guruhlar soni (standart 20)",
        "en": "number of first groups shown (default 20)",
    },
    "cli.help.dupes_top": {
        "uz": "eng katta N ta guruhni ham ko'rsatish (standart 0 - ko'rsatilmaydi)",
        "en": "also show the N largest groups (default 0 = none)",
    },
    "cli.help.dupes_out": {
        "uz": "takroriy deb belgilangan yozuvlarni shu faylga yozish (format kengaytmadan yoki --to bilan)",
        "en": "write the records marked as duplicates to this file (format from the extension or --to)",
    },
    "cli.help.dupes_memory": {
        "uz": "kalitlar uchun xotira, masalan 256MB (standart: «Xotira chegarasi»ning 1/4 qismi; oshsa, diskdan "
              "foydalaniladi)",
        "en": "memory for the keys, e.g. 256MB (default: 1/4 of the \"Memory limit\"; beyond it the disk is used)",
    },
    "cli.help.dupes_json": {"uz": "natijani JSON sifatida chiqarish", "en": "print the result as JSON"},
    "cli.stage.dupes": {"uz": "Dublikatlar qidirilmoqda", "en": "Looking for duplicates"},
    "cli.dupes.key": {"uz": "Kalit: {key}", "en": "Key: {key}"},
    "cli.dupes.counts": {
        "uz": "Guruhlar: {groups}; takroriy yozuvlar: {duplicates} ({keep})",
        "en": "Groups: {groups}; duplicate records: {duplicates} ({keep})",
    },
    "cli.dupes.keep.first": {"uz": "har guruhning birinchisidan tashqari", "en": "all but the first of each group"},
    "cli.dupes.keep.last": {"uz": "har guruhning oxirgisidan tashqari", "en": "all but the last of each group"},
    "cli.dupes.keep.none": {"uz": "guruhlarning barcha yozuvlari", "en": "every record of each group"},
    "cli.dupes.records": {
        "uz": "Kaliti bor: {with_key}; kalitsiz: {missing}; o'qib bo'lmagan: {skipped}; noyob qiymatlar: {distinct}",
        "en": "With the key: {with_key}; without it: {missing}; unreadable: {skipped}; distinct values: {distinct}",
    },
    "cli.dupes.none": {"uz": "Takroriy qiymat yo'q.", "en": "No value occurs more than once."},
    "cli.dupes.examples": {
        "uz": "Misollar (dastlabki {count} ta guruh):",
        "en": "Examples (the first {count} groups):",
    },
    "cli.dupes.examples.one": {
        "uz": "Misollar (dastlabki {count} ta guruh):",
        "en": "Examples (the first {count} group):",
    },
    "cli.dupes.top": {"uz": "Eng katta {count} ta guruh:", "en": "The {count} largest groups:"},
    "cli.dupes.top.one": {"uz": "Eng katta {count} ta guruh:", "en": "The {count} largest group:"},
    "cli.dupes.group": {"uz": "{key}  ({records}): {positions}", "en": "{key}  ({records}): {positions}"},
    "cli.summary.dupes": {"uz": "{records} tekshirildi ({seconds})", "en": "{records} checked ({seconds})"},
    "cli.summary.dupes_out": {
        "uz": "{count} ta takroriy yozuv -> {output} ({size})",
        "en": "{count} duplicate records -> {output} ({size})",
    },
    "cli.summary.dupes_out.one": {
        "uz": "{count} ta takroriy yozuv -> {output} ({size})",
        "en": "{count} duplicate record -> {output} ({size})",
    },
    "cli.warn.output_options_unused": {
        "uz": "{options}: faqat -o FAYL bilan ishlatiladi, e'tiborga olinmadi.",
        "en": "{options}: only used with -o FILE, ignored.",
    },
    # schema / validate
    "cli.cmd.schema.help": {
        "uz": "ma'lumotdan JSON Schema (draft 2020-12) yaratish",
        "en": "make a JSON Schema (draft 2020-12) from the data",
    },
    "cli.cmd.schema.description": {
        "uz": "Fayl tuzilmasidan JSON Schema (draft 2020-12) yaratadi: maydonlar turi, majburiy kalitlar, massiv\n"
              "elementlari, misollar. Standart bo'yicha barcha yozuvlar o'qiladi (shunda schema o'z faylini doim\n"
              "qabul qiladi); --sample N tezroq, lekin namunaga tushmagan holatlarni bilmaydi. --strict qat'iy\n"
              "cheklovlar qo'shadi (min/max, enum, formatlar, ortiqcha kalitlar taqiqi). OUTPUT berilmasa, schema\n"
              "ekranga chiqadi.",
        "en": "Makes a JSON Schema (draft 2020-12) from the structure of the file: field types, required keys, array\n"
              "items, examples. By default every record is read (so the schema always accepts its own file);\n"
              "--sample N is faster but does not know what the sample missed. --strict adds strict constraints\n"
              "(min/max, enum, formats, no extra keys). Without OUTPUT the schema is printed.",
    },
    "cli.cmd.schema.epilog": {
        "uz": "Misollar:\n"
              "  jsonreshape schema data.json data.schema.json\n"
              "  jsonreshape schema data.json -o data.schema.json --strict\n"
              "  jsonreshape schema data.json --record --title Mijozlar\n"
              "  jsonreshape schema data.json --sample 10000 --compact\n"
              "\n"
              "--document (standart): butun fayl uchun schema (massiv, kalitli obyekt yoki tashqi o'rama bilan);\n"
              "--record: bitta yozuv uchun schema.",
        "en": "Examples:\n"
              "  jsonreshape schema data.json data.schema.json\n"
              "  jsonreshape schema data.json -o data.schema.json --strict\n"
              "  jsonreshape schema data.json --record --title Customers\n"
              "  jsonreshape schema data.json --sample 10000 --compact\n"
              "\n"
              "--document (default): a schema of the whole file (with its array, keyed object or outer wrapper);\n"
              "--record: a schema of one record.",
    },
    "cli.group.schema": {"uz": "schema parametrlari", "en": "schema options"},
    "cli.help.schema_output": {
        "uz": "schema fayli (masalan data.schema.json); berilmasa, ekranga chiqadi",
        "en": "schema file (e.g. data.schema.json); printed when not given",
    },
    "cli.help.schema_sample": {
        "uz": "faqat N ta yozuvdan namuna olish (tezroq, lekin namunada yo'q holatlarni bilmaydi)",
        "en": "use a sample of N records only (faster, but misses what the sample does not hold)",
    },
    "cli.help.schema_full": {"uz": "barcha yozuvlarni o'qish (standart)", "en": "read every record (the default)"},
    "cli.help.schema_strict": {
        "uz": "qat'iy cheklovlar: min/max, uzunliklar, enum, formatlar, ortiqcha kalitlarni taqiqlash",
        "en": "strict constraints: min/max, lengths, enum, formats, no extra keys",
    },
    "cli.help.schema_document": {
        "uz": "butun fayl uchun schema (standart)",
        "en": "a schema of the whole file (default)",
    },
    "cli.help.schema_record": {"uz": "bitta yozuv uchun schema", "en": "a schema of one record"},
    "cli.help.schema_title": {
        "uz": "schema sarlavhasi (standart: fayl nomi)",
        "en": "schema title (default: the file name)",
    },
    "cli.stage.schema_strict": {"uz": "Qat'iy cheklovlar tekshirilmoqda", "en": "Checking the strict constraints"},
    "cli.warn.schema_sample": {
        "uz": "Schema {total} ta yozuvdan {count} tasi (namuna) bo'yicha yaratildi: namunaga tushmagan yozuvlar unga "
              "mos kelmasligi mumkin (barcha yozuvlar uchun --sample'ni olib tashlang).",
        "en": "The schema was made from a sample of {count} of {total} records: records outside the sample may not "
              "match it (remove --sample to read every record).",
    },
    "cli.warn.schema_sample.one": {
        "uz": "Schema {total} ta yozuvdan {count} tasi (namuna) bo'yicha yaratildi: namunaga tushmagan yozuvlar unga "
              "mos kelmasligi mumkin (barcha yozuvlar uchun --sample'ni olib tashlang).",
        "en": "The schema was made from a sample of {count} of {total} record: records outside the sample may not "
              "match it (remove --sample to read every record).",
    },
    "cli.summary.schema": {
        "uz": "JSON Schema -> {output}: {fields}, {records} bo'yicha ({seconds})",
        "en": "JSON Schema -> {output}: {fields} from {records} ({seconds})",
    },
    "cli.summary.schema_stdout": {
        "uz": "JSON Schema: {fields}, {records} bo'yicha ({seconds})",
        "en": "JSON Schema: {fields} from {records} ({seconds})",
    },
    "cli.cmd.validate.help": {
        "uz": "faylni JSON Schema bo'yicha tekshirish",
        "en": "check a file against a JSON Schema",
    },
    "cli.cmd.validate.description": {
        "uz": "Yozuvlarni JSON Schema (draft 2020-12) bo'yicha tekshiradi va har bir xatoni yozuv raqami, yo'l va\n"
              "sabab bilan chiqaradi: #yozuv [kalit] yo'l: xabar (kalit so'z). Sonlar aniq solishtiriladi, regex\n"
              "ECMA qoidalari bo'yicha; tarmoqdagi schemalar hech qachon o'qilmaydi. Xato topilsa yoki ba'zi\n"
              "yozuvlarni tekshirib bo'lmasa, chiqish kodi 5.",
        "en": "Checks the records against a JSON Schema (draft 2020-12) and prints every issue with the record\n"
              "number, the path and the reason: #record [key] path: message (keyword). Numbers compare exactly,\n"
              "regular expressions follow the ECMA rules; schemas on the network are never read. Exit code 5 when\n"
              "an issue is found or some records could not be checked.",
    },
    "cli.cmd.validate.epilog": {
        "uz": "Misollar:\n"
              "  jsonreshape validate data.json --schema data.schema.json\n"
              "  jsonreshape validate data.json --schema data.schema.json --formats --max-issues 20\n"
              "  jsonreshape validate data.json --schema data.schema.json --export xatolar.csv\n"
              "  jsonreshape validate data.jsonl --schema yozuv.schema.json --scope record --json\n"
              "\n"
              "--scope: auto (standart; schema'dagi izoh yoki uning ko'rinishi bo'yicha), record - har bir yozuv\n"
              "alohida, document - butun fayl. Chiqish kodlari: 0 - mos (har bir yozuv tekshirildi), 5 - xato\n"
              "topildi yoki ba'zi yozuvlarni tekshirib bo'lmadi (juda katta, o'qib bo'lmaydigan yoki juda chuqur:\n"
              "ular mosligi ko'rsatilmagan hisoblanadi), 1 - schema yoki parametr xatosi, 2 - faylda JSON xatosi.",
        "en": "Examples:\n"
              "  jsonreshape validate data.json --schema data.schema.json\n"
              "  jsonreshape validate data.json --schema data.schema.json --formats --max-issues 20\n"
              "  jsonreshape validate data.json --schema data.schema.json --export issues.csv\n"
              "  jsonreshape validate data.jsonl --schema record.schema.json --scope record --json\n"
              "\n"
              "--scope: auto (default; by the note in the schema or by its shape), record - every record on its\n"
              "own, document - the whole file. Exit codes: 0 - valid (every record checked), 5 - issues found or\n"
              "some records could not be checked (too large, unreadable or nested too deeply: they are not shown\n"
              "to match), 1 - a schema or option error, 2 - a JSON error in the file.",
    },
    "cli.group.validate": {"uz": "tekshiruv parametrlari", "en": "validation options"},
    "cli.help.validate_schema": {"uz": "JSON Schema fayli (draft 2020-12)", "en": "JSON Schema file (draft 2020-12)"},
    "cli.help.validate_scope": {
        "uz": "schema nimani tasvirlaydi: auto (standart), record - har bir yozuvni, document - butun faylni",
        "en": "what the schema describes: auto (default), record - every record, document - the whole file",
    },
    "cli.help.validate_formats": {
        "uz": "format kalit so'zini ham tekshirish (date-time, date, time, email, uri, uuid, ipv4, ipv6)",
        "en": "also check the format keyword (date-time, date, time, email, uri, uuid, ipv4, ipv6)",
    },
    "cli.help.max_issues": {
        "uz": "ekranga chiqariladigan xatolar soni (standart 100; 0 - faqat xulosa)",
        "en": "number of issues printed (default 100; 0 = the summary only)",
    },
    "cli.help.max_per_record": {
        "uz": "bitta yozuvdan olinadigan xatolar soni (standart 10; 0 - cheklovsiz)",
        "en": "issues kept per record (default 10; 0 = no limit)",
    },
    "cli.help.validate_export": {
        "uz": "barcha xatolarni faylga yozish: .jsonl, .json yoki .csv",
        "en": "write every issue to a file: .jsonl, .json or .csv",
    },
    "cli.help.validate_json": {"uz": "hisobotni JSON sifatida chiqarish", "en": "print the report as JSON"},
    "cli.stage.validating": {"uz": "Tekshirilmoqda", "en": "Validating"},
    "cli.validate.document": {"uz": "(hujjat)", "en": "(document)"},
    "cli.validate.ok": {
        "uz": "Schema'ga mos: {records} tekshirildi, xato yo'q ({seconds}).",
        "en": "Valid: {records} checked, no issues ({seconds}).",
    },
    "cli.validate.ok.one": {
        "uz": "Schema'ga mos: {records} tekshirildi, xato yo'q ({seconds}).",
        "en": "Valid: {records} checked, no issues ({seconds}).",
    },
    "cli.validate.failed": {
        "uz": "Schema'ga mos emas: {records}dan {invalid} tasida {issues} ({seconds}).",
        "en": "Not valid: {issues} in {invalid} of {records} ({seconds}).",
    },
    "cli.validate.keywords": {"uz": "Kalit so'zlar bo'yicha: {items}", "en": "By keyword: {items}"},
    "cli.validate.unchecked": {
        "uz": "Mosligi tasdiqlanmadi: {records} tekshirildi va xato topilmadi, lekin {skipped} tekshirilmadi "
              "(sababi ogohlantirishda) ({seconds}).",
        "en": "Not confirmed: {records} checked without issues, but {skipped} could not be checked (see the "
              "warning for why) ({seconds}).",
    },
    "cli.validate.skipped": {
        "uz": "Bundan tashqari {skipped} tekshirilmadi (sababi ogohlantirishda).",
        "en": "Besides, {skipped} could not be checked (see the warning for why).",
    },
    "cli.validate.more": {
        "uz": "Faqat dastlabki {shown} ta xato ko'rsatildi (jami {total} ta); ko'proq uchun --max-issues N yoki "
              "--export FAYL.",
        "en": "Only the first {shown} issues are shown (of {total}); use --max-issues N or --export FILE for more.",
    },
    "cli.validate.more.one": {
        "uz": "Faqat birinchi xato ko'rsatildi (jami {total} ta); ko'proq uchun --max-issues N yoki --export FAYL.",
        "en": "Only the first issue is shown (of {total}); use --max-issues N or --export FILE for more.",
    },
    "cli.validate.exported": {"uz": "{count} ta xato yozildi: {path}", "en": "{count} issues written to {path}"},
    "cli.validate.exported.one": {"uz": "{count} ta xato yozildi: {path}", "en": "{count} issue written to {path}"},
    "cli.count.issues": {"uz": "{count} ta xato", "en": "{count} issues"},
    "cli.count.issues.one": {"uz": "{count} ta xato", "en": "{count} issue"},
    "cli.error.output_is_schema": {
        "uz": "Natija fayli schema faylining o'zi: {path}",
        "en": "The output file is the schema file itself: {path}",
    },
    "cli.error.issues_format": {
        "uz": "Xatolar fayli nomi {formats} bilan tugashi kerak: {path}",
        "en": "The issues file must end in {formats}: {path}",
    },
    # split / merge / extract
    "cli.cmd.split.help": {
        "uz": "faylni qismlarga bo'lish (soni, hajmi yoki yozuvlari bo'yicha)",
        "en": "split a file into parts (by count, size or records)",
    },
    "cli.cmd.split.description": {
        "uz": "Faylni ketma-ket yozuvlardan iborat qismlarga bo'ladi: --parts N (hajmi teng N ta qism), --size\n"
              "HAJM (har qism ko'pi bilan shu hajmda) yoki --records N (har qismda N ta yozuv). Qismlar asl fayl\n"
              "formati, kodlashi, siqilishi va tashqi o'ramasini saqlaydi (--no-wrapper o'ramani tashlaydi; --to\n"
              "boshqa formatga yozadi). Nomlar: <nom>_<k><kengaytma>, masalan data_01.json. Hamma qism yoziladi\n"
              "yoki hech biri; bor qism fayllar faqat --force bilan almashtiriladi (dasturda saqlanmagan\n"
              "o'zgarishlari bor fayl hech qachon). --remove-stale oldingi bo'lishdan qolgan ortiqcha qismlarni\n"
              "o'chiradi.",
        "en": "Splits the file into parts of consecutive records: --parts N (N parts of about equal size), --size\n"
              "SIZE (at most that size each) or --records N (N records each). The parts keep the format, encoding,\n"
              "compression and outer wrapper of the file (--no-wrapper drops the wrapper; --to writes another\n"
              "format). Names: <name>_<k><extension>, e.g. data_01.json. Either every part is written or none;\n"
              "existing part files are replaced only with --force (a file with unsaved changes in this program\n"
              "never). --remove-stale deletes the extra parts left by an earlier split.",
    },
    "cli.cmd.split.epilog": {
        "uz": "Misollar:\n"
              "  jsonreshape split data.json --parts 10\n"
              "  jsonreshape split data.json --size 500MB -o qismlar\n"
              "  jsonreshape split data.jsonl --records 100000 --name \"{stem}_qism{n}{ext}\"\n"
              "  jsonreshape split data.json --parts 4 --to jsonl --where \"active == true\"\n"
              "  jsonreshape split data.json --parts 10 --force\n"
              "  jsonreshape split data.json --parts 10 --force --remove-stale\n"
              "\n"
              "HAJM: 500MB, 2GB (birliksiz son - megabayt; kamida 1MB). JSON xatosi bor faylni avval ta'mirlang\n"
              "(jsonreshape repair); JSON Lines fayldagi buzuq qatorlar uchun --drop-broken.\n"
              "--remove-stale faqat oldingi bo'lishning davomi bo'lgan fayllarni o'chiradi (masalan, 12 ta qismdan\n"
              "keyin 10 ta qism: data_11 va data_12); boshqa mos fayllar faqat ogohlantirishda ko'rsatiladi.",
        "en": "Examples:\n"
              "  jsonreshape split data.json --parts 10\n"
              "  jsonreshape split data.json --size 500MB -o parts\n"
              "  jsonreshape split data.jsonl --records 100000 --name \"{stem}_part{n}{ext}\"\n"
              "  jsonreshape split data.json --parts 4 --to jsonl --where \"active == true\"\n"
              "  jsonreshape split data.json --parts 10 --force\n"
              "  jsonreshape split data.json --parts 10 --force --remove-stale\n"
              "\n"
              "SIZE: 500MB, 2GB (a number without a unit is in megabytes; at least 1MB). Repair a file with a JSON\n"
              "error first (jsonreshape repair); for broken lines of a JSON Lines file use --drop-broken.\n"
              "--remove-stale deletes only files that continue an earlier split (e.g. 12 parts, then 10: data_11\n"
              "and data_12); other matching files are only named in a warning.",
    },
    "cli.group.split": {"uz": "bo'lish parametrlari", "en": "split options"},
    "cli.help.parts": {"uz": "N ta qismga bo'lish (hajmi teng)", "en": "split into N parts (of about equal size)"},
    "cli.help.records_per_part": {"uz": "har bir qismda N ta yozuv", "en": "N records per part"},
    "cli.help.part_size": {
        "uz": "qism hajmi, masalan 500MB yoki 2GB (birliksiz son - megabayt; kamida 1MB)",
        "en": "part size, e.g. 500MB or 2GB (a number without a unit is in megabytes; at least 1MB)",
    },
    "cli.help.balance": {
        "uz": "--parts uchun: bytes - hajmi teng (standart) yoki records - yozuvlar soni teng",
        "en": "with --parts: bytes - equal sizes (default) or records - equal numbers of records",
    },
    "cli.help.out_dir": {
        "uz": "qismlar papkasi (standart: kirish fayli papkasi)",
        "en": "folder of the parts (default: the input's folder)",
    },
    "cli.help.part_name": {
        "uz": "qism nomi andozasi: {stem} - fayl nomi, {n} - qism raqami (majburiy), {ext} - kengaytma (standart: "
              "{stem}_{n}{ext})",
        "en": "part name pattern: {stem} - the file name, {n} - the part number (required), {ext} - the extension "
              "(default: {stem}_{n}{ext})",
    },
    "cli.help.force": {
        "uz": "shu nomdagi qism fayllar bor bo'lsa, ularni almashtirish",
        "en": "replace part files of these names that already exist",
    },
    "cli.help.remove_stale": {
        "uz": "oldingi bo'lishdan qolgan ortiqcha qismlarni (shu nom andozasidagi, raqamlari davom etadigan fayllar) "
              "shu bo'lish bilan birga o'chirish",
        "en": "delete the extra parts left by an earlier split (files of the name pattern whose numbers continue "
              "this split's) together with this split",
    },
    "cli.hint.remove_stale": {
        "uz": "Oldingi bo'lishdan qolgan qismlarni o'chirish uchun bo'lishni --force --remove-stale bilan qayta "
              "bajaring (faqat raqamlari shu qismlarnikidan davom etadigan fayllar o'chiriladi).",
        "en": "To delete the parts left by an earlier split, run the split again with --force --remove-stale (only "
              "files whose numbers continue those of these parts are removed).",
    },
    "cli.split.removed": {
        "uz": "O'chirildi (oldingi bo'lishdan qolgan qism): {path}",
        "en": "Removed (a part of an earlier split): {path}",
    },
    "cli.help.drop_broken": {
        "uz": "JSON Lines fayldagi buzuq qatorlarni tashlab bo'lish",
        "en": "split a JSON Lines file without its broken lines",
    },
    "cli.stage.splitting": {"uz": "Bo'linmoqda", "en": "Splitting"},
    "cli.count.parts": {"uz": "{count} ta qism", "en": "{count} parts"},
    "cli.count.parts.one": {"uz": "{count} ta qism", "en": "{count} part"},
    "cli.summary.split": {
        "uz": "{records} -> {parts} ({folder}; {size}, {seconds})",
        "en": "{records} -> {parts} ({folder}; {size}, {seconds})",
    },
    "cli.error.parts_exist": {
        "uz": "Qism fayllar allaqachon bor ({count} ta): {names}",
        "en": "Part files already exist ({count}): {names}",
    },
    "cli.hint.force": {
        "uz": "Ularni almashtirish uchun --force qo'shing yoki boshqa papka (-o) yoki nom andozasini (--name) tanlang.",
        "en": "Add --force to replace them, or choose another folder (-o) or name pattern (--name).",
    },
    "cli.hint.repair_first": {
        "uz": "Avval faylni ta'mirlang (jsonreshape repair), keyin ta'mirlangan faylni bo'ling.",
        "en": "Repair the file first (jsonreshape repair), then split the repaired file.",
    },
    "cli.error.out_dir_file": {
        "uz": "-o papka bo'lishi kerak, bu esa fayl: {path}",
        "en": "-o must be a folder, but this is a file: {path}",
    },
    "cli.warn.balance_parts": {
        "uz": "--balance faqat --parts bilan ishlaydi: e'tiborga olinmadi.",
        "en": "--balance only works with --parts: ignored.",
    },
    "cli.cmd.merge.help": {
        "uz": "bir nechta faylni bittaga birlashtirish (dublikatlarni tozalash bilan)",
        "en": "merge several files into one (optionally without duplicates)",
    },
    "cli.cmd.merge.description": {
        "uz": "Fayllar yozuvlarini berilgan tartibda bitta JSON yoki JSON Lines faylga yozadi (-o OUTPUT; format\n"
              "kengaytmadan yoki --to / --jsonl dan). * va ? bilan berilgan andozalarni dasturning o'zi ochadi\n"
              "(cmd.exe va PowerShell ochmaydi), mos fayllar tabiiy tartibda olinadi: qism_2, keyin qism_10.\n"
              "--dedupe-by KEY takroriy yozuvlarni olib tashlaydi (standart: birinchi uchragani qoladi).",
        "en": "Writes the records of the files, in the order given, into one JSON or JSON Lines file (-o OUTPUT; the\n"
              "format comes from the extension or --to / --jsonl). Patterns with * and ? are expanded by the program\n"
              "itself (cmd.exe and PowerShell do not), the matching files in natural order: part_2, then part_10.\n"
              "--dedupe-by KEY drops duplicate records (by default the first one met stays).",
    },
    "cli.cmd.merge.epilog": {
        "uz": "Misollar:\n"
              "  jsonreshape merge a.json b.json -o hammasi.json\n"
              "  jsonreshape merge \"qism_*.json\" -o hammasi.jsonl\n"
              "  jsonreshape merge a.jsonl b.jsonl -o toza.jsonl --dedupe-by id\n"
              "  jsonreshape merge a.json b.json -o hammasi.json --dedupe-by id --keep last --indent 4\n"
              "\n"
              "Butun yozuv bo'yicha dublikatlar: --dedupe-by $; a'zo kaliti bo'yicha: --dedupe-by $key (PowerShell va\n"
              "bash'da bitta tirnoqda). Birinchi faylning tashqi o'ramasi saqlanadi (--no-wrapper - tashlanadi).",
        "en": "Examples:\n"
              "  jsonreshape merge a.json b.json -o all.json\n"
              "  jsonreshape merge \"part_*.json\" -o all.jsonl\n"
              "  jsonreshape merge a.jsonl b.jsonl -o clean.jsonl --dedupe-by id\n"
              "  jsonreshape merge a.json b.json -o all.json --dedupe-by id --keep last --indent 4\n"
              "\n"
              "Duplicates by the whole record: --dedupe-by $; by the member key: --dedupe-by $key (in single quotes\n"
              "in PowerShell and bash). The outer wrapper of the first file is kept (--no-wrapper drops it).",
    },
    "cli.group.merge": {"uz": "birlashtirish parametrlari", "en": "merge options"},
    "cli.help.merge_inputs": {
        "uz": "birlashtiriladigan fayllar (andoza ham bo'ladi, masalan \"qism_*.json\")",
        "en": "files to merge (patterns too, e.g. \"part_*.json\")",
    },
    "cli.help.merge_output": {"uz": "natija fayli: .json yoki .jsonl", "en": "output file: .json or .jsonl"},
    "cli.help.dedupe_by": {
        "uz": "shu kalit bo'yicha takroriy yozuvlarni olib tashlash: id, contact.phone, $ - butun yozuv, $key - a'zo "
              "kaliti",
        "en": "drop duplicate records by this key: id, contact.phone, $ = the whole record, $key = the member key",
    },
    "cli.help.merge_keep": {
        "uz": "--dedupe-by bilan qaysi nusxa qoladi: first - birinchi uchragani (standart), last - oxirgisi",
        "en": "with --dedupe-by, the copy that stays: first - the first one met (default), last - the last one",
    },
    "cli.help.merge_memory": {
        "uz": "--dedupe-by kalitlari uchun xotira, masalan 256MB (standart: «Xotira chegarasi»ning 1/4 qismi)",
        "en": "memory for the --dedupe-by keys, e.g. 256MB (default: 1/4 of the \"Memory limit\")",
    },
    "cli.help.merge_to": {
        "uz": "natija formati: json yoki jsonl (standart: OUTPUT kengaytmasidan)",
        "en": "output format: json or jsonl (default: from the OUTPUT extension)",
    },
    "cli.help.merge_no_wrapper": {
        "uz": "birinchi faylning tashqi o'ramasini yozmaslik (faqat yozuvlar)",
        "en": "do not write the outer wrapper of the first file (records only)",
    },
    "cli.stage.merging": {"uz": "Birlashtirilmoqda", "en": "Merging"},
    "cli.count.files": {"uz": "{count} ta fayl", "en": "{count} files"},
    "cli.count.files.one": {"uz": "{count} ta fayl", "en": "{count} file"},
    "cli.merge.input": {
        "uz": "  {path}: {written} / {records} ta yozuv",
        "en": "  {path}: {written} / {records} records",
    },
    "cli.merge.input.one": {
        "uz": "  {path}: {written} / {records} ta yozuv",
        "en": "  {path}: {written} / {records} record",
    },
    "cli.summary.merge": {
        "uz": "{inputs} -> {output}: {records} ({size}, {seconds})",
        "en": "{inputs} -> {output}: {records} ({size}, {seconds})",
    },
    "cli.summary.merge_dupes": {
        "uz": "{count} ta takroriy yozuv olib tashlandi ({groups} ta guruh)",
        "en": "{count} duplicate records dropped ({groups} groups)",
    },
    "cli.summary.merge_dupes.one": {
        "uz": "{count} ta takroriy yozuv olib tashlandi ({groups} ta guruh)",
        "en": "{count} duplicate record dropped ({groups} groups)",
    },
    "cli.error.no_glob_match": {
        "uz": "Andozaga mos fayl topilmadi: {pattern}",
        "en": "No file matches the pattern: {pattern}",
    },
    "cli.error.no_glob_match.hint": {
        "uz": "Papka va andozani tekshiring (masalan: \"qism_*.json\").",
        "en": "Check the folder and the pattern (for example: \"part_*.json\").",
    },
    "cli.warn.output_matched": {
        "uz": "«{pattern}» andozasiga natija fayli {path} ham mos keldi; u kirish fayli sifatida olinmadi.",
        "en": "The pattern \"{pattern}\" also matches the output file {path}; it is not taken as an input.",
    },
    "cli.warn.keep_without_dedupe": {
        "uz": "--keep faqat --dedupe-by bilan ishlaydi: e'tiborga olinmadi.",
        "en": "--keep only works with --dedupe-by: ignored.",
    },
    "cli.cmd.extract.help": {
        "uz": "yo'ldagi massiv, tugun yoki qiymatlarni alohida faylga ajratish",
        "en": "extract the array at a path, a node or values into a file",
    },
    "cli.cmd.extract.description": {
        "uz": "--path YO'L: shu yo'ldagi massiv (yoki obyekt) yozuvlarini tashqi o'ramasiz yozadi (har qanday\n"
              "formatda, masalan CSV); --node JOY: bitta tugunni (#5.orders, /data/5/orders); --gather ANDOZA: har\n"
              "bir yozuvdagi mos qiymatlarni bitta ro'yxatga yig'adi (orders[*] - barcha buyurtmalar). Tugun va\n"
              "yig'ilgan qiymatlar JSON yoki JSON Lines bo'ladi. Qiymatlar baytma-bayt ko'chiriladi: sonlar va matn\n"
              "o'zgarmaydi.",
        "en": "--path PATH writes the records of the array (or object) at that path without the outer wrapper (any\n"
              "format, e.g. CSV); --node LOCATION one node (#5.orders, /data/5/orders); --gather PATTERN collects\n"
              "the matching values of every record into one list (orders[*] = every order). A node and gathered\n"
              "values are written as JSON or JSON Lines. Values are copied byte for byte: numbers and text do not\n"
              "change.",
    },
    "cli.cmd.extract.epilog": {
        "uz": "Misollar:\n"
              "  jsonreshape extract katta.json elementlar.json --path data.items\n"
              "  jsonreshape extract katta.json elementlar.csv --path data.items --columns \"id,name\"\n"
              "  jsonreshape extract data.json buyurtmalar.jsonl --gather \"orders[*]\"\n"
              "  jsonreshape extract data.json -o tugun.json --node \"#5.orders\"",
        "en": "Examples:\n"
              "  jsonreshape extract big.json items.json --path data.items\n"
              "  jsonreshape extract big.json items.csv --path data.items --columns \"id,name\"\n"
              "  jsonreshape extract data.json orders.jsonl --gather \"orders[*]\"\n"
              "  jsonreshape extract data.json -o node.json --node \"#5.orders\"",
    },
    "cli.group.extract": {"uz": "ajratish parametrlari", "en": "extract options"},
    "cli.help.extract_output": {
        "uz": "natija fayli (format kengaytmadan yoki --to bilan)",
        "en": "output file (format from the extension or --to)",
    },
    "cli.help.extract_path": {
        "uz": "shu yo'ldagi massiv yozuvlarini tashqi o'ramasiz yozish (masalan data.items; --record-path bilan "
              "bir xil)",
        "en": "write the records of the array at this path without the outer wrapper (e.g. data.items; same as "
              "--record-path)",
    },
    "cli.help.gather": {
        "uz": "har bir yozuvdan shu andozaga mos qiymatlarni yig'ish: orders[*], contact.address, tags[*], **.id",
        "en": "gather the values matching this pattern from every record: orders[*], contact.address, tags[*], **.id",
    },
    "cli.stage.extracting": {"uz": "Ajratilmoqda", "en": "Extracting"},
    "cli.summary.extract": {
        "uz": "{values} ta qiymat -> {output} ({size}, {seconds})",
        "en": "{values} values -> {output} ({size}, {seconds})",
    },
    "cli.summary.extract.one": {
        "uz": "{values} ta qiymat -> {output} ({size}, {seconds})",
        "en": "{values} value -> {output} ({size}, {seconds})",
    },
    "cli.summary.node": {
        "uz": "{node} -> {output} ({size}, {seconds})",
        "en": "{node} -> {output} ({size}, {seconds})",
    },
    "cli.error.node_combined": {
        "uz": "--node bitta tugunni yozadi: uni {options} bilan birga ishlatib bo'lmaydi.",
        "en": "--node writes one node: it cannot be combined with {options}.",
    },
    "cli.error.extract_path_twice": {
        "uz": "--path ({path}) va --record-path ({record_path}) har xil: bittasini bering.",
        "en": "--path ({path}) and --record-path ({record_path}) differ: give one of them.",
    },
    "cli.warn.ignored_mode": {
        "uz": "{options}: {mode} ({format}) uchun ishlatilmaydi, e'tiborga olinmadi.",
        "en": "{options}: not used for {mode} ({format}), ignored.",
    },
    "cli.error.output_twice": {
        "uz": "Natija fayli ikki marta berilgan ({first} va -o {second}): bittasini qoldiring.",
        "en": "The output file is given twice ({first} and -o {second}): keep one.",
    },
    "cli.error.output_missing": {
        "uz": "Natija fayli ko'rsatilmagan: OUTPUT yoki -o FAYL bering.",
        "en": "No output file: give OUTPUT or -o FILE.",
    },
    "cli.error.in_file": {"uz": "Fayl: {path}", "en": "File: {path}"},
    # repair
    "cli.cmd.repair.help": {
        "uz": "buzuq JSON'ni ta'mirlash (hisobot bilan)",
        "en": "repair broken JSON (with a report)",
    },
    "cli.cmd.repair.description": {
        "uz": "Buzuq JSON yoki JSON Lines faylni to'g'ri JSON qilib OUTPUT ga yozadi va har bir tuzatishni aytadi:\n"
              "izohlar, oxirgi vergullar, bitta tirnoqlar, tirnoqsiz kalitlar, NaN / Infinity, BOM, jingalak qavs\n"
              "ichidagi obyektlar ro'yxati, ajratuvchisiz ketma-ket qiymatlar, kesilib qolgan fayl (oxirgi to'liq\n"
              "yozuvgacha). Asl fayl o'zgarmaydi. Ta'mirlab bo'lmaydigan joyda to'xtaydi va hech narsa yozmaydi\n"
              "(--allow-partial: undan oldingi yozuvlar yoziladi). --check faqat tekshiradi.",
        "en": "Writes a broken JSON or JSON Lines file to OUTPUT as valid JSON and reports every fix: comments,\n"
              "trailing commas, single quotes, unquoted keys, NaN / Infinity, BOM, a list of objects in curly\n"
              "braces, values one after another, a cut-off file (up to the last complete record). The original is\n"
              "never modified. It stops at a place it cannot repair and writes nothing (--allow-partial writes the\n"
              "records before it). --check only checks.",
    },
    "cli.cmd.repair.epilog": {
        "uz": "Misollar:\n"
              "  jsonreshape repair buzuq.json tuzatilgan.json\n"
              "  jsonreshape repair buzuq.json --check\n"
              "  jsonreshape repair buzuq.json -o tuzatilgan.json --report tuzatishlar.csv\n"
              "  jsonreshape repair buzuq.jsonl tuzatilgan.jsonl --nan string --allow-partial\n"
              "\n"
              "Chiqish kodlari: 0 - yozildi (--check: muammo yo'q); 2 - ta'mirlab bo'lmaydi yoki --check muammo\n"
              "topdi; 1 - noto'g'ri parametr (masalan, OUTPUT kirish faylining o'zi).",
        "en": "Examples:\n"
              "  jsonreshape repair broken.json fixed.json\n"
              "  jsonreshape repair broken.json --check\n"
              "  jsonreshape repair broken.json -o fixed.json --report fixes.csv\n"
              "  jsonreshape repair broken.jsonl fixed.jsonl --nan string --allow-partial\n"
              "\n"
              "Exit codes: 0 - written (--check: no problems); 2 - cannot be repaired, or --check found problems;\n"
              "1 - an invalid option (e.g. OUTPUT is the input itself).",
    },
    "cli.group.repair": {"uz": "ta'mirlash parametrlari", "en": "repair options"},
    "cli.help.repair_input": {
        "uz": "ta'mirlanadigan fayl (.json, .jsonl, .gz; UTF-8 yoki UTF-16)",
        "en": "file to repair (.json, .jsonl, .gz; UTF-8 or UTF-16)",
    },
    "cli.help.repair_output": {
        "uz": "ta'mirlangan fayl (kirish fayli bo'lishi mumkin emas; .gz bilan tugasa siqiladi)",
        "en": "repaired file (must not be the input; compressed when it ends in .gz)",
    },
    "cli.help.repair_check": {
        "uz": "faqat tekshirish: hech narsa yozilmaydi (chiqish kodi 0 - muammo yo'q, 2 - muammo bor)",
        "en": "check only: nothing is written (exit code 0 = no problems, 2 = problems)",
    },
    "cli.help.repair_nan": {
        "uz": "NaN va Infinity o'rniga: null (standart) yoki string (\"NaN\" matni)",
        "en": "NaN and Infinity become: null (default) or string (the text \"NaN\")",
    },
    "cli.help.repair_jsonl": {
        "uz": "ketma-ket qiymatlarni massiv o'rniga JSON Lines qilib yozish",
        "en": "write values one after another as JSON Lines instead of an array",
    },
    "cli.help.repair_partial": {
        "uz": "ta'mirlab bo'lmaydigan joy topilsa, undan oldingi to'liq yozuvlarni yozish",
        "en": "at a place that cannot be repaired, write the complete records before it",
    },
    "cli.help.repair_report": {
        "uz": "har bir tuzatishni faylga yozish: .csv, .json yoki .jsonl",
        "en": "write every fix to a file: .csv, .json or .jsonl",
    },
    "cli.help.repair_json": {"uz": "hisobotni JSON sifatida chiqarish", "en": "print the report as JSON"},
    "cli.help.repair_format": {
        "uz": "kirish formati (standart: auto; fayl boshi buzuq bo'lib, aniqlash adashsa, ko'rsating)",
        "en": "input format (default: auto; give it when a broken start misleads the detection)",
    },
    "cli.stage.repairing": {"uz": "Ta'mirlanmoqda", "en": "Repairing"},
    "cli.stage.checking": {"uz": "Tekshirilmoqda", "en": "Checking"},
    "cli.error.repair_output": {
        "uz": "Ta'mirlangan fayl uchun nom bering (OUTPUT yoki -o FAYL) yoki faqat tekshirish uchun --check qo'shing.",
        "en": "Give a name for the repaired file (OUTPUT or -o FILE), or add --check to only check.",
    },
    "cli.error.report_is_output": {
        "uz": "Hisobot fayli ta'mirlangan faylning o'zi: {path}",
        "en": "The report file is the repaired file itself: {path}",
    },
    "cli.error.report_format": {
        "uz": "Hisobot fayli nomi {formats} bilan tugashi kerak: {path}",
        "en": "The report file must end in {formats}: {path}",
    },
    "cli.hint.repair_partial": {
        "uz": "Muammodan oldingi {count} ta to'liq yozuvni saqlash uchun --allow-partial qo'shing yoki shu joyni "
              "qo'lda tuzating.",
        "en": "Add --allow-partial to save the {count} complete records before the problem, or fix that place by "
              "hand.",
    },
    "cli.hint.repair_partial.one": {
        "uz": "Muammodan oldingi {count} ta to'liq yozuvni saqlash uchun --allow-partial qo'shing yoki shu joyni "
              "qo'lda tuzating.",
        "en": "Add --allow-partial to save the {count} complete record before the problem, or fix that place by "
              "hand.",
    },
    "cli.hint.repair_report": {
        "uz": "Har bir tuzatish ro'yxati uchun --report FAYL.csv qo'shing.",
        "en": "Add --report FILE.csv for the list of every fix.",
    },
    "cli.summary.repair_report": {
        "uz": "Tuzatishlar ro'yxati ({count} ta) yozildi: {path}",
        "en": "The list of fixes ({count}) was written to {path}",
    },
    "cli.summary.repair_report.one": {
        "uz": "Tuzatishlar ro'yxati ({count} ta) yozildi: {path}",
        "en": "The list of fixes ({count}) was written to {path}",
    },
    "cli.warn.check_output": {
        "uz": "--check hech narsa yozmaydi: {path} yozilmaydi.",
        "en": "--check writes nothing: {path} is not written.",
    },
    "cli.warn.repair_partial": {
        "uz": "Fayl to'liq ta'mirlanmadi: {line}-qator, {column}-ustundagi muammodan keyingi ma'lumot natijaga "
              "yozilmadi.",
        "en": "The file was not repaired completely: the data after the problem at line {line}, column {column} is "
              "not in the result.",
    },
    # diff
    "cli.cmd.diff.help": {
        "uz": "ikki faylni solishtirish (yozuvma-yozuv)",
        "en": "compare two files (record by record)",
    },
    "cli.cmd.diff.description": {
        "uz": "A va B fayllarni yozuvma-yozuv solishtiradi: ~ o'zgargan, + qo'shilgan (faqat B da), - o'chirilgan\n"
              "(faqat A da), > joyi o'zgargan element. Sonlar qiymati bo'yicha (1 = 1.0; --numbers text - yozilishi\n"
              "bo'yicha), obyekt kalitlari tartibi hisobga olinmaydi. --key KEY yozuvlarni kalit bo'yicha juftlaydi\n"
              "(har qanday hajmda); kalitsiz kichik fayllar (standart 200 MB gacha) mazmun bo'yicha tekislanadi,\n"
              "kattaroqlari uchun --key yoki --by-position kerak.",
        "en": "Compares the files A and B record by record: ~ changed, + added (only in B), - removed (only in A),\n"
              "> an element that moved. Numbers compare by value (1 = 1.0; --numbers text compares their spelling),\n"
              "the order of object keys does not matter. --key KEY pairs the records by a key (any size); without a\n"
              "key, smaller files (up to 200 MB by default) are aligned by content, bigger ones need --key or\n"
              "--by-position.",
    },
    "cli.cmd.diff.epilog": {
        "uz": "Misollar:\n"
              "  jsonreshape diff eski.json yangi.json\n"
              "  jsonreshape diff eski.json yangi.json --key id\n"
              "  jsonreshape diff eski.jsonl yangi.jsonl --key id --array-key tags --limit 20\n"
              "  jsonreshape diff eski.json yangi.json --ignore-order --exit-code\n"
              "  jsonreshape diff eski.json yangi.json --key id -o farqlar.csv --summary\n"
              "\n"
              "Chiqish kodlari: 0 - solishtirildi; --exit-code bilan 0 - bir xil, 5 - farq bor; 1 - noto'g'ri\n"
              "parametr; 2 - faylda JSON xatosi.",
        "en": "Examples:\n"
              "  jsonreshape diff old.json new.json\n"
              "  jsonreshape diff old.json new.json --key id\n"
              "  jsonreshape diff old.jsonl new.jsonl --key id --array-key tags --limit 20\n"
              "  jsonreshape diff old.json new.json --ignore-order --exit-code\n"
              "  jsonreshape diff old.json new.json --key id -o differences.csv --summary\n"
              "\n"
              "Exit codes: 0 - compared; with --exit-code 0 - identical, 5 - different; 1 - an invalid option; 2 - a\n"
              "JSON error in a file.",
    },
    "cli.group.diff": {"uz": "solishtirish parametrlari", "en": "comparison options"},
    "cli.help.diff_a": {"uz": "birinchi (eski) fayl", "en": "first (old) file"},
    "cli.help.diff_b": {"uz": "ikkinchi (yangi) fayl", "en": "second (new) file"},
    "cli.help.diff_key": {
        "uz": "yozuvlarni shu kalit bo'yicha juftlash: id, contact.phone, $key (har qanday hajmdagi fayl uchun)",
        "en": "pair the records by this key: id, contact.phone, $key (for files of any size)",
    },
    "cli.help.by_position": {
        "uz": "yozuvlarni o'rni bo'yicha juftlash (0-yozuv 0-yozuv bilan)",
        "en": "pair the records by position (record 0 with record 0)",
    },
    "cli.help.align": {
        "uz": "yozuvlarni mazmuni bo'yicha tekislash (kalitsiz kichik fayllar uchun standart)",
        "en": "align the records by content (the default for smaller files without a key)",
    },
    "cli.help.array_key": {
        "uz": "massiv elementlarini shu maydon bo'yicha juftlash: MAYDON yoki YO'L=MAYDON (masalan id yoki "
              "orders=oid); bir necha marta berish mumkin",
        "en": "match array elements by this field: FIELD or PATH=FIELD (e.g. id or orders=oid); may be repeated",
    },
    "cli.help.numbers": {
        "uz": "sonlarni solishtirish: value - qiymati bo'yicha, 1 = 1.0 (standart), exact - value bilan bir xil, text "
              "- yozilishi bo'yicha",
        "en": "how numbers compare: value - by value, 1 = 1.0 (default), exact - same as value, text - by spelling",
    },
    "cli.help.ignore_order": {
        "uz": "tartibni hisobga olmaslik: massivlar to'plam sifatida, --key'siz yozuvlar butun qiymati bo'yicha "
              "juftlanadi",
        "en": "ignore order: arrays compare as multisets, records without --key are paired by their whole value",
    },
    "cli.help.max_changes": {
        "uz": "bitta yozuvdagi saqlanadigan o'zgarishlar soni (standart 10000; 0 - faqat sanash)",
        "en": "changes kept per record (default 10000; 0 = count only)",
    },
    "cli.help.diff_limit": {
        "uz": "ekranga chiqariladigan farqli yozuvlar soni (standart 100; 0 - hammasi)",
        "en": "number of differing records printed (default 100; 0 = all)",
    },
    "cli.help.diff_summary": {
        "uz": "faqat sonlarni chiqarish (yozuvlarsiz)",
        "en": "print the counts only (no records)",
    },
    "cli.help.diff_json": {
        "uz": "har bir farqli yozuvni JSON Lines qatori sifatida chiqarish, oxirida xulosa",
        "en": "print every differing record as a JSON Lines line, then the summary",
    },
    "cli.help.diff_out": {
        "uz": "barcha farqlarni faylga yozish: .json, .jsonl yoki .csv",
        "en": "write every difference to a file: .json, .jsonl or .csv",
    },
    "cli.help.exit_code": {
        "uz": "farq bo'lsa, chiqish kodi 5 (bir xil bo'lsa 0)",
        "en": "exit code 5 when the files differ (0 when identical)",
    },
    "cli.help.diff_memory": {
        "uz": "--key kalitlari uchun xotira, masalan 256MB (standart: «Xotira chegarasi»ning 1/4 qismi)",
        "en": "memory for the --key keys, e.g. 256MB (default: 1/4 of the \"Memory limit\")",
    },
    "cli.stage.comparing": {"uz": "Solishtirilmoqda", "en": "Comparing"},
    "cli.diff.wrapper": {"uz": "(tashqi o'rama)", "en": "(wrapper)"},
    "cli.diff.more_changes": {
        "uz": "… yana {count} ta o'zgarish (hammasi uchun --max-changes N yoki -o FAYL)",
        "en": "… {count} more changes (use --max-changes N or -o FILE for all)",
    },
    "cli.diff.more_changes.one": {
        "uz": "… yana {count} ta o'zgarish (hammasi uchun --max-changes N yoki -o FAYL)",
        "en": "… {count} more change (use --max-changes N or -o FILE for all)",
    },
    "cli.diff.identical": {
        "uz": "Farq yo'q: A va B bir xil ({mode}; yozuvlar: A {a}, B {b}; {seconds}).",
        "en": "No differences: A and B are the same ({mode}; records: A {a}, B {b}; {seconds}).",
    },
    "cli.diff.summary": {
        "uz": "Farqli yozuvlar: o'zgargan {changed}, o'chirilgan {removed}, qo'shilgan {added}; bir xil {equal} "
              "({mode}; yozuvlar: A {a}, B {b}; {seconds})",
        "en": "Differing records: changed {changed}, removed {removed}, added {added}; equal {equal} ({mode}; "
              "records: A {a}, B {b}; {seconds})",
    },
    "cli.diff.wrapper_differs": {"uz": "tashqi o'ramalar farq qiladi", "en": "the wrappers differ"},
    "cli.diff.moved": {"uz": "{count} ta element joyi o'zgargan", "en": "{count} elements moved"},
    "cli.diff.moved.one": {"uz": "{count} ta element joyi o'zgargan", "en": "{count} element moved"},
    "cli.diff.more": {
        "uz": "Faqat dastlabki {shown} ta farqli yozuv ko'rsatildi (jami {total} ta); hammasi uchun --limit 0 yoki "
              "-o FAYL.",
        "en": "Only the first {shown} differing records are shown (of {total}); use --limit 0 or -o FILE for all.",
    },
    "cli.diff.more.one": {
        "uz": "Faqat birinchi farqli yozuv ko'rsatildi (jami {total} ta); hammasi uchun --limit 0 yoki -o FAYL.",
        "en": "Only the first differing record is shown (of {total}); use --limit 0 or -o FILE for all.",
    },
    "cli.diff.written": {
        "uz": "{count} ta farqli yozuv yozildi: {path}",
        "en": "{count} differing records written to {path}",
    },
    "cli.diff.written.one": {
        "uz": "{count} ta farqli yozuv yozildi: {path}",
        "en": "{count} differing record written to {path}",
    },
    "cli.diff.mode.key": {"uz": "kalit bo'yicha: {key}", "en": "by key: {key}"},
    "cli.diff.mode.align": {"uz": "mazmun bo'yicha tekislab", "en": "aligned by content"},
    "cli.diff.mode.position": {"uz": "o'rni bo'yicha", "en": "by position"},
    "cli.diff.mode.document": {"uz": "bitta hujjat sifatida", "en": "as one document"},
    "cli.diff.mode.unordered": {"uz": "tartibsiz, butun yozuv bo'yicha", "en": "unordered, by whole record"},
    "cli.error.diff_format": {
        "uz": "Farqlar fayli nomi {formats} bilan tugashi kerak: {path}",
        "en": "The differences file must end in {formats}: {path}",
    },
    "cli.warn.memory_limit_cap_tool": {
        "uz": "--memory-limit {value}: «{setting}» sozlamasi ({limit}) bu ishga ko'pi bilan {cap} beradi; {cap} "
              "ishlatiladi.",
        "en": "--memory-limit {value}: the \"{setting}\" setting ({limit}) allows at most {cap} here; {cap} is used.",
    },
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
    "gui.session.bookmark.gone": {"uz": "(o'chirilgan)", "en": "(deleted)"},
    # ---- gui.edit ----
    "gui.edit.reason.indexing": {
        "uz": "Tahrirlash hali tayyor emas: fayl o'qilmoqda.",
        "en": "Editing is not ready yet: the file is still being read.",
    },
    "gui.edit.reason.partial": {
        "uz": "Faylda JSON xatosi bor: faqat xatogacha bo'lgan yozuvlar ochilgan, shuning uchun uni tahrirlab "
        "bo'lmaydi.",
        "en": "The file has a JSON error: only the records before the error are open, so it cannot be edited.",
    },
    "gui.edit.reason.locked": {
        "uz": "Bu fayl boshqa oynada yoki boshqa dasturda tahrirlanmoqda: bu yerda uni faqat ko'rish mumkin.",
        "en": "This file is being edited in another tab or program: here it can only be viewed.",
    },
    "gui.edit.reason.huge_single": {
        "uz": "Fayl bitta juda katta JSON qiymatidan iborat (tahrirlash chegarasidan katta): uni faqat ko'rish mumkin.",
        "en": "The file is one very large JSON value (above the editing limit): it can only be viewed.",
    },
    "gui.edit.reason.recovery_pending": {
        "uz": "Avvalgi saqlanmagan o'zgarishlar topildi: avval ularni tiklang yoki o'chiring.",
        "en": "Unsaved changes from before were found: restore or discard them first.",
    },
    "gui.edit.reason.file_changed": {
        "uz": "Fayl diskda o'zgardi, shuning uchun tahrirlash to'xtatildi. O'zgarishlarni JSON Patch sifatida "
        "saqlang yoki faylni qayta oching.",
        "en": "The file changed on disk, so editing stopped. Save your changes as a JSON Patch or reopen the file.",
    },
    "gui.edit.reason.file_grown": {
        "uz": "Fayl diskda kattalashdi (oxiriga ma'lumot qo'shildi), shuning uchun tahrirlash to'xtatildi. "
        "O'zgarishlarni boshqa nom bilan saqlang yoki faylni qayta oching.",
        "en": "The file grew on disk (data was appended), so editing stopped. Save your changes under another name "
        "or reopen the file.",
    },
    "gui.edit.reason.saving": {"uz": "Fayl saqlanmoqda…", "en": "The file is being saved…"},
    "gui.edit.reason.applying": {"uz": "O'zgarish qo'llanmoqda…", "en": "A change is being applied…"},
    "gui.edit.reason.closed": {
        "uz": "Tahrirlash yopilgan (fayl saqlandi yoki yopilmoqda).",
        "en": "Editing is closed (the file was saved or is being closed).",
    },
    "gui.edit.reason.unavailable": {
        "uz": "Tahrirlashni boshlab bo'lmadi (sababi xato oynasida ko'rsatilgan).",
        "en": "Editing could not be started (the error message says why).",
    },
    "gui.edit.unavailable": {"uz": "Tahrirlab bo'lmaydi: {reason}", "en": "Editing is not possible: {reason}"},
    "gui.edit.busy": {
        "uz": "Oldingi amal hali tugamadi: biroz kuting.",
        "en": "The previous action has not finished yet: wait a moment.",
    },
    "gui.edit.too_many": {
        "uz": "{count} ta tugun tanlangan, bitta buyruq esa ko'pi bilan {max} ta tugun bilan ishlaydi. Ommaviy "
        "amallardan foydalaning (Ctrl+H).",
        "en": "{count} nodes are selected, but one command works on at most {max} nodes. Use Bulk operations "
        "(Ctrl+H).",
    },
    "gui.edit.stale": {
        "uz": "Hujjat shu orada o'zgardi: amalni yana bir bor bajaring.",
        "en": "The document changed meanwhile: do it once more.",
    },
    "gui.edit.nothing_undo": {"uz": "Ortga qaytariladigan amal yo'q.", "en": "Nothing to undo."},
    "gui.edit.nothing_redo": {"uz": "Qaytariladigan amal yo'q.", "en": "Nothing to redo."},
    "gui.edit.restored": {
        "uz": "Saqlanmagan o'zgarishlar tiklandi ({steps} ta qadam).",
        "en": "The unsaved changes were restored ({steps} steps).",
    },
    "gui.edit.preview_stale": {
        "uz": "Hisob eskirdi: hujjat shu orada o'zgardi. Oldindan ko'rishni qayta bajaring.",
        "en": "The preview is out of date: the document changed meanwhile. Run the preview again.",
    },
    "gui.edit.patch_exported": {"uz": "{count} ta amal yozildi: {path}", "en": "{count} operations written to {path}"},
    "gui.edit.nothing_to_discard": {"uz": "Tashlab yuboriladigan o'zgarish yo'q.", "en": "There are no changes to discard."},
    "gui.edit.discarded": {"uz": "Barcha o'zgarishlar tashlab yuborildi.", "en": "All changes were discarded."},
    "gui.edit.nothing_to_save": {"uz": "Saqlanadigan o'zgarish yo'q.", "en": "There are no changes to save."},
    "gui.edit.save_cancelled": {
        "uz": "Saqlash bekor qilindi: fayl o'zgarmadi.",
        "en": "Saving was cancelled: the file did not change.",
    },
    "gui.edit.saved": {"uz": "Saqlandi: {path} ({records} ta yozuv).", "en": "Saved: {path} ({records} records)."},
    "gui.edit.save_wait": {
        "uz": "Fayl saqlanmoqda: saqlash tugashini kuting.",
        "en": "The file is being saved: wait for the save to finish.",
    },
    "gui.edit.task.save": {"uz": "Saqlanmoqda: {name}", "en": "Saving {name}"},
    "gui.edit.task.save_as": {"uz": "Saqlanmoqda: {name} → {target}", "en": "Saving {name} as {target}"},
    "gui.edit.task.delete_records": {"uz": "Yozuvlar o'chirilmoqda: {name}", "en": "Deleting records in {name}"},
    "gui.edit.task.duplicate_records": {"uz": "Yozuvlar nusxalanmoqda: {name}", "en": "Duplicating records in {name}"},
    "gui.edit.task.bulk_preview": {
        "uz": "Ommaviy amal hisoblanmoqda: {name}",
        "en": "Computing a bulk operation for {name}",
    },
    "gui.edit.task.import_patch": {"uz": "JSON Patch qo'llanmoqda: {name}", "en": "Applying a JSON Patch to {name}"},
    "gui.edit.task.export_patch": {
        "uz": "O'zgarishlar JSON Patch sifatida yozilmoqda: {name}",
        "en": "Writing the changes of {name} as a JSON Patch",
    },
    "gui.edit.task.export_pending": {
        "uz": "Avvalgi o'zgarishlar JSON Patch sifatida yozilmoqda: {name}",
        "en": "Writing the earlier changes of {name} as a JSON Patch",
    },
    "gui.edit.ask.title": {"uz": "Saqlanmagan o'zgarishlar", "en": "Unsaved changes"},
    "gui.edit.ask.title.brace_list": {"uz": "Standart bo'lmagan JSON", "en": "Non-standard JSON"},
    "gui.edit.ask.title.broken": {"uz": "Buzilgan qatorlar", "en": "Broken lines"},
    "gui.edit.ask.title.gzip": {"uz": "Siqilgan fayl", "en": "Compressed file"},
    "gui.edit.ask.title.save_as": {"uz": "Joyida saqlab bo'lmadi", "en": "Cannot save in place"},
    "gui.edit.ask.title.damaged": {"uz": "Shikastlangan o'zgarishlar", "en": "Damaged changes"},
    "gui.edit.ask.title.target": {"uz": "Fayl boshqa varaqda ochiq", "en": "The file is open in another tab"},
    "gui.edit.ask.close": {
        "uz": "{name} faylida saqlanmagan o'zgarishlar bor ({steps} ta qadam). Ular saqlansinmi?",
        "en": "{name} has unsaved changes ({steps} steps). Save them?",
    },
    "gui.edit.ask.close_changed": {
        "uz": "{name} fayli diskda o'zgardi, unda esa saqlanmagan o'zgarishlar bor ({steps} ta qadam). Faylni endi "
        "joyida saqlab bo'lmaydi: o'zgarishlar JSON Patch sifatida saqlansinmi?",
        "en": "{name} changed on disk and has unsaved changes ({steps} steps). The file can no longer be saved in "
        "place: save the changes as a JSON Patch?",
    },
    "gui.edit.ask.close_grown": {
        "uz": "{name} fayli diskda kattalashdi, unda esa saqlanmagan o'zgarishlar bor ({steps} ta qadam). Faylni "
        "joyida saqlab bo'lmaydi: u boshqa nom bilan saqlansinmi?",
        "en": "{name} grew on disk and has unsaved changes ({steps} steps). It cannot be saved in place: save it "
        "under another name?",
    },
    "gui.edit.ask.discard": {
        "uz": "Barcha saqlanmagan o'zgarishlar ({steps} ta qadam) o'chirilsinmi? Ularni qaytarib bo'lmaydi.",
        "en": "Discard all unsaved changes ({steps} steps)? This cannot be undone.",
    },
    "gui.edit.ask.discard_pending": {
        "uz": "{time} dagi saqlanmagan o'zgarishlar ({steps} ta qadam) butunlay o'chirilsinmi? Ularni qaytarib "
        "bo'lmaydi.",
        "en": "Delete the unsaved changes from {time} ({steps} steps) for good? This cannot be undone.",
    },
    "gui.edit.ask.damaged": {
        "uz": "Tahrir qatlami {seq}-qadamda buzilgan. {last_good}-qadamgacha bo'lgan o'zgarishlar tiklansinmi? "
        "Keyingi qadamlar yo'qoladi.",
        "en": "The edit layer is damaged at step {seq}. Restore the changes up to step {last_good}? The later "
        "steps are lost.",
    },
    "gui.edit.ask.broken_lines": {
        "uz": "Faylda {count} ta buzilgan qator bor. Ular saqlangan faylga yozilmaydi. Davom etilsinmi?",
        "en": "The file has {count} broken lines. They are not written to the saved file. Continue?",
    },
    "gui.edit.ask.save_as_instead": {
        "uz": "Faylni joyida saqlab bo'lmadi. U boshqa nom bilan saqlansinmi?",
        "en": "The file cannot be saved in place. Save it under another name?",
    },
    "gui.edit.button.save": {"uz": "Saqlash", "en": "Save"},
    "gui.edit.button.save_as": {"uz": "Boshqacha saqlash…", "en": "Save As…"},
    "gui.edit.button.patch": {"uz": "JSON Patch sifatida saqlash…", "en": "Save as JSON Patch…"},
    "gui.edit.button.discard": {"uz": "Saqlamaslik", "en": "Don't save"},
    "gui.edit.button.cancel": {"uz": "Bekor qilish", "en": "Cancel"},
    "gui.edit.path.save_as": {"uz": "Boshqacha saqlash", "en": "Save As"},
    "gui.edit.path.patch": {
        "uz": "O'zgarishlarni JSON Patch sifatida saqlash",
        "en": "Save the changes as a JSON Patch",
    },
    "gui.edit.path.filter_patch": {"uz": "JSON Patch (*.json)", "en": "JSON Patch (*.json)"},
    "gui.edit.path.filter_all": {"uz": "Barcha fayllar (*)", "en": "All files (*)"},
    "gui.edit.path.copy_suffix": {"uz": "_tahrirlangan", "en": "_edited"},
    "gui.edit.pending.gone": {"uz": "Bu tahrir qatlami endi mavjud emas.", "en": "This edit layer no longer exists."},
    "gui.edit.pending.not_restorable": {
        "uz": "Bu o'zgarishlar faylning boshqa versiyasiga tegishli yoki buzilgan: ularni JSON Patch sifatida "
        "saqlash yoki o'chirish mumkin.",
        "en": "These changes belong to another version of the file or are damaged: they can be saved as a JSON "
        "Patch or deleted.",
    },
    "gui.edit.pending.roll_conflict": {
        "uz": "To'xtab qolgan saqlashni na yakunlab, na bekor qilib bo'ldi: {message}",
        "en": "The interrupted save could neither be finished nor undone: {message}",
    },
    "gui.edit.pending.roll_conflict.hint": {
        "uz": "Ko'rsatilgan fayllarni qo'lda tekshiring; saqlash yozuvi (jurnal) o'chirilmadi.",
        "en": "Check the files named above by hand; the save journal was kept.",
    },
    "gui.edit.pending.roll_kept": {
        "uz": "{path} fayli hali o'zgarishlar kiritilgan versiyada: oxirgi saqlash faylga yetib bormagan.",
        "en": "{path} is still the version the changes were made for: the last save never reached the file.",
    },
    "gui.edit.pending.roll_unconfirmed": {
        "uz": "To'xtab qolgan saqlash yakunlanganini tasdiqlab bo'lmadi. {message}",
        "en": "The interrupted save could not be confirmed. {message}",
    },
    "gui.edit.pending.roll_unconfirmed.hint": {
        "uz": "O'zgarishlar o'chirilmadi: ularni tiklang (\"Tiklash\") va faylni qayta saqlang.",
        "en": "The changes were kept: restore them (\"Restore\") and save the file again.",
    },
    "gui.edit.refused": {"uz": "Bajarilmadi: {reason}", "en": "Not done: {reason}"},
    "gui.edit.target.unsaved": {
        "uz": "{path} fayli boshqa tabda ochiq va unda saqlanmagan o'zgarishlar (yoki tugamagan amal) bor: uning "
        "ustiga yozib bo'lmaydi.",
        "en": "{path} is open in another tab with unsaved changes (or an operation that has not finished): it "
        "cannot be overwritten.",
    },
    "gui.edit.target.unsaved.hint": {
        "uz": "Avval o'sha tabdagi o'zgarishlarni saqlang yoki tabni yoping, yoxud boshqa nom tanlang.",
        "en": "First save the changes in that tab or close it, or choose another name.",
    },
    "gui.edit.target.locked": {
        "uz": "{path} fayli dasturning boshqa oynasida tahrirlash uchun ochiq: uning ustiga yozib bo'lmaydi.",
        "en": "{path} is open for editing in another window of the program: it cannot be overwritten.",
    },
    "gui.edit.target.locked.hint": {
        "uz": "Faylni o'sha oynada yoping yoki boshqa nom tanlang.",
        "en": "Close the file in that window or choose another name.",
    },
    "gui.edit.target.open": {
        "uz": "{path} fayli boshqa tabda ochiq. Uning ustiga yozilsinmi? O'sha tab yangi faylni qayta ochadi.",
        "en": "{path} is open in another tab. Overwrite it? That tab will open the new file again.",
    },
    "gui.edit.target.pending": {
        "uz": "{path} faylida dasturda saqlanmagan o'zgarishlar bor (ular tiklash uchun saqlab qo'yilgan): uning "
              "ustiga yozib bo'lmaydi.",
        "en": "{path} has unsaved changes in this program (they are kept to be restored): it cannot be overwritten.",
    },
    "gui.edit.target.pending.hint": {
        "uz": "Avval faylni jsonreshape'da oching va o'zgarishlarni tiklab saqlang yoki bekor qiling, yoxud boshqa "
              "nom tanlang.",
        "en": "First open the file in jsonreshape and restore and save the changes, or discard them, or choose "
              "another name.",
    },
    "gui.edit.ask.busy_title": {"uz": "Amal davom etmoqda", "en": "An operation is running"},
    "gui.edit.ask.busy": {
        "uz": "{name}: \"{task}\" hali tugamagan. Tugashini kutasizmi yoki uni to'xtatasizmi? To'xtatilsa, uning "
        "natijasi saqlanmaydi.",
        "en": "{name}: \"{task}\" has not finished yet. Wait for it, or stop it? If it is stopped, its result is not "
        "kept.",
    },
    "gui.edit.ask.readers": {
        "uz": "{count} ta vazifa hali {name} faylini o'qiyapti ({tasks}). Fayl hozir saqlansa, ular xato bilan "
        "tugaydi. Ular tugashini kutib, keyin saqlansinmi?",
        "en": "{count} tasks are still reading {name} ({tasks}). If the file is saved now, they fail. Wait for them "
        "to finish and save then?",
    },
    "gui.edit.button.wait": {"uz": "Kutish", "en": "Wait"},
    "gui.edit.button.stop": {"uz": "To'xtatish", "en": "Stop"},
    "gui.edit.button.wait_save": {"uz": "Kutib, keyin saqlash", "en": "Wait, then save"},
    "gui.edit.button.stop_save": {"uz": "Ularni to'xtatib saqlash", "en": "Stop them and save"},
    "gui.edit.task.save_wait": {
        "uz": "Saqlash navbatda: {name} (vazifalar tugashi kutilmoqda)",
        "en": "Saving {name} after the running tasks",
    },
    "gui.edit.save_waits": {
        "uz": "Fayl {count} ta vazifa tugagach saqlanadi.",
        "en": "The file will be saved when {count} tasks have finished.",
    },
    "gui.edit.editor.invalid": {
        "uz": "Ochiq tahrirlash maydonidagi qiymat noto'g'ri: {reason} Uni tuzating yoki Esc bilan bekor qiling.",
        "en": "The value in the open edit box is not valid: {reason} Fix it or press Esc to cancel it.",
    },
    "gui.edit.editor.checking": {
        "uz": "Ochiq tahrirlash maydonidagi kalit hali tekshirilmoqda: biroz kuting va qayta urinib ko'ring.",
        "en": "The key in the open edit box is still being checked: wait a moment and try again.",
    },
    "gui.edit.editor.busy": {
        "uz": "Oldingi amal hali tugamadi: biroz kutib, qayta urinib ko'ring (ochiq tahrirlash maydonidagi qiymat "
        "yo'qolmaydi).",
        "en": "The previous action has not finished yet: try again in a moment (the value in the open edit box is "
        "kept).",
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
    # ---- gui.sessionstore ----
    "gui.sessionstore.ask": {
        "uz": "Oxirgi sessiyani tiklash? ({count} ta fayl)",
        "en": "Restore the last session? ({count} file(s))",
    },
    "gui.sessionstore.restore": {"uz": "Tiklash", "en": "Restore"},
    "gui.sessionstore.skip": {"uz": "Yo'q", "en": "No"},
    "gui.sessionstore.more": {"uz": "va yana {count} ta", "en": "and {count} more"},
    "gui.sessionstore.restored": {
        "uz": "Oxirgi sessiya tiklandi: {count} ta fayl.",
        "en": "The last session was restored: {count} file(s).",
    },
    "gui.sessionstore.missing": {
        "uz": "{count} ta fayl topilmadi va ochilmadi: {names}.",
        "en": "{count} file(s) were not found and were not opened: {names}.",
    },
    "gui.sessionstore.failed": {
        "uz": "Oxirgi sessiyani tiklab bo'lmadi: {error}",
        "en": "The last session could not be restored: {error}",
    },
    "gui.sessionstore.task.plan": {"uz": "Sessiya fayllari tekshirilmoqda", "en": "Checking the files of the session"},
    "gui.sessionstore.task.expand": {"uz": "Ochilgan tugunlar tiklanmoqda", "en": "Restoring the expanded nodes"},
    # ---- gui.workspace ----
    "gui.workspace.act.open": {"uz": "Workspace'ni ochish…", "en": "Open workspace…"},
    "gui.workspace.act.open.tip": {
        "uz": "Saqlangan ish maydonini ochadi: fayllar, ko'rinishlar, filtrlar, saralash, xatcho'plar, jadval "
              "ustunlari va tuzilma quruvchi daraxti.",
        "en": "Opens a saved workspace: files, views, filters, sort, bookmarks, table columns and the structure "
              "builder's tree.",
    },
    "gui.workspace.act.save": {"uz": "Workspace'ni saqlash…", "en": "Save workspace…"},
    "gui.workspace.act.save.tip": {
        "uz": "Ochiq fayllar va ularning holatini (ko'rinishlar, filtrlar, saralash, xatcho'plar, jadval ustunlari, "
              "tuzilma quruvchi daraxti) .jrworkspace fayliga yozadi. Saqlanmagan o'zgarishlar unga ko'chirilmaydi.",
        "en": "Writes the open files and their state (views, filters, sort, bookmarks, table columns, the structure "
              "builder's tree) to a .jrworkspace file. Unsaved changes are not copied into it.",
    },
    "gui.workspace.menu.recent": {"uz": "Oxirgi workspace'lar", "en": "Recent workspaces"},
    "gui.workspace.recent.empty": {"uz": "Ro'yxat bo'sh", "en": "No recent workspaces"},
    "gui.workspace.recent.clear": {"uz": "Ro'yxatni tozalash", "en": "Clear the list"},
    "gui.workspace.recent.missing": {
        "uz": "Workspace fayli topilmadi va ro'yxatdan olib tashlandi: {path}",
        "en": "The workspace file was not found and was removed from the list: {path}",
    },
    "gui.workspace.filter": {"uz": "Workspace fayllari ({patterns})", "en": "Workspace files ({patterns})"},
    "gui.workspace.open.title": {"uz": "Workspace'ni ochish", "en": "Open workspace"},
    "gui.workspace.save.title": {"uz": "Workspace'ni saqlash", "en": "Save workspace"},
    "gui.workspace.ask.title": {"uz": "Ochiq fayllar", "en": "Open files"},
    "gui.workspace.ask": {
        "uz": "Hozir {count} ta fayl ochiq. Workspace ochilishidan oldin ular yopilsinmi yoki workspace fayllari "
              "ularga qo'shilsinmi? (Saqlanmagan o'zgarishlar yopishda har bir fayl uchun alohida so'raladi.)",
        "en": "{count} file(s) are open now. Close them before the workspace opens, or add the workspace's files to "
              "them? (Unsaved changes are asked about file by file when closing.)",
    },
    "gui.workspace.ask.replace": {"uz": "Yopib ochish", "en": "Close them"},
    "gui.workspace.ask.add": {"uz": "Qo'shish", "en": "Add"},
    "gui.workspace.opened": {
        "uz": "Workspace ochildi: {name} ({count} ta fayl).",
        "en": "Workspace opened: {name} ({count} file(s)).",
    },
    "gui.workspace.saved": {"uz": "Workspace saqlandi: {path}", "en": "Workspace saved: {path}"},
    "gui.workspace.empty": {"uz": "Saqlash uchun ochiq fayl yo'q.", "en": "No file is open to save."},
    "gui.workspace.pending_edits": {
        "uz": "{count} ta faylda saqlanmagan o'zgarishlar bor edi: ular workspace'ga ko'chirilmagan; fayl ochilgach "
              "tiklash paneli ularni taklif qiladi.",
        "en": "{count} file(s) had unsaved changes: they are not part of the workspace; the recovery bar offers them "
              "once the file is open.",
    },
    "gui.workspace.task.read": {"uz": "Workspace o'qilmoqda: {name}", "en": "Reading the workspace {name}"},
    "gui.workspace.task.save": {"uz": "Workspace saqlanmoqda: {name}", "en": "Saving the workspace {name}"},
    "gui.workspace.task.mapping": {"uz": "Tuzilma tiklanmoqda: {name}", "en": "Restoring the structure of {name}"},
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
    "gui.actions.no_document": {"uz": "Hujjat ochiq emas.", "en": "The document is not open."},
    "gui.actions.stats.need_record": {
        "uz": "Statistika yozuvlar ichidagi maydonlar uchun (tashqi o'rama uchun emas).",
        "en": "Statistics are for fields inside the records (not for the wrapper).",
    },
    "gui.actions.tools.unavailable": {"uz": "Bu yerda mavjud emas.", "en": "Not available here."},
    "gui.actions.edit.value": {"uz": "Qiymatni tahrirlash", "en": "Edit value"},
    "gui.actions.edit.json": {"uz": "JSON matn sifatida tahrirlash…", "en": "Edit as JSON text…"},
    "gui.actions.edit.rename": {"uz": "Kalitni qayta nomlash", "en": "Rename key"},
    "gui.actions.edit.insert": {"uz": "Qo'shish", "en": "Insert"},
    "gui.actions.edit.insert_child": {"uz": "Ichiga (bola sifatida)…", "en": "Inside (as a child)…"},
    "gui.actions.edit.insert_before": {"uz": "Oldiga…", "en": "Before…"},
    "gui.actions.edit.insert_after": {"uz": "Ortiga…", "en": "After…"},
    "gui.actions.edit.duplicate": {"uz": "Nusxasini qo'shish", "en": "Duplicate"},
    "gui.actions.edit.delete": {"uz": "O'chirish", "en": "Delete"},
    "gui.actions.edit.move_up": {"uz": "Yuqoriga surish", "en": "Move up"},
    "gui.actions.edit.move_down": {"uz": "Pastga surish", "en": "Move down"},
    "gui.actions.edit.paste": {"uz": "Buferdagi JSON'ni qo'yish", "en": "Paste JSON from the clipboard"},
    "gui.actions.edit.change_type": {"uz": "Turini o'zgartirish", "en": "Change type"},
    "gui.actions.edit.type.string": {"uz": "Satr", "en": "String"},
    "gui.actions.edit.type.number": {"uz": "Son", "en": "Number"},
    "gui.actions.edit.type.bool": {"uz": "Mantiqiy (true/false)", "en": "Boolean (true/false)"},
    "gui.actions.edit.type.null": {"uz": "null", "en": "null"},
    "gui.actions.edit.type.object": {"uz": "Obyekt", "en": "Object"},
    "gui.actions.edit.type.array": {"uz": "Massiv", "en": "Array"},
    "gui.actions.edit.type.same": {"uz": "Tugun allaqachon shu turda.", "en": "The node already has this type."},
    "gui.actions.edit.no_editing": {
        "uz": "Bu hujjatni hozir tahrirlab bo'lmaydi.",
        "en": "This document cannot be edited right now.",
    },
    "gui.actions.edit.busy": {
        "uz": "Oldingi amal hali bajarilmoqda, biroz kuting.",
        "en": "The previous change is still being made, please wait a moment.",
    },
    "gui.actions.edit.not_here": {
        "uz": "Bu amal shu ko'rinishda mavjud emas.",
        "en": "This command is not available in this view.",
    },
    "gui.actions.edit.no_key_element": {
        "uz": "Massiv elementining kaliti yo'q (u faqat o'rni bilan aniqlanadi).",
        "en": "An array element has no key (only its position).",
    },
    "gui.actions.edit.no_key_record": {
        "uz": "Bu hujjatda yozuvlarning kaliti yo'q (ular faqat raqami bilan aniqlanadi).",
        "en": "The records of this document have no key (only their number).",
    },
    "gui.actions.edit.no_key_root": {"uz": "Ildiz tugunning kaliti yo'q.", "en": "The root node has no key."},
    "gui.actions.edit.need_container": {
        "uz": "Bola faqat obyekt yoki massivga qo'shiladi.",
        "en": "Children can only be added to an object or an array.",
    },
    "gui.actions.edit.root_no_siblings": {
        "uz": "Ildiz tugunning yonida boshqa tugun bo'lmaydi.",
        "en": "The root node cannot have neighbours.",
    },
    "gui.actions.edit.reason.indexing": {
        "uz": "Fayl hali indekslanmoqda; tugagach tahrirlash mumkin bo'ladi.",
        "en": "The file is still being indexed; editing becomes possible when it is done.",
    },
    "gui.actions.edit.reason.partial": {
        "uz": "Faylda JSON xatosi bor, shuning uchun u faqat ko'rish uchun ochilgan.",
        "en": "The file has a JSON error, so it is open for viewing only.",
    },
    "gui.actions.edit.reason.locked": {
        "uz": "Bu faylni boshqa oyna yoki dastur nusxasi tahrirlamoqda.",
        "en": "Another tab or another copy of the program is editing this file.",
    },
    "gui.actions.edit.reason.huge_single": {
        "uz": "Fayl bitta juda katta qiymatdan iborat; uni tahrirlab bo'lmaydi.",
        "en": "The file is one huge value; it cannot be edited.",
    },
    "gui.actions.edit.reason.recovery_pending": {
        "uz": "Avval topilgan saqlanmagan o'zgarishlar haqida qaror qiling (tiklash yoki o'chirish).",
        "en": "First decide about the unsaved changes that were found (restore or discard them).",
    },
    "gui.actions.edit.reason.file_changed": {
        "uz": "Fayl tashqaridan o'zgartirildi; tahrirlash uchun uni qayta oching.",
        "en": "The file was changed by another program; reopen it to edit.",
    },
    "gui.actions.edit.reason.file_grown": {
        "uz": "Fayl ochilgandan keyin o'sdi; tahrirlash uchun uni qayta oching.",
        "en": "The file grew after it was opened; reopen it to edit.",
    },
    "gui.actions.edit.reason.saving": {"uz": "Fayl saqlanmoqda.", "en": "The file is being saved."},
    "gui.actions.edit.reason.applying": {
        "uz": "O'zgarish qo'llanmoqda.",
        "en": "A change is being applied.",
    },
    "gui.actions.edit.reason.closed": {"uz": "Hujjat yopilgan.", "en": "The document is closed."},
    "gui.actions.edit.reason.unavailable": {
        "uz": "Tahrirlashni tayyorlab bo'lmadi (tafsilotlar xato oynasida va logda).",
        "en": "Editing could not be set up (the details are in the error window and the log).",
    },
    "gui.actions.edit.reason.other": {
        "uz": "Tahrirlash hozir mumkin emas ({reason}).",
        "en": "Editing is not possible right now ({reason}).",
    },
    # ---- gui.widgets ----
    "gui.widgets.edit.unavailable": {
        "uz": "Hozir tahrirlab bo'lmaydi.",
        "en": "Editing is not possible right now.",
    },
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
    # ---- gui.smart ----
    # smart values in the views (tree / table cells, the full-value window): links, thumbnails
    "gui.smart.open.title": {"uz": "Havolani ochish", "en": "Open the link"},
    "gui.smart.open.question": {
        "uz": "Brauzerda ochilsinmi?\n\n{url}\n\nHavolani tizimdagi brauzer ochadi; dasturning o'zi hech narsa "
              "yubormaydi.",
        "en": "Open in the browser?\n\n{url}\n\nThe system browser opens the link; the program itself sends "
              "nothing.",
    },
    "gui.smart.open.mail": {
        "uz": "Pochta dasturida ochilsinmi?\n\n{url}\n\nManzilni tizimdagi pochta dasturi ochadi; dasturning o'zi "
              "hech narsa yubormaydi.",
        "en": "Open in the mail program?\n\n{url}\n\nThe system's mail program opens the address; the program "
              "itself sends nothing.",
    },
    "gui.smart.open.button": {"uz": "Brauzerda ochish…", "en": "Open in the browser…"},
    "gui.smart.open.mail_button": {"uz": "Pochta dasturida ochish…", "en": "Open in the mail program…"},
    "gui.smart.open.link_tip": {
        "uz": "Bosilsa, avval so'raladi, keyin tizimdagi dasturda ochiladi",
        "en": "Click: you are asked first, then it opens in the system's program",
    },
    "gui.smart.link.url": {"uz": "Havola:", "en": "Link:"},
    "gui.smart.link.email": {"uz": "E-pochta manzili:", "en": "E-mail address:"},
    "gui.smart.thumb.truncated": {
        "uz": "Qiymat to'liq yuklanmagan, shuning uchun rasmning kichik nusxasi ko'rsatilmaydi.",
        "en": "The value was not loaded in full, so no small copy of the image is shown.",
    },
    "gui.smart.thumb.too_large": {
        "uz": "Rasm juda katta ({width}×{height} piksel): kichik nusxasi ko'rsatilmaydi.",
        "en": "The image is too large ({width}×{height} pixels): no small copy is shown.",
    },
    "gui.smart.thumb.failed": {
        "uz": "Rasmni o'qib bo'lmadi: kichik nusxasi ko'rsatilmaydi.",
        "en": "The image could not be read: no small copy is shown.",
    },
    # ---- gui.editors ----
    # editing widgets and dialogs (gui/editors.py): inline editor, text / JSON / key / insert dialogs, type menu
    "gui.editors.error.number": {
        "uz": "Bu JSON soni emas. Masalan: 12, -3.5, 0.1, 1e3 (oldida + yoki ortiqcha 0 bo'lmaydi, kasr nuqta bilan "
              "yoziladi).",
        "en": "This is not a JSON number. For example: 12, -3.5, 0.1, 1e3 (no leading + or extra 0; decimals use a "
              "dot).",
    },
    "gui.editors.error.key_empty": {"uz": "Kalit bo'sh bo'lishi mumkin emas.", "en": "The key cannot be empty."},
    "gui.editors.error.key_exists": {
        "uz": "Bu obyektda «{key}» kaliti allaqachon bor.",
        "en": "This object already has the key \"{key}\".",
    },
    "gui.editors.error.key_required": {"uz": "Kalitni kiriting.", "en": "Enter a key."},
    "gui.editors.error.escaped": {
        "uz": "Kodli yozuv noto'g'ri ({column}-belgi): {reason}.",
        "en": "The escaped text is not valid (character {column}): {reason}.",
    },
    "gui.editors.error.quote": {
        "uz": "qo'shtirnoq \\\" ko'rinishida yozilishi kerak",
        "en": "a quote must be written as \\\"",
    },
    "gui.editors.error.key_invalid": {"uz": "Kalit noto'g'ri: {reason}", "en": "The key is not valid: {reason}"},
    "gui.editors.error.insert_invalid": {"uz": "Qo'shib bo'lmaydi: {reason}", "en": "Cannot insert: {reason}"},
    "gui.editors.error.too_long": {
        "uz": "Matn juda uzun ({size}; bu oynada ko'pi bilan {limit}).",
        "en": "The text is too long ({size}; at most {limit} in this window).",
    },
    "gui.editors.escaped.tip": {
        "uz": "Maxsus belgilar kod bilan ko'rsatilgan: \\n — yangi qator, \\r — qator boshi, \\t — tabulyatsiya, "
              "\\\" — qo'shtirnoq, \\\\ — teskari chiziq, \\uXXXX — boshqa belgilar.",
        "en": "Special characters are shown as escapes: \\n line break, \\r carriage return, \\t tab, \\\" quote, "
              "\\\\ backslash, \\uXXXX other characters.",
    },
    "gui.editors.special.mixed_newlines": {
        "uz": "har xil qator tugashlari (\\r\\n va \\n)",
        "en": "mixed line endings (\\r\\n and \\n)",
    },
    "gui.editors.special.surrogates": {
        "uz": "juftsiz surrogat belgilar (\\ud800…\\udfff)",
        "en": "unpaired surrogate characters (\\ud800…\\udfff)",
    },
    "gui.editors.special.separators": {
        "uz": "maxsus ajratuvchi belgilar (U+2029, U+FDD0, U+FDD1)",
        "en": "special separator characters (U+2029, U+FDD0, U+FDD1)",
    },
    "gui.editors.loading": {"uz": "Qiymat yuklanmoqda…", "en": "Loading the value…"},
    "gui.editors.load_failed": {
        "uz": "Qiymatni yuklab bo'lmadi: {error}",
        "en": "The value could not be loaded: {error}",
    },
    "gui.editors.not_string": {"uz": "Bu tugun matn (string) emas.", "en": "This node is not a string."},
    "gui.editors.discard.question": {
        "uz": "Kiritilgan o'zgarishlar saqlanmaydi. Oynani yopaymi?",
        "en": "The changes you made will be lost. Close the window?",
    },
    "gui.editors.task.load": {"uz": "Tahrirlash uchun qiymat yuklanmoqda", "en": "Loading a value to edit"},
    "gui.editors.task.validate": {"uz": "JSON tekshirilmoqda", "en": "Checking the JSON"},
    "gui.editors.task.format": {"uz": "JSON formatlanmoqda", "en": "Formatting the JSON"},
    # string dialog
    "gui.editors.string.title": {"uz": "Matnni tahrirlash", "en": "Edit text"},
    "gui.editors.string.wrap": {"uz": "Qatorlarni o'rash", "en": "Wrap lines"},
    "gui.editors.string.escapes": {
        "uz": "Maxsus belgilarni kod bilan ko'rsatish",
        "en": "Show special characters as escapes",
    },
    "gui.editors.string.count": {"uz": "{count} ta belgi, {lines} qator", "en": "{count} characters, {lines} lines"},
    "gui.editors.string.count_invalid": {
        "uz": "Belgilar soni: kodli yozuv tuzatilgach ko'rsatiladi",
        "en": "Characters: shown once the escaped text is valid",
    },
    "gui.editors.string.too_large": {
        "uz": "Bu matn juda katta ({size}): bu oynada {limit} gacha bo'lgan matnlarni tahrirlash mumkin.",
        "en": "This text is too large ({size}): this window edits texts up to {limit}.",
    },
    "gui.editors.string.too_large.hint": {
        "uz": "Katta matnlarni «Ommaviy amallar» (Ctrl+H) dagi «Topish va almashtirish» bilan yoki JSON Patch import "
              "qilib o'zgartiring.",
        "en": "Change large texts with Find and replace in Bulk operations (Ctrl+H) or by importing a JSON Patch.",
    },
    "gui.editors.string.forced_escapes": {
        "uz": "Bu matnda oddiy tahrirlash maydoni aynan saqlay olmaydigan belgilar bor: {what}. Shuning uchun matn "
              "kod bilan ko'rsatilgan.",
        "en": "This text has characters the plain editor cannot keep exactly: {what}. So it is shown with escapes.",
    },
    "gui.editors.string.plain_refused": {
        "uz": "Oddiy ko'rinishga o'tib bo'lmaydi: matnda {what} bor.",
        "en": "Cannot switch to plain text: the text has {what}.",
    },
    # JSON dialog
    "gui.editors.json.title": {"uz": "JSON sifatida tahrirlash", "en": "Edit as JSON"},
    "gui.editors.json.valid": {"uz": "✓ To'g'ri JSON: {type}", "en": "✓ Valid JSON: {type}"},
    "gui.editors.json.invalid": {
        "uz": "✗ {line}-qator, {column}-ustun: {reason}",
        "en": "✗ Line {line}, column {column}: {reason}",
    },
    "gui.editors.json.checking": {"uz": "Tekshirilmoqda…", "en": "Checking…"},
    "gui.editors.json.indent": {"uz": "Chekinish:", "en": "Indent:"},
    "gui.editors.json.indent.2": {"uz": "2 ta bo'sh joy", "en": "2 spaces"},
    "gui.editors.json.indent.4": {"uz": "4 ta bo'sh joy", "en": "4 spaces"},
    "gui.editors.json.indent.tab": {"uz": "Tabulyatsiya", "en": "Tab"},
    "gui.editors.json.format": {"uz": "Formatlash", "en": "Format"},
    "gui.editors.json.format.tip": {
        "uz": "Matnni tanlangan chekinish bilan qayta joylashtiradi; qiymatlar (sonlar, matnlar) aynan saqlanadi.",
        "en": "Re-indents the text with the chosen indent; values (numbers, strings) stay exactly as written.",
    },
    "gui.editors.json.ok.tip": {
        "uz": "Saqlash (Ctrl+Enter). Faqat JSON to'g'ri bo'lganda ishlaydi.",
        "en": "Save (Ctrl+Enter). Available only while the JSON is valid.",
    },
    "gui.editors.json.too_large": {
        "uz": "Bu tugun juda katta ({size}): bu oynada {limit} gacha bo'lgan JSON matnini tahrirlash mumkin.",
        "en": "This node is too large ({size}): this window edits JSON texts up to {limit}.",
    },
    "gui.editors.json.too_large.hint": {
        "uz": "Katta tugunlarni JSON Patch yoki «Ommaviy amallar» (Ctrl+H) bilan o'zgartiring.",
        "en": "Change large nodes with a JSON Patch or with Bulk operations (Ctrl+H).",
    },
    # key and insert dialogs
    "gui.editors.key.title": {"uz": "Kalitni qayta nomlash", "en": "Rename key"},
    "gui.editors.key.label": {"uz": "Yangi kalit:", "en": "New key:"},
    "gui.editors.key.checking": {
        "uz": "Bunday kalit bor-yo'qligi tekshirilmoqda…",
        "en": "Checking whether this key exists already…",
    },
    "gui.editors.insert.title": {"uz": "Qo'shish", "en": "Insert"},
    "gui.editors.insert.position": {"uz": "Qayerga:", "en": "Where:"},
    "gui.editors.insert.before": {"uz": "Oldiga", "en": "Before"},
    "gui.editors.insert.after": {"uz": "Ortiga", "en": "After"},
    "gui.editors.insert.child": {"uz": "Ichiga (oxiriga)", "en": "Inside (at the end)"},
    "gui.editors.insert.key": {"uz": "Kalit:", "en": "Key:"},
    "gui.editors.insert.key.placeholder": {"uz": "yangi a'zoning kaliti", "en": "the key of the new member"},
    "gui.editors.insert.type": {"uz": "Turi:", "en": "Type:"},
    "gui.editors.insert.value": {"uz": "Qiymat:", "en": "Value:"},
    "gui.editors.insert.null": {"uz": "null qiymati", "en": "the null value"},
    "gui.editors.insert.object": {"uz": "Bo'sh obyekt {}", "en": "An empty object {}"},
    "gui.editors.insert.array": {"uz": "Bo'sh massiv []", "en": "An empty array []"},
    # type names (menus, combos)
    "gui.editors.type.menu": {"uz": "Turini o'zgartirish", "en": "Change type"},
    "gui.editors.type.string": {"uz": "Satr", "en": "String"},
    "gui.editors.type.number": {"uz": "Son", "en": "Number"},
    "gui.editors.type.bool": {"uz": "Mantiqiy (true/false)", "en": "Boolean (true/false)"},
    "gui.editors.type.null": {"uz": "null", "en": "null"},
    "gui.editors.type.object": {"uz": "Obyekt {…}", "en": "Object {…}"},
    "gui.editors.type.array": {"uz": "Massiv […]", "en": "Array […]"},
    "gui.editors.type.json": {"uz": "JSON matni", "en": "JSON text"},
    "gui.editors.type.same": {"uz": "Qiymat allaqachon shu turda.", "en": "The value already has this type."},
    "gui.editors.type.impossible": {
        "uz": "Turni o'zgartirib bo'lmaydi: {source} qiymati {target} turiga aylanmaydi.",
        "en": "A {source} value cannot be converted to {target}.",
    },
    # ---- gui.bulk ----
    # the bulk operations dialog (gui/bulkdialog.py)
    "gui.bulk.title": {"uz": "Ommaviy amallar — {name}", "en": "Bulk operations — {name}"},
    "gui.bulk.operation": {"uz": "Amal:", "en": "Operation:"},
    "gui.bulk.params": {"uz": "Parametrlar", "en": "Parameters"},
    "gui.bulk.scope": {"uz": "Qaysi yozuvlarda", "en": "Records"},
    "gui.bulk.scope.all": {"uz": "Barcha yozuvlar ({count})", "en": "All records ({count})"},
    "gui.bulk.scope.view": {"uz": "Joriy ko'rinish ({count})", "en": "Current view ({count})"},
    "gui.bulk.scope.view_base": {
        "uz": "Joriy ko'rinish (filtr yo'q — barcha yozuvlar)",
        "en": "Current view (no filter: all records)",
    },
    "gui.bulk.scope.selected": {"uz": "Tanlangan yozuvlar ({count})", "en": "Selected records ({count})"},
    "gui.bulk.scope.selected_none": {
        "uz": "Tanlangan yozuvlar (hech narsa tanlanmagan)",
        "en": "Selected records (nothing selected)",
    },
    "gui.bulk.scope.selected.tip": {
        "uz": "Jadval yoki daraxtda yozuvlarni tanlang (bir nechtasini Ctrl yoki Shift bilan).",
        "en": "Select records in the table or the tree (several with Ctrl or Shift).",
    },
    "gui.bulk.scope.view_busy": {
        "uz": "Joriy ko'rinish hali hisoblanmoqda: filtr yoki saralash tugashini kuting.",
        "en": "The current view is still being computed: wait for the filter or sort to finish.",
    },
    "gui.bulk.scope.no_selection": {"uz": "Hech qanday yozuv tanlanmagan.", "en": "No records are selected."},
    "gui.bulk.preview": {"uz": "Ko'rib chiqish", "en": "Preview"},
    "gui.bulk.preview.tip": {
        "uz": "Hech narsani o'zgartirmasdan natijani hisoblaydi: nechta yozuv va qiymat o'zgarishini va misollarni "
              "ko'rsatadi.",
        "en": "Computes the result without changing anything: shows how many records and values would change, with "
              "examples.",
    },
    "gui.bulk.stop": {"uz": "To'xtatish", "en": "Stop"},
    "gui.bulk.apply": {"uz": "Qo'llash", "en": "Apply"},
    "gui.bulk.apply.tip": {
        "uz": "Ko'rib chiqilgan o'zgarishlarni bitta qadam sifatida qo'llaydi (Ctrl+Z bilan qaytarish mumkin).",
        "en": "Applies the previewed changes as one step (Ctrl+Z undoes it).",
    },
    "gui.bulk.close.tip": {
        "uz": "Oynani yopadi; qo'llanmagan ko'rib chiqish natijasi o'chiriladi.",
        "en": "Closes the window; a preview that was not applied is discarded.",
    },
    "gui.bulk.param.find": {"uz": "Topish:", "en": "Find:"},
    "gui.bulk.param.replacement": {"uz": "O'rniga:", "en": "Replace with:"},
    "gui.bulk.param.regex": {"uz": "Muntazam ifoda (regex)", "en": "Regular expression"},
    "gui.bulk.param.case_sensitive": {"uz": "Katta-kichik harfni farqlash", "en": "Match case"},
    "gui.bulk.param.whole_word": {"uz": "Butun so'z", "en": "Whole word"},
    "gui.bulk.param.path": {"uz": "Yo'l:", "en": "Path:"},
    "gui.bulk.param.path_optional": {
        "uz": "ixtiyoriy: masalan address.city yoki ..name (bo'sh — hamma joyda)",
        "en": "optional: e.g. address.city or ..name (empty: everywhere)",
    },
    "gui.bulk.param.path_required": {
        "uz": "masalan: address.city, items[*].price yoki ..id",
        "en": "e.g. address.city, items[*].price or ..id",
    },
    "gui.bulk.param.objects_path": {
        "uz": "ixtiyoriy: obyektlar yo'li, masalan items[*] (bo'sh — hamma obyektlar)",
        "en": "optional: the path of the objects, e.g. items[*] (empty: every object)",
    },
    "gui.bulk.param.old": {"uz": "Eski kalit:", "en": "Old key:"},
    "gui.bulk.param.new": {"uz": "Yangi kalit:", "en": "New key:"},
    "gui.bulk.param.value": {"uz": "Qiymat (JSON):", "en": "Value (JSON):"},
    "gui.bulk.param.value.placeholder": {
        "uz": "masalan: \"Toshkent\", 42, true, null yoki {\"a\": 1}",
        "en": "e.g. \"Tashkent\", 42, true, null or {\"a\": 1}",
    },
    "gui.bulk.param.mode": {"uz": "Qachon:", "en": "When:"},
    "gui.bulk.param.trim": {"uz": "Chetdagi bo'sh joylarni olib tashlash", "en": "Trim spaces at both ends"},
    "gui.bulk.param.collapse": {
        "uz": "Ketma-ket bo'sh joylarni bittaga yig'ish",
        "en": "Collapse runs of spaces into one",
    },
    "gui.bulk.param.to": {"uz": "Yangi tur:", "en": "New type:"},
    "gui.bulk.param.key": {"uz": "Kalit maydon:", "en": "Key field:"},
    "gui.bulk.param.key.placeholder": {
        "uz": "masalan: id yoki email; yozuv kaliti uchun $key",
        "en": "e.g. id or email; $key for the record key",
    },
    "gui.bulk.param.keep": {"uz": "Qaysi biri qoladi:", "en": "Keep:"},
    "gui.bulk.param.path.help": {
        "uz": "Yo'llar: a.b — ichma-ich maydon, items[*] — massivning har bir elementi, ..id — istalgan chuqurlikdagi "
              "id.",
        "en": "Paths: a.b a nested field, items[*] every element of an array, ..id an id at any depth.",
    },
    "gui.bulk.result.title": {"uz": "Natija", "en": "Result"},
    "gui.bulk.result.counts": {
        "uz": "{scanned} ta yozuv ko'rildi · {records} ta yozuv o'zgaradi · {values} ta qiymat",
        "en": "{scanned} records scanned · {records} records would change · {values} values",
    },
    "gui.bulk.result.counts_dedupe": {
        "uz": "{scanned} ta yozuv ko'rildi · {records} ta takror yozuv o'chiriladi",
        "en": "{scanned} records scanned · {records} duplicate records would be deleted",
    },
    "gui.bulk.result.conflicts": {"uz": "{count} ta ziddiyat", "en": "conflicts: {count}"},
    "gui.bulk.result.failures": {"uz": "{count} ta xato", "en": "failures: {count}"},
    "gui.bulk.result.huge": {
        "uz": "{count} ta juda katta yozuv o'tkazib yuborildi",
        "en": "very large records skipped: {count}",
    },
    "gui.bulk.result.detail": {"uz": "Yangi ma'lumot: {size} · vaqt: {elapsed}", "en": "New data: {size} · time: {elapsed}"},
    "gui.bulk.result.nothing": {"uz": "Hech narsa o'zgarmaydi.", "en": "Nothing would change."},
    "gui.bulk.examples": {"uz": "Misollar (oldin → keyin)", "en": "Examples (before → after)"},
    "gui.bulk.problems": {"uz": "Muammolar", "en": "Problems"},
    "gui.bulk.col.record": {"uz": "Yozuv", "en": "Record"},
    "gui.bulk.col.path": {"uz": "Yo'l", "en": "Path"},
    "gui.bulk.col.before": {"uz": "Oldin", "en": "Before"},
    "gui.bulk.col.after": {"uz": "Keyin", "en": "After"},
    "gui.bulk.col.value": {"uz": "Qiymat", "en": "Value"},
    "gui.bulk.col.reason": {"uz": "Sabab", "en": "Reason"},
    "gui.bulk.example.reveal.tip": {
        "uz": "Ikki marta bosing: yozuvni daraxtda ko'rsatadi",
        "en": "Double-click to show the record in the tree",
    },
    "gui.bulk.status.running": {"uz": "Ko'rib chiqilmoqda…", "en": "Previewing…"},
    "gui.bulk.status.done": {"uz": "Ko'rib chiqildi: {label}", "en": "Preview ready: {label}"},
    "gui.bulk.status.failed": {"uz": "Ko'rib chiqib bo'lmadi: {error}", "en": "The preview failed: {error}"},
    "gui.bulk.status.cancelled": {"uz": "Ko'rib chiqish to'xtatildi.", "en": "The preview was stopped."},
    "gui.bulk.status.stale": {
        "uz": "Ko'rib chiqishdan keyin ma'lumot o'zgardi: qo'llash uchun amalni qaytadan ko'rib chiqing.",
        "en": "The data changed after the preview: preview the operation again to apply it.",
    },
    "gui.bulk.status.applying": {"uz": "Qo'llanmoqda…", "en": "Applying…"},
    "gui.bulk.status.applied": {"uz": "Qo'llandi: {label}", "en": "Applied: {label}"},
    "gui.bulk.status.not_applied": {"uz": "O'zgarishlar qo'llanmadi.", "en": "The changes were not applied."},
    "gui.bulk.status.busy": {
        "uz": "Boshqa tahrir bajarilmoqda: u tugashini kuting.",
        "en": "Another edit is in progress: wait for it to finish.",
    },
    "gui.bulk.unavailable": {"uz": "Hozir tahrirlab bo'lmaydi.", "en": "Editing is not possible right now."},
    "gui.bulk.huge_note": {
        "uz": "Diqqat: {limit} dan katta yozuvlar ommaviy amallarda o'zgartirilmaydi, ular «o'tkazib yuborildi» deb "
              "sanaladi.",
        "en": "Note: records larger than {limit} are not changed by bulk operations; they are counted as skipped.",
    },
    "gui.bulk.task.huge": {"uz": "Juda katta yozuvlar tekshirilmoqda", "en": "Checking for very large records"},
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
    # gui.tree: editing (§12.9)
    "gui.tree.edited": {"uz": "tahrirlangan", "en": "edited"},
    "gui.tree.edited.tip": {
        "uz": "Bu tugun tahrirlangan va hali saqlanmagan, shuning uchun faylda o'rni yo'q.",
        "en": "This node was edited and is not saved yet, so it has no position in the file.",
    },
    "gui.tree.size.tip.edited": {
        "uz": "{bytes} bayt\nTahrirlangan: faylda hali o'rni yo'q",
        "en": "{bytes} bytes\nEdited: no position in the file yet",
    },
    "gui.tree.edit.unavailable": {"uz": "Hozir tahrirlab bo'lmaydi", "en": "Editing is not possible right now"},
    "gui.tree.edit.no_node": {
        "uz": "Avval tugunni tanlang (yoki u yuklanishini kuting)",
        "en": "Select a node first (or wait until it is loaded)",
    },
    "gui.tree.edit.records_locked": {
        "uz": "Yozuvlarni saqlovchi massiv va uni o'rab turgan obyekt butunligicha almashtirilmaydi: "
              "yozuvlarni alohida tahrirlang.",
        "en": "The array holding the records and the object around it cannot be replaced as a whole: "
              "edit the records one by one.",
    },
    "gui.tree.edit.too_large": {
        "uz": "Bu qiymat tahrirlash oynasi uchun juda katta ({size}). Ommaviy almashtirish yoki JSON Patch'dan "
              "foydalaning.",
        "en": "This value is too large for the editor ({size}). Use Bulk replace or a JSON Patch.",
    },
    "gui.tree.edit.title.value": {"uz": "{path} — qiymatni tahrirlash", "en": "{path} — edit the value"},
    "gui.tree.edit.title.json": {"uz": "{path} — JSON sifatida tahrirlash", "en": "{path} — edit as JSON"},
    "gui.tree.edit.title.key": {"uz": "{path} — kalitni qayta nomlash", "en": "{path} — rename the key"},
    "gui.tree.edit.title.insert": {"uz": "{path} — yangi tugun qo'shish", "en": "{path} — insert a node"},
    "gui.tree.edit.no_key": {
        "uz": "Bu tugunning kaliti yo'q (massiv elementi yoki ildiz)",
        "en": "This node has no key (an array element or the root)",
    },
    "gui.tree.edit.insert.child": {"uz": "Ichiga, oxiriga qo'shish…", "en": "Insert as a child (at the end)…"},
    "gui.tree.edit.insert.before": {"uz": "Oldiga qo'shish…", "en": "Insert before…"},
    "gui.tree.edit.insert.after": {"uz": "Ortiga qo'shish…", "en": "Insert after…"},
    "gui.tree.edit.insert.record_before": {"uz": "Oldiga yangi yozuv…", "en": "New record before…"},
    "gui.tree.edit.insert.record_after": {"uz": "Ortiga yangi yozuv…", "en": "New record after…"},
    "gui.tree.edit.need_container": {
        "uz": "Bola faqat obyekt yoki massivga qo'shiladi",
        "en": "Children can only be added to objects and arrays",
    },
    "gui.tree.edit.root_no_siblings": {
        "uz": "Ildiz tugunning yonida boshqa tugun bo'lmaydi",
        "en": "The root node has no siblings",
    },
    "gui.tree.edit.too_many": {
        "uz": "Bitta buyruq ko'pi bilan {limit} ta tugunni qabul qiladi. Ko'proq tugun uchun Ommaviy amallardan "
              "foydalaning (Ctrl+H).",
        "en": "One command takes at most {limit} nodes. For more nodes use Bulk operations (Ctrl+H).",
    },
    "gui.tree.edit.nothing_selected": {"uz": "Hech narsa tanlanmagan", "en": "Nothing is selected"},
    "gui.tree.edit.not_siblings": {
        "uz": "Birga faqat bitta ota tugundagi qo'shni tugunlarni ko'chirish mumkin",
        "en": "Only nodes with the same parent can be moved together",
    },
    "gui.tree.edit.view_records": {
        "uz": "Yozuvlar tartibini faqat filtr va saralashsiz ko'rinishda o'zgartirish mumkin",
        "en": "Records can be reordered only in the view without a filter or sorting",
    },
    "gui.tree.edit.sorted_parent": {
        "uz": "Ota tugun saralangan holda ko'rsatilmoqda: ko'chirish uchun avval asl tartibga qayting",
        "en": "The parent is shown sorted: switch back to the original order to move its children",
    },
    "gui.tree.edit.clipboard_empty": {
        "uz": "Buferda joylashtiriladigan matn yo'q",
        "en": "The clipboard holds no text to paste",
    },
    "gui.tree.edit.task.positions": {"uz": "Tanlangan yozuvlar aniqlanmoqda", "en": "Finding the selected records"},
    "gui.tree.edit.task.refs": {"uz": "Tanlangan tugunlar o'qilmoqda", "en": "Reading the selected nodes"},
    "gui.tree.edit.gone": {
        "uz": "Tahrirlanayotgan tugun endi yo'q (u o'chirilgan yoki hujjat qayta ochilgan). O'zgarish "
              "qo'llanmadi.",
        "en": "The node being edited no longer exists (it was deleted or the document was reopened). The "
              "change was not applied.",
    },
    "gui.tree.task.key_check": {"uz": "Kalit tekshirilmoqda", "en": "Checking the key"},
    "gui.tree.edit.fixed.wrapper": {
        "uz": "O'rama obyektni o'chirib, nusxalab yoki surib bo'lmaydi: yozuvlar uning ichida. Uning boshqa "
              "a'zolarini yoki yozuvlarni tanlang.",
        "en": "The wrapper object cannot be deleted, duplicated or moved: the records are inside it. Select its "
              "other members or the records.",
    },
    "gui.tree.edit.fixed.records": {
        "uz": "Yozuvlar konteynerini o'chirib, nusxalab yoki surib bo'lmaydi: yozuvlarning o'zini tanlang.",
        "en": "The record container cannot be deleted, duplicated or moved: select the records themselves.",
    },
    "gui.tree.edit.fixed.root": {
        "uz": "Hujjat ildizini o'chirib, nusxalab yoki surib bo'lmaydi.",
        "en": "The root of the document cannot be deleted, duplicated or moved.",
    },
    "gui.tree.task.load_value": {"uz": "Qiymat tahrirlash uchun o'qilmoqda", "en": "Reading the value to edit"},
    "gui.tree.task.refresh": {"uz": "O'zgargan tugunlar qayta o'qilmoqda", "en": "Reading the changed nodes again"},
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
    "gui.table.edit.index": {
        "uz": "Raqam ustunini tahrirlab bo'lmaydi: u yozuvning o'rni.",
        "en": "The number column cannot be edited: it is the record's position.",
    },
    "gui.table.edit.loading": {"uz": "Yozuv hali yuklanmoqda.", "en": "The record is still loading."},
    "gui.table.edit.no_key": {
        "uz": "Bu hujjatning yozuvlarida kalit yo'q.",
        "en": "The records of this document have no keys.",
    },
    "gui.table.edit.invalid_column": {"uz": "Ustun yo'li noto'g'ri.", "en": "The column path is not valid."},
    "gui.table.edit.huge": {
        "uz": "Yozuv tahrirlash uchun juda katta: Ommaviy amallar yoki JSON Patch'dan foydalaning.",
        "en": "The record is too large to edit: use Bulk operations or JSON Patch.",
    },
    "gui.table.edit.wildcard_none": {
        "uz": "Bu katakda qiymat yo'q. Yangi maydonni aniq yo'lli ustunda kiriting (masalan, orders[0].id).",
        "en": "This cell has no value. Enter a new field in a column with a concrete path (e.g. orders[0].id).",
    },
    "gui.table.edit.wildcard_many": {
        "uz": "Katakda bir nechta qiymat bor: ularni Ommaviy amallar (Ctrl+H) bilan o'zgartiring.",
        "en": "The cell holds several values: change them with Bulk operations (Ctrl+H).",
    },
    "gui.table.edit.no_cell": {"uz": "Tahrirlanadigan katak tanlanmagan.", "en": "No cell to edit is selected."},
    "gui.table.edit.title": {"uz": "{path} — qiymatni tahrirlash", "en": "{path} — edit the value"},
    "gui.table.edit.gone": {
        "uz": "Tahrirlanayotgan qiymat endi yo'q (u o'chirilgan). O'zgarish saqlanmadi.",
        "en": "The value being edited no longer exists (it was deleted). The change was not applied.",
    },
    "gui.table.edit.changed": {
        "uz": "Shu orada ma'lumot o'zgardi: amalni yana bir bor bajaring.",
        "en": "The data changed meanwhile: please try again.",
    },
    "gui.table.edit.not_ready": {
        "uz": "Fayl hali to'liq o'qilmagan: tahrirlash indekslash tugagach mumkin.",
        "en": "The file has not been read completely yet: editing is possible once indexing has finished.",
    },
    "gui.table.edit.collect": {"uz": "Tanlangan maydonlar qidirilmoqda", "en": "Looking up the selected fields"},
    "gui.table.task.key_check": {"uz": "Kalit tekshirilmoqda", "en": "Checking the key"},
    "gui.table.delete.wildcard": {
        "uz": "Yulduzchali (*) ustunlardagi qiymatlar o'chirilmadi: ularni Ommaviy amallar (Ctrl+H) bilan o'chiring.",
        "en": "Values in wildcard (*) columns were not deleted: delete them with Bulk operations (Ctrl+H).",
    },
    "gui.table.delete.nothing": {
        "uz": "Tanlangan kataklarda o'chiriladigan maydon yo'q.",
        "en": "The selected cells have no field to delete.",
    },
    "gui.table.delete.too_many": {
        "uz": "Juda ko'p katak tanlangan ({count}, ko'pi bilan {limit}): maydonni Ommaviy amallar (Ctrl+H) bilan "
              "o'chiring.",
        "en": "Too many cells are selected ({count}, at most {limit}): delete the field with Bulk operations "
              "(Ctrl+H).",
    },
    "gui.table.delete.missing": {
        "uz": "Tanlangan yozuvlarda bu maydonlar yo'q.",
        "en": "The selected records do not have these fields.",
    },
    "gui.table.records.single": {
        "uz": "Bitta qiymatdan iborat hujjatda yozuv qo'shib, nusxalab yoki o'chirib bo'lmaydi.",
        "en": "A document of one value has no records to insert, duplicate or delete.",
    },
    "gui.table.records.collect": {"uz": "Yozuvlar yig'ilmoqda ({count})", "en": "Collecting the records ({count})"},
    "gui.table.records.delete_confirm": {
        "uz": "{count} ta yozuv o'chirilsinmi? (Ctrl+Z bilan qaytarish mumkin.)",
        "en": "Delete {count} records? (Ctrl+Z undoes it.)",
    },
    "gui.table.records.insert_title": {"uz": "Yangi yozuv", "en": "New record"},
    "gui.table.menu.delete_records": {"uz": "Yozuvni o'chirish", "en": "Delete record"},
    "gui.table.edit.key_no_json": {
        "uz": "Kalitni JSON sifatida tahrirlab bo'lmaydi: uni F2 bilan qayta nomlang.",
        "en": "A key cannot be edited as JSON: rename it with F2.",
    },
    "gui.table.edit.no_rename": {
        "uz": "Bu katakda qayta nomlanadigan kalit yo'q (massiv elementi yoki mavjud bo'lmagan maydon).",
        "en": "This cell has no key to rename (an array element, or a field that does not exist).",
    },
    "gui.table.edit.no_container": {
        "uz": "Bola faqat obyekt yoki massivga qo'shiladi.",
        "en": "A child can only be added to an object or an array.",
    },
    "gui.table.records.move_base_only": {
        "uz": "Yozuvlarni faqat filtrlanmagan va tartiblanmagan ko'rinishda surish mumkin.",
        "en": "Records can only be moved in the unfiltered, unsorted view.",
    },
    "gui.table.records.too_many": {
        "uz": "Juda ko'p yozuv tanlangan ({count}, ko'pi bilan {limit}).",
        "en": "Too many records are selected ({count}, at most {limit}).",
    },
    "gui.table.records.paste_empty": {"uz": "Buferda matn yo'q.", "en": "The clipboard holds no text."},
    "gui.table.records.not_in_view": {
        "uz": "Yangi yozuv joriy ko'rinishda (filtr / tartib) yo'q: uni ko'rish uchun ko'rinishni yangilang.",
        "en": "The new record is not in the current view (filter / sort): refresh the view to see it.",
    },
    "gui.table.menu.delete_records_n": {"uz": "Yozuvlarni o'chirish ({count})", "en": "Delete records ({count})"},
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
    "gui.table.menu.stats.index": {
        "uz": "Yozuv raqami ($index) maydon emas: uning statistikasi yo'q.",
        "en": "The record number ($index) is not a field: it has no statistics.",
    },
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
    "gui.text.edit_json.button": {"uz": "Tahrirlash…", "en": "Edit…"},
    "gui.text.edit_json.tip": {
        "uz": "Tugunni JSON matni sifatida tahrirlash, jonli tekshiruv bilan (Shift+Enter)",
        "en": "Edit the node as JSON text, with live validation (Shift+Enter)",
    },
    "gui.text.menu.edit_json": {"uz": "JSON sifatida tahrirlash…", "en": "Edit as JSON…"},
    "gui.text.edit_json.title": {"uz": "{path} — JSON sifatida tahrirlash", "en": "{path} — edit as JSON"},
    "gui.text.edit_json.no_node": {"uz": "Tahrirlanadigan tugun yo'q.", "en": "There is no node to edit."},
    "gui.text.edit_json.virtual": {
        "uz": "JSON Lines faylining ildizini butunicha tahrirlab bo'lmaydi: yozuvlarni alohida tahrirlang.",
        "en": "The root of a JSON Lines file cannot be edited as a whole: edit its records one by one.",
    },
    "gui.text.edit_json.too_large": {
        "uz": "Tugun matn sifatida tahrirlash uchun juda katta ({size} dan ortiq): Ommaviy amallar yoki JSON "
              "Patch'dan foydalaning.",
        "en": "The node is too large to edit as text (over {size}): use Bulk operations or JSON Patch.",
    },
    "gui.text.edit_json.json_error": {
        "uz": "Tugun ichida JSON xatosi bor: bunday faylni tahrirlab bo'lmaydi.",
        "en": "The node contains a JSON error: such a file cannot be edited.",
    },
    "gui.text.edit_json.gone": {
        "uz": "Tahrirlanayotgan tugun endi yo'q (u o'chirilgan). O'zgarish saqlanmadi.",
        "en": "The node being edited no longer exists (it was deleted). The change was not applied.",
    },
    "gui.text.edited": {"uz": "tahrirlangan", "en": "edited"},
    "gui.text.node_deleted": {
        "uz": "Bu tugun tahrir natijasida o'chirildi.",
        "en": "This node was deleted by an edit.",
    },
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
    "gui.raw.edited_note": {
        "uz": "Tanlangan tugun tahrirlangan — xom ko'rinish asl faylni ko'rsatadi, bu tugun unda yo'q.",
        "en": "The selected node was edited — the raw view shows the original file, where this node is not.",
    },
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
    "gui.schema.header.stale": {"uz": "tahrirlardan keyin yangilanmoqda…", "en": "updating after the edits…"},
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
    "gui.schema.act.mapping": {"uz": "Tuzilma quruvchi…", "en": "Structure builder…"},
    "gui.schema.act.mapping.tip": {
        "uz": "Maydonlarni sudrab yangi tuzilma yasash va butun faylni shu tuzilmada eksport qilish",
        "en": "Build a new structure by dragging the fields and export the whole file in it",
    },
    "gui.schema.why.mapping_wait": {
        "uz": "Fayl to'liq ochilib, indekslangandan keyin.",
        "en": "Once the file is open and indexed.",
    },
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
    "gui.schema.col.nulls": {"uz": "null / yo'q", "en": "null / missing"},
    "gui.schema.col.nulls.tip": {
        "uz": "null qiymatlar soni / maydon yo'q bo'lgan joylar soni (yozuvlar yoki massiv elementlari)",
        "en": "Number of null values / of the places without the field (records or array elements)",
    },
    "gui.schema.tip.nulls": {"uz": "null: {nulls} ta (qiymatlarning {pct})", "en": "null: {nulls} ({pct} of the values)"},
    "gui.schema.tip.missing": {"uz": "Yo'q: {missing} / {base} ({context})", "en": "Missing: {missing} of {base} ({context})"},
    "gui.schema.tip.basis.sample": {
        "uz": "Namuna bo'yicha: {scanned} ta yozuv (jami {total}); aniq sonlar to'liq skanerlashdan keyin.",
        "en": "From a sample of {scanned} records (of {total}); exact numbers after a full scan.",
    },
    "gui.schema.tip.basis.full": {
        "uz": "Barcha {scanned} ta yozuv bo'yicha (to'liq skanerlash).",
        "en": "From all {scanned} records (full scan).",
    },
    "gui.schema.detail.integers": {"uz": "butun {pct}", "en": "integers {pct}"},
    "gui.schema.tip.integers": {
        "uz": "  shundan butun sonlar: {count} ta ({pct})",
        "en": "  of them integers: {count} ({pct})",
    },
    "gui.schema.why.dupes_array": {
        "uz": "Massiv ichidagi maydonning bitta yozuvda bir nechta qiymati bo'lishi mumkin: u takroriylik kaliti "
              "bo'la olmaydi.",
        "en": "A field inside an array may have several values in one record: it cannot be a duplicates key.",
    },
    "gui.schema.why.dupes_pooled": {
        "uz": "Birlashtirilgan kalitlar (*) takroriylik kaliti bo'la olmaydi.",
        "en": "Pooled keys (*) cannot be a duplicates key.",
    },
    # ---- gui.statspanel ----
    # the field statistics dock (gui/statspanel.py)
    "gui.statspanel.field.label": {"uz": "Maydon:", "en": "Field:"},
    "gui.statspanel.field.name": {"uz": "Statistika maydoni", "en": "Statistics field"},
    "gui.statspanel.field.placeholder": {
        "uz": "masalan: contact.address.city, orders[*].sum, $key",
        "en": "e.g. contact.address.city, orders[*].sum, $key",
    },
    "gui.statspanel.field.tip": {
        "uz": "Statistikasi hisoblanadigan maydon (yozuv ichidagi yo'l). Ro'yxat hujjat tuzilmasidan olinadi; "
              "istalgan yo'lni yozish mumkin: [*] — massiv elementlari, * — obyektning har bir kaliti, .. — "
              "istalgan chuqurlik, $ — butun yozuv, $key — kalitli hujjatda yozuvning kaliti. Enter — hisoblash.",
        "en": "The field to compute statistics for (a path inside the record). The list comes from the document's "
              "structure; any path can be typed: [*] array elements, * every key of an object, .. any depth, $ "
              "the whole record, $key the record key of a keyed document. Enter computes.",
    },
    "gui.statspanel.field.empty": {
        "uz": "Maydon yo'lini yozing yoki ro'yxatdan tanlang.",
        "en": "Type a field path or pick one from the list.",
    },
    "gui.statspanel.scope.label": {"uz": "Doira:", "en": "Scope:"},
    "gui.statspanel.scope.name": {"uz": "Statistika doirasi", "en": "Statistics scope"},
    "gui.statspanel.scope.view": {
        "uz": "Joriy ko'rinish ({count} ta yozuv): {label}",
        "en": "Current view ({count} records): {label}",
    },
    "gui.statspanel.scope.view.one": {
        "uz": "Joriy ko'rinish ({count} ta yozuv): {label}",
        "en": "Current view ({count} record): {label}",
    },
    "gui.statspanel.scope.all": {"uz": "Barcha yozuvlar ({count} ta)", "en": "All records ({count})"},
    "gui.statspanel.scope.single": {"uz": "Butun hujjat (bitta qiymat)", "en": "The whole document (one value)"},
    "gui.statspanel.scope.tip": {
        "uz": "Joriy ko'rinish — filtr, tartib yoki qidiruv natijasi ko'rsatayotgan yozuvlar; barcha yozuvlar — "
              "fayldagi hamma yozuvlar.",
        "en": "Current view: the records the filter, sort or search result shows; all records: every record of "
              "the file.",
    },
    "gui.statspanel.compute": {"uz": "Hisoblash", "en": "Compute"},
    "gui.statspanel.compute.tip": {
        "uz": "Maydon statistikasini fon vazifasida hisoblash (Enter).",
        "en": "Compute the statistics of the field in a background task (Enter).",
    },
    "gui.statspanel.cancel": {"uz": "Bekor qilish", "en": "Cancel"},
    "gui.statspanel.cancel.tip": {
        "uz": "Hisoblashni to'xtatish (shu paytgacha olingan oraliq natija ko'rinib turadi).",
        "en": "Stop the computation (the partial result so far stays on screen).",
    },
    "gui.statspanel.exact": {"uz": "Aniq hisoblash", "en": "Exact count"},
    "gui.statspanel.exact.tip": {
        "uz": "Noyob qiymatlar juda ko'p bo'lgani uchun natija taxminiy. Aniq qayta hisoblash vaqtinchalik "
              "fayllardan foydalanadi va sekinroq ishlaydi.",
        "en": "The result is approximate because there are very many distinct values. The exact recount uses "
              "temporary files and is slower.",
    },
    "gui.statspanel.exact.tip_off": {
        "uz": "Natija taxminiy bo'lganda uni aniq qayta hisoblash mumkin.",
        "en": "An approximate result can be counted again exactly.",
    },
    "gui.statspanel.refresh": {"uz": "Yangilash", "en": "Refresh"},
    "gui.statspanel.refresh.tip": {
        "uz": "Statistikani hozirgi ma'lumot va doira bo'yicha qayta hisoblash.",
        "en": "Compute the statistics again for the current data and scope.",
    },
    "gui.statspanel.task": {"uz": "Statistika: {field} ({name})", "en": "Statistics: {field} ({name})"},
    "gui.statspanel.task.exact": {
        "uz": "Aniq statistika: {field} ({name})",
        "en": "Exact statistics: {field} ({name})",
    },
    "gui.statspanel.task.reveal": {"uz": "Qiymat daraxtda qidirilmoqda", "en": "Finding the value in the tree"},
    "gui.statspanel.status.no_document": {"uz": "Hujjat ochilmagan.", "en": "No document is open."},
    "gui.statspanel.status.not_ready": {
        "uz": "Hujjat hali o'qilmoqda (indekslanmoqda): tugagach hisoblash mumkin.",
        "en": "The document is still being read (indexed): you can compute once it is done.",
    },
    "gui.statspanel.status.view_busy": {
        "uz": "Joriy ko'rinish (filtr yoki tartib) hali hisoblanmoqda: tugashini kuting yoki «Barcha yozuvlar» "
              "doirasini tanlang.",
        "en": "The current view (filter or sort) is still being computed: wait for it or choose the scope "
              "\"All records\".",
    },
    "gui.statspanel.status.idle": {
        "uz": "Maydonni tanlang va «Hisoblash» tugmasini bosing.",
        "en": "Choose a field and click Compute.",
    },
    "gui.statspanel.status.running": {"uz": "Hisoblanmoqda…", "en": "Computing…"},
    "gui.statspanel.status.partial": {
        "uz": "Hisoblanmoqda: {done} / {total} ta yozuv ko'rildi (oraliq natija).",
        "en": "Computing: {done} of {total} records seen (partial result).",
    },
    "gui.statspanel.status.done": {"uz": "Tayyor ({seconds} s).", "en": "Done ({seconds} s)."},
    "gui.statspanel.status.cancelled": {
        "uz": "To'xtatildi: ko'rsatilgan natija to'liq emas.",
        "en": "Stopped: the result shown is not complete.",
    },
    "gui.statspanel.status.cancelled_empty": {"uz": "To'xtatildi.", "en": "Stopped."},
    "gui.statspanel.status.failed": {"uz": "Hisoblab bo'lmadi: {error}", "en": "Could not compute: {error}"},
    "gui.statspanel.status.reopened": {
        "uz": "Fayl qayta ochildi: hisoblash to'xtatildi.",
        "en": "The file was reopened: the computation was stopped.",
    },
    "gui.statspanel.stale.data": {
        "uz": "Natija eskirgan: hujjat o'zgardi. Qayta hisoblash uchun «Yangilash»ni bosing.",
        "en": "The result is out of date: the document changed. Click Refresh to compute it again.",
    },
    "gui.statspanel.stale.view": {
        "uz": "Natija eskirgan: joriy ko'rinish o'zgardi. Qayta hisoblash uchun «Yangilash»ni bosing.",
        "en": "The result is out of date: the current view changed. Click Refresh to compute it again.",
    },
    "gui.statspanel.header": {
        "uz": "{field} — {records} ta yozuv, {seconds} s",
        "en": "{field} — {records} records, {seconds} s",
    },
    "gui.statspanel.header.one": {
        "uz": "{field} — {records} ta yozuv, {seconds} s",
        "en": "{field} — {records} record, {seconds} s",
    },
    "gui.statspanel.header.partial": {
        "uz": "{field} — {done} / {total} ta yozuv ko'rildi",
        "en": "{field} — {done} of {total} records seen",
    },
    "gui.statspanel.header.scope": {"uz": "Doira: {label}", "en": "Scope: {label}"},
    "gui.statspanel.header.context": {
        "uz": "Mavjudlik har bir {context} elementi bo'yicha sanaladi ({count} ta element); filtrlar esa butun "
              "yozuvlarni tanlaydi.",
        "en": "Presence is counted per element of {context} ({count} elements); filters select whole records.",
    },
    "gui.statspanel.col.what": {"uz": "Ko'rsatkich", "en": "Measure"},
    "gui.statspanel.col.value": {"uz": "Qiymat", "en": "Value"},
    "gui.statspanel.col.share": {"uz": "Ulush", "en": "Share"},
    "gui.statspanel.col.count": {"uz": "Soni", "en": "Count"},
    "gui.statspanel.col.records": {"uz": "Yozuvlar", "en": "Records"},
    "gui.statspanel.col.rank": {"uz": "№", "en": "#"},
    "gui.statspanel.summary.name": {"uz": "Maydon statistikasi", "en": "Field statistics"},
    "gui.statspanel.top.name": {"uz": "Eng ko'p uchragan qiymatlar", "en": "Most frequent values"},
    "gui.statspanel.types.name": {"uz": "Turlar taqsimoti", "en": "Type distribution"},
    "gui.statspanel.types.tip": {"uz": "Turlar: {types}", "en": "Types: {types}"},
    "gui.statspanel.row.present": {"uz": "Mavjud", "en": "Present"},
    "gui.statspanel.row.null": {"uz": "null", "en": "null"},
    "gui.statspanel.row.missing": {"uz": "Yo'q", "en": "Missing"},
    "gui.statspanel.row.type": {"uz": "Turi: {type}", "en": "Type: {type}"},
    "gui.statspanel.row.distinct": {"uz": "Noyob qiymatlar", "en": "Distinct values"},
    "gui.statspanel.row.values": {"uz": "Qiymatlar (jami)", "en": "Values (total)"},
    "gui.statspanel.row.numbers": {"uz": "Sonlar", "en": "Numbers"},
    "gui.statspanel.row.min": {"uz": "Eng kichik son", "en": "Minimum"},
    "gui.statspanel.row.max": {"uz": "Eng katta son", "en": "Maximum"},
    "gui.statspanel.row.mean": {"uz": "O'rtacha", "en": "Mean"},
    "gui.statspanel.row.sum": {"uz": "Yig'indi", "en": "Sum"},
    "gui.statspanel.row.str_min": {"uz": "Eng qisqa satr", "en": "Shortest string"},
    "gui.statspanel.row.str_max": {"uz": "Eng uzun satr", "en": "Longest string"},
    "gui.statspanel.row.empty": {"uz": "Bo'sh satrlar", "en": "Empty strings"},
    "gui.statspanel.row.arr_min": {"uz": "Eng qisqa massiv", "en": "Shortest array"},
    "gui.statspanel.row.arr_max": {"uz": "Eng uzun massiv", "en": "Longest array"},
    "gui.statspanel.row.obj_min": {"uz": "Eng kam kalitli obyekt", "en": "Object with the fewest keys"},
    "gui.statspanel.row.obj_max": {"uz": "Eng ko'p kalitli obyekt", "en": "Object with the most keys"},
    "gui.statspanel.value.numbers": {
        "uz": "{count} ta (butun: {integers})",
        "en": "{count} (integers: {integers})",
    },
    "gui.statspanel.value.chars": {"uz": "{count} belgi", "en": "{count} characters"},
    "gui.statspanel.value.chars.one": {"uz": "{count} belgi", "en": "{count} character"},
    "gui.statspanel.value.items": {"uz": "{count} element", "en": "{count} elements"},
    "gui.statspanel.value.items.one": {"uz": "{count} element", "en": "{count} element"},
    "gui.statspanel.value.keys": {"uz": "{count} kalit", "en": "{count} keys"},
    "gui.statspanel.value.keys.one": {"uz": "{count} kalit", "en": "{count} key"},
    "gui.statspanel.distinct.exact": {"uz": "{count} (aniq)", "en": "{count} (exact)"},
    "gui.statspanel.distinct.approx": {
        "uz": "≈ {count} (±{error}; HyperLogLog, ±{rse} %)",
        "en": "≈ {count} (±{error}; HyperLogLog, ±{rse} %)",
    },
    "gui.statspanel.distinct.partial": {"uz": "hozircha {count}", "en": "{count} so far"},
    "gui.statspanel.distinct.partial_approx": {
        "uz": "hozircha ≈ {count} (±{error}; HyperLogLog, ±{rse} %)",
        "en": "≈ {count} so far (±{error}; HyperLogLog, ±{rse} %)",
    },
    "gui.statspanel.sum.approx": {"uz": "≈ {sum} (50 ta muhim raqam)", "en": "≈ {sum} (50 significant digits)"},
    "gui.statspanel.sum.skipped": {
        "uz": "{sum} ({count} ta son qo'shilmadi)",
        "en": "{sum} ({count} numbers left out)",
    },
    "gui.statspanel.sum.skipped.one": {
        "uz": "{sum} ({count} ta son qo'shilmadi)",
        "en": "{sum} ({count} number left out)",
    },
    "gui.statspanel.sum.approx_skipped": {
        "uz": "≈ {sum} (50 ta muhim raqam; {count} ta son qo'shilmadi)",
        "en": "≈ {sum} (50 significant digits; {count} numbers left out)",
    },
    "gui.statspanel.sum.approx_skipped.one": {
        "uz": "≈ {sum} (50 ta muhim raqam; {count} ta son qo'shilmadi)",
        "en": "≈ {sum} (50 significant digits; {count} number left out)",
    },
    "gui.statspanel.mean.skipped": {
        "uz": "{mean} ({count} ta son hisobga olinmadi)",
        "en": "{mean} ({count} numbers left out)",
    },
    "gui.statspanel.mean.skipped.one": {
        "uz": "{mean} ({count} ta son hisobga olinmadi)",
        "en": "{mean} ({count} number left out)",
    },
    "gui.statspanel.top.title": {
        "uz": "Eng ko'p uchragan {count} ta qiymat",
        "en": "The {count} most frequent values",
    },
    "gui.statspanel.top.title.one": {"uz": "Eng ko'p uchragan qiymat", "en": "The most frequent value"},
    "gui.statspanel.top.title_approx": {
        "uz": "Eng ko'p uchragan {count} ta qiymat (taxminiy: har birining haqiqiy soni {bound} tagacha ko'p "
              "bo'lishi mumkin)",
        "en": "The {count} most frequent values (approximate: each true count may be up to {bound} higher)",
    },
    "gui.statspanel.top.title_partial": {
        "uz": "Eng ko'p uchragan qiymatlar (oraliq natija)",
        "en": "The most frequent values (partial result)",
    },
    "gui.statspanel.top.title_partial_approx": {
        "uz": "Eng ko'p uchragan qiymatlar (oraliq natija, taxminiy: har birining haqiqiy soni {bound} tagacha ko'p "
              "bo'lishi mumkin)",
        "en": "The most frequent values (partial result, approximate: each true count may be up to {bound} higher)",
    },
    "gui.statspanel.top.empty": {"uz": "Bu maydonda qiymatlar yo'q.", "en": "The field has no values."},
    "gui.statspanel.top.count_approx": {"uz": "≈ {count}", "en": "≈ {count}"},
    "gui.statspanel.top.count_tip": {
        "uz": "Haqiqiy soni {low} dan {high} gacha.",
        "en": "The true count is between {low} and {high}.",
    },
    "gui.statspanel.top.value_tip": {
        "uz": "{value}\nBirinchi marta #{record} yozuvda uchraydi.",
        "en": "{value}\nFirst seen in record #{record}.",
    },
    "gui.statspanel.top.records_tip": {
        "uz": "Shu qiymat bor yozuvlar: {count} ta. Filtr aynan shu yozuvlarni tanlaydi.",
        "en": "Records holding this value: {count}. The filter selects these records.",
    },
    "gui.statspanel.tip.context": {
        "uz": "Mavjudlik {context} elementlari bo'yicha sanalgan; filtr esa butun yozuvlarni tanlaydi.",
        "en": "Presence is counted per element of {context}; the filter selects whole records.",
    },
    "gui.statspanel.tip.missing_context": {
        "uz": "Filtr {field} maydoni birorta elementida ham bo'lmagan yozuvlarni tanlaydi.",
        "en": "The filter selects the records in which no element has the field {field}.",
    },
    "gui.statspanel.tip.first": {
        "uz": "Birinchi marta #{record} yozuvda uchraydi.",
        "en": "First seen in record #{record}.",
    },
    "gui.statspanel.menu.filter": {"uz": "Filtr: {expr}", "en": "Filter: {expr}"},
    "gui.statspanel.menu.filter_none": {"uz": "Filtr qo'yib bo'lmaydi", "en": "No filter available"},
    "gui.statspanel.menu.reveal": {"uz": "Daraxtda ko'rsatish", "en": "Show in tree"},
    "gui.statspanel.menu.copy": {"uz": "Qiymatni nusxalash", "en": "Copy the value"},
    "gui.statspanel.dupes": {"uz": "Takrorlar…", "en": "Duplicates…"},
    "gui.statspanel.dupes.tip": {
        "uz": "Shu maydon qiymati bo'yicha takroriy yozuvlarni topish.",
        "en": "Find the records that repeat a value of this field.",
    },
    "gui.statspanel.why.single": {
        "uz": "Bitta qiymatdan iborat hujjatni filtrlab bo'lmaydi.",
        "en": "A document holding one value cannot be filtered.",
    },
    "gui.statspanel.why.too_long": {
        "uz": "Qiymat juda uzun (1 MiB dan ortiq): u bo'yicha filtr qo'yib bo'lmaydi.",
        "en": "The value is too long (over 1 MiB) to filter by.",
    },
    "gui.statspanel.why.multi_len": {
        "uz": "Bu maydon bitta yozuvda bir nechta qiymatga ega bo'lishi mumkin, len() esa faqat birinchisini "
              "tekshiradi: uzunlik bo'yicha filtr taklif qilinmaydi.",
        "en": "This field may hold several values in a record and len() checks only the first one, so no length "
              "filter is offered.",
    },
    "gui.statspanel.why.no_filter": {
        "uz": "Bu qiymat bo'yicha filtr qo'yib bo'lmaydi: {error}",
        "en": "Cannot filter by this value: {error}",
    },
    "gui.statspanel.why.stale": {
        "uz": "Natija eskirgan: yozuv raqamlari o'zgargan bo'lishi mumkin. Avval «Yangilash»ni bosing.",
        "en": "The result is out of date: the record numbers may have changed. Click Refresh first.",
    },
    "gui.statspanel.why.dupes_wild": {
        "uz": "Takrorlar faqat aniq yo'l bo'yicha qidiriladi ('*', '[*]' va '..' ishlatilmaydi).",
        "en": "Duplicates are looked for by an exact path only ('*', '[*]' and '..' cannot be used).",
    },
    "gui.statspanel.why.dupes_none": {
        "uz": "Bu maydonda takrorlanadigan qiymat yo'q.",
        "en": "No value of this field repeats.",
    },
    "gui.statspanel.reveal.not_found": {
        "uz": "Qiymat #{record} yozuvda topilmadi: yozuvning o'zi ko'rsatildi.",
        "en": "The value was not found in record #{record}: the record itself is shown.",
    },
    "gui.statspanel.filter.applied": {"uz": "Filtr qo'yildi: {expr}", "en": "Filter applied: {expr}"},
    "gui.statspanel.copied": {"uz": "Nusxalandi.", "en": "Copied."},
    # menu items / dock title for the integration (round B: main window, node menu, table header, schema view)
    "gui.statspanel.dock.title": {"uz": "Statistika", "en": "Statistics"},
    "gui.statspanel.action": {"uz": "Maydon statistikasi…", "en": "Field statistics…"},
    "gui.statspanel.action.tip": {
        "uz": "Maydon statistikasi: mavjud / null / yo'q, turlar, noyob qiymatlar, sonlar, eng ko'p uchragan "
              "qiymatlar (qiymat bosilsa filtr qo'yiladi).",
        "en": "Field statistics: present / null / missing, types, distinct values, numbers, the most frequent "
              "values (click a value to filter by it).",
    },
    "gui.statspanel.action.node": {"uz": "Statistika", "en": "Statistics"},
    "gui.statspanel.action.node.tip": {
        "uz": "Shu maydonning statistikasi (massiv elementlari [*] bo'yicha).",
        "en": "Statistics of this field (array elements as [*]).",
    },
    # the chart area (stage 7): histogram of the numbers, string lengths, the top values as bars
    "gui.statspanel.chart.name": {"uz": "Diagramma", "en": "Chart"},
    "gui.statspanel.chart.kind.name": {"uz": "Diagramma turi", "en": "Chart type"},
    "gui.statspanel.chart.kind.tip": {
        "uz": "Nimani chizish: sonlar taqsimoti (gistogramma), satr uzunliklari yoki eng ko'p uchragan qiymatlar.",
        "en": "What to draw: the distribution of the numbers (a histogram), the string lengths or the most "
              "frequent values.",
    },
    "gui.statspanel.chart.kind.hist": {"uz": "Sonlar taqsimoti", "en": "Number distribution"},
    "gui.statspanel.chart.kind.lens": {"uz": "Satr uzunliklari", "en": "String lengths"},
    "gui.statspanel.chart.kind.top": {"uz": "Eng ko'p uchragan qiymatlar", "en": "Most frequent values"},
    "gui.statspanel.chart.title.hist": {
        "uz": "Sonlar taqsimoti: {bins} ta oraliq, {count} ta son",
        "en": "Number distribution: {bins} bins, {count} numbers",
    },
    "gui.statspanel.chart.title.hist_sampled": {
        "uz": "≈ Sonlar taqsimoti: {bins} ta oraliq, {count} ta son ({sample} ta namuna bo'yicha)",
        "en": "≈ Number distribution: {bins} bins, {count} numbers (from a sample of {sample})",
    },
    "gui.statspanel.chart.title.lens": {
        "uz": "Satr uzunliklari: {bins} ta oraliq, {count} ta satr",
        "en": "String lengths: {bins} bins, {count} strings",
    },
    "gui.statspanel.chart.title.lens_grouped": {
        "uz": "≈ Satr uzunliklari: {bins} ta oraliq, {count} ta satr (uzunliklar {width} tadan guruhlangan)",
        "en": "≈ String lengths: {bins} bins, {count} strings (lengths grouped by {width})",
    },
    "gui.statspanel.chart.title.top": {
        "uz": "Eng ko'p uchragan {count} ta qiymat",
        "en": "The {count} most frequent values",
    },
    "gui.statspanel.chart.title.top.one": {"uz": "Eng ko'p uchragan qiymat", "en": "The most frequent value"},
    "gui.statspanel.chart.tip.sampled": {
        "uz": "Gistogramma {count} ta sondan {sample} tasining tasodifiy namunasi bo'yicha chizilgan: ustunlar "
              "jami songa moslab kengaytirilgan, shuning uchun sonlar taxminiy (≈).",
        "en": "The histogram is drawn from a random sample of {sample} of the {count} numbers: the bars are "
              "scaled to the total, so the counts are approximate (≈).",
    },
    "gui.statspanel.chart.tip.grouped": {
        "uz": "Xil uzunliklar 4 096 tadan ko'p: uzunliklar {width} tadan guruhlab sanaldi.",
        "en": "More than 4 096 distinct lengths: the lengths were counted in groups of {width}.",
    },
    "gui.statspanel.chart.tip.click": {
        "uz": "Ustun ustiga kelsangiz oraliq va soni ko'rsatiladi; bosilsa shu oraliq bo'yicha filtr qo'yiladi.",
        "en": "Hover a bar for its range and count; click it to filter by that range.",
    },
    "gui.statspanel.chart.bar.tip": {"uz": "{range}\n{count} ({share})", "en": "{range}\n{count} ({share})"},
    "gui.statspanel.chart.len.label": {"uz": "{range} belgi", "en": "{range} characters"},
    "gui.statspanel.chart.info.idle": {
        "uz": "{count} ta ustun · ustun bosilsa filtr qo'yiladi",
        "en": "{count} bars · click a bar to filter",
    },
    "gui.statspanel.chart.info.idle.one": {
        "uz": "{count} ta ustun · ustun bosilsa filtr qo'yiladi",
        "en": "{count} bar · click it to filter",
    },
    "gui.statspanel.chart.info.hover": {
        "uz": "{range}: {count} ({share}) — {action}",
        "en": "{range}: {count} ({share}) — {action}",
    },
    # ---- gui.dupesdialog ----
    # the duplicates dialog (gui/dupesdialog.py)
    "gui.dupesdialog.title": {"uz": "Takroriy yozuvlar — {name}", "en": "Duplicate records — {name}"},
    "gui.dupesdialog.key.label": {"uz": "Kalit maydon:", "en": "Key field:"},
    "gui.dupesdialog.key.name": {"uz": "Takrorlar qidiriladigan maydon", "en": "Field to find duplicates by"},
    "gui.dupesdialog.key.placeholder": {
        "uz": "masalan: id, email, contact.phone, $key",
        "en": "e.g. id, email, contact.phone, $key",
    },
    "gui.dupesdialog.key.tip": {
        "uz": "Takrorlar shu maydon qiymati bo'yicha qidiriladi. Aniq yo'l bo'lishi kerak ('*', '[*]' va '..' "
              "ishlatilmaydi); $ — butun yozuv, $key — kalitli hujjatda yozuvning kaliti. Sonlar aniq qiymati "
              "bo'yicha solishtiriladi (1 = 1.0), satrlar esa harfma-harf.",
        "en": "Duplicates are found by the value of this field. It must be an exact path ('*', '[*]' and '..' "
              "cannot be used); $ is the whole record, $key the record key of a keyed document. Numbers compare "
              "by their exact value (1 = 1.0), strings letter by letter.",
    },
    "gui.dupesdialog.key.empty": {
        "uz": "Kalit maydonni yozing yoki ro'yxatdan tanlang.",
        "en": "Type a key field or pick one from the list.",
    },
    "gui.dupesdialog.keep.label": {"uz": "Har guruhda qoladi:", "en": "Keep in each group:"},
    "gui.dupesdialog.keep.first": {"uz": "birinchi yozuv", "en": "the first record"},
    "gui.dupesdialog.keep.last": {"uz": "oxirgi yozuv", "en": "the last record"},
    "gui.dupesdialog.keep.tip": {
        "uz": "Har bir guruhda qaysi yozuv asl nusxa hisoblanadi (fayldagi o'rni bo'yicha); qolganlari ortiqcha "
              "nusxa deb belgilanadi.",
        "en": "Which record of each group counts as the original (by its position in the file); the others are "
              "marked as extra copies.",
    },
    "gui.dupesdialog.scope.label": {"uz": "Doira:", "en": "Scope:"},
    "gui.dupesdialog.scope.view": {
        "uz": "Joriy ko'rinish ({count} ta yozuv)",
        "en": "Current view ({count} records)",
    },
    "gui.dupesdialog.scope.view.one": {
        "uz": "Joriy ko'rinish ({count} ta yozuv)",
        "en": "Current view ({count} record)",
    },
    "gui.dupesdialog.scope.view_base": {
        "uz": "Joriy ko'rinish (barcha yozuvlar)",
        "en": "Current view (all records)",
    },
    "gui.dupesdialog.scope.all": {"uz": "Barcha yozuvlar ({count} ta)", "en": "All records ({count})"},
    "gui.dupesdialog.run": {"uz": "Qidirish", "en": "Find"},
    "gui.dupesdialog.run.tip": {
        "uz": "Takroriy yozuvlarni fon vazifasida qidirish.",
        "en": "Find the duplicate records in a background task.",
    },
    "gui.dupesdialog.stop": {"uz": "To'xtatish", "en": "Stop"},
    "gui.dupesdialog.task": {"uz": "Takrorlar: {key} ({name})", "en": "Duplicates: {key} ({name})"},
    "gui.dupesdialog.task.copy": {"uz": "Takroriy yozuvlar tayyorlanmoqda", "en": "Preparing the duplicate records"},
    "gui.dupesdialog.task.value": {"uz": "Kalit qiymati o'qilmoqda", "en": "Reading the key value"},
    "gui.dupesdialog.status.no_document": {"uz": "Hujjat yopilgan.", "en": "The document is closed."},
    "gui.dupesdialog.status.not_ready": {
        "uz": "Hujjat hali o'qilmoqda (indekslanmoqda): tugagach qidirish mumkin.",
        "en": "The document is still being read (indexed): you can search once it is done.",
    },
    "gui.dupesdialog.status.view_busy": {
        "uz": "Joriy ko'rinish (filtr yoki tartib) hali hisoblanmoqda: tugashini kuting yoki «Barcha yozuvlar» "
              "doirasini tanlang.",
        "en": "The current view (filter or sort) is still being computed: wait for it or choose the scope "
              "\"All records\".",
    },
    "gui.dupesdialog.status.idle": {
        "uz": "Kalit maydonni tanlang va «Qidirish»ni bosing.",
        "en": "Choose a key field and click Find.",
    },
    "gui.dupesdialog.status.running": {"uz": "Takrorlar qidirilmoqda…", "en": "Looking for duplicates…"},
    "gui.dupesdialog.status.done": {"uz": "Tayyor ({seconds} s).", "en": "Done ({seconds} s)."},
    "gui.dupesdialog.status.cancelled": {"uz": "To'xtatildi.", "en": "Stopped."},
    "gui.dupesdialog.status.failed": {"uz": "Qidirib bo'lmadi: {error}", "en": "Could not search: {error}"},
    "gui.dupesdialog.status.reopened": {
        "uz": "Fayl qayta ochildi: qidiruv to'xtatildi.",
        "en": "The file was reopened: the search was stopped.",
    },
    "gui.dupesdialog.stale.data": {
        "uz": "Natija eskirgan: hujjat o'zgardi — qayta qidiring.",
        "en": "The result is out of date: the document changed. Search again.",
    },
    "gui.dupesdialog.stale.view": {
        "uz": "Qidiruvdan keyin joriy ko'rinish o'zgardi.",
        "en": "The current view changed after the search.",
    },
    "gui.dupesdialog.count.scanned": {"uz": "Tekshirilgan yozuvlar", "en": "Records checked"},
    "gui.dupesdialog.count.with_key": {"uz": "Kaliti bor yozuvlar", "en": "Records with the key"},
    "gui.dupesdialog.count.missing": {"uz": "Kaliti yo'q yozuvlar", "en": "Records without the key"},
    "gui.dupesdialog.count.skipped": {"uz": "O'qib bo'lmagan yozuvlar", "en": "Records that could not be read"},
    "gui.dupesdialog.count.distinct": {"uz": "Noyob kalit qiymatlari", "en": "Distinct key values"},
    "gui.dupesdialog.count.groups": {"uz": "Takrorlangan qiymatlar (guruhlar)", "en": "Repeated values (groups)"},
    "gui.dupesdialog.count.duplicates": {"uz": "Ortiqcha nusxalar", "en": "Extra copies (duplicates)"},
    "gui.dupesdialog.groups.title": {
        "uz": "Birinchi {count} ta guruh (fayldagi o'rni bo'yicha)",
        "en": "The first {count} groups (by position in the file)",
    },
    "gui.dupesdialog.groups.title.one": {
        "uz": "Takroriy guruh ({count} ta)",
        "en": "The group of duplicates ({count})",
    },
    "gui.dupesdialog.groups.none": {
        "uz": "Takrorlanadigan qiymat topilmadi.",
        "en": "No repeated values were found.",
    },
    "gui.dupesdialog.groups.col.key": {"uz": "Kalit qiymati / yozuv", "en": "Key value / record"},
    "gui.dupesdialog.groups.col.size": {"uz": "Yozuvlar", "en": "Records"},
    "gui.dupesdialog.groups.name": {"uz": "Takroriy guruhlar", "en": "Groups of duplicates"},
    "gui.dupesdialog.groups.kept": {"uz": "(qoladi)", "en": "(kept)"},
    "gui.dupesdialog.groups.more": {"uz": "… yana {count} ta yozuv", "en": "… {count} more records"},
    "gui.dupesdialog.groups.more.one": {"uz": "… yana {count} ta yozuv", "en": "… {count} more record"},
    "gui.dupesdialog.menu.reveal": {"uz": "Daraxtda ko'rsatish", "en": "Show in tree"},
    "gui.dupesdialog.menu.filter_key": {
        "uz": "Shu kalit qiymati bo'yicha filtr",
        "en": "Filter by this key value",
    },
    "gui.dupesdialog.menu.copy": {"uz": "Kalit qiymatini nusxalash", "en": "Copy the key value"},
    "gui.dupesdialog.filter": {"uz": "Filtr", "en": "Filter"},
    "gui.dupesdialog.filter.tip": {
        "uz": "Ko'rinishni tanlangan guruhning kalit qiymati bo'yicha filtrlash.",
        "en": "Filter the view by the key value of the selected group.",
    },
    "gui.dupesdialog.filter.applied": {"uz": "Filtr qo'yildi: {expr}", "en": "Filter applied: {expr}"},
    "gui.dupesdialog.show": {"uz": "Takroriylarni ko'rsatish", "en": "Show the duplicates"},
    "gui.dupesdialog.show.tip": {
        "uz": "Ortiqcha nusxalarni (har guruhda qoladigan yozuvdan boshqa hammasini) joriy ko'rinish sifatida "
              "ochish.",
        "en": "Show the extra copies (every record of a group except the one kept) as the current view.",
    },
    "gui.dupesdialog.shown": {
        "uz": "Takroriy yozuvlar ko'rsatildi: {count} ta.",
        "en": "Duplicate records shown: {count}.",
    },
    "gui.dupesdialog.shown.one": {
        "uz": "Takroriy yozuvlar ko'rsatildi: {count} ta.",
        "en": "Duplicate record shown: {count}.",
    },
    "gui.dupesdialog.export": {"uz": "Eksport…", "en": "Export…"},
    "gui.dupesdialog.export.tip": {
        "uz": "Ortiqcha nusxalarni faylga eksport qilish.",
        "en": "Export the extra copies to a file.",
    },
    "gui.dupesdialog.remove": {"uz": "Takroriylarni o'chirish…", "en": "Remove the duplicates…"},
    "gui.dupesdialog.remove.tip": {
        "uz": "Takrorlarni o'chirishning oldindan ko'rinishini ommaviy amallar oynasida ochish: "
              "tasdiqlamaguningizcha hech narsa o'zgarmaydi.",
        "en": "Open the preview of removing the duplicates in the bulk operations window: nothing changes until "
              "you confirm.",
    },
    "gui.dupesdialog.remove.view_changed": {
        "uz": "Qidiruv joriy ko'rinishda bajarilgan edi, ammo ko'rinish o'zgardi: o'chirishdan oldin qayta "
              "qidiring yoki «Barcha yozuvlar» doirasini tanlang.",
        "en": "The search ran on the current view, but the view has changed: search again or choose \"All "
              "records\" before removing.",
    },
    "gui.dupesdialog.close": {"uz": "Yopish", "en": "Close"},
    "gui.dupesdialog.why.no_result": {"uz": "Avval takrorlarni qidiring.", "en": "Find the duplicates first."},
    "gui.dupesdialog.why.none": {"uz": "Takroriy yozuvlar yo'q.", "en": "There are no duplicate records."},
    "gui.dupesdialog.why.no_group": {"uz": "Ro'yxatdan guruhni tanlang.", "en": "Select a group in the list."},
    "gui.dupesdialog.why.single": {
        "uz": "Bitta qiymatdan iborat hujjatda bu amal bajarilmaydi.",
        "en": "This is not available for a document holding one value.",
    },
    "gui.dupesdialog.why.stale": {
        "uz": "Natija eskirgan (hujjat o'zgardi): qayta qidiring.",
        "en": "The result is out of date (the document changed): search again.",
    },
    "gui.dupesdialog.copied": {"uz": "Nusxalandi.", "en": "Copied."},
    # menu items for the integration (round B: Tools menu, schema view)
    "gui.dupesdialog.action": {"uz": "Takroriy yozuvlar…", "en": "Duplicate records…"},
    "gui.dupesdialog.action.tip": {
        "uz": "Tanlangan kalit bo'yicha takroriy yozuvlarni topish, ko'rsatish, eksport qilish yoki o'chirish.",
        "en": "Find the records that repeat a key, then show, export or remove them.",
    },
    # ---- gui.mapview ----
    # the structure builder view (gui/mapview.py, gui/mapmodel.py; docs/ARCHITECTURE.md §13.5)
    "gui.mapview.act.open": {"uz": "Tuzilmani ochish…", "en": "Open mapping…"},
    "gui.mapview.act.open.tip": {
        "uz": "Saqlangan tuzilma (mapping) faylini ochish",
        "en": "Open a saved mapping file",
    },
    "gui.mapview.act.save": {"uz": "Tuzilmani saqlash…", "en": "Save mapping…"},
    "gui.mapview.act.save.tip": {
        "uz": "Tuzilmani mapping fayli sifatida saqlash (keyin «jsonreshape apply» bilan ishlatish mumkin)",
        "en": "Save the structure as a mapping file (usable later with «jsonreshape apply»)",
    },
    "gui.mapview.act.undo": {"uz": "Ortga qaytarish", "en": "Undo"},
    "gui.mapview.act.undo.tip": {
        "uz": "Tuzilmadagi oxirgi o'zgarishni ortga qaytarish (Ctrl+Z)",
        "en": "Undo the last change of the structure (Ctrl+Z)",
    },
    "gui.mapview.act.redo": {"uz": "Qayta bajarish", "en": "Redo"},
    "gui.mapview.act.redo.tip": {
        "uz": "Ortga qaytarilgan o'zgarishni qayta bajarish (Ctrl+Y, Ctrl+Shift+Z)",
        "en": "Do the undone change again (Ctrl+Y, Ctrl+Shift+Z)",
    },
    "gui.mapview.act.reset": {"uz": "Asl holat", "en": "Original"},
    "gui.mapview.act.reset.tip": {
        "uz": "Fayldan aniqlangan asl tuzilmaga qaytish (buni ham qaytarish mumkin)",
        "en": "Go back to the structure found in the file (this can be undone too)",
    },
    "gui.mapview.act.expand": {"uz": "Hammasini ochish", "en": "Expand all"},
    "gui.mapview.act.collapse": {"uz": "Yig'ish", "en": "Collapse"},
    "gui.mapview.act.full_scan": {"uz": "To'liq skanerlash", "en": "Full scan"},
    "gui.mapview.act.full_scan.running": {"uz": "Skanerlanmoqda…", "en": "Scanning…"},
    "gui.mapview.act.full_scan.tip": {
        "uz": "Tuzilmani namunadan emas, barcha yozuvlardan aniqlash (fonda, vazifalar panelida bekor qilish "
              "mumkin)",
        "en": "Find the structure from every record instead of a sample (in the background; it can be cancelled "
              "in the tasks panel)",
    },
    "gui.mapview.act.compact": {"uz": "Ixcham", "en": "Compact"},
    "gui.mapview.act.compact.tip": {
        "uz": "Eksportda JSON'ni chekinishsiz, ixcham yozish",
        "en": "Write the exported JSON compact, without indentation",
    },
    "gui.mapview.act.export": {"uz": "Eksport…", "en": "Export…"},
    "gui.mapview.act.export.tip": {
        "uz": "Butun faylni shu tuzilma bo'yicha qayta qurib saqlash (Ctrl+E)",
        "en": "Restructure the whole file with this structure and save it (Ctrl+E)",
    },
    "gui.mapview.pane.tree": {"uz": "Tuzilma", "en": "Structure"},
    "gui.mapview.pane.preview": {"uz": "Ko'rinish", "en": "Preview"},
    "gui.mapview.hint.inside": {"uz": "Sudrab ko'chir: qator o'rtasiga → ichiga", "en": "Drag: onto the middle → inside"},
    "gui.mapview.hint.sibling": {"uz": "tepasi / pastiga → yoniga", "en": "top / bottom → next to it"},
    "gui.mapview.hint.merge": {
        "uz": "massivni massivga → elementlari qo'shiladi",
        "en": "an array onto an array → its elements are added",
    },
    "gui.mapview.hint.keys": {
        "uz": "Klaviatura: F2 — nomlash, Del — olib tashlash, Insert — yangi guruh, Ctrl+X va Ctrl+V — ko'chirish, "
              "Alt+↑/↓ — surish, Ctrl+Shift+U — ichidagilarni chiqarish, Enter — ochish/yig'ish",
        "en": "Keyboard: F2 rename, Del remove, Insert new group, Ctrl+X then Ctrl+V move, Alt+↑/↓ move up/down, "
              "Ctrl+Shift+U unwrap, Enter expand/collapse",
    },
    "gui.mapview.source.sample": {
        "uz": "Namunadan: {scanned} / {total} yozuv",
        "en": "From a sample: {scanned} of {total} records",
    },
    "gui.mapview.source.sample_members": {
        "uz": "Namunadan: {scanned} / {total} a'zo",
        "en": "From a sample: {scanned} of {total} members",
    },
    "gui.mapview.source.full": {"uz": "Barcha {scanned} yozuvdan", "en": "From all {scanned} records"},
    "gui.mapview.source.full_members": {"uz": "Barcha {scanned} a'zodan", "en": "From all {scanned} members"},
    "gui.mapview.source.file": {"uz": "Fayldan: {name}", "en": "From the file {name}"},
    "gui.mapview.source.skipped": {
        "uz": "{count} ta juda katta yozuv hisobga olinmadi",
        "en": "{count} very large records were left out",
    },
    "gui.mapview.source.truncated": {"uz": "tuzilma qisqartirildi", "en": "structure shortened"},
    "gui.mapview.source.flattening": {"uz": "tekislanmoqda…", "en": "flattening…"},
    "gui.mapview.state.waiting_open": {"uz": "Fayl ochilmoqda…", "en": "Opening the file…"},
    "gui.mapview.state.waiting_index": {
        "uz": "Fayl indekslanmoqda: tuzilma indeks tugagach aniqlanadi.",
        "en": "The file is being indexed: the structure is found once the index is complete.",
    },
    "gui.mapview.state.building": {
        "uz": "Tuzilma {count} ta yozuvlik namunadan aniqlanmoqda…",
        "en": "Finding the structure from a sample of {count} records…",
    },
    "gui.mapview.state.scanning": {
        "uz": "Tuzilma barcha yozuvlardan aniqlanmoqda…",
        "en": "Finding the structure from every record…",
    },
    "gui.mapview.state.failed": {"uz": "Tuzilmani aniqlab bo'lmadi: {error}", "en": "The structure could not be found: {error}"},
    "gui.mapview.state.unavailable": {
        "uz": "Fayl ochilmadi: tuzilma quruvchi ishlamaydi.",
        "en": "The file did not open: the structure builder cannot work.",
    },
    "gui.mapview.retry": {"uz": "Qayta urinish", "en": "Try again"},
    "gui.mapview.caret.open": {"uz": "Ochish", "en": "Expand"},
    "gui.mapview.caret.close": {"uz": "Yig'ish", "en": "Collapse"},
    "gui.mapview.rename.name": {"uz": "Yangi nom", "en": "New name"},
    "gui.mapview.menu.cut": {"uz": "Ko'chirish uchun kesib olish", "en": "Cut (to move)"},
    "gui.mapview.menu.paste": {"uz": "Shu yerga qo'yish", "en": "Put here"},
    "gui.mapview.menu.move_up": {"uz": "Yuqoriga surish", "en": "Move up"},
    "gui.mapview.menu.move_down": {"uz": "Pastga surish", "en": "Move down"},
    "gui.mapview.menu.open": {"uz": "Ochish", "en": "Expand"},
    "gui.mapview.menu.close": {"uz": "Yig'ish", "en": "Collapse"},
    "gui.mapview.menu.copy_source": {"uz": "Manba yo'lini nusxalash", "en": "Copy the source path"},
    "gui.mapview.why.no_tree": {"uz": "Tuzilma hali tayyor emas.", "en": "The structure is not ready yet."},
    "gui.mapview.why.busy": {
        "uz": "Rekursiv tekislash hisoblanmoqda: tugashini kuting.",
        "en": "A recursive flatten is being computed: wait until it is done.",
    },
    "gui.mapview.why.scanning": {
        "uz": "Tuzilma barcha yozuvlardan aniqlanmoqda va tugagach shu tuzilmaning o'rnini oladi: tugashini "
              "kuting (vazifalar panelida bekor qilish mumkin).",
        "en": "The structure is being found from every record and replaces this one when done: wait until it is "
              "done (it can be cancelled in the tasks panel).",
    },
    "gui.mapview.why.no_cut": {
        "uz": "Avval ko'chiriladigan tugunni Ctrl+X bilan kesib oling.",
        "en": "First cut the node to move with Ctrl+X.",
    },
    "gui.mapview.why.no_children": {"uz": "Bu qatorning ichida hech narsa yo'q.", "en": "This row holds nothing."},
    "gui.mapview.why.original": {
        "uz": "Tuzilma hali o'zgartirilmagan: u asl holatda.",
        "en": "The structure has not been changed: it is the original.",
    },
    "gui.mapview.msg.cut": {
        "uz": "«{name}» kesib olindi: qo'yiladigan qatorni tanlab Ctrl+V bosing (Esc — bekor qilish).",
        "en": "«{name}» was cut: select the row to put it on and press Ctrl+V (Esc cancels).",
    },
    "gui.mapview.msg.cut_cancelled": {"uz": "Ko'chirish bekor qilindi.", "en": "The move was cancelled."},
    "gui.mapview.msg.flattening": {
        "uz": "«{key}» rekursiv tekislanmoqda…",
        "en": "Flattening «{key}» recursively…",
    },
    "gui.mapview.msg.saved": {"uz": "Tuzilma saqlandi: {path}", "en": "The mapping was saved: {path}"},
    "gui.mapview.msg.loaded": {"uz": "Tuzilma ochildi: {path}", "en": "The mapping was opened: {path}"},
    "gui.mapview.msg.rebuilt": {
        "uz": "Fayl boshqa ko'rinishda qayta ochildi: tuzilma yangidan aniqlandi (oldingisi bu yozuvlarga mos emas).",
        "en": "The file was opened again another way: the structure was found anew (the previous one does not fit "
              "these records).",
    },
    "gui.mapview.msg.not_ready": {"uz": "Tuzilma hali tayyor emas.", "en": "The structure is not ready yet."},
    "gui.mapview.ask.title": {"uz": "Tuzilma quruvchi", "en": "Structure builder"},
    "gui.mapview.ask.replace": {
        "uz": "Tuzilmadagi {count} ta o'zgarish yo'qoladi (keyin qaytarib bo'lmaydi). Davom etilsinmi?",
        "en": "The {count} changes of the structure will be lost (they cannot be undone afterwards). Continue?",
    },
    "gui.mapview.open.title": {"uz": "Tuzilma (mapping) faylini ochish", "en": "Open a mapping file"},
    "gui.mapview.save.title": {"uz": "Tuzilmani (mapping) saqlash", "en": "Save the mapping"},
    "gui.mapview.filter": {
        "uz": "Mapping fayllari ({patterns});;Barcha fayllar (*)",
        "en": "Mapping files ({patterns});;All files (*)",
    },
    "gui.mapview.undo_label": {"uz": "tuzilma", "en": "structure"},
    "gui.mapview.task.build": {"uz": "Tuzilma aniqlanmoqda: {name}", "en": "Finding the structure: {name}"},
    "gui.mapview.task.full": {
        "uz": "Tuzilma barcha yozuvlardan aniqlanmoqda: {name}",
        "en": "Finding the structure from every record: {name}",
    },
    "gui.mapview.task.flatten": {"uz": "Rekursiv tekislash: {name}", "en": "Recursive flatten: {name}"},
    "gui.mapview.task.preview": {"uz": "Tuzilma ko'rinishi: {name}", "en": "Structure preview: {name}"},
    "gui.mapview.task.open": {"uz": "Tuzilma ochilmoqda: {name}", "en": "Opening the mapping: {name}"},
    "gui.mapview.task.save": {"uz": "Tuzilma saqlanmoqda: {name}", "en": "Saving the mapping: {name}"},
    "gui.mapview.preview.out": {"uz": "Natija", "en": "Result"},
    "gui.mapview.preview.src": {"uz": "Asl", "en": "Original"},
    "gui.mapview.preview.kind_tip": {
        "uz": "Ko'rinish turi: natija yoki fayldagi asl yozuv",
        "en": "Preview kind: the result or the original record of the file",
    },
    "gui.mapview.preview.prev": {"uz": "Oldingi yozuv", "en": "Previous record"},
    "gui.mapview.preview.next": {"uz": "Keyingi yozuv", "en": "Next record"},
    "gui.mapview.preview.number": {"uz": "Yozuv raqami (0 dan)", "en": "Record number (from 0)"},
    "gui.mapview.preview.total": {"uz": "/ {count} ta yozuv", "en": "of {count} records"},
    "gui.mapview.preview.total_members": {"uz": "/ {count} ta a'zo", "en": "of {count} members"},
    "gui.mapview.preview.more_out": {"uz": "… davomi eksportda", "en": "… the rest is in the export"},
    "gui.mapview.preview.more_items": {"uz": "… yana {count} ta", "en": "… {count} more"},
    "gui.mapview.preview.more_keys": {"uz": "… yana {count} ta kalit", "en": "… {count} more keys"},
    "gui.mapview.preview.cut": {
        "uz": "… ko'rinish shu yerda qisqartirildi (eksportda hammasi chiqadi)",
        "en": "… the preview is cut here (the export writes everything)",
    },
    "gui.mapview.preview.error": {"uz": "Ko'rinishni hisoblab bo'lmadi: {error}", "en": "The preview could not be computed: {error}"},
    "gui.mapview.preview.too_large": {
        "uz": "#{record} yozuv ko'rinish uchun juda katta ({size}, chegara {limit}); eksport uni baribir qayta quradi.",
        "en": "Record #{record} is too large for the preview ({size}, limit {limit}); the export restructures it "
              "anyway.",
    },
    "gui.mapview.preview.no_records": {"uz": "Hujjatda yozuv yo'q.", "en": "The document has no records."},
    "gui.mapview.preview.missing_member": {
        "uz": "«{key}» a'zosi natijaga yozilmaydi: tuzilma unga qiymat bermaydi.",
        "en": "The member «{key}» is not written: the structure gives it no value.",
    },
    "gui.mapview.preview.foot.out": {"uz": "#{record} yozuv natijada qanday chiqishi.", "en": "How record #{record} comes out."},
    "gui.mapview.preview.foot.out_member": {
        "uz": "#{record} a'zo natijada qanday chiqishi.",
        "en": "How member #{record} comes out.",
    },
    "gui.mapview.preview.foot.limit": {
        "uz": "Ko'rinishda massivlarning 20 tasigacha elementi; eksportda hammasi chiqadi.",
        "en": "The preview shows up to 20 elements of each array; the export writes them all.",
    },
    "gui.mapview.preview.foot.src": {
        "uz": "Fayldagi #{record} yozuv, o'zgartirilmagan holda.",
        "en": "Record #{record} of the file, unchanged.",
    },
    "gui.mapview.preview.foot.src_member": {
        "uz": "Fayldagi #{record} a'zo, o'zgartirilmagan holda.",
        "en": "Member #{record} of the file, unchanged.",
    },
    "gui.mapview.preview.foot.src_value": {"uz": "Fayldagi asl ma'lumot.", "en": "The original data of the file."},
    "gui.mapview.preview.foot.edited": {
        "uz": "Saqlanmagan o'zgarishlar ham hisobga olingan.",
        "en": "Unsaved changes are included.",
    },
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
    "gui.search.stale_edit": {
        "uz": "Hujjat o'zgardi (tahrir yoki ularni bekor qilish): qidiruvni qaytadan bajaring.",
        "en": "The document changed (an edit, or the changes were discarded): run the search again.",
    },
    "gui.search.outdated": {
        "uz": "Hujjat qidiruvdan keyin tahrirlandi: natijalar eskirgan bo'lishi mumkin. Yangilash uchun qaytadan "
        "qidiring.",
        "en": "The document was edited after the search: the results may be out of date. Search again to "
        "refresh them.",
    },
    "gui.search.outdated_cleared": {
        "uz": "Hujjat qidiruvdan keyin tahrirlandi va natijalar juda ko'p edi: qidiruvni qaytadan bajaring.",
        "en": "The document was edited after the search and there were too many results to update: run the "
        "search again.",
    },
    "gui.search.export.outdated": {
        "uz": "Qidirilgan ko'rinishdagi yozuvlar tahrirda ko'chdi: eksport uchun qaytadan qidiring.",
        "en": "The records of the view searched were moved by an edit: search again to export.",
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
    "gui.export.mapping.title": {"uz": "Tuzilma bo'yicha eksport — {name}", "en": "Export with the mapping — {name}"},
    "gui.export.mapping.use": {
        "uz": "Tuzilma bo'yicha qayta qurish ({summary})",
        "en": "Restructure with the mapping ({summary})",
    },
    "gui.export.mapping.use_plain": {"uz": "Tuzilma bo'yicha qayta qurish", "en": "Restructure with the mapping"},
    "gui.export.mapping.use.tip": {
        "uz": "Har bir yozuv tuzilma quruvchidagi tuzilma bo'yicha qayta quriladi. Belgini olib tashlasangiz, oddiy "
              "eksport bo'ladi.",
        "en": "Every record is restructured with the structure builder's mapping. Clear it for an ordinary export.",
    },
    "gui.export.mapping.formats": {
        "uz": "Tuzilma bo'yicha eksport faqat JSON yoki JSON Lines bo'ladi.",
        "en": "An export with the mapping is JSON or JSON Lines only.",
    },
    "gui.export.mapping.no_node": {
        "uz": "Tuzilma bo'yicha eksportda bitta tugunni tanlab bo'lmaydi: yozuvlar qayta quriladi.",
        "en": "An export with the mapping cannot take one node: it restructures the records.",
    },
    "gui.export.mapping.not_used": {
        "uz": "Tuzilma bo'yicha eksportda ishlatilmaydi (kalitlar tartibi tuzilmadan olinadi).",
        "en": "Not used by an export with the mapping (the key order comes from the structure).",
    },
    "gui.export.mapping.skip_huge": {
        "uz": "Juda katta yozuvlarni o'tkazib yuborish",
        "en": "Skip very large records",
    },
    "gui.export.mapping.skip_huge.tip": {
        "uz": "{limit} dan katta yozuvlarni xotirada qayta qurib bo'lmaydi: belgilansa ular o'tkazib yuboriladi va "
              "soni hisobotda aytiladi, aks holda eksport ularga yetganda to'xtaydi.",
        "en": "Records larger than {limit} cannot be restructured in memory: when this is checked they are skipped "
              "and counted in the report, otherwise the export stops at the first one.",
    },
    "gui.export.mapping.task": {"uz": "Tuzilma bo'yicha eksport: {name}", "en": "Export with the mapping: {name}"},
    "gui.export.mapping.no_tree": {"uz": "Tuzilma hali tayyor emas.", "en": "The structure is not ready yet."},
    "gui.export.mapping.gzip": {
        "uz": "Tuzilma bo'yicha eksport siqilmagan JSON yozadi, nom esa .gz bilan tugaydi: nomdan .gz ni olib "
              "tashlang.",
        "en": "The export with the mapping writes uncompressed JSON, but the name ends in .gz: remove .gz from "
              "the name.",
    },
    "gui.export.path.label": {"uz": "Fayl:", "en": "File:"},
    "gui.export.path.placeholder": {"uz": "Natija fayli yo'li", "en": "Path of the result file"},
    "gui.export.browse": {"uz": "Tanlash…", "en": "Browse…"},
    "gui.export.choose.title": {"uz": "Natijani qayerga saqlash", "en": "Where to save the result"},
    "gui.export.format.label": {"uz": "Format:", "en": "Format:"},
    "gui.export.format.json": {"uz": "JSON", "en": "JSON"},
    "gui.export.format.jsonl": {"uz": "JSON Lines (har qatorda bitta yozuv)", "en": "JSON Lines (one record per line)"},
    "gui.export.format.csv": {"uz": "CSV (jadval)", "en": "CSV (table)"},
    "gui.export.format.xlsx": {"uz": "Excel (XLSX)", "en": "Excel (XLSX)"},
    "gui.export.format.parquet": {"uz": "Parquet (ustunli jadval)", "en": "Parquet (columnar table)"},
    "gui.export.format.sqlite": {"uz": "SQLite ma'lumotlar bazasi", "en": "SQLite database"},
    "gui.export.filter.json": {"uz": "JSON (*.json)", "en": "JSON (*.json)"},
    "gui.export.filter.jsonl": {"uz": "JSON Lines (*.jsonl *.ndjson)", "en": "JSON Lines (*.jsonl *.ndjson)"},
    "gui.export.filter.csv": {"uz": "CSV (*.csv)", "en": "CSV (*.csv)"},
    "gui.export.filter.xlsx": {"uz": "Excel (*.xlsx)", "en": "Excel (*.xlsx)"},
    "gui.export.filter.parquet": {"uz": "Parquet (*.parquet)", "en": "Parquet (*.parquet)"},
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
        "uz": "Faqat tanlangan tugun yoziladi. JSON / JSON Lines: sonlar va matnlar baytma-bayt o'zgarmaydi. CSV, "
              "Excel, SQLite: massiv yoki obyekt tugunning elementlari yozuvlar sifatida yoziladi.",
        "en": "Only the selected node is written. JSON / JSON Lines: numbers and strings stay byte for byte. CSV, "
              "Excel, SQLite: the elements of an array or object node are written as the records.",
    },
    "gui.export.scope.selected": {
        "uz": "Tanlangan yozuvlar ({count} ta)",
        "en": "Selected records ({count})",
    },
    "gui.export.scope.selected.tip": {
        "uz": "Jadvalda yoki daraxtda tanlangan yozuvlar, ko'rinishdagi tartibda.",
        "en": "The records selected in the table or the tree, in the order of the view.",
    },
    "gui.export.scope.selected_none": {"uz": "Tanlangan yozuvlar (tanlanmagan)", "en": "Selected records (none)"},
    "gui.export.scope.selected_none.tip": {
        "uz": "Avval jadvalda qatorlarni yoki daraxtda tugunlarni tanlang.",
        "en": "First select rows in the table or nodes in the tree.",
    },
    "gui.export.scope.selected_label": {"uz": "Tanlangan yozuvlar", "en": "Selected records"},
    "gui.export.scope.records": {"uz": "{label} ({count} ta yozuv)", "en": "{label} ({count} records)"},
    "gui.export.scope.records.tip": {
        "uz": "Shu yozuvlar ro'yxati yoziladi; joriy ko'rinish o'zgarmaydi.",
        "en": "This list of records is written; the current view does not change.",
    },
    "gui.export.scope.records_default": {"uz": "Berilgan yozuvlar", "en": "Given records"},
    "gui.export.scope.records_gone": {
        "uz": "Yozuvlar ro'yxati endi yo'q (hujjat yopilgan).",
        "en": "The list of records is gone (the document was closed).",
    },
    "gui.export.mapping.no_scope": {
        "uz": "Tuzilma bo'yicha eksport faqat joriy ko'rinish yoki barcha yozuvlar uchun.",
        "en": "An export with the mapping takes the current view or all records only.",
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
    "gui.export.keep_wrapper.tip.json": {
        "uz": "Yozuvlar massivi atrofidagi tashqi o'rama maydonlari (masalan, meta) ham yoziladi.",
        "en": "The wrapper's fields around the record array (e.g. meta) are written too.",
    },
    "gui.export.keep_wrapper.tip.sqlite": {
        "uz": "Tashqi o'rama maydonlari (masalan, meta) alohida _wrapper jadvaliga yoziladi.",
        "en": "The wrapper's fields (e.g. meta) are written to a separate _wrapper table.",
    },
    "gui.export.keep_wrapper.tip.jsonl": {
        "uz": "JSON Lines'da tashqi o'rama uchun joy yo'q: belgilansa hisobotda bu haqda eslatma chiqadi; "
              "belgilanmasa faqat yozuvlar so'ralgan hisoblanadi.",
        "en": "JSON Lines has no place for the wrapper: when checked, the report notes that it was not written; "
              "unchecked means only the records are wanted.",
    },
    "gui.export.keep_wrapper.tip.csv": {
        "uz": "CSV'da tashqi o'rama uchun joy yo'q: belgilansa hisobotda bu haqda eslatma chiqadi; belgilanmasa "
              "faqat yozuvlar so'ralgan hisoblanadi.",
        "en": "CSV has no place for the wrapper: when checked, the report notes that it was not written; unchecked "
              "means only the records are wanted.",
    },
    "gui.export.keep_wrapper.tip.xlsx": {
        "uz": "Excel faylida tashqi o'rama uchun joy yo'q: belgilansa hisobotda bu haqda eslatma chiqadi; "
              "belgilanmasa faqat yozuvlar so'ralgan hisoblanadi.",
        "en": "An Excel file has no place for the wrapper: when checked, the report notes that it was not written; "
              "unchecked means only the records are wanted.",
    },
    "gui.export.keep_wrapper.tip.parquet": {
        "uz": "Parquet faylida tashqi o'rama uchun joy yo'q: belgilansa hisobotda bu haqda eslatma chiqadi; "
              "belgilanmasa faqat yozuvlar so'ralgan hisoblanadi.",
        "en": "A Parquet file has no place for the wrapper: when checked, the report notes that it was not written; "
              "unchecked means only the records are wanted.",
    },
    "gui.export.keep_wrapper.node": {
        "uz": "Tugun eksportida natija faqat shu tugunning o'zi bo'ladi (tashqi o'ramasiz).",
        "en": "A node export writes the node alone (without a wrapper).",
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
        "uz": "Tugunning o'zi faqat JSON yoki JSON Lines formatida yoziladi (CSV, Excel va SQLite massiv yoki "
              "obyekt tugunning elementlarini yozuv sifatida yozadi).",
        "en": "The node itself is written only as JSON or JSON Lines (CSV, Excel and SQLite write the elements of "
              "an array or object node as records).",
    },
    "gui.export.progress.node_open": {
        "uz": "Tugun yozuvlar sifatida ochilmoqda: {name}",
        "en": "Opening the node as records: {name}",
    },
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
    "gui.export.report.mapping": {
        "uz": "Tuzilma bo'yicha: {records} ta yozuv → {items} ta element ({passes} o'tish), {size}; {seconds} vaqt "
              "ketdi ({rate} MB/s).",
        "en": "With the mapping: {records} records → {items} items ({passes} passes), {size}; took {seconds} "
              "({rate} MB/s).",
    },
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
    "gui.export.report.edited": {
        "uz": "Eksportga saqlanmagan o'zgarishlar ham kirdi.",
        "en": "The unsaved changes were exported too.",
    },
    "gui.export.edited_note": {
        "uz": "Hujjatda saqlanmagan o'zgarishlar bor: eksportga ular ham kiradi (fayl o'zi o'zgarmaydi).",
        "en": "The document has unsaved changes: the export includes them (the file itself is not changed).",
    },
    # ---- gui.validatepanel ----
    "gui.validatepanel.menu.json_schema": {"uz": "JSON Schema", "en": "JSON Schema"},
    "gui.validatepanel.menu.generate": {"uz": "Schema yaratish…", "en": "Generate a schema…"},
    "gui.validatepanel.menu.generate.tip": {
        "uz": "Hujjat ma'lumotlaridan JSON Schema (draft 2020-12) yaratib, faylga yozish",
        "en": "Generate a JSON Schema (draft 2020-12) from the document's data and write it to a file",
    },
    "gui.validatepanel.menu.validate": {"uz": "Schema bo'yicha tekshirish…", "en": "Validate against a schema…"},
    "gui.validatepanel.menu.validate.tip": {
        "uz": "Hujjatni JSON Schema fayli bo'yicha tekshirish: xatolar yozuv raqami va yo'li bilan chiqadi",
        "en": "Validate the document against a JSON Schema file: problems are listed with the record number and "
              "path",
    },
    "gui.validatepanel.dock": {"uz": "Schema tekshiruvi", "en": "Schema validation"},
    "gui.validatepanel.gen.title": {"uz": "Schema yaratish — {name}", "en": "Generate a schema — {name}"},
    "gui.validatepanel.gen.intro": {
        "uz": "Hujjat ma'lumotlaridan JSON Schema (draft 2020-12) yaratiladi va faylga yoziladi. Sonlar aynan "
              "fayldagidek saqlanadi.",
        "en": "A JSON Schema (draft 2020-12) is generated from the document's data and written to a file. Numbers "
              "are kept exactly as in the file.",
    },
    "gui.validatepanel.gen.source": {"uz": "Nimaga asoslanadi", "en": "Based on"},
    "gui.validatepanel.gen.source.cached": {
        "uz": "Tayyor to'liq tahlil ({count} ta yozuv)",
        "en": "The full analysis already made ({count} records)",
    },
    "gui.validatepanel.gen.source.cached.tip": {
        "uz": "Tuzilma ko'rinishidagi to'liq skanerlash natijasi qayta ishlatiladi: fayl yana o'qilmaydi.",
        "en": "The result of the structure view's full scan is reused: the file is not read again.",
    },
    "gui.validatepanel.gen.source.cached_none": {
        "uz": "Tayyor to'liq tahlil (hali yo'q)",
        "en": "The full analysis already made (none yet)",
    },
    "gui.validatepanel.gen.source.cached.tip_none": {
        "uz": "To'liq tahlil hali bajarilmagan yoki hujjat o'zgargandan keyin eskirgan. \"To'liq skanerlash\"ni "
              "tanlang.",
        "en": "No full analysis has been made yet, or the document changed since. Choose \"Full scan\".",
    },
    "gui.validatepanel.gen.source.full": {
        "uz": "To'liq skanerlash (barcha yozuvlar o'qiladi)",
        "en": "Full scan (every record is read)",
    },
    "gui.validatepanel.gen.source.full.tip": {
        "uz": "Aniq schema: har bir yozuv hisobga olinadi. Katta faylda biroz vaqt oladi (fonda bajariladi, "
              "to'xtatish mumkin).",
        "en": "An exact schema: every record is taken into account. Takes a while for a big file (it runs in the "
              "background and can be stopped).",
    },
    "gui.validatepanel.gen.source.sample": {"uz": "Namuna ({count} ta yozuv)", "en": "A sample ({count} records)"},
    "gui.validatepanel.gen.source.sample.tip": {
        "uz": "Tez, lekin taxminiy: faqat namunadagi yozuvlar ko'riladi.",
        "en": "Fast but approximate: only the records of the sample are looked at.",
    },
    "gui.validatepanel.gen.sample_warning": {
        "uz": "Diqqat: namuna asosidagi schema hamma yozuvlarni hisobga olmaydi — kam uchraydigan maydonlar tushib "
              "qolishi, ba'zi maydonlar esa noto'g'ri \"majburiy\" (required) deb belgilanishi mumkin. Aniq schema "
              "uchun to'liq skanerlashni tanlang.",
        "en": "Note: a schema made from a sample does not take every record into account: rare fields may be "
              "missing and some fields may wrongly be marked \"required\". Choose the full scan for an exact "
              "schema.",
    },
    "gui.validatepanel.gen.level": {"uz": "Schema nimani tasvirlaydi", "en": "The schema describes"},
    "gui.validatepanel.gen.level.document": {"uz": "Butun hujjatni", "en": "The whole document"},
    "gui.validatepanel.gen.level.document.tip": {
        "uz": "Massiv uchun \"items\", kalitli obyekt uchun \"additionalProperties\", o'ralgan hujjatda o'rovchi "
              "obyekt bilan birga. JSON Lines va bitta qiymatli faylda bitta yozuv schemasi bilan bir xil.",
        "en": "\"items\" for an array, \"additionalProperties\" for a keyed object, with the wrapper object for a "
              "wrapped document. For JSON Lines and single-value files it is the same as the record schema.",
    },
    "gui.validatepanel.gen.level.record": {"uz": "Bitta yozuvni", "en": "One record"},
    "gui.validatepanel.gen.level.record.tip": {
        "uz": "Har bir yozuv shu schemaga mos kelishi kerak (masalan, API javobidagi bitta element).",
        "en": "Every record must match this schema (e.g. one item of an API response).",
    },
    "gui.validatepanel.gen.strict": {"uz": "Qat'iy cheklovlar", "en": "Strict constraints"},
    "gui.validatepanel.gen.strict.tip": {
        "uz": "Uzunlik va son oraliqlari (min/max), kam qiymatli maydonlar uchun enum, formatlar (sana, email, "
              "URI…) va ortiqcha maydonlarni taqiqlash. Buning uchun hujjat yana bir marta o'qiladi.",
        "en": "Length and number ranges (min/max), enum for fields with few values, formats (date, email, URI…) "
              "and no extra members. The document is read once more for this.",
    },
    "gui.validatepanel.gen.title_label": {"uz": "&Sarlavha (title):", "en": "&Title:"},
    "gui.validatepanel.gen.save": {"uz": "Saqlash…", "en": "Save…"},
    "gui.validatepanel.gen.save.tip": {
        "uz": "Schema faylini tanlab, yaratishni boshlash",
        "en": "Choose the schema file and start",
    },
    "gui.validatepanel.gen.save_title": {"uz": "Schemani saqlash", "en": "Save the schema"},
    "gui.validatepanel.gen.stop": {"uz": "To'xtatish", "en": "Stop"},
    "gui.validatepanel.gen.open": {"uz": "Faylni ochish", "en": "Open the file"},
    "gui.validatepanel.gen.open.tip": {
        "uz": "Yozilgan schemani yangi varaqda ochish",
        "en": "Open the written schema in a new tab",
    },
    "gui.validatepanel.gen.task": {"uz": "Schema yaratilmoqda: {name}", "en": "Generating a schema: {name}"},
    "gui.validatepanel.gen.progress.scan": {
        "uz": "Tuzilma tahlil qilinmoqda: {name}",
        "en": "Analysing the structure of {name}",
    },
    "gui.validatepanel.gen.progress.strict": {
        "uz": "Qat'iy cheklovlar uchun qiymatlar o'qilmoqda: {name}",
        "en": "Reading the values for the strict constraints: {name}",
    },
    "gui.validatepanel.gen.progress.write": {"uz": "Yozilmoqda: {name}", "en": "Writing {name}"},
    "gui.validatepanel.gen.running": {"uz": "Schema yaratilmoqda…", "en": "Generating the schema…"},
    "gui.validatepanel.gen.done": {
        "uz": "Schema yozildi: {path} ({size}; {fields} ta maydon; {records} ta yozuv asosida)",
        "en": "Schema written: {path} ({size}; {fields} fields; based on {records} records)",
    },
    "gui.validatepanel.gen.done_sample": {
        "uz": "Diqqat: schema namuna asosida ({records} / {total} ta yozuv).",
        "en": "Note: the schema is based on a sample ({records} of {total} records).",
    },
    "gui.validatepanel.gen.failed": {"uz": "Schema yaratilmadi: {error}", "en": "The schema was not generated: {error}"},
    "gui.validatepanel.gen.cancelled": {"uz": "To'xtatildi; fayl yozilmadi.", "en": "Stopped; no file was written."},
    "gui.validatepanel.gen.no_document": {"uz": "Hujjat ochilmagan.", "en": "No document is open."},
    "gui.validatepanel.gen.status": {"uz": "Schema yozildi: {path}", "en": "Schema written: {path}"},
    "gui.validatepanel.same_file.hint": {
        "uz": "Manba faylning ustiga yozib bo'lmaydi: boshqa nom tanlang.",
        "en": "The source file cannot be overwritten: choose another name.",
    },
    "gui.validatepanel.title": {"uz": "Schema bo'yicha tekshirish — {name}", "en": "Validate against a schema — {name}"},
    "gui.validatepanel.schema.label": {"uz": "&Schema:", "en": "&Schema:"},
    "gui.validatepanel.schema.placeholder": {
        "uz": "JSON Schema fayli (draft 2020-12)",
        "en": "A JSON Schema file (draft 2020-12)",
    },
    "gui.validatepanel.schema.browse": {"uz": "Tanlash…", "en": "Browse…"},
    "gui.validatepanel.schema.browse_title": {"uz": "JSON Schema faylini tanlash", "en": "Choose a JSON Schema file"},
    "gui.validatepanel.schema.filter": {
        "uz": "JSON Schema (*.schema.json *.json);;Barcha fayllar (*)",
        "en": "JSON Schema (*.schema.json *.json);;All files (*)",
    },
    "gui.validatepanel.schema.checking": {"uz": "Schema tekshirilmoqda…", "en": "Checking the schema…"},
    "gui.validatepanel.schema.ok": {
        "uz": "Schema yaroqli ({count} ta qism-schema).",
        "en": "The schema is valid ({count} subschemas).",
    },
    "gui.validatepanel.schema.ok_title": {
        "uz": "Schema yaroqli: \"{title}\" ({count} ta qism-schema).",
        "en": "The schema is valid: \"{title}\" ({count} subschemas).",
    },
    "gui.validatepanel.schema.need": {"uz": "Avval schema faylini tanlang.", "en": "Choose a schema file first."},
    "gui.validatepanel.schema_path_tip": {"uz": "Schemadagi joyi: {pointer}", "en": "Place in the schema: {pointer}"},
    "gui.validatepanel.task.schema": {"uz": "Schema fayli tekshirilmoqda", "en": "Checking a schema file"},
    "gui.validatepanel.scope.label": {"uz": "&Qo'llanish:", "en": "&Applies to:"},
    "gui.validatepanel.scope.auto": {"uz": "Avtomatik", "en": "Automatic"},
    "gui.validatepanel.scope.record": {"uz": "Har bir yozuvga", "en": "Each record"},
    "gui.validatepanel.scope.document": {"uz": "Butun hujjatga", "en": "The whole document"},
    "gui.validatepanel.scope.tip": {
        "uz": "Avtomatik: schema butun hujjatni (masalan, massivni) yoki bitta yozuvni tasvirlashi o'zi aniqlanadi.",
        "en": "Automatic: whether the schema describes the whole document (e.g. the array) or one record is "
              "detected.",
    },
    "gui.validatepanel.source.label": {"uz": "&Yozuvlar:", "en": "&Records:"},
    "gui.validatepanel.source.all": {"uz": "Barcha yozuvlar", "en": "All records"},
    "gui.validatepanel.source.view": {"uz": "Joriy ko'rinish", "en": "The current view"},
    "gui.validatepanel.source.view_label": {
        "uz": "Joriy ko'rinish — {label} ({count} ta)",
        "en": "The current view — {label} ({count})",
    },
    "gui.validatepanel.source.view.tip": {
        "uz": "\"Joriy ko'rinish\": filtr, tartiblash yoki qidiruv natijasidagi yozuvlar ({label}).",
        "en": "\"The current view\": the records of the filter, sort or search result ({label}).",
    },
    "gui.validatepanel.source.single": {
        "uz": "Bitta qiymatli hujjat: butun hujjat tekshiriladi.",
        "en": "A single-value document: the whole document is validated.",
    },
    "gui.validatepanel.formats": {"uz": "Formatlarni tekshirish", "en": "Check formats"},
    "gui.validatepanel.formats.tip": {
        "uz": "\"format\" kalit so'zini talab sifatida tekshirish: date-time, date, time, email, uri, uuid, ipv4, "
              "ipv6 (aks holda u faqat izoh hisoblanadi).",
        "en": "Assert the \"format\" keyword: date-time, date, time, email, uri, uuid, ipv4, ipv6 (otherwise it is "
              "only an annotation).",
    },
    "gui.validatepanel.max_issues": {"uz": "&Ko'pi bilan xato:", "en": "&Issues listed at most:"},
    "gui.validatepanel.max_issues.tip": {
        "uz": "Ro'yxatda saqlanadigan xatolar soni; hammasi sanaladi va eksportga kiradi.",
        "en": "Issues kept in the list; all of them are counted and exported.",
    },
    "gui.validatepanel.per_record": {"uz": "&Har yozuvda:", "en": "&Per record:"},
    "gui.validatepanel.per_record.tip": {
        "uz": "Bitta yozuvdan ko'pi bilan nechta xato yig'iladi (yozuv baribir xato deb belgilanadi).",
        "en": "Issues collected per record at most (the record is marked invalid anyway).",
    },
    "gui.validatepanel.run": {"uz": "Tekshirish", "en": "Validate"},
    "gui.validatepanel.run.tip": {
        "uz": "Hujjatni schema bo'yicha tekshirish (fonda; Enter)",
        "en": "Validate the document against the schema (in the background; Enter)",
    },
    "gui.validatepanel.cancel": {"uz": "To'xtatish", "en": "Stop"},
    "gui.validatepanel.task": {"uz": "Schema bo'yicha tekshirilmoqda: {name}", "en": "Validating {name}"},
    "gui.validatepanel.progress.schema": {"uz": "Schema o'qilmoqda: {name}", "en": "Reading the schema {name}"},
    "gui.validatepanel.col.record": {"uz": "Yozuv", "en": "Record"},
    "gui.validatepanel.col.path": {"uz": "Yo'l", "en": "Path"},
    "gui.validatepanel.col.message": {"uz": "Xato", "en": "Problem"},
    "gui.validatepanel.col.keyword": {"uz": "Kalit so'z", "en": "Keyword"},
    "gui.validatepanel.document": {"uz": "hujjat", "en": "document"},
    "gui.validatepanel.empty": {"uz": "Xatolar shu yerda chiqadi", "en": "The problems appear here"},
    "gui.validatepanel.empty_ok": {"uz": "Xato topilmadi", "en": "No problems found"},
    "gui.validatepanel.live": {
        "uz": "Tekshirilmoqda… {issues} ta xato topildi",
        "en": "Validating… {issues} problems found",
    },
    "gui.validatepanel.running": {"uz": "Tekshirilmoqda…", "en": "Validating…"},
    "gui.validatepanel.summary.ok": {
        "uz": "{scanned} ta yozuv tekshirildi: hammasi schemaga mos ({seconds}).",
        "en": "{scanned} records validated: all match the schema ({seconds}).",
    },
    "gui.validatepanel.summary.bad": {
        "uz": "{scanned} ta yozuv tekshirildi: {invalid} tasi schemaga mos emas, jami {issues} ta xato ({seconds}).",
        "en": "{scanned} records validated: {invalid} do not match the schema, {issues} problems in all "
              "({seconds}).",
    },
    "gui.validatepanel.summary.scope.record": {
        "uz": "Schema har bir yozuvga qo'llandi.",
        "en": "The schema was applied to each record.",
    },
    "gui.validatepanel.summary.scope.document": {
        "uz": "Schema butun hujjatga qo'llandi.",
        "en": "The schema was applied to the whole document.",
    },
    "gui.validatepanel.summary.view": {
        "uz": "Faqat joriy ko'rinishdagi yozuvlar: {label}.",
        "en": "Only the records of the current view: {label}.",
    },
    "gui.validatepanel.summary.formats_on": {"uz": "Formatlar ham tekshirildi.", "en": "Formats were checked too."},
    "gui.validatepanel.summary.document": {
        "uz": "Hujjatning o'ziga tegishli xatolar: {count}",
        "en": "Problems of the document itself: {count}",
    },
    "gui.validatepanel.summary.keywords": {"uz": "Kalit so'zlar bo'yicha: {items}", "en": "By keyword: {items}"},
    "gui.validatepanel.summary.paths": {"uz": "Ko'p uchragan yo'llar: {items}", "en": "Most frequent paths: {items}"},
    "gui.validatepanel.summary.item": {"uz": "{name} ({count})", "en": "{name} ({count})"},
    "gui.validatepanel.cancelled": {
        "uz": "To'xtatildi: tekshiruv to'liq emas ({issues} ta xato ko'rsatildi).",
        "en": "Stopped: the validation is incomplete ({issues} problems listed).",
    },
    "gui.validatepanel.failed": {"uz": "Tekshirib bo'lmadi: {error}", "en": "The validation failed: {error}"},
    "gui.validatepanel.stale": {
        "uz": "Fayl qayta ochildi: natijalar o'chirildi, qaytadan tekshiring.",
        "en": "The file was reopened: the results were cleared, validate again.",
    },
    "gui.validatepanel.stale_edit": {
        "uz": "Hujjat o'zgardi va yozuvlar joyi siljidi: natijalar o'chirildi, qaytadan tekshiring.",
        "en": "The document changed and records moved: the results were cleared, validate again.",
    },
    "gui.validatepanel.stale_running": {
        "uz": "Tekshiruv paytida hujjat o'zgardi: tekshiruv to'xtatildi, qaytadan tekshiring.",
        "en": "The document changed during the validation: it was stopped, validate again.",
    },
    "gui.validatepanel.outdated": {
        "uz": "Hujjat tekshiruvdan keyin o'zgartirildi: natijalar eskirgan bo'lishi mumkin, qaytadan tekshiring.",
        "en": "The document was edited after the validation: the results may be outdated, validate again.",
    },
    "gui.validatepanel.no_document": {"uz": "Hujjat ochilmagan.", "en": "No document is open."},
    "gui.validatepanel.view_busy": {
        "uz": "Ko'rinish (filtr yoki tartiblash) hali hisoblanmoqda; tugashini kuting.",
        "en": "The view (filter or sort) is still being computed; wait until it is done.",
    },
    "gui.validatepanel.whole_document": {
        "uz": "Bu xato butun hujjatga (yozuvlar to'plamiga) tegishli: daraxtda unga alohida tugun yo'q.",
        "en": "This problem concerns the whole document (the set of records): it has no node of its own in the "
              "tree.",
    },
    "gui.validatepanel.records": {"uz": "Xatoli yozuvlarni ko'rsatish", "en": "Show the invalid records"},
    "gui.validatepanel.records.tip": {
        "uz": "Schemaga mos kelmagan yozuvlarni ko'rinish sifatida ochish (filtr va tartiblash ularga qo'llanadi)",
        "en": "Show the records that do not match the schema as the view (filter and sort then work on them)",
    },
    "gui.validatepanel.records.need": {
        "uz": "Avval tekshiruvni oxirigacha bajaring.",
        "en": "Run a complete validation first.",
    },
    "gui.validatepanel.records.none": {"uz": "Xatoli yozuv yo'q.", "en": "There are no invalid records."},
    "gui.validatepanel.records.outdated": {
        "uz": "Hujjat tekshiruvdan keyin o'zgardi: qaytadan tekshiring.",
        "en": "The document changed after the validation: validate again.",
    },
    "gui.validatepanel.records.single": {
        "uz": "Bitta qiymatli hujjatda yozuvlar ko'rinishi yo'q.",
        "en": "A single-value document has no record view.",
    },
    "gui.validatepanel.records_label": {"uz": "Schemaga mos emas: {name}", "en": "Not matching {name}"},
    "gui.validatepanel.task.records": {
        "uz": "Xatoli yozuvlar tayyorlanmoqda",
        "en": "Preparing the invalid records",
    },
    "gui.validatepanel.export": {"uz": "Eksport…", "en": "Export…"},
    "gui.validatepanel.export.tip": {
        "uz": "Barcha xatolarni (faqat ro'yxatdagilarni emas) CSV yoki JSON Lines faylga yozish: hujjat joriy "
              "sozlamalar bilan qaytadan tekshiriladi.",
        "en": "Write every problem (not only the listed ones) to a CSV or JSON Lines file: the document is "
              "validated again with the current settings.",
    },
    "gui.validatepanel.export.title": {"uz": "Xatolarni eksport qilish", "en": "Export the problems"},
    "gui.validatepanel.export.filter": {
        "uz": "CSV (*.csv);;JSON Lines (*.jsonl)",
        "en": "CSV (*.csv);;JSON Lines (*.jsonl)",
    },
    "gui.validatepanel.export.task": {
        "uz": "Schema xatolari eksport qilinmoqda: {name}",
        "en": "Exporting the schema problems: {name}",
    },
    "gui.validatepanel.export.done": {"uz": "{count} ta xato yozildi: {path}", "en": "{count} problems written to {path}"},
    "gui.validatepanel.export.bad_format": {
        "uz": "Faqat .csv yoki .jsonl fayl yozish mumkin: {name}",
        "en": "Only a .csv or .jsonl file can be written: {name}",
    },
    "gui.validatepanel.menu.show": {"uz": "Daraxtda ko'rsatish", "en": "Show in the tree"},
    "gui.validatepanel.menu.copy_message": {"uz": "Xato matnini nusxalash", "en": "Copy the message"},
    "gui.validatepanel.menu.copy_path": {"uz": "Yo'lni nusxalash", "en": "Copy the path"},
    "gui.validatepanel.menu.copy_rows": {"uz": "Tanlangan qatorlarni nusxalash", "en": "Copy the selected rows"},
    # ---- gui.repairdialog ----
    "gui.repairdialog.menu.repair": {"uz": "JSON'ni ta'mirlash…", "en": "Repair JSON…"},
    "gui.repairdialog.menu.repair.tip": {
        "uz": "Buzilgan JSON fayldan to'g'ri JSON nusxa yasash (manba fayl o'zgarmaydi)",
        "en": "Make a valid copy of a broken JSON file (the source file is not changed)",
    },
    "gui.repairdialog.bar.repair": {"uz": "Ta'mirlash…", "en": "Repair…"},
    "gui.repairdialog.bar.brace_list": {"uz": "Standart JSON'ga aylantirish…", "en": "Convert to standard JSON…"},
    "gui.repairdialog.title": {"uz": "JSON'ni ta'mirlash — {name}", "en": "Repair JSON — {name}"},
    "gui.repairdialog.title_none": {"uz": "JSON'ni ta'mirlash", "en": "Repair JSON"},
    "gui.repairdialog.intro": {
        "uz": "Buzilgan JSON fayldan to'g'ri JSON nusxa yasaladi. Manba fayl o'zgarmaydi; har bir tuzatish "
              "hisobotda ko'rsatiladi, hech narsa jimgina o'zgartirilmaydi.",
        "en": "A valid JSON copy is made of a broken JSON file. The source file is not changed; every fix is "
              "listed in the report, nothing is changed silently.",
    },
    "gui.repairdialog.browse": {"uz": "Tanlash…", "en": "Browse…"},
    "gui.repairdialog.source.label": {"uz": "&Manba:", "en": "&Source:"},
    "gui.repairdialog.source.title": {"uz": "Ta'mirlanadigan faylni tanlash", "en": "Choose the file to repair"},
    "gui.repairdialog.source.filter": {
        "uz": "JSON fayllar (*.json *.jsonl *.ndjson *.geojson *.gz *.txt);;Barcha fayllar (*)",
        "en": "JSON files (*.json *.jsonl *.ndjson *.geojson *.gz *.txt);;All files (*)",
    },
    "gui.repairdialog.source.need": {"uz": "Ta'mirlanadigan faylni tanlang.", "en": "Choose the file to repair."},
    "gui.repairdialog.target.label": {"uz": "&Natija:", "en": "&Output:"},
    "gui.repairdialog.target.title": {"uz": "Ta'mirlangan faylni saqlash", "en": "Save the repaired file"},
    "gui.repairdialog.target.filter": {
        "uz": "JSON (*.json);;JSON Lines (*.jsonl);;Siqilgan JSON (*.gz);;Barcha fayllar (*)",
        "en": "JSON (*.json);;JSON Lines (*.jsonl);;Compressed JSON (*.gz);;All files (*)",
    },
    "gui.repairdialog.same_file.hint": {
        "uz": "Ta'mirlangan nusxa hech qachon manba faylning ustiga yozilmaydi: boshqa nom tanlang.",
        "en": "The repaired copy is never written over the source file: choose another name.",
    },
    "gui.repairdialog.overwrite": {
        "uz": "{path} allaqachon bor. Ustiga yozilsinmi?",
        "en": "{path} already exists. Overwrite it?",
    },
    "gui.repairdialog.options": {"uz": "Parametrlar", "en": "Options"},
    "gui.repairdialog.format.label": {"uz": "&Format:", "en": "&Format:"},
    "gui.repairdialog.format.auto": {"uz": "Avtomatik aniqlash", "en": "Detect automatically"},
    "gui.repairdialog.format.tip": {
        "uz": "Buzilgan fayl boshida format noto'g'ri aniqlanishi mumkin; kerak bo'lsa shu yerda ko'rsating.",
        "en": "The format of a broken file may be detected wrongly; choose it here when needed.",
    },
    "gui.repairdialog.record_path.label": {"uz": "&Yozuvlar yo'li:", "en": "&Record path:"},
    "gui.repairdialog.record_path.placeholder": {
        "uz": "masalan: data.items (ixtiyoriy)",
        "en": "e.g. data.items (optional)",
    },
    "gui.repairdialog.record_path.invalid": {
        "uz": "Yozuvlar yo'li noto'g'ri: {error}",
        "en": "Invalid record path: {error}",
    },
    "gui.repairdialog.nan.label": {"uz": "NaN / &Infinity:", "en": "NaN / &Infinity:"},
    "gui.repairdialog.nan.null": {"uz": "null ga almashtirish", "en": "replace with null"},
    "gui.repairdialog.nan.string": {"uz": "matn sifatida saqlash (\"NaN\")", "en": "keep as text (\"NaN\")"},
    "gui.repairdialog.jsonl": {
        "uz": "Ketma-ket qiymatlarni JSON Lines qilib yozish",
        "en": "Write concatenated values as JSON Lines",
    },
    "gui.repairdialog.jsonl.tip": {
        "uz": "Faqat bir-biridan keyin kelgan qiymatlar yoki JSON Lines fayl uchun; aks holda natija massiv "
              "bo'ladi.",
        "en": "Only for values one after another or a JSON Lines file; otherwise the result is an array.",
    },
    "gui.repairdialog.partial": {
        "uz": "Ta'mirlab bo'lmaydigan joydan oldingi yozuvlarni saqlash",
        "en": "Keep the records before an unrepairable place",
    },
    "gui.repairdialog.partial.tip": {
        "uz": "O'chiq bo'lsa, ta'mirlab bo'lmaydigan muammo topilganda hech narsa yozilmaydi.",
        "en": "When off, nothing is written if an unrepairable problem is found.",
    },
    "gui.repairdialog.check": {"uz": "Tekshirish", "en": "Check"},
    "gui.repairdialog.check.tip": {
        "uz": "Faylni tekshirish: nima tuzatilishini ko'rsatadi, hech narsa yozmaydi",
        "en": "Check the file: shows what would be fixed, writes nothing",
    },
    "gui.repairdialog.repair": {"uz": "Ta'mirlash", "en": "Repair"},
    "gui.repairdialog.repair.tip": {
        "uz": "Ta'mirlangan nusxani natija fayliga yozish",
        "en": "Write the repaired copy to the output file",
    },
    "gui.repairdialog.stop": {"uz": "To'xtatish", "en": "Stop"},
    "gui.repairdialog.task.check": {"uz": "Tekshirilmoqda: {name}", "en": "Checking {name}"},
    "gui.repairdialog.task.repair": {"uz": "Ta'mirlanmoqda: {name}", "en": "Repairing {name}"},
    "gui.repairdialog.progress": {"uz": "{done} / {total} · {rate}/s", "en": "{done} / {total} · {rate}/s"},
    "gui.repairdialog.progress_eta": {
        "uz": "{done} / {total} · {rate}/s · {eta} qoldi",
        "en": "{done} / {total} · {rate}/s · {eta} left",
    },
    "gui.repairdialog.cancelled": {"uz": "To'xtatildi; hech narsa yozilmadi.", "en": "Stopped; nothing was written."},
    "gui.repairdialog.failed": {"uz": "Bajarilmadi: {error}", "en": "Failed: {error}"},
    "gui.repairdialog.status.written": {"uz": "Ta'mirlandi: {path}", "en": "Repaired: {path}"},
    "gui.repairdialog.confirm_stop": {
        "uz": "Ta'mirlash hali tugamagan. U to'xtatilib, oyna yopilsinmi? (Hech narsa yozilmaydi.)",
        "en": "The repair has not finished. Stop it and close the window? (Nothing is written.)",
    },
    "gui.repairdialog.kinds": {"uz": "Format: {kind} → natija: {output}", "en": "Format: {kind} → output: {output}"},
    "gui.repairdialog.kinds.record_path": {"uz": "yozuvlar yo'li: {path}", "en": "record path: {path}"},
    "gui.repairdialog.col.category": {"uz": "Turi", "en": "Kind"},
    "gui.repairdialog.col.fix": {"uz": "Tuzatish", "en": "Fix"},
    "gui.repairdialog.col.count": {"uz": "Soni", "en": "Count"},
    "gui.repairdialog.col.first": {"uz": "Birinchi joyi", "en": "First place"},
    "gui.repairdialog.col.line": {"uz": "Qator", "en": "Line"},
    "gui.repairdialog.col.column": {"uz": "Ustun", "en": "Column"},
    "gui.repairdialog.col.before": {"uz": "Oldin", "en": "Before"},
    "gui.repairdialog.col.after": {"uz": "Keyin", "en": "After"},
    "gui.repairdialog.col.record": {"uz": "Yozuv", "en": "Record"},
    "gui.repairdialog.col.record.tip": {
        "uz": "Ta'mirlangan fayldagi yozuv raqami (0 dan)",
        "en": "The record number in the repaired file (from 0)",
    },
    "gui.repairdialog.location": {"uz": "{line}-qator, {column}-ustun", "en": "line {line}, column {column}"},
    "gui.repairdialog.location.tip": {
        "uz": "Manba faylning {offset}-bayti; ko'rish uchun ikki marta bosing yoki Enter",
        "en": "Byte {offset} of the source file; double-click or press Enter to see it",
    },
    "gui.repairdialog.nothing": {"uz": "(hech narsa)", "en": "(nothing)"},
    "gui.repairdialog.summary.empty": {"uz": "Tuzatish kerak bo'lmadi", "en": "Nothing needed fixing"},
    "gui.repairdialog.fixes.title": {"uz": "Barcha tuzatishlar ({count} ta):", "en": "Every fix ({count}):"},
    "gui.repairdialog.fixes.shown": {
        "uz": "Ro'yxatda dastlabki {shown} ta tuzatish (jami {total} ta); hammasini \"Hisobotni saqlash\" orqali "
              "olish mumkin.",
        "en": "The first {shown} fixes are listed ({total} in all); save the report to get all of them.",
    },
    "gui.repairdialog.fixes.empty": {"uz": "Tuzatishlar shu yerda chiqadi", "en": "The fixes appear here"},
    "gui.repairdialog.truncated.title": {"uz": "Fayl kesilgan", "en": "The file is cut off"},
    "gui.repairdialog.truncated.text": {
        "uz": "Ma'lumot {line}-qator, {column}-ustunda uziladi. Oxirgi to'liq yozuvgacha saqlandi: {kept} ta "
              "yozuv; tashlab yuborildi: {dropped}; oxiriga qo'shildi: {closers}",
        "en": "The data breaks off at line {line}, column {column}. Kept up to the last complete record: {kept} "
              "records; dropped: {dropped}; appended: {closers}",
    },
    "gui.repairdialog.problem.title": {"uz": "Ta'mirlab bo'lmaydigan joy", "en": "An unrepairable place"},
    "gui.repairdialog.problem.nothing_written": {"uz": "Hech narsa yozilmadi.", "en": "Nothing was written."},
    "gui.repairdialog.show_location": {"uz": "Joyini ko'rsatish", "en": "Show the place"},
    "gui.repairdialog.show_location.tip": {
        "uz": "Manba faylning xom ko'rinishida shu joyni ochish",
        "en": "Open this place in the raw view of the source file",
    },
    "gui.repairdialog.show_location.none": {
        "uz": "Manba fayl ochiq varaqda emas: joyini ko'rsatib bo'lmaydi.",
        "en": "The source file is not open in a tab: the place cannot be shown.",
    },
    "gui.repairdialog.save_partial": {"uz": "Qisman natijani saqlash", "en": "Save the partial result"},
    "gui.repairdialog.save_partial.tip": {
        "uz": "Muammodan oldingi to'liq yozuvlarni natija fayliga yozish (to'g'ri yopilgan holda)",
        "en": "Write the complete records before the problem to the output file (properly closed)",
    },
    "gui.repairdialog.save_report": {"uz": "Hisobotni saqlash…", "en": "Save the report…"},
    "gui.repairdialog.save_report.title": {"uz": "Ta'mirlash hisobotini saqlash", "en": "Save the repair report"},
    "gui.repairdialog.save_report.filter": {
        "uz": "CSV (*.csv);;JSON (*.json);;JSON Lines (*.jsonl)",
        "en": "CSV (*.csv);;JSON (*.json);;JSON Lines (*.jsonl)",
    },
    "gui.repairdialog.save_report.task": {
        "uz": "Ta'mirlash hisoboti yozilmoqda: {name}",
        "en": "Writing the repair report: {name}",
    },
    "gui.repairdialog.save_report.done": {"uz": "Hisobot yozildi: {path}", "en": "Report written: {path}"},
    "gui.repairdialog.save_report.none": {"uz": "Avval tekshiring yoki ta'mirlang.", "en": "Check or repair first."},
    "gui.repairdialog.save_report.bad_format": {
        "uz": "Hisobot faqat .csv, .json yoki .jsonl faylga yoziladi: {name}",
        "en": "The report is written only to a .csv, .json or .jsonl file: {name}",
    },
    "gui.repairdialog.open": {"uz": "Ta'mirlangan faylni ochish", "en": "Open the repaired file"},
    "gui.repairdialog.open.tip": {"uz": "Yangi varaqda ochish", "en": "Open it in a new tab"},
    # ---- gui.filetools ----
    # split / merge dialogs (gui/filetoolsdialog.py, docs/ARCHITECTURE.md §14.9); the action texts are for the menus
    "gui.filetools.action.split": {"uz": "Faylni bo'lish…", "en": "Split file…"},
    "gui.filetools.action.split.tip": {
        "uz": "Faylni yoki joriy ko'rinishni bir nechta kichikroq faylga bo'lish",
        "en": "Split the file or the current view into several smaller files",
    },
    "gui.filetools.action.split_node": {"uz": "Shu tugunni qismlarga bo'lish…", "en": "Split this node into parts…"},
    "gui.filetools.action.split_node.tip": {
        "uz": "Shu massiv elementlarini (yoki obyekt a'zolarini) bir nechta faylga bo'lish",
        "en": "Split the elements of this array (or the members of this object) into several files",
    },
    "gui.filetools.action.merge": {"uz": "Fayllarni birlashtirish…", "en": "Merge files…"},
    "gui.filetools.action.merge.tip": {
        "uz": "Bir nechta JSON / JSON Lines fayl va ochiq tablarni bitta faylga birlashtirish",
        "en": "Merge several JSON / JSON Lines files and open tabs into one file",
    },
    "gui.filetools.choose": {"uz": "Tanlash…", "en": "Choose…"},
    "gui.filetools.close.tip": {
        "uz": "Oynani yopish; boshlangan ish davom etadi (Vazifalar panelida) va natijasi baribir ko'rsatiladi",
        "en": "Close the window; a started job goes on (in the Tasks panel) and its result is still shown",
    },
    "gui.filetools.stop": {"uz": "To'xtatish", "en": "Stop"},
    "gui.filetools.stop.tip": {
        "uz": "Ishni to'xtatish: hech qanday fayl yozilmaydi, mavjud fayllar o'zgarmaydi",
        "en": "Stop the job: no file is written, existing files stay as they were",
    },
    "gui.filetools.more_names": {"uz": "va yana {count} ta", "en": "and {count} more"},
    "gui.filetools.format.same": {"uz": "Asl formatda (baytma-bayt nusxa)", "en": "Original format (byte-for-byte copy)"},
    "gui.filetools.format.same.tip": {
        "uz": "Qismlar faylning o'z turida, kodlashida va siqilishida (gzip) yoziladi; yozuvlar baytma-bayt "
              "ko'chiriladi. Joriy ko'rinish bo'linganda yozuvlar fayl formatida qayta yoziladi.",
        "en": "The parts keep the file's own kind, encoding and compression (gzip); the records are copied byte "
              "for byte. When the current view is split, its records are written again in the file's format.",
    },
    "gui.filetools.format.json": {"uz": "JSON", "en": "JSON"},
    "gui.filetools.format.jsonl": {"uz": "JSON Lines (har qatorda bitta yozuv)", "en": "JSON Lines (one record per line)"},
    "gui.filetools.format.csv": {"uz": "CSV (jadval)", "en": "CSV (table)"},
    "gui.filetools.format.xlsx": {"uz": "Excel (XLSX)", "en": "Excel (XLSX)"},
    "gui.filetools.format.sqlite": {"uz": "SQLite ma'lumotlar bazasi", "en": "SQLite database"},
    "gui.filetools.indent": {"uz": "Chekinish:", "en": "Indentation:"},
    "gui.filetools.indent.2": {"uz": "2 bo'shliq", "en": "2 spaces"},
    "gui.filetools.indent.4": {"uz": "4 bo'shliq", "en": "4 spaces"},
    "gui.filetools.indent.tab": {"uz": "Tab", "en": "Tab"},
    "gui.filetools.indent.compact": {"uz": "Ixcham (bo'shliqlarsiz)", "en": "Compact (no whitespace)"},
    "gui.filetools.indent.n": {"uz": "{n} bo'shliq", "en": "{n} spaces"},
    "gui.filetools.sort": {"uz": "Kalitlarni tartiblash:", "en": "Sort keys:"},
    "gui.filetools.sort.none": {"uz": "Yo'q (fayldagi tartib)", "en": "No (file order)"},
    "gui.filetools.sort.alpha": {"uz": "Alifbo bo'yicha", "en": "Alphabetically"},
    "gui.filetools.sort.codepoint": {"uz": "Kod nuqtasi bo'yicha", "en": "By code point"},
    "gui.filetools.sort.tip": {
        "uz": "Har bir yozuvdagi obyekt kalitlarini tartiblash (o'zbek alifbosi, raqamlar tabiiy tartibda); "
              "yozuvlarning o'z tartibi o'zgarmaydi",
        "en": "Sort the object keys inside every record (Uzbek collation, numbers in natural order); the order of "
              "the records does not change",
    },
    "gui.filetools.split.title": {"uz": "Faylni bo'lish — {name}", "en": "Split file — {name}"},
    "gui.filetools.split.source.title": {"uz": "Nimani bo'lish", "en": "What to split"},
    "gui.filetools.split.source.document": {
        "uz": "Butun fayl ({count} ta yozuv)",
        "en": "The whole file ({count} records)",
    },
    "gui.filetools.split.source.view": {
        "uz": "Joriy ko'rinish: {label} ({count} ta yozuv)",
        "en": "The current view: {label} ({count} records)",
    },
    "gui.filetools.split.source.view_all": {
        "uz": "Joriy ko'rinish (barcha {count} ta yozuv)",
        "en": "The current view (all {count} records)",
    },
    "gui.filetools.split.source.node": {"uz": "Tugun: {path}", "en": "Node: {path}"},
    "gui.filetools.split.source.node.tip": {
        "uz": "Tugundagi massiv elementlari (yoki obyekt a'zolari) qismlarga bo'linadi; buning uchun fayl shu "
              "tugun yozuvlar sifatida qayta o'qiladi.",
        "en": "The elements of the node's array (or the members of its object) are split into parts; for that the "
              "file is read again with this node as the records.",
    },
    "gui.filetools.split.mode.title": {"uz": "Qanday bo'lish", "en": "How to split"},
    "gui.filetools.split.mode.parts": {"uz": "Qismlar soni:", "en": "Number of parts:"},
    "gui.filetools.split.mode.records": {"uz": "Har qismdagi yozuvlar:", "en": "Records per part:"},
    "gui.filetools.split.mode.size": {"uz": "Qism hajmi:", "en": "Part size:"},
    "gui.filetools.split.balance.bytes": {"uz": "hajmi teng", "en": "equal size"},
    "gui.filetools.split.balance.records": {"uz": "yozuvlari soni teng", "en": "equal record counts"},
    "gui.filetools.split.balance.tip": {
        "uz": "Qismlar hajm (baytlar) yoki yozuvlar soni bo'yicha teng bo'linadi",
        "en": "The parts are balanced by size (bytes) or by the number of records",
    },
    "gui.filetools.split.size.placeholder": {"uz": "masalan: 100 MB", "en": "for example: 100 MB"},
    "gui.filetools.split.size.tip": {
        "uz": "Har bir qismning eng katta hajmi. Birliklar: KB, MB, GB (1 MB = 1024 KB); faqat son yozilsa — MB. "
              "Yozuvning o'zi bundan katta bo'lsa, u alohida qismga yoziladi.",
        "en": "The largest size of each part. Units: KB, MB, GB (1 MB = 1024 KB); a plain number means MB. A "
              "record larger than that by itself gets a part of its own.",
    },
    "gui.filetools.split.output.title": {"uz": "Natija", "en": "Result"},
    "gui.filetools.split.folder": {"uz": "&Papka:", "en": "&Folder:"},
    "gui.filetools.split.folder.title": {"uz": "Qismlar yoziladigan papka", "en": "Folder for the parts"},
    "gui.filetools.split.name": {"uz": "&Nom andozasi:", "en": "&Name pattern:"},
    "gui.filetools.split.name.tip": {
        "uz": "{stem} — fayl nomi, {n} — qism raqami (majburiy), {ext} — kengaytma. Masalan: {stem}_qism{n}{ext}",
        "en": "{stem} — the file name, {n} — the part number (required), {ext} — the extension. For example: "
              "{stem}_part{n}{ext}",
    },
    "gui.filetools.split.format": {"uz": "F&ormat:", "en": "F&ormat:"},
    "gui.filetools.split.keep_wrapper": {
        "uz": "Har bir qismda o'ram (wrapper) saqlansin",
        "en": "Keep the wrapper in every part",
    },
    "gui.filetools.split.keep_wrapper.tip": {
        "uz": "Yozuvlar massivini o'rab turgan obyekt (masalan, meta) har bir qismga yoziladi. Belgilanmasa, "
              "qismda faqat yozuvlar bo'ladi.",
        "en": "The object around the record array (for example meta) is written into every part. Unchecked: the "
              "parts hold the records only.",
    },
    "gui.filetools.split.keep_wrapper.only_wrapped": {
        "uz": "Faqat yozuvlari obyekt ichida joylashgan fayllar uchun",
        "en": "Only for files whose records are inside an object",
    },
    "gui.filetools.split.drop_broken": {"uz": "Buzuq qatorlarni tashlab yuborish", "en": "Leave out the broken lines"},
    "gui.filetools.split.drop_broken.tip": {
        "uz": "Faylda {count} ta buzuq qator bor: belgilansa, ular qismlarga yozilmaydi.",
        "en": "The file has {count} broken lines: when checked, they are not written to the parts.",
    },
    "gui.filetools.split.drop_broken.none": {"uz": "Faylda buzuq qatorlar yo'q", "en": "The file has no broken lines"},
    "gui.filetools.split.remove_stale": {"uz": "Eski qismlarni o'chirish", "en": "Remove the old parts"},
    "gui.filetools.split.remove_stale.tip": {
        "uz": "Belgilansa, papkada oldingi bo'lishdan qolgan qismlar yangi qismlar bilan bir vaqtda o'chiriladi: "
              "shu nom andozasidagi, raqamlari uzilishsiz davom etadigan fayllar (masalan, fayl avval 12 ta, endi "
              "10 ta qismga bo'linsa — _11 va _12). Hech qachon o'chirilmaydi: manba fayl, faqat o'qish uchun "
              "bo'lgan fayllar, dasturda saqlanmagan o'zgarishlari bor fayllar va oldingi bo'lishga o'xshamaydigan "
              "fayllar (masalan, x_2023.json yoki yolg'iz x_1.json) — ular faqat ogohlantirishda nomlanadi.",
        "en": "When checked, the parts that an earlier split left in the folder are deleted together with writing "
              "the new parts: files of the same name pattern whose numbers go on without a gap (for example _11 "
              "and _12 when a file split into 12 parts is now split into 10). Never deleted: the source file, "
              "read-only files, files with unsaved changes in this program and files that do not look like an "
              "earlier split (for example x_2023.json or a lone x_1.json) — those are only named in a warning.",
    },
    "gui.filetools.split.edited_note": {
        "uz": "Hujjatda saqlanmagan o'zgarishlar bor: ular ham qismlarga kiradi (faylning o'zi o'zgarmaydi).",
        "en": "The document has unsaved changes: the parts include them (the file itself is not changed).",
    },
    "gui.filetools.split.plan.title": {"uz": "Reja", "en": "Plan"},
    "gui.filetools.split.plan.pending": {"uz": "Reja tuzilmoqda…", "en": "Planning…"},
    "gui.filetools.split.plan.stale": {
        "uz": "Hujjat o'zgardi — reja qaytadan tuzilmoqda…",
        "en": "The document changed — planning again…",
    },
    "gui.filetools.split.plan.summary": {
        "uz": "{parts} ta qism · {records} ta yozuv · jami ≈ {size}",
        "en": "{parts} parts · {records} records · ≈ {size} in all",
    },
    "gui.filetools.split.plan.col.n": {"uz": "#", "en": "#"},
    "gui.filetools.split.plan.col.name": {"uz": "Fayl", "en": "File"},
    "gui.filetools.split.plan.col.range": {"uz": "Oraliq", "en": "Range"},
    "gui.filetools.split.plan.col.range.tip": {
        "uz": "Qismdagi birinchi va oxirgi yozuvning o'rni (0 dan; ko'rinish bo'linganda — ko'rinishdagi o'rni)",
        "en": "The positions of the part's first and last record (from 0; for a view: its positions in the view)",
    },
    "gui.filetools.split.plan.col.records": {"uz": "Yozuvlar", "en": "Records"},
    "gui.filetools.split.plan.col.size": {"uz": "Hajm", "en": "Size"},
    "gui.filetools.split.plan.range": {"uz": "#{first} – #{last}", "en": "#{first} – #{last}"},
    "gui.filetools.split.plan.more": {"uz": "… yana {count} ta qism", "en": "… {count} more parts"},
    "gui.filetools.split.plan.existing": {
        "uz": "{count} ta mavjud fayl almashtiriladi: {names}",
        "en": "{count} existing files will be replaced: {names}",
    },
    "gui.filetools.split.plan.space": {
        "uz": "Diskda joy yetmasligi mumkin: taxminan {need} kerak, bo'sh joy {free}.",
        "en": "The disk may not have enough space: about {need} is needed, {free} is free.",
    },
    "gui.filetools.split.plan_task": {"uz": "Bo'lish rejasi: {name}", "en": "Split plan: {name}"},
    "gui.filetools.split.run": {"uz": "&Bo'lish", "en": "&Split"},
    "gui.filetools.split.task": {"uz": "Bo'linmoqda: {name}", "en": "Splitting {name}"},
    "gui.filetools.split.progress.node": {"uz": "Tugun o'qilmoqda: {path}", "en": "Reading the node {path}"},
    "gui.filetools.split.overwrite": {
        "uz": "{count} ta fayl almashtiriladi:\n{names}\n\nDavom etilsinmi?",
        "en": "{count} files will be replaced:\n{names}\n\nContinue?",
    },
    "gui.filetools.split.overwrite_open": {
        "uz": "{count} ta fayl almashtiriladi:\n{names}\n\nUlardan {open} tasi boshqa tabda ochiq: {tabs}. O'sha "
              "tablar yangi fayllarni qayta ochadi.\n\nDavom etilsinmi?",
        "en": "{count} files will be replaced:\n{names}\n\n{open} of them are open in other tabs: {tabs}. Those "
              "tabs will open the new files again.\n\nContinue?",
    },
    "gui.filetools.error.bad_size": {
        "uz": "Qism hajmi noto'g'ri: «{text}». Misol: 100 MB, 1.5 GB, 500 KB.",
        "en": "Invalid part size: \"{text}\". For example: 100 MB, 1.5 GB, 500 KB.",
    },
    "gui.filetools.error.need_folder": {
        "uz": "Qismlar uchun papkani ko'rsating.",
        "en": "Choose the folder for the parts.",
    },
    "gui.filetools.error.not_dir": {"uz": "Bu papka emas: {path}", "en": "This is not a folder: {path}"},
    "gui.filetools.error.not_ready": {
        "uz": "Fayl hali ochilmoqda yoki indekslanmoqda; tugashini kuting.",
        "en": "The file is still being opened or indexed; wait until it is done.",
    },
    "gui.filetools.error.closed": {"uz": "Fayl yopilgan.", "en": "The file was closed."},
    "gui.filetools.error.view_busy": {
        "uz": "Ko'rinish hali hisoblanmoqda (filtr yoki tartiblash); tugashini kuting.",
        "en": "The view is still being computed (filter or sort); wait until it is done.",
    },
    "gui.filetools.error.view_changed": {
        "uz": "Ko'rinish o'zgarib ketdi; qaytadan urinib ko'ring.",
        "en": "The view changed meanwhile; try again.",
    },
    "gui.filetools.error.node_edited": {
        "uz": "Tugunni bo'lish uchun avval o'zgarishlarni saqlang: tugun fayldan qayta o'qiladi.",
        "en": "Save the changes first to split a node: the node is read from the file again.",
    },
    "gui.filetools.error.node_kind": {
        "uz": "«{kind}» faylidagi tugunni alohida bo'lib bo'lmaydi. Butun faylni yoki ko'rinishni bo'ling yoki "
              "tugunni avval alohida faylga ajrating.",
        "en": "A node of a \"{kind}\" file cannot be split on its own. Split the whole file or the view, or "
              "extract the node into a file of its own first.",
    },
    "gui.filetools.error.node_scalar": {
        "uz": "Tugun massiv yoki obyekt emas, uni bo'lib bo'lmaydi: {path}",
        "en": "The node is not an array or an object, so it cannot be split: {path}",
    },
    "gui.filetools.error.node_missing": {
        "uz": "Tugun faylda topilmadi: {path}",
        "en": "The node was not found in the file: {path}",
    },
    "gui.filetools.error.unexpected": {"uz": "Kutilmagan xato: {error}", "en": "Unexpected error: {error}"},
    "gui.filetools.hint.single": {
        "uz": "Daraxtda ichidagi massivni tanlang va uning menyusidan «Shu tugunni qismlarga bo'lish…» ni "
              "tanlang.",
        "en": "Select the array inside it in the tree and choose \"Split this node into parts…\" from its menu.",
    },
    "gui.filetools.hint.partial": {
        "uz": "Avval faylni ta'mirlang (Asboblar ▸ Ta'mirlash…), keyin ta'mirlangan faylni bo'ling.",
        "en": "Repair the file first (Tools ▸ Repair…), then split the repaired file.",
    },
    "gui.filetools.hint.broken": {
        "uz": "Ularsiz bo'lish uchun «Buzuq qatorlarni tashlab yuborish» ni belgilang.",
        "en": "Check \"Leave out the broken lines\" to split without them.",
    },
    "gui.filetools.hint.partial_input": {
        "uz": "Faylni ta'mirlang yoki «JSON xatosi bor fayllardan xatogacha bo'lgan yozuvlarni olish» ni "
              "belgilang.",
        "en": "Repair the file or check \"Take the records before the error from files with a JSON error\".",
    },
    "gui.filetools.hint.merge_format": {
        "uz": "Natijani .json yoki .jsonl faylga yozing, keyin uni Eksport bilan kerakli formatga o'giring.",
        "en": "Write the result to a .json or .jsonl file, then convert it with Export.",
    },
    "gui.filetools.merge.title": {"uz": "Fayllarni birlashtirish", "en": "Merge files"},
    "gui.filetools.merge.inputs": {
        "uz": "Birlashtiriladigan fayllar (shu tartibda)",
        "en": "Files to merge (in this order)",
    },
    "gui.filetools.merge.add_files": {"uz": "Fayl &qo'shish…", "en": "&Add files…"},
    "gui.filetools.merge.add_files.title": {
        "uz": "Birlashtiriladigan fayllarni tanlang",
        "en": "Choose the files to merge",
    },
    "gui.filetools.merge.add_tabs": {"uz": "Ochiq tablar", "en": "Open tabs"},
    "gui.filetools.merge.add_tabs.all": {"uz": "Barcha ochiq tablar", "en": "All open tabs"},
    "gui.filetools.merge.add_tabs.none": {"uz": "Ochiq tab yo'q", "en": "No open tabs"},
    "gui.filetools.merge.remove": {"uz": "Olib tashlash", "en": "Remove"},
    "gui.filetools.merge.remove.tip": {
        "uz": "Tanlangan faylni ro'yxatdan olib tashlash (Delete)",
        "en": "Remove the selected file from the list (Delete)",
    },
    "gui.filetools.merge.up": {"uz": "Yuqoriga", "en": "Move up"},
    "gui.filetools.merge.up.tip": {
        "uz": "Tanlangan faylni bitta yuqoriga surish (Alt+↑)",
        "en": "Move the selected file up (Alt+↑)",
    },
    "gui.filetools.merge.down": {"uz": "Pastga", "en": "Move down"},
    "gui.filetools.merge.down.tip": {
        "uz": "Tanlangan faylni bitta pastga surish (Alt+↓)",
        "en": "Move the selected file down (Alt+↓)",
    },
    "gui.filetools.merge.files_filter": {
        "uz": "JSON fayllar ({patterns});;Barcha fayllar (*)",
        "en": "JSON files ({patterns});;All files (*)",
    },
    "gui.filetools.merge.input.probing": {"uz": "{name} — tekshirilmoqda…", "en": "{name} — checking…"},
    "gui.filetools.merge.input.file": {
        "uz": "{name} — {kind} · {records} ta yozuv · {size}",
        "en": "{name} — {kind} · {records} records · {size}",
    },
    "gui.filetools.merge.input.partial": {
        "uz": "{name} — JSON xatosi ({line}-qator): xatogacha {records} ta yozuv bor",
        "en": "{name} — JSON error (line {line}): {records} records before it",
    },
    "gui.filetools.merge.input.failed": {
        "uz": "{name} — ochib bo'lmadi: {error}",
        "en": "{name} — cannot be opened: {error}",
    },
    "gui.filetools.merge.input.tab": {
        "uz": "{name} — ochiq tab · {kind} · {records} ta yozuv",
        "en": "{name} — open tab · {kind} · {records} records",
    },
    "gui.filetools.merge.input.tab_dirty": {
        "uz": "{name} — ochiq tab, saqlanmagan o'zgarishlar bilan · {kind} · {records} ta yozuv",
        "en": "{name} — open tab with unsaved changes · {kind} · {records} records",
    },
    "gui.filetools.merge.input.tab_wait": {
        "uz": "{name} — ochiq tab (hali indekslanmoqda…)",
        "en": "{name} — open tab (still being indexed…)",
    },
    "gui.filetools.merge.output.title": {"uz": "Natija", "en": "Result"},
    "gui.filetools.merge.output": {"uz": "&Fayl:", "en": "&File:"},
    "gui.filetools.merge.output.choose_title": {"uz": "Natija fayli", "en": "Result file"},
    "gui.filetools.merge.output_filter": {
        "uz": "JSON (*.json *.json.gz);;JSON Lines (*.jsonl *.ndjson *.jsonl.gz)",
        "en": "JSON (*.json *.json.gz);;JSON Lines (*.jsonl *.ndjson *.jsonl.gz)",
    },
    "gui.filetools.merge.format": {"uz": "Format:", "en": "Format:"},
    "gui.filetools.merge.keep_wrapper": {
        "uz": "Birinchi faylning o'rami (wrapper) saqlansin",
        "en": "Keep the first file's wrapper",
    },
    "gui.filetools.merge.keep_wrapper.tip": {
        "uz": "Birinchi faylning yozuvlari obyekt ichida bo'lsa (masalan, meta bilan), natija ham o'sha obyekt "
              "ichida yoziladi; boshqa fayllarning o'ramlari tashlab yuboriladi.",
        "en": "When the first file's records are inside an object (with meta, for example), the result is written "
              "inside that object too; the wrappers of the other files are dropped.",
    },
    "gui.filetools.merge.keep_wrapper.jsonl": {
        "uz": "JSON Lines faylda o'ram uchun joy yo'q",
        "en": "A JSON Lines file has no place for a wrapper",
    },
    "gui.filetools.merge.allow_partial": {
        "uz": "JSON xatosi bor fayllardan xatogacha bo'lgan yozuvlarni olish",
        "en": "Take the records before the error from files with a JSON error",
    },
    "gui.filetools.merge.allow_partial.tip": {
        "uz": "Belgilanmasa, JSON xatosi bor fayl birlashtirilmaydi (avval uni ta'mirlang)",
        "en": "Unchecked: a file with a JSON error is not merged (repair it first)",
    },
    "gui.filetools.merge.dedupe": {"uz": "Takroriy yozuvlarni olib tashlash", "en": "Remove duplicate records"},
    "gui.filetools.merge.dedupe.key": {"uz": "&Kalit:", "en": "&Key:"},
    "gui.filetools.merge.dedupe.key.tip": {
        "uz": "Yozuvlar shu maydon bo'yicha solishtiriladi (masalan, id yoki contact.phone). $ — butun yozuv, "
              "$key — a'zo kaliti (kalitli fayllarda). Sonlar qiymati bo'yicha, obyektlar esa kalitlar "
              "tartibidan qat'i nazar solishtiriladi.",
        "en": "The records are compared by this field (for example id or contact.phone). $ — the whole record, "
              "$key — the member key (keyed files). Numbers are compared by value, objects whatever the order of "
              "their keys.",
    },
    "gui.filetools.merge.dedupe.whole": {"uz": "Butun yozuv", "en": "The whole record"},
    "gui.filetools.merge.dedupe.member_key": {
        "uz": "A'zo kaliti (kalitli fayllar)",
        "en": "The member key (keyed files)",
    },
    "gui.filetools.merge.keep": {"uz": "Qoldirish:", "en": "Keep:"},
    "gui.filetools.merge.keep.first": {
        "uz": "birinchisini (oldingi fayl ustun)",
        "en": "the first (the earlier file wins)",
    },
    "gui.filetools.merge.keep.last": {"uz": "oxirgisini", "en": "the last"},
    "gui.filetools.merge.edited_note": {
        "uz": "Ochiq tablarning saqlanmagan o'zgarishlari ham birlashtiriladi: {names}",
        "en": "The unsaved changes of open tabs are merged too: {names}",
    },
    "gui.filetools.merge.warn.twice": {
        "uz": "{name} ro'yxatda bir necha marta bor.",
        "en": "{name} is listed more than once.",
    },
    "gui.filetools.merge.error.no_inputs": {
        "uz": "Birlashtirish uchun kamida bitta fayl qo'shing.",
        "en": "Add at least one file to merge.",
    },
    "gui.filetools.merge.error.need_output": {"uz": "Natija faylini ko'rsating.", "en": "Choose the result file."},
    "gui.filetools.merge.error.output_is_input": {
        "uz": "Natija fayli birlashtiriladigan fayllardan biri bo'la olmaydi: {path}",
        "en": "The result file cannot be one of the files to merge: {path}",
    },
    "gui.filetools.merge.error.input_failed": {
        "uz": "Faylni ochib bo'lmadi: {name}: {error}",
        "en": "The file cannot be opened: {name}: {error}",
    },
    "gui.filetools.merge.error.tab_not_ready": {
        "uz": "Ochiq tab hali tayyor emas (indekslanmoqda): {name}",
        "en": "An open tab is not ready yet (being indexed): {name}",
    },
    "gui.filetools.merge.error.need_key": {
        "uz": "Takroriylarni topish uchun kalitni yozing (masalan, id yoki $).",
        "en": "Enter the key to find duplicates by (for example id or $).",
    },
    "gui.filetools.merge.run": {"uz": "&Birlashtirish", "en": "&Merge"},
    "gui.filetools.merge.task": {"uz": "Birlashtirilmoqda: {name}", "en": "Merging into {name}"},
    "gui.filetools.merge.probe_task": {"uz": "Fayl tekshirilmoqda: {name}", "en": "Checking {name}"},
    "gui.filetools.merge.fields_task": {"uz": "Maydonlar aniqlanmoqda: {name}", "en": "Finding the fields of {name}"},
    "gui.filetools.merge.progress.open": {"uz": "Ochilmoqda: {name}", "en": "Opening {name}"},
    "gui.filetools.merge.progress.index": {"uz": "Indekslanmoqda: {name}", "en": "Indexing {name}"},
    "gui.filetools.merge.overwrite": {
        "uz": "Fayl allaqachon bor:\n{path}\n\nUning o'rniga yozilsinmi?",
        "en": "The file already exists:\n{path}\n\nReplace it?",
    },
    "gui.filetools.status.running": {"uz": "Bajarilmoqda…", "en": "Running…"},
    "gui.filetools.status.queued": {
        "uz": "Navbatda: boshqa og'ir vazifa tugashini kutmoqda…",
        "en": "Queued: waiting for another heavy task to finish…",
    },
    "gui.filetools.status.split_done": {"uz": "Tayyor: {parts} ta qism, {size}.", "en": "Done: {parts} parts, {size}."},
    "gui.filetools.status.merge_done": {
        "uz": "Tayyor: {records} ta yozuv, {size}.",
        "en": "Done: {records} records, {size}.",
    },
    "gui.filetools.status.failed": {"uz": "Xato: {error}", "en": "Failed: {error}"},
    "gui.filetools.status.refused": {"uz": "Bajarilmadi: {error}", "en": "Not done: {error}"},
    "gui.filetools.status.cancelled": {
        "uz": "To'xtatildi; hech narsa yozilmadi.",
        "en": "Stopped; nothing was written.",
    },
    "gui.filetools.status.split_done_path": {
        "uz": "Bo'lish tugadi: {parts} ta qism, {size} → {path}",
        "en": "Split finished: {parts} parts, {size} → {path}",
    },
    "gui.filetools.status.merge_done_path": {
        "uz": "Birlashtirish tugadi: {records} ta yozuv, {size} → {path}",
        "en": "Merge finished: {records} records, {size} → {path}",
    },
    "gui.filetools.status.cancelled_path": {"uz": "To'xtatildi: {path}", "en": "Stopped: {path}"},
    "gui.filetools.report.split_title": {"uz": "Bo'lish natijasi", "en": "Split result"},
    "gui.filetools.report.merge_title": {"uz": "Birlashtirish natijasi", "en": "Merge result"},
    "gui.filetools.report.split": {
        "uz": "{parts} ta qism yozildi: {records} ta yozuv, {size}, {seconds} ({rate} MB/s).",
        "en": "{parts} parts written: {records} records, {size}, {seconds} ({rate} MB/s).",
    },
    "gui.filetools.report.source": {"uz": "Manba: {source}", "en": "Source: {source}"},
    "gui.filetools.report.engine.same": {
        "uz": "Asl formatda, baytma-bayt nusxa.",
        "en": "In the original format, byte for byte.",
    },
    "gui.filetools.report.engine.export": {"uz": "Format: {fmt}.", "en": "Format: {fmt}."},
    "gui.filetools.report.edited": {
        "uz": "Saqlanmagan o'zgarishlar ham yozildi.",
        "en": "The unsaved changes were written too.",
    },
    "gui.filetools.report.edited_tabs": {
        "uz": "Ochiq tablarning saqlanmagan o'zgarishlari ham kiritildi: {names}",
        "en": "The unsaved changes of open tabs were included: {names}",
    },
    "gui.filetools.report.merge": {
        "uz": "{records} ta yozuv yozildi: {size}, {seconds} ({rate} MB/s).",
        "en": "{records} records written: {size}, {seconds} ({rate} MB/s).",
    },
    "gui.filetools.report.dupes": {
        "uz": "{duplicates} ta takroriy yozuv olib tashlandi ({groups} ta guruh).",
        "en": "{duplicates} duplicate records were removed ({groups} groups).",
    },
    "gui.filetools.report.inputs": {"uz": "Kiruvchi fayllar ({count}):", "en": "Input files ({count}):"},
    "gui.filetools.report.input_line": {
        "uz": "{name} — {kind}: {records} ta yozuvdan {written} tasi yozildi",
        "en": "{name} — {kind}: {written} of {records} records written",
    },
    "gui.filetools.report.files": {"uz": "Fayllar ({count}):", "en": "Files ({count}):"},
    "gui.filetools.report.removed": {"uz": "O'chirilgan eski qismlar ({count}):", "en": "Old parts removed ({count}):"},
    "gui.filetools.report.more_files": {"uz": "… yana {count} ta fayl", "en": "… {count} more files"},
    "gui.filetools.report.warnings": {"uz": "Ogohlantirishlar ({count}):", "en": "Warnings ({count}):"},
    "gui.filetools.report.open_folder": {"uz": "Papkani ochish", "en": "Open folder"},
    "gui.filetools.report.open_first": {"uz": "Birinchi qismni ochish", "en": "Open the first part"},
    "gui.filetools.report.open_result": {"uz": "Natijani ochish", "en": "Open the result"},
    # ---- gui.diff ----
    # side-by-side structural diff (gui/difftree.py, gui/diffview.py)
    "gui.diff.gap": {"uz": "… {count} ta o'zgarishsiz", "en": "… {count} unchanged"},
    "gui.diff.unchanged.object": {
        "uz": "{{…}} {count} ta maydon, o'zgarishsiz",
        "en": "{{…}} {count} fields, unchanged",
    },
    "gui.diff.unchanged.object.one": {
        "uz": "{{…}} {count} ta maydon, o'zgarishsiz",
        "en": "{{…}} {count} field, unchanged",
    },
    "gui.diff.unchanged.array": {
        "uz": "[…] {count} ta element, o'zgarishsiz",
        "en": "[…] {count} elements, unchanged",
    },
    "gui.diff.unchanged.array.one": {
        "uz": "[…] {count} ta element, o'zgarishsiz",
        "en": "[…] {count} element, unchanged",
    },
    "gui.diff.summary.equal": {"uz": "Farq yo'q", "en": "No differences"},
    "gui.diff.summary.counts": {
        "uz": "{changed} ta o'zgargan, {added} ta qo'shilgan, {removed} ta o'chirilgan, {moved} ta ko'chirilgan",
        "en": "{changed} changed, {added} added, {removed} removed, {moved} moved",
    },
    "gui.diff.col.key": {"uz": "Kalit", "en": "Key"},
    "gui.diff.col.a": {"uz": "A: {name}", "en": "A: {name}"},
    "gui.diff.col.b": {"uz": "B: {name}", "en": "B: {name}"},
    "gui.diff.empty": {"uz": "Taqqoslash uchun ikki qiymat tanlang.", "en": "Choose two values to compare."},
    "gui.diff.loading": {"uz": "Taqqoslanmoqda…", "en": "Comparing…"},
    "gui.diff.cancelled": {"uz": "Taqqoslash bekor qilindi.", "en": "The comparison was cancelled."},
    "gui.diff.failed": {"uz": "Taqqoslab bo'lmadi: {error}", "en": "Could not compare: {error}"},
    "gui.diff.unexpected": {
        "uz": "kutilmagan xato (tafsilotlar log faylida)",
        "en": "an unexpected error (details are in the log file)",
    },
    "gui.diff.task": {"uz": "Taqqoslash: {a} ↔ {b}", "en": "Compare {a} ↔ {b}"},
    "gui.diff.task.layout": {"uz": "Massiv elementlari joylanmoqda", "en": "Laying out array elements"},
    "gui.diff.task.text": {"uz": "Matnlar solishtirilmoqda", "en": "Comparing the texts"},
    "gui.diff.task.copy": {"uz": "Qiymat nusxalanmoqda", "en": "Copying the value"},
    "gui.diff.match.label": {"uz": "Massivlar:", "en": "Arrays:"},
    "gui.diff.match.auto": {"uz": "Avto", "en": "Auto"},
    "gui.diff.match.auto.tip": {
        "uz": "Elementlar mazmuni bo'yicha tekislanadi: boshiga qo'shilgan element qolganlarini siljitmaydi.",
        "en": "Elements are aligned by content: an element inserted at the front does not shift the others.",
    },
    "gui.diff.match.index": {"uz": "Indeks", "en": "Index"},
    "gui.diff.match.index.tip": {
        "uz": "i-element i-element bilan solishtiriladi.",
        "en": "Element i is compared with element i.",
    },
    "gui.diff.match.unordered": {"uz": "Tartibsiz", "en": "Unordered"},
    "gui.diff.match.unordered.tip": {
        "uz": "Elementlar tartibi hisobga olinmaydi: bir xil elementlar juftlanadi.",
        "en": "The order of the elements does not matter: equal elements are paired.",
    },
    "gui.diff.match.key": {"uz": "Kalit bo'yicha", "en": "By key"},
    "gui.diff.match.key.tip": {
        "uz": "Elementlar shu maydon qiymati bo'yicha juftlanadi; joyi o'zgarganlari «ko'chirilgan» deb ko'rsatiladi "
              "(YO'L=MAYDON — faqat shu yo'ldagi massivlar uchun).",
        "en": "Elements are paired by the value of this field; the ones at another place are shown as moved "
              "(PATH=FIELD: only the arrays at that path).",
    },
    "gui.diff.match.key.placeholder": {"uz": "maydon, masalan id", "en": "field, e.g. id"},
    "gui.diff.match.key_needed": {
        "uz": "Elementlarni juftlash uchun maydon nomini kiriting (masalan id).",
        "en": "Enter the field to pair the elements by (for example id).",
    },
    "gui.diff.numbers_text": {"uz": "Sonlar yozilishi bo'yicha", "en": "Numbers as written"},
    "gui.diff.numbers_text.tip": {
        "uz": "1.10 va 1.1 farqli deb ko'rsatiladi. Odatda sonlar qiymati bo'yicha solishtiriladi: 1 = 1.0, -0 = 0.",
        "en": "1.10 and 1.1 are shown as different. Numbers are normally compared by value: 1 = 1.0, -0 = 0.",
    },
    "gui.diff.only_changes": {"uz": "Faqat farqlar", "en": "Only differences"},
    "gui.diff.only_changes.tip": {
        "uz": "O'zgarmagan qismlarni yashirish (har bir guruhi bitta qator bo'lib qoladi).",
        "en": "Hide the unchanged parts (each run of them becomes one row).",
    },
    "gui.diff.only_changes.forced": {
        "uz": "Qiymatlar katta yoki farqlar juda ko'p: faqat farqlar ko'rsatiladi.",
        "en": "The values are large or the differences too many: only the differences are shown.",
    },
    "gui.diff.prev.tip": {"uz": "Oldingi farq ({key})", "en": "Previous difference ({key})"},
    "gui.diff.next.tip": {"uz": "Keyingi farq ({key})", "en": "Next difference ({key})"},
    "gui.diff.expand_all.tip": {
        "uz": "Farqli qismlarning hammasini ochish (*)",
        "en": "Expand every part with differences (*)",
    },
    "gui.diff.collapse_all.tip": {"uz": "Hammasini yig'ish", "en": "Collapse everything"},
    "gui.diff.swap.tip": {"uz": "A va B ning o'rnini almashtirish", "en": "Swap A and B"},
    "gui.diff.text_mode": {"uz": "Matn", "en": "Text"},
    "gui.diff.text_mode.tip": {
        "uz": "Ikki qiymat matnini qatorma-qator solishtirish",
        "en": "Compare the texts of the two values line by line",
    },
    "gui.diff.truncated": {
        "uz": "Faqat birinchi {shown} ta farq ko'rsatildi (jami {total}).",
        "en": "Only the first {shown} differences are shown ({total} in total).",
    },
    "gui.diff.changes_only": {
        "uz": "Qiymatlar katta: faqat farqli yo'llar ko'rsatildi.",
        "en": "The values are large: only the paths with differences are shown.",
    },
    "gui.diff.capped": {
        "uz": "Qismlar juda ko'p: hammasi ochilmadi.",
        "en": "Too many parts: not everything was expanded.",
    },
    "gui.diff.no_more": {"uz": "Boshqa farq yo'q", "en": "No more differences"},
    "gui.diff.status.equal": {"uz": "O'zgarishsiz", "en": "Unchanged"},
    "gui.diff.status.changed": {"uz": "O'zgargan", "en": "Changed"},
    "gui.diff.status.added": {"uz": "Qo'shilgan (faqat B tomonda)", "en": "Added (only in B)"},
    "gui.diff.status.removed": {"uz": "O'chirilgan (faqat A tomonda)", "en": "Removed (only in A)"},
    "gui.diff.status.moved": {
        "uz": "Ko'chirilgan (massivda boshqa joyda)",
        "en": "Moved (at another place in the array)",
    },
    "gui.diff.status.contains": {"uz": "Ichida farqlar bor", "en": "Differences inside"},
    "gui.diff.status.gap": {"uz": "O'zgarmagan qismlar", "en": "Unchanged parts"},
    "gui.diff.tip.paths": {"uz": "A: {a}\nB: {b}", "en": "A: {a}\nB: {b}"},
    "gui.diff.tip.changes": {"uz": "{count} ta farq", "en": "{count} differences"},
    "gui.diff.tip.changes.one": {"uz": "{count} ta farq", "en": "{count} difference"},
    "gui.diff.tip.kinds": {"uz": "Turi o'zgargan: {a} → {b}", "en": "Type changed: {a} → {b}"},
    "gui.diff.menu.copy_a": {"uz": "A qiymatini nusxalash", "en": "Copy the A value"},
    "gui.diff.menu.copy_b": {"uz": "B qiymatini nusxalash", "en": "Copy the B value"},
    "gui.diff.menu.copy_path": {"uz": "Yo'lni nusxalash", "en": "Copy the path"},
    "gui.diff.menu.reveal_a": {"uz": "Daraxtda ko'rsatish (A)", "en": "Show in the tree (A)"},
    "gui.diff.menu.reveal_b": {"uz": "Daraxtda ko'rsatish (B)", "en": "Show in the tree (B)"},
    "gui.diff.text.lines": {"uz": "{count} ta qator farq qiladi", "en": "{count} lines differ"},
    "gui.diff.text.lines.one": {"uz": "{count} ta qator farq qiladi", "en": "{count} line differs"},
    "gui.diff.text.same": {"uz": "Matnlar bir xil", "en": "The texts are the same"},
    "gui.diff.text.truncated": {
        "uz": "{side}: matnning faqat boshi ({size} / {total}) ko'rsatildi",
        "en": "{side}: only the beginning of the text ({size} of {total}) is shown",
    },
    "gui.diff.text.uncolored": {
        "uz": "faqat birinchi {count} ta farqli qator rangda",
        "en": "only the first {count} differing lines are coloured",
    },
    # ---- gui.compare ----
    # compare windows, the files dialog and the compare mark (gui/comparewindow.py)
    "gui.compare.menu.files": {"uz": "Ikki faylni taqqoslash…", "en": "Compare two files…"},
    "gui.compare.menu.files.tip": {
        "uz": "Ikki faylni (yoki ochiq tablarni) yozuvma-yozuv taqqoslash",
        "en": "Compare two files (or open tabs) record by record",
    },
    "gui.compare.menu.mark": {"uz": "Taqqoslash uchun belgilash", "en": "Mark for comparison"},
    "gui.compare.menu.with_marked": {"uz": "Belgilangan bilan taqqoslash", "en": "Compare with the marked node"},
    "gui.compare.menu.with_marked.tip": {"uz": "Belgilangan: {label}", "en": "Marked: {label}"},
    "gui.compare.menu.with_marked.none": {
        "uz": "Avval boshqa tugunni «Taqqoslash uchun belgilash» bilan belgilang",
        "en": "First mark another node with “Mark for comparison”",
    },
    "gui.compare.menu.with_marked.same": {
        "uz": "Shu tugun belgilangan: boshqa tugunni tanlang",
        "en": "This node is the marked one: choose another node",
    },
    "gui.compare.menu.selected": {"uz": "Tanlangan ikkitasini taqqoslash", "en": "Compare the two selected"},
    "gui.compare.menu.selected.need_two": {
        "uz": "Taqqoslash uchun aynan ikkita tugunni tanlang",
        "en": "Select exactly two nodes to compare",
    },
    "gui.compare.marked": {"uz": "Taqqoslash uchun belgilandi: {label}", "en": "Marked for comparison: {label}"},
    "gui.compare.no_mark": {
        "uz": "Taqqoslash uchun belgilangan tugun yo'q (yoki uning tabi yopilgan)",
        "en": "No node is marked for comparison (or its tab was closed)",
    },
    "gui.compare.title": {"uz": "Taqqoslash: {a} ↔ {b}", "en": "Compare: {a} ↔ {b}"},
    "gui.compare.header": {"uz": "A: {a}    B: {b}", "en": "A: {a}    B: {b}"},
    "gui.compare.files.title": {"uz": "Ikki faylni taqqoslash", "en": "Compare two files"},
    "gui.compare.side.a": {"uz": "A:", "en": "A:"},
    "gui.compare.side.b": {"uz": "B:", "en": "B:"},
    "gui.compare.source.tab": {"uz": "Ochiq tab: {name}", "en": "Open tab: {name}"},
    "gui.compare.source.current": {"uz": "{name} (joriy tab)", "en": "{name} (current tab)"},
    "gui.compare.source.file": {"uz": "Fayl…", "en": "File…"},
    "gui.compare.path.placeholder": {"uz": "fayl yo'li", "en": "file path"},
    "gui.compare.browse": {"uz": "Tanlash…", "en": "Browse…"},
    "gui.compare.browse.title": {"uz": "Taqqoslanadigan fayl", "en": "File to compare"},
    "gui.compare.browse.filter": {
        "uz": "JSON fayllar (*.json *.jsonl *.ndjson *.json.gz *.geojson);;Barcha fayllar (*)",
        "en": "JSON files (*.json *.jsonl *.ndjson *.json.gz *.geojson);;All files (*)",
    },
    "gui.compare.mode": {"uz": "Usul:", "en": "Method:"},
    "gui.compare.mode.auto": {"uz": "Avto", "en": "Automatic"},
    "gui.compare.mode.auto.tip": {
        "uz": "Kalit berilsa — kalit bo'yicha; kalitli hujjatlar — $key bo'yicha; ikki yaxlit hujjat — butunligicha; "
              "aks holda yozuvlar mazmuni bo'yicha tekislanadi (fayllar chegaradan katta bo'lmasa).",
        "en": "With a key: by key; keyed documents: by $key; two single documents: as a whole; otherwise the "
              "records are aligned by content (when the files are within the limit).",
    },
    "gui.compare.mode.key": {"uz": "Kalit bo'yicha", "en": "By key"},
    "gui.compare.mode.key.tip": {
        "uz": "Yozuvlar kalit maydoni qiymati bo'yicha juftlanadi (har qanday hajmdagi fayllar uchun).",
        "en": "Records are paired by the value of the key field (files of any size).",
    },
    "gui.compare.mode.align": {"uz": "Mazmun bo'yicha tekislash", "en": "Align by content"},
    "gui.compare.mode.align.tip": {
        "uz": "Yozuvlar mazmuni bo'yicha tekislanadi: qo'shilgan va o'chirilgan yozuvlar topiladi.",
        "en": "The records are aligned by content: added and removed records are found.",
    },
    "gui.compare.mode.position": {"uz": "Tartib raqami bo'yicha", "en": "By position"},
    "gui.compare.mode.position.tip": {
        "uz": "i-yozuv i-yozuv bilan solishtiriladi (har qanday hajm, eng tez).",
        "en": "Record i is compared with record i (any size, the fastest).",
    },
    "gui.compare.mode.document": {"uz": "Butun hujjat", "en": "Whole document"},
    "gui.compare.mode.document.tip": {
        "uz": "Ikki hujjat bitta qiymat sifatida taqqoslanadi.",
        "en": "The two documents are compared as one value each.",
    },
    "gui.compare.key": {"uz": "Yozuv kaliti:", "en": "Record key:"},
    "gui.compare.key.placeholder": {
        "uz": "masalan id ($key — kalitli hujjatlarda yozuv kaliti)",
        "en": "e.g. id ($key: the record key of keyed documents)",
    },
    "gui.compare.key.tip": {
        "uz": "Yozuvlar shu maydon qiymati bo'yicha juftlanadi. Bo'sh qolsa — usulga qarab.",
        "en": "Records are paired by the value of this field. Empty: as the method says.",
    },
    "gui.compare.array_match": {"uz": "Massiv elementlari:", "en": "Array elements:"},
    "gui.compare.array_key.placeholder": {"uz": "maydon, masalan id yoki orders=oid",
                                          "en": "field, e.g. id or orders=oid"},
    "gui.compare.run": {"uz": "Taqqoslash", "en": "Compare"},
    "gui.compare.error.no_source": {"uz": "{side} uchun fayl yoki tab tanlang.", "en": "Choose a file or a tab for {side}."},
    "gui.compare.error.no_file": {"uz": "Fayl topilmadi: {path}", "en": "File not found: {path}"},
    "gui.compare.error.not_ready": {
        "uz": "{name} hali ochilmoqda yoki indekslanmoqda — tayyor bo'lgach qayta urinib ko'ring.",
        "en": "{name} is still opening or being indexed — try again when it is ready.",
    },
    "gui.compare.error.key_needed": {
        "uz": "Kalit bo'yicha taqqoslash uchun yozuv kalitini kiriting (masalan id).",
        "en": "Enter the record key to compare by (for example id).",
    },
    "gui.compare.error.closed": {
        "uz": "{name} yopilgan yoki qayta ochilgan: qayta taqqoslang.",
        "en": "{name} was closed or reopened: compare again.",
    },
    "gui.compare.error.results_gone": {
        "uz": "Taqqoslash natijalari endi mavjud emas: qayta taqqoslang.",
        "en": "The comparison results are gone: compare again.",
    },
    "gui.compare.task": {"uz": "Taqqoslash: {a} ↔ {b}", "en": "Comparing {a} ↔ {b}"},
    "gui.compare.task.filter": {"uz": "Natijalar saralanmoqda", "en": "Filtering the results"},
    "gui.compare.task.records": {"uz": "Yozuvlar to'plami tayyorlanmoqda", "en": "Preparing the record set"},
    "gui.compare.task.export": {"uz": "Farqlar hisoboti: {name}", "en": "Differences report: {name}"},
    "gui.compare.running": {"uz": "Taqqoslanmoqda…", "en": "Comparing…"},
    "gui.compare.cancelled": {"uz": "Taqqoslash bekor qilindi.", "en": "The comparison was cancelled."},
    "gui.compare.failed": {"uz": "Taqqoslab bo'lmadi: {error}", "en": "Could not compare: {error}"},
    "gui.compare.summary": {
        "uz": "{changed} ta o'zgargan, {added} ta qo'shilgan, {removed} ta o'chirilgan, {equal} ta bir xil yozuv "
              "(A: {a}, B: {b}; {seconds} s)",
        "en": "{changed} changed, {added} added, {removed} removed, {equal} equal records (A: {a}, B: {b}; "
              "{seconds} s)",
    },
    "gui.compare.summary.identical": {
        "uz": "Farq yo'q: {equal} ta yozuv bir xil ({seconds} s)",
        "en": "No differences: {equal} records are equal ({seconds} s)",
    },
    "gui.compare.method.key": {"uz": "kalit bo'yicha: {key}", "en": "by key: {key}"},
    "gui.compare.method.align": {"uz": "mazmun bo'yicha tekislandi", "en": "aligned by content"},
    "gui.compare.method.position": {"uz": "tartib raqami bo'yicha", "en": "by position"},
    "gui.compare.method.document": {"uz": "butun hujjat", "en": "whole document"},
    "gui.compare.filter.changed": {"uz": "O'zgargan ({count})", "en": "Changed ({count})"},
    "gui.compare.filter.added": {"uz": "Qo'shilgan ({count})", "en": "Added ({count})"},
    "gui.compare.filter.removed": {"uz": "O'chirilgan ({count})", "en": "Removed ({count})"},
    "gui.compare.col.status": {"uz": "Holat", "en": "Status"},
    "gui.compare.col.key": {"uz": "Kalit", "en": "Key"},
    "gui.compare.col.a": {"uz": "A #", "en": "A #"},
    "gui.compare.col.b": {"uz": "B #", "en": "B #"},
    "gui.compare.col.changes": {"uz": "Farqlar", "en": "Differences"},
    "gui.compare.col.path": {"uz": "Birinchi farq", "en": "First difference"},
    "gui.compare.status.changed": {"uz": "o'zgargan", "en": "changed"},
    "gui.compare.status.added": {"uz": "qo'shilgan", "en": "added"},
    "gui.compare.status.removed": {"uz": "o'chirilgan", "en": "removed"},
    "gui.compare.status.wrapper": {"uz": "o'rovchi", "en": "wrapper"},
    "gui.compare.wrapper": {"uz": "(o'rovchi)", "en": "(wrapper)"},
    "gui.compare.wrapper_of": {"uz": "{name} (o'rovchi)", "en": "{name} (wrapper)"},
    "gui.compare.no_results": {"uz": "Natija yo'q", "en": "No results"},
    "gui.compare.row_failed": {"uz": "o'qib bo'lmadi", "en": "could not be read"},
    "gui.compare.open_records": {"uz": "Yozuvlar to'plami sifatida ochish", "en": "Open as a record set"},
    "gui.compare.open_records.tip": {
        "uz": "Farqli yozuvlarni A yoki B tabida yozuvlar to'plami sifatida ko'rsatish",
        "en": "Show the differing records in the tab of A or B as a record set",
    },
    "gui.compare.open_records.a": {
        "uz": "A ({name}): o'zgargan va o'chirilgan yozuvlar",
        "en": "A ({name}): changed and removed records",
    },
    "gui.compare.open_records.b": {
        "uz": "B ({name}): o'zgargan va qo'shilgan yozuvlar",
        "en": "B ({name}): changed and added records",
    },
    "gui.compare.open_records.no_tab": {"uz": "{name} tabda ochiq emas", "en": "{name} is not open in a tab"},
    "gui.compare.records_label": {
        "uz": "Taqqoslash: {count} ta farqli yozuv ({other} bilan)",
        "en": "Comparison: {count} differing records (with {other})",
    },
    "gui.compare.records_label.one": {
        "uz": "Taqqoslash: {count} ta farqli yozuv ({other} bilan)",
        "en": "Comparison: {count} differing record (with {other})",
    },
    "gui.compare.export": {"uz": "Hisobotni eksport qilish…", "en": "Export the report…"},
    "gui.compare.export.title": {"uz": "Farqlar hisobotini saqlash", "en": "Save the differences report"},
    "gui.compare.export.filter": {
        "uz": "JSON (*.json);;JSON Lines (*.jsonl);;CSV (*.csv)",
        "en": "JSON (*.json);;JSON Lines (*.jsonl);;CSV (*.csv)",
    },
    "gui.compare.export.done": {"uz": "Hisobot saqlandi: {count} ta yozuv → {path}",
                                "en": "Report saved: {count} records → {path}"},
    "gui.compare.reveal.a": {"uz": "Daraxtda ko'rsatish (A)", "en": "Show in the tree (A)"},
    "gui.compare.reveal.b": {"uz": "Daraxtda ko'rsatish (B)", "en": "Show in the tree (B)"},
    "gui.compare.options": {"uz": "Sozlamalar…", "en": "Options…"},
    "gui.compare.refresh": {"uz": "Yangilash", "en": "Refresh"},
    "gui.compare.swap.tip": {"uz": "A va B ning o'rnini almashtirib qayta taqqoslash",
                             "en": "Swap A and B and compare again"},
    "gui.compare.match_locked": {
        "uz": "Fayllar taqqoslashining sozlamasi — «Sozlamalar…» orqali o'zgartiring",
        "en": "Set by the file comparison: change it with “Options…”",
    },
    "gui.compare.stale.changed": {"uz": "{name} o'zgardi.", "en": "{name} changed."},
    "gui.compare.stale.closed": {"uz": "{name} yopildi.", "en": "{name} was closed."},
    "gui.compare.stale.hint": {
        "uz": "Natijalar eskirgan — yangilash uchun qayta taqqoslang.",
        "en": "The results are out of date — compare again to refresh them.",
    },
    # ---- gui.palette ----
    "gui.palette.action": {"uz": "Buyruqlar palitrasi…", "en": "Command palette…"},
    "gui.palette.action.tip": {
        "uz": "Istalgan buyruqni nomi bo'yicha topib ishga tushirish",
        "en": "Find any command by its name and run it",
    },
    "gui.palette.placeholder": {
        "uz": "Buyruq nomini yozing: saqlash, eksport, filtr…",
        "en": "Type a command name: save, export, filter…",
    },
    "gui.palette.node": {"uz": "Tugun", "en": "Node"},
    "gui.palette.tab": {"uz": "Tab: {name}", "en": "Tab: {name}"},
    "gui.palette.open": {"uz": "Ochish: {name}", "en": "Open: {name}"},
    "gui.palette.reason.unavailable": {
        "uz": "Joriy ko'rinish yoki faylning holati bunga mos emas.",
        "en": "The current view or the file's state does not allow it.",
    },
    "gui.palette.reason.no_file": {"uz": "Ochiq fayl yo'q.", "en": "No file is open."},
    "gui.palette.disabled_tip": {"uz": "Hozir ishlamaydi — {reason}", "en": "Not available now — {reason}"},
    "gui.palette.cannot_run": {
        "uz": "Bu buyruqni hozir ishga tushirib bo'lmaydi — {reason}",
        "en": "This command cannot run now — {reason}",
    },
    "gui.palette.recent_tag": {"uz": "yaqinda", "en": "recent"},
    "gui.palette.empty": {"uz": "Hech narsa topilmadi", "en": "Nothing found"},
    "gui.palette.status": {
        "uz": "{shown} / {total} ta buyruq · Enter — ishga tushirish, Esc — yopish",
        "en": "{shown} of {total} commands · Enter runs, Esc closes",
    },
    # ---- gui.keymap ----
    "gui.keymap.col.action": {"uz": "Buyruq", "en": "Command"},
    "gui.keymap.col.path": {"uz": "Menyu", "en": "Menu"},
    "gui.keymap.col.default": {"uz": "Asl tugma", "en": "Default key"},
    "gui.keymap.col.current": {"uz": "Joriy tugma", "en": "Current key"},
    "gui.keymap.filter.placeholder": {
        "uz": "Buyruq, menyu yoki tugma bo'yicha qidirish…",
        "en": "Filter by command, menu or key…",
    },
    "gui.keymap.record.label": {"uz": "Yangi tugma:", "en": "New key:"},
    "gui.keymap.record.tip": {
        "uz": "Jadvalda buyruqni tanlang, so'ng shu yerni bosib, yangi tugmalar birikmasini bosing "
              "(masalan, Ctrl+Alt+E).",
        "en": "Select a command in the table, click here and press the new key combination (e.g. Ctrl+Alt+E).",
    },
    "gui.keymap.none": {"uz": "Tugmasiz", "en": "No key"},
    "gui.keymap.none.tip": {
        "uz": "Tanlangan buyruqdan barcha tugmalarni olib tashlash",
        "en": "Remove every key of the selected command",
    },
    "gui.keymap.reset": {"uz": "Asl holiga", "en": "Reset"},
    "gui.keymap.reset.tip": {"uz": "Bu buyruqning asl tugmasini qaytarish", "en": "Give this command its default key back"},
    "gui.keymap.reset_all": {"uz": "Hammasini asl holiga", "en": "Reset all"},
    "gui.keymap.reset_all.tip": {
        "uz": "Barcha buyruqlarning asl tugmalarini qaytarish (\"Saqlash\" bosilganda qo'llanadi)",
        "en": "Give every command its default keys back (applied when you press \"Save\")",
    },
    "gui.keymap.no_keys": {"uz": "(yo'q)", "en": "(none)"},
    "gui.keymap.changed.tip": {"uz": "O'zgartirilgan; asl tugma: {keys}", "en": "Changed; the default is {keys}"},
    "gui.keymap.hint": {
        "uz": "Buyruqni tanlang va yangi tugmani bosing. O'zgarishlar \"Saqlash\" bosilganda darhol qo'llanadi.",
        "en": "Select a command and press its new key. The changes apply as soon as you press \"Save\".",
    },
    "gui.keymap.unavailable": {
        "uz": "Tugmalarni o'zgartirish uchun Sozlamalarni asosiy oynaning menyusidan oching "
              "(Asboblar ▸ Sozlamalar…).",
        "en": "Open Settings from the main window's menu (Tools ▸ Settings…) to change the keys.",
    },
    "gui.keymap.assigned": {
        "uz": "«{action}» buyrug'ining tugmasi endi: {keys}",
        "en": "«{action}» now has the key {keys}",
    },
    "gui.keymap.cleared": {"uz": "«{action}» endi tugmasiz.", "en": "«{action}» has no key now."},
    "gui.keymap.reset_done": {
        "uz": "«{action}» asl tugmasiga qaytdi: {keys}",
        "en": "«{action}» has its default key again: {keys}",
    },
    "gui.keymap.all_reset": {
        "uz": "Barcha tugmalar asl holiga qaytarildi (\"Saqlash\" bosilganda qo'llanadi).",
        "en": "Every key is back to its default (applied when you press \"Save\").",
    },
    "gui.keymap.conflict": {
        "uz": "«{keys}» band: u «{action}» ({path}) buyrug'iga berilgan. Avval o'sha buyruqning tugmasini "
              "o'zgartiring.",
        "en": "«{keys}» is taken by «{action}» ({path}). Change that command's key first.",
    },
    "gui.keymap.fixed_conflict": {
        "uz": "«{keys}» daraxt va jadvalning o'z tugmasi ({what}); uni boshqa buyruqqa berib bo'lmaydi.",
        "en": "«{keys}» is a key of the tree and the table themselves ({what}); it cannot be given to another "
              "command.",
    },
    "gui.keymap.menu_conflict": {
        "uz": "«{keys}» «{menu}» menyusini ochadi; boshqa tugmani tanlang.",
        "en": "«{keys}» opens the «{menu}» menu; choose another key.",
    },
    "gui.keymap.reset_conflict": {
        "uz": "Asl tugma «{keys}» hozir «{action}» buyrug'ida; avval o'sha buyruqning tugmasini o'zgartiring.",
        "en": "The default key «{keys}» is used by «{action}» now; change that command's key first.",
    },
    "gui.keymap.multi": {
        "uz": "Faqat bitta tugmalar birikmasi ishlatiladi (masalan, Ctrl+Alt+E), ketma-ket bir nechtasi emas: {keys}",
        "en": "Only one key combination can be used (e.g. Ctrl+Alt+E), not several in a row: {keys}",
    },
    "gui.keymap.invalid": {
        "uz": "Bu tugmalar birikmasini ishlatib bo'lmaydi: {keys}",
        "en": "This key combination cannot be used: {keys}",
    },
    "gui.keymap.twice": {"uz": "«{keys}» ikki marta berilgan.", "en": "«{keys}» is given twice."},
    "gui.keymap.unknown": {"uz": "Bunday buyruq yo'q: {action}", "en": "There is no such command: {action}"},
    "gui.keymap.fixed.title": {"uz": "O'zgarmas tugmalar (daraxt va jadvalda)", "en": "Fixed keys (tree and table)"},
    "gui.keymap.fixed.note": {
        "uz": "Bu tugmalar ko'rinishlarning o'zida ishlaydi; ularni o'zgartirib ham, boshqa buyruqqa berib ham "
              "bo'lmaydi.",
        "en": "These keys work inside the views themselves; they cannot be changed or given to another command.",
    },
    "gui.keymap.fixed.col.keys": {"uz": "Tugma", "en": "Key"},
    "gui.keymap.fixed.col.what": {"uz": "Vazifasi", "en": "What it does"},
    "gui.keymap.fixed.move": {
        "uz": "Yurish; daraxtda → ochadi, ← yopadi",
        "en": "Move; in the tree → expands, ← collapses",
    },
    "gui.keymap.fixed.page": {
        "uz": "Sahifalab yurish, boshiga / oxiriga o'tish",
        "en": "Move by pages, to the first / last row",
    },
    "gui.keymap.fixed.extend": {"uz": "Tanlovni kengaytirish", "en": "Extend the selection"},
    "gui.keymap.fixed.select_all": {"uz": "Hammasini tanlash", "en": "Select everything"},
    "gui.keymap.fixed.toggle_select": {
        "uz": "Qatorni tanlovga qo'shish / tanlovdan olib tashlash",
        "en": "Add the row to the selection / remove it",
    },
    "gui.keymap.fixed.expand_all": {"uz": "Hammasini ochish (daraxt)", "en": "Expand everything (tree)"},
    "gui.keymap.fixed.expand_collapse": {
        "uz": "Tugunni ochish / yopish (daraxt)",
        "en": "Expand / collapse the node (tree)",
    },
    "gui.keymap.fixed.full_value": {"uz": "To'liq qiymatni ko'rish (daraxt)", "en": "Show the full value (tree)"},
    "gui.keymap.fixed.edit": {
        "uz": "Qiymatni tahrirlash (jadvalda konteyner daraxtda ko'rsatiladi)",
        "en": "Edit the value (in the table a container is shown in the tree)",
    },
    "gui.keymap.fixed.edit_json": {"uz": "JSON sifatida tahrirlash", "en": "Edit as JSON"},
    "gui.keymap.fixed.rename": {"uz": "Kalit nomini o'zgartirish", "en": "Rename the key"},
    "gui.keymap.fixed.insert": {"uz": "Qo'shish", "en": "Insert"},
    "gui.keymap.fixed.delete": {"uz": "O'chirish", "en": "Delete"},
    "gui.keymap.fixed.duplicate": {"uz": "Nusxasini qo'shish (dublikat)", "en": "Duplicate"},
    "gui.keymap.fixed.move_node": {
        "uz": "Tugunni yuqoriga / pastga surish (daraxt)",
        "en": "Move the node up / down (tree)",
    },
    "gui.keymap.fixed.quick_filter": {"uz": "Ustunning tezkor filtri (jadval)", "en": "The column's quick filter (table)"},
    "gui.keymap.fixed.paste": {"uz": "JSON'ni joylash", "en": "Paste JSON"},
    "gui.keymap.fixed.copy_value": {"uz": "Qiymatni nusxalash", "en": "Copy the value"},
    "gui.keymap.fixed.copy_path": {"uz": "Yo'lni nusxalash", "en": "Copy the path"},
    "gui.keymap.fixed.cancel": {
        "uz": "Bekor qilish, qidiruv qatorini yopish",
        "en": "Cancel, close the find bar",
    },
    "gui.keymap.fixed.focus": {
        "uz": "Keyingi / oldingi maydonga o'tish",
        "en": "Move to the next / previous field",
    },
    # ---- gui.settings ----
    "gui.settings.title": {"uz": "Sozlamalar", "en": "Settings"},
    "gui.settings.tab.general": {"uz": "Umumiy", "en": "General"},
    "gui.settings.tab.export": {"uz": "Eksport", "en": "Export"},
    "gui.settings.tab.view": {"uz": "Ko'rinish", "en": "Appearance"},
    "gui.settings.tab.advanced": {"uz": "Qo'shimcha", "en": "Advanced"},
    "gui.settings.tab.keys": {"uz": "Klaviatura", "en": "Keyboard"},
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
    "gui.settings.diff_align_max_bytes.tip": {
        "uz": "Ikki fayl kalitsiz (yozuvlarni mazmuni bo'yicha moslab) faqat ikkalasi ham shu hajmdan oshmasa "
        "to'liq taqqoslanadi. Kattaroq fayllar uchun kalit maydonini tanlang yoki tartib raqami bo'yicha "
        "taqqoslang. Kattaroq qiymat ko'proq xotira talab qiladi.",
        "en": "Two files are compared in full without a key (records matched by content) only while both are at "
        "most this size. For bigger files choose a key field or compare by position. A bigger value needs more "
        "memory.",
    },
    "gui.settings.editing": {"uz": "Tahrirlash", "en": "Editing"},
    "gui.settings.edits_location.appdata": {"uz": "Dastur papkasida ({path})", "en": "App folder ({path})"},
    "gui.settings.edits_location.beside": {
        "uz": "Fayl yonida (<fayl nomi>.jredit)",
        "en": "Next to the file (<name>.jredit)",
    },
    "gui.settings.edits_location.tip": {
        "uz": "Fayl saqlanmaguncha qilingan o'zgarishlar shu yerda doimiy saqlanib boradi (dastur kutilmaganda "
        "yopilsa ham yo'qolmaydi). Fayl yonida: fayl bilan birga ko'chadi, lekin u papkaga yozish mumkin "
        "bo'lishi kerak, aks holda dastur papkasi ishlatiladi. Tanlov yangi tahrirlarga ta'sir qiladi; ikkala "
        "joydagi saqlanmagan o'zgarishlar ham topiladi.",
        "en": "Changes made before the file is saved are kept here all the time (they survive a crash). Next to "
        "the file: they move with the file, but that folder must be writable, otherwise the app folder is used. "
        "The choice applies to new edits; unsaved changes are found in both places.",
    },
    "gui.settings.backup_on_save.tip": {
        "uz": "Saqlash faylni yangisi bilan almashtirganda, avvalgi holati yonida .bak fayl bo'lib qoladi "
        "(masalan, data.json.bak). U fayl hajmicha disk joyini egallaydi.",
        "en": "When Save replaces the file, its previous version stays next to it as a .bak file "
        "(for example data.json.bak). It takes as much disk space as the file.",
    },
    "gui.settings.undo_limit.tip": {
        "uz": "Ortga qaytarish (Ctrl+Z) necha qadam orqaga bora olishi (kamida 200). Eskiroq qadamlar "
        "o'zgarishlar bilan birga saqlanib qoladi, faqat ularni endi ortga qaytarib bo'lmaydi.",
        "en": "How many steps Undo (Ctrl+Z) can go back (at least 200). Older steps stay applied; they just "
        "can no longer be undone.",
    },
    "gui.settings.startup": {"uz": "Ishga tushirish", "en": "Start-up"},
    "gui.settings.restore_session": {"uz": "Oxirgi sessiyani tiklash", "en": "Restore the last session"},
    "gui.settings.restore_session.always": {"uz": "Har doim", "en": "Always"},
    "gui.settings.restore_session.ask": {"uz": "Har safar so'rash", "en": "Ask every time"},
    "gui.settings.restore_session.never": {"uz": "Hech qachon", "en": "Never"},
    "gui.settings.restore_session.tip": {
        "uz": "Dastur faylsiz ishga tushirilganda oxirgi marta ochiq bo'lgan fayllar qayta ochiladi: ko'rinishlari, "
              "joriy tuguni, ochilgan tugunlari, filtri va saralashi bilan. Fayl buyruq satrida (yoki «Open with» "
              "orqali) berilsa, faqat o'sha fayl ochiladi va saqlangan sessiya keyingi safarga qoladi.",
        "en": "When the program starts without a file, the files that were open last time open again: with their "
              "views, current node, expanded nodes, filter and sort. When a file is given on the command line (or "
              "through \"Open with\"), only that file opens and the saved session is kept for the next start.",
    },
    # ---- gui.os ----
    "gui.os.settings.label": {"uz": "Windows «Open with»:", "en": "Windows \"Open with\":"},
    "gui.os.add": {"uz": ".json fayllarni «Open with» ro'yxatiga qo'shish", "en": "Add to \"Open with\" for .json files"},
    "gui.os.add.tip": {
        "uz": "Explorer'da faylni o'ng tugma bilan bosganda «Open with» ro'yxatida JSON Reshape chiqadi ({command}). "
        "Faqat sizning foydalanuvchingiz uchun yoziladi; standart dastur o'zgarmaydi.",
        "en": "JSON Reshape appears in Explorer's \"Open with\" list when you right-click a file ({command}). "
        "Written for your user account only; the default program does not change.",
    },
    "gui.os.remove": {"uz": "«Open with» ro'yxatidan olib tashlash", "en": "Remove from \"Open with\""},
    "gui.os.remove.tip": {
        "uz": "Dastur Windows ro'yxatiga yozgan yozuvlarni o'chiradi; boshqa dasturlarning yozuvlari o'zgarmaydi.",
        "en": "Deletes the entries the program wrote to the Windows registry; other programs' entries stay as "
        "they are.",
    },
    "gui.os.status.none": {"uz": "Hozir «Open with» ro'yxatida yo'q.", "en": "Not in the \"Open with\" list now."},
    "gui.os.status.current": {"uz": "«Open with» ro'yxatiga qo'shilgan.", "en": "Added to the \"Open with\" list."},
    "gui.os.status.other": {
        "uz": "Ro'yxatda dasturning boshqa nusxasi turibdi: {command}. Qo'shish uni shu nusxaga almashtiradi.",
        "en": "Another copy of the program is in the list: {command}. Adding replaces it with this copy.",
    },
    "gui.os.status.partial": {
        "uz": "Ro'yxatga qisman qo'shilgan (ba'zi yozuvlar yo'q). Qayta qo'shish buni tuzatadi.",
        "en": "Partly added (some entries are missing). Adding again repairs it.",
    },
    "gui.os.status.unknown": {
        "uz": "Windows ro'yxatini o'qib bo'lmadi: {error}",
        "en": "Could not read the Windows registry: {error}",
    },
    "gui.os.unavailable.platform": {"uz": "Bu faqat Windows'da ishlaydi.", "en": "This works on Windows only."},
    "gui.os.unavailable.exe": {"uz": "Bu faqat {name} dasturidan ishlaydi.", "en": "This works only from {name}."},
    "gui.os.unavailable.python": {
        "uz": "pythonw.exe topilmadi: {path}. Ishlab chiqish rejimida ro'yxatga «pythonw.exe -m jsonreshape.gui» "
        "qo'shiladi.",
        "en": "pythonw.exe was not found: {path}. In development the list gets \"pythonw.exe -m jsonreshape.gui\".",
    },
    "gui.os.confirm.title": {"uz": "Windows «Open with»", "en": "Windows \"Open with\""},
    "gui.os.confirm.add": {
        "uz": "JSON Reshape Windows'ning «Open with» ro'yxatiga qo'shilsinmi? U {extensions} fayllar uchun "
        "chiqadi.\n\nFaqat sizning foydalanuvchingiz uchun yoziladi (HKCU\\Software\\Classes). Standart dastur "
        "o'zgarmaydi: xohlasangiz, uni Windows'da o'zingiz tanlaysiz.\n\nBuyruq: {command}",
        "en": "Add JSON Reshape to the Windows \"Open with\" list? It appears for {extensions} files.\n\nOnly "
        "your user account is changed (HKCU\\Software\\Classes). The default program stays as it is: you can "
        "choose it yourself in Windows.\n\nCommand: {command}",
    },
    "gui.os.confirm.remove": {
        "uz": "JSON Reshape Windows'ning «Open with» ro'yxatidan olib tashlansinmi? Faqat dastur yozgan yozuvlar "
        "o'chiriladi.",
        "en": "Remove JSON Reshape from the Windows \"Open with\" list? Only the entries the program wrote are "
        "deleted.",
    },
    "gui.os.done.add": {
        "uz": "Qo'shildi. Explorer'da faylni o'ng tugma bilan bosing ▸ «Open with» ▸ JSON Reshape.",
        "en": "Added. In Explorer right-click a file ▸ \"Open with\" ▸ JSON Reshape.",
    },
    "gui.os.done.remove": {"uz": "«Open with» ro'yxatidan olib tashlandi.", "en": "Removed from the \"Open with\" list."},
    "gui.os.error.add": {
        "uz": "JSON Reshape'ni «Open with» ro'yxatiga qo'shib bo'lmadi: {detail}",
        "en": "Could not add JSON Reshape to the \"Open with\" list: {detail}",
    },
    "gui.os.error.add.hint": {
        "uz": "Windows ro'yxatiga yozishga ruxsat yo'q bo'lishi mumkin (masalan, tashkilot siyosati). Yozilgan "
        "qismini «olib tashlash» o'chiradi.",
        "en": "Writing to the registry may not be allowed (for example by an organization policy). \"Remove\" "
        "deletes whatever was written.",
    },
    "gui.os.error.remove": {
        "uz": "«Open with» ro'yxatidan olib tashlab bo'lmadi: {detail}",
        "en": "Could not remove from the \"Open with\" list: {detail}",
    },
    "gui.os.error.remove.hint": {
        "uz": "Windows ro'yxatini o'zgartirishga ruxsat yo'q bo'lishi mumkin. Keyinroq qayta urinib ko'ring.",
        "en": "Changing the registry may not be allowed. Try again later.",
    },
    "gui.os.type_name": {"uz": "JSON fayl (JSON Reshape)", "en": "JSON file (JSON Reshape)"},
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
        "uz": "Bu faylda xatcho'p yo'q. Tugunni tanlab Ctrl+B bosing.",
        "en": "This file has no bookmarks. Select a node and press Ctrl+B.",
    },
    "gui.nav.bookmarks.deleted": {"uz": "(o'chirilgan)", "en": "(deleted)"},
    "gui.nav.bookmarks.deleted.tip": {
        "uz": "Bu xatcho'p ko'rsatgan tugun tahrirda o'chirilgan.",
        "en": "The node of this bookmark was deleted by an edit.",
    },
    "gui.nav.goto.changed": {
        "uz": "Hujjat qidirish paytida o'zgardi; qaytadan qidirilmoqda.",
        "en": "The document changed while looking; looking again.",
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
    "gui.doctab.view.mapping": {"uz": "Tuzilma quruvchi", "en": "Structure builder"},
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
    "gui.doctab.changed.save_patch": {
        "uz": "O'zgarishlarni JSON Patch sifatida saqlash…",
        "en": "Save the changes as JSON Patch…",
    },
    "gui.doctab.changed.detail_dirty": {
        "uz": "Saqlanmagan o'zgarishlaringiz bor, lekin fayl o'zgargani uchun ularni faylga yozib bo'lmaydi. "
        "Ularni JSON Patch sifatida saqlab qo'ying, keyin faylni qayta oching.",
        "en": "You have unsaved changes, but they cannot be written into the file because it changed. "
        "Save them as a JSON Patch, then reopen the file.",
    },
    "gui.doctab.readonly": {
        "uz": "Bu hujjat hozir faqat ko'rish uchun ochiq.",
        "en": "This document is open for viewing only right now.",
    },
    "gui.doctab.edit_unavailable": {"uz": "Tahrirlash hozir mumkin emas.", "en": "Editing is not available right now."},
    "gui.doctab.recovery.found": {
        "uz": "Saqlanmagan o'zgarishlar topildi: {time}, {steps} ta qadam.",
        "en": "Unsaved changes were found: {time}, {steps} steps.",
    },
    "gui.doctab.recovery.found.detail": {
        "uz": "Oxirgi o'zgarish: {label}. Ular haqida qaror qilmaguningizcha faylni tahrirlab bo'lmaydi.",
        "en": "Last change: {label}. The file cannot be edited until you decide about them.",
    },
    "gui.doctab.recovery.found.detail_nolabel": {
        "uz": "Ular haqida qaror qilmaguningizcha faylni tahrirlab bo'lmaydi.",
        "en": "The file cannot be edited until you decide about them.",
    },
    "gui.doctab.recovery.restore": {"uz": "Tiklash", "en": "Restore"},
    "gui.doctab.recovery.discard": {"uz": "O'chirish…", "en": "Discard…"},
    "gui.doctab.recovery.save_patch": {"uz": "JSON Patch sifatida saqlash…", "en": "Save as JSON Patch…"},
    "gui.doctab.recovery.delete": {"uz": "O'chirish…", "en": "Delete…"},
    "gui.doctab.recovery.journal": {
        "uz": "Bu faylni saqlash tugallanmay qolgan ({time}).",
        "en": "Saving this file was interrupted ({time}).",
    },
    "gui.doctab.recovery.journal.detail": {
        "uz": "Davom etishdan oldin uzilib qolgan saqlashni yakunlash (yoki orqaga qaytarish) kerak.",
        "en": "Before going on, the interrupted save has to be finished (or undone).",
    },
    "gui.doctab.recovery.journal.finish": {"uz": "Saqlashni yakunlash", "en": "Finish the save"},
    "gui.doctab.recovery.journal_missing": {
        "uz": "Bu faylni saqlash tugallanmay qolgan ({time}): fayl o'z joyida yo'q.",
        "en": "Saving this file was interrupted ({time}): the file is not in its place.",
    },
    "gui.doctab.recovery.journal_missing.detail": {
        "uz": "«Saqlashni yakunlash» saqlashni oxiriga yetkazadi yoki faylni saqlashdan oldingi holatiga "
        "qaytaradi, so'ng uni qayta ochadi.",
        "en": "“Finish the save” completes the save or puts the file back as it was before it, then opens it "
        "again.",
    },
    "gui.doctab.recovery.journal_conflict": {
        "uz": "Uzilib qolgan saqlashni yakunlab ham, orqaga qaytarib ham bo'lmadi ({time}).",
        "en": "The interrupted save could neither be finished nor undone ({time}).",
    },
    "gui.doctab.recovery.journal_conflict.detail": {
        "uz": "{reason}\nSaqlanmagan o'zgarishlarni JSON Patch sifatida saqlab qo'yishingiz yoki o'chirishingiz "
        "mumkin; ko'rsatilgan fayllarni qo'lda tekshiring. Fayllarni tuzatgach, yakunlashga yana urinib "
        "ko'rish mumkin.",
        "en": "{reason}\nYou can save the unsaved changes as a JSON Patch or delete them; check the files named "
        "by hand. Once they are fixed, finishing can be tried again.",
    },
    "gui.doctab.recovery.journal_conflict.files": {
        "uz": "Fayllardan saqlash qayergacha yetgani aniq emas. Tegishli fayllar: {files}",
        "en": "The files do not show how far the save got. Files involved: {files}",
    },
    "gui.doctab.recovery.exported": {
        "uz": "O'zgarishlar JSON Patch sifatida saqlandi ({count} ta amal): {path}",
        "en": "The changes were saved as a JSON Patch ({count} operations): {path}",
    },
    "gui.doctab.recovery.delete_ask": {
        "uz": "{time} dagi saqlanmagan o'zgarishlar ({steps} ta qadam) butunlay o'chirilsinmi? Ularni qaytarib "
        "bo'lmaydi.",
        "en": "Delete the unsaved changes from {time} ({steps} steps) for good? This cannot be undone.",
    },
    "gui.doctab.task.journals": {
        "uz": "Uzilib qolgan saqlash qidirilmoqda",
        "en": "Looking for an interrupted save",
    },
    "gui.doctab.task.roll": {"uz": "{name}: saqlash yakunlanmoqda", "en": "{name}: finishing the save"},
    "gui.doctab.task.export": {
        "uz": "{name}: o'zgarishlar JSON Patch sifatida saqlanmoqda",
        "en": "{name}: saving the changes as a JSON Patch",
    },
    "gui.doctab.task.delete": {
        "uz": "{name}: saqlanmagan o'zgarishlar o'chirilmoqda",
        "en": "{name}: deleting the unsaved changes",
    },
    "gui.doctab.task.sweep": {"uz": "Ortiqcha fayllar tozalanmoqda", "en": "Removing leftover files"},
    "gui.doctab.recovery.damaged": {
        "uz": "Saqlanmagan o'zgarishlar ({time}, {steps} ta qadam) shikastlangan, ularni tiklab bo'lmaydi.",
        "en": "Unsaved changes ({time}, {steps} steps) are damaged and cannot be restored.",
    },
    "gui.doctab.recovery.damaged.detail": {
        "uz": "Sabab: {reason}. Ularni JSON Patch sifatida saqlab olishga urinib ko'ring yoki o'chiring.",
        "en": "Reason: {reason}. Try to save them as a JSON Patch, or delete them.",
    },
    "gui.doctab.recovery.stale": {
        "uz": "Faylning oldingi versiyasiga tegishli saqlanmagan o'zgarishlar topildi ({time}, {steps} ta qadam).",
        "en": "Unsaved changes of an earlier version of this file were found ({time}, {steps} steps).",
    },
    "gui.doctab.recovery.stale.detail": {
        "uz": "Fayl o'shandan beri o'zgargani uchun ularni qo'llab bo'lmaydi. Ularni JSON Patch sifatida saqlab "
        "qo'yishingiz yoki o'chirishingiz mumkin.",
        "en": "The file has changed since, so they cannot be applied. You can save them as a JSON Patch or "
        "delete them.",
    },
    "gui.doctab.recovery.locked": {
        "uz": "Bu faylning saqlanmagan o'zgarishlari boshqa oynada ochiq ({time}).",
        "en": "Unsaved changes of this file are open in another window ({time}).",
    },
    "gui.doctab.recovery.locked.detail": {
        "uz": "O'sha oyna yopilgach, faylni qayta ochsangiz, ularni shu yerda tiklash mumkin bo'ladi.",
        "en": "Once that window is closed, reopen the file to restore them here.",
    },
    "gui.doctab.recovery.more": {
        "uz": "Yana {count} ta saqlanmagan o'zgarishlar to'plami bor; ular navbat bilan ko'rsatiladi.",
        "en": "{count} more sets of unsaved changes follow, one after another.",
    },
    "gui.doctab.patch.title": {"uz": "JSON Patch sifatida saqlash", "en": "Save as JSON Patch"},
    "gui.doctab.patch.filter": {
        "uz": "JSON Patch (*.json);;Barcha fayllar (*)",
        "en": "JSON Patch (*.json);;All files (*)",
    },
    "gui.doctab.view_outdated": {
        "uz": "Ko'rinish eskirgan: yozuvlar o'zgardi, filtr va tartib natijasi yangilanmadi.",
        "en": "The view is out of date: records changed and the filter / sort result was not updated.",
    },
    "gui.doctab.view_outdated.detail": {
        "uz": "Hozir barcha yozuvlar ko'rsatilmoqda. Filtr va tartibni qayta hisoblash uchun \"Yangilash\"ni bosing.",
        "en": "All records are shown now. Press \"Refresh\" to compute the filter and the sort again.",
    },
    "gui.doctab.view_outdated.refresh": {"uz": "Yangilash", "en": "Refresh"},
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
    "gui.status.offset.edited": {"uz": "tahrirlangan", "en": "edited"},
    "gui.status.offset.edited.tip": {
        "uz": "Joriy tugun tahrirlangan: u saqlanmagan o'zgarishlarda turibdi, faylda hali joyi yo'q.",
        "en": "The current node is edited: it lives in the unsaved changes and has no place in the file yet.",
    },
    "gui.status.dirty": {"uz": "● {count} o'zgarish", "en": "● {count} changes"},
    "gui.status.dirty.tip": {
        "uz": "Saqlanmagan o'zgarishlar bor. Ctrl+S — saqlash, Ctrl+Z — ortga qaytarish.",
        "en": "There are unsaved changes. Ctrl+S saves them, Ctrl+Z undoes the last one.",
    },
    "gui.status.dirty.undo_tip": {"uz": "Oxirgi o'zgarish: {label}", "en": "Last change: {label}"},
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
    # ---- gui.importdialog ----
    # import of CSV / YAML / XML files (gui/importdialog.py, docs/ARCHITECTURE.md §17); the menu action of File
    "gui.importdialog.menu.import": {"uz": "&Import (CSV, YAML, XML)…", "en": "&Import (CSV, YAML, XML)…"},
    "gui.importdialog.menu.import.tip": {
        "uz": "CSV, YAML yoki XML fayldan JSON / JSON Lines fayl yasash va uni ochish (manba fayl o'zgarmaydi)",
        "en": "Make a JSON / JSON Lines file from a CSV, YAML or XML file and open it (the source is not changed)",
    },
    "gui.importdialog.title": {"uz": "Import — {name}", "en": "Import — {name}"},
    "gui.importdialog.title_none": {"uz": "Import (CSV, YAML, XML)", "en": "Import (CSV, YAML, XML)"},
    "gui.importdialog.intro": {
        "uz": "CSV / TSV, YAML yoki XML fayl JSON yoki JSON Lines fayliga aylantiriladi. Manba fayl o'zgarmaydi; "
              "sonlarning matni aynan saqlanadi (1.10 o'zgarmaydi, 007 matn qoladi).",
        "en": "A CSV / TSV, YAML or XML file is converted into a JSON or JSON Lines file. The source file is not "
              "changed; number texts are kept exactly (1.10 never changes, 007 stays a string).",
    },
    "gui.importdialog.browse": {"uz": "Tanlash…", "en": "Browse…"},
    "gui.importdialog.source.label": {"uz": "&Manba:", "en": "&Source:"},
    "gui.importdialog.source.title": {"uz": "Import qilinadigan faylni tanlash", "en": "Choose the file to import"},
    "gui.importdialog.source.filter": {
        "uz": "Import fayllari (*.csv *.tsv *.yaml *.yml *.xml);;CSV / TSV (*.csv *.tsv);;YAML (*.yaml *.yml);;"
              "XML (*.xml);;Barcha fayllar (*)",
        "en": "Import files (*.csv *.tsv *.yaml *.yml *.xml);;CSV / TSV (*.csv *.tsv);;YAML (*.yaml *.yml);;"
              "XML (*.xml);;All files (*)",
    },
    "gui.importdialog.source.need": {"uz": "Import qilinadigan faylni tanlang.", "en": "Choose the file to import."},
    "gui.importdialog.format.label": {"uz": "&Tur:", "en": "&Type:"},
    "gui.importdialog.format.auto": {"uz": "Kengaytma bo'yicha", "en": "By the extension"},
    "gui.importdialog.detected": {"uz": "Aniqlangan tur: {fmt}", "en": "Detected type: {fmt}"},
    "gui.importdialog.detected.none": {
        "uz": "Tur kengaytmadan aniqlanmadi: turni tanlang.",
        "en": "The type is not known from the extension: choose it.",
    },
    "gui.importdialog.unknown_format.hint": {
        "uz": "Turni (CSV, YAML yoki XML) ro'yxatdan tanlang.",
        "en": "Choose the type (CSV, YAML or XML) from the list.",
    },
    "gui.importdialog.target.label": {"uz": "&Natija:", "en": "&Output:"},
    "gui.importdialog.target.title": {"uz": "Natija faylini saqlash", "en": "Save the result"},
    "gui.importdialog.target.filter": {
        "uz": "JSON Lines (*.jsonl);;JSON (*.json);;Barcha fayllar (*)",
        "en": "JSON Lines (*.jsonl);;JSON (*.json);;All files (*)",
    },
    "gui.importdialog.target.need": {"uz": "Natija fayli uchun nom bering.", "en": "Give a name for the result file."},
    "gui.importdialog.same_file.hint": {
        "uz": "Natija hech qachon manba faylning ustiga yozilmaydi: boshqa nom tanlang.",
        "en": "The result is never written over the source file: choose another name.",
    },
    "gui.importdialog.overwrite": {
        "uz": "{path} allaqachon bor. Ustiga yozilsinmi?",
        "en": "{path} already exists. Overwrite it?",
    },
    "gui.importdialog.options": {"uz": "Parametrlar", "en": "Options"},
    "gui.importdialog.types.label": {"uz": "&Qiymatlar:", "en": "&Values:"},
    "gui.importdialog.types.auto": {
        "uz": "Son, true / false / null — avtomatik; qolgani matn",
        "en": "Numbers, true / false / null — automatically; the rest strings",
    },
    "gui.importdialog.types.string": {"uz": "Hammasi matn", "en": "Everything a string"},
    "gui.importdialog.types.tip": {
        "uz": "Aynan JSON son ko'rinishidagi katak son bo'ladi (1.10 o'zgarmaydi; 007, +1 matn qoladi), true / false / "
              "null — shunday qiymat",
        "en": "A cell that is exactly a JSON number becomes a number (1.10 stays 1.10; 007 and +1 stay strings), "
              "true / false / null become those values",
    },
    "gui.importdialog.indent.label": {"uz": "JSON &chekinishi:", "en": "JSON &indentation:"},
    "gui.importdialog.indent.2": {"uz": "2 bo'sh joy", "en": "2 spaces"},
    "gui.importdialog.indent.4": {"uz": "4 bo'sh joy", "en": "4 spaces"},
    "gui.importdialog.indent.tab": {"uz": "Tab", "en": "Tab"},
    "gui.importdialog.indent.compact": {"uz": "Ixcham (bo'sh joylarsiz)", "en": "Compact (no whitespace)"},
    "gui.importdialog.indent.tip": {
        "uz": "Faqat .json natija uchun; JSON Lines har doim ixcham",
        "en": "For a .json result only; JSON Lines is always compact",
    },
    "gui.importdialog.csv": {"uz": "CSV / TSV", "en": "CSV / TSV"},
    "gui.importdialog.delimiter.label": {"uz": "&Ajratuvchi:", "en": "&Delimiter:"},
    "gui.importdialog.delimiter.auto": {"uz": "Avtomatik ( , ; tab | )", "en": "Automatic ( , ; tab | )"},
    "gui.importdialog.delimiter.comma": {"uz": "Vergul ( , )", "en": "Comma ( , )"},
    "gui.importdialog.delimiter.semicolon": {"uz": "Nuqta-vergul ( ; )", "en": "Semicolon ( ; )"},
    "gui.importdialog.delimiter.tab": {"uz": "Tab", "en": "Tab"},
    "gui.importdialog.delimiter.pipe": {"uz": "Vertikal chiziq ( | )", "en": "Vertical bar ( | )"},
    "gui.importdialog.encoding.label": {"uz": "&Kodirovka:", "en": "&Encoding:"},
    "gui.importdialog.encoding.auto": {
        "uz": "Avtomatik (UTF-8, BOM, UTF-16)",
        "en": "Automatic (UTF-8, BOM, UTF-16)",
    },
    "gui.importdialog.encoding.tip": {
        "uz": "Ro'yxatdan tanlang yoki kodirovka nomini yozing (masalan, cp1251)",
        "en": "Choose from the list or type a codec name (for example cp1251)",
    },
    "gui.importdialog.empty.label": {"uz": "&Bo'sh katak:", "en": "&Empty cell:"},
    "gui.importdialog.empty.null": {"uz": "null", "en": "null"},
    "gui.importdialog.empty.string": {"uz": "Bo'sh matn (\"\")", "en": "Empty string (\"\")"},
    "gui.importdialog.header": {"uz": "Birinchi qator — ustun nomlari (&sarlavha)", "en": "The first row names the columns (&header)"},
    "gui.importdialog.header.tip": {
        "uz": "Belgilanmasa, ustunlar c1, c2, … deb nomlanadi",
        "en": "Unchecked: the columns are named c1, c2, …",
    },
    "gui.importdialog.nest": {"uz": "a.b.c ustunlar — ichma-ich &obyekt", "en": "a.b.c columns become nested &objects"},
    "gui.importdialog.nest.tip": {
        "uz": "Nuqtali ustun nomi ichma-ich obyekt bo'ladi: contact.city → {\"contact\": {\"city\": …}}; belgilanmasa, "
              "nom tekis kalit qoladi",
        "en": "A dotted column name becomes nested objects: contact.city → {\"contact\": {\"city\": …}}; unchecked, "
              "the name stays a flat key",
    },
    "gui.importdialog.skip_bad": {"uz": "Buzuq qatorlarni o'tkazib &yuborish", "en": "Skip malformed &rows"},
    "gui.importdialog.skip_bad.tip": {
        "uz": "Maydonlar soni boshqa yoki tirnoqlari buzuq qatorlar hisobotda sanaladi; belgilanmasa, birinchi "
              "bunday qatorda xato bilan to'xtaydi",
        "en": "Rows with another number of fields or broken quoting are counted in the report; unchecked, the first "
              "such row stops the import with an error",
    },
    "gui.importdialog.xml": {"uz": "XML", "en": "XML"},
    "gui.importdialog.record_tag.label": {"uz": "Yozuv &tegi:", "en": "Record &tag:"},
    "gui.importdialog.record_tag.placeholder": {
        "uz": "masalan: item (bo'sh — ildiz elementining bolalari)",
        "en": "for example: item (empty — the children of the root element)",
    },
    "gui.importdialog.record_path.label": {"uz": "Yozuv yo'&li:", "en": "Record &path:"},
    "gui.importdialog.record_path.placeholder": {
        "uz": "masalan: catalog/books/book (ildizdan)",
        "en": "for example: catalog/books/book (from the root)",
    },
    "gui.importdialog.xml.note": {
        "uz": "Atributlar \"@nom\", matn \"#text\", takrorlangan elementlar massiv bo'ladi; faqat matnli element — "
              "shu matn.",
        "en": "Attributes become \"@name\", text \"#text\", repeated elements arrays; an element with text only "
              "is that text.",
    },
    "gui.importdialog.run": {"uz": "&Import", "en": "&Import"},
    "gui.importdialog.run.tip": {
        "uz": "Faylni aylantirish va natijani yangi faylga yozish",
        "en": "Convert the file and write the result to a new file",
    },
    "gui.importdialog.stop": {"uz": "To'xtatish", "en": "Stop"},
    "gui.importdialog.task": {"uz": "Import: {name}", "en": "Import: {name}"},
    "gui.importdialog.progress": {"uz": "{done} / {total} · {rate}/s", "en": "{done} / {total} · {rate}/s"},
    "gui.importdialog.progress_eta": {
        "uz": "{done} / {total} · {rate}/s · {eta} qoldi",
        "en": "{done} / {total} · {rate}/s · {eta} left",
    },
    "gui.importdialog.cancelled": {"uz": "To'xtatildi; hech narsa yozilmadi.", "en": "Stopped; nothing was written."},
    "gui.importdialog.failed": {"uz": "Bajarilmadi: {error}", "en": "Failed: {error}"},
    "gui.importdialog.status.written": {"uz": "Import qilindi: {path}", "en": "Imported: {path}"},
    "gui.importdialog.confirm_stop": {
        "uz": "Import hali tugamagan. U to'xtatilib, oyna yopilsinmi? (Hech narsa yozilmaydi.)",
        "en": "The import has not finished. Stop it and close the window? (Nothing is written.)",
    },
    "gui.importdialog.report": {"uz": "Natija", "en": "Result"},
    "gui.importdialog.report.summary": {
        "uz": "{records} ta yozuv: {source} ({fmt}) → {output} ({kind}, {size}, {seconds})",
        "en": "{records} records: {source} ({fmt}) → {output} ({kind}, {size}, {seconds})",
    },
    "gui.importdialog.output.json": {"uz": "JSON", "en": "JSON"},
    "gui.importdialog.output.jsonl": {"uz": "JSON Lines", "en": "JSON Lines"},
    "gui.importdialog.open": {"uz": "Natijani &ochish", "en": "&Open the result"},
    "gui.importdialog.open.tip": {"uz": "Yangi varaqda ochish", "en": "Open it in a new tab"},
    # ---- gui.main ----
    "gui.main.folder_ignored": {
        "uz": "Papka ochilmaydi, faqat fayllar: {path}",
        "en": "Folders are not opened, only files: {path}",
    },
    "gui.main.jobs_continue": {
        "uz": "{name} yopildi; uning {count} ta ishi (eksport, bo'lish, taqqoslash va h.k.) fonda davom etmoqda, "
              "tugagach natijasi chiqadi.",
        "en": "{name} was closed; its {count} background job(s) (export, split, comparison, ...) go on, the result "
              "appears when they are done.",
    },
    "gui.main.jobs_running.title": {"uz": "Ishlar tugamagan", "en": "Jobs not finished"},
    "gui.main.jobs_running": {
        "uz": "{count} ta ish (eksport, bo'lish, birlashtirish, ta'mirlash, tekshirish yoki taqqoslash) hali "
              "tugamagan. Dastur yopilsa ular to'xtatiladi va natijasi yozilmaydi. Baribir yopilsinmi?",
        "en": "{count} job(s) (export, split, merge, repair, validation or comparison) have not finished yet. "
              "Closing the program stops them and their results are not written. Close anyway?",
    },
    "gui.main.jobs_stop.title": {"uz": "Tekshiruv tugamagan", "en": "Validation not finished"},
    "gui.main.jobs_stop": {
        "uz": "{name} hali JSON Schema bo'yicha tekshirilmoqda. Varaq yopilsa tekshiruv to'xtatiladi va natijasi "
              "yo'qoladi. Baribir yopilsinmi?",
        "en": "{name} is still being validated against a JSON Schema. Closing the tab stops the validation and "
              "its result is lost. Close anyway?",
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
    "gui.main.act.save": {"uz": "&Saqlash", "en": "&Save"},
    "gui.main.act.save.tip": {
        "uz": "O'zgarishlarni faylga yozish, uning o'z formatida (Ctrl+S)",
        "en": "Write the changes into the file, in its own format (Ctrl+S)",
    },
    "gui.main.act.save_as": {"uz": "&Boshqacha saqlash…", "en": "Save &As…"},
    "gui.main.act.save_as.tip": {
        "uz": "Hujjatni boshqa faylga shu formatda saqlash (Ctrl+Shift+S)",
        "en": "Save the document to another file in the same format (Ctrl+Shift+S)",
    },
    "gui.main.act.undo": {"uz": "&Ortga qaytarish", "en": "&Undo"},
    "gui.main.act.undo_step": {"uz": "&Ortga qaytarish: {label}", "en": "&Undo: {label}"},
    "gui.main.act.undo.tip": {"uz": "Oxirgi o'zgarishni ortga qaytarish (Ctrl+Z)", "en": "Undo the last change (Ctrl+Z)"},
    "gui.main.act.redo": {"uz": "&Qayta bajarish", "en": "&Redo"},
    "gui.main.act.redo_step": {"uz": "&Qayta bajarish: {label}", "en": "&Redo: {label}"},
    "gui.main.act.redo.tip": {
        "uz": "Ortga qaytarilgan o'zgarishni qayta bajarish (Ctrl+Y)",
        "en": "Do the undone change again (Ctrl+Y)",
    },
    "gui.main.view_undo.none": {
        "uz": "Tuzilma quruvchida ortga qaytariladigan o'zgarish yo'q (hujjat tahrirlarini qaytarish uchun boshqa "
              "ko'rinishga o'ting).",
        "en": "Nothing to undo in the structure builder (switch to another view to undo the document's edits).",
    },
    "gui.main.view_redo.none": {
        "uz": "Tuzilma quruvchida qayta bajariladigan o'zgarish yo'q.",
        "en": "Nothing to redo in the structure builder.",
    },
    "gui.main.act.bulk": {"uz": "O&mmaviy amallar…", "en": "&Bulk operations…"},
    "gui.main.act.bulk.tip": {
        "uz": "Ko'p yozuvni birdaniga o'zgartirish: almashtirish, kalitni nomlash, maydonni o'chirish… (Ctrl+H)",
        "en": "Change many records at once: replace, rename keys, delete fields… (Ctrl+H)",
    },
    "gui.main.act.patch_import": {"uz": "JSON Patch'ni &qo'llash…", "en": "&Import JSON Patch…"},
    "gui.main.act.patch_import.tip": {
        "uz": "RFC 6902 JSON Patch faylidagi o'zgarishlarni hujjatga qo'llash (bitta qadam)",
        "en": "Apply the changes of an RFC 6902 JSON Patch file to the document (one step)",
    },
    "gui.main.act.patch_export": {
        "uz": "O'zgarishlarni JSON Patch sifatida &saqlash…",
        "en": "&Export changes as JSON Patch…",
    },
    "gui.main.act.patch_export.tip": {
        "uz": "Saqlanmagan o'zgarishlarni RFC 6902 JSON Patch fayliga yozish",
        "en": "Write the unsaved changes into an RFC 6902 JSON Patch file",
    },
    "gui.main.act.discard": {"uz": "Barcha o'zgarishlarni &tashlab yuborish…", "en": "&Discard all changes…"},
    "gui.main.act.discard.tip": {
        "uz": "Saqlanmagan o'zgarishlarni o'chirib, faylni asl holiga qaytarish (avval so'raladi)",
        "en": "Delete the unsaved changes and show the file as it is (asks first)",
    },
    "gui.main.save.nothing": {"uz": "Saqlanmagan o'zgarish yo'q.", "en": "There are no unsaved changes."},
    "gui.main.save_as.title": {"uz": "Boshqacha saqlash — {name}", "en": "Save as — {name}"},
    "gui.main.save_as.filter": {"uz": "Hujjat formati ({patterns})", "en": "The document's format ({patterns})"},
    "gui.main.save_as.other_format.title": {"uz": "Boshqa format", "en": "Another format"},
    "gui.main.save_as.other_format": {
        "uz": "\"{ext}\" kengaytmasi {fmt} formatiga tegishli. \"Boshqacha saqlash\" hujjatni o'z formatida "
        "saqlaydi. Uning o'rniga eksport oynasi ochilsinmi?",
        "en": "The \"{ext}\" extension belongs to the {fmt} format. \"Save As\" keeps the document's own format. "
        "Open the export dialog instead?",
    },
    "gui.main.patch.import.title": {"uz": "JSON Patch'ni qo'llash", "en": "Import JSON Patch"},
    "gui.main.patch.export.title": {"uz": "O'zgarishlarni JSON Patch sifatida saqlash", "en": "Export changes as JSON Patch"},
    "gui.main.patch.filter": {
        "uz": "JSON Patch (*.json *.json-patch);;Barcha fayllar (*)",
        "en": "JSON Patch (*.json *.json-patch);;All files (*)",
    },
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
        "uz": "JSON Reshape {version}\nKatta JSON fayllarni ko'rish, qidirish, filtrlash va eksport qilish uchun.\n\nTezkor yadro: {backend}\n\nDastur to'liq oflayn ishlaydi: hech qanday ma'lumot tarmoqqa yuborilmaydi, telemetriya ham, yangilanish tekshiruvi ham yo'q.\n\n© 2026 Erkin Pardayev · github.com/Erkinbek · Telegram: @ErkinPardayev\nLitsenziya: Apache License 2.0. Claude Opus 5.5 yordamida yaratilgan.",
        "en": "JSON Reshape {version}\nView, search, filter and export huge JSON files.\n\nFast core: {backend}\n\nThe program works fully offline: nothing is sent over the network, there is no telemetry and no update check.\n\n© 2026 Erkin Pardayev · github.com/Erkinbek · Telegram: @ErkinPardayev\nLicense: Apache License 2.0. Built with Claude Opus 5.5.",
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
    """Switch the UI language (``uz``, ``en`` or ``ru``)."""
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


#: Languages that write numbers as ``1 234 567,5`` (space-grouped thousands, decimal comma).
SPACE_GROUPED_LANGUAGES = frozenset({"uz", "ru"})


def space_grouped() -> bool:
    """True when the current language groups thousands with a space and uses a decimal comma
    (``uz``, ``ru``); English keeps ``1,234,567.5``."""
    return _language in SPACE_GROUPED_LANGUAGES


_russian: dict[str, str] | None = None


def _russian_texts() -> dict[str, str]:
    """``i18n_ru.MESSAGES_RU``, imported on first use (the Uzbek / English UI never loads it)."""
    global _russian
    if _russian is None:
        from .i18n_ru import MESSAGES_RU

        _russian = MESSAGES_RU
    return _russian


def t(key: str, /, **params: object) -> str:
    """Translate ``key`` into the current language and format it with ``params``."""
    entry = MESSAGES.get(key)
    if entry is None:
        text = key
    elif _language == "ru":
        text = _russian_texts().get(key) or entry.get("en") or key
    else:
        text = entry.get(_language) or entry.get("en") or key
    if not params:
        return text
    try:
        return _formatter.vformat(text, (), _SafeDict(params))
    except (ValueError, IndexError, AttributeError, KeyError):
        return text
