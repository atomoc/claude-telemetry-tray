@echo off
cd /d "%~dp0"
echo Removing the "downloaded from Internet" mark from all files in this folder...
powershell -NoProfile -Command "Get-ChildItem -LiteralPath '%~dp0.' -Recurse -File | Unblock-File"
echo Done. Now the .vbs and .py will run normally.
pause
