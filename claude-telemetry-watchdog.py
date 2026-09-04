#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""claude-telemetry-watchdog.py — внешний сторож трея телеметрии.

Трей иногда заклинивает: его индекс сессий пустеет (после сна/обновления Claude
или просто спустя время), владелец сессий не определяется, и телеметрия идёт с
чужой подписью или не фильтруется. Внутренний авто-перезапуск ненадёжен —
он живёт в том же деградировавшем процессе. Этот сторож — отдельный короткий
процесс, запускается планировщиком раз в несколько минут и не разделяет
состояние трея.

Логика:
  1. Смотрит хвост лога трея. Если за последние ~7 минут есть предупреждение
     «индекс сессий пуст» и нет более свежей здоровой строки «индекс сессий: N>0»
     — трей, похоже, заклинило.
  2. Подтверждает пробой (--count-sessions в свежем процессе), что сессии на
     диске правда есть (иначе перезапускать смысла нет — у Claude просто нет
     данных).
  3. Если заклинило и сессии есть — убивает процесс трея и запускает заново.

Ничего не делает, если трей здоров или не запущен вовсе (последнее — не наше
дело; трей поднимает автозапуск).
"""

import os
import re
import sys
import time
import subprocess
from datetime import datetime, timedelta

TRAY = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                    "claude-telemetry-tray.py")
PYW = sys.executable  # тот же интерпретатор, которым запущен сторож


def data_dir():
    root = os.environ.get("APPDATA", os.path.expanduser("~"))
    return os.path.join(root, "claude-telemetry")


def wlog(text):
    try:
        d = data_dir(); os.makedirs(d, exist_ok=True)
        p = os.path.join(d, "watchdog.log")
        try:
            if os.path.getsize(p) > 2 * 1024 * 1024:
                os.replace(p, p + ".1")
        except OSError:
            pass
        with open(p, "a", encoding="utf-8") as f:
            f.write("%s  %s\n" % (datetime.now().strftime("%Y-%m-%d %H:%M:%S"), text))
    except Exception:
        pass


def looks_wedged(window_min=7):
    """True, если в последние window_min минут есть пустой индекс и нет более
    свежей здоровой строки индекса."""
    log = os.path.join(data_dir(), "claude-telemetry.log")
    try:
        with open(log, "r", encoding="utf-8", errors="replace") as f:
            lines = f.readlines()[-400:]
    except OSError:
        return False
    cutoff = datetime.now() - timedelta(minutes=window_min)
    last_empty = last_healthy = None
    for ln in lines:
        m = re.match(r"^(\d{4}-\d\d-\d\d \d\d:\d\d:\d\d)\s", ln)
        if not m:
            continue
        try:
            ts = datetime.strptime(m.group(1), "%Y-%m-%d %H:%M:%S")
        except ValueError:
            continue
        if ts < cutoff:
            continue
        if "индекс сессий пуст" in ln:
            last_empty = ts
        elif re.search(r"индекс сессий: [1-9]", ln):
            last_healthy = ts
    if not last_empty:
        return False
    # заклинило, только если после последнего пустого не было здорового
    return last_healthy is None or last_healthy < last_empty


def sessions_exist():
    try:
        out = subprocess.run(
            [PYW, TRAY, "--count-sessions"],
            capture_output=True, text=True, timeout=60,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0)).stdout
        return int((out or "0").strip() or "0") > 0
    except Exception:
        return False


def tray_pids():
    """PID процессов, где в командной строке наш трей."""
    try:
        out = subprocess.run(
            ["powershell", "-NoProfile", "-Command",
             "Get-CimInstance Win32_Process | "
             "Where-Object { $_.CommandLine -like '*claude-telemetry-tray.py*' } | "
             "ForEach-Object { $_.ProcessId }"],
            capture_output=True, text=True, timeout=30,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0)).stdout
        return [int(x) for x in (out or "").split() if x.strip().isdigit()]
    except Exception:
        return []


def restart_tray():
    pids = tray_pids()
    for pid in pids:
        try:
            subprocess.run(["taskkill", "/F", "/PID", str(pid)],
                           capture_output=True, timeout=15,
                           creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        except Exception:
            pass
    time.sleep(2)
    try:
        flags = (getattr(subprocess, "DETACHED_PROCESS", 0)
                 | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0))
        subprocess.Popen([PYW, TRAY], creationflags=flags, close_fds=True)
        wlog("перезапустил трей (убито pid: %s)" % (pids or "нет"))
    except Exception as e:
        wlog("не смог запустить трей: %s" % e.__class__.__name__)


def main():
    if not looks_wedged():
        return
    if not sessions_exist():
        wlog("индекс пуст, но и свежая проба сессий не видит — не перезапускаю")
        return
    wlog("трей заклинило (индекс пуст, а сессии на диске есть) — перезапускаю")
    restart_tray()


if __name__ == "__main__":
    try:
        main()
    except Exception:
        pass
