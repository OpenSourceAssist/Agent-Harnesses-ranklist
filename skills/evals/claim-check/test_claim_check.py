"""Tests for skills/claim-check/scripts.

Every fixture is built in tmp_path at test time: synthetic Claude Code and
Codex transcripts in the record shapes of the harness facts file (Q1), fake
settings files, and small git repositories. Nothing here reads the real home
folder, runs a real harness, or touches the network.

Run: uv run -q --python 3.12 --with pytest python -m pytest -q -p no:cacheprovider skills/evals/claim-check
"""
from __future__ import annotations

import itertools
import json
import os
import re
import subprocess
import sys
import time

import pytest

sys.dont_write_bytecode = True
HERE = os.path.dirname(os.path.abspath(__file__))
SCRIPTS = os.path.abspath(os.path.join(HERE, "..", "..", "claim-check", "scripts"))
sys.path.insert(0, SCRIPTS)

import claims as C  # noqa: E402
import evidence as E  # noqa: E402
import install as I  # noqa: E402
import stop_hook as S  # noqa: E402
import transcripts as T  # noqa: E402
import weakened as W  # noqa: E402


@pytest.fixture(autouse=True)
def _no_real_config(monkeypatch, tmp_path):
    """Keep the caller's environment from pointing any reader at real data."""
    for name in ("CLAUDE_CONFIG_DIR", "CODEX_HOME", "XDG_DATA_HOME", "OPENCODE_DB"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("HOME", str(tmp_path / "home"))


# ---------------------------------------------------------------------------
# Claims in assistant text
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("text", [
    "All 42 tests pass.",
    "The test suite passes.",
    "Tests are passing now.",
    "Ran the suite again: 118 passed.",
    "33/33 tests green.",
    "Harness tests: 26/26 passing.",
    "Fixed the failing test; all 42 tests pass now.",
    "Full suite: 39 pass / 0 fail (exit 0).",
    "`pytest -q` passes.",
    "There are no failing tests.",
    "Done: 12 passed, 1 skipped.",
    "- [x] Tests pass",
    "**All 24 tests pass.**",
    "Both new tests now pass locally.",
    "I ran the tests and they all pass.",
    "Phase 1 finished: 118 of 118 engine tests passing.",
    "Tests: 259 of 259 unit and 22 of 22 end-to-end pass.",
    "99 tests pass and CI is green.",
    "Tests pass locally but CI is red.",
    "The evaluator passed 12 tests.",
    "All 42 tests pass with 0 failures.",
    "No test failures.",
])
def test_test_claims_are_found(text):
    assert [c["kind"] for c in E.find_claims(text)] == ["tests"]


@pytest.mark.parametrize("text", [
    "The build succeeds.",
    "Typecheck clean, all verification passed.",
    "`tsc --noEmit` is clean.",
    "Production build is clean, all routes static.",
    "It compiles cleanly.",
    "tsc clean.",
    "No type errors.",
    "Build passes (exit 0, TypeScript clean).",
    "Typecheck passes and no em-dashes were added.",
    "Typecheck and build pass.",
    "'typecheck' clean, 'build' exit 0.",
])
def test_build_claims_are_found(text):
    assert [c["kind"] for c in E.find_claims(text)] == ["build"]


def test_one_message_can_claim_tests_and_build_once_each():
    text = "Build succeeds and all tests pass.\n\nSummary: all 12 tests pass, and the build is clean."
    claims = E.find_claims(text)
    assert [c["kind"] for c in claims] == ["tests", "build"]
    assert claims[0]["excerpt"] == "Build succeeds and all tests pass."


@pytest.mark.parametrize("dash", ["\u2014", "\u2013", "-"])
def test_a_spaced_dash_ends_the_clause_before_a_claim(dash):
    text = "Fixed the import error %s all 12 tests pass." % dash
    assert [c["kind"] for c in E.find_claims(text)] == ["tests"]


@pytest.mark.parametrize("text", [
    "Let me run the tests to make sure they pass.",
    "Once the tests pass, I'll commit.",
    "The tests should pass now.",
    "Tests are failing.",
    "3 tests fail and 97 pass.",
    "All tests pass except the flaky one.",
    "Do the tests pass?",
    "Your agent said tests passed 35 times.",
    "It claims all tests pass.",
    'The phrase "all tests pass" is common.',
    "```\n5 passed in 0.12s\n```",
    "> All tests pass",
    "| tests | pass |",
    "- [ ] Tests pass",
    "No failures, so no alert was sent.",
    "This test passes an invalid token to the API.",
    "I can build that sweep as a gated, auditable pass.",
    "Make the tests pass before merging.",
    "CI: all tests pass.",
    "All tests pass in CI.",
    "Tests pass with 2 failures.",
    "Next I'll make sure the tests pass.",
    "After the fix, the tests should pass.",
    "The build pass is next.",
    "Run 'all tests pass' through the checker.",
    "21/22 pass, and the one failure is my test.",
    "3 of 5 tests pass.",
    "Task 2 passed after one reviewed fix.",
    "Round 4 passed all three gates.",
    "The live test passed.",
    "The money test passed today.",
    "MONEY TEST PASSED: $99 real charge.",
    "Smoke tests pass (node v26.5.0, gh 2.96.0).",
    '-d "{\\"subject\\":\\"Email test passed\\"}"',
    "Result:\n\n    All tests pass\n",
    "Looks like all tests pass.",
    "If the build succeeds, deploy it.",
    "The build directory is clean.",
    "Tests passed previously, before the refactor.",
    "We need all tests passing before release.",
    "The build failed.",
    "Voice lint passed clean.",
    "I didn't run the tests, but they should pass.",
    "Tests passing is required before merge.",
    "Next step: a final test pass over the parser.",
    "Use a phrase, e.g. all tests pass, in the report.",
    # dropped for precision after the fresh-eyes review: a condition, history, or a plan word before the claim
    "The old test failed, but after the fix all tests pass.",
    "Verification evidence: full suite is 39 pass / 0 fail (exit 0).",
    "I'll commit next; all 24 tests pass.",
    "When I ran them, all 42 tests passed.",
])
def test_non_claims_are_skipped(text):
    assert E.find_claims(text) == []


def test_empty_and_missing_text_give_no_claims():
    assert E.find_claims("") == []
    assert E.find_claims(None) == []


# ---------------------------------------------------------------------------
# Shell commands: segments, runners, and file changes
# ---------------------------------------------------------------------------

def test_shell_parser_splits_on_operators_and_keeps_quoted_text_whole():
    segs = E.parse_shell("cd 'my app' && pytest -q 2>&1 | tail -3; echo \"a && b\" || true &")
    assert [s.words for s in segs] == [["cd", "my app"], ["pytest", "-q"], ["tail", "-3"],
                                       ["echo", "a && b"], ["true"]]
    assert [s.op for s in segs] == ["&&", "|", ";", "||", "&"]


def test_shell_parser_reads_redirects_and_heredoc_bodies():
    [seg] = E.parse_shell("cat > src/x.py <<'EOF'\nprint('pytest')\nEOF")
    assert seg.words == ["cat"]
    assert seg.redirects == [(">", "src/x.py")]
    assert seg.heredoc == "print('pytest')"
    [seg] = E.parse_shell("pytest -q > /tmp/out.txt 2>&1")
    assert seg.words == ["pytest", "-q"] and seg.redirects == [(">", "/tmp/out.txt")]


def test_shell_parser_handles_comments_line_breaks_and_subshells():
    segs = E.parse_shell("# run it\n(cd app && \\\n  npm test)\nls")
    assert [s.words for s in segs] == [["cd", "app"], ["npm", "test"], ["ls"]]


@pytest.mark.parametrize("command,kinds", [
    ("pytest -q", {"tests"}),
    ("python3 -m pytest tests/ -q 2>&1 | tail -5", {"tests"}),
    ("cd app && uv run -q --python 3.12 --with pytest python -m pytest -q -p no:cacheprovider tests", {"tests"}),
    ("PYTHONDONTWRITEBYTECODE=1 timeout 120 pytest", {"tests"}),
    ("env CI=1 npm test", {"tests"}),
    ("time pytest -x", {"tests"}),
    ("npm test", {"tests"}),
    ("npm run test:unit -- --watch=false", {"tests"}),
    ("pnpm test", {"tests"}),
    ("pnpm --filter web run test", {"tests"}),
    ("yarn test", {"tests"}),
    ("bun test src/app.test.ts 2>&1 | tail -15", {"tests"}),
    ("bun run test", {"tests"}),
    ("npx jest --ci", {"tests"}),
    ("npx vitest run", {"tests"}),
    ("node --test", {"tests"}),
    ("deno test -A", {"tests"}),
    ("npx playwright test", {"tests"}),
    ("go test ./...", {"tests", "build"}),
    ("cargo test --workspace", {"tests", "build"}),
    ("bundle exec rspec spec/models", {"tests"}),
    ("vendor/bin/phpunit", {"tests"}),
    ("mvn -q test", {"tests", "build"}),
    ("./gradlew test", {"tests", "build"}),
    ("dotnet test", {"tests", "build"}),
    ("swift test", {"tests", "build"}),
    ("xcodebuild test -scheme App", {"tests", "build"}),
    ("make test", {"tests"}),
    ("make -C sub check", {"tests"}),
    ("ctest --output-on-failure", {"tests"}),
    ("tox -e py312", {"tests"}),
    ("nox -s tests", {"tests"}),
    ("python -m unittest -v", {"tests"}),
    ("python3 tests/test_api.py", {"tests"}),
    ("python manage.py test", {"tests"}),
    ("bash -c \"pytest -q && echo ok\"", {"tests"}),
    ("for f in a b; do pytest $f; done", {"tests"}),
    ("npx tsc --noEmit", {"build"}),
    ("tsc -b", {"build"}),
    ("npm run build", {"build"}),
    ("pnpm build", {"build"}),
    ("yarn typecheck", {"build"}),
    ("cargo build --release", {"build"}),
    ("cargo check", {"build"}),
    ("cargo test --no-run", {"build"}),
    ("go build ./...", {"build"}),
    ("go vet ./...", {"build"}),
    ("mypy src", {"build"}),
    ("python -m mypy src", {"build"}),
    ("pyright", {"build"}),
    ("npx next build", {"build"}),
    ("npx vite build", {"build"}),
    ("swift build", {"build"}),
    ("dotnet build", {"build"}),
    ("mvn -DskipTests package", {"build"}),
    ("./gradlew assemble", {"build"}),
    ("cmake --build build", {"build"}),
    ("make", {"build"}),
    ("make build", {"build"}),
    ("scripts/test tests.test_portability", {"tests"}),
    ("./script/test", {"tests"}),
    ("bin/test", {"tests"}),
    ("./test.sh", {"tests"}),
    ("bash scripts/run_tests.sh -q", {"tests"}),
    ("sh ./run-tests", {"tests"}),
])
def test_runner_commands_are_recognized(command, kinds):
    assert E.command_kinds(command) == frozenset(kinds)


@pytest.mark.parametrize("command", [
    "pytest --version",
    "tsc --help",
    "npm install",
    "npm run lint",
    "git status",
    "echo pytest",
    "grep -r pytest .",
    "cat tests/test_x.py",
    "ls tests",
    "pip install pytest",
    "uv pip install pytest",
    "python3 script.py",
    "python3 -c 'import pytest'",
    "cat > test.sh <<'EOF'\npytest -q\nEOF",
    "rg 'npm test' README.md",
    "pytest --collect-only -q",
    "make lint",
    "cargo clippy",
    "jest --listTests",
    "git commit -m \"make tests pass\"",
    "python3 - <<'EOF'\nimport subprocess\nsubprocess.run(['pytest'])\nEOF",
    "test -e README.md && echo yes",
    "cat scripts/test",
])
def test_other_commands_are_not_runs(command):
    assert E.command_kinds(command) == frozenset()


def _steps(command):
    return [(kind, sorted(value) if isinstance(value, (set, frozenset)) else value)
            for kind, value, _index in E.shell_steps(command)]


@pytest.mark.parametrize("command,steps", [
    ("sed -i '' 's/a/b/' src/app.py", [("edit", ["src/app.py"])]),
    ("sed -i.bak -e 's/x/y/' a.py b.py", [("edit", ["a.py", "b.py"])]),
    ("perl -pi -e 's/x/y/' lib/x.pl", [("edit", ["lib/x.pl"])]),
    ("echo hi > notes.txt", [("edit", ["notes.txt"])]),
    ("cat > src/x.py <<'EOF'\nprint(1)\nEOF", [("edit", ["src/x.py"])]),
    ("pytest -q > /tmp/out.txt 2>&1", [("run", ["tests"]), ("edit", ["/tmp/out.txt"])]),
    ("pytest 2>/dev/null", [("run", ["tests"])]),
    ("echo x | tee -a log.md", [("edit", ["log.md"])]),
    ("python3 - <<'EOF'\np = 'src/a.py'\ns = open(p).read()\nopen(p, 'w').write(s.replace('a', 'b'))\nEOF",
     [("edit", ["src/a.py"])]),
    ("python3 - <<'EOF'\nimport json\nprint(json.load(open('data.json'))['x'])\nEOF", []),
    ("python3 -c \"open('out.txt', 'w').write('x')\"", [("edit", ["out.txt"])]),
    ("python3 -c \"u = 'example.com'; open('out.json', 'w').write(u)\"", [("edit", ["out.json"])]),
    ("node -e \"require('fs').writeFileSync('a.js', 'x')\"", [("edit", ["a.js"])]),
    ("python3 - <<'EOF'\nimport sys\nsys.stdout.write('hi')\nEOF", []),
    ("python3 - <<'EOF'\nfrom pathlib import Path\nPath(base, name).write_text(body)\nEOF", [("edit", None)]),
    ("black .", [("edit", None)]),
    ("ruff check --fix .", [("edit", None)]),
    ("ruff check .", []),
    ("ruff format --check .", []),
    ("prettier --write src", [("edit", None)]),
    ("npx prettier --check src", []),
    ("eslint --fix src", [("edit", None)]),
    ("gofmt -w .", [("edit", None)]),
    ("cargo fmt", [("edit", None)]),
    ("cargo fmt --check", []),
    ("git checkout -- src/app.py", [("edit", ["src/app.py"])]),
    ("git checkout main", [("edit", None)]),
    ("git checkout feature/login", [("edit", None)]),
    ("git checkout -b new-branch", []),
    ("git switch feature", [("edit", None)]),
    ("git switch -c new-branch", []),
    ("git stash", []),
    ("git stash pop", [("edit", None)]),
    ("git stash apply stash@{1}", [("edit", None)]),
    ("git stash list", []),
    ("git apply fix.patch", [("edit", None)]),
    ("git reset --hard HEAD", [("edit", None)]),
    ("git reset HEAD src/app.py", []),
    ("git restore src/app.py", [("edit", ["src/app.py"])]),
    ("git restore --staged src/app.py", []),
    ("git pull --rebase", [("edit", None)]),
    ("git status && git diff", []),
    ("git commit -am 'wip'", []),
    ("patch -p1 < fix.diff", [("edit", None)]),
    ("mv src/a.py src/b.py", [("edit", ["src/a.py", "src/b.py"])]),
    ("cp a.py b.py", [("edit", ["b.py"])]),
    ("rm src/old.py", [("edit", ["src/old.py"])]),
    ("sed -i '' 's/a/b/' src/app.py && pytest -q", [("edit", ["src/app.py"]), ("run", ["tests"])]),
    ("pytest -q && black .", [("run", ["tests"]), ("edit", None)]),
])
def test_shell_steps_list_runs_and_file_changes_in_order(command, steps):
    assert _steps(command) == steps


# ---------------------------------------------------------------------------
# How a run ended
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("command,output,exit_code,want", [
    # the exit code decides when it belongs to the runner
    ("pytest -q", "5 passed in 0.12s", 0, "pass"),
    ("pytest -q", "1 failed, 4 passed in 0.50s", 1, "fail"),
    ("pytest -q", "log line: 2 failed logins\n5 passed in 0.10s", 0, "pass"),
    ("pytest", "garbage", 2, "fail"),
    ("cd missing && pytest", "cd: no such file or directory: missing", 1, "fail"),
    ("set -o pipefail; pytest | tail -1", "", 0, "pass"),
    ("set -euo pipefail\npytest -q | tail -1", "", 1, "fail"),
    # a pipe, `;`, or `||` after the runner: the exit code is another program's
    ("pytest -q 2>&1 | tail -3", "1 failed, 4 passed in 0.50s", 0, "fail"),
    ("pytest -q 2>&1 | tail -3", "5 passed in 0.12s", 0, "pass"),
    ("pytest -q 2>&1 | tail -3", "", 0, "unknown"),
    ("pytest -q 2>&1 | head -20", "....", 0, "unknown"),
    ("pytest -q; echo done", "1 failed in 0.10s\ndone", 0, "fail"),
    ("pytest -q || true", "", 0, "unknown"),
    ("pytest -q && git push", "5 passed in 0.10s\nerror: failed to push", 1, "pass"),
    # echo markers right after the runner
    ("pytest -q && echo ALL_OK", "ALL_OK", None, "pass"),
    ("pytest -q && echo OK || echo BROKEN", "BROKEN", None, "fail"),
    ("pytest -q 2>&1 | tail -1 && echo ALL_OK", "ALL_OK", None, "unknown"),
    # runner summaries
    ("pytest", "no tests ran in 0.01s", None, "fail"),
    ("pytest -q | tail -1", "3 skipped in 0.01s", 0, "fail"),
    ("npm test", "Tests:       1 failed, 9 passed, 10 total", None, "fail"),
    ("npx jest 2>&1 | tail", "Tests:       10 passed, 10 total", 0, "pass"),
    ("npx vitest run | tail -5", "      Tests  2 failed | 8 passed (10)", 0, "fail"),
    ("npx vitest run | tail -5", " Test Files  3 passed (3)\n      Tests  10 passed (10)", 0, "pass"),
    ("npx mocha | tail", "  5 passing (20ms)\n  1 failing", 0, "fail"),
    ("npx mocha | tail", "  5 passing (20ms)", 0, "pass"),
    ("bun test 2>&1 | tail -4", " 12 pass\n 0 fail\nRan 12 tests across 3 files. [40.00ms]", 0, "pass"),
    ("bun test 2>&1 | tail -4", " 11 pass\n 1 fail\nRan 12 tests across 3 files.", 0, "fail"),
    ("node --test | tail", "# pass 5\n# fail 0", 0, "pass"),
    ("node --test | tail", "ℹ pass 4\nℹ fail 1", 0, "fail"),
    ("deno test | tail", "ok | 5 passed | 0 failed (20ms)", 0, "pass"),
    ("deno test | tail", "FAILED | 4 passed | 1 failed (20ms)", 0, "fail"),
    ("go test ./... | tail", "ok  \texample.com/a\t0.1s\nFAIL\texample.com/b\t0.2s", 0, "fail"),
    ("go test ./... | tail", "ok  \texample.com/a\t0.1s\nok  \texample.com/b\t0.2s", 0, "pass"),
    ("cargo test | tail", "test result: ok. 5 passed; 0 failed; 0 ignored", 0, "pass"),
    ("cargo test | tail", "test result: FAILED. 4 passed; 1 failed; 0 ignored", 0, "fail"),
    ("rspec | tail", "5 examples, 0 failures", 0, "pass"),
    ("rspec | tail", "5 examples, 1 failure", 0, "fail"),
    ("vendor/bin/phpunit | tail", "OK (5 tests, 10 assertions)", 0, "pass"),
    ("vendor/bin/phpunit | tail", "FAILURES!\nTests: 5, Assertions: 9, Failures: 1.", 0, "fail"),
    ("mvn test | tail", "[INFO] BUILD SUCCESS", 0, "pass"),
    ("mvn test | tail", "[INFO] BUILD FAILURE", 0, "fail"),
    ("./gradlew test | tail", "BUILD SUCCESSFUL in 3s", 0, "pass"),
    ("./gradlew test | tail", "BUILD FAILED in 3s", 0, "fail"),
    ("dotnet test | tail", "Passed!  - Failed:     0, Passed:     5, Skipped:     0, Total:     5", 0, "pass"),
    ("dotnet test | tail", "Failed!  - Failed:     1, Passed:     4, Skipped:     0, Total:     5", 0, "fail"),
    ("swift test 2>&1 | tail", "Executed 5 tests, with 0 failures (0 unexpected) in 0.1 (0.1) seconds", 0, "pass"),
    ("swift test 2>&1 | tail", "Executed 5 tests, with 1 failure (0 unexpected) in 0.1 (0.1) seconds", 0, "fail"),
    ("swift test 2>&1 | tail", "✔ Test run with 12 tests in 3 suites passed after 0.1 seconds.", 0, "pass"),
    ("swift test 2>&1 | tail", "✘ Test run with 12 tests in 3 suites failed after 0.1 seconds with 1 issue.",
     0, "fail"),
    ("xcodebuild test -scheme A | tail", "** TEST SUCCEEDED **", 0, "pass"),
    ("xcodebuild test -scheme A | tail", "** TEST FAILED **", 0, "fail"),
    ("ctest | tail", "100% tests passed, 0 tests failed out of 5", 0, "pass"),
    ("ctest | tail", "80% tests passed, 1 tests failed out of 5", 0, "fail"),
    ("tox | tail", "  py312: OK (2.34=setup[0.10]+cmd[2.24] seconds)\n  congratulations :) (2.40 seconds)", 0,
     "pass"),
    ("tox | tail", "  py312: FAIL code 1 (1.23 seconds)\n  evaluation failed :( (1.30 seconds)", 0, "fail"),
    ("python -m unittest | tail", "Ran 5 tests in 0.001s\n\nOK", 0, "pass"),
    ("python -m unittest | tail", "Ran 5 tests in 0.001s\n\nFAILED (failures=1)", 0, "fail"),
    ("npm test 2>&1 | tail", "npm error Lifecycle script `test` failed with error:", 0, "fail"),
    ("make test 2>&1 | tail", "make: *** [Makefile:3: test] Error 1", 0, "fail"),
    ("pytest -q | tail -3", "zsh: command not found: pytest", 0, "fail"),
    # several runs in one command: a failure anywhere counts
    ("pytest a -q; pytest b -q", "1 failed in 0.10s\n3 passed in 0.10s", 0, "fail"),
])
def test_test_run_outcomes(command, output, exit_code, want):
    assert E.run_outcome(command, output, exit_code, "tests")[0] == want


@pytest.mark.parametrize("command,output,exit_code,want", [
    ("npx tsc --noEmit", "", 0, "pass"),
    ("npx tsc --noEmit", "src/a.ts(3,5): error TS2322: Type 'x' is not assignable.", 2, "fail"),
    ("npx tsc --noEmit 2>&1 | tail -3", "", 0, "pass"),
    ("npx tsc --noEmit 2>&1 | tail -3", "src/a.ts(3,5): error TS2322: Type 'x' is not assignable.", 0, "fail"),
    ("npx tsc --noEmit 2>&1 | grep -c error", "0", 0, "unknown"),
    ("npx tsc --noEmit && echo TSC_OK", "TSC_OK", None, "pass"),
    # two build runs in one command: the second one's result cannot be read
    ("npx tsc --noEmit && echo TSC_OK && npm run build 2>&1 | grep -c x", "TSC_OK\n3", 0, "unknown"),
    ("mypy src | tail -1", "Success: no issues found in 12 source files", 0, "pass"),
    ("mypy src | tail -1", "Found 2 errors in 1 file (checked 12 source files)", 0, "fail"),
    ("pyright | tail -1", "0 errors, 0 warnings, 0 informations", 0, "pass"),
    ("pyright | tail -1", "2 errors, 0 warnings, 0 informations", 0, "fail"),
    ("cargo build 2>&1 | tail", "error[E0425]: cannot find value `x` in this scope", 0, "fail"),
    ("cargo build 2>&1 | tail -1", "    Finished `dev` profile [unoptimized + debuginfo] target(s) in 0.52s", 0,
     "pass"),
    ("npm run build 2>&1 | tail", "✓ Compiled successfully", 0, "pass"),
    ("npm run build 2>&1 | tail", "Failed to compile.", 0, "fail"),
    ("npx vite build | tail -2", "✓ built in 1.23s", 0, "pass"),
    ("go build ./... 2>&1 | head", "./main.go:5:2: undefined: x", 0, "fail"),
    ("go vet ./... 2>&1 | head", "", 0, "pass"),
    ("go test ./... | tail", "ok  \texample.com/a\t0.1s", 0, "pass"),
])
def test_build_run_outcomes(command, output, exit_code, want):
    assert E.run_outcome(command, output, exit_code, "build")[0] == want


def test_outcome_detail_names_the_evidence():
    assert E.run_outcome("pytest -q | tail -1", "1 failed, 4 passed in 0.5s", 0, "tests") == \
        ("fail", "1 failed, 4 passed")
    assert E.run_outcome("pytest -q", "", 3, "tests") == ("fail", "exit code 3")
    assert E.run_outcome("pytest -q", "", 0, "tests") == ("pass", "exit code 0")


def test_a_cut_off_output_cannot_show_a_pass_by_the_absence_of_errors():
    assert E.run_outcome("npx tsc --noEmit | tail -3", "x" * 50, 0, "build", truncated=True)[0] == "unknown"
    assert E.run_outcome("pytest | tail -3", "...", 0, "tests", truncated=True)[0] == "unknown"


def test_a_timed_out_run_did_not_pass():
    assert E.run_outcome("pytest", "Command timed out after 2m 0.0s", None, "tests", error=True) == \
        ("fail", "did not finish")
    assert E.run_outcome("pytest", "Error: something odd", None, "tests", error=True)[0] == "unknown"


# ---------------------------------------------------------------------------
# Which changed files can change a test result
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("path,matters", [
    ("src/app.py", True),
    ("/work/app/src/app.py", True),
    ("tests/test_app.py", True),
    ("package.json", True),
    (None, True),
    ("README.md", False),
    ("docs/guide.rst", False),
    ("notes.txt", False),
    ("CHANGELOG", False),
    ("img/logo.png", False),
    ("/tmp/x.py", False),
    ("/private/tmp/claude-501/scratch/x.json", False),
    ("/var/folders/ab/T/x.py", False),
    ("/dev/null", False),
    ("node_modules/x/index.js", False),
    ("dist/app.js", False),
    (".pytest_cache/v/cache/nodeids", False),
    ("pkg/__pycache__/a.cpython-312.pyc", False),
    (".superpowers/sdd/run-1/review.diff", False),
    ("build.log", False),
    ("fix.patch", False),
    (".github/workflows/ci.yml", True),
    (".github/tests/test_check.py", True),
    (".cargo/config.toml", True),
    (".circleci/config.yml", True),
    (".husky/pre-commit", True),
    (".config/tool.toml", True),
    ("/work/app/.vscode/settings.json", False),
    (".idea/workspace.xml", False),
    (".git/info/exclude", False),
    (".nox/py312/bin/activate", False),
    (".superpowers/sdd/run-1/plan.json", False),
    (".env", True),
    ("config/.eslintrc.json", True),
    ("$QUERY_PLAN_FILE", False),
    ("$f", False),
    ("$ROOT/src/app.py", True),
    ("site", False),
    ("site/index.html", False),
    ("tmp", False),
    ("tmp/x.py", False),
    ("_build/html/index.html", False),
    ("/work/app/site", False),
    ("/work/app/pangram/tmp", False),
    ("/work/app/scripts/build", True),
    ("state/handoff_defects.jsonl", False),
    ("tests/fixtures/cases.jsonl", True),
])
def test_path_matters(path, matters):
    assert E.path_matters(path) is matters


def test_path_matters_ignores_the_harness_folders_in_home():
    home = "/Users/sam"
    assert E.path_matters(home + "/.claude/projects/-w/memory/notes.json", home=home) is False
    assert E.path_matters(home + "/.codex/config.toml", home=home) is False
    assert E.path_matters(home + "/code/app/.claude/settings.json", home=home) is False  # a tool's hidden folder
    assert E.path_matters(home + "/code/app/src/settings.json", home=home) is True


def test_path_matters_counts_temporary_files_inside_the_working_folder():
    assert E.path_matters("/tmp/proj/src/a.py", cwd="/tmp/proj") is True
    assert E.path_matters("/tmp/other/a.py", cwd="/tmp/proj") is False
    assert E.path_matters("/tmp/proj/README.md", cwd="/tmp/proj") is False


@pytest.mark.parametrize("path,matters", [
    # Source, scripts, and tests in editor and agent folders can change a result.
    (".claude/tests/test_check.py", True),
    (".vscode/tests/test_check.py", True),
    (".claude/hooks/check.py", True),
    (".claude/hooks/guard.sh", True),
    (".claude/tests/fixtures/case.json", True),
    # Data files in test and fixture folders can change a result.
    ("tests/fixtures/answer.txt", True),
    ("pkg/testdata/golden.txt", True),
    ("src/__snapshots__/view.png", True),
    ("spec/fixtures/report.pdf", True),
    # Plans, notes, settings, and state files that agents and editors keep in their own folders cannot.
    (".claude/settings.json", False),
    (".claude/settings.local.json", False),
    (".claude/plans/x.md", False),
    (".claude/agents/reviewer.md", False),
    (".superpowers/plan.json", False),
    (".superpowers/sdd/run-1/state.yaml", False),
    (".cursor/rules/style.mdc", False),
    (".codex/config.toml", False),
    (".vscode/launch.json", False),
    (".idea/modules.xml", False),
    (".gemini/notes.txt", False),
    # Version control folders are skipped whole; prose and logs in test folders do not count.
    (".git/hooks/pre-commit", False),
    (".hg/hgrc", False),
    ("tests/README.md", False),
    ("tests/run.log", False),
])
def test_path_matters_inside_the_working_folder(path, matters):
    assert E.path_matters("/work/app/" + path, cwd="/work/app") is matters


# ---------------------------------------------------------------------------
# Transcript builders. The record helpers are copied from
# skills/evals/shared/test_transcripts.py (facts file Q1 shapes).
# ---------------------------------------------------------------------------

_ids = itertools.count(1)
SID = "5f0c3a1e-8d7b-4c2a-9e61-0123456789ab"
TID = "019a2b3c-4d5e-7f60-8a9b-0c1d2e3f4a5b"


def write_jsonl(path, records):
    os.makedirs(os.path.dirname(str(path)), exist_ok=True)
    with open(str(path), "w", encoding="utf-8") as fh:
        for rec in records:
            fh.write(rec if isinstance(rec, str) else json.dumps(rec))
            fh.write("\n")
    return str(path)


def cc_env(ts, **kw):
    rec = {
        "parentUuid": None, "isSidechain": False, "userType": "external",
        "cwd": "/work/app", "sessionId": SID, "version": "2.1.284",
        "gitBranch": "main", "entrypoint": "cli", "slug": "calm-river",
        "uuid": "u-%d" % next(_ids), "timestamp": ts,
    }
    rec.update(kw)
    return rec


def cc_usage(inp=3, out=10):
    return {"input_tokens": inp, "cache_creation_input_tokens": 0, "cache_read_input_tokens": 0,
            "cache_creation": {"ephemeral_5m_input_tokens": 0, "ephemeral_1h_input_tokens": 0},
            "output_tokens": out, "service_tier": "standard"}


def cc_assistant(mid, block, ts, usage=None, model="claude-opus-5-5", **kw):
    msg = {"id": mid, "type": "message", "role": "assistant", "model": model,
           "content": [block], "stop_reason": None, "stop_sequence": None,
           "usage": usage if usage is not None else cc_usage()}
    kw.setdefault("requestId", "req_" + mid)
    return cc_env(ts, type="assistant", message=msg, **kw)


def text(t):
    return {"type": "text", "text": t}


def tool_use(tid, name, inp):
    return {"type": "tool_use", "id": tid, "name": name, "input": inp, "caller": {"type": "direct"}}


def cc_user(content, ts, **kw):
    return cc_env(ts, type="user", message={"role": "user", "content": content}, **kw)


def cc_result(tid, content, ts, is_error=None, tool_use_result=None, **kw):
    block = {"tool_use_id": tid, "type": "tool_result", "content": content}
    if is_error is not None:
        block["is_error"] = is_error
    if tool_use_result is not None:
        kw["toolUseResult"] = tool_use_result
    return cc_user([block], ts, sourceToolAssistantUUID="u-x", **kw)


def bash_result(stdout="", interrupted=False, **extra):
    r = {"stdout": stdout, "stderr": "", "interrupted": interrupted, "isImage": False, "noOutputExpected": False}
    r.update(extra)
    return r


def cc_project(home, cwd="/work/app"):
    return os.path.join(str(home), ".claude", "projects", "".join(c if c.isalnum() else "-" for c in cwd))


_BASE = int(time.time()) - 86400  # transcripts written "yesterday", so every window holds them


def stamp(seconds, base=None):
    t = (_BASE if base is None else base) + seconds
    return time.strftime("%Y-%m-%dT%H:%M:%S.000Z", time.gmtime(t))


class CC:
    """A Claude Code transcript, one second per record unless `at` is given."""

    def __init__(self, start=0, sidechain=False, agent_id=None, cwd="/work/app"):
        self.records, self.t = [], start
        self.extra = {"isSidechain": True, "agentId": agent_id} if sidechain else {}
        self.cwd = cwd

    def _ts(self, at=None):
        if at is not None:
            self.t = at
        self.t += 1
        return stamp(self.t)

    def _env(self):
        return dict(self.extra, cwd=self.cwd)

    def user(self, t, at=None):
        self.records.append(cc_user(t, self._ts(at), **self._env()))
        return self

    def say(self, t, at=None):
        mid = "msg_%d" % next(_ids)
        self.records.append(cc_assistant(mid, text(t), self._ts(at), **self._env()))
        return self

    def call(self, name, inp, content, at=None, is_error=None, tool_use_result=None, say=None, **kw):
        mid, tid = "msg_%d" % next(_ids), "toolu_%d" % next(_ids)
        ts = self._ts(at)
        if say is not None:  # text and the tool call in one API response (two records, one message id)
            self.records.append(cc_assistant(mid, text(say), ts, **self._env()))
        self.records.append(cc_assistant(mid, tool_use(tid, name, inp), ts, **self._env()))
        if content is not None:
            self.records.append(cc_result(tid, content, self._ts(), is_error=is_error,
                                          tool_use_result=tool_use_result, **dict(self._env(), **kw)))
        return self

    def bash(self, command, output="", exit_code=0, background=False, interrupted=False, at=None, say=None,
             no_meta=False, **kw):
        inp = {"command": command, "description": "run"}
        if background:
            inp["run_in_background"] = True
            return self.call("Bash", inp, "Command running in background with ID: b1", at=at, say=say,
                             tool_use_result=bash_result("", backgroundTaskId="b1"))
        if no_meta:  # like a subagent file: no toolUseResult, so no exit code
            return self.call("Bash", inp, output, at=at, say=say, is_error=bool(exit_code) or None)
        if exit_code == 0:
            return self.call("Bash", inp, output, at=at, say=say,
                             tool_use_result=bash_result(output, interrupted=interrupted), **kw)
        return self.call("Bash", inp, "Exit code %d\n%s" % (exit_code, output), at=at, say=say, is_error=True,
                         tool_use_result="Error: Exit code %d" % exit_code, **kw)

    def denied_bash(self, command):
        return self.call("Bash", {"command": command}, "The user doesn't want to proceed with this tool use.",
                         is_error=True, toolDenialKind="user-rejected")

    def edit(self, path, ok=True, at=None):
        if ok:
            return self.call("Edit", {"file_path": path, "old_string": "a", "new_string": "b"},
                             "The file %s has been updated." % path, at=at)
        return self.call("Edit", {"file_path": path, "old_string": "a", "new_string": "b"},
                         "<tool_use_error>String to replace not found in file.</tool_use_error>", is_error=True, at=at)

    def write(self, path, at=None):
        return self.call("Write", {"file_path": path, "content": "x"}, "File created successfully at: " + path, at=at)

    def save(self, home, name=SID):
        return write_jsonl(os.path.join(cc_project(home, self.cwd), name + ".jsonl"), self.records)

    def save_subagent(self, home, agent_id, parent=SID):
        return write_jsonl(os.path.join(cc_project(home, self.cwd), parent, "subagents", "agent-%s.jsonl" % agent_id),
                           self.records)


def cc_load(tmp_path, builder, name=SID):
    return T.load_session("claude-code", builder.save(tmp_path / "home", name))


def labels(session, **kw):
    return [c["label"] for c in E.label_claims(session, **kw)]


_ordinal = itertools.count(1)


def cx(ts, kind, payload):
    return {"timestamp": ts, "ordinal": next(_ordinal), "type": kind, "payload": payload}


def cx_meta(ts, tid=TID, cwd="/work/app", source="vscode", **extra):
    payload = {"id": tid, "session_id": tid, "timestamp": ts, "cwd": cwd, "originator": "Codex Desktop",
               "cli_version": "0.155.0", "source": source, "thread_source": "user",
               "model_provider": "openai", "base_instructions": {"text": "You are Codex."},
               "history_mode": "legacy", "context_window": {"window_id": "w1"}}
    payload.update(extra)
    rec = cx(ts, "session_meta", payload)
    rec["git"] = {"commit_hash": "abc", "branch": "main", "repository_url": None}
    return rec


def cx_turn(ts, model="gpt-6-astra"):
    return cx(ts, "turn_context", {"turn_id": "turn-1", "cwd": "/work/app", "model": model,
                                   "approval_policy": "on-request", "effort": "medium",
                                   "sandbox_policy": {"type": "workspace-write", "network_access": False},
                                   "current_date": "2026-09-25", "timezone": "UTC"})


def cx_msg(ts, role, *texts):
    item = "output_text" if role == "assistant" else "input_text"
    return cx(ts, "response_item", {"type": "message", "role": role,
                                    "content": [{"type": item, "text": t} for t in texts]})


def cx_exec(ts, call_id, script):
    return cx(ts, "response_item", {"type": "custom_tool_call", "name": "exec", "input": script,
                                    "call_id": call_id, "status": "completed"})


def cx_custom_out(ts, call_id, text_out):
    return cx(ts, "response_item", {"type": "custom_tool_call_output", "call_id": call_id,
                                    "output": [{"type": "input_text", "text": text_out}]})


def cx_item(ts, item):
    return cx(ts, "event_msg", {"type": "item_completed", "thread_id": TID, "turn_id": "turn-1",
                                "started_at_ms": 1, "completed_at_ms": 2, "item": item})


def cx_command(item_id, command, exit_code=0, status="completed", output=""):
    return {"type": "CommandExecution", "id": item_id, "process_id": "p1",
            "command": ["/bin/zsh", "-lc", command], "cwd": "/work/app",
            "parsed_cmd": [{"type": "unknown", "cmd": command}], "source": "unified_exec_startup",
            "status": status, "stdout": output, "stderr": "", "aggregated_output": output,
            "exit_code": exit_code, "duration": {"secs": 1, "nanos": 0}, "formatted_output": output}


class CX:
    """A Codex rollout in code mode: shell work runs through `exec` scripts."""

    def __init__(self, start=0):
        self.t = start
        self.records = [cx_meta(stamp(start)), cx_turn(stamp(start))]

    def _ts(self):
        self.t += 1
        return stamp(self.t)

    def user(self, t):
        self.records.append(cx_msg(self._ts(), "user", t))
        return self

    def say(self, t):
        self.records.append(cx_msg(self._ts(), "assistant", t))
        return self

    def run(self, command, output="", exit_code=0):
        cid, iid = "call_%d" % next(_ids), "item_%d" % next(_ids)
        self.records.append(cx_exec(self._ts(), cid, "text(await tools.exec_command({cmd: %s}))" % json.dumps(command)))
        self.records.append(cx_item(self._ts(), cx_command(iid, command, exit_code=exit_code,
                                                           status="completed" if exit_code == 0 else "failed",
                                                           output=output)))
        self.records.append(cx_custom_out(self._ts(), cid, "Script completed\nWall time 1.0 seconds\nOutput:\n"))
        return self

    def patch(self, path):
        cid = "call_%d" % next(_ids)
        body = "*** Begin Patch\n*** Update File: %s\n@@\n-a\n+b\n*** End Patch" % path
        self.records.append(cx(self._ts(), "response_item", {"type": "custom_tool_call", "name": "apply_patch",
                                                             "input": body, "call_id": cid, "status": "completed"}))
        self.records.append(cx_custom_out(self._ts(), cid, "Exit code: 0\nWall time: 0 seconds\nOutput:\nSuccess."))
        return self

    def script(self, js, output):
        """A code-mode `exec` script with no per-command records, as Codex Desktop writes them."""
        cid = "call_%d" % next(_ids)
        self.records.append(cx_exec(self._ts(), cid, js))
        self.records.append(cx_custom_out(self._ts(), cid, output))
        return self

    def wait(self, cell, output):
        cid = "call_%d" % next(_ids)
        self.records.append(cx(self._ts(), "response_item", {"type": "function_call", "name": "wait", "call_id": cid,
                                                             "arguments": json.dumps({"cell_id": cell,
                                                                                      "yield_time_ms": 1000})}))
        self.records.append(cx(self._ts(), "response_item", {"type": "function_call_output", "call_id": cid,
                                                             "output": output}))
        return self

    def save(self, home, name="rollout-2026-09-25T10-00-00-%s.jsonl" % TID):
        return write_jsonl(os.path.join(str(home), ".codex", "sessions", "2026", "09", "25", name), self.records)


# ---------------------------------------------------------------------------
# Labels, one session at a time
# ---------------------------------------------------------------------------

PASS = ("pytest -q", "5 passed in 0.12s", 0)
FAIL = ("pytest -q", "1 failed, 4 passed in 0.50s", 1)


def test_a_passing_run_with_no_change_after_it_backs_the_claim(tmp_path):
    s = cc_load(tmp_path, CC().user("fix it").bash(*PASS).say("All 5 tests pass."))
    [claim] = E.label_claims(s)
    assert claim["label"] == "backed" and claim["kind"] == "tests"
    assert claim["run"]["command"] == "pytest -q" and claim["run"]["result"] == "pass"
    assert claim["excerpt"] == "All 5 tests pass."
    assert claim["ts"] == stamp(4) and claim["session"] == SID and claim["harness"] == "claude-code"


def test_a_code_change_after_the_passing_run_makes_the_claim_stale(tmp_path):
    s = cc_load(tmp_path, CC().bash(*PASS).edit("/work/app/src/app.py").say("All 5 tests pass."))
    [claim] = E.label_claims(s)
    assert claim["label"] == "stale" and claim["changed"] == ["/work/app/src/app.py"]


def test_a_documentation_change_after_the_run_keeps_the_claim_backed(tmp_path):
    s = cc_load(tmp_path, CC().bash(*PASS).edit("/work/app/README.md").write("/tmp/notes.py").say("All tests pass."))
    assert labels(s) == ["backed"]


@pytest.mark.parametrize("path", ["/work/app/.github/tests/test_check.py", "/work/app/.cargo/config.toml",
                                  "/work/app/.claude/hooks/check.py", "/work/app/.vscode/tests/test_check.py",
                                  "/work/app/tests/fixtures/answer.txt"])
def test_a_change_in_a_hidden_folder_with_tests_or_build_settings_makes_the_claim_stale(tmp_path, path):
    s = cc_load(tmp_path, CC().bash(*PASS).edit(path).say("All 5 tests pass."))
    [claim] = E.label_claims(s)
    assert claim["label"] == "stale" and claim["changed"] == [path]


def test_changes_to_test_code_in_agent_folders_and_to_fixtures_make_the_claim_stale(tmp_path):
    paths = ["/work/app/.claude/tests/test_check.py", "/work/app/.vscode/tests/test_check.py",
             "/work/app/.claude/hooks/check.py", "/work/app/tests/fixtures/answer.txt"]
    builder = CC().bash(*PASS)
    for path in paths:
        builder.edit(path)
    [claim] = E.label_claims(cc_load(tmp_path, builder.say("All 5 tests pass.")))
    assert claim["label"] == "stale" and claim["changed"] == paths


def test_agent_plans_and_settings_written_after_the_run_keep_the_claim_backed(tmp_path):
    builder = CC().bash(*PASS)
    for path in ("/work/app/.claude/settings.json", "/work/app/.claude/plans/x.md",
                 "/work/app/.superpowers/plan.json", "/work/app/.vscode/settings.json"):
        builder.edit(path)
    assert labels(cc_load(tmp_path, builder.say("All 5 tests pass."))) == ["backed"]


@pytest.mark.parametrize("path", ["/work/app/.git/info/exclude", "/work/app/.pytest_cache/v/cache/lastfailed"])
def test_a_change_in_a_version_control_or_cache_folder_keeps_the_claim_backed(tmp_path, path):
    s = cc_load(tmp_path, CC().bash(*PASS).edit(path).say("All 5 tests pass."))
    assert labels(s) == ["backed"]


def test_a_failing_latest_run_contradicts_the_claim(tmp_path):
    s = cc_load(tmp_path, CC().bash(*FAIL).say("All tests pass."))
    [claim] = E.label_claims(s)
    assert claim["label"] == "contradicted" and claim["run"]["detail"] == "exit code 1"


def test_a_fix_after_a_failing_run_without_a_rerun_is_still_contradicted(tmp_path):
    s = cc_load(tmp_path, CC().bash(*FAIL).edit("/work/app/src/app.py").say("Fixed; all tests pass now."))
    assert labels(s) == ["contradicted"]


def test_no_run_before_the_claim_makes_it_unsupported(tmp_path):
    s = cc_load(tmp_path, CC().edit("/work/app/src/app.py").say("All tests pass."))
    [claim] = E.label_claims(s)
    assert claim["label"] == "unsupported" and claim["run"] is None


def test_a_run_whose_result_cannot_be_read_makes_the_claim_unclear(tmp_path):
    s = cc_load(tmp_path, CC().bash("pytest -q | tail -3", "", 0).say("All tests pass."))
    assert labels(s) == ["unclear"]


def test_the_latest_run_decides(tmp_path):
    s = cc_load(tmp_path, CC().bash(*FAIL).bash(*PASS).say("All tests pass."))
    assert labels(s) == ["backed"]
    s = cc_load(tmp_path, CC().bash(*PASS).bash(*FAIL).say("All tests pass."), name="other")
    assert labels(s) == ["contradicted"]


def test_a_failed_edit_changes_nothing(tmp_path):
    s = cc_load(tmp_path, CC().bash(*PASS).edit("/work/app/src/app.py", ok=False).say("All tests pass."))
    assert labels(s) == ["backed"]


def test_build_claims_need_a_build_run(tmp_path):
    s = cc_load(tmp_path, CC().bash("npx tsc --noEmit", "", 0).say("tsc is clean."))
    assert labels(s) == ["backed"]
    s = cc_load(tmp_path, CC().bash(*PASS).say("The build succeeds."), name="other")
    assert labels(s) == ["unsupported"]


def test_go_test_compiles_the_code_so_it_backs_a_build_claim(tmp_path):
    s = cc_load(tmp_path, CC().bash("go test ./...", "ok  \texample.com/app\t0.1s", 0).say("It compiles cleanly."))
    assert labels(s) == ["backed"]


def test_a_claim_made_before_the_run_in_the_same_response_is_unsupported(tmp_path):
    s = cc_load(tmp_path, CC().bash(*PASS, say="All tests pass."))
    assert labels(s) == ["unsupported"]


def test_changes_made_by_shell_commands_count(tmp_path):
    s = cc_load(tmp_path, CC().bash(*PASS).bash("sed -i '' 's/a/b/' src/app.py", "", 0).say("All tests pass."))
    assert labels(s) == ["stale"]
    s = cc_load(tmp_path, CC().bash("sed -i '' 's/a/b/' src/app.py && pytest -q", "5 passed in 0.1s", 0)
                .say("All tests pass."), name="b")
    assert labels(s) == ["backed"]
    s = cc_load(tmp_path, CC().bash("pytest -q && black .", "5 passed in 0.1s", 0).say("All tests pass."), name="c")
    assert labels(s) == ["stale"]


def test_a_denied_run_is_not_a_run(tmp_path):
    s = cc_load(tmp_path, CC().denied_bash("pytest -q").say("All tests pass."))
    assert labels(s) == ["unsupported"]


def test_interrupted_and_background_runs_are_unclear(tmp_path):
    s = cc_load(tmp_path, CC().bash("pytest -q", "", 0, interrupted=True).say("All tests pass."))
    assert labels(s) == ["unclear"]
    s = cc_load(tmp_path, CC().bash("pytest -q", background=True).say("All tests pass."), name="b")
    assert labels(s) == ["unclear"]


def test_a_run_recorded_without_an_exit_code_reads_the_error_flag(tmp_path):
    s = cc_load(tmp_path, CC().bash("pytest -q", "", 0, no_meta=True).say("All tests pass."))
    assert labels(s) == ["backed"]
    s = cc_load(tmp_path, CC().bash("pytest -q | tail -1", "", 0, no_meta=True).say("All tests pass."), name="b")
    assert labels(s) == ["unclear"]


def test_one_claim_per_kind_per_message(tmp_path):
    s = cc_load(tmp_path, CC().bash(*PASS).say("All tests pass.\n\nTo recap: the test suite passes."))
    assert labels(s) == ["backed"]


def test_claims_before_the_window_are_left_out(tmp_path):
    s = cc_load(tmp_path, CC().bash(*PASS).say("All tests pass.").say("All 5 tests pass.", at=100))
    since = E.epoch(stamp(50))
    assert [c["ts"] for c in E.label_claims(s, since=since)] == [stamp(101)]


def test_subagent_runs_and_changes_count_for_the_main_session(tmp_path):
    home = tmp_path / "home"
    main = CC().user("build it").call("Agent", {"description": "impl", "prompt": "go"}, "done", at=1)
    main.say("All 12 tests pass.", at=30)
    sub = CC(start=5, sidechain=True, agent_id="a1").user("go").bash(*PASS).say("All tests pass.")
    s = T.load_session("claude-code", main.save(home))
    child = T.load_session("claude-code", sub.save_subagent(home, "a1"))
    assert labels(s) == ["unsupported"]
    assert labels(s, subagents=[child]) == ["backed"]
    sub.edit("/work/app/src/app.py")
    child = T.load_session("claude-code", sub.save_subagent(home, "a1"))
    assert labels(s, subagents=[child]) == ["stale"]


def test_codex_sessions_are_labeled_the_same_way(tmp_path):
    home = tmp_path / "home"
    s = T.load_session("codex", CX().user("fix").run("pytest -q", "5 passed in 0.1s").say("All tests pass.")
                       .save(home))
    assert labels(s) == ["backed"]
    s = T.load_session("codex", CX().run("pytest -q", "5 passed in 0.1s").patch("src/app.py").say("All tests pass.")
                       .save(home, name="rollout-b.jsonl"))
    assert labels(s) == ["stale"]
    s = T.load_session("codex", CX().run("pytest -q", "1 failed in 0.1s", 1).say("All tests pass.")
                       .save(home, name="rollout-c.jsonl"))
    assert labels(s) == ["contradicted"]


def test_last_test_run_reports_the_last_run_and_the_changes_after_it(tmp_path):
    s = cc_load(tmp_path, CC().bash(*PASS).edit("/work/app/src/app.py").edit("/work/app/NOTES.md"))
    state = E.last_test_run(s)
    assert state["run"]["result"] == "pass" and state["changed"] == ["/work/app/src/app.py"]
    assert E.last_test_run(cc_load(tmp_path, CC().edit("/work/app/src/app.py"), name="b"))["run"] is None


# ---------------------------------------------------------------------------
# claims.py scan
# ---------------------------------------------------------------------------

def run_cli(capsys, *argv):
    code = C.main(list(argv))
    out = capsys.readouterr().out
    return code, out


def scan_json(capsys, *argv):
    code, out = run_cli(capsys, "scan", "--json", *argv)
    return code, json.loads(out)


def three_sessions(home):
    CC().user("fix").bash(*PASS).say("All 5 tests pass.").save(home, "s-backed")
    CC().bash(*PASS).edit("/work/app/src/app.py").say("All 5 tests pass.").save(home, "s-stale")
    CX().run("pytest -q", "1 failed in 0.1s", 1).say("All tests pass.").save(home)


def test_scan_counts_claims_by_label_kind_and_harness(tmp_path, capsys):
    three_sessions(tmp_path / "home")
    code, data = scan_json(capsys)
    assert code == 0
    assert data["claims"] == 3 and data["not_backed"] == 2 and data["sessions"] == 3
    assert data["by_label"] == {"backed": 1, "stale": 1, "contradicted": 1, "unsupported": 0, "unclear": 0}
    assert data["by_harness"]["claude-code"]["stale"] == 1 and data["by_harness"]["codex"]["contradicted"] == 1
    assert data["by_kind"]["tests"]["backed"] == 1 and data["by_kind"]["build"]["backed"] == 0
    assert data["headline"] == ("Found 3 claims that tests or builds passed in the last 30 days. "
                                "2 of those claims were not backed by a passing run.")
    assert set(data) >= {"headline", "window_days", "sessions", "subagent_sessions", "harnesses", "claims",
                         "not_backed", "by_label", "by_kind", "by_harness", "examples", "notes", "skipped_lines"}


def test_scan_examples_show_the_evidence_worst_first(tmp_path, capsys):
    three_sessions(tmp_path / "home")
    _code, data = scan_json(capsys)
    assert [e["label"] for e in data["examples"]] == ["contradicted", "stale"]
    stale = data["examples"][1]
    assert stale["claim"] == "All 5 tests pass." and stale["kind"] == "tests"
    assert stale["run"]["command"] == "pytest -q" and stale["run"]["result"] == "pass"
    assert stale["changed"] == ["/work/app/src/app.py"] and stale["session"] == "s-stale"
    assert stale["harness"] == "claude-code" and stale["time"].endswith("UTC")


def test_scan_markdown_leads_with_the_bold_headline(tmp_path, capsys):
    three_sessions(tmp_path / "home")
    code, out = run_cli(capsys, "scan")
    assert code == 0
    assert out.startswith("**Found 3 claims that tests or builds passed in the last 30 days. 2 of those claims")
    assert "| stale | 1 |" in out and "Codex" in out and "pytest -q" in out


def test_scan_headline_when_there_is_nothing_to_check(tmp_path, capsys):
    code, data = scan_json(capsys)
    assert code == 0 and data["claims"] == 0
    assert data["headline"] == "No agent sessions from the last 30 days were found on this machine."
    CC().bash(*PASS).say("Done.").save(tmp_path / "home")
    _code, data = scan_json(capsys)
    assert data["headline"] == "No claims that tests or builds passed were found in 1 session from the last 30 days."


def test_scan_headline_when_every_claim_is_backed(tmp_path, capsys):
    CC().bash(*PASS).say("All 5 tests pass.").bash("npx tsc --noEmit", "", 0).say("tsc is clean.") \
        .save(tmp_path / "home")
    _code, data = scan_json(capsys)
    assert data["headline"] == ("Found 2 claims that tests or builds passed in the last 30 days. "
                                "Every one was backed by a passing run.")
    CC().bash("pytest -q | tail -1", "", 0).say("All tests pass.").save(tmp_path / "home", "b")
    _code, data = scan_json(capsys)
    assert data["headline"].endswith("None was contradicted, stale, or unsupported; 1 could not be checked.")


def test_scan_fail_flag_sets_the_exit_code(tmp_path, capsys):
    CC().bash(*PASS).say("All 5 tests pass.").save(tmp_path / "home")
    assert run_cli(capsys, "scan", "--fail")[0] == 0
    CC().edit("/work/app/src/app.py").say("All 5 tests pass.").save(tmp_path / "home", "b")
    assert run_cli(capsys, "scan", "--fail")[0] == 1


def test_scan_rejects_a_bad_window(capsys):
    with pytest.raises(SystemExit) as exc:
        C.main(["scan", "--since", "last-tuesday"])
    assert exc.value.code == 2


def test_scan_window_accepts_days_and_weeks(tmp_path, capsys):
    CC().bash(*PASS).say("All tests pass.", at=10).save(tmp_path / "home")
    _code, data = scan_json(capsys, "--since", "2w")
    assert data["window_days"] == 14 and data["claims"] == 1
    _code, data = scan_json(capsys, "--since", "7")
    assert data["window_days"] == 7


def test_scan_out_writes_the_report_to_a_file(tmp_path, capsys):
    three_sessions(tmp_path / "home")
    target = tmp_path / "report.md"
    code, out = run_cli(capsys, "scan", "--out", str(target))
    assert code == 0 and str(target) in out
    assert target.read_text(encoding="utf-8").startswith("**Found 3 claims")


def test_scan_counts_a_forked_copy_of_a_session_once(tmp_path, capsys):
    home = tmp_path / "home"
    first = CC().bash(*PASS).say("All 5 tests pass.")
    first.save(home, "s-first")
    first.say("Recap: all 5 tests pass.", at=50)
    first.save(home, "s-fork")
    _code, data = scan_json(capsys)
    assert data["claims"] == 2 and data["sessions"] == 2


def test_scan_uses_subagent_evidence_and_skips_subagent_claims(tmp_path, capsys):
    home = tmp_path / "home"
    CC().user("go").call("Agent", {"description": "impl", "prompt": "go"}, "done", at=1) \
        .say("All 12 tests pass.", at=30).save(home)
    CC(start=5, sidechain=True, agent_id="a1").user("go").bash(*PASS).say("All tests pass.") \
        .save_subagent(home, "a1")
    _code, data = scan_json(capsys)
    assert data["claims"] == 1 and data["by_label"]["backed"] == 1
    assert data["sessions"] == 1 and data["subagent_sessions"] == 1


def test_scan_filters_by_project_and_harness(tmp_path, capsys):
    home = tmp_path / "home"
    three_sessions(home)
    CC(cwd="/work/other").bash(*PASS).say("All tests pass.").save(home, "s-other")
    _code, data = scan_json(capsys, "--project", "/work/other")
    assert data["claims"] == 1 and data["sessions"] == 1
    _code, data = scan_json(capsys, "--harness", "codex")
    assert data["claims"] == 1 and list(data["harnesses"]) == ["codex"]


def test_scan_masks_secrets_and_flattens_untrusted_text(tmp_path, capsys):
    secret = "sk-" + "ant-" + "api03-" + "Ab3dEf6hIj9kLm2nOp5qRs8tUv1wXy4z"
    command = "pytest -q -k `x` | tail -1\necho '%s'" % secret
    CC().bash(command, "1 failed in 0.1s", 0).say("All tests pass for token %s | ok \ud800 done." % secret) \
        .save(tmp_path / "home")
    code, out = run_cli(capsys, "scan")
    assert code == 0 and secret not in out and "\ud800" not in out
    example = [line for line in out.splitlines() if "Last test run" in line][0]
    assert "`x`" not in example and "tail -1 echo" in example.replace("/ tail", "tail")
    _code, data = scan_json(capsys)
    assert secret not in json.dumps(data) and "\n" not in data["examples"][0]["run"]["command"]
    assert "|" not in data["examples"][0]["claim"]


def test_scan_notes_lines_it_could_not_read(tmp_path, capsys):
    path = CC().bash(*PASS).say("All tests pass.").save(tmp_path / "home")
    with open(path, "a", encoding="utf-8") as fh:
        fh.write("{not json\n")
    code, data = scan_json(capsys)
    assert code == 0 and data["skipped_lines"] == 1 and data["claims"] == 1
    assert any("could not be read" in n for n in data["notes"])


def test_every_script_prints_help_under_the_system_python():
    for script in ("claims.py", "stop_hook.py", "install.py"):
        done = subprocess.run([sys.executable, os.path.join(SCRIPTS, script), "--help"],
                              capture_output=True, text=True, timeout=30)
        assert done.returncode == 0 and "usage" in done.stdout.lower(), script


# ---------------------------------------------------------------------------
# claims.py diff: weakened-test signals
# ---------------------------------------------------------------------------

def git(repo, *args):
    done = subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@example.com", "-c", "commit.gpgsign=false",
                           "-c", "init.defaultBranch=main", "-C", str(repo)] + list(args),
                          capture_output=True, text=True, timeout=30)
    assert done.returncode == 0, done.stderr
    return done.stdout


def put(repo, rel, body):
    path = repo / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body, encoding="utf-8")


def make_repo(tmp_path, files):
    repo = tmp_path / "repo"
    repo.mkdir()
    git(repo, "init", "-q")
    for rel, body in files.items():
        put(repo, rel, body)
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", "base")
    return repo


PY_TESTS = ("import pytest\n\n\ndef test_a():\n    assert 1 == 1\n    assert 2 == 2\n\n\n"
            "def test_b():\n    assert 3 == 3\n")


def kinds_of(repo, base=None):
    return sorted(s["kind"] for s in W.find_signals(str(repo), base=base)["signals"])


def test_diff_flags_a_deleted_test_file_but_not_a_renamed_one(tmp_path):
    other = "def test_other():\n    assert 'other' == 'other'\n    assert 4 == 4\n"
    repo = make_repo(tmp_path, {"tests/test_a.py": PY_TESTS, "tests/test_b.py": other, "src/app.py": "x = 1\n"})
    (repo / "tests" / "test_a.py").unlink()
    git(repo, "mv", "tests/test_b.py", "tests/test_c.py")
    signals = W.find_signals(str(repo))["signals"]
    assert [(s["kind"], s["file"]) for s in signals] == [("deleted-test-file", "tests/test_a.py")]


def test_diff_flags_removed_assertions_and_removed_tests(tmp_path):
    repo = make_repo(tmp_path, {"tests/test_a.py": PY_TESTS})
    put(repo, "tests/test_a.py", "import pytest\n\n\ndef test_a():\n    assert 1 == 1\n")
    signals = {s["kind"]: s for s in W.find_signals(str(repo))["signals"]}
    assert set(signals) == {"removed-assertions", "removed-test"}
    assert signals["removed-assertions"]["detail"] == "2 assertions removed, 0 added"
    assert signals["removed-test"]["detail"] == "test_b"


def test_diff_accepts_a_replaced_assertion_and_a_moved_test(tmp_path):
    repo = make_repo(tmp_path, {"tests/test_a.py": PY_TESTS, "tests/test_z.py": "def test_z():\n    assert True\n"})
    put(repo, "tests/test_a.py", PY_TESTS.replace("assert 3 == 3", "assert 3 == 4 - 1").replace(
        "def test_b():\n    assert 3 == 4 - 1\n", ""))
    put(repo, "tests/test_z.py", "def test_z():\n    assert True\n\n\ndef test_b():\n    assert 3 == 3\n")
    assert kinds_of(repo) == []


def test_diff_ignores_assertions_outside_test_files(tmp_path):
    repo = make_repo(tmp_path, {"src/app.py": "def f(x):\n    assert x\n    return x\n"})
    put(repo, "src/app.py", "def f(x):\n    return x\n")
    assert kinds_of(repo) == []


@pytest.mark.parametrize("rel,before,after,kind", [
    ("tests/test_a.py", "def test_a():\n    assert 1\n", "@pytest.mark.skip\ndef test_a():\n    assert 1\n",
     "added-skip"),
    ("tests/test_a.py", "def test_a():\n    assert 1\n", "@pytest.mark.xfail\ndef test_a():\n    assert 1\n",
     "added-skip"),
    ("src/app.test.ts", "it('adds', () => { expect(1).toBe(1) })\n", "it.skip('adds', () => { expect(1).toBe(1) })\n",
     "added-skip"),
    ("src/app.spec.js", "it('adds', () => { expect(1).toBe(1) })\n", "xit('adds', () => { expect(1).toBe(1) })\n",
     "added-skip"),
    ("src/app.test.ts", "it('adds', () => { expect(1).toBe(1) })\n", "it.only('adds', () => { expect(1).toBe(1) })\n",
     "added-focus"),
    ("pkg/app_test.go", "func TestA(t *testing.T) {\n}\n", "func TestA(t *testing.T) {\n\tt.Skip(\"later\")\n}\n",
     "added-skip"),
    ("tests/it.rs", "#[test]\nfn adds() {}\n", "#[test]\n#[ignore]\nfn adds() {}\n", "added-skip"),
    ("src/test/java/AppTest.java", "@Test\nvoid adds() {}\n", "@Disabled\n@Test\nvoid adds() {}\n", "added-skip"),
])
def test_diff_flags_new_skip_and_focus_markers(tmp_path, rel, before, after, kind):
    repo = make_repo(tmp_path, {rel: before})
    put(repo, rel, after)
    assert kinds_of(repo) == [kind]


def test_diff_accepts_a_skip_marker_that_only_moved(tmp_path):
    before = "@pytest.mark.skip\ndef test_a():\n    assert 1\n\n\ndef test_b():\n    assert 2\n"
    after = "def test_a():\n    assert 1\n\n\n@pytest.mark.skip\ndef test_b():\n    assert 2\n"
    repo = make_repo(tmp_path, {"tests/test_a.py": before})
    put(repo, "tests/test_a.py", after)
    assert kinds_of(repo) == []


def test_diff_flags_test_commands_that_may_now_fail_silently(tmp_path):
    workflow = ("jobs:\n  test:\n    steps:\n      - run: pip install -e .\n      - run: pytest -q\n"
                "  upload:\n    steps:\n      - run: ./upload.sh\n")
    repo = make_repo(tmp_path, {".github/workflows/ci.yml": workflow, "package.json": '{"scripts": {"test": "jest"}}\n'})
    put(repo, ".github/workflows/ci.yml", workflow.replace("run: pytest -q", "run: pytest -q || true"))
    put(repo, "package.json", '{"scripts": {"test": "jest --passWithNoTests"}}\n')
    assert kinds_of(repo) == ["ignored-failure", "ignored-failure"]


def test_diff_flags_continue_on_error_only_near_a_test_step(tmp_path):
    workflow = ("jobs:\n  test:\n    steps:\n      - name: Tests\n        run: pytest -q\n"
                "  upload:\n    steps:\n      - name: Upload\n        run: ./upload.sh\n")
    repo = make_repo(tmp_path, {".github/workflows/ci.yml": workflow})
    put(repo, ".github/workflows/ci.yml", workflow.replace("        run: pytest -q\n",
                                                           "        run: pytest -q\n        continue-on-error: true\n"))
    assert kinds_of(repo) == ["ignored-failure"]
    git(repo, "checkout", "--", ".")
    put(repo, ".github/workflows/ci.yml", workflow.replace("        run: ./upload.sh\n",
                                                           "        run: ./upload.sh\n        continue-on-error: true\n"))
    assert kinds_of(repo) == []


@pytest.mark.parametrize("rel,before,after,flagged", [
    ("pyproject.toml", "[tool.coverage.report]\nfail_under = 90\n", "[tool.coverage.report]\nfail_under = 80\n", True),
    ("pyproject.toml", "[tool.coverage.report]\nfail_under = 90\n", "[tool.coverage.report]\nfail_under = 95\n", False),
    ("pyproject.toml", "[tool.coverage.report]\nfail_under = 90\nshow_missing = true\n",
     "[tool.coverage.report]\nshow_missing = true\n", True),
    ("Makefile", "test:\n\tpytest --cov=app --cov-fail-under=85\n", "test:\n\tpytest --cov=app --cov-fail-under=70\n",
     True),
    ("jest.config.js", "module.exports = {coverageThreshold: {global: {\n  lines: 90,\n}}}\n",
     "module.exports = {coverageThreshold: {global: {\n  lines: 60,\n}}}\n", True),
    ("config.yml", "retries:\n  lines: 90\n", "retries:\n  lines: 60\n", False),
])
def test_diff_flags_lowered_coverage_thresholds(tmp_path, rel, before, after, flagged):
    repo = make_repo(tmp_path, {rel: before})
    put(repo, rel, after)
    assert kinds_of(repo) == (["lowered-coverage"] if flagged else [])


def test_diff_does_not_count_markers_in_new_test_files(tmp_path):
    repo = make_repo(tmp_path, {"src/app.py": "x = 1\n"})
    put(repo, "tests/test_new.py", "import os, pytest\n\n\n@pytest.mark.skipif(os.name != 'posix', reason='x')\n"
                                   "def test_new():\n    assert 1\n")
    assert kinds_of(repo) == []
    assert W.find_signals(str(repo))["files_changed"] == 1


def test_diff_reads_new_untracked_files_for_moved_tests(tmp_path):
    repo = make_repo(tmp_path, {"tests/test_a.py": PY_TESTS})
    put(repo, "tests/test_a.py", PY_TESTS.split("def test_b")[0])
    put(repo, "tests/test_b.py", "def test_b():\n    assert 3 == 3\n")
    assert kinds_of(repo) == []


def test_diff_ignores_marker_text_inside_strings_and_other_languages(tmp_path):
    repo = make_repo(tmp_path, {"tests/test_a.py": PY_TESTS})
    put(repo, "tests/test_a.py", PY_TESTS + '\nCASE = "@pytest.mark.skip\\ndef test_x(): it.only(1)"\nskip = True\n')
    assert kinds_of(repo) == []


def test_diff_flags_a_condition_skip_added_to_an_existing_test(tmp_path):
    repo = make_repo(tmp_path, {"tests/test_a.py": PY_TESTS})
    put(repo, "tests/test_a.py", PY_TESTS.replace("def test_b():", "@pytest.mark.skipif(sys.platform == 'darwin', "
                                                                    "reason='flaky')\ndef test_b():"))
    assert kinds_of(repo) == ["added-skip"]


def test_diff_against_a_base_branch_starts_where_the_branch_began(tmp_path):
    repo = make_repo(tmp_path, {"tests/test_a.py": PY_TESTS})
    git(repo, "checkout", "-q", "-b", "feature")
    put(repo, "src/app.py", "x = 2\n")
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", "feature work")
    git(repo, "checkout", "-q", "main")
    put(repo, "tests/test_main_only.py", PY_TESTS)
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", "main moves on")
    git(repo, "checkout", "-q", "feature")
    assert kinds_of(repo, base="main") == []
    (repo / "tests" / "test_a.py").unlink()
    assert kinds_of(repo, base="main") == ["deleted-test-file"]


def test_diff_cli_headline_json_and_exit_codes(tmp_path, capsys):
    repo = make_repo(tmp_path, {"tests/test_a.py": PY_TESTS})
    code, out = run_cli(capsys, "diff", "--repo", str(repo))
    assert code == 0 and out.startswith("**The current diff shows no signs of weakened tests")
    put(repo, "tests/test_a.py", PY_TESTS.replace("def test_a():", "@pytest.mark.skip\ndef test_a():"))
    code, out = run_cli(capsys, "diff", "--repo", str(repo), "--json", "--fail")
    data = json.loads(out)
    assert code == 1 and data["counts"] == {"added-skip": 1}
    assert data["headline"] == "The current diff shows 1 sign of weakened tests: 1 new skip marker."
    assert set(data) >= {"headline", "repo", "base", "files_changed", "signals", "counts"}
    assert data["signals"][0] == {"kind": "added-skip", "file": "tests/test_a.py", "line": 4,
                                  "detail": "@pytest.mark.skip"}


def test_diff_outside_a_git_repository_is_an_input_error(tmp_path, capsys):
    (tmp_path / "plain").mkdir()
    code = C.main(["diff", "--repo", str(tmp_path / "plain")])
    assert code == 2 and "not a git repository" in capsys.readouterr().err


def test_diff_with_an_unknown_base_is_an_input_error(tmp_path, capsys):
    repo = make_repo(tmp_path, {"tests/test_a.py": PY_TESTS})
    code = C.main(["diff", "--repo", str(repo), "--base", "no-such-branch"])
    assert code == 2 and "no-such-branch" in capsys.readouterr().err


def test_diff_changes_nothing_in_the_repository(tmp_path):
    repo = make_repo(tmp_path, {"tests/test_a.py": PY_TESTS})
    put(repo, "tests/test_a.py", "def test_a():\n    assert 1\n")
    before = {p: (p.stat().st_mtime_ns, p.read_bytes()) for p in repo.rglob("*") if p.is_file()}
    W.find_signals(str(repo))
    after = {p: (p.stat().st_mtime_ns, p.read_bytes()) for p in repo.rglob("*") if p.is_file()}
    assert sorted(str(p.relative_to(repo)) for p in set(before) | set(after) if before.get(p) != after.get(p)) == []


# ---------------------------------------------------------------------------
# stop_hook.py
# ---------------------------------------------------------------------------

def hook_payload(path, active=False, **extra):
    payload = {"session_id": SID, "hook_event_name": "Stop", "permission_mode": "default",
               "stop_hook_active": active, "last_assistant_message": "All tests pass.",
               "transcript_path": str(path), "cwd": "/work/app"}
    payload.update(extra)
    return payload


def run_hook(monkeypatch, capsys, stdin_text, *argv):
    import io
    monkeypatch.setattr(sys, "stdin", io.StringIO(stdin_text))
    code = S.main(list(argv))
    out = capsys.readouterr().out
    return code, json.loads(out)


def test_stop_hook_blocks_after_a_failing_run(tmp_path):
    path = CC().bash(*FAIL).say("All tests pass.").save(tmp_path / "home")
    reason = S.decide(hook_payload(path))
    assert "the last test run failed" in reason and "Run the tests again before finishing" in reason


def test_stop_hook_blocks_when_code_changed_after_the_last_pass(tmp_path):
    path = CC().bash(*PASS).edit("/work/app/src/app.py").save(tmp_path / "home")
    reason = S.decide(hook_payload(path))
    assert "changed after the last passing test run" in reason and "/work/app/src/app.py" in reason


@pytest.mark.parametrize("path", ["/work/app/.github/tests/test_check.py", "/work/app/.cargo/config.toml"])
def test_stop_hook_blocks_when_a_hidden_folder_with_tests_or_build_settings_changed(tmp_path, path):
    reason = S.decide(hook_payload(CC().bash(*PASS).edit(path).save(tmp_path / "home")))
    assert "changed after the last passing test run" in reason and path in reason


@pytest.mark.parametrize("builder", [
    lambda: CC().bash(*PASS),
    lambda: CC().bash(*PASS).edit("/work/app/README.md"),
    lambda: CC().bash(*PASS).edit("/work/app/.git/info/exclude"),
    lambda: CC().bash(*PASS).edit("/work/app/.pytest_cache/v/cache/lastfailed"),
    lambda: CC().edit("/work/app/src/app.py"),
    lambda: CC().bash("pytest -q | tail -1", "", 0).edit("/work/app/src/app.py"),
])
def test_stop_hook_allows_a_clean_state_no_tests_or_an_unreadable_result(tmp_path, builder):
    path = builder().save(tmp_path / "home")
    assert S.decide(hook_payload(path)) is None


def test_stop_hook_never_blocks_twice_in_a_row(tmp_path):
    path = CC().bash(*FAIL).save(tmp_path / "home")
    assert S.decide(hook_payload(path, active=True)) is None


def test_stop_hook_counts_subagent_changes(tmp_path):
    home = tmp_path / "home"
    path = CC().bash(*PASS).call("Agent", {"description": "d", "prompt": "p"}, "Done.\nagentId: a1 (internal ID)",
                                 at=5).save(home)
    CC(start=6, sidechain=True, agent_id="a1").edit("/work/app/src/app.py").save_subagent(home, "a1")
    assert "changed after the last passing test run" in S.decide(hook_payload(path))


def test_stop_hook_reads_codex_rollouts(tmp_path):
    path = CX().run("pytest -q", "1 failed in 0.1s", 1).say("All tests pass.").save(tmp_path / "home")
    assert "the last test run failed" in S.decide(hook_payload(path), harness="codex")


def test_stop_hook_prints_one_json_object_and_exits_zero(tmp_path, monkeypatch, capsys):
    path = CC().bash(*FAIL).save(tmp_path / "home")
    code, out = run_hook(monkeypatch, capsys, json.dumps(hook_payload(path)))
    assert code == 0 and out["decision"] == "block" and "Run the tests again" in out["reason"]
    code, out = run_hook(monkeypatch, capsys, json.dumps(hook_payload(path, active=True)))
    assert (code, out) == (0, {})


@pytest.mark.parametrize("stdin_text", ["", "not json", "[1, 2]", json.dumps({"transcript_path": 7}),
                                        json.dumps({"transcript_path": "/no/such/file.jsonl"})])
def test_stop_hook_allows_on_any_bad_input(monkeypatch, capsys, stdin_text):
    assert run_hook(monkeypatch, capsys, stdin_text) == (0, {})


def test_stop_hook_reason_is_one_safe_line(tmp_path):
    odd = "/work/app/src/a`b|c\nd.py"
    path = CC().bash(*PASS).edit(odd).save(tmp_path / "home")
    reason = S.decide(hook_payload(path))
    assert "\n" not in reason and "`" not in reason and "|" not in reason


# ---------------------------------------------------------------------------
# install.py (only ever against settings files in tmp_path)
# ---------------------------------------------------------------------------

def install(capsys, settings, *argv):
    code = I.main(["--settings", str(settings)] + list(argv))
    return code, capsys.readouterr()


def our_hooks(data):
    return [h for group in data.get("hooks", {}).get("Stop", []) for h in group.get("hooks", [])
            if "stop_hook.py" in h.get("command", "")]


def test_install_dry_run_shows_the_change_and_writes_nothing(tmp_path, capsys):
    settings = tmp_path / "settings.json"
    code, out = install(capsys, settings)
    assert code == 0 and not settings.exists()
    assert "+++" in out.out and "stop_hook.py" in out.out and "--write" in out.out


def test_install_write_adds_a_stop_hook_that_runs(tmp_path, capsys):
    settings = tmp_path / "settings.json"
    code, _out = install(capsys, settings, "--write")
    data = json.loads(settings.read_text())
    [hook] = our_hooks(data)
    assert code == 0 and hook["type"] == "command" and hook["timeout"] == 60
    import shlex
    argv = shlex.split(hook["command"])
    assert argv[0] == "python3" and os.path.isabs(argv[1]) and os.path.isfile(argv[1])
    assert hook["command"].endswith(" || true")
    transcript = CC().bash(*FAIL).save(tmp_path / "home")
    done = subprocess.run(hook["command"], shell=True, input=json.dumps(hook_payload(transcript)),
                          capture_output=True, text=True, timeout=60)
    assert done.returncode == 0 and json.loads(done.stdout)["decision"] == "block"


def test_install_merges_with_existing_settings_and_is_idempotent(tmp_path, capsys):
    settings = tmp_path / "settings.json"
    existing = {"model": "opus", "hooks": {
        "Stop": [{"hooks": [{"type": "command", "command": "say done"}]}],
        "PreToolUse": [{"matcher": "Bash", "hooks": [{"type": "command", "command": "guard.sh"}]}]}}
    settings.write_text(json.dumps(existing, indent=4))
    install(capsys, settings, "--write")
    code, out = install(capsys, settings, "--write")
    data = json.loads(settings.read_text())
    assert code == 0 and "already" in out.out
    assert data["model"] == "opus" and data["hooks"]["PreToolUse"] == existing["hooks"]["PreToolUse"]
    assert data["hooks"]["Stop"][0] == existing["hooks"]["Stop"][0] and len(our_hooks(data)) == 1
    assert settings.read_text().startswith('{\n    "model"')


def test_uninstall_removes_only_its_own_entry(tmp_path, capsys):
    settings = tmp_path / "settings.json"
    settings.write_text(json.dumps({"hooks": {"Stop": [{"hooks": [{"type": "command", "command": "say done"}]}]}}))
    install(capsys, settings, "--write")
    code, _out = install(capsys, settings, "--uninstall")
    assert code == 0 and len(our_hooks(json.loads(settings.read_text()))) == 1
    install(capsys, settings, "--uninstall", "--write")
    data = json.loads(settings.read_text())
    assert our_hooks(data) == [] and data["hooks"]["Stop"] == [{"hooks": [{"type": "command", "command": "say done"}]}]
    only_ours = tmp_path / "ours.json"
    install(capsys, only_ours, "--write")
    install(capsys, only_ours, "--uninstall", "--write")
    assert json.loads(only_ours.read_text()) == {}


def test_install_refuses_a_settings_file_it_cannot_parse(tmp_path, capsys):
    settings = tmp_path / "settings.json"
    settings.write_text("{ broken")
    code, out = install(capsys, settings, "--write")
    assert code == 2 and settings.read_text() == "{ broken" and "settings" in out.err


def test_install_for_codex_passes_the_harness_and_names_the_trust_step(tmp_path, capsys):
    settings = tmp_path / "hooks.json"
    code, out = install(capsys, settings, "--harness", "codex", "--write")
    [hook] = our_hooks(json.loads(settings.read_text()))
    assert code == 0 and "--harness codex" in hook["command"] and "/hooks" in out.out
    assert hook["command"].endswith(" || echo '{}'")  # Codex wants JSON on stdout at exit 0


def test_install_picks_the_settings_file_for_each_scope(tmp_path, monkeypatch, capsys):
    project = tmp_path / "proj"
    project.mkdir()
    home = tmp_path / "home"
    cases = [("claude-code", "user", home / ".claude" / "settings.json"),
             ("claude-code", "project", project / ".claude" / "settings.json"),
             ("claude-code", "local", project / ".claude" / "settings.local.json"),
             ("codex", "user", home / ".codex" / "hooks.json"),
             ("codex", "project", project / ".codex" / "hooks.json")]
    for harness, scope, want in cases:
        assert I.settings_path(harness, scope, str(project)) == str(want)
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "cc"))
    assert I.settings_path("claude-code", "user", str(project)) == str(tmp_path / "cc" / "settings.json")
    code = I.main(["--harness", "codex", "--scope", "local"])
    assert code == 2


# ---------------------------------------------------------------------------
# Runs that finish later, and runs inside Codex code-mode scripts
# ---------------------------------------------------------------------------

def js_exec(cmd, printer="text(r.output);", style="json"):
    arg = json.dumps(cmd) if style == "json" else ("`%s`" % cmd if style == "template" else "'%s'" % cmd)
    key = '"cmd"' if style == "json" else "cmd"
    return 'const r = await tools.exec_command({%s:%s,"workdir":"/work/app","yield_time_ms":30000}); %s' % (
        key, arg, printer)


DONE = "Script completed\nWall time 1.0 seconds\nOutput:\n"


def cx_load(tmp_path, builder, name="rollout-x.jsonl"):
    return T.load_session("codex", builder.save(tmp_path / "home", name))


@pytest.mark.parametrize("style", ["json", "template", "single"])
def test_codex_script_commands_are_read_from_the_javascript(style):
    js = js_exec("python3 -m pytest -q", style=style) + " " + js_exec("git status")
    assert E.codex_script_commands(js) == ["python3 -m pytest -q", "git status"]
    assert E.codex_script_commands('const r = await tools.exec_command({"cmd":"echo \\"hi\\"\\npytest"});') \
        == ['echo "hi"\npytest']


def test_codex_script_runs_are_labeled(tmp_path):
    s = cx_load(tmp_path, CX().script(js_exec("python3 -m pytest -q"), DONE + "5 passed in 0.1s").say("All tests pass."))
    assert labels(s) == ["backed"]
    s = cx_load(tmp_path, CX().script(js_exec("python3 -m pytest -q"), DONE + "1 failed in 0.1s").say("All tests pass."),
                name="b.jsonl")
    assert labels(s) == ["contradicted"]


def test_codex_script_patches_count_as_changes(tmp_path):
    patch = "text(await tools.apply_patch(String.raw`*** Begin Patch\n*** Update File: /work/app/src/a.py\n@@\n-a\n+b\n" \
            "*** End Patch`));"
    s = cx_load(tmp_path, CX().script(js_exec("pytest -q"), DONE + "5 passed in 0.1s").script(patch, DONE + "Success.")
                .say("All tests pass."))
    [claim] = E.label_claims(s)
    assert claim["label"] == "stale" and claim["changed"] == ["/work/app/src/a.py"]


def test_codex_script_printed_exit_code_is_read(tmp_path):
    js = js_exec("npx tsc --noEmit", printer="text(JSON.stringify({exit_code:r.exit_code, output:r.output}));")
    s = cx_load(tmp_path, CX().script(js, DONE + '{"exit_code":0,"output":""}').say("tsc is clean."))
    assert labels(s) == ["backed"]
    s = cx_load(tmp_path, CX().script(js, DONE + '{"exit_code":2,"output":"x"}').say("tsc is clean."), name="b.jsonl")
    assert labels(s) == ["contradicted"]


def test_codex_script_that_finishes_in_a_later_wait(tmp_path):
    builder = CX().script(js_exec("pytest -q"), "Script running with cell ID 4\nWall time 1.0 seconds\nOutput:\n")
    builder.say("All tests pass.")
    builder.wait("4", DONE + "5 passed in 0.1s").say("All tests pass.")
    assert labels(cx_load(tmp_path, builder)) == ["unclear", "backed"]


def test_codex_script_with_a_syntax_error_ran_nothing(tmp_path):
    s = cx_load(tmp_path, CX().run("git status").script(js_exec("pytest -q"),
                                                        "Script failed\nWall time 0.0 seconds\nOutput:\n"
                                                        "Script error: SyntaxError: missing ) after argument list")
                .say("All tests pass."))
    assert labels(s) == ["unsupported"]


def test_a_transcript_with_no_tool_calls_cannot_be_checked(tmp_path):
    s = cx_load(tmp_path, CX().user("status?").say("All 42 tests pass."))
    [claim] = E.label_claims(s)
    assert claim["label"] == "unclear" and claim["run"] is None and claim["why"] == "no tool calls recorded"


def test_a_background_run_finished_by_task_output_counts(tmp_path):
    builder = CC().bash("pytest -q", background=True).say("All tests pass.")
    builder.call("TaskOutput", {"task_id": "b1", "block": True, "timeout": 60000},
                 "<retrieval_status>success</retrieval_status>\n\n<task_id>b1</task_id>\n\n<task_type>local_bash"
                 "</task_type>\n\n<status>completed</status>\n\n<exit_code>0</exit_code>\n\n<output>\n5 passed in 0.1s\n"
                 "</output>")
    builder.say("All tests pass.")
    assert labels(cc_load(tmp_path, builder)) == ["unclear", "backed"]


def test_scan_leaves_out_old_claims_in_a_recently_modified_file(tmp_path, capsys):
    old_base = int(time.time()) - 40 * 86400
    builder = CC()
    builder.records.append(cc_user("old work", stamp(1, old_base)))
    builder.records.append(cc_assistant("msg_old", text("All 9 tests pass."), stamp(2, old_base)))
    builder.bash(*PASS).say("All 5 tests pass.")
    builder.save(tmp_path / "home")
    _code, data = scan_json(capsys)
    assert data["claims"] == 1 and data["by_label"]["backed"] == 1


def exec_json(**fields):
    base = {"chunk_id": "ab12", "wall_time_seconds": 1.5, "original_token_count": 10}
    base.update(fields)
    return DONE + json.dumps(base)


@pytest.mark.parametrize("key", ["session_id", '"session_id"'])
def test_codex_process_that_finishes_in_a_later_poll(tmp_path, key):
    start = js_exec(".venv/bin/python -m unittest discover -s tests -q", printer="text(JSON.stringify(r));")
    poll = "const r = await tools.write_stdin({%s:97522, chars:\"\", yield_time_ms:30000}); " \
           "text(JSON.stringify(r));" % key
    builder = CX().script(start, exec_json(session_id=97522, output="..."))
    builder.say("All tests pass.")
    builder.script(poll, exec_json(exit_code=0, output="......\nRan 6 tests in 0.2s\n\nOK\n"))
    builder.say("All tests pass.")
    assert labels(cx_load(tmp_path, builder)) == ["unclear", "backed"]


def test_codex_poll_that_arrives_through_a_cell_wait(tmp_path):
    start = js_exec("python3 -m pytest -q", printer="text(JSON.stringify(r));")
    poll = "const r = await tools.write_stdin({session_id:80064, chars:\"\"}); text(JSON.stringify(r));"
    builder = CX().script(start, exec_json(session_id=80064, output=""))
    builder.script(poll, "Script running with cell ID 7\nWall time 1.0 seconds\nOutput:\n")
    builder.wait("7", exec_json(exit_code=1, output="1 failed, 4 passed in 2.1s"))
    builder.say("All tests pass.")
    assert labels(cx_load(tmp_path, builder)) == ["contradicted"]


def test_codex_json_result_exit_code_decides(tmp_path):
    start = js_exec("python3 -m pytest -q", printer="text(JSON.stringify(r));")
    s = cx_load(tmp_path, CX().script(start, exec_json(exit_code=0, output="5 passed in 0.1s")).say("All tests pass."))
    assert labels(s) == ["backed"]


def test_exit_code_echoed_after_the_run_is_read():
    assert E.run_outcome('npm run typecheck > /dev/null 2>&1; echo "typecheck exit=$?"', "typecheck exit=0", 0,
                         "build") == ("pass", "exit code 0")
    assert E.run_outcome("pytest -q | tail -1; echo EXIT=$?", "1 failed in 0.1s\nEXIT=0", 0, "tests")[0] == "fail"
    assert E.run_outcome("pytest -q > log.txt 2>&1; echo EXIT=$?", "EXIT=1", 0, "tests") == ("fail", "exit code 1")


def test_a_nextjs_route_table_means_the_build_finished():
    out = "Route (app)\n┌ ○ /\n└ ○ /about\n\n○  (Static)  prerendered as static content\n"
    assert E.run_outcome("npm run build 2>&1 | tail -6", out, 0, "build")[0] == "pass"


# ---------------------------------------------------------------------------
# Precision guards found by checking labels against real sessions
# ---------------------------------------------------------------------------

def test_claim_scope_tells_one_named_test_from_the_suite():
    assert E.find_claims("The functional e2e test passes.")[0]["scope"] == "one"
    assert E.find_claims("DB migration test passes.")[0]["scope"] == "one"
    for text_ in ("All 5 tests pass.", "33/33 tests green.", "200 passed.", "The test suite passes."):
        assert E.find_claims(text_)[0]["scope"] == "all", text_


def test_a_claim_about_one_named_test_is_not_judged_by_a_run_with_other_failures(tmp_path):
    s = cc_load(tmp_path, CC().bash(*FAIL).say("The new parser test passes."))
    assert labels(s) == ["unclear"]
    s = cc_load(tmp_path, CC().say("The new parser test passes."), name="b")
    assert labels(s) == ["unclear"]
    s = cc_load(tmp_path, CC().bash(*PASS).say("The new parser test passes."), name="c")
    assert labels(s) == ["backed"]
    s = cc_load(tmp_path, CC().bash(*FAIL).say("All tests pass."), name="d")
    assert labels(s) == ["contradicted"]


def test_a_command_that_prints_a_runner_summary_counts_as_a_run(tmp_path):
    s = cc_load(tmp_path, CC().bash(*FAIL).bash("swift run DevTests 2>&1 | tail -3", "33/33 tests passed", 0)
                .say("33/33 tests green."))
    assert labels(s) == ["backed"]
    s = cc_load(tmp_path, CC().bash("cat old-results.txt", "5 passed in 0.1s", 0).say("All 5 tests pass."), name="b")
    assert labels(s) == ["unsupported"]


def test_an_unreadable_test_like_command_after_the_last_run_makes_the_claim_unclear(tmp_path):
    s = cc_load(tmp_path, CC().bash(*FAIL).bash("swift run DevTests", "all good", 0).say("All tests pass."))
    assert labels(s) == ["unclear"]
    s = cc_load(tmp_path, CC().bash("node scripts/verify_workbook.mjs", "PASS reopened workbook", 0)
                .say("Synthetic calibration tests passed."), name="b")
    assert labels(s) == ["unclear"]
    s = cc_load(tmp_path, CC().bash("git diff tests/", "", 0).bash("ls tests", "", 0).say("All tests pass."), name="c")
    assert labels(s) == ["unsupported"]


def test_subagents_still_working_at_claim_time_are_not_evidence(tmp_path):
    home = tmp_path / "home"
    main = CC().user("go").call("Agent", {"description": "d", "prompt": "p", "run_in_background": True},
                                "Async agent launched", at=1)
    main.bash(*PASS, at=10).say("All tests pass.", at=13)
    busy = CC(start=2, sidechain=True, agent_id="a1").user("go")
    busy.bash(*FAIL, at=11).edit("/work/app/src/app.py", at=12).bash(*PASS, at=40)
    s = T.load_session("claude-code", main.save(home))
    child = T.load_session("claude-code", busy.save_subagent(home, "a1"))
    assert labels(s, subagents=[child]) == ["backed"]


def test_a_typecheck_script_that_prints_nothing_passed():
    out = "\n> app@0.1.0 typecheck\n> tsc --noEmit\n\n"
    assert E.run_outcome("npm run typecheck 2>&1 | tail -8", out, 0, "build")[0] == "pass"
    assert E.run_outcome("npm run typecheck 2>&1 | tail -8", out + "src/a.ts(1,1): error TS2304: x\n", 0,
                         "build")[0] == "fail"
    assert E.run_outcome("npm run build 2>&1 | tail -8", "\n> app@0.1.0 build\n> next build\n\n", 0,
                         "build")[0] == "unknown"


def test_changed_paths_follow_cd_and_simple_variables():
    command = "S=/tmp/scratch; cd $S/pw && cat > funnel.mjs <<'EOF'\nx\nEOF"
    assert [v for k, v, _i in E.shell_steps(command, cwd="/work/app") if k == "edit"] == [["/tmp/scratch/pw/funnel.mjs"]]
    assert [v for k, v, _i in E.shell_steps("cd sub && echo x > a.py", cwd="/work/app") if k == "edit"] == \
        [["/work/app/sub/a.py"]]
    assert [v for k, v, _i in E.shell_steps("cd ~/proj && sed -i '' s/a/b/ x.py", cwd="/work/app",
                                            home="/Users/sam") if k == "edit"] == [["/Users/sam/proj/x.py"]]


def test_scratch_files_written_after_cd_to_a_temporary_folder_do_not_make_a_claim_stale(tmp_path):
    command = "S=/private/tmp/scratch; cd $S/pw && cat > funnel-png.mjs <<'EOF'\nconsole.log(1)\nEOF"
    s = cc_load(tmp_path, CC().bash(*PASS).bash(command, "", 0).say("All tests pass."))
    assert labels(s) == ["backed"]


def test_changes_in_another_repository_do_not_make_a_claim_stale(tmp_path):
    repo_a, repo_b = tmp_path / "repo-a", tmp_path / "repo-b"
    for repo in (repo_a, repo_b):
        (repo / ".git").mkdir(parents=True)
    run = ("cd '%s' && pytest -q" % repo_a, "5 passed in 0.1s", 0)
    work = str(tmp_path)  # the session works in the folder that holds both repositories
    s = cc_load(tmp_path, CC(cwd=work).bash(*run).edit(str(repo_b / "config.yaml")).say("All tests pass."))
    assert labels(s) == ["backed"]
    s = cc_load(tmp_path, CC(cwd=work).bash(*run).edit(str(repo_a / "src" / "app.py")).say("All tests pass."),
                name="b")
    assert labels(s) == ["stale"]


def test_printed_summaries_count_only_at_the_end_of_a_program_s_output(tmp_path):
    s = cc_load(tmp_path, CC().bash("wc -l x.md && sed -n 1,5p x.md && command -v gbrain",
                                    "12 x.md\n8/8 tests passing\n/usr/local/bin/gbrain", 0).say("All tests pass."))
    assert labels(s) == ["unsupported"]
    brief = "Step 1: run pytest\nExpected: FAIL because render is missing\nThen: 5 passed in 0.1s\n" + \
            "\n".join("line %d of the brief" % i for i in range(30)) + "\nwrote brief: 69 lines"
    s = cc_load(tmp_path, CC().bash("bash scripts/task-brief plan.md 8", brief, 0).say("All tests pass."), name="b")
    assert labels(s) == ["unsupported"]


def test_a_tox_like_line_in_other_output_is_not_a_result():
    assert E.run_outcome("tox | tail", "Expected: FAIL because x", 0, "tests")[0] == "unknown"
    assert E.run_outcome("tox | tail", "Status: OK", 0, "tests")[0] == "unknown"


def test_codex_script_that_passes_commands_by_variable_is_unclear(tmp_path):
    js = ('const cmds=[["storefront","npm run typecheck"],["kit","bash -n install.sh"]]; '
          'for (const [n, cmd] of cmds) { const r = await tools.exec_command({cmd, workdir: n}); text(r.output) }')
    s = cx_load(tmp_path, CX().script(js, DONE + "storefront exit=0\nkit exit=0").say("Storefront typecheck passes."))
    assert labels(s) == ["unclear"]


def test_without_a_repository_the_run_folder_scopes_the_changes(tmp_path):
    book, voice = tmp_path / "book", tmp_path / "voice"
    book.mkdir()
    voice.mkdir()
    run = ("cd '%s' && npx vitest run" % book, " Test Files  2 passed (2)\n      Tests  27 passed (27)", 0)
    work = str(tmp_path)
    s = cc_load(tmp_path, CC(cwd=work).bash(*run).edit(str(voice / "agents" / "openai.yaml")).say("All tests pass."))
    assert labels(s) == ["backed"]
    s = cc_load(tmp_path, CC(cwd=work).bash(*run).edit(str(book / "src" / "a.ts")).say("All tests pass."), name="b")
    assert labels(s) == ["stale"]


def test_a_run_folder_named_by_cd_scopes_changes_even_after_it_is_gone(tmp_path):
    gone = "/nonexistent/projects/book"
    run = ("cd '%s' && npx vitest run" % gone, "      Tests  27 passed (27)", 0)
    s = cc_load(tmp_path, CC(cwd="/nonexistent").bash(*run).edit("/nonexistent/voice/agents/openai.yaml")
                .say("All tests pass."))
    assert labels(s) == ["backed"]
    s = cc_load(tmp_path, CC(cwd="/nonexistent/projects/book").bash("npx vitest run", "      Tests  27 passed (27)", 0)
                .edit("/nonexistent/voice/agents/openai.yaml").say("All tests pass."), name="b")
    assert labels(s) == ["stale"]


def undated_codex_subagent(home, start, *runs):
    """A Codex subagent page whose lines all carry one time, as paginated pages do."""
    child = CX(start=start)
    child.records[0] = cx_meta(stamp(start), tid="child-1", parent_thread_id=TID,
                               source={"subagent": {"thread_spawn": {"parent_thread_id": TID, "depth": 1}}})
    for run in runs:
        child.run(*run)
    for rec in child.records:
        rec["timestamp"] = stamp(start)
    return T.load_session("codex", child.save(home, name="rollout-child.jsonl"))


def test_claims_after_an_undated_subagent_page_are_unclear(tmp_path):
    home = tmp_path / "home"
    child = undated_codex_subagent(home, 20, ("pytest -q", "1 failed in 0.1s", 1), ("pytest -q", "5 passed in 0.1s", 0),
                                   ("git status", "", 0), ("git status", "", 0), ("git log -1", "", 0))
    main = T.load_session("codex", CX().user("go").run("pytest -q", "5 passed in 0.1s").say("All tests pass.")
                          .save(home))
    early = E.label_claims(main, subagents=[child])
    assert [c["label"] for c in early] == ["backed"]
    builder = CX().user("go").run("pytest -q", "5 passed in 0.1s").say("All tests pass.").user("and now?")
    builder.t = 39  # the second claim comes after the subagent page began (at 20)
    main = T.load_session("codex", builder.say("All tests pass.").save(home, name="rollout-b.jsonl"))
    late = E.label_claims(main, subagents=[child])
    assert [c["label"] for c in late] == ["backed", "unclear"]
    assert late[1]["why"] == "a subagent's transcript has no event times"


def test_a_claim_that_may_relay_a_subagent_still_working_is_unclear(tmp_path):
    home = tmp_path / "home"
    main = CC().user("go").call("Agent", {"description": "d", "prompt": "p", "run_in_background": True},
                                "Async agent launched", at=1)
    main.say("All tests pass.", at=13)
    busy = CC(start=2, sidechain=True, agent_id="a1").user("go").bash(*PASS, at=10).edit("/work/app/x.py", at=40)
    s = T.load_session("claude-code", main.save(home))
    child = T.load_session("claude-code", busy.save_subagent(home, "a1"))
    [claim] = E.label_claims(s, subagents=[child])
    assert claim["label"] == "unclear" and claim["why"] == "the claim may relay a subagent that was still working"


@pytest.mark.parametrize("harness,want", [("claude-code", ""), ("codex", "{}")])
def test_an_installed_hook_whose_script_is_gone_still_lets_the_agent_finish(tmp_path, capsys, harness, want):
    import shutil
    copy = tmp_path / "skill-copy" / "claim-check" / "scripts"
    shutil.copytree(SCRIPTS, str(copy), ignore=shutil.ignore_patterns("__pycache__"))
    settings = tmp_path / "settings.json"
    done = subprocess.run([sys.executable, str(copy / "install.py"), "--harness", harness, "--settings", str(settings),
                           "--write"], capture_output=True, text=True, timeout=60)
    assert done.returncode == 0, done.stderr
    [hook] = our_hooks(json.loads(settings.read_text()))
    assert str(copy / "stop_hook.py") in hook["command"]
    (copy / "stop_hook.py").unlink()
    ran = subprocess.run(hook["command"], shell=True, input="{}", capture_output=True, text=True, timeout=60)
    assert ran.returncode == 0 and ran.stdout.strip() == want


def test_install_dry_run_masks_secrets_in_the_settings_file(tmp_path, capsys):
    secret = "sk-" + "ant-" + "api03-" + "Zz9Yy8Xx7Ww6Vv5Uu4Tt3Ss2Rr1Qq0Pp"
    settings = tmp_path / "settings.json"
    settings.write_text(json.dumps({"env": {"ANTHROPIC_API_KEY": secret}, "note": "use `rm` carefully"}, indent=2))
    code, out = install(capsys, settings)
    assert code == 0 and secret not in out.out and "`" not in out.out


def edits_of(command, cwd="/w", home="/Users/sam"):
    return [value for kind, value, _i in E.shell_steps(command, cwd=cwd, home=home) if kind == "edit"]


def test_a_commit_message_in_a_heredoc_is_not_a_command():
    command = ('git add a.py && git commit -m "$(cat <<\'EOF\'\nFix the (odd) parser > now\nrm -rf stuff\n'
               'Run pytest -q "later"\nEOF\n)"')
    assert E.shell_steps(command) == []


def test_unknown_changes_happen_in_the_working_folder_and_git_dash_c_names_it():
    assert edits_of("git -C /repo/main merge --ff-only feat", cwd="/work/wt") == [["/repo/main"]]
    assert edits_of("black .", cwd="/work/app") == [["/work/app"]]
    assert E.shell_steps("black .") == [("edit", None, 0)]


def test_inline_scripts_name_their_files_by_argument_or_variable():
    heredoc = ('M=/Users/sam/notes/MEMORY.md; python3 - "$M" <<\'PY\'\nimport sys, pathlib\n'
               'p = pathlib.Path(sys.argv[1]); p.write_text("x")\nPY')
    assert edits_of(heredoc) == [["/Users/sam/notes/MEMORY.md"]]
    inline = 'M=/Users/sam/n.md; python3 -c "import pathlib; pathlib.Path(\'$M\').write_text(\'x\')"'
    assert edits_of(inline) == [["/Users/sam/n.md"]]
    noisy = ("python3 - <<'EOF'\nfrom pathlib import Path\np = Path(\"scripts/build.py\"); s = p.read_text()\n"
             "p.write_text(s.replace('](../', '](../../templates/'))\nEOF")
    assert edits_of(noisy) == [["/w/scripts/build.py"]]


def test_copying_into_a_folder_changes_the_file_inside_it():
    assert edits_of("cp /tmp/fake.txt . && mv a.py lib/") == [["/w/fake.txt"], ["/w/a.py", "/w/lib/a.py"]]


def test_stop_hook_does_not_block_when_a_later_unreadable_test_command_ran(tmp_path):
    path = CC().bash(*FAIL).bash("swift run DevTests", "all good", 0).save(tmp_path / "home")
    assert S.decide(hook_payload(path)) is None


# ---------------------------------------------------------------------------
# Fixes from the fresh-eyes review (2026-10)
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("text", [
    "0 tests passed; the import is broken.",
    "Zero tests pass.",
    "Only 3 tests pass so far.",
    "Most tests pass; 2 still fail.",
    "The unit tests pass but the integration tests fail.",
    "Just 2 tests pass.",
    "Nearly all tests pass.",
    "0 passed.",
    "The suite passes sometimes.",
    "The flaky test passes on retry.",
    "All 12 tests pass after the broken import was removed.",
])
def test_accurate_reports_of_failure_are_not_claims(text):
    assert E.find_claims(text) == []


@pytest.mark.parametrize("text", [
    "Done when: all tests pass.",
    "Definition of done: the build succeeds and all tests pass.",
    "Goal: all tests pass.",
    "Target: 100% of tests passing.",
    "Expected result: all tests pass.",
    "TODO: tests pass on Windows",
    "Acceptance: all tests pass.",
    "Next step: all tests pass on the new runner.",
    "Plan: the build passes, then we ship.",
    "Success criteria:\n- All tests pass\n- The build is clean",
    "**Next steps:**\n\n1. All tests pass\n   on every platform\n2. tsc clean",
])
def test_plans_and_criteria_are_not_claims(text):
    assert E.find_claims(text) == []


def test_a_list_under_a_results_header_still_holds_claims():
    assert [c["kind"] for c in E.find_claims("Success criteria:\n- tests pass\n\nResults:\n- All 9 tests pass")] == \
        ["tests"]


@pytest.mark.parametrize("text", [
    "Tests pass upstream.",
    "Their tests pass.",
    "Your test suite passes.",
    "All tests pass on main.",
    "All tests pass in the original branch.",
    "All tests passed yesterday.",
    "Tests passed last week.",
    "The suite passed two days ago.",
    "The issue reported that all tests pass.",
    "pytest reports 42 passed.",
    "The reviewer mentioned all tests pass.",
    "He told me all tests pass.",
    "I wrote it so all tests pass.",
    "Hypothetically, all tests pass.",
    "In theory all tests pass.",
])
def test_other_people_s_words_and_old_results_are_not_claims(text):
    assert E.find_claims(text) == []


@pytest.mark.parametrize("command,kinds", [
    ("npm run type-check", {"build"}),
    ("npm run check:types", {"build"}),
    ("pnpm types:check", {"build"}),
    ("npm run test:types", {"build"}),
    ("npm run lint:check", set()),
    ("npm run format:check", set()),
    ("npm run spell:check", set()),
    ("npm run check-links", set()),
    ("yarn check", set()),
    ("npm run ci", set()),
    ("pnpm run verify", set()),
    ("npx turbo run check", set()),
    ("make check", {"tests"}),
    ("make ci", {"tests"}),
    ("just check", {"tests"}),
    ("task verify", {"tests"}),
    ("npm run test:e2e", {"tests"}),
])
def test_package_scripts_are_read_by_the_words_in_their_names(command, kinds):
    assert E.command_kinds(command) == frozenset(kinds)


def test_a_passing_typecheck_script_does_not_back_a_test_claim(tmp_path):
    s = cc_load(tmp_path, CC().bash(*FAIL).bash("npm run type-check", "", 0).say("All tests pass."))
    assert labels(s) == ["contradicted"]


def test_a_script_named_check_or_ci_may_run_tests_so_the_claim_is_unclear(tmp_path):
    s = cc_load(tmp_path, CC().bash(*FAIL).bash("npm run check", "", 0).say("All tests pass."))
    assert labels(s) == ["unclear"]


@pytest.mark.parametrize("command", [
    "ruff check .",
    "black --check .",
    "isort --check-only .",
    "flake8 tests/",
    "pylint tests/test_app.py",
    "npx eslint tests/",
    "npx prettier --check src",
    "npx biome check .",
    "cargo clippy --tests",
    "go vet ./...",
    "shellcheck scripts/test.sh",
    "terraform validate",
    "pre-commit run --all-files",
    "osascript -e 'display notification \"tests done\"'",
    "./notify.sh 'tests done'",
])
def test_linters_and_messages_do_not_hide_a_failed_run(tmp_path, command):
    s = cc_load(tmp_path, CC().bash(*FAIL).bash(command, "", 0).say("All tests pass."))
    assert labels(s) == ["contradicted"]


def test_a_later_command_that_names_a_test_runner_keeps_a_failed_run_unclear(tmp_path):
    s = cc_load(tmp_path, CC().bash(*FAIL).bash("docker compose exec web pytest -q", "ok", 0).say("All tests pass."))
    assert labels(s) == ["unclear"]
    s = cc_load(tmp_path, CC().bash(*FAIL).bash("node scripts/verify_workbook.mjs", "PASS", 0).say("All tests pass."),
                name="b")
    assert labels(s) == ["contradicted"]


def test_stop_hook_blocks_a_failed_run_followed_by_a_linter(tmp_path):
    path = CC().bash(*FAIL).bash("ruff check .", "All checks passed!", 0).save(tmp_path / "home")
    assert "the last test run failed" in S.decide(hook_payload(path))


NOTE = ("<task-notification>\n<task-id>%s</task-id>\n<tool-use-id>toolu_1</tool-use-id>\n<status>completed</status>\n"
        "<summary>Agent finished</summary>\n</task-notification>")
LAUNCHED = "Async agent launched successfully.\nagentId: %s (internal ID)"


def test_a_subagent_counts_once_its_result_reached_the_parent(tmp_path):
    home = tmp_path / "home"
    main = CC().user("go").call("Agent", {"description": "d", "prompt": "p", "run_in_background": True},
                                LAUNCHED % "a1", at=1)
    main.say("All tests pass.", at=10).user(NOTE % "a1", at=20).say("All tests pass.", at=25)
    sub = CC(start=3, sidechain=True, agent_id="a1").user("go").bash(*PASS, at=4)
    s = T.load_session("claude-code", main.save(home))
    child = T.load_session("claude-code", sub.save_subagent(home, "a1"))
    claims = E.label_claims(s, subagents=[child])
    assert [c["label"] for c in claims] == ["unclear", "backed"]
    assert claims[0]["why"] == "the claim may relay a subagent that was still working"


def test_a_foreground_subagent_counts_from_its_result(tmp_path):
    home = tmp_path / "home"
    main = CC().user("go").call("Agent", {"description": "d", "prompt": "p"}, "Done.\nagentId: a2 (internal ID)", at=1)
    main.say("All tests pass.", at=30)
    sub = CC(start=3, sidechain=True, agent_id="a2").user("go").bash(*PASS, at=4)
    s = T.load_session("claude-code", main.save(home))
    child = T.load_session("claude-code", sub.save_subagent(home, "a2"))
    assert labels(s, subagents=[child]) == ["backed"]


def test_stop_hook_ignores_a_subagent_still_running(tmp_path):
    home = tmp_path / "home"
    path = CC().user("go").call("Agent", {"description": "d", "prompt": "p", "run_in_background": True},
                                LAUNCHED % "a1", at=1).bash(*PASS, at=5).say("All tests pass.", at=8).save(home)
    CC(start=6, sidechain=True, agent_id="a1").user("go").bash(*FAIL, at=9).save_subagent(home, "a1")
    assert S.decide(hook_payload(path)) is None


def test_stop_hook_ignores_runs_in_another_repository(tmp_path):
    repo_a, repo_b = tmp_path / "repo-a", tmp_path / "repo-b"
    for repo in (repo_a, repo_b):
        (repo / ".git").mkdir(parents=True)
    path = CC(cwd=str(repo_a)).user("go").bash(*PASS).bash("cd '%s' && pytest -q" % repo_b, "1 failed in 0.1s", 1) \
        .save(tmp_path / "home")
    assert S.decide(hook_payload(path, cwd=str(repo_a))) is None


def test_stop_hook_blocks_only_for_work_done_since_the_last_prompt(tmp_path):
    home = tmp_path / "home"
    quiet = CC().user("go").bash(*FAIL).say("Two tests still fail.").user("why?").say("A timeout.").save(home, "a")
    assert S.decide(hook_payload(quiet)) is None
    assert S.decide(hook_payload(CC().user("go").bash(*FAIL).say("All tests pass.").save(home, "b"))) is not None
    old_edit = CC().user("go").bash(*PASS).edit("/work/app/src/app.py").user("thanks").say("Glad to help.")
    assert S.decide(hook_payload(old_edit.save(home, "c"))) is None
    new_edit = CC().user("go").bash(*PASS).user("one more fix").edit("/work/app/src/app.py")
    assert "changed after the last passing test run" in S.decide(hook_payload(new_edit.save(home, "d")))


def test_a_change_elsewhere_in_the_repository_after_a_run_in_a_subfolder_is_unclear(tmp_path):
    repo = tmp_path / "mono"
    (repo / ".git").mkdir(parents=True)
    for pkg in ("a", "b"):
        (repo / "packages" / pkg).mkdir(parents=True)
    run = ("cd packages/a && npm test", "Tests:       5 passed, 5 total", 0)
    s = cc_load(tmp_path, CC(cwd=str(repo)).bash(*run).edit(str(repo / "packages" / "b" / "x.js")).say("All tests pass."))
    [claim] = E.label_claims(s)
    assert claim["label"] == "unclear" and claim["why"] == "code changed elsewhere in the repository"
    s = cc_load(tmp_path, CC(cwd=str(repo)).bash(*run).edit(str(repo / "packages" / "a" / "y.js")).say("All tests pass."),
                name="b")
    assert labels(s) == ["stale"]
    s = cc_load(tmp_path, CC(cwd=str(repo)).bash("npm test", "Tests:       5 passed, 5 total", 0)
                .edit(str(repo / "packages" / "b" / "x.js")).say("All tests pass."), name="c")
    assert labels(s) == ["stale"]


def test_jest_reads_the_suite_line_too():
    out = "Test Suites: 1 failed, 3 passed, 4 total\nTests:       10 passed, 10 total"
    assert E.run_outcome("npx jest 2>&1 | tail", out, 0, "tests")[0] == "fail"


def test_a_test_run_without_a_recorded_result_makes_the_claim_unclear(tmp_path):
    s = cc_load(tmp_path, CC().bash(*PASS).call("Bash", {"command": "pytest -q"}, None).say("All tests pass."))
    assert labels(s) == ["unclear"]


def test_mcp_tools_that_write_files_are_changes(tmp_path):
    s = cc_load(tmp_path, CC().bash(*PASS).call("mcp__fs__write_file", {"path": "/work/app/src/a.py", "content": "x"},
                                                "ok").say("All tests pass."))
    [claim] = E.label_claims(s)
    assert claim["label"] == "stale" and claim["changed"] == ["/work/app/src/a.py"]
    s = cc_load(tmp_path, CC().bash(*PASS).call("mcp__fs__read_file", {"path": "/work/app/src/a.py"}, "x")
                .say("All tests pass."), name="b")
    assert labels(s) == ["backed"]
    s = cc_load(tmp_path, CC().bash(*PASS).call("mcp__notes__replace_text", {"id": 3}, "ok").say("All tests pass."),
                name="c")
    assert labels(s) == ["stale"]


def test_a_branch_switch_after_the_run_makes_the_claim_stale(tmp_path):
    s = cc_load(tmp_path, CC().bash(*PASS).bash("git checkout main", "", 0).say("All tests pass."))
    assert labels(s) == ["stale"]


def test_scan_markdown_puts_untrusted_text_in_inline_code(tmp_path, capsys):
    CC().bash("pytest -q", "1 failed, 4 passed in 0.5s", 1).say("All tests pass <b>now</b>.").save(tmp_path / "home")
    _code, out = run_cli(capsys, "scan")
    assert "- Claim: `All tests pass <b>now</b>.`" in out
    assert "project `/work/app`" in out and "failed (`exit code 1`)" in out


def _outside_code(md):
    """The markdown with fenced blocks and inline code spans taken out: what renders as markdown."""
    md = re.sub(r"(?ms)^```.*?^```", "", md)
    return re.sub(r"``.+?``|`[^`\n]*`", "", md)


def test_scan_markdown_puts_the_session_id_in_inline_code(tmp_path, capsys):
    cx = CX().run("pytest -q", "1 failed, 4 passed in 0.5s", 1).patch("src/[y](//e.co).py") \
        .say("All tests pass, see [z](//e.co).")
    cx.records[0]["payload"]["id"] = "[x](//e.co)"  # the Codex session id comes from the transcript
    cx.save(tmp_path / "home")
    _code, out = run_cli(capsys, "scan")
    assert ", session `[x](//e.co)`, project `/work/app`" in out
    assert not [line for line in out.splitlines() if "e.co" in _outside_code(line)]
    _code, data = scan_json(capsys)
    assert data["examples"][0]["session"] == "[x](//e.co)"  # JSON values stay plain


def test_diff_markdown_puts_the_detail_in_inline_code(tmp_path, capsys):
    repo = make_repo(tmp_path, {"tests/test_a.py": PY_TESTS})
    put(repo, "tests/test_a.py", PY_TESTS.replace("def test_a():", "@pytest.mark.skip\ndef test_a():"))
    _code, out = run_cli(capsys, "diff", "--repo", str(repo))
    assert "| `@pytest.mark.skip` |" in out


def test_scan_lists_unclear_claims_with_their_reason_after_the_unbacked_ones(tmp_path, capsys):
    three_sessions(tmp_path / "home")
    CC().bash("pytest -q | tail -3", "", 0).say("All tests pass.").save(tmp_path / "home", "s-unclear")
    _code, data = scan_json(capsys)
    assert all("why" in e for e in data["examples"])
    [unclear] = data["unclear_examples"]
    assert unclear["label"] == "unclear" and unclear["why"] == "the run's result could not be read"
    _code, out = run_cli(capsys, "scan")
    assert out.index("## Claims that were not backed") < out.index("## Claims that could not be checked")
    assert "| unclear | 1 | The evidence could not be read or ordered; the why column says which case. |" in out


def test_scan_notes_that_unusual_phrasing_can_be_missed(tmp_path, capsys):
    three_sessions(tmp_path / "home")
    _code, data = scan_json(capsys)
    assert any("unusual phrasing" in n for n in data["notes"])


def test_install_writes_through_a_symlinked_settings_file(tmp_path, capsys):
    target = tmp_path / "dotfiles" / "settings.json"
    target.parent.mkdir()
    target.write_text('{"model": "opus"}\n')
    link = tmp_path / "settings.json"
    link.symlink_to(target)
    code, out = install(capsys, link, "--write")
    assert code == 0 and link.is_symlink() and str(target) in out.out
    assert len(our_hooks(json.loads(target.read_text()))) == 1


def test_install_dry_run_shows_only_the_claim_check_entry(tmp_path, capsys):
    token = "Bearer " + "abc123" * 6
    settings = tmp_path / "settings.json"
    settings.write_text('{"model":"opus","env":{"API_HEADERS":"Authorization: %s"},"hooks":{"Stop":[]}}' % token)
    code, out = install(capsys, settings)
    assert code == 0 and "stop_hook.py" in out.out
    assert "abc123" not in out.out and "API_HEADERS" not in out.out and "opus" not in out.out
    assert "other lines will be reformatted" in out.out


def test_install_keeps_a_backup_and_uninstall_restores_it(tmp_path, capsys):
    settings = tmp_path / "settings.json"
    original = '{"model":   "opus",\n "hooks": {}}\n'
    settings.write_text(original)
    install(capsys, settings, "--write")
    backup = tmp_path / "settings.json.claim-check.bak"
    assert backup.read_text() == original
    install(capsys, settings, "--write", "--uninstall")
    assert settings.read_text() == original and not backup.exists()


def test_uninstall_keeps_later_edits_and_the_backup(tmp_path, capsys):
    settings = tmp_path / "settings.json"
    settings.write_text('{"model": "opus"}\n')
    install(capsys, settings, "--write")
    data = json.loads(settings.read_text())
    data["model"] = "sonnet"
    settings.write_text(json.dumps(data))
    install(capsys, settings, "--write", "--uninstall")
    assert json.loads(settings.read_text()) == {"model": "sonnet"}
    assert (tmp_path / "settings.json.claim-check.bak").exists()


def test_scripts_write_no_bytecode_next_to_themselves(tmp_path):
    import shutil
    copy = tmp_path / "scripts"
    shutil.copytree(SCRIPTS, str(copy), ignore=shutil.ignore_patterns("__pycache__"))
    env = {k: v for k, v in os.environ.items() if k != "PYTHONDONTWRITEBYTECODE"}
    for script in ("claims.py", "stop_hook.py", "install.py"):
        done = subprocess.run([sys.executable, str(copy / script), "--help"], capture_output=True, text=True,
                              timeout=60, env=env)
        assert done.returncode == 0, done.stderr
    assert not (copy / "__pycache__").exists()


def test_diff_accepts_a_coverage_threshold_that_moved_between_files(tmp_path):
    repo = make_repo(tmp_path, {"setup.cfg": "[coverage:report]\nfail_under = 90\n",
                                "pyproject.toml": "[project]\nname = 'x'\n"})
    put(repo, "setup.cfg", "[metadata]\nname = x\n")
    put(repo, "pyproject.toml", "[project]\nname = 'x'\n\n[tool.coverage.report]\nfail_under = 90\n")
    assert kinds_of(repo) == []
    put(repo, "pyproject.toml", "[project]\nname = 'x'\n\n[tool.coverage.report]\nfail_under = 70\n")
    assert kinds_of(repo) == ["lowered-coverage"]


def test_diff_reads_several_tests_folded_into_one_parametrized_test_as_one_signal(tmp_path):
    before = ("def test_one():\n    assert f(1) == 2\n\n\ndef test_two():\n    assert f(2) == 3\n\n\n"
              "def test_three():\n    assert f(3) == 4\n")
    after = ("import pytest\n\n\n@pytest.mark.parametrize('x', [1, 2, 3])\ndef test_f(x):\n    assert f(x) == x + 1\n")
    repo = make_repo(tmp_path, {"tests/test_f.py": before})
    put(repo, "tests/test_f.py", after)
    [signal] = W.find_signals(str(repo))["signals"]
    assert signal["kind"] == "replaced-tests" and signal["detail"] == "3 tests replaced by test_f (parametrized)"


def test_skill_md_names_the_hook_scope_and_reply_rules():
    text = open(os.path.join(SCRIPTS, "..", "SKILL.md"), encoding="utf-8").read()
    assert "--scope local" in text and "blocks once per reply" in text
    assert "fewer than two" in text


def test_a_run_on_a_stashed_tree_is_not_the_result_of_the_current_code(tmp_path):
    command = ("pytest -q 2>&1 | tail -2; git stash -q -- src/fix.py && pytest -q tests/test_new.py 2>&1 | tail -1; "
               "git stash pop -q && git diff --stat")
    out = "108 passed in 2.0s\n1 failed, 15 passed in 1.0s\n src/fix.py | 2 +-"
    s = cc_load(tmp_path, CC().bash(command, out, 0).say("All 108 tests pass."))
    assert labels(s) != ["contradicted"]
    command = "pytest -q && echo TESTS_OK; git stash && pytest -q tests/test_new.py; git stash pop"
    s = cc_load(tmp_path, CC().bash(command, "TESTS_OK\n1 failed in 0.1s", 0).say("All tests pass."), name="b")
    assert labels(s) == ["backed"]  # the first run printed its marker; the stashed run is left out


def test_a_baseline_run_between_stash_and_pop_in_separate_commands_leaves_the_claim_unclear(tmp_path):
    builder = CC().bash(*PASS).bash("git stash", "Saved working directory", 0).bash(*FAIL)
    builder.bash("git stash pop", "On branch main", 0).say("All tests pass.")
    assert labels(cc_load(tmp_path, builder)) == ["unclear"]
    kept = CC().bash("git stash", "Saved working directory", 0).bash(*PASS).say("All tests pass.")
    assert labels(cc_load(tmp_path, kept, name="b")) == ["backed"]  # never popped: the run tested the tree as it is


HANDBACK = ('Another Claude session sent a message:\n<agent-message from="%s">\n[Subagent hand-back] Report follows.\n'
            '</agent-message>')


def test_a_subagent_hand_back_message_marks_when_it_finished(tmp_path):
    home = tmp_path / "home"
    main = CC().user("go").call("Agent", {"description": "d", "prompt": "p", "run_in_background": True},
                                LAUNCHED % "a3", at=1)
    main.say("All tests pass.", at=10).user(HANDBACK % "a3", at=20).say("All tests pass.", at=25)
    sub = CC(start=3, sidechain=True, agent_id="a3").user("go").bash(*PASS, at=4)
    s = T.load_session("claude-code", main.save(home))
    child = T.load_session("claude-code", sub.save_subagent(home, "a3"))
    assert labels(s, subagents=[child]) == ["unclear", "backed"]


def test_a_hand_back_message_is_not_a_user_prompt_for_the_stop_hook(tmp_path):
    path = CC().user("go").bash(*FAIL).user(HANDBACK % "a4").say("All tests pass.").save(tmp_path / "home")
    assert "the last test run failed" in S.decide(hook_payload(path))


def test_a_change_by_another_subagent_after_a_subagent_s_run_leaves_the_claim_unclear(tmp_path):
    home = tmp_path / "home"
    main = CC().user("go")
    for agent in ("a5", "a6"):
        main.call("Agent", {"description": agent, "prompt": "p", "run_in_background": True}, LAUNCHED % agent, at=1)
    main.user(HANDBACK % "a5", at=30).user(HANDBACK % "a6", at=31).say("All tests pass.", at=35)
    tester = CC(start=3, sidechain=True, agent_id="a5").user("go").bash(*PASS, at=5)
    editor = CC(start=3, sidechain=True, agent_id="a6").user("go").edit("/work/app/src/other.py", at=8)
    s = T.load_session("claude-code", main.save(home))
    kids = [T.load_session("claude-code", tester.save_subagent(home, "a5")),
            T.load_session("claude-code", editor.save_subagent(home, "a6"))]
    [claim] = E.label_claims(s, subagents=kids)
    assert claim["label"] == "unclear" and claim["why"] == "another subagent changed code after the run"


# ---------------------------------------------------------------------------
# Found by the 120-day label review after the fresh-eyes fixes
# ---------------------------------------------------------------------------

def test_terminal_color_codes_do_not_hide_a_failure_line():
    out = "\x1b[1A\x1b[2K  1 failed\n    [chromium] tests/e2e/servers.spec.ts:287:5\n  1 passed (1.6m)"
    assert E.run_outcome("npx playwright test | tail -6", out, 0, "tests")[0] == "fail"


def test_a_build_error_line_is_not_a_test_run(tmp_path):
    s = cc_load(tmp_path, CC().bash(*PASS).bash("./scripts/make-app.sh 2>&1 | tail -5",
                                                "error: accessing build database: disk I/O error", 0)
                .say("All tests pass."))
    assert labels(s) == ["backed"]


def test_a_custom_runner_s_passed_and_failed_counts_are_read(tmp_path):
    s = cc_load(tmp_path, CC().bash(*FAIL).bash("swift run DevTests 2>&1 | tail -2", "24 passed, 0 failed", 0)
                .say("24/24 tests green."))
    assert labels(s) == ["backed"]
    s = cc_load(tmp_path, CC().bash(*PASS).bash("swift run DevTests 2>&1 | tail -2", "23 passed, 1 failed", 0)
                .say("All tests pass."), name="b")
    assert labels(s) == ["contradicted"]


def test_the_target_of_run_is_read_past_options_with_values(tmp_path):
    s = cc_load(tmp_path, CC().bash(*FAIL).bash('swift run --scratch-path "$HOME/.cache/b" DevTests', "all good", 0)
                .say("All tests pass."))
    assert labels(s) == ["unclear"]


def test_a_claim_that_names_its_command_is_judged_by_that_command_s_run(tmp_path):
    builder = CC().bash("npm test", "Ran 298 tests across 6 files.\n 298 pass\n 0 fail", 0)
    builder.bash("npx playwright test | tail -3", "  1 failed\n  1 passed (1.6m)", 0).say("`npm test`: 298/298 pass.")
    [claim] = E.label_claims(cc_load(tmp_path, builder))
    assert claim["label"] == "unclear" and claim["why"] == "the claim names another command than the last run"
    s = cc_load(tmp_path, CC().bash(*PASS).say("`pytest -q` passes."), name="b")
    assert labels(s) == ["backed"]


def test_a_branch_switch_inside_a_stash_detour_is_not_a_change(tmp_path):
    detour = ("git stash -u && git checkout 2ca6a59d && npx playwright test | tail -3; "
              "git checkout feature && git stash pop")
    s = cc_load(tmp_path, CC().bash(*PASS).bash(detour, "  1 failed\n  1 passed (1.7m)", 0).say("All tests pass."))
    assert labels(s) == ["backed"]


def test_a_count_of_passing_tests_in_a_description_is_not_a_claim():
    assert E.find_claims("Status: 22 passing tests, demo mode only.") == []
    assert [c["kind"] for c in E.find_claims("Harness tests: 26/26 passing.")] == ["tests"]


def test_a_printed_test_run_is_scoped_to_the_folder_the_command_changed_into(tmp_path):
    app, other = tmp_path / "app", tmp_path / "notes"
    app.mkdir()
    other.mkdir()
    run = ("cd '%s' && swift run DevTests 2>&1 | tail -2" % app, "33 passed, 0 failed", 0)
    s = cc_load(tmp_path, CC(cwd=str(tmp_path)).bash(*run).edit(str(other / "workspaces.json")).say("33/33 tests green."))
    assert labels(s) == ["backed"]


def test_a_status_line_reports_a_state_not_a_run():
    assert E.find_claims("Status: built, 24 tests green; the eval re-run is in flight.") == []


@pytest.mark.parametrize("command", [
    "for f in how-to-test-drive.md tests.md; do wc -c \"$f\"; done",
    "\"$HOME/voice/scripts/lint-copy.sh\" how-to-test-drive-a-harness.md",
    "./scripts/render.sh docs/testing-guide.md",
])
def test_doc_files_loops_and_lint_scripts_are_not_hidden_test_runs(tmp_path, command):
    s = cc_load(tmp_path, CC().bash(*PASS).bash(command, "", 0).say("All tests pass."))
    assert labels(s) == ["backed"]
