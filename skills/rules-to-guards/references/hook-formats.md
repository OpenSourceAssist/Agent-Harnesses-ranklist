# Hook formats per harness

What `rules.py generate` writes for each harness, and why. Checked 2026-09-28 against each harness's documentation (links in each section).

## The hook's contract

`rules_guard.py` is one Python file, standard library only, with your rules embedded. Every harness runs it the same way:

- It reads one pre-tool event as JSON on stdin.
- To block, it exits with code 2 and writes the reason to stderr. Exit 2 blocks in Claude Code, Codex, Gemini CLI, and Cursor alike.
- To allow, it prints `{}` and exits 0. The empty JSON object matters for Cursor, which treats invalid JSON from a permission hook as a block, and for Gemini CLI, which expects JSON on stdout.
- On any error of its own it prints `{}` and exits 0, so a bug in the hook lets the call through instead of stopping your work. It reads its arguments by hand for the same reason: a usage error must never exit 2. Each rule compiles on its own, so one broken pattern is skipped (and named on stderr) while the others still hold.
- Each settings entry runs `/absolute/path/rules_guard.py --harness <name>` with a 10-second timeout. The file starts with `#!/usr/bin/env python3` and is written with run permission, so the harness starts it directly, with the `python3` on its PATH. This form matters: `python3 /missing/file.py` exits with code 2, which would block every call, while a missing hook file exits 127, a file without run permission exits 126, and a missing `python3` exits 127. None of those codes blocks, with or without a shell.
- The absolute path works from any folder the harness starts in, but it exists only on this machine, so keep a settings file with this entry out of git, or have each teammate run `generate`.
- `generate` writes nothing through a symlink that leads outside the project (or, with `--scope user`, outside the harness folder) unless you pass `--follow-symlinks`, and it will not replace a file at the hook path that rules-to-guards did not write. A second `generate` keeps the rules the hook already holds, replacing any with the same id; `--replace` writes only the new rules and names the ones it drops.

## Claude Code

Source: https://code.claude.com/docs/en/hooks

- **Files.** Project: `.claude/settings.local.json`, the project settings file that is not shared, because the entry names an absolute path. User: `~/.claude/settings.json`, or `settings.json` under `$CLAUDE_CONFIG_DIR`. Hooks from every settings level merge, and the same handler defined twice runs once.
- **Entry.** Event `PreToolUse`: `{"matcher": "Bash|Monitor|Edit|MultiEdit|NotebookEdit|Write", "hooks": [{"type": "command", "command": "...", "timeout": 10}]}`. A matcher made only of letters, digits, and `|` is an exact list of tool names; `*` means every tool, which `generate` uses when a `forbid_tool` rule has to see every call.
- **Input.** `tool_name`, `tool_input` (Bash and Monitor: `command`, since Monitor also runs a shell command; Read, Edit, MultiEdit, Write: `file_path`, always absolute; NotebookEdit: `notebook_path`), and `cwd`. `CLAUDE_PROJECT_DIR` names the project folder, which a `./` glob starts from.
- **Blocking.** Exit 2 blocks the call and Claude reads stderr as the reason. A hook's exit 2 blocks before permission rules run, and a hook cannot loosen a deny rule. Exit 1 does not block. A timed-out PreToolUse hook does not block either.

## Codex

Source: https://learn.chatgpt.com/docs/hooks.md

- **Files.** Project: `<repo>/.codex/hooks.json`, loaded only in trusted projects. User: `~/.codex/hooks.json`, or `hooks.json` under `$CODEX_HOME`. Every source loads; higher layers do not replace lower ones.
- **Trust.** Codex runs a new or changed hook only after you review and trust it in `/hooks`.
- **Entry.** Event `PreToolUse`, same shape as Claude Code: `{"matcher": "^(Bash|apply_patch)$", "hooks": [{"type": "command", "command": "...", "timeout": 10}]}`. The matcher is a regular expression on the tool name.
- **Input.** Shell and unified exec calls arrive as `Bash` with `tool_input.command`. Patches arrive as `apply_patch` with the patch text in `tool_input.command`; the hook reads the file paths from its `*** Update File:`, `*** Add File:`, `*** Delete File:`, and `*** Move to:` lines. Codex has no separate read tool, so a rule about reading a file needs `shell` in its `tool` to catch `cat`.
- **Blocking.** Exit 2 with the reason on stderr. Codex does not support `ask` from a PreToolUse hook (it marks the hook failed and runs the call), so the hook only ever blocks.

## Gemini CLI

Source: https://github.com/google-gemini/gemini-cli/blob/main/docs/hooks/reference.md

- **Files.** Project: `.gemini/settings.json`. User: `~/.gemini/settings.json`. System: `/etc/gemini-cli/settings.json`. Project hooks are fingerprinted, so a new or changed name or command is untrusted until you accept it.
- **Entry.** Event `BeforeTool`: `{"matcher": "^(run_shell_command|replace|edit|write_file)$", "hooks": [{"type": "command", "command": "...", "name": "rules-to-guards", "timeout": 10000, "description": "..."}]}`. The timeout is in **milliseconds**, unlike Claude Code and Codex.
- **Input.** `tool_name` (`run_shell_command`, `read_file`, `read_many_files`, `write_file`, `replace`) and `tool_input`, the tool's arguments (`command`, `file_path`, `absolute_path`, or `paths`).
- **Blocking.** Exit 2 blocks, with stderr as the reason. On exit 0, stdout must be JSON.

## Cursor

Sources: https://cursor.com/docs/hooks.md and https://cursor.com/docs/reference/third-party-hooks.md

- **Files.** Project: `.cursor/hooks.json`, run only in trusted workspaces. User: `~/.cursor/hooks.json`. Enterprise and team hooks also run.
- **Entry.** Event `preToolUse`, each entry one flat object: `{"version": 1, "hooks": {"preToolUse": [{"command": "...", "type": "command", "timeout": 10, "matcher": "^(Shell|Write|Delete)$"}]}}`.
- **Input.** Tool names are `Shell`, `Read`, `Write`, `Grep`, `Delete`, `Task`, and `MCP:<tool>`. The docs do not list the keys inside `tool_input`, so the hook reads `command`, `file_path`, `path`, and the other common names. The first entry in `workspace_roots` is the project folder for `./` globs.
- **Shell.** Neither the facts this skill was built from nor the Cursor pages above say whether Cursor runs a hook command through a shell. The entry needs no shell when the hook's path has no spaces. With spaces in the path and no shell, the hook cannot start; that failure does not block, so the rules would simply not be enforced.
- **Blocking.** A permission hook returns `{"permission": "deny", "user_message": "...", "agent_message": "..."}`; exit 2 also blocks. The hook does both, so Cursor gets the reason either way. `ask` is not enforced on preToolUse.
- **Claude Code hooks.** By default Cursor also runs the hooks in `.claude/settings.local.json`, `.claude/settings.json`, and `~/.claude/settings.json`, mapping `PreToolUse` to `preToolUse`. Claude matchers such as `Bash` never fire there, but `Read` and `Write` do, so with both entries installed the hook can run twice for those calls. That is harmless.
- **Status.** Not verified on a real Cursor install.

## OpenCode

Sources: https://opencode.ai/docs/plugins and https://opencode.ai/docs/permissions

OpenCode has no shell-command hooks; it runs JavaScript or TypeScript plugins instead (`tool.execute.before`, which throws to block). `generate` does not write for OpenCode. `count` and `test` still read its sessions, on a best-effort basis. Its `permission` setting takes wildcard patterns such as `"bash": {"npm *": "deny"}`, and the last matching pattern wins.

## Permission rules: the partial layer

`generate` also prints permission rules for command rules that are a plain prefix, such as `^npm(?:\s|$)`. They are optional and partial: they match the start of a command, so they miss the same command inside `sh -c '...'` or called by its full path. The hook covers those forms.

- **Claude Code** `permissions.deny`: `Bash(npm *)` matches `npm` and `npm install`. Deny rules apply when any part of a compound command matches and look past wrappers such as `timeout` and `nohup`; the docs still say Bash rules are not a security boundary. Source: https://code.claude.com/docs/en/permissions
- **Codex** `.rules` files: `prefix_rule(pattern=["npm"], decision="forbidden")`. Scripts of plain words joined by `&&`, `||`, `;`, or `|` are split and each part is checked. Marked experimental; test with `codex execpolicy check`. Source: https://learn.chatgpt.com/docs/agent-configuration/rules.md
- **Gemini CLI** policy files in `~/.gemini/policies/`: a `[[rule]]` with `toolName = "run_shell_command"`, `commandPrefix`, and `decision = "deny"`. Workspace policy files do not work yet (issue #18186). Source: https://github.com/google-gemini/gemini-cli/blob/main/docs/reference/policy-engine.md
- **Cursor CLI** `permissions.deny` in `~/.cursor/cli-config.json`: `Shell(npm)`. Source: https://cursor.com/docs/cli/reference/permissions.md
