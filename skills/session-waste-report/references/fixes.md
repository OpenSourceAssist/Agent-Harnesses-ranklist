# Fixes for each kind of waste and failure

One section per row of the report, in the order the report uses. Each section says what the row means and names the change that removes it, plus the sibling skill that does the work when one exists. [how-it-counts.md](how-it-counts.md) explains how each number is measured.

## Re-reads of unchanged files

The agent read the same file, the same way, three or more times in one turn, and nothing changed the file in between. The text was still in the conversation, so each extra read paid for a model call and for the same tokens again.

- Add a line to AGENTS.md or CLAUDE.md: "Read a file once per task. For one detail, search it (`rg -n`) or read a line range."
- Split very long files, so one read covers what the agent needs.
- A re-read right after a compaction is expected: the summary dropped the text. The report does not count those.

## Tool results over 10,000 tokens

One tool result put more than 10,000 tokens (about 40,000 characters) into the conversation. Every later model call in the session pays to read those tokens again.

- Trim command output where it starts: `| tail -n 50`, `| head -n 50`, `| grep -m 20 error`.
- Use quiet flags: `pytest -q`, `npm test --silent`, `git diff --stat` before a full `git diff`.
- Read files by line range: the Claude Code Read tool takes `offset` and `limit`; in a shell, `sed -n '1,120p' file`.
- For an MCP tool that returns huge results, look for a limit or page parameter. [tool-design-checker](../../tool-design-checker/) finds tools that have none.
- The report's "Largest groups" line names the tool and command to fix first.

## Cache rebuilds after pauses

Providers keep the start of a conversation in a prompt cache, so the next call reads it for a small part of the normal price. Anthropic keeps it for 5 minutes, or 1 hour when the session writes to the 1-hour cache. After a longer pause, the next call writes the whole conversation to the cache again at full price.

- Before a break in a long session, run `/compact` (Claude Code and Codex both have it), so the next call rebuilds a short summary instead of the whole conversation.
- To pick up old work after hours or days, start a new session with a short note of where you left off, instead of resuming a very long one.
- Run commands that take more than a few minutes in the background, so the model is not left waiting past the cache lifetime.
- Every rebuild costs in proportion to the conversation size, so shorter sessions (see Compactions) shrink every rebuild.

## Polling loops

The agent ran the same check again and again with only `sleep` between, for example to wait for a CI run. Each check is a new model call that reads the whole conversation again.

- Wait inside one command: `gh run watch <run-id>` or `gh pr checks --watch` for CI, or a loop with a limit for anything else: `sh -c 'i=0; until <check> || [ $i -ge 60 ]; do i=$((i+1)); sleep 15; done'`.
- Ask the agent to start long jobs in the background and to check once when they finish.
- For loops that do not stop, [runaway-guard](../../runaway-guard/) stops the session when the same call repeats.

## Compactions

The conversation filled the model's context window, and the harness replaced it with a summary. The report shows how big the context was before each one. A long context makes every call cost more, and a compaction loses detail that the agent then has to find again.

- Start a new session for each separate task (`/clear` in Claude Code).
- Compact on purpose at a natural break, with a note on what to keep: `/compact keep the failing test names and the plan`.
- Cut oversized tool results and re-reads: they fill the context fastest.

## Subagent share

Subagents are separate conversations that the main agent starts for side tasks. Their spend is not waste by itself: they keep searches and large reads out of the main conversation. A large share with small results can mean the agent hands off too much.

- Give subagents narrow tasks with file paths, so they do not explore the whole project again.
- Use a cheaper model for search-only subagents. A Claude Code subagent definition can name its own model.

## Tool errors

Tool calls that failed: a command that exited with an error, an edit whose text was not found, a file that does not exist. Failing tests during test-driven work are expected. The same command failing again and again is not.

- Start with the tools in the "Most common" column, and fix the top failing command first.
- If the agent guesses commands (a wrong script name, the wrong test runner), write the right ones in AGENTS.md. [agents-md-checker](../../agents-md-checker/) checks that the documented commands still work.

## Error streaks

Three or more failed tool calls in a row: the agent is stuck and keeps trying variations.

- [runaway-guard](../../runaway-guard/) asks you to step in after a run of failed calls.
- Once you find what works, add it to AGENTS.md so the next session starts there.

## Permission denials

Tool calls that were blocked: by you (user-rejected), by a permission rule, by a hook, or by an automatic reviewer (auto-reviewer).

- If you reject the same safe command many times, allow it. In Claude Code, `/fewer-permission-prompts` proposes allow rules from your transcripts.
- If the agent keeps trying something your rules forbid, write the rule in AGENTS.md so it stops trying. [rules-to-guards](../../rules-to-guards/) turns the rule into a tested hook.
- [guardrail-tester](../../guardrail-tester/) checks that your rules and hooks block what you think they block.

## User interrupts

You stopped the agent in the middle of a turn. An interrupt is not a problem by itself, but the same reason again and again is a rule waiting to be written.

- Note why you interrupt. If it is the same thing each time, add it to AGENTS.md, and use [rules-to-guards](../../rules-to-guards/) to enforce it.

## User corrections

Messages that correct the agent, such as "that's wrong", "I said use pnpm", or "why did you delete the tests?". The report matches a short list of phrases, so it misses corrections worded another way.

- Turn the corrections you repeat into AGENTS.md rules, and the checkable ones into hooks with [rules-to-guards](../../rules-to-guards/).

## Identical-call loops

The same tool call, with the same input, three or more times in a row with no waiting between: the agent is repeating itself.

- [runaway-guard](../../runaway-guard/) stops the session when the same call repeats three times with no file change between.

## Sessions that ended on an error or interrupt

The last thing in the session was a failed tool call, an API error, a denied call, or an interrupt, so the work may be half done.

- Before you close a session, ask the agent what is done and what is not. [claim-check](../../claim-check/) checks its "tests pass" claims against the transcript.
- Many API errors at the end point to rate limits or provider outages, not to the agent.
