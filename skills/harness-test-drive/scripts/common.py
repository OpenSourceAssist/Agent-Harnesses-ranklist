"""Helpers shared by mine_tasks.py and drive.py.

- safe_text() and code(): from safe.py, the shared helpers that make untrusted
  text (commit messages, file names, harness output) safe to show in a
  markdown report.
- classify(): sorts a repository path into test, doc, source, or other.
- make_workspace(): a fresh folder holding one commit's files as a new git
  repository with a single commit, so an agent working there cannot see later
  commits (the real fix) and cannot touch the real repository's branches.
- write_files(), run_command(), remove_workspace().

Only read-only git commands touch the user's repository (ls-tree, cat-file,
log, rev-parse). Python 3.9+, standard library only.
"""

from __future__ import annotations

import os
import re
import shutil
import signal
import subprocess
import tempfile
import time

from safe import code, safe_text  # noqa: F401  (also kept for callers that import them from here)

# --- File classes ------------------------------------------------------------------

_TEST_DIRS = {"test", "tests", "__tests__", "spec", "specs", "testdata", "__snapshots__", "__mocks__"}
_TEST_NAME_RE = re.compile(
    r"(^test_.*\.py$|_test\.py$|^conftest\.py$|_test\.go$|_spec\.rb$|_test\.exs$"
    r"|\.(test|spec)\.[cm]?[jt]sx?$"
    r"|(Test|Tests)\.(java|kt|scala|swift|php|cs)$"
    # test-runner settings: an agent that edits them could switch tests off
    r"|^(pytest\.ini|tox\.ini|noxfile\.py|phpunit\.xml(\.dist)?|\.mocharc(\.\w+)?|\.nycrc(\.\w+)?|\.rspec(-local)?)$"
    r"|^(jest|vitest|karma)\.conf(ig)?\.[cm]?[jt]s(on)?$|^(ava|playwright)\.config\.[cm]?[jt]s$)")
_DOC_DIRS = {"doc", "docs"}
_DOC_EXT = {".md", ".markdown", ".rst", ".txt", ".adoc"}
_DOC_NAME_RE = re.compile(r"^(LICENSE|LICENCE|COPYING|CHANGELOG|CHANGES|AUTHORS|NOTICE|CONTRIBUTORS)(\..*)?$", re.I)
_SOURCE_EXT = {
    ".py", ".pyi", ".js", ".jsx", ".mjs", ".cjs", ".ts", ".tsx", ".mts", ".cts", ".go", ".rs",
    ".java", ".kt", ".kts", ".scala", ".groovy", ".rb", ".php", ".c", ".h", ".cc", ".cpp", ".cxx",
    ".hpp", ".hh", ".cs", ".fs", ".swift", ".m", ".mm", ".dart", ".ex", ".exs", ".erl", ".hs",
    ".ml", ".clj", ".lua", ".pl", ".pm", ".r", ".jl", ".sh", ".bash", ".zsh", ".sql", ".vue",
    ".svelte", ".zig", ".nim", ".ps1",
}


_MANIFEST_RE = re.compile(
    r"^(package(-lock)?\.json|npm-shrinkwrap\.json|pnpm-lock\.yaml|yarn\.lock|bun\.lockb?|pyproject\.toml"
    r"|setup\.(py|cfg)|requirements[\w.-]*\.txt|Pipfile(\.lock)?|poetry\.lock|uv\.lock|go\.(mod|sum)"
    r"|Cargo\.(toml|lock)|Gemfile(\.lock)?|composer\.(json|lock))$")


def touches_manifest(paths) -> bool:
    """True when a path is a dependency manifest or lock file, so the setup
    command must run again after it changes."""
    return any(_MANIFEST_RE.match(p.rsplit("/", 1)[-1]) for p in paths)


def classify(path) -> str:
    """'test', 'doc', 'source', or 'other' for a repository path (forward slashes)."""
    parts = path.split("/")
    name = parts[-1]
    if _TEST_NAME_RE.search(name) or any(p in _TEST_DIRS for p in parts[:-1]):
        return "test"
    ext = os.path.splitext(name)[1].lower()
    if any(p.lower() in _DOC_DIRS for p in parts[:-1]) or ext in _DOC_EXT or _DOC_NAME_RE.match(name):
        return "doc"
    return "source" if ext in _SOURCE_EXT else "other"


# --- Git and workspaces --------------------------------------------------------------

# Files that tools create while an agent works or tests run; the diff ignores them.
_EXCLUDE = ["__pycache__/", "*.pyc", ".pytest_cache/", ".mypy_cache/", ".ruff_cache/", ".tox/",
            ".nox/", ".venv/", "venv/", "node_modules/", ".coverage", "coverage/", ".next/",
            ".turbo/", "target/", "dist/", "build/", "*.egg-info/", ".eggs/"]


def _read_env() -> dict:
    # No lock files, no password prompts, paths taken literally, and in a
    # partial clone no fetching of missing objects over the network.
    return dict(os.environ, GIT_OPTIONAL_LOCKS="0", GIT_TERMINAL_PROMPT="0", GIT_LITERAL_PATHSPECS="1",
                GIT_NO_LAZY_FETCH="1")


def git(repo, *args) -> bytes:
    """Run a read-only git command in the user's repository; return stdout."""
    res = subprocess.run(["git", "-C", repo] + list(args), env=_read_env(), stdin=subprocess.DEVNULL,
                         capture_output=True)
    if res.returncode != 0:
        raise RuntimeError("git %s failed: %s" % (args[0], code(res.stderr.decode("utf-8", "replace"))))
    return res.stdout


def _workspace_git(ws, *args, input=None) -> bytes:
    """git inside a workspace, ignoring the user's global config (hooks, signing)."""
    env = dict(os.environ, GIT_CONFIG_GLOBAL=os.devnull, GIT_CONFIG_NOSYSTEM="1", GIT_TERMINAL_PROMPT="0",
               GIT_LITERAL_PATHSPECS="1")
    feed = {"input": input} if input is not None else {"stdin": subprocess.DEVNULL}
    res = subprocess.run(["git", "-c", "user.name=harness-test-drive", "-c", "user.email=harness-test-drive@localhost"]
                         + list(args), cwd=ws, env=env, capture_output=True, **feed)
    if res.returncode != 0:
        raise RuntimeError("git %s failed in the workspace: %s"
                           % (args[0], code(res.stderr.decode("utf-8", "replace"))))
    return res.stdout


def _tree(repo, commit, paths=None) -> dict:
    """{path: (mode, type, object id)} for a commit, limited to `paths` when given."""
    args = ["ls-tree", "-r", "-z", "--full-tree", commit]
    if paths is not None:
        args += ["--"] + list(paths)
    entries = {}
    for rec in git(repo, *args).split(b"\0"):
        if rec:
            meta, _, path = rec.partition(b"\t")
            mode, kind, oid = meta.decode().split()
            entries[os.fsdecode(path)] = (mode, kind, oid)
    return entries


def _inside(path) -> bool:
    """True for a relative path that stays inside the workspace and out of .git."""
    parts = path.split("/")
    return not path.startswith("/") and ".." not in parts and ".git" not in (p.lower() for p in parts)


def _remove(target) -> None:
    if os.path.islink(target) or os.path.isfile(target):
        os.unlink(target)
    elif os.path.isdir(target):
        shutil.rmtree(target)


def _clear_path(dest, path) -> str:
    """Remove whatever stands at `path` inside `dest`, and any link or file where
    one of its parent folders belongs, so a write never follows a link an agent
    planted out of the workspace. Returns the full path."""
    current = dest
    for part in path.split("/")[:-1]:
        current = os.path.join(current, part)
        if os.path.islink(current) or (os.path.lexists(current) and not os.path.isdir(current)):
            os.unlink(current)
    target = os.path.join(dest, path)
    _remove(target)
    return target


def _write_entries(repo, entries, dest) -> None:
    blobs = [(p, mode, oid) for p, (mode, kind, oid) in sorted(entries.items()) if kind == "blob" and _inside(p)]
    if not blobs:
        return
    proc = subprocess.Popen(["git", "-C", repo, "cat-file", "--batch"], stdin=subprocess.PIPE,
                            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, env=_read_env())
    try:
        for path, mode, oid in blobs:
            proc.stdin.write(oid.encode() + b"\n")
            proc.stdin.flush()
            header = proc.stdout.readline().split()
            if len(header) != 3:  # "<id> missing": a partial or damaged clone
                raise RuntimeError("git object %s is not in this clone" % oid)
            size = int(header[2])
            data = proc.stdout.read(size)
            proc.stdout.read(1)  # the newline after each object
            target = _clear_path(dest, path)
            os.makedirs(os.path.dirname(target), exist_ok=True)
            if mode == "120000":
                os.symlink(os.fsdecode(data), target)
            else:
                with open(target, "wb") as fh:
                    fh.write(data)
                if mode == "100755":
                    os.chmod(target, 0o755)
    finally:
        proc.stdin.close()
        proc.stdout.close()
        proc.wait()


def write_files(repo, commit, paths, dest) -> None:
    """Set each path in `dest` to its state at `commit`: written when the commit
    has it, removed when it does not."""
    paths = [p for p in paths if _inside(p)]
    entries = _tree(repo, commit, paths)
    for path in paths:
        if path not in entries:
            _clear_path(dest, path)
    _write_entries(repo, entries, dest)


def make_workspace(repo, commit, root=None) -> str:
    """A new temporary folder with the files of `commit`, committed once as a
    fresh git repository. Later commits never reach it."""
    ws = tempfile.mkdtemp(prefix="harness-test-drive-", dir=root)
    try:
        _write_entries(repo, _tree(repo, commit), ws)
        _workspace_git(ws, "init", "-q")
        with open(os.path.join(ws, ".git", "info", "exclude"), "a") as fh:
            fh.write("\n".join(_EXCLUDE) + "\n")
        _workspace_git(ws, "add", "-A", "-f")
        _workspace_git(ws, "commit", "-q", "--no-verify", "--allow-empty", "-m", "Starting point for the task")
    except BaseException:
        remove_workspace(ws)
        raise
    return ws


def workspace_head(ws) -> str:
    return _workspace_git(ws, "rev-parse", "HEAD").decode().strip()


def changed_files(ws, base, patch_path=None) -> tuple:
    """(lines added plus deleted, paths) for everything in the workspace that
    differs from commit `base`: edits, new files, deletions, and commits the
    agent made. Tool caches listed in _EXCLUDE are left out. With `patch_path`,
    the full diff is saved there for review."""
    _workspace_git(ws, "add", "-A")
    if patch_path:
        with open(patch_path, "wb") as fh:
            fh.write(_workspace_git(ws, "diff", "--cached", "--binary", "--no-renames", base))
    lines, paths = 0, []
    for rec in _workspace_git(ws, "diff", "--cached", "--numstat", "-z", "--no-renames", base).split(b"\0"):
        if rec:
            added, deleted, path = rec.split(b"\t", 2)
            lines += sum(int(n) for n in (added, deleted) if n.isdigit())  # "-" for binary files
            paths.append(os.fsdecode(path))
    return lines, paths


def score_patch(ws, base, paths) -> bytes:
    """The staged diff from `base` of `paths` only; changed_files() staged it."""
    return _workspace_git(ws, "diff", "--cached", "--binary", "--no-renames", base, "--", *paths) if paths else b""


def reset_workspace(ws, base) -> None:
    """Put the copy back to commit `base`, deleting every untracked and ignored
    file, such as an edit inside node_modules."""
    _workspace_git(ws, "reset", "-q", "--hard", base)
    _workspace_git(ws, "clean", "-q", "-ffdx")


def apply_patch(ws, patch) -> None:
    if patch:
        _workspace_git(ws, "apply", "--binary", "--whitespace=nowarn", "-", input=patch)


def remove_workspace(path) -> None:
    shutil.rmtree(path, ignore_errors=True)
    if os.path.exists(path):  # an agent made a folder read-only
        for dirpath, _, _ in os.walk(path):
            os.chmod(dirpath, 0o700)
        shutil.rmtree(path, ignore_errors=True)


# --- Running commands ------------------------------------------------------------------

# Handles to the session that started this script. A harness, or repository code
# it runs, could use them to reach the user's live agent session.
_SESSION_VARS = {
    "CLAUDECODE", "AI_AGENT", "CLAUDE_PID", "CLAUDE_EFFORT", "CLAUDE_CODE_SESSION_ID", "CLAUDE_CODE_CHILD_SESSION",
    "CLAUDE_CODE_BRIDGE_SESSION_ID", "CLAUDE_CODE_ENTRYPOINT", "CLAUDE_CODE_HOST_SESSION_ID", "CLAUDE_CODE_EXECPATH",
    "CLAUDE_CODE_MESSAGING_SOCKET", "CLAUDE_CODE_MESSAGING_TOKEN", "CLAUDE_AGENT_SDK_VERSION",
    "CLAUDE_CODE_SDK_HAS_HOST_AUTH_REFRESH", "CLAUDE_CODE_SESSION_ATTENDED"}


def clean_env() -> dict:
    """This process's environment without the calling session's handles."""
    return {k: os.environ[k] for k in os.environ if k not in _SESSION_VARS and not k.startswith("CODEX_SANDBOX")}


def _raise_interrupt(signum, frame):
    raise KeyboardInterrupt


def install_stop_handlers() -> dict:
    """Make SIGINT, SIGTERM, and SIGHUP (a stopped background task, a closed
    terminal) raise KeyboardInterrupt, so every stop takes the path that ends
    the agent, records the run, and deletes the copy. Returns the old handlers."""
    old = {}
    for sig in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP):
        old[sig] = signal.signal(sig, _raise_interrupt)
    return old


def restore_handlers(old) -> None:
    for sig, handler in old.items():
        signal.signal(sig, handler)


def _groups_under(pid) -> set:
    """Process groups of `pid` and every process below it. A child can leave
    the group (Claude Code runs its shells in their own sessions), so killpg on
    the first group is not enough."""
    try:
        listing = subprocess.run(["ps", "-A", "-o", "pid=,ppid=,pgid="], capture_output=True, text=True,
                                 timeout=10, stdin=subprocess.DEVNULL).stdout
    except (OSError, subprocess.TimeoutExpired):
        return {pid}
    children, group = {}, {}
    for line in listing.splitlines():
        parts = line.split()
        if len(parts) == 3 and all(p.isdigit() for p in parts):
            child, parent, pgid = (int(p) for p in parts)
            children.setdefault(parent, []).append(child)
            group[child] = pgid
    found, stack, seen = {pid}, [pid], set()
    while stack:
        current = stack.pop()
        if current not in seen:
            seen.add(current)
            found.add(group.get(current, pid))
            stack.extend(children.get(current, []))
    return {g for g in found if g > 1 and g != os.getpgrp()}


def _signal_groups(groups, sig) -> None:
    for g in groups:
        try:
            os.killpg(g, sig)
        except (ProcessLookupError, PermissionError):
            pass


def _stop_group(proc) -> None:
    """End every process the command started: its process group, every group
    below it, and background children left behind after a normal exit."""
    groups = _groups_under(proc.pid) if proc.poll() is None else {proc.pid}
    _signal_groups(groups, signal.SIGTERM)
    try:
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        pass
    _signal_groups(groups, signal.SIGKILL)  # whatever ignored SIGTERM
    proc.wait()


def run_command(cmd, cwd, timeout, out_path=None, err_path=None, env=None) -> dict:
    """Run a shell command (a string) or a program (a list) in `cwd` with no
    input and, unless `env` is given, clean_env(). Output goes to `out_path` and
    `err_path` when given, else nowhere. On timeout every process it started is
    stopped. Returns exit, seconds, timed_out."""
    out = open(out_path, "wb") if out_path else subprocess.DEVNULL
    err = open(err_path, "wb") if err_path else (subprocess.STDOUT if out_path else subprocess.DEVNULL)
    started = time.monotonic()
    timed_out = False
    try:
        proc = subprocess.Popen(cmd, shell=isinstance(cmd, str), cwd=cwd, stdin=subprocess.DEVNULL,
                                stdout=out, stderr=err, start_new_session=True,
                                env=clean_env() if env is None else env)
        try:
            proc.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            timed_out = True
        except BaseException:  # Ctrl-C: never leave an agent running on its own
            _stop_group(proc)
            raise
        _stop_group(proc)
    finally:
        for fh in (out, err):
            if hasattr(fh, "close"):
                fh.close()
    return {"exit": None if timed_out else proc.returncode,
            "seconds": round(time.monotonic() - started, 2), "timed_out": timed_out}


def duration(seconds) -> str:
    """'45 s', '3 min 5 s', '2 h 4 min'."""
    seconds = int(round(seconds or 0))
    if seconds < 60:
        return "%d s" % seconds
    if seconds < 3600:
        return "%d min %d s" % (seconds // 60, seconds % 60)
    return "%d h %d min" % (seconds // 3600, seconds % 3600 // 60)


def run_tests(cmd, cwd, timeout, out_path=None) -> dict:
    """run_command for the test command with no Python bytecode from earlier
    runs: Python trusts a cached file when the source has the same size and
    modified second, so a restored test or gold file written right after an
    earlier run could otherwise run as its old version. The workspace's
    __pycache__ folders are deleted, and PYTHONPYCACHEPREFIX points Python 3.8
    and later at a new empty folder, also on interpreters that keep bytecode
    elsewhere (Apple's /usr/bin/python3 uses ~/Library/Caches). The folder is
    deleted after the run."""
    for dirpath, dirnames, _ in os.walk(cwd):
        dirnames[:] = [d for d in dirnames if d not in (".git", "node_modules", ".venv", "venv")]
        if "__pycache__" in dirnames:
            dirnames.remove("__pycache__")
            shutil.rmtree(os.path.join(dirpath, "__pycache__"), ignore_errors=True)
    prefix = tempfile.mkdtemp(prefix="harness-test-drive-pycache-")
    try:
        return run_command(cmd, cwd, timeout, out_path, env=dict(clean_env(), PYTHONPYCACHEPREFIX=prefix))
    finally:
        shutil.rmtree(prefix, ignore_errors=True)
