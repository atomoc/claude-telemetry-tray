@echo off
setlocal
cd /d "%~dp0"
echo Removing "downloaded from Internet" mark from files...
powershell -NoProfile -Command "Get-ChildItem -LiteralPath '%~dp0.' -Recurse -File | Unblock-File" 2>nul
set "PY="
where py >nul 2>&1 && set "PY=py"
if not defined PY ( where python >nul 2>&1 && set "PY=python" )
if not defined PY ( echo [ERROR] Python not found. Install from python.org with "Add to PATH". & pause & exit /b 1 )
%PY% --version
%PY% -m pip install --user --upgrade pystray Pillow
if errorlevel 1 ( echo [ERROR] Dependency install failed. & pause & exit /b 1 )
%PY% "%~dp0claude-telemetry-tray.py"
echo.
echo Tray exited. If there is an error above, please report it.
pause
