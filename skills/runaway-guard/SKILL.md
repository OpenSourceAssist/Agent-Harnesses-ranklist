---
name: runaway-guard
description: >-
  Runaway guard: a hook that stops a live Claude Code or Codex session when
  the agent loops on the same tool call, keeps failing, or exceeds a dollar
  cap. Use when the user wants a spending cap, budget limit, or cost ceiling
  for interactive agent sessions (the built-in --max-budget-usd works only in
  print mode); wants to halt an agent that repeats the same command, is stuck
  in a loop, or runs up a bill unattended overnight; wants a circuit breaker
  for several failed tool calls in a row; or asks how much the current session
  has spent against its cap, why a call was blocked, or how to raise or reset
  the cap. Runs locally: reads the session transcripts before each tool call
  and sends nothing.
license: MIT
compatibility: "Python 3.9+ on macOS or Linux, and Claude Code or Codex with hooks enabled. Makes no network calls."
metadata:
  author: "Ryan Alberts"
  version: "1.0.0"
  source: "https://github.com/RyanAlberts/best-of-Agent-Harnesses"
---

# Runaway guard

An agent left alone can repeat a failing command, retry broken calls, or keep spending long after the
work stopped paying off, and Claude Code's `--max-budget-usd` cap works only in print mode. This skill
installs a **hook** (a command the harness runs before every tool call) that steps in at three trip
wires: the same call a third time with nothing changed, five failed calls in a row, and a dollar cap.
The hook reads the session's transcript files on this machine, keeps only counts and hashes, and sends
nothing anywhere.

## When to use

- The user wants a spend cap, budget, or cost ceiling for interactive Claude Code or Codex sessions.
- The user wants an agent stopped when it loops on one command or keeps failing, for example before
  leaving it unattended.
- The user asks how much the current session has spent against its cap, why the guard blocked a call,
  or how to raise the cap or start the count over.
- The user wants to remove the guard.

## When not to use

- Finding where past tokens and money went: use `session-waste-report`.
- Testing whether permission rules and hooks stop dangerous commands: use `guardrail-tester`.
- Turning a rule from AGENTS.md or CLAUDE.md into a hook: use `rules-to-guards`.
- A one-off print-mode run (`claude -p`): its own `--max-budget-usd` and `--max-turns` flags cap it.
- Gemini CLI, Cursor, and OpenCode: the guard does not install there. Gemini CLI and OpenCode already
  detect loops; `references/harness-hooks.md` says what each lacks.

## Steps

`<skill-dir>` means the folder that holds this SKILL.md (Claude Code shows it as the skill's base
directory). Run every command through `python3` exactly as written, with the path in double quotes:
skill folders can sit under paths with spaces.

1. **Explain the three trip wires and their defaults** in plain words:
   - **Loop**: the same tool call with the same input, a third time with nothing changed in between,
     is blocked. Reads and searches in between change nothing; an edit, another command, or a new
     user prompt does. Re-running tests after an edit never trips it, and neither do deliberate waits
     or starting subagents.
   - **Failures**: after 5 failed tool calls in a row, Claude Code asks the user before the next call
     runs. Codex hooks cannot ask, so there the call is blocked with a message telling the agent to
     ask the user.
   - **Spend**: the running cost of the session and its subagents at API list prices. A warning at 80%
     of the cap; at the cap every tool call is blocked (in Claude Code the turn ends) until the user
     raises the cap or starts the count over. A model missing from the price table is priced like the
     most expensive known model of its family, so the cap still holds.

   Done when the user has heard all three wires and the $10 default cap.

2. **Ask for three choices**: the dollar cap, the harness (Claude Code or Codex), and the scope (user:
   every session of this user; project: only sessions in the current project). Say that on a
   subscription plan the dollar figure measures usage at API prices, not the bill. Done when you have
   all three answers, or the user accepted the defaults: Claude Code, user scope, $10.

3. **Show the dry run** with the user's choices:

   ```bash
   python3 "<skill-dir>/scripts/install.py" --harness claude-code --scope user --cap 10
   ```

   It prints the headline, the settings file it would change, the diff, and notes, and writes nothing.
   Add `--project <folder>` for project scope when the agent's working folder is not the project.
   Done when the user has seen the headline and the diff and has said yes or no.

4. **Install only on a clear yes**: run the same command with `--write` added. Done when the output
   starts with the headline and says "Done". Exit code 2 means a settings file could not be read as
   JSON, has an unexpected shape, or could not be written; nothing was changed. Quote the message,
   and leave the file for the user to fix.

5. **Relay the notes** printed under "Notes", in particular:
   - Claude Code normally applies hook changes to sessions already running. A session the guard
     already counts is blocked at its next tool call once it is over the cap; a session it sees for the
     first time after spending more than the cap is counted from that point.
   - Codex runs the hook only after the user trusts it in `/hooks`.
   - Before the user moves, updates, or removes this skill, they run `--uninstall`, then install again
     from the new place: the hook entry points at this folder.

   Done when every note is passed on.

6. **Offer status**:

   ```bash
   python3 "<skill-dir>/scripts/status.py"
   ```

   Done when the user has the headline, or declined.

To remove the guard, run `python3 "<skill-dir>/scripts/install.py" --uninstall`, show the diff, and
add `--write` on a clear yes. With no `--harness`, `--scope`, or `--settings`, it checks all four places
the guard can be (Claude Code and Codex, user and project) and names each file it changes. It removes
only the guard's own entries, and puts a file back exactly as it was when nothing else changed it.

## When the guard stops a call

A blocked call comes back as a tool error that starts with "Runaway guard". Stop and pass the message
to the user in your own words, naming the trip wire.

- **Spend stop**: every tool call is blocked, `status.py` included, and in Claude Code the turn ends.
  Answer in text only. Give the user the two ways on that the message names, for them to use in their
  own terminal: the setting that holds the cap (the file, or the `RUNAWAY_GUARD_CAP_USD` variable), and
  the `status.py ... --reset` command. To see the counts, they run `python3 "<skill-dir>/scripts/status.py"`
  there too. The cap is theirs to lift.
- **Loop**: change the approach rather than the wording of the same call.
- **Failure question**: the user answers it in the permission prompt; continue with what they allow.

## Read the results

`install.py`:

- **Headline**: the limits the hook will enforce after the change, from `--cap`, the guard's settings
  files, and any `RUNAWAY_GUARD_*` variable set in the shell.
- **Diff**: the one PreToolUse entry added or removed; the hooks already there stay unchanged.
- **Notes**: steps the user still takes, and settings that change the picture, such as
  `disableAllHooks`.
- `--json` for an install: `headline`, `action`, `settings_path`, `config_path`, `command`, `cap_usd`,
  `limits`, `changed`, `written`, `other_hooks`, `notes`. For `--uninstall`: `headline`, `action`,
  `hook_entries_removed`, `changed`, `written`, `notes`, and `targets` (each file checked, with
  `removed` and `restored_exactly`).

`status.py` (defaults to the session the guard checked most recently; `--session` takes an id or its
first characters, `--list` shows every session):

- **Headline**: the session's first 8 characters, dollars since the start or the last reset, the cap,
  and how many times the guard stepped in.
- **Table**: each wire's limit, its current value, and how often it fired.
- **Latest events**: time, wire, tool name, detail. Warnings appear here too.
- **Estimate line**: the part of the dollar figure priced by estimate, for models missing from the
  price table, and which models they are.
- `--json`: `session`, `spent_usd`, `total_usd`, `cap_usd`, `limits`, `trips`, `recent_trips`,
  `failure_streak`, `transcripts`, `estimated_usd`, `estimated_tokens`, `estimated_models`, `resets`,
  `errors_logged`.

`references/how-it-decides.md` has every rule, threshold, setting, and safeguard, with calibration on
real sessions; `references/harness-hooks.md` has what each harness's hooks allow, with sources.

## Report to the user

After an install or a dry run:

1. The headline, verbatim, in bold.
2. What changes: the settings file and the one entry, and the cap file when `--cap` was given.
3. The notes, as a short list.
4. One line: how to check status, and how to remove the guard.

For status:

1. The headline, verbatim, in bold.
2. The table.
3. When a wire has fired: the latest events and the next step. For a spend stop, the raise or reset
   command from the block message, for the user to run in their own terminal.

## Files

- `scripts/guard.py`: the hook. Reads the hook input on stdin and prints a decision. Always exits 0,
  and lets the call through on any internal error, logging it to `errors.log` in the state folder.
- `scripts/install.py`: prints the settings change and applies it with `--write`; `--uninstall`
  removes only the guard's entries. Flags: `--harness`, `--scope`, `--project`, `--cap`, `--settings`,
  `--json`, `--out`.
- `scripts/status.py`: spend and trip counts per session; `--list`, `--session`, `--reset`, `--json`,
  `--out`.
- `scripts/transcripts.py`, `scripts/pricing.py`, `scripts/safe.py`: the shared transcript reader, price
  table, and text cleaner that puts session ids, tool names, and model ids in the report inside inline
  code, copied from this repository's shared code.
- `references/how-it-decides.md`: the trip wires, thresholds, settings, safeguards, and speed.
- `references/harness-hooks.md`: hook support in Claude Code, Codex, Gemini CLI, Cursor, and OpenCode.
