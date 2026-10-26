"""Mesmer in PyTorch, step 1 (docs/mesmer_torch_plan.md §6 A): the network and
its strictly loaded weights.

The comparison with TF is the port's correctness check (user ruling
2026-10-25): heads within 1e-4 on the CPU. It needs TensorFlow and the
installed MultiplexSegmentation model and is skipped without them -- the
release check runs it for real.
"""
import os

import numpy as np
import pytest

torch = pytest.importorskip("torch")

from block01.seg_runner.mesmer_torch import graph, weights  # noqa: E402

KERAS_MODEL = os.environ.get("DEEPCELL_MESMER_MODEL_PATH") or \
    "/sda1/Fusion/benchmark/spacec/models/Mesmer_model/MultiplexSegmentation"


def _random_params(seed=0):
    g = torch.Generator().manual_seed(seed)
    tensors = {k: torch.randn(shape, generator=g) * 0.05 for k, shape in graph.expected_shapes().items()}
    for k in tensors:
        if k.endswith(".var"):
            tensors[k] = tensors[k].abs() + 1.0
    eps = {name: 1e-3 for name in graph.batch_norm_layers()}
    return {"format": weights.FORMAT, "tensors": tensors, "eps": eps}


def test_the_network_has_resnet50_and_four_heads():
    shapes = graph.expected_shapes()
    assert len([k for k in shapes if k.endswith(".weight")]) == 78
    assert len(graph.batch_norm_layers()) == 57
    assert shapes["conv_1_semantic_1.weight"] == (3, 128, 1, 1)
    assert shapes["conv5_block3_3_conv.weight"] == (2048, 512, 1, 1)


def test_forward_gives_four_heads_of_the_input_size():
    data = _random_params()
    params = {"tensors": data["tensors"], "eps": data["eps"]}
    x = torch.rand(2, 2, 256, 256)
    with torch.inference_mode():
        heads = graph.forward(params, x)
    assert [tuple(h.shape) for h in heads] == [(2, 1, 256, 256), (2, 3, 256, 256),
                                               (2, 1, 256, 256), (2, 3, 256, 256)]
    assert all(bool((h >= 0).all()) for h in heads)                      # relu / softmax
    assert torch.allclose(heads[1].sum(1), torch.ones(2, 256, 256), atol=1e-5)


def test_weights_round_trip_and_load_strictly(tmp_path):
    data = _random_params()
    path = tmp_path / weights.WEIGHTS_FILE
    torch.save(dict(data, source={"note": "synthetic"}), path)
    params = weights.load(str(path))
    assert set(params["tensors"]) == set(graph.expected_shapes())
    assert params["source"] == {"note": "synthetic"}

    def broken(mutate):
        d = _random_params()
        mutate(d)
        p = tmp_path / "broken.pt"
        torch.save(d, p)
        with pytest.raises(weights.WeightsError):
            weights.load(str(p))

    broken(lambda d: d["tensors"].pop("P3.bias"))                           # missing
    broken(lambda d: d["tensors"].update({"extra.weight": torch.zeros(1)}))  # unexpected
    broken(lambda d: d["tensors"].update({"P3.bias": torch.zeros(7)}))       # wrong shape
    broken(lambda d: d["eps"].pop("conv1_bn"))                               # epsilon missing
    broken(lambda d: d.update(format="something-else"))                      # foreign file


def _keras():
    tf = pytest.importorskip("tensorflow")
    if not os.path.isdir(KERAS_MODEL):
        pytest.skip(f"no Mesmer Keras model at {KERAS_MODEL}")
    return tf.keras.models.load_model(KERAS_MODEL, compile=False)


def test_the_port_matches_the_tf_network_on_the_cpu():
    from block01.seg_runner.mesmer_torch import convert
    km = _keras()
    tensors, eps = weights.check(convert.from_keras(km))
    params = {"tensors": {k: v.float() for k, v in tensors.items()}, "eps": eps}
    x = np.random.default_rng(1).random((2, 256, 256, 2)).astype(np.float32)
    want = km.predict(x, batch_size=1, verbose=0)
    with torch.inference_mode():
        got = graph.forward(params, torch.from_numpy(x).permute(0, 3, 1, 2).contiguous())
    for i, (a, b) in enumerate(zip(want, got)):
        diff = float(np.abs(a - b.permute(0, 2, 3, 1).numpy()).max())
        assert diff <= 1e-4, (i, diff)
