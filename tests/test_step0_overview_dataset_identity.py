"""A committed dataset switch takes the previous slide's tissue off the screen.

The thumbnail lives in two places — the page's own overview panel and the one
inside the Tissue Preview popup — and each holds two pictures: the host-pushed
channel image and the panel's own DAPI overview. The switch cleared the image
ITEM of the first panel only, so the very next repaint drew slide A's channel
image again, and the popup was never touched at all. The read in flight for A
also had nothing to say which slide it was for, so it could land in a panel
already bound to B.

A panel that has been bound to a dataset now REFUSES a picture or a read
result that cannot name one, so the installs below name it -- which is what a
production caller does too.

Own module: page-heavy PyQt suites crash pyqtgraph offscreen when combined.
"""

import gc
import os

import numpy as np
import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

pytest.importorskip("PyQt5")

from block01.ui.step0 import step0_page as sp  # noqa: E402

from test_step0_background_correction_tab import (  # noqa: E402
    _GpuPathLoader,
    app,            # noqa: F401  (pytest fixture)
)


@pytest.fixture(autouse=True)
def _collect_before_the_flush():
    yield
    gc.collect()


def _rgb(value):
    return np.full((16, 16, 3), value, np.uint8)


def _page_showing_a(tmp_path, loader=None):
    page = sp.Step0Page()
    page.loader = loader if loader is not None else _GpuPathLoader()
    page.ome_path = str(tmp_path / "A.tif")
    page.patches = [(0, 32, 0, 32)]
    page.current_patch_idx = 0
    page.nucleus_channel = "DAPI"
    page._rebuild_channel_list()
    page.current_channel = "CD3"
    page.overview.loader = page.loader
    page.overview.full_h, page.overview.full_w = 64, 64
    page.overview._on_overview_loaded(np.zeros((16, 16), np.float32))
    page.overview.set_channel_image(_rgb(200))      # slide A on screen
    return page


def _switch_to_b(page, tmp_path, monkeypatch, new_loader=None, raises=None):
    b = tmp_path / "B.tif"
    b.write_bytes(b"not-really-a-tiff")
    made = new_loader if new_loader is not None else _GpuPathLoader()

    def _ctor(*_a, **_kw):
        if raises is not None:
            raise raises
        return made

    monkeypatch.setattr(sp, "OMETIFFLoader", _ctor)
    for name in ("warning", "critical", "information"):
        monkeypatch.setattr(sp.QMessageBox, name, lambda *a, **k: None)
    monkeypatch.setattr(type(page), "_auto_open_tissue_navigator", lambda self: None)
    # The read itself is not what these tests are about; what it would deliver
    # is driven explicitly below.
    monkeypatch.setattr(type(page.overview), "_load_overview", lambda self: None)
    page._ome_path_edit.setText(str(b))
    page._out_path_edit.setText(str(tmp_path / "out"))
    page._panel_csv_edit.setText("")
    page._reload_from_paths()
    return made


def test_the_page_panel_loses_the_previous_slides_pixels(app, tmp_path, monkeypatch):
    page = _page_showing_a(tmp_path)
    try:
        assert page.overview._channel_rgb is not None
        _switch_to_b(page, tmp_path, monkeypatch)

        assert page.overview._channel_rgb is None
        assert page.overview._overview_arr is None
        assert page.overview.img_item.image is None
    finally:
        page.close()


def test_the_popup_panel_loses_them_too(app, tmp_path, monkeypatch):
    page = _page_showing_a(tmp_path)
    try:
        page.show_tissue_navigator()
        popup = page._tissue_navigator_popup
        token = popup.overview.dataset_token()
        popup.overview._on_overview_loaded(
            np.zeros((16, 16), np.float32), popup.overview._ov_gen,
            popup.overview.loader, token)
        popup.overview.set_channel_image(_rgb(180), token)
        assert popup.overview.img_item.image is not None

        _switch_to_b(page, tmp_path, monkeypatch)

        assert popup.overview._channel_rgb is None
        assert popup.overview._overview_arr is None
        assert popup.overview.img_item.image is None
        assert page._tissue_navigator_popup is popup      # same window
    finally:
        page.close()


def test_the_new_slides_picture_is_the_only_one_that_arrives(app, tmp_path, monkeypatch):
    page = _page_showing_a(tmp_path)
    try:
        new_loader = _GpuPathLoader()
        _switch_to_b(page, tmp_path, monkeypatch, new_loader=new_loader)
        assert page.overview.img_item.image is None

        panel = page.overview
        panel._on_overview_loaded(np.full((16, 16), 7.0, np.float32),
                                  panel._ov_gen, panel.loader,
                                  panel.dataset_token())
        shown = panel.img_item.image

        assert shown is not None
        assert float(np.asarray(shown).max()) == pytest.approx(7.0)
    finally:
        page.close()


def test_a_late_read_from_the_previous_slide_is_dropped(app, tmp_path, monkeypatch):
    """A's overview read finishes after the switch. Its pixels are A's."""
    page = _page_showing_a(tmp_path)
    try:
        old_loader = page.loader
        stale_gen = getattr(page.overview, "_ov_gen", 0)
        _switch_to_b(page, tmp_path, monkeypatch)

        page.overview._on_overview_loaded(
            np.full((16, 16), 5.0, np.float32), stale_gen, old_loader)

        assert page.overview._overview_arr is None
        assert page.overview.img_item.image is None
    finally:
        page.close()


def test_a_read_for_another_loader_is_dropped(app, tmp_path, monkeypatch):
    page = _page_showing_a(tmp_path)
    try:
        old_loader = page.loader
        _switch_to_b(page, tmp_path, monkeypatch)
        current_gen = getattr(page.overview, "_ov_gen", 0)

        page.overview._on_overview_loaded(
            np.full((16, 16), 5.0, np.float32), current_gen, old_loader)

        assert page.overview._overview_arr is None
    finally:
        page.close()


def test_a_failed_load_leaves_the_current_slide_on_screen(app, tmp_path, monkeypatch):
    """Nothing was committed, so nothing may be taken away."""
    page = _page_showing_a(tmp_path)
    try:
        before = np.asarray(page.overview.img_item.image).copy()

        _switch_to_b(page, tmp_path, monkeypatch, raises=RuntimeError("bad file"))

        assert page.overview._channel_rgb is not None
        assert np.array_equal(np.asarray(page.overview.img_item.image), before)
    finally:
        page.close()
