"""Mine test-drive tasks from a repository's own git history.

A task is a past commit that changed both source files and test files. The
task text is the commit message, the hidden tests are the commit's test-file
changes, and the gold patch is the rest of the change. With --validate each
task is checked in a fresh temporary copy of the repository: at the parent
commit plus the hidden tests the test command must fail, and with the whole
commit it must pass, twice (a changing result is flaky). Only tasks that pass
that check are kept. The HEAD check's output goes to <tasks folder>/logs/.

Usage:
    python3 mine_tasks.py --repo . [--test-cmd "<cmd>"] [--validate] [--max 10] [--since 365d]
        [--setup-cmd "<cmd>"] [--test-timeout 600] [--tasks <path>] [--json] [--out <path>]

Exit codes: 0 success (including "no tasks found"), 2 usage or input error
(not a git repository, no test command, or a test command that fails at HEAD),
130 stopped (SIGINT, SIGTERM, or SIGHUP; the temporary copies are deleted).
The user's repository is only read: tasks.json is the one file written.
Python 3.9+, standard library only.
"""

from __future__ import annotations

import argparse
import datetime
import json
import os
import re
import sys
import time

from common import (classify, duration, git, install_stop_handlers, make_workspace, remove_workspace,
                    restore_handlers, run_command, run_tests, touches_manifest, write_files)
from safe import code, safe_text

MAX_FILES = 30     # commits that change more files are too large for a task
MAX_LINES = 1000   # same for added plus deleted lines
_TRAILER_RE = re.compile(r"^[A-Za-z][A-Za-z0-9-]*: \S")
_NO_TEST_SCRIPT = "no test specified"   # npm's placeholder "test" script
MAX_CHECKS_PER_TASK = 3   # check at most this many candidates per wanted task
DEFAULT_FOLDER = ".harness-test-drive"


class UsageError(Exception):
    """Bad input: the message says what to change."""


def detect_test_cmd(repo) -> tuple:
    """(command, what it came from) from the project files, or (None, "")."""
    def path(name):
        return os.path.join(repo, name)

    def read(name):
        try:
            with open(path(name), encoding="utf-8", errors="replace") as fh:
                return fh.read()
        except OSError:
            return ""

    try:
        script = json.loads(read("package.json") or "{}").get("scripts", {}).get("test")
    except (ValueError, AttributeError):
        script = None
    if isinstance(script, str) and script.strip() and _NO_TEST_SCRIPT not in script:
        runner = ("pnpm test" if os.path.exists(path("pnpm-lock.yaml")) else
                  "yarn test" if os.path.exists(path("yarn.lock")) else
                  "bun run test" if os.path.exists(path("bun.lock")) or os.path.exists(path("bun.lockb")) else
                  "npm test")
        return runner, "package.json scripts.test"
    pytest = "uv run pytest -q" if os.path.exists(path("uv.lock")) else "python3 -m pytest -q"
    for name, marker in (("pytest.ini", ""), ("pyproject.toml", "[tool.pytest"), ("setup.cfg", "[tool:pytest]"),
                         ("tox.ini", "[pytest]"), ("conftest.py", ""), ("tests/conftest.py", ""),
                         ("test/conftest.py", "")):
        if os.path.isfile(path(name)) and marker in read(name):
            return pytest, name
    if os.path.isdir(path("tests")) and any(n.startswith("test_") and n.endswith(".py") for n in os.listdir(path("tests"))):
        return pytest, "test files in tests/"
    if os.path.isfile(path("go.mod")):
        return "go test ./...", "go.mod"
    if os.path.isfile(path("Cargo.toml")):
        return "cargo test", "Cargo.toml"
    if re.search(r"^test\s*:", read("Makefile"), re.M):
        return "make test", "Makefile test target"
    return None, ""


def parse_since(text) -> str:
    """'365d' becomes '365 days ago'; a YYYY-MM-DD date passes through."""
    m = re.fullmatch(r"(\d+)d", text or "")
    if m:
        return "%d days ago" % int(m.group(1))
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", text or ""):
        datetime.date.fromisoformat(text)  # raises ValueError for a bad date
        return text
    raise ValueError("--since takes a number of days such as 365d, or a date such as 2026-01-15")


def _lines(count) -> int:
    return int(count) if count.isdigit() else 0   # "-" for binary files


def find_candidates(repo, since="365d", max_files=MAX_FILES, max_lines=MAX_LINES) -> tuple:
    """Commits on HEAD since `since`, newest first, that change both source and
    test files. Returns (candidates, counts)."""
    try:
        out = git(repo, "log", "-z", "--no-merges", "--no-renames", "--since=" + parse_since(since),
                  "--format=%x1e%H%x1f%P", "--numstat", "HEAD")
    except RuntimeError as err:
        raise UsageError("Could not read the history (%s). A partial clone keeps old file contents on the "
                         "server, and this skill fetches nothing: fetch the full history first "
                         "(git fetch --refetch), then run again." % err)
    commits = []
    for tok in out.split(b"\0"):
        tok = tok.lstrip(b"\n")
        if tok.startswith(b"\x1e"):
            sha, _, parents = tok[1:].decode().partition("\x1f")
            commits.append((sha, parents.split(), []))
        elif tok and commits:
            added, deleted, name = tok.split(b"\t", 2)
            commits[-1][2].append((_lines(added.decode()) + _lines(deleted.decode()), os.fsdecode(name)))
    candidates, skipped = [], {}
    for sha, parents, files in commits:
        kinds = {name: classify(name) for _, name in files}
        tests = [name for _, name in files if kinds[name] == "test"]
        if not parents:
            reason = "first commit"
        elif len(files) > max_files or sum(n for n, _ in files) > max_lines:
            reason = "too large"
        elif "source" not in kinds.values():
            reason = "no source changes"
        elif not tests:
            reason = "no test changes"
        else:
            candidates.append({
                "commit": sha, "parent": parents[0], "hidden_tests": tests,
                "gold_files": [name for _, name in files if kinds[name] != "test"],
                "gold_lines": sum(n for n, name in files if kinds[name] in ("source", "other"))})
            continue
        skipped[reason] = skipped.get(reason, 0) + 1
    return candidates, {"commits": len(commits), "candidates": len(candidates), "skipped": skipped}


def task_text(repo, sha) -> tuple:
    """(subject, body) of a commit message, with trailers such as
    Co-Authored-By removed from the body."""
    message = git(repo, "log", "-1", "--format=%B", sha).decode("utf-8", "replace").strip()
    subject, _, body = message.partition("\n")
    paragraphs = [p for p in body.strip().split("\n\n") if p.strip()]
    if paragraphs and all(_TRAILER_RE.match(line) for line in paragraphs[-1].splitlines() if line.strip()):
        paragraphs.pop()
    return subject.strip(), "\n\n".join(paragraphs).strip()


def _check(repo, commit, test_cmd, setup_cmd, timeout, work_root, hidden=(), gold=(), logs=None) -> tuple:
    """Run the tests in a fresh copy of `commit`. With no `gold`, this is the
    HEAD check: the tests must pass as they are. Otherwise the tests must fail
    with the hidden tests written, pass with the gold files written too, and do
    both once more. Returns (reason the check failed or "", the runs)."""
    try:
        ws = make_workspace(repo, commit, root=work_root)
    except (OSError, RuntimeError):  # a damaged clone or a full disk
        return "could not copy the commit", {}
    runs, logs = {}, logs or {}

    def setup():
        return not setup_cmd or run_command(setup_cmd, ws, timeout, logs.get("setup"))["exit"] == 0

    def switch(at, paths):  # write paths as they are at `at`; a changed manifest needs the setup again
        write_files(repo, at, paths, ws)
        return not touches_manifest(paths) or setup()

    try:
        if not setup():
            return "setup command failed", runs
        if not gold:
            runs["after"] = run_tests(test_cmd, ws, timeout, logs.get("tests"))
            return ("" if runs["after"]["exit"] == 0 else "tests fail"), runs
        fix, gold_files = gold
        if not switch(fix, hidden):
            return "setup command failed", runs
        runs["before"] = run_tests(test_cmd, ws, timeout)
        if runs["before"]["exit"] == 0:
            return "tests pass before the fix", runs
        if not switch(fix, gold_files):
            return "setup command failed", runs
        runs["after"] = run_tests(test_cmd, ws, timeout)
        if runs["after"]["exit"] != 0:
            return ("tests time out with the fix" if runs["after"]["timed_out"] else "tests fail with the fix"), runs
        # Once more each way: tests whose result changes between runs cannot score an agent.
        if not switch(commit, gold_files):
            return "setup command failed", runs
        runs["before_again"] = run_tests(test_cmd, ws, timeout)
        if not switch(fix, gold_files):
            return "setup command failed", runs
        runs["after_again"] = run_tests(test_cmd, ws, timeout)
        if runs["before_again"]["exit"] == 0 or runs["after_again"]["exit"] != 0:
            return "flaky", runs
        return "", runs
    finally:
        remove_workspace(ws)


def _output_tail(path, lines=15) -> str:
    """The last lines of a saved log, made safe, and where the full log is."""
    try:
        with open(path, encoding="utf-8", errors="replace") as fh:
            text = [code(line, 200) for line in fh.read().splitlines() if line.strip()]
    except (OSError, TypeError):
        return ""
    shown = "\n".join("  " + line for line in text[-lines:]) or "  (no output)"
    return "\nLast lines of its output:\n%s\nFull output: %s" % (shown, code(path, 300))


def _task(repo, cand, validated, runs=None) -> dict:
    subject, body = task_text(repo, cand["commit"])
    return dict({"id": cand["commit"][:12], "subject": subject, "body": body}, **cand,
                validated=validated, check=runs or {})


def mine(repo, test_cmd, test_cmd_source="--test-cmd", since="365d", max_tasks=10, validate=False,
         setup_cmd="", test_timeout=600, work_root=None, logs_dir=None) -> dict:
    """Find tasks, check them when `validate` is set, and return the tasks
    document. With `logs_dir`, the HEAD check saves its output there."""
    top = git(repo, "rev-parse", "--show-toplevel").decode().strip()
    head = git(top, "rev-parse", "HEAD").decode().strip()
    candidates, counts = find_candidates(top, since)
    counts.update(checked=0, validated=0, rejected={})
    doc = {"schema": "harness-test-drive/tasks/1", "repo": top, "head": head, "since": since,
           "created": datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
           "test_cmd": test_cmd, "test_cmd_source": test_cmd_source, "setup_cmd": setup_cmd,
           "test_timeout": test_timeout, "head_check": None, "counts": counts, "validation_seconds": None,
           "tasks": []}
    if not validate:
        doc["tasks"] = [_task(top, c, False) for c in candidates[:max_tasks]]
        return doc
    started = time.monotonic()
    logs = {}
    if logs_dir:
        os.makedirs(logs_dir, exist_ok=True)
        logs = {"setup": os.path.join(logs_dir, "head-setup.log"), "tests": os.path.join(logs_dir, "head-check.log")}
    reason, runs = _check(top, head, test_cmd, setup_cmd, test_timeout, work_root, logs=logs)
    doc["head_check"] = runs.get("after")
    if reason == "could not copy the commit":
        raise UsageError("Could not copy the files of HEAD into a fresh folder (a partial or damaged clone?).")
    if reason == "setup command failed":
        raise UsageError("The setup command fails at HEAD in a fresh copy of the repository. Fix --setup-cmd."
                         + _output_tail(logs.get("setup")))
    if reason:
        failed = runs["after"]
        what = ("it timed out after %s; raise --test-timeout if the tests need longer" % duration(failed["seconds"])
                if failed["timed_out"] else "exit %s after %s" % (failed["exit"], duration(failed["seconds"])))
        raise UsageError("The test command fails at HEAD in a fresh copy of the repository (%s). Pass a working "
                         "--test-cmd, or --setup-cmd to install what the tests need in the copy.%s"
                         % (what, _output_tail(logs.get("tests"))))
    for cand in candidates[:MAX_CHECKS_PER_TASK * max_tasks]:
        if len(doc["tasks"]) >= max_tasks:
            break
        counts["checked"] += 1
        reason, runs = _check(top, cand["parent"], test_cmd, setup_cmd, test_timeout, work_root,
                              hidden=cand["hidden_tests"], gold=(cand["commit"], cand["gold_files"]))
        if reason:
            counts["rejected"][reason] = counts["rejected"].get(reason, 0) + 1
        else:
            doc["tasks"].append(_task(top, cand, True, runs))
    counts["validated"] = len(doc["tasks"])
    doc["validation_seconds"] = round(time.monotonic() - started, 1)
    return doc


def _counted(counts) -> str:
    return ", ".join("%d %s" % (n, reason) for reason, n in sorted(counts.items(), key=lambda kv: -kv[1]))


def render_report(doc, tasks_path) -> str:
    counts, tasks = doc["counts"], doc["tasks"]
    since = safe_text(parse_since(doc["since"]))
    if tasks and counts["checked"]:
        headline = ("Found %d task%s in your git history: %d of %d checked commits fail their tests before "
                    "the change and pass after it, twice." % (len(tasks), "" if len(tasks) == 1 else "s",
                                                              counts["validated"], counts["checked"]))
    elif tasks:
        headline = ("Found %d candidate task%s in your git history. None is checked yet: run again with "
                    "--validate to keep only tasks whose tests fail before the change and pass after it."
                    % (len(tasks), "" if len(tasks) == 1 else "s"))
    elif counts["checked"]:
        headline = ("Found no usable tasks: none of %d checked commits fails its tests before the change and "
                    "passes after it, twice." % counts["checked"])
    else:
        headline = "Found no candidate tasks in your git history since %s." % since
    lines = ["**%s**" % headline, ""]
    cmd = "Test command: %s (%s)." % (code(doc["test_cmd"], 200), safe_text(doc["test_cmd_source"]))
    if doc["head_check"]:
        cmd += " It passes at HEAD in %s." % duration(doc["head_check"]["seconds"])
    lines += [cmd, ""]
    if tasks:
        lines += ["| # | Task | Change | Hidden test files | Lines in the original fix | Checked |",
                  "|---|---|---|---|---|---|"]
        for i, t in enumerate(tasks, 1):
            lines.append("| %d | %s | %s | %d | %d | %s |" % (
                i, t["id"][:7], code(t["subject"], 70), len(t["hidden_tests"]), t["gold_lines"],
                "yes" if t["validated"] else "no"))
        lines.append("")
    history = "History since %s: %d commits, %d change both source and test files." % (
        since, counts["commits"], counts["candidates"])
    if counts["skipped"]:
        history += " Skipped: %s." % _counted(counts["skipped"])
    lines.append(history)
    if counts["checked"]:
        checked = "Checked %d in fresh copies of the repository: %d kept" % (counts["checked"], counts["validated"])
        if counts["rejected"]:
            checked += ", %s" % _counted(counts["rejected"])
        lines.append("%s. Checking took %s." % (checked, duration(doc["validation_seconds"])))
    lines += ["", "Tasks file: %s" % code(tasks_path, 300)]
    if tasks and counts["checked"]:
        lines.append("Next: %s shows the number of runs and a cost range."
                     % code("drive.py estimate --tasks " + tasks_path, 330))
    return "\n".join(lines) + "\n"


def summary_json(doc, tasks_path) -> dict:
    """The --json output: stable keys, untrusted text made safe."""
    return {
        "tasks_file": safe_text(tasks_path, 300), "repo": safe_text(doc["repo"], 300), "head": doc["head"],
        "test_cmd": safe_text(doc["test_cmd"], 300), "test_cmd_source": doc["test_cmd_source"],
        "head_check": doc["head_check"], "counts": doc["counts"], "validation_seconds": doc["validation_seconds"],
        "tasks": [{"id": t["id"], "commit": t["commit"], "subject": safe_text(t["subject"], 120),
                   "hidden_tests": [safe_text(p, 200) for p in t["hidden_tests"]],
                   "gold_files": [safe_text(p, 200) for p in t["gold_files"]],
                   "gold_lines": t["gold_lines"], "validated": t["validated"]} for t in doc["tasks"]]}


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Mine test-drive tasks from a repository's git history.")
    parser.add_argument("--repo", default=".", help="the git repository (default: the current folder)")
    parser.add_argument("--test-cmd", help="shell command that runs the tests (default: detected)")
    parser.add_argument("--validate", action="store_true",
                        help="keep only tasks whose tests fail before the change and pass after it, twice")
    parser.add_argument("--max", type=int, default=10, help="most tasks to keep (default 10)")
    parser.add_argument("--since", default="365d", help="how far back: 365d, or a date such as 2026-01-15")
    parser.add_argument("--setup-cmd", default="", help="shell command run in each fresh copy before the tests")
    parser.add_argument("--test-timeout", type=int, default=600, help="seconds per test run (default 600)")
    parser.add_argument("--tasks", help="where to write tasks.json (default: <repo>/%s/tasks.json)" % DEFAULT_FOLDER)
    parser.add_argument("--json", action="store_true", help="print JSON instead of the markdown report")
    parser.add_argument("--out", help="also write the report to this file")
    args = parser.parse_args(argv)
    handlers = install_stop_handlers()
    try:
        return _main(args)
    except KeyboardInterrupt:
        print("Stopped. The temporary copies are deleted, and no tasks file was written.", file=sys.stderr)
        return 130
    finally:
        restore_handlers(handlers)


def _main(args) -> int:
    try:
        if args.max < 1 or args.test_timeout < 1:
            raise UsageError("--max and --test-timeout must be at least 1.")
        parse_since(args.since)
        try:
            top = git(args.repo, "rev-parse", "--show-toplevel").decode().strip()
        except (RuntimeError, OSError):
            raise UsageError("%s is not a git repository." % code(args.repo, 300))
        test_cmd, source = (args.test_cmd, "--test-cmd") if args.test_cmd else detect_test_cmd(top)
        if not test_cmd:
            raise UsageError("No test command found (no package.json test script, pytest config or tests folder, "
                             "go.mod, Cargo.toml, or Makefile test target). Pass --test-cmd. Look in "
                             ".github/workflows or the README for the command CI runs.")
        tasks_path = args.tasks or os.path.join(top, DEFAULT_FOLDER, "tasks.json")
        folder = os.path.dirname(os.path.abspath(tasks_path))
        os.makedirs(folder, exist_ok=True)
        if not args.tasks and not os.path.exists(os.path.join(folder, ".gitignore")):
            with open(os.path.join(folder, ".gitignore"), "w") as fh:
                fh.write("# Written by harness-test-drive. Keeps this folder out of git.\n*\n")
        print("Test command: %s (%s)" % (code(test_cmd, 200), source), file=sys.stderr)
        doc = mine(top, test_cmd, source, args.since, args.max, args.validate, args.setup_cmd, args.test_timeout,
                   logs_dir=os.path.join(folder, "logs"))
    except (UsageError, ValueError, RuntimeError) as err:  # RuntimeError: a git read failed, such as no commits
        print("error: %s" % err, file=sys.stderr)
        return 2
    with open(tasks_path, "w", encoding="utf-8") as fh:
        json.dump(doc, fh, indent=2)
        fh.write("\n")
    text = (json.dumps(summary_json(doc, tasks_path), indent=2) + "\n" if args.json
            else render_report(doc, tasks_path))
    sys.stdout.write(text)
    if args.out:
        with open(args.out, "w", encoding="utf-8") as fh:
            fh.write(render_report(doc, tasks_path))
    return 0


if __name__ == "__main__":
    sys.exit(main())
