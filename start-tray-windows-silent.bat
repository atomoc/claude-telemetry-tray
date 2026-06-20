@echo off
cd /d "%~dp0"
powershell -NoProfile -Command "Get-ChildItem -LiteralPath '%~dp0.' -Recurse -File | Unblock-File" 2>nul
where pythonw >nul 2>&1 && ( start "" pythonw "%~dp0claude-telemetry-tray.py" & exit /b )
where py      >nul 2>&1 && ( start "" py -w "%~dp0claude-telemetry-tray.py" & exit /b )
where python  >nul 2>&1 && ( start "" python "%~dp0claude-telemetry-tray.py" & exit /b )
echo [ERROR] Python not found. Install from python.org and tick "Add python.exe to PATH".
pause
