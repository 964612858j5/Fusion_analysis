"""v15: Step0PreviewSourceProvider — the SAVE-boundary contract (revised).

Background correction and remap stay separate: remap consumes only the saved
corrected artifact (loader-served after Save), never an unsaved in-memory
preview. Unsaved channels are served raw and honestly labeled. Mutual state
stays visible via describe(); Save is the only corrected-stage invalidation.

Qt tests need an offscreen platform (env: QT_QPA_PLATFORM=offscreen).
"""

import numpy as np
import pytest

pytest.importorskip("PyQt5")


@pytest.fixture(scope="module")
def app():
    from PyQt5 import QtWidgets
    return QtWidgets.QApplication.instance() or QtWidgets.QApplication([])


class _Loader:
    """Fake loader with the corrected-store surface the provider reads."""
    _corrected_zarr_path = None
    _corrected_decisions = {}

    def channel_names(self):
        return ["DAPI", "CD3", "CD20"]


def _page(app):
    from block01.ui.step0.step0_page import Step0Page
    p = Step0Page()
    p.loader = _Loader()
    p.nucleus_channel = "DAPI"
    p.patches = [(0, 32, 0, 32)]
    p.current_patch_idx = 0
    p._rebuild_channel_list()
    # deterministic pixels through the preload cache (no disk IO); after a
    # real Save, _hotswap_corrected re-reads these as corrected pixels.
    p._preload_cache = {0: {ch: np.full((32, 32), i + 1, np.float32)
                            for i, ch in enumerate(["DAPI", "CD3", "CD20"])}}
    return p


def _mark_saved(page, channel, method, pixels):
    """Simulate the Save hand-off: loader now serves corrected pixels."""
    page.loader._corrected_zarr_path = "/fake/corrected_channels.zarr"
    page.loader._corrected_decisions = dict(page.loader._corrected_decisions,
                                            **{channel: method})
    page._preload_cache[0][channel] = pixels          # hot-swapped cache


def test_unsaved_channel_served_raw_and_labeled(app):
    page = _page(app)
    pr = page._preview_provider
    # a computed but UNSAVED preview must NOT leak into the corrected stage
    page._channel_methods["CD3"] = "tophat"
    page._preview_cache[("CD3", 0)] = {
        "tophat_raw": np.full((32, 32), 99.0, np.float32)}
    out = pr.get_pixels("CD3", "corrected")
    assert float(out[0, 0]) == 2.0                    # raw, not 99.0
    d = pr.describe("CD3")
    assert d["served_corrected_stage"] == "raw_unsaved"
    assert d["source_note"] == "raw — background correction not saved"
    assert d["correction"]["saved"] is False


def test_saved_channel_served_corrected_and_labeled(app):
    page = _page(app)
    pr = page._preview_provider
    corrected = np.full((32, 32), 7.5, np.float32)
    _mark_saved(page, "CD3", "tophat", corrected)
    assert float(pr.get_pixels("CD3", "corrected")[0, 0]) == 7.5
    d = pr.describe("CD3")
    assert d["served_corrected_stage"] == "corrected_saved"
    assert d["source_note"] == "corrected (tophat, saved)"
    assert d["correction"]["saved_method"] == "tophat"


def test_nucleus_note(app):
    page = _page(app)
    assert page._preview_provider.source_note("DAPI") == (
        "raw (nucleus — excluded from correction)")


def test_remapped_stage_runs_production_remap_on_served_stage(app):
    from block01.core.channel_remap import apply_channel_remap
    page = _page(app)
    pr = page._preview_provider
    corrected = np.linspace(0, 100, 32 * 32, dtype=np.float32).reshape(32, 32)
    _mark_saved(page, "CD3", "cucim", corrected)
    page._cond_workbench._params["CD3"] = {"min": 10.0, "max": 90.0,
                                           "gamma": 1.0}
    out = pr.get_pixels("CD3", "remapped")
    expect = apply_channel_remap(corrected, page._cond_workbench._params["CD3"])
    assert np.allclose(out, expect)


def test_preview_and_method_changes_do_not_invalidate(app):
    page = _page(app)
    got = []
    page._preview_provider.stage_invalidated.connect(
        lambda ch, st: got.append((ch, st)))
    page.current_channel = "CD3"
    page._on_batch_patch_done("CD20", 0, {"tophat_raw": np.ones((4, 4))})
    page._on_channel_method_changed("CD20", "cucim")
    assert got == []                                   # only Save invalidates


def test_describe_mutual_visibility(app):
    page = _page(app)
    pr = page._preview_provider
    page._channel_methods["CD3"] = "tophat"
    page._channel_decisions["CD3"] = "tophat"
    page._channel_params["CD3"] = {"tophat_radius": 33, "cucim_sigma": 44}
    page._cond_workbench._params["CD3"] = {"min": 5.0, "max": 200.0,
                                           "gamma": 1.5}
    page._cond_workbench._user_adjusted["CD3"] = True
    d = pr.describe("CD3")
    assert d["correction"]["assigned_method"] == "tophat"
    assert d["correction"]["params"]["tophat_radius"] == 33
    assert d["remap"] == {"min": 5.0, "max": 200.0, "gamma": 1.5,
                          "user_adjusted": True}


def test_region_reserved_for_viewport_foundation(app):
    page = _page(app)
    with pytest.raises(NotImplementedError):
        page._preview_provider.get_pixels("CD3", "corrected",
                                          region=(0, 10, 0, 10))


def test_save_invalidation_repulls_into_workbench(app):
    """End-to-end: Save lands -> provider announces -> remap view re-pulls
    the saved corrected pixels."""
    page = _page(app)
    wb = page._cond_workbench
    wb.set_channel_images({"CD3": page._preload_cache[0]["CD3"]})
    assert float(wb._raw["CD3"][0, 0]) == 2.0
    corrected = np.full((32, 32), 9.0, np.float32)
    _mark_saved(page, "CD3", "tophat", corrected)
    page._preview_provider.invalidate("CD3")           # what Save emits
    assert float(wb._raw["CD3"][0, 0]) == 9.0
