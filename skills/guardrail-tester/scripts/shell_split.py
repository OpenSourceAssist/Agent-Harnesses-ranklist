"""Split a shell command line into the simple commands it would run.

This is static analysis for simulating permission rules. Nothing is executed.
It follows bash syntax closely enough for rule matching: quotes, escapes, line
continuations, the separators && || ; | |& & and newlines, subshells, command
substitution ($(...) and backticks), process substitution, redirections,
here-documents (their bodies are data, not commands), comments, leading
VAR=value assignments, and the bodies of if, for, while, until, case, brace
groups, and functions.

Each simple command keeps its words as written (quotes and escapes kept), so a
rule such as Bash(git push *) does not match `git 'push' origin main`, which is
how Claude Code documents it. The values with quotes removed are kept too.

Run with --help to print this text. Python 3.9+, standard library only.
"""

from __future__ import annotations

import re
import sys
from dataclasses import dataclass, field

_OPS = ("&&", "||", ";;", "|&", ";", "|", "&")
_WORD_END = set(" \t\n;&|()<>")
_REDIR_RE = re.compile(r"(\d*)(>>|>\||>&|>|<<<|<<-|<<|<>|<&|<)")
_ASSIGN_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*(\[[^\]]*\])?\+?=")
_OPENERS = {"if", "while", "until", "for", "select", "case"}
_CLOSERS = {"fi", "done", "esac"}
_SKIP_WORDS = {"then", "do", "else", "elif", "!", "{", "}"}
_SEPARATORS = {"&&", "||", ";", "|", "|&", "&", "\n"}


@dataclass
class Command:
    """One simple command: its words, leading assignments, and redirections."""
    words: list = field(default_factory=list)        # raw tokens, quotes kept
    values: list = field(default_factory=list)       # tokens with quotes and escapes removed
    assignments: list = field(default_factory=list)  # leading NAME=value tokens, raw
    redirects: list = field(default_factory=list)    # (op, raw target, target value)
    nested: bool = False                             # inside (), $(), ``, <(), or a control body
    flags: list = field(default_factory=list)        # per word: set of glob, variable, substitution
    scripts: list = field(default_factory=list, repr=False)  # nested command text found in this command

    @property
    def text(self) -> str:
        return " ".join(self.words)

    def file_redirects(self) -> list:
        """[(">" or "<", target)] for the redirections that name a file.

        File-descriptor copies (2>&1), here-documents, and here-strings name no
        file and are left out."""
        out = []
        for op, _raw, value in self.redirects:
            if op in ("<<", "<<-", "<<<"):
                continue
            if op in (">&", "<&") and (value.isdigit() or value == "-"):
                continue
            out.append(("<" if op in ("<", "<&") else ">", value))
        return out


@dataclass
class Parsed:
    commands: list = field(default_factory=list)
    ops: set = field(default_factory=set)        # separators used at this level
    features: set = field(default_factory=set)   # substitution, variable, glob, redirection, ...
    dangling: bool = False                       # ends with && or || and nothing after
    error: str = ""                              # why the text could not be parsed


def _ansi_c(body):
    table = {"n": "\n", "t": "\t", "r": "\r", "\\": "\\", "'": "'", '"': '"', "a": "\a", "e": "\x1b"}
    out, i = [], 0
    while i < len(body):
        if body[i] == "\\" and i + 1 < len(body):
            out.append(table.get(body[i + 1], "\\" + body[i + 1]))
            i += 2
        else:
            out.append(body[i])
            i += 1
    return "".join(out)


class _Lexer:
    """Turns text into tokens: ("word", raw, value, flags, scripts), ("op", op), ("redir", op)."""

    def __init__(self, text):
        self.s = text
        self.n = len(text)
        self.i = 0
        self.tokens = []
        self.features = set()
        self.heredocs = []        # pending (delimiter, strip leading tabs)
        self.skipped = []         # (start, end) of here-document bodies and comments
        self.error = ""

    def _fail(self, why):
        if not self.error:
            self.error = why

    # -- scanning helpers ---------------------------------------------------
    def match_close(self, i, open_ch, close_ch):
        """Index of the close_ch that balances an open_ch just before i, or -1."""
        depth = 1
        while i < self.n:
            c = self.s[i]
            if c == "\\":
                i += 2
                continue
            if c == "'":
                j = self.s.find("'", i + 1)
                if j < 0:
                    return -1
                i = j + 1
                continue
            if c == '"' or c == "`":
                j = self.end_double(i + 1) if c == '"' else self.end_backtick(i + 1)
                if j < 0:
                    return -1
                i = j + 1
                continue
            if c == open_ch:
                depth += 1
            elif c == close_ch:
                depth -= 1
                if depth == 0:
                    return i
            i += 1
        return -1

    def end_double(self, i):
        """Index of the quote that closes a double-quoted string whose body starts at i."""
        while i < self.n:
            c = self.s[i]
            if c == "\\":
                i += 2
                continue
            if c == '"':
                return i
            if self.s.startswith("$(", i):
                j = self.match_close(i + 2, "(", ")")
                if j < 0:
                    return -1
                i = j + 1
                continue
            if c == "`":
                j = self.end_backtick(i + 1)
                if j < 0:
                    return -1
                i = j + 1
                continue
            i += 1
        return -1

    def end_backtick(self, i):
        while i < self.n:
            if self.s[i] == "\\":
                i += 2
                continue
            if self.s[i] == "`":
                return i
            i += 1
        return -1

    # -- main loop ------------------------------------------------------------
    def run(self):
        s = self.s
        while self.i < self.n and not self.error:
            c = s[self.i]
            if s.startswith("\\\n", self.i):
                self.i += 2
                continue
            if c in " \t":
                self.i += 1
                continue
            if c == "\n":
                self.tokens.append(("op", "\n"))
                self.i += 1
                self._skip_heredoc_bodies()
                continue
            if c == "#":
                j = s.find("\n", self.i)
                end = self.n if j < 0 else j
                self.skipped.append((self.i, end))
                self.i = end
                continue
            if c in "<>" and s.startswith("(", self.i + 1):
                self._word()
                continue
            op = next((o for o in ("&>>", "&>") if s.startswith(o, self.i)), None)
            if op:
                self.tokens.append(("redir", op))
                self.features.add("redirection")
                self.i += len(op)
                continue
            op = next((o for o in _OPS if s.startswith(o, self.i)), None)
            if op:
                self.tokens.append(("op", op))
                self.i += len(op)
                continue
            if c in "()":
                self.tokens.append(("op", c))
                self.i += 1
                continue
            m = _REDIR_RE.match(s, self.i)
            if m:
                self._redirect(m)
                continue
            self._word()
        return self

    def _redirect(self, m):
        op = m.group(2)
        self.i = m.end()
        self.tokens.append(("redir", op))
        self.features.add("redirection")
        if op in ("<<", "<<-"):
            self.features.add("heredoc")
            while self.i < self.n and self.s[self.i] in " \t":
                self.i += 1
            count = len(self.tokens)
            self._word()
            if len(self.tokens) > count and self.tokens[-1][0] == "word":
                self.heredocs.append((self.tokens[-1][2], op == "<<-"))
        elif op == "<<<":
            self.features.add("heredoc")

    def _skip_heredoc_bodies(self):
        while self.heredocs:
            delim, strip_tabs = self.heredocs.pop(0)
            start = self.i
            while self.i < self.n:
                j = self.s.find("\n", self.i)
                line = self.s[self.i:] if j < 0 else self.s[self.i:j]
                self.i = self.n if j < 0 else j + 1
                if (line.lstrip("\t") if strip_tabs else line) == delim:
                    break
            self.skipped.append((start, self.i))

    def _word(self):
        s = self.s
        raw, value, flags, scripts = [], [], set(), []
        start = self.i
        while self.i < self.n:
            c = s[self.i]
            if c in _WORD_END:
                if c in "<>" and self.i == start and s.startswith("(", self.i + 1):
                    j = self.match_close(self.i + 2, "(", ")")
                    if j < 0:
                        self._fail("unclosed process substitution")
                        return
                    scripts.append(s[self.i + 2:j])
                    raw.append(s[self.i:j + 1])
                    value.append(s[self.i:j + 1])
                    self.features.add("process_substitution")
                    self.i = j + 1
                    continue
                break
            if c == "\\":
                if s.startswith("\\\n", self.i):
                    self.i += 2
                    continue
                nxt = s[self.i + 1:self.i + 2]
                raw.append(c + nxt)
                value.append(nxt)
                self.i += 2
                continue
            if c == "'":
                j = s.find("'", self.i + 1)
                if j < 0:
                    self._fail("unclosed single quote")
                    return
                raw.append(s[self.i:j + 1])
                value.append(s[self.i + 1:j])
                self.i = j + 1
                continue
            if c == '"':
                j = self.end_double(self.i + 1)
                if j < 0:
                    self._fail("unclosed double quote")
                    return
                raw.append(s[self.i:j + 1])
                value.append(self._double_value(s[self.i + 1:j], flags, scripts))
                self.i = j + 1
                continue
            if c == "`":
                j = self.end_backtick(self.i + 1)
                if j < 0:
                    self._fail("unclosed backtick")
                    return
                scripts.append(s[self.i + 1:j])
                raw.append(s[self.i:j + 1])
                value.append(s[self.i:j + 1])
                flags.add("substitution")
                self.i = j + 1
                continue
            if c == "$":
                self._dollar(raw, value, flags, scripts)
                if self.error:
                    return
                continue
            if c in "*?[":
                flags.add("glob")
            raw.append(c)
            value.append(c)
            self.i += 1
        word = "".join(raw)
        if word in ("[", "[[", "]", "]]"):
            flags.discard("glob")
        self.features |= flags
        self.tokens.append(("word", word, "".join(value), flags, scripts))

    def _dollar(self, raw, value, flags, scripts):
        s, i = self.s, self.i
        if s.startswith("$((", i):
            j = self.match_close(i + 3, "(", ")")
            if j < 0 or not s.startswith(")", j + 1):
                self._fail("unclosed arithmetic expansion")
                return
            seg, self.i = s[i:j + 2], j + 2
            self.features.add("arithmetic")
        elif s.startswith("$(", i):
            j = self.match_close(i + 2, "(", ")")
            if j < 0:
                self._fail("unclosed command substitution")
                return
            scripts.append(s[i + 2:j])
            flags.add("substitution")
            seg, self.i = s[i:j + 1], j + 1
        elif s.startswith("$'", i):
            j = i + 2
            while j < self.n and s[j] != "'":
                j += 2 if s[j] == "\\" else 1
            if j >= self.n:
                self._fail("unclosed ANSI-C quote")
                return
            raw.append(s[i:j + 1])
            value.append(_ansi_c(s[i + 2:j]))
            self.i = j + 1
            return
        elif s.startswith("${", i):
            j = self.match_close(i + 2, "{", "}")
            if j < 0:
                self._fail("unclosed parameter expansion")
                return
            flags.add("variable")
            seg, self.i = s[i:j + 1], j + 1
        else:
            m = re.match(r"\$([A-Za-z_][A-Za-z0-9_]*|[0-9@*#?$!-])", s[i:])
            if m:
                flags.add("variable")
                seg = m.group(0)
            else:
                seg = "$"
            self.i = i + len(seg)
        raw.append(seg)
        value.append(seg)

    def _double_value(self, body, flags, scripts):
        inner = _Lexer(body)
        out, i, n = [], 0, len(body)
        while i < n:
            c = body[i]
            if c == "\\" and i + 1 < n and body[i + 1] in '"\\$`\n':
                if body[i + 1] != "\n":
                    out.append(body[i + 1])
                i += 2
                continue
            if body.startswith("$(", i) and not body.startswith("$((", i):
                j = inner.match_close(i + 2, "(", ")")
                j = n - 1 if j < 0 else j
                scripts.append(body[i + 2:j])
                flags.add("substitution")
                out.append(body[i:j + 1])
                i = j + 1
                continue
            if c == "`":
                j = inner.end_backtick(i + 1)
                j = n - 1 if j < 0 else j
                scripts.append(body[i + 1:j])
                flags.add("substitution")
                out.append(body[i:j + 1])
                i = j + 1
                continue
            if c == "$" and i + 1 < n and (body[i + 1] in "{_@*#?$!-" or body[i + 1].isalnum()):
                flags.add("variable")
            out.append(c)
            i += 1
        return "".join(out)


class _Builder:
    """Turns tokens into simple commands, skipping control-flow keywords.

    Each finished command is followed by the commands nested inside it
    (command and process substitution), which count as nested."""

    def __init__(self, nested):
        self.base_nested = nested
        self.out = []
        self.depth = 0
        self.cur = None
        self.skip_header = False     # the words of a for/select header
        self.case_pattern = False    # inside case, before the ")" of an arm
        self.cases = 0
        self.parens = 0              # open subshells
        self.subshells = 0           # subshells seen
        self.error = ""

    def _command(self):
        if self.cur is None:
            self.cur = Command(nested=self.base_nested or self.depth > 0)
        return self.cur

    def _emit_scripts(self, scripts):
        for script in scripts:
            self.out.extend(_parse(script, nested=True).commands)

    def end(self):
        cmd, self.cur = self.cur, None
        if cmd is None:
            return
        if cmd.words:
            self.out.append(cmd)
        self._emit_scripts(cmd.scripts)

    def feed(self, tokens):
        redirect = None
        i = 0
        while i < len(tokens):
            tok = tokens[i]
            i += 1
            if tok[0] == "op":
                redirect = None
                op = tok[1]
                if op == "(":
                    if self.case_pattern:
                        continue                 # "(pattern)" form of a case arm
                    if self.cur is not None and len(self.cur.words) == 1 and not self.cur.assignments \
                            and i < len(tokens) and tokens[i] == ("op", ")"):
                        self.cur = None          # "name()" starts a function definition
                        i += 1
                        continue
                    self.end()
                    self.depth += 1
                    self.parens += 1
                    self.subshells += 1
                elif op == ")":
                    if self.case_pattern:
                        self.case_pattern = False
                        self.cur = None
                        continue
                    if self.parens == 0:
                        self.error = "unbalanced parenthesis"
                        return
                    self.end()
                    self.parens -= 1
                    self.depth = max(0, self.depth - 1)
                else:
                    self.end()
                    self.skip_header = False
                    if op == ";;" and self.cases:
                        self.case_pattern = True
                continue
            if tok[0] == "redir":
                self._command()
                redirect = tok[1]
                continue
            _, raw, value, flags, scripts = tok
            if redirect is not None:
                self.cur.redirects.append((redirect, raw, value))
                self.cur.scripts.extend(scripts)
                redirect = None
                continue
            if self.skip_header or self.case_pattern:
                self._emit_scripts(scripts)
                if self.case_pattern and raw == "esac":
                    self.case_pattern = False
                    self._close_case()
                elif self.case_pattern and raw.endswith(")"):
                    self.case_pattern = False
                continue
            starting = self.cur is None or (not self.cur.words and not self.cur.assignments)
            if starting and raw in _OPENERS:
                self.depth += 1
                if raw in ("for", "select"):
                    self.skip_header = True
                elif raw == "case":
                    self.cases += 1
                    while i < len(tokens) and not (tokens[i][0] == "word" and tokens[i][1] == "in"):
                        if tokens[i][0] == "word":
                            self._emit_scripts(tokens[i][4])
                        i += 1
                    i += 1
                    self.case_pattern = True
                continue
            if starting and raw in _CLOSERS:
                if raw == "esac":
                    self._close_case()
                else:
                    self.depth = max(0, self.depth - 1)
                continue
            if starting and raw == "function":
                i += 1
                if i + 1 < len(tokens) and tokens[i] == ("op", "(") and tokens[i + 1] == ("op", ")"):
                    i += 2
                continue
            if starting and raw in _SKIP_WORDS:
                if raw == "{":
                    self.depth += 1
                elif raw == "}":
                    self.depth = max(0, self.depth - 1)
                continue
            cmd = self._command()
            if not cmd.words and _ASSIGN_RE.match(raw):
                cmd.assignments.append(raw)
            else:
                cmd.words.append(raw)
                cmd.values.append(value)
                cmd.flags.append(set(flags))
            cmd.scripts.extend(scripts)
        self.end()
        if self.parens and not self.error:
            self.error = "unbalanced parenthesis"

    def _close_case(self):
        self.cases = max(0, self.cases - 1)
        self.depth = max(0, self.depth - 1)


def _parse(text, nested=False) -> Parsed:
    lexer = _Lexer(text).run()
    parsed = Parsed(features=set(lexer.features), error=lexer.error)
    if lexer.error:
        return parsed
    for tok in lexer.tokens:
        if tok[0] == "op" and tok[1] in _SEPARATORS:
            parsed.ops.add(tok[1])
        elif tok[0] == "word" and tok[1] in _OPENERS | {"function", "{"}:
            parsed.features.add("control")
    tail = [t for t in lexer.tokens if t != ("op", "\n")]
    if tail and tail[-1][0] == "op" and tail[-1][1] in ("&&", "||"):
        parsed.dangling = True
    elif tail and tail[-1][0] == "op" and tail[-1][1] in ("|", "|&"):
        parsed.error = "unfinished pipeline"
        return parsed
    builder = _Builder(nested)
    builder.feed(lexer.tokens)
    if builder.error:
        parsed.error = builder.error
        return parsed
    parsed.commands = builder.out
    for cmd in parsed.commands:
        if cmd.assignments:
            parsed.features.add("assignment")
        for flags in cmd.flags:
            parsed.features |= flags
    if builder.subshells:
        parsed.features.add("subshell")
    return parsed


def parse(command) -> Parsed:
    """Parse one command line. Odd input never raises: see Parsed.error."""
    if not isinstance(command, str):
        command = "" if command is None else str(command)
    return _parse(command)


def without_heredocs_and_comments(text) -> str:
    """The text with here-document bodies and comments cut out; everything
    else stays exactly as written."""
    text = text if isinstance(text, str) else ""
    lexer = _Lexer(text).run()
    out, pos = [], 0
    for start, end in sorted(lexer.skipped):
        if start >= pos:
            out.append(text[pos:start])
            pos = end
    out.append(text[pos:])
    return "".join(out)


def split_chain(text, ops=("&&", "||", ";")) -> list:
    """Split text at the given top-level operators only, keeping quotes,
    escapes, and anything inside parentheses or backticks whole."""
    parts, buf, i, n, depth, quote = [], [], 0, len(text or ""), 0, None
    text = text or ""
    while i < n:
        c = text[i]
        if quote:
            buf.append(c)
            if c == "\\" and quote == '"' and i + 1 < n:
                buf.append(text[i + 1])
                i += 2
                continue
            if c == quote:
                quote = None
            i += 1
            continue
        if c == "\\" and i + 1 < n:
            buf.append(text[i:i + 2])
            i += 2
            continue
        if c in "'\"`":
            quote = c
            buf.append(c)
            i += 1
            continue
        if c == "(":
            depth += 1
        elif c == ")":
            depth = max(0, depth - 1)
        if depth == 0:
            op = next((o for o in ops if text.startswith(o, i)), None)
            if op and not (op == ";" and text.startswith(";;", i)):
                parts.append("".join(buf))
                buf = []
                i += len(op)
                continue
        buf.append(c)
        i += 1
    parts.append("".join(buf))
    return [p.strip() for p in parts if p.strip()]


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] in ("-h", "--help"):
        print(__doc__.strip())
        sys.exit(0)
    print("shell_split is a helper module for test_guards.py; run it with --help for details.")
