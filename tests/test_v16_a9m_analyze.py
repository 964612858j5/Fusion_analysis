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


def test_m0_pairs_each_posted_input_with_its_handling_and_finds_the_owed_level(tmp_path, capsys):
    recs = _log(tmp_path, [
        (1.000, "a9.begin", "actions=4"),
        (1.000, "a9.action", "n=1 do=mark expect=gpu label=step1 zoom in"),
        (1.100, "a9.action", "n=2 do=wheel expect=gpu notches=1"),
        (1.101, "a9.driver", "dur_ms=0.5 t_begin=1.1005 what=viewer cached=True"),
        (1.102, "a9.post", "n=2 k=1"),
        (1.112, "a9.handled", "n=2 qt=31 seq=1"),
        (1.130, "coverage", "where=gpu frame=1 gap_cells=0 target_fraction=1.0 "
                            "exact_fraction=0.4 target_index=2 sampled=128"),
        (1.250, "coverage", "where=gpu frame=2 gap_cells=0 target_fraction=1.0 "
                            "exact_fraction=1.0 target_index=2 sampled=128"),
        (1.260, "gpu.fine_refused", "channel=CD3 level=1 tiles=150 bytes=1 budget=1"),
        (1.300, "a9.action", "n=3 do=settle expect=gpu"),
        (1.700, "a9.settled", "n=3 timed_out=False reads=0 camera_user=1 camera_jump=0"),
    ])
    apl.report_m0(recs)
    out = capsys.readouterr().out
    assert "input queue (post -> handled) wheel  ms: n=   1  p50=   10.0" in out
    assert "first n=   1  p50=   18.0" in out          # handled -> first frame
    assert "any   n=   1  p50=   18.0" in out          # complete, any fine level
    assert "owed  n=   1  p50=  138.0" in out          # complete at the owed level
    assert "fine-budget refusals: 1 CD3@L1x1" in out


def test_m0_a_notch_is_credited_only_its_own_viewer_and_frames(tmp_path, capsys):
    recs = _log(tmp_path, [
        (1.000, "a9.begin", "actions=5"),
        (1.000, "a9.action", "n=1 do=mark expect=gpu label=zoom"),
        (1.100, "gpu.frame", "frame=7 level=2"),           # submitted BEFORE the notch
        (1.200, "a9.action", "n=2 do=wheel expect=gpu notches=1"),
        (1.210, "a9.handled", "n=2 qt=31 seq=1"),
        (1.220, "coverage", "where=gpu frame=7 gap_cells=0 target_fraction=1.0"),
        (1.230, "coverage", "where=cpu frame=3 gap_cells=0 target_fraction=1.0"),
        (1.300, "a9.action", "n=3 do=wheel expect=gpu notches=1"),
        (1.310, "a9.handled", "n=3 qt=31 seq=1"),
        (1.320, "gpu.frame", "frame=8 level=2"),
        (1.340, "coverage", "where=gpu frame=8 gap_cells=0 target_fraction=1.0"),
        (1.400, "a9.end", "camera_user=0 camera_jump=0 reads_total=0"),
    ])
    apl.report_m0(recs)
    out = capsys.readouterr().out
    # notch 1: the old pending frame 7 and the CPU frame do not count, and
    # its window closes at notch 2 -- so it is reported as not complete
    assert "any   n=   1  p50=   30.0" in out
    assert "not complete before the next input: 1" in out
