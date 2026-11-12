"""Every entry into Step 0.5 (Regions) starts on Select ▸ Click, whatever
tool was left on (the user's acceptance notes item 3, 2026-11-12;
docs/v17_acceptance_notes_plan.md B19); a half-drawn polygon goes with it.
The step bar's own handlers, the main window rig of the Regions tests.
"""

import pytest

from test_regions_r2 import _pump, _enter_regions  # noqa: F401
from test_step0_compare_tiles import app  # noqa: F401
from test_step1_shared_camera import _close, _window


@pytest.mark.parametrize("away", ["_go_to_step0", "_go_to_step1", "_go_to_step2"])
def test_coming_back_to_regions_starts_on_select_click(app, monkeypatch, tmp_path, away):
    from block01.qt import QtWidgets
    monkeypatch.setattr(QtWidgets.QMessageBox, "information", staticmethod(lambda *a, **k: None))
    monkeypatch.setattr(QtWidgets.QMessageBox, "warning", staticmethod(lambda *a, **k: None))
    rig = _window(app, monkeypatch, tmp_path)
    try:
        _enter_regions(rig)
        page = rig.w._regions
        page._set_tool("polygon")
        page.ctl.poly_pts = [(10, 10), (10, 60)]                 # a polygon half drawn
        assert page.family_buttons["Mask"].isChecked()
        rig.w._step1_context_ready = True
        getattr(rig.w, away)()
        _pump()
        assert rig.w._current_step != rig.w.REGIONS_STEP
        rig.w._go_to_regions()
        _pump(12)
        assert rig.w._current_step == rig.w.REGIONS_STEP
        assert page.ctl.tool == "select" and page.ctl.poly_pts == []
        assert page.family_buttons["Select"].isChecked() and page.family_buttons["Select"].text() == "Select: Click ▾"
        assert not page.family_buttons["Mask"].isChecked()
    finally:
        _close(rig)
