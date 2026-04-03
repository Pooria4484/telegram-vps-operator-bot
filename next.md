# Stage Tracker

Last update: 2026-04-03

## Update Rule
- At the end of each stage, update both files:
- `next.md` (next implementation target)
- `current-test.md` (manual test plan for current implemented stage)

## Current Implemented Stage
- Phase 5 stream control (minimal slice) is implemented:
- live PTY-backed `/run`
- one active session per user
- `/status`, `/tail`, `/stop` wired to live session state
- `/ctrl c` for SIGINT to active process group
- `/ctrl d` for EOF to active PTY
- `/n` for Enter/newline to active PTY
- plain text messages are forwarded to active PTY session
- inline session control buttons are added on run start:
- `Stop`, `Ctrl+C`, `Ctrl+D`, `Enter`, `Tail`, `Status`, `Stream`, `Clear Output`
- stale button callbacks are handled safely by session id check
- `/stream <on|off|toggle|status>` (alias: `/live`) is added
- when stream mode is on, live output snapshots are pushed automatically
- persistent reply keyboard keeps controls always available
- `/clear` command clears active session output buffer
- output lines are rendered as per-word code tokens for easy copy
- service runs as `pooria` user
- per-user working directory behavior preserved

## Next Stage Target
- Phase 5 (stream/watch workflows - minimal slice)
- add `/watch <command>` for long-running stream commands
- keep one active watcher per user (reuse current one-session rule)
- send throttled output updates to avoid Telegram spam
- cap output buffer and provide summary when output is too noisy

## Acceptance For Next Stage
- `/watch <command>` supports basic stream use-cases (`tail -f`, `journalctl -f`)
- output updates are throttled and stable under noisy streams
- `/stop`, `/status`, `/tail`, inline `Tail` still work correctly for watcher sessions
- existing `/run` interactive workflow keeps current behavior
- whitelist and per-user workdir behavior stay unchanged
