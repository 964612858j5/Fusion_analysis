"""The contract a chosen pre-segmentation result hands to Step2.

Step2 hook-up, step 2. No Qt. Save (Step1) writes the block under
`CONTRACT_KEY` at the top level of the segmentation-params file; Step2 reads
it back and checks it. The block carries everything that decides "the same
result": the method and the combination's parameters exactly as the engine
received them in Step1, the HALO, the pixels (`pixel_key`), the Fusion
settings, which run and combination, and the engine identity OF THAT RUN.

A file is new-format only by its version number (`CONTRACT_KEY` present);
a file without the key is an old file and keeps the old path (user ruling,
2026-09-25). A new-format file with an unknown version or a missing field
is an error, never a silent fall-back.
"""

import copy
import math

from . import preseg_run

CONTRACT_KEY = "preseg_contract"
VERSION = 1
REQUIRED = ("version", "method", "params", "halo_px", "pixel_key", "fusion_settings_hash",
            "preseg_run_id", "combo_id", "engine_identity")


class ContractError(ValueError):
    """A contract that cannot be written or trusted; the message says why."""


def fixed_rules(method):
    """Settings a method always runs with, whatever the combination: Mesmer
    uses DeepCell's own preprocessing only (no extra normalisation), and its
    listed thresholds act on one output. The engine applies both by
    construction (`seg_runner/engines.py`, MESMER_THRESHOLD_TARGET); they are
    written down so the file, Step2's check and the engine message all say
    the same. Empty for other methods."""
    if not method.startswith("mesmer_"):
        return {}
    return {"normalize_input": False,
            "threshold_target": "nuclear" if method == "mesmer_nuclei" else "whole_cell"}


def build(run, combo_id, records):
    """The block for `combo_id` of `run`, from the run's records.

    The engine identity comes from the combination's successful records, which
    must all agree with each other and with `run["engines"]`; it is never the
    identity of the environment doing the saving.
    """
    combo = next((c for c in run.get("combos") or [] if c.get("combo_id") == combo_id), None)
    if combo is None:
        raise ContractError(f"combination {combo_id} is not in run {run.get('run_id')}")
    method = combo["method"]
    ok = [r for r in (records or {}).values()
          if r.get("combo_id") == combo_id and r.get("status") == preseg_run.OK]
    if not ok:
        raise ContractError("no patch of this combination succeeded")
    for rec in ok:
        if rec.get("params") != combo["params"] or rec.get("method") != method:
            raise ContractError("a patch record does not match the combination's parameters")
    ids = [rec.get("engine_identity") for rec in ok]
    if any(not isinstance(i, dict) or not i for i in ids):
        raise ContractError("the engine identity is missing from a patch record")
    if any(i != ids[0] for i in ids[1:]):
        raise ContractError("the patches of this combination ran on different engines")
    engine = preseg_run.ps_engine(method)
    run_identity = (run.get("engines") or {}).get(engine)
    if not isinstance(run_identity, dict) or not run_identity:
        raise ContractError(f"the run does not record its {engine} engine")
    if run_identity != ids[0]:
        raise ContractError("the patch records and the run disagree on the engine")
    rules = fixed_rules(method)
    block = {
        "version": VERSION,
        "method": method,
        # the combination's parameters plus the method's fixed rules
        "params": dict(copy.deepcopy(combo["params"]), **rules),
        "fixed_rules": sorted(rules),
        "halo_px": run.get("halo_px"),
        "pixel_key": (run.get("source") or {}).get("pixel_key"),
        "fusion_settings_hash": (run.get("fusion") or {}).get("hash"),
        "preseg_run_id": run.get("run_id"),
        "combo_id": combo_id,
        "engine_identity": copy.deepcopy(ids[0]),
    }
    missing = [k for k in REQUIRED if block.get(k) in (None, "")]
    if missing:
        raise ContractError(f"the run does not record: {', '.join(missing)}")
    return block


def validate(cfg):
    """The block of `cfg`, checked; None for an old-format file (no block)."""
    if not isinstance(cfg, dict) or CONTRACT_KEY not in cfg:
        return None
    block = cfg[CONTRACT_KEY]
    if not isinstance(block, dict):
        raise ContractError("the pre-segmentation contract is not a JSON object")
    if block.get("version") != VERSION:
        raise ContractError(f"unknown pre-segmentation contract version {block.get('version')!r}")
    missing = [k for k in REQUIRED if block.get(k) in (None, "")]
    if missing:
        raise ContractError(f"the pre-segmentation contract lacks: {', '.join(missing)}")
    if not isinstance(block["params"], dict):
        raise ContractError("the contract's parameters are not a JSON object")
    rules = fixed_rules(block["method"])
    if any(block["params"].get(k, object()) != v for k, v in rules.items()):
        raise ContractError("the contract's parameters lack the method's fixed rules")
    ident = block["engine_identity"]
    if not isinstance(ident, dict) or ident.get("engine") != preseg_run.ps_engine(block["method"]):
        raise ContractError("the contract's engine identity does not fit its method")
    if cfg.get("method") and cfg.get("method") != block["method"]:
        raise ContractError(f"the file's method {cfg.get('method')!r} is not the contract's "
                            f"{block['method']!r}")
    return block


def runner_params(cfg):
    """What the engine receives: the combination's parameters, the method's
    fixed rules and the method. Step1's run sent the same minus the fixed
    rules, which the engine applies by construction anyway."""
    block = validate(cfg)
    if block is None:
        raise ContractError("not a pre-segmentation contract")
    return dict(copy.deepcopy(block["params"]), method=block["method"])


def _same(key, a, b):
    if key == "diameter" and a in (None, 0) and b in (None, 0):
        return True                               # 0 and auto both let Cellpose estimate
    if isinstance(a, bool) or isinstance(b, bool) or a is None or b is None:
        return a == b
    if isinstance(a, (int, float)) and isinstance(b, (int, float)):
        return math.isclose(float(a), float(b), rel_tol=0.0, abs_tol=1e-9)
    return a == b


def mismatches(block, effective_params):
    """The contract parameters that `effective_params` (the normalised Step2
    config the worker reads) does not carry unchanged: [(key, want, got)]."""
    out = []
    for key, want in block["params"].items():
        got = effective_params.get(key)
        if not _same(key, want, got):
            out.append((key, want, got))
    return out
