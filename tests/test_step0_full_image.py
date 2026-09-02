"""Full-image drill-down inside Background Correction.

Its own module, not appended to test_step0_background_correction_tab.py:
that file already constructs 32 `Step0Page` instances, and one more was
enough to make a whole-file run segfault inside a PRE-EXISTING widget
test's `processEvents` (the known pyqtgraph/offscreen fragility recorded as
INCONCLUSIVE). Sharing a single page between the two sections reduced but
did not remove it -- 1 of 3 runs still crashed -- so these live here and
the older file is back to its previously stable content.

Everything here drives the page's real methods with a recording stand-in
for the Explore tab; no viewer stack and no slide are involved.
"""

import os

import numpy as np
import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

pytest.importorskip("PyQt5")

from block01.ui.step0.step0_page import (  # noqa: E402
    PREVIEW_PAGE_COMPARE,
    PREVIEW_PAGE_FULL_IMAGE,
)

from test_step0_background_correction_tab import (  # noqa: E402
    _GpuPathLoader,
    app,            # noqa: F401  (pytest fixture)
)


@pytest.fixture(scope="module")
def full_image_page(app):
    """One page for this whole module."""
    from block01.ui.step0.step0_page import Step0Page

    page = Step0Page()
    page.loader = _GpuPathLoader()
    page.patches = [(0, 32, 0, 32)]
    page.current_patch_idx = 0
    page.nucleus_channel = "DAPI"
    page._rebuild_channel_list()
    page._preload_cache = {0: {ch: np.zeros((32, 32), np.float32)
                               for ch in ("DAPI", "CD3", "CD20")}}
    page.current_channel = "CD3"
    page._update_full_image_buttons()
    return page


class _RecordingExploreTab:
    """Stands in for Step0ExploreTab: records what it is asked to show."""

    def __init__(self):
        self.calls = []
        self.viewports = []
        self.stack = None
        self.build_attempts = 0
        self.released = []

    def show_source(self, channel, method, params=(), *, viewport_l0=None):
        self.calls.append((channel, method, tuple(params)))
        # Recorded separately: most cases here are about the selection, and
        # the viewport is asserted in test_step0_full_image_viewport.py.
        self.viewports.append(viewport_l0)
        self.build_attempts += 1
        return True

    def release_for_production(self, reason):
        self.released.append(reason)

    def teardown(self, **_kw):
        pass

    def set_dataset(self, _path):
        pass


def test_the_top_level_tabs_are_only_correction_and_remap(full_image_page):
    page = full_image_page
    titles = [page._step0_tabs.tabText(i)
              for i in range(page._step0_tabs.count())]
    assert titles == ["Background Correction", "Channel Remap"], titles
    assert not hasattr(page, "_explore_tab_index")


def test_the_viewer_is_created_once_on_first_use_and_lives_in_the_stack(
        full_image_page):
    """One instance, created on first use, owned by the full-image page.

    Not built with the page: a user who never opens the full image should
    not pay for the widget, and the dataset it needs is already on
    `page.ome_path` -- so deferring it needs no extra state.
    """
    from block01.ui.step0.step0_explore_tab import Step0ExploreTab

    page = full_image_page
    assert page._preview_stack.count() == 2

    if page._explore_tab is None:
        assert page.findChildren(Step0ExploreTab) == [], (
            "the viewer exists before anything asked for it")

    first = page._ensure_explore_tab()
    again = page._ensure_explore_tab()

    assert first is again, "a second viewer was created"
    found = page.findChildren(Step0ExploreTab)
    assert found == [first], f"expected exactly one, found {len(found)}"
    full_page = page._preview_stack.widget(PREVIEW_PAGE_FULL_IMAGE)
    assert first in full_page.findChildren(Step0ExploreTab), (
        "the viewer is not inside the full-image page")


def test_the_viewer_catches_up_on_a_dataset_loaded_before_it_existed(
        app, tmp_path):
    """`set_dataset` can run before the viewer is created; the value it
    would have been given is already on the page."""
    from block01.ui.step0.step0_page import Step0Page

    page = Step0Page()
    page.ome_path = str(tmp_path / "slide.ome.tif")

    tab = page._ensure_explore_tab()

    assert tab.dataset_path == page.ome_path


@pytest.mark.parametrize(
    ("source", "expected_method", "expected_key"),
    [("original", None, None),
     ("tophat", "tophat", "effective_tophat_radius"),
     ("cucim", "cucim", "effective_cucim_sigma")],
)
def test_each_button_maps_to_its_result_with_provider_parameters(
        full_image_page, monkeypatch, source, expected_method, expected_key):
    page = full_image_page
    tab = _RecordingExploreTab()
    monkeypatch.setattr(page, "_explore_tab", tab)

    # The parameters must come from the PROVIDER's effective fields. Proved
    # by making the provider answer something the page's own per-channel
    # store does not contain: if the page read `_channel_params` instead,
    # the sentinel could not appear. (Asserting the store is never touched
    # would be wrong -- `describe()` itself reads it, which is exactly the
    # seam's job.)
    page._channel_params["CD3"] = {"tophat_radius": 3, "cucim_sigma": 4}
    real_describe = page.preview_source_provider.describe

    def sentinel_describe(channel):
        info = real_describe(channel)
        info["correction"]["effective_tophat_radius"] = 777
        info["correction"]["effective_cucim_sigma"] = 888
        return info

    monkeypatch.setattr(page.preview_source_provider, "describe",
                        sentinel_describe)
    try:
        page._full_image_buttons[source].click()
    finally:
        page._channel_params.pop("CD3", None)

    expected_params = ()
    if expected_key == "effective_tophat_radius":
        expected_params = (777,)
    elif expected_key == "effective_cucim_sigma":
        expected_params = (888,)
    assert tab.calls == [("CD3", expected_method, expected_params)]
    assert page._preview_stack.currentIndex() == PREVIEW_PAGE_FULL_IMAGE
    assert page._full_image_source == source
    page._return_to_compare()


def test_the_source_label_names_the_method_and_its_parameter(full_image_page,
                                                             monkeypatch):
    page = full_image_page
    monkeypatch.setattr(page, "_explore_tab", _RecordingExploreTab())

    page._enter_full_image("tophat")
    text = page._full_source_lbl.text()
    radius = page.preview_source_provider.describe(
        "CD3")["correction"]["effective_tophat_radius"]

    assert "CD3" in text and "Top-hat" in text and str(radius) in text
    page._return_to_compare()


def test_returning_to_compare_changes_nothing_but_the_page(full_image_page,
                                                           monkeypatch):
    """No teardown, no set_selection, no recompute -- and the compare
    ViewBoxes keep their own ranges while hidden, which is why nothing is
    saved and restored."""
    page = full_image_page
    tab = _RecordingExploreTab()
    monkeypatch.setattr(page, "_explore_tab", tab)

    ranges_before = [vb.viewRange() for vb in page._preview_vbs]
    page._enter_full_image("cucim")
    calls_after_enter = list(tab.calls)

    page._return_to_compare()

    assert page._preview_stack.currentIndex() == PREVIEW_PAGE_COMPARE
    assert tab.calls == calls_after_enter, "returning asked the viewer again"
    assert [vb.viewRange() for vb in page._preview_vbs] == ranges_before
    assert page._full_image_source == "cucim", "the choice is remembered"


def test_reopening_reads_the_parameters_again(full_image_page, monkeypatch):
    page = full_image_page
    tab = _RecordingExploreTab()
    monkeypatch.setattr(page, "_explore_tab", tab)
    page._enter_full_image("tophat")

    page._channel_params["CD3"] = {"tophat_radius": 41, "cucim_sigma": 7}
    page._reopen_full_image()

    assert tab.calls[-1] == ("CD3", "tophat", (41,)), (
        "reopening must re-read the effective parameters, not replay the old")
    page._return_to_compare()
    page._channel_params.pop("CD3", None)


def test_a_busy_run_blocks_reopening_without_touching_the_viewer(
        full_image_page, monkeypatch):
    page = full_image_page
    tab = _RecordingExploreTab()
    monkeypatch.setattr(page, "_explore_tab", tab)
    monkeypatch.setattr(page, "production_correction_busy",
                        lambda: "patch background correction")

    page._enter_full_image("tophat")

    assert tab.calls == [], "the viewer was asked to build during a run"
    assert tab.build_attempts == 0
    # The label still says what WOULD be shown, so the page is not blank.
    assert "Top-hat" in page._full_source_lbl.text()
    page._return_to_compare()


def test_a_channel_change_updates_the_full_image_only_when_visible(
        full_image_page, monkeypatch):
    page = full_image_page
    tab = _RecordingExploreTab()
    monkeypatch.setattr(page, "_explore_tab", tab)

    # Hidden: nothing happens.
    page._return_to_compare()
    page.current_channel = "CD20"
    page._sync_full_image_to_channel()
    assert tab.calls == []

    # Visible: it follows, keeping the chosen source.
    page._enter_full_image("cucim")
    tab.calls.clear()
    page.current_channel = "CD3"
    page._sync_full_image_to_channel()
    sigma = page.preview_source_provider.describe(
        "CD3")["correction"]["effective_cucim_sigma"]
    assert tab.calls == [("CD3", "cucim", (sigma,))]
    page._return_to_compare()


def test_a_channel_change_during_a_run_does_not_rebuild(full_image_page,
                                                        monkeypatch):
    """A channel change can itself start an on-demand run, whose gate has
    just released the viewer. Rebuilding here would undo that release."""
    page = full_image_page
    tab = _RecordingExploreTab()
    monkeypatch.setattr(page, "_explore_tab", tab)
    page._enter_full_image("tophat")
    tab.calls.clear()
    monkeypatch.setattr(page, "production_correction_busy",
                        lambda: "on-demand background correction")

    page.current_channel = "CD20"
    page._sync_full_image_to_channel()

    assert tab.calls == []
    page._return_to_compare()


def test_the_nucleus_channel_shows_original_without_losing_the_choice(
        full_image_page, monkeypatch):
    page = full_image_page
    tab = _RecordingExploreTab()
    monkeypatch.setattr(page, "_explore_tab", tab)
    page._enter_full_image("tophat")
    tab.calls.clear()

    page.current_channel = "DAPI"          # the nucleus channel
    page._update_full_image_buttons()
    page._sync_full_image_to_channel()

    assert tab.calls == [("DAPI", None, ())], (
        "the nucleus channel is excluded from correction; it must show "
        "Original")
    assert page._full_image_source == "tophat", (
        "the user's choice must survive the detour")
    assert page._full_image_buttons["tophat"].isEnabled() is False
    assert page._full_image_buttons["cucim"].isEnabled() is False
    assert page._full_image_buttons["original"].isEnabled() is True

    tab.calls.clear()
    page.current_channel = "CD3"
    page._update_full_image_buttons()
    page._sync_full_image_to_channel()
    radius = page.preview_source_provider.describe(
        "CD3")["correction"]["effective_tophat_radius"]
    assert tab.calls == [("CD3", "tophat", (radius,))], (
        "the chosen source must come back on an ordinary channel")
    assert page._full_image_buttons["tophat"].isEnabled() is True
    page._return_to_compare()


def test_fit_whole_slide_only_moves_the_full_image_view(full_image_page,
                                                        monkeypatch):
    page = full_image_page
    ranges_before = [vb.viewRange() for vb in page._preview_vbs]

    class _ViewBox:
        def __init__(self):
            self.calls = []

        def setRange(self, **kwargs):
            self.calls.append(sorted(kwargs))

    class _Stack:
        def __init__(self):
            self.view = type("V", (), {"view_box": _ViewBox()})()
            self.provider = type("P", (), {
                "level_shape": staticmethod(lambda _l: (4096, 2048))})()

    tab = _RecordingExploreTab()
    tab.stack = _Stack()
    monkeypatch.setattr(page, "_explore_tab", tab)
    # Fitting is done FROM the full-image page, and must leave you there --
    # it is not "back to compare" under another name.
    page._enter_full_image("tophat")

    page._fit_full_image()

    assert tab.stack.view.view_box.calls == [["padding", "xRange", "yRange"]]
    assert [vb.viewRange() for vb in page._preview_vbs] == ranges_before
    assert page._preview_stack.currentIndex() == PREVIEW_PAGE_FULL_IMAGE, (
        "fitting must not change which page is shown")
    page._return_to_compare()


def test_fit_whole_slide_without_a_stack_is_a_no_op(full_image_page,
                                                    monkeypatch):
    page = full_image_page
    tab = _RecordingExploreTab()          # stack is None
    monkeypatch.setattr(page, "_explore_tab", tab)
    page._fit_full_image()                 # must not raise
