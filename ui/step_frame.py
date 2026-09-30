"""One page frame for every step (block A1b; plan v2.2 §4, user ruling
2026-09-30).

Each step page used to lay out its own margins, title, tabs, columns and
bottom row, so the same window put the channel panel and the viewer at
different places in each step and the picture moved when the user switched.
A `StepFrame` owns that geometry once:

    ┌ title slot (fixed height) ───────────────────────────────┐
    ├ left tabs ──────────┬ right tabs ─────────────────────────┤  <- one tab row
    │ left slot           │ tool row (fixed height)             │
    │                     │ content                             │
    ├ bottom slot (fixed height) ───────────────────────────────┤
    └───────────────────────────────────────────────────────────┘

This module ONLY LAYS OUT. It knows nothing about fusion, segmentation, ROIs,
masks or any particular step -- a page fills the slots. Every number comes
from one `StepFrameMetrics`, never from asking another page's widgets at run
time. The shared channel-column width is applied by the window
(`MainWindow._apply_channel_column_fraction`), the one place that sees every
page's splitter.
"""

from dataclasses import dataclass

from PyQt5 import QtWidgets
from PyQt5.QtCore import Qt
from PyQt5.QtWidgets import QHBoxLayout, QSizePolicy, QSplitter, QVBoxLayout, QWidget

# One tab look for every framed page's two tab widgets, so each page reads as
# one surface rather than two (moved here from `main_window` in S2: Step0
# builds its own frame and must not import the window).
FRAME_TAB_QSS = (
    "QTabWidget::pane{border:1px solid #444;border-radius:5px;}"
    "QTabBar::tab{background:#222;color:#bbb;padding:5px 12px;border:1px solid #444;}"
    "QTabBar::tab:selected{color:#fff;border-bottom-color:#111;}"
)


def free_tab_bar(tabs):
    """Let a tab widget be narrower than its labels.

    A QTabBar reports the width of every label as its minimum, and a column
    whose floor is its tab bar cannot follow a proportion. Eliding and
    scrolling keeps both tabs reachable at any width.
    """
    bar = tabs.tabBar()
    bar.setElideMode(Qt.ElideRight)
    bar.setUsesScrollButtons(True)
    bar.setExpanding(False)
    bar.setMinimumWidth(1)
    tabs.setMinimumWidth(1)
    tabs.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Expanding)


@dataclass(frozen=True)
class StepFrameMetrics:
    """The frame's geometry, in one place (block A1b S1).

    `title_height` and `bottom_height` are taken ONCE from Step0's load bar
    and Per-Channel Decision frame, when Step0 assembles its own frame (S2);
    every page reads these numbers, so no page's font or border moves
    another page.
    """

    title_height: int
    bottom_height: int
    tab_qss: str = ""
    page_margins: tuple = (6, 6, 6, 11)
    spacing: int = 4
    tool_height: int = 25
    tool_spacing: int = 6


class StepFrame(QWidget):
    """A step page's frame: title slot, two tabbed columns, bottom slot."""

    def __init__(self, metrics, free_tab_bar=None, parent=None):
        super().__init__(parent)
        self.metrics = metrics
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        root = QVBoxLayout(self)
        root.setContentsMargins(*metrics.page_margins)
        root.setSpacing(metrics.spacing)
        self.root_layout = root

        self.title_slot = QWidget()
        self.title_slot.setFixedHeight(metrics.title_height)
        self.title_slot.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        title_lay = QVBoxLayout(self.title_slot)
        title_lay.setContentsMargins(0, 0, 0, 0)
        title_lay.setSpacing(0)
        root.addWidget(self.title_slot)

        self.splitter = QSplitter(Qt.Horizontal)
        self.splitter.setChildrenCollapsible(False)
        self.left_tabs = self._tabs(metrics, free_tab_bar)
        self.left_tabs.setMinimumWidth(0)
        self.right_tabs = self._tabs(metrics, free_tab_bar)
        self.splitter.addWidget(self.left_tabs)
        self.splitter.addWidget(self.right_tabs)
        self.splitter.setStretchFactor(0, 1)
        self.splitter.setStretchFactor(1, 2)
        root.addWidget(self.splitter, 1)

        self.bottom_slot = QWidget()
        self.bottom_slot.setFixedHeight(metrics.bottom_height)
        self.bottom_slot.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        self.bottom_layout = QHBoxLayout(self.bottom_slot)
        self.bottom_layout.setContentsMargins(0, 0, 0, 0)
        root.addWidget(self.bottom_slot)

    @staticmethod
    def _tabs(metrics, free_tab_bar):
        tabs = QtWidgets.QTabWidget()
        if metrics.tab_qss:
            tabs.setStyleSheet(metrics.tab_qss)
        if free_tab_bar is not None:
            free_tab_bar(tabs)
        tabs.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        return tabs

    # ── filling the slots ─────────────────────────────────────────────
    def set_title(self, widget):
        """The page's title bar (Step0's load bar, a name + Tissue Navigator)."""
        self.title_slot.layout().addWidget(widget)
        return widget

    def tool_row(self, page_layout, row_layout):
        """Put `row_layout` in a fixed-height tool row at the top of a tab
        page laid out by `page_layout`; the content below keeps the page's
        own layout, one `tool_spacing` under the row."""
        row = QWidget()
        row.setFixedHeight(self.metrics.tool_height)
        row.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        row_layout.setContentsMargins(0, 0, 0, 0)
        row.setLayout(row_layout)
        page_layout.setContentsMargins(0, 0, 0, 0)
        page_layout.setSpacing(self.metrics.tool_spacing)
        page_layout.insertWidget(0, row)
        return row
