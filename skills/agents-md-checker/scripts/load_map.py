#!/usr/bin/env python3
"""
Map which instruction files each coding agent loads from a folder.

Usage:
    python3 load_map.py [--repo .] [--cwd DIR] [--json] [--out FILE]
                        [--fail-on problem|warning]

For Claude Code, Codex, Gemini CLI, OpenCode, Cursor, GitHub Copilot, and
Aider it lists the files each agent loads, in order, with their size and an
estimated token count (bytes divided by 4), and what gets cut or skipped.
The rules come from each agent's documentation, checked 2026-09-28; see
references/load-rules.md.

Standard library only, Python 3.9+. Read-only: the only file it writes is
the report named by --out. Files outside the repo (user-level files in the
home folder, files in folders above the repo) are measured by size only:
their text is never read or printed.
"""

from __future__ import annotations

import argparse
import glob
import json
import os
import re
import sys

from safe import code, safe_text

try:
    import tomllib  # Python 3.11+
except ImportError:  # Python 3.9 and 3.10 cannot read TOML with the standard library
    tomllib = None

VERSION = "1.0.0"
TOKEN_NOTE = "Token counts are estimates: bytes divided by 4."

CLAUDE_MAX_BYTES = 4 * 1024 * 1024
CLAUDE_GUIDE_LINES = 200
CLAUDE_IMPORT_HOPS = 4
CLAUDE_MODES = ("claude-md-or-agents-md", "claude-md-and-agents-md", "claude-md", "managed-only")
CODEX_DEFAULT_MAX_BYTES = 32 * 1024
GEMINI_IMPORT_DEPTH = 5
CURSOR_GUIDE_LINES = 500
MAX_SCAN_DIRS = 20000
READ_LIMIT = 8 * 1024 * 1024

HARNESSES = (
    ("claude-code", "Claude Code"),
    ("codex", "Codex"),
    ("gemini-cli", "Gemini CLI"),
    ("opencode", "OpenCode"),
    ("cursor", "Cursor"),
    ("copilot", "GitHub Copilot"),
    ("aider", "Aider"),
)

LOADED = "loaded"
TRUNCATED = "truncated"
DROPPED = "dropped"
SKIPPED = "skipped"
ON_DEMAND = "on_demand"
CONDITIONAL = "conditional"
UNVERIFIED = "unverified"
AT_START = (LOADED, TRUNCATED)

SEVERITY_RANK = {"problem": 3, "warning": 2, "info": 1}

# Folders the nested scan never enters: dependencies and build output.
SKIP_DIRS = {
    "node_modules", "bower_components", "__pycache__", "venv", "site-packages",
    "dist", "build", "target", "vendor", "Pods", "coverage",
}

# Order in which findings compete for the headline.
HEADLINE_ORDER = (
    "claude-ignores-agents-md",
    "codex-untrusted",
    "codex-cuts",
    "claude-file-too-large",
    "codex-empty-override",
    "gemini-settings-ignored",
    "broken-import",
    "gemini-import-form",
    "cursor-md-ignored",
    "aider-read-missing",
    "codex-reads-nothing",
    "gemini-reads-nothing",
    "opencode-reads-nothing",
    "cursor-reads-nothing",
    "cursor-legacy-rules",
)

DOC_EXTENSIONS = (".md", ".mdc", ".markdown", ".txt")
FENCE_RE = re.compile(r"^ {0,3}(`{3,}|~{3,})(.*)$")
INLINE_CODE_RE = re.compile(r"(`+)(.+?)(?<!`)\1(?!`)")
IMPORT_RE = re.compile(r"(?:^|(?<=\s))@(\S+)")
IMPORT_TRAILING = ".,;:!?)]}>\"'"


class UsageError(Exception):
    pass


# ------------------------------------------------------------- formatting


def fmt_bytes(count):
    if count < 1024:
        return "1 byte" if count == 1 else "%d bytes" % count
    for unit, size in (("KB", 1024.0), ("MB", 1024.0 * 1024)):
        value = count / size
        if unit == "KB" and value >= 1024:
            continue
        text = ("%.1f" % value) if value < 10 else ("%d" % round(value))
        if text.endswith(".0"):
            text = text[:-2]
        return "%s %s" % (text, unit)
    return "%d bytes" % count


def fmt_int(count):
    return "{:,}".format(count)


def join_clauses(clauses):
    if not clauses:
        return ""
    if len(clauses) == 1:
        return clauses[0] + "."
    if len(clauses) == 2:
        return "%s and %s." % (clauses[0], clauses[1])
    return "%s, and %s." % (", ".join(clauses[:-1]), clauses[-1])


def plural(count, word, many=None):
    return "%d %s" % (count, word if count == 1 else (many or word + "s"))


# ---------------------------------------------------------- text helpers


def scan_markdown(text):
    """Yield (line_number, line, kind, lang) for each line of markdown.

    kind is "prose", "fence" (an opening or closing fence line), "code"
    (inside a fenced block), or "comment" (a block-level HTML comment line).
    lang is the fence's info word, lowercased.
    """
    fence = None
    in_comment = False
    for number, line in enumerate(text.splitlines(), 1):
        if fence is not None:
            match = FENCE_RE.match(line)
            if match and match.group(1)[0] == fence[0] and len(match.group(1)) >= fence[1] and not match.group(2).strip():
                fence = None
                yield number, line, "fence", ""
            else:
                yield number, line, "code", fence[2]
            continue
        if in_comment:
            if "-->" in line:
                in_comment = False
            yield number, line, "comment", ""
            continue
        match = FENCE_RE.match(line)
        if match and not (match.group(1)[0] == "`" and "`" in match.group(2)):
            info = match.group(2).strip()
            lang = re.sub(r"[^a-z0-9_+#-]", "", info.split()[0].lower()) if info else ""
            fence = (match.group(1)[0], len(match.group(1)), lang)
            yield number, line, "fence", lang
            continue
        stripped = line.lstrip(" ")
        if len(line) - len(stripped) <= 3 and stripped.startswith("<!--"):
            if "-->" not in stripped[4:]:
                in_comment = True
            yield number, line, "comment", ""
            continue
        yield number, line, "prose", ""


def block_comment_bytes(text):
    """Bytes taken by block-level HTML comments, which Claude Code strips."""
    raw_lines = text.splitlines(True)
    total = 0
    for number, _line, kind, _lang in scan_markdown(text):
        if kind == "comment":
            total += len(raw_lines[number - 1].encode("utf-8"))
    return total


def find_imports(text):
    """Return (line_number, path) for each @path outside code spans, fenced blocks, and comments."""
    found = []
    for number, line, kind, _lang in scan_markdown(text):
        if kind != "prose":
            continue
        plain = INLINE_CODE_RE.sub(" ", line)
        for match in IMPORT_RE.finditer(plain):
            token = match.group(1).split("#", 1)[0].rstrip(IMPORT_TRAILING)
            if token and ("/" in token or "." in token):
                found.append((number, token))
    return found


def unquote(value):
    value = value.strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
        return value[1:-1]
    return value


def frontmatter(text):
    """Top-level keys of a leading --- block. Values are strings or lists of strings."""
    lines = (text or "").splitlines()
    if not lines or lines[0].strip() != "---":
        return {}
    data = {}
    key = None
    for line in lines[1:]:
        if line.strip() in ("---", "..."):
            return data
        match = re.match(r"^([A-Za-z_][\w-]*)\s*:\s*(.*)$", line)
        if match:
            key = match.group(1)
            value = match.group(2).strip()
            if value.startswith("[") and value.endswith("]"):
                data[key] = [unquote(v) for v in value[1:-1].split(",") if v.strip()]
            else:
                data[key] = unquote(value)
            continue
        item = line.strip()
        if key is not None and item.startswith("-"):
            current = data.get(key)
            if not isinstance(current, list):
                current = [current] if current else []
            value = unquote(item[1:].strip())
            if value:
                current.append(value)
            data[key] = current
    return {}


def as_list(value):
    if isinstance(value, list):
        return [str(v).strip() for v in value if str(v).strip()]
    if isinstance(value, str) and value.strip():
        return [v.strip() for v in value.split(",") if v.strip()]
    return []


def strip_json_comments(text):
    """Remove // and /* */ comments and trailing commas outside strings (JSONC)."""
    out = []
    i, size = 0, len(text)
    in_string = False
    while i < size:
        char = text[i]
        if in_string:
            out.append(char)
            if char == "\\" and i + 1 < size:
                out.append(text[i + 1])
                i += 2
                continue
            if char == '"':
                in_string = False
            i += 1
            continue
        if char == '"':
            in_string = True
        elif text.startswith("//", i):
            end = text.find("\n", i)
            i = size if end == -1 else end
            continue
        elif text.startswith("/*", i):
            end = text.find("*/", i + 2)
            i = size if end == -1 else end + 2
            continue
        elif char == ",":
            rest = re.match(r"(?:\s|//[^\n]*|/\*.*?\*/)*([}\]])", text[i + 1:], re.S)
            if rest:
                i += 1
                continue
        out.append(char)
        i += 1
    return "".join(out)


def dig(data, *keys):
    for key in keys:
        if not isinstance(data, dict):
            return None
        data = data.get(key)
    return data


def glob_regex(pattern):
    """Compile a path glob: ** crosses folders, * and ? stay inside one folder."""
    out = []
    i = 0
    while i < len(pattern):
        char = pattern[i]
        if pattern.startswith("**/", i):
            out.append("(?:.*/)?")
            i += 3
            continue
        if pattern.startswith("**", i):
            out.append(".*")
            i += 2
            continue
        if char == "*":
            out.append("[^/]*")
        elif char == "?":
            out.append("[^/]")
        elif char == "{" and "}" in pattern[i:]:
            end = pattern.index("}", i)
            options = pattern[i + 1:end].split(",")
            out.append("(?:%s)" % "|".join(re.escape(o) for o in options))
            i = end
        else:
            out.append(re.escape(char))
        i += 1
    return re.compile("^%s$" % "".join(out))


def truthy(value):
    return str(value or "").strip().lower() not in ("", "0", "false", "no", "off")


def same_path(a, b):
    try:
        a = os.path.expanduser(str(a))
        b = os.path.expanduser(str(b))
        return os.path.abspath(a) == os.path.abspath(b) or os.path.realpath(a) == os.path.realpath(b)
    except (OSError, ValueError):
        return False


def within(path, parent):
    path_real = os.path.realpath(path)
    parent_real = os.path.realpath(os.path.expanduser(str(parent)))
    return path_real == parent_real or path_real.startswith(parent_real.rstrip(os.sep) + os.sep)


def walk_files(directory):
    found = []
    if not os.path.isdir(directory):
        return found
    for root, dirs, files in os.walk(directory):
        dirs.sort()
        for name in sorted(files):
            found.append(os.path.join(root, name))
    return found


def default_managed_dir():
    if sys.platform == "darwin":
        return "/Library/Application Support/ClaudeCode"
    if sys.platform.startswith("linux"):
        return "/etc/claude-code"
    if sys.platform.startswith("win"):
        return r"C:\Program Files\ClaudeCode"
    return None


# ---------------------------------------------------------------- context


class Context:
    """Everything one load map needs: paths, environment, caches, findings."""

    def __init__(self, repo, cwd, home, env, managed_dir):
        self.repo = os.path.abspath(repo)
        self.cwd = os.path.abspath(cwd)
        self.home = os.path.abspath(home)
        self.env = env
        self.managed_dir = managed_dir
        self.notes = []
        self.findings = []
        self.scan_capped = False
        self._listing = {}
        self._text = {}
        self._dirs = None

    def located(self, path):
        """True when the path sits in the repo's folder tree; a link counts by where the link is."""
        path = os.path.abspath(path)
        return path == self.repo or path.startswith(self.repo + os.sep)

    def inside(self, path):
        """True when the file's real text is in the repo; a link to a home-folder file is outside."""
        real, repo = os.path.realpath(path), os.path.realpath(self.repo)
        return real == repo or real.startswith(repo + os.sep)

    def show(self, path, limit=200):
        """The display path as inline code, for report text."""
        return code(self.display(path), limit)

    def display(self, path):
        path = os.path.abspath(path)
        if self.located(path):
            rel = os.path.relpath(path, self.repo)
            return "." if rel == "." else rel.replace(os.sep, "/")
        if path.startswith(self.home + os.sep):
            return "~/" + os.path.relpath(path, self.home).replace(os.sep, "/")
        return path

    def size(self, path):
        try:
            return os.path.getsize(path)
        except OSError:
            return None

    def read(self, path):
        """Text of a file inside the repo; None for anything outside it."""
        path = os.path.abspath(path)
        if not self.inside(path):
            return None
        if path not in self._text:
            text = None
            try:
                if os.path.getsize(path) <= READ_LIMIT:
                    with open(path, "r", encoding="utf-8", errors="replace") as handle:
                        text = handle.read()
            except OSError:
                text = None
            self._text[path] = text
        return self._text[path]

    def lines(self, path):
        text = self.read(path)
        if text is None:
            return None
        return text.count("\n") + (1 if text and not text.endswith("\n") else 0)

    def is_blank(self, path):
        text = self.read(path)
        if text is not None:
            return not text.strip()
        return (self.size(path) or 0) == 0

    def _entry(self, directory, name):
        if directory not in self._listing:
            try:
                self._listing[directory] = os.listdir(directory)
            except OSError:
                self._listing[directory] = []
        names = self._listing[directory]
        if name in names:
            return name
        lower = name.lower()
        for actual in names:
            if actual.lower() == lower and os.path.exists(os.path.join(directory, name)):
                return actual
        return None

    def find(self, directory, rel):
        """Path of the file rel under directory, or None. Follows the file system's case rules."""
        current = directory
        parts = rel.split("/")
        for part in parts[:-1]:
            actual = self._entry(current, part)
            if actual is None:
                return None
            current = os.path.join(current, actual)
        actual = self._entry(current, parts[-1])
        if actual is None:
            return None
        path = os.path.join(current, actual)
        return path if os.path.isfile(path) else None

    def up_dirs(self, start):
        """start and every ancestor except the file system root, root-first."""
        dirs = []
        current = os.path.abspath(start)
        while True:
            parent = os.path.dirname(current)
            if parent == current:
                break
            dirs.append(current)
            current = parent
        return list(reversed(dirs))

    def repo_dirs(self):
        if self._dirs is None:
            dirs = []
            for root, subdirs, _files in os.walk(self.repo):
                subdirs[:] = sorted(s for s in subdirs if not s.startswith(".") and s not in SKIP_DIRS)
                dirs.append(root)
                if len(dirs) >= MAX_SCAN_DIRS:
                    self.scan_capped = True
                    break
            self._dirs = dirs
        return self._dirs

    def below(self, base):
        prefix = os.path.abspath(base) + os.sep
        return [d for d in self.repo_dirs() if d.startswith(prefix)]

    def note(self, text, owner=None):
        target = owner.notes if owner is not None else self.notes
        if text not in target:
            target.append(text)

    def config_json(self, path, owner=None):
        if not os.path.isfile(path):
            return None
        try:
            with open(path, "r", encoding="utf-8") as handle:
                return json.loads(strip_json_comments(handle.read()))
        except ValueError:
            self.note("Could not read %s: it is not valid JSON, so its settings were not applied." % self.show(path), owner)
        except OSError:
            self.note("Could not open %s, so its settings were not applied." % self.show(path), owner)
        return None

    def config_toml(self, path, owner=None):
        """Parsed TOML; {} when the file is missing or broken; None when Python cannot read TOML."""
        if not os.path.isfile(path):
            return {}
        if tomllib is None:
            return None
        try:
            with open(path, "rb") as handle:
                return tomllib.load(handle)
        except (OSError, ValueError):
            self.note("Could not read %s as TOML, so its settings were not applied." % self.show(path), owner)
            return {}

    def finding(self, fid, harness, severity, message, fix, path="", clause=None):
        self.findings.append({
            "id": fid,
            "harness": harness.id if harness is not None else "",
            "severity": severity,
            "message": message,
            "fix": fix,
            "path": path,
            "_clause": clause,
        })


class Harness:
    """The ordered list of files one agent loads, with a status for each."""

    def __init__(self, ctx, hid, name):
        self.ctx = ctx
        self.id = hid
        self.name = name
        self.files = []
        self.notes = []
        self.index = {}
        self.cut_bytes = 0

    def add(self, path, status, reason="", via="", scope=None, loaded_bytes=None, anchor=None):
        ctx = self.ctx
        key = os.path.abspath(path)
        old = self.index.get(key)
        if old is not None:
            if old["status"] in AT_START or status not in AT_START:
                return old
            self.files.remove(old)
        size = ctx.size(key) or 0
        if loaded_bytes is None:
            loaded_bytes = size if status in AT_START else 0
        item = {
            "path": ctx.display(key),  # raw here; code() or safe_text() runs when it is shown
            "scope": scope or ("project" if ctx.inside(key) else "outside"),
            "status": status,
            "bytes": size,
            "loaded_bytes": loaded_bytes,
            "tokens_est": loaded_bytes // 4,
            "lines": ctx.lines(key),
            "reason": reason,
            "via": via,
            "_abs": key,
            "_anchor": anchor or default_anchor(ctx, key),
        }
        self.files.append(item)
        self.index[key] = item
        return item

    def status_of(self, path):
        item = self.index.get(os.path.abspath(path))
        return item["status"] if item else None

    def summary(self):
        at_start = [f for f in self.files if f["status"] in AT_START]
        loaded = sum(f["loaded_bytes"] for f in at_start)
        return {
            "id": self.id,
            "name": self.name,
            "files": self.files,
            "loaded_files": len(at_start),
            "loaded_bytes": loaded,
            "loaded_tokens_est": loaded // 4,
            "cut_bytes": self.cut_bytes,
            "notes": self.notes,
        }


def default_anchor(ctx, path):
    """Folder a file's commands run from: its own folder, above any dot folder like .github."""
    if not ctx.located(path):
        return ctx.repo
    rel = os.path.relpath(os.path.dirname(path), ctx.repo)
    parts = []
    for part in rel.split(os.sep):
        if part in ("", "."):
            continue
        if part.startswith("."):
            break
        parts.append(part)
    return os.path.join(ctx.repo, *parts)


def resolve_import(token, base_dir, home):
    if token.startswith("~/"):
        return os.path.join(home, token[2:])
    if os.path.isabs(token):
        return token
    return os.path.normpath(os.path.join(base_dir, token))


def follow_imports(ctx, h, importer, max_depth, seen, add_file, depth=1, accept=None, rejected=None):
    """Add the files importer pulls in with @path, depth first, up to max_depth levels.

    accept, when given, decides which @path forms this agent documents; an
    existing file behind another form is appended to rejected instead.
    """
    text = ctx.read(importer)
    if text is None:
        return
    anchor = h.index[os.path.abspath(importer)]["_anchor"] if os.path.abspath(importer) in h.index else None
    for number, token in find_imports(text):
        target = resolve_import(token, os.path.dirname(importer), ctx.home)
        if accept is not None and not accept(token):
            if rejected is not None and os.path.isfile(target):
                rejected.append((importer, number, token))
            continue
        if not os.path.isfile(target):
            if token.lower().endswith(DOC_EXTENSIONS) and not os.path.isdir(target):
                ctx.finding(
                    "broken-import", h, "problem",
                    "%s imports %s, but that file does not exist, so %s loads nothing from it."
                    % (code("%s:%d" % (ctx.display(importer), number), 200), code("@" + token), h.name),
                    "Fix the path after @ or remove the line.",
                    ctx.display(target),
                    clause="%s cannot find a file your instructions import" % h.name,
                )
            continue
        real = os.path.realpath(target)
        if real in seen:
            continue
        seen.add(real)
        via = "@import in %s" % ctx.show(importer)
        if depth > max_depth:
            h.add(target, SKIPPED, reason="imported more than %d levels deep" % max_depth, via=via, anchor=anchor)
            continue
        if add_file(target, via, anchor):
            follow_imports(ctx, h, target, max_depth, seen, add_file, depth + 1, accept, rejected)


# ------------------------------------------------------------ Claude Code


def claude_user_dir(ctx):
    return ctx.env.get("CLAUDE_CONFIG_DIR") or os.path.join(ctx.home, ".claude")


def claude_settings(ctx, h):
    """instructionFiles mode, where it came from, and the merged claudeMdExcludes patterns."""
    layers = []
    if ctx.managed_dir:
        for path in sorted(glob.glob(os.path.join(ctx.managed_dir, "managed-settings.d", "*.json")), reverse=True):
            layers.append(("managed", path))
        layers.append(("managed", os.path.join(ctx.managed_dir, "managed-settings.json")))
    for folder in (ctx.cwd, ctx.repo) if ctx.cwd != ctx.repo else (ctx.cwd,):
        layers.append(("local", os.path.join(folder, ".claude", "settings.local.json")))
        layers.append(("project", os.path.join(folder, ".claude", "settings.json")))
    layers.append(("user", os.path.join(claude_user_dir(ctx), "settings.json")))
    mode, source, excludes = None, "default", []
    for layer, path in layers:
        data = ctx.config_json(path, h)
        if not isinstance(data, dict):
            continue
        patterns = data.get("claudeMdExcludes")
        if isinstance(patterns, list):
            excludes.extend(p for p in patterns if isinstance(p, str))
        value = dig(data, "pluginConfigs", "agents-md@builtin", "options", "instructionFiles")
        if value is None or mode is not None:
            continue
        if value not in CLAUDE_MODES:
            ctx.note("instructionFiles in %s has an unknown value, so the default applies." % ctx.show(path), h)
            continue
        if value == "managed-only" and layer not in ("managed", "user"):
            ctx.note(
                "instructionFiles is managed-only in %s, but Claude Code takes managed-only only from user "
                "or managed settings, so it is ignored there." % ctx.show(path), h)
            continue
        mode, source = value, ctx.show(path)
    return mode or CLAUDE_MODES[0], source, excludes


def build_claude(ctx):
    h = Harness(ctx, "claude-code", "Claude Code")
    mode, source, exclude_patterns = claude_settings(ctx, h)
    excludes = [glob_regex(p) for p in exclude_patterns]
    unverified = mode == "managed-only"
    if mode != CLAUDE_MODES[0]:
        ctx.note("instructionFiles is %s (from %s)." % (mode, source), h)
    if unverified:
        ctx.note(
            "In managed-only mode Claude Code loads only managed instruction files. This checker does not "
            "model that mode, so other files are marked unverified; run /memory in Claude Code to see the list.", h)

    user_dir = claude_user_dir(ctx)
    user_md = os.path.join(user_dir, "CLAUDE.md")
    user_rules = os.path.join(user_dir, "rules")
    user_key = os.path.realpath(user_md)
    seen = set()
    walk = ctx.up_dirs(ctx.cwd)

    blockers = []
    for directory in walk:
        for rel in ("CLAUDE.md", ".claude/CLAUDE.md", "CLAUDE.local.md"):
            path = ctx.find(directory, rel)
            if path and os.path.realpath(path) != user_key:
                blockers.append(path)
    agents_blocked = mode == "claude-md-or-agents-md" and bool(blockers)
    inside_blockers = [b for b in blockers if ctx.located(b)]
    nearest = (inside_blockers or blockers)[-1] if blockers else None

    def excluded(path):
        norm = os.path.abspath(path).replace(os.sep, "/")
        return any(p.match(norm) for p in excludes)

    def load(path, via="", scope=None, anchor=None, reason=""):
        if unverified and scope != "managed":
            h.add(path, UNVERIFIED, reason="instructionFiles is managed-only", via=via, scope=scope, anchor=anchor)
            return False
        if scope != "managed" and excluded(path):
            h.add(path, SKIPPED, reason="matches claudeMdExcludes", via=via, scope=scope, anchor=anchor)
            return False
        size = ctx.size(path) or 0
        if size > CLAUDE_MAX_BYTES:
            item = h.add(path, SKIPPED, reason="over the 4 MiB limit, so Claude Code skips it", via=via, scope=scope, anchor=anchor)
            ctx.finding(
                "claude-file-too-large", h, "problem",
                "Claude Code skips %s: at %s it is over the 4 MiB limit." % (code(item["path"], 200), fmt_bytes(size)),
                "Keep the essentials in the file and move the rest into linked docs.",
                item["path"],
                clause="Claude Code skips %s because it is over 4 MiB" % code(item["path"], 200),
            )
            return False
        text = ctx.read(path)
        loaded = size - block_comment_bytes(text) if text is not None else None
        h.add(path, LOADED, via=via, scope=scope, loaded_bytes=loaded, anchor=anchor, reason=reason)
        seen.add(os.path.realpath(path))
        return text is not None

    def load_import(path, via, anchor):
        reason = "" if within(path, ctx.cwd) else "Claude Code asks once before loading an import from outside the start folder"
        return load(path, via=via, anchor=anchor, reason=reason)

    def top(path, scope=None):
        if load(path, scope=scope):
            follow_imports(ctx, h, path, CLAUDE_IMPORT_HOPS, seen, load_import)

    def rule(path, scope=None):
        paths = as_list(frontmatter(ctx.read(path)).get("paths")) if ctx.inside(path) else []
        if paths:
            h.add(path, CONDITIONAL, reason="loads when Claude Code works on files matching %s"
                  % code(", ".join(paths)))
        else:
            top(path, scope=scope)

    def same_as_blocker(path):
        for blocker in blockers:
            if os.path.realpath(blocker) == os.path.realpath(path):
                return "same file as %s (a link), so nothing is lost" % ctx.show(blocker)
        text = ctx.read(path)
        for blocker in blockers:
            if text is not None and ctx.read(blocker) == text:
                return "same text as %s, so nothing is lost" % ctx.show(blocker)
        return None

    ignored = []

    def agents(path):
        if unverified:
            h.add(path, UNVERIFIED, reason="instructionFiles is managed-only")
        elif mode == "claude-md":
            h.add(path, SKIPPED, reason="instructionFiles is claude-md, so Claude Code never reads AGENTS.md")
        elif agents_blocked:
            same = same_as_blocker(path)
            if same:
                h.add(path, SKIPPED, reason=same)
            else:
                h.add(path, SKIPPED, reason="%s exists in the start folder or above" % ctx.show(nearest))
                ignored.append(path)
        else:
            top(path)

    managed_md = os.path.join(ctx.managed_dir, "CLAUDE.md") if ctx.managed_dir else None
    if managed_md and os.path.isfile(managed_md):
        load(managed_md, scope="managed")
    if os.path.isfile(user_md):
        load(user_md, scope="user")
    for path in walk_files(user_rules):
        if path.endswith(".md"):
            load(path, scope="user")

    for directory in walk:
        for rel in ("CLAUDE.md", ".claude/CLAUDE.md"):
            path = ctx.find(directory, rel)
            if path and os.path.realpath(path) != user_key:
                top(path)
        for rel in ("AGENTS.md", ".claude/AGENTS.md"):
            path = ctx.find(directory, rel)
            if path:
                agents(path)
        rules_dir = os.path.join(directory, ".claude", "rules")
        if os.path.realpath(rules_dir) != os.path.realpath(user_rules):
            for path in walk_files(rules_dir):
                if path.endswith(".md"):
                    rule(path)
        path = ctx.find(directory, "CLAUDE.local.md")
        if path:
            top(path)
        if ctx.located(directory):
            for rel in ("AGENTS.override.md", "AGENTS.local.md"):
                path = ctx.find(directory, rel)
                if path:
                    h.add(path, SKIPPED, reason="Claude Code does not read %s" % rel)

    for directory in ctx.below(ctx.cwd):
        where = code(ctx.display(directory) + "/", 200)
        for rel in ("CLAUDE.md", ".claude/CLAUDE.md", "CLAUDE.local.md"):
            path = ctx.find(directory, rel)
            if path:
                h.add(path, ON_DEMAND, reason="loads when Claude Code reads files in %s" % where)
        for rel in ("AGENTS.md", ".claude/AGENTS.md"):
            path = ctx.find(directory, rel)
            if not path:
                continue
            if unverified:
                h.add(path, UNVERIFIED, reason="instructionFiles is managed-only")
            elif mode == "claude-md" or agents_blocked:
                h.add(path, SKIPPED, reason="a CLAUDE.md exists in the start folder or above" if agents_blocked
                      else "instructionFiles is claude-md")
            else:
                h.add(path, ON_DEMAND, reason="loads when Claude Code reads files in %s" % where)

    still_ignored = [p for p in ignored if h.status_of(p) == SKIPPED]
    if still_ignored:
        skipped_path = still_ignored[0]
        agents_path = ctx.show(skipped_path)
        blocker = ctx.show(nearest)
        name = os.path.basename(nearest) if ctx.located(nearest) else None
        if name:  # name is CLAUDE.md or CLAUDE.local.md in the file system's case, so it stays plain
            label = "a .claude/CLAUDE.md" if ctx.display(nearest).endswith(".claude/CLAUDE.md") else "a %s" % safe_text(name)
            clause = "Claude Code ignores your AGENTS.md because %s exists" % label
            target = next((b for b in reversed(inside_blockers) if os.path.basename(b) == "CLAUDE.md"), nearest)
            target_dir = os.path.dirname(target)
            folder = os.path.dirname(target_dir) if os.path.basename(target_dir) == ".claude" else target_dir
            agents_dir = os.path.dirname(skipped_path)
            if agents_dir.startswith(folder + os.sep):
                fix = ("Add a CLAUDE.md in %s whose first line is `@AGENTS.md`, or set instructionFiles to "
                       "claude-md-and-agents-md." % code(ctx.display(agents_dir) + "/", 200))
            else:
                rel = os.path.relpath(skipped_path, target_dir).replace(os.sep, "/")
                fix = ("Add %s as the first line of %s so Claude Code reads both (the path is relative to the "
                       "file that holds the import), or set instructionFiles to claude-md-and-agents-md."
                       % (code("@" + rel), ctx.show(target)))
        else:
            clause = "Claude Code ignores your AGENTS.md because %s exists above this repo" % blocker
            fix = ("Move or rename %s, or add a CLAUDE.md to this repo whose first line is `@AGENTS.md`."
                   % blocker)
        ctx.finding(
            "claude-ignores-agents-md", h, "problem",
            "Claude Code skips %s because %s exists. It reads AGENTS.md only when no CLAUDE.md, "
            ".claude/CLAUDE.md, or CLAUDE.local.md exists in the start folder or above." % (agents_path, blocker),
            fix, ctx.display(skipped_path), clause=clause,
        )

    for item in h.files:
        if item["status"] == LOADED and item["scope"] == "project" and (item["lines"] or 0) > CLAUDE_GUIDE_LINES:
            ctx.finding(
                "claude-file-long", h, "info",
                "%s has %d lines; Claude Code's guidance is under 200 lines per file." % (code(item["path"], 200), item["lines"]),
                "Move procedures into linked docs and keep the file to what every session needs.",
                item["path"],
            )
    return h


# ------------------------------------------------------------------ Codex


def codex_trust(config, root, cwd):
    if config is None:
        return "unknown"
    projects = config.get("projects") if isinstance(config, dict) else None
    if not isinstance(projects, dict):
        return "unlisted"
    for candidate in (root, cwd):
        for key, value in projects.items():
            if isinstance(value, dict) and same_path(key, candidate):
                level = value.get("trust_level")
                if level in ("trusted", "untrusted"):
                    return level
    return "unlisted"


def build_codex(ctx):
    h = Harness(ctx, "codex", "Codex")
    codex_home = ctx.env.get("CODEX_HOME") or os.path.join(ctx.home, ".codex")
    config_path = os.path.join(codex_home, "config.toml")
    config = ctx.config_toml(config_path, h)
    if config is None:
        ctx.note(
            "Reading %s needs Python 3.11 or newer (tomllib), so this map uses Codex defaults: the 32 KiB "
            "limit, no fallback file names, and trust not checked." % ctx.show(config_path), h)
    settings = config or {}

    def setting(data, key, default, kind):
        value = data.get(key) if isinstance(data, dict) else None
        return value if isinstance(value, kind) and not isinstance(value, bool) else default

    max_bytes = setting(settings, "project_doc_max_bytes", CODEX_DEFAULT_MAX_BYTES, int)
    fallbacks = [n for n in setting(settings, "project_doc_fallback_filenames", [], list) if isinstance(n, str)]
    markers = [m for m in setting(settings, "project_root_markers", [".git"], list) if isinstance(m, str)]

    override = os.path.join(codex_home, "AGENTS.override.md")
    plain = os.path.join(codex_home, "AGENTS.md")
    if os.path.isfile(override) and (ctx.size(override) or 0) > 0:
        h.add(override, LOADED, scope="user", reason="global instructions")
        if os.path.isfile(plain):
            h.add(plain, SKIPPED, scope="user", reason="AGENTS.override.md in the same folder wins")
    elif os.path.isfile(plain):
        if os.path.isfile(override):
            h.add(override, SKIPPED, scope="user", reason="empty, so Codex reads AGENTS.md instead")
        h.add(plain, LOADED, scope="user", reason="global instructions")

    root = None
    if markers:
        for directory in reversed(ctx.up_dirs(ctx.cwd)):
            if any(os.path.lexists(os.path.join(directory, m)) for m in markers):
                root = directory
                break
    chain = ctx.up_dirs(ctx.cwd)
    chain = chain[chain.index(root):] if root else [ctx.cwd]
    if root is None:
        ctx.note("No project root marker (%s) found, so Codex reads only the start folder."
                 % (", ".join(code(m) for m in markers) or "none configured"), h)

    trust = codex_trust(config, root or ctx.cwd, ctx.cwd)
    if trust == "trusted":
        for directory in chain:
            local = ctx.config_toml(os.path.join(directory, ".codex", "config.toml"), h) or {}
            max_bytes = setting(local, "project_doc_max_bytes", max_bytes, int)
            fallbacks = [n for n in setting(local, "project_doc_fallback_filenames", fallbacks, list) if isinstance(n, str)]
    elif trust == "unlisted":
        ctx.note("This project is not listed in %s yet. Codex asks whether to trust it on first launch and "
                 "loads project files only if you trust it." % ctx.show(config_path), h)
    names = ["AGENTS.override.md", "AGENTS.md"] + [n for n in fallbacks if n not in ("AGENTS.override.md", "AGENTS.md")]

    for directory in ctx.up_dirs(ctx.cwd):
        if ctx.located(directory) and directory not in chain:
            for name in names:
                path = ctx.find(directory, name)
                if path:
                    h.add(path, SKIPPED, reason="above the folder where Codex's project root starts")

    project_seen = False
    if trust == "untrusted":
        for directory in chain:
            for name in names:
                path = ctx.find(directory, name)
                if path:
                    project_seen = True
                    h.add(path, SKIPPED, reason="this project is marked untrusted in Codex, so it loads no project file")
        if project_seen:
            ctx.finding(
                "codex-untrusted", h, "problem",
                "Codex loads none of this project's instruction files because %s marks the project untrusted."
                % ctx.show(config_path),
                "Trust the project in Codex, or change trust_level to \"trusted\" for this path in %s."
                % ctx.show(config_path),
                clause="Codex skips your project files because the project is marked untrusted",
            )
    else:
        remaining = max_bytes
        cut_files = []
        for directory in chain:
            chosen = None
            for name in names:
                path = ctx.find(directory, name)
                if not path:
                    continue
                project_seen = True
                if chosen is not None:
                    if ctx.is_blank(chosen):
                        reason = "the empty %s in this folder hides it" % code(os.path.basename(chosen))
                        if os.path.basename(chosen) == "AGENTS.override.md" and not ctx.is_blank(path):
                            ctx.finding(
                                "codex-empty-override", h, "problem",
                                "Codex takes the empty %s, skips it as empty, and never reads %s."
                                % (ctx.show(chosen), ctx.show(path)),
                                "Delete the empty AGENTS.override.md or move your rules into it.",
                                ctx.display(chosen),
                                clause="an empty AGENTS.override.md hides your AGENTS.md from Codex",
                            )
                    else:
                        reason = "Codex reads one file per folder and took %s" % code(os.path.basename(chosen))
                    h.add(path, SKIPPED, reason=reason)
                    continue
                chosen = path
                size = ctx.size(path) or 0
                if ctx.is_blank(path):
                    h.add(path, SKIPPED, reason="empty, so Codex skips it")
                elif remaining <= 0:
                    h.add(path, DROPPED, reason="the %s limit was used up by earlier files" % fmt_bytes(max_bytes))
                    h.cut_bytes += size
                    cut_files.append(path)
                elif size > remaining:
                    h.add(path, TRUNCATED, loaded_bytes=remaining,
                          reason="cut at the %s limit (project_doc_max_bytes)" % fmt_bytes(max_bytes))
                    h.cut_bytes += size - remaining
                    remaining = 0
                    cut_files.append(path)
                else:
                    h.add(path, LOADED)
                    remaining -= size
        if h.cut_bytes:
            total = sum(f["bytes"] for f in h.files if f["scope"] != "user" and f["status"] in (LOADED, TRUNCATED, DROPPED))
            ctx.finding(
                "codex-cuts", h, "problem",
                "Codex reads at most %s of project instructions (project_doc_max_bytes). Your files hold %s, so "
                "it cuts the last %s: %s." % (fmt_bytes(max_bytes), fmt_bytes(total), fmt_bytes(h.cut_bytes),
                                             ", ".join(ctx.show(p) for p in cut_files)),
                "Move long sections into linked docs so Codex keeps the whole file, or raise "
                "project_doc_max_bytes in %s." % ctx.show(config_path),
                ctx.display(cut_files[0]),
                clause="Codex cuts its last %s" % fmt_bytes(h.cut_bytes),
            )

    for directory in ctx.below(ctx.cwd):
        for name in names:
            path = ctx.find(directory, name)
            if path:
                h.add(path, SKIPPED, reason="Codex reads folders from the project root down to the start folder, not below it")

    if not project_seen and trust != "untrusted":
        ctx.finding(
            "codex-reads-nothing", h, "warning",
            "Codex finds no AGENTS.md between the project root and the start folder, so it starts with no "
            "project instructions.",
            "Add an AGENTS.md at the repo root. If your rules live in CLAUDE.md, move them into AGENTS.md and "
            "make CLAUDE.md a single `@AGENTS.md` line, or set project_doc_fallback_filenames = [\"CLAUDE.md\"] "
            "in %s." % ctx.show(config_path),
            clause="Codex reads no project file",
        )
    return h


# ------------------------------------------------------------- Gemini CLI


def gemini_trust(ctx, user_settings, h):
    if dig(user_settings, "security", "folderTrust", "enabled") is False:
        return "trusted"
    path = ctx.env.get("GEMINI_CLI_TRUSTED_FOLDERS_PATH") or os.path.join(ctx.home, ".gemini", "trustedFolders.json")
    rules = ctx.config_json(path, h) if os.path.isfile(path) else {}
    if not isinstance(rules, dict):
        rules = {}
    roots = []
    for key, level in rules.items():
        if level == "TRUST_FOLDER":
            roots.append(key)
        elif level == "TRUST_PARENT":
            roots.append(os.path.dirname(os.path.expanduser(key)))
    if any(within(ctx.cwd, r) for r in roots):
        return "trusted"
    if any(level == "DO_NOT_TRUST" and same_path(ctx.cwd, key) for key, level in rules.items()):
        return "untrusted"
    return "unlisted"


def build_gemini(ctx):
    h = Harness(ctx, "gemini-cli", "Gemini CLI")
    gemini_dir = os.path.join(ctx.home, ".gemini")
    user = ctx.config_json(os.path.join(gemini_dir, "settings.json"), h)
    user = user if isinstance(user, dict) else {}
    trust = gemini_trust(ctx, user, h)
    layers = [user]
    project_path = os.path.join(ctx.cwd, ".gemini", "settings.json")
    project = ctx.config_json(project_path, h) if os.path.isfile(project_path) else None
    if isinstance(project, dict):
        if trust == "untrusted":
            ctx.note("Gemini CLI ignores %s because this folder is not trusted." % ctx.show(project_path), h)
            if dig(project, "context", "fileName") is not None:
                ctx.finding(
                    "gemini-settings-ignored", h, "problem",
                    "%s sets context.fileName, but Gemini CLI ignores project settings in untrusted folders, so it "
                    "reads only its default GEMINI.md." % ctx.show(project_path),
                    "Trust this folder in Gemini CLI (or remove its DO_NOT_TRUST entry in ~/.gemini/trustedFolders.json), "
                    "or set context.fileName in ~/.gemini/settings.json.",
                    ctx.display(project_path),
                    clause="Gemini CLI ignores your .gemini/settings.json because the folder is not trusted",
                )
        else:
            layers.append(project)
            if trust == "unlisted":
                ctx.note("Gemini CLI asks whether to trust this folder on first launch; %s applies only after you "
                         "trust it." % ctx.show(project_path), h)

    names, markers = ["GEMINI.md"], [".git"]
    for layer in layers:
        value = dig(layer, "context", "fileName")
        if as_list(value) and not isinstance(value, bool):
            names = as_list(value) if isinstance(value, list) else [value.strip()]
        boundary = dig(layer, "context", "memoryBoundaryMarkers")
        if isinstance(boundary, list):
            markers = [m for m in boundary if isinstance(m, str)]
        if dig(layer, "context", "includeDirectories"):
            ctx.note("context.includeDirectories adds more folders; their files are not mapped here.", h)
    if names != ["GEMINI.md"]:
        ctx.note("context.fileName: %s." % ", ".join(code(n) for n in names), h)

    for name in names:
        path = os.path.join(gemini_dir, name)
        if os.path.isfile(path):
            h.add(path, LOADED, scope="user", reason="global instructions")

    chain = []
    current = ctx.cwd
    boundary_found = False
    while True:
        chain.append(current)
        if markers and any(os.path.lexists(os.path.join(current, m)) for m in markers):
            boundary_found = True
            break
        parent = os.path.dirname(current)
        if parent == current:
            break
        current = parent
    chain = list(reversed(chain)) if boundary_found else [ctx.cwd]
    if not boundary_found:
        ctx.note("No boundary marker (%s) found; this map assumes Gemini CLI reads only the start folder."
                 % (", ".join(code(m) for m in markers) or "none configured"), h)

    seen = set()
    rejected = []

    def load_import(path, via, anchor):
        h.add(path, LOADED, via=via, anchor=anchor)
        return ctx.inside(path)

    def documented_form(token):
        return token.startswith(("./", "../", "/"))

    project_loaded = False
    for directory in chain:
        for name in names:
            path = ctx.find(directory, name)
            if path and os.path.realpath(path) not in seen:
                seen.add(os.path.realpath(path))
                h.add(path, LOADED)
                project_loaded = True
                follow_imports(ctx, h, path, GEMINI_IMPORT_DEPTH, seen, load_import,
                               accept=documented_form, rejected=rejected)
    if rejected:
        importer, number, token = rejected[0]
        ctx.finding(
            "gemini-import-form", h, "warning",
            "%s imports %s, but Gemini CLI documents imports only as @./file.md, @../file.md, or "
            "@/absolute/file.md, so this map does not count it as loaded%s."
            % (code("%s:%d" % (ctx.display(importer), number), 200), code("@" + token),
               " (%d more like it)" % (len(rejected) - 1) if len(rejected) > 1 else ""),
            "Write the import as %s." % code("@./" + token),
            ctx.display(importer),
            clause="Gemini CLI may skip the %s import in %s" % (code("@" + token), code(os.path.basename(importer))),
        )

    for directory in ctx.below(ctx.cwd):
        for name in names:
            path = ctx.find(directory, name)
            if path:
                h.add(path, ON_DEMAND, reason="loads just in time when Gemini CLI works in %s"
                      % code(ctx.display(directory) + "/", 200))

    if not project_loaded:
        if any(ctx.find(d, "AGENTS.md") for d in chain):
            fix = ("Add .gemini/settings.json with {\"context\": {\"fileName\": [\"AGENTS.md\", \"GEMINI.md\"]}}, "
                   "or add a GEMINI.md whose only line is `@./AGENTS.md`.")
        elif any(ctx.find(d, "CLAUDE.md") for d in chain):
            fix = ("Add .gemini/settings.json with {\"context\": {\"fileName\": [\"CLAUDE.md\", \"GEMINI.md\"]}}, "
                   "or add a GEMINI.md whose only line is `@./CLAUDE.md`.")
        else:
            fix = ("After you add AGENTS.md, add .gemini/settings.json with {\"context\": {\"fileName\": "
                   "[\"AGENTS.md\", \"GEMINI.md\"]}}, or a GEMINI.md whose only line is `@./AGENTS.md`.")
        ctx.finding(
            "gemini-reads-nothing", h, "warning",
            "Gemini CLI looks for %s and finds none between the project root and the start folder, so it starts "
            "with no project instructions." % " or ".join(code(n) for n in names),
            fix,
            clause="Gemini CLI reads no project file",
        )
    return h


# --------------------------------------------------------------- OpenCode


def build_opencode(ctx):
    h = Harness(ctx, "opencode", "OpenCode")
    disable_all = truthy(ctx.env.get("OPENCODE_DISABLE_CLAUDE_CODE"))
    disable_prompt = disable_all or truthy(ctx.env.get("OPENCODE_DISABLE_CLAUDE_CODE_PROMPT"))

    chain = []
    for directory in reversed(ctx.up_dirs(ctx.cwd)):
        chain.append(directory)
        if os.path.lexists(os.path.join(directory, ".git")):
            break
    chain.reverse()
    agents = [p for p in (ctx.find(d, "AGENTS.md") for d in chain) if p]
    claude = [p for p in (ctx.find(d, "CLAUDE.md") for d in chain) if p]
    project_loaded = False
    if agents:
        for path in agents:
            h.add(path, LOADED)
        for path in claude:
            h.add(path, SKIPPED, reason="OpenCode reads CLAUDE.md only when no AGENTS.md exists")
        project_loaded = True
    elif claude:
        for path in claude:
            if disable_all:
                h.add(path, SKIPPED, reason="OPENCODE_DISABLE_CLAUDE_CODE is set")
            else:
                h.add(path, LOADED, reason="used because no AGENTS.md exists")
                project_loaded = True

    config_dir = os.path.join(ctx.env.get("XDG_CONFIG_HOME") or os.path.join(ctx.home, ".config"), "opencode")
    global_agents = os.path.join(config_dir, "AGENTS.md")
    user_claude = os.path.join(ctx.home, ".claude", "CLAUDE.md")
    if os.path.isfile(global_agents):
        h.add(global_agents, LOADED, scope="user", reason="global instructions")
        if os.path.isfile(user_claude):
            h.add(user_claude, SKIPPED, scope="user", reason="the OpenCode global AGENTS.md wins")
    elif os.path.isfile(user_claude):
        if disable_prompt:
            h.add(user_claude, SKIPPED, scope="user", reason="an OPENCODE_DISABLE_CLAUDE_CODE setting is on")
        else:
            h.add(user_claude, LOADED, scope="user", reason="used because OpenCode has no global AGENTS.md")

    configs = []
    for directory in (ctx.repo, ctx.cwd):
        for name in ("opencode.json", "opencode.jsonc"):
            path = os.path.join(directory, name)
            if os.path.isfile(path) and path not in configs:
                configs.append(path)
    custom = ctx.env.get("OPENCODE_CONFIG")
    if custom and os.path.isfile(custom) and os.path.abspath(custom) not in configs:
        configs.append(os.path.abspath(custom))
    for config in configs:
        data = ctx.config_json(config, h)
        items = data.get("instructions") if isinstance(data, dict) else None
        if not isinstance(items, list):
            continue
        base = os.path.dirname(config)
        for item in items:
            if not isinstance(item, str):
                continue
            if re.match(r"^https?://", item):
                ctx.note("OpenCode also fetches %s at startup; remote files are not measured here."
                         % code(item.split("?", 1)[0]), h)
                continue
            pattern = os.path.expanduser(item)
            if not os.path.isabs(pattern):
                pattern = os.path.join(base, pattern)
            matches = sorted(glob.glob(pattern, recursive=True)) if re.search(r"[*?\[]", item) else [pattern]
            matches = [m for m in matches if os.path.isfile(m)]
            if not matches:
                ctx.note("instructions entry %s in %s matches no file." % (code(item), ctx.show(config)), h)
            for match in matches:
                h.add(match, LOADED, via="instructions in %s" % ctx.show(config), anchor=ctx.repo)
                project_loaded = project_loaded or ctx.inside(match)
    for name in ("opencode.json", "opencode.jsonc"):
        data = ctx.config_json(os.path.join(config_dir, name), h)
        if isinstance(data, dict) and isinstance(data.get("instructions"), list) and data["instructions"]:
            ctx.note("Your global OpenCode config lists %s; they are not mapped here."
                     % plural(len(data["instructions"]), "more instructions entry", "more instructions entries"), h)

    if not project_loaded:
        ctx.finding(
            "opencode-reads-nothing", h, "warning",
            "OpenCode finds no AGENTS.md (or CLAUDE.md to fall back on) from the start folder up to the project "
            "root, so it starts with no project instructions.",
            "Add an AGENTS.md at the repo root.",
            clause="OpenCode reads no project file",
        )
    return h


# ----------------------------------------------------------------- Cursor


def build_cursor(ctx):
    h = Harness(ctx, "cursor", "Cursor")
    md_ignored, manual, too_long = [], [], []
    rules_dir = os.path.join(ctx.repo, ".cursor", "rules")
    for path in walk_files(rules_dir):
        lower = path.lower()
        if lower.endswith(".mdc"):
            meta = frontmatter(ctx.read(path))
            always = str(meta.get("alwaysApply", "")).strip().lower() == "true"
            globs = as_list(meta.get("globs"))
            description = meta.get("description")
            description = description.strip() if isinstance(description, str) else ""
            if always:
                h.add(path, LOADED, reason="alwaysApply: true")
            elif globs:
                h.add(path, CONDITIONAL, reason="auto-attached when files match %s" % code(", ".join(globs)))
            elif description:
                h.add(path, CONDITIONAL, reason="the agent decides from the description")
            else:
                h.add(path, ON_DEMAND, reason="manual: applies only when you @-mention it")
                manual.append(path)
            if (ctx.lines(path) or 0) > CURSOR_GUIDE_LINES:
                too_long.append(path)
        elif lower.endswith(".md"):
            h.add(path, SKIPPED, reason="Cursor reads only .mdc files in .cursor/rules")
            md_ignored.append(path)
    for directory in ctx.repo_dirs():
        if directory == ctx.repo:
            continue
        for path in walk_files(os.path.join(directory, ".cursor", "rules")):
            if path.lower().endswith((".mdc", ".md")):
                h.add(path, UNVERIFIED, reason="nested rules folder; Cursor's rules docs describe only the project one")

    for directory in ctx.up_dirs(ctx.cwd):
        if ctx.located(directory):
            path = ctx.find(directory, "AGENTS.md")
            if path:
                h.add(path, LOADED, reason="project root" if directory == ctx.repo else "applies in this subtree")
    for directory in ctx.below(ctx.cwd):
        path = ctx.find(directory, "AGENTS.md")
        if path:
            h.add(path, ON_DEMAND, reason="applies when working in %s" % code(ctx.display(directory) + "/", 200))

    legacy = ctx.find(ctx.repo, ".cursorrules")
    if legacy:
        h.add(legacy, UNVERIFIED, reason="legacy file that Cursor's current docs do not mention, so it may be ignored")
        ctx.finding(
            "cursor-legacy-rules", h, "warning",
            ".cursorrules is the old single-file format; Cursor's current rules docs do not mention it, so Cursor "
            "may ignore it.",
            "Move these rules into .cursor/rules/*.mdc files or into AGENTS.md.",
            ".cursorrules",
            clause="Cursor may ignore your .cursorrules file",
        )
    claude_md = ctx.find(ctx.repo, "CLAUDE.md")
    if claude_md:
        h.add(claude_md, UNVERIFIED, reason="Cursor's docs do not say whether it reads CLAUDE.md")
    ctx.note("User Rules and Team Rules live in the Cursor app, not in files, so they are not shown.", h)
    if not any(f["scope"] == "project" and f["status"] in (LOADED, TRUNCATED, CONDITIONAL) for f in h.files):
        ctx.finding(
            "cursor-reads-nothing", h, "warning",
            "Cursor finds no AGENTS.md and no .mdc rule file, the files its docs say it reads, so it may start "
            "with no project instructions.",
            "Add an AGENTS.md at the repo root; Cursor reads it.",
            clause="Cursor may read no project file",
        )

    if md_ignored:
        listed = ", ".join(ctx.show(p) for p in md_ignored)
        ctx.finding(
            "cursor-md-ignored", h, "problem",
            "Cursor ignores %s in .cursor/rules because they end in .md: %s." % (plural(len(md_ignored), "file"), listed),
            "Rename each file to .mdc and add frontmatter with description, globs, or alwaysApply: true.",
            ctx.display(md_ignored[0]),
            clause="Cursor ignores %s that end in .md" % plural(len(md_ignored), "rule file"),
        )
    if manual:
        ctx.finding(
            "cursor-manual-rule", h, "info",
            "%s no alwaysApply, globs, or description, so it applies only when you @-mention it: %s."
            % ("1 Cursor rule has" if len(manual) == 1 else "%d Cursor rules have" % len(manual),
               ", ".join(ctx.show(p) for p in manual)),
            "Add alwaysApply: true, globs, or a description if the rule should apply on its own.",
            ctx.display(manual[0]),
        )
    for path in too_long:
        ctx.finding(
            "cursor-rule-too-long", h, "info",
            "%s has %d lines; Cursor's guidance is under 500 lines per rule." % (ctx.show(path), ctx.lines(path)),
            "Split it into smaller rules, or move reference material into linked docs.",
            ctx.display(path),
        )
    return h


# ---------------------------------------------------------------- Copilot


def build_copilot(ctx):
    h = Harness(ctx, "copilot", "GitHub Copilot")
    repo_wide = ctx.find(ctx.repo, ".github/copilot-instructions.md")
    if repo_wide:
        h.add(repo_wide, LOADED, reason="repository-wide instructions")
    for path in walk_files(os.path.join(ctx.repo, ".github", "instructions")):
        if not path.endswith(".instructions.md"):
            continue
        globs = as_list(frontmatter(ctx.read(path)).get("applyTo"))
        if any(g in ("**", "**/*", "*") for g in globs):
            h.add(path, LOADED, reason="applyTo: %s" % code(", ".join(globs)))
        elif globs:
            h.add(path, CONDITIONAL, reason="applies to files matching %s" % code(", ".join(globs)))
        else:
            h.add(path, ON_DEMAND, reason="no applyTo, so it is used only when attached")
    for name in ("AGENTS.md", "CLAUDE.md", "GEMINI.md"):
        path = ctx.find(ctx.repo, name)
        if path:
            h.add(path, LOADED, reason="agent instructions")
    for directory in ctx.below(ctx.repo):
        path = ctx.find(directory, "AGENTS.md")
        if path:
            h.add(path, ON_DEMAND, reason="agent instructions for %s" % code(ctx.display(directory) + "/", 200))
    personal = os.path.join(ctx.home, ".copilot")
    path = os.path.join(personal, "copilot-instructions.md")
    if os.path.isfile(path):
        h.add(path, LOADED, scope="user", reason="Copilot CLI personal instructions")
    for path in walk_files(os.path.join(personal, "instructions")):
        if path.endswith(".instructions.md"):
            h.add(path, LOADED, scope="user", reason="Copilot CLI personal instructions (applyTo not read)")
    extra = ctx.env.get("COPILOT_CUSTOM_INSTRUCTIONS_DIRS")
    if extra:
        ctx.note("COPILOT_CUSTOM_INSTRUCTIONS_DIRS adds %s; they are not mapped here."
                 % plural(len([d for d in re.split(r"[,%s]" % re.escape(os.pathsep), extra) if d.strip()]), "folder"), h)
    if any(ctx.read(f["_abs"]) and find_imports(ctx.read(f["_abs"])) for f in h.files if f["scope"] == "project"):
        ctx.note("Copilot CLI also follows @path references inside these files; they are not expanded here.", h)
    ctx.note("Copilot's coding agent, CLI, editor chat, and code review each read a different subset of these "
             "files; see GitHub's custom instructions support table.", h)
    return h


# ------------------------------------------------------------------ Aider


def yaml_list_value(text, key):
    """Value of a top-level key in simple YAML as a list of strings, or None when absent."""
    lines = (text or "").splitlines()
    for index, line in enumerate(lines):
        match = re.match(r"^%s\s*:\s*(.*)$" % re.escape(key), line)
        if not match:
            continue
        value = re.sub(r"\s+#.*$", "", match.group(1)).strip()
        if value.startswith("[") and value.endswith("]"):
            return [unquote(v) for v in value[1:-1].split(",") if v.strip()]
        if value:
            return [unquote(value)]
        items = []
        for following in lines[index + 1:]:
            stripped = following.strip()
            if not stripped or stripped.startswith("#"):
                continue
            if stripped.startswith("- "):
                items.append(unquote(re.sub(r"\s+#.*$", "", stripped[2:])))
                continue
            break
        return items
    return None


def build_aider(ctx):
    h = Harness(ctx, "aider", "Aider")
    reads, source = None, None
    for directory in (ctx.repo, ctx.cwd):
        path = os.path.join(directory, ".aider.conf.yml")
        if os.path.isfile(path):
            values = yaml_list_value(ctx.read(path), "read")
            if values is not None:
                reads, source = values, path
    ctx.note("Aider also loads files you pass with --read or /read, and settings in ~/.aider.conf.yml; "
             "those are not visible here.", h)
    if not reads:
        ctx.finding(
            "aider-not-configured", h, "info",
            "Aider loads no instruction file on its own; it reads one only when .aider.conf.yml lists it under read:.",
            "If you use Aider, add `read: AGENTS.md` to .aider.conf.yml at the repo root.",
        )
        return h
    missing = []
    for item in reads:
        path = os.path.expanduser(item)
        if not os.path.isabs(path):
            path = os.path.join(ctx.cwd, path)
        if os.path.isfile(path):
            h.add(path, LOADED, via="read: in %s" % ctx.show(source), anchor=ctx.cwd)
        else:
            missing.append(item)
    if missing:
        ctx.finding(
            "aider-read-missing", h, "problem",
            "The read: setting in %s names %s that do not exist: %s."
            % (ctx.show(source), plural(len(missing), "file"), ", ".join(code(m) for m in missing)),
            "Fix the file names under read: in %s." % ctx.show(source),
            ctx.display(source),
            clause="Aider's read: setting names a file that does not exist",
        )
    return h


# ------------------------------------------------------------------- main


BUILDERS = (build_claude, build_codex, build_gemini, build_opencode, build_cursor, build_copilot, build_aider)


def build_load_map(repo, cwd=None, home=None, env=None, managed_dir="default"):
    """Compute the load map for every agent. See the module docstring for the privacy rules."""
    env = dict(os.environ if env is None else env)
    home = home or env.get("HOME") or os.path.expanduser("~")
    if managed_dir == "default":
        managed_dir = default_managed_dir()
    ctx = Context(repo, cwd or repo, home, env, managed_dir)
    harnesses = [builder(ctx) for builder in BUILDERS]
    if ctx.scan_capped:
        ctx.note("The nested-folder scan stopped after %s folders; files deeper in the repo may be missing."
                 % fmt_int(MAX_SCAN_DIRS))

    has_files = any(f["scope"] in ("project", "outside") for h in harnesses for f in h.files)
    findings = ctx.findings
    if not has_files:
        findings = [f for f in findings if not f["id"].endswith("-reads-nothing") and f["id"] != "aider-not-configured"]
    order = {hid: index for index, (hid, _name) in enumerate(HARNESSES)}
    findings.sort(key=lambda f: (-SEVERITY_RANK[f["severity"]], order.get(f["harness"], 99)))

    summaries = [h.summary() for h in harnesses]
    if not has_files:
        headline = ("No agent context files in this repo: agents start here with no project instructions "
                    "(AGENTS.md, CLAUDE.md, GEMINI.md, or rules files).")
    else:
        headline = compose_headline(findings, 3) or quiet_headline(summaries)
    return {
        "tool": "agents-md-checker/load_map",
        "version": VERSION,
        "repo": ctx.repo,
        "cwd": ctx.display(ctx.cwd),
        "headline": headline,
        "token_note": TOKEN_NOTE,
        "harnesses": summaries,
        "findings": findings,
        "notes": ctx.notes,
    }


def join_names(names):
    if len(names) <= 2:
        return " and ".join(names)
    return "%s, and %s" % (", ".join(names[:-1]), names[-1])


def headline_clauses(findings):
    rank = {fid: index for index, fid in enumerate(HEADLINE_ORDER)}
    ranked = sorted((f for f in findings if f.get("_clause") and f["id"] in rank), key=lambda f: rank[f["id"]])
    names = dict(HARNESSES)
    certain = [f for f in ranked if f["id"].endswith("-reads-nothing") and f["id"] != "cursor-reads-nothing"]
    blind = [names[f["harness"]] for f in certain]
    clauses = []
    for item in ranked:
        clause = item["_clause"]
        if item in certain and len(blind) > 1:
            clause = "%s read no project file" % join_names(blind)
        if clause not in clauses:
            clauses.append(clause)
    return clauses


def compose_headline(findings, limit):
    return join_clauses(headline_clauses(findings)[:limit])


def quiet_headline(summaries):
    top = max(summaries, key=lambda s: s["loaded_tokens_est"])
    return ("No load problems found: %s starts with the most project and user instructions, about %s tokens "
            "from %s." % (top["name"], fmt_int(top["loaded_tokens_est"]), plural(top["loaded_files"], "file")))


def public(value):
    """Copy of the result for JSON output: internal keys (those starting with _) removed, and every
    string passed through safe_text, because paths, commands, and output come from the repo."""
    if isinstance(value, dict):
        return {k: public(v) for k, v in value.items() if not str(k).startswith("_")}
    if isinstance(value, list):
        return [public(v) for v in value]
    if isinstance(value, str):
        return safe_text(value, 2000)
    return value


def exit_code(findings, fail_on):
    if not fail_on:
        return 0
    threshold = SEVERITY_RANK[fail_on]
    return 1 if any(SEVERITY_RANK[f["severity"]] >= threshold for f in findings) else 0


STATUS_WORDS = {
    LOADED: "loaded",
    TRUNCATED: "cut short",
    DROPPED: "dropped",
    SKIPPED: "skipped",
    ON_DEMAND: "on demand",
    CONDITIONAL: "conditional",
    UNVERIFIED: "unverified",
}


def line_text(text):
    """Report text built from safe parts: keep it on one line."""
    return " ".join("".join(ch if ch.isprintable() else " " for ch in str(text)).split())


def cell(text):
    return line_text(text).replace("|", "/")


def render_table(result):
    lines = [
        "| Agent | Loads at session start | Size | Cut or skipped |",
        "|---|---|---|---|",
    ]
    for h in result["harnesses"]:
        at_start = [f for f in h["files"] if f["status"] in AT_START]
        names = ", ".join(code(f["path"], 200) for f in at_start[:3])
        if len(at_start) > 3:
            names += " and %d more" % (len(at_start) - 3)
        size = "%s, about %s tokens" % (fmt_bytes(h["loaded_bytes"]), fmt_int(h["loaded_tokens_est"])) if at_start else "none"
        cut = []
        if h["cut_bytes"]:
            cut.append("%s cut" % fmt_bytes(h["cut_bytes"]))
        skipped = [f["path"] for f in h["files"] if f["status"] == SKIPPED and f["scope"] == "project"]
        if skipped:
            cut.append("skips " + ", ".join(code(p, 200) for p in skipped[:3]) + (" and more" if len(skipped) > 3 else ""))
        unverified = [f["path"] for f in h["files"] if f["status"] == UNVERIFIED and f["scope"] == "project"]
        if unverified:
            cut.append("unverified: " + ", ".join(code(p, 200) for p in unverified[:3])
                       + (" and more" if len(unverified) > 3 else ""))
        lines.append("| %s | %s | %s | %s |" % (h["name"], cell(names or "nothing"), size, cell("; ".join(cut) or "nothing")))
    return lines


def render_findings(findings):
    lines = []
    for item in findings:
        where = dict(HARNESSES).get(item["harness"], "")
        label = "%s, %s" % (item["severity"].capitalize(), where) if where else item["severity"].capitalize()
        lines.append("- **%s:** %s Fix: %s" % (label, line_text(item["message"]), line_text(item["fix"])))
    return lines


def render_details(result):
    lines = []
    for h in result["harnesses"]:
        if not h["files"] and not h["notes"]:
            continue
        lines.append("**%s**" % h["name"])
        lines.append("")
        for item in h["files"]:
            parts = [STATUS_WORDS[item["status"]]]
            if item["status"] in AT_START:
                parts.append("%s, about %s tokens" % (fmt_bytes(item["loaded_bytes"]), fmt_int(item["tokens_est"])))
            else:
                parts.append(fmt_bytes(item["bytes"]))
            if item["scope"] in ("user", "managed", "outside"):
                parts.append("%s file, size only" % item["scope"])
            text = "- %s: %s" % (code(item["path"], 200), ", ".join(parts))
            detail = "; ".join(line_text(x) for x in (item["via"], item["reason"]) if x)
            if detail:
                text += " (%s)" % detail
            lines.append(text)
        for note in h["notes"]:
            lines.append("- Note: %s" % line_text(note))
        lines.append("")
    return lines


def render_sections(result):
    lines = ["Repo: %s. Start folder: %s. %s" % (code(result["repo"], 400), code(result["cwd"], 200), TOKEN_NOTE), ""]
    lines += ["## What each agent loads", ""] + render_table(result) + [""]
    if result["findings"]:
        lines += ["## Load findings", ""] + render_findings(result["findings"]) + [""]
    lines += ["## Files per agent", ""] + render_details(result)
    for note in result["notes"]:
        lines.append("- Note: %s" % line_text(note))
    return lines


def render_markdown(result):
    return "\n".join(["**%s**" % line_text(result["headline"]), ""] + render_sections(result)).rstrip() + "\n"


def resolve_paths(repo_arg, cwd_arg):
    repo = os.path.abspath(os.path.expanduser(repo_arg))
    if not os.path.isdir(repo):
        raise UsageError("--repo %s is not a folder" % repo_arg)
    if not cwd_arg:
        return repo, repo
    cwd = os.path.abspath(os.path.join(repo, os.path.expanduser(cwd_arg)))
    if not os.path.isdir(cwd) or not (cwd == repo or cwd.startswith(repo + os.sep)):
        raise UsageError("--cwd %s is not a folder inside the repo" % cwd_arg)
    return repo, cwd


def emit(text, out):
    if out:
        with open(out, "w", encoding="utf-8") as handle:
            handle.write(text)
        print("Report written to %s" % out, file=sys.stderr)
    else:
        sys.stdout.write(text)


def add_common_arguments(parser):
    parser.add_argument("--repo", default=".", help="the repo to check (default: the current folder)")
    parser.add_argument("--cwd", help="the folder the agent starts in, relative to --repo (default: the repo root)")
    parser.add_argument("--json", action="store_true", help="print stable machine-readable JSON")
    parser.add_argument("--out", help="write the report to this file instead of printing it")
    parser.add_argument("--fail-on", choices=("problem", "warning"),
                        help="exit 1 when a finding at this level or higher exists")


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="Show which instruction files each coding agent loads from a folder, and what gets cut or "
                    "skipped. Read-only; files outside the repo are measured by size only.")
    add_common_arguments(parser)
    args = parser.parse_args(argv)
    try:
        repo, cwd = resolve_paths(args.repo, args.cwd)
    except UsageError as err:
        print("error: %s" % err, file=sys.stderr)
        return 2
    result = build_load_map(repo, cwd=cwd)
    text = json.dumps(public(result), indent=2) + "\n" if args.json else render_markdown(result)
    emit(text, args.out)
    return exit_code(result["findings"], args.fail_on)


if __name__ == "__main__":
    sys.exit(main())
