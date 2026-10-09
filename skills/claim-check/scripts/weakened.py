"""Signs of weakened tests in a git diff: deleted test files, removed
assertions or tests, several tests folded into one parametrized test, new
skip or focus markers, test commands allowed to fail, and lowered coverage
thresholds.

Read-only: every git command runs with --no-optional-locks, so not even the
index is refreshed. Python 3.9+, standard library only.
"""

from __future__ import annotations

import os
import re
import subprocess

from evidence import TESTS, command_kinds

EMPTY_TREE = "4b825dc642cb6eb9a060e54bf8d69288fbee4904"  # git's id for an empty tree


class GitError(Exception):
    """git could not answer: not a repository, or an unknown ref."""


def _git(repo, *args) -> str:
    try:
        done = subprocess.run(["git", "--no-optional-locks", "-c", "core.quotePath=false", "-C", repo] + list(args),
                              capture_output=True, timeout=120)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise GitError("git did not run: %s" % type(exc).__name__)
    if done.returncode != 0:
        raise GitError(done.stderr.decode("utf-8", "replace").strip().splitlines()[0] if done.stderr else "git failed")
    return done.stdout.decode("utf-8", "replace")


# ---------------------------------------------------------------------------
# Which files are tests, CI, or configuration
# ---------------------------------------------------------------------------

_CODE_EXT = (".py", ".js", ".jsx", ".ts", ".tsx", ".mjs", ".cjs", ".mts", ".cts", ".go", ".rb", ".rs", ".java",
             ".kt", ".kts", ".cs", ".swift", ".php", ".scala", ".ex", ".exs", ".c", ".cc", ".cpp", ".m", ".dart")
_TEST_NAME = re.compile(r"(?:^|/)(?:test_[^/]+\.py|[^/]+_test\.(?:py|go|rb|exs?|dart)|[^/]+\.(?:test|spec)\.[cm]?[jt]sx?|"
                        r"[^/]+_spec\.rb|[^/]+(?:Test|Tests|IT|Spec)\.(?:java|kt|cs|swift|php|scala)|conftest\.py)$")
_TEST_DIR = re.compile(r"(?:^|/)(?:tests?|__tests__|specs?|testing|src/test)/")
_CI = re.compile(r"(?:^|/)(?:\.github/workflows/[^/]+\.ya?ml|\.gitlab-ci\.ya?ml|\.circleci/config\.ya?ml|"
                 r"azure-pipelines\.ya?ml|Jenkinsfile|bitbucket-pipelines\.ya?ml|\.travis\.ya?ml|\.drone\.ya?ml|"
                 r"\.buildkite/[^/]+)$")
_SCRIPTS = re.compile(r"(?:^|/)(?:[^/]+\.(?:sh|bash|mk)|[Mm]akefile|package\.json|tox\.ini|noxfile\.py|justfile|"
                      r"Taskfile\.ya?ml|pyproject\.toml|setup\.cfg|\.pre-commit-config\.ya?ml)$")


def is_test_file(path) -> bool:
    return bool(_TEST_NAME.search(path)) or (path.endswith(_CODE_EXT) and bool(_TEST_DIR.search(path)))


# ---------------------------------------------------------------------------
# Patterns
# ---------------------------------------------------------------------------

_ASSERT = re.compile(r"\bassert\w*\b|\bexpect\s*\(|\.should\b|\bt\.(?:Error|Errorf|Fatal|Fatalf|Fail|FailNow)\b|"
                     r"\brequire\.\w+\(|\bXCTAssert\w*|\$this->assert\w*|\bAssert\.\w+|\bpytest\.raises\b|"
                     r"\.to(?:Be|Equal|Match|Throw|Have|Contain|StrictEqual)\w*\(")
_TEST_DEFS = [
    re.compile(r"^\s*(?:async\s+)?def\s+(test\w*)\s*\("),
    re.compile(r"\b[xf]?(?:it|test|specify)(?:\.(?:only|skip|todo|each\([^)]*\)))?\s*\(\s*(['\"`])(.+?)\1"),
    re.compile(r"^\s*func\s+(Test\w+)\s*\("),
    re.compile(r"^\s*(?:it|test|specify)\s+(['\"])(.+?)\1"),
    re.compile(r"\bfunction\s+(test\w+)\s*\("),
    re.compile(r"\bfunc\s+(test\w+)\s*\("),
]
_SKIPS = {  # file endings -> markers that skip a test or expect it to fail
    (".py",): r"@pytest\.mark\.(?:skip|skipif|xfail)\b|\bpytest\.(?:skip|xfail)\(|@unittest\.(?:skip|skipIf|"
              r"skipUnless|expectedFailure)\b|\bself\.skipTest\(",
    (".js", ".jsx", ".ts", ".tsx", ".mjs", ".cjs", ".mts", ".cts"):
        r"\b(?:it|test|describe|context|suite)\.(?:skip|todo)\s*\(|\b(?:xit|xtest|xdescribe|xcontext)\s*\(|"
        r"\btest\.fails\s*\(",
    (".go",): r"\bt\.Skip(?:f|Now)?\(",
    (".rs",): r"#\[ignore\b",
    (".java", ".kt", ".kts", ".scala"): r"@(?:Disabled|Ignore)\b",
    (".rb",): r"^\s*(?:xit|xspecify|xdescribe|xcontext|skip|pending)\b",
    (".php",): r"\$this->mark(?:TestSkipped|TestIncomplete)\(",
    (".cs",): r"\[(?:Ignore\b|Fact\s*\(\s*Skip\s*=|Theory\s*\(\s*Skip\s*=)",
    (".swift",): r"\bXCTSkip(?:If|Unless)?\b",
}
_FOCUS = {(".js", ".jsx", ".ts", ".tsx", ".mjs", ".cjs", ".mts", ".cts"):
          r"\b(?:it|test|describe|context)\.only\s*\(|\b(?:fit|fdescribe|fcontext)\s*\("}
_PARAMETRIZED = re.compile(r"@pytest\.mark\.parametrize\b|\.each\b|@Parameterized?Test\b|"
                           r"\[(?:Theory|TestCase|InlineData)\b|\bt\.Run\(")
_STRING = re.compile(r'"(?:[^"\\\n]|\\.)*"|\'(?:[^\'\\\n]|\\.)*\'|`(?:[^`\\\n]|\\.)*`')


def _code(line) -> str:
    """A source line with its string literals emptied: marker text inside a
    string (test data, a message) is not a marker."""
    return _STRING.sub('""', line)


def _pattern(table, path):
    for endings, pattern in table.items():
        if path.endswith(endings):
            return re.compile(pattern)
    return None


_IGNORE_FAIL = re.compile(r"\|\|\s*(?:true|:|exit\s+0)\b|;\s*(?:true|exit\s+0)\s*[\"']?\s*$|--passWithNoTests\b|"
                          r"--exit-zero\b|\|\|\s*echo\b")
_ALLOW_FAIL = re.compile(r"^\s*(?:continue-on-error|allow_failure)\s*:\s*true\b")
_THRESHOLDS = [
    re.compile(r"(--cov-fail-under)[= ](\d+(?:\.\d+)?)"),
    re.compile(r"\b(fail[_-]under)\s*[=:]\s*(\d+(?:\.\d+)?)"),
    re.compile(r"\b(minimum_coverage)\s*\(?\s*(\d+(?:\.\d+)?)"),
]
_COVERAGE_KEYS = re.compile(r"[\"']?\b(branches|functions|lines|statements)\b[\"']?\s*:\s*(\d+(?:\.\d+)?)")
_RUN_PREFIX = re.compile(r"^\s*(?:-\s+)?(?:run|script|command|commands)?\s*:?\s*(?:-\s+)?[\"']?|[\"']?\s*,?\s*$")


def _command_text(line) -> str:
    """The shell command inside a YAML `run:` line or a package.json script."""
    m = re.match(r'^\s*"[\w:.-]+"\s*:\s*"(.*)"\s*,?\s*$', line)
    return m.group(1) if m else _RUN_PREFIX.sub("", line)


# ---------------------------------------------------------------------------
# Reading the diff
# ---------------------------------------------------------------------------

class _File:
    __slots__ = ("path", "old_path", "status", "added", "removed")

    def __init__(self, path):
        self.path, self.old_path, self.status = path, path, "M"
        self.added, self.removed = [], []  # [(line number, text)]


def _unquote(path) -> str:
    if path.startswith('"') and path.endswith('"'):
        return path[1:-1].encode("latin-1", "backslashreplace").decode("unicode_escape").encode(
            "latin-1", "replace").decode("utf-8", "replace")
    return path


def _parse_diff(text) -> list:
    files, cur, new_line = [], None, 0
    for line in text.splitlines():
        if line.startswith("diff --git "):
            cur = None
        elif line.startswith("--- "):
            old = _unquote(line[4:].rstrip("\t"))
            cur = _File("")
            cur.old_path = old[2:] if old.startswith("a/") else ""
            files.append(cur)
        elif line.startswith("+++ ") and cur is not None:
            new = _unquote(line[4:].rstrip("\t"))
            cur.path = new[2:] if new.startswith("b/") else ""
            cur.status = "D" if not cur.path else ("A" if not cur.old_path else "M")
            if not cur.path:
                cur.path = cur.old_path
        elif line.startswith("@@") and cur is not None:
            m = re.match(r"^@@ -\d+(?:,\d+)? \+(\d+)", line)
            new_line = int(m.group(1)) if m else 0
        elif cur is not None and line.startswith("+"):
            cur.added.append((new_line, line[1:]))
            new_line += 1
        elif cur is not None and line.startswith("-"):
            cur.removed.append((0, line[1:]))
    return files


def _untracked(repo) -> list:
    out = []
    for rel in _git(repo, "ls-files", "--others", "--exclude-standard", "-z").split("\0"):
        if not rel:
            continue
        full = os.path.join(repo, rel)
        try:
            if os.path.getsize(full) > 1 << 20:
                continue
            with open(full, "rb") as fh:
                data = fh.read()
        except OSError:
            continue
        if b"\0" in data:
            continue
        f = _File(rel)
        f.status, f.old_path = "A", ""
        f.added = list(enumerate(data.decode("utf-8", "replace").splitlines(), 1))
        out.append(f)
    return out


# ---------------------------------------------------------------------------
# Signals
# ---------------------------------------------------------------------------

def _signal(kind, path, line, detail) -> dict:
    return {"kind": kind, "file": path, "line": line, "detail": detail}


def _test_names(lines) -> list:
    names = []
    for _n, text in lines:
        for rx in _TEST_DEFS:
            m = rx.search(text)
            if m:
                names.append(m.group(m.lastindex))
                break
    return names


def _markers(f, rx) -> list:
    added = [(n, t) for n, t in f.added if rx.search(_code(t))]
    removed = [t for _n, t in f.removed if rx.search(_code(t))]
    return added[len(removed):] if len(added) > len(removed) else []


def _read(repo, path) -> str:
    try:
        with open(os.path.join(repo, path), encoding="utf-8", errors="replace") as fh:
            return fh.read()
    except OSError:
        return ""


def _indent(line) -> int:
    return len(line) - len(line.lstrip(" "))


def _block_runs_tests(repo, f, line_no) -> bool:
    """Whether the YAML block that holds line `line_no` (a CI step or job)
    runs a test command."""
    lines = _read(repo, f.path).splitlines()
    if not 0 < line_no <= len(lines):
        return False
    i = line_no - 1
    own = _indent(lines[i])
    start = next((j for j in range(i - 1, -1, -1) if lines[j].strip() and _indent(lines[j]) < own), 0)
    top = _indent(lines[start])
    end = next((j for j in range(start + 1, len(lines)) if lines[j].strip() and _indent(lines[j]) <= top), len(lines))
    return any(TESTS in command_kinds(_command_text(t)) for t in lines[start:end])


def _thresholds(lines, coverage_context) -> dict:
    found = {}
    for _n, text in lines:
        for rx in _THRESHOLDS:
            for key, value in rx.findall(text):
                found.setdefault(re.sub(r"^-+(?:cov-)?", "", key).replace("-", "_"), float(value))
        if coverage_context:
            for key, value in _COVERAGE_KEYS.findall(text):
                found.setdefault(key, float(value))
    return found


def _number(value) -> str:
    return ("%d" % value) if value == int(value) else ("%s" % value)


def _assertions(lines) -> int:
    return sum(1 for _n, t in lines if _ASSERT.search(_code(t)))


def _coverage_context(repo, f) -> bool:
    return "cov" in f.path.lower() or "coverage" in _read(repo, f.path).lower() or \
        any("cov" in t.lower() for _n, t in f.added + f.removed)


def _file_signals(repo, f, added_names, net_loss) -> list:
    out = []
    if f.status == "D":
        return [_signal("deleted-test-file", f.path, 0, "test file deleted")] if is_test_file(f.path) else []
    if is_test_file(f.path):
        gone = [n for n in _test_names(f.removed) if n not in added_names]
        new = [n for n in _test_names(f.added) if n not in _test_names(f.removed)]
        if len(gone) > 1 and new and any(_PARAMETRIZED.search(_code(t)) for _n, t in f.added):
            out.append(_signal("replaced-tests", f.path, 0,
                               "%d tests replaced by %s (parametrized)" % (len(gone), new[0])))
            gone = []
        else:
            removed, added = _assertions(f.removed), _assertions(f.added)
            if removed > added and net_loss:
                out.append(_signal("removed-assertions", f.path, 0, "%d assertion%s removed, %d added" % (
                    removed, "" if removed == 1 else "s", added)))
        out.extend(_signal("removed-test", f.path, 0, name) for name in gone)
        if f.status == "M":  # a new test file weakens nothing that existed; its guards are its own
            for table, kind in ((_SKIPS, "added-skip"), (_FOCUS, "added-focus")):
                rx = _pattern(table, f.path)
                if rx:
                    out.extend(_signal(kind, f.path, n, t.strip()) for n, t in _markers(f, rx))
    if _CI.search(f.path) or _SCRIPTS.search(f.path):
        removed_text = {t.strip() for _n, t in f.removed}
        for n, t in f.added:
            if t.strip() in removed_text:
                continue
            if _IGNORE_FAIL.search(t) and TESTS in command_kinds(re.split(r"\|\||;", _command_text(t))[0]):
                out.append(_signal("ignored-failure", f.path, n, t.strip()))
            elif _CI.search(f.path) and _ALLOW_FAIL.match(t) and _block_runs_tests(repo, f, n):
                out.append(_signal("ignored-failure", f.path, n, t.strip()))
            elif "--passWithNoTests" in t and '"test' in t:
                out.append(_signal("ignored-failure", f.path, n, t.strip()))
    return out


def _coverage_signals(repo, files) -> list:
    """Coverage thresholds compared across the whole diff by key, so a
    threshold moved to another file at the same or a higher value is fine."""
    before, after = {}, {}
    for f in files:
        context = _coverage_context(repo, f)
        for key, value in _thresholds(f.removed, context).items():
            before.setdefault(key, (value, f.path))
        for key, value in _thresholds(f.added, context).items():
            after[key] = max(after.get(key, value), value)
    out = []
    for key, (old, path) in before.items():
        if key not in after:
            out.append(_signal("lowered-coverage", path, 0, "%s %s removed" % (key, _number(old))))
        elif after[key] < old:
            out.append(_signal("lowered-coverage", path, 0, "%s %s to %s" % (key, _number(old), _number(after[key]))))
    return out


def find_signals(repo, base=None) -> dict:
    """Weakened-test signals in the working tree compared with `base` (from
    where the current branch left it), or with HEAD when no base is given."""
    repo = os.path.abspath(repo)
    top = _git(repo, "rev-parse", "--show-toplevel").strip() if os.path.isdir(repo) else None
    if not top:
        raise GitError("not a git repository: %s" % repo)
    if base:
        try:
            start = _git(top, "merge-base", base, "HEAD").strip()
        except GitError:
            raise GitError("unknown base ref: %s" % base)
    else:
        try:
            start = _git(top, "rev-parse", "--verify", "-q", "HEAD").strip()
        except GitError:
            start = EMPTY_TREE
    files = _parse_diff(_git(top, "diff", "--no-color", "--no-ext-diff", "-U0", "-M", start, "--"))
    files += _untracked(top)
    added_names, removed_total, added_total = set(), 0, 0
    for f in files:
        if is_test_file(f.path) and f.status != "D":
            added_names.update(_test_names(f.added))
            removed_total += _assertions(f.removed)
            added_total += _assertions(f.added)
    signals = []
    for f in files:  # assertions moved to another test file are a replacement, not a loss
        signals.extend(_file_signals(top, f, added_names, removed_total > added_total))
    signals.extend(_coverage_signals(top, files))
    return {"repo": top, "base": base or "HEAD", "files_changed": len(files), "signals": signals}
