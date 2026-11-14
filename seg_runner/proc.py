"""An engine's process tree: started as its own group, ended as a whole
(docs/v17_windows_native_plan.md, phase W1).

POSIX, as before: the engine runs in its own session (`start_new_session`)
and `killpg` ends the group -- SIGTERM, then SIGKILL after the grace time.

Windows has no process groups to signal. The engine starts in its own
group (`CREATE_NEW_PROCESS_GROUP`) and its TREE is ended, root last: the
children are listed (psutil) before anything is ended, then
`taskkill /PID <root> /T` (polite), after the grace time (or at once when
the polite end is refused, as it is for a console process without a
window) `taskkill /T /F`, and the listed children are waited for and,
should one still live, killed. Never `terminate()` on the root first: a
child such as Mesmer's weight conversion (`mesmer_torch/prepare.py`) would
be orphaned, and `/T` would no longer find it.
"""
import os
import signal
import subprocess

IS_WINDOWS = os.name == "nt"
_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)


def group_kwargs():
    """`Popen` keywords that start a process as the root of its own group."""
    if IS_WINDOWS:
        return {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP}
    return {"start_new_session": True}


def _children(pid):
    try:
        import psutil
        return psutil.Process(pid).children(recursive=True)
    except Exception:                                   # noqa: BLE001 -- gone, or no psutil
        return []


def _taskkill(pid, force):
    cmd = ["taskkill", "/PID", str(pid), "/T"] + (["/F"] if force else [])
    return subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                          creationflags=_NO_WINDOW).returncode


def end_tree(proc, grace=5.0):
    """End `proc` (a `Popen` started with `group_kwargs`) and everything it
    started; return once the root has exited. Returns the pids of children
    still alive afterwards (none, normally)."""
    if proc.poll() is not None:
        return []
    if not IS_WINDOWS:
        for sig in (signal.SIGTERM, signal.SIGKILL):
            try:
                os.killpg(proc.pid, sig)
            except ProcessLookupError:
                break
            try:
                proc.wait(timeout=grace)
                break
            except subprocess.TimeoutExpired:
                continue
        proc.wait()
        return []
    children = _children(proc.pid)                    # before anything is ended
    if _taskkill(proc.pid, force=False) == 0:
        try:
            proc.wait(timeout=grace)
        except subprocess.TimeoutExpired:
            pass
    if proc.poll() is None:
        _taskkill(proc.pid, force=True)
    try:
        proc.wait(timeout=grace)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait()
    return _reap(children, grace)


def _reap(children, grace):
    if not children:
        return []
    import psutil
    _gone, alive = psutil.wait_procs(children, timeout=grace)
    for p in alive:
        try:
            p.kill()
        except psutil.Error:
            pass
    _gone, alive = psutil.wait_procs(alive, timeout=grace)
    return [p.pid for p in alive]


def kill_tree_now(proc):
    """End `proc`'s tree at once, without waiting (the GUI thread's Stop while
    the engine still loads); `end_tree` later settles what is left."""
    if proc is None or proc.poll() is not None:
        return
    if not IS_WINDOWS:
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            pass
        return
    try:
        subprocess.Popen(["taskkill", "/PID", str(proc.pid), "/T", "/F"],
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                         creationflags=_NO_WINDOW)
    except OSError:
        proc.kill()
