# Stop a runaway agent session

A hook for Claude Code and Codex that stops a live session when the agent repeats the same call, keeps failing, or spends past a dollar cap you set.

## What you get

A sample. The numbers are invented; your install prints its own.

The install shows its plan first and writes nothing until you say yes:

```
**Runaway guard will stop Claude Code when the same call repeats 3 times, 5 calls fail in a row, or spend passes $10.00.**

Dry run: nothing is written yet. Run the same command with --write to apply this change.

Settings file ~/.claude/settings.json: adds one PreToolUse hook entry and leaves the 2 PreToolUse hooks already there unchanged.
```

Later, in the middle of a session, the guard steps in on its own. At the cap, the turn ends and you see a message such as:

```
Runaway guard: spend cap reached ($10.07 of $10.00); tool calls are blocked. To go on, raise
spend_cap_usd in /Users/you/.local/state/runaway-guard/runaway-guard.json, or start the count over
by running this in a terminal: python3 /Users/you/.claude/skills/runaway-guard/scripts/status.py
--session 5f0c3a1e-8d7b-4c2a-9e61-0123456789ab --reset
```

And the status report shows what it counted:

```
**Session `5f0c3a1e` has spent $10.07 of its $10.00 cap; the guard stepped in 3 times.**

| Trip wire | Limit | Now | Stepped in |
|---|---|---|---|
| Spend | $10.00 per session, warning at 80% | $10.07 (101%) | 1 call blocked |
| Loop | the same call 3 times with nothing changed |  | 2 calls blocked |
| Failures | 5 failed calls in a row | 1 in a row | stepped in 0 times |

Latest events:
- 2026-09-28 14:02 UTC, loop: `Bash`, 3rd identical call
- 2026-09-28 14:31 UTC, warning: `Read`, $8.04 of the $10.00 cap
- 2026-09-28 14:40 UTC, spend: `Edit`, $10.07 of the $10.00 cap
```

## Install

```
npx skills add https://github.com/RyanAlberts/best-of-Agent-Harnesses/tree/main/skills/runaway-guard
```

In Claude Code, all ten at once:

```
/plugin marketplace add RyanAlberts/best-of-Agent-Harnesses
/plugin install harness-skills@agent-harnesses
```

Manual: copy `skills/runaway-guard/` into your agent's skills folder: `~/.claude/skills/` (Claude Code), `~/.agents/skills/` (Codex, Gemini CLI, Cursor, and OpenCode all read it).

Installing the skill adds no hook by itself. The agent asks for your cap, shows the settings change, and writes it only after you agree.

## Use it

Ask your agent: "Stop my Claude Code sessions if they loop or spend more than $20."

To do it yourself, look at the change first, then apply it:

```bash
python3 "$HOME/.claude/skills/runaway-guard/scripts/install.py" --cap 20
python3 "$HOME/.claude/skills/runaway-guard/scripts/install.py" --cap 20 --write
```

Add `--harness codex` for Codex and `--scope project` to guard only the current project. Check a session with `status.py`, and start its counts over with `status.py --reset`.

To remove the hook, run `install.py --uninstall`, then again with `--write`. With no `--harness` or `--scope`, it checks Claude Code and Codex, user and project settings, and names each file it changes. Run it before you move, update, or remove the skill, then install again from the new place: the hook entry points at the skill's folder. If the folder disappears first, the hook does nothing and never blocks a call, and `--uninstall` from the new copy still removes the old entry.

## How it works

Claude Code and Codex can run a command before every tool call, pass it the call as JSON, and let it allow, ask about, or block the call. That command is a **hook**. `install.py` adds one hook entry to your settings, next to any hooks you already have. Before each tool call, `guard.py`:

- **Spend**: reads only the new lines of the session's transcript and its subagents' transcripts, counts each model response once, and prices it at API list prices. A model missing from the price table is priced like the most expensive known model of its family, so the cap still holds. It warns you at 80% of the cap. At the cap it blocks every tool call, and in Claude Code it ends the turn.
- **Loop**: compares the call's tool name and input with the calls before it. The same call a third time, with only reads and searches in between, is blocked. An edit, another command, or a new prompt from you starts the count over, so re-running tests after a fix never trips it.
- **Failures**: counts failed calls in a row from the transcript. After 5, Claude Code asks you whether the next call should run.

It keeps a small file per session with counts and hashes, answers in about 50 ms, and lets the call through if anything inside it goes wrong. [How it decides](references/how-it-decides.md) covers every rule, with the numbers from replaying real sessions: across 14,663 tool calls from the 15 busiest sessions on one Mac, the loop rule never fired and the failure rule asked once.

## Works with

| Harness | Support | What differs |
|---|---|---|
| Claude Code | full; replayed on real sessions | all three trip wires; asks you after 5 failures; warns at 80% |
| Codex | loop, failures, and spend; transcript reading replayed on a real session | Codex hooks cannot ask or warn, so after 5 failures the call is blocked and the agent is told to ask you, and the 80% warning shows only in `status.py`; you trust the hook once in `/hooks`; not yet tested in a live Codex session |
| Gemini CLI | not supported | already detects loops on its own; no confirmed prices for a spend cap |
| OpenCode | not supported | already asks on repeated identical calls (`doom_loop`); extensions are JavaScript plugins |
| Cursor | not supported | its transcripts have no token counts; Cursor also runs Claude Code hooks, and the guard is built to answer them with no decision (not tested in Cursor) |

It runs on macOS and Linux with Python 3.9 or newer and nothing else to install. [Hook support per harness](references/harness-hooks.md) has the details and sources.

## Limits

What the dollar figure is:

- An estimate at API list prices. On a subscription plan it measures usage, not your bill. Batch discounts, fast mode, and regional pricing are left out.
- A little behind. The check runs before each tool call, so a response that makes no tool call is counted at the next one, and each subagent's last response stays uncounted. The total runs about 1% to 3% low, more with many subagents.
- High for a model the price table does not know yet, until you update the skill.
- Counted from the guard's first check. A session that had already spent more than the cap when the guard first saw it is counted from that point, and the guard says so once.

What the trip wires leave to the spend cap:

- Two different commands that alternate end each other's runs, so the loop rule misses them.
- Commands that wait on purpose (`sleep`, `wait`) and calls that start subagents never count as loops.
- A failure question can come one call late, because the newest results are read at the next call.

Where it runs:

- In print mode (`claude -p`) no one can answer the failure question, so it counts as a no. Use `--max-budget-usd` there.
- The first check of a long session reads the whole transcript once: about a quarter of a second for 5,000 lines.
- Each check takes about 50 ms when Python can cache the compiled scripts next to them, as it does by default. With `PYTHONDONTWRITEBYTECODE` set, a check takes about 175 ms.
- Windows outside WSL2 is untested.

## Privacy

- **Read**: the current session's transcript files and your settings files, on this machine. For Codex it also reads the first line of other recent rollout files to find this session's subagents, and keeps only their file names.
- **Kept**: one small JSON file per session in `~/.local/state/runaway-guard/`, with read positions, dollar totals, hashes of recent tool calls and responses (not their content), and the latest events (time, trip wire, tool name). Files older than 30 days are removed.
- **Written**: your settings file and the cap file, only with `--write`. The install also keeps a private copy of your settings file as it was, so that `--uninstall` can put it back exactly; the uninstall deletes that copy.
- **Sent**: nothing. The guard makes no network calls.

## Related

In this repository:

- [session-waste-report](../session-waste-report/): where past tokens and money went, and what to change.
- [guardrail-tester](../guardrail-tester/): tests whether your permission rules and hooks stop dangerous commands.
- [Safe Claude Code settings](../../templates/claude-code-safe-settings/): permission rules and a guard hook for dangerous commands, to pair with this one.
- [Why the harness matters](../../comparisons/why-the-harness-matters.md): how much the harness around a model changes what an agent does.

Credits and prior work:

- Built-in parts of the same idea: Claude Code's print-mode `--max-budget-usd` ([CLI reference](https://code.claude.com/docs/en/cli-reference)), OpenCode's `doom_loop` ([permissions](https://opencode.ai/docs/permissions)), and Gemini CLI's loop detection.
- Earlier attempts: [cc-blackbox](https://github.com/softcane/cc-blackbox) (archived) and [claude-bumper-lanes](https://github.com/kylesnowschwartz/claude-bumper-lanes), which caps the size of edits.
- The request for an interactive cap: [anthropics/claude-code#95964](https://github.com/anthropics/claude-code/issues/95964).
