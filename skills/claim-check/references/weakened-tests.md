# Weakened-test signals in a diff

`claims.py diff` compares the working tree (staged, unstaged, and new untracked files) with the last
commit, or with the point where the current branch left `--base`. It runs git read-only
(`--no-optional-locks`, so not even the index is refreshed). A signal is a change a person should
look at; some are intended. Only test files that already existed can produce skip, focus, assertion,
or test-removal signals: a new test file weakens nothing, and its platform guards are its own.

## Why these checks exist

- An audit of 101 "tests pass" claims from coding agents found 35 false, most of them after an edit
  that was never tested again ([dev.to, 2026-09-24](https://dev.to/vinzenz_eiberger/i-checked-101-tests-pass-claims-from-my-ai-coding-agents-35-werent-true-h6n)).
  The same audit notes that a piped test command reports the exit code of the last program in the
  pipe, which `scan` accounts for.
- A developer found an agent reporting all tests green after it had deleted the failing one, and
  built diff checks for skips, deleted test files, and removed assertions; a later gap was a test
  swapped for another, which keeps the counts equal
  ([dev.to, 2026-09-16](https://dev.to/leoleroy/i-got-tired-of-coding-agents-saying-all-tests-pass-when-the-diff-said-otherwise-5ce9);
  tool: [i-dont-believe-you](https://github.com/LeonardLeroy/i-dont-believe-you)). The removed-test
  signal below covers that swap.
- SpecBench (arXiv:2605.21384, 2026) reports that frontier agents pass the visible tests while
  gaming them, and BAITBENCH (arXiv:2608.30724, 2026-08-31) finds reward hacking in more than half of
  runs on tasks with planted shortcuts, even when agents are told not to cheat.

## The signals

| Signal | What triggers it |
|---|---|
| `deleted-test-file` | A test file was deleted. A renamed test file is not deleted. |
| `removed-test` | A test definition (`def test_x`, `it("...")`, `test("...")`, `func TestX`, `function testX`) was removed and the same name was not added anywhere in the diff, so a test moved to another file is fine. |
| `removed-assertions` | A test file lost more assertion lines than it gained, and the diff as a whole lost assertions (moving tests between files is not a loss). Assertions: `assert`, `self.assert*`, `pytest.raises`, `expect(`, `.toBe*`/`.toEqual*` and similar, `.should`, Go `t.Error`/`t.Fatal`, `require.`, `XCTAssert`, `$this->assert`, `Assert.` |
| `replaced-tests` | A test file removed two or more tests and added one parametrized test (`@pytest.mark.parametrize`, `it.each`/`test.each`, `@ParameterizedTest`, `[Theory]`, Go `t.Run`). One signal for the file, in place of `removed-test` and `removed-assertions`: check that the new test covers each removed case. |
| `added-skip` | More skip or expected-failure markers were added than removed in an existing test file (see the table below). A marker that moved within the file is not new. |
| `added-focus` | JavaScript `it.only(`, `describe.only(`, `test.only(`, `fit(`, `fdescribe(`: only the focused tests run. |
| `ignored-failure` | A test command in CI, a shell script, a Makefile, package.json, tox.ini, or noxfile.py gained `\|\| true`, `\|\| exit 0`, `; true`, `\|\| echo`, `--passWithNoTests`, or `--exit-zero`; or a CI step or job that runs tests gained `continue-on-error: true` (GitHub Actions) or `allow_failure: true` (GitLab). |
| `lowered-coverage` | `--cov-fail-under`, `fail_under`, `minimum_coverage`, or (in a file about coverage) a `branches`, `functions`, `lines`, or `statements` threshold went down or was removed. Thresholds are compared across the whole diff by name, so one moved to another file at the same or a higher value is not a signal. |

Marker text inside quoted strings is ignored, so test data and messages that mention a marker do not
count.

## Skip and expected-failure markers by framework

| Files | Markers |
|---|---|
| Python | `@pytest.mark.skip`, `@pytest.mark.skipif`, `@pytest.mark.xfail`, `pytest.skip(`, `pytest.xfail(`, `@unittest.skip`, `skipIf`, `skipUnless`, `expectedFailure`, `self.skipTest(` |
| JavaScript and TypeScript (jest, vitest, mocha) | `it.skip(`, `test.skip(`, `describe.skip(`, `it.todo(`, `test.todo(`, `xit(`, `xtest(`, `xdescribe(`, `test.fails(` |
| Go | `t.Skip(`, `t.Skipf(`, `t.SkipNow(` |
| Rust | `#[ignore]` |
| Java, Kotlin, Scala (JUnit) | `@Disabled`, `@Ignore` |
| Ruby (RSpec) | `xit`, `xspecify`, `xdescribe`, `xcontext`, `skip`, `pending` at the start of a line |
| PHP (PHPUnit) | `$this->markTestSkipped(`, `$this->markTestIncomplete(` |
| C# (NUnit, xUnit) | `[Ignore]`, `[Fact(Skip = ...)]`, `[Theory(Skip = ...)]` |
| Swift (XCTest) | `XCTSkip`, `XCTSkipIf`, `XCTSkipUnless` |

## Which files count as tests

Names such as `test_*.py`, `*_test.py`, `*_test.go`, `*.test.ts`, `*.spec.js`, `*_spec.rb`,
`*Test.java`, `*Tests.cs`, and `conftest.py`, plus source files inside `tests/`, `test/`,
`__tests__/`, `spec/`, or `src/test/`. Rust tests written inside source files (`#[cfg(test)]`) are
outside these rules.

## Limits

- A weaker assertion that keeps its line (`assert x == 3` becoming `assert x`) is not counted.
- A test that now always passes by other means (mocking the thing under test, catching every error)
  is not detected.
- Skips added through configuration (a `-k "not slow"` filter, an ignore list in a config file) are
  not detected.
- Python triple-quoted strings that span lines are read line by line, so marker text inside them can
  still count.
