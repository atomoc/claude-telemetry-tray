#!/usr/bin/env python3
"""Собирает ОДИН самодостаточный файл-инсталлятор для macOS:
dist/claude-telemetry-macos.command — в него целиком вшит claude-telemetry-tray.py.

Пользователю достаточно скачать этот файл из GitHub Releases и запустить
(в первый раз: правый клик → «Открыть», т.к. файл из интернета на карантине).
Инсталлятор сам:
  • распакует скрипт в ~/Library/Application Support/claude-telemetry/,
  • создаст venv и поставит зависимости из готовых wheel,
  • запустит значок в menu bar и закроет окно Terminal,
  • приложение само включит автозапуск при входе в систему.
"""
import os

HERE = os.path.dirname(os.path.abspath(__file__))
SRC = os.path.join(HERE, "claude-telemetry-tray.py")
MON = os.path.join(HERE, "claude-agent-monitor.py")
OUT = os.path.join(HERE, "dist", "claude-telemetry-macos.command")
DELIM = "__CTT_PAYLOAD_EOF__"
MON_DELIM = "__CTT_MONITOR_EOF__"

payload = open(SRC, "r", encoding="utf-8").read()
monitor = open(MON, "r", encoding="utf-8").read()
if DELIM in payload or MON_DELIM in payload or MON_DELIM in monitor:
    raise SystemExit("delimiter collision in payload")

HEAD = r'''#!/bin/bash
# Claude Telemetry Tray — единый установщик для macOS.
# Двойной клик (в первый раз: правый клик → «Открыть»).
set -e

APP="$HOME/Library/Application Support/claude-telemetry"
VENV="$APP/venv"
PY="$VENV/bin/python3"
SCRIPT="$APP/claude-telemetry-tray.py"
MONITOR="$APP/claude-agent-monitor.py"
mkdir -p "$APP"

echo "Распаковываю…"
cat > "$SCRIPT" <<'__CTT_PAYLOAD_EOF__'
'''

MID = r'''__CTT_PAYLOAD_EOF__

cat > "$MONITOR" <<'__CTT_MONITOR_EOF__'
'''

TAIL = r'''__CTT_MONITOR_EOF__

if [ ! -x "$PY" ]; then
  echo "Готовлю окружение (это нужно один раз)…"
  python3 -m venv "$VENV"
fi

if ! "$PY" -c 'import pystray, PIL, AppKit, Foundation, certifi' >/dev/null 2>&1; then
  echo "Устанавливаю зависимости…"
  "$PY" -m pip install --upgrade pip >/dev/null 2>&1 || true
  "$PY" -m pip install --only-binary=:all: \
      pystray Pillow certifi pyobjc-framework-Cocoa pyobjc-framework-Quartz
fi

echo "Запускаю…"
LOG="/tmp/claude-telemetry-launch.log"
nohup "$PY" "$SCRIPT" >"$LOG" 2>&1 &
APP_PID=$!
disown 2>/dev/null || true

sleep 3
if kill -0 "$APP_PID" 2>/dev/null; then
  echo "Готово. Значок — в строке меню. Открой «Настройки…», впиши токен и URL коллектора."
  WIN_NAME="$(basename "$0")"
  osascript -e "tell application \"Terminal\" to close (every window whose name contains \"$WIN_NAME\") saving no" >/dev/null 2>&1 &
  exit 0
else
  echo
  echo "Не удалось запустить — окно оставлено открытым. Подробности:"
  cat "$LOG"
  exit 1
fi
'''

# каждый payload оканчиваем переводом строки, чтобы закрывающий разделитель
# heredoc стоял на своей строке
if not payload.endswith("\n"):
    payload += "\n"
if not monitor.endswith("\n"):
    monitor += "\n"

os.makedirs(os.path.dirname(OUT), exist_ok=True)
with open(OUT, "w", encoding="utf-8") as f:
    f.write(HEAD + payload + MID + monitor + TAIL)
os.chmod(OUT, 0o755)
print("built:", OUT, "(%d bytes)" % os.path.getsize(OUT))
