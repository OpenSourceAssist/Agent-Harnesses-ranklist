# Harness commands

How `scripts/harnesses.py` runs each coding agent without a person at the keyboard (headless), and
how it reads the result. Checked 2026-09-28 against each harness's docs and source, linked below.
Each command uses the most restrictive settings that still let the agent edit files in its copy of
the repository and run the test command. The prompt is the same for every harness (see
`method.md`). Every harness runs with the user's environment minus the variables that lead back to
the agent session that started the script.

## Claude Code

```
claude -p "<prompt>" --output-format stream-json --verbose --permission-mode acceptEdits
  --allowedTools "Bash(<test command>),Bash(<test command> *)" --no-session-persistence
  --strict-mcp-config --max-budget-usd <budget left> [--model <id>]
```

- `-p` runs one prompt and exits. It cannot answer permission prompts, so any call not allowed up
  front is denied.
- `--permission-mode acceptEdits` approves file edits in the working folder. `--allowedTools`
  approves the test command, with and without extra arguments; a compound test command such as
  `npm ci && npm test` gets one rule per part, because a rule must match every part. The user's own
  allow rules from their settings still apply on top.
- `--strict-mcp-config` with no `--mcp-config` loads no MCP server, so the agent works with its
  built-in tools only.
- `--max-budget-usd` stops the run when its spend reaches the budget left under `--max-usd`
  (subagent spend counts). The script rounds the budget down to the cent.
- `--no-session-persistence` keeps these runs out of the user's session history.
- `stream-json` with `--verbose` prints one JSON object per line as the run goes. The last line is
  the result: `total_cost_usd`, `num_turns`, `modelUsage` (tokens and cost per model, subagents
  included), `permission_denials`, and `is_error`. When `is_error` is true, the error shown is the
  result's own message (`result`, else the first of `errors`, else `terminal_reason`), never the
  `subtype`, which reads `success` on a failed login. When a run is stopped before the result, the
  cost is rebuilt from the `usage` of each assistant message (the last copy of each message id,
  leaving out `<synthetic>` placeholder replies), priced with `pricing.py`. Any assistant message
  with usage counts as model work.
- Not used: `--dangerously-skip-permissions` and `--permission-mode bypassPermissions`.

Sources: https://code.claude.com/docs/en/headless , https://code.claude.com/docs/en/cli-reference ,
https://code.claude.com/docs/en/permissions

## Codex

```
codex exec --json --sandbox workspace-write --ephemeral [--model <id>] "<prompt>"
```

- `codex exec` runs one prompt without a person. It needs a git repository, which each fresh copy
  is.
- `--sandbox workspace-write` lets commands write inside the working folder and blocks the network
  unless the user's Codex config allows it; `.git` stays read-only. A test command that needs the
  network, or writes outside the folder (for example a package manager's cache), fails inside the
  sandbox, so Codex cannot check its own work with it. The score does not depend on that, because
  the script runs the tests itself, outside any sandbox. Still, prefer a setup that installs into
  the copy and a test command that runs offline. The script counts Codex's own runs of the test
  command (`command_execution` items whose command contains it) and how many failed, and the report
  notes the failures.
- `--ephemeral` writes no session file, so these runs stay out of the user's history.
- `--json` prints events as JSON lines: `thread.started`, `turn.completed` with `usage`
  (`input_tokens`, `cached_input_tokens`, `cache_write_input_tokens`, `output_tokens`,
  `reasoning_output_tokens`), `turn.failed`, `error`, and `item.completed` items such as a
  `command_execution` whose status is `declined`. The error shown is the `error` or `turn.failed`
  message. Any `item.completed` counts as model work.
- Cost: Codex reports neither a cost nor its model. The script takes the model from `--model` or
  the top-level `model` key in `$CODEX_HOME/config.toml` (`~/.codex/config.toml` by default), and
  prices the tokens with `pricing.py`. Cached input is part of `input_tokens` and reasoning is part
  of `output_tokens`, so the script subtracts the first and never adds the second again. With no
  known model Codex never runs, even with `--allow-unpriced`: pin one with `--model codex=<id>`.
- Codex has no per-run spend or turn limit, so the run's timeout is its only limit.
- Codex skips the project's AGENTS.md in folders it does not trust. Whether `codex exec` trusts a
  new temporary folder is not documented.
- Not used: `--dangerously-bypass-approvals-and-sandbox`, `--sandbox danger-full-access`, and the
  deprecated `--full-auto`.

Sources: https://learn.chatgpt.com/docs/non-interactive-mode.md ,
https://github.com/openai/codex/blob/main/codex-rs/exec/src/exec_events.rs ,
https://github.com/openai/codex/blob/main/codex-rs/exec/src/cli.rs ,
https://learn.chatgpt.com/docs/agent-approvals-security.md ,
https://learn.chatgpt.com/docs/agent-configuration/agents-md.md

## Gemini CLI

```
gemini --output-format json --approval-mode auto_edit --skip-trust
  --allowed-tools "run_shell_command(<test command>)" -p "<prompt>"
```

- `--approval-mode auto_edit` approves file edits. `--allowed-tools` approves shell commands that
  start with the test command, one entry per part of a compound command; the user's own policy
  rules still apply. In headless mode any other call that would ask the user is denied. The docs
  mark `--allowed-tools` deprecated in favor of policy files, which cannot be passed on the command
  line.
- `--skip-trust`: a new folder is untrusted, and Gemini CLI turns off automatic approval in
  untrusted folders, so without it the agent could not edit. With it, the copy's own
  `.gemini/settings.json` and MCP servers load too.
- `--output-format json` prints one object: `response`, `stats`, and `error` when the run failed.
  Tokens come from `stats.models.<model>.tokens`: `prompt` includes `cached`, and `thoughts` is
  not part of `candidates`, so the script counts input as `prompt` minus `cached` and output as
  `candidates` plus `thoughts`. Turns are the sum of `api.totalRequests`; denials are
  `stats.tools.totalDecisions.reject`. The error shown is `error.message`.
- Cost: Gemini CLI reports no cost, and `pricing.py` has no Gemini prices, because none were
  confirmed on Google's official pricing page. Its runs need `--allow-unpriced gemini-cli` and
  count $0 toward the cap. With a Google account's free tier that is the real cost; with a paid API
  key it is not.
- The headless docs list no model flag, so `--model` is refused for Gemini CLI. Set the model in
  its settings.
- Not used: `--approval-mode yolo` and `--yolo`.

Sources: https://github.com/google-gemini/gemini-cli/blob/main/docs/cli/headless.md ,
https://github.com/google-gemini/gemini-cli/blob/main/docs/cli/cli-reference.md ,
https://github.com/google-gemini/gemini-cli/blob/main/docs/cli/trusted-folders.md ,
https://github.com/google-gemini/gemini-cli/blob/main/docs/reference/configuration.md

## Errors that stop a harness

A run that did not time out, exited with an error, and changed nothing could not run. When its
message shows an error that will repeat on every task, the harness's other runs are skipped and the
fix is printed: a login problem ("authenticate", "OAuth", "not logged in", "invalid API key") gives
"sign in: run `<program>` once"; "requires a newer version" gives "upgrade `<harness>`"; a missing
program gives "install `<harness>`, or put `<program>` on PATH".

## Left out

- OpenCode: `opencode run --format json` exists, but its JSON event format is not documented, so
  its output cannot be parsed with confidence. Source: https://opencode.ai/docs/cli
- Cursor CLI (`agent -p`): its JSON result documents no token or cost fields, so its spend cannot
  be counted. Source: https://cursor.com/docs/cli/reference/output-format.md

## Prices

`scripts/pricing.py` holds the token prices, copied from the official pages on 2026-09-28:
https://platform.claude.com/docs/en/about-claude/pricing.md and
https://developers.openai.com/api/docs/pricing.md . Batch discounts, fast mode, and long-context
rates are not modeled.
