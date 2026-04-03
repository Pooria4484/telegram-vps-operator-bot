# Current Stage Test Plan

Last update: 2026-04-03
Stage under test: Phase 5 (Stream control - minimal)

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
- expected: command result includes `pooria` (not root)

3. Start interactive shell session
- send `/run bash`
- expected: start message includes session id, state, pid, current dir, command
- expected: inline control keyboard is visible (`Stop`, `Ctrl+C`, `Ctrl+D`, `Enter`, `Tail`, `Status`, `Stream`, `Clear Output`)
- expected: persistent reply keyboard is visible in chat input area
- expected: control buttons are arranged in 4-column rows for better UX

4. Active status
- immediately send `/status`
- expected: active session shown with `running` state and non-empty runtime

5. Send plain text to shell
- send `pwd` as a normal text message (without `/run`)
- expected: input is forwarded to active shell and output appears in `/tail`

6. Send Enter using inline button
- click `Enter` button on control message
- expected: session accepts newline input without error

7. Send Enter using `/n` command
- send `/n`
- expected: session accepts newline input without error

8. Run long command inside shell
- send `sleep 100` in shell
- expected: command starts and blocks foreground

9. Interrupt using inline `Ctrl+C`
- click `Ctrl+C` button
- expected: foreground command is interrupted and shell prompt returns

10. Tail button
- click `Tail` button
- expected: bot sends tail snapshot message for current session

11. Status button
- click `Status` button
- expected: bot sends session status message with same active session id

12. Stream button toggle
- click `Stream` button
- expected: callback confirms stream mode toggled

13. Stream mode on
- send `/stream on`
- expected: bot confirms stream mode enabled

14. Stream mode status
- send `/stream status` (or `/live status`)
- expected: stream mode is `on`

15. Live stream push
- in active shell run a noisy command (example: `for i in 1 2 3; do echo \"tick $i\"; sleep 1; done`)
- expected: bot automatically pushes live stream snapshots while command is running

16. Stream mode off
- send `/stream off`
- expected: bot confirms stream mode disabled

17. Output copy behavior
- run a command that prints multi-word output (example: `echo \"word1 word2\"`)
- expected: words are rendered as separate code tokens and each word can be copied independently

18. Clear output buffer via inline button
- click `Clear Output`
- expected: output buffer is cleared
- expected: `Tail` immediately shows `[no output]` until new output arrives

19. Clear output buffer via command
- send `/clear`
- expected: output buffer is cleared and confirmation message is returned

20. Send EOF using inline `Ctrl+D`
- click `Ctrl+D` button
- expected: shell exits and session ends

21. Final status after exit
- send `/status`
- expected: `No active session` is shown

22. Stale button safety
- after session is ended, click an old control button from previous message
- expected: no command is executed; callback is rejected as stale/no-active

23. Tail during/after interactive run
- send `/tail`
- expected: no crash; recent output is shown

24. Working directory persistence
- send `/run cd /tmp`
- send `/status` (with no active session, dir should be shown in no-session message)
- expected: current dir reflects `/tmp`

25. File get path resolution
- send `/run touch test_phase2.txt`
- send `/get test_phase2.txt`
- expected: file is sent successfully, path resolved from current dir, SHA256 shown

26. Whitelist protection (negative test)
- from non-whitelisted Telegram user, send `/id` or `/run ls`
- expected: bot does not execute commands for that user

## Exit Criteria
- all 26 tests pass
- no regression in `/run`, `/status`, `/tail`, `/stop`, `/ctrl`, `/n`, `/clear`, `/stream`, inline controls, `/get`, upload flow
