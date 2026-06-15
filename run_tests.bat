@echo off
setlocal enabledelayedexpansion
rem Run the BugZapper test suite on Windows (stdlib unittest, no install).
set "DIR=%~dp0"

for %%P in ("py -3" "py" "python" "python3") do (
  %%~P -c "import sys" >nul 2>&1
  if !errorlevel! equ 0 (
    cd /d "%DIR%"
    %%~P -m unittest discover -s tests -p "test_*.py" -v %*
    exit /b !errorlevel!
  )
)
echo Error: no Python 3 found on PATH.>&2
exit /b 1
