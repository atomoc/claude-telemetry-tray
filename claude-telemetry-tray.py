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
import sys
import json
import shlex
import shutil
import platform
import tempfile
import threading
import subprocess
import urllib.request
import urllib.error
import http.server
from datetime import datetime

SCRIPT = os.path.abspath(__file__)
SYS = platform.system()
REFRESH_INTERVAL = 5
__version__ = "2.5"

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
    "logUserPrompts": True,
    "logTelemetry": True,   # писать тело отправляемой телеметрии в лог-файл
    "proxyPort": 4318,
    "scope": "user",   # "user" = ~/.claude/settings.json (без админа); "system" = managed (нужен админ)
}

PROXY_STATE = {"ok": None, "detail": "трафика ещё не было", "bound": False}


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
        "OTEL_LOGS_EXPORT_INTERVAL": "5000",
        "OTEL_RESOURCE_ATTRIBUTES": "team.id=" + cfg["teamId"],
    }
    if cfg.get("logUserPrompts"):
        env["OTEL_LOG_USER_PROMPTS"] = "1"
    return env


def do_enable(cfg):
    f = managed_file() if cfg.get("scope") == "system" else user_file()
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
    lines.append("  Режим: " + ("на всю систему (managed)" if cfg.get("scope") == "system" else "только мой пользователь"))
    lines.append("  Фильтр аккаунта: " + (acct if acct else "выкл (отправляется всё)"))
    return "\n".join(lines)


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


def account_matches(account, body_bytes, attrs):
    """True если телеметрия относится к указанному аккаунту."""
    acct = (account or "").strip().lower()
    if not acct:
        return True  # фильтр выключен — пропускаем всё
    for k in ACCOUNT_ATTR_KEYS:
        if k in attrs and acct == attrs[k].strip().lower():
            return True
    # запасной вариант — подстрока в значениях атрибутов или в теле
    if any(acct in v.lower() for v in attrs.values()):
        return True
    try:
        return acct in body_bytes.decode("utf-8", "replace").lower()
    except Exception:
        return False


# ── Локальный прокси-логгер ──────────────────────────────────────────────────
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
            attrs = otlp_attrs(body)

            # Фильтр по аккаунту
            acct = (cfg.get("account") or "").strip()
            if acct and not account_matches(acct, body, attrs):
                if cfg.get("logTelemetry", True):
                    seen = [a + "=" + attrs[a] for a in ACCOUNT_ATTR_KEYS if a in attrs]
                    log_line("ПРОПУЩЕНО %s: аккаунт != '%s' (в телеметрии: %s)"
                             % (self.path, acct, ", ".join(seen) or "не найден"))
                self._reply(200)  # говорим Claude "ок", но НЕ пересылаем
                return

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
                with urllib.request.urlopen(req, timeout=15) as r:
                    code = getattr(r, "status", None) or r.getcode()
                    resp = r.read()
                PROXY_STATE.update(ok=True, detail="сервер ответил HTTP %s" % code)
            except urllib.error.HTTPError as e:
                code = e.code
                try:
                    resp = e.read()
                except Exception:
                    resp = b""
                if code in (401, 403):
                    PROXY_STATE.update(ok=False, detail="HTTP %s — неверный токен" % code)
                elif code == 404:
                    PROXY_STATE.update(ok=False, detail="HTTP 404 — неверный адрес")
                else:
                    PROXY_STATE.update(ok=True, detail="сервер ответил HTTP %s" % code)
            except Exception as e:
                code, resp = 502, b""
                PROXY_STATE.update(ok=False, detail="нет связи с сервером (%s)" % e.__class__.__name__)
                if cfg.get("logTelemetry", True):
                    log_line("ОШИБКА пересылки → %s: %s" % (upstream, e))
            self._reply(code, resp)

        do_POST = _forward

        def log_message(self, *a):
            pass

    return Handler


def start_proxy():
    cfg = load_config()
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


def elevate_enable(cfg):
    if cfg.get("scope") != "system":   # user-scope: без прав админа
        do_enable(cfg)
        return True
    if is_admin():
        do_enable(cfg)
        return True
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
        if on:
            os.makedirs(os.path.dirname(p), exist_ok=True)
            plist = (
                '<?xml version="1.0" encoding="UTF-8"?>\n'
                '<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" '
                '"http://www.apple.com/DTDs/PropertyList-1.0.dtd">\n'
                '<plist version="1.0"><dict>\n'
                '  <key>Label</key><string>io.github.claude-telemetry-tray</string>\n'
                '  <key>ProgramArguments</key><array>'
                '<string>%s</string><string>%s</string></array>\n'
                '  <key>RunAtLoad</key><true/>\n'
                '</dict></plist>\n' % (sys.executable, SCRIPT)
            )
            with open(p, "w", encoding="utf-8") as f:
                f.write(plist)
            subprocess.run(["launchctl", "load", p])
        else:
            subprocess.run(["launchctl", "unload", p])
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
def run_settings_window():
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

    ttk.Label(frm, text="Аккаунт Claude для отправки (email/ID; пусто = слать всё):").pack(anchor="w")
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
                    variable=logtel_var).pack(anchor="w", pady=(0, 4))

    scope_var = tk.BooleanVar(value=(cfg.get("scope") == "system"))
    ttk.Checkbutton(frm, text="Применять на всю систему (нужен админ; пользователь не отключит)",
                    variable=scope_var).pack(anchor="w", pady=(0, 14))

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
            "scope": "system" if scope_var.get() else "user",
        }
        if not new["token"]:
            messagebox.showwarning("Внимание", "Токен не указан.")
            return
        if not new["base"]:
            messagebox.showwarning("Внимание", "Базовый URL не указан.")
            return
        save_config(new)
        if is_on():
            ok = elevate_enable(new)
            if ok:
                messagebox.showinfo("Сохранено",
                                    "Настройки сохранены и применены.\n"
                                    "Перезапусти трей, терминалы и IDE.")
            else:
                messagebox.showwarning("Сохранено",
                                       "Настройки сохранены, но применить не удалось "
                                       "(права отклонены?).\nНажми «Включить» в трее вручную.")
        else:
            messagebox.showinfo("Сохранено",
                                "Настройки сохранены.\nНажми «Включить» в трее, чтобы применить.")
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
        if "scope" in data:
            scope_var.set(data.get("scope") == "system")
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
            "scope": "system" if scope_var.get() else "user",
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
def ensure_tray_deps():
    try:
        import pystray  # noqa
        from PIL import Image  # noqa
        return True
    except Exception:
        pass
    for args in (["--user", "pystray", "Pillow"],
                 ["--user", "--break-system-packages", "pystray", "Pillow"]):
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
}


def make_image(state):
    from PIL import Image, ImageDraw
    img = Image.new("RGBA", (64, 64), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    d.ellipse((6, 6, 58, 58), fill=COLORS.get(state, COLORS["off"]))
    return img


def compute_state():
    if not is_on():
        return "off"
    if PROXY_STATE.get("ok") is False:
        return "error"
    return "on"


def tray_main():
    if not ensure_tray_deps():
        sys.stderr.write("Не удалось установить pystray/Pillow. "
                         "Установи вручную: pip install pystray Pillow\n")
        sys.exit(1)
    import time
    import traceback
    import pystray
    from pystray import Menu, MenuItem

    state = {"value": compute_state(), "settings": None}
    settings_lock = threading.Lock()
    titles = {
        "on": "Claude Telemetry: ВКЛ, доставка идёт",
        "off": "Claude Telemetry: выключено",
        "error": "Claude Telemetry: ошибка доставки!",
    }
    labels = {
        "on": "● Телеметрия ВКЛ — доставка идёт",
        "off": "○ Телеметрия выкл",
        "error": "⚠ Доставка НЕ работает",
    }

    def notify(icon, msg):
        try:
            icon.notify(msg, "Claude Telemetry")
        except Exception:
            print(msg)

    def update_now(icon):
        st = compute_state()
        state["value"] = st
        try:
            icon.icon = make_image(st)
            icon.title = titles.get(st, "Claude Telemetry")
            icon.update_menu()
        except Exception:
            pass

    def monitor(icon):
        while True:
            update_now(icon)
            time.sleep(REFRESH_INTERVAL)

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
        MenuItem("Состояние", act_status),
        MenuItem("Выход", act_quit),
        MenuItem("dblclick-open-settings", act_double, default=True, visible=False),
    )
    icon = pystray.Icon("claude-telemetry", make_image(state["value"]), "Claude Telemetry", menu)

    def setup(icon):
        icon.visible = True
        start_proxy()
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
