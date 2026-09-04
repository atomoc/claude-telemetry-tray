#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
claude-telemetry-tray.py — управление телеметрией Claude Code из системного трея.
Кросс-платформенно: Windows / macOS / Linux.

Модель (v2.2):
  Телеметрия Claude Code ВСЕГДА идёт через локальный прокси на 127.0.0.1:<порт>.
  Прокси:
    1) (опц.) пишет реальное тело каждого запроса в лог-файл;
    2) фильтрует по аккаунту Claude: если в Настройках задан аккаунт, на сервер
       пересылается ТОЛЬКО телеметрия этого аккаунта (email/ID берётся из самого
       тела телеметрии); чужая телеметрия не отправляется;
    3) пересылает подходящее на твой настоящий адрес коллектора (base) и
       возвращает ответ Claude Code.
  Программа сама в сеть ничего лишнего не шлёт — только реальный трафик Claude.

  ВАЖНО: т.к. телеметрия всегда идёт через прокси, трей должен быть запущен,
  иначе телеметрия не доставится. Включи "Запуск вместе с системой".

Зависимости трея: pystray, Pillow (ставятся автоматически при первом запуске).
Окно настроек использует tkinter (на Linux: пакет python3-tk).
"""

import os
import re
import sys
import glob
import json
import hashlib
import time
import shlex
import shutil
import platform
import tempfile
import threading
import subprocess
import urllib.request
import urllib.error
import http.server
import traceback
from datetime import datetime

SCRIPT = os.path.abspath(__file__)
SYS = platform.system()
REFRESH_INTERVAL = 5          # раз в сколько секунд перепроверять состояние
# Прокси работает в своём потоке и будит перерисовку сразу, как только пришёл
# пакет: раньше значок ждал очередного тика таймера и отставал до пяти секунд.
STATE_CHANGED = threading.Event()
__version__ = "3.35"

TELEMETRY_KEYS = [
    "CLAUDE_CODE_ENABLE_TELEMETRY", "OTEL_LOG_USER_PROMPTS", "OTEL_METRICS_EXPORTER",
    "OTEL_LOGS_EXPORTER", "OTEL_EXPORTER_OTLP_PROTOCOL", "OTEL_EXPORTER_OTLP_ENDPOINT",
    "OTEL_EXPORTER_OTLP_METRICS_ENDPOINT", "OTEL_EXPORTER_OTLP_LOGS_ENDPOINT",
    "OTEL_EXPORTER_OTLP_HEADERS", "OTEL_METRIC_EXPORT_INTERVAL",
    "OTEL_LOGS_EXPORT_INTERVAL", "OTEL_RESOURCE_ATTRIBUTES",
]

# Атрибуты в телеметрии, по которым опознаётся аккаунт Claude.
ACCOUNT_ATTR_KEYS = ["user.email", "user.account_uuid", "user.id", "organization.id"]

DEFAULT_CONFIG = {
    "token": "",
    "base": "",
    "teamId": "",
    "account": "",          # фильтр: отправлять только этот аккаунт (пусто = слать всё)
    "fixAccount": True,     # чинить чужую подпись аккаунта по владельцу сессии
    "accountEmails": {},    # account_uuid → email, накапливается из самой телеметрии
    "logsExportInterval": 1000,   # мс; как часто Claude отдаёт накопленные логи
    "logUserPrompts": True,
    "logTelemetry": True,   # писать тело отправляемой телеметрии в лог-файл
    "proxyPort": 4318,
    "autostartDefaulted": False,
}

PROXY_STATE = {
    "ok": None, "last": None, "detail": "трафика ещё не было", "bound": False,
    "sent": 0, "filtered": 0, "errors": 0,
    "last_ok": 0.0, "last_any": 0.0,
    "seq": 0, "ok_seq": 0, "err_seq": 0,
    "act": {"kind": None, "time": 0.0},   # последнее действие пользователя
    "accounts": {},          # email → {ok, filtered, last, at}
    "blind_since": 0.0,      # с какого момента режем работу с неопознанным владельцем
    "blind_warned": False,   # чтобы не спамить оповещением
    "started_at": 0.0,      # монотонное время старта — для защиты от циклов перезапуска
    "empty_since": 0.0,     # с какого момента индекс сессий пуст (для авто-перезапуска)
    "leaks": 0,              # сколько раз секрет замечен в исходящей телеметрии
    "leak_seen": set(),      # отпечатки уже замеченных секретов (без дедупа спамило бы)
    "leak_alerts": [],       # очередь уведомлений для monitor-потока
}
STALE_AFTER = 300            # сек без успешной доставки → «трафика нет»


def note_traffic(kind, email=None, detail=None, activity=True, blind=False):
    """Учёт пакета: kind ∈ {ok, filtered, error}.

    Фильтрация поднимает синий и держит его: успешная доставка следующего
    фонового экспорта синий не сбрасывает — иначе значок мигал бы от пингов
    коллектора и застать «сейчас что-то режется» было бы невозможно.
    Ошибка доставки перебивает всё и красит в красный."""
    st = PROXY_STATE
    now = time.monotonic()
    # Порядок событий считаем счётчиком, а не временем: на Windows таймер
    # грубый, и ошибка сразу после успеха попадала с ним в один и тот же тик.
    st["seq"] += 1
    st["last_any"] = now

    if kind == "error":
        st.update(ok=False, last="error", errors=st["errors"] + 1,
                  err_seq=st["seq"], detail=detail or st["detail"])
        STATE_CHANGED.set()
        return
    if kind == "ok":
        # успешная доставка снимает красный и держит «трафик есть» даже когда
        # это фоновый пинг, но в счётчики и в цвет пинг не попадает
        st.update(ok=True, last="ok", last_ok=now, ok_seq=st["seq"])
        STATE_CHANGED.set()      # успешный пинг снимает красный — это видно сразу
    if not activity:
        return

    if kind == "ok":
        st["sent"] += 1
    else:
        st.update(filtered=st["filtered"] + 1, last="skipped")
        if blind:
            # рабочий пакет отрезан, а владелец не определён — это ровно тот
            # случай, когда своя же телеметрия молча теряется
            if not st["blind_since"]:
                st["blind_since"] = now
        else:
            st["blind_since"] = 0.0
            st["blind_warned"] = False
    if kind == "ok":
        # доставка идёт — значит вслепую уже не режем
        st["blind_since"] = 0.0
        st["blind_warned"] = False
    st["act"] = {"kind": kind, "time": now}
    if detail:
        st["detail"] = detail
    if email:
        acc = st["accounts"].setdefault(email, {"ok": 0, "filtered": 0,
                                                "last": None, "at": 0.0})
        acc[kind] += 1
        acc["last"], acc["at"] = kind, now
    STATE_CHANGED.set()


def accounts_title(state, accounts, titles):
    """Подсказка к значку: расшифровка кружков в том же порядке, что и они."""
    head = titles.get(state, "Claude Telemetry")
    if not accounts or len(accounts) < 2 or state not in ("on", "skipped"):
        return "%s — %s" % (head, traffic_summary())
    marks = {"on": "доставляется", "skipped": "фильтруется", "idle": "молчит"}
    return "Claude Telemetry: " + "; ".join(
        "%s — %s" % (email, marks.get(st, st)) for email, st in accounts)


def traffic_summary():
    st = PROXY_STATE
    parts = ["доставлено %d" % st["sent"]]
    if st["filtered"]:
        parts.append("отфильтровано %d" % st["filtered"])
    if st["errors"]:
        parts.append("ошибок %d" % st["errors"])
    return ", ".join(parts)


# ── Пути ─────────────────────────────────────────────────────────────────────
def managed_file():
    if os.environ.get("CT_MANAGED"):
        return os.environ["CT_MANAGED"]
    if SYS == "Windows":
        base = os.environ.get("ProgramFiles", r"C:\Program Files")
        return os.path.join(base, "ClaudeCode", "managed-settings.json")
    if SYS == "Darwin":
        return "/Library/Application Support/ClaudeCode/managed-settings.json"
    return "/etc/claude-code/managed-settings.json"


def user_file():
    if os.environ.get("CT_USER"):
        return os.environ["CT_USER"]
    return os.path.join(os.path.expanduser("~"), ".claude", "settings.json")


# ── Наблюдатель за действиями агента (хук Claude Code) ────────────────────────
def monitor_script():
    """Путь к claude-agent-monitor.py рядом с треем (кладётся установщиком)."""
    return os.path.join(os.path.dirname(SCRIPT), "claude-agent-monitor.py")


def monitor_hook_cmd():
    py = sys.executable or "python"
    # трей запущен под pythonw.exe (без консоли); для хука берём обычный python
    if py.lower().endswith("pythonw.exe"):
        cand = py[:-len("pythonw.exe")] + "python.exe"
        if os.path.isfile(cand):
            py = cand
    return '"%s" "%s"' % (py, monitor_script())


def monitor_enabled():
    """True, если наш хук уже прописан в settings.json."""
    s = read_json(user_file()) or {}
    for e in (s.get("hooks", {}) or {}).get("PreToolUse", []) or []:
        for h in e.get("hooks", []) or []:
            if "claude-agent-monitor" in (h.get("command") or ""):
                return True
    return False


def set_monitor(on):
    """Регистрирует или убирает хук-наблюдатель, не трогая остальной settings.json."""
    p = user_file()
    s = read_json(p) or {}
    hooks = s.setdefault("hooks", {})
    pre = hooks.setdefault("PreToolUse", [])
    # выкидываем прежнюю запись наблюдателя (путь мог измениться)
    pre = [e for e in pre
           if not any("claude-agent-monitor" in (h.get("command") or "")
                      for h in e.get("hooks", []) or [])]
    if on:
        pre.append({"matcher": "*", "hooks": [
            {"type": "command", "command": monitor_hook_cmd()}]})
    hooks["PreToolUse"] = pre
    if not pre:
        hooks.pop("PreToolUse", None)
    if not hooks:
        s.pop("hooks", None)
    os.makedirs(os.path.dirname(p), exist_ok=True)
    with open(p, "w", encoding="utf-8") as f:
        json.dump(s, f, ensure_ascii=False, indent=2)
        f.write("\n")


def config_path():
    if os.environ.get("CT_CONFIG"):
        return os.environ["CT_CONFIG"]
    if SYS == "Windows":
        root = os.environ.get("APPDATA", os.path.expanduser("~"))
    elif SYS == "Darwin":
        root = os.path.join(os.path.expanduser("~"), "Library", "Application Support")
    else:
        root = os.environ.get("XDG_CONFIG_HOME", os.path.join(os.path.expanduser("~"), ".config"))
    return os.path.join(root, "claude-telemetry", "config.json")


# ── Конфиг ───────────────────────────────────────────────────────────────────
def load_config():
    cfg = dict(DEFAULT_CONFIG)
    try:
        with open(config_path(), "r", encoding="utf-8") as f:
            cfg.update(json.load(f))
    except Exception:
        pass
    return cfg


def save_config(cfg):
    p = config_path()
    os.makedirs(os.path.dirname(p), exist_ok=True)
    with open(p, "w", encoding="utf-8") as f:
        json.dump(cfg, f, indent=2, ensure_ascii=False)
        f.write("\n")


# ── Настройки Claude Code ────────────────────────────────────────────────────
def read_json(file):
    try:
        with open(file, "r", encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        return None
    except Exception:
        return None


def endpoints(cfg):
    """Телеметрия ВСЕГДА идёт через локальный прокси."""
    host = "http://127.0.0.1:%s" % cfg.get("proxyPort", 4318)
    return host + "/metrics", host + "/logs"


def build_env(cfg):
    metrics_ep, logs_ep = endpoints(cfg)
    env = {
        "CLAUDE_CODE_ENABLE_TELEMETRY": "1",
        "OTEL_METRICS_EXPORTER": "otlp",
        "OTEL_LOGS_EXPORTER": "otlp",
        "OTEL_EXPORTER_OTLP_PROTOCOL": "http/json",
        "OTEL_EXPORTER_OTLP_METRICS_ENDPOINT": metrics_ep,
        "OTEL_EXPORTER_OTLP_LOGS_ENDPOINT": logs_ep,
        "OTEL_EXPORTER_OTLP_HEADERS": "Authorization=Bearer " + cfg["token"],
        "OTEL_METRIC_EXPORT_INTERVAL": "60000",
        # Чем реже Claude отдаёт логи, тем позже трей узнаёт о сообщении и тем
        # позже перекрашивается значок. При простое экспорт не происходит вовсе,
        # так что частый интервал ничего не стоит.
        "OTEL_LOGS_EXPORT_INTERVAL": str(int(cfg.get("logsExportInterval", 1000))),
        "OTEL_RESOURCE_ATTRIBUTES": "team.id=" + cfg["teamId"],
    }
    if cfg.get("logUserPrompts"):
        env["OTEL_LOG_USER_PROMPTS"] = "1"
    return env


def do_enable(cfg):
    f = managed_file()
    os.makedirs(os.path.dirname(f), exist_ok=True)
    settings = read_json(f) or {}
    env = settings.get("env") or {}
    env.update(build_env(cfg))
    settings["env"] = env
    with open(f, "w", encoding="utf-8") as fh:
        json.dump(settings, fh, indent=2, ensure_ascii=False)
        fh.write("\n")


def disable_one(file):
    settings = read_json(file)
    if settings is None:
        return "нет файла"
    env = settings.get("env")
    if not isinstance(env, dict):
        return "нет телеметрии"
    hit = [k for k in TELEMETRY_KEYS if k in env]
    if not hit:
        return "нет телеметрии"
    for k in hit:
        del env[k]
    if not env:
        settings.pop("env", None)
    if not settings:
        os.remove(file)
        return "файл удалён"
    with open(file, "w", encoding="utf-8") as fh:
        json.dump(settings, fh, indent=2, ensure_ascii=False)
        fh.write("\n")
    return "ключи удалены"


def do_disable(managed, user):
    return disable_one(managed), disable_one(user)


# ── Состояние ────────────────────────────────────────────────────────────────
def file_state(file):
    s = read_json(file)
    if s is None:
        return {"present": False}
    env = s.get("env") or {}
    hit = [k for k in TELEMETRY_KEYS if k in env]
    if not hit:
        return {"present": True, "on": False}
    return {
        "present": True, "on": True,
        "endpoint": env.get("OTEL_EXPORTER_OTLP_LOGS_ENDPOINT")
        or env.get("OTEL_EXPORTER_OTLP_METRICS_ENDPOINT") or "",
        "prompts": env.get("OTEL_LOG_USER_PROMPTS") == "1",
    }


def is_on():
    return file_state(managed_file()).get("on") or file_state(user_file()).get("on") or False


def status_text():
    lines = ["Состояние телеметрии Claude Code (%s):" % SYS, ""]
    for label, f in [("Системный (managed)", managed_file()), ("Пользовательский", user_file())]:
        st = file_state(f)
        if not st["present"]:
            lines.append("  %s: файла нет — чисто" % label)
        elif not st["on"]:
            lines.append("  %s: телеметрии нет — чисто" % label)
        else:
            lines.append("  %s: ВКЛ → %s%s" % (
                label, st["endpoint"],
                "  [+ЛОГ ПРОМПТОВ]" if st["prompts"] else ""))
    cfg = load_config()
    acct = (cfg.get("account") or "").strip()
    lines.append("")
    lines.append("  Фильтр аккаунтов: " + (acct if acct else "выкл (отправляется всё)"))

    st = PROXY_STATE
    lines.append("  Трафик с запуска: " + traffic_summary())
    if st["ok_seq"]:
        lines.append("  Последняя доставка: %s назад"
                     % _ago(time.monotonic() - st["last_ok"]))
    if st["accounts"]:
        lines.append("")
        lines.append("  По аккаунтам (доставлено / отфильтровано):")
        for email, acc in sorted(st["accounts"].items()):
            mark = {"ok": "доставляется", "filtered": "фильтруется"}.get(acc["last"], "")
            if acc["at"] and time.monotonic() - acc["at"] >= STALE_AFTER:
                mark = "молчит"
            lines.append("    %-28s %d / %d   %s"
                         % (email or "без подписи", acc["ok"], acc["filtered"], mark))
    return "\n".join(lines)


def _ago(sec):
    sec = int(sec)
    if sec < 60:
        return "%d с" % sec
    if sec < 3600:
        return "%d мин" % (sec // 60)
    return "%d ч %d мин" % (sec // 3600, (sec % 3600) // 60)


# ── Лог ──────────────────────────────────────────────────────────────────────
def log_path():
    return os.path.join(os.path.dirname(config_path()), "claude-telemetry.log")


def log_line(text):
    try:
        p = log_path()
        os.makedirs(os.path.dirname(p), exist_ok=True)
        try:
            if os.path.getsize(p) > 5 * 1024 * 1024:
                os.replace(p, p + ".1")
        except OSError:
            pass
        with open(p, "a", encoding="utf-8") as f:
            f.write("%s  %s\n" % (datetime.now().strftime("%Y-%m-%d %H:%M:%S"), text))
    except Exception:
        pass


# ── Разбор OTLP-тела для фильтра по аккаунту ─────────────────────────────────
def otlp_attrs(body_bytes):
    """Достаёт {ключ: значение} из атрибутов OTLP/JSON (resource/scope/record)."""
    out = {}
    try:
        obj = json.loads(body_bytes.decode("utf-8", "replace"))
    except Exception:
        return out

    def walk(o):
        if isinstance(o, dict):
            if "key" in o and isinstance(o.get("value"), dict):
                v = o["value"]
                val = v.get("stringValue")
                if val is None:
                    for kk in ("intValue", "boolValue", "doubleValue"):
                        if kk in v:
                            val = v[kk]
                            break
                if val is not None:
                    out[str(o["key"])] = str(val)
            for vv in o.values():
                walk(vv)
        elif isinstance(o, list):
            for it in o:
                walk(it)

    walk(obj)
    return out


# ── Настоящий владелец сессии ────────────────────────────────────────────────
# Claude Code подписывает телеметрию аккаунтом из ~/.claude.json (oauthAccount),
# а не аккаунтом окна, в котором реально работает пользователь. Если в приложении
# подключено несколько аккаунтов, подпись оказывается чужой: чат ведётся в одном
# аккаунте, а user.email/account_uuid/organization.id уезжают от другого.
# Настоящего владельца видно по тому, в чьей папке приложение хранит сессию:
#   <данные Claude>/claude-code-sessions/<account_uuid>/<organization_uuid>/<session_id>
_SESS_CACHE = {"map": {}, "orgs": {}, "scanned": 0.0, "logged": None, "warned": 0.0}
_SESS_RESCAN_INTERVAL = 5       # сек; свежая сессия появляется на диске не сразу


_ROOTS_CACHE = {"value": None, "at": 0.0}
_SESSION_DIRS = ("claude-code-sessions", "local-agent-mode-sessions")


def claude_data_roots():
    """Каталоги данных приложения Claude.

    Имя каталога жёстко не задаём: на Windows их сразу два — «Claude» и
    «Claude-msix2», на других системах оно тоже может отличаться. Поэтому
    просматриваем стандартные места и берём те каталоги со словом claude,
    внутри которых действительно лежат сессии."""
    # Пустой результат не кэшируем: при старте вместе с системой каталоги
    # приложения могут быть ещё не видны, и запомнить «ничего нет» на пять минут
    # означает работать с пустым индексом — то есть молча ронять чужую сессию
    # в фильтр, не проверив её владельца.
    if (_ROOTS_CACHE["value"]
            and time.monotonic() - _ROOTS_CACHE["at"] < 300):
        return _ROOTS_CACHE["value"]

    home = os.path.expanduser("~")
    if SYS == "Windows":
        bases = [os.environ.get("APPDATA"), os.environ.get("LOCALAPPDATA")]
    elif SYS == "Darwin":
        bases = [os.path.join(home, "Library", "Application Support")]
    else:
        bases = [os.path.join(home, ".config"),
                 os.path.join(home, ".local", "share")]

    roots = []
    for base in bases:
        if not base:
            continue
        for name in _listdir(base):
            if "claude" not in name.lower():
                continue
            path = os.path.join(base, name)
            if any(os.path.isdir(os.path.join(path, d)) for d in _SESSION_DIRS):
                roots.append(path)
    _ROOTS_CACHE.update(value=roots, at=time.monotonic())
    return roots


_UUID_RE = re.compile(
    r"\b[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\b", re.I)
_FILE_CACHE = {}     # путь → (mtime, размер, [session_id, …])


def _uuids_in_file(path):
    """id сессий внутри файла-указателя; результат кэшируется по mtime/размеру."""
    try:
        st = os.stat(path)
    except OSError:
        return []
    key = (st.st_mtime, st.st_size)
    hit = _FILE_CACHE.get(path)
    if hit and hit[0] == key:
        return hit[1]
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            ids = list({u.lower() for u in _UUID_RE.findall(f.read())})
    except OSError:
        ids = []
    _FILE_CACHE[path] = (key, ids)
    return ids


def _scan_sessions():
    """session_id → (account_uuid, organization_uuid).

    Приложение раскладывает сессии по папкам аккаунта и организации, но набор
    самих папок различается между системами: на Windows это «claude-code-sessions»
    и «local-agent-mode-sessions», на macOS второй есть, а первого нет. Поэтому
    имена не перечисляем, а берём внутри каталога данных всё, что похоже на
    хранилище сессий, и разбираем оба встречающихся вида раскладки:

      <sessions>/<acct>/<org>/<session_id>            — обычный чат
      <sessions>/<acct>/<org>/deleted_<session_id>    — удалённый чат
      <sessions>/<acct>/<org>/local_*.json            — файл-указатель, id внутри
      <sessions>/<acct>/<org>/*/.claude/projects/*/<session_id>.jsonl
    """
    out = {}
    for root in claude_data_roots():
        for store in _listdir(root):
            if "session" not in store.lower():
                continue
            base = os.path.join(root, store)
            for acct in _listdir(base):
                if not _UUID_RE.fullmatch(acct):
                    continue
                for org in _listdir(os.path.join(base, acct)):
                    odir = os.path.join(base, acct, org)
                    # организация нужна, чтобы подставить подпись по процессу,
                    # когда сессии в индексе нет
                    _SESS_CACHE["orgs"].setdefault(acct, org)
                    for name in _listdir(odir):
                        if name.startswith("local_") and name.endswith(".json"):
                            for sid in _uuids_in_file(os.path.join(odir, name)):
                                out[sid] = (acct, org)
                            continue
                        sid = name[8:] if name.startswith("deleted_") else name
                        sid = os.path.splitext(sid)[0].lower()
                        if _UUID_RE.fullmatch(sid):
                            out[sid] = (acct, org)
                    for path in glob.glob(os.path.join(
                            odir, "*", ".claude", "projects", "*", "*.jsonl")):
                        sid = os.path.splitext(os.path.basename(path))[0].lower()
                        if _UUID_RE.fullmatch(sid):
                            out[sid] = (acct, org)
    return out


def _listdir(path):
    try:
        return os.listdir(path)
    except OSError:
        return []


def session_owner(session_id):
    """(account_uuid, organization_uuid) владельца сессии либо None.

    Только чтение готового индекса: обход диска стоит доли секунды и живёт
    в фоновом потоке, иначе он тормозил бы пересылку телеметрии."""
    if not session_id:
        return None
    return _SESS_CACHE["map"].get(session_id)


def _session_index_loop():
    """Фоновое обновление индекса сессий.

    Пауза растёт вместе со стоимостью обхода: на машине с сотнями сессий он
    занимает около секунды, и молотить его каждые пять секунд незачем."""
    while True:
        took = 0.0
        try:
            # Само-восстановление: если индекс пуст, не доверяем кэшам каталогов.
            # После обновления Claude или выхода из гибернации долго живущий
            # процесс заклинивало с пустым индексом, хотя свежий процесс те же
            # каталоги видел. Сбрасываем кэши и ищем с нуля — тогда заклинивший
            # трей ведёт себя как только что запущенный, без ручного перезапуска.
            if not _SESS_CACHE["map"]:
                _ROOTS_CACHE["value"] = None
                _FILE_CACHE.clear()
            started = time.monotonic()
            scanned = _scan_sessions()
            # Пустой обход не затирает рабочий индекс: во время обновления
            # приложения каталоги на секунды пропадают, а терять по этому поводу
            # разбор сотен сессий (и снова слепо фильтровать) нельзя. Каталоги
            # реально исчезли — увидим по warning ниже, но старую карту храним.
            if scanned or not _SESS_CACHE["map"]:
                _SESS_CACHE["map"] = scanned
            if _SESS_CACHE["map"]:
                PROXY_STATE["empty_since"] = 0.0
            elif not PROXY_STATE["empty_since"]:
                PROXY_STATE["empty_since"] = time.monotonic()
            refresh_connection_owners(int(load_config().get("proxyPort", 4318)))
            took = time.monotonic() - started
            mark = (len(_SESS_CACHE["map"]), tuple(claude_data_roots()))
            if mark != _SESS_CACHE["logged"]:
                _SESS_CACHE["logged"] = mark
                log_line("индекс сессий: %d шт. за %.1f с, каталоги: %s"
                         % (mark[0], took, ", ".join(mark[1]) or "не найдены"))
            # Пустой индекс — это неработающая проверка аккаунта, а не тишина:
            # напоминаем о нём, иначе трей часами фильтрует вслепую и молчит.
            if not _SESS_CACHE["map"]:
                now = time.monotonic()
                if now - _SESS_CACHE["warned"] > 300:
                    _SESS_CACHE["warned"] = now
                    log_line("ВНИМАНИЕ: индекс сессий пуст — владелец сессии не "
                             "проверяется, телеметрия судится по исходной подписи")
        except Exception:
            # поток обязан пережить любую ошибку: без него подпись не чинится
            log_line("индекс сессий: сбой обхода:" + chr(10)
                     + traceback.format_exc())
        time.sleep(max(_SESS_RESCAN_INTERVAL, took * 10))


# ── Аккаунт по процессу, а не по файлам сессий ───────────────────────────────
# Указатели сессий на диске появляются и исчезают, поэтому опознать владельца
# удаётся не всегда. Но у каждого профиля приложения свой каталог данных, и в
# нём лежит config.json с полем lastKnownAccountUuid — то есть аккаунт можно
# узнать по самому процессу, который прислал пакет: соединение → PID → каталог
# профиля → аккаунт. Это не зависит от того, успел ли Claude записать сессию.
CONN_ACCOUNTS = {}              # порт клиента → account_uuid
_PROFILE_CACHE = {}             # config.json → (mtime, account_uuid)


def _profile_account(exe_path):
    """account_uuid профиля, из которого запущен процесс."""
    d = os.path.dirname(exe_path or "")
    for _ in range(6):                       # claude-code/<версия>/claude.exe → корень
        cfg = os.path.join(d, "config.json")
        try:
            st = os.stat(cfg)
        except OSError:
            st = None
        if st:
            hit = _PROFILE_CACHE.get(cfg)
            if not hit or hit[0] != st.st_mtime:
                acct = None
                try:
                    with open(cfg, encoding="utf-8") as f:
                        acct = (json.load(f) or {}).get("lastKnownAccountUuid")
                except Exception:
                    pass
                _PROFILE_CACHE[cfg] = (st.st_mtime, acct)
            acct = _PROFILE_CACHE[cfg][1]
            if acct:
                return acct
        parent = os.path.dirname(d)
        if parent == d:
            break
        d = parent
    return None


_CONN_REFRESH = {"at": 0.0}
_CONN_REFRESH_INTERVAL = 30     # сек


def refresh_connection_owners(port, force=False):
    """Обновляет карту «соединение → аккаунт» по таблице TCP.

    Делается в фоне одним вызовом на все соединения: разбирать процесс прямо
    в обработчике запроса значило бы задерживать пересылку телеметрии."""
    if SYS != "Windows":
        return
    now = time.monotonic()
    if not force and now - _CONN_REFRESH["at"] < _CONN_REFRESH_INTERVAL:
        return
    _CONN_REFRESH["at"] = now
    ps = ("Get-NetTCPConnection -RemotePort %d -ErrorAction SilentlyContinue | "
          "ForEach-Object { $e=(Get-CimInstance Win32_Process -Filter "
          "(\"ProcessId=\"+$_.OwningProcess)).ExecutablePath; "
          "\"$($_.LocalPort) $e\" }" % port)
    try:
        out = subprocess.run(
            ["powershell", "-NoProfile", "-Command", ps],
            capture_output=True, text=True, timeout=30,
            # без этого флага каждый вызов из pythonw.exe рисует консольное окно:
            # трей живёт без консоли, и PowerShell заводит её себе сам
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0)).stdout
    except Exception:
        return
    fresh = {}
    for line in (out or "").splitlines():
        parts = line.strip().split(None, 1)
        if len(parts) != 2 or not parts[0].isdigit():
            continue
        acct = _profile_account(parts[1])
        if acct:
            fresh[int(parts[0])] = acct
    if fresh:
        CONN_ACCOUNTS.clear()
        CONN_ACCOUNTS.update(fresh)


def _resolve_connection(port):
    """Спрашивает процесс по одному соединению — когда фоновая карта не успела.

    Короткоживущее соединение может возникнуть и закрыться между фоновыми
    обходами; его пакеты тогда уходили по исходной подписи, а она при одном
    залогиненном аккаунте у всех одинаковая. Пока запрос обрабатывается,
    соединение заведомо живо, поэтому спросить о нём можно прямо здесь."""
    if SYS != "Windows":
        return None
    ps = ("$c=Get-NetTCPConnection -LocalPort %d -ErrorAction SilentlyContinue | "
          "Select-Object -First 1; if ($c) { (Get-CimInstance Win32_Process -Filter "
          "(\"ProcessId=\"+$c.OwningProcess)).ExecutablePath }" % port)
    try:
        out = subprocess.run(
            ["powershell", "-NoProfile", "-Command", ps],
            capture_output=True, text=True, timeout=15,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0)).stdout
    except Exception:
        return None
    return _profile_account((out or "").strip())


def connection_owner(client_address, resolve=False):
    """(account_uuid, organization_uuid) по процессу, приславшему пакет."""
    try:
        port = client_address[1]
    except Exception:
        return None
    acct = CONN_ACCOUNTS.get(port)
    if not acct and resolve:
        acct = _resolve_connection(port)
        if acct:
            CONN_ACCOUNTS[port] = acct
    if not acct:
        return None
    org = _SESS_CACHE["orgs"].get(acct)
    return (acct, org) if org else None


# Подсессии (агенты, хуки) своих указателей на диске не получают, опознать их
# напрямую невозможно. Но экспортёр каждого процесса Claude держит собственное
# соединение с прокси, и внутри одного соединения аккаунт не меняется — значит
# неопознанную сессию можно отнести к владельцу последней опознанной в нём.
PROC_OWNER = {}                 # соединение → (account_uuid, organization_uuid)


def _sv(attr):
    """stringValue атрибута OTLP."""
    return ((attr or {}).get("value") or {}).get("stringValue")


def _fix_attr_list(lst, emails, changes, fallback=None, resolved=None):
    """Правит подпись аккаунта в одном наборе атрибутов (запись или точка метрики)."""
    am = {a.get("key"): a for a in lst if isinstance(a, dict) and a.get("key")}
    if "session.id" not in am:
        return
    cur_acct, cur_mail = _sv(am.get("user.account_uuid")), _sv(am.get("user.email"))
    # Пара «номер аккаунта ↔ почта» внутри пакета всегда согласованна (обе берутся
    # из одного места), неверна лишь её привязка к сессии. Поэтому таблицу почт
    # копим из любого пакета, даже из того, который сейчас будем править.
    if cur_acct and cur_mail:
        emails.setdefault(cur_acct, cur_mail)
    sid = _sv(am["session.id"])
    owner = session_owner(sid)
    if owner:
        if resolved is not None:
            resolved.append(owner)
    elif fallback:
        owner = fallback
        changes.append(("~", "сессия %s отнесена к процессу" % (sid or "")[:8]))
    else:
        changes.append(("?", "владелец сессии %s неизвестен" % (sid or "")[:8]))
        return
    acct, org = owner
    if cur_acct == acct:
        return

    def put(key, val):
        if key in am:
            am[key]["value"] = {"stringValue": val}
        else:
            lst.append({"key": key, "value": {"stringValue": val}})

    mail = emails.get(acct)
    if not mail:
        # Почта этого аккаунта неизвестна. Чинить номер аккаунта, оставив чужую
        # почту, нельзя — получится противоречивая подпись, которая хуже исходной.
        # Оставляем запись как есть и помечаем решение фильтра недостоверным.
        changes.append(("?", "почта аккаунта %s неизвестна — подпись не тронута"
                        % acct[:8]))
        return

    put("user.account_uuid", acct)
    put("organization.id", org)
    put("user.email", mail)
    changes.append((cur_mail or cur_acct or "?", mail))


def _walk_attr_lists(obj, fn):
    if isinstance(obj, dict):
        for k, v in obj.items():
            if k == "attributes" and isinstance(v, list):
                fn(v)
            else:
                _walk_attr_lists(v, fn)
    elif isinstance(obj, list):
        for item in obj:
            _walk_attr_lists(item, fn)


_PAIR_RE = re.compile(
    r"user\.email=([^\s,\]]+).*?user\.account_uuid=([0-9a-f-]{36})", re.I)


def seed_emails_from_log(emails):
    """Достаёт пары «аккаунт → почта» из собственного лога.

    Почта аккаунта берётся только из самой телеметрии, а Claude сейчас может
    подписывать все пакеты одним аккаунтом — тогда почту второго узнать неоткуда.
    Но в старых записях лога она, как правило, уже встречалась.
    Возвращает число новых пар."""
    added = 0
    for path in (log_path(), log_path() + ".1"):
        try:
            with open(path, "r", encoding="utf-8", errors="replace") as f:
                for line in f:
                    if "user.email=" not in line:
                        continue
                    for mail, acct in _PAIR_RE.findall(line):
                        if acct.lower() not in emails:
                            emails[acct.lower()] = mail
                            added += 1
        except OSError:
            continue
    return added


def fix_identity(body_bytes, emails, fallback=None):
    """Меняет чужую подпись аккаунта на владельца сессии.
    Возвращает (тело, список замен). Тело не меняется, если правит нечего."""
    try:
        obj = json.loads(body_bytes.decode("utf-8", "replace"))
    except Exception:
        return body_bytes, [], None
    changes, resolved = [], []
    _walk_attr_lists(obj, lambda lst: _fix_attr_list(
        lst, emails, changes, fallback, resolved))
    owner = resolved[-1] if resolved else None
    if not [c for c in changes if c[0] not in ("?", "~")]:
        return body_bytes, changes, owner
    return json.dumps(obj, ensure_ascii=False).encode("utf-8"), changes, owner


# События, которые означают именно работу. Служебные — plugin_loaded,
# mcp_server_connection — приходят при открытии окна и переподключении MCP,
# то есть сами по себе, и раньше красили кружок аккаунта в «доставляется»
# спустя часы после того, как в нём кто-то последний раз что-то делал.
WORK_EVENTS = ("user_prompt", "api_request", "assistant_response",
               "tool_decision", "tool_result", "hook_execution")


# ── Сканер утечек в исходящей телеметрии ─────────────────────────────────────
# Проверяем ровно то, что уходит на коллектор. Реакция — только лог и
# уведомление: ничего не режем, чтобы не потерять легитимную телеметрию из-за
# ложного совпадения, но факт утечки виден сразу, а не постфактум в логе.
SECRET_PATTERNS = [
    ("приватный ключ",  re.compile(r"-----BEGIN [A-Z0-9 ]*PRIVATE KEY-----")),
    ("ssh-ключ",        re.compile(r"ssh-(?:rsa|ed25519|dss)\s+[A-Za-z0-9+/]{40,}")),
    ("AWS access key",  re.compile(r"AKIA[0-9A-Z]{16}")),
    ("токен GitHub",    re.compile(r"gh[pousr]_[A-Za-z0-9]{20,}")),
    ("токен Slack",     re.compile(r"xox[baprs]-[A-Za-z0-9-]{10,}")),
    ("ключ OpenAI/ant", re.compile(r"(?:sk|sk-ant)-[A-Za-z0-9_\-]{20,}")),
    ("Bearer-токен",    re.compile(r"(?i)Bearer[ ]+[A-Za-z0-9._\-]{20,}")),
    ("JWT",             re.compile(r"eyJ[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{10,}")),
    ("пароль в тексте", re.compile(r"(?i)(?:password|passwd|pwd|пароль)\s*[:=]\s*\S{4,}")),
    ("секрет=…",        re.compile(r"(?i)(?:secret|api[_-]?key|token)\s*[:=]\s*\S{12,}")),
]


def scan_secrets(text):
    """[(категория, отпечаток)] найденных секретов. Отпечаток — не сам секрет,
    а его хеш: в лог и уведомление сам секрет не кладём."""
    out = []
    for name, rx in SECRET_PATTERNS:
        for m in rx.finditer(text):
            frag = m.group(0)
            fp = hashlib.sha1(frag.encode("utf-8", "replace")).hexdigest()[:10]
            out.append((name, fp))
    return out


def check_leak(body_bytes, path, email):
    """Сканирует пересылаемое тело; новые находки — в лог и в очередь уведомлений."""
    try:
        text = body_bytes.decode("utf-8", "replace")
    except Exception:
        return
    hits = scan_secrets(text)
    if not hits:
        return
    st = PROXY_STATE
    fresh = []
    for name, fp in hits:
        if fp in st["leak_seen"]:
            continue
        st["leak_seen"].add(fp)
        fresh.append(name)
    st["leaks"] += len(hits)
    if fresh:
        log_line("ВОЗМОЖНАЯ УТЕЧКА %s [%s]: %s (уходит на коллектор)"
                 % (path, email or "?", ", ".join(sorted(set(fresh)))))
        st["leak_alerts"].append(
            "Похоже на секрет в телеметрии (%s), уже ушёл на коллектор. "
            "Проверь лог и подумай про logUserPrompts." % ", ".join(sorted(set(fresh))))
        STATE_CHANGED.set()


def payload_has_activity(body_bytes):
    """True, если в пакете есть работа, а не периодический экспорт по таймеру.

    Метрики Claude шлёт раз в минуту независимо от того, происходит ли что-то;
    такой пинг не должен ни перекрашивать значок, ни попадать в счётчики.
    Работой считаются события из WORK_EVENTS: промпт, запрос к модели, вызов
    инструмента, хук, ответ. Не считаются метрики (у них событий нет вовсе) и
    служебные события вроде подключения MCP-серверов."""
    try:
        text = body_bytes.decode("utf-8", "replace")
    except Exception:
        return False
    return any('"%s"' % e in text for e in WORK_EVENTS)


def _account_entries(account):
    """Разбивает поле аккаунта в список (запятая/точка-с-запятой/пробел)."""
    raw = (account or "").replace(",", " ").replace(";", " ")
    return [p.strip().lower() for p in raw.split() if p.strip()]


def account_matches(account, body_bytes, attrs):
    """True, если телеметрия относится к одному из указанных значений.
    В поле можно перечислить через запятую/пробел:
      * точный email     — test@gmail.com
      * домен            — gmail.com  или  @gmail.com  (совпадут все *@gmail.com)
      * произвольный ID  — значение user.id / user.account_uuid / organization.id
    Пусто = фильтр выключен (отправляется всё)."""
    entries = _account_entries(account)
    if not entries:
        return True

    email = (attrs.get("user.email") or "").strip().lower()
    domain = email.split("@", 1)[1] if "@" in email else ""
    ids = [attrs.get(k, "").strip().lower() for k in ACCOUNT_ATTR_KEYS]
    ids = [v for v in ids if v]

    for e in entries:
        if "@" in e and not e.startswith("@"):
            # точный email
            if email and e == email:
                return True
        elif e.startswith("@") or "." in e:
            # домен: gmail.com или @gmail.com
            d = e[1:] if e.startswith("@") else e
            if domain and domain == d:
                return True
            if e in ids:  # вдруг это ID с точкой
                return True
        else:
            # произвольный идентификатор
            if e in ids:
                return True
    return False


# ── Локальный прокси-логгер ──────────────────────────────────────────────────
_SSL_CTX = None
_SSL_CTX_DONE = False


def _get_ssl_context():
    """SSL-контекст для пересылки на коллектор. На macOS системный Python часто
    не имеет CA-бандла и не может проверить HTTPS-сертификаты — используем certifi,
    иначе forward падает (значок краснеет, до коллектора ничего не доходит)."""
    global _SSL_CTX, _SSL_CTX_DONE
    if _SSL_CTX_DONE:
        return _SSL_CTX
    _SSL_CTX_DONE = True
    try:
        import ssl
        try:
            import certifi
            _SSL_CTX = ssl.create_default_context(cafile=certifi.where())
        except Exception:
            _SSL_CTX = ssl.create_default_context()
    except Exception:
        _SSL_CTX = None
    return _SSL_CTX


def _make_handler():
    class Handler(http.server.BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def _reply(self, code, resp=b"ok"):
            try:
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(resp)))
                self.end_headers()
                if resp:
                    self.wfile.write(resp)
            except Exception:
                pass

        def _forward(self):
            length = int(self.headers.get("Content-Length", 0) or 0)
            body = self.rfile.read(length) if length else b""
            cfg = load_config()

            # Подпись аккаунта, которую ставит Claude, при нескольких подключённых
            # аккаунтах бывает чужой. Чиним её по владельцу сессии — до фильтра,
            # чтобы фильтр работал по настоящему аккаунту, и до отправки, чтобы
            # на коллектор ушла верная разметка.
            activity = payload_has_activity(body)
            changes = []
            if cfg.get("fixAccount", True):
                emails = dict(cfg.get("accountEmails") or {})
                conn = self.client_address        # одно соединение = один процесс
                # спрашиваем процесс, если соединение ещё не в фоновой карте:
                # иначе пакет ушёл бы с чужой подписью, а вернуть его нельзя
                fallback = (connection_owner(conn, resolve=True)
                            or PROC_OWNER.get(conn))
                body, changes, owner = fix_identity(body, emails, fallback=fallback)
                if owner:
                    PROC_OWNER[conn] = owner
                if emails != (cfg.get("accountEmails") or {}):
                    cfg["accountEmails"] = emails
                    try:
                        save_config(cfg)
                    except Exception:
                        pass
                if cfg.get("logTelemetry", True):
                    # номер соединения в логе позволяет отличить процессы Claude
                    # друг от друга — иначе наследование аккаунта не проверить
                    where = "%s [соединение %s]" % (self.path, conn[1])
                    fixed = sorted({"%s → %s" % c for c in changes
                                    if c[0] not in ("?", "~")})
                    if fixed:
                        log_line("ПОДПИСЬ ИСПРАВЛЕНА %s: %s" % (where, ", ".join(fixed)))
                    inherited = sorted({c[1] for c in changes if c[0] == "~"})
                    if inherited:
                        log_line("ПОДПИСЬ ПО ПРОЦЕССУ %s: %s" % (where, ", ".join(inherited)))
                    skipped = sorted({c[1] for c in changes if c[0] == "?"})
                    if skipped:
                        log_line("ПОДПИСЬ НЕ ПРОВЕРЕНА %s: %s" % (where, ", ".join(skipped)))
            attrs = otlp_attrs(body)

            # Фильтр по аккаунту. Если владельца сессии определить не удалось,
            # пакет всё равно проходит фильтр — по той подписи, что в нём есть.
            # Отправлять «на всякий случай» нельзя: телеметрия чужого аккаунта
            # утечёт на коллектор, а это ровно то, ради чего фильтр и заведён.
            acct = (cfg.get("account") or "").strip()
            if acct and not account_matches(acct, body, attrs):
                seen = [a + "=" + attrs[a] for a in ACCOUNT_ATTR_KEYS if a in attrs]
                # «вслепую» — рабочий пакет режется, а владельца определить не
                # удалось: подпись могла остаться чужой, и это теряет свою работу
                blind = activity and cfg.get("fixAccount", True) and any(
                    c[0] == "?" for c in changes)
                note_traffic("filtered", attrs.get("user.email"),
                             detail="отфильтровано (аккаунт не в списке): "
                                    + (", ".join(seen) or "аккаунт не найден"),
                             activity=activity, blind=blind)
                if cfg.get("logTelemetry", True):
                    log_line("ПРОПУЩЕНО %s: аккаунт != '%s' (в телеметрии: %s)"
                             % (self.path, acct, ", ".join(seen) or "не найден"))
                self._reply(200)  # говорим Claude "ок", но НЕ пересылаем
                return

            check_leak(body, self.path, attrs.get("user.email"))

            base = cfg.get("base", "").rstrip("/")
            upstream = base + self.path
            if cfg.get("logTelemetry", True):
                who = [a + "=" + attrs[a] for a in ACCOUNT_ATTR_KEYS if a in attrs]
                try:
                    text = body.decode("utf-8", "replace")
                except Exception:
                    text = "<%d байт>" % len(body)
                log_line("ОТПРАВКА %s → %s  (%d байт)%s\n%s" % (
                    self.path, upstream, len(body),
                    ("  [" + ", ".join(who) + "]") if who else "", text))

            req = urllib.request.Request(upstream, data=body, method="POST")
            for h in ("Content-Type", "Authorization", "Content-Encoding"):
                if self.headers.get(h):
                    req.add_header(h, self.headers[h])
            try:
                ctx = (_get_ssl_context()
                       if upstream.lower().startswith("https") else None)
                with urllib.request.urlopen(req, timeout=15, context=ctx) as r:
                    code = getattr(r, "status", None) or r.getcode()
                    resp = r.read()
                if 200 <= int(code) < 300:
                    note_traffic("ok", attrs.get("user.email"),
                                 detail="сервер принял HTTP %s" % code,
                                 activity=activity)
                else:
                    note_traffic("error", detail="сервер ответил HTTP %s (не 2xx)" % code)
                    if cfg.get("logTelemetry", True):
                        log_line("ОТКАЗ сервера HTTP %s ← %s\n%s" % (
                            code, upstream, (resp or b"")[:400].decode("utf-8", "replace")))
            except urllib.error.HTTPError as e:
                code = e.code
                try:
                    resp = e.read()
                except Exception:
                    resp = b""
                if code in (401, 403):
                    note_traffic("error", detail="HTTP %s — неверный токен" % code)
                elif code == 404:
                    note_traffic("error", detail="HTTP 404 — неверный адрес/путь")
                elif code == 429:
                    note_traffic("error",
                                 detail="HTTP 429 — лимит запросов у коллектора (приёмник отбивает)")
                else:
                    # ЛЮБОЙ не-2xx — это отказ, а не успех (раньше ошибочно зеленело)
                    note_traffic("error", detail="сервер отверг HTTP %s" % code)
                if cfg.get("logTelemetry", True):
                    log_line("ОТКАЗ сервера HTTP %s ← %s\n%s" % (
                        code, upstream, (resp or b"")[:400].decode("utf-8", "replace")))
            except Exception as e:
                code, resp = 502, b""
                note_traffic("error",
                             detail="нет связи с сервером (%s)" % e.__class__.__name__)
                if cfg.get("logTelemetry", True):
                    log_line("ОШИБКА пересылки → %s: %s" % (upstream, e))
            self._reply(code, resp)

        def finish(self):
            # Номера портов операционная система переиспользует: если не забыть
            # закрытое соединение, новый процесс может унаследовать чужой аккаунт.
            try:
                PROC_OWNER.pop(self.client_address, None)
            finally:
                http.server.BaseHTTPRequestHandler.finish(self)

        def _monitor(self):
            """Приём тревог от внешнего наблюдателя (хук Claude Code).

            Хук за действиями агента живёт отдельным процессом и шлёт сюда
            находки, чтобы уведомление шло через тот же трей, а не заводить
            второй механизм оповещений."""
            length = int(self.headers.get("Content-Length", 0) or 0)
            raw = self.rfile.read(length) if length else b""
            try:
                ev = json.loads(raw.decode("utf-8", "replace"))
            except Exception:
                self._reply(400, b"bad json")
                return
            msg = str(ev.get("message") or "").strip()[:300]
            level = str(ev.get("level") or "info")
            if msg:
                log_line("ДЕЙСТВИЕ АГЕНТА [%s]: %s" % (level, msg))
                # Всплывашка только на серьёзное (alert): секретные пути,
                # скачать-и-запустить, эксфильтрация. Простое обращение наружу
                # (warn) — это любой git/curl/pip/yt_dlp, оно шумит; оставляем
                # его следом в логе, но не дёргаем уведомлением.
                if level == "alert":
                    PROXY_STATE["leak_alerts"].append("Claude: " + msg)
                    STATE_CHANGED.set()
            self._reply(200)

        def do_POST(self):
            if self.path.rstrip("/") == "/monitor":
                self._monitor()
            else:
                self._forward()

        def log_message(self, *a):
            pass

    return Handler


def start_proxy():
    PROXY_STATE["started_at"] = time.monotonic()
    cfg = load_config()
    if cfg.get("fixAccount", True):
        emails = dict(cfg.get("accountEmails") or {})
        if seed_emails_from_log(emails):
            cfg["accountEmails"] = emails
            try:
                save_config(cfg)
            except Exception:
                pass
            log_line("аккаунты из лога: " + ", ".join(
                "%s→%s" % (k[:8], v) for k, v in sorted(emails.items())))
    threading.Thread(target=_session_index_loop, daemon=True).start()
    port = int(cfg.get("proxyPort", 4318))
    try:
        httpd = http.server.ThreadingHTTPServer(("127.0.0.1", port), _make_handler())
    except Exception as e:
        PROXY_STATE.update(ok=False, bound=False, detail="порт %s занят (%s)" % (port, e.__class__.__name__))
        log_line("НЕ удалось открыть прокси на 127.0.0.1:%s — %s" % (port, e))
        return None
    PROXY_STATE.update(bound=True)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    log_line("прокси-логгер слушает 127.0.0.1:%s" % port)
    return httpd


# ── Права администратора и повышение ─────────────────────────────────────────
def is_admin():
    if SYS == "Windows":
        try:
            import ctypes
            return ctypes.windll.shell32.IsUserAnAdmin() != 0
        except Exception:
            return False
    try:
        return os.geteuid() == 0
    except AttributeError:
        return False


def run_elevated(extra_args):
    if SYS == "Windows":
        import ctypes
        params = subprocess.list2cmdline([SCRIPT] + extra_args)
        rc = ctypes.windll.shell32.ShellExecuteW(None, "runas", sys.executable, params, None, 1)
        return int(rc) > 32
    if SYS == "Darwin":
        cmd = " ".join(shlex.quote(x) for x in [sys.executable, SCRIPT] + extra_args)
        osa = 'do shell script "%s" with administrator privileges' % cmd.replace("\\", "\\\\").replace('"', '\\"')
        return subprocess.run(["osascript", "-e", osa]).returncode == 0
    if shutil.which("pkexec"):
        return subprocess.run(["pkexec", sys.executable, SCRIPT] + extra_args).returncode == 0
    return subprocess.run(["sudo", sys.executable, SCRIPT] + extra_args).returncode == 0


def _mac_run_root_shell(shell_cmd):
    """Выполняет shell-команду от root через osascript и возвращает успех.
    Реальную ошибку (отказ, отмена пароля и т.п.) пишем в лог."""
    osa = ('do shell script "%s" with administrator privileges'
           % shell_cmd.replace("\\", "\\\\").replace('"', '\\"'))
    r = subprocess.run(["osascript", "-e", osa], capture_output=True, text=True)
    if r.returncode != 0:
        log_line("osascript (root) ошибка: " + ((r.stderr or "").strip() or "код %d" % r.returncode))
    return r.returncode == 0


def _mac_write_root(dest, content):
    """Кладёт content в файл dest (system-wide) с правами root, без запуска
    Python от root: пишем во временный файл и копируем его shell-командой."""
    tmp = tempfile.NamedTemporaryFile("w", suffix=".json", delete=False, encoding="utf-8")
    tmp.write(content)
    tmp.close()
    d = os.path.dirname(dest)
    cmd = "/bin/mkdir -p %s && /bin/cp %s %s && /bin/chmod 644 %s" % (
        shlex.quote(d), shlex.quote(tmp.name), shlex.quote(dest), shlex.quote(dest))
    try:
        return _mac_run_root_shell(cmd)
    finally:
        try:
            os.remove(tmp.name)
        except OSError:
            pass


def elevate_enable(cfg):
    if is_admin():
        do_enable(cfg)
        return True
    if SYS == "Darwin":
        # Без запуска Python от root: собираем managed-settings.json в обычном
        # процессе и копируем его на место одной shell-командой через osascript.
        f = managed_file()
        settings = read_json(f) or {}
        env = settings.get("env") or {}
        env.update(build_env(cfg))
        settings["env"] = env
        content = json.dumps(settings, indent=2, ensure_ascii=False) + "\n"
        return _mac_write_root(f, content)
    tmp = tempfile.NamedTemporaryFile("w", suffix=".json", delete=False, encoding="utf-8")
    json.dump(cfg, tmp, ensure_ascii=False)
    tmp.close()
    return run_elevated(["--worker-enable", tmp.name])


def elevate_disable():
    if not file_state(managed_file()).get("on"):   # только пользовательский файл — без админа
        do_disable(managed_file(), user_file())
        return True
    if is_admin():
        do_disable(managed_file(), user_file())
        return True
    if SYS == "Darwin":
        # Пользовательский файл чистим без прав (он наш), managed — через root.
        disable_one(user_file())
        f = managed_file()
        settings = read_json(f) or {}
        env = settings.get("env")
        if isinstance(env, dict):
            for k in TELEMETRY_KEYS:
                env.pop(k, None)
            if not env:
                settings.pop("env", None)
        if not settings:
            return _mac_run_root_shell("/bin/rm -f %s" % shlex.quote(f))
        content = json.dumps(settings, indent=2, ensure_ascii=False) + "\n"
        return _mac_write_root(f, content)
    return run_elevated(["--worker-disable", user_file()])


# ── Автозапуск ───────────────────────────────────────────────────────────────
APP_ID = "ClaudeTelemetryTray"


def _win_launch_cmd():
    exe = sys.executable
    pyw = os.path.join(os.path.dirname(exe), "pythonw.exe")
    runner = pyw if os.path.exists(pyw) else exe
    return '"%s" "%s"' % (runner, SCRIPT)


def _mac_plist():
    return os.path.join(os.path.expanduser("~"), "Library", "LaunchAgents",
                        "io.github.claude-telemetry-tray.plist")


def _mac_label():
    return "io.github.claude-telemetry-tray"


def _xml_escape(v):
    return v.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _installed_script():
    """Путь к копии скрипта в app-support (не под защитой TCC)."""
    return os.path.join(os.path.dirname(config_path()), "claude-telemetry-tray.py")


def _ensure_installed_script():
    """Копирует скрипт в ~/Library/Application Support/claude-telemetry/.
    Папки ~/Documents, ~/Desktop, ~/Downloads защищены TCC: агент launchd при
    входе в систему получает 'Operation not permitted' и не может их прочитать.
    Поэтому для автозапуска используем копию в app-support."""
    dst = _installed_script()
    try:
        if os.path.abspath(SCRIPT) == os.path.abspath(dst):
            return dst
        os.makedirs(os.path.dirname(dst), exist_ok=True)
        shutil.copy2(SCRIPT, dst)
        return dst
    except Exception:
        log_line("не удалось скопировать скрипт в app-support:\n"
                 + traceback.format_exc())
        return SCRIPT


def _write_mac_plist(p):
    """Пишет LaunchAgent с перенаправлением логов, рабочей директорией и PATH,
    чтобы при входе в систему агент стартовал и любую ошибку можно было увидеть."""
    os.makedirs(os.path.dirname(p), exist_ok=True)
    logdir = os.path.dirname(config_path())
    try:
        os.makedirs(logdir, exist_ok=True)
    except OSError:
        pass
    out = os.path.join(logdir, "launchd.out")
    err = os.path.join(logdir, "launchd.err")
    prog = _ensure_installed_script()   # копия в app-support (доступна launchd)
    workdir = os.path.dirname(prog) or os.path.expanduser("~")
    plist = (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" '
        '"http://www.apple.com/DTDs/PropertyList-1.0.dtd">\n'
        '<plist version="1.0"><dict>\n'
        '  <key>Label</key><string>%s</string>\n'
        '  <key>ProgramArguments</key><array>'
        '<string>%s</string><string>%s</string></array>\n'
        '  <key>RunAtLoad</key><true/>\n'
        '  <key>WorkingDirectory</key><string>%s</string>\n'
        '  <key>StandardOutPath</key><string>%s</string>\n'
        '  <key>StandardErrorPath</key><string>%s</string>\n'
        '  <key>EnvironmentVariables</key><dict>'
        '<key>PATH</key><string>/usr/bin:/bin:/usr/sbin:/sbin</string></dict>\n'
        '</dict></plist>\n' % (
            _mac_label(),
            _xml_escape(sys.executable), _xml_escape(prog),
            _xml_escape(workdir), _xml_escape(out), _xml_escape(err))
    )
    with open(p, "w", encoding="utf-8") as f:
        f.write(plist)


def _linux_desktop():
    base = os.environ.get("XDG_CONFIG_HOME", os.path.join(os.path.expanduser("~"), ".config"))
    return os.path.join(base, "autostart", "claude-telemetry-tray.desktop")


def autostart_enabled():
    try:
        if SYS == "Windows":
            import winreg
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER,
                                r"Software\Microsoft\Windows\CurrentVersion\Run") as k:
                winreg.QueryValueEx(k, APP_ID)
                return True
        if SYS == "Darwin":
            return os.path.exists(_mac_plist())
        return os.path.exists(_linux_desktop())
    except FileNotFoundError:
        return False
    except Exception:
        return False


def set_autostart(on):
    if SYS == "Windows":
        import winreg
        with winreg.CreateKey(winreg.HKEY_CURRENT_USER,
                              r"Software\Microsoft\Windows\CurrentVersion\Run") as k:
            if on:
                winreg.SetValueEx(k, APP_ID, 0, winreg.REG_SZ, _win_launch_cmd())
            else:
                try:
                    winreg.DeleteValue(k, APP_ID)
                except FileNotFoundError:
                    pass
        return
    if SYS == "Darwin":
        p = _mac_plist()
        label = _mac_label()
        try:
            uid = os.getuid()
        except AttributeError:
            uid = None
        if on:
            # Просто кладём plist: при следующем входе launchd сам его запустит.
            # Не делаем bootstrap/load сейчас, чтобы не поднять второй экземпляр
            # поверх уже работающего (конфликт порта прокси, два значка).
            _write_mac_plist(p)
        else:
            if uid is not None:
                subprocess.run(["launchctl", "bootout", "gui/%d/%s" % (uid, label)],
                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            subprocess.run(["launchctl", "unload", p],
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            try:
                os.remove(p)
            except OSError:
                pass
        return
    p = _linux_desktop()
    if on:
        os.makedirs(os.path.dirname(p), exist_ok=True)
        desktop = (
            "[Desktop Entry]\n"
            "Type=Application\n"
            "Name=Claude Telemetry Tray\n"
            'Exec=%s "%s"\n'
            "Terminal=false\n"
            "X-GNOME-Autostart-enabled=true\n" % (sys.executable, SCRIPT)
        )
        with open(p, "w", encoding="utf-8") as f:
            f.write(desktop)
    else:
        try:
            os.remove(p)
        except OSError:
            pass


# ── Окно настроек (Tkinter) ──────────────────────────────────────────────────
def _open_config_in_editor():
    """Запасной путь: если нативное окно построить не удалось, открываем
    config.json в текстовом редакторе по умолчанию."""
    p = config_path()
    try:
        save_config(load_config())  # гарантируем, что файл есть и со всеми ключами
    except Exception:
        pass
    try:
        subprocess.run(["open", "-t", p])
    except Exception:
        try:
            subprocess.run(["open", p])
        except Exception:
            sys.stderr.write("Открой конфиг вручную: %s\n" % p)
    try:
        subprocess.run([
            "osascript", "-e",
            'display notification "Отредактируй значения, сохрани (Cmd+S) и закрой '
            'файл, затем нажми \u00abВключить\u00bb." with title "Claude Telemetry"'])
    except Exception:
        pass


def run_settings_window_macos():
    """Нативное окно настроек на AppKit (pyobjc). Используется на macOS, т.к.
    системный Tk 8.5 из CommandLineTools рисует пустые окна. При любой ошибке
    откатываемся к редактированию config.json в редакторе."""
    try:
        import AppKit
        import objc  # noqa: F401
        from Foundation import NSObject, NSMakeRect
    except Exception:
        _open_config_in_editor()
        return

    try:
        cfg = load_config()
        W, H, M = 520.0, 430.0, 20.0

        app = AppKit.NSApplication.sharedApplication()
        app.setActivationPolicy_(0)  # Regular — обычное окно с фокусом

        style = (getattr(AppKit, "NSWindowStyleMaskTitled", 1)
                 | getattr(AppKit, "NSWindowStyleMaskClosable", 2))
        backing = getattr(AppKit, "NSBackingStoreBuffered", 2)
        win = AppKit.NSWindow.alloc().initWithContentRect_styleMask_backing_defer_(
            NSMakeRect(0, 0, W, H), style, backing, False)
        win.setTitle_("Claude Telemetry — настройки")
        win.center()
        content = win.contentView()

        def mklabel(text, y, size=12.0):
            t = AppKit.NSTextField.alloc().initWithFrame_(
                NSMakeRect(M, y, W - 2 * M, 17))
            t.setStringValue_(text)
            t.setBezeled_(False); t.setDrawsBackground_(False)
            t.setEditable_(False); t.setSelectable_(False)
            t.setFont_(AppKit.NSFont.systemFontOfSize_(size))
            content.addSubview_(t)

        def mkfield(value, y):
            fld = AppKit.NSTextField.alloc().initWithFrame_(
                NSMakeRect(M, y, W - 2 * M, 24))
            fld.setStringValue_("" if value is None else str(value))
            content.addSubview_(fld)
            return fld

        def mkcheck(text, on, y):
            b = AppKit.NSButton.alloc().initWithFrame_(
                NSMakeRect(M, y, W - 2 * M, 22))
            b.setButtonType_(getattr(AppKit, "NSButtonTypeSwitch",
                                     getattr(AppKit, "NSSwitchButton", 3)))
            b.setTitle_(text)
            b.setState_(1 if on else 0)
            content.addSubview_(b)
            return b

        specs = [
            ("token", "Токен (Authorization: Bearer):"),
            ("base", "Базовый URL коллектора:"),
            ("account", "Аккаунты/домены (email, gmail.com, @gmail.com; пусто = всё):"),
            ("teamId", "Team ID:"),
            ("proxyPort", "Порт прокси (1–65535):"),
        ]
        fields = {}
        y = H - M - 17
        for key, lab in specs:
            mklabel(lab, y); y -= 26
            fields[key] = mkfield(cfg.get(key, ""), y); y -= 22
        y -= 6
        prompts_btn = mkcheck("Логировать текст промптов (чувствительно к ПДн!)",
                              bool(cfg.get("logUserPrompts", True)), y); y -= 28
        logtel_btn = mkcheck("Логировать отправляемую телеметрию в файл",
                             bool(cfg.get("logTelemetry", True)), y)

        def _alert(msg):
            a = AppKit.NSAlert.alloc().init()
            a.setMessageText_("Внимание")
            a.setInformativeText_(msg)
            a.runModal()

        class _SettingsDelegate(NSObject):
            def save_(self, sender):
                port = str(fields["proxyPort"].stringValue()).strip()
                if not port.isdigit() or not (1 <= int(port) <= 65535):
                    _alert("Порт прокси должен быть числом 1–65535."); return
                token = str(fields["token"].stringValue()).strip()
                base = str(fields["base"].stringValue()).strip().rstrip("/")
                if not token:
                    _alert("Токен не указан."); return
                if not base:
                    _alert("Базовый URL не указан."); return
                # Дописываем в существующий конфиг, не подменяя его целиком —
                # иначе теряются ключи, которых окно не знает (таблица почт).
                merged = load_config()
                merged.update({
                    "token": token,
                    "base": base,
                    "account": str(fields["account"].stringValue()).strip(),
                    "teamId": str(fields["teamId"].stringValue()).strip(),
                    "logUserPrompts": bool(prompts_btn.state()),
                    "logTelemetry": bool(logtel_btn.state()),
                    "proxyPort": int(port),
                })
                save_config(merged)
                app.terminate_(None)

            def cancel_(self, sender):
                app.terminate_(None)

            def windowWillClose_(self, note):
                app.terminate_(None)

            def import_(self, sender):
                # NSOpenPanel в незабандленном агенте не получает клики мыши
                # (панель живёт в отдельном XPC-процессе), поэтому показываем
                # системный выбор файла через AppleScript — он кликается стабильно.
                script = (
                    'try\n'
                    '  set f to choose file with prompt "Импорт настроек (JSON)"\n'
                    '  return POSIX path of f\n'
                    'on error\n'
                    '  return ""\n'
                    'end try'
                )
                r = subprocess.run(["osascript", "-e", script],
                                   capture_output=True, text=True)
                path = (r.stdout or "").strip()
                if not path:
                    return
                try:
                    with open(path, "r", encoding="utf-8") as fh:
                        data = json.load(fh)
                    if not isinstance(data, dict):
                        raise ValueError("ожидался JSON-объект")
                except Exception as e:
                    _alert("Не удалось прочитать файл:\n%s" % e)
                    return
                for k in ("token", "base", "account", "teamId", "proxyPort"):
                    if k in data:
                        fields[k].setStringValue_(str(data.get(k, "")))
                if "logUserPrompts" in data:
                    prompts_btn.setState_(1 if data.get("logUserPrompts") else 0)
                if "logTelemetry" in data:
                    logtel_btn.setState_(1 if data.get("logTelemetry") else 0)
                _alert("Настройки загружены. Проверь значения и нажми «Сохранить».")

            def export_(self, sender):
                script = (
                    'try\n'
                    '  set f to choose file name with prompt "Экспорт настроек" '
                    'default name "claude-telemetry-config.json"\n'
                    '  return POSIX path of f\n'
                    'on error\n'
                    '  return ""\n'
                    'end try'
                )
                r = subprocess.run(["osascript", "-e", script],
                                   capture_output=True, text=True)
                path = (r.stdout or "").strip()
                if not path:
                    return
                port = str(fields["proxyPort"].stringValue()).strip()
                cur = {
                    "token": str(fields["token"].stringValue()).strip(),
                    "base": str(fields["base"].stringValue()).strip().rstrip("/"),
                    "account": str(fields["account"].stringValue()).strip(),
                    "teamId": str(fields["teamId"].stringValue()).strip(),
                    "logUserPrompts": bool(prompts_btn.state()),
                    "logTelemetry": bool(logtel_btn.state()),
                    "proxyPort": int(port) if port.isdigit() else 4318,
                }
                try:
                    with open(path, "w", encoding="utf-8") as fh:
                        json.dump(cur, fh, indent=2, ensure_ascii=False)
                        fh.write("\n")
                except Exception as e:
                    _alert("Не удалось сохранить:\n%s" % e)
                    return
                _alert("Настройки сохранены в файл.")

        delegate = _SettingsDelegate.alloc().init()
        win.setDelegate_(delegate)

        bw, bh = 100.0, 30.0

        def mkbtn(title, x, action, default=False):
            b = AppKit.NSButton.alloc().initWithFrame_(NSMakeRect(x, M, bw, bh))
            b.setTitle_(title)
            b.setBezelStyle_(getattr(AppKit, "NSBezelStyleRounded", 1))
            if default:
                b.setKeyEquivalent_("\r")
            b.setTarget_(delegate); b.setAction_(action)
            content.addSubview_(b)
            return b

        # слева — импорт/экспорт, справа — отмена/сохранить
        mkbtn("Импорт…", M, "import:")
        mkbtn("Экспорт…", M + bw + 8, "export:")
        mkbtn("Отмена", W - M - 2 * bw - 8, "cancel:")
        mkbtn("Сохранить", W - M - bw, "save:", default=True)

        win.makeKeyAndOrderFront_(None)
        app.activateIgnoringOtherApps_(True)
        app.run()
    except Exception:
        log_line("нативное окно настроек не построилось:\n" + traceback.format_exc())
        _open_config_in_editor()


def run_settings_window():
    if SYS == "Darwin":
        return run_settings_window_macos()
    try:
        import tkinter as tk
        from tkinter import ttk, messagebox, filedialog
    except Exception:
        sys.stderr.write("Tkinter недоступен. На Linux установи пакет python3-tk.\n")
        return
    cfg = load_config()
    root = tk.Tk()
    root.title("Claude Telemetry — настройки")
    root.resizable(False, False)

    # --- Буфер обмена: правый клик + Ctrl/Cmd по КОДУ клавиши (не зависит от раскладки) ---
    _clip = tk.Menu(root, tearoff=0)

    def _clip_do(ev):
        w = getattr(_clip, "_t", None)
        if w is not None:
            w.event_generate(ev)

    _clip.add_command(label="Вырезать", command=lambda: _clip_do("<<Cut>>"))
    _clip.add_command(label="Копировать", command=lambda: _clip_do("<<Copy>>"))
    _clip.add_command(label="Вставить", command=lambda: _clip_do("<<Paste>>"))
    _clip.add_separator()
    _clip.add_command(label="Выделить всё", command=lambda: _clip_do("<<SelectAll>>"))

    def _clip_popup(e):
        _clip._t = e.widget
        try:
            _clip.tk_popup(e.x_root, e.y_root)
        finally:
            _clip.grab_release()
        return "break"

    if SYS == "Windows":
        _kc = {86: "<<Paste>>", 67: "<<Copy>>", 88: "<<Cut>>", 65: "<<SelectAll>>"}
    elif SYS == "Darwin":
        _kc = {9: "<<Paste>>", 8: "<<Copy>>", 7: "<<Cut>>", 0: "<<SelectAll>>"}
    else:
        _kc = {55: "<<Paste>>", 54: "<<Copy>>", 53: "<<Cut>>", 38: "<<SelectAll>>"}

    def _on_mod_key(e):
        act = _kc.get(e.keycode)
        if act:
            e.widget.event_generate(act)
            return "break"
        return None

    for _cls in ("TEntry", "Entry"):
        root.bind_class(_cls, "<Button-3>", _clip_popup)
        root.bind_class(_cls, "<Button-2>", _clip_popup)
        root.bind_class(_cls, "<Control-KeyPress>", _on_mod_key)
        try:
            root.bind_class(_cls, "<Command-KeyPress>", _on_mod_key)
        except Exception:
            pass

    frm = ttk.Frame(root, padding=16)
    frm.pack(fill="both", expand=True)

    ttk.Label(frm, text="Токен (Authorization: Bearer):").pack(anchor="w")
    token_var = tk.StringVar(value=cfg.get("token", ""))
    ttk.Entry(frm, textvariable=token_var, width=60).pack(fill="x", pady=(0, 8))

    ttk.Label(frm, text="Базовый URL коллектора:").pack(anchor="w")
    base_var = tk.StringVar(value=cfg.get("base", ""))
    ttk.Entry(frm, textvariable=base_var, width=60).pack(fill="x", pady=(0, 8))

    ttk.Label(frm, text="Аккаунты/домены (email, gmail.com, @gmail.com; через запятую; пусто = всё):").pack(anchor="w")
    acct_var = tk.StringVar(value=cfg.get("account", ""))
    ttk.Entry(frm, textvariable=acct_var, width=60).pack(fill="x", pady=(0, 8))

    row = ttk.Frame(frm)
    row.pack(fill="x", pady=(0, 8))
    ttk.Label(row, text="Team ID:").pack(side="left")
    team_var = tk.StringVar(value=cfg.get("teamId", ""))
    ttk.Entry(row, textvariable=team_var, width=18).pack(side="left", padx=(6, 16))
    ttk.Label(row, text="Порт прокси:").pack(side="left")
    port_var = tk.StringVar(value=str(cfg.get("proxyPort", 4318)))
    ttk.Entry(row, textvariable=port_var, width=8).pack(side="left", padx=(6, 0))

    prompts_var = tk.BooleanVar(value=bool(cfg.get("logUserPrompts", True)))
    ttk.Checkbutton(frm, text="Логировать текст промптов (чувствительно к ПДн!)",
                    variable=prompts_var).pack(anchor="w", pady=(0, 4))

    logtel_var = tk.BooleanVar(value=bool(cfg.get("logTelemetry", True)))
    ttk.Checkbutton(frm, text="Логировать отправляемую телеметрию в файл",
                    variable=logtel_var).pack(anchor="w", pady=(0, 14))

    def save():
        port = port_var.get().strip()
        if not port.isdigit() or not (1 <= int(port) <= 65535):
            messagebox.showwarning("Внимание", "Порт прокси должен быть числом 1–65535.")
            return
        new = {
            "token": token_var.get().strip(),
            "base": base_var.get().strip().rstrip("/"),
            "account": acct_var.get().strip(),
            "teamId": team_var.get().strip(),
            "logUserPrompts": bool(prompts_var.get()),
            "logTelemetry": bool(logtel_var.get()),
            "proxyPort": int(port),
        }
        if not new["token"]:
            messagebox.showwarning("Внимание", "Токен не указан.")
            return
        if not new["base"]:
            messagebox.showwarning("Внимание", "Базовый URL не указан.")
            return
        managed_keys = ("token", "teamId", "proxyPort", "logUserPrompts",
                        "logsExportInterval")
        need_reapply = is_on() and any(
            str(cfg.get(k, "")) != str(new.get(k, "")) for k in managed_keys)
        # Дописываем поля окна в существующий конфиг, а не подменяем его целиком:
        # иначе стирается всё, чего окно не знает (таблица почт аккаунтов и пр.).
        merged = load_config()
        merged.update(new)
        new = merged
        save_config(new)
        if need_reapply:
            ok = elevate_enable(new)
            if ok:
                messagebox.showinfo("Сохранено",
                                    "Настройки сохранены и применены.\n"
                                    "Перезапусти терминалы и IDE.")
            else:
                messagebox.showwarning("Сохранено",
                                       "Настройки сохранены, но применить не удалось "
                                       "(права отклонены?).\nНажми «Включить» в трее вручную.")
        else:
            messagebox.showinfo("Сохранено", "Настройки сохранены.")
        root.destroy()

    def do_import():
        path = filedialog.askopenfilename(
            title="Импорт настроек",
            filetypes=[("Конфиг JSON", "*.json"), ("Все файлы", "*.*")])
        if not path:
            return
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
        except Exception as e:
            messagebox.showerror("Импорт", "Не удалось прочитать файл:\n%s" % e)
            return
        if not isinstance(data, dict):
            messagebox.showerror("Импорт", "Файл не похож на конфиг (ожидался JSON-объект).")
            return
        if "token" in data:
            token_var.set(str(data.get("token", "")))
        if "base" in data:
            base_var.set(str(data.get("base", "")))
        if "account" in data:
            acct_var.set(str(data.get("account", "")))
        if "teamId" in data:
            team_var.set(str(data.get("teamId", "")))
        if "proxyPort" in data:
            port_var.set(str(data.get("proxyPort", "")))
        if "logUserPrompts" in data:
            prompts_var.set(bool(data.get("logUserPrompts")))
        if "logTelemetry" in data:
            logtel_var.set(bool(data.get("logTelemetry")))
        messagebox.showinfo("Импорт", "Настройки загружены из файла.\nПроверь значения и нажми «Сохранить».")

    def do_export():
        path = filedialog.asksaveasfilename(
            title="Экспорт настроек", defaultextension=".json",
            initialfile="claude-telemetry-config.json",
            filetypes=[("Конфиг JSON", "*.json")])
        if not path:
            return
        port = port_var.get().strip()
        cur = {
            "token": token_var.get().strip(),
            "base": base_var.get().strip().rstrip("/"),
            "account": acct_var.get().strip(),
            "teamId": team_var.get().strip(),
            "logUserPrompts": bool(prompts_var.get()),
            "logTelemetry": bool(logtel_var.get()),
            "proxyPort": int(port) if port.isdigit() else 4318,
        }
        try:
            with open(path, "w", encoding="utf-8") as f:
                json.dump(cur, f, indent=2, ensure_ascii=False)
                f.write("\n")
            messagebox.showinfo("Экспорт", "Настройки сохранены в файл.")
        except Exception as e:
            messagebox.showerror("Экспорт", "Не удалось сохранить:\n%s" % e)

    btns = ttk.Frame(frm)
    btns.pack(fill="x")
    ttk.Button(btns, text="Сохранить", command=save).pack(side="right")
    ttk.Button(btns, text="Отмена", command=root.destroy).pack(side="right", padx=(0, 8))
    ttk.Button(btns, text="Импорт…", command=do_import).pack(side="left")
    ttk.Button(btns, text="Экспорт…", command=do_export).pack(side="left", padx=(8, 0))
    root.update_idletasks()
    root.mainloop()


# ── Трей ─────────────────────────────────────────────────────────────────────
def _venv_dir():
    return os.path.join(os.path.dirname(config_path()), "venv")


def _venv_python(vdir):
    if SYS == "Windows":
        return os.path.join(vdir, "Scripts", "python.exe")
    return os.path.join(vdir, "bin", "python3")


def _deps_importable():
    try:
        import pystray  # noqa
        from PIL import Image  # noqa
        import certifi      # noqa  (нужен для проверки HTTPS-сертификатов коллектора)
        if SYS == "Darwin":
            import AppKit       # noqa
            import Foundation   # noqa
        return True
    except Exception:
        return False


def ensure_runtime():
    """Делает программу самодостаточной: если нужных модулей нет, создаёт
    собственный venv, ставит туда зависимости ТОЛЬКО из готовых wheel
    (без компиляции) и перезапускает себя внутри этого venv.

    Это обходит типичные проблемы macOS:
      • системный python 3.9 из CommandLineTools без рабочего компилятора;
      • старый pip, не знающий --break-system-packages;
      • PEP 668 / externally-managed environment;
      • отсутствие cp39-колёс у новейшего pyobjc (venv + --only-binary
        заставляют pip взять совместимую версию).
    """
    # Авто-bootstrap имеет смысл там, где зависимости ставятся как wheel.
    # На Linux трею нужны системные пакеты (GTK/AppIndicator) — там полагаемся
    # на штатный путь и инструкции в README.
    if SYS not in ("Darwin", "Windows"):
        return
    if _deps_importable():
        return

    tries = int(os.environ.get("CT_BOOTSTRAP", "0") or "0")
    if tries >= 2:
        return  # больше не зацикливаемся — отдадим управление обычному пути

    vdir = _venv_dir()
    vpy = _venv_python(vdir)
    try:
        if not os.path.exists(vpy):
            os.makedirs(os.path.dirname(vdir), exist_ok=True)
            print("Готовлю окружение (это нужно один раз)…")
            subprocess.run([sys.executable, "-m", "venv", vdir], check=True)
        # pip посвежее лучше разбирает --only-binary; ошибки не критичны.
        try:
            subprocess.run([vpy, "-m", "pip", "install", "--upgrade", "pip"],
                           check=False)
        except Exception:
            pass
        pkgs = ["pystray", "Pillow", "certifi"]
        extra = []
        if SYS == "Darwin":
            pkgs += ["pyobjc-framework-Cocoa", "pyobjc-framework-Quartz"]
            extra = ["--only-binary=:all:"]
        print("Устанавливаю зависимости…")
        subprocess.run([vpy, "-m", "pip", "install"] + extra + pkgs, check=True)
    except Exception:
        log_line("bootstrap venv не удался:\n" + traceback.format_exc())
        return  # пусть отработает ensure_tray_deps / сообщение об ошибке

    # Перезапускаемся внутри venv, где все модули уже доступны.
    env = dict(os.environ)
    env["CT_BOOTSTRAP"] = str(tries + 1)
    try:
        os.execve(vpy, [vpy, SCRIPT] + sys.argv[1:], env)
    except Exception:
        log_line("re-exec в venv не удался:\n" + traceback.format_exc())


def ensure_tray_deps():
    try:
        import pystray  # noqa
        from PIL import Image  # noqa
        return True
    except Exception:
        pass
    # На macOS бэкенду pystray нужен pyobjc (AppKit/Foundation/Quartz),
    # иначе значок в menu bar не появляется.
    pkgs = ["pystray", "Pillow", "certifi"]
    extra = []
    if SYS == "Darwin":
        pkgs += ["pyobjc-framework-Cocoa", "pyobjc-framework-Quartz"]
        # Только готовые wheel: системный python 3.9 (CommandLineTools) не может
        # собрать pyobjc-core из исходников ("Cannot locate a working compiler").
        # Флаг заставляет pip выбрать версию pyobjc с готовым wheel (11.1 для cp39)
        # вместо новейшей 12.0, у которой колёс под 3.9 уже нет.
        extra = ["--only-binary=:all:"]
    # --break-system-packages понимает только новый pip (23+); старый pip 21.x
    # падает на нём с "no such option", поэтому держим его отдельной последней
    # попыткой и не прерываем цепочку при ошибке.
    attempts = [
        ["--user"] + extra + pkgs,
        extra + pkgs,
        ["--user", "--break-system-packages"] + extra + pkgs,
    ]
    for args in attempts:
        try:
            subprocess.run([sys.executable, "-m", "pip", "install"] + args, check=True)
            import importlib
            importlib.invalidate_caches()
            import pystray  # noqa
            from PIL import Image  # noqa
            return True
        except Exception:
            continue
    return False


COLORS = {
    "on": (46, 160, 67, 255),
    "off": (120, 120, 120, 255),
    "error": (210, 55, 55, 255),
    "skipped": (40, 110, 220, 255),  # синий — телеметрия сейчас фильтруется
    "waiting": (230, 170, 40, 255),  # жёлтый — включено, но трафика нет
    "leaking": (150, 90, 200, 255),  # фиолетовый — выключено, а данные ещё идут
    "idle": (140, 140, 140, 255),    # серый кружок — этот аккаунт молчит
}


MAX_ACCOUNT_DOTS = 4        # больше кружков в значке 16×16 уже не различить


def account_states():
    """[(почта, состояние)] по каждому замеченному аккаунту.

    Один значок на два аккаунта отвечает сразу на два вопроса и потому не
    отвечает ни на один: пока шла работа одного аккаунта, зелёный цвет выглядел
    как «доставлено» и для сообщений другого."""
    now = time.monotonic()
    out = []
    for email, acc in PROXY_STATE["accounts"].items():
        if now - acc["at"] < STALE_AFTER:
            out.append((email, "on" if acc["last"] == "ok" else "skipped"))
        else:
            out.append((email, "idle"))
    out.sort(key=lambda x: (x[1] == "idle", x[0]))   # активные впереди
    return out[:MAX_ACCOUNT_DOTS]


def _dot_boxes(n):
    """Рамки кружков внутри значка 64×64."""
    if n <= 1:
        return [(6, 6, 58, 58)]
    if n == 2:
        return [(1, 17, 31, 47), (33, 17, 63, 47)]
    if n == 3:
        return [(1, 22, 21, 42), (22, 22, 42, 42), (43, 22, 63, 42)]
    return [(1, 1, 31, 31), (33, 1, 63, 31), (1, 33, 31, 63), (33, 33, 63, 63)]


def make_image(state, accounts=None):
    from PIL import Image, ImageDraw
    img = Image.new("RGBA", (64, 64), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    # Делим значок на кружки только когда речь про сами аккаунты. Выключено,
    # ошибка доставки, тишина — это про канал целиком, там кружок один.
    if accounts and len(accounts) > 1 and state in ("on", "skipped"):
        for box, (_, acc_state) in zip(_dot_boxes(len(accounts)), accounts):
            d.ellipse(box, fill=COLORS.get(acc_state, COLORS["off"]))
    else:
        d.ellipse((6, 6, 58, 58), fill=COLORS.get(state, COLORS["off"]))
    return img


def compute_state():
    """Цвет значка отвечает только на вопрос «доходит ли телеметрия».

    Фильтрация цвет не меняет — это штатная работа, её видно в счётчиках.
    У состояния есть срок годности: один давний успех больше не держит
    значок зелёным вечно."""
    now = time.monotonic()
    st = PROXY_STATE
    recent = lambda mark, seq: seq > 0 and now - mark < STALE_AFTER
    act = st["act"]
    broken = st["err_seq"] > st["ok_seq"]

    if not is_on():
        # «Выключено» обязано означать «ничего не уходит». Но уже запущенные
        # процессы Claude держат старое окружение и продолжают слать, пока их
        # не перезапустят, — про это надо говорить прямо, а не показывать серый.
        return "leaking" if recent(st["last_any"], st["seq"]) else "off"
    if broken:
        return "error"       # ошибка доставки важнее всего остального
    if act["kind"] and now - act["time"] < STALE_AFTER:
        # цвет задаёт последнее настоящее сообщение, а не фоновый экспорт
        return "skipped" if act["kind"] == "filtered" else "on"
    if recent(st["last_ok"], st["ok_seq"]):
        return "on"          # сообщений давно не было, но доставка жива
    return "waiting"


def _fresh_process_sees_sessions():
    """True, если только что запущенный процесс тем же кодом видит сессии.
    Отличает «нас заклинило» от «сессий действительно нет»."""
    try:
        out = subprocess.run(
            [sys.executable, SCRIPT, "--count-sessions"],
            capture_output=True, text=True, timeout=60,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0)).stdout
        return int((out or "0").strip() or "0") > 0
    except Exception:
        return False


def restart_self():
    """Перезапускает трей новым процессом и завершает текущий."""
    try:
        flags = 0
        if SYS == "Windows":
            flags = (getattr(subprocess, "DETACHED_PROCESS", 0)
                     | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0))
        subprocess.Popen([sys.executable, SCRIPT],
                         creationflags=flags, close_fds=True)
    except Exception:
        log_line("АВТО-ПЕРЕЗАПУСК не удался:" + chr(10) + traceback.format_exc())
        return
    time.sleep(1)
    os._exit(0)   # жёстко: наш процесс всё равно деградировал


def tray_main():
    if not ensure_tray_deps():
        if SYS == "Darwin":
            sys.stderr.write(
                "Не удалось установить pystray/Pillow/pyobjc.\n"
                "Установи вручную (только бинарные wheel, без компиляции):\n"
                "  python3 -m pip install --user --only-binary=:all: "
                "pystray Pillow pyobjc-framework-Cocoa pyobjc-framework-Quartz\n")
        else:
            sys.stderr.write("Не удалось установить pystray/Pillow. "
                             "Установи вручную: pip install pystray Pillow\n")
        sys.exit(1)
    import time
    import traceback
    import pystray
    from pystray import Menu, MenuItem

    state = {"value": compute_state(), "settings": None}
    settings_lock = threading.Lock()

    def run_on_main(fn):
        """На macOS все вызовы AppKit (NSStatusItem) обязаны идти из главного
        потока. setup и monitor у pystray работают в фоновых потоках, поэтому
        прямые обращения к значку приводили к тому, что иконка не появлялась.
        Перекидываем работу в главный цикл NSApplication."""
        if SYS == "Darwin":
            try:
                from Foundation import NSOperationQueue
                NSOperationQueue.mainQueue().addOperationWithBlock_(fn)
                return
            except Exception:
                pass
        fn()
    titles = {
        "on": "Claude Telemetry: ВКЛ, доставка идёт",
        "off": "Claude Telemetry: выключено",
        "error": "Claude Telemetry: ошибка доставки!",
        "skipped": "Claude Telemetry: телеметрия фильтруется (аккаунт не в списке)",
        "waiting": "Claude Telemetry: ВКЛ, но доставки давно не было",
        "leaking": "Claude Telemetry: ВЫКЛЮЧЕНО, но данные ещё идут — "
                   "перезапусти Claude",
    }
    labels = {
        "on": "● Телеметрия ВКЛ — доставка идёт",
        "off": "○ Телеметрия выкл",
        "error": "⚠ Доставка НЕ работает",
        "skipped": "◆ Идёт фильтрация: аккаунт не в списке",
        "waiting": "◐ ВКЛ — доставки давно не было",
        "leaking": "◆ ВЫКЛ, но данные ещё идут — перезапусти Claude",
    }

    def notify(icon, msg):
        try:
            icon.notify(msg, "Claude Telemetry")
        except Exception:
            print(msg)

    def update_now(icon):
        st = compute_state()
        state["value"] = st

        def apply():
            try:
                accounts = account_states()
                icon.icon = make_image(st, accounts)
                icon.title = accounts_title(st, accounts, titles)
                icon.update_menu()
            except Exception:
                pass

        run_on_main(apply)

    def check_health(icon):
        """Оповещает, когда своя же работа режется вслепую.

        Сегодняшний сбой (индекс сессий обнулился на обновлении Claude) выглядел
        как «аккаунт молчит»: рабочие пакеты шли, но владельца определить не
        удавалось, подпись оставалась чужой и фильтр их отбрасывал. Молчать про
        это нельзя — телеметрия теряется, а по значку не отличить от простоя."""
        st = PROXY_STATE
        while st["leak_alerts"]:
            notify(icon, st["leak_alerts"].pop(0))
        since = st["blind_since"]
        if since and not st["blind_warned"] and time.monotonic() - since > 120:
            st["blind_warned"] = True
            notify(icon, "Телеметрия режется, но владелец сессии не определяется "
                         "— похоже, обновился Claude или ПК выходил из сна. "
                         "Пробую восстановиться сам.")
        # Авто-перезапуск: индекс сессий пуст дольше 5 минут. Раньше триггером
        # был blind_since, но он сбрасывался на каждой успешной доставке, а в
        # смешанном состоянии (часть пакетов доходит, часть режется вслепую) до
        # 5 минут не доживал. Пустой индекс — сигнал прямее и от доставок не
        # зависит. Сброс кэшей (v3.30) это не чинит: процесс заклинивает так, что
        # даже discovery каталогов возвращает пусто, хотя свежий процесс их видит.
        empty_since = PROXY_STATE["empty_since"]
        if (empty_since and time.monotonic() - empty_since > 300
                and time.monotonic() - PROXY_STATE["started_at"] > 600):
            if _fresh_process_sees_sessions():
                log_line("АВТО-ПЕРЕЗАПУСК: индекс пуст >5 мин, а свежий процесс "
                         "сессии видит — перезапускаюсь")
                restart_self()

    def monitor(icon):
        while True:
            update_now(icon)
            check_health(icon)
            # ждём либо события от прокси, либо таймера: событие даёт мгновенную
            # реакцию на трафик, таймер — срабатывание выдержек и вкл/выкл
            STATE_CHANGED.wait(REFRESH_INTERVAL)
            STATE_CHANGED.clear()

    def act_enable(icon, item):
        cfg = load_config()
        if not cfg.get("token"):
            notify(icon, "Сначала укажи токен в Настройках.")
            subprocess.Popen([sys.executable, SCRIPT, "--settings"])
            return
        ok = elevate_enable(cfg)
        if SYS == "Windows":
            time.sleep(2)
        update_now(icon)
        notify(icon, "Телеметрия включена. Перезапусти терминалы и IDE."
               if ok else "Не удалось включить (отклонены права?).")

    def act_disable(icon, item):
        ok = elevate_disable()
        if SYS == "Windows":
            time.sleep(2)
        update_now(icon)
        notify(icon, "Телеметрия отключена. Перезапусти терминалы и IDE."
               if ok else "Не удалось отключить (отклонены права?).")

    last_click = {"t": 0.0}

    def open_settings(icon):
        with settings_lock:
            p = state.get("settings")
            if p is not None and p.poll() is None:
                return  # окно настроек уже открыто — держим один экземпляр
            try:
                state["settings"] = subprocess.Popen([sys.executable, SCRIPT, "--settings"])
            except Exception as e:
                notify(icon, "Не удалось открыть настройки: %s" % e)

    def act_settings(icon, item):
        open_settings(icon)

    def act_double(icon, item):
        now = time.monotonic()
        if now - last_click["t"] <= 0.5:
            last_click["t"] = 0.0
            open_settings(icon)
        else:
            last_click["t"] = now

    def act_autostart(icon, item):
        try:
            set_autostart(not autostart_enabled())
        except Exception as e:
            notify(icon, "Не удалось изменить автозапуск: %s" % e)
        icon.update_menu()

    def act_monitor(icon, item):
        want = not monitor_enabled()
        if want and not os.path.isfile(monitor_script()):
            notify(icon, "Файл наблюдателя не найден рядом с треем:\n"
                         + monitor_script())
            return
        try:
            set_monitor(want)
        except Exception as e:
            notify(icon, "Не удалось изменить слежку: %s" % e)
            return
        icon.update_menu()
        notify(icon, "Слежка за действиями Claude включена — перезапусти Claude, "
                     "чтобы хук загрузился." if want
               else "Слежка за действиями Claude выключена.")

    def act_status(icon, item):
        notify(icon, status_text() + "\n\nДоставка: " + PROXY_STATE.get("detail", "—"))

    def act_quit(icon, item):
        p = state.get("settings")
        if p is not None and p.poll() is None:
            try:
                p.terminate()
            except Exception:
                pass
        icon.visible = False
        icon.stop()

    menu = Menu(
        MenuItem(lambda i: labels.get(state["value"], "Claude Telemetry"), None, enabled=False),
        Menu.SEPARATOR,
        MenuItem("Включить", act_enable),
        MenuItem("Отключить", act_disable),
        Menu.SEPARATOR,
        MenuItem("Настройки…", act_settings),
        MenuItem("Запуск вместе с системой", act_autostart,
                 checked=lambda i: autostart_enabled()),
        MenuItem("Следить за действиями Claude", act_monitor,
                 checked=lambda i: monitor_enabled()),
        MenuItem("Состояние", act_status),
        MenuItem("Выход", act_quit),
        MenuItem("dblclick-open-settings", act_double, default=True, visible=False),
    )
    icon = pystray.Icon("claude-telemetry", make_image(state["value"]), "Claude Telemetry", menu)

    if SYS == "Darwin":
        # Без явной политики активации процесс-скрипт может стартовать как
        # «Prohibited» — тогда NSStatusItem не показывается. Accessory делает
        # приложение агентом menu bar (и убирает иконку-«ракету» из Dock).
        try:
            import AppKit
            AppKit.NSApplication.sharedApplication().setActivationPolicy_(
                AppKit.NSApplicationActivationPolicyAccessory)
        except Exception:
            log_line("не удалось задать activation policy:\n" + traceback.format_exc())

    def setup(icon):
        # Показ значка тоже трогает AppKit — делаем это в главном потоке.
        run_on_main(lambda: setattr(icon, "visible", True))
        start_proxy()
        # По умолчанию включаем автозапуск при первом старте (однократно;
        # дальше уважаем ручное вкл/выкл через меню).
        try:
            c = load_config()
            if not c.get("autostartDefaulted"):
                if not autostart_enabled():
                    set_autostart(True)
                c["autostartDefaulted"] = True
                save_config(c)
            # Если автозапуск включён — переписываем plist в актуальный формат
            # (с логами/рабочей директорией), эффект — при следующем входе.
            if SYS == "Darwin" and autostart_enabled():
                _write_mac_plist(_mac_plist())
        except Exception:
            log_line("автозапуск по умолчанию не удалось включить:\n"
                     + traceback.format_exc())
        threading.Thread(target=monitor, args=(icon,), daemon=True).start()

    log_line("трей запускается (%s)" % SYS)
    try:
        icon.run(setup)
    except Exception:
        log_line("значок трея недоступен, работаю без значка:\n" + traceback.format_exc())
        try:
            if not PROXY_STATE.get("bound"):
                start_proxy()
        except Exception:
            pass
        try:
            while True:
                time.sleep(3600)
        except KeyboardInterrupt:
            pass
    log_line("трей остановлен")


# ── Точка входа ──────────────────────────────────────────────────────────────
def main():
    args = sys.argv[1:]
    if "--worker-enable" in args:
        p = args[args.index("--worker-enable") + 1]
        with open(p, "r", encoding="utf-8") as f:
            cfg = json.load(f)
        do_enable(cfg)
        try:
            os.remove(p)
        except OSError:
            pass
        return
    if "--worker-disable" in args:
        i = args.index("--worker-disable")
        uf = args[i + 1] if len(args) > i + 1 else user_file()
        do_disable(managed_file(), uf)
        return
    if "--settings" in args:
        run_settings_window()
        return
    if "--count-sessions" in args:
        # проба здоровья: свежий процесс печатает, сколько сессий он видит.
        # Ею пользуется авто-перезапуск, чтобы отличить «процесс заклинило»
        # (свежий видит, а рабочий нет) от «сессий правда нет».
        try:
            print(len(_scan_sessions()))
        except Exception:
            print(-1)
        return
    if "--status" in args:
        print(status_text())
        return
    if "--log" in args:
        print(log_path())
        return
    if "--enable-autostart" in args:
        set_autostart(True)
        print("autostart enabled")
        return
    if "--disable-autostart" in args:
        set_autostart(False)
        print("autostart disabled")
        return
    if "--enable" in args:
        elevate_enable(load_config())
        return
    if "--disable" in args:
        elevate_disable()
        return
    ensure_runtime()
    tray_main()


if __name__ == "__main__":
    try:
        main()
    except SystemExit:
        raise
    except Exception:
        import traceback
        tb = traceback.format_exc()
        try:
            log_line("ОШИБКА запуска:\n" + tb)
        except Exception:
            pass
        sys.stderr.write(tb)
        raise
