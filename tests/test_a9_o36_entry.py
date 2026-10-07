"""Block A9-O3-6: the first Step1 entry does no work for what is not on screen.

P1'  Step0's panel-host workbench builds its channel list and the inspector's
     histogram when they are shown; the params are installed at once.
P3'  a pre-segmentation restore works the pixel identity out once.
P4   Step1's GPU viewer refreshes once per whole-state install, not once per
     announced colour and window.
"""

import numpy as np
import pytest
from PyQt5 import QtWidgets


@pytest.fixture(scope="module")
def app():
    return QtWidgets.QApplication.instance() or QtWidgets.QApplication([])


# ── P1' ───────────────────────────────────────────────────────────────

def _workbench(app):
    from block01.ui.widgets.channel_workbench import ChannelWorkbench
    return ChannelWorkbench(show_reference_bar=False, show_enabled_checkbox=False,
                            multichannel_overlay=True, show_banner=False,
                            step0_intensity_panel=True)


def _images():
    rng = np.random.default_rng(0)
    return {"DAPI": rng.random((32, 32), np.float32) * 100,
            "CD3": None, "CD8": None}


def test_hidden_panel_host_installs_params_but_builds_no_rows(app):
    wb = _workbench(app)
    wb.set_channel_images(_images(), active="DAPI", visible=["DAPI"])
    assert set(wb._params) == {"DAPI", "CD3", "CD8"}
    assert wb.channel_params_seeded("DAPI")
    assert wb.active_channel() == "DAPI"
    assert wb._layer_list._rows == {}, "rows were built for a hidden widget"
    assert wb._layer_list_owed and wb._histogram_owed


def test_rows_and_histogram_are_built_when_shown(app):
    wb = _workbench(app)
    wb.set_channel_images(_images(), active="DAPI", visible=["DAPI"])
    calls = []
    real = wb._histogram.set_data
    wb._histogram.set_data = lambda *a, **k: (calls.append(1), real(*a, **k))[1]
    wb.show()
    app.processEvents()
    assert set(wb._layer_list._rows) == {"DAPI", "CD3", "CD8"}
    assert not wb._layer_list_owed
    assert calls, "the owed histogram was not drawn when the inspector showed"
    assert not wb._histogram_owed
    wb.hide()


def test_detached_inspector_pays_the_histogram_in_its_own_window(app):
    wb = _workbench(app)
    wb.set_channel_images(_images(), active="DAPI", visible=["DAPI"])
    panel = wb.detach_inspector()
    host = QtWidgets.QWidget()
    lay = QtWidgets.QVBoxLayout(host)
    lay.addWidget(panel)
    host.show()
    app.processEvents()
    assert not wb._histogram_owed
    assert wb._layer_list._rows == {}, "the workbench itself is still hidden"
    host.hide()


def test_other_hosts_build_rows_at_once(app):
    from block01.ui.widgets.channel_workbench import ChannelWorkbench
    wb = ChannelWorkbench()
    wb.set_channel_images(_images(), active="DAPI")
    assert set(wb._layer_list._rows) == {"DAPI", "CD3", "CD8"}


# ── P4 ────────────────────────────────────────────────────────────────

class _State:
    def __init__(self):
        self.fanout = False

    def install_fanout_active(self):
        return self.fanout


def _mount(monkeypatch, state):
    from block01.ui import step1_viewer_mount as vm
    mount = vm.Step1WholeSlideMount.__new__(vm.Step1WholeSlideMount)
    monkeypatch.setattr(vm.Step1WholeSlideMount, "_domain", property(lambda self: None))
    monkeypatch.setattr(vm.Step1WholeSlideMount, "_state", property(lambda self: state))
    mount._mode = "overlay"
    monkeypatch.setattr(vm.draft_spec, "build_spec",
                        lambda *a, **k: {"mode": "overlay", "weights": {"CD3": 1.0}})
    refreshes = []
    monkeypatch.setattr(vm.Step1WholeSlideMount, "_refresh_gpu",
                        lambda self, reason: refreshes.append(reason) or True)
    return mount, refreshes


def test_an_install_refreshes_the_gpu_once(monkeypatch):
    state = _State()
    mount, refreshes = _mount(monkeypatch, state)
    state.fanout = True
    mount._on_gpu_state_installed(None)          # announced first, state whole
    for _ in range(29):
        mount._on_gpu_color("CD3", "#ff0000")
        mount._on_gpu_mapping("CD3")
    assert refreshes == ["state-installed"]


def test_outside_an_install_every_change_still_refreshes(monkeypatch):
    state = _State()
    mount, refreshes = _mount(monkeypatch, state)
    mount._on_gpu_color("CD3", "#ff0000")
    mount._on_gpu_mapping("CD3")
    assert refreshes == ["colour", "intensity"]


# ── P3' ───────────────────────────────────────────────────────────────

def test_a_preseg_restore_works_the_pixel_identity_out_once(monkeypatch):
    from block01.ui import main_window as mw

    class _W:
        _params_source = mw.PRESEG_SOURCE

        def __init__(self):
            self.current_calls = 0
            self.refreshed_with = None
            self._preseg_methods = type("M", (), {"set_methods": lambda s, m: None,
                                                  "set_progress": lambda s, t: None})()
            self._preseg_patches = type("P", (), {"set_selected_ids": lambda s, i: None})()

        def _preseg_current(self):
            self.current_calls += 1
            return "key", "fhash"

        def _start_results_for_run(self, combos):
            pass

        def _refresh_preseg_results(self, current=None):
            self.refreshed_with = current

        def _check_save_unlock(self):
            pass

        def _show_montage_patches(self):
            pass

        def _montage_sync_outlines(self):
            pass

    monkeypatch.setattr(mw.run_store, "is_done", lambda d: True)
    monkeypatch.setattr(mw.preseg_run, "read_run",
                        lambda d: {"combos": [{"combo_id": "c1"}], "tasks": []})
    monkeypatch.setattr(mw.preseg_run, "load_records", lambda d: {})
    monkeypatch.setattr(mw.preseg_run, "selectable", lambda *a: (True, ""))
    monkeypatch.setattr(mw, "summary_line", lambda *a: "")
    monkeypatch.setattr(mw.QTimer, "singleShot", staticmethod(lambda *a: None))
    w = _W()
    assert mw.MainWindow._rm_restore_preseg(
        w, {"methods": [1], "run_dir": "/x/preseg_1", "used_combo": "c1"})
    assert w.current_calls == 1
    assert w.refreshed_with == ("key", "fhash")
    assert w._preseg_selected["combo_id"] == "c1"
