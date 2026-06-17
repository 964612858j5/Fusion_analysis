"""
block01/ui/widgets/channel_layer_list.py — v13.1 channel-first layer list.

Left column of the channel-first viewer (see
docs/v13_1_channel_conditioning/04_UI_REDESIGN_SPEC.md).

Hard rules from the spec:
  - Vertical scrolling only. NO horizontal scrolling.
  - Each row shows ONLY: visibility checkbox, color swatch, channel name,
    one small value (opacity or weight). No intensity sliders in the row —
    those live in the right inspector.
  - Clicking a row makes that channel the active channel.

Host-agnostic: data in via set_channels(); edits out via signals. No knowledge
of Step3 or any page.
"""

from __future__ import annotations

from PyQt5 import QtCore, QtWidgets
from PyQt5.QtCore import Qt, pyqtSignal


class _ChannelRow(QtWidgets.QWidget):
    """Compact single-channel row widget. Fixed, never needs horizontal scroll."""

    visibility_toggled = pyqtSignal(str, bool)

    def __init__(self, name, color="#888888", visible=True, mini_value=1.0,
                 mini_label="w", parent=None):
        super().__init__(parent)
        self._name = name

        lay = QtWidgets.QHBoxLayout(self)
        lay.setContentsMargins(4, 2, 4, 2)
        lay.setSpacing(6)

        self._chk = QtWidgets.QCheckBox()
        self._chk.setChecked(bool(visible))
        self._chk.setToolTip("Visible in viewer")
        self._chk.toggled.connect(self._on_toggle)
        lay.addWidget(self._chk)

        self._swatch = QtWidgets.QLabel()
        self._swatch.setFixedSize(14, 14)
        self._set_swatch_color(color)
        lay.addWidget(self._swatch)

        self._name_lbl = QtWidgets.QLabel(name)
        self._name_lbl.setStyleSheet("color:#ddd;font-size:11px;")
        # Elide rather than grow: keeps the list inside its width (no h-scroll).
        self._name_lbl.setSizePolicy(QtWidgets.QSizePolicy.Ignored,
                                     QtWidgets.QSizePolicy.Preferred)
        self._name_lbl.setToolTip(name)
        lay.addWidget(self._name_lbl, stretch=1)

        self._mini = QtWidgets.QLabel(f"{mini_label}:{mini_value:.2f}")
        self._mini.setStyleSheet("color:#888;font-size:10px;")
        self._mini.setFixedWidth(48)
        self._mini.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
        lay.addWidget(self._mini)

    def _set_swatch_color(self, color):
        self._swatch.setStyleSheet(
            f"background:{color};border:1px solid #333;border-radius:2px;"
        )

    def _on_toggle(self, checked):
        self.visibility_toggled.emit(self._name, bool(checked))

    def set_visible_checked(self, checked):
        self._chk.blockSignals(True)
        self._chk.setChecked(bool(checked))
        self._chk.blockSignals(False)

    def set_color(self, color):
        self._set_swatch_color(color)

    def set_mini(self, value, label="w"):
        self._mini.setText(f"{label}:{value:.2f}")


class ChannelLayerList(QtWidgets.QWidget):
    """Vertical, scrollable, channel-first layer list.

    Signals
    -------
    active_changed(str)            : a channel row was selected
    visibility_changed(str, bool)  : a row's visibility checkbox toggled
    """

    active_changed = pyqtSignal(str)
    visibility_changed = pyqtSignal(str, bool)

    def __init__(self, parent=None):
        super().__init__(parent)
        self._rows = {}  # name -> _ChannelRow

        lay = QtWidgets.QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(2)

        hdr = QtWidgets.QLabel("Channels")
        hdr.setStyleSheet(
            "color:#61afef;font-weight:bold;font-size:11px;padding:2px;"
        )
        lay.addWidget(hdr)

        self._list = QtWidgets.QListWidget()
        # The whole point: vertical only, never horizontal.
        self._list.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self._list.setVerticalScrollBarPolicy(Qt.ScrollBarAsNeeded)
        self._list.setSelectionMode(QtWidgets.QAbstractItemView.SingleSelection)
        self._list.setStyleSheet(
            "QListWidget{background:#161616;border:1px solid #333;}"
            "QListWidget::item:selected{background:#2c3e50;}"
        )
        self._list.currentItemChanged.connect(self._on_current_changed)
        lay.addWidget(self._list, stretch=1)

    def set_channels(self, channels):
        """Populate the list.

        Parameters
        ----------
        channels : list of dict with keys:
            name (str), color (str hex, optional), visible (bool, optional),
            mini_value (float, optional), mini_label (str, optional)
        """
        self._list.clear()
        self._rows.clear()
        for ch in channels:
            name = ch["name"]
            row = _ChannelRow(
                name,
                color=ch.get("color", "#888888"),
                visible=ch.get("visible", True),
                mini_value=ch.get("mini_value", 1.0),
                mini_label=ch.get("mini_label", "w"),
            )
            row.visibility_toggled.connect(self.visibility_changed.emit)
            item = QtWidgets.QListWidgetItem()
            item.setSizeHint(row.sizeHint())
            item.setData(Qt.UserRole, name)
            self._list.addItem(item)
            self._list.setItemWidget(item, row)
            self._rows[name] = row

        if self._list.count() > 0:
            self._list.setCurrentRow(0)

    def set_active(self, name):
        for i in range(self._list.count()):
            item = self._list.item(i)
            if item.data(Qt.UserRole) == name:
                self._list.setCurrentItem(item)
                return

    def active_name(self):
        item = self._list.currentItem()
        return item.data(Qt.UserRole) if item else None

    def update_mini(self, name, value, label="w"):
        row = self._rows.get(name)
        if row:
            row.set_mini(value, label)

    def _on_current_changed(self, current, _previous):
        if current is not None:
            self.active_changed.emit(current.data(Qt.UserRole))
