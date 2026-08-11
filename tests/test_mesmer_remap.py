"""Mesmer selected-channels input applies the Step0 manual channel remap.

Before: build_mesmer_input always percentile-normalized raw nuclear/membrane
channels, so a saved Step0 remap never reached Mesmer's selected-channels path.
Now each channel that carries a remap is conditioned with apply_channel_remap
(raw units); channels without a remap keep the percentile path (partial coverage
is fine)."""

import numpy as np

import block01.utils.mesmer_utils as mu

FULL = {"min": 0.0, "max": 300.0, "gamma": 1.0}


def _src():
    rng = np.random.default_rng(0)
    return {
        "DAPI": (rng.random((16, 16)) * 300).astype(np.float32),
        "PanCK": (rng.random((16, 16)) * 300).astype(np.float32),
    }


def test_mesmer_applies_remap_to_nuclear_and_membrane(monkeypatch):
    calls = []
    real = mu.apply_channel_remap
    monkeypatch.setattr(mu, "apply_channel_remap", lambda arr, p: calls.append(p) or real(arr, p))
    mu.build_mesmer_input(
        _src(), nuclear_channel="DAPI", membrane_channels=["PanCK"],
        input_mode="selected_channels",
        channel_remap_params={"DAPI": FULL, "PanCK": FULL})
    assert calls == [FULL, FULL]  # nuclear + one membrane, in read order


def test_mesmer_percentile_when_no_remap(monkeypatch):
    def _boom(*a, **k):
        raise AssertionError("apply_channel_remap must NOT run without a remap")
    monkeypatch.setattr(mu, "apply_channel_remap", _boom)
    mu.build_mesmer_input(_src(), nuclear_channel="DAPI",
                          membrane_channels=["PanCK"], input_mode="selected_channels")


def test_mesmer_partial_remap_membrane_only(monkeypatch):
    calls = []
    real = mu.apply_channel_remap
    monkeypatch.setattr(mu, "apply_channel_remap", lambda arr, p: calls.append(p) or real(arr, p))
    mu.build_mesmer_input(
        _src(), nuclear_channel="DAPI", membrane_channels=["PanCK"],
        input_mode="selected_channels", channel_remap_params={"PanCK": FULL})
    assert calls == [FULL]  # DAPI falls back to percentile; only PanCK remapped


class _FakeLoader:
    """OMETIFFLoader-like source: records the normalize flag read_region gets."""
    def __init__(self):
        self.calls = []

    def read_region(self, ch, y0, y1, x0, x1, downsample=1, normalize=True):
        self.calls.append((ch, normalize))
        return (np.arange((y1 - y0) * (x1 - x0), dtype=np.float32)
                .reshape(y1 - y0, x1 - x0) * 10.0)


def test_mesmer_loader_source_reads_raw_not_normalized():
    # Step1 Mesmer patch preview passes the loader as channel_source. Each channel
    # must be read RAW (normalize=False) so the raw-unit remap window is correct;
    # read_region defaults to normalize=True, which caused the preview intensity bug.
    ldr = _FakeLoader()
    mu.build_mesmer_input(ldr, nuclear_channel="DAPI", membrane_channels=["PanCK"],
                          bbox=(0, 8, 0, 8), input_mode="selected_channels")
    assert ldr.calls  # nuclear + membrane read
    assert all(norm is False for _ch, norm in ldr.calls)


def test_mesmer_remap_window_above_data_zeroes_channel():
    batch = mu.build_mesmer_input(
        _src(), nuclear_channel="DAPI", membrane_channels=["PanCK"],
        input_mode="selected_channels",
        channel_remap_params={"PanCK": {"min": 1e5, "max": 1e5 + 1, "gamma": 1.0}})
    assert batch.shape == (1, 16, 16, 2)
    assert float(batch[0, :, :, 1].max()) < 0.01  # membrane conditioned to ~0
