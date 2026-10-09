"""Simulate Claude Code permission rules, permission modes, and hook matching.

Everything here follows the documented behavior (fetched 2026-09-28):
  https://code.claude.com/docs/en/permissions
  https://code.claude.com/docs/en/permission-modes
  https://code.claude.com/docs/en/hooks
  https://code.claude.com/docs/en/settings and /settings-reference
  https://code.claude.com/docs/en/sandboxing
Results are labeled "simulated from the documented rules": Claude Code's own
parser can differ on commands the docs do not cover. Where the docs are silent,
the simulation takes the reading that claims less protection, and
references/harness-rules.md says so.

Nothing here runs a command. Run with --help to print this text.
Python 3.9+, standard library only.
"""

from __future__ import annotations

import fnmatch
import glob as globmod
import json
import os
import re
import shlex
import sys
from dataclasses import dataclass, field
from typing import Optional

import shell_split
from safe import code

MODES =("default", "acceptEdits", "plan", "auto", "dontAsk", "bypassPermissions")
MODE_ALIASES = {"manual": "default"}
# The docs name these as safe to strip before allow rules; the full list is internal.
SAFE_ENV = frozenset({"NODE_ENV", "LANG", "NO_COLOR"})
WRAPPERS = frozenset({"timeout", "time", "nice", "nohup", "stdbuf", "command", "builtin", "noglob"})
READ_ONLY = frozenset({"ls", "cat", "echo", "pwd", "head", "tail", "grep", "find", "wc", "which",
                       "diff", "stat", "du", "cd"})
GIT_READ_ONLY = frozenset({"status", "diff", "log", "show", "blame", "rev-parse", "ls-files", "ls-tree",
                           "describe", "shortlog", "grep", "cat-file", "merge-base", "rev-list",
                           "show-ref", "for-each-ref", "count-objects", "check-ignore", "name-rev",
                           "whatchanged", "show-branch", "cherry", "version", "help"})
FIND_WRITES = frozenset({"-exec", "-execdir", "-ok", "-okdir", "-delete", "-fprint", "-fprint0",
                         "-fprintf", "-fls"})
GLOB_WRITE_CAPABLE = frozenset({"find", "sort", "sed", "git"})
FS_COMMANDS = frozenset({"mkdir", "touch", "rm", "rmdir", "mv", "cp", "sed"})
# The permissions page: "Exec wrappers such as watch, setsid, ionice, and flock can't be
# auto-approved by a prefix rule"; the same holds for find with -exec or -delete.
EXEC_WRAPPERS = frozenset({"watch", "setsid", "ionice", "flock"})
READ_FAMILY = frozenset({"Read", "Grep", "Glob", "NotebookRead", "LS"})
EDIT_FAMILY = frozenset({"Edit", "MultiEdit", "Write", "NotebookEdit"})
IGNORED_PATH_TOOLS = {"Write": "Edit", "NotebookEdit": "Edit", "MultiEdit": "Edit", "Glob": "Read"}
PRIMARY_FIELD = {"Bash": "command", "PowerShell": "command", "Read": "file_path", "Edit": "file_path",
                 "Write": "file_path", "Grep": "path", "Glob": "path", "NotebookEdit": "notebook_path",
                 "WebFetch": "url"}
KNOWN_PARAMS = {
    "Bash": {"description", "timeout", "run_in_background", "dangerouslyDisableSandbox"},
    "PowerShell": {"description", "timeout", "run_in_background", "dangerouslyDisableSandbox"},
    "Agent": {"model", "isolation", "subagent_type", "description", "prompt", "run_in_background", "name"},
    "Task": {"model", "isolation", "subagent_type", "description", "prompt", "run_in_background", "name"},
    "Read": {"offset", "limit"},
    "Edit": {"old_string", "new_string", "replace_all"},
    "Write": {"content"},
    "Grep": {"pattern", "glob", "output_mode", "type", "multiline", "head_limit"},
    "Glob": {"pattern"},
    "WebFetch": {"prompt"},
    "WebSearch": {"query", "allowed_domains", "blocked_domains"},
}
PROTECTED_DIRS = frozenset({".git", ".vscode", ".idea", ".husky", ".cargo", ".devcontainer", ".yarn",
                            ".mvn", ".claude"})
PROTECTED_FILES = frozenset({
    ".gitconfig", ".gitmodules", ".bashrc", ".bash_profile", ".bash_login", ".bash_aliases",
    ".bash_logout", ".zshrc", ".zprofile", ".zshenv", ".zlogin", ".zlogout", ".profile", ".envrc",
    ".npmrc", ".yarnrc", ".yarnrc.yml", ".pnp.cjs", ".pnp.loader.mjs", ".pnpmfile.cjs", "bunfig.toml",  # skillscan:allow (the documented protected-path list)
    ".bunfig.toml", ".bazelrc", ".bazelversion", ".bazeliskrc", ".pre-commit-config.yaml",
    "lefthook.yml", "lefthook.yaml", ".lefthook.yml", ".lefthook.yaml", "gradle-wrapper.properties",
    "maven-wrapper.properties", ".devcontainer.json", ".ripgreprc", "pyrightconfig.json", ".mcp.json",
    ".claude.json"})
TOP_LEVEL_NAMES = frozenset({"bin", "boot", "dev", "etc", "home", "lib", "lib64", "mnt", "opt", "private",
                             "proc", "root", "run", "sbin", "srv", "sys", "tmp", "usr", "var", "Users",
                             "Applications", "Library", "System", "Volumes", "data"})
SETTINGS_SOURCES = frozenset({"managed", "user", "project", "local"})
LAYER_ORDER = ("managed", "local", "project", "user")        # highest precedence first
if sys.platform == "darwin":
    MANAGED_DIRS = ["/Library/Application Support/ClaudeCode"]
elif sys.platform.startswith("linux"):
    MANAGED_DIRS = ["/etc/claude-code"]
else:
    MANAGED_DIRS = []


# ---------------------------------------------------------------------------
# Data shapes
# ---------------------------------------------------------------------------

@dataclass
class Rule:
    raw: str
    list_name: str = "deny"       # allow | ask | deny (or "if" for a hook's if field)
    source: str = "project"
    source_root: str = ""         # where a "/path" pattern anchors
    tool: str = ""
    spec: Optional[str] = None    # None for a bare tool name
    kind: str = "tool"            # tool, glob, bash, path, param, domain, agent, mcp, other
    negated: bool = False
    param: tuple = ()
    ignored: str = ""             # why Claude Code never applies this rule, when it does not


@dataclass
class Result:
    verdict: str                  # deny | ask | allow | classifier | unknown
    layer: str                    # hook | rule | built-in | mode | sandbox
    detail: str
    rule: str = ""
    source: str = ""
    mode_only: Optional[bool] = None   # an ask that only the permission mode causes (None: layer == "mode")


@dataclass
class HookDecision:
    decision: Optional[str] = None   # allow | deny | ask | None
    reason: str = ""
    source: str = ""


@dataclass
class HookSpec:
    event: str
    matcher: Optional[str]
    handler: dict
    source: str                      # managed | local | project | user | plugin:<name>
    source_path: str = ""
    plugin_root: str = ""

    @property
    def type(self):
        return str(self.handler.get("type") or "command")

    @property
    def command(self):
        return self.handler.get("command") if isinstance(self.handler.get("command"), str) else ""

    @property
    def args(self):
        args = self.handler.get("args")
        return [str(a) for a in args] if isinstance(args, list) else None

    @property
    def timeout(self):
        t = self.handler.get("timeout")
        return t if isinstance(t, (int, float)) and not isinstance(t, bool) and t > 0 else None

    @property
    def is_async(self):
        return bool(self.handler.get("async") or self.handler.get("asyncRewake"))

    @property
    def if_rule(self):
        value = self.handler.get("if")
        return value if isinstance(value, str) and value.strip() else ""


@dataclass
class Layer:
    name: str
    path: str
    found: bool = False
    error: str = ""
    data: dict = field(default_factory=dict)


@dataclass
class Config:
    cwd: str
    home: str
    rules: dict = field(default_factory=lambda: {"allow": [], "ask": [], "deny": []})
    mode: str = "default"
    mode_source: str = "built-in default"
    additional_dirs: list = field(default_factory=list)
    sandbox: dict = field(default_factory=dict)
    layers: list = field(default_factory=list)
    hooks: list = field(default_factory=list)          # PreToolUse hooks that would run
    skipped_hooks: list = field(default_factory=list)  # (HookSpec, why it never runs)
    disable_bypass: bool = False
    auto_available: bool = True        # False when any settings file sets disableAutoMode to "disable"
    plan_uses_auto: bool = True        # useAutoModeDuringPlan (user, local, or managed settings)
    block_outside_reads: bool = False  # permissions.blockReadsOutsideWorkingDirectories
    sandbox_strict: bool = False       # strictAllowlist (user or managed) or allowManagedDomainsOnly
    sandbox_fs_disabled: bool = False  # sandbox.filesystem.disabled (user or managed)
    smells: list = field(default_factory=list)
    notes: list = field(default_factory=list)
    claude_dir: str = ""


@dataclass
class Sub:
    cmd: object                   # shell_split.Command
    words: list                   # after stripping assignments and wrappers (deny view)
    values: list
    deny_text: str
    allow_text: str


@dataclass
class FileOp:
    kind: str                     # read | write
    path: str                     # absolute, or "" when it cannot be resolved
    how: str                      # redirect | tee | cat | head | tail | sed
    raw: str
    needs_approval: bool = False  # starts with ~ or holds a glob or variable


@dataclass
class BashFacts:
    raw: str
    parsed: object
    unparseable: bool
    subs: list
    reads: list
    writes: list


# ---------------------------------------------------------------------------
# Rule parsing
# ---------------------------------------------------------------------------

_PARAM_RE = re.compile(r"^\s*([A-Za-z_][A-Za-z0-9_-]*)\s*:\s*(.*?)\s*$", re.S)


def parse_rule(text, list_name="deny", source="project", source_root="") -> Rule:
    text = str(text).strip()
    m = re.match(r"^([^()]*?)\s*\((.*)\)\s*$", text, re.S)
    tool, spec = (m.group(1).strip(), m.group(2)) if m else (text, None)
    rule = Rule(raw=text, list_name=list_name, source=source, source_root=source_root, tool=tool, spec=spec)
    if tool.startswith("mcp__"):
        rule.kind = "mcp"
        if spec is not None and source in SETTINGS_SOURCES:
            rule.ignored = "settings files skip any mcp__ rule with parentheses"
        elif "*" in tool and list_name == "allow" and not re.match(r"^mcp__[^*_][^*]*?__", tool):
            rule.ignored = "allow rules accept tool-name globs only after a literal mcp__<server>__ prefix"
        return rule
    if spec is None:
        if "*" in tool or "?" in tool:
            rule.kind = "glob"
            if list_name == "allow":
                rule.ignored = "allow rules accept tool-name globs only after a literal mcp__<server>__ prefix"
        return rule
    pm = _PARAM_RE.match(spec)
    if pm and list_name in ("deny", "ask") and tool in KNOWN_PARAMS | PRIMARY_FIELD.keys():
        name, value = pm.group(1), pm.group(2)
        if name == PRIMARY_FIELD.get(tool):
            rule.kind, rule.param = "param", (name, value)
            rule.ignored = "a parameter rule cannot match the %s field; use %s(...) instead" % (name, tool)
            return rule
        if name in KNOWN_PARAMS.get(tool, ()):
            rule.kind, rule.param = "param", (name, value)
            return rule
    if tool in ("Bash", "PowerShell"):
        if spec.strip() in ("*", ""):
            rule.spec = None
            return rule
        rule.kind = "bash"
        rule.spec = spec[:-2] + " *" if spec.endswith(":*") else spec
        return rule
    if tool in ("Read", "Edit"):
        rule.kind = "path"
        rule.negated = spec.startswith("!")
        return rule
    if tool in IGNORED_PATH_TOOLS:
        rule.kind = "path"
        rule.ignored = "Claude Code never consults %s(path) rules; write %s instead" % (
            tool, code("%s(%s)" % (IGNORED_PATH_TOOLS[tool], spec)))
        return rule
    if tool == "WebFetch" and spec.strip().lower().startswith("domain:"):
        rule.kind, rule.spec = "domain", spec.strip()[len("domain:"):].strip()
        return rule
    if tool in ("Agent", "Task"):
        rule.kind = "agent"
        return rule
    rule.kind = "other"
    return rule


# ---------------------------------------------------------------------------
# Bash matching
# ---------------------------------------------------------------------------

def bash_pattern_matches(spec, text) -> bool:
    """Claude Code Bash pattern: `*` matches any text; a trailing ` *` that is
    the only wildcard also matches the bare command; no `*` means exact."""
    if "*" not in spec:
        return text == spec
    regex = "".join(".*" if ch == "*" else re.escape(ch) for ch in spec)
    if re.fullmatch(regex, text, re.S):
        return True
    return spec.endswith(" *") and spec.count("*") == 1 and text == spec[:-2]


def _strip_wrappers(words, values):
    i, n = 0, len(values)
    while i < n and words[i] == values[i]:
        w = values[i]
        j = i + 1
        if w == "timeout":
            while j < n and values[j].startswith("-"):
                opt = values[j]
                j += 1
                if opt in ("-s", "--signal", "-k", "--kill-after") and j < n:
                    j += 1
            j += 1                                   # the duration
        elif w == "nice":
            while j < n and values[j].startswith("-"):
                opt = values[j]
                j += 1
                if opt in ("-n", "--adjustment") and j < n:
                    j += 1
        elif w == "stdbuf":
            while j < n and values[j].startswith("-"):
                opt = values[j]
                j += 1
                if opt in ("-i", "-o", "-e", "--input", "--output", "--error") and j < n:
                    j += 1
        elif w == "time":
            while j < n and values[j].startswith("-"):
                j += 1
        elif w == "command":
            if j < n and values[j] in ("-v", "-V"):
                break
            while j < n and values[j] == "-p":
                j += 1
        elif w in ("nohup", "builtin", "noglob"):
            pass
        elif w == "xargs":
            if j < n and not values[j].startswith("-"):
                pass
            else:
                break
        else:
            break
        if j >= n:
            break
        i = j
    return words[i:], values[i:]


def _sub(cmd) -> Sub:
    words, values = _strip_wrappers(cmd.words, cmd.values)
    names = [a.split("=", 1)[0].rstrip("+") for a in cmd.assignments]
    if all(n in SAFE_ENV for n in names):
        allow_text = " ".join(words)
    else:
        allow_text = " ".join(cmd.assignments + cmd.words)
    return Sub(cmd=cmd, words=words, values=values, deny_text=" ".join(words), allow_text=allow_text)


def _expand(path, cwd, home):
    if path == "~":
        path = home
    elif path.startswith("~/"):
        path = os.path.join(home, path[2:])
    if not os.path.isabs(path):
        path = os.path.join(cwd, path)
    return os.path.normpath(path)


def _file_op(kind, target, how, cwd, home) -> FileOp:
    unresolved = "$" in target or "`" in target or any(c in target for c in "*?[")
    op = FileOp(kind=kind, path="" if unresolved else _expand(target, cwd, home), how=how, raw=target,
                needs_approval=unresolved or target.startswith("~"))
    return op


def _cmd_file_args(name, values):
    """(reads, writes) of file arguments for the commands the docs name."""
    args = values[1:]
    if name == "cat":
        files = [a for a in args if not a.startswith("-")]
        return files, []
    if name in ("head", "tail"):
        files, skip = [], False
        for a in args:
            if skip:
                skip = False
                continue
            if a in ("-n", "-c", "--lines", "--bytes"):
                skip = True
                continue
            if a.startswith("-"):
                continue
            files.append(a)
        return files, []
    if name == "sed":
        in_place, script_given, rest, i = False, False, [], 0
        while i < len(args):
            a = args[i]
            if a in ("-e", "--expression", "-f", "--file"):
                script_given = True
                i += 2
                continue
            if a == "-i" or a.startswith("--in-place"):
                in_place = True
                if a == "-i" and i + 1 < len(args) and args[i + 1] == "":
                    i += 1
                i += 1
                continue
            if a.startswith("-i"):
                in_place = True
                i += 1
                continue
            if a.startswith("-") and a != "-":
                i += 1
                continue
            rest.append(a)
            i += 1
        files = rest if script_given else rest[1:]
        return files, (files if in_place else [])
    if name == "tee":
        return [], [a for a in args if not a.startswith("-")]
    if name == "grep":
        return _grep_files(args), []
    if name in OPERAND_COMMANDS:
        return _operands(args, OPERAND_COMMANDS[name]), []
    return [], []


# Options that take a value, per read-only command whose other arguments are files.
OPERAND_COMMANDS = {
    "wc": frozenset({"--files0-from"}),
    "diff": frozenset({"-U", "-C", "-I", "-x", "-X", "-S", "-L", "-F", "-W", "-D", "--label", "--exclude",
                       "--exclude-from", "--ignore-matching-lines", "--starting-file", "--width", "--ifdef"}),
    "stat": frozenset({"-f", "-c", "--format", "--printf"}),
}
_GREP_VALUE = "efmABCdD"
_GREP_LONG_VALUE = frozenset({"--regexp", "--file", "--max-count", "--after-context", "--before-context",
                              "--context", "--include", "--exclude", "--exclude-dir", "--label", "--binary-files",
                              "--devices", "--directories", "--color", "--colour"})


def _operands(args, value_opts):
    files, i, done = [], 0, False
    while i < len(args):
        a = args[i]
        if not done and a == "--":
            done = True
        elif not done and a in value_opts:
            i += 1
        elif done or not a.startswith("-") or a == "-":
            files.append(a)
        i += 1
    return files


def _grep_files(args):
    """grep's file operands: every argument after the pattern that is not an
    option (the pattern is the first operand unless -e or -f gave one)."""
    rest, given, i, done = [], False, 0, False
    while i < len(args):
        a = args[i]
        if done or not a.startswith("-") or a == "-":
            rest.append(a)
        elif a == "--":
            done = True
        elif a.startswith("--"):
            name = a.split("=", 1)[0]
            given = given or name in ("--regexp", "--file")
            if name in _GREP_LONG_VALUE and "=" not in a:
                i += 1
        else:
            for k, ch in enumerate(a[1:], 1):
                if ch in _GREP_VALUE:
                    given = given or ch in "ef"
                    if k == len(a) - 1:
                        i += 1
                    break
        i += 1
    return rest if given else rest[1:]


def analyze_bash(command, cwd, home) -> BashFacts:
    command = command if isinstance(command, str) else ""
    parsed = shell_split.parse(command)
    unparseable = bool(parsed.error or parsed.dangling or len(command) > 10000)
    subs, reads, writes = [], [], []
    if unparseable:
        stripped = command.strip()
        subs = [Sub(cmd=shell_split.Command(words=[stripped], values=[stripped]), words=[stripped],
                    values=[stripped], deny_text=stripped, allow_text=stripped)]
        return BashFacts(command, parsed, True, subs, reads, writes)
    for cmd in parsed.commands:
        sub = _sub(cmd)
        subs.append(sub)
        name = sub.values[0] if sub.values and sub.words[0] == sub.values[0] else ""
        file_reads, file_writes = _cmd_file_args(name, sub.values)
        reads.extend(_file_op("read", f, name, cwd, home) for f in file_reads)
        writes.extend(_file_op("write", f, name, cwd, home) for f in file_writes)
        for op, target in cmd.file_redirects():
            if target == "/dev/null" or target.startswith("/dev/fd/") or target in ("/dev/stdout", "/dev/stderr"):
                continue
            if op == ">":
                writes.append(_file_op("write", target, "redirect", cwd, home))
            else:
                reads.append(_file_op("read", target, "redirect", cwd, home))
    return BashFacts(command, parsed, False, subs, reads, writes)


def _bash_list_hit(rule, facts, list_name):
    if list_name == "allow":
        return all(bash_pattern_matches(rule.spec, s.allow_text) for s in facts.subs) and not facts.unparseable
    return any(bash_pattern_matches(rule.spec, s.deny_text) for s in facts.subs)


def bash_rule_matches(rule_text, command, list_name="deny", cwd="/", home="/") -> bool:
    """One Bash rule against one command: for allow rules every subcommand must
    match; for deny and ask rules any subcommand is enough."""
    rule = parse_rule(rule_text, list_name, source="cli")
    facts = analyze_bash(command, cwd, home)
    if rule.ignored:
        return False
    if rule.kind == "tool" and rule.tool == "Bash":
        return list_name != "allow" or not facts.unparseable
    if rule.kind != "bash":
        return False
    return _bash_list_hit(rule, facts, list_name)


# ---------------------------------------------------------------------------
# Paths (gitignore-style, with Claude Code's anchors)
# ---------------------------------------------------------------------------

def _gi_regex(body, list_name="deny"):
    """A gitignore pattern as a regex. A pattern Python cannot compile matches
    its own text literally in deny and ask rules and matches nothing in allow
    rules, so a broken pattern never widens what runs."""
    try:
        return _gi_compile(body)
    except re.error:
        return re.compile(re.escape(body) if list_name in ("deny", "ask") else r"(?!)")


def _gi_compile(body):
    out, i, n = [], 0, len(body)
    while i < n:
        if body.startswith("**/", i):
            out.append("(?:.*/)?")
            i += 3
            continue
        if body.startswith("/**", i) and i + 3 == n:
            out.append("/.*")
            i += 3
            continue
        if body.startswith("**", i):
            out.append(".*")
            i += 2
            continue
        c = body[i]
        if c == "*":
            out.append("[^/]*")
        elif c == "?":
            out.append("[^/]")
        elif c == "[":
            j = body.find("]", i + 1)
            if j < 0:
                out.append(re.escape(c))
            else:
                inner = body[i + 1:j]
                if inner.startswith("!"):
                    inner = "^" + inner[1:]
                out.append("[" + inner.replace("\\", "\\\\") + "]")
                i = j
        elif c == "\\" and i + 1 < n:
            out.append(re.escape(body[i + 1]))
            i += 1
        else:
            out.append(re.escape(c))
        i += 1
    return re.compile("".join(out), re.S)


def _gi_match(pattern, rel, list_name="deny"):
    """(matched, via a parent directory) for a gitignore pattern and a relative path."""
    anchored = pattern.startswith("/")
    body = pattern.lstrip("/")
    dir_only = body.endswith("/")
    body = body.rstrip("/")
    if not body:
        return False, False
    if "/" in body:
        anchored = True
    regex = _gi_regex(body, list_name)
    parts = [p for p in rel.split("/") if p]
    for i in range(1, len(parts) + 1):
        last = i == len(parts)
        if dir_only and last:
            continue
        target = "/".join(parts[:i]) if anchored else parts[i - 1]
        if regex.fullmatch(target):
            return True, not last
    return False, False


def _source_root(source, cwd, home, claude_dir=""):
    if source == "user":
        return claude_dir or os.path.join(home, ".claude")
    if source == "managed":
        return MANAGED_DIRS[0] if MANAGED_DIRS else cwd
    return cwd


def _anchor(spec, rule, cwd, home, list_name):
    """(base folder, gitignore pattern) for a Read or Edit specifier."""
    if spec.startswith("//"):
        return "/", spec[1:]
    if spec == "~":
        return home, "/**"
    if spec.startswith("~/"):
        return home, spec[1:]
    if spec.startswith("/"):
        return rule.source_root or cwd, spec
    if spec.startswith("./"):
        return cwd, spec[1:]
    if list_name in ("deny", "ask") and re.fullmatch(r"[^/]+/\*\*", spec):
        return cwd, "**/" + spec                      # single-segment folder: any depth
    return cwd, spec


def _rel(base, path):
    rel = os.path.relpath(path, base) if base != "/" else path.lstrip("/")
    if rel == "." or rel.startswith(".."):
        return None
    return rel.replace(os.sep, "/")


def _path_match(rule, path, cwd, home, list_name):
    base, pattern = _anchor(rule.spec, rule, cwd, home, list_name)
    paths = [path]
    bases = [base]
    if list_name in ("deny", "ask"):
        real_path, real_base = os.path.realpath(path), os.path.realpath(base)
        if real_path != path:
            paths.append(real_path)
        if real_base != base:
            bases.append(real_base)
    for b in bases:
        for p in paths:
            rel = _rel(b, p)
            if rel is None:
                continue
            hit, via_parent = _gi_match(pattern, rel, list_name)
            if hit:
                return True, via_parent
    return False, False


def _is_relative_spec(spec):
    return not (spec.startswith("/") or spec.startswith("~"))


def _negation_matches(rule, path, cwd):
    rel = _rel(cwd, path)
    return rel is not None and _gi_match(rule.spec[1:], rel, "allow")[0]


def _source_hit(rules, family, path, cwd, home, list_name):
    """The rule from one settings source that decides `path`, honoring `!` carve-outs."""
    state, carvable = None, False
    for rule in rules:
        if rule.kind != "path" or rule.tool != family or rule.ignored:
            continue
        if rule.negated:
            if state is not None and carvable and _negation_matches(rule, path, cwd):
                state = None
            continue
        hit, via_parent = _path_match(rule, path, cwd, home, list_name)
        if hit:
            state, carvable = rule, _is_relative_spec(rule.spec) and not via_parent
    return state


def _path_list_hit(rules, family, path, cwd, home, list_name):
    by_source = {}
    for rule in rules:
        by_source.setdefault(rule.source, []).append(rule)
    for group in by_source.values():
        hit = _source_hit(group, family, path, cwd, home, list_name)
        if hit is not None:
            return hit
    return None


def path_rule_matches(rule_text, path, list_name="deny", cwd="/", home="/", source="project") -> bool:
    rule = parse_rule(rule_text, list_name, source, _source_root(source, cwd, home))
    if rule.kind != "path" or rule.ignored or rule.negated:
        return False
    return _path_match(rule, path, cwd, home, list_name)[0]


def path_denied(rules_by_source, path, cwd, home, family="Read", list_name="deny"):
    """The raw text of the rule that blocks `path`, or None (tests and reports)."""
    for source, texts in rules_by_source.items():
        rules = [parse_rule(t, list_name, source, _source_root(source, cwd, home)) for t in texts]
        hit = _source_hit(rules, family, path, cwd, home, list_name)
        if hit is not None:
            return hit.raw
    return None


# ---------------------------------------------------------------------------
# Other rule kinds
# ---------------------------------------------------------------------------

_HOST_RE = re.compile(r"^[A-Za-z][A-Za-z0-9+.-]*://(?:[^@/?#]*@)?(\[[^\]]*\]|[^:/?#]*)")


def _host(url):
    """The host name of a URL, lowercased, without a trailing dot."""
    m = _HOST_RE.match(url.strip()) if isinstance(url, str) else None
    return m.group(1).strip("[]").lower().rstrip(".") if m else ""


def _domain_matches(pattern, url):
    host = _host(url)
    pattern = pattern.lower().rstrip(".")
    if not host:
        return False
    if pattern == "*":
        return True
    if pattern.startswith("*."):
        return host.endswith(pattern[1:])
    regex = "".join("[^.]*" if ch == "*" else re.escape(ch) for ch in pattern)
    return re.fullmatch(regex, host) is not None


def _param_value(value):
    if isinstance(value, bool):
        return "true" if value else "false"
    if value is None:
        return ""
    return value if isinstance(value, str) else json.dumps(value)


def _tool_names(tool):
    return {"Agent", "Task"} if tool in ("Agent", "Task") else {tool}


def _rule_hits(rule, tool, tool_input, list_name):
    """Every rule kind except path and bash, which need the call's facts."""
    kind = rule.kind
    if kind == "tool":
        if rule.tool == "Read":
            return tool in READ_FAMILY
        if rule.tool == "Edit":
            return tool in EDIT_FAMILY
        return tool in _tool_names(rule.tool)
    if kind == "glob":
        return fnmatch.fnmatchcase(tool, rule.tool)
    if kind == "mcp":
        if "*" in rule.tool:
            return fnmatch.fnmatchcase(tool, rule.tool)
        if rule.tool.count("__") == 1:
            return tool.startswith(rule.tool + "__")
        return tool == rule.tool
    if kind == "param":
        name, pattern = rule.param
        if tool not in _tool_names(rule.tool) or not isinstance(tool_input, dict) or name not in tool_input:
            return False
        value = _param_value(tool_input[name])
        return bash_pattern_matches(pattern, value) if "*" in pattern else value == pattern
    if kind == "domain":
        return tool == "WebFetch" and _domain_matches(rule.spec, (tool_input or {}).get("url"))
    if kind == "agent":
        return tool in ("Agent", "Task") and (tool_input or {}).get("subagent_type") == rule.spec
    return False


def tool_rule_matches(rule_text, tool, tool_input, list_name="deny", source="cli", cwd="/", home="/") -> bool:
    rule = parse_rule(rule_text, list_name, source)
    if rule.ignored:
        return False
    if rule.kind == "bash":
        return tool in ("Bash", "PowerShell") and _bash_list_hit(
            rule, analyze_bash((tool_input or {}).get("command"), cwd, home), list_name)
    if rule.kind == "path":
        path = (tool_input or {}).get(PRIMARY_FIELD.get(tool, "file_path"))
        return isinstance(path, str) and _path_match(rule, path, cwd, home, list_name)[0]
    return _rule_hits(rule, tool, tool_input, list_name)


# ---------------------------------------------------------------------------
# Built-in checks: protected paths, critical-path removals, read-only commands
# ---------------------------------------------------------------------------

def is_protected(path) -> bool:
    parts = [p for p in os.path.normpath(path).split("/") if p]
    for i, part in enumerate(parts):
        if part == ".claude" and i + 1 < len(parts) and parts[i + 1] == "worktrees":
            continue
        if part in PROTECTED_DIRS:
            return True
        if part == ".config" and i + 1 < len(parts) and parts[i + 1] == "git":
            return True
    return bool(parts) and parts[-1] in PROTECTED_FILES


_VAR_TARGET = re.compile(r'^\$(\{[^}]*\}|[A-Za-z_][A-Za-z0-9_]*)(.*)$', re.S)
_SUBST_ONLY = re.compile(r'^"?(\$\([^)]*\)|`[^`]*`)"?$')
_TRAILING_SUBST = re.compile(r'(\$\([^)]*\)|`[^`]*`)$')


def _critical_target(raw, value, recursive, cwd, home):
    if recursive and _SUBST_ONLY.match(raw):
        return True
    unq = raw.replace('"', "")
    m = _VAR_TARGET.match(unq)
    if m:
        name, rest = m.group(1), m.group(2)
        if name.startswith("{") and ":?" in name:
            return False
        if rest in ("/", "/*") or rest.startswith("/*"):
            return True
        parts = [p for p in rest.split("/") if p]
        return len(parts) == 1 and parts[0] in TOP_LEVEL_NAMES
    value = _TRAILING_SUBST.sub("", value)
    if "$" in value or "`" in value:
        return False
    glob_under = value == "*" or value.endswith("/*")
    if glob_under:
        value = value[:-1] or "."
    elif any(c in value for c in "*?["):
        return False
    path = _expand(value, cwd, home) if value else home
    if path == "/" or os.path.dirname(path) == "/":
        return True
    if path == os.path.normpath(home):
        return True
    return path == cwd or cwd.startswith(path.rstrip("/") + "/")


def _critical_in(facts, cwd, home):
    for sub in facts.subs:
        if not sub.values or sub.words[0] != sub.values[0] or sub.values[0] not in ("rm", "rmdir"):
            continue
        recursive, done_opts = False, False
        for raw, value in zip(sub.words[1:], sub.values[1:]):
            if not done_opts and value == "--":
                done_opts = True
                continue
            if not done_opts and value.startswith("-") and len(value) > 1:
                recursive = recursive or value == "--recursive" or (
                    not value.startswith("--") and ("r" in value or "R" in value))
                continue
        for raw, value in zip(sub.words[1:], sub.values[1:]):
            if value.startswith("-") and len(value) > 1:
                continue
            if _critical_target(raw, value, recursive, cwd, home):
                return True
    return False


# The docs name cat, head, tail, sed, and tee as examples of the file commands
# Claude Code recognizes; the read-only commands grep, wc, diff, and stat name
# their files too, so the simulation treats them the same way.
FILE_COMMANDS = ("cat", "head", "tail", "sed", "tee", "grep", "wc", "diff", "stat")


def unlisted_file_rule(cfg, command) -> str:
    """A Read or Edit deny rule that names a file argument of a command outside
    FILE_COMMANDS. The simulation does not apply such a rule; the report says so."""
    facts = analyze_bash(command, cfg.cwd, cfg.home)
    if facts.unparseable:
        return ""
    for sub in facts.subs:
        if not sub.values or sub.values[0] in FILE_COMMANDS:
            continue
        for value in sub.values[1:]:
            if not value or value.startswith("-") or any(c in value for c in "$`()'\"=@ "):
                continue
            path = _expand(value, cfg.cwd, cfg.home)
            for family in ("Read", "Edit"):
                hit = _path_list_hit(cfg.rules.get("deny", []), family, path, cfg.cwd, cfg.home, "deny")
                if hit is not None:
                    return hit.raw
    return ""


def critical_rm(command, cwd, home) -> bool:
    facts = analyze_bash(command, cwd, home)
    return not facts.unparseable and _critical_in(facts, cwd, home)


def _git_read_only(values):
    if len(values) < 2 or values[1].startswith("-"):
        return False
    sub, rest = values[1], values[2:]
    if sub in GIT_READ_ONLY:
        return not any(v.startswith("--output") for v in rest)
    if sub == "branch":
        return all(v in ("-a", "-r", "-v", "-vv", "-l", "--list", "--all", "--remotes", "--show-current",
                         "--verbose", "--merged", "--no-merged") for v in rest)
    if sub == "tag":
        return all(v in ("-l", "--list", "-n") for v in rest)
    if sub == "remote":
        return not rest or rest in (["-v"], ["--verbose"]) or rest[0] in ("show", "get-url")
    if sub == "config":
        return any(v in ("--get", "--get-all", "--get-regexp", "--list", "-l") for v in rest)
    if sub in ("stash", "worktree"):
        return bool(rest) and rest[0] in (("list", "show") if sub == "stash" else ("list",))
    if sub == "reflog":
        return not rest or rest[0] == "show"
    return False


def _read_only(sub, cfg):
    if not sub.values or sub.words[0] != sub.values[0]:
        return False
    name = sub.values[0]
    if name == "git":
        return _git_read_only(sub.values)
    if name not in READ_ONLY:
        return False
    if name == "find" and any(v in FIND_WRITES for v in sub.values):
        return False
    if name in GLOB_WRITE_CAPABLE and any("glob" in f for f in sub.cmd.flags):
        return False
    if name == "cd":
        target = sub.values[1] if len(sub.values) > 1 else "~"
        if "$" in target or target == "-":
            return False
        return _in_working_dirs(cfg, _expand(target, cfg.cwd, cfg.home))
    return True


def _in_working_dirs(cfg, path):
    for d in [cfg.cwd] + list(cfg.additional_dirs):
        if path == d or path.startswith(d.rstrip("/") + "/"):
            return True
    return False


# ---------------------------------------------------------------------------
# Evaluation
# ---------------------------------------------------------------------------

@dataclass
class CallFacts:
    tool: str
    tool_input: dict
    bash: Optional[BashFacts] = None
    path: str = ""
    critical: bool = False
    protected: bool = False


def analyze_call(cfg, tool, tool_input) -> CallFacts:
    tool_input = tool_input if isinstance(tool_input, dict) else {}
    facts = CallFacts(tool=tool, tool_input=tool_input)
    if tool == "Bash":
        facts.bash = analyze_bash(tool_input.get("command"), cfg.cwd, cfg.home)
        if not facts.bash.unparseable:
            facts.critical = _critical_in(facts.bash, cfg.cwd, cfg.home)
        facts.protected = any(op.path and is_protected(op.path) for op in facts.bash.writes
                              if op.how in ("redirect", "tee"))
        return facts
    key = PRIMARY_FIELD.get(tool, "file_path")
    path = tool_input.get(key) or tool_input.get("notebook_path")
    if isinstance(path, str) and path:
        facts.path = _expand(path, cfg.cwd, cfg.home)
        if tool in EDIT_FAMILY:
            facts.protected = is_protected(facts.path)
    return facts


def _first_hit(cfg, list_name, facts, skip_bare_bash=False):
    rules = cfg.rules.get(list_name, [])
    tool, tool_input = facts.tool, facts.tool_input
    for rule in rules:
        if rule.ignored or rule.kind == "path":
            continue
        if rule.kind == "bash":
            if tool == rule.tool and facts.bash is not None and _bash_list_hit(rule, facts.bash, list_name):
                return rule
            continue
        if skip_bare_bash and rule.kind == "tool" and rule.tool == "Bash":
            continue
        if _rule_hits(rule, tool, tool_input, list_name):
            return rule
    checks = []
    if facts.bash is not None:
        checks += [("Read", op.path) for op in facts.bash.reads if op.path]
        checks += [("Edit", op.path) for op in facts.bash.writes if op.path]
    elif facts.path and tool in READ_FAMILY:
        checks.append(("Read", facts.path))
    elif facts.path and tool in EDIT_FAMILY:
        checks.append(("Edit", facts.path))
        if list_name == "deny" and tool != "NotebookEdit":
            checks.append(("Read", facts.path))
    for family, path in checks:
        hit = _path_list_hit(rules, family, path, cfg.cwd, cfg.home, list_name)
        if hit is not None:
            return hit
    return None


def _exact_only(sub):
    """Commands only an exact-match allow rule approves: exec wrappers, and find
    with -exec or -delete."""
    if not sub.values or sub.words[0] != sub.values[0]:
        return False
    name = sub.values[0]
    return name in EXEC_WRAPPERS or (name == "find" and any(v in FIND_WRITES for v in sub.values))


def _allow_rule_for_text(cfg, text, exact_only=False):
    for rule in cfg.rules.get("allow", []):
        if rule.ignored:
            continue
        if exact_only:
            if rule.kind == "bash" and rule.tool == "Bash" and "*" not in rule.spec and rule.spec == text:
                return rule
            continue
        if rule.kind == "tool" and rule.tool == "Bash":
            return rule
        if rule.kind == "bash" and rule.tool == "Bash" and bash_pattern_matches(rule.spec, text):
            return rule
    return None


def _path_allowed(cfg, family, path):
    return _path_list_hit(cfg.rules.get("allow", []), family, path, cfg.cwd, cfg.home, "allow")


def _fs_in_scope(cfg, sub):
    if not sub.values or sub.words[0] != sub.values[0] or sub.values[0] not in FS_COMMANDS:
        return False
    name = sub.values[0]
    if name == "sed":
        reads, writes = _cmd_file_args("sed", sub.values)
        targets = reads or writes
    else:
        targets = [v for v in sub.values[1:] if not v.startswith("-")]
    for target in targets:
        op = _file_op("write", target, name, cfg.cwd, cfg.home)
        if not op.path or op.needs_approval or not _in_working_dirs(cfg, op.path) or is_protected(op.path):
            return False
    return True


def _approve_bash(cfg, facts, mode):
    bash = facts.bash
    if bash.unparseable:
        return None
    used, layers = None, set()
    changes_dir = any(s.values and s.values[0] == "cd" and len(s.values) > 1
                      and _expand(s.values[1], cfg.cwd, cfg.home) != cfg.cwd for s in bash.subs)
    for sub in bash.subs:
        rule = _allow_rule_for_text(cfg, sub.allow_text, _exact_only(sub))
        if rule is not None:
            used = used or rule
            layers.add("rule")
        elif _read_only(sub, cfg):
            if changes_dir and sub.values and sub.values[0] == "git":
                return None
            layers.add("built-in")
        elif mode == "acceptEdits" and _fs_in_scope(cfg, sub):
            layers.add("mode")
        else:
            return None
    for op in bash.writes:
        if op.how not in ("redirect", "tee"):
            continue
        if op.needs_approval or not op.path or (changes_dir and not os.path.isabs(op.raw)):
            return None
        rule = _path_allowed(cfg, "Edit", op.path)
        if rule is not None:
            used = used or rule
            layers.add("rule")
        elif mode == "acceptEdits" and _in_working_dirs(cfg, op.path):
            layers.add("mode")
        else:
            return None
    for op in bash.reads:
        if op.how != "redirect":
            continue
        if op.needs_approval or not op.path or (changes_dir and not os.path.isabs(op.raw)):
            return None
        if not _in_working_dirs(cfg, op.path) and _path_allowed(cfg, "Read", op.path) is None:
            return None
    if "rule" in layers:
        return Result("allow", "rule", "an allow rule approves it", rule=used.raw, source=used.source)
    if "mode" in layers:
        return Result("allow", "mode", "acceptEdits mode approves file changes inside the project")
    return Result("allow", "built-in", "Claude Code treats it as read-only and runs it without asking")


def _approve(cfg, facts, mode):
    tool = facts.tool
    if tool == "Bash":
        return _approve_bash(cfg, facts, mode)
    if tool in READ_FAMILY:
        if not facts.path or _in_working_dirs(cfg, facts.path):
            return Result("allow", "built-in", "reads inside the working folders run without asking")
        rule = _path_allowed(cfg, "Read", facts.path)
        if rule is not None:
            return Result("allow", "rule", "an allow rule approves it", rule=rule.raw, source=rule.source)
        if mode == "auto":
            return Result("allow", "mode", "auto mode runs file reads without asking, outside the working folders "
                                           "too, after a one-time question")
        return None
    if tool in EDIT_FAMILY:
        rule = _path_allowed(cfg, "Edit", facts.path) if facts.path else None
        if rule is not None:
            return Result("allow", "rule", "an allow rule approves it", rule=rule.raw, source=rule.source)
        if mode in ("acceptEdits", "auto") and facts.path and _in_working_dirs(cfg, facts.path):
            return Result("allow", "mode", "%s mode approves edits inside the working folders" % mode)
        return None
    for rule in cfg.rules.get("allow", []):
        if not rule.ignored and rule.kind not in ("bash", "path") and _rule_hits(rule, tool, facts.tool_input, "allow"):
            return Result("allow", "rule", "an allow rule approves it", rule=rule.raw, source=rule.source)
    return None


def _sandboxed(cfg, facts, mode):
    box = cfg.sandbox or {}
    if facts.tool != "Bash" or not box.get("enabled") or box.get("autoAllowBashIfSandboxed") is False:
        return False
    if mode == "plan":
        return False
    return not _excluded_from_sandbox(cfg, facts)


def _excluded_from_sandbox(cfg, facts):
    patterns = [p for p in (cfg.sandbox or {}).get("excludedCommands") or [] if isinstance(p, str)]
    bash = facts.bash
    if not patterns or bash is None or bash.unparseable or not bash.subs:
        return False
    if bash.parsed.features & {"substitution", "subshell", "control", "process_substitution"}:
        return False
    for sub in bash.subs:
        name = sub.words[0] if sub.words else ""
        if name in ("sudo", "eval", "xargs", "cd", "pushd", "popd") or "$" in name:
            return False
        if sub.cmd.file_redirects():
            return False
        if not any(bash_pattern_matches(p, sub.deny_text) for p in patterns):
            return False
    return True


def _box_section(cfg, name):
    value = (cfg.sandbox or {}).get(name)
    return value if isinstance(value, dict) else {}


def _all_hosts_allowed(cfg):
    domains = _box_section(cfg, "network").get("allowedDomains")
    return isinstance(domains, list) and "*" in domains


def _box_path(entry, cfg):
    if not isinstance(entry, str) or not entry or any(c in entry for c in "*?["):
        return ""
    if entry == "~" or entry.startswith("~/"):
        return os.path.normpath(cfg.home + entry[1:])
    return os.path.normpath(entry) if entry.startswith("/") else ""


def _sandbox_denies_read(cfg, facts):
    """A file a recognized command reads that sandbox.filesystem.denyRead
    covers and no narrower allowRead entry reopens."""
    fs = _box_section(cfg, "filesystem")
    denied = [p for p in (_box_path(e, cfg) for e in fs.get("denyRead") or []) if p]
    allowed = [p for p in (_box_path(e, cfg) for e in fs.get("allowRead") or []) if p]

    def under(path, folder):
        return path == folder or path.startswith(folder.rstrip("/") + "/")
    for op in facts.bash.reads if facts.bash is not None else []:
        hits = [d for d in denied if op.path and under(op.path, d)]
        if hits and not any(under(op.path, a) and len(a) > max(len(d) for d in hits) for a in allowed):
            return True
    return False


def _fallback(cfg, mode, tool, detail):
    """What the mode does with a call nothing else decided."""
    if mode == "dontAsk":
        return Result("deny", "mode", detail + "; dontAsk mode denies anything that would ask")
    if mode == "auto":
        return Result("classifier", "mode", detail + "; auto mode leaves it to the classifier, which this test "
                                                     "cannot run")
    if mode == "plan" and tool == "Bash" and cfg.auto_available and cfg.plan_uses_auto:
        return Result("classifier", "mode", detail + "; plan mode sends shell commands to the auto-mode classifier")
    return Result("ask", "mode", detail + ", so Claude Code asks first")


def _retry(cfg, facts, mode, what):
    """The sandbox stopped the command; Claude may retry it outside the sandbox."""
    if (cfg.sandbox or {}).get("allowUnsandboxedCommands") is False:
        return Result("deny", "sandbox", "stopped by the sandbox: it %s, and allowUnsandboxedCommands is off, so "
                                         "Claude Code cannot retry it outside the sandbox" % what)
    detail = "the sandbox stops it (it %s) and a retry outside the sandbox" % what
    retry_input = dict(facts.tool_input, dangerouslyDisableSandbox=True)
    for rule in cfg.rules.get("ask", []):
        if not rule.ignored and rule.kind == "param" and _rule_hits(rule, "Bash", retry_input, "ask"):
            return _prompt(mode, "rule", detail + " asks first because of an ask rule", rule)
    if cfg.block_outside_reads:
        return _prompt(mode, "rule", detail + " asks first while blockReadsOutsideWorkingDirectories is on",
                       BLOCK_READS)
    result = _fallback(cfg, mode, "Bash", detail + " needs approval")
    if result.verdict == "ask":
        result.layer, result.mode_only = "sandbox", True
    return result


def _sandbox_result(cfg, facts, mode, needs):
    """A sandboxed shell command under auto-allow, given what it needs to do its harm."""
    needs = set(needs or ())
    fs_on = not cfg.sandbox_fs_disabled
    if fs_on and "write-git" in needs:
        return _retry(cfg, facts, mode, "writes .git/hooks or .git/config, which the sandbox protects")
    if fs_on and "write-outside" in needs:
        return _retry(cfg, facts, mode, "writes outside the folders the sandbox lets it change")
    if fs_on and _sandbox_denies_read(cfg, facts):
        return _retry(cfg, facts, mode, "reads a path sandbox.filesystem.denyRead blocks")
    if "network" in needs and not _all_hosts_allowed(cfg):
        if cfg.sandbox_strict:
            return Result("deny", "sandbox", "stopped by the sandbox: the host is not on the network allowlist, "
                                             "and the strict allowlist refuses it in every mode")
        if mode == "auto":
            return Result("classifier", "sandbox", "auto mode refuses hosts outside the network allowlist unless "
                                                   "the classifier approves the hosts the command lists")
        if mode == "dontAsk":
            return Result("deny", "sandbox", "dontAsk mode refuses hosts outside the network allowlist")
        return Result("ask", "sandbox", "asks first (needs network): the sandbox asks before it reaches a host "
                                        "outside the network allowlist", mode_only=True)
    return Result("allow", "sandbox", "the sandbox's auto-allow runs it without a prompt; the sandbox limits what "
                                      "it can reach")


def _prompt(mode, layer, detail, rule=None):
    """Ask first, or deny in dontAsk mode. `rule` is a Rule or the name of a setting."""
    if isinstance(rule, str):
        raw, source = rule, ""
    else:
        raw, source = (rule.raw, rule.source) if rule is not None else ("", "")
    if mode == "dontAsk":
        return Result("deny", layer, detail + "; dontAsk mode denies anything that would ask", raw, source)
    return Result("ask", layer, detail, raw, source)


def normalize_mode(mode):
    mode = MODE_ALIASES.get(mode, mode)
    return mode if mode in MODES else "default"


BLOCK_READS = "permissions.blockReadsOutsideWorkingDirectories"


def _reads_outside(cfg, facts):
    """With blockReadsOutsideWorkingDirectories on: whether this call reads
    outside the working folders, or is a shell command the parser cannot trace."""
    if not cfg.block_outside_reads:
        return False
    if facts.tool in READ_FAMILY:
        return bool(facts.path) and not _in_working_dirs(cfg, facts.path)
    bash = facts.bash
    if bash is None:
        return False
    if bash.unparseable or "subshell" in bash.parsed.features:
        return True
    if sum(1 for sub in bash.subs if sub.values and sub.values[0] == "cd") > 1:
        return True
    return any(op.path and not _in_working_dirs(cfg, op.path) for op in bash.reads)


def evaluate(cfg, tool, tool_input, mode=None, hook=None, needs=()) -> Result:
    """What Claude Code does with one tool call, in the documented order:
    a blocking hook, deny rules, plan mode's edit block, blocked reads outside
    the working folders, ask rules, a hook's ask, critical-path removals, a
    hook's allow, protected paths, the mode, allow rules and the read-only set,
    the sandbox (`needs` says what the command needs to do its harm: network,
    write-outside, write-git, write-project), then the mode's fallback."""
    mode = normalize_mode(mode or cfg.mode)
    facts = analyze_call(cfg, tool, tool_input)
    if hook is not None and hook.decision == "deny":
        return Result("deny", "hook", hook.reason or "a PreToolUse hook blocked it", source=hook.source)
    rule = _first_hit(cfg, "deny", facts)
    if rule is not None:
        return Result("deny", "rule", "a deny rule blocks it", rule.raw, rule.source)
    if mode == "plan" and tool in EDIT_FAMILY:
        return Result("deny", "mode", "plan mode blocks edits until you approve the plan")
    outside = _reads_outside(cfg, facts)
    if outside and tool in READ_FAMILY:
        return Result("deny", "rule", "blockReadsOutsideWorkingDirectories refuses reads outside the working "
                                      "folders in every mode", BLOCK_READS)
    sandboxed = _sandboxed(cfg, facts, mode)
    rule = _first_hit(cfg, "ask", facts, skip_bare_bash=sandboxed)
    if rule is not None:
        return _prompt(mode, "rule", "an ask rule makes Claude Code ask first", rule)
    if hook is not None and hook.decision == "ask":
        return _prompt(mode, "hook", hook.reason or "a PreToolUse hook asks first")
    if facts.critical:
        return _prompt(mode, "built-in", "it removes a critical path (the root, a top-level folder, "
                                         "the home folder, or the working folder or a parent)")
    if outside:
        if sandboxed and not cfg.sandbox_fs_disabled:
            return _retry(cfg, facts, mode, "reads outside the working folders, which the sandbox blocks while "
                                            "blockReadsOutsideWorkingDirectories is on")
        return _prompt(mode, "rule", "blockReadsOutsideWorkingDirectories makes shell commands that read outside "
                                     "the working folders ask first, in every mode", BLOCK_READS)
    if hook is not None and hook.decision == "allow":
        return Result("allow", "hook", hook.reason or "a PreToolUse hook approved it", source=hook.source)
    if facts.protected:
        verdict = {"bypassPermissions": "allow", "dontAsk": "deny", "auto": "classifier"}.get(mode, "ask")
        if mode == "plan" and tool == "Bash" and cfg.auto_available and cfg.plan_uses_auto:
            verdict = "classifier"
        return Result(verdict, "built-in", "it writes a protected path such as .git, .claude, or a shell "
                                           "startup file")
    if mode == "bypassPermissions":
        return Result("allow", "mode", "bypassPermissions mode runs it without asking")
    approved = _approve(cfg, facts, mode)
    if approved is not None:
        return approved
    if sandboxed:
        return _sandbox_result(cfg, facts, mode, needs)
    return _fallback(cfg, mode, tool, "no rule covers it")


# ---------------------------------------------------------------------------
# Hooks: matcher and if field
# ---------------------------------------------------------------------------

def hook_matcher_matches(matcher, tool) -> bool:
    if matcher is None or matcher in ("", "*"):
        return True
    if not isinstance(matcher, str):
        return False
    if re.fullmatch(r"[A-Za-z0-9_\- ,|]+", matcher):
        return tool in [n.strip() for n in re.split(r"[|,]", matcher) if n.strip()]
    try:
        return re.search(matcher, tool) is not None
    except re.error:
        return False


def if_matches(if_rule, tool, tool_input, cfg) -> bool:
    """Whether a hook handler's `if` field lets it run for this call."""
    rule = parse_rule(if_rule, "if", source="cli")
    tool_input = tool_input if isinstance(tool_input, dict) else {}
    if rule.kind == "bash":
        if tool != rule.tool:
            return False
        facts = analyze_bash(tool_input.get("command"), cfg.cwd, cfg.home)
        if facts.unparseable:
            return True
        beyond_name = " " in (rule.spec[:-2] if rule.spec.endswith(" *") else rule.spec).strip()
        if beyond_name and facts.parsed.features & {"substitution", "variable", "process_substitution"}:
            return True
        for sub in facts.subs:
            first = sub.words[0] if sub.words else ""
            if "$" in first or "`" in first or bash_pattern_matches(rule.spec, sub.deny_text):
                return True
        return False
    if rule.kind == "path":
        family = rule.tool
        if (family == "Read" and tool not in READ_FAMILY) or (family == "Edit" and tool not in EDIT_FAMILY):
            return False
        path = tool_input.get(PRIMARY_FIELD.get(tool, "file_path"))
        return isinstance(path, str) and _path_match(rule, _expand(path, cfg.cwd, cfg.home),
                                                     cfg.cwd, cfg.home, "allow")[0]
    return _rule_hits(rule, tool, tool_input, "if")


# ---------------------------------------------------------------------------
# Settings loading
# ---------------------------------------------------------------------------

def config_from_rules(allow=(), ask=(), deny=(), cwd="/", home="/", mode="default", additional_dirs=(),
                      sandbox=None, source="project", auto_available=True, plan_uses_auto=True,
                      block_outside_reads=False) -> Config:
    """A Config from rule lists (tests and fix checks). A sandbox dict here is
    taken as coming from a file that may set every sandbox key."""
    cfg = Config(cwd=os.path.normpath(cwd), home=os.path.normpath(home), mode=normalize_mode(mode),
                 additional_dirs=[_expand(d, cwd, home) for d in additional_dirs], sandbox=dict(sandbox or {}),
                 auto_available=auto_available, plan_uses_auto=plan_uses_auto,
                 block_outside_reads=block_outside_reads)
    network = cfg.sandbox.get("network") if isinstance(cfg.sandbox.get("network"), dict) else {}
    filesystem = cfg.sandbox.get("filesystem") if isinstance(cfg.sandbox.get("filesystem"), dict) else {}
    cfg.sandbox_strict = network.get("strictAllowlist") is True or network.get("allowManagedDomainsOnly") is True
    cfg.sandbox_fs_disabled = filesystem.get("disabled") is True
    root = _source_root(source, cfg.cwd, cfg.home)
    for list_name, texts in (("allow", allow), ("ask", ask), ("deny", deny)):
        cfg.rules[list_name] = [parse_rule(t, list_name, source, root) for t in texts]
    return cfg


def _read_json(path):
    with open(path, encoding="utf-8") as fh:
        data = json.load(fh)
    if not isinstance(data, dict):
        raise ValueError("not a JSON object")
    return data


def _merge(base, extra):
    out = dict(base)
    for key, value in extra.items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = _merge(out[key], value)
        elif isinstance(value, list) and isinstance(out.get(key), list):
            out[key] = out[key] + value
        else:
            out[key] = value
    return out


def _git_root(start):
    path = start
    while True:
        if os.path.exists(os.path.join(path, ".git")):
            return path
        parent = os.path.dirname(path)
        if parent == path:
            return ""
        path = parent


def _load_layer(name, paths, cfg):
    """One settings layer from one or more files (later files merge over earlier ones)."""
    layer = Layer(name=name, path=paths[0] if paths else "")
    for path in paths:
        if not os.path.isfile(path):
            continue
        try:
            data = _read_json(path)
        except (OSError, ValueError) as exc:
            layer.error = "%s: %s" % (os.path.basename(path), type(exc).__name__)
            cfg.notes.append("Could not read %s settings (%s: %s); it was skipped." % (
                name, code(os.path.basename(path)), type(exc).__name__))
            continue
        layer.found = True
        layer.path = path if not layer.data else layer.path
        layer.data = _merge(layer.data, data)
    return layer


def _perm(layer):
    perm = layer.data.get("permissions")
    return perm if isinstance(perm, dict) else {}


def load(project, home=None, managed_dirs=None) -> Config:
    """Read every Claude Code settings layer that applies to `project`.

    `home` stands in for the home folder (tests and copies of a setup); when it
    is given, CLAUDE_CONFIG_DIR and the system-wide managed folder are ignored."""
    cwd = os.path.normpath(os.path.abspath(project))
    if home is None:
        home_dir = os.path.expanduser("~")
        claude_dir = os.environ.get("CLAUDE_CONFIG_DIR") or os.path.join(home_dir, ".claude")
        managed = MANAGED_DIRS if managed_dirs is None else managed_dirs
    else:
        home_dir = os.path.normpath(os.path.abspath(home))
        claude_dir = os.path.join(home_dir, ".claude")
        managed = managed_dirs or []
    cfg = Config(cwd=cwd, home=home_dir, claude_dir=claude_dir)
    managed_files = []
    for d in managed:
        managed_files.append(os.path.join(d, "managed-settings.json"))
        managed_files.extend(sorted(globmod.glob(os.path.join(globmod.escape(d), "managed-settings.d", "*.json"))))
    repo = _git_root(cwd)
    local_root = repo if repo and os.path.normpath(repo) != home_dir else cwd
    local_files = [os.path.join(cwd, ".claude", "settings.local.json")]
    if local_root != cwd:
        local_files.insert(0, os.path.join(local_root, ".claude", "settings.local.json"))
        local_files.reverse()  # the repository-root file wins where both set a key
    layers = [
        _load_layer("managed", managed_files, cfg),
        _load_layer("local", local_files, cfg),
        _load_layer("project", [os.path.join(cwd, ".claude", "settings.json")], cfg),
        _load_layer("user", [os.path.join(claude_dir, "settings.json")], cfg),
    ]
    cfg.layers = layers
    roots = {"managed": managed[0] if managed else cwd, "local": cwd, "project": cwd, "user": claude_dir}
    managed_only = bool(_perm(layers[0]).get("allowManagedPermissionRulesOnly") or
                        layers[0].data.get("allowManagedPermissionRulesOnly"))
    for layer in layers:
        if managed_only and layer.name != "managed":
            continue
        perm = _perm(layer)
        for list_name in ("allow", "ask", "deny"):
            texts = perm.get(list_name)
            for text in texts if isinstance(texts, list) else []:
                if isinstance(text, str) and text.strip():
                    cfg.rules[list_name].append(parse_rule(text, list_name, layer.name, roots[layer.name]))
        dirs = perm.get("additionalDirectories")
        for d in dirs if isinstance(dirs, list) else []:
            if isinstance(d, str) and d:
                cfg.additional_dirs.append(_expand(d, cwd, home_dir))
    if managed_only:
        cfg.notes.append("Managed settings set allowManagedPermissionRulesOnly, so only managed rules apply.")
    cfg.block_outside_reads = any(_perm(l).get("blockReadsOutsideWorkingDirectories") is True for l in layers)
    for layer in layers:                           # highest precedence first; project settings cannot set it
        if layer.name != "project" and isinstance(layer.data.get("useAutoModeDuringPlan"), bool):
            cfg.plan_uses_auto = layer.data["useAutoModeDuringPlan"]
            break
    _resolve_mode(cfg, layers)
    _resolve_sandbox(cfg, layers)
    _load_hooks(cfg, layers)
    cfg.smells = static_smells(cfg)
    return cfg


_INTERPRETERS = {"bash", "sh", "zsh", "dash", "python", "python3", "node", "ruby", "perl", "deno", "bun"}


def hook_script_path(spec, cfg):
    """The script file a hook command runs, when it can be read from the command."""
    try:
        tokens = [spec.command] + spec.args if spec.args is not None else shlex.split(spec.command)
    except ValueError:
        return None
    env = {"CLAUDE_PROJECT_DIR": cfg.cwd, "HOME": cfg.home, "CLAUDE_PLUGIN_ROOT": spec.plugin_root}

    def expand(token):
        for key, value in env.items():
            if value:
                token = token.replace("${%s}" % key, value).replace("$" + key, value)
        return cfg.home + token[1:] if token.startswith("~/") else token

    tokens = [expand(t) for t in tokens if t]
    if not tokens:
        return None
    candidate = tokens[0]
    if os.path.basename(candidate) in _INTERPRETERS:
        rest = [t for t in tokens[1:] if not t.startswith("-")]
        candidate = rest[0] if rest else ""
    if "/" not in candidate or "$" in candidate or "`" in candidate:
        return None
    return os.path.normpath(os.path.join(cfg.cwd, candidate))


def _smell(sid, severity, text, source=""):
    return {"id": sid, "severity": severity, "text": text, "source": source}


def hook_source(source):
    """A hook's source for a report line: a settings layer as is, a plugin's
    name (read from settings) in inline code."""
    return code(source) if source.startswith("plugin:") else source


def hook_label(source, command):
    """'The <source> hook <command>' for a problem line, the command in inline code."""
    return "The %s hook %s" % (hook_source(source), code(command))


def static_smells(cfg) -> list:
    """Configuration problems visible without running anything."""
    out = []
    if cfg.mode == "bypassPermissions":
        out.append(_smell("bypass-default", "high", "Sessions start in bypassPermissions mode, which skips "
                          "every prompt; only deny rules, ask rules, and hooks still apply.", cfg.mode_source))
    if not cfg.disable_bypass:
        out.append(_smell("bypass-not-disabled", "medium", "No settings file sets permissions."
                          "disableBypassPermissionsMode to \"disable\", so anyone can start a session with "
                          "--dangerously-skip-permissions, which skips every prompt."))
    for list_name in ("allow", "ask", "deny"):
        for rule in cfg.rules[list_name]:
            if rule.ignored:
                out.append(_smell("rule-ignored", "medium", "The %s rule %s is ignored: %s." % (
                    list_name, code(rule.raw), rule.ignored), rule.source))
            if list_name != "allow":
                continue
            if rule.kind == "tool" and rule.tool == "Bash":
                out.append(_smell("allow-all-bash", "high", "The allow rule %s approves every shell command "
                                  "without asking, so only deny rules, ask rules, and hooks stand in the way."
                                  % code(rule.raw), rule.source))
            if rule.kind == "bash":
                tokens = rule.spec.split()
                star = next((i for i, t in enumerate(tokens) if "*" in t), None)
                if star is not None and star <= 1 and star < len(tokens) - 1:
                    out.append(_smell("allow-wildcard-before-subcommand", "medium", "The allow rule %s has a "
                                      "wildcard before the subcommand, so it approves any subcommand, including "
                                      "git -c options that run programs." % code(rule.raw), rule.source))
    cursor = os.path.isdir(os.path.join(cfg.home, ".cursor"))
    for spec in cfg.hooks:
        label = hook_label(spec.source, spec.command)
        if spec.timeout is None:
            out.append(_smell("hook-no-timeout", "low", label + " sets no timeout, so Claude Code waits up to "
                              "600 seconds for it; a hook that times out lets the call through.", spec.source))
        path = hook_script_path(spec, cfg)
        if path and not os.path.exists(path):
            out.append(_smell("hook-missing-script", "high", label + " points at a script that does not exist, "
                              "so it cannot start and never blocks anything.", spec.source))
        elif path and os.path.isfile(path) and os.path.getsize(path) < 262144:
            try:
                with open(path, encoding="utf-8", errors="replace") as fh:
                    text = fh.read()
            except OSError:
                text = ""
            if re.search(r"\bexit\s+1\b", text) and not re.search(r"\bexit\s+2\b", text) and \
                    "permissionDecision" not in text and "decision" not in text:
                out.append(_smell("hook-exit-1", "medium", label + " can only exit 1, which Claude Code treats as "
                                  "a non-blocking error: the call goes ahead. Blocking needs exit 2.", spec.source))
        if cursor and spec.source in ("user", "project", "local") and hook_matcher_matches(spec.matcher, "Bash") \
                and not hook_matcher_matches(spec.matcher, "Shell") and spec.matcher not in (None, "", "*"):
            out.append(_smell("cursor-bash-matcher", "medium", label + " also loads in Cursor, but its matcher "
                              "%s never fires there: Cursor calls its shell tool Shell." % code(spec.matcher),
                              spec.source))
    for spec, why in cfg.skipped_hooks:
        if why.startswith("disableAllHooks"):
            out.append(_smell("hooks-disabled", "high", "%s never runs: %s." % (hook_label(spec.source, spec.command),
                              why), spec.source))
    unique = {}
    for smell in out:                              # the same handler under two matchers is one finding
        unique.setdefault((smell["id"], smell["text"]), smell)
    return list(unique.values())


BUILT_IN_SOURCE = "built-in default on 2.1.283+"


def _resolve_mode(cfg, layers):
    """The mode a terminal session starts in, from the permission-modes page:
    the highest settings layer with a defaultMode wins, except that "auto" in
    project or local settings falls back to the built-in default and
    "bypassPermissions" there starts Manual mode; with no defaultMode, the
    built-in default is auto on v2.1.283 and later, or Manual when a settings
    file sets disableAutoMode to "disable"."""
    cfg.disable_bypass = any(_perm(l).get("disableBypassPermissionsMode") == "disable" for l in layers)
    cfg.auto_available = not any(_perm(l).get("disableAutoMode") == "disable" for l in layers)
    chosen, source = None, ""
    for layer in layers:
        mode = _perm(layer).get("defaultMode")
        if not isinstance(mode, str):
            continue
        mode = MODE_ALIASES.get(mode, mode)
        if mode not in MODES:
            cfg.notes.append("%s settings name an unknown defaultMode, which is skipped." % layer.name.capitalize())
            continue
        if mode == "auto" and layer.name in ("project", "local"):
            cfg.notes.append("%s settings set defaultMode auto, which does not take effect there; Claude Code uses "
                             "the built-in default instead." % layer.name.capitalize())
            break
        if mode == "bypassPermissions" and layer.name in ("project", "local"):
            cfg.notes.append("%s settings set defaultMode bypassPermissions, which does not take effect there; the "
                             "session starts in Manual mode." % layer.name.capitalize())
            chosen, source = "default", "%s settings (bypassPermissions is ignored there)" % layer.name
            break
        chosen, source = mode, "%s settings" % layer.name
        break
    if chosen is None:
        chosen, source = ("auto", BUILT_IN_SOURCE) if cfg.auto_available else (
            "default", "built-in default (auto mode is disabled)")
    cfg.mode, cfg.mode_source = chosen, source
    if cfg.mode == "bypassPermissions" and cfg.disable_bypass:
        cfg.notes.append("defaultMode bypassPermissions is turned off by disableBypassPermissionsMode.")
        cfg.mode, cfg.mode_source = "default", "bypassPermissions is disabled"
    if cfg.mode == "auto" and not cfg.auto_available:
        cfg.notes.append("defaultMode auto is turned off by disableAutoMode.")
        cfg.mode, cfg.mode_source = "default", "auto mode is disabled"


def _resolve_sandbox(cfg, layers):
    box = {}
    for layer in reversed(layers):                 # lowest precedence first, so higher layers win
        data = layer.data.get("sandbox")
        if isinstance(data, dict):
            box = _merge(box, data)
    cfg.sandbox = box

    def section(layer, name):
        data = layer.data.get("sandbox")
        value = data.get(name) if isinstance(data, dict) else None
        return value if isinstance(value, dict) else {}
    # strictAllowlist and filesystem.disabled count only from user or managed settings.
    cfg.sandbox_strict = any(section(l, "network").get("strictAllowlist") is True for l in layers
                             if l.name in ("user", "managed")) or any(
        section(l, "network").get("allowManagedDomainsOnly") is True for l in layers if l.name == "managed")
    cfg.sandbox_fs_disabled = any(section(l, "filesystem").get("disabled") is True for l in layers
                                  if l.name in ("user", "managed"))


def _hook_groups(data):
    hooks = data.get("hooks") if isinstance(data, dict) else None
    groups = hooks.get("PreToolUse") if isinstance(hooks, dict) else None
    return [g for g in groups if isinstance(g, dict)] if isinstance(groups, list) else []


def _plugin_hooks(cfg, layers):
    enabled = {}
    for layer in reversed(layers):
        plugins = layer.data.get("enabledPlugins")
        if isinstance(plugins, dict):
            enabled.update({k: v for k, v in plugins.items() if isinstance(k, str)})
    names = [k for k, v in enabled.items() if v is True]
    if not names:
        return []
    listing = os.path.join(cfg.claude_dir, "plugins", "installed_plugins.json")
    try:
        installed = _read_json(listing).get("plugins")
    except (OSError, ValueError):
        return []
    found = []
    for name in names:
        entries = installed.get(name) if isinstance(installed, dict) else None
        entries = entries if isinstance(entries, list) else [entries] if isinstance(entries, dict) else []
        for entry in entries:
            if not isinstance(entry, dict) or not isinstance(entry.get("installPath"), str):
                continue
            scope, where = entry.get("scope"), entry.get("projectPath")
            if scope not in (None, "user") and not (isinstance(where, str) and os.path.normpath(where) == cfg.cwd):
                continue
            root = entry["installPath"]
            sources = [os.path.join(root, "hooks", "hooks.json")]
            try:
                manifest = _read_json(os.path.join(root, ".claude-plugin", "plugin.json"))
            except (OSError, ValueError):
                manifest = {}
            inline = manifest.get("hooks")
            if isinstance(inline, dict):
                for group in _hook_groups({"hooks": inline.get("hooks", inline)}):
                    found.append(HookSpec("PreToolUse", group.get("matcher"), {}, "plugin:" + name,
                                          os.path.join(root, ".claude-plugin", "plugin.json"), root))
                    found[-1].handler = group
            extra = [inline] if isinstance(inline, str) else inline if isinstance(inline, list) else []
            sources += [os.path.join(root, p) for p in extra if isinstance(p, str)]
            for path in sources:
                try:
                    data = _read_json(path)
                except (OSError, ValueError):
                    continue
                for group in _hook_groups(data):
                    found.append(HookSpec("PreToolUse", group.get("matcher"), group, "plugin:" + name, path, root))
            break
    out = []
    for spec in found:
        group = spec.handler
        for handler in group.get("hooks") or []:
            if isinstance(handler, dict):
                out.append(HookSpec("PreToolUse", group.get("matcher"), handler, spec.source,
                                    spec.source_path, spec.plugin_root))
    if out:
        cfg.notes.append("Plugin hooks were found through Claude Code's plugin list, an internal file; "
                         "treat that part as best effort.")
    return out


def _load_hooks(cfg, layers):
    by_name = {l.name: l for l in layers}
    disable_all = None
    for layer in layers:                           # highest precedence first
        if "disableAllHooks" in layer.data:
            disable_all = (layer.name, bool(layer.data.get("disableAllHooks")))
            break
    managed_only = bool(by_name["managed"].data.get("allowManagedHooksOnly"))
    specs = []
    for layer in layers:                           # every (group, handler); a call runs each handler once
        for group in _hook_groups(layer.data):
            for handler in group.get("hooks") or []:
                if isinstance(handler, dict):
                    specs.append(HookSpec("PreToolUse", group.get("matcher"), handler, layer.name, layer.path))
    specs += _plugin_hooks(cfg, layers)
    for spec in specs:
        why = ""
        if disable_all and disable_all[1] and (disable_all[0] == "managed" or spec.source != "managed"):
            why = "disableAllHooks is set in %s settings" % disable_all[0]
        elif managed_only and spec.source != "managed":
            why = "managed settings allow only managed hooks"
        elif spec.type != "command":
            why = "a %s hook; this test runs command hooks only" % code(spec.type)
        elif spec.is_async:
            why = "an async hook runs in the background and cannot block"
        elif not spec.command:
            why = "the hook has no command"
        if why:
            cfg.skipped_hooks.append((spec, why))
        else:
            cfg.hooks.append(spec)


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] in ("-h", "--help"):
        print(__doc__.strip())
        sys.exit(0)
    print("claude_rules is a helper module for test_guards.py; run it with --help for details.")
