# Test-drive coding agents on tasks from your own repo

Runs Claude Code, Codex, and Gemini CLI on tasks taken from your own git history, scores each one with your repository's tests, and reports how many tasks each passed, the dollars per pass, the minutes, and the size of each change.

## What you get

A sample report. The numbers and commits are invented; your run prints its own.

```
**On 8 tasks from your git history, Claude Code passed 7 at $0.84 each and Codex passed 6 at $0.47 each.**

| Harness | Version | Passed | Pass rate | Median minutes | Cost per pass | Total cost | Median lines changed |
|---|---|---|---|---|---|---|---|
| Claude Code | `2.1.300 (Claude Code)` | 7 of 8 | 88% | 4.2 | $0.84 | $5.88 | 14 |
| Codex | `codex-cli 0.160.0` | 6 of 8 | 75% | 6.8 | $0.47 | $2.82 | 9 |

Per task (the size of the original fix in changed lines, then each harness):

| Task | Change | Original fix lines | Claude Code | Codex |
|---|---|---|---|---|
| 3f2a91c | `Fix off-by-one on the pager's last page` | 6 | passed, 3.1 min, $0.48 | passed, 5.2 min, $0.31 |
| 9b07d4e | `Retry uploads after a 429 response` | 41 | passed, 7.9 min, $1.62 | failed, 9.4 min, $0.66 |
| c41e8a2 | `Keep time zones when parsing due dates` | 12 | failed, 6.3 min, $0.97 | passed, 4.4 min, $0.28 |

- Dollar figures are API list prices: what the harness reported, or its tokens times the price
  table checked 2026-09-28. On a subscription plan, runs count against the plan's limits instead.

Counted toward the spend cap: $8.70.
```

When a harness cannot run at all, the headline says so instead of scoring it: "On 8 tasks from your git history, Codex passed 6 at $0.47 each; Claude Code could not run (`Failed to authenticate: OAuth session expired`)." The run prints the fix ("sign in: run claude once") and retries those tasks next time at no charge.

## Install

```
npx skills add https://github.com/RyanAlberts/best-of-Agent-Harnesses/tree/main/skills/harness-test-drive
```

In Claude Code, all ten at once:

```
/plugin marketplace add RyanAlberts/best-of-Agent-Harnesses
/plugin install harness-skills@agent-harnesses
```

Manual: copy `skills/harness-test-drive/` into your agent's skills folder: `~/.claude/skills/` (Claude Code), `~/.agents/skills/` (Codex, Gemini CLI, Cursor, and OpenCode all read it).

## Use it

Ask your agent: "Which coding agent works best on this repo? Test-drive Claude Code and Codex on tasks from my git history."

The agent finds your test command, keeps the past commits whose tests fail before the change and pass after it, shows how many runs that means and a cost range, and asks for your spending cap before it runs anything. To run the steps yourself, from the root of your repository:

```bash
python3 "$HOME/.claude/skills/harness-test-drive/scripts/mine_tasks.py" --repo . --test-cmd "npm test" --setup-cmd "npm ci" --validate
python3 "$HOME/.claude/skills/harness-test-drive/scripts/drive.py" estimate
python3 "$HOME/.claude/skills/harness-test-drive/scripts/drive.py" run --harness claude-code,codex --max-usd 10
python3 "$HOME/.claude/skills/harness-test-drive/scripts/drive.py" report
```

The setup command installs into each fresh copy: `npm ci`, `pnpm install --frozen-lockfile`, or `python3 -m venv .venv && .venv/bin/pip install -e .` with the test command `.venv/bin/python -m pytest`. Add `--model codex=gpt-6-sol` to pin a model, and `--allow-unpriced gemini-cli` to include Gemini CLI, whose spend cannot be counted. A stopped run resumes with the same command.

## How it works

1. **Mine.** `mine_tasks.py` reads your git history for commits that changed both code and tests. The commit message becomes the task; the commit's test changes become hidden tests the agent never sees.
2. **Check.** With `--validate`, each task runs twice in a fresh copy of your repository: the tests must fail at the commit before the change (with the hidden tests added) and pass with the change. Tasks that fail this check would score an agent that does nothing, and tasks whose result changes between runs are flaky; both are dropped.
3. **Run.** `drive.py run` gives every harness the same prompt in its own fresh copy at the commit before the change, with the most restrictive settings that still allow edits and the test command. The copy is a new git repository whose only commit is that starting point, so the real fix is not in its history (an agent that searched your disk could still find your repository).
4. **Score.** The agent's saved diff, minus test files, goes into a clean copy with the hidden tests, the setup runs again, and your test command decides: passed or failed. A harness that exits with an error and changes nothing could not run, and that run is not scored. The scoreboard compares harnesses only on tasks all of them finished.

Your spending cap is required. The script stops starting runs once the counted spend reaches it, gives Claude Code the budget left as its own limit, and counts earlier runs when it resumes. Stopping it (Ctrl-C, a closed terminal, a stopped background task) ends the agent, records the run with its spend, and deletes the copy. The [method reference](references/method.md) covers every rule, and the [command reference](references/harness-commands.md) lists each harness's flags with sources.

## Works with

| Harness | Run as | Cost measured from | Per-run limit | Support |
|---|---|---|---|---|
| Claude Code | `claude -p`, edits allowed; in the shell, the test command, plus your own allow rules; no MCP servers | its own report | budget left, plus the timeout | full |
| Codex | `codex exec`, workspace-write sandbox | tokens times the price of its model | the timeout | needs a known model: its config's, or `--model codex=<id>` |
| Gemini CLI | `gemini -p`, edits allowed; in the shell, the test command, plus your own allow rules | not measured | the timeout | runs and scores with `--allow-unpriced gemini-cli` |
| OpenCode, Cursor CLI | not supported | | | OpenCode's JSON events are not documented, and Cursor reports no tokens |

Python 3.9 or newer and git, on macOS or Linux. Nothing else to install besides the harnesses you compare.

## Limits

- A handful of tasks gives a rough answer: with five or ten tasks, one or two passes of difference can be noise.
- Tests check behavior, not code quality. Every run's diff is saved in `.harness-test-drive/logs/` for a human review.
- Test files and test-runner settings are put back before scoring, but a change to a file that mixes other settings in, such as `package.json` or `pyproject.toml`, can still alter how tests run. The diff shows it.
- For a public repository, a model may have seen the real change during training.
- Only commits that changed tests become tasks, so well-tested code is overrepresented, and a terse commit message makes a hard prompt for every harness alike.
- Each harness still loads your personal settings: its instruction file in your home folder, hooks, and (except Claude Code) MCP servers.
- The copies leave out untracked files, submodules, and Git LFS content; tasks that need them are dropped by the check.
- The cap limits counted spend. Claude Code stops itself at the budget left. A Codex run has no limit except `--timeout`, and a run cut off there is counted as an estimate, so the bill can pass the cap by more than one run. Runs allowed with `--allow-unpriced` count $0.

## Privacy

- **Read**: your git history, the files of the commits it copies, and the `model` line of Codex's config.
- **Sent**: nothing, by the scripts. Each harness sends its prompt (your commit message) and the code its agent reads to its model provider, as in normal use, and that costs money.
- **Passed on**: each harness and test command gets your environment, minus the variables that point back to the agent session that started the script.
- **Written**: `.harness-test-drive/` in your repository (tasks, results, and logs: the HEAD check's output, and per run the harness output, diff, and test output), a folder git ignores. Each run's copy lives in the system temporary folder and is deleted afterwards unless you pass `--keep`.
- **Untrusted code**: the check runs your test and setup commands, and agents edit files and run the test command in each copy. For a repository you did not write, run the skill inside a container or VM.

## Related

- [How to test-drive a harness](../../comparisons/how-to-test-drive-a-harness.md): the manual method this skill automates, including the human review it cannot do.
- [Terminal coding agents](../../comparisons/terminal-coding-agents.md) and [Why the harness matters more than the model](../../comparisons/why-the-harness-matters.md): the background for comparing harnesses on your own work.
- [session-waste-report](../session-waste-report/) for past spend, and [regression-finder](../regression-finder/) for changes after an update.
- The fail-before, pass-after check comes from [SWE-bench](https://github.com/SWE-bench/SWE-bench). [Harbor](https://github.com/harbor-framework/harbor) runs agents on benchmark tasks at scale, [RepoTrials](https://github.com/PozziTiv4ik/Repo-Trials) mines tasks from git history, and [sample-agent-cost-bench](https://github.com/aws-samples/sample-agent-cost-bench) measures cost across agents. [Harness-Bench](https://arxiv.org/abs/2605.27922) found that harness rankings barely carry over between models.
