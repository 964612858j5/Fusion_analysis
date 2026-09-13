"""THE channel-row visual template: one source for every channel list.

WHY THIS MODULE EXISTS. Block01 drew channel rows in more than one place, and
before this it BUILT them more than once: the shared dock's row and Step1's
private `ConfigPanel.ChannelRow` each created their own checkbox, swatch and
name label, each with their own stylesheet, and Step1 carried a second list
stylesheet and a second colour palette on top. Both are gone with B5; the one
row left is `global_dock.GlobalChannelRow`, and it is built here. The
result was measurable rather than theoretical -- on a real window at 1500x950,
the same channel showed a 22x18 themed checkbox in Step0 and a 14x15 platform
one in Step1, its swatch at x=50 against x=25, its name at x=68 against x=43,
and a row 25 px tall against 22 px.

So the ROW CORE is built here and only here:

    checkbox | state slot | swatch | name | <accessory supplied by the host>

A host page supplies the accessory -- Step0's correction-method combo, Step1's
weight slider and number -- and nothing else. It does not build the core, does
not restyle it, and does not move its columns. `build_row_core` stamps the
widget with `CORE_TOKEN`, and `require_template_row` refuses one that is not
stamped; that is what makes "one template" a rule rather than a convention.

THE STATE SLOT IS ALWAYS THERE. Step0 draws a compute-state glyph in it;
Step1 draws nothing. It keeps its 12 px either way, because if it collapsed
the swatch and the name would sit at different x in the two steps -- which is
exactly the difference this module removes.

WHAT IS NOT HERE. No model, no signals, no scientific meaning. This is the
visual template; ownership of visibility, weight, colour and correction is
unchanged by it and belongs to the pages and to `ChannelDisplayState`.
"""

import os

from PyQt5 import QtCore, QtWidgets
from PyQt5.QtCore import Qt
from PyQt5.QtWidgets import QWIDGETSIZE_MAX

# ── the stamp ────────────────────────────────────────────────────────────
# Identity, not a boolean: a host cannot set it by accident, and a row that
# does not carry it did not come from this module.
CORE_TOKEN = object()

# ── geometry tokens ──────────────────────────────────────────────────────
ROW_HEIGHT = 26                 # the list item's height
# The row WIDGET's height inside that item: the item minus the 1 px separator
# the list draws between rows (`QListWidget::item { border-bottom: 1px }`).
# Stated as a token and applied to every row, because otherwise each row is as
# tall as its own accessories allow -- which made Step0's row 25 px and
# Step1's 22 px from the same 26 px item.
ROW_CONTENT_HEIGHT = ROW_HEIGHT - 1
ROW_MARGINS = (6, 2, 6, 2)
ROW_SPACING = 5
CHECKBOX_SIZE = (22, 18)        # footprint, so a restyled indicator never re-flows
INDICATOR_SIZE = 13
STATE_SLOT_WIDTH = 12           # kept even when empty -- see the module docstring
SWATCH_SIZE = 13
NAME_WIDTH_PADDING = 6          # added to the longest name in the list
ACCESSORY_SPACING = 5

# ── colour tokens ────────────────────────────────────────────────────────
COLOR_BACKGROUND = "#101620"
COLOR_BORDER = "#253246"
COLOR_ROW_SEPARATOR = "#202c3b"
COLOR_HOVER = "#1a3e33"
COLOR_SELECTED = "#1a2b3e"
COLOR_FIELD_BG = "#182230"
COLOR_FIELD_BORDER = "#354a63"
COLOR_TEXT = "#dce5ef"
COLOR_ACCENT = "#9bd0ff"
COLOR_MUTED = "#6d8196"
COLOR_FRAME = "#61afef"
NAME_FONT_PX = 11

# THE palette. One deal for the process, in Step0's order, because Step0's is
# what `ChannelDisplayState` already serves as its default and therefore what
# every view has been showing. Step1 used to carry a second, different list;
# a standalone panel now gets this one too, so "no shared state yet" can never
# be a reason to deal a different colour.
CHANNEL_PALETTE = [
    "#00ff00", "#ff0000", "#00ffff", "#ff00ff", "#ffff00",
    "#ffffff", "#ff8800", "#88ff00", "#0088ff", "#ff0088",
]


def palette_color(index):
    """The default colour for the channel at `index`."""
    return CHANNEL_PALETTE[int(index) % len(CHANNEL_PALETTE)]


# ── stylesheet tokens ────────────────────────────────────────────────────
_CHECK_ICON = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                           "check.svg").replace(os.sep, "/")

# An explicitly drawn indicator that sits visibly on top of the list
# highlight, and a CHECKMARK rather than a filled box when checked.
CHECKBOX_INDICATOR_QSS = (
    f"QCheckBox::indicator{{width:{INDICATOR_SIZE}px;height:{INDICATOR_SIZE}px;"
    f"border-radius:2px;border:1px solid {COLOR_MUTED};"
    f"background:{COLOR_FIELD_BG};}}"
    f"QCheckBox::indicator:checked{{background:{COLOR_FIELD_BG};"
    f"border:1px solid {COLOR_ACCENT};image:url({_CHECK_ICON});}}"
    f"QCheckBox::indicator:disabled{{border:1px solid #3a4a5c;"
    f"background:#141b26;}}"
)

NAME_QSS = f"color:{COLOR_TEXT};font-size:{NAME_FONT_PX}px;"

# The state slot carries an explicit font size even when it is EMPTY. An
# unstyled empty label measures itself with the application font, which on
# this desk is tall enough to set the whole row's minimum height -- so Step0,
# whose glyph stylesheet already pinned a font size, produced a 25 px row and
# Step1, whose slot was empty and unstyled, produced 27 px from the same
# template.
STATE_SLOT_QSS = f"color:{COLOR_MUTED};font-size:{NAME_FONT_PX}px;"

# Rows and their children stay transparent so the list's hover/selected
# highlight paints through; a host page's opaque section stylesheet would
# otherwise cascade boxes onto every child.
ROW_QSS = "*{background:transparent;}" + CHECKBOX_INDICATOR_QSS

LIST_QSS = (
    f"QListWidget {{ background:{COLOR_BACKGROUND};"
    f" border:1px solid {COLOR_BORDER}; border-radius:4px; }}"
    f"QListWidget::item {{ border-bottom:1px solid {COLOR_ROW_SEPARATOR}; }}"
    f"QListWidget::item:hover {{ background:{COLOR_HOVER}; }}"
    f"QListWidget::item:selected {{ background:{COLOR_SELECTED}; }}"
    # WITHOUT this last rule a selected row under the cursor falls back to the
    # green hover colour, which is what Step1 did and Step0 did not.
    f"QListWidget::item:selected:hover {{ background:{COLOR_SELECTED}; }}"
)

DOCK_QSS = (
    f"QWidget#ChannelDockRoot {{ background:{COLOR_BACKGROUND}; }}"
    f"QLineEdit {{ background:{COLOR_FIELD_BG}; color:{COLOR_TEXT};"
    f" border:1px solid {COLOR_FIELD_BORDER}; border-radius:4px;"
    f" padding:2px 6px; font-size:10px; }}"
    f"QPushButton#dockTool {{ color:{COLOR_ACCENT}; background:{COLOR_FIELD_BG};"
    f" border:1px solid {COLOR_FIELD_BORDER}; border-radius:4px;"
    f" padding:2px 8px; font-size:10px; }}"
    f"QPushButton#dockTool:hover {{ background:#23354a; }}"
) + LIST_QSS

# The accessory styling family: a host's own controls look like they belong to
# the row without being allowed to restyle it.
ACCESSORY_FIELD_QSS = (
    f"background:{COLOR_FIELD_BG};color:{COLOR_TEXT};"
    f"border:1px solid {COLOR_FIELD_BORDER};border-radius:3px;font-size:10px;"
)
ACCESSORY_COMBO_QSS = (
    f"QComboBox{{{ACCESSORY_FIELD_QSS}padding:1px 2px;}}"
    "QComboBox::drop-down{border:none;}"
    "QComboBox:disabled{color:#555;}"
)
ACCESSORY_SPINBOX_QSS = f"QDoubleSpinBox{{{ACCESSORY_FIELD_QSS}}}"


def frame_qss(color=COLOR_FRAME):
    """The outer "Channels" frame: border, title gap and title position.

    Reserve vertical room for the title (margin-top) AND position the title
    sub-control in that margin so it sits ABOVE the border/body -- without the
    ::title rule and enough margin, the first body child rides up and occludes
    the title (the styled-QGroupBox "eats its title" bug).
    """
    return (
        f"QGroupBox{{border:1px solid {color};border-radius:5px;margin-top:16px;"
        f"font-weight:bold;color:{color};font-size:11px;}}"
        f"QGroupBox::title{{subcontrol-origin:margin;subcontrol-position:top left;"
        f"left:8px;padding:0 4px;}}"
    )


# ── the row core ─────────────────────────────────────────────────────────
class RowCore:
    """The widgets every channel row has, in the order it has them."""

    __slots__ = ("layout", "checkbox", "state_slot", "swatch", "name_label")

    def __init__(self, layout, checkbox, state_slot, swatch, name_label):
        self.layout = layout
        self.checkbox = checkbox
        self.state_slot = state_slot
        self.swatch = swatch
        self.name_label = name_label


SWATCH_TOOLTIP = "Click to change display color"


def build_row_core(row, name="", show_visibility=True, checkable=True,
                   tooltips=True):
    """Build `row`'s common columns and stamp it as template-built.

    `row` is any QWidget; this installs the layout and the four common
    widgets, applies the tokens and returns them. The caller appends its
    accessory through `add_accessory` and touches nothing else.

    `tooltips=False` is for a host whose rows are contractually free of hover
    text -- Step1's, by an explicit product decision that a popup over every
    control in a long channel list is in the way of the work. Hover TEXT is
    content, not visual template; the geometry and the style are the same
    either way.
    """
    layout = QtWidgets.QHBoxLayout(row)
    layout.setContentsMargins(*ROW_MARGINS)
    layout.setSpacing(ROW_SPACING)

    checkbox = QtWidgets.QCheckBox()
    checkbox.setFixedSize(*CHECKBOX_SIZE)
    checkbox.setEnabled(bool(checkable))
    checkbox.setVisible(bool(show_visibility))
    layout.addWidget(checkbox)

    # Always present, always this wide. See the module docstring.
    state_slot = QtWidgets.QLabel("")
    state_slot.setAlignment(Qt.AlignCenter)
    state_slot.setFixedWidth(STATE_SLOT_WIDTH)
    state_slot.setStyleSheet(STATE_SLOT_QSS)
    layout.addWidget(state_slot)

    swatch = QtWidgets.QLabel()
    swatch.setFixedSize(SWATCH_SIZE, SWATCH_SIZE)
    swatch.setCursor(Qt.PointingHandCursor)
    if tooltips:
        swatch.setToolTip(SWATCH_TOOLTIP)
    layout.addWidget(swatch)

    name_label = QtWidgets.QLabel(name)
    name_label.setStyleSheet(NAME_QSS)
    if tooltips:
        name_label.setToolTip(name)
    # Non-stretching: the list gives every row the same name width (see
    # `uniform_name_width`) so the accessories line up down the column.
    name_label.setSizePolicy(QtWidgets.QSizePolicy.Fixed,
                             QtWidgets.QSizePolicy.Preferred)
    layout.addWidget(name_label)

    # A trailing stretch, always. Without it a row whose accessory does not
    # expand has its whole core centred by the layout -- the checkbox of a
    # plain row sat at x=67 instead of x=6. Accessories go in through
    # `add_accessory`, which inserts BEFORE this stretch.
    layout.addStretch(1)

    row.setStyleSheet(ROW_QSS)
    # THE TEMPLATE OWNS THE ROW HEIGHT, not the accessories. Left to the
    # layout, a row is as tall as its own children allow: Step1's (a 16 px
    # slider, a 20 px number box) settled at 22 px while Step0's, which has an
    # unbounded status label, filled 25 px -- from the same 26 px item.
    row.setMinimumHeight(ROW_CONTENT_HEIGHT)
    row.setMaximumHeight(QWIDGETSIZE_MAX)
    row._channel_row_core = CORE_TOKEN
    return RowCore(layout, checkbox, state_slot, swatch, name_label)


def add_accessory(layout, widget, stretch=0):
    """Append a host's accessory, keeping the core left-aligned.

    Inserted before the trailing stretch `build_row_core` put there, so the
    common columns stay where the template placed them whatever the host adds.
    """
    layout.insertWidget(layout.count() - 1, widget, stretch)
    return widget


def swatch_qss(color):
    return (f"background:{color};border:1px solid {COLOR_FIELD_BORDER};"
            f"border-radius:2px;")


def apply_swatch_color(swatch, color):
    swatch.setStyleSheet(swatch_qss(color))


def is_template_row(row):
    """True when `row`'s core was built here."""
    return getattr(row, "_channel_row_core", None) is CORE_TOKEN


def require_template_row(row):
    """Refuse a row whose core did not come from this module.

    The dock calls this on every row it builds. A host that returns its own
    widget -- the escape hatch that let two steps drift apart -- fails here
    rather than silently producing a second template.
    """
    if not is_template_row(row):
        raise TypeError(
            f"{type(row).__name__} was not built from the shared channel row "
            "template. A host supplies the accessory; the row core "
            "(checkbox, state slot, swatch, name) comes from "
            "channel_dock.template.build_row_core.")
    return row


def uniform_name_width(rows, padding=NAME_WIDTH_PADDING):
    """Give every row the same name column: the longest name plus `padding`.

    One width for the list, so the accessories line up. Measured from the
    text each label is actually showing, which includes any marker a host has
    appended (Step0's nucleus star, Step1's ambiguity asterisk) -- so a marker
    widens the column for everyone rather than shifting one row's columns.
    """
    labels = [r.name_label for r in rows
              if getattr(r, "name_label", None) is not None]
    if not labels:
        return 0
    width = max(lbl.fontMetrics().boundingRect(lbl.text()).width()
                for lbl in labels) + int(padding)
    for lbl in labels:
        lbl.setFixedWidth(width)
    return width


def apply_list_style(list_widget):
    """The one list shell: style, scroll policy and per-pixel scrolling."""
    list_widget.setStyleSheet(LIST_QSS)
    list_widget.setVerticalScrollMode(QtWidgets.QListWidget.ScrollPerPixel)
    list_widget.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)


ACCESSORY_MAX_HEIGHT = ROW_HEIGHT - ROW_MARGINS[1] - ROW_MARGINS[3] - 2


def item_size_hint(row=None, width=10):
    """The list item's size. `ROW_HEIGHT`, for every row in every list.

    A TOKEN, not a measurement. It used to be
    `max(ROW_HEIGHT, row.sizeHint().height())`, which let one accessory decide
    the row height for its whole list: Step1's number box asks for 27 px and
    Step0's method combo for less, so the same channel was a different height
    in the two steps. An accessory that wants more than
    `ACCESSORY_MAX_HEIGHT` is the thing to fix, not the row.
    """
    return QtCore.QSize(width, ROW_HEIGHT)


def fit_accessory(widget):
    """Keep an accessory inside the row's height token."""
    widget.setMaximumHeight(ACCESSORY_MAX_HEIGHT)
    return widget
