#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
build-installers.py — package Claude Telemetry Tray into self-extracting installers.

Produces, in ./dist:
  * claude-telemetry.zip                 (folder: claude-telemetry/<files>)
  * install-claude-telemetry.bat         (Windows; double-click)
  * install-claude-telemetry.command     (macOS; double-click)
  * install-claude-telemetry.sh          (Linux)

Each installer embeds the zip as base64 and unpacks it LOCALLY, so the extracted
files never get the OS "downloaded from the Internet" mark (Windows MOTW /
macOS com.apple.quarantine). The installer also installs deps and enables login
auto-start.

Usage:  python3 build-installers.py
"""

import base64
import os
import zipfile

HERE = os.path.dirname(os.path.abspath(__file__))
DIST = os.path.join(HERE, "dist")
FOLDER = "claude-telemetry"

# Files bundled into the installer payload (only those that exist are included).
PAYLOAD_FILES = [
    "claude-telemetry-tray.py",
    "start-tray-windows.bat",
    "start-tray-windows-silent.bat",
    "start-tray-windows.vbs",
    "unblock-windows.bat",
    "start-tray-mac-linux.sh",
    "README.txt",
]

WIN_HEADER = r'''@echo off
setlocal
cd /d "%~dp0"
echo Installing Claude Telemetry Tray...
powershell -NoProfile -ExecutionPolicy Bypass -Command "$s=Get-Content -LiteralPath '%~f0' -Raw; $i=$s.LastIndexOf('__CTB64__'); $b=($s.Substring($i+9)) -replace '\s',''; $bytes=[Convert]::FromBase64String($b); $zip=Join-Path $env:TEMP ('ct_'+[guid]::NewGuid().ToString()+'.zip'); [IO.File]::WriteAllBytes($zip,$bytes); Expand-Archive -LiteralPath $zip -DestinationPath '%~dp0' -Force; Remove-Item $zip -Force; Get-ChildItem -LiteralPath (Join-Path '%~dp0' 'claude-telemetry') -Recurse -File | Unblock-File"
if errorlevel 1 ( echo [ERROR] Unpacking failed. & pause & exit /b 1 )
set "PY="
where py >nul 2>&1 && set "PY=py"
if not defined PY ( where python >nul 2>&1 && set "PY=python" )
if not defined PY ( echo [ERROR] Python not found. Install from python.org with "Add to PATH". & pause & exit /b 1 )
set "APP=%~dp0claude-telemetry\claude-telemetry-tray.py"
%PY% -m pip install --user --upgrade pystray Pillow
if errorlevel 1 ( echo [ERROR] Dependency install failed. & pause & exit /b 1 )
%PY% "%APP%" --enable-autostart
set "TGT=%APP%"
powershell -NoProfile -Command "$ws=New-Object -ComObject WScript.Shell;$d=[Environment]::GetFolderPath('Desktop');$lnk=$ws.CreateShortcut((Join-Path $d 'Claude Telemetry.lnk'));$pw=(Get-Command pythonw -ErrorAction SilentlyContinue).Source;if(-not $pw){$pw=(Get-Command python -ErrorAction SilentlyContinue).Source};$lnk.TargetPath=$pw;$lnk.Arguments=([char]34+$env:TGT+[char]34);$lnk.WorkingDirectory=(Split-Path $env:TGT);$lnk.Save()" 2>nul
where pythonw >nul 2>&1 && ( start "" pythonw "%APP%" ) || ( start "" %PY% "%APP%" )
exit /b
__CTB64__
'''

NIX_HEADER = r'''#!/usr/bin/env bash
# Self-extracting installer for Claude Telemetry Tray (macOS / Linux).
set -e
DIR="$(cd "$(dirname "$0")" && pwd)"
echo "Installing Claude Telemetry Tray..."
command -v python3 >/dev/null 2>&1 || { echo "[ERROR] python3 not found. Install Python 3."; exit 1; }
python3 - "$0" "$DIR" <<'PY'
import sys, base64, zipfile, io
data=open(sys.argv[1],"rb").read(); i=data.rfind(b"__CTB64__")
b=b"".join(data[i+9:].split())
zipfile.ZipFile(io.BytesIO(base64.b64decode(b))).extractall(sys.argv[2])
print("Extracted to:", sys.argv[2]+"/claude-telemetry")
PY
chmod +x "$DIR/claude-telemetry/start-tray-mac-linux.sh" 2>/dev/null || true
command -v xattr >/dev/null 2>&1 && xattr -dr com.apple.quarantine "$DIR/claude-telemetry" 2>/dev/null || true
APP="$DIR/claude-telemetry/claude-telemetry-tray.py"
python3 -m pip install --user --upgrade pystray Pillow >/dev/null 2>&1 || \
  python3 -m pip install --user --break-system-packages --upgrade pystray Pillow >/dev/null 2>&1 || true
python3 "$APP" --enable-autostart || true
if [ "$(uname)" = "Darwin" ]; then
  echo "Done. Tray starting; it will auto-start at every login."
  ( sleep 1; /usr/bin/osascript -e "tell application \"Terminal\" to close (every window whose tty is \"$(tty)\")" >/dev/null 2>&1 ) &
else
  nohup python3 "$APP" >/dev/null 2>&1 &
  echo "Done. Tray started and will auto-start at every login."
fi
exit 0
__CTB64__
'''

QUICKSTART = """Claude Telemetry Tray
=====================
Windows : double-click start-tray-windows.bat (first run), then -silent.bat
macOS/Linux : ./start-tray-mac-linux.sh  (auto-starts at login after install)
Open "Settings..." in the tray to set token, collector URL and account filter.
Full docs: https://github.com/<you>/claude-telemetry-tray
"""


def build_zip():
    os.makedirs(DIST, exist_ok=True)
    # write a small quickstart if README.txt is not present in the repo
    if not os.path.exists(os.path.join(HERE, "README.txt")):
        with open(os.path.join(DIST, "_README.txt"), "w", encoding="utf-8") as f:
            f.write(QUICKSTART)
    zip_path = os.path.join(DIST, "claude-telemetry.zip")
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as z:
        for name in PAYLOAD_FILES:
            src = os.path.join(HERE, name)
            if name == "README.txt" and not os.path.exists(src):
                src = os.path.join(DIST, "_README.txt")
                if not os.path.exists(src):
                    continue
            if os.path.exists(src):
                z.write(src, arcname=FOLDER + "/" + name)
    return zip_path


def write_installer(path, header, b64):
    with open(path, "w", encoding="utf-8", newline="\n") as f:
        f.write(header)
        f.write(b64)
        f.write("\n")
    try:
        if not path.endswith(".bat"):
            os.chmod(path, 0o755)
    except OSError:
        pass


def main():
    zip_path = build_zip()
    b64 = base64.b64encode(open(zip_path, "rb").read()).decode("ascii")
    write_installer(os.path.join(DIST, "install-claude-telemetry.bat"), WIN_HEADER, b64)
    write_installer(os.path.join(DIST, "install-claude-telemetry.command"), NIX_HEADER, b64)
    write_installer(os.path.join(DIST, "install-claude-telemetry.sh"), NIX_HEADER, b64)
    print("Built in", DIST + ":")
    for f in ("claude-telemetry.zip", "install-claude-telemetry.bat",
              "install-claude-telemetry.command", "install-claude-telemetry.sh"):
        p = os.path.join(DIST, f)
        if os.path.exists(p):
            print("  %-34s %d bytes" % (f, os.path.getsize(p)))


if __name__ == "__main__":
    main()
