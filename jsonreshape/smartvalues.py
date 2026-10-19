"""Smart values (docs/ARCHITECTURE.md §14.0 / §14.7; AGENT_PROMPT §9 "Aqlli ko'rinishlar").

:func:`detect` ``(value, kind=None) -> SmartValue | None`` recognizes, in one JSON scalar, what a
view can show in a friendlier way -- pure and Qt-free; the GUI views (tree value column, table
cells, the full-value window) draw the result:

* **Unix times** (numbers only): seconds in ``[1e9, 4e9]`` (2001-09-09 .. 2096-10-02) and
  milliseconds in ``[1e12, 4e12]``, fractions allowed, read exactly from the number token (never
  through a float). Numeric *strings* are not taken as times (ids and phone numbers look alike).
* **ISO 8601** strings: dates ``YYYY-MM-DD`` and date-times ``YYYY-MM-DD[T| ]HH:MM[:SS[.fraction]]``
  with an optional ``Z`` / ``+-HH:MM`` / ``+-HHMM`` / ``+-HH`` offset (the extended format of RFC
  3339; ``24:00`` is the end of the day; leap seconds ``:60`` are not accepted). Real calendar
  dates only (``2023-02-29`` is not one).
* **Times are shown in Asia/Tashkent time as a fixed UTC+05:00** (:data:`TASHKENT`): exact since
  1992 -- Uzbekistan has no daylight saving time since then; no ``tzdata`` dependency (README §15,
  ARCHITECTURE §14.0). Older instants get a note (:data:`TASHKENT_SINCE`). A date-time without an
  offset is shown as written ("no time zone given"), a date without conversion. Weekday names
  are localized (Uzbek: dushanba ... yakshanba).
* **URLs**: ``http://`` / ``https://`` with a host, ``mailto:`` with an address -- the whole string,
  no whitespace or control characters, at most :data:`URL_MAX_CHARS`. The GUI opens them only
  after an explicit confirmation (the program itself sends nothing; §11.0 amended in §14.0).
* **Colours**: ``#rgb``, ``#rrggbb``, ``#rrggbbaa`` (case-insensitive) -> RGBA and a normalized hex
  text for a swatch.
* **Base64 images**: a ``data:image/<type>;base64,`` URL or a bare base64 string (standard or
  URL-safe alphabet, whitespace ignored) whose first characters spell the PNG / JPEG / GIF / WebP
  magic; at most the first :data:`IMAGE_PREFIX_BYTES` (64 KiB) are decoded to confirm the magic
  and read the size (PNG ``IHDR``, GIF screen, JPEG ``SOF``, WebP ``VP8``/``VP8L``/``VP8X``). Without
  a readable size it is no image (the signature alone, a header cut short or corrupt -- review
  R5A-12); only a JPEG whose metadata segments run on past the 64 KiB read is an image of unknown
  size. :func:`image_bytes` decodes the whole image for a thumbnail.

Every :class:`SmartValue` carries a localized multi-line ``tooltip`` (``smartvalues.*`` texts in
the language current when :func:`detect` ran).
"""

from __future__ import annotations

import base64
import binascii
import logging
import re
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from decimal import Context, Decimal, InvalidOperation
from typing import Any
from urllib.parse import urlsplit

from . import codec
from .i18n import t

log = logging.getLogger(__name__)

__all__ = [
    "IMAGE_PREFIX_BYTES",
    "TASHKENT",
    "TASHKENT_SINCE",
    "ImageInfo",
    "SmartValue",
    "detect",
    "image_bytes",
    "tashkent_time",
]

KiB = 1 << 10
MiB = 1 << 20

#: Asia/Tashkent as a fixed offset (exact since 1992: no daylight saving time)
TASHKENT = timezone(timedelta(hours=5))
TASHKENT_SINCE = 1992          # instants before this year get a note
UNIX_SECONDS = (Decimal(10) ** 9, Decimal(4) * Decimal(10) ** 9)
UNIX_MILLISECONDS = (Decimal(10) ** 12, Decimal(4) * Decimal(10) ** 12)
URL_MAX_CHARS = 8192
IMAGE_PREFIX_BYTES = 64 * KiB  # decoded to confirm an image and read its size
IMAGE_MAX_BYTES = 32 * MiB     # image_bytes() decodes at most this much
B64_MIN_CHARS = 12             # a bare base64 image is at least this long (an 8-byte signature)
_EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)
_CTX = Context(prec=60)        # exact scaling of unix times (whatever the thread's context)

_COLOR_RE = re.compile(r"#([0-9A-Fa-f]{3}|[0-9A-Fa-f]{6}|[0-9A-Fa-f]{8})")
_ISO_RE = re.compile(
    r"(\d{4})-(\d{2})-(\d{2})"
    r"(?:[Tt ](\d{2}):(\d{2})(?::(\d{2})(?:[.,](\d+))?)?"
    r"(Z|z|[+-]\d{2}(?::?\d{2})?)?)?", re.ASCII)      # ASCII digits only
_URL_RE = re.compile(r"(?i)(https?)://\S+")
_MAILTO_RE = re.compile(r"(?i)mailto:([^\s@?]+@[^\s@?]+)(?:\?\S*)?")
_BAD_URL_CHARS = re.compile(r"[\x00-\x20\x7f-\x9f  ‪-‮⁦-⁩﻿]")
_DATA_URL_RE = re.compile(r"data:(image/(?:png|jpeg|jpg|gif|webp));base64,", re.I)
_B64_CHARS = re.compile(r"[A-Za-z0-9+/=\s]*")
_B64URL_CHARS = re.compile(r"[A-Za-z0-9\-_=\s]*")
_WS_RE = re.compile(r"\s+")
# the base64 spelling of the image signatures (standard alphabet; URL-safe: "-" "_")
_B64_MAGIC = (("iVBORw0KGgo", "PNG"), ("/9j/", "JPEG"), ("_9j_", "JPEG"), ("R0lGOD", "GIF"), ("UklGR", "WEBP"))
_MIMES = {"PNG": "image/png", "JPEG": "image/jpeg", "GIF": "image/gif", "WEBP": "image/webp"}


@dataclass(frozen=True)
class ImageInfo:
    """A base64 image: ``format`` (PNG, JPEG, GIF, WEBP), ``mime``, ``width`` / ``height``
    (``None`` when the size is not in the decoded prefix), ``data_url`` (``data:...`` form)."""

    format: str
    mime: str
    width: int | None
    height: int | None
    data_url: bool


@dataclass(frozen=True)
class SmartValue:
    """What :func:`detect` found.

    ``kind``: ``"unix_s"``, ``"unix_ms"``, ``"datetime"``, ``"date"``, ``"url"``, ``"email"``,
    ``"color"`` or ``"image"``; ``text``: the value's text (number token / the string);
    ``tooltip``: localized multi-line display text; ``utc``: the instant (aware datetime, UTC) of
    times with a known zone; ``local``: the instant in Tashkent time (UTC+05:00), or the date-time
    as written when it has no zone; ``day``: the calendar date of a ``date``; ``url``: what to open
    (``http(s)://`` or ``mailto:``); ``rgba``: ``(r, g, b, a)`` 0..255 and ``hex`` the normalized
    ``#rrggbb`` / ``#rrggbbaa``; ``image``: :class:`ImageInfo`."""

    kind: str
    text: str
    tooltip: str
    utc: datetime | None = None
    local: datetime | None = None
    day: date | None = None
    url: str | None = None
    rgba: tuple[int, int, int, int] | None = None
    hex: str | None = None
    image: ImageInfo | None = None


# ----------------------------------------------------------------------------------
# entry point
# ----------------------------------------------------------------------------------
def detect(value: Any, kind: str | None = None) -> SmartValue | None:
    """The smart value of a JSON scalar, or ``None``.

    ``value``: a decoded string, a number (``codec`` types, ``Decimal``) or a number token text;
    ``kind``: ``"string"`` / ``"number"`` (the JSON type of the node; ``None``: from the Python
    type -- a ``str`` is a string then)."""
    if kind is None:
        if isinstance(value, bool) or value is None:
            return None
        if isinstance(value, str):
            kind = "string"
        elif isinstance(value, (int, float, Decimal)):
            kind = "number"
        else:
            return None
    try:
        if kind == "number":
            text = value if isinstance(value, str) else codec.number_text(value)
            return _unix_time(text.strip())
        if kind == "string" and isinstance(value, str):
            return _string(value)
    except (ValueError, OverflowError, ArithmeticError, TypeError) as exc:   # pragma: no cover - defensive
        log.debug("smartvalues: %r not recognized: %s", value[:80] if isinstance(value, str) else value, exc)
    return None


def _string(s: str) -> SmartValue | None:
    if not s:
        return None
    c = s[0]
    if c == "#":
        return _color(s)
    if c.isdigit():
        return _iso(s)
    if c in "hH":
        return _url(s)
    if c in "mM":
        return _mailto(s)
    if c in "dD" and s[:5].lower() == "data:":
        return _image(s)
    if len(s) >= B64_MIN_CHARS and (c.isalnum() or c in "/_"):
        return _image(s)
    return None


# ----------------------------------------------------------------------------------
# times
# ----------------------------------------------------------------------------------
def tashkent_time(instant: datetime) -> datetime:
    """``instant`` (aware) in Asia/Tashkent time (fixed UTC+05:00, module docstring)."""
    return instant.astimezone(TASHKENT)


def _unix_time(text: str) -> SmartValue | None:
    try:
        v = Decimal(text)
    except (InvalidOperation, ValueError):
        return None
    if not v.is_finite() or v <= 0:
        return None
    if UNIX_SECONDS[0] <= v <= UNIX_SECONDS[1]:
        kind = "unix_s"
        micros = _CTX.multiply(v, Decimal(1_000_000))
    elif UNIX_MILLISECONDS[0] <= v <= UNIX_MILLISECONDS[1]:
        kind = "unix_ms"
        micros = _CTX.multiply(v, Decimal(1000))
    else:
        return None
    utc = _EPOCH + timedelta(microseconds=int(micros))      # exact (truncated to microseconds)
    local = tashkent_time(utc)
    frac = 3 if kind == "unix_ms" or micros % 1_000_000 else 0
    if kind == "unix_s" and micros % 1000:
        frac = 6
    lines = [t("smartvalues." + kind), _tashkent_line(local, frac), t("smartvalues.utc", date=utc.date().isoformat(),
                                                                      time=_clock(utc, frac))]
    if local.year < TASHKENT_SINCE:   # pragma: no cover - unix ranges start in 2001
        lines.append(t("smartvalues.old_offset", year=TASHKENT_SINCE))
    return SmartValue(kind, text, "\n".join(lines), utc=utc, local=local)


def _iso(s: str) -> SmartValue | None:
    m = _ISO_RE.fullmatch(s)
    if m is None:
        return None
    y, mo, d, hh, mi, ss, frac, zone = m.groups()
    try:
        day = date(int(y), int(mo), int(d))
    except ValueError:
        return None
    if hh is None:
        lines = [t("smartvalues.date"), t("smartvalues.date_line", date=day.isoformat(), weekday=_weekday(day))]
        return SmartValue("date", s, "\n".join(lines), day=day)
    h, mn, sec = int(hh), int(mi), int(ss or 0)
    micro = int((frac or "0")[:6].ljust(6, "0"))
    if h == 24:
        if mn or sec or (frac and frac.strip("0")):
            return None
        h = 0
        extra = timedelta(days=1)
    else:
        extra = timedelta()
    if h > 23 or mn > 59 or sec > 59:
        return None
    tz = None
    if zone:
        if zone in "Zz":
            tz = timezone.utc
        else:
            sign = -1 if zone[0] == "-" else 1
            body = zone[1:].replace(":", "")
            oh, om = int(body[:2]), int(body[2:4] or 0)
            if oh > 23 or om > 59:
                return None
            tz = timezone(sign * timedelta(hours=oh, minutes=om))
    try:
        naive = datetime(day.year, day.month, day.day, h, mn, sec, micro) + extra
    except (ValueError, OverflowError):
        return None
    digits = min(6, len(frac)) if frac else 0
    if tz is None:
        lines = [t("smartvalues.datetime"),
                 t("smartvalues.naive", date=naive.date().isoformat(), time=_clock(naive, digits),
                   weekday=_weekday(naive.date()))]
        return SmartValue("datetime", s, "\n".join(lines), local=naive)
    aware = naive.replace(tzinfo=tz)
    try:
        utc = aware.astimezone(timezone.utc)
        local = tashkent_time(aware)
    except (OverflowError, ValueError):   # beyond year 1 .. 9999 after the conversion
        return None
    lines = [t("smartvalues.datetime"), _tashkent_line(local, digits),
             t("smartvalues.utc", date=utc.date().isoformat(), time=_clock(utc, digits))]
    if local.year < TASHKENT_SINCE:
        lines.append(t("smartvalues.old_offset", year=TASHKENT_SINCE))
    return SmartValue("datetime", s, "\n".join(lines), utc=utc, local=local)


def _clock(dt: datetime, digits: int) -> str:
    text = f"{dt.hour:02d}:{dt.minute:02d}:{dt.second:02d}"
    if digits:
        text += "." + f"{dt.microsecond:06d}"[:digits]
    return text


def _weekday(day: date) -> str:
    return t(f"smartvalues.weekday.{day.weekday()}")


def _tashkent_line(local: datetime, digits: int) -> str:
    return t("smartvalues.tashkent", date=local.date().isoformat(), time=_clock(local, digits),
             weekday=_weekday(local.date()))


# ----------------------------------------------------------------------------------
# links and colours
# ----------------------------------------------------------------------------------
def _url(s: str) -> SmartValue | None:
    if len(s) > URL_MAX_CHARS or _URL_RE.fullmatch(s) is None or _BAD_URL_CHARS.search(s):
        return None
    try:
        parts = urlsplit(s)
    except ValueError:
        return None
    if parts.scheme.lower() not in ("http", "https") or not parts.netloc or not parts.hostname:
        return None
    return SmartValue("url", s, t("smartvalues.url", url=s), url=s)


def _mailto(s: str) -> SmartValue | None:
    if len(s) > URL_MAX_CHARS or _BAD_URL_CHARS.search(s):
        return None
    m = _MAILTO_RE.fullmatch(s)
    if m is None:
        return None
    return SmartValue("email", s, t("smartvalues.email", address=m.group(1)), url=s)


def _color(s: str) -> SmartValue | None:
    m = _COLOR_RE.fullmatch(s)
    if m is None:
        return None
    hx = m.group(1).lower()
    if len(hx) == 3:
        hx = "".join(ch * 2 for ch in hx)
    r, g, b = int(hx[0:2], 16), int(hx[2:4], 16), int(hx[4:6], 16)
    a = int(hx[6:8], 16) if len(hx) == 8 else 255
    norm = "#" + hx
    if len(hx) == 8:
        tip = t("smartvalues.color_alpha", hex=norm, r=r, g=g, b=b, alpha=round(a * 100 / 255))
    else:
        tip = t("smartvalues.color", hex=norm, r=r, g=g, b=b)
    return SmartValue("color", s, tip, rgba=(r, g, b, a), hex=norm)


# ----------------------------------------------------------------------------------
# base64 images
# ----------------------------------------------------------------------------------
def _b64_payload(s: str) -> tuple[str, bool, str | None] | None:
    """(base64 text, data URL, declared mime) of a possible image string, else ``None``."""
    m = _DATA_URL_RE.match(s)
    if m is not None:
        return s[m.end():], True, m.group(1).lower().replace("jpg", "jpeg")
    if len(s) < B64_MIN_CHARS:
        return None
    head = s[:16]
    if not any(head.startswith(magic) for magic, _ in _B64_MAGIC):
        return None
    return s, False, None


def _decode_prefix(payload: str, max_bytes: int) -> bytes | None:
    """The first decoded bytes (about ``max_bytes``) of base64 text, ``None`` when invalid."""
    got = _decode_head(payload, max_bytes)
    return None if got is None else got[0]


def _decode_head(payload: str, max_bytes: int) -> tuple[bytes, bool] | None:
    """(the first decoded bytes -- about ``max_bytes`` --, they are all of it) of base64 text,
    ``None`` when invalid."""
    chars = (max_bytes + 2) // 3 * 4
    cut = chars + chars // 8 + 64                   # room for whitespace
    complete = len(payload) <= cut
    piece = payload[:cut]
    if _WS_RE.search(piece):
        piece = _WS_RE.sub("", piece)
    if len(piece) > chars:
        piece = piece[:chars]
        complete = False
    urlsafe = "-" in piece or "_" in piece
    if (_B64URL_CHARS if urlsafe else _B64_CHARS).fullmatch(piece) is None:
        return None
    if "=" in piece.rstrip("="):
        return None
    if complete:
        piece = piece + "=" * (-len(piece.rstrip("=")) % 4 if not piece.endswith("=") else 0)
    else:
        piece = piece[:len(piece) - len(piece) % 4]       # a prefix: whole quanta only
    try:
        if urlsafe:
            return base64.urlsafe_b64decode(piece), complete
        return base64.b64decode(piece, validate=True), complete
    except (binascii.Error, ValueError):
        return None


def _image_info(data: bytes, complete: bool = True) -> tuple[str, int | None, int | None] | None:
    """(format, width, height) from the header of image bytes; ``None`` unless the header holds
    the size (PNG ``IHDR``, GIF screen, JPEG ``SOF``, WebP ``VP8``/``VP8L``/``VP8X``): the signature
    alone, or a header cut short or corrupt, is no image (review R5A-12: they were "images of
    unknown size" whose thumbnail could not be decoded). The only size ``None``: a JPEG whose
    segments run on past ``data`` when ``data`` is a prefix of the image (``complete`` False) --
    metadata (EXIF, ICC) bigger than the prefix read."""
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        if len(data) >= 24 and data[12:16] == b"IHDR":
            w, h = int.from_bytes(data[16:20], "big"), int.from_bytes(data[20:24], "big")
            if w and h:
                return "PNG", w, h
        return None
    if data[:6] in (b"GIF87a", b"GIF89a"):
        if len(data) >= 13:            # the logical screen descriptor
            return "GIF", int.from_bytes(data[6:8], "little"), int.from_bytes(data[8:10], "little")
        return None
    if data.startswith(b"\xff\xd8\xff"):
        size = _jpeg_size(data, complete)
        if size is None:
            return None
        return "JPEG", size[0], size[1]
    if len(data) >= 12 and data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        w, h = _webp_size(data)
        if w is None or h is None:
            return None
        return "WEBP", w, h
    return None


_SOF = frozenset((0xC0, 0xC1, 0xC2, 0xC3, 0xC5, 0xC6, 0xC7, 0xC9, 0xCA, 0xCB, 0xCD, 0xCE, 0xCF))


def _jpeg_size(data: bytes, complete: bool) -> tuple[int | None, int | None] | None:
    """(width, height) from the frame header, ``(None, None)`` when the segments run on past a
    prefix (``complete`` False), ``None`` for a corrupt header: a segment shorter than its length
    field, a scan or the end of the image before a frame header, a byte that is no marker."""
    i = 2
    n = len(data)
    while True:
        if i + 4 > n:
            return None if complete else (None, None)
        if data[i] != 0xFF:
            return None
        marker = data[i + 1]
        if marker == 0xFF:            # fill byte
            i += 1
            continue
        if marker in (0xD8, 0x01) or 0xD0 <= marker <= 0xD7:
            i += 2
            continue
        seg = int.from_bytes(data[i + 2:i + 4], "big")
        if seg < 2 or marker in (0xDA, 0xD9):   # a scan / the end before any frame header
            return None
        if marker in _SOF:
            if seg < 8:
                return None
            if i + 9 <= n:
                w, h = int.from_bytes(data[i + 7:i + 9], "big"), int.from_bytes(data[i + 5:i + 7], "big")
                return (w, h) if w and h else None
            return None if complete else (None, None)
        if i + 2 + seg > n and complete:
            return None               # a segment longer than the image
        i += 2 + seg


def _webp_size(data: bytes) -> tuple[int | None, int | None]:
    chunk = data[12:16]
    if chunk == b"VP8X" and len(data) >= 30:
        return int.from_bytes(data[24:27], "little") + 1, int.from_bytes(data[27:30], "little") + 1
    if chunk == b"VP8L" and len(data) >= 25 and data[20] == 0x2F:
        bits = int.from_bytes(data[21:25], "little")
        return (bits & 0x3FFF) + 1, ((bits >> 14) & 0x3FFF) + 1
    if chunk == b"VP8 " and len(data) >= 30 and data[23:26] == b"\x9d\x01\x2a":
        return int.from_bytes(data[26:28], "little") & 0x3FFF, int.from_bytes(data[28:30], "little") & 0x3FFF
    return None, None


def _image(s: str) -> SmartValue | None:
    found = _b64_payload(s)
    if found is None:
        return None
    payload, data_url, mime = found
    got = _decode_head(payload, IMAGE_PREFIX_BYTES)
    if got is None or not got[0]:
        return None
    info = _image_info(got[0], got[1])
    if info is None:
        return None
    fmt, w, h = info
    if mime is not None and mime != _MIMES[fmt]:
        log.debug("smartvalues: data URL says %s, the bytes are %s", mime, fmt)
    image = ImageInfo(fmt, _MIMES[fmt], w, h, data_url)
    if w is not None and h is not None:
        tip = t("smartvalues.image", format=fmt, width=w, height=h)
    else:
        tip = t("smartvalues.image_unknown_size", format=fmt)
    return SmartValue("image", s, tip + "\n" + t("smartvalues.image_hint"), image=image)


def image_bytes(text: str, limit: int = IMAGE_MAX_BYTES) -> bytes | None:
    """The decoded bytes of a base64 image string (bare or ``data:`` URL) for a thumbnail;
    ``None`` when it is not one of the recognized images or decodes to more than ``limit``
    bytes."""
    found = _b64_payload(text)
    if found is None:
        return None
    payload = found[0]
    if len(payload) * 3 // 4 > limit + 3 * KiB:
        stripped = _WS_RE.sub("", payload[:(limit // 3 + 2) * 4 * 2])
        if len(stripped) * 3 // 4 > limit:
            return None
    data = _decode_prefix(payload, limit)
    if data is None or len(data) > limit or _image_info(data) is None:   # the whole header (JPEG: every segment)
        return None
    return data
