# Stage Tracker

Last update: 2026-04-03

## Update Rule
- At the end of each stage, update both files:
- `next.md` (next implementation target)
- `current-test.md` (manual test plan for current implemented stage)

## Current Implemented Stage
- Phase 5 + Chat mode (special capability #1) is implemented:
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
- `/chat` mode is added with model config (`CHAT_MODEL`, default `gpt-5.4`)
- chat auth is loaded from `~/.codex/auth.json` (configurable by `CODEX_AUTH_PATH`)
- chat inference is executed via local `codex exec` (uses Codex login/session auth)
- codex binary path is resolved via `CODEX_BIN`, PATH, or `~/.nvm/.../bin/codex`
- codex launch is hardened for service PATH:
- if `codex` is a symlink, launch keeps symlink path and forces sibling `node` when available
- prevents `/usr/bin/node` (old version) from breaking codex with `Unexpected reserved word`
- chat mode keyboard replaces ops keyboard (`/chat new`, `/chat resume`, `/chat usage`, `/chat exit`)
- chat usage limits are tracked for 5-hour and weekly windows with reset timestamps
- `/chat new` starts a new chat thread
- `/chat resume` lists previous chat-mode threads only and can resume by button
- `/chat usage` shows usage and reset dates
- plain text routing priority is fixed:
- if an active PTY session exists, plain text is always sent to PTY
- if no active session exists and chat mode is enabled, plain text goes to Codex chat
- fixed a chat handler syntax regression that caused bot startup failure
- plain text with neither active session nor chat mode now returns an explicit guidance message (no silent drop)
- `/chat` enter now warns when an active shell session exists and clarifies routing priority
- chat mode now streams Codex output live by running `codex exec --json` and editing one progress message
- final assistant reply still uses the last message output and shows usage snapshot
- chat stream has heartbeat updates while waiting for first token
- chat request timeout is enforced (180s) to avoid stuck `Codex stream: ...` state
- parser now accepts `item.completed` agent-message events from codex JSON stream
- fixed chat stream message editing bug: removed unsupported reply keyboard from `edit_text` calls
- chat progress edit now has fallback send (if edit fails, a fresh message is posted)
- added lightweight warning log when stream edit fails for faster production debugging
- chat timeout tuned to 120s for faster fail/feedback
- codex JSON `error` events are surfaced in stream status (reconnect visibility)
- stream callback to Telegram is now non-blocking with a short guard timeout, so UI update delays do not stall codex processing
- heartbeat/update message path now enforces Telegram API timeouts (edit/send) and cannot block the chat handler
- heartbeat now keeps and displays recent Codex reconnect/error status instead of overwriting it
- outer guard timeout now tracks closer to chat timeout (+5s) for faster terminal error delivery
- reconnect fail-fast added: high reconnect state without output aborts early with explicit error (no long hanging wait)
- command runner now builds execution env from the user's login shell profile (`zsh -il`) to inherit terminal-like PATH/HOME/USER
- live `/run` execution uses shell command mode (`-lc`) with login-derived env, so tools like `codex` resolve as in direct terminal usage
- command PATH sanitizer now removes transient Codex sandbox/vendor PATH entries to avoid wrong binary resolution
- terminal output sanitizer now strips ANSI/escape control sequences and handles carriage-return line overwrite for cleaner `/tail`
- `codex --version` and similar codex help/version probes are routed through one-shot non-PTY execution so they finish reliably

## Next Stage Target
- Phase 6 (chat robustness and persistence)
- persist chat threads and usage counters (SQLite) so chat resume survives restart
- add pagination for `/chat resume` when there are many chats
- add safe truncation/chunking for long chat responses to avoid Telegram message limits

## Acceptance For Next Stage
- chat history and usage survive bot restart
- `/chat resume` works with paginated history
- long model outputs are delivered reliably without Telegram send errors
- existing `/run` and stream workflows keep current behavior
- whitelist and per-user workdir behavior stay unchanged
