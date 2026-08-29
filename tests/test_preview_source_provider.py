"""v15 Workstream B step 1: Step0PreviewSourceProvider.

New contract (replaces the old save-then-remap flow): background correction
and channel remap interact LIVE on the same data — the corrected stage is
served from in-memory preview results with no Save, both sides' state is
mutually visible via describe(), and stage changes invalidate downstream
consumers.

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
    # deterministic raw pixels through the preload cache (no disk IO)
    p._preload_cache = {0: {ch: np.full((32, 32), i + 1, np.float32)
                            for i, ch in enumerate(["DAPI", "CD3", "CD20"])}}
    return p


def _provider(page):
    from block01.ui.step0.preview_source_provider import Step0PreviewSourceProvider
    return Step0PreviewSourceProvider(page)


def test_raw_stage_serves_preload_cache(app):
    page = _page(app)
    pr = _provider(page)
    arr = pr.get_pixels("CD3", "raw")
    assert float(arr[0, 0]) == 2.0


def test_corrected_falls_back_to_raw_without_preview(app):
    page = _page(app)
    pr = _provider(page)
    # no method assigned, no preview computed -> raw
    assert float(pr.get_pixels("CD3", "corrected")[0, 0]) == 2.0
    d = pr.describe("CD3")
    assert d["served_corrected_stage"] == "raw"
    assert d["correction"]["preview_computed"] is False


def test_corrected_serves_live_preview_without_save(app):
    page = _page(app)
    pr = _provider(page)
    page._channel_methods["CD3"] = "tophat"
    corrected = np.full((32, 32), 7.5, np.float32)
    page._preview_cache[("CD3", 0)] = {"tophat_raw": corrected}
    # NOT in _computed_channels, nothing saved — still served live
    out = pr.get_pixels("CD3", "corrected")
    assert float(out[0, 0]) == 7.5
    d = pr.describe("CD3")
    assert d["served_corrected_stage"] == "corrected_preview"
    assert d["correction"]["saved"] is False


def test_both_or_original_method_has_no_single_corrected_stage(app):
    page = _page(app)
    pr = _provider(page)
    page._preview_cache[("CD3", 0)] = {
        "tophat_raw": np.ones((32, 32), np.float32)}
    for m in ("both", "original"):
        page._channel_methods["CD3"] = m
        assert pr.active_method("CD3") is None
        assert float(pr.get_pixels("CD3", "corrected")[0, 0]) == 2.0  # raw


def test_remapped_stage_runs_production_remap_on_corrected(app):
    from block01.core.channel_remap import apply_channel_remap
    page = _page(app)
    pr = _provider(page)
    page._channel_methods["CD3"] = "cucim"
    corrected = np.linspace(0, 100, 32 * 32, dtype=np.float32).reshape(32, 32)
    page._preview_cache[("CD3", 0)] = {"cucim_raw": corrected}
    params = {"min": 10.0, "max": 90.0, "gamma": 1.0}
    page._cond_workbench._params["CD3"] = dict(params)
    out = pr.get_pixels("CD3", "remapped")
    expect = apply_channel_remap(corrected, page._cond_workbench._params["CD3"])
    assert np.allclose(out, expect)


def test_invalidation_signal_on_preview_and_method_change(app):
    page = _page(app)
    got = []
    page._preview_provider.stage_invalidated.connect(
        lambda ch, st: got.append((ch, st)))
    # a live preview payload lands for the current patch
    page._on_batch_patch_done("CD20", 0, {"tophat_raw": np.ones((4, 4))})
    # a method change
    page._on_channel_method_changed("CD20", "cucim")
    assert ("CD20", "corrected") in got
    assert got.count(("CD20", "corrected")) >= 2


def test_describe_mutual_visibility(app):
    page = _page(app)
    pr = _provider(page)
    page._channel_methods["CD3"] = "tophat"
    page._channel_decisions["CD3"] = "tophat"
    page._channel_params["CD3"] = {"tophat_radius": 33, "cucim_sigma": 44}
    page._cond_workbench._params["CD3"] = {
        "min": 5.0, "max": 200.0, "gamma": 1.5}
    page._cond_workbench._user_adjusted["CD3"] = True
    d = pr.describe("CD3")
    # correction state visible to the remap side
    assert d["correction"]["assigned_method"] == "tophat"
    assert d["correction"]["params"]["tophat_radius"] == 33
    # remap state visible to the correction side
    assert d["remap"] == {"min": 5.0, "max": 200.0, "gamma": 1.5,
                          "user_adjusted": True}


def test_region_reserved_for_viewport_foundation(app):
    page = _page(app)
    pr = _provider(page)
    with pytest.raises(NotImplementedError):
        pr.get_pixels("CD3", "raw", region=(0, 10, 0, 10))


def test_workbench_invalidate_channel_pixels_repulls_provider(app):
    """End-to-end: preview lands -> remap view drops cache and re-pulls the
    live corrected pixels (no Save)."""
    page = _page(app)
    wb = page._cond_workbench
    wb.set_channel_images({"CD3": page._preload_cache[0]["CD3"]})
    assert float(wb._raw["CD3"][0, 0]) == 2.0
    page.current_channel = "CD20"   # keep the BG display path out of this test
    page._channel_methods["CD3"] = "tophat"
    corrected = np.full((32, 32), 9.0, np.float32)
    page._on_batch_patch_done("CD3", 0, {"tophat_raw": corrected})
    assert float(wb._raw["CD3"][0, 0]) == 9.0   # live re-pull happened
