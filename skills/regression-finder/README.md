# Find the update where your coding agent started working differently

Splits your own Claude Code or Codex sessions by version, model, or week, measures how the agent works in each, and places the change at the update, or within the span of versions, where the numbers moved, along with anything else that changed then.

## What you get

A sample report. Every number and version in it is invented; your agent prints its own.

```
**After Claude Code `2.1.270`, your agent reads 41% less before it edits and gets interrupted twice as often.**

Claude Code, last 90 days, split by version: 2,412 turns in 318 sessions across 21 versions, with 14 updates
tested. Each session counts once: the test compares one value per session, and a change is flagged when p is
under 0.01 after adjusting for the number of tests and the change is at least 20%.

## Flagged changes

| Where           | What changed                                 | Before (per session) | After (per session) | Change | Usually means | Compared               | Sessions (before, after) | Adjusted p |
|-----------------|----------------------------------------------|----------------------|---------------------|--------|---------------|------------------------|--------------------------|------------|
| after `2.1.270` | Reads before the first edit (session median) | 5                    | 3                   | -41%   | worse         | `2.1.268` vs `2.1.270` | 34, 29                   | 0.0021     |
| after `2.1.270` | Interrupts per 100 turns (session mean)      | 4.1                  | 8.3                 | +102%  | worse         | `2.1.268` vs `2.1.270` | 34, 29                   | 0.0068     |

## What else changed at the same point

These numbers show what changed, not why: the kind of work may have changed too.

- after `2.1.270`: One project, `~/code/api`, holds 64% of the turns after. Run again with
  `--project '~/code/api'` to check the change within that project alone.

## By version

| Version   | Days                     | Sessions | Turns | Reads before edit | Reads per edit | Interrupts/100 | Cost/turn |
|-----------|--------------------------|----------|-------|-------------------|----------------|----------------|-----------|
| `2.1.262` | 2026-08-30 to 2026-09-06 | 19       | 141   | 5                 | 4.2            | 3.5            | $0.184    |
| `2.1.268` | 2026-09-06 to 2026-09-12 | 15       | 118   | 5                 | 4.0            | 4.2            | $0.191    |
| `2.1.270` | 2026-09-12 to 2026-09-19 | 29       | 244   | 3                 | 2.6            | 8.3            | $0.163    |

## Notes

- Left out `2.1.199`: it ran 2026-09-01 to 2026-09-18, after newer versions, which usually means a second
  install such as the desktop app or an SDK script. Run with --by week to see it in time order.
- Not tested: `2.1.265` (4 sessions): each side of an update needs 20 sessions and 30 turns, even with
  neighbors added.
```

The real report has a column for each of the eleven numbers (reads before the first edit, reads per edit, edits to files not read earlier, edits per turn, failed tool calls, interrupts, corrections, output tokens, reasoning share, tool calls, and cost per turn). When the test had to borrow neighboring versions, "Where" reads "between `2.1.260` and `2.1.270`": the change is placed within that span. With `--svg` it also draws one small line chart per flagged number, and with `--json` it prints the same result for a program.

## Install

```
npx skills add https://github.com/RyanAlberts/best-of-Agent-Harnesses/tree/main/skills/regression-finder
```

In Claude Code, all ten at once:

```
/plugin marketplace add RyanAlberts/best-of-Agent-Harnesses
/plugin install harness-skills@agent-harnesses
```

Manual: copy `skills/regression-finder/` into your agent's skills folder: `~/.claude/skills/` (Claude Code), `~/.agents/skills/` (Codex, Gemini CLI, Cursor, and OpenCode all read it).

## Use it

Ask your agent: "Did Claude Code get worse after the last update? Check my sessions."

The agent runs the check, leads with the headline, and reruns it to rule out what else changed at the same point, such as a new model or different projects. To run the script yourself:

```bash
python3 "$HOME/.claude/skills/regression-finder/scripts/regress.py"
```

Add `--harness codex` for Codex, `--by model` or `--by week` to split another way, `--project <folder>` to compare like with like, `--since 180d` for a longer window, `--svg chart.svg` for the chart, and `--fail-on worse` to exit 1 when a flagged change usually means worse (useful in a scheduled job).

## How it works

- **Turns.** Each prompt you typed, plus everything the agent did until your next prompt, is one turn. Work done by subagents counts in the turn that started it.
- **Eleven numbers**, two of them measured as in [issue #42796](https://github.com/anthropics/claude-code/issues/42796) (reads per edit, and edits to files not read earlier in the session) and the rest this skill's own, such as reads before the first edit, interrupts, and cost. The [metrics reference](references/metrics.md) defines each one.
- **Versions in time order.** Turns are grouped by the harness version recorded with each prompt (Claude Code) or session (Codex). A version that ran after newer ones, which usually means a second install such as the desktop app or an SDK script, is left out and named in the notes.
- **One test per update.** The sessions just before an update are compared with the sessions just after it. An update with few sessions borrows its neighbors until each side has 20 sessions and 30 turns, and then the change is placed within the span of versions it covers.
- **Each session counts once.** Turns from one session resemble each other, so the test (Mann-Whitney U, with Fisher's exact test for rare events) runs on one value per session, and the sizes shown come from the same values. In our simulations of histories with no real change, testing turns one by one raised a false alarm in 147 of 150 of them; the full method raised one in about 1 of 100, also when a second install ran an old version the whole time.
- **Flagged** means: p under 0.01 after adjusting for the number of tests (Benjamini-Hochberg, over tests that could reach significance, with overlapping windows counted once), a change of at least 20% per session, and both pointing the same way. The [statistics reference](references/statistics.md) has the details and the limits.
- **Confounders named.** For each flagged update the report checks whether the model, the projects, the length of sessions, or the share of scripted runs changed at the same point, or whether both sides ran at the same time. It says so in the headline and suggests the rerun that separates them.

## Works with

| Harness | Split by version | Split by model or week | Support |
|---|---|---|---|
| Claude Code | yes, from the version on each prompt record | yes | full; smoke-tested on macOS |
| Codex | yes, from `cli_version` (once per session file) | yes | full; smoke-tested on macOS |
| Gemini CLI | no: its transcripts record no version | yes | tested on synthetic files and a small real history |
| OpenCode | yes, from the session's version | yes | not verified on a real install |
| Cursor | no | no | keeps no usable transcripts |

It needs Python 3.9 or newer and nothing else to install. It has been run on macOS.

## Limits

- It shows what changed, not why. A new model, different projects, or scripted runs can move the numbers as much as an update; the report names the ones it can see, and it cannot see a change in how hard your tasks were.
- Small changes need many sessions to show. Even a 50% drop in reads before editing is rarely found with a few sessions a day. With fewer than 20 sessions on each side of an update, it reports that the history is too short instead of guessing.
- A change found by borrowing neighboring versions is placed within a span of versions, not at one version.
- A second install on an old version, or a rollback to an old version after an update, is left out of the version split and named in the notes; `--by week` shows those sessions in time order.
- It counts edits made through edit tools. Files changed by shell commands (`sed -i`, `cat > file`) are left out of the edit counts.
- Corrections come from a short list of phrases, so many corrections go uncounted; the same list applies to every version.
- Claude Code deletes transcripts after 30 days by default (`cleanupPeriodDays`), except sessions from Claude Desktop, so a 90-day window may hold 30 days. The report says so when it happens.
- Codex records its version once per session file, so a Codex session resumed after an update keeps its first version.
- Costs use list prices checked on 2026-09-28; turns with a model that has no known price are left out of the cost number.

## Privacy

- **Read**: session files under `~/.claude/projects/`, `~/.codex/sessions/`, `~/.gemini/tmp/`, or the OpenCode database, only those changed within the window. Prompt text is read on your machine only to spot corrections.
- **Printed**: counts, rates, token totals, costs, version numbers, model names, and project folders (your home folder shown as `~`). Never prompt text, commands, or file contents.
- **Sent**: nothing. It makes no network calls.
- **Written**: nothing, unless you pass `--out` or `--svg`, which write the one file you name.

## Related

- [session-waste-report](../session-waste-report/): where tokens and money go, whatever the version.
- [Why the harness matters more than the model](../../comparisons/why-the-harness-matters.md): why a harness update alone can change how an agent works.
- [harness-test-drive](../harness-test-drive/): compare harnesses on tasks from your own git history.
- The method comes from [anthropics/claude-code#42796](https://github.com/anthropics/claude-code/issues/42796) by Stella Laurenzo, a one-off analysis of 6,852 session files that measured the same kinds of numbers. [claude-session-analyzer](https://github.com/lucemia/claude-session-analyzer) repeats that analysis for Claude Code, split into two date halves.
- Statistics: Mann and Whitney (1947) for the rank test, Fisher (1935) for the exact test, Tarone (1990) for setting aside tests that cannot pass, and Benjamini and Hochberg (1995) for the adjustment across tests.
