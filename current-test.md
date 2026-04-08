# Current Stage Test Plan

Last update: 2026-04-08
Stage under test: Phase 5 core (without chat mode)

## Preconditions
- Bot service is running (`tg-vps-bot.service`)
- tester Telegram user id is in `ALLOWED_USER_IDS`
- bot is reachable and responding to `/start`

## Manual Test Cases
1. Access and identity
- send `/id`
- expected: bot returns Telegram user id

2. Command execution user check
- send `/run whoami`
- expected: command result includes service user (for example `pooria`), not `root`

3. Start interactive shell session
- send `/run bash`
- expected: start message includes session id, state, pid, current dir, command
- expected: inline control keyboard is visible
- expected: persistent reply keyboard is visible

4. Active status
- send `/status`
- expected: active session shown with `running` state and non-empty runtime

5. Send plain text to shell
- send `pwd` as plain text (without `/run`)
- expected: input is forwarded to active shell and output appears in `/tail`

6. Send Enter controls
- click inline `Enter` and send `/n`
- expected: session accepts newline input

7. Interrupt foreground command
- in shell send `sleep 100`
- click inline `Ctrl+C` (or send `/ctrl c`)
- expected: command interrupted and shell prompt returns

8. Tail and status controls
- click inline `Tail` and `Status`
- expected: bot sends tail/status for same active session id

9. Stream mode
- send `/stream on`
- run: `for i in 1 2 3; do echo "tick $i"; sleep 1; done`
- expected: live stream snapshots are pushed
- send `/stream off`
- expected: stream stops

10. Clear output buffer
- run `echo hello`
- send `/clear`
- send `/tail`
- expected: buffer was cleared (`[no output]` until new output)

11. Stop active session
- send `/stop`
- expected: session stops cleanly
- send `/status`
- expected: `No active session`

12. Stale button safety
- after session ended, click old inline control button
- expected: callback is rejected as stale/no-active

13. Working directory persistence
- send `/run cd /tmp`
- send `/status` with no active session
- expected: current dir shown as `/tmp`

14. File upload to current directory
- while current dir is `/tmp`, upload a small file
- expected: file saved in `/tmp` and bot returns SHA256

15. Duplicate upload confirmation
- upload same file again
- expected: overwrite confirmation appears
- click `Cancel`
- expected: file unchanged
- upload again and click `Overwrite`
- expected: file replaced and SHA256 shown

16. Upload filename sanitization
- upload a file with path-like name (for example containing `/` fragments)
- expected: bot either rejects invalid name or stores safely as plain basename in current dir
- expected: no write outside user's current directory

17. File download path resolution
- send `/get <relative-path>` for a file in current dir
- expected: file is sent with SHA256

18. Unauthorized user check (negative test)
- from a Telegram user outside whitelist, send `/id` or `/run ls`
- expected: no command execution
