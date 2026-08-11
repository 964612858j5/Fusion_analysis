"""HQ marker segmentation applies the Step0 manual channel remap.

Before: segment_nuclei_hq always percentile-normalized raw markers, ignoring the
remap config (so HQ 'attached but not applied'). Now it uses apply_channel_remap
for channels that carry a remap (parity with HQ2/CSD)."""

import numpy as np

import block01.workers.hq_marker_segmentation as hq


def _case():
    nuclei = np.zeros((30, 30), np.uint32)
    nuclei[14:17, 14:17] = 1
    marker = (np.random.default_rng(0).random((30, 30)) * 200).astype(np.float32)
    return nuclei, marker


def test_hq_uses_remap_when_params_given(monkeypatch):
    calls = []
    real = hq.apply_channel_remap
    monkeypatch.setattr(hq, "apply_channel_remap",
                        lambda arr, p: calls.append(p) or real(arr, p))
    nuclei, marker = _case()
    hq.segment_nuclei_hq(
        nuclei, [marker], ["CD8"], max_cell_radius=6,
        channel_remap_params={"CD8": {"min": 0.0, "max": 200.0, "gamma": 1.0}})
    assert calls == [{"min": 0.0, "max": 200.0, "gamma": 1.0}]


def test_hq_percentile_when_no_remap(monkeypatch):
    def _boom(*a, **k):
        raise AssertionError("apply_channel_remap must NOT run without a remap")
    monkeypatch.setattr(hq, "apply_channel_remap", _boom)
    nuclei, marker = _case()
    # no channel_remap_params -> plain percentile path, apply_channel_remap unused
    hq.segment_nuclei_hq(nuclei, [marker], ["CD8"], max_cell_radius=6)


def test_hq_remap_changes_normalized_signal():
    """A remap window above the data zeroes the marker; percentile keeps signal."""
    from block01.core.channel_remap import apply_channel_remap
    marker = (np.random.default_rng(1).random((20, 20)) * 100 + 20).astype(np.float32)
    remapped = apply_channel_remap(marker, {"min": 1000.0, "max": 1001.0, "gamma": 1.0})
    pct = hq.percentile_normalize(marker, 1.0, 99.5)
    assert float(remapped.max()) < 0.01          # window above data -> ~0
    assert float(pct.max()) > 0.5                 # percentile keeps signal
