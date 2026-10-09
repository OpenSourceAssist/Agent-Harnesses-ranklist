# How the test drive works

The manual method is in
[How to test-drive a harness](https://github.com/RyanAlberts/best-of-Agent-Harnesses/blob/main/comparisons/how-to-test-drive-a-harness.md).
This skill automates its measurable half: the same tasks for every harness, taken from your own
history, scored by your own tests, with cost and time. Checked 2026-09-28.

## Mining tasks (`mine_tasks.py`)

A **task** is a past commit on the current branch that changed both source files and test files.
Tasks include bug fixes, features, and refactors: anything whose tests failed before the commit and
passed after it.

- Commits come from `git log HEAD` since `--since` (365 days by default). Merge commits are left
  out. So are the first commit (no parent to start from), commits that change more than 30 files
  or more than 1,000 lines ("too large"), commits with no source changes, and commits with no test
  changes.
- A **test file** sits in a folder named `test`, `tests`, `__tests__`, `spec`, `specs`, `testdata`,
  `__snapshots__`, or `__mocks__`, or has a test name: `test_x.py`, `x_test.py`, `conftest.py`,
  `x_test.go`, `x.test.ts` and `x.spec.js` (any JavaScript or TypeScript extension), `x_spec.rb`,
  `x_test.exs`, or `XTest.java` (also Kotlin, Scala, Swift, PHP, C#). Test-runner settings count as
  test files too (`pytest.ini`, `tox.ini`, `noxfile.py`, `phpunit.xml`, `.mocharc`, `.nycrc`,
  `.rspec`, `.rspec-local`, and the jest, vitest, karma, ava, and playwright config files), because
  editing them can switch tests off. A folder named `testing` is not a test folder, because
  libraries such as `numpy.testing` ship code there.
- A **doc** is Markdown, reStructuredText, text, or AsciiDoc, anything under `doc/` or `docs/`, and
  files such as LICENSE and CHANGELOG. A **source file** has a code extension (Python, JavaScript,
  TypeScript, Go, Rust, Java, Ruby, C, C#, Swift, shell, and more). Everything else, such as
  configuration and data, is "other": it can be part of a change, but a commit needs a source change
  to count.
- The **task text** is the commit subject and body. A last paragraph made only of trailers
  (`Co-Authored-By:`, `Signed-off-by:`) is removed.
- The **hidden tests** are the commit's test-file changes; the agent never sees them. The **gold
  patch** is the rest of the commit. "Original fix lines" counts the added and deleted lines in
  its source and other files.
- The **test command** is `--test-cmd`, or the first match of: the `test` script in package.json
  (run with pnpm, yarn, bun, or npm after the lockfile; npm's placeholder script is skipped), a
  pytest setup (`pytest.ini`, `[tool.pytest` in pyproject.toml, `[tool:pytest]` in setup.cfg,
  `[pytest]` in tox.ini, a conftest.py at the top or in `tests/` or `test/`, or `test_*.py` files
  in `tests/`; `uv run pytest -q` when uv.lock exists), go.mod (`go test ./...`), Cargo.toml
  (`cargo test`), or a Makefile `test` target. When nothing matches, the command CI runs is usually
  in `.github/workflows` or the README.

## Checking tasks (`--validate`)

A task is kept only when its tests prove the change: they **fail before it and pass after it,
twice**.

1. In a fresh copy of HEAD, the test command must pass. If it fails, the script stops with exit 2
   and ends the message with the last 15 lines of the output. The full output is in
   `.harness-test-drive/logs/head-check.log` (and `head-setup.log` for the setup command). A
   missing module or command means the clean copy needs `--setup-cmd`; a time-out means the suite
   needs a longer `--test-timeout`.
2. For each candidate, newest first, up to three times `--max`: a fresh copy of the parent commit,
   plus the hidden tests. The test command must fail.
3. The same copy plus the gold patch (now equal to the commit): the test command must pass.
4. Back to the parent's files, then the commit's again: the tests must fail and pass once more.

Rejected candidates are counted by reason:

- "tests pass before the fix": the tests would not notice the missing change, so every agent,
  including one that does nothing, would score a pass.
- "tests fail with the fix": the commit does not pass its own tests here, often because of a
  missing dependency, a flaky test, or a commit that was broken when made.
- "flaky": the second round gave a different result, so the tests cannot score an agent.
- "tests time out with the fix" (after `--test-timeout`, 600 seconds by default), "setup command
  failed", and "could not copy the commit". A time-out before the fix counts as a failure, because
  a hang can be the bug.

`--setup-cmd` runs in every fresh copy before the tests and before the agent, and again whenever
the files just written include a dependency manifest or lock file (package.json, a lockfile,
pyproject.toml, requirements*.txt, go.mod, Cargo.toml, Gemfile, and others). It must install inside
the copy, for example `npm ci`, `pnpm install --frozen-lockfile`, or
`python3 -m venv .venv && .venv/bin/pip install -e .` with the test command
`.venv/bin/python -m pytest`. A setup that installs into the user's own environment changes that
environment 30 times over. No test run reads Python bytecode from an earlier run: Python trusts a
cached file when the source has the same size and the same modified second, so a test file written
right after an earlier run could otherwise run as its old version. Before each run the copy's
`__pycache__` folders are deleted, and `PYTHONPYCACHEPREFIX` points the run at a new empty folder,
which also covers interpreters that keep bytecode outside the copy, such as Apple's
`/usr/bin/python3`.

## The fresh copy

Every check and every agent run gets a new temporary folder holding the files of one commit,
committed once as a brand-new git repository. It is not a git worktree, for two reasons:

- A worktree shares the repository's history, so `git log --all` inside it shows the commit that
  made the change. In the fresh copy, later commits do not exist. (An agent that searched the disk
  could still find the user's repository.)
- A worktree shares branches, the stash, hooks, and config with the user's repository, so an agent
  could change them. The fresh copy shares nothing.

The user's repository is only read, with `git log`, `git ls-tree`, `git cat-file`, and
`git rev-parse`. The copy leaves out untracked files (such as `.env`), submodules, and Git LFS
content (only the pointer files are copied). Tests that need them fail the check, so those tasks are
dropped. A partial clone (`git clone --filter`) keeps old file contents on the server; the skill
fetches nothing, so it stops and asks for the full history first. Restored files are written only
inside the copy: a link the agent planted where a folder belongs is removed first. When counting the
agent's changes, the copy ignores common tool caches (`__pycache__`, `node_modules`, `.venv`,
`dist`, `build`, `target`, `*.egg-info`, and others).

Every command the scripts start (setup, tests, and each harness) gets this process's environment
minus the variables that lead back to the agent session that started it (`CLAUDECODE`, the
`CLAUDE_CODE_*` session, messaging, and bridge variables, `CLAUDE_PID`, `AI_AGENT`, and
`CODEX_SANDBOX*`), so an agent running repository code cannot reach the user's live session.

## Running (`drive.py run`)

Every harness gets the same prompt:

```
Make the change described below in this repository.

Change request:
<commit subject>

<commit body>

When you finish, the repository's own tests will check your work. Test files you edit are put back first, so change the code, not the tests. You can run the tests with this command: <test command>
Do not commit. No one will answer questions, so make reasonable choices and complete the change.
```

(The third paragraph is one line in the prompt; the body paragraph appears only when the commit
message has one.)

Runs go task by task: every chosen harness runs a task before the next task starts. Each run has
`--timeout` seconds (900 by default). When a run ends, times out, or is stopped, every process it
started is stopped too: its process group, and every group below it, found by walking the process
tree (Claude Code runs its shell commands in their own sessions). The flags per harness are in
`harness-commands.md`.

- **One run per results file.** `<results>.lock` holds the running script's process id; a second
  `run` on the same file stops with an error while that process is alive.
- **Stops.** SIGINT (Ctrl-C), SIGTERM (a stopped background task), and SIGHUP (a closed terminal)
  end the agent, record the run as interrupted with its spend, delete the copy, and exit 130. The
  next `run` starts that run again.
- **SIGKILL.** Before each agent starts, `.harness-test-drive/inflight.json` records the task, the
  harness, the copy, and a high cost estimate; it is deleted once the run is recorded. When the
  next `run` finds it, it records the lost run as interrupted with that estimate, deletes the copy,
  and runs the task again.
- **Logs.** For each run, `.harness-test-drive/logs/` keeps `<task>-<harness>.out` (the harness
  output), `.err`, `.diff` (the agent's changes), and `.tests` (the scoring test run).

## Scoring

1. The agent's changes are counted against the starting commit (lines added plus deleted, and
   files touched, new files and deletions included) and saved as the run's diff.
2. When the run did not time out, the harness exited with an error, and it changed nothing, the run
   **could not run**: it is not scored and stays out of pass rates and cost per pass. Its error is
   the harness's own message. An error that will repeat on every task (a login, an old version, a
   missing program) skips that harness's remaining runs and prints the fix, such as "sign in: run
   claude once" or "upgrade Codex". A run that could not run and cost nothing is tried again by the
   next `run`.
3. Otherwise the copy is put back to the starting commit, every untracked and ignored file is
   deleted, the saved diff minus test files is applied, the change's own tests are written, and the
   setup runs again. What a person reviews in the diff is then exactly what was scored: an edit
   inside `node_modules` or another ignored folder does not count.
4. The test command runs. Exit 0 means **passed**. The whole suite must pass, so a change that
   breaks another test fails.

The scoreboard compares harnesses only on the tasks that every one of them finished, so a stop at
the cap or an error on one side does not skew the numbers; a harness with no finished run is listed
as could not run. Harnesses appear in rank order: most passes, then the lowest cost per pass. Cost
per pass is all the spend on those tasks, failed runs included, divided by the passes: the guide's
dollars per accepted task.

## Fairness rules from the guide

| Rule in the guide | What the skill does |
|---|---|
| Hold the model constant where you can | Not possible across these three providers, so a result compares whole stacks (harness plus model). `--model` pins a model per harness for repeatable runs; every result records the model. |
| Port your instructions once | Each copy holds the repository's own instruction files as committed at that point. Each harness still loads the user's personal instructions and hooks from the home folder (and, except Claude Code, its MCP servers); keep those comparable, or write the difference down. |
| Pin versions and write them down | Each result records the harness's `--version`. |
| Fresh git worktree per candidate per task | A fresh copy per harness per task (safer than a worktree, as above). |
| Define "pass" before running anything | Pass means the commit's own tests pass, fixed when the task was mined. |
| Never let the agent grade itself | The tests are hidden, and only the agent's non-test changes are scored. |

Every harness also gets the same prompt, the same timeout, and the same test command.

## Money

- `--max-usd` is required. It covers every run in `results.jsonl`, including runs from earlier
  commands, so a rerun resumes without resetting the count.
- The cap limits counted spend. Claude Code stops itself at the budget left. A Codex run has no
  limit except `--timeout`, and a run cut off there is counted as an estimate, so the bill can pass
  the cap by more than one run. Runs allowed with `--allow-unpriced` count $0.
- Before each run the script compares the counted spend with the cap and starts no run once the cap
  is reached.
- What counts: Claude Code's reported `total_cost_usd` (an estimate at API prices; on a
  subscription plan nothing is billed per token). Codex tokens times the price of its model, which
  comes from `--model` or the top-level `model` in Codex's config; with neither, Codex does not run.
  Gemini CLI reports no cost and runs only with `--allow-unpriced gemini-cli`.
- When a priced run's cost cannot be read: a run cut off at the time limit counts a high estimate
  for every 900 seconds it ran; a run whose output shows model work (for Codex any completed item,
  for Claude Code any reply with usage) counts one high estimate; a run that never reached a model
  counts $0. A Claude Code run stopped before its result counts the replies it sent.
- The estimate assumes a short run of 194,000 tokens (20,000 input, 150,000 cached reads, 20,000
  cache writes, 4,000 output) on the cheaper model, up to a long run of 3,350,000 tokens (150,000
  input, 3,000,000 cached reads, 150,000 cache writes, 50,000 output) on the pricier one: Claude
  Sonnet 5.5 to Claude Opus 5.5 for Claude Code, and the configured model for Codex. These are
  assumptions, not measurements.

## Limits

- **Sample size.** Five or ten tasks give a rough answer. A difference of one or two passes can be
  noise.
- **Tests check behavior, not quality.** A passing change can still be one you would reject in
  review. The saved diffs are there for the human half of the guide: a second person accepts or
  rejects each one.
- **Training data.** For a public repository, a model may have seen the real change during
  training.
- **Flaky tests** can still flip a result. The check runs each task twice, but a test that fails
  only now and then can slip through.
- **Skew.** Tasks come from commits that changed tests, so well-tested parts of the code are
  overrepresented. A terse commit message makes a hard prompt, the same for every harness.
- **Mixed settings files.** Test-runner settings files are put back, but an agent could still change
  how tests run through a file that holds other things too, such as `package.json`,
  `pyproject.toml`, or a Makefile. The saved diffs show it.
- **Untrusted repositories.** The check runs the repository's test and setup commands at up to 31
  commits; each harness loads the repository's own agent settings, edits files, runs the test
  command, and reads commit messages as prompts. For code you did not write, run the skill inside a
  container or VM.
- macOS and Linux only: stopping a run relies on POSIX process groups and `ps`.
