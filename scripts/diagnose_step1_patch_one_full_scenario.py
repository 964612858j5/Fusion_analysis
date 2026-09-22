"""G3.2b.5A.6: ONE complete scenario, four confirmations from the SAME gesture.

The wiring that puts a popup-drawn Patch into the page's single model is the
product's own `Step0Page.furnish_navigator(popup)`, which `display.navigator()`
calls when it lazily creates the popup. Earlier runs hand-called
`popup.set_overview_context(...)` instead and so left that model bridge out;
this one lets the product furnish the popup and hand-connects nothing.

From ONE gesture, all four must hold together -- results are never stitched
from different runs or phases:

  1. the popup gesture adds a Patch AND the page's authoritative model
     receives that same Patch;
  2. the real worker returns `published` and the temp project gains the
     matching geometry files;
  3. `geometry_committed` fires by itself and MainWindow adopts it;
  4. whatever really happened in between -- source check / re-bind, session
     save, GPU submit, resizeGL, paintGL -- lined up on one clock.

If 1-3 hold and there is still no display change, the honest report is
"the complete temp-project path did not reproduce it", and the guessing
stops there. "What happens after the write" is NOT to be called the most
likely root cause.

Production is read-only; writes go to /tmp only.

Usage: python scripts/diagnose_step1_patch_one_full_scenario.py OUT.json
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

TMP = pathlib.Path("/tmp/g325a6_project")
REC, SEQ, T0 = [], {"n": 0, "submit": 0, "paint": 0}, time.monotonic()
ARMED = {"on": False, "first": None}


def rec(kind, **f):
    SEQ["n"] += 1
    row = {"i": SEQ["n"], "t_ms": round((time.monotonic() - T0) * 1000, 3),
           "kind": kind, **f}
    REC.append(row)
    return row


def stack():
    out = []
    for fr in traceback.extract_stack()[:-1]:
        if "/block01" not in fr.filename or "/scripts/" in fr.filename:
            continue
        out.append(f"{os.path.basename(fr.filename)}:{fr.lineno} {fr.name}")
    return out[-16:]


def note_first(kind, row):
    if not ARMED["on"] or ARMED["first"] is not None:
        return
    ARMED["first"] = {"kind": kind, "i": row["i"], "t_ms": row["t_ms"],
                      "stack": stack()}
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
    Overview = importlib.import_module(
        f"{pkg}.ui.step0.overview_panel").OverviewPanel
    mount_cls = importlib.import_module(
        f"{pkg}.ui.step1_viewer_mount").Step1WholeSlideMount
    layer = gpu.Step1GpuLayer
    r_submit, r_paint, r_resize = layer.submit, layer.paintGL, layer.resizeGL

    def submit(self, source, display, viewport):
        SEQ["submit"] += 1
        before = getattr(self, "_target_size", None)
        row = rec("submit.enter", n=SEQ["submit"],
                  widget=[self.width(), self.height()],
                  target_before=list(before) if before else None,
                  viewport_physical=list(viewport.physical_size),
                  world_rect=[round(float(v), 3) for v in viewport.world_rect],
                  levels=[(c.channel, c.selected_level)
                          for c in (source.channels or ())])
        note_first("submit", row)
        out = r_submit(self, source, display, viewport)
        after = getattr(self, "_target_size", None)
        rec("submit.return", n=SEQ["submit"],
            target_after=list(after) if after else None,
            reallocated=(before != after))
        return out

    def paintGL(self):
        SEQ["paint"] += 1
        w = [self.width(), self.height()]
        t = getattr(self, "_target_size", None)
        t = list(t) if t else None
        dpr = float(self.devicePixelRatioF())
        phys = None if t is None else [int(round(w[0] * dpr)),
                                       int(round(w[1] * dpr))]
        row = rec("paintGL.enter", n=SEQ["paint"], consumes_submit=SEQ["submit"],
                  widget=w, fbo=t, widget_physical=phys,
                  blit_is_rescaled=(t is not None and phys is not None
                                    and tuple(t) != tuple(phys)))
        note_first("paintGL", row)
        out = r_paint(self)
        rec("paintGL.return", n=SEQ["paint"])
        return out

    def resizeGL(self, w, h):
        row = rec("resizeGL", w=int(w), h=int(h),
                  target=list(getattr(self, "_target_size", None) or ()) or None)
        note_first("resizeGL", row)
        return r_resize(self, w, h)

    layer.submit, layer.paintGL, layer.resizeGL = submit, paintGL, resizeGL

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

    for n in ("_on_patches", "_rebuild_patch_buttons",
              "_on_step0_geometry_committed", "_schedule_step1_session_save",
              "_refresh_patch_preview", "_show_active_roi_preview",
              "_save_step1_session"):
        wrap(MainWindow, n, f"main.{n}")
    for n in ("_persist_geometry_edit", "_on_geometry_persist_published",
              "_on_geometry_persist_skipped", "_on_geometry_persist_failed",
              "_reconcile_roi_edit", "furnish_navigator"):
        wrap(Step0Page, n, f"step0.{n}")
    wrap(Overview, "_add_patch", "overview._add_patch")
    # SOURCE CHECK / RE-BIND: recorded only if a real entry is called. A
    # written `.zattrs` is never taken as evidence that one was.
    for n in ("show_patch", "jump_to_point", "rebind_source", "_rebind_source",
              "set_source", "_rebuild_source", "refresh_source"):
        wrap(mount_cls, n, f"mount.{n}")
    return gpu, layer


def watcher(qtcore, gl):
    class W(qtcore.QObject):
        def eventFilter(self, obj, ev):                     # noqa: N802
            t = ev.type()
            if t == qtcore.QEvent.Resize:
                note_first("qt.Resize", rec("qt.Resize", who=type(obj).__name__,
                                            size=[obj.width(), obj.height()],
                                            is_gl=(obj is gl)))
            elif t == qtcore.QEvent.LayoutRequest:
                rec("qt.LayoutRequest", who=type(obj).__name__)
            return False
    return W()


def main():
    out = pathlib.Path(sys.argv[1] if len(sys.argv) > 1
                       else "/tmp/one_full_scenario.json")
    from PyQt5 import QtCore, QtWidgets
    import pyqtgraph as pg
    pg.setConfigOptions(antialias=True, imageAxisOrder="row-major")

    bench = importlib.import_module("diagnose_step1_patch_draw_wobble")
    pkg = "block01" if "block01" in sys.modules else PKG
    gpu, layer_cls = install(pkg)

    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication(sys.argv[:1])
    app.setStyle("Fusion")
    bench.APP = app
    bench.commit_geometry = lambda *_a, **_kw: None     # the WORKER publishes

    window, loader = bench.real_window()
    window.resize(1400, 900)
    window.show()
    bench.pump(600)
    window._stack.setCurrentWidget(window._step1_page_widget)
    bench.pump(600)
    window.right_tabs.setCurrentWidget(window.viewer_tab)
    bench.pump(600)
    page = window._step0

    # ── a VALID temp project, published with ZERO patches ──────────
    if TMP.exists():
        shutil.rmtree(TMP)
    step0_dir = TMP / "roi1" / "step0"
    step0_dir.mkdir(parents=True)
    h, w = page.loader.shape
    page.overview.full_wsi_mode = False
    page.overview._rois = [{"name": "ROI_1", "display_name": "ROI_1",
                            "bbox_fullres": [0, int(h), 0, int(w)],
                            "polygon_fullres": [[0, 0], [0, int(w)],
                                                [int(h), int(w)], [int(h), 0]],
                            "shape": "rect", "type": "roi"}]
    page.overview._patches = []          # the published baseline has none
    page.output_dir, page.panel_csv_path, page.panel_groups = str(TMP), "", {}
    page._roi_context = {"roi_id": "roi1", "roi_dir": str(TMP / "roi1"),
                         "project_dir": str(TMP),
                         "step_dirs": {"step0": str(step0_dir),
                                       "step1": str(TMP / "roi1" / "step1"),
                                       "step2": str(TMP / "roi1" / "step2")}}
    page._roi_context_sig = page._roi_context_signature(page._standard_rois())
    page._write_step0_handoff(
        {"method_params": {"tophat_radius": 25, "cucim_sigma": 30},
         "channel_decisions": {}}, str(step0_dir / "corrected_channels.zarr"))
    rec("project.published", files=sorted(p.name for p in step0_dir.iterdir()))

    # `_write_step0_handoff` runs the real ROI-output path and can leave the
    # page's nucleus channel empty; `furnish_navigator` then binds the popup
    # with `nuc_ch=""` and the overview read fails with "Channel '' not
    # found". Re-assert the page's own value (a product field, not a new
    # one) BEFORE the popup is created, and record what it was.
    names = list(loader.channel_names())
    rec("nucleus.before", page=getattr(page, "nucleus_channel", None),
        overview=getattr(page.overview, "nuc_ch", None))
    if not getattr(page, "nucleus_channel", ""):
        page.nucleus_channel = names[0]
    if not getattr(page.overview, "nuc_ch", ""):
        page.overview.nuc_ch = page.nucleus_channel
    rec("nucleus.after", page=page.nucleus_channel,
        overview=page.overview.nuc_ch, loader_shape=list(page.loader.shape))

    # THE AUTHORITATIVE MODEL, synced the way the product's own load flow
    # does it (step0_page.py:6111): `_roi_model.adopt(...)`. Setting only
    # `page.nucleus_channel` and `page.overview.nuc_ch` left the model's
    # channel empty, and `furnish_navigator` feeds the popup FROM the model
    # (`_feed_popup_from_model`), so the empty value overwrote the popup's
    # DAPI -- and the final `bind_dataset` returned early because the token
    # had not changed. This is a bench initialisation gap, not evidence
    # about the product's own load path.
    page._roi_model.adopt(
        rois=list(page.overview._rois), patches=[], full_wsi_mode=False,
        loader=page.loader, nucleus_channel=page.nucleus_channel)
    rec("roi_model.synced",
        model_nucleus=page._roi_model.nucleus_channel,
        model_loader_shape=list(getattr(page._roi_model.loader, "shape", ())
                                or ()),
        model_rois=len(page._roi_model.rois),
        model_patches=len(page._roi_model.patches),
        model_full_wsi=page._roi_model.full_wsi_mode)
    assert page._roi_model.nucleus_channel == page.nucleus_channel, (
        "the model did not take the nucleus channel")

    # MAINWINDOW MUST BE BOUND TO THIS HANDOFF or it ignores the commit:
    # `_on_step0_geometry_committed` compares the incoming manifest path with
    # the bound one and returns early otherwise -- which is what a previous
    # run showed (signal received, `_on_patches` never called, so the button
    # rebuild and its layout never ran at all).
    window.step0_output = dict(getattr(window, "step0_output", None) or {})
    window.step0_output["step0_manifest_path"] = str(
        (step0_dir / "step0_roi_result.json").resolve())
    rec("mainwindow.bound_to_handoff",
        path=window.step0_output["step0_manifest_path"])

    committed, outcomes = [], []
    page.geometry_committed.connect(
        lambda p: (committed.append(p),
                   rec("SIGNAL.geometry_committed", payload=small(p))))
    worker = page._geometry_persist()
    worker.published.connect(lambda p: outcomes.append(
        ("published", (p or {}).get("outcome"),
         (p or {}).get("patch_config_path"))))
    worker.skipped.connect(lambda p: outcomes.append(
        ("skipped", (p or {}).get("outcome"), None)))
    worker.failed.connect(lambda p: outcomes.append(
        ("failed", str((p or {}).get("error"))[:200], None)))

    # ── the popup, furnished by the PRODUCT ────────────────────────
    popup = window._display.show_navigator()
    assert popup is not None, "no navigator"
    bench.pump(800)
    bridged = popup.overview.receivers(popup.overview.patches_changed)
    rec("popup.furnished", patches_changed_receivers=int(bridged),
        is_window=bool(popup.isWindow()),
        model_nucleus=page._roi_model.nucleus_channel,
        popup_overview_nucleus=getattr(popup.overview, "nuc_ch", None),
        page_nucleus=page.nucleus_channel)
    assert getattr(popup.overview, "nuc_ch", "") == page.nucleus_channel, (
        f"popup nuc_ch={popup.overview.nuc_ch!r} != page "
        f"{page.nucleus_channel!r} -- the model sync did not reach the popup")
    assert bridged > 0, ("furnish_navigator did not connect the model bridge; "
                         "refusing to hand-wire it")
    deadline = time.monotonic() + 150.0
    while time.monotonic() < deadline:
        bench.pump(250)
        if (int(getattr(popup.overview, "ov_h", 0) or 0) > 8
                and popup.overview.img_item.image is not None):
            break
    if popup.overview.img_item.image is None:
        # A FAILED RUN MUST STILL LEAVE EVIDENCE rather than just an
        # assertion: record what the page and the popup actually hold, so
        # the blocker can be named precisely instead of guessed at.
        rec("BLOCKED.no_thumbnail",
            status=popup.overview.status.text(),
            page_nucleus=getattr(page, "nucleus_channel", None),
            page_overview_nuc=getattr(page.overview, "nuc_ch", None),
            popup_overview_nuc=getattr(popup.overview, "nuc_ch", None),
            page_loader=str(getattr(page, "loader", None)),
            page_loader_shape=list(getattr(getattr(page, "loader", None),
                                           "shape", ()) or ()),
            page_loader_channels=list(
                getattr(page, "loader", None).channel_names())[:6]
            if getattr(page, "loader", None) is not None else None,
            bench_loader_shape=list(loader.shape),
            popup_loader=str(getattr(popup.overview, "loader", None)),
            popup_loader_shape=list(getattr(
                getattr(popup.overview, "loader", None), "shape", ()) or ()))
        out.write_text(json.dumps(
            {"block": "G3.2b.5A.6", "blocked": "popup thumbnail never loaded",
             "records": REC}, indent=1, default=str))
        print("BLOCKED: the popup thumbnail never loaded; evidence written")
        for r in REC:
            if r["kind"].startswith(("nucleus.", "BLOCKED.", "project.",
                                     "popup.")):
                print("  ", json.dumps(r, default=str)[:400])
        print(f"wrote {out}")
        return 2
    bench.pump(2500)

    gl = next(iter(window.findChildren(gpu.Step1GpuLayer)), None)
    assert gl is not None
    wt = watcher(QtCore, gl)
    for widget in (window, window.viewer_tab, gl,
                   window._step1_mount.host.stack.controller.view.graphics.viewport()):
        widget.installEventFilter(wt)

    # ── ONE gesture ────────────────────────────────────────────────
    before_files = {str(p) for p in TMP.rglob("*") if p.is_file()}
    popup_n0 = len(popup.overview._patches)
    page_n0 = len(page._standard_patches(page._standard_rois()))
    ARMED.update(on=True, first=None)
    rec("gesture.start", popup_patches=popup_n0, page_patches=page_n0)
    bench.draw_patch(popup, _Probe(window), fraction=(0.30, 0.30, 0.60, 0.60))
    for _ in range(50):
        bench.pump(400)
        if committed or outcomes:
            break
    bench.pump(2500)
    ARMED["on"] = False
    rec("gesture.end")

    after_files = {str(p) for p in TMP.rglob("*") if p.is_file()}
    page_n1 = len(page._standard_patches(page._standard_rois()))
    checks = {
        "1_popup_added": len(popup.overview._patches) - popup_n0,
        "1_page_model_received": page_n1 - page_n0,
        "2_worker_outcomes": outcomes,
        "2_files_added": sorted(os.path.relpath(p, TMP)
                                for p in (after_files - before_files)),
        "3_geometry_committed_fired": len(committed),
        "3_main_adopted": sum(1 for r in REC
                              if r["kind"] == "main._on_patches.enter"),
        "4_submits": SEQ["submit"], "4_paints": SEQ["paint"],
        "4_first_display_change": ARMED["first"],
    }
    all_ok = (checks["1_popup_added"] == 1 and checks["1_page_model_received"] == 1
              and any(o[0] == "published" for o in outcomes)
              and checks["2_files_added"] and checks["3_geometry_committed_fired"])
    report = {"block": "G3.2b.5A.6", "one_gesture": True,
              "all_of_1_to_3_hold": bool(all_ok), "checks": checks,
              "records": REC,
              "honesty": [
                  "the popup was furnished by the product's own "
                  "furnish_navigator(); no signal was hand-connected",
                  "a source re-bind is reported only if a real entry was "
                  "called; a written .zattrs is not evidence of one",
                  "all four checks come from the SAME gesture",
              ]}
    out.write_text(json.dumps(report, indent=1, default=str))
    print(f"\n1) popup added={checks['1_popup_added']}  "
          f"page model received={checks['1_page_model_received']}")
    print(f"2) worker outcomes={outcomes}")
    print(f"   files added={checks['2_files_added']}")
    print(f"3) geometry_committed fired={checks['3_geometry_committed_fired']}")
    print(f"4) submits={SEQ['submit']} paints={SEQ['paint']}")
    first = ARMED["first"]
    if first is None:
        print("   NO display-state change during the gesture")
    else:
        print(f"   FIRST display change: {first['kind']} at {first['t_ms']}ms")
        for line in first["stack"]:
            print("      ", line)
    print(f"\nALL OF 1-3 HOLD: {all_ok}")
    print(f"wrote {out}")
    return 0


class _Probe:
    def __init__(self, window):
        self.w = window

    def mark(self, *_a, **_kw):
        return None


if __name__ == "__main__":
    sys.exit(main())
