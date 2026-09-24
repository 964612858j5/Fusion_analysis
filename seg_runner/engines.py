"""The three engines as the runner sees them: load once, predict per task.

Each engine applies exactly its one standard preprocessing (plan 7.11.5):
    cellpose  eval(normalize=True), the library default
    stardist  csbdeep normalize(img, 1, 99.8) -- the library itself has none
    mesmer    DeepCell's default preprocessing (no preprocess_kwargs)
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
# The listed Mesmer thresholds and the output they act on (plan P2).
MESMER_THRESHOLDS = ("maxima_threshold", "interior_threshold")
MESMER_THRESHOLD_TARGET = {"mesmer_whole_cell": "whole_cell",
                           "mesmer_nuclear_guided": "whole_cell",
                           "mesmer_nuclei": "nuclear"}

MODELS_MANIFEST = pathlib.Path(__file__).resolve().parent.parent / "envs" / "fusion_mesmer" / "models.json"


class ModelMissingError(RuntimeError):
    """The model the manifest names is not where it says (plan 7.10.3: no
    silent fall back to some other path)."""


def mesmer_model_path(manifest=MODELS_MANIFEST):
    """The Mesmer model directory: the manifest's root, or the directory in
    the manifest's `env_override` variable when that is set. Checked against
    the manifest's file list and sizes; anything missing is an error."""
    try:
        entry = json.loads(pathlib.Path(manifest).read_text())["models"]["mesmer_multiplex_segmentation"]
    except (OSError, ValueError, KeyError) as exc:
        raise ModelMissingError(f"no Mesmer entry in the model manifest {manifest}") from exc
    root = os.environ.get(entry.get("env_override") or "") or entry["root"]
    root = os.path.expanduser(root)
    for f in entry.get("files") or []:
        path = os.path.join(root, f["path"])
        if not os.path.isfile(path):
            raise ModelMissingError(f"Mesmer model file missing: {path} (model manifest {manifest})")
        if f.get("size") is not None and os.path.getsize(path) != int(f["size"]):
            raise ModelMissingError(f"Mesmer model file has the wrong size: {path} "
                                    f"({os.path.getsize(path)} B, the manifest says {f['size']} B)")
    return root


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


def _tf_gpu_count():
    import tensorflow as tf
    gpus = tf.config.list_physical_devices("GPU")
    for g in gpus:
        try:
            tf.config.experimental.set_memory_growth(g, True)
        except Exception:  # noqa: BLE001 -- already initialised
            pass
    return len(gpus)


class CellposeEngine:
    name = "cellpose"

    def __init__(self):
        import torch
        from cellpose import models
        self._model = models.CellposeModel(gpu=torch.cuda.is_available())
        self.device = str(self._model.device)

    def lib_versions(self):
        import cellpose
        import torch
        return {"cellpose": getattr(cellpose, "__version__", "") or _dist_version("cellpose"),
                "torch": torch.__version__}

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


class StardistEngine:
    name = "stardist"

    def __init__(self, model_name="2D_versatile_fluo"):
        os.environ.setdefault("KERAS_BACKEND", "tensorflow")
        n = _tf_gpu_count()
        from stardist.models import StarDist2D
        self._model = StarDist2D.from_pretrained(model_name)
        self.device = "gpu" if n else "cpu"

    def lib_versions(self):
        import stardist
        import tensorflow as tf
        return {"stardist": stardist.__version__, "tensorflow": tf.__version__}

    def predict(self, image, params):
        from csbdeep.utils import normalize
        kwargs = {k: params[k] for k in ("prob_thresh", "nms_thresh") if params.get(k) is not None}
        labels, _ = self._model.predict_instances(normalize(image, 1, 99.8, axis=(0, 1)), **kwargs)
        return labels


class MesmerEngine:
    name = "mesmer"

    def __init__(self):
        n = _tf_gpu_count()
        import tensorflow as tf
        from deepcell.applications import Mesmer
        path = mesmer_model_path()
        self.model_path = path
        self._app = Mesmer(model=tf.keras.models.load_model(path))
        self.device = "gpu" if n else "cpu"

    def lib_versions(self):
        import tensorflow as tf
        return {"deepcell": _dist_version("deepcell"), "tensorflow": tf.__version__}

    def predict(self, image, params, compartment, thresholds=None):
        """`thresholds` go to the post-processing of `compartment` (DeepCell
        fills in its defaults for the rest); no preprocess_kwargs, so its
        default preprocessing runs once (plan 7.11.5)."""
        import numpy as np
        batch = image[None] if image.ndim == 3 else image
        post = {"postprocess_kwargs_whole_cell" if compartment == "whole-cell"
                else "postprocess_kwargs_nuclear": dict(thresholds or {})}
        pred = self._app.predict(batch, image_mpp=float(params.get("image_mpp", 0.5)),
                                 compartment=compartment, batch_size=1, **post)
        return np.squeeze(pred)


def _dist_version(name):
    from importlib import metadata
    try:
        return metadata.version(name)
    except metadata.PackageNotFoundError:
        return ""


def load(engine):
    return {"cellpose": CellposeEngine, "stardist": StardistEngine, "mesmer": MesmerEngine}[engine]()


def run(engine_obj, method, image, params):
    """Masks for one task: {"cell": array|None, "nucleus": array|None}."""
    import numpy as np
    outputs = METHOD_OUTPUTS[method]
    result = {"cell": None, "nucleus": None}
    if engine_obj.name == "mesmer":
        listed = {k: float(params[k]) for k in MESMER_THRESHOLDS if params.get(k) is not None}
        target = MESMER_THRESHOLD_TARGET[method]
        min_size = int(params.get("postprocess_min_size", params.get("min_size", 0)) or 0)
        if "cell" in outputs:
            result["cell"] = postprocess_mask(
                engine_obj.predict(image, params, "whole-cell", listed), min_size)
        if "nucleus" in outputs:
            nuc = engine_obj.predict(image, params, "nuclear",
                                     listed if target == "nuclear" else None)
            # nuclear-guided: the second output, library defaults, not post-processed
            result["nucleus"] = postprocess_mask(nuc, min_size) if "cell" not in outputs else nuc
    elif method in EXPANSION_METHODS:
        nuclei = np.asarray(engine_obj.predict(image, params))
        result["nucleus"] = nuclei
        result["cell"] = expand(nuclei, params.get("expand_distance", 8))
    else:
        result[outputs[0]] = engine_obj.predict(image, params)
    return {k: (None if v is None else np.asarray(v).astype(np.uint32, copy=False))
            for k, v in result.items()}
