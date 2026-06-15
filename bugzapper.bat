@echo off
setlocal enabledelayedexpansion
rem BugZapper (Windows) — finds a Python 3 with tkinter and runs flashui.py.
rem Usage: bugzapper.bat [firmware-dir]
rem Env: BUGZAPPER_TITLE, BUGZAPPER_ICON, BUGZAPPER_FW_DIR (see flashui.py).
rem
rem esptool, nodemcu-uploader and pyserial are bundled in vendor\ (pure python),
rem so nothing needs installing. Python.org's installer ships tkinter; if you
rem used a minimal install, re-run it and tick "tcl/tk and IDLE".

set "DIR=%~dp0"

rem Prefer the Windows "py" launcher, then python / python3 on PATH. Pick the
rem first that can actually import tkinter (test by running, not by presence).
for %%P in ("py -3" "py" "python" "python3") do (
  %%~P -c "import tkinter" >nul 2>&1
  if !errorlevel! equ 0 (
    %%~P "%DIR%flashui.py" %*
    exit /b !errorlevel!
  )
)

echo Error: no Python 3 with tkinter found.>&2
echo Install Python 3 from https://www.python.org/ (tick "tcl/tk and IDLE").>&2
exit /b 1
