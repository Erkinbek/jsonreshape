"""jsonreshape.importers (docs/ARCHITECTURE.md §17): CSV / TSV, YAML and XML -> JSON / JSON Lines.

Every format and option: sniffed and given delimiters, quoting and embedded newlines, header / no header, nested
columns, the value rules (exact numbers -- ``1.10`` stays ``1.10``, ``007`` stays a string -- literals, empty cells),
duplicate headers and nesting conflicts, malformed rows (error at the line / ``skip_bad``), encodings (UTF-8 BOM,
UTF-16, a given codec, bad bytes, UTF-32, gzip), the JSON array layouts (the bytes of ``codec.dumps``), YAML scalars
(exact numbers, YAML-only forms, dates, ``~``, binary, merge keys, aliases, non-string keys), YAML streams, the safe
loader, recursive and exploding aliases, the size limits, XML conventions (``@attr``, ``#text``, arrays, namespaces,
record tag / path), the entity refusal, malformed XML, the target refusals (the source itself, an edited file, a
folder), cancellation (nothing written), progress in bytes, and the memory of a 100 MB CSV in a subprocess.
"""

from __future__ import annotations

import csv
import gzip
import json
import os
import subprocess
import sys
import textwrap
import threading
from pathlib import Path
from typing import Any

import pytest

from jsonreshape import codec, importers
from jsonreshape.codec import JsonFloat, JsonInt
from jsonreshape.errors import Cancelled, FileFormatError, UserInputError
from jsonreshape.i18n import t
from jsonreshape.importers import ImportOptions, ImportReport, import_file
from jsonreshape.settings import Settings
from jsonreshape.tasks import CancelToken, Progress

ROOT = Path(__file__).resolve().parents[1]

CSV = ("id,name,price,contact.city,active,note,big,exp,neg\n"
       "1,Olma,1.10,Toshkent,true,,12345678901234567890,1e400,-0\n"
       '007,"Anor, katta",2,Samarqand,false,"ko\'p\nqatorli",+1,1.5e3,-0.0\n')
RECORDS = [
    {"id": 1, "name": "Olma", "price": JsonFloat("1.10"), "contact": {"city": "Toshkent"}, "active": True,
     "note": None, "big": JsonInt("12345678901234567890"), "exp": JsonFloat("1e400"), "neg": JsonInt("-0")},
    {"id": "007", "name": "Anor, katta", "price": 2, "contact": {"city": "Samarqand"}, "active": False,
     "note": "ko'p\nqatorli", "big": "+1", "exp": JsonFloat("1.5e3"), "neg": JsonFloat("-0.0")},
]


def write(path: Path, data: bytes | str, encoding: str = "utf-8") -> Path:
    if isinstance(data, str):
        data = data.encode(encoding)
    path.write_bytes(data)
    return path


def imp(src: Path, out: Path, **opts: Any) -> ImportReport:
    settings = opts.pop("settings", None)
    progress = opts.pop("progress", None)
    cancel = opts.pop("cancel", None)
    return import_file(src, out, ImportOptions(**opts), settings=settings, progress=progress, cancel=cancel)


def lines(path: Path) -> list[Any]:
    raw = path.read_bytes()
    assert raw.endswith(b"\n")
    return [codec.loads(line) for line in raw[:-1].split(b"\n")]


def texts(path: Path) -> list[str]:
    return path.read_text(encoding="utf-8").splitlines()


@pytest.fixture
def csv_file(tmp_path: Path) -> Path:
    return write(tmp_path / "a.csv", CSV)


# ----------------------------------------------------------------------------------------
# CSV: values, layouts, options
# ----------------------------------------------------------------------------------------
def test_csv_to_json_lines_keeps_numbers_exactly(csv_file: Path, tmp_path: Path) -> None:
    out = tmp_path / "a.jsonl"
    rep = imp(csv_file, out)
    assert lines(out) == RECORDS
    text = texts(out)
    assert '"price":1.10' in text[0] and '"big":12345678901234567890' in text[0] and '"exp":1e400' in text[0]
    assert text[0].endswith('"neg":-0}') and '"id":"007"' in text[1] and text[1].endswith('"neg":-0.0}')
    assert rep.fmt == "csv" and rep.output_fmt == "jsonl" and rep.records == 2
    assert rep.columns == ["id", "name", "price", "contact.city", "active", "note", "big", "exp", "neg"]
    assert rep.encoding == "utf-8" and rep.delimiter == ","
    assert rep.numbers == 8 and rep.literals == 2 and rep.empty_cells == 1 and rep.skipped == 0
    assert rep.bytes_read == csv_file.stat().st_size and rep.bytes_written == out.stat().st_size
    assert rep.note_keys == ["importers.note.numbers", "importers.note.literals", "importers.note.empty_null"]
    assert rep.notes[0] == "8 ta katak son bo'ldi." and rep.warnings == []
    assert csv_file.read_text(encoding="utf-8") == CSV          # the source is never changed


@pytest.mark.parametrize("indent", [2, 4, "\t", None])
def test_csv_to_json_array_has_the_writer_layout(csv_file: Path, tmp_path: Path, indent: Any) -> None:
    out = tmp_path / "a.json"
    rep = imp(csv_file, out, indent=indent)
    assert rep.output_fmt == "json"
    assert out.read_bytes() == codec.dumps(RECORDS, indent=indent)


def test_output_format_from_extension_or_option(csv_file: Path, tmp_path: Path) -> None:
    assert imp(csv_file, tmp_path / "a.ndjson").output_fmt == "jsonl"
    assert imp(csv_file, tmp_path / "a.txt").output_fmt == "jsonl"
    assert imp(csv_file, tmp_path / "b.txt", output="json").output_fmt == "json"
    assert codec.loads((tmp_path / "b.txt").read_bytes()) == RECORDS
    assert imp(csv_file, tmp_path / "c.json", output="jsonl").output_fmt == "jsonl"
    assert lines(tmp_path / "c.json") == RECORDS


def test_indent_values_are_normalized() -> None:
    assert ImportOptions(indent="tab").indent == "\t" and ImportOptions(indent="4").indent == 4
    assert ImportOptions(indent="compact").indent is None and ImportOptions(indent=None).indent is None
    with pytest.raises(UserInputError) as exc:
        ImportOptions(indent=17)
    assert exc.value.key == "importers.error.option" and "indent" in exc.value.message
    with pytest.raises(UserInputError):
        ImportOptions(types="numbers")
    with pytest.raises(UserInputError):
        ImportOptions(delimiter=",,")
    with pytest.raises(UserInputError) as exc:
        ImportOptions(record_tag="a", record_path="a/b")
    assert exc.value.key == "importers.error.record_both"


@pytest.mark.parametrize("delimiter, name", [(";", "s.csv"), ("\t", "t.csv"), ("|", "p.csv"), ("\t", "t.tsv")])
def test_delimiter_is_sniffed(tmp_path: Path, delimiter: str, name: str) -> None:
    rows = [["id", "name", "city"], ["1", "Olma, sariq", "Toshkent"], ["2", "Anor", "Buxoro; eski"]]
    src = tmp_path / name
    with open(src, "w", encoding="utf-8", newline="") as f:
        csv.writer(f, delimiter=delimiter).writerows(rows)
    out = tmp_path / "o.jsonl"
    rep = imp(src, out)
    assert rep.delimiter == delimiter
    assert lines(out) == [{"id": 1, "name": "Olma, sariq", "city": "Toshkent"},
                          {"id": 2, "name": "Anor", "city": "Buxoro; eski"}]


def test_given_delimiter_wins_over_the_sniff(tmp_path: Path) -> None:
    src = write(tmp_path / "a.csv", "a;b,c\n1;2,3\n")
    out = tmp_path / "o.jsonl"
    imp(src, out, delimiter=";")
    assert lines(out) == [{"a": 1, "b,c": "2,3"}]
    imp(src, out, delimiter="tab")
    assert lines(out) == [{"a;b,c": "1;2,3"}]
    assert ImportOptions(delimiter="\\t").delimiter == "\t"


def test_quoting_embedded_newlines_and_escaped_quotes(tmp_path: Path) -> None:
    src = write(tmp_path / "q.csv", 'a,b\n"x ""y"" z","line1\r\nline2"\n"",plain\n')
    out = tmp_path / "o.jsonl"
    imp(src, out)
    assert lines(out) == [{"a": 'x "y" z', "b": "line1\r\nline2"}, {"a": None, "b": "plain"}]


def test_no_header_names_columns_c1_cn(tmp_path: Path) -> None:
    src = write(tmp_path / "a.csv", "1,2,3\n4,5\n\n6,7,8,9\n")
    out = tmp_path / "o.jsonl"
    rep = imp(src, out, header=False)
    assert lines(out) == [{"c1": 1, "c2": 2, "c3": 3}, {"c1": 4, "c2": 5}, {"c1": 6, "c2": 7, "c3": 8, "c4": 9}]
    assert rep.columns == [] and rep.records == 3


def test_types_string_and_empty_string(csv_file: Path, tmp_path: Path) -> None:
    out = tmp_path / "o.jsonl"
    rep = imp(csv_file, out, types="string")
    first = lines(out)[0]
    assert first["id"] == "1" and first["price"] == "1.10" and first["active"] == "true" and first["note"] is None
    assert rep.numbers == 0 and rep.literals == 0 and rep.empty_cells == 1
    rep = imp(csv_file, out, empty="string")
    assert lines(out)[0]["note"] == "" and rep.note_keys[-1] == "importers.note.empty_string"
    rep = imp(csv_file, out, types="string", empty="string")
    assert lines(out)[0]["note"] == "" and lines(out)[0]["neg"] == "-0"


def test_value_rule_edge_cases(tmp_path: Path) -> None:
    cells = ["0", "-0", "007", "+1", "1.", ".5", "1,5", " 1", "1 ", "1e5", "1E+05", "0.10", "True", "NULL", "nan",
             "Infinity", "-", "1_000", "0x1F", "12345678901234567890123"]
    src = write(tmp_path / "v.csv", "v\n" + "\n".join(f'"{c}"' for c in cells) + "\n")
    out = tmp_path / "o.jsonl"
    imp(src, out)
    got = [codec.value_text(rec["v"]) for rec in lines(out)]
    assert got == cells                                  # every text is kept: as a number token or as a string
    kinds = [codec.json_type(rec["v"]) for rec in lines(out)]
    assert kinds == ["number", "number", "string", "string", "string", "string", "string", "string", "string",
                     "number", "number", "number", "string", "string", "string", "string", "string", "string",
                     "string", "number"]
    assert importers.number_value("1.10") == JsonFloat("1.10") and type(importers.number_value("12")) is int
    assert type(importers.number_value("1.5")) is float and importers.number_value("-0") == JsonInt("-0")


def test_nested_columns(tmp_path: Path) -> None:
    src = write(tmp_path / "n.csv", "a.b.c,a.b.d,a.e,x,tags.0,p..q,.r\n1,2,3,4,5,6,7\n")
    out = tmp_path / "o.jsonl"
    imp(src, out)
    assert lines(out) == [{"a": {"b": {"c": 1, "d": 2}, "e": 3}, "x": 4, "tags": {"0": 5}, "p..q": 6, ".r": 7}]
    imp(src, out, nest=False)
    assert lines(out) == [{"a.b.c": 1, "a.b.d": 2, "a.e": 3, "x": 4, "tags.0": 5, "p..q": 6, ".r": 7}]


def test_nesting_conflict_is_refused(tmp_path: Path) -> None:
    src = write(tmp_path / "n.csv", "a,a.b\n1,2\n")
    out = tmp_path / "o.jsonl"
    with pytest.raises(FileFormatError) as exc:
        imp(src, out)
    assert exc.value.key == "importers.error.csv_nest_conflict" and exc.value.params == {"first": "a", "second": "a.b"}
    assert "--no-nest" in (exc.value.hint or "") and not out.exists()
    imp(src, out, nest=False)
    assert lines(out) == [{"a": 1, "a.b": 2}]


def test_duplicate_header_names_the_columns(tmp_path: Path) -> None:
    src = write(tmp_path / "d.csv", "id,name,id,x,name\n1,2,3,4,5\n")
    with pytest.raises(FileFormatError) as exc:
        imp(src, tmp_path / "o.jsonl")
    assert exc.value.key == "importers.error.csv_duplicate_header"
    assert exc.value.params == {"name": "id", "columns": "1, 3"}
    assert exc.value.message == "CSV sarlavhasida «id» nomi takrorlangan (1, 3-ustunlar)."
    assert not (tmp_path / "o.jsonl").exists()


def test_empty_header_name_and_blank_lines(tmp_path: Path) -> None:
    src = write(tmp_path / "e.csv", "\n\nid,,name\n\n1,x,Olma\n\n")
    out = tmp_path / "o.jsonl"
    rep = imp(src, out)
    assert lines(out) == [{"id": 1, "c2": "x", "name": "Olma"}] and rep.columns == ["id", "c2", "name"]


def test_header_only_gives_no_records(tmp_path: Path) -> None:
    src = write(tmp_path / "h.csv", "id,name\n")
    out = tmp_path / "o.jsonl"
    rep = imp(src, out)
    assert out.read_bytes() == b"" and rep.records == 0 and rep.warning_keys == ["importers.warn.no_records"]
    rep = imp(src, tmp_path / "o.json")
    assert (tmp_path / "o.json").read_bytes() == b"[]" and rep.records == 0
    src = write(tmp_path / "empty.csv", "")
    rep = imp(src, out)
    assert rep.records == 0 and out.read_bytes() == b""


# ----------------------------------------------------------------------------------------
# CSV: malformed rows
# ----------------------------------------------------------------------------------------
def test_row_width_mismatch_is_an_error_at_the_line(tmp_path: Path) -> None:
    src = write(tmp_path / "w.csv", "a,b\n1,2\n3\n4,5,6\n7,8\n")
    out = tmp_path / "o.jsonl"
    with pytest.raises(FileFormatError) as exc:
        imp(src, out)
    assert exc.value.key == "importers.error.csv_row_width"
    assert exc.value.params == {"line": 3, "expected": 2, "got": 1}
    assert exc.value.message == "3-qator: 1 ta maydon, sarlavhada esa 2 ta."
    assert "--skip-bad" in (exc.value.hint or "") and not out.exists()


def test_skip_bad_counts_and_names_the_lines(tmp_path: Path) -> None:
    src = write(tmp_path / "w.csv", "a,b\n1,2\n3\n4,5,6\n7,8\n" + "9\n" * 10)
    out = tmp_path / "o.jsonl"
    rep = imp(src, out, skip_bad=True)
    assert lines(out) == [{"a": 1, "b": 2}, {"a": 7, "b": 8}]
    assert rep.skipped == 12 and rep.skipped_lines == [3, 4, 6, 7, 8]
    assert rep.warning_keys == ["importers.warn.skipped"]
    assert rep.warnings == ["12 ta buzuq qator o'tkazib yuborildi (birinchilari: 3, 4, 6, 7, 8-qatorlar)."]


def test_bad_quoting_is_an_error_or_skipped(tmp_path: Path) -> None:
    src = write(tmp_path / "q.csv", 'a,b\n1,2\n"x"y,3\n4,5\n')
    out = tmp_path / "o.jsonl"
    with pytest.raises(FileFormatError) as exc:
        imp(src, out)
    assert exc.value.key == "importers.error.csv_malformed" and exc.value.params["line"] == 3
    rep = imp(src, out, skip_bad=True)
    assert lines(out) == [{"a": 1, "b": 2}, {"a": 4, "b": 5}] and rep.skipped == 1 and rep.skipped_lines == [3]


def test_field_limit_bounds_an_unbalanced_quote(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    old = csv.field_size_limit()
    try:
        monkeypatch.setattr(importers, "FIELD_LIMIT", 40)
        csv.field_size_limit(40)
        src = write(tmp_path / "u.csv", 'a,b\n1,"open quote\n' + "x" * 200 + "\n")
        with pytest.raises(FileFormatError) as exc:
            imp(src, tmp_path / "o.jsonl")
        assert exc.value.key == "importers.error.csv_malformed" and "limit" in exc.value.params["detail"]
    finally:
        csv.field_size_limit(old)


# ----------------------------------------------------------------------------------------
# CSV: encodings
# ----------------------------------------------------------------------------------------
TEXT = "ism,shahar\nOʻzbek,Тошкент\n"


@pytest.mark.parametrize("encoding, codec_name", [("utf-8", "utf-8"), ("utf-8-sig", "utf-8-sig"),
                                                  ("utf-16", "utf-16"), ("utf-16-le", "utf-16"),
                                                  ("utf-16-be", "utf-16")])
def test_encodings_are_detected(tmp_path: Path, encoding: str, codec_name: str) -> None:
    data = TEXT.encode(encoding)
    if encoding in ("utf-16-le", "utf-16-be"):
        data = ("﻿" + TEXT).encode(encoding)         # a BOM written by hand
    src = write(tmp_path / "e.csv", data)
    out = tmp_path / "o.jsonl"
    rep = imp(src, out)
    assert lines(out) == [{"ism": "Oʻzbek", "shahar": "Тошкент"}] and rep.encoding == codec_name


def test_bomless_utf16_is_detected_from_the_ascii_start(tmp_path: Path) -> None:
    src = write(tmp_path / "e.csv", TEXT.encode("utf-16-le"))
    out = tmp_path / "o.jsonl"
    rep = imp(src, out)
    assert lines(out) == [{"ism": "Oʻzbek", "shahar": "Тошкент"}] and rep.encoding == "utf-16-le"


def test_given_encoding(tmp_path: Path) -> None:
    src = write(tmp_path / "c.csv", "ism,shahar\nАли,Тошкент\n".encode("cp1251"))
    out = tmp_path / "o.jsonl"
    rep = imp(src, out, encoding="cp1251")
    assert lines(out) == [{"ism": "Али", "shahar": "Тошкент"}] and rep.encoding == "cp1251"
    # utf-8 given still accepts a BOM
    src = write(tmp_path / "b.csv", TEXT.encode("utf-8-sig"))
    imp(src, out, encoding="utf-8")
    assert lines(out)[0]["ism"] == "Oʻzbek"
    with pytest.raises(UserInputError) as exc:
        ImportOptions(encoding="no-such-codec")
    assert exc.value.key == "importers.error.encoding_unknown"


def test_bad_bytes_name_the_line(tmp_path: Path) -> None:
    src = write(tmp_path / "bad.csv", b"a,b\n1,2\n3,\xff\xfe\n")
    out = tmp_path / "o.jsonl"
    with pytest.raises(FileFormatError) as exc:
        imp(src, out)
    assert exc.value.key == "importers.error.decode" and exc.value.params["encoding"] == "utf-8"
    assert exc.value.params["line"] == 3 and "--encoding" in (exc.value.hint or "")
    assert not out.exists()
    src = write(tmp_path / "late.csv", b"a,b\n" + b"1,2\n" * 300000 + b"3,\xff\n")
    with pytest.raises(FileFormatError) as exc:
        imp(src, out)
    assert exc.value.params["line"] == 300002
    src = write(tmp_path / "u16.csv", "a,b\n1,2\n3,".encode("utf-16") + b"\x00\xd8\n\x00")   # a lone surrogate
    with pytest.raises(FileFormatError) as exc:
        imp(src, out)
    assert exc.value.params["line"] == 3 and exc.value.params["encoding"] == "utf-16"


def test_utf32_and_gzip_are_refused(tmp_path: Path) -> None:
    src = write(tmp_path / "u.csv", TEXT.encode("utf-32"))
    with pytest.raises(FileFormatError) as exc:
        imp(src, tmp_path / "o.jsonl")
    assert exc.value.key == "importers.error.utf32"
    src = write(tmp_path / "g.csv", gzip.compress(TEXT.encode("utf-8")))
    with pytest.raises(UserInputError) as exc:
        imp(src, tmp_path / "o.jsonl")
    assert exc.value.key == "importers.error.gzip" and exc.value.hint


# ----------------------------------------------------------------------------------------
# common: format detection, targets, cancellation, progress
# ----------------------------------------------------------------------------------------
def test_format_by_extension_or_option(tmp_path: Path) -> None:
    assert importers.detect_format("a.CSV") == "csv" and importers.detect_format("a.tsv") == "csv"
    assert importers.detect_format("a.yml") == "yaml" and importers.detect_format("a.Yaml") == "yaml"
    assert importers.detect_format("a.xml") == "xml" and importers.detect_format("a.txt") is None
    assert importers.output_format_for("o.json") == "json" and importers.output_format_for("o.NDJSON") == "jsonl"
    assert importers.output_format_for("o.txt") is None
    src = write(tmp_path / "data.txt", CSV)
    with pytest.raises(UserInputError) as exc:
        imp(src, tmp_path / "o.jsonl")
    assert exc.value.key == "importers.error.unknown_format" and "--from" in (exc.value.hint or "")
    rep = imp(src, tmp_path / "o.jsonl", fmt="csv")
    assert rep.records == 2
    assert importers.format_name("csv") == "CSV / TSV" and importers.format_name("yaml") == "YAML"


def test_target_refusals(csv_file: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    with pytest.raises(UserInputError) as exc:
        imp(csv_file, csv_file)
    assert exc.value.key == "writer.error.same_file"
    with pytest.raises(UserInputError) as exc:
        imp(csv_file, tmp_path / "A.CSV" if os.name == "nt" else tmp_path / "a.csv")
    assert exc.value.key == "writer.error.same_file"
    with pytest.raises(UserInputError) as exc:
        imp(csv_file, tmp_path)
    assert exc.value.key == "fileio.error.is_directory"
    with pytest.raises(UserInputError) as exc:
        imp(csv_file, tmp_path / "missing" / "o.jsonl")
    assert exc.value.key == "writer.error.no_dir"
    with pytest.raises(UserInputError) as exc:
        imp(tmp_path / "nope.csv", tmp_path / "o.jsonl")
    assert exc.value.key == "error.file_not_found"
    assert csv_file.read_text(encoding="utf-8") == CSV
    # a target with unsaved edits in this program (the writer's rule) is refused before any work
    from jsonreshape import writer

    def refuse(paths: Any) -> None:
        raise UserInputError("filetools.error.part_open_edited", path=str(list(paths)[0]))

    monkeypatch.setattr(writer, "refuse_edited_targets", refuse)
    with pytest.raises(UserInputError) as exc:
        imp(csv_file, tmp_path / "edited.jsonl")
    assert exc.value.key == "filetools.error.part_open_edited" and not (tmp_path / "edited.jsonl").exists()


def big_csv(path: Path, rows: int) -> Path:
    with open(path, "w", encoding="utf-8", newline="") as f:
        f.write("id,name,price,contact.city,tags\n")
        for i in range(rows):
            f.write(f'{i},Nom {i},{i}.10,Toshkent,"a;b"\n')
    return path


def test_cancellation_leaves_nothing(tmp_path: Path) -> None:
    src = big_csv(tmp_path / "big.csv", 20000)
    out = tmp_path / "o.jsonl"
    token = CancelToken()
    seen: list[int] = []

    def on_progress(info: Any) -> None:
        seen.append(info.done)
        if info.done > 0:
            token.cancel()

    progress = Progress(callback=on_progress, min_interval=0)
    with pytest.raises(Cancelled):
        imp(src, out, progress=progress, cancel=token)
    assert not out.exists() and [p.name for p in tmp_path.iterdir()] == ["big.csv"]
    assert seen and seen[-1] < src.stat().st_size
    token = CancelToken()
    token.cancel()
    with pytest.raises(Cancelled):
        imp(src, out, cancel=token)
    assert not out.exists()


def test_progress_counts_bytes(tmp_path: Path) -> None:
    src = big_csv(tmp_path / "big.csv", 5000)
    out = tmp_path / "o.jsonl"
    infos: list[Any] = []
    progress = Progress(callback=infos.append, min_interval=0)
    rep = imp(src, out, progress=progress)
    size = src.stat().st_size
    assert progress.total == size and progress.done == size and progress.finished and progress.unit == "bytes"
    assert infos[-1].finished and infos[-1].fraction == 1.0 and infos[-1].message == "CSV o'qilmoqda"
    assert all(a.done <= b.done for a, b in zip(infos, infos[1:]))
    assert rep.records == 5000 and lines(out)[4999] == {"id": 4999, "name": "Nom 4999", "price": JsonFloat("4999.10"),
                                                        "contact": {"city": "Toshkent"}, "tags": "a;b"}


def test_import_options_rejects_unknown_formats() -> None:
    with pytest.raises(UserInputError):
        ImportOptions(fmt="json")
    with pytest.raises(UserInputError):
        ImportOptions(output="csv")
    assert ImportOptions(fmt="CSV").fmt == "csv"


def test_report_to_dict_is_json(csv_file: Path, tmp_path: Path) -> None:
    rep = imp(csv_file, tmp_path / "o.jsonl")
    data = json.loads(codec.dumps(rep.to_dict()))
    assert data["records"] == 2 and data["format"] == "csv" and data["output_format"] == "jsonl"
    assert data["columns"][3] == "contact.city" and data["numbers"] == 8 and data["note_keys"][0] == "importers.note.numbers"


def test_texts_in_english(csv_file: Path, tmp_path: Path, lang_en) -> None:
    src = write(tmp_path / "w.csv", "a,b\n1,2\n3\n")
    rep = imp(src, tmp_path / "o.jsonl", skip_bad=True)
    assert rep.warnings == ["1 malformed row was skipped (it ends at line 3)."]
    assert rep.notes == ["2 cells became numbers."]
    rep = imp(write(tmp_path / "one.csv", "a,b\n1,\n"), tmp_path / "o.jsonl")
    assert rep.notes == ["1 cell became a number.", "1 empty cell became null."]


# ----------------------------------------------------------------------------------------
# memory: a 100 MB CSV in a subprocess
# ----------------------------------------------------------------------------------------
_MEMORY_SCRIPT = textwrap.dedent(r'''
    import json, os, sys, time
    import psutil
    from jsonreshape.importers import import_file
    src, dst = sys.argv[1], sys.argv[2]
    t0 = time.perf_counter()
    rep = import_file(src, dst)
    dt = time.perf_counter() - t0
    mem = psutil.Process().memory_info()
    peak = getattr(mem, "peak_wset", None)
    if peak is None:   # POSIX: ru_maxrss (KiB on Linux)
        import resource
        peak = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss * 1024
    print(json.dumps({"bytes": os.path.getsize(src), "seconds": dt, "peak": peak, "records": rep.records,
                      "out": os.path.getsize(dst)}))
''')


@pytest.mark.slow
def test_100mb_csv_converts_with_bounded_memory(tmp_path: Path) -> None:
    src = tmp_path / "big.csv"
    row = '{i},"Toshkent shahri, {i}-uy",{i}.10,{i}50,true,,"a;b;c",2026-10-{d:02d},O\'zbekiston {i}\n'
    with open(src, "w", encoding="utf-8", newline="") as f:
        f.write("id,address.street,price,zip,active,note,tags,date,country\n")
        i = 0
        while f.tell() < 100 * 1000 * 1000:
            f.write("".join(row.format(i=i + k, d=(i + k) % 28 + 1) for k in range(1000)))
            i += 1000
    env = dict(os.environ, PYTHONIOENCODING="utf-8")
    proc = subprocess.run([sys.executable, "-c", _MEMORY_SCRIPT, str(src), str(tmp_path / "big.jsonl")], cwd=ROOT,
                          env=env, capture_output=True, text=True, timeout=600)
    assert proc.returncode == 0, proc.stderr
    data = json.loads(proc.stdout.strip().splitlines()[-1])
    mb = data["bytes"] / 1e6
    print(f"\nimport of a {mb:.0f} MB CSV: {data['records']} records in {data['seconds']:.1f} s "
          f"({mb / data['seconds']:.0f} MB/s), peak {data['peak'] / 1e6:.0f} MB")
    assert data["records"] == i and data["peak"] < 200 * 1000 * 1000
    with open(tmp_path / "big.jsonl", "rb") as f:
        first = codec.loads(f.readline())
    assert first == {"id": 0, "address": {"street": "Toshkent shahri, 0-uy"}, "price": JsonFloat("0.10"), "zip": "050",
                     "active": True, "note": None, "tags": "a;b;c", "date": "2026-10-01", "country": "O'zbekiston 0"}


# ----------------------------------------------------------------------------------------
# YAML
# ----------------------------------------------------------------------------------------
YAML = textwrap.dedent("""\
    exact: 1.10
    neg: -0
    big: 12345678901234567890
    exp: 1.5e+3
    hex: 0x1F
    octal: 017
    o12: 0o17
    under: 1_000
    plus: +12
    sexa: 1:30
    dot5: .5
    one: 1.
    notnum: 1e3
    inf: .inf
    nan: .nan
    nil: ~
    empty:
    yes_: yes
    off_: off
    date: 2001-12-14
    stamp: 2001-12-14t21:59:43.10-05:00
    bin: !!binary |
      R0lGODlhDAAM
      AIQAAP//9/X1
    base: &base {k: 1, l: [1, 2]}
    merged:
      <<: *base
      z: 2
    again: *base
    2: int key
    true: bool key
    ~: null key
    2001-01-01: date key
    set: !!set {a, b}
    omap: !!omap [{x: 1}, {y: 2}]
    text: "Oʻzbekiston"
    """)


def test_yaml_document_to_json(tmp_path: Path) -> None:
    src = write(tmp_path / "a.yaml", YAML)
    out = tmp_path / "a.json"
    rep = imp(src, out)
    assert rep.fmt == "yaml" and rep.output_fmt == "json" and rep.records == 1
    doc = codec.loads(out.read_bytes())
    text = out.read_text(encoding="utf-8")
    assert '"exact": 1.10' in text and '"neg": -0,' in text and '"big": 12345678901234567890' in text
    assert '"exp": 1.5e+3' in text
    assert doc["hex"] == 31 and doc["octal"] == 15 and doc["under"] == 1000 and doc["plus"] == 12 and doc["sexa"] == 90
    assert doc["dot5"] == 0.5 and doc["one"] == 1.0 and doc["notnum"] == "1e3" and doc["o12"] == "0o17"
    assert doc["inf"] is None and doc["nan"] is None and doc["nil"] is None and doc["empty"] is None
    assert doc["yes_"] is True and doc["off_"] is False
    assert doc["date"] == "2001-12-14" and doc["stamp"] == "2001-12-14T21:59:43.100000-05:00"
    assert doc["bin"] == "R0lGODlhDAAMAIQAAP//9/X1"
    assert doc["merged"] == {"k": 1, "l": [1, 2], "z": 2} and doc["again"] == {"k": 1, "l": [1, 2]}
    assert doc["2"] == "int key" and doc["true"] == "bool key" and doc["null"] == "null key"
    assert "1" not in doc                 # PyYAML: the keys 1 and true would be ONE Python dict key (True == 1)
    assert doc["2001-01-01"] == "date key"
    assert doc["set"] == {"a": None, "b": None} and doc["omap"] == [["x", 1], ["y", 2]]
    assert doc["text"] == "Oʻzbekiston"
    assert rep.yaml_values == 6 and rep.nonfinite == 2 and rep.dates == 3 and rep.binaries == 1   # 1_000: exact
    assert rep.warning_keys == ["importers.warn.yaml_nonfinite"]
    assert rep.warnings == ["2 ta .inf / .nan qiymati null bo'ldi (JSON'da cheksizlik va NaN yo'q)."]
    assert rep.note_keys == ["importers.note.yaml_values", "importers.note.dates", "importers.note.binary"]
    # the layout is codec.dumps(doc, indent=2) of the same value
    assert out.read_bytes() == codec.dumps(doc, indent=2)
    imp(src, out, indent="\t")
    assert out.read_bytes() == codec.dumps(doc, indent="\t")
    imp(src, out, indent=None)
    assert out.read_bytes() == codec.dumps(doc)


def test_yaml_stream_to_json_lines(tmp_path: Path) -> None:
    src = write(tmp_path / "s.yaml", "---\na: 1\n---\n- 1.10\n- x\n---\nplain\n...\n")
    out = tmp_path / "s.jsonl"
    rep = imp(src, out)
    assert rep.output_fmt == "jsonl" and rep.records == 3
    assert texts(out) == ['{"a":1}', '[1.10,"x"]', '"plain"']
    rep = imp(src, tmp_path / "s.json")
    assert rep.output_fmt == "json" and codec.loads((tmp_path / "s.json").read_bytes()) == [{"a": 1}, [JsonFloat("1.10"), "x"], "plain"]
    # one document into a .jsonl target: one line; into an untyped target: JSON
    rep = imp(write(tmp_path / "one.yaml", "a: 1\n"), tmp_path / "one.jsonl")
    assert rep.output_fmt == "jsonl" and texts(tmp_path / "one.jsonl") == ['{"a":1}']
    rep = imp(tmp_path / "one.yaml", tmp_path / "one.out")
    assert rep.output_fmt == "json" and (tmp_path / "one.out").read_bytes() == b'{\n  "a": 1\n}'


def test_empty_yaml_gives_no_records(tmp_path: Path) -> None:
    src = write(tmp_path / "e.yaml", "# only a comment\n")
    rep = imp(src, tmp_path / "e.json")
    assert rep.records == 0 and (tmp_path / "e.json").read_bytes() == b"[]"
    assert rep.warning_keys == ["importers.warn.no_records"]
    rep = imp(src, tmp_path / "e.jsonl")
    assert (tmp_path / "e.jsonl").read_bytes() == b""


def test_yaml_scalars_at_the_root(tmp_path: Path) -> None:
    assert imp(write(tmp_path / "n.yaml", "1.10\n"), tmp_path / "n.json").records == 1
    assert (tmp_path / "n.json").read_bytes() == b"1.10"
    imp(write(tmp_path / "s.yaml", "Salom\n"), tmp_path / "s.json")
    assert (tmp_path / "s.json").read_bytes() == b'"Salom"'


def test_yaml_errors(tmp_path: Path) -> None:
    src = write(tmp_path / "bad.yaml", "a: 1\nb: [1, 2\nc: 3\n")
    with pytest.raises(FileFormatError) as exc:
        imp(src, tmp_path / "o.json")
    assert exc.value.key == "importers.error.yaml" and exc.value.params["line"] == 3
    assert exc.value.message.startswith("YAML xatosi (3-qator, ") and "-ustun): " in exc.value.message
    assert not (tmp_path / "o.json").exists()
    # unsafe tags never construct objects
    src = write(tmp_path / "evil.yaml", "!!python/object/apply:os.system ['echo x']\n")
    with pytest.raises(FileFormatError) as exc:
        imp(src, tmp_path / "o.json")
    assert exc.value.key == "importers.error.yaml"
    # a recursive alias cannot be JSON
    src = write(tmp_path / "rec.yaml", "a: &a\n  b: *a\n")
    with pytest.raises(UserInputError) as exc:
        imp(src, tmp_path / "o.json")
    assert exc.value.key == "importers.error.yaml_recursive"
    src = write(tmp_path / "rec2.yaml", "- &a [1, *a]\n")
    with pytest.raises(UserInputError):
        imp(src, tmp_path / "o.json")
    # aliases that would expand to billions of values
    bomb = "a: &a [x, x, x, x, x, x, x, x, x, x]\n"
    prev = "a"
    for name in "bcdefgh":
        bomb += f"{name}: &{name} [*{prev}, *{prev}, *{prev}, *{prev}, *{prev}, *{prev}, *{prev}, *{prev}, *{prev}, *{prev}]\n"
        prev = name
    src = write(tmp_path / "bomb.yaml", bomb)
    with pytest.raises(UserInputError) as exc:
        imp(src, tmp_path / "o.json")
    assert exc.value.key == "importers.error.yaml_expanded" and not (tmp_path / "o.json").exists()


def test_yaml_shared_aliases_are_expanded(tmp_path: Path) -> None:
    src = write(tmp_path / "al.yaml", "a: &a [1, 2]\nb: *a\nc: {x: *a, y: *a}\n")
    imp(src, tmp_path / "o.json", indent=None)
    assert (tmp_path / "o.json").read_bytes() == b'{"a":[1,2],"b":[1,2],"c":{"x":[1,2],"y":[1,2]}}'


def test_yaml_missing_library(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setitem(sys.modules, "yaml", None)
    src = write(tmp_path / "a.yaml", "a: 1\n")
    with pytest.raises(UserInputError) as exc:
        imp(src, tmp_path / "o.json")
    assert exc.value.key == "importers.error.yaml_missing" and "pip install pyyaml" in (exc.value.hint or "")
    assert not (tmp_path / "o.json").exists()


def test_yaml_size_limits(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    src = write(tmp_path / "a.yaml", "a: 1\nb: [1, 2, 3]\n")
    settings = Settings(memory_limit_bytes=4)
    with pytest.raises(UserInputError) as exc:
        imp(src, tmp_path / "o.json", settings=settings)
    assert exc.value.key == "importers.error.yaml_too_big" and exc.value.params["limit"] == "8 B"
    monkeypatch.setattr(importers, "YAML_BIG_BYTES", 4)
    rep = imp(src, tmp_path / "o.json")
    assert rep.warning_keys == ["importers.warn.yaml_big"] and rep.records == 1


def test_yaml_encodings(tmp_path: Path) -> None:
    text = "ism: Oʻzbek\nshahar: Тошкент\n"
    for enc in ("utf-8", "utf-8-sig", "utf-16"):
        src = write(tmp_path / f"{enc}.yaml", text.encode(enc))
        imp(src, tmp_path / "o.json", indent=None)
        assert codec.loads((tmp_path / "o.json").read_bytes()) == {"ism": "Oʻzbek", "shahar": "Тошкент"}
    src = write(tmp_path / "c.yaml", "ism: Али\n".encode("cp1251"))
    imp(src, tmp_path / "o.json", encoding="cp1251")
    assert codec.loads((tmp_path / "o.json").read_bytes()) == {"ism": "Али"}
    src = write(tmp_path / "g.yaml", gzip.compress(b"a: 1\n"))
    with pytest.raises(UserInputError) as exc:
        imp(src, tmp_path / "o.json")
    assert exc.value.key == "importers.error.gzip"


def test_yaml_cancellation_and_progress(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(importers, "READ_BUFFER", 1 << 16)       # several reads of the ~900 KB file
    src = tmp_path / "big.yaml"
    with open(src, "w", encoding="utf-8") as f:
        for i in range(20000):
            f.write(f"- {{id: {i}, name: Nom {i}, price: {i}.10}}\n")
    infos: list[Any] = []
    progress = Progress(callback=infos.append, min_interval=0)
    rep = imp(src, tmp_path / "o.json", progress=progress, indent=None)
    size = src.stat().st_size
    assert rep.records == 1 and progress.total == size and progress.done == size and progress.finished
    assert infos[-1].message == "YAML tahlil qilinmoqda" and any(0 < i.done < size for i in infos)
    doc = codec.loads((tmp_path / "o.json").read_bytes())
    assert len(doc) == 20000 and doc[7]["price"] == JsonFloat("7.10")
    token = CancelToken()
    progress = Progress(callback=lambda info: token.cancel() if info.done > 0 else None, min_interval=0)
    with pytest.raises(Cancelled):
        imp(src, tmp_path / "c.json", progress=progress, cancel=token)
    assert not (tmp_path / "c.json").exists() and not [p for p in tmp_path.iterdir() if p.suffix == ".tmp"]


# ----------------------------------------------------------------------------------------
# XML
# ----------------------------------------------------------------------------------------
XML = textwrap.dedent("""\
    <?xml version="1.0" encoding="UTF-8"?>
    <!DOCTYPE catalog SYSTEM "catalog.dtd">
    <!-- a comment -->
    <catalog xmlns:x="urn:x" xmlns="urn:default" version="1.10">
      <book id="1" x:lang="uz">
        <title>Oʻzbek adabiyoti</title>
        <tag>a</tag>
        <tag>b</tag>
        <x:price cur="USD">1.10</x:price>
        <empty/>
        <blank>   </blank>
        <nested><a><b>deep</b></a></nested>
      </book>
      <book id="007">
        text before <note xml:lang="en">n</note> text after
      </book>
      <meta count="2"/>
    </catalog>
    """)


def test_xml_children_of_the_root_are_records(tmp_path: Path) -> None:
    src = write(tmp_path / "c.xml", XML)
    out = tmp_path / "c.jsonl"
    rep = imp(src, out)
    assert rep.fmt == "xml" and rep.records == 3 and rep.root_tag == "catalog"
    got = lines(out)
    assert got[0] == {"@id": 1, "@x:lang": "uz", "title": "Oʻzbek adabiyoti", "tag": ["a", "b"],
                      "x:price": {"@cur": "USD", "#text": JsonFloat("1.10")}, "empty": None, "blank": None,
                      "nested": {"a": {"b": "deep"}}}
    assert got[1] == {"@id": "007", "note": {"@xml:lang": "en", "#text": "n"}, "#text": "text before text after"}
    assert got[2] == {"@count": 2}
    assert '"#text":1.10' in texts(out)[0] and rep.numbers == 3 and rep.literals == 0
    rep = imp(src, out, types="string")
    assert lines(out)[0]["@id"] == "1" and lines(out)[0]["x:price"]["#text"] == "1.10" and rep.numbers == 0


def test_xml_record_tag_and_path(tmp_path: Path) -> None:
    src = write(tmp_path / "t.xml", "<r><g><item id='1'><item id='inner'/></item></g><g><x:item xmlns:x='u' id='2'/>"
                                    "<other/></g></r>")
    out = tmp_path / "t.jsonl"
    rep = imp(src, out, record_tag="item")
    assert lines(out) == [{"@id": 1, "item": {"@id": "inner"}}, {"@id": 2}] and rep.records == 2
    imp(src, out, record_path="r/g")
    assert lines(out) == [{"item": {"@id": 1, "item": {"@id": "inner"}}}, {"x:item": {"@id": 2}, "other": None}]
    imp(src, out, record_path="/r/g/item")
    assert lines(out) == [{"@id": 1, "item": {"@id": "inner"}}]
    rep = imp(src, out, record_path="nope/g")
    assert rep.records == 0 and out.read_bytes() == b"" and rep.warning_keys == ["importers.warn.xml_no_records"]
    assert rep.warnings[0].startswith("Birorta ham yozuv topilmadi (ildiz elementi: <r>).")
    rep = imp(src, out, record_tag="r")
    assert rep.records == 1 and lines(out)[0]["g"][0]["item"]["@id"] == 1


def test_xml_json_array_layout(tmp_path: Path) -> None:
    src = write(tmp_path / "c.xml", XML)
    out = tmp_path / "c.json"
    imp(src, out, indent=4)
    imp(src, tmp_path / "c.jsonl")
    assert out.read_bytes() == codec.dumps(lines(tmp_path / "c.jsonl"), indent=4)


def test_xml_entities_are_refused(tmp_path: Path) -> None:
    evil = ('<?xml version="1.0"?>\n<!DOCTYPE lolz [\n <!ENTITY lol "lol">\n <!ENTITY lol2 "&lol;&lol;&lol;">\n]>\n'
            "<r><a>&lol2;</a></r>")
    src = write(tmp_path / "e.xml", evil)
    with pytest.raises(UserInputError) as exc:
        imp(src, tmp_path / "o.jsonl")
    assert exc.value.key == "importers.error.xml_entities" and not (tmp_path / "o.jsonl").exists()


def test_xml_entity_check_spans_chunks(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(importers, "CHUNK_BYTES", 37)
    head = '<?xml version="1.0"?>\n<!DOCTYPE r [ <!ENTITY e "x"> ]>\n<r><a>&e;</a></r>'
    assert head.encode().find(b"<!ENTITY") // 37 != (head.encode().find(b"<!ENTITY") + 7) // 37   # cut by a chunk
    src = write(tmp_path / "e.xml", head)
    with pytest.raises(UserInputError) as exc:
        imp(src, tmp_path / "o.jsonl")
    assert exc.value.key == "importers.error.xml_entities"


def test_xml_streams_across_chunks(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(importers, "CHUNK_BYTES", 100)
    src = tmp_path / "many.xml"
    with open(src, "w", encoding="utf-8") as f:
        f.write("<root>\n")
        for i in range(2000):
            f.write(f'  <item n="{i}"><v>{i}.10</v><t>Oʻzbek {i}</t></item>\n')
        f.write("</root>\n")
    out = tmp_path / "o.jsonl"
    infos: list[Any] = []
    progress = Progress(callback=infos.append, min_interval=0)
    rep = imp(src, out, progress=progress)
    assert rep.records == 2000 and lines(out)[1999] == {"@n": 1999, "v": JsonFloat("1999.10"), "t": "Oʻzbek 1999"}
    assert progress.done == progress.total == src.stat().st_size and infos[-1].message == "XML o'qilmoqda"
    token = CancelToken()
    progress = Progress(callback=lambda info: token.cancel() if info.done > 300 else None, min_interval=0)
    with pytest.raises(Cancelled):
        imp(src, tmp_path / "c.jsonl", progress=progress, cancel=token)
    assert not (tmp_path / "c.jsonl").exists()


def test_xml_deep_nesting_and_utf16(tmp_path: Path) -> None:
    depth = 3000
    src = write(tmp_path / "deep.xml", "<r><rec>" + "<a>" * depth + "x" + "</a>" * depth + "</rec></r>")
    out = tmp_path / "o.jsonl"
    rep = imp(src, out)
    node = lines(out)[0]
    for _ in range(depth):
        node = node["a"]
    assert node == "x" and rep.records == 1
    src = write(tmp_path / "u16.xml", '<?xml version="1.0" encoding="UTF-16"?><r><a>Тошкент</a></r>'.encode("utf-16"))
    imp(src, out)
    assert lines(out) == ["Тошкент"]


def test_xml_malformed_names_the_place(tmp_path: Path) -> None:
    src = write(tmp_path / "bad.xml", "<r>\n  <a>1</a>\n  <b>2</c>\n</r>")
    out = tmp_path / "o.jsonl"
    with pytest.raises(FileFormatError) as exc:
        imp(src, out)
    assert exc.value.key == "importers.error.xml" and exc.value.params["line"] == 3 and exc.value.params["column"] >= 8
    assert "mismatched tag" in exc.value.params["reason"] and not out.exists()
    src = write(tmp_path / "cut.xml", "<r><a>1</a>")
    with pytest.raises(FileFormatError) as exc:
        imp(src, out)
    assert exc.value.key == "importers.error.xml"
    src = write(tmp_path / "empty.xml", "")
    with pytest.raises(FileFormatError):
        imp(src, out)
    assert not out.exists()


def test_xml_records_are_values_when_text_only(tmp_path: Path) -> None:
    src = write(tmp_path / "v.xml", "<r><n>1.10</n><n>true</n><n>007</n><n/><n>  x  </n></r>")
    out = tmp_path / "v.jsonl"
    imp(src, out)
    assert texts(out) == ["1.10", "true", '"007"', "null", '"x"']


# ----------------------------------------------------------------------------------------
# threads: a shared loader class is safe to use from several threads
# ----------------------------------------------------------------------------------------
def test_yaml_imports_in_parallel_threads(tmp_path: Path) -> None:
    errors: list[BaseException] = []

    def work(n: int) -> None:
        try:
            src = write(tmp_path / f"{n}.yaml", f"n: {n}\nv: {n}.10\nd: 2001-01-0{n % 9 + 1}\n")
            imp(src, tmp_path / f"{n}.json", indent=None)
            assert (tmp_path / f"{n}.json").read_bytes() == f'{{"n":{n},"v":{n}.10,"d":"2001-01-0{n % 9 + 1}"}}'.encode()
        except BaseException as exc:  # noqa: BLE001 - collected
            errors.append(exc)

    threads = [threading.Thread(target=work, args=(n,)) for n in range(8)]
    for th in threads:
        th.start()
    for th in threads:
        th.join()
    assert not errors, errors
