"""v16_5 R3b: what the Rust core reads of a Step3 mask.

`describe` turns a `core.step3_masks.MaskSource` into what the core reads:
the level-0 mask and the label pyramid's levels on disk, each with its
`.zarray` as written. A pyramid that lives only in memory (the run folder
could not be written) or a layout the core does not take is refused with
the reason -- the viewer's masks then stay with Python (`Step3LabelBinding`,
R3b plan §2.6).
"""

import json
import os
from typing import Any, Dict, Optional


class CoreLabelsRefused(ValueError):
    """The core does not read this mask; the reason says why."""


def _array(path: str) -> Dict[str, Any]:
    try:
        with open(os.path.join(path, ".zarray"), encoding="utf-8") as fh:
            meta = json.load(fh)
    except (OSError, ValueError) as exc:
        raise CoreLabelsRefused(f"{path}: no readable .zarray ({exc})") from exc
    comp = meta.get("compressor") or {}
    fill = meta.get("fill_value", 0)
    if fill is None:
        fill = 0
    if not isinstance(fill, int) or isinstance(fill, bool) or not 0 <= fill < 2 ** 32:
        raise CoreLabelsRefused(f"{path}: fill value {fill!r} is not a uint32")
    shape, chunks = meta.get("shape") or [], meta.get("chunks") or []
    if len(shape) != 2 or len(chunks) != 2:
        raise CoreLabelsRefused(f"{path}: not a 2-D array")
    return {"path": path, "shape": [int(v) for v in shape], "chunks": [int(v) for v in chunks],
            "dtype": str(meta.get("dtype")), "order": str(meta.get("order", "C")),
            "compressor": str(comp.get("id", "")), "cname": str(comp.get("cname", "")),
            "shuffle": int(comp.get("shuffle", 0) or 0), "filters_none": not meta.get("filters"),
            "fill": int(fill), "separator": str(meta.get("dimension_separator", "."))}


def describe(source) -> Optional[Dict[str, Any]]:
    """The core's description of `source` (None for None)."""
    if source is None:
        return None
    pyramid = None
    if source.pyramid is not None:
        if getattr(source.pyramid, "where", "disk") != "disk":
            raise CoreLabelsRefused("the zoomed-out levels exist only in memory")
        pyramid = []
        for lv in source.pyramid.attrs.get("levels") or []:
            level = int(lv["level"])
            path = os.path.join(source.pyramid_path, str(lv.get("path") or level))
            pyramid.append({"level": level, "origin": [int(v) for v in lv["origin"]], "array": _array(path)})
    return {"level0": _array(source.mask_path), "origin0": [int(source.bbox[0]), int(source.bbox[2])],
            "pyramid": pyramid}


def check(core_module, source) -> Optional[str]:
    """Why the core would refuse `source` (None: it reads it). The arrays'
    layout is checked by the core itself (`zarr_v2::U32Array`)."""
    try:
        spec = describe(source)
    except CoreLabelsRefused as exc:
        return str(exc)
    if spec is None:
        return None
    try:
        for array in [spec["level0"]] + [lv["array"] for lv in spec["pyramid"] or []]:
            core_module.check_label_array(array)
    except ValueError as exc:
        return str(exc)
    return None
