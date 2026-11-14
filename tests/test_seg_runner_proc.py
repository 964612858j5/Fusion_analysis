"""seg_runner/proc.py and the engine client's two readers (Windows plan W1).

A tiny fake engine instead of a model: a Python child that answers like the
runner, or starts a child of its own and sleeps. Runs on Linux, the Mac and
the Windows job; the queued reader (Windows' way) is exercised everywhere.
"""
import os
import queue
import subprocess
import sys
import threading
import time

import psutil
import pytest

from block01.seg_runner import proc as seg_proc
from block01.seg_runner import protocol
from block01.seg_runner.client import EngineProcess

# answers one hello, then one result per task line, then "done" on shutdown
FAKE_ENGINE = r"""
import json, sys
def send(kind, **f):
    m = {"protocol_version": %d, "type": kind}; m.update(f)
    sys.stdout.write(json.dumps(m) + "\n"); sys.stdout.flush()
send("hello", engine="fake")
for line in sys.stdin:
    msg = json.loads(line)
    if msg["type"] == "shutdown":
        break
    send("result", task_id=msg["task_id"], record="r.json")
send("done")
""" % protocol.PROTOCOL_VERSION

# starts a grandchild that sleeps, prints its pid, then sleeps itself
PARENT_OF_A_SLEEPER = r"""
import subprocess, sys, time
child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(120)"])
print(child.pid, flush=True)
time.sleep(120)
"""


def _start(code, queued):
    ep = EngineProcess("fake")
    ep.proc = subprocess.Popen([sys.executable, "-c", code], stdin=subprocess.PIPE,
                               stdout=subprocess.PIPE, bufsize=0, **seg_proc.group_kwargs())
    if queued:                                   # Windows' reader, on any system
        ep._chunks = queue.Queue()
        threading.Thread(target=ep._pump, args=(ep.proc.stdout.fileno(), ep._chunks),
                         daemon=True).start()
    return ep


def _gone(pid, timeout=10.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        try:
            if psutil.Process(pid).status() == psutil.STATUS_ZOMBIE:
                return True
        except psutil.NoSuchProcess:
            return True
        time.sleep(0.05)
    return False


@pytest.mark.parametrize("queued", [False, True] if os.name != "nt" else [True])
def test_start_reply_and_a_clean_close(queued):
    ep = _start(FAKE_ENGINE, queued)
    try:
        assert ep._read(30)["type"] == "hello"
        states = ep.run([{"task_id": "t1", "input": "a", "out_dir": "."},
                         {"task_id": "t2", "input": "b", "out_dir": "."}])
        assert states == {"t1": protocol.OK, "t2": protocol.OK}
    finally:
        ep.close()
    assert ep.proc.returncode is not None


@pytest.mark.parametrize("queued", [False, True] if os.name != "nt" else [True])
def test_a_read_times_out_and_then_ends_at_the_end_of_the_stream(queued):
    ep = _start("import time, sys; time.sleep(1.0); sys.exit(0)", queued)
    try:
        t0 = time.monotonic()
        assert ep._read(0.3) is None                       # nothing yet: the timeout
        assert time.monotonic() - t0 < 0.9
        assert ep._read(30) is None                        # the end of the stream
    finally:
        ep.terminate()


def test_a_stop_ends_the_child_of_the_child_too():
    ep = _start(PARENT_OF_A_SLEEPER, queued=False)        # the pid read here directly
    line = b""
    while not line.endswith(b"\n"):
        line += os.read(ep.proc.stdout.fileno(), 64)
    grandchild = int(line)
    try:
        assert psutil.pid_exists(grandchild)
        t0 = time.monotonic()
        left = seg_proc.end_tree(ep.proc, grace=5.0)
        assert left == []
        assert ep.proc.poll() is not None
        assert _gone(grandchild), "the engine's own child outlived the stop"
        assert time.monotonic() - t0 < 15
    finally:
        try:
            psutil.Process(grandchild).kill()
        except psutil.Error:
            pass
        ep.terminate()


def test_a_kill_while_the_model_loads_ends_the_wait_for_hello():
    ep = _start("import time; time.sleep(120)", queued=os.name == "nt")   # never says hello
    try:
        threading.Timer(0.5, seg_proc.kill_tree_now, args=(ep.proc,)).start()
        t0 = time.monotonic()
        assert ep._read(60) is None                        # EOF, not the 60 s
        assert time.monotonic() - t0 < 20
    finally:
        ep.terminate()
    assert ep.proc.returncode is not None


def test_group_kwargs_make_the_engine_its_own_group():
    kw = seg_proc.group_kwargs()
    if os.name == "nt":
        assert kw == {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP}
    else:
        assert kw == {"start_new_session": True}
