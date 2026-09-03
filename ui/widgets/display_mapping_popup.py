"""block01/ui/widgets/display_mapping_popup.py — floating Display window.

The Step0 compare view ("Patch Preview") used to carry two inline rows of
min / max / gamma spin boxes plus two Marker/Nucleus toggle buttons. They ate
two rows of the preview's vertical space for controls that are touched once
per channel. They now live here, in a floating window opened from the preview
header's "Display…" button.

This widget is a VIEW ONLY. It owns no mapping: the page owns one display
mapping per channel (`Step0Page.set_display_mapping`), the popup emits what
the user did and is told what to show:

    mapping_edited(role, lo, hi, gamma)   a spin box was edited
    auto_requested(role)                  "Auto" was clicked
    show_toggled(role, on)                a "Show" checkbox flipped
    use_as_remap_requested()              copy the marker mapping into the
                                          segmentation remap

`role` is "marker" or "nucleus". `set_channel_names`, `set_mapping` and
`set_shown` push the page's state back into the controls with signals
BLOCKED, so a sync never loops back out as an edit.

Nothing here computes anything: no pixels are read, no worker is started.
"""

from __future__ import annotations

from PyQt5 import QtWidgets
from PyQt5.QtCore import Qt, pyqtSignal

ROLES = ("marker", "nucleus")

_LBL = "color:#aaa;font-size:10px;"
_SPIN = ("QDoubleSpinBox{background:#1a1a1a;color:#ddd;border:1px solid #444;"
         "border-radius:3px;font-size:10px;padding:0 2px;}")
_BTN = ("QPushButton{color:#aaa;border:1px solid #555;border-radius:3px;"
        "font-size:10px;background:#1a1a1a;padding:2px 8px;}"
        "QPushButton:hover{background:#333;}")

# The caveat the "Use as segmentation remap" button must carry: the two
# pipelines do not read the same pixels.
REMAP_TOOLTIP = (
    "Copy this channel's display mapping (min / max / gamma) into the "
    "Channel Remap tab as its segmentation remap.\n\n"
    "Caveat: the remap preview operates on the SAVED corrected pixels (or "
    "raw when nothing is saved), while the display mapping is applied to the "
    "raw / TopHat / cuCIM arrays shown in Patch Preview. The numbers are raw "
    "intensity units in both, and the two mean the same thing only where the "
    "underlying pixels are the same (brightness 0, contrast 1)."
)


class DisplayMappingPopup(QtWidgets.QWidget):
    """Floating "Display" window: per-role min / max / gamma + Auto + Show."""

    mapping_edited = pyqtSignal(str, float, float, float)
    auto_requested = pyqtSignal(str)
    show_toggled = pyqtSignal(str, bool)
    use_as_remap_requested = pyqtSignal()

    def __init__(self, parent=None):
        # Same window flags as the Tissue Navigator popup: a native title bar
        # with minimize / maximize / close, kept above the page.
        super().__init__(
            parent,
            Qt.Window
            | Qt.WindowMinimizeButtonHint
            | Qt.WindowMaximizeButtonHint
            | Qt.WindowCloseButtonHint
            | Qt.WindowStaysOnTopHint,
        )
        self.setWindowTitle("Display")
        self.setMinimumWidth(360)
        self.resize(460, 170)
        self.setStyleSheet("background:#141414;")

        self._syncing = False
        self._widgets = {}       # role -> dict(min, max, gamma, auto, show, label)
        self._names = {"marker": "", "nucleus": ""}

        outer = QtWidgets.QVBoxLayout(self)
        outer.setContentsMargins(8, 6, 8, 6)
        outer.setSpacing(4)

        hint = QtWidgets.QLabel(
            "Raw intensity in, screen value out:  "
            "shown = clip((v − min)/(max − min))^γ.  Display only.")
        hint.setWordWrap(True)
        hint.setStyleSheet("color:#777;font-size:9px;")
        outer.addWidget(hint)

        outer.addLayout(self._build_row("marker", "Marker", "#98c379"))
        outer.addLayout(self._build_row("nucleus", "DAPI", "#56b6c2"))

        self.btn_use_as_remap = QtWidgets.QPushButton("Use as segmentation remap")
        self.btn_use_as_remap.setStyleSheet(_BTN)
        self.btn_use_as_remap.setToolTip(REMAP_TOOLTIP)
        self.btn_use_as_remap.clicked.connect(
            lambda _=False: self.use_as_remap_requested.emit())
        bottom = QtWidgets.QHBoxLayout()
        bottom.addStretch()
        bottom.addWidget(self.btn_use_as_remap)
        outer.addLayout(bottom)
        outer.addStretch()

        self.set_channel_names("", "")

    # ── construction ──────────────────────────────────────────────────
    def _build_row(self, role, title, color):
        row = QtWidgets.QHBoxLayout()
        row.setSpacing(4)
        lbl = QtWidgets.QLabel(title)
        lbl.setStyleSheet(f"color:{color};font-size:10px;min-width:120px;")
        row.addWidget(lbl)

        spins = {}
        for key, text, rng, step, dec in (("min", "Min", (-1e6, 1e7), 1.0, 1),
                                          ("max", "Max", (-1e6, 1e7), 1.0, 1),
                                          ("gamma", "γ", (0.1, 5.0), 0.1, 2)):
            t = QtWidgets.QLabel(text)
            t.setStyleSheet(_LBL)
            sp = QtWidgets.QDoubleSpinBox()
            sp.setRange(*rng)
            sp.setSingleStep(step)
            sp.setDecimals(dec)
            sp.setValue(1.0 if key == "gamma" else 0.0)
            sp.setKeyboardTracking(False)
            sp.setStyleSheet(_SPIN)
            sp.setToolTip(f"{title} display {text.lower()} "
                          f"(raw intensity units; gamma is unitless).")
            sp.valueChanged.connect(lambda _v, r=role: self._on_edited(r))
            row.addWidget(t)
            row.addWidget(sp, stretch=1)
            spins[key] = sp

        btn_auto = QtWidgets.QPushButton("Auto")
        btn_auto.setStyleSheet(_BTN)
        btn_auto.setToolTip("Re-seed min/max from the whole slide's tissue "
                            "(QuPath-style 0.1 / 99.9 percentiles); gamma back to 1.")
        btn_auto.clicked.connect(lambda _=False, r=role: self.auto_requested.emit(r))
        row.addWidget(btn_auto)

        chk = QtWidgets.QCheckBox("Show")
        chk.setChecked(True)
        chk.setStyleSheet("color:#ccc;font-size:10px;")
        chk.setToolTip(f"Show/hide the {title.lower()} layer in the compare panels.")
        chk.toggled.connect(
            lambda on, r=role: None if self._syncing else self.show_toggled.emit(r, bool(on)))
        row.addWidget(chk)

        self._widgets[role] = {"min": spins["min"], "max": spins["max"],
                               "gamma": spins["gamma"], "auto": btn_auto,
                               "show": chk, "label": lbl, "title": title}
        return row

    # ── the page tells the popup what to show (never re-emits) ────────
    def set_channel_names(self, marker, nucleus):
        self._names = {"marker": marker or "", "nucleus": nucleus or ""}
        for role in ROLES:
            w = self._widgets[role]
            name = self._names[role]
            w["label"].setText(f"{w['title']} ({name})" if name else f"{w['title']} (—)")

    def set_mapping(self, role, lo, hi, gamma):
        w = self._widgets.get(role)
        if w is None:
            return
        self._syncing = True
        try:
            for key, val in (("min", lo), ("max", hi), ("gamma", gamma)):
                sp = w[key]
                sp.blockSignals(True)
                try:
                    sp.setValue(float(val))
                finally:
                    sp.blockSignals(False)
        finally:
            self._syncing = False

    def set_shown(self, role, on):
        w = self._widgets.get(role)
        if w is None:
            return
        chk = w["show"]
        self._syncing = True
        chk.blockSignals(True)
        try:
            chk.setChecked(bool(on))
        finally:
            chk.blockSignals(False)
            self._syncing = False

    def mapping(self, role):
        """`(min, max, gamma)` currently shown for `role`."""
        w = self._widgets[role]
        return (w["min"].value(), w["max"].value(), w["gamma"].value())

    def is_shown(self, role):
        return self._widgets[role]["show"].isChecked()

    # ── user edits ────────────────────────────────────────────────────
    def _on_edited(self, role):
        if self._syncing:
            return
        lo, hi, gamma = self.mapping(role)
        if hi <= lo:
            hi = lo + 1.0
        self.mapping_edited.emit(role, float(lo), float(hi), float(gamma))
