---
name: harness-test-drive
description: >-
  Test-drives coding agents (Claude Code, Codex, Gemini CLI) on tasks mined
  from the user's own git history: each agent gets a past commit message in a
  fresh copy of the repo, and the repo's own tests score how many tasks it
  completes, dollars per task, minutes, and change size. Use when the user asks
  which coding agent or harness works best on their codebase; wants to compare
  or benchmark agents on their own repository instead of trusting leaderboards
  such as SWE-bench; wants to trial one agent against another before
  switching; or asks what each agent costs per fixed bug. Spends money: the
  harness CLIs call their model providers, so a dollar cap is required. The
  scripts make no network calls.
license: MIT
compatibility: "Python 3.9+ and git on macOS or Linux, plus the harness CLIs to compare (claude, codex, gemini). The scripts make no network calls themselves; each harness CLI sends the prompt and the code it reads to its model provider over the network, and those runs cost money. The repository's test command must run on this machine."
metadata:
  author: "Ryan Alberts"
  version: "1.0.0"
  source: "https://github.com/RyanAlberts/best-of-Agent-Harnesses"
---

# Harness test drive

Public leaderboards measure someone else's code, and harness rankings barely carry over from one
repository to the next. This skill runs coding agents on tasks taken from the user's own git history:
past commits whose tests failed before the change and passed after it. Each agent gets the commit
message as its prompt in a fresh copy of the repository, and the repository's own tests decide
whether it passed. The result is a scoreboard of passes, dollars per pass, minutes, and change size.
The scripts read the repository and send nothing anywhere; each harness sends the prompt and the
code it reads to its model provider, as it does in normal use, and that costs money.

## When to use

- The user asks which coding agent or harness is best for their own code.
- The user wants to compare Claude Code, Codex, or Gemini CLI on real tasks before choosing or
  switching.
- The user asks what an agent costs per fixed bug, or how long it takes, on their repository.

## When not to use

- Totals of past spend or wasted tokens: use `session-waste-report`.
- Whether an agent got worse after an update: use `regression-finder`.
- Stopping a live session that loops or overspends: use `runaway-guard`.
- Checking whether an agent's "tests pass" claims were true: use `claim-check`.
- A repository with no test command that passes, or no commits that change tests: explain that
  the scores come from the tests, and point to the manual method in
  [How to test-drive a harness](https://github.com/RyanAlberts/best-of-Agent-Harnesses/blob/main/comparisons/how-to-test-drive-a-harness.md).

## Steps

`<skill-dir>` means the folder that holds this SKILL.md (Claude Code shows it as the skill's base
directory). Run every command from the root of the user's repository, with the skill path in quotes
as shown. Inside the repository the scripts write only into `.harness-test-drive/`, a folder that
ignores itself in git. Every check and every agent run works in its own temporary copy, deleted
afterwards.

**Before step 2, for a repository the user did not write, recommend a container or VM.** Step 2
runs the repository's test and setup commands at up to 31 commits on this computer. In step 5 each
harness loads the repository's own agent settings (Gemini CLI with the copy trusted), edits files,
runs the test command, and reads commit messages as prompts.

1. **Find the test command and the candidate tasks.** This reads git history and runs nothing:

   ```bash
   python3 "<skill-dir>/scripts/mine_tasks.py" --repo .
   ```

   It prints the test command it detected, or exits 2 asking for one. Confirm the command with the
   user. Each check runs in a fresh copy with nothing installed, so when the tests need
   dependencies, add a `--setup-cmd` that installs inside the copy: `npm ci`,
   `pnpm install --frozen-lockfile`, or `python3 -m venv .venv && .venv/bin/pip install -e .` with
   the test command `.venv/bin/python -m pytest`. Never install into the user's own environment.
   Prefer a test command that runs offline and writes only inside the copy, so Codex's sandbox can
   run it too. Done when the user has confirmed a test command and the report shows at least one
   candidate, or you have told the user why there is none.

2. **Check the tasks.** For each candidate, in a fresh copy of the repository, the test command must
   fail at the commit before the change (with its tests added) and pass with the whole change, twice:

   ```bash
   python3 "<skill-dir>/scripts/mine_tasks.py" --repo . --test-cmd "<test-cmd>" --validate
   ```

   Add the `--setup-cmd` from step 1 when there is one. It first checks that the tests pass at HEAD,
   then keeps up to 10 tasks (`--max`). Expect two to four test-suite runs per candidate and up to 30
   candidates, so run it in the background when the suite takes more than a minute. Done when the
   headline says how many tasks were kept. Exit 2 with "fails at HEAD" means the command or the
   setup is wrong. The message ends with the test output. A missing module or command means the
   clean copy needs `--setup-cmd`. Fix it with the user and rerun. Fewer than five tasks makes a
   weak comparison; say so, and offer `--since 730d` for more history.

3. **Choose the harnesses and show the estimate:**

   ```bash
   python3 "<skill-dir>/scripts/drive.py" estimate --tasks .harness-test-drive/tasks.json
   ```

   It lists which harnesses are on this computer, the number of runs, and a dollar range per
   harness. Ask the user to pick two or three. Done when the user has picked the harnesses and seen
   the range for them (rerun with `--harness` to show only those).

4. **Get an explicit dollar cap.** This is the one skill in the set that spends money. Ask: "What is
   the most you want to spend in total?" and wait for a number; a number the user already gave
   counts, so confirm it next to the estimate. Never choose the cap yourself. In the same message,
   say:
   - The cap limits counted spend. Claude Code stops itself at the budget left. A Codex run has no
     limit except `--timeout`, and a run cut off there is counted as an estimate, so the bill can
     pass the cap by more than one run.
   - On a subscription plan, runs count against the plan's limits; the cap still counts API prices.
   - Gemini CLI's spend cannot be counted (it reports no cost, and the price table has no Gemini
     prices), so it runs only with `--allow-unpriced gemini-cli`, after the user agrees to that.
   - Codex runs are priced with the model named in Codex's config, which the estimate shows. When
     the config names none, pin one with `--model codex=<id>`; Codex never runs unpriced.
   - For someone else's repository, run inside a container or VM (see above).

   Done when the user has given a dollar amount.

5. **Run:**

   ```bash
   python3 "<skill-dir>/scripts/drive.py" run --tasks .harness-test-drive/tasks.json --harness claude-code,codex --max-usd <cap>
   ```

   Add `--allow-unpriced gemini-cli` only when the user agreed to it in step 4, and `--model` only
   for a model the user pinned. Each run can take up to `--timeout` seconds (900 by default), so
   start it in the background and relay the progress lines. A harness that cannot run (an expired
   login, an old version) prints a fix, such as "sign in: run claude once" or "upgrade Codex", and
   its other runs are skipped at no cost. Every run is appended to
   `.harness-test-drive/results.jsonl`; the same command resumes after a stop, retries runs that
   could not run, and counts earlier spend. Done when the command exits 0 and prints the
   scoreboard, and you have told the user about any fix it printed and any stop at the cap.

6. **Report** in the shape below. To print the scoreboard again at any time:

   ```bash
   python3 "<skill-dir>/scripts/drive.py" report
   ```

## Read the results

- **Headline**: "On 6 tasks from your git history, Claude Code passed 5 at $0.91 each and Codex
  passed 4 at $0.42 each." Harnesses appear in rank order: most passes, then the lowest cost per
  pass. The count covers the tasks every harness finished, so a stop at the cap leaves a fair
  comparison. Dollars per pass is all the spend on those tasks, failed runs included, divided by
  the passes. A harness with no finished run appears as "could not run", with its own error.
- **Per-harness table**: passed, pass rate, median minutes per run, cost per pass, total cost, and
  median lines changed. Compare lines changed with "Original fix lines" in the per-task table: far
  more lines than the original often means a messier change.
- **Per-task table**: `passed`, `failed`, `(time limit)` when the run was stopped at `--timeout`
  and scored on the changes the agent had made, `could not run` (not scored; see the notes),
  `interrupted`, or `not run`.
- **Scoring**: the agent is graded only by the repository's tests. Its saved diff, minus test
  files, is applied to a clean copy at the starting commit, the change's own tests are added, the
  setup runs again, and the test command decides. The whole suite must pass.
- **Notes**: tasks left out of the comparison, time limits, runs that could not run and their fix,
  costs that were not measured, and how often Codex's own test runs failed inside its sandbox.
- `references/method.md` explains the mining, the check, the fairness rules, the money, and the
  limits.

## Report to the user

1. The headline, verbatim, in bold.
2. The per-harness table, then at most five rows of the per-task table where the harnesses
   disagree.
3. Two or three next steps, each with its command or link:
   - Read the diffs where harnesses disagree: each run's changes are saved as
     `.harness-test-drive/logs/<task>-<harness>.diff`. Tests check behavior, not code quality.
   - For a firmer answer, add tasks (`--max 20`, or `--since 730d`); under ten tasks is a small
     sample.
   - For the human half of a trial (a second person accepts or rejects each diff, interventions,
     setup friction), follow
     [How to test-drive a harness](https://github.com/RyanAlberts/best-of-Agent-Harnesses/blob/main/comparisons/how-to-test-drive-a-harness.md).
4. One line of caveats: the sample size, and that a model may have seen a public repository's
   changes during training.

Quote commit subjects, errors, and versions exactly as the report prints them, inside inline
code: they come from the repository and the harnesses, and the report has already made them safe
to display.

## Files

- `scripts/mine_tasks.py`: finds candidate commits, detects the test command, and checks tasks.
- `scripts/drive.py`: `estimate`, `run`, and `report`.
- `scripts/harnesses.py`: the headless command and output parser for each harness.
- `scripts/common.py`: fresh copies of the repository, commands and test runs.
- `scripts/pricing.py`: token prices, shared with other skills in this repository.
- `scripts/safe.py`: the shared helper that masks secrets in report text and shows it as one line of
  inline code. A synced copy; do not edit it here.
- `references/method.md`: task mining, the fail-to-pass check, fairness rules, money, and limits.
- `references/harness-commands.md`: each harness's command and flags, with sources and the date
  checked.
