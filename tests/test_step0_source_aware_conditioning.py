"""v14.5b: Step0 Channel Conditioning writes source-aware PREVIEW configs.

Drives Step0Page._apply_source_aware_identity (the save-path injection) with a
fake loader + a real corrected zarr. Asserts per-channel SourceRequest /
CalibrationSourceIdentity, mixture mode from actual identities, preview_only /
step2_ready invariants, and that flipping to step2_ready hard-rejects.
"""

import numpy as np
import pytest

pytest.importorskip("PyQt5")
zarr = pytest.importorskip("zarr")


@pytest.fixture(scope="module")
def app():
    from PyQt5 import QtWidgets
    a = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    return a


class _FakeLoader:
    def __init__(self, corrected_path=None):
        self.filepath = "/data/raw.ome.tif"
        self.ch_map = {"CD68": 0, "CK19": 1, "DAPI": 9}
        self.shape = (200, 200)
        self._corrected_zarr_path = corrected_path

    def read_region(self, ch, y0, y1, x0, x1, normalize=False):
        return np.ones((y1 - y0, x1 - x0), dtype=np.float32)


def _corrected_zarr(tmp_path, channels=("CD68",), shape=(64, 64)):
    from block01.core.bg_correction import stamp_corrected_channel_identity
    path = str(tmp_path / "corrected_channels.zarr")
    root = zarr.open_group(path, mode="w")
    g = root.create_group("ROI_1")
    g.attrs["bbox_fullres"] = [0, shape[0], 0, shape[1]]
    for i, ch in enumerate(channels):
        ds = g.create_dataset(ch, data=np.ones(shape, dtype=np.float32))
        stamp_corrected_channel_identity(
            ds, ch, channel_index=i, correction_method="tophat",
            roi_name="ROI_1", roi_bbox_fullres=[0, shape[0], 0, shape[1]])
    return path


def _step0(app, corrected_path=None):
    from block01.ui.step0.step0_page import Step0Page
    s = Step0Page()
    s.loader = _FakeLoader(corrected_path)
    s.patches = [(0, 64, 0, 64)]
    s.current_patch_idx = 0
    return s


def _cfg(channels=("CD68", "CK19")):
    return {
        "channels": {c: {"min": 0.0, "max": 1.0} for c in channels},
        "source_policy": {"preview_only": True, "step2_ready": False},
    }


# 1 + 9. Preview invariants + camp default.
def test_save_keeps_preview_only_and_camp_default(app, tmp_path):
    s = _step0(app, _corrected_zarr(tmp_path))
    cfg = _cfg()
    s._apply_source_aware_identity(cfg)
    assert cfg["source_policy"]["preview_only"] is True
    assert cfg["source_policy"]["step2_ready"] is False
    assert cfg["camp_source_policy"] == "raw_gi_only"


# 2. Default (no request set) -> raw for all.
def test_default_all_raw(app, tmp_path):
    s = _step0(app, _corrected_zarr(tmp_path))
    cfg = _cfg()
    s._apply_source_aware_identity(cfg)
    for ch in ("CD68", "CK19"):
        assert cfg["channels"][ch]["source_request"]["requested_source"] == "raw_ome"
        assert cfg["channels"][ch]["calibration_source_identity"]["actual_source_kind"] == "raw_ome"
    assert cfg["source_mixture_mode"] == "homogeneous_raw"


# 3. Corrected request, available -> corrected identity from actual array.
def test_corrected_request_available(app, tmp_path):
    s = _step0(app, _corrected_zarr(tmp_path, channels=("CD68", "CK19")))
    s.set_channel_source_request("CD68", "corrected_zarr")
    s.set_channel_source_request("CK19", "corrected_zarr")
    cfg = _cfg()
    s._apply_source_aware_identity(cfg)
    for ch in ("CD68", "CK19"):
        csi = cfg["channels"][ch]["calibration_source_identity"]
        assert cfg["channels"][ch]["source_request"]["requested_source"] == "corrected_zarr"
        assert csi["actual_source_kind"] == "corrected_zarr"
        assert csi["intensity_space"] == "background_corrected_marker_image"
        assert csi["source_shape"] == [64, 64]      # actual array
    assert cfg["source_mixture_mode"] == "homogeneous_corrected"


# 4 + 5. Corrected request, channel missing -> Strategy B fallback, honest.
def test_corrected_request_fallback(app, tmp_path):
    s = _step0(app, _corrected_zarr(tmp_path, channels=("CD68",)))   # CK19 absent
    s.set_channel_source_request("CK19", "corrected_zarr")
    cfg = _cfg()
    s._apply_source_aware_identity(cfg)
    req = cfg["channels"]["CK19"]["source_request"]
    csi = cfg["channels"]["CK19"]["calibration_source_identity"]
    assert req["requested_source"] == "corrected_zarr"
    assert req["reason"] == "corrected_unavailable_fallback"
    assert csi["actual_source_kind"] == "raw_ome"   # never faked corrected


# 7. All requested corrected, partial fallback -> mixed_raw_corrected.
def test_partial_fallback_is_mixed(app, tmp_path):
    s = _step0(app, _corrected_zarr(tmp_path, channels=("CD68",)))   # CK19 absent
    s.set_channel_source_request("CD68", "corrected_zarr")
    s.set_channel_source_request("CK19", "corrected_zarr")
    cfg = _cfg()
    s._apply_source_aware_identity(cfg)
    assert cfg["channels"]["CD68"]["calibration_source_identity"]["actual_source_kind"] == "corrected_zarr"
    assert cfg["channels"]["CK19"]["calibration_source_identity"]["actual_source_kind"] == "raw_ome"
    assert cfg["source_mixture_mode"] == "mixed_raw_corrected"     # NOT homogeneous_corrected


# 10. Flip the produced preview config to step2_ready -> hard reject.
def test_flip_to_step2_ready_hard_rejects(app, tmp_path):
    from block01.utils.channel_remap_config import validate_step2_remap_config
    s = _step0(app, _corrected_zarr(tmp_path))
    s.set_channel_source_request("CD68", "corrected_zarr")
    cfg = _cfg(("CD68",))
    # give the channel concrete step2-shaped params so only the source-aware
    # guard (not other validators) is what trips on the flip
    cfg["channels"]["CD68"].update(
        min=0.0, max=1.0, step2_compatible=True,
        calibration_source_matches_step2=True,
        intensity_space="raw_ome_native_float",
        step2_pre_remap_source="raw_ome_native_float")
    cfg["source_policy"].update(
        calibration_source_matches_step2=True,
        source_alignment_mode="single_native_source")
    s._apply_source_aware_identity(cfg)
    # manual tamper: claim step2_ready
    cfg["source_policy"]["preview_only"] = False
    cfg["source_policy"]["step2_ready"] = True
    errs, resolved = validate_step2_remap_config(cfg, allow_preview_remap=False)
    assert any("source-aware" in e for e in errs)
    assert resolved == {}


# 11. No corrected zarr at all -> default raw, no crash.
def test_no_corrected_zarr_defaults_raw(app):
    s = _step0(app, corrected_path=None)
    s.set_channel_source_request("CD68", "corrected_zarr")   # but none exists
    cfg = _cfg(("CD68",))
    s._apply_source_aware_identity(cfg)
    csi = cfg["channels"]["CD68"]["calibration_source_identity"]
    assert csi["actual_source_kind"] == "raw_ome"
    assert cfg["channels"]["CD68"]["source_request"]["reason"] == "corrected_unavailable_fallback"


# 6. Successful config: every conditioned channel carries both fields.
def test_success_every_channel_has_both_fields(app, tmp_path):
    s = _step0(app, _corrected_zarr(tmp_path, channels=("CD68",)))
    s.set_channel_source_request("CD68", "corrected_zarr")     # CK19 default raw
    cfg = _cfg(("CD68", "CK19"))
    s._apply_source_aware_identity(cfg)
    for ch in ("CD68", "CK19"):
        assert "source_request" in cfg["channels"][ch]
        assert "calibration_source_identity" in cfg["channels"][ch]


# ── v14.5b.1 Strategy A: whole-save failure, no partial config ───────────────

class _BadLoader(_FakeLoader):
    """Raw read raises for `bad_channel` -> that channel's identity unresolvable."""
    def __init__(self, bad_channel, corrected_path=None):
        super().__init__(corrected_path)
        self._bad = bad_channel

    def read_region(self, ch, y0, y1, x0, x1, normalize=False):
        if ch == self._bad:
            raise RuntimeError("disk read failed")
        return np.ones((y1 - y0, x1 - x0), dtype=np.float32)


def _step0_bad(app, bad_channel):
    from block01.ui.step0.step0_page import Step0Page
    s = Step0Page()
    s.loader = _BadLoader(bad_channel)
    s.patches = [(0, 64, 0, 64)]
    s.current_patch_idx = 0
    return s


# 1+2. Identity failure raises and produces NO partial config (nothing stamped).
def test_identity_failure_raises_and_no_partial(app):
    from block01.utils.calibration_source import SourceAwareIdentityError
    s = _step0_bad(app, "CK19")
    cfg = _cfg(("CD68", "CK19"))
    with pytest.raises(SourceAwareIdentityError) as ei:
        s._apply_source_aware_identity(cfg)
    assert ei.value.channel == "CK19"
    # NO partial: not even the would-succeed CD68 is stamped
    for ch in ("CD68", "CK19"):
        assert "source_request" not in cfg["channels"][ch]
        assert "calibration_source_identity" not in cfg["channels"][ch]
    # 4. mixture mode NOT computed from the partial subset
    assert "source_mixture_mode" not in cfg


# 3. Failed channel name is surfaced in the error.
def test_identity_failure_surfaces_channel_name(app):
    from block01.utils.calibration_source import SourceAwareIdentityError
    s = _step0_bad(app, "CD68")
    with pytest.raises(SourceAwareIdentityError) as ei:
        s._apply_source_aware_identity(_cfg(("CD68", "CK19")))
    assert "CD68" in str(ei.value)
    assert ei.value.channel == "CD68"
    assert "disk read failed" in ei.value.message


# 1. _save_step0_remap_config writes NO file when identity fails.
def test_save_writes_no_file_on_identity_failure(app, tmp_path, monkeypatch):
    from block01.ui.step0 import step0_page as sp
    from block01.utils.calibration_source import SourceAwareIdentityError

    s = sp.Step0Page()

    # stub workbench: has data, returns a minimal config
    class _WB:
        def has_channel_data(self):
            return True

        def build_config(self):
            return {"channels": {"CD68": {"min": 0.0, "max": 1.0}},
                    "source_policy": {"preview_only": True, "step2_ready": False}}

    s._cond_workbench = _WB()
    s.output_dir = str(tmp_path)
    # force identity resolution to fail
    monkeypatch.setattr(
        sp.Step0Page, "_apply_source_aware_identity",
        lambda self, cfg: (_ for _ in ()).throw(
            SourceAwareIdentityError(channel="CK19", requested_source="corrected_zarr")))
    # spy the file dialog + the warning
    dialog_calls = []
    monkeypatch.setattr(
        sp.QFileDialog, "getSaveFileName",
        lambda *a, **k: dialog_calls.append(a) or (str(tmp_path / "should_not_exist.json"), ""))
    warnings = []
    monkeypatch.setattr(sp.QMessageBox, "warning",
                        lambda *a, **k: warnings.append(a))
    monkeypatch.setattr(sp.QMessageBox, "information", lambda *a, **k: None)

    s._save_step0_remap_config()

    # aborted BEFORE the file dialog -> no path chosen, no file written
    assert dialog_calls == []
    assert not (tmp_path / "should_not_exist.json").exists()
    # no config files anywhere under output_dir
    import os
    written = [f for _, _, fs in os.walk(str(tmp_path)) for f in fs if f.endswith(".json")]
    assert written == []
    # warning surfaced the failed channel
    assert warnings and any("CK19" in str(arg) for arg in warnings[0])
