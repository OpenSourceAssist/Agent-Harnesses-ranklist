#!/usr/bin/env python3
"""Find the rules your coding agent breaks, count the breaks, and turn them into tested hooks.

  extract   list candidate rules in the context files coding agents load
  count     count how often recent sessions broke each rule in rules.json
  generate  write a hook that enforces the rules, plus its settings entries (dry run by default)
  test      replay recorded breaks (must block) and other recent calls (must allow) through the hook
"""
from __future__ import annotations

import datetime
import json
import os
import re
import shlex
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import rules_guard as G  # noqa: E402
import transcripts as T  # noqa: E402
from safe import code, safe_text  # noqa: E402

VERSION = "1.0.0"


# ---------------------------------------------------------------------------
# rules.json
# ---------------------------------------------------------------------------

class RulesError(Exception):
    """rules.json cannot be used; `problems` lists every reason."""

    def __init__(self, problems):
        self.problems = list(problems)
        super().__init__("\n".join(self.problems))


_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$")
# A rule that matches all of these, or an empty command, would block everything.
_PROBE_COMMANDS = ("ls", "cd src", "git status", "python3 -m pytest -q", "echo hello", "cat README.md",
                   "make test", "node index.js", "zq")
_PROBE_TOOLS = ("Bash", "Read", "Edit", "Write", "Grep", "WebFetch", "mcp__docs__search", "run_shell_command",
                "apply_patch", "Shell")
_PROBE_PATHS = ("README.md", "src/app.py", "docs/guide/intro.md", "package.json", "notes.txt")


def load_rules(path):
    """(checkable rules, advice-only rules) from a rules.json file; RulesError when unusable."""
    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
    except OSError as exc:
        raise RulesError(["cannot read %s: %s" % (path, exc.strerror or type(exc).__name__)])
    except ValueError as exc:
        raise RulesError(["%s is not valid JSON: %s" % (path, exc)])
    return validate(data)


def validate(data):
    items = data.get("rules") if isinstance(data, dict) else data
    if not isinstance(items, list):
        raise RulesError(['rules.json must be a list of rules, or an object with a "rules" list'])
    rules, advice, problems, seen = [], [], [], set()
    for n, raw in enumerate(items, 1):
        problem = _rule_problem(raw, n, seen)
        if problem:
            problems.append(problem)
        elif raw.get("kind") == "advice":
            advice.append(raw)
        else:
            rules.append(raw)
    if problems:
        raise RulesError(problems)
    return rules, advice


def _rule_problem(raw, n, seen):
    if not isinstance(raw, dict):
        return "rule %d: each rule must be a JSON object" % n
    rid = raw.get("id")
    if not isinstance(rid, str) or not _ID_RE.match(rid):
        return "rule %d: id must be 1 to 64 letters, digits, dots, dashes, or underscores" % n
    if rid in seen:
        return "rule %s: the id is used twice" % rid
    seen.add(rid)
    for key in ("text", "source", "message", "tool"):
        if key in raw and not isinstance(raw[key], str):
            return "rule %s: %s must be a string" % (rid, key)
    kind = raw.get("kind")
    if kind == "advice":
        return None
    if kind not in G.CHECKABLE:
        return "rule %s: kind must be forbid_command, protect_path, forbid_tool, or advice" % rid
    pattern = raw.get("pattern")
    if not isinstance(pattern, str) or not pattern.strip():
        return "rule %s: pattern must be a non-empty string" % rid
    try:
        G.tool_kinds(kind, raw.get("tool"))
    except ValueError as exc:
        return "rule %s: %s" % (rid, exc)
    broad = _too_broad(kind, pattern)
    return "rule %s: %s" % (rid, broad) if broad else None


def _too_broad(kind, pattern):
    if kind == "protect_path":
        try:
            G.check_glob(pattern)
        except re.error as exc:
            return "pattern is not a valid glob: %s" % exc
        if all(G.glob_matches(pattern, p, cwd="/work/project") for p in _PROBE_PATHS):
            return "refused: the glob matches every path. Name the file or folder to protect, such as .env or dist/**."
        return None
    trap = _version_trap(pattern)
    if trap:
        return ("refused: the pattern uses %s, which one Python version accepts and another rejects. The hook "
                "runs on whatever python3 the harness finds, as old as Python 3.9, so rewrite it without that." % trap)
    try:
        rx = re.compile(pattern)
    except re.error as exc:
        return "pattern is not a valid regular expression: %s" % exc
    what = "command" if kind == "forbid_command" else "tool"
    probes = _PROBE_COMMANDS if kind == "forbid_command" else _PROBE_TOOLS
    if rx.search("") or all(rx.search(p) for p in probes):
        return ("refused: the pattern matches every %s. Anchor it with ^ and name the %s, "
                "for example ^npm(?:\\s|$)." % (what, what))
    return None


_QUANTIFIER_RE = re.compile(r"\{\d+(?:,\d*)?\}|\{,\d+\}")
_GLOBAL_FLAGS_RE = re.compile(r"\(\?[aiLmsux]+\)")


def _version_trap(pattern):
    """What in the regex reads differently across Python versions, or None:
    atomic groups and possessive quantifiers (errors before Python 3.11), and a
    global flag such as (?i) after the start (an error from Python 3.11 on)."""
    i, n, in_class, content, quantified = 0, len(pattern), False, False, False
    while i < n:
        c = pattern[i]
        if c == "\\":
            i, content, quantified = i + 2, True, False
            continue
        if in_class:
            in_class = c != "]"
            i += 1
            continue
        if c == "[":
            i, in_class, content, quantified = i + 1, True, True, False
            i += 1 if pattern.startswith("^", i) else 0
            i += 1 if pattern.startswith("]", i) else 0
            continue
        if pattern.startswith("(?>", i):
            return "an atomic group (?>...)"
        flags = _GLOBAL_FLAGS_RE.match(pattern, i)
        if flags:
            if content:
                return "a global flag such as (?i) after the start of the pattern"
            i = flags.end()
            continue
        if pattern.startswith("(?#", i):
            end = pattern.find(")", i)
            i = n if end < 0 else end + 1
            continue
        content = True
        if pattern.startswith("(?", i):
            i, quantified = i + 2, False
            continue
        brace = _QUANTIFIER_RE.match(pattern, i) if c == "{" else None
        if brace or c in "*+?":
            if quantified and c == "+":
                return "a possessive quantifier such as ++ or *+"
            quantified = not (quantified and c == "?")  # *? and +? are lazy, not possessive
            i = brace.end() if brace else i + 1
            continue
        quantified = False
        i += 1
    return None


# ---------------------------------------------------------------------------
# Output helpers
# ---------------------------------------------------------------------------

def safe(text, limit=160):
    """Untrusted text (a rule line, a command, a path) as one inert line, for JSON.
    Markdown shows untrusted text only through code(), inside inline code."""
    return safe_text(text, limit)


def _n(count, word, plural=None):
    return "{:,} {}".format(count, word if count == 1 else (plural or word + "s"))


def _home():
    return os.path.normpath(os.path.expanduser("~"))


def _display(path, base=None):
    """A path for the report: relative to `base` when inside it, ~ for the home folder."""
    path, home = os.path.normpath(path), _home()
    if base and (path == base or path.startswith(base.rstrip(os.sep) + os.sep)):
        return os.path.relpath(path, base)
    if path == home or path.startswith(home + os.sep):
        return "~" + path[len(home):]
    return path


def _write_out(text, stream=None):
    stream = stream or sys.stdout
    buffer = getattr(stream, "buffer", None)
    if buffer is not None:
        stream.flush()
        buffer.write(text.encode("utf-8", "replace"))
        buffer.flush()
    else:
        stream.write(text)


def _emit(args, result, render):
    text = json.dumps(result, indent=2) + "\n" if args.json else render(result)
    if args.out:
        with open(args.out, "w", encoding="utf-8", errors="replace") as fh:
            fh.write(text)
        _write_out("Report written to %s\n" % code(args.out, 300))
    else:
        _write_out(text)


# ---------------------------------------------------------------------------
# extract: candidate rules in the context files agents load (facts Q5)
# ---------------------------------------------------------------------------

_KEYWORD_RE = re.compile(r"(?i)\b(never|don['\u2019]t|do not|should not|shouldn['\u2019]t|always|must|only use|"
                         r"avoid|prefer)\b")
_ROOT_FILES = ("CLAUDE.md", ".claude/CLAUDE.md", "CLAUDE.local.md", "AGENTS.md", "AGENTS.override.md",
               ".claude/AGENTS.md", "GEMINI.md", ".github/copilot-instructions.md", ".cursorrules")
_ROOT_FOLDERS = ((".claude/rules", ".md"), (".cursor/rules", ".mdc"), (".github/instructions", ".instructions.md"))
_NESTED_NAMES = {"CLAUDE.md", "CLAUDE.local.md", "AGENTS.md", "AGENTS.override.md", "GEMINI.md"}
_SKIP_DIRS = {"node_modules", "vendor", "dist", "build", "target", "venv", "__pycache__", "site-packages",
              "Pods", "third_party"}
_MAX_FILES, _MAX_DEPTH, _MAX_IMPORT_HOPS = 500, 8, 4
_IMPORT_RE = re.compile(r"(?:^|(?<=\s))@((?:~/|\.{0,2}/)?[A-Za-z0-9_][^\s`'\")\]>]*)")
_SPAN_RE = re.compile(r"`([^`]+)`")
_TOOL_HINT = re.compile(r"(?i)\b(mcp|web ?fetch|web ?search|sub-?agents?|task tool|browser tool)\b")
_FILE_WORDS = re.compile(r"(?i)\b(files?|folders?|director(?:y|ies)|paths?|lockfiles?|secrets?|credentials?)\b")
_FILE_VERBS = re.compile(r"(?i)\b(edit|modify|change|touch|write|read|open|delete|remove|commit)\b")
_SPAN_COMMANDS = {"npm", "pnpm", "yarn", "bun", "npx", "pip", "pip3", "uv", "poetry", "git", "rm", "sudo", "curl",
                  "wget", "docker", "kubectl", "terraform", "make", "cargo", "go", "python", "python3", "node",
                  "brew", "apt", "chmod", "chown", "ssh", "scp", "rsync", "psql", "mysql", "aws", "gcloud", "az",
                  "gh", "helm", "pytest", "jest", "eslint", "prettier", "tsc", "deno", "mvn", "gradle"}
_BARE_COMMANDS = _SPAN_COMMANDS - {"make", "go", "python", "node", "az", "apt"}
_COMMAND_VERBS = re.compile(r"(?i)\b(force[- ]push\w*|push\w*|commit\w*|amend\w*|rebase\w*|install\w*|"
                            r"publish\w*|deploy\w*|merge\w*|reset\w*)\b")


def _looks_like_path(span):
    s = span.strip().strip("<>")
    if "@" in s or (" " in s and "/" not in s and "*" not in s):
        return False
    return "/" in s or s.startswith(".") or "*" in s or bool(re.search(r"\.[A-Za-z0-9]{1,6}$", s))


def _hint(text):
    """command, path, tool, or advice: a first guess at what a hook could check."""
    if _TOOL_HINT.search(text):
        return "tool"
    spans = _SPAN_RE.findall(text)
    if any(_looks_like_path(s) for s in spans) or (_FILE_WORDS.search(text) and _FILE_VERBS.search(text)):
        return "path"
    words = {w.rstrip(".,;:!?") for w in re.findall(r"[a-z0-9][a-z0-9.+_-]*", text)}
    if (any(s.split() and s.split()[0].lower() in _SPAN_COMMANDS for s in spans)
            or words & _BARE_COMMANDS or _COMMAND_VERBS.search(text)):
        return "command"
    return "advice"


def _rule_lines(lines):
    """(line number, keyword, text) for each candidate rule. Skips frontmatter, fenced
    code, and HTML comments; list items under a lead-in such as "Never:" or a
    heading with a rule word inherit it."""
    out, start, fence, in_comment, lead, heading_lead = [], 0, None, False, None, None
    if lines and lines[0].strip() == "---":
        start = next((k + 1 for k in range(1, len(lines)) if lines[k].strip() in ("---", "...")), 0)
    for idx in range(start, len(lines)):
        s = lines[idx].strip()
        if fence:
            fence = None if s.startswith(fence) else fence
            continue
        if s.startswith(("```", "~~~")):
            fence = s[:3]
            continue
        if in_comment:
            in_comment = "-->" not in s
            continue
        if s.startswith("<!--") and "-->" not in s:
            in_comment = True
            continue
        s = re.sub(r"<!--.*?-->", "", s).strip()
        if not s:
            continue
        heading = re.match(r"^#{1,6}\s+(.*)$", s)
        if heading:
            lead, heading_lead = None, heading.group(1) if _KEYWORD_RE.search(heading.group(1)) else None
            continue
        item = re.match(r"^(?:[-*+]|\d+[.)])\s+(.*)$", s)
        text = item.group(1).strip() if item else s
        if not item:
            lead = None
            if text.endswith(":") and len(text) <= 60 and _KEYWORD_RE.search(text):
                lead = text[:-1].strip()
                continue
        m = _KEYWORD_RE.search(text)
        inherited = lead or heading_lead
        if m:
            out.append((idx + 1, m.group(1), text))
        elif item and inherited:
            out.append((idx + 1, _KEYWORD_RE.search(inherited).group(1), "%s: %s" % (inherited, text)))
    return [(n, k.lower().replace("\u2019", "'"), t) for n, k, t in out]


def _user_context_files(home):
    claude = os.environ.get("CLAUDE_CONFIG_DIR") or os.path.join(home, ".claude")
    codex = os.environ.get("CODEX_HOME") or os.path.join(home, ".codex")
    override = os.path.join(codex, "AGENTS.override.md")
    use_override = os.path.isfile(override) and os.path.getsize(override) > 0
    files = [os.path.join(claude, "CLAUDE.md")] + _walk_suffix(os.path.join(claude, "rules"), ".md")
    files.append(override if use_override else os.path.join(codex, "AGENTS.md"))
    files += [os.path.join(home, ".gemini", "GEMINI.md"), os.path.join(home, ".config", "opencode", "AGENTS.md"),
              os.path.join(home, ".copilot", "copilot-instructions.md")]
    return files


def _walk_suffix(folder, suffix):
    found = []
    for dirpath, dirs, files in os.walk(folder):
        dirs.sort()
        found.extend(os.path.join(dirpath, f) for f in sorted(files) if f.endswith(suffix))
    return found


def _nested_context_files(repo):
    found, root_depth = [], repo.rstrip(os.sep).count(os.sep)
    for dirpath, dirs, files in os.walk(repo):
        depth = dirpath.count(os.sep) - root_depth
        dirs[:] = sorted(d for d in dirs if not d.startswith(".") and d not in _SKIP_DIRS) \
            if depth < _MAX_DEPTH else []
        if dirpath != repo:
            found.extend(os.path.join(dirpath, f) for f in sorted(files) if f in _NESTED_NAMES)
        if len(found) >= _MAX_FILES:
            break
    return found


def extract(repo, user=False, files=()):
    """Candidate rules in the repo's context files (plus the user's files with
    `user`, plus any `files`), following @path imports."""
    repo, home = os.path.normpath(os.path.abspath(repo)), _home()
    queue = [os.path.join(repo, f) for f in _ROOT_FILES]
    for folder, suffix in _ROOT_FOLDERS:
        queue += _walk_suffix(os.path.join(repo, folder), suffix)
    queue += _nested_context_files(repo)
    if user:
        queue += _user_context_files(home)
    queue += [os.path.abspath(f) for f in files]
    seen, hops, read, candidates, notes, skipped = set(), {}, [], [], [], 0
    allowed = [os.path.realpath(repo)] + ([os.path.realpath(home)] if user else [])
    while queue:
        path = os.path.normpath(queue.pop(0))
        if path in seen or not os.path.isfile(path):
            continue
        seen.add(path)
        if len(read) >= _MAX_FILES:
            notes.append("Stopped after %d context files." % _MAX_FILES)
            break
        try:
            with open(path, encoding="utf-8", errors="replace") as fh:
                lines = fh.read(4 << 20).splitlines()
        except OSError as exc:
            notes.append("Cannot read %s (%s)." % (code(_display(path, repo)), type(exc).__name__))
            continue
        shown = safe(_display(path, repo))
        read.append({"path": shown, "lines": len(lines)})
        for number, keyword, text in _rule_lines(lines):
            candidates.append({"file": shown, "line": number, "keyword": keyword, "hint": _hint(text),
                               "text": safe(text)})
        if hops.get(path, 0) < _MAX_IMPORT_HOPS:
            for target in _imports(lines, os.path.dirname(path), home):
                real = os.path.realpath(target)
                if not any(real == a or real.startswith(a.rstrip(os.sep) + os.sep) for a in allowed):
                    skipped += 1  # imports are followed only inside the project, or home with --user
                    continue
                hops.setdefault(target, hops.get(path, 0) + 1)
                queue.append(target)
    if skipped:
        notes.append("Skipped %s outside the project%s." % (_n(skipped, "import"), " and your home folder" if user
                                                            else " (add --user to follow imports into your home folder)"))
    checkable = sum(1 for c in candidates if c["hint"] != "advice")
    if not read:
        headline = ("No context files found in %s. Pass --file to read one by name, or --user to read your "
                    "personal ones." % code(_display(repo), 300))
    elif not candidates:
        headline = "Found no rule lines (never, always, must, do not, avoid, prefer) in %s." % _n(
            len(read), "context file")
    else:
        headline = "Found %s in %s; %d %s a command, a path, or a tool, so a hook can check %s." % (
            _n(len(candidates), "candidate rule"), _n(len(read), "context file"), checkable,
            "names" if checkable == 1 else "name", "it" if checkable == 1 else "them")
    return {"tool": "rules-to-guards", "version": VERSION, "command": "extract", "headline": headline,
            "repo": safe(_display(repo), 300), "files": read, "candidates": candidates, "notes": notes}


def _imports(lines, folder, home):
    """Files named by @path imports outside fenced code (Claude Code, Gemini CLI, Copilot)."""
    found, fence = [], False
    for line in lines:
        if line.strip().startswith(("```", "~~~")):
            fence = not fence
            continue
        if fence:
            continue
        for ref in _IMPORT_RE.findall(_SPAN_RE.sub("", line)):
            ref = ref.rstrip(".,;:")
            path = home + ref[1:] if ref.startswith("~/") else os.path.join(folder, ref)
            if os.path.isfile(path):
                found.append(os.path.normpath(path))
    return found


def render_extract(result):
    out = ["**%s**" % result["headline"], ""]
    if result["files"]:
        out += ["Files read: " + ", ".join("%s (%s)" % (code(f["path"]), _n(f["lines"], "line"))
                                         for f in result["files"]) + ".", ""]
    if result["candidates"]:
        out += ["| Where | Hint | Rule |", "|---|---|---|"]
        out += ["| %s | %s | %s |" % (code("%s:%d" % (c["file"], c["line"])), c["hint"], code(c["text"]))
                for c in result["candidates"]]
        out += ["", "Hints: command, path, and tool rules can become hooks; advice rules stay as text.", "",
                "Next: write the checkable rules to rules.json (schema and tested patterns: "
                "references/checkable-rules.md), then count the breaks with `rules.py count --rules rules.json`."]
    if result["notes"]:
        out += [""] + ["Note: %s" % n for n in result["notes"]]
    return "\n".join(out).rstrip() + "\n"


# ---------------------------------------------------------------------------
# count: breaks per rule across recent sessions
# ---------------------------------------------------------------------------

HARNESS_NAMES = {"claude-code": "Claude Code", "codex": "Codex", "gemini-cli": "Gemini CLI", "opencode": "OpenCode",
                 "cursor": "Cursor"}
_SINCE_RE = re.compile(r"^(\d+)([dwh])$")


def parse_since(value):
    """(days, label, cutoff) for 30d, 2w, 12h, or a YYYY-MM-DD date. The cutoff is
    transcripts.cutoff(days): the UTC time that call timestamps compare against."""
    now = datetime.datetime.now(datetime.timezone.utc)
    text = str(value).strip().lower()
    m = _SINCE_RE.match(text)
    if m:
        amount, unit = int(m.group(1)), m.group(2)
        days = amount * {"d": 1.0, "w": 7.0, "h": 1 / 24.0}[unit]
        label = "the last " + (_n(amount, "hour") if unit == "h" else _n(int(days), "day"))
    else:
        try:
            start = datetime.datetime.strptime(text, "%Y-%m-%d").replace(tzinfo=datetime.timezone.utc)
        except ValueError:
            raise ValueError("--since takes days such as 30d, weeks such as 2w, hours such as 12h, "
                             "or a date such as 2026-09-01")
        days, label = (now - start).total_seconds() / 86400.0, "since " + text
    if days <= 0:
        raise ValueError("--since must reach back into the past")
    return days, label, T.cutoff(days)


class Call:
    """One recorded tool call and where it happened."""
    __slots__ = ("harness", "session", "cwd", "ts", "tool")

    def __init__(self, harness, session, cwd, ts, tool):
        self.harness, self.session, self.cwd, self.ts, self.tool = harness, session, cwd, ts, tool

    def shape(self):
        """The call in the shape the hook sees (rules_guard.normalize_hook_input)."""
        t = self.tool
        return {"kind": t.kind, "name": t.name, "command": t.command, "paths": list(t.paths), "cwd": self.cwd,
                "root": self.cwd}


def load_calls(since, harness="all", project=None):
    """(calls in the window, oldest first, and the number of unreadable lines). A
    forked session repeats the calls it copied; unique_events counts them once."""
    days, _label, since_ts = parse_since(since)
    sessions, skipped = [], 0
    for h, path in T.find_sessions(harness=harness, since_days=days, project=project):
        s = T.load_session(h, path)
        skipped += len(s.warnings)
        s.events = [e for e in s.events if e.kind == "tool" and e.tool is not None]
        for e in s.events:
            e.tool.output = ""  # results are not needed; free the memory
        sessions.append(s)
    # A recently modified file can hold calls older than the window; since= drops them.
    calls = [Call(s.harness, s.id, s.cwd, _date_time(e.ts or e.tool.ts), e.tool)
             for s, e in T.unique_events(sessions, since=since_ts)]
    calls.sort(key=lambda c: c.ts)
    return calls, skipped


def _applies(rule, kind):
    return rule.kinds is None or kind in rule.kinds


def _label(raw):
    return code(str(raw.get("text") or "").strip().rstrip(".") or raw["id"], 80)


def _excerpt(call, match):
    """What the example shows: the command, the file, or the tool name. When a
    command is too long to show whole, the example shows the part that matched."""
    if call.tool.kind == "shell":
        command = call.tool.command or ""
        if isinstance(match, str) and match.strip() and match.strip() != command.strip() \
                and len(safe(command, len(command) + 10)) > 160:
            return safe("in a longer command: " + match)
        return safe(command)
    if call.tool.kind in ("read", "edit", "write") and isinstance(match, str) and match != call.tool.name:
        return safe("%s %s" % (call.tool.name, _display(match, call.cwd or None)))
    return safe(call.tool.name)


_DATE_TIME_RE = re.compile(r"^\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}")


def _date_time(ts):
    """A session timestamp, or "" when it does not start with a date and time:
    reports show its first characters as written."""
    return ts if isinstance(ts, str) and _DATE_TIME_RE.match(ts) else ""


def _when(ts):
    return ts[:16].replace("T", " ") + " UTC" if ts else "unknown time"


def classify(rules, calls):
    """Per rule: the calls it checked and the calls that broke it."""
    compiled = G.compile_rules(rules)
    stats = [{"rule": r, "raw": raw, "checked": 0, "hits": []} for r, raw in zip(compiled, rules)]
    by_id = {st["rule"].id: st for st in stats}
    for call in calls:
        shape = call.shape()
        for st in stats:
            if _applies(st["rule"], shape["kind"]):
                st["checked"] += 1
        for hit in G.matches(compiled, shape):
            by_id[hit["rule"]]["hits"].append((call, hit["match"]))
    return stats


def count(rules, advice=(), since="30d", harness="all", project=None):
    _days, label, cutoff = parse_since(since)
    calls, skipped = load_calls(since, harness, project)
    stats = classify(rules, calls)
    results, warnings = [], []
    for st in stats:
        r, raw, hits = st["rule"], st["raw"], st["hits"]
        newest = sorted(hits, key=lambda h: h[0].ts, reverse=True)
        by_harness = {}
        for call, _m in hits:
            by_harness[call.harness] = by_harness.get(call.harness, 0) + 1
        stopped = sum(1 for call, _m in hits if call.tool.denied)
        results.append({
            "id": r.id, "source": safe(raw.get("source") or "", 80), "text": safe(raw.get("text") or ""),
            "kind": r.kind, "tool": raw.get("tool") or G._DEFAULT_TOOL[r.kind], "pattern": raw["pattern"],
            "violations": len(hits), "ran": len(hits) - stopped, "stopped": stopped,
            "sessions": len({(c.harness, c.session) for c, _m in hits}), "by_harness": by_harness,
            "last": newest[0][0].ts if newest else "", "checked": st["checked"],
            "share": round(len(hits) / float(st["checked"]), 3) if st["checked"] else 0.0,
            "examples": [{"harness": c.harness, "session": safe(c.session[:8], 8), "time": _when(c.ts),
                          "tool": safe(c.tool.name, 60), "excerpt": _excerpt(c, m),
                          "stopped": c.tool.denied} for c, m in newest[:3]],
        })
        if st["checked"] >= 20 and len(hits) > st["checked"] / 2.0:
            warnings.append("Rule %s matches %d%% of the calls it checks (%d of %d): the pattern may be too broad. "
                            "Check its examples before you make a hook." % (
                                r.id, round(100.0 * len(hits) / st["checked"]), len(hits), st["checked"]))
    notes = []
    if harness == "cursor":
        notes.append("Cursor keeps no usable transcripts, so its calls cannot be counted.")
    if skipped:
        notes.append("%s in the session files could not be read and were skipped." % _n(skipped, "line"))
    if any(c.harness == "opencode" for c in calls):
        notes.append("OpenCode sessions are read on a best-effort basis, not verified on a real install.")
    sessions = {}
    for c in calls:
        sessions.setdefault(c.harness, set()).add(c.session)
    return {"tool": "rules-to-guards", "version": VERSION, "command": "count",
            "headline": _count_headline(results, rules, calls, label), "window": label, "since": cutoff + "Z",
            "since_option": safe(since, 40),
            "harness": harness, "project": safe(_display(os.path.abspath(project)), 300) if project else None,
            "sessions": sum(len(v) for v in sessions.values()),
            "by_harness": {h: len(v) for h, v in sorted(sessions.items())}, "tool_calls": len(calls),
            "rules": results, "advice_only": [{"id": a["id"], "source": safe(a.get("source") or "", 80),
                                               "text": safe(a.get("text") or "")} for a in advice],
            "warnings": warnings, "notes": notes}


def _count_headline(results, rules, calls, label):
    if not calls:
        return "No agent sessions found in %s, so there is nothing to count." % label
    broken = sorted((r for r in results if r["violations"]), key=lambda r: -r["violations"])
    total = sum(r["violations"] for r in broken)
    raw = {r["id"]: r for r in rules}
    if not broken:
        return "Your agent broke none of your %s in %s (%s checked)." % (
            _n(len(results), "checkable rule"), label, _n(len(calls), "tool call"))
    top = broken[0]
    if len(results) == 1:
        return "Your agent broke %s %s in %s, most recently on %s." % (
            _label(raw[top["id"]]), _n(total, "time"), label, top["last"][:10])
    return "Your agent broke %d of your %s %s in %s; %s leads with %d." % (
        len(broken), _n(len(results), "checkable rule"), _n(total, "time"), label, _label(raw[top["id"]]),
        top["violations"])


def render_count(res):
    out = ["**%s**" % res["headline"], ""]
    if res["tool_calls"]:
        where = "in sessions under %s" % code(res["project"], 300) if res["project"] else "across all projects"
        split = ", ".join("%s %s" % (HARNESS_NAMES.get(h, h), "{:,}".format(n)) for h, n in res["by_harness"].items())
        out += ["Checked %s tool calls in %s from %s (%s), %s." % (
            "{:,}".format(res["tool_calls"]), _n(res["sessions"], "session"), res["window"], split, where), ""]
        out += ["| Rule | Breaks | Ran | Stopped | Sessions | Last | Source |", "|---|---|---|---|---|---|---|"]
        for r in res["rules"]:
            out.append("| %s | %d | %d | %d | %d | %s | %s |" % (
                r["id"], r["violations"], r["ran"], r["stopped"], r["sessions"], r["last"][:10] or "never",
                code(r["source"]) if r["source"] else "-"))
        out += ["", "Stopped means you, a permission rule, a hook, or an auto reviewer refused the call before it ran."]
        examples = [(r["id"], e) for r in res["rules"] for e in r["examples"]]
        if examples:
            out += ["", "Examples, newest first (secrets masked, cut to 160 characters):"]
            out += ["- %s, %s, %s, %s: %s" % (rid, HARNESS_NAMES.get(e["harness"], e["harness"]), e["time"],
                                            "stopped (%s)" % e["stopped"] if e["stopped"] else "ran", code(e["excerpt"]))
                    for rid, e in examples]
        out += ["", "Patterns:"] + ["- %s (%s, %s): %s" % (r["id"], r["kind"], r["tool"], code(r["pattern"], 2000))
                                    for r in res["rules"]]
    for w in res["warnings"]:
        out += ["", "Warning: " + w]
    if res["advice_only"]:
        out += ["", "Advice only, not counted: " + ", ".join(
            "%s (%s)" % (a["id"], code(a["source"]) if a["source"] else "no source") for a in res["advice_only"]) + "."]
    broken = [r["id"] for r in res["rules"] if r["violations"]]
    if broken:
        same = (" --project %s" % shlex.quote(res["project"]) if res["project"] else "") + \
            (" --since %s" % res["since_option"] if res["since_option"] != "30d" else "")
        out += ["", "Next: replay the same sessions through a hook with `rules.py test --rules <rules.json> --only %s%s`, "
                    "then preview the install with `rules.py generate`." % (",".join(broken), same)]
    for n in res["notes"]:
        out += ["", "Note: " + n]
    return "\n".join(out).rstrip() + "\n"


# ---------------------------------------------------------------------------
# generate: the hook script plus each harness's settings entry (facts Q2)
# ---------------------------------------------------------------------------

HOOK_NAME = "rules_guard.py"
_RULES_LINE = "RULES = []  # rules-to-guards: `rules.py generate` writes your rules here\n"
# (call kind, tool name) per harness, in matcher order.
_TOOL_NAMES = {
    "claude-code": [("shell", "Bash"), ("shell", "Monitor"), ("read", "Read"), ("edit", "Edit"), ("edit", "MultiEdit"),
                    ("edit", "NotebookEdit"), ("write", "Write")],
    "codex": [("shell", "Bash"), ("edit", "apply_patch"), ("write", "apply_patch")],
    "gemini-cli": [("shell", "run_shell_command"), ("read", "read_file"), ("read", "read_many_files"),
                   ("edit", "replace"), ("edit", "edit"), ("write", "write_file")],
    "cursor": [("shell", "Shell"), ("read", "Read"), ("edit", "Write"), ("edit", "Delete"), ("write", "Write")],
}
_EVENTS = {"claude-code": "PreToolUse", "codex": "PreToolUse", "gemini-cli": "BeforeTool", "cursor": "preToolUse"}
_HARNESS_NOTES = {
    "claude-code": "Claude Code: start a new session so it loads the new settings.",
    "codex": "Codex runs a new or changed hook only after you review and trust it in /hooks, and it loads "
             "project hooks only in trusted projects.",
    "gemini-cli": "Gemini CLI treats a new or changed project hook as untrusted until you accept it.",
    "cursor": "Cursor runs project hooks only in trusted workspaces. It also runs Claude Code hooks from "
              "the .claude settings files by default, so with both installed the hook can run twice for Read and "
              "Write calls, which is harmless. Not verified on a real Cursor install.",
}


def _settings_path(harness, scope, project):
    """(settings file, the folder it must stay inside)."""
    home = _home()
    if harness == "claude-code":  # the project entry names an absolute path, so it goes in the unshared file
        if scope == "user":
            folder = os.environ.get("CLAUDE_CONFIG_DIR") or os.path.join(home, ".claude")
            return os.path.join(folder, "settings.json"), folder
        return os.path.join(project, ".claude", "settings.local.json"), project
    if harness == "codex":
        folder = (os.environ.get("CODEX_HOME") or os.path.join(home, ".codex")) if scope == "user" \
            else os.path.join(project, ".codex")
        return os.path.join(folder, "hooks.json"), folder if scope == "user" else project
    folder = os.path.join(home if scope == "user" else project, ".gemini" if harness == "gemini-cli" else ".cursor")
    name = "settings.json" if harness == "gemini-cli" else "hooks.json"
    return os.path.join(folder, name), folder if scope == "user" else project


def _matcher(harness, compiled):
    """None when some rule needs every tool; else the tool names, or [] when this
    harness has no tool of the kinds the rules check."""
    if any(r.kinds is None for r in compiled):
        return None
    kinds = set().union(*[r.kinds for r in compiled]) if compiled else set()
    names = []
    for kind, name in _TOOL_NAMES[harness]:
        if kind in kinds and name not in names:
            names.append(name)
    return names


def _entry(harness, command, names):
    """The settings entry that runs the hook; names None means every tool."""
    if harness == "cursor":
        entry = {"command": command, "type": "command", "timeout": 10}
        if names is not None:
            entry["matcher"] = "^(%s)$" % "|".join(names)
        return entry
    handler = {"type": "command", "command": command, "timeout": 10}
    if harness == "gemini-cli":  # Gemini CLI timeouts are in milliseconds
        handler.update(name="rules-to-guards", timeout=10000,
                       description="Blocks tool calls that break the rules in rules.json")
    if harness == "claude-code":  # an exact list of tool names
        matcher = "*" if names is None else "|".join(names)
    elif names is not None:
        matcher = "^(%s)$" % "|".join(names)
    else:
        matcher = "*" if harness == "gemini-cli" else None
    return {"matcher": matcher, "hooks": [handler]} if matcher else {"hooks": [handler]}


def hook_command(hook, harness):
    """The settings command: the hook runs by its #!/usr/bin/env python3 line. A
    missing file exits 127 and a file that cannot run exits 126, and neither code
    blocks, with or without a shell. (python3 <file> would exit 2 on a missing
    file, and exit 2 means block.)"""
    return "%s --harness %s" % (shlex.quote(hook), harness)


def _ours(command):
    return isinstance(command, str) and HOOK_NAME in command and "--harness" in command


def _merge(data, harness, entry):
    """Settings with every rules-to-guards hook removed, then `entry` added (unless None)."""
    if not isinstance(data, dict):
        raise ValueError("the file is not a JSON object")
    data = json.loads(json.dumps(data))
    hooks = data.get("hooks", {})
    if not isinstance(hooks, dict):
        raise ValueError('"hooks" is not a JSON object')
    event = _EVENTS[harness]
    groups = hooks.get(event, [])
    if not isinstance(groups, list):
        raise ValueError('"hooks.%s" is not a list' % event)
    kept = []
    for group in groups:
        if harness == "cursor":
            if not (isinstance(group, dict) and _ours(group.get("command"))):
                kept.append(group)
            continue
        inner = group.get("hooks") if isinstance(group, dict) else None
        if isinstance(inner, list):
            mine = [h for h in inner if isinstance(h, dict) and _ours(h.get("command"))]
            if mine:
                rest = [h for h in inner if h not in mine]
                if not rest:
                    continue
                group = dict(group, hooks=rest)
        kept.append(group)
    if entry is not None:
        kept.append(entry)
        if harness == "cursor":
            data.setdefault("version", 1)
    if kept:
        hooks[event] = kept
    else:
        hooks.pop(event, None)
    if hooks:
        data["hooks"] = hooks
    else:
        data.pop("hooks", None)
    return data


def _json_style(text):
    """(indent, trailing newline) of an existing JSON file, so that a rewrite
    changes only the hooks and keeps the file's own spacing."""
    for line in text.splitlines()[1:]:
        body = line.lstrip(" \t")
        if body and len(body) < len(line):
            space = line[:len(line) - len(body)]
            return ("\t" if space.startswith("\t") else len(space)), text.endswith("\n")
    return 2, text.endswith("\n") or not text.strip()


def _dump(data, style):
    indent, newline = style
    return json.dumps(data, indent=indent, ensure_ascii=False) + ("\n" if newline else "")


def hook_source(rules):
    """The hook script with the rules embedded. Strings from context files are
    kept on one safe line, because the hook shows its message to the agent."""
    import pprint
    embedded = []
    for r in rules:
        item = {"id": r["id"], "kind": r["kind"], "tool": r.get("tool") or G._DEFAULT_TOOL[r["kind"]],
                "pattern": r["pattern"], "source": safe(r.get("source") or "", 80),
                "message": safe(r.get("message") or r.get("text") or "", 300)}
        embedded.append(item)
    with open(os.path.join(HERE, HOOK_NAME), encoding="utf-8") as fh:
        template = fh.read()
    if _RULES_LINE not in template:
        raise RuntimeError("the hook template has lost its RULES line")
    block = ("# Generated by rules-to-guards %s. To change the rules, edit rules.json and run generate again.\n"
             "RULES = %s\n" % (VERSION, pprint.pformat(embedded, width=110, sort_dicts=False)))
    source = template.replace(_RULES_LINE, block, 1)
    compile(source, HOOK_NAME, "exec")
    return source


def hook_rules(text):
    """The RULES list embedded in an existing hook's source, or None."""
    import ast
    try:
        tree = ast.parse(text)
    except SyntaxError:
        return None
    for node in tree.body:
        if isinstance(node, ast.Assign) and any(isinstance(t, ast.Name) and t.id == "RULES" for t in node.targets):
            try:
                value = ast.literal_eval(node.value)
            except ValueError:
                return None
            return value if isinstance(value, list) else None
    return None


def _literal_prefix(pattern):
    """The command words of a pattern such as ^git\\s+push\\s+--force\\b, or None
    when the pattern is more than a plain prefix."""
    if not pattern.startswith("^"):
        return None
    body = pattern[1:]
    for end in (r"(?:\s|$)", r"(\s|$)", r"(?=\s|$)", r"\b"):
        if body.endswith(end):
            body = body[:-len(end)]
            break
    words = []
    for word in re.split(r"\\s\+|\\s| ", body):
        plain = re.sub(r"\\([.\-+])", r"\1", word)
        if not plain or not re.match(r"^[A-Za-z0-9_.:@=+,/-]+$", plain):
            return None
        words.append(plain)
    return words or None


def _permission_rules(rules, harnesses):
    out = []
    for r in rules:
        words = _literal_prefix(r["pattern"]) if r["kind"] == "forbid_command" else None
        if not words:
            continue
        reason = safe(r.get("message") or r.get("text") or "", 200)
        for h in harnesses:
            if h == "claude-code":
                out.append({"harness": h, "rule": r["id"], "entry": "Bash(%s *)" % " ".join(words)})
            elif h == "codex":
                out.append({"harness": h, "rule": r["id"], "entry": "prefix_rule(pattern=[%s], decision=\"forbidden\", "
                            "justification=%s)" % (", ".join(json.dumps(w) for w in words), json.dumps(reason))})
            elif h == "gemini-cli":
                out.append({"harness": h, "rule": r["id"], "entry": "[[rule]] toolName = \"run_shell_command\" "
                            "commandPrefix = %s decision = \"deny\" priority = 500" % json.dumps(" ".join(words))})
            elif h == "cursor" and len(words) == 1:
                out.append({"harness": h, "rule": r["id"], "entry": "Shell(%s)" % words[0]})
    return out


_PERMISSION_WHERE = {
    "claude-code": "permissions.deny in the same settings file",
    "codex": "a .rules file next to your Codex config, such as ~/.codex/rules/default.rules",
    "gemini-cli": "a policy file such as ~/.gemini/policies/rules-to-guards.toml, one line per key",
    "cursor": "permissions.deny in ~/.cursor/cli-config.json (Cursor CLI only)",
}


def _diff_text(before, after, path):
    """A unified diff with secrets masked, and each line kept printable and cut to
    500 characters. Settings files hold API keys and passwords."""
    import difflib
    lines = difflib.unified_diff(before.splitlines(), after.splitlines(), path, path, lineterm="")
    return "".join("".join(ch if ch.isprintable() or ch == "\t" else " " for ch in T.redact(line)[:500]).rstrip()
                   + "\n" for line in lines)


def _outside(path, folder):
    """The real file `path` leads to when a symlink takes it outside `folder`, else None."""
    real, top = os.path.realpath(path), os.path.realpath(folder)
    return None if real == top or real.startswith(top.rstrip(os.sep) + os.sep) else real


def _cannot_update(path, harness, why, entry):
    return ValueError("cannot update %s: %s. Add this entry by hand under hooks.%s: %s" % (
        safe(_display(path), 300), why, _EVENTS[harness], json.dumps(entry, ensure_ascii=False)))


def plan(rules, harnesses, scope="project", project=".", hook_path=None, uninstall=False, replace=False):
    """Every file change `generate` makes. Raises ValueError on a settings file it
    cannot parse or a hook file that rules-to-guards did not write."""
    project = os.path.normpath(os.path.abspath(project))
    home = _home()
    hook = os.path.abspath(hook_path) if hook_path else os.path.join(
        home if scope == "user" else project, ".agents", "hooks", HOOK_NAME)
    hook_root = os.path.dirname(hook) if hook_path else (os.path.join(home, ".agents") if scope == "user" else project)
    old_source = None
    if os.path.isfile(hook):
        with open(hook, encoding="utf-8", errors="replace") as fh:
            old_source = fh.read()
        if "rules-to-guards hook" not in old_source:
            raise ValueError("%s exists and is not a rules-to-guards hook. Move it, or pass --hook-path to write the "
                             "hook somewhere else." % safe(_display(hook), 300))
    old_rules = hook_rules(old_source) if old_source else []
    if old_source and old_rules is None and not replace and not uninstall:
        raise ValueError("cannot read the rules inside %s; run again with --replace to write it from rules.json "
                         "alone." % safe(_display(hook), 300))
    final, kept, dropped = list(rules), [], []
    if not uninstall and old_rules:
        new_ids = {r["id"] for r in rules}
        if replace:
            dropped = [r.get("id") for r in old_rules if r.get("id") not in new_ids]
        else:
            carried = [r for r in old_rules if r.get("id") not in new_ids]
            try:
                validate(carried)
            except RulesError as exc:
                raise ValueError("the hook already holds rules that are no longer valid (%s); run again with "
                                 "--replace to drop them." % "; ".join(exc.problems))
            kept = [r["id"] for r in carried]
            by_id = {r["id"]: r for r in rules}
            final = [by_id.pop(r["id"], r) for r in old_rules if isinstance(r, dict)] + \
                [r for r in rules if r["id"] in by_id]
    compiled = G.compile_rules(final)
    changes, notes, redirects = [], [], []
    for h in harnesses:
        path, folder = _settings_path(h, scope, project)
        names = _matcher(h, compiled)
        if not uninstall and names == []:
            notes.append("%s has no tool of the kinds these rules check, so no hook is added there." %
                         HARNESS_NAMES[h])
            continue
        entry = None if uninstall else _entry(h, hook_command(hook, h), names)
        before = ""
        if os.path.exists(path):
            with open(path, encoding="utf-8", errors="replace") as fh:
                before = fh.read()
        try:
            data = json.loads(before) if before.strip() else {}
        except ValueError as exc:
            comments = re.search(r"(?m)^\s*//|/\*", before)
            why = ("it has comments, which %s allows, and generate cannot rewrite it without losing them" %
                   HARNESS_NAMES[h] if comments and h == "gemini-cli" else
                   "it is not valid JSON (%s)" % ("it has comments" if comments else exc))
            raise _cannot_update(path, h, why, entry or {})
        try:
            after_data = _merge(data, h, entry)
        except ValueError as exc:
            raise _cannot_update(path, h, str(exc), entry or {})
        if before.strip() and data == after_data:
            action = "unchanged"
        elif not before.strip() and uninstall:
            continue
        else:
            action = "update" if before.strip() else "create"
        real = _outside(path, folder)
        if real and action != "unchanged":
            redirects.append((path, real))
        changes.append({"harness": h, "path": path, "action": action, "before": before,
                        "after": _dump(after_data, _json_style(before))})
    hook_change = None
    if not uninstall and changes:
        source = hook_source(final)
        action = "unchanged" if old_source == source else ("update" if old_source else "create")
        real = _outside(hook, hook_root)
        if real and action != "unchanged":
            redirects.append((hook, real))
        hook_change = {"path": hook, "action": action, "source": source}
    return {"hook": hook_change, "hook_path": hook, "changes": changes, "notes": notes, "rules": final,
            "kept": kept, "dropped": dropped, "redirects": redirects}


def apply(planned):
    hook = planned["hook"]
    if hook and hook["action"] != "unchanged":
        os.makedirs(os.path.dirname(hook["path"]), exist_ok=True)
        with open(hook["path"], "w", encoding="utf-8") as fh:
            fh.write(hook["source"])
        os.chmod(hook["path"], 0o755)
    for c in planned["changes"]:
        if c["action"] != "unchanged":
            os.makedirs(os.path.dirname(c["path"]), exist_ok=True)
            with open(c["path"], "w", encoding="utf-8") as fh:
                fh.write(c["after"])


def _names(harnesses):
    names = [HARNESS_NAMES[h] for h in harnesses]
    return names[0] if len(names) == 1 else (" and ".join(names) if len(names) == 2 else
                                             ", ".join(names[:-1]) + ", and " + names[-1])


def generate(rules, harnesses, scope="project", project=".", hook_path=None, write=False, uninstall=False,
             replace=False, follow_symlinks=False):
    planned = plan(rules, harnesses, scope, project, hook_path, uninstall, replace)
    changed = [c for c in planned["changes"] if c["action"] != "unchanged"] + (
        [planned["hook"]] if planned["hook"] and planned["hook"]["action"] != "unchanged" else [])
    links = ["%s leads to %s" % (code(_display(path), 300), code(_display(real), 300))
             for path, real in planned["redirects"]]
    if write and links and not follow_symlinks:
        raise ValueError("nothing was written: %s, outside the %s, through a symlink. Check the target, then run "
                         "again with --follow-symlinks to write there anyway." % (
                             "; ".join(links), "project" if scope == "project" else "harness folder"))
    where = _names([c["harness"] for c in planned["changes"]] or harnesses)
    rules_n = _n(len(planned["rules"]), "rule")
    extra = ""
    if planned["kept"]:
        extra += " (%d kept from the hook already there)" % len(planned["kept"])
    if planned["dropped"]:
        extra += ". No longer enforced: %s" % ", ".join(code(d, 64) for d in planned["dropped"])
    if not changed:
        headline = ("The rules-to-guards hook is already installed and up to date in %s." % where if not uninstall
                    else "There is no rules-to-guards hook to remove in %s." % _names(harnesses))
    elif write:
        apply(planned)
        headline = ("Installed a hook that enforces %s in %s%s." % (rules_n, where, extra) if not uninstall else
                    "Removed the rules-to-guards hook from %s. The hook file stays at %s; delete it if you "
                    "like." % (where, code(_display(planned["hook_path"]), 300)))
    else:
        headline = ("Dry run: this would %s. Nothing was written; run again with --write to apply." % (
            "install a hook that enforces %s in %s%s" % (rules_n, where, extra) if not uninstall else
            "remove the rules-to-guards hook from %s" % where))
    notes = list(planned["notes"])
    if links:
        notes.append("Symlinks lead outside the %s: %s. generate writes there only with --follow-symlinks." % (
            "project" if scope == "project" else "harness folder", "; ".join(links)))
    notes += [_HARNESS_NOTES[c["harness"]] for c in planned["changes"] if not uninstall]
    if not uninstall and planned["changes"]:
        notes += ["The hook runs by its first line, #!/usr/bin/env python3, so it needs python3 on your PATH. If "
                  "it hits an error, or its file is missing or cannot run, it allows the call, so a bug in it never "
                  "blocks your work.",
                  "The settings entry names the hook by its absolute path on this machine. Keep project settings "
                  "files with this entry out of git, or have each teammate run generate.",
                  "The agent could edit or delete the hook file; add a deny rule for its folder if that matters."]
    return {"tool": "rules-to-guards", "version": VERSION, "command": "generate", "headline": headline,
            "written": bool(write and changed), "uninstall": uninstall,
            "hook": None if not planned["hook"] else {"path": planned["hook"]["path"],
                                                        "action": planned["hook"]["action"]},
            "rules": [] if uninstall else [
                {"id": r["id"], "kind": r["kind"], "tool": r.get("tool") or G._DEFAULT_TOOL[r["kind"]],
                 "pattern": r["pattern"], "kept": r["id"] in planned["kept"]} for r in planned["rules"]],
            "kept": planned["kept"], "dropped": planned["dropped"],
            "symlinks": [{"path": path, "real": real} for path, real in planned["redirects"]],
            "settings": [{"harness": c["harness"], "path": c["path"], "action": c["action"],
                          "diff": _diff_text(c["before"], c["after"], _display(c["path"]))}
                         for c in planned["changes"]],
            "permission_rules": [] if uninstall else _permission_rules(planned["rules"], harnesses),
            "permission_note": "Optional and partial: permission rules match the start of a command, so they "
                               "miss wrapped forms such as sh -c '...'. The hook covers those.",
            "notes": notes}


def render_generate(res):
    out = ["**%s**" % res["headline"], ""]
    if res["hook"]:
        out += ["Hook script: %s (%s; Python 3.9+, standard library only, rules embedded):" % (
            code(res["hook"]["path"], 300), {"create": "new file", "update": "replaced",
                                            "unchanged": "unchanged"}[res["hook"]["action"]])]
        out += ["- %s (%s, %s): %s%s" % (r["id"], r["kind"], r["tool"], code(r["pattern"], 2000),
                                         ", kept from the hook already there" if r["kept"] else "")
                for r in res["rules"]]
        out.append("")
    for s in res["settings"]:
        label = {"create": "new file", "update": "changed", "unchanged": "unchanged"}[s["action"]]
        out.append("%s: %s (%s)" % (HARNESS_NAMES[s["harness"]], code(_display(s["path"]), 300), label))
        if s["diff"]:
            fence = "`" * max(3, max([len(run) for run in re.findall(r"`+", s["diff"])] or [0]) + 1)
            out += [fence + "diff", s["diff"].rstrip("\n"), fence]
        out.append("")
    if res["permission_rules"]:
        out.append("Permission rules you can add too. " + res["permission_note"])
        for h in HARNESS_NAMES:
            entries = [p for p in res["permission_rules"] if p["harness"] == h]
            if entries:
                out.append("- %s, in %s: %s" % (HARNESS_NAMES[h], _PERMISSION_WHERE[h],
                                                "; ".join(code(p["entry"], 400) for p in entries)))
        out.append("")
    for n in res["notes"]:
        out.append("Note: " + n)
    return "\n".join(out).rstrip() + "\n"


# ---------------------------------------------------------------------------
# test: replay recorded calls through the hook
# ---------------------------------------------------------------------------

def _patch_text(tool):
    for key in ("raw", "input", "command"):
        if isinstance(tool.input.get(key), str) and "*** " in tool.input[key]:
            return tool.input[key]
    return "*** Begin Patch\n%s*** End Patch" % "".join("*** Update File: %s\n" % p for p in tool.paths)


def hook_event(call):
    """(the pre-tool hook JSON the harness that recorded the call would send, the
    --harness value). OpenCode has no shell hooks; its calls use the Claude Code shape."""
    t = call.tool
    if call.harness == "codex":
        if t.kind == "shell":
            name, inp = "Bash", {"command": t.command}
        elif t.name == "apply_patch":
            name, inp = "apply_patch", {"command": _patch_text(t)}
        else:
            name, inp = t.name, dict(t.input)
        return ({"session_id": call.session, "transcript_path": None, "cwd": call.cwd,
                 "hook_event_name": "PreToolUse", "model": "", "permission_mode": "default", "tool_name": name,
                 "tool_use_id": t.id, "tool_input": inp}, "codex")
    if call.harness == "gemini-cli":
        return ({"session_id": call.session, "transcript_path": "", "cwd": call.cwd, "hook_event_name": "BeforeTool",
                 "timestamp": call.ts, "tool_name": t.name, "tool_input": dict(t.input)}, "gemini-cli")
    return ({"session_id": call.session, "transcript_path": "", "cwd": call.cwd, "permission_mode": "default",
             "hook_event_name": "PreToolUse", "tool_name": t.name, "tool_input": dict(t.input),
             "tool_use_id": t.id}, "claude-code")


def run_hook(hook, call, timeout=10):
    """("blocked" | "allowed" | "error", milliseconds, the rule id the hook named, exit code).
    Runs the settings command the harness would run, through /bin/sh, so the hook
    meets the same python3; it feeds the hook the recorded call as JSON, and the
    recorded command itself never runs."""
    import subprocess
    import time
    event, fmt = hook_event(call)
    started = time.time()
    try:
        done = subprocess.run(["/bin/sh", "-c", hook_command(hook, fmt)], input=json.dumps(event),
                              capture_output=True, text=True, timeout=timeout,
                              env=dict(os.environ, CLAUDE_PROJECT_DIR=call.cwd or os.getcwd()))
    except (OSError, subprocess.SubprocessError):
        return "error", (time.time() - started) * 1000, "", None
    ms = (time.time() - started) * 1000
    named = re.search(r"\(rule ([A-Za-z0-9][A-Za-z0-9_.-]{0,63})", done.stderr or "")
    rule_id = named.group(1) if named else ""
    if done.returncode == 2:
        return "blocked", ms, rule_id, 2
    if done.returncode != 0:
        return "error", ms, "", done.returncode
    try:
        out = json.loads(done.stdout) if done.stdout.strip().startswith("{") else {}
    except ValueError:
        out = {}
    specific = out.get("hookSpecificOutput") if isinstance(out.get("hookSpecificOutput"), dict) else {}
    denied = (specific.get("permissionDecision") == "deny" or out.get("permission") == "deny"
              or out.get("decision") in ("deny", "block"))
    return ("blocked" if denied else "allowed"), ms, rule_id if denied else "", 0


def _call_key(call):
    return (call.tool.name, call.tool.command, tuple(call.tool.paths))


def replay(rules, hook=None, since="30d", harness="all", project=None, sample=400, max_per_rule=200, workers=8):
    import shutil
    import tempfile
    from concurrent.futures import ThreadPoolExecutor
    _days, label, _cutoff = parse_since(since)
    calls, _skipped = load_calls(since, harness, project)
    stats = classify(rules, calls)
    compiled = [st["rule"] for st in stats]
    breaking = {id(c) for st in stats for c, _m in st["hits"]}
    tested = {}
    for st in stats:
        st["tested"] = sorted(st["hits"], key=lambda h: h[0].ts, reverse=True)[:max_per_rule]
        for call, _m in st["tested"]:
            tested[id(call)] = call
    others, seen = [], set()
    for call in reversed(calls):
        if len(others) >= sample:
            break
        key = _call_key(call)
        if id(call) in breaking or key in seen or not any(_applies(r, call.tool.kind) for r in compiled):
            continue
        seen.add(key)
        others.append(call)
    temp = None
    if hook is None:
        temp = tempfile.mkdtemp(prefix="rules-to-guards-")
        hook = os.path.join(temp, HOOK_NAME)
        with open(hook, "w", encoding="utf-8") as fh:
            fh.write(hook_source(rules))
        os.chmod(hook, 0o755)
    try:
        todo = list(tested.values()) + others
        with ThreadPoolExecutor(max_workers=workers) as pool:
            outcomes = dict(zip([id(c) for c in todo], pool.map(lambda c: run_hook(hook, c), todo)))
    finally:
        if temp:
            shutil.rmtree(temp, ignore_errors=True)
    return _replay_result(rules, stats, others, outcomes, calls, label, temp is not None, hook)


def _example(call, match=None):
    """A replayed call for the report; `match` is the path or text a rule matched."""
    if match is None:
        match = (call.tool.paths or [""])[0] if call.tool.kind in ("read", "edit", "write") else call.tool.name
    return {"harness": call.harness, "session": safe(call.session[:8], 8), "time": _when(call.ts),
            "tool": safe(call.tool.name, 60), "excerpt": _excerpt(call, match)}


_FRESH_HOOK = "a fresh copy made from rules.json (not installed)"


def _replay_result(rules, stats, others, outcomes, calls, label, fresh, hook):
    results, raw = [], {r["id"]: r for r in rules}
    for st in stats:
        verdicts = [(c, m, outcomes[id(c)][0]) for c, m in st["tested"]]
        missed = [(c, m) for c, m, v in verdicts if v != "blocked"]
        results.append({"id": st["rule"].id, "violations": len(st["hits"]), "tested": len(verdicts),
                        "blocked": len(verdicts) - len(missed), "missed": len(missed),
                        "missed_examples": [_example(c, m) for c, m in missed[:3]]})
    other_verdicts = [(c, outcomes[id(c)][0]) for c in others]
    wrongly = [c for c, v in other_verdicts if v == "blocked"]
    times = sorted(o[1] for o in outcomes.values())
    tested_calls = {id(c): outcomes[id(c)][0] for st in stats for c, _m in st["tested"]}
    errors = sum(1 for o in outcomes.values() if o[0] == "error")
    commands_only = all(r["kind"] == "forbid_command" for r in rules)
    headline = _replay_headline(results, rules, raw, tested_calls, len(others), len(wrongly), calls, label,
                                "command" if commands_only else "call")
    notes = []
    if any(r["missed"] for r in results) or wrongly:
        notes.append("If the hook was made from an older rules.json, run generate again; a stale hook explains "
                     "most misses and wrong blocks.")
    if errors:
        notes.append("%s hit a hook error, timed out, or exited with a code other than 0 or 2; the harness lets "
                     "such calls through." % _n(errors, "replayed call"))
    if any(o[3] == 126 for o in outcomes.values()):
        notes.append("Exit code 126 means the hook file cannot run: give it run permission (chmod +x), or, if the "
                     "folder does not allow running programs, test the installed hook with --hook.")
    return {"tool": "rules-to-guards", "version": VERSION, "command": "test", "headline": headline,
            "hook": _FRESH_HOOK if fresh else safe(_display(hook), 300),
            "window": label, "rules": results,
            "others": {"sampled": len(others), "allowed": sum(1 for _c, v in other_verdicts if v == "allowed"),
                       "blocked": len(wrongly), "errors": sum(1 for _c, v in other_verdicts if v == "error"),
                       "examples": [dict(_example(c), rule=safe(outcomes[id(c)][2], 64)) for c in wrongly[:5]]},
            "errors": errors,
            "timing_ms": {"median": round(times[len(times) // 2], 1) if times else 0.0,
                          "max": round(times[-1], 1) if times else 0.0},
            "notes": notes}


def _replay_headline(results, rules, raw, tested_calls, others, wrongly, calls, label, noun):
    if not calls:
        return "No recorded tool calls in %s, so there is nothing to replay." % label
    one = "the one other recent %s" % noun
    last = "your last %s" % _n(others, "other %s" % noun)
    total = len(tested_calls)
    if not total:
        if not others:
            return "No recorded call broke these rules in %s, and no recent call used the tools they check." % label
        if wrongly:
            return "No recorded call broke these rules in %s, yet the hook wrongly blocks %s." % (
                label, one if others == 1 else "%d of %s" % (wrongly, last))
        return "No recorded call broke these rules in %s. The hook allows %s." % (
            label, one if others == 1 else "all of " + last)
    broken = [r for r in results if r["violations"]]
    if len(rules) == 1:
        who = _label(raw[broken[0]["id"]])
    elif len(broken) == len(rules):
        who = "%d rules" % len(broken)
    else:
        who = "%d of these %d rules" % (len(broken), len(rules))
    blocked = sum(1 for v in tested_calls.values() if v == "blocked")
    if blocked == total:
        blocks = "The hook blocks it" if total == 1 else "The hook blocks all %d" % total
    else:
        blocks = "The hook blocks %d of %d (%d get through)" % (blocked, total, total - blocked)
    if not others:
        tail = "."
    elif wrongly:
        tail = ", and it wrongly blocks %s." % (one if others == 1 else "%d of %s" % (wrongly, last))
    else:
        tail = " and %s." % ("not " + one if others == 1 else "none of " + last)
    return "Your agent broke %s %s in %s. %s%s" % (
        who, _n(sum(r["violations"] for r in results), "time"), label, blocks, tail)


def render_replay(res):
    hook = res["hook"] if res["hook"] == _FRESH_HOOK else code(res["hook"], 300)
    out = ["**%s**" % res["headline"], "", "Hook: %s." % hook, ""]
    if res["rules"]:
        out += ["| Rule | Breaks | Replayed | Blocked | Missed |", "|---|---|---|---|---|"]
        out += ["| %s | %d | %d | %d | %d |" % (r["id"], r["violations"], r["tested"], r["blocked"], r["missed"])
                for r in res["rules"]]
        out.append("")
    o = res["others"]
    out.append("Other recent calls replayed: %d distinct, %d allowed, %d blocked, %d hook errors." % (
        o["sampled"], o["allowed"], o["blocked"], o["errors"]))
    misses = [(r["id"], e) for r in res["rules"] for e in r["missed_examples"]]
    if misses:
        out += ["", "Breaks the hook let through:"] + ["- %s, %s, %s: %s" % (
            rid, HARNESS_NAMES.get(e["harness"], e["harness"]), e["time"], code(e["excerpt"])) for rid, e in misses]
    if o["examples"]:
        out += ["", "Calls the hook blocked that rules.json does not count as breaks (false positives):"] + [
            "- %s, %s, blocked by %s: %s" % (HARNESS_NAMES.get(e["harness"], e["harness"]), e["time"],
                                             e["rule"] or "an unnamed rule", code(e["excerpt"])) for e in o["examples"]]
    out += ["", "Hook speed: median %s ms, slowest %s ms per call (Python start-up included)." % (
        res["timing_ms"]["median"], res["timing_ms"]["max"])]
    for n in res["notes"]:
        out += ["", "Note: " + n]
    return "\n".join(out).rstrip() + "\n"


# ---------------------------------------------------------------------------
# Command line
# ---------------------------------------------------------------------------

def _parser():
    import argparse
    parser = argparse.ArgumentParser(
        prog="rules.py", description=__doc__.strip().split("\n\n")[0],
        epilog="Exit codes: 0 done, 1 a problem --strict asked to fail on, 2 usage or input error.")
    sub = parser.add_subparsers(dest="command", metavar="COMMAND")

    def window(p):
        p.add_argument("--since", default="30d", help="how far back: 30d, 2w, 12h, or a date such as 2026-09-01 "
                                                      "(default: 30d)")
        p.add_argument("--harness", default="all", choices=("all",) + T.HARNESSES + ("cursor",),
                       help="which agent's sessions to read (default: all)")
        p.add_argument("--project", metavar="PATH", help="only sessions that ran in this folder or below it")

    def output(p):
        p.add_argument("--json", action="store_true", help="print machine-readable JSON")
        p.add_argument("--out", metavar="PATH", help="write the report to this file instead of printing it")

    p = sub.add_parser("extract", help="list candidate rules in the context files agents load",
                       description="List lines with never, don't, do not, always, must, only use, avoid, or "
                                   "prefer in AGENTS.md, CLAUDE.md, GEMINI.md, Cursor rules, Copilot "
                                   "instructions, and the files they import. Reads files only.")
    p.add_argument("--repo", default=".", help="the project folder (default: current folder)")
    p.add_argument("--user", action="store_true",
                   help="also read your personal files, such as ~/.claude/CLAUDE.md and ~/.codex/AGENTS.md")
    p.add_argument("--file", action="append", default=[], metavar="PATH", help="also read this file (repeatable)")
    output(p)

    p = sub.add_parser("count", help="count how often recent sessions broke each rule",
                       description="Count the tool calls in recent Claude Code, Codex, Gemini CLI, and OpenCode "
                                   "sessions that broke each rule in rules.json. Reads session files only.")
    p.add_argument("--rules", required=True, metavar="PATH", help="the rules file (see references/checkable-rules.md)")
    window(p)
    output(p)

    p = sub.add_parser("generate", help="write a hook that enforces the rules (dry run unless --write)",
                       description="Write rules_guard.py, a hook with your rules embedded, and add it to each "
                                   "harness's settings, merged with the hooks already there. Without --write it "
                                   "only shows the changes.")
    p.add_argument("--rules", metavar="PATH", help="the rules file (not needed with --uninstall)")
    p.add_argument("--harness", default="claude-code",
                   help="comma-separated: claude-code, codex, gemini-cli, cursor (default: claude-code)")
    p.add_argument("--scope", choices=("project", "user"), default="project",
                   help="project settings in --project, or your user settings (default: project)")
    p.add_argument("--project", default=".", help="the project folder (default: current folder)")
    p.add_argument("--hook-path", metavar="PATH", help="where to write the hook (default: .agents/hooks/%s in "
                                                      "the project, or in your home folder with --scope user)"
                   % HOOK_NAME)
    p.add_argument("--only", metavar="IDS", help="comma-separated rule ids to enforce (default: every rule)")
    p.add_argument("--write", action="store_true", help="apply the changes")
    p.add_argument("--replace", action="store_true",
                   help="write the hook with only these rules (default: keep the rules the hook already holds, "
                        "replacing any with the same id)")
    p.add_argument("--follow-symlinks", action="store_true",
                   help="write through symlinks that lead outside the project or the harness folder")
    p.add_argument("--uninstall", action="store_true", help="remove the rules-to-guards hook entries instead")
    output(p)

    p = sub.add_parser("test", help="replay recorded calls through the hook: breaks must block, others must pass",
                       description="Feed each recorded break (must block) and a sample of other recent calls (must "
                                   "allow) to the hook as JSON, and report blocks, misses, and false positives. Only "
                                   "the hook runs; the recorded commands never do.")
    p.add_argument("--rules", required=True, metavar="PATH", help="the rules file")
    p.add_argument("--hook", metavar="PATH", help="the hook to test (default: a fresh copy made from --rules in a "
                                                 "temporary folder)")
    p.add_argument("--only", metavar="IDS", help="comma-separated rule ids to test (default: every rule)")
    p.add_argument("--sample", type=int, default=400, help="how many other recent calls to replay (default: 400)")
    p.add_argument("--strict", action="store_true", help="exit 1 on any miss, false positive, or hook error")
    window(p)
    output(p)
    return parser


def main(argv=None):
    parser = _parser()
    args = parser.parse_args(argv)
    if args.command == "extract":
        if not os.path.isdir(args.repo):
            print("error: project folder not found: %s" % safe(args.repo, 300), file=sys.stderr)
            return 2
        _emit(args, extract(args.repo, user=args.user, files=args.file), render_extract)
        return 0
    if args.command == "count":
        try:
            rules, advice = load_rules(args.rules)
            parse_since(args.since)
        except (RulesError, ValueError) as exc:
            return _input_error(exc)
        _emit(args, count(rules, advice, since=args.since, harness=args.harness, project=args.project),
              render_count)
        return 0
    if args.command == "generate":
        return _generate_command(args)
    if args.command == "test":
        try:
            rules = _pick(load_rules(args.rules)[0], args.only)
            parse_since(args.since)
            if args.hook and not os.path.isfile(args.hook):
                raise ValueError("hook file not found: %s" % safe(args.hook, 300))
            if not rules:
                raise ValueError("rules.json has no checkable rules to test")
        except (RulesError, ValueError) as exc:
            return _input_error(exc)
        result = replay(rules, hook=os.path.abspath(args.hook) if args.hook else None, since=args.since,
                        harness=args.harness, project=args.project, sample=max(args.sample, 0))
        _emit(args, result, render_replay)
        failed = result["errors"] or result["others"]["blocked"] or any(r["missed"] for r in result["rules"])
        return 1 if args.strict and failed else 0
    parser.print_help()
    return 2


def _pick(rules, only):
    if not only:
        return rules
    wanted = [i.strip() for i in only.split(",") if i.strip()]
    unknown = [i for i in wanted if i not in {r["id"] for r in rules}]
    if unknown:
        raise ValueError("--only names rules that rules.json does not have: %s" % safe(", ".join(unknown)))
    return [r for r in rules if r["id"] in wanted]


def _harness_list(value):
    harnesses = [h.strip() for h in str(value).split(",") if h.strip()]
    bad = [h for h in harnesses if h not in G.HARNESSES]
    if bad or not harnesses:
        raise ValueError("--harness takes claude-code, codex, gemini-cli, or cursor (comma-separated); got %s. "
                         "OpenCode has no shell hooks, only plugins." % safe(", ".join(bad) or "nothing"))
    return harnesses


def _generate_command(args):
    try:
        harnesses = _harness_list(args.harness)
        rules = []
        if not args.uninstall:
            if not args.rules:
                raise ValueError("generate needs --rules, unless you pass --uninstall")
            rules = _pick(load_rules(args.rules)[0], args.only)
            if not rules:
                raise ValueError("rules.json has no checkable rules to enforce")
        result = generate(rules, harnesses, scope=args.scope, project=args.project, hook_path=args.hook_path,
                          write=args.write, uninstall=args.uninstall, replace=args.replace,
                          follow_symlinks=args.follow_symlinks)
    except (RulesError, ValueError) as exc:
        return _input_error(exc)
    _emit(args, result, render_generate)
    return 0


def _input_error(exc):
    problems = exc.problems if isinstance(exc, RulesError) else [str(exc)]
    sys.stderr.write("error: " + ("\n  - ".join([""] + problems) if len(problems) > 1 else problems[0]) + "\n")
    return 2


if __name__ == "__main__":
    sys.exit(main())
