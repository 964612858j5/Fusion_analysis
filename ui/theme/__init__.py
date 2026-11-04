"""Block01's theme layer (docs/v17_desktop_ui_plan.md): DeepSeek Harness's
colours and button feel, dark by default, light the other choice.

`apply(app)` installs the palette and the ONE scoped style sheet; widgets take
a role with `role(widget, "primary")` instead of a colour literal. The theme
is chosen by `BLOCK01_THEME` or the user setting `ui/theme` (no visible
switch until the user says where one goes -- UI_SURFACE_RULES).
"""

import os

from ...qt import QtCore

from . import tokens
from .sheet import palette, stylesheet

SETTINGS_KEY = "ui/theme"


def current_name():
    """The theme asked for: the environment, the user setting, else dark."""
    name = os.environ.get("BLOCK01_THEME", "").strip().lower()
    if not name:
        try:
            name = str(QtCore.QSettings("Block01", "Block01").value(SETTINGS_KEY, "") or "")
        except Exception:                                   # noqa: BLE001
            name = ""
    return name if name in tokens.THEMES else tokens.DEFAULT


def apply(app, name=None):
    """Palette + style sheet of `name` (or the one asked for) on `app`."""
    t = tokens.THEMES.get(name or current_name(), tokens.THEMES[tokens.DEFAULT])
    app.setPalette(palette(t))
    app.setStyleSheet(stylesheet(t))
    app.setProperty("block01_theme", t["name"])
    return t["name"]


def role(widget, name, **flags):
    """Give `widget` a role of the theme's sheet (and flags such as on=1);
    re-polished, so a change after the widget is shown takes effect."""
    widget.setProperty("ds", name)
    for key, value in flags.items():
        widget.setProperty(key, value)
    style = widget.style()
    style.unpolish(widget)
    style.polish(widget)
    return widget


def set_flag(widget, key, value):
    """Change one flag of a themed widget (e.g. the current step)."""
    if widget.property(key) == value:
        return
    widget.setProperty(key, value)
    style = widget.style()
    style.unpolish(widget)
    style.polish(widget)


def adopt(target, source):
    """`target` looks like `source`: its role, flags and any style sheet of
    its own (what copying `styleSheet()` alone used to do)."""
    for key in ("ds", "corner"):
        value = source.property(key)
        if value is not None:
            target.setProperty(key, value)
    target.setStyleSheet(source.styleSheet())
    style = target.style()
    style.unpolish(target)
    style.polish(target)
    return target
