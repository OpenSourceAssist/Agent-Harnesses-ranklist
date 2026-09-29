#!/usr/bin/env python3
"""Runaway guard: a PreToolUse hook for Claude Code and Codex.

Before each tool call it checks three trip wires and answers the harness:
  loop      the same tool call a third time with nothing changed in between: deny
  failures  five failed tool calls in a row: ask the user (Codex: deny, with a
            reason that tells the agent to ask the user, because Codex hooks cannot ask)
  spend     running cost from the session transcripts: warn once at 80% of the
            cap, deny every tool call at 100%

Settings: runaway-guard.json in the state folder (user) or in the project's
.claude/ or .codex/ folder, and RUNAWAY_GUARD_* environment variables. A
project file can lower a limit but never raise one set for the user.

The hook reads the hook JSON on stdin and prints a decision as JSON on stdout.
It always exits 0: exit code 2 would block the tool call, so a bug or a bad
flag in the guard must never produce it. On any internal error it allows the
call and appends one line to errors.log in the state folder.

Python 3.9+, standard library only. Nothing leaves the machine.
"""

from __future__ import annotations

import argparse
import datetime
import hashlib
import json
import os
import re
import shlex
import sys
import time

try:
    import fcntl
except ImportError:  # Windows has no fcntl: the guard then runs without a lock
    fcntl = None

import pricing
import transcripts

STATE_VERSION = 2
CONFIG_NAME = "runaway-guard.json"
DEFAULTS = {"spend_cap_usd": 10.0, "warn_at": 0.8, "loop_repeats": 3, "failure_streak": 5}
ENV_NAMES = {"spend_cap_usd": "RUNAWAY_GUARD_CAP_USD", "warn_at": "RUNAWAY_GUARD_WARN_AT",
             "loop_repeats": "RUNAWAY_GUARD_LOOP_REPEATS", "failure_streak": "RUNAWAY_GUARD_FAILURE_STREAK"}
LOCK_TIMEOUT = 2.0      # seconds to wait for a parallel call of the same session
MAX_COUNTS = 64         # loop fingerprints kept per agent
MAX_AGENTS = 100        # agents (main plus subagents) kept per session
MAX_TRIPS = 50
PRUNE_DAYS = 30        # session files older than this are removed
SHARED_IDS = "counted-ids.bin"  # responses counted by any session: 8-byte id hash + 8-byte session hash
MAX_SHARED_IDS = 32768
MAX_SESSION_IDS = 4096

# Calls that change nothing, so repeats with only these in between still count.
READ_ONLY_TOOLS = {"Read", "Grep", "Glob", "LS", "NotebookRead", "WebFetch", "WebSearch", "ToolSearch"}
# Calls whose repeats are deliberate: they start or wait for subagents, wait for
# background work, or ask the user. They never count, and they end a run of repeats.
NEVER_LOOP = {"agent", "task", "spawn_agent", "followup_task", "send_message", "sendmessage", "wait",
              "wait_agent", "sleep", "taskoutput", "bashoutput", "monitor", "schedulewakeup", "list_agents",
              "listagents", "askuserquestion", "exitplanmode", "request_user_input",
              "request_user_input_async"}
# Browser steps that repeat on purpose: scrolling, key presses, and waits.
NAV_ACTIONS = {"scroll", "key", "press_key", "wait"}
# Calls that stop work or hand in a final answer: they still pass at the spend cap.
STOP_TOOLS = {"TaskStop", "KillShell", "KillBash", "StructuredOutput"}
SHELL_TOOLS = {"Bash", "exec_command", "shell", "shell_command", "local_shell"}
# A model missing from the price table is priced like the dearest known model of its family.
FAMILIES = ("claude-opus", "claude-sonnet", "claude-haiku", "claude-fable", "gpt")
POLL_RE = re.compile(r"\b(?:sleep|wait)\b")   # a command that waits on purpose is a poll, not a loop
_SAFE_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")


# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------

def _now_iso() -> str:
    return datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def state_dir(env=None) -> str:
    """${XDG_STATE_HOME:-~/.local/state}/runaway-guard"""
    env = os.environ if env is None else env
    base = env.get("XDG_STATE_HOME") or ""
    if not os.path.isabs(base):
        home = env.get("HOME") or os.path.expanduser("~")
        base = os.path.join(home, ".local", "state")
    return os.path.join(base, "runaway-guard")


def session_stem(session_id) -> str:
    """A file name for a session id: the id itself when it is plain, else a hash."""
    if _SAFE_ID_RE.match(session_id) and session_id != "runaway-guard":
        return session_id
    return "s-" + hashlib.sha256(session_id.encode("utf-8", "surrogatepass")).hexdigest()[:32]


def _read_json(path) -> dict:
    try:
        with open(path, encoding="utf-8") as fh:
            value = json.load(fh)
    except (OSError, ValueError):
        return {}
    return value if isinstance(value, dict) else {}


def _valid(key, value):
    """A setting as a number, or None when it is missing or out of range."""
    if isinstance(value, bool) or value is None:
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if number != number or number in (float("inf"), float("-inf")):
        return None
    if key == "warn_at":
        return number if 0 < number <= 1 else None
    if key == "spend_cap_usd":
        return number if number >= 0 else None
    if number != int(number):
        return None
    number = int(number)
    if key == "loop_repeats":
        return number if number == 0 or number >= 2 else None
    return number if number >= 0 else None      # failure_streak


def _stricter(a, b) -> bool:
    """True when limit a is stricter than limit b (0 means off)."""
    return a != 0 and (b == 0 or a < b)


def project_config_path(project_dir) -> str:
    """The project's runaway-guard.json (.claude/ first, then .codex/), or ''."""
    for sub in (".claude", ".codex"):
        path = os.path.join(project_dir, sub, CONFIG_NAME) if project_dir else ""
        if path and os.path.isfile(path):
            return path
    return ""


def load_limits(env, project_dir):
    """(limits, sources) for a session whose project folder is `project_dir`."""
    project = project_config_path(project_dir)
    user = os.path.join(state_dir(env), CONFIG_NAME)
    return merge_limits(env, _read_json(user), _read_json(project) if project else {}, user, project)


def merge_limits(env, user, project, user_path="", project_path=""):
    """Environment, then the user file; a project file can only make a limit stricter,
    except where neither of the first two sets it. Returns (limits, sources), where
    each source is ('environment', variable), ('user', file), ('project', file),
    or ('default', '')."""
    limits, sources = {}, {}
    for key, default in DEFAULTS.items():
        base, source = _valid(key, env.get(ENV_NAMES[key])), ("environment", ENV_NAMES[key])
        if base is None:
            base, source = _valid(key, user.get(key)), ("user", user_path)
        local = _valid(key, project.get(key))
        if base is None:
            base, source = default, ("default", "")
            if local is not None:
                base, source = local, ("project", project_path)
        elif local is not None and _stricter(local, base):
            base, source = local, ("project", project_path)
        limits[key], sources[key] = base, source
    return limits, sources


def detect_harness(hook, flag=None) -> str:
    if flag in ("claude-code", "codex"):
        return flag
    path = hook.get("transcript_path") or ""
    if "turn_id" in hook or os.path.basename(str(path)).startswith("rollout-"):
        return "codex"
    return "claude-code"


# ---------------------------------------------------------------------------
# State: one JSON file per session, guarded by a lock file
# ---------------------------------------------------------------------------

def new_state(session_id, harness) -> dict:
    now = _now_iso()
    return {"version": STATE_VERSION, "session_id": session_id, "harness": harness, "created": now,
            "created_ts": time.time(), "updated": now, "files": {}, "cost_usd": 0.0, "baseline_usd": 0.0,
            "estimated_usd": 0.0, "estimated_tokens": 0, "estimated_models": [], "estimate_noted": False,
            "warned_cap": None, "streaks": {}, "agents": {},
            "denied_ids": [], "trips": [], "counts": {"loop": 0, "failures": 0, "spend": 0, "warning": 0},
            "limits": {}}


def load_state(path):
    state = _read_json(path)
    return state if state.get("version") == STATE_VERSION else None


def save_state(path, state) -> None:
    tmp = "%s.%d.tmp" % (path, os.getpid())
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(state, fh, separators=(",", ":"))
    os.replace(tmp, path)


def prune(folder, keep) -> None:
    """Remove session files not touched for PRUNE_DAYS (settings, the error log, and
    the shared list of counted responses stay). A lock file stays while its session
    file is fresh: a lock file's own time does not change while it is in use."""
    cutoff = time.time() - PRUNE_DAYS * 86400
    for e in _scandir(folder):
        if (e.name in (CONFIG_NAME, "errors.log", SHARED_IDS) or e.name.startswith(keep + ".")
                or e.name.startswith(SHARED_IDS + ".")):
            continue
        if e.name.endswith((".json", ".lock", ".tmp")):
            try:
                if e.name.endswith(".lock") and os.stat(e.path[:-len(".lock")] + ".json").st_mtime >= cutoff:
                    continue
            except OSError:
                pass
            try:
                if e.stat().st_mtime < cutoff:
                    os.remove(e.path)
            except OSError:
                pass


def _acquire(path):
    """An open, locked file descriptor; -1 when locks are not available; None on timeout."""
    if fcntl is None:
        return -1
    fd = os.open(path, os.O_CREAT | os.O_RDWR, 0o600)
    deadline = time.monotonic() + LOCK_TIMEOUT
    while True:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return fd
        except OSError:
            if time.monotonic() >= deadline:
                os.close(fd)
                return None
            time.sleep(0.005)


def _release(fd) -> None:
    if fd is not None and fd >= 0:
        try:
            fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            os.close(fd)


def _record(state, wire, tool, detail, log=True) -> None:
    """Count one trip; `log` adds it to the list of recent trips as well."""
    state["counts"][wire] = state["counts"].get(wire, 0) + 1
    if log:
        state["trips"] = (state["trips"] + [{"wire": wire, "at": _now_iso(), "tool": transcripts.safe_text(tool, 80),
                                             "detail": detail}])[-MAX_TRIPS:]


# ---------------------------------------------------------------------------
# Loop wire
# ---------------------------------------------------------------------------

def fingerprint(tool, tool_input) -> str:
    """Tool name plus input. For shell tools the description label is left out and
    runs of spaces in the command count as one."""
    inp = tool_input
    if tool in SHELL_TOOLS and isinstance(inp, dict):
        inp = {k: (" ".join(v.split()) if k in ("command", "cmd") and isinstance(v, str) else v)
               for k, v in inp.items() if k != "description"}
    blob = json.dumps([tool, inp], sort_keys=True, ensure_ascii=False, default=str)
    return hashlib.blake2b(blob.encode("utf-8", "surrogatepass"), digest_size=8).hexdigest()


def _shell_text(tool, tool_input) -> str:
    if tool in SHELL_TOOLS and isinstance(tool_input, dict):
        cmd = tool_input.get("command", tool_input.get("cmd"))
        return cmd if isinstance(cmd, str) else ""
    return ""


def loop_count(agent, tool, tool_input) -> int:
    """How many times this exact call has come with nothing changed in between
    (this call included); 0 for calls that never count."""
    short = tool.rsplit("__", 1)[-1].lower()
    action = str(tool_input.get("action") or "").lower() if isinstance(tool_input, dict) else ""
    if (short in NEVER_LOOP or short.endswith("press_key") or action in NAV_ACTIONS
            or POLL_RE.search(_shell_text(tool, tool_input))):
        agent["counts"] = []
        return 0
    fp = fingerprint(tool, tool_input)
    counts = agent.get("counts") or []
    n = next((c[1] for c in counts if c[0] == fp), 0) + 1
    if tool in READ_ONLY_TOOLS:
        agent["counts"] = ([c for c in counts if c[0] != fp] + [[fp, n]])[-MAX_COUNTS:]
    else:  # anything else may have changed files or state: earlier repeats no longer count
        agent["counts"] = [[fp, n]]
    return n


def _agent(state, key) -> dict:
    agents = state["agents"]
    agent = agents.pop(key, None) or {}
    agents[key] = agent          # most recent last
    while len(agents) > MAX_AGENTS:
        agents.pop(next(iter(agents)))
    return agent


def agent_key(hook, harness) -> str:
    if isinstance(hook.get("agent_id"), str) and hook["agent_id"]:
        return hook["agent_id"]
    if harness == "codex" and isinstance(hook.get("transcript_path"), str):
        return "path:" + hook["transcript_path"]
    return "main"


# ---------------------------------------------------------------------------
# Transcripts: the main file plus subagent files, each read from its own offset
# ---------------------------------------------------------------------------

def _scandir(folder) -> list:
    try:
        return list(os.scandir(folder))
    except OSError:
        return []


def _track(files, path, key) -> None:
    if path not in files:
        files[path] = {"offset": 0, "size": 0, "key": key}


def _track_files(state, hook, harness) -> dict:
    """Add this session's transcript files to state["files"]: path -> offset, size, key.
    Keys match agent_key(): Claude Code 'main' and the subagent id; Codex 'path:<file>'."""
    files = state.setdefault("files", {})
    main = hook.get("transcript_path")
    if not isinstance(main, str) or not main:
        return files
    if harness == "claude-code":
        if os.path.basename(main).startswith("agent-"):  # given a subagent's own file: use its session's
            folder = os.path.dirname(main)
            for _ in range(3):
                if os.path.basename(folder) == "subagents":
                    main = os.path.dirname(folder) + ".jsonl"
                    break
                folder = os.path.dirname(folder)
        _track(files, main, "main")
        # Subagents: <session>/subagents/agent-<id>.jsonl, workflow agents one level deeper.
        sub = os.path.join(main[:-len(".jsonl")] if main.endswith(".jsonl") else main, "subagents")
        folders = [sub] + [e.path for e in _scandir(os.path.join(sub, "workflows")) if e.is_dir()]
        for folder in folders:
            for e in _scandir(folder):
                if e.name.startswith("agent-") and e.name.endswith(".jsonl"):
                    _track(files, e.path, e.name[len("agent-"):-len(".jsonl")])
    else:
        _track(files, main, "path:" + main)
        _track_codex_children(state, files, main, hook.get("session_id"))
    return files


def _codex_meta(path) -> dict:
    """The session_meta payload on the first line of a Codex rollout, or {}."""
    try:
        with open(path, "rb") as fh:
            rec = json.loads(fh.readline())
    except (OSError, ValueError):
        return {}
    return rec.get("payload") or {} if isinstance(rec, dict) and rec.get("type") == "session_meta" else {}


def _epoch(iso) -> float:
    try:
        return datetime.datetime.fromisoformat(str(iso).replace("Z", "+00:00")).timestamp()
    except ValueError:
        return 0.0


def _track_codex_children(state, files, main, session_id) -> None:
    """Codex writes each subagent to its own rollout, whose session_meta carries the
    root session id. Look in the main rollout's day folder and in today's and
    yesterday's, checking each rollout written since the session started once."""
    if "codex_started" not in state:
        state["codex_started"] = _epoch(_codex_meta(main).get("timestamp")) or state.get("created_ts", 0)
    since = state["codex_started"] - 60
    sessions = os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(main))))
    folders = {os.path.dirname(main)}
    now = time.time()
    for moment in (now, now - 86400):
        for stamp in (time.gmtime(moment), time.localtime(moment)):
            folders.add(os.path.join(sessions, *time.strftime("%Y %m %d", stamp).split()))
    checked = state.setdefault("codex_checked", [])
    seen = set(checked)
    for folder in sorted(folders):
        for e in _scandir(folder):
            if (not e.name.startswith("rollout-") or not e.name.endswith(".jsonl") or e.path in files
                    or e.name in seen):
                continue
            try:
                if e.stat().st_mtime < since:
                    continue
            except OSError:
                continue
            meta = _codex_meta(e.path)
            if not meta.get("id"):  # still being written: look again at the next call
                continue
            seen.add(e.name)
            checked.append(e.name)
            if meta.get("session_id") == session_id and meta.get("id") != session_id:
                _track(files, e.path, "path:" + e.path)
    del checked[:-2000]


_TURN_RE = re.compile(rb'"type"\s*:\s*"turn_context"')


def _codex_model_in(path, start, end) -> str:
    """The model named by the last Codex turn_context line between two offsets, or ''.
    Each read starts a fresh reader, which knows the model only from turn_context
    lines inside the new bytes, so the guard carries it from read to read."""
    if end <= start:
        return ""
    try:
        with open(path, "rb") as fh:
            fh.seek(start)
            data = fh.read(end - start)
    except OSError:
        return ""
    model = ""
    if _TURN_RE.search(data):
        for line in data.split(b"\n"):
            if _TURN_RE.search(line):
                try:
                    rec = json.loads(line)
                except ValueError:
                    continue
                payload = rec.get("payload") if isinstance(rec, dict) else None
                if isinstance(payload, dict) and isinstance(payload.get("model"), str) and payload["model"]:
                    model = payload["model"]
    return model


def estimate_usd(usage, model, harness) -> float:
    """The cost of usage from a model missing from the price table: the most it would
    cost on any known model of the same family (Codex models count as GPT), or on any
    known model when the family is unknown. Too high rather than $0, so a cap still holds."""
    name = (model or "").lower()
    family = next((f for f in FAMILIES if f in name), "gpt" if harness == "codex" else "")
    rows = [k for k in pricing.PRICES if family and k.startswith(family)] or list(pricing.PRICES)
    return max(pricing.cost_usd(usage, k) or 0.0 for k in rows)


def _add_cost(state, usage, model, harness) -> None:
    cost = pricing.cost_usd(usage, model)
    if cost is None:
        cost = estimate_usd(usage, model, harness)
        state["estimated_usd"] += cost
        state["estimated_tokens"] += usage.total_input() + usage.output
        name = transcripts.safe_text(model or "(no model id)", 60)
        if name not in state["estimated_models"]:
            state["estimated_models"] = (state["estimated_models"] + [name])[-10:]
    state["cost_usd"] += cost


def _digest(text) -> bytes:
    return hashlib.blake2b(text.encode("utf-8", "surrogatepass"), digest_size=8).digest()


def _shared_ids(folder) -> dict:
    """Responses any session has counted: id hash -> the hash of the session that counted it."""
    try:
        with open(os.path.join(folder, SHARED_IDS), "rb") as fh:
            data = fh.read()
    except OSError:
        return {}
    if len(data) % 16:  # a cut-off write: start the list over rather than misread it
        return {}
    return {data[i:i + 8]: data[i + 8:i + 16] for i in range(0, len(data), 16)}


def _share_ids(folder, session, hashes) -> None:
    """Add this session's newly counted responses to the shared list, under its own lock."""
    lock = _acquire(os.path.join(folder, SHARED_IDS + ".lock"))
    if lock is None:
        return
    try:
        path = os.path.join(folder, SHARED_IDS)
        size = os.path.getsize(path) if os.path.exists(path) else 0
        with open(path, "r+b" if size else "wb") as fh:
            if size % 16:
                fh.truncate(0)
            fh.seek(0, os.SEEK_END)
            fh.write(b"".join(h + session for h in hashes))
        if os.path.getsize(path) > 2 * MAX_SHARED_IDS * 16:
            with open(path, "rb") as fh:
                fh.seek(-MAX_SHARED_IDS * 16, os.SEEK_END)
                keep = fh.read()
            with open(path + ".tmp", "wb") as fh:
                fh.write(keep)
            os.replace(path + ".tmp", path)
    finally:
        _release(lock)


def _read_transcripts(state, hook, harness, folder) -> None:
    """Read what each transcript gained since the last call and fold it into the state.
    Each response counts once: a file read again from the start, or a resumed session
    whose file begins with a copy of an earlier session's records, adds nothing twice."""
    files = _track_files(state, hook, harness)
    counted = state.get("counted") or ""
    seen = {counted[i:i + 16] for i in range(0, len(counted), 16)}
    me, shared, fresh = _digest(state["session_id"]), None, []
    streaks = state.setdefault("streaks", {})
    denied = set(state["denied_ids"])
    no_prompt_id = not (hook.get("prompt_id") or hook.get("turn_id"))
    hook_model = hook.get("model") if isinstance(hook.get("model"), str) else ""
    for path, info in list(files.items()):
        try:
            size = os.path.getsize(path)
        except OSError:
            continue
        if size == info["size"]:
            continue
        if size < info["offset"]:  # the file was replaced: read it again from the start
            info["offset"] = 0
        start = info["offset"]
        try:
            events, offset = transcripts.read_new_events(harness, path, start)
        except Exception as exc:  # one unreadable file must not stop the others
            _log_error(exc)
            continue
        info["offset"], info["size"] = offset, size
        key = info["key"]
        known_model = info.get("model") or hook_model
        for ev in events:
            if ev.kind == "assistant" and ev.usage is not None:  # one event per API response
                if ev.id:
                    h = _digest("%s:%s" % (ev.kind, ev.id))
                    if h.hex() in seen:
                        continue
                    if start == 0:  # copies sit at the start of a file
                        shared = _shared_ids(folder) if shared is None else shared
                        if shared.get(h, me) != me:
                            continue
                    seen.add(h.hex())
                    fresh.append(h)
                _add_cost(state, ev.usage, ev.model or known_model, harness)
            elif ev.kind == "tool" and ev.tool is not None:
                _count_result(streaks, key, ev.tool, denied)
            elif ev.kind == "interrupt":
                streaks[key] = 0
            elif ev.kind == "user" and not ev.injected:  # the user typed: they are in the loop
                streaks[key] = 0
                if no_prompt_id and key in state["agents"]:
                    state["agents"][key]["counts"] = []
        if harness == "codex":
            info["model"] = _codex_model_in(path, start, offset) or info.get("model", "")
    if fresh:
        state["counted"] = (counted + "".join(h.hex() for h in fresh))[-MAX_SESSION_IDS * 16:]
        try:
            _share_ids(folder, me, fresh)
        except OSError as exc:
            _log_error(exc)


def _count_result(streaks, key, call, denied) -> None:
    if not call.has_result or call.id in denied:
        return
    if call.denied == "user-rejected" or call.interrupted:
        streaks[key] = 0
    elif call.denied:  # a rule or another hook said no: not a failure of the call itself
        return
    elif call.is_error:
        streaks[key] = streaks.get(key, 0) + 1
    else:
        streaks[key] = 0


# ---------------------------------------------------------------------------
# Decisions
# ---------------------------------------------------------------------------

def _decision(harness, kind, reason, user_message="") -> dict:
    out = {"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": kind,
                                  "permissionDecisionReason": reason}}
    if harness == "claude-code" and user_message:
        out["systemMessage"] = user_message
    return out


def _ordinal(n) -> str:
    return "%d%s" % (n, "th" if 10 <= n % 100 <= 20 else {1: "st", 2: "nd", 3: "rd"}.get(n % 10, "th"))


def run_hook(hook, harness=None, env=None):
    """Decide one PreToolUse call. Returns the JSON object to print, or None to
    let the normal permission flow run."""
    env = os.environ if env is None else env
    if not isinstance(hook, dict) or hook.get("hook_event_name") != "PreToolUse":
        return None
    if "cursor_version" in hook:  # Cursor runs Claude Code hooks too; its transcripts lack usage and results
        return {}                 # an empty answer could read as invalid JSON there, which blocks
    if str(env.get("RUNAWAY_GUARD_OFF", "")).strip().lower() in ("1", "true", "yes", "on"):
        return None
    session_id = hook.get("session_id")
    if not isinstance(session_id, str) or not session_id:
        return None
    harness = detect_harness(hook, harness)
    limits, sources = load_limits(env, env.get("CLAUDE_PROJECT_DIR") or hook.get("cwd") or "")
    folder = state_dir(env)
    os.makedirs(folder, mode=0o700, exist_ok=True)
    stem = session_stem(session_id)
    lock = _acquire(os.path.join(folder, stem + ".lock"))
    if lock is None:  # another call of this session holds the state too long: allow
        return None
    try:
        path = os.path.join(folder, stem + ".json")
        first_check = not os.path.exists(path)
        state = load_state(path)
        if state is None:  # a new session (or an unreadable state file): start fresh
            state = new_state(session_id, harness)
            prune(folder, stem)
        state["limits"] = limits
        out = _decide(state, hook, harness, limits, sources["spend_cap_usd"], env, first_check)
        state["updated"] = _now_iso()
        save_state(path, state)
        return out
    finally:
        _release(lock)


def _deny(state, hook, harness, reason, user_message):
    call_id = hook.get("tool_use_id")
    if isinstance(call_id, str) and call_id:  # its error result is the guard's doing, not a failure
        state["denied_ids"] = (state["denied_ids"] + [call_id])[-200:]
    return _decision(harness, "deny", reason, user_message)


def spent(state) -> float:
    """Dollars counted since the session started or was last reset."""
    return max(0.0, state["cost_usd"] - state["baseline_usd"])


def _spend_help(state, env, source) -> str:
    """How to go on after a spend stop, for wherever the cap in force comes from."""
    status = os.path.join(os.path.dirname(os.path.abspath(__file__)), "status.py")
    kind, where = source
    if kind == "environment":
        change = "raise or unset %s where the agent runs" % where
    elif kind == "project":
        change = "raise or remove spend_cap_usd in %s" % transcripts.safe_text(where, 300)
    elif kind == "user":
        change = "raise spend_cap_usd in %s" % transcripts.safe_text(where, 300)
    else:
        change = "set a higher spend_cap_usd in %s" % transcripts.safe_text(
            os.path.join(state_dir(env), CONFIG_NAME), 300)
    return ("%s, or start the count over by running this in a terminal: python3 %s --session %s --reset" % (
        change, transcripts.safe_text(shlex.quote(status), 300),
        transcripts.safe_text(shlex.quote(state["session_id"]), 140)))


def _decide(state, hook, harness, limits, cap_source, env, first_check=False):
    tool = hook.get("tool_name") if isinstance(hook.get("tool_name"), str) else ""
    name = transcripts.safe_text(tool, 80)
    key = agent_key(hook, harness)
    agent = _agent(state, key)
    _read_transcripts(state, hook, harness, state_dir(env))
    prompt = hook.get("prompt_id") or hook.get("turn_id") or ""
    if prompt and agent.get("prompt") != prompt:  # a new user prompt: earlier repeats no longer count
        agent["prompt"], agent["counts"] = prompt, []
    n = loop_count(agent, tool, hook.get("tool_input"))
    cap, used = limits["spend_cap_usd"], spent(state)
    notes = []
    if first_check and cap and used >= cap:  # spent before the guard first saw it: count from here
        state["baseline_usd"] = state["cost_usd"]
        _record(state, "baseline", tool, "$%.2f spent before the first check" % used)
        notes.append("Runaway guard started counting this session at $%.2f, which is already over its $%.2f "
                     "cap, so it counts from $0.00 from here." % (used, cap))
        used = 0.0
    if cap and used >= cap and tool not in STOP_TOOLS:
        first = not state["trips"] or state["trips"][-1]["wire"] != "spend"   # list one entry per stop
        _record(state, "spend", tool, "$%.2f of the $%.2f cap" % (used, cap), log=first)
        help_text = _spend_help(state, env, cap_source)
        estimated = _estimate_note(state)
        out = _deny(
            state, hook, harness,
            "Runaway guard: this session has spent about $%.2f, over its $%.2f cap, so tool calls are "
            "blocked.%s Stop and tell the user. To go on, the user can %s" % (used, cap, estimated, help_text),
            "Runaway guard: spend cap reached ($%.2f of $%.2f); tool calls are blocked.%s To go on, %s"
            % (used, cap, estimated, help_text))
        if harness == "claude-code":  # end the turn, so each block does not start another response
            out["continue"], out["stopReason"] = False, out["systemMessage"]
        return out
    if limits["loop_repeats"] and n >= limits["loop_repeats"]:
        _record(state, "loop", tool, "%s identical call" % _ordinal(n))
        return _deny(
            state, hook, harness,
            "Runaway guard blocked this %s call: it is the %s identical call with nothing changed in "
            "between, so it would most likely give the same result. Change the approach, or stop and ask "
            "the user." % (name, _ordinal(n)),
            "Runaway guard blocked a repeated %s call (the same call %d times with nothing changed in "
            "between)." % (name, n))
    streak = state["streaks"].get(key, 0)
    if limits["failure_streak"] and streak >= limits["failure_streak"]:
        state["streaks"][key] = 0  # ask once; the count starts over
        _record(state, "failures", tool, "%d failed calls in a row" % streak)
        if harness == "claude-code":
            return _decision(
                harness, "ask",
                "Runaway guard: the last %d tool calls failed in a row. Check that the agent is on track "
                "before you allow this %s call." % (streak, name))
        return _deny(  # Codex hooks cannot ask, so the agent is told to ask
            state, hook, harness,
            "Runaway guard: the last %d tool calls failed in a row. Stop and ask the user how to go on "
            "before you try again." % streak, "")
    if cap and limits["warn_at"] * cap <= used < cap and state.get("warned_cap") != cap:
        state["warned_cap"] = cap  # warn once per cap
        _record(state, "warning", tool, "$%.2f of the $%.2f cap" % (used, cap))
        notes.append("Runaway guard: this session has spent about $%.2f of its $%.2f cap (%d%%). At the cap, "
                     "tool calls are blocked." % (used, cap, 100 * used / cap))
    if state["estimated_models"] and not state["estimate_noted"]:
        state["estimate_noted"] = True  # name the first such model once per session
        notes.append("Runaway guard: no price is known for %s, so its tokens are priced like the most "
                     "expensive model of the same family. Update the skill to get its list price."
                     % state["estimated_models"][0])
    if notes and harness == "claude-code":  # Codex hooks have no way to show a message
        return {"systemMessage": " ".join(notes)}
    return None


def _estimate_note(state) -> str:
    if not state["estimated_usd"]:
        return ""
    return (" About $%.2f of it is an estimate for %s, which has no known price." % (
        state["estimated_usd"], ", ".join(state["estimated_models"])))


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def _log_error(exc) -> None:
    """One line per internal error in <state folder>/errors.log; never raises."""
    try:
        import traceback
        folder = state_dir()
        os.makedirs(folder, mode=0o700, exist_ok=True)
        path = os.path.join(folder, "errors.log")
        if os.path.exists(path) and os.path.getsize(path) > 256 * 1024:
            open(path, "w").close()
        frames = traceback.extract_tb(exc.__traceback__) if exc.__traceback__ else []
        where = "%s:%d" % (os.path.basename(frames[-1].filename), frames[-1].lineno) if frames else "?"
        with open(path, "a", encoding="utf-8") as fh:
            fh.write("%s %s %s: %s\n" % (_now_iso(), where, type(exc).__name__,
                                         transcripts.safe_text(str(exc), 200)))
    except Exception:
        pass


def _parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description="Runaway guard hook: reads one PreToolUse hook JSON object on stdin and prints a "
                    "decision as JSON. Always exits 0, because exit code 2 would block the tool call. "
                    "install.py writes the settings entry that runs it.")
    p.add_argument("--harness", choices=("claude-code", "codex"),
                   help="which harness calls the hook (default: read from the hook input)")
    return p


def main(argv=None) -> int:
    try:
        # Extra words are ignored: run without a shell, the installed "|| true" arrives as two.
        args, _extra = _parser().parse_known_args(argv)
    except SystemExit as exc:
        if exc.code in (0, None):
            raise                     # --help
        return 0                      # a bad flag in the settings entry must not block tool calls
    try:
        raw = sys.stdin.read()
        hook = json.loads(raw) if raw.strip() else None
        out = run_hook(hook, args.harness)
        if out is not None:
            sys.stdout.write(json.dumps(out) + "\n")
            sys.stdout.flush()
    except Exception as exc:  # a guard bug never blocks work
        _log_error(exc)
    return 0


if __name__ == "__main__":
    sys.exit(main())
