"""jsonreshape.smartvalues (docs/ARCHITECTURE.md §14.0 / §14.7; AGENT_PROMPT §9 "Aqlli ko'rinishlar"):
unix seconds / milliseconds (exact, range limits), ISO 8601 dates and date-times (offsets, ``Z``,
fractions, ``24:00``, invalid calendar dates, the pre-1992 note), Asia/Tashkent as UTC+05:00 with
Uzbek weekday names, URLs and ``mailto:``, colours, base64 images (PNG / JPEG / GIF / WebP with
their sizes, data URLs, MIME line breaks, the URL-safe alphabet) and what must NOT be detected."""

from __future__ import annotations

import base64
import zlib
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal

import pytest

from jsonreshape import codec
from jsonreshape.i18n import set_language
from jsonreshape.smartvalues import TASHKENT, ImageInfo, detect, image_bytes, tashkent_time


# ----------------------------------------------------------------------------------
# unix times
# ----------------------------------------------------------------------------------
def test_unix_seconds(lang_uz) -> None:
    sv = detect(1627551368)
    assert sv is not None and sv.kind == "unix_s" and sv.text == "1627551368"
    assert sv.utc == datetime(2021, 7, 29, 9, 36, 8, tzinfo=timezone.utc)
    assert sv.local == sv.utc and sv.local.utcoffset() == timedelta(hours=5)
    assert sv.local.hour == 14
    assert sv.tooltip.splitlines() == ["Unix vaqti (soniyalarda)",
                                       "Toshkent vaqti: 2021-07-29 14:36:08, payshanba (UTC+05:00)",
                                       "UTC: 2021-07-29 09:36:08"]
    set_language("en")
    assert detect(1627551368).tooltip.splitlines()[1] == "Tashkent time: 2021-07-29 14:36:08, Thursday (UTC+05:00)"


def test_unix_milliseconds_and_fractions(lang_en) -> None:
    sv = detect(1627551368123)
    assert sv.kind == "unix_ms" and sv.utc == datetime(2021, 7, 29, 9, 36, 8, 123000, tzinfo=timezone.utc)
    assert "14:36:08.123" in sv.tooltip
    sv = detect("1627551368.5", "number")
    assert sv.kind == "unix_s" and sv.utc.microsecond == 500000 and "14:36:08.500" in sv.tooltip
    sv = detect(Decimal("1627551368.000001"))
    assert sv.utc.microsecond == 1 and "14:36:08.000001" in sv.tooltip
    # exact tokens, never through a float
    sv = detect(codec.loads(b"1.627551368123E9"))
    assert sv.kind == "unix_s" and sv.utc.microsecond == 123000
    sv = detect(codec.loads(b"1627551368123.999"))
    assert sv.kind == "unix_ms" and sv.utc.microsecond == 123999
    # whatever the thread's Decimal context
    import decimal

    with decimal.localcontext() as ctx:
        ctx.prec = 3
        assert detect(1627551368123).utc == datetime(2021, 7, 29, 9, 36, 8, 123000, tzinfo=timezone.utc)


@pytest.mark.parametrize("value, kind", [
    (1_000_000_000, "unix_s"), (4_000_000_000, "unix_s"), (999_999_999, None), (4_000_000_001, None),
    (10**12, "unix_ms"), (4 * 10**12, "unix_ms"), (10**12 - 1, None), (4 * 10**12 + 1, None),
    (0, None), (-1627551368, None), (codec.loads(b"1e400"), None), (codec.loads(b"-0"), None),
    (True, None), (None, None), (3.5, None),
])
def test_unix_ranges(value, kind) -> None:
    sv = detect(value)
    assert (sv.kind if sv else None) == kind
    if kind == "unix_s" and value == 1_000_000_000:
        assert sv.utc == datetime(2001, 9, 9, 1, 46, 40, tzinfo=timezone.utc)


def test_numeric_strings_are_not_times() -> None:
    assert detect("1627551368") is None
    assert detect("1627551368", "string") is None
    assert detect("998901234567") is None
    assert detect("not a number", "number") is None


# ----------------------------------------------------------------------------------
# ISO 8601
# ----------------------------------------------------------------------------------
def test_iso_dates(lang_uz) -> None:
    sv = detect("2024-01-01")
    assert sv.kind == "date" and sv.day == date(2024, 1, 1) and sv.utc is None
    assert sv.tooltip.splitlines() == ["Sana (ISO 8601)", "2024-01-01, dushanba"]
    assert detect("2024-02-29").day == date(2024, 2, 29)
    for bad in ("2023-02-29", "2024-13-01", "2024-00-10", "2024-1-1", "24-01-01", "2024-01-01x", "2024/01/01",
                "٢٠٢٤-٠١-٠١"):          # (Arabic-Indic digits)
        assert detect(bad) is None, bad
    names = [detect(f"2024-01-0{d}").tooltip.splitlines()[1].split(", ")[1] for d in range(1, 8)]
    assert names == ["dushanba", "seshanba", "chorshanba", "payshanba", "juma", "shanba", "yakshanba"]


def test_iso_date_times_with_zones(lang_en) -> None:
    sv = detect("2024-01-01T10:00:00Z")
    assert sv.kind == "datetime" and sv.utc == datetime(2024, 1, 1, 10, tzinfo=timezone.utc)
    assert sv.local == datetime(2024, 1, 1, 15, tzinfo=TASHKENT)
    assert sv.tooltip.splitlines() == ["Date and time (ISO 8601)",
                                       "Tashkent time: 2024-01-01 15:00:00, Monday (UTC+05:00)",
                                       "UTC: 2024-01-01 10:00:00"]
    sv = detect("2024-01-01 10:00:00+03:00")
    assert sv.utc == datetime(2024, 1, 1, 7, tzinfo=timezone.utc) and sv.local.hour == 12
    assert detect("2024-01-01t10:00:00-0530").utc == datetime(2024, 1, 1, 15, 30, tzinfo=timezone.utc)
    assert detect("2024-01-01T10:00:00+05").utc == datetime(2024, 1, 1, 5, tzinfo=timezone.utc)
    sv = detect("2024-01-01T10:00:00.123456789Z")
    assert sv.utc.microsecond == 123456 and "15:00:00.123456" in sv.tooltip
    assert "15:00:00.5," in detect("2024-01-01T10:00:00,5Z").tooltip
    # the end of the day
    assert detect("2024-12-31T24:00:00Z").utc == datetime(2025, 1, 1, tzinfo=timezone.utc)
    assert detect("2024-12-31T24:00Z").utc == datetime(2025, 1, 1, tzinfo=timezone.utc)
    for bad in ("2024-01-01T24:00:01Z", "2024-01-01T23:59:60Z", "2024-01-01T25:00:00Z", "2024-01-01T10:60:00Z",
                "2024-01-01T10:00:00+24:00", "2024-01-01T10:00:00+05:60", "2024-01-01T10", "2024-01-01T10:00:00ZZ",
                "0001-01-01T00:00:00+05:00", "9999-12-31T23:00:00-05:00"):
        assert detect(bad) is None, bad


def test_iso_without_zone_and_old_dates(lang_en) -> None:
    sv = detect("2024-03-08T09:30")
    assert sv.kind == "datetime" and sv.utc is None and sv.local == datetime(2024, 3, 8, 9, 30)
    assert sv.tooltip.splitlines()[1] == "2024-03-08 09:30:00, Friday (no time zone given)"
    sv = detect("1985-06-01T12:00:00Z")
    assert sv.local.year == 1985 and "Before 1992 Tashkent used other time rules" in sv.tooltip
    assert "Before 1992" not in detect("1992-01-01T00:00:00+05:00").tooltip


def test_tashkent_time_is_a_fixed_offset() -> None:
    for month in range(1, 13):        # no daylight saving time
        utc = datetime(2024, month, 15, 12, tzinfo=timezone.utc)
        assert tashkent_time(utc).utcoffset() == timedelta(hours=5) and tashkent_time(utc).hour == 17


# ----------------------------------------------------------------------------------
# links and colours
# ----------------------------------------------------------------------------------
def test_urls(lang_en) -> None:
    sv = detect("https://example.com/a?b=1#c")
    assert sv.kind == "url" and sv.url == "https://example.com/a?b=1#c"
    assert sv.tooltip.startswith("Link: https://example.com/a?b=1#c") and "you are asked first" in sv.tooltip
    assert detect("HTTP://EXAMPLE.COM").kind == "url"
    assert detect("http://127.0.0.1:8080/x").kind == "url"
    for bad in ("http://", "https://", "https:///path", "https://exa mple.com", "ftp://example.com",
                "javascript:alert(1)", "https://example.com\u202e", "https://example.com\n", " https://example.com",
                "https://" + "a" * 9000, "file:///C:/x"):
        assert detect(bad) is None, bad


def test_mailto(lang_uz) -> None:
    sv = detect("mailto:ali@example.uz")
    assert sv.kind == "email" and sv.url == "mailto:ali@example.uz"
    assert sv.tooltip.startswith("E-pochta manzili: ali@example.uz")
    assert detect("MAILTO:ali@example.uz?subject=Salom").kind == "email"
    for bad in ("mailto:", "mailto:ali", "mailto:ali @x.uz", "mailto:@x.uz"):
        assert detect(bad) is None, bad


@pytest.mark.parametrize("text, rgba, hx", [
    ("#fff", (255, 255, 255, 255), "#ffffff"), ("#FF8800", (255, 136, 0, 255), "#ff8800"),
    ("#11223344", (17, 34, 51, 68), "#11223344"), ("#000", (0, 0, 0, 255), "#000000"),
])
def test_colours(text, rgba, hx, lang_en) -> None:
    sv = detect(text)
    assert sv.kind == "color" and sv.rgba == rgba and sv.hex == hx
    assert sv.tooltip.startswith(f"Colour: {hx} (R {rgba[0]}, G {rgba[1]}, B {rgba[2]}")
    if len(hx) == 9:
        assert "opacity 27 %" in sv.tooltip


@pytest.mark.parametrize("text", ["#ffff", "#12345", "#ggg", "#", "fff", "#fffffff", "# fff", "#ff ff ff"])
def test_not_colours(text) -> None:
    assert detect(text) is None


# ----------------------------------------------------------------------------------
# base64 images
# ----------------------------------------------------------------------------------
def png(width: int, height: int) -> bytes:
    def chunk(tag: bytes, data: bytes) -> bytes:
        return len(data).to_bytes(4, "big") + tag + data + zlib.crc32(tag + data).to_bytes(4, "big")

    ihdr = width.to_bytes(4, "big") + height.to_bytes(4, "big") + b"\x08\x02\x00\x00\x00"
    raw = b"".join(b"\x00" + b"\x10\x20\x30" * width for _ in range(height))
    return b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", ihdr) + chunk(b"IDAT", zlib.compress(raw)) + chunk(b"IEND", b"")


def gif(width: int, height: int) -> bytes:
    return b"GIF89a" + width.to_bytes(2, "little") + height.to_bytes(2, "little") + b"\x00\x00\x00" + b"\x3b"


def jpeg(width: int, height: int) -> bytes:
    app0 = b"\xff\xe0" + (16).to_bytes(2, "big") + b"JFIF\x00\x01\x01\x00\x00\x01\x00\x01\x00\x00"
    sof = b"\xff\xc0" + (17).to_bytes(2, "big") + b"\x08" + height.to_bytes(2, "big") + width.to_bytes(2, "big") \
        + b"\x03\x01\x22\x00\x02\x11\x01\x03\x11\x01"
    return b"\xff\xd8" + app0 + sof + b"\xff\xda\x00\x02" + b"\x00" * 10 + b"\xff\xd9"


def webp(kind: str, width: int, height: int) -> bytes:
    if kind == "VP8X":
        body = b"VP8X" + (10).to_bytes(4, "little") + b"\x00\x00\x00\x00" \
            + (width - 1).to_bytes(3, "little") + (height - 1).to_bytes(3, "little")
    elif kind == "VP8L":
        bits = (width - 1) | ((height - 1) << 14)
        body = b"VP8L" + (5).to_bytes(4, "little") + b"\x2f" + bits.to_bytes(4, "little")
    else:
        body = b"VP8 " + (10).to_bytes(4, "little") + b"\x00\x00\x00\x9d\x01\x2a" \
            + width.to_bytes(2, "little") + height.to_bytes(2, "little")
    return b"RIFF" + (4 + len(body)).to_bytes(4, "little") + b"WEBP" + body


IMAGES = [("PNG", png(3, 2), (3, 2)), ("GIF", gif(640, 480), (640, 480)), ("JPEG", jpeg(800, 600), (800, 600)),
          ("WEBP", webp("VP8X", 1920, 1080), (1920, 1080)), ("WEBP", webp("VP8L", 100, 50), (100, 50)),
          ("WEBP", webp("VP8 ", 300, 200), (300, 200))]


@pytest.mark.parametrize("fmt, data, size", IMAGES)
def test_base64_images(fmt, data, size, lang_en) -> None:
    text = base64.b64encode(data).decode()
    sv = detect(text)
    assert sv is not None and sv.kind == "image"
    assert sv.image == ImageInfo(fmt, f"image/{fmt.lower()}", size[0], size[1], False)
    assert sv.tooltip.splitlines() == [f"Image: {fmt}, {size[0]}×{size[1]} pixels",
                                       "The full-value window shows a small copy of the image."]
    assert image_bytes(text) == data
    # a data URL, MIME line breaks
    url = f"data:image/{fmt.lower()};base64," + text
    assert detect(url).image == ImageInfo(fmt, f"image/{fmt.lower()}", size[0], size[1], True)
    wrapped = "\n".join(text[i:i + 76] for i in range(0, len(text), 76))
    assert detect(wrapped).image.width == size[0] and image_bytes(wrapped) == data


def test_big_and_url_safe_images(lang_en) -> None:
    data = png(1000, 700)
    big = data + b"\x00" * 300_000                       # the size is read from the first 64 KiB
    text = base64.b64encode(big).decode()
    sv = detect(text)
    assert sv.image.width == 1000 and sv.image.height == 700
    assert image_bytes(text) == big
    assert image_bytes(text, limit=1000) is None
    safe = base64.urlsafe_b64encode(jpeg(5, 7) + b"\xfb\xff" * 10).decode()
    assert "-" in safe or "_" in safe
    assert detect(safe).image.format == "JPEG"
    # the signature alone is no image (review R5A-12: it was "an image of unknown size" whose
    # thumbnail could not be decoded): the size must be readable
    sv = detect(base64.b64encode(b"\x89PNG\r\n\x1a\n").decode())
    assert sv is None or sv.kind != "image"


@pytest.mark.parametrize("text", [
    "Toshkent", "iVBORw0KGgo", "iVBORw0KGgo!!!!", "R0lGODlh" + "*" * 20,
    base64.b64encode(b"RIFF\x00\x00\x00\x00WAVEfmt ").decode(),       # RIFF but not WebP
    base64.b64encode(b"not an image at all, just text").decode(),
    "data:image/png;base64,bm90IGFuIGltYWdl", "data:text/plain;base64," + base64.b64encode(png(1, 1)).decode(),
    "", " ", "1", "a" * 50,
])
def test_not_images(text) -> None:
    sv = detect(text)
    assert sv is None or sv.kind != "image"
    assert image_bytes(text) is None


def test_kinds_and_types() -> None:
    assert detect("https://example.com", "number") is None
    assert detect(1627551368, "string") is None
    assert detect({"a": 1}) is None and detect([1]) is None and detect(None) is None
    assert detect(1627551368, "number").kind == "unix_s"
    assert detect("#fff", "string").kind == "color"
