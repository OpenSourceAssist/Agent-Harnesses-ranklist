"""Tests for check_real_data.py, run against a fake home folder in tmp_path."""

import json
import os
import subprocess
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import test_transcripts as F  # noqa: E402  (fixture builders)

SCRIPT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "check_real_data.py")
PROMPT = "please refactor the billing module"


def run(*args):
    env = dict(os.environ)
    for name in ("CLAUDE_CONFIG_DIR", "CODEX_HOME", "XDG_DATA_HOME", "OPENCODE_DB"):
        env.pop(name, None)
    r = subprocess.run([sys.executable, SCRIPT] + list(args), capture_output=True, text=True, env=env)
    return r.returncode, r.stdout, r.stderr


def fake_home(tmp_path):
    records = [
        F.cc_user(PROMPT, "2026-09-25T10:00:00.000Z"),
        F.cc_assistant("m1", F.tool_use("t1", "Bash", {"command": "pytest"}), "2026-09-25T10:00:01.000Z",
                       F.cc_usage(inp=1000, out=2000, read=1000000, write=100000, w1h=100000),
                       model="claude-opus-5-5"),
        F.cc_result("t1", "Exit code 1\nboom", "2026-09-25T10:00:02.000Z", is_error=True),
        F.cc_assistant("m2", F.tool_use("t2", "Edit", {"file_path": "/w/a.py"}), "2026-09-25T10:00:03.000Z",
                       F.cc_usage(inp=0, out=0), model="claude-opus-5-5"),
        F.cc_result("t2", "The user doesn't want to proceed", "2026-09-25T10:00:04.000Z", is_error=True,
                    toolDenialKind="user-rejected"),
        F.cc_user([F.text("[Request interrupted by user]")], "2026-09-25T10:00:05.000Z"),
        "{broken line",
        {"type": "cost-state", "totalCostUSD": 0.9, "modelUsage": {"claude-opus-5-5": {"costUSD": 0.9}}},
    ]
    F.set_age(F.cc_file(tmp_path, records), 2)
    # A forked session: a copy of the first response (same ids), then one new response.
    F.set_age(F.cc_file(tmp_path, [records[1], F.cc_assistant("m9", F.text("new"), "2026-09-25T11:00:00.000Z",
                                                              F.cc_usage(inp=500, out=0), model="claude-opus-5-5")],
                        sid="s-fork"), 1)
    F.write_jsonl(os.path.join(F.cc_project(tmp_path), F.SID, "subagents", "workflows", "wf_1", "agent-a1.jsonl"),
                  [F.cc_assistant("s1", F.text("step"), "2026-09-25T10:00:03.000Z", F.cc_usage(inp=1000, out=0),
                                  model="claude-opus-5-5", isSidechain=True)])
    F.cx_file(tmp_path, [F.cx_meta("2026-09-25T10:00:00.000Z"), F.cx_turn("2026-09-25T10:00:00.100Z", "gpt-6-sol"),
                         F.cx_count("2026-09-25T10:00:01.000Z", F.cx_tokens(2000, 1000, 100, 0),
                                    F.cx_tokens(2000, 1000, 100, 0))])
    return str(tmp_path)


def test_reports_aggregates_per_harness_as_json(tmp_path):
    code, out, _err = run("--home", fake_home(tmp_path), "--json")
    assert code == 0
    report = json.loads(out)
    cc, cx = report["harnesses"]["claude-code"], report["harnesses"]["codex"]
    assert (cc["sessions"], cc["main_sessions"], cc["subagent_sessions"]) == (3, 2, 1)
    assert cc["tool_calls"] == 2 and cc["tool_calls_by_kind"] == {"shell": 1, "edit": 1}
    assert (cc["tool_errors"], cc["denials_by_kind"], cc["interrupts"], cc["compactions"]) == (2, {"user-rejected": 1}, 1, 0)
    assert cc["tokens_by_model"]["claude-opus-5-5"]["cache_read"] == 1000000
    # (1,000 + 1,000 workflow agent + 500 fork) x 4 + 1,000,000 x 0.20 + 100,000 x 8 (1-hour) + 2,000 x 20, per million
    assert cc["cost_by_model_usd"] == {"claude-opus-5-5": 1.05}
    assert (cc["repeated_responses"], cc["repeated_tool_calls"]) == (1, 1)
    assert cc["warnings"] == 1 and cc["top_warnings"] == [["malformed JSON line", 1]]
    assert cc["lines"] == 11
    assert cc["cost_state_check"] == {"sessions": 1, "within_1_cent": 0, "recorded_usd": 0.9, "computed_usd": 1.048}
    assert cx["tokens_by_model"]["gpt-6-sol"]["input"] == 1000
    assert cx["cost_by_model_usd"] == {"gpt-6-sol": 0.0032}   # 1,000 x 2.00 + 1,000 x 0.20 + 100 x 10.00
    assert report["harnesses"].get("gemini-cli") is None


def test_text_report_holds_no_prompts_or_paths(tmp_path):
    home = fake_home(tmp_path)
    code, out, _err = run("--home", home)
    assert code == 0
    assert "claude-code" in out and "codex" in out
    assert PROMPT not in out and home not in out and "/w/a.py" not in out and "boom" not in out


def test_a_home_without_harnesses_reports_nothing_found(tmp_path):
    code, out, _err = run("--home", str(tmp_path), "--json")
    assert code == 0 and json.loads(out)["harnesses"] == {}
