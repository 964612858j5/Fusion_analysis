"""G3.2b.5A.5: make the persistence chain really run, THEN observe the gesture.

Two phases, in order, and phase 2 only runs if phase 1 succeeded.

PHASE 1 -- a VALID temp project. 5A.4 pointed `_roi_context["step_dirs"]
["step0"]` at an empty /tmp folder and the worker skipped with
`outcome = "no_published_handoff"` (recorded, not inferred). A directory is
not a project: the commit needs a PUBLISHED handoff to update. This builds
one the way the repo's own passing fixture does
(`tests/test_step0_geometry_commit.py::_page`): ROI mode, a matching
`_roi_context_sig`, then `page._write_step0_handoff(config, zarr_path)`.
Phase 1 then makes one geometry edit and requires the REAL worker to write
files and emit `geometry_committed` ITSELF -- no hand-driven callback.

PHASE 2 -- the observed gesture, once and only once, with the display
observers from 5A.3 attached (submit / paintGL / resizeGL / Qt Resize /
LayoutRequest) plus the post-commit chain (source re-bind, session save).
The question stays the single one: what display state changes FIRST after a
successful Patch creation, and who triggered it.

NOT ASSUMED: that `.zattrs` being written means a source re-bind happened.
Re-binds are recorded by wrapping the real re-bind entries; a file mtime is
not taken as evidence of a call.

Production is read-only. Writes go to /tmp only.

Usage: python scripts/diagnose_step1_patch_persist_then_observe.py OUT.json
"""

import importlib
import json
import os
import pathlib
import shutil
import sys
import time
import traceback

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PKG = os.path.basename(_ROOT)
for extra in (os.path.dirname(_ROOT), os.path.join(_ROOT, "scripts"),
              os.path.join(_ROOT, "tests")):
    if extra not in sys.path:
        sys.path.insert(0, extra)

TMP = pathlib.Path("/tmp/g325a5_valid_project")
REC = []
SEQ = {"n": 0, "submit": 0, "paint": 0}
T0 = time.monotonic()
ARMED = {"on": False, "first": None}


def rec(kind, **fields):
    SEQ["n"] += 1
    row = {"i": SEQ["n"], "t_ms": round((time.monotonic() - T0) * 1000, 3),
           "kind": kind, **fields}
    REC.append(row)
    return row


def product_stack():
    out = []
    for frame in traceback.extract_stack()[:-1]:
        if "/block01" not in frame.filename or "/scripts/" in frame.filename:
            continue
        out.append(f"{os.path.basename(frame.filename)}:{frame.lineno} {frame.name}")
    return out[-16:]


def note_first(kind, row):
    if not ARMED["on"] or ARMED["first"] is not None:
        return
    ARMED["first"] = {"kind": kind, "record_index": row["i"],
                      "t_ms": row["t_ms"], "stack": product_stack()}
    rec("FIRST_DISPLAY_CHANGE", of=kind, stack=ARMED["first"]["stack"])


def small(v, d=0):
    if d > 3:
        return "<deep>"
    if v is None or isinstance(v, (bool, int, float, str)):
        return v
    if isinstance(v, dict):
        return {str(k): small(x, d + 1) for k, x in list(v.items())[:30]}
    if isinstance(v, (list, tuple)):
        return [small(x, d + 1) for x in list(v)[:6]]
    return str(v)[:300]


def install(pkg):
    gpu = importlib.import_module(f"{pkg}.ui.step1_gpu_layer")
    MainWindow = importlib.import_module(f"{pkg}.ui.main_window").MainWindow
    Step0Page = importlib.import_module(f"{pkg}.ui.step0.step0_page").Step0Page
    OverviewPanel = importlib.import_module(
        f"{pkg}.ui.step0.overview_panel").OverviewPanel
    mount_cls = importlib.import_module(
        f"{pkg}.ui.step1_viewer_mount").Step1WholeSlideMount
    layer_cls = gpu.Step1GpuLayer

    real_submit, real_paint, real_resize = (layer_cls.submit, layer_cls.paintGL,
                                            layer_cls.resizeGL)

    def submit(self, source, display, viewport):
        SEQ["submit"] += 1
        before = getattr(self, "_target_size", None)
        row = rec("submit.enter", submit_seq=SEQ["submit"],
                  widget_size=[self.width(), self.height()],
                  target_before=list(before) if before else None,
                  viewport_physical=list(viewport.physical_size),
                  world_rect=[round(float(v), 3) for v in viewport.world_rect],
                  levels=[(c.channel, c.selected_level)
                          for c in (source.channels or ())])
        note_first("submit", row)
        out = real_submit(self, source, display, viewport)
        after = getattr(self, "_target_size", None)
        rec("submit.return", submit_seq=SEQ["submit"],
            target_after=list(after) if after else None,
            reallocated=(before != after))
        return out

    def paintGL(self):
        SEQ["paint"] += 1
        widget = [self.width(), self.height()]
        target = getattr(self, "_target_size", None)
        target = list(target) if target else None
        dpr = float(self.devicePixelRatioF())
        phys = None if target is None else [int(round(widget[0] * dpr)),
                                            int(round(widget[1] * dpr))]
        row = rec("paintGL.enter", paint_seq=SEQ["paint"],
                  consumes_submit=SEQ["submit"], widget_size=widget,
                  fbo_target_size=target, widget_physical_size=phys,
                  blit_is_rescaled=(target is not None and phys is not None
                                    and tuple(target) != tuple(phys)))
        note_first("paintGL", row)
        out = real_paint(self)
        rec("paintGL.return", paint_seq=SEQ["paint"])
        return out

    def resizeGL(self, w, h):
        row = rec("resizeGL", w=int(w), h=int(h),
                  target=list(getattr(self, "_target_size", None) or ()) or None)
        note_first("resizeGL", row)
        return real_resize(self, w, h)

    layer_cls.submit, layer_cls.paintGL, layer_cls.resizeGL = (
        submit, paintGL, resizeGL)

    def wrap(owner, name, kind):
        real = getattr(owner, name, None)
        if real is None:
            rec("hook.missing", target=f"{owner.__name__}.{name}")
            return

        def called(self, *a, **kw):
            rec(f"{kind}.enter")
            try:
                return real(self, *a, **kw)
            finally:
                rec(f"{kind}.return")
        setattr(owner, name, called)

    for name in ("_on_patches", "_rebuild_patch_buttons",
                 "_on_step0_geometry_committed", "_schedule_step1_session_save",
                 "_refresh_patch_preview", "_show_active_roi_preview"):
        wrap(MainWindow, name, f"main.{name}")
    for name in ("_persist_geometry_edit", "_on_geometry_persist_published",
                 "_on_geometry_persist_skipped", "_on_geometry_persist_failed"):
        wrap(Step0Page, name, f"step0.{name}")
    wrap(OverviewPanel, "_add_patch", "overview._add_patch")
    for name in ("show_patch", "jump_to_point", "rebind_source",
                 "_rebind_source", "set_source"):
        wrap(mount_cls, name, f"mount.{name}")
    return gpu, MainWindow, layer_cls


def make_watcher(qtcore, layer):
    class Watcher(qtcore.QObject):
        def eventFilter(self, obj, event):                  # noqa: N802
            t = event.type()
            if t == qtcore.QEvent.Resize:
                row = rec("qt.Resize", who=type(obj).__name__,
                          size=[obj.width(), obj.height()],
                          is_gl_layer=(obj is layer))
                note_first("qt.Resize", row)
            elif t == qtcore.QEvent.LayoutRequest:
                rec("qt.LayoutRequest", who=type(obj).__name__)
            return False
    return Watcher()


def make_valid_project(page, bench):
    """A published handoff on disk, built the way the repo's own fixture does."""
    if TMP.exists():
        shutil.rmtree(TMP)
    step0_dir = TMP / "roi1" / "step0"
    step0_dir.mkdir(parents=True)

    height, width = page.loader.shape
    # An ROI that COVERS the slide, so patches drawn anywhere fall inside it,
    # but ROI MODE rather than full-WSI mode -- full-WSI makes
    # `_roi_context_signature` depend on the loader shape and the commit
    # refuses with `roi_changed`.
    y1, x1 = int(height), int(width)
    roi = {"name": "ROI_1", "display_name": "ROI_1",
           "bbox_fullres": [0, y1, 0, x1],
           "polygon_fullres": [[0, 0], [0, x1], [y1, x1], [y1, 0]],
           "shape": "rect", "type": "roi"}
    page.overview.full_wsi_mode = False
    page.overview._rois = [roi]
    page.output_dir = str(TMP)
    page.panel_csv_path = ""
    page.panel_groups = {}
    page._roi_context = {
        "roi_id": "roi1", "roi_dir": str(TMP / "roi1"),
        "project_dir": str(TMP),
        "step_dirs": {"step0": str(step0_dir),
                      "step1": str(TMP / "roi1" / "step1"),
                      "step2": str(TMP / "roi1" / "step2")}}
    page._roi_context_sig = page._roi_context_signature(page._standard_rois())
    config = {"method_params": {"tophat_radius": 25, "cucim_sigma": 30},
              "channel_decisions": {}}
    zarr_path = str(step0_dir / "corrected_channels.zarr")
    page._write_step0_handoff(config, zarr_path)
    files = sorted(p.name for p in step0_dir.iterdir())
    rec("phase1.handoff_written", step0_dir=str(step0_dir), files=files)
    return step0_dir, zarr_path, files


def main():
    out = pathlib.Path(sys.argv[1] if len(sys.argv) > 1
                       else "/tmp/patch_persist_observe.json")
    from PyQt5 import QtCore, QtWidgets
    import pyqtgraph as pg
    pg.setConfigOptions(antialias=True, imageAxisOrder="row-major")

    bench = importlib.import_module("diagnose_step1_patch_draw_wobble")
    pkg = "block01" if "block01" in sys.modules else PKG
    gpu, MainWindow, layer_cls = install(pkg)

    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication(sys.argv[:1])
    app.setStyle("Fusion")
    bench.APP = app
    bench.commit_geometry = lambda *_a, **_kw: None   # the WORKER must publish

    window, loader = bench.real_window()
    window.resize(1400, 900)
    window.show()
    bench.pump(600)
    window._stack.setCurrentWidget(window._step1_page_widget)
    bench.pump(600)
    window.right_tabs.setCurrentWidget(window.viewer_tab)
    bench.pump(600)
    page = window._step0

    report = {"block": "G3.2b.5A.5", "phase1": {}, "phase2": {}}

    # ── PHASE 1 ────────────────────────────────────────────────────
    step0_dir, zarr_path, files = make_valid_project(page, bench)
    report["phase1"]["handoff_files"] = files
    report["phase1"]["step0_dir"] = str(step0_dir)

    committed = []
    page.geometry_committed.connect(
        lambda payload: (committed.append(payload),
                         rec("SIGNAL.geometry_committed",
                             payload=small(payload))))
    worker = page._geometry_persist()
    outcomes = []
    worker.published.connect(lambda p: outcomes.append(("published", small(p))))
    worker.skipped.connect(lambda p: outcomes.append(
        ("skipped", (p or {}).get("outcome"))))
    worker.failed.connect(lambda p: outcomes.append(
        ("failed", str((p or {}).get("error"))[:200])))

    # ONE geometry edit through the page's own model, then let the REAL
    # worker do its work. No hand-driven callback.
    page.overview._patches = [{"roi_idx": 0, "coords": (1000, 3000, 1000, 3000)}]
    rec("phase1.edit_made")
    page._persist_geometry_edit()
    deadline = time.monotonic() + 30.0
    while time.monotonic() < deadline:
        bench.pump(200)
        if committed or outcomes:
            break
    bench.pump(1000)
    written = sorted(str(p.relative_to(TMP)) for p in TMP.rglob("*") if p.is_file())
    report["phase1"].update(
        outcomes=outcomes, geometry_committed_fired=len(committed),
        files_after=written[:40], n_files_after=len(written),
        persist_state=page.geometry_persist_state()
        if hasattr(page, "geometry_persist_state") else None,
        worker_stats=small(worker.stats()))
    ok = bool(committed) and any(o[0] == "published" for o in outcomes)
    report["phase1"]["chain_works"] = ok
    print("PHASE 1: handoff files =", files)
    print("PHASE 1: outcomes      =", outcomes)
    print("PHASE 1: geometry_committed fired =", len(committed))
    print("PHASE 1: worker stats  =", report["phase1"]["worker_stats"])
    print("PHASE 1: chain works   =", ok)

    if not ok:
        report["phase2"] = {"skipped": "phase 1 did not publish; "
                                       "the observed gesture is not run"}
        report["records"] = REC
        out.write_text(json.dumps(report, indent=1, default=str))
        print(f"\nwrote {out}  -- PHASE 2 NOT RUN")
        return 1

    # ── PHASE 2: one observed gesture ──────────────────────────────
    layer = next(iter(window.findChildren(gpu.Step1GpuLayer)), None)
    assert layer is not None
    watcher = make_watcher(QtCore, layer)
    for w in (window, window.viewer_tab, layer,
              window._step1_mount.host.stack.controller.view.graphics.viewport()):
        w.installEventFilter(watcher)

    # PHASE 1 set `page.overview._patches` directly to make one edit. That
    # list is what `_standard_patches` prefers, so leaving it there would
    # make the popup's drag invisible to the task and the commit would come
    # back "unchanged" -- which is exactly what the first run of phase 2
    # showed (a patch was added, no file changed). Hand authority back to
    # the page's own `patches`, which the popup feeds.
    page.overview._patches = []
    rec("phase2.page_overview_patches_cleared",
        page_patches=len(getattr(page, "patches", []) or []))

    popup = window._display.show_navigator()
    popup.set_overview_context(loader=loader, nuc_ch=page.nucleus_channel,
                               rois=[], patches=[],
                               dataset_token=str(loader.filepath))
    deadline = time.monotonic() + 120.0
    while time.monotonic() < deadline:
        bench.pump(250)
        if (int(getattr(popup.overview, "ov_h", 0) or 0) > 8
                and popup.overview.img_item.image is not None):
            break
    assert popup.overview.img_item.image is not None, "no thumbnail"
    bench.pump(2500)

    n0 = len(popup.overview._patches)
    before_files = {str(p) for p in TMP.rglob("*") if p.is_file()}
    ARMED.update(on=True, first=None)
    rec("phase2.gesture.start")
    bench.draw_patch(popup, _Probe(window), fraction=(0.30, 0.30, 0.60, 0.60))
    for _ in range(40):
        bench.pump(500)
        if {str(p) for p in TMP.rglob("*") if p.is_file()} != before_files:
            break
    bench.pump(2000)
    ARMED["on"] = False
    rec("phase2.gesture.end")
    after_files = {str(p) for p in TMP.rglob("*") if p.is_file()}
    report["phase2"] = {
        "patches_added": len(popup.overview._patches) - n0,
        "files_changed": sorted(os.path.relpath(p, TMP)
                                for p in (after_files - before_files)),
        "geometry_committed_total": len(committed),
        "first_display_change": ARMED["first"],
        "submits": SEQ["submit"], "paints": SEQ["paint"],
    }
    report["records"] = REC
    report["honesty"] = [
        "the worker published on its own; no completion callback was driven",
        "a source re-bind is only reported if a re-bind entry was actually "
        "called -- a written .zattrs is not taken as evidence of one",
        "co-occurrence in time is a lead, not proof of causation",
    ]
    out.write_text(json.dumps(report, indent=1, default=str))
    print(f"\nPHASE 2: patches_added = {report['phase2']['patches_added']}")
    print(f"PHASE 2: files changed = {report['phase2']['files_changed']}")
    print(f"PHASE 2: submits={SEQ['submit']} paints={SEQ['paint']}")
    first = ARMED["first"]
    if first is None:
        print("PHASE 2: NO display-state change was recorded")
    else:
        print(f"PHASE 2: FIRST change = {first['kind']} at {first['t_ms']}ms")
        for line in first["stack"]:
            print("     ", line)
    print(f"wrote {out}")
    return 0


class _Probe:
    def __init__(self, window):
        self.w = window

    def mark(self, *_a, **_kw):
        return None


if __name__ == "__main__":
    sys.exit(main())
