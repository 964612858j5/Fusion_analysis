"""Self-check: `python -m seg_runner.selftest [engine ...]`.

For each engine: start it in its own subprocess through the real client, run
one synthetic task, report versions, device, timings and peak memory. Used
as-is for the offline check (`unshare -n python -m seg_runner.selftest`).
Writes only into a temporary directory.
"""
import json
import sys
import tempfile
import time

from . import synthetic
from .client import EngineProcess

TASKS = {
    "cellpose": ("cellpose_wholecell_fusion", synthetic.wholecell_rgb, {"diameter": None}),
    "stardist": ("stardist_nuclei_dapi", synthetic.nuclei, {}),
    "mesmer": ("mesmer_nuclear_guided", synthetic.mesmer_pair, {"image_mpp": 0.5}),
}


def _peak_rss_mib(pid):
    try:
        with open(f"/proc/{pid}/status") as f:
            for line in f:
                if line.startswith("VmHWM:"):
                    return round(int(line.split()[1]) / 1024, 1)
    except OSError:
        pass
    return None


def _gpu_mib(pid):
    """GPU memory held by `pid` right now, or None if NVML cannot tell."""
    try:
        import pynvml
        pynvml.nvmlInit()
        used = 0
        for i in range(pynvml.nvmlDeviceGetCount()):
            h = pynvml.nvmlDeviceGetHandleByIndex(i)
            for p in pynvml.nvmlDeviceGetComputeRunningProcesses(h):
                if p.pid == pid and p.usedGpuMemory:
                    used += p.usedGpuMemory
        return round(used / 2**20, 1)
    except Exception:  # noqa: BLE001 -- measurement only
        return None


def check(engine, workdir):
    import numpy as np
    method, make, params = TASKS[engine]
    image = make()
    inp = f"{workdir}/{engine}_input.npy"
    np.save(inp, image)
    ep = EngineProcess(engine, log_path=f"{workdir}/{engine}.log")
    t0 = time.perf_counter()
    hello = ep.start()
    t_load = time.perf_counter() - t0
    t1 = time.perf_counter()
    states = ep.run([{"task_id": f"{engine}-1", "input": inp, "out_dir": workdir,
                      "params": dict(params, method=method)}])
    t_task = time.perf_counter() - t1
    rss = _peak_rss_mib(ep.proc.pid)
    gpu = _gpu_mib(ep.proc.pid)
    ep.close()
    record = ep.states[f"{engine}-1"]["detail"]
    counts = {}
    if states[f"{engine}-1"] == "ok":
        rec = json.load(open(record))
        counts = {k: rec[k].get("count") for k in ("cell", "nucleus")}
    return {"engine": engine, "state": states[f"{engine}-1"], "device": hello["device"],
            "identity": hello["identity"], "load_s": round(t_load, 2), "task_s": round(t_task, 2),
            "peak_rss_mib": rss, "gpu_mib": gpu, "counts": counts, "exit_code": ep.proc.returncode}


def main(argv=None):
    engines = (argv if argv is not None else sys.argv[1:]) or list(TASKS)
    ok = True
    with tempfile.TemporaryDirectory(prefix="seg_selftest_") as wd:
        for e in engines:
            try:
                r = check(e, wd)
            except Exception as exc:  # noqa: BLE001 -- report and continue
                r = {"engine": e, "state": "error", "error": repr(exc)}
            ok &= r.get("state") == "ok"
            print(json.dumps(r, ensure_ascii=False))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
