# Copied from skills/evals/shared/transcripts.py by skills/evals/tools/sync_shared.py. Edit the source, then run the sync.
"""Read coding-agent session transcripts into one simple shape.

Supported: Claude Code, Codex CLI, Gemini CLI, and OpenCode. OpenCode support
is best effort: it follows the documented database schema and is not verified
on a real install. Cursor keeps no usable transcripts (no tool results, token
usage, or timestamps), so it is skipped.

Read-only, Python 3.9+, standard library only. Every format rule comes from
the harness facts file (research-harness-facts.md, section Q1) plus read-only
checks of real files. The formats are internal to each harness and change
between versions, so unknown records are counted in Session.warnings and never
stop the parse.

Token counts use Anthropic meanings for every harness: `input` is uncached
input, cache reads and writes are separate, and `output` includes reasoning.
"""

from __future__ import annotations

import datetime
import hashlib
import json
import os
import pathlib
import re
import shlex
import sqlite3
import time
from dataclasses import dataclass, field, fields
from typing import Optional

# safe.py sits next to this file (sync_shared.py copies both). Skills reach these
# names through this module too, as transcripts.safe_text and transcripts.redact.
from safe import code, redact, safe_text  # noqa: F401

HARNESSES = ("claude-code", "codex", "gemini-cli", "opencode")
MAX_OUTPUT_CHARS = 20000


@dataclass
class Usage:
    input: int = 0            # uncached input tokens
    cache_read: int = 0
    cache_write: int = 0      # all cache-write tokens (5-minute plus 1-hour)
    cache_write_1h: int = 0   # the 1-hour part of cache_write, when known
    output: int = 0           # includes reasoning or thinking tokens
    reasoning: int = 0        # the reasoning part of output, when known

    def total_input(self) -> int:
        return self.input + self.cache_read + self.cache_write

    def __add__(self, other: "Usage") -> "Usage":
        return Usage(**{f.name: getattr(self, f.name) + getattr(other, f.name) for f in fields(self)})


@dataclass
class ToolCall:
    id: str
    name: str             # harness-native name: Bash, exec_command, run_shell_command, mcp__x__y, ...
    kind: str             # "shell" | "read" | "edit" | "write" | "search" | "web" | "agent" | "mcp" | "other"
    input: dict           # parsed input; free-form input becomes {"raw": "<text>"}
    command: str = ""     # the shell command or script text when kind == "shell"
    paths: list = field(default_factory=list)   # files read or changed, when known
    ts: str = ""
    output: str = ""      # result text, cut to MAX_OUTPUT_CHARS
    output_chars: int = 0  # full result length before the cut
    has_result: bool = False
    is_error: bool = False
    exit_code: Optional[int] = None  # only when the harness records it (or, for Claude Code, implies 0)
    denied: str = ""      # "" | "user-rejected" | "permission-rule" | "hook" | "auto-reviewer" | "other"
    interrupted: bool = False


@dataclass
class Event:
    kind: str             # "user" | "assistant" | "tool" | "interrupt" | "compaction" | "api_error"
    ts: str = ""          # ISO 8601 UTC
    text: str = ""        # user or assistant text; compaction summary; error text
    injected: bool = False  # user-role content the harness added (context files, reminders, hook output)
    tool: Optional[ToolCall] = None   # kind == "tool"
    usage: Optional[Usage] = None     # kind == "assistant": one Event with usage per API response
    model: str = ""
    sidechain: bool = False
    id: str = ""          # the harness id behind the event (API response id, record id, call id), when known.
    # A forked or resumed Claude Code session can begin with a copy of the earlier
    # session's records, ids included: when totaling several sessions, count each id once.
    mode: str = ""        # Claude Code permission mode in effect for a tool call (the latest
    # user record's permissionMode: default, plan, acceptEdits, auto, bypassPermissions); "" if unknown.


@dataclass
class Session:
    harness: str
    id: str
    path: str             # the file; for OpenCode '<database file>#<session id>'
    cwd: str = ""
    version: str = ""     # Claude Code record "version", Codex cli_version, OpenCode version; else ""
    models: list = field(default_factory=list)
    started: str = ""
    ended: str = ""
    is_subagent: bool = False
    parent_id: str = ""
    events: list = field(default_factory=list)
    warnings: list = field(default_factory=list)   # skipped lines and unknown records: no content

    def tool_calls(self) -> list:
        return [e.tool for e in self.events if e.kind == "tool" and e.tool is not None]

    def usage_total(self) -> Usage:
        total = Usage()
        for e in self.events:
            if e.usage is not None:
                total = total + e.usage
        return total

    def usage_by_model(self) -> dict:
        out = {}
        for e in self.events:
            if e.usage is not None:
                out[e.model] = out.get(e.model, Usage()) + e.usage
        return out

    def user_prompts(self) -> list:
        return [e for e in self.events if e.kind == "user" and not e.injected]


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------

_SAFE_NAME_RE = re.compile(r"^[A-Za-z0-9_.:\-]{1,40}$")


def _warn(session, lineno, what) -> None:
    session.warnings.append("line %d: %s" % (lineno, what) if lineno else what)


def _name(value) -> str:
    """A record type name that is safe to put in a warning (no content)."""
    return repr(value) if isinstance(value, str) and _SAFE_NAME_RE.match(value) else "(unnamed)"


def _add(session, event) -> Event:
    session.events.append(event)
    return event


def _set_output(call, text) -> None:
    text = text if isinstance(text, str) else ""
    call.output_chars = len(text)
    call.output = text[:MAX_OUTPUT_CHARS]


def _block_text(content) -> str:
    """Text of a string or of a list of content blocks (non-text blocks skipped)."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(b.get("text") or "" for b in content
                         if isinstance(b, dict) and isinstance(b.get("text"), str))
    return ""


def _as_dict(value) -> dict:
    return value if isinstance(value, dict) else {}


def _int(value) -> int:
    return value if isinstance(value, int) and not isinstance(value, bool) else 0


def _track_time(session, ts) -> None:
    if isinstance(ts, str) and ts:
        if not session.started or ts < session.started:
            session.started = ts
        if ts > session.ended:
            session.ended = ts


def _raw_lines(path, start=0, complete_only=False):
    """Yield (lineno, offset, raw bytes) per line. With complete_only, a last line
    that has no newline yet (a file still being written) is not read."""
    with open(path, "rb") as fh:
        fh.seek(start)
        offset = start
        for lineno, raw in enumerate(fh, 1):
            if complete_only and not raw.endswith(b"\n"):
                break
            yield lineno, offset, raw
            offset += len(raw)


def _parse(session, lineno, raw):
    if not raw.strip():
        return None
    try:
        rec = json.loads(raw)
    except ValueError:
        _warn(session, lineno, "malformed JSON line")
        return None
    if not isinstance(rec, dict):
        _warn(session, lineno, "line is not a JSON object")
        return None
    return rec


def _records(path, session):
    """Yield (lineno, record) for each JSON line; bad lines become warnings."""
    try:
        for lineno, _offset, raw in _raw_lines(path):
            rec = _parse(session, lineno, raw)
            if rec is not None:
                yield lineno, rec
    except OSError as exc:
        _warn(session, 0, "cannot read file: %s" % type(exc).__name__)


# ---------------------------------------------------------------------------
# Claude Code (facts Q1.1)
# ---------------------------------------------------------------------------

_CC_KINDS = {
    "Bash": "shell", "Monitor": "shell", "Read": "read", "NotebookRead": "read", "Edit": "edit", "MultiEdit": "edit",
    "NotebookEdit": "edit", "Write": "write", "Grep": "search", "Glob": "search", "LS": "search",
    "WebFetch": "web", "WebSearch": "web", "Agent": "agent", "Task": "agent",
}
_CC_METADATA = {
    "last-prompt", "custom-title", "ai-title", "agent-name", "queue-operation", "bridge-session",
    "atis-latch", "mode", "file-history-snapshot", "file-history-delta", "cost-state", "summary",
    "pr-link", "frame-link", "artifact-comment-monitor", "artifact-autoreact-ledger", "relocated",
}
_CC_INTERRUPTS = ("[Request interrupted by user]", "[Request interrupted by user for tool use]")
_CC_INJECTED_TAGS = ("<system-reminder>", "<task-notification>", "<local-command-stdout>",
                     "<local-command-stderr>", "<local-command-caveat>", "<bash-stdout>",
                     "<bash-stderr>")
_CC_DENIALS = {"user-rejected": "user-rejected", "permission-rule": "permission-rule",
               "automode-blocked": "auto-reviewer"}
_CC_EXIT_RE = re.compile(r"Exit code (-?\d+)")


def _cc_kind(name) -> str:
    if name.startswith("mcp__"):
        return "mcp"
    return _CC_KINDS.get(name, "other")


def _cc_usage(u) -> Optional[Usage]:
    if not isinstance(u, dict):
        return None
    write = _int(u.get("cache_creation_input_tokens"))
    return Usage(
        input=_int(u.get("input_tokens")),
        cache_read=_int(u.get("cache_read_input_tokens")),
        cache_write=write,
        cache_write_1h=min(write, _int(_as_dict(u.get("cache_creation")).get("ephemeral_1h_input_tokens"))),
        output=_int(u.get("output_tokens")),
        reasoning=_int(_as_dict(u.get("output_tokens_details")).get("thinking_tokens")),
    )


class _Claude:
    """Turns Claude Code records into Events, in file order."""

    def __init__(self, session):
        self.s = session
        self.responses = {}      # message.id (or requestId) -> assistant Event
        self.calls = {}          # tool_use id -> ToolCall
        self.hook_denied = set()
        self.last_compaction = None
        self.uuid = ""           # id of the record being read
        self.mode = ""           # latest permissionMode seen on a user record

    def feed(self, lineno, rec) -> None:
        kind = rec.get("type")
        if kind in ("user", "assistant", "system", "attachment"):
            _track_time(self.s, rec.get("timestamp"))
            if not self.s.cwd and isinstance(rec.get("cwd"), str):
                self.s.cwd = rec["cwd"]
            if not self.s.version and isinstance(rec.get("version"), str):
                self.s.version = rec["version"]
            self.uuid = rec.get("uuid") if isinstance(rec.get("uuid"), str) else ""
            getattr(self, "_" + kind)(lineno, rec, rec.get("timestamp") or "", bool(rec.get("isSidechain")))
        elif kind is None:
            _warn(self.s, lineno, "record without a type")
        elif kind not in _CC_METADATA:
            _warn(self.s, lineno, "unknown record type %s" % _name(kind))

    def _assistant(self, lineno, rec, ts, side) -> None:
        msg = _as_dict(rec.get("message"))
        model = msg.get("model") if isinstance(msg.get("model"), str) else ""
        blocks = msg.get("content")
        if isinstance(blocks, str):
            blocks = [{"type": "text", "text": blocks}]
        blocks = [b for b in blocks if isinstance(b, dict)] if isinstance(blocks, list) else []
        texts = [b["text"] for b in blocks if b.get("type") == "text" and isinstance(b.get("text"), str)]
        if model == "<synthetic>":  # harness-made message, not an API response: no usage
            kind = "api_error" if rec.get("isApiErrorMessage") else "assistant"
            _add(self.s, Event(kind, ts, "\n".join(texts), model="" if kind == "api_error" else model,
                               sidechain=side, id=self.uuid))
            return
        key = msg.get("id") or rec.get("requestId") or "line-%d" % lineno
        usage = _cc_usage(msg.get("usage"))
        ev = self.responses.get(key)
        if ev is None:  # first record of this API response
            ev = self.responses[key] = _add(self.s, Event("assistant", ts, usage=usage, model=model,
                                                          sidechain=side, id=str(key)))
            if model and model not in self.s.models:
                self.s.models.append(model)
        elif usage is not None:  # later records repeat the input side and grow the output
            ev.usage = usage
        if texts:
            ev.text = "\n".join([ev.text] + texts) if ev.text else "\n".join(texts)
        for b in blocks:
            if b.get("type") == "tool_use":
                call = self._call(b, ts)
                _add(self.s, Event("tool", ts, tool=call, model=model, sidechain=side, id=call.id,
                                   mode=self.mode))

    def _call(self, b, ts) -> ToolCall:
        name = b.get("name") if isinstance(b.get("name"), str) else ""
        inp = b.get("input")
        inp = inp if isinstance(inp, dict) else ({} if inp is None else {"raw": str(inp)})
        call = ToolCall(id=str(b.get("id") or ""), name=name, kind=_cc_kind(name), input=inp, ts=ts)
        if call.kind == "shell":
            call.command = str(inp.get("command") or "")
        elif call.kind in ("read", "edit", "write"):
            path = inp.get("file_path") or inp.get("notebook_path")
            if isinstance(path, str) and path:
                call.paths = [path]
        self.calls[call.id] = call
        return call

    def _user(self, lineno, rec, ts, side) -> None:
        if isinstance(rec.get("permissionMode"), str) and rec["permissionMode"]:
            self.mode = rec["permissionMode"]
        content = _as_dict(rec.get("message")).get("content")
        blocks = [{"type": "text", "text": content}] if isinstance(content, str) else content
        blocks = [b for b in blocks if isinstance(b, dict)] if isinstance(blocks, list) else []
        if rec.get("isCompactSummary"):
            summary = _block_text(blocks)
            if self.last_compaction is not None and not self.last_compaction.text:
                self.last_compaction.text = summary
            else:
                self.last_compaction = _add(self.s, Event("compaction", ts, summary, sidechain=side, id=self.uuid))
            return
        origin = rec.get("origin")
        origin_kind = origin.get("kind") if isinstance(origin, dict) else None
        harness_made = bool(rec.get("isMeta")) or origin_kind not in (None, "human")
        run, run_injected = [], False
        for b in blocks:
            if b.get("type") == "tool_result":
                self._result(lineno, b, rec)
                continue
            if b.get("type") != "text" or not isinstance(b.get("text"), str):
                continue
            txt = b["text"]
            if txt.strip() in _CC_INTERRUPTS:
                self._flush(run, run_injected, ts, side)
                run = []
                _add(self.s, Event("interrupt", ts, txt.strip(), sidechain=side, id=self.uuid))
                continue
            injected = harness_made or txt.lstrip().startswith(_CC_INJECTED_TAGS)
            if run and injected != run_injected:
                self._flush(run, run_injected, ts, side)
                run = []
            run.append(txt)
            run_injected = injected
        self._flush(run, run_injected, ts, side)

    def _flush(self, texts, injected, ts, side) -> None:
        if texts:
            _add(self.s, Event("user", ts, "\n".join(texts), injected=injected, sidechain=side, id=self.uuid))

    def _result(self, lineno, b, rec) -> None:
        call = self.calls.get(b.get("tool_use_id"))
        if call is None:
            _warn(self.s, lineno, "tool result without a matching call")
            return
        txt = _block_text(b.get("content"))
        call.has_result = True
        call.is_error = bool(b.get("is_error"))
        _set_output(call, txt)
        extra = rec.get("toolUseResult")
        extra = extra if isinstance(extra, dict) else None
        if extra is not None and extra.get("interrupted") is True:
            call.interrupted = True
        denial = rec.get("toolDenialKind")
        if isinstance(denial, str) and denial:
            call.denied = _CC_DENIALS.get(denial, "other")
            call.is_error = True
        if call.kind == "shell":
            m = _CC_EXIT_RE.match(txt) if call.is_error else None
            if m:
                call.exit_code = int(m.group(1))
            elif (not call.is_error and not call.interrupted and extra is not None
                  and not extra.get("returnCodeInterpretation") and not extra.get("backgroundTaskId")):
                call.exit_code = 0  # the Bash tool flags every nonzero exit it does not explain

    def _system(self, lineno, rec, ts, side) -> None:
        sub = rec.get("subtype")
        if sub == "compact_boundary":
            self.last_compaction = _add(self.s, Event("compaction", ts, sidechain=side, id=self.uuid))
        elif sub == "api_error":
            err = _as_dict(rec.get("error"))
            words = [str(err[k]) for k in ("status", "message") if err.get(k) not in (None, "")]
            _add(self.s, Event("api_error", ts, " ".join(words), sidechain=side, id=self.uuid))

    def _attachment(self, lineno, rec, ts, side) -> None:
        att = _as_dict(rec.get("attachment"))
        kind = att.get("type")
        if (kind == "hook_blocking_error" and att.get("hookEvent") in ("PreToolUse", "PermissionRequest")
                and att.get("toolUseID")):
            self.hook_denied.add(att["toolUseID"])
        elif kind == "hook_additional_context":
            content = att.get("content")
            txt = "\n".join(str(x) for x in content) if isinstance(content, list) else str(content or "")
            if txt:
                _add(self.s, Event("user", ts, txt, injected=True, sidechain=side, id=self.uuid))

    def finish(self) -> None:
        for tool_id in self.hook_denied:
            call = self.calls.get(tool_id)
            if call is not None and not call.denied:
                call.denied = "hook"
                call.is_error = True


def _cc_session(path) -> Session:
    stem = os.path.basename(path)[:-len(".jsonl")] if path.endswith(".jsonl") else os.path.basename(path)
    # Subagents live in <session>/subagents/, Workflow agents in <session>/subagents/workflows/<id>/.
    folder = os.path.dirname(path)
    for _ in range(3):
        if os.path.basename(folder) == "subagents" and stem.startswith("agent-"):
            return Session("claude-code", stem[len("agent-"):], path, is_subagent=True,
                           parent_id=os.path.basename(os.path.dirname(folder)))
        folder = os.path.dirname(folder)
    return Session("claude-code", stem, path)


def _load_claude(path) -> Session:
    s = _cc_session(path)
    reader = _Claude(s)
    for lineno, rec in _records(path, s):
        reader.feed(lineno, rec)
    reader.finish()
    return s


def _cc_newest_response(rows):
    """Index of the first record of the newest API response, or None."""
    first_seen = {}
    for i, (_lineno, rec) in enumerate(rows):
        if rec.get("type") == "assistant":
            msg = _as_dict(rec.get("message"))
            if msg.get("model") != "<synthetic>":
                first_seen.setdefault(msg.get("id") or rec.get("requestId") or i, i)
    return max(first_seen.values()) if first_seen else None


# ---------------------------------------------------------------------------
# Codex CLI (facts Q1.2)
# ---------------------------------------------------------------------------

# Response items the model produces; the first one after a usage line starts a response.
_CX_MODEL_ITEMS = {"reasoning", "function_call", "custom_tool_call", "local_shell_call", "web_search_call",
                   "tool_search_call", "image_generation_call"}
_CX_OUTPUTS = {"function_call_output", "custom_tool_call_output", "tool_search_output",
               "local_shell_call_output"}
_CX_QUIET_ITEMS = {"compaction", "agent_message", "ghost_snapshot"}
_CX_QUIET_LINES = {"world_state", "inter_agent_communication", "inter_agent_communication_metadata",
                   "retained_context", "security_risk_score", "realtime_item"}
_CX_SHELL = {"shell", "exec_command", "shell_command", "container.exec", "local_shell"}
_CX_DENIALS = (("exec command rejected by user", "user-rejected"), ("patch rejected by user", "user-rejected"),
               ("rejected by configuration", "hook"),
               ("automatic approval review denied the action", "auto-reviewer"))
_CX_EXIT_RES = (re.compile(r"^Exit code: (-?\d+)", re.M), re.compile(r"^Process exited with code (-?\d+)", re.M))
_CX_PATCH_RE = re.compile(r"^\*\*\* (?:Update|Add|Delete) File: (.+)$|^\*\*\* Move to: (.+)$", re.M)
# Context the harness adds as a user message: one whole <tag>...</tag> block, or AGENTS.md text.
_CX_CONTEXT_RE = re.compile(r"^\s*(?:<([A-Za-z_][A-Za-z0-9_]*)[\s>].*</\1>\s*$|# AGENTS\.md instructions)", re.S)
# Matches the record type key only: inside text values the quotes are escaped.
_CX_USAGE_MARK = re.compile(rb'"type"\s*:\s*"token_usage_record"')
_CX_NAME_RE = re.compile(r"rollout-\d{4}-\d\d-\d\dT\d\d-\d\d-\d\d-(.+?)\.jsonl")


def _cx_kind(name, namespace) -> str:
    if namespace.startswith("mcp__") or name.startswith("mcp__"):
        return "mcp"
    if not namespace and name in _CX_SHELL:
        return "shell"
    if name == "apply_patch":
        return "edit"
    if name == "view_image":
        return "read"
    return {"collaboration": "agent", "web": "web"}.get(namespace, "other")


def _cx_argv(argv) -> str:
    """The script text of a [shell, -lc, script] command, else the joined argv."""
    if isinstance(argv, str):
        return argv
    if not isinstance(argv, list) or not all(isinstance(a, str) for a in argv):
        return ""
    if len(argv) >= 3 and argv[-2] in ("-c", "-lc", "-ic", "-lic"):
        return argv[-1]
    return shlex.join(argv)


def _cx_usage(u) -> Optional[Usage]:
    """OpenAI meanings (cached is part of input, reasoning part of output) to Usage."""
    if not isinstance(u, dict):
        return None
    cached = _int(u.get("cached_input_tokens"))
    return Usage(input=max(0, _int(u.get("input_tokens")) - cached), cache_read=cached,
                 cache_write=_int(u.get("cache_write_input_tokens")), output=_int(u.get("output_tokens")),
                 reasoning=_int(u.get("reasoning_output_tokens")))


def _cx_text(output) -> str:
    if isinstance(output, str):
        return output
    if isinstance(output, list):
        return "\n".join(x["text"] for x in output if isinstance(x, dict) and isinstance(x.get("text"), str))
    return ""


def _cx_denial(text) -> str:
    """Denial kind from the start of a tool output (where Codex puts the message);
    output that merely quotes the words further down is not a denial."""
    head = text[:200]
    for marker, kind in _CX_DENIALS:
        if marker in head:
            return kind
    return ""


class _Codex:
    """Turns Codex rollout lines into Events, in file order.

    Usage comes from token_usage_record lines (one per response id) when the file
    has them, otherwise from token_count events with repeats removed. In code
    mode the model calls one `exec` script that runs the real operations; each
    operation is logged as an item_completed event, so those become the tool
    calls and the script call itself is kept only when it ran none."""

    def __init__(self, session, use_records):
        self.s = session
        self.use_records = use_records
        self.model = ""
        self.meta_seen = False
        self.all_injected = False     # review subagents: every user message is harness-made
        self.pending = None           # assistant Event of the response being read
        self.pending_at = None        # where that Event goes once it has text or usage
        self.calls = {}               # call_id -> ToolCall
        self.open_scripts = []        # exec call ids still waiting for their output
        self.script_events = {}       # exec call id -> [Event, number of operations it ran]
        self.dropped = set()
        self.response_ids = set()
        self.last_total = None

    # -- helpers ------------------------------------------------------------
    def _assistant(self, ts) -> Event:
        if self.pending is None:
            self.pending = Event("assistant", ts, model=self.model)
            at = len(self.s.events) if self.pending_at is None else self.pending_at
            self.s.events.insert(at, self.pending)
            self.pending_at = None
        return self.pending

    def _close(self) -> None:
        self.pending, self.pending_at = None, None

    def _set_model(self, model) -> None:
        if isinstance(model, str) and model:
            self.model = model
            if model not in self.s.models:
                self.s.models.append(model)

    def _usage(self, ts, usage, response_id="") -> None:
        if usage is None:
            return
        ev = self._assistant(ts)
        ev.usage, ev.id = usage, response_id
        ev.model = ev.model or self.model
        self._close()

    def _tool(self, ts, call) -> Event:
        return _add(self.s, Event("tool", ts, tool=call, model=self.model, id=call.id))

    # -- dispatch -----------------------------------------------------------
    def feed(self, lineno, rec) -> None:
        kind, ts = rec.get("type"), rec.get("timestamp") or ""
        _track_time(self.s, ts)
        p = _as_dict(rec.get("payload"))
        if kind == "session_meta":
            self._meta(p)
        elif kind == "turn_context":
            self._close()
            self._set_model(p.get("model"))
        elif kind == "response_item":
            self._item(lineno, ts, p)
        elif kind == "event_msg":
            self._event(ts, p)
        elif kind == "token_usage_record":
            if self.use_records and p.get("response_id") not in self.response_ids:
                self.response_ids.add(p.get("response_id"))
                self._usage(ts, _cx_usage(p.get("usage")), str(p.get("response_id") or ""))
        elif kind == "compacted":
            _add(self.s, Event("compaction", ts, str(p.get("message") or "")))
        elif kind is None:
            _warn(self.s, lineno, "record without a type")
        elif kind not in _CX_QUIET_LINES:
            _warn(self.s, lineno, "unknown record type %s" % _name(kind))

    def _meta(self, p) -> None:
        if self.meta_seen:  # a later session_meta describes the thread this one forked from
            return
        self.meta_seen = True
        s = self.s
        s.id = str(p.get("id") or s.id)
        s.cwd = p.get("cwd") if isinstance(p.get("cwd"), str) else ""
        s.version = p.get("cli_version") if isinstance(p.get("cli_version"), str) else ""
        source = p.get("source")
        sub = source.get("subagent") if isinstance(source, dict) else None
        if sub is not None:
            spawn = sub.get("thread_spawn") if isinstance(sub, dict) else None
            s.is_subagent = True
            s.parent_id = str(p.get("parent_thread_id") or _as_dict(spawn).get("parent_thread_id") or "")
            self.all_injected = not isinstance(spawn, dict)

    def _event(self, ts, p) -> None:
        kind = p.get("type")
        if kind == "token_count" and not self.use_records:
            info = p.get("info")
            if isinstance(info, dict):
                total = _as_dict(info.get("total_token_usage")).get("total_tokens")
                if total != self.last_total:  # the same total repeated is the same response
                    self.last_total = total
                    self._usage(ts, _cx_usage(info.get("last_token_usage")))
        elif kind == "turn_aborted":
            self._close()
            _add(self.s, Event("interrupt", ts, str(p.get("reason") or "")))
        elif kind == "thread_settings_applied":
            self._set_model(_as_dict(p.get("thread_settings")).get("model"))
        elif kind == "item_completed":
            self._operation(ts, _as_dict(p.get("item")))

    def _item(self, lineno, ts, p) -> None:
        kind = p.get("type")
        if kind == "message":
            role = p.get("role")
            if role == "assistant":
                ev = self._assistant(ts)
                txt = _cx_text(p.get("content"))
                ev.text = ev.text + "\n" + txt if ev.text and txt else ev.text or txt
            else:
                self._close()
                self._message(ts, role, p.get("content"))
        elif kind in _CX_MODEL_ITEMS:
            if self.pending is None and self.pending_at is None:
                self.pending_at = len(self.s.events)
            if kind != "reasoning":
                self._call(ts, p)
        elif kind in _CX_OUTPUTS:
            self._close()
            self._result(lineno, p)
        elif kind not in _CX_QUIET_ITEMS:
            _warn(self.s, lineno, "unknown response item %s" % _name(kind))

    def _message(self, ts, role, content) -> None:
        typed, context = [], []
        for x in content if isinstance(content, list) else []:
            if isinstance(x, dict) and isinstance(x.get("text"), str):
                harness_made = role != "user" or self.all_injected or bool(_CX_CONTEXT_RE.match(x["text"]))
                (context if harness_made else typed).append(x["text"])
        if context:
            _add(self.s, Event("user", ts, "\n".join(context), injected=True))
        if typed:
            _add(self.s, Event("user", ts, "\n".join(typed)))

    def _call(self, ts, p) -> None:
        kind = p.get("type")
        name = p.get("name") if isinstance(p.get("name"), str) else kind
        namespace = p.get("namespace") if isinstance(p.get("namespace"), str) else ""
        call_id = str(p.get("call_id") or p.get("id") or "")
        if kind == "function_call":
            raw = p.get("arguments")
            try:
                args = json.loads(raw) if isinstance(raw, str) else raw
            except ValueError:
                args = None
            inp = args if isinstance(args, dict) else {"raw": str(raw)}
        elif kind == "custom_tool_call":
            inp = {"raw": str(p.get("input") or "")}
        else:
            inp = _as_dict(p.get("action"))
        full = namespace + "__" + name if namespace and not name.startswith(namespace) else name
        call = ToolCall(id=call_id, name=full, kind=_cx_kind(name, namespace), input=inp, ts=ts)
        if kind == "local_shell_call":
            call.kind, call.command = "shell", _cx_argv(inp.get("command"))
        elif kind == "web_search_call":
            call.kind, call.has_result = "web", p.get("status") == "completed"
        elif name == "exec" and kind == "custom_tool_call":
            call.kind, call.command = "shell", inp["raw"]
        elif call.kind == "shell":
            call.command = _cx_argv(inp.get("cmd") or inp.get("command"))
        elif name == "apply_patch":
            call.paths = [a or b for a, b in _CX_PATCH_RE.findall(inp.get("raw") or inp.get("input") or "")]
        if call_id:
            self.calls[call_id] = call
        ev = self._tool(ts, call)
        if name == "exec" and kind == "custom_tool_call":
            self.open_scripts.append(call_id)
            self.script_events[call_id] = [ev, 0]

    def _result(self, lineno, p) -> None:
        call = self.calls.get(p.get("call_id"))
        if call is None:
            _warn(self.s, lineno, "tool output without a matching call")
            return
        txt = _cx_text(p.get("output"))
        exit_code = None
        if txt.startswith("{"):  # older shell tools: {"output": ..., "metadata": {"exit_code": N}}
            try:
                legacy = json.loads(txt)
            except ValueError:
                legacy = None
            if isinstance(legacy, dict) and isinstance(legacy.get("metadata"), dict) and "output" in legacy:
                txt = str(legacy.get("output") or "")
                exit_code = legacy["metadata"].get("exit_code")
        if exit_code is None and call.kind in ("shell", "edit"):
            for rx in _CX_EXIT_RES:
                m = rx.search(txt[:2000])
                if m:
                    exit_code = int(m.group(1))
                    break
        call.has_result = True
        _set_output(call, txt)
        call.exit_code = exit_code if isinstance(exit_code, int) else None
        call.is_error = call.is_error or (call.exit_code not in (None, 0)) or txt.startswith("Script failed")
        if txt.strip() == "aborted" or txt.startswith("aborted by user"):
            call.interrupted = True
        call.denied = _cx_denial(txt)
        if call.denied:
            call.is_error = True
        script = self.script_events.pop(call.id, None)
        if script is not None:
            if call.id in self.open_scripts:
                self.open_scripts.remove(call.id)
            if script[1] and not call.denied:  # its operations stand in for it
                self.dropped.add(id(script[0]))

    def _operation(self, ts, item) -> None:
        kind = item.get("type")
        if kind not in ("CommandExecution", "FileChange", "McpToolCall", "Extension", "ImageView"):
            return
        item_id = str(item.get("id") or "")
        direct = self.calls.get(item_id)
        if direct is not None:  # the log entry of a call the model made directly
            if item.get("status") == "failed":
                direct.is_error = True
            return
        status = item.get("status")
        call = ToolCall(id=item_id, name="", kind="other", input={}, ts=ts,
                        has_result=status in (None, "completed", "failed"), is_error=status == "failed")
        if kind == "CommandExecution":
            call.name, call.kind = "exec_command", "shell"
            call.command = _cx_argv(item.get("command"))
            call.input = {"command": item.get("command"), "cwd": item.get("cwd")}
            call.paths = [c["path"] for c in item.get("parsed_cmd") or []
                          if isinstance(c, dict) and c.get("type") == "read" and isinstance(c.get("path"), str)]
            code = item.get("exit_code")
            call.exit_code = code if isinstance(code, int) and not isinstance(code, bool) else None
            call.is_error = call.is_error or call.exit_code not in (None, 0)
            out = item.get("aggregated_output")
            if not isinstance(out, str):
                out = "\n".join(x for x in (item.get("stdout"), item.get("stderr")) if isinstance(x, str) and x)
            _set_output(call, out)
        elif kind == "FileChange":
            changes = _as_dict(item.get("changes"))
            call.name, call.kind = "apply_patch", "edit"
            call.input = {"changes": {k: _as_dict(v).get("type") for k, v in changes.items()}}
            call.paths = list(changes) + [v["move_path"] for v in changes.values()
                                          if isinstance(v, dict) and isinstance(v.get("move_path"), str)]
            _set_output(call, "\n".join(x for x in (item.get("stdout"), item.get("stderr")) if isinstance(x, str) and x))
        elif kind == "McpToolCall":
            call.name, call.kind = "mcp__%s__%s" % (item.get("server"), item.get("tool")), "mcp"
            args = item.get("arguments")
            call.input = args if isinstance(args, dict) else {"raw": str(args)}
            result = item.get("result")
            _set_output(call, _cx_text(_as_dict(result).get("content")) if isinstance(result, dict) else _cx_text(result))
        elif kind == "Extension":
            name = item.get("kind") if isinstance(item.get("kind"), str) else "extension"
            call.name, call.kind, call.input = name, "web" if name.startswith("web") else "other", _as_dict(item.get("action"))
        else:
            path = item.get("path") if isinstance(item.get("path"), str) else ""
            call.name, call.kind, call.input, call.paths = "view_image", "read", {"path": path}, [path] if path else []
        call.denied = _cx_denial(call.output)
        if call.denied:
            call.is_error = True
        self._tool(ts, call)
        if self.open_scripts:
            self.script_events[self.open_scripts[-1]][1] += 1

    def finish(self) -> None:
        for ev, ran in self.script_events.values():
            if ran:
                self.dropped.add(id(ev))
        if self.dropped:
            self.s.events = [e for e in self.s.events if id(e) not in self.dropped]


def _cx_session(path) -> Session:
    m = _CX_NAME_RE.search(os.path.basename(path))
    return Session("codex", m.group(1) if m else os.path.basename(path), path)


def _cx_newest_response(rows):
    """Index of the first item of the newest response, or None."""
    newest, after_usage = None, True
    for i, (_lineno, rec) in enumerate(rows):
        kind, p = rec.get("type"), _as_dict(rec.get("payload"))
        if kind == "token_usage_record" or (kind == "event_msg" and p.get("type") == "token_count"):
            after_usage = True
        elif kind == "response_item" and after_usage and (
                p.get("type") in _CX_MODEL_ITEMS or (p.get("type") == "message" and p.get("role") == "assistant")):
            newest, after_usage = i, False
    return newest


def _file_matches(path, pattern) -> bool:
    """Search a file for a bytes pattern, 1 MB at a time, without parsing it."""
    try:
        with open(path, "rb") as fh:
            tail = b""
            while True:
                chunk = fh.read(1 << 20)
                if not chunk:
                    return False
                if pattern.search(tail + chunk):
                    return True
                tail = chunk[-256:]
    except OSError:
        return False


def _cx_total_before(path, offset) -> Optional[int]:
    """total_tokens of the last token_count event in the 4 MB before `offset`."""
    start = max(0, offset - (4 << 20))
    try:
        with open(path, "rb") as fh:
            fh.seek(start)
            data = fh.read(offset - start)
    except OSError:
        return None
    for line in reversed(data.split(b"\n")[1 if start else 0:]):
        if b"token_count" in line:
            try:
                rec = json.loads(line)
            except ValueError:
                continue
            p = _as_dict(_as_dict(rec).get("payload"))
            if _as_dict(rec).get("type") == "event_msg" and p.get("type") == "token_count" and isinstance(p.get("info"), dict):
                return _as_dict(p["info"].get("total_token_usage")).get("total_tokens")
    return None


def _load_codex(path) -> Session:
    s = _cx_session(path)
    if path.endswith(".zst"):
        _warn(s, 0, "compressed rollout (.jsonl.zst) skipped: reading zstd needs Python 3.14")
        return s
    reader = _Codex(s, _file_matches(path, _CX_USAGE_MARK))
    for lineno, rec in _records(path, s):
        reader.feed(lineno, rec)
    reader.finish()
    return s


# ---------------------------------------------------------------------------
# Gemini CLI (facts Q1.3)
# ---------------------------------------------------------------------------

_GM_KINDS = {
    "run_shell_command": "shell", "read_file": "read", "read_many_files": "read", "write_file": "write",
    "replace": "edit", "edit": "edit", "glob": "search", "search_file_content": "search",
    "grep_search": "search", "grep": "search", "list_directory": "search", "ls": "search",
    "google_web_search": "web", "web_search": "web", "web_fetch": "web",
}
_GM_EXIT_RE = re.compile(r"^Exit Code: (-?\d+)", re.M)


def _gm_kind(name) -> str:
    if name in _GM_KINDS:
        return _GM_KINDS[name]
    return "mcp" if name.startswith("mcp_") or "__" in name else "other"


def _gm_usage(t) -> Optional[Usage]:
    """Gemini meanings (cached is part of input; thoughts are not in output) to Usage."""
    if not isinstance(t, dict):
        return None
    cached, thoughts = _int(t.get("cached")), _int(t.get("thoughts"))
    return Usage(input=max(0, _int(t.get("input")) - cached), cache_read=cached,
                 output=_int(t.get("output")) + thoughts, reasoning=thoughts)


def _gm_result(result) -> str:
    parts = []
    for part in result if isinstance(result, list) else []:
        response = _as_dict(_as_dict(part).get("functionResponse")).get("response")
        if isinstance(response, dict):
            value = response.get("output", response.get("error"))
            parts.append(value if isinstance(value, str) else json.dumps(response))
        elif isinstance(_as_dict(part).get("text"), str):
            parts.append(part["text"])
    return "\n".join(parts)


def _gm_call(tc, ts) -> ToolCall:
    name = tc.get("name") if isinstance(tc.get("name"), str) else ""
    args = tc.get("args") if isinstance(tc.get("args"), dict) else {}
    status = tc.get("status")
    call = ToolCall(id=str(tc.get("id") or ""), name=name, kind=_gm_kind(name), input=args,
                    ts=tc.get("timestamp") if isinstance(tc.get("timestamp"), str) else ts,
                    has_result=status in ("success", "error", "cancelled"), is_error=status == "error",
                    denied="other" if status == "cancelled" else "")
    if call.kind == "shell":
        call.command = str(args.get("command") or "")
    elif call.kind in ("read", "write", "edit"):
        many = args.get("paths")
        one = args.get("absolute_path") or args.get("file_path") or args.get("path")
        call.paths = [p for p in many if isinstance(p, str)] if isinstance(many, list) else (
            [one] if isinstance(one, str) and one else [])
    text = _gm_result(tc.get("result"))
    if not text and isinstance(tc.get("resultDisplay"), str):
        text = tc["resultDisplay"]
    _set_output(call, text)
    if call.kind == "shell":
        m = _GM_EXIT_RE.search(text)
        if m:
            call.exit_code = int(m.group(1))
            call.is_error = call.is_error or call.exit_code != 0
    return call


def _gm_project_dir(path) -> str:
    """The <project temp dir> that holds this chat file."""
    chats = os.path.dirname(path)
    if os.path.basename(chats) != "chats":  # a subagent chat: chats/<parent session id>/<id>.jsonl
        chats = os.path.dirname(chats)
    return os.path.dirname(chats)


def _gm_project_root(folder) -> str:
    try:
        with open(os.path.join(folder, ".project_root"), encoding="utf-8") as fh:
            return fh.read().strip()
    except OSError:
        pass
    try:
        with open(os.path.join(os.path.dirname(os.path.dirname(folder)), "projects.json"), encoding="utf-8") as fh:
            projects = _as_dict(_as_dict(json.load(fh)).get("projects"))
    except (OSError, ValueError):
        return ""
    name = os.path.basename(folder)  # a slug, or sha256(root) for folders made by older versions
    return next((root for root, slug in projects.items()
                 if name in (slug, hashlib.sha256(root.encode("utf-8", "surrogateescape")).hexdigest())), "")


def _load_gemini(path) -> Session:
    stem = os.path.splitext(os.path.basename(path))[0]
    s = Session("gemini-cli", stem, path)
    meta, order, messages = {}, [], {}

    def keep(m):
        if isinstance(m, dict) and isinstance(m.get("id"), str):
            if m["id"] not in messages:
                order.append(m["id"])
            messages[m["id"]] = m  # a later line with the same id replaces the earlier one

    if path.endswith(".json"):  # legacy: one JSON document
        try:
            with open(path, encoding="utf-8") as fh:
                doc = json.load(fh)
        except (OSError, ValueError) as exc:
            _warn(s, 0, "cannot read file: %s" % type(exc).__name__)
            return s
        meta = _as_dict(doc)
        for m in meta.get("messages") or []:
            keep(m)
    else:  # the current format is an operation log: replay it in order
        for lineno, rec in _records(path, s):
            if "$set" in rec:
                update = _as_dict(rec["$set"])
                if isinstance(update.get("messages"), list):  # a checkpoint replaces all messages
                    order, messages = [], {}
                    for m in update["messages"]:
                        keep(m)
                meta.update((k, v) for k, v in update.items() if k != "messages")
            elif "$rewindTo" in rec:  # drop that message and everything after it
                target = rec["$rewindTo"]
                cut = order.index(target) if target in messages else 0
                for mid in order[cut:]:
                    del messages[mid]
                del order[cut:]
            elif isinstance(rec.get("id"), str):
                keep(rec)
            elif isinstance(rec.get("sessionId"), str):
                meta.update(rec)
            else:
                _warn(s, lineno, "unknown line shape")
    s.id = meta.get("sessionId") if isinstance(meta.get("sessionId"), str) else stem
    s.cwd = _gm_project_root(_gm_project_dir(path))
    parent_dir = os.path.basename(os.path.dirname(path))
    if meta.get("kind") == "subagent" or parent_dir != "chats":
        s.is_subagent = True
        s.parent_id = parent_dir if parent_dir != "chats" else ""
    for key in ("startTime", "lastUpdated"):
        _track_time(s, meta.get(key))
    for mid in order:
        m = messages[mid]
        kind, ts = m.get("type"), m.get("timestamp") if isinstance(m.get("timestamp"), str) else ""
        _track_time(s, ts)
        text = _block_text(m.get("content"))
        if kind == "user":
            _add(s, Event("user", ts, text, id=mid))
        elif kind == "gemini":
            model = m.get("model") if isinstance(m.get("model"), str) else ""
            if model and model not in s.models:
                s.models.append(model)
            _add(s, Event("assistant", ts, text, usage=_gm_usage(m.get("tokens")), model=model, id=mid))
            for tc in m.get("toolCalls") or []:
                if isinstance(tc, dict):
                    call = _gm_call(tc, ts)
                    _add(s, Event("tool", call.ts, tool=call, model=model, id=call.id))
        elif kind == "error":
            _add(s, Event("api_error", ts, text, id=mid))
        elif kind not in ("info", "warning"):
            _warn(s, 0, "unknown message type %s" % _name(kind))
    return s


# ---------------------------------------------------------------------------
# OpenCode (facts Q1.5). Built from the documented database schema; not
# verified on a real install. Session paths are '<database file>#<session id>'.
# ---------------------------------------------------------------------------

_OC_KINDS = {"bash": "shell", "read": "read", "write": "write", "edit": "edit", "multiedit": "edit",
             "patch": "edit", "grep": "search", "glob": "search", "list": "search", "webfetch": "web",
             "websearch": "web", "task": "agent"}
# Error texts for permission denials (unverified wording from the OpenCode source).
_OC_DENIALS = (("rejected permission", "user-rejected"), ("specified a rule which prevents", "permission-rule"))


def _iso_ms(ms) -> str:
    if not isinstance(ms, (int, float)) or isinstance(ms, bool):
        return ""
    stamp = datetime.datetime.fromtimestamp(ms / 1000.0, tz=datetime.timezone.utc)
    return stamp.strftime("%Y-%m-%dT%H:%M:%S.") + "%03dZ" % (int(ms) % 1000)


def _oc_connect(db):
    """Open the database read-only; it is never written."""
    uri = pathlib.Path(os.path.abspath(db)).as_uri() + "?mode=ro"
    return sqlite3.connect(uri, uri=True)


def _oc_usage(t) -> Optional[Usage]:
    # Recorded values are kept as they are: input without cache, output as the
    # provider reported it, reasoning as its own count (unverified on a real install).
    if not isinstance(t, dict):
        return None
    cache = _as_dict(t.get("cache"))
    return Usage(input=_int(t.get("input")), cache_read=_int(cache.get("read")),
                 cache_write=_int(cache.get("write")), output=_int(t.get("output")),
                 reasoning=_int(t.get("reasoning")))


def _oc_error_text(err) -> str:
    err = _as_dict(err)
    msg = _as_dict(err.get("data")).get("message")
    return msg if isinstance(msg, str) else str(err.get("name") or "")


def _oc_call(part, ts) -> ToolCall:
    name = part.get("tool") if isinstance(part.get("tool"), str) else ""
    state = _as_dict(part.get("state"))
    inp = state.get("input") if isinstance(state.get("input"), dict) else {}
    status = state.get("status")
    start = _as_dict(state.get("time")).get("start")
    call = ToolCall(id=str(part.get("callID") or ""), name=name, kind=_OC_KINDS.get(name, "other"), input=inp,
                    ts=_iso_ms(start) or ts, has_result=status in ("completed", "error"), is_error=status == "error")
    if call.kind == "shell":
        call.command = str(inp.get("command") or "")
    elif name == "patch":
        call.paths = [a or b for a, b in _CX_PATCH_RE.findall(str(inp.get("patchText") or ""))]
    elif isinstance(inp.get("filePath"), str) and call.kind in ("read", "write", "edit"):
        call.paths = [inp["filePath"]]
    text = state.get("output") if status == "completed" else state.get("error")
    _set_output(call, text if isinstance(text, str) else "")
    exit_code = _as_dict(state.get("metadata")).get("exit")
    if call.kind == "shell" and isinstance(exit_code, int) and not isinstance(exit_code, bool):
        call.exit_code = exit_code
        call.is_error = call.is_error or exit_code != 0
    if status == "error":
        lowered = call.output.lower()
        call.denied = next((kind for marker, kind in _OC_DENIALS if marker in lowered), "")
        call.interrupted = "aborted" in lowered
    return call


def _oc_assistant(s, message_id, data, parts) -> None:
    ts = _iso_ms(_as_dict(data.get("time")).get("created"))
    model = data.get("modelID") if isinstance(data.get("modelID"), str) else ""
    if model and model not in s.models:
        s.models.append(model)
    step, stepped, steps = None, False, 0  # one Event per model step: a step-finish part holds that call's tokens

    def current():
        nonlocal step, steps
        if step is None:
            steps += 1
            step = _add(s, Event("assistant", ts, model=model, id="%s:%d" % (message_id, steps)))
        return step

    for part in parts:
        kind = part.get("type")
        if kind == "step-start":
            step = None
            current()
        elif kind == "text" and isinstance(part.get("text"), str):
            ev = current()
            ev.text = ev.text + "\n" + part["text"] if ev.text else part["text"]
        elif kind == "tool":
            call = _oc_call(part, ts)
            current()
            _add(s, Event("tool", call.ts, tool=call, model=model, id=call.id))
        elif kind == "step-finish":
            current().usage = _oc_usage(part.get("tokens"))
            step, stepped = None, True
        elif kind == "retry":
            _add(s, Event("api_error", ts, _oc_error_text(part.get("error")), id=message_id))
        elif kind == "compaction":
            _add(s, Event("compaction", ts, id=message_id))
    if not stepped:  # no step parts: the message holds the tokens of its one call
        usage = _oc_usage(data.get("tokens"))
        if step is not None or (usage is not None and usage != Usage()):
            current().usage = usage
    err = data.get("error")
    if isinstance(err, dict):
        aborted = "abort" in str(err.get("name") or "").lower()
        _add(s, Event("interrupt" if aborted else "api_error", ts, _oc_error_text(err), id=message_id))


def _oc_user(s, message_id, data, parts) -> None:
    ts = _iso_ms(_as_dict(data.get("time")).get("created"))
    for part in parts:
        if part.get("type") == "text" and isinstance(part.get("text"), str):
            _add(s, Event("user", ts, part["text"], injected=bool(part.get("synthetic")), id=message_id))
        elif part.get("type") == "compaction":
            _add(s, Event("compaction", ts, id=message_id))


def _oc_json(s, raw) -> dict:
    try:
        value = json.loads(raw)
    except (TypeError, ValueError):
        _warn(s, 0, "malformed JSON in the database")
        return {}
    return _as_dict(value)


def _load_opencode(path) -> Session:
    db, _, sid = path.rpartition("#")
    s = Session("opencode", sid, path)
    try:
        con = _oc_connect(db)
        try:
            con.row_factory = sqlite3.Row
            row = con.execute("SELECT * FROM session WHERE id = ?", (sid,)).fetchone()
            messages = con.execute("SELECT id, data FROM message WHERE session_id = ? ORDER BY id", (sid,)).fetchall()
            parts = con.execute("SELECT message_id, data FROM part WHERE session_id = ? ORDER BY id", (sid,)).fetchall()
        finally:
            con.close()
    except sqlite3.Error as exc:
        _warn(s, 0, "cannot read database: %s" % type(exc).__name__)
        return s
    if row is not None:
        info = dict(row)
        s.version = str(info.get("version") or "")
        s.parent_id = str(info.get("parent_id") or "")
        s.is_subagent = bool(s.parent_id)
        for key in ("time_created", "time_updated"):
            _track_time(s, _iso_ms(info.get(key)))
    by_message = {}
    for message_id, raw in parts:
        by_message.setdefault(message_id, []).append(_oc_json(s, raw))
    for message_id, raw in messages:
        data = _oc_json(s, raw)
        own = by_message.get(message_id, [])
        if data.get("role") == "assistant":
            if not s.cwd and isinstance(_as_dict(data.get("path")).get("cwd"), str):
                s.cwd = data["path"]["cwd"]
            _oc_assistant(s, message_id, data, own)
        elif data.get("role") == "user":
            _oc_user(s, message_id, data, own)
        else:
            _warn(s, 0, "unknown message role %s" % _name(data.get("role")))
    for e in s.events:
        _track_time(s, e.ts)
    return s


# ---------------------------------------------------------------------------
# Where each harness keeps its files
# ---------------------------------------------------------------------------

def _roots(home=None) -> dict:
    """Root folders per harness. `home` stands in for the home folder (tests);
    when it is given, CLAUDE_CONFIG_DIR, CODEX_HOME, XDG_DATA_HOME, and
    OPENCODE_DB are ignored so that nothing outside `home` is read."""
    if home is None:
        env, base = os.environ, os.path.expanduser("~")
    else:
        env, base = {}, str(home)
    data = env.get("XDG_DATA_HOME") or os.path.join(base, ".local", "share")
    return {
        "claude-code": env.get("CLAUDE_CONFIG_DIR") or os.path.join(base, ".claude"),
        "codex": env.get("CODEX_HOME") or os.path.join(base, ".codex"),
        "gemini-cli": os.path.join(base, ".gemini"),
        "opencode": os.path.join(data, "opencode"),
        "opencode-db": env.get("OPENCODE_DB") or "",
    }


def _scan(folder):
    try:
        return list(os.scandir(folder))
    except OSError:
        return []


def _mtime(entry) -> float:
    try:
        return entry.stat().st_mtime
    except OSError:
        return 0.0


def _norm(path) -> str:
    return os.path.normpath(os.path.abspath(os.path.expanduser(str(path))))


def _under(cwd, project) -> bool:
    if not cwd:
        return False
    cwd = os.path.normpath(cwd)
    return cwd == project or cwd.startswith(project.rstrip(os.sep) + os.sep)


def _cc_encode(path) -> str:
    return re.sub(r"[^A-Za-z0-9]", "-", path)


def _cc_first_cwd(path) -> str:
    probe = Session("claude-code", "", path)
    for lineno, rec in _records(path, probe):
        if isinstance(rec.get("cwd"), str) and rec["cwd"]:
            return rec["cwd"]
        if lineno >= 50:
            break
    return ""


def _find_claude(roots, cutoff, project, include_subagents) -> list:
    found = []
    enc = _cc_encode(project) if project else ""
    for folder in _scan(os.path.join(roots["claude-code"], "projects")):
        if not folder.is_dir():
            continue
        # The folder name is the lossy encoded cwd (cut to 200 characters plus a
        # hash when long), so it only preselects; the recorded cwd decides.
        if project and not (folder.name == enc or folder.name.startswith(enc + "-")
                            or (len(enc) > 200 and folder.name.startswith(enc[:200]))):
            continue
        candidates = []
        for entry in _scan(folder.path):
            if entry.is_file():
                candidates.append(entry)
            elif include_subagents and entry.is_dir():  # agent-<id>.jsonl only: workflows also keep a journal.jsonl
                subagents = os.path.join(entry.path, "subagents")
                folders = [subagents] + [f.path for f in _scan(os.path.join(subagents, "workflows"))]
                candidates.extend(e for f in folders for e in _scan(f) if e.name.startswith("agent-") and e.is_file())
        for entry in candidates:
            name = entry.name
            if not name.endswith(".jsonl") or ".orphaned-" in name or ".superseded-" in name:
                continue
            mtime = _mtime(entry)
            if cutoff is not None and mtime < cutoff:
                continue
            if project:
                cwd = _cc_first_cwd(entry.path)
                if not (_under(cwd, project) if cwd else folder.name == enc):
                    continue
            found.append((mtime, "claude-code", entry.path))
    return found


def _cx_meta(path) -> dict:
    """The session_meta payload on the first line of a rollout, or {}."""
    try:
        with open(path, "rb") as fh:
            rec = json.loads(fh.readline())
    except (OSError, ValueError):
        return {}
    return _as_dict(rec.get("payload")) if isinstance(rec, dict) and rec.get("type") == "session_meta" else {}


def _find_codex(roots, cutoff, project, include_subagents) -> list:
    entries = []
    for year in _scan(os.path.join(roots["codex"], "sessions")):
        for month in _scan(year.path):
            for day in _scan(month.path):
                entries.extend(_scan(day.path))
    entries.extend(_scan(os.path.join(roots["codex"], "archived_sessions")))
    found = []
    for entry in entries:
        name = entry.name
        if not name.startswith("rollout-") or not (name.endswith(".jsonl") or name.endswith(".jsonl.zst")):
            continue
        mtime = _mtime(entry)
        if cutoff is not None and mtime < cutoff:
            continue
        if name.endswith(".zst"):
            if project:  # cannot read the working folder of a compressed file
                continue
        elif project or not include_subagents:
            meta = _cx_meta(entry.path)
            source = meta.get("source")
            if not include_subagents and isinstance(source, dict) and "subagent" in source:
                continue
            if project and not _under(meta.get("cwd") if isinstance(meta.get("cwd"), str) else "", project):
                continue
        found.append((mtime, "codex", entry.path))
    return found


def _find_gemini(roots, cutoff, project, include_subagents) -> list:
    found = []
    legacy_name = hashlib.sha256(project.encode("utf-8", "surrogateescape")).hexdigest() if project else ""
    for folder in _scan(os.path.join(roots["gemini-cli"], "tmp")):
        if not folder.is_dir():
            continue
        if project and folder.name != legacy_name and not _under(_gm_project_root(folder.path), project):
            continue
        for entry in _scan(os.path.join(folder.path, "chats")):  # stat only; nothing is opened here
            if entry.is_dir():
                if include_subagents:
                    found.extend((_mtime(e), "gemini-cli", e.path) for e in _scan(entry.path)
                                 if e.name.endswith(".jsonl"))
            elif entry.name.startswith("session-") and entry.name.endswith((".jsonl", ".json")):
                found.append((_mtime(entry), "gemini-cli", entry.path))
    return [f for f in found if cutoff is None or f[0] >= cutoff]


def _oc_databases(roots) -> list:
    if roots["opencode-db"]:
        return [roots["opencode-db"]] if os.path.isfile(roots["opencode-db"]) else []
    return sorted(e.path for e in _scan(roots["opencode"])
                  if e.is_file() and e.name.startswith("opencode") and e.name.endswith(".db"))


def _find_opencode(roots, cutoff, project, include_subagents) -> list:
    found = []
    for db in _oc_databases(roots):
        changed = max(os.path.getmtime(p) for p in (db, db + "-wal") if os.path.exists(p))
        if cutoff is not None and changed < cutoff:
            continue
        try:
            con = _oc_connect(db)
            try:
                columns = [r[1] for r in con.execute("PRAGMA table_info(session)")]
                stamp = next((c for c in ("time_updated", "time_created") if c in columns), None)
                rows = con.execute("SELECT id, parent_id, %s FROM session" % (stamp or "NULL")).fetchall()
                cwds = {}
                if project:
                    for sid, raw in con.execute("SELECT session_id, data FROM message ORDER BY id"):
                        if sid not in cwds and '"cwd"' in (raw or ""):
                            cwd = _as_dict(_oc_json(Session("opencode", sid, db), raw).get("path")).get("cwd")
                            if isinstance(cwd, str):
                                cwds[sid] = cwd
            finally:
                con.close()
        except sqlite3.Error:
            continue
        for sid, parent, updated in rows:
            when = updated / 1000.0 if isinstance(updated, (int, float)) else changed
            if (cutoff is not None and when < cutoff) or (parent and not include_subagents):
                continue
            if project and not _under(cwds.get(sid, ""), project):
                continue
            found.append((when, "opencode", "%s#%s" % (db, sid)))
    return found


def detect_harnesses(home=None) -> dict:
    """Map each installed harness to its root folder (only harnesses with data folders)."""
    roots = _roots(home)
    checks = {
        "claude-code": [os.path.join(roots["claude-code"], "projects")],
        "codex": [os.path.join(roots["codex"], "sessions"), os.path.join(roots["codex"], "archived_sessions")],
        "gemini-cli": [os.path.join(roots["gemini-cli"], "tmp")],
    }
    found = {h: roots[h] for h in HARNESSES if any(os.path.isdir(p) for p in checks.get(h, []))}
    databases = _oc_databases(roots)
    if databases:
        found["opencode"] = os.path.dirname(databases[0])
    return found


# ---------------------------------------------------------------------------
# Public entry points
# ---------------------------------------------------------------------------

def load_session(harness, path) -> Session:
    """Parse one session file (OpenCode: '<database>#<session id>')."""
    loaders = {"claude-code": _load_claude, "codex": _load_codex, "gemini-cli": _load_gemini,
               "opencode": _load_opencode}
    if harness not in loaders:
        raise ValueError("unknown harness %r; expected one of %s" % (harness, ", ".join(HARNESSES)))
    return loaders[harness](str(path))


_FINDERS = {"claude-code": _find_claude, "codex": _find_codex, "gemini-cli": _find_gemini,
            "opencode": _find_opencode}


def find_sessions(harness=None, since_days=30, project=None, home=None,
                  include_subagents=True) -> list:
    """List (harness, path) pairs, newest first. Files are filtered by modified
    time before any is opened. `project` keeps sessions whose working folder is
    that folder or inside it. `since_days=None` means no time limit."""
    if harness in (None, "all"):
        wanted = HARNESSES
    elif harness == "cursor":
        return []  # Cursor transcripts have no results, usage, or timestamps
    elif harness in HARNESSES:
        wanted = (harness,)
    else:
        raise ValueError("unknown harness %r; expected one of %s" % (harness, ", ".join(HARNESSES)))
    cutoff = None if not since_days else time.time() - float(since_days) * 86400
    roots = _roots(home)
    project = _norm(project) if project else None
    found = []
    for h in wanted:
        if h in _FINDERS:
            found.extend(_FINDERS[h](roots, cutoff, project, include_subagents))
    found.sort(key=lambda item: item[0], reverse=True)
    return [(h, p) for _mtime_, h, p in found]


def iter_sessions(**kwargs):
    """Yield each Session that find_sessions(**kwargs) lists."""
    for harness, path in find_sessions(**kwargs):
        yield load_session(harness, path)


def cutoff(days) -> str:
    """The UTC time `days` days ago, in the form event timestamps use, for
    unique_events(since=...)."""
    stamp = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(days=days)
    return stamp.strftime("%Y-%m-%dT%H:%M:%S")


def unique_events(sessions, since=None):
    """Yield (session, event) for every event across `sessions`, oldest session
    first, skipping an event whose (kind, id) an earlier session already had.
    A forked or resumed Claude Code session starts with a copy of the earlier
    session's records, ids included; totals must count that copy once.
    With `since` (an ISO 8601 UTC time, see cutoff()), events older than it
    are skipped too: find_sessions picks files by modified time, and a
    recently modified file can hold records months old. Events without a
    timestamp are kept."""
    seen = set()
    floor = since[:19] if since else ""
    for s in sorted(sessions, key=lambda s: s.started or s.ended or ""):
        for e in s.events:
            if floor and e.ts and e.ts[:19] < floor:
                continue
            if e.id:
                if (e.kind, e.id) in seen:
                    continue
                seen.add((e.kind, e.id))
            yield s, e


def _incremental_reader(harness, session, path, offset):
    if harness == "claude-code":
        return _Claude(session)
    reader = _Codex(session, _file_matches(path, _CX_USAGE_MARK))  # the source is chosen per file
    if not reader.use_records:
        reader.last_total = _cx_total_before(path, offset)
    return reader


_INCREMENTAL = {"claude-code": (_cc_session, _cc_newest_response), "codex": (_cx_session, _cx_newest_response)}


def read_new_events(harness, path, offset) -> tuple:
    """Read the lines added since `offset` and return (events, new_offset).

    For hooks that re-read a growing transcript. Only whole lines are read. The
    newest API response, and everything after it, is held back until the next
    response starts, because its records are still being written; so every
    response comes back exactly once, with its final usage and with every tool
    result linked to its call. Pass new_offset to the next call. Supported for
    claude-code and codex (JSONL files that only grow). Subagent files are
    separate files: read each one with its own offset."""
    if harness not in _INCREMENTAL:
        raise ValueError("read_new_events supports %s, not %r" % (", ".join(sorted(_INCREMENTAL)), harness))
    make_session, newest = _INCREMENTAL[harness]
    session = make_session(str(path))
    rows, starts = [], []
    end = offset
    try:
        for lineno, start, raw in _raw_lines(str(path), offset, complete_only=True):
            end = start + len(raw)
            rec = _parse(session, lineno, raw)
            if rec is not None:
                rows.append((lineno, rec))
                starts.append(start)
    except OSError:
        return [], offset
    cut = newest(rows)
    if cut is not None:
        end = starts[cut]
        rows = rows[:cut]
    reader = _incremental_reader(harness, session, str(path), offset)
    for lineno, rec in rows:
        reader.feed(lineno, rec)
    reader.finish()
    return session.events, end
