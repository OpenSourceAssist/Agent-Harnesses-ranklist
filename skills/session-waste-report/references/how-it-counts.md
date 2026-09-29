# How the report counts

Every number in the report comes from one of the rules below, applied to the session files on your machine. Each rule names what you can check in a transcript to get the same number. [fixes.md](fixes.md) says what to change for each row.

## What it reads

| Harness | Files | Notes |
|---|---|---|
| Claude Code | `~/.claude/projects/<project>/<session>.jsonl`, and subagent files in `<session>/subagents/` | `CLAUDE_CONFIG_DIR` moves the folder |
| Codex | `~/.codex/sessions/YYYY/MM/DD/rollout-*.jsonl` and `~/.codex/archived_sessions/` | `CODEX_HOME` moves the folder; compressed `.jsonl.zst` files are skipped with a note |
| Gemini CLI | `~/.gemini/tmp/<project>/chats/session-*.jsonl` (and older `.json` files) | |
| OpenCode | `~/.local/share/opencode/opencode.db`, opened read-only | Built from the documented database layout; not checked on a real install |
| Cursor | nothing | Its transcripts have no tool results, token counts, or times |

- **Window**: files changed in the window are read (default: the last 30 days), and then every event older than the window is left out. A long session that you continued recently counts only its recent part.
- **Forks**: a forked or resumed Claude Code session starts with a copy of the earlier records. Each record counts once.
- **Split records**: Claude Code writes one model response as several records that share a message id. The report counts the response once, with the usage on its last record, and skips harness-made messages (model `<synthetic>`).
- **Unreadable lines** and unknown record types are skipped and counted in a note. They never stop the report.

The formats are internal to each harness and change between versions. The field names used here come from the harness docs and source code (for example https://code.claude.com/docs/en/sessions and https://github.com/openai/codex/blob/main/codex-rs/protocol/src/protocol.rs) and from read-only checks of real files.

## Tokens and dollars

Every harness is converted to the same four token counts, with Anthropic's meanings:

| Count | Meaning |
|---|---|
| input | prompt tokens that were not read from or written to the cache |
| cache reads | prompt tokens read from the prompt cache |
| cache writes | prompt tokens written to the cache (the 5-minute and 1-hour parts, when known) |
| output | the model's reply, including reasoning or thinking tokens |

- **Claude Code** records these four directly (`input_tokens`, `cache_read_input_tokens`, `cache_creation_input_tokens` with its 1-hour part, `output_tokens`).
- **Codex** records OpenAI counts, where cached tokens sit inside input and reasoning sits inside output. Input becomes `input_tokens - cached_input_tokens`; cache reads are `cached_input_tokens`; output stays as recorded. The report uses `token_usage_record` lines (one per response id) when a file has them, and otherwise `token_count` events with repeated totals removed. Codex records no cache writes.
- **Gemini CLI** records `tokens.input` (which includes the cached part), `tokens.cached`, `tokens.output`, and `tokens.thoughts`. Input becomes `input - cached`; output becomes `output + thoughts`. No cache writes are recorded.
- **OpenCode** records input, output, reasoning, and cache reads and writes per model step.

**Tokens** in the report are input + cache reads + cache writes + output, added over every model call.

**Dollars** are API list prices per 1 million tokens, checked 2026-09-28, from https://platform.claude.com/docs/en/about-claude/pricing and https://developers.openai.com/api/docs/pricing. Cache reads cost 10% of input on most Claude models, 5% on Opus 5.5, and 2.5% on Fable 5.1. Cache writes cost 1.25 times input for the 5-minute cache and 2 times input for the 1-hour cache; writes whose 1-hour part is unknown use the 5-minute price. Models with no listed price (Gemini models, and Codex's `codex-auto-review`) are counted in tokens and named in a note, with no dollars. Batch discounts, fast mode, regional pricing, and long-context rates are not applied. On a subscription plan you pay a flat fee, so read the dollars as relative cost.

## The two costs of a tool call

A tool call that should not have happened costs twice:

1. **The model call that asked for it.** Its tokens and dollars, split evenly across the tool calls that model call asked for. When that call was also a cache rebuild, the rebuild's extra dollars are taken out first, because the cache-rebuild row counts them.
2. **Carrying its result.** The result's text joins the conversation, and every later model call in the session reads it again until the next compaction or the end of the session.
   - Tokens of a result: its characters divided by 4. A result cannot add more than the prompt grew: if the results that reach one model call add up to more tokens than that call's prompt grew since the previous call, each result is scaled down by the same factor. This matters for Codex, which keeps a command's full output in the file but can shorten what the model sees.
   - Carried tokens: the result's tokens times the number of model calls that carried it (from the next call to the next compaction or the end of the session).
   - Carried dollars: the next call pays its own price for prompt tokens it did not read from the cache (its cache-write and uncached-input cost divided by those tokens); each later call pays its cache-read price.
   - Charged calls are skipped. A model call that asked for a re-read, a polling check, or a `sleep` in a polling loop is charged: that row counts its cost (its share of it, when the call asked for several tools), so carrying adds nothing at that call, neither tokens nor dollars. If a result entered the conversation at a charged call, the next uncharged calls count it at their cache-read price. This way no dollar is counted twice, and the waste rows add up to no more than the spend.

Worked example: a command prints 60,000 characters, so 15,000 tokens. The next call (Opus 5.5) wrote 16,000 new tokens for $0.08 and so paid $5.00 per million new tokens; one more call follows at $0.20 per million for cache reads. Neither call is charged. Carried tokens: 15,000 x 2 = 30,000. Carried dollars: 15,000 x ($5.00 + $0.20) / 1,000,000 = $0.078.

## Waste rows

Each tool call counts in one waste row at most. When two rows fit, the order is: polling loops, then re-reads, then oversized results.

### Re-reads of unchanged files

- **Rule**: three or more reads of the same file, read the same way (the same tool input, such as the same `offset` and `limit`, or the same shell command), with no possible change in between. Every read after the first is a re-read.
- **A possible change**: an edit or write of that file; a shell command whose text contains the file's name; an MCP call or any other tool call whose input contains the file's name; any subagent call (a subagent may have edited any file); a message you typed (you may have edited the file yourself); or a compaction (the text left the conversation).
- **Not counted**: reads that failed or were denied, and reads of a different part of the file.
- **Reads it sees**: the Read tool (Claude Code), `read_file` and `read_many_files` (Gemini CLI), `read` (OpenCode), and Codex shell commands that Codex itself marks as file reads (such as `cat`, `sed -n`, `nl`). A Claude Code `cat` through Bash is not seen as a read.
- **Cost**: both costs of each re-read.

### Tool results over 10,000 tokens

- **Rule**: a result that put more than 10,000 tokens into the prompt (characters divided by 4, after the growth cap), and that at least one later model call carried.
- **Cost**: the carrying cost only; the call itself was needed.
- **Groups**: by tool, and for shell commands by program and subcommand: `cd app && git diff --stat` groups as `git diff`, `cat big.log` as `cat`.

### Cache rebuilds after pauses

- **Rule**: a model call that comes more than 5 minutes after the previous model call in the same session, or more than 1 hour once the session has written to the 1-hour cache, with no compaction between them. These are Anthropic's cache lifetimes (the usage fields name them `ephemeral_5m` and `ephemeral_1h`); the same gap marks a pause for every harness.
- **Rebuilt tokens**: the part of the cache that the previous call left and this call did not read back: the smaller of (the previous call's cache reads plus cache writes) and (this call's whole prompt), minus this call's cache reads, never below zero. Codex and Gemini CLI record no cache writes, because their providers cache the whole prompt on their own, so for them the previous call's whole prompt counts as cached. A pause after which the cache still held the conversation rebuilds nothing and is not counted. A session that never read from or wrote to the cache (every cache count is zero) has no rebuilds: nothing was cached, so a pause loses nothing.
- **Cost**: rebuilt tokens times (this call's price per prompt token it did not read from the cache, minus its cache-read price): what the pause cost beyond reading the same tokens from a warm cache.

### Polling loops

- **Rule**: three or more calls of the same tool with the same input (for shell commands, the same command, ignoring a `sleep` before or after it in the same command), with only `sleep` commands between them and at least one wait: a `sleep` call between two checks, or a `sleep` inside the command. A message you typed or a compaction ends the loop.
- **Cost**: both costs of every call after the first check, counting the checks and the `sleep` calls between them.
- Without any waiting, the same run is an identical-call loop (see Failures).

### Compactions

- **Count** of compactions, and the prompt size of the last model call before each one ("context before"). There is no dollar figure: Claude Code does not write the call that makes the summary to the transcript.

### Subagent share

- **Rule**: the tokens and dollars of subagent sessions (Claude Code subagent files, Codex subagent threads, Gemini CLI subagent chats, OpenCode child sessions), as a share of all tokens and all dollars. This is context, not waste.

## The headline and the by-harness table

- **Headline**: the two waste rows with the most dollars, each of at least half a cent ($0.005). When no waste row has a dollar figure (every model involved is unpriced), the two rows with the most tokens, each of at least 1,000. With neither, the headline says the waste stayed under those amounts, or that there was none.
- **Small amounts**: dollar amounts under half a cent print as `<$0.01`, and shares under 0.1% as `<0.1%`.
- **By harness**: sessions, model calls, tokens, dollars, the waste share, and the costliest waste row for each harness. The waste share is of spend, or of tokens for a harness with no priced model. `--json` lists every waste row per harness under `by_harness`.
- **Examples**: the markdown report names each example's session id, time, and working folder. The session file's path appears only in `--json`, because Claude Code names its session folders after the working folder, which can spell out the user name.

## Failure rows

Rates are per 100 tool calls, per 100 messages you typed in main sessions (not subagents, not text the harness added), or per 100 main sessions.

| Row | Rule | Rate per 100 |
|---|---|---|
| Tool errors | Failed tool calls, not counting denials and interrupted calls. Claude Code marks a Bash command that exits with an error as failed; Codex, a command with a non-zero exit code or a failed script; Gemini CLI, status `error`. Broken down by tool. | tool calls |
| Error streaks | Three or more failed tool calls in a row. A call that succeeds, is denied, or is interrupted ends the streak, as does a typed message or a compaction. | tool calls |
| Permission denials | Blocked tool calls, by kind: `user-rejected`, `permission-rule`, `hook`, `auto-reviewer` (Claude Code auto mode, Codex approval reviewer), `other` (a Gemini CLI cancel). Claude Code records the kind in `toolDenialKind`; Codex only in the output text. | tool calls |
| User interrupts | Claude Code's "[Request interrupted by user]" messages, Codex `turn_aborted` events, OpenCode aborted messages, in main sessions. | messages you typed |
| User corrections | Messages you typed that match the phrase list below. | messages you typed |
| Identical-call loops | Three or more calls of the same tool with the same input in a row, with no other call and no waiting between them. Repeated reads count as re-reads instead. | tool calls |
| Sessions that ended on an error or interrupt | Main sessions whose last event (not counting text the harness added) is a failed call or an API error ("error"), or an interrupt, a denied call, or an interrupted call ("interrupt"). | main sessions |

### The correction phrases

Matched without regard to case, at the start of a message, after an optional "no,", "nope,", "wait,", "hmm,", "ugh," or "argh,":

- "that's / that is / this is / it's / it is" followed by "wrong", "not right", "incorrect", or "not what I asked / wanted / meant / said"
- "wrong", "incorrect"
- "you didn't / did not / forgot / missed / broke / ignored / skipped / never"
- "why did you", "why are you", "why would you"
- "I said", "I told you", "I asked you", "I asked for", "I meant"
- "stop that", "stop doing that", "don't do that", "do not do that" (or "this")
- "undo" or "revert" followed by "that", "this", "it", "your", or "the last"
- "not what I asked / wanted / meant / said"
- "it / this / that (still) doesn't / don't / didn't work"

Matched anywhere in a message: "I told you", "I already told you", "as I said", "you're not listening", "still doesn't / don't / didn't work", "still broken / failing / wrong / not working / nothing", "stop screwing", "stop messing".

The list is short on purpose, so a match is almost always a correction. It misses corrections worded another way, so read this rate as a floor.

## What the counts leave out

- **Estimates**: characters divided by 4 is an estimate. Claude models from 4.7 on produce about 30% more tokens for the same text, so result sizes can run low.
- **Images** in tool results count as zero tokens: the transcript text has no size for them.
- **Calls outside the transcripts**, such as the one that writes a compaction summary. The report covers what the transcripts record.
- **Your own edits** in an editor are invisible, which is why a typed message ends a run of re-reads.
- **Carrying stops only at a compaction.** If a harness clears old tool results in some other way, the carried cost of those results runs high.

## Check a number yourself

1. Run with `--json` and take an example: it has the session file (`path`), the time, and the evidence.
2. Open the file and find the model calls around that time. In Claude Code, keep the last record of each message id.
3. Apply the rule above to the usage fields and timestamps. For a cache rebuild: take the previous call's cache reads plus cache writes, the smaller of that and this call's prompt, minus this call's cache reads, then multiply by the price difference from `scripts/pricing.py`.
