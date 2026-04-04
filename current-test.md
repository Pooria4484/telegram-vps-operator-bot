# Current Stage Test Plan

Last update: 2026-04-03
Stage under test: Phase 5 + Chat mode (special capability #1)

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

27. Enter chat mode
- send `/chat`
- expected: chat mode summary is shown with active chat id and usage windows
- expected: ops keyboard is replaced by chat keyboard (`/chat usage`, `/chat new`, `/chat resume`, `/chat exit`)

28. Chat usage view
- send `/chat usage`
- expected: usage for 5-hour and weekly windows plus reset timestamps is shown

29. Chat with model
- while in chat mode, send plain text like `hello`
- expected: response from model is returned
- expected: usage counters increase

30. New chat thread
- send `/chat new`
- expected: active chat id changes and starts empty/new thread

31. Resume previous chat
- send `/chat resume`
- expected: only chat-mode threads are listed as buttons
- click one previous chat button
- expected: selected chat becomes active and is resumed

32. Exit chat mode
- send `/chat exit`
- expected: chat mode is disabled and persistent ops keyboard is restored

33. Plain text routing after chat exit
- with active shell session, send plain text
- expected: text goes to PTY session (not model chat)

34. Chat mode without Codex auth file (negative test)
- temporarily move or invalidate `~/.codex/auth.json`, then send `/chat` and plain text
- expected: clear auth-file error is shown and bot does not crash

35. Chat mode codex binary resolution
- run bot under service environment where `codex` is not in PATH
- send `/chat` and plain text
- expected: bot resolves `codex` from `CODEX_BIN` or `~/.nvm/.../bin/codex` and returns response

36. Chat mode with old system node on PATH
- run bot where `/usr/bin/node` is old (example v12) and codex installed under `~/.nvm/...`
- send `/chat` and plain text
- expected: bot launches codex using sibling nvm `node` and does not fail with `Unexpected reserved word`

37. Interactive session precedence over chat mode
- enter chat mode with `/chat`
- start interactive shell with `/run bash`
- send plain text `pwd`
- expected: text is forwarded to active PTY session (not Codex chat)
- expected: `/tail` shows shell output

38. Chat path still works when no active session
- stop/exit active session
- while chat mode is enabled, send plain text `hello`
- expected: message is handled by Codex chat and response is returned

39. Plain text without active route (no silent drop)
- ensure no active session and chat mode is disabled (`/chat exit`)
- send plain text `hello`
- expected: bot responds with guidance to use `/chat` or `/run <command>`

40. `/chat` enter with active session shows routing note
- start active session with `/run bash`
- send `/chat`
- expected: chat summary includes a note that plain text is routed to active shell session until stopped

41. Chat streaming updates in-place
- send `/chat`
- send a prompt that takes a few seconds
- expected: bot sends one progress message (`Codex stream: ...`) and updates the same message while response is generated

42. Chat stream finalize behavior
- after stream completes, expected: same progress message is replaced by final assistant response + usage summary
- expected: no silent drop (either edited message or fallback new message appears)

43. Chat heartbeat while waiting
- send `/chat`
- send a prompt and wait before first token arrives
- expected: progress message is periodically updated (`waiting for model response... Ns`)

44. Chat timeout behavior
- simulate unreachable model endpoint/network issue
- expected: within 120 seconds progress message is replaced with explicit timeout error
- expected: no indefinite `Codex stream: ...` hang

45. Codex JSON `item.completed` compatibility
- run `/chat` and send a short prompt
- expected: when codex emits `item.completed` (agent_message), bot captures it and posts final answer
- expected: final answer is not lost even if no token-level delta events were emitted

46. Chat progress edit validation
- send `/chat` and a prompt
- expected: progress message is edited without Telegram validation errors
- expected: no stuck `Codex stream: ...` due invalid reply markup on `edit_text`

47. Chat edit fallback behavior
- simulate edit failure case (or observe from logs)
- expected: bot posts a fresh progress/final message instead of staying frozen
- expected: service log contains a warning for failed edit path

48. Reconnect status visibility
- trigger a slow/unsteady chat request
- expected: stream text shows status hints like `Reconnecting...` when codex emits JSON error events

49. Stream callback guard timeout
- force slow/unstable Telegram edit path while chat is running
- expected: codex processing still reaches final response or explicit timeout
- expected: chat does not freeze at an intermediate second counter due blocked UI callback

50. Heartbeat cannot deadlock handler
- during long chat, observe heartbeat updates crossing 114s and beyond if needed
- expected: either explicit timeout/final response appears; handler must not remain stuck on a fixed second value

51. Heartbeat preserves reconnect context
- during reconnect cycles, keep watching waiting message
- expected: waiting line includes `last status: ...` (for example reconnect attempts) instead of hiding transient codex errors immediately

52. Reconnect fail-fast
- force reconnect-heavy run with no token output
- expected: after fail-fast threshold (~75s) bot returns explicit reconnect failure instead of continuing long wait

53. `/run codex` path resolution from login shell env
- send `/run codex --version`
- expected: command is recognized without manually exporting PATH inside session
- expected: one-shot command finishes and returns version output (no hanging live session)

54. `/run zsh` then `codex` availability
- send `/run zsh`
- then send plain text `codex --version`
- expected: codex is found in the interactive shell (matching direct terminal behavior)

55. `/run bash` then `codex` availability
- send `/run bash`
- then send plain text `codex --version`
- expected: codex is still found (PATH inherited from login-shell-derived env)

56. `/tail` plain-text cleanup for terminal escape codes
- run a command that emits colored/control output
- expected: `/tail` shows plain text without ANSI fragments like `[39;49m`, `[K`, `?25h`

57. Carriage-return overwrite behavior
- run a command with progress-style carriage returns
- expected: tail keeps the latest line state instead of appending noisy partial redraw fragments

## Exit Criteria
- all 57 tests pass
- no regression in `/run`, `/status`, `/tail`, `/stop`, `/ctrl`, `/n`, `/clear`, `/stream`, `/chat`, inline controls, `/get`, upload flow
