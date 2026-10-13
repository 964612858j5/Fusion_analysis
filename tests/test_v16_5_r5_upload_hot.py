"""v16_5 R5 (docs/v16_5_r5_zoom_plan.md §2): the Rust core's upload hot
window, apart from the input hot window, through the Python config entry.

The pacing rule itself (which deadline, whether blocks may be reserved) is
tested in Rust (`frame/pace.rs`, `cargo test`); here: the defaults, the two
measurement switches' units, and a config without `upload_hot_s` (the core
then takes `input_hot_s`) still builds a core.
"""

import os

import numpy as np
import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from block01.ui import step1_core_binding as binding  # noqa: E402
from block01.ui.step1_core_binding import core_config  # noqa: E402
from block01.ui.step1_viewer_mount import demo_budgets  # noqa: E402

SWITCHES = ("BLOCK01_A9_RUST_UPLOAD_HOT_MS", "BLOCK01_A9_RUST_HOT_UPLOAD_MS")


@pytest.fixture
def no_switches(monkeypatch):
    for name in SWITCHES:
        monkeypatch.delenv(name, raising=False)
    return monkeypatch


def test_defaults_without_switches(no_switches):
    config = core_config(demo_budgets())
    assert config["input_hot_s"] == binding.INPUT_HOT_S
    assert config["upload_hot_s"] == binding.UPLOAD_HOT_S
    assert config["hot_upload_ms"] == binding.HOT_UPLOAD_MS
    # the R5 choice (A9 §55: candidate D)
    assert (config["input_hot_s"], config["upload_hot_s"], config["hot_upload_ms"]) == (0.060, 0.060, 4.0)


def test_upload_window_switch_is_seconds(no_switches):
    no_switches.setenv("BLOCK01_A9_RUST_UPLOAD_HOT_MS", "20")
    config = core_config(demo_budgets())
    assert config["upload_hot_s"] == pytest.approx(0.020)      # ms -> s
    assert config["hot_upload_ms"] == binding.HOT_UPLOAD_MS    # the other one untouched
    assert config["input_hot_s"] == binding.INPUT_HOT_S        # reservation window unchanged


def test_hot_upload_switch_stays_ms(no_switches):
    no_switches.setenv("BLOCK01_A9_RUST_HOT_UPLOAD_MS", "1.5")  # not the default
    config = core_config(demo_budgets())
    assert config["hot_upload_ms"] == pytest.approx(1.5)
    assert config["upload_hot_s"] == binding.UPLOAD_HOT_S


def test_config_without_upload_window_builds(no_switches, tmp_path):
    fvc = pytest.importorskip("fusion_viewer_core")
    tifffile = pytest.importorskip("tifffile")
    path = tmp_path / "r5.ome.tif"
    data = np.random.default_rng(5).integers(0, 255, (2, 600, 500), dtype=np.uint8)
    with tifffile.TiffWriter(str(path), ome=True) as tw:
        tw.write(data, tile=(256, 256), metadata={"axes": "CYX", "Channel": {"Name": ["A", "B"]}})
    from block01.ui import step1_core_adapter as adapter
    from block01.viewer.raw_tile_provider import RawTileProvider
    raw = RawTileProvider(str(path))
    config = core_config(demo_budgets())
    del config["upload_hot_s"]
    core = fvc.ViewerCore(adapter.build_raw_manifest(raw), config)
    core.set_display([(0, 0.0, 255.0, 1.0, 1.0, 1.0, 1.0, 1.0)], False)
    core.set_view(0, [0, 0, 256, 256], 1, True)
    core.paint(0, 1, 1, [0.0, 256.0, 0.0, 256.0])          # no GL: drains, plans, returns
