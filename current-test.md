# Current Stage Test Plan

Last update: 2026-04-16
Stage under test: Phase 6 persistence + quick shell UX + smart output rendering

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

3. Slash command suggestions
- type `/` in private chat with bot
- expected: Telegram shows bot command suggestions from `setMyCommands`

4. Quick action keyboard
- send `/start`
- expected: quick action buttons are visible
- expected: quick action keyboard stays available at the bottom of chat
- expected: `Open Shell` is visible

5. Start interactive shell session
- send `/run bash`
- expected: start message includes session id, state, pid, current dir, command
- expected: inline session controls are visible

6. Quick shell launch UX
- tap `Open Shell`
- choose `zsh`
- expected: a new zsh session starts without typing `/run zsh`
- send `pwd`
- expected: input is forwarded to the shell and output appears

7. Inline menu uniqueness
- open inline main menu and output submenu
- expected: no duplicate action in both places (for example `Tail` only in Output)

8. Active status
- send `/status`
- expected: active session shown with `running` state and non-empty runtime

9. Send plain text to shell
- send `pwd` as plain text (without `/run`)
- expected: input is forwarded to active shell and output appears in `/tail`

10. Enter and interrupt controls
- send `/n` or inline `Enter`
- expected: newline sent
- run `sleep 100` and send `/ctrl c` or inline `Ctrl+C`
- expected: foreground command interrupted

11. Stream mode
- send `/stream on`
- run: `for i in 1 2 3; do echo "tick $i"; sleep 1; done`
- expected: live stream snapshots are pushed
- send `/stream off`
- expected: stream stops

12. Buffer policy
- send `/stream on`
- run `echo before-reset`
- send plain text `echo after-reset`
- expected: stream frame starts fresh for the new interactive input
- send `/stream off`
- run `echo tail-once`
- send `/tail`
- expected: output is shown
- send `/tail` again
- expected: the shown buffer was consumed (`[no output]` until new output)

13. Stop active session
- send `/stop`
- expected: session stops cleanly
- send `/status`
- expected: `No active session`

14. Stale session button safety
- after session ended, click old inline control button
- expected: callback is rejected as stale/no-active

15. Working directory persistence
- send `/run cd /tmp`
- send `/status` with no active session
- expected: current dir shown as `/tmp`

16. File upload to current directory
- while current dir is `/tmp`, upload a small file
- expected: file saved in `/tmp` and bot returns SHA256

17. Duplicate upload confirmation
- upload same file again
- expected: overwrite confirmation appears
- click `Cancel`
- expected: upload is cancelled
- upload again and click `Overwrite`
- expected: file replaced and SHA256 shown

18. Stale upload prompt safety
- trigger overwrite prompt twice for same file
- click an older prompt's button
- expected: stale upload prompt is rejected and latest prompt remains authoritative

19. Upload filename sanitization
- upload a file with path-like name (containing `/` fragments)
- expected: bot rejects invalid name or stores safely as basename in current dir
- expected: no write outside user's current directory

20. Upload size limit
- upload a file larger than `MAX_UPLOAD_BYTES`
- expected: upload is rejected with explicit size/limit message

21. File download path resolution
- send `/get <relative-path>` for a file in current dir
- expected: file is sent with SHA256

22. Context inline controls on usage/error
- send malformed commands like `/run` or `/get`
- expected: inline `Help`/`Status` controls appear
- click each control
- expected: corresponding response is sent

23. Smart output rendering
- run `history | grep codex`
- expected: history number and full command stay monospace/copy-friendly
- expected: live stream formatting does not degrade mid-frame; if needed it rolls to the next frame
- run `printenv | grep OPENAI`
- expected: `key=value` style output remains readable and copy-friendly
- run `ss -tulpn` or `journalctl -n 20`
- expected: structured output remains readable and does not fall back to broken plain formatting

24. Codex slash-command routing
- start `codex`
- send `!/init`
- expected: slash command is forwarded to codex
- send a normal bot command like `/status`
- expected: bot handles it as a bot command, not as codex input

25. Unauthorized user check (negative test)
- from a Telegram user outside whitelist, send `/id` or `/run ls`
- expected: no command execution
