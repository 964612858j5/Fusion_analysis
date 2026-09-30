"""Step3 -- the QC viewer, rebuilt as a simplified Step1 (block 2c-1).

User ruling, 2026-09-26: Step3 has Step1's layout and look and reuses Step1's
parts; it is Step1 with the channel panel and the viewer only. Step3 shares
Step1's ticks, weights, fusion draft, Overlay / Fusion mode and Tissue
Preview (block 2b), so everything here that changes the picture is a
Step1 action handed in by the window.

This module only LAYS OUT. It builds no control that decides anything, reads
and writes no file, and owns no state: the window builds the controls from
the same style sources as Step1's and hands them in (`assemble`), and the one
public channel dock is mounted into `channels_host()` like every other step.

Right-hand side: one row over the viewer slot -- the mask controls (block
4c: the run drop-down, `Cell mask ▾`, `Nucleus mask ▾`, the hint line) and
the Overlay / Fusion pair at its right end.
"""

from PyQt5 import QtWidgets
from PyQt5.QtCore import Qt, pyqtSignal
from PyQt5.QtWidgets import (
    QHBoxLayout, QLabel, QPushButton, QSizePolicy, QVBoxLayout, QWidget,
)

from .step_frame import StepFrame

#: What the viewer slot says until block 2c-2 connects the viewer.
VIEWER_PENDING_TEXT = ("The whole-slide view is not connected yet.\n"
                       "Channels, weights and the Overlay / Fusion mode already "
                       "act on Step1's picture and the Tissue Preview.")


class Step3Page(QWidget):
    """The Step3 page: a container the window fills (`assemble`)."""

    go_back = pyqtSignal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self._channels_host = None
        self._splitter = None
        self._viewer_layout = None
        self._viewer_notice = None
        self._frame = None
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)

    # ── built once, by the window ─────────────────────────────────────
    def assemble(self, *, title_bar, metrics, free_tab_bar, frame_qss,
                 header_widgets, header_margins, header_spacing,
                 weight_widgets, mode_widgets, mask_widgets=(), mask_hint=None,
                 corner_widget=None):
        """Lay out what the window built, where Step1 has it.

        `title_bar` is Step1's kind of bar (name + Tissue Navigator);
        `metrics` / `free_tab_bar` are the page frame's (block A1b S1: the
        same frame, so the same geometry, as Step1's page); `frame_qss` is the
        `Channels` frame's; the widget lists are Step1's controls, already
        connected to Step1's actions. `mask_widgets` and `mask_hint` open
        the viewer's top row (block 4c; the patch strip first, plan step 3);
        the hint takes the spare width. `corner_widget` (the run drop-down)
        sits at the right of the tab bar's row, outside the tab's content.
        """
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)
        frame = StepFrame(metrics, free_tab_bar)
        outer.addWidget(frame)
        self._frame = frame
        frame.set_title(title_bar)
        split = frame.splitter
        self._splitter = split

        # LEFT -- Step1's `Channels` frame: header row, rule, weight tools,
        # then the dock's list (mounted at the end by the window).
        left = QWidget()
        left.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        left.setStyleSheet("background:#1c1c1c;")
        left_lay = QVBoxLayout(left)
        left_lay.setContentsMargins(0, 3, 0, 0)
        left_lay.setSpacing(4)
        box = QtWidgets.QGroupBox("Channels")
        box.setStyleSheet(frame_qss)
        box_lay = QVBoxLayout(box)
        box_lay.setContentsMargins(4, 4, 4, 4)
        box_lay.setSpacing(4)
        header = QHBoxLayout()
        header.setContentsMargins(*header_margins)
        header.setSpacing(header_spacing)
        for widget in header_widgets:
            header.addWidget(widget)
        header.addStretch()
        box_lay.addLayout(header)
        rule = QtWidgets.QFrame()
        rule.setFrameShape(QtWidgets.QFrame.HLine)
        rule.setStyleSheet("color:#333;")
        box_lay.addWidget(rule)
        weights = QHBoxLayout()
        weights.setSpacing(4)
        for widget in weight_widgets:
            weights.addWidget(widget)
        weights.addStretch()
        box_lay.addLayout(weights)
        self._channels_host = box_lay
        left_lay.addWidget(box, 1)
        self._left_panel = left
        frame.left_tabs.addTab(left, "Fusion")

        # RIGHT -- the mode pair over the viewer slot.
        right = QWidget()
        right.setMinimumSize(300, 300)
        right_lay = QVBoxLayout(right)
        mode_row = QHBoxLayout()
        mode_row.setSpacing(4)
        for widget in mask_widgets:
            mode_row.addWidget(widget)
        if mask_hint is not None:
            mode_row.addSpacing(6)
            mode_row.addWidget(mask_hint, 1)
        else:
            mode_row.addStretch()
        for widget in mode_widgets:
            mode_row.addWidget(widget)
        mode_row.addSpacing(8)
        frame.tool_row(right_lay, mode_row)
        notice = QLabel(VIEWER_PENDING_TEXT)
        notice.setAlignment(Qt.AlignCenter)
        notice.setWordWrap(True)
        notice.setStyleSheet("color:#888;font-size:12px;background:#111;")
        right_lay.addWidget(notice, 1)
        self._viewer_layout = right_lay
        self._viewer_notice = notice
        right_tabs = frame.right_tabs
        right_tabs.addTab(right, "Viewer")
        if corner_widget is not None:
            right_tabs.setCornerWidget(corner_widget, Qt.TopRightCorner)
        self._right_tabs = right_tabs

        back = QPushButton("← Back to Step 2")
        back.setStyleSheet("padding:6px 16px;background:#333;color:#ddd;border-radius:4px;")
        back.clicked.connect(self.go_back.emit)
        frame.bottom_layout.addWidget(back)
        frame.bottom_layout.addStretch()

    # ── what the window reads ─────────────────────────────────────────
    def channels_host(self):
        """Where the one public channel dock is mounted in Step3."""
        return self._channels_host

    def left_panel(self):
        """The `Fusion` tab's page: the column whose floor the window holds."""
        return getattr(self, "_left_panel", None)

    def channel_column_splitter(self):
        """The handle that joins the shared channel-column width."""
        return self._splitter

    def viewer_layout(self):
        """The viewer slot's layout (block 2c-2 installs the viewer here)."""
        return self._viewer_layout

    def viewer_notice(self):
        """The notice in the viewer slot -- the viewer's fallback widget
        (block 2c-2): hidden while the viewer draws, shown when it cannot."""
        return self._viewer_notice

    def set_viewer_notice(self, text=None):
        """Say why the viewer is not drawing; None puts the default back."""
        if self._viewer_notice is not None:
            self._viewer_notice.setText(VIEWER_PENDING_TEXT if text is None else str(text))
