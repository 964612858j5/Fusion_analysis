"""Sample one thread's kernel state every ~2 ms (block A9 §38): state
(R running / S sleeping / D disk), wchan and the syscall number, with
CLOCK_MONOTONIC stamps (the perf log's clock). Writes TSV lines.
Usage: a9_threadstate.py PID TID OUT
"""
import os
import sys
import time

pid, tid, out = sys.argv[1], sys.argv[2], sys.argv[3]
base = f"/proc/{pid}/task/{tid}"
with open(out, "w") as f:
    while True:
        try:
            stat = open(base + "/stat").read()
            state = stat.rsplit(")", 1)[1].split()[0]
            wchan = open(base + "/wchan").read().strip() or "-"
            try:
                sysc = open(base + "/syscall").read().split()[0]
            except OSError:
                sysc = "?"
        except OSError:
            break
        f.write(f"{time.monotonic():.6f}\t{state}\t{wchan}\t{sysc}\n")
        time.sleep(0.002)
