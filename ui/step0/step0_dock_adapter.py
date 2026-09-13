"""Adapter: Step0 background-correction page ↔ shared ChannelDock.

Replaces the hand-rolled QListWidget in the BG tab with the shared dock while
preserving the page's data model and slot contract untouched:

- the legacy row registry (``page._channel_rows[ch]`` with keys ``checkbox``,
  ``label``, ``badge``, ``item``, ``method_cb``, ``status_lbl``,
  ``row_widget``) is still populated, so ``_refresh_channel_row``,
  ``_set_channel_computing`` and ``_set_channel_done`` keep working;
- ``page._channel_list`` still points at a QListWidget (the dock's), so
  selection code using ``setCurrentItem``/``currentRowChanged`` is unchanged;
- checkbox toggles and method-combo changes are forwarded to the existing
  page slots.

No correction/remap math, worker behavior, or config semantics change here.
"""

from PyQt5.QtCore import QObject, Qt

from ...core.display_identity import ChannelCapabilities
from ..widgets.channel_dock import (
    ChannelDock, ChannelSetModel, ChannelState, Step0ChannelRow,
    SCOPE_PROCESSING,
)


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
    answer. Same shape as `_swatch_hex` / `_dapi_visible`: the adapter
    describes the page, it does not decide for it, and a host that has no
    signature bookkeeping simply gets no glyph."""
    fn = getattr(page, "_channel_compute_state", None)
    return "" if fn is None else str(fn(ch))


def _landing_channel(page) -> str:
    """The channel a slide with nothing chosen yet is shown in, asked of the
    page when it can answer. Same shape as `_swatch_hex` / `_dapi_visible`:
    the adapter describes the page, it does not decide for it, and a host
    without the landing rule gets the pre-landing one (the first marker)."""
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
    """Owns the shared dock instance mounted in Step0's BG tab."""

    def __init__(self, page):
        super().__init__(page)
        self._page = page
        self.model = ChannelSetModel(self)
        self.dock = ChannelDock(
            self.model,
            row_factory=self._make_row,
            title="",                    # page wraps the dock in its own group box
            show_search=True,
            show_bulk_buttons=False,     # Step0 keeps its legacy "All" checkbox
        )

    # -- row construction --------------------------------------------------
    def _make_row(self, model, cid):
        page = self._page
        row = Step0ChannelRow(model, cid)
        # Every channel carries its own display-colour swatch here now (the
        # colour buttons that used to sit in the Patch Preview header are
        # gone). Row order: checkbox · swatch · name · method combo.
        row.swatch.setVisible(True)
        _swatch = getattr(page, "_on_channel_swatch_clicked", None)
        if _swatch is not None:
            row.color_clicked.connect(_swatch)
        # The right-edge status badge column (★ on nucleus, — boxes elsewhere)
        # adds no information here — computing/done are shown by the row
        # background and the green checkbox. Hidden, not removed: legacy code
        # still writes text into it harmlessly.
        row.status_lbl.setVisible(False)
        is_nucleus = (cid == page.nucleus_channel)
        # THE CHECKBOX IS DISPLAY VISIBILITY, for every channel including the
        # nucleus. It used to carry two answers at once: the row's own
        # `_on_visibility_toggled` wrote the dock model's `visible` while the
        # page's slot wrote a CORRECTION decision, so ticking a marker
        # assigned it a background-correction method and unticking it wrote
        # `original`. A correction decision is made in the method combo or
        # the selected-channel inspector; this box shows and hides.
        row.checkbox.stateChanged.connect(
            lambda state, name=cid: page._on_channel_visibility_toggled(
                name, state == Qt.Checked))
        row.checkbox.setToolTip(
            "Show / hide the DAPI layer" if is_nucleus
            else "Show / hide this channel")
        row.method_changed.connect(page._on_channel_method_changed)
        # A REAL CLICK on the row, told apart from a programmatic selection:
        # clicking a hidden channel's name means "show me this one", while a
        # session restore or a dataset switch selecting it does not.
        clicked = getattr(row, "row_clicked", None)
        handler = getattr(page, "_on_channel_row_clicked", None)
        if clicked is not None and handler is not None:
            clicked.connect(handler)
        # The checkbox and the method combo are enabled by the row itself,
        # from `display_toggleable` and `correction_eligible`. This adapter
        # used to overrule both by hand from `is_nucleus`, which is how the
        # capabilities stayed a comment rather than a dependency.
        return row

    # -- rebuild (mirrors legacy _rebuild_channel_list) ------------------------
    def rebuild(self):
        page = self._page
        current = page.current_channel
        page._channel_rows.clear()
        page._channel_order = []
        if not page.loader:
            self.model.set_channels([])
            return

        # WHAT IS SHOWN, and who already answered it. A dataset whose
        # display answers are resident -- a session restore, or A -> B -> A --
        # keeps every one of them; only a channel nobody has answered for
        # takes the default below.
        display = getattr(page, "display", None)
        known = dict(display.state.display_visibility()) if display else {}
        order_names = list(page.loader.channel_names())
        # FROM THE LOADER'S ORDER, not `page._channel_order`: that list is
        # emptied at the top of this method, so asking the page for its
        # landing channel here answers None.
        #
        # THE ONE MARKER A NEW SLIDE SHOWS. The page LANDS on DAPI -- that is
        # the viewing rule -- but DAPI is the nucleus and has its own default;
        # the marker that is current (or, on a slide nobody has chosen one
        # for, the first) is the one whose display answer starts as shown, so
        # selecting it is a picture rather than an empty frame. Every other
        # marker starts hidden: a slide handed to Step1 with all of them
        # stacked is not what the user asked for.
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

        states = []
        for ch in order_names:
            is_nucleus = (ch == page.nucleus_channel)
            # THE FINAL DECISION the row will show: the page's one answer,
            # which is `original` for a channel nobody has assigned. It used
            # to fall back to the bulk box's method, so a fresh row came up
            # claiming a correction the page did not intend and Save did not
            # write.
            saved = page._channel_row_method(ch)
            states.append(ChannelState(
                channel_id=ch,
                name=f"{ch} ★" if is_nucleus else ch,
                visible=_default_visible(ch),
                color=_swatch_hex(page, ch),
                locked=is_nucleus,
                # THE SAME THREE FACTS the shared state is given below, so
                # the row's permissions and Block01's capabilities cannot
                # drift apart: one is a copy of the other, made here.
                display_toggleable=True,
                correction_eligible=not is_nucleus,
                bulk_toggleable=not is_nucleus,
                bg_final_method=saved,
                bg_preview_method=page._channel_methods.get(ch),
                # The compute state is DERIVED from the page's signature
                # bookkeeping, never stored twice: the row is seeded with
                # the current answer here and re-asked on every change
                # (see `page._refresh_channel_state`).
                status=_compute_state(page, ch),
                scope=SCOPE_PROCESSING,
            ))
        self.model.set_channels(states)

        # ONE ATOMIC INSTALL of the display state this rebuild describes:
        # the channel order, the per-channel capabilities, and the ONE display
        # visibility Step0 actually knows -- written to Block01's shared state
        # before anything is announced.
        #
        # FROM THE LOADER, NOT `page._channel_order`. That list is emptied at
        # the top of this method and refilled further down, in the legacy
        # registry loop, so reading it here installed an empty order, empty
        # capabilities and an empty visibility map.
        # A page that has never been through a dataset commit still has a
        # path, and an install needs a namespace to go into. Binding lazily
        # here is what makes "the order is always installed" true for the
        # landing state as well as after a Load.
        if display is not None and display._ensure_binding() is not None:
            order = tuple(st.channel_id for st in states)
            caps = {}
            for ch in order:
                is_nucleus = (ch == page.nucleus_channel)
                caps[ch] = ChannelCapabilities(
                    is_nucleus=is_nucleus,
                    # SEPARATE FACTS, none of them derived from another. The
                    # nucleus is shown and hidden like any other channel; what
                    # it is NOT is background-correctable, sweepable by a bulk
                    # Show all / Hide all, or removable from the fusion.
                    display_toggleable=True,
                    weight_editable=not is_nucleus,
                    correction_eligible=not is_nucleus,
                    bulk_toggleable=not is_nucleus,
                    fusion_toggleable=not is_nucleus,
                )
            # ONE INSTALL: the order, the capabilities, the display
            # visibility this rebuild resolved AND the selection, so nothing
            # observes a slide whose channels are known but whose answers are
            # not. Every channel now has a display answer -- an answer
            # already given is kept, and only a channel nobody has answered
            # for takes the landing default.
            payload = {
                "order": order,
                "capabilities": caps,
                "visibility": {st.channel_id: bool(st.visible)
                               for st in states},
            }
            # The page's landing rule decides what a slide with nothing
            # chosen opens on (DAPI); the row loop below puts the list on the
            # same channel, so the selection installed here is that answer.
            selection = (current if current in set(order)
                         else (page.nucleus_channel
                               if page.nucleus_channel in set(order)
                               else visible_marker))
            if selection:
                payload["selection"] = selection
            display.state.install(payload)

        # The uniform name-column width is the DOCK's now
        # (`template.uniform_name_width`, applied in `ChannelDock.rebuild`),
        # so both channel lists compute it the same way and this adapter no
        # longer reaches into the rows to set it.

        # Legacy registry: same keys/widgets the page code mutates directly.
        for ch in self.model.order():
            row = self.dock.row(ch)
            page._channel_rows[ch] = {
                "checkbox": row.checkbox,
                "label": row.name_label,
                "badge": row.status_lbl,
                "item": self.dock.item(ch),
                "method_cb": row.method_cb,
                "status_lbl": row.status_lbl,
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
            if landing:
                lw.blockSignals(True)
                lw.setCurrentItem(page._channel_rows[landing]["item"])
                lw.blockSignals(False)
