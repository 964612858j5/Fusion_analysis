"""Adapter: Step0 background correction ↔ the ONE public channel dock.

Step0 no longer builds a channel list. It BINDS to the public dock that
`Block01DisplayServices` owns and the main window mounts outside the stacked
pages, and it keeps doing the two things that are Step0's:

- installing the slide's channel order, capabilities and display visibility
  into `ChannelDisplayState` (one atomic install, unchanged from B4-A);
- answering the dock's Step0 accessory -- the correction method combo and the
  compute-state glyph -- out of the page's correction domain.

The legacy row registry (``page._channel_rows[ch]`` with keys ``checkbox``,
``label``, ``item``, ``method_cb``, ``row_widget``) is still populated, now
pointing at the PUBLIC row's widgets -- the ``badge``/``status_lbl`` entries
went with the empty label they named,
and ``page._channel_list`` still points at a QListWidget (the public dock's),
so the page's existing slots keep working against one list instead of two.

No correction/remap math, worker behaviour or config semantics change here.
"""

import weakref

from PyQt5.QtCore import QObject

from ...core.display_identity import ChannelCapabilities


def _hex(color) -> str:
    if isinstance(color, str):
        return color
    try:
        r, g, b = color[:3]
        return f"#{int(r):02x}{int(g):02x}{int(b):02x}"
    except Exception:
        return "#888888"


def _dapi_visible(page) -> bool:
    """Whether the DAPI layer is shown (the nucleus row's checkbox state)."""
    fn = getattr(page, "_nucleus_layer_visible", None)
    return True if fn is None else bool(fn())


def _compute_state(page, ch) -> str:
    """The row's compute-state glyph key, asked of the page when it can
    answer. The adapter describes the page, it does not decide for it, and a
    host that has no signature bookkeeping simply gets no glyph."""
    fn = getattr(page, "_channel_compute_state", None)
    return "" if fn is None else str(fn(ch))


def _landing_channel(page) -> str:
    """The channel a slide with nothing chosen yet is shown in, asked of the
    page when it can answer."""
    fn = getattr(page, "_landing_channel", None)
    if fn is not None:
        return fn()
    return next((ch for ch in page._channel_order
                 if ch != page.nucleus_channel), None)


def _swatch_hex(page, ch) -> str:
    """The channel's swatch colour, asked of the page when it can answer."""
    fn = getattr(page, "_channel_swatch_hex", None)
    if fn is not None:
        return fn(ch)
    return _hex(page._channel_colors.get(ch, "#888888"))


class Step0ChannelDockAdapter(QObject):
    """Binds Step0 to the ONE public channel dock. It owns no list."""

    def __init__(self, page):
        super().__init__(page)
        # WEAK, both ways. The dock belongs to Block01's lifetime and this
        # adapter answers its Step0 questions, so a strong page reference
        # here (reached through the providers the dock holds) would keep a
        # destroyed Step0 alive and let it answer for a slide it no longer
        # has. The adapter is parented to the page, so it dies with it; what
        # it hands the dock resolves the page weakly and fails closed.
        self._page_ref = weakref.ref(page)
        # THE public dock, from the object that owns Block01's lifetime. This
        # adapter used to construct a `ChannelDock` of its own here, which is
        # how Step0 and Step1 came to hold two public lists over the same
        # channels.
        self.dock = page.display.ensure_channel_dock()
        # WHAT STEP0 ANSWERS FOR, asked of the page rather than stored twice:
        # the correction decision is `_channel_decisions` (its one authority
        # since B4-A) and the compute state is derived from the page's
        # signature bookkeeping. Installed as ONE controller, which the dock
        # holds weakly and this adapter detaches when the page is released.
        self.dock.attach_step0_correction_controller(
            self,
            method=lambda ch: self._ask(lambda p: p._channel_row_method(ch),
                                        ""),
            state=lambda ch: self._ask(lambda p: _compute_state(p, ch), ""),
            name=lambda ch: self._ask(self._row_name_of(ch), str(ch)))
        self.dock.correction_method_changed.connect(
            self._on_correction_method_changed)

    # -- the page, while there still is one --------------------------------
    @property
    def _page(self):
        return self._page_ref()

    def detach(self):
        """Give the dock back its Step0 slot. Idempotent.

        Called from `Step0Page.release_block01_display`. After it, the dock
        holds no provider of this adapter's, no correction signal of this
        adapter's, and no row shows a correction answer -- while every public
        field (visibility, colour, selection, weight, participation) goes on
        working, because none of them was ever Step0's.
        """
        dock = self.dock
        if dock is None:
            return
        try:
            dock.correction_method_changed.disconnect(
                self._on_correction_method_changed)
        except (TypeError, RuntimeError):
            pass
        dock.detach_step0_correction_controller(self)

    # -- the Step0 accessory ------------------------------------------------
    def _ask(self, fn, fallback):
        """Ask the page, and answer nothing once the page is gone.

        The public dock outlives Step0, so every provider resolves the page
        first: a destroyed one raises inside a repaint, and a collected one
        is simply absent. Either way a Step0 answer with no owner is no
        answer, not a stale one.
        """
        page = self._page_ref()
        if page is None:
            return fallback
        try:
            return fn(page)
        except RuntimeError:                                # C++ side gone
            return fallback

    def _row_name_of(self, ch):
        return lambda page: (f"{ch} ★"
                             if ch == page.nucleus_channel else str(ch))

    def _row_name(self, ch):
        return self._ask(self._row_name_of(ch), str(ch))

    def _on_correction_method_changed(self, ch, text):
        """The dock's Step0 accessory moved: the page's correction domain is
        the only thing that may act on it.

        FAIL CLOSED once that page is gone. The dock outlives Step0 -- that is
        the point of one public dock -- so a correction decision arriving
        after the page was destroyed has nowhere authoritative to land, and
        writing it through a dangling pointer would be a decision nobody owns.
        """
        page = self._page_ref()
        if page is None:
            return
        try:
            handler = page._on_channel_method_changed
        except RuntimeError:                                # page destroyed
            return
        handler(ch, text)

    # -- rebuild (mirrors legacy _rebuild_channel_list) ------------------------
    def rebuild(self):
        page = self._page_ref()
        if page is None:
            return
        current = page.current_channel
        page._channel_rows.clear()
        page._channel_order = []
        if not page.loader:
            self.dock.set_channels([])
            return

        # WHAT IS SHOWN, and who already answered it. A dataset whose
        # display answers are resident -- a session restore, or A -> B -> A --
        # keeps every one of them; only a channel nobody has answered for
        # takes the default below.
        display = getattr(page, "display", None)
        known = dict(display.state.display_visibility()) if display else {}
        order_names = list(page.loader.channel_names())
        # THE ONE MARKER A NEW SLIDE SHOWS: the current marker, or the first
        # one on a slide nobody has chosen for. Every other marker starts
        # hidden -- a slide handed to Step1 with all of them stacked is not
        # what the user asked for -- and DAPI keeps its own default.
        visible_marker = current if (current and current in order_names
                                     and current != page.nucleus_channel) \
            else next((ch for ch in order_names
                       if ch != page.nucleus_channel), None)

        def _default_visible(ch):
            if ch in known:
                return bool(known[ch])
            if ch == page.nucleus_channel:
                return _dapi_visible(page)
            return ch == visible_marker

        visibility = {ch: _default_visible(ch) for ch in order_names}
        colors = {ch: _swatch_hex(page, ch) for ch in order_names}

        # ONE ATOMIC INSTALL of the display state this rebuild describes:
        # the channel order, the per-channel capabilities, and the ONE display
        # visibility Step0 actually knows -- written to Block01's shared state
        # before anything is announced. The dock follows that install; it is
        # not told separately, because a second telling is a second answer.
        if display is not None and display._ensure_binding() is not None:
            order = tuple(order_names)
            caps = {}
            for ch in order:
                is_nucleus = (ch == page.nucleus_channel)
                # SEPARATE FACTS, none of them derived from another. The
                # nucleus is shown and hidden like any other channel; what it
                # is NOT is background-correctable, sweepable by a bulk
                # Show all / Hide all, weight-editable or removable from the
                # fusion.
                caps[ch] = ChannelCapabilities(
                    is_nucleus=is_nucleus,
                    display_toggleable=True,
                    weight_editable=not is_nucleus,
                    correction_eligible=not is_nucleus,
                    bulk_toggleable=not is_nucleus,
                    fusion_toggleable=not is_nucleus,
                )
            payload = {
                "order": order,
                "capabilities": caps,
                "visibility": dict(visibility),
            }
            selection = (current if current in set(order)
                         else (page.nucleus_channel
                               if page.nucleus_channel in set(order)
                               else visible_marker))
            if selection:
                payload["selection"] = selection
            display.state.install(payload)

        # The dock draws the order the install just landed. Same universe,
        # same rows: a rebuild for the SAME channels keeps every row object,
        # the search text and the scroll position.
        self.dock.set_channels(order_names)
        for ch in order_names:
            row = self.dock.row(ch)
            if row is None:
                continue
            if display is None or display.state.channel_order() != tuple(order_names):
                # No binding to install into (a half-built page and its
                # tests): the dock still has to show what this rebuild
                # resolved, so the answers go to the rows directly.
                row.set_visible_state(visibility.get(ch, False))
                row.set_color(colors.get(ch, "#888888"))

        # Legacy registry: same keys the page code mutates directly, now
        # pointing at the PUBLIC row.
        for ch in self.dock.channel_order():
            row = self.dock.row(ch)
            page._channel_rows[ch] = {
                "checkbox": row.checkbox,
                "label": row.name_label,
                "item": self.dock.item(ch),
                "method_cb": row.method_cb,
                "row_widget": row,
            }
            page._channel_order.append(ch)
            page._refresh_channel_row(ch)

        # Restore selection with the legacy rules.
        lw = self.dock.list_widget
        if current in page._channel_rows:
            page.current_channel = current
            lw.blockSignals(True)
            lw.setCurrentItem(page._channel_rows[current]["item"])
            lw.blockSignals(False)
        else:
            # No channel chosen yet -- a freshly loaded slide. The page says
            # what that lands on (`_landing_channel`: DAPI), and the row for
            # it is selected, so the list agrees with the picture.
            landing = _landing_channel(page)
            page.current_channel = landing
            if landing and landing in page._channel_rows:
                lw.blockSignals(True)
                lw.setCurrentItem(page._channel_rows[landing]["item"])
                lw.blockSignals(False)
