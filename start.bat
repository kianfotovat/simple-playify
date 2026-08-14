@echo off
setlocal EnableExtensions DisableDelayedExpansion
cd /d "%~dp0" || exit /b 1
title Playify

set "PLAYIFY_PYTHON="
for %%V in (3.14 3.13 3.12) do (
    if not defined PLAYIFY_PYTHON (
        py -%%V -c "import sys" >nul 2>&1 && set "PLAYIFY_PYTHON=py -%%V"
    )
)
if not defined PLAYIFY_PYTHON (
    python -c "import sys; raise SystemExit(0 if sys.version_info[:2] in {(3,12),(3,13),(3,14)} else 1)" >nul 2>&1 && set "PLAYIFY_PYTHON=python"
)
if not defined PLAYIFY_PYTHON (
    echo Playify requires Python 3.12-3.14 x64.
    choice /M "Install Python 3.14 with winget"
    if errorlevel 2 exit /b 1
    winget install --id Python.Python.3.14 -e
    if errorlevel 1 exit /b 1
    set "PLAYIFY_PYTHON=py -3.14"
)

%PLAYIFY_PYTHON% bootstrap.py
set "PLAYIFY_EXIT=%ERRORLEVEL%"
if not "%PLAYIFY_EXIT%"=="0" pause
exit /b %PLAYIFY_EXIT%
