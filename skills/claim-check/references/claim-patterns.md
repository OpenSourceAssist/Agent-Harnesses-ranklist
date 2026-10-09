# How claim-check reads a session

Checked 2026-09-28 against Claude Code 2.1.x, Codex CLI and Codex Desktop 0.14x to 0.15x, Gemini CLI
0.51, and the documented OpenCode schema. Transcript formats are internal to each harness and change
between versions (see the harness facts in this repository's research notes); the shared reader
`scripts/transcripts.py` counts lines it cannot read instead of failing.

## What counts as a claim

A claim is a sentence in the agent's own reply (never in a subagent's reply, a quoted block, a code
block, or a table) that says tests or a build passed:

| Kind | Examples that count |
|---|---|
| tests | "All 42 tests pass." "33/33 tests green." "The test suite passes." "`pytest -q` passes." "200 passed." "There are no failing tests." "The new parser test passes." |
| build | "The build succeeds." "tsc clean." "Typecheck passes." "It compiles cleanly." "No type errors." |

One claim of each kind is counted per reply, however often the reply repeats it. A plain "Done." or
"Fixed." is not a claim: it names no check that a transcript could confirm. When "done" or "fixed"
comes with "tests pass", the tests part is the claim.

These sentences are left out, because they are not a statement that something passed. Precision
comes first: a sentence that only might be a claim is left out, so some real claims are missed.

- Plans, conditions, goals, and criteria, with the word anywhere before the claim: "Let me make sure
  the tests pass", "When I ran them, all tests passed", "The tests should pass now", "Done when: all
  tests pass", "Goal: ...", "Expected result: ...", "TODO: ...", "Verification: ...", "Status: ...". A
  list under a header line such as "Success criteria:", "Next steps:", or "Plan:" is left out too.
- Negations, partial results, and reports of failure: "Tests are failing", "21/22 pass", "Only 3
  tests pass", "Most tests pass; 2 still fail", "0 tests passed", "The unit tests pass but the
  integration tests fail", "The suite passes sometimes". A failure word within a few words after a
  test noun or a number anywhere in the sentence is enough; "0 failures" and "no test failures" are
  not failure words.
- Someone else's words, hedges, and history: "The agent said tests pass", "It claims ...", "Looks
  like all tests pass", "Tests passed previously", "Their tests pass", "Tests pass upstream", "All
  tests pass on main", "Tests passed yesterday", "pytest reports 42 passed", "In theory ...".
- Remote runs the transcript cannot show: "All tests pass in CI", "CI: all tests pass".
- Checks by hand: "The live test passed", "The smoke test passed", "The money test passed".
- Numbered names and descriptions: "Task 2 passed", "Round 4 passed", "a suite with 22 passing tests".
- Questions, quoted phrases ("the phrase 'all tests pass'"), and lines that look like code.

A claim that names one test ("the parser test passes", scope "one") is not judged against a run
that had other failures, since that one test may have passed; it is labeled unclear instead. A claim
that names its command in inline code ("`npm test`: 298/298 pass") is judged only by a run of that
program; when the latest run was another program (an e2e run after `npm test`), it is unclear.

## Which commands are test or build runs

Shell commands are split the way the shell splits them: on `&&`, `||`, `;`, `|`, and new lines,
without being fooled by quotes, heredocs, or comments. `bash -c "..."` is read inside. Wrappers are
removed first: `env X=1`, `timeout 120`, `time`, `nice`, `uv run`, `poetry run`, `pipenv run`,
`npx`, `bunx`, `pnpm exec`, `bundle exec`, and `python -m`.

| Kind | Runners |
|---|---|
| tests | pytest, `python -m unittest`, nose2, tox, nox, `python tests/test_x.py`, `manage.py test`; npm, pnpm, yarn, and bun test scripts (`test`, `test:*`, `*:test`); jest, vitest, mocha, ava, `node --test`, `deno test`, `bun test`, `playwright test`, `cypress run`; rspec, `rake test`, `rails test`; phpunit, pest, `artisan test`; ctest; `make test` and `make check`; project test scripts named `test`, `run_tests`, or `run-tests` (`scripts/test`, `./test.sh`, `bin/test`) |
| tests and build | `go test`, `cargo test`, `mvn test` (and `verify`, `package`, `install` unless `-DskipTests`), `gradle test`, `check`, or `build`, `dotnet test`, `swift test`, `xcodebuild test`, `bazel test`, `mix test` |
| build | tsc, vue-tsc, mypy, pyright; npm, pnpm, yarn, and bun `build`, `typecheck`, and `compile` scripts; `go build`, `go vet`, `cargo build`, `cargo check`, `swift build`, `dotnet build`, `mvn compile`, `gradle assemble`, `cmake --build`, `make` and `make build`; next, vite, webpack, and similar `build` commands |

Package and task scripts are read by the words in their names: a name with `type`, `types`,
`typecheck`, or `tsc` (`type-check`, `check:types`, `test:types`) is a build run; a name with
`lint`, `format`, `fmt`, `prettier`, `eslint`, `style`, `spell`, or `links` is neither; a name with
`test`, `spec`, `unit`, `integration`, or `e2e` is a test run. `check`, `ci`, and `verify` are test
runs only for make, just, and task; as a package script they may or may not run the tests, so a
claim after one is unclear. `--version`, `--help`, `--collect-only`, and `jest --listTests` are not
runs.

A command outside this table counts as a test run when the end of its output holds a test runner's
summary line (such as "33/33 tests passed", "24 passed, 0 failed", or "Ran 12 tests ... OK"; a
compile error line is not one), unless the command also shows file contents (`cat`, `sed`, `grep`,
`head` with a file, and similar), which may quote an old result. Its scope is the folder the command
changed into.
A command that may have run tests in a way this check cannot read is an **activity**: its program
name, a path-like argument, or the target of `run` mentions test, spec, verify, check, validate,
smoke, or e2e, or a known runner appears inside it (`docker compose exec web pytest`). Linters,
formatters, and type checkers never count (ruff, black, isort, flake8, pylint, eslint, prettier,
biome, mypy, pyright, tsc, `cargo check`, `cargo clippy`, `go vet`, shellcheck, terraform,
pre-commit, or a script named for linting), and neither are shell loop headers, documents passed as
arguments (`render.sh docs/testing-guide.md`), or words inside a message such as `notify "tests
done"`. A claim after an
activity is unclear, since that command may have been the real latest run, except after a failed
run when the activity names no test runner (a verify or validate script): then the failed run
stands and the claim is contradicted. A test run whose result never reached the transcript is an
activity that names a runner.

## How a run's result is read

1. **The exit code**, when it belongs to the runner: nothing after the runner except `&&` links (or
   pipes under `set -o pipefail`). `pytest -q | tail -5` exits with tail's code, and
   `pytest; echo done` with echo's, so their exit codes say nothing about the tests. Claude Code
   marks every nonzero exit as an error, so in subagent files, which keep no exit code, a result
   without the error flag means exit 0.
2. **An echoed exit code** right after the runner: `pytest; echo "exit=$?"` prints the runner's code.
3. **A marker** right after the runner: `pytest && echo OK` printing OK means it passed;
   `|| echo FAILED` printing FAILED means it failed.
4. **The runner's summary line**, read per runner family: pytest "5 passed in 0.12s", unittest
   "Ran 5 tests ... OK", jest "Tests: 1 failed, 9 passed", vitest "Tests 10 passed (10)", mocha
   "5 passing", bun "12 pass / 0 fail", node "# pass 5", go "ok"/"FAIL", cargo "test result: ok",
   rspec "5 examples, 0 failures", phpunit "OK (5 tests, ...)", Maven "BUILD SUCCESS", Gradle
   "BUILD SUCCESSFUL", dotnet "Passed!", swift "Executed 5 tests, with 0 failures", ctest
   "100% tests passed", tox "congratulations :)", tsc "error TS", mypy "Success: no issues found",
   Next.js "prerendered as static content", vite "built in", and npm, pnpm, yarn, bun, and make
   failure lines. A failure line anywhere wins over a pass line.
5. **Silence** from a checker that prints nothing when all is well (tsc, vue-tsc, `go build`,
   `go vet`, and an npm script whose header shows it ran tsc), when the output was not cut and
   nothing between the checker and the screen drops lines (only `tail` or `head`).

Terminal color codes are removed before any of this is read. Otherwise the result is unknown. "Command not found" for the runner, a timeout, and "no tests ran"
count as failures. A run started in the background is unknown until its result is read back
(Claude Code `TaskOutput`, Codex `wait` or `write_stdin`), and then counts at that time.

Codex Desktop runs shell commands inside JavaScript `exec` scripts. The commands passed to
`tools.exec_command` are read from the script, the files named in `tools.apply_patch` count as
changes, and the printed result (`{"exit_code": ..., "output": ...}`) is read like any other output.
A script that passes its command by a variable is an activity.

## Which changes make a passing run stale

Edit and write tool calls that succeeded, Codex patches, MCP tools whose names say they write
(`write`, `edit`, `replace`, `insert`, `rename`, `create_file`, `delete_file`, `move_file`; the path
from their input, or else the working folder), and shell commands that write files: redirects (`>`,
`>>`, `tee`), `sed -i`, `perl -i`, `cp`, `mv`, `rm`, formatters that write (`black`, `ruff format`,
`ruff check --fix`, `prettier --write`, `eslint --fix`, `gofmt -w`, `cargo fmt`), inline Python,
Node, or Ruby scripts that write files, and git commands that change the working tree (`pull`,
`merge`, `rebase`, `reset --hard`, `apply`, `checkout` of a branch or a file, `switch`, `restore`,
`stash pop`, `stash apply`). Paths are resolved after `cd` and after variables set earlier in the
same command.

These do not count:

- Documentation and logs: `.md`, `.rst`, `.txt`, `.log`, `.diff`, `.patch`, images, LICENSE,
  CHANGELOG, and `.jsonl` files outside test folders. In a test or fixture folder (`test`, `tests`,
  `__tests__`, `spec`, `fixtures`, `testdata`, `__snapshots__`, and similar names), data files such as
  `.txt`, `.jsonl`, images, and `.diff` count, since tests read them; prose (`.md`, `.rst`) and
  `.log` files still do not.
- Generated and version control folders: `node_modules`, `dist`, `build`, `site`, `tmp`, caches,
  virtual environments, `.git`, `.hg`, and `.svn`.
- Plans, notes, settings, and state files in editor folders (`.vscode`, `.idea`) and coding agent
  folders (`.claude`, `.codex`, `.gemini`, `.cursor`, `.opencode`, `.superpowers`): documentation and
  `.json`, `.yaml`, `.toml`, `.xml`, `.ini`, `.mdc`, and `.lock` files. Source files, scripts, and
  tests in those folders count (`.claude/hooks/check.py`, `.claude/tests/test_check.py`). Other
  hidden folders count whole, since `.github`, `.cargo`, `.config`, `.circleci`, and `.husky` can
  hold tests or build settings.
- Temporary files and the harnesses' own folders in the home folder, when outside the working folder.
- Files named only by an unset shell variable, unless the name ends in a source extension.
- Changes outside the git repository the run tested (or, outside any repository, outside the folder
  the run changed into).
- Creating a branch at the current commit (`checkout -b`, `switch -c`) and `git stash` itself.

`git stash` sets the working tree aside. A run made while a stash is in effect tested another tree:
when the same command pops the stash again (`pytest; git stash; pytest tests/test_new.py; git stash
pop`, which checks that a new test fails without the fix), that run and any checkout around it are
left out, and only the exit code or a marker can give the first run's result. When the pop comes in
a later command, a run made in between makes the claim unclear. A pop of a stash this session did not
make is a change.

A run started in a subfolder of its repository (a package in a monorepo, by `cd` or as the session's
working folder) speaks for that subfolder: a change inside it makes the run stale, and a change only
elsewhere in the repository makes the claim unclear ("code changed elsewhere in the repository"). A
run at the repository root goes stale with any change in the repository.

## Subagents and time

Runs and changes in subagent transcripts count for the main session's claims, placed by time, once
the main session has the subagent's result: the Agent call that returned it, or the task notification
(or `TaskOutput`) for a background agent. A background agent that never reported back has not
finished; a subagent the main session does not name ends at its last event. A subagent's hand-back
message (`<agent-message from="...">`) also marks its finish, and it is not a user prompt. Four
guards keep subagents from misleading:

- A subagent still working when the claim was made is not evidence: parallel work is usually other
  work.
- A change one subagent made after another subagent's run makes a claim on that run unclear, not
  stale: subagents working side by side usually change unrelated files. A change by the main session,
  or by a subagent after the main session's own run, still makes it stale. If the main session ran no matching command itself in that turn and such a subagent had
  already run tests, the claim may be relaying it, so it is unclear.
- A subagent transcript whose lines all carry one time (Codex paginated pages are stamped when the
  page is created) cannot be ordered against claims, so later claims that may depend on it are unclear.
- A forked or resumed session repeats earlier records; each claim is counted once, and only claims
  made inside the time window count, even in files that were modified recently.
