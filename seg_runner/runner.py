"""Child process: `python -m seg_runner.runner --engine <cellpose|stardist|mesmer>`.

Reads task messages from stdin, writes masks and one record per task into the
task's `out_dir`, answers each task with exactly one `result` or `error`.
Normal end: parent sends `shutdown`, child answers `done` and exits 0. The
parent owns every other ending (Stop, crash, close); see `client`.
"""
import argparse
import hashlib
import json
import os
import pathlib
import sys
import time
import traceback

from . import protocol

_PKG = pathlib.Path(__file__).resolve().parent
_REPO = _PKG.parent


def _sha256_files(paths):
    h = hashlib.sha256()
    for p in paths:
        if p.is_file():
            h.update(p.name.encode())
            h.update(p.read_bytes())
    return h.hexdigest()[:16]


def engine_identity(engine, engine_obj=None):
    """What decides whether a result came from 'the same segmentation'
    (plan 7.10.5; block A0.5): the engine kind, its explicit behaviour
    version and the model it loads -- `engines.behavior_identity`. Nothing
    about the environment: that is `engine_provenance`."""
    from . import engines
    return engines.behavior_identity(engine)


def _git_commit():
    try:
        import subprocess
        out = subprocess.run(["git", "-C", str(_REPO), "rev-parse", "--short", "HEAD"],
                             capture_output=True, text=True, timeout=5)
        return out.stdout.strip() or None
    except (OSError, ValueError, subprocess.SubprocessError):
        return None


def engine_provenance(engine, engine_obj):
    """What this engine process actually ran with. RECORDED, never compared
    (block A0.5): an unrelated package, a log line or a protocol change must
    not read as "another engine". Model file integrity is the deployment
    check `scripts/model_manifest.py --verify`, not a hash at every start."""
    env = _REPO / "envs" / "fusion_mesmer"
    manifest = env / "models.json"
    models = {}
    if manifest.is_file():
        models = {k: v for k, v in json.loads(manifest.read_text()).get("models", {}).items()
                  if v.get("engine") == engine}
    resolved = getattr(engine_obj, "model_resolved_path", None)
    try:
        resolved = resolved() if callable(resolved) else None
    except Exception:  # noqa: BLE001 -- provenance must never stop an engine
        resolved = None
    return {
        "git_commit": _git_commit(),
        "runner_version": _sha256_files(sorted(_PKG.glob("*.py"))),
        "lib_versions": engine_obj.lib_versions(),
        "env_lock_hash": _sha256_files([env / "conda-linux-64.lock", env / "requirements-pip.txt"]),
        "device": engine_obj.device,
        "model_manifest_entry_hash": hashlib.sha256(json.dumps(models, sort_keys=True).encode()).hexdigest()[:16],
        "model_resolved_path": resolved,
    }


def _run_task(engine_obj, msg):
    import numpy as np
    from . import engines
    params = dict(msg.get("params") or {})
    method = params["method"]
    out_dir = pathlib.Path(msg["out_dir"])
    out_dir.mkdir(parents=True, exist_ok=True)
    image = np.load(msg["input"])
    started = time.perf_counter()
    masks = engines.run(engine_obj, method, image, params)
    record = {"task_id": msg["task_id"], "method": method, "params": params,
              "status": protocol.OK, "device_used": engine_obj.device,
              "runtime_s": round(time.perf_counter() - started, 3)}
    for kind in ("cell", "nucleus"):
        arr = masks[kind]
        if arr is None:
            record[kind] = {"status": "not_produced"}
            continue
        path = out_dir / f"{msg['task_id']}.{kind}.npy"
        protocol.publish_array(str(path), arr)          # masks first ...
        labels = np.unique(arr)
        record[kind] = {"status": protocol.OK, "path": str(path),
                        "count": int((labels > 0).sum())}
    record_path = out_dir / f"{msg['task_id']}.record.json"
    protocol.publish_json(str(record_path), record)     # ... the record last
    return str(record_path)


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--engine", required=True, choices=["cellpose", "stardist", "mesmer"])
    args = ap.parse_args(argv)

    # The protocol keeps the real stdout; everything else printed to fd 1 --
    # by us, by a library, by a C extension -- lands on stderr instead.
    proto = os.fdopen(os.dup(1), "w", buffering=1)
    os.dup2(2, 1)
    sys.stdout = sys.stderr

    def send(kind, **fields):
        proto.write(protocol.encode(kind, **fields))
        proto.flush()

    from . import engines
    engine_obj = engines.load(args.engine)
    send("hello", engine=args.engine, identity=engine_identity(args.engine, engine_obj),
         provenance=engine_provenance(args.engine, engine_obj), device=engine_obj.device)

    for line in sys.stdin:
        if not line.strip():
            continue
        msg = protocol.decode(line)
        if msg["type"] == "shutdown":
            break
        if msg["type"] != "task":
            raise protocol.ProtocolError(f"unexpected message {msg['type']!r}")
        try:
            record_path = _run_task(engine_obj, msg)
        except Exception:  # noqa: BLE001 -- reported to the parent, one per task
            send("error", task_id=msg["task_id"], message=traceback.format_exc()[-4000:])
        else:
            send("result", task_id=msg["task_id"], record=record_path)
    send("done")
    return 0


if __name__ == "__main__":
    sys.exit(main())
