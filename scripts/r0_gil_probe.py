"""v16_5 R0: what the GIL costs the GUI thread, Python readers vs Rust.

    r0_gil_probe.py roundtrip [--calls 20000]
        the cost of one call into Rust that releases and re-takes the GIL
        (`detached_spin(0)`) against one that keeps it (`held_spin(0)`),
        with 0, 1 and 6 busy pure-Python threads competing for the GIL.
    r0_gil_probe.py heartbeat PATH [--seconds 5]
        a 4 ms QTimer on the main (GUI) thread records its intervals while
        (a) nothing else runs, (b) 6 Python threads read tiles through
        `RawTileProvider` (the current path), (c) Rust reads the same tiles
        on 6 threads with the GIL released. Warm page cache.

No display needed (QCoreApplication). Measurement only.
"""

import argparse
import json
import os
import random
import sys
import threading
import time

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import r0_decode_probe as probe  # noqa: E402 -- also maps `block01`

import fusion_viewer_core as core  # noqa: E402


def stats_ms(values):
    a = np.asarray(values) * 1000.0
    return {k: round(float(v), 4) for k, v in
            (("p50", np.percentile(a, 50)), ("p95", np.percentile(a, 95)),
             ("p99", np.percentile(a, 99)), ("max", a.max()), ("mean", a.mean()))}


def busy_threads(n):
    stop = threading.Event()

    def spin():
        while not stop.is_set():
            sum(i * i for i in range(2000))

    threads = [threading.Thread(target=spin, daemon=True) for _ in range(n)]
    for t in threads:
        t.start()
    return stop, threads


def roundtrip(args):
    out = {"switch_interval_ms": sys.getswitchinterval() * 1000.0, "runs": []}
    for busy in (0, 1, 6):
        stop, threads = busy_threads(busy)
        time.sleep(0.05)
        for name, fn in (("keeps_gil", core.held_spin), ("releases_gil", core.detached_spin)):
            lat = []
            for _ in range(args.calls if busy == 0 else args.calls // 20):
                t = time.perf_counter()
                fn(0)
                lat.append(time.perf_counter() - t)
            row = {"busy_python_threads": busy, "call": name, **stats_ms(lat)}
            out["runs"].append(row)
            print(json.dumps(row), flush=True)
        stop.set()
        for t in threads:
            t.join()
    return out


def heartbeat(args):
    from PyQt5 import QtCore

    app = QtCore.QCoreApplication.instance() or QtCore.QCoreApplication([])
    provider, reader = probe.open_both(args.path)
    jobs = random.Random(5).sample(probe.tile_jobs(provider, 0) + probe.tile_jobs(provider, 1), 3000)
    reader.bench(jobs, 6)                                    # warm the page cache

    # calibrate a pure-Python chunk of GUI "bookkeeping" to ~gui_work_ms alone
    loops = 200000
    t = time.perf_counter()
    sum(i * i for i in range(loops))
    loops = max(1, int(loops * (args.gui_work_ms / 1000.0) / (time.perf_counter() - t)))

    def measure(label, start_load):
        """Tiles count when they FINISHED inside [t0, t1], the interval the
        heartbeat was recorded over, and throughput uses its real length."""
        beats, works, stop, finished = [], [], threading.Event(), []
        load = start_load(stop, finished) if start_load else []

        def beat():
            beats.append(time.perf_counter())
            if args.gui_work_ms > 0:
                t = time.perf_counter()
                sum(i * i for i in range(loops))
                works.append(time.perf_counter() - t)

        timer = QtCore.QTimer()
        timer.setTimerType(QtCore.Qt.PreciseTimer)
        timer.setInterval(4)
        timer.timeout.connect(beat)
        timer.start()
        t0, p0 = time.monotonic(), time.perf_counter()
        while time.perf_counter() - p0 < args.seconds:
            app.processEvents(QtCore.QEventLoop.AllEvents, 50)
        t1 = time.monotonic()
        timer.stop()
        stop.set()
        for t in load:
            t.join()
        gaps = np.diff(beats)
        done = sum(1 for end in list(finished) if t0 <= end <= t1)
        row = {"load": label, "gui_work_ms": args.gui_work_ms, "beats": len(beats),
               "interval": stats_ms(gaps), **({"gui_work": stats_ms(works)} if works else {}),
               "over_20ms": int((gaps > 0.020).sum()),
               "tiles_per_ms": round(done / ((t1 - t0) * 1000.0), 3)}
        print(json.dumps(row), flush=True)
        return row

    # both loads cycle through the SAME 3000 jobs; each records the
    # time.monotonic() at which every tile finished (review fix)
    def python_readers(stop, finished):
        def run(i):
            k = i
            while not stop.is_set():
                probe.python_read(provider, jobs[k % len(jobs)])
                finished.append(time.monotonic())             # list.append: atomic
                k += 6
        threads = [threading.Thread(target=run, args=(i,)) for i in range(6)]
        for t in threads:
            t.start()
        return threads

    def rust_readers(stop, finished):
        def run():
            k = 0
            while not stop.is_set():
                chunk = [jobs[(k + j) % len(jobs)] for j in range(60)]
                _wall, spans, _n, _sum = reader.bench(chunk, 6)   # GIL released inside
                finished.extend(end for _start, end in spans)     # same clock
                k += 60
        t = threading.Thread(target=run)
        t.start()
        return [t]

    out = {"path": args.path, "seconds": args.seconds,
           "runs": [measure("idle", None), measure("python_readers_6", python_readers),
                    measure("rust_readers_6", rust_readers)]}
    provider.close()
    return out


def main():
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("roundtrip")
    r.add_argument("--calls", type=int, default=20000)
    h = sub.add_parser("heartbeat")
    h.add_argument("path")
    h.add_argument("--seconds", type=float, default=5.0)
    h.add_argument("--gui-work-ms", type=float, default=0.0,
                   help="pure-Python work the GUI thread does on every beat")
    for p in (r, h):
        p.add_argument("--out", default="")
    args = ap.parse_args()
    report = roundtrip(args) if args.cmd == "roundtrip" else heartbeat(args)
    if args.out:
        with open(args.out, "w") as fh:
            json.dump(report, fh, indent=1)


if __name__ == "__main__":
    main()
