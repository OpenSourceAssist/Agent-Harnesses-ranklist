"""Run a hook command with test JSON on stdin and read its decision.

The only commands guardrail-tester ever runs are the user's own hook commands.
Each gets one JSON object on stdin that describes a tool call; the tool call
itself (for example a dangerous shell command in the battery) is only text
inside that JSON and is never run. Every run has a timeout, and the whole
process group is killed when it expires.

Decisions follow each harness's documented hook contract (fetched 2026-09-28):
  Claude Code  https://code.claude.com/docs/en/hooks
  Codex        https://learn.chatgpt.com/docs/hooks.md
  Gemini CLI   https://github.com/google-gemini/gemini-cli/blob/main/docs/hooks/reference.md
  Cursor       https://cursor.com/docs/hooks.md

Run with --help to print this text. Python 3.9+, standard library only.
"""

from __future__ import annotations

import datetime
import json
import os
import signal
import subprocess
import sys
import time
from dataclasses import dataclass
from typing import Optional

_RANK = {"deny": 4, "defer": 3, "ask": 2, "allow": 1}


@dataclass
class HookRun:
    exit_code: Optional[int] = None
    stdout: str = ""
    stderr: str = ""
    timed_out: bool = False
    duration: float = 0.0
    error: str = ""                  # why the command could not start


@dataclass
class Outcome:
    decision: Optional[str] = None   # allow | deny | ask | None
    reason: str = ""
    problem: str = ""                # "" or a short code: exit-1, error, timeout, not-started, bad-json, ...


def _substitute(text, env):
    for key in ("CLAUDE_PROJECT_DIR", "CLAUDE_PLUGIN_ROOT", "CLAUDE_PLUGIN_DATA"):
        if key in env:
            text = text.replace("${%s}" % key, env[key])
    return text


def run_hook(command, args=None, stdin_obj=None, cwd=None, env_extra=None, timeout=10.0, shell=None):
    """Run one hook command. Shell form (no args) goes through sh -c, as the
    Claude Code docs describe for macOS and Linux, or bash -c when the handler
    sets "shell": "bash"; exec form runs `command` with `args`, no shell."""
    env = dict(os.environ)
    env.update({k: str(v) for k, v in (env_extra or {}).items()})
    if args is not None:
        argv = [_substitute(command, env)] + [_substitute(str(a), env) for a in args]
    else:
        interpreter = "/bin/bash" if shell == "bash" and os.path.exists("/bin/bash") else "/bin/sh"
        argv = [interpreter, "-c", command]
    data = json.dumps(stdin_obj if stdin_obj is not None else {}).encode("utf-8")
    run = HookRun()
    start = time.monotonic()
    try:
        proc = subprocess.Popen(argv, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                cwd=cwd or None, env=env, start_new_session=True)
    except OSError as exc:
        run.error = type(exc).__name__
        run.duration = time.monotonic() - start
        return run
    try:
        out, err = proc.communicate(data, timeout=timeout)
    except subprocess.TimeoutExpired:
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except OSError:
            proc.kill()
        out, err = proc.communicate()
        run.timed_out = True
    run.duration = time.monotonic() - start
    run.exit_code = None if run.timed_out else proc.returncode
    run.stdout = out.decode("utf-8", "replace")
    run.stderr = err.decode("utf-8", "replace")
    return run


def _json_object(stdout):
    """(object, problem): a JSON object when stdout starts with { and ends with }."""
    text = stdout.strip()
    if not (text.startswith("{") and text.endswith("}")):
        return None, ""
    try:
        value = json.loads(text)
    except ValueError:
        return None, "bad-json"
    return (value, "") if isinstance(value, dict) else (None, "bad-json")


def _first_line(text):
    for line in text.splitlines():
        if line.strip():
            return line.strip()
    return ""


def _not_started(run):
    return bool(run.error) or run.exit_code in (126, 127)


def claude_outcome(run) -> Outcome:
    """Claude Code PreToolUse: exit 2 blocks; JSON decides on any other exit
    code; exit 1 and other codes without JSON are non-blocking errors; a
    timed-out hook decides nothing."""
    if run.timed_out:
        return Outcome(problem="timeout")
    if _not_started(run):
        return Outcome(problem="not-started")
    obj, problem = _json_object(run.stdout)
    decision, reason = None, ""
    if obj is not None:
        spec = obj.get("hookSpecificOutput")
        if obj.get("continue") is False:
            decision, reason = "deny", str(obj.get("stopReason") or "the hook stopped the session")
        elif isinstance(spec, dict):
            if spec.get("hookEventName") != "PreToolUse":
                problem = "schema"
            else:
                value = spec.get("permissionDecision")
                reason = str(spec.get("permissionDecisionReason") or "")
                if value in ("allow", "deny", "ask"):
                    decision = value
                elif value == "defer":
                    problem = "defer"
                elif value is not None:
                    problem = "schema"
        elif obj.get("decision") in ("approve", "block"):
            decision = "allow" if obj["decision"] == "approve" else "deny"
            reason = str(obj.get("reason") or "")
    if run.exit_code == 2:
        return Outcome("deny", reason or _first_line(run.stderr) or "the hook exited 2", "")
    if decision is not None:
        return Outcome(decision, reason, "")
    if problem:
        return Outcome(problem=problem)
    if run.exit_code == 0:
        return Outcome()
    return Outcome(problem="exit-1" if run.exit_code == 1 else "error")


def codex_outcome(run) -> Outcome:
    """Codex PreToolUse: exit 2 or a JSON deny (or legacy block) blocks; ask,
    continue, stopReason, and suppressOutput mark the hook failed and the call
    proceeds; plain text stdout is ignored."""
    if run.timed_out:
        return Outcome(problem="timeout")
    if _not_started(run):
        return Outcome(problem="not-started")
    if run.exit_code == 2:
        return Outcome("deny", _first_line(run.stderr) or "the hook exited 2")
    obj, problem = _json_object(run.stdout)
    if obj is not None:
        spec = obj.get("hookSpecificOutput") if isinstance(obj.get("hookSpecificOutput"), dict) else {}
        if spec.get("permissionDecision") == "deny" or obj.get("decision") == "block":
            return Outcome("deny", str(spec.get("permissionDecisionReason") or obj.get("reason") or ""))
        if spec.get("permissionDecision") == "ask" or obj.get("decision") == "approve" or any(
                k in obj for k in ("continue", "stopReason", "suppressOutput")):
            return Outcome(problem="unsupported")
    if problem:
        return Outcome(problem=problem)
    return Outcome() if run.exit_code == 0 else Outcome(problem="error")


def gemini_outcome(run) -> Outcome:
    """Gemini CLI BeforeTool: exit 2 blocks with stderr as the reason; on exit 0
    the JSON decision deny (alias block) blocks and continue false stops the
    agent loop; other exit codes are warnings and the call proceeds."""
    if run.timed_out:
        return Outcome(problem="timeout")
    if _not_started(run):
        return Outcome(problem="not-started")
    if run.exit_code == 2:
        return Outcome("deny", _first_line(run.stderr) or "the hook exited 2")
    if run.exit_code != 0:
        return Outcome(problem="error")
    obj, problem = _json_object(run.stdout)
    if obj is not None:
        if obj.get("continue") is False:
            return Outcome("deny", str(obj.get("stopReason") or "the hook stopped the agent"))
        if obj.get("decision") in ("deny", "block"):
            return Outcome("deny", str(obj.get("reason") or ""))
    return Outcome(problem=problem)


def cursor_outcome(run, fail_closed=False, event="preToolUse") -> Outcome:
    """Cursor permission hooks: exit 2 blocks; on exit 0 invalid JSON blocks and
    permission deny blocks (ask is enforced for beforeShellExecution, not for
    preToolUse); crashes, timeouts, and other exit codes fail open unless
    failClosed is set. Claude Code's nested hookSpecificOutput is accepted."""
    failed = run.timed_out or _not_started(run) or run.exit_code not in (0, 2)
    if failed:
        problem = "timeout" if run.timed_out else "not-started" if _not_started(run) else "error"
        return Outcome("deny", "failClosed is set and the hook failed", problem) if fail_closed \
            else Outcome(problem=problem)
    if run.exit_code == 2:
        return Outcome("deny", _first_line(run.stderr) or "the hook exited 2")
    text = run.stdout.strip()
    if not text:
        return Outcome("deny", "failClosed is set and the hook gave no output", "no-output") if fail_closed \
            else Outcome(problem="no-output")
    try:
        obj = json.loads(text)
    except ValueError:
        return Outcome("deny", "invalid JSON from a permission hook blocks the action", "bad-json")
    if not isinstance(obj, dict):
        return Outcome("deny", "invalid JSON from a permission hook blocks the action", "bad-json")
    spec = obj.get("hookSpecificOutput") if isinstance(obj.get("hookSpecificOutput"), dict) else {}
    permission = obj.get("permission") or spec.get("permissionDecision")
    reason = str(obj.get("user_message") or spec.get("permissionDecisionReason") or "")
    if permission == "deny":
        return Outcome("deny", reason)
    if permission == "ask":
        return Outcome("ask", reason) if event != "preToolUse" else Outcome(problem="ask-ignored")
    if permission == "allow":
        return Outcome("allow", reason)
    return Outcome()


def combine(outcomes) -> Outcome:
    """Several hooks on one call: deny beats defer beats ask beats allow."""
    best = Outcome()
    for outcome in outcomes:
        if outcome.decision and _RANK.get(outcome.decision, 0) > _RANK.get(best.decision or "", 0):
            best = outcome
    return Outcome(best.decision, best.reason)


def claude_payload(tool, tool_input, cwd, mode, transcript, tool_use_id="toolu_guardrail_tester"):
    return {"session_id": "guardrail-tester", "transcript_path": transcript, "cwd": cwd,
            "permission_mode": mode, "hook_event_name": "PreToolUse", "tool_name": tool,
            "tool_input": tool_input, "tool_use_id": tool_use_id}


def codex_payload(tool, tool_input, cwd, tool_use_id="call_guardrail_tester"):
    return {"session_id": "guardrail-tester", "transcript_path": None, "cwd": cwd,
            "hook_event_name": "PreToolUse", "model": "", "turn_id": "guardrail-tester",
            "permission_mode": "default", "tool_name": tool, "tool_use_id": tool_use_id, "tool_input": tool_input}


def gemini_payload(tool, tool_input, cwd, transcript):
    return {"session_id": "guardrail-tester", "transcript_path": transcript, "cwd": cwd,
            "hook_event_name": "BeforeTool",
            "timestamp": datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "tool_name": tool, "tool_input": tool_input}


def cursor_payload(event, tool, tool_input, cwd):
    base = {"conversation_id": "guardrail-tester", "generation_id": "guardrail-tester", "hook_event_name": event,
            "workspace_roots": [cwd], "transcript_path": None, "model": "", "cursor_version": ""}
    if event == "beforeShellExecution":
        base.update({"command": tool_input.get("command", ""), "cwd": cwd, "sandbox": False})
    elif event == "beforeReadFile":
        base.update({"file_path": tool_input.get("file_path", ""), "content": "", "attachments": []})
    else:
        base.update({"tool_name": tool, "tool_input": tool_input, "tool_use_id": "guardrail-tester", "cwd": cwd})
    return base


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] in ("-h", "--help"):
        print(__doc__.strip())
        sys.exit(0)
    print("hook_runner is a helper module for test_guards.py; run it with --help for details.")
