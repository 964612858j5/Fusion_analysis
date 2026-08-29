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
    color_clicked = pyqtSignal(str)

    def __init__(self, name, color="#888888", visible=True, mini_value=1.0,
                 mini_label="w", parent=None):
        super().__init__(parent)
        self._name = name

        lay = QtWidgets.QHBoxLayout(self)
        lay.setContentsMargins(6, 1, 6, 1)   # compact: 4/5 row height
        lay.setSpacing(5)

        # v15 template (matches the Step0 BG rows): transparent children so the
        # list's hover/selected highlight paints through, and an explicitly
        # drawn checkbox indicator that sits visibly on top of the highlight.
        self.setStyleSheet(
            "*{background:transparent;}"
            "QCheckBox::indicator{width:13px;height:13px;border-radius:2px;"
            "border:1px solid #6d8196;background:#182230;}"
            "QCheckBox::indicator:checked{background:#9bd0ff;border:1px solid #9bd0ff;}"
            "QCheckBox::indicator:disabled{border:1px solid #3a4a5c;background:#141b26;}")

        self._chk = QtWidgets.QCheckBox()
        self._chk.setChecked(bool(visible))
        self._chk.setFixedSize(22, 18)
        self._chk.setToolTip("Visible in viewer")
        self._chk.toggled.connect(self._on_toggle)
        lay.addWidget(self._chk)

        self._name_lbl = QtWidgets.QLabel(name)
        self._name_lbl.setStyleSheet("color:#dce5ef;font-size:11px;")
        # Non-stretching fixed column; the list applies one uniform width (the
        # longest name) after populate so the color boxes align across rows.
        self._name_lbl.setSizePolicy(QtWidgets.QSizePolicy.Fixed,
                                     QtWidgets.QSizePolicy.Preferred)
        self._name_lbl.setFixedWidth(self._name_lbl.sizeHint().width() + 2)
        self._name_lbl.setToolTip(name)
        lay.addWidget(self._name_lbl)

        # Clickable pseudocolor box directly after the name (where the BG rows
        # put the method combo). Checkbox-indicator size; shows the channel
        # color; click opens the color picker (handled upstream).
        self._swatch = QtWidgets.QLabel()
        self._swatch.setFixedSize(13, 13)
        self._swatch.setCursor(QtCore.Qt.PointingHandCursor)
        self._swatch.setToolTip("Click to change channel color")
        self._swatch.mousePressEvent = self._on_swatch_clicked
        self._set_swatch_color(color)
        lay.addWidget(self._swatch)

        lay.addStretch(1)                    # push only the mini value right

        self._mini = QtWidgets.QLabel(f"{mini_label}:{mini_value:.2f}")
        self._mini.setStyleSheet("color:#879bb1;font-size:10px;")
        self._mini.setFixedWidth(48)
        self._mini.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
        lay.addWidget(self._mini)

    def _set_swatch_color(self, color):
        self._swatch.setStyleSheet(
            f"background:{color};border:1px solid #6d8196;border-radius:2px;"
        )

    def set_name_width(self, width):
        """Uniform name-column width (set by the list to the longest name)."""
        self._name_lbl.setFixedWidth(int(width))

    def _on_toggle(self, checked):
        self.visibility_toggled.emit(self._name, bool(checked))

    def _on_swatch_clicked(self, _event):
        self.color_clicked.emit(self._name)

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
    color_clicked = pyqtSignal(str)

    def __init__(self, parent=None, *, show_header=True):
        super().__init__(parent)
        self._rows = {}  # name -> _ChannelRow

        lay = QtWidgets.QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(2)

        # The internal "Channels" header is optional: hosts that wrap this list in
        # a titled group box (Step0) hide it to avoid a duplicate title.
        if show_header:
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
        # v15 template palette: same base / hover / selected colors as the
        # shared ChannelDock (Step0 BG tab).
        self._list.setStyleSheet(
            "QListWidget{background:#101620;border:1px solid #253246;"
            "border-radius:4px;}"
            "QListWidget::item{border-bottom:1px solid #202c3b;}"
            "QListWidget::item:hover{background:#1a3e33;}"
            "QListWidget::item:selected{background:#1a2b3e;}"
            "QListWidget::item:selected:hover{background:#1a2b3e;}"
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
            row.color_clicked.connect(self.color_clicked.emit)
            item = QtWidgets.QListWidgetItem()
            # Same row height as the shared ChannelDock template (min 26px) —
            # no 0.8 compression.
            _sh = row.sizeHint()
            item.setSizeHint(QtCore.QSize(_sh.width(), max(26, _sh.height())))
            item.setData(Qt.UserRole, name)
            self._list.addItem(item)
            self._list.setItemWidget(item, row)
            self._rows[name] = row

        # Uniform name column = longest name, so the color boxes align across
        # rows while sitting right next to each name (BG-row template).
        if self._rows:
            width = max(
                r._name_lbl.fontMetrics().boundingRect(r._name_lbl.text()).width()
                for r in self._rows.values()) + 6
            for r in self._rows.values():
                r.set_name_width(width)

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

    def scroll_value(self):
        """Current vertical scroll offset (save before a rebuild)."""
        return self._list.verticalScrollBar().value()

    def set_scroll_value(self, value):
        """Restore a saved vertical scroll offset after a rebuild."""
        self._list.verticalScrollBar().setValue(int(value))

    def filter_rows(self, text):
        """Show only rows whose channel name contains `text` (case-insensitive).

        Display-only: rows are hidden via QListWidgetItem.setHidden — the model
        (names/colors/visibility) is untouched. Empty text shows every row.
        """
        needle = (text or "").strip().lower()
        for i in range(self._list.count()):
            item = self._list.item(i)
            name = str(item.data(Qt.UserRole) or "").lower()
            item.setHidden(needle not in name)

    def update_mini(self, name, value, label="w"):
        row = self._rows.get(name)
        if row:
            row.set_mini(value, label)

    def set_row_checked(self, name, checked):
        """Set a single row's visibility checkbox without emitting its signal."""
        row = self._rows.get(name)
        if row:
            row.set_visible_checked(bool(checked))

    def set_channel_color(self, name, color):
        """Update a single row's color swatch."""
        row = self._rows.get(name)
        if row:
            row.set_color(color)

    def set_all_visible(self, checked):
        """Set every row's visibility checkbox without emitting per-row signals.

        Used by the workbench Select all / Clear all controls; the workbench
        updates its own model directly, so signals stay blocked here.
        """
        for row in self._rows.values():
            row.set_visible_checked(bool(checked))

    def _on_current_changed(self, current, _previous):
        if current is not None:
            self.active_changed.emit(current.data(Qt.UserRole))
