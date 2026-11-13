"""The Regions outlines' look (the user's acceptance notes 2026-11-12,
docs/v17_acceptance_notes_plan.md B26): bound cores and exclusions drawn
3 px thick, a selected shape at least that over a white halo. Offscreen,
with the page tests' synthetic project and a rendered overlay.
"""

import pytest

from test_tma_f3_page import _centre, _line, _page, _set_target, app, env  # noqa: F401
from test_regions_r3 import _open_exclusions, _square

from block01.ui.regions.overlay import BOUND_WIDTH, SELECTED_HALO, SELECTED_WIDTH


def test_bound_cores_and_exclusions_are_drawn_thick(app, env, monkeypatch):
    page = _page(app, env, monkeypatch)
    _set_target(page, "X1-Y01", "X1-Y03", "Along column")
    _line(page, (1, 1), (1, 3))
    page.cores.numbering.btn_confirm.click()
    d = page.holder.d
    items, _sel, _h, _o = page.cores.draw_parts()
    by_id = {it["id"]: it for it in items}
    bound = [p for p, piece in d["pieces"].items()
             if piece["owner"] and d["cores"][piece["owner"]]["position_key"]]
    unbound = [p for p, piece in d["pieces"].items()
               if piece["owner"] and not d["cores"][piece["owner"]]["position_key"]]
    assert bound and unbound
    assert all(by_id[p]["width"] == BOUND_WIDTH == 3.0 for p in bound)
    assert all(by_id[p]["width"] == BOUND_WIDTH for p in unbound)     # every core 3 px (B28)
    _open_exclusions(page)
    _square(page, 100, 100, 80)
    page._key("enter")
    ex = page.exclusions.draw_items()
    assert ex and all(it["width"] == BOUND_WIDTH and it["hatch"] for it in ex)
    page.shutdown()


def _render(app, selected):
    """A square outline (level 0 == view coordinates), rendered; the image."""
    from test_f5_core_display import _view
    from block01.ui.regions.overlay import RegionsOverlay
    view = _view(app)
    view.view_box.setRange(xRange=(0, 400), yRange=(0, 400), padding=0)
    app.processEvents()
    ov = RegionsOverlay(view.graphics, view.view_box, None)
    item = {"id": "s", "polygon": [[100, 100], [300, 100], [300, 300], [100, 300]], "color": "#43a047"}
    ov.set_items([item], selected={"s"} if selected else ())
    ov.resize(view.graphics.viewport().size())
    img = ov.grab().toImage()
    ov.detach()
    view.graphics.close()
    return img


def _whitest_near_the_left_edge(img):
    """The brightest grey pixel in a band across the square's left edge, row
    in the middle (the halo is white; the outline green; outside is clear)."""
    y = img.height() // 2
    best = 0
    for x in range(max(0, img.width() // 4 - 12), img.width() // 4 + 12):
        c = img.pixelColor(x, y)
        if c.alpha() > 0 and abs(c.red() - c.green()) < 30 and abs(c.green() - c.blue()) < 30:
            best = max(best, c.red())
    return best


def test_a_selected_shape_has_a_white_halo(app):
    assert SELECTED_WIDTH >= BOUND_WIDTH and SELECTED_HALO > 0
    plain = _render(app, selected=False)
    chosen = _render(app, selected=True)
    assert _whitest_near_the_left_edge(plain) < 120                    # no white around a plain one
    assert _whitest_near_the_left_edge(chosen) > 200                   # the halo


# ── B27: the screen-space hatch; run cores hatched green ─────────────────

def _render_items(app, items, x_range):
    from test_f5_core_display import _view
    from block01.ui.regions.overlay import RegionsOverlay
    view = _view(app)
    view.view_box.setRange(xRange=x_range, yRange=x_range, padding=0)
    app.processEvents()
    ov = RegionsOverlay(view.graphics, view.view_box, None)
    ov.set_items(items)
    ov.resize(view.graphics.viewport().size())
    img = ov.grab().toImage()
    ov.detach()
    view.graphics.close()
    return img


def _row_runs(img, y, x0, x1, colour):
    """(number of hatch lines crossed, widths in pixels) along row y between
    x0 and x1: runs of pixels close to `colour` and nearly opaque."""
    from block01.qt import QtGui
    ref = QtGui.QColor(colour)
    runs, cur = [], 0
    for x in range(x0, x1):
        c = img.pixelColor(x, y)
        near = (c.alpha() > 150 and abs(c.red() - ref.red()) < 60 and abs(c.green() - ref.green()) < 60
                and abs(c.blue() - ref.blue()) < 60)
        if near:
            cur += 1
        elif cur:
            runs.append(cur)
            cur = 0
    if cur:
        runs.append(cur)
    return runs


def test_the_screen_hatch_keeps_its_width_and_thins_out_when_zoomed_out(app):
    sq = {"id": "e", "polygon": [[100, 100], [300, 100], [300, 300], [100, 300]], "color": "#e040fb",
          "hatch": True, "hatch_style": "screen", "width": 3.0}
    near = _render_items(app, [sq], (50, 350))                 # the square fills most of the view
    far = _render_items(app, [sq], (-800, 1200))               # zoomed out: the square is small
    y_near, y_far = near.height() // 2, far.height() // 2
    w = near.width()
    lines_near = _row_runs(near, y_near, int(w * 0.25), int(w * 0.75), "#e040fb")
    # the square spans (100..300) of (-800..1200): 45%..55% of the width
    lines_far = _row_runs(far, y_far, int(w * 0.455), int(w * 0.545), "#e040fb")
    assert len(lines_near) > len(lines_far) >= 1               # fewer lines on the smaller shape
    assert max(lines_near) <= 4 and max(lines_far) <= 4         # same thin width at both zooms
    # the translucent fill: every pixel inside is tinted (between the lines too), at both zooms
    assert min(near.pixelColor(x, y_near).alpha() for x in range(int(w * 0.3), int(w * 0.7))) > 0
    assert min(far.pixelColor(x, y_far).alpha() for x in range(int(w * 0.47), int(w * 0.53))) > 0


def test_an_old_hatch_without_the_style_is_unchanged(app):
    """Step2's skipped tiles pass `hatch` without the style: Qt's pattern, no fill."""
    sq = {"id": "t", "polygon": [[100, 100], [300, 100], [300, 300], [100, 300]], "color": "#c678dd",
          "hatch": True}
    img = _render_items(app, [sq], (50, 350))
    alphas = {img.pixelColor(x, img.height() // 2).alpha() for x in range(img.width() // 3, img.width() // 2)}
    assert 0 in alphas                                         # no fill under Qt's pattern


def test_cores_are_never_hatched_and_exclusions_are(app, env, monkeypatch):
    """The screen hatch is for the exclusions only (the user, 2026-11-12):
    a core, run or not, is an outline without a hatch."""
    from block01.core.regions import document as D
    page = _page(app, env, monkeypatch)
    _set_target(page, "X1-Y01", "X1-Y03", "Along column")
    _line(page, (1, 1), (1, 3))
    page.cores.numbering.btn_confirm.click()
    d = page.holder.d
    keys = [c["position_key"] for c in d["cores"].values() if c["position_key"]]
    page.holder.do(D.set_run, keys[0], True)
    page.holder.do(D.set_run, keys[1], False)
    items = page.cores.draw_parts()[0]
    assert items and not any(it.get("hatch") or it.get("hatch_style") for it in items)
    _open_exclusions(page)
    _square(page, 100, 100, 80)
    page._key("enter")
    ex = page.exclusions.draw_items()
    assert ex and all(it["hatch"] and it["hatch_style"] == "screen" for it in ex)
    page.shutdown()
