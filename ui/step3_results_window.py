"""Step3's results window (docs/v17_step3_multi_results_plan.md §3.1).

ONE floating tool window lists every result record (a run and its region,
as Step3's list has them). Each row, as Step1's Results rows: its title
and upstream tag, `×` where the run may be deleted, `Use` (the one record
Step4 quantifies; the `In use` row is framed green), a `Cells` and a
`Nuclei` tick -- ticked, that mask is drawn on Step3's viewer -- each with
its own colour swatch and settings panel, and a status line of its own.
At the top: `Load…`, the "all Cells" / "all Nuclei" ticks and the
viewer-wide hint.

The window keeps, per entry key, the ticks and the styles for the session:
a list refresh or closing the window loses nothing. Default colours are
given once per entry, in the order entries are first seen (Step1's
palette, `mask_layers.PALETTE`): a new record never recolours an older one.

Controls and that remembered state only: it reads no file and decides
nothing. The main window fills the rows, the hint and the statuses, and
turns `ticks_changed` / `style_changed` / `use_requested` /
`delete_requested` / `load_requested` into its own actions.
"""

import dataclasses

from PyQt5 import QtCore, QtGui, QtWidgets
from PyQt5.QtCore import Qt, pyqtSignal

from .step1_button_styles import MODE_BUTTON_QSS
from .step1_presegmentation.mask_layers import PALETTE
from .step1_presegmentation.method_blocks import block_frame_qss
from .step3_label_binding import CELL, DEFAULT_STYLES, KINDS, NUCLEUS
from .step3_mask_bar import ElidedLabel, SwatchButton

TICK_LABELS = {CELL: "Cells", NUCLEUS: "Nuclei"}
NO_RESULTS_TEXT = "No segmentation results for this slide — run Step2"
_GREY = "color:#999;font-size:10px;"
_WARN = "color:#ffd166;font-size:10px;"


@dataclasses.dataclass(frozen=True)
class RowInfo:
    """One record as the window lists it."""
    key: str                    # the entry key (`run folder \x1f region`)
    run_dir: str
    title: str
    tag: str = ""               # the upstream's name, right-aligned
    deletable: bool = False
    has: tuple = (CELL, NUCLEUS)   # the masks this record has
    reasons: tuple = ()            # ((kind, why it is missing), ...)


def _rgb(hex_colour):
    c = QtGui.QColor(hex_colour)
    return (c.redF(), c.greenF(), c.blueF())


class _AllCheck(QtWidgets.QCheckBox):
    """Ticked: every row shows this kind; unticked: none does (Step1's
    `_AllCheck`). Half-ticked only SAYS the rows disagree; a click from
    there ticks them all."""

    def __init__(self, text, parent=None):
        super().__init__(text, parent)
        self.setTristate(True)

    def nextCheckState(self):  # noqa: N802
        self.setCheckState(Qt.Unchecked if self.checkState() == Qt.Checked else Qt.Checked)


class ResultRow(QtWidgets.QFrame):
    """One record."""

    def __init__(self, info, ticks, styles, parent=None):
        super().__init__(parent)
        self.info = info
        self.setObjectName("resultRow")
        self.setFrameShape(QtWidgets.QFrame.StyledPanel)
        self.setSizePolicy(QtWidgets.QSizePolicy.Preferred, QtWidgets.QSizePolicy.Fixed)
        lay = QtWidgets.QVBoxLayout(self)
        lay.setContentsMargins(4, 3, 4, 3)
        lay.setSpacing(2)
        top = QtWidgets.QHBoxLayout()
        top.setSpacing(4)
        self.btn_delete = None
        if info.deletable:
            self.btn_delete = QtWidgets.QToolButton(self)
            self.btn_delete.setText("×")
            self.btn_delete.setToolTip("Delete this run and everything made from it")
            self.btn_delete.setStyleSheet("QToolButton{color:#e06c75;border:none;font-weight:bold;}")
            top.addWidget(self.btn_delete)
        self.title = QtWidgets.QLabel(info.title, self)
        self.title.setWordWrap(True)
        self.title.setSizePolicy(QtWidgets.QSizePolicy.Ignored, QtWidgets.QSizePolicy.Preferred)
        self.title.setStyleSheet("font-weight:bold;font-size:11px;color:#ddd;")
        top.addWidget(self.title, 1)
        self.btn_use = QtWidgets.QPushButton("Use", self)
        self.btn_use.setMinimumWidth(52)
        self.btn_use.setMaximumWidth(64)
        self.btn_use.setToolTip("Step4 quantifies this record (one at a time)")
        top.addWidget(self.btn_use, 0, Qt.AlignTop)
        lay.addLayout(top)
        # the upstream on a line of its own, cut to fit (a long fuse name
        # must never push `Use` out of the row; the whole of it on hover)
        self.tag = None
        if info.tag:
            self.tag = ElidedLabel(self)
            self.tag.set_full_text(info.tag)
            self.tag.setToolTip(f"made from {info.tag}")
            lay.addWidget(self.tag)
        ctl = QtWidgets.QHBoxLayout()
        ctl.setSpacing(4)
        self.ticks, self.swatches = {}, {}
        for kind in KINDS:
            tick = QtWidgets.QCheckBox(TICK_LABELS[kind], self)
            tick.setStyleSheet("font-size:10px;color:#ddd;")
            tick.setEnabled(kind in info.has)
            why = dict(info.reasons).get(kind)
            if kind not in info.has and why:
                tick.setToolTip(f"Not available: {why}")
            tick.setChecked(bool(ticks.get(kind)) and kind in info.has)
            swatch = SwatchButton(kind, style=styles[kind], parent=self)
            swatch.setEnabled(kind in info.has)
            ctl.addWidget(tick)
            ctl.addWidget(swatch)
            ctl.addSpacing(8)
            self.ticks[kind], self.swatches[kind] = tick, swatch
        ctl.addStretch(1)
        lay.addLayout(ctl)
        self.status = QtWidgets.QLabel("", self)
        self.status.setWordWrap(True)
        self.status.setStyleSheet(_GREY)
        self.status.setVisible(False)
        lay.addWidget(self.status)
        self.set_in_use(False)

    def set_in_use(self, in_use):
        self.btn_use.setText("In use" if in_use else "Use")
        self.btn_use.setEnabled(not in_use)
        self.setStyleSheet(block_frame_qss("resultRow", "#6fcf97" if in_use else "#555"))

    def set_status(self, text, warn=False):
        self.status.setText(str(text or ""))
        self.status.setStyleSheet(_WARN if warn else _GREY)
        self.status.setVisible(bool(text))


class Step3ResultsWindow(QtWidgets.QWidget):
    """The floating results window (plan §3.1)."""

    ticks_changed = pyqtSignal()                  # any record's Cells / Nuclei tick
    style_changed = pyqtSignal(str, str, dict)    # entry key, kind, the fields that changed
    use_requested = pyqtSignal(str)               # entry key
    delete_requested = pyqtSignal(str)            # run folder
    load_requested = pyqtSignal()

    def __init__(self, parent=None):
        # A normal top-level window with its own minimize / close (Qt.Tool
        # has no minimize on X11 -- the tissue navigator's #1a lesson). On
        # top as the navigator is: X11 gives a Qt.Window no transient-for, so
        # the parent alone would let the main window cover it. Hidden off
        # Step3 (main window `_step3_follow_page`).
        super().__init__(parent, Qt.Window | Qt.WindowMinimizeButtonHint
                         | Qt.WindowCloseButtonHint | Qt.WindowStaysOnTopHint)
        self.setWindowTitle("Segmentation results")
        self.setStyleSheet("QWidget{background:#1c1c1c;color:#ddd;}")
        self.resize(420, 520)
        #: per entry key, for the session: {"ticks": {kind: bool}, "styles": {kind: MaskStyle}}
        self._state = {}
        self._seen = 0                            # entries given a colour so far
        self._rows = {}                           # key -> ResultRow, in list order
        self._order = []
        self._active = None
        lay = QtWidgets.QVBoxLayout(self)
        lay.setContentsMargins(6, 6, 6, 6)
        lay.setSpacing(4)
        top = QtWidgets.QHBoxLayout()
        self.load_button = QtWidgets.QPushButton("Load…")
        self.load_button.setStyleSheet(MODE_BUTTON_QSS)
        self.load_button.setToolTip("Load a Step2 result made on this slide (its run folder), "
                                    "from any project")
        self.load_button.clicked.connect(self.load_requested.emit)
        top.addWidget(self.load_button)
        top.addSpacing(10)
        self.all_ticks = {}
        for kind in KINDS:
            check = _AllCheck(f"all {TICK_LABELS[kind]}", self)
            check.setStyleSheet("font-size:10px;")
            check.clicked.connect(lambda _c, k=kind: self._all_clicked(k))
            top.addWidget(check)
            self.all_ticks[kind] = check
        top.addStretch(1)
        lay.addLayout(top)
        self.hint = ElidedLabel()
        lay.addWidget(self.hint)
        self.scroll = QtWidgets.QScrollArea(self)
        self.scroll.setWidgetResizable(True)
        self.scroll.setFrameShape(QtWidgets.QFrame.NoFrame)
        self.scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self._rows_host = QtWidgets.QWidget(self.scroll)
        self._rows_lay = QtWidgets.QVBoxLayout(self._rows_host)
        self._rows_lay.setContentsMargins(0, 0, 0, 0)
        self._rows_lay.setSpacing(3)
        self._rows_lay.addStretch(1)
        self.scroll.setWidget(self._rows_host)
        lay.addWidget(self.scroll, 1)
        self.empty = QtWidgets.QLabel(NO_RESULTS_TEXT, self)
        self.empty.setStyleSheet(_GREY)
        self.empty.setWordWrap(True)
        lay.addWidget(self.empty)
        self._show_all_state()

    # ── what the owner sets ───────────────────────────────────────────
    def set_rows(self, rows, active_key=None):
        """The records, in list order (top first). Their ticks and styles
        are kept from before; a record seen for the first time gets the next
        palette colours and no tick."""
        for row in self._rows.values():
            row.setParent(None)
            row.deleteLater()
        self._rows, self._order = {}, []
        for info in rows:
            state = self._state_of(info.key)
            row = ResultRow(info, state["ticks"], state["styles"], self._rows_host)
            row.btn_use.clicked.connect(lambda _c=False, k=info.key: self.use_requested.emit(k))
            if row.btn_delete is not None:
                row.btn_delete.clicked.connect(
                    lambda _c=False, d=info.run_dir: self.delete_requested.emit(d))
            for kind in KINDS:
                row.ticks[kind].toggled.connect(
                    lambda on, k=info.key, kd=kind: self._ticked(k, kd, on))
                row.swatches[kind].panel.changed.connect(
                    lambda change, k=info.key, kd=kind: self._styled(k, kd, change))
            self._rows_lay.insertWidget(self._rows_lay.count() - 1, row)
            self._rows[info.key] = row
            self._order.append(info.key)
        self.empty.setVisible(not rows)
        self.set_active(active_key)
        self._show_all_state()

    def set_active(self, key):
        self._active = key
        for k, row in self._rows.items():
            row.set_in_use(k == key)

    def set_hint(self, text):
        self.hint.set_full_text(text)

    def set_row_status(self, key, text, warn=False):
        row = self._rows.get(key)
        if row is not None:
            row.set_status(text, warn)

    def tick(self, key, kind, on=True):
        """Tick (or untick) one record's mask, as a click would: the
        default at scene start, and `Load…`. A mask the record lacks is
        left alone. Returns whether the state changed."""
        row = self._rows.get(key)
        if row is None or kind not in row.info.has:
            return False
        if row.ticks[kind].isChecked() == bool(on):
            return False
        row.ticks[kind].setChecked(bool(on))       # -> `_ticked`
        return True

    def tick_default(self, key):
        """A record's default (plan §3.1): its cells, or its nuclei when it
        has no cell mask. Returns whether a tick changed."""
        row = self._rows.get(key)
        if row is None:
            return False
        return self.tick(key, CELL if CELL in row.info.has else NUCLEUS)

    def forget(self, keys):
        """Records deleted: their rows, ticks and styles go."""
        for key in list(keys):
            self._state.pop(key, None)
            row = self._rows.pop(key, None)
            if row is not None:
                row.setParent(None)
                row.deleteLater()
                self._order.remove(key)
        self.empty.setVisible(not self._order)
        self._show_all_state()

    # ── what the owner reads ──────────────────────────────────────────
    def keys(self):
        return list(self._order)

    def row(self, key):
        return self._rows.get(key)

    def ticked(self):
        """[(entry key, kind)] that are ticked, in list order (top first),
        cells before nuclei."""
        out = []
        for key in self._order:
            row = self._rows[key]
            for kind in KINDS:
                if kind in row.info.has and self._state[key]["ticks"].get(kind):
                    out.append((key, kind))
        return out

    def style(self, key, kind):
        return self._state_of(key)["styles"][kind]

    # ── inside ────────────────────────────────────────────────────────
    def _state_of(self, key):
        state = self._state.get(key)
        if state is None:
            i = self._seen
            self._seen += 1
            styles = {CELL: dataclasses.replace(DEFAULT_STYLES[CELL],
                                                color=_rgb(PALETTE[(2 * i) % len(PALETTE)])),
                      NUCLEUS: dataclasses.replace(DEFAULT_STYLES[NUCLEUS],
                                                   color=_rgb(PALETTE[(2 * i + 1) % len(PALETTE)]),
                                                   dash=True)}
            state = {"ticks": {CELL: False, NUCLEUS: False}, "styles": styles}
            self._state[key] = state
        return state

    def _ticked(self, key, kind, on):
        self._state_of(key)["ticks"][kind] = bool(on)
        self._show_all_state()
        self.ticks_changed.emit()

    def _styled(self, key, kind, change):
        state = self._state_of(key)
        state["styles"][kind] = dataclasses.replace(state["styles"][kind], **change)
        self.style_changed.emit(key, kind, dict(change))

    def _all_clicked(self, kind):
        on = self.all_ticks[kind].checkState() == Qt.Checked
        changed = False
        for key in self._order:
            row = self._rows[key]
            if kind not in row.info.has or row.ticks[kind].isChecked() == on:
                continue
            row.ticks[kind].blockSignals(True)
            row.ticks[kind].setChecked(on)
            row.ticks[kind].blockSignals(False)
            self._state_of(key)["ticks"][kind] = on
            changed = True
        self._show_all_state()
        if changed:
            self.ticks_changed.emit()

    def _show_all_state(self):
        for kind, check in self.all_ticks.items():
            rows = [self._rows[k] for k in self._order if kind in self._rows[k].info.has]
            on = sum(1 for r in rows if r.ticks[kind].isChecked())
            check.blockSignals(True)
            check.setEnabled(bool(rows))
            check.setCheckState(Qt.Unchecked if not on else
                                Qt.Checked if on == len(rows) else Qt.PartiallyChecked)
            check.blockSignals(False)
