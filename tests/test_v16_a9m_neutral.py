"""Block A9-M: the measuring tools change nothing the user sees.

With tracing off nothing is emitted and no probe exists; with it on, the
Step0 view paints the same pixels, reads the same pixels and leaves the
same camera; the timing decorator returns what the method returns."""

import os
import time

import numpy as np
import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
pytest.importorskip("PyQt5")
pytest.importorskip("tifffile")

from PyQt5 import QtWidgets  # noqa: E402

from block01.utils import perf_trace  # noqa: E402
from block01.viewer import read_ledger  # noqa: E402
from test_v16_pixel_source import _data, _write  # noqa: E402


@pytest.fixture(scope="module")
def app():
    return QtWidgets.QApplication.instance() or QtWidgets.QApplication([])


@pytest.fixture
def slide(tmp_path):
    path = tmp_path / "n.ome.tif"
    _write(path, _data("uint8"), 3, (64, 64))
    return str(path)


def test_timed_is_a_plain_call_when_tracing_is_off(monkeypatch):
    spans = []
    monkeypatch.setattr(perf_trace, "span", lambda ev, **k: spans.append(ev) or _Null())

    @perf_trace.timed("x.y")
    def f(a, b=2):
        return a + b
    monkeypatch.delenv("BLOCK01_PERF", raising=False)
    assert f(1, b=3) == 4 and spans == []
    monkeypatch.setenv("BLOCK01_PERF", "1")
    assert f(1) == 3 and spans == ["x.y"]


class _Null:
    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def _frame(app, slide, monkeypatch, perf):
    from block01.ui.step0.step0_explore_tab import build_default_stack
    if perf:
        monkeypatch.setenv("BLOCK01_PERF", "1")
    else:
        monkeypatch.delenv("BLOCK01_PERF", raising=False)
    read_ledger.reset()
    stack = build_default_stack(slide, 0)
    try:
        stack.view.resize(320, 240)
        stack.view.show()
        end = time.monotonic() + 3.0
        while time.monotonic() < end:
            app.processEvents()
            time.sleep(0.02)
        assert (stack.controller._a9_probe is not None) == perf
        image = stack.view.graphics.grab().toImage()
        ptr = image.constBits()
        ptr.setsize(image.byteCount())
        pixels = np.frombuffer(ptr, np.uint8).copy()
        camera = stack.view.view_box.viewRange()
        return pixels, camera, read_ledger.snapshot().get("raw", 0)
    finally:
        stack.view.hide()
        try:
            stack.teardown()
        except Exception:
            pass


def test_the_step0_view_is_the_same_with_tracing_on(app, slide, monkeypatch, tmp_path):
    monkeypatch.setenv("BLOCK01_PERF_LOG", str(tmp_path / "perf.log"))
    off_px, off_cam, off_reads = _frame(app, slide, monkeypatch, perf=False)
    on_px, on_cam, on_reads = _frame(app, slide, monkeypatch, perf=True)
    assert np.array_equal(off_px, on_px)
    assert off_cam == on_cam
    assert off_reads == on_reads and off_reads > 0
