"""Per-widget styles from the theme's tokens (docs/v17_desktop_ui_plan.md
§10; user rulings 2026-11-03).

Every function returns or sets a widget's OWN style sheet: the role's colours
for rest / hover / pressed / checked / disabled, plus the widget's METRICS
(padding, font size, border width), which stay what they were -- geometry is
the widget's, never the theme's (plan §8). Built from the theme chosen at
start-up, so a test without the application sheet sees the same sizes.

Roles (closed set):
  panel   THE in-panel button: neutral grey (user ruling: option A)
  run     the page's run action, green (#22AA55) -- the only green
  danger  Stop / Delete: red text and hairline
  ghost   tool rows over a viewer: no fill until hovered
  toggle  on/off: neutral, checked = active grey (no green, ruling 4)
  seg     one-of-N (Original / TopHat / cuCIM): checked = active grey
"""

from ...qt import QtGui

from . import current_name, tokens

#: the One Dark accents the panels use, and their light-theme versions
#: (One Dark is too pale on white -- plan §10)
_LIGHT_ACCENT = {
    "#61afef": "#2F7FD6", "#e5c07b": "#B7791F", "#c678dd": "#8E44C8",
    "#98c379": "#3E9C45", "#e06c75": "#D13B47", "#d19a66": "#C2681F",
    "#56b6c2": "#1F8C9A", "#7dd3fc": "#1F7FB0",
}
_RUN = {"dark": ("#22AA55", "#2DBB66", "#1E9349"),
        "light": ("#22AA55", "#1E9349", "#1A8040")}


def t():
    return tokens.THEMES[current_name()]


def _rgba(hexc, alpha):
    c = QtGui.QColor(hexc)
    return f"rgba({c.red()},{c.green()},{c.blue()},{alpha})"


def accent(color):
    """A panel colour as this theme shows it."""
    color = str(color).lower()
    if t()["name"] == "light":
        return _LIGHT_ACCENT.get(color, color)
    return color


def button_qss(role, metrics="", cls="QPushButton"):
    """`cls`'s style sheet in `role`'s colours, with `metrics` (padding,
    font-size, font-weight ...) as the widget's own."""
    k = t()
    radius = "border-radius:6px;"
    if role == "panel":
        rest = (f"background:{k['layer2']};color:{k['text2']};"
                f"border:1px solid {k['border']};{radius}")
        hover = f"background:{k['layer3']};color:{k['text']};"
        pressed = f"background:{k['active']};"
        checked = f"background:{k['active']};color:{k['text']};border:1px solid {k['border_strong']};"
        disabled = (f"background:{k['layer1']};color:{k['disabled_text']};"
                    f"border:1px solid {k['border']};")
    elif role == "run":
        fill, hov, prs = _RUN[k["name"]]
        rest = f"background:{fill};color:#FFFFFF;border:none;{radius}"
        hover, pressed, checked = f"background:{hov};", f"background:{prs};", ""
        disabled = f"background:{k['layer3']};color:{k['disabled_text']};"
    elif role == "danger":
        d = k["danger"]
        rest = f"background:transparent;color:{d};border:1px solid {_rgba(d, 0.45)};{radius}"
        hover = f"background:{_rgba(d, 0.12 if k['name'] == 'dark' else 0.08)};"
        pressed = f"background:{_rgba(d, 0.20 if k['name'] == 'dark' else 0.14)};"
        checked = ""
        disabled = f"color:{k['disabled_text']};border:1px solid {k['border']};"
    elif role == "ghost":
        rest = f"background:transparent;color:{k['text2']};border:1px solid transparent;{radius}"
        hover = f"background:{k['hover']};color:{k['text']};"
        pressed = f"background:{k['pressed']};"
        checked = f"background:{k['active']};color:{k['text']};"
        disabled = f"color:{k['disabled_text']};"
    elif role in ("toggle", "seg"):
        rest = (f"background:transparent;color:{k['text2'] if role == 'toggle' else k['text3']};"
                f"border:1px solid {k['border']};{radius}")
        hover = f"background:{k['hover']};color:{k['text']};"
        pressed = f"background:{k['pressed']};"
        checked = (f"background:{k['active']};color:{k['text']};"
                   f"border:1px solid {k['border_strong']};")
        disabled = f"color:{k['disabled_text']};border:1px solid {k['border']};"
    else:
        raise ValueError(f"no button role {role!r}")
    qss = f"{cls}{{{rest}{metrics}}}{cls}:hover{{{hover}}}{cls}:pressed{{{pressed}}}"
    if checked:
        # a checked button still answers the mouse: one step lighter
        lit = QtGui.QColor(k["active"]).lighter(118).name()
        qss += (f"{cls}:checked{{{checked}}}"
                f"{cls}:checked:hover{{{checked}background:{lit};}}")
    qss += f"{cls}:disabled{{{disabled}}}"
    return qss


def button(widget, role, metrics=""):
    """Give `widget` (a QPushButton / QToolButton) a role's look."""
    from ...qt import QtWidgets
    cls = "QToolButton" if isinstance(widget, QtWidgets.QToolButton) else "QPushButton"
    widget.setStyleSheet(button_qss(role, metrics, cls))
    return widget


def panel_box_qss(color, geometry=("border-radius:5px;margin-top:16px;"
                                   "font-weight:bold;font-size:11px;"),
                  title="subcontrol-origin:margin;subcontrol-position:top left;"
                        "left:8px;padding:0 4px;"):
    """A coloured panel (QGroupBox): the title in the panel's colour, the
    border that colour at 45 % (dark) / 55 % (light) -- the colour stays,
    the box is softer (plan §10). `geometry` / `title` keep the caller's."""
    c = accent(color)
    alpha = 0.55 if t()["name"] == "light" else 0.45
    return (f"QGroupBox{{border:1px solid {_rgba(c, alpha)};color:{c};{geometry}}}"
            f"QGroupBox::title{{{title}}}")


def neutral_box_qss(geometry=("border-radius:4px;margin-top:16px;"
                              "font-weight:bold;font-size:11px;"),
                    title="subcontrol-origin:margin;subcontrol-position:top left;"
                          "left:8px;padding:0 4px;"):
    """A panel without a colour of its own (Patches, Methods, Results)."""
    k = t()
    return (f"QGroupBox{{border:1px solid {k['border']};color:{k['text2']};{geometry}}}"
            f"QGroupBox::title{{{title}}}")


def frame_tab_qss(pane_radius=5):
    """The step frames' tab widgets, "attached" (ruling 2): the selected tab
    joins its pane; the metrics (padding 5px 12px, 1 px border) are the ones
    the frame's geometry was built on -- never bold."""
    k = t()
    return (f"QTabWidget::pane{{border:1px solid {k['border']};border-radius:{pane_radius}px;}}"
            f"QTabBar::tab{{background:transparent;color:{k['text3']};padding:5px 12px;"
            f"border:1px solid transparent;border-top-left-radius:6px;"
            f"border-top-right-radius:6px;}}"
            f"QTabBar::tab:hover{{background:{k['hover']};color:{k['text2']};}}"
            f"QTabBar::tab:pressed{{background:{k['pressed']};}}"
            f"QTabBar::tab:selected{{background:{k['layer1']};color:{k['text']};"
            f"border:1px solid {k['border']};border-bottom-color:{k['layer1']};}}")


def caption_qss(metrics="font-size:10px;", tone="text3"):
    """A quiet line (status, hint): the theme's grey, or a state colour
    (tone = success / danger / warning)."""
    return f"color:{t()[tone]};{metrics}"


def step_title_html(step, name):
    """"Step 2 — Segmentation & Merge": the number grey, the name bright
    (ruling 6)."""
    k = t()
    name = str(name).replace("&", "&amp;")
    return (f'<span style="color:{k["caption"]}">{step}</span>'
            f'<span style="color:{k["disabled_text"]}"> — </span>'
            f'<span style="color:{k["text"]};font-weight:600">{name}</span>')
