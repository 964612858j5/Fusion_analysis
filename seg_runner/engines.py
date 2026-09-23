"""The three engines as the runner sees them: load once, predict per task.

Each engine applies exactly its one standard preprocessing (plan 7.11.5):
    cellpose  eval(normalize=True), the library default
    stardist  csbdeep normalize(img, 1, 99.8) -- the library itself has none
    mesmer    DeepCell's default preprocessing (no preprocess_kwargs)
The input array arrives already built by the application side (plan 7.11.4);
nothing here stretches it again.

Prototype scope (V0): model inference only. Expansion and the other
post-processing steps are placed by the input ownership table and wired in
V1/C.
"""
import os

# Which output each method produces (plan 7.2): "cell", "nucleus" or both.
METHOD_OUTPUTS = {
    "cellpose_wholecell_fusion": ("cell",),
    "cellpose_nuclei_dapi": ("nucleus",),
    "cellpose_nuclei_expansion": ("nucleus",),
    "stardist_nuclei_dapi": ("nucleus",),
    "stardist_nuclei_expansion": ("nucleus",),
    "mesmer_whole_cell": ("cell",),
    "mesmer_nuclei": ("nucleus",),
    "mesmer_nuclear_guided": ("cell", "nucleus"),
}
METHOD_ENGINE = {m: m.split("_", 1)[0] for m in METHOD_OUTPUTS}

MESMER_MODEL_DEFAULT = "/sda1/Fusion/benchmark/spacec/models/Mesmer_model/MultiplexSegmentation"


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
        path = os.environ.get("DEEPCELL_MESMER_MODEL_PATH") or MESMER_MODEL_DEFAULT
        self._app = Mesmer(model=tf.keras.models.load_model(path))
        self.device = "gpu" if n else "cpu"

    def lib_versions(self):
        import tensorflow as tf
        return {"deepcell": _dist_version("deepcell"), "tensorflow": tf.__version__}

    def predict(self, image, params, compartment):
        import numpy as np
        batch = image[None] if image.ndim == 3 else image
        pred = self._app.predict(batch, image_mpp=float(params.get("image_mpp", 0.5)),
                                 compartment=compartment, batch_size=1)
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
        if "cell" in outputs:
            result["cell"] = engine_obj.predict(image, params, "whole-cell")
        if "nucleus" in outputs:
            result["nucleus"] = engine_obj.predict(image, params, "nuclear")
    else:
        result[outputs[0]] = engine_obj.predict(image, params)
    return {k: (None if v is None else np.asarray(v).astype(np.uint32, copy=False))
            for k, v in result.items()}
