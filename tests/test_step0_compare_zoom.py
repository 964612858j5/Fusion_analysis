"""The three compare panels are ONE camera, moved synchronously, and a
recompute of the region on screen keeps it.

The complaint this design answers was about motion: zoom "one frame at a
time", a visible lag between the three panels while dragging. Both are
properties of a design that FETCHES on a range change. These panels do not.
Each holds one in-memory array, so a wheel step is a `setRange` on three
ViewBoxes and a repaint -- which is why the mirroring can be done inside the
event that caused it, with no timer, no debounce and no frame in which the
three may disagree.

That is what this module measures rather than describes: ranges compared for
equality immediately after the handler returns, and the handler timed on
arrays the size of a real virtual patch.

Own module: the page-heavy Step0 suites crash pyqtgraph offscreen when
combined with the background-correction module in one process.
"""

import os
import time

import numpy as np
import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

pytest.importorskip("PyQt5")

import pyqtgraph as pg  # noqa: E402
from PyQt5 import QtCore, QtTest  # noqa: E402

from block01.ui.step0 import step0_page as sp  # noqa: E402

from test_step0_background_correction_tab import _GpuPathLoader, app  # noqa: E402,F401


# ── the page, with a virtual patch on it ─────────────────────────────────

def _payload(h=64, w=64, value=500.0):
    m = np.full((h, w), value, np.float32)
    metrics = {"snr": 4.0, "bg_cv": 0.25}
    return {"original_raw": m, "tophat_raw": m * 0.5, "cucim_raw": m * 0.8,
            "original_metrics": metrics, "tophat_metrics": metrics,
            "cucim_metrics": metrics, "nucleus_raw": None}


def _page(app, payload=None):
    page = sp.Step0Page()
    page.loader = _GpuPathLoader()
    page.patches = [(0, 64, 0, 64)]
    page.current_patch_idx = 0
    page.nucleus_channel = "DAPI"
    page._rebuild_channel_list()
    page.current_channel = "CD3"
    page.resize(1500, 900)
    page.show()
    page._set_compare_mode(True)
    QtTest.QTest.qWait(50)
    if payload is not None:
        page._compare_region = (0.0, 0.0, 64.0, 64.0)
        page._compare_fitted = False
        page._apply_compare_payload(payload)
        QtTest.QTest.qWait(30)
    return page


# ── fake input events, so a gesture can be delivered without a mouse ─────

class _Wheel:
    """The three things `ViewBox.wheelEvent` asks of a wheel event."""

    def __init__(self, pos, delta=120):
        self._pos, self._delta = pg.Point(pos), delta
        self.accepted = False

    def delta(self):
        return self._delta

    def pos(self):
        return self._pos

    def accept(self):
        self.accepted = True


class _Drag:
    """The surface `ViewBox.mouseDragEvent` uses for a left-button pan."""

    def __init__(self, pos, last_pos):
        self._pos, self._last = pg.Point(pos), pg.Point(last_pos)

    def accept(self):
        pass

    def pos(self):
        return self._pos

    def lastPos(self):
        return self._last

    def buttonDownPos(self, _btn=None):
        return self._last

    def button(self):
        return QtCore.Qt.LeftButton

    def isFinish(self):
        return False


def _ranges(page):
    return [vb.viewRange() for vb in page._preview_vbs]


def _same(a, b, atol=1e-6):
    return np.allclose(np.asarray(a, float), np.asarray(b, float),
                       rtol=0, atol=atol)


def _all_agree(page, atol=1e-9):
    """The three panels report ONE camera.

    Equal, not "equal to within a fit": they are equal-stretch columns of one
    `QGraphicsLayoutWidget`, whose grid lays out in floating point, so the
    three ViewBoxes have identical sizes and their aspect locks have
    identical work to do with the rectangle they are handed.
    """
    first, *rest = _ranges(page)
    return all(_same(first, other, atol) for other in rest)


# ── the camera is one camera ─────────────────────────────────────────────

def test_the_three_panels_start_on_one_camera(app):
    page = _page(app, _payload())
    assert _all_agree(page)


def test_a_wheel_step_moves_all_three_and_does_it_inside_the_event(app):
    """No timer and no debounce: the assertion runs with no event loop turn
    between it and the handler, so a mirroring that was queued would fail."""
    page = _page(app, _payload())
    before = _ranges(page)

    page._preview_vbs[1].wheelEvent(_Wheel((10, 10)))

    after = _ranges(page)
    assert not _same(after, before), "the wheel did nothing"
    assert _all_agree(page)


def test_a_wheel_step_on_any_panel_drives_the_other_two(app):
    page = _page(app, _payload())
    for src in (0, 1, 2):
        page._preview_vbs[src].wheelEvent(_Wheel((5, 5), delta=-120))
        assert _all_agree(page), f"panel {src} did not drive the others"


def test_sixty_drag_moves_are_sixty_synchronous_updates(app):
    """A drag is delivered as a stream of move events. Every one of them
    must move all three, in the event -- 60 in, 60 out, none coalesced."""
    page = _page(app, _payload())
    seen = [0, 0, 0]

    def _count(idx):
        def _slot(*_a):
            seen[idx] += 1
        return _slot

    slots = [_count(i) for i in range(3)]
    for vb, slot in zip(page._preview_vbs, slots):
        vb.sigRangeChanged.connect(slot)

    for step in range(60):
        page._preview_vbs[0].mouseDragEvent(
            _Drag((step + 1, step + 1), (step, step)))
        assert _all_agree(page), f"panels disagreed on move {step}"

    for vb, slot in zip(page._preview_vbs, slots):
        vb.sigRangeChanged.disconnect(slot)

    assert seen == [60, 60, 60], seen


def test_a_wheel_step_costs_under_five_milliseconds_on_a_real_sized_patch(app):
    """The arrays are the size a virtual patch really is. If a range change
    were a fetch, or a re-composite, this is where it would show."""
    page = _page(app, _payload(h=2048, w=2048))
    assert page._preview_imgs[0].image.shape == (2048, 2048)

    for _ in range(5):                       # warm the transforms
        page._preview_vbs[0].wheelEvent(_Wheel((100, 100)))

    steps = 40
    t0 = time.perf_counter()
    for i in range(steps):
        delta = 120 if i % 2 == 0 else -120
        page._preview_vbs[0].wheelEvent(_Wheel((100, 100), delta=delta))
    per_step_ms = (time.perf_counter() - t0) * 1000 / steps

    assert _all_agree(page)
    print(f"\nWHEEL STEP: {per_step_ms:.3f} ms (2048x2048 per panel)")
    assert per_step_ms < 5.0, f"{per_step_ms:.2f} ms per wheel step"


def test_zooming_out_fetches_nothing(app):
    """Nothing beyond the region is ever asked for. Zoom all the way out and
    the picture shrinks inside the panel with background around it."""
    page = _page(app, _payload())
    region, level = page._compare_region, page._compare_level
    req_id, payload = page._compare_req_id, page._compare_payload

    for _ in range(30):
        page._preview_vbs[0].wheelEvent(_Wheel((10, 10), delta=-120))

    (x0, x1), (y0, y1) = page._preview_vbs[0].viewRange()
    assert x1 - x0 > 64 and y1 - y0 > 64, "did not zoom past the image"
    # No worker, no new region, no new level, and the same arrays on screen.
    assert page._compare_worker is None
    assert page._compare_req_id == req_id
    assert page._compare_region == region
    assert page._compare_level == level
    assert page._compare_payload is payload


def test_the_lock_is_permanent_and_has_no_button(app):
    """Three panels that could drift apart are not a comparison, so there is
    no switch to turn the link off -- and no per-panel reset that would move
    one of them alone."""
    page = _page(app, _payload())
    for gone in ("_btn_lock_zoom", "_reset_all_views", "_reset_single_view"):
        assert not hasattr(page, gone), gone
    assert callable(page._sync_zoom)


# ── a recompute keeps the camera; a new region takes it ──────────────────

def _zoom_in(page):
    page._preview_vbs[0].setRange(xRange=(10, 20), yRange=(30, 40), padding=0)
    page._sync_zoom(0)
    return _ranges(page)


def test_recomputing_the_same_region_keeps_the_zoom(app):
    """A channel row change or a parameter edit recomputes the SAME region.
    The user asked to see this place computed differently, not to be sent
    back to the top."""
    page = _page(app, _payload())
    zoomed = _zoom_in(page)

    page._apply_compare_payload(_payload(value=900.0))

    assert _same(_ranges(page), zoomed)
    assert page._preview_imgs[0].image[0, 0] == np.float32(900.0), (
        "the new arrays must still replace the old pixels")


def test_the_first_payload_of_a_region_fits_it(app):
    """Nothing was on screen, so there is no camera to keep."""
    page = _page(app)
    before = _ranges(page)
    assert page._compare_fitted is False

    page._compare_region = (0.0, 0.0, 64.0, 64.0)
    page._apply_compare_payload(_payload())

    assert page._compare_fitted is True
    assert not _same(_ranges(page), before)
    assert _all_agree(page)


def test_a_new_region_is_fitted_again(app):
    """A different region is a different picture: the old rectangle would
    frame an arbitrary corner of it."""
    page = _page(app, _payload())
    zoomed = _zoom_in(page)

    page._compare_region = (500.0, 500.0, 64.0, 64.0)
    page._compare_fitted = False              # what `_enter_compare_mode` does
    page._apply_compare_payload(_payload(value=100.0))

    assert not _same(_ranges(page), zoomed)
    assert _all_agree(page)


def test_a_display_mapping_change_never_moves_the_camera(app):
    """`_refresh_preview_display(keep_zoom=True)` is a levels-and-table swap
    and nothing else -- every colour, Min/Max and switch caller passes it."""
    page = _page(app, _payload())
    zoomed = _zoom_in(page)

    page.set_display_mapping("CD3", 0.0, 250.0, 2.0)
    page._channel_colors["CD3"] = (1.0, 0.0, 1.0)
    page._refresh_preview_display(keep_zoom=True)

    assert _same(_ranges(page), zoomed)
    assert list(page._preview_imgs[0].levels) == pytest.approx([0.0, 250.0])
