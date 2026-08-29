"""Shared ChannelDock shell (v15 Workstream A).

Dark fixed-policy dock: fixed header (search + Show all / Hide all + optional
host extras), independently scrolling compact channel list, and a
selected-channel tool area. Rows are supplied by a page-specific row factory;
scientific semantics stay in the page adapter.

Visual reference: deepseek/step5_v8/agentic/montage_viewer_web.py (ideas only).
"""

from typing import Callable, Dict, Optional

from PyQt5 import QtCore, QtWidgets
from PyQt5.QtCore import Qt, pyqtSignal

from .model import ChannelSetModel

_DOCK_STYLE = """
QWidget#ChannelDockRoot { background:#101620; }
QLineEdit { background:#182230; color:#dce5ef; border:1px solid #354a63;
            border-radius:4px; padding:2px 6px; font-size:10px; }
QPushButton#dockTool { color:#9bd0ff; background:#182230;
            border:1px solid #354a63; border-radius:4px;
            padding:2px 8px; font-size:10px; }
QPushButton#dockTool:hover { background:#23354a; }
QListWidget { background:#101620; border:1px solid #253246; border-radius:4px; }
QListWidget::item { border-bottom:1px solid #202c3b; }
QListWidget::item:hover { background:#151d29; }
QListWidget::item:selected { background:#1a2b3e; }
QListWidget::item:selected:hover { background:#1a2b3e; }
"""

ROW_HEIGHT = 26


class ChannelDock(QtWidgets.QWidget):
    """Reusable channel sidebar shell.

    Shared: search/filter, bulk visibility, selection, ordering, colors,
    scroll behavior, styling. Page-specific: the row widgets (row_factory)
    and the tool-area widget (set_tool_widget).
    """

    color_edit_requested = pyqtSignal(str)     # swatch clicked on a row

    def __init__(self, model: ChannelSetModel,
                 row_factory: Callable[[ChannelSetModel, str], QtWidgets.QWidget],
                 parent=None, title: str = "Channels",
                 show_search: bool = True, show_bulk_buttons: bool = True,
                 fixed_width: Optional[int] = None):
        super().__init__(parent)
        self.setObjectName("ChannelDockRoot")
        self.setStyleSheet(_DOCK_STYLE)
        if fixed_width:
            self.setFixedWidth(fixed_width)

        self._model = model
        self._row_factory = row_factory
        self._rows: Dict[str, QtWidgets.QWidget] = {}
        self._items: Dict[str, QtWidgets.QListWidgetItem] = {}
        self._filter_text = ""

        lay = QtWidgets.QVBoxLayout(self)
        lay.setContentsMargins(4, 4, 4, 4)
        lay.setSpacing(4)

        # -- fixed header ---------------------------------------------------
        if title:
            hdr = QtWidgets.QLabel(title)
            hdr.setStyleSheet("color:#9bd0ff;font-size:11px;font-weight:bold;")
            lay.addWidget(hdr)

        self.search = QtWidgets.QLineEdit()
        self.search.setPlaceholderText("Search channels…")
        self.search.setClearButtonEnabled(True)
        self.search.textChanged.connect(self.filter_rows)
        self.search.setVisible(show_search)
        lay.addWidget(self.search)

        tools = QtWidgets.QHBoxLayout()
        tools.setSpacing(4)
        self.btn_show_all = QtWidgets.QPushButton("Show all")
        self.btn_hide_all = QtWidgets.QPushButton("Hide all")
        for b in (self.btn_show_all, self.btn_hide_all):
            b.setObjectName("dockTool")
            tools.addWidget(b)
        tools.addStretch(1)
        self.header_extra = QtWidgets.QHBoxLayout()
        self.header_extra.setSpacing(4)
        tools.addLayout(self.header_extra)
        self.btn_show_all.clicked.connect(lambda: self._model.set_all_visible(True))
        self.btn_hide_all.clicked.connect(lambda: self._model.set_all_visible(False))
        self.btn_show_all.setVisible(show_bulk_buttons)
        self.btn_hide_all.setVisible(show_bulk_buttons)
        lay.addLayout(tools)

        # -- scrolling list ---------------------------------------------------
        self.list_widget = QtWidgets.QListWidget()
        self.list_widget.setVerticalScrollMode(QtWidgets.QListWidget.ScrollPerPixel)
        self.list_widget.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.list_widget.currentItemChanged.connect(self._on_current_item)
        lay.addWidget(self.list_widget, stretch=1)

        # -- selected-channel tool area --------------------------------------
        self.tool_area = QtWidgets.QVBoxLayout()
        self.tool_area.setContentsMargins(0, 0, 0, 0)
        lay.addLayout(self.tool_area)
        self._tool_widget: Optional[QtWidgets.QWidget] = None

        model.reset.connect(self.rebuild)
        model.selection_changed.connect(self._on_model_selection)
        self.rebuild()

    # -- structure --------------------------------------------------------
    def rebuild(self):
        self.list_widget.blockSignals(True)
        self.list_widget.clear()
        self._rows.clear()
        self._items.clear()
        for cid in self._model.order():
            item = QtWidgets.QListWidgetItem(self.list_widget)
            row = self._row_factory(self._model, cid)
            item.setSizeHint(QtCore.QSize(10, max(ROW_HEIGHT, row.sizeHint().height())))
            self.list_widget.setItemWidget(item, row)
            if hasattr(row, "color_clicked"):
                row.color_clicked.connect(self.color_edit_requested.emit)
            self._rows[cid] = row
            self._items[cid] = item
        self.list_widget.blockSignals(False)
        self._apply_filter()
        sel = self._model.selected()
        if sel:
            self._on_model_selection(sel)

    def row(self, cid: str) -> Optional[QtWidgets.QWidget]:
        return self._rows.get(cid)

    def rows(self) -> Dict[str, QtWidgets.QWidget]:
        return dict(self._rows)

    def item(self, cid: str) -> Optional[QtWidgets.QListWidgetItem]:
        return self._items.get(cid)

    def set_tool_widget(self, w: Optional[QtWidgets.QWidget]):
        if self._tool_widget is not None:
            self.tool_area.removeWidget(self._tool_widget)
            self._tool_widget.setParent(None)
        self._tool_widget = w
        if w is not None:
            self.tool_area.addWidget(w)

    def tool_widget(self) -> Optional[QtWidgets.QWidget]:
        return self._tool_widget

    # -- filtering ----------------------------------------------------------
    def filter_rows(self, text: str):
        self._filter_text = (text or "").strip().lower()
        self._apply_filter()

    def _apply_filter(self):
        for cid, item in self._items.items():
            st = self._model.get(cid)
            name = (st.name if st else cid).lower()
            item.setHidden(bool(self._filter_text) and self._filter_text not in name)

    def visible_row_ids(self):
        return [cid for cid, it in self._items.items() if not it.isHidden()]

    # -- selection ------------------------------------------------------------
    def _on_current_item(self, current, _prev):
        if current is None:
            return
        for cid, item in self._items.items():
            if item is current:
                self._model.select(cid)
                return

    def _on_model_selection(self, cid):
        item = self._items.get(cid)
        if item is not None and self.list_widget.currentItem() is not item:
            self.list_widget.blockSignals(True)
            self.list_widget.setCurrentItem(item)
            self.list_widget.blockSignals(False)
