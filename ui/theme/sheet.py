"""One application style sheet and palette from a token dict
(docs/v17_desktop_ui_plan.md §7-§8).

SCOPED: the sheet styles only widgets that carry a role -- the dynamic
property `ds` -- plus tool tips, menus and scroll bars. Nothing matches a
bare QWidget, so viewer canvases, GL widgets, pyqtgraph and overlays are
never touched. The palette gives unstyled chrome its base colours.

Roles (phase 1):
  ds=bar       the step bar and Step0's load row (a QFrame / QWidget)
  ds=step      a step in the step bar (QLabel); on=1 for the current one
  ds=stepsep   the arrow between two steps
  ds=field     a text field (QLineEdit)
  ds=primary   the main action of its row (Load, Save)
  ds=outline   a secondary button with a hairline (Browse, Tissue Navigator)
  ds=split     the triangle beside a primary button (Save's menu)
  ds=caption   a quiet status line

Buttons, tool buttons, tab bars and message boxes WITHOUT a role get the
neutral in-panel look (user rulings 2026-11-03: every button and tab
hovers); a widget's own sheet still wins property by property.

Menus and scroll bars take the palette's colours only: a sheet rule on them
changes their metrics (codex review: scroll bar 14 -> 10 px, the Save menu's
size), and geometry is not this phase's to change (plan §8).
"""

from PyQt5 import QtGui

from . import tokens as T


def _qcolor(value):
    value = str(value)
    if value.startswith("rgba("):
        r, g, b, a = (float(v) for v in value[5:-1].split(","))
        return QtGui.QColor(int(r), int(g), int(b), int(round(a * 255)))
    return QtGui.QColor(value)


def palette(t):
    """The base colours of everything the sheet does not style."""
    pal = QtGui.QPalette()
    roles = {
        QtGui.QPalette.Window: t["canvas"],
        QtGui.QPalette.WindowText: t["text"],
        QtGui.QPalette.Base: t["layer1"],
        QtGui.QPalette.AlternateBase: t["layer2"],
        QtGui.QPalette.Text: t["text"],
        QtGui.QPalette.Button: t["layer2"],
        QtGui.QPalette.ButtonText: t["text"],
        QtGui.QPalette.BrightText: t["danger"],
        QtGui.QPalette.Highlight: t["accent"],
        QtGui.QPalette.HighlightedText: t["on_primary"] if t["name"] == "dark" else "#FFFFFF",
        QtGui.QPalette.ToolTipBase: t["tooltip_bg"],
        QtGui.QPalette.ToolTipText: t["tooltip_text"],
        QtGui.QPalette.Link: t["accent"],
        QtGui.QPalette.PlaceholderText: t["caption"],
    }
    for role, value in roles.items():
        pal.setColor(role, _qcolor(value))
    for role in (QtGui.QPalette.WindowText, QtGui.QPalette.Text, QtGui.QPalette.ButtonText):
        pal.setColor(QtGui.QPalette.Disabled, role, _qcolor(t["disabled_text"]))
    return pal


def stylesheet(t):
    r_sm, r_md = T.RADIUS_SM, T.RADIUS_MD
    return f"""
QPushButton:!flat {{ background:{t['layer2']}; color:{t['text2']}; border:1px solid {t['border']};
    border-radius:{r_sm}px; padding:3px 10px; }}
QPushButton:!flat:hover {{ background:{t['layer3']}; color:{t['text']}; }}
QPushButton:!flat:pressed {{ background:{t['active']}; }}
QPushButton:!flat:checked {{ background:{t['active']}; color:{t['text']}; border:1px solid {t['border_strong']}; }}
QPushButton:!flat:disabled {{ background:{t['layer1']}; color:{t['disabled_text']}; }}
QToolButton {{ background:transparent; color:{t['text2']}; border:1px solid transparent;
    border-radius:{r_sm}px; padding:1px 2px; }}
QToolButton:hover {{ background:{t['hover']}; color:{t['text']}; }}
QToolButton:pressed {{ background:{t['pressed']}; }}
QTabBar::tab {{ background:transparent; color:{t['text3']}; padding:5px 12px;
    border:1px solid transparent; border-top-left-radius:{r_sm}px; border-top-right-radius:{r_sm}px; }}
QTabBar::tab:hover {{ background:{t['hover']}; color:{t['text2']}; }}
QTabBar::tab:selected {{ background:{t['layer1']}; color:{t['text']};
    border:1px solid {t['border']}; border-bottom-color:{t['layer1']}; }}
*[ds="bar"] {{ background:{t['bar']}; }}
QLabel[ds="step"] {{ font-size:12px; color:{t['caption']}; padding:4px 12px;
    border-radius:{r_md}px; background:transparent; }}
QLabel[ds="step"]:hover {{ color:{t['text2']}; background:{t['hover']}; }}
QLabel[ds="step"][on="1"] {{ color:{t['text']}; font-weight:bold;
    background:{t['active']}; }}
QLabel[ds="stepsep"] {{ color:{t['caption']}; background:transparent; }}
QLabel[ds="caption"] {{ color:{t['text3']}; background:transparent; }}
QLineEdit[ds="field"] {{ font-size:11px; background:{t['layer1']}; color:{t['text']};
    border:1px solid {t['border_strong']}; border-radius:{r_sm}px; padding:2px 6px;
    selection-background-color:{t['accent']}; }}
QLineEdit[ds="field"]:focus {{ border:1px solid {t['accent']}; }}
QPushButton[ds="outline"] {{ font-size:10px; color:{t['text']}; background:transparent;
    border:1px solid {t['border_strong']}; border-radius:{r_sm}px; padding:2px 8px; }}
QPushButton[ds="outline"]:hover {{ background:{t['hover']}; color:{t['text']}; }}
QPushButton[ds="outline"]:pressed {{ background:{t['pressed']}; color:{t['text']}; }}
QPushButton[ds="outline"]:disabled {{ color:{t['disabled_text']}; border-color:{t['border']}; }}
QPushButton[ds="primary"] {{ font-weight:bold; color:{t['on_primary']};
    background:{t['primary']}; border:none; border-radius:{r_md}px; padding:2px 14px; }}
QPushButton[ds="primary"]:hover {{ background:{t['primary_hover']}; color:{t['on_primary']}; }}
QPushButton[ds="primary"]:pressed {{ background:{t['text3']}; color:{t['on_primary']}; }}
QPushButton[ds="primary"]:disabled {{ background:{t['layer3']}; color:{t['disabled_text']}; }}
QPushButton[ds="primary"][corner="left"] {{ border-top-right-radius:0px;
    border-bottom-right-radius:0px; }}
QToolButton[ds="split"] {{ color:{t['on_primary']}; background:{t['primary']}; border:none;
    border-left:1px solid {t['border_strong']}; border-top-right-radius:{r_md}px;
    border-bottom-right-radius:{r_md}px; padding:0 6px; }}
QToolButton[ds="split"]:hover {{ background:{t['primary_hover']}; color:{t['on_primary']}; }}
QToolButton[ds="split"]:disabled {{ background:{t['layer3']}; color:{t['disabled_text']}; }}
QToolButton[ds="split"]::menu-indicator {{ image:none; width:0px; }}
QToolTip {{ color:{t['tooltip_text']}; background:{t['tooltip_bg']}; border:none;
    border-radius:{r_sm}px; padding:4px 8px; }}
QMessageBox {{ background:{t['layer1']}; }}
QMessageBox QLabel {{ color:{t['text']}; }}
QMessageBox QPushButton {{ background:transparent; color:{t['text']};
    border:1px solid {t['border_strong']}; border-radius:{r_md}px; padding:4px 14px; min-width:64px; }}
QMessageBox QPushButton:hover {{ background:{t['hover']}; }}
QMessageBox QPushButton:pressed {{ background:{t['pressed']}; }}
QMessageBox QPushButton:default {{ background:{t['primary']}; color:{t['on_primary']};
    border:1px solid {t['primary']}; font-weight:bold; }}
QMessageBox QPushButton:default:hover {{ background:{t['primary_hover']}; color:{t['on_primary']}; }}
QMessageBox QPushButton:default:pressed {{ background:{t['text3']}; color:{t['on_primary']}; }}
QMessageBox QPushButton[ds="danger"] {{ background:transparent; color:{t['danger']};
    border:1px solid {t['danger']}; font-weight:normal; }}
QMessageBox QPushButton[ds="danger"]:hover {{ background:{t['hover']}; color:{t['danger']}; }}
"""
