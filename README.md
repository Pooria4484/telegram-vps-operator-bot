# Telegram VPS Operator Bot

A Telegram bot for running shell operations on a VPS for a **small trusted whitelist** of Telegram users.

This project focuses on practical VPS workflows:
- run commands
- keep per-user working directory
- manage a live PTY session
- stream/tail output
- start shell sessions quickly from Telegram UI
- upload and download files
- show SHA256 for upload/download integrity

## Key Features

- Whitelist access control via `ALLOWED_USER_IDS`
- Per-user working directory persistence
- Per-user preference persistence (for example stream mode on/off)
- Live PTY sessions (one active session per user)
- Session controls: stop, Ctrl+C, Ctrl+D, Enter
- Output tail buffer with ANSI/control-sequence sanitization
- Optional live stream output mode (`/stream` or `/live`)
- Quick shell launch actions for `bash` and `zsh`
- Smart monospace output rendering with copy-friendly units for history, paths, URLs, key/value lines, and stream frames
- File upload to current working directory with overwrite confirmation
- Upload filename sanitization and size limit enforcement
- File download with `/get <path>`
- SHA256 hash in upload/download responses
- Command suggestions on `/` via Telegram bot command menu
- Persistent quick action keyboard
- Context inline controls (`Help`, `Status`, `Tail`) on usage/error messages
- Baseline operational logging (startup/session/upload/get)

## Commands

- `/help` Show bilingual help in bot
- `/id` Show your Telegram user ID
- `/run <command>` Run a command
- `/run cd <path>` Change your current working directory
- `/sessions [page]` List your sessions (with pagination)
- `/attach [session_id|suffix]` Attach to a running detached session
- `/detach` Detach current session without stopping it
- `/status [session_id|suffix]` Show active/target session status
- `/tail [session_id|suffix]` Show recent output (or latest session output)
- `/stop [session_id|suffix]` Stop active/target session
- `/kill [session_id|suffix]` Force kill with two-step confirmation
- `/ctrl c` Send Ctrl+C to active process group
- `/ctrl d` Send Ctrl+D (EOF) to active PTY
- `/n` Send Enter/newline to active PTY
- `/stream on|off|toggle|status` Control stream mode
- `/live ...` Alias for `/stream ...`
- `/get <path>` Download file from VPS
- `/codex <task>` Run Codex non-interactively in the current workspace

Buffer behavior:
- In stream `on`, buffer is cleared before each new interactive input.
- In stream `off`, `/tail` shows and consumes (clears) the shown buffer.
- Stream mode is persisted per user in SQLite until the user changes it.
- Live stream output uses rolling frames; control buttons stay on the latest live frame only.
- Long output is chunked by both message length and Telegram formatting budget.
- Live frames roll over before formatting degrades, so large outputs keep their copy-friendly monospace rendering.

Output rendering behavior:
- History-like lines render as `index` + full command, so the command can be copied in one tap.
- Paths, URLs, proxy links, hashes, UUIDs, and similar standalone values render as one copy unit.
- `key=value` and `key: value` lines render as key + value units instead of word-by-word.
- Table-like and log-like lines keep readable structured monospace output.

Codex behavior:
- `/codex <task>` runs Codex non-interactively in the user's current working directory.
- `Codex` on the reply keyboard opens a workspace panel and is the preferred UX.
- The panel defaults to the directory where the user opened Codex.
- From the panel, the user can:
  - start by sending task text directly
  - toggle continue/new mode
  - choose model from a button list
  - choose reasoning effort (`low`/`medium`/`high`, configurable)
  - change directory
  - inspect status/session
  - fetch latest logs/retry/changes/files/patch
  - cancel the active run
  - end the current Codex session
- If there is an active Codex session and no active shell/PTTY session, plain text continues the Codex chat flow automatically.
- Shell/PTTY sessions still take precedence for plain text routing, so interactive shell behavior is preserved.
- Codex also provides post-run inline actions (`Retry`, `Logs`, `Changes`, `Files`, `Export Patch`) under result messages.
- Each Codex run stores its selected model in SQLite, so status/log views remain accurate after model changes.
- Each Codex run also stores selected reasoning effort in SQLite.
- Default model list (when env is unset): `gpt-5.4`, `gpt-5.4-mini`, `gpt-5.3-codex`, `gpt-5.2`.
- Default effort list (when env is unset): `low`, `medium`, `high`, `xhigh`.

## Quick Action Keyboard

Current quick actions shown in chat keyboard:
- `Status`
- `Tail`
- `Sessions`
- `Stop`
- `Detach`
- `Ctrl+C`
- `Ctrl+D`
- `Enter`
- `Stream`
- `Help`
- `Open Shell`
- `Codex`

Quick shell actions:
- `Open Shell` opens an inline picker for `zsh` and `bash`.
- `/start` also shows shell start actions when you want to open a session quickly.
- `/sessions` includes `New zsh` and `New bash` actions for creating another session from the sessions view.

## Interactive Shell Examples

- Start Bash session:
  - `/run bash`
  - then send plain text: `pwd`
  - exit with `/ctrl d` or `/stop`

- Start Zsh session:
  - `/run zsh`
  - then send plain text: `whoami`
  - exit with `/ctrl d` or `/stop`

- Start shell from Telegram UI:
  - tap `Open Shell`
  - choose `zsh` or `bash`
  - then send plain text commands such as `pwd`

- Codex session note:
  - if you start `codex` by mistake, use `Ctrl+D`, `Stop`, or `Kill` from the bot controls to leave it

## Installation

1. Clone and enter project:

```bash
git clone <your-repo-url>
cd tg_vps_bot
```

2. Create virtualenv and install dependencies:

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

3. Create `.env`:

```env
BOT_TOKEN=YOUR_TELEGRAM_BOT_TOKEN
ALLOWED_USER_IDS=123456789,987654321
DEFAULT_SHELL=/bin/bash
WORKDIR=/home/your-user
MAX_TAIL_LINES=30
MAX_UPLOAD_BYTES=1073741824
TELEGRAM_API_BASE_URL=
TELEGRAM_API_IS_LOCAL=0
TELEGRAM_API_FILE_LIMIT_BYTES=20971520
MAX_RUNNING_SESSIONS_PER_USER=3
MAX_SESSION_HISTORY_PER_USER=20
SESSIONS_PAGE_SIZE=5
DETACHED_SESSION_TTL_SECONDS=3600
DETACHED_SWEEP_INTERVAL_SECONDS=30
TIME_OFFSET=+03:30
SESSION_DB_PATH=./session_store.sqlite3
LOG_LEVEL=INFO
```

### Upload Size Reality (`MAX_UPLOAD_BYTES` vs Telegram API)

- `MAX_UPLOAD_BYTES` is the bot-side policy limit.
- Telegram public Bot API has its own file size limit (commonly much lower than 1 GB).
- To actually allow large uploads (for example 1 GB), run a Local Telegram Bot API server and set:
  - `TELEGRAM_API_BASE_URL=http://127.0.0.1:8081`
  - `TELEGRAM_API_IS_LOCAL=1`
  - `TELEGRAM_API_FILE_LIMIT_BYTES=1073741824` (or up to your local Bot API capacity)

### Time Offset (`TIME_OFFSET`)

- `TIME_OFFSET` is applied to bot timestamps and session times saved in SQLite.
- It should represent your local timezone difference from UTC in `+HH:MM` or `-HH:MM` format.
- Example for Tehran: `TIME_OFFSET=+03:30`.
- If your server timezone differs from your desired display/storage timezone, set this value explicitly.
- Wrong value can make `started_at`, `ended_at`, and `runtime` appear inconsistent.

4. Run:

```bash
python -m app.main
```

## systemd Service (Optional)

A sample unit file exists at `systemd/tg-vps-bot.service`.

Typical flow:

```bash
sudo cp systemd/tg-vps-bot.service /etc/systemd/system/tg-vps-bot.service
sudo systemctl daemon-reload
sudo systemctl enable --now tg-vps-bot.service
sudo systemctl status tg-vps-bot.service
```

## Security Notes

- This bot is designed for a **small trusted whitelist**.
- Do not remove whitelist checks.
- Never commit real secrets (`.env`, bot token) to git.
- Keep the bot process under a non-root user when possible.

## Tech Stack

- Python 3
- aiogram 3
- python-dotenv
- Native PTY/process APIs (`asyncio`, `pty`, `os`, `signal`)

## Project Structure

- `app/main.py` entry point
- `app/bot.py` command handlers and Telegram UX
- `app/command_runner.py` process and PTY lifecycle
- `app/session_manager.py` session state and output buffering
- `app/config.py` environment configuration
- `systemd/tg-vps-bot.service` service unit sample
