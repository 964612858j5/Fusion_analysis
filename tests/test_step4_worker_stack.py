"""Step4's worker thread has a stack numba's compiler fits in
(docs/v17_step4_worker_stack_plan.md, backlog 14): on macOS a QThread gets
512 KB, and LLVM compiling the quantification kernels on a cold cache
overflowed it (the whole GUI closed). The compile runs in a fresh process
with an empty NUMBA_CACHE_DIR, inside a QThread with the worker's stack.
"""

import os
import subprocess
import sys
import textwrap

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
pytest.importorskip("block01.qt")
pytest.importorskip("numba")

from block01.workers import feature_extract_worker as fe  # noqa: E402


def test_the_worker_asks_for_a_32_mib_stack():
    w = fe.FeatureExtractWorker("/nowhere", "/nowhere")
    assert fe.STACK_BYTES >= 32 * 1024 * 1024
    assert w.stackSize() == fe.STACK_BYTES


_SCRIPT = textwrap.dedent("""
    import sys
    import numpy as np
    from block01.qt import QtCore
    from block01.workers import feature_extract_worker as fe
    import test_quant_engine as T

    app = QtCore.QCoreApplication([])
    result = {}

    class Compile(QtCore.QThread):
        def run(self):
            try:
                lab, chans, n = T.make_case(seed=22)
                rng = np.random.default_rng(1)
                chans = chans + [rng.integers(0, 60000, size=lab.shape, dtype=np.uint16)]
                nuc, table = T.make_nuclei(lab, corrupt=False)
                res, _t = T.run(lab, chans, n, nuclei=nuc, table=table, regions=("nucleus", "cytoplasm"),
                                distribution=T.qe.DIST_STATS, markers=["ch0", "ch1", "ch3"], tile=37)
                result["ok"] = list(res.dist_markers) == ["ch0", "ch1", "ch3"]
            except BaseException as exc:                 # noqa: BLE001
                result["error"] = repr(exc)

    t = Compile()
    t.setStackSize(fe.STACK_BYTES)                      # the worker's own stack
    t.start()
    if not t.wait(600000):
        print("timed out"); sys.exit(3)
    print(result)
    sys.exit(0 if result.get("ok") else 2)
""")


def test_numba_compiles_on_a_cold_cache_inside_a_thread_with_the_workers_stack(tmp_path):
    env = dict(os.environ, NUMBA_CACHE_DIR=str(tmp_path / "numba_cache"), QT_QPA_PLATFORM="offscreen")
    tests_dir = os.path.dirname(os.path.abspath(__file__))
    env["PYTHONPATH"] = os.pathsep.join(p for p in (env.get("PYTHONPATH", ""), tests_dir) if p)
    r = subprocess.run([sys.executable, "-c", _SCRIPT], env=env, capture_output=True, text=True,
                       timeout=900, cwd=str(tmp_path))
    assert r.returncode == 0, (r.returncode, r.stdout[-2000:], r.stderr[-2000:])
    assert "'ok': True" in r.stdout
    assert any((tmp_path / "numba_cache").rglob("*.nbi"))          # it really compiled, cold
