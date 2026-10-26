"""Convert the Keras Mesmer model into the `.pt` the Mesmer engine loads.

Run once per deployment, in the fusion_mesmer environment (the only step that
needs TensorFlow), from the repository root:

    python scripts/convert_mesmer_to_torch.py [--force]

Source: the manifest entry `mesmer_multiplex_segmentation` (its root, or
DEEPCELL_MESMER_MODEL_PATH). Output: the directory of the entry
`mesmer_multiplex_segmentation_torch` (its root, or BLOCK01_MESMER_TORCH_PATH)
-- the same path rule the manifest tool and the engine use. Writes the weights
and a SOURCE.txt (origin, licence, what was changed), checks the four heads
against the Keras model, then rewrites and verifies the manifest.

The weights stay under the non-commercial academic licence of the original
(deepcell-auth ASSET_LICENSE): never commit or publish them.
"""
import argparse
import datetime
import hashlib
import os
import pathlib
import sys

_REPO = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO))
sys.path.insert(0, str(_REPO / "scripts"))

import model_manifest  # noqa: E402
from seg_runner.engines import model_root  # noqa: E402
from seg_runner.mesmer_torch import convert, graph, weights  # noqa: E402

TOLERANCE = 1e-4
SOURCE_TEXT = """Mesmer weights converted for PyTorch by Block01
(scripts/convert_mesmer_to_torch.py, docs/mesmer_torch_plan.md).

Source model: {source_root}
Source files (sha256):
{source_files}
Converted: {when}  deepcell {deepcell}  tensorflow {tensorflow}  torch {torch}
Head check against the Keras model (max |difference|): {diffs}

Changes: Keras Conv2D kernels transposed HWIO -> OIHW and stored with their
biases; batch-norm gamma/beta/moving mean/moving variance stored with each
layer's epsilon. No value is altered otherwise.

Licence: the original Mesmer weights are licensed under the deepcell-auth
ASSET_LICENSE (modified Apache 2.0): non-commercial academic use only; a
conversion is still the licensed Work. Do not commit or redistribute this
file. https://github.com/vanvalenlab/deepcell-auth/blob/main/ASSET_LICENSE
"""


def _sha256(path, chunk=1 << 20):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for block in iter(lambda: f.read(chunk), b""):
            h.update(block)
    return h.hexdigest()


def _head_diffs(keras_model, params):
    import numpy as np
    import torch
    x = np.random.default_rng(0).random((2, *graph.INPUT_SHAPE, 2)).astype(np.float32)
    want = keras_model.predict(x, batch_size=1, verbose=0)
    with torch.inference_mode():
        got = graph.forward(params, torch.from_numpy(x).permute(0, 3, 1, 2).contiguous())
    return [float(np.abs(a - b.permute(0, 2, 3, 1).numpy()).max()) for a, b in zip(want, got)]


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--force", action="store_true", help="replace an existing conversion")
    args = ap.parse_args(argv)
    src = pathlib.Path(model_root(model_manifest.MODELS["mesmer_multiplex_segmentation"]))
    out = pathlib.Path(model_root(model_manifest.MODELS["mesmer_multiplex_segmentation_torch"]))
    target = out / weights.WEIGHTS_FILE
    if not src.is_dir():
        print(f"no Keras Mesmer model at {src} (set DEEPCELL_MESMER_MODEL_PATH)")
        return 1
    if target.exists() and not args.force:
        print(f"{target} exists; use --force to replace it")
        return 1

    os.environ.setdefault("CUDA_VISIBLE_DEVICES", "")      # the conversion needs no GPU
    os.environ.setdefault("TF_CPP_MIN_LOG_LEVEL", "3")
    import tensorflow as tf
    import torch
    from importlib import metadata
    keras_model = tf.keras.models.load_model(str(src), compile=False)
    data = convert.from_keras(keras_model)
    weights.check(data)
    source_files = sorted(str(p.relative_to(src)) for p in src.rglob("*") if p.is_file())
    data["source"] = {"model_key": "mesmer_multiplex_segmentation", "root": str(src),
                      "files": {n: _sha256(src / n) for n in source_files},
                      "deepcell": metadata.version("deepcell"), "tensorflow": tf.__version__}
    out.mkdir(parents=True, exist_ok=True)
    tmp = target.with_suffix(".pt.tmp")
    torch.save(data, tmp)
    try:
        diffs = _head_diffs(keras_model, weights.load(str(tmp)))
    except Exception:
        tmp.unlink(missing_ok=True)
        raise
    if max(diffs) > TOLERANCE:
        tmp.unlink(missing_ok=True)
        print(f"conversion check failed: head differences {diffs} exceed {TOLERANCE}")
        return 1
    os.replace(tmp, target)
    (out / "SOURCE.txt").write_text(SOURCE_TEXT.format(
        source_root=src, when=datetime.datetime.now().isoformat(timespec="seconds"),
        source_files="\n".join(f"  {h}  {n}" for n, h in data["source"]["files"].items()),
        deepcell=data["source"]["deepcell"], tensorflow=tf.__version__, torch=torch.__version__,
        diffs=", ".join(f"{d:.2e}" for d in diffs)), encoding="utf-8")
    print(f"wrote {target} (head differences {', '.join(f'{d:.2e}' for d in diffs)})")
    if model_manifest.write():
        return 1
    return model_manifest.verify()


if __name__ == "__main__":
    sys.exit(main())
