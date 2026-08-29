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

from PyQt5.QtCore import QObject

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
        # BG tab has no per-channel display color; the grey swatch reads as a
        # dead checkbox next to the real one — hide it (user request).
        row.swatch.setVisible(False)
        is_nucleus = (cid == page.nucleus_channel)
        # Forward user interaction to the unchanged page slots. The page's
        # legacy lambda signature uses Qt CheckState ints.
        row.checkbox.stateChanged.connect(
            lambda state, name=cid: page._on_channel_checkbox_toggled(name, state))
        row.method_changed.connect(page._on_channel_method_changed)
        if is_nucleus:
            row.checkbox.setEnabled(False)
            row.method_cb.setEnabled(False)
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
                visible=(ch in page._channel_methods) and not is_nucleus,
                color=_hex(page._channel_colors.get(ch, "#888888")),
                locked=is_nucleus,
                bg_final_method=saved,
                bg_preview_method=page._channel_methods.get(ch),
                scope=SCOPE_PROCESSING,
            ))
        self.model.set_channels(states)

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
