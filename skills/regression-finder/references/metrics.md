# What each number measures

Every number starts from **turns**: one prompt the user typed, plus everything the agent did until the next typed prompt. Work done by subagents (helper agents the main agent starts) counts in the turn that started them. Messages the harness adds on its own, such as context files, reminders, and hook output, do not start a turn.

The numbers follow the analysis in [anthropics/claude-code#42796](https://github.com/anthropics/claude-code/issues/42796) (opened 2026-04-02, 3,287 reactions when checked on 2026-09-28). Its author mined 6,852 Claude Code session files and 234,760 tool calls from January 30 to April 1, 2026, and tied a quality drop to a change in how the agent worked: it read less before editing and was interrupted more. Two of the numbers below are measured the way that issue measured them; the others are this skill's own, and each section says which.

Each number is shown two ways:

- **Per session**, in the flagged changes: one value per session (the mean of its turns, or for ratios and shares the session's own total ratio), then the median or the mean of those session values. The test ranks the same per-session values, so the size of a change and its p value come from the same data.
- **Turn by turn**, in the By version table: every turn of the slice pooled together.

| Number | Per session | Usually a bad sign when |
|---|---|---|
| Reads before the first edit | median of sessions | it falls |
| Reads per edit | median of sessions | it falls |
| Edits to files not read earlier in the session (%) | mean of sessions | it rises |
| Edits per turn | median of sessions | no clear direction |
| Failed tool calls (%) | mean of sessions | it rises |
| Interrupts per 100 turns | mean of sessions | it rises |
| Corrections per 100 prompts | mean of sessions | it rises |
| Output tokens per turn | median of sessions | no clear direction |
| Share of output spent reasoning (%) | mean of sessions | it falls |
| Tool calls per turn | median of sessions | no clear direction |
| Cost per turn | median of sessions | it rises |

## Reads and edits

A **read** is a call that looks at files without changing them:

- File-reading and search tools: Claude Code `Read`, `Grep`, `Glob`, and `LS`; Codex `view_image`; Gemini CLI `read_file`, `glob`, and its search tools; OpenCode `read`, `grep`, `glob`, and `list`.
- Shell commands that only inspect, such as `cat`, `head`, `tail`, `nl`, `wc`, `ls`, `tree`, `find` (without `-delete` or `-exec`), `fd`, `rg`, `grep`, `sed` without `-i`, `awk`, `jq`, `diff`, and `git log`, `show`, `diff`, `status`, `blame`, or `grep`. A command counts only when every part of it inspects: `cd src && rg parse` is a read, `rg parse > out.txt` and `rg parse; npm test` are not.
- Codex writes most shell work as a short script (code mode). The script counts as a read when every command it passes to `exec_command` is a read.

An **edit** is a call to a tool that edits or writes a file: Claude Code `Edit`, `MultiEdit`, `Write`, and `NotebookEdit`, Codex `apply_patch`, and the Gemini CLI and OpenCode equivalents. Files changed through shell commands (for example `sed -i`) are not counted as edits, so a harness that moves editing into the shell will look like it edits less.

### Reads per edit (as in #42796)

All reads in a session divided by all its edits, for sessions with at least one edit. #42796 measured this ratio over all tool calls in a period and saw it fall from 6.6 reads per edit to 2.0.

### Edits to files not read earlier in the session (as in #42796)

The share of edits made to a file the session had not read before. A file counts as known once any read in the session names it (a `Read` call, or a shell read such as `cat src/a.py` or `sed -n 1,80p src/a.py`) or once the session has edited it. Files are matched on the last two parts of their path, so `/work/app/src/a.py` and `src/a.py` match. Whole-file writes (`Write`), which often create new files, are left out. In #42796 this share rose from 6.2% to 33.7% of edits, and those edits broke surrounding code.

### Reads before the first edit (this skill's own measure)

The reads a turn makes before its first edit, for turns that edit. It is a proxy for "does the agent look before it changes something" at the level of one request; #42796 did not measure it this way.

## Edits per turn

How many edit calls a turn makes. More edits can mean bigger tasks or trial and error on the same file (#42796 lists repeated edits to one file as a symptom); fewer can mean smaller tasks or an agent that stops early. Read it together with the other numbers.

## Failed tool calls

A call fails when the tool reports an error or a shell command exits with a nonzero code. Calls blocked by a person, a permission rule, a hook, or an automatic reviewer do not count as failures, and neither do calls the user interrupted: those are choices, not failures.

## Interrupts per 100 turns

An interrupt is the user stopping the agent: Claude Code records `[Request interrupted by user]` (Escape), and Codex records an aborted turn. This skill counts them per 100 turns; #42796 counted them per 1,000 tool calls, where they rose from 0.9 to 11.4. The two rates are not comparable number for number.

## Corrections per 100 prompts

A correction is a typed prompt that tells the agent it got something wrong. The script matches a short list of phrases chosen for precision over recall, so it misses many corrections but rarely counts a prompt that is not one. The same list applies to every group, so missed phrases affect every group alike.

- At the start of a prompt: "no," "nope," "wrong," "that's wrong," "that's not what I asked," "undo that," "revert that," "why did you," "you didn't," "you forgot," "you broke," "I said," "I told you," "that didn't work," "it still doesn't work," "still failing," "try again," "stop," "wait," "don't do that."
- Anywhere in a prompt: "not what I asked," "I already told you," "you're not done," "read the file first," "you didn't read."

In #42796 the share of prompts with frustration words rose from 5.8% to 9.8%.

## Output tokens and the share spent reasoning

Output tokens include the model's reasoning (thinking) tokens and the output of subagents. The reasoning share is reasoning tokens divided by output tokens. Codex records reasoning tokens for every response. Claude Code records them only for some models and versions, so a slice where most turns record none shows "n/a", and its turns are left out of both sides of every reasoning comparison instead of counting as 0%. #42796 estimated that thinking depth had fallen by about two thirds by late February 2026, before the quality drop was reported in March.

## Tool calls per turn

All tool calls in the turn, subagents included. More calls can mean more thorough work or more thrashing; read it with the reading numbers and the failure rate.

## Cost per turn

Tokens times the list prices in `scripts/pricing.py` (checked 2026-09-28): uncached input, cache reads, cache writes, and output, each at its own rate. A turn that used a model without a known price is left out of this number, and the report names those models. On a subscription plan you do not pay per token, but the number still tracks how much work a turn takes.

## Where the numbers come from

`scripts/transcripts.py` reads the session files: Claude Code `~/.claude/projects/`, Codex `~/.codex/sessions/`, Gemini CLI `~/.gemini/tmp/`, and the OpenCode database (not checked on a real install). It counts each API response once, drops copies that a forked or resumed session repeats, and skips events older than the window.

The harness version and the way Claude Code was started (the entrypoint: the command line, the desktop app, or an SDK script) come from each Claude Code prompt record, so a session resumed after an update counts its later turns under the newer version. Codex records its version once per session file (`cli_version`), so a Codex session keeps the version it started with. Gemini CLI records no version: split its sessions by model or week instead.
