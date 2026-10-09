#!/usr/bin/env python3
"""Find the harness version, model, or week where a coding agent's behavior changed.

Reads the user's own session transcripts (through transcripts.py), cuts them
into user turns, measures the same behavior numbers for every turn, splits the
turns by harness version, model, or ISO week, and tests each update: the
sessions just before it against the sessions just after it, each session
counting once. Read-only; nothing leaves the machine. Python 3.9+, standard
library only.
"""

from __future__ import annotations

import argparse
import calendar
import datetime
import json
import math
import os
import re
import shlex
import statistics
import sys
import time
from dataclasses import dataclass, field
from typing import Optional
from xml.sax.saxutils import escape

sys.dont_write_bytecode = True  # leave no cache files in the skill folder
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import pricing  # noqa: E402
import transcripts  # noqa: E402
from safe import code, safe_text  # noqa: E402


# ---------------------------------------------------------------------------
# User corrections (references/metrics.md). High precision over recall: the
# same list applies to every group, so a missed phrase shifts no comparison.
# ---------------------------------------------------------------------------

_CORRECTION_START = re.compile(r"""^(?:(?:please|hey|ok|okay|hmm|um|so)[\s,]+)?(?:
    no+\s*[,.!;:]|no+\s*$|nope\b|nah\b
  | (?:that|this|it)(?:'s|\s+is|\s+was)\s+(?:wrong|incorrect|not\s+(?:right|correct|what\s+i))
  | wrong\s*[,.!;:]|wrong\s+(?:file|one|place|branch|function|folder|directory|approach|way|command|path)\b
  | not\s+what\s+i\b
  | (?:undo|revert)\s+(?:that|this|it|your|those|these|what\s+you)\b
  | why\s+(?:did|would|are|do|have)\s+you\b
  | you\s+(?:didn't|did\s+not|forgot|missed|broke|ignored|skipped|deleted|removed|keep|still|never
            |haven't|have\s+not)\b
  | i\s+(?:already\s+)?(?:said|told\s+you|asked(?:\s+you)?)\b
  | (?:that|this|it)\s+(?:still\s+)?(?:didn't|did\s+not|doesn't|does\s+not|isn't|is\s+not)\s+work
  | still\s+(?:broken|failing|fails|wrong|not|the\s+same|doesn't|does\s+not|isn't|getting|seeing)\b
  | (?:try|do\s+it)\s+again\b
  | stop\s*[,.!;:]|stop\s+(?:doing|that|it|changing|editing|adding|making|guessing|asking)\b
  | wait\s*[,.!;:]
  | (?:don't|do\s+not)\s+do\s+that\b
)""", re.I | re.X)
_CORRECTION_ANYWHERE = re.compile(
    r"\b(?:not\s+what\s+i\s+(?:asked|wanted|meant|said)|i\s+(?:already|just)\s+told\s+you"
    r"|you(?:'re|\s+are)\s+not\s+done|read\s+the\s+(?:file|code|docs?)\s+first"
    r"|you\s+(?:didn't|did\s+not)\s+(?:read|check|test|run|look))\b", re.I)


def is_correction(text) -> bool:
    """True when a typed prompt tells the agent it got something wrong."""
    s = str(text or "").replace("\u2019", "'").strip()
    return bool(s) and bool(_CORRECTION_START.match(s) or _CORRECTION_ANYWHERE.search(s))


# ---------------------------------------------------------------------------
# Tool-call classes (references/metrics.md): "research" reads or searches
# files, "edit" changes a file through an edit tool, "other" is the rest.
# ---------------------------------------------------------------------------

# Programs that only look at files, including the filters a pipeline ends in.
_READERS = {"cat", "head", "tail", "less", "more", "bat", "nl", "wc", "file", "stat", "ls", "tree",
            "find", "fd", "rg", "grep", "egrep", "fgrep", "ag", "ack", "jq", "yq", "diff", "cmp", "du",
            "readlink", "realpath", "sed", "awk", "git", "sort", "uniq", "cut", "tr", "column"}
_NEUTRAL = {"cd", "pushd", "popd", "echo", "printf", "pwd", "true", ":", "export", "set", "unset"}
_WRAPPERS = {"time", "command", "builtin", "nohup", "env"}
_GIT_READS = {"log", "show", "diff", "status", "blame", "grep", "ls-files", "ls-tree", "rev-parse",
              "describe", "shortlog", "reflog", "cat-file"}
_FIND_ACTIONS = {"-delete", "-exec", "-execdir", "-ok", "-okdir", "-fprint", "-fprintf", "-fls"}
_ASSIGNMENT_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=")


def _split_shell(command):
    """Split a command at unquoted ;, &&, ||, |, & and line breaks.
    Returns (segments, writes): `writes` is True when output is redirected to
    a file other than /dev/null. Returns None when the quotes do not balance."""
    segments, cur, quote, writes, i, n = [], [], None, False, 0, len(command)
    while i < n:
        ch = command[i]
        if quote:
            cur.append(ch)
            if ch == "\\" and quote == '"' and i + 1 < n:
                cur.append(command[i + 1])
                i += 1
            elif ch == quote:
                quote = None
        elif ch == "\\" and i + 1 < n:
            cur.extend((ch, command[i + 1]))
            i += 1
        elif ch in "'\"":
            quote = ch
            cur.append(ch)
        elif ch == "#" and (not cur or cur[-1].isspace()):
            while i + 1 < n and command[i + 1] != "\n":
                i += 1
        elif ch == ">":
            cur.append(ch)
            j = i + 1
            if j < n and command[j] == ">":
                j += 1
            if j < n and command[j] == "&":  # 2>&1, >&2: a copy of another output, not a file
                i = j
                cur.append("&")
            else:
                while j < n and command[j] in " \t":
                    j += 1
                k = j
                while k < n and not command[k].isspace() and command[k] not in ";|&<>()":
                    k += 1
                if command[j:k] != "/dev/null":
                    writes = True
        elif ch == "&" and ((cur and cur[-1] == ">") or command[i + 1:i + 2] == ">"):
            cur.append(ch)  # &> and >& are redirections, not separators
        elif ch in ";|&\n":
            segments.append("".join(cur))
            cur = []
            if ch in "&|" and command[i + 1:i + 2] == ch:
                i += 1
        else:
            cur.append(ch)
        i += 1
    if quote:
        return None
    segments.append("".join(cur))
    return [s.strip() for s in segments if s.strip()], writes


def _segment_class(words) -> str:
    """'read', 'neutral', or 'other' for one simple command, split into words."""
    while words and (_ASSIGNMENT_RE.match(words[0]) or words[0] in _WRAPPERS):
        words = words[1:]
    if not words:
        return "neutral"
    program, args = os.path.basename(words[0]), words[1:]
    if program in _NEUTRAL:
        return "neutral"
    if program not in _READERS:
        return "other"
    if program == "sed" and any(a == "--in-place" or (a.startswith("-") and not a.startswith("--") and "i" in a)
                                for a in args):
        return "other"
    if program == "awk" and "inplace" in args:
        return "other"
    if program == "find" and _FIND_ACTIONS.intersection(args):
        return "other"
    if program == "fd" and ({"-x", "-X", "--exec", "--exec-batch"} & set(args)):
        return "other"
    if program == "git":
        rest = list(args)
        while rest and rest[0].startswith("-"):
            rest = rest[2:] if rest[0] in ("-C", "-c") else rest[1:]
        return "read" if rest and rest[0] in _GIT_READS else "other"
    return "read"


def shell_is_read(command) -> bool:
    """True when a shell command only inspects files: every part is a reader
    (cat, rg, sed -n, git log, ...) or a no-op (cd, echo), at least one part
    reads, and nothing is written to a file."""
    split = _split_shell(str(command or ""))
    if split is None or split[1]:
        return False
    classes = []
    for segment in split[0]:
        try:
            words = shlex.split(segment)
        except ValueError:
            return False
        classes.append(_segment_class(words))
    return "read" in classes and "other" not in classes


# Codex code mode: the model writes one script that calls tools.<name>(...).
_JS_STRING = r'"(?:\\.|[^"\\])*"|\'(?:\\.|[^\'\\])*\'|`(?:\\.|[^`\\])*`'
_CODE_CMD_RE = re.compile(r"exec_command\s*\(\s*\{[^{}]*?(?:\"cmd\"|'cmd'|\bcmd)\s*:\s*(" + _JS_STRING + ")", re.S)
_CODE_TOOL_RE = re.compile(r"\btools\.([A-Za-z_][A-Za-z0-9_]*)\s*\(")
_JS_ESCAPES = {"n": "\n", "t": "\t", "r": "\r"}


def _js_unquote(literal) -> str:
    if literal.startswith('"'):
        try:
            return json.loads(literal)
        except ValueError:
            pass
    return re.sub(r"\\(.)", lambda m: _JS_ESCAPES.get(m.group(1), m.group(1)), literal[1:-1], flags=re.S)


def script_commands(script) -> list:
    """The shell commands a Codex code-mode script runs through tools.exec_command."""
    return [_js_unquote(m.group(1)) for m in _CODE_CMD_RE.finditer(str(script or ""))]


def classify_call(call) -> str:
    """'research', 'edit', or 'other' for a transcripts.ToolCall."""
    if call.kind in ("edit", "write"):
        return "edit"
    if call.kind in ("read", "search"):
        return "research"
    if call.kind != "shell":
        return "other"
    if call.name != "exec":  # a plain shell command
        return "research" if shell_is_read(call.command) else "other"
    tools = set(_CODE_TOOL_RE.findall(call.command))  # a Codex code-mode script
    if "apply_patch" in tools:
        return "edit"
    commands = script_commands(call.command)
    runs = len(re.findall(r"\bexec_command\s*\(", call.command))
    if (tools and tools <= {"exec_command", "view_image"} and len(commands) == runs
            and all(map(shell_is_read, commands))):
        return "research"
    return "other"



# Files a read-only command names, for "edits to files not read earlier".
_PATH_READERS = {"cat", "head", "tail", "nl", "less", "more", "bat", "wc", "sed", "awk"}
_VALUE_OPTIONS = {"head": {"-n", "-c", "--lines", "--bytes"}, "tail": {"-n", "-c", "--lines", "--bytes"},
                  "sed": {"-e", "-f", "--expression", "--file"}, "awk": {"-F", "-v", "-f"}}
_SCRIPT_OPTIONS = {"-e", "-f", "--expression", "--file"}  # sed or awk read their script from this, not a plain argument
_PATCH_FILE_RE = re.compile(r"\*\*\* (?:Update|Add|Delete) File: ([^\n\\\"'`]+)")


def shell_read_paths(command) -> list:
    """The files a read-only shell command names: `cat a b`, `sed -n 1,9p f`, `head -n 20 f`."""
    split = _split_shell(str(command or ""))
    if split is None or split[1]:
        return []
    out = []
    for segment in split[0]:
        try:
            words = shlex.split(segment)
        except ValueError:
            return []
        while words and (_ASSIGNMENT_RE.match(words[0]) or words[0] in _WRAPPERS):
            words = words[1:]
        program = os.path.basename(words[0]) if words else ""
        if program not in _PATH_READERS:
            continue
        takes, plain, has_script, args, j = _VALUE_OPTIONS.get(program, set()), [], False, words[1:], 0
        while j < len(args):
            a = args[j]
            if a in takes:
                has_script = has_script or a in _SCRIPT_OPTIONS
                j += 2
                continue
            if not (a.startswith("-") and a != "-"):
                plain.append(a)
            j += 1
        if program in ("sed", "awk") and not has_script:
            plain = plain[1:]  # the first plain argument is the script
        out.extend(p for p in plain if not p.isdigit())
    return out


def path_key(path) -> str:
    """The last two parts of a file path, so /work/app/src/a.py and src/a.py match."""
    parts = [p for p in str(path or "").replace("\\", "/").split("/") if p and p != "."]
    return "/".join(parts[-2:])


def _call_paths(call, act) -> tuple:
    """Keys of the files a read or edit call touched, when they can be known."""
    if act not in ("research", "edit"):
        return ()
    paths = list(call.paths)
    if not paths and call.kind == "shell":
        if call.name != "exec":
            paths = shell_read_paths(call.command)
        elif act == "edit":
            paths = [p.strip() for p in _PATCH_FILE_RE.findall(call.command)]
        else:
            paths = [p for c in script_commands(call.command) for p in shell_read_paths(c)]
    return tuple(k for k in (path_key(p) for p in paths) if k)


# ---------------------------------------------------------------------------
# Turns: one typed prompt and everything the agent did until the next one
# ---------------------------------------------------------------------------

_TS_RE = re.compile(r"^(\d{4})-(\d\d)-(\d\d)[T ](\d\d):(\d\d):(\d\d)(?:\.(\d+))?(Z|[+-]\d\d:?\d\d)?$")


def epoch(ts) -> Optional[float]:
    """Seconds since 1970 for an ISO 8601 time (UTC when it has no zone); None when it does not parse."""
    m = _TS_RE.match(ts.strip()) if isinstance(ts, str) else None
    if not m:
        return None
    value = float(calendar.timegm(tuple(int(g) for g in m.group(1, 2, 3, 4, 5, 6)) + (0, 0, 0)))
    if m.group(7):
        value += int(m.group(7)[:6]) / 10.0 ** len(m.group(7)[:6])
    zone = m.group(8)
    if zone and zone != "Z":
        value -= (1 if zone[0] == "+" else -1) * (int(zone[1:3]) * 3600 + int(zone[-2:]) * 60)
    return value


@dataclass
class Turn:
    """One typed prompt and what the agent did until the next one, subagents included."""
    harness: str
    session: str
    project: str
    t: float                        # when the prompt was sent: seconds since 1970, UTC
    version: str = ""
    model: str = ""
    entrypoint: str = ""            # how Claude Code was started: cli, claude-desktop, sdk-ts, ...
    correction: bool = False        # the prompt corrects the agent
    interrupts: int = 0
    calls: int = 0
    reads: int = 0                  # calls that read or search files
    edits: int = 0
    path_edits: int = 0             # edits (not whole-file writes) to a known file
    blind_edits: int = 0            # of those, edits to a file not read earlier in the session
    errors: int = 0                 # failed calls, not counting denials and interruptions
    reads_before_edit: Optional[int] = None  # None when the turn made no edit
    responses: int = 0              # API responses with token counts
    output: int = 0
    reasoning: int = 0
    cost: Optional[float] = 0.0     # None when a model in the turn has no known price


@dataclass
class _Ev:
    """The parts of a transcripts.Event this script keeps; the rest is dropped to save memory."""
    kind: str                 # user | assistant | tool | interrupt
    id: str
    t: float
    ts: str = ""              # the event's own ISO time, for transcripts.unique_events(since=...)
    version: str = ""         # user: the Claude Code record's version
    entrypoint: str = ""      # user: the Claude Code record's entrypoint
    correction: bool = False  # user
    act: str = ""             # tool: research | edit | other
    failed: bool = False      # tool
    paths: tuple = ()         # tool: keys of the files read or edited
    whole_file: bool = False  # tool: a whole-file write rather than an edit
    model: str = ""           # assistant
    usage: object = None      # assistant: a transcripts.Usage or None


class _Draft:
    def __init__(self, session, prompt, fallback_model):
        self.session, self.prompt, self.fallback_model = session, prompt, fallback_model
        self.model, self.interrupts, self.acts, self.responses = "", 0, [], []


def _prompt_records(path) -> dict:
    """Record uuid -> (version, entrypoint) for Claude Code user records. Session.version
    is the file's first version; a session resumed after an update records later
    turns under the newer version, and each record says which."""
    found = {}
    try:
        with open(path, "rb") as fh:
            for raw in fh:
                if b'"user"' not in raw or b'"tool_use_id"' in raw:
                    continue
                try:
                    rec = json.loads(raw)
                except ValueError:
                    continue
                if (isinstance(rec, dict) and rec.get("type") == "user" and isinstance(rec.get("uuid"), str)
                        and isinstance(rec.get("version"), str)):
                    entry = rec.get("entrypoint")
                    found[rec["uuid"]] = (rec["version"], entry if isinstance(entry, str) else "")
    except OSError:
        pass
    return found


def _compact(session, records) -> tuple:
    """(compact events, prompts left out because they have no readable time)."""
    out, dropped, last_t = [], 0, epoch(session.started)
    for e in session.events:
        t = epoch(e.ts)
        if t is None:
            if last_t is None:  # nothing in the session has a time yet: the event cannot be placed
                dropped += int(e.kind == "user" and not e.injected)
                continue
            t = last_t
        last_t = t
        if e.kind == "user" and not e.injected:
            version, entry = records.get(e.id, ("", ""))
            out.append(_Ev("user", e.id, t, e.ts, version=version, entrypoint=entry, correction=is_correction(e.text)))
        elif e.kind == "tool" and e.tool is not None:
            c = e.tool
            act = classify_call(c)
            out.append(_Ev("tool", e.id, t, e.ts, act=act, failed=c.is_error and not c.denied and not c.interrupted,
                           paths=_call_paths(c, act), whole_file=c.kind == "write"))
        elif e.kind == "assistant":
            model = e.model if e.model and not e.model.startswith("<") else ""  # skip "<synthetic>"
            out.append(_Ev("assistant", e.id, t, e.ts, model=model, usage=e.usage))
        elif e.kind == "interrupt":
            out.append(_Ev("interrupt", e.id, t, e.ts))
    return out, dropped


def _drafts(session, events) -> list:
    drafts, cur, last_model = [], None, ""
    for e in events:
        if e.kind == "user":
            cur = _Draft(session, e, last_model)
            drafts.append(cur)
            continue
        if e.kind == "assistant" and e.model:
            last_model = e.model
        if cur is None:
            continue
        if e.kind == "tool":
            cur.acts.append((e.t, e.act, e.failed, e.paths, e.whole_file))
        elif e.kind == "assistant":
            cur.model = cur.model or e.model
            if e.usage is not None:
                cur.responses.append((e.model, e.usage))
        elif e.kind == "interrupt":
            cur.interrupts += 1
    return drafts


def _finish(draft, known) -> tuple:
    """The Turn for a draft. `known` holds the files the session has read or
    edited so far; it grows as the session's turns are finished in order."""
    s, p = draft.session, draft.prompt
    turn = Turn(harness=s.harness, session=s.id, project=s.cwd, t=p.t, version=p.version or s.version,
                model=draft.model or draft.fallback_model, entrypoint=p.entrypoint, correction=p.correction,
                interrupts=draft.interrupts)
    acts = sorted(draft.acts, key=lambda a: a[0])  # subagent calls fall in between by time
    turn.calls = len(acts)
    turn.reads = sum(1 for a in acts if a[1] == "research")
    turn.edits = sum(1 for a in acts if a[1] == "edit")
    turn.errors = sum(1 for a in acts if a[2])
    first_edit = next((i for i, a in enumerate(acts) if a[1] == "edit"), None)
    if first_edit is not None:
        turn.reads_before_edit = sum(1 for a in acts[:first_edit] if a[1] == "research")
    for _t, act, _failed, paths, whole_file in acts:
        if act == "edit" and paths and not whole_file:
            turn.path_edits += 1
            turn.blind_edits += int(not any(k in known for k in paths))
        if act in ("research", "edit"):
            known.update(paths)
    unpriced = set()
    for model, usage in draft.responses:
        turn.responses += 1
        turn.output += usage.output
        turn.reasoning += usage.reasoning
        cost = pricing.cost_usd(usage, model)
        if cost is not None:
            turn.cost = None if turn.cost is None else turn.cost + cost
        elif usage.total_input() + usage.output:
            turn.cost = None
            unpriced.add(model or "(no model id)")
    return turn, unpriced


def collect_turns(harness, since_days=90, project=None, home=None) -> tuple:
    """Turns from the last `since_days` days, oldest first, and counts for the report's notes.
    Files are picked by modified time, and a recently modified file can hold
    months-old records, so events older than the window are dropped too."""
    info = {"files": 0, "subagent_files": 0, "subagents_attached": 0, "turns_before_window": 0,
            "warnings": 0, "unpriced_models": {}, "prompts_without_time": 0}
    sessions = []
    for h, path in transcripts.find_sessions(harness=harness, since_days=since_days, project=project, home=home):
        s = transcripts.load_session(h, path)
        info["files"] += 1
        info["subagent_files"] += int(s.is_subagent)
        info["warnings"] += len(s.warnings)
        s.events, dropped = _compact(s, _prompt_records(path) if h == "claude-code" and not s.is_subagent else {})
        info["prompts_without_time"] += dropped
        sessions.append(s)
    since = transcripts.cutoff(since_days) if since_days else None
    by_session = {}
    for s, e in transcripts.unique_events(sessions, since=since):  # a forked session's copy counts once
        by_session.setdefault(id(s), (s, []))[1].append(e)
    if since:
        info["turns_before_window"] = sum(1 for _s, e in transcripts.unique_events(sessions)
                                          if e.kind == "user" and e.ts and e.ts[:19] < since[:19])
    drafts, subagents = {}, []
    for s, events in by_session.values():
        if s.is_subagent:
            subagents.append((s, events))
        else:
            drafts[(s.harness, s.id)] = _drafts(s, events)
    for s, events in subagents:  # subagent work belongs to the turn that was running when it started
        parent = drafts.get((s.harness, s.parent_id))
        if not parent or not events:
            continue
        target = parent[0]
        for d in parent:
            if d.prompt.t > events[0].t:
                break
            target = d
        info["subagents_attached"] += 1
        for e in events:
            if e.kind == "tool":
                target.acts.append((e.t, e.act, e.failed, e.paths, e.whole_file))
            elif e.kind == "assistant" and e.usage is not None:
                target.responses.append((e.model, e.usage))
    turns = []
    for session_drafts in drafts.values():
        known = set()
        for d in session_drafts:
            turn, unpriced = _finish(d, known)
            turns.append(turn)
            for model in unpriced:
                info["unpriced_models"][model] = info["unpriced_models"].get(model, 0) + 1
    turns.sort(key=lambda t: t.t)
    return turns, info


# ---------------------------------------------------------------------------
# Statistics (references/statistics.md)
# ---------------------------------------------------------------------------

P_LIMIT = 0.01            # adjusted p value a change must stay under
MIN_CHANGE_PCT = 20.0     # smallest change worth flagging, in the per-session values
MIN_TURNS = 30            # turns each side of an update needs
MIN_SESSIONS = 20         # sessions each side of an update needs
MIN_SESSION_VALUES = 12   # sessions with a value for one metric, each side, before it is tested
MIN_EVENT_SESSIONS = 5    # for a rare event: sessions with the event on the busier side
TWO_WEEKS = 14 * 86400.0  # how far from a model's first use its comparison reaches


def _ranks(values) -> tuple:
    """(average ranks in input order, the tie term: sum of t^3 - t over runs of ties)."""
    order = sorted(range(len(values)), key=lambda k: values[k])
    ranks, tie_term, i = [0.0] * len(values), 0.0, 0
    while i < len(order):
        j = i
        while j + 1 < len(order) and values[order[j + 1]] == values[order[i]]:
            j += 1
        for k in range(i, j + 1):
            ranks[order[k]] = (i + j) / 2.0 + 1  # a run of ties shares the average rank
        tie_term += (j - i + 1) ** 3 - (j - i + 1)
        i = j + 1
    return ranks, tie_term


def mann_whitney(a, b) -> tuple:
    """Two-sided Mann-Whitney U test with the normal approximation, the tie
    correction, and a continuity correction of 0.5 (the defaults of R's
    wilcox.test with exact=FALSE and of scipy's asymptotic method).
    Returns (U of `a`, z, p). z is negative when `a` tends to be smaller."""
    n1, n2 = len(a), len(b)
    if not n1 or not n2:
        return 0.0, 0.0, 1.0
    ranks, tie_term = _ranks(list(a) + list(b))
    u = sum(ranks[:n1]) - n1 * (n1 + 1) / 2.0
    n = n1 + n2
    mean = n1 * n2 / 2.0
    var = n1 * n2 / 12.0 * ((n + 1) - tie_term / (n * (n - 1)))
    if var <= 0:
        return u, 0.0, 1.0
    z = max(abs(u - mean) - 0.5, 0.0) / math.sqrt(var)
    return u, math.copysign(z, u - mean), min(1.0, math.erfc(z / math.sqrt(2)))


def fisher_exact(k1, n1, k2, n2) -> float:
    """Two-sided Fisher exact test for k1 events in n1 against k2 in n2: with
    the margins fixed, the chance of a table no more likely than the one seen.
    Exact integer arithmetic (R's fisher.test gives the same values)."""
    n, k = n1 + n2, k1 + k2
    low, high = max(0, n1 - (n - k)), min(k, n1)
    weights, w = [], math.comb(k, low) * math.comb(n - k, n1 - low)
    for x in range(low, high + 1):  # C(k, x) * C(n - k, n1 - x), one step at a time
        weights.append(w)
        if x < high:
            w = w * (k - x) * (n1 - x) // ((x + 1) * (n - k - n1 + x + 1))
    seen = weights[k1 - low]
    return min(1.0, sum(v for v in weights if v <= seen) / math.comb(n, n1))


def _mode(values) -> tuple:
    counts = {}
    for v in values:
        counts[v] = counts.get(v, 0) + 1
    value = max(counts, key=lambda v: (counts[v], -v))
    return value, counts[value]


def _p_value(a, b) -> tuple:
    """(z, p): Mann-Whitney, and when one value holds more than half the
    sessions, the larger of that p and Fisher's exact test on the sessions away
    from that value against the sessions at it (the normal approximation is too
    small there). For a rare event the common value is 0, so "away" means "had it"."""
    _u, z, p = mann_whitney(a, b)
    mode, count = _mode(list(a) + list(b))
    if 2 * count > len(a) + len(b):
        away_a, away_b = sum(1 for v in a if v != mode), sum(1 for v in b if v != mode)
        p = max(p, fisher_exact(away_a, len(a), away_b, len(b)))
    return z, p


def session_p(a, b) -> tuple:
    """(z, p, best possible p for these values and side sizes) for per-session values."""
    z, p = _p_value(a, b)
    pooled, n = sorted(list(a) + list(b)), len(a)
    best = min(_p_value(pooled[:n], pooled[n:])[1], _p_value(pooled[-n:], pooled[:-n])[1])
    return z, p, best


def session_test(a, b) -> Optional[tuple]:
    """session_p, or None when one value holds more than half the sessions and
    fewer than MIN_EVENT_SESSIONS sessions on the busier side are away from it
    (a rare event seen too seldom to test)."""
    mode, count = _mode(list(a) + list(b))
    if 2 * count > len(a) + len(b):
        if max(sum(1 for v in a if v != mode), sum(1 for v in b if v != mode)) < MIN_EVENT_SESSIONS:
            return None
    return session_p(a, b)


def bh_adjust(pvalues) -> list:
    """Benjamini-Hochberg adjusted p values (R's p.adjust(p, "BH")), in input order."""
    m = len(pvalues)
    order = sorted(range(m), key=lambda k: pvalues[k])
    adjusted, running = [1.0] * m, 1.0
    for rank in range(m, 0, -1):
        k = order[rank - 1]
        running = min(running, pvalues[k] * m / rank)
        adjusted[k] = running
    return adjusted


def tarone(best_ps, alpha=P_LIMIT) -> list:
    """Indices of the tests that can reach significance at all (Tarone, 1990):
    for the smallest K with at most K tests whose best possible p is under
    alpha / K, those tests. The others cannot pass and only dilute the adjustment."""
    for k in range(1, len(best_ps) + 1):
        keep = [i for i, best in enumerate(best_ps) if best < alpha / k]
        if len(keep) <= k:
            return keep
    return []


_SEMVER_RE = re.compile(r"^v?(\d+)(?:\.(\d+))?(?:\.(\d+))?(?:-([0-9A-Za-z.-]+))?(?:\+[0-9A-Za-z.-]*)?$")


def version_key(version) -> tuple:
    """Sort key: semantic-version order (a pre-release sorts before its
    release; numeric parts compare as numbers), other strings after, by name."""
    m = _SEMVER_RE.match(str(version).strip())
    if not m:
        return (1, str(version))
    core = tuple(int(g or 0) for g in m.group(1, 2, 3))
    if m.group(4) is None:
        return (0, core, (1,))
    parts = tuple((0, int(p), "") if p.isdigit() else (1, 0, p) for p in m.group(4).split("."))
    return (0, core, (0, parts))


# ---------------------------------------------------------------------------
# Metrics (references/metrics.md)
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class Metric:
    key: str
    label: str
    turn_summary: str     # the By version table: median | mean | per100 | share (a percent) | ratio
    session_summary: str  # the test and the flagged changes: median or mean of the per-session values
    scale: float          # 100 for rates and shares shown as per 100 or percent
    bad: str              # the direction that usually means worse: up, down, or "" for neither
    phrases: tuple        # less, more, times as much, dropped to zero, rose from zero


METRICS = (
    Metric("reads_before_edit", "Reads before the first edit", "median", "median", 1, "down",
           ("reads {pct}% less before it edits", "reads {pct}% more before it edits",
            "reads {times} as much before it edits", "edits without reading first in most sessions",
            "reads before it edits in most sessions, where before it mostly did not")),
    Metric("reads_per_edit", "Reads per edit", "ratio", "median", 1, "down",
           ("reads {pct}% less per edit", "reads {pct}% more per edit", "reads {times} as much per edit",
            "edits without reading in most sessions",
            "reads before editing in most sessions, where before it mostly did not")),
    Metric("blind_edits", "Edits to files not read earlier in the session (%)", "share", "mean", 100, "up",
           ("makes {pct}% fewer edits to files it had not read", "makes {pct}% more edits to files it had not read",
            "makes {times} as many edits to files it had not read", "no longer edits files it had not read",
            "now edits files it had not read, where before it did not")),
    Metric("edits_per_turn", "Edits per turn", "mean", "median", 1, "",
           ("makes {pct}% fewer edits per turn", "makes {pct}% more edits per turn",
            "makes {times} as many edits per turn", "makes no edits in most sessions",
            "makes edits in most sessions, where before it mostly did not")),
    Metric("tool_error_rate", "Failed tool calls (%)", "share", "mean", 100, "up",
           ("hits {pct}% fewer tool errors", "hits {pct}% more tool errors", "hits tool errors {times} as often",
            "no longer hits tool errors", "now hits tool errors, where before it had none")),
    Metric("interrupts", "Interrupts per 100 turns", "per100", "mean", 100, "up",
           ("gets interrupted {pct}% less often", "gets interrupted {pct}% more often",
            "gets interrupted {times} as often", "no longer gets interrupted",
            "now gets interrupted, where before it never was")),
    Metric("corrections", "Corrections per 100 prompts", "per100", "mean", 100, "up",
           ("needs correcting {pct}% less often", "needs correcting {pct}% more often",
            "needs correcting {times} as often", "no longer needs correcting",
            "now needs correcting, where before it never did")),
    Metric("output_tokens", "Output tokens per turn", "median", "median", 1, "",
           ("writes {pct}% less output per turn", "writes {pct}% more output per turn",
            "writes {times} as much output per turn", "writes no output in most sessions",
            "writes output in most sessions, where before it mostly did not")),
    Metric("reasoning_share", "Share of output spent reasoning (%)", "share", "mean", 100, "down",
           ("spends {pct}% less of its output on reasoning", "spends {pct}% more of its output on reasoning",
            "spends {times} as much of its output on reasoning", "no longer spends output on reasoning",
            "now spends output on reasoning, where before it did not")),
    Metric("tool_calls", "Tool calls per turn", "median", "median", 1, "",
           ("makes {pct}% fewer tool calls per turn", "makes {pct}% more tool calls per turn",
            "makes {times} as many tool calls per turn", "makes no tool calls in most sessions",
            "makes tool calls in most sessions, where before it mostly did not")),
    Metric("cost", "Cost per turn (USD)", "median", "median", 1, "up",
           ("costs {pct}% less per turn", "costs {pct}% more per turn", "costs {times} as much per turn",
            "costs nothing in most sessions", "costs money in most sessions, where before it mostly did not")),
)
METRIC = {m.key: m for m in METRICS}
_TOTALS = {"reads_per_edit": ("reads", "edits"), "blind_edits": ("blind_edits", "path_edits"),
           "tool_error_rate": ("errors", "calls"), "reasoning_share": ("reasoning", "output")}


def _sample(key, t):
    """The turn's value for a per-turn metric, or None when the turn does not count for it."""
    if key == "reads_before_edit":
        return t.reads_before_edit
    if key == "edits_per_turn":
        return t.edits
    if key == "interrupts":
        return t.interrupts
    if key == "corrections":
        return 1 if t.correction else 0
    if key == "output_tokens":
        return t.output if t.responses else None
    if key == "tool_calls":
        return t.calls
    if key == "cost":
        return t.cost if t.responses and t.cost is not None else None
    num, den = _TOTALS[key]
    return getattr(t, num) / float(getattr(t, den)) if getattr(t, den) else None


def samples(key, turns) -> list:
    return [v for v in (_sample(key, t) for t in turns) if v is not None]


def _session_value(key, items):
    if key in _TOTALS:  # a session's own total ratio, not an average of its turns
        num, den = _TOTALS[key]
        total = sum(getattr(t, den) for t in items)
        return sum(getattr(t, num) for t in items) / float(total) if total else None
    values = samples(key, items)
    return sum(values) / float(len(values)) if values else None


def session_values(key, turns) -> list:
    """One value per session, so each session counts once in the test: the mean
    of its turns' values, or for ratios and shares the session's own total ratio."""
    by_session = {}
    for t in turns:
        by_session.setdefault(t.session, []).append(t)
    return [v for v in (_session_value(key, items) for items in by_session.values()) if v is not None]


def session_summary(key, values):
    """The median or mean of per-session values, on the scale the report shows."""
    if not values:
        return None
    m = METRIC[key]
    value = statistics.median(values) if m.session_summary == "median" else sum(values) / float(len(values))
    return value * m.scale


def _reasoning_recorded(turns) -> bool:
    """Claude Code records thinking tokens only for some models and versions: a
    slice counts as recording reasoning when most of its turns with output do."""
    with_output = [t for t in turns if t.output]
    return bool(with_output) and 2 * sum(1 for t in with_output if t.reasoning) >= len(with_output)


def metric_value(key, turns) -> tuple:
    """Turn-level value for the By version table: (value, turns it rests on), or (None, 0)."""
    if key == "reasoning_share" and not _reasoning_recorded(turns):
        return None, 0
    kind = METRIC[key].turn_summary
    if kind in ("ratio", "share"):
        num, den = _TOTALS[key]
        total = sum(getattr(t, den) for t in turns)
        if not total:
            return None, 0
        value = sum(getattr(t, num) for t in turns) / float(total)
        return value * (100.0 if kind == "share" else 1.0), sum(1 for t in turns if getattr(t, den))
    values = samples(key, turns)
    if not values:
        return None, 0
    if kind == "median":
        return statistics.median(values), len(values)
    if kind == "mean":
        return sum(values) / len(values), len(values)
    return 100.0 * sum(values) / len(values), len(values)  # per100


def group_values(turns) -> dict:
    return {m.key: metric_value(m.key, turns) for m in METRICS}


# ---------------------------------------------------------------------------
# Slices and the windows around each update
# ---------------------------------------------------------------------------

@dataclass
class Slice:
    key: str                       # a version, a model id, or an ISO week such as 2026-W38
    turns: list = field(default_factory=list)


@dataclass
class Window:
    i: int                         # the update: the first slice of the after side
    b0: int                        # the before side comes from slices[b0:i]
    a1: int                        # the after side from slices[i:a1]
    before: list                   # [(slice index, turns)]
    after: list


def _week(t) -> str:
    year, week, _day = datetime.datetime.fromtimestamp(t, tz=datetime.timezone.utc).isocalendar()
    return "%d-W%02d" % (year, week)


def week_start(key) -> str:
    """The Monday of an ISO week key such as 2026-W38."""
    year, week = key.split("-W")
    return datetime.date.fromisocalendar(int(year), int(week), 1).isoformat()


def _sessions(turns) -> int:
    return len({t.session for t in turns})


def _enough(turns, min_turns, min_sessions) -> bool:
    return len(turns) >= min_turns and _sessions(turns) >= min_sessions


def _time_chain(slices) -> list:
    """Indices of the versions to keep: the longest run, in version order, whose
    median turn time never falls, counted in versions first and sessions second.
    A version outside it ran later than newer versions: a second install."""
    med = [statistics.median(t.t for t in s.turns) for s in slices]
    size = [_sessions(s.turns) for s in slices]
    best, prev = [(1, size[i]) for i in range(len(slices))], [-1] * len(slices)
    for i in range(len(slices)):
        for j in range(i):
            candidate = (best[j][0] + 1, best[j][1] + size[i])
            if med[j] <= med[i] and candidate > best[i]:
                best[i], prev[i] = candidate, j
    i, chain = max(range(len(slices)), key=lambda k: (best[k], k)), []
    while i != -1:
        chain.append(i)
        i = prev[i]
    return chain[::-1]


def make_slices(turns, by, min_turns=MIN_TURNS, min_sessions=MIN_SESSIONS) -> tuple:
    """Slices in order (versions by number, weeks by date, models by first use)
    and notes: turns with no version or model, versions left out because they
    ran after newer ones, and models left out for having too little data."""
    keyed, first_seen = {}, {}
    notes = {"no_key": 0, "left_out": [], "out_of_order": []}
    for t in turns:
        key = t.version if by == "version" else t.model if by == "model" else _week(t.t)
        if not key:
            notes["no_key"] += 1
            continue
        keyed.setdefault(key, []).append(t)
        first_seen[key] = min(first_seen.get(key, t.t), t.t)
    if by == "version":
        order = sorted(keyed, key=version_key)
    elif by == "model":
        order = sorted(keyed, key=lambda k: (first_seen[k], k))
    else:
        order = sorted(keyed)
    slices = [Slice(k, sorted(keyed[k], key=lambda t: t.t)) for k in order]
    if by == "version" and len(slices) > 1:
        keep = set(_time_chain(slices))
        notes["out_of_order"] = [(s.key, s.turns[0].t, s.turns[-1].t, _sessions(s.turns))
                                 for i, s in enumerate(slices) if i not in keep]
        slices = [s for i, s in enumerate(slices) if i in keep]
    if by == "model":
        notes["left_out"] = [(s.key, len(s.turns), _sessions(s.turns)) for s in slices
                             if not _enough(s.turns, min_turns, min_sessions)]
        slices = [s for s in slices if _enough(s.turns, min_turns, min_sessions)]
    return slices, notes


def windows(slices, by, min_turns=MIN_TURNS, min_sessions=MIN_SESSIONS) -> list:
    """A Window for each update with enough data around it. Versions and weeks
    borrow their nearest neighbors until a side has min_turns turns from
    min_sessions sessions. A model is compared with the model before it, using
    only the two weeks before and after its first use."""
    out = []
    for i in range(1, len(slices)):
        if by == "model":
            start = slices[i].turns[0].t
            before = [t for t in slices[i - 1].turns if start - TWO_WEEKS <= t.t < start]
            after = [t for t in slices[i].turns if t.t < start + TWO_WEEKS]
            if _enough(before, min_turns, min_sessions) and _enough(after, min_turns, min_sessions):
                out.append(Window(i, i - 1, i + 1, [(i - 1, before)], [(i, after)]))
            continue
        before, pooled_b, b0 = [], [], i
        while b0 > 0 and not _enough(pooled_b, min_turns, min_sessions):
            b0 -= 1
            before.append((b0, slices[b0].turns))
            pooled_b += slices[b0].turns
        after, pooled_a, a1 = [], [], i
        while a1 < len(slices) and not _enough(pooled_a, min_turns, min_sessions):
            after.append((a1, slices[a1].turns))
            pooled_a += slices[a1].turns
            a1 += 1
        if _enough(pooled_b, min_turns, min_sessions) and _enough(pooled_a, min_turns, min_sessions):
            out.append(Window(i, b0, a1, before[::-1], after))
    return out


# ---------------------------------------------------------------------------
# Comparisons (references/statistics.md)
# ---------------------------------------------------------------------------

@dataclass
class Comparison:
    metric: str
    update: int                    # index of the first slice after the update
    b0: int                        # the before side is slices[b0:update]
    a1: int                        # the after side is slices[update:a1]
    before_value: float            # the per-session summary each side, on the scale shown
    after_value: float
    n_before: int                  # turns on each side
    n_after: int
    sessions_before: int           # sessions with a value: the units of the test
    sessions_after: int
    z: float
    p: float
    best_p: float = 0.0            # the smallest p these values could give
    in_adjustment: bool = True     # False: it could not reach significance (Tarone)
    group: int = -1                # windows of one metric and direction that overlap share a group
    representative: bool = False   # the group's smallest p, the one the adjustment counts
    p_adjusted: float = 1.0        # the group's Benjamini-Hochberg adjusted p
    passes: bool = False           # passes every rule
    same_as: Optional[int] = None  # the update named for this change instead
    flagged: bool = False
    stand_out: bool = False        # part of a slice that differs from both neighbors

    @property
    def change(self) -> tuple:
        """(percent change, from_zero). The percent is None when the change starts from zero."""
        return _change(self.before_value, self.after_value)


def _change(before, after) -> tuple:
    if before == 0:
        return (0.0, False) if after == 0 else (None, True)
    return (after - before) / abs(before) * 100.0, False


def _rising(before, after) -> bool:
    return after > before


def _way(c) -> str:
    return "up" if c.z < 0 else "down" if c.z > 0 else ""


def _passes(c) -> bool:
    """A change of MIN_CHANGE_PCT or more, p under P_LIMIT, and the test
    pointing the same way as the change (z < 0: the after side is higher)."""
    pct, from_zero = c.change
    big = from_zero or abs(pct) >= MIN_CHANGE_PCT
    return c.p < P_LIMIT and big and c.z != 0 and (c.z < 0) == _rising(c.before_value, c.after_value)


def _group(pool) -> list:
    """Overlapping windows of one metric pointing the same way test one change:
    each group is the smallest-p window left plus every window whose update
    lies inside its span."""
    groups, left = [], sorted(pool, key=lambda c: (c.p, c.update))
    while left:
        seed = left.pop(0)
        members = [seed] + [c for c in left if _way(seed) and c.metric == seed.metric and _way(c) == _way(seed)
                            and seed.b0 < c.update < seed.a1]
        taken = {id(c) for c in members}
        left = [c for c in left if id(c) not in taken]
        groups.append(members)
    return groups


def compare(slices, wins) -> tuple:
    """Test every metric at every window, each session counting once. Returns
    (comparisons, counts for the notes)."""
    recorded = [_reasoning_recorded(s.turns) for s in slices]
    size = [_sessions(s.turns) for s in slices]
    out, sparse = [], 0
    for w in wins:
        for m in METRICS:
            before, after = w.before, w.after
            if m.key == "reasoning_share":  # only slices that record reasoning, or the rest count as 0%
                before = [(j, ts) for j, ts in before if recorded[j]]
                after = [(j, ts) for j, ts in after if recorded[j]]
            tb = [t for _j, ts in before for t in ts]
            ta = [t for _j, ts in after for t in ts]
            sb, sa = session_values(m.key, tb), session_values(m.key, ta)
            if len(sb) < MIN_SESSION_VALUES or len(sa) < MIN_SESSION_VALUES or len(set(sb + sa)) == 1:
                continue  # too few sessions, or every session has the same value
            tested = session_test(sb, sa)
            if tested is None:
                sparse += 1
                continue
            z, p, best = tested
            out.append(Comparison(m.key, w.i, w.b0, w.a1, session_summary(m.key, sb), session_summary(m.key, sa),
                                  len(tb), len(ta), len(sb), len(sa), z, p, best))
    testable = set(tarone([c.best_p for c in out]))
    for k, c in enumerate(out):
        c.in_adjustment = k in testable
    groups = _group([c for c in out if c.in_adjustment])
    for gid, (members, q) in enumerate(zip(groups, bh_adjust([g[0].p for g in groups]))):
        members[0].representative = True
        for c in members:
            c.group, c.p_adjusted = gid, q
        passing = [c for c in members if _passes(c)] if q < P_LIMIT else []
        if passing:  # name the window whose own boundary is best supported, then the smallest p
            named = max(passing, key=lambda c: (min(size[c.update - 1], size[c.update]), -c.p, -c.update))
            named.flagged = True
            for c in passing:
                c.passes = True
                c.same_as = None if c is named else named.update
    return out, {"sparse": sparse, "untestable": len(out) - len(testable)}


def _stand_outs(by, slices, comparisons) -> list:
    """A flag at one update followed, within its after side, by the opposite
    flag on the same metric: the slices between differ from both neighbors.
    Report them once as standing out, and headline neither flag."""
    flags = sorted((c for c in comparisons if c.flagged), key=lambda c: c.update)
    out = []
    for f in flags:
        for g in flags:
            if (not f.stand_out and not g.stand_out and g.metric == f.metric and f.update < g.update <= f.a1
                    and _way(g) != _way(f)):
                f.stand_out = g.stand_out = True
                f.flagged = g.flagged = False
                keys = [clean_key(s.key) for s in slices[f.update:g.update]]
                span = keys[0] if len(keys) == 1 else "%s to %s" % (keys[0], keys[-1])
                one, many = UNITS[by]
                if by == "week":
                    who = "The week of %s stands" % week_start(slices[f.update].key) if len(keys) == 1 else \
                        "The weeks %s stand" % span
                else:
                    who = "%s %s stands" % (one.capitalize(), _code_keys(span)) if len(keys) == 1 else \
                        "%s %s stand" % (many.capitalize(), _code_keys(span))
                around = "the %s around %s" % (many, "it" if len(keys) == 1 else "them")
                out.append({"metric": f.metric, "versions": span, "text": "%s out from %s: %s %s before, %s there, "
                            "%s after." % (who, around, METRIC[f.metric].label.lower(), _fmt(f.metric, f.before_value),
                                           _fmt(f.metric, f.after_value), _fmt(g.metric, g.after_value))})
    return out


# ---------------------------------------------------------------------------
# The result: headline, slices, comparisons
# ---------------------------------------------------------------------------

HARNESS_NAMES = {"claude-code": "Claude Code", "codex": "Codex", "gemini-cli": "Gemini CLI", "opencode": "OpenCode"}
UNITS = {"version": ("version", "versions"), "model": ("model", "models"), "week": ("week", "weeks")}
_KEY_RE = re.compile(r"[^A-Za-z0-9._:@/+-]")


def clean_key(text) -> str:
    """A version or model id as plain characters only, secrets masked first:
    nothing in it can carry markup, a table break, or an instruction."""
    return _KEY_RE.sub("", transcripts.redact(str(text)))[:80] or "(unnamed)"


def _k(text) -> str:
    """A version or model id for markdown text: plain characters inside inline
    code, so a key such as a bare URL does not render as a link."""
    return code(clean_key(text))


_JOINS = ("after", "between", "and", "to", "vs")


def _code_keys(text) -> str:
    """A place or span made of keys ("between 2.1.260 and 2.1.270") for
    markdown: each key in inline code, the joining words plain. Keys hold no
    spaces, so a split on spaces finds them."""
    return " ".join(w if w in _JOINS else code(w) for w in text.split(" "))


def _safe(text, limit=80) -> str:
    """Untrusted text such as a folder name as one inert line."""
    return safe_text(text, limit=limit)


def _home(path) -> str:
    home = os.path.expanduser("~")
    if path and (path == home or path.startswith(home + os.sep)):
        return "~" + path[len(home):]
    return path


def _folder(path) -> str:
    """A folder for the markdown report: shortened, made inert, inside inline code."""
    return code(_home(path), 160)


def _num(value):
    if value is None or isinstance(value, int):
        return value
    return float("%.6g" % value)


def _day(t) -> str:
    return datetime.datetime.fromtimestamp(t, tz=datetime.timezone.utc).strftime("%Y-%m-%d")


def _span(slices, first, stop) -> str:
    keys = [clean_key(s.key) for s in slices[first:stop]]
    return keys[0] if len(keys) == 1 else "%s to %s" % (keys[0], keys[-1])


def _pct_int(pct) -> int:
    """One whole percent, rounded half away from zero, for the phrase and the table alike."""
    return int(math.copysign(math.floor(abs(pct) + 0.5), pct))


def _times(ratio) -> str:
    if 1.95 <= ratio < 2.05:
        return "twice"
    return ("%.1f" % ratio).rstrip("0").rstrip(".") + " times"


def phrase(c) -> str:
    """What the agent does differently, as a verb phrase ("reads 41% less before it edits")."""
    less, more, times, to_zero, from_zero = METRIC[c.metric].phrases
    pct, starts_at_zero = c.change
    if starts_at_zero:
        return from_zero
    if c.after_value == 0:
        return to_zero
    if pct >= 100:
        return times.format(times=_times(c.after_value / c.before_value))
    return (less if pct < 0 else more).format(pct=abs(_pct_int(pct)))


def _usually(c) -> str:
    bad = METRIC[c.metric].bad
    if not bad:
        return "no clear direction"
    return "worse" if ("up" if _rising(c.before_value, c.after_value) else "down") == bad else "better"


def _where(by, slices, c) -> str:
    """Where the change sits: after one slice, or between two when the window
    borrowed neighbors (or its own slice is too small to name alone)."""
    key = clean_key(slices[c.update].key)
    if by == "model":
        return "%s vs %s" % (key, clean_key(slices[c.update - 1].key))
    borrowed = c.update - c.b0 > 1 or c.a1 - c.update > 1
    if (borrowed or _sessions(slices[c.update].turns) < 3) and c.a1 - 1 > c.b0 + 1:
        return "between %s and %s" % (clean_key(slices[c.b0 + 1].key), clean_key(slices[c.a1 - 1].key))
    return "after %s" % key


def _comparison_dict(c, slices, by="version") -> dict:
    pct, from_zero = c.change
    return {
        "metric": c.metric, "label": METRIC[c.metric].label, "update": clean_key(slices[c.update].key),
        "where": _where(by, slices, c), "before": _span(slices, c.b0, c.update),
        "after": _span(slices, c.update, c.a1), "measured": "%s per session" % METRIC[c.metric].session_summary,
        "before_value": _num(c.before_value), "after_value": _num(c.after_value),
        "change_pct": None if pct is None else _pct_int(pct), "from_zero": from_zero,
        "direction": "up" if _rising(c.before_value, c.after_value) else "down", "usually": _usually(c),
        "phrase": phrase(c), "p": _num(c.p), "best_p": _num(c.best_p), "in_adjustment": c.in_adjustment,
        "group": c.group, "representative": c.representative, "p_adjusted": _num(c.p_adjusted),
        "n_before": c.n_before, "n_after": c.n_after,
        "sessions_before": c.sessions_before, "sessions_after": c.sessions_after,
        "passes_test": c.passes, "flagged": c.flagged, "stand_out": c.stand_out,
        "same_change_as": None if c.same_as is None else clean_key(slices[c.same_as].key),
    }


def _lead(by, name, slices, c) -> str:
    where = _where(by, slices, c)
    if by == "model":
        return "On %s, compared with %s" % (_k(slices[c.update].key), _k(slices[c.update - 1].key))
    if by == "version":
        if where.startswith("between"):
            return "After an update %s" % _code_keys(where).replace("between ", "between %s " % name, 1)
        return "After %s %s" % (name, _k(slices[c.update].key))
    if where.startswith("between"):
        return "After a change between the weeks of %s and %s" % (week_start(slices[c.b0 + 1].key),
                                                                   week_start(slices[c.a1 - 1].key))
    return "Since the week of %s" % week_start(slices[c.update].key)


def _window_phrase(since_days) -> str:
    return " in the last %d days" % since_days if since_days else ""


_FAMILY = {"reads_before_edit": "reading", "reads_per_edit": "reading"}
_ALSO_CHANGED = (("model", "the model"), ("version", "the harness version"), ("project_mix", "the projects"),
                 ("work", "the kind of work"), ("entrypoint", "the share of scripted runs"))


def _and(words) -> str:
    return words[0] if len(words) == 1 else "%s and %s" % (", ".join(words[:-1]), words[-1])


def _headline(by, harness, turns, slices, wins, flagged, stand_outs, info, since_days, min_sessions, confounders,
              slice_notes, project) -> str:
    name = HARNESS_NAMES.get(harness, harness)
    one, many = UNITS[by]
    where = " in %s" % _folder(project) if project else ""
    if not turns:
        if info and info.get("files"):
            return "No typed prompts found in {:,} {} session files{}{}.".format(
                info["files"], name, where, _window_phrase(since_days))
        text = "No %s turns found%s%s." % (name, where, _window_phrase(since_days))
        if project:
            text += (" Check the --project path: it must be the folder the sessions ran in, or a folder that "
                     "contains it.")
        return text
    if slice_notes["no_key"] == len(turns):
        others = [u for u in ("version", "model", "week") if u != by]
        return "%s records no %s, so there is nothing to split by %s. Run again with --by %s or --by %s." % (
            name, one, one, others[0], others[1])
    if by == "model" and len(slices) < 2 and slice_notes["left_out"]:
        return ("Not enough history to compare models yet: {:,} {} turns from {:,} sessions, and each model needs "
                "{} sessions.".format(len(turns), name, _sessions(turns), min_sessions))
    if len(slices) == 1:
        place = "in one week" if by == "week" else "on one %s" % one
        return "All {:,} {} turns{} ran {} ({}), so there is no update to test yet.".format(
            len(turns), name, _window_phrase(since_days), place, _k(slices[0].key))
    if not wins and by == "model":
        return ("Not enough history to compare models yet: {:,} {} turns from {:,} sessions across {} models, and "
                "each model needs {} sessions within two weeks of the switch.".format(
                    len(turns), name, _sessions(turns), len(slices), min_sessions))
    if not wins:
        return ("Not enough history to test an update yet: {:,} {} turns from {:,} sessions across {} {}, and each "
                "side of an update needs {} sessions.".format(len(turns), name, _sessions(turns), len(slices),
                                                              many, min_sessions))
    if not flagged:
        if stand_outs:
            return ("No lasting behavior change passed the test across {} {} {} and {:,} turns; {} {} out from "
                    "the ones around {}.".format(len(slices), name, many, len(turns),
                                                 _count(len(stand_outs), one),
                                                 "stands" if len(stand_outs) == 1 else "stand",
                                                 "it" if len(stand_outs) == 1 else "them"))
        return "No behavior change passed the test across {} {} {} and {:,} turns.".format(
            len(slices), name, many, len(turns))
    counts = {}
    for c in flagged:
        counts[c.update] = counts.get(c.update, 0) + 1
    update = max(counts, key=lambda i: (counts[i], i))
    order = [m.key for m in METRICS]  # the metrics in order of how much they say about quality
    here, families = [], set()
    for c in sorted((c for c in flagged if c.update == update),
                    key=lambda c: (_usually(c) != "worse", order.index(c.metric))):
        family = _FAMILY.get(c.metric, c.metric)  # two reading measures would say the same thing twice
        if family not in families:
            families.add(family)
            here.append(c)
    text = "%s, your agent %s" % (_lead(by, name, slices, here[0]), " and ".join(phrase(c) for c in here[:2]))
    kinds = {c["kind"] for c in confounders if c["update"] == clean_key(slices[update].key)}
    extra = []
    also = [words for kind, words in _ALSO_CHANGED if kind in kinds]
    if also:
        extra.append("%s changed at the same point" % _and(also))
    if "overlap" in kinds:
        extra.append("both sides were in use at the same time")
    if "concurrent" in kinds:
        extra.append("both models were in use at the same time")
    text += ", but %s." % " and ".join(extra) if extra else "."
    rest = len(flagged) - min(2, len(here))
    if rest:
        text += " %d more flagged change%s listed below." % (rest, "s are" if rest > 1 else " is")
    return text


# ---------------------------------------------------------------------------
# Confounders and notes: what else changed, and what was left out
# ---------------------------------------------------------------------------

def _pct(share) -> str:
    return "%d%%" % int(math.floor(100 * share + 0.5))


def _shares(turns, attr) -> list:
    """[(value, share of turns)], largest first."""
    counts = {}
    for t in turns:
        key = getattr(t, attr) or "(unknown)"
        counts[key] = counts.get(key, 0) + 1
    return sorted(((k, n / float(len(turns))) for k, n in counts.items()), key=lambda x: (-x[1], x[0]))


def _count(n, word) -> str:
    return "{:,} {}{}".format(n, word, "" if n == 1 else "s")


def _session_lengths(turns) -> list:
    counts = {}
    for t in turns:
        counts[t.session] = counts.get(t.session, 0) + 1
    return list(counts.values())


def _turns_word(m) -> str:
    m = int(m) if m == int(m) else round(m, 1)
    return "%s turn%s" % (m, "" if m == 1 else "s")


def _confounders(by, slices, flagged, wins, filtered_by_project) -> list:
    """For each update with a flagged change: the other things that changed at the same point."""
    out, by_update = [], {w.i: w for w in wins}
    mixes = {"version": [("model", "model", "--by model")],
             "model": [("version", "harness version", "--by version")],
             "week": [("model", "model", "--by model"), ("version", "harness version", "--by version")]}[by]
    for update in sorted({c.update for c in flagged}):
        c = next(c for c in flagged if c.update == update)
        w = by_update[update]
        before = [t for _j, ts in w.before for t in ts]
        after = [t for _j, ts in w.after for t in ts]
        key, where = clean_key(slices[update].key), _where(by, slices, c)

        def add(kind, text, **extra):
            out.append(dict({"update": key, "where": where, "kind": kind, "text": text}, **extra))

        for attr, word, flag in mixes:
            sb, sa = _shares(before, attr), _shares(after, attr)
            if sb[0][0] != sa[0][0] or abs(sa[0][1] - dict(sb).get(sa[0][0], 0.0)) >= 0.3:
                add(attr, "The %s mix changed at the same point: before, %s ran %s of turns; after, %s ran %s. Run "
                    "again with %s to separate the two." % (word, _k(sb[0][0]), _pct(sb[0][1]),
                                                            _k(sa[0][0]), _pct(sa[0][1]), flag))
        if not filtered_by_project:
            pb, pa = dict(_shares(before, "project")), dict(_shares(after, "project"))
            common = set(pb) & set(pa)
            shared = min((sum(v for k, v in pa.items() if k in common), "after"),
                         (sum(v for k, v in pb.items() if k in common), "before"))
            if shared[0] < 0.5 and common:
                best = max(common, key=lambda k: (min(pb[k], pa[k]), k))
                add("project_mix", "The work moved between projects: only %s of the turns %s come from projects that "
                    "appear on both sides. To compare like with like, run again with `--project %s`." % (
                        _pct(shared[0]), shared[1], shlex.quote(_safe(_home(best), 160))),
                    project=_safe(_home(best), 500))
            elif shared[0] < 0.5:
                add("project_mix", "The two sides share no project, so the change may come from the work, not the "
                    "update.")
            elif len(set(pb) | set(pa)) > 1:
                top, side = max((max(pb.items(), key=lambda x: x[1]), "before"),
                                (max(pa.items(), key=lambda x: x[1]), "after"), key=lambda x: x[0][1])
                if top[1] >= 0.6:
                    add("one_project", "One project, %s, holds %s of the turns %s. Run again with `--project %s` to "
                        "check the change within that project alone." % (_folder(top[0]), _pct(top[1]), side,
                                                                        shlex.quote(_safe(_home(top[0]), 160))),
                        project=_safe(_home(top[0]), 500))
        lb, la = _session_lengths(before), _session_lengths(after)
        mb, ma = statistics.median(lb), statistics.median(la)
        ob, oa = sum(1 for n in lb if n == 1) / float(len(lb)), sum(1 for n in la if n == 1) / float(len(la))
        if max(mb, ma) >= 2 * min(mb, ma) or abs(oa - ob) >= 0.25:
            add("work", "Sessions changed shape: the median session had %s before and %s after, and %s of sessions "
                "had one turn before against %s after. That often means a different kind of work, such as scripted "
                "runs instead of long conversations." % (_turns_word(mb), _turns_word(ma), _pct(ob), _pct(oa)))
        if max(t.t for t in before) > min(t.t for t in after) + 86400:
            add("overlap", "Both sides were in use at the same time: the before side ran until %s, after the after "
                "side began on %s. That usually means two installs, so the difference may come from what each was "
                "used for." % (_day(max(t.t for t in before)), _day(min(t.t for t in after))))
        if any(t.entrypoint for t in before + after):
            sdk_b = sum(1 for t in before if t.entrypoint.startswith("sdk-")) / float(len(before))
            sdk_a = sum(1 for t in after if t.entrypoint.startswith("sdk-")) / float(len(after))
            if abs(sdk_a - sdk_b) >= 0.25:
                add("entrypoint", "The share of scripted runs changed: %s of turns before came from the SDK and %s "
                    "after." % (_pct(sdk_b), _pct(sdk_a)))
        if by == "model":
            start = slices[update].turns[0].t
            old = sum(1 for t in slices[update - 1].turns if start <= t.t < start + TWO_WEEKS)
            if old and old >= 0.3 * (old + len(after)):
                add("concurrent", "Both models were in use at the same time, so the difference may come from the "
                    "tasks each was used for. In the two weeks after %s first ran, %s still ran %s of the turns of "
                    "the two." % (_k(key), _k(slices[update - 1].key), _pct(old / float(old + len(after)))))
    return out


_RETENTION = {
    "claude-code": "Claude Code deletes transcripts older than cleanupPeriodDays (30 days by default); set it "
                   "higher in ~/.claude/settings.json to keep more history for this check.",
    "gemini-cli": "Gemini CLI deletes sessions older than general.sessionRetention.maxAge (30 days by default).",
}


def _names(keys) -> str:
    return _and([_k(k) for k in keys])


def _notes(by, harness, turns, slices, slice_notes, wins, comparisons, counts, info, since_days, min_turns,
           min_sessions, untested) -> list:
    notes = []
    if info:
        text = "Read %s" % _count(info.get("files", 0), "session file")
        if info.get("subagent_files"):
            text += "; %d of the %d subagent files are counted in the turn that started them" % (
                info.get("subagents_attached", 0), info["subagent_files"])
        notes.append(text + ".")
        if info.get("turns_before_window"):
            notes.append("Left out %s older than the %d-day window." % (
                _count(info["turns_before_window"], "turn"), since_days or 0))
        if info.get("prompts_without_time"):
            notes.append("Left out %s with no readable time." % _count(info["prompts_without_time"], "prompt"))
        if info.get("unpriced_models"):
            notes.append("Cost per turn leaves out %s that used models without a known price: %s." % (
                _count(sum(info["unpriced_models"].values()), "turn"), _names(sorted(info["unpriced_models"]))))
        if info.get("warnings"):
            notes.append("Skipped %s." % _count(info["warnings"], "unreadable line"))
    if slice_notes["no_key"] and slice_notes["no_key"] < len(turns):
        notes.append("Left out %s with no recorded %s." % (_count(slice_notes["no_key"], "turn"), UNITS[by][0]))
    rows = slice_notes["out_of_order"]
    if rows:
        one = len(rows) == 1
        notes.append("Left out %s: %s ran %s to %s, after newer versions, which usually means a second install "
                     "such as the desktop app or an SDK script. Run with --by week to see %s in time order." % (
                         _names([r[0] for r in rows]), "it" if one else "they", _day(min(r[1] for r in rows)),
                         _day(max(r[2] for r in rows)), "it" if one else "them"))
    if slice_notes["left_out"]:
        notes.append("Left out models with too little data (each needs %d turns from %d sessions): %s." % (
            min_turns, min_sessions, ", ".join("%s (%s, %s)" % (_k(k), _count(n, "turn"), _count(s, "session"))
                                               for k, n, s in slice_notes["left_out"])))
    if untested and wins:
        notes.append("Not tested: %s: each side of %s needs %d sessions and %d turns, even with neighbors "
                     "added." % (", ".join("%s (%s)" % (code(u["update"]), _count(u["sessions"], "session"))
                                           for u in untested),
                                 "a model change" if by == "model" else "an update", min_sessions, min_turns))
    if counts["untestable"]:
        notes.append("%s could not reach p under %g with this many sessions and %s left out of the adjustment "
                     "for the number of tests." % (_count(counts["untestable"], "comparison"), P_LIMIT,
                                                   "was" if counts["untestable"] == 1 else "were"))
    if counts["sparse"]:
        notes.append("%s of rare events %s not tested: the busier side had fewer than %d sessions with the "
                     "event." % (_count(counts["sparse"], "comparison"), "was" if counts["sparse"] == 1 else "were",
                                 MIN_EVENT_SESSIONS))
    same = sum(1 for c in comparisons if c.passes and not c.flagged and not c.stand_out)
    if same:
        notes.append("%s showed the same changes from a neighboring update; each change is listed once." % (
            _count(same, "more window")))
    silent = [s.key for s in slices if not _reasoning_recorded(s.turns)]
    if silent and len(silent) == len(slices):
        notes.append("No reasoning tokens are recorded here, so the reasoning share is left out.")
    elif silent:
        notes.append("The reasoning share is left out for %s: most of their turns record no reasoning tokens." % (
            _names(silent[:8]) + (" and %d more" % (len(silent) - 8) if len(silent) > 8 else "")))
    if turns and since_days:
        days_back = (time.time() - min(t.t for t in turns)) / 86400.0
        if days_back < since_days - 7:
            text = "The oldest turn found is %d days old, though the window is %d days." % (int(days_back), since_days)
            if days_back <= 35 and harness in _RETENTION:  # a history that fits a 30-day cleanup
                text += " " + _RETENTION[harness]
            notes.append(text)
    return notes


def evaluate(turns, by="version", min_turns=MIN_TURNS, min_sessions=MIN_SESSIONS, harness=None, info=None,
             since_days=None, project=None) -> dict:
    """Slice the turns, test each update window, and build the result (the --json shape)."""
    harness = harness or (turns[0].harness if turns else "claude-code")
    slices, slice_notes = make_slices(turns, by, min_turns, min_sessions)
    wins = windows(slices, by, min_turns, min_sessions)
    comparisons, counts = compare(slices, wins)
    stand_outs = _stand_outs(by, slices, comparisons)
    flagged = [c for c in comparisons if c.flagged]
    confounders = _confounders(by, slices, flagged, wins, bool(project))
    tested = {w.i for w in wins}
    untested = [{"update": clean_key(slices[i].key), "sessions": _sessions(slices[i].turns)}
                for i in range(1, len(slices)) if i not in tested]
    recorded = [_reasoning_recorded(s.turns) for s in slices]
    result = {
        "harness": harness, "by": by, "since_days": since_days,
        "project": _safe(_home(project), 200) if project else None,
        "headline": _headline(by, harness, turns, slices, wins, flagged, stand_outs, info, since_days, min_sessions,
                              confounders, slice_notes, project),
        "totals": {"turns": len(turns), "sessions": _sessions(turns), "slices": len(slices),
                   "updates_tested": len(wins)},
        "thresholds": {"p_adjusted_below": P_LIMIT, "min_change_pct": MIN_CHANGE_PCT, "min_turns": min_turns,
                       "min_sessions": min_sessions, "min_session_values": MIN_SESSION_VALUES,
                       "min_event_sessions": MIN_EVENT_SESSIONS},
        "slices": [], "tests": [_comparison_dict(c, slices, by) for c in comparisons],
        "flagged": [_comparison_dict(c, slices, by) for c in flagged], "stand_outs": stand_outs,
        "confounders": confounders, "untested": untested,
        "left_out": {"versions": [{"version": clean_key(k), "first_day": _day(a), "last_day": _day(b), "sessions": n}
                                  for k, a, b, n in slice_notes["out_of_order"]],
                     "models": [{"model": clean_key(k), "turns": n, "sessions": s}
                                for k, n, s in slice_notes["left_out"]]},
        "notes": _notes(by, harness, turns, slices, slice_notes, wins, comparisons, counts, info, since_days,
                        min_turns, min_sessions, untested),
        "svg": None,
    }
    for j, sl in enumerate(slices):
        result["slices"].append({
            "key": clean_key(sl.key), "first_day": _day(sl.turns[0].t), "last_day": _day(sl.turns[-1].t),
            "sessions": _sessions(sl.turns), "turns": len(sl.turns),
            "values": {k: {"value": _num(v), "n": n} for k, (v, n) in group_values(sl.turns).items()},
            "per_session": {m.key: None if m.key == "reasoning_share" and not recorded[j]
                            else _num(session_summary(m.key, session_values(m.key, sl.turns))) for m in METRICS},
        })
    return result


def analyze(harness="claude-code", by="version", since_days=90, project=None, min_turns=MIN_TURNS,
            min_sessions=MIN_SESSIONS, home=None) -> dict:
    """Read the sessions and evaluate them: the whole run behind the report."""
    turns, info = collect_turns(harness, since_days=since_days, project=project, home=home)
    return evaluate(turns, by, min_turns, min_sessions, harness=harness, info=info, since_days=since_days,
                    project=project)


# ---------------------------------------------------------------------------
# Markdown report
# ---------------------------------------------------------------------------

SHORT = {"reads_before_edit": "Reads before edit", "reads_per_edit": "Reads per edit",
         "blind_edits": "Edits to unread files %", "edits_per_turn": "Edits/turn", "tool_error_rate": "Tool errors %",
         "interrupts": "Interrupts/100", "corrections": "Corrections/100", "output_tokens": "Output tokens",
         "reasoning_share": "Reasoning %", "tool_calls": "Tool calls", "cost": "Cost/turn"}
CHANGES = {"version": "update", "model": "model change", "week": "week-to-week change"}


def _fmt(key, value) -> str:
    if value is None:
        return "n/a"
    if key == "cost":
        return "$%.3f" % value if value >= 0.001 else "$%.4f" % value
    if key == "output_tokens":
        return "{:,}".format(int(round(value)))
    if METRIC[key].scale == 100 or key == "edits_per_turn":
        return "%.1f" % value if METRIC[key].scale == 100 else "%.2f" % value
    return "%d" % value if value == int(value) else "%.1f" % value


def _fmt_change(c) -> str:
    return "from 0" if c["from_zero"] else "%+d%%" % c["change_pct"]


def _fmt_p(p) -> str:
    return "< 0.0001" if p < 0.0001 else "%.4f" % p


def _days(first, last) -> str:
    return first if first == last else "%s to %s" % (first, last)


def render_markdown(result) -> str:
    by, name = result["by"], HARNESS_NAMES.get(result["harness"], result["harness"])
    lines = ["**%s**" % result["headline"], ""]
    totals = result["totals"]
    if totals["turns"]:
        where = " in %s" % code(result["project"], 200) if result["project"] else ""
        window = ", last %d days" % result["since_days"] if result["since_days"] else ""
        text = "%s%s%s, split by %s: %s in %s across %d %s, with %s tested." % (
            name, where, window, by, _count(totals["turns"], "turn"), _count(totals["sessions"], "session"),
            totals["slices"], UNITS[by][0] if totals["slices"] == 1 else UNITS[by][1],
            _count(totals["updates_tested"], CHANGES[by]))
        if result["tests"]:
            text += (" Each session counts once: the test compares one value per session, and a change is flagged "
                     "when p is under %g after adjusting for the number of tests and the change is at least %d%%."
                     % (P_LIMIT, int(MIN_CHANGE_PCT)))
        lines += [text, ""]
    if result["flagged"]:
        lines += ["## Flagged changes", "",
                  "| Where | What changed | Before (per session) | After (per session) | Change | Usually means "
                  "| Compared | Sessions (before, after) | Adjusted p |",
                  "|---|---|---|---|---|---|---|---|---|"]
        for c in result["flagged"]:
            lines.append("| %s | %s (session %s) | %s | %s | %s | %s | %s vs %s | %d, %d | %s |" % (
                _code_keys(c["where"]), c["label"], c["measured"].split()[0], _fmt(c["metric"], c["before_value"]),
                _fmt(c["metric"], c["after_value"]), _fmt_change(c), c["usually"], _code_keys(c["before"]),
                _code_keys(c["after"]),
                c["sessions_before"], c["sessions_after"], _fmt_p(c["p_adjusted"])))
        lines.append("")
    if result["stand_outs"]:
        lines += ["## Stands out", ""] + ["- %s" % s["text"] for s in result["stand_outs"]] + [""]
    if result["confounders"]:
        lines += ["## What else changed at the same point", "",
                  "These numbers show what changed, not why: the kind of work may have changed too.", ""]
        lines += ["- %s: %s" % (_code_keys(c["where"]), c["text"]) for c in result["confounders"]]
        lines.append("")
    if result["slices"]:
        first = UNITS[by][0].capitalize()
        lines += ["## By %s" % by, "",
                  "| %s | Days | Sessions | Turns | %s |" % (first, " | ".join(SHORT[m.key] for m in METRICS)),
                  "|---|---|---|---|%s" % ("---|" * len(METRICS))]
        for sl in result["slices"]:
            lines.append("| %s | %s | %d | %d | %s |" % (
                code(sl["key"]), _days(sl["first_day"], sl["last_day"]), sl["sessions"], sl["turns"],
                " | ".join(_fmt(m.key, sl["values"][m.key]["value"]) for m in METRICS)))
        lines.append("")
    if result["notes"]:
        lines += ["## Notes", ""] + ["- " + n for n in result["notes"]] + [""]
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# SVG: one small line chart per flagged metric, no dependencies
# ---------------------------------------------------------------------------

_INK, _LINE, _FLAG = "#767676", "#4e79a7", "#d9730d"  # mid tones that read on light and dark backgrounds


def _attr(text) -> str:
    return escape(str(text), {'"': "&quot;"})


def render_svg(result) -> Optional[str]:
    """The chart as SVG text, or None when nothing is flagged. It plots the same
    per-session values the flagged changes are measured in."""
    if not result["flagged"]:
        return None
    order = [m.key for m in METRICS]
    metrics = sorted({c["metric"] for c in result["flagged"]}, key=order.index)
    slices = result["slices"]
    width, height, top = 560, 170, 36
    left, right = 64, width - 24
    out = ['<svg xmlns="http://www.w3.org/2000/svg" width="%d" height="%d" viewBox="0 0 %d %d" role="img" '
           'font-family="system-ui, -apple-system, Segoe UI, sans-serif" font-size="11">' % (
               width, top + height * len(metrics), width, top + height * len(metrics)),
           "<title>%s</title>" % escape(result["headline"].replace("`", "")),  # no inline-code marks in a chart
           '<text x="12" y="22" fill="%s" font-size="13">%s by %s, one value per session</text>' % (
               _INK, escape(HARNESS_NAMES.get(result["harness"], result["harness"])), escape(result["by"]))]
    n = len(slices)

    def x_at(i):
        return left + (right - left) * (i / float(n - 1) if n > 1 else 0.5)

    for k, key in enumerate(metrics):
        y0 = top + k * height
        plot_top, plot_bottom = y0 + 26, y0 + height - 34
        points = [(i, sl["per_session"][key]) for i, sl in enumerate(slices) if sl["per_session"][key] is not None]
        low, high = min(v for _i, v in points), max(v for _i, v in points)
        if high == low:
            low, high = low - 1, high + 1

        def y_at(v):
            return plot_bottom - (v - low) / float(high - low) * (plot_bottom - plot_top)

        out.append('<g class="chart" data-metric="%s">' % _attr(key))
        out.append('<text x="12" y="%d" fill="%s" font-size="12">%s, %s per session</text>' % (
            y0 + 14, _INK, escape(METRIC[key].label), METRIC[key].session_summary))
        out.append('<line x1="%d" y1="%.1f" x2="%d" y2="%.1f" stroke="%s" stroke-width="1"/>' % (
            left, plot_bottom, right, plot_bottom, _INK))
        for value in (high, low):
            out.append('<text x="%d" y="%.1f" fill="%s" text-anchor="end">%s</text>' % (
                left - 6, y_at(value) + 4, _INK, escape(_fmt(key, value))))
        out.append('<polyline fill="none" stroke="%s" stroke-width="2" points="%s"/>' % (
            _LINE, " ".join("%.1f,%.1f" % (x_at(i), y_at(v)) for i, v in points)))
        for i, v in points:
            out.append('<circle cx="%.1f" cy="%.1f" r="3" fill="%s"/>' % (x_at(i), y_at(v), _LINE))
        keys = [sl["key"] for sl in slices]
        for c in (c for c in result["flagged"] if c["metric"] == key and c["update"] in keys):
            u = keys.index(c["update"])
            x = (x_at(u - 1) + x_at(u)) / 2.0 if u > 0 else x_at(u)
            out.append('<line x1="%.1f" y1="%d" x2="%.1f" y2="%.1f" stroke="%s" stroke-width="1.5" '
                       'stroke-dasharray="4 3"/>' % (x, plot_top - 6, x, plot_bottom, _FLAG))
            out.append('<text x="%.1f" y="%d" fill="%s" text-anchor="middle">%s: %s</text>' % (
                x, plot_top - 9, _FLAG, escape(c["where"]), escape(_fmt_change(c))))
        out.append('<text x="%d" y="%.1f" fill="%s">%s</text>' % (left, plot_bottom + 16, _INK, escape(keys[0])))
        out.append('<text x="%d" y="%.1f" fill="%s" text-anchor="end">%s</text>' % (
            right, plot_bottom + 16, _INK, escape(keys[-1])))
        out.append("</g>")
    out.append("</svg>")
    return "\n".join(out) + "\n"


# ---------------------------------------------------------------------------
# Command line
# ---------------------------------------------------------------------------

def parse_since(text) -> int:
    m = re.match(r"^\s*(\d+)\s*d?\s*$", str(text or ""))
    if not m or int(m.group(1)) < 1:
        raise ValueError("expected a number of days, such as 90d")
    return int(m.group(1))


def _write(path, text) -> Optional[str]:
    try:
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(text)
    except OSError as exc:
        return "error: cannot write %s: %s" % (path, exc.strerror or exc)
    return None


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        prog="regress.py", description=__doc__.strip().split("\n\n")[0],
        epilog="Exit codes: 0 done, 1 a flagged change matches --fail-on, 2 usage error.")
    parser.add_argument("--since", default="90d", help="how far back to read, in days, such as 30d (default 90d)")
    parser.add_argument("--harness", default="claude-code", choices=transcripts.HARNESSES,
                        help="which agent's sessions to read (default claude-code); Cursor keeps no usable transcripts")
    parser.add_argument("--by", default="version", choices=("version", "model", "week"),
                        help="split by harness version (default), model, or ISO week")
    parser.add_argument("--project", metavar="PATH", help="only sessions whose working folder is PATH or inside it")
    parser.add_argument("--min-turns", type=int, default=MIN_TURNS,
                        help="turns each side of an update needs before it is tested (default %d)" % MIN_TURNS)
    parser.add_argument("--min-sessions", type=int, default=MIN_SESSIONS,
                        help="sessions each side of an update needs (default %d, at least %d)" % (
                            MIN_SESSIONS, MIN_SESSION_VALUES))
    parser.add_argument("--svg", metavar="PATH", help="also write a line chart of each flagged change to PATH")
    parser.add_argument("--json", action="store_true", help="print machine-readable JSON")
    parser.add_argument("--out", metavar="PATH", help="write the report to this file instead of printing it")
    parser.add_argument("--fail-on", choices=("worse", "any"),
                        help="exit 1 when a flagged change usually means worse (worse) or when anything is flagged (any)")
    args = parser.parse_args(argv)
    try:
        since = parse_since(args.since)
    except ValueError:
        parser.error("--since takes a number of days, such as 90d")
    if args.min_sessions < MIN_SESSION_VALUES:
        parser.error("--min-sessions must be at least %d" % MIN_SESSION_VALUES)
    if args.min_turns < 1:
        parser.error("--min-turns must be at least 1")
    result = analyze(args.harness, args.by, since, args.project, args.min_turns, args.min_sessions)
    chart_line = ""
    if args.svg:
        svg = render_svg(result)
        if svg is None:
            chart_line = "No chart written: nothing was flagged."
        else:
            error = _write(args.svg, svg)
            if error:
                print(error, file=sys.stderr)
                return 2
            result["svg"] = args.svg
            chart_line = "Chart written to %s" % args.svg
    if args.json:
        text = json.dumps(result, indent=2) + "\n"
    else:
        text = render_markdown(result) + ("\n" + chart_line + "\n" if chart_line else "")
    if args.out:
        error = _write(args.out, text)
        if error:
            print(error, file=sys.stderr)
            return 2
        print("Report written to %s" % args.out)
        if chart_line and args.json:
            print(chart_line)
    else:
        sys.stdout.write(text)
    if args.fail_on and any(c["usually"] == "worse" or args.fail_on == "any" for c in result["flagged"]):
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
