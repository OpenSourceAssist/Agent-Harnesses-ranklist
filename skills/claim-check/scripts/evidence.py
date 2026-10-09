"""Read the evidence behind "tests pass" claims in coding-agent sessions.

Three readers, used by claims.py (the report) and stop_hook.py (the hook):

- find_claims(text): sentences in which the agent says tests or a build pass.
- shell commands: which test and build runs a command makes, how each ended,
  and which files it changes.
- label_claims(session): walks one session in order (plus its subagents) and
  labels each claim backed, stale, contradicted, unsupported, or unclear.

Python 3.9+, standard library only. Read-only.
"""

from __future__ import annotations

import calendar
import glob
import json
import os
import re
from typing import Optional

TESTS, BUILD = "tests", "build"

# ---------------------------------------------------------------------------
# Claims
# ---------------------------------------------------------------------------

_T_TOKEN, _B_TOKEN = "RUNNERTESTS", "RUNNERBUILD"

_NOUN_T = r"(?:test suites?|test cases?|tests?|suites?|specs?|%s)" % _T_TOKEN
_FILL_T = (r"(?:\s+(?:are|is|were|was|all|now|still|also|again|both|locally|fully|each|have|has|had|did|do|"
           r"finally|consistently|\d+(?:\s*/\s*\d+)?|\(\d+(?:\s*/\s*\d+)?\)))")
_VERB_T = r"(?:pass|passes|passed|passing|succeed|succeeds|succeeded|green)"
_NOUN_B = (r"(?:builds?|typechecks?|type-checks?|type checks?|typechecking|type checking|tsc|typescript|mypy|"
           r"pyright|compiles?|compilation|%s)" % _B_TOKEN)
_FILL_B = (r"(?:\s+(?:is|was|are|were|still|now|also|again|both|fully|runs?|ran|remains?|stays?|came back|"
           r"comes back|went))")
_VERB_B = (r"(?:pass|passes|passed|passing|succeeds|succeeded|successful|successfully|clean|cleanly|green|"
           r"(?:with|without) (?:no |any )?errors)")

# (kind, pattern, needs test words elsewhere in the sentence)
_CLAIM_RES = [
    (TESTS, re.compile(r"\b%s%s{0,3}\s+%s\b" % (_NOUN_T, _FILL_T, _VERB_T), re.I), False),
    (TESTS, re.compile(r"\btests? and (?:they|all|both|each)(?:\s+(?:all|now|still|also|both))?\s+"
                       r"(?:pass|passed|passing)\b", re.I), False),
    (TESTS, re.compile(r"\b(?:no|zero|0) (?:failing|failed) (?:tests?|specs?)\b|\bno test failures\b", re.I), False),
    (TESTS, re.compile(r"\b\d+ (?:tests? )?passed\b", re.I), False),
    (TESTS, re.compile(r"\b\d+\s*/\s*\d+\s+(?:\w+\s+){0,2}?(?:pass|passes|passed|passing|green)\b", re.I), True),
    (TESTS, re.compile(r"\b\d+ (?:pass|passing)\b", re.I), True),
    (TESTS, re.compile(r"\b\d+ of \d+\s+(?:[\w-]+\s+){0,2}?(?:(?:tests?|specs?)\s+)?(?:pass|passes|passed|passing|"
                       r"green)\b", re.I), True),
    (TESTS, re.compile(r"\b(?:passed|passes|passing) (?:all )?\d+(?: of \d+)? (?:[\w-]+ )?(?:tests?|specs?)\b", re.I),
     False),
    (BUILD, re.compile(r"\b%s%s{0,2}\s+%s\b" % (_NOUN_B, _FILL_B, _VERB_B), re.I), False),
    (BUILD, re.compile(r"\b(?:no|zero|0) (?:type|typescript|ts|tsc|compile|compiler|compilation|build|mypy) "
                       r"errors\b", re.I), False),
]
_TEST_WORDS = re.compile(r"\b(?:tests?|suites?|specs?|pytest|jest|vitest|mocha|%s)\b" % _T_TOKEN, re.I)

# Anywhere in the sentence: someone else's words, a remote run, a partial or hedged claim, or history.
_SKIP_SENTENCE = re.compile(
    r"\b(?:claims?|claimed|claiming|said|says|saying|according to|reportedly|supposedly|allegedly|pretend\w*|"
    r"eg|ie|for example|for instance|such as|GitHub Actions|workflow runs?|except|apart from|aside from|"
    r"other than|all but|but (?:one|two|three)|looks? like|seems?|seemed|appears?|appeared|probably|likely|"
    r"presumably|possibly|I think|I believe|hopefully|hope|previously|earlier|originally|initially|used to|"
    r"at first|before (?:the|my|our|this|that|these|those|your) \w+|upstream|their|your|on main|"
    r"in the (?:other|original|upstream)|yesterday|last (?:week|night|time)|ago|reported|reports|mentioned|told|"
    r"wrote|hypothetically|in theory)\b", re.I)
_CI = re.compile(r"\bCI\b")
_REMOTE_AFTER = re.compile(r"\s+(?:in|on|via|under|through)\s+(?:the\s+)?(?:CI|GitHub)\b")
# Before the claim in the same sentence, or after it in the same clause: the claim is a plan, a
# condition, a goal, or a hedge, not a statement of fact.
_NOT_FACT = re.compile(
    r"(?:\b(?:will|would|should|could|might|may|must|shall|can|cannot|going to|gonna|need|needs|needed|"
    r"have to|has to|had to|want|wants|wanted|try|tries|trying|attempt\w*|expect\w*|assum\w*|suppos\w*|if|"
    r"unless|once|until|whether|when|whenever|let me|let's|lets|let us|make sure|ensure\w*|verify|verifies|"
    r"confirm|confirms|check|checks|make|makes|get|gets|getting|keep|keeps|goal|criteri\w*|requir\w*|"
    r"wait\w*|plan\w*|ideally|eventually|so that|in order)\b|'ll\b|'d\b|\bto (?:pass|succeed|be green|go green))",
    re.I)
# Anywhere before the claim, or as a list header: a plan, a goal, or a criterion, not a result.
_PLAN_WORDS = re.compile(r"\b(?:done when|definition of done|goal|target|todo|expected|success|acceptance|criteria|"
                         r"next steps?|plan|verification)\b|\bstatus\s*:", re.I)
# In the clause before the claim: only part of the tests passed.
_PART = re.compile(r"\b(?:zero|0|only|just|most|some|few|several|half|almost|nearly|barely|partially)\b", re.I)
_NOW_AND_THEN = re.compile(r"\b(?:sometimes|intermittently|on retry)\b", re.I)
_FAIL_NEAR_TESTS = re.compile(r"\b(?:tests?|specs?|suites?|\d+)\b[^.]{0,40}\b(?:fail\w*|failures?|broken|erroring)\b",
                              re.I)
_CLAUSE_SPLIT = re.compile(r"[,;:()]|\s[-\u2013\u2014]\s|\b(?:but|however|although|though|while|whereas)\b", re.I)
_ZERO_FAIL = re.compile(r"\b(?:0|zero|no) (?:tests? |specs? )?(?:fail|fails|failed|failures?|failing|errors?)\b|"
                        r"\b(?:with|without) (?:no |any )?(?:errors?|failures?)\b", re.I)
_FAIL_WORDS = re.compile(r"\b(?:fail|fails|failed|failing|failures?|errors?|broken)\b", re.I)
_NEGATION = re.compile(
    r"\b(?:not|no|never|none|nothing|neither|nor|cannot|without|fail|fails|failed|failing|failures?|broken|"
    r"breaks?|broke|errors?|red|flaky|timed out|timeout|crash\w*)\b|n't\b", re.I)
_AFTER_OBJECT = re.compile(r"\s+(?:a|an|the|its|their|his|her|this|that|these|those|through|along|over|into|"
                           r"it|them|my|our|your)\b", re.I)
_BEFORE_NOUN_PASS = re.compile(r"\b(?:a|an|the|this|that|each|every|another|one|one more|second|third|final|last|"
                               r"quick|full|first|next)\s+$", re.I)
_PARTIAL = re.compile(r"(\d+)\s*(?:/|of)\s*(\d+)")
_ID_BEFORE = re.compile(r"\b(?:task|step|phase|round|stage|check|gate|case|item|part|chapter|section|issue|pr|ticket|"
                        r"story|milestone|level|batch|wave|test|attempt|run|trial|option|scenario|job)\s*#?\s*$", re.I)
_MANUAL = re.compile(r"\b(?:live|money|e-?mail|manual|real|real-money|purchase|payment|checkout|deploy|deployment|"
                     r"production|prod|canary|browser|smoke|sanity|cold|load|stress|user|acceptance|hand)[\s-]+"
                     r"tests?\b", re.I)
_CODE_LIKE = re.compile(r'\\"|\{"|":\s*["\d\[{]|2>&1|\$\(|\$\{')
_KEEP_WORDS = {"test", "tests", "suite", "build", "typecheck", "type-check", "tsc", "mypy", "pyright", "typescript",
               "compile", "compiles"}
_ABBREVIATIONS = re.compile(r"\b(e\.g|i\.e|etc|vs|approx|cf)\.", re.I)
_SENTENCE_END = re.compile(r"(?<=[.!?])\s+")
_CODE_SPAN = re.compile(r"`([^`\n]+)`")
_QUOTED = re.compile(r"\"[^\"\n]{0,200}\"|“[^”\n]{0,200}”|(?<![\w'])'[^'\n]{1,120}'(?![\w'])")
_LINK = re.compile(r"\[([^\]\n]*)\]\([^)\n]*\)")
_LIST_MARK = re.compile(r"^\s*(?:[-*+]|\d+[.)])\s+")


def _code_token(code) -> str:
    """Stand-in for code or a quote: a runner command becomes a runner word,
    a single claim word stays, anything else becomes CODE."""
    kinds = command_kinds(code)
    if TESTS in kinds:
        return " %s " % _T_TOKEN
    if BUILD in kinds:
        return " %s " % _B_TOKEN
    return " %s " % code.strip() if code.strip().lower() in _KEEP_WORDS else " CODE "


def _prose_lines(text):
    """Lines of prose: code blocks (fenced or indented), code-like lines,
    quotes, tables, open task items, and lists under a plan header such as
    "Success criteria:" left out."""
    fence, blank, indented, criteria = None, True, False, False
    for line in text.splitlines():
        stripped = line.strip()
        if fence:
            if stripped.startswith(fence):
                fence = None
            continue
        if stripped.startswith(("```", "~~~")):
            fence = stripped[:3]
            continue
        if not stripped:
            blank = True
            continue
        if criteria and (_LIST_MARK.match(line) or line[:1] in " \t"):
            blank = False
            continue  # an item (or its continuation) under "Success criteria:" or "Next steps:"
        header = _LIST_MARK.sub("", re.sub(r"^#+\s*", "", stripped)).replace("**", "").replace("__", "").strip()
        criteria = header.endswith(":") and bool(_PLAN_WORDS.search(header))
        code = bool(re.match(r"^(?: {4,}|\t)", line)) and not re.match(r"^\s*(?:[-*+]|\d+[.)])\s", line)
        indented, blank = code and (blank or indented), False
        if indented or _CODE_LIKE.search(stripped):
            continue
        if stripped.startswith((">", "|")) or re.match(r"^(?:[-*+]|\d+[.)])\s+\[ \]", stripped):
            continue
        stripped = re.sub(r"^#+\s*", "", stripped)
        stripped = _LIST_MARK.sub("", stripped)
        stripped = re.sub(r"^\[[xX]\]\s*", "", stripped)
        yield stripped


def _normalize(sentence) -> str:
    s = _CODE_SPAN.sub(lambda m: _code_token(m.group(1)), sentence)
    s = _QUOTED.sub(lambda m: _code_token(m.group(0)[1:-1]), s)
    s = _LINK.sub(r"\1", s)
    s = s.replace("**", "").replace("__", "")
    s = re.sub(r"(?<!\w)\*(?=\S)|(?<=\S)\*(?!\w)", "", s)
    return " ".join(s.split())


def _clause(text, start, end) -> tuple:
    """The clause around text[start:end], as (before the match, after the match)."""
    left = 0
    for m in _CLAUSE_SPLIT.finditer(text, 0, start):
        left = m.end()
    m = _CLAUSE_SPLIT.search(text, end)
    right = m.start() if m else len(text)
    return text[left:start], text[end:right]


def _is_fact(sentence, m, needs_test_words) -> bool:
    if needs_test_words and not _TEST_WORDS.search(sentence[:m.start()] + " " + sentence[m.end():]):
        return False
    after, words = sentence[m.end():], m.group(0).lower().split()
    if _AFTER_OBJECT.match(after) and words[-1] not in ("passing", "green"):
        return False  # "the test passes a token to ...": a verb with an object, not a result
    if words[-1] == "pass" and not words[0].endswith("s") and _BEFORE_NOUN_PASS.search(sentence[:m.start()]):
        return False  # "a final test pass": the noun, not the verb
    if words[-1] == "passing" and re.match(r"\s+(?:tests?|specs?|cases?|checks?)\b", after, re.I):
        return False  # "22 passing tests": a description of the suite, not a run
    if _CI.search(sentence[:m.start()]) or _REMOTE_AFTER.match(after):
        return False  # a claim about a remote run the transcript cannot show
    if _MANUAL.search(sentence[max(0, m.start() - 30):m.end()]):
        return False  # "the live test passed": a check by hand, not a test run
    if words[0][:1].isdigit() and _ID_BEFORE.search(sentence[:m.start()]):
        return False  # "Task 2 passed": a name, not a count
    before, rest = _clause(sentence, m.start(), m.end())
    if any(a != b for a, b in _PARTIAL.findall(before + m.group(0) + rest)):
        return False  # "21/22 pass": some did not
    count = re.search(r"\d+", m.group(0))
    if count and int(count.group(0)) == 0 and not _FAIL_WORDS.search(m.group(0)):
        return False  # "0 passed"
    if _PART.search(before) or _NOW_AND_THEN.search(rest):
        return False  # "only 3 tests pass", "the suite passes sometimes"
    if _FAIL_NEAR_TESTS.search(_ZERO_FAIL.sub(" ", sentence)):
        return False  # "the unit tests pass but the integration tests fail"
    prefix = sentence[:m.start()]
    if _NOT_FACT.search(prefix) or _PLAN_WORDS.search(prefix) or _NOT_FACT.search(rest):
        return False  # "Done when: all tests pass", "When the build succeeds, ..."
    return not (_NEGATION.search(_ZERO_FAIL.sub(" ", before)) or _FAIL_WORDS.search(_ZERO_FAIL.sub(" ", rest)))


def _scope(rx, m) -> str:
    """"one" when the claim names a single test ("the parser test passes"), else "all"."""
    words = m.group(0).lower().split()
    if rx is _CLAIM_RES[0][1] and words[0] in ("test", "spec") and (len(words) < 2 or words[1] not in (
            "suite", "suites", "cases")):
        return "one"
    return "all"


def _programs(command) -> set:
    """The runner programs in a command (npm, pytest, go, ...)."""
    out = set()
    for seg in _expand(parse_shell(command)):
        w = _unwrap(seg.words)
        if w and _segment_kinds(seg.words):
            out.add(_base(w[0]))
    return out


def find_claims(text) -> list:
    """Claims that tests or a build pass, at most one per kind, in text order:
    [{"kind": "tests" | "build", "excerpt": <the sentence as written>,
    "scope": "one" (a single named test) | "all", "commands": runner programs
    the sentence names in inline code, such as ["npm"] for `npm test`}]."""
    if not text or not isinstance(text, str):
        return []
    found = {}
    for line in _prose_lines(text):
        codes = []
        protected = _CODE_SPAN.sub(lambda m: codes.append(m.group(0)) or "\x00%d\x00" % (len(codes) - 1), line)
        protected = _ABBREVIATIONS.sub(lambda m: m.group(1).replace(".", ""), protected)
        for part in _SENTENCE_END.split(protected):
            original = re.sub("\x00(\\d+)\x00", lambda m: codes[int(m.group(1))], part).strip()
            if not original or original.endswith("?"):
                continue
            sentence = _normalize(original)
            if _SKIP_SENTENCE.search(sentence):
                continue
            for kind, rx, needs_words in _CLAIM_RES:
                if kind in found:
                    continue
                m = next((m for m in rx.finditer(sentence) if _is_fact(sentence, m, needs_words)), None)
                if m:
                    named = sorted(set().union(*[_programs(c) for c in _CODE_SPAN.findall(original)]))
                    found[kind] = (original.replace("**", ""), _scope(rx, m), named)
    return [{"kind": k, "excerpt": found[k][0], "scope": found[k][1], "commands": found[k][2]}
            for k in (TESTS, BUILD) if k in found]


# ---------------------------------------------------------------------------
# Shell commands
# ---------------------------------------------------------------------------

class Segment:
    """One simple command: its words (quotes removed), the operator after it
    ("&&", "||", "|", "|&", ";", "&", "\\n", or "" at the end), its file
    redirects [(op, target)], and the body of a heredoc it reads."""

    __slots__ = ("words", "op", "redirects", "heredoc")

    def __init__(self):
        self.words, self.op, self.redirects, self.heredoc = [], "", [], ""

    def __repr__(self):
        return "Segment(%r, op=%r, redirects=%r)" % (self.words, self.op, self.redirects)


class _Shell:
    """A small POSIX-style tokenizer: enough to split a command into segments
    without being fooled by quotes, heredocs, comments, or redirects."""

    def __init__(self, text):
        self.t, self.i, self.segs = text, 0, []
        self.cur = Segment()
        self.word, self.in_word, self.quoted = [], False, False
        self.redirect = None
        self.pending = []  # heredocs waiting for the end of the line: (delimiter, strip tabs, segment)

    def parse(self) -> list:
        t = self.t
        while self.i < len(t):
            c = t[self.i]
            if c in " \t":
                self.end_word()
                self.i += 1
            elif c == "\n":
                self.end_word()
                self.end_segment("\n")
                self.i += 1
                self.read_heredocs()
            elif c == "#" and not self.in_word:
                end = t.find("\n", self.i)
                self.i = len(t) if end < 0 else end
            elif c == "\\":
                if t[self.i + 1:self.i + 2] == "\n":
                    self.i += 2
                else:
                    self.add(t[self.i + 1:self.i + 2])
                    self.i += 2
            elif c == "'":
                end = t.find("'", self.i + 1)
                end = len(t) if end < 0 else end
                self.add(t[self.i + 1:end], quoted=True)
                self.i = end + 1
            elif c == '"':
                self.read_double()
            elif c == "$" and t[self.i + 1:self.i + 2] == "'":
                self.read_ansi()
            elif c == "$" and t[self.i + 1:self.i + 2] in ("(", "{"):
                self.add(t[self.i])
                self.i += 1
                self.read_group(t[self.i], ")" if t[self.i] == "(" else "}")
            elif c == "`":
                end = t.find("`", self.i + 1)
                end = len(t) if end < 0 else end
                self.add(t[self.i:end + 1])
                self.i = end + 1
            elif c in "<>" and t[self.i + 1:self.i + 2] == "(":
                self.add(c)
                self.i += 1
                self.read_group("(", ")")
            elif c in "<>" or (c == "&" and t[self.i + 1:self.i + 2] == ">"):
                self.read_redirect()
            elif c in ";&|":
                self.end_word()
                two = t[self.i:self.i + 2]
                if two in ("&&", "||", "|&", ";;"):
                    op, self.i = (";" if two == ";;" else two), self.i + 2
                else:
                    op, self.i = c, self.i + 1
                self.end_segment(op)
            elif c in "()":
                self.end_word()
                self.end_segment("")
                self.i += 1
            else:
                self.add(c)
                self.i += 1
        self.end_word()
        self.end_segment("")
        return self.segs

    # -- words --------------------------------------------------------------
    def add(self, chars, quoted=False) -> None:
        self.word.append(chars)
        self.in_word = True
        self.quoted = self.quoted or quoted

    def end_word(self) -> None:
        if not self.in_word:
            return
        w, quoted = "".join(self.word), self.quoted
        self.word, self.in_word, self.quoted = [], False, False
        op, self.redirect = self.redirect, None
        if op is None:
            if not (w in ("{", "}") and not quoted):
                self.cur.words.append(w)
        elif op in ("<<", "<<-"):
            self.pending.append((w, op == "<<-", self.cur))
        elif op in (">&", "<&") and re.match(r"^(?:\d+|-)$", w):
            pass  # a copied file descriptor such as 2>&1, not a file
        elif op != "<<<":
            self.cur.redirects.append((">" if op in (">&", "&>") else op, w))

    def end_segment(self, op) -> None:
        if self.cur.words or self.cur.redirects:
            self.cur.op = op
            self.segs.append(self.cur)
            self.cur = Segment()
        elif op and self.segs and self.segs[-1].op in ("", "\n"):
            self.segs[-1].op = op  # "(a && b) && c": the operator after the group

    def read_double(self) -> None:
        t, i, out = self.t, self.i + 1, []
        while i < len(t) and t[i] != '"':
            if t[i] == "\\" and i + 1 < len(t) and t[i + 1] in '"\\$`\n':
                if t[i + 1] != "\n":
                    out.append(t[i + 1])
                i += 2
            else:
                out.append(t[i])
                i += 1
        self.add("".join(out), quoted=True)
        self.i = i + 1

    def read_ansi(self) -> None:
        t, i, out = self.t, self.i + 2, []
        while i < len(t) and t[i] != "'":
            if t[i] == "\\" and i + 1 < len(t):
                out.append({"n": "\n", "t": "\t"}.get(t[i + 1], t[i + 1]))
                i += 2
            else:
                out.append(t[i])
                i += 1
        self.add("".join(out), quoted=True)
        self.i = i + 1

    def read_group(self, open_ch, close_ch) -> None:
        """$(...), ${...}, <(...): kept inside the current word as written."""
        t, i, depth, quote = self.t, self.i, 0, None
        while i < len(t):
            ch = t[i]
            if quote:
                if ch == quote:
                    quote = None
            elif ch in "'\"":
                quote = ch
            elif ch == open_ch:
                depth += 1
            elif ch == close_ch:
                depth -= 1
                if depth == 0:
                    break
            i += 1
        self.add(t[self.i:i + 1])
        self.i = i + 1

    def read_redirect(self) -> None:
        t = self.t
        if self.in_word and "".join(self.word).isdigit() and not self.quoted:
            self.word, self.in_word = [], False  # a file descriptor number such as the 2 in 2>
        else:
            self.end_word()
        for op in ("<<<", "<<-", "&>>", "<<", ">>", ">|", ">&", "<&", "&>", "<>", "<", ">"):
            if t.startswith(op, self.i):
                self.i += len(op)
                self.redirect = {">|": ">", "&>>": ">>", "<>": "<"}.get(op, op)
                return

    def read_heredocs(self) -> None:
        t = self.t
        for delim, strip_tabs, seg in self.pending:
            lines = []
            while self.i < len(t):
                end = t.find("\n", self.i)
                end = len(t) if end < 0 else end
                line = t[self.i:end]
                self.i = end + 1
                if (line.lstrip("\t") if strip_tabs else line) == delim:
                    break
                lines.append(line)
            seg.heredoc = "\n".join(filter(None, [seg.heredoc, "\n".join(lines)]))
        self.pending = []


def parse_shell(command) -> list:
    """Split a shell command into Segments, in order."""
    return _Shell(command if isinstance(command, str) else "").parse()


# -- wrappers such as `timeout 60`, `uv run`, `npx`, and `python -m` ---------

_PY = re.compile(r"^(?:python(?:\d(?:\.\d+)?)?|pypy3?|py)$")
_SHELLS = {"bash", "sh", "zsh", "dash", "ksh"}
_UV_VALUE_OPTS = {"--python", "-p", "--with", "--with-editable", "--with-requirements", "--project", "--directory",
                  "--package", "--extra", "--group", "--only-group", "--no-group", "--index", "--index-url",
                  "--extra-index-url", "--env-file", "--config-file", "--cache-dir", "--from", "--python-platform",
                  "--resolution", "--prerelease", "--exclude-newer", "--color", "--link-mode"}
_PM_VALUE_OPTS = {"--filter", "-F", "-C", "--dir", "--prefix", "--cwd", "-w", "--workspace"}
_PY_VALUE_OPTS = {"-X", "-W"}


def _base(word) -> str:
    return word.rstrip("/").rsplit("/", 1)[-1]


def _skip_opts(args, value_opts=()) -> list:
    """Drop leading options (and the values of options that take one)."""
    i = 0
    while i < len(args):
        a = args[i]
        if a == "--":
            return args[i + 1:]
        if not a.startswith("-") or a == "-":
            break
        i += 2 if (a in value_opts and "=" not in a) else 1
    return args[i:]


def _unwrap(words) -> list:
    """The real program and its arguments, with wrappers removed."""
    w = list(words)
    while w:
        while w and re.match(r"^[A-Za-z_][A-Za-z0-9_]*=", w[0]):
            w.pop(0)
        if not w:
            break
        p, rest = _base(w[0]), w[1:]
        if p in ("time", "nohup", "command", "exec", "builtin", "caffeinate", "unbuffer", "!", "do", "then", "else"):
            w = _skip_opts(rest, {"-t", "-w"})
        elif p == "env":
            rest = _skip_opts(rest, {"-u", "--unset", "-C", "--chdir"})
            while rest and re.match(r"^[A-Za-z_][A-Za-z0-9_]*=", rest[0]):
                rest = rest[1:]
            w = rest
        elif p in ("timeout", "gtimeout"):
            rest = _skip_opts(rest, {"-s", "--signal", "-k", "--kill-after"})
            w = rest[1:]
        elif p == "nice":
            w = _skip_opts(rest, {"-n", "--adjustment"})
        elif p in ("stdbuf", "sudo", "xvfb-run"):
            w = _skip_opts(rest, {"-u", "-g", "-s", "-n", "-o", "-e", "-i", "--server-args"})
        elif p in ("uv", "poetry", "pipenv", "pdm", "rye", "pixi", "hatch") and rest[:1] == ["run"]:
            w = _skip_opts(rest[1:], _UV_VALUE_OPTS)
        elif p == "uv" and rest[:2] == ["tool", "run"]:
            w = _skip_opts(rest[2:], _UV_VALUE_OPTS)
        elif p == "uvx":
            w = _skip_opts(rest, _UV_VALUE_OPTS)
        elif p == "conda" and rest[:1] == ["run"]:
            w = _skip_opts(rest[1:], {"-n", "--name", "-p", "--prefix", "--cwd"})
        elif p in ("npx", "bunx", "pnpx"):
            w = _skip_opts(rest, {"-p", "--package"})
        elif p in ("npm", "pnpm", "yarn", "bun") and _skip_opts(rest, _PM_VALUE_OPTS)[:1] in (["exec"], ["dlx"],
                                                                                                ["x"]):
            w = _skip_opts(_skip_opts(rest, _PM_VALUE_OPTS)[1:], {"-p", "--package"})
        elif p == "bundle" and rest[:1] == ["exec"]:
            w = rest[1:]
        elif p == "dotenv":
            w = _skip_opts(rest, {"-e", "-c", "-v"})
        elif _PY.match(p) and "-m" in _py_opts(rest):
            opts = _py_opts(rest)
            w = opts[opts.index("-m") + 1:]
            break
        else:
            break
    return w


def _py_opts(args) -> list:
    """Python's own options, up to and including `-m MODULE` or the script."""
    out, i = [], 0
    while i < len(args):
        a = args[i]
        out.append(a)
        if a == "-m":
            out.extend(args[i + 1:])
            return out
        if not a.startswith("-") or a in ("-", "-c"):
            return out
        if a in _PY_VALUE_OPTS and i + 1 < len(args):
            out.append(args[i + 1])
            i += 1
        i += 1
    return out


# -- which runs a command makes ----------------------------------------------

_BOTH = frozenset([TESTS, BUILD])
_T, _B, _NONE = frozenset([TESTS]), frozenset([BUILD]), frozenset()
_NOT_A_RUN = {"--version", "--help", "-h", "--collect-only", "--co", "--fixtures", "--markers", "--listTests",
              "--showConfig", "--init"}
_TYPE_WORDS = {"type", "types", "typecheck", "typings", "tsc"}
_LINT_WORDS = {"lint", "linter", "eslint", "stylelint", "markdownlint", "prettier", "format", "formatting", "fmt",
               "style", "spell", "spellcheck", "cspell", "link", "links"}
_TEST_NAME_WORDS = {"test", "tests", "spec", "specs", "unit", "unittest", "unittests", "integration", "e2e"}
_CHECK_WORDS = {"check", "ci", "verify"}
_BUILD_NAME_WORDS = {"build", "compile", "all", "release"}
_PM_COMMANDS = {"install", "i", "add", "remove", "rm", "uninstall", "up", "update", "upgrade", "why", "list", "ls",
                "outdated", "audit", "info", "view", "publish", "pack", "link", "unlink", "init", "create",
                "config", "store", "cache", "dedupe", "import", "rebuild", "prune", "patch", "env", "setup",
                "licenses", "bin", "root", "fetch", "deploy", "doctor", "workspace", "workspaces", "version",
                "login", "logout", "whoami", "ci", "set", "get", "help"}
_TEST_FILE = re.compile(r"^(?:test_.*|.*_test|tests?|run_?tests?|runtests)\.py$")
_TEST_SCRIPT = re.compile(r"^(?:tests?|run[-_]?tests?|runtests|test[-_](?:all|suite))(?:\.(?:sh|bash|zsh))?$")


def _name_words(name) -> list:
    return [w for w in re.split(r"[:_.\-/\s]+", str(name).lower()) if w]


def _target_kinds(names, bare_checks=False) -> frozenset:
    """Kinds of run for script or target names, read by their words: type
    checks are builds, lint and format scripts are neither, and check, ci,
    or verify count as tests only for make, just, and task (bare_checks)."""
    kinds = set()
    for n in names:
        words = _name_words(n)
        if not words or _LINT_WORDS & set(words) and not _TYPE_WORDS & set(words):
            continue
        if _TYPE_WORDS & set(words):
            kinds.add(BUILD)
        elif _TEST_NAME_WORDS & set(words) or (bare_checks and words[0] in _CHECK_WORDS):
            kinds.add(TESTS)
        elif words[0] in _BUILD_NAME_WORDS:
            kinds.add(BUILD)
    return frozenset(kinds)


def _ambiguous_name(name) -> bool:
    """A package script named check, ci, or verify: it may run the tests."""
    words = _name_words(name)
    return bool(words) and words[0] in _CHECK_WORDS and not (_TYPE_WORDS | _LINT_WORDS | _TEST_NAME_WORDS) & set(words)


def _args(args, value_opts=()) -> list:
    """Arguments that are not options (nor option values)."""
    out, i = [], 0
    while i < len(args):
        a = args[i]
        if a.startswith("-") and a != "-":
            i += 2 if (a in value_opts and "=" not in a) else 1
            continue
        out.append(a)
        i += 1
    return out


def _pm_scripts(p, args) -> list:
    """The package scripts a package manager command runs, by name."""
    rest = _skip_opts(args, _PM_VALUE_OPTS)
    if not rest:
        return []
    sub, tail = rest[0], rest[1:]
    if sub in ("run", "run-script", "rum", "urn"):
        return _args(tail, _PM_VALUE_OPTS)[:1]
    if p in ("pnpm", "yarn") and sub not in _PM_COMMANDS and sub not in ("test", "t", "tst"):
        return [sub]  # yarn and pnpm run package scripts by name
    return []


def _pm_kinds(p, args) -> frozenset:
    rest = _skip_opts(args, _PM_VALUE_OPTS)
    if not rest:
        return _NONE
    if rest[0] in ("test", "t", "tst") and p != "bun":
        return _T
    if p == "bun" and rest[0] in ("test", "build"):
        return _T if rest[0] == "test" else _B
    return _target_kinds(_pm_scripts(p, args))


def _task_names(args) -> list:
    """The task names a turbo, nx, or lerna command runs."""
    names = _args(args, {"-t", "--target", "--filter", "--scope", "-p", "--projects"})
    names = [n.rsplit(":", 1)[-1] for n in names if n not in ("run", "run-many", "affected", "exec")]
    return names + [args[i + 1] for i, a in enumerate(args[:-1]) if a in ("-t", "--target")]


def _ambiguous_run(words) -> bool:
    """A package or task script named check, ci, or verify, which may run the tests."""
    w = _unwrap(words)
    if not w:
        return False
    p, args = _base(w[0]), w[1:]
    names = _pm_scripts(p, args) if p in ("npm", "pnpm", "yarn", "bun") else \
        (_task_names(args) if p in ("turbo", "lerna", "nx") else [])
    return any(_ambiguous_name(n) for n in names)


def _segment_kinds(words) -> frozenset:
    w = _unwrap(words)
    if not w:
        return _NONE
    p, args = _base(w[0]), w[1:]
    if _NOT_A_RUN.intersection(args):
        return _NONE
    first = _args(args)[:1]
    sub = first[0] if first else ""
    if p in ("pytest", "py.test", "pytest-3", "unittest", "nose2", "nosetests", "tox", "nox", "ctest", "rspec",
             "phpunit", "pest", "paratest", "mocha", "ava", "jasmine", "tap"):
        return _T
    if p in ("jest", "vitest"):
        return _NONE if sub in ("list", "init", "bench") else _T
    if p in ("tsc", "vue-tsc", "tsgo", "mypy", "pyright", "basedpyright", "pytype", "svelte-check", "webpack",
             "rollup", "esbuild", "tsup", "ninja", "py_compile", "compileall"):
        return _T if (p == "ninja" and sub == "test") else _B
    if p in ("npm", "pnpm", "yarn", "bun"):
        return _pm_kinds(p, args)
    if _PY.match(p):
        return _T if first and (_TEST_FILE.match(_base(first[0])) or
                                (_base(first[0]) in ("manage.py", "setup.py") and "test" in args)) else _NONE
    if p in _SHELLS and args and not args[0].startswith("-"):
        return _T if _TEST_SCRIPT.match(_base(args[0])) else _NONE
    if ("/" in w[0] or w[0].endswith((".sh", ".bash"))) and _TEST_SCRIPT.match(p):
        return _T  # a project test script: scripts/test, ./run_tests.sh, bin/test
    table = {
        "go": {"test": _BOTH, "build": _B, "vet": _B},
        "cargo": {"test": _BOTH, "t": _BOTH, "nextest": _BOTH, "build": _B, "b": _B, "check": _B, "c": _B},
        "dotnet": {"test": _BOTH, "build": _B, "publish": _B, "msbuild": _B},
        "swift": {"test": _BOTH, "build": _B},
        "bazel": {"test": _BOTH, "build": _B}, "bazelisk": {"test": _BOTH, "build": _B},
        "mix": {"test": _BOTH, "compile": _B}, "stack": {"test": _BOTH, "build": _B},
        "cabal": {"test": _BOTH, "build": _B}, "flutter": {"test": _T, "build": _B}, "dart": {"test": _T},
        "deno": {"test": _T, "check": _B}, "playwright": {"test": _T}, "cypress": {"run": _T},
        "rake": {"test": _T, "spec": _T}, "rails": {"test": _T}, "composer": {"test": _T},
        "meson": {"test": _T, "compile": _B}, "lein": {"test": _T}, "just": {}, "task": {},
        "next": {"build": _B}, "nuxt": {"build": _B}, "astro": {"build": _B}, "ng": {"build": _B, "test": _T},
        "vite": {"build": _B}, "parcel": {"build": _B}, "gatsby": {"build": _B},
    }
    if p in table:
        kinds = table[p].get(sub)
        if kinds is None and p in ("just", "task"):
            kinds = _target_kinds([sub], bare_checks=True) if sub else _NONE
        if p == "cargo" and kinds == _BOTH and "--no-run" in args:
            return _B
        return kinds or _NONE
    if p == "node":
        return _T if "--test" in args else _NONE
    if p == "php":
        return _T if args[:2] == ["artisan", "test"] else _NONE
    if p in ("mvn", "mvnw"):
        goals = set(_args(args, {"-f", "-pl", "-P", "-s"}))
        skip = any(a.startswith(("-DskipTests", "-Dmaven.test.skip=true")) for a in args)
        if "test" in goals or (goals & {"verify", "integration-test", "install", "package", "deploy"} and not skip):
            return _BOTH
        return _B if goals & {"compile", "test-compile", "package", "install", "verify", "deploy"} else _NONE
    if p in ("gradle", "gradlew"):
        excluded = {args[i + 1] for i, a in enumerate(args[:-1]) if a in ("-x", "--exclude-task")}
        tasks = [t.rsplit(":", 1)[-1] for t in _args(args, {"-x", "--exclude-task", "-p", "-b", "-c"})]
        tasks = [t for t in tasks if t not in excluded]
        if any(t in ("test", "check", "build", "connectedCheck") or "test" in t.lower() for t in tasks) \
                and "test" not in excluded:
            return _BOTH
        return _B if any(t in ("assemble", "build", "compileJava", "compileKotlin", "classes", "jar")
                         for t in tasks) else _NONE
    if p == "xcodebuild":
        if "test" in args:
            return _BOTH
        return _B if ("build" in args or "archive" in args) else _NONE
    if p in ("make", "gmake"):
        targets = [a for a in _args(args, {"-C", "-f", "--directory", "--file", "-j", "--jobs", "-l"})
                   if "=" not in a]
        if not targets:
            return _B
        kinds = _target_kinds(targets, bare_checks=True)
        return _T if TESTS in kinds else kinds
    if p == "cmake":
        return _B if "--build" in args else _NONE
    if p in ("turbo", "lerna", "nx"):
        return _target_kinds(_task_names(args))
    if p == "zig" and sub == "build":
        return _BOTH if "test" in args else _B
    if p == "sbt":
        return _BOTH if "test" in args else (_B if "compile" in args else _NONE)
    return _NONE


# -- which files a command changes ---------------------------------------------

_FORMATTERS = {
    # program: (flags that make it write, flags that keep it read-only); None means it writes by default
    "black": (None, {"--check", "--diff"}), "isort": (None, {"--check", "--check-only", "--diff"}),
    "autopep8": ({"-i", "--in-place"}, set()), "yapf": ({"-i", "--in-place"}, set()),
    "prettier": ({"--write", "-w"}, set()), "eslint": ({"--fix"}, set()),
    "gofmt": ({"-w"}, set()), "goimports": ({"-w"}, set()), "rustfmt": (None, {"--check"}),
    "clang-format": ({"-i"}, set()), "swiftformat": (None, {"--lint"}),
}
_WRITES = re.compile(
    r"(?<!stdout)(?<!stderr)\.write(?:_text|_bytes|lines)?\(|"
    r"\bopen\([^)\n]*,\s*(?:mode\s*=\s*)?['\"][rb+]*[wxa][rwxab+]*['\"]|"
    r"\b(?:writeFile|writeFileSync|appendFile|appendFileSync|renameSync|unlinkSync|rmSync|copyFileSync)\(|"
    r"\bos\.(?:replace|rename|remove|unlink)\(|\bshutil\.(?:move|copy\w*|rmtree)\(|\.unlink\(|\.rename\(|"
    r"\b(?:File|IO|Files)\.write")
_LITERAL = re.compile(r"(['\"])((?:(?!\1)[^\n\\]){2,300})\1")
_INLINE_FLAGS = {"-c", "-e", "--eval", "-r", "-p"}


_FILE_EXT = re.compile(r"\.(?:py|pyi|js|jsx|ts|tsx|mjs|cjs|mts|cts|json|jsonl|ya?ml|toml|ini|cfg|conf|md|mdx|rst|"
                       r"txt|csv|tsv|html?|css|scss|sass|less|xml|svg|sh|bash|zsh|go|rs|rb|java|kts?|swift|c|h|cc|"
                       r"cpp|hpp|mm?|cs|php|sql|lock|env|vue|svelte|dart|exs?|lua|scala|gradle|properties|plist|"
                       r"ipynb|proto|graphql|tf|mk)$", re.I)
_DOMAIN = re.compile(r"^[\w-]+(?:\.[\w-]+)*\.(?:com|org|net|io|dev|ai|app|co|gov|edu|sh)(?:/|$)", re.I)


_PATH_SHAPE = re.compile(r"^(?:[\w.~$@+%-]|(?<=\w) (?=\w))+$")  # letters, digits, ./~$@+%- and inner spaces


def _code_paths(code, args=()) -> Optional[list]:
    """Files an inline script writes: the path-like string literals in it (a
    shell variable such as '$M' counts; the caller fills it in), or else the
    path-like arguments the script was given, or None when neither names one."""
    paths = []
    for _q, lit in _LITERAL.findall(code):
        if "://" in lit or lit.startswith("-") or _DOMAIN.match(lit) or not _PATH_SHAPE.match(lit.replace("/", "")):
            continue
        if "/" in lit or _FILE_EXT.search(lit) or re.match(r"^\$\{?\w+\}?$", lit):
            if lit not in paths:
                paths.append(lit)
    if not paths:
        paths = [a for a in args if not a.startswith("-") and ("/" in a or "$" in a or _FILE_EXT.search(a))]
    return paths or None


def _git_edit(args):
    """False when this git command leaves the working tree alone, else the paths
    it names (None when it may change any file)."""
    args = _skip_opts(args, {"-C", "-c", "--git-dir", "--work-tree"})
    if not args:
        return False
    sub, rest = args[0], args[1:]
    named = rest[rest.index("--") + 1:] if "--" in rest else None
    if sub == "checkout":
        if named:
            return named
        targets = _args(rest, {"-b", "-B", "--orphan"})
        if "." in targets:
            return None
        paths = [t for t in targets if re.search(r"\.\w{1,8}$", t)]
        return paths or (None if targets else False)  # a switch to another branch or commit changes any file
    if sub == "switch":
        return None if _args(rest, {"-c", "-C", "--create", "--force-create", "--orphan"}) else False
    if sub == "restore":
        if ({"--staged", "-S"} & set(rest)) and not ({"--worktree", "-W"} & set(rest)):
            return False
        return named or _args(rest, {"-s", "--source"}) or None
    if sub == "reset":
        return None if ({"--hard", "--merge", "--keep"} & set(rest)) else False
    if sub in ("apply", "am"):
        return False if ({"--check", "--stat", "--numstat", "--summary"} & set(rest)) else None
    if sub in ("merge", "rebase", "pull", "cherry-pick", "revert"):
        return None
    if sub == "clean":
        return False if ({"-n", "--dry-run"} & set(rest)) else None
    if sub == "mv":
        return _args(rest) or None
    if sub == "rm":
        return False if "--cached" in rest else (_args(rest) or None)
    return False


def _segment_edits(seg):
    """False when the segment changes no file, else a list of paths (or None
    when the files are unknown)."""
    paths = [target for op, target in seg.redirects if op in (">", ">>") and not target.startswith("/dev/")]
    w = _unwrap(seg.words)
    found = _program_edits(w, seg) if w else False
    if found is None:
        return None
    if found:
        paths.extend(p for p in found if p not in paths)
    return paths or False


def _program_edits(w, seg):
    p, args = _base(w[0]), w[1:]
    if p in ("sed", "gsed"):
        if not any(a == "-i" or a.startswith(("-i", "--in-place")) for a in args):
            return False
        scripts, files, i = [], [], 0
        while i < len(args):
            a = args[i]
            if a == "-i" and i + 1 < len(args) and (args[i + 1] == "" or args[i + 1].startswith(".")):
                i += 2
                continue
            if a in ("-e", "--expression", "-f", "--file"):
                scripts.append(args[i + 1] if i + 1 < len(args) else "")
                i += 2
                continue
            if not a.startswith("-"):
                files.append(a)
            i += 1
        return (files if scripts else files[1:]) or None
    if p == "perl" and any(re.match(r"^-[A-Za-z]*i", a) for a in args):
        return _args(args, {"-e", "-E", "-M", "-I"}) or None
    if p == "tee":
        return [a for a in _args(args) if not a.startswith("/dev/")] or False
    if p in _FORMATTERS:
        write_flags, read_flags = _FORMATTERS[p]
        if read_flags & set(args):
            return False
        return None if (write_flags is None or write_flags & set(args)) else False
    if p == "ruff":
        sub = _args(args)[:1]
        if sub == ["format"]:
            return False if ({"--check", "--diff"} & set(args)) else None
        return None if (sub == ["check"] and "--fix" in args) else False
    if p == "biome":
        return None if ({"--write", "--apply", "--fix", "--apply-unsafe"} & set(args)) else False
    if p == "cargo" and args[:1] == ["fmt"]:
        return False if "--check" in args else None
    if p == "dotnet" and args[:1] == ["format"]:
        return False if "--verify-no-changes" in args else None
    if p == "git":
        return _git_edit(args)
    if p == "patch":
        return False if "--dry-run" in args else None
    if p in ("mv", "cp"):
        rest = _args(args, {"-t", "--target-directory"})
        if len(rest) < 2:
            return False
        sources, dest = rest[:-1], rest[-1]
        if dest in (".", "..") or dest.endswith("/") or len(sources) > 1:  # into a folder
            targets = [dest.rstrip("/") + "/" + _base(src) if dest not in (".", "..") else dest + "/" + _base(src)
                       for src in sources]
        else:
            targets = [dest]
        return (sources + targets) if p == "mv" else targets
    if p in ("rm", "unlink", "rmdir"):
        return _args(args) or False
    if _PY.match(p) or p in ("node", "ruby", "perl", "php", "bun", "deno"):
        code, rest = "", []
        if not args or args[0] == "-" or (_PY.match(p) and not _args(args)):
            code, rest = seg.heredoc, args[1:] if args[:1] == ["-"] else []
        for i, a in enumerate(args[:-1]):
            if a in _INLINE_FLAGS or (p == "deno" and a == "eval") or (p == "bun" and a == "eval"):
                code, rest = args[i + 1], args[i + 2:]
                break
        if code and _WRITES.search(code):
            return _code_paths(code, rest)
        return False
    return False


def _expand(segs, depth=0):
    """Segments with `bash -c "..."` replaced by the segments of its script."""
    out = []
    for seg in segs:
        w = _unwrap(seg.words)
        if depth < 3 and w and _base(w[0]) in _SHELLS:
            flags = [a for a in w[1:] if a.startswith("-")]
            if any("c" in f.lstrip("-") for f in flags):
                script = next((a for a in w[1:] if not a.startswith("-")), "")
                inner = _expand(parse_shell(script), depth + 1)
                if inner:
                    inner[-1].op = seg.op
                    out.extend(inner)
                    continue
        out.append(seg)
    return out


_ASSIGN = re.compile(r"^([A-Za-z_][A-Za-z0-9_]*)=(.*)$", re.S)


def _stash_action(words) -> Optional[str]:
    """"push" for `git stash` (and push, save), "pop" for stash pop and apply, else None."""
    w = _unwrap(words)
    if not w or _base(w[0]) != "git":
        return None
    args = _skip_opts(w[1:], {"-C", "-c", "--git-dir", "--work-tree"})
    if args[:1] != ["stash"]:
        return None
    sub = args[1] if len(args) > 1 else ""
    if sub in ("pop", "apply"):
        return "pop"
    return "push" if sub in ("", "push", "save") or sub.startswith("-") else None


def _expand_vars(text, names, home) -> str:
    def value(m):
        name = m.group(1) or m.group(2)
        if name == "HOME" and home:
            return home
        return names.get(name, m.group(0))
    text = re.sub(r"\$\{(\w+)\}|\$(\w+)", value, text)
    if home and (text == "~" or text.startswith("~/")):
        text = home + text[1:]
    return text


def _resolve(path, cur, names, home) -> str:
    """A path as the shell would see it: variables set earlier in the command
    and ~ filled in, and a relative path joined to the current folder."""
    p = _expand_vars(path, names, home)
    if "$" in p or p.startswith("~"):
        return p
    if p.startswith("/"):
        return os.path.normpath(p)
    return os.path.normpath(os.path.join(cur, p)) if cur else p


def _walk_shell(command, cwd=None, home=None, stashed=False):
    """Yield (step, value, index, folder) for each run and file change, where
    folder is the working folder of that segment when it is known. Steps:
    "run" (value: kinds), "edit" (paths or None), and for `git stash`:
    ("stash", "push"), ("stash", "restore") for a pop of that stash, and
    ("stash", "pop") for a pop of a stash made elsewhere, which changes files.
    A run made while a stash is in effect that this command pops again is a
    "detour": it tested another tree (`stashed` says a stash is in effect
    when the command starts). The last step is ("end", None) with the folder
    the command ended in."""
    cur, names = cwd, {}
    segs = _expand(parse_shell(command))
    actions = [_stash_action(seg.words) for seg in segs]
    for i, seg in enumerate(segs):
        words = seg.words
        if actions[i]:
            yield "stash", ("push" if actions[i] == "push" else ("restore" if stashed else "pop")), i, cur
            stashed = actions[i] == "push"
            continue
        if words and all(_ASSIGN.match(w) for w in words):
            names.update(_ASSIGN.match(w).groups() for w in words)
            continue
        if words[:1] == ["export"]:
            names.update(_ASSIGN.match(w).groups() for w in words[1:] if _ASSIGN.match(w))
            continue
        if words[:1] in (["cd"], ["pushd"]):
            target = words[1] if len(words) > 1 else (home or "~")
            new = _resolve(target, cur, names, home)
            cur = new if new.startswith("/") and "$" not in new else None
            continue
        detour = stashed and "pop" in actions[i + 1:]
        kinds = _segment_kinds(words)
        if kinds:
            yield ("detour" if detour else "run"), kinds, i, cur
        if detour and _unwrap(words)[:1] == ["git"]:
            continue  # a checkout to compare with another commit, undone before the stash comes back
        edits = _segment_edits(seg)
        if edits is not False:
            where = cur
            unwrapped = _unwrap(words)
            if unwrapped[:1] == ["git"] and "-C" in unwrapped[1:-1]:
                target = _resolve(unwrapped[unwrapped.index("-C") + 1], cur, names, home)
                where = target if target.startswith("/") and "$" not in target else None
            if edits is None:  # the files are not named: they are somewhere in the folder it ran in
                yield "edit", ([where] if where else None), i, cur
            else:
                yield "edit", [_resolve(p, where if unwrapped[:1] == ["git"] else cur, names, home)
                               for p in edits], i, cur
    yield "end", None, len(segs), cur  # the folder the command ended in


def shell_steps(command, cwd=None, home=None) -> list:
    """What a shell command does, in order: ("run", kinds, index) for each test
    or build run and ("edit", paths or None, index) for each file change,
    where index is the segment number. With cwd (and home), changed paths are
    resolved the way the shell would: after `cd`, with variables set earlier
    in the command filled in."""
    out = []
    for step, value, i, folder in _walk_shell(command, cwd, home):
        if step in ("run", "detour"):
            out.append(("run", value, i))
        elif step == "edit":
            out.append((step, value, i))
        elif value == "pop":
            out.append(("edit", [folder] if folder else None, i))
    return out


def command_kinds(command) -> frozenset:
    """Every kind of run (tests, build) a shell command makes."""
    kinds = set()
    for step, value, _i in shell_steps(command):
        if step == "run":
            kinds |= value
    return frozenset(kinds)


# ---------------------------------------------------------------------------
# How a run ended
# ---------------------------------------------------------------------------

def _counts(text) -> dict:
    out = {}
    for n, word in re.findall(r"(\d+) ([a-z]+)", text.lower()):
        word = {"error": "errors", "failure": "failures", "warning": "warnings"}.get(word, word)
        out[word] = out.get(word, 0) + int(n)
    return out


def _pytest_line(m):
    c = _counts(m.group(1))
    detail = m.group(1).strip(" ,")
    detail = re.sub(r",?\s*\d+ warnings?", "", detail).strip(" ,") or detail
    if c.get("failed") or c.get("errors"):
        return "fail", detail
    return ("pass", detail) if c.get("passed") else ("fail", "no tests passed")


def _count_line(fail_words, pass_words):
    def read(m):
        c = _counts(m.group(1) if m.groups() else m.group(0))
        detail = " ".join(m.group(0).split())
        if any(c.get(w) for w in fail_words):
            return "fail", detail
        return ("pass", detail) if any(c.get(w) for w in pass_words) else None
    return read


def _fixed(result, detail=None):
    return lambda m: (result, detail or " ".join(m.group(0).split()))


def _nonzero(result_if_nonzero, group=1, else_result=None):
    def read(m):
        detail = " ".join(m.group(0).split())
        if int(m.group(group)) > 0:
            return result_if_nonzero, detail
        return (else_result, detail) if else_result else None
    return read


def _unittest(m):
    ran, verdict = int(m.group(1)), m.group(2)
    if verdict == "OK" and ran > 0:
        return "pass", "Ran %d tests: OK" % ran
    return "fail", ("no tests ran" if ran == 0 or verdict == "NO TESTS RAN" else "Ran %d tests: FAILED" % ran)


def _ratio(fail_group, total_group):
    def read(m):
        detail = " ".join(m.group(0).split())
        if int(m.group(fail_group)) > 0:
            return "fail", detail
        if int(m.group(total_group)) > 0:
            return "pass", detail
        return "fail", detail
    return read


_TK, _BK, _AK = frozenset([TESTS]), frozenset([BUILD]), _BOTH
# family -> [(regex, reader, kinds the result speaks for)]
_SUMMARIES = {
    "pytest": [
        (re.compile(r"(?m)^[=\s]*((?:\d+ (?:failed|passed|errors?|skipped|xfailed|xpassed|deselected|warnings?|"
                    r"rerun)(?:,\s*|\s))+)in [\d.]+s\b"), _pytest_line, _TK),
        (re.compile(r"(?m)^[=\s]*no tests ran\b"), _fixed("fail", "no tests ran"), _TK),
    ],
    "unittest": [(re.compile(r"(?m)^Ran (\d+) tests? in [\d.]+s\s*\n+\s*(OK|FAILED|NO TESTS RAN)"), _unittest, _TK)],
    "jest": [(re.compile(r"(?m)^Tests:\s+(.*\d+ total)"), _count_line(("failed",), ("passed",)), _TK),
             (re.compile(r"(?m)^Test Suites:\s+(.*\d+ total)"), _count_line(("failed",), ("passed",)), _TK)],
    "vitest": [(re.compile(r"(?m)^\s*(?:Tests|Test Files)\s+((?:\d+ [a-z]+(?:\s*\|\s*)?)+)\s*\(\d+\)"),
                _count_line(("failed",), ("passed",)), _TK)],
    "mocha": [(re.compile(r"(?m)^\s*(\d+) failing\b"), _nonzero("fail"), _TK),
              (re.compile(r"(?m)^\s*(\d+) passing\b"), _nonzero("pass"), _TK)],
    "bun": [(re.compile(r"(?m)^\s*(\d+) fail\s*$"), _nonzero("fail"), _TK),
            (re.compile(r"(?m)^\s*(\d+) pass\s*$"), _nonzero("pass"), _TK)],
    "node": [(re.compile("(?m)^[#ℹ]\\s*fail (\\d+)"), _nonzero("fail"), _TK),
             (re.compile("(?m)^[#ℹ]\\s*pass (\\d+)"), _nonzero("pass"), _TK)],
    "deno": [(re.compile(r"(?m)^(?:ok|FAILED) \| \d+ passed.*?\| (\d+) failed"), _nonzero("fail", 1, "pass"), _TK)],
    "playwright": [(re.compile(r"(?m)^\s*(\d+) failed\b"), _nonzero("fail"), _TK),
                   (re.compile(r"(?m)^\s*(\d+) passed \("), _nonzero("pass"), _TK)],
    "go": [(re.compile(r"(?m)^(?:FAIL\b|--- FAIL:).*"), _fixed("fail"), _TK),
           (re.compile(r"(?m)^\S+\.go:\d+:\d+: .*|\[build failed\]"), _fixed("fail"), _AK),
           (re.compile(r"(?m)^(?:ok\s+\S+|PASS\s*$)"), _fixed("pass"), _AK)],
    "cargo": [(re.compile(r"test result: (?:ok|FAILED)\. \d+ passed; (\d+) failed"), _nonzero("fail", 1, "pass"),
               _TK),
              (re.compile(r"test result: ok\."), _fixed("pass", "test result: ok"), _BK),
              (re.compile(r"(?m)^error(?:\[E\d+\])?: .*"), _fixed("fail"), _AK),
              (re.compile(r"(?m)^\s*Finished\b.*\btarget\(s\) in .*"), _fixed("pass", "Finished"), _BK)],
    "rspec": [(re.compile(r"(\d+) examples?, (\d+) failures?"), _ratio(2, 1), _TK)],
    "minitest": [(re.compile(r"(\d+) runs, \d+ assertions, (\d+) failures, (\d+) errors"),
                  lambda m: ("fail" if int(m.group(2)) + int(m.group(3)) else
                             ("pass" if int(m.group(1)) else "fail"), " ".join(m.group(0).split())), _TK)],
    "phpunit": [(re.compile(r"(?m)^OK(?:, but [^\n]*)?\s*\((\d+) tests?, \d+ assertions?\)"), _fixed("pass"), _TK),
                (re.compile(r"(?m)^(?:FAILURES|ERRORS)!\s*$"), _fixed("fail"), _TK),
                (re.compile(r"(?m)^Tests: \d+, Assertions: \d+, (?:Errors|Failures): \d+.*"), _fixed("fail"), _TK)],
    "maven": [(re.compile(r"BUILD SUCCESS\b"), _fixed("pass"), _AK),
              (re.compile(r"BUILD FAILURE\b"), _fixed("fail"), _TK),
              (re.compile(r"Tests run: \d+, Failures: (\d+), Errors: (\d+)"),
               lambda m: ("fail", " ".join(m.group(0).split())) if int(m.group(1)) + int(m.group(2)) else None, _TK),
              (re.compile(r"COMPILATION ERROR"), _fixed("fail"), _AK)],
    "gradle": [(re.compile(r"BUILD SUCCESSFUL\b"), _fixed("pass"), _AK),
               (re.compile(r"BUILD FAILED\b"), _fixed("fail"), _TK),
               (re.compile(r"Compilation failed|compileJava FAILED|compileKotlin FAILED"), _fixed("fail"), _AK)],
    "dotnet": [(re.compile(r"(?m)^\s*(?:Passed|Failed)!\s+-\s+Failed:\s+(\d+), Passed:\s+(\d+)"),
                _nonzero("fail", 1, "pass"), _TK),
               (re.compile(r"Test Run Successful\."), _fixed("pass"), _TK),
               (re.compile(r"Test Run Failed\."), _fixed("fail"), _TK),
               (re.compile(r"Build succeeded\."), _fixed("pass"), _BK),
               (re.compile(r"Build FAILED\."), _fixed("fail"), _AK)],
    "swift": [(re.compile(r"Executed (\d+) tests?, with (\d+) failures?"), _ratio(2, 1), _TK),
              (re.compile(r"Test run with \d+ tests?(?: in \d+ suites?)? passed"), _fixed("pass"), _TK),
              (re.compile(r"Test run with \d+ tests?(?: in \d+ suites?)? failed"), _fixed("fail"), _TK),
              (re.compile(r"Build complete!"), _fixed("pass"), _BK),
              (re.compile(r"(?m)^error: .*"), _fixed("fail"), _AK)],
    "xcode": [(re.compile(r"\*\* TEST SUCCEEDED \*\*"), _fixed("pass"), _AK),
              (re.compile(r"\*\* TEST FAILED \*\*"), _fixed("fail"), _TK),
              (re.compile(r"\*\* BUILD SUCCEEDED \*\*"), _fixed("pass"), _BK),
              (re.compile(r"\*\* BUILD FAILED \*\*"), _fixed("fail"), _AK)],
    "ctest": [(re.compile(r"\d+% tests passed, (\d+) tests? failed out of (\d+)"), _ratio(1, 2), _TK)],
    "tox": [(re.compile(r"(?m)^\s*congratulations :\)"), _fixed("pass", "congratulations"), _TK),
            (re.compile(r"(?m)^\s*(?:ERROR:\s+)?[\w.-]+: (?:FAIL code \d+|commands failed).*|evaluation failed :\("),
             _fixed("fail"), _TK),
            (re.compile(r"(?m)^\s*[\w.-]+: (?:OK \(|commands succeeded)"), _fixed("pass"), _TK)],
    "tsc": [(re.compile(r"error TS\d+:.*"), _fixed("fail"), _BK),
            (re.compile(r"(?m)^Found (\d+) errors?"), _nonzero("fail"), _BK)],
    "mypy": [(re.compile(r"(?m)^Success: no issues found.*"), _fixed("pass"), _BK),
             (re.compile(r"(?m)^Found \d+ errors? in \d+ files?.*"), _fixed("fail"), _BK)],
    "pyright": [(re.compile(r"(?m)^\s*(\d+) errors?, \d+ warnings?"), _nonzero("fail", 1, "pass"), _BK)],
    "bundler": [(re.compile(r"[Cc]ompiled successfully|\u2713 built in|\bbuilt in [\d.]+m?s|"
                            r"prerendered as static content"), _fixed("pass"), _BK),
                (re.compile(r"Failed to compile|Build error occurred|error during build|compiled with \d+ errors?|"
                            r"^ERROR in .*", re.M), _fixed("fail"), _BK)],
    "pm": [(re.compile(r"^npm (?:ERR!|error) .*|\bELIFECYCLE\b|ERR_PNPM_\w+|"
                       r"^error Command failed with exit code \d+.*|^error: script \"[^\"]+\" exited .*", re.M),
            _fixed("fail"), _AK)],
    "make": [(re.compile(r"(?m)^g?make(?:\[\d+\])?: \*\*\* .*Error \d+.*"), _fixed("fail"), _AK)],
}
_JS = {"jest", "vitest", "mocha", "bun", "node", "playwright", "pm", "bundler", "tsc"}
_ANY = set(_SUMMARIES)
_FAMILIES = {
    "pytest": {"pytest"}, "py.test": {"pytest"}, "pytest-3": {"pytest"}, "unittest": {"unittest", "pytest"},
    "nose2": {"unittest"}, "nosetests": {"unittest"}, "tox": {"tox", "pytest", "unittest"},
    "nox": {"tox", "pytest", "unittest"}, "jest": {"jest"}, "vitest": {"vitest"}, "mocha": {"mocha"},
    "ava": {"mocha", "node"}, "tap": {"node"}, "jasmine": {"mocha"}, "playwright": {"playwright"},
    "cypress": {"mocha"}, "node": {"node"}, "deno": {"deno"}, "go": {"go"}, "cargo": {"cargo"},
    "rspec": {"rspec"}, "rake": {"rspec", "minitest"}, "rails": {"minitest"}, "phpunit": {"phpunit"},
    "pest": {"phpunit"}, "paratest": {"phpunit"}, "php": {"phpunit"}, "composer": {"phpunit"},
    "mvn": {"maven"}, "mvnw": {"maven"}, "gradle": {"gradle"}, "gradlew": {"gradle"}, "dotnet": {"dotnet"},
    "swift": {"swift"}, "xcodebuild": {"xcode"}, "ctest": {"ctest"}, "tsc": {"tsc"}, "vue-tsc": {"tsc"},
    "tsgo": {"tsc"}, "mypy": {"mypy"}, "pyright": {"pyright"}, "basedpyright": {"pyright"},
    "npm": _JS | {"pytest"}, "pnpm": _JS | {"pytest"}, "yarn": _JS | {"pytest"}, "bun": _JS,
    "next": {"bundler"}, "nuxt": {"bundler"}, "vite": {"bundler"}, "webpack": {"bundler"}, "astro": {"bundler"},
    "ng": {"bundler", "jest"}, "rollup": {"bundler"}, "esbuild": {"bundler"}, "tsup": {"bundler"},
    "parcel": {"bundler"}, "gatsby": {"bundler"}, "svelte-check": {"tsc"},
}


def _is_silent(words, output="") -> bool:
    """Checkers that print nothing when all is well. A package script counts
    when npm's header line shows it ran one ("> tsc --noEmit")."""
    w = _unwrap(words)
    p = _base(w[0]) if w else ""
    if p in ("npm", "pnpm", "yarn", "bun"):
        return bool(re.search(r"(?m)^> (?:tsc|vue-tsc|tsgo)\b", output))
    return p in ("tsc", "vue-tsc", "tsgo") or (p == "go" and w[1:2] in (["build"], ["vet"]))

_KEEPS_LINES = {"tail", "head"}
_NEUTRAL = {"tail", "head", "cat", "tee", "sed", "awk", "sort", "uniq", "wc", "cut", "tr", "echo", "printf", "true",
            ":"}
_TIMED_OUT = re.compile(r"timed out|timeout|Terminated|Killed", re.I)
_ANSI = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]|\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)")
_NOT_FOUND = re.compile(r"command not found: ([\w./-]+)|([\w./-]+): (?:command )?not found\s*$|"
                        r"No module named '?([\w.]+)", re.M)


def _families(words) -> set:
    w = _unwrap(words)
    if not w:
        return set()
    p, args = _base(w[0]), w[1:]
    if _PY.match(p):
        first = _args(args)[:1]
        return {"unittest", "pytest"} if first else set()
    if p in ("go",) and args[:1] in (["build"], ["vet"]):
        return {"go"}
    return _FAMILIES.get(p, _ANY if p in ("make", "gmake", "just", "task", "turbo", "nx", "lerna", "bazel",
                                          "bazelisk", "meson", "mix", "sbt", "zig", "stack", "cabal",
                                          "flutter", "dart", "lein", "ninja", "cmake") else set())


def _summaries(output, families, kind, pure=False) -> list:
    """[(position, result, detail)] from the runner summaries in the output.
    With pure, only lines that speak for that kind alone (a test summary, not
    a compile error that would fail a build too)."""
    found = []
    for fam in families:
        for rx, read, kinds in _SUMMARIES.get(fam, ()):
            if kind not in kinds or (pure and kinds != frozenset([kind])):
                continue
            for m in rx.finditer(output):
                got = read(m)
                if got:
                    found.append((m.start(), got[0], got[1]))
    found.sort()
    return found


def _marker(segs, r, output) -> Optional[str]:
    """"pass" or "fail" from `runner && echo OK || echo FAIL` when the echoed text was printed."""
    lines = {line.strip() for line in output.splitlines()}
    seg, j = segs[r], r + 1
    op = seg.op
    while op in ("&&", "||") and j < len(segs):
        w = _unwrap(segs[j].words)
        if not w or _base(w[0]) not in ("echo", "printf"):
            return None
        text = " ".join(a for a in w[1:] if not a.startswith("-")).strip()
        if text and text in lines:
            return "pass" if op == "&&" else "fail"
        op = segs[j].op
        j += 1
    return None


def _echoed_exit(segs, r, output) -> Optional[int]:
    """The exit code from `runner; echo "exit=$?"` when that line was printed."""
    if segs[r].op not in (";", "\n") or r + 1 >= len(segs):
        return None
    w = _unwrap(segs[r + 1].words)
    if not w or _base(w[0]) not in ("echo", "printf"):
        return None
    text = " ".join(a for a in w[1:] if not a.startswith("-"))
    if "$?" not in text:
        return None
    rx = re.compile("^" + re.escape(text.strip()).replace(re.escape("$?"), r"(-?\d+)", 1) + "$", re.M)
    m = rx.search(output)
    return int(m.group(1)) if m else None


def _exit_verdict(segs, r, exit_code, pipefail, kind) -> Optional[str]:
    """What the exit code says about run r, when it belongs to that run."""
    if exit_code is None:
        return None
    later = segs[r + 1:]
    links = [s.op for s in segs[r:-1]]
    if segs[-1].op == "&" or not all(op == "&&" or (pipefail and op in ("|", "|&")) for op in links):
        return None
    if exit_code == 0:
        return "pass"
    neutral = all((_unwrap(s.words) or [""])[0] and _base(_unwrap(s.words)[0]) in _NEUTRAL for s in later)
    if not neutral:
        return None
    return "fail" if (kind == TESTS or _segment_kinds(segs[r].words) == _B) else None


def _pipe_keeps_lines(segs, r) -> bool:
    j = r
    while segs[j].op in ("|", "|&") and j + 1 < len(segs):
        w = _unwrap(segs[j + 1].words)
        if not w or _base(w[0]) not in _KEEPS_LINES:
            return False
        j += 1
    return True


def run_outcome(command, output, exit_code, kind, truncated=False, error=False, skip=frozenset()) -> tuple:
    """How the `kind` runs in one shell command ended: ("pass" | "fail" |
    "unknown", detail). The exit code counts only when it belongs to the run
    (no pipe, `;`, or `||` after it); otherwise the runner's own summary line
    decides, then an `&& echo` marker, then silence from a silent checker.
    Segments in `skip` (runs on a stashed tree) are left out; their output
    may hold any summary line, so then only the exit code and markers count."""
    segs = _expand(parse_shell(command))
    runs = [i for i, s in enumerate(segs) if kind in _segment_kinds(s.words) and i not in skip]
    if not runs:
        return "unknown", ""
    output = _ANSI.sub("", output) if isinstance(output, str) else ""
    pipefail = bool(re.search(r"\bpipefail\b", command or ""))
    families = set()
    for r in runs:
        families |= _families(segs[r].words)
    found = [] if skip else _summaries(output, families, kind)
    fails = [f for f in found if f[1] == "fail"]
    if len(runs) > 1 and fails:
        return "fail", fails[-1][2]
    for m in _NOT_FOUND.finditer(output):
        missing = _base(m.group(1) or m.group(2) or m.group(3) or "")
        programs = {_base(_unwrap(segs[r].words)[0]) for r in runs if _unwrap(segs[r].words)}
        programs |= {_base(segs[r].words[0]) for r in runs if segs[r].words}
        if missing in programs:
            return "fail", "command not found"
    direct, details = [], []
    for r in runs:
        verdict, detail = _exit_verdict(segs, r, exit_code, pipefail, kind), "exit code %s" % exit_code
        if verdict is None:
            echoed = _echoed_exit(segs, r, output)
            if echoed is not None and (kind == TESTS or _segment_kinds(segs[r].words) == _B or echoed == 0):
                verdict, detail = ("pass" if echoed == 0 else "fail"), "exit code %d" % echoed
        if verdict is None:
            verdict = _marker(segs, r, output)
            detail = "success printed" if verdict == "pass" else "failure printed"
        direct.append(verdict)
        details.append(detail)
    if "fail" in direct:
        return "fail", details[direct.index("fail")]
    if all(d == "pass" for d in direct):
        return "pass", details[-1]
    if fails:
        return "fail", fails[-1][2]
    passes = [f for f in found if f[1] == "pass"]
    missing_runs = [r for r, d in zip(runs, direct) if d != "pass"]
    silent_ok = [r for r in missing_runs
                 if _is_silent(segs[r].words, output) and not truncated and not skip
                 and _pipe_keeps_lines(segs, r)]
    missing_runs = [r for r in missing_runs if r not in silent_ok]
    if not missing_runs:
        return "pass", "no errors printed"
    if passes and len(passes) >= len(missing_runs):
        return "pass", passes[-1][2]
    if error and exit_code is None and _TIMED_OUT.search(output[:300]):
        return "fail", "did not finish"
    return "unknown", ""


# ---------------------------------------------------------------------------
# Which changed files can change a result
# ---------------------------------------------------------------------------

_DOC_EXTS = {".md", ".mdx", ".markdown", ".rst", ".txt", ".adoc", ".asciidoc", ".org", ".png", ".jpg", ".jpeg",
             ".gif", ".webp", ".ico", ".pdf", ".log", ".diff", ".patch", ".orig", ".rej", ".bak"}
_PROSE_EXTS = {".md", ".mdx", ".markdown", ".rst", ".adoc", ".asciidoc", ".org"}
_LEFTOVER_EXTS = {".log", ".orig", ".rej", ".bak"}
_DOC_NAMES = {"license", "licence", "notice", "authors", "changelog", "changes", "history", "contributors",
              "copying"}
_GENERATED = {"node_modules", "dist", "build", "target", ".pytest_cache", "__pycache__", ".mypy_cache",
              ".ruff_cache", "coverage", "htmlcov", ".next", ".nuxt", ".turbo", ".cache", ".tox", ".nox", ".venv",
              "venv", "site", "_site", "_build", "storybook-static", "tmp", "temp"}
# Version control folders hold no file that a run tests: a change in them never counts.
_VCS_FOLDERS = {".git", ".hg", ".svn"}
# Editor and coding agent folders: agents keep plans, notes, settings, and state files in them, and those
# cannot change a test result. Source files, scripts, and tests in them can (a repository can test its
# .claude/hooks scripts), so they count. Other hidden folders count whole: .github, .cargo, .config,
# .circleci, and .husky can hold tests or build settings.
_TOOL_FOLDERS = {".idea", ".vscode", ".claude", ".codex", ".gemini", ".cursor", ".opencode", ".superpowers"}
_TOOL_STATE_EXTS = {".json", ".jsonc", ".json5", ".jsonl", ".ndjson", ".yaml", ".yml", ".toml", ".xml", ".iml",
                    ".ini", ".mdc", ".lock"}
# Test and fixture folders: a data file in them (expected output, a golden image) is test input.
_TEST_PARTS = {"test", "tests", "__tests__", "testing", "spec", "specs", "fixture", "fixtures", "__fixtures__",
               "testdata", "test_data", "snapshots", "__snapshots__"}
_TEMP = ("/tmp/", "/private/tmp/", "/var/folders/", "/private/var/folders/", "/dev/")
_HARNESS_HOME = (".claude", ".codex", ".gemini", ".cursor", ".config/opencode", ".local/share/opencode")


def path_matters(path, home=None, cwd=None) -> bool:
    """Whether a change to this file can change a test or build result.
    Documentation, images, generated and version control folders, the plans and settings
    in editor and agent folders, and, outside the working folder, temporary files and the
    harnesses' own folders in the home folder do not. Data files in test and fixture
    folders do. An unknown path does."""
    if not path:
        return True
    p = str(path).replace("\\", "/")
    if "$" in p and not _FILE_EXT.search(p):
        return False  # a file named by a shell variable (a scratch or log file), not a source file
    inside = bool(cwd) and (not p.startswith("/") or p.startswith(str(cwd).rstrip("/") + "/"))
    if not inside:
        if p.startswith(_TEMP):
            return False
        home = (home or os.path.expanduser("~")).rstrip("/")
        for folder in _HARNESS_HOME:
            if p.startswith(home + "/" + folder + "/"):
                return False
    if inside and p.startswith("/"):
        p = p[len(str(cwd).rstrip("/")) + 1:]  # judge folders from the working folder down
    parts = [x for x in p.split("/") if x]
    if not parts or any(x in _GENERATED or x in _VCS_FOLDERS for x in parts[:-1]):
        return False  # generated output, or a version control folder (.git, .hg, .svn)
    if parts[-1] in _GENERATED and (len(parts) == 1 or parts[-1] != "build"):
        return False  # the generated folder itself, as in `rm -rf dist` (a file named build may be a script)
    name = parts[-1].lower()
    stem, ext = os.path.splitext(name)
    if stem in _DOC_NAMES and ext in ("", ".txt"):
        return False  # LICENSE, CHANGELOG
    if any(x.lower() in _TEST_PARTS for x in parts[:-1]):
        return ext not in _PROSE_EXTS and ext not in _LEFTOVER_EXTS  # test input: fixtures, expected output
    if ext in _TOOL_STATE_EXTS and any(x in _TOOL_FOLDERS for x in parts[:-1]):
        return False  # an agent's or editor's plan, settings, or state file (.claude/settings.json)
    if ext in (".jsonl", ".ndjson"):
        return False  # a log or state file an agent appends to, not source
    return ext not in _DOC_EXTS


# ---------------------------------------------------------------------------
# One session, in order
# ---------------------------------------------------------------------------

def subagent_files(path) -> list:
    """Claude Code keeps a session's subagent transcripts in
    <session>/subagents/ (workflow agents one level deeper), whatever their age."""
    path = str(path)
    folder = path[:-len(".jsonl")] + os.sep + "subagents" if path.endswith(".jsonl") else ""
    if not folder or not os.path.isdir(folder):
        return []
    return sorted(glob.glob(os.path.join(folder, "agent-*.jsonl")) +
                  glob.glob(os.path.join(folder, "workflows", "*", "agent-*.jsonl")))


_TS = re.compile(r"^(\d{4})-(\d\d)-(\d\d)[T ](\d\d):(\d\d):(\d\d)(\.\d+)?(Z|[+-]\d\d:?\d\d)?$")


def epoch(ts) -> Optional[float]:
    """Seconds since 1970 for an ISO 8601 time, or None."""
    m = _TS.match(ts or "")
    if not m:
        return None
    y, mo, d, h, mi, s = (int(m.group(i)) for i in range(1, 7))
    value = calendar.timegm((y, mo, d, h, mi, s, 0, 0, 0)) + float(m.group(7) or 0)
    zone = m.group(8)
    if zone and zone != "Z":
        sign = 1 if zone[0] == "+" else -1
        value -= sign * (int(zone[1:3]) * 3600 + int(zone[-2:]) * 60)
    return value


def _exit_code(call, harness) -> Optional[int]:
    if call.exit_code is not None:
        return call.exit_code
    # Claude Code marks every nonzero exit of a test runner as an error, even in
    # files that keep no exit code (subagent transcripts): no error means exit 0.
    if harness == "claude-code" and call.has_result and not call.is_error and not call.interrupted:
        return 0
    return None


# -- Codex code mode: shell work inside a JavaScript `exec` script -------------

_JS_EXEC = re.compile(r"tools\.exec_command\(\s*\{")
_JS_CMD_KEY = re.compile(r"(?:[\"']cmd[\"']|\bcmd)\s*:\s*")
_JS_PATCH = re.compile(r"tools\.apply_patch\(")
_PATCH_FILE = re.compile(r"\*\*\* (?:Update|Add|Delete) File: ([^\n`'\"]+?)(?=\s*(?:\n|\\n|$))|"
                         r"\*\*\* Move to: ([^\n`'\"]+?)(?=\s*(?:\n|\\n|$))")
_JS_ESCAPES = {"n": "\n", "t": "\t", "r": "\r", "0": "\0", "b": "\b", "f": "\f", "v": "\v"}
_PRINTED_EXIT = re.compile(r"[\"']?exit_?[cC]ode[\"']?\s*[=:]\s*(-?\d+)")


def _js_string(text, i) -> tuple:
    """The JavaScript string literal that starts at text[i], unescaped, and the index after it."""
    quote, out, i = text[i], [], i + 1
    while i < len(text) and text[i] != quote:
        ch = text[i]
        if ch == "\\" and i + 1 < len(text):
            nxt = text[i + 1]
            if nxt == "u" and re.match(r"[0-9a-fA-F]{4}", text[i + 2:i + 6]):
                out.append(chr(int(text[i + 2:i + 6], 16)))
                i += 6
                continue
            out.append(_JS_ESCAPES.get(nxt, nxt))
            i += 2
            continue
        out.append(ch)
        i += 1
    return "".join(out), i + 1


def codex_script_commands(script) -> list:
    """The shell commands a Codex code-mode script passes to tools.exec_command, in order."""
    cmds = []
    for m in _JS_EXEC.finditer(script or ""):
        nxt = _JS_EXEC.search(script, m.end())
        k = _JS_CMD_KEY.search(script, m.end(), nxt.start() if nxt else len(script))
        if k and k.end() < len(script) and script[k.end()] in "\"'`":
            cmds.append(_js_string(script, k.end())[0])
    return cmds


_JS_WORKDIR = re.compile(r"(?:[\"']workdir[\"']|\bworkdir)\s*:\s*")


def _js_workdir(script) -> Optional[str]:
    """The workdir of the first tools.exec_command call, when it is a literal."""
    m = _JS_EXEC.search(script or "")
    k = _JS_WORKDIR.search(script, m.end()) if m else None
    if k and k.end() < len(script) and script[k.end()] in "\"'`":
        value = _js_string(script, k.end())[0]
        return value if value.startswith("/") else None
    return None


def _hidden_commands(script) -> list:
    """Activity steps for commands a script passes to tools.exec_command by
    variable ({cmd} or {cmd: name}): what they did cannot be read, so a
    runner named anywhere in the script's strings makes later claims unclear."""
    calls = len(_JS_EXEC.findall(script or ""))
    if not calls or len(codex_script_commands(script)) >= calls:
        return []
    named, kinds = set(), set()
    for _q, lit in _LITERAL.findall(script):
        named |= command_kinds(lit)
        kinds |= {k for k in (TESTS, BUILD) if _ACTIVITY[k].search(lit)}
    return [("activity", k, k in named) for k in (TESTS, BUILD) if k in kinds | named]


_ROOTS = {}


def _repo_root(folder) -> Optional[str]:
    """The git repository that holds a folder on this machine, when it can be found."""
    if not folder or not str(folder).startswith("/"):
        return None
    folder = str(folder)
    if folder not in _ROOTS:
        cur, found = folder, None
        for _ in range(64):
            if os.path.exists(os.path.join(cur, ".git")):
                found = cur
                break
            parent = os.path.dirname(cur)
            if parent == cur:
                break
            cur = parent
        _ROOTS[folder] = found
    return _ROOTS[folder]


def _run_scope(folder, named=False) -> Optional[str]:
    """The folder a run's result speaks for: its git repository, or, outside
    any repository, the folder the run started in when the command named it
    with cd (or the folder exists). Changes outside it do not make the run stale."""
    root = _repo_root(folder)
    if root:
        return root
    if folder and str(folder).startswith("/") and (named or os.path.isdir(str(folder))):
        return str(folder)
    return None


def _inside(path, folder) -> bool:
    return path == folder or path.startswith(folder.rstrip("/") + "/")


def _near(folder, root) -> Optional[str]:
    """The folder a run started in, when it is a subfolder of its repository
    (a package in a monorepo): changes inside it make the run stale, and
    changes elsewhere in the repository leave its result in doubt."""
    if root and folder and str(folder).startswith("/") and folder != root and _inside(str(folder), root):
        return str(folder)
    return None


def _script_patch_paths(script) -> list:
    paths = []
    for m in _JS_PATCH.finditer(script or ""):
        nxt = _JS_PATCH.search(script, m.end())
        for a, b in _PATCH_FILE.findall(script[m.end():nxt.start() if nxt else len(script)]):
            if (a or b).strip() not in paths:
                paths.append((a or b).strip())
    return paths


def _exec_results(body) -> list:
    """The exec_command result objects ({"chunk_id", "exit_code", "session_id",
    "output", ...}) a Codex script printed as JSON."""
    found, decoder, i = [], json.JSONDecoder(), 0
    while True:
        i = body.find('{"chunk_id"', i)
        if i < 0:
            return found
        try:
            value, end = decoder.raw_decode(body, i)
        except ValueError:
            i += 1
            continue
        if isinstance(value, dict):
            found.append(value)
        i = end


_VIEWERS = {"cat", "head", "tail", "less", "more", "grep", "egrep", "fgrep", "rg", "ag", "sed", "awk", "jq", "yq",
            "ls", "find", "fd", "tree", "wc", "sort", "uniq", "cut", "tr", "diff", "cmp", "echo", "printf", "true",
            "false", "git", "gh", "curl", "wget", "open", "mkdir", "touch", "cp", "mv", "rm", "ln", "chmod", "cd",
            "pwd", "which", "type", "stat", "file", "du", "df", "ps", "kill", "pkill", "sleep", "date", "export",
            "source", ".", "set", "unset", "test", "[", "nl", "xxd", "od", "base64", "shasum", "md5", "tar", "zip",
            "unzip", "pip", "pip3", "brew", "code", "vim", "nano", "lsof", "xargs", "column", "printenv", "read"}
_WORD_START = r"(?:(?<![A-Za-z0-9])|(?<=[a-z])(?=[A-Z]))"  # a word start, or a camelCase part (DevTests)
_ACTIVITY = {TESTS: re.compile(_WORD_START + r"(?i:test(?:s|ing)?|specs?|verif(?:y|ies|ication)|checks?|"
                               r"validat(?:e|es|ion|or)|smoke|e2e)(?![a-z])"),
             BUILD: re.compile(_WORD_START + r"(?i:build|compile|typecheck|type-check|tsc)(?![a-z])")}
_RUNNER_NAME = {TESTS: re.compile(_WORD_START + r"(?i:test(?:s|ing)?|specs?|e2e|smoke)(?![a-z])"),
                BUILD: re.compile(_WORD_START + r"(?i:build|compile|typecheck|type-check|tsc)(?![a-z])")}
_LINTERS = {"ruff", "black", "isort", "flake8", "pylint", "eslint", "prettier", "biome", "mypy", "pyright",
            "basedpyright", "tsc", "vue-tsc", "shellcheck", "terraform", "pre-commit", "stylelint", "markdownlint",
            "rubocop", "golangci-lint", "codespell"}
_KEYWORDS = {"for", "while", "until", "if", "elif", "case", "select", "function", "in"}
_TARGET_AFTER = {"run", "exec", "--bin", "--example", "--test", "-p", "--package", "--project"}
_OUTPUT_FAMILIES = ("pytest", "unittest", "jest", "vitest", "mocha", "bun", "node", "deno", "rspec", "phpunit",
                    "dotnet", "swift", "ctest", "minitest")
_PRINTERS = {"cat", "sed", "nl", "less", "more", "grep", "egrep", "fgrep", "rg", "ag", "awk", "jq", "yq", "bat",
             "xxd", "od", "strings"}
_GENERIC_SUMMARY = [
    (re.compile(r"(?mi)\b(\d+)\s*/\s*(\d+)\s+(?:tests?\s+)?(?:passed|pass|passing|ok|green)\b"),
     lambda m: ("pass" if m.group(1) == m.group(2) and int(m.group(2)) > 0 else "fail", " ".join(m.group(0).split()))),
    (re.compile(r"(?mi)^\s*(?:ok|FAIL)\s+\S+\s+[\d.]+s\s*$"),
     lambda m: ("fail" if m.group(0).strip().startswith("FAIL") else "pass", " ".join(m.group(0).split()))),
    (re.compile(r"test result: (?:ok|FAILED)\. \d+ passed; (\d+) failed"),
     lambda m: ("fail" if int(m.group(1)) else "pass", " ".join(m.group(0).split()))),
    (re.compile(r"(?mi)^\W*(\d+) (?:tests? )?passed,\s*(\d+) (?:tests? )?failed\b"),
     lambda m: ("fail" if int(m.group(2)) or not int(m.group(1)) else "pass", " ".join(m.group(0).split()))),
]


def _executing(command, skip=frozenset()) -> list:
    """The segments of a command that run a program, not just show or move files."""
    out = []
    for i, seg in enumerate(_expand(parse_shell(command))):
        if i in skip:
            continue
        if seg.words[:1] in (["command"], ["type"], ["which"]) and any(a in ("-v", "-V") for a in seg.words[1:2]):
            continue  # a lookup, not a run
        w = _unwrap(seg.words)
        if w and _base(w[0]) not in _VIEWERS:
            out.append(w)
    return out


def _path_like(arg) -> bool:
    """A file or folder argument, other than a document (a script run on a guide is not a test run)."""
    if " " in arg or "://" in arg or arg.startswith("-") or os.path.splitext(arg)[1].lower() in _DOC_EXTS:
        return False
    return "/" in arg or bool(_FILE_EXT.search(arg))


def _run_target(args) -> str:
    """The program after `run` or `exec` (swift run --scratch-path X DevTests), or the value of --bin."""
    for j, a in enumerate(args):
        if a in _TARGET_AFTER and a.startswith("-") and j + 1 < len(args):
            return args[j + 1]
        if a in ("run", "exec"):
            k = j + 1
            while k < len(args) and args[k].startswith("-"):
                k += 2 if (args[k].startswith("--") and "=" not in args[k]) else 1
            return args[k] if k < len(args) else ""
    return ""


def _activity(w, kind) -> Optional[bool]:
    """Whether an unrecognized command may have run tests (or a build):
    None when it does not look like it; True when it names a runner (one
    nested in another command, a script that may wrap one, or a program or
    script named for tests); False when only a script name or path suggests
    it. Linters, formatters, and type checkers never count, and only the
    program, path-like arguments, and the target of `run` are read, so a
    message such as "tests done" is not a run."""
    p, args = _base(w[0]), w[1:]
    if p in _LINTERS or _LINT_WORDS & set(_name_words(p)) or p in _KEYWORDS or \
            (p == "cargo" and args[:1] in (["check"], ["clippy"], ["fmt"])) or (p == "go" and args[:1] in (["vet"], ["fmt"])):
        return None  # a linter or formatter, a lint script, or a shell loop header
    if kind == TESTS and _ambiguous_run(w):
        return True
    for i in range(1, len(w)):  # a runner inside: docker compose exec web pytest, ssh host "npm test"
        if kind in _segment_kinds(w[i:]) or (" " in w[i] and kind in command_kinds(w[i])):
            return True
    names = [p] + [a for a in args if _path_like(a)] + [_run_target(args)]
    hits = [n for n in names if n and _ACTIVITY[kind].search(n) and not _LINT_WORDS & set(_name_words(_base(n)))]
    if not hits:
        return None
    return any(_RUNNER_NAME[kind].search(_base(n)) for n in hits)


def _prints_files(command) -> bool:
    """Whether the command shows file contents, whose text may quote old results."""
    for seg in _expand(parse_shell(command)):
        w = _unwrap(seg.words)
        p = _base(w[0]) if w else ""
        if p in _PRINTERS or (p in ("head", "tail") and _args(w[1:], {"-n", "-c"})):
            return True
    return False


def _printed_test_run(output) -> Optional[tuple]:
    """(result, detail) when the end of the output holds a test runner's
    summary line (runners print it last)."""
    output = "\n".join([line for line in _ANSI.sub("", output).splitlines() if line.strip()][-15:])
    found = _summaries(output, _OUTPUT_FAMILIES, TESTS, pure=True)
    for rx, read in _GENERIC_SUMMARY:
        for m in rx.finditer(output):
            found.append((m.start(),) + read(m))
    if not found:
        return None
    found.sort()
    fails = [f for f in found if f[1] == "fail"]
    return ("fail", fails[-1][2]) if fails else ("pass", found[-1][2])


class _Steps:
    """Turns one session's tool calls into steps, in order: ("run", {kind:
    (result, detail)}, command, ts), ("edit", [paths that matter]), and
    ("activity", kind) for a command that may have run tests (or a build)
    in a way this reader cannot see. A run that finishes later (a Claude Code
    background command read back with TaskOutput, a Codex script finished by
    `wait`) gives a run step with its result when that result arrives."""

    def __init__(self, harness, cwd, home):
        self.harness, self.cwd, self.home = harness, cwd, home
        self.pending = {}  # background task id or script cell id -> (command, script)
        self.procs = {}    # Codex process session id -> the command still running in it
        self.stashed = False  # a `git stash` is in effect: runs test another tree until it is popped

    def _kept(self, paths):
        return [p for p in (paths or [None]) if path_matters(p, self.home, self.cwd)]

    def _shell(self, command, ts, outcome, output=None, cwd=None):
        """Steps for a shell command; outcome(kind) gives each run's result.
        With the finished output, an unrecognized command that printed a test
        runner's summary counts as a test run, and one that looks like a test
        or build command becomes an activity step."""
        steps, results, cwd = [], {}, cwd or self.cwd
        walked = list(_walk_shell(command, cwd, self.home or os.path.expanduser("~"), self.stashed))
        skip = frozenset(i for step, _v, i, _f in walked if step == "detour")
        ended = walked[-1][3] if walked else None
        for step, value, _i, folder in walked:
            if step == "stash":
                if value == "pop":  # a stash this session did not make: its files come back
                    kept = self._kept([folder] if folder else None)
                    steps.extend([("edit", kept)] if kept else [])
                elif value == "restore" and self.stashed:
                    steps.append(("unstash",))
                self.stashed = value == "push"
            elif step == "run":
                for kind in value:
                    if kind not in results:
                        results[kind] = outcome(kind, skip)
                where = folder or cwd
                root = _run_scope(where, bool(folder) and folder != cwd)
                steps.append(("run", {k: results[k] for k in value}, command, ts, root, _near(where, root),
                              self.stashed))
            elif step == "edit":
                kept = self._kept(value)
                if kept:
                    steps.append(("edit", kept))
        running = _executing(command, skip)
        if running and output is not None and TESTS not in results and not skip and not _prints_files(command):
            printed = _printed_test_run(output)
            if printed:  # scoped to the folder the command changed into, if any
                results[TESTS] = printed
                where = ended or cwd
                root = _run_scope(where, bool(ended) and ended != cwd)
                steps.append(("run", {TESTS: printed}, command, ts, root, _near(where, root), self.stashed))
        for kind in (TESTS, BUILD):
            found = [a for a in (_activity(w, kind) for w in running) if a is not None] if kind not in results else []
            if found:
                steps.append(("activity", kind, any(found)))
        return steps

    def call(self, call) -> list:
        if call.denied:
            return []
        if not call.has_result:  # a run whose result never reached the transcript
            command = call.command or ""
            if self.harness == "codex" and call.name == "exec" and "tools." in command:
                command = "\n".join(codex_script_commands(command))
            kinds = command_kinds(command) if call.kind == "shell" or call.name == "exec" else _NONE
            return [("activity", k, True) for k in (TESTS, BUILD) if k in kinds]
        if call.kind in ("edit", "write"):
            kept = [] if call.is_error else self._kept(call.paths)
            return [("edit", kept)] if kept else []
        if call.kind == "mcp" and not call.is_error and _MCP_WRITES.search(call.name.rsplit("__", 1)[-1]):
            home = self.home or os.path.expanduser("~")
            named = [_resolve(p, self.cwd, {}, home) for p in _input_paths(call.input)]
            kept = self._kept(named or ([self.cwd] if self.cwd else None))
            return [("edit", kept)] if kept else []
        if self.harness == "codex" and call.name == "exec" and "tools." in (call.command or ""):
            return self._codex_script(call)
        if self.harness == "codex" and call.name == "wait":
            return self._finish(str(_as_dict(call.input).get("cell_id", "")), call)
        if self.harness == "claude-code" and call.name == "TaskOutput":
            return self._finish(str(_as_dict(call.input).get("task_id", "")), call)
        if call.kind != "shell" or not call.command:
            return []
        if call.interrupted:
            return self._shell(call.command, call.ts, lambda kind, skip: ("unknown", "interrupted"))
        if _as_dict(call.input).get("run_in_background"):
            m = re.search(r"with ID:\s*(\S+)", call.output)
            if m and command_kinds(call.command):
                self.pending[m.group(1)] = (call.command, None)
            return self._shell(call.command, call.ts, lambda kind, skip: ("unknown", "started in the background"))
        exit_code, truncated = _exit_code(call, self.harness), call.output_chars > len(call.output)
        return self._shell(call.command, call.ts, lambda kind, skip: run_outcome(
            call.command, call.output, exit_code, kind, truncated=truncated, error=call.is_error, skip=skip),
            call.output)

    def _script_result(self, script, text, ts, truncated):
        """Steps for a finished Codex script. A command that outlives the call
        returns a process session_id instead of an exit code; a later
        tools.write_stdin poll on that session brings the exit code."""
        body = text.split("Output:\n", 1)[1] if "Output:\n" in text else ""
        truncated = truncated or "Warning: truncated output" in body[:200]
        results = _exec_results(body)
        polled = re.findall(r"tools\.write_stdin\(\s*\{[^}]*?[\"']?session_id[\"']?\s*:\s*[\"']?(\d+)", script)
        if polled:
            if len(polled) == 1 and len(results) == 1 and isinstance(results[0].get("exit_code"), int) \
                    and polled[0] in self.procs:
                (command, workdir), done = self.procs.pop(polled[0]), results[0]
                return self._shell(command, ts, lambda kind, skip: run_outcome(
                    command, str(done.get("output") or ""), done["exit_code"], kind, truncated=truncated, skip=skip),
                    str(done.get("output") or ""), workdir)
            return []
        cmds = codex_script_commands(script)
        command = "\n".join(cmds)
        if not command:
            return []
        workdir = _js_workdir(script)
        if len(cmds) == 1 and len(results) == 1:
            done = results[0]
            if isinstance(done.get("exit_code"), int):
                return self._shell(command, ts, lambda kind, skip: run_outcome(
                    command, str(done.get("output") or ""), done["exit_code"], kind, truncated=truncated, skip=skip),
                    str(done.get("output") or ""), workdir)
            if done.get("session_id") is not None:
                if command_kinds(command):
                    self.procs[str(done["session_id"])] = (command, workdir)
                return self._shell(command, ts, lambda kind, skip: ("unknown", "still running"), None, workdir)
        exit_code = None
        codes = set(_PRINTED_EXIT.findall(body))
        if len(cmds) == 1 and len(codes) == 1 and re.search(r"exit_?[cC]ode", script):
            exit_code = int(codes.pop())
        return self._shell(command, ts, lambda kind, skip: run_outcome(command, body, exit_code, kind,
                                                                       truncated=truncated, skip=skip),
                           body, workdir)

    def _codex_script(self, call) -> list:
        text, script = call.output, call.command
        if text.startswith("Script failed") and "SyntaxError" in text[:500]:
            return []  # the script never ran
        command = "\n".join(codex_script_commands(script))
        steps = []
        patch = self._kept(_script_patch_paths(script)) if _JS_PATCH.search(script) else []
        running = re.match(r"Script running with cell ID (\S+)", text)
        workdir = _js_workdir(script)
        if running:
            if command_kinds(command) or "tools.write_stdin" in script:
                self.pending[running.group(1)] = (command, script)
            steps = self._shell(command, call.ts, lambda kind, skip: ("unknown", "still running"), None, workdir) \
                if command else []
        elif text.startswith("Script failed"):
            steps = self._shell(command, call.ts, lambda kind, skip: ("unknown", "the script failed"), None, workdir) \
                if command else []
        else:
            steps = self._script_result(script, text, call.ts, call.output_chars > len(call.output))
        return ([("edit", patch)] if patch else []) + steps + _hidden_commands(script)

    def _finish(self, key, call) -> list:
        """A background command or a running script reports its result."""
        if key not in self.pending:
            return []
        command, script = self.pending[key]
        text = call.output
        if script is not None:  # a Codex script cell
            if text.startswith("Script running"):
                return []
            del self.pending[key]
            if text.startswith("Script failed"):
                return self._shell(command, call.ts, lambda kind, skip: ("unknown", "the script failed")) \
                    if command else []
            return self._script_result(script, text, call.ts, call.output_chars > len(call.output))
        status = re.search(r"<status>(\w+)</status>", text)
        if not status or status.group(1) in ("running", "pending"):
            return []
        del self.pending[key]
        code = re.search(r"<exit_code>(-?\d+)</exit_code>", text)
        out = re.search(r"<output>\n?(.*?)(?:</output>|\Z)", text, re.S)
        exit_code = int(code.group(1)) if code else None
        body = out.group(1) if out else ""
        return self._shell(command, call.ts, lambda kind, skip: run_outcome(command, body, exit_code, kind, skip=skip),
                           body)


def _as_dict(value) -> dict:
    return value if isinstance(value, dict) else {}


_MCP_WRITES = re.compile(r"(?:^|[_.-])(?:write|edit|replace|insert|rename)(?:$|[_.-])|"
                         r"(?:create|delete|move)[_-]?file", re.I)
_PATH_KEY = re.compile(r"^(?:paths?|files?|file_?path|filename|target|destination|dest|source|new_?path|old_?path)$",
                       re.I)


def _input_paths(inp) -> list:
    """File paths named in an MCP tool call's input."""
    out = []
    for key, value in _as_dict(inp).items():
        if _PATH_KEY.match(str(key)):
            out.extend(v for v in (value if isinstance(value, list) else [value]) if isinstance(v, str) and v)
    return out


def _timeline(session, subagents, home):
    """(epoch, step, source) triples: the main session in file order (source
    None), with each subagent's steps (source = its index) placed by time.
    Claims come only from the main session."""
    main, last, reader = [], 0.0, _Steps(session.harness, session.cwd, home)
    for e in session.events:
        t = epoch(e.ts)
        last = max(last, t) if t is not None else last
        if e.kind == "tool" and e.tool is not None:
            main.extend((last, step, None) for step in reader.call(e.tool))
        elif e.kind == "assistant" and e.text and not e.sidechain:
            claims = find_claims(e.text)
            if claims:
                main.append((last, ("claim", claims, e), None))
        elif e.kind == "user" and not e.injected and not _HANDBACK.search(e.text or ""):
            main.append((last, ("prompt",), None))
    extra = []
    for k, sub in enumerate(subagents):
        sub_last, sub_reader = 0.0, _Steps(sub.harness, sub.cwd or session.cwd, home)
        for e in sub.events:
            t = epoch(e.ts)
            sub_last = t if t is not None else sub_last
            if e.kind == "tool" and e.tool is not None:
                extra.extend((sub_last, step, k) for step in sub_reader.call(e.tool))
    extra.sort(key=lambda item: item[0])
    merged, j = [], 0
    for item in main:
        while j < len(extra) and extra[j][0] < item[0]:
            merged.append(extra[j])
            j += 1
        merged.append(item)
    merged.extend(extra[j:])
    return merged


def _entry(run=None) -> dict:
    # changed: files the run's result speaks for; elsewhere: other files in the same repository;
    # others: files another subagent changed after a subagent's run (parallel work);
    # activity: a later command that may have run tests unseen (runner: it names a runner);
    # edits: how many changes counted, repeats included
    return {"run": run, "changed": [], "elsewhere": [], "others": [], "activity": False, "runner": False,
            "edits": 0}


def _fresh() -> dict:
    return {kind: _entry() for kind in (TESTS, BUILD)}


def _apply(state, step, src=None) -> None:
    """Fold one step into the state; src is None for the main session, else the subagent's number."""
    if step[0] == "run":
        for kind, result in step[1].items():
            state[kind] = _entry({"command": step[2], "result": result[0], "detail": result[1], "ts": step[3],
                                  "root": step[4] if len(step) > 4 else None,
                                  "near": step[5] if len(step) > 5 else None, "by": src})
            state[kind]["stashed"] = bool(step[6]) if len(step) > 6 else False
    elif step[0] == "edit":
        for kind in state:
            run = state[kind]["run"]
            if run is None:
                continue
            root, near = run.get("root"), run.get("near")
            for p in step[1]:
                absolute = bool(p) and p.startswith("/")
                if root and absolute and not _inside(p, root):
                    continue  # a change in another repository than the one the run tested
                if src is not None and run.get("by") is not None and src != run["by"]:
                    where = "others"  # two subagents working side by side: the change may be unrelated
                elif near and absolute and not _inside(p, near) and not _inside(near, p):
                    where = "elsewhere"
                else:
                    where = "changed"
                name = p or "(files the command did not name)"
                if name not in state[kind][where]:
                    state[kind][where].append(name)
                if where == "changed":
                    state[kind]["edits"] += 1
    elif step[0] == "unstash":  # the stash came back: a run made while it was in effect tested another tree
        for kind in state:
            if state[kind].get("stashed"):
                state[kind]["activity"], state[kind]["runner"] = True, True
    elif step[0] == "activity":
        state[step[1]]["activity"] = True
        state[step[1]]["runner"] = state[step[1]]["runner"] or bool(step[2] if len(step) > 2 else True)


_AGENT_ID = re.compile(r"\bagentId:\s*([\w-]+)")
# A subagent's report, delivered to the main session as a user-role message (not a person's prompt).
_HANDBACK = re.compile(r"<agent-message from=\"([\w-]+)\">")
_NOTICE = re.compile(r"<task-notification>(.*?)</task-notification>", re.S)


def _subagent_ends(session, subagents) -> list:
    """When the main session received each subagent's result: the Agent call
    that returned it (the call's time orders it, since the main session waits
    for it), or the hand-back message, task notification, or TaskOutput for a
    background agent.
    A background agent that never reported back has not finished. A subagent
    the main session does not name ends at its last event."""
    done, launched = {}, set()
    for e in session.events:
        t, call = epoch(e.ts), (e.tool if e.kind == "tool" else None)
        if t is None:
            continue
        if call is not None and call.name in ("Agent", "Task") and call.has_result:
            m = _AGENT_ID.search(call.output)
            if m and call.output.lstrip().startswith("Async agent launched"):
                launched.add(m.group(1))
            elif m:
                done.setdefault(m.group(1), t)
        elif call is not None and call.name == "TaskOutput" and call.has_result and \
                re.search(r"<status>(?:completed|failed|killed)</status>", call.output):
            done.setdefault(str(_as_dict(call.input).get("task_id", "")), t)
        elif e.kind == "user" and _HANDBACK.search(e.text or ""):
            for agent in _HANDBACK.findall(e.text):
                done.setdefault(agent, t)
        elif e.kind == "user" and "<task-notification>" in (e.text or ""):
            for block in _NOTICE.findall(e.text):
                task = re.search(r"<task-id>\s*([^<\s]+)\s*</task-id>", block)
                status = re.search(r"<status>\s*(\w+)", block)
                if task and not (status and status.group(1) in ("running", "pending")):
                    done.setdefault(task.group(1), t)
    ends = []
    for sub in subagents:
        if sub.id in done:
            ends.append(done[sub.id])
        elif sub.id in launched:
            ends.append(float("inf"))
        else:
            ends.append(max([epoch(e.ts) or 0.0 for e in sub.events] or [0.0]))
    return ends


def _undated(sub) -> bool:
    """A transcript whose lines all carry one time (a Codex paginated page is
    stamped when it is created): its events cannot be ordered against claims."""
    return len(sub.events) >= 5 and len({e.ts for e in sub.events if e.ts}) == 1


def _step_kinds(step) -> set:
    if step[0] == "run":
        return set(step[1])
    if step[0] == "activity":
        return {step[1]}
    return {TESTS, BUILD} if step[0] == "edit" else set()


def _claim_states(session, subagents, home):
    """Yield (epoch, claim step, state, doubt) for each claim. The state holds
    the main session's runs and changes plus those of the subagents that had
    finished before the claim: a subagent still working in parallel is doing
    other work, so its runs and changes are not evidence yet. `doubt` maps a
    kind to the reason its evidence cannot be trusted: an undated subagent
    page that may hold later runs, or a claim that may relay a subagent that
    was still working (the main session ran no test itself in this turn)."""
    merged, ends = _timeline(session, subagents, home), _subagent_ends(session, subagents)
    skip = {k for k, sub in enumerate(subagents) if _undated(sub)}
    undated = {}   # undated subagent -> (when its page began, kinds it touched)
    tested = {}    # dated subagent -> {kind: time of its first run or test-like command}
    for t, step, src in merged:
        if src in skip:
            start, kinds = undated.get(src, (t, set()))
            undated[src] = (min(start, t), kinds | _step_kinds(step))
        elif src is not None and step[0] in ("run", "activity"):
            for kind in _step_kinds(step):
                tested.setdefault(src, {}).setdefault(kind, t)
    turn, finished, state, pos = set(), None, None, 0
    for i, (t, step, src) in enumerate(merged):
        if src is None and step[0] == "prompt":
            turn = set()
        elif src is None and step[0] == "run":
            turn |= set(step[1])
        if step[0] != "claim":
            continue
        now = frozenset(k for k, end in enumerate(ends) if end <= t and k not in skip)
        if now != finished:
            finished, state, pos = now, _fresh(), 0  # a subagent finished: walk again with its evidence
        while pos < i:
            src_p = merged[pos][2]
            if src_p is None or src_p in finished:
                _apply(state, merged[pos][1], src_p)
            pos += 1
        doubt = {}
        for start, kinds in undated.values():
            if start <= t:
                for kind in kinds:
                    doubt.setdefault(kind, "a subagent's transcript has no event times")
        for k, first in tested.items():
            if ends[k] > t:
                for kind, when in first.items():
                    if when <= t and kind not in turn:
                        doubt.setdefault(kind, "the claim may relay a subagent that was still working")
        yield t, step, state, doubt


def _label(entry, scope, named=()) -> tuple:
    """(label, why) for one claim from the evidence before it. A later
    command that may have run tests leaves the claim unclear, except after a
    failed run when that command names no test runner (a linter, a message)."""
    run = entry["run"]
    if entry["activity"] and not (run is not None and run["result"] == "fail" and not entry["runner"]):
        return "unclear", "a later command may have run tests in a way this check cannot read"
    if run is None:
        if scope == "one":
            return "unclear", "the claim names one test, which a custom command may have run"
        return "unsupported", ""
    if named and not set(named) & _programs(run["command"]):
        return "unclear", "the claim names another command than the last run"
    if run["result"] == "fail":
        if scope == "one":
            return "unclear", "the run had a failure, and the claim names one test that may have passed"
        return "contradicted", ""
    if run["result"] == "unknown":
        return "unclear", run["detail"] or "the run's result could not be read"
    if entry["changed"]:
        return "stale", ""
    if entry["elsewhere"]:
        return "unclear", "code changed elsewhere in the repository"
    if entry["others"]:
        return "unclear", "another subagent changed code after the run"
    return "backed", ""


def label_claims(session, subagents=(), home=None, since=None) -> list:
    """Every claim in the main session, labeled from the runs and file changes
    before it (finished subagents count). `since` is an epoch: earlier claims
    are left out. A transcript that records no tool calls at all gives no
    evidence either way, so its claims are unclear."""
    subagents = list(subagents)
    silent = not any(e.kind == "tool" for s in [session] + subagents for e in s.events)
    out = []
    for t, step, state, doubt in _claim_states(session, subagents, home):
        if since is not None and t < since:
            continue
        for claim in step[1]:
            entry = state[claim["kind"]]
            label, why = _label(entry, claim.get("scope", "all"), claim.get("commands", ()))
            if claim["kind"] in doubt:
                label, why = "unclear", doubt[claim["kind"]]
            if silent:
                label, why = "unclear", "no tool calls recorded"
            out.append({"kind": claim["kind"], "label": label, "why": why, "excerpt": claim["excerpt"],
                        "scope": claim.get("scope", "all"), "ts": step[2].ts, "session": session.id,
                        "harness": session.harness, "path": session.path, "cwd": session.cwd, "event": step[2],
                        "run": dict(entry["run"]) if entry["run"] else None, "changed": list(entry["changed"])})
    return out


def last_test_run(session, subagents=(), home=None) -> dict:
    """The last test run as the session ends and the files changed after it,
    plus whether that run or a change came after the user's last prompt.
    Subagents count once the main session has their result; runs in another
    repository than the session's own are left out."""
    subagents = list(subagents)
    main_end = max([epoch(e.ts) or 0.0 for e in session.events] or [0.0])
    done = [sub for sub, end in zip(subagents, _subagent_ends(session, subagents)) if end <= main_end]
    own_root = _repo_root(session.cwd)
    state, prompt, run_at, change_at = _fresh(), -1, -1, -1
    for i, (_t, step, src) in enumerate(_timeline(session, done, home)):
        if step[0] == "prompt":
            prompt = i
            continue
        if step[0] == "run" and own_root and len(step) > 4 and step[4] and step[4] != own_root:
            continue  # a run in another repository
        edits = state[TESTS]["edits"]
        _apply(state, step, src)
        if step[0] == "run" and TESTS in step[1]:
            run_at = i
        elif state[TESTS]["edits"] > edits:
            change_at = i
    final = state[TESTS]
    return {"run": dict(final["run"]) if final["run"] else None, "changed": list(final["changed"]),
            "activity": final["activity"], "runner": final["runner"],
            "current": run_at > prompt or change_at > prompt}
