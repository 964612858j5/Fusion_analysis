"""Step3's mask controls and results window, controls only
(`ui/step3_mask_bar.py`, `ui/step3_results_window.py`; multi-result plan
§3.1).

  * a mask's panel: no Show, no Fill; colour, opacity, width 1..4 and
    Solid / Dashed each emit ONE change naming only its field; presets and
    `Custom…` (answered directly -- no modal box) set the colour; the
    swatch follows;
  * the window: one row per record with Cells / Nuclei ticks (greyed when
    the record lacks that mask), a swatch each, `Use` (the active row says
    `In use`), `×` only where deletable; ticks emit `ticks_changed` and
    `ticked()` lists them top first, cells before nuclei;
  * default colours are given once per entry from Step1's palette: a new
    record never recolours an older one; nuclei start dashed;
  * ticks and styles survive a new list; `forget` drops a record's;
  * "all Cells" ticks every row that has cells, half-ticked when rows
    disagree; `tick` (the default and `Load…`) leaves a missing mask alone;
  * the hint is cut to fit and shows its whole text on hover.
"""

import os

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt5 import QtCore, QtGui, QtWidgets  # noqa: E402

from block01.ui import step3_mask_bar as mb  # noqa: E402
from block01.ui import step3_results_window as rw  # noqa: E402
from block01.ui.step1_presegmentation.mask_layers import PALETTE  # noqa: E402


@pytest.fixture(scope="module")
def app():
    return QtWidgets.QApplication.instance() or QtWidgets.QApplication([])


@pytest.fixture
def panel(app):
    made = mb.MaskPanel("cell")
    made.changes = []
    made.changed.connect(made.changes.append)
    return made


def _rows(*keys, has=None):
    return [rw.RowInfo(key=k, run_dir=f"/r/{k}", title=f"run {k}", deletable=(k == "a"),
                       has=(has or {}).get(k, ("cell", "nucleus"))) for k in keys]


@pytest.fixture
def win(app):
    made = rw.Step3ResultsWindow()
    made.events = []
    made.ticks_changed.connect(lambda: made.events.append("ticks"))
    made.style_changed.connect(lambda k, kind, ch: made.events.append((k, kind, ch)))
    made.use_requested.connect(lambda k: made.events.append(("use", k)))
    made.delete_requested.connect(lambda d: made.events.append(("delete", d)))
    yield made
    made.deleteLater()


def test_the_panel_has_no_show_or_fill_and_emits_one_change_per_field(panel):
    assert not hasattr(panel, "show_box") and not hasattr(panel, "fill")
    assert panel.colour == (0.0, 1.0, 0.0)
    assert panel.opacity.value() == 75 and panel.width.value() == 1
    assert panel.width.minimum() == 1 and panel.width.maximum() == 4
    assert panel.solid.isChecked() and not panel.dashed.isChecked()
    panel.opacity.setValue(40)
    panel.width.setValue(3)
    panel.dashed.setChecked(True)
    panel.solid.setChecked(True)
    assert panel.changes == [{"alpha": 0.4}, {"width": 3.0}, {"dash": True}, {"dash": False}]
    assert panel.opacity_value.text() == "40 %"


def test_presets_and_custom_set_the_colour_and_the_swatch_follows(app):
    button = mb.SwatchButton("nucleus")
    panel = button.panel
    changes = []
    panel.changed.connect(changes.append)
    rgb, swatch = panel.swatches[2]                      # yellow
    swatch.click()
    assert changes[-1] == {"color": rgb} and panel.colour == rgb
    swatch.click()                                       # the same colour: nothing new
    assert len(changes) == 1
    panel.pick_custom_colour = lambda initial: QtGui.QColor(10, 20, 30)
    panel.custom.click()
    assert changes[-1]["color"] == pytest.approx((10 / 255, 20 / 255, 30 / 255))
    icon = button.icon().pixmap(12, 12).toImage()
    assert QtGui.QColor(icon.pixel(6, 6)).getRgb()[:3] == (10, 20, 30)
    panel.pick_custom_colour = lambda initial: QtGui.QColor()    # cancelled
    panel.custom.click()
    assert len(changes) == 2


def test_rows_ticks_use_and_delete(win):
    assert win.empty.isVisible() is False or not win.keys()
    win.set_rows(_rows("a", "b", has={"b": ("nucleus",)}), active_key="b")
    a, b = win.row("a"), win.row("b")
    assert b.btn_use.text() == "In use" and not b.btn_use.isEnabled()
    assert a.btn_use.text() == "Use" and a.btn_use.isEnabled()
    assert a.btn_delete is not None and b.btn_delete is None
    assert not b.ticks["cell"].isEnabled() and b.ticks["nucleus"].isEnabled()
    assert win.ticked() == []
    b.ticks["nucleus"].setChecked(True)
    a.ticks["nucleus"].setChecked(True)
    a.ticks["cell"].setChecked(True)
    assert win.events == ["ticks"] * 3
    assert win.ticked() == [("a", "cell"), ("a", "nucleus"), ("b", "nucleus")]
    a.btn_use.click()
    a.btn_delete.click()
    assert win.events[-2:] == [("use", "a"), ("delete", "/r/a")]
    win.set_active("a")
    assert a.btn_use.text() == "In use" and b.btn_use.text() == "Use"


def test_colours_are_given_once_and_kept_with_the_ticks_across_a_new_list(win):
    def colour(hexa):
        c = QtGui.QColor(hexa)
        return (c.redF(), c.greenF(), c.blueF())
    win.set_rows(_rows("a"))
    assert win.style("a", "cell").color == colour(PALETTE[0])
    assert win.style("a", "nucleus").color == colour(PALETTE[1])
    assert win.style("a", "nucleus").dash and not win.style("a", "cell").dash
    win.row("a").ticks["cell"].setChecked(True)
    win.row("a").swatches["cell"].panel.width.setValue(2)
    assert win.events[-1] == ("a", "cell", {"width": 2.0})
    win.set_rows(_rows("new", "a"))                    # a newer record arrives on top
    assert win.style("a", "cell").color == colour(PALETTE[0]), "an older row was recoloured"
    assert win.style("new", "cell").color == colour(PALETTE[2])
    assert win.style("a", "cell").width == 2.0 and win.row("a").ticks["cell"].isChecked()
    assert win.ticked() == [("a", "cell")]
    win.forget(["a"])
    assert win.keys() == ["new"] and win.ticked() == []
    # wraps round the palette
    win.set_rows(_rows(*[f"k{i}" for i in range(6)]))
    # "a" and "new" were seen first: k5 is the eighth entry
    assert win.style("k5", "nucleus").color == colour(PALETTE[(2 * 7 + 1) % len(PALETTE)])


def test_all_ticks_and_the_programmatic_tick(win):
    win.set_rows(_rows("a", "b", "c", has={"c": ("nucleus",)}))
    all_cells = win.all_ticks["cell"]
    assert all_cells.checkState() == QtCore.Qt.Unchecked
    win.row("a").ticks["cell"].setChecked(True)
    assert all_cells.checkState() == QtCore.Qt.PartiallyChecked
    all_cells.click()                                   # from half-ticked: all
    assert win.ticked() == [("a", "cell"), ("b", "cell")]
    assert all_cells.checkState() == QtCore.Qt.Checked
    all_cells.click()
    assert win.ticked() == []
    assert win.tick("c", "cell") is False, "a missing mask was ticked"
    assert win.tick("c", "nucleus") is True and win.ticked() == [("c", "nucleus")]


def test_the_hint_is_cut_with_its_whole_text_on_hover(win):
    long = "Mask memory is full: " + "a very long reason " * 10
    win.hint.resize(120, 20)
    win.set_hint(long)
    assert win.hint.toolTip() == long and win.hint.full_text() == long
    assert win.hint.text().endswith("…") and len(win.hint.text()) < len(long)
    win.set_hint("")
    assert win.hint.text() == ""


def test_a_long_upstream_never_pushes_use_out_of_the_row(win, app):
    long = "10-25 05:26 · DAPI input · CD206 1, CD34 1, CD45 1, CD68 1, CD163 1, CK19 1" * 2
    win.set_rows([rw.RowInfo("a", "/r/a", "mesmer_nuclear_guided · 10-26 05:42", tag=long)])
    win.resize(420, 400)
    win.show()
    app.processEvents()
    row = win.row("a")
    try:
        right = row.btn_use.mapTo(row, row.btn_use.rect().topRight()).x()
        assert row.btn_use.isVisible() and right <= row.width(), (right, row.width())
        assert row.tag.full_text() == long and row.tag.text().endswith("…")
    finally:
        win.close()
