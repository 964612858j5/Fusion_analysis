"""Block A9-M: `analyze_perf_log.py --a9` turns a scripted run into the
A9 numbers -- presented latency only from the intended viewer's complete
frame, settled latency apart, timeouts and self-settling actions counted."""

import importlib.util
import os

HERE = os.path.dirname(os.path.abspath(__file__))
spec = importlib.util.spec_from_file_location(
    "apl", os.path.join(HERE, "..", "docs", "perf_timeline", "analyze_perf_log.py"))
apl = importlib.util.module_from_spec(spec)
spec.loader.exec_module(apl)


def _log(tmp_path, lines):
    p = tmp_path / "a9.log"
    p.write_text("".join(f"PERF t={t:.6f} ev={ev} {extra}\n" for t, ev, extra in lines))
    return apl.read(str(p))


def test_presented_and_settled_are_kept_apart(tmp_path, capsys):
    recs = _log(tmp_path, [
        (1.000, "a9.begin", "actions=6"),
        (1.000, "a9.action", "n=1 do=drag expect=gpu"),
        (1.050, "a9.handled", "n=1 qt=5"),
        (1.090, "coverage.probe", "where=gpu probe_ms=1.5"),
        (1.095, "coverage.probe", "where=cpu-step1 probe_ms=0.5"),
        (1.100, "coverage", "where=cpu frame=1 gap_cells=0 target_fraction=1.0 sampled=256"),
        (1.200, "coverage", "where=gpu frame=2 gap_cells=0 target_fraction=1.0 sampled=256"),
        (1.200, "a9.action", "n=2 do=settle expect=gpu"),
        (1.700, "a9.settled", "n=2 timed_out=False reads=4 camera_user=1 camera_jump=0"),
        (1.800, "a9.action", "n=3 do=window expect=gpu"),
        (1.810, "a9.delivered", "n=3 kind=programmatic"),
        (1.830, "coverage", "where=gpu frame=3 gap_cells=0 target_fraction=1.0 sampled=256"),
        (2.200, "a9.settled", "n=3 timed_out=False reads=0 camera_user=0 camera_jump=0"),
        (2.300, "a9.action", "n=4 do=wheel expect=gpu"),
        (2.310, "a9.handled", "n=4 qt=31"),
        (2.300, "a9.action", "n=5 do=settle expect=gpu"),
        (9.000, "a9.settled", "n=5 timed_out=True reads=2 camera_user=1 camera_jump=1"),
    ])
    apl.report_a9(recs)
    out = capsys.readouterr().out
    pres = out.split("SETTLED")[0]
    assert "drag     n=  1  p50=   150.0" in pres          # the GPU frame, not the CPU one
    assert "window   n=  1  p50=    20.0" in pres          # self-settling action included
    assert "timeouts=1" in out and "wheel" in out           # timeout-only kind printed
    assert "window=0/0" in out and "drag=4/4" in out
    assert "program camera jumps during drag/wheel: 1" in out
    assert "probe's own cost: total 0.00 s" in out and "max 1.50 ms" in out


def test_wsl_gpu_wakes_are_told_apart_from_slow_frames():
    """User ruling 2026-10-07 (deployment is native Windows): GL work that
    waited ~500 ms on WSL's GPU wake-up -- 250-1000 ms after the GPU's last
    work -- is counted apart; a slow frame right after another is not."""
    a = apl

    def rec(ev, t, **kw):
        return dict(ev=ev, t=str(t), **{k: str(v) for k, v in kw.items()})
    records = [
        rec("gpu.present", 1.0),
        rec("gpu.paint", 1.5), rec("gpu.present", 2.0),            # wake at swap
        rec("gpu.submit", 3.0, t_begin=2.5),                       # wake in a submit
        rec("gpu.paint", 3.01), rec("gpu.present", 3.02),
        rec("gpu.paint", 3.05), rec("gpu.present", 3.55),          # slow, NOT after idle
        rec("gpu.paint", 6.0), rec("gpu.present", 6.05),           # long idle: fast wake
    ]
    assert a.wsl_gpu_wakes(records) == [(1.5, 2.0), (2.5, 3.0)]


def test_wakes_are_only_suspected_on_a_wsl_log(capsys):
    """codex: timing alone proves nothing -- a native log keeps them all."""
    def rec(ev, t, **kw):
        return dict(ev=ev, t=str(t), **{k: str(v) for k, v in kw.items()})
    base = [rec("gpu.present", 1.0), rec("gpu.paint", 1.5), rec("gpu.present", 2.0)]
    apl.report_a9([rec("a9.platform", 0.5, wsl=False)] + base)
    assert "without the 0 suspected" in capsys.readouterr().out
    apl.report_a9([rec("a9.platform", 0.5, wsl=True)] + base)
    assert "without the 1 suspected" in capsys.readouterr().out
