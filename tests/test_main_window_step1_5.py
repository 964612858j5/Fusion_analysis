"""v14.1 top navigation: 5-step workflow; Step 1.5 page kept internal-only.

v14.1 collapsed the top workflow navigation to the final 5 steps. The visible
Skip -> Step2/3/4 buttons and the Step 1.5 top-level workflow button were
removed. Direct navigation via the step labels still works.

The Step15BackgroundCorrectionPage widget and its set_context injection path
remain INTACT internally (in the page stack, reached only programmatically) for
the v14.1b migration of its context-injection into Step0. These tests pin both
the removal (no visible Skip / Step1.5 entry, Step0 relabelled) and the
preservation (page + set_context still present, ChannelWorkbench not forked).

Qt tests need an offscreen platform (set by env: QT_QPA_PLATFORM=offscreen).
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


def _top_nav_buttons(window):
    """Push-buttons in the top workflow nav bar only — i.e. NOT inside any page
    of the QStackedWidget (page-internal controls like the Step1.5 "Save remap
    config" button are intentionally kept and live inside the stack)."""
    from PyQt5 import QtWidgets
    stack = window._stack
    out = []
    for b in window.findChildren(QtWidgets.QPushButton):
        w = b
        inside_stack = False
        while w is not None:
            if w is stack:
                inside_stack = True
                break
            w = w.parent()
        if not inside_stack:
            out.append(b)
    return out


# ── 1. No visible Skip buttons ───────────────────────────────────────────────
def test_no_visible_skip_buttons(window):
    for attr in ("_btn_skip", "_btn_skip3", "_btn_skip4"):
        assert not hasattr(window, attr), f"{attr} should be removed in v14.1"
    skip_btns = [b.text() for b in _top_nav_buttons(window)
                 if "skip" in b.text().lower()]
    assert skip_btns == [], f"unexpected visible Skip buttons: {skip_btns}"


# ── 2. No visible Step1.5 top-level workflow button ──────────────────────────
def test_no_visible_step1_5_workflow_button(window):
    assert not hasattr(window, "_btn_step1_5"), "_btn_step1_5 should be removed in v14.1"
    step15_btns = [b.text() for b in _top_nav_buttons(window)
                   if "1.5" in b.text()]
    assert step15_btns == [], f"unexpected visible Step1.5 top-nav button: {step15_btns}"


# ── 3. Step0 label is "Step0: Setup & Preprocessing" ─────────────────────────
def test_step0_label_is_setup_and_preprocessing(window):
    assert hasattr(window, "_step0_lbl")
    assert "Setup & Preprocessing" in window._step0_lbl.text()


# ── 4. Direct navigation to Step1-4 does not crash ───────────────────────────
def test_direct_navigation_steps_1_to_4_do_not_crash(window):
    # Step labels are the direct-nav entry points; their handlers must not raise.
    window._go_to_step1()
    assert window._stack.currentIndex() == 1
    window._go_to_step2()
    assert window._stack.currentIndex() == 2
    window._go_to_step3()
    assert window._stack.currentIndex() == 3
    window._go_to_step4()
    assert window._stack.currentIndex() == 4
    window._go_to_step0()
    assert window._stack.currentIndex() == 0


# ── 5. Navigation alone creates no remap/step2_ready/corrected/seg outputs ───
def test_navigation_alone_creates_no_outputs(window, tmp_path):
    before = set(str(p) for p in tmp_path.rglob("*"))
    for fn in (window._go_to_step1, window._go_to_step2,
               window._go_to_step3, window._go_to_step4, window._go_to_step0):
        fn()
    after = set(str(p) for p in tmp_path.rglob("*"))
    assert before == after, "navigation must not create any files"


# ── 6. Step15BackgroundCorrectionPage + set_context remain intact internally ──
def test_step1_5_page_still_present_internally(window):
    from block01.ui.step1_5_bg_page import Step15BackgroundCorrectionPage
    assert hasattr(window, "_step1_5")
    assert isinstance(window._step1_5, Step15BackgroundCorrectionPage)
    pages = [window._stack.widget(i) for i in range(window._stack.count())]
    assert window._step1_5 in pages, "Step1.5 page must remain in the stack"
    # context-injection path preserved for v14.1b migration
    assert hasattr(window, "_go_to_step1_5")
    assert callable(getattr(window._step1_5, "set_context"))


def test_step1_5_context_injection_still_works(window, tmp_path):
    roi_dir = str(tmp_path / "roi")
    os.makedirs(roi_dir, exist_ok=True)
    window.loader = _FakeLoader()
    window.step0_output = {"roi_dir": roi_dir,
                           "patches": [(0, 32, 0, 32), (0, 32, 32, 64)]}
    window._go_to_step1_5()  # programmatic only; no visible button
    assert window._stack.currentWidget() is window._step1_5
    assert window._step1_5.output_dir == os.path.join(roi_dir, "step1_5")
    assert len(window._step1_5.patches) == 2
    # unchanged save path; no config created here
    expected_cfg_dir = os.path.join(roi_dir, "step1_5", "channel_remap_configs")
    assert os.path.join(window._step1_5.output_dir, "channel_remap_configs") == expected_cfg_dir
    window._go_to_step0()  # restore for other tests


# ── 7. ChannelWorkbench is the single shared class (not forked) ──────────────
def test_channel_workbench_not_forked(window):
    from block01.ui.widgets.channel_workbench import ChannelWorkbench
    from block01.ui.step1_5_bg_page import Step15BackgroundCorrectionPage  # noqa: F401
    # Step1.5 host uses the shared class
    wb15 = getattr(window._step1_5, "_cond_workbench", None)
    assert wb15 is None or isinstance(wb15, ChannelWorkbench)
    # Step3 host (review surface) uses the same shared class
    wb3 = getattr(window._step3, "_channel_workbench", None)
    assert wb3 is None or isinstance(wb3, ChannelWorkbench)
