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

from . import template
from .model import ChannelSetModel

# Re-exported for the existing importers; both are defined in `template`,
# which is the one place a channel list's look is decided.
_DOCK_STYLE = template.DOCK_QSS
ROW_HEIGHT = template.ROW_HEIGHT


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
        self.setStyleSheet(template.DOCK_QSS)
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
        template.apply_list_style(self.list_widget)
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
        """Rebuild the rows, keeping the list's own UI state.

        Search text, scroll position, the current channel and keyboard focus
        belong to the LIST, not to the data: a rebuild is the same channels
        drawn again, and losing where the user was scrolled to is not part of
        that. Restored with signals blocked, so putting the current item back
        is not a selection the model hears about.
        """
        keep_scroll = self.list_widget.verticalScrollBar().value()
        keep_focus = self.list_widget.hasFocus()
        keep_current = self._model.selected()
        self.list_widget.blockSignals(True)
        self.list_widget.clear()
        self._rows.clear()
        self._items.clear()
        for cid in self._model.order():
            item = QtWidgets.QListWidgetItem(self.list_widget)
            row = template.require_template_row(
                self._row_factory(self._model, cid))
            item.setSizeHint(template.item_size_hint(row))
            self.list_widget.setItemWidget(item, row)
            if hasattr(row, "color_clicked"):
                row.color_clicked.connect(self.color_edit_requested.emit)
            self._rows[cid] = row
            self._items[cid] = item
        # ONE name column for the list -- the longest name -- so every row's
        # accessory starts at the same x. The dock owns this; a host adapter
        # setting its own widths is how two lists came to disagree.
        template.uniform_name_width(list(self._rows.values()))
        self.list_widget.blockSignals(False)
        self._apply_filter()
        sel = keep_current or self._model.selected()
        if sel:
            self._on_model_selection(sel)
        self.list_widget.verticalScrollBar().setValue(keep_scroll)
        if keep_focus:
            self.list_widget.setFocus(Qt.OtherFocusReason)

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
