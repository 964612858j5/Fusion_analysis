"""A committed dataset switch leaves nothing of the previous slide in the
Tissue Preview -- whichever way the picture got there.

REPORTED: after loading another dataset in Step0 the main view shows the new
slide while the shared Tissue Preview / ROI Navigator still shows the previous
one. `74e2c1e` already cleared both panels at the switch and dropped late
reads by generation and loader; the report survived it.

What that fix could not cover, and what this module pins:

* The clearing happened INSIDE the reload's rebuild -- after the overview
  artists, the ROI reset and the patch rebuild, and for the popup only in the
  model feed at the end. Every one of those steps had to run for the previous
  slide to leave the screen. The transition is now made at the commit point,
  before any of them, and it is one call per panel.
* A picture pushed by the HOST (`_update_tissue_preview`, from a debounce
  timer or from a finished overview read) carried no identity at all. It was
  accepted by whichever panel it reached. It now names the dataset it was
  rendered from.
* `forget_pixels()` cleared the pixels but did not invalidate the read in
  flight; the generation only moved at the NEXT `_load_overview`. Binding a
  panel to a dataset moves it immediately.
* The popup reloaded its thumbnail "once per LOADER". Identity is per
  DATASET now, so a slide change cannot be missed because a loader object
  looks familiar -- and hidden or minimized does not skip it.

A and B here have the same shape, the same channel names and the same ROI and
patch coordinates. Only the pixels differ, so a test cannot pass because two
slides happened to be distinguishable by their geometry.

Own module: page-heavy PyQt suites crash pyqtgraph offscreen when combined.
"""

import gc
import os
import threading
import time
import weakref

import numpy as np
import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

pytest.importorskip("PyQt5")

from PyQt5 import QtWidgets  # noqa: E402

from block01.ui.step0 import overview_panel as op  # noqa: E402
from block01.ui.step0 import step0_page as sp  # noqa: E402


@pytest.fixture(scope="module")
def app():
    return QtWidgets.QApplication.instance() or QtWidgets.QApplication([])


@pytest.fixture(autouse=True)
def _collect_before_the_flush():
    yield
    gc.collect()


class _Loader:
    """Two of these differ in their pixels AND, when asked, in their shape.

    A wide-short slide and a narrow-tall one: the reported failure was the
    previous slide's picture STRETCHED onto the new slide's rectangle, which
    two slides of the same proportions cannot show.
    """

    _CHANNELS = ["DAPI", "CD3", "CD20"]

    def __init__(self, value, shape=(64, 64)):
        self.value = float(value)
        self.shape = tuple(int(v) for v in shape)
        self._corrected_zarr_path = None
        self._corrected_decisions = {}

    def channel_names(self):
        return list(self._CHANNELS)

    @property
    def ch_map(self):
        return {c: i for i, c in enumerate(self._CHANNELS)}

    def set_corrected_zarr_store(self, path, decisions):
        self._corrected_zarr_path = path
        self._corrected_decisions = dict(decisions or {})

    def set_correction_config(self, _cfg):
        pass

    def read_region(self, ch, y0, y1, x0, x1, downsample=1, normalize=True,
                    **_kw):
        ds = max(1, int(downsample))
        return self._pattern(max(1, (y1 - y0) // ds),
                             max(1, (x1 - x0) // ds))

    # The whole-slide low-resolution read the tissue thumbnail is rendered
    # from -- the REAL producer, so the tests below go through the real
    # pixel chain rather than through a lambda standing in for it.
    def overview_downsample(self):
        return 4

    def read_region_lowres(self, ch, y0, y1, x0, x1, ds, normalize=False):
        ds = max(1, int(ds))
        return self._pattern(max(1, (y1 - y0) // ds),
                             max(1, (x1 - x0) // ds))

    def _pattern(self, h, w):
        """Not a constant: a gradient plus the slide's own value, so two
        slides cannot be confused by a normalisation that flattens them."""
        rows = np.linspace(0.0, 40.0, h, dtype=np.float32)[:, None]
        cols = np.linspace(0.0, 20.0, w, dtype=np.float32)[None, :]
        return (rows + cols + self.value).astype(np.float32)


def _rgb(value):
    return np.full((16, 16, 3), value, np.uint8)


def _page_showing_a(tmp_path, value=11.0, shape=(64, 64)):
    """Dataset A loaded, with its picture on both Tissue Previews."""
    page = sp.Step0Page()
    page.loader = _Loader(value, shape)
    page.ome_path = str(tmp_path / "A.tif")
    page.output_dir = str(tmp_path / "out")
    page.patches = [(0, 32, 0, 32)]
    page.current_patch_idx = 0
    page.nucleus_channel = "DAPI"
    page._rebuild_channel_list()
    page.current_channel = "CD3"
    page.overview.loader = page.loader
    page.overview.full_h = page.loader.shape[0]
    page.overview.full_w = page.loader.shape[1]
    return page


def _show_a_everywhere(page, value=200):
    """Put A's picture on both panels, the way the host does."""
    popup = page._ensure_tissue_navigator()
    for panel in page._registered_roi_overviews():
        panel.adopt_dataset(page._dataset_token())
        panel._on_overview_loaded(np.full((16, 16), 5.0, np.float32))
        panel.set_channel_image(_rgb(value), page._dataset_token())
        assert panel.img_item.image is not None
    return popup


def _switch_to_b(page, tmp_path, monkeypatch, *, value=99.0, raises=None,
                 load_overview=True, shape=(64, 64)):
    """The real Load entry, with only the loader constructor stubbed."""
    b = tmp_path / "B.tif"
    b.write_bytes(b"not-really-a-tiff")
    made = _Loader(value, shape)

    def _ctor(*_a, **_kw):
        if raises is not None:
            raise raises
        return made

    monkeypatch.setattr(sp, "OMETIFFLoader", _ctor)
    for name in ("warning", "critical", "information"):
        monkeypatch.setattr(sp.QMessageBox, name, lambda *a, **k: None)
    if not load_overview:
        # The read itself is driven explicitly by the test.
        monkeypatch.setattr(type(page.overview), "_load_overview",
                            lambda self: None)
    page._ome_path_edit.setText(str(b))
    page._out_path_edit.setText(str(tmp_path / "out"))
    page._panel_csv_edit.setText("")
    page._reload_from_paths()
    return made


def _panels(page):
    return {"page": page.overview,
            "popup": page._tissue_navigator_popup.overview}


def _blank(panel):
    return (panel._channel_rgb is None and panel._overview_arr is None
            and panel.img_item.image is None)


# ── 1. the real Load entry, with the popup already showing A ─────────────

def test_both_previews_lose_the_previous_slide_at_the_commit(
        app, tmp_path, monkeypatch):
    page = _page_showing_a(tmp_path)
    popup = _show_a_everywhere(page)
    try:
        _switch_to_b(page, tmp_path, monkeypatch, load_overview=False)

        for name, panel in _panels(page).items():
            assert _blank(panel), f"{name} still holds the previous slide"
        assert page._tissue_navigator_popup is popup      # one shared window

        # B arrives: both show B, and only B.
        for panel in _panels(page).values():
            panel._on_overview_loaded(np.full((16, 16), 99.0, np.float32),
                                      panel._ov_gen, panel.loader,
                                      panel.dataset_token())
        for name, panel in _panels(page).items():
            shown = np.asarray(panel.img_item.image)
            assert float(shown.max()) == pytest.approx(99.0), name
    finally:
        page.close()


def test_the_transition_does_not_depend_on_the_rest_of_the_reload(
        app, tmp_path, monkeypatch):
    """The previous slide leaves because the switch was COMMITTED.

    Before this, the popup was only rebound in the model feed at the very end
    of the reload, so every step in between -- the overview artists, the ROI
    reset, the patch rebuild -- had to run first. One that raised left the
    previous slide's tissue on the shared preview under the new slide's name.
    """
    page = _page_showing_a(tmp_path)
    popup = _show_a_everywhere(page)
    try:
        boom = RuntimeError("a rebuild step failed")
        monkeypatch.setattr(type(page), "_on_patches_changed",
                            lambda self, patches: (_ for _ in ()).throw(boom))
        with pytest.raises(RuntimeError):
            _switch_to_b(page, tmp_path, monkeypatch, load_overview=False)

        for name, panel in _panels(page).items():
            assert _blank(panel), (
                f"{name} kept the previous slide because a later step failed")
            assert panel.dataset_token() == page._dataset_token(), name
        assert page._tissue_navigator_popup is popup
    finally:
        page.close()


# ── 2. late reads from A, on BOTH panels ────────────────────────────────

def test_a_late_read_of_the_previous_slide_is_refused_on_both_panels(
        app, tmp_path, monkeypatch):
    page = _page_showing_a(tmp_path)
    _show_a_everywhere(page)
    try:
        old = {name: (getattr(panel, '_ov_gen', 0), panel.loader,
                      panel.dataset_token())
               for name, panel in _panels(page).items()}
        _switch_to_b(page, tmp_path, monkeypatch, load_overview=False)

        for name, panel in _panels(page).items():
            gen, loader, token = old[name]
            panel._on_overview_loaded(np.full((16, 16), 11.0, np.float32),
                                      gen, loader, token)
            assert _blank(panel), f"{name} accepted the previous slide's read"

        # And B's own read is still the one that lands.
        for panel in _panels(page).values():
            panel._on_overview_loaded(np.full((16, 16), 99.0, np.float32),
                                      panel._ov_gen, panel.loader,
                                      panel.dataset_token())
        for name, panel in _panels(page).items():
            assert float(np.asarray(panel.img_item.image).max()) == \
                pytest.approx(99.0), name
    finally:
        page.close()


def test_binding_a_panel_invalidates_the_read_it_has_in_flight(app, tmp_path):
    """Not at the next load: AT the switch.

    `forget_pixels()` cleared the pixels and left the generation alone, so a
    result that arrived between the clearing and the next `_load_overview`
    was installed -- the previous slide, back on a panel that had already let
    it go.
    """
    page = _page_showing_a(tmp_path)
    panel = page.overview
    try:
        panel.adopt_dataset(page._dataset_token())
        in_flight = panel._ov_gen = 7
        token = panel.dataset_token()

        panel.bind_dataset(("gen2", "/B.tif"), loader=page.loader,
                           full_shape=(64, 64))

        panel._on_overview_loaded(np.full((16, 16), 11.0, np.float32),
                                  in_flight, page.loader, token)
        assert _blank(panel)
        assert panel._ov_gen != in_flight
    finally:
        page.close()


def test_a_result_that_names_another_dataset_is_refused_even_if_it_looks_current(
        app, tmp_path):
    """The third check, and the only one that is about the RESULT itself.

    The generation is per PANEL and the loader can be reused, so both of the
    older checks can pass for a read that was started for another slide. A
    read carries the dataset it was started for, and that is what decides.
    """
    page = _page_showing_a(tmp_path)
    panel = page.overview
    try:
        panel.adopt_dataset(("gen1", "/A.tif"))
        panel._ov_gen = 4
        loader = panel.loader

        # Rebound to another slide, and its generation happens to be back at
        # the value the older read carries (a panel that was rebuilt, a read
        # replayed): neither the generation nor the loader can tell.
        panel.bind_dataset(("gen2", "/B.tif"), loader=loader)
        panel._ov_gen = 4

        panel._on_overview_loaded(np.full((16, 16), 11.0, np.float32),
                                  4, loader, ("gen1", "/A.tif"))
        assert _blank(panel), "a read for another slide was installed"

        panel._on_overview_loaded(np.full((16, 16), 99.0, np.float32),
                                  4, loader, ("gen2", "/B.tif"))
        assert float(np.asarray(panel.img_item.image).max()) == \
            pytest.approx(99.0)
    finally:
        page.close()


def test_the_same_loader_under_a_new_dataset_still_reloads_the_popup(
        app, tmp_path, monkeypatch):
    """"Once per loader" is not "once per slide".

    The popup skipped its load when the loader object was one it had already
    loaded for -- so a slide that arrives on a familiar loader kept the
    previous thumbnail. Keyed by the DATASET now, with the loader kept in the
    key so a new loader still reloads.
    """
    page = _page_showing_a(tmp_path)
    popup = _show_a_everywhere(page)
    try:
        loads = []
        monkeypatch.setattr(type(popup.overview), "_load_overview",
                            lambda self: loads.append(self.dataset_token()))
        # Through the production path both times: the first feed is what
        # records "already loaded", the second is the slide change.
        page._roi_model.adopt(loader=page.loader)
        page._feed_popup_from_model()
        first = list(loads)

        page._dataset_gen += 1                      # another slide...
        page._feed_popup_from_model()               # ...on the same loader

        assert len(loads) > len(first), (
            f"the popup skipped the new slide because the loader was "
            f"familiar: {loads}")
        assert loads[-1] == page._dataset_token(), loads
    finally:
        page.close()


# ── 3. the host push, and the timer that was armed for A ────────────────

def test_a_push_rendered_for_the_previous_slide_is_refused(
        app, tmp_path, monkeypatch):
    page = _page_showing_a(tmp_path)
    _show_a_everywhere(page)
    try:
        stale_token = page._dataset_token()
        _switch_to_b(page, tmp_path, monkeypatch, load_overview=False)

        for name, panel in _panels(page).items():
            assert panel.set_channel_image(_rgb(200), stale_token) is False
            assert _blank(panel), f"{name} took the previous slide's picture"
    finally:
        page.close()


def test_a_frame_requested_for_the_previous_slide_renders_the_new_one(
        app, tmp_path, monkeypatch):
    """A frame is pending while A is current and its slot arrives after the
    switch -- the real sequence, driven through Block01's real frame clock.

    What it must NOT do is put A back. What it DOES do is the honest half of
    the contract: a pending revision carries no picture, so when its slot
    comes the frame is composed from the CURRENT state and its push is the
    new slide's. With B's pixels not read yet there is nothing to compose,
    and the panels stay empty -- which is what "Loading" means.

    (The dataset bind at the switch retires the pending revision outright;
    this pins that the OUTCOME is the new slide or nothing, by whichever of
    the two mechanisms gets there first.)
    """
    page = _page_showing_a(tmp_path)
    _show_a_everywhere(page)
    try:
        pushed = []
        for panel in _panels(page).values():
            panel.set_channel_image = (
                lambda rgb, token=None, _p=panel: pushed.append((_p, token)))

        coordinator = page.display.coordinator
        # Armed, not fired: a frame is due and has not been composed yet.
        coordinator._arm(5000)
        page._queue_tissue_preview()
        assert coordinator.frame_stats()["pending"] == 1
        a_token = page._dataset_token()

        _switch_to_b(page, tmp_path, monkeypatch, load_overview=False)
        # The real clock, fired by the real event loop.
        coordinator._arm(0)
        _pump(app, 1.0,
              until=lambda: not coordinator.frame_stats()["in_flight"])

        assert all(token != a_token for _panel, token in pushed), pushed
        for name, panel in _panels(page).items():
            assert panel._channel_rgb is None, name

        # And when the page CAN render, the push is the new slide's and it is
        # accepted -- the same path, with B's pixels available. A PATTERNED
        # array, not a constant: a display window is seeded from these pixels
        # now, and a constant one has no window to find.
        monkeypatch.setattr(
            type(page), "_slide_lowres_array",
            lambda self, ch, **k: (np.arange(16 * 16, dtype=np.float32)
                                   .reshape(16, 16) + 99.0))
        page.current_channel = "CD3"
        pushed.clear()
        # Re-install the spies: the switch rebuilt the panels, so the objects
        # registered now are not the ones patched before it.
        for panel in _panels(page).values():
            panel.set_channel_image = (
                lambda rgb, token=None, _p=panel: pushed.append((_p, token)))
        page._update_tissue_preview()
        assert pushed, "nothing was rendered for the new slide"
        assert {token for _panel, token in pushed} == {page._dataset_token()}
    finally:
        page.close()


def test_the_pushed_picture_carries_the_dataset_it_was_rendered_from(
        app, tmp_path, monkeypatch):
    """The identity is produced WITH the pixels, not signed at install time.

    `_tissue_preview_rgb` returns the picture and the dataset its pixels were
    fetched under, and that is what travels to the panel. A renderer that
    hands back the previous slide's pixels therefore hands back the previous
    slide's token too, and the push is refused -- instead of the page signing
    a bare array with whatever it is showing at that moment.
    """
    page = _page_showing_a(tmp_path)
    _show_a_everywhere(page)
    try:
        a_token = page._dataset_token()
        pushed = []
        for panel in _panels(page).values():
            panel.set_channel_image = (
                lambda rgb, token=None: pushed.append(token) or True)

        rgb, token = page._tissue_preview_rgb()
        assert rgb is not None and token == a_token

        page._update_tissue_preview()
        assert pushed and set(pushed) == {a_token}, pushed

        # A renderer handing back the PREVIOUS slide's pixels returns the
        # previous slide's token with them; the page moves on, and the push
        # is refused rather than re-signed.
        pushed.clear()
        for panel in _panels(page).values():
            del panel.set_channel_image
        page._dataset_gen += 1
        b_token = page._dataset_token()
        for panel in _panels(page).values():
            panel.bind_dataset(b_token, loader=page.loader,
                               full_shape=(64, 64))
        monkeypatch.setattr(type(page), "_tissue_preview_rgb",
                            lambda self: (_rgb(200), a_token))

        page._update_tissue_preview()

        for name, panel in _panels(page).items():
            assert panel._channel_rgb is None, (
                f"{name} took a picture of the previous slide")
    finally:
        page.close()


# ── 4. hidden and minimized popups ──────────────────────────────────────

@pytest.mark.parametrize("state", ["visible", "hidden", "minimized"])
def test_the_popup_follows_the_switch_in_every_window_state(
        app, tmp_path, monkeypatch, state):
    page = _page_showing_a(tmp_path)
    popup = _show_a_everywhere(page)
    if state == "hidden":
        popup.hide()
    elif state == "minimized":
        popup.minimize_to_bar()
    try:
        loads = []
        monkeypatch.setattr(type(popup.overview), "_load_overview",
                            lambda self: loads.append(self.dataset_token()))
        # `load_overview=True`: the recorder above IS the load path here.
        _switch_to_b(page, tmp_path, monkeypatch)

        assert _blank(popup.overview), f"{state} popup kept the old slide"
        assert loads and loads[-1] == page._dataset_token(), (
            f"the {state} popup never asked for the new slide: {loads}")
    finally:
        page.close()


def test_the_popup_reloads_even_when_the_loader_object_looks_familiar(
        app, tmp_path, monkeypatch):
    """Identity is the DATASET, not the loader object.

    The popup reloaded its thumbnail "once per loader", so a slide change
    that reused a loader object the popup had already seen was skipped
    entirely -- the previous slide's thumbnail stayed up for good.
    """
    page = _page_showing_a(tmp_path)
    popup = _show_a_everywhere(page)
    try:
        loads = []
        monkeypatch.setattr(type(popup.overview), "_load_overview",
                            lambda self: loads.append(self.dataset_token()))
        same_loader = page.loader
        _switch_to_b(page, tmp_path, monkeypatch)
        # The page ends up with a loader object the popup has seen before.
        page.loader = same_loader
        page._roi_model.adopt(loader=same_loader)
        page._feed_popup_from_model()

        assert loads, "the popup never reloaded"
        assert loads[-1] == page._dataset_token()
    finally:
        page.close()


# ── 5/6. identical geometry, and a pre-commit failure ───────────────────

def test_two_slides_that_look_alike_still_cannot_share_pixels(
        app, tmp_path, monkeypatch):
    page = _page_showing_a(tmp_path)
    _show_a_everywhere(page)
    page.overview.set_rois_and_patches(
        [{"name": "ROI_1", "bbox_fullres": [0, 32, 0, 32],
          "polygon_display": [(0, 0), (32, 0), (32, 32)],
          "polygon_fullres": [(0, 0), (32, 0), (32, 32)]}],
        [(0, 16, 0, 16)], False)
    try:
        a_pixels = np.asarray(page.overview.img_item.image).copy()
        _switch_to_b(page, tmp_path, monkeypatch, value=99.0,
                     load_overview=False)
        for panel in _panels(page).values():
            panel._on_overview_loaded(np.full((16, 16), 99.0, np.float32),
                                      panel._ov_gen, panel.loader,
                                      panel.dataset_token())

        for name, panel in _panels(page).items():
            shown = np.asarray(panel.img_item.image)
            assert shown.shape != a_pixels.shape or not np.array_equal(
                shown, a_pixels), name
            assert float(shown.max()) == pytest.approx(99.0), name
    finally:
        page.close()


def test_a_load_that_fails_before_the_commit_keeps_the_current_slide(
        app, tmp_path, monkeypatch):
    page = _page_showing_a(tmp_path)
    popup = _show_a_everywhere(page)
    try:
        before = {name: np.asarray(panel.img_item.image).copy()
                  for name, panel in _panels(page).items()}
        camera = popup.overview.vb.viewRange()

        _switch_to_b(page, tmp_path, monkeypatch,
                     raises=RuntimeError("bad file"), load_overview=False)

        for name, panel in _panels(page).items():
            assert panel._channel_rgb is not None, name
            assert np.array_equal(np.asarray(panel.img_item.image),
                                  before[name]), name
        assert popup.overview.vb.viewRange() == camera
    finally:
        page.close()


# ── 7. A → B → C, completing out of order ───────────────────────────────

def test_three_slides_completing_out_of_order_end_on_the_last(
        app, tmp_path, monkeypatch):
    page = _page_showing_a(tmp_path)
    _show_a_everywhere(page)
    try:
        reads = []
        for panel in _panels(page).values():
            reads.append((panel, getattr(panel, '_ov_gen', 0), panel.loader,
                          panel.dataset_token()))
        _switch_to_b(page, tmp_path, monkeypatch, value=22.0,
                     load_overview=False)
        for panel in _panels(page).values():
            reads.append((panel, getattr(panel, '_ov_gen', 0), panel.loader,
                          panel.dataset_token()))
        _switch_to_b(page, tmp_path, monkeypatch, value=33.0,
                     load_overview=False)

        # C's reads land first, then B's and A's, which are refused.
        for panel in _panels(page).values():
            panel._on_overview_loaded(np.full((16, 16), 33.0, np.float32),
                                      panel._ov_gen, panel.loader,
                                      panel.dataset_token())
        for panel, gen, loader, token in reversed(reads):
            panel._on_overview_loaded(np.full((16, 16), 11.0, np.float32),
                                      gen, loader, token)

        for name, panel in _panels(page).items():
            assert float(np.asarray(panel.img_item.image).max()) == \
                pytest.approx(33.0), name
    finally:
        page.close()


# ── real overview threads: lifetime, not just which pixels win ──────────

class _BarrierLoader(_Loader):
    """A loader whose read blocks until it is released, per slide."""

    def __init__(self, value, gate):
        super().__init__(value)
        self.gate = gate
        self.entered = threading.Event()

    def read_region(self, ch, y0, y1, x0, x1, downsample=1, normalize=True,
                    **_kw):
        self.entered.set()
        assert self.gate.wait(30), "a read was never released"
        return super().read_region(ch, y0, y1, x0, x1, downsample=downsample,
                                   normalize=normalize, **_kw)


def _pump(app, seconds=2.0, until=None):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        app.processEvents()
        if until is not None and until():
            return True
        time.sleep(0.005)
    app.processEvents()
    return until() if until is not None else True


def test_three_real_reads_may_be_in_flight_and_only_the_last_one_lands(
        app, tmp_path):
    """Real `OverviewLoaderThread`s, a real event loop, and a barrier.

    The panel used to keep ONE reference (`self._ov_thread`), so starting the
    next slide's read dropped the previous one's QThread while it was still
    inside `read_region` -- "QThread: Destroyed while thread is still
    running". Refusing a result by identity says nothing about the object's
    lifetime; these are two different problems.
    """
    gates = [threading.Event() for _ in range(3)]
    loaders = [_BarrierLoader(v, g) for v, g in zip((11.0, 22.0, 33.0), gates)]
    panel = op.OverviewPanel(loaders[0], "DAPI", lazy=True)
    panel.full_h, panel.full_w = 64, 64
    started = set(op.live_overview_workers())
    try:
        for i, loader in enumerate(loaders):
            panel.bind_dataset((i, f"/slide{i}.tif"), loader=loader,
                               full_shape=(64, 64))
            panel._load_overview()
            assert loader.entered.wait(10), f"read {i} never started"

        mine = op.live_overview_workers() - started
        assert len(mine) == 3, (
            f"a running overview thread lost its last reference: {mine}")
        assert all(w.isRunning() for w in mine)

        # Released in the order that is worst for a panel keeping one
        # reference: the oldest last.
        for gate in reversed(gates):
            gate.set()
        assert _pump(app, 10.0,
                     until=lambda: not (op.live_overview_workers() - started)), (
            "a finished worker was never retired")

        shown = panel.img_item.image
        assert shown is not None
        # The slide's own value is the gradient's floor, so the minimum
        # names which slide these pixels are.
        assert float(np.asarray(shown).min()) == pytest.approx(33.0), (
            "a previous slide's read landed")
        assert panel.dataset_token() == (2, "/slide2.tif")
    finally:
        for gate in gates:
            gate.set()
        _pump(app, 5.0,
              until=lambda: not (op.live_overview_workers() - started))
        panel.deleteLater()


def test_a_panel_that_is_closed_does_not_release_a_running_read(app,
                                                                tmp_path):
    """The panel dies first; the thread must not die with it."""
    gate = threading.Event()
    loader = _BarrierLoader(11.0, gate)
    before = set(op.live_overview_workers())
    panel = op.OverviewPanel(loader, "DAPI", lazy=True)
    panel.full_h, panel.full_w = 64, 64
    panel.bind_dataset((1, "/A.tif"), loader=loader, full_shape=(64, 64))
    panel._load_overview()
    assert loader.entered.wait(10)
    try:
        worker = (op.live_overview_workers() - before).pop()
        panel.close()
        panel.deleteLater()
        del panel
        gc.collect()
        app.processEvents()

        assert worker.isRunning(), "the read was abandoned with the panel"
        gate.set()
        assert _pump(app, 10.0,
                     until=lambda: not (op.live_overview_workers() - before))
    finally:
        gate.set()
        _pump(app, 5.0,
              until=lambda: not (op.live_overview_workers() - before))


def test_a_failed_read_of_the_previous_slide_does_not_touch_the_new_one(
        app, tmp_path):
    """The FAILURE has an identity too.

    A read of slide A that fails after the switch would otherwise turn the new
    slide's "Loading…" into "Overview load failed" -- the previous slide
    writing over the current one's state, which is the same defect as
    installing its pixels.
    """
    class _Boom(_Loader):
        def __init__(self, value, gate):
            super().__init__(value)
            self.gate = gate
            self.entered = threading.Event()

        def read_region(self, *_a, **_k):
            self.entered.set()
            assert self.gate.wait(30)
            raise RuntimeError("A is unreadable")

    gate = threading.Event()
    bad = _Boom(11.0, gate)
    before = set(op.live_overview_workers())
    panel = op.OverviewPanel(bad, "DAPI", lazy=True)
    panel.full_h, panel.full_w = 64, 64
    try:
        panel.bind_dataset((1, "/A.tif"), loader=bad, full_shape=(64, 64))
        panel._load_overview()
        assert bad.entered.wait(10)

        good = _Loader(99.0)
        panel.bind_dataset((2, "/B.tif"), loader=good, full_shape=(64, 64))
        panel.status.setText("Loading overview, please wait...")

        gate.set()                       # A fails, after the switch
        _pump(app, 2.0,
              until=lambda: not (op.live_overview_workers() - before))

        assert "failed" not in panel.status.text().lower(), panel.status.text()
        assert _blank(panel)

        # B's own failure IS reported.
        panel._on_overview_failed("B is unreadable", panel._ov_gen,
                                  panel.loader, panel.dataset_token())
        assert "failed" in panel.status.text().lower()
    finally:
        gate.set()
        _pump(app, 5.0,
              until=lambda: not (op.live_overview_workers() - before))
        panel.deleteLater()


# ── the commit point is fail-safe ───────────────────────────────────────

def test_a_panel_that_cannot_clear_itself_takes_the_old_picture_off_screen(
        app, tmp_path, monkeypatch):
    """`img_item.clear()` raising may not leave slide A VISIBLE.

    Emptying the two stores is not enough: what the user is looking at is the
    image item, and "the Tissue Preview still shows the old slide" is the
    whole complaint. So the item is hidden (or taken out of the view) and the
    panel counts as empty because nothing of A can be seen -- and the new
    slide's picture puts a working item back.
    """
    page = _page_showing_a(tmp_path)
    popup = _show_a_everywhere(page)
    try:
        # This panel's item only: patching the CLASS would break every other
        # image item on the page and prove nothing about this contract.
        def _refuse():
            raise RuntimeError("the item refuses")

        panel = page.overview
        panel.img_item.clear = _refuse
        a_pixels = np.asarray(panel.img_item.image).copy()

        _switch_to_b(page, tmp_path, monkeypatch, load_overview=False)

        for name, other in _panels(page).items():
            assert other._channel_rgb is None, name
            assert other._overview_arr is None, name
            assert other.dataset_token() == page._dataset_token(), name
            assert other.is_empty(), f"{name} can still show the old slide"
        # Nothing of A is on the screen: either the item holds no image, or
        # it is not being drawn at all.
        assert not (panel.img_item.isVisible()
                    and panel.img_item.scene() is not None
                    and panel.img_item.image is not None
                    and np.array_equal(np.asarray(panel.img_item.image),
                                       a_pixels)), "slide A is still visible"
        # The page reports a clean load: the previous slide is not on screen.
        assert "could not be emptied" not in page._load_status.text()

        # B arrives: the panel comes back, showing B and only B.
        panel._on_overview_loaded(np.full((16, 16), 99.0, np.float32),
                                  panel._ov_gen, panel.loader,
                                  panel.dataset_token())
        assert panel.img_item.isVisible()
        assert panel.img_item.scene() is not None
        assert float(np.asarray(panel.img_item.image).max()) == \
            pytest.approx(99.0)
        assert popup is page._tissue_navigator_popup
    finally:
        page.close()


def test_a_same_token_rebind_reports_safety_and_keeps_the_picture(app,
                                                                  tmp_path):
    """Rebinding to the slide already showing is not a clear -- and the
    answer is about SAFETY, not about whether anything changed."""
    page = _page_showing_a(tmp_path)
    panel = page.overview
    try:
        token = page._dataset_token()
        panel.bind_dataset(token, loader=page.loader, full_shape=(64, 64))
        panel.set_channel_image(_rgb(200), token)
        assert panel.img_item.image is not None

        assert panel.bind_dataset(token, loader=page.loader) is True
        assert panel._channel_rgb is not None, (
            "a context refresh threw away the current slide's picture")

        # A panel that can neither clear nor hide its item is NOT safe, and
        # a same-token rebind may not say it is.
        panel.img_item.clear = lambda: (_ for _ in ()).throw(RuntimeError("x"))
        panel.img_item.setVisible = lambda _v: None
        panel.vb.removeItem = lambda _i: (_ for _ in ()).throw(
            RuntimeError("stuck"))
        assert panel.bind_dataset((99, "/C.tif"), loader=page.loader) is False
        assert panel.bind_dataset((99, "/C.tif"), loader=page.loader) is False
    finally:
        page.close()


# ── a destroyed panel is not a hidden one ───────────────────────────────

@pytest.mark.parametrize("outcome", ["done", "error"])
def test_a_destroyed_panel_is_never_reached_by_its_own_read(app, tmp_path,
                                                            outcome, capfd):
    """The worker outlives the window on purpose; the window must not outlive
    itself through the worker, and the result must not be delivered into a
    C++ object that is gone.

    The destruction is made EXPLICIT with `sip.delete`, so the state under
    test is the one that crashes in production -- a live Python wrapper whose
    C++ object has been deleted -- rather than whatever the garbage collector
    happens to do. The panel is also kept referenced here, which is the worse
    case: a weak reference alone would have gone quiet on its own.
    """
    from PyQt5 import sip

    class _Boom(_Loader):
        def __init__(self, gate):
            super().__init__(11.0)
            self.gate = gate
            self.entered = threading.Event()

        def read_region(self, *_a, **_k):
            self.entered.set()
            assert self.gate.wait(30)
            raise RuntimeError("A is unreadable")

    gate = threading.Event()
    loader = _Boom(gate) if outcome == "error" else _BarrierLoader(11.0, gate)
    before = set(op.live_overview_workers())
    panel = op.OverviewPanel(loader, "DAPI", lazy=True)
    panel.full_h, panel.full_w = 64, 64
    panel.bind_dataset((1, "/A.tif"), loader=loader, full_shape=(64, 64))
    panel._load_overview()
    assert loader.entered.wait(10)
    worker = (op.live_overview_workers() - before).pop()
    capfd.readouterr()
    try:
        panel.close()
        sip.delete(panel)
        assert sip.isdeleted(panel), "the C++ object was not destroyed"
        assert worker.isRunning(), "the read was abandoned with the panel"

        gate.set()
        assert _pump(app, 10.0,
                     until=lambda: not (op.live_overview_workers() - before)), (
            "the worker never finished")

        out, err = capfd.readouterr()
        for bad in ("has been deleted", "Traceback",
                    "Destroyed while thread is still running"):
            assert bad not in err, err
            assert bad not in out, out
    finally:
        gate.set()
        _pump(app, 5.0,
              until=lambda: not (op.live_overview_workers() - before))


def test_a_panel_whose_cpp_object_died_without_closing_is_still_safe(
        app, tmp_path, capfd):
    """The case the `_disposed` flag cannot catch.

    A panel can lose its C++ object without anyone calling `close()` -- its
    parent is destroyed, or Qt takes it down. The Python wrapper is still
    there and still says it is not disposed, so the result would be delivered
    into a deleted object: "wrapped C/C++ object of type OverviewPanel has
    been deleted". The relay touches the object before it delivers anything.
    """
    from PyQt5 import sip

    gate = threading.Event()
    loader = _BarrierLoader(11.0, gate)
    before = set(op.live_overview_workers())
    panel = op.OverviewPanel(loader, "DAPI", lazy=True)
    panel.full_h, panel.full_w = 64, 64
    panel.bind_dataset((1, "/A.tif"), loader=loader, full_shape=(64, 64))
    panel._load_overview()
    assert loader.entered.wait(10)
    capfd.readouterr()
    try:
        sip.delete(panel)                     # no close(), no dispose flag
        assert sip.isdeleted(panel)
        assert panel._disposed is False, (
            "this test is about the case the dispose flag does NOT cover")

        gate.set()
        assert _pump(app, 10.0,
                     until=lambda: not (op.live_overview_workers() - before))

        out, err = capfd.readouterr()
        for bad in ("has been deleted", "Traceback"):
            assert bad not in err, err
            assert bad not in out, out
    finally:
        gate.set()
        _pump(app, 5.0,
              until=lambda: not (op.live_overview_workers() - before))


def test_a_panel_dropped_while_its_read_runs_is_not_kept_alive_by_it(
        app, tmp_path):
    """The keep-alive is for the THREAD, not for the window it came from."""
    gate = threading.Event()
    loader = _BarrierLoader(11.0, gate)
    before = set(op.live_overview_workers())
    panel = op.OverviewPanel(loader, "DAPI", lazy=True)
    panel.full_h, panel.full_w = 64, 64
    panel.bind_dataset((1, "/A.tif"), loader=loader, full_shape=(64, 64))
    panel._load_overview()
    assert loader.entered.wait(10)
    ref = weakref.ref(panel)
    try:
        panel.close()
        panel.deleteLater()
        del panel
        gc.collect()
        app.processEvents()

        assert ref() is None, (
            "the worker's connection kept the panel alive")
        assert (op.live_overview_workers() - before), \
            "the read was dropped with the panel"

        gate.set()
        assert _pump(app, 10.0,
                     until=lambda: not (op.live_overview_workers() - before))
    finally:
        gate.set()
        _pump(app, 5.0,
              until=lambda: not (op.live_overview_workers() - before))


def test_a_hidden_panel_still_takes_the_current_slides_result(app, tmp_path):
    """Hidden is not disposed: a collapsed or minimised popup is still showing
    the current dataset and must take its picture."""
    page = _page_showing_a(tmp_path)
    popup = _show_a_everywhere(page)
    try:
        popup.minimize_to_bar()
        popup.hide()
        panel = popup.overview
        panel.bind_dataset((7, "/B.tif"), loader=page.loader,
                           full_shape=(64, 64))

        panel._on_overview_loaded(np.full((16, 16), 99.0, np.float32),
                                  panel._ov_gen, panel.loader,
                                  panel.dataset_token())

        assert float(np.asarray(panel.img_item.image).max()) == \
            pytest.approx(99.0)
    finally:
        page.close()


# ── the application does not exit over a running read ───────────────────

def test_closing_the_application_waits_for_an_overview_read(app, tmp_path):
    """A read cannot be interrupted, so keeping its thread alive only moves
    the crash to interpreter teardown unless the application waits too."""
    from PyQt5 import QtGui
    from block01.ui.main_window import MainWindow

    gate = threading.Event()
    loader = _BarrierLoader(11.0, gate)
    before = set(op.live_overview_workers())
    w = MainWindow()
    panel = w._step0.overview
    panel.loader = loader
    panel.full_h, panel.full_w = 64, 64
    panel.bind_dataset((1, "/A.tif"), loader=loader, full_shape=(64, 64))
    panel._load_overview()
    assert loader.entered.wait(10)
    try:
        event = QtGui.QCloseEvent()
        w.closeEvent(event)
        assert not event.isAccepted(), (
            "the window closed over a running overview read")
        assert "overview read" in w.prev_status.text().lower(), \
            w.prev_status.text()
        assert (op.live_overview_workers() - before)

        gate.set()
        assert _pump(app, 10.0,
                     until=lambda: not (op.live_overview_workers() - before))

        event = QtGui.QCloseEvent()
        w.closeEvent(event)
        assert event.isAccepted(), "the window would not close afterwards"
    finally:
        gate.set()
        _pump(app, 5.0,
              until=lambda: not (op.live_overview_workers() - before))
        w.close()


def test_a_normal_load_never_waits_for_the_previous_read(app, tmp_path,
                                                         monkeypatch):
    """The waiting is for EXIT only. A Load with a read still in flight must
    return at once -- the previous slide's read is refused by identity, not
    waited for."""
    gate = threading.Event()
    loader = _BarrierLoader(11.0, gate)
    before = set(op.live_overview_workers())
    page = _page_showing_a(tmp_path)
    page.loader = loader
    page.overview.loader = loader
    page.overview.bind_dataset((1, "/A.tif"), loader=loader,
                               full_shape=(64, 64))
    page.overview._load_overview()
    assert loader.entered.wait(10)
    try:
        started = time.monotonic()
        _switch_to_b(page, tmp_path, monkeypatch, load_overview=False)
        elapsed = time.monotonic() - started

        assert elapsed < 1.0, f"the Load waited {elapsed:.1f}s for a read"
        assert (op.live_overview_workers() - before), \
            "the running read was dropped instead of kept"

        gate.set()
        assert _pump(app, 10.0,
                     until=lambda: not (op.live_overview_workers() - before))
        # The held read finishes and is refused; what the panel shows is the
        # new slide's picture (rendered from B's own pixels) or nothing at
        # all -- never A's.
        panel = page.overview
        for store in (panel._channel_rgb, panel._overview_arr):
            if store is not None:
                assert float(np.asarray(store).min()) != pytest.approx(11.0), (
                    "the previous slide's read landed")
    finally:
        gate.set()
        _pump(app, 5.0,
              until=lambda: not (op.live_overview_workers() - before))
        page.close()


def test_a_bound_panel_refuses_a_picture_that_cannot_name_its_slide(
        app, tmp_path):
    """Fail closed. An install path that cannot say which dataset it is for
    is a path that cannot be checked, and those are what put the previous
    slide back."""
    page = _page_showing_a(tmp_path)
    panel = page.overview
    try:
        panel.bind_dataset(page._dataset_token(), loader=page.loader,
                           full_shape=(64, 64))
        assert panel.set_channel_image(_rgb(200)) is False
        assert _blank(panel)
        assert panel.set_channel_image(_rgb(200),
                                       page._dataset_token()) is not False
        assert panel.img_item.image is not None

        # A panel nobody has bound is standalone: it still draws, which is
        # what the camera and drawing tests need.
        loose = op.OverviewPanel(page.loader, "DAPI", lazy=True)
        loose.full_h, loose.full_w = 64, 64
        assert loose.set_channel_image(_rgb(120)) is not False
        loose.deleteLater()
    finally:
        page.close()


# ── the real machine's picture: B's geometry with A's pixels ────────────

class _Record:
    """What a viewer's shared overview store hands back."""

    def __init__(self, arr, source, channel, level=0):
        self.arr = arr
        self.source = source
        self.channel = channel
        self.level = level


class _Source:
    def __init__(self, path):
        self.dataset_path = path
        self.dataset_fingerprint = "1:1"
        self.stage = "raw"


class _Controller:
    """A faithful stand-in for a viewer controller: it answers with whatever
    its shared store holds for the SOURCE it was built for.

    The store outlives individual stacks in production and is keyed by
    source, so a controller can be asked for a channel and answer with a
    record produced for another slide. That is the shape this reproduces --
    with the page's own adoption path, not with a lambda.
    """

    def __init__(self, record):
        self.record = record

    def overview_record(self, channel, level=None, source=None):
        return self.record if self.record.channel == channel else None

    def overview_read_pending(self, channel, level=None, source=None):
        return False


def test_a_record_of_the_previous_slide_is_never_adopted_or_drawn(
        app, tmp_path, monkeypatch):
    """THE REPORTED PICTURE: the previous slide, squashed to the new slide's
    shape.

    A and B have different proportions (64x128 and 128x64). After the switch,
    a viewer-side record still holding A's pixels is offered to the page. It
    used to be adopted -- the record's own source was never checked -- cached
    under the channel NAME, rendered, signed with the page's current token
    and pushed; the panel's entry guard saw a token that matched, and
    `_apply_thumbnail` stretched A's array onto B's rectangle. Every one of
    those steps is now checked against the dataset the pixels came from.
    """
    page = _page_showing_a(tmp_path, shape=(64, 128))      # A: wide and short
    _show_a_everywhere(page)
    a_path = os.path.abspath(page.ome_path)
    a_pixels = _Loader(11.0, (64, 128))._pattern(16, 32)
    stale = _Controller(_Record(a_pixels, _Source(a_path), "CD3"))
    try:
        _switch_to_b(page, tmp_path, monkeypatch, value=99.0,
                     shape=(128, 64), load_overview=False)  # B: narrow, tall

        # At the commit, nothing of A is left anywhere.
        for name, panel in _panels(page).items():
            assert _blank(panel), name

        # The previous slide's record is offered to the page, through the
        # real adoption path.
        monkeypatch.setattr(type(page), "_overview_hosts",
                            lambda self: [stale])
        page.current_channel = "CD3"
        page._slide_lowres.clear()

        served = page._slide_lowres_array("CD3", blocking=False)
        assert served is not None
        assert float(np.asarray(served).min()) == pytest.approx(99.0), (
            "the page adopted a record belonging to the previous slide")
        assert np.asarray(served).shape == (32, 16), (
            "the adopted array has the previous slide's proportions")

        rgb, token = page._tissue_preview_rgb()
        assert token == page._dataset_token()
        page._update_tissue_preview()
        for name, panel in _panels(page).items():
            drawn = panel._channel_rgb
            if drawn is not None:
                assert tuple(np.asarray(drawn).shape[:2]) == (32, 16), (
                    f"{name} was given the previous slide's pixels: "
                    f"{np.asarray(drawn).shape}")
                assert panel._channel_rgb_token == page._dataset_token(), name

        # B's own overview arrives and is what the panels show, at B's shape.
        for panel in _panels(page).values():
            panel._on_overview_loaded(
                _Loader(99.0, (128, 64))._pattern(32, 16), panel._ov_gen,
                panel.loader, panel.dataset_token())
        for name, panel in _panels(page).items():
            shown = np.asarray(panel.img_item.image)
            # B's proportions, whichever of the two stores is on top (the
            # channel picture wins when there is one, and it is B's).
            assert tuple(shown.shape[:2]) == (32, 16), (name, shown.shape)
            assert (panel.ov_h, panel.ov_w) == (32, 16), name
            assert panel._overview_arr_token == page._dataset_token(), name
            if panel._channel_rgb is not None:
                assert panel._channel_rgb_token == page._dataset_token(), name
    finally:
        page.close()


def test_a_store_holding_another_slides_pixels_is_dropped_at_paint(app,
                                                                   tmp_path):
    """The last line: even a store that got past the entry guard is checked
    again when it is drawn.

    Constructed directly -- pixels of A, token of B -- because that is the
    state the entry guard cannot see: it checks what the CALLER claims, and
    a caller handed the wrong array claims the wrong thing in good faith.
    """
    page = _page_showing_a(tmp_path, shape=(64, 128))
    panel = page.overview
    try:
        a_token = page._dataset_token()
        panel.bind_dataset(a_token, loader=page.loader, full_shape=(64, 128))
        panel.set_channel_image(_rgb(200), a_token)
        assert panel.img_item.image is not None

        # The panel moves to B; the store is left behind, as a bypassed
        # guard would leave it.
        b_token = (a_token[0] + 1, "/B.tif")
        panel._dataset_token = b_token
        panel.full_h, panel.full_w = 128, 64

        panel._apply_thumbnail()

        assert panel._channel_rgb is None, (
            "the previous slide's picture was drawn on the new slide's "
            "rectangle")
        assert panel.img_item.image is None

        # And the new slide's own overview still draws.
        panel._on_overview_loaded(np.full((32, 16), 99.0, np.float32),
                                  panel._ov_gen, panel.loader, b_token)
        assert float(np.asarray(panel.img_item.image).min()) == \
            pytest.approx(99.0)
    finally:
        page.close()


def test_the_lowres_cache_cannot_serve_another_slide_under_the_same_channel(
        app, tmp_path, monkeypatch):
    """`_slide_lowres` was keyed by channel NAME. Both slides have a CD3, and
    clearing the cache at the switch is not enough: a producer that finishes
    afterwards refills it."""
    page = _page_showing_a(tmp_path, shape=(64, 128))
    try:
        a_token = page._dataset_token()
        page.current_channel = "CD3"
        first = page._slide_lowres_array("CD3", blocking=False)
        assert first is not None
        assert page._slide_lowres["CD3"][0] == a_token

        # A late producer refills the cache with A's array after the switch.
        _switch_to_b(page, tmp_path, monkeypatch, value=99.0, shape=(128, 64),
                     load_overview=False)
        page._slide_lowres["CD3"] = (a_token, first)

        served = page._slide_lowres_array("CD3", blocking=False)
        assert served is None or float(np.asarray(served).min()) != \
            pytest.approx(11.0), "the previous slide's array was served"
        assert "CD3" not in page._slide_lowres or \
            page._slide_lowres["CD3"][0] == page._dataset_token()
    finally:
        page.close()


# ── the identity log ────────────────────────────────────────────────────

def test_the_identity_log_is_silent_unless_it_is_asked_for(app, tmp_path,
                                                           monkeypatch):
    from block01.utils import dataset_trace

    path = tmp_path / "identity.log"
    page = _page_showing_a(tmp_path)
    _show_a_everywhere(page)
    try:
        monkeypatch.delenv(dataset_trace.DEBUG_ENV, raising=False)
        monkeypatch.delenv(dataset_trace.LOG_ENV, raising=False)
        _switch_to_b(page, tmp_path, monkeypatch, load_overview=False)
        assert not path.exists()

        monkeypatch.setenv(dataset_trace.LOG_ENV, str(path))
        _switch_to_b(page, tmp_path, monkeypatch, load_overview=False)
        lines = path.read_text(encoding="utf-8").splitlines()
        events = {line.split()[1] for line in lines}
        assert "page.bind" in events, lines
        assert "panel.bind" in events, lines
        assert "panel.forget" in events, lines
        assert all("token=" in line for line in lines
                   if line.split()[1].startswith(("panel.", "page."))), lines
    finally:
        page.close()
