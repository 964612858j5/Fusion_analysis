"""What the pre-segmentation method editor may ask for, per method (plan 7.1).

Pure data and functions -- no Qt. One definition for the editor, the plan
store and, later, the execution layer and Step2's loading (plan 7.12, R14).
`SEGMENTATION_METHODS` stays the registry of defaults; this module says which
of those parameters the new UI shows, their ranges and precision, and which
of them may be LISTS (R3).

Ranges and precision follow Step2's own controls (plan 7.1 / 7.8): a value
that Step2's spin boxes would round or clamp is refused here, so the chosen
combination reaches Step2 unchanged. Mesmer's thresholds are the exception
(3 decimals, Step2 carries them through `params`; block C/E).
"""

import hashlib
import itertools
import json
from dataclasses import dataclass

from .segmentation_config import (
    CELLPOSE_NUCLEI_CSD, CELLPOSE_NUCLEI_DAPI, CELLPOSE_NUCLEI_EXPANSION,
    CELLPOSE_NUCLEI_HQ, CELLPOSE_NUCLEI_HQ2, CELLPOSE_WHOLECELL_FUSION,
    MESMER_NUCLEAR_GUIDED, MESMER_NUCLEI, MESMER_WHOLE_CELL, SEGMENTATION_METHODS,
    STARDIST_NUCLEI_DAPI, STARDIST_NUCLEI_EXPANSION,
)

#: The methods the new UI lists, in this order (R2: HQ / HQ2 / CDS are only
#: hidden -- their code and Step2's support stay).
UI_METHODS = [
    CELLPOSE_WHOLECELL_FUSION, CELLPOSE_NUCLEI_DAPI, CELLPOSE_NUCLEI_EXPANSION,
    STARDIST_NUCLEI_DAPI, STARDIST_NUCLEI_EXPANSION,
    MESMER_WHOLE_CELL, MESMER_NUCLEI, MESMER_NUCLEAR_GUIDED,
]
HIDDEN_METHODS = (CELLPOSE_NUCLEI_HQ, CELLPOSE_NUCLEI_HQ2, CELLPOSE_NUCLEI_CSD)
MESMER_METHODS = (MESMER_WHOLE_CELL, MESMER_NUCLEI, MESMER_NUCLEAR_GUIDED)

AUTO = "auto"


@dataclass(frozen=True)
class ParamSpec:
    key: str
    label: str
    kind: str                 # "float" | "int" | "str"
    lo: float = None
    hi: float = None
    decimals: int = 0
    default: object = None    # None means "auto" where `auto` is allowed
    listable: bool = False
    auto: bool = False        # "auto" (None: the library decides) is a value
    note: str = ""

    def describe(self):
        rng = ""
        if self.kind in ("float", "int") and self.lo is not None:
            rng = f"{_fmt(self.lo)}–{_fmt(self.hi)}"
        bits = [b for b in (rng, "auto" if self.auto else "", "list" if self.listable else "") if b]
        return f"{self.label} ({', '.join(bits)})" if bits else self.label


def _fmt(v):
    if v is None:
        return AUTO
    if isinstance(v, float) and v.is_integer():
        return str(int(v)) if abs(v) >= 1 or v == 0 else str(v)
    return str(v)


def _cellpose(expansion):
    specs = [
        ParamSpec("diameter", "diameter", "float", 0, 300, 1, None, True, True,
                  "0 or auto: Cellpose estimates it"),
        ParamSpec("flow_threshold", "flow", "float", 0, 3, 2, 0.4, True),
        ParamSpec("cellprob_threshold", "cellprob", "float", -6, 6, 2, 0.0, True),
        ParamSpec("min_size", "min size", "int", 1, 10000, 0, 15),
    ]
    if expansion:
        # R3 names StarDist's expand as a list, not Cellpose's (P3).
        specs.append(ParamSpec("expand_distance", "expand", "float", 0, 200, 1, 8.0))
    return specs


def _stardist(expansion):
    specs = [
        ParamSpec("prob_thresh", "prob", "float", 0, 1, 2, None, True, True,
                  "auto: the model's own threshold"),
        ParamSpec("nms_thresh", "nms", "float", 0, 1, 2, None, True, True,
                  "auto: the model's own threshold"),
    ]
    if expansion:
        specs.append(ParamSpec("expand_distance", "expand", "float", 0, 200, 1, 8.0, True))
    specs.append(ParamSpec("model_name", "model", "str", default="2D_versatile_fluo"))
    return specs


def _mesmer(method):
    # Plan P2: the lists act on the method's primary output.
    note = ("applies to the nuclei" if method == MESMER_NUCLEI
            else "applies to the cells (the nuclei use DeepCell's default)"
            if method == MESMER_NUCLEAR_GUIDED else "applies to the cells")
    maxima = 0.1 if method == MESMER_NUCLEI else 0.075        # DeepCell's defaults
    return [
        ParamSpec("maxima_threshold", "maxima", "float", 0, 1, 3, maxima, True, note=note),
        ParamSpec("interior_threshold", "interior", "float", 0, 1, 3, 0.2, True, note=note),
        ParamSpec("image_mpp", "image mpp", "float", 0.01, 10, 3, 0.5),
        ParamSpec("postprocess_min_size", "min size", "int", 0, 100000, 0, 0),
    ]


METHOD_PARAMS = {
    CELLPOSE_WHOLECELL_FUSION: _cellpose(False),
    CELLPOSE_NUCLEI_DAPI: _cellpose(False),
    CELLPOSE_NUCLEI_EXPANSION: _cellpose(True),
    STARDIST_NUCLEI_DAPI: _stardist(False),
    STARDIST_NUCLEI_EXPANSION: _stardist(True),
    MESMER_WHOLE_CELL: _mesmer(MESMER_WHOLE_CELL),
    MESMER_NUCLEI: _mesmer(MESMER_NUCLEI),
    MESMER_NUCLEAR_GUIDED: _mesmer(MESMER_NUCLEAR_GUIDED),
}


def display_name(method):
    return SEGMENTATION_METHODS.get(method, {}).get("display_name", method)


def specs(method):
    return list(METHOD_PARAMS[method])


def default_values(method):
    """{key: [value]} -- one value per parameter, the registry's defaults."""
    return {s.key: [s.default] for s in METHOD_PARAMS[method]}


def mesmer_available():
    """Whether this environment can run Mesmer at all (plan F1)."""
    import importlib.util
    try:
        return importlib.util.find_spec("deepcell") is not None
    except (ImportError, ValueError):
        return False


# ── parsing ─────────────────────────────────────────────────────────────────
class ParamError(ValueError):
    pass


def format_values(spec, values):
    return ", ".join(_fmt(v) for v in values)


def parse_values(spec, text):
    """`text` as the list of values `spec` allows. Returns (values, note):
    `note` says what was tidied (a repeated value dropped); a ParamError
    says why the text is refused."""
    raw = [t.strip() for t in str(text or "").split(",")]
    if all(not t for t in raw):
        raise ParamError("enter a value")
    if any(not t for t in raw):
        raise ParamError("an empty value between commas")
    if len(raw) > 1 and not spec.listable:
        raise ParamError("one value only")
    out, dropped = [], 0
    for tok in raw:
        v = _parse_one(spec, tok)
        if v in out:
            dropped += 1
            continue
        out.append(v)
    note = f"{dropped} repeated value{'s' if dropped != 1 else ''} dropped" if dropped else ""
    return out, note


def _parse_one(spec, tok):
    if spec.kind == "str":
        return tok
    if tok.lower() == AUTO:
        if not spec.auto:
            raise ParamError(f"'{tok}' is not allowed here")
        return None
    try:
        v = float(tok)
    except ValueError:
        raise ParamError(f"'{tok}' is not a number") from None
    if not (spec.lo - 1e-12 <= v <= spec.hi + 1e-12):
        raise ParamError(f"{tok} is outside {_fmt(spec.lo)}–{_fmt(spec.hi)}")
    if spec.kind == "int":
        if not float(v).is_integer():
            raise ParamError(f"{tok} is not a whole number")
        return int(v)
    if round(v, spec.decimals) != v and abs(round(v, spec.decimals) - v) > 1e-9:
        raise ParamError(f"{tok} has more than {spec.decimals} decimal"
                         f"{'s' if spec.decimals != 1 else ''}")
    v = round(v, spec.decimals)
    if spec.auto and spec.key == "diameter" and v == 0:
        return None                                  # 0 means auto, as in Step2
    return v


# ── combinations ────────────────────────────────────────────────────────────
def combo_count(method, values):
    n = 1
    for s in METHOD_PARAMS[method]:
        n *= max(1, len(values.get(s.key) or [s.default]))
    return n


def combinations(method, values):
    """Every parameter combination (R3: a cartesian product), as dicts."""
    keys = [s.key for s in METHOD_PARAMS[method]]
    lists = [values.get(k) or [s.default] for k, s in zip(keys, METHOD_PARAMS[method])]
    return [dict(zip(keys, combo)) for combo in itertools.product(*lists)]


def combo_id(method, params):
    """A combination's identity (plan 7.3): method + canonical params."""
    canon = {k: (round(v, 6) if isinstance(v, float) else v) for k, v in sorted(params.items())}
    blob = json.dumps({"method": method, "params": canon}, sort_keys=True)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()[:12]


def summary(method, values):
    parts = []
    for s in METHOD_PARAMS[method]:
        vals = values.get(s.key) or [s.default]
        parts.append(f"{s.label} {format_values(s, vals)}")
    return " · ".join(parts)


# ── same-name merge (R9) ────────────────────────────────────────────────────
def merge(method, old, new):
    """R9: list parameters take the union (old's order, then new values);
    a single-value parameter that differs is a CONFLICT for the user to
    decide. Returns (merged, conflicts={key: (old_value, new_value)}); a
    conflicting key keeps the old value in `merged`. No filtering or
    de-duplication after expansion (R9)."""
    merged, conflicts = {}, {}
    for s in METHOD_PARAMS[method]:
        a = list(old.get(s.key) or [s.default])
        b = list(new.get(s.key) or [s.default])
        if s.listable:
            merged[s.key] = a + [v for v in b if v not in a]
        elif a == b:
            merged[s.key] = a
        else:
            merged[s.key] = a
            conflicts[s.key] = (a[0], b[0])
    return merged, conflicts


def valid_plan_method(method):
    return method in METHOD_PARAMS
