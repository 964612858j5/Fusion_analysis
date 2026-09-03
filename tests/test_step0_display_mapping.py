"""One display mapping per channel, shared by the compare panels, the full
image and its DAPI overlay.

Why: the full image looked dimmer than the compare panels for the same
channel because the two views normalised differently (per-patch percentiles
against a slide-wide range). Now both read `(min, max, gamma)` from the
Channel Remap workbench's per-channel params -- the ONE place the numbers
live -- seeded once per channel from the slide, and never normalise into the
pixels.

Own module: page-heavy Step0 suites crash pyqtgraph offscreen when combined
with the background-correction module in one process.
"""

import os

import numpy as np
import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

pytest.importorskip("PyQt5")

from block01.core.display_mapping import build_display_lut, seed_display_range  # noqa: E402
from block01.ui.step0 import step0_page as sp  # noqa: E402

from test_step0_background_correction_tab import _GpuPathLoader, app  # noqa: E402,F401


class _Overlay:
    def __init__(self):
        self.mappings = []

    def set_display_mapping(self, lo, hi, gamma=None):
        self.mappings.append((lo, hi, gamma))


class _Ctl:
    def __init__(self):
        self.mappings = []
        self.channel, self.method, self.params = "CD3", None, ()

    def set_display_mapping(self, lo, hi, gamma=None, *, channel=None):
        self.mappings.append((lo, hi, gamma, channel))

    def set_marker_visible(self, v):
        pass

    def set_tint(self, rgb):
        pass


class _Stack:
    def __init__(self):
        self.controller = _Ctl()
        self.overlay = _Overlay()
        self.provider = self
        self.view = None

    def level_shape(self, _l):
        return (1000, 1000)


class _Tab:
    def __init__(self, stack):
        self.stack = stack

    def show_source(self, *a, **k):
        return True

    def set_dataset(self, _p):
        pass

    def teardown(self, **_k):
        pass


def _page(app, stack=None):
    page = sp.Step0Page()
    page.loader = _GpuPathLoader()
    page.patches = [(0, 32, 0, 32)]
    page.current_patch_idx = 0
    page.nucleus_channel = "DAPI"
    page._rebuild_channel_list()
    page.current_channel = "CD3"
    page._explore_tab = _Tab(stack)
    return page


# ── the pure functions ───────────────────────────────────────────────────

def test_seed_is_the_qupath_window_over_nonzero_pixels():
    rng = np.random.default_rng(0)
    arr = rng.uniform(50, 1050, size=(300, 300)).astype(np.float32)
    arr[:100] = 0                                  # glass: excluded
    lo, hi = seed_display_range(arr)
    assert 50 <= lo < 60 and 1040 < hi <= 1050
    assert seed_display_range(np.zeros((8, 8))) == (0.0, 1.0)
    lo, hi = seed_display_range(np.full((8, 8), 7.0))
    assert lo == 7.0 and hi == 8.0                 # constant: unit window


def test_the_table_is_gamma_then_colour():
    lut = build_display_lut((1.0, 0.5, 0.0), gamma=2.0)
    assert lut.shape == (256, 3) and lut.dtype == np.uint8
    assert tuple(lut[0]) == (0, 0, 0) and tuple(lut[255]) == (255, 128, 0)
    assert lut[128][0] == round((128 / 255) ** 2 * 255)
    grey = build_display_lut(None, 1.0)
    assert np.array_equal(grey[:, 0], np.arange(256))


# ── the page shares the mapping with the full image ──────────────────────

def test_showing_the_full_image_hands_it_both_mappings(app):
    stack = _Stack()
    page = _page(app, stack)
    page.set_display_mapping("CD3", 10.0, 900.0, 1.2)
    page.set_display_mapping("DAPI", 2.0, 80.0, 0.8)

    page._apply_full_image_display(stack)

    assert stack.controller.mappings[-1] == (10.0, 900.0, 1.2, "CD3")
    assert stack.overlay.mappings[-1] == (2.0, 80.0, 0.8)


def test_a_mapping_change_reaches_the_live_full_image(app):
    stack = _Stack()
    page = _page(app, stack)
    page.set_display_mapping("CD3", 0.0, 1000.0, 1.0)
    n = len(stack.controller.mappings)

    page.set_display_mapping("CD3", 0.0, 500.0, 0.7)
    page.set_display_mapping("DAPI", 1.0, 60.0, 1.0)

    assert stack.controller.mappings[n:] == [(0.0, 500.0, 0.7, "CD3")]
    assert stack.overlay.mappings[-1] == (1.0, 60.0, 1.0)


def test_another_channels_change_does_not_touch_the_full_image(app):
    stack = _Stack()
    page = _page(app, stack)
    page.set_display_mapping("CD3", 0.0, 1000.0, 1.0)
    n = len(stack.controller.mappings)
    page.set_display_mapping("CD20", 0.0, 50.0, 1.0)
    assert len(stack.controller.mappings) == n


def test_the_mapping_lives_in_the_workbench_params(app):
    """Single source of truth: the Channel Remap params ARE the display
    mapping, brightness/contrast pinned neutral. The channel model keeps a
    mirror, but nothing reads it back."""
    page = _page(app)
    page._sync_step0_to_workbench()
    wb = page._cond_workbench
    assert "CD3" in wb._params, "the workbench must know the page's channels"

    page.set_display_mapping("CD3", 3.0, 30.0, 1.1)

    p = wb._params["CD3"]
    assert (p["min"], p["max"], p["gamma"]) == (3.0, 30.0, 1.1)
    assert (p["brightness"], p["contrast"]) == (0.0, 1.0)
    assert page._display_mapping_for("CD3") == (3.0, 30.0, 1.1)
    state = page._dock_adapter.model.get("CD3")
    assert (state.display_min, state.display_max, state.display_gamma) == (3.0, 30.0, 1.1)


def test_moving_the_inspector_reaches_the_full_image(app):
    """The inspector's own controls emit `params_changed`, which is the one
    signal that pushes the numbers out to every view."""
    stack = _Stack()
    page = _page(app, stack)
    page._sync_step0_to_workbench()
    wb = page._cond_workbench
    wb.set_active_channel("CD3")
    n = len(stack.controller.mappings)

    wb._sp_max.setValue(777.0)

    assert wb._params["CD3"]["max"] == 777.0
    assert page._display_mapping_for("CD3")[1] == 777.0
    assert stack.controller.mappings[n:][-1] == (
        page._display_mapping_for("CD3") + ("CD3",))


def test_the_seed_lands_once_and_never_over_a_user_value(app):
    page = _page(app)
    page._sync_step0_to_workbench()
    wb = page._cond_workbench
    page._display_mapping_for("CD3")                  # first show -> seeded
    assert "CD3" in page._display_seeded
    seeded = tuple(page._display_mapping_for("CD3"))

    page.set_display_mapping("CD3", 11.0, 22.0, 1.0)  # a deliberate edit
    assert wb._user_adjusted["CD3"] is True
    for _ in range(3):
        assert page._display_mapping_for("CD3") == (11.0, 22.0, 1.0)
    assert seeded != (11.0, 22.0, 1.0)


def test_auto_reseeds_from_the_available_pixels(app):
    page = _page(app)
    rng = np.random.default_rng(1)
    raw = rng.uniform(200, 4000, size=(32, 32)).astype(np.float32)
    m = {"snr": 1.0, "bg_cv": 0.1}
    page._on_batch_patch_done("CD3", 0, {"original_raw": raw, "tophat_raw": raw, "cucim_raw": raw,
                                        "original_metrics": m, "tophat_metrics": m, "cucim_metrics": m,
                                        "nucleus_raw": None})
    page.set_display_mapping("CD3", 0.0, 1.0, 2.0)

    page._on_display_auto("marker")

    lo, hi, gamma = page._display_mapping_for("CD3")
    assert 200 <= lo < 300 and 3900 < hi <= 4000 and gamma == 1.0


def test_no_channel_means_a_harmless_default(app):
    page = _page(app)
    page.current_channel = None
    assert page._display_mapping_for(None) == (0.0, 1.0, 1.0)
    page._apply_full_image_display(_Stack())      # must not raise
