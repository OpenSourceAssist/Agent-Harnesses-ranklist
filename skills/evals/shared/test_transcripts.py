"""Tests for transcripts.py. Every fixture is built in tmp_path from the record
shapes in the harness facts file (Q1); nothing here reads the real home folder."""

import itertools
import json
import os
import sys
import time

import pytest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import transcripts as T  # noqa: E402


@pytest.fixture(autouse=True)
def _no_real_config(monkeypatch):
    """Keep the caller's environment from pointing the reader at real data."""
    for name in ("CLAUDE_CONFIG_DIR", "CODEX_HOME", "XDG_DATA_HOME", "OPENCODE_DB"):
        monkeypatch.delenv(name, raising=False)


# ---------------------------------------------------------------------------
# Fixture helpers (test-only)
# ---------------------------------------------------------------------------

_ids = itertools.count(1)


def write_jsonl(path, records):
    os.makedirs(os.path.dirname(str(path)), exist_ok=True)
    with open(str(path), "w", encoding="utf-8") as fh:
        for rec in records:
            fh.write(rec if isinstance(rec, str) else json.dumps(rec))
            fh.write("\n")
    return str(path)


def set_age(path, days):
    t = time.time() - days * 86400
    os.utime(str(path), (t, t))


SID = "5f0c3a1e-8d7b-4c2a-9e61-0123456789ab"


def cc_env(ts, **kw):
    rec = {
        "parentUuid": None, "isSidechain": False, "userType": "external",
        "cwd": "/work/app", "sessionId": SID, "version": "2.1.284",
        "gitBranch": "main", "entrypoint": "cli", "slug": "calm-river",
        "uuid": "u-%d" % next(_ids), "timestamp": ts,
    }
    rec.update(kw)
    return rec


def cc_usage(inp=3, out=10, read=0, write=0, w1h=0, thinking=None):
    u = {
        "input_tokens": inp, "cache_creation_input_tokens": write,
        "cache_read_input_tokens": read,
        "cache_creation": {"ephemeral_5m_input_tokens": write - w1h,
                           "ephemeral_1h_input_tokens": w1h},
        "output_tokens": out, "service_tier": "standard", "speed": "standard",
        "inference_geo": "not_available",
        "server_tool_use": {"web_search_requests": 0, "web_fetch_requests": 0},
    }
    u["iterations"] = [dict((k, u[k]) for k in ("input_tokens", "output_tokens"))]
    if thinking is not None:
        u["output_tokens_details"] = {"thinking_tokens": thinking}
    return u


def cc_assistant(mid, block, ts, usage=None, model="claude-opus-5-5", **kw):
    msg = {"id": mid, "type": "message", "role": "assistant", "model": model,
           "content": [block], "stop_reason": None, "stop_sequence": None,
           "usage": usage if usage is not None else cc_usage()}
    kw.setdefault("requestId", "req_" + mid)
    return cc_env(ts, type="assistant", message=msg, **kw)


def text(t):
    return {"type": "text", "text": t}


def thinking(t="..."):
    return {"type": "thinking", "thinking": t, "signature": "sig"}


def tool_use(tid, name, inp):
    return {"type": "tool_use", "id": tid, "name": name, "input": inp,
            "caller": {"type": "direct"}}


def cc_user(content, ts, **kw):
    return cc_env(ts, type="user", message={"role": "user", "content": content}, **kw)


def cc_result(tid, content, ts, is_error=None, tool_use_result=None, **kw):
    block = {"tool_use_id": tid, "type": "tool_result", "content": content}
    if is_error is not None:
        block["is_error"] = is_error
    if tool_use_result is not None:
        kw["toolUseResult"] = tool_use_result
    return cc_user([block], ts, sourceToolAssistantUUID="u-x", **kw)


def bash_result(stdout="", interrupted=False, **extra):
    r = {"stdout": stdout, "stderr": "", "interrupted": interrupted,
         "isImage": False, "noOutputExpected": False}
    r.update(extra)
    return r


def cc_project(home, cwd="/work/app"):
    enc = "".join(c if c.isalnum() else "-" for c in cwd)
    return os.path.join(str(home), ".claude", "projects", enc)


def cc_file(home, records, sid=SID, cwd="/work/app"):
    return write_jsonl(os.path.join(cc_project(home, cwd), sid + ".jsonl"), records)


def kinds(session):
    return [e.kind for e in session.events]


# ---------------------------------------------------------------------------
# Usage
# ---------------------------------------------------------------------------

def test_usage_total_input_adds_uncached_cache_read_and_cache_write():
    u = T.Usage(input=10, cache_read=200, cache_write=30, cache_write_1h=5, output=7)
    assert u.total_input() == 240


def test_usage_add_sums_every_field():
    a = T.Usage(input=1, cache_read=2, cache_write=3, cache_write_1h=1, output=5, reasoning=2)
    b = T.Usage(input=10, cache_read=20, cache_write=30, cache_write_1h=10, output=50, reasoning=20)
    assert a + b == T.Usage(input=11, cache_read=22, cache_write=33, cache_write_1h=11,
                            output=55, reasoning=22)


# ---------------------------------------------------------------------------
# Redaction and safe text live in safe.py (tests: test_safe.py)
# ---------------------------------------------------------------------------

def test_transcripts_keeps_the_names_it_takes_from_safe():
    import safe
    assert (T.redact, T.safe_text, T.code) == (safe.redact, safe.safe_text, safe.code)


# ---------------------------------------------------------------------------
# Claude Code
# ---------------------------------------------------------------------------

def test_claude_split_records_give_one_usage_per_response(tmp_path):
    # One API response written as three records (one block each) that share
    # message.id; a tool result lands between them, as in real files.
    records = [
        cc_user("fix the bug", "2026-09-25T10:00:00.000Z", promptId="p1"),
        cc_assistant("msg_A", thinking(), "2026-09-25T10:00:01.000Z",
                     cc_usage(inp=3, out=5, read=1000, write=100, w1h=100)),
        cc_assistant("msg_A", tool_use("toolu_1", "Read", {"file_path": "/work/app/a.py"}),
                     "2026-09-25T10:00:02.000Z", cc_usage(inp=3, out=20, read=1000, write=100, w1h=100)),
        cc_result("toolu_1", "1\tprint('hi')", "2026-09-25T10:00:03.000Z",
                  tool_use_result={"type": "text", "file": {"filePath": "/work/app/a.py"}}),
        cc_assistant("msg_A", text("Found it."), "2026-09-25T10:00:04.000Z",
                     cc_usage(inp=3, out=42, read=1000, write=100, w1h=100, thinking=30)),
    ]
    s = T.load_session("claude-code", cc_file(tmp_path, records))
    assistant = [e for e in s.events if e.kind == "assistant"]
    assert len(assistant) == 1
    assert assistant[0].usage == T.Usage(input=3, cache_read=1000, cache_write=100,
                                         cache_write_1h=100, output=42, reasoning=30)
    assert assistant[0].text == "Found it."
    assert assistant[0].ts == "2026-09-25T10:00:01.000Z"
    assert s.usage_total().output == 42
    assert kinds(s) == ["user", "assistant", "tool"]


def test_claude_groups_by_request_id_when_message_id_is_missing(tmp_path):
    a1 = cc_assistant("x", text("a"), "2026-09-25T10:00:01.000Z", cc_usage(out=4), requestId="req_9")
    a2 = cc_assistant("x", text("b"), "2026-09-25T10:00:02.000Z", cc_usage(out=9), requestId="req_9")
    for a in (a1, a2):
        del a["message"]["id"]
    s = T.load_session("claude-code", cc_file(tmp_path, [a1, a2]))
    assert [e.usage.output for e in s.events if e.usage] == [9]


def test_claude_synthetic_records_are_left_out_of_usage_and_models(tmp_path):
    records = [
        cc_assistant("msg_1", text("real answer"), "2026-09-25T10:00:01.000Z",
                     cc_usage(inp=10, out=10), model="claude-sonnet-5"),
        cc_assistant("msg_2", text("API Error: 529 Overloaded"), "2026-09-25T10:00:02.000Z",
                     cc_usage(inp=0, out=0), model="<synthetic>", isApiErrorMessage=True),
        cc_assistant("msg_3", text("No response requested."), "2026-09-25T10:00:03.000Z",
                     cc_usage(inp=0, out=0), model="<synthetic>"),
    ]
    s = T.load_session("claude-code", cc_file(tmp_path, records))
    assert s.models == ["claude-sonnet-5"]
    assert list(s.usage_by_model()) == ["claude-sonnet-5"]
    assert kinds(s) == ["assistant", "api_error", "assistant"]
    assert s.events[1].text == "API Error: 529 Overloaded"
    assert s.events[2].usage is None and s.events[2].model == "<synthetic>"


def test_claude_links_results_to_calls_and_reads_exit_codes(tmp_path):
    long_out = "x" * (T.MAX_OUTPUT_CHARS + 500)
    records = [
        cc_assistant("m1", tool_use("t_fail", "Bash", {"command": "pytest -q"}), "2026-09-25T10:00:01.000Z"),
        cc_result("t_fail", "Exit code 2\nE   assert 1 == 2", "2026-09-25T10:00:02.000Z", is_error=True,
                  tool_use_result="Error: Exit code 2"),
        cc_assistant("m2", tool_use("t_ok", "Bash", {"command": "ls"}), "2026-09-25T10:00:03.000Z"),
        cc_result("t_ok", long_out, "2026-09-25T10:00:04.000Z", tool_use_result=bash_result(long_out)),
        cc_assistant("m3", tool_use("t_grep", "Bash", {"command": "grep -r nope ."}), "2026-09-25T10:00:05.000Z"),
        cc_result("t_grep", "No matches", "2026-09-25T10:00:06.000Z",
                  tool_use_result=bash_result("", returnCodeInterpretation="No matches found")),
        cc_assistant("m4", tool_use("t_bg", "Bash", {"command": "npm run dev", "run_in_background": True}),
                     "2026-09-25T10:00:07.000Z"),
        cc_result("t_bg", "Command running in background with ID: b1", "2026-09-25T10:00:08.000Z",
                  tool_use_result=bash_result("", backgroundTaskId="b1")),
        cc_assistant("m5", tool_use("t_pending", "Bash", {"command": "sleep 100"}), "2026-09-25T10:00:09.000Z"),
    ]
    s = T.load_session("claude-code", cc_file(tmp_path, records))
    calls = {c.id: c for c in s.tool_calls()}
    fail = calls["t_fail"]
    assert (fail.kind, fail.command, fail.is_error, fail.exit_code, fail.has_result) == \
        ("shell", "pytest -q", True, 2, True)
    assert fail.output.startswith("Exit code 2")
    ok = calls["t_ok"]
    assert (ok.is_error, ok.exit_code) == (False, 0)
    assert len(ok.output) == T.MAX_OUTPUT_CHARS and ok.output_chars == T.MAX_OUTPUT_CHARS + 500
    assert (calls["t_grep"].is_error, calls["t_grep"].exit_code) == (False, None)
    assert calls["t_bg"].exit_code is None
    assert (calls["t_pending"].has_result, calls["t_pending"].exit_code) == (False, None)
    assert calls["t_fail"].ts == "2026-09-25T10:00:01.000Z"


def test_claude_result_content_given_as_blocks_is_joined(tmp_path):
    records = [
        cc_assistant("m1", tool_use("t1", "mcp__docs__search", {"q": "hooks"}), "2026-09-25T10:00:01.000Z"),
        cc_result("t1", [{"type": "text", "text": "first"}, {"type": "image", "source": {}},
                         {"type": "text", "text": "second"}], "2026-09-25T10:00:02.000Z"),
    ]
    call = T.load_session("claude-code", cc_file(tmp_path, records)).tool_calls()[0]
    assert call.kind == "mcp"
    assert call.output == "first\nsecond"


@pytest.mark.parametrize("name,inp,kind,paths", [
    ("Bash", {"command": "make"}, "shell", []),
    ("Read", {"file_path": "/w/a.py"}, "read", ["/w/a.py"]),
    ("Edit", {"file_path": "/w/a.py", "old_string": "a", "new_string": "b"}, "edit", ["/w/a.py"]),
    ("MultiEdit", {"file_path": "/w/b.py", "edits": []}, "edit", ["/w/b.py"]),
    ("NotebookEdit", {"notebook_path": "/w/n.ipynb", "new_source": ""}, "edit", ["/w/n.ipynb"]),
    ("Write", {"file_path": "/w/c.py", "content": "x"}, "write", ["/w/c.py"]),
    ("Grep", {"pattern": "TODO", "path": "/w"}, "search", []),
    ("Glob", {"pattern": "**/*.py"}, "search", []),
    ("WebFetch", {"url": "https://example.com", "prompt": "p"}, "web", []),
    ("WebSearch", {"query": "q"}, "web", []),
    ("Agent", {"description": "d", "prompt": "p", "subagent_type": "Explore"}, "agent", []),
    ("Task", {"description": "d", "prompt": "p"}, "agent", []),
    ("mcp__plugin_x_srv__do", {}, "mcp", []),
    ("TodoWrite", {"todos": []}, "other", []),
])
def test_claude_tool_kinds_and_paths(tmp_path, name, inp, kind, paths):
    records = [cc_assistant("m1", tool_use("t1", name, inp), "2026-09-25T10:00:01.000Z")]
    call = T.load_session("claude-code", cc_file(tmp_path, records)).tool_calls()[0]
    assert (call.name, call.kind, call.paths, call.input) == (name, kind, paths, inp)


def test_claude_denial_kinds(tmp_path):
    rejected = "The user doesn't want to proceed with this tool use. The tool use was rejected."
    cases = [("t1", "user-rejected", rejected), ("t2", "permission-rule", rejected),
             ("t3", "automode-blocked", "Permission for this action was denied by auto mode."),
             ("t4", "automode-unavailable", "claude-opus-5-5 is temporarily unavailable"),
             ("t5", "some-future-kind", "Denied.")]
    records = []
    for i, (tid, kind, msg) in enumerate(cases):
        records.append(cc_assistant("m%d" % i, tool_use(tid, "Bash", {"command": "rm -rf build"}),
                                    "2026-09-25T10:00:0%d.000Z" % i))
        records.append(cc_result(tid, msg, "2026-09-25T10:00:0%d.500Z" % i, is_error=True,
                                 toolDenialKind=kind))
    # A PreToolUse hook that blocks the call leaves an attachment naming the call.
    records.append(cc_assistant("m9", tool_use("t9", "Bash", {"command": "git push -f"}),
                                "2026-09-25T10:00:09.000Z"))
    records.append(cc_env("2026-09-25T10:00:09.100Z", type="attachment", attachment={
        "type": "hook_blocking_error", "hookName": "PreToolUse:Bash", "toolUseID": "t9",
        "hookEvent": "PreToolUse", "blockingError": "blocked by policy"}))
    records.append(cc_result("t9", "PreToolUse:Bash hook error: blocked by policy",
                             "2026-09-25T10:00:09.200Z", is_error=True))
    # A Stop hook block is not a tool denial.
    records.append(cc_env("2026-09-25T10:00:10.000Z", type="attachment", attachment={
        "type": "hook_blocking_error", "hookName": "Stop", "toolUseID": "",
        "hookEvent": "Stop", "blockingError": "keep going"}))
    s = T.load_session("claude-code", cc_file(tmp_path, records))
    denied = {c.id: c.denied for c in s.tool_calls()}
    assert denied == {"t1": "user-rejected", "t2": "permission-rule", "t3": "auto-reviewer",
                      "t4": "other", "t5": "other", "t9": "hook"}
    assert all(c.is_error for c in s.tool_calls())


def test_claude_interrupts(tmp_path):
    records = [
        cc_user("run the suite", "2026-09-25T10:00:00.000Z"),
        cc_assistant("m1", tool_use("t1", "Bash", {"command": "pytest"}), "2026-09-25T10:00:01.000Z"),
        cc_result("t1", "Interrupted", "2026-09-25T10:00:05.000Z", is_error=True,
                  tool_use_result=bash_result("", interrupted=True)),
        cc_user([text("[Request interrupted by user for tool use]")], "2026-09-25T10:00:05.100Z"),
        cc_assistant("m2", text("Working..."), "2026-09-25T10:00:06.000Z"),
        cc_user([text("[Request interrupted by user]")], "2026-09-25T10:00:07.000Z"),
    ]
    s = T.load_session("claude-code", cc_file(tmp_path, records))
    assert kinds(s) == ["user", "assistant", "tool", "interrupt", "assistant", "interrupt"]
    assert s.tool_calls()[0].interrupted is True
    assert s.tool_calls()[0].exit_code is None
    assert [e.text for e in s.user_prompts()] == ["run the suite"]


def test_claude_compaction_boundary_carries_the_summary(tmp_path):
    records = [
        cc_user("long task", "2026-09-25T10:00:00.000Z"),
        cc_env("2026-09-25T11:00:00.000Z", type="system", subtype="compact_boundary",
               content="Conversation compacted", level="info", logicalParentUuid="u-1",
               compactMetadata={"trigger": "auto", "preTokens": 180000, "postTokens": 9000,
                                "durationMs": 1200}),
        cc_user("This session is being continued. Summary: fixed the parser.",
                "2026-09-25T11:00:01.000Z", isCompactSummary=True, isVisibleInTranscriptOnly=True),
        cc_user("thanks, now add tests", "2026-09-25T11:05:00.000Z"),
    ]
    s = T.load_session("claude-code", cc_file(tmp_path, records))
    assert kinds(s) == ["user", "compaction", "user"]
    assert s.events[1].text.endswith("fixed the parser.")
    assert s.events[1].ts == "2026-09-25T11:00:00.000Z"


def test_claude_api_error_records(tmp_path):
    records = [cc_env("2026-09-25T10:00:00.000Z", type="system", subtype="api_error", level="error",
                      error={"status": 529, "message": "Overloaded", "requestId": "r1"},
                      retryInMs=2000, retryAttempt=1, maxRetries=10)]
    s = T.load_session("claude-code", cc_file(tmp_path, records))
    assert kinds(s) == ["api_error"]
    assert "529" in s.events[0].text and "Overloaded" in s.events[0].text


def test_claude_marks_harness_added_user_content_as_injected(tmp_path):
    records = [
        cc_user("Base directory for this skill: /x", "2026-09-25T10:00:00.000Z", isMeta=True),
        cc_user("<task-notification><status>completed</status></task-notification>",
                "2026-09-25T10:00:01.000Z", origin={"kind": "task-notification"}),
        cc_user([text("<system-reminder>Todo list is empty.</system-reminder>"),
                 text("please rename the helper")], "2026-09-25T10:00:02.000Z",
                origin={"kind": "human"}),
        cc_user("<local-command-stdout>Compacted</local-command-stdout>", "2026-09-25T10:00:03.000Z"),
        cc_env("2026-09-25T10:00:04.000Z", type="attachment", attachment={
            "type": "hook_additional_context", "content": ["Branch is main."],
            "hookName": "SessionStart:startup", "toolUseID": "x", "hookEvent": "SessionStart"}),
        cc_user("Ünïcödé prompt: 修复 🐛", "2026-09-25T10:00:05.000Z"),
    ]
    s = T.load_session("claude-code", cc_file(tmp_path, records))
    assert [e.text for e in s.user_prompts()] == ["please rename the helper", "Ünïcödé prompt: 修复 🐛"]
    injected = [e.text for e in s.events if e.kind == "user" and e.injected]
    assert injected == ["Base directory for this skill: /x",
                        "<task-notification><status>completed</status></task-notification>",
                        "<system-reminder>Todo list is empty.</system-reminder>",
                        "<local-command-stdout>Compacted</local-command-stdout>",
                        "Branch is main."]


def test_claude_session_fields(tmp_path):
    records = [
        {"type": "queue-operation", "operation": "enqueue", "timestamp": "2026-09-25T09:59:59.000Z",
         "sessionId": SID},
        cc_user("hi", "2026-09-25T10:00:00.000Z"),
        cc_assistant("m1", text("hello"), "2026-09-25T10:00:02.000Z", model="claude-opus-4-8"),
        cc_assistant("m2", text("again"), "2026-09-25T10:00:03.000Z", model="claude-fable-5",
                     version="2.1.290"),
        {"type": "last-prompt", "lastPrompt": "hi", "leafUuid": "u-2"},
        {"type": "cost-state", "totalCostUSD": 0.5, "modelUsage": {}},
    ]
    s = T.load_session("claude-code", cc_file(tmp_path, records))
    assert (s.harness, s.id, s.cwd, s.version) == ("claude-code", SID, "/work/app", "2.1.284")
    assert (s.started, s.ended) == ("2026-09-25T10:00:00.000Z", "2026-09-25T10:00:03.000Z")
    assert s.models == ["claude-opus-4-8", "claude-fable-5"]
    assert (s.is_subagent, s.parent_id, s.warnings) == (False, "", [])
    assert s.path.endswith(SID + ".jsonl")


def test_claude_subagent_file_is_its_own_session(tmp_path):
    rec = cc_assistant("m1", text("sub result"), "2026-09-25T10:00:01.000Z", isSidechain=True,
                       agentId="a1b2c3d4e5f6a7b8c")
    path = write_jsonl(os.path.join(cc_project(tmp_path), SID, "subagents",
                                    "agent-a1b2c3d4e5f6a7b8c.jsonl"), [rec])
    s = T.load_session("claude-code", path)
    assert (s.is_subagent, s.parent_id, s.id) == (True, SID, "a1b2c3d4e5f6a7b8c")
    assert all(e.sidechain for e in s.events)


def test_malformed_lines_and_unknown_records_become_warnings(tmp_path):
    records = [
        cc_user("start", "2026-09-25T10:00:00.000Z"),
        '{"type": "user", "message": {"content": "secret-ish text that must not leak"',
        {"type": "brand-new-record", "payload": {"k": 1}},
        {"no_type_at_all": True},
        cc_assistant("m1", text("still parsed"), "2026-09-25T10:00:02.000Z"),
    ]
    s = T.load_session("claude-code", cc_file(tmp_path, records))
    assert kinds(s) == ["user", "assistant"]
    assert len(s.warnings) == 3
    assert not any("secret-ish" in w for w in s.warnings)
    assert any("brand-new-record" in w for w in s.warnings)


def test_unreadable_or_missing_file_gives_an_empty_session_with_a_warning(tmp_path):
    s = T.load_session("claude-code", str(tmp_path / "missing.jsonl"))
    assert s.events == [] and len(s.warnings) == 1


# ---------------------------------------------------------------------------
# Finding sessions (Claude Code layout plus the shared rules)
# ---------------------------------------------------------------------------

def _cc_minimal(home, sid, cwd="/work/app", age_days=1):
    path = cc_file(home, [cc_user("hi", "2026-09-25T10:00:00.000Z", cwd=cwd, sessionId=sid)],
                   sid=sid, cwd=cwd)
    set_age(path, age_days)
    return path


def test_find_sessions_filters_by_modified_time_and_lists_newest_first(tmp_path):
    new = _cc_minimal(tmp_path, "s-new", age_days=1)
    mid = _cc_minimal(tmp_path, "s-mid", age_days=5)
    _cc_minimal(tmp_path, "s-old", age_days=40)
    assert T.find_sessions(home=str(tmp_path)) == [("claude-code", new), ("claude-code", mid)]
    assert len(T.find_sessions(home=str(tmp_path), since_days=None)) == 3


def test_find_sessions_skips_set_aside_transcripts(tmp_path):
    keep = _cc_minimal(tmp_path, "s-keep")
    folder = cc_project(tmp_path)
    for name in ("s-keep.orphaned-1727000000-ab12.jsonl", "s-keep.jsonl.superseded-1727000000"):
        write_jsonl(os.path.join(folder, name), [cc_user("x", "2026-09-25T10:00:00.000Z")])
    assert T.find_sessions(home=str(tmp_path)) == [("claude-code", keep)]


def test_find_sessions_includes_subagent_files_unless_told_not_to(tmp_path):
    main = _cc_minimal(tmp_path, SID)
    sub = write_jsonl(os.path.join(cc_project(tmp_path), SID, "subagents", "agent-a1.jsonl"),
                      [cc_user("task", "2026-09-25T10:00:00.000Z", isSidechain=True)])
    set_age(sub, 2)
    write_jsonl(os.path.join(cc_project(tmp_path), SID, "subagents", "agent-a1.meta.json"), [{}])
    assert T.find_sessions(home=str(tmp_path)) == [("claude-code", main), ("claude-code", sub)]
    assert T.find_sessions(home=str(tmp_path), include_subagents=False) == [("claude-code", main)]


def test_find_sessions_project_filter_matches_the_folder_and_its_subfolders(tmp_path):
    app = _cc_minimal(tmp_path, "s-app", cwd="/work/app", age_days=1)
    sub = _cc_minimal(tmp_path, "s-sub", cwd="/work/app/api", age_days=2)
    _cc_minimal(tmp_path, "s-two", cwd="/work/app-two", age_days=3)   # same encoded prefix
    _cc_minimal(tmp_path, "s-else", cwd="/work/other", age_days=4)
    got = T.find_sessions(home=str(tmp_path), project="/work/app")
    assert got == [("claude-code", app), ("claude-code", sub)]


def test_home_argument_ignores_the_environment_and_env_vars_apply_without_it(tmp_path, monkeypatch):
    alt = tmp_path / "alt-config"
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(alt))
    in_home = _cc_minimal(tmp_path / "home", "s-home")
    enc = os.path.join(str(alt), "projects", "-work-app")
    in_alt = write_jsonl(os.path.join(enc, "s-alt.jsonl"), [cc_user("x", "2026-09-25T10:00:00.000Z")])
    assert T.find_sessions(home=str(tmp_path / "home")) == [("claude-code", in_home)]
    assert T.find_sessions(harness="claude-code") == [("claude-code", in_alt)]


def test_find_sessions_skips_cursor_and_rejects_unknown_harnesses(tmp_path):
    write_jsonl(os.path.join(str(tmp_path), ".cursor", "projects", "Users-x-app", "agent-transcripts",
                             "c1", "c1.jsonl"), [{"role": "user", "message": {"content": []}}])
    assert T.find_sessions(harness="cursor", home=str(tmp_path)) == []
    with pytest.raises(ValueError):
        T.find_sessions(harness="aider", home=str(tmp_path))


def test_detect_harnesses_reports_roots_that_exist(tmp_path):
    assert T.detect_harnesses(home=str(tmp_path)) == {}
    os.makedirs(os.path.join(str(tmp_path), ".claude", "projects"))
    os.makedirs(os.path.join(str(tmp_path), ".codex", "sessions"))
    os.makedirs(os.path.join(str(tmp_path), ".gemini", "tmp"))
    os.makedirs(os.path.join(str(tmp_path), ".cursor", "projects"))
    assert T.detect_harnesses(home=str(tmp_path)) == {
        "claude-code": os.path.join(str(tmp_path), ".claude"),
        "codex": os.path.join(str(tmp_path), ".codex"),
        "gemini-cli": os.path.join(str(tmp_path), ".gemini"),
    }


def test_iter_sessions_loads_what_find_sessions_finds(tmp_path):
    _cc_minimal(tmp_path, "s-1", age_days=1)
    _cc_minimal(tmp_path, "s-2", age_days=2)
    got = list(T.iter_sessions(home=str(tmp_path), harness="claude-code"))
    assert [s.id for s in got] == ["s-1", "s-2"]
    assert all(s.user_prompts()[0].text == "hi" for s in got)


# ---------------------------------------------------------------------------
# read_new_events (hooks that read a growing file)
# ---------------------------------------------------------------------------

def _append(path, records):
    with open(path, "a", encoding="utf-8") as fh:
        for rec in records:
            fh.write(rec if isinstance(rec, str) else json.dumps(rec) + "\n")


def test_read_new_events_holds_back_the_newest_response_until_the_next_starts(tmp_path):
    path = cc_file(tmp_path, [
        cc_user("run the tests", "2026-09-25T10:00:00.000Z"),
        cc_assistant("msg_A", thinking(), "2026-09-25T10:00:01.000Z", cc_usage(inp=5, read=100, out=3)),
    ])
    events, off1 = T.read_new_events("claude-code", path, 0)
    assert [e.kind for e in events] == ["user"]
    held = os.path.getsize(path)
    assert 0 < off1 < held          # msg_A starts at off1 and is not consumed yet

    # The same response keeps streaming: its tool call, the result, then the next response.
    _append(path, [
        cc_assistant("msg_A", tool_use("t1", "Bash", {"command": "pytest"}), "2026-09-25T10:00:02.000Z",
                     cc_usage(inp=5, read=100, out=9)),
        cc_result("t1", "Exit code 1\nFAILED", "2026-09-25T10:00:05.000Z", is_error=True),
        cc_assistant("msg_B", text("One test fails."), "2026-09-25T10:00:06.000Z",
                     cc_usage(inp=2, read=200, out=4)),
    ])
    events, off2 = T.read_new_events("claude-code", path, off1)
    assert [e.kind for e in events] == ["assistant", "tool"]
    assert events[0].usage == T.Usage(input=5, cache_read=100, output=9)   # counted once, final value
    assert events[1].tool.has_result and events[1].tool.exit_code == 1

    _append(path, [cc_user("thanks", "2026-09-25T10:01:00.000Z"),
                   cc_assistant("msg_C", text("Welcome."), "2026-09-25T10:01:01.000Z")])
    events, off3 = T.read_new_events("claude-code", path, off2)
    assert [e.kind for e in events] == ["assistant", "user"]
    assert events[0].text == "One test fails." and events[0].usage.output == 4
    assert off3 < os.path.getsize(path)


def test_read_new_events_skips_a_line_still_being_written(tmp_path):
    path = cc_file(tmp_path, [cc_user("first", "2026-09-25T10:00:00.000Z")])
    complete = os.path.getsize(path)
    _append(path, ['{"type": "user", "message": {"content": "sec'])
    events, off = T.read_new_events("claude-code", path, 0)
    assert [e.text for e in events] == ["first"] and off == complete
    events, off_again = T.read_new_events("claude-code", path, off)
    assert events == [] and off_again == complete


def test_read_new_events_sums_match_a_full_load(tmp_path):
    records = []
    for i in range(6):
        mid = "msg_%d" % i
        records.append(cc_assistant(mid, thinking(), "2026-09-25T10:00:%02d.000Z" % (i * 5),
                                    cc_usage(inp=i, read=10 * i, out=1)))
        records.append(cc_assistant(mid, tool_use("t%d" % i, "Read", {"file_path": "/w/f%d" % i}),
                                    "2026-09-25T10:00:%02d.500Z" % (i * 5), cc_usage(inp=i, read=10 * i, out=7)))
        records.append(cc_result("t%d" % i, "ok", "2026-09-25T10:00:%02d.900Z" % (i * 5)))
    records.append(cc_assistant("msg_end", text("done"), "2026-09-25T10:01:00.000Z", cc_usage(out=2)))
    path = cc_file(tmp_path, [])
    offset, total, calls = 0, T.Usage(), []
    for rec in records:        # feed the file one line at a time, reading after each
        _append(path, [rec])
        events, offset = T.read_new_events("claude-code", path, offset)
        for e in events:
            if e.usage:
                total = total + e.usage
            if e.tool:
                calls.append((e.tool.id, e.tool.has_result))
    full = T.load_session("claude-code", path)
    last = full.events[-1]
    assert total + last.usage == full.usage_total()     # only the newest response is still held back
    assert calls == [("t%d" % i, True) for i in range(6)]


def test_read_new_events_handles_a_missing_file_and_rejects_other_harnesses(tmp_path):
    assert T.read_new_events("claude-code", str(tmp_path / "nope.jsonl"), 0) == ([], 0)
    for harness in ("gemini-cli", "opencode"):
        with pytest.raises(ValueError):
            T.read_new_events(harness, str(tmp_path / "x"), 0)


# ---------------------------------------------------------------------------
# Codex (facts Q1.2, plus item_completed records seen in real rollouts)
# ---------------------------------------------------------------------------

TID = "019a2b3c-4d5e-7f60-8a9b-0c1d2e3f4a5b"
_ordinal = itertools.count(1)


def cx(ts, kind, payload):
    return {"timestamp": ts, "ordinal": next(_ordinal), "type": kind, "payload": payload}


def cx_meta(ts, tid=TID, cwd="/work/app", source="vscode", **extra):
    payload = {"id": tid, "session_id": tid, "timestamp": ts, "cwd": cwd, "originator": "Codex Desktop",
               "cli_version": "0.155.0", "source": source, "thread_source": "user",
               "model_provider": "openai", "base_instructions": {"text": "You are Codex."},
               "history_mode": "legacy", "context_window": {"window_id": "w1"}}
    payload.update(extra)
    rec = cx(ts, "session_meta", payload)
    rec["git"] = {"commit_hash": "abc", "branch": "main", "repository_url": None}
    return rec


def cx_turn(ts, model="gpt-6-astra"):
    return cx(ts, "turn_context", {"turn_id": "turn-1", "cwd": "/work/app", "model": model,
                                   "approval_policy": "on-request", "effort": "medium",
                                   "sandbox_policy": {"type": "workspace-write", "network_access": False},
                                   "current_date": "2026-09-25", "timezone": "UTC"})


def cx_msg(ts, role, *texts):
    item = "output_text" if role == "assistant" else "input_text"
    return cx(ts, "response_item", {"type": "message", "role": role,
                                    "content": [{"type": item, "text": t} for t in texts]})


def cx_tokens(inp, cached, out, reasoning, write=0):
    return {"input_tokens": inp, "cached_input_tokens": cached, "cache_write_input_tokens": write,
            "output_tokens": out, "reasoning_output_tokens": reasoning, "total_tokens": inp + out}


def cx_record(ts, response_id, usage):
    return cx(ts, "token_usage_record", {"thread_id": TID, "turn_id": "turn-1", "session_id": TID,
                                         "root_turn_id": "turn-1", "response_id": response_id,
                                         "usage": usage, "turn_token_usage": usage,
                                         "thread_token_usage": usage})


def cx_count(ts, total, last):
    info = None if total is None else {"total_token_usage": total, "last_token_usage": last,
                                       "model_context_window": 258400}
    return cx(ts, "event_msg", {"type": "token_count", "info": info,
                                "rate_limits": {"primary": {"used_percent": 1.0, "window_minutes": 300}}})


def cx_exec(ts, call_id, script):
    return cx(ts, "response_item", {"type": "custom_tool_call", "name": "exec", "input": script,
                                    "call_id": call_id, "status": "completed"})


def cx_custom_out(ts, call_id, text_out):
    return cx(ts, "response_item", {"type": "custom_tool_call_output", "call_id": call_id,
                                    "output": [{"type": "input_text", "text": text_out}]})


def cx_item(ts, item):
    return cx(ts, "event_msg", {"type": "item_completed", "thread_id": TID, "turn_id": "turn-1",
                                "started_at_ms": 1, "completed_at_ms": 2, "item": item})


def cx_command(item_id, command, exit_code=0, status="completed", output="", parsed=None):
    return {"type": "CommandExecution", "id": item_id, "process_id": "p1",
            "command": ["/bin/zsh", "-lc", command], "cwd": "/work/app",
            "parsed_cmd": parsed or [{"type": "unknown", "cmd": command}], "source": "unified_exec_startup",
            "status": status, "stdout": output, "stderr": "", "aggregated_output": output,
            "exit_code": exit_code, "duration": {"secs": 1, "nanos": 0}, "formatted_output": output}


def cx_fc(ts, call_id, name, args, namespace=None):
    payload = {"type": "function_call", "name": name, "arguments": json.dumps(args), "call_id": call_id}
    if namespace:
        payload["namespace"] = namespace
    return cx(ts, "response_item", payload)


def cx_fc_out(ts, call_id, output):
    return cx(ts, "response_item", {"type": "function_call_output", "call_id": call_id, "output": output})


def cx_file(home, records, name="rollout-2026-09-25T10-00-00-%s.jsonl" % TID, day=("2026", "09", "25")):
    return write_jsonl(os.path.join(str(home), ".codex", "sessions", day[0], day[1], day[2], name), records)


def test_codex_usage_comes_from_usage_records_by_response_id(tmp_path):
    u1, u2, compact = cx_tokens(1000, 800, 150, 100), cx_tokens(1300, 1000, 40, 0), cx_tokens(240000, 0, 9000, 0)
    records = [
        cx_meta("2026-09-25T10:00:00.000Z"), cx_turn("2026-09-25T10:00:00.100Z"),
        cx_msg("2026-09-25T10:00:00.200Z", "user", "fix it"),
        cx_msg("2026-09-25T10:00:03.000Z", "assistant", "Looking."),
        cx_record("2026-09-25T10:00:03.100Z", "resp_1", u1), cx_count("2026-09-25T10:00:03.200Z", u1, u1),
        cx_count("2026-09-25T10:00:03.300Z", u1, u1),
        cx_record("2026-09-25T10:00:05.000Z", "resp_2", u2), cx_count("2026-09-25T10:00:05.100Z", u2, u2),
        cx_record("2026-09-25T10:00:06.000Z", "resp_compact", compact),   # compaction call: no token_count
        cx_record("2026-09-25T10:00:06.500Z", "resp_2", u2),              # same response id again
    ]
    s = T.load_session("codex", cx_file(tmp_path, records))
    usages = [e.usage for e in s.events if e.usage]
    assert usages == [T.Usage(input=200, cache_read=800, output=150, reasoning=100),
                      T.Usage(input=300, cache_read=1000, output=40),
                      T.Usage(input=240000, output=9000)]
    assert s.events[1].kind == "assistant" and s.events[1].text == "Looking."
    assert s.usage_by_model() == {"gpt-6-astra": usages[0] + usages[1] + usages[2]}


def test_codex_usage_falls_back_to_token_counts_without_repeats(tmp_path):
    a, b = cx_tokens(500, 100, 20, 5), cx_tokens(700, 600, 30, 0)
    records = [
        cx_meta("2026-09-25T10:00:00.000Z"), cx_turn("2026-09-25T10:00:00.100Z", model="gpt-6-sol"),
        cx_count("2026-09-25T10:00:01.000Z", a, a),
        cx_count("2026-09-25T10:00:01.100Z", a, a),        # repeat with the same total
        cx_count("2026-09-25T10:00:01.200Z", None, None),  # rate-limit-only update
        cx_turn("2026-09-25T10:00:02.000Z", model="gpt-6-luna"),
        cx_count("2026-09-25T10:00:03.000Z", cx_tokens(1200, 700, 50, 5), b),
    ]
    s = T.load_session("codex", cx_file(tmp_path, records))
    assert s.usage_by_model() == {"gpt-6-sol": T.Usage(input=400, cache_read=100, output=20, reasoning=5),
                                  "gpt-6-luna": T.Usage(input=100, cache_read=600, output=30)}
    assert s.models == ["gpt-6-sol", "gpt-6-luna"]


def test_codex_session_fields_and_subagents(tmp_path):
    main = T.load_session("codex", cx_file(tmp_path, [cx_meta("2026-09-25T10:00:00.000Z"),
                                                      cx_msg("2026-09-25T10:05:00.000Z", "user", "hi")]))
    assert (main.id, main.cwd, main.version, main.is_subagent) == (TID, "/work/app", "0.155.0", False)
    assert (main.started, main.ended) == ("2026-09-25T10:00:00.000Z", "2026-09-25T10:05:00.000Z")
    spawn = {"subagent": {"thread_spawn": {"parent_thread_id": "parent-1", "depth": 1,
                                           "agent_path": "/root/x", "agent_nickname": "x", "agent_role": "worker"}}}
    child = T.load_session("codex", cx_file(tmp_path, [
        cx_meta("2026-09-25T10:00:00.000Z", tid="child-1", source=spawn, parent_thread_id="parent-1"),
        cx_msg("2026-09-25T10:00:01.000Z", "user", "look into the parser")], name="rollout-child.jsonl"))
    assert (child.id, child.is_subagent, child.parent_id) == ("child-1", True, "parent-1")
    assert [e.text for e in child.user_prompts()] == ["look into the parser"]
    reviewer = T.load_session("codex", cx_file(tmp_path, [
        cx_meta("2026-09-25T10:00:00.000Z", tid="rev-1", source={"subagent": {"other": "guardian"}},
                parent_thread_id="parent-1"),
        cx_turn("2026-09-25T10:00:00.100Z", model="codex-auto-review"),
        cx_msg("2026-09-25T10:00:01.000Z", "user", "Review this command: rm -rf build")],
        name="rollout-rev.jsonl"))
    assert (reviewer.is_subagent, reviewer.parent_id, reviewer.user_prompts()) == (True, "parent-1", [])
    assert reviewer.models == ["codex-auto-review"]


def test_codex_exec_script_is_replaced_by_the_operations_it_ran(tmp_path):
    script = 'text(await tools.exec_command({cmd: "pytest -q"}))'
    records = [
        cx_meta("2026-09-25T10:00:00.000Z"), cx_turn("2026-09-25T10:00:00.100Z"),
        cx_exec("2026-09-25T10:00:01.000Z", "call_1", script),
        cx_item("2026-09-25T10:00:02.000Z", cx_command("i1", "cat src/app.py", output="print()",
                                                       parsed=[{"type": "read", "cmd": "cat src/app.py",
                                                                "name": "app.py", "path": "src/app.py"}])),
        cx_item("2026-09-25T10:00:03.000Z", cx_command("i2", "pytest -q", exit_code=1, status="failed",
                                                       output="1 failed")),
        cx_item("2026-09-25T10:00:04.000Z", {"type": "FileChange", "id": "i3", "status": "completed",
                                              "stdout": "", "stderr": "", "changes": {
                                                  "/work/app/src/app.py": {"type": "update", "unified_diff": "@@",
                                                                           "move_path": None},
                                                  "/work/app/tests/test_new.py": {"type": "add", "content": "x"}}}),
        cx_item("2026-09-25T10:00:05.000Z", {"type": "McpToolCall", "id": "i4", "server": "docs", "tool": "search",
                                              "arguments": {"q": "hooks"}, "status": "failed",
                                              "result": {"content": [{"type": "text", "text": "timeout"}]},
                                              "duration": {"secs": 1, "nanos": 0}}),
        cx_item("2026-09-25T10:00:06.000Z", {"type": "Extension", "kind": "web.search", "id": "i5",
                                              "query": "codex hooks", "action": {"type": "search", "query": "q",
                                                                                 "queries": ["q"]},
                                              "results": []}),
        cx_item("2026-09-25T10:00:07.000Z", {"type": "ImageView", "id": "i6", "path": "/work/app/shot.png"}),
        cx_custom_out("2026-09-25T10:00:08.000Z", "call_1", "Script completed\nWall time 7.0 seconds\nOutput:\n..."),
    ]
    s = T.load_session("codex", cx_file(tmp_path, records))
    got = [(c.name, c.kind, c.command, c.paths, c.exit_code, c.is_error) for c in s.tool_calls()]
    assert got == [
        ("exec_command", "shell", "cat src/app.py", ["src/app.py"], 0, False),
        ("exec_command", "shell", "pytest -q", [], 1, True),
        ("apply_patch", "edit", "", ["/work/app/src/app.py", "/work/app/tests/test_new.py"], None, False),
        ("mcp__docs__search", "mcp", "", [], None, True),
        ("web.search", "web", "", [], None, False),
        ("view_image", "read", "", ["/work/app/shot.png"], None, False),
    ]
    assert all(c.has_result for c in s.tool_calls())
    assert s.tool_calls()[1].output == "1 failed"
    assert s.tool_calls()[3].output == "timeout"


def test_codex_exec_script_without_operations_is_itself_the_call(tmp_path):
    records = [cx_meta("2026-09-25T10:00:00.000Z"),
               cx_exec("2026-09-25T10:00:01.000Z", "call_1", "const x = 1 + 1; text(x)"),
               cx_custom_out("2026-09-25T10:00:02.000Z", "call_1", "Script failed\nWall time 0.1 seconds\nOutput:\nboom")]
    [call] = T.load_session("codex", cx_file(tmp_path, records)).tool_calls()
    assert (call.name, call.kind, call.command, call.is_error, call.exit_code) == \
        ("exec", "shell", "const x = 1 + 1; text(x)", True, None)


def test_codex_function_calls_patches_and_shell_calls(tmp_path):
    patch = ("*** Begin Patch\n*** Update File: src/a.py\n@@\n-x\n+y\n*** Add File: src/b.py\n+z\n"
             "*** End Patch")
    records = [
        cx_meta("2026-09-25T10:00:00.000Z"),
        cx_fc("2026-09-25T10:00:01.000Z", "c1", "exec_command", {"cmd": "npm test", "workdir": "/work/app"}),
        cx_fc_out("2026-09-25T10:00:02.000Z", "c1", "Chunk ID: ab12\nWall time: 1.5 seconds\n"
                                                    "Process exited with code 3\nOriginal token count: 9\nOutput:\nfail"),
        cx_fc("2026-09-25T10:00:03.000Z", "c2", "spawn_agent", {"task_name": "scan"}, namespace="collaboration"),
        cx_fc_out("2026-09-25T10:00:04.000Z", "c2", '{"accepted":true}'),
        cx_fc("2026-09-25T10:00:05.000Z", "c3", "js", {"code": "1"}, namespace="mcp__cua_repl"),
        cx_item("2026-09-25T10:00:05.500Z", {"type": "McpToolCall", "id": "c3", "server": "cua_repl", "tool": "js",
                                              "arguments": {"code": "1"}, "status": "failed", "result": None,
                                              "duration": {"secs": 0, "nanos": 1}}),
        cx_fc_out("2026-09-25T10:00:06.000Z", "c3", [{"type": "input_text", "text": "error: boom"}]),
        cx(ts="2026-09-25T10:00:07.000Z", kind="response_item", payload={
            "type": "custom_tool_call", "name": "apply_patch", "input": patch, "call_id": "c4",
            "status": "completed"}),
        cx_custom_out("2026-09-25T10:00:08.000Z", "c4", "Exit code: 0\nWall time: 0 seconds\nOutput:\nSuccess."),
        cx(ts="2026-09-25T10:00:09.000Z", kind="response_item", payload={
            "type": "local_shell_call", "call_id": "c5", "status": "completed",
            "action": {"type": "exec", "command": ["bash", "-lc", "ls -la"], "timeout_ms": 1000}}),
        cx_fc_out("2026-09-25T10:00:10.000Z", "c5", json.dumps({"output": "total 0",
                                                               "metadata": {"exit_code": 0, "duration_seconds": 0.1}})),
        cx(ts="2026-09-25T10:00:11.000Z", kind="response_item", payload={
            "type": "web_search_call", "status": "completed", "action": {"type": "search", "query": "q"}}),
    ]
    s = T.load_session("codex", cx_file(tmp_path, records))
    got = [(c.name, c.kind, c.command, c.paths, c.exit_code, c.is_error, c.has_result) for c in s.tool_calls()]
    assert got == [
        ("exec_command", "shell", "npm test", [], 3, True, True),
        ("collaboration__spawn_agent", "agent", "", [], None, False, True),
        ("mcp__cua_repl__js", "mcp", "", [], None, True, True),
        ("apply_patch", "edit", "", ["src/a.py", "src/b.py"], 0, False, True),
        ("local_shell_call", "shell", "ls -la", [], 0, False, True),
        ("web_search_call", "web", "", [], None, False, True),
    ]
    assert s.tool_calls()[0].input == {"cmd": "npm test", "workdir": "/work/app"}
    assert s.tool_calls()[2].output == "error: boom"
    assert s.tool_calls()[4].output == "total 0"


@pytest.mark.parametrize("marker,kind", [
    ("exec command rejected by user", "user-rejected"),
    ("patch rejected by user", "user-rejected"),
    ("rejected by configuration", "hook"),
    ("automatic approval review denied the action", "auto-reviewer"),
])
def test_codex_denials_are_read_from_the_output_text(tmp_path, marker, kind):
    records = [cx_meta("2026-09-25T10:00:00.000Z"),
               cx_fc("2026-09-25T10:00:01.000Z", "c1", "exec_command", {"cmd": "git push -f"}),
               cx_fc_out("2026-09-25T10:00:02.000Z", "c1", "Error: " + marker)]
    [call] = T.load_session("codex", cx_file(tmp_path, records)).tool_calls()
    assert (call.denied, call.is_error) == (kind, True)


def test_codex_turn_aborted_is_an_interrupt(tmp_path):
    records = [
        cx_meta("2026-09-25T10:00:00.000Z"),
        cx_exec("2026-09-25T10:00:01.000Z", "call_1", "text(await tools.exec_command({cmd: 'sleep 99'}))"),
        cx_custom_out("2026-09-25T10:00:09.000Z", "call_1", "aborted"),
        cx(ts="2026-09-25T10:00:09.100Z", kind="event_msg", payload={
            "type": "turn_aborted", "reason": "interrupted", "turn_id": "turn-1", "duration_ms": 8000}),
    ]
    s = T.load_session("codex", cx_file(tmp_path, records))
    assert kinds(s) == ["tool", "interrupt"]
    assert s.events[1].text == "interrupted"
    assert s.tool_calls()[0].interrupted is True and s.tool_calls()[0].is_error is False


def test_codex_compacted_line_is_a_compaction(tmp_path):
    records = [
        cx_meta("2026-09-25T10:00:00.000Z"),
        cx("2026-09-25T10:00:00.500Z", "response_item", {"type": "compaction", "encrypted_content": "gAAA"}),
        cx("2026-09-25T10:30:00.000Z", "compacted", {"message": "Summary: parser fixed.", "replacement_history": [],
                                                     "window_number": 2}),
    ]
    s = T.load_session("codex", cx_file(tmp_path, records))
    assert kinds(s) == ["compaction"]
    assert s.events[0].text == "Summary: parser fixed." and s.events[0].ts == "2026-09-25T10:30:00.000Z"


def test_codex_marks_harness_context_as_injected(tmp_path):
    records = [
        cx_meta("2026-09-25T10:00:00.000Z"),
        cx_msg("2026-09-25T10:00:00.100Z", "developer", "<permissions>sandbox: workspace-write</permissions>"),
        cx_msg("2026-09-25T10:00:00.200Z", "user", "# AGENTS.md instructions for /work/app\n\n<INSTRUCTIONS>x"
                                                   "</INSTRUCTIONS>",
               "<environment_context>\n  <cwd>/work/app</cwd>\n</environment_context>"),
        cx_msg("2026-09-25T10:00:01.000Z", "user", "please add a test"),
    ]
    s = T.load_session("codex", cx_file(tmp_path, records))
    assert [e.text for e in s.user_prompts()] == ["please add a test"]
    assert len([e for e in s.events if e.kind == "user" and e.injected]) == 2


def test_codex_unknown_lines_are_counted(tmp_path):
    records = [cx_meta("2026-09-25T10:00:00.000Z"),
               cx("2026-09-25T10:00:01.000Z", "world_state", {"full": True, "state": {}}),
               cx("2026-09-25T10:00:02.000Z", "hologram", {}),
               cx("2026-09-25T10:00:03.000Z", "response_item", {"type": "teleport_call"})]
    s = T.load_session("codex", cx_file(tmp_path, records))
    assert len(s.warnings) == 2


def test_codex_compressed_rollouts_are_listed_and_skipped_with_a_warning(tmp_path):
    folder = os.path.join(str(tmp_path), ".codex", "sessions", "2026", "09", "25")
    os.makedirs(folder)
    zst = os.path.join(folder, "rollout-2026-09-25T10-00-00-%s.jsonl.zst" % TID)
    with open(zst, "wb") as fh:
        fh.write(b"\x28\xb5\x2f\xfd")
    assert T.find_sessions(home=str(tmp_path)) == [("codex", zst)]
    s = T.load_session("codex", zst)
    assert s.events == [] and len(s.warnings) == 1 and "zst" in s.warnings[0]


def test_codex_find_sessions_reads_only_the_first_line_to_filter(tmp_path, monkeypatch):
    spawn = {"subagent": {"thread_spawn": {"parent_thread_id": TID, "depth": 1}}}
    main = cx_file(tmp_path, [cx_meta("2026-09-25T10:00:00.000Z")], name="rollout-a-main.jsonl")
    child = cx_file(tmp_path, [cx_meta("2026-09-25T10:00:00.000Z", tid="c1", source=spawn)],
                    name="rollout-b-child.jsonl")
    other = cx_file(tmp_path, [cx_meta("2026-09-25T10:00:00.000Z", tid="o1", cwd="/work/other")],
                    name="rollout-c-other.jsonl")
    archived = write_jsonl(os.path.join(str(tmp_path), ".codex", "archived_sessions", "rollout-d.jsonl"),
                           [cx_meta("2026-09-20T10:00:00.000Z", tid="d1")])
    old = cx_file(tmp_path, [cx_meta("2026-07-01T10:00:00.000Z", tid="old")], name="rollout-e-old.jsonl",
                  day=("2026", "07", "01"))
    for i, p in enumerate([main, child, other, archived]):
        set_age(p, i + 1)
    set_age(old, 60)
    home = str(tmp_path)
    assert T.find_sessions(home=home) == [("codex", p) for p in (main, child, other, archived)]
    assert T.find_sessions(home=home, include_subagents=False) == [("codex", p) for p in (main, other, archived)]
    assert T.find_sessions(home=home, project="/work/app") == [("codex", p) for p in (main, child, archived)]
    monkeypatch.setenv("CODEX_HOME", os.path.join(home, ".codex"))
    assert T.find_sessions(harness="codex", since_days=None)[-1] == ("codex", old)


def test_codex_read_new_events_holds_back_the_newest_response(tmp_path):
    u1 = cx_tokens(100, 0, 10, 0)
    path = cx_file(tmp_path, [
        cx_meta("2026-09-25T10:00:00.000Z"), cx_turn("2026-09-25T10:00:00.100Z"),
        cx_msg("2026-09-25T10:00:00.200Z", "user", "go"),
        cx_exec("2026-09-25T10:00:01.000Z", "call_1", "text(await tools.exec_command({cmd: 'ls'}))"),
        cx_record("2026-09-25T10:00:01.100Z", "r1", u1), cx_count("2026-09-25T10:00:01.200Z", u1, u1),
    ])
    events, off = T.read_new_events("codex", path, 0)
    assert [e.kind for e in events] == ["user"]           # the response with the exec call is held back
    u2 = cx_tokens(150, 100, 5, 0)
    _append(path, [
        cx_item("2026-09-25T10:00:02.000Z", cx_command("i1", "ls", output="a b")),
        cx_custom_out("2026-09-25T10:00:03.000Z", "call_1", "Script completed\nWall time 1 seconds\nOutput:\na b"),
        cx_msg("2026-09-25T10:00:04.000Z", "assistant", "Two files."),
        cx_record("2026-09-25T10:00:04.100Z", "r2", u2), cx_count("2026-09-25T10:00:04.200Z", u2, u2),
    ])
    events, off2 = T.read_new_events("codex", path, off)
    assert [e.kind for e in events] == ["assistant", "tool"]   # the response first, then what it ran
    assert events[0].usage == T.Usage(input=100, output=10)
    assert events[1].tool.command == "ls" and events[1].tool.has_result
    assert off < off2 < os.path.getsize(path)


def test_codex_text_that_mentions_usage_records_does_not_switch_the_usage_source(tmp_path):
    a = cx_tokens(500, 100, 20, 5)
    records = [cx_meta("2026-09-25T10:00:00.000Z"), cx_turn("2026-09-25T10:00:00.100Z"),
               cx_msg("2026-09-25T10:00:00.200Z", "user", 'why is {"type":"token_usage_record"} missing?'),
               cx_count("2026-09-25T10:00:01.000Z", a, a)]
    path = cx_file(tmp_path, [json.dumps(r, separators=(",", ":")) for r in records])
    assert T.load_session("codex", path).usage_total() == T.Usage(input=400, cache_read=100, output=20,
                                                                   reasoning=5)


def _replay_total(path, lines, harness="codex"):
    """Append lines one at a time, reading after each; return summed usage and the offset."""
    open(path, "w").close()
    offset, total = 0, T.Usage()
    for line in lines:
        _append(path, [line])
        events, offset = T.read_new_events(harness, path, offset)
        for e in events:
            if e.usage:
                total = total + e.usage
    return total, offset


def test_codex_read_new_events_uses_the_files_usage_source_across_reads(tmp_path):
    # In files with usage records, a token_count can repeat after the next
    # response has started; a read that sees only that repeat must not count it.
    u1, u2 = cx_tokens(100, 0, 10, 0), cx_tokens(300, 100, 20, 0)
    lines = [
        cx_meta("2026-09-25T10:00:00.000Z"), cx_turn("2026-09-25T10:00:00.100Z"),
        cx_exec("2026-09-25T10:00:01.000Z", "call_1", "text(1)"),
        cx_record("2026-09-25T10:00:01.100Z", "r1", u1),
        cx_custom_out("2026-09-25T10:00:02.000Z", "call_1", "Script completed"),
        cx_count("2026-09-25T10:00:02.100Z", u1, u1),
        cx(ts="2026-09-25T10:00:03.000Z", kind="response_item", payload={"type": "reasoning", "summary": []}),
        cx_count("2026-09-25T10:00:03.100Z", u1, u1),              # the same total again
        cx_msg("2026-09-25T10:00:04.000Z", "assistant", "done"),
        cx_record("2026-09-25T10:00:04.100Z", "r2", u2), cx_count("2026-09-25T10:00:04.200Z", u2, u2),
        cx_msg("2026-09-25T10:00:05.000Z", "user", "next"),
        cx_msg("2026-09-25T10:00:06.000Z", "assistant", "ok"),
    ]
    path = cx_file(tmp_path, [])
    total, _ = _replay_total(path, lines)
    assert total == T.load_session("codex", path).usage_total()   # nothing counted twice
    assert total == T.Usage(input=300, cache_read=100, output=30)


def test_codex_read_new_events_drops_repeated_token_counts_across_reads(tmp_path):
    a, b = cx_tokens(100, 0, 10, 0), cx_tokens(250, 50, 15, 0)
    lines = [
        cx_meta("2026-09-25T10:00:00.000Z"), cx_turn("2026-09-25T10:00:00.100Z"),
        cx_msg("2026-09-25T10:00:01.000Z", "assistant", "one"), cx_count("2026-09-25T10:00:01.100Z", a, a),
        cx(ts="2026-09-25T10:00:02.000Z", kind="response_item", payload={"type": "reasoning", "summary": []}),
        cx_count("2026-09-25T10:00:02.100Z", a, a),              # repeat after the next response began
        cx_msg("2026-09-25T10:00:03.000Z", "assistant", "two"),
        cx_count("2026-09-25T10:00:03.100Z", cx_tokens(350, 50, 25, 0), b),
        cx_msg("2026-09-25T10:00:04.000Z", "assistant", "three"),
    ]
    path = cx_file(tmp_path, [])
    total, _ = _replay_total(path, lines)
    assert total == T.Usage(input=100, output=10) + T.Usage(input=200, cache_read=50, output=15)


# ---------------------------------------------------------------------------
# Gemini CLI (facts Q1.3): an append-only operation log
# ---------------------------------------------------------------------------

GSID = "7c9e6679-7425-40de-944b-e07fc1f90ae7"


def gm_header(ts="2026-09-25T10:00:00.000Z", sid=GSID, kind="main", updated=None):
    return {"sessionId": sid, "projectHash": "ab" * 32, "startTime": ts,
            "lastUpdated": updated or ts, "kind": kind}


def gm_user(mid, ts, content):
    return {"id": mid, "timestamp": ts, "type": "user", "content": content}


def gm_tokens(inp, out, cached=0, thoughts=0, tool=0):
    return {"input": inp, "output": out, "cached": cached, "thoughts": thoughts, "tool": tool,
            "total": inp + out + thoughts}


def gm_model(mid, ts, content, tokens=None, tool_calls=None, model="gemini-3.1-pro"):
    rec = {"id": mid, "timestamp": ts, "type": "gemini", "content": content,
           "thoughts": [{"subject": "Plan", "description": "...", "timestamp": ts}],
           "tokens": tokens or gm_tokens(100, 10), "model": model}
    if tool_calls is not None:
        rec["toolCalls"] = tool_calls
    return rec


def gm_call(cid, name, args, status="success", output=None, ts="2026-09-25T10:00:03.000Z", error=None):
    response = {"output": output} if output is not None else ({"error": error} if error else None)
    result = None if response is None else [{"functionResponse": {"id": cid, "name": name, "response": response}}]
    return {"id": cid, "name": name, "args": args, "result": result, "status": status, "timestamp": ts,
            "displayName": name, "description": "", "resultDisplay": "", "renderOutputAsMarkdown": True}


def gm_dir(home, slug="app", root="/work/app"):
    folder = os.path.join(str(home), ".gemini", "tmp", slug)
    os.makedirs(os.path.join(folder, "chats"), exist_ok=True)
    with open(os.path.join(folder, ".project_root"), "w") as fh:
        fh.write(root)
    return folder


def gm_file(home, lines, name="session-2026-09-25T10-00-7c9e6679.jsonl", slug="app", root="/work/app"):
    return write_jsonl(os.path.join(gm_dir(home, slug, root), "chats", name), lines)


def test_gemini_replays_updates_and_rewinds(tmp_path):
    running = gm_call("c1", "run_shell_command", {"command": "npm test"}, status="executing")
    done = gm_call("c1", "run_shell_command", {"command": "npm test"},
                   output="Command: npm test\nOutput: 2 failing\nExit Code: 1")
    lines = [
        gm_header(),
        gm_user("m1", "2026-09-25T10:00:01.000Z", [{"text": "run the tests"}]),
        gm_model("m2", "2026-09-25T10:00:02.000Z", "Running them.", tool_calls=[running]),
        gm_model("m2", "2026-09-25T10:00:02.000Z", "Running them.", tool_calls=[done]),   # same id: replaces
        gm_user("m3", "2026-09-25T10:00:05.000Z", "try again"),
        gm_model("m4", "2026-09-25T10:00:06.000Z", "Again."),
        {"$rewindTo": "m3"},                                                              # drops m3 and m4
        {"$set": {"lastUpdated": "2026-09-25T10:00:09.000Z"}},
        gm_user("m5", "2026-09-25T10:00:10.000Z", "thanks"),
        {"mystery": 1},
    ]
    s = T.load_session("gemini-cli", gm_file(tmp_path, lines))
    assert kinds(s) == ["user", "assistant", "tool", "user"]
    assert [e.text for e in s.user_prompts()] == ["run the tests", "thanks"]
    [call] = s.tool_calls()
    assert (call.kind, call.command, call.has_result, call.exit_code, call.is_error) == \
        ("shell", "npm test", True, 1, True)
    assert "2 failing" in call.output
    assert len(s.warnings) == 1
    assert (s.id, s.cwd, s.version) == (GSID, "/work/app", "")
    assert (s.started, s.ended) == ("2026-09-25T10:00:00.000Z", "2026-09-25T10:00:10.000Z")


def test_gemini_checkpoint_replaces_all_messages_and_unknown_rewind_clears(tmp_path):
    lines = [gm_header(), gm_user("m1", "2026-09-25T10:00:01.000Z", "old one"),
             {"$set": {"messages": [gm_user("k1", "2026-09-25T10:00:02.000Z", "kept")],
                       "lastUpdated": "2026-09-25T10:00:02.000Z"}},
             gm_user("m2", "2026-09-25T10:00:03.000Z", "after")]
    s = T.load_session("gemini-cli", gm_file(tmp_path, lines))
    assert [e.text for e in s.events] == ["kept", "after"]
    s2 = T.load_session("gemini-cli", gm_file(tmp_path, lines + [{"$rewindTo": "nope"}], name="session-b.jsonl"))
    assert s2.events == []


def test_gemini_tokens_become_anthropic_style_usage(tmp_path):
    lines = [gm_header(), gm_model("m1", "2026-09-25T10:00:01.000Z", [{"text": "ok"}],
                                   tokens=gm_tokens(1000, 50, cached=600, thoughts=30), model="gemini-3.1-pro")]
    s = T.load_session("gemini-cli", gm_file(tmp_path, lines))
    assert s.events[0].usage == T.Usage(input=400, cache_read=600, output=80, reasoning=30)
    assert s.events[0].text == "ok"
    assert s.usage_by_model() == {"gemini-3.1-pro": s.events[0].usage} and s.models == ["gemini-3.1-pro"]


@pytest.mark.parametrize("name,args,kind,paths", [
    ("read_file", {"absolute_path": "/w/a.py"}, "read", ["/w/a.py"]),
    ("read_file", {"file_path": "/w/b.py"}, "read", ["/w/b.py"]),
    ("read_many_files", {"paths": ["/w/a.py", "/w/c.py"]}, "read", ["/w/a.py", "/w/c.py"]),
    ("write_file", {"file_path": "/w/n.py", "content": "x"}, "write", ["/w/n.py"]),
    ("replace", {"file_path": "/w/a.py", "old_string": "a", "new_string": "b"}, "edit", ["/w/a.py"]),
    ("glob", {"pattern": "*.py"}, "search", []),
    ("search_file_content", {"pattern": "TODO"}, "search", []),
    ("list_directory", {"path": "/w"}, "search", []),
    ("google_web_search", {"query": "q"}, "web", []),
    ("web_fetch", {"prompt": "https://example.com"}, "web", []),
    ("github__search_issues", {"q": "bug"}, "mcp", []),
    ("save_memory", {"fact": "x"}, "other", []),
])
def test_gemini_tool_kinds_and_paths(tmp_path, name, args, kind, paths):
    lines = [gm_header(), gm_model("m1", "2026-09-25T10:00:01.000Z", "", tool_calls=[
        gm_call("c1", name, args, output="done")])]
    [call] = T.load_session("gemini-cli", gm_file(tmp_path, lines)).tool_calls()
    assert (call.name, call.kind, call.paths, call.input, call.output) == (name, kind, paths, args, "done")


def test_gemini_tool_statuses(tmp_path):
    lines = [gm_header(), gm_model("m1", "2026-09-25T10:00:01.000Z", "", tool_calls=[
        gm_call("c1", "run_shell_command", {"command": "ls"}, status="error", error="Command not allowed"),
        gm_call("c2", "write_file", {"file_path": "/w/x"}, status="cancelled"),
        gm_call("c3", "read_file", {"file_path": "/w/y"}, status="awaiting_approval"),
    ])]
    calls = T.load_session("gemini-cli", gm_file(tmp_path, lines)).tool_calls()
    got = [(c.id, c.is_error, c.denied, c.has_result, c.output) for c in calls]
    assert got == [("c1", True, "", True, "Command not allowed"), ("c2", False, "other", True, ""),
                   ("c3", False, "", False, "")]


def test_gemini_error_messages_are_api_errors_and_notes_are_skipped(tmp_path):
    lines = [gm_header(),
             {"id": "e1", "timestamp": "2026-09-25T10:00:01.000Z", "type": "error", "content": "Quota exceeded"},
             {"id": "i1", "timestamp": "2026-09-25T10:00:02.000Z", "type": "info", "content": "Switched model"},
             {"id": "w1", "timestamp": "2026-09-25T10:00:03.000Z", "type": "warning", "content": "Slow"}]
    s = T.load_session("gemini-cli", gm_file(tmp_path, lines))
    assert kinds(s) == ["api_error"] and s.events[0].text == "Quota exceeded"


def test_gemini_reads_legacy_json_files(tmp_path):
    doc = dict(gm_header(), messages=[gm_user("m1", "2026-02-10T10:00:01.000Z", "hello"),
                                      gm_model("m2", "2026-02-10T10:00:02.000Z", "hi",
                                               tokens=gm_tokens(50, 5), model="gemini-2.5-pro")])
    path = os.path.join(gm_dir(tmp_path), "chats", "session-2026-02-10T10-00-7c9e6679.json")
    with open(path, "w") as fh:
        json.dump(doc, fh)
    s = T.load_session("gemini-cli", path)
    assert kinds(s) == ["user", "assistant"] and s.usage_total() == T.Usage(input=50, output=5)
    assert s.id == GSID


def test_gemini_subagent_chat(tmp_path):
    path = write_jsonl(os.path.join(gm_dir(tmp_path), "chats", GSID, "sub-1.jsonl"),
                       [gm_header(sid="sub-1", kind="subagent"), gm_user("m1", "2026-09-25T10:00:01.000Z", "task")])
    s = T.load_session("gemini-cli", path)
    assert (s.id, s.is_subagent, s.parent_id, s.cwd) == ("sub-1", True, GSID, "/work/app")


def test_gemini_find_sessions_filters_by_time_project_and_subagents(tmp_path):
    home = str(tmp_path)
    keep = gm_file(tmp_path, [gm_header()], name="session-a.jsonl")
    sub = write_jsonl(os.path.join(gm_dir(tmp_path), "chats", GSID, "sub-1.jsonl"), [gm_header(kind="subagent")])
    old = gm_file(tmp_path, [gm_header()], name="session-old.jsonl")
    other = gm_file(tmp_path, [gm_header()], name="session-o.jsonl", slug="other", root="/work/other")
    hashed_dir = os.path.join(home, ".gemini", "tmp", __import__("hashlib").sha256(b"/work/legacy").hexdigest())
    legacy = write_jsonl(os.path.join(hashed_dir, "chats", "session-l.jsonl"), [gm_header()])
    for i, p in enumerate([keep, sub, other, legacy]):
        set_age(p, i + 1)
    set_age(old, 45)
    for i in range(300):   # many quiet old files are only stat-ed, never opened
        set_age(write_jsonl(os.path.join(gm_dir(tmp_path), "chats", "session-bulk-%d.jsonl" % i), ["{bad json"]), 90)
    assert T.find_sessions(home=home) == [("gemini-cli", p) for p in (keep, sub, other, legacy)]
    assert T.find_sessions(home=home, include_subagents=False) == [("gemini-cli", p) for p in (keep, other, legacy)]
    assert T.find_sessions(home=home, project="/work/app") == [("gemini-cli", keep), ("gemini-cli", sub)]
    assert T.find_sessions(home=home, project="/work/legacy") == [("gemini-cli", legacy)]


def test_gemini_working_folder_for_legacy_hash_named_folders_comes_from_the_registry(tmp_path):
    import hashlib
    gemini = os.path.join(str(tmp_path), ".gemini")
    hashed = os.path.join(gemini, "tmp", hashlib.sha256(b"/work/old-app").hexdigest())
    path = write_jsonl(os.path.join(hashed, "chats", "session-x.jsonl"), [gm_header()])
    with open(os.path.join(gemini, "projects.json"), "w") as fh:
        json.dump({"projects": {"/work/old-app": "old-app", "/work/app": "app"}}, fh)
    assert T.load_session("gemini-cli", path).cwd == "/work/old-app"


# ---------------------------------------------------------------------------
# OpenCode (facts Q1.5): SQLite store, built here from the documented schema
# ---------------------------------------------------------------------------

import sqlite3  # noqa: E402

T0 = 1759000000000   # 2025-09-27T19:06:40Z, epoch milliseconds


def oc_db(home, sessions, messages, parts, name="opencode.db"):
    folder = os.path.join(str(home), ".local", "share", "opencode")
    os.makedirs(folder, exist_ok=True)
    path = os.path.join(folder, name)
    con = sqlite3.connect(path)
    con.executescript("""
        CREATE TABLE session (id TEXT PRIMARY KEY, project_id TEXT, parent_id TEXT, slug TEXT, title TEXT,
            version TEXT, cost REAL, tokens_input INTEGER, tokens_output INTEGER, tokens_reasoning INTEGER,
            tokens_cache_read INTEGER, tokens_cache_write INTEGER, model TEXT, agent TEXT, permission TEXT,
            time_created INTEGER, time_updated INTEGER);
        CREATE TABLE message (id TEXT PRIMARY KEY, session_id TEXT, data TEXT);
        CREATE TABLE part (id TEXT PRIMARY KEY, message_id TEXT, session_id TEXT, data TEXT);
    """)
    for sid, parent, updated in sessions:
        con.execute("INSERT INTO session VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (sid, "prj_1", parent, "slug", "title", "1.2.0", 0.0, 0, 0, 0, 0, 0,
                     json.dumps({"id": "claude-sonnet-4-5", "providerID": "anthropic"}), "build", "{}",
                     T0, updated))
    for mid, sid, data in messages:
        con.execute("INSERT INTO message VALUES (?,?,?)", (mid, sid, json.dumps(data)))
    for pid, mid, sid, data in parts:
        con.execute("INSERT INTO part VALUES (?,?,?,?)", (pid, mid, sid, json.dumps(data)))
    con.commit()
    con.close()
    return path


def oc_assistant(t, tokens=None, error=None, cwd="/work/app", model="claude-sonnet-4-5"):
    data = {"role": "assistant", "modelID": model, "providerID": "anthropic", "agent": "build", "mode": "build",
            "parentID": "msg_u", "path": {"cwd": cwd, "root": cwd}, "time": {"created": t, "completed": t + 500},
            "cost": 0.01, "tokens": tokens or {"input": 0, "output": 0, "reasoning": 0, "cache": {"read": 0, "write": 0}}}
    if error:
        data["error"] = error
    return data


def oc_tokens(inp, out, reasoning=0, read=0, write=0):
    return {"input": inp, "output": out, "reasoning": reasoning, "cache": {"read": read, "write": write}}


def oc_tool(call_id, tool, status, inp, output=None, error=None, metadata=None, t=T0):
    state = {"status": status, "input": inp, "time": {"start": t, "end": t + 100}}
    if status == "completed":
        state.update(output=output or "", title=tool, metadata=metadata or {})
    if status == "error":
        state["error"] = error
    return {"type": "tool", "callID": call_id, "tool": tool, "state": state}


def _oc_fixture(home):
    main, sub = "ses_01main", "ses_02sub"
    messages = [
        ("msg_01", main, {"role": "user", "time": {"created": T0}, "agent": "build"}),
        ("msg_02", main, oc_assistant(T0 + 1000, tokens=oc_tokens(15, 50, 10, 2100, 200))),
        ("msg_03", main, oc_assistant(T0 + 5000, error={"name": "MessageAbortedError",
                                                        "data": {"message": "The operation was aborted."}})),
        ("msg_04", main, {"role": "user", "time": {"created": T0 + 6000}}),
        ("msg_05", main, oc_assistant(T0 + 7000, tokens=oc_tokens(3, 4))),
        ("msg_06", sub, oc_assistant(T0 + 2000, tokens=oc_tokens(7, 8))),
    ]
    parts = [
        ("prt_01", "msg_01", main, {"type": "text", "text": "fix the tests"}),
        ("prt_02", "msg_01", main, {"type": "text", "text": "<file>a.ts contents</file>", "synthetic": True}),
        ("prt_03", "msg_02", main, {"type": "step-start"}),
        ("prt_04", "msg_02", main, {"type": "text", "text": "Running the tests."}),
        ("prt_05", "msg_02", main, oc_tool("call_1", "bash", "completed", {"command": "npm test", "description": "t"},
                                          output="1 failing", metadata={"exit": 1, "description": "t"})),
        ("prt_06", "msg_02", main, {"type": "step-finish", "reason": "tool-calls", "cost": 0.004,
                                    "tokens": oc_tokens(10, 20, 0, 1000, 200)}),
        ("prt_07", "msg_02", main, {"type": "step-start"}),
        ("prt_08", "msg_02", main, oc_tool("call_2", "edit", "error", {"filePath": "/work/app/a.ts"},
                                          error="The user rejected permission to use this specific tool call.")),
        ("prt_09", "msg_02", main, oc_tool("call_3", "read", "completed", {"filePath": "/work/app/b.ts"}, output="x")),
        ("prt_10", "msg_02", main, {"type": "step-finish", "reason": "stop", "cost": 0.002,
                                    "tokens": oc_tokens(5, 30, 10, 1100, 0)}),
        ("prt_11", "msg_04", main, {"type": "compaction", "auto": True}),
        ("prt_12", "msg_05", main, {"type": "retry", "attempt": 1, "error": {"name": "APIError",
                                                                            "data": {"message": "Overloaded"}}}),
        ("prt_13", "msg_05", main, {"type": "text", "text": "Summary written."}),
        ("prt_14", "msg_06", sub, {"type": "text", "text": "sub answer"}),
    ]
    db = oc_db(home, [(main, None, T0 + 8000), (sub, main, T0 + 3000)], messages, parts)
    return db, main, sub


def test_opencode_session_from_the_documented_schema(tmp_path):
    db, main, _sub = _oc_fixture(tmp_path)
    s = T.load_session("opencode", db + "#" + main)
    assert kinds(s) == ["user", "user", "assistant", "tool", "assistant", "tool", "tool", "interrupt",
                        "compaction", "api_error", "assistant"]
    assert [e.text for e in s.user_prompts()] == ["fix the tests"]
    usages = [e.usage for e in s.events if e.usage]
    assert usages == [T.Usage(input=10, cache_read=1000, cache_write=200, output=20),
                      T.Usage(input=5, cache_read=1100, output=30, reasoning=10),
                      T.Usage(input=3, output=4)]
    assert s.events[2].text == "Running the tests."
    bash, edit, read = s.tool_calls()
    assert (bash.kind, bash.command, bash.exit_code, bash.is_error, bash.output) == ("shell", "npm test", 1, True,
                                                                                   "1 failing")
    assert (edit.kind, edit.paths, edit.is_error, edit.denied) == ("edit", ["/work/app/a.ts"], True, "user-rejected")
    assert (read.kind, read.paths, read.has_result) == ("read", ["/work/app/b.ts"], True)
    assert s.events[9].text == "Overloaded"
    assert (s.id, s.cwd, s.version, s.models, s.is_subagent) == (main, "/work/app", "1.2.0",
                                                                  ["claude-sonnet-4-5"], False)
    assert s.started == "2025-09-27T19:06:40.000Z"


def test_opencode_subagent_session(tmp_path):
    db, main, sub = _oc_fixture(tmp_path)
    s = T.load_session("opencode", db + "#" + sub)
    assert (s.is_subagent, s.parent_id, [e.text for e in s.events]) == (True, main, ["sub answer"])
    assert s.usage_total() == T.Usage(input=7, output=8)


def test_opencode_find_sessions(tmp_path, monkeypatch):
    db, main, sub = _oc_fixture(tmp_path)
    home = str(tmp_path)
    assert T.find_sessions(home=home, since_days=None) == [("opencode", db + "#" + main), ("opencode", db + "#" + sub)]
    assert T.find_sessions(home=home, since_days=None, include_subagents=False) == [("opencode", db + "#" + main)]
    assert T.find_sessions(home=home, since_days=None, project="/work/elsewhere") == []
    assert T.find_sessions(home=home, since_days=None, project="/work") == [("opencode", db + "#" + main),
                                                                            ("opencode", db + "#" + sub)]
    assert T.find_sessions(home=home) == []            # the sessions were last updated in 2025
    assert T.detect_harnesses(home=home) == {"opencode": os.path.dirname(db)}
    monkeypatch.setenv("OPENCODE_DB", db)
    assert T.find_sessions(harness="opencode", since_days=None, include_subagents=False) == [("opencode", db + "#" + main)]


def test_opencode_database_is_opened_read_only(tmp_path):
    db, main, _sub = _oc_fixture(tmp_path)
    os.chmod(db, 0o444)
    before = os.path.getmtime(db)
    try:
        s = T.load_session("opencode", db + "#" + main)
    finally:
        os.chmod(db, 0o644)
    assert s.events and s.warnings == [] and os.path.getmtime(db) == before


def test_opencode_unreadable_database_gives_a_warning(tmp_path):
    bad = tmp_path / "opencode.db"
    bad.write_text("not a database")
    s = T.load_session("opencode", str(bad) + "#ses_x")
    assert s.events == [] and len(s.warnings) == 1


# ---------------------------------------------------------------------------
# Performance: the contract is 1,000 sessions of 500 records in under 20 s;
# this runs a tenth of that against a tenth of the budget.
# ---------------------------------------------------------------------------

def _big_session(home, sid, n=500):
    records = [cc_user("go", "2026-09-25T10:00:00.000Z", sessionId=sid)]
    for i in range((n - 1) // 3):
        ts = "2026-09-25T10:%02d:%02d.000Z" % (i // 60 % 60, i % 60)
        records.append(cc_assistant("m%d" % i, text("step %d" % i), ts, cc_usage(inp=3, read=5000, out=40)))
        records.append(cc_assistant("m%d" % i, tool_use("t%d" % i, "Read", {"file_path": "/w/f%d.py" % i}), ts,
                                    cc_usage(inp=3, read=5000, out=80)))
        records.append(cc_result("t%d" % i, "line\n" * 40, ts, tool_use_result={"type": "text"}))
    return cc_file(home, records[:n], sid=sid)


def test_loading_many_sessions_is_fast(tmp_path):
    for i in range(100):
        _big_session(tmp_path, "perf-%03d" % i)
    start = time.perf_counter()
    sessions = list(T.iter_sessions(home=str(tmp_path), harness="claude-code"))
    elapsed = time.perf_counter() - start
    assert len(sessions) == 100 and all(len(s.tool_calls()) == 166 for s in sessions)
    assert elapsed < 2.0, elapsed


def test_claude_workflow_agents_in_nested_folders_are_subagent_sessions(tmp_path):
    main = _cc_minimal(tmp_path, SID, age_days=1)
    nested = write_jsonl(os.path.join(cc_project(tmp_path), SID, "subagents", "workflows", "wf_1a2b-3c", "agent-a9f.jsonl"),
                         [cc_assistant("m1", text("wf step"), "2026-09-25T10:00:01.000Z", isSidechain=True)])
    set_age(nested, 2)
    write_jsonl(os.path.join(cc_project(tmp_path), SID, "subagents", "workflows", "wf_1a2b-3c", "agent-a9f.meta.json"),
                [{}])
    write_jsonl(os.path.join(cc_project(tmp_path), SID, "subagents", "workflows", "wf_1a2b-3c", "journal.jsonl"),
                [{"type": "launched"}, {"type": "started", "agentId": "a9f", "key": "k"},
                 {"type": "result", "agentId": "a9f", "key": "k", "result": {}}])   # a run log, not a transcript
    assert T.find_sessions(home=str(tmp_path)) == [("claude-code", main), ("claude-code", nested)]
    assert T.find_sessions(home=str(tmp_path), include_subagents=False) == [("claude-code", main)]
    s = T.load_session("claude-code", nested)
    assert (s.is_subagent, s.parent_id, s.id) == (True, SID, "a9f")


def test_codex_command_output_that_quotes_a_denial_marker_is_not_a_denial(tmp_path):
    quoted = "line 1\n" * 40 + "denials show up as: exec command rejected by user\n"
    records = [cx_meta("2026-09-25T10:00:00.000Z"),
               cx_exec("2026-09-25T10:00:01.000Z", "call_1", "text(await tools.exec_command({cmd: 'cat notes.md'}))"),
               cx_item("2026-09-25T10:00:02.000Z", cx_command("i1", "cat notes.md", output=quoted)),
               cx_custom_out("2026-09-25T10:00:03.000Z", "call_1", "Script completed\nWall time 1 seconds\nOutput:\n" + quoted)]
    [call] = T.load_session("codex", cx_file(tmp_path, records)).tool_calls()
    assert (call.denied, call.is_error) == ("", False)


def test_opencode_finder_skips_one_bad_row_not_the_whole_database(tmp_path):
    db, main, sub = _oc_fixture(tmp_path)
    con = sqlite3.connect(db)
    con.execute("INSERT INTO message VALUES (?,?,?)", ("msg_00", sub, '{"role": "assistant", "path": {"cwd": "/w'))
    con.commit()
    con.close()
    assert T.find_sessions(home=str(tmp_path), since_days=None, project="/work/app") == [
        ("opencode", db + "#" + main), ("opencode", db + "#" + sub)]


# ---------------------------------------------------------------------------
# Event ids: a forked or resumed Claude Code session can start with a copy of
# the earlier session's records, so totals across sessions count each id once.
# ---------------------------------------------------------------------------

def test_event_ids_are_the_harness_ids_and_survive_a_copied_history(tmp_path):
    shared = [cc_user("start", "2026-09-25T10:00:00.000Z", uuid="u-first"),
              cc_assistant("msg_A", tool_use("toolu_1", "Read", {"file_path": "/w/a"}), "2026-09-25T10:00:01.000Z",
                           cc_usage(inp=5, out=7))]
    original = T.load_session("claude-code", cc_file(tmp_path, shared, sid="s-old"))
    fork = T.load_session("claude-code", cc_file(tmp_path, shared + [
        cc_user("continue", "2026-09-25T11:00:00.000Z", uuid="u-new"),
        cc_assistant("msg_B", text("ok"), "2026-09-25T11:00:01.000Z", cc_usage(inp=1, out=1))], sid="s-fork"))
    assert [(e.kind, e.id) for e in original.events] == [("user", "u-first"), ("assistant", "msg_A"),
                                                         ("tool", "toolu_1")]
    seen, total = set(), T.Usage()
    for s in (original, fork):
        for e in s.events:
            if e.usage is not None and e.id not in seen:
                seen.add(e.id)
                total = total + e.usage
    assert total == T.Usage(input=6, output=8)


def test_event_ids_for_codex_gemini_and_opencode(tmp_path):
    u = cx_tokens(10, 0, 1, 0)
    cx_s = T.load_session("codex", cx_file(tmp_path, [cx_meta("2026-09-25T10:00:00.000Z"),
                                                      cx_record("2026-09-25T10:00:01.000Z", "resp_9", u)]))
    assert [(e.kind, e.id) for e in cx_s.events] == [("assistant", "resp_9")]
    gm_s = T.load_session("gemini-cli", gm_file(tmp_path, [gm_header(), gm_model(
        "m7", "2026-09-25T10:00:01.000Z", "hi", tool_calls=[gm_call("c1", "glob", {}, output="x")])]))
    assert [(e.kind, e.id) for e in gm_s.events] == [("assistant", "m7"), ("tool", "c1")]
    db, main, _sub = _oc_fixture(tmp_path)
    oc_s = T.load_session("opencode", db + "#" + main)
    assert [e.id for e in oc_s.events if e.usage] == ["msg_02:1", "msg_02:2", "msg_05:1"]


def test_unique_events_counts_a_forked_copy_once_oldest_session_first():
    older = T.Session(harness="claude-code", id="a", path="a.jsonl", started="2026-09-01T00:00:00Z",
                      events=[T.Event(kind="assistant", id="msg_1"), T.Event(kind="tool", id="toolu_1"),
                              T.Event(kind="user")])
    fork = T.Session(harness="claude-code", id="b", path="b.jsonl", started="2026-09-02T00:00:00Z",
                     events=[T.Event(kind="assistant", id="msg_1"), T.Event(kind="tool", id="toolu_1"),
                             T.Event(kind="user"), T.Event(kind="tool", id="msg_1"),
                             T.Event(kind="assistant", id="msg_2")])
    got = [(s.id, e.kind, e.id) for s, e in T.unique_events([fork, older])]  # given newest first
    assert got == [("a", "assistant", "msg_1"), ("a", "tool", "toolu_1"), ("a", "user", ""),
                   ("b", "user", ""), ("b", "tool", "msg_1"), ("b", "assistant", "msg_2")]


def test_unique_events_since_drops_old_records_in_a_recent_file():
    s = T.Session(harness="claude-code", id="a", path="a.jsonl", started="2026-06-01T00:00:00.000Z",
                  events=[T.Event(kind="assistant", id="old", ts="2026-06-01T00:00:00.000Z"),
                          T.Event(kind="assistant", id="new", ts="2026-09-20T10:00:00.123Z"),
                          T.Event(kind="user")])
    got = [e.id or e.kind for _, e in T.unique_events([s], since="2026-09-01T00:00:00")]
    assert got == ["new", "user"]
    assert [e.id for _, e in T.unique_events([s])][:2] == ["old", "new"]


def test_cutoff_is_a_utc_timestamp_prefix():
    import datetime
    c = T.cutoff(30)
    assert len(c) == 19 and c[10] == "T"
    parsed = datetime.datetime.strptime(c, "%Y-%m-%dT%H:%M:%S")
    age = datetime.datetime.now(datetime.timezone.utc).replace(tzinfo=None) - parsed
    assert datetime.timedelta(days=29, hours=23) < age < datetime.timedelta(days=30, minutes=1)


def test_claude_code_monitor_tool_is_a_shell_call():
    assert T._cc_kind("Monitor") == "shell" and T._cc_kind("Bash") == "shell"


def test_find_sessions_accepts_a_project_path_that_is_not_utf8(tmp_path):
    odd = str(tmp_path) + "/caf\udce9"  # a path byte that is not UTF-8, as os.fsdecode gives it
    assert T.find_sessions(harness="gemini-cli", project=odd, home=str(tmp_path)) == []


def test_claude_code_tool_events_carry_the_permission_mode(tmp_path):
    lines = [
        {"type": "user", "uuid": "u1", "timestamp": "2026-09-01T00:00:00.000Z", "sessionId": "s", "cwd": "/p",
         "permissionMode": "plan", "message": {"role": "user", "content": "hi"}},
        {"type": "assistant", "uuid": "a1", "timestamp": "2026-09-01T00:00:01.000Z", "sessionId": "s",
         "message": {"id": "m1", "model": "claude-opus-5-5", "role": "assistant",
                     "content": [{"type": "tool_use", "id": "t1", "name": "Bash", "input": {"command": "ls"}}],
                     "usage": {"input_tokens": 1, "output_tokens": 1}}},
        {"type": "user", "uuid": "u2", "timestamp": "2026-09-01T00:00:02.000Z", "sessionId": "s",
         "permissionMode": "bypassPermissions", "message": {"role": "user", "content": "go"}},
        {"type": "assistant", "uuid": "a2", "timestamp": "2026-09-01T00:00:03.000Z", "sessionId": "s",
         "message": {"id": "m2", "model": "claude-opus-5-5", "role": "assistant",
                     "content": [{"type": "tool_use", "id": "t2", "name": "Bash", "input": {"command": "pwd"}}],
                     "usage": {"input_tokens": 1, "output_tokens": 1}}},
    ]
    f = tmp_path / "s.jsonl"
    f.write_text("\n".join(json.dumps(x) for x in lines) + "\n")
    s = T.load_session("claude-code", str(f))
    assert [e.mode for e in s.events if e.kind == "tool"] == ["plan", "bypassPermissions"]
