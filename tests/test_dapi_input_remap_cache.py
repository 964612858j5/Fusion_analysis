"""DAPI/StarDist input-zarr reuse invalidates on a Step0 remap change.

Before: _dapi_meta_compare_view had no remap fingerprint, so after the user
changed the DAPI channel's Step0 remap the existing DAPI input zarr was reused
un-changed (stale, un-remapped input). Now the meta carries a channel_remap_hash
for the DAPI channel and the compare view includes it — but only when the DAPI
channel actually carries a remap, so legacy/no-remap runs are not needlessly
regenerated."""

import os
import types

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
pytest.importorskip("PyQt5")

import block01.ui.main_window as mw

MainWindow = mw.MainWindow
FCFG = {"nucleus": {"channel": "DAPI"}, "resolution": None}


def _stub(remap):
    s = types.SimpleNamespace()
    s._active_roi = None
    s._rois = []
    s.step0_output = {}
    s._corrected_zarr_path = ""
    s.loader = types.SimpleNamespace(shape=(64, 48), filepath="/x/raw.ome.tiff")
    s._load_step0_remap_params = lambda: (remap, "")
    s._remap_params_hash = MainWindow._remap_params_hash
    return s


def _meta(remap):
    return MainWindow._expected_dapi_input_meta(_stub(remap), FCFG, "stardist")


def _view(meta):
    return MainWindow._dapi_meta_compare_view(meta)


def test_no_remap_has_no_hash_key():
    assert "channel_remap_hash" not in _meta({})


def test_dapi_remap_adds_hash():
    assert _meta({"DAPI": {"min": 0.0, "max": 100.0}}).get("channel_remap_hash")


def test_non_dapi_remap_ignored():
    # a membrane-channel remap does not affect the DAPI-only input
    assert "channel_remap_hash" not in _meta({"PanCK": {"min": 0.0, "max": 100.0}})


def test_no_remap_matches_legacy_meta():
    expected = _meta({})
    legacy = dict(expected)  # a pre-fingerprint meta also lacks the key
    assert _view(expected) == _view(legacy)


def test_remap_change_invalidates():
    a = _meta({"DAPI": {"min": 0.0, "max": 100.0}})
    b = _meta({"DAPI": {"min": 0.0, "max": 200.0}})
    assert _view(a) != _view(b)


def test_remap_vs_legacy_invalidates():
    remapped = _meta({"DAPI": {"min": 0.0, "max": 100.0}})
    legacy = _meta({})  # no key -> reused zarr predates the remap -> regenerate
    assert _view(remapped) != _view(legacy)


def test_remap_params_hash_stable_and_sensitive():
    h1 = MainWindow._remap_params_hash({"DAPI": {"min": 0.0, "max": 100.0}})
    h2 = MainWindow._remap_params_hash({"DAPI": {"max": 100.0, "min": 0.0}})
    assert h1 == h2 and len(h1) == 16              # key order independent
    assert h1 != MainWindow._remap_params_hash({"DAPI": {"min": 0.0, "max": 101.0}})
