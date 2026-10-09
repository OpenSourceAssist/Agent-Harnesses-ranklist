---
name: regression-finder
description: >-
  Regression check for coding agents: shows how the agent behaved before and
  after each harness update, model switch, or week in the user's own Claude
  Code or Codex history, and finds the point where it changed. Use when the
  user says the agent's quality dropped or it got worse, dumber, lazier, or
  degraded since an update or since they upgraded; asks whether a new Claude
  Code or Codex version or model made it worse than the old one; wants to know
  which release, version, or week it regressed in; or wants numbers to report a
  regression, such as reads before edits, interruptions, and corrections per
  version. Runs locally and reads transcripts only; nothing goes over the
  network.
license: MIT
metadata:
  author: "Ryan Alberts"
  version: "1.0.0"
  source: "https://github.com/RyanAlberts/best-of-Agent-Harnesses"
---

# Regression finder

When a coding agent seems worse after an update, the user's own session history can show whether it changed and when. This skill splits the user's Claude Code or Codex sessions by harness version, model, or week, measures the same behavior in each (reads before the first edit, reads per edit, edits to files not read first, interrupts, corrections, failed tool calls, output, cost), and places the change at the update, or within the span of versions, where the numbers moved, along with anything else that changed at the same point. It reads local transcripts, prints counts and rates, never prints prompt text, and sends nothing anywhere.

## When to use

- The user says the agent got worse, dumber, or lazier after an update, or asks "is it just me?"
- The user asks whether a new Claude Code or Codex version, or a new model, changed how the agent works.
- The user wants to know which release or week a regression started.
- The user wants evidence for a bug report about a regression, like [anthropics/claude-code#42796](https://github.com/anthropics/claude-code/issues/42796).

## When not to use

- Where tokens and money go in general (re-reads, cache rebuilds, oversized results): use `session-waste-report`.
- Stopping a session that loops or overspends right now: use `runaway-guard`.
- Comparing two harnesses on the same tasks: use `harness-test-drive`.
- Checking whether "tests pass" claims were true: use `claim-check`.
- Finding which instruction-file rules the agent breaks: use `rules-to-guards`.
- Cursor: it keeps no usable transcripts, so there is nothing to measure.

## What it reads

Tell the user this when they ask what the check looks at:

- **Read**: the session files each harness keeps (Claude Code under `~/.claude/projects/`, Codex under `~/.codex/sessions/`), only those changed within the window. Prompt text is read on this machine only to spot corrections such as "no, that's wrong".
- **Printed**: counts, rates, token totals, versions, model names, and project folders. Never prompt text, commands, or file contents.
- **Written**: nothing, unless the user asks for `--out` or `--svg`, which write the one file named.
- **Sent**: nothing. It makes no network calls.

## Steps

`<skill-dir>` means the folder that holds this SKILL.md (Claude Code shows it as the skill's base directory). Keep the quotes around the script path in every command: skill folders can sit under paths with spaces.

1. **Run the default check.** Pick the harness the user asks about (`claude-code` by default, or `codex`) and run:

   ```bash
   python3 "<skill-dir>/scripts/regress.py" --harness claude-code
   ```

   It reads the last 90 days and splits by harness version. Match the flags to the question:

   | The user says | Add |
   |---|---|
   | "since the last Codex update" | `--harness codex` |
   | "since I switched to the new model" | `--by model` |
   | "worse these past few weeks" | `--by week` |
   | "in my api repo" | `--project <that folder>` |
   | "since the spring" | `--since 180d` |

   It takes a few seconds per thousand sessions and exits 0 even when it finds nothing. Done when the output starts with a bold headline, or you have told the user the exact error.

2. **Answer the user's own question first.** If the user named an update or a time such as last week, find it in the By version table first. If it was not tested, lead with that (for example: 2.1.280 had 7 sessions, and the test needs 20 on each side), then give the headline as background, not as the answer. The notes list every update that was not tested, with its sessions.

   Then read the headline. It is one of these kinds:
   - A flagged change: "After Claude Code `2.1.270`, your agent reads 41% less before it edits...", or "After an update between Claude Code `2.1.260` and `2.1.270`, ..." when the test had to borrow neighboring versions. The change lies somewhere in that span; say the span, not one version. Go to step 3.
   - "No behavior change passed the test across ...": the numbers wobble but nothing passed. Say so plainly and give the session counts; go to step 5.
   - "No lasting behavior change passed the test ...; 1 version stands out": one version differs from the versions on both sides of it. Report the "Stands out" line as it is.
   - "Not enough history to test an update yet": fewer than 20 sessions on each side of every update. Offer, in this order, a longer window (`--since 180d`, when the history goes back that far), a split by time (`--by week`), and last a lower bar (`--min-sessions 12`, the floor). A lower bar tests more updates, but each test can only catch larger changes.
   - "No ... found", "records no version", "ran on one ...", or "Not enough history to compare models": nothing to compare yet. Say so, and name what the notes list as left out.

   Done when the user's own update or time is answered, or you know which kind of headline it is.

3. **Follow each confounder.** A confounder is anything else that changed at the same update and could explain the numbers. The section "What else changed at the same point" lists them, and the headline ends with "but ... changed at the same point" or "both sides were in use at the same time" when they exist. For each line:
   - The model changed: run again with `--by model` and see whether the change follows the model instead.
   - The harness version changed (in a model or week split): run again with `--by version`.
   - The work moved between projects, or one project holds most turns: run again with the `--project` argument the line prints. Copy the --project argument exactly as printed, quotes included.
   - Sessions changed shape, or the share of scripted runs changed: tell the user the kind of work changed (for example scripted runs against long conversations), so the update may not be the cause.
   - Both sides were in use at the same time, or both models were: tell the user two installs or two models ran side by side, so the difference may come from what each was used for.

   Done when each confounder line has a rerun result or a one-sentence explanation for the user.

4. **Offer the chart** when something is flagged and the user wants to see it or share it:

   ```bash
   python3 "<skill-dir>/scripts/regress.py" --harness claude-code --svg regression.svg
   ```

   It writes one small line chart per flagged number to the path given, and nothing else. Done when you have given the user the path, or the user declined.

5. **Report** in the shape below.

## Read the results

- **Headline**: the update with the most flagged changes, its two most telling changes, and the confounders at that update.
- **Flagged changes**: one row per change. "Where" is "after X" when the test compared X with the version just before it, or "between X and Y" when it borrowed neighbors: the change is placed within that span, not at one version. Before and after are per-session values, the same values the test ranks (the median or mean of one value per session, as the row says). The sessions on each side are the sample size to quote.
- **A change is flagged** when p is under 0.01 after adjusting for the number of tests, the per-session value moved at least 20%, and both point the same way. Windows that overlap and show the same change count as one test and one row.
- **Stands out**: a version (or week) that differs from the ones on both sides of it. It is reported once and is not a lasting change.
- **By version (or model, or week)**: every number for every slice, turn by turn, including slices too small to test. "n/a" means that number cannot be measured there, for example reasoning tokens that Claude Code did not record.
- **Notes**: what was left out and why: versions that ran after newer ones (a second install, such as the desktop app or an SDK script), updates not tested and their sessions, comparisons that could not reach significance, rare events seen too seldom to test, models with too little data, turns older than the window, and a history shorter than the window.
- `--json` prints the same result for a program: `headline`, `totals`, `thresholds`, `slices`, `tests` (every comparison, flagged or not), `flagged`, `stand_outs`, `confounders`, `untested`, `left_out`, and `notes`.

Open `references/metrics.md` to explain what a number measures and why it matters, and `references/statistics.md` when the user asks how sure the result is or why a visible change was not flagged.

## Report to the user

1. The headline, verbatim, in bold.
2. The flagged changes as a short table: where, what changed, before and after (per session), change, sessions before and after. Six rows at most, worse changes first.
3. The confounders, one line each, with what the rerun in step 3 showed.
4. Two or three next actions that fit the result:
   - Read what changed in the releases the change is placed in: the [Claude Code changelog](https://github.com/anthropics/claude-code/blob/main/CHANGELOG.md) or the [Codex releases](https://github.com/openai/codex/releases).
   - Narrow the check with `--project <path>` or `--by model`.
   - If it holds after the reruns, file it upstream with the numbers: `--json` for the data and `--svg` for the chart.
   - For tokens and money lost to habits rather than updates, run `session-waste-report`.

An example of the shape (the numbers are invented):

> **After Claude Code `2.1.270`, your agent reads 41% less before it edits and gets interrupted twice as often.**
>
> | Where | What changed | Before | After | Change | Sessions |
> |---|---|---|---|---|---|
> | after `2.1.270` | Reads before the first edit (session median) | 5 | 3 | -41% | 34, 29 |
> | after `2.1.270` | Interrupts per 100 turns (session mean) | 4.1 | 8.3 | +102% | 34, 29 |
>
> Nothing else changed at that update: same model, same projects. Next: read the `2.1.270` entry in the Claude Code changelog, and if it matches, file it with `--json` and `--svg`.

When nothing was flagged, say what was compared (versions, sessions, turns) and that small histories cannot show small changes; quote no percentages as findings.

Quote versions, models, and paths exactly as the report prints them, inside inline code: it shows the home folder as `~`, and it has already made any text taken from transcripts safe to display.

## Files

- `scripts/regress.py`: the check. Flags: `--harness`, `--by version|model|week`, `--since 90d`, `--project`, `--min-turns 30`, `--min-sessions 20` (at least 12), `--svg <path>`, `--json`, `--out <path>`, `--fail-on worse|any` (exit 1 when a flagged change usually means worse, or when anything is flagged).
- `scripts/transcripts.py`, `scripts/pricing.py`, `scripts/safe.py`: the shared reader for session files, the price table, and the text cleaner that puts transcript text in the report inside inline code; several skills in this repository use them.
- `references/metrics.md`: each number's definition and why it matters, next to the method of issue #42796.
- `references/statistics.md`: the test, the thresholds, the minimum samples, how changes are placed, and the limits.
