#!/usr/bin/env python3
"""Show where coding agents waste tokens, money, and time.

Reads the session transcripts of Claude Code, Codex, Gemini CLI, and OpenCode
on this machine and reports waste habits (re-reads, oversized tool results,
cache rebuilds after pauses, polling loops, compactions, subagent share) and
failure patterns (tool errors, error streaks, denials, interrupts,
corrections, identical-call loops, sessions that ended badly). Every number
follows a rule in references/how-it-counts.md. Read-only; nothing leaves the
machine.
"""

from __future__ import annotations

import argparse
import bisect
import datetime
import json
import os
import re
import shlex
import sys
import time

sys.dont_write_bytecode = True
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import pricing  # noqa: E402
import transcripts  # noqa: E402
from safe import code, safe_text  # noqa: E402

# Rules and thresholds; references/how-it-counts.md explains each one.
CHARS_PER_TOKEN = 4
OVERSIZED_TOKENS = 10000
REREAD_MIN = 3          # reads of the same file, the same way, with no change between
LOOP_MIN = 3            # the same call this many times in a row: a polling loop or an identical-call loop
STREAK_MIN = 3          # failed tool calls in a row
CACHE_LIFETIME = 300    # seconds: Anthropic's default prompt cache
CACHE_LIFETIME_1H = 3600  # once a session writes to the 1-hour cache
EXAMPLES = 5

HARNESS_NAMES = {"claude-code": "Claude Code", "codex": "Codex", "gemini-cli": "Gemini CLI",
                 "opencode": "OpenCode"}
WASTE = [  # id, label; the report ranks them by dollars
    ("rereads", "Re-reads of unchanged files"),
    ("oversized", "Tool results over 10,000 tokens"),
    ("rebuilds", "Cache rebuilds after pauses"),
    ("polling", "Polling loops"),
]
FIX_SECTIONS = {  # category id -> its section in references/fixes.md
    "rereads": "Re-reads of unchanged files", "oversized": "Tool results over 10,000 tokens",
    "rebuilds": "Cache rebuilds after pauses", "polling": "Polling loops", "compactions": "Compactions",
    "subagents": "Subagent share", "tool_errors": "Tool errors", "error_streaks": "Error streaks",
    "denials": "Permission denials", "interrupts": "User interrupts", "corrections": "User corrections",
    "loops": "Identical-call loops", "ended_badly": "Sessions that ended on an error or interrupt",
}
FIX_SHORT = {
    "rereads": "Read once; search or read a range", "oversized": "Trim output; read line ranges",
    "rebuilds": "Compact before breaks; start fresh", "polling": "Wait inside one command",
    "compactions": "one task per session", "subagents": "narrow tasks, cheaper models",
    "tool_errors": "Fix the top failing command", "error_streaks": "runaway-guard",
    "denials": "Allow safe commands; write the rules", "interrupts": "Write the reason down as a rule",
    "corrections": "Turn corrections into rules", "loops": "runaway-guard",
    "ended_badly": "Check the state before closing",
}
FAILURES = [
    ("tool_errors", "Tool errors"),
    ("error_streaks", "Error streaks (3+ failed calls in a row)"),
    ("denials", "Permission denials"),
    ("interrupts", "User interrupts"),
    ("corrections", "User corrections"),
    ("loops", "Identical-call loops (3+ same calls in a row)"),
    ("ended_badly", "Sessions that ended on an error or interrupt"),
]
BY_TOOL = ("tool_errors", "loops")  # rows broken down by tool names from the transcripts; the rest use fixed kinds


# ---------------------------------------------------------------------------
# Arguments
# ---------------------------------------------------------------------------

_SINCE_RE = re.compile(r"^(\d+(?:\.\d+)?)([dhw]?)$")


def parse_since(text) -> float:
    """'30d', '30', '12h', or '2w' as a number of days."""
    m = _SINCE_RE.match(str(text).strip().lower())
    if not m or float(m.group(1)) <= 0:
        raise ValueError("expected a number of days such as 30d, 12h, or 2w")
    return float(m.group(1)) * {"": 1.0, "d": 1.0, "h": 1 / 24.0, "w": 7.0}[m.group(2)]


def _since_arg(text) -> float:
    try:
        return parse_since(text)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(str(exc))


# ---------------------------------------------------------------------------
# Money, tokens, and time
# ---------------------------------------------------------------------------

def _tokens(usage) -> int:
    return usage.total_input() + usage.output


def _new_rate(usage, model):
    """Dollars per prompt token that a call did not read from the cache (cache
    writes and uncached input, at that call's mix); None for an unpriced model."""
    fresh = usage.input + usage.cache_write
    if fresh <= 0:
        return pricing.cost_usd(transcripts.Usage(input=1), model)
    cost = pricing.cost_usd(transcripts.Usage(input=usage.input, cache_write=usage.cache_write,
                                              cache_write_1h=usage.cache_write_1h), model)
    return None if cost is None else cost / fresh


_TS_RE = re.compile(r"^(\d{4})-(\d\d)-(\d\d)T(\d\d):(\d\d):(\d\d)(\.\d+)?")


def _epoch(ts):
    """Seconds since 1970 for an ISO 8601 UTC time, or None."""
    m = _TS_RE.match(ts) if isinstance(ts, str) else None
    if not m:
        return None
    try:
        stamp = datetime.datetime(*[int(x) for x in m.groups()[:6]], tzinfo=datetime.timezone.utc)
    except ValueError:
        return None
    return stamp.timestamp() + (float(m.group(7)) if m.group(7) else 0.0)


# ---------------------------------------------------------------------------
# One session's model calls
# ---------------------------------------------------------------------------

class _Timeline:
    """The model calls of one session in order, what each cost, and what it
    cost to carry a tool result from the next call to the end of its context
    (the next compaction or the end of the session)."""

    def __init__(self, session):
        ev = session.events
        self.calls = [i for i, e in enumerate(ev) if e.kind == "assistant" and e.usage is not None]
        self.compactions = [i for i, e in enumerate(ev) if e.kind == "compaction"]
        n = len(self.calls)
        usages = [ev[p].usage for p in self.calls]
        models = [ev[p].model for p in self.calls]
        self.segment = [bisect.bisect_right(self.compactions, p) for p in self.calls]
        self.prompt = [x.total_input() for x in usages]
        self.tokens = [_tokens(x) for x in usages]
        self.cost = [pricing.cost_usd(x, m) for x, m in zip(usages, models)]
        self.read_rate = [pricing.cost_usd(transcripts.Usage(cache_read=1), m) for m in models]
        self.new_rate = [_new_rate(x, m) for x, m in zip(usages, models)]
        self.asked = [0] * n            # tool calls each model call made
        arriving = [0.0] * n            # result tokens that reach each call's prompt
        for p, e in enumerate(ev):
            if e.kind == "tool" and e.tool is not None:
                k = self.issuer(p)
                if k >= 0:
                    self.asked[k] += 1
                if k + 1 < n:
                    arriving[k + 1] += e.tool.output_chars / CHARS_PER_TOKEN
        self.scale = []                 # results never add more than the prompt grew
        for j in range(n):
            growth = self.prompt[j] - (self.prompt[j - 1] if j else 0)
            self.scale.append(1.0 if arriving[j] <= max(growth, 0) or not arriving[j]
                              else max(growth, 0) / arriving[j])
        self.rebuild_tokens, self.rebuild_dollars = [0] * n, [0.0] * n
        self._find_rebuilds(ev, usages)
        self.charge(set())

    def charge(self, charged) -> None:
        """Mark model calls whose whole cost a waste row already counts (the calls
        that asked for re-reads and polling). Carrying a result costs nothing at
        those calls, so no dollar is counted twice."""
        n = len(self.calls)
        self.charged = set(charged)
        self.carry_calls = [0] * (n + 1)    # uncharged calls from j to the end of j's context
        self.carry_read = [0.0] * (n + 1)   # their cache-read prices, added up
        for j in range(n - 1, -1, -1):
            same = j + 1 < n and self.segment[j + 1] == self.segment[j]
            free = j not in self.charged
            self.carry_calls[j] = int(free) + (self.carry_calls[j + 1] if same else 0)
            self.carry_read[j] = ((self.read_rate[j] or 0.0) if free else 0.0) + (self.carry_read[j + 1] if same else 0.0)

    def _find_rebuilds(self, ev, usages) -> None:
        """A call that comes more than the cache lifetime after the previous
        call (with no compaction between) pays full price again for the part of
        the cache the previous call left that it did not read back."""
        if not any(x.cache_read or x.cache_write for x in usages):
            return  # the session never used the prompt cache, so a pause loses nothing
        writes = any(x.cache_write for x in usages)  # Codex and Gemini report none: all of a prompt is cached
        long_cache = False
        for j in range(1, len(self.calls)):
            prev = usages[j - 1]
            long_cache = long_cache or prev.cache_write_1h > 0
            before, after = _epoch(ev[self.calls[j - 1]].ts), _epoch(ev[self.calls[j]].ts)
            if self.segment[j] != self.segment[j - 1] or before is None or after is None:
                continue
            if after - before <= (CACHE_LIFETIME_1H if long_cache else CACHE_LIFETIME):
                continue
            cached = prev.cache_read + prev.cache_write if writes else prev.total_input()
            rebuilt = max(0, min(cached, self.prompt[j]) - usages[j].cache_read)
            self.rebuild_tokens[j] = rebuilt
            if rebuilt and self.new_rate[j] is not None and self.read_rate[j] is not None:
                self.rebuild_dollars[j] = rebuilt * max(0.0, self.new_rate[j] - self.read_rate[j])

    def issuer(self, pos) -> int:
        """Index of the model call that asked for the tool call at `pos`, or -1."""
        return bisect.bisect_right(self.calls, pos) - 1

    def entered(self, pos, chars) -> float:
        """Tokens a tool result at `pos` added to the next call's prompt (0 when
        no call in the same context read it): characters / 4, scaled down when
        the results of one step add up to more than the prompt grew."""
        j = self.issuer(pos) + 1
        if j >= len(self.calls) or self.segment[j] != bisect.bisect_right(self.compactions, pos):
            return 0.0
        return chars / float(CHARS_PER_TOKEN) * self.scale[j]

    def carried(self, pos, chars) -> tuple:
        """(tokens carried, dollars) for a tool result at `pos`: it enters the
        next call's prompt as new tokens and every later call in the same
        context reads it from the cache. Charged calls are skipped."""
        entered = self.entered(pos, chars)
        if not entered:
            return 0.0, 0.0
        j, n = self.issuer(pos) + 1, len(self.calls)
        first = 0.0 if j in self.charged else (self.new_rate[j] or 0.0)
        later = self.carry_read[j + 1] if j + 1 < n and self.segment[j + 1] == self.segment[j] else 0.0
        return entered * self.carry_calls[j], entered * (first + later)


# ---------------------------------------------------------------------------
# Waste rules
# ---------------------------------------------------------------------------

_SUBCOMMAND_TOOLS = {"git", "npm", "pnpm", "yarn", "bun", "gh", "docker", "kubectl", "cargo", "go", "uv",
                     "pip", "pip3", "poetry", "make", "just", "terraform", "aws", "gcloud", "az", "brew",
                     "npx", "dotnet", "mvn", "gradle", "deno", "swift"}
_SETUP_RE = re.compile(r"^(cd|export|set|source|\.|pushd|popd|unset|[A-Za-z_][A-Za-z0-9_]*=\S*)(\s|$)")


def _command_key(command) -> str:
    """The program (and its subcommand) a shell command runs, for grouping:
    'cd x && git diff --stat' becomes 'git diff'."""
    for segment in re.split(r"&&|\|\||;|\||\n", command or ""):
        segment = segment.strip()
        if not segment or _SETUP_RE.match(segment):
            continue
        try:
            words = shlex.split(segment)
        except ValueError:
            words = segment.split()
        while words and (re.match(r"^[A-Za-z_][A-Za-z0-9_]*=", words[0]) or words[0] in ("sudo", "env", "time",
                                                                                          "command", "exec")):
            words = words[1:]
        if not words:
            continue
        key = os.path.basename(words[0])
        if key in _SUBCOMMAND_TOOLS and len(words) > 1 and re.match(r"^[A-Za-z][\w:.-]*$", words[1]):
            key += " " + words[1]
        return key
    return ""


def _group(call) -> str:
    if call.kind == "shell":
        return ("%s %s" % (call.name, _command_key(call.command))).strip()
    return call.name


def _target(call, cwd) -> str:
    """What a tool call acted on, for evidence: the command, the files, or the tool name."""
    if call.kind == "shell" and call.command:
        return call.command
    if call.paths:
        return ", ".join(_show_path(_norm_path(p, cwd), cwd) for p in call.paths)
    return call.name


def _tool_cost(tl, pos, call) -> tuple:
    """(tokens, dollars) of a tool call that should not have happened: its share
    of the model call that asked for it, plus the cost of carrying its result."""
    share_tokens = share_dollars = 0.0
    k = tl.issuer(pos)
    if k >= 0 and tl.asked[k]:
        share_tokens = tl.tokens[k] / float(tl.asked[k])
        share_dollars = ((tl.cost[k] or 0.0) - tl.rebuild_dollars[k]) / tl.asked[k]
    tokens, dollars = tl.carried(pos, call.output_chars)
    return share_tokens + tokens, share_dollars + dollars


def _norm_path(path, cwd) -> str:
    if cwd and not os.path.isabs(path):
        path = os.path.join(cwd, path)
    return os.path.normpath(path)


def _show_path(path, cwd) -> str:
    """A path as the report shows it: relative to the session's folder when
    inside it, with the home folder as ~."""
    if cwd and (path + os.sep).startswith(cwd.rstrip(os.sep) + os.sep) and path != cwd:
        return os.path.relpath(path, cwd)
    home = os.path.expanduser("~")
    if home and home != "~" and (path + os.sep).startswith(home.rstrip(os.sep) + os.sep):
        return "~" + path[len(home.rstrip(os.sep)):]
    return path


def _item(category, session, event, tokens, dollars, evidence, count=1, group=""):
    """`evidence` is (template, values): fixed text with a {} for each value
    from the transcripts, already passed through safe_text."""
    return {"category": category, "session": session, "event": event, "tokens": tokens,
            "dollars": dollars, "evidence": evidence, "count": count, "group": group}


_SLEEP_ONLY = re.compile(r"^\s*sleep\s+\d+(?:\.\d+)?[smhd]?\s*;?\s*$")
_SLEEP_EDGE = re.compile(r"^\s*sleep\s+\d+(?:\.\d+)?[smhd]?\s*(?:&&|;)\s*"
                         r"|(?:&&|;)\s*sleep\s+\d+(?:\.\d+)?[smhd]?\s*;?\s*$")


def _call_key(call) -> tuple:
    """(what makes two calls the same, whether the call itself waits): a shell
    command with a `sleep` before or after it is the same command, and waits."""
    if call.kind != "shell":
        return (call.name, json.dumps(call.input, sort_keys=True)), False
    core, waited = call.command, False
    while True:
        core, n = _SLEEP_EDGE.subn("", core)
        if not n:
            break
        waited = True
    return (call.name, " ".join(core.split())), waited


def _is_read(call) -> bool:
    return bool(call.paths) and call.kind in ("read", "shell")


def _loops(s, counted, claimed, polled) -> tuple:
    """Runs of LOOP_MIN or more identical calls with nothing but waits (sleep
    commands) between them. With any waiting, a run is a polling loop: every
    call after the first check (checks and waits) is waste. Without waiting,
    it is an identical-call loop, a failure pattern; repeated reads are left to
    the re-read rule."""
    polls, loops = [], []
    state = {"key": None, "run": [], "waits": [], "between": [], "waited": False}

    def close():
        run, between = state["run"], state["between"]
        if len(run) >= LOOP_MIN and state["waited"]:
            polled.update(id(e) for _, e in run + between)
            members = [(p, e) for p, e in run[1:] + between if counted(e) and id(e) not in claimed]
            if members:
                claimed.update(id(e) for _, e in members)
                first, last = _epoch(run[0][1].ts), _epoch(run[-1][1].ts)
                span = ", over %s" % _duration(last - first) if first is not None and last is not None else ""
                polls.append({"category": "polling", "anchor": run[1][1], "members": members, "count": 1,
                              "evidence": ("{} ran %d times with only waiting between%s" % (len(run), span),
                                           [safe_text(_target(run[0][1].tool, s.cwd))])})
        elif len(run) >= LOOP_MIN and not _is_read(run[0][1].tool) and counted(run[-1][1]):
            loops.append((s, run[0][1].tool.name, len(run)))
        state.update(key=None, run=[], waits=[], between=[], waited=False)

    for p, e in enumerate(s.events):
        if e.kind == "compaction" or (e.kind == "user" and not e.injected):
            close()
            continue
        if e.kind != "tool" or e.tool is None:
            continue
        if e.tool.kind == "shell" and _SLEEP_ONLY.match(e.tool.command):
            state["waits"].append((p, e))
            continue
        key, waited = _call_key(e.tool)
        if state["run"] and key == state["key"]:
            state["between"] += state["waits"]
            state["waited"] = state["waited"] or waited or bool(state["waits"])
            state["run"].append((p, e))
            state["waits"] = []
        else:
            close()
            state.update(key=key, run=[(p, e)], waited=waited)
    close()
    return polls, loops


def _rereads(s, counted, claimed, polled) -> list:
    """Runs of REREAD_MIN or more reads of the same file with the same input and
    no possible change between: every read after the first is waste. A change
    is an edit or write of the file, a shell command, MCP call, or other tool
    call whose input names it, any subagent call, a message from the user, or
    a compaction."""
    found, runs = [], {}

    def close(keys):
        for key in keys:
            run = runs.pop(key)
            extra = [(p, e) for p, e in run[1:] if counted(e) and id(e) not in claimed]
            if len(run) < REREAD_MIN or not extra:
                continue
            claimed.update(id(e) for _, e in extra)
            shown = ", ".join(_show_path(x, s.cwd) for x in key[0])
            found.append({"category": "rereads", "anchor": extra[0][1], "members": extra, "count": len(extra),
                          "evidence": ("{} read %d times with no change between" % len(run), [safe_text(shown)])})

    for p, e in enumerate(s.events):
        if e.kind == "compaction" or (e.kind == "user" and not e.injected):
            close(list(runs))
            continue
        if e.kind != "tool" or e.tool is None:
            continue
        c = e.tool
        paths = tuple(sorted(set(_norm_path(x, s.cwd) for x in c.paths)))
        if c.kind in ("edit", "write"):
            close([k for k in runs if set(k[0]) & set(paths)])
        elif paths and c.kind in ("read", "shell"):
            if not (c.is_error or c.denied or id(e) in polled):
                key = (paths, c.command if c.kind == "shell" else json.dumps(c.input, sort_keys=True))
                runs.setdefault(key, []).append((p, e))
        elif c.kind == "shell":
            close([k for k in runs if any(os.path.basename(x) and os.path.basename(x) in c.command
                                          for x in k[0])])
        elif c.kind == "agent":     # a subagent may have edited any file
            close(list(runs))
        elif c.kind in ("mcp", "other"):
            text = json.dumps(c.input, ensure_ascii=False)
            close([k for k in runs if any(os.path.basename(x) and os.path.basename(x) in text for x in k[0])])
    close(list(runs))
    return found


def _duration(seconds) -> str:
    if seconds < 120:
        return "%d seconds" % round(seconds)
    if seconds < 7200:
        return "%d minutes" % round(seconds / 60.0)
    return "%.1f hours" % (seconds / 3600.0)


def _pause(seconds) -> str:
    minutes = int(round(seconds / 60.0))
    if minutes < 120:
        return "%d-minute" % minutes
    if minutes < 48 * 60:
        hours, minutes = divmod(minutes, 60)
        return "%d-hour" % hours + (" %d-minute" % minutes if minutes else "")
    days, minutes = divmod(minutes, 1440)
    return "%d-day" % days + (" %d-hour" % (minutes // 60) if minutes >= 60 else "")


def _rebuilds(s, tl, counted) -> list:
    items = []
    for j, pos in enumerate(tl.calls):
        e = s.events[pos]
        if tl.rebuild_tokens[j] and counted(e):
            gap = _epoch(e.ts) - _epoch(s.events[tl.calls[j - 1]].ts)
            items.append(_item("rebuilds", s, e, tl.rebuild_tokens[j], tl.rebuild_dollars[j],
                               ("%s pause before this call; %s tokens rebuilt" % (
                                   _pause(gap), _fmt_tokens(tl.rebuild_tokens[j])), [])))
    return items


def _oversized(s, tl, counted, claimed) -> list:
    """Results that added more than OVERSIZED_TOKENS to the prompt."""
    found = []
    for p, e in enumerate(s.events):
        if e.kind != "tool" or e.tool is None or id(e) in claimed or not counted(e):
            continue
        entered = tl.entered(p, e.tool.output_chars)
        if entered > OVERSIZED_TOKENS:
            found.append({"category": "oversized", "anchor": e, "members": [(p, e)], "count": 1,
                          "carry_only": True, "group": _group(e.tool),
                          "evidence": ("{} returned about %s tokens: {}" % _fmt_tokens(entered),
                                       [safe_text(e.tool.name, 80), safe_text(_target(e.tool, s.cwd))])})
    return found


def _costed(s, tl, found) -> dict:
    """An item with the tokens and dollars of what a detector found: both costs
    of each wasted tool call, or only the carrying of an oversized result."""
    tokens = dollars = 0.0
    for p, e in found["members"]:
        t, d = tl.carried(p, e.tool.output_chars) if found.get("carry_only") else _tool_cost(tl, p, e.tool)
        tokens, dollars = tokens + t, dollars + d
    return _item(found["category"], s, found["anchor"], tokens, dollars, found["evidence"],
                 count=found["count"], group=found.get("group", ""))


# ---------------------------------------------------------------------------
# Analysis
# ---------------------------------------------------------------------------

def _fmt_tokens(n) -> str:
    n = float(n)
    for size, unit in ((1e9, "B"), (1e6, "M"), (1e3, "k")):
        if abs(n) >= size:
            return ("%.1f" % (n / size)).rstrip("0").rstrip(".") + unit
    return "%d" % round(n)


def _show_time(ts) -> str:
    stamp = _epoch(ts)
    if stamp is None:
        return ""
    return datetime.datetime.fromtimestamp(stamp, tz=datetime.timezone.utc).strftime("%Y-%m-%d %H:%M UTC")


def _summarize(items, totals) -> dict:
    tokens = sum(i["tokens"] for i in items)
    dollars = sum(i["dollars"] for i in items)
    return {"count": sum(i["count"] for i in items), "tokens": int(round(tokens)), "dollars": round(dollars, 6),
            "share_of_spend": round(dollars / totals["dollars"], 6) if totals["dollars"] else 0.0,
            "share_of_tokens": round(tokens / totals["tokens"], 6) if totals["tokens"] else 0.0}


# User corrections: a short list of phrases, matched at the start of a message
# (after an optional "no," or "wait,") or, for the second list, anywhere.
_CORRECTION_LEAD = r"^\s*(?:(?:no|nope|wait|hmm|ugh|argh)\b[,.!]*\s*)?"
_CORRECTION_STARTS = [
    r"(?:that'?s|that is|this is|it'?s|it is)\s+(?:wrong|not right|incorrect|not what i (?:asked|wanted|meant|said))\b",
    r"(?:wrong|incorrect)\b",
    r"you\s+(?:didn'?t|did not|forgot|missed|broke|ignored|skipped|never)\b",
    r"why\s+(?:did|are|would)\s+you\b",
    r"i\s+(?:said|told you|asked you|asked for|meant)\b",
    r"(?:stop(?:\s+doing)?|don'?t\s+do|do not\s+do)\s+(?:that|this)\b",
    r"(?:undo|revert)\s+(?:that|this|it|your|the last)\b",
    r"not what i (?:asked|wanted|meant|said)\b",
    r"(?:it|this|that)\s+(?:still\s+)?(?:do(?:es)?n'?t|do(?:es)? not|didn'?t|did not)\s+work\b",
]
_CORRECTION_ANYWHERE = [r"\bi (?:already )?told you\b", r"\bas i said\b", r"\byou(?:'re| are) not listening\b",
                        r"\bstill\s+(?:do(?:es)?n'?t|do(?:es)? not|didn'?t|did not)\s+work\b",
                        r"\bstill\s+(?:broken|failing|wrong|not working|nothing)\b",
                        r"\b(?:stop|quit)\s+(?:screwing|messing)\b"]
_CORRECTION_RE = re.compile(_CORRECTION_LEAD + "(?:" + "|".join(_CORRECTION_STARTS) + ")|"
                            + "|".join(_CORRECTION_ANYWHERE), re.I)


def is_correction(text) -> bool:
    return bool(_CORRECTION_RE.search((text or "").replace("\u2019", "'")))


def _failed(call) -> bool:
    return call.is_error and not call.denied and not call.interrupted


def _ending(s):
    """How a main session ended: 'error', 'interrupt', or '' for a normal end."""
    for e in reversed(s.events):
        if (e.kind == "user" and e.injected) or e.kind == "compaction" or (
                e.kind == "assistant" and not e.text and e.usage is None):
            continue
        if e.kind == "interrupt" or (e.kind == "tool" and e.tool is not None and (e.tool.denied or e.tool.interrupted)):
            return "interrupt", e
        if e.kind == "api_error" or (e.kind == "tool" and e.tool is not None and _failed(e.tool)):
            return "error", e
        return "", e
    return "", None


def _session_failures(s, counted, tally) -> None:
    """Add one session's failure counts to `tally`."""
    streak = []

    def close():
        if len(streak) >= STREAK_MIN and counted(streak[0]):
            tally["error_streaks"] += 1
        del streak[:]

    for e in s.events:
        if e.kind == "compaction" or (e.kind == "user" and not e.injected):
            close()
        if e.kind == "tool" and e.tool is not None:
            c = e.tool
            if _failed(c):
                streak.append(e)
            else:
                close()
            if counted(e) and _failed(c):
                tally["tool_errors"][c.name] = tally["tool_errors"].get(c.name, 0) + 1
            if counted(e) and c.denied:
                tally["denials"][c.denied] = tally["denials"].get(c.denied, 0) + 1
        elif counted(e) and not s.is_subagent:
            if e.kind == "interrupt":
                tally["interrupts"] += 1
            elif e.kind == "user" and not e.injected and is_correction(e.text):
                tally["corrections"] += 1
    close()
    if not s.is_subagent:
        how, last = _ending(s)
        if how and counted(last):
            tally["ended_badly"][how] = tally["ended_badly"].get(how, 0) + 1


def _rate(count, base) -> float:
    return round(100.0 * count / base, 2) if base else 0.0


def _failures(totals, tally, loops, main_sessions) -> list:
    by_tool = {}
    for _s, name, _n in loops:
        by_tool[name] = by_tool.get(name, 0) + 1
    counts = {
        "tool_errors": (tally["tool_errors"], "tool calls"),
        "error_streaks": (tally["error_streaks"], "tool calls"),
        "denials": (tally["denials"], "tool calls"),
        "interrupts": (tally["interrupts"], "user messages"),
        "corrections": (tally["corrections"], "user messages"),
        "loops": (by_tool, "tool calls"),
        "ended_badly": (tally["ended_badly"], "sessions"),
    }
    bases = {"tool calls": totals["tool_calls"], "user messages": totals["user_messages"], "sessions": main_sessions}
    rows = []
    for fid, label in FAILURES:
        value, per = counts[fid]
        breakdown = {}
        if isinstance(value, dict):
            for k, v in sorted(value.items(), key=lambda kv: (-kv[1], kv[0])):
                k = safe_text(k)
                breakdown[k] = breakdown.get(k, 0) + v
            value = sum(breakdown.values())
        rows.append({"id": fid, "label": label, "count": value, "per_100": _rate(value, bases[per]), "per": per,
                     "breakdown": breakdown, "fix": _fix(fid)})
    return rows


def _empty_counts() -> dict:
    return {"sessions": 0, "subagent_sessions": 0, "model_calls": 0, "tool_calls": 0, "user_messages": 0,
            "tokens": 0, "dollars": 0.0, "unpriced_tokens": 0}


def _anchor(heading) -> str:
    """The link anchor GitHub gives a markdown heading."""
    return "-".join("".join(ch for ch in heading.lower() if ch.isalnum() or ch in " -").split())


def _fix(cid) -> str:
    return "references/fixes.md#" + _anchor(FIX_SECTIONS[cid])


def analyze(sessions, since_days=30.0, now=None, harness="all", project=None) -> dict:
    """The whole report as a dict (the --json output)."""
    sessions = list(sessions)
    now = time.time() if now is None else now
    cutoff = now - float(since_days) * 86400
    totals, by_harness = _empty_counts(), {}
    unpriced, unique, active = set(), set(), set()
    sub = {"sessions": set(), "tokens": 0, "dollars": 0.0}
    for s, e in transcripts.unique_events(sessions):
        stamp = _epoch(e.ts)
        if stamp is not None and stamp < cutoff:
            continue
        unique.add(id(e))
        buckets = (totals, by_harness.setdefault(s.harness, _empty_counts()))
        if id(s) not in active:
            active.add(id(s))
            for b in buckets:
                b["sessions"] += 1
                b["subagent_sessions"] += int(s.is_subagent)
        if e.usage is not None:
            tokens, cost = _tokens(e.usage), pricing.cost_usd(e.usage, e.model)
            for b in buckets:
                b["model_calls"] += 1
                b["tokens"] += tokens
                b["unpriced_tokens" if cost is None else "dollars"] += tokens if cost is None else cost
            if cost is None:
                unpriced.add(e.model or "(no model id)")
            if s.is_subagent:
                sub["sessions"].add(id(s))
                sub["tokens"] += tokens
                sub["dollars"] += cost or 0.0
        elif e.kind == "tool":
            for b in buckets:
                b["tool_calls"] += 1
        elif e.kind == "user" and not e.injected and not s.is_subagent:
            for b in buckets:
                b["user_messages"] += 1

    def counted(e):
        return id(e) in unique

    items, loops = [], []
    tally = {"tool_errors": {}, "error_streaks": 0, "denials": {}, "interrupts": 0, "corrections": 0,
             "ended_badly": {}}
    compactions = {"count": 0, "tokens_before": 0}
    for s in sessions:
        tl = _Timeline(s)
        _session_failures(s, counted, tally)
        for pos in tl.compactions:
            if counted(s.events[pos]):
                k = tl.issuer(pos)
                compactions["count"] += 1
                compactions["tokens_before"] += tl.prompt[k] if k >= 0 else 0
        claimed, polled = set(), set()      # a tool call counts in one waste row at most
        polls, session_loops = _loops(s, counted, claimed, polled)
        rereads = _rereads(s, counted, claimed, polled)
        found = polls + rereads + _oversized(s, tl, counted, claimed)
        tl.charge(set(tl.issuer(p) for f in polls + rereads for p, _e in f["members"]) - {-1})
        items += [_costed(s, tl, f) for f in found] + _rebuilds(s, tl, counted)
        loops += session_loops

    waste = []
    for cid, label in WASTE:
        row = {"id": cid, "label": label}
        row.update(_summarize([i for i in items if i["category"] == cid], totals))
        row["fix"] = _fix(cid)
        waste.append(row)
    waste.sort(key=lambda r: (-r["dollars"], -r["tokens"]))
    groups = {}
    for i in items:
        if i["category"] == "oversized":
            groups.setdefault(i["group"], []).append(i)
    oversized_groups = []
    for name, members in groups.items():
        row = {"group": safe_text(name)}
        row.update(_summarize(members, totals))
        oversized_groups.append(row)
    oversized_groups.sort(key=lambda r: (-r["dollars"], -r["tokens"]))
    labels = dict(WASTE)
    examples = []
    for i in sorted(items, key=lambda i: (-i["dollars"], -i["tokens"]))[:EXAMPLES]:
        (s, e), (template, values) = (i["session"], i["event"]), i["evidence"]
        examples.append({"category": i["category"], "label": labels[i["category"]],
                         "tokens": int(round(i["tokens"])), "dollars": round(i["dollars"], 6),
                         "harness": s.harness, "session": safe_text(s.id[:8]),
                         "folder": safe_text(_show_path(s.cwd, ""), 400) if s.cwd else "",
                         "path": safe_text(_show_path(s.path, ""), 400),
                         "time": _show_time(e.ts), "evidence": template.format(*values),
                         "evidence_template": template, "evidence_values": values})
    for h, counts in by_harness.items():
        spent = [i for i in items if i["session"].harness == h]
        counts["dollars"] = round(counts["dollars"], 6)
        counts["waste"] = {}
        for cid, _label in WASTE:
            mine = [i for i in spent if i["category"] == cid]
            counts["waste"][cid] = {"count": sum(i["count"] for i in mine),
                                    "tokens": int(round(sum(i["tokens"] for i in mine))),
                                    "dollars": round(sum(i["dollars"] for i in mine), 6)}
        counts["waste_dollars"] = round(sum(i["dollars"] for i in spent), 6)
        counts["waste_tokens"] = int(round(sum(i["tokens"] for i in spent)))
        if counts["dollars"]:
            counts["waste_share"], counts["waste_share_of"] = round(counts["waste_dollars"] / counts["dollars"], 6), "spend"
        else:
            counts["waste_share"] = round(counts["waste_tokens"] / float(counts["tokens"]), 6) if counts["tokens"] else 0.0
            counts["waste_share_of"] = "tokens"
    main_sessions = sum(1 for x in sessions if id(x) in active and not x.is_subagent)
    warned = [x for x in sessions if x.warnings]
    result = {
        "window_days": since_days, "harness": harness,
        "project": safe_text(project, 400) if project else None,
        "prices_checked": pricing.PRICES_CHECKED, "headline": "",
        "totals": dict(totals, dollars=round(totals["dollars"], 6),
                       unpriced_models=sorted(safe_text(m) for m in unpriced)),
        "by_harness": {h: by_harness[h] for h in sorted(by_harness)},
        "waste": waste, "oversized_groups": oversized_groups,
        "compactions": {"count": compactions["count"], "tokens_before": compactions["tokens_before"],
                        "average_tokens_before": int(round(compactions["tokens_before"] / float(compactions["count"])))
                        if compactions["count"] else 0, "fix": _fix("compactions")},
        "subagents": {"sessions": len(sub["sessions"]), "tokens": sub["tokens"], "dollars": round(sub["dollars"], 6),
                      "share_of_spend": round(sub["dollars"] / totals["dollars"], 6) if totals["dollars"] else 0.0,
                      "share_of_tokens": round(sub["tokens"] / float(totals["tokens"]), 6) if totals["tokens"] else 0.0,
                      "fix": _fix("subagents")},
        "failures": _failures(totals, tally, loops, main_sessions),
        "examples": examples, "notes": [],
        "warnings": {"sessions": len(warned), "lines": sum(len(x.warnings) for x in warned)},
    }
    result["headline"] = _headline(result)
    result["notes"] = _notes(result, safe_text)
    return result


# ---------------------------------------------------------------------------
# Report
# ---------------------------------------------------------------------------

HEADLINE_PHRASES = {"rereads": "re-reading files that had not changed",
                    "oversized": "tool results over 10,000 tokens",
                    "rebuilds": "cache rebuilds after pauses", "polling": "polling loops"}
UNITS = {"rereads": "re-read", "oversized": "result", "rebuilds": "pause", "polling": "loop"}


def _plural(n, word) -> str:
    return "%s %s%s" % (format(n, ","), word, "" if n == 1 else "s")


def _money(d) -> str:
    if d >= 10:
        return "$" + format(int(round(d)), ",")
    if d >= 0.005:
        return "$%.2f" % d
    return "<$0.01" if d > 0 else "$0"


def _pct(share) -> str:
    x = share * 100.0
    if x >= 1:
        return "%d%%" % round(x)
    if x >= 0.1:
        return "%.1f%%" % x
    return "<0.1%" if x > 0 else "0%"


def _window_words(days) -> str:
    if days < 1:
        return _plural(int(round(days * 24)), "hour")
    return "%s day%s" % (("%g" % days), "" if days == 1 else "s")


HEADLINE_DOLLARS = 0.005   # a row makes the headline from half a cent, or 1,000 tokens when nothing is priced
HEADLINE_TOKENS = 1000


def _ranked(waste) -> tuple:
    """(rows for the headline, whether they are ranked by dollars): by dollars
    when any row has a price, otherwise by tokens."""
    if any(c["dollars"] > 0 for c in waste):
        return sorted([c for c in waste if c["dollars"] >= HEADLINE_DOLLARS], key=lambda c: -c["dollars"]), True
    return sorted([c for c in waste if c["tokens"] >= HEADLINE_TOKENS], key=lambda c: -c["tokens"]), False


def _headline(r) -> str:
    t, window = r["totals"], _window_words(r["window_days"])
    if not t["sessions"]:
        return "No sessions found in the last %s." % window
    ranked, priced = _ranked(r["waste"])
    if not ranked:
        cost = ", %s at API prices" % _money(t["dollars"]) if t["dollars"] else ""
        if not any(c["dollars"] or c["tokens"] for c in r["waste"]):
            what = "no waste found"
        else:
            what = "waste under $0.01" if priced else "waste under 1,000 tokens"
        return "Last %s: %s in %s (%s tokens%s)." % (
            window, what, _plural(t["sessions"], "session"), _fmt_tokens(t["tokens"]), cost)
    first = ranked[0]
    if priced:
        text = "Last %s: %s of spend (%s at API prices) went to %s" % (
            window, _pct(first["share_of_spend"]), _money(first["dollars"]), HEADLINE_PHRASES[first["id"]])
        if len(ranked) > 1:
            text += ", and %s to %s" % (_money(ranked[1]["dollars"]), HEADLINE_PHRASES[ranked[1]["id"]])
    else:
        text = "Last %s: %s of tokens went to %s" % (
            window, _pct(first["share_of_tokens"]), HEADLINE_PHRASES[first["id"]])
        if len(ranked) > 1:
            text += ", and %s tokens to %s" % (_fmt_tokens(ranked[1]["tokens"]), HEADLINE_PHRASES[ranked[1]["id"]])
    return text + "."


def _notes(r, show) -> list:
    """The notes, with `show` applied to model ids: safe_text for JSON, code for markdown."""
    t, notes = r["totals"], []
    if t["sessions"]:
        notes.append("Dollars are API list prices checked %s. On a subscription plan, read them as relative cost."
                     % r["prices_checked"])
        notes.append("This report covers what the transcripts record. Some calls never reach them, such as the "
                     "one that writes a compaction summary.")
    if t["unpriced_tokens"]:
        notes.append("%s tokens on models with no known price (%s) are left out of the dollar figures." % (
            _fmt_tokens(t["unpriced_tokens"]), ", ".join(map(show, t["unpriced_models"]))))
    w = r["warnings"]
    if w["lines"]:
        notes.append("%s in %s could not be read or had an unknown record type, and %s skipped." % (
            _plural(w["lines"], "line"), _plural(w["sessions"], "session"), "was" if w["lines"] == 1 else "were"))
    if "opencode" in r["by_harness"]:
        notes.append("OpenCode support follows its documented database layout and has not been checked on a "
                     "real install.")
    if r["harness"] == "cursor":
        notes.append("Cursor keeps no transcripts with tool results, token usage, or times, "
                     "so there is nothing to measure.")
    return notes


def _num(x) -> str:
    return ("%.2f" % x).rstrip("0").rstrip(".")


def _top(breakdown, show=str, n=3) -> str:
    return ", ".join("%s %s" % (show(k), format(v, ",")) for k, v in list(breakdown.items())[:n])


def render_markdown(r) -> str:
    t = r["totals"]
    out = ["**%s**" % r["headline"], ""]
    if t["sessions"]:
        names = [HARNESS_NAMES.get(h, h) for h in r["by_harness"]]
        where = " and ".join([", ".join(names[:-1]), names[-1]]) if len(names) > 1 else names[0]
        out.append("%s%s in %s: %s, %s tokens, %s at API prices. For plain totals by day and model, run ccusage."
                   % (_plural(t["sessions"], "session"),
                      " (%s of them subagents)" % format(t["subagent_sessions"], ",") if t["subagent_sessions"] else "",
                      where, _plural(t["model_calls"], "model call"), _fmt_tokens(t["tokens"]), _money(t["dollars"])))
        share, share_key = ("Share of spend", "share_of_spend") if t["dollars"] else ("Share of tokens", "share_of_tokens")
        out += ["", "## Waste", "", "| Waste | Count | Tokens | Dollars | %s | Fix |" % share, "|---|---|---|---|---|---|"]
        for c in r["waste"]:
            out.append("| %s | %s | %s | %s | %s | %s |" % (c["label"], _plural(c["count"], UNITS[c["id"]]),
                                                          _fmt_tokens(c["tokens"]), _money(c["dollars"]),
                                                          _pct(c[share_key]), FIX_SHORT[c["id"]]))
        out.append("")
        if r["oversized_groups"]:
            out.append("Largest groups of oversized results: %s." % "; ".join(
                "%s (%s, %s tokens, %s)" % (code(g["group"]), _plural(g["count"], "result"), _fmt_tokens(g["tokens"]),
                                          _money(g["dollars"])) for g in r["oversized_groups"][:5]))
        comp, sub = r["compactions"], r["subagents"]
        if comp["count"]:
            out.append("Compactions: %s, with about %s tokens of context before each. Fix: %s." % (
                format(comp["count"], ","), _fmt_tokens(comp["average_tokens_before"]), FIX_SHORT["compactions"]))
        else:
            out.append("Compactions: none.")
        if sub["sessions"]:
            out.append("Subagents: %s of %s (%s, %s tokens) in %s. Fix: %s." % (
                _pct(sub[share_key]), share.split()[-1], _money(sub["dollars"]), _fmt_tokens(sub["tokens"]),
                _plural(sub["sessions"], "subagent session"), FIX_SHORT["subagents"]))
        else:
            out.append("Subagents: none.")
        out += ["", "## Failures", "", "| Failure | Count | Rate | Most common | Fix |", "|---|---|---|---|---|"]
        for f in r["failures"]:
            out.append("| %s | %s | %s per 100 %s | %s | %s |" % (f["label"], format(f["count"], ","),
                                                             _num(f["per_100"]), f["per"],
                                                             _top(f["breakdown"], code if f["id"] in BY_TOOL else str),
                                                             FIX_SHORT[f["id"]]))
        out += ["", "## Top examples", ""]
        if not r["examples"]:
            out.append("No waste found.")
        for n, x in enumerate(r["examples"], 1):
            out.append("%d. %s: %s, %s tokens. %s." % (n, x["label"], _money(x["dollars"]), _fmt_tokens(x["tokens"]),
                                                     x["evidence_template"].format(*map(code, x["evidence_values"]))))
            out.append("   %s session %s%s%s" % (HARNESS_NAMES.get(x["harness"], x["harness"]), code(x["session"]),
                                             " at " + x["time"] if x["time"] else "",
                                             ", in %s" % code(x["folder"], 400) if x["folder"] else ""))
        out += ["", "## By harness", "",
                "| Harness | Sessions | Model calls | Tokens | Dollars | Waste share | Top waste |",
                "|---|---|---|---|---|---|---|"]
        labels = dict(WASTE)
        for h, c in r["by_harness"].items():
            rows = [dict(v, id=k) for k, v in c["waste"].items()]
            key = "dollars" if any(x["dollars"] > 0 for x in rows) else "tokens"
            top = max(rows, key=lambda x: x[key])
            top_text = "none" if not top[key] else "%s %s" % (labels[top["id"]], _money(top["dollars"]) if
                                                              key == "dollars" else _fmt_tokens(top["tokens"]) + " tokens")
            share = _pct(c["waste_share"]) + ("" if c["waste_share_of"] == "spend" else " of tokens")
            out.append("| %s | %s | %s | %s | %s | %s | %s |" % (
                HARNESS_NAMES.get(h, h), format(c["sessions"], ","), format(c["model_calls"], ","),
                _fmt_tokens(c["tokens"]), _money(c["dollars"]), share, top_text))
        out.append("")
        out.append("Each fix is explained in references/fixes.md, in the section named like the row.")
    notes = _notes(r, code)
    if notes:
        out += ["", "## Notes", ""] + ["- " + n for n in notes]
    return "\n".join(out) + "\n"


# ---------------------------------------------------------------------------
# Command line
# ---------------------------------------------------------------------------

def load_sessions(harness="all", since_days=30.0, project=None, home=None) -> list:
    """Sessions oldest file first, so a fork's copied records count in the original."""
    found = transcripts.find_sessions(harness=harness, since_days=since_days, project=project, home=home)
    sessions = []
    for h, p in reversed(found):
        s = transcripts.load_session(h, p)
        for e in s.events:
            if e.tool is not None:
                e.tool.output = ""     # only the size (output_chars) is used; this keeps memory low
        sessions.append(s)
    return sessions


def main(argv=None, home=None) -> int:
    parser = argparse.ArgumentParser(
        prog="waste.py", description=__doc__.strip().split("\n\n")[0],
        epilog="Exit codes: 0 done (also when no sessions are found), 2 usage error or a report file that "
               "cannot be written.")
    parser.add_argument("--since", type=_since_arg, default=30.0, metavar="30d",
                        help="how far back to look: days (30d or 30), hours (12h), or weeks (2w); default 30d")
    parser.add_argument("--harness", default="all", choices=("all",) + transcripts.HARNESSES + ("cursor",),
                        help="which harness to read (default: all)")
    parser.add_argument("--project", metavar="PATH",
                        help="only sessions whose working folder is this folder or inside it")
    parser.add_argument("--json", action="store_true", help="print machine-readable JSON")
    parser.add_argument("--out", metavar="PATH", help="write the report to this file instead of printing it")
    args = parser.parse_args(argv)
    try:
        (args.project or "").encode("utf-8")
    except UnicodeEncodeError:
        print("error: --project %s is not a UTF-8 path, so it cannot match a session's folder"
              % safe_text(args.project, 400), file=sys.stderr)
        return 2
    sessions = [] if args.harness == "cursor" else load_sessions(args.harness, args.since, args.project, home)
    result = analyze(sessions, since_days=args.since, harness=args.harness, project=args.project)
    text = json.dumps(result, indent=2, ensure_ascii=False) + "\n" if args.json else render_markdown(result)
    if args.out:
        try:
            with open(args.out, "w", encoding="utf-8") as fh:
                fh.write(text)
        except OSError as exc:
            print("error: cannot write the report to %s: %s" % (safe_text(args.out, 400),
                                                                 exc.strerror or type(exc).__name__), file=sys.stderr)
            return 2
        print("Report written to %s" % args.out)
    else:
        sys.stdout.write(text)
    return 0


if __name__ == "__main__":
    sys.exit(main())
