"""Block PA-2 in the window (acceptance 2026-10-05): 8 patches, one plan run,
a second plan added -- Run said "16 tasks" and ran the first plan again.

A finished, unchanged result of the run on screen is kept: copied into the
new run (it names where it came from) and not computed; an unchanged Run
does nothing; a plan only REMOVED gives a new run that copies what is left,
so the list shows what is configured now. Own module (see the module this
borrows its window from)."""

import os

import pytest

from test_step1_preseg_run_ui import SD, _quiet, _vals, _wait, _window, app  # noqa: F401
from block01.core import preseg_run


def _plan(w, probs):
    """The whole plan replaced, as loading one does (adopt would merge)."""
    w._preseg_methods.set_methods([{"method": SD, "values": _vals(SD, prob_thresh=probs)}])


def _rdir(w):
    return os.path.join(w._preseg_step1_dir(), "presegmentation_runs", w._preseg_run["run_id"])


def test_a_plan_added_runs_only_the_new_one(app, tmp_path, monkeypatch):  # noqa: F811
    pytest.importorskip("stardist")
    w = _window(app, tmp_path, monkeypatch, n_patches=2)
    try:
        told = _quiet(monkeypatch)
        _plan(w, [0.4])
        assert w._on_preseg_run() is True
        _wait(w)
        first = w._preseg_run["run_id"]
        assert w._preseg_methods.progress.text().startswith("Finished: 2/2 done · 2 ok")

        # a second plan beside the first: only its 2 tasks are computed
        _plan(w, [0.4, 0.6])
        assert w._on_preseg_run() is True
        _wait(w)
        second = w._preseg_run["run_id"]
        assert second != first
        assert w._preseg_methods.progress.text().startswith("Finished: 4/4 done · 4 ok")
        recs = preseg_run.load_records(_rdir(w))
        kept = [r for r in recs.values() if r.get("reused_from_run")]
        assert len(recs) == 4 and len(kept) == 2
        assert {r["reused_from_run"] for r in kept} == {first}
        assert all(r["params"]["prob_thresh"] == 0.4 for r in kept)
        assert all(os.path.dirname(r["nucleus"]["path"]).startswith(_rdir(w)) for r in kept)
        assert len(w._preseg_results.rows()) == 2
        assert all(r.btn_use.isEnabled() for r in w._preseg_results.rows())

        # nothing changed: nothing runs, no new run
        n_runs = len(os.listdir(os.path.dirname(_rdir(w))))
        assert w._on_preseg_run() is False
        assert "already up to date" in told[-1]
        assert w._preseg_run["run_id"] == second
        assert len(os.listdir(os.path.dirname(_rdir(w)))) == n_runs

        # a plan removed: a new run with what is left, copied, nothing computed
        _plan(w, [0.6])
        assert w._on_preseg_run() is True, told[-1:]
        _wait(w, timeout=30)
        recs = preseg_run.load_records(_rdir(w))
        assert len(recs) == 2 and all(r["reused_from_run"] == second for r in recs.values())
        assert len(w._preseg_results.rows()) == 1
        assert w._preseg_methods.progress.text().startswith("Finished: 2/2 done · 2 ok")
        # no engine ran, yet the run names the one its kept results ran on, so
        # Step2's contract accepts them (codex PA-2)
        from block01.core import preseg_contract
        run = preseg_run.read_run(_rdir(w))
        combo = run["combos"][0]["combo_id"]
        block = preseg_contract.build(run, combo, recs)
        assert block["method"] == SD
    finally:
        w.close()


def test_new_fusion_settings_run_everything_again(app, tmp_path, monkeypatch):  # noqa: F811
    pytest.importorskip("stardist")
    w = _window(app, tmp_path, monkeypatch, n_patches=1)
    try:
        _quiet(monkeypatch)
        _plan(w, [0.4])
        assert w._on_preseg_run() is True
        _wait(w)
        w._test_snapshot["value"] = dict(w._test_snapshot["value"], hash="fh2")
        assert w._on_preseg_run() is True
        _wait(w)
        recs = preseg_run.load_records(_rdir(w))
        assert len(recs) == 1 and not any(r.get("reused_from_run") for r in recs.values())
    finally:
        w.close()


def test_the_question_counts_only_what_will_run(app, tmp_path, monkeypatch):  # noqa: F811
    pytest.importorskip("stardist")
    w = _window(app, tmp_path, monkeypatch, n_patches=3)
    try:
        told = _quiet(monkeypatch)
        _plan(w, [0.4])
        assert w._on_preseg_run() is True
        _wait(w)
        # 3 patches x 5 combinations = 15, of which 3 are kept: 12 to run
        told.clear()
        monkeypatch.setattr(QtWidgets_box(), "question",
                            staticmethod(lambda *a, **k: (told.append(a[2]),
                                                          QtWidgets_box().No)[1]))
        _plan(w, [0.4, 0.5, 0.6, 0.7, 0.8])
        assert w._on_preseg_run() is False
        assert "12 tasks" in told[-1] and "3 unchanged results are kept" in told[-1]
    finally:
        w.close()


def QtWidgets_box():
    from PyQt5 import QtWidgets
    return QtWidgets.QMessageBox
