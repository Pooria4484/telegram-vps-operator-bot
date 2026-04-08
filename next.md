# Stage Tracker

Last update: 2026-04-08

## Update Rule
- Update these files only when explicitly requested by operator:
- `next.md` (next implementation target)
- `current-test.md` (manual test plan for current implemented stage)

## Current Implemented Stage
- Phase 5 core (without chat mode) is implemented:
- whitelist-based access control (`ALLOWED_USER_IDS`)
- live PTY-backed `/run` (one active session per user)
- `/run cd <path>` updates per-user current working directory
- `/status`, `/tail`, `/stop` wired to live session state
- `/ctrl c` for SIGINT to active process group
- `/ctrl d` for EOF to active PTY
- `/n` for Enter/newline to active PTY
- plain text messages are forwarded to active PTY session when active
- inline session control buttons on run start:
- `Stop`, `Ctrl+C`, `Ctrl+D`, `Enter`, `Tail`, `Status`, `Stream`, `Clear Output`
- stale button callbacks are guarded by session id checks
- `/stream <on|off|toggle|status>` (alias: `/live`) controls live output snapshots
- `/clear` clears active session output buffer
- output is sanitized from ANSI/control sequences for clean tail display
- upload/download workflows are active:
- file upload saved into current working directory (with overwrite confirmation)
- file download via `/get <path>` (relative to current directory)
- SHA256 is shown for upload/download integrity
- upload filename is sanitized to stay within current working directory

## Next Stage Target
- Phase 6 robustness and persistence (non-chat):
- persist session metadata (SQLite)
- improve restart recovery behavior for active/ended sessions
- add explicit upload size limits and clearer user-facing upload errors
- add safer long-output handling (chunk/truncation for Telegram limits)

## Acceptance For Next Stage
- session metadata survives bot restart
- bot recovers cleanly without broken active-session pointers
- oversize uploads fail with explicit user-facing message
- long outputs are delivered safely without Telegram send/edit failures
- existing `/run`, stream, file transfer, whitelist, and per-user workdir behavior stay unchanged
