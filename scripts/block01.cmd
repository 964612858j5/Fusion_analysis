@echo off
rem Start FusionFlux (Block01) on Windows: double-click this file, or the
rem desktop shortcut the installer made (docs/v17_windows_native_plan.md, W3).
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0block01.ps1" %*
