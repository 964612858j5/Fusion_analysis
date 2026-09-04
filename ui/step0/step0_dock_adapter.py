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
        # Forward user interaction to the unchanged page slots. The page's
        # legacy lambda signature uses Qt CheckState ints.
        row.checkbox.stateChanged.connect(
            lambda state, name=cid: page._on_channel_checkbox_toggled(name, state))
        row.method_changed.connect(page._on_channel_method_changed)
        if is_nucleus:
            # The nucleus row's checkbox is NOT a processing checkbox: it is
            # the DAPI layer's show/hide switch (compare panels + full
            # image). It therefore stays enabled while the method combo does
            # not -- DAPI is never background-corrected, never enters
            # Process/Apply/on-demand/Save. `_on_channel_checkbox_toggled`
            # returns early for it, so no method is ever recorded.
            row.checkbox.setEnabled(True)
            row.checkbox.setToolTip("Show / hide the DAPI layer")
            row.method_cb.setEnabled(False)
            _dapi = getattr(page, "_on_nucleus_visibility_toggled", None)
            if _dapi is not None:
                row.checkbox.stateChanged.connect(
                    lambda state: _dapi(state == Qt.Checked))
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

        states = []
        for ch in page.loader.channel_names():
            is_nucleus = (ch == page.nucleus_channel)
            saved = (page._channel_decisions.get(ch)
                     or page._channel_methods.get(ch)
                     or getattr(page, "_method_all", None)
                     and page._method_all.currentText().lower()
                     or "both")
            states.append(ChannelState(
                channel_id=ch,
                name=f"{ch} ★" if is_nucleus else ch,
                visible=(_dapi_visible(page) if is_nucleus
                         else (ch in page._channel_methods)),
                color=_swatch_hex(page, ch),
                locked=is_nucleus,
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

        # One uniform name-column width = the longest name, so the method
        # combos align across rows while sitting right next to the names.
        rows = [self.dock.row(ch) for ch in self.model.order()]
        if rows:
            name_w = max(r.name_label.fontMetrics().boundingRect(
                r.name_label.text()).width() for r in rows) + 6
            for r in rows:
                r.name_label.setFixedWidth(name_w)

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
            first = next((ch for ch in page._channel_order
                          if ch != page.nucleus_channel), None)
            page.current_channel = first
            if first:
                lw.blockSignals(True)
                lw.setCurrentItem(page._channel_rows[first]["item"])
                lw.blockSignals(False)
