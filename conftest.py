"""Repo-root pytest bootstrap: make `import block01.*` resolve to THIS tree.

The package is imported as `block01`, but this checkout may live in a
directory with another name (block01_v14, a git worktree, ...) while an
older checkout named `block01` sits on sys.path. Without this shim, tests
either fail to collect (ModuleNotFoundError) or — worse — silently import
the OLD checkout's code.

This registers a `block01` alias module bound to this directory before any
test imports run, shadowing any other candidate. Tests are then reproducible
with plain:

    python -m pytest tests/<file>.py -q

from the repo root, no symlinks or PYTHONPATH needed.
"""

import importlib.util
import pathlib
import sys

_ROOT = pathlib.Path(__file__).resolve().parent


def _register_block01_alias():
    existing = sys.modules.get("block01")
    if existing is not None:
        path = getattr(existing, "__file__", "") or ""
        if pathlib.Path(path).resolve().parent == _ROOT:
            return                      # already this tree
        raise RuntimeError(
            f"'block01' already imported from {path!r}, not from {_ROOT}; "
            "refusing to run tests against the wrong checkout.")
    spec = importlib.util.spec_from_file_location(
        "block01", _ROOT / "__init__.py",
        submodule_search_locations=[str(_ROOT)])
    mod = importlib.util.module_from_spec(spec)
    sys.modules["block01"] = mod
    spec.loader.exec_module(mod)


_register_block01_alias()


# ── Qt garbage is collected BETWEEN tests, never inside a paint ─────────────
#
# Controllers, views and fake schedulers reference each other (a scheduler
# keeps bound-method callbacks, a ViewBox keeps a bound-method slot), so a
# test's Qt objects die in a CYCLIC garbage-collection pass, not by refcount
# at the end of the test. Left to the allocator, that pass fired inside the
# next test's `ImageItem.paint`, destroying still-shown top-level widgets
# while another widget was painting -- a segfault in `QPainter::drawImage`
# (native backtrace: drawImage -> jump to a garbage address). This is the
# "pre-existing pyqtgraph/offscreen crash" of the Step0 suites. Forcing the
# collection here, with no paint in progress, makes the destruction
# deterministic and safe.

import pytest as _pytest


@_pytest.fixture(autouse=True)
def _collect_qt_garbage_between_tests():
    yield
    try:
        from PyQt5 import QtWidgets
    except Exception:
        return
    app = QtWidgets.QApplication.instance()
    if app is None:
        return
    import gc
    # Flush FIRST: a queued slot invocation already posted to a receiver
    # that the collection then destroys would be delivered to a dead
    # object (native backtrace: PyQtSlot::call -> segfault). Then collect,
    # then flush the deferred deletes the collection posted.
    app.processEvents()
    app.processEvents()
    gc.collect()
    app.processEvents()
    gc.collect()
    app.processEvents()


# ── NO TEST MAY WRITE INTO A REAL PROJECT ──────────────────────────────────
#
# `block01/config.py` hard-codes `OUTPUT_DIR` at the user's own dataset, and a
# window built from it autosaves there: on 2026-09-16 a Step1 draft change in a
# test overwrote `<real project>/step1_session.json` and destroyed it. Thirty
# suites build a `MainWindow` and only a handful stubbed that autosave.
#
# INSTALLED FOR THE WHOLE PROCESS, in `pytest_configure` -- not as a fixture.
# The first attempt was an autouse fixture, and it leaked: Step1's autosave is
# a 500 ms QTimer, the fixture's monkeypatch came off at the end of the test,
# and the Qt-garbage fixture's `processEvents()` then fired the timer with no
# guard in place. The real file was written a second time. Installing at
# configure time puts collection, every teardown, every Qt event drain and
# interpreter shutdown inside the guard.
#
# The machinery is `block01/utils/project_write_guard.py`, in the package so
# that a test and the guard share ONE exception class: a root `conftest.py`
# inside a package is imported under the package's name, and a test doing
# `from conftest import ...` would get a second, unrelated copy of it.

import tempfile as _tempfile  # noqa: E402

from block01.utils import project_write_guard as _guard  # noqa: E402

RealProjectWriteRefused = _guard.RealProjectWriteRefused

_SANDBOX = _tempfile.mkdtemp(prefix="block01-test-output-")


def pytest_configure(config):
    """Guard first, before a single test module is imported.

    THERE IS NO `pytest_unconfigure` COUNTERPART. Uninstalling at the end of
    the session would take the guard off while the interpreter is still
    running -- atexit handlers, Qt objects being finalised, a QTimer that has
    not fired yet -- which is the same class of hole the per-test fixture had.
    The patches are process-wide and stay until the process is gone.
    """
    _guard.install(_SANDBOX)
