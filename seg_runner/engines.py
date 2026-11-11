"""The three engines as the runner sees them: load once, predict per task.

Each engine applies exactly its one standard preprocessing (plan 7.11.5):
    cellpose  eval(normalize=True), the library default
    stardist  csbdeep normalize(img, 1, 99.8) -- the library itself has none
    mesmer    DeepCell's default preprocessing, run by `mesmer_torch` (the
              Mesmer network on PyTorch; docs/mesmer_torch_plan.md)
The input array arrives already built by the application side (plan 7.11.4);
nothing here stretches it again.

Engine-side post-processing (plan 7.10.8 ownership table), each done here
and only here:
    expansion   `expand_labels(nuclei, expand_distance)` -> the cell mask;
                the nuclei BEFORE expansion are returned as the nucleus mask
    mesmer      the listed thresholds reach DeepCell's post-processing of the
                method's primary output (whole-cell, or nuclear for Mesmer
                nuclei); nuclear-guided's second, nuclear output keeps the
                library defaults. Then `postprocess_mask` (min size) on the
                primary output only, as the Step1 worker does.
Ownership (keeping the patch centre of a HALO window) is the application's.
"""
import json
import os
import pathlib

# Which output each method produces (plan 7.2): "cell", "nucleus" or both.
METHOD_OUTPUTS = {
    "cellpose_wholecell_fusion": ("cell",),
    "cellpose_wholecell_nuclei": ("cell", "nucleus"),
    "hybrid_cellpose_stardist": ("cell", "nucleus"),
    "cellpose_nuclei_dapi": ("nucleus",),
    "cellpose_nuclei_expansion": ("cell", "nucleus"),
    "stardist_nuclei_dapi": ("nucleus",),
    "stardist_nuclei_expansion": ("cell", "nucleus"),
    "mesmer_whole_cell": ("cell",),
    "mesmer_nuclei": ("nucleus",),
    "mesmer_nuclear_guided": ("cell", "nucleus"),
}
METHOD_ENGINE = {m: m.split("_", 1)[0] for m in METHOD_OUTPUTS}
EXPANSION_METHODS = ("cellpose_nuclei_expansion", "stardist_nuclei_expansion")
#: Cells and nuclei from two passes of ONE model on one input
#: (docs/v17_cellpose_cell_nucleus_plan.md): the nucleus pass reads the
#: input's last channel -- the nucleus channel -- with its own parameters.
#: Returned RAW; the pairing is the application's (core/nuclei_pairing.py).
CELL_AND_NUCLEUS_METHODS = ("cellpose_wholecell_nuclei", "hybrid_cellpose_stardist")
#: docs/v17_hybrid_cellpose_stardist_plan.md: cells from the Cellpose part,
#: nuclei from the StarDist part of ONE "hybrid" engine process
HYBRID_METHODS = ("hybrid_cellpose_stardist",)
#: the engines a composite engine is made of (its manifest models, provenance)
ENGINE_PARTS = {"hybrid": ("cellpose", "stardist")}
#: the registry's nucleus defaults (utils/segmentation_config.py), here so
#: the engine process imports nothing of the application
NUCLEUS_PASS_DEFAULTS = {"diameter": None, "flow_threshold": 0.4,
                         "cellprob_threshold": 0.0, "min_size": 15}


def nucleus_pass_params(params):
    """The nucleus pass's parameters: `nucleus_<key>` as `<key>`."""
    return {k: params.get(f"nucleus_{k}", d) for k, d in NUCLEUS_PASS_DEFAULTS.items()}
# The listed Mesmer thresholds and the output they act on (plan P2).
MESMER_THRESHOLDS = ("maxima_threshold", "interior_threshold")
MESMER_THRESHOLD_TARGET = {"mesmer_whole_cell": "whole_cell",
                           "mesmer_nuclear_guided": "whole_cell",
                           "mesmer_nuclei": "nuclear"}

MODELS_MANIFEST = pathlib.Path(__file__).resolve().parent.parent / "envs" / "fusion_mesmer" / "models.json"

# ── compatibility identity (block A0.5) ──────────────────────────────────
# What decides whether a Step2 run is "the same segmentation" as the Step1
# result it was handed. Deliberately small: environment hashes, library
# versions and code hashes are PROVENANCE (recorded, never compared) -- see
# `seg_runner.runner.engine_provenance`. Model file integrity is checked at
# deployment by `scripts/model_manifest.py --verify`, not at every start
# (the Mesmer torch weights are also checked by content at every load).
IDENTITY_VERSION = 2
#: Bump an engine's number ONLY for a change that can alter its segmentation
#: output: normalisation, threshold semantics, the expansion algorithm, the
#: input-channel arrangement, post-processing, another default model. Adding
#: a package, logging, the protocol or the UI never bumps it.
#: mesmer 2 (user ruling 2026-10-25): the network runs on PyTorch (GPU first,
#: CPU otherwise) instead of TensorFlow; results differ from version 1 at the
#: level of floating-point rounding, so they are another behaviour.
#: stardist 2 (user ruling 2026-11-10): the same -- the network runs on PyTorch
#: (docs/stardist_torch_plan.md); only the inference implementation changed.
#: hybrid 1 (2026-11-10): the Cellpose and StarDist parts as their own engines
#: run them; bump it whenever either part's output changes.
BEHAVIOR_VERSION = {"cellpose": 1, "stardist": 2, "mesmer": 2, "hybrid": 1}
#: The LOGICAL model of each engine (its `models.json` key). Mesmer's is the
#: Keras model; the file it loads is that model's torch conversion, stored as
#: `MESMER_TORCH_ENTRY` -- the same weights, so the same model identity.
MODEL_ID = {"cellpose": "cellpose_cpsam", "stardist": "stardist_2D_versatile_fluo",
            "mesmer": "mesmer_multiplex_segmentation",
            "hybrid": "cellpose_cpsam+stardist_2D_versatile_fluo"}
MESMER_TORCH_ENTRY = "mesmer_multiplex_segmentation_torch"
#: StarDist's torch conversion of `stardist_2D_versatile_fluo`'s own
#: `weights_best.h5` (docs/stardist_torch_plan.md §4): the same weights, so
#: `MODEL_ID["stardist"]` stays the logical model.
STARDIST_TORCH_ENTRY = "stardist_2D_versatile_fluo_torch"
#: Compared between a Step1 result and the engine Step2 runs.
COMPARED_FIELDS = ("behavior_version", "model_id")


def behavior_identity(engine):
    """The compatibility identity of `engine` as this code would run it. Pure:
    no engine library is imported, so the GUI can ask before a run."""
    return {"version": IDENTITY_VERSION, "engine": engine,
            "behavior_version": BEHAVIOR_VERSION[engine], "model_id": MODEL_ID[engine]}


def compare_identity(step1, current):
    """How the engine about to run differs from the one a Step1 result ran on.

    Returns {"engine_differs", "legacy_identity", "differences"}:
    `differences` is a list of [field, step1_value, current_value]. A Step1
    identity from before version 2 (it carried `lock_hash` and no
    `behavior_version`) is LEGACY: only the engine kind is checked, because
    its other fields mean something else.
    """
    step1, current = dict(step1 or {}), dict(current or {})
    legacy = int(step1.get("version") or 0) < IDENTITY_VERSION
    differences = [] if legacy else [
        [k, step1.get(k), current.get(k)] for k in COMPARED_FIELDS if step1.get(k) != current.get(k)]
    return {"engine_differs": step1.get("engine") != current.get("engine"),
            "legacy_identity": legacy, "differences": differences}


class ModelMissingError(RuntimeError):
    """The model the manifest names is not where it says (plan 7.10.3: no
    silent fall back to some other path)."""


def model_root(entry):
    """The directory a manifest entry's files are in: the directory named by
    its `env_override` variable when that is set, else its `root`. THE one
    path rule -- the converter, `scripts/model_manifest.py --write/--verify`
    and the engines all resolve a model directory through it."""
    return os.path.expanduser(os.environ.get(entry.get("env_override") or "") or entry["root"])


def manifest_entry(key, manifest=MODELS_MANIFEST):
    """Entry `key` of the model manifest; a missing one is an error."""
    try:
        return json.loads(pathlib.Path(manifest).read_text())["models"][key]
    except (OSError, ValueError, KeyError) as exc:
        raise ModelMissingError(f"no {key} entry in the model manifest {manifest}") from exc


def model_path(key, manifest=MODELS_MANIFEST):
    """The directory of manifest entry `key`, checked against the manifest's
    file list and sizes (a `generated` entry lists no sizes: its file is
    checked by content when loaded); anything missing is an error."""
    entry = manifest_entry(key, manifest)
    root = model_root(entry)
    for f in entry.get("files") or []:
        path = os.path.join(root, f["path"])
        if not os.path.isfile(path):
            raise ModelMissingError(f"model file missing: {path} ({key}, model manifest {manifest})")
        if f.get("size") is not None and os.path.getsize(path) != int(f["size"]):
            raise ModelMissingError(f"model file has the wrong size: {path} "
                                    f"({os.path.getsize(path)} B, the manifest says {f['size']} B)")
    return root


def mesmer_model_path(manifest=MODELS_MANIFEST):
    """The Keras Mesmer model directory (the converter's source and the TF
    reference of the tests; the engine runs the torch conversion)."""
    return model_path("mesmer_multiplex_segmentation", manifest)


def postprocess_mask(mask, min_size=0):
    """Remove objects smaller than `min_size` pixels -- the part of Step1's
    `utils.mesmer_utils.postprocess_mask` the new flow uses (fill_holes and
    remove_border_objects stay off there too). A copy, because the runner
    imports nothing from the application; `tests/test_seg_runner_engines.py`
    holds the two equal until Step2 moves onto this runner."""
    import numpy as np
    mask = np.asarray(mask, dtype=np.uint32)
    if min_size and int(min_size) > 0:
        from skimage.morphology import remove_small_objects
        mask = remove_small_objects(mask, min_size=int(min_size)).astype(np.uint32)
    return mask.astype(np.uint32, copy=False)


def expand(nuclei, distance):
    """The cell mask of an expansion method: labels grown by `distance`, ids
    kept, so each cell has its nucleus's id. Distance 0 is no growth."""
    import numpy as np
    dist = float(distance or 0)
    if dist <= 0:
        return np.array(nuclei, copy=True)
    from skimage.segmentation import expand_labels
    return expand_labels(nuclei, distance=dist)


#: set by `client.EngineProcess` when Step2's "Use GPU" is off: hiding CUDA
#: (CUDA_VISIBLE_DEVICES) does not hide the Mac's GPU (MPS)
CPU_ONLY_ENV = "BLOCK01_ENGINE_CPU_ONLY"


def torch_device():
    """The torch engines' device (docs/v17_cellpose_mac_gpu_plan.md):
    "cuda" when CUDA works, else "mps" (Apple silicon) when available, else
    "cpu"; "cpu" whenever the GPU is not wanted."""
    import torch
    if os.environ.get(CPU_ONLY_ENV) == "1":
        return "cpu"
    if torch.cuda.is_available():
        return "cuda"
    mps = getattr(torch.backends, "mps", None)
    if mps is not None and mps.is_available():
        return "mps"
    return "cpu"


def cellpose_model_args():
    """`models.CellposeModel` keyword arguments for `torch_device()`. On CUDA
    exactly the call made before (`gpu=True`, Cellpose's own device check);
    on the Mac's GPU the device named explicitly (it overrides `gpu`)."""
    import torch
    dev = torch_device()
    if dev == "cuda":
        return {"gpu": True}
    if dev == "mps":
        return {"gpu": True, "device": torch.device("mps")}
    return {"gpu": False}


def _hide_tensorflow_gpu():
    """Import TensorFlow and take its GPUs away BEFORE the Keras model is
    built: csbdeep computes at construction (`csbdeep/models/base_model.py:162-167`),
    so hiding afterwards would be too late. Returns what TF saw, for the
    provenance; raises when the isolation cannot be VERIFIED -- a log line is
    not proof (docs/stardist_torch_plan.md §6.5)."""
    import tensorflow as tf
    before = [d.name for d in tf.config.list_physical_devices("GPU")]
    if before:
        try:
            tf.config.set_visible_devices([], "GPU")
        except RuntimeError as exc:     # its GPUs are already initialised
            raise RuntimeError(
                "TensorFlow initialised its GPU before it could be hidden; the StarDist "
                f"engine refuses to share it with PyTorch (devices {before})") from exc
    after = [d.name for d in tf.config.list_logical_devices("GPU")]
    if after:
        raise RuntimeError(f"TensorFlow still sees GPU devices {after} after hiding them")
    return {"physical_before": before, "logical_after": after}


class CellposeEngine:
    name = "cellpose"

    def __init__(self):
        from cellpose import models
        self._model = models.CellposeModel(**cellpose_model_args())
        self.device = str(self._model.device)

    def lib_versions(self):
        import cellpose
        import torch
        return {"cellpose": getattr(cellpose, "__version__", "") or _dist_version("cellpose"),
                "torch": torch.__version__, **_common_lib_versions()}

    def model_resolved_path(self):
        path = getattr(self._model, "pretrained_model", None)
        if isinstance(path, (list, tuple)):
            path = path[0] if path else None
        return str(path) if path else None

    def predict(self, image, params):
        kwargs = {
            "diameter": params.get("diameter"),
            "flow_threshold": params.get("flow_threshold", 0.4),
            "cellprob_threshold": params.get("cellprob_threshold", 0.0),
            "min_size": int(params.get("min_size", 15)),
        }
        if image.ndim == 3:
            kwargs["channel_axis"] = -1
        # Cellpose 4.1.1 normalises a multi-channel input IN PLACE (measured:
        # up to 0.04 on [0,1] data), so the caller's array must not be the one
        # handed over -- reusing it would segment a different image next time.
        masks, _, _ = self._model.eval(image.copy(), **kwargs)
        return masks


#: the directory of an installed StarDist model (config.json,
#: thresholds.json, weights_best.h5); unset -> `from_pretrained` (~/.keras)
STARDIST_DIR_ENV = "BLOCK01_STARDIST_MODEL_PATH"


class StardistEngine:
    """StarDist on PyTorch (docs/stardist_torch_plan.md).

    The stardist package still runs the model: `StarDist2D` holds the config,
    the thresholds and the prediction loop; only its Keras network is replaced
    by `stardist_torch.network.TorchNetwork`, which runs the converted
    `weights_best.h5`. Normalization, padding, the sparse NMS and the polygon
    reconstruction are the package's own code.

    Device from `torch_device()` (CUDA, else the Mac's GPU, else CPU -- and CPU
    whenever `BLOCK01_ENGINE_CPU_ONLY=1`); numerics as in
    `stardist_torch/numerics.py`. TensorFlow's GPUs are hidden before the Keras
    model is built, because that construction computes (§6.5)."""

    name = "stardist"

    def __init__(self, model_name="2D_versatile_fluo"):
        from .stardist_torch import network, numerics, weights
        os.environ.setdefault("KERAS_BACKEND", "tensorflow")
        # BEFORE the Keras model exists: it computes at construction (§6.5)
        self._tf_gpu = _hide_tensorflow_gpu()
        dev = torch_device()
        entry = manifest_entry(STARDIST_TORCH_ENTRY)
        path = os.path.join(model_path(STARDIST_TORCH_ENTRY), weights.WEIGHTS_FILE)
        params = weights.load(path, dev, entry.get("content_sha256"))
        from stardist.models import StarDist2D
        folder = os.environ.get(STARDIST_DIR_ENV)
        if folder:
            # an installed model folder (docs/v17_install_plan.md §2.5): the
            # instance `from_pretrained` builds, without its download / unzip
            folder = os.path.abspath(os.path.expanduser(folder))
            model = StarDist2D(None, name=os.path.basename(folder),
                               basedir=os.path.dirname(folder))
        else:
            model = StarDist2D.from_pretrained(model_name)
        model.keras_model = network.TorchNetwork(params, dev)     # the one swap
        self._model = model
        self.model_path = path
        self._numerics = dict(numerics.NUMERICS)
        self._artifact = {"entry": STARDIST_TORCH_ENTRY, "sha256": _file_sha256(path),
                          "content_sha256": params["content_sha256"],
                          "source": params.get("source") or {}}
        self.device = {"cuda": "gpu"}.get(dev, dev)

    def lib_versions(self):
        import stardist
        import tensorflow as tf
        import torch
        return {"stardist": stardist.__version__, "tensorflow": tf.__version__,
                "torch": torch.__version__, "cuda": torch.version.cuda or "",
                "cudnn": str(torch.backends.cudnn.version() or ""),
                "csbdeep": _dist_version("csbdeep"),
                "stardist_backend": "torch", "stardist_numerics": dict(self._numerics),
                "tf_gpu_physical": len(self._tf_gpu["physical_before"]),
                "tf_gpu_hidden": not self._tf_gpu["logical_after"],
                **_common_lib_versions()}

    def model_resolved_path(self):
        return str(self.model_path) if self.model_path else None

    def model_artifact(self):
        """The weights file this process runs and where it came from: its
        content digest (checked against the manifest at load), its file sha256
        (may differ between machines: recorded, never compared) and the
        conversion source."""
        return dict(self._artifact)

    def predict(self, image, params):
        import numpy as np
        kwargs = {k: params[k] for k in ("prob_thresh", "nms_thresh") if params.get(k) is not None}
        x = stardist_input(image)
        if x is None:                     # nothing to normalise: no nucleus, no model call
            return np.zeros(np.shape(image)[:2], np.int32)
        labels, _ = self._model.predict_instances(x, **kwargs)
        return labels


#: plan §15.4: percentile 99.8 - percentile 1 at or below this is "no contrast"
NEAR_BLANK = 1e-6


def stardist_input(image):
    """StarDist's input: csbdeep's `normalize(image, 1, 99.8, axis=(0, 1))`,
    computed the same way (the same percentiles, `normalize_mi_ma`). Only on a
    window with no contrast at those percentiles -- a near-blank tile, where
    the division by ~1e-20 sends the distances to ~1e19 and StarDist's
    polygon NMS (ClipperLib) aborts the process (plan §15.4) -- the
    percentiles are taken over the non-zero pixels instead; None when there
    is still no contrast (nothing to segment)."""
    import numpy as np
    from csbdeep.utils import normalize_mi_ma
    mi = np.percentile(image, 1, axis=(0, 1), keepdims=True)
    ma = np.percentile(image, 99.8, axis=(0, 1), keepdims=True)
    if np.all(ma - mi > NEAR_BLANK):
        return normalize_mi_ma(image, mi, ma)
    values = np.asarray(image)[np.asarray(image) > 0]
    if values.size == 0:
        return None
    mi, ma = np.percentile(values, 1), np.percentile(values, 99.8)
    if ma - mi <= NEAR_BLANK:
        return None
    import sys
    print(f"[stardist] near-blank window: normalised over its {values.size} non-zero pixels",
          file=sys.stderr, flush=True)
    return normalize_mi_ma(image, mi, ma)


def _device_form(dev):
    """One spelling of a torch device for the records: `gpu` (CUDA), `mps`,
    `cpu` -- as StarDist and Mesmer report it."""
    dev = str(dev)
    return "gpu" if dev.startswith("cuda") else dev


class HybridEngine:
    """Cellpose whole cells + StarDist nuclei in ONE engine process
    (docs/v17_hybrid_cellpose_stardist_plan.md): one torch device, one CUDA
    context. StarDist is built first -- it hides TensorFlow's GPUs before its
    Keras model exists; Cellpose imports no TensorFlow. Both parts must sit on
    the same device, else the engine does not start."""

    name = "hybrid"

    def __init__(self):
        self.stardist = StardistEngine()
        self.cellpose = CellposeEngine()
        a, b = _device_form(self.cellpose.device), _device_form(self.stardist.device)
        if a != b:
            raise RuntimeError(f"the hybrid engine's parts are on different devices: "
                               f"Cellpose {a}, StarDist {b}")
        self.device = a

    def lib_versions(self):
        return {**self.stardist.lib_versions(), **self.cellpose.lib_versions()}

    def model_resolved_path(self):
        return (f"cellpose: {self.cellpose.model_resolved_path()}; "
                f"stardist: {self.stardist.model_resolved_path()}")

    def model_artifact(self):
        return self.stardist.model_artifact()


class MesmerEngine:
    """Mesmer on PyTorch: the converted MultiplexSegmentation weights through
    `mesmer_torch.app` -- on `torch_device()` (CUDA, else the Mac's GPU,
    else CPU); batch 1, float32,
    deterministic cuDNN and no TF32 during the network pass
    (`app.NUMERICS`), so a rerun on the same device gives the same masks."""
    name = "mesmer"

    def __init__(self):
        import sys
        from .mesmer_torch import app, prepare, weights
        dev = torch_device()
        entry = manifest_entry(MESMER_TORCH_ENTRY)
        root = model_root(entry)
        # first use on this machine: made from the Keras model (plan §10)
        prepare.ensure(root, model_root(manifest_entry(entry["derived_from"])),
                       entry.get("content_sha256"), lambda m: print(m, file=sys.stderr, flush=True))
        path = os.path.join(model_path(MESMER_TORCH_ENTRY), weights.WEIGHTS_FILE)
        self.model_path = path
        params = weights.load(path, dev, entry.get("content_sha256"))
        self._artifact = {"entry": MESMER_TORCH_ENTRY, "sha256": _file_sha256(path),
                          "content_sha256": params["content_sha256"],
                          "source": params.get("source") or {}}
        self._app = app.MesmerTorch(params, dev)
        self._numerics = app.NUMERICS
        self.device = {"cuda": "gpu"}.get(dev, dev)       # "gpu" on CUDA, as before

    def lib_versions(self):
        import torch
        return {"torch": torch.__version__, "cuda": torch.version.cuda or "",
                "cudnn": str(torch.backends.cudnn.version() or ""),
                "deepcell_toolbox": _dist_version("deepcell-toolbox"),
                "mesmer_backend": "torch", "mesmer_numerics": dict(self._numerics),
                **_common_lib_versions()}

    def model_resolved_path(self):
        return str(self.model_path) if self.model_path else None

    def model_artifact(self):
        """The weights file this process runs and where it came from: its
        content digest (checked against the manifest at load), its file
        sha256 (may differ between machines: recorded, never compared) and
        the conversion source (model key, the Keras files' hashes, versions)."""
        return dict(self._artifact)

    def predict(self, image, params, compartments, kwargs=None):
        """{compartment: mask} for `image` (H, W, 2); `kwargs` maps a
        compartment to the post-processing values that replace DeepCell's
        defaults (no preprocess arguments: its default preprocessing runs
        once, plan 7.11.5). One network pass serves every compartment."""
        import numpy as np
        batch = image[None] if image.ndim == 3 else image
        out = self._app.predict(batch, image_mpp=float(params.get("image_mpp", 0.5)),
                                compartments=tuple(compartments), kwargs=dict(kwargs or {}))
        return {c: np.squeeze(m) for c, m in out.items()}


def _file_sha256(path, chunk=1 << 20):
    import hashlib
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for block in iter(lambda: f.read(chunk), b""):
            h.update(block)
    return h.hexdigest()


def _common_lib_versions():
    """Libraries every engine's pre- / post-processing touches (provenance)."""
    return {name: _dist_version(name) for name in ("numpy", "scipy", "scikit-image")}


def _dist_version(name):
    from importlib import metadata
    try:
        return metadata.version(name)
    except metadata.PackageNotFoundError:
        return ""


def load(engine):
    return {"cellpose": CellposeEngine, "stardist": StardistEngine, "mesmer": MesmerEngine,
            "hybrid": HybridEngine}[engine]()


def run(engine_obj, method, image, params):
    """Masks for one task: {"cell": array|None, "nucleus": array|None}."""
    import numpy as np
    outputs = METHOD_OUTPUTS[method]
    result = {"cell": None, "nucleus": None}
    if engine_obj.name == "mesmer":
        listed = {k: float(params[k]) for k in MESMER_THRESHOLDS if params.get(k) is not None}
        target = "whole-cell" if MESMER_THRESHOLD_TARGET[method] == "whole_cell" else "nuclear"
        min_size = int(params.get("postprocess_min_size", params.get("min_size", 0)) or 0)
        wanted = [c for c, kind in (("whole-cell", "cell"), ("nuclear", "nucleus")) if kind in outputs]
        # the listed thresholds reach the primary output's post-processing only
        masks = engine_obj.predict(image, params, wanted, {target: listed})
        if "cell" in outputs:
            result["cell"] = postprocess_mask(masks["whole-cell"], min_size)
        if "nucleus" in outputs:
            nuc = masks["nuclear"]
            # nuclear-guided: the second output, library defaults, not post-processed
            result["nucleus"] = postprocess_mask(nuc, min_size) if "cell" not in outputs else nuc
    elif method in HYBRID_METHODS:
        # (before the generic two-pass branch, which feeds Cellpose's
        # nucleus-pass parameters) cells as whole-cell, nuclei as StarDist
        # nuclei-DAPI on the input's last channel
        image = np.asarray(image)
        result["cell"] = engine_obj.cellpose.predict(image, params)
        result["nucleus"] = engine_obj.stardist.predict(
            np.ascontiguousarray(image[..., -1]), params)
    elif method in CELL_AND_NUCLEUS_METHODS:
        image = np.asarray(image)
        result["cell"] = engine_obj.predict(image, params)
        result["nucleus"] = engine_obj.predict(np.ascontiguousarray(image[..., -1]),
                                               nucleus_pass_params(params))
    elif method in EXPANSION_METHODS:
        nuclei = np.asarray(engine_obj.predict(image, params))
        result["nucleus"] = nuclei
        result["cell"] = expand(nuclei, params.get("expand_distance", 8))
    else:
        result[outputs[0]] = engine_obj.predict(image, params)
    return {k: (None if v is None else np.asarray(v).astype(np.uint32, copy=False))
            for k, v in result.items()}
