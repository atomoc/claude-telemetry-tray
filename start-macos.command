#!/bin/bash
# Двойной клик в Finder. Скрипт сам:
#   1) создаёт изолированное окружение (venv) — один раз,
#   2) ставит зависимости ТОЛЬКО из готовых wheel (компилятор не нужен),
#   3) запускает значок Claude Telemetry в menu bar в фоне,
#   4) при успешном запуске закрывает это окно Terminal.
# Работает даже на системном python3 из CommandLineTools (3.9, старый pip).
set -e
cd "$(dirname "$0")"

APP_SUPPORT="$HOME/Library/Application Support/claude-telemetry"
VENV="$APP_SUPPORT/venv"
PY="$VENV/bin/python3"

if [ ! -x "$PY" ]; then
  echo "Готовлю окружение (это нужно один раз)…"
  mkdir -p "$APP_SUPPORT"
  python3 -m venv "$VENV"
fi

if ! "$PY" -c 'import pystray, PIL, AppKit, Foundation, certifi' >/dev/null 2>&1; then
  echo "Устанавливаю зависимости…"
  "$PY" -m pip install --upgrade pip >/dev/null 2>&1 || true
  "$PY" -m pip install --only-binary=:all: \
      pystray Pillow certifi pyobjc-framework-Cocoa pyobjc-framework-Quartz
fi

echo "Запускаю…"
SCRIPT_PY="$(pwd)/claude-telemetry-tray.py"
LOG="/tmp/claude-telemetry-launch.log"
# Запускаем в фоне и отвязываем от терминала, чтобы приложение пережило
# закрытие окна.
nohup "$PY" "$SCRIPT_PY" "$@" >"$LOG" 2>&1 &
APP_PID=$!
disown 2>/dev/null || true

# Считаем запуск успешным, если процесс жив через несколько секунд.
sleep 3
if kill -0 "$APP_PID" 2>/dev/null; then
  echo "Запущено. Значок — в строке меню."
  WIN_NAME="$(basename "$0")"
  # Закрываем именно это окно Terminal (по имени), не трогая остальные.
  osascript -e "tell application \"Terminal\" to close (every window whose name contains \"$WIN_NAME\") saving no" >/dev/null 2>&1 &
  exit 0
else
  echo
  echo "Не удалось запустить — окно оставлено открытым. Подробности:"
  cat "$LOG"
  exit 1
fi
