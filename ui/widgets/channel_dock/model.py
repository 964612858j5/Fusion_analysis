"""Shared per-channel state model and signal contract (v15 Workstream A).

The model carries the union of per-page fields so channel identity, order,
color, visibility and selection transfer consistently between steps. Each page
consumes only the fields in its semantic scope:

- Step0 (processing): bg_preview_method, bg_final_method, status,
  display min/max/gamma (remap preview).
- Step1 (processing): weight.
- Step3 (display-only): visible/color + display min/max/gamma; must never be
  written into processing/segmentation configs.
"""

from dataclasses import dataclass, field
from typing import Dict, List, Optional

from PyQt5.QtCore import QObject, pyqtSignal

SCOPE_PROCESSING = "processing"
SCOPE_DISPLAY_ONLY = "display-only"


@dataclass
class ChannelState:
    channel_id: str
    name: str = ""
    visible: bool = True
    color: str = "#888888"
    locked: bool = False              # e.g. nucleus channel: not toggleable
    display_min: Optional[float] = None
    display_max: Optional[float] = None
    display_gamma: float = 1.0
    bg_preview_method: Optional[str] = None   # Step0: method shown in preview
    bg_final_method: Optional[str] = None     # Step0: user's selected decision
    status: str = ""                  # e.g. "", "computing", "done", "unsaved"
    weight: Optional[float] = None    # Step1 fusion weight (0..1)
    scope: str = SCOPE_PROCESSING

    def __post_init__(self):
        if not self.name:
            self.name = self.channel_id


class ChannelSetModel(QObject):
    """Ordered channel collection with change signals.

    Persistence-agnostic: it never reads or writes config files; adapters
    subscribe to signals and decide what (if anything) to persist.
    """

    reset = pyqtSignal()
    visibility_changed = pyqtSignal(str, bool)
    color_changed = pyqtSignal(str, str)
    selection_changed = pyqtSignal(str)          # "" = nothing selected
    display_changed = pyqtSignal(str)            # min/max/gamma of channel
    bg_preview_changed = pyqtSignal(str, str)
    bg_final_changed = pyqtSignal(str, str)
    status_changed = pyqtSignal(str, str)
    weight_changed = pyqtSignal(str, float)

    def __init__(self, parent=None):
        super().__init__(parent)
        self._channels: Dict[str, ChannelState] = {}
        self._order: List[str] = []
        self._selected: str = ""

    # -- structure -----------------------------------------------------
    def set_channels(self, channels: List[ChannelState]):
        self._channels = {c.channel_id: c for c in channels}
        self._order = [c.channel_id for c in channels]
        if self._selected not in self._channels:
            self._selected = ""
        self.reset.emit()

    def order(self) -> List[str]:
        return list(self._order)

    def get(self, cid: str) -> Optional[ChannelState]:
        return self._channels.get(cid)

    def channels(self) -> List[ChannelState]:
        return [self._channels[c] for c in self._order]

    def __contains__(self, cid) -> bool:
        return cid in self._channels

    def __len__(self) -> int:
        return len(self._order)

    # -- selection -----------------------------------------------------
    def selected(self) -> str:
        return self._selected

    def select(self, cid: str):
        if cid and cid not in self._channels:
            return
        if cid == self._selected:
            return
        self._selected = cid or ""
        self.selection_changed.emit(self._selected)

    # -- per-channel setters --------------------------------------------
    def set_visible(self, cid: str, visible: bool):
        ch = self._channels.get(cid)
        if ch is None or ch.visible == bool(visible):
            return
        ch.visible = bool(visible)
        self.visibility_changed.emit(cid, ch.visible)

    def set_all_visible(self, visible: bool):
        for cid in self._order:
            ch = self._channels[cid]
            if not ch.locked:
                self.set_visible(cid, visible)

    def set_color(self, cid: str, color: str):
        ch = self._channels.get(cid)
        if ch is None or ch.color == color:
            return
        ch.color = color
        self.color_changed.emit(cid, color)

    def set_display(self, cid: str, dmin=None, dmax=None, gamma=None):
        ch = self._channels.get(cid)
        if ch is None:
            return
        changed = False
        if dmin is not None and ch.display_min != dmin:
            ch.display_min = float(dmin); changed = True
        if dmax is not None and ch.display_max != dmax:
            ch.display_max = float(dmax); changed = True
        if gamma is not None and ch.display_gamma != gamma:
            ch.display_gamma = float(gamma); changed = True
        if changed:
            self.display_changed.emit(cid)

    def set_bg_preview(self, cid: str, method: str):
        ch = self._channels.get(cid)
        if ch is None or ch.bg_preview_method == method:
            return
        ch.bg_preview_method = method
        self.bg_preview_changed.emit(cid, method)

    def set_bg_final(self, cid: str, method: str):
        ch = self._channels.get(cid)
        if ch is None or ch.bg_final_method == method:
            return
        ch.bg_final_method = method
        self.bg_final_changed.emit(cid, method)

    def set_status(self, cid: str, status: str):
        ch = self._channels.get(cid)
        if ch is None or ch.status == status:
            return
        ch.status = status
        self.status_changed.emit(cid, status)

    def set_weight(self, cid: str, weight: float):
        ch = self._channels.get(cid)
        if ch is None:
            return
        w = max(0.0, min(1.0, float(weight)))
        if ch.weight is not None and abs(ch.weight - w) < 1e-9:
            return
        ch.weight = w
        self.weight_changed.emit(cid, w)
