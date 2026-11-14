"""The Mesmer weight conversion's lock (`prepare._lock`, Windows plan W1):
one holder at a time across processes -- POSIX `flock`, Windows
`msvcrt.locking` -- released when its holder closes the file or exits."""
import os
import subprocess
import sys
import time

from block01.seg_runner.mesmer_torch import prepare

HOLDER = r"""
import sys, time
from block01.seg_runner.mesmer_torch import prepare
f = open(sys.argv[1], "a")
assert prepare._lock(f, wait=False)
print("held", flush=True)
time.sleep(float(sys.argv[2]))
f.close()
"""


def _holder(path, seconds):
    env = dict(os.environ, PYTHONPATH=os.pathsep.join(p for p in sys.path if p))
    p = subprocess.Popen([sys.executable, "-c", HOLDER, str(path), str(seconds)],
                         stdout=subprocess.PIPE, text=True, env=env)
    assert p.stdout.readline().strip() == "held"
    return p


def test_a_second_holder_is_refused_then_waits_for_the_first(tmp_path):
    path = tmp_path / prepare.LOCK_FILE
    first = _holder(path, 1.5)
    try:
        with open(path, "a") as f:
            assert prepare._lock(f, wait=False) is False          # held elsewhere
            t0 = time.monotonic()
            assert prepare._lock(f, wait=True) is True            # after the holder lets go
            assert time.monotonic() - t0 > 0.3
    finally:
        first.wait(timeout=30)


def test_the_lock_is_free_once_its_holder_is_gone(tmp_path):
    path = tmp_path / prepare.LOCK_FILE
    first = _holder(path, 0.1)
    first.wait(timeout=30)
    with open(path, "a") as f:
        assert prepare._lock(f, wait=False) is True
