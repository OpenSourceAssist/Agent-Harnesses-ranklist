#!/usr/bin/env python3
"""rules-to-guards hook: blocks a tool call that breaks one of the rules embedded below.

Made by rules-to-guards (https://github.com/RyanAlberts/best-of-Agent-Harnesses).
It reads one pre-tool hook event as JSON on stdin (Claude Code and Codex
PreToolUse, Gemini CLI BeforeTool, Cursor preToolUse) and blocks the call when
it breaks a rule. Python 3.9+, standard library only. Any error inside this
script lets the call through, so a bug here never stops your work.
"""
from __future__ import annotations

import json
import os
import re
import shlex
import sys

RULES = []  # rules-to-guards: `rules.py generate` writes your rules here

MAX_CHARS = 200000   # longer commands are checked on their first 200,000 characters
MAX_DEPTH = 4        # nesting limit for $(...), sh -c '...', and eval
MAX_COMMANDS = 5000  # simple commands checked per tool call

# ---------------------------------------------------------------------------
# Shell commands: split into simple commands, then unwrap each one
# ---------------------------------------------------------------------------

# The delimiter word runs up to an unquoted blank or shell metacharacter.
_HEREDOC_RE = re.compile(r"<<(-?)[ \t]*((?:'[^'\n]*'|\"[^\"\n]*\"|\\[^\n]|[^ \t\n|&;()<>'\"\\])+)")
_UNQUOTE_RE = re.compile(r"'([^']*)'|\"([^\"]*)\"|\\(.)")


def split_commands(script):
    """The simple commands in a shell script, as written: split on unquoted ;, &,
    |, newlines, and parentheses, with $(...) and backtick bodies added as their
    own commands, also inside a heredoc whose delimiter is not quoted, and the
    whole body of a heredoc fed to a shell such as bash. Other heredoc text and
    comments are skipped."""
    out = []
    _scan(script if isinstance(script, str) else "", out, 0, None)
    return out


def _scan(s, out, depth, blank, marks=False):
    """With marks, `out` also gets ("(",) and (")",) where a subshell opens and
    closes, ("|",) for each pipe, and ("$", body) in place of the commands of each
    $(...) and backtick body, so the caller can follow cd in each scope."""
    s = s[:MAX_CHARS]
    n, i, buf, word_start, heredocs = len(s), 0, [], True, []

    def keep(text, shown=None):
        buf.append(text)
        if blank is not None:
            blank.append(text if shown is None else shown)

    def flush():
        text = "".join(buf).strip()
        for doc in heredocs:  # the command a heredoc belongs to is now known
            if doc[3] is None:
                doc[3] = _feeds_shell(text)
        if text:
            out.append(text)
        del buf[:]

    def nested(text):
        if marks:
            out.append(("$", text))
        elif depth < MAX_DEPTH:
            _scan(text, out, depth + 1, None)

    while i < n:
        c = s[i]
        if c == "\\":
            if s.startswith("\\\n", i):  # line continuation
                i += 2
                continue
            keep(s[i:i + 2])
            i, word_start = i + 2, False
        elif c == "'":
            j = s.find("'", i + 1)
            if j < 0:  # a quote that never closes is a literal character
                keep(c)
                i, word_start = i + 1, False
                continue
            keep(s[i:j + 1], "''")
            i, word_start = j + 1, False
        elif c == '"':
            inner, j = _expansions(s, i + 1, '"')
            if j >= n:  # a quote that never closes is a literal character
                keep(c)
                i, word_start = i + 1, False
                continue
            for text in inner:
                nested(text)
            keep(s[i:j + 1], '""')
            i, word_start = j + 1, False
        elif s.startswith("$(", i):
            inner, j = _balanced(s, i + 2)
            nested(inner)
            keep(s[i:j])
            i, word_start = j, False
        elif c == "`" and s.find("`", i + 1) > 0:
            k = s.find("`", i + 1)
            nested(s[i + 1:k])
            keep(s[i:k + 1])
            i, word_start = k + 1, False
        elif c == "#" and word_start:
            k = s.find("\n", i)
            i = n if k < 0 else k
        elif s.startswith("<<", i) and not s.startswith("<<<", i) and _HEREDOC_RE.match(s, i):
            m = _HEREDOC_RE.match(s, i)
            heredocs.append(_heredoc(m))
            keep(m.group(0))
            i, word_start = m.end(), False
        elif c == "\n":
            flush()
            if blank is not None:
                blank.append("\n")
            i, word_start = i + 1, True
            if heredocs:
                i, heredocs = _skip_heredocs(s, i, heredocs, nested), []
        elif (c == "&" and (s.startswith("&>", i) or (buf and buf[-1].endswith(">")))) or \
                (c == "|" and buf and buf[-1].endswith(">")):
            keep(c)  # part of a redirection: &>, >&, >|
            i += 1
        elif c in ";&|()":
            flush()
            if blank is not None:
                blank.append(c)
            if marks and (c in "()" or (c == "|" and "|" not in (s[i - 1:i], s[i + 1:i + 2]))):
                out.append((c,))  # a subshell opens or closes, or a pipe (not ||) joins two
            i, word_start = i + 1, True
        else:
            keep(c)
            i, word_start = i + 1, c in " \t"
    flush()


def _balanced(s, start):
    """(text inside $( ... ), index after the closing parenthesis). Heredoc bodies
    are skipped and a quote that never closes is a literal character, so an
    apostrophe in a commit message cannot swallow the rest of the command."""
    depth, j, n, heredocs = 1, start, len(s), []
    while j < n:
        c = s[j]
        if c == "\\":
            j += 2
            continue
        if c in "'\"":
            k = _quote_end(s, j)
            j = j + 1 if k < 0 else k + 1
            continue
        if s.startswith("<<", j) and not s.startswith("<<<", j) and _HEREDOC_RE.match(s, j):
            m = _HEREDOC_RE.match(s, j)
            heredocs.append(_heredoc(m))
            j = m.end()
            continue
        if c == "\n" and heredocs:
            j, heredocs = _skip_heredocs(s, j + 1, heredocs), []
            continue
        if c == "(":
            depth += 1
        elif c == ")":
            depth -= 1
            if depth == 0:
                return s[start:j], j + 1
        j += 1
    return s[start:], n


def _quote_end(s, j):
    """Index of the quote that closes the one at s[j], or -1 when it never closes."""
    if s[j] == "'":
        return s.find("'", j + 1)
    k = j + 1
    while k < len(s):
        if s[k] == "\\":
            k += 2
        elif s[k] == '"':
            return k
        else:
            k += 1
    return -1


def _expansions(s, j=0, stop=None):
    """(the $(...) and backtick bodies in text that starts at s[j], index of the
    `stop` character that ends it or len(s)). The text is double-quoted, or a
    heredoc body whose delimiter is not quoted: the shell runs these bodies."""
    inner, n = [], len(s)
    while j < n and s[j] != stop:
        if s[j] == "\\":
            j += 2
        elif s.startswith("$(", j):
            text, j = _balanced(s, j + 2)
            inner.append(text)
        elif s[j] == "`" and s.find("`", j + 1) > 0:
            k = s.find("`", j + 1)
            inner.append(s[j + 1:k])
            j = k + 1
        else:
            j += 1
    return inner, j


def _heredoc(m):
    """[delimiter, expands, strips tabs, fed to a shell] for a heredoc operator.
    The delimiter is the word with its quotes and backslashes removed, as bash
    does. The body expands $(...) and backticks only when no part of the word is
    quoted: <<EOF, not <<'EOF', <<E"OF", or <<\\EOF. The last item stays None
    until the command the heredoc belongs to ends."""
    raw = m.group(2)
    word = _UNQUOTE_RE.sub(lambda q: "".join(g or "" for g in q.groups()), raw)
    return [word, word == raw, m.group(1) == "-", None]


def _skip_heredocs(s, i, heredocs, run=None):
    """Index after the heredoc bodies that start at s[i]. A body ends at a line
    that holds its delimiter alone; <<- also strips leading tabs from that line.
    `run` gets the text the shell runs: the whole body of a heredoc fed to a
    shell, and each $(...) and backtick body inside a heredoc that expands."""
    n = len(s)
    for word, expands, tabs, shell in heredocs:
        start = end = i
        while i < n:
            k = s.find("\n", i)
            line = s[i:] if k < 0 else s[i:k]
            end, i = i, (n if k < 0 else k + 1)
            if (line.lstrip("\t") if tabs else line) == word:
                break
        else:
            end = n
        if run:
            body = s[start:end]
            if shell:
                run(body)
            if expands:
                for text in _expansions(body)[0]:
                    run(text)
    return i


_ASSIGN_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*(?:\[[^\]]*\])?\+?=")
_DURATION_RE = re.compile(r"^\d+(?:\.\d+)?[smhd]?$")
_KEYWORDS = {"if", "then", "else", "elif", "do", "while", "until", "!", "{", "}", "(", ")", "fi", "done", "esac"}
# Commands that run another command; each maps to its options that take a value.
_WRAPPERS = {
    "sudo": {"-u", "-g", "-h", "-p", "-C", "-D", "-r", "-t", "-U", "-T", "--user", "--group", "--host",
             "--prompt", "--chdir"},
    "doas": {"-u", "-C"},
    "env": {"-u", "--unset", "-C", "--chdir", "-S", "--split-string"},
    "command": set(), "builtin": set(), "exec": {"-a"}, "noglob": set(), "nohup": set(), "time": set(),
    "nice": {"-n", "--adjustment"}, "ionice": {"-c", "-n", "-p"}, "stdbuf": {"-i", "-o", "-e"},
    "timeout": {"-s", "--signal", "-k", "--kill-after"}, "caffeinate": set(), "unbuffer": set(),
    "xargs": {"-n", "-I", "-L", "-P", "-s", "-d", "-E", "-a", "--max-args", "--replace", "--max-lines",
              "--max-procs", "--delimiter", "--arg-file", "--eof"},
}
_SHELLS = {"sh", "bash", "zsh", "dash", "ksh", "fish"}
_GREP_LIKE = {"grep", "egrep", "fgrep", "rg", "ag", "ack"}
_GREP_VALUE_OPTS = {"-A", "-B", "-C", "-m", "-f", "-g", "-t", "-T", "-j", "-M", "--max-count", "--context",
                    "--after-context", "--before-context", "--glob", "--type", "--type-not", "--max-depth",
                    "--file", "--include", "--exclude", "--exclude-dir"}
# Free text, not commands: message and format values of git and gh.
_MESSAGE_OPTS = {"git": {"-m", "--message", "-F", "--file", "--grep", "--format", "--pretty"},
                 "gh": {"-t", "--title", "-b", "--body"}}
_REDIRECT_OPS = {">", ">>", "<", "&>", "&>>", ">|", "1>", "1>>", "2>", "2>>"}
_REDIRECT_RE = re.compile(r"^(?:\d*|&)(>>?|<|>\|)(.+)$")


def _tokens(segment):
    lex = shlex.shlex(segment, posix=True)
    lex.whitespace_split = True
    lex.commenters = ""
    try:
        return list(lex)
    except ValueError:  # unbalanced quotes
        return segment.split()


def _base(word):
    return word.rsplit("/", 1)[-1] if "/" in word else word


def _pull_redirections(tokens):
    """(tokens without redirections, input targets, output targets)."""
    kept, inputs, outputs, i = [], [], [], 0
    while i < len(tokens):
        t = tokens[i]
        if t in _REDIRECT_OPS:
            if i + 1 < len(tokens):
                (inputs if t == "<" else outputs).append(tokens[i + 1])
            i += 2
            continue
        m = _REDIRECT_RE.match(t)
        if m and not t[:1].isalpha():
            if not m.group(2).startswith("&"):
                (inputs if m.group(1) == "<" else outputs).append(m.group(2))
            i += 1
            continue
        kept.append(t)
        i += 1
    return kept, inputs, outputs


def _unwrap(tokens):
    """Drop leading variable assignments, shell keywords, and wrapper commands
    such as sudo, env, timeout, and xargs, with their options."""
    i, n = 0, len(tokens)
    while i < n:
        t = tokens[i]
        if t in _KEYWORDS or _ASSIGN_RE.match(t):
            i += 1
            continue
        spec = _WRAPPERS.get(_base(t))
        if spec is None:
            break
        base, i = _base(t), i + 1
        while i < n and tokens[i].startswith("-") and tokens[i] != "-":
            opt, i = tokens[i], i + 1
            if opt == "--":
                break
            if base == "command" and not opt.startswith("--") and ("v" in opt or "V" in opt):
                return []  # command -v npm looks npm up; it runs nothing
            if opt in spec:
                i += 1
        if base == "env":
            while i < n and _ASSIGN_RE.match(tokens[i]):
                i += 1
        elif base == "timeout" and i < n and _DURATION_RE.match(tokens[i]):
            i += 1
    return tokens[i:]


def _feeds_shell(text):
    """True when the simple command `text` is a shell, such as bash or sudo sh:
    a heredoc on it is a script the shell runs, quoted or not."""
    body = _unwrap(_pull_redirections(_tokens(_HEREDOC_RE.sub(" ", text)))[0])
    return bool(body) and _base(body[0]) in _SHELLS


def _shell_script(args):
    """The script of `sh -c '<script>'` and similar, or None."""
    has_c, i = False, 0
    while i < len(args):
        a = args[i]
        if a in ("-o", "+o", "-O", "+O"):
            i += 2
            continue
        if a.startswith("-") and not a.startswith("--") and len(a) > 1:
            has_c = has_c or "c" in a[1:]
        elif not a.startswith("--"):
            return a if has_c else None
        i += 1
    return None


def _find_exec(args):
    """Commands run by find -exec, -execdir, -ok, or -okdir."""
    found, i = [], 0
    while i < len(args):
        if args[i] in ("-exec", "-execdir", "-ok", "-okdir"):
            j = i + 1
            while j < len(args) and args[j] not in (";", "+"):
                j += 1
            if j > i + 1:
                found.append(" ".join(args[i + 1:j]))
            i = j
        i += 1
    return found


def _drop_free_text(body):
    """The command without arguments that are text rather than commands, paths,
    or options: the words of echo and printf, the search pattern of grep-like
    tools, the script of sed and awk, and git and gh messages."""
    if not body:
        return body
    word, rest = body[0], body[1:]
    if word in ("echo", "printf"):
        return [word]
    if word in _GREP_LIKE:
        return [word] + _drop_pattern(rest, {"-e", "--regexp"}, _GREP_VALUE_OPTS)
    if word in ("sed", "gsed"):
        return [word] + _drop_pattern(rest, {"-e", "--expression"}, {"-f", "--file", "-l"})
    if word in ("awk", "gawk", "mawk", "nawk"):
        return [word] + _drop_pattern(rest, set(), {"-f", "-v", "-F"})
    opts = _MESSAGE_OPTS.get(word)
    if opts:
        kept, i = [word], 0
        while i < len(rest):
            a = rest[i]
            if a in opts or (word == "git" and re.match(r"^-[A-Za-z]*[mF]$", a)):  # -m, -am, -sm, -F
                i += 2
                continue
            if (a.startswith("--") and a.split("=", 1)[0] in opts) or (word == "git" and re.match(r"^-[mF].", a)):
                i += 1  # --message=..., or -m"..." which arrives as one word
                continue
            kept.append(a)
            i += 1
        return kept
    return body


def _drop_pattern(args, pattern_opts, value_opts):
    """Drop the pattern or script argument: every value of pattern_opts when one
    is given, otherwise the first operand."""
    dropped = any(a in pattern_opts or a.split("=", 1)[0] in pattern_opts for a in args)
    kept, i = [], 0
    while i < len(args):
        a = args[i]
        if a in pattern_opts:
            i += 2
            continue
        if a.startswith("--") and a.split("=", 1)[0] in pattern_opts:
            i += 1
            continue
        if a in value_opts:
            kept.extend(args[i:i + 2])
            i += 2
            continue
        if not dropped and not a.startswith("-"):
            dropped = True
            i += 1
            continue
        kept.append(a)
        i += 1
    return kept


# Commands that only look at a file's name or metadata: their arguments are not reads or writes.
_METADATA = {"ls", "stat", "test", "[", "[[", "du", "realpath", "readlink", "dirname", "basename", "which",
             "type", "file"}


class _Facts:
    """What the rules see in one shell command."""

    def __init__(self):
        self.texts = []    # command texts a command rule is matched against
        # (path, folder) pairs; the folder is where cd went, from the working folder
        self.paths = []    # arguments and input redirection targets a path rule is matched against
        self.outputs = []  # output redirection targets (> and >>): writes only
        self.commands = 0  # simple commands seen, up to MAX_COMMANDS
        self._seen = set()

    def text(self, t):
        if t and ("t", t) not in self._seen:
            self._seen.add(("t", t))
            self.texts.append(t)

    def path(self, p, output=False, here=""):
        key = ("o" if output else "p", p, here)
        if p and "://" not in p and key not in self._seen:
            self._seen.add(key)
            (self.outputs if output else self.paths).append((p, here))


_CD_OPTION_RE = re.compile(r"^-[LPe@]+$")


def _change_folder(here, base, args, pushed):
    """The folder after cd, pushd, or popd, relative to the working folder or
    absolute. When the hook cannot tell where it goes (cd -, a variable, ~user),
    the folder stays where the hook last knew it."""
    if base == "popd":
        return pushed.pop() if pushed and not args else here
    while args and _CD_OPTION_RE.match(args[0]):
        args = args[1:]
    args = args[1:] if args[:1] == ["--"] else args
    if base == "pushd":
        if not args:
            return here  # swaps the top two folders
        pushed.append(here)
    if len(args) > 1:
        return here
    to = args[0] if args else "~"
    if to == "~" or to.startswith("~/"):
        to = os.path.expanduser("~") + to[1:]
    if to == "-" or to.startswith(("~", "+")) or re.search(r"[$`*?\[]", to):
        return here
    return os.path.normpath(os.path.join(here, to))


def _shell_facts(command, facts=None, depth=0, here="", sub=0):
    """`here` is the folder the command starts in, as _change_folder gives it.
    `depth` counts sh -c, eval, and find -exec levels; `sub` counts $(...) levels
    inside one of them. Each stops at MAX_DEPTH on its own."""
    facts = facts or _Facts()
    command = command if isinstance(command, str) else ""
    blank, segments = [], []
    _scan(command, segments, 0, blank, marks=True)
    if depth == 0 and sub == 0:
        facts.text("".join(blank).strip())
    outer, pushed = [], []  # the folder to go back to when each ( closes; pushd's stack
    before, piped = here, False  # the folder before the last command; True right after a pipe
    for seg in segments:
        if facts.commands >= MAX_COMMANDS:
            break
        if isinstance(seg, tuple):
            if seg[0] == "(":
                outer.append(here)
            elif seg[0] == ")":
                here = outer.pop() if outer else here
            elif seg[0] == "|":  # bash runs each part of a pipeline in a subshell
                here, piped = before, True
            elif sub < MAX_DEPTH:  # a $(...) body starts here, and its cd stays inside it
                _shell_facts(seg[1], facts, depth, here, sub + 1)
            continue
        facts.commands += 1
        before, in_pipe, piped = here, piped, False
        tokens, inputs, outputs = _pull_redirections(_tokens(seg))
        for t in inputs:
            facts.path(t, here=here)
        for t in outputs:
            facts.path(t, output=True, here=here)
        body = _unwrap(tokens)
        if not body:
            continue
        prefix = tokens[:len(tokens) - len(body)]
        base = _base(body[0])
        core = _drop_free_text([base] + body[1:])
        facts.text(" ".join(prefix + [body[0]] + core[1:]))
        facts.text(" ".join(core))
        script = None
        if base in _SHELLS:
            script = _shell_script(body[1:])
        elif base == "eval":
            script = " ".join(body[1:])
        if script is None and base not in _METADATA:
            for arg in core[1:]:
                if arg.startswith("-"):
                    arg = arg.split("=", 1)[1] if arg.startswith("--") and "=" in arg else ""
                facts.path(arg, here=here)
        if base in ("cd", "pushd", "popd") and not in_pipe:
            here = _change_folder(here, base, body[1:], pushed)
        if depth < MAX_DEPTH:
            if script:
                _shell_facts(script, facts, depth + 1, here)
            if base == "find":
                for run in _find_exec(body[1:]):
                    _shell_facts(run, facts, depth + 1, here)
    return facts


def command_candidates(command):
    """The texts a command rule is matched against: the whole command with quoted
    text, heredoc bodies, and comments emptied, then each simple command as
    written and unwrapped, without its free text."""
    return _shell_facts(command).texts


def shell_path_args(command):
    """Arguments and redirection targets of a shell command that a path rule checks."""
    return [p for p, _here in _shell_facts(command).paths]


# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------

def _glob_re(pat):
    """A compiled full-match regex for a glob: * and ? stay inside one folder,
    ** crosses folders, and a trailing /** also matches the folder itself."""
    tail = ""
    if pat.endswith("/**"):
        pat, tail = pat[:-3], "(?:/.*)?"
    out, i, n = [], 0, len(pat)
    while i < n:
        c = pat[i]
        if pat.startswith("**/", i):
            out.append("(?:.*/)?")
            i += 3
            continue
        if pat.startswith("**", i):
            out.append(".*")
            i += 2
            continue
        if c == "*":
            out.append("[^/]*")
        elif c == "?":
            out.append("[^/]")
        elif c == "[" and pat.find("]", i + 2) > 0:
            j = pat.find("]", i + 2)
            body = pat[i + 1:j]
            body = "^" + body[1:] if body.startswith("!") else body
            out.append("[" + body.replace("\\", "\\\\") + "]")
            i = j + 1
            continue
        else:
            out.append(re.escape(c))
        i += 1
    return re.compile("".join(out) + tail + r"\Z")


def _plain_glob(pattern):
    """The glob with a trailing / written as /**, and a leading ./ removed."""
    pat = pattern.strip()
    if pat.endswith("/") and len(pat) > 1:
        pat = pat.rstrip("/") + "/**"
    return pat[2:] if pat.startswith("./") else pat


def check_glob(pattern):
    """Raise re.error when the glob cannot be used, such as a range like [z-a]."""
    _glob_re(_plain_glob(pattern))


def glob_matches(pattern, path, cwd="", home=None, root=None):
    """True when `path` matches the glob. A glob without a slash matches any
    folder or file name at any depth (.env, *.lock, node_modules). A glob with a
    slash matches inside the project only, at any depth (dist/**); with a leading
    ./ it matches at the project root only. A glob that starts with / or ~/ is an
    absolute path. Relative paths start from `cwd`; the project is `root`, or `cwd`."""
    if not pattern or not path or not isinstance(path, str):
        return False
    home = home or os.path.expanduser("~")
    p = home + path[1:] if path == "~" or path.startswith("~/") else path
    if not os.path.isabs(p) and cwd:
        p = os.path.join(cwd, p)
    p = os.path.normpath(p)
    top = root or cwd
    rel = None
    if not os.path.isabs(p):
        rel = p
    elif top:
        base = os.path.normpath(top).rstrip("/")
        if p.startswith(base + "/"):
            rel = p[len(base) + 1:]
    pat = pattern.strip()
    if pat == "~" or pat.startswith("~/"):
        pat = home + pat[1:]
    anchored = pat.startswith("./")
    pat = _plain_glob(pat)
    if pat.startswith("/"):
        return os.path.isabs(p) and bool(_glob_re(pat).match(p))
    if anchored:
        return rel is not None and bool(_glob_re(pat).match(rel))
    rx = _glob_re(pat)
    if "/" not in pat:
        return any(rx.match(part) for part in (rel if rel is not None else p.lstrip("/")).split("/"))
    if rel is None:
        return False  # outside the project
    parts = rel.split("/")
    return any(rx.match("/".join(parts[i:])) for i in range(len(parts)))


# ---------------------------------------------------------------------------
# Hook input: one call shape from every harness's pre-tool event
# ---------------------------------------------------------------------------

# Tool name -> kind. Names from Claude Code, Codex (hooks and transcripts),
# Gemini CLI, Cursor, and OpenCode; the kinds match transcripts.py.
TOOL_KINDS = {
    "Bash": "shell", "Read": "read", "NotebookRead": "read", "Edit": "edit", "MultiEdit": "edit",
    "NotebookEdit": "edit", "Write": "write", "Monitor": "shell",
    "apply_patch": "edit", "exec_command": "shell", "shell": "shell", "shell_command": "shell",
    "local_shell_call": "shell", "local_shell": "shell", "container.exec": "shell", "exec": "shell",
    "view_image": "read",
    "run_shell_command": "shell", "read_file": "read", "read_many_files": "read", "write_file": "write",
    "replace": "edit", "edit": "edit",
    "Shell": "shell", "Delete": "edit",
    "bash": "shell", "read": "read", "write": "write", "multiedit": "edit", "patch": "edit",
}
_PATH_KEYS = ("file_path", "notebook_path", "absolute_path", "path", "filePath", "target_file")
_PATCH_KEYS = ("command", "patchText", "patch", "input", "raw")
_PATCH_RE = re.compile(r"^\*\*\* (?:Update|Add|Delete) File: (.+)$|^\*\*\* Move to: (.+)$", re.M)


def tool_kind(name):
    if name in TOOL_KINDS:
        return TOOL_KINDS[name]
    return "mcp" if name.startswith(("mcp__", "mcp_", "MCP:")) else "other"


def _command_text(value):
    """A shell command given as a string, or as an argv list such as [bash, -lc, script]."""
    if isinstance(value, str):
        return value
    if isinstance(value, list) and value and all(isinstance(a, str) for a in value):
        if len(value) >= 3 and value[-2] in ("-c", "-lc", "-ic", "-lic"):
            return value[-1]
        return shlex.join(value)
    return ""


def normalize_hook_input(data):
    """{"kind", "name", "command", "paths", "cwd", "root"} for one pre-tool hook event.
    The root is the project folder: CLAUDE_PROJECT_DIR, GEMINI_PROJECT_DIR, or
    Cursor's first workspace root when set, otherwise the working folder."""
    data = data if isinstance(data, dict) else {}
    name = data.get("tool_name") if isinstance(data.get("tool_name"), str) else ""
    inp = data.get("tool_input") if isinstance(data.get("tool_input"), dict) else {}
    kind, command, paths = tool_kind(name), "", []
    if kind == "shell":
        command = _command_text(inp.get("command")) or _command_text(inp.get("cmd"))
    elif name in ("apply_patch", "patch"):
        text = next((inp[k] for k in _PATCH_KEYS if isinstance(inp.get(k), str)), "")
        paths = [a or b for a, b in _PATCH_RE.findall(text)]
    elif kind in ("read", "edit", "write"):
        many = inp.get("paths") if isinstance(inp.get("paths"), list) else []
        for p in [inp.get(k) for k in _PATH_KEYS] + many:
            if isinstance(p, str) and p and p not in paths:
                paths.append(p)
    roots = data.get("workspace_roots")
    workspace = roots[0] if isinstance(roots, list) and roots and isinstance(roots[0], str) else ""
    cwd = data.get("cwd") if isinstance(data.get("cwd"), str) else ""
    cwd = cwd or workspace
    root = os.environ.get("CLAUDE_PROJECT_DIR") or os.environ.get("GEMINI_PROJECT_DIR") or workspace or cwd
    return {"kind": kind, "name": name, "command": command, "paths": paths, "cwd": cwd, "root": root}


# ---------------------------------------------------------------------------
# Rules
# ---------------------------------------------------------------------------

CHECKABLE = ("forbid_command", "protect_path", "forbid_tool")
TOOLS = ("shell", "edit", "write", "read", "any")
_DEFAULT_TOOL = {"forbid_command": "shell", "protect_path": "edit|write", "forbid_tool": "any"}


class Rule:
    def __init__(self, raw):
        self.id = str(raw.get("id") or "")
        self.kind = raw.get("kind")
        self.pattern = raw.get("pattern")
        self.message = str(raw.get("message") or raw.get("text") or "")
        self.source = str(raw.get("source") or "")
        self.kinds = tool_kinds(self.kind, raw.get("tool"))
        self.regex = re.compile(self.pattern) if self.kind in ("forbid_command", "forbid_tool") else None
        if self.kind == "protect_path":
            check_glob(self.pattern)


def tool_kinds(kind, tool):
    """The call kinds a rule checks; None means every tool (forbid_tool with any)."""
    if kind not in CHECKABLE:
        raise ValueError("kind must be one of %s" % ", ".join(CHECKABLE))
    parts = [p.strip() for p in str(tool or _DEFAULT_TOOL[kind]).split("|") if p.strip()]
    if not parts or any(p not in TOOLS for p in parts):
        raise ValueError("tool must be shell, edit, write, read, or any, or several joined by |")
    if kind == "forbid_command":
        if "shell" not in parts and "any" not in parts:
            raise ValueError("a forbid_command rule checks shell commands, so its tool must be shell or any")
        return {"shell"}
    if "any" in parts:
        return None if kind == "forbid_tool" else {"shell", "read", "edit", "write"}
    return set(parts)


def compile_rules(rules, errors=None):
    """Rule objects for the checkable rules. Advice-only entries are skipped. A rule
    that cannot compile is skipped on its own, and (id, reason) goes to `errors`,
    so one bad pattern never disables the others."""
    compiled = []
    for r in rules:
        if not (isinstance(r, dict) and r.get("kind") in CHECKABLE and r.get("pattern")):
            continue
        try:
            compiled.append(Rule(r))
        except Exception as exc:
            if errors is not None:
                errors.append((str(r.get("id") or "?"), type(exc).__name__))
    return compiled


def matches(rules, call):
    """Yield {"rule": id, "match": what matched} for every rule the call breaks, in
    rule order. `call` is {"kind", "name", "command", "paths", "cwd", "root"}, as made
    by normalize_hook_input."""
    kind, cwd, root, facts = call.get("kind"), call.get("cwd") or "", call.get("root") or "", None
    for r in rules:
        found = None
        if r.kind == "forbid_tool":
            name = call.get("name") or ""
            if (r.kinds is None or kind in r.kinds) and r.regex.search(name):
                found = name
        elif kind == "shell" and "shell" in r.kinds:
            facts = facts or _shell_facts(call.get("command") or "")
            if r.kind == "forbid_command":
                found = next((t for t in facts.texts if r.regex.search(t)), None)
            else:
                # > and >> only write, so a rule about reading skips their targets.
                reads_only = "read" in r.kinds and not r.kinds & {"edit", "write"}
                candidates = facts.paths if reads_only else facts.paths + facts.outputs
                # The hook cannot tell whether a cd ran, so a relative path counts from the
                # starting folder and from the folder cd went to; either one can match.
                found = next((p for p, here in candidates
                              if any(glob_matches(r.pattern, p, start, root=root or cwd)
                                     for start in ((cwd, os.path.join(cwd, here)) if here else (cwd,)))), None)
        elif r.kind == "protect_path" and kind in r.kinds:
            found = next((p for p in call.get("paths") or [] if glob_matches(r.pattern, p, cwd, root=root)), None)
        if found is not None:
            yield {"rule": r.id, "match": found}


def check(rules, call):
    """The first rule the call breaks, as {"rule": id, "match": what matched}, or None."""
    return next(matches(rules, call), None)


# ---------------------------------------------------------------------------
# The hook
# ---------------------------------------------------------------------------

HARNESSES = ("claude-code", "codex", "gemini-cli", "cursor")
USAGE = """usage: rules_guard.py [--harness claude-code|codex|gemini-cli|cursor] < hook-event.json

A pre-tool hook made by rules-to-guards. It reads one hook event as JSON on stdin.
When the call breaks one of the rules embedded in this file, it exits 2 with the
reason on stderr (Cursor also gets a JSON deny on stdout). Otherwise it prints {}
and exits 0. Any error inside the hook allows the call. Rules embedded: %d.
"""


def deny_reason(rule):
    what = (rule.message or "this call breaks one of your rules").rstrip(". ") + "."
    where = " from %s" % rule.source if rule.source else ""
    return ("Blocked by rules-to-guards (rule %s%s): %s If this rule should not apply here, "
            "ask the user instead of working around it." % (rule.id, where, what))


def _read(stream):
    buffer = getattr(stream, "buffer", None)
    return buffer.read().decode("utf-8", "replace") if buffer is not None else stream.read()


def _write(stream, text):
    buffer = getattr(stream, "buffer", None)
    if buffer is not None:
        buffer.write(text.encode("utf-8", "replace"))
        buffer.flush()
    else:
        stream.write(text)


def main(argv=None, stdin=None, stdout=None, stderr=None, rules=None):
    """Exit 0 with {} on stdout to allow; exit 2 with the reason on stderr to
    block. Cursor also gets a JSON deny decision on stdout. Arguments are read by
    hand: an argument error must never exit 2, because exit 2 means block."""
    stdin, stdout, stderr = stdin or sys.stdin, stdout or sys.stdout, stderr or sys.stderr
    argv = list(sys.argv[1:] if argv is None else argv)
    if "--help" in argv or "-h" in argv:
        _write(stdout, USAGE % len(RULES if rules is None else rules))
        return 0
    harness = "claude-code"
    if "--harness" in argv:
        i = argv.index("--harness")
        if i + 1 < len(argv) and argv[i + 1] in HARNESSES:
            harness = argv[i + 1]
    try:
        skipped = []
        compiled = compile_rules(RULES if rules is None else rules, skipped)
        for rule_id, reason in skipped:
            _write(stderr, "rules-to-guards: rule %s has an error (%s) and is skipped.\n" % (rule_id, reason))
        data = json.loads(_read(stdin) or "null")
        hit = check(compiled, normalize_hook_input(data)) if isinstance(data, dict) else None
        broken = next((r for r in compiled if hit and r.id == hit["rule"]), None)
    except Exception as exc:  # fail open: a bug in this hook must never block work
        try:
            _write(stderr, "rules-to-guards: internal error (%s); the call is allowed.\n" % type(exc).__name__)
            _write(stdout, "{}\n")
        except Exception:
            pass
        return 0
    if broken is None:
        _write(stdout, "{}\n")
        return 0
    reason = deny_reason(broken)
    try:
        if harness == "cursor":
            _write(stdout, json.dumps({"permission": "deny", "user_message": reason, "agent_message": reason}) + "\n")
        _write(stderr, reason + "\n")
    except Exception:
        pass
    return 2


if __name__ == "__main__":
    sys.exit(main())
