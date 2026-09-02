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
    """Fake loader mimicking the corrected-store surface: read_region serves
    a corrected marker value (100+i) for channels in the store decisions and
    a raw marker (i+1) otherwise — same contract as OMETIFFLoader."""

    _CHANNELS = ["DAPI", "CD3", "CD20"]

    def __init__(self):
        self._corrected_zarr_path = None
        self._corrected_decisions = {}

    def channel_names(self):
        return list(self._CHANNELS)

    def set_corrected_zarr_store(self, zarr_path, decisions):
        self._corrected_zarr_path = zarr_path
        self._corrected_decisions = {
            str(ch): str(m).strip().lower()
            for ch, m in dict(decisions or {}).items()
            if str(m).strip().lower() in {"tophat", "cucim"}
        } if zarr_path else {}

    def read_region(self, ch, y0, y1, x0, x1, downsample=1,
                    correction_config=None, normalize=True):
        i = self._CHANNELS.index(ch)
        val = 100.0 + i if ch in self._corrected_decisions else float(i + 1)
        return np.full((y1 - y0, x1 - x0), val, np.float32)


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


def _saved_via_store(page, decisions):
    """Go through the real reconcile entry point (store swap + cache diff)."""
    page._apply_corrected_store("/fake/corrected_channels.zarr", decisions)


def test_withdrawn_correction_reverts_to_raw(app):
    """corrected -> Original: the cache must stop serving old corrected."""
    page = _page(app)
    pr = page._preview_provider
    _saved_via_store(page, {"CD3": "tophat"})
    assert float(pr.get_pixels("CD3", "corrected")[0, 0]) == 101.0
    got = []
    pr.stage_invalidated.connect(lambda ch, st: got.append(ch))
    # re-save with CD3 switched to Original -> store no longer lists it
    _saved_via_store(page, {"CD3": "original"})
    assert float(pr.get_pixels("CD3", "corrected")[0, 0]) == 2.0   # raw again
    assert pr.describe("CD3")["served_corrected_stage"] == "raw_unsaved"
    assert "CD3" in got


def test_incremental_save_drops_removed_channel(app):
    """corrected {A,B} -> {A}: B reverts to raw, A stays corrected."""
    page = _page(app)
    pr = page._preview_provider
    _saved_via_store(page, {"CD3": "tophat", "CD20": "cucim"})
    assert float(pr.get_pixels("CD20", "corrected")[0, 0]) == 102.0
    _saved_via_store(page, {"CD3": "tophat"})
    assert float(pr.get_pixels("CD3", "corrected")[0, 0]) == 101.0
    assert float(pr.get_pixels("CD20", "corrected")[0, 0]) == 3.0  # raw again
    assert pr.describe("CD20")["served_corrected_stage"] == "raw_unsaved"


def test_clear_all_corrections_reverts_everything(app):
    """all corrected -> none (store cleared): every channel back to raw."""
    page = _page(app)
    pr = page._preview_provider
    _saved_via_store(page, {"CD3": "tophat", "CD20": "cucim"})
    got = []
    pr.stage_invalidated.connect(lambda ch, st: got.append(ch))
    page._apply_corrected_store(None, {})
    assert float(pr.get_pixels("CD3", "corrected")[0, 0]) == 2.0
    assert float(pr.get_pixels("CD20", "corrected")[0, 0]) == 3.0
    assert set(got) == {"CD3", "CD20"}
    for ch in ("CD3", "CD20"):
        assert pr.describe(ch)["served_corrected_stage"] == "raw_unsaved"


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


# ── effective correction parameters (the render-ready pair) ──────────────────
#
# `params` says whether a channel was tuned; the `effective_*` fields say what
# a preview would actually use. A consumer that renders needs the second and
# must never have to re-derive the fallback rule itself.

def test_effective_params_are_the_explicit_per_channel_values(app):
    page = _page(app)
    pr = page.preview_source_provider
    page._channel_params["CD3"] = {"tophat_radius": 33, "cucim_sigma": 44}

    correction = pr.describe("CD3")["correction"]

    assert correction["effective_tophat_radius"] == 33
    assert correction["effective_cucim_sigma"] == 44
    # The raw field still reports the override itself.
    assert correction["params"] == {"tophat_radius": 33, "cucim_sigma": 44}


def test_effective_params_fall_back_exactly_as_the_page_resolves_them(app):
    """No second copy of the fallback rule: the value must equal what
    `_resolve_channel_params` returns, whatever that is."""
    page = _page(app)
    pr = page.preview_source_provider
    assert "CD20" not in page._channel_params

    correction = pr.describe("CD20")["correction"]
    expected_tr, expected_cs = page._resolve_channel_params("CD20")

    assert correction["effective_tophat_radius"] == expected_tr
    assert correction["effective_cucim_sigma"] == expected_cs
    assert correction["effective_tophat_radius"] is not None
    assert correction["effective_cucim_sigma"] is not None
    # And the untouched field keeps its old meaning: not tuned -> None.
    assert correction["params"] == {"tophat_radius": None, "cucim_sigma": None}


def test_effective_params_come_from_the_pages_resolver_not_a_copy(app):
    """The rule must be REUSED, not duplicated.

    Comparing against the current default constants cannot show that: a
    reimplementation with the same literals would agree. So the resolver is
    replaced with one that answers differently, and `describe` has to
    follow it.
    """
    page = _page(app)
    pr = page.preview_source_provider
    page._resolve_channel_params = lambda ch: (4242, 8484)

    correction = pr.describe("CD20")["correction"]

    assert correction["effective_tophat_radius"] == 4242
    assert correction["effective_cucim_sigma"] == 8484


def test_a_partial_override_still_resolves_both_effective_values(app):
    """A channel with only one of the two set must still yield a usable
    pair -- the missing half comes from the page's own fallback."""
    page = _page(app)
    pr = page.preview_source_provider
    page._channel_params["CD3"] = {"tophat_radius": 7}

    correction = pr.describe("CD3")["correction"]
    expected_tr, expected_cs = page._resolve_channel_params("CD3")

    assert correction["effective_tophat_radius"] == expected_tr == 7
    assert correction["effective_cucim_sigma"] == expected_cs
    assert correction["effective_cucim_sigma"] is not None
    assert correction["params"] == {"tophat_radius": 7, "cucim_sigma": None}


def test_method_fields_pass_both_through_untouched(app):
    """`both` is a real value in this page's model (the Process path
    computes tophat AND cucim). The provider reports it verbatim; deciding
    what it means is the caller's job, not this seam's."""
    page = _page(app)
    pr = page.preview_source_provider
    page._channel_decisions["CD3"] = "both"
    page._channel_methods["CD3"] = "both"

    correction = pr.describe("CD3")["correction"]

    assert correction["assigned_method"] == "both"
    assert correction["preview_method"] == "both"


def test_preview_source_provider_property_is_the_one_instance(app):
    page = _page(app)

    assert page.preview_source_provider is page._preview_provider
    assert page.preview_source_provider is page.preview_source_provider

    with pytest.raises(AttributeError):
        page.preview_source_provider = object()      # read-only, no setter

    # Reading it does not construct anything.
    assert page.preview_source_provider is page._preview_provider


def test_preview_source_provider_property_answers_none_if_unbuilt(app):
    """On a live page the provider always exists: `__init__` builds the
    conditioning tab unconditionally, and that is where it is created. (A
    never-`__init__`'d Step0Page cannot even be inspected -- PyQt raises
    "super-class __init__() ... was never called" on attribute access -- so
    that state is not reachable to test.) The guard is for a page whose
    construction failed partway; simulate exactly that.
    """
    page = _page(app)
    assert page.preview_source_provider is not None

    del page._preview_provider
    assert page.preview_source_provider is None, (
        "the property must answer rather than raise on a half-built page")
