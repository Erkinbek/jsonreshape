r"""XLSX export (docs/ARCHITECTURE.md §8; stage 7): jsonreshape's own streaming writer of the
SpreadsheetML package -- ``fmt="xlsx"`` of :func:`jsonreshape.writer.export`. It replaced
``xlsxwriter`` (which wrote every cell in Python in the calling thread, ~12 MB/s): the rows are
turned into worksheet XML and deflated **in the worker processes**, and the calling thread only
appends the compressed pieces to the zip in order. The columns, cells, number rules, splits and
warnings are those of :mod:`jsonreshape.writer` ("CSV and XLSX"); the texts live in the ``writer``
section of :mod:`jsonreshape.i18n`.

The package
===========
* ``xl/worksheets/sheetN.xml`` (one per sheet), ``xl/workbook.xml``, ``xl/_rels/workbook.xml.rels``,
  ``xl/styles.xml`` (Calibri 11; style 1 = bold, the header row), ``docProps/app.xml`` /
  ``core.xml`` (creation time), ``_rels/.rels``, ``[Content_Types].xml``. No theme part, no
  shared-strings table, no ``<dimension>``: every string is an inline string
  (``<c t="inlineStr"><is><t>…</t></is></c>``, what ``xlsxwriter``'s ``constant_memory`` mode
  wrote), numbers ``<c><v>…</v></c>`` formatted ``%.16G`` (``1E+20``, ``-0``), booleans ``t="b"``.
  Every sheet has a bold, frozen header row (``<pane ySplit="1" … state="frozen"/>``).
* Rows and cells carry **no** ``r`` (position) attributes -- the position of a row is the one after
  the previous row, of a cell the next column (ECMA-376 makes both optional; Excel, LibreOffice and
  openpyxl place them so); blank cells inside a row are ``<c/>``, trailing blanks are omitted, a row
  without cells is ``<row></row>``. This is what lets a worker build the XML of its block without
  knowing how many rows come before it. (Checked with Excel itself: it opens such workbooks -- many
  sheets, ``files`` parts, zip64 records -- and reads every cell as written.)
* Strings exactly as ``xlsxwriter`` wrote them: lone surrogates (from ``\ud800`` escapes) as the
  text ``\ud800``; texts cut to 32 767 UTF-16 units (``writer.warn.xlsx_truncated``); control
  characters as ``_xHHHH_`` and a literal ``_xHHHH_`` as ``_x005F_xHHHH_``; ``U+FFFE`` /
  ``U+FFFF`` escaped; ``&``, ``<``, ``>`` as entities; ``xml:space="preserve"`` on a text with
  leading or trailing whitespace -- with one deliberate difference: a text that starts with ``<r>``
  and ends with ``</r>`` is a text (xlsxwriter wrote it as raw rich-text XML, a workbook Excel
  refuses to open). Numbers follow the writer's 15-digit rule (``_XlsxCells``: others are text,
  ``writer.warn.xlsx_number_text``), written ``%.16G`` as xlsxwriter did, so every cell has the
  value and type of the stage 1-6 workbook (``tests/test_writer_xlsx_fast.py`` compares them).

The zip
=======
Written by :class:`_ZipWriter` directly (no :mod:`zipfile`): every sheet is one deflated member
built from **pieces** -- each piece is a raw deflate stream of its rows ended with a sync flush
(``Z_SYNC_FLUSH``: complete blocks on a byte boundary), so the pieces of a sheet, concatenated
and closed with one final empty block, are one valid deflate stream (the way ``pigz`` works). A
worker returns ``(deflated bytes, CRC-32, raw length, rows, truncated texts)`` per job; the
calling thread combines the CRCs (:func:`crc32_combine`, zlib's algorithm with cached
matrices) and patches CRC and sizes into the member's local header when the sheet ends (the
file is seekable; no data descriptors). The small parts are deflated here. Level
:data:`DEFLATE_LEVEL` = 6 throughout (xlsxwriter's). The central directory is zip64 when an
offset or the entry count needs it (archives over 4 GiB; xlsxwriter refused any archive that
big); a single sheet member of 4 GiB or more (compressed or uncompressed, :data:`MEMBER_LIMIT`) is
``writer.error.xlsx_too_large`` as soon as it is reached (the hint: split into files or lower the
rows per sheet -- both make the sheets smaller).

Splits, parts, workers
======================
* ``xlsx_max_rows`` (header included) per sheet: the pieces are appended while they fit; a piece
  that straddles the limit is inflated, cut at the row boundary and the two parts deflated again
  (once per sheet boundary). ``xlsx_split="sheets"``: new sheets ``Sheet1``, ``Sheet2``, ... in one
  workbook; ``"files"``: new workbooks staged next to the target and committed all or nothing
  (``writer._commit_parts``; ``<stem>_1.xlsx``, ... when more than one).
* Rows in worker processes exactly as CSV (:class:`jsonreshape.writer._RowsPool`, the CSV rule
  for the number of workers, ``ExportReport.workers``): a job (:class:`writer._RowsJob` with
  ``fmt="xlsx"``) builds the typed cells, the XML (:class:`_RowXml`) and the deflated piece.
  ``workers=1`` runs the same jobs in the calling thread over the same blocks, so the workbook is
  **byte for byte the same** (but ``docProps/core.xml``, the creation time) whatever the number of
  workers: the piece boundaries depend only on the records. Records a job leaves to the calling
  thread (huge, too deep, many brackets) are encoded here in their place.
* Memory: a piece (≤ ``POOL_JOB_CELLS`` cells of XML, deflated: ~0.1 MB per MiB of records) per
  job in flight; nothing is staged on disk (the old ``xlsx-<pid>-<hex>`` staging folders are no
  longer created; stale ones of earlier versions are still purged).
* Progress by bytes read as CSV; ``cancel`` is checked for every block, every job result, every
  write and before the commit. An error or a cancellation leaves no temporary file and an
  existing target untouched (``atomic_path`` / the staged parts).
"""

from __future__ import annotations

import os
import re
import struct
import tempfile
import time
import zlib
from pathlib import Path
from typing import Any

from .errors import ExportError, UserInputError
from .fileio import atomic_path, long_path, os_error
from .tasks import CancelToken, check
from .writer import (
    WRITE_BUFFER,
    XLSX_MAX_COLUMNS,
    XLSX_MAX_STRING,
    _SURROGATE_RE,
    _XLSX_SAFE_CHARS,
    _builder,
    _check_target,
    _commit_parts,
    _Ctx,
    _escape_surrogates,
    _pool_workers,
    _remove_quiet,
    _RowBuilder,
    _RowsPool,
    _warn_unfound,
    _XlsxCells,
)

__all__ = ["DEFLATE_LEVEL", "crc32_combine", "export_xlsx"]

DEFLATE_LEVEL = 6                     # zlib level of every member (xlsxwriter's default)
ZIP64_LIMIT = 0xFFFFFFFF              # offsets (and the central directory) from this on: zip64 structures
MEMBER_LIMIT = 0xFFFFFFFF             # a member this big (compressed or not) would need zip64 sizes: refused
ZIP_ENTRIES_LIMIT = 0xFFFF            # entries from this on need the zip64 end record
_DOS_TIME = 0                         # 1980-01-01 00:00:00 (every member)
_DOS_DATE = (1 << 5) | 1
_EXTERNAL_ATTR = 0o100666 << 16       # a regular file, rw-rw-rw- (as xlsxwriter's zip)

# -- XML escaping of inline strings (xlsxwriter's rules, see the module docstring) ----------------
# any string that needs the slow path: XML specials, control characters, non-characters, lone
# surrogates, or a literal _xHHHH_ (escaped as _x005F_xHHHH_)
_SPECIAL_RE = re.compile("[&<>\x00-\x08\x0b-\x1f￾￿" + chr(0xD800) + "-" + chr(0xDFFF) + "]"
                         "|_x[0-9a-fA-F]{4}_")
_XESCAPE_RE = re.compile("(_x[0-9a-fA-F]{4}_)")
_CONTROL_RE = re.compile("([\x00-\x08\x0b-\x1f])")
_XML_NS = ('xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" '
           'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships"')
_XML_HEAD = '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n'
_SHEET_HEAD = (_XML_HEAD + "<worksheet " + _XML_NS + '><sheetViews><sheetView{selected} workbookViewId="0">'
               '<pane ySplit="1" topLeftCell="A2" activePane="bottomLeft" state="frozen"/>'
               '<selection pane="bottomLeft"/></sheetView></sheetViews><sheetFormatPr defaultRowHeight="15"/>'
               "<sheetData>")
_SHEET_TAIL = ('</sheetData><pageMargins left="0.7" right="0.7" top="0.75" bottom="0.75" header="0.3" footer="0.3"/>'
               "</worksheet>")
_STYLES = (_XML_HEAD + '<styleSheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
           '<fonts count="2"><font><sz val="11"/><name val="Calibri"/><family val="2"/></font>'
           '<font><b/><sz val="11"/><name val="Calibri"/><family val="2"/></font></fonts>'
           '<fills count="2"><fill><patternFill patternType="none"/></fill>'
           '<fill><patternFill patternType="gray125"/></fill></fills>'
           '<borders count="1"><border><left/><right/><top/><bottom/><diagonal/></border></borders>'
           '<cellStyleXfs count="1"><xf numFmtId="0" fontId="0" fillId="0" borderId="0"/></cellStyleXfs>'
           '<cellXfs count="2"><xf numFmtId="0" fontId="0" fillId="0" borderId="0" xfId="0"/>'
           '<xf numFmtId="0" fontId="1" fillId="0" borderId="0" xfId="0" applyFont="1"/></cellXfs>'
           '<cellStyles count="1"><cellStyle name="Normal" xfId="0" builtinId="0"/></cellStyles>'
           '<dxfs count="0"/><tableStyles count="0" defaultTableStyle="TableStyleMedium9" '
           'defaultPivotStyle="PivotStyleLight16"/></styleSheet>')
_RELS = (_XML_HEAD + '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
         '<Relationship Id="rId1" '
         'Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" '
         'Target="xl/workbook.xml"/><Relationship Id="rId2" '
         'Type="http://schemas.openxmlformats.org/package/2006/relationships/metadata/core-properties" '
         'Target="docProps/core.xml"/><Relationship Id="rId3" '
         'Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/extended-properties" '
         'Target="docProps/app.xml"/></Relationships>')
_CT_HEAD = (_XML_HEAD + '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
            '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
            '<Default Extension="xml" ContentType="application/xml"/>'
            '<Override PartName="/docProps/app.xml" '
            'ContentType="application/vnd.openxmlformats-officedocument.extended-properties+xml"/>'
            '<Override PartName="/docProps/core.xml" '
            'ContentType="application/vnd.openxmlformats-package.core-properties+xml"/>'
            '<Override PartName="/xl/styles.xml" '
            'ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.styles+xml"/>'
            '<Override PartName="/xl/workbook.xml" '
            'ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/>')
_CT_SHEET = ('<Override PartName="/xl/worksheets/sheet{n}.xml" '
             'ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>')
_WB_HEAD = (_XML_HEAD + "<workbook " + _XML_NS + "><workbookPr/><bookViews>"
            '<workbookView xWindow="240" yWindow="15" windowWidth="16095" windowHeight="9660"/></bookViews><sheets>')
_WB_SHEET = '<sheet name="{name}" sheetId="{n}" r:id="rId{n}"/>'
_WB_TAIL = '</sheets><calcPr calcId="124519" fullCalcOnLoad="1"/></workbook>'
_WB_RELS_HEAD = (_XML_HEAD + '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">')
_WB_RELS_SHEET = ('<Relationship Id="rId{n}" '
                  'Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" '
                  'Target="worksheets/sheet{n}.xml"/>')
_WB_RELS_TAIL = ('<Relationship Id="rId{n}" '
                 'Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/styles" '
                 'Target="styles.xml"/></Relationships>')
_CORE = (_XML_HEAD + '<cp:coreProperties xmlns:cp="http://schemas.openxmlformats.org/package/2006/metadata/'
         'core-properties" xmlns:dc="http://purl.org/dc/elements/1.1/" xmlns:dcterms="http://purl.org/dc/terms/" '
         'xmlns:dcmitype="http://purl.org/dc/dcmitype/" xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance">'
         "<dc:creator></dc:creator><cp:lastModifiedBy></cp:lastModifiedBy>"
         '<dcterms:created xsi:type="dcterms:W3CDTF">{when}</dcterms:created>'
         '<dcterms:modified xsi:type="dcterms:W3CDTF">{when}</dcterms:modified></cp:coreProperties>')
_APP = (_XML_HEAD + '<Properties xmlns="http://schemas.openxmlformats.org/officeDocument/2006/extended-properties" '
        'xmlns:vt="http://schemas.openxmlformats.org/officeDocument/2006/docPropsVTypes">'
        "<Application>Microsoft Excel</Application><DocSecurity>0</DocSecurity><ScaleCrop>false</ScaleCrop>"
        '<HeadingPairs><vt:vector size="2" baseType="variant"><vt:variant><vt:lpstr>Worksheets</vt:lpstr>'
        "</vt:variant><vt:variant><vt:i4>{count}</vt:i4></vt:variant></vt:vector></HeadingPairs>"
        '<TitlesOfParts><vt:vector size="{count}" baseType="lpstr">{titles}</vt:vector></TitlesOfParts>'
        "<Company></Company><LinksUpToDate>false</LinksUpToDate><SharedDoc>false</SharedDoc>"
        "<HyperlinksChanged>false</HyperlinksChanged><AppVersion>12.0000</AppVersion></Properties>")


# ----------------------------------------------------------------------------------
# strings and rows -> worksheet XML
# ----------------------------------------------------------------------------------
def _escape_text(s: str) -> tuple[str, bool]:
    """``(text as xlsxwriter wrote it inside <t>, truncated)``: lone surrogates escaped, cut to
    :data:`XLSX_MAX_STRING` UTF-16 units, ``_xHHHH_`` escapes, XML entities."""
    truncated = False
    if not s.isascii() and _SURROGATE_RE.search(s):
        s = _escape_surrogates(s)
    if len(s) > _XLSX_SAFE_CHARS:
        data = s.encode("utf-16-le", "surrogatepass")
        if len(data) > 2 * XLSX_MAX_STRING:
            data = data[:2 * XLSX_MAX_STRING]
            if 0xD8 <= data[-1] <= 0xDB:      # do not split a surrogate pair
                data = data[:-2]
            s = data.decode("utf-16-le", "surrogatepass")
            truncated = True
    s = _XESCAPE_RE.sub(r"_x005F\1", s)
    s = _CONTROL_RE.sub(lambda m: "_x%04X_" % ord(m.group(1)), s)
    if "￾" in s or "￿" in s:
        s = s.replace("￾", "_xFFFE_").replace("￿", "_xFFFF_")
    # (whether the text needs xml:space="preserve" is decided by the caller on this text: the
    # entities below do not change its ends)
    if "&" in s:
        s = s.replace("&", "&amp;")
    if "<" in s:
        s = s.replace("<", "&lt;")
    if ">" in s:
        s = s.replace(">", "&gt;")
    return s, truncated


class _RowXml:
    """Rows of typed cells (:class:`writer._XlsxCells`: ``None``/``""`` blank, ``str``, ``bool``,
    ``int``/``float``) -> the ``<row>…</row>`` XML of a sheet (no position attributes, see the module
    docstring). :attr:`truncated` counts the texts cut to Excel's limit."""

    __slots__ = ("truncated",)

    def __init__(self) -> None:
        self.truncated = 0

    def rows(self, rows: list[list[Any]], style: str = "") -> bytes:
        """The XML of ``rows`` (``style``: the ``s`` attribute of every cell, ``' s="1"'`` for the header)."""
        special = _SPECIAL_RE.search
        safe = _XLSX_SAFE_CHARS
        blank = "<c/>"
        str_open = "<c" + style + ' t="inlineStr"><is><t>'
        str_open_ws = "<c" + style + ' t="inlineStr"><is><t xml:space="preserve">'
        str_close = "</t></is></c>"
        num_open = "<c" + style + "><v>"
        bool_true = "<c" + style + ' t="b"><v>1</v></c>'
        bool_false = "<c" + style + ' t="b"><v>0</v></c>'
        parts: list[str] = []
        append = parts.append
        truncated = 0
        for row in rows:
            append("<row>")
            blanks = 0
            for v in row:
                if v is None:
                    blanks += 1
                    continue
                tv = type(v)
                if tv is str:
                    if not v:
                        blanks += 1
                        continue
                    if blanks:
                        append(blank * blanks)
                        blanks = 0
                    if len(v) > safe or special(v) is not None:
                        v, cut = _escape_text(v)
                        if cut:
                            truncated += 1
                    if v[0].isspace() or v[-1].isspace():
                        append(str_open_ws)
                    else:
                        append(str_open)
                    append(v)
                    append(str_close)
                elif tv is bool:
                    if blanks:
                        append(blank * blanks)
                        blanks = 0
                    append(bool_true if v else bool_false)
                else:
                    if blanks:
                        append(blank * blanks)
                        blanks = 0
                    append(num_open)
                    append(format(v, ".16G"))
                    append("</v></c>")
            append("</row>")
        self.truncated += truncated
        return "".join(parts).encode("utf-8")


def _piece(xml: bytes, rows: int, truncated: int = 0) -> tuple[bytes, int, int, int, int]:
    """``(deflated, crc32, raw length, rows, truncated)``: the XML as one piece of a sheet's deflate stream
    (complete blocks, ended with a sync flush)."""
    z = zlib.compressobj(DEFLATE_LEVEL, zlib.DEFLATED, -15)
    return (z.compress(xml) + z.flush(zlib.Z_SYNC_FLUSH), zlib.crc32(xml), len(xml), rows, truncated)


def _inflate(comp: bytes) -> bytes:
    return zlib.decompressobj(-15).decompress(comp)


def _row_offset(xml: bytes, k: int) -> int:
    """The offset of row ``k`` (0-based) in the XML of rows."""
    pos = -1
    for _ in range(k + 1):
        pos = xml.find(b"<row>", pos + 1)
        if pos < 0:  # pragma: no cover - a piece always holds its rows
            raise ValueError("row not found")
    return pos


# ----------------------------------------------------------------------------------
# CRC-32 of concatenated pieces (zlib's crc32_combine with the shift matrices cached)
# ----------------------------------------------------------------------------------
_CRC_POLY = 0xEDB88320
_crc_shift: list[list[int]] = []      # _crc_shift[k]: the operator of 2**k zero bytes


def _gf2_times(mat: list[int], vec: int) -> int:
    s = 0
    i = 0
    while vec:
        if vec & 1:
            s ^= mat[i]
        vec >>= 1
        i += 1
    return s


def _gf2_square(mat: list[int]) -> list[int]:
    return [_gf2_times(mat, mat[n]) for n in range(32)]


def _crc_operator(k: int) -> list[int]:
    """The matrix that advances a CRC-32 over ``2**k`` zero bytes (cached)."""
    if not _crc_shift:
        odd = [_CRC_POLY] + [1 << (n - 1) for n in range(1, 32)]   # one zero bit
        for _ in range(3):                                          # eight bits: one byte
            odd = _gf2_square(odd)
        _crc_shift.append(odd)
    while len(_crc_shift) <= k:
        _crc_shift.append(_gf2_square(_crc_shift[-1]))
    return _crc_shift[k]


def crc32_combine(crc1: int, crc2: int, len2: int) -> int:
    """The CRC-32 of ``a + b`` from ``crc32(a)``, ``crc32(b)`` and ``len(b)``."""
    if len2 <= 0:
        return crc1
    k = 0
    while len2:
        if len2 & 1:
            crc1 = _gf2_times(_crc_operator(k), crc1)
        len2 >>= 1
        k += 1
    return crc1 ^ crc2


# ----------------------------------------------------------------------------------
# the zip container
# ----------------------------------------------------------------------------------
class _XlsxSink:
    """The file an XLSX workbook is written to: ``cancel`` is checked for every write and the bytes
    written are counted."""

    def __init__(self, f: Any, cancel: CancelToken | None) -> None:
        self._f = f
        self._cancel = cancel
        self.written = 0

    def write(self, data: bytes) -> int:
        check(self._cancel)
        self._f.write(data)
        self.written += len(data)
        return len(data)

    def tell(self) -> int:
        return self._f.tell()

    def seek(self, pos: int, whence: int = 0) -> int:
        return self._f.seek(pos, whence)

    def flush(self) -> None:
        self._f.flush()


class _ZipWriter:
    """A zip archive written in one pass over a seekable file: :meth:`add` small members (deflated
    here); :meth:`begin` / :meth:`piece` / :meth:`end` a streamed member assembled from deflated
    pieces (module docstring, "The zip"); :meth:`close` the central directory (zip64 when needed)."""

    def __init__(self, sink: _XlsxSink, out: Path) -> None:
        self.sink = sink
        self.out = out
        self.entries: list[tuple[bytes, int, int, int, int]] = []   # name, crc, csize, usize, offset
        self.cur: list[Any] | None = None

    def _local_header(self, name: bytes, crc: int, csize: int, usize: int) -> bytes:
        return struct.pack("<4sHHHHHIIIHH", b"PK\x03\x04", 20, 0, 8, _DOS_TIME, _DOS_DATE, crc, csize, usize,
                           len(name), 0) + name

    def add(self, name: str, data: bytes) -> None:
        nameb = name.encode("utf-8")
        z = zlib.compressobj(DEFLATE_LEVEL, zlib.DEFLATED, -15)
        comp = z.compress(data) + z.flush()
        crc = zlib.crc32(data)
        offset = self.sink.tell()
        self.sink.write(self._local_header(nameb, crc, len(comp), len(data)) + comp)
        self.entries.append((nameb, crc, len(comp), len(data), offset))

    def begin(self, name: str) -> None:
        assert self.cur is None
        nameb = name.encode("utf-8")
        offset = self.sink.tell()
        self.sink.write(self._local_header(nameb, 0, 0, 0))
        self.cur = [nameb, offset, 0, 0, 0]          # name, offset, crc, csize, usize

    def piece(self, comp: bytes, crc: int, raw: int) -> None:
        cur = self.cur
        assert cur is not None
        if cur[3] + len(comp) + 2 >= MEMBER_LIMIT or cur[4] + raw >= MEMBER_LIMIT:
            raise ExportError("writer.error.xlsx_too_large", path=str(self.out))   # (as soon as it is known)
        self.sink.write(comp)
        cur[2] = crc32_combine(cur[2], crc, raw)
        cur[3] += len(comp)
        cur[4] += raw

    def end(self) -> None:
        cur, self.cur = self.cur, None
        assert cur is not None
        self.sink.write(b"\x03\x00")                 # the final (empty) deflate block
        nameb, offset, crc, csize, usize = cur
        csize += 2
        pos = self.sink.tell()
        self.sink.seek(offset + 14)
        self.sink.write(struct.pack("<III", crc, csize, usize))
        self.sink.seek(pos)
        self.entries.append((nameb, crc, csize, usize, offset))

    def close(self) -> None:
        assert self.cur is None
        sink = self.sink
        cd_offset = sink.tell()
        zip64 = False
        for nameb, crc, csize, usize, offset in self.entries:
            extra = b""
            version = 20
            off = offset
            if offset >= ZIP64_LIMIT:
                extra = struct.pack("<HHQ", 0x0001, 8, offset)
                off = 0xFFFFFFFF
                version = 45
                zip64 = True
            sink.write(struct.pack("<4sHHHHHHIIIHHHHHII", b"PK\x01\x02", version, version, 0, 8, _DOS_TIME, _DOS_DATE,
                                   crc, csize, usize, len(nameb), len(extra), 0, 0, 0, _EXTERNAL_ATTR, off)
                       + nameb + extra)
        cd_size = sink.tell() - cd_offset
        n = len(self.entries)
        if zip64 or n >= ZIP_ENTRIES_LIMIT or cd_size >= ZIP64_LIMIT or cd_offset >= ZIP64_LIMIT:
            rec_offset = sink.tell()
            sink.write(struct.pack("<4sQHHIIQQQQ", b"PK\x06\x06", 44, 45, 45, 0, 0, n, n, cd_size, cd_offset))
            sink.write(struct.pack("<4sIQI", b"PK\x06\x07", 0, rec_offset, 1))
        entries = 0xFFFF if n >= ZIP_ENTRIES_LIMIT else n                # (the values of the zip64 record)
        sink.write(struct.pack("<4sHHHHIIH", b"PK\x05\x06", 0, 0, entries, entries,
                               0xFFFFFFFF if cd_size >= ZIP64_LIMIT else cd_size,
                               0xFFFFFFFF if cd_offset >= ZIP64_LIMIT else cd_offset, 0))


# ----------------------------------------------------------------------------------
# the workbook(s)
# ----------------------------------------------------------------------------------
class _XlsxBook:
    """Pieces and rows -> one or more workbooks: new sheets (``sheets``) or new files (``files``,
    staged in the target directory) when ``xlsx_max_rows`` is reached (module docstring)."""

    def __init__(self, ctx: _Ctx, header: list[str], first_path: Path | None) -> None:
        self.ctx = ctx
        self.header = header
        self.max_rows = ctx.opts.xlsx_max_rows
        self.files_mode = first_path is None
        self.first_path = first_path
        self.xml = _RowXml()
        self.staged: list[Path] = []
        self.fh: Any = None
        self.sink: _XlsxSink | None = None
        self.zip: _ZipWriter | None = None
        self.row = 0                 # rows in the current sheet (header included)
        self.sheets = 0              # sheets written in all (files mode: one per file)
        self.book_sheets = 0         # ... in the current workbook
        self.truncated = 0
        # the header row is the same on every sheet: its piece once (and its truncated texts per sheet)
        before = self.xml.truncated
        xml = self.xml.rows([header], ' s="1"')
        self.header_truncated = self.xml.truncated - before
        self.xml.truncated = before
        self.header_piece = _piece(xml, 1)

    # -- workbooks and sheets -----------------------------------------------------------
    def _stage(self) -> Path:
        out = self.ctx.out
        fd, name = tempfile.mkstemp(prefix=f".{out.name[:48]}.", suffix=".part", dir=long_path(out.parent))
        os.close(fd)
        path = Path(name)
        self.staged.append(path)
        return path

    def open(self) -> None:
        path = self._stage() if self.files_mode else self.first_path
        assert path is not None
        try:
            self.fh = open(long_path(path), "wb", buffering=WRITE_BUFFER)  # noqa: SIM115
        except OSError as exc:
            raise os_error(exc, self.ctx.out) from exc
        self.sink = _XlsxSink(self.fh, self.ctx.cancel)
        self.zip = _ZipWriter(self.sink, self.ctx.out)
        self.book_sheets = 0
        self._new_sheet()

    def _new_sheet(self) -> None:
        assert self.zip is not None
        self.book_sheets += 1
        self.sheets += 1
        n = self.book_sheets
        self.zip.begin(f"xl/worksheets/sheet{n}.xml")
        head = _SHEET_HEAD.format(selected=' tabSelected="1"' if n == 1 else "").encode("utf-8")
        self._put(_piece(head, 0))
        self._put(self.header_piece)
        self.truncated += self.header_truncated
        self.row = 1

    def _end_sheet(self) -> None:
        assert self.zip is not None
        self._put(_piece(_SHEET_TAIL.encode("utf-8"), 0))
        self.zip.end()

    def _next_sheet(self) -> None:
        """The current sheet is full: the next one (``files``: in a new workbook)."""
        if self.files_mode:
            self.close_workbook()
            self.open()
        else:
            self._end_sheet()
            self._new_sheet()

    def _put(self, piece: tuple) -> None:
        assert self.zip is not None
        self.zip.piece(piece[0], piece[1], piece[2])

    # -- rows ----------------------------------------------------------------------------
    def write_piece(self, piece: tuple) -> None:
        """A piece of rows (a job's result): appended to the current sheet, cut at the sheet limit."""
        comp, crc, raw, n, truncated = piece
        self.truncated += truncated
        while n:
            if self.row >= self.max_rows:
                self._next_sheet()
            room = self.max_rows - self.row
            if n <= room:
                self._put((comp, crc, raw))
                self.row += n
                return
            xml = _inflate(comp)
            cut = _row_offset(xml, room)
            self._put(_piece(xml[:cut], room))
            self.row += room
            comp, crc, raw, _rows, _t = _piece(xml[cut:], n - room)
            n -= room

    def write_rows(self, rows: list[list[Any]]) -> None:
        """Rows made in the calling thread (records a job left to it)."""
        if not rows:
            return
        before = self.xml.truncated
        xml = self.xml.rows(rows)
        self.write_piece(_piece(xml, len(rows), self.xml.truncated - before))

    # -- finishing -------------------------------------------------------------------------
    def close_workbook(self, final: bool = False) -> None:
        """End the current sheet, write the package parts and the central directory, close the file."""
        zip_ = self.zip
        if zip_ is None:
            return
        ctx = self.ctx
        try:
            check(ctx.cancel)
            self._end_sheet()
            self.zip = None
            n = self.book_sheets
            names = [f"Sheet{k}" for k in range(1, n + 1)]
            zip_.add("xl/workbook.xml", (_WB_HEAD + "".join(_WB_SHEET.format(name=nm, n=k + 1)
                                                            for k, nm in enumerate(names)) + _WB_TAIL).encode())
            zip_.add("xl/_rels/workbook.xml.rels",
                     (_WB_RELS_HEAD + "".join(_WB_RELS_SHEET.format(n=k) for k in range(1, n + 1))
                      + _WB_RELS_TAIL.format(n=n + 1)).encode())
            zip_.add("xl/styles.xml", _STYLES.encode())
            zip_.add("docProps/app.xml", _APP.format(count=n, titles="".join(f"<vt:lpstr>{nm}</vt:lpstr>"
                                                                            for nm in names)).encode())
            zip_.add("docProps/core.xml", _CORE.format(when=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())).encode())
            zip_.add("_rels/.rels", _RELS.encode())
            zip_.add("[Content_Types].xml",
                     (_CT_HEAD + "".join(_CT_SHEET.format(n=k) for k in range(1, n + 1)) + "</Types>").encode())
            zip_.close()
        except OSError as exc:
            self._close_file()
            raise os_error(exc, ctx.out) from exc
        except BaseException:
            self._close_file()
            raise
        self._finish_file()

    def _finish_file(self) -> None:
        """Flush and close the file of a completed workbook (errors are reported)."""
        sink, fh = self.sink, self.fh
        self.sink = self.fh = None
        if fh is None:  # pragma: no cover
            return
        try:
            fh.flush()
            os.fsync(fh.fileno())
            fh.close()
        except OSError as exc:
            try:
                fh.close()
            except OSError:
                pass
            raise os_error(exc, self.ctx.out) from exc

    def _close_file(self) -> None:
        """Close the file of an unfinished workbook (quietly)."""
        fh = self.fh
        self.sink = self.fh = None
        self.zip = None
        if fh is not None:
            try:
                fh.close()
            except OSError:
                pass

    def commit_files(self) -> list[Path]:
        """``files`` mode: move the staged parts to their final names (all or nothing)."""
        ctx = self.ctx
        out = ctx.out
        n = len(self.staged)
        if n == 1:
            names = [out]
        else:
            names = [out.with_name(f"{out.stem}_{k}{out.suffix}") for k in range(1, n + 1)]
        for final in names:
            _check_target(ctx.doc, final)
        stale = []
        k = n + 1 if n > 1 else 1
        while True:
            p = out.with_name(f"{out.stem}_{k}{out.suffix}")
            if not os.path.lexists(long_path(p)):
                break
            stale.append(p)
            k += 1
        ctx.commit_check(names)          # (the part names: a part with unsaved edits is never replaced)
        _commit_parts(self.staged, names)
        self.staged = []
        if stale:
            ctx.warn("writer.warn.xlsx_stale_parts", add=len(stale),
                     names=", ".join(p.name for p in stale[:5]) + (", …" if len(stale) > 5 else ""))
        return names

    def abort(self) -> None:
        self._close_file()
        for p in self.staged:
            _remove_quiet(p)
        self.staged = []


class _XlsxOut:
    """Where :class:`writer._RowsPool` puts the rows of an XLSX export: a job's deflated pieces straight
    into the workbook, rows made in the calling thread encoded here -- in their order."""

    def __init__(self, book: _XlsxBook) -> None:
        self.book = book

    def piece(self, piece: tuple) -> None:
        self.book.write_piece(piece)

    def rows(self, rows: list[list[Any]]) -> None:
        self.book.write_rows(rows)

    def flush(self) -> None:
        pass


# ----------------------------------------------------------------------------------
# the export
# ----------------------------------------------------------------------------------
def _xlsx_rows(ctx: _Ctx, builder: _RowBuilder, book: _XlsxBook) -> None:
    """The rows of an XLSX export into ``book``: jobs in worker processes (the CSV rule), or the same jobs
    in this thread (``workers=1``) -- the same pieces either way."""
    _RowsPool(ctx, builder, _XlsxOut(book), _pool_workers(ctx)).run()


def export_xlsx(ctx: _Ctx, out: Path) -> list[Path]:
    """Write the rows of ``ctx`` (columns prepared by the writer) as the workbook(s) ``out`` (module docstring)."""
    opts = ctx.opts
    if len(ctx.columns) > XLSX_MAX_COLUMNS:
        raise UserInputError("writer.error.too_many_columns", max=XLSX_MAX_COLUMNS, count=len(ctx.columns))
    cells = _XlsxCells(opts.join_sep)
    builder = _builder(ctx, cells)
    header = [c.text for c in ctx.columns]
    files: list[Path]
    if opts.xlsx_split == "sheets":
        with atomic_path(out) as tmp:
            book = _XlsxBook(ctx, header, tmp)
            try:
                book.open()
                _xlsx_rows(ctx, builder, book)
                book.close_workbook(final=True)
                ctx.commit_check()
            except BaseException:
                book.abort()
                raise
        files = [out]
        if book.sheets > 1:
            ctx.warn("writer.warn.xlsx_sheets", add=book.sheets)
    else:
        book = _XlsxBook(ctx, header, None)
        try:
            book.open()
            _xlsx_rows(ctx, builder, book)
            book.close_workbook(final=True)
            parts = len(book.staged)
            files = book.commit_files()
        except BaseException:
            book.abort()
            raise
        if parts > 1:
            ctx.warn("writer.warn.xlsx_files", add=parts)
    if cells.numbers_as_text:
        ctx.warn("writer.warn.xlsx_number_text", add=cells.numbers_as_text)
    if book.truncated:
        ctx.warn("writer.warn.xlsx_truncated", add=book.truncated, max=XLSX_MAX_STRING)
    _warn_unfound(ctx, builder)
    return files
