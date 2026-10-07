"""Block A9-M: the baseline driver -- loaded only on request, plays a
scenario, counts camera writes without changing them."""

import os
import time

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
pytest.importorskip("PyQt5")
from PyQt5 import QtCore, QtWidgets  # noqa: E402

from block01.scripts import a9_drive  # noqa: E402


@pytest.fixture(scope="module")
def app():
    return QtWidgets.QApplication.instance() or QtWidgets.QApplication([])


def test_repeats_are_flattened_with_marks():
    flat = a9_drive._flatten([{"do": "repeat", "n": 2, "body": [{"do": "pause", "ms": 1}]}])
    assert [a["do"] for a in flat] == ["mark", "pause", "mark", "pause"]


def test_nothing_is_attached_without_the_variable(monkeypatch):
    monkeypatch.delenv(a9_drive.ENV, raising=False)
    assert a9_drive.attach(QtCore.QObject()) is None


def test_the_default_scenario_is_valid_json():
    import json
    with open(a9_drive.DEFAULT_SCENARIO, encoding="utf-8") as f:
        actions = a9_drive._flatten(json.load(f))
    kinds = {a["do"] for a in actions}
    assert {"drag", "wheel", "step", "settle", "tick", "window"} <= kinds
    assert all(hasattr(a9_drive.Driver, f"_do_{k}") for k in kinds - {"open"})
    assert all(hasattr(a9_drive.Driver, f"_do_{k}") for k in kinds)


class _Owner:
    def __init__(self):
        self.calls = []

    def user_navigated(self, step, snap):
        self.calls.append("user")

    def jump(self, step, snap):
        self.calls.append("jump")


class _Window(QtCore.QObject):
    def __init__(self):
        super().__init__()
        self.step0_output = {"step0_manifest_path": "/x/m.json"}
        self._camera_owner = _Owner()
        self.went = []

    def _go_to_step1(self):
        self.went.append(1)
        self._camera_owner.jump(1, None)


def test_a_scenario_plays_and_camera_writes_are_counted_not_changed(app):
    w = _Window()
    d = a9_drive.Driver(w, [{"do": "mark", "label": "x"}, {"do": "step", "to": 1},
                            {"do": "pause", "ms": 10}])
    d._wait_for_project = lambda: d._step()               # no 3 s start delay
    d.start()
    end = time.monotonic() + 3
    while time.monotonic() < end and d.i < 3:
        app.processEvents()
        time.sleep(0.01)
    for _ in range(20):
        app.processEvents()
        time.sleep(0.005)
    assert w.went == [1]
    assert d.camera == {"user": 0, "jump": 1}
    assert w._camera_owner.calls == ["jump"]             # the real method still ran


class _Shown(_Window):
    """A window that can say whether it is on screen."""

    def __init__(self, minimized=False):
        super().__init__()
        self.minimized = minimized

    def isMinimized(self):  # noqa: N802
        return self.minimized

    def windowHandle(self):  # noqa: N802
        return None


def test_a_run_whose_window_stopped_painting_is_stopped_as_invalid(app, monkeypatch):
    """User ruling 2026-10-07: a run whose window stopped painting (WSLg
    gives a minimised or covered window no frames) measures nothing -- three
    settles in a row with no new frame stop it, as does a minimised window."""
    monkeypatch.delenv("BLOCK01_A9_QUIT", raising=False)
    d = a9_drive.Driver(_Shown(), [{"do": "mark", "label": str(i)} for i in range(9)])
    d.i = 2
    assert not d._run_is_blind("cpu", done=False, presented=False)
    assert not d._run_is_blind("cpu", done=False, presented=False)
    for _ in range(5):              # codex: a no-op mode/tick draws nothing
        assert not d._run_is_blind("cpu", done=False, presented=False, prior="mode")
    assert not d._run_is_blind("cpu", done=True, presented=True), "a frame resets it"
    assert not d._run_is_blind("cpu", done=False, presented=False)
    assert not d._run_is_blind("cpu", done=False, presented=False)
    assert d._run_is_blind("cpu", done=False, presented=False)
    assert d._invalid == "no_frames" and d.i == len(d.actions)

    m = a9_drive.Driver(_Shown(minimized=True), [{"do": "mark", "label": "x"}])
    assert m._run_is_blind("cpu", done=True, presented=True)
    assert m._invalid == "minimized"
