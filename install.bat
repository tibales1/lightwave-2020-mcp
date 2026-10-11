@echo off
setlocal
rem ---------------------------------------------------------------
rem  LightWave MCP - one-time setup
rem  1. creates a Python 3 virtual env in .venv
rem  2. installs the MCP SDK
rem  3. registers the server with the Claude desktop app
rem ---------------------------------------------------------------
cd /d "%~dp0"

set "PY="
if exist "%USERPROFILE%\miniconda3\python.exe" set "PY=%USERPROFILE%\miniconda3\python.exe"
if not defined PY (
  where py >nul 2>nul && set "PY=py -3"
)
if not defined PY (
  where python >nul 2>nul && set "PY=python"
)
if not defined PY (
  echo Could not find Python 3.10+. Install it from python.org and run this again.
  pause & exit /b 1
)

echo Using: %PY%
%PY% -c "import sys; sys.exit(0 if sys.version_info >= (3,10) else 1)" || (
  echo Python 3.10 or newer is required.
  pause & exit /b 1
)

if not exist ".venv\Scripts\python.exe" (
  %PY% -m venv .venv || (echo venv creation failed & pause & exit /b 1)
)
".venv\Scripts\python.exe" -m pip install --upgrade pip >nul
".venv\Scripts\python.exe" -m pip install -r requirements.txt || (echo pip install failed & pause & exit /b 1)

".venv\Scripts\python.exe" register_with_claude.py

echo.
echo Done. Next, in LightWave:
echo   Layout : Utilities ^> Add Plugins ^> %~dp0lightwave_bridge\lw_mcp_layout_bridge.py
echo            then Utilities ^> Master Plugins ^> add "Claude MCP Bridge"
echo   Modeler: Utilities ^> Add Plugins ^> %~dp0lightwave_bridge\lw_mcp_modeler_bridge.py
echo            then Edit ^> Edit Keyboard Shortcuts ^> "Claude MCP Modeler Bridge" ^> assign Shift-F12
echo Then fully quit (tray icon ^> Quit) and reopen the Claude desktop app.
pause
