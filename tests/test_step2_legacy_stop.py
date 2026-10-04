"""Step2 without a Step1 hand-over: a Stop in ROI mode registers nothing.

Block K (plan, section 5). A params file without a `preseg_contract` block
(manual parameters, an old params file) runs the old path. A Stop there used
to end the ROI loop and still write the summary, register the run as
completed, mark it done in the ROI index and emit `finished`. Now it ends
the way a stopped hand-over does: `error('Stopped by user.')`, nothing
registered, no `finished`; results registered before stay as they were.

  * one ROI, Stop during its tiles;
  * two ROIs, Stop during the second ROI's tiles;
  * two ROIs, Stop after the first ROI is done, before the second starts;
  * no Stop: the run is registered and marked done.

Synthetic projects in the test's temporary directory only. Real StarDist,
never mocked; skipped when StarDist is not installed.
"""

import json
import os

import numpy as np
import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

pytest.importorskip("PyQt5")
from PyQt5 import QtCore, QtWidgets  # noqa: E402

H, W = 176, 208
ROWS, COLS, OVERLAP = 2, 2, 32
METHOD = "stardist_nuclei_dapi"
ROI_ID = "roi_block_k"


@pytest.fixture(scope="module")
def app():
    return QtWidgets.QApplication.instance() or QtWidgets.QApplication([])


@pytest.fixture
def roi_dir(tmp_path):
    """A synthetic ROI workspace of the run model (block RM): a published
    correct run and, on it, a published fuse run holding fused.zarr."""
    pytest.importorskip("stardist")
    import zarr
    from block01.utils import run_store
    (tmp_path / "project_manifest.json").write_text("{}")
    d = tmp_path / "rois" / ROI_ID
    (d / "step2").mkdir(parents=True)
    with open(d / "roi_manifest.json", "w", encoding="utf-8") as f:
        json.dump({"roi_id": ROI_ID, "display_name": "A"}, f)
    correct = run_store.new_run(str(d), "correct")
    run_store.write_inputs(correct, None, slide_id="s")
    run_store.publish(correct)
    fuse = run_store.new_run(str(d), "fuse")
    img = _image()
    z = zarr.open(os.path.join(fuse, "fused.zarr"), mode="w", shape=img.shape,
                  chunks=(64, 64, 2), dtype=np.uint16)
    z[:] = img
    run_store.write_inputs(fuse, correct)
    run_store.publish(fuse)
    return d


def _image(seed=0):
    """uint16 [fusion, nucleus]: round nuclei, each in a brighter ring."""
    rng = np.random.default_rng(seed)
    yy, xx = np.mgrid[0:H, 0:W]
    nuc = np.zeros((H, W), np.float32)
    mem = np.zeros((H, W), np.float32)
    for _ in range(70):
        cy, cx = rng.uniform(8, H - 8), rng.uniform(8, W - 8)
        d = np.hypot(yy - cy, xx - cx)
        nuc = np.maximum(nuc, np.clip(1.0 - d / 7.0, 0, 1))
        mem = np.maximum(mem, np.exp(-((d - 11.0) ** 2) / 4.0))
    img = np.stack([mem, nuc], -1) + rng.normal(0, 0.02, (H, W, 2))
    return (np.clip(img, 0, 1) * 60000).astype(np.uint16)


def _legacy_config():
    """Manual parameters: no `preseg_contract` block."""
    return {"method": METHOD, "prob_thresh": 0.5, "nms_thresh": 0.4,
            "model_name": "2D_versatile_fluo"}


def _worker(roi_dir, rois):
    from block01.workers.segment_merge_worker import SegmentMergeWorker
    from block01.utils import run_store
    fused = os.path.join(run_store.latest_run(str(roi_dir), "fuse"), "fused.zarr")
    w = SegmentMergeWorker(fused, seg_config=_legacy_config(),
                           n_rows=ROWS, n_cols=COLS, overlap_px=OVERLAP,
                           output_dir=str(roi_dir / "step2"), rois=rois)
    assert w.roi_dir == str(roi_dir) and w.roi_id == ROI_ID
    return w


def _collect(worker):
    got = {"finished": [], "error": [], "progress": []}
    d = QtCore.Qt.DirectConnection
    worker.finished.connect(lambda o, n: got["finished"].append(n), d)
    worker.error.connect(lambda m: got["error"].append(m), d)
    worker.progress.connect(lambda a, b, m: got["progress"].append(m), d)
    return got


def _records(roi_dir):
    """Block RM: what says a run exists -- the published segment runs and
    their .done -- as bytes."""
    from block01.utils import run_store
    return {r: open(os.path.join(r, run_store.DONE), "rb").read()
            for r in run_store.list_runs(str(roi_dir), "segment")}


def _registered(worker, roi_dir):
    """Block RM-4: a run is registered by being published (its .done); the
    registry and the run indexes are gone."""
    from block01.utils import run_store
    return run_store.is_done(worker.output_dir)


def _complete_run(roi_dir, rois):
    worker = _worker(roi_dir, rois)
    got = _collect(worker)
    worker.run()
    assert got["error"] == [] and len(got["finished"]) == 1 and got["finished"][0] > 0
    assert _registered(worker, roi_dir) is True
    return worker


def test_a_run_that_is_not_stopped_is_registered(app, roi_dir):
    _complete_run(roi_dir, [{"name": "A"}, {"name": "B"}])


@pytest.mark.parametrize("case, rois, stop_on", [
    ("one ROI, during its tiles", ["A"], "[A] Tile [2/"),
    ("second ROI, during its tiles", ["A", "B"], "[B] Tile [2/"),
    ("between the two ROIs", ["A", "B"], "✓ ROI A"),
])
def test_a_stop_registers_nothing(app, roi_dir, case, rois, stop_on):
    rois = [{"name": n} for n in rois]
    earlier = _complete_run(roi_dir, rois)
    before = _records(roi_dir)

    worker = _worker(roi_dir, rois)
    got = _collect(worker)
    worker.progress.connect(lambda a, b, m: worker.stop() if m.startswith(stop_on) else None,
                            QtCore.Qt.DirectConnection)
    worker.run()

    assert any(m.startswith(stop_on) for m in got["progress"])
    if stop_on.startswith("✓"):
        assert not any("ROI [2/2]" in m for m in got["progress"])
    if len(rois) == 2 and stop_on.startswith("[B]"):
        assert any(m.startswith("✓ ROI A") for m in got["progress"])
    assert got["finished"] == [] and got["error"] == ["Stopped by user."]
    assert _registered(worker, roi_dir) is False
    assert not os.path.exists(os.path.join(worker.output_dir, "segmentation_meta.json"))
    # What was registered before is untouched.
    assert _records(roi_dir) == before
    assert _registered(earlier, roi_dir) is True
