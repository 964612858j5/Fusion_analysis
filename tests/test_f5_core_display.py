"""TMA cores outside the Regions page (docs/f5_tma_display_plan.md parts 2
and 9.1): the frozen display list in the snapshot (green and yellow, as at
the freeze, never in the identity), the overlay items (one per piece, the
sheet's name on the first, no bridge lines), the read-only overlay with its
own label threshold, and Step2's overview coordinates."""

import os

import numpy as np
import pytest

from test_regions_r1 import SHAPE, SID, _finished, _place, project  # noqa: F401

from block01.core.regions import analysis as A
from block01.core.regions import display as RD
from block01.core.regions import document as D
from block01.core.regions import geometry as G
from block01.core.regions import lock as K


def _some_ticked(n=3, named=True):
    s, d = _finished("identity")
    keys = sorted(d["positions"])
    for key in keys[:n]:
        D.set_run(d, key, True)
    if named:
        for p in s["map_sheet"]["positions"]:
            if p["donor_block_id"]:
                p["annotation"] = f"A{p['position_id']}"
    return s, d


def test_the_snapshot_freezes_every_core_green_or_yellow_outside_its_identity(project):
    s, d = _some_ticked()
    K.save(project, d, s)
    snap = A.build_snapshot(project, SID, ["DAPI"], study=s)
    shown = snap["display_cores"]
    assert len(shown) == len(d["cores"])
    run = {c["uid"] for c in snap["cores"]}
    assert {c["uid"] for c in shown if c["run"]} == run and len(run) == 3
    assert all(c["reason"] for c in shown if not c["run"])            # why each yellow one is
    assert all(c["name"].startswith("X") and c["name"].split(" ")[1].startswith("D")
               for c in shown if c["position_key"])           # "X1-Y01 D11": the Regions page's name
    ident = A.identity(snap)
    snap2 = dict(snap, display_cores=[])
    assert A.identity(snap2) == ident == snap["identity"]              # drawing only
    path = A.save_snapshot(os.path.join(project, "r", "regions_snapshot.json"), snap)
    assert A.load_snapshot(path)["display_cores"] == shown


def test_a_later_save_never_changes_what_a_frozen_run_shows(project):
    s, d = _some_ticked()
    K.save(project, d, s)
    snap = A.build_snapshot(project, SID, ["DAPI"], study=s)
    before = RD.snapshot_items(snap)
    for key in sorted(d["positions"]):
        D.set_run(d, key, False)
    D.set_run(d, sorted(d["positions"])[-1], True)
    K.save(project, d, s)                                              # Save B: other cores run
    assert RD.snapshot_items(snap) == before
    greens = [i for i in before if i["color"] == RD.GREEN]
    assert len(greens) == 3 and len(before) > len(greens)


def test_items_are_one_per_piece_named_once_and_unlinked_cores_get_no_name(project):
    s, d = _some_ticked()
    uid = next(iter(d["cores"]))
    c = d["cores"][uid]
    cy, cx = _place(1, 1)
    extra = D.add_piece(d, G.circle_polygon(cy + 5, cx + 5, 20, SHAPE))
    D.merge(d, uid, extra)                                             # a second piece of that core
    assert len(d["cores"][uid]["pieces"]) == 2
    items = RD.saved_items(d, s, {uid})
    mine = [i for i in items if i["id"].startswith(uid + "#")]
    assert len(mine) == len(d["cores"][uid]["pieces"])
    assert [("label" in i) for i in mine] == [True] + [False] * (len(mine) - 1)
    assert all(i["color"] == RD.GREEN for i in mine)
    assert all(i["color"] == RD.YELLOW for i in items if not i["id"].startswith(uid + "#"))
    unlinked = RD.saved_items(d, None, ())                             # no sheet: no names at all
    assert unlinked and not any("label" in i for i in unlinked)


def test_an_old_snapshot_shows_its_run_cores_green_with_the_bridges_taken_out():
    a = [[0, 0], [10, 0], [10, 10], [0, 10]]
    b = [[30, 0], [40, 0], [40, 10], [30, 10]]
    c = [[0, 30], [10, 30], [10, 40], [0, 40]]
    joined = G.keyhole([a, b, c])
    rings = RD.split_keyhole(joined)
    canon = lambda r: sorted(map(tuple, r))                           # noqa: E731 -- start/direction free
    assert sorted(map(canon, rings)) == sorted(map(canon, [a, b, c]))
    assert RD.split_keyhole(a) == [a]
    old = {"cores": [{"uid": "u1", "position_key": "k", "polygon": joined, "label": "X1-Y06",
                      "donor_block_id": "K2024"}]}
    items = RD.snapshot_items(old)
    assert len(items) == 3 and all(i["color"] == RD.GREEN for i in items)
    assert [i.get("label") for i in items] == ["X1-Y06 K2024", None, None]


def test_the_union_outline_of_a_real_multi_piece_core_splits_back_without_bridges():
    pieces = [G.circle_polygon(100, 100, 30, (1000, 1000)), G.circle_polygon(100, 300, 30, (1000, 1000))]
    joined = G.region_outline(pieces, (1000, 1000))["polygon_fullres"]
    rings = RD.split_keyhole(joined)
    assert len(rings) == 2
    for r in rings:                                                    # each ring stays on one circle
        xs = [q[0] for q in r]
        assert max(xs) - min(xs) < 80


@pytest.fixture(scope="module")
def app():
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    pytest.importorskip("PyQt5")
    from PyQt5 import QtWidgets
    return QtWidgets.QApplication.instance() or QtWidgets.QApplication([])


def _view(app):
    import types
    import pyqtgraph as pg
    gv = pg.GraphicsLayoutWidget()
    vb = gv.addViewBox()
    vb.invertY(True)
    gv.resize(400, 400)
    gv.show()
    app.processEvents()
    return types.SimpleNamespace(graphics=gv, view_box=vb)


def test_the_outlines_are_read_only_and_follow_the_switch(app):
    from PyQt5 import QtCore
    from PyQt5.QtCore import Qt
    from block01.ui.regions.core_outlines import CORE_LABEL_MIN_PX, CoreOutlines
    view = _view(app)
    items = [{"id": "u#0", "polygon": [[10, 10], [50, 10], [50, 50]], "color": RD.GREEN, "label": "N"}]
    on = {"v": True}
    co = CoreOutlines(lambda: view, lambda: items, lambda: on["v"])
    assert co.refresh() == 1 and co.overlay.items == items
    assert co.overlay.label_min_px == CORE_LABEL_MIN_PX == 60
    assert co.overlay.testAttribute(Qt.WA_TransparentForMouseEvents)
    ev = QtCore.QEvent(QtCore.QEvent.MouseButtonPress)
    assert co.overlay.eventFilter(view.graphics.viewport(), ev) is False   # goes on to the view
    on["v"] = False
    assert co.refresh() == 1 and co.overlay.items == []                   # off: drawn nothing
    co2 = CoreOutlines(lambda: None, lambda: items, lambda: True)
    assert co2.refresh() == 0 and co2.overlay is None


def test_step2_maps_slide_pixels_by_its_inputs_origin_and_stride(app):
    from block01.ui.regions.core_outlines import CoreOutlines
    view = _view(app)
    oy, ox, ds = 1000, 3000, 4
    items = [{"id": "u#0", "polygon": [[3040, 1080], [3080, 1080], [3080, 1120]], "color": RD.GREEN}]
    co = CoreOutlines(lambda: view, lambda: items, lambda: True,
                      to_view=lambda x, y: ((x - ox) / ds, (y - oy) / ds))
    co.refresh()
    assert co.overlay.items[0]["polygon"] == [[10.0, 20.0], [20.0, 20.0], [20.0, 30.0]]


def test_the_regions_page_keeps_its_own_label_threshold(app):
    from block01.ui.regions.overlay import LABEL_MIN_PX, RegionsOverlay
    view = _view(app)
    ov = RegionsOverlay(view.graphics, view.view_box, None)
    assert ov.label_min_px == LABEL_MIN_PX == 14
    ov.detach()


def test_a_failed_refresh_leaves_nothing_stale_on_screen(app):
    from block01.ui.regions.core_outlines import CoreOutlines
    view = _view(app)
    items = [{"id": "u#0", "polygon": [[10, 10], [50, 10], [50, 50]], "color": RD.GREEN}]
    state = {"fail": False}

    def source():
        if state["fail"]:
            raise OSError("the Save went away")
        return items
    co = CoreOutlines(lambda: view, source, lambda: True)
    co.refresh()
    assert co.overlay.items == items
    state["fail"] = True
    with pytest.raises(OSError):
        co.refresh()
    assert co.overlay.items == []


def test_step2s_page_draws_at_its_inputs_own_origin(app, monkeypatch, tmp_path):
    from block01.core import tile_tissue
    from block01.ui.step2_page import Step2Page
    page = Step2Page()
    page._zarr_path = str(tmp_path / "fused.zarr")
    page._ov_h, page._ov_w, page._ov_ds = 100, 100, 4
    monkeypatch.setattr(tile_tissue, "fused_region", lambda p: ((1000, 1400, 3000, 3400), "s", "DAPI"))
    page.saved_core_items = lambda: [{"id": "u#0", "polygon": [[3040, 1080], [3080, 1080], [3080, 1120]],
                                      "color": RD.YELLOW}]
    assert page.refresh_core_outlines() == 1
    assert page._core_outlines.overlay.items[0]["polygon"] == [[10.0, 20.0], [20.0, 20.0], [20.0, 30.0]]
    assert not page.btn_tma_cores.isHidden()
    snap = {"display_cores": [{"uid": "u", "polygons": [[[3000, 1000], [3010, 1000], [3010, 1010]]],
                               "name": "N", "run": True}]}
    page._run_core_snapshot = snap                                     # the run's frozen cores win
    page.refresh_core_outlines()
    assert page._core_outlines.overlay.items[0]["color"] == RD.GREEN
    page.btn_tma_cores.setChecked(False)                               # the switch
    assert page._core_outlines.overlay.items == []
    page.saved_core_items = lambda: []
    page._run_core_snapshot = None
    page.refresh_core_outlines()
    assert page.btn_tma_cores.isHidden()                               # nothing to draw: no switch
