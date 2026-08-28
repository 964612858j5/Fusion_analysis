"""Adapter: Step3 QC display settings ↔ shared ChannelDock (display-only).

Contract (v15 plan §11.2): Step3 rows show visibility, color and name only;
the selected-channel tool area provides Min/Max/Gamma explicitly labeled
display/QC only. Everything edited through this adapter stays in a
display-settings dict shaped like Step3's ``_channel_settings``
({key: {visible, color, opacity, contrast}}); it must never be written into
processing or segmentation configs.

Phase 1A status: adapter + test contract only — not yet mounted in Step3Page.
"""

from PyQt5.QtCore import QObject, pyqtSignal

from .widgets.channel_dock import (
    ChannelDock, ChannelSetModel, ChannelState, DisplayChannelRow,
    Step3Inspector, SCOPE_DISPLAY_ONLY,
)

# Keys that may legally appear in the display dict this adapter writes.
DISPLAY_KEYS = frozenset({"visible", "color", "opacity", "contrast",
                          "display_min", "display_max", "display_gamma"})


class Step3DisplayDockAdapter(QObject):
    """Display/QC-only channel dock over a `_channel_settings`-shaped dict."""

    display_settings_changed = pyqtSignal(str)   # channel key

    def __init__(self, settings: dict, parent=None):
        super().__init__(parent)
        self._settings = settings
        self.model = ChannelSetModel(self)
        self.dock = ChannelDock(
            self.model,
            row_factory=DisplayChannelRow,
            title="Channel Overlay",
        )
        self.inspector = Step3Inspector()
        self.dock.set_tool_widget(self.inspector)

        self.model.visibility_changed.connect(self._on_visible)
        self.model.color_changed.connect(self._on_color)
        self.model.display_changed.connect(self._on_display)
        self.model.selection_changed.connect(self._on_selected)
        self.inspector.remap.params_changed.connect(self._on_inspector_params)
        self.refresh()

    # -- settings -> dock ----------------------------------------------------
    def refresh(self):
        states = []
        for key, cfg in self._settings.items():
            cfg = cfg or {}
            states.append(ChannelState(
                channel_id=key,
                visible=bool(cfg.get("visible", True)),
                color=str(cfg.get("color", "#888888")),
                display_min=cfg.get("display_min"),
                display_max=cfg.get("display_max"),
                display_gamma=float(cfg.get("display_gamma", 1.0)),
                scope=SCOPE_DISPLAY_ONLY,
            ))
        self.model.set_channels(states)

    # -- dock -> settings (display keys only, never processing config) -------
    def _write(self, key, field, value):
        cfg = self._settings.setdefault(key, {})
        assert field in DISPLAY_KEYS, f"non-display field write blocked: {field}"
        cfg[field] = value
        self.display_settings_changed.emit(key)

    def _on_visible(self, cid, visible):
        self._write(cid, "visible", bool(visible))

    def _on_color(self, cid, color):
        self._write(cid, "color", color)

    def _on_display(self, cid):
        st = self.model.get(cid)
        if st is None:
            return
        self._write(cid, "display_min", st.display_min)
        self._write(cid, "display_max", st.display_max)
        self._write(cid, "display_gamma", st.display_gamma)

    def _on_selected(self, cid):
        st = self.model.get(cid) if cid else None
        if st is not None:
            self.inspector.remap.set_values(
                st.display_min, st.display_max, st.display_gamma)

    def _on_inspector_params(self, dmin, dmax, gamma):
        cid = self.model.selected()
        if cid:
            self.model.set_display(cid, dmin, dmax, gamma)
