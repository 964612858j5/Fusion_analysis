"""Step1's small picture buttons -- one definition for every place they appear.

`MODE_BUTTON_QSS` is the look of the Viewer's Overlay / Fusion buttons; the
Pre-seg Results montage uses the very same string for its own pair (user
ruling, 2026-09-25: they must look the same). `layer_button_qss` is the same
shape, font and border for the montage's Membrane / nucleus toggles, lit in
the colour that layer has in the Fusion picture (cyto red, nucleus blue) so
they read as "which part of the picture", not as a third mode.
"""

MODE_BUTTON_QSS = (
    "QPushButton{color:#9bd0ff;background:#182230;"
    "border:1px solid #354a63;border-radius:4px;"
    "padding:2px 10px;font-size:10px;}"
    "QPushButton:checked{background:#2a5;color:#111;font-weight:bold;}")

#: The colour a Fusion layer has on screen (`FusionEngine.to_rgb`: cyto in
#: red, nucleus in blue).
LAYER_COLORS = {"membrane": "#d9534f", "nucleus": "#4a7bd6"}


def layer_button_qss(layer):
    lit = LAYER_COLORS[layer]
    return (
        "QPushButton{color:#8a96a6;background:#182230;"
        "border:1px solid #354a63;border-radius:4px;"
        "padding:2px 10px;font-size:10px;}"
        f"QPushButton:checked{{background:{lit};color:#fff;font-weight:bold;"
        f"border:1px solid {lit};}}")
