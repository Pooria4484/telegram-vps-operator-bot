# Telegram VPS Operator Bot

A Telegram bot for running shell operations on a VPS for a **small trusted whitelist** of Telegram users.

This project focuses on practical VPS workflows:
- run commands
- keep per-user working directory
- manage a live PTY session
- stream/tail output
- upload and download files
- show SHA256 for upload/download integrity

## Key Features

- Whitelist access control via `ALLOWED_USER_IDS`
- Per-user working directory persistence
- Live PTY sessions (one active session per user)
- Session controls: stop, Ctrl+C, Ctrl+D, Enter
- Output tail buffer with ANSI/control-sequence sanitization
- Optional live stream output mode (`/stream` or `/live`)
- File upload to current working directory with overwrite confirmation
- Upload filename sanitization and size limit enforcement
- File download with `/get <path>`
- SHA256 hash in upload/download responses
- Command suggestions on `/` via Telegram bot command menu
- Quick action keyboard (non-persistent)
- Context inline controls (`Help`, `Status`, `Tail`) on usage/error messages
- Baseline operational logging (startup/session/upload/get)

## Commands

- `/help` Show bilingual help in bot
- `/id` Show your Telegram user ID
- `/run <command>` Run a command
- `/run cd <path>` Change your current working directory
- `/status` Show active session status
- `/tail` Show recent output (or latest session output)
- `/stop` Stop active session
- `/ctrl c` Send Ctrl+C to active process group
- `/ctrl d` Send Ctrl+D (EOF) to active PTY
- `/n` Send Enter/newline to active PTY
- `/clear` Clear active session output buffer
- `/stream on|off|toggle|status` Control stream mode
- `/live ...` Alias for `/stream ...`
- `/get <path>` Download file from VPS

## Quick Action Keyboard

Current quick actions shown in chat keyboard:
- `Status`
- `Tail`
- `Stop`
- `Ctrl+C`
- `Enter`
- `Clear`
- `Stream`
- `Help`

## Interactive Shell Examples

- Start Bash session:
  - `/run bash`
  - then send plain text: `pwd`
  - exit with `/ctrl d` or `/stop`

- Start Zsh session:
  - `/run zsh`
  - then send plain text: `whoami`
  - exit with `/ctrl d` or `/stop`

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
MAX_UPLOAD_BYTES=20971520
LOG_LEVEL=INFO
```

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
