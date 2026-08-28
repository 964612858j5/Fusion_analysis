"""Adapter: Step1 fusion ConfigPanel ↔ shared ChannelDock (weights view).

Additive: the group-based ConfigPanel stays the source of truth for fusion
config; the dock presents a flat per-channel weight view (color, name, weight
slider + numeric input) two-way synced with the ChannelWeightRow spinboxes.

Step1 rows show NO background-correction method and NO Min/Max/Gamma editors
(those are Step0 semantics).
"""

from PyQt5.QtCore import QObject

from .widgets.channel_dock import (
    ChannelDock, ChannelSetModel, ChannelState, WeightChannelRow,
    SCOPE_PROCESSING,
)

_GROUP_COLORS = ["#e06c75", "#98c379", "#61afef", "#e5c07b", "#c678dd"]


class Step1FusionDockAdapter(QObject):
    """Flat weights mirror of ConfigPanel's grouped ChannelWeightRows."""

    def __init__(self, config_panel, parent=None):
        super().__init__(parent or config_panel)
        self._panel = config_panel
        self._busy = False
        self.model = ChannelSetModel(self)
        self.dock = ChannelDock(
            self.model,
            row_factory=WeightChannelRow,
            title="Fusion Weights",
            show_search=True,
            show_bulk_buttons=False,   # visibility has no fusion semantic here
        )
        self.model.weight_changed.connect(self._on_dock_weight)
        config_panel.config_changed.connect(self.refresh)
        self.refresh()

    @staticmethod
    def channel_key(group: str, channel: str) -> str:
        return f"{group}:{channel}"

    def _iter_panel_rows(self):
        """Yield (cid, group, channel, ChannelWeightRow)."""
        for gi, (gname, gpanel) in enumerate(sorted(self._panel._panels.items())):
            for ch, row in gpanel._rows.items():
                yield self.channel_key(gname, ch), gname, ch, row

    # -- panel -> dock ------------------------------------------------------
    def refresh(self):
        if self._busy:
            return
        selected = self.model.selected()
        states = []
        for gi, (gname, gpanel) in enumerate(sorted(self._panel._panels.items())):
            color = _GROUP_COLORS[gi % len(_GROUP_COLORS)]
            for ch, row in gpanel._rows.items():
                states.append(ChannelState(
                    channel_id=self.channel_key(gname, ch),
                    name=f"{ch}  ({gname})",
                    color=color,
                    weight=float(row.weight()),
                    scope=SCOPE_PROCESSING,
                ))
        self._busy = True
        try:
            self.model.set_channels(states)
            if selected in self.model:
                self.model.select(selected)
        finally:
            self._busy = False

    # -- dock -> panel -------------------------------------------------------
    def _on_dock_weight(self, cid, weight):
        if self._busy:
            return
        for key, _g, _ch, row in self._iter_panel_rows():
            if key == cid:
                if abs(row.weight() - weight) > 1e-9:
                    self._busy = True
                    try:
                        row.spin.setValue(float(weight))
                    finally:
                        self._busy = False
                return
