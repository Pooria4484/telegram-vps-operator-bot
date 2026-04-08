# AGENTS.md

## Project overview

This project is a Telegram-based VPS operations bot for a **small whitelist of trusted Telegram users**.

The bot runs on the VPS itself and provides:

- shell command execution
- per-user working directory tracking
- file download from the VPS using `/get <path>`
- file upload into the user's current working directory
- file integrity reporting using SHA256
- a roadmap toward interactive PTY-based sessions with live controls such as stop, Ctrl+C, Ctrl+D, Enter, and stream-oriented workflows

The project is being built **incrementally**. Do not refactor the whole codebase unless the current task explicitly requires it.

---

## Current implemented behavior

At the current stage, the bot already supports:

- Telegram user whitelist via `ALLOWED_USER_IDS`
- basic bot startup and command routing
- `/id`
- `/run <command>` with live PTY-backed execution
- persistent per-user working directory
- `cd` handling through `/run cd ...`
- `/status`
- `/tail`
- `/stop`
- `/ctrl c`, `/ctrl d`, `/n`
- plain-text routing into active PTY session
- `/stream <on|off|toggle|status>` (`/live` alias)
- `/clear` output buffer
- `/get <path>`
- direct file upload into current working directory
- overwrite confirmation for duplicate uploaded files
- stale-safe overwrite/cancel upload callbacks
- upload filename sanitization
- upload size-limit enforcement via `MAX_UPLOAD_BYTES`
- SHA256 reporting for file send and upload
- quick action keyboard in chat
- inline session control menus and context inline helper controls
- Telegram slash command suggestions via `setMyCommands`
- baseline operational logging with configurable `LOG_LEVEL`

---

## Primary roadmap

Work through the following roadmap in order unless the user explicitly changes priorities.

### Phase 1 - Foundation
Completed or mostly completed:

- whitelist-based access control
- config loading from `.env`
- basic command execution
- working directory persistence per Telegram user
- file transfer helpers
- SHA256 reporting

### Phase 2 - Live execution core
Completed:

- replace simple command execution with **PTY-based live sessions**
- keep one active session per Telegram user
- allow long-running commands to remain active
- maintain live output tail buffers
- add `/stop`
- make `/status` meaningful for live sessions

### Phase 3 - Interactive controls
Completed:

- `/ctrl c`
- `/ctrl d`
- `/n` for Enter/newline
- optional plain-text input routing to the active attached session
- clean session state transitions

### Phase 4 - Telegram UX controls
Mostly completed:

- inline buttons for Stop / Kill / Ctrl+C / Ctrl+D / Enter / Status
- separate control message and output message
- message editing for status and live output
- safer handling of stale buttons

### Phase 5 - Stream/watch workflows
After live PTY is stable:

- `tail -f`
- `journalctl -f`
- `docker logs -f`
- `tcpdump`-style streaming with throttled UI updates
- summary mode or capped tail mode for noisy streams

### Phase 6 - Robustness and persistence
In progress:

- SQLite-backed session metadata
- transcript persistence
- better recovery and audit trail
- upload size limits and explicit user-facing error messages (implemented)
- file overwrite policies and conflict handling improvements

---

## Architectural rules

### 1. Keep the implementation incremental
Do not jump ahead and implement future phases unless the task explicitly asks for it.

### 2. One active session per Telegram user
The design assumes each Telegram user can have at most one active live session at a time.

### 3. Preserve per-user working directory
The bot must maintain a separate current working directory for each allowed Telegram user.

### 4. Prefer minimal, local changes
When making changes, prefer targeted edits over broad rewrites.

### 5. Avoid unnecessary abstractions
This project should stay pragmatic and debuggable.

### 6. File behavior must stay predictable
- `/get <path>` should resolve relative paths against the user's current working directory.
- uploaded files should be saved to the user's current working directory.
- duplicate uploads should require explicit confirmation before overwrite.

### 7. Security assumptions
This bot is intentionally designed for a **small trusted whitelist**, but changes should still avoid careless behavior.

Do not silently broaden access. Do not remove whitelist checks. Do not introduce behavior that causes commands from untrusted users to run.

---

## Language and formatting rules

These rules are important.

### English-only for code comments and logs
- **All code comments must be in English.**
- **All logging messages must be in English.**
- exception/debug/service messages inside code should also be English unless there is a strong reason otherwise.

### Persian is allowed outside the code
- User-facing planning notes may be in Persian if the user asks for them.
- Prompts prepared for the human operator may be in Persian.
- But source code comments and logs must remain English.

### Keep user-facing bot text consistent
Prefer short, clear bot messages. Avoid noisy or overly verbose responses.

---

## Editing rules for Codex

When working on this repository:

1. Read the current files before changing them.
2. Preserve existing behavior unless the task explicitly changes it.
3. Do not remove working features while implementing the next phase.
4. If a task depends on a later roadmap phase, say so clearly.
5. Keep imports tidy and avoid dead code.
6. Prefer explicit helper functions over duplicated logic.
7. Keep function and variable names descriptive.
8. Do not introduce hidden magic behavior.
9. If you add a new command, update the `/start` help text if relevant.
10. If you add a new state or workflow, keep the state transitions understandable.
11. Temporary operator override (until further notice): do **not** auto-update `next.md` or `current-test.md` at the end of each step.

---

## Suggested project direction for the next task

The next recommended implementation step is:

### Phase 6 persistence and long-output safety

Scope:

- persist session metadata (SQLite) so key state survives process restart
- add safe output truncation/chunking for Telegram message length limits
- keep current PTY/live workflows and controls unchanged
- keep whitelist and per-user working-directory behavior unchanged

Success criteria:

- bot restart does not leave broken active-session pointers
- long outputs are delivered without Telegram send/edit failures
- current command and control workflows remain stable

---

## What to avoid right now

Avoid doing all of these at once in one step unless explicitly requested:

- PTY + full interactive text routing + inline buttons + transcript persistence + SQLite migration
- large codebase reorganization
- broad security redesign
- replacing all existing bot messages just for style reasons

Small, testable steps are preferred.

---

## If uncertain

If a requested change conflicts with the roadmap or existing behavior:

- prefer the smallest safe change
- explain tradeoffs clearly
- keep comments and logs in English
