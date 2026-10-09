#!/usr/bin/env python3
"""claim-check Stop hook: when the agent tries to finish, block once if the
last test run failed or code changed after the last passing test run, and
that run or change came after the user's last prompt.

Claude Code and Codex call it with the hook JSON on stdin (session_id,
transcript_path, stop_hook_active, ...). It prints one JSON object:
{"decision": "block", "reason": "..."} to send the agent back to run the
tests, or {} to let it finish. It blocks once per reply (stop_hook_active),
does nothing in a session that ran no tests, leaves out subagents still
working and runs in other repositories, and allows the stop on any error,
so a bug here never traps a session.

Read-only. Python 3.9+, standard library only. Install with install.py.
"""

from __future__ import annotations

import argparse
import json
import os
import sys

sys.dont_write_bytecode = True  # leave no __pycache__ in the skill folder
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import evidence as E  # noqa: E402
import transcripts as T  # noqa: E402

AGAIN = "Run the tests again before finishing; if they fail, say so plainly."


def decide(payload, harness="claude-code"):
    """The reason to block this stop, or None to let the agent finish."""
    if not isinstance(payload, dict) or payload.get("stop_hook_active"):
        return None
    path = payload.get("transcript_path")
    if not isinstance(path, str) or not os.path.isfile(path):
        return None
    session = T.load_session(harness, path)
    subagents = [T.load_session(harness, p) for p in E.subagent_files(path)] if harness == "claude-code" else []
    state = E.last_test_run(session, subagents)
    run = state["run"]
    if run is None or run["result"] == "unknown" or not state["current"]:
        return None  # no test run, an unreadable result, or nothing new since the user's last prompt
    if state["activity"] and not (run["result"] == "fail" and not state["runner"]):
        return None  # a later command may have run the tests in a way this hook cannot read
    if run["result"] == "fail":
        detail = " (%s)" % T.safe_text(run["detail"], 80) if run["detail"] else ""
        return "claim-check: the last test run failed%s. %s" % (detail, AGAIN)
    if state["changed"]:
        shown = T.safe_text(state["changed"][0], 120)
        more = " and %d more" % (len(state["changed"]) - 1) if len(state["changed"]) > 1 else ""
        return "claim-check: files changed after the last passing test run (%s%s). %s" % (shown, more, AGAIN)
    return None


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(
        prog="stop_hook.py",
        description="Stop hook for Claude Code and Codex: blocks the agent from finishing once when the last "
                    "test run failed or code changed after the last passing run. Reads the hook JSON on stdin.")
    ap.add_argument("--harness", default="claude-code", choices=("claude-code", "codex"),
                    help="which harness calls the hook; default claude-code")
    args = ap.parse_args(argv)
    reason = None
    try:
        payload = json.loads(sys.stdin.read(1 << 20) or "null")
        reason = decide(payload, args.harness)
    except Exception:  # a guard bug must never trap a session: allow the stop
        reason = None
    print(json.dumps({"decision": "block", "reason": reason} if reason else {}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
