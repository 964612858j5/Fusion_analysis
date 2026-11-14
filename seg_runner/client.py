"""Parent side: start one engine process, feed it tasks, settle every task.

Every task ends in exactly one terminal state (protocol.TERMINAL), assigned
once and never replaced:
  * the runner answered `result`             -> ok
  * the runner answered `error`              -> failed (its traceback)
  * the user stopped the run                 -> cancelled, for the task in
    flight and every task not yet sent. The stop is recorded BEFORE the
    process is ended, so the process dying afterwards cannot turn these
    into `failed`.
  * the process died without being stopped   -> failed (exit code + stderr tail)

The process runs in its own session (its own process group on Windows), so
ending it ends everything it started (`proc.end_tree`).
"""
import os
import pathlib
import queue
import selectors
import subprocess
import sys
import tempfile
import threading
import time

from . import proc as _proc
from . import protocol

REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent


class EngineStartError(RuntimeError):
    pass


class EngineProcess:
    def __init__(self, engine, python=None, log_path=None, start_timeout=600, cpu_only=False):
        self.engine = engine
        # True: the child sees no GPU, so every engine (torch or TensorFlow)
        # runs on the CPU -- the user's choice in Step2, or a broken GPU setup.
        self.cpu_only = bool(cpu_only)
        self.python = python or sys.executable
        self.log_path = log_path
        self.start_timeout = start_timeout
        self.proc = None
        self.hello = None
        self.states = {}          # task_id -> {"state": ..., "detail": ...}
        self._stopped = False
        self._log = None
        self._buf = b""
        # Windows: `select` takes sockets only, so a thread reads the pipe
        # into this queue (b"" = end of stream); POSIX reads it directly.
        self._chunks = None
        self._eof = False

    # ── lifecycle ────────────────────────────────────────────────────
    def start(self):
        env = dict(os.environ)
        env["PYTHONPATH"] = str(REPO_ROOT) + os.pathsep + env.get("PYTHONPATH", "")
        env.setdefault("KERAS_BACKEND", "tensorflow")
        env.setdefault("TF_CPP_MIN_LOG_LEVEL", "2")
        # an operation the Mac's GPU lacks runs on the CPU instead of failing
        # (read by torch at import, in the child: set before it starts)
        env["PYTORCH_ENABLE_MPS_FALLBACK"] = "1"
        if self.cpu_only:
            env["CUDA_VISIBLE_DEVICES"] = ""
            env["BLOCK01_ENGINE_CPU_ONLY"] = "1"      # engines.CPU_ONLY_ENV: hides MPS too
        else:
            env.pop("BLOCK01_ENGINE_CPU_ONLY", None)
        self._log = open(self.log_path, "ab") if self.log_path else subprocess.DEVNULL
        self.proc = subprocess.Popen(
            [self.python, "-m", "seg_runner.runner", "--engine", self.engine],
            # Not the repository: CUDA libraries drop files such as cufile.log
            # into the working directory. Imports come from PYTHONPATH.
            cwd=tempfile.gettempdir(), env=env, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=self._log, bufsize=0, **_proc.group_kwargs())
        self._buf = b""
        self._eof = False
        if _proc.IS_WINDOWS:
            self._chunks = queue.Queue()
            threading.Thread(target=self._pump, args=(self.proc.stdout.fileno(), self._chunks),
                             name=f"engine-{self.engine}-stdout", daemon=True).start()
        msg = self._read(self.start_timeout)
        if msg is None or msg.get("type") != "hello":
            self.terminate()
            raise EngineStartError(f"{self.engine} did not start: {msg!r}; {self._stderr_tail()}")
        self.hello = msg
        return msg

    def terminate(self, grace=5.0):
        """End the process tree: SIGTERM, then SIGKILL after `grace` seconds
        (Windows: `taskkill /T`, then `/T /F`; `proc.end_tree`)."""
        if self.proc is None:
            return
        left = _proc.end_tree(self.proc, grace)
        if left and self._log not in (None, subprocess.DEVNULL):
            try:
                self._log.write(f"[engine] still alive after the stop: pids {left}\n".encode())
            except (OSError, ValueError):
                pass
        for stream in (self.proc.stdin, self.proc.stdout):
            try:
                stream.close()
            except Exception:  # noqa: BLE001 -- broken pipe after a kill
                pass
        if self._log not in (None, subprocess.DEVNULL):
            self._log.close()
            self._log = None

    def close(self):
        """Normal end: ask for shutdown, then make sure the group is gone."""
        if self.proc is not None and self.proc.poll() is None:
            try:
                self._send(protocol.encode("shutdown"))
                if (self._read(30) or {}).get("type") == "done":
                    self.proc.wait(timeout=30)      # let it exit on its own
            except (BrokenPipeError, OSError, subprocess.TimeoutExpired):
                pass
        self.terminate()

    def stop(self):
        """User Stop. Callable from another thread; `run` settles the tasks."""
        self._stopped = True

    # ── tasks ────────────────────────────────────────────────────────
    def run(self, tasks, on_settle=None):
        """Send `tasks` one at a time; return {task_id: state}. Never raises
        for a task failure -- every task is settled instead."""
        pending = [t["task_id"] for t in tasks]
        for task in tasks:
            if self._stopped:
                break
            tid = task["task_id"]
            # The child runs in another working directory: send absolute paths.
            task = dict(task, input=os.path.abspath(task["input"]),
                        out_dir=os.path.abspath(task["out_dir"]))
            try:
                self._send(protocol.encode("task", **task))
            except (BrokenPipeError, OSError):
                break
            msg = self._wait_answer()
            if msg is None:                      # stopped, or the process died
                break
            if msg.get("task_id") != tid:
                raise protocol.ProtocolError(f"answer for {msg.get('task_id')!r}, expected {tid!r}")
            if msg["type"] == "result":
                self._settle(tid, protocol.OK, msg["record"], on_settle)
            else:
                self._settle(tid, protocol.FAILED, msg.get("message", ""), on_settle)

        unsettled = [tid for tid in pending if tid not in self.states]
        if unsettled:
            if self._stopped:
                # Record the stop first; only then end the process.
                for tid in unsettled:
                    self._settle(tid, protocol.CANCELLED, "stopped by user", on_settle)
                self.terminate()
            else:
                self.terminate()
                detail = f"engine exited with {self.proc.returncode}; {self._stderr_tail()}"
                for tid in unsettled:
                    self._settle(tid, protocol.FAILED, detail, on_settle)
        return {tid: self.states[tid]["state"] for tid in pending}

    def _settle(self, tid, state, detail, on_settle):
        if tid in self.states:                   # a terminal state is final
            return
        self.states[tid] = {"state": state, "detail": detail}
        if on_settle is not None:
            on_settle(tid, state, detail)

    def _wait_answer(self):
        while True:
            if self._stopped:
                return None
            msg = self._read(0.2)
            if msg is not None:
                return msg
            if self.proc.poll() is not None:
                return None

    def _send(self, text):
        self.proc.stdin.write(text.encode("utf-8"))
        self.proc.stdin.flush()

    def _read(self, timeout):
        """One protocol message, or None on timeout / end of stream.

        Reads the raw descriptor into our own buffer: a buffered reader could
        hold a complete line that `select` then never reports.
        """
        if self._chunks is not None:
            return self._read_queued(timeout)
        fd = self.proc.stdout.fileno()
        deadline = time.monotonic() + timeout
        sel = selectors.DefaultSelector()
        sel.register(fd, selectors.EVENT_READ)
        try:
            while True:
                if b"\n" in self._buf:
                    line, self._buf = self._buf.split(b"\n", 1)
                    if line.strip():
                        return protocol.decode(line.decode("utf-8"))
                    continue
                left = deadline - time.monotonic()
                if left <= 0 or not sel.select(left):
                    return None
                chunk = os.read(fd, 65536)
                if not chunk:
                    return None
                self._buf += chunk
        finally:
            sel.close()

    @staticmethod
    def _pump(fd, chunks):
        """Windows: the pipe's bytes into `chunks`, then b"" at its end."""
        try:
            while True:
                chunk = os.read(fd, 65536)
                chunks.put(chunk)
                if not chunk:
                    return
        except OSError:                          # closed by `terminate`
            chunks.put(b"")

    def _read_queued(self, timeout):
        """`_read` on Windows: the same buffer and timeout, fed by `_pump`."""
        deadline = time.monotonic() + timeout
        while True:
            if b"\n" in self._buf:
                line, self._buf = self._buf.split(b"\n", 1)
                if line.strip():
                    return protocol.decode(line.decode("utf-8"))
                continue
            if self._eof:
                return None
            left = deadline - time.monotonic()
            if left <= 0:
                return None
            try:
                chunk = self._chunks.get(timeout=left)
            except queue.Empty:
                return None
            if not chunk:
                self._eof = True
                return None
            self._buf += chunk

    def _stderr_tail(self, n=2000):
        if not self.log_path or not os.path.exists(self.log_path):
            return ""
        with open(self.log_path, "rb") as f:
            f.seek(0, os.SEEK_END)
            f.seek(max(0, f.tell() - n))
            return f.read().decode("utf-8", "replace")
