"""The test suite may not write into the user's own data.

`block01/config.py` points `OUTPUT_DIR` at a real dataset, and a window built
in a test carries that path. Two things went wrong on 2026-09-16 and both are
pinned here:

* a Step1 draft change autosaved `step1_session.json` into the user's project
  and destroyed it. A first guard, written as an autouse fixture, did not
  help: the autosave is a 500 ms QTimer and it fired during the Qt-garbage
  fixture that runs AFTER the monkeypatch came off;
* the proof tests themselves then called `os.remove` on a REAL file to show
  that the refusal worked. Run against a deliberately broken guard, that
  deleted the project's `roi_index.json`.

So the rule this module obeys is the rule it enforces: **the real project is
read-only here**. Its protection is proved by inspection -- it is in the
protected roots, and `guard.under` recognises it -- and every destructive
probe runs against a SYNTHETIC project under `tmp_path`, protected for the
duration by `guard.protecting`. Nothing in this file writes, deletes or
renames a real path, whatever state the guard is in.
"""

import hashlib
import io as io_module
import os
import pathlib

import pytest

from block01.utils import project_write_guard as guard
from block01.utils.project_write_guard import RealProjectWriteRefused

#: Read AT IMPORT: the guard must already be up when this module is collected.
INSTALLED_AT_IMPORT = guard.is_installed()
REAL_OUTPUT_DIR = guard.configured_output_dir()

pytestmark = pytest.mark.skipif(
    not REAL_OUTPUT_DIR,
    reason="no real project configured, so there is nothing to protect")


def _listing(directory):
    """The directory's own entries and their sizes -- a read, and nothing more."""
    root = pathlib.Path(directory)
    if not root.exists():
        return {}
    out = {}
    for entry in sorted(root.iterdir()):
        try:
            out[entry.name] = (entry.is_dir(), entry.stat().st_size)
        except OSError:
            out[entry.name] = (None, None)
    return out


#: What the real project looked like when this module was imported. Asserted
#: unchanged at the end: this suite must leave no trace in it at all.
REAL_LISTING_AT_IMPORT = _listing(REAL_OUTPUT_DIR)


@pytest.fixture
def synthetic_project(tmp_path):
    """A stand-in for the user's project, with a sentinel file in it.

    Everything destructive happens here. The sentinel's bytes are what a
    broken guard would change, so a mutation shows up as a failed assertion
    rather than as damage to real data.
    """
    root = tmp_path / "synthetic-real-project"
    root.mkdir()
    sentinel = root / "roi_index.json"
    sentinel.write_text('{"version": 1, "rois": []}', encoding="utf-8")
    digest = hashlib.sha256(sentinel.read_bytes()).hexdigest()
    with guard.protecting(root):
        yield root, sentinel, digest


class _Loader:
    """The least a window needs to build a Step1 session payload."""

    filepath = "/tmp/guard-probe.ome.tiff"
    shape = (64, 64)

    def __init__(self):
        self._names = ["DAPI", "CD3"]
        self.ch_map = {c: i for i, c in enumerate(self._names)}

    def channel_names(self):
        return list(self._names)

    def read_region(self, channel, y0, y1, x0, x1, downsample=1,
                    normalize=True):
        import numpy as np
        return np.zeros((max(1, y1 - y0), max(1, x1 - x0)), np.float32)


def _drain(app, ms=1200):
    """Run the event loop past the 500 ms autosave timer."""
    from PyQt5 import QtCore
    clock = QtCore.QElapsedTimer()
    clock.start()
    while clock.elapsed() < ms:
        app.processEvents(QtCore.QEventLoop.AllEvents, 50)


# ── the real project: inspected, never touched ────────────────────────

def test_the_guard_is_installed_before_any_test_module_is_imported():
    """A per-test fixture cannot do this, which is why it is not one."""
    assert INSTALLED_AT_IMPORT, "the guard was not installed at collection time"
    assert guard.is_installed()


def test_the_configured_output_dir_is_protected():
    roots = guard.real_project_roots()
    assert roots
    assert pathlib.Path(REAL_OUTPUT_DIR).resolve() in roots
    # A path inside it is recognised -- asked of the guard, not of the disk.
    assert guard.under(os.path.join(REAL_OUTPUT_DIR, "step1_session.json"))
    assert guard.under(os.path.join(REAL_OUTPUT_DIR, "rois", "x", "y.json"))


def test_the_sandbox_and_tmp_path_are_not_protected(tmp_path):
    assert guard.under(tmp_path / "x.json") is None
    assert guard.under(pathlib.Path(guard.sandbox_dir()) / "x.json") is None


def test_the_raw_helpers_refuse_the_real_project():
    """The one bypass in the guard may not be pointed at real data.

    This is the hole the `roi_index.json` deletion went through.
    """
    # NAMES THAT ARE NOT THERE, deliberately. `raw_remove` checks the path
    # before deleting, so even with the refusal mutated away this test can
    # destroy nothing -- which is the lesson of the `roi_index.json` deletion:
    # a probe must be harmless against a broken guard, not merely refused by a
    # working one.
    missing_file = os.path.join(
        REAL_OUTPUT_DIR, "guard-refusal-probe-that-does-not-exist.json")
    missing_dir = os.path.join(
        REAL_OUTPUT_DIR, "guard-refusal-probe-that-does-not-exist")
    assert not os.path.exists(missing_file)
    assert not os.path.exists(missing_dir)
    with pytest.raises(RealProjectWriteRefused):
        guard.raw_remove(missing_file)
    with pytest.raises(RealProjectWriteRefused):
        guard.raw_rmdir(missing_dir)


# ── the refusal, proved on a synthetic project ────────────────────────

def test_writing_into_a_protected_project_is_refused(synthetic_project):
    root, sentinel, digest = synthetic_project
    with pytest.raises(RealProjectWriteRefused) as excinfo:
        with open(sentinel, "w") as handle:          # noqa: SIM115
            handle.write("destroyed")
    message = str(excinfo.value)
    assert "roi_index.json" in message
    assert "test_writing_into_a_protected_project_is_refused" in message
    assert hashlib.sha256(sentinel.read_bytes()).hexdigest() == digest


@pytest.mark.parametrize("door", ["io.open", "pathlib", "os.open"])
def test_the_other_write_entry_points_are_refused_too(synthetic_project, door):
    """`builtins.open` is not the only door into a file."""
    _root, sentinel, digest = synthetic_project
    with pytest.raises(RealProjectWriteRefused):
        if door == "io.open":
            io_module.open(sentinel, "w").close()
        elif door == "pathlib":
            pathlib.Path(sentinel).open("w").close()
        else:
            os.close(os.open(sentinel, os.O_WRONLY | os.O_TRUNC))
    assert hashlib.sha256(sentinel.read_bytes()).hexdigest() == digest


def test_creating_a_directory_in_a_protected_project_is_refused(
        synthetic_project):
    root, _sentinel, _digest = synthetic_project
    target = root / "made_by_a_test"
    with pytest.raises(RealProjectWriteRefused):
        os.makedirs(target, exist_ok=True)
    with pytest.raises(RealProjectWriteRefused):
        pathlib.Path(target).mkdir(parents=True, exist_ok=True)
    assert not target.exists()


def test_deleting_from_a_protected_project_is_refused(synthetic_project):
    _root, sentinel, digest = synthetic_project
    for call in (os.remove, os.unlink):
        with pytest.raises(RealProjectWriteRefused):
            call(sentinel)
    assert hashlib.sha256(sentinel.read_bytes()).hexdigest() == digest


def test_moving_and_copying_into_a_protected_project_are_refused(
        synthetic_project, tmp_path):
    root, sentinel, digest = synthetic_project
    import shutil

    outside = tmp_path / "outside.json"
    outside.write_text("{}", encoding="utf-8")
    with pytest.raises(RealProjectWriteRefused):
        shutil.move(str(outside), str(root / "moved.json"))
    with pytest.raises(RealProjectWriteRefused):
        shutil.copyfile(str(outside), str(sentinel))
    with pytest.raises(RealProjectWriteRefused):
        shutil.rmtree(str(root))
    assert hashlib.sha256(sentinel.read_bytes()).hexdigest() == digest
    assert outside.exists()


def test_a_protected_project_is_still_readable(synthetic_project):
    _root, sentinel, _digest = synthetic_project
    assert "version" in sentinel.read_text(encoding="utf-8")
    with open(sentinel) as handle:                   # noqa: SIM115
        assert handle.read()


def test_outside_a_protecting_block_the_synthetic_project_is_ordinary(tmp_path):
    """The injection is scoped: it protects during the block and no longer."""
    root = tmp_path / "synthetic-real-project"
    root.mkdir()
    probe = root / "file.json"
    with guard.protecting(root):
        with pytest.raises(RealProjectWriteRefused):
            probe.write_text("{}", encoding="utf-8")
    probe.write_text("{}", encoding="utf-8")
    assert probe.read_text(encoding="utf-8") == "{}"


# ── the redirect ──────────────────────────────────────────────────────

def test_output_dir_is_redirected_everywhere_it_was_bound():
    """Including in modules imported AFTER the guard went in."""
    import importlib
    import sys

    from block01 import config

    sandbox = pathlib.Path(guard.sandbox_dir()).resolve()
    real = pathlib.Path(REAL_OUTPUT_DIR).resolve()
    assert pathlib.Path(config.OUTPUT_DIR).resolve() == sandbox

    late = importlib.import_module("block01.ui.main_window")
    assert pathlib.Path(late.OUTPUT_DIR).resolve() == sandbox

    for name, module in list(sys.modules.items()):
        if not name.startswith("block01"):
            continue
        value = getattr(module, "OUTPUT_DIR", None)
        if value:
            assert pathlib.Path(str(value)).resolve() != real, name


def test_a_test_s_own_tmp_path_is_writable(tmp_path):
    (tmp_path / "out").mkdir()
    (tmp_path / "out" / "session.json").write_text("{}", encoding="utf-8")
    assert (tmp_path / "out" / "session.json").read_text(encoding="utf-8") == "{}"


# ── the path that actually did the damage ─────────────────────────────

def test_a_window_s_session_autosave_lands_in_the_redirect(tmp_path):
    """End to end, with the timer allowed to fire.

    The autosave is scheduled on a 500 ms timer, so the check happens after
    the events have been drained for longer than that -- the window the first
    guard leaked through. The assertion is on the REDIRECT (a write that
    landed where it should), never on the real project, which this module
    only ever reads.
    """
    pytest.importorskip("PyQt5")
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PyQt5 import QtWidgets

    from block01.ui import main_window as main_window_module

    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    window = main_window_module.MainWindow()
    try:
        # A payload is only built when there is a loader (`_step1_session_payload`
        # returns None otherwise), and a test that writes nothing proves nothing.
        window.loader = _Loader()
        window._step1_restore_active = False
        window.step0_output = {"output_dir": str(tmp_path / "session-home")}
        window._schedule_step1_session_save()
        window._save_step1_session()
        _drain(app)
    finally:
        window._display.shutdown("test")
        window.close()
        _drain(app)

    # It went where this test told it to, and that place is not protected.
    assert guard.under(str(tmp_path / "session-home")) is None
    assert (tmp_path / "session-home" / "step1_session.json").exists(), (
        "the autosave did not run at all, so this test proves nothing")


# ── and the suite left no trace ───────────────────────────────────────

def test_this_module_left_the_real_project_exactly_as_it_found_it():
    """Runs last (alphabetically last is not a contract -- the listing is).

    Whatever the other tests did, the real project's own entries and their
    sizes are what they were when this module was imported.
    """
    assert _listing(REAL_OUTPUT_DIR) == REAL_LISTING_AT_IMPORT
