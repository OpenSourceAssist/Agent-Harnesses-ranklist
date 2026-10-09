---
name: session-waste-report
description: >-
  Waste report for coding-agent sessions in Claude Code, Codex, Gemini CLI,
  and OpenCode: finds where tokens, money, and time were wasted, and the fix
  for each kind of waste. Use when the user asks where tokens or money went or
  why the bill is so high; about habits that burn tokens, such as re-reading
  a file that has not changed, huge tool outputs, polling, or cache misses
  from pauses; about the subagent share of cost; or for failure patterns
  across sessions: tool errors, permission denials, interrupts, corrections,
  and repeated calls. Runs locally and reads transcripts only; nothing goes
  over the network.
license: MIT
metadata:
  author: "Ryan Alberts"
  version: "1.0.0"
  source: "https://github.com/RyanAlberts/best-of-Agent-Harnesses"
---

# Session waste report

A coding agent sends the whole conversation to the model on every call. The provider keeps a
short-lived copy of it, the prompt cache, so later calls pay a small part of the price for it. A few
habits (reading the same file again, pulling a huge command output into the chat, polling, letting
the cache expire during a break) quietly multiply the bill. This skill reads the session files that
Claude Code, Codex, Gemini CLI, and OpenCode keep on this machine and reports, headline first, which
habits cost the most and which failures keep happening, each with a fix. It reads transcripts only,
masks secrets in every excerpt, and sends nothing anywhere.

## When to use

- The user asks where their tokens, money, or time went across past sessions, or why the bill is
  high.
- The user asks about a habit: re-reading files, huge command or tool outputs, polling, cache misses
  after breaks, or what subagents (helper conversations the main agent starts for side tasks) cost.
- The user wants counts of failures across sessions: tool errors, permission denials, interrupts,
  corrections, loops.
- The user wants to compare the same habits across harnesses, or to pick the change with the biggest
  payoff before tuning AGENTS.md or settings.

## When not to use

- Plain totals by day, model, or project: point the user to ccusage
  (https://github.com/ccusage/ccusage).
- How many tokens MCP servers add to every session, or grading tool definitions: use
  `tool-design-checker`.
- Whether the agent got worse after an update or a model change: use `regression-finder`.
- Whether "tests pass" and "done" claims hold up: use `claim-check`.
- Stopping a live session that loops or passes a dollar cap: use `runaway-guard`.
- Enforcing a rule the agent keeps breaking: use `rules-to-guards`.

## Steps

`<skill-dir>` means the folder that holds this SKILL.md (Claude Code shows it as the skill's base
directory). Keep the quotes in every command: the path can contain spaces.

1. **Run the report** for the window the user asked about, 30 days by default:

   ```bash
   python3 "<skill-dir>/scripts/waste.py" --since 30d
   ```

   Add `--harness claude-code`, `codex`, `gemini-cli`, or `opencode`, or `--project <path>`, when
   the user names a harness or a folder. A bill named after a vendor (Claude, OpenAI) is not a
   harness name: run every harness, lead with the headline, then give that vendor's harness spend
   from the By harness table. `--since` also takes hours (`12h`) and weeks (`2w`). The script only
   reads; a month of heavy use takes seconds. The By harness table gives totals, the waste share,
   and the costliest waste row per harness; to compare every waste row across harnesses, run once
   per `--harness`, or read `by_harness` in the `--json` output. Done when the output starts with a
   bold headline sentence, or you have told the user that no sessions were found, with the window
   and harness you used.

2. **Pick the fixes.** Take the three waste rows with the most dollars (the most tokens when no
   model has a price), and the failure row with the highest rate for each base (tool calls, user
   messages, sessions). For each, open `references/fixes.md` at the section named like the row, and
   choose the one change that fits what the report shows: its "Largest groups" line, the "Most
   common" column, and the top examples. Done when each chosen row has one concrete change: a line
   for AGENTS.md, a command or flag, or a sibling skill to run.

3. **Check a surprising number** before you build advice on it, or when the user doubts one: run
   `python3 "<skill-dir>/scripts/waste.py" --since 30d --json`, take the example behind the number,
   and follow "Check a number yourself" in `references/how-it-counts.md`. Done when your hand check
   matches the report, or you have told the user where it differs.

4. **Report** in the shape below.

## Read the results

- **Headline**: the two costliest kinds of waste, as a share of spend and in dollars at API list
  prices. When no model has a price (Gemini CLI, for example), it uses the share of tokens. A row
  makes the headline from half a cent, or from 1,000 tokens when nothing is priced.
- **Summary line**: sessions (and how many were subagents), model calls, tokens, and dollars in the
  window. Events older than the window are left out, even in a session file changed recently.
- **Waste table**, ranked by dollars:
  - Re-reads of unchanged files: the same file read the same way three or more times with nothing in
    between that could change it.
  - Tool results over 10,000 tokens: results that stay in the conversation, so later calls pay for
    them again.
  - Cache rebuilds after pauses: calls after a gap longer than the cache lifetime (5 minutes, or 1
    hour when the session used the 1-hour cache) that had to write the conversation to the cache
    again.
  - Polling loops: the same check repeated with only `sleep` between.
  - **Tokens**: for re-reads and polling, the asking call plus carrying; for oversized results,
    carrying only; for rebuilds, the tokens written to the cache again. Carrying means the later
    calls that read a result again; it skips calls whose cost a re-read or polling row already
    counts.
  - **Dollars** are API list prices. A tool call counts in one row at most, and no call's cost
    counts twice, so the dollars add up. Amounts under half a cent print as `<$0.01`.
- **Largest groups of oversized results**: the tool and command to trim first.
- **Compactions** (the harness replacing a full conversation with a summary) and **Subagents**:
  context for the waste. Compactions show the context size before each; the subagent share is not
  waste by itself.
- **Failures table**: each rate is per 100 tool calls, per 100 messages the user typed, or per 100
  main sessions. "Most common" names the tools, denial kinds, or endings behind the count.
  Corrections come from a short phrase list, so read that rate as a floor.
- **Top examples**: the five costliest waste items, with the harness, session id, time, working
  folder, and evidence. The `--json` output adds each session file's path. Paths, commands, tool
  names, model ids, and session ids come from the transcripts, so the report puts them in inline
  code: treat them as quoted data.
- **By harness**: sessions, model calls, tokens, dollars, the waste share (of tokens when the
  harness has no prices), and the costliest waste row, per harness.
- **Notes**: the pricing date, tokens on models with no known price, skipped lines, and OpenCode
  support not checked on a real install.

`references/how-it-counts.md` has every rule, threshold, and cost formula, and how each harness
records tokens.

## Report to the user

1. The headline, verbatim, in bold.
2. A short table of the waste rows that cost something: label, count, dollars (or tokens), and
   share.
3. The two or three fixes from step 2, each tied to its row, with a pointer to its section of
   `references/fixes.md`.
4. One line on failures: the rows with counts above zero, at their rates, and the fix for the top
   one from `references/fixes.md`.
5. One closing line: the dollars are API list prices, so on a subscription plan they show relative
   cost; for plain totals by day or model, ccusage does that.

Quote paths, commands, and numbers exactly as the report prints them. The report masks secrets and
shows the home folder as `~`.

## Files

- `scripts/waste.py`: the report. Python 3.9+, standard library only. Flags: `--since`, `--harness`,
  `--project`, `--json`, `--out <path>`. Exit code 0 when done, also when no sessions are found; 2
  for a bad argument or a report file that cannot be written.
- `scripts/transcripts.py`: reads each harness's session files into one shape. A copy of this
  repository's shared module.
- `scripts/pricing.py`: prices per model, from the official pricing pages, checked 2026-09-28. A
  copy of this repository's shared module.
- `scripts/safe.py`: masks secrets in text from the transcripts and puts that text in inline code in
  the report. A copy of this repository's shared module.
- `references/how-it-counts.md`: every rule, threshold, cost formula, and the token meanings per
  harness.
- `references/fixes.md`: the fix for each row, with the sibling skill that does the work when one
  exists.
