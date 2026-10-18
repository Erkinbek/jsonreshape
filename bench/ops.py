r"""One measured benchmark operation per sub-command (AGENT_PROMPT §12, §14 test 25).

``bench/run_bench.py`` starts every operation in a fresh process with a fresh
``JSONRESHAPE_HOME`` (no cached index unless the operation is ``reopen``) and measures the
wall time and the peak RSS of that process from outside. This module performs the
operation itself with the jsonreshape Python API and prints ONE JSON line on stdout with
its internal timings and counts plus the process's own resource usage at the end (peak
working set / peak RSS, peak private bytes, CPU time).

Usage (from the project root, with the venv interpreter)::

    python bench/ops.py open     PATH [--records 50]
    python bench/ops.py index    PATH
    python bench/ops.py reopen   PATH [--records 50]            # needs the JSONRESHAPE_HOME of an `index` run
    python bench/ops.py node     PATH [--key items] [--format single|auto] [--page 50]
    python bench/ops.py search   PATH TEXT [--mode plain|regex|number] [--max-hits N] [--no-prebuild]
    python bench/ops.py filter   PATH EXPR [--strategy auto|extract|parse]
    python bench/ops.py sort     PATH KEY [KEY ...] [--by=-KEY] [--memory-limit SIZE]
    python bench/ops.py export   PATH OUT [--fmt json|jsonl|csv] [--indent 2|tab|none] [--columns a,b.c] [--keep]
    python bench/ops.py sort+export   PATH OUT KEY [KEY ...] [--by=-KEY] [--memory-limit SIZE] [--fmt F] [--keep]
    python bench/ops.py filter+export PATH OUT EXPR [--strategy auto|extract|parse] [--fmt F] [--keep]
    python bench/ops.py schema   PATH [--sample 10000]
    python bench/ops.py coldread PATH                            # Windows: unbuffered (uncached) reads
    python bench/ops.py cold     PATH [--records 50]            # PATH must not be in the OS file cache

What each operation measures (all times in seconds, ``time.perf_counter``):

* ``open``: ``Document.open`` + background index (``start_index``) + the first ``--records``
  records as the tree shows them (``root().children(0, n)``, value preview and parsed value);
  ``first_records_seconds`` is counted from the ``Document.open`` call,
  ``since_process_start_seconds`` from the creation of this process (interpreter start and
  imports included). The background index is then cancelled.
* ``index``: ``Document.open`` + ``build_index()``; ``mb_per_s`` = data bytes / index wall time;
  ``index_stats`` are the reader's own timings (``first_record_seconds`` included).
* ``reopen``: ``Document.open`` finding the complete cached index + the first records.
* ``node``: the inner container ``--key`` (default ``items``) of a document opened as one
  value (``--format single``): expanding the root (``root_children_seconds``), then the
  node itself -- ``child_count()`` (on-demand child scan, child spans on disk) + the first
  page of children with their previews (``node_open_seconds``), the last page and the
  middle element's value (``node_tail_seconds``), and a second ``Document.open`` that finds
  the child index in the cache (``node_reopen_seconds``). ``--format auto`` opens the file
  as detected (``node_1m.json`` becomes a wrapped document whose records are ``items``).
* ``search``: index first (timed separately, so the search can use byte mode), then
  ``search.search``; ``first_hit_seconds`` is the delay of the first ``on_hits`` batch.
  ``--no-prebuild`` searches right after ``Document.open`` while the index is built in the
  background (the GUI flow); its times are counted from the ``Document.open`` call.
* ``filter`` / ``sort`` / ``export`` / ``schema``: index first (timed separately), then the
  operation; ``mb_per_s`` = record bytes read (export: data bytes) / operation time.
* ``sort+export`` / ``filter+export``: what the user gets from ``jsonreshape sort IN OUT
  --by KEY`` / ``jsonreshape filter IN OUT --where EXPR`` (AGENT_PROMPT §6.4: the sort
  result is exported in sorted order): ``Document.open`` + index + ``sort_records`` /
  ``filter_records`` + ``writer.export`` of the resulting RecordSet (default: JSONL, or the
  format of OUT's extension). ``total_seconds`` = from the ``Document.open`` call until the
  output file is written (the end-to-end time, used by the §12 sort row);
  ``index_seconds``, ``sort_seconds`` / ``filter_seconds`` and ``export_seconds`` are the
  phases (``seconds`` = the phases after the index); ``export_mb_per_s`` = bytes written /
  export time; ``mb_per_s`` = data bytes / ``total_seconds``; ``peak_rss_after_sort`` = the
  process's peak RSS when the sort phase ended (before the export). ``sort`` and ``export`` alone
  do not show this: the export of a sorted set reads the records in key order (from random
  positions of the file), the plain ``export`` reads them in file order.
* ``coldread`` (Windows): ``FILE_FLAG_NO_BUFFERING`` reads bypass the OS file cache:
  ``read_mb_per_s`` = raw sequential disk read, ``scan_mb_per_s`` = unbuffered reads in a
  reader thread feeding the core ``Scanner`` (an approximation of a cold index).
* ``cold``: ``open`` + waiting for the background index, on a file that is not in the OS
  cache (``run_bench.py`` makes it with :func:`cold_copy`); ``disk_read_bytes`` is the
  growth of the read counter of the physical disk holding the file (system-wide, so other
  programs add noise) and shows whether the data really came from the disk.

Without ``JSONRESHAPE_HOME`` a temporary home under ``bench/data/homes`` is used and
removed at the end (never the real ``%LOCALAPPDATA%\jsonreshape``).
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
import threading
import time
import traceback
from pathlib import Path
from typing import Any, Callable

T_START = time.perf_counter()

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import psutil  # noqa: E402

MiB = 1 << 20
DEFAULT_PAGE = 50
COLD_CHUNK = 8 * MiB
SECTOR = 4096          # alignment of unbuffered I/O (a multiple of every common sector size)

now = time.perf_counter


def _r(x: float | None, nd: int = 6) -> float | None:
    return None if x is None else round(float(x), nd)


# ----------------------------------------------------------------------------------
# Windows helpers: unbuffered I/O, disk numbers (also used by run_bench.py)
# ----------------------------------------------------------------------------------
if sys.platform == "win32":
    import ctypes
    from ctypes import wintypes as wt

    _k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    _k32.CreateFileW.restype = wt.HANDLE
    _k32.CreateFileW.argtypes = [wt.LPCWSTR, wt.DWORD, wt.DWORD, ctypes.c_void_p, wt.DWORD, wt.DWORD, wt.HANDLE]
    _k32.ReadFile.restype = wt.BOOL
    _k32.ReadFile.argtypes = [wt.HANDLE, ctypes.c_void_p, wt.DWORD, ctypes.POINTER(wt.DWORD), ctypes.c_void_p]
    _k32.WriteFile.restype = wt.BOOL
    _k32.WriteFile.argtypes = [wt.HANDLE, ctypes.c_void_p, wt.DWORD, ctypes.POINTER(wt.DWORD), ctypes.c_void_p]
    _k32.VirtualAlloc.restype = ctypes.c_void_p
    _k32.VirtualAlloc.argtypes = [ctypes.c_void_p, ctypes.c_size_t, wt.DWORD, wt.DWORD]
    _k32.VirtualFree.restype = wt.BOOL
    _k32.VirtualFree.argtypes = [ctypes.c_void_p, ctypes.c_size_t, wt.DWORD]
    _k32.CloseHandle.restype = wt.BOOL
    _k32.CloseHandle.argtypes = [wt.HANDLE]
    _k32.DeviceIoControl.restype = wt.BOOL
    _k32.DeviceIoControl.argtypes = [wt.HANDLE, wt.DWORD, ctypes.c_void_p, wt.DWORD, ctypes.c_void_p, wt.DWORD,
                                     ctypes.POINTER(wt.DWORD), ctypes.c_void_p]

    _GENERIC_READ = 0x80000000
    _GENERIC_WRITE = 0x40000000
    _SHARE_ALL = 0x1 | 0x2 | 0x4
    _OPEN_EXISTING = 3
    _CREATE_ALWAYS = 2
    _NO_BUFFERING = 0x20000000
    _WRITE_THROUGH = 0x80000000
    _SEQUENTIAL_SCAN = 0x08000000
    _MEM_COMMIT_RESERVE = 0x1000 | 0x2000
    _MEM_RELEASE = 0x8000
    _PAGE_READWRITE = 0x04
    _INVALID_HANDLE = ctypes.c_void_p(-1).value
    _IOCTL_STORAGE_GET_DEVICE_NUMBER = 0x2D1080

    class _StorageDeviceNumber(ctypes.Structure):
        _fields_ = [("DeviceType", wt.DWORD), ("DeviceNumber", wt.DWORD), ("PartitionNumber", wt.DWORD)]

    def _create(path: str, access: int, disposition: int, flags: int) -> Any:
        h = _k32.CreateFileW(path, access, _SHARE_ALL, None, disposition, flags, None)
        if h is None or h == _INVALID_HANDLE:
            raise ctypes.WinError(ctypes.get_last_error())
        return h

    def _alloc(size: int) -> int:
        buf = _k32.VirtualAlloc(None, size, _MEM_COMMIT_RESERVE, _PAGE_READWRITE)  # page aligned
        if not buf:
            raise ctypes.WinError(ctypes.get_last_error())
        return buf

    def _read_into(h: Any, buf: int, size: int) -> int:
        n = wt.DWORD(0)
        if not _k32.ReadFile(h, buf, size, ctypes.byref(n), None):
            raise ctypes.WinError(ctypes.get_last_error())
        return n.value


def cold_copy(src: str | os.PathLike[str], dst: str | os.PathLike[str], chunk: int = COLD_CHUNK) -> float:
    """Copy ``src`` to ``dst`` with unbuffered reads and unbuffered, write-through writes,
    so that the copy never enters the OS file cache: the first read of ``dst`` comes from
    the disk (a "cold" file). Windows only. Returns the seconds taken. The last partial
    sector is appended with an ordinary write (at most 4 KiB end up cached)."""
    if sys.platform != "win32":
        raise OSError("cold_copy needs Windows (FILE_FLAG_NO_BUFFERING)")
    src, dst = os.path.abspath(src), os.path.abspath(dst)
    size = os.path.getsize(src)
    bulk = size - size % SECTOR
    t0 = now()
    hin = _create(src, _GENERIC_READ, _OPEN_EXISTING, _NO_BUFFERING | _SEQUENTIAL_SCAN)
    try:
        hout = _create(dst, _GENERIC_WRITE, _CREATE_ALWAYS, _NO_BUFFERING | _WRITE_THROUGH)
    except BaseException:
        _k32.CloseHandle(hin)
        raise
    buf = 0
    ok = False
    try:
        buf = _alloc(chunk)
        done = 0
        written = wt.DWORD(0)
        while done < bulk:
            want = min(chunk, bulk - done)
            if _read_into(hin, buf, want) != want:
                raise OSError(f"short read from {src} at {done}")
            if not _k32.WriteFile(hout, buf, want, ctypes.byref(written), None) or written.value != want:
                raise ctypes.WinError(ctypes.get_last_error())
            done += want
        ok = True
    finally:
        if buf:
            _k32.VirtualFree(buf, 0, _MEM_RELEASE)
        _k32.CloseHandle(hin)
        _k32.CloseHandle(hout)
        if not ok:
            try:
                os.remove(dst)
            except OSError:
                pass
    if size > bulk:
        with open(src, "rb") as f:
            f.seek(bulk)
            tail = f.read()
        with open(dst, "ab") as f:
            f.write(tail)
    if os.path.getsize(dst) != size:
        raise OSError(f"cold copy of {src} has the wrong size")
    return now() - t0


def disk_key(path: str | os.PathLike[str]) -> str | None:
    """``psutil.disk_io_counters(perdisk=True)`` key of the physical disk holding ``path``
    (Windows: ``PhysicalDriveN`` via ``IOCTL_STORAGE_GET_DEVICE_NUMBER``), or ``None``."""
    if sys.platform != "win32":
        return None
    drive = os.path.splitdrive(os.path.abspath(path))[0]
    if len(drive) != 2 or drive[1] != ":":
        return None
    try:
        h = _create("\\\\.\\" + drive, 0, _OPEN_EXISTING, 0)
    except OSError:
        return None
    try:
        sdn = _StorageDeviceNumber()
        got = wt.DWORD(0)
        if not _k32.DeviceIoControl(h, _IOCTL_STORAGE_GET_DEVICE_NUMBER, None, 0, ctypes.byref(sdn),
                                    ctypes.sizeof(sdn), ctypes.byref(got), None):
            return None
        return f"PhysicalDrive{sdn.DeviceNumber}"
    finally:
        _k32.CloseHandle(h)


def disk_read_bytes(key: str | None) -> int | None:
    """Bytes read so far from disk ``key`` (all disks when ``None``), system-wide."""
    try:
        counters = psutil.disk_io_counters(perdisk=True)
    except Exception:
        return None
    if not counters:
        return None
    if key is not None and key in counters:
        return int(counters[key].read_bytes)
    return int(sum(c.read_bytes for c in counters.values()))


# ----------------------------------------------------------------------------------
# process bookkeeping
# ----------------------------------------------------------------------------------
def self_usage() -> dict[str, Any]:
    """Resource usage of this process so far (peak working set on Windows)."""
    p = psutil.Process()
    out: dict[str, Any] = {}
    mi = p.memory_info()
    out["rss_end"] = int(mi.rss)
    if hasattr(mi, "peak_wset"):
        out["peak_rss"] = int(mi.peak_wset)
        out["peak_private"] = int(getattr(mi, "peak_pagefile", 0) or 0)
    else:  # POSIX: ru_maxrss (KiB on Linux, bytes on macOS)
        import resource

        ru = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
        out["peak_rss"] = int(ru if sys.platform == "darwin" else ru * 1024)
        out["peak_private"] = None
    try:
        ct = p.cpu_times()
        out["cpu_user"] = _r(ct.user, 3)
        out["cpu_system"] = _r(ct.system, 3)
    except Exception:
        pass
    try:
        io = p.io_counters()
        out["io_read_bytes"] = int(io.read_bytes)
        out["io_write_bytes"] = int(io.write_bytes)
    except Exception:
        pass
    return out


def _since_process_start() -> float | None:
    try:
        return time.time() - psutil.Process().create_time()
    except Exception:
        return None


def _open_doc(path: str, **kw: Any) -> tuple[Any, float]:
    from jsonreshape.reader import Document

    t0 = now()
    doc = Document.open(path, **kw)
    return doc, now() - t0


def _index(doc: Any) -> dict[str, Any]:
    t0 = now()
    doc.build_index()
    seconds = now() - t0
    data = max(1, doc.data_size - doc.info.data_start)
    return {
        "index_seconds": _r(seconds),
        "index_mb_per_s": _r(data / seconds / 1e6 if seconds > 0 else 0.0, 3),
        "record_count": doc.record_count,
        "index_from_cache": bool(doc.index_from_cache),
        "index_stats": dict(doc.index_stats or {}),
    }


def _doc_info(doc: Any) -> dict[str, Any]:
    from jsonreshape import core

    return {
        "kind": doc.kind,
        "record_path": doc.info.record_path_text,
        "data_size": int(doc.data_size),
        "core_backend": core.BACKEND,
        "reader_backend": doc.backend,
        "flags": int(doc.flags),
        "json_error": None if doc.error is None else str(doc.error),
    }


def _first_records(doc: Any, n: int) -> dict[str, Any]:
    """The first ``n`` records as the GUI shows them: tree rows (value preview) + parsed
    values (table/text view). Waits for the growing index, never for the whole of it."""
    kind = doc.kind
    shown = chars = 0
    if kind in ("array", "keyed", "jsonl", "stream", "brace_list"):
        nodes = doc.root().children(0, n)
        for nd in nodes:
            chars += len(nd.scalar_text(120))
            if not nd.is_large:
                nd.value()
            shown += 1
    else:  # wrapped / single: records straight from the (growing) index
        for _i, value in doc.iter_records(None):
            chars += len(repr(value)[:120])
            shown += 1
            if shown >= n:
                break
    return {"records_shown": shown, "preview_chars": chars}


# ----------------------------------------------------------------------------------
# operations
# ----------------------------------------------------------------------------------
def op_open(args: argparse.Namespace) -> dict[str, Any]:
    doc, open_s = _open_doc(args.path, use_cache=False)
    try:
        t0 = now() - open_s
        task = doc.start_index()
        first = _first_records(doc, args.records)
        first_s = now() - t0
        since_start = _since_process_start()
        count_then = doc.record_count
        task.cancel()
        doc.wait_index(60)
        out = {
            "open_seconds": _r(open_s),
            "first_records_seconds": _r(first_s),
            "since_process_start_seconds": _r(since_start),
            "records_indexed_at_first": count_then,
            **first,
            **_doc_info(doc),
        }
    finally:
        doc.close()
    return out


def op_index(args: argparse.Namespace) -> dict[str, Any]:
    doc, open_s = _open_doc(args.path, use_cache=False)
    try:
        idx = _index(doc)
        stats = idx["index_stats"]
        return {
            "open_seconds": _r(open_s),
            **idx,
            "seconds": idx["index_seconds"],
            "mb_per_s": idx["index_mb_per_s"],
            "first_record_seconds": stats.get("first_record_seconds"),
            **_doc_info(doc),
        }
    finally:
        doc.close()


def op_reopen(args: argparse.Namespace) -> dict[str, Any]:
    doc, open_s = _open_doc(args.path)
    try:
        t0 = now() - open_s
        cached = bool(doc.index_from_cache and doc.index_complete)
        first = _first_records(doc, args.records)
        first_s = now() - t0
        return {
            "open_seconds": _r(open_s),
            "first_records_seconds": _r(first_s),
            "index_from_cache": cached,
            "record_count": doc.record_count,
            **first,
            **_doc_info(doc),
        }
    finally:
        doc.close()


def _page_preview(nodes: list[Any]) -> int:
    return sum(len(nd.scalar_text(120)) for nd in nodes)


def op_node(args: argparse.Namespace) -> dict[str, Any]:
    fmt = None if args.format == "auto" else args.format
    doc, open_s = _open_doc(args.path, format=fmt, use_cache=False)
    try:
        info = _doc_info(doc)
        t0 = now()
        root = doc.root()
        kids = root.children()
        root_s = now() - t0
        node = next((k for k in kids if k.key == args.key), None)
        if node is None:
            raise SystemExit(f"node {args.key!r} not found among the root children")
        scanned_before = doc.bytes_scanned_for_nodes
        t0 = now()
        count = node.child_count()
        page = node.children(0, args.page)
        _page_preview(page)
        node_s = now() - t0
        scanned = doc.bytes_scanned_for_nodes - scanned_before
        t0 = now()
        tail = node.children(max(0, count - args.page), count)
        _page_preview(tail)
        middle = node.child(count // 2).value() if count else None
        tail_s = now() - t0
        peak_after_node = self_usage().get("peak_rss")
    finally:
        doc.close()
    # a second open finds the child index in the on-disk cache
    t0 = now()
    doc2, _ = _open_doc(args.path, format=fmt)
    try:
        node2 = doc2.node(args.key)
        count2 = node2.child_count()
        _page_preview(node2.children(0, args.page))
        reopen_s = now() - t0
        rescanned = doc2.bytes_scanned_for_nodes
    finally:
        doc2.close()
    return {
        "open_seconds": _r(open_s),
        "root_children_seconds": _r(root_s),
        "root_children": len(kids),
        "node_open_seconds": _r(node_s),
        "node_tail_seconds": _r(tail_s),
        "node_reopen_seconds": _r(reopen_s),
        "open_to_node_seconds": _r(open_s + root_s + node_s),
        "child_count": count,
        "child_count_reopen": count2,
        "node_bytes": node.size,
        "node_scan_bytes": scanned,
        "reopen_scan_bytes": rescanned,
        "page": len(page),
        "middle_value": middle,
        "peak_rss_after_node": peak_after_node,
        "format_requested": args.format,
        **info,
    }


def op_search(args: argparse.Namespace) -> dict[str, Any]:
    from jsonreshape import search as S

    t_open0 = now()
    doc, open_s = _open_doc(args.path, use_cache=False)
    try:
        idx: dict[str, Any] = {}
        if not args.no_prebuild:
            idx = _index(doc)
        opts = S.SearchOptions.build(args.text, mode=args.mode, case_sensitive=args.case_sensitive,
                                     translit=args.translit)
        needles = opts.query.prefilter_needles(doc.flags) if doc.index_complete else None
        first: dict[str, Any] = {}
        batches = 0
        t0 = t_open0 if args.no_prebuild else now()

        def on_hits(batch: Any) -> None:
            nonlocal batches
            batches += 1
            if "seconds" not in first and len(batch):
                first["seconds"] = now() - t0
                first["record"] = batch[0].record

        t_search = now()
        res = S.search(doc, opts, max_hits=args.max_hits, on_hits=on_hits)
        search_s = now() - t_search
        total_s = now() - t0
        try:
            out = {
                "open_seconds": _r(open_s),
                **idx,
                "text": args.text,
                "mode_used": res.mode_used,
                "needles": None if needles is None else len(needles),
                "hit_count": res.hit_count,
                "hits_stored": len(res.hits),
                "matching_records": res.record_count,
                "truncated": res.truncated,
                "records_scanned": res.records_scanned,
                "records_parsed": res.records_parsed,
                "bytes_scanned": res.bytes_scanned,
                "seconds": _r(search_s),
                "search_elapsed": _r(res.elapsed),
                "total_seconds": _r(total_s),
                "mb_per_s": _r(res.bytes_scanned / search_s / 1e6 if search_s > 0 else 0.0, 3),
                "first_hit_seconds": _r(first.get("seconds")),
                "first_hit_record": first.get("record"),
                "batches": batches,
                "prebuilt_index": not args.no_prebuild,
                **_doc_info(doc),
            }
        finally:
            res.close()
        return out
    finally:
        doc.close()


def op_filter(args: argparse.Namespace) -> dict[str, Any]:
    from jsonreshape.recordset import filter_records

    doc, open_s = _open_doc(args.path, use_cache=False)
    try:
        idx = _index(doc)
        t0 = now()
        rs = filter_records(doc, args.expr, strategy=args.strategy)
        seconds = now() - t0
        try:
            rep = rs.report
            return {
                "open_seconds": _r(open_s),
                **idx,
                "expr": args.expr,
                "strategy": rep.strategy,
                "scanned": rep.scanned,
                "matched": rep.matched,
                "skipped_huge": rep.skipped_huge,
                "unparseable": rep.unparseable,
                "bytes_read": rep.bytes_read,
                "seconds": _r(seconds),
                "report_elapsed": _r(rep.elapsed),
                "mb_per_s": _r(rep.bytes_read / seconds / 1e6 if seconds > 0 else 0.0, 3),
                "warnings": rep.warning_texts(),
                **_doc_info(doc),
            }
        finally:
            rs.close()
    finally:
        doc.close()


def op_sort(args: argparse.Namespace) -> dict[str, Any]:
    from jsonreshape.recordset import sort_records
    from jsonreshape.tasks import parse_size

    keys = list(args.keys) + list(args.by)
    if not keys:
        raise SystemExit("sort needs at least one key")
    doc, open_s = _open_doc(args.path, use_cache=False)
    try:
        idx = _index(doc)
        limit = parse_size(args.memory_limit) if args.memory_limit else None
        t0 = now()
        rs = sort_records(doc, keys, memory_limit=limit)
        seconds = now() - t0
        try:
            rep = rs.report
            head = [int(rs[i]) for i in range(min(3, len(rs)))]
            return {
                "open_seconds": _r(open_s),
                **idx,
                "keys": keys,
                "records": rep.records,
                "result_len": len(rs),
                "strategy": rep.strategy,
                "runs": rep.runs,
                "merge_passes": rep.merge_passes,
                "peak_bytes_estimate": rep.peak_bytes_estimate,
                "key_bytes": rep.key_bytes,
                "bytes_read": rep.bytes_read,
                "memory_limit": limit if limit is not None else doc.settings.sort_memory_bytes,
                "seconds": _r(seconds),
                "report_elapsed": _r(rep.elapsed),
                "mb_per_s": _r(rep.bytes_read / seconds / 1e6 if seconds > 0 else 0.0, 3),
                "first_records": head,
                "warnings": rep.warning_texts(),
                **_doc_info(doc),
            }
        finally:
            rs.close()
    finally:
        doc.close()


def _export_options(args: argparse.Namespace, default_fmt: str) -> Any:
    """``ExportOptions`` from ``--fmt`` (default: the extension of ``args.out``, else
    ``default_fmt``), ``--indent`` and ``--columns``; creates the output folder."""
    from jsonreshape.writer import ExportOptions, format_for_path

    fmt = args.fmt or format_for_path(args.out) or default_fmt
    kw: dict[str, Any] = {}
    indent = getattr(args, "indent", None)
    if indent is not None:
        kw["indent"] = None if indent.lower() == "none" else ("\t" if indent.lower() == "tab" else int(indent))
    columns = getattr(args, "columns", None)
    if columns:
        kw["columns"] = [c.strip() for c in columns.split(",") if c.strip()]
    os.makedirs(os.path.dirname(os.path.abspath(args.out)) or ".", exist_ok=True)
    return ExportOptions(fmt, **kw)


def _remove_outputs(args: argparse.Namespace, files: list[Path]) -> None:
    """Remove the written output file(s) unless ``--keep``."""
    if args.keep:
        return
    for p in files or [Path(args.out)]:
        try:
            os.remove(p)
        except OSError:
            pass


def _rate(nbytes: int | float, seconds: float) -> float | None:
    return _r(nbytes / seconds / 1e6 if seconds > 0 else 0.0, 3)


def op_export(args: argparse.Namespace) -> dict[str, Any]:
    from jsonreshape.writer import export

    opts = _export_options(args, "json")
    doc, open_s = _open_doc(args.path, use_cache=False)
    files: list[Path] = []
    try:
        idx = _index(doc)
        t0 = now()
        rep = export(doc, args.out, opts)
        seconds = now() - t0
        files = list(rep.files)
        data = max(1, doc.data_size - doc.info.data_start)
        return {
            "open_seconds": _r(open_s),
            **idx,
            "fmt": rep.fmt,
            "indent": opts.indent if rep.fmt == "json" else None,
            "records": rep.records,
            "rows": rep.rows,
            "bytes_in": data,
            "bytes_written": rep.bytes_written,
            "files": len(rep.files),
            "seconds": _r(seconds),
            "report_elapsed": _r(rep.elapsed),
            "mb_per_s": _rate(data, seconds),
            "out_mb_per_s": _rate(rep.bytes_written, seconds),
            "columns": list(rep.columns),
            "warnings": list(rep.warnings),
            **_doc_info(doc),
        }
    finally:
        doc.close()
        _remove_outputs(args, files)


def op_sort_export(args: argparse.Namespace) -> dict[str, Any]:
    """``jsonreshape sort IN OUT --by KEY``: index, sort_records, export of the sorted set."""
    from jsonreshape.recordset import sort_records
    from jsonreshape.tasks import parse_size
    from jsonreshape.writer import export

    keys = list(args.keys) + list(args.by)
    if not keys:
        raise SystemExit("sort+export needs at least one key")
    limit = parse_size(args.memory_limit) if args.memory_limit else None
    opts = _export_options(args, "jsonl")
    files: list[Path] = []
    t_start = now()
    doc, open_s = _open_doc(args.path, use_cache=False)
    try:
        idx = _index(doc)
        t0 = now()
        rs = sort_records(doc, keys, memory_limit=limit)
        sort_s = now() - t0
        try:
            peak_after_sort = self_usage().get("peak_rss")
            t1 = now()
            rep = export(doc, args.out, opts, source=rs)
            export_s = now() - t1
            files = list(rep.files)
            srep = rs.report
            result_len = len(rs)
            head = [int(rs[i]) for i in range(min(3, result_len))]
        finally:
            rs.close()
        total_s = now() - t_start
        data = max(1, doc.data_size - doc.info.data_start)
        return {
            "open_seconds": _r(open_s),
            **idx,
            "keys": keys,
            "records": srep.records,
            "result_len": result_len,
            "strategy": srep.strategy,
            "runs": srep.runs,
            "merge_passes": srep.merge_passes,
            "peak_bytes_estimate": srep.peak_bytes_estimate,
            "key_bytes": srep.key_bytes,
            "bytes_read": srep.bytes_read,
            "memory_limit": limit if limit is not None else doc.settings.sort_memory_bytes,
            "sort_seconds": _r(sort_s),
            "sort_mb_per_s": _rate(srep.bytes_read, sort_s),
            "peak_rss_after_sort": peak_after_sort,
            "fmt": rep.fmt,
            "exported": rep.records,
            "bytes_in": data,
            "bytes_written": rep.bytes_written,
            "files": len(rep.files),
            "export_seconds": _r(export_s),
            "export_mb_per_s": _rate(rep.bytes_written, export_s),
            "seconds": _r(sort_s + export_s),
            "total_seconds": _r(total_s),
            "mb_per_s": _rate(data, total_s),
            "first_records": head,
            "warnings": srep.warning_texts() + list(rep.warnings),
            **_doc_info(doc),
        }
    finally:
        doc.close()
        _remove_outputs(args, files)


def op_filter_export(args: argparse.Namespace) -> dict[str, Any]:
    """``jsonreshape filter IN OUT --where EXPR``: index, filter_records, export of the matches."""
    from jsonreshape.recordset import filter_records
    from jsonreshape.writer import export

    opts = _export_options(args, "jsonl")
    files: list[Path] = []
    t_start = now()
    doc, open_s = _open_doc(args.path, use_cache=False)
    try:
        idx = _index(doc)
        t0 = now()
        rs = filter_records(doc, args.expr, strategy=args.strategy)
        filter_s = now() - t0
        try:
            t1 = now()
            rep = export(doc, args.out, opts, source=rs)
            export_s = now() - t1
            files = list(rep.files)
            frep = rs.report
        finally:
            rs.close()
        total_s = now() - t_start
        data = max(1, doc.data_size - doc.info.data_start)
        return {
            "open_seconds": _r(open_s),
            **idx,
            "expr": args.expr,
            "strategy": frep.strategy,
            "scanned": frep.scanned,
            "matched": frep.matched,
            "skipped_huge": frep.skipped_huge,
            "unparseable": frep.unparseable,
            "bytes_read": frep.bytes_read,
            "filter_seconds": _r(filter_s),
            "filter_mb_per_s": _rate(frep.bytes_read, filter_s),
            "fmt": rep.fmt,
            "exported": rep.records,
            "bytes_in": data,
            "bytes_written": rep.bytes_written,
            "files": len(rep.files),
            "export_seconds": _r(export_s),
            "export_mb_per_s": _rate(rep.bytes_written, export_s),
            "seconds": _r(filter_s + export_s),
            "total_seconds": _r(total_s),
            "mb_per_s": _rate(data, total_s),
            "warnings": frep.warning_texts() + list(rep.warnings),
            **_doc_info(doc),
        }
    finally:
        doc.close()
        _remove_outputs(args, files)


def op_schema(args: argparse.Namespace) -> dict[str, Any]:
    from jsonreshape.schema import infer_schema

    doc, open_s = _open_doc(args.path, use_cache=False)
    try:
        idx = _index(doc)
        t0 = now()
        sch = infer_schema(doc, sample=args.sample)
        seconds = now() - t0
        return {
            "open_seconds": _r(open_s),
            **idx,
            "sample": args.sample,
            "seconds": _r(seconds),
            "fields": len(sch.fields()),
            "records_analysed": sch.scanned,
            "total_records": sch.total_records,
            "summary": sch.summary(),
            **_doc_info(doc),
        }
    finally:
        doc.close()


def _unbuffered_read(path: str) -> tuple[int, float]:
    h = _create(os.path.abspath(path), _GENERIC_READ, _OPEN_EXISTING, _NO_BUFFERING | _SEQUENTIAL_SCAN)
    buf = 0
    try:
        buf = _alloc(COLD_CHUNK)
        total = 0
        t0 = now()
        while True:
            n = _read_into(h, buf, COLD_CHUNK)
            total += n
            if n < COLD_CHUNK:
                break
        return total, now() - t0
    finally:
        if buf:
            _k32.VirtualFree(buf, 0, _MEM_RELEASE)
        _k32.CloseHandle(h)


def _unbuffered_scan(path: str, nbuf: int = 4) -> tuple[int, float, int, Any]:
    """Unbuffered reads in a reader thread feeding the core Scanner (MODE_CONTAINER over
    the root array), i.e. what indexing costs when nothing comes from the OS cache."""
    import queue

    from jsonreshape import core

    h = _create(os.path.abspath(path), _GENERIC_READ, _OPEN_EXISTING, _NO_BUFFERING | _SEQUENTIAL_SCAN)
    bufs: list[int] = []
    free: queue.Queue = queue.Queue()
    full: queue.Queue = queue.Queue()
    stop = threading.Event()
    try:
        for _ in range(nbuf):
            bufs.append(_alloc(COLD_CHUNK))
            free.put(bufs[-1])

        def reader() -> None:
            try:
                while not stop.is_set():
                    b = free.get()
                    if b is None:
                        return
                    n = _read_into(h, b, COLD_CHUNK)
                    full.put((b, n))
                    if n < COLD_CHUNK:
                        full.put((None, None))
                        return
            except BaseException as exc:  # handed to the consumer
                full.put((None, exc))

        t0 = now()
        th = threading.Thread(target=reader, name="coldread", daemon=True)
        th.start()
        scanner = core.Scanner(core.MODE_CONTAINER, [], 0)
        total = spans = 0
        while True:
            b, n = full.get()
            if b is None:
                if isinstance(n, BaseException):
                    raise n
                break
            data = ctypes.string_at(b, n)
            free.put(b)
            total += n
            spans += len(scanner.feed(data)) // 16
        spans += len(scanner.finish()) // 16
        seconds = now() - t0
        th.join(10)
        return total, seconds, spans, scanner.error
    finally:
        stop.set()
        free.put(None)
        for b in bufs:
            _k32.VirtualFree(b, 0, _MEM_RELEASE)
        _k32.CloseHandle(h)


def op_coldread(args: argparse.Namespace) -> dict[str, Any]:
    if sys.platform != "win32":
        raise SystemExit("coldread needs Windows (FILE_FLAG_NO_BUFFERING)")
    from jsonreshape import core

    key = disk_key(args.path)
    d0 = disk_read_bytes(key)
    total, read_s = _unbuffered_read(args.path)
    d1 = disk_read_bytes(key)
    total2, scan_s, spans, err = _unbuffered_scan(args.path)
    return {
        "bytes": total,
        "read_seconds": _r(read_s),
        "read_mb_per_s": _r(total / read_s / 1e6 if read_s > 0 else 0.0, 3),
        "scan_seconds": _r(scan_s),
        "scan_mb_per_s": _r(total2 / scan_s / 1e6 if scan_s > 0 else 0.0, 3),
        "scan_records": spans,
        "scan_error": None if err is None else list(err),
        "disk": key,
        "disk_read_bytes": None if d0 is None or d1 is None else d1 - d0,
        "core_backend": core.BACKEND,
    }


def op_cold(args: argparse.Namespace) -> dict[str, Any]:
    key = disk_key(args.path)
    d0 = disk_read_bytes(key)
    doc, open_s = _open_doc(args.path, use_cache=False)
    try:
        t0 = now() - open_s
        task = doc.start_index()
        first = _first_records(doc, args.records)
        first_s = now() - t0
        doc.wait_index()
        task.result()
        all_s = now() - t0
        d1 = disk_read_bytes(key)
        stats = dict(doc.index_stats or {})
        return {
            "open_seconds": _r(open_s),
            "first_records_seconds": _r(first_s),
            "open_and_index_seconds": _r(all_s),
            "record_count": doc.record_count,
            "index_stats": stats,
            "mb_per_s": stats.get("mb_per_s"),
            "disk": key,
            "disk_read_bytes": None if d0 is None or d1 is None else d1 - d0,
            **first,
            **_doc_info(doc),
        }
    finally:
        doc.close()


OPS: dict[str, Callable[[argparse.Namespace], dict[str, Any]]] = {
    "open": op_open,
    "index": op_index,
    "reopen": op_reopen,
    "node": op_node,
    "search": op_search,
    "filter": op_filter,
    "sort": op_sort,
    "export": op_export,
    "sort+export": op_sort_export,
    "filter+export": op_filter_export,
    "schema": op_schema,
    "coldread": op_coldread,
    "cold": op_cold,
}

EXPORT_FORMATS = ["json", "jsonl", "csv", "xlsx", "sqlite"]


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(prog="ops.py", description=__doc__.splitlines()[0])
    sub = ap.add_subparsers(dest="op", required=True)
    p = sub.add_parser("open", help="open + first records visible")
    p.add_argument("path")
    p.add_argument("--records", type=int, default=DEFAULT_PAGE)
    p = sub.add_parser("index", help="open + full index")
    p.add_argument("path")
    p = sub.add_parser("reopen", help="open with the cached index")
    p.add_argument("path")
    p.add_argument("--records", type=int, default=DEFAULT_PAGE)
    p = sub.add_parser("node", help="expand an inner node with many children")
    p.add_argument("path")
    p.add_argument("--key", default="items")
    p.add_argument("--format", default="single", help="single (default) or auto")
    p.add_argument("--page", type=int, default=DEFAULT_PAGE)
    p = sub.add_parser("search", help="full-text search")
    p.add_argument("path")
    p.add_argument("text")
    p.add_argument("--mode", default="plain", choices=["plain", "regex", "number"])
    p.add_argument("--case-sensitive", action="store_true")
    p.add_argument("--translit", action="store_true")
    p.add_argument("--max-hits", type=int, default=100000)
    p.add_argument("--no-prebuild", action="store_true", help="search while the index is built in the background")
    p = sub.add_parser("filter", help="filter expression")
    p.add_argument("path")
    p.add_argument("expr")
    p.add_argument("--strategy", default="auto", choices=["auto", "extract", "parse"])
    p = sub.add_parser("sort", help="sort by one or more keys")
    p.add_argument("path")
    p.add_argument("keys", nargs="*", help="sort keys (a descending key: --by=-id)")
    p.add_argument("--by", action="append", default=[], help="another sort key, e.g. --by=-id")
    p.add_argument("--memory-limit", default=None, help="e.g. 256MB (default: Settings.sort_memory_bytes)")
    p = sub.add_parser("export", help="export to json/jsonl/csv")
    p.add_argument("path")
    p.add_argument("out")
    p.add_argument("--fmt", default=None, choices=EXPORT_FORMATS)
    p.add_argument("--indent", default=None, help="2, 4, tab or none (json)")
    p.add_argument("--columns", default=None)
    p.add_argument("--keep", action="store_true", help="keep the output file")
    p = sub.add_parser("sort+export", help="sort, then export the sorted records (jsonreshape sort)")
    p.add_argument("path")
    p.add_argument("out")
    p.add_argument("keys", nargs="*", help="sort keys (a descending key: --by=-id)")
    p.add_argument("--by", action="append", default=[], help="another sort key, e.g. --by=-id")
    p.add_argument("--memory-limit", default=None, help="e.g. 256MB (default: Settings.sort_memory_bytes)")
    p.add_argument("--fmt", default=None, choices=EXPORT_FORMATS, help="default: from OUT's extension, else jsonl")
    p.add_argument("--indent", default=None, help="2, 4, tab or none (json)")
    p.add_argument("--keep", action="store_true", help="keep the output file")
    p = sub.add_parser("filter+export", help="filter, then export the matching records (jsonreshape filter)")
    p.add_argument("path")
    p.add_argument("out")
    p.add_argument("expr")
    p.add_argument("--strategy", default="auto", choices=["auto", "extract", "parse"])
    p.add_argument("--fmt", default=None, choices=EXPORT_FORMATS, help="default: from OUT's extension, else jsonl")
    p.add_argument("--indent", default=None, help="2, 4, tab or none (json)")
    p.add_argument("--keep", action="store_true", help="keep the output file")
    p = sub.add_parser("schema", help="structure inference from a sample")
    p.add_argument("path")
    p.add_argument("--sample", type=int, default=10000)
    p = sub.add_parser("coldread", help="unbuffered disk read (+ scan)")
    p.add_argument("path")
    p = sub.add_parser("cold", help="open + first records + index of an uncached file")
    p.add_argument("path")
    p.add_argument("--records", type=int, default=DEFAULT_PAGE)
    return ap


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    temp_home: Path | None = None
    if not os.environ.get("JSONRESHAPE_HOME"):
        base = ROOT / "bench" / "data" / "homes"
        base.mkdir(parents=True, exist_ok=True)
        temp_home = base / f"ops-{os.getpid()}-{int(time.time())}"
        temp_home.mkdir(parents=True, exist_ok=True)
        os.environ["JSONRESHAPE_HOME"] = str(temp_home)
    result: dict[str, Any] = {"op": args.op, "pid": os.getpid()}
    code = 0
    t_import = now()
    try:
        import jsonreshape.reader  # noqa: F401  (the import cost is reported separately)

        result["import_seconds"] = _r(now() - t_import)
        t0 = now()
        result.update(OPS[args.op](args))
        result["op_seconds"] = _r(now() - t0)
        result["ok"] = True
    except BaseException as exc:  # reported as JSON; the runner decides
        if isinstance(exc, KeyboardInterrupt):
            raise
        result["ok"] = False
        result["error"] = f"{type(exc).__name__}: {exc}"
        result["traceback"] = traceback.format_exc(limit=8)
        code = 1
    result["process_seconds"] = _r(now() - T_START)
    result.update(self_usage())
    result["home"] = os.environ.get("JSONRESHAPE_HOME")
    sys.stdout.write(json.dumps(result, ensure_ascii=True, default=str) + "\n")
    sys.stdout.flush()
    if temp_home is not None:
        shutil.rmtree(temp_home, ignore_errors=True)
    return code


if __name__ == "__main__":
    raise SystemExit(main())
