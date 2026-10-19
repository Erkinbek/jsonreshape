r"""Edit benchmark: AGENT_PROMPT §12 "export with edits >= 50 MB/s, RAM < 300 MB" and the stage-3 operations
(docs/ARCHITECTURE.md §12) on a big record array.

Scenarios (each one an edit layer of its own, made once per run in a fresh ``JSONRESHAPE_HOME``):

* ``base``    -- no edits (the reference: the same operations on the plain file);
* ``point``   -- 100 point edits (``set_value`` of ``name`` in random records);
* ``third``   -- a bulk ``set_field`` on every 3rd record (``scope = range(0, n, 3)``);
* ``all``     -- a bulk ``set_field`` on every record;
* ``dels``    -- 10 % of the records deleted (every 10th) + 1 000 records inserted (100 pastes of 10 records at
  random positions).

Operations, each in a FRESH process (best of ``--repeat`` runs; memory = the largest peak over the runs):

* ``make``          -- building the scenario (the bulk previews of ``third`` / ``all`` are timed here);
* ``restore``       -- ``Document.open`` (cached index) + ``EditSession.open(restore=True)``;
* ``export_json``   -- ``writer.export`` JSON, indent 2; ``export_jsonl``; ``export_csv`` (9 scalar columns);
* ``export_csv_auto`` -- CSV with automatic columns (the column scan + the rows); ``export_xlsx`` (the 9 scalar
  columns) and ``export_sqlite`` -- of the scenarios ``base`` and ``all`` only (minutes each: XLSX is bounded
  by xlsxwriter, README decision 39);
* ``save_as``       -- ``saver.save(doc, target, backup=False)``;
* ``patch_export``  -- ``jsonpatch.export_patch`` (the net effect against the original file);
* ``patch_import``  -- the exported patch of a scenario applied to the plain file (a fresh session);
* ``undo_redo``     -- undo and redo of the scenario's last step;
* ``bulk_<kind>``   -- the preview of every bulk operation kind over ALL records of the plain file (discarded).

Measurement: the time of the operation itself (the open / restore before it is timed separately); throughput =
the effective bytes of the records (the edited document) / time; ``fsync`` time is measured on its own (the
exports and the save end with an ``fsync`` of the output: on this machine's drive that is a large share), and the
throughput without it is reported too. Memory: a thread of the child samples every 20 ms the working set (RSS)
and the private bytes of the process AND its child processes (worker processes count); the peak of the
operation's phase is reported, plus the process' own ``peak_wset``. Before every run the machine must be
quiet (system CPU load <= ``--quiet-load`` percent, waiting at most ``--quiet-timeout`` s); the load before and
during the run is recorded (the machine is shared).

Usage (from the project root)::

    .venv/Scripts/python.exe bench/edit_bench.py --work DIR                       # everything (~1 hour)
    .venv/Scripts/python.exe bench/edit_bench.py --work DIR --only export_json:all,bulk_set_field
    .venv/Scripts/python.exe bench/edit_bench.py --work DIR --pkg OTHER_DIR       # another copy of jsonreshape
    .venv/Scripts/python.exe bench/edit_bench.py --report RESULTS.json [--baseline BEFORE.json]  # markdown only
    .venv/Scripts/python.exe bench/edit_bench.py --report RESULTS.json --export-report [--baseline BEFORE.json]

``--work`` holds the homes, layers and outputs (several GB: the ``all`` layer is as big as the file); outputs are
deleted after every run, the work folder at the end unless ``--keep-work``. Results: ``<results-dir>/edit-<ts>.json``
and ``<results-dir>/edit_latest.md`` (Uzbek by default, ``--lang en``), with a "before" column from ``--baseline``;
``--export-report`` writes ``<results-dir>/export_latest.md`` instead: the table exports (CSV, XLSX, SQLite) of
every scenario. ``--workers N`` asks the exports for N worker processes (``writer.export(..., workers=N)``, when
the measured package has that parameter; default: automatic, as in the program).
"""

from __future__ import annotations

import argparse
import json
import os
import platform
import random
import shutil
import subprocess
import sys
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Sequence

BENCH = Path(__file__).resolve().parent
ROOT = BENCH.parent
DEFAULT_DATA = BENCH / "data" / "syn_2g.json"
MARK = "@@EDIT_BENCH@@ "

SCENARIOS = ("base", "point", "third", "all", "dels")
EXPORT_OPS = ("export_json", "export_jsonl", "export_csv", "save_as")
TABLE_OPS = ("export_csv_auto", "export_xlsx", "export_sqlite")
TABLE_SLOW_OPS = ("export_xlsx", "export_sqlite")      # minutes each: only for TABLE_SLOW_SCENARIOS
TABLE_SLOW_SCENARIOS = ("base", "all")
BULK_KINDS = ("set_field", "replace", "rename_key", "delete_field", "clean_strings", "convert_type", "dedupe")
SCALAR_COLUMNS = ["id", "name", "active", "score", "created_at", "contact.phone", "contact.address.city",
                  "contact.address.street", "contact.address.zip"]
# the bulk operation of every kind, over all records of the file (syn_2g.json shapes: every record has
# "active", "tags", "score" and a phone "+998...")
BULK_SPECS: dict[str, dict[str, Any]] = {
    "set_field": {"path": "country", "value": '"UZ"'},
    "replace": {"find": "+998", "replacement": "998"},
    "rename_key": {"old": "active", "new": "is_active"},
    "delete_field": {"path": "tags"},
    "clean_strings": {"trim": True, "collapse_spaces": True},
    "convert_type": {"path": "score", "to": "string"},
    "dedupe": {"key": "id"},
}
MB = 1e6
CHILD_REFRESH = 0.5        # seconds between refreshes of the sampler's list of child processes


# ======================================================================================================
# child side: one operation in this process
# ======================================================================================================
class _Sampler:
    """Peak RSS (working set) and private bytes of this process plus its child processes, per phase.

    The list of child processes is refreshed every :data:`CHILD_REFRESH` seconds (``children(recursive=True)``
    walks every process of the system: ~11 ms with ~400 processes, holding the GIL -- every 20 ms it took a
    third of the measured operation's thread); the memory of the known ones is sampled every 20 ms."""

    def __init__(self) -> None:
        import psutil

        self.psutil = psutil
        self.proc = psutil.Process()
        self.lock = threading.Lock()
        self.phase = "init"
        self.peaks: dict[str, list[int]] = {}
        self.children: dict[int, Any] = {}
        self.kids: list[Any] = []
        self.kids_at = 0.0
        threading.Thread(target=self._loop, daemon=True, name="edit-bench-sampler").start()

    def _measure(self, refresh: bool = False) -> tuple[int, int, int]:
        mi = self.proc.memory_info()
        rss, prv = int(mi.rss), int(getattr(mi, "private", 0) or mi.vms)
        nkids = 0
        now = time.monotonic()
        if refresh or now - self.kids_at >= CHILD_REFRESH:
            self.kids_at = now
            try:
                self.kids = self.proc.children(recursive=True)
            except Exception:  # noqa: BLE001 - sampling must never fail
                self.kids = []
        kids = self.kids
        for k in kids:
            try:
                p = self.children.get(k.pid)
                if p is None:
                    p = self.children[k.pid] = k
                m = p.memory_info()
                rss += int(m.rss)
                prv += int(getattr(m, "private", 0) or m.vms)
                nkids += 1
            except Exception:  # noqa: BLE001 - a worker that just exited
                continue
        return rss, prv, nkids

    def _loop(self) -> None:
        while True:
            try:
                rss, prv, nk = self._measure()
            except Exception:  # noqa: BLE001
                rss = prv = nk = 0
            with self.lock:
                cur = self.peaks.setdefault(self.phase, [0, 0, 0])
                cur[0] = max(cur[0], rss)
                cur[1] = max(cur[1], prv)
                cur[2] = max(cur[2], nk)
            time.sleep(0.02)

    def start(self, name: str) -> None:
        rss, prv, nk = self._measure(refresh=True)
        with self.lock:
            self.phase = name
            self.peaks[name] = [rss, prv, nk]

    def stop(self) -> dict[str, Any]:
        rss, prv, nk = self._measure(refresh=True)
        with self.lock:
            pk = self.peaks.get(self.phase, [0, 0, 0])
            pk[0], pk[1], pk[2] = max(pk[0], rss), max(pk[1], prv), max(pk[2], nk)
            name = self.phase
            self.phase = "idle"
        return {"phase": name, "rss_peak_mb": round(pk[0] / MB, 1), "private_peak_mb": round(pk[1] / MB, 1),
                "workers": pk[2]}


class _Child:
    def __init__(self, args: argparse.Namespace) -> None:
        self.args = args
        self.data = Path(args.data)
        self.work = Path(args.work)
        self.sampler = _Sampler()
        self.fsync_s = 0.0
        orig = os.fsync

        def timed_fsync(fd: int) -> None:
            t0 = time.perf_counter()
            try:
                orig(fd)
            finally:
                self.fsync_s += time.perf_counter() - t0

        os.fsync = timed_fsync          # every fsync of the program (outputs, layer segments) is timed
        self.out: dict[str, Any] = {"op": args.op, "scen": args.scen, "phases": {}}

    def phase(self, name: str):
        child = self

        class _P:
            def __enter__(self_inner) -> dict[str, Any]:
                child.sampler.start(name)
                child._fs0 = child.fsync_s
                self_inner.t0 = time.perf_counter()
                self_inner.info = {}
                return self_inner.info

            def __exit__(self_inner, *exc: Any) -> None:
                dt = time.perf_counter() - self_inner.t0
                mem = child.sampler.stop()
                info = self_inner.info
                info.update(mem)
                info["seconds"] = round(dt, 3)
                info["fsync_s"] = round(child.fsync_s - child._fs0, 3)
                child.out["phases"][name] = info

        return _P()

    # -- helpers ------------------------------------------------------------------------------------
    def open_doc(self) -> Any:
        from jsonreshape.reader import Document

        with self.phase("open") as info:
            doc = Document.open(self.data)
            doc.build_index()
            info.update(records=doc.record_count, from_cache=bool(doc.index_from_cache))
        return doc

    def open_session(self, doc: Any, restore: bool = True) -> Any:
        from jsonreshape.edits import EditSession

        with self.phase("restore") as info:
            s = EditSession.open(doc, restore=restore)
            st = s.state
            info.update(head=st.version, count=st.count, layers=len(st.layers), pieces=st.pieces.npieces)
        self.out["effective_bytes"] = int(doc.effective_bytes()) if doc.edited else int(doc.data_size)
        return s

    def fresh_session(self, doc: Any) -> Any:
        """A session without edits (an existing layer is discarded first)."""
        from jsonreshape.edits import EditSession

        s = EditSession.open(doc, restore=True)
        if s.dirty or s.state.version:
            s.discard()
        return s

    def rm(self, p: Path) -> None:
        for q in (p, *p.parent.glob(p.name + ".*")):
            try:
                os.remove(q)
            except OSError:
                pass

    # -- operations ---------------------------------------------------------------------------------
    def run(self) -> dict[str, Any]:
        op = self.args.op
        fn = getattr(self, "op_" + op.split(":")[0], None)
        if fn is None:
            raise SystemExit(f"unknown op {op}")
        fn()
        try:
            mi = self.sampler.proc.memory_info()
            self.out["process_peak_wset_mb"] = round(getattr(mi, "peak_wset", 0) / MB, 1)
            self.out["process_peak_pagefile_mb"] = round(getattr(mi, "peak_pagefile", 0) / MB, 1)
        except Exception:  # noqa: BLE001
            pass
        from jsonreshape import core

        self.out["backend"] = core.BACKEND
        return self.out

    def op_make(self) -> None:
        from jsonreshape.bulk import BulkSpec
        from jsonreshape.edits import Ref

        scen = self.args.scen
        doc = self.open_doc()
        s = self.fresh_session(doc)
        n = doc.record_count
        rng = random.Random(1)
        if scen == "base":
            pass
        elif scen == "point":
            with self.phase("edits") as info:
                for i in range(100):
                    s.set_value(Ref(rng.randrange(n), ("name",)), json.dumps(f"Tahrir {i}"))
                info["steps"] = s.state.version
        elif scen in ("third", "all"):
            spec = BulkSpec("set_field", {"path": "edited", "value": "true"}) if scen == "third" else \
                BulkSpec("set_field", {"path": "country", "value": '"UZ"'})
            scope = range(0, n, 3) if scen == "third" else None
            with self.phase("bulk_preview") as info:
                pv = s.bulk_preview(spec, scope)
                info.update(scanned=pv.scanned, records_changed=pv.records_changed, bytes_new=pv.bytes_new,
                            bytes=int(doc.data_size))
            with self.phase("bulk_apply"):
                s.bulk_apply(pv)
        elif scen == "dels":
            with self.phase("delete_records") as info:
                s.delete_records(range(0, n, 10))
                info["count"] = s.state.count
            with self.phase("inserts") as info:
                for j in range(100):
                    recs = [{"id": -(10 * j + k) - 1, "name": f"Yangi yozuv {10 * j + k}", "active": False,
                             "score": 0.1, "created_at": 1700000000, "tags": ["yangi"]} for k in range(10)]
                    s.paste(None, json.dumps(recs, ensure_ascii=False), index=rng.randrange(s.state.count + 1),
                            as_records=True)
                info.update(count=s.state.count, steps=s.state.version)
        else:
            raise SystemExit(f"unknown scenario {scen}")
        self.out["effective_bytes"] = int(doc.effective_bytes()) if doc.edited else int(doc.data_size)
        layer = s.store.dir if s.store is not None else None
        if layer is not None:
            self.out["layer_mb"] = round(sum(f.stat().st_size for f in Path(layer).iterdir() if f.is_file()) / MB, 1)
        s.close()                         # keeps the layer for the other operations
        doc.close()

    def op_restore(self) -> None:
        doc = self.open_doc()
        s = self.open_session(doc)
        s.close()
        doc.close()

    def _export(self, fmt: str) -> None:
        import inspect

        from jsonreshape.writer import ExportOptions, export

        doc = self.open_doc()
        s = self.open_session(doc)
        ext = {"csv_auto": "csv"}.get(fmt, fmt)
        out = self.work / "out" / f"{self.args.scen}_{fmt}.{ext}"
        out.parent.mkdir(parents=True, exist_ok=True)
        if fmt == "json":
            opts = ExportOptions("json", indent=2)
        elif fmt == "jsonl":
            opts = ExportOptions("jsonl")
        elif fmt == "csv_auto":
            opts = ExportOptions("csv")
        elif fmt == "xlsx":
            opts = ExportOptions("xlsx", columns=list(SCALAR_COLUMNS))
        elif fmt == "sqlite":
            opts = ExportOptions("sqlite")
        else:
            opts = ExportOptions("csv", columns=list(SCALAR_COLUMNS))
        kw: dict[str, Any] = {}
        if self.args.workers and "workers" in inspect.signature(export).parameters:
            kw["workers"] = int(self.args.workers)
        files: list[Path] = [out]
        try:
            with self.phase("op") as info:
                rep = export(doc, out, opts, **kw)
                files = list(rep.files) or [out]
                info.update(bytes=self.out["effective_bytes"], records=rep.records, rows=rep.rows,
                            written=rep.bytes_written, columns=len(rep.columns), files=len(rep.files),
                            warnings=list(getattr(rep, "warning_keys", [])))
                used = getattr(rep, "workers", None)
                if used is not None:
                    info["workers_used"] = int(used)
        finally:
            for p in files:
                self.rm(Path(p))
        s.close()
        doc.close()

    def op_export_json(self) -> None:
        self._export("json")

    def op_export_jsonl(self) -> None:
        self._export("jsonl")

    def op_export_csv(self) -> None:
        self._export("csv")

    def op_export_csv_auto(self) -> None:
        self._export("csv_auto")

    def op_export_xlsx(self) -> None:
        self._export("xlsx")

    def op_export_sqlite(self) -> None:
        self._export("sqlite")

    def op_save_as(self) -> None:
        from jsonreshape import saver

        doc = self.open_doc()
        s = self.open_session(doc)
        out = self.work / "out" / f"{self.args.scen}_saved.json"
        out.parent.mkdir(parents=True, exist_ok=True)
        try:
            with self.phase("op") as info:
                rep = saver.save(doc.pinned(), out, backup=False)
                info.update(bytes=self.out["effective_bytes"], records=rep.records, written=rep.bytes_written,
                            copied=rep.copied_bytes, edited_records=rep.edited_records)
        finally:
            self.rm(out)
        s.close()
        doc.close()

    def op_patch_export(self) -> None:
        doc = self.open_doc()
        s = self.open_session(doc)
        out = Path(self.args.keep) if self.args.keep else self.work / "out" / f"{self.args.scen}_patch.json"
        out.parent.mkdir(parents=True, exist_ok=True)
        try:
            with self.phase("op") as info:
                nops = s.export_patch(out)
                info.update(ops=nops, written=out.stat().st_size, bytes=self.out["effective_bytes"])
        finally:
            if not self.args.keep:
                self.rm(out)
        s.close()
        doc.close()

    def op_patch_import(self) -> None:
        doc = self.open_doc()
        s = self.fresh_session(doc)
        patch = Path(self.args.patch)
        with self.phase("op") as info:
            cs = s.import_patch(patch)
            info.update(bytes=patch.stat().st_size, count=cs.state.count, kind=cs.kind)
        with self.phase("discard"):
            s.discard()
        s.close()
        doc.close()

    def op_undo_redo(self) -> None:
        doc = self.open_doc()
        s = self.open_session(doc)
        with self.phase("undo") as info:
            cs = s.undo()
            info["kind"] = None if cs is None else cs.kind
        with self.phase("redo") as info:
            cs = s.redo()
            info["kind"] = None if cs is None else cs.kind
        s.close()
        doc.close()

    def op_bulk(self) -> None:
        from jsonreshape.bulk import BulkSpec

        kind = self.args.op.split(":", 1)[1]
        doc = self.open_doc()
        s = self.fresh_session(doc)
        spec = BulkSpec(kind, dict(BULK_SPECS[kind]))
        with self.phase("op") as info:
            pv = s.bulk_preview(spec, None)
            info.update(bytes=int(doc.data_size), scanned=pv.scanned, records_changed=pv.records_changed,
                        values_changed=pv.values_changed, bytes_new=pv.bytes_new, conflicts=pv.conflicts,
                        failures=pv.failures)
        pv.discard()
        s.close()
        doc.close()


def child_main(args: argparse.Namespace) -> None:
    pkg = args.pkg or str(ROOT)
    sys.path.insert(0, pkg)
    os.environ["JSONRESHAPE_HOME"] = str(Path(args.work) / "homes" / args.home)
    import jsonreshape

    if Path(jsonreshape.__file__).resolve().parent.parent != Path(pkg).resolve():
        raise SystemExit(f"jsonreshape imported from {jsonreshape.__file__}, not from {pkg}")
    # This script runs its work behind ``if __name__ == "__main__"`` like the program's own entry points
    # (``python -m jsonreshape``, the console scripts), so spawned worker processes are safe: let the bulk
    # preview and the JSON Patch export (which asks ``bulk._spawn_safe`` too) choose their worker processes as
    # they do in the program (a plain script is otherwise refused).
    from jsonreshape import bulk as _bulk

    if hasattr(_bulk, "_spawn_safe"):
        _bulk._spawn_safe = lambda: True
    res = _Child(args).run()
    sys.stdout.write(MARK + json.dumps(res, default=str) + "\n")
    sys.stdout.flush()


# ======================================================================================================
# runner side
# ======================================================================================================
def _wait_quiet(limit: float, timeout: float) -> float:
    import psutil

    deadline = time.monotonic() + timeout
    load = psutil.cpu_percent(interval=1.0)
    while load > limit and time.monotonic() < deadline:
        load = psutil.cpu_percent(interval=1.0)
    return load


class _LoadWatch:
    """System CPU load during a run (other processes' share is what matters: the machine is shared)."""

    def __init__(self) -> None:
        import psutil

        self.psutil = psutil
        self.samples: list[float] = []
        self.stop = threading.Event()
        self.t = threading.Thread(target=self._loop, daemon=True)
        psutil.cpu_percent(interval=None)
        self.t.start()

    def _loop(self) -> None:
        while not self.stop.wait(1.0):
            self.samples.append(self.psutil.cpu_percent(interval=None))

    def finish(self) -> dict[str, float]:
        self.stop.set()
        self.t.join(2)
        s = self.samples or [0.0]
        return {"load_mean": round(sum(s) / len(s), 1), "load_max": round(max(s), 1)}


def run_child(args: argparse.Namespace, op: str, scen: str, home: str, **extra: str) -> dict[str, Any]:
    cmd = [sys.executable, str(Path(__file__).resolve()), "--child", "--op", op, "--scen", scen, "--home", home,
           "--data", str(args.data), "--work", str(args.work)]
    if args.pkg:
        cmd += ["--pkg", str(args.pkg)]
    if args.workers:
        cmd += ["--workers", str(args.workers)]
    for k, v in extra.items():
        cmd += [f"--{k}", str(v)]
    load0 = _wait_quiet(args.quiet_load, args.quiet_timeout)
    watch = _LoadWatch()
    t0 = time.perf_counter()
    proc = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace")
    wall = time.perf_counter() - t0
    load = watch.finish()
    res: dict[str, Any] | None = None
    for line in proc.stdout.splitlines():
        if line.startswith(MARK):
            res = json.loads(line[len(MARK):])
    if res is None:
        res = {"op": op, "scen": scen, "error": (proc.stderr or proc.stdout)[-3000:], "returncode": proc.returncode}
    res.update(wall=round(wall, 2), load_before=round(load0, 1), **load)
    return res


def _summary(res: dict[str, Any]) -> str:
    if "error" in res:
        return f"ERROR {res['error'][-400:]}"
    parts = []
    for name, ph in res.get("phases", {}).items():
        s = f"{name} {ph['seconds']:.2f}s"
        b = ph.get("bytes")
        if b and ph["seconds"] > 0:
            s += f" {b / MB / ph['seconds']:.1f}MB/s"
            if ph.get("fsync_s"):
                s += f" (excl fsync {ph['fsync_s']:.1f}s: {b / MB / max(1e-9, ph['seconds'] - ph['fsync_s']):.1f}MB/s)"
        s += f" rss {ph['rss_peak_mb']} prv {ph['private_peak_mb']}"
        if ph.get("workers"):
            s += f" w{ph['workers']}"
        if ph.get("workers_used"):
            s += f" used{ph['workers_used']}"
        parts.append(s)
    return "; ".join(parts) + f" | wall {res['wall']}s load {res['load_before']}/{res['load_mean']}/{res['load_max']}"


def plan(args: argparse.Namespace) -> list[tuple[str, str]]:
    """``(op, scenario)`` in run order (``make`` first; patch imports after their exports)."""
    out: list[tuple[str, str]] = []
    for scen in SCENARIOS:
        out.append(("make", scen))
    for scen in SCENARIOS:
        out.append(("restore", scen))
        for op in EXPORT_OPS:
            out.append((op, scen))
        for op in TABLE_OPS:
            if op not in TABLE_SLOW_OPS or scen in TABLE_SLOW_SCENARIOS:
                out.append((op, scen))
        if scen != "base":
            out.append(("patch_export", scen))
            out.append(("undo_redo", scen))
    for scen in ("third", "all", "dels"):
        out.append(("patch_import", scen))
    for kind in BULK_KINDS:
        out.append((f"bulk:{kind}", "base"))
    return out


def _selected(op: str, scen: str, only: list[str] | None) -> bool:
    if not only:
        return True
    name = op.replace("bulk:", "bulk_")
    for o in only:
        if ":" in o and not o.startswith("bulk"):
            a, b = o.split(":", 1)
            if a == name and b == scen:
                return True
        elif o == name or o == scen or (o == "make" and op == "make"):
            return True
    return False


def runner_main(args: argparse.Namespace) -> None:
    args.work = Path(args.work).resolve()
    args.work.mkdir(parents=True, exist_ok=True)
    only = [x.strip() for x in args.only.split(",")] if args.only else None
    results_path = Path(args.results_dir) / f"edit-{datetime.now():%Y%m%d-%H%M%S}.json"
    if args.label:
        results_path = results_path.with_name(results_path.stem + f"-{args.label}.json")
    started = time.time()
    runs: list[dict[str, Any]] = []
    patches = args.work / "patches"
    patches.mkdir(exist_ok=True)
    steps = plan(args)
    made: set[str] = set()
    for i, (op, scen) in enumerate(steps, 1):
        if op == "make":
            home = args.work / "homes" / scen
            exists = home.exists() and any((home / "edits").glob("*")) if scen != "base" else home.exists()
            needed = _selected(op, scen, only) or (not exists and any(
                _selected(o, s, only) for o, s in steps if s == scen and o != "make"))
            if not needed:
                continue
            reps = 1
        else:
            if not _selected(op, scen, only):
                continue
            reps = args.repeat
        home = scen if not op.startswith("patch_import") else "patchimp"
        if op.startswith("bulk:"):
            home = "bulk"
        extra: dict[str, str] = {}
        if op == "patch_export":
            extra["keep"] = str(patches / f"{scen}.json")
        if op == "patch_import":
            p = patches / f"{scen}.json"
            if not p.exists():
                r = run_child(args, "patch_export", scen, scen, keep=str(p))
                print(f"[{i}/{len(steps)}] (patch for import) {_summary(r)}", flush=True)
            extra["patch"] = str(p)
        for k in range(reps):
            res = run_child(args, op, scen, home, **extra)
            res["repeat"] = k + 1
            runs.append(res)
            print(f"[{i}/{len(steps)}] {op} {scen} {k + 1}/{reps}: {_summary(res)}", flush=True)
            results_path.parent.mkdir(parents=True, exist_ok=True)
            payload = {"started": started, "args": {k2: str(v) for k2, v in vars(args).items()},
                       "machine": machine_info(), "runs": runs}
            results_path.write_text(json.dumps(payload, indent=1), encoding="utf-8")
            op_s = res.get("phases", {}).get("op", {}).get("seconds", 0.0)
            if op_s > args.max_repeat_seconds:
                break                       # a very slow operation is measured once (said in the report)
        if op == "make":
            made.add(scen)
    print("results:", results_path, flush=True)
    if not args.keep_work:
        shutil.rmtree(args.work / "out", ignore_errors=True)
    if runs and not args.only:
        md = write_report(results_path, Path(args.baseline) if args.baseline else None, args.lang,
                          Path(args.md) if args.md else None)
        print("report:", md, flush=True)


def machine_info() -> dict[str, Any]:
    import psutil

    info: dict[str, Any] = {"python": sys.version.split()[0], "platform": platform.platform(),
                            "cpus": os.cpu_count(), "ram_gb": round(psutil.virtual_memory().total / 1e9, 1)}
    try:
        info["processor"] = platform.processor()
    except Exception:  # noqa: BLE001
        pass
    return info


# ======================================================================================================
# the report (bench/results/edit_latest.md): Uzbek by default, English with --lang en
# ======================================================================================================
# The texts belong to this developer tool, not to the program: they are registered into i18n.MESSAGES when the
# report is written (like bench/run_bench.py); a key that jsonreshape/i18n.py defines itself wins.
EDIT_BENCH_MESSAGES: dict[str, dict[str, str]] = {
    "eb.title": {"uz": "# jsonreshape: tahrirlar benchmarki (AGENT_PROMPT §12, 3-bosqich)",
                 "en": "# jsonreshape: editing benchmark (AGENT_PROMPT §12, stage 3)"},
    "eb.head": {"uz": "Sana: {date} · fayl: `{data}` ({size}, {records} ta yozuv) · to'liq natijalar: `{json}`{before}",
                "en": "Date: {date} · file: `{data}` ({size}, {records} records) · full results: `{json}`{before}"},
    "eb.head.before": {"uz": " · oldingi o'lchov: `{json}`", "en": " · earlier run: `{json}`"},
    "eb.sec.main": {"uz": "## §12 maqsadi: tahrirlar bilan eksport va saqlash",
                    "en": "## §12 target: export and save with edits"},
    "eb.sec.extra": {"uz": "## Tahrirlash amallari (qo'shimcha o'lchovlar)",
                     "en": "## Editing operations (extra measurements)"},
    "eb.sec.misses": {"uz": "## Maqsadga yetilmagan joylar", "en": "## Targets not met"},
    "eb.sec.machine": {"uz": "## Kompyuter", "en": "## Machine"},
    "eb.sec.conditions": {"uz": "## O'lchash shartlari", "en": "## Measurement conditions"},
    "eb.sec.unmeasured": {"uz": "## Nimani o'lchab bo'lmadi", "en": "## What could not be measured"},
    "eb.col": {"uz": "| Amal | Maqsad | Oldin | Keyin | Holat |",
               "en": "| Operation | Target | Before | After | Status |"},
    "eb.col1": {"uz": "| Amal | Maqsad | Natija | Holat |", "en": "| Operation | Target | Result | Status |"},
    "eb.legend": {
        "uz": "✅ maqsadga yetildi · ❌ yetilmadi (sababi va variantlari pastda) · — maqsad belgilanmagan. Vaqt — eng "
              "yaxshi takror, xotira — barcha takrorlarning eng kattasi (RSS = ishchi to'plam, private = shaxsiy "
              "baytlar; ishchi jarayonlar bilan birga); MB = 10⁶ bayt; tezlik = tahrirlangan hujjat yozuvlarining "
              "baytlari / vaqt. Holat «Keyin» ustuniga qarab.",
        "en": "✅ target met · ❌ not met (causes and options below) · — no target. Time = the best repeat, memory = the "
              "largest of all repeats (RSS = working set, private = private bytes; worker processes included); MB = "
              "10⁶ bytes; rate = bytes of the edited document's records / time. Status from the «After» column."},
    "eb.target.export": {"uz": "≥ 50 MB/s, RAM < 300 MB", "en": "≥ 50 MB/s, RAM < 300 MB"},
    "eb.target.bulk": {"uz": "≥ 50 MB/s (hamma yozuv o'zgarganda), dastur < 500 MB",
                       "en": "≥ 50 MB/s (every record changed), program < 500 MB"},
    "eb.target.bulk_some": {"uz": "dastur < 500 MB", "en": "program < 500 MB"},
    "eb.target.bulk_third": {"uz": "≥ 50 MB/s, dastur < 500 MB", "en": "≥ 50 MB/s, program < 500 MB"},
    "eb.target.none": {"uz": "— (§12 da yo'q)", "en": "— (not in §12)"},
    "eb.target.program": {"uz": "< 500 MB", "en": "< 500 MB"},
    "eb.scen.base": {"uz": "tahrirsiz fayl (nazorat)", "en": "the plain file (control)"},
    "eb.scen.point": {"uz": "100 ta nuqtaviy tahrir", "en": "100 point edits"},
    "eb.scen.third": {"uz": "har 3-yozuv o'zgargan (bulk)", "en": "every 3rd record changed (bulk)"},
    "eb.scen.all": {"uz": "hamma yozuv o'zgargan (bulk)", "en": "every record changed (bulk)"},
    "eb.scen.dels": {"uz": "10% yozuv o'chirilgan + 1 000 ta qo'shilgan",
                     "en": "10% of the records deleted + 1,000 inserted"},
    "eb.op.export_json": {"uz": "Eksport JSON (indent 2)", "en": "Export JSON (indent 2)"},
    "eb.op.export_jsonl": {"uz": "Eksport JSON Lines", "en": "Export JSON Lines"},
    "eb.op.export_csv": {"uz": "Eksport CSV (9 ta skalyar ustun)", "en": "Export CSV (9 scalar columns)"},
    "eb.op.export_csv_auto": {"uz": "Eksport CSV (avtomatik ustunlar)", "en": "Export CSV (automatic columns)"},
    "eb.op.export_xlsx": {"uz": "Eksport XLSX (9 ta skalyar ustun)", "en": "Export XLSX (9 scalar columns)"},
    "eb.op.export_sqlite": {"uz": "Eksport SQLite", "en": "Export SQLite"},
    "eb.x.title": {"uz": "# jsonreshape: jadval eksportlari benchmarki (CSV, XLSX, SQLite; AGENT_PROMPT §12)",
                   "en": "# jsonreshape: table export benchmark (CSV, XLSX, SQLite; AGENT_PROMPT §12)"},
    "eb.x.sec.main": {"uz": "## §12 maqsadi: tahrirlar bilan (va tahrirsiz) jadval eksporti",
                      "en": "## §12 target: table exports with (and without) edits"},
    "eb.x.what": {
        "uz": "P6 o'zgarishlari (`jsonreshape/writer.py`): CSV qatorlari ishchi jarayonlarda tayyorlanadi "
              "(`writer.export(..., workers=None)`: 64 MiB dan katta tanlovda avtomatik, `bulk.auto_workers` — "
              "standart xotira chegarasi (512 MiB) bilan 4 ta); ota jarayon bloklarni o'qiydi va natijalarni tartib "
              "bilan yozadi — fayl bir oqimli yozuvchinikidan bayt-bayt farq qilmaydi (2,1 GB faylda SHA-256 bilan "
              "tekshirildi: tahrirsiz, «hamma yozuv o'zgargan» va «10% o'chirilgan» holatlarida, 9 ta ustun, avtomatik "
              "ustunlar va qatorlar rejimi). Avtomatik ustunlarni topish skaneri ham ishchi jarayonlarda (har blok o'z "
              "skaneri, natijalar tartib bilan birlashtiriladi; map-ga o'xshash obyekt yoki juda ko'p yo'l bo'lsa — "
              "bir oqimli skaner). XLSX: 2 ta ishchi kataklarni tayyorlaydi, xlsxwriter ota jarayonda (varaq bir "
              "xil). SQLite o'zgarmagan (nazorat). Qatlamni tiklash (`editstore.py`): 64 MiB va undan katta nazorat "
              "yig'indisi oralig'i 4 ta oqimda bo'laklab o'qiladi (tekshiruv o'sha, holat qaytarilishidan oldin "
              "tugaydi).",
        "en": "P6 changes (`jsonreshape/writer.py`): the CSV rows are built in worker processes "
              "(`writer.export(..., workers=None)`: automatic for a selection over 64 MiB, `bulk.auto_workers` of "
              "them — 4 with the default 512 MiB memory limit); the parent reads the blocks and writes the results "
              "in order — byte-identical to the one-thread writer (checked with SHA-256 on the 2.1 GB file: plain, "
              "«every record changed» and «10% deleted», 9 columns, automatic columns and rows mode). The scan for "
              "automatic columns runs in the workers too (a scan per block, merged in order; map-like objects or "
              "very many paths: the one-thread scan). XLSX: 2 workers prepare the cells, xlsxwriter in the parent "
              "(the same workbook). SQLite unchanged (control). Restoring a layer (`editstore.py`): a checksummed "
              "range of 64 MiB or more is read in pieces by 4 threads (the same check, finished before the state "
              "is returned)."},
    "eb.x.method": {
        "uz": "«Oldin» va «Keyin» bir xil skript bilan, bir xil sharoitda o'lchandi (`--pkg`: P6 dan oldingi va "
              "keyingi paket nusxalari): CSV — har ssenariyda 2 tadan, eng yaxshisi; XLSX va SQLite — bir martadan, "
              "ketma-ket (har biri bir necha daqiqa; SQLite kodi o'zgarmagan — farq kompyuter yuklamasidan); "
              "qatlamni tiklash — 3 tadan, ketma-ket. Xotira o'lchagichi bola jarayonlar ro'yxatini endi har 0,5 s da "
              "yangilaydi (avval har 20 ms da: ~400 jarayonli tizimda ~11 ms GIL ni band qilib, o'lchanayotgan "
              "bir oqimli amalni sekinlashtirgan — P3 jadvalidagi CSV raqamlari shu sababli ham pastroq); bu "
              "jadvaldagi ikkala ustun yangi o'lchagich bilan.",
        "en": "«Before» and «After» ran with the same script under the same conditions (`--pkg`: copies of the "
              "package before and after P6): CSV twice per scenario, the best counts; XLSX and SQLite once each, back "
              "to back (minutes per run; the SQLite code did not change — the difference is the machine's load); the "
              "layer restore 3 times, back to back. The memory sampler now refreshes its list of child processes every "
              "0.5 s (it did every 20 ms: ~11 ms holding the GIL on a system with ~400 processes, slowing the "
              "measured one-thread operation — one reason the CSV numbers of the P3 table were lower); both columns "
              "here use the new sampler."},
    "eb.x.final": {
        "uz": "«Oldin» — P6 dan oldingi paket nusxasi (`--pkg`), 2026-10-19 da kompyuter boshqa agentlar bilan band "
              "paytida o'lchangan; «Keyin» — yakuniy kod (6-bosqich), kompyuterda boshqa agent yoki og'ir ish yo'q "
              "paytda (S6-D), o'sha skript va o'sha xotira o'lchagich bilan (bola jarayonlar ro'yxati har 0,5 s da "
              "yangilanadi). Takrorlar: 2 ta, eng yaxshisi; 150 s dan uzoq amal bir marta.",
        "en": "«Before» is the package copy from before P6 (`--pkg`), measured on 2026-10-19 while other agents used "
              "the machine; «After» is the final code (stage 6) with no other agent and no heavy work on the machine "
              "(S6-D), the same script and the same memory sampler (the list of child processes refreshed every "
              "0.5 s). Runs: 2, the best counts; an operation over 150 s once."},
    "eb.x.cause.export_csv": {
        "uz": "qatorlar ishchi jarayonlarda tayyorlanadi; ota jarayon tahrirlangan hujjat bloklarini yig'adi va "
              "natijani yozadi — kompyuter band bo'lsa ishchilar sekinlashadi.",
        "en": "the rows are built in worker processes; the parent assembles the blocks of the edited document and "
              "writes the results — the workers slow down on a busy machine."},
    "eb.x.opts.export_csv": {
        "uz": "1) ko'proq ishchi jarayon (memory_limit_bytes oshirilsa); 2) qator qurishni Rust yadroga o'tkazish.",
        "en": "1) more worker processes (a larger memory_limit_bytes); 2) move the row building into the Rust core."},
    "eb.x.cause.export_csv_auto": {
        "uz": "ustunlarni topish uchun barcha yozuvlar ikki marta o'qiladi (skaner + qatorlar).",
        "en": "every record is read twice (the column scan, then the rows)."},
    "eb.x.opts.export_csv_auto": {
        "uz": "1) ustunlarni qo'lda tanlash (skaner o'tkazib yuboriladi); 2) Rust yadroda skaner.",
        "en": "1) choose the columns (no scan); 2) a scan in the Rust core."},
    "eb.x.cause.export_xlsx": {
        "uz": "xlsxwriter har katakni Python'da XML ga yozadi (~2,5 mks / katak) va oxirida zip quradi (README qaror "
              "39): cProfile bo'yicha vaqtning ~80% i xlsxwriter'ning katak yozishi, ~5% zip; qiymatlarni tayyorlash "
              "(~15%) endi ishchi jarayonlarda, lekin xlsxwriter ota jarayonda bitta oqimda qoladi.",
        "en": "xlsxwriter writes every cell as XML in Python (~2.5 µs per cell) and builds the zip at the end (README "
              "decision 39): by cProfile ~80% of the time is xlsxwriter's cell writing, ~5% the zip; preparing the "
              "values (~15%) now runs in worker processes, but xlsxwriter stays one thread in the parent."},
    "eb.x.opts.export_xlsx": {
        "uz": "o'z XLSX yozuvchimiz: varaq XML'ini ishchi jarayonlarda tayyorlash (qatorlar tartib bilan), zip alohida "
              "oqimda — 6-bosqichda ixtiyoriy (qaror 39); hozircha katta jadvallar uchun CSV.",
        "en": "an own XLSX writer: the sheet XML built in worker processes (rows in order), the zip in its own "
              "thread — optional in stage 6 (decision 39); for big tables CSV for now."},
    "eb.x.cause.export_sqlite": {
        "uz": "ikki o'tish (map-larni topish, so'ng qatorlar); har yozuv Python'da jadvallarga bo'linadi (har katak "
              "uchun tur tekshiruvi, ustun/jadval reestri) va sqlite3 executemany bilan bitta oqimda yoziladi.",
        "en": "two passes (map detection, then the rows); every record is split into tables in Python (a type "
              "check per cell, the column / table registry) and written with sqlite3 executemany in one thread."},
    "eb.x.opts.export_sqlite": {
        "uz": "1) birinchi o'tishni (map-lar) ishchi jarayonlarda bajarish (CSV skaneri kabi); 2) qatorlarni ishchi "
              "jarayonlarda tayyorlash (jadval/ustun reestri va _id/_parent ota jarayonda) — keyingi ish.",
        "en": "1) run the first pass (maps) in worker processes (like the CSV scan); 2) build the rows in worker "
              "processes (the table / column registry and _id / _parent in the parent) — later work."},
    "eb.x.rerun": {
        "uz": "Qayta o'lchash: `.venv/Scripts/python.exe bench/edit_bench.py --work DIR --only "
              "export_csv,export_csv_auto,export_xlsx,export_sqlite [--pkg PAKET] [--workers N]`; faqat hisobot: "
              "`--report NATIJA.json --export-report --baseline OLDINGI.json`.",
        "en": "Re-run: `.venv/Scripts/python.exe bench/edit_bench.py --work DIR --only "
              "export_csv,export_csv_auto,export_xlsx,export_sqlite [--pkg PACKAGE] [--workers N]`; the report only: "
              "`--report RESULTS.json --export-report --baseline EARLIER.json`."},
    "eb.op.save_as": {"uz": "Boshqacha saqlash (Save As)", "en": "Save As"},
    "eb.op.patch_export": {"uz": "JSON Patch eksporti", "en": "JSON Patch export"},
    "eb.op.patch_import": {"uz": "JSON Patch importi (tahrirsiz faylga, yangi sessiya)",
                           "en": "JSON Patch import (onto the plain file, a fresh session)"},
    "eb.op.restore": {"uz": "Ochish + qatlamni tiklash (keshdagi indeks)",
                      "en": "Open + restore the layer (cached index)"},
    "eb.op.undo_redo": {"uz": "Oxirgi qadamni bekor qilish / qaytarish", "en": "Undo / redo of the last step"},
    "eb.op.make_bulk": {"uz": "Bulk ko'rib chiqish set_field: {scen}", "en": "Bulk preview set_field: {scen}"},
    "eb.op.bulk": {"uz": "Bulk ko'rib chiqish «{kind}», hamma yozuvlar ({what})",
                   "en": "Bulk preview «{kind}», all records ({what})"},
    "eb.op.program": {"uz": "Dasturning umumiy xotirasi (barcha amallarning eng katta peak RSS)",
                      "en": "Whole program memory (largest peak RSS of all operations)"},
    "eb.bulk.what.set_field": {"uz": "har yozuvga maydon", "en": "a field added to every record"},
    "eb.bulk.what.replace": {"uz": "«+998» → «998», har yozuvda", "en": "«+998» → «998», in every record"},
    "eb.bulk.what.rename_key": {"uz": "active → is_active", "en": "active → is_active"},
    "eb.bulk.what.delete_field": {"uz": "tags o'chiriladi", "en": "tags removed"},
    "eb.bulk.what.clean_strings": {"uz": "trim + bo'sh joylar, hech narsa o'zgarmaydi",
                                   "en": "trim + spaces, nothing changes"},
    "eb.bulk.what.convert_type": {"uz": "score → satr", "en": "score → string"},
    "eb.bulk.what.dedupe": {"uz": "id bo'yicha, takror yo'q", "en": "by id, no duplicates"},
    "eb.val.rate": {"uz": "{rate} MB/s, RSS {rss} MB, private {prv} MB ({secs}{extra})",
                    "en": "{rate} MB/s, RSS {rss} MB, private {prv} MB ({secs}{extra})"},
    "eb.val.secs": {"uz": "{secs}, RSS {rss} MB, private {prv} MB{extra}",
                    "en": "{secs}, RSS {rss} MB, private {prv} MB{extra}"},
    "eb.val.fsync": {"uz": "; fsync {f}, fsync'siz {r} MB/s", "en": "; fsync {f}, {r} MB/s without it"},
    "eb.val.workers": {"uz": "; {n} ta ishchi jarayon", "en": "; {n} worker processes"},
    "eb.val.ops": {"uz": "; {ops} ta amal", "en": "; {ops} operations"},
    "eb.val.opsps": {"uz": "; {ops} ta amal, {rate} amal/s", "en": "; {ops} operations, {rate} ops/s"},
    "eb.val.changed": {"uz": "; {n} ta yozuv o'zgardi", "en": "; {n} records changed"},
    "eb.val.undo": {"uz": "bekor qilish {u}, qaytarish {r}; RSS {rss} MB", "en": "undo {u}, redo {r}; RSS {rss} MB"},
    "eb.val.program": {"uz": "{rss} MB RSS (eng kattasi: {op}), private {prv} MB",
                       "en": "{rss} MB RSS (largest: {op}), private {prv} MB"},
    "eb.val.single": {"uz": " — bir marta o'lchandi (juda sekin)", "en": " — measured once (very slow)"},
    "eb.val.missing": {"uz": "o'lchanmagan", "en": "not measured"},
    "eb.val.error": {"uz": "XATO", "en": "ERROR"},
    "eb.unit.s": {"uz": "{v} s", "en": "{v} s"},
    "eb.unit.ms": {"uz": "{v} ms", "en": "{v} ms"},
    "eb.miss.line": {"uz": "- **{row}** — {value} (maqsad: {target}).",
                     "en": "- **{row}** — {value} (target: {target})."},
    "eb.miss.cause": {"uz": "  - Sabab: {text}", "en": "  - Cause: {text}"},
    "eb.miss.options": {"uz": "  - Variantlar: {text}", "en": "  - Options: {text}"},
    "eb.cause.csv": {
        "uz": "CSV yozuvchi (writer.py) har bir yozuvni parse qilib, kataklarni Python'da quradi; tahrirsiz faylning "
              "nazorat qatori ham shu tezlikda, ya'ni chegarani tahrirlar emas, CSV yozish belgilaydi (tahrirlangan "
              "yozuvlarni o'qish qo'shimchasi kichik).",
        "en": "the CSV writer (writer.py) parses every record and builds the cells in Python; the control row of the "
              "plain file runs at the same rate, so the CSV writing, not the edits, sets the limit (reading the "
              "edited records adds little)."},
    "eb.opts.csv": {
        "uz": "1) CSV qatorlarini bloklab ishchi jarayonlarda tayyorlash (engine.apply / bulk kabi; writer.py egasiga "
              "so'rov); 2) qator qurishni Rust yadroga o'tkazish (PathExtractor spanlari + xom tokenlar).",
        "en": "1) build the CSV rows of blocks in worker processes (like engine.apply / bulk; a request to the owner "
              "of writer.py); 2) move the row building into the Rust core (PathExtractor spans + raw tokens)."},
    "eb.cause.export": {
        "uz": "tahrirlangan hujjat bloklari yozuvma-yozuv yig'iladi (fayl va qatlam segmentidan) va yozuvchi ularni "
              "yana bo'laklaydi; o'lchov vaqtida kompyuter boshqa agentlar bilan band edi.",
        "en": "the blocks of an edited document are assembled record by record (from the file and the layer's "
              "segment) and the writer slices them again; the machine was shared with other agents."},
    "eb.opts.export": {
        "uz": "1) yozuvchi (writer.py) aralash bloklarni o'zi bo'laklamasdan to'g'ridan-to'g'ri qabul qilishi; 2) "
              "eksportni ham ishchi jarayonlarga bo'lish.",
        "en": "1) let the writer (writer.py) take mixed blocks without slicing them again; 2) split the export into "
              "worker processes as well."},
    "eb.cause.save": {
        "uz": "saver.py har bir run uchun Python'da qism qo'shadi (har 3-yozuv o'zgarganda ~4,3 mln run); fsync "
              "vaqti ham kiradi.",
        "en": "saver.py adds pieces per run in Python (~4.3 M runs when every 3rd record changed); the fsync time is "
              "included."},
    "eb.opts.save": {
        "uz": "saver._Planner._window'ni oynaviy massivlar bilan (reader._EffWindow kabi: spanlar + belgi baytlari, "
              "runlarsiz) ishlash (saver.py egasiga so'rov).",
        "en": "make saver._Planner._window work on window arrays (like reader._EffWindow: spans + mark bytes, no "
              "per-run Python) (a request to the owner of saver.py)."},
    "eb.cause.bulk": {
        "uz": "har yozuv parse qilinadi, qoida Python'da qo'llanadi va qayta yoziladi; ishchi jarayonlar soni xotira "
              "byudjeti bilan cheklangan (512 MiB → 4 ta).",
        "en": "every record is parsed, the rule runs in Python and the record is written again; the number of worker "
              "processes is bounded by the memory budget (512 MiB → 4)."},
    "eb.opts.bulk": {
        "uz": "1) memory_limit_bytes oshirilsa, ko'proq ishchi jarayon; 2) «replace» uchun MultiFinder.locate_tokens "
              "bilan faqat ignasi bor satrlarni tekshirish; 3) qoidalarni Rust yadroga o'tkazish.",
        "en": "1) more worker processes with a larger memory_limit_bytes; 2) for «replace», only the strings with "
              "needles (MultiFinder.locate_tokens); 3) move the rules into the Rust core."},
    "eb.cause.memory": {
        "uz": "o'zgargan yozuvlarning spanlari (24 bayt / yozuv) va yozuvlar keshi (memory_limit_bytes // 4) xotirada.",
        "en": "the spans of the changed records (24 bytes per record) and the record cache (memory_limit_bytes // 4) "
              "are kept in memory."},
    "eb.opts.memory": {
        "uz": "span jurnalini vaqtinchalik faylga to'kish (bulk preview kabi), siyrak indeks bilan qidirish.",
        "en": "spill the span log to a temporary file (like the bulk preview) and look it up through a sparse index."},
    "eb.machine.cpu": {"uz": "- Protsessor: {cpu}, {cores} yadro / {logical} mantiqiy protsessor",
                       "en": "- CPU: {cpu}, {cores} cores / {logical} logical processors"},
    "eb.machine.ram": {"uz": "- RAM: {ram}", "en": "- RAM: {ram}"},
    "eb.machine.disk": {"uz": "- Disk, {what}: {drive} {model} ({bus}, {media}, {fs}), bo'sh joy {free}",
                        "en": "- Disk, {what}: {drive} {model} ({bus}, {media}, {fs}), {free} free"},
    "eb.machine.disk.data": {"uz": "ma'lumot fayli", "en": "data file"},
    "eb.machine.disk.work": {"uz": "chiqishlar va qatlamlar", "en": "outputs and layers"},
    "eb.machine.os": {"uz": "- OS: {os}; Python {py}; yadro: {backend}",
                      "en": "- OS: {os}; Python {py}; core: {backend}"},
    "eb.cond.process": {
        "uz": "- Har bir amal alohida yangi jarayonda (`bench/edit_bench.py`), har ssenariy o'z JSONRESHAPE_HOME'ida; "
              "ochish va qatlamni tiklash alohida o'lchanadi, jadvalda faqat amalning o'zi. Takrorlar: {repeat} "
              "(juda sekin amal — {slow} s dan ortig'i — bir marta), eng yaxshisi olinadi.",
        "en": "- Every operation runs in a fresh process (`bench/edit_bench.py`), every scenario in its own "
              "JSONRESHAPE_HOME; opening and restoring the layer are timed separately, the table shows the operation "
              "itself. Repeats: {repeat} (a very slow operation — over {slow} s — once); the best one counts."},
    "eb.cond.memory": {
        "uz": "- Xotira: jarayonning o'zi va uning bola jarayonlari (ishchi jarayonlar) har 20 ms da o'lchandi (RSS va "
              "private yig'indisi); RSS ga jarayon tegib o'tgan mmap sahifalari ham kiradi.",
        "en": "- Memory: the process and its child processes (workers) sampled every 20 ms (sum of RSS and of "
              "private bytes); the RSS includes the mapped pages the process touched."},
    "eb.cond.fsync": {
        "uz": "- Chiqish fayli fsync qilinadi; fsync vaqti alohida o'lchandi (bu diskda u katta ulush) va fsync'siz "
              "tezlik ham ko'rsatilgan. Chiqishlar va qatlamlar C: diskida (agentning vaqtinchalik papkasi).",
        "en": "- The output is fsynced; the fsync time is measured on its own (a large share on this drive) and the "
              "rate without it is shown too. Outputs and layers are on drive C: (the agent's scratch folder)."},
    "eb.cond.load": {
        "uz": "- Har o'lchovdan oldin tizim CPU yuklamasi ≤ {limit}% bo'lishi kutildi (ko'pi bilan {timeout} s); "
              "o'lchovlar paytidagi yuklama (o'lchanayotgan amalning o'zi ham kiradi): o'rtacha {mean}%, eng ko'pi "
              "{max}%.",
        "en": "- Before every run the system CPU load had to drop to ≤ {limit}% (waiting at most {timeout} s); the "
              "load during the runs (the measured operation included): {mean}% on average, at most {max}%."},
    "eb.cond.shared": {
        "uz": "- Kompyuter boshqa agentlar bilan umumiy edi (ular o'z testlari va o'lchovlarini ishlatgan).",
        "en": "- The machine was shared with other agents (running their own tests and measurements)."},
    "eb.cond.final": {
        "uz": "- Yakuniy o'lchov (6-bosqich, S6-D): kompyuterda boshqa agent yoki og'ir ish yo'q edi; «Keyin» — "
              "yakuniy kod. «Oldin» — 3-bosqichning unumdorlik ishidan (P3) oldingi paket nusxasi, 2026-10-19 da "
              "kompyuter boshqa agentlar bilan band paytida o'lchangan (`edit-20261019-112336-before.json`); "
              "tahrirsiz fayl qatorlari ikki o'lchov sharoitining farqini ko'rsatadi.",
        "en": "- The final measurement (stage 6, S6-D): no other agent and no heavy work on the machine; «After» is "
              "the final code. «Before» is the package copy from before the stage-3 performance round (P3), "
              "measured on 2026-10-19 while other agents used the machine (`edit-20261019-112336-before.json`); "
              "the plain-file rows show how the conditions of the two runs differ."},
    "eb.cond.snapshots": {
        "uz": "- «Oldin» — `--baseline` dagi o'lchov (boshqa paket nusxasi, `--pkg`, yoki oldingi o'lchov): o'rtada "
              "boshqa modullar ham o'zgargan (writer, editstore, index, fileio, ...); tahrirsiz fayl qatorlari "
              "ularning ta'sirini ko'rsatuvchi nazorat.",
        "en": "- «Before» is the run of `--baseline` (another copy of the package, `--pkg`, or an earlier run): other "
              "modules changed in between too (writer, editstore, index, fileio, ...); the plain-file rows are the "
              "control for their effect."},
    "eb.cond.p6": {
        "uz": "- P6 (2026-10-19): «Keyin» ustunidagi «Eksport CSV» qatorlari va «Ochish + qatlamni tiklash» qatorlari "
              "(har 3-yozuv, hamma yozuv, 10% o'chirilgan) P6 dan keyingi paket bilan qayta o'lchandi (CSV qatorlari "
              "ishchi jarayonlarda; qatlam nazorat yig'indilari 4 ta oqimda; `edit-*-ab-final-*.json`) va yangi xotira "
              "o'lchagich bilan: u bola jarayonlar ro'yxatini har 0,5 s da yangilaydi (avvalgi har-20-ms ro'yxat GIL ni "
              "~11 ms band qilib, bir oqimli amallarni sekinlashtirgan, shuning uchun «Oldin» ustunidagi CSV raqamlari "
              "pastroq). Qolgan qatorlar P3 o'lchovlari. Bir xil sharoitdagi oldin/keyin: `export_latest.md`.",
        "en": "- P6 (2026-10-19): the «Export CSV» rows and the «Open + restore the layer» rows (every 3rd record, "
              "every record, 10% deleted) of the «After» column were measured again with the package after P6 (the "
              "CSV rows in worker processes; the layer's checksums in 4 threads; `edit-*-ab-final-*.json`) and the new "
              "memory sampler: it refreshes its list of child processes every 0.5 s (listing them every 20 ms held "
              "the GIL ~11 ms and slowed one-thread operations, so the CSV numbers of the «Before» column are lower). "
              "The other rows are P3's. Before / after under the same conditions: `export_latest.md`."},
    "eb.cond.cache": {
        "uz": "- Ma'lumot fayli OS keshida (iliq kesh); 205 GB RAM.",
        "en": "- The data file is in the OS cache (warm); 205 GB of RAM."},
    "eb.unmeasured.gui": {
        "uz": "- GUI'dagi vaqtlar (kutubxona darajasida o'lchandi; GUI shu amallarni fon vazifalarida chaqiradi).",
        "en": "- Times in the GUI (measured at the library level; the GUI runs the same calls in background tasks)."},
    "eb.unmeasured.5g": {
        "uz": "- §12 dagi 5 GB holat: 2,1 GB fayl ishlatildi (tezlik fayl hajmiga bog'liq emas; xotira o'zgargan "
              "yozuvlar soniga ~24 bayt / yozuv bilan o'sadigan joylar ayrim aytilgan).",
        "en": "- The 5 GB case of §12: the 2.1 GB file was used (rates do not depend on the size; where memory grows "
              "with the changed records, ~24 bytes per record, it is said)."},
    "eb.rerun": {
        "uz": "Qayta o'lchash: `.venv/Scripts/python.exe bench/edit_bench.py --work DIR [--pkg PAKET] "
              "[--baseline OLDINGI.json]`; faqat hisobot: `--report NATIJA.json --baseline OLDINGI.json`.",
        "en": "Re-run: `.venv/Scripts/python.exe bench/edit_bench.py --work DIR [--pkg PACKAGE] "
              "[--baseline EARLIER.json]`; the report only: `--report RESULTS.json --baseline EARLIER.json`."},
}


def _register_messages() -> None:
    from jsonreshape import i18n

    for key, entry in EDIT_BENCH_MESSAGES.items():
        i18n.MESSAGES.setdefault(key, entry)


def _runs_of(results: dict[str, Any] | None) -> dict[tuple[str, str], list[dict[str, Any]]]:
    out: dict[tuple[str, str], list[dict[str, Any]]] = {}
    if not results:
        return out
    for r in results.get("runs", []):
        out.setdefault((r.get("op", ""), r.get("scen", "")), []).append(r)
    return out


def _fmt_num(x: float, digits: int = 1) -> str:
    if x >= 100:
        return f"{x:,.0f}".replace(",", " ")
    return f"{x:.{digits}f}"


def _fmt_secs(s: float) -> str:
    from jsonreshape.i18n import t

    if s < 1:
        return t("eb.unit.ms", v=_fmt_num(s * 1000))
    return t("eb.unit.s", v=_fmt_num(s, 2) if s < 10 else _fmt_num(s))


class _Cell:
    """One measured value: the text, the rate (MB/s), the memory peaks."""

    def __init__(self, text: str, rate: float | None = None, rss: float | None = None, prv: float | None = None,
                 secs: float | None = None) -> None:
        self.text = text
        self.rate = rate
        self.rss = rss
        self.prv = prv
        self.secs = secs


def _cell(runs: list[dict[str, Any]] | None, phase: str, *, rate: bool = True, extra: str = "",
          bytes_of: Callable[[dict[str, Any]], float] | None = None) -> _Cell:
    from jsonreshape.i18n import t

    if not runs:
        return _Cell(t("eb.val.missing"))
    good = [r for r in runs if "error" not in r and phase in r.get("phases", {})]
    if not good:
        return _Cell(t("eb.val.error"))
    best = min(good, key=lambda r: r["phases"][phase]["seconds"])
    ph = best["phases"][phase]
    rss = max(r["phases"][phase]["rss_peak_mb"] for r in good)
    prv = max(r["phases"][phase]["private_peak_mb"] for r in good)
    secs = float(ph["seconds"])
    more = extra
    if ph.get("workers"):
        more += t("eb.val.workers", n=ph["workers"])
    note = t("eb.val.single") if len(good) == 1 and secs > 150 else ""
    if not rate:
        return _Cell(t("eb.val.secs", secs=_fmt_secs(secs), rss=_fmt_num(rss), prv=_fmt_num(prv), extra=more) + note,
                     None, rss, prv, secs)
    nbytes = bytes_of(best) if bytes_of is not None else float(ph.get("bytes") or best.get("effective_bytes") or 0)
    r = nbytes / MB / max(secs, 1e-9)
    fs = float(ph.get("fsync_s") or 0.0)
    if fs >= 0.05:
        more = t("eb.val.fsync", f=_fmt_secs(fs), r=_fmt_num(nbytes / MB / max(secs - fs, 1e-9))) + more
    return _Cell(t("eb.val.rate", rate=_fmt_num(r), rss=_fmt_num(rss), prv=_fmt_num(prv), secs=_fmt_secs(secs),
                   extra=more) + note, r, rss, prv, secs)


def _status(ok: bool | None) -> str:
    return "—" if ok is None else ("✅" if ok else "❌")


def load_results(paths: list[Path]) -> dict[str, Any]:
    """Results files merged: the runs of a later file replace those of the same operation and scenario in the
    earlier ones (a targeted re-run with ``--only``); the first file gives the rest."""
    merged = json.loads(paths[0].read_text(encoding="utf-8"))
    for p in paths[1:]:
        more = json.loads(p.read_text(encoding="utf-8"))
        redone = {(r.get("op"), r.get("scen")) for r in more.get("runs", [])}
        merged["runs"] = [r for r in merged.get("runs", []) if (r.get("op"), r.get("scen")) not in redone]
        merged["runs"] += more.get("runs", [])
        merged.setdefault("merged", []).append(p.name)
    return merged


def write_report(results_path: Path | list[Path], baseline_path: Path | None, lang: str = "uz",
                 out_md: Path | None = None, notes: Sequence[str] = ()) -> Path:
    """The markdown report of a results file (or several: :func:`load_results`) with an «earlier» column
    from ``baseline_path``; ``notes``: keys of :data:`EDIT_BENCH_MESSAGES` added to the measurement
    conditions (e.g. ``eb.cond.p6``: which rows a later run replaced)."""
    for p in (str(ROOT), str(BENCH)):
        if p not in sys.path:
            sys.path.insert(0, p)
    from jsonreshape import i18n
    from jsonreshape.i18n import t

    _register_messages()
    i18n.set_language(lang)
    paths = results_path if isinstance(results_path, list) else [results_path]
    results_path = paths[0]
    after = load_results(paths)
    before = json.loads(baseline_path.read_text(encoding="utf-8")) if baseline_path else None
    A, B = _runs_of(after), _runs_of(before)
    rows_main: list[tuple[str, str, _Cell, _Cell, bool | None, str]] = []
    rows_extra: list[tuple[str, str, _Cell, _Cell, bool | None, str]] = []

    def ok_export(c: _Cell) -> bool | None:
        if c.rate is None:
            return None
        return c.rate >= 50 and (c.rss or 0) < 300

    for scen in ("point", "third", "all", "dels", "base"):
        for op in ("export_json", "export_jsonl", "export_csv", "save_as"):
            label = f"{t('eb.op.' + op)}, {t('eb.scen.' + scen)}"
            a, b = _cell(A.get((op, scen)), "op"), _cell(B.get((op, scen)), "op")
            cat = "csv" if op == "export_csv" else ("save" if op == "save_as" else "export")
            (rows_main if scen != "base" else rows_extra).append(
                (label, t("eb.target.export"), b, a, ok_export(a), cat))
    for scen in ("all", "third"):
        a, b = _cell(A.get(("make", scen)), "bulk_preview"), _cell(B.get(("make", scen)), "bulk_preview")
        ok = None if a.rate is None else (a.rate >= 50 and (a.rss or 0) < 500)
        rows_extra.append((t("eb.op.make_bulk", scen=t("eb.scen." + scen)),
                           t("eb.target.bulk" if scen == "all" else "eb.target.bulk_third"), b, a, ok, "bulk"))
    for kind in BULK_KINDS:
        a, b = _cell(A.get((f"bulk:{kind}", "base")), "op"), _cell(B.get((f"bulk:{kind}", "base")), "op")
        every = kind in ("set_field", "replace", "rename_key", "delete_field", "convert_type")
        if a.rate is None:
            ok = None
        elif every:
            ok = a.rate >= 50 and (a.rss or 0) < 500
        else:
            ok = (a.rss or 0) < 500
        rows_extra.append((t("eb.op.bulk", kind=kind, what=t("eb.bulk.what." + kind)),
                           t("eb.target.bulk" if every else "eb.target.bulk_some"), b, a, ok, "bulk"))
    for scen in ("point", "third", "all", "dels"):
        def ops_of(res: dict[tuple[str, str], list[dict[str, Any]]], s: str = scen) -> int:
            for r in res.get(("patch_export", s), []):
                n = r.get("phases", {}).get("op", {}).get("ops")
                if n is not None:
                    return int(n)
            return 0

        na, nb = ops_of(A), ops_of(B)
        as_rate = scen in ("third", "all")          # (a few changed records: the document's MB/s says nothing)
        a = _cell(A.get(("patch_export", scen)), "op", rate=as_rate,
                  extra=t("eb.val.ops", ops=f"{na:,}".replace(",", " ")))
        b = _cell(B.get(("patch_export", scen)), "op", rate=as_rate,
                  extra=t("eb.val.ops", ops=f"{nb:,}".replace(",", " ")))
        rows_extra.append((f"{t('eb.op.patch_export')}, {t('eb.scen.' + scen)}", t("eb.target.bulk_some"), b, a,
                           None if a.prv is None else (a.rss or 0) < 500, "memory"))
        if scen != "point":
            ca = _cell(A.get(("patch_import", scen)), "op", rate=False)
            cb = _cell(B.get(("patch_import", scen)), "op", rate=False)
            for c, n in ((ca, na), (cb, nb)):
                if c.secs:
                    c.text += t("eb.val.opsps", ops=f"{n:,}".replace(",", " "), rate=_fmt_num(n / c.secs))
            rows_extra.append((f"{t('eb.op.patch_import')}, {t('eb.scen.' + scen)}", t("eb.target.bulk_some"), cb,
                               ca, None if ca.rss is None else (ca.rss or 0) < 500, "memory"))
    for scen in ("point", "third", "all", "dels"):
        a = _cell(A.get(("restore", scen)), "restore", rate=False)
        b = _cell(B.get(("restore", scen)), "restore", rate=False)
        rows_extra.append((f"{t('eb.op.restore')}, {t('eb.scen.' + scen)}", t("eb.target.none"), b, a, None, ""))
        cells = []
        for res in (B, A):
            runs = [r for r in res.get(("undo_redo", scen), []) if "error" not in r and "undo" in r.get("phases", {})]
            if runs:
                u = min(r["phases"]["undo"]["seconds"] for r in runs)
                rd = min(r["phases"]["redo"]["seconds"] for r in runs)
                rss = max(max(r["phases"]["undo"]["rss_peak_mb"], r["phases"]["redo"]["rss_peak_mb"]) for r in runs)
                cells.append(_Cell(t("eb.val.undo", u=_fmt_secs(u), r=_fmt_secs(rd), rss=_fmt_num(rss)), None, rss))
            else:
                cells.append(_Cell(t("eb.val.missing")))
        rows_extra.append((f"{t('eb.op.undo_redo')}, {t('eb.scen.' + scen)}", t("eb.target.none"), cells[0],
                           cells[1], None, ""))

    def program(res: dict[tuple[str, str], list[dict[str, Any]]]) -> _Cell:
        best: tuple[float, float, str] = (0.0, 0.0, "")
        for (op, scen), runs in res.items():
            for r in runs:
                for name, ph in r.get("phases", {}).items():
                    if ph.get("rss_peak_mb", 0) > best[0]:
                        best = (ph["rss_peak_mb"], ph.get("private_peak_mb", 0), f"{op} {scen} ({name})")
        if not best[2]:
            return _Cell(t("eb.val.missing"))
        return _Cell(t("eb.val.program", rss=_fmt_num(best[0]), op=best[2], prv=_fmt_num(best[1])), None, best[0],
                     best[1])

    pa, pb = program(A), program(B)
    rows_main.append((t("eb.op.program"), t("eb.target.program"), pb, pa,
                      None if pa.rss is None else pa.rss < 500, "memory"))

    data = Path(after.get("args", {}).get("data", "syn_2g.json"))
    size = data.stat().st_size if data.exists() else 0
    records = ""
    for r in after.get("runs", []):
        rec = r.get("phases", {}).get("open", {}).get("records")
        if rec:
            records = f"{int(rec):,}".replace(",", " ")
            break
    lines = [t("eb.title"), "",
             t("eb.head", date=datetime.fromtimestamp(float(after.get("started", time.time()))).strftime(
                 "%Y-%m-%d %H:%M"),
               data=data.name, size=f"{size / 1e9:.1f} GB", records=records,
               json="`, `".join(p.name for p in paths),
               before=t("eb.head.before", json=baseline_path.name) if baseline_path else ""), ""]

    def table(rows: list[tuple[str, str, _Cell, _Cell, bool | None, str]]) -> list[str]:
        out = [t("eb.col"), "|---|---|---|---|:---:|"]
        for label, target, b, a, ok, _cat in rows:
            out.append(f"| {label} | {target} | {b.text} | {a.text} | {_status(ok)} |")
        return out

    lines += [t("eb.sec.main"), ""] + table(rows_main) + ["", t("eb.legend"), "", t("eb.sec.extra"), ""]
    lines += table(rows_extra) + [""]
    misses = [row for row in rows_main + rows_extra if row[4] is False]
    lines += [t("eb.sec.misses"), ""]
    seen: set[str] = set()
    for label, target, _b, a, _ok, cat in misses:
        lines.append(t("eb.miss.line", row=label, value=a.text, target=target))
        if cat and cat not in seen:
            seen.add(cat)
            lines.append(t("eb.miss.cause", text=t(f"eb.cause.{cat}")))
            lines.append(t("eb.miss.options", text=t(f"eb.opts.{cat}")))
    if not misses:
        lines.append("—")
    lines += _machine_and_conditions(after, data, before is not None)
    lines += [t(key) for key in notes]
    lines += ["", t("eb.sec.unmeasured"), "", t("eb.unmeasured.gui"), t("eb.unmeasured.5g"), "", t("eb.rerun"), ""]
    text = "\n".join(line for line in lines if line is not None)
    target = out_md or (results_path.parent / "edit_latest.md")
    target.write_text(text, encoding="utf-8")
    return target


def _machine_and_conditions(after: dict[str, Any], data: Path, with_before: bool) -> list[str]:
    """The «Machine» and «Measurement conditions» sections of a report."""
    from jsonreshape.i18n import t

    lines = ["", t("eb.sec.machine"), ""]
    m = dict(after.get("machine", {}))
    try:
        import run_bench  # bench/run_bench.py: the machine description of the main benchmark

        m.update(run_bench.machine_info(data.parent))
    except Exception:  # noqa: BLE001 - the report still works without it
        pass
    lines.append(t("eb.machine.cpu", cpu=m.get("cpu") or m.get("processor") or "?",
                   cores=m.get("cores_physical") or m.get("cpus") or "?", logical=m.get("cores_logical") or "?"))
    ram = m.get("ram_total")
    lines.append(t("eb.machine.ram", ram=f"{ram / 1e9:.1f} GB" if ram else f"{m.get('ram_gb', '?')} GB"))
    work = Path(after.get("args", {}).get("work", "."))
    for what, place in (("data", data.parent), ("work", work)):
        try:
            import run_bench

            d = run_bench._disk_model(place if place.exists() else Path(str(place)[:3]))
        except Exception:  # noqa: BLE001 - the report still works without it
            continue
        lines.append(t("eb.machine.disk", what=t(f"eb.machine.disk.{what}"), drive=d.get("drive") or "?",
                       model=d.get("model") or "?", bus=d.get("bus") or "?", media=d.get("media") or "?",
                       fs=d.get("fs") or "?", free=f"{(d.get('free') or 0) / 1e9:.0f} GB"))
    backend = next((r.get("backend") for r in after.get("runs", []) if r.get("backend")), "?")
    lines.append(t("eb.machine.os", os=m.get("os") or m.get("platform") or "?", py=m.get("python", "?"),
                   backend=backend))
    loads = [r for r in after.get("runs", []) if "load_mean" in r]
    args = after.get("args", {})
    lines += [line for line in ("", t("eb.sec.conditions"), "",
              t("eb.cond.process", repeat=args.get("repeat", "?"), slow=args.get("max_repeat_seconds", "150")),
              t("eb.cond.memory"), t("eb.cond.fsync"),
              t("eb.cond.load", limit=args.get("quiet_load", "35"), timeout=args.get("quiet_timeout", "60"),
                mean=_fmt_num(sum(r["load_mean"] for r in loads) / max(1, len(loads))),
                max=_fmt_num(max((r["load_max"] for r in loads), default=0.0))),
              t("eb.cond.snapshots") if with_before else None, t("eb.cond.cache"))
              if line is not None]
    return lines


# ======================================================================================================
# the table-export report (bench/results/export_latest.md): CSV, XLSX and SQLite of every scenario
# ======================================================================================================
TABLE_REPORT_OPS = ("export_csv", "export_csv_auto", "export_xlsx", "export_sqlite")


def write_export_report(results_path: Path | list[Path], baseline_path: Path | list[Path] | None,
                        lang: str = "uz", out_md: Path | None = None, notes: Sequence[str] = ()) -> Path:
    """The markdown report of the table exports (``export_csv``, ``export_csv_auto``, ``export_xlsx``,
    ``export_sqlite``) of a results file (or several, :func:`load_results`) with an «earlier» column from
    ``baseline_path`` (one file or several, merged the same way; default output:
    ``<results-dir>/export_latest.md``); ``notes``: keys of :data:`EDIT_BENCH_MESSAGES` that describe how the two
    columns were measured (instead of ``eb.x.method``, the P6 comparison)."""
    for p in (str(ROOT), str(BENCH)):
        if p not in sys.path:
            sys.path.insert(0, p)
    from jsonreshape import i18n
    from jsonreshape.i18n import t

    _register_messages()
    i18n.set_language(lang)
    paths = results_path if isinstance(results_path, list) else [results_path]
    after = load_results(paths)
    bases = [] if not baseline_path else baseline_path if isinstance(baseline_path, list) else [baseline_path]
    before = load_results(bases) if bases else None
    baseline_path = bases[0] if bases else None
    A, B = _runs_of(after), _runs_of(before)
    rows: list[tuple[str, str, _Cell, _Cell, bool | None, str]] = []
    for scen in ("base", "point", "third", "all", "dels"):
        for op in TABLE_REPORT_OPS:
            if not A.get((op, scen)) and not B.get((op, scen)):
                continue
            a, b = _cell(A.get((op, scen)), "op"), _cell(B.get((op, scen)), "op")   # (workers: from the sampler)
            ok = None if a.rate is None else (a.rate >= 50 and (a.rss or 0) < 300)
            rows.append((f"{t('eb.op.' + op)}, {t('eb.scen.' + scen)}", t("eb.target.export"), b, a, ok, op))
        if A.get(("restore", scen)) or B.get(("restore", scen)):          # (the layer's checksums, P6)
            a = _cell(A.get(("restore", scen)), "restore", rate=False)
            b = _cell(B.get(("restore", scen)), "restore", rate=False)
            rows.append((f"{t('eb.op.restore')}, {t('eb.scen.' + scen)}", t("eb.target.none"), b, a, None, "restore"))
    data = Path(after.get("args", {}).get("data", "syn_2g.json"))
    size = data.stat().st_size if data.exists() else 0
    records = ""
    for r in after.get("runs", []):
        rec = r.get("phases", {}).get("open", {}).get("records")
        if rec:
            records = f"{int(rec):,}".replace(",", " ")
            break
    lines = [t("eb.x.title"), "",
             t("eb.head", date=datetime.fromtimestamp(float(after.get("started", time.time()))).strftime(
                 "%Y-%m-%d %H:%M"),
               data=data.name, size=f"{size / 1e9:.1f} GB", records=records,
               json="`, `".join(p.name for p in paths),
               before=t("eb.head.before", json="`, `".join(p.name for p in bases)) if bases else ""), "",
             t("eb.x.sec.main"), "", t("eb.col"), "|---|---|---|---|:---:|"]
    for label, target, b, a, ok, _op in rows:
        lines.append(f"| {label} | {target} | {b.text} | {a.text} | {_status(ok)} |")
    method = [t(key).lstrip("- ") for key in notes] or [t("eb.x.method")]
    lines += ["", t("eb.legend"), "", t("eb.x.what"), ""] + [x for m in method for x in (m, "")]
    lines += [t("eb.sec.misses"), ""]
    misses = [row for row in rows if row[4] is False]
    seen: set[str] = set()
    for label, target, _b, a, _ok, op in misses:
        lines.append(t("eb.miss.line", row=label, value=a.text, target=target))
        if op not in seen:
            seen.add(op)
            lines.append(t("eb.miss.cause", text=t(f"eb.x.cause.{op}")))
            lines.append(t("eb.miss.options", text=t(f"eb.x.opts.{op}")))
    if not misses:
        lines.append("—")
    lines += _machine_and_conditions(after, data, before is not None)
    lines += ["", t("eb.sec.unmeasured"), "", t("eb.unmeasured.gui"), t("eb.unmeasured.5g"), "", t("eb.x.rerun"), ""]
    text = "\n".join(line for line in lines if line is not None)
    target = out_md or (paths[0].parent / "export_latest.md")
    target.write_text(text, encoding="utf-8")
    return target


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--child", action="store_true", help=argparse.SUPPRESS)
    ap.add_argument("--op", default="")
    ap.add_argument("--scen", default="base")
    ap.add_argument("--home", default="base")
    ap.add_argument("--keep", default="")
    ap.add_argument("--patch", default="")
    ap.add_argument("--data", default=str(DEFAULT_DATA))
    ap.add_argument("--work", default=str(BENCH / "data" / "work" / "edit"))
    ap.add_argument("--pkg", default="", help="folder holding the jsonreshape package to measure")
    ap.add_argument("--repeat", type=int, default=2)
    ap.add_argument("--max-repeat-seconds", type=float, default=150.0,
                    help="an operation slower than this is not repeated")
    ap.add_argument("--only", default="")
    ap.add_argument("--label", default="")
    ap.add_argument("--results-dir", default=str(BENCH / "results"))
    ap.add_argument("--quiet-load", type=float, default=35.0)
    ap.add_argument("--quiet-timeout", type=float, default=60.0)
    ap.add_argument("--keep-work", action="store_true")
    ap.add_argument("--report", default="",
                    help="write the markdown of a results file (several, comma-separated: merged) and exit")
    ap.add_argument("--baseline", default="", help="results file of the 'before' run (report column)")
    ap.add_argument("--lang", default="uz")
    ap.add_argument("--md", default="", help="markdown file of the report (default: <results-dir>/edit_latest.md)")
    ap.add_argument("--workers", type=int, default=0,
                    help="worker processes asked of the exports (0: automatic, as in the program)")
    ap.add_argument("--note", action="append", default=[],
                    help="with --report: a message key added to the measurement conditions (repeatable)")
    ap.add_argument("--export-report", action="store_true",
                    help="with --report: write the table-export report (default: <results-dir>/export_latest.md)")
    args = ap.parse_args(argv)
    if args.child:
        child_main(args)
    elif args.report:
        paths = [Path(p) for p in args.report.split(",")]
        base = Path(args.baseline) if args.baseline else None
        if args.export_report:
            bases = [Path(p) for p in args.baseline.split(",")] if args.baseline else None
            out = write_export_report(paths, bases, args.lang, Path(args.md) if args.md else None, notes=args.note)
        else:
            out = write_report(paths, base, args.lang, Path(args.md) if args.md else None, notes=args.note)
        print("report:", out)
    else:
        runner_main(args)


if __name__ == "__main__":
    main()
