"""Helpers of the saver tests (docs/ARCHITECTURE.md §12.7): source documents of every kind, style and
encoding, edit patterns, and an independent oracle of the saved bytes.

* :func:`make_doc` writes a source document (the 7 kinds + ``wrapped_obj`` of
  ``tests/overlay_helpers.py``) in a style (``compact``, ``pretty2``, ``pretty4``, ``tab``, ``messy``,
  ``crlf`` = pretty2 with CRLF line ends) and an encoding (``utf-8``, ``utf-8-bom``, ``utf-16-le`` /
  ``utf-16-be`` with or without BOM, ``gzip``, ``gzip-bom``, ...), opens and indexes it.
* :func:`apply_pattern` performs one of :data:`PATTERNS` with an
  :class:`~overlay_helpers.OverlayBuilder` (the helpers' scenarios plus inserts at the start / middle /
  end, deletes, deleting every record, moves).
* :func:`oracle_text` restates the byte rules of §12.7 record by record (no runs, no batching): the
  UTF-8 text a save of the edited document must produce. :func:`decode_saved` undoes gzip, the BOM and
  UTF-16 of a saved file.
* :class:`FakeStore` records save journals (``journal(data)``) like ``EditStore.journal``, also into a
  JSON file a crashed subprocess leaves behind.
* :func:`edited` (a source with a pattern's edits set as its overlay), :func:`check_saved` (the saved file
  holds exactly the oracle's text and reopens to the expected records), :func:`temp_leftovers`.
"""

from __future__ import annotations

import codecs
import gzip
import json
import random
from pathlib import Path
from typing import Any

import os

from jsonreshape import codec, editops, saver
from jsonreshape.reader import Document
from jsonreshape.settings import Settings
from overlay_helpers import SCENARIOS, OverlayBuilder, apply_scenario, new_record, write_source

WS = b" \t\r\n"
STYLES = ("compact", "pretty2", "pretty4", "tab", "messy", "crlf")
ENCODINGS = ("utf-8", "utf-8-bom", "utf-16-le", "utf-16-le-bom", "utf-16-be", "utf-16-be-bom", "gzip",
             "gzip-bom", "gzip-utf-16-le-bom")
PATTERNS = SCENARIOS + ("insert_start", "insert_middle", "insert_end", "delete", "delete_all", "move")

__all__ = [
    "ENCODINGS",
    "PATTERNS",
    "STYLES",
    "FakeStore",
    "apply_pattern",
    "check_saved",
    "decode_saved",
    "edited",
    "encode_source",
    "jsonl_filter",
    "make_doc",
    "oracle_text",
    "reopen",
    "temp_leftovers",
]


def encode_source(data: bytes, encoding: str) -> bytes:
    """UTF-8 ``data`` as stored with ``encoding`` (one of :data:`ENCODINGS`)."""
    gz = encoding.startswith("gzip")
    enc = encoding[5:] if gz else encoding
    enc = enc or "utf-8"
    bom = enc.endswith("-bom") or enc == "bom"
    if enc == "bom":
        enc = "utf-8"
    elif bom:
        enc = enc[:-4]
    raw = data
    if enc in ("utf-16-le", "utf-16-be"):
        raw = data.decode("utf-8").encode(enc)
        if bom:
            raw = "\ufeff".encode(enc) + raw
    elif bom:
        raw = codecs.BOM_UTF8 + data
    if gz:
        raw = gzip.compress(raw, mtime=0)
    return raw


def source_traits(encoding: str) -> tuple[str, bool, bool]:
    """``(info.encoding, has BOM, gzip)`` of a source written with ``encoding``."""
    gz = encoding.startswith("gzip")
    enc = encoding[5:] if gz else encoding
    enc = enc or "utf-8"
    bom = enc.endswith("-bom") or enc == "bom"
    if enc == "bom":
        enc = "utf-8"
    elif bom:
        enc = enc[:-4]
    info_enc = "utf-8-bom" if (enc == "utf-8" and bom) else enc
    return info_enc, bom, gz


def make_doc(directory: str | Path, kind: str, style: str = "compact", *, n: int = 24, seed: int = 0,
             encoding: str = "utf-8", settings: Settings | None = None, name: str | None = None,
             text: bytes | None = None, open_kw: dict | None = None) -> Document:
    """Write a source document (or ``text`` with ``open_kw``) and open + index it (the caller closes)."""
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    if text is None:
        plain = directory / "plain.tmpsrc"
        kw = write_source(plain, kind, n, seed, "pretty2" if style == "crlf" else style)
        data = plain.read_bytes()
        plain.unlink()
        if style == "crlf":
            data = data.replace(b"\n", b"\r\n")
    else:
        data = text
        kw = dict(open_kw or {})
    if name is None:
        name = "src.json.gz" if encoding.startswith("gzip") else "src.json"
    path = directory / name
    path.write_bytes(encode_source(data, encoding))
    doc = Document.open(path, settings=settings, **kw)
    doc.build_index()
    assert doc.error is None, doc.error
    return doc


def apply_pattern(ob: OverlayBuilder, doc: Document, pattern: str, seed: int = 7) -> None:
    """One edit pattern of :data:`PATTERNS` (single documents: the root is replaced instead of record-level
    structure changes)."""
    rng = random.Random(seed)
    if pattern in SCENARIOS:
        apply_scenario(ob, doc, pattern, rng)
        return
    rec = lambda i: new_record(doc, rng, i, f"p{i}")  # noqa: E731
    if doc.kind == "single":
        ob.set(0, rec(1))
        return
    n = len(ob)
    if pattern == "insert_start":
        ob.insert(0, rec(1))
    elif pattern == "insert_middle":
        ob.insert(n // 2, rec(2))
        ob.insert(n // 2 + 1, rec(3))
    elif pattern == "insert_end":
        ob.insert(n, rec(4))
    elif pattern == "delete":
        ob.delete(*sorted({0, 1, n // 2, n - 1} & set(range(n))))
    elif pattern == "delete_all":
        ob.delete(*range(n))
    elif pattern == "move":
        if n >= 3:
            ob.move([0, 1], n - 2)
            ob.move([len(ob) - 1], 0)
    else:
        raise ValueError(pattern)


def jsonl_filter(data: bytes) -> bytes:
    """A JSON Lines byte range without its broken lines (§12.7): a line holding a non-whitespace byte is
    removed with its line end; an unterminated broken last line without."""
    pieces = data.split(b"\n")
    last = len(pieces) - 1
    out = []
    for i, p in enumerate(pieces):
        if p.strip(WS):
            if i == last:
                out.append(b"")
            continue
        out.append(p)
    return b"\n".join(out)


def _raw(doc: Document) -> bytes:
    base = doc.pinned(None)
    with base.open_data() as f:
        return f.read()[:doc.data_size]


def oracle_text(doc: Document, ob: OverlayBuilder | None, *, drop_broken: bool = False) -> bytes:
    """The UTF-8 text (no BOM) a save of ``doc`` with the edits of ``ob`` (``None``: no edits) must write,
    built record by record from the rules of docs/ARCHITECTURE.md §12.7 (+ the saver's decisions 3/4)."""
    raw = _raw(doc)
    ds, dz = int(doc.info.data_start), int(doc.data_size)
    base = doc.pinned(None)
    N = base.base_record_count
    spans = [base.record_span(i) for i in range(N)]
    style = editops.detect_style(doc)
    sep = style.record_sep
    kind = doc.kind
    filt = jsonl_filter if (kind == "jsonl" and drop_broken) else (lambda b: b)
    if ob is None:
        rids = list(range(N))
        recs = [raw[s:e] for s, e in spans]
        edited: set[int] = set()
        wtext = None
    else:
        rids = list(ob.rids)
        recs = list(ob.records)
        edited = {r for r in rids if r >= N or ob.content(r) is not None}
        wtext = ob.wrapper_text
    n = len(rids)
    body: list[bytes] = []
    for j, rid in enumerate(rids):
        if j:
            a = rids[j - 1]
            if rid == a + 1 and rid < N:
                gap = raw[spans[a][1]:spans[rid][0]]
                if kind == "stream" and not gap and (a in edited or rid in edited):
                    gap = sep
                body.append(filt(gap))
            else:
                body.append(sep)
        body.append(recs[j])
    text = b"".join(body)
    rest = raw[ds:]
    rs = ds + len(rest) - len(rest.lstrip(WS))
    te = len(raw.rstrip(WS)) if rest.strip(WS) else ds
    if kind in ("jsonl", "stream"):
        if not n:
            return b""
        if N:
            return filt(raw[ds:spans[0][0]]) + text + filt(raw[spans[-1][1]:dz])
        return text + style.newline
    unit = style.unit
    nl_open = style.newline + (unit * style.record_level).encode() if unit is not None else b""
    nl_close = style.newline + (unit * max(0, style.record_level - 1)).encode() if unit is not None else b""
    if kind == "wrapped":
        cs0, cs1 = doc.container_span
        if wtext is not None:
            loc = editops.locate(wtext, tuple(doc.record_path or ()))
            prefix = raw[ds:rs] + wtext[:loc.start]
            suffix = wtext[loc.end:] + raw[te:dz]
        else:
            prefix, suffix = raw[ds:cs0], raw[cs1:dz]
        if not n:
            return prefix + style.empty + suffix
        if N:
            return prefix + raw[cs0:spans[0][0]] + text + raw[spans[-1][1]:cs1] + suffix
        return prefix + raw[cs0:cs0 + 1] + nl_open + text + nl_close + raw[cs1 - 1:cs1] + suffix
    if kind == "single":
        return raw[ds:spans[0][0]] + text + raw[spans[0][1]:dz]
    lead, trail = raw[ds:rs], raw[te:dz]
    opener = b"[" if kind == "brace_list" else raw[rs:rs + 1]
    closer = b"]" if kind == "brace_list" else raw[te - 1:te]
    if not n:
        return lead + style.empty + trail
    if N:
        return lead + opener + raw[rs + 1:spans[0][0]] + text + raw[spans[-1][1]:te - 1] + closer + trail
    return lead + opener + nl_open + text + nl_close + closer + trail


def decode_saved(raw: bytes) -> tuple[bytes, str, bool, bool]:
    """``(UTF-8 text, encoding, has BOM, gzip)`` of a saved file's bytes."""
    gz = raw[:2] == b"\x1f\x8b"
    if gz:
        raw = gzip.decompress(raw)
    if raw.startswith(codecs.BOM_UTF8):
        return raw[3:], "utf-8-bom", True, gz
    if raw.startswith(b"\xff\xfe"):
        return raw[2:].decode("utf-16-le").encode("utf-8"), "utf-16-le", True, gz
    if raw.startswith(b"\xfe\xff"):
        return raw[2:].decode("utf-16-be").encode("utf-8"), "utf-16-be", True, gz
    if len(raw) >= 2 and raw[0] and not raw[1]:
        return raw.decode("utf-16-le").encode("utf-8"), "utf-16-le", False, gz
    if len(raw) >= 2 and not raw[0] and raw[1]:
        return raw.decode("utf-16-be").encode("utf-8"), "utf-16-be", False, gz
    return raw, "utf-8", False, gz


def reopen(path: str | Path, report: Any, settings: Settings | None = None) -> Document:
    """Open a saved file with the report's ``reopen_options`` and index it."""
    doc = Document.open(path, settings=settings, **report.reopen_options)
    doc.build_index()
    return doc


class FakeStore:
    """``journal(data)`` like ``EditStore.journal`` (durable: also written to ``path`` when given)."""

    def __init__(self, path: str | Path | None = None) -> None:
        self.saving: dict | None = None
        self.calls: list[dict | None] = []
        self.path = Path(path) if path is not None else None

    def journal(self, data: dict | None) -> None:
        self.saving = None if data is None else json.loads(json.dumps(data))
        self.calls.append(self.saving)
        if self.path is not None:
            self.path.write_text(json.dumps(self.saving), encoding="utf-8")


def edited(tmp_path: Path, docs: list, kind: str, pattern: str, *, style: str = "compact", n: int = 24,
           seed: int = 0, encoding: str = "utf-8", settings: Settings | None = None,
           name: str | None = None) -> tuple[Document, OverlayBuilder | None]:
    """A source document with the edits of ``pattern`` set as its overlay (``None`` builder for "none")."""
    doc = make_doc(tmp_path / "src", kind, style, n=n, seed=seed, encoding=encoding, settings=settings, name=name)
    docs.append(doc)
    ob = OverlayBuilder(doc, tmp_path / "layer")
    apply_pattern(ob, doc, pattern, seed + 7)
    doc.set_overlay(ob.state())
    return doc, ob


def check_saved(doc: Document, ob: OverlayBuilder | None, path: Path, report: saver.SaveReport, *,
                drop_broken: bool = False, want: bytes | None = None) -> bytes:
    """(1) + (2): the saved file holds exactly the oracle's text and reopens to the expected records
    (``want``: the oracle computed before an in-place save replaced the source)."""
    text, _enc, _bom, _gz = decode_saved(path.read_bytes())
    if want is None:
        want = oracle_text(doc, ob, drop_broken=drop_broken)
    assert text == want
    new = reopen(path, report)          # default settings: records of any size are read whole
    try:
        assert new.error is None and new.kind == report.kind
        assert ob is not None
        expected = list(ob.records)
        assert new.record_count == len(expected) == report.records
        assert [new.record_bytes(i) for i in range(new.record_count)] == expected
        if new.kind == "wrapped":
            # the wrapper around the records, parsed
            want_root = codec.loads(want)
            got_root = new.root().value()
            assert got_root == want_root
    finally:
        new.close()
    return text


def temp_leftovers(folder: Path) -> list[str]:
    return sorted(n for n in os.listdir(folder) if n.endswith((saver.TEMP_SUFFIX, ".old", ".tmp")))
