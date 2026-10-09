"""Memory ledger sampler (block A9 §34 M0): a separate process that, every
PERIOD seconds, records for the process tree rooted at PID its total RSS
(and per-process), its device memory as nvidia-smi reports it (graphics and
compute contexts), and the whole device's used memory -- as JSON lines with
`time.monotonic()` (the perf log's clock).

Usage: a9_memwatch.py PID LOG [PERIOD=0.5]"""

import json
import os
import subprocess
import sys
import time
import xml.etree.ElementTree as ET


def tree(pid):
    out, todo = [], [pid]
    while todo:
        p = todo.pop()
        out.append(p)
        try:
            with open(f"/proc/{p}/task/{p}/children") as f:
                todo.extend(int(c) for c in f.read().split())
        except OSError:
            pass
    return out


def rss_kb(pid):
    try:
        with open(f"/proc/{pid}/status") as f:
            for line in f:
                if line.startswith("VmRSS:"):
                    return int(line.split()[1])
    except OSError:
        return None
    return 0


def gpu():
    xml = subprocess.run(["nvidia-smi", "-q", "-x"], capture_output=True, text=True).stdout
    root = ET.fromstring(xml)
    g = root.find("gpu")
    used = int(g.find("fb_memory_usage/used").text.split()[0])
    procs = {}
    for p in g.findall("processes/process_info"):
        mem = p.find("used_memory").text.split()[0]
        procs[int(p.find("pid").text)] = int(mem) if mem.isdigit() else 0
    return used, procs


def main():
    pid, log = int(sys.argv[1]), sys.argv[2]
    period = float(sys.argv[3]) if len(sys.argv) > 3 else 0.5
    with open(log, "a") as out:
        while os.path.exists(f"/proc/{pid}"):
            t = time.monotonic()
            pids = tree(pid)
            rss = {p: rss_kb(p) for p in pids}
            used, procs = gpu()
            out.write(json.dumps({
                "t": t, "rss_mib": round(sum(v or 0 for v in rss.values()) / 1024, 1),
                "rss_main_mib": round((rss.get(pid) or 0) / 1024, 1),
                "procs": len(pids),
                "gpu_tree_mib": sum(procs.get(p, 0) for p in pids),
                "gpu_main_mib": procs.get(pid, 0),
                "gpu_device_mib": used}) + "\n")
            out.flush()
            time.sleep(max(0.0, period - (time.monotonic() - t)))


if __name__ == "__main__":
    main()
