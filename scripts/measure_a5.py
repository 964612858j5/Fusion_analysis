"""A5 resource recorder (plan v2.3 §7.1): sample a running FusionFlux process.

Not imported by the product. Attach it to the app's PID while the user works
(viewer browsing, Step2, Step4 on the representative dataset); it writes a
timeline CSV and, on Ctrl-C or when the process ends, a summary JSON with the
peaks per phase.

    python -m block01.scripts.measure_a5 --pid <PID> --out <dir>
    # mark a phase from another terminal:
    python -m block01.scripts.measure_a5 --mark <dir> "step2 start"

Recorded per sample (every --interval s): RSS and VMS of the process and its
children, system available memory, swap used, and the process's GPU memory
(nvidia-smi, if present). Windows-side working set / commit / page-file
growth cannot be read from WSL; the user reads them on Windows.
"""

import argparse
import csv
import json
import os
import subprocess
import time

import signal

import psutil

_STOP = []


def _request_stop(*_a):
    _STOP.append(True)


def _gpu_mb(pids):
    try:
        out = subprocess.run(
            ["nvidia-smi", "--query-compute-apps=pid,used_memory",
             "--format=csv,noheader,nounits"],
            capture_output=True, text=True, timeout=5).stdout
    except (OSError, subprocess.SubprocessError):
        return None
    total, seen = 0, False
    for line in out.strip().splitlines():
        parts = [p.strip() for p in line.split(",")]
        if len(parts) == 2 and parts[0].isdigit() and parts[1].isdigit():
            seen = True
            if int(parts[0]) in pids:
                total += int(parts[1])
    if not seen:
        # WSL often reports no per-process rows: fall back to the device total
        try:
            out = subprocess.run(
                ["nvidia-smi", "--query-gpu=memory.used", "--format=csv,noheader,nounits"],
                capture_output=True, text=True, timeout=5).stdout
            return float(out.strip().splitlines()[0])
        except (OSError, subprocess.SubprocessError, IndexError, ValueError):
            return None
    return float(total)


def _mark(out_dir, label):
    with open(os.path.join(out_dir, "marks.txt"), "a", encoding="utf-8") as f:
        f.write(f"{time.time():.3f}\t{label}\n")
    print(f"marked: {label}")


def _record(pid, out_dir, interval):
    os.makedirs(out_dir, exist_ok=True)
    proc = psutil.Process(pid)
    path = os.path.join(out_dir, "timeline.csv")
    rows = []
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["t", "rss_mb", "vms_mb", "children_rss_mb", "avail_mb",
                    "swap_used_mb", "gpu_mb"])
        print(f"recording pid {pid} -> {path} (Ctrl-C to stop)")
        signal.signal(signal.SIGINT, _request_stop)
        signal.signal(signal.SIGTERM, _request_stop)
        try:
            while proc.is_running() and not _STOP:
                try:
                    kids = proc.children(recursive=True)
                    mem = proc.memory_info()
                except psutil.Error:
                    break                       # the process itself is gone
                kid_rss = 0
                for k in kids:                  # a child may end between the two calls
                    try:
                        kid_rss += k.memory_info().rss
                    except psutil.Error:
                        pass
                vm, sw = psutil.virtual_memory(), psutil.swap_memory()
                gpu = _gpu_mb({pid} | {k.pid for k in kids})
                row = [round(time.time(), 3), mem.rss / 2**20, mem.vms / 2**20,
                       kid_rss / 2**20, vm.available / 2**20, sw.used / 2**20, gpu]
                w.writerow(row)
                f.flush()
                rows.append(row)
                time.sleep(interval)
        except KeyboardInterrupt:
            pass
    _summary(out_dir, rows)


def _summary(out_dir, rows):
    marks = []
    mpath = os.path.join(out_dir, "marks.txt")
    if os.path.exists(mpath):
        for line in open(mpath, encoding="utf-8"):
            t, _tab, label = line.rstrip("\n").partition("\t")
            marks.append((float(t), label))
    edges = [(rows[0][0] if rows else 0.0, "start")] + marks + [(float("inf"), "end")]
    phases = []
    for (t0, label), (t1, _next) in zip(edges, edges[1:]):
        sel = [r for r in rows if t0 <= r[0] < t1]
        if not sel:
            continue

        def peak(i):
            vals = [r[i] for r in sel if r[i] is not None]
            return round(max(vals), 1) if vals else None
        phases.append({"phase": label, "seconds": round(sel[-1][0] - sel[0][0], 1),
                       "peak_rss_mb": peak(1), "peak_children_rss_mb": peak(3),
                       "min_available_mb": round(min(r[4] for r in sel), 1),
                       "peak_swap_used_mb": peak(5), "peak_gpu_mb": peak(6)})
    out = {"samples": len(rows), "phases": phases}
    with open(os.path.join(out_dir, "summary.json"), "w", encoding="utf-8") as f:
        json.dump(out, f, indent=2)
    print(json.dumps(out, indent=2))


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--pid", type=int)
    ap.add_argument("--out", default="a5_record")
    ap.add_argument("--interval", type=float, default=0.5)
    ap.add_argument("--mark", nargs=2, metavar=("DIR", "LABEL"))
    args = ap.parse_args()
    if args.mark:
        _mark(*args.mark)
        return
    if not args.pid:
        ap.error("--pid is required (or --mark DIR LABEL)")
    _record(args.pid, args.out, args.interval)


if __name__ == "__main__":
    main()
