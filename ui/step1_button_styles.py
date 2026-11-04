"""Step1's small picture buttons -- one definition for every place they appear.

`MODE_BUTTON_QSS` is the look of the Viewer's Overlay / Fusion buttons; the
Pre-seg Results montage uses the very same string for its own pair (user
ruling, 2026-09-25: they must look the same). `layer_button_qss` is the same
shape, font and border for the montage's Membrane / nucleus toggles, lit in
the colour that layer has in the Fusion picture (cyto red, nucleus blue) so
they read as "which part of the picture", not as a third mode.
"""

from .theme import widgets as _theme_widgets

# On/off in the theme's neutral "toggle" (docs/v17_desktop_ui_plan.md §10,
# ruling 4: checked is the active grey, not green -- green is the run
# buttons'). Metrics as before.
MODE_BUTTON_QSS = _theme_widgets.button_qss(
    "toggle", "padding:2px 10px;font-size:10px;",
) + "QPushButton:checked{font-weight:bold;}"

#: The colour a Fusion layer has on screen (`FusionEngine.to_rgb`: cyto in
#: red, nucleus in blue).
LAYER_COLORS = {"membrane": "#d9534f", "nucleus": "#4a7bd6"}


def layer_button_qss(layer):
    """The toggle's look, lit in the layer's own colour when on -- a data
    colour, kept (plan §10)."""
    lit = LAYER_COLORS[layer]
    return (_theme_widgets.button_qss("toggle", "padding:2px 10px;font-size:10px;")
            + f"QPushButton:checked{{background:{lit};color:#fff;font-weight:bold;"
            f"border:1px solid {lit};}}"
            f"QPushButton:checked:hover{{background:{lit};}}")


#: The titled frame of a section of the Pre-segmentation tab -- Patches,
#: Methods, Results (user ruling 2026-09-25, design A) -- the very look of
#: the "Segmentation Method" box of the controls below them
#: (`ui/step0/search_ctrl.py`; a test holds the two equal).
SECTION_BOX_QSS = _theme_widgets.neutral_box_qss()
