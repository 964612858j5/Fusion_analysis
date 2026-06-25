"""step0-remove-auto-advance: Save is save-only (no auto-jump to Step1) and the
redundant Next button is gone.

#9: _on_step0_complete still performs the Step0->Step1 DATA handoff (state Step1
reads) but no longer navigates. #11: the Next button is removed; navigation is via
the step names. Qt offscreen.
"""

import pytest

pytest.importorskip("PyQt5")


@pytest.fixture(scope="module")
def app():
    from PyQt5 import QtWidgets
    a = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    return a


class _FakeLoader:
    def __init__(self):
        self.filepath = "/x.ome.tif"
        self.shape = (100, 100)
        self.ch_map = {"DAPI": 0, "CD68": 1}
        self._cfg = None
        self._zarr = None

    def channel_names(self):
        return ["DAPI", "CD68"]

    def set_correction_config(self, c):
        self._cfg = c

    def set_corrected_zarr_store(self, path, decisions):
        self._zarr = (path, decisions)


def _payload():
    return {
        "loader": _FakeLoader(),
        "patches": [(0, 50, 0, 50)],
        "rois": [{"name": "R1"}],
        "correction_config": {"method_params": {}},
        "corrected_zarr_path": "/x/corrected_channels.zarr",
        "corrected_decisions": {"CD68": "tophat"},
        "ome_tiff_path": "/x.ome.tif",
        "step1_dir": "/tmp/s1", "step2_dir": "/tmp/s2", "output_dir": "/tmp",
        "panel_groups": {}, "panel_nucleus": None,
    }


@pytest.fixture()
def window(app, monkeypatch):
    from block01.ui.main_window import MainWindow
    w = MainWindow()
    # isolate the navigation/handoff-state logic from the deep Step1 ROI loader
    # (its own concern; unchanged here) so the test targets save-vs-jump.
    monkeypatch.setattr(type(w), "_load_step0_roi_result",
                        lambda self, *a, **k: None)
    w._stack.setCurrentIndex(0)
    w._current_step = 0
    yield w
    w.close()


# ── #9: SAVE-ONLY — handoff state set, but NO auto-jump ───────────────────────
def test_step0_complete_sets_handoff_state(window):
    window._on_step0_complete(_payload())
    # the Step0->Step1 data handoff state Step1 reads is still set
    assert window.step0_done is True
    assert window.loader is not None
    assert window._corrected_zarr_path == "/x/corrected_channels.zarr"
    assert window._corrected_decisions == {"CD68": "tophat"}
    # the loader received the correction config + corrected store (handoff)
    assert window.loader._cfg == {"method_params": {}}
    assert window.loader._zarr == ("/x/corrected_channels.zarr", {"CD68": "tophat"})


def test_step0_complete_does_not_auto_jump(window):
    assert window._stack.currentIndex() == 0          # in Step0 before
    window._on_step0_complete(_payload())
    assert window._stack.currentIndex() == 0          # STAYS in Step0 (no jump)
    assert window._current_step == 0


def test_save_button_is_save_only_label(window):
    txt = window._step0._btn_continue.text()
    assert "Continue" not in txt
    assert "Step 1" not in txt and "Step1" not in txt
    assert "Save" in txt


# ── #11: Next button removed; step-name navigation still works ───────────────
def test_next_button_removed(window):
    from PyQt5 import QtWidgets
    # not shown in the top nav bar
    assert window._btn_next.isVisible() is False
    # the handler it served is gone
    assert not hasattr(window, "_go_next_step")


def test_step_name_navigation_still_works(window):
    # after save (no jump), the user navigates by the step path; _go_to_stepN
    # (what the step-name clicks call) still switches the view.
    window._on_step0_complete(_payload())
    window._go_to_step1()
    assert window._stack.currentIndex() == 1
    window._go_to_step0()
    assert window._stack.currentIndex() == 0
