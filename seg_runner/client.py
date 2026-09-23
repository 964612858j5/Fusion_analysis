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

The process runs in its own session, so ending it ends everything it started.
"""
import os
import pathlib
import selectors
import signal
import subprocess
import sys
import tempfile
import time

from . import protocol

REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent


class EngineStartError(RuntimeError):
    pass


class EngineProcess:
    def __init__(self, engine, python=None, log_path=None, start_timeout=600):
        self.engine = engine
        self.python = python or sys.executable
        self.log_path = log_path
        self.start_timeout = start_timeout
        self.proc = None
        self.hello = None
        self.states = {}          # task_id -> {"state": ..., "detail": ...}
        self._stopped = False
        self._log = None
        self._buf = b""

    # ── lifecycle ────────────────────────────────────────────────────
    def start(self):
        env = dict(os.environ)
        env["PYTHONPATH"] = str(REPO_ROOT) + os.pathsep + env.get("PYTHONPATH", "")
        env.setdefault("KERAS_BACKEND", "tensorflow")
        env.setdefault("TF_CPP_MIN_LOG_LEVEL", "2")
        self._log = open(self.log_path, "ab") if self.log_path else subprocess.DEVNULL
        self.proc = subprocess.Popen(
            [self.python, "-m", "seg_runner.runner", "--engine", self.engine],
            # Not the repository: CUDA libraries drop files such as cufile.log
            # into the working directory. Imports come from PYTHONPATH.
            cwd=tempfile.gettempdir(), env=env, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=self._log, bufsize=0, start_new_session=True)
        self._buf = b""
        msg = self._read(self.start_timeout)
        if msg is None or msg.get("type") != "hello":
            self.terminate()
            raise EngineStartError(f"{self.engine} did not start: {msg!r}; {self._stderr_tail()}")
        self.hello = msg
        return msg

    def terminate(self, grace=5.0):
        """End the process group: SIGTERM, then SIGKILL after `grace` seconds."""
        if self.proc is None:
            return
        if self.proc.poll() is None:
            for sig in (signal.SIGTERM, signal.SIGKILL):
                try:
                    os.killpg(self.proc.pid, sig)
                except ProcessLookupError:
                    break
                try:
                    self.proc.wait(timeout=grace)
                    break
                except subprocess.TimeoutExpired:
                    continue
        self.proc.wait()
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

    def _stderr_tail(self, n=2000):
        if not self.log_path or not os.path.exists(self.log_path):
            return ""
        with open(self.log_path, "rb") as f:
            f.seek(0, os.SEEK_END)
            f.seek(max(0, f.tell() - n))
            return f.read().decode("utf-8", "replace")
