"""jsonreshape.fileio: shared opening, long paths, identity, sniffing, data-file
preparation (gzip / UTF-16 -> UTF-8), atomic writes, exact range reads.

AGENT_PROMPT §1.7 (Windows: long paths, network drives, shared read, change detection),
§3 (UTF-8 with/without BOM, UTF-16, .json.gz), §10 (results written to a temp file and
renamed atomically).
"""

from __future__ import annotations

import errno
import gzip
import hashlib
import io
import json
import os
import shutil
import sqlite3
import sys
import threading
import time
from pathlib import Path

import pytest

import jsongen
from jsonreshape import fileio
from jsonreshape.errors import Cancelled, FileChangedError, FileFormatError, JsonReshapeError, UserInputError
from jsonreshape.fileio import (
    HEAD_HASH_BYTES,
    FileIdentity,
    PreparedFile,
    Sniff,
    atomic_path,
    atomic_write,
    encoding_label,
    file_identity,
    long_path,
    normalize_path,
    open_shared,
    os_error,
    prepare_data_file,
    prepared_dir,
    prune_prepared,
    read_range,
    sniff,
)
from jsonreshape.tasks import CancelToken, Progress

WINDOWS = sys.platform == "win32"
windows_only = pytest.mark.skipif(not WINDOWS, reason="Windows-specific behaviour")

if WINDOWS:
    import ctypes
    from ctypes import wintypes

    _k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    _CreateFileW = _k32.CreateFileW
    _CreateFileW.argtypes = (wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, wintypes.LPVOID,
                             wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE)
    _CreateFileW.restype = wintypes.HANDLE
    _CloseHandle = _k32.CloseHandle
    _CloseHandle.argtypes = (wintypes.HANDLE,)

    def win_open(path: Path, share: int) -> int:
        """Open ``path`` for reading like another program would, with the given share mode."""
        handle = _CreateFileW(str(path), 0x80000000, share, None, 3, 0x80, None)
        assert handle not in (None, wintypes.HANDLE(-1).value), ctypes.get_last_error()
        return handle


def prepared_files() -> list[str]:
    return sorted(os.listdir(prepared_dir()))


# ----------------------------------------------------------------------------------
# long paths
# ----------------------------------------------------------------------------------
@windows_only
def test_long_path_windows(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    short = str(tmp_path / "a.json")
    assert long_path(short) == short
    assert long_path(Path(short)) == short
    long = "C:\\" + "\\".join(["d" * 50] * 5) + "\\file.json"
    assert len(long) >= fileio.LONG_PATH_THRESHOLD
    assert long_path(long) == "\\\\?\\" + long
    messy = long.replace("\\", "/") + "/../file.json"
    assert long_path(messy) == "\\\\?\\" + os.path.abspath(messy)  # normalized: \\?\ disables it
    assert "/" not in long_path(messy) and ".." not in long_path(messy)
    unc = "\\\\server\\share\\" + "x" * 250 + ".json"
    assert long_path(unc) == "\\\\?\\UNC\\server\\share\\" + "x" * 250 + ".json"
    for already in ("\\\\?\\C:\\x", "\\\\?\\UNC\\s\\h\\x", "\\\\.\\pipe\\x"):
        assert long_path(already) == already
    monkeypatch.chdir(tmp_path)
    rel = "r" * 250
    assert long_path(rel) == "\\\\?\\" + os.path.abspath(rel)
    astral = "C:\\" + "😀" * 120 + ".json"  # 128 code points, 248 UTF-16 units
    assert long_path(astral).startswith("\\\\?\\")
    assert long_path(os.fsencode(short)) == short


def test_long_path_is_identity_off_windows(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(fileio, "_WINDOWS", False)
    p = "/data/" + "y" * 300 + ".json"
    assert long_path(p) == p
    assert long_path(Path("rel.json")) == str(Path("rel.json"))


def test_paths_longer_than_max_path(tmp_path: Path) -> None:
    parts = [f"segment_{i}_" + "x" * 40 for i in range(6)]
    deep_dir = tmp_path.joinpath(*parts)
    os.makedirs(long_path(deep_dir), exist_ok=True)
    try:
        target = deep_dir / ("data_" + "y" * 30 + ".json")
        assert len(str(target)) > 260
        data = jsongen.build_document(jsongen.records(5, seed=1), "array")
        with atomic_write(target) as f:
            f.write(data)
        with open_shared(target) as f:
            assert f.read() == data
            assert read_range(f, 1, 10) == data[1:10]
        ident = file_identity(target)
        assert ident.size == len(data) and ident.path == normalize_path(target)
        u16 = deep_dir / ("u16_" + "z" * 30 + ".json")
        with atomic_write(u16) as f:
            f.write(data.decode("utf-8").encode("utf-16"))
        pf = prepare_data_file(u16)
        assert pf.encoding == "utf-16-le" and Path(pf.data_path).read_bytes() == data
        assert prepare_data_file(target).data_path == fileio._abspath(target)
    finally:
        shutil.rmtree(long_path(tmp_path / parts[0]), ignore_errors=True)


# ----------------------------------------------------------------------------------
# open_shared
# ----------------------------------------------------------------------------------
def test_open_shared_reads_and_modes(tmp_path: Path) -> None:
    p = tmp_path / "a.bin"
    p.write_bytes(b"0123456789")
    with open_shared(p) as f:
        assert f.read(3) == b"012"
        assert "a.bin" in str(f.name)
        assert f.readable() and not f.writable()
    with open_shared(str(p), "rb", buffering=0) as f:
        assert isinstance(f, io.FileIO) and f.read() == b"0123456789"
    for bad_mode in ("r", "w", "rt", "a", "b"):
        with pytest.raises(ValueError):
            open_shared(p, bad_mode)


def test_open_shared_write_modes(tmp_path: Path) -> None:
    p = tmp_path / "w.bin"
    with open_shared(p, "wb") as f:
        f.write(b"abc")
    with open_shared(p, "ab") as f:
        f.write(b"def")
    assert p.read_bytes() == b"abcdef"
    with open_shared(p, "r+b") as f:
        f.seek(1)
        f.write(b"X")
    assert p.read_bytes() == b"aXcdef"
    with open_shared(p, "w+b") as f:
        f.write(b"zz")
        f.seek(0)
        assert f.read() == b"zz"
    with pytest.raises(JsonReshapeError) as info:
        open_shared(p, "xb")
    assert info.value.key == "fileio.error.open"
    with open_shared(tmp_path / "new.bin", "xb") as f:
        f.write(b"1")
    assert (tmp_path / "new.bin").read_bytes() == b"1"


@windows_only
def test_open_shared_allows_write_rename_and_delete(tmp_path: Path) -> None:
    p = tmp_path / "data.json"
    p.write_bytes(b"[1,2,3]" * 1000)
    with open_shared(p, buffering=0) as f:  # unbuffered: see the other writer's bytes
        assert f.read(7) == b"[1,2,3]"
        with open(p, "r+b") as other:  # another program writes while we read
            other.write(b"[9")
        moved = tmp_path / "moved.json"
        os.rename(p, moved)  # ... renames it
        assert not p.exists() and moved.exists()
        f.seek(0)
        assert f.read(2) == b"[9"
        os.remove(moved)  # ... and deletes it
        f.seek(7)
        assert f.read(7) == b"[1,2,3]"  # the open handle keeps working
    assert not moved.exists()
    # for comparison: a plain open() blocks renaming on Windows
    q = tmp_path / "plain.json"
    q.write_bytes(b"[]")
    with open(q, "rb"):
        with pytest.raises(PermissionError):
            os.rename(q, tmp_path / "plain2.json")


def test_open_shared_errors(tmp_path: Path, lang_en) -> None:
    missing = tmp_path / "missing.json"
    with pytest.raises(UserInputError) as info:
        open_shared(missing)
    exc = info.value
    assert exc.key == "error.file_not_found" and exc.params["path"] == str(missing)
    assert str(exc) == f"File not found: {missing}"
    assert isinstance(exc.__cause__, FileNotFoundError)
    with pytest.raises(UserInputError) as info:
        open_shared(tmp_path / "no_dir" / "x.json")
    assert info.value.key == "error.file_not_found"
    with pytest.raises(UserInputError) as info:
        open_shared(tmp_path)
    assert info.value.key == "fileio.error.is_directory"
    assert "folder" in str(info.value)


@windows_only
def test_sharing_violation_says_the_file_is_in_use(tmp_path: Path, lang_uz) -> None:
    p = tmp_path / "locked.json"
    p.write_bytes(b"[]")
    handle = win_open(p, 0)  # another program holds the file exclusively
    try:
        with pytest.raises(UserInputError) as info:
            open_shared(p)
        # not "permission denied": the file is in use by another program (robustness F13)
        assert info.value.key == "fileio.error.in_use"
        assert "band qilingan" in str(info.value) and str(p) in str(info.value)
        assert info.value.hint and "dasturni yoping" in info.value.hint
    finally:
        _CloseHandle(handle)
    with open_shared(p) as f:
        assert f.read() == b"[]"


def test_os_error_mapping(tmp_path: Path) -> None:
    p = tmp_path / "x"
    assert os_error(FileNotFoundError(errno.ENOENT, "nope"), p).key == "error.file_not_found"
    assert os_error(PermissionError(errno.EACCES, "denied"), p).key == "error.permission"
    assert os_error(IsADirectoryError(errno.EISDIR, "dir"), p).key == "fileio.error.is_directory"
    full = os_error(OSError(errno.ENOSPC, "No space left on device"), p)
    assert full.key == "fileio.error.disk_full" and full.hint
    other = os_error(OSError(errno.EIO, "I/O error"), p)
    assert other.key == "fileio.error.open" and other.params["detail"] == "I/O error"
    assert os_error(OSError(errno.EIO, "x"), "\\\\?\\C:\\long").params["path"] == "C:\\long"


# ----------------------------------------------------------------------------------
# identity
# ----------------------------------------------------------------------------------
def test_file_identity(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    p = tmp_path / "Data.JSON"
    p.write_bytes(b"x" * 100)
    a = file_identity(p)
    assert a.size == 100 and a.head_hash == hashlib.sha256(b"x" * 100).hexdigest()
    assert a.path == os.path.normcase(os.path.abspath(p))
    assert a.mtime_ns == os.stat(p).st_mtime_ns
    assert a.matches(file_identity(p)) and a.matches(a) and not a.matches(None)
    monkeypatch.chdir(tmp_path)
    assert file_identity("Data.JSON").matches(a)
    if WINDOWS:
        assert file_identity(str(p).upper()).matches(a)  # case-insensitive file system
        assert file_identity("\\\\?\\" + str(p)).matches(a)
        assert a.path == a.path.lower()
    p.write_bytes(b"y" * 100)  # same size, new content
    b = file_identity(p)
    assert not a.matches(b) and b.head_hash != a.head_hash
    os.utime(p, ns=(b.mtime_ns + 10**9, b.mtime_ns + 10**9))  # touched only
    c = file_identity(p)
    assert not b.matches(c) and c.head_hash == b.head_hash
    big = tmp_path / "big.bin"
    big.write_bytes(b"a" * HEAD_HASH_BYTES + b"tail")
    assert file_identity(big).head_hash == hashlib.sha256(b"a" * HEAD_HASH_BYTES).hexdigest()
    assert FileIdentity.from_dict(json.loads(json.dumps(a.to_dict()))) == a
    assert len(a.digest()) == 32 and a.digest() == FileIdentity.from_dict(a.to_dict()).digest()
    assert a.digest() != b.digest()
    with pytest.raises(UserInputError):
        file_identity(tmp_path / "missing.json")


def test_normalize_path(tmp_path: Path) -> None:
    p = tmp_path / "Sub" / ".." / "File.json"
    n = normalize_path(p)
    assert ".." not in n and n == os.path.normcase(os.path.abspath(tmp_path / "File.json"))
    assert normalize_path(str(tmp_path / "File.json")) == n
    if WINDOWS:
        assert normalize_path("\\\\?\\" + str(tmp_path / "FILE.JSON")) == n


# ----------------------------------------------------------------------------------
# sniff
# ----------------------------------------------------------------------------------
@pytest.mark.parametrize(
    "head, encoding, bom_len, gz",
    [
        (b"", "utf-8", 0, False),
        (b"[", "utf-8", 0, False),
        (b"[1]", "utf-8", 0, False),
        (b'  {"a":1}', "utf-8", 0, False),
        (b'"\xd0\xa2"', "utf-8", 0, False),
        (b"\xef\xbb\xbf[]", "utf-8-bom", 3, False),
        (b"\xef\xbb\xbf", "utf-8-bom", 3, False),
        (b"\xff\xfe[\x00", "utf-16-le", 2, False),
        (b"\xff\xfe", "utf-16-le", 2, False),
        (b"\xfe\xff\x00[", "utf-16-be", 2, False),
        (b"\xff\xfe\x00\x00[\x00\x00\x00", "utf-32-le", 4, False),
        (b"\x00\x00\xfe\xff\x00\x00\x00[", "utf-32-be", 4, False),
        (b"[\x00{\x00", "utf-16-le", 0, False),
        (b"\x00[\x00{", "utf-16-be", 0, False),
        (b"1\x00", "utf-16-le", 0, False),
        (b"\x001", "utf-16-be", 0, False),
        (b" \x00\n\x00", "utf-16-le", 0, False),
        (b'"\x00\x22\x04', "utf-16-le", 0, False),  # '"' then a Cyrillic letter
        (b'\x00"\x04\x22', "utf-16-be", 0, False),
        (b"[\x00\x00\x00", "utf-32-le", 0, False),
        (b"\x00\x00\x00[", "utf-32-be", 0, False),
        (b"\x1f\x8b\x08\x00", "utf-8", 0, True),
        (b"\x1f\x8b", "utf-8", 0, True),
        (b"\x1f", "utf-8", 0, False),
    ],
)
def test_sniff(head: bytes, encoding: str, bom_len: int, gz: bool) -> None:
    assert sniff(head) == Sniff(encoding, bom_len, gz)
    assert sniff(bytearray(head)) == Sniff(encoding, bom_len, gz)
    assert sniff(memoryview(head + b"rest of the file ignored")) .encoding == encoding


@pytest.mark.parametrize("encoding", ["utf-16-le", "utf-16-be"])
@pytest.mark.parametrize("text", ['[{"a":"Тошкент"}]', ' \r\n{"x":1}', '"Ўзбекистон"', "12", "true", "\t[]"])
def test_sniff_real_utf16_documents(encoding: str, text: str) -> None:
    raw = text.encode(encoding)
    assert sniff(raw) == Sniff(encoding, 0, False)
    assert sniff("\ufeff".encode(encoding) + raw) == Sniff(encoding, 2, False)


def test_encoding_labels() -> None:
    assert encoding_label("utf-16-le") == "UTF-16 LE"
    assert encoding_label("utf-8-bom") == "UTF-8 (BOM)"
    assert encoding_label("latin-1") == "latin-1"


# ----------------------------------------------------------------------------------
# prepare_data_file
# ----------------------------------------------------------------------------------
CASES = [
    ("utf-8", False, False),
    ("utf-8", True, False),
    ("utf-16-le", True, False),
    ("utf-16-le", False, False),
    ("utf-16-be", True, False),
    ("utf-16-be", False, False),
    ("utf-8", False, True),
    ("utf-8", True, True),
    ("utf-16-le", True, True),
    ("utf-16-le", False, True),
    ("utf-16-be", True, True),
    ("utf-16-be", False, True),
]


@pytest.mark.parametrize("fmt", ["array", "jsonl", "wrapped"])
@pytest.mark.parametrize("encoding, bom, gz", CASES)
def test_prepare_round_trip(tmp_path: Path, encoding: str, bom: bool, gz: bool, fmt: str) -> None:
    recs = jsongen.records(40, seed=3)
    src = tmp_path / ("input.json.gz" if gz else "input.json")
    utf8 = jsongen.write_file(src, recs, fmt, style="pretty2", encoding=encoding, bom=bom, gz=gz)
    pf = prepare_data_file(src)
    assert isinstance(pf, PreparedFile)
    assert pf.compression == ("gzip" if gz else None)
    assert pf.encoding == (encoding + "-bom" if bom and encoding == "utf-8" else encoding)
    data = Path(pf.data_path).read_bytes()
    assert data[pf.bom_len:] == utf8
    assert pf.identity.matches(file_identity(src))
    assert pf.warnings == ()
    if encoding == "utf-8" and not gz:
        assert pf.data_path == fileio._abspath(src) and pf.bom_len == (3 if bom else 0)
        assert not pf.converted
    else:
        assert pf.bom_len == 0 and pf.converted
        assert Path(pf.data_path).parent == prepared_dir()
        assert not any(name.endswith(".part") for name in prepared_files())
    data_path, enc, comp = pf  # tuple form of docs/ARCHITECTURE.md §4
    assert (data_path, enc, comp) == (pf.data_path, pf.encoding, pf.compression)


def test_gzip_is_detected_by_magic_not_extension(tmp_path: Path) -> None:
    payload = b'[{"a":1}]'
    disguised = tmp_path / "data.json"
    disguised.write_bytes(gzip.compress(payload))
    pf = prepare_data_file(disguised)
    assert pf.compression == "gzip" and Path(pf.data_path).read_bytes() == payload
    plain = tmp_path / "plain.json.gz"
    plain.write_bytes(payload)
    pf = prepare_data_file(plain)
    assert pf.compression is None and pf.data_path == fileio._abspath(plain)


def test_empty_and_tiny_files(tmp_path: Path) -> None:
    empty = tmp_path / "empty.json"
    empty.write_bytes(b"")
    pf = prepare_data_file(empty)
    assert pf.encoding == "utf-8" and pf.compression is None and pf.identity.size == 0
    gz_empty = tmp_path / "empty.json.gz"
    gz_empty.write_bytes(gzip.compress(b""))
    pf = prepare_data_file(gz_empty)
    assert pf.compression == "gzip" and Path(pf.data_path).read_bytes() == b""
    one = tmp_path / "one.json"
    one.write_bytes("1".encode("utf-16-le"))
    pf = prepare_data_file(one)
    assert pf.encoding == "utf-16-le" and Path(pf.data_path).read_bytes() == b"1"


def test_large_utf16_crosses_chunks(tmp_path: Path) -> None:
    # odd chunk boundaries split code units and surrogate pairs
    text = "[" + ",".join(f'{{"i":{i},"s":"Тошкент 😀 {i}"}}' for i in range(40000)) + "]"
    src = tmp_path / "big16.json"
    src.write_bytes(text.encode("utf-16-be"))
    assert src.stat().st_size > 2 * fileio.CHUNK_BYTES
    pf = prepare_data_file(src)
    assert Path(pf.data_path).read_bytes() == text.encode("utf-8")


def test_prepared_file_is_reused(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    recs = jsongen.records(20, seed=5)
    src = tmp_path / "a.json.gz"
    jsongen.write_file(src, recs, "array", gz=True)
    pf1 = prepare_data_file(src)
    stamp = os.stat(pf1.data_path).st_mtime_ns

    def no_rebuild(*args, **kwargs):
        raise AssertionError("prepared file was rebuilt")

    monkeypatch.setattr(fileio, "_build_prepared", no_rebuild)
    progress = Progress()
    pf2 = prepare_data_file(src, progress=progress)
    assert pf2 == pf1 and os.stat(pf1.data_path).st_mtime_ns == stamp
    assert progress.total == src.stat().st_size and progress.done == progress.total
    monkeypatch.undo()

    # a changed source gets a new prepared file
    jsongen.write_file(src, recs[:3], "array", gz=True)
    os.utime(src, ns=(time.time_ns(), time.time_ns() + 10**9))
    pf3 = prepare_data_file(src)
    assert pf3.data_path != pf1.data_path
    assert Path(pf3.data_path).read_bytes() == jsongen.build_document(recs[:3], "array")

    # a damaged (wrong size) or meta-less prepared file is rebuilt, not reused
    Path(pf3.data_path).write_bytes(b"[")
    pf4 = prepare_data_file(src)
    assert Path(pf4.data_path).read_bytes() == jsongen.build_document(recs[:3], "array")
    Path(pf4.data_path).with_suffix(".meta").unlink()
    pf5 = prepare_data_file(src)
    assert Path(pf5.data_path).read_bytes() == jsongen.build_document(recs[:3], "array")
    assert Path(pf5.data_path).with_suffix(".meta").exists()


def test_concurrent_preparation_builds_once(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    src = tmp_path / "c.json"
    src.write_bytes(jsongen.build_document(jsongen.records(200, seed=9), "array").decode().encode("utf-16"))
    builds = []
    real_build = fileio._build_prepared

    def counting_build(*args, **kwargs):
        builds.append(1)
        time.sleep(0.05)
        return real_build(*args, **kwargs)

    monkeypatch.setattr(fileio, "_build_prepared", counting_build)
    results: list[PreparedFile] = []
    errors: list[BaseException] = []

    def run() -> None:
        try:
            results.append(prepare_data_file(src))
        except BaseException as exc:  # pragma: no cover - reported below
            errors.append(exc)

    threads = [threading.Thread(target=run) for _ in range(4)]
    for th in threads:
        th.start()
    for th in threads:
        th.join(30)
    assert not errors and len(results) == 4 and len(builds) == 1
    assert len({r.data_path for r in results}) == 1


def test_prepare_progress_and_cancel(tmp_path: Path) -> None:
    text = "[" + ",".join(f'{{"name":"Тошкент","i":{i}}}' for i in range(60000)) + "]"
    src = tmp_path / "p16.json"
    src.write_bytes(text.encode("utf-16"))
    seen = []
    progress = Progress(callback=seen.append, min_interval=0)
    pf = prepare_data_file(src, progress=progress)
    assert Path(pf.data_path).read_bytes() == text.encode("utf-8")
    assert progress.total == src.stat().st_size and progress.done == progress.total
    dones = [i.done for i in seen]
    assert dones == sorted(dones) and len(set(dones)) >= 3
    assert progress.message  # "UTF-16 dan UTF-8 ga o'girilmoqda…"

    # cancellation during the build: no partial file, no metadata
    src2 = tmp_path / "p16b.json.gz"
    src2.write_bytes(gzip.compress(text.encode("utf-16-le") * 2, compresslevel=1))
    before = prepared_files()
    token = CancelToken()

    def cancel_soon(info) -> None:
        if info.done > 0:
            token.cancel()

    with pytest.raises(Cancelled):
        prepare_data_file(src2, progress=Progress(callback=cancel_soon, min_interval=0), cancel=token)
    assert prepared_files() == before
    pre_cancelled = CancelToken()
    pre_cancelled.cancel()
    with pytest.raises(Cancelled):
        prepare_data_file(src2, cancel=pre_cancelled)
    assert prepared_files() == before


@pytest.mark.parametrize("encoding, bom", [("utf-32-le", True), ("utf-32-be", True), ("utf-32-le", False),
                                           ("utf-32-be", False)])
def test_utf32_is_rejected(tmp_path: Path, encoding: str, bom: bool, lang_en) -> None:
    raw = '[{"a": 1}]'.encode(encoding)
    if bom:
        raw = "\ufeff".encode(encoding) + raw
    for gz in (False, True):
        p = tmp_path / f"u32_{gz}.json"
        p.write_bytes(gzip.compress(raw) if gz else raw)
        with pytest.raises(FileFormatError) as info:
            prepare_data_file(p)
        exc = info.value
        assert exc.key == "fileio.error.utf32"
        assert "UTF-32" in str(exc) and str(p) in str(exc)
        assert exc.hint and "UTF-8" in exc.hint
    assert not any(name.endswith((".part", ".json")) for name in prepared_files())


def test_invalid_utf16_reports_offset(tmp_path: Path, lang_en) -> None:
    lone = tmp_path / "lone.json"
    lone.write_bytes('["a'.encode("utf-16-le") + b"\x00\xd8" + '"]'.encode("utf-16-le"))
    with pytest.raises(FileFormatError) as info:
        prepare_data_file(lone)
    assert info.value.key == "fileio.error.decode" and info.value.params["offset"] == 6
    assert "UTF-16 LE" in str(info.value)
    odd = tmp_path / "odd.json"
    odd.write_bytes("\ufeff[1]".encode("utf-16-be") + b"\x00")
    with pytest.raises(FileFormatError) as info:
        prepare_data_file(odd)
    assert info.value.params["offset"] == 8  # the dangling byte
    assert not any(name.endswith(".part") for name in prepared_files())


def test_gzip_variants(tmp_path: Path) -> None:
    a = jsongen.build_document(jsongen.records(30, seed=1), "jsonl")
    b = jsongen.build_document(jsongen.records(30, seed=2), "jsonl")

    multi = tmp_path / "multi.gz"
    multi.write_bytes(gzip.compress(a) + gzip.compress(b))
    pf = prepare_data_file(multi)
    assert Path(pf.data_path).read_bytes() == a + b and pf.warnings == ()

    padded = tmp_path / "padded.gz"
    padded.write_bytes(gzip.compress(a) + b"\x00" * 100)
    pf = prepare_data_file(padded)
    assert Path(pf.data_path).read_bytes() == a and pf.warnings == ()

    trailing = tmp_path / "trailing.gz"
    trailing.write_bytes(gzip.compress(a) + b"not gzip")
    pf = prepare_data_file(trailing)
    assert Path(pf.data_path).read_bytes() == a
    assert pf.warnings == ("fileio.warning.gzip_trailing",)

    big = jsongen.build_document(jsongen.records(3000, seed=4), "array")
    full = gzip.compress(big)
    truncated = tmp_path / "truncated.json.gz"
    truncated.write_bytes(full[: len(full) // 2])
    pf = prepare_data_file(truncated)
    data = Path(pf.data_path).read_bytes()
    assert 0 < len(data) < len(big) and big.startswith(data)
    assert pf.warnings == ("fileio.warning.gzip_truncated",)
    assert prepare_data_file(truncated).warnings == pf.warnings  # kept when reused

    bomb = tmp_path / "spaces.gz"  # compresses ~1000:1; output is produced in bounded steps
    bomb.write_bytes(gzip.compress(b"[" + b" " * (5 * fileio.CHUNK_BYTES) + b"]"))
    pf = prepare_data_file(bomb)
    assert os.path.getsize(pf.data_path) == 5 * fileio.CHUNK_BYTES + 2


def test_corrupt_gzip(tmp_path: Path, lang_en) -> None:
    good = gzip.compress(b'{"a": [1, 2, 3]}' * 100)
    bad_crc = bytearray(good)
    bad_crc[-8] ^= 0xFF  # CRC32 in the trailer
    garbage = good[:10] + bytes(range(256)) * 4
    for name, blob in (("crc.gz", bytes(bad_crc)), ("garbage.gz", garbage)):
        p = tmp_path / name
        p.write_bytes(blob)
        with pytest.raises(FileFormatError) as info:
            prepare_data_file(p)
        assert info.value.key == "fileio.error.gzip"
        assert "gzip" in str(info.value)
    assert not any(n.endswith(".part") for n in prepared_files())


def test_prune_prepared(tmp_path: Path) -> None:
    sources = []
    for i in range(3):
        src = tmp_path / f"s{i}.json.gz"
        src.write_bytes(gzip.compress(b"[" + b"1," * (1000 * (i + 1)) + b"1]"))
        sources.append(prepare_data_file(src))
    now = time.time()
    for age, pf in zip((300, 200, 100), sources):  # s0 least recently used
        meta = Path(pf.data_path).with_suffix(".meta")
        os.utime(meta, (now - age, now - age))
    sizes = [os.path.getsize(pf.data_path) for pf in sources]
    assert prune_prepared(sum(sizes), min_age=0) == 0  # within the limit
    assert prune_prepared(0, min_age=1000) == 0  # everything used too recently
    # least recently used first, only until the total fits
    assert prune_prepared(sizes[1] + sizes[2], min_age=0) == sizes[0]
    assert not Path(sources[0].data_path).exists() and not Path(sources[0].data_path).with_suffix(".meta").exists()
    assert Path(sources[1].data_path).exists() and Path(sources[2].data_path).exists()
    # files in `keep` are skipped, the next one goes instead
    assert prune_prepared(0, min_age=0, keep=[sources[1].data_path]) == sizes[2]
    assert Path(sources[1].data_path).exists() and not Path(sources[2].data_path).exists()
    assert prune_prepared(0, min_age=0) == sizes[1] and not prepared_files()
    # leftovers of crashed builds
    stale = prepared_dir() / "dead.123.abcd.part"
    stale.write_bytes(b"x")
    os.utime(stale, (now - 3 * 86400, now - 3 * 86400))
    fresh = prepared_dir() / "live.123.abcd.part"
    fresh.write_bytes(b"x")
    orphan = prepared_dir() / "orphan.json"
    orphan.write_bytes(b"[]")
    os.utime(orphan, (now - 7200, now - 7200))
    prune_prepared(10**12)
    assert not stale.exists() and fresh.exists() and not orphan.exists()


def test_prune_skips_prepared_files_in_use(tmp_path: Path) -> None:
    src = tmp_path / "s.json.gz"
    src.write_bytes(gzip.compress(b"[" + b"1," * 1000 + b"1]"))
    pf = prepare_data_file(src)
    meta = Path(pf.data_path).with_suffix(".meta")
    old = time.time() - 10_000
    os.utime(meta, (old, old))
    fileio.retain_prepared(pf.data_path)
    try:
        assert prune_prepared(0, min_age=0) == 0  # held by an open document
        assert Path(pf.data_path).exists()
        fileio.retain_prepared(pf.data_path)  # reference counted
        fileio.release_prepared(pf.data_path)
        assert prune_prepared(0, min_age=0) == 0
    finally:
        fileio.release_prepared(pf.data_path)
    os.utime(meta, (old, old))  # retain refreshed it; make it old again
    assert prune_prepared(0, min_age=0) > 0
    assert not Path(pf.data_path).exists()


def test_open_document_retains_prepared_copy(tmp_path: Path) -> None:
    from jsonreshape.reader import Document

    src = tmp_path / "d.json.gz"
    src.write_bytes(gzip.compress(b'[{"a": 1}, {"a": 2}]'))
    doc = Document.open(src)
    try:
        data = Path(doc.data_path)
        old = time.time() - 10_000
        os.utime(data.with_suffix(".meta"), (old, old))
        assert prune_prepared(0, min_age=0) == 0
        doc.build_index()
        assert doc.record(1) == {"a": 2}
    finally:
        doc.close()
    os.utime(data.with_suffix(".meta"), (old, old))
    assert prune_prepared(0, min_age=0) > 0


# ----------------------------------------------------------------------------------
# atomic writes
# ----------------------------------------------------------------------------------
def test_atomic_write_replaces_on_success(tmp_path: Path) -> None:
    p = tmp_path / "out.json"
    p.write_bytes(b"old")
    with atomic_write(p) as f:
        f.write(b"new content")
        assert p.read_bytes() == b"old"  # untouched until the block completes
    assert p.read_bytes() == b"new content"
    assert os.listdir(tmp_path) == ["out.json"]
    with atomic_write(str(p), "w", encoding="utf-8", newline="\n") as f:
        f.write("Тошкент\n")
    assert p.read_bytes() == "Тошкент\n".encode("utf-8")
    new = tmp_path / "created.json"
    with atomic_write(new, fsync=False) as f:
        f.write(b"[]")
    assert new.read_bytes() == b"[]"
    with pytest.raises(ValueError):
        with atomic_write(p, "ab"):
            pass


@pytest.mark.parametrize("exc_type", [RuntimeError, Cancelled, KeyboardInterrupt])
def test_atomic_write_leaves_no_temp_file_on_error(tmp_path: Path, exc_type: type[BaseException]) -> None:
    p = tmp_path / "out.json"
    p.write_bytes(b"old")
    with pytest.raises(exc_type):
        with atomic_write(p) as f:
            f.write(b"partial" * 1000)
            raise exc_type()
    assert p.read_bytes() == b"old"
    assert os.listdir(tmp_path) == ["out.json"]
    q = tmp_path / "never.json"
    with pytest.raises(exc_type):
        with atomic_write(q) as f:
            f.write(b"x")
            raise exc_type()
    assert not q.exists() and os.listdir(tmp_path) == ["out.json"]


def test_atomic_write_into_missing_directory(tmp_path: Path) -> None:
    with pytest.raises(UserInputError) as info:
        with atomic_write(tmp_path / "missing_dir" / "out.json") as f:
            f.write(b"x")
    assert info.value.key == "error.file_not_found"


@windows_only
def test_atomic_write_target_held_by_another_program(tmp_path: Path, lang_en) -> None:
    p = tmp_path / "report.xlsx"
    p.write_bytes(b"old")
    handle = win_open(p, 0x1)  # e.g. Excel: FILE_SHARE_READ only, no delete/rename
    try:
        with pytest.raises(JsonReshapeError) as info:
            with atomic_write(p) as f:
                f.write(b"new")
        assert info.value.key == "fileio.error.replace"
        assert "another program" in str(info.value) and info.value.hint
    finally:
        _CloseHandle(handle)
    assert p.read_bytes() == b"old" and os.listdir(tmp_path) == ["report.xlsx"]


def test_atomic_path_for_libraries_needing_a_filename(tmp_path: Path) -> None:
    db = tmp_path / "out.sqlite"
    with atomic_path(db) as tmp:
        assert tmp.parent == tmp_path and tmp != db
        con = sqlite3.connect(tmp)
        con.execute("create table t (x)")
        con.execute("insert into t values (1)")
        con.commit()
        con.close()
        assert not db.exists()
    con = sqlite3.connect(db)
    assert con.execute("select x from t").fetchall() == [(1,)]
    con.close()
    with pytest.raises(RuntimeError):
        with atomic_path(tmp_path / "other.sqlite") as tmp:
            tmp.write_bytes(b"junk")
            raise RuntimeError
    assert sorted(os.listdir(tmp_path)) == ["out.sqlite"]


# ----------------------------------------------------------------------------------
# read_range
# ----------------------------------------------------------------------------------
class _Trickle(io.RawIOBase):
    """Raw stream returning at most 3 bytes per read (short reads)."""

    def __init__(self, data: bytes) -> None:
        self._data = data
        self._pos = 0
        self.name = "trickle.json"

    def readable(self) -> bool:
        return True

    def seekable(self) -> bool:
        return True

    def seek(self, pos: int, whence: int = 0) -> int:
        self._pos = pos
        return pos

    def readinto(self, b) -> int:  # noqa: ANN001
        chunk = self._data[self._pos:self._pos + min(3, len(b))]
        b[: len(chunk)] = chunk
        self._pos += len(chunk)
        return len(chunk)


def test_read_range(tmp_path: Path) -> None:
    data = bytes(range(256)) * 10
    p = tmp_path / "r.bin"
    p.write_bytes(data)
    with open_shared(p) as f:
        assert read_range(f, 0, 0) == b""
        assert read_range(f, 10, 20) == data[10:20]
        assert read_range(f, 0, len(data)) == data
        assert read_range(f, 2550, 2560) == data[2550:2560]
        with pytest.raises(FileChangedError) as info:
            read_range(f, 2555, 2570)  # the file is shorter than the index says
        assert info.value.key == "error.file_changed" and "r.bin" in info.value.params["path"]
        with pytest.raises(ValueError):
            read_range(f, 5, 4)
        with pytest.raises(ValueError):
            read_range(f, -1, 4)
    raw = _Trickle(data)
    assert read_range(raw, 100, 200) == data[100:200]
    with pytest.raises(FileChangedError):
        read_range(raw, 2500, 2600)
