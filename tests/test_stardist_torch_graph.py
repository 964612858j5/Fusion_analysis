"""StarDist on PyTorch, step 1 (docs/stardist_torch_plan.md §12): the network
and its strictly loaded weights.

The comparison with TensorFlow is the port's DIAGNOSTIC (plan §8): it lives in
`scripts/stardist_consistency.py`, reports numbers and asserts nothing. What is
asserted here needs no TensorFlow: the structure of the graph, the shapes the
stardist package expects, strict loading and the content digest.
"""
import numpy as np
import pytest

torch = pytest.importorskip("torch")

from block01.seg_runner.stardist_torch import graph, weights  # noqa: E402


def _random_params(seed=0):
    g = torch.Generator().manual_seed(seed)
    tensors = {k: torch.randn(shape, generator=g) * 0.05
               for k, shape in graph.expected_shapes().items()}
    return {"format": weights.FORMAT, "tensors": tensors,
            "source": {"model_key": "stardist_2D_versatile_fluo", "root": "/keras/here"}}


def test_the_network_has_nineteen_convolutions_and_two_heads():
    names = graph.layer_names()
    shapes = graph.expected_shapes()
    assert len(names) == 19
    assert len(shapes) == 38                      # weight + bias of every convolution
    assert names[:2] == ["conv2d_1", "conv2d_2"]
    assert names[-3:] == ["features", "prob", "dist"]
    assert shapes["prob.weight"] == (1, graph.CONV_AFTER_UNET, 1, 1)
    assert shapes["dist.weight"] == (graph.N_RAYS, graph.CONV_AFTER_UNET, 1, 1)
    # the three down levels, then the three up levels of the U-Net
    assert [shapes[f"down_level_{n}_no_1.weight"][0] for n in range(3)] == [32, 64, 128]
    assert [shapes[f"up_level_{n}_no_{graph.N_CONV_PER_DEPTH}.weight"][0]
            for n in (2, 1, 0)] == [64, 32, 32]


def test_forward_gives_half_size_heads():
    params = _random_params()
    x = torch.from_numpy(np.random.default_rng(0).random((1, 1, 64, 64)).astype(np.float32))
    with torch.inference_mode():
        prob, dist = graph.forward(params["tensors"], x)
    assert tuple(prob.shape) == (1, 1, 32, 32)
    assert tuple(dist.shape) == (1, graph.N_RAYS, 32, 32)
    assert torch.isfinite(prob).all() and torch.isfinite(dist).all()
    assert float(prob.min()) >= 0.0 and float(prob.max()) <= 1.0   # the head is a sigmoid


def test_forward_refuses_another_input_shape():
    params = _random_params()
    with pytest.raises(ValueError):
        graph.forward(params["tensors"], torch.zeros(64, 64))
    with pytest.raises(ValueError):
        graph.forward(params["tensors"], torch.zeros(1, 2, 64, 64))


def test_weights_round_trip_and_load_strictly(tmp_path):
    data = _random_params()
    path = tmp_path / "w.pt"
    torch.save(data, path)
    digest = weights.content_digest(data)
    loaded = weights.load(path, "cpu", digest)
    assert loaded["content_sha256"] == digest
    assert set(loaded["tensors"]) == set(graph.expected_shapes())
    for k, v in loaded["tensors"].items():
        assert v.dtype == torch.float32 and tuple(v.shape) == graph.expected_shapes()[k]
    with pytest.raises(weights.WeightsError):          # no digest: refuse, never guess
        weights.load(path, "cpu", "")


def test_a_missing_extra_or_misshaped_file_is_refused(tmp_path):
    good = _random_params()
    digest = weights.content_digest(good)
    path = tmp_path / "w.pt"

    torch.save({"format": weights.FORMAT,
                "tensors": {k: v for k, v in list(good["tensors"].items())[1:]}}, path)
    with pytest.raises(weights.WeightsError):
        weights.load(path, "cpu", digest)

    extra = dict(good["tensors"])
    extra["nope.weight"] = torch.zeros(1, 1, 1, 1)
    torch.save({"format": weights.FORMAT, "tensors": extra}, path)
    with pytest.raises(weights.WeightsError):
        weights.load(path, "cpu", digest)

    wrong = dict(good["tensors"])
    wrong["prob.weight"] = torch.zeros(2, 1, 1, 1)
    torch.save({"format": weights.FORMAT, "tensors": wrong}, path)
    with pytest.raises(weights.WeightsError):
        weights.load(path, "cpu", digest)

    torch.save(dict(good, format="other/9"), path)
    with pytest.raises(weights.WeightsError):
        weights.load(path, "cpu", digest)


def test_the_digest_ignores_where_and_when_but_not_what():
    a, b = _random_params(), _random_params()
    b["source"] = {"model_key": "x", "root": "/another/place", "torch": "9.9"}
    assert weights.content_digest(a) == weights.content_digest(b)
    assert weights.content_digest(a) != weights.content_digest(_random_params(seed=1))


def test_one_changed_tensor_changes_the_digest_and_load_refuses(tmp_path):
    data = _random_params()
    digest = weights.content_digest(data)
    path = tmp_path / "w.pt"
    torch.save(data, path)
    tampered = dict(data["tensors"])
    tampered["middle_0.bias"] = tampered["middle_0.bias"] + 1e-3
    changed = dict(data, tensors=tampered)
    assert weights.content_digest(changed) != digest
    torch.save(changed, path)
    with pytest.raises(weights.WeightsError):
        weights.load(path, "cpu", digest)


def test_a_different_dtype_changes_the_digest_and_is_refused(tmp_path):
    data = _random_params()
    digest = weights.content_digest(data)
    path = tmp_path / "w64.pt"
    torch.save(dict(data, tensors={k: v.double() for k, v in data["tensors"].items()}), path)
    stored = weights.read(path)
    assert weights.content_digest(stored) != digest      # the dtype is part of the content
    with pytest.raises(weights.WeightsError):
        weights.load(path, "cpu", digest)
