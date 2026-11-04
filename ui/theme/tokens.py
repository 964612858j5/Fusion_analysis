"""Block01's theme tokens, after DeepSeek Harness's design language
(docs/v17_desktop_ui_plan.md §2; user rulings 2026-11-03: dark is the
default, light the other choice).

Colours only, plus the radius scale and the font stack. Sizes are NOT taken
from DeepSeek: Block01's tool rows and channel rows keep their own heights
(plan §8), so the radii here are the ones that fit them.
"""

DARK = {
    "name": "dark",
    "canvas": "#151517",          # window background
    "layer1": "#232324",          # inputs, cards
    "layer2": "#2C2C2E",          # raised surfaces, button fill
    "layer3": "#353638",
    "bar": "#1B1B1C",             # the step bar and the load row
    "active": "#43454A",          # the selected item's fill
    "border": "rgba(255,255,255,0.12)",
    "border_strong": "rgba(255,255,255,0.20)",
    "text": "#F9FAFB",
    "text2": "#CFD3D6",
    "text3": "#ADB2B8",
    "caption": "#81858C",
    "primary": "#F9FAFB",         # primary button: inverted
    "primary_hover": "#EBEEF2",
    "on_primary": "#0F1115",
    "accent": "#7AAAFF",          # links, focus, the current step's mark
    "hover": "rgba(255,255,255,0.08)",
    "pressed": "rgba(255,255,255,0.14)",
    "danger": "#F25A5A",
    "success": "#22C55E",
    "warning": "#F59E0B",
    "tooltip_bg": "#43454A",
    "tooltip_text": "#F9FAFB",
    "scroll_thumb": "rgba(255,255,255,0.20)",
    "disabled_text": "#5E6166",
}

LIGHT = {
    "name": "light",
    "canvas": "#FFFFFF",
    "layer1": "#FFFFFF",
    "layer2": "#F9FAFB",
    "layer3": "#F1F3F5",
    "bar": "#F9FAFB",
    "active": "#EBEEF2",
    "border": "rgba(0,0,0,0.10)",
    "border_strong": "rgba(0,0,0,0.16)",
    "text": "#0F1115",
    "text2": "#61666B",
    "text3": "#81858C",
    "caption": "#ADB2B8",
    "primary": "#0F1115",
    "primary_hover": "#43454A",
    "on_primary": "#FFFFFF",
    "accent": "#4176E6",
    "hover": "rgba(38,49,72,0.06)",
    "pressed": "rgba(38,49,72,0.10)",
    "danger": "#EC1313",
    "success": "#22C55E",
    "warning": "#F59E0B",
    "tooltip_bg": "#2C2C2E",
    "tooltip_text": "#FFFFFF",
    "scroll_thumb": "rgba(0,0,0,0.20)",
    "disabled_text": "#ADB2B8",
}

THEMES = {"dark": DARK, "light": LIGHT}
DEFAULT = "dark"

#: radii that fit Block01's 22-28 px controls (DeepSeek: 8 / 12 at 28 / 36)
RADIUS_SM = 6
RADIUS_MD = 8

FONT_STACK = ("'Segoe UI', 'PingFang SC', 'Microsoft YaHei', 'Noto Sans CJK SC', "
              "'Ubuntu', 'Helvetica Neue', Arial, sans-serif")
