"""The Compare panels show ONE channel: its pixels, its window, its colour.

Two faults were measured on the real page and are pinned here.

  * A channel whose whole-slide overview has not been read yet has no display
    window, and the placeholder standing in for one is 0..1 — which, published
    as a window over 8/16-bit data, saturates every pixel. That is the
    over-exposed panel the user sees on first click; switching away and back
    "fixes" it because by then the record is resident. A placeholder is now
    never published: each panel keeps the range it derives from its own
    overview until the real numbers arrive.
  * Recolouring the channel on screen changed nothing. The only route from a
    colour change to the panels ran through `_refresh_preview_display`, inside
    a branch that required a legacy payload — and in compare mode there is
    none.

Colour and window are also tagged with the channel they belong to, so a
switch that is still pending cannot repaint the pixels it is still showing in
the colour or the window of the channel that has not arrived.

Own module: page-heavy PyQt suites crash pyqtgraph offscreen when combined.
"""

import os

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

pytest.importorskip("PyQt5")

from PyQt5 import QtWidgets  # noqa: E402

import test_step0_compare_tiles as T  # noqa: E402  the built harness


@pytest.fixture(scope="module")
def app():
    return QtWidgets.QApplication.instance() or QtWidgets.QApplication([])


@pytest.fixture
def page_strip(app):
    page = T._page(app)
    strip = T._enter(page)
    try:
        yield page, strip
    finally:
        page.close()


def _select(page, channel):
    page._on_channel_row_changed(page._channel_order.index(channel))


def _mappings(strip):
    return [list(c.mappings) for c in strip.controllers]


def _tints(strip):
    return [list(c.tints) for c in strip.controllers]


def test_every_panel_follows_the_selected_channel(page_strip):
    """A ticked-but-unselected marker never becomes what the panels show."""
    page, strip = page_strip
    page._channel_methods["CD20"] = "tophat"        # ticked for processing

    for wanted in ("CD3", "CD20", "CD3"):
        _select(page, wanted)
        assert page.current_channel == wanted
        for controller in strip.controllers:
            assert controller.channel == wanted
        assert strip._displayed_channel == wanted


def test_a_window_the_page_does_not_have_is_not_published(page_strip):
    """0..1 is "no window read yet", not a window. Publishing it saturates
    every pixel of the channel it is published for."""
    page, strip = page_strip
    page._slide_lowres_array = lambda ch, blocking=True, resident_only=False: None
    page._preview_provider = None
    wb = page._cond_workbench
    for ch in ("CD3", "CD20"):
        wb._params.pop(ch, None)
        wb._user_adjusted.pop(ch, None)
    page._display_seeded.clear()
    page._display_fallback.clear()

    assert page._display_mapping_for("CD20") == (0.0, 1.0, 1.0)
    assert page._display_mapping_is_real("CD20") is False

    before = _mappings(strip)
    page.current_channel = "CD20"
    page._refresh_preview_display()

    assert _mappings(strip) == before


def test_the_window_is_published_once_it_is_real(page_strip):
    page, strip = page_strip
    # The window is seeded from the whole-slide array, and that array is
    # READ IN THE BACKGROUND now -- the page no longer takes it on the
    # GUI thread from inside a seed. Warmed here, which is what the
    # read thread does a moment after the channel appears.
    page._slide_lowres_array("CD20")
    _select(page, "CD20")
    assert page._display_mapping_is_real("CD20") is True

    published = [m for m in _mappings(strip)[0] if m[3] == "CD20"]
    assert published, "the real window never reached the panels"
    lo, hi, _gamma, _ch = published[-1]
    assert (lo, hi) != (0.0, 1.0)


def test_the_window_arrives_before_the_pixels(page_strip):
    """One publication, not two: the panels know the new channel's window
    when its overview and tiles are installed, rather than a moment after."""
    page, strip = page_strip
    # The window is seeded from the whole-slide array, and that array is
    # READ IN THE BACKGROUND now -- the page no longer takes it on the
    # GUI thread from inside a seed. Warmed here, which is what the
    # read thread does a moment after the channel appears.
    page._slide_lowres_array("CD20")
    page._slide_lowres_array("CD3")
    _select(page, "CD20")            # make CD20's window known
    _select(page, "CD3")

    order = []
    for controller in strip.controllers:
        for name in ("set_display_mapping", "set_selection"):
            real = getattr(controller, name)

            def record(*a, _real=real, _name=name, **k):
                order.append((_name, k.get("channel")))
                return _real(*a, **k)
            setattr(controller, name, record)

    _select(page, "CD20")

    first_map = next(i for i, (n, ch) in enumerate(order)
                     if n == "set_display_mapping" and ch == "CD20")
    first_pixels = next(i for i, (n, _c) in enumerate(order)
                        if n == "set_selection")
    assert first_map < first_pixels


def test_a_colour_change_reaches_the_panels_at_once(page_strip):
    page, strip = page_strip
    _select(page, "CD3")
    before = _tints(strip)
    reads = len(page._compare_builds)

    page._apply_channel_color("CD3", (1.0, 0.0, 0.0))

    after = _tints(strip)
    assert [len(a) - len(b) for a, b in zip(after, before)] == [1, 1, 1]
    assert all(t[-1] == (1.0, 0.0, 0.0) for t in after)
    assert len(page._compare_builds) == reads      # nothing rebuilt


def test_recolouring_the_channel_on_screen_moves_no_camera_and_reads_nothing(page_strip):
    page, strip = page_strip
    _select(page, "CD3")
    cams = [c.view.view_box.viewRange() for c in strip.controllers]
    page._display_mapping_for("CD3")             # warm what is already known
    cached = dict(page._slide_lowres)
    reads = []
    real = page._slide_lowres_array

    def counted(ch, blocking=True, resident_only=False):
        if ch not in cached:
            reads.append(ch)
        return real(ch, blocking=blocking, resident_only=resident_only)
    page._slide_lowres_array = counted

    page._apply_channel_color("CD3", (0.0, 1.0, 0.0))

    assert reads == [], "a colour change read a channel off the slide"
    assert [c.view.view_box.viewRange() for c in strip.controllers] == cams


def test_recolouring_another_channel_leaves_the_panels_alone(page_strip):
    """...and the new colour is what that channel is drawn in when it is
    selected."""
    page, strip = page_strip
    _select(page, "CD3")
    before = [t[-1] if t else None for t in _tints(strip)]

    page._apply_channel_color("CD20", (1.0, 0.0, 1.0))

    assert [t[-1] if t else None for t in _tints(strip)] == before

    _select(page, "CD20")
    assert all(t[-1] == (1.0, 0.0, 1.0) for t in _tints(strip))


def test_a_colour_for_another_channel_is_held_until_that_channel_is_shown(app):
    """The controller's own rule, in isolation: a tagged colour for a channel
    it is not showing is remembered, not painted."""
    from block01.viewer.explore_view import ExploreController

    c = ExploreController.__new__(ExploreController)
    c._host_tint = {}
    c._tint = (1.0, 1.0, 1.0)
    c.channel = "CD3"

    try:
        ExploreController.set_tint(c, (1.0, 0.0, 0.0), channel="CD20")
    except AttributeError:
        pass                       # no view/pools on a bare object: fine
    assert c._tint == (1.0, 1.0, 1.0)
    assert c._host_tint["CD20"] == (1.0, 0.0, 0.0)


def test_zoom_lock_does_not_change_any_of_this(page_strip):
    page, strip = page_strip
    for locked in (True, False):
        holder = getattr(page, "_btn_zoom_lock", None)
        if holder is not None:
            holder.setChecked(locked)
        _select(page, "CD3")
        _select(page, "CD20")
        for controller in strip.controllers:
            assert controller.channel == "CD20"
        page._apply_channel_color("CD20", (0.2, 0.4, 0.6))
        assert all(t[-1] == (0.2, 0.4, 0.6) for t in _tints(strip))
