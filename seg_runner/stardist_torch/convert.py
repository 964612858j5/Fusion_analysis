# The weights and the file format handled here are StarDist 0.9.2's (BSD-3-Clause,
# Uwe Schmidt and Martin Weigert); the code is ours. See NOTICE and
# LICENSE-stardist / LICENSE-csbdeep in this directory.
"""Convert StarDist's Keras weights (`weights_best.h5`) into the `.pt` the
network loads -- without TensorFlow.

The `.h5` is a plain Keras weights file: layer groups with `weight_names`
attributes and `kernel`/`bias` datasets. `h5py` reads them directly, so a
conversion needs neither TensorFlow nor the Keras model, and can run on any
machine that has the installed model folder.

Strictness: every layer `graph.expected_shapes()` names must be there with
its shape and float32 dtype, and no other tensor may be present. A mismatch
is an error, never a half-converted file. The self-check (a forward pass on
random input) and the content digest run on a temporary file inside the
target directory; only then is it moved into place with one `os.replace`.

    python -m seg_runner.stardist_torch.convert --source DIR --out DIR [--force]
"""
import argparse
import json
import os
import pathlib
import secrets
import sys

from . import graph, weights

#: the files of the source model this conversion reads / records.
SOURCE_FILES = ("config.json", "thresholds.json", "weights_best.h5")


class ConvertError(RuntimeError):
    """The source model is not the one this port reproduces."""


def _sha256(path, chunk=1 << 20):
    import hashlib
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for block in iter(lambda: f.read(chunk), b""):
            h.update(block)
    return h.hexdigest()


def check_config(config):
    """The config must describe exactly the network `graph` implements."""
    for key, want in graph.CONFIG.items():
        got = config.get(key)
        same = list(got) == list(want) if isinstance(want, list) or isinstance(got, list) else got == want
        if not same:
            raise ConvertError(f"config.json has {key}={got!r}; this port reproduces {want!r} "
                               "(2D_versatile_fluo with backbone unet)")


def from_h5(path):
    """`{tensor name: float32 tensor}` read from the Keras weights file."""
    import h5py
    import numpy as np
    import torch

    want = graph.expected_shapes()
    found = {}
    with h5py.File(path, "r") as f:
        names = f.attrs.get("layer_names")
        if names is None:
            raise ConvertError(f"{path} is not a Keras weights file (no layer_names)")
        for group_name in names:
            group_name = group_name.decode() if isinstance(group_name, bytes) else group_name
            try:
                group = f[group_name]
            except KeyError as exc:
                raise ConvertError(f"{path}: layer_names names a group that is not "
                                   f"there: {group_name!r}") from exc
            weight_names = group.attrs.get("weight_names")
            if weight_names is None:
                weight_names = []
            for weight_name in weight_names:
                weight_name = weight_name.decode() if isinstance(weight_name, bytes) else weight_name
                layer, _, rest = weight_name.partition("/")
                field = rest.split(":", 1)[0]
                key = {"kernel": "weight", "bias": "bias"}.get(field)
                if key is None:
                    raise ConvertError(f"{weight_name}: this port has no '{field}' weights")
                key = f"{layer}.{key}"
                if key not in want:
                    raise ConvertError(f"{weight_name}: the network has no tensor {key!r}")
                array = np.asarray(group[weight_name][()])
                if array.dtype != np.float32:
                    raise ConvertError(f"{weight_name}: dtype {array.dtype}, expected float32")
                if field == "kernel":                       # Keras HWIO -> torch OIHW
                    array = np.transpose(array, (3, 2, 0, 1))
                if tuple(array.shape) != want[key]:
                    raise ConvertError(f"{weight_name}: shape {tuple(array.shape)}, "
                                       f"the network needs {want[key]}")
                found[key] = torch.from_numpy(np.ascontiguousarray(array))
    missing = sorted(set(want) - set(found))
    if missing:
        raise ConvertError(f"{path} is missing {len(missing)} tensors, e.g. {missing[:3]}")
    return found


def source_dict(source_root):
    """What the conversion came from (kept in the file, not in its digest)."""
    from importlib import metadata

    def version(name):
        try:
            return metadata.version(name)
        except metadata.PackageNotFoundError:
            return ""

    files = {name: _sha256(os.path.join(source_root, name))
             for name in SOURCE_FILES if os.path.isfile(os.path.join(source_root, name))}
    keras_version = ""
    try:
        import h5py
        with h5py.File(os.path.join(source_root, "weights_best.h5"), "r") as f:
            raw = f.attrs.get("keras_version")
            keras_version = raw.decode() if isinstance(raw, bytes) else str(raw or "")
    except OSError:
        pass
    import torch
    return {"model_key": "stardist_2D_versatile_fluo",
            "root": os.path.abspath(source_root),
            "files": files,
            "stardist": version("stardist"), "csbdeep": version("csbdeep"),
            "keras_version": keras_version, "torch": str(torch.__version__)}


def self_check(data):
    """Forward passes on random input, on the data it is given: a 256x256
    square and a 301x257 rectangle padded to 304x272 the way `stardist` pads
    before the network (`_axes_div_by`). Called on the file's data before it
    is published."""
    import numpy as np
    import torch
    unit = graph.PAD_UNIT
    sizes = [(256, 256),
             (((301 + unit - 1) // unit) * unit, ((257 + unit - 1) // unit) * unit)]
    for height, width in sizes:
        x = torch.from_numpy(np.random.default_rng(0).random((1, 1, height, width)).astype(np.float32))
        with torch.inference_mode():
            prob, dist = graph.forward(data["tensors"], x)
        want_h, want_w = height // graph.GRID[0], width // graph.GRID[1]
        if tuple(prob.shape) != (1, 1, want_h, want_w) or \
                tuple(dist.shape) != (1, graph.N_RAYS, want_h, want_w):
            raise ConvertError(f"the converted network gives {tuple(prob.shape)} / {tuple(dist.shape)} "
                               f"for a {height}x{width} input, expected "
                               f"(1, 1, {want_h}, {want_w}) / (1, {graph.N_RAYS}, {want_h}, {want_w})")
        if not torch.isfinite(prob).all() or not torch.isfinite(dist).all():
            raise ConvertError("the converted network gives NaN or inf on random input")


def convert_dir(source_root, out_root, force=False, expected_digest=None):
    """Convert `source_root` into `out_root/WEIGHTS_FILE`; returns the content
    digest of the result.

    `expected_digest` (the manifest's) is checked BEFORE anything is published:
    a source that is not the model this repository names writes nothing, and
    leaves an existing file exactly as it was. An existing file with the same
    content is reused without writing; another content needs `force`."""
    import torch

    source_root = pathlib.Path(os.path.abspath(source_root))
    out_root = pathlib.Path(os.path.abspath(out_root))
    h5 = source_root / "weights_best.h5"
    for name in ("config.json", "weights_best.h5"):
        if not (source_root / name).is_file():
            raise ConvertError(f"{source_root} is not the installed StarDist model ({name} missing)")
    config = json.loads((source_root / "config.json").read_text(encoding="utf-8"))
    check_config(config)

    data = {"format": weights.FORMAT, "tensors": from_h5(h5), "source": source_dict(source_root)}
    self_check(data)
    digest = weights.content_digest(data)
    if expected_digest is not None and digest != expected_digest:
        raise ConvertError(f"the weights in {source_root} are not the ones the manifest names "
                           f"(content {digest[:12]}..., expected {expected_digest[:12]}...); "
                           "nothing was written")

    target = out_root / weights.WEIGHTS_FILE
    if target.is_file() and not force:
        try:
            here = weights.content_digest(weights.read(target))
        except (weights.WeightsError, OSError, RuntimeError):
            here = None
        if here == digest:
            return digest
        raise ConvertError(f"{target} already exists with other content; "
                           "run this again with --force to replace it")
    out_root.mkdir(parents=True, exist_ok=True)
    tmp = out_root / f".convert-{os.getpid()}-{secrets.token_hex(4)}.tmp"
    try:
        torch.save(data, tmp)
        stored = weights.read(tmp)                  # what we publish is what we verify:
        self_check(stored)                          # a forward pass from the FILE's data
        if weights.content_digest(stored) != digest:
            raise ConvertError(f"{tmp} does not read back as it was written")
        os.replace(tmp, target)                     # the one commit point
    finally:
        if tmp.exists():
            tmp.unlink()
    print(f"{target}: content {digest}", file=sys.stderr)
    return digest


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--source", required=True, help="the installed StarDist model folder")
    ap.add_argument("--out", required=True, help="where the .pt is written")
    ap.add_argument("--force", action="store_true", help="replace an existing file")
    args = ap.parse_args(argv)
    try:
        convert_dir(args.source, args.out, force=args.force)
    except ConvertError as exc:
        print(f"cannot convert: {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
