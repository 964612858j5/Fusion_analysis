"""The canonical hash of a Step1 configuration -- one implementation.

Moved out of `MainWindow._step1_config_hash` (which now calls this) so that
code without a window -- the pre-segmentation run records, their pixel key --
hashes exactly as the window does: keys sorted, floats to 6 places, numpy
scalars as Python numbers, and time-like fields left out.
"""

import hashlib
import json

import numpy as np

TRANSIENT_KEYS = frozenset({
    "generated_at", "created_at", "saved_at", "last_used",
    "runtime_seconds", "avg_tile_s", "tile_seconds",
    "preview_path", "config_hash", "old_hash", "new_hash",
})


def canonical_value(value):
    if isinstance(value, dict):
        return {
            str(k): canonical_value(v)
            for k, v in sorted(value.items(), key=lambda item: str(item[0]))
            if str(k) not in TRANSIENT_KEYS
        }
    if isinstance(value, (list, tuple)):
        return [canonical_value(v) for v in value]
    if isinstance(value, float):
        return round(value, 6)
    if isinstance(value, np.floating):
        return round(float(value), 6)
    if isinstance(value, np.integer):
        return int(value)
    return value


def config_hash(value):
    payload = json.dumps(canonical_value(value), sort_keys=True, separators=(",", ":"),
                         default=str)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()
