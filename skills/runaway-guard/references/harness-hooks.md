# Hook support per harness

What each coding agent's hooks allow, and what runaway guard does with it. Checked 2026-09-28 against each harness's documentation (linked per section) and, where noted, against real session files on one Mac.

## Claude Code: supported

- **Where the hook goes**: `~/.claude/settings.json` (user) or `.claude/settings.local.json` (project, not shared with the team). `$CLAUDE_CONFIG_DIR` moves the user folder. Source: https://code.claude.com/docs/en/hooks
- **Shape**: `{"hooks": {"PreToolUse": [{"hooks": [{"type": "command", "command": "...", "timeout": 10}]}]}}`. With no `matcher`, the hook runs for every tool. Hooks from all settings files run; the guard's entry sits beside the ones already there. The command is `python3 '<skill folder>/scripts/guard.py' || true`: without `|| true`, a moved or deleted skill folder would make `python3` exit 2, and exit 2 blocks the call.
- **Input the guard reads**: `session_id`, `transcript_path`, `cwd`, `prompt_id` (v2.1.196 and later), `tool_name`, `tool_input`, `tool_use_id`, and `agent_id` for calls made inside a subagent.
- **Answers the guard gives**: `permissionDecision: "deny"` (the reason is shown to Claude), `permissionDecision: "ask"` (the reason is shown to the user), and `systemMessage` (a message shown to the user). At the spend cap the block also carries `continue: false` and a `stopReason`, which end the turn. Exit code 2 would also block; the guard never uses it.
- **Hook changes**: Claude Code normally applies changes to hook settings in sessions that are already running.
- **Timeouts**: 600 seconds by default; the guard's entry sets 10. A PreToolUse hook that times out does not block the call.
- **Transcripts**: `~/.claude/projects/<project>/<session>.jsonl`, with subagents in `<session>/subagents/agent-<id>.jsonl`. One API response is written as several records that share a message id; the reader keeps the last one's usage. Source: https://code.claude.com/docs/en/sessions
- **Built-in caps**: `--max-budget-usd` and `--max-turns` work in print mode (`claude -p`) only. Source: https://code.claude.com/docs/en/cli-reference
- **Tested**: unit tests with transcripts built from the documented record shapes, and a replay of real local sessions through the hook (see [how-it-decides.md](how-it-decides.md)).

## Codex: supported, with two gaps

- **Where the hook goes**: `~/.codex/hooks.json` (user; `$CODEX_HOME` moves it) or `<repo>/.codex/hooks.json` (project). The shape matches Claude Code's. Source: https://learn.chatgpt.com/docs/hooks.md
- **Trust**: Codex runs a new or changed hook only after the user trusts it in `/hooks`. Project hooks run only in trusted projects.
- **Input the guard reads**: `session_id` (subagent hooks get the parent's id), `transcript_path` (may be null; then only the loop wire works), `turn_id`, `model`, `tool_name` (shell commands arrive as `Bash`, patches as `apply_patch`), `tool_input`, `tool_use_id`.
- **Answers**: `permissionDecision: "deny"` blocks. On PreToolUse, `ask`, `continue`, `stopReason`, and `suppressOutput` mark the hook as failed and the call proceeds, so the guard sends none of them to Codex.
  - Gap 1: no question. After a run of failed calls the guard blocks the next call and tells the agent to ask the user.
  - Gap 2: no warning. The early spend warning shows only in `status.py`.
- **Not seen by hooks**: hosted tools such as web search.
- **Transcripts**: `$CODEX_HOME/sessions/YYYY/MM/DD/rollout-*.jsonl`. Each subagent writes its own rollout, and its first line (`session_meta`) carries the root session id. The model comes from `turn_context` lines; the guard carries it from one read to the next. Source: https://github.com/openai/codex/blob/main/codex-rs/protocol/src/protocol.rs
- **Tested**: unit tests from the documented shapes, and a replay of one real Codex session with 36 subagent rollouts, which the guard found and priced within 0.6% of a full read. The hook input and answers follow the Codex documentation; they were not tested in a live Codex session.

## Gemini CLI: not supported

- Gemini CLI has `BeforeTool` hooks that can deny, with timeouts in milliseconds. Source: https://github.com/google-gemini/gemini-cli/blob/main/docs/hooks/reference.md
- It already detects loops, on by default (`model.disableLoopDetection` turns it off).
- A spend cap needs two things Gemini CLI does not give this guard: its chat files are an operation log that checkpoints and rewinds rewrite, which the shared reader does not read incrementally, and no Gemini price was confirmed on Google's official pricing page.

## Cursor: not supported

- Cursor's `preToolUse` hooks can deny; an `ask` answer is not enforced there. Source: https://cursor.com/docs/hooks.md
- Its transcripts hold no tool results, token usage, or timestamps, so neither the failure wire nor the spend wire could work.
- Cursor also runs the hooks in Claude Code settings files, a setting that is on by default, with its own tool names (`Shell`, not `Bash`). Source: https://cursor.com/docs/reference/third-party-hooks.md. The guard recognizes Cursor's input (it carries `cursor_version`) and answers `{}`, which makes no decision: Cursor reads invalid JSON from a permission hook as a block, and empty output may count as invalid. This has not been tested in Cursor.

## OpenCode: not supported

- OpenCode has no shell-command hooks; extensions are JavaScript plugins (`tool.execute.before`). Source: https://opencode.ai/docs/plugins
- It already ships the `doom_loop` permission, which asks when the same tool call repeats 3 times with identical input. Source: https://opencode.ai/docs/permissions
