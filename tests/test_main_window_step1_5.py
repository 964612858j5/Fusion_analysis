"""Step 1.5 is reachable from the main GUI workflow (2.1c-b GUI entrypoint).

Verifies the main window not only constructs but actually EXPOSES the Step 1.5
Background Correction + Channel Conditioning / Remap page: it is in the page
stack, has a step-bar action, and the action navigates to it and feeds context.
No remap config is created here — only the navigation/exposure is exercised.

Qt tests need an offscreen platform (set by conftest/env: QT_QPA_PLATFORM=offscreen).
"""

import os

import numpy as np
import pytest

pytest.importorskip("PyQt5")


@pytest.fixture(scope="module")
def app():
    from PyQt5 import QtWidgets
    a = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    return a


class _FakeLoader:
    """Minimal OMETIFFLoader stand-in with the attrs Step1.5 reads."""

    def __init__(self, names=("DAPI", "CD68", "CK19"), filepath="/tmp/fake.ome.tif",
                 shape=(64, 64)):
        self._names = list(names)
        self.filepath = filepath
        self.shape = shape

    def channel_names(self):
        return list(self._names)

    def read_region(self, ch, y0, y1, x0, x1, downsample=1,
                    correction_config=None, normalize=True):
        a = np.zeros((y1 - y0, x1 - x0), dtype=np.float32)
        return a if normalize else a


@pytest.fixture(scope="module")
def window(app):
    from block01.ui.main_window import MainWindow
    w = MainWindow()
    yield w
    w.close()


def test_step1_5_page_is_in_the_stack(window):
    from block01.ui.step1_5_bg_page import Step15BackgroundCorrectionPage
    assert hasattr(window, "_step1_5")
    assert isinstance(window._step1_5, Step15BackgroundCorrectionPage)
    pages = [window._stack.widget(i) for i in range(window._stack.count())]
    assert window._step1_5 in pages


def test_step1_5_page_has_conditioning_tab(window):
    tabs = [window._step1_5._s15_tabs.tabText(i)
            for i in range(window._step1_5._s15_tabs.count())]
    assert "Background Correction" in tabs
    assert any("Channel Conditioning" in t for t in tabs)


def test_step_bar_has_step1_5_action(window):
    assert hasattr(window, "_btn_step1_5")
    assert "Step 1.5" in window._btn_step1_5.text()
    assert hasattr(window, "_go_to_step1_5")


def test_go_to_step1_5_without_loader_does_not_crash(window, monkeypatch):
    # No loader -> informational message, no navigation, no crash.
    monkeypatch.setattr(
        "block01.ui.main_window.QMessageBox.information", lambda *a, **k: None)
    window.loader = None
    window._go_to_step1_5()  # must not raise


def test_go_to_step1_5_navigates_and_feeds_context(window, tmp_path):
    roi_dir = str(tmp_path / "roi")
    os.makedirs(roi_dir, exist_ok=True)
    window.loader = _FakeLoader()
    window.step0_output = {"roi_dir": roi_dir,
                           "patches": [(0, 32, 0, 32), (0, 32, 32, 64)]}
    window._go_to_step1_5()
    # navigated to the Step1.5 page
    assert window._stack.currentWidget() is window._step1_5
    # fed from Step1.5's own context: output dir is <ROI>/step1_5 so the page saves
    # configs under <ROI>/step1_5/channel_remap_configs/ (unchanged save path)
    assert window._step1_5.output_dir == os.path.join(roi_dir, "step1_5")
    assert len(window._step1_5.patches) == 2
    expected_cfg_dir = os.path.join(roi_dir, "step1_5", "channel_remap_configs")
    # the save helper targets exactly this dir (we do NOT create a config here)
    assert os.path.join(window._step1_5.output_dir, "channel_remap_configs") == expected_cfg_dir
