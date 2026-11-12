"""Step0's Save writes real display windows, never the 0..1 placeholder
(docs/v17_step0_placeholder_windows_plan.md, backlog 12).

Found on a 40-channel uint16 TMA: the Save filled every channel without a
window with min 0 / max 1, and the restore, the handoff and Step1's file
fallback took it as a window -- every pixel above 1 at full brightness.

The page is `test_step0_own_windows_page.py`'s (three channels, overviews in
memory); the slide's pixel type is set per test.
"""

import json
import os
import types

import numpy as np
import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
pytest.importorskip("block01.qt")

tifffile = pytest.importorskip("tifffile")

from block01.core.display_identity import SHARED_TABLE, STEP0_TABLE  # noqa: E402
from block01.core.display_mapping import seed_display_range  # noqa: E402
from block01.ui.step0 import window_tables as wt  # noqa: E402

import test_step0_own_windows_page as own  # noqa: E402
from test_step0_background_correction_tab import app  # noqa: E402,F401

PLACEHOLDER = wt.window_params(0.0, 1.0)


def _page(app, monkeypatch, integer=True, pixels=None):
    page = own._page(app)
    monkeypatch.setattr(page, "_slide_is_integer", lambda: integer)
    if pixels is not None:
        page._slide_lowres_array = (lambda ch, blocking=False, resident_only=False: pixels.get(ch))
    return page


def _win(p):
    return (float(p["min"]), float(p["max"]), float(p.get("gamma", 1.0)))


def test_is_placeholder_is_an_exact_match():
    assert wt.is_placeholder(PLACEHOLDER)
    assert not wt.is_placeholder(wt.window_params(0.0, 1.0, auto=True))      # a real seed of an all-zero channel
    assert not wt.is_placeholder(wt.window_params(0.0, 1.0, 0.8))
    assert not wt.is_placeholder(wt.window_params(0.0, 2.0))
    assert not wt.is_placeholder(None) and not wt.is_placeholder({"min": "x"})


@pytest.mark.parametrize("dtype", [np.uint16, np.uint8])
def test_the_save_writes_a_real_window_for_every_channel(app, monkeypatch, dtype):
    hi = 60000 if dtype == np.uint16 else 250
    rng = np.random.default_rng(3)
    pixels = {ch: rng.uniform(1, hi, (64, 64)).astype(np.float32) for ch in ("DAPI", "CD3", "CD20")}
    page = _page(app, monkeypatch, pixels=pixels)
    records = page._records(STEP0_TABLE)
    records["params"].clear()                                       # nothing seeded yet ...
    records["user_adjusted"].clear()
    records["params"]["CD20"] = dict(PLACEHOLDER)                   # ... and one stale placeholder
    page.set_display_mapping("CD3", 100.0, 900.0, 1.0)              # a typed window
    cfg = page._step0_save_config(page._cond_workbench)
    chans = cfg["channels"]
    assert sorted(chans) == ["CD20", "CD3", "DAPI"]
    assert not any(wt.is_placeholder(p) for p in chans.values())
    assert _win(chans["CD3"]) == (100.0, 900.0, 1.0)                # the typed one is kept
    for ch in ("DAPI", "CD20"):
        lo, hi_ = seed_display_range(pixels[ch])
        assert _win(chans[ch])[:2] == pytest.approx((lo, hi_)), ch
        # filed like an on-screen seed: the record and the state agree
        assert _win(page._records(STEP0_TABLE)["params"][ch])[:2] == pytest.approx((lo, hi_))
        assert page.display.state.mapping(ch, table=STEP0_TABLE)[:2] == pytest.approx((lo, hi_))
    assert page._save_unwindowed == []


def test_a_channel_not_in_memory_is_left_out_not_written_as_0_1(app, monkeypatch, tmp_path):
    pixels = dict(own.PIXELS)
    pixels.pop("CD20")
    page = _page(app, monkeypatch, pixels=pixels)
    page._records(STEP0_TABLE)["params"].pop("CD20", None)
    page.display.state.set_scope("step0")
    cfg = page._step0_save_config(page._cond_workbench)
    assert "CD20" not in cfg["channels"]                             # not the workbench's own 0..1
    assert page._save_unwindowed == ["CD20"]
    # the written file and the change check agree: a second look is "no change"
    path = tmp_path / "step0_channel_remap.json"
    monkeypatch.setattr(page, "_step0_conditioning_config_path", lambda: str(path))
    path.write_text(json.dumps(cfg))
    assert page._intensity_settings_changed() is False
    # once the overview is here, the next Save writes it
    page._slide_lowres_array = lambda ch, blocking=False, resident_only=False: own.PIXELS.get(ch)
    cfg2 = page._step0_save_config(page._cond_workbench)
    assert "CD20" in cfg2["channels"] and not wt.is_placeholder(cfg2["channels"]["CD20"])
    assert page._intensity_settings_changed() is True


def test_a_restore_does_not_take_a_placeholder_on_an_integer_slide(app, monkeypatch):
    page = _page(app, monkeypatch)
    records = page._records(STEP0_TABLE)
    records["params"]["CD20"] = wt.window_params(1.0, 5.0)
    records["user_adjusted"].add("CD20")
    saved = {"CD3": dict(PLACEHOLDER), "DAPI": wt.window_params(30.0, 300.0)}
    page._install_windows(STEP0_TABLE, saved, user={"DAPI"}, replace=True)
    records = page._records(STEP0_TABLE)
    assert "CD3" not in records["params"]                             # left to the seed
    assert _win(records["params"]["DAPI"]) == (30.0, 300.0, 1.0)
    assert records["user_adjusted"] == {"DAPI"}                      # replace=True cleared CD20 and its flag
    assert "CD20" not in records["params"]
    state = page.display.state
    assert state.mapping("CD3", table=STEP0_TABLE) is None
    assert state.mapping("DAPI", nucleus=True, table=STEP0_TABLE)[:2] == (30.0, 300.0)   # the nucleus role


def test_a_float_slide_keeps_a_stored_0_1_window(app, monkeypatch):
    page = _page(app, monkeypatch, integer=False)
    page._install_windows(STEP0_TABLE, {"CD20": dict(PLACEHOLDER)}, user=False, replace=True)
    assert _win(page._records(STEP0_TABLE)["params"]["CD20"]) == (0.0, 1.0, 1.0)


def test_the_handoff_copies_real_windows_only(app, monkeypatch):
    page = _page(app, monkeypatch)
    src = page._records(STEP0_TABLE)
    src["params"].clear()
    src["params"]["CD3"] = dict(PLACEHOLDER)
    src["params"]["DAPI"] = wt.window_params(20.0, 200.0)
    page._handoff_windows_to_shared()
    dst = page._records(SHARED_TABLE)
    assert sorted(dst["params"]) == ["DAPI"]
    assert page.display.state.mapping("CD3") in (None, page.display.state.mapping("CD3", table=SHARED_TABLE))
    assert page.display.state.mapping("DAPI")[:2] == (20.0, 200.0)


def test_step1s_file_fallback_checks_the_hash_then_drops_placeholders(tmp_path):
    from block01.ui.main_window import MainWindow
    from block01.utils import channel_remap_config as crc
    cfg = crc.default_channel_remap_config()
    cfg["channels"] = {"CD3": dict(PLACEHOLDER), "DAPI": wt.window_params(20.0, 200.0)}
    path = tmp_path / "step0_channel_remap.json"
    crc.save_channel_remap_config(cfg, str(path))
    good_hash = crc.channel_remap_config_hash(crc.load_channel_remap_config(str(path)))

    def fake(integer, expected):
        win = types.SimpleNamespace()
        win._step0 = types.SimpleNamespace(_slide_is_integer=lambda: integer)
        win.step0_output = {"handoff_schema_version": 2, "channel_remap_config_path": str(path),
                            "channel_remap_config_hash": expected}
        win._without_remap_placeholders = types.MethodType(MainWindow._without_remap_placeholders, win)
        return win

    params, src = MainWindow._load_step0_remap_params(fake(True, good_hash))
    assert src == str(path.resolve()) or src == os.path.abspath(str(path))
    assert sorted(params) == ["DAPI"]                                # integer slide: the placeholder dropped
    params, _ = MainWindow._load_step0_remap_params(fake(False, good_hash))
    assert sorted(params) == ["CD3", "DAPI"]                         # a float slide keeps it
    params, src = MainWindow._load_step0_remap_params(fake(True, "0" * 64))
    assert (params, src) == ({}, "")                                 # the hash is checked on the whole file


def test_after_a_load_every_overview_is_asked_for_in_the_background(app, monkeypatch):
    page = _page(app, monkeypatch)
    asked = []
    monkeypatch.setattr(page.display, "ensure_lowres", lambda names: asked.append(list(names)) or [])
    page._request_all_overviews()
    assert asked and sorted(asked[-1]) == sorted(page._channel_order)


@pytest.mark.parametrize("dtype,integer", [(np.uint16, True), (np.uint8, True), (np.float32, False)])
def test_the_pixel_type_comes_from_the_slide_file(app, tmp_path, dtype, integer):
    page = own._page(app)
    path = tmp_path / f"s_{np.dtype(dtype).name}.ome.tif"
    tifffile.imwrite(str(path), np.zeros((2, 32, 32), dtype), ome=True, metadata={"axes": "CYX"})
    page.loader.filepath = str(path)
    assert page._slide_is_integer() is integer


def test_a_typed_0_1_window_is_kept_as_typed(app, monkeypatch):
    """A window the user typed is never a placeholder, whatever its numbers:
    the file, the record and the state keep it alike."""
    page = _page(app, monkeypatch)
    page.set_display_mapping("CD3", 0.0, 1.0, 1.0)                  # typed, the placeholder's numbers
    assert wt.is_user(page._records(STEP0_TABLE), "CD3")
    cfg = page._step0_save_config(page._cond_workbench)
    assert _win(cfg["channels"]["CD3"]) == (0.0, 1.0, 1.0)
    assert page.display.state.mapping("CD3", table=STEP0_TABLE) == (0.0, 1.0, 1.0)
    # and a restore of a typed 0..1 keeps it too
    page._install_windows(STEP0_TABLE, {"CD3": dict(PLACEHOLDER)}, user={"CD3"}, replace=True)
    assert _win(page._records(STEP0_TABLE)["params"]["CD3"]) == (0.0, 1.0, 1.0)


def test_a_placeholder_already_in_the_state_is_replaced_there_too(app, monkeypatch):
    page = _page(app, monkeypatch)
    records = page._records(STEP0_TABLE)
    records["params"]["CD20"] = dict(PLACEHOLDER)                   # untyped, as an old Save left it
    records["user_adjusted"].discard("CD20")
    page.display.state.set_mapping("CD20", 0.0, 1.0, 1.0, origin="restore", persist=False, table=STEP0_TABLE)
    cfg = page._step0_save_config(page._cond_workbench)
    lo, hi = seed_display_range(own.PIXELS["CD20"])
    assert _win(cfg["channels"]["CD20"])[:2] == pytest.approx((lo, hi))
    assert page.display.state.mapping("CD20", table=STEP0_TABLE)[:2] == pytest.approx((lo, hi))
    assert _win(page._records(STEP0_TABLE)["params"]["CD20"])[:2] == pytest.approx((lo, hi))
    assert not wt.is_user(page._records(STEP0_TABLE), "CD20")       # filed as a seed, not typed


def test_the_save_writes_the_file_without_placeholders(app, monkeypatch, tmp_path):
    """The real Save function, its JSON on disk: every channel real; one not
    in memory is absent and the warning says so."""
    pixels = dict(own.PIXELS)
    pixels.pop("CD20")
    page = _page(app, monkeypatch, pixels=pixels)
    page._records(STEP0_TABLE)["params"].pop("CD20", None)
    monkeypatch.setattr(page, "_step0_conditioning_out_dir", lambda: str(tmp_path))
    monkeypatch.setattr(page, "_apply_source_aware_identity", lambda cfg: None)
    warned = []
    monkeypatch.setattr(page, "_warn_load_row", warned.append)
    assert page._persist_step0_remap_config() is True
    saved = json.loads((tmp_path / "step0_channel_remap.json").read_text())["channels"]
    assert sorted(saved) == ["CD3", "DAPI"]
    assert not any(wt.is_placeholder(p) for p in saved.values())
    assert warned and "1 channel(s) had no display window yet" in warned[0]
    assert page._intensity_settings_changed() is False               # the file is what the Save writes
