#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""claude-agent-monitor.py — наблюдатель за действиями Claude Code.

Ставится хуком PreToolUse: Claude Code запускает его перед каждым вызовом
инструмента и передаёт на stdin JSON с именем инструмента и аргументами.
Наблюдатель НИЧЕГО не блокирует (всегда exit 0) — только:
  * пишет находку в security-monitor.log рядом с логами трея;
  * при подозрительном действии шлёт тревогу в трей на /monitor, чтобы всплыло
    уведомление.

Что считается подозрительным:
  * обращение инструментов к чувствительным путям (.ssh, .aws, .env, ключи,
    хранилища паролей браузера, /etc/shadow, реестровые кусты и т.п.);
  * команды с признаками эксфильтрации или обхода (curl/wget наружу, base64|sh,
    nc, /dev/tcp, git remote add, scp на внешний хост, -enc в PowerShell и др.).

Регистрация в ~/.claude/settings.json:
  "hooks": {
    "PreToolUse": [
      {"matcher": "*", "hooks": [
        {"type": "command",
         "command": "python C:\\\\...\\\\claude-agent-monitor.py"}]}
    ]
  }
"""

import os
import re
import sys
import json
import platform
from datetime import datetime

SYS = platform.system()


def log_dir():
    if os.environ.get("CT_CONFIG"):
        return os.path.dirname(os.environ["CT_CONFIG"])
    if SYS == "Windows":
        root = os.environ.get("APPDATA", os.path.expanduser("~"))
    elif SYS == "Darwin":
        root = os.path.join(os.path.expanduser("~"), "Library", "Application Support")
    else:
        root = os.environ.get("XDG_CONFIG_HOME",
                              os.path.join(os.path.expanduser("~"), ".config"))
    return os.path.join(root, "claude-telemetry")


def proxy_port():
    # тот же порт, что и у прокси телеметрии; можно переопределить переменной
    return int(os.environ.get("CT_PROXY_PORT", "4318"))


def log_line(text):
    try:
        d = log_dir()
        os.makedirs(d, exist_ok=True)
        p = os.path.join(d, "security-monitor.log")
        try:
            if os.path.getsize(p) > 5 * 1024 * 1024:
                os.replace(p, p + ".1")
        except OSError:
            pass
        with open(p, "a", encoding="utf-8") as f:
            f.write("%s  %s\n" % (datetime.now().strftime("%Y-%m-%d %H:%M:%S"), text))
    except Exception:
        pass


def notify_tray(level, message):
    """Шлёт тревогу трею, чтобы уведомление шло через один механизм."""
    try:
        import urllib.request
        body = json.dumps({"level": level, "message": message,
                           "source": "agent-monitor"}).encode("utf-8")
        req = urllib.request.Request(
            "http://127.0.0.1:%d/monitor" % proxy_port(),
            data=body, method="POST",
            headers={"Content-Type": "application/json"})
        urllib.request.urlopen(req, timeout=3).read()
    except Exception:
        pass  # трея нет — находка всё равно в логе


# ── Правила ──────────────────────────────────────────────────────────────────
# Чувствительные пути: обращение к ним любым файловым инструментом — тревога.
# Только настоящий секретный материал. Шаблоны (.env.example), публичные ключи
# (.pub), ssh config и known_hosts — не секреты, их чтение рутинно и не флагается.
SENSITIVE_PATH = re.compile(r"""(?ix)
      (id_rsa|id_ed25519|id_ecdsa|id_dsa)(?!\.pub)   # приватные ключи, но не .pub
    | \.pem\b | \.p12\b | \.pfx\b | \.key\b
    | \.aws(/|\\)               | \.gnupg(/|\\)
    | \.config[/\\]gcloud       | \.kube[/\\]config
    | \.netrc | \.pgpass
    | (^|[/\\])\.env(?!\.(example|sample|template|dist|ci)\b)(\.[\w]+)?(?=$|['"\s])  # .env, но не шаблоны
    | credentials(\.json)?\b    | secrets?\.(json|ya?ml|txt)\b
    | /etc/shadow | /etc/sudoers
    | NTUSER\.DAT | \\SAM$ | \\SECURITY$   # кусты реестра Windows
    | Login\ Data | Cookies$ | Web\ Data    # хранилища браузера
    | wallet\.dat | \.kdbx\b                 # кошельки/пароли
""")

# Подозрительные команды в Bash/PowerShell.
SUSP_CMD = [
    ("выкачивание и запуск",   re.compile(r"(?is)(curl|wget|Invoke-WebRequest|iwr)\b.*\|\s*(sh|bash|python|iex|Invoke-Expression)")),
    ("base64 → интерпретатор", re.compile(r"(?is)base64\s+(-d|--decode|-D).*\|\s*(sh|bash|python)")),
    ("обратный шелл /dev/tcp", re.compile(r"/dev/tcp/")),
    ("netcat",                 re.compile(r"(?i)\b(nc|ncat|netcat)\b\s+-")),
    ("PowerShell -EncodedCommand", re.compile(r"(?i)-e(nc|ncodedcommand)?\s+[A-Za-z0-9+/=]{20,}")),
    ("certutil/bitsadmin загрузка", re.compile(r"(?i)\b(certutil|bitsadmin)\b.*(urlcache|http)")),
    ("чтение приватного ключа", re.compile(r"(?i)(cat|type|Get-Content)\b[^|;&\n]*((id_rsa|id_ed25519|id_ecdsa)(?!\.pub)|\.pem\b|\.p12\b|\.key\b)")),
    ("git remote add",         re.compile(r"(?i)git\s+remote\s+add\b")),
    ("scp/rsync наружу",       re.compile(r"(?i)\b(scp|rsync)\b.*@[\w.-]+:")),
    ("выгрузка окружения",     re.compile(r"(?i)(printenv|(^|\s)env\s*$|Get-ChildItem\s+Env:).*\|\s*(curl|nc|wget)")),
]

# Внешний сетевой адрес в команде (не localhost) — отдельный сигнал.
EXTERNAL_URL = re.compile(r"(?i)https?://(?!(127\.0\.0\.1|localhost|0\.0\.0\.0|::1)\b)[\w.-]+")


# Инструменты поиска: их аргумент — это шаблон поиска, а не доступ к файлу.
# grep "password" ничего не читает из секрета, флагать такое — только шум.
SEARCH_TOOLS = re.compile(r"(?i)^\s*(sudo\s+)?(grep|rg|ripgrep|ack|ag|findstr|"
                          r"Select-String|git\s+grep)\b")

# Работа над самим наблюдателем/треем — не подозрительная активность.
SELF_REPO = re.compile(r"(?i)(claude-agent-monitor|claude-telemetry-tray|"
                       r"[/\\]claude-telemetry([/\\]|\b))")


def strip_noise(cmd):
    """Убирает из команды куски, где «опасные» слова — это текст, а не действие:
    тела heredoc и сообщения коммита (git commit -m/-F). Иначе описание паттернов
    в commit-сообщении или в heredoc ловится как атака."""
    # heredoc: <<'EOF' ... EOF  и  << EOF ... EOF
    def drop_heredoc(m):
        return "<<%s %s" % (m.group(1), m.group(2))  # оставляем только маркер
    cmd = re.sub(r"<<-?\s*'?([A-Za-z_]\w*)'?(.*?)\n\1",
                 lambda m: "<<" + m.group(1), cmd, flags=re.S)
    # -m "..."  и  -m '...'
    cmd = re.sub(r"-m\s+\"(?:[^\"\\]|\\.)*\"", "-m <msg>", cmd)
    cmd = re.sub(r"-m\s+'(?:[^'\\]|\\.)*'", "-m <msg>", cmd)
    return cmd


def classify(tool, tin):
    """Возвращает (level, [сообщения])."""
    findings = []
    # аргументы файловых инструментов
    path = ""
    keys = ["file_path", "path", "notebook_path"]
    # у Glob pattern — это путь-glob (можно искать ключи: **/.ssh/*), а у Grep
    # pattern — регэксп по содержимому, путём его считать нельзя
    if tool == "Glob":
        keys.append("pattern")
    for k in keys:
        v = tin.get(k)
        if isinstance(v, str):
            path += " " + v
    # чтение/правка самого наблюдателя или трея — не тревога
    if path and SENSITIVE_PATH.search(path) and not SELF_REPO.search(path):
        findings.append(("alert", "%s → чувствительный путь: %s"
                         % (tool, path.strip()[:150])))

    # команды
    cmd = tin.get("command")
    if isinstance(cmd, str) and cmd:
        # поиск по паттерну и работа над этим репо — не действие с секретом
        if not SEARCH_TOOLS.search(cmd) and not SELF_REPO.search(cmd):
            scan = strip_noise(cmd)   # без тел heredoc и текста commit-сообщений
            for name, rx in SUSP_CMD:
                if rx.search(scan):
                    findings.append(("alert", "%s: %s — %s"
                                     % (tool, name, cmd.strip()[:150])))
            if SENSITIVE_PATH.search(scan):
                findings.append(("alert", "%s читает/трогает секретный путь: %s"
                                 % (tool, cmd.strip()[:150])))
            m = EXTERNAL_URL.search(scan)
            if m:
                findings.append(("warn", "%s обращается наружу: %s"
                                 % (tool, cmd.strip()[:150])))
    return findings


def main():
    try:
        payload = json.load(sys.stdin)
    except Exception:
        return  # не наше дело падать — просто выходим
    tool = payload.get("tool_name") or payload.get("tool") or "?"
    tin = payload.get("tool_input") or payload.get("input") or {}
    if not isinstance(tin, dict):
        tin = {}

    # «пульс»: отметка последнего вызова хука. Позволяет проверить, что хук
    # вообще срабатывает (в десктоп-агенте это не самоочевидно), не засоряя лог.
    # Пишем cwd и сессию — так видно, какой именно инстанс Claude сработал.
    try:
        cwd = payload.get("cwd") or "?"
        sess = (payload.get("session_id") or "?")[:8]
        d = log_dir(); os.makedirs(d, exist_ok=True)
        hb = os.path.join(d, "agent-monitor-lastfired.txt")
        line = "%s  %-6s sess=%s  cwd=%s\n" % (
            datetime.now().strftime("%Y-%m-%d %H:%M:%S"), tool, sess, cwd)
        old = ""
        try:
            with open(hb, encoding="utf-8") as f:
                old = "".join(f.readlines()[-49:])   # держим последние ~50 строк
        except OSError:
            pass
        with open(hb, "w", encoding="utf-8") as f:
            f.write(old + line)
    except Exception:
        pass

    findings = classify(tool, tin)
    if not findings:
        return
    for level, msg in findings:
        log_line("[%s] %s" % (level.upper(), msg))
    # самое серьёзное — в уведомление
    top = "alert" if any(l == "alert" for l, _ in findings) else "warn"
    notify_tray(top, findings[0][1])


if __name__ == "__main__":
    # Хук не должен ломать работу Claude ни при каких обстоятельствах.
    try:
        main()
    except Exception:
        pass
    sys.exit(0)
