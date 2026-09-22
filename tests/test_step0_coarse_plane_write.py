"""Step0 publishes the plane Step1 reads, and never an out-of-date one.

G3.2b.4E phase B, write side. The plane is accumulated from the corrected
pixels while they are being written, so level 0 is never scanned a second
time; the gates here are about the two things that can go wrong with a
derived file: is it the SAME answer, and can an old one ever be mistaken
for a new one.

The dangerous case has its own gate
(`test_a_second_write_of_the_same_settings_retires_the_old_plane`): same
channel, same method, same parameter, same ROI, same shape, DIFFERENT
pixels. Every static field matches; only the per-write token does not.

Everything is written under tmp_path. No real project is touched.
"""

import os

import numpy as np
import pytest

pytest.importorskip("zarr")
pytest.importorskip("tifffile")
import tifffile  # noqa: E402
import zarr  # noqa: E402

from block01.ui.step0 import search_ctrl  # noqa: E402
from block01.viewer import step1_source as sources  # noqa: E402

ROI = [96, 96 + 640, 48, 48 + 512]          # unaligned on purpose
CHANNELS = ("CH_A", "CH_B")


class _Loader:
    """Only what the worker reads from a loader."""

    def __init__(self, path, data):
        self.filepath = str(path)
        self._data = data
        self.shape = data.shape[1:]
        self.ch_map = {name: i for i, name in enumerate(CHANNELS)}

    def _read_roi_zarr(self, index, y0, y1, x0, x1):
        return self._data[index, y0:y1, x0:x1]


def _slide(tmp_path, seed=1, levels=2):
    """A small pyramidal OME-TIFF, so the stride is READ, never guessed."""
    rng = np.random.default_rng(seed)
    data = (rng.random((len(CHANNELS), 1024, 768)) * 400).astype(np.uint16)
    path = tmp_path / f"slide_{seed}_{levels}.ome.tif"
    with tifffile.TiffWriter(str(path)) as writer:
        if levels > 1:
            writer.write(data, subifds=levels - 1, tile=(256, 256),
                         photometric="minisblack")
            reduced = data
            for _ in range(levels - 1):
                reduced = reduced[:, ::4, ::4].copy()
                writer.write(reduced, subfiletype=1, tile=(256, 256),
                             photometric="minisblack")
        else:
            writer.write(data, tile=(256, 256), photometric="minisblack")
    return path, data


def _small_writer_tiles(monkeypatch, size=256):
    """Make the writer cut the ROI into many tiles.

    The worker's tile is 4096, so a fixture ROI is ONE tile and a cancel
    can only ever land after the channel is already complete -- which is
    not the case these gates are about. The real `_tile_slices` is kept;
    only the size it is called with changes, so the partition, the halo and
    the crop are still the product's own.
    """
    real = search_ctrl._tile_slices

    def smaller(height, width, tile_size, overlap):
        return real(height, width, size, overlap)

    monkeypatch.setattr(search_ctrl, "_tile_slices", smaller)


def _run_save(tmp_path, loader, out_dir, channels=CHANNELS, incremental=False,
              cancel_after_tiles=None, polygon=None):
    config = {"channel_decisions": {ch: "cucim" for ch in CHANNELS},
              "method_params": {"tophat_radius": 5, "cucim_sigma": 4}}
    roi = {"name": "ROI_1", "bbox_fullres": list(ROI)}
    if polygon is not None:
        roi["polygon_fullres"] = polygon
    worker = search_ctrl.WsiCorrectionWorker(
        loader, str(out_dir), config, rois=[roi],
        process_channels=set(channels), incremental=incremental)
    if cancel_after_tiles is not None:
        state = {"n": 0}
        real_stop = worker.stop_after_current_channel

        class _Cancelling(dict):
            pass

        original = search_ctrl._CoarsePlaneAccumulator.add

        def counting(self, values, y0, x0):
            state["n"] += 1
            if state["n"] >= cancel_after_tiles:
                real_stop()
            return original(self, values, y0, x0)

        search_ctrl._CoarsePlaneAccumulator.add = counting
        try:
            worker.run()
        finally:
            search_ctrl._CoarsePlaneAccumulator.add = original
        return worker
    worker.run()
    return worker


def _table(out_dir, channel):
    return sources.Step1SourceTable(
        decisions={channel: "cucim"},
        corrected_zarr_path=str(out_dir / "corrected_channels.zarr"),
        roi_name="ROI_1", roi_bbox=list(ROI), handoff_revision="r1")


def _oracle_tiles(table, channel, stride, blocks):
    """What the runtime reduction says, tile by tile -- the definition.

    Never empty: a gate that iterates this and finds nothing would pass
    without comparing anything.
    """
    (by0, bx0), (bh, bw) = blocks
    assert bh > 0 and bw > 0
    region = table.region(channel)
    out = {}
    step = 64
    for ty in range(by0, by0 + bh, step):
        for tx in range(bx0, bx0 + bw, step):
            rect = (ty, ty + step, tx, tx + step)
            out[rect] = sources.reduce_corrected(
                region, tuple(v * stride for v in rect), stride)
    return out


def _plain(value):
    """zarr hands attrs back in its own containers; compare by value."""
    if isinstance(value, (list, tuple)):
        return [_plain(v) for v in value]
    if isinstance(value, np.generic):
        return value.item()
    return value


def _product(out_dir, channel):
    root = zarr.open_group(str(out_dir / "corrected_channels.zarr"), mode="r")
    return root["ROI_1"][channel]


def _plane(out_dir, channel, mode="r"):
    path = out_dir / sources.COARSE_SIDECAR_DIRNAME
    if not path.exists():
        return None
    root = zarr.open_group(str(path), mode=mode)
    if "ROI_1" not in root or channel not in root["ROI_1"]:
        return None
    return root["ROI_1"][channel]


# ── the plane Step0 writes is the plane the reduction would compute ──

def test_a_save_publishes_a_plane_that_equals_the_runtime_reduction(tmp_path):
    slide, data = _slide(tmp_path)
    loader = _Loader(slide, data)
    out = tmp_path / "step0"
    _run_save(tmp_path, loader, out)

    level, stride = search_ctrl.coarsest_level_stride(slide)
    assert (level, stride) == (1, 4)
    for channel in CHANNELS:
        stored = _plane(out, channel)
        assert stored is not None, channel
        assert bool(stored.attrs["complete"]) is True
        table = _table(out, channel)
        plane = table.coarse_plane(channel, stride)
        assert plane is not None, "Step1 refused the plane Step0 just wrote"
        blocks = sources.coarse_block_range(ROI, stride)
        oracle = _oracle_tiles(table, channel, stride, blocks)
        assert oracle, "no tiles to compare"
        for rect, (want, want_valid) in oracle.items():
            values, valid = plane.tile(rect)
            assert np.array_equal(valid, want_valid)
            assert np.array_equal(values[valid], want[want_valid]), rect


def test_the_written_plane_composes_to_the_same_picture(tmp_path):
    from block01.viewer import step1_compose as compose

    slide, data = _slide(tmp_path)
    _run_save(tmp_path, _Loader(slide, data), tmp_path / "step0")
    out = tmp_path / "step0"
    _level, stride = search_ctrl.coarsest_level_stride(slide)
    (by0, bx0), (bh, bw) = sources.coarse_block_range(ROI, stride)
    rect = (by0, by0 + bh, bx0, bx0 + bw)

    from_plane, from_runtime = {}, {}
    for channel in CHANNELS:
        table = _table(out, channel)
        plane = table.coarse_plane(channel, stride)
        from_plane[channel] = plane.tile(rect)
        from_runtime[channel] = sources.reduce_corrected(
            table.region(channel), tuple(v * stride for v in rect), stride)

    mappings = {ch: (0.0, 300.0, 1.0) for ch in CHANNELS}
    for mode, kwargs in (
            ("overlay", dict(weights={ch: 1.0 for ch in CHANNELS},
                             colors={"CH_A": (1.0, 0.0, 0.0),
                                     "CH_B": (0.0, 0.5, 1.0)},
                             mappings=mappings)),
            ("fusion", dict(groups={"m": {ch: 1.0 for ch in CHANNELS}},
                            group_weights={"m": 1.0}, nucleus=("", 0.0),
                            mappings=mappings))):
        rgba_a, valid_a, _ = compose.compose(mode, from_runtime, **kwargs)
        rgba_b, valid_b, _ = compose.compose(mode, from_plane, **kwargs)
        assert rgba_a is not None and rgba_b is not None
        diff = np.abs(rgba_a[..., :3].astype(np.int16)
                      - rgba_b[..., :3].astype(np.int16))
        assert int(diff.max()) <= 1, mode
        assert np.array_equal(rgba_a[..., 3], rgba_b[..., 3]), mode
        assert np.array_equal(valid_a, valid_b), mode


# ── THE IDENTITY GATE ────────────────────────────────────────────────

def test_a_second_write_of_the_same_settings_retires_the_old_plane(tmp_path):
    """Same channel, same method, same parameter, same ROI, same shape --
    different pixels. Only the per-write token can tell them apart."""
    out = tmp_path / "step0"
    slide_a, data_a = _slide(tmp_path, seed=1)
    _run_save(tmp_path, _Loader(slide_a, data_a), out)

    _level, stride = search_ctrl.coarsest_level_stride(slide_a)
    SETTINGS = ("correction_method", "correction_param_name",
                "correction_param_value", "bg_correction_algo_version",
                "roi_name", "roi_bbox_fullres", "source_shape")
    before = _product(out, "CH_A")
    first_settings = {field: _plain(before.attrs[field]) for field in SETTINGS}
    first_token = str(before.attrs["source_identity"])
    first_pixels = np.asarray(before[:64, :64], np.float32).copy()
    assert first_token
    first_plane = np.asarray(_plane(out, "CH_A")[:, :], np.float32).copy()
    assert _table(out, "CH_A").coarse_plane("CH_A", stride) is not None

    # a second Save of the same settings over DIFFERENT raw pixels
    slide_b, data_b = _slide(tmp_path, seed=2)
    _run_save(tmp_path, _Loader(slide_b, data_b), out)

    product = _product(out, "CH_A")
    second_token = str(product.attrs["source_identity"])
    assert second_token and second_token != first_token, (
        "the write token did not move, so a stale plane could not be caught")
    # THE POINT OF THIS GATE: every static field of THIS channel is the same
    # across the two writes, so nothing but the token can tell them apart.
    # (An earlier version compared against CH_B and ended in `or True`, which
    # made the whole comparison vacuous.)
    second_settings = {field: _plain(product.attrs[field]) for field in SETTINGS}
    assert second_settings == first_settings, (
        f"the settings moved, so this is not the case the gate is about: "
        f"{first_settings} -> {second_settings}")
    # ...and the pixels underneath really did change
    assert not np.array_equal(np.asarray(product[:64, :64], np.float32),
                              first_pixels), (
        "the second write produced the same pixels; nothing to catch")

    stored = _plane(out, "CH_A")
    assert str(stored.attrs["source_identity"]) == second_token
    now = np.asarray(stored[:, :], np.float32)
    assert not np.allclose(now, first_plane), (
        "the plane was not rewritten for the new pixels")

    table = _table(out, "CH_A")
    plane = table.coarse_plane("CH_A", stride)
    assert plane is not None
    blocks = sources.coarse_block_range(ROI, stride)
    oracle = _oracle_tiles(table, "CH_A", stride, blocks)
    assert oracle, "no tiles to compare"
    for rect, (want, want_valid) in oracle.items():
        values, valid = plane.tile(rect)
        assert np.array_equal(valid, want_valid)
        assert np.array_equal(values[valid], want[want_valid])


def test_a_stale_plane_from_the_previous_write_is_refused(tmp_path):
    """The same situation, with the new write interrupted: the plane on
    disk belongs to the previous one and must not be used."""
    out = tmp_path / "step0"
    slide_a, data_a = _slide(tmp_path, seed=1)
    _run_save(tmp_path, _Loader(slide_a, data_a), out)
    _level, stride = search_ctrl.coarsest_level_stride(slide_a)
    old_plane = np.asarray(_plane(out, "CH_A")[:, :], np.float32).copy()

    # put the OLD plane back after a new write, as an interrupted run would
    slide_b, data_b = _slide(tmp_path, seed=2)
    _run_save(tmp_path, _Loader(slide_b, data_b), out)
    stored = _plane(out, "CH_A", mode="a")
    stored[:, :] = old_plane
    stored.attrs["source_identity"] = "a-token-from-the-previous-write"

    table = _table(out, "CH_A")
    assert table.coarse_plane("CH_A", stride) is None
    # and the tile is still right, from the reduction
    region = table.region("CH_A")
    (by0, bx0), (bh, bw) = sources.coarse_block_range(ROI, stride)
    rect = (by0, by0 + min(bh, 8), bx0, bx0 + min(bw, 8))
    values, valid = sources.read_tile(table, "CH_A", rect, stride=stride)
    want, want_valid = sources.reduce_corrected(
        region, tuple(v * stride for v in rect), stride)
    assert np.array_equal(valid, want_valid)
    assert np.array_equal(values[valid], want[want_valid])


def test_a_product_without_a_write_token_never_uses_a_plane(tmp_path):
    """Both stamps are cleared, so only the "a product must say which write
    it is" rule can refuse this -- not the ordinary field comparison."""
    slide, data = _slide(tmp_path)
    out = tmp_path / "step0"
    _run_save(tmp_path, _Loader(slide, data), out)
    _level, stride = search_ctrl.coarsest_level_stride(slide)
    root = zarr.open_group(str(out / "corrected_channels.zarr"), mode="a")
    root["ROI_1"]["CH_A"].attrs["source_identity"] = ""
    _plane(out, "CH_A", mode="a").attrs["source_identity"] = ""
    assert _table(out, "CH_A").coarse_plane("CH_A", stride) is None


# ── incremental, cancel, and the slide with no pyramid ───────────────

def test_an_incremental_save_leaves_the_other_channel_untouched(tmp_path):
    out = tmp_path / "step0"
    slide_a, data_a = _slide(tmp_path, seed=1)
    _run_save(tmp_path, _Loader(slide_a, data_a), out)
    keep_token = str(_product(out, "CH_B").attrs["source_identity"])
    keep_plane = np.asarray(_plane(out, "CH_B")[:, :], np.float32).copy()
    keep_pixels = np.asarray(_product(out, "CH_B")[:16, :16], np.float32).copy()

    slide_b, data_b = _slide(tmp_path, seed=2)
    _run_save(tmp_path, _Loader(slide_b, data_b), out, channels=("CH_A",),
              incremental=True)

    assert str(_product(out, "CH_B").attrs["source_identity"]) == keep_token
    assert np.array_equal(np.asarray(_plane(out, "CH_B")[:, :], np.float32),
                          keep_plane)
    assert np.array_equal(np.asarray(_product(out, "CH_B")[:16, :16],
                                     np.float32), keep_pixels)
    # ...and CH_A did move
    assert np.asarray(_plane(out, "CH_A")[:, :], np.float32).shape == keep_plane.shape


def test_a_cancelled_channel_publishes_no_plane(tmp_path, monkeypatch):
    out = tmp_path / "step0"
    slide, data = _slide(tmp_path)
    _small_writer_tiles(monkeypatch)
    _run_save(tmp_path, _Loader(slide, data), out, cancel_after_tiles=2)
    # a fresh save that was cancelled leaves neither product nor plane
    assert not (out / "corrected_channels.zarr").exists()
    assert not (out / sources.COARSE_SIDECAR_DIRNAME).exists()


def test_a_cancelled_incremental_save_keeps_the_finished_channels_plane(
        tmp_path, monkeypatch):
    out = tmp_path / "step0"
    slide_a, data_a = _slide(tmp_path, seed=1)
    _run_save(tmp_path, _Loader(slide_a, data_a), out)
    keep = np.asarray(_plane(out, "CH_B")[:, :], np.float32).copy()

    slide_b, data_b = _slide(tmp_path, seed=2)
    _small_writer_tiles(monkeypatch)
    _run_save(tmp_path, _Loader(slide_b, data_b), out, channels=("CH_A",),
              incremental=True, cancel_after_tiles=2)

    # CH_A: its level 0 was dropped and its plane went first -- neither may
    # be readable as "the current one"
    assert _plane(out, "CH_A") is None
    assert np.array_equal(np.asarray(_plane(out, "CH_B")[:, :], np.float32),
                          keep)


def test_a_slide_without_a_pyramid_writes_no_plane_and_still_saves(tmp_path):
    out = tmp_path / "step0"
    slide, data = _slide(tmp_path, levels=1)
    assert search_ctrl.coarsest_level_stride(slide) is None
    _run_save(tmp_path, _Loader(slide, data), out)
    assert (out / "corrected_channels.zarr").exists()
    assert _plane(out, "CH_A") is None
    # and Step1 reduces as it always did
    table = _table(out, "CH_A")
    assert table.coarse_plane("CH_A", 4) is None


# ── the pixels the mask writes, and the accumulator's own bound ──────

def test_polygon_zeros_are_samples_in_the_plane_too(tmp_path):
    out = tmp_path / "step0"
    slide, data = _slide(tmp_path)
    y0, y1, x0, x1 = ROI
    polygon = [[x0 + 8, y0 + 8], [x1 - 8, y0 + 8], [x1 - 8, y1 - 8],
               [x0 + 8, y1 - 8]]
    _run_save(tmp_path, _Loader(slide, data), out, polygon=polygon)
    _level, stride = search_ctrl.coarsest_level_stride(slide)
    table = _table(out, "CH_A")
    plane = table.coarse_plane("CH_A", stride)
    assert plane is not None
    region = table.region("CH_A")
    (by0, bx0), (bh, bw) = sources.coarse_block_range(ROI, stride)
    rect = (by0, by0 + bh, bx0, bx0 + bw)
    values, valid = plane.tile(rect)
    want, want_valid = sources.reduce_corrected(
        region, tuple(v * stride for v in rect), stride)
    assert np.array_equal(valid, want_valid)
    assert np.array_equal(values[valid], want[want_valid])
    # A ZERO IS A SAMPLE. The blocks entirely inside the masked-out border
    # are averages of zeros: value 0.0 and VALID -- not "no sample", which
    # would make the region's edge transparent instead of dark.
    masked_blocks = values[valid] == 0.0
    assert int(np.count_nonzero(masked_blocks)) > 0, (
        "the polygon's zero band produced no all-zero block, so this gate "
        "never exercised the zero-is-a-sample rule")


def test_the_completion_mark_is_written_after_the_pixels_and_the_identity(
        tmp_path, monkeypatch):
    """A plane says it is there only once it IS there.

    The order is what protects an interrupted run: pixels, then identity,
    then `complete`. This records the writer's own sequence of operations
    on the sidecar dataset rather than inspecting the file afterwards,
    because afterwards the order is exactly what cannot be seen.
    """
    events = []

    class _Recorder:
        def __init__(self, inner):
            self._inner = inner
            self.attrs = _Attrs(inner.attrs)

        def __setitem__(self, item, value):
            events.append("pixels")
            self._inner[item] = value

        def __getattr__(self, name):
            return getattr(self._inner, name)

    class _Attrs:
        def __init__(self, inner):
            self._inner = inner

        def __setitem__(self, key, value):
            events.append(f"attr:{key}")
            self._inner[key] = value

        def __getitem__(self, key):
            return self._inner[key]

        def get(self, key, default=None):
            return self._inner.get(key, default)

    real_group = zarr.open_group

    def patched(path, mode="r", **kwargs):
        group = real_group(path, mode=mode, **kwargs)
        if str(path).endswith(sources.COARSE_SIDECAR_DIRNAME) and mode == "a":
            class _Wrap:
                def require_group(self, name):
                    inner = group.require_group(name)

                    class _G:
                        def create_dataset(self, *a, **kw):
                            return _Recorder(inner.create_dataset(*a, **kw))

                        def __contains__(self, key):
                            return key in inner

                        def __getitem__(self, key):
                            return inner[key]

                        def __delitem__(self, key):
                            del inner[key]

                    return _G()

                def __contains__(self, key):
                    return key in group

                def __getitem__(self, key):
                    return group[key]

            return _Wrap()
        return group

    monkeypatch.setattr(zarr, "open_group", patched)
    slide, data = _slide(tmp_path)
    _run_save(tmp_path, _Loader(slide, data), tmp_path / "step0")

    assert events, "the writer published no plane at all"
    assert events[-1] == "attr:complete", events[-6:]
    assert "pixels" in events[:events.index("attr:complete")]
    assert any(e.startswith("attr:source_identity")
               for e in events[:events.index("attr:complete")])


def test_the_accumulator_costs_two_small_arrays_per_region(tmp_path):
    acc = search_ctrl._CoarsePlaneAccumulator([0, 59040, 0, 35520], 64)
    assert acc.nbytes() == 923 * 555 * 16
    assert acc.nbytes() < 9 * 1024 * 1024
