"""The offline analyser, on a synthetic timeline whose answer is known.

The reason it exists: the report has to say which callback held the GUI thread
during a stall and what was reading at the time, and neither is readable off a
log of thousands of lines by eye -- adjacent lines are not overlapping
intervals, and a background read that was cancelled mid-`read_region` is still
reading. So the attribution is arithmetic over intervals, and these tests pin
the arithmetic against a log built to have one right answer.

Own module: pure text in, pure text out, no Qt.
"""

import importlib.util
import os
import pathlib

import pytest

HERE = pathlib.Path(__file__).resolve().parent
ANALYZER = HERE.parent / "docs" / "perf_timeline" / "analyze_perf_log.py"


@pytest.fixture(scope="module")
def analyze():
    spec = importlib.util.spec_from_file_location("analyze_perf_log", ANALYZER)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _log(tmp_path, lines):
    path = tmp_path / "perf.log"
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return str(path)


# A stall of 300 ms at t=100.10..100.40, inside one GUI callback that ran
# 100.00..100.50, while two reads from two different jobs were open -- one of
# them cancelled while still inside its read. Everything the report has to
# say is in these nine lines, and none of it is adjacent to the gap.
TIMELINE = [
    "PERF t=99.000000 ev=run.begin run=r1 pid=1",
    "PERF t=99.500000 ev=job.begin source=step0.preload job=1 active_jobs=1 active_reads=0 patches=2 channels=3",
    "PERF t=99.600000 ev=read.begin source=step0.preload job=1 active_jobs=1 active_reads=1 patch=0 channel=CD3",
    "PERF t=99.900000 ev=job.begin source=step1.preview job=2 active_jobs=2 active_reads=1 patch=1 channels=2",
    "PERF t=99.950000 ev=read.begin source=step1.preview job=2 active_jobs=2 active_reads=2 patch=1 channel=CD8",
    "PERF t=100.050000 ev=intensity.in channel=CD3 rev=31",
    "PERF t=100.400000 ev=gui.gap dur_ms=0 gap_ms=300 expected_ms=5 t_begin=100.100000 active_jobs=2 active_reads=2 where=main",
    "PERF t=100.500000 ev=patch.persist dur_ms=500.00 t_begin=100.000000",
    "PERF t=100.450000 ev=handoff.zarr_report dur_ms=200.00 t_begin=100.250000",
    "PERF t=100.700000 ev=read.end source=step0.preload job=1 dur_ms=1100.00 active_jobs=2 active_reads=1 patch=0 channel=CD3 outcome=cancelled_after_read",
    "PERF t=100.800000 ev=step1.publish dur_ms=40.00 t_begin=100.760000 rev=17 mode=overlay",
    "PERF t=101.000000 ev=job.end source=step0.preload job=1 dur_ms=1500.00 active_jobs=1 active_reads=1",
]


def test_a_gap_is_attributed_to_the_span_that_contains_it(analyze, tmp_path,
                                                          capsys):
    analyze.main([_log(tmp_path, TIMELINE), "--gaps"])
    out = capsys.readouterr().out

    assert "gui.gap 300 ms" in out
    assert "held by (span contains the whole gap):" in out
    assert "patch.persist" in out, \
        "the callback that ran across the stall was not named"
    # and the shorter span INSIDE the stall is reported as such, not as the
    # thing that held the thread
    held = out.split("held by")[1].split("and, overlapping the gap:")[0]
    assert "handoff.zarr_report" not in held
    assert "handoff.zarr_report" in out


def test_a_gap_names_the_reads_that_were_open(analyze, tmp_path, capsys):
    analyze.main([_log(tmp_path, TIMELINE), "--gaps"])
    out = capsys.readouterr().out

    assert "background at the time: 2 job(s), 2 read(s)" in out
    assert "ch=CD3" in out and "ch=CD8" in out
    assert "still open" in out, \
        "a read with no end in the log is an unfinished read, not a missing one"


def test_actives_can_be_reconstructed_at_any_instant(analyze, tmp_path):
    records = analyze.read(_log(tmp_path, TIMELINE))

    jobs, reads = analyze.actives_at(records, 99.550000)
    assert len(jobs) == 1 and len(reads) == 0

    jobs, reads = analyze.actives_at(records, 100.200000)
    assert len(jobs) == 2 and len(reads) == 2

    jobs, reads = analyze.actives_at(records, 100.900000)
    assert [r[2].get("channel") for r in reads] == ["CD8"], \
        "the read that ended at 100.7 is still being counted"


def test_a_cancelled_read_is_visible_as_such(analyze, tmp_path):
    records = analyze.read(_log(tmp_path, TIMELINE))
    reads = analyze.intervals(records, "read")

    cancelled = [rec for _b, _e, rec in reads
                 if rec.get("outcome") == "cancelled_after_read"]
    assert len(cancelled) == 1
    assert cancelled[0].get("channel") == "CD3"


def test_the_published_frame_is_measured_against_the_input(analyze, tmp_path):
    """"The picture lags the mouse" as a number: 31 had arrived, 17 was
    drawn."""
    records = analyze.read(_log(tmp_path, TIMELINE))

    assert analyze.revision_lag(records) == [14.0]


def test_the_summary_reports_the_worst_regions_and_the_stalls(analyze,
                                                              tmp_path,
                                                              capsys):
    analyze.main([_log(tmp_path, TIMELINE)])
    out = capsys.readouterr().out

    assert "run=r1" in out
    assert "patch.persist" in out and "handoff.zarr_report" in out
    assert "GUI thread unavailable: 1 times" in out
    assert "published frames behind the input" in out


def test_dropped_lines_are_reported_as_holes(analyze, tmp_path, capsys):
    analyze.main([_log(tmp_path, TIMELINE + [
        "PERF t=101.500000 ev=perf.dropped lines=12 total=12 limit=10"])])
    out = capsys.readouterr().out

    assert "DROPPED lines: 12" in out
    assert "timeline has holes" in out


def test_a_gap_with_no_instrumented_caller_says_so(analyze, tmp_path, capsys):
    """Honest about its own blind spot: the thread may have been in code this
    timeline does not cover."""
    analyze.main([_log(tmp_path, [
        "PERF t=5.000000 ev=gui.gap gap_ms=120 t_begin=4.880000 "
        "active_jobs=0 active_reads=0 where=main"]), "--gaps"])
    out = capsys.readouterr().out

    assert "no GUI span contains it" in out


def test_an_instant_report_lists_spans_and_reads(analyze, tmp_path, capsys):
    analyze.main([_log(tmp_path, TIMELINE), "--at", "100.200000"])
    out = capsys.readouterr().out

    assert "2 job(s), 2 read(s)" in out
    assert "in span patch.persist" in out


# ── the two defects the first real run exposed ───────────────────────────

def test_gaps_with_no_containing_span_do_not_crash(analyze, tmp_path, capsys):
    """`--gaps` raised NameError on exactly this shape: no span contained the
    stall, so the sort key for the overlapping ones referred to a variable
    that had never been bound. The real 7.6 s stall is this shape."""
    analyze.main([_log(tmp_path, [
        "PERF t=10.000000 ev=run.begin run=r1 pid=1",
        "PERF t=20.000000 ev=gui.gap gap_ms=500 t_begin=19.500000 "
        "active_jobs=1 active_reads=1 where=main",
        "PERF t=20.400000 ev=step1.frame dur_ms=700.00 mode=overlay patch=0",
        "PERF t=19.600000 ev=step1.remap dur_ms=8.00 channel=CD3",
    ]), "--gaps"])
    out = capsys.readouterr().out

    assert "no GUI span contains it" in out
    assert "overlapping the gap" in out
    assert "step1.frame" in out and "step1.remap" in out


def test_a_damaged_t_begin_is_recovered_by_arithmetic(analyze, tmp_path):
    """The first instrumented run wrote every span's start through a `%.4g`
    formatter: 759147.127727 became 7.591e+05, losing tens of seconds. The
    duration is exact, so the interval is recovered from `t - dur_ms` and the
    written value is used only when it agrees."""
    records = analyze.read(_log(tmp_path, [
        "PERF t=759154.621387 ev=step1.frame dur_ms=7493.66 t_begin=7.591e+05 "
        "mode=overlay patch=0",
    ]))
    (begin, end, ev, rec), = analyze.spans(records)

    assert ev == "step1.frame"
    assert end == pytest.approx(759154.621387)
    assert begin == pytest.approx(759154.621387 - 7.49366, abs=1e-6)


def test_an_agreeing_t_begin_is_kept(analyze, tmp_path):
    records = analyze.read(_log(tmp_path, [
        "PERF t=100.500000 ev=step1.frame dur_ms=500.00 t_begin=100.000000",
    ]))
    (begin, _end, _ev, _rec), = analyze.spans(records)

    assert begin == pytest.approx(100.0, abs=1e-9)


def test_runs_are_segmented_and_the_last_one_is_the_default(analyze, tmp_path,
                                                            capsys):
    """Job ids and input revisions restart in a new process. Pairing across
    that boundary invents intervals and makes revision lag meaningless, so an
    appended log is split first."""
    path = _log(tmp_path, [
        "PERF t=1.000000 ev=run.begin run=old pid=1",
        "PERF t=2.000000 ev=job.begin source=step0.preload job=1 "
        "active_jobs=1 active_reads=0",
        "PERF t=3.000000 ev=run.begin run=new pid=2",
        "PERF t=4.000000 ev=job.begin source=step1.preview job=1 "
        "active_jobs=1 active_reads=0",
        "PERF t=5.000000 ev=job.end source=step1.preview job=1 dur_ms=1000.00 "
        "active_jobs=0 active_reads=0",
    ])
    records = analyze.read(path)

    assert len(analyze.runs(records)) == 2

    analyze.main([path])
    out = capsys.readouterr().out
    assert "2 runs in this log" in out
    assert "run 2/2  (new)" in out
    assert "(old)" not in out, "the default must be the last run, not all of them"

    analyze.main([path, "--run", "all"])
    out = capsys.readouterr().out
    assert "(old)" in out and "(new)" in out


def test_a_job_is_not_paired_across_a_run_boundary(analyze, tmp_path):
    """Both runs have a job 1. Paired across the boundary, the older one
    would appear to have read for the whole of the second run."""
    records = analyze.read(_log(tmp_path, [
        "PERF t=1.000000 ev=run.begin run=old pid=1",
        "PERF t=2.000000 ev=job.begin source=step0.preload job=1 "
        "active_jobs=1 active_reads=0",
        "PERF t=3.000000 ev=run.begin run=new pid=2",
        "PERF t=9.000000 ev=job.end source=step0.preload job=1 dur_ms=10.00 "
        "active_jobs=0 active_reads=0",
    ]))
    first, second = analyze.runs(records)

    open_in_first = analyze.intervals(first, "job")
    assert open_in_first[0][1] is None, \
        "the first run's job never ended in the first run"
    assert len(analyze.intervals(second, "job")) == 1


# ── the real pre-fix run, so the report's numbers are reproducible ───────

EXCERPT = (pathlib.Path(__file__).resolve().parent.parent / "docs"
           / "perf_timeline" / "prefix_run_excerpt.log")


def test_the_prefix_run_excerpt_reproduces_the_frozen_frame(analyze):
    """The measurement item 3 is argued from: one GUI frame of 7493.66 ms
    inside a 7652 ms stall, with a 29-channel preload reading underneath it."""
    records = analyze.read(str(EXCERPT))
    frames = [(b, e, r) for b, e, ev, r in analyze.spans(records)
              if ev == "step1.frame"]

    assert frames, "the excerpt lost the frame it exists to show"
    begin, end, rec = max(frames, key=lambda item: analyze.num(item[2],
                                                               "dur_ms", 0.0))
    assert analyze.num(rec, "dur_ms") == pytest.approx(7493.66)
    assert end - begin == pytest.approx(7.49366, abs=1e-5)

    gaps = [r for r in records if r.get("ev") == "gui.gap"]
    assert any(analyze.num(r, "gap_ms") == pytest.approx(7652.0)
               for r in gaps)

    # What was underneath it: the 29-channel Step0 preload, still reading.
    jobs, reads = analyze.actives_at(records, begin + 1.0)
    assert [r[2].get("source") for r in jobs] == ["step0.preload"]
    assert reads, "the preload was between reads for a whole second?"

    # And what set it off: Step1's own DAPI read finished 2 ms earlier, so the
    # frame is the callback that ran the moment the channel arrived.
    dapi = [(b, e, r) for b, e, r in analyze.intervals(records, "job")
            if r.get("source") == "step1.preview" and r.get("patch") == "0"]
    assert dapi, "the Step1 read that triggered the frame is missing"
    _b, dapi_end, _r = dapi[0]
    assert 0.0 <= begin - dapi_end < 0.01, (
        f"the frame began {begin - dapi_end:.4f}s after the channel arrived")


def test_the_excerpt_names_the_release_callback_cost(analyze):
    """Both gestures: the mouse release did 111 ms and 87 ms of synchronous
    work, nearly all of it the geometry persistence."""
    records = analyze.read(str(EXCERPT))
    emits = sorted(analyze.num(r, "dur_ms")
                   for _b, _e, ev, r in analyze.spans(records)
                   if ev == "patch.emit_patches_changed")
    persists = sorted(analyze.num(r, "dur_ms")
                      for _b, _e, ev, r in analyze.spans(records)
                      if ev == "patch.persist")

    assert emits == pytest.approx([86.61, 111.24])
    assert persists == pytest.approx([83.07, 105.32])
    assert all(p > 0.9 * e for p, e in zip(persists, emits)), \
        "the release callback's cost is the persistence, not the model update"
