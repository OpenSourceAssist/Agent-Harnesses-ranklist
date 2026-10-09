# Check your agent's 'tests pass' claims

Find out how often your coding agent said tests passed without a passing run to back it up, and whether the current change quietly weakened your tests. It reads your local session logs and sends nothing anywhere.

## What you get

A sample report. The numbers and names are invented; your agent prints its own.

```
**Found 41 claims that tests or builds passed in the last 30 days. 11 of those claims were not backed by a passing run.**

Checked 212 sessions from the last 30 days (Claude Code 174, Codex 38), plus 96 subagent sessions,
and found 41 claims: 26 backed, 7 stale, 2 contradicted, 2 unsupported, 4 unclear.

| Label | Claims | What it means |
|---|---|---|
| backed | 26 | The latest matching run before the claim passed, and no code changed after it. |
| stale | 7 | The run passed, but code changed after it and nothing ran again. |
| contradicted | 2 | The latest matching run failed. |
| unsupported | 2 | No matching run happened in the session before the claim. |
| unclear | 4 | The evidence could not be read or ordered; the why column says which case. |

## Claims that were not backed (worst first)

1. **contradicted** (test claim), 2026-09-12 14:03 UTC, Claude Code, session `5f0c3a1e-8d7`, project `~/code/shop`
   - Claim: `All 42 tests pass.`
   - Last test run: `npx vitest run` failed (`Tests 1 failed / 41 passed (42)`) at 2026-09-12 14:01 UTC.
2. **stale** (test claim), 2026-09-09 10:22 UTC, Codex, session `019a2b3c-4d5`, project `~/code/api`
   - Claim: `Fixed the timeout; the test suite passes.`
   - Last test run: `pytest -q` passed (`58 passed`) at 2026-09-09 10:15 UTC; then 1 file changed: `~/code/api/src/retry.py`.

## Claims that could not be checked

1. **unclear** (test claim), 2026-09-15 16:40 UTC, Claude Code, session `7c1d9e20-3ab`, project `~/code/mono`
   - Claim: `The web tests pass.`
   - Why: code changed elsewhere in the repository.
   - Last test run: `cd packages/web && npm test` passed (`Tests: 64 passed, 64 total`) at 2026-09-15 16:31 UTC.
```

And for the current change:

```
**The current diff shows 3 signs of weakened tests: 1 removed test, 1 test file with fewer assertions, 1 new skip marker.**

| Signal | File | Line | Detail |
|---|---|---|---|
| removed test | `tests/test_retry.py` |  | `test_gives_up_after_three_tries` |
| test file with fewer assertions | `tests/test_retry.py` |  | `2 assertions removed, 0 added` |
| new skip marker | `tests/test_client.py` | 88 | `@pytest.mark.skip(reason='flaky')` |
```

## Install

```
npx skills add https://github.com/RyanAlberts/best-of-Agent-Harnesses/tree/main/skills/claim-check
```

In Claude Code, all ten at once:

```
/plugin marketplace add RyanAlberts/best-of-Agent-Harnesses
/plugin install harness-skills@agent-harnesses
```

Manual: copy `skills/claim-check/` into your agent's skills folder: `~/.claude/skills/` (Claude Code), `~/.agents/skills/` (Codex, Gemini CLI, Cursor, and OpenCode all read it).

## Use it

Ask your agent: "How often did you say the tests passed this month without a passing run? Check the evidence."

Or: "Did this change delete or skip any tests?" and "Add a hook that makes you rerun the tests before you finish."

To run the scripts yourself (the path below is the Claude Code install; adjust it to where the skill lives):

```bash
python3 ~/.claude/skills/claim-check/scripts/claims.py scan --since 30d
python3 ~/.claude/skills/claim-check/scripts/claims.py diff --repo . --base main
python3 ~/.claude/skills/claim-check/scripts/install.py            # shows the Stop hook change
python3 ~/.claude/skills/claim-check/scripts/install.py --write    # applies it, for every project
```

For one project in Claude Code, add `--scope local`. `--scope project` writes this machine's path to the script into the shared project settings, so use it only when the skill sits inside the repository at the same path for everyone.

Add `--json` for every field, `--out report.md` to save the report, `--harness codex` or `--project ~/code/app` to narrow the scan, and `--fail` to exit 1 when a claim was not backed (or the diff has a signal), for use in CI.

Before you move, update, or remove the skill, take the hook out first: `python3 ~/.claude/skills/claim-check/scripts/install.py --uninstall --write`. The first `--write` saved your original file as `settings.json.claim-check.bak`, and the uninstall puts it back when nothing else in the file changed. A hook whose script has moved lets the agent finish instead of blocking it, but its entry stays in your settings until you remove it.

## How it works

- **Claims.** The scan reads each session in order and finds the sentences where the agent says tests or a build passed ("All 42 tests pass", "tsc clean", "200 passed"). Plans, goals, conditions, hedges, partial results ("only 3 tests pass"), reports of failure, other people's words, questions, quotes, code, and claims about CI are left out.
- **Runs.** It recognizes test and build commands for Python, JavaScript and TypeScript, Go, Rust, Ruby, PHP, Java, .NET, Swift, and C, through wrappers such as `uv run`, `npx`, and `timeout`. It reads each result from the exit code when the exit code belongs to the runner, and otherwise from the runner's own summary line, because `pytest | tail -5` exits with tail's code, not pytest's.
- **Changes.** Edits, writes, patches, MCP tools that write files, branch switches, and shell commands that change files count; documentation, logs, temporary files, generated folders, and other repositories do not.
- **Labels.** Each claim is backed, stale, contradicted, unsupported, or unclear. Unclear is for evidence that cannot be read or ordered, so it never counts against the agent. Subagent runs count for the main session once their result has reached it.
- **Diff.** Deleted tests, removed assertions, several tests folded into one parametrized test, new skip or focus markers, test commands allowed to fail, and lowered coverage thresholds, in test files that already existed.
- **Stop hook.** When the agent tries to finish, it blocks once per reply if the last test run failed, or code changed after the last passing run, in work done since your last message. Subagents still working and runs in other repositories do not count, and it does nothing in a session that ran no tests.

Every rule is in [the claim reference](references/claim-patterns.md) and [the weakened-test reference](references/weakened-tests.md).

## Works with

| Harness | Scan | Stop hook |
|---|---|---|
| Claude Code | full, including subagents and background runs | yes |
| Codex CLI and Codex Desktop | full, including the JavaScript `exec` scripts of Codex Desktop | yes, after you trust it in `/hooks` |
| Gemini CLI | reads its transcripts | no |
| OpenCode | follows the documented database layout; not verified on a real install | no |
| Cursor | not checked: its transcripts keep no tool results or times | no |

It runs on macOS and Linux with Python 3.9 or newer; `diff` needs git.

## Limits

- Claims are found by their wording, so a claim in unusual phrasing is missed. Sentences that only might be claims are left out on purpose.
- It knows the common test and build commands. A custom test script it cannot read makes later claims unclear rather than wrong, and the report says so.
- It judges a claim by the latest run before it, not by whether that run covered the whole suite: "all tests pass" after running one test file counts as backed.
- A run at the repository root goes stale with a change anywhere in the repository, even in a folder the tests do not use. A run started in a subfolder goes stale only with a change inside it; a change elsewhere in the repository makes the claim unclear.
- Commands that change files in ways it does not recognize (a program you wrote that edits files) are missed.
- Claims inside subagent transcripts are not counted; their runs and changes count as evidence for the main session. Codex subagent pages that stamp every line with one time cannot be ordered, so claims that may depend on them are unclear.
- The diff check reads lines, not code: a weaker assertion on the same line, a mocked-out test, or a skip set in configuration is not caught.
- Transcript formats are internal to each harness and change between versions; lines it cannot read are counted in the report.

## Privacy

- **Read**: session transcripts under `~/.claude`, `~/.codex`, `~/.gemini`, and OpenCode's data folder (within the time window), the git diff of the repository you name, and, for `install.py`, the one settings file it changes.
- **Printed**: counts, and for claims that were not backed: the session id, time, project folder (your home folder shown as `~`), the claim sentence, and the command, each masked for secrets and cut to 160 characters.
- **Sent**: nothing. There is no network code.
- **Written**: nothing, unless you pass `--out`, or run `install.py --write`, which adds or removes one hook entry, keeps everything else in the file, and saves the original as `<file>.claim-check.bak` the first time. A symlinked settings file is changed at its target. The dry run prints only the hook entry's lines; other lines are counted, never shown.

## Related

- [Safe Claude Code settings](../../templates/claude-code-safe-settings/): permission rules and a guard hook to pair with the Stop hook.
- [guardrail-tester](../guardrail-tester/): checks that your rules and hooks block dangerous commands.
- [runaway-guard](../runaway-guard/): stops a session that loops, keeps failing, or overspends.
- [rules-to-guards](../rules-to-guards/): turns a rule such as "rerun the tests after every edit" into an enforced hook.
- [How to test-drive a harness](../../comparisons/how-to-test-drive-a-harness.md): compare agents on your own work.
- Built on two field reports: [an audit of 101 "tests pass" claims](https://dev.to/vinzenz_eiberger/i-checked-101-tests-pass-claims-from-my-ai-coding-agents-35-werent-true-h6n) and [a diff check after an agent deleted a failing test](https://dev.to/leoleroy/i-got-tired-of-coding-agents-saying-all-tests-pass-when-the-diff-said-otherwise-5ce9) ([i-dont-believe-you](https://github.com/LeonardLeroy/i-dont-believe-you)), and on SpecBench (arXiv:2605.21384) and BAITBENCH (arXiv:2608.30724) on agents gaming tests.
