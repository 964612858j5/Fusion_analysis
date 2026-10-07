"""Block PA-5b: entering a step does no heavy work on the GUI thread
(measured on the A5 synthetic slide, 2026-10-05)."""

import subprocess
import sys


def test_step2_asks_the_gpu_size_without_importing_torch():
    """First Step2 entry froze 7.4 s in `import torch` (fix 2)."""
    code = ("import sys; from block01.ui.step2_page import Step2Page as P; "
            "a = P._detect_vram_gb(); b = P._detect_vram_gb(); "
            "assert a == b; assert 'torch' not in sys.modules, 'torch was imported'; "
            "print('ok', a)")
    import os
    import block01
    root = os.path.dirname(os.path.dirname(os.path.abspath(block01.__file__)))
    env = dict(os.environ, QT_QPA_PLATFORM="offscreen",
               PYTHONPATH=os.pathsep.join([root, os.environ.get("PYTHONPATH", "")]))
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True,
                         timeout=120, env=env)
    assert out.returncode == 0, out.stderr[-2000:]
    assert out.stdout.strip().splitlines()[-1].startswith("ok")


def test_the_gpu_size_is_asked_once(monkeypatch):
    from block01.ui.step2_page import Step2Page
    monkeypatch.setattr(Step2Page, "_vram_gb_cache", [])
    calls = []
    real = subprocess.run
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: (calls.append(a), real(*a, **k))[1])
    first = Step2Page._detect_vram_gb()
    assert Step2Page._detect_vram_gb() == first
    assert len(calls) <= 1



def test_the_gpu_asked_is_cuda_s_first_visible_one(monkeypatch):
    """codex PA-5: CUDA's device 0 is the first of CUDA_VISIBLE_DEVICES."""
    import sys
    from block01.ui.step2_page import Step2Page
    seen = []
    # An earlier test in the same run may have imported torch, which this
    # function asks first -- the real card would answer, not nvidia-smi.
    monkeypatch.delitem(sys.modules, "torch", raising=False)

    class _Out:
        stdout = "6144\n"
    monkeypatch.setattr(subprocess, "run", lambda args, **k: (seen.append(args), _Out())[1])
    for visible, expect in (("1,0", "1"), ("GPU-abc", "GPU-abc"), (None, "0")):
        monkeypatch.setattr(Step2Page, "_vram_gb_cache", [])
        if visible is None:
            monkeypatch.delenv("CUDA_VISIBLE_DEVICES", raising=False)
        else:
            monkeypatch.setenv("CUDA_VISIBLE_DEVICES", visible)
        assert Step2Page._detect_vram_gb() == 6.0
        assert seen[-1][1:3] == ["-i", expect]
    monkeypatch.setattr(Step2Page, "_vram_gb_cache", [])
    monkeypatch.setenv("CUDA_VISIBLE_DEVICES", "")
    n = len(seen)
    assert Step2Page._detect_vram_gb() is None and len(seen) == n


def test_a_window_on_a_channel_not_composed_redraws_nothing(monkeypatch):
    """Fix 1b (user ruling 2026-10-06): only a channel the picture composes
    -- weight above 0 -- redraws Step1's GPU picture when its window moves."""
    from block01.ui import step1_viewer_mount as vm
    mount = vm.Step1WholeSlideMount.__new__(vm.Step1WholeSlideMount)
    monkeypatch.setattr(vm.Step1WholeSlideMount, "_domain", property(lambda self: None))
    monkeypatch.setattr(vm.Step1WholeSlideMount, "_state", property(lambda self: None))
    mount._mode = "overlay"
    spec = {"mode": "overlay", "weights": {"DAPI": 1.0, "CD3": 0.7, "TOX": 0.0}}
    monkeypatch.setattr(vm.draft_spec, "build_spec", lambda *a, **k: dict(spec))
    redraws = []
    monkeypatch.setattr(vm.Step1WholeSlideMount, "_refresh_gpu",
                        lambda self, reason: redraws.append(reason) or True)
    for channel in ("TOX", "CD8", "CD20"):            # not composed
        mount._on_gpu_mapping(channel)
    assert redraws == []
    mount._on_gpu_mapping("CD3")                       # composed, ticked
    mount._on_gpu_mapping("DAPI")
    assert redraws == ["intensity", "intensity"]
    # a ticked channel counts even before its window exists (it is composed
    # by weight, not by being drawn already)
    spec["weights"]["CD8"] = 0.5
    mount._on_gpu_mapping("CD8")
    assert len(redraws) == 3
    # unsure -> redraw, as before
    monkeypatch.setattr(vm.draft_spec, "build_spec",
                        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("x")))
    mount._on_gpu_mapping("TOX")
    assert len(redraws) == 4
