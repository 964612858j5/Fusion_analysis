"""The program's name and icon: FusionFlux, not "python".

The logo lives in ui/icons/: the SVG is the source; the .ico (Windows
shortcut and window icon), .icns (macOS app bundle) and 256 px PNG are
rendered from it.

* Windows: the taskbar groups a process under python.exe's name and icon
  unless the process has its own AppUserModelID, which must be set before the
  first window shows (`before_application`).
* macOS: the Dock names a bare executable after its file name; the launcher
  (scripts/block01) starts Python through a link called FusionFlux. The Dock
  icon is the application's window icon.
* Linux: the dock matches a window to the menu entry `scripts/block01
  --desktop` writes (fusionflux.desktop, with the logo): by the desktop file
  name on Wayland, by the window class (the application name) on X11.
"""

import sys
from pathlib import Path

from ..qt import QtGui

APP_NAME = "FusionFlux"
APP_USER_MODEL_ID = "FusionFlux.Block01"
DESKTOP_FILE = "fusionflux"            # scripts/block01 --desktop writes fusionflux.desktop
ICON_DIR = Path(__file__).resolve().parent / "icons"


def before_application():
    """Windows: give the process its own taskbar identity (no-op elsewhere)."""
    if sys.platform != "win32":
        return
    try:
        import ctypes
        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(APP_USER_MODEL_ID)
    except Exception as exc:                 # an old shell: the python icon stays
        print(f"[Block01] taskbar identity not set: {exc}")


def app_icon():
    """The logo as a QIcon; the PNG when the vector form cannot be read."""
    first = "fusionflux.ico" if sys.platform == "win32" else "fusionflux.svg"
    for name in (first, "fusionflux_256.png"):
        icon = QtGui.QIcon(str(ICON_DIR / name))
        if not icon.pixmap(32, 32).isNull():
            return icon
    return QtGui.QIcon()


def apply(app):
    """Name the application and give every window the logo."""
    app.setApplicationName(APP_NAME)
    app.setWindowIcon(app_icon())
    if sys.platform.startswith("linux"):
        app.setDesktopFileName(DESKTOP_FILE)
