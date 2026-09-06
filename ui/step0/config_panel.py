"""
block01/ui/step0/config_panel.py — Step1's ONE channel panel.

One flat channel list, one state owner. Each row carries the three things a
channel has in Step1:

    click the row  -> it becomes the CURRENT channel (what Intensity edits)
    the checkbox   -> whether it takes part in the multi-channel overlay
    the weight box -> its 0..1 contribution to the FUSION preview

Those are three separate states on purpose. Ticking a channel does not change
which channel is current, and a weight of 0 does not hide a channel from the
overlay: the overlay is display (colour + Min/Max/Gamma), the weight is fusion.

Channel GROUPS are still here, they are just not on screen any more. Group
membership and group weight remain in the model and in every file that carries
them, because folding `group_weight x channel_weight` into one number would
change old projects, the HQ workers, Mesmer and the on-disk fusion. New
projects get a single group at weight 1.0 and never notice.
"""

import os
import json

from PyQt5.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QLabel, QPushButton,
    QMessageBox, QFileDialog, QCheckBox, QSlider,
    QDoubleSpinBox, QListWidget, QListWidgetItem, QColorDialog,
)
from PyQt5.QtCore import pyqtSignal, Qt, QSize
from PyQt5.QtGui import QColor

from ...config import NUCLEUS_CONFIG

DEFAULT_GROUP = "markers"

# Dealt by channel order to any channel nobody has picked a colour for. Same
# spirit as Step0's palette: a channel has a colour before anyone chooses one.
_PALETTE = [
    "#4d96ff", "#6bcb77", "#ff6b6b", "#ffd93d", "#c678dd",
    "#19e0e0", "#ff9f45", "#98c379", "#e06c75", "#61afef",
]

_LIST_STYLE = """
QListWidget { background:#101620; border:1px solid #253246; border-radius:4px; }
QListWidget::item { border-bottom:1px solid #202c3b; }
QListWidget::item:hover { background:#1a3e33; }
QListWidget::item:selected { background:#1a2b3e; }
"""

ROW_HEIGHT = 26


class ChannelRow(QWidget):
    """One channel: select / show / weight."""

    selected = pyqtSignal(str)
    visibility_toggled = pyqtSignal(str, bool)
    weight_edited = pyqtSignal(str)
    color_clicked = pyqtSignal(str)

    def __init__(self, channel, weight=0.0, visible=False, color="#888888",
                 parent=None):
        super().__init__(parent)
        self.channel = channel
        self._busy = False

        lay = QHBoxLayout(self)
        lay.setContentsMargins(6, 2, 6, 2)
        lay.setSpacing(5)

        self.checkbox = QCheckBox()
        self.checkbox.setChecked(bool(visible))
        self.checkbox.setToolTip("Show this channel in the overlay")
        self.checkbox.toggled.connect(self._on_toggled)
        lay.addWidget(self.checkbox)

        self.swatch = QLabel()
        self.swatch.setFixedSize(13, 13)
        self.swatch.setCursor(Qt.PointingHandCursor)
        self.swatch.setToolTip("Click to change this channel's overlay colour")
        lay.addWidget(self.swatch)

        self.name_label = QLabel(channel)
        self.name_label.setStyleSheet("color:#dce5ef;font-size:11px;")
        self.name_label.setMinimumWidth(52)
        lay.addWidget(self.name_label, stretch=1)

        self.slider = QSlider(Qt.Horizontal)
        self.slider.setRange(0, 100)
        self.slider.setValue(int(round(float(weight) * 100)))
        self.slider.setFixedHeight(16)
        self.slider.setMinimumWidth(60)
        self.slider.setToolTip("Fusion weight (not used by the overlay)")
        lay.addWidget(self.slider, stretch=2)

        self.spin = QDoubleSpinBox()
        self.spin.setRange(0.0, 1.0)
        self.spin.setSingleStep(0.05)
        self.spin.setDecimals(2)
        self.spin.setValue(float(weight))
        self.spin.setFixedWidth(56)
        self.spin.setAlignment(Qt.AlignRight)
        self.spin.setStyleSheet(
            "QDoubleSpinBox{background:#182230;color:#dce5ef;"
            "border:1px solid #354a63;border-radius:3px;font-size:10px;}")
        lay.addWidget(self.spin)

        self.slider.valueChanged.connect(self._on_slider)
        self.spin.valueChanged.connect(self._on_spin)
        self.setStyleSheet("*{background:transparent;}")
        self.set_color(color)

    # -- weight (the spinbox is the authority; the slider follows it) -------
    def weight(self):
        return float(self.spin.value())

    def set_weight(self, value):
        if abs(self.weight() - float(value)) < 1e-9:
            return
        self._busy = True
        try:
            self.spin.setValue(float(value))
            self.slider.setValue(int(round(float(value) * 100)))
        finally:
            self._busy = False

    def _on_slider(self, v):
        if self._busy:
            return
        self._busy = True
        try:
            self.spin.setValue(v / 100.0)
        finally:
            self._busy = False
        self.weight_edited.emit(self.channel)

    def _on_spin(self, v):
        if self._busy:
            return
        self._busy = True
        try:
            self.slider.setValue(int(round(float(v) * 100)))
        finally:
            self._busy = False
        self.weight_edited.emit(self.channel)

    # -- visibility ---------------------------------------------------------
    def is_visible(self):
        return self.checkbox.isChecked()

    def set_weight_editable(self, editable):
        """A read-only row still shows its weight; it just cannot be moved."""
        self.slider.setEnabled(bool(editable))
        self.spin.setReadOnly(not editable)
        self.spin.setButtonSymbols(
            QDoubleSpinBox.UpDownArrows if editable else QDoubleSpinBox.NoButtons)
        self.slider.setToolTip(
            "Fusion weight (not used by the overlay)" if editable else
            "The nucleus weight comes from Step0 and is read-only here.")

    def set_visible(self, visible):
        if self.checkbox.isChecked() == bool(visible):
            return
        self.checkbox.blockSignals(True)
        self.checkbox.setChecked(bool(visible))
        self.checkbox.blockSignals(False)

    def _on_toggled(self, checked):
        self.visibility_toggled.emit(self.channel, bool(checked))

    # -- colour -------------------------------------------------------------
    def set_color(self, color):
        self._color = color
        self.swatch.setStyleSheet(
            f"background:{color};border:1px solid #354a63;border-radius:2px;")

    def color(self):
        return self._color

    # -- selection ----------------------------------------------------------
    def mousePressEvent(self, ev):
        self.selected.emit(self.channel)
        super().mousePressEvent(ev)

    def mouseReleaseEvent(self, ev):
        if self.swatch.geometry().contains(ev.pos()):
            self.color_clicked.emit(self.channel)
        super().mouseReleaseEvent(ev)


class ConfigPanel(QWidget):
    """The one owner of Step1's channel state."""

    config_changed = pyqtSignal()                 # weights / nucleus changed
    visibility_changed = pyqtSignal(str, bool)    # overlay membership changed
    current_channel_changed = pyqtSignal(str)     # what Intensity should edit
    color_changed = pyqtSignal(str, str)          # overlay colour changed

    def __init__(self, all_channels):
        super().__init__()
        self.all_channels = list(all_channels or [])
        # group membership and group weight: model-only, never on screen
        self._groups = {}          # group -> {"weight": float, "members": [ch]}
        # The per-(group, channel) weights EXACTLY as they were loaded.  A row
        # can only show one number, but an old project may put the same channel
        # in two groups at two weights; those values are kept here so a session
        # nobody edited round-trips value for value.
        self._group_channel_weights = {}   # group -> {channel: float}
        self._nucleus_channel = ""
        self._nucleus_weight = 0.0
        # Channels whose weight the USER moved since the config was loaded.
        # Only those are written back, and then to every group they belong to.
        self._edited_channels = set()
        self._rows = {}            # channel -> ChannelRow
        self._items = {}           # channel -> QListWidgetItem
        self._colors = {}          # channel -> "#rrggbb" (user picks win)
        self._current = ""
        self._selecting = False
        self._setup_ui()
        self._rebuild_rows()

    # ── construction ──────────────────────────────────────────────────
    def _setup_ui(self):
        lay = QVBoxLayout(self)
        lay.setContentsMargins(4, 4, 4, 4)
        lay.setSpacing(4)

        nuc_row = QHBoxLayout()
        nuc_row.setSpacing(4)
        nuc_lbl = QLabel("Nucleus:")
        nuc_lbl.setStyleSheet("color:#9bd0ff;font-size:10px;")
        nuc_row.addWidget(nuc_lbl)
        # Read-only on purpose.  The nucleus channel and its weight come from
        # the Step0 handoff; a flat per-channel list cannot express "this
        # channel is the nucleus at one weight AND a marker at another", so
        # Step1 shows the authoritative answer rather than inventing a second
        # one.  Changing the nucleus is a Step0 decision.
        self._nuc_value = QLabel("—")
        self._nuc_value.setStyleSheet("color:#dce5ef;font-size:10px;")
        self._nuc_value.setToolTip(
            "The nucleus channel and weight come from Step0's handoff and are "
            "read-only here.")
        nuc_row.addWidget(self._nuc_value, stretch=1)
        lay.addLayout(nuc_row)

        self._list = QListWidget()
        self._list.setStyleSheet(_LIST_STYLE)
        self._list.setSelectionMode(QListWidget.SingleSelection)
        self._list.currentItemChanged.connect(self._on_current_item)
        lay.addWidget(self._list, stretch=1)

        btn_row = QHBoxLayout()
        btn_row.setSpacing(4)
        btn_reset = QPushButton("Reset weights")
        btn_reset.setStyleSheet(
            "QPushButton{color:#e5c07b;background:#182230;border:1px solid #354a63;"
            "border-radius:4px;padding:2px 8px;font-size:10px;}"
            "QPushButton:hover{background:#23354a;}")
        btn_reset.clicked.connect(self._reset_all_channel_weights)
        btn_row.addWidget(btn_reset)

        btn_load_weights = QPushButton("Load weights")
        btn_load_weights.setStyleSheet(
            "QPushButton{color:#9bd0ff;background:#182230;border:1px solid #354a63;"
            "border-radius:4px;padding:2px 8px;font-size:10px;}"
            "QPushButton:hover{background:#23354a;}")
        btn_load_weights.clicked.connect(self._load_weights_from_file)
        btn_row.addWidget(btn_load_weights)
        btn_row.addStretch()
        lay.addLayout(btn_row)

    # ── rows ──────────────────────────────────────────────────────────
    def _default_color(self, ch):
        try:
            i = self.all_channels.index(ch)
        except ValueError:
            i = len(self._rows)
        return _PALETTE[i % len(_PALETTE)]

    def _rebuild_rows(self):
        """Rebuild the visible list from `all_channels`, keeping state."""
        weights = {ch: row.weight() for ch, row in self._rows.items()}
        visible = {ch: row.is_visible() for ch, row in self._rows.items()}
        current = self._current

        self._list.clear()
        self._rows.clear()
        self._items.clear()

        for ch in self.all_channels:
            color = self._colors.get(ch) or self._default_color(ch)
            row = ChannelRow(ch,
                             weight=weights.get(ch, 0.0),
                             visible=visible.get(ch, False),
                             color=color)
            row.selected.connect(self._on_row_selected)
            row.visibility_toggled.connect(self._on_row_visibility)
            row.weight_edited.connect(self._on_row_weight_edited)
            row.color_clicked.connect(self._pick_color)
            item = QListWidgetItem(self._list)
            item.setSizeHint(QSize(200, ROW_HEIGHT))
            self._list.setItemWidget(item, row)
            self._rows[ch] = row
            self._items[ch] = item

        self._refresh_nucleus_display()
        if current in self._rows:
            self.set_current_channel(current)
        else:
            self._current = ""

    # ── selection / visibility / colour ───────────────────────────────
    def _on_row_selected(self, channel):
        self.set_current_channel(channel)

    def _on_current_item(self, item, _prev):
        if item is None or self._selecting:
            return
        for ch, it in self._items.items():
            if it is item:
                self.set_current_channel(ch)
                return

    def current_channel(self):
        return self._current

    def set_current_channel(self, channel, auto_show=True):
        """Make `channel` current.

        A user CLICK also ticks a hidden channel, because selecting something
        you cannot see is a dead end. Restoring a saved session is not a click:
        it passes `auto_show=False` so a channel the user deliberately left
        hidden comes back hidden. Ticking some OTHER channel never moves the
        selection either way.
        """
        if channel not in self._rows:
            return
        row = self._rows[channel]
        newly_visible = auto_show and not row.is_visible()
        if newly_visible:
            row.set_visible(True)
        changed = (channel != self._current)
        self._current = channel
        self._selecting = True
        try:
            self._list.setCurrentItem(self._items[channel])
        finally:
            self._selecting = False
        if newly_visible:
            self.visibility_changed.emit(channel, True)
        if changed:
            self.current_channel_changed.emit(channel)

    def _on_row_visibility(self, channel, visible):
        self.visibility_changed.emit(channel, bool(visible))

    def visible_channels(self):
        return [ch for ch in self.all_channels
                if ch in self._rows and self._rows[ch].is_visible()]

    def set_channel_visible(self, channel, visible):
        row = self._rows.get(channel)
        if row is None or row.is_visible() == bool(visible):
            return
        row.set_visible(visible)
        self.visibility_changed.emit(channel, bool(visible))

    def channel_color(self, channel):
        row = self._rows.get(channel)
        if row is not None:
            return row.color()
        return self._colors.get(channel) or self._default_color(channel)

    def channel_colors(self):
        return {ch: self.channel_color(ch) for ch in self._rows}

    def set_channel_color(self, channel, color):
        if not color:
            return
        self._colors[channel] = color
        row = self._rows.get(channel)
        if row is not None:
            row.set_color(color)
        self.color_changed.emit(channel, color)

    def _pick_color(self, channel):
        current = QColor(self.channel_color(channel))
        picked = QColorDialog.getColor(current, self, f"Colour for {channel}")
        if picked.isValid():
            self.set_channel_color(channel, picked.name())

    # ── weights ───────────────────────────────────────────────────────
    def channel_weight(self, channel):
        row = self._rows.get(channel)
        return float(row.weight()) if row is not None else 0.0

    def set_channel_weight(self, channel, weight):
        row = self._rows.get(channel)
        if row is None:
            return
        row.set_weight(weight)

    def nucleus_channel(self):
        return self._nucleus_channel

    def set_nucleus(self, channel, weight=None):
        """Adopt the nucleus Step0 handed over.  Not a user-editable choice."""
        self._nucleus_channel = str(channel or "")
        if weight is not None:
            self._nucleus_weight = float(weight)
        self._refresh_nucleus_display()

    def set_nucleus_weight(self, weight):
        self._nucleus_weight = float(weight)
        self._refresh_nucleus_display()

    def _refresh_nucleus_display(self):
        nuc = self._nucleus_channel
        self._nuc_value.setText(
            f"{nuc}  (weight {self._nucleus_weight:.2f})" if nuc else "—")
        for ch, row in self._rows.items():
            is_nuc = bool(nuc) and ch == nuc
            row.set_weight_editable(not is_nuc)
            if is_nuc:
                row.set_weight(self._nucleus_weight)

    def _on_row_weight_edited(self, channel):
        """The user moved a weight: from now on it is theirs.

        The rule for a channel that an old project put in several groups is
        stated once, here: an edited weight applies to EVERY group the channel
        belongs to. Until it is edited, each group keeps the value it was
        loaded with.
        """
        self._edited_channels.add(channel)
        self.config_changed.emit()

    def _stored_weights_for(self, channel):
        """Every weight this channel was LOADED with, one per group it is in
        (plus the nucleus slot when it is the nucleus)."""
        values = [weights[channel]
                  for weights in self._group_channel_weights.values()
                  if channel in weights]
        if channel and channel == self._nucleus_channel:
            values.append(float(self._nucleus_weight))
        return values

    def _representative_weight(self, channel):
        values = self._stored_weights_for(channel)
        if not values:
            return 0.0
        return max(values)

    def ambiguous_channels(self):
        """Channels whose loaded weights disagree between groups: one row
        cannot show two numbers, so the disagreement is named rather than
        quietly resolved."""
        out = {}
        for ch in self._rows:
            values = self._stored_weights_for(ch)
            if len(set(round(v, 6) for v in values)) > 1:
                out[ch] = sorted(set(round(v, 6) for v in values))
        return out

    def _effective_weight(self, group, channel):
        """What `get_groups` reports for one (group, channel) pair."""
        if channel in self._edited_channels:
            return self.channel_weight(channel)
        stored = self._group_channel_weights.get(group, {})
        if channel in stored:
            return float(stored[channel])
        return self.channel_weight(channel)

    def zero_marker_weights(self):
        """Every channel but the nucleus back to 0 — the fresh-project state.

        This is a real edit, not a repaint: the zero is what every group the
        channel belongs to reports afterwards, so what the user sees and what
        gets fused and saved are the same number.  One signal, at the end.
        """
        nuc = self._nucleus_channel
        for ch, row in self._rows.items():
            if ch != nuc:
                row.set_weight(0.0)
                self._edited_channels.add(ch)
        self.config_changed.emit()

    def _reset_all_channel_weights(self):
        self.zero_marker_weights()

    # ── groups: model only ────────────────────────────────────────────
    def _group_of(self, channel):
        for name, data in self._groups.items():
            if channel in data["members"]:
                return name
        return None

    def _add_group(self, name, channel_weights=None):
        if name in self._groups:
            return
        self._groups[name] = {"weight": 1.0, "members": []}
        for ch, w in (channel_weights or {}).items():
            self._add_to_group(name, ch, w)

    def _add_to_group(self, name, channel, weight=0.0):
        data = self._groups.setdefault(name, {"weight": 1.0, "members": []})
        if channel not in data["members"]:
            data["members"].append(channel)
        self._group_channel_weights.setdefault(name, {})[channel] = float(weight)
        self.set_channel_weight(channel, self._representative_weight(channel))

    def _del_group(self, name):
        self._groups.pop(name, None)
        self._group_channel_weights.pop(name, None)

    def group_weight(self, name):
        data = self._groups.get(name)
        return float(data["weight"]) if data else 1.0

    def set_group_weight(self, name, weight):
        data = self._groups.setdefault(name, {"weight": 1.0, "members": []})
        data["weight"] = float(weight)

    # ── the config contract (unchanged shape) ─────────────────────────
    def get_groups(self):
        return {name: {ch: self._effective_weight(name, ch)
                       for ch in data["members"]}
                for name, data in self._groups.items()}

    def get_group_weights(self):
        return {name: float(data["weight"]) for name, data in self._groups.items()}

    def get_nucleus(self):
        return self._nucleus_channel, float(self._nucleus_weight)

    def get_full_config(self):
        nuc_ch, nuc_w = self.get_nucleus()
        return {
            "nucleus": {"channel": nuc_ch, "weight": nuc_w},
            "groups": {
                name: {"group_weight": float(data["weight"]),
                       "channels": {ch: self._effective_weight(name, ch)
                                    for ch in data["members"]}}
                for name, data in self._groups.items()
            },
        }

    def load_panel(self, groups, nuc_ch):
        """Load the panel for a freshly opened dataset.

        Defaults, deliberately: the nucleus is the only channel shown and the
        only one with weight; every marker starts at 0 and unticked.
        """
        # A new dataset starts with its own colours: nothing is inherited from
        # the slide that was open before.
        self._colors = {}
        self._groups = {}
        self._group_channel_weights = {}
        self._edited_channels = set()
        self._nucleus_weight = 0.0
        nuc = str(nuc_ch or "")
        for gname, channels in (groups or {}).items():
            self._add_group(str(gname), {str(ch): 0.0 for ch in channels
                                         if str(ch) != nuc})

        self._rebuild_rows()

        self._nucleus_channel = str(nuc_ch or "")

        for ch, row in self._rows.items():
            row.set_weight(0.0)
            row.set_visible(False)
        if nuc_ch and nuc_ch in self._rows:
            self._nucleus_weight = 1.0
            self.set_channel_weight(nuc_ch, 1.0)
            self._refresh_nucleus_display()
            self._rows[nuc_ch].set_visible(True)
            self._current = nuc_ch
            self._selecting = True
            try:
                self._list.setCurrentItem(self._items[nuc_ch])
            finally:
                self._selecting = False
        else:
            self._current = ""
        self.config_changed.emit()

    def apply_full_config(self, cfg, adopt_nucleus=False):
        """Restore groups, group weights and per-channel weights.

        The nucleus is NOT taken from `cfg` by default. It is the Step0
        handoff's answer, and Step1 shows it read-only; letting a saved session
        replace it would create a hidden state the user cannot see or correct.
        `adopt_nucleus=True` is for the case where nothing has told us the
        nucleus yet (a legacy session restored without a handoff).

        The nucleus is also kept OUT of the marker groups: a channel that is
        both the nucleus and a marker would contribute twice, once as blue and
        once as red. Dropping it is announced rather than done quietly.
        """
        cfg = dict(cfg or {})
        nucleus_cfg = cfg.get("nucleus") or {}
        groups_cfg = cfg.get("groups") or {}

        if adopt_nucleus or not self._nucleus_channel:
            self._nucleus_channel = str(nucleus_cfg.get("channel") or "")
            self._nucleus_weight = float(nucleus_cfg.get("weight", 0.0) or 0.0)
        nuc_ch = self._nucleus_channel

        self._groups = {}
        self._group_channel_weights = {}
        self._edited_channels = set()
        dropped = []
        for gname, gdata in groups_cfg.items():
            channels = {}
            for ch, w in ((gdata or {}).get("channels") or {}).items():
                ch = str(ch)
                if self.all_channels and ch not in self.all_channels:
                    continue
                if nuc_ch and ch == nuc_ch:
                    dropped.append(f"{gname}:{ch}")
                    continue
                channels[ch] = float(w)
            self._add_group(str(gname), channels)
            self.set_group_weight(str(gname),
                                  float((gdata or {}).get("group_weight", 1.0)))
        if dropped:
            print("[Step1] nucleus channel removed from marker groups "
                  f"(it would contribute twice): {sorted(dropped)}")
        if nuc_ch:
            self.set_channel_weight(nuc_ch,
                                    self._representative_weight(nuc_ch))
        self._refresh_nucleus_display()
        ambiguous = self.ambiguous_channels()
        for ch, values in ambiguous.items():
            row = self._rows.get(ch)
            if row is not None:
                row.name_label.setText(f"{ch} *")
                row.setToolTip(
                    f"{ch} is in several groups at different weights "
                    f"({', '.join(str(v) for v in values)}). The row shows the "
                    "largest; editing it applies that weight to every group.")
        if ambiguous:
            print(f"[Step1] channels in several groups at different weights: "
                  f"{sorted(ambiguous)}")
        self.config_changed.emit()

    def restore_display_state(self, colors=None, visibility=None,
                              current_channel=""):
        """Put back a saved display state in one go, with no side effects.

        Restoring is not clicking: a channel the user left hidden must come
        back hidden even when it is the current one, and the host should redraw
        once at the end rather than once per channel.
        """
        for ch, color in (colors or {}).items():
            ch = str(ch)
            if not color:
                continue
            self._colors[ch] = str(color)
            row = self._rows.get(ch)
            if row is not None:
                row.set_color(str(color))

        if visibility:
            for ch in list(self.all_channels or []):
                if ch in visibility:
                    row = self._rows.get(ch)
                    if row is not None:
                        row.set_visible(bool(visibility[ch]))

        if current_channel and current_channel in self._rows:
            self.set_current_channel(current_channel, auto_show=False)

    def _load_weights_from_file(self):
        mw = self.window()
        out_dir = ""
        if hasattr(mw, "current_gui_work_dir"):
            out_dir = mw.current_gui_work_dir()
        if not out_dir and hasattr(mw, "_out_path_edit"):
            out_dir = mw._out_path_edit.text().strip()
        start_dir = out_dir if out_dir and os.path.exists(out_dir) else os.getcwd()
        path, _ = QFileDialog.getOpenFileName(
            self, "Load Channel Weights", start_dir, "JSON Files (*.json)"
        )
        if not path:
            return
        try:
            with open(path, "r", encoding="utf-8") as f:
                cfg = json.load(f)
        except Exception as e:
            QMessageBox.warning(self, "Load Weights", f"Invalid JSON file:\n{e}")
            return
        try:
            self.apply_full_config(cfg)
        except Exception as e:
            QMessageBox.warning(
                self, "Load Weights",
                f"Failed to apply weight configuration:\n{e}")

    # ── channel universe ──────────────────────────────────────────────
    def set_channels(self, channels):
        self.all_channels = list(channels or [])
        self._rebuild_rows()
