# Claude Telemetry Tray

A tiny, cross-platform **system-tray app** that turns Claude Code's OpenTelemetry
output into something you can actually see and control — **without** running a full
OpenTelemetry Collector, Prometheus/Grafana stack, or any server-side infrastructure.

It runs a local proxy on `127.0.0.1`, points Claude Code's telemetry at it, then:

- **logs the real outgoing telemetry** to a local file (the exact payloads Claude Code sends),
- **filters by Claude account** — only telemetry belonging to a chosen account is forwarded,
- **forwards** the allowed telemetry to your own collector endpoint,
- **fixes the account signature** when several accounts are signed in — Claude Code stamps
  telemetry with the account from `~/.claude.json`, not with the account of the window you
  actually work in,
- shows a **tray icon** reflecting real delivery status.

> One Python file. No Docker, no Collector, no cloud. Works on Windows, macOS and Linux.

---

## Why this exists

Claude Code (and Cowork, which runs on top of it) can export OpenTelemetry metrics and
logs via standard `OTEL_*` environment variables, configured through **managed settings**
that users can't override. Most existing tooling around this assumes a heavyweight
observability pipeline (an OTel Collector forwarding to Prometheus/Loki/Grafana, or a SaaS
backend like Datadog/Honeycomb).

This project is the opposite: a **single-file desktop utility** for people who just want to

- turn Claude Code telemetry on/off from a tray menu,
- **see exactly what is being sent** in a plain log file,
- restrict sending to a specific account,
- and forward it to one endpoint of their choice.

## Features

- 🖥️ **System-tray UI** (Windows / macOS / Linux) — enable/disable, settings, status.
- 🔎 **Leak & agent watch** — every forwarded body is scanned for secrets (private keys,
  tokens, passwords) and flagged, never blocked. Enable **Следить за действиями Claude** in
  the tray to also register a Claude Code hook (`claude-agent-monitor.py`) that logs
  suspicious tool use — reads of `.ssh`/`.env`/credential stores, `curl … | sh`,
  `powershell -enc`, exfiltration shapes — to `security-monitor.log` and the tray. Both
  layers only log and notify. See [monitoring](#monitoring--leak-detection).
- 🔌 **Local logging proxy** — telemetry always flows through `127.0.0.1:<port>`; the proxy
  records each real request body and forwards it upstream.
- 🧾 **Plain-text log** of everything actually sent (with rotation).
- 👤 **Account filter** — reads `user.email` / `user.account_uuid` / `user.id` /
  `organization.id` from the OTLP payload and forwards **only** the configured account's
  telemetry. Switch Claude accounts and telemetry stops automatically.
- 🪪 **Correct account with several accounts signed in** — Claude Code takes the identity
  from `~/.claude.json`, so with two accounts every packet carries the same wrong one and the
  filter drops exactly what it should keep. The proxy resolves the real owner of the session
  from the app's own on-disk layout and rewrites `user.email`, `user.account_uuid` and
  `organization.id` before forwarding. Every substitution is written to the log.
- 🔵🔴🟢 **Live status icon** — red = delivery is failing (bad token / wrong URL / no
  connection), blue = telemetry is being filtered right now, green = delivering, yellow =
  on, but nothing has arrived for five minutes, purple = switched off while running processes
  still send, grey = off and silent. Background exports change neither the colour nor the
  counters — only real messages do. Derived from **real** responses, no synthetic probes.
- 🚀 **One-click install** — self-extracting installers per OS, with login auto-start.
- 🔒 Writes Claude Code **managed settings** (system-wide, admin-elevated) so the config
  can't be overridden by individual users.

## How it works

```
Claude Code ──OTLP/HTTP──> 127.0.0.1:<port> (this app's proxy)
                                   │
                                   ├─ write request body to claude-telemetry.log   (optional)
                                   ├─ account filter: keep only configured account
                                   └─ forward ──> your collector (base URL + token)
                                              <── response ──> back to Claude Code
```

The app sets these (via managed settings, so they apply to every user on the machine and
can't be disabled by them):

```
CLAUDE_CODE_ENABLE_TELEMETRY=1
OTEL_METRICS_EXPORTER=otlp
OTEL_LOGS_EXPORTER=otlp
OTEL_EXPORTER_OTLP_PROTOCOL=http/json
OTEL_EXPORTER_OTLP_METRICS_ENDPOINT=http://127.0.0.1:<port>/metrics
OTEL_EXPORTER_OTLP_LOGS_ENDPOINT=http://127.0.0.1:<port>/logs
OTEL_EXPORTER_OTLP_HEADERS=Authorization=Bearer <token>
OTEL_RESOURCE_ATTRIBUTES=team.id=<teamId>
# OTEL_LOG_USER_PROMPTS=1   (only if "log prompts" is enabled)
```

> Because telemetry always goes through the local proxy, **the tray app must be running**
> for telemetry to be delivered. The installers enable login auto-start for you.

## Install on macOS (one file)

Download **`claude-telemetry-macos.command`** from the latest [GitHub Release](https://github.com/atomoc/claude-telemetry-tray/releases) and run it (the first time: right-click → **Open**, because files from the internet are quarantined by Gatekeeper). That single file contains the whole app; on launch it:

- unpacks the script into `~/Library/Application Support/claude-telemetry/`,
- creates a private virtualenv and installs dependencies as prebuilt wheels,
- starts the menu-bar icon and closes the Terminal window,
- enables **start-at-login** automatically.

Then open **Settings…** from the tray icon, enter your token and collector URL, **Save**, and **Enable** (asks for the admin password once). Restart Claude Code / your terminals so it picks up the managed settings.

To update: download the newer `.command` from Releases and run it again.

Maintainers regenerate this file with `python3 build-macos-command.py` (outputs `dist/claude-telemetry-macos.command`) and attach it to the Release.

## macOS notes

**Zero-setup launch.** Just double-click **`start-macos.command`** (or run `python3 claude-telemetry-tray.py`). On first run the app makes itself self-contained: it creates a private virtual environment under `~/Library/Application Support/claude-telemetry/venv`, installs its dependencies there **as prebuilt wheels** (no compiler needed), and re-launches itself inside that environment. Later runs start instantly.

This deliberately avoids the common failure modes of the stock `/Library/Developer/CommandLineTools` Python 3.9: no working C compiler (so `pyobjc-core` can't build from source), an old `pip` that doesn't know `--break-system-packages`, PEP 668 "externally managed" environments, and the fact that the newest pyobjc no longer ships cp39 wheels (the venv + `--only-binary=:all:` make pip pick a compatible 11.x build automatically).

On macOS the tray lives in the **menu bar**. The app now:

- installs the required **pyobjc** frameworks (`pyobjc-framework-Cocoa`, `pyobjc-framework-Quartz`) automatically — pystray's macOS backend needs them or the icon never appears;
- sets the process to **accessory** activation policy, so the menu-bar icon is allowed to show and no Python "rocket" appears in the Dock;
- performs all status-item updates on the **main thread** (pystray runs the setup and refresh callbacks in background threads, and AppKit must be touched only from the main thread — otherwise the icon silently fails to render).

The dependency install uses `--only-binary=:all:` on macOS so pip downloads ready-made **wheels** instead of compiling `pyobjc-core` (the stock `/Library/Developer/CommandLineTools` Python 3.9 has no working compiler, and pyobjc 12.0 no longer ships cp39 wheels — pip automatically falls back to 11.1).

If you hit a build error like *"Cannot locate a working compiler"* on an older Python, install the deps manually:

```bash
python3 -m pip install --user --only-binary=:all: \
  pystray Pillow pyobjc-framework-Cocoa pyobjc-framework-Quartz
```

**Settings window is native.** macOS' built-in Tk (8.5, shipped with the CommandLineTools Python) renders blank windows, so the Settings panel is drawn with native AppKit (pyobjc) instead. If that ever fails, the app falls back to opening `config.json` in your default editor.

If the icon still doesn't show, check the log (`python3 claude-telemetry-tray.py --log`) and make sure no other menu-bar item is hiding it behind the notch.

## Requirements

- **Python 3.8+** (on Windows tick *"Add python.exe to PATH"* during install).
- `pystray` and `Pillow` — installed automatically on first run.
- A GUI toolkit for the settings window (**Tkinter**) and a tray backend. The installers set
  these up automatically — on Debian/Ubuntu they `apt install` `python3-tk`, `python3-gi`,
  `gir1.2-ayatanaappindicator3-0.1` and the GNOME AppIndicator extension. On GNOME you may
  need to log out/in once for the tray icon to appear.

## Install

### Option A — clone and run (recommended for developers)

Cloning with Git does **not** apply the OS "downloaded from the Internet" mark, so nothing
gets blocked.

```bash
git clone https://github.com/atomoc/claude-telemetry-tray.git
cd claude-telemetry-tray
python3 claude-telemetry-tray.py        # macOS/Linux
# Windows: double-click start-tray-windows.bat
```

### Option B — self-extracting installers (for non-technical teammates)

Build them once (see [Building installers](#building-installers)), then share **one file**
per OS. Each installer unpacks locally (so files are never flagged by the OS), installs
dependencies, enables login auto-start, and launches the tray:

- **Windows:** `install-claude-telemetry.bat` (double-click)
- **macOS:** `install-claude-telemetry.command` (double-click; if it won't open,
  right-click → Open once)
- **Linux:** `install-claude-telemetry.sh` (`bash install-claude-telemetry.sh`)

## Usage

Right-click (or click) the tray icon:

| Menu item | What it does |
|---|---|
| **Enable / Disable** | Writes/clears Claude Code managed settings (asks for admin) |
| **Settings…** | Token, collector URL, **account/domain filter**, Team ID, proxy port, logging, Import/Export |
| **Start at login** | Toggle auto-start |
| **Status** | Current state + last delivery result |
| **Quit** | Exit |

After enabling/disabling or changing settings, **fully restart** your terminals and IDE —
Claude Code reads settings only at process start.

### CLI

```
python3 claude-telemetry-tray.py --status            # print current state
python3 claude-telemetry-tray.py --settings          # open settings window
python3 claude-telemetry-tray.py --enable|--disable   # write/clear managed settings (admin)
python3 claude-telemetry-tray.py --enable-autostart   # add login auto-start
python3 claude-telemetry-tray.py --log                # print log file path
```

## Account / domain filter

The **Account** field forwards only telemetry belonging to the accounts/domains you list
(comma- or space-separated); leave it empty to forward everything. Each entry can be:

- an exact email — `alice@corp.com`
- a domain — `gmail.com` or `@gmail.com` (matches every `*@gmail.com`)
- an account id — a `user.id` / `user.account_uuid` / `organization.id` value

The matcher reads `user.email` and the id attributes from the OTLP payload. When a
non-matching account's telemetry arrives, the proxy replies `200 OK` to Claude Code (so it
doesn't retry) but **does not forward** it, writes a skip line to the log, and the tray icon
turns **blue** while filtering is going on. A successful background export does not clear
the blue: otherwise the icon would flicker with every periodic ping and you could never catch
the filter at work. A packet whose owner could not be established is judged by the signature it
arrived with — it is never forwarded "just in case".

> Note: telemetry is enabled through Claude Code **managed settings** (system-wide, requires
> admin once) — per-user settings do not enable Claude Code telemetry, so this is the only mode.

## Monitoring & leak detection

Two independent layers, both **log and notify only — nothing is ever blocked**, so no
legitimate work is lost to a false positive.

**1. Secrets in outgoing telemetry (built into the tray).** Before forwarding any body to
your collector, the proxy scans it for secret shapes: private/ssh keys, AWS/GitHub/Slack/
OpenAI tokens, Bearer tokens, JWTs, `password=`/`secret=` assignments. A match is written to
the telemetry log as `ВОЗМОЖНАЯ УТЕЧКА` and raised as a tray notification. Only a category
and a hash are recorded — never the secret itself — and findings are deduplicated. Always on.

**2. Agent actions (opt-in Claude Code hook).** Turn on **Следить за действиями Claude** in
the tray menu: it registers `claude-agent-monitor.py` as a `PreToolUse` hook in
`~/.claude/settings.json` (your other settings are left untouched). Before each tool call the
hook flags — without blocking — reads/edits of sensitive paths (`.ssh`, `.aws`, `.env`, keys,
browser credential stores, `/etc/shadow`, registry hives, wallets) and shell commands with
exfiltration or evasion shapes (`curl … | sh`, `base64 -d | sh`, `/dev/tcp`, netcat,
`powershell -enc`, `certutil` download, `git remote add`, `scp`/`rsync` to an external host,
reaching a non-local URL). Findings go to `security-monitor.log` (next to the telemetry log)
and, for the serious ones, to the tray. Commit messages, heredoc bodies, search-tool patterns
and work on the monitor's own repo are stripped to keep the noise down. The hook takes effect
**after the next Claude restart**; a heartbeat file `agent-monitor-lastfired.txt` lets you
confirm it is actually firing (the desktop agent does honor user hooks, but it is worth
checking).

**Scope, honestly:** the tray only sees Claude's OpenTelemetry export, and the hook only sees
the agent's tool calls. Neither sees Claude's API traffic, MCP traffic, or any other network
egress — that would need a separate network layer.

## File locations

| | Config (token/account) | Telemetry log | Claude Code managed settings (admin) |
|---|---|---|---|
| **Windows** | `%APPDATA%\claude-telemetry\config.json` | `%APPDATA%\claude-telemetry\claude-telemetry.log` | `%ProgramFiles%\ClaudeCode\managed-settings.json` |
| **macOS** | `~/Library/Application Support/claude-telemetry/config.json` | same dir / `claude-telemetry.log` | `/Library/Application Support/ClaudeCode/managed-settings.json` |
| **Linux** | `~/.config/claude-telemetry/config.json` | same dir / `claude-telemetry.log` | `/etc/claude-code/managed-settings.json` |

## Building installers

`build-installers.py` zips the run files and produces the three self-extracting installers
(`.bat`, `.command`, `.sh`) that unpack locally without triggering OS quarantine:

```bash
python3 build-installers.py
# outputs: dist/install-claude-telemetry.{bat,command,sh} and dist/claude-telemetry.zip
```

## Privacy & security notes

- **Prompt logging is sensitive.** Enabling "log prompts" sets `OTEL_LOG_USER_PROMPTS=1`,
  which makes Claude Code include prompt text in telemetry — which this proxy will then log
  to disk and forward. Off by default in the UI's intent; treat it as personal data.
- **Managed settings are machine-wide and not user-overridable.** Enabling requires admin
  rights and affects every OS user on the machine. Use responsibly and with consent.
- The token is stored in your per-user `config.json`, **not** in source code.

## Comparison to related projects

| Project | Approach |
|---|---|
| this project | Single-file desktop tray + local logging/forwarding proxy + account filter |
| [TechNickAI/claude_telemetry](https://github.com/TechNickAI/claude_telemetry) | CLI wrapper (`claude`→`claudia`) exporting to Logfire/Sentry/Honeycomb/Datadog |
| [ColeMurray/claude-code-otel](https://github.com/ColeMurray/claude-code-otel) | Full OTel Collector → Prometheus/Loki/Grafana stack |
| [DEVtheOPS/opencode-plugin-otel](https://github.com/DEVtheOPS/opencode-plugin-otel) | OTLP exporter plugin for *opencode* |

## Roadmap

- English UI / i18n (the current UI strings are in Russian).
- Optional packaged binaries (PyInstaller) for a no-Python install.
- Per-account log files.

## License

MIT — see [LICENSE](LICENSE).

## Disclaimer

Not affiliated with or endorsed by Anthropic. "Claude" and "Claude Code" are trademarks of
their respective owner. This is an independent, unofficial utility.
