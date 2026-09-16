"""Refuse, and redirect, writes into the user's own project during tests.

`block01/config.py` hard-codes `OUTPUT_DIR` at a real dataset, and a window
built in a test carries that path: on 2026-09-16 a Step1 draft change reached
`MainWindow._save_step1_session`, which opened
`<real project>/step1_session.json` for writing and destroyed what was there.
No backup existed.

WHY THIS IS A PROCESS-WIDE INSTALL AND NOT A FIXTURE. The first attempt put
both halves in an autouse fixture. A fixture's monkeypatch is undone when the
test ends, and Step1's autosave is a 500 ms QTimer: the Qt-garbage fixture that
runs afterwards calls `processEvents()`, the timer fires THERE -- after the
guard came off -- and the real file was written a second time. So the guard is
installed once in `pytest_configure`, before any test module is imported, and
IS NEVER TAKEN OFF: there is no `pytest_unconfigure` counterpart, because
lifting it at the end of the session would leave the same hole open for
atexit handlers, Qt finalisation and a timer that has not fired yet.
Collection, every fixture's teardown, every Qt event drain and interpreter
shutdown are inside the guard.

A SECOND LESSON, from the same day: the proof tests themselves called
`os.remove` on a REAL file to show the refusal worked, and against a
deliberately broken guard that deleted the project's `roi_index.json`. The
real project is READ-ONLY in the tests now; every destructive probe runs
against a synthetic project registered with `protecting`.

Two layers, because either alone lets something through:

* REDIRECT -- `config.OUTPUT_DIR` is pointed at a session sandbox BEFORE the
  first `block01` submodule is imported, so every later
  `from ..config import OUTPUT_DIR` binds the sandbox by itself, and the
  modules already imported are rewritten in place. Code that writes where it
  always wrote keeps working.
* REFUSE -- every write entry point raises for a path under a real-project
  root, naming the test and the path, so the next unnoticed path is a failure
  instead of a lost file.

Reads are untouched. A test's own `tmp_path` is untouched. Nothing here runs in
production: the package imports it nowhere else.
"""

import builtins
import io
import os
import pathlib
import shutil
import sys

#: Modes that create or change a file.
WRITE_MODES = ("w", "a", "x", "+")

#: Low-level `os.open` flags that mean "for writing".
_WRITE_FLAGS = os.O_WRONLY | os.O_RDWR | os.O_APPEND | os.O_CREAT | os.O_TRUNC


class RealProjectWriteRefused(RuntimeError):
    """A test tried to write into the user's own data."""


_state = {"roots": [], "sandbox": "", "restore": [], "installed": False}

#: The REAL project's roots, kept apart from `_state["roots"]` (which a test
#: may extend with a synthetic project of its own).
_REAL_ROOTS = []

#: The real functions, kept before anything is patched. A test that probes the
#: guard must be able to clean up after itself EVEN WHEN THE GUARD IS BROKEN --
#: a mutation run that removes a door would otherwise leave its probe file in
#: the user's project (it did, on 2026-09-16).
_RAW = {"remove": os.remove, "rmdir": os.rmdir, "exists": os.path.exists}


def _refuse_raw_on_the_real_project(path):
    """`raw_*` exists for a test's own synthetic project, and for nothing else.

    It bypasses the guard, so a real-project path handed to it would be the
    one deletion nothing could stop. On 2026-09-16 a probe that ran with the
    refusal mutated away deleted the project's `roi_index.json`; these two
    helpers must never be able to do that, mutated or not.
    """
    hit = under(path, _REAL_ROOTS)
    if hit is not None:
        raise RealProjectWriteRefused(
            f"raw_remove/raw_rmdir refuses {hit}: it is inside the real "
            "project, and these helpers exist only for a test's own "
            "synthetic project.")


def raw_remove(path):
    """Delete `path` if it is there, bypassing the guard. Never raises.

    Refuses a real-project path outright -- see
    `_refuse_raw_on_the_real_project`.
    """
    _refuse_raw_on_the_real_project(path)
    try:
        if _RAW["exists"](path):
            _RAW["remove"](path)
    except (OSError, ValueError):
        pass


def raw_rmdir(path):
    """Remove an EMPTY directory if it is there, bypassing the guard."""
    _refuse_raw_on_the_real_project(path)
    try:
        if _RAW["exists"](path):
            _RAW["rmdir"](path)
    except (OSError, ValueError):
        pass


def real_project_roots():
    """The protected roots, as read before the redirect. Empty until install."""
    return list(_state["roots"])


def real_output_dir():
    """`config.OUTPUT_DIR` as configured on disk, not as redirected."""
    roots = _state["roots"]
    return str(roots[0]) if roots else ""


def sandbox_dir():
    return _state["sandbox"]


def is_installed():
    """True once `install` has run -- read at import time by the proof tests."""
    return bool(_state["installed"])


def configured_output_dir():
    """`OUTPUT_DIR` as it stands on disk, guard installed or not.

    With the guard in, that is the recorded root; without it, the config's own
    value. A test module must be able to name the real project even when the
    guard failed to install -- that is exactly the case it has to fail on.
    """
    if _state["roots"]:
        return str(_state["roots"][0])
    try:
        from .. import config
    except Exception:
        return ""
    return str(getattr(config, "OUTPUT_DIR", "") or "")


def _read_protected_roots(config):
    roots = []
    for name in ("OUTPUT_DIR", "OME_TIFF_FILE"):
        value = getattr(config, name, None)
        if not value:
            continue
        path = pathlib.Path(str(value))
        roots.append(path if name == "OUTPUT_DIR" else path.parent)
    return [r.resolve() for r in roots if str(r) not in ("", "/")]


def under(path, roots=None):
    """The resolved path when it is inside a protected root, else None."""
    roots = _state["roots"] if roots is None else roots
    if not roots:
        return None
    try:
        resolved = pathlib.Path(os.fspath(path)).resolve()
    except (TypeError, ValueError, OSError):
        return None
    for root in roots:
        if resolved == root or root in resolved.parents:
            return resolved
    return None


def refuse(path, what):
    hit = under(path)
    if hit is None:
        return
    test = os.environ.get("PYTEST_CURRENT_TEST", "<outside a test>")
    raise RealProjectWriteRefused(
        f"{test} tried to {what} {hit} -- inside the real project "
        f"({', '.join(str(r) for r in _state['roots'])}). Tests write to "
        "tmp_path or to the redirected OUTPUT_DIR, never there.")


def _patch(target, name, value):
    _state["restore"].append((target, name, getattr(target, name)))
    setattr(target, name, value)


def _install_refusal():
    real_open = builtins.open
    real_io_open = io.open
    real_os_open = os.open
    real_path_open = pathlib.Path.open
    real_makedirs = os.makedirs
    real_mkdir = os.mkdir
    real_path_mkdir = pathlib.Path.mkdir
    real_rename = os.rename
    real_replace = os.replace
    real_remove = os.remove
    real_unlink = os.unlink
    real_move = shutil.move
    real_rmtree = shutil.rmtree
    real_copy = shutil.copy
    real_copyfile = shutil.copyfile
    real_copytree = shutil.copytree

    def guarded_open(file, mode="r", *args, **kwargs):
        if any(flag in mode for flag in WRITE_MODES):
            refuse(file, "open for writing")
        return real_open(file, mode, *args, **kwargs)

    def guarded_io_open(file, mode="r", *args, **kwargs):
        if any(flag in mode for flag in WRITE_MODES):
            refuse(file, "open for writing")
        return real_io_open(file, mode, *args, **kwargs)

    def guarded_os_open(path, flags, *args, **kwargs):
        if flags & _WRITE_FLAGS:
            refuse(path, "open for writing")
        return real_os_open(path, flags, *args, **kwargs)

    def guarded_path_open(self, mode="r", *args, **kwargs):
        if any(flag in mode for flag in WRITE_MODES):
            refuse(self, "open for writing")
        return real_path_open(self, mode, *args, **kwargs)

    def guarded_makedirs(name, *args, **kwargs):
        refuse(name, "create")
        return real_makedirs(name, *args, **kwargs)

    def guarded_mkdir(path, *args, **kwargs):
        refuse(path, "create")
        return real_mkdir(path, *args, **kwargs)

    def guarded_path_mkdir(self, *args, **kwargs):
        refuse(self, "create")
        return real_path_mkdir(self, *args, **kwargs)

    def guarded_rename(src, dst, *args, **kwargs):
        refuse(dst, "rename onto")
        refuse(src, "rename away")
        return real_rename(src, dst, *args, **kwargs)

    def guarded_replace(src, dst, *args, **kwargs):
        refuse(dst, "replace")
        refuse(src, "replace away")
        return real_replace(src, dst, *args, **kwargs)

    def guarded_remove(path, *args, **kwargs):
        refuse(path, "delete")
        return real_remove(path, *args, **kwargs)

    def guarded_unlink(path, *args, **kwargs):
        refuse(path, "delete")
        return real_unlink(path, *args, **kwargs)

    def guarded_move(src, dst, *args, **kwargs):
        refuse(dst, "move onto")
        refuse(src, "move away")
        return real_move(src, dst, *args, **kwargs)

    def guarded_rmtree(path, *args, **kwargs):
        refuse(path, "delete the tree at")
        return real_rmtree(path, *args, **kwargs)

    def guarded_copy(src, dst, *args, **kwargs):
        refuse(dst, "copy onto")
        return real_copy(src, dst, *args, **kwargs)

    def guarded_copyfile(src, dst, *args, **kwargs):
        refuse(dst, "copy onto")
        return real_copyfile(src, dst, *args, **kwargs)

    def guarded_copytree(src, dst, *args, **kwargs):
        refuse(dst, "copy a tree onto")
        return real_copytree(src, dst, *args, **kwargs)

    _patch(builtins, "open", guarded_open)
    _patch(io, "open", guarded_io_open)
    _patch(os, "open", guarded_os_open)
    _patch(pathlib.Path, "open", guarded_path_open)
    _patch(os, "makedirs", guarded_makedirs)
    _patch(os, "mkdir", guarded_mkdir)
    _patch(pathlib.Path, "mkdir", guarded_path_mkdir)
    _patch(os, "rename", guarded_rename)
    _patch(os, "replace", guarded_replace)
    _patch(os, "remove", guarded_remove)
    _patch(os, "unlink", guarded_unlink)
    _patch(shutil, "move", guarded_move)
    _patch(shutil, "rmtree", guarded_rmtree)
    _patch(shutil, "copy", guarded_copy)
    _patch(shutil, "copyfile", guarded_copyfile)
    _patch(shutil, "copytree", guarded_copytree)


def install(sandbox):
    """Install both layers for the whole process. Idempotent.

    `sandbox` is where the redirected `OUTPUT_DIR` points -- one directory for
    the session, created here.
    """
    if _state["installed"]:
        return _state
    from .. import config

    _state["roots"] = _read_protected_roots(config)
    del _REAL_ROOTS[:]
    _REAL_ROOTS.extend(_state["roots"])
    sandbox = pathlib.Path(sandbox)
    sandbox.mkdir(parents=True, exist_ok=True)
    _state["sandbox"] = str(sandbox)

    # BEFORE the refusal, and before any test module is imported: a module that
    # does `from ..config import OUTPUT_DIR` later binds this value by itself.
    _patch(config, "OUTPUT_DIR", str(sandbox))
    for name, module in list(sys.modules.items()):
        if name.startswith("block01") and getattr(module, "OUTPUT_DIR", None):
            _patch(module, "OUTPUT_DIR", str(sandbox))

    _install_refusal()
    _state["installed"] = True
    return _state


class protecting:
    """Protect a synthetic project for the duration of a `with` block.

    Every destructive probe belongs here rather than on the real project: a
    test creates `tmp_path/"synthetic-real-project"`, puts a sentinel in it,
    and proves the guard refuses writes to it. The real roots stay protected
    throughout, and `raw_*` still refuses them.
    """

    def __init__(self, *roots):
        self._added = [pathlib.Path(str(r)).resolve() for r in roots]

    def __enter__(self):
        _state["roots"] = list(_state["roots"]) + self._added
        return self

    def __exit__(self, *_exc):
        _state["roots"] = [r for r in _state["roots"] if r not in self._added]
        return False


def uninstall():
    """Put everything back.

    NOT part of the normal pytest lifecycle: `conftest.py` installs the guard
    and never removes it, so the protection outlives the last test and the
    interpreter's own shutdown. This exists for a caller that installed the
    guard itself -- a benchmark script, a REPL session -- and for the mutation
    runs that have to prove the guard is what is doing the work.
    """
    for target, name, value in reversed(_state["restore"]):
        try:
            setattr(target, name, value)
        except Exception:
            pass
    _state["restore"] = []
    _state["installed"] = False
