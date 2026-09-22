"""G3.2b.4B1.2.3b: where the Full Image <-> Compare round trip loses its place.

Ten rounds at ONE fixed viewport pixel, through the real widget stack: a real
`ExploreView`, a real `ViewBox`, a real `GraphicsScene`, and real
`QMouseEvent`s -- because the whole question is whether screen -> scene ->
view agrees with where the page then puts the camera, and only the real
widget can answer that. Handing a fixed level-0 point in instead is exactly
the mistake that hid this from the older tests: a user's hand holds a fixed
SCREEN pixel, which is a different world point every round.

Per round it records every stage the user's place passes through:

    full_before            the full image's camera before the right-click
    screen_pos             the (fixed) mouse pixel
    right_click_level0     the level-0 point under it THIS round
    compare_after_entry    the panels' camera once entry has settled
    compare_before_exit    the panels' camera just before leaving
    full_immediate         the full image's camera when `_exit_compare_mode`
                           returns
    full_after_settle      ...and after Qt's layout has settled

...plus, for each seam, the level-0 centre delta, that delta IN SCREEN
PIXELS, and the scale ratio -- so a finding names the stage it belongs to
instead of a total.

Nothing here changes production behaviour: the page is driven only through
the public gestures (`_send_right_click`, `_exit_compare_mode`).

Usage: python scripts/diagnose_step0_b123b_camera.py OUT.json [label]
"""

import importlib.util
import json
import os
import pathlib
import sys

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

ROOT = pathlib.Path(__file__).resolve().parents[1]
if "block01" not in sys.modules:
    _spec = importlib.util.spec_from_file_location(
        "block01", ROOT / "__init__.py", submodule_search_locations=[str(ROOT)])
    _module = importlib.util.module_from_spec(_spec)
    sys.modules["block01"] = _module
    _spec.loader.exec_module(_module)
sys.path.insert(0, str(ROOT.parent))
sys.path.insert(0, str(ROOT / "tests"))

from PyQt5 import QtCore, QtTest, QtWidgets  # noqa: E402

APP = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])

# The drift suite's own real-widget rig, borrowed rather than rebuilt: one
# rig for one page, so a change there cannot leave this script measuring a
# different page from the one the gates guard.
import test_step0_compare_toggle_drift as RIG  # noqa: E402


def seam(before, after, label):
    """One stage's loss, in level-0 units AND in screen pixels."""
    if before is None or after is None:
        return {"stage": label, "measurable": False}
    dcx = float(after[0]) - float(before[0])
    dcy = float(after[1]) - float(before[1])
    scale = float(after[2])
    return {
        "stage": label,
        "measurable": True,
        "d_level0": (round(dcx, 6), round(dcy, 6)),
        "d_screen_px": (round(dcx * scale, 6), round(dcy * scale, 6)),
        "scale_ratio": round(float(after[2]) / float(before[2]), 12),
    }


def run(page, pixel, rounds=10, move=None):
    """`rounds` right-click round trips at `pixel`, recording every stage."""
    out = []
    for index in range(rounds):
        full_before = page._full_image_camera()
        level0 = RIG._level0_under(page, pixel)
        RIG._send_right_click(page, pixel)
        RIG._settle(page)
        if not page._compare_mode():
            out.append({"round": index, "entered": False})
            continue
        compare_after_entry = page._compare_camera()
        if move is not None:
            move(page)
            RIG._settle(page)
        compare_before_exit = page._compare_camera()
        page._exit_compare_mode()
        full_immediate = page._full_image_camera()
        RIG._settle(page)
        full_after_settle = page._full_image_camera()
        out.append({
            "round": index,
            "entered": True,
            "full_before": full_before,
            "screen_pos": (pixel.x(), pixel.y()),
            "right_click_level0": level0,
            "compare_after_entry": compare_after_entry,
            "compare_before_exit": compare_before_exit,
            "full_immediate_after_exit": full_immediate,
            "full_after_layout_settle": full_after_settle,
            "seams": [
                seam(full_before, compare_after_entry, "full_before->entry"),
                seam(compare_after_entry, compare_before_exit,
                     "entry->before_exit"),
                seam(compare_before_exit, full_immediate,
                     "before_exit->full_immediate"),
                seam(full_immediate, full_after_settle,
                     "full_immediate->after_settle"),
                seam(full_before, full_after_settle,
                     "ROUND TRIP full_before->after_settle"),
            ],
            "entry_lands_on_click_point_px": (
                round((compare_after_entry[0] - level0[0])
                      * compare_after_entry[2], 6),
                round((compare_after_entry[1] - level0[1])
                      * compare_after_entry[2], 6)),
        })
    return out


def summarise(rounds):
    walked = [r for r in rounds if r.get("full_after_layout_settle") is not None]
    skipped = [r["round"] for r in rounds
               if r.get("full_after_layout_settle") is None]
    if not walked:
        return {"rounds": 0, "rounds_that_never_entered": skipped}
    first, last = walked[0]["full_before"], walked[-1]["full_after_layout_settle"]
    scale = float(last[2])
    return {
        "rounds": len(walked),
        "rounds_that_never_entered": skipped,
        "first_full_camera": first,
        "last_full_camera": last,
        "total_walk_level0": (round(last[0] - first[0], 6),
                              round(last[1] - first[1], 6)),
        "total_walk_screen_px": (round((last[0] - first[0]) * scale, 6),
                                 round((last[1] - first[1]) * scale, 6)),
        "scale_ratio_over_all_rounds": round(float(last[2]) / float(first[2]),
                                             12),
        "max_abs_round_trip_screen_px": max(
            max(abs(v) for v in r["seams"][4]["d_screen_px"]) for r in walked),
        "max_abs_exit_application_screen_px": max(
            max(abs(v) for v in r["seams"][2]["d_screen_px"]) for r in walked),
        "max_abs_layout_settle_screen_px": max(
            max(abs(v) for v in r["seams"][3]["d_screen_px"]) for r in walked),
        "max_abs_panels_moved_by_themselves_screen_px": max(
            max(abs(v) for v in r["seams"][1]["d_screen_px"]) for r in walked),
        "max_abs_entry_off_click_point_screen_px": max(
            max(abs(v) for v in r["entry_lands_on_click_point_px"])
            for r in walked),
    }


def main():
    out = pathlib.Path(sys.argv[1] if len(sys.argv) > 1 else "b123b.json")
    label = sys.argv[2] if len(sys.argv) > 2 else "g32b4b123b"

    report = {"label": label, "cases": {}, "honesty": [
        "real ExploreView, real ViewBox, real GraphicsScene, real "
        "QMouseEvent at a FIXED viewport pixel -- not a fixed level-0 point",
        "the page is driven only through its public gestures",
        "synthetic slide from the compare suite's rig; no real data is read",
    ]}

    # ── A. off-centre pixel, the user's reported gesture ───────────────
    page = RIG._real_page(APP)
    rounds = run(page, RIG.FIXED_PIXEL, rounds=10)
    report["cases"]["A_offcentre_pixel_10_rounds"] = {
        "pixel": (RIG.FIXED_PIXEL.x(), RIG.FIXED_PIXEL.y()),
        "summary": summarise(rounds), "rounds": rounds}

    # ── B. the integer pixel NEAREST the centre (the half-pixel story) ──
    page2 = RIG._real_page(APP)
    vp = RIG._viewport(page2)
    centre_px = QtCore.QPointF(float(int(vp.width() / 2)),
                               float(int(vp.height() / 2)))
    rounds2 = run(page2, centre_px, rounds=10)
    report["cases"]["B_nearest_centre_pixel_10_rounds"] = {
        "pixel": (centre_px.x(), centre_px.y()),
        "viewport_px": (vp.width(), vp.height()),
        "summary": summarise(rounds2), "rounds": rounds2}

    # ── C. the user DID move the panels: the camera must be adopted ────
    page3 = RIG._real_page(APP)

    def pan(page):
        camera = page._compare_camera()
        page._compare_strip_widget.set_camera(camera[0] + 1500.0,
                                              camera[1] + 900.0, camera[2])

    rounds3 = run(page3, RIG.FIXED_PIXEL, rounds=3, move=pan)
    report["cases"]["C_panned_each_round"] = {
        "summary": summarise(rounds3), "rounds": rounds3}

    # ── D. a genuine SUB-PIXEL offset from the centre ──────────────────
    # Case B's viewport is 1028x470, whose centre (514.0, 235.0) an integer
    # pixel names EXACTLY -- so B measures zero quantisation even before the
    # fix and, on its own, is a gate that cannot fail. The older suite
    # described a half-screen-pixel step on a 1091-wide viewport; this rig's
    # offscreen layout pins the viewport at 1028x470 and resizing the page
    # does not move it (measured: 1200..1301 all give 1028), so that exact
    # geometry cannot be reproduced here. What CAN be reproduced is the
    # phenomenon it is about: a click half a screen pixel off the centre.
    page4 = RIG._real_page(APP)
    vp4 = RIG._viewport(page4)
    half = QtCore.QPointF(vp4.width() / 2.0 + 0.5, vp4.height() / 2.0 + 0.5)
    rounds4 = run(page4, half, rounds=10)
    report["cases"]["D_half_pixel_off_centre_10_rounds"] = {
        "pixel": (half.x(), half.y()),
        "viewport_px": (vp4.width(), vp4.height()),
        "true_centre_px": (vp4.width() / 2.0, vp4.height() / 2.0),
        "note": ("the older suite's 1091-wide viewport cannot be reproduced "
                 "here -- this rig's layout pins the viewport at 1028x470 "
                 "whatever the page is resized to -- so the sub-pixel offset "
                 "is applied directly instead"),
        "summary": summarise(rounds4), "rounds": rounds4}

    out.write_text(json.dumps(report, indent=2, default=str))
    print(f"wrote {out}")
    for name, case in report["cases"].items():
        s = case["summary"]
        print(f"{name}: walk_px={s['total_walk_screen_px']} "
              f"scale_ratio={s['scale_ratio_over_all_rounds']} "
              f"exit_apply_px={s['max_abs_exit_application_screen_px']:.4f} "
              f"layout_px={s['max_abs_layout_settle_screen_px']:.4f} "
              f"panels_self_px={s['max_abs_panels_moved_by_themselves_screen_px']:.4f} "
              f"entry_off_click_px={s['max_abs_entry_off_click_point_screen_px']:.4f}")


if __name__ == "__main__":
    main()
