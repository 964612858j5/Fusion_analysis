"""Mesmer in PyTorch, step 3 (docs/mesmer_torch_plan.md §6 B and C): the
TF-free prediction flow against DeepCell's own `Mesmer.predict`, BOTH running
the same torch network (DeepCell's model swapped for an adapter). Only the
flow is compared, so it must agree pixel for pixel -- the intermediate
values and every final mask.

Needs deepcell (with TensorFlow, to build the original Application) and the
converted weights; skipped without them. Real tiles are read (never written)
from a fused store when it is on this machine.
"""
import os

import numpy as np
import pytest

torch = pytest.importorskip("torch")

from block01.seg_runner import engines  # noqa: E402
from block01.seg_runner.mesmer_torch import app, graph, weights  # noqa: E402

REAL_STORE = "/sda1/Fusion/tmp/rois/full_wsi_20260810_201330_8de7/step1/fused_Full WSI.zarr"


@pytest.fixture(scope="module")
def pair():
    os.environ.setdefault("TF_CPP_MIN_LOG_LEVEL", "3")
    tf = pytest.importorskip("tensorflow")
    try:
        torch_dir = engines.model_path("mesmer_multiplex_segmentation_torch")
        keras_dir = engines.mesmer_model_path()
    except engines.ModelMissingError as exc:
        pytest.skip(str(exc))
    from deepcell.applications import Mesmer
    params = weights.load(os.path.join(torch_dir, weights.WEIGHTS_FILE))

    class _Adapter:                       # DeepCell's model slot, our network
        input_shape = (None, 256, 256, 2)

        def predict(self, x, batch_size=1):
            with torch.inference_mode():
                out = graph.forward(params, torch.from_numpy(np.ascontiguousarray(x, np.float32))
                                    .permute(0, 3, 1, 2).contiguous())
            return [o.permute(0, 2, 3, 1).numpy() for o in out]

    original = Mesmer(model=tf.keras.models.load_model(keras_dir, compile=False))
    original.model = _Adapter()
    return original, app.MesmerTorch(params, "cpu")


def _synthetic(h, w, seed=0, nuclei_only=False):
    rng = np.random.default_rng(seed)
    out = np.zeros((1, h, w, 2), np.float32)
    yy, xx = np.mgrid[:h, :w]
    for _ in range(max(4, h * w // 2500)):
        cy, cx, r = rng.uniform(0, h), rng.uniform(0, w), rng.uniform(4, 9)
        d = np.hypot(yy - cy, xx - cx)
        out[0, ..., 0] += np.exp(-(d / r) ** 2) * rng.uniform(0.4, 1.0)
        out[0, ..., 1] += np.exp(-((d - 2 * r) / 1.5) ** 2) * rng.uniform(0.2, 1.0)
    out += rng.normal(0, 0.03, out.shape).clip(0).astype(np.float32)
    if nuclei_only:
        out[..., 1] = 0                   # the nuclei methods' [nucleus, 0]
    return out


def _both(pair, image, mpp=0.5, comps=("whole-cell", "nuclear"), kwargs=None):
    original, ours = pair
    kwargs = kwargs or {}
    got = ours.predict(image, image_mpp=mpp, compartments=comps, kwargs=kwargs)
    for c in comps:
        key = "postprocess_kwargs_whole_cell" if c == "whole-cell" else "postprocess_kwargs_nuclear"
        want = original.predict(image, image_mpp=mpp, compartment=c, batch_size=1,
                                **{key: dict(kwargs.get(c) or {})})
        assert want.shape == got[c].shape, (c, want.shape, got[c].shape)
        assert np.array_equal(want, got[c]), (c, int(want.max()), int(got[c].max()),
                                              float(np.mean(want == got[c])))
    return got


@pytest.mark.parametrize("h,w", [(200, 200), (256, 256), (257, 301), (256, 700), (1100, 1300)])
def test_final_masks_equal_deepcell_at_every_size(pair, h, w):
    got = _both(pair, _synthetic(h, w, seed=h + w))
    assert got["whole-cell"].max() > 0 and got["nuclear"].max() > 0


@pytest.mark.parametrize("mpp", [0.325, 0.65])
def test_the_resolution_change_both_ways(pair, mpp):
    # stays >= 256 on both axes after the change (straddling 256 -- one axis
    # below, the other above -- is refused, as by the original)
    _both(pair, _synthetic(520, 500, seed=7), mpp=mpp)


def test_nuclei_input_and_non_default_thresholds(pair):
    _both(pair, _synthetic(300, 300, seed=3, nuclei_only=True), comps=("nuclear",),
          kwargs={"nuclear": {"maxima_threshold": 0.2, "interior_threshold": 0.3}})
    _both(pair, _synthetic(300, 300, seed=4),
          kwargs={"whole-cell": {"maxima_threshold": 0.05, "interior_threshold": 0.1}})


def test_intermediate_values_equal_deepcell(pair):
    original, ours = pair
    image = _synthetic(520, 610, seed=11)
    resized = original._resize_input(image, 0.65)
    from block01.seg_runner.mesmer_torch import postprocess, preprocess, resize, tiling
    assert np.array_equal(resized, resize.to_model_resolution(image, 0.65))
    pre_o = original._preprocess(resized)
    pre_t = preprocess.mesmer_preprocess(resized)
    assert pre_o.dtype == pre_t.dtype and np.array_equal(pre_o, pre_t)
    want = original._run_model(image=resized, batch_size=1, pad_mode="constant")
    tiles, info = tiling.tile(pre_t)
    got = postprocess.format_outputs(tiling.untile(ours._network(tiles), info))
    for c in ("whole-cell", "nuclear"):
        for a, b in zip(want[c], got[c]):
            assert a.dtype == b.dtype and np.array_equal(a, b), c


def test_a_window_the_model_cannot_take_is_refused_like_the_original(pair):
    original, ours = pair
    image = _synthetic(300, 200, seed=5)
    with pytest.raises(Exception):
        original.predict(image, image_mpp=0.5, compartment="whole-cell", batch_size=1)
    with pytest.raises(ValueError, match="cannot take a 300 x 200"):
        ours.predict(image, image_mpp=0.5, compartments=("whole-cell",))


def test_real_tiles_equal_deepcell(pair):
    if not os.path.isdir(REAL_STORE):
        pytest.skip("no real fused store on this machine")
    import zarr
    z = zarr.open(REAL_STORE, mode="r")
    rng = np.random.default_rng(1)
    done = 0
    for _ in range(60):
        y, x = int(rng.integers(0, z.shape[0] - 768)), int(rng.integers(0, z.shape[1] - 768))
        t = np.asarray(z[y:y + 768, x:x + 768, :])
        if (t[..., 1] > 0).mean() < 0.9:
            continue
        f = t.astype(np.float32) / 65535.0
        _both(pair, np.stack([f[..., 1], f[..., 0]], -1)[None])   # [nucleus, fusion]
        done += 1
        if done == 2:
            break
    assert done == 2
