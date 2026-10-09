# See where your coding agent wastes tokens and money

A report on your own Claude Code, Codex, Gemini CLI, and OpenCode sessions that shows which habits cost the most, such as re-read files, huge tool outputs, polling, and breaks that expire the prompt cache (the provider's cheap copy of the conversation), and which failures repeat, each with a fix.

## What you get

A sample report. Every number and name in it is invented; your agent prints its own.

```
**Last 30 days: 12% of spend ($172 at API prices) went to cache rebuilds after pauses, and $63 to tool results over 10,000 tokens.**

386 sessions (121 of them subagents) in Claude Code and Codex: 9,214 model calls, 1.6B tokens, $1,487 at API prices. For plain totals by day and model, run ccusage.

## Waste

| Waste | Count | Tokens | Dollars | Share of spend | Fix |
|---|---|---|---|---|---|
| Cache rebuilds after pauses | 118 pauses | 13.9M | $172 | 12% | Compact before breaks; start fresh |
| Tool results over 10,000 tokens | 74 results | 96.3M | $63 | 4% | Trim output; read line ranges |
| Re-reads of unchanged files | 41 re-reads | 12.8M | $19 | 1% | Read once; search or read a range |
| Polling loops | 3 loops | 2.2M | $4.35 | 0.3% | Wait inside one command |

Largest groups of oversized results: `Read` (52 results, 71.4M tokens, $47); `Bash git diff` (7 results, 9.6M tokens, $6.80).
Compactions: 17, with about 176.3k tokens of context before each. Fix: one task per session.
Subagents: 38% of spend ($565, 611M tokens) in 121 subagent sessions. Fix: narrow tasks, cheaper models.

## Failures

| Failure | Count | Rate | Most common | Fix |
|---|---|---|---|---|
| Tool errors | 377 | 3.41 per 100 tool calls | `Bash` 268, `Edit` 61, `Read` 19 | Fix the top failing command |
| Permission denials | 29 | 0.26 per 100 tool calls | user-rejected 22, permission-rule 7 | Allow safe commands; write the rules |
| User interrupts | 23 | 4.13 per 100 user messages |  | Write the reason down as a rule |
| Sessions that ended on an error or interrupt | 18 | 6.79 per 100 sessions | error 11, interrupt 7 | Check the state before closing |

## Top examples

1. Cache rebuilds after pauses: $5.40, 284k tokens. 1-hour 52-minute pause before this call; 284k tokens rebuilt.
   Claude Code session `8c1d2e4f` at 2026-09-14 16:05 UTC, in `~/code/shop`
2. Tool results over 10,000 tokens: $3.65, 3.1M tokens. `Read` returned about 16.9k tokens: `src/data/catalog.json`.
   Claude Code session `3b9e0f12` at 2026-09-20 09:41 UTC, in `~/code/shop`
```

The full report also has error streaks, corrections, identical-call loops, a table per harness, and notes on prices and anything it skipped. `--json` prints all of it for scripts.

## Install

```
npx skills add https://github.com/RyanAlberts/best-of-Agent-Harnesses/tree/main/skills/session-waste-report
```

In Claude Code, all ten at once:

```
/plugin marketplace add RyanAlberts/best-of-Agent-Harnesses
/plugin install harness-skills@agent-harnesses
```

Manual: copy `skills/session-waste-report/` into your agent's skills folder: `~/.claude/skills/` (Claude Code), `~/.agents/skills/` (Codex, Gemini CLI, Cursor, and OpenCode all read it).

## Use it

Ask your agent: "Where did my tokens go this month? Run the session waste report."

The agent runs the report, leads with its headline, and picks the two or three fixes with the biggest payoff. To run the script yourself:

```bash
python3 "$HOME/.claude/skills/session-waste-report/scripts/waste.py" --since 30d
```

Add `--harness codex` (or `claude-code`, `gemini-cli`, `opencode`) or `--project ~/code/shop` to narrow it, `--since 7d` or `--since 12h` for a shorter window, `--json` for every number, and `--out report.md` to save it.

## How it works

The script reads each harness's session files into one shape and prices every model call with the official list prices. Then it applies a fixed set of rules:

- **Re-reads**: the same file read the same way three or more times, with nothing between the reads that could change it: an edit, a command or tool call that names the file, any subagent (a helper conversation the main agent starts for a side task), a message from you, or a compaction (the harness replacing the conversation with a summary).
- **Oversized results**: a tool result that put more than 10,000 tokens (about 40,000 characters) into the conversation.
- **Cache rebuilds**: a model call more than 5 minutes after the one before (1 hour when the session uses the 1-hour cache) that had to write the conversation to the prompt cache again.
- **Polling loops**: the same check run three or more times with only `sleep` between.
- **Subagents**: their share of spend.
- **Failures**: tool errors, error streaks, permission denials, interrupts, correction phrases, identical-call loops, and sessions that ended on an error or an interrupt, each as a rate per 100.

A wasted tool call costs twice: the model call that asked for it, and its result, which every later call in the session reads again. The report counts both, so a 15,000-token log that stays in a long session shows its real cost. Each tool call lands in one row at most, and the carrying skips any call whose cost a row already counts, so the dollars add up. [How the report counts](references/how-it-counts.md) gives every rule, threshold, and formula, with a worked example and the steps to check any number by hand. [Fixes](references/fixes.md) has the change for each row.

## Works with

| Harness | What it reads | Support |
|---|---|---|
| Claude Code | `~/.claude/projects/` sessions and subagent files | full; run on real sessions |
| Codex | `~/.codex/sessions/` and `archived_sessions/` rollout files | full; run on real sessions; compressed `.jsonl.zst` files are skipped |
| Gemini CLI | `~/.gemini/tmp/*/chats/` session files | full rules; tokens only, since no Gemini price was confirmed |
| OpenCode | `~/.local/share/opencode/opencode.db`, read-only | built from the documented layout; not checked on a real install |
| Cursor | skipped | its transcripts lack tool results, token counts, and times |

It needs Python 3.9 or newer and nothing else. It reads plain files, and was tested on macOS.

## Limits

- It covers what the transcripts record. Some calls never reach them, such as the one that writes a compaction summary.
- Result sizes are estimated as characters divided by 4. Newer Claude models make about 30% more tokens from the same text, so those sizes can run low; images count as zero.
- A re-read counts only when the same read repeats within one turn. Reading another part of a file, re-reading after you replied, or reading through `cat` in Claude Code does not count.
- The cache rule uses Anthropic's cache lifetimes as the mark of a pause for every harness, and counts only the part of the conversation the next call did not read back from the cache.
- Dollars are API list prices, checked 2026-09-28. Subscription plans, batch discounts, and long-context rates are outside the model.
- Corrections come from a short list of phrases, so the count is a floor.
- The session formats are internal to each harness and change between versions. Unknown records are skipped and counted in a note.

## Privacy

- **Read**: the session files your harnesses keep on this machine, for the window you choose. The OpenCode database is opened read-only.
- **Printed**: counts, tokens, and dollars, plus, for the top five examples, the session id, time, working folder (your home folder shown as `~`), and the file path or command involved, with secrets masked and each excerpt cut to 160 characters.
- **Only in `--json`**: the path of each example's session file. Claude Code names its session folders after the working folder, so that path can spell out your user name.
- **Sent**: nothing. The script makes no network calls.
- **Written**: nothing, unless you pass `--out`.

## Related

- [runaway-guard](../runaway-guard/): stops a live session that loops, keeps failing, or passes a dollar cap.
- [rules-to-guards](../rules-to-guards/): turns the rules your agent keeps breaking into tested hooks.
- [regression-finder](../regression-finder/): shows whether these numbers moved after a harness update or a model change.
- [tool-design-checker](../tool-design-checker/): how many tokens your MCP tools add to every session.
- [Context files, skills, and tool search](../../comparisons/progressive-disclosure.md): why tool output is one of the three ways context fills up.
- [One AGENTS.md for every coding agent](../../templates/agents-md/): where to write the fixes down.
- Credit: [ccusage](https://github.com/ccusage/ccusage) for totals across agents; [token-dashboard](https://github.com/nateherkai/token-dashboard) and [claude-context-optimizer](https://github.com/egorfedorov/claude-context-optimizer), which flag repeated reads, large results, and cache re-warm cost in Claude Code; Anthropic's [prompt caching guide](https://platform.claude.com/docs/en/build-with-claude/prompt-caching) for the cache lifetimes and token fields.
