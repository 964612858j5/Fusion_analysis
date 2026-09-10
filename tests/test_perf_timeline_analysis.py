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
