"""The program shows as FusionFlux with its logo, not as python."""

import os
import subprocess
import sys
from pathlib import Path

import pytest

from block01.qt import QtWidgets
from block01.ui import app_identity

REPO = Path(__file__).resolve().parents[1]


@pytest.fixture
def app():
    return QtWidgets.QApplication.instance() or QtWidgets.QApplication([])


def test_the_logo_loads_as_the_application_icon(app):
    for name in ("fusionflux.svg", "fusionflux.ico", "fusionflux.icns", "fusionflux_256.png"):
        assert (app_identity.ICON_DIR / name).is_file(), name
    icon = app_identity.app_icon()
    assert not icon.pixmap(64, 64).isNull()
    app_identity.apply(app)
    assert app.applicationName() == "FusionFlux"
    assert not app.windowIcon().pixmap(32, 32).isNull()


def test_before_application_is_harmless_off_windows():
    app_identity.before_application()        # no-op here; on Windows sets the taskbar id


@pytest.mark.skipif(sys.platform == "win32", reason="bash launcher")
def test_the_launcher_starts_python_under_the_fusionflux_name(tmp_path):
    prefix = tmp_path / "env"
    (prefix / "bin").mkdir(parents=True)
    py = prefix / "bin" / "python"
    py.write_text("#!/bin/sh\n")
    py.chmod(0o755)
    out = subprocess.run(
        ["bash", "-c", f'. "{REPO / "scripts" / "block01"}"; STATE_DIR="{tmp_path / "state"}"; '
                       f'BLOCK01_PREFIX="{prefix}"; python_named_fusionflux'],
        env={**os.environ, "BLOCK01_LAUNCH_SOURCED": "1"},
        capture_output=True, text=True, check=True).stdout.strip()
    if sys.platform == "darwin":
        assert Path(out).name == "FusionFlux"
        assert os.path.realpath(out) == os.path.realpath(py)
    else:
        assert out == str(py)


@pytest.mark.skipif(sys.platform == "win32", reason="bash launcher")
def test_the_linux_menu_entry_is_fusionflux_with_the_logo(tmp_path):
    apps = tmp_path / "share" / "applications"
    apps.mkdir(parents=True)
    (apps / "block01.desktop").write_text("[Desktop Entry]\nName=Block01\n")   # an old install's
    subprocess.run(
        ["bash", "-c", f'. "{REPO / "scripts" / "block01"}"; write_desktop_entry'],
        env={**os.environ, "BLOCK01_LAUNCH_SOURCED": "1", "XDG_DATA_HOME": str(tmp_path / "share")},
        capture_output=True, text=True, check=True)
    assert not (apps / "block01.desktop").exists()
    entry = dict(line.split("=", 1) for line in
                 (apps / f"{app_identity.DESKTOP_FILE}.desktop").read_text().splitlines() if "=" in line)
    assert entry["Name"] == "FusionFlux"
    assert entry["StartupWMClass"] == "FusionFlux"
    assert Path(entry["Icon"]) == app_identity.ICON_DIR / "fusionflux_256.png"
    assert Path(entry["Icon"]).is_file()
    assert entry["Exec"].strip('"').endswith("scripts/block01")


def test_on_linux_the_app_names_its_menu_entry(app, monkeypatch):
    monkeypatch.setattr(sys, "platform", "linux")
    app_identity.apply(app)
    assert app.desktopFileName() == app_identity.DESKTOP_FILE
