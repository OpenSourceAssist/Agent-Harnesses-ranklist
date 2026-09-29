# How runaway guard decides

Before each tool call, the harness runs `guard.py` as a **PreToolUse hook**: a command the harness starts with the call's details as JSON on its input, before the call happens. The guard answers with nothing (the call goes on through the normal permission flow), a message, a question, or a block. The checks run in this order, and the first one that fires decides:

1. Spend cap reached: block (in Claude Code the turn also ends).
2. Loop: block.
3. Failure streak: ask the user (Codex: block, see below).
4. Messages: the spend warning, and the first model priced by estimate. The call goes on.

## Spend

**What it counts.** Every finished model response (one API call) in the session's transcript and in each subagent's transcript. The token counts come from the usage fields the harness writes. A response that the harness writes as several records counts once, with its final numbers. Each response is priced by `pricing.py` at API list prices checked on 2026-09-28: uncached input, cache reads, cache writes (5-minute and 1-hour), and output, per model. Model ids from Amazon Bedrock and Google Vertex AI get the model's list price.

**Each response counts once.** The guard keeps a short hash of every response it counted. A transcript read again from the start (a file that was replaced by a shorter one) adds nothing twice. A resumed session whose file begins with a copy of an earlier session's records skips the responses that the earlier session already counted: a small shared list in the state folder (`counted-ids.bin`, the latest 32,768 responses) records which session counted each one.

**Which transcripts.**

- Claude Code: the session file named in the hook input, plus every `agent-*.jsonl` under `<session>/subagents/` and `<session>/subagents/workflows/*/`.
- Codex: the rollout file named in the hook input, plus each subagent rollout whose first line (`session_meta`) carries the same session id. The guard looks in the main rollout's day folder and in today's and yesterday's folders. It reads each new file's first line, and marks the file as checked once that line is there.

**Lag.** The response still being written is counted at the next tool call. The last response of each subagent is never followed by another, so it is not counted. The total runs about 1% to 3% low, more with many subagents.

**Models missing from the price table.** A newer model, such as the next Opus version, is priced like the most expensive known model of its family (Claude Opus, Sonnet, Haiku, or Fable, or GPT; any Codex model counts as GPT), and a model of no known family like the most expensive known model. The estimate is too high rather than $0, so the cap still holds. Claude Code shows one message naming the first such model in a session; `status.py` shows how much of the total is estimated.

**The warning.** Once per cap, when spend is between `warn_at` (default 0.8) of the cap and the cap. Claude Code shows it to the user. Codex hooks have no way to show a message, so it appears only in `status.py`.

**The stop.** At the cap, every tool call is blocked, subagents' calls included. Four calls still pass, because they stop work or hand in a final answer: `TaskStop`, `KillShell`, `KillBash`, `StructuredOutput`. In Claude Code the block also ends the turn (`continue: false`), so each block does not start another model response. The block message tells the agent to stop and tell the user, and names the two ways to go on, for wherever the cap in force comes from:

- A project file: raise or remove `spend_cap_usd` in that file.
- `RUNAWAY_GUARD_CAP_USD`: raise or unset the variable where the agent runs.
- The user file: raise `spend_cap_usd` there (or set it, when no file sets a cap).
- In every case: run `status.py --session <id> --reset` in a terminal, which starts the count from $0 and keeps the session total in the record.

The user runs these in their own terminal: at the cap, the agent's own tool calls are blocked, `status.py` included, so the agent cannot lift its own cap.

**A session the guard sees for the first time.** Claude Code normally applies hook changes to sessions already running, so a new install can meet a session that has already spent more than the cap. At its first check, such a session is counted from that point: the guard sets the reset point there and says so once. A session the guard already counts is blocked at its next tool call once it is over the cap.

**Subscription plans.** The dollar figure is what the same tokens would cost at API list prices. On a subscription plan it measures usage, not the bill.

## Loop

**Fingerprint.** A call is its tool name plus its input. For shell commands, the `description` label is left out and runs of spaces count as one. Only a hash of the fingerprint is stored.

**The rule.** A call is blocked when the same fingerprint arrives for the third time (`loop_repeats`, default 3) with nothing changed in between. It stays blocked until something changes.

**What counts as a change.**

- Reads and searches change nothing: `Read`, `Grep`, `Glob`, `LS`, `NotebookRead`, `WebFetch`, `WebSearch`, `ToolSearch`. They neither end a run of repeats nor reset one.
- Any other call may change files or state: an edit, another shell command, a browser click, an MCP call. It ends every other call's run of repeats. A call's own repeats still count, so the same edit or the same command three times in a row is a loop.
- A new user prompt clears the counts: Claude Code's `prompt_id` and Codex's `turn_id` in the hook input. Older Claude Code versions send no `prompt_id`; there the typed prompt in the transcript clears them.

**Never counted** (these also end a run of repeats):

- Calls that start or message subagents: `Agent`, `Task`, `spawn_agent`, `send_message`, `followup_task`. Starting several subagents with the same prompt is a deliberate pattern.
- Calls that wait on purpose: `wait`, `wait_agent`, `sleep`, `TaskOutput`, `BashOutput`, `Monitor`, `ScheduleWakeup`, `list_agents`, and any shell command that contains `sleep` or `wait` (a poll, not a loop). The spend cap still covers a poll that never ends.
- Browser steps that repeat on purpose: an `action` of `scroll`, `key`, `press_key`, or `wait`, and tools whose name ends in `press_key`.
- Questions to the user: `AskUserQuestion`, `ExitPlanMode`, `request_user_input`.

**Per agent.** The main session and each subagent keep their own counts, so two subagents reading the same file are not a loop.

**Calibration on real data.** Over 19,625 tool calls in 400 local Claude Code transcripts, the rule would have fired twice, both in one session that ran the same browser script three and four times in a row. Replaying the 15 busiest sessions through the hook (14,663 calls, subagents included), it fired zero times. A looser rule that only an edit resets would have fired 71 times in 200 of those transcripts (9,559 calls), mostly on browser screenshots taken between clicks.

**What it misses.** Two different commands that alternate (A, B, A, B) end each other's runs, so that pattern never trips. The spend cap and the failure streak cover it.

## Failure streak

**Where results come from.** The transcript: each tool result is a success or a failure (an error result or a nonzero exit code).

- Not failures: a call denied by a permission rule, by another hook, or by the auto-mode reviewer. These neither count nor end the streak. Calls the guard itself blocked do not count either.
- Ends the streak: a success, a call the user rejected, an interrupt, a new typed prompt.

**At the limit** (`failure_streak`, default 5):

- Claude Code: the guard answers `ask`, so the user sees the reason and decides whether this call runs. The count then starts over.
- Codex: an `ask` answer makes Codex treat the hook as failed and run the call anyway, so the guard blocks the call instead, with a message that tells the agent to stop and ask the user. The count then starts over.
- Print mode (`claude -p`) has no one to answer, so a question there counts as a no. Use `--max-budget-usd` for print mode.

**Per agent**, like the loop counts.

**Lag.** The results of the newest response are read at the next call, so the question can come one call late.

**Calibration on real data.** Five failures in a row happened 5 times in 400 local transcripts. The replay of the 15 busiest sessions asked once in 14,663 calls.

## Settings

| Key | Default | Environment variable | Meaning |
|---|---|---|---|
| `spend_cap_usd` | 10 | `RUNAWAY_GUARD_CAP_USD` | Dollars per session; 0 turns the spend wire off |
| `warn_at` | 0.8 | `RUNAWAY_GUARD_WARN_AT` | Share of the cap that triggers the warning, above 0 and at most 1 |
| `loop_repeats` | 3 | `RUNAWAY_GUARD_LOOP_REPEATS` | Identical calls that count as a loop; 0 turns it off, otherwise 2 or more |
| `failure_streak` | 5 | `RUNAWAY_GUARD_FAILURE_STREAK` | Failed calls in a row before the question; 0 turns it off |

`RUNAWAY_GUARD_OFF=1` makes the hook do nothing.

**Where the values come from.** The user file is `runaway-guard.json` in the state folder, `${XDG_STATE_HOME:-~/.local/state}/runaway-guard/`. A project file is `<project>/.claude/runaway-guard.json` (or `.codex/`). An environment variable wins over the user file. A project file can make a limit stricter (a lower number that is not 0) but never looser, except for a value that neither the environment nor the user file sets. That way a repository you clone cannot switch off your cap. Invalid values are ignored. The hook reads the files on every call, so a change applies at the next call. When `install.py --cap` asks for a cap that a stricter setting overrides, the install says which setting keeps it.

## Never blocking work by mistake

- The hook always exits 0. Exit code 2 would block the tool call, so a bad flag, bad input, or an unreadable settings file lets the call through.
- The installed command ends in `|| true`. If the skill folder moves or is deleted, `python3` would exit 2 on the missing script and block every call; `|| true` turns that into a pass. Run `install.py --uninstall` before moving, updating, or removing the skill, then install again, so the guard keeps running.
- Any internal error lets the call through and adds one line to `errors.log` in the state folder: the time, the place in the code, the error type, and a short message with secrets masked.
- `install.py` sets a 10-second timeout. A PreToolUse hook that times out does not block: the harness goes on.
- Parallel calls of one session wait for each other through a lock file. A lock held longer than 2 seconds lets the call through unchecked.
- A transcript that cannot be read is skipped; the loop wire still works from the hook input alone.
- Cursor runs Claude Code hooks by default. The guard recognizes Cursor's input and answers `{}`, which makes no decision, because Cursor may read empty output as invalid JSON and block. This has not been tested in Cursor.

## Speed

Repeat calls read only the bytes added since the last call. On a 5,000-line transcript (4.6 MB), a repeat call took 54 ms at the median with Python 3.9.6 on a laptop, 22 ms of which is Python starting. The first call of a session reads the whole transcript once: 269 ms for the same file. Replaying a real 864-call session through the hook: 51 ms at the median, 57 ms at the 95th percentile. These times assume Python can cache the compiled scripts next to them, which it does by default. With `PYTHONDONTWRITEBYTECODE` set, or in a folder Python cannot write to, it compiles them on every call, and a check takes about 175 ms.

## State

One JSON file per session in the state folder: the transcripts' read offsets and sizes, the dollar total and the reset point, the estimated part and the models behind it, hashes of the responses counted, per-agent loop hashes and failure counts, the ids of calls the guard blocked, and the latest 50 events (time, trip wire, tool name, detail). The shared `counted-ids.bin` holds only hashes. Neither holds commands, prompts, or file contents. Session files not touched for 30 days are removed when a new session starts; a session's lock file stays as long as its session file is in use.

`install.py` also keeps, in `installs/`, a copy of each settings file as it was before the install, so that `--uninstall` can put it back byte for byte. That copy holds whatever the settings file held, such as other hook commands or `env` values, so it is created readable only by you, and the uninstall deletes it.
