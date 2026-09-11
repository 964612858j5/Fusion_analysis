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

import numpy as np
import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

pytest.importorskip("PyQt5")

from PyQt5 import QtWidgets  # noqa: E402

from block01.ui.step0 import step0_page as sp  # noqa: E402


@pytest.fixture(scope="module")
def app():
    return QtWidgets.QApplication.instance() or QtWidgets.QApplication([])


@pytest.fixture(autouse=True)
def _collect_before_the_flush():
    yield
    gc.collect()


class _Loader:
    """Two of these differ ONLY in the value their pixels carry."""

    _CHANNELS = ["DAPI", "CD3", "CD20"]
    shape = (64, 64)

    def __init__(self, value):
        self.value = float(value)
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
        return np.full((max(1, (y1 - y0) // ds), max(1, (x1 - x0) // ds)),
                       self.value, np.float32)


def _rgb(value):
    return np.full((16, 16, 3), value, np.uint8)


def _page_showing_a(tmp_path, value=11.0):
    """Dataset A loaded, with its picture on both Tissue Previews."""
    page = sp.Step0Page()
    page.loader = _Loader(value)
    page.ome_path = str(tmp_path / "A.tif")
    page.output_dir = str(tmp_path / "out")
    page.patches = [(0, 32, 0, 32)]
    page.current_patch_idx = 0
    page.nucleus_channel = "DAPI"
    page._rebuild_channel_list()
    page.current_channel = "CD3"
    page.overview.loader = page.loader
    page.overview.full_h, page.overview.full_w = 64, 64
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
                 load_overview=True):
    """The real Load entry, with only the loader constructor stubbed."""
    b = tmp_path / "B.tif"
    b.write_bytes(b"not-really-a-tiff")
    made = _Loader(value)

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


def test_a_preview_timer_armed_for_the_previous_slide_draws_nothing(
        app, tmp_path, monkeypatch):
    """The debounce timer is armed while A is current and fires after the
    switch. Whatever it renders, it may not put A back."""
    page = _page_showing_a(tmp_path)
    _show_a_everywhere(page)
    try:
        page._queue_tissue_preview()
        assert page._tissue_preview_timer.isActive()
        stale_token = page._dataset_token()
        _switch_to_b(page, tmp_path, monkeypatch, load_overview=False)

        # The timer's own slot, with the previous slide's picture in hand.
        monkeypatch.setattr(type(page), "_tissue_preview_rgb",
                            lambda self: _rgb(200))
        monkeypatch.setattr(type(page), "_dataset_token",
                            lambda self: stale_token)
        page._update_tissue_preview()

        for name, panel in _panels(page).items():
            assert _blank(panel), f"{name} was repainted with the old slide"
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
