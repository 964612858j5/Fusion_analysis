"""The montage canvas and its base images (plan block D, step 1).

Layout (row packing, hit testing, fit, click selects without moving), the
supply (the viewer's own spec -> channels -> provider -> compose chain,
the two caches and what empties them, the worker thread, late results
refused, nothing after close), and the page's wiring (the tab, which
patches it shows, following the draft, released on leaving Step1 and on
close). Own module: page-heavy PyQt suites crash pyqtgraph offscreen when
combined.
"""
import os
import time

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import numpy as np
import pytest

pytest.importorskip("PyQt5")
from PyQt5 import QtCore, QtWidgets  # noqa: E402

from block01.ui.step1_presegmentation import montage_supply as ms  # noqa: E402
from block01.ui.step1_presegmentation.montage_view import (  # noqa: E402
    MontageLayout, MontageView, pack_rows)
from block01.viewer import step1_compose as compose_core  # noqa: E402


@pytest.fixture(scope="module")
def app():
    return QtWidgets.QApplication.instance() or QtWidgets.QApplication([])


def _pump(cond, timeout=10.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        QtWidgets.QApplication.processEvents()
        if cond():
            return True
        time.sleep(0.005)
    QtWidgets.QApplication.processEvents()
    return cond()


# ── layout ──────────────────────────────────────────────────────────────────
def test_patches_of_different_sizes_are_packed_in_rows_without_overlap():
    sizes = [(512, 512), (300, 800), (1000, 400), (64, 64), (512, 900), (200, 200)]
    rects, (H, W) = pack_rows(sizes)
    assert [(h, w) for _, _, h, w in rects] == sizes                     # order and size kept
    for i, (y, x, h, w) in enumerate(rects):
        assert 0 <= y and y + h <= H and 0 <= x and x + w <= W
        for (y2, x2, h2, w2) in rects[i + 1:]:
            assert y + h <= y2 or y2 + h2 <= y or x + w <= x2 or x2 + w2 <= x
    assert len({y for y, _, _, _ in rects}) > 1                          # it wrapped


def test_a_click_hits_its_patch_and_a_gap_hits_nothing():
    lay = MontageLayout([{"id": i, "name": f"P{i}", "bbox": [0, h, 0, w]}
                         for i, (h, w) in enumerate([(100, 100), (50, 200), (80, 60)], start=1)])
    for i, (y, x, h, w) in enumerate(lay.rects):
        assert lay.hit(y + h / 2, x + w / 2) == i
        assert lay.hit(y, x) == i and lay.hit(y + h - 0.5, x + w - 0.5) == i
    y, x, h, w = lay.rects[0]
    assert lay.hit(y + 1, x + w + 1) is None                              # the gap


def test_the_view_fits_selects_without_moving_and_picks_a_coarser_level_zoomed_out(app):
    view = MontageView()
    view.resize(600, 400)
    view.show()
    view.set_downsamples([1.0, 4.0, 16.0])
    patches = [{"id": 1, "name": "P1", "bbox": [0, 2000, 0, 2000]},
               {"id": 7, "name": "edge", "bbox": [5000, 5400, 100, 900]}]
    view.set_patches(patches)
    QtWidgets.QApplication.processEvents()
    (x0, x1), (y0, y1) = view.vb.viewRange()
    H, W = view.layout_table.size
    assert x0 <= 0 and x1 >= W and y0 <= 0 and y1 >= H                  # everything in view
    assert view.wanted_level() > 0                                       # 2000+ px in 600 px
    before = view.vb.viewRange()
    y, x, h, w = view.canvas_rect(7)
    got = []
    view.patch_clicked.connect(got.append)
    assert view.click_at(y + h / 2, x + w / 2) == 7
    assert got == [7] and view.selected == 1 and view.vb.viewRange() == before
    view.vb.setRange(xRange=(x, x + 50), yRange=(y, y + 50), padding=0)
    QtWidgets.QApplication.processEvents()
    assert view.wanted_level() == 0                                      # zoomed in
    view.fit()
    assert view.vb.viewRange()[0][1] >= W
    rgba = np.zeros((25, 50, 4), np.uint8)                               # a level-16 image
    assert view.set_image((5000, 5400, 100, 900), rgba)
    r = view._images[1].mapRectToParent(view._images[1].boundingRect())
    assert (r.x(), r.y(), r.width(), r.height()) == (x, y, w, h)         # on its canvas rect
    view.close()


# ── the supply ──────────────────────────────────────────────────────────────
class _Provider:
    """Two channels over a 4000 x 4000 slide, three levels, NaN outside a
    'ROI' in the left half; counts reads."""

    num_levels = 3
    _ds = [1.0, 4.0, 16.0]

    def __init__(self):
        rng = np.random.default_rng(0)
        self.base = {"DAPI": rng.uniform(0, 255, (4000, 4000)).astype(np.float32),
                     "CD3": rng.uniform(0, 255, (4000, 4000)).astype(np.float32)}
        self.base["CD3"][:, 2000:] = np.nan
        self.calls = []

    def level_downsample(self, level):
        return self._ds[level]

    def level_downsample_yx(self, level):
        return (self._ds[level], self._ds[level])

    def read_region(self, ch, level, y0, y1, x0, x1):
        self.calls.append((ch, level, y0, y1, x0, x1))
        s = int(self._ds[level])
        return self.base[ch][::s, ::s][y0:y1, x0:x1].copy(), (y0, x0)


OVERLAY = {"mode": "overlay", "weights": {"DAPI": 1.0, "CD3": 0.5},
           "colors": {"DAPI": (0.0, 0.0, 1.0), "CD3": (1.0, 0.0, 0.0)},
           "mappings": {"DAPI": (0.0, 255.0, 1.0), "CD3": (10.0, 200.0, 1.0)}}
FUSION = {"mode": "fusion", "groups": {"T": {"CD3": 1.0}}, "group_weights": {"T": 1.0},
          "nucleus": ("DAPI", 0.8), "colors": {},
          "mappings": {"DAPI": (0.0, 255.0, 1.0), "CD3": (10.0, 200.0, 1.0)}}


def _collect(supply):
    got = []
    supply.composed.connect(lambda b, lvl, rgba, gen: got.append((b, lvl, rgba, gen)))
    return got


@pytest.mark.parametrize("spec", [OVERLAY, FUSION])
def test_a_patch_image_is_the_viewers_composition_of_the_providers_pixels(app, spec):
    prov = _Provider()
    supply = ms.MontageSupply(prov, "pk")
    got = _collect(supply)
    bbox = (400, 1200, 1600, 2400)                     # crosses the NaN edge of CD3
    supply.request([bbox], 1, spec)
    assert _pump(lambda: len(got) == 1)
    _, level, rgba, gen = got[0]
    tiles = {}
    for ch in ms.spec_channels(spec):
        v = prov.base[ch][::4, ::4][100:300, 400:600]
        tiles[ch] = (v, ~np.isnan(v))
    want, _, missing = compose_core.compose(
        spec["mode"], tiles, weights=spec.get("weights"), colors=spec.get("colors"),
        mappings=spec.get("mappings"), groups=spec.get("groups"),
        group_weights=spec.get("group_weights"), nucleus=spec.get("nucleus") or ("", 0.0))
    assert level == 1 and gen == supply.generation and not missing
    np.testing.assert_array_equal(rgba, want)
    assert sorted(c[0] for c in prov.calls) == sorted(ms.spec_channels(spec))
    assert all(c[1:] == (1, 100, 300, 400, 600) for c in prov.calls)
    supply.close()


def test_intensity_or_mode_recompose_without_reading_again_and_other_pixels_empty_all(app):
    prov = _Provider()
    supply = ms.MontageSupply(prov, "pk")
    got = _collect(supply)
    bbox = (0, 800, 0, 800)
    supply.request([bbox], 2, OVERLAY)
    assert _pump(lambda: len(got) == 1)
    reads = supply.reads
    brighter = dict(OVERLAY, mappings={"DAPI": (0.0, 100.0, 1.0), "CD3": (10.0, 200.0, 1.0)})
    supply.forget_composites()
    supply.request([bbox], 2, brighter)
    supply.request([bbox], 2, FUSION)                    # supersedes the one before
    assert _pump(lambda: got[-1][3] == supply.generation and len(got) >= 2)
    assert supply.reads == reads                          # the channel blocks held
    assert len(supply.channels) == 2 and len(supply.composites) >= 1
    supply.forget_patches([(1, 2, 3, 4)])                 # the patch was unticked
    assert len(supply.channels) == 0 and len(supply.composites) == 0
    supply.request([bbox], 2, OVERLAY)
    assert _pump(lambda: supply.reads == reads + 2)
    supply.set_pixel_key("pk")
    assert len(supply.channels) == 2                      # the same pixels: kept
    supply.set_pixel_key("pk2")
    assert len(supply.channels) == 0 and len(supply.composites) == 0
    supply.close()


def test_a_channel_without_a_window_is_asked_for_not_guessed(app):
    prov = _Provider()
    supply = ms.MontageSupply(prov, "pk")
    got, missing = _collect(supply), []
    supply.missing.connect(missing.append)
    spec = dict(OVERLAY, mappings={"DAPI": (0.0, 255.0, 1.0)})
    supply.request([(0, 400, 0, 400)], 2, spec)
    assert _pump(lambda: missing and got)
    assert missing[0] == ["CD3"] and len(supply.composites) == 0      # not cached half-made
    supply.close()


def test_the_caches_are_bounded_by_bytes():
    lru = ms.ByteLRU(1000)
    for i in range(5):
        lru.put(i, np.zeros(300, np.uint8), 300)
    assert len(lru) == 3 and lru.nbytes == 900 and lru.value(0) is None and lru.value(4) is not None
    lru.put("big", np.zeros(2000, np.uint8), 2000)                       # larger than it all
    assert lru.value("big") is None and len(lru) == 3
    assert ms.CHANNEL_CACHE_BYTES == 1 << 30 and ms.COMPOSED_CACHE_BYTES == 256 << 20


def test_close_ends_the_thread_empties_the_caches_and_reports_nothing_after(app):
    prov = _Provider()
    supply = ms.MontageSupply(prov, "pk")
    got = _collect(supply)
    supply.request([(0, 4000, 0, 4000)], 0, OVERLAY)                     # big: still working
    supply.close()
    assert not supply.is_alive()
    assert len(supply.channels) == 0 and len(supply.composites) == 0
    n = len(got)
    supply.request([(0, 400, 0, 400)], 2, OVERLAY)                       # after close: nothing
    time.sleep(0.2)
    QtWidgets.QApplication.processEvents()
    assert len(got) == n


# ── the page ────────────────────────────────────────────────────────────────
def _no_gpu():
    raise RuntimeError("no GPU layer in this test")


def _window(app, tmp_path, monkeypatch, provider, gpu=False):
    """`gpu=False`: the CPU picture (the fallback when no GPU layer starts)."""
    from block01.ui.main_window import MainWindow
    from block01.ui.step0.roi_context_model import Patch
    w = MainWindow()
    if not gpu:
        w._montage_gpu_factory = _no_gpu
    w.step0_output = {"step1_dir": str(tmp_path / "step1")}
    w._on_patches([Patch((0, 400, 0, 400), 1), Patch((400, 1200, 0, 600), 2),
                   Patch((2000, 2300, 2000, 2600), 3)])
    monkeypatch.setattr(w, "_montage_provider", lambda: provider)
    monkeypatch.setattr(w, "_preseg_current", lambda: ("pk", "fh"))
    monkeypatch.setattr(w, "_montage_spec", lambda: dict(w._test_spec))
    w._test_spec = dict(OVERLAY)
    w._current_step = 1
    w.resize(1400, 900)
    w.show()
    w._stack.setCurrentIndex(1)
    w.right_tabs.setCurrentIndex(w._montage_tab_index)
    QtWidgets.QApplication.processEvents()
    return w


def test_the_tab_shows_the_ticked_patches_and_follows_the_draft(app, tmp_path, monkeypatch):
    prov = _Provider()
    w = _window(app, tmp_path, monkeypatch, prov)
    try:
        assert w.right_tabs.tabText(w._montage_tab_index) == "Pre-seg Results"
        assert w.right_tabs.indexOf(w.patch_results_tab) >= 0              # the old tab stays
        view = w._preseg_montage
        assert [p["id"] for p in view.layout_table.patches] == [1, 2, 3]
        assert _pump(lambda: all(img.image is not None for img in view._images))
        w._preseg_patches.tiles()[1].set_checked(False)                      # untick P2
        assert [p["id"] for p in view.layout_table.patches] == [1, 3]
        assert all(k[0] != (400, 1200, 0, 600) for k in w._preseg_montage_supply.channels._d)
        assert _pump(lambda: all(img.image is not None for img in view._images))
        # Let the re-fit's level check and its request settle first, so the
        # only thing left to redraw the patch is the Intensity change itself.
        assert _pump(lambda: not view._level_timer.isActive())
        supply = w._preseg_montage_supply
        gen = supply.generation
        assert _pump(lambda: False, timeout=0.5) is False and supply.generation == gen
        first = view._images[0].image.copy()
        reads = w._preseg_montage_supply.reads
        w._test_spec = dict(OVERLAY, mappings={"DAPI": (0.0, 60.0, 1.0),
                                               "CD3": (10.0, 200.0, 1.0)})
        w._display.state.mapping_changed.emit("DAPI")                        # Intensity moved
        assert _pump(lambda: not np.array_equal(view._images[0].image, first))
        assert w._preseg_montage_supply.reads == reads                       # no disk read
    finally:
        w.close()


def test_leaving_step1_and_closing_release_the_montage(app, tmp_path, monkeypatch):
    prov = _Provider()
    w = _window(app, tmp_path, monkeypatch, prov)
    supply = w._preseg_montage_supply
    assert supply is not None and supply.is_alive()
    w._step1_whole_slide_step_changed(0)
    assert w._preseg_montage_supply is None and not supply.is_alive()
    assert len(supply.channels) == 0 and len(supply.composites) == 0
    w._current_step = 1
    w._request_montage_images()
    again = w._preseg_montage_supply
    assert again is not None and again.is_alive()
    w.close()
    QtWidgets.QApplication.processEvents()
    assert not again.is_alive() and w._preseg_montage_supply is None


def test_a_run_shows_its_own_frozen_patches(app, tmp_path, monkeypatch):
    prov = _Provider()
    w = _window(app, tmp_path, monkeypatch, prov)
    try:
        w._preseg_run = {"patches": [{"id": 3, "name": "P3", "bbox": [2000, 2300, 2000, 2600]}],
                         "combos": [], "tasks": []}
        w._show_montage_patches()
        assert [p["id"] for p in w._preseg_montage.layout_table.patches] == [3]
        w._preseg_patches.tiles()[0].set_checked(False)                      # ticks: not the run
        assert [p["id"] for p in w._preseg_montage.layout_table.patches] == [3]
    finally:
        w._preseg_run = None
        w.close()


# ── after the first acceptance (user, 2026-09-24) ───────────────────────────
def test_double_click_shows_one_patch_alone_and_again_shows_them_all(app):
    view = MontageView()
    view.resize(600, 400)
    view.show()
    view.set_patches([{"id": 1, "name": "P1", "bbox": [0, 400, 0, 400]},
                      {"id": 2, "name": "P2", "bbox": [0, 300, 0, 900]}])
    QtWidgets.QApplication.processEvents()
    H, W = view.layout_table.size
    y, x, h, w = view.canvas_rect(2)
    assert view.double_click_at(y + h / 2, x + w / 2) == 1              # P2 alone
    (x0, x1), (y0, y1) = view.vb.viewRange()
    assert x0 >= x - w * 0.05 and x1 <= x + w * 1.05                    # it fills the view
    assert view.selected == 1
    assert view.double_click_at(y + h / 2, x + w / 2) is None            # again: all of them
    assert view.vb.viewRange()[0][1] >= W
    view.double_click_at(y + h / 2, x + w / 2)
    gy, gx = view.layout_table.rects[0][0] + 1, view.layout_table.rects[0][3] + 2
    assert view.layout_table.hit(gy, gx) is None
    assert view.double_click_at(gy, gx) is None                          # a gap: all of them
    view.close()


def test_images_are_composed_no_finer_than_the_screen_and_only_on_screen(app):
    view = MontageView()
    view.resize(600, 400)
    view.show()
    view.set_downsamples([1.0, 4.0, 16.0])
    patches = [{"id": i, "name": f"P{i}", "bbox": [0, 12000, i * 13000, i * 13000 + 12000]}
               for i in range(4)]
    view.set_patches(patches)
    QtWidgets.QApplication.processEvents()
    level, stride = view.wanted()
    scale = view.screen_px_per_canvas_px()
    assert level == 2 and stride > 1                                     # coarser than level 2
    assert 16 * stride <= 1 / scale < 16 * (stride + 1)                  # never finer than screen
    assert len(view.bboxes_by_need()) == 4
    y, x, h, w = view.canvas_rect(2)
    view.focus(2)
    QtWidgets.QApplication.processEvents()
    need = view.bboxes_by_need()
    assert need[0] == (0, 12000, 26000, 38000) and len(need) < 4         # the one in view first
    prov = _Provider()
    supply = ms.MontageSupply(prov, "pk")
    got = _collect(supply)
    supply.request([(0, 800, 0, 800)], 0, OVERLAY, stride=4)
    assert _pump(lambda: got)
    assert got[0][2].shape[:2] == (200, 200)                             # every 4th pixel
    supply.close()
    view.close()


def test_the_montage_has_its_own_overlay_and_fusion_buttons_in_step_with_the_viewers(
        app, tmp_path, monkeypatch):
    prov = _Provider()
    w = _window(app, tmp_path, monkeypatch, prov)
    try:
        view = w._preseg_montage
        assert view.btn_overlay.isChecked() and not view.btn_fusion.isChecked()
        view.btn_fusion.click()
        assert w._step1_preview_mode == "fusion" and w._btn_mode_fusion.isChecked()
        w._btn_mode_overlay.click()
        assert w._step1_preview_mode == "overlay" and view.btn_overlay.isChecked()
    finally:
        w.close()


def test_each_finished_request_says_where_the_time_went(app, capsys):
    prov = _Provider()
    supply = ms.MontageSupply(prov, "pk")
    got = _collect(supply)
    supply.request([(0, 400, 0, 400), (400, 800, 0, 400)], 2, OVERLAY)
    assert _pump(lambda: len(got) == 2)
    time.sleep(0.05)
    out = capsys.readouterr().out
    assert "[Montage] 2 patches level=2 stride=1: 4 reads" in out and "total" in out
    supply.close()


def _settle(w):
    """Everything at rest before a timing test: the window's size, the
    level check, the settle timer and the frame in flight."""
    view = w._preseg_montage
    size = None
    for _ in range(50):
        _pump(lambda: False, timeout=0.05)
        now = (view.vb.width(), view.vb.height())
        if now == size and not view._level_timer.isActive():
            break
        size = now
    assert _pump(lambda: w._montage_inflight is None and not w._montage_timer.isActive())


def test_intensity_is_drawn_while_the_slider_moves_not_after_it_stops(app, tmp_path, monkeypatch):
    """The real-machine report of 2026-09-25: a restarted timer drew nothing
    until the drag paused. Now a frame is drawn while the drag goes on, each
    with the newest settings (the CPU fallback's picture: full resolution
    every frame, never switching)."""
    prov = _Provider()
    w = _window(app, tmp_path, monkeypatch, prov)
    try:
        view = w._preseg_montage
        assert _pump(lambda: all(img.image is not None for img in view._images))
        _settle(w)
        shown = []
        real = view.set_image

        def spy(bbox, rgba):
            shown.append((time.monotonic(), tuple(bbox), rgba.shape))
            return real(bbox, rgba)

        monkeypatch.setattr(view, "set_image", spy)
        full = {b: img.image.shape for b, img in zip(view.bboxes(), view._images)}
        camera = view.vb.viewRange()
        start = time.monotonic()
        while time.monotonic() - start < 1.0:                              # a one-second drag
            hi = 60.0 + 190.0 * (time.monotonic() - start)
            w._test_spec = dict(OVERLAY, mappings={"DAPI": (0.0, hi, 1.0),
                                                   "CD3": (10.0, 200.0, 1.0)})
            w._display.state.mapping_changed.emit("DAPI")
            end = time.monotonic() + 0.016
            while time.monotonic() < end:
                QtWidgets.QApplication.processEvents()
        during = [(t, b, shape) for t, b, shape in shown if t - start < 1.0]
        assert view.vb.viewRange() == camera                               # images never move it
        frames = {round(t, 2) for t, _, _ in during}
        assert len(during) >= 10 and len(frames) >= 5, f"only {len(frames)} frames while dragging"
        assert during[0][0] - start < 0.2                                  # the first at once
        # the CPU fallback no longer steps down to half resolution while
        # things move: that switching was the shimmer of 2026-09-25
        assert all(shape == full[b] for _, b, shape in during)
        stop = time.monotonic()

        def settled():
            return all(any(t > stop and b2 == b and shape == full[b] for t, b2, shape in shown)
                       for b in full)
        assert _pump(settled, timeout=3)
        last = max(min(t for t, b2, shape in shown if t > stop and b2 == b and shape == full[b])
                   for b in full)
        assert last - stop < 1.0                                           # full resolution soon
    finally:
        w.close()


def test_the_newest_settings_are_drawn_as_soon_as_the_frame_in_flight_is_up(
        app, tmp_path, monkeypatch):
    """With a slow frame, changes pile up while it is drawn; the newest is
    drawn right after it -- not only when the settle timer fires."""
    prov = _Provider()
    w = _window(app, tmp_path, monkeypatch, prov)
    try:
        _settle(w)
        supply = w._preseg_montage_supply
        real_compose = supply._compose

        def slow(req):
            time.sleep(0.06)
            return real_compose(req)

        monkeypatch.setattr(supply, "_compose", slow)
        w._montage_timer.setInterval(5000)          # keep the full-resolution frame out of it
        gens = []
        supply.finished.connect(gens.append)
        for hi in (80.0, 120.0, 160.0):             # three changes within one slow frame
            w._test_spec = dict(OVERLAY, mappings={"DAPI": (0.0, hi, 1.0),
                                                   "CD3": (10.0, 200.0, 1.0)})
            w._display.state.mapping_changed.emit("DAPI")
            _pump(lambda: False, timeout=0.01)
        assert _pump(lambda: len(gens) >= 2, timeout=3)    # the frame, then the newest
        assert w._montage_inflight is None and not w._montage_dirty
        assert supply._stats["gen"] == gens[-1]
    finally:
        w.close()


# ── the GPU picture: Step1's own layer (user ruling, 2026-09-25) ────────────
from block01.ui.step1_presegmentation import montage_gpu as mg  # noqa: E402


def _gpu_or_skip(w):
    if w._montage_backend != "gpu":
        pytest.skip("no GPU layer on this machine")


def test_without_a_gpu_layer_the_cpu_picture_is_used_and_it_is_said(app, tmp_path, monkeypatch,
                                                                    capsys):
    w = _window(app, tmp_path, monkeypatch, _Provider())
    try:
        assert w._montage_backend == "cpu" and w._montage_gpu_layer is None
        assert all(img.isVisible() for img in w._preseg_montage._images)
        assert "GPU layer unavailable, using the CPU picture instead" in capsys.readouterr().out
    finally:
        w.close()


def test_the_display_snapshot_is_the_viewers_copy_of_the_spec():
    from block01.ui.step1_viewer_mount import Step1WholeSlideMount
    from block01.ui import step1_draft_spec as ds

    class _Stub:
        _domain = _state = None
        _mode = "fusion"
        _note_gpu_missing_windows = staticmethod(lambda spec, mappings: None)

    for spec in (OVERLAY, FUSION):
        stub = _Stub()
        orig = ds.build_spec
        ds.build_spec = lambda *a, **k: dict(spec)
        try:
            theirs = Step1WholeSlideMount._gpu_display_snapshot(stub)
        finally:
            ds.build_spec = orig
        assert mg.display_snapshot(spec) == theirs


def test_the_planes_fit_the_budget_however_big_the_patch():
    ds = [1.0, 4.0, 16.0, 64.0]
    dsyx = lambda lvl: (ds[lvl], ds[lvl])                                # noqa: E731
    huge = [{"bbox": [0, 60000, 0, 40000]}]                             # a whole-slide patch
    rects = [(0, 0, 60000, 40000)]
    chans = [f"C{i}" for i in range(12)]
    # all of it in view: coarse only, bounded
    coarse, fine = mg.plan_planes(huge, rects, (0, 40000, 0, 60000), 3, chans, "pk", ds, dsyx)
    assert fine == [] and sum(p.nbytes() for p in coarse) <= mg.COARSE_BUDGET
    # zoomed to 1000 x 800 level-0 pixels at full resolution: only that part, fine
    coarse, fine = mg.plan_planes(huge, rects, (20000, 21000, 30000, 30800), 0, chans, "pk", ds,
                                  dsyx)
    assert fine and all(p.level == 0 for p in fine)
    assert sum(p.nbytes() for p in fine) <= mg.FINE_BUDGET
    x0, x1, y0, y1 = fine[0].world
    assert x0 <= 20000 and x1 >= 21000 and y0 <= 30000 and y1 >= 30800  # covers the view
    assert (x1 - x0) * (y1 - y0) < 4 * 1000 * 800                        # and not much more
    # a 4K screen's worth at level 0 with 12 channels: steps coarser to fit
    coarse, fine = mg.plan_planes(huge, rects, (0, 3840 * 4, 0, 2160 * 4), 0, chans, "pk", ds,
                                  dsyx)
    assert sum(p.nbytes() for p in fine) <= mg.FINE_BUDGET and fine[0].level > 0
    assert sum(p.nbytes() for p in coarse + fine) <= mg.TEXTURE_BUDGET


def test_a_plane_lies_exactly_where_its_pixels_are():
    ds = [1.0, 4.0]
    dsyx = lambda lvl: (ds[lvl], ds[lvl])                                # noqa: E731
    patches = [{"bbox": [1001, 3003, 2002, 3999]}]                      # not multiples of 4
    rects = [(100, 200, 2002, 1997)]
    coarse, _ = mg.plan_planes(patches, rects, (0, 5000, 0, 5000), 1, ["A"], "pk", ds, dsyx,
                               coarse_max_side=600)
    p = coarse[0]
    assert p.level == 1 and p.rect == (250, 751, 500, 1000)
    # level-1 row 250 starts at level-0 row 1000: one pixel above the patch
    assert p.world == (200 + 500 * 4 - 2002, 200 + 1000 * 4 - 2002,
                       100 + 250 * 4 - 1001, 100 + 751 * 4 - 1001)


def test_intensity_and_ticks_redraw_on_the_card_at_once_without_reading(
        app, tmp_path, monkeypatch):
    w = _window(app, tmp_path, monkeypatch, _Provider(), gpu=True)
    try:
        _gpu_or_skip(w)
        layer, supply = w._montage_gpu_layer, w._preseg_montage_supply
        assert _pump(lambda: not supply._plane_pending and w._submit_montage_gpu())
        first = layer.readback_rgba_for_test().copy()
        assert first[..., 3].any()                                        # something drawn
        reads, calls, drawn = supply.reads, [], []
        real = layer.submit
        monkeypatch.setattr(layer, "submit", lambda *a: (calls.append(a[1]),
                                                         drawn.append(a[0].channels), real(*a))[2])
        w._test_spec = dict(OVERLAY, mappings={"DAPI": (0.0, 40.0, 1.0),
                                               "CD3": (10.0, 200.0, 1.0)})
        w._display.state.mapping_changed.emit("DAPI")
        assert calls and calls[-1].mappings["DAPI"] == (0.0, 40.0, 1.0)   # synchronously
        assert supply.reads == reads                                      # nothing read
        assert not np.array_equal(layer.readback_rgba_for_test(), first)
        assert all(not img.isVisible() for img in w._preseg_montage._images)
        # a channel unticked: nothing to read
        w._test_spec = dict(OVERLAY, weights={"DAPI": 1.0}, mappings={"DAPI": (0.0, 40.0, 1.0)})
        w._display.state.visibility_changed.emit("CD3", False)
        assert supply.reads == reads
        # a channel never read before: its planes are read, then drawn
        prov = supply.provider
        prov.base["CD8"] = prov.base["DAPI"][::-1].copy()
        before = len(calls)
        w._test_spec = dict(OVERLAY, weights={"DAPI": 1.0, "CD8": 1.0},
                            colors={"DAPI": (0.0, 0.0, 1.0), "CD8": (0.0, 1.0, 0.0)},
                            mappings={"DAPI": (0.0, 40.0, 1.0), "CD8": (0.0, 255.0, 1.0)})
        w._display.state.visibility_changed.emit("CD8", True)
        assert _pump(lambda: supply.reads > reads and not supply._plane_pending)
        assert _pump(lambda: any("CD8" in [c.channel for c in call_desc]
                                 for call_desc in drawn))
    finally:
        w.close()


def test_names_and_frames_stay_above_the_gpu_picture(app, tmp_path, monkeypatch):
    w = _window(app, tmp_path, monkeypatch, _Provider(), gpu=True)
    try:
        _gpu_or_skip(w)
        view = w._preseg_montage
        w.resize(1300, 850)
        _pump(lambda: False, timeout=0.2)
        kids = [c for c in view.gv.viewport().children() if isinstance(c, QtWidgets.QWidget)]
        assert kids.index(view.overlay) > kids.index(w._montage_gpu_layer)
        assert view.overlay.testAttribute(QtCore.Qt.WA_TransparentForMouseEvents)
        y, x, h, wd = view.canvas_rect(1)
        r = view.overlay.widget_rect((y, x, h, wd))
        assert r.width() > 10 and view.overlay.rect().contains(r.center().toPoint())
    finally:
        w.close()


def test_moving_patches_on_the_canvas_neither_fails_nor_reads_again(app, tmp_path, monkeypatch,
                                                                    capsys):
    w = _window(app, tmp_path, monkeypatch, _Provider(), gpu=True)
    try:
        _gpu_or_skip(w)
        supply = w._preseg_montage_supply
        assert _pump(lambda: not supply._plane_pending)
        reads = supply.reads
        w._preseg_patches.tiles()[0].set_checked(False)                   # the others move
        assert _pump(lambda: not supply._plane_pending)
        assert w._montage_backend == "gpu" and supply.reads == reads
        assert "GPU submission failed" not in capsys.readouterr().out
    finally:
        w.close()


def test_leaving_step1_and_closing_give_the_gpu_layer_back(app, tmp_path, monkeypatch):
    w = _window(app, tmp_path, monkeypatch, _Provider(), gpu=True)
    _gpu_or_skip(w)
    layer = w._montage_gpu_layer
    disposed = []
    real = layer.dispose
    monkeypatch.setattr(layer, "dispose", lambda: (disposed.append(1), real())[1])
    w._step1_whole_slide_step_changed(0)
    assert disposed == [1] and w._montage_gpu_layer is None and w._montage_backend is None
    assert all(img.isVisible() for img in w._preseg_montage._images)
    w._current_step = 1
    w._request_montage_images()
    again = w._montage_gpu_layer
    assert again is not None
    gone = []
    real2 = again.dispose
    monkeypatch.setattr(again, "dispose", lambda: (gone.append(1), real2())[1])
    w.close()
    QtWidgets.QApplication.processEvents()
    assert gone == [1]


# ── Fusion mode: the fusion signal and the nucleus on / off (2026-09-25) ────
def test_the_signal_and_nucleus_toggles_show_only_in_fusion_mode(app):
    view = MontageView()
    view.show()
    assert not view.btn_show_fusion.isVisible() and not view.btn_show_nucleus.isVisible()
    view.set_mode("fusion")
    assert view.btn_show_fusion.isVisible() and view.btn_show_nucleus.isVisible()
    assert view.shown_layers() == (True, True)
    view.set_mode("overlay")
    assert not view.btn_show_fusion.isVisible()
    view.close()


def test_leaving_out_the_signal_or_the_nucleus_changes_only_the_montages_picture(
        app, tmp_path, monkeypatch):
    w = _window(app, tmp_path, monkeypatch, _Provider(), gpu=True)
    try:
        _gpu_or_skip(w)
        view = w._preseg_montage
        monkeypatch.setattr(w, "_montage_spec", lambda: w._montage_layers(dict(FUSION)))
        w.set_preview_mode("fusion")
        assert view.btn_show_nucleus.isVisible() and view.btn_show_nucleus.text() == "DAPI"
        assert view.btn_show_fusion.text() == "Membrane"
        layer = w._montage_gpu_layer
        sent = []
        real = layer.submit
        monkeypatch.setattr(layer, "submit", lambda *a: (sent.append(a[1]), real(*a))[1])
        model_before = w._display.fusion.effective_config()
        view.btn_show_fusion.setChecked(False)                            # DAPI only
        assert sent and sent[-1].groups == {} and sent[-1].nucleus == ("DAPI", 0.8)
        view.btn_show_nucleus.setChecked(False)                           # neither
        assert sent[-1].groups == {} and sent[-1].nucleus == ("", 0.0)
        view.btn_show_fusion.setChecked(True)                             # fusion only
        assert sent[-1].groups == {"T": {"CD3": 1.0}} and sent[-1].nucleus == ("", 0.0)
        view.btn_show_nucleus.setChecked(True)
        assert sent[-1].groups == {"T": {"CD3": 1.0}} and sent[-1].nucleus == ("DAPI", 0.8)
        assert w._display.fusion.effective_config() == model_before       # model untouched
        # Overlay mode ignores them
        assert w._montage_layers(dict(OVERLAY)) == OVERLAY
    finally:
        w.close()


def test_the_montages_buttons_look_like_the_viewers(app, tmp_path, monkeypatch):
    from block01.ui.step1_button_styles import MODE_BUTTON_QSS, layer_button_qss
    w = _window(app, tmp_path, monkeypatch, _Provider())
    try:
        view = w._preseg_montage
        # the very same look as the Viewer's Overlay / Fusion
        assert w._btn_mode_overlay.styleSheet() == MODE_BUTTON_QSS
        assert view.btn_overlay.styleSheet() == view.btn_fusion.styleSheet() == MODE_BUTTON_QSS
        # the layer toggles: the same shape, their own colours, not the mode look
        for b, layer in ((view.btn_show_fusion, "membrane"), (view.btn_show_nucleus, "nucleus")):
            qss = b.styleSheet()
            assert qss == layer_button_qss(layer) and qss != MODE_BUTTON_QSS
            for part in ("border-radius:4px", "font-size:10px", "padding:2px 10px"):
                assert part in qss
        assert view.btn_show_fusion.styleSheet() != view.btn_show_nucleus.styleSheet()
    finally:
        w.close()
