# How the tester reads each harness's rules

Checked 2026-09-28; the Claude Code permission modes, sandbox, and hook pages were checked again
2026-10-08. Every rule below comes from the official page named in its section. Where a page
is silent, the tester takes the reading that claims less protection, and this page marks it
**Assumption**. A run's results carry the label "simulated from the documented rules": the harness's
own parser can differ on commands its docs do not cover.

## Claude Code

Sources: [permissions](https://code.claude.com/docs/en/permissions),
[permission modes](https://code.claude.com/docs/en/permission-modes),
[hooks](https://code.claude.com/docs/en/hooks), [settings](https://code.claude.com/docs/en/settings),
[settings reference](https://code.claude.com/docs/en/settings-reference),
[sandboxing](https://code.claude.com/docs/en/sandboxing).

### Settings layers

Highest first: managed (`/Library/Application Support/ClaudeCode/managed-settings.json` on macOS,
`/etc/claude-code/` on Linux, plus `managed-settings.d/*.json` merged after it), local
(`.claude/settings.local.json` at the git repository root, plus an older copy in the project folder),
project (`.claude/settings.json`), and user (`settings.json` in `$CLAUDE_CONFIG_DIR` or the `.claude`
folder in your home folder).

- Rule lists merge across every layer. Single values come from the highest layer that sets them.
- `allowManagedPermissionRulesOnly` in managed settings drops every other layer's rules.
- `defaultMode`: the highest layer that sets it wins, with two exceptions. `auto` in project or
  local settings does not take effect, and the session uses the built-in default rather than a
  user `defaultMode`; `bypassPermissions` there starts Manual mode. `manual` means `default`.
  `disableBypassPermissionsMode` or `disableAutoMode` set to `"disable"` in any layer turns that
  mode off. `--mode` replaces the setting for a run; `--mode bypassPermissions` with bypass turned
  off simulates Manual mode and says so.
- With no `defaultMode`, a terminal session starts in auto mode on Claude Code 2.1.283 and later,
  or in Manual mode when a settings file sets `disableAutoMode`. The tester simulates auto mode,
  labels it "built-in default on 2.1.283+", and adds a Manual-mode row. `claude -p` and the Agent
  SDK can start in Manual mode instead.
- `useAutoModeDuringPlan` (user, local, or managed settings; on by default) lets the classifier
  review shell commands in plan mode.
- **Assumption**: the workspace trust prompt was accepted, so project allow rules apply. A
  `claude -p` run never uses them.

### Order of decisions

1. A PreToolUse hook that blocks: exit 2, a JSON `deny`, the older `decision: "block"`, or
   `continue: false`. A blocking hook stops the call before any rule.
2. Deny rules, from any layer.
3. Plan mode blocks Edit, Write, MultiEdit, and NotebookEdit until a plan is approved.
4. With `permissions.blockReadsOutsideWorkingDirectories` on (any layer), the file tools refuse
   paths outside the working folders in every mode.
5. Ask rules. In `dontAsk` mode every ask becomes a denial.
6. A hook that returns `ask`.
7. `rm` or `rmdir` of a critical path asks, even past an allow rule or a hook's allow.
8. With `blockReadsOutsideWorkingDirectories` on, a shell command that reads outside the working
   folders with a recognized file command, or that the parser cannot trace (a subshell, more than
   one `cd`), asks in every mode, auto and `bypassPermissions` included.
9. A hook that returns `allow` skips the prompt; deny and ask rules above still win.
10. Writes to protected paths ask in `default`, `acceptEdits`, and `plan` (the classifier reviews
    shell writes in plan mode when auto mode is available); are denied in `dontAsk`; go to the
    classifier in `auto`; and run in `bypassPermissions`. Allow rules do not approve them.
11. `bypassPermissions` runs everything that reached this point.
12. Allow rules, the read-only command set, in `acceptEdits` the file commands `mkdir`, `touch`,
    `rm`, `rmdir`, `mv`, `cp`, and `sed` on paths inside the project, and in `auto` file reads
    anywhere (after a one-time question for reads outside the working folders) and file edits
    inside the working folders.
13. With `sandbox.enabled` and `autoAllowBashIfSandboxed` left on, a shell command runs in the
    sandbox without a prompt, except in `plan` mode and for commands `excludedCommands` takes out.
    The "Sandbox" section below says what the sandbox then does with the command's needs.
14. The mode's fallback: ask in `default` and `acceptEdits`; deny in `dontAsk`; the classifier in
    `auto` (which the tester cannot run, so it reports "left to the classifier"); in `plan`, the
    classifier for shell commands when auto mode is available and `useAutoModeDuringPlan` is on,
    else ask. **Assumption**: plan mode with bypass permissions available is not simulated.

**Assumption**: a hook's `allow` on a protected-path write counts as approval; the docs do not say.

### Shell command matching

- In `Bash(...)`, `*` matches any text; a trailing ` *` that is the only wildcard also matches the
  bare command; a rule with no `*` matches exactly; a trailing `:*` equals a trailing ` *`.
- Commands split at `&&`, `||`, `;`, `|`, `|&`, `&`, and newlines. Deny and ask rules apply when any
  command matches, including commands inside `( )`, `$( )`, backticks, and loop bodies. An allow rule
  must match every command. A trailing `&&` or `||` makes the line unparseable, so no allow rule
  approves it; a line over 10,000 characters always asks.
- Stripped before matching: `timeout`, `time`, `nice`, `nohup`, `stdbuf`, `command`, `builtin`,
  `noglob`, and `xargs` with no flags. Not stripped: `command -v`, `nocorrect`, `env`, `sudo`,
  `npx`, `docker exec`, and `xargs` with flags.
- The exec wrappers `watch`, `setsid`, `ionice`, and `flock`, and `find` with `-exec` or
  `-delete`, are approved only by an allow rule that names the exact command; `Bash(watch *)` or
  `Bash(find *)` does not approve them.
- Deny and ask rules match past any leading `NAME=value`. Allow rules match past only the safe
  variables the docs name (`NODE_ENV`, `LANG`, `NO_COLOR`); Claude Code's full list is not
  published, so an allow rule may match in more cases than the tester shows.
- Words keep their quotes: `git 'push' origin main` does not match `Bash(git push *)`.
- **Assumption**: redirections are left out of the text a rule matches, and each target is checked
  on its own ("a rule such as Bash(git commit *) allows the command, not the target").
- `Tool(param:value)` rules work in deny and ask lists for the fields the hooks page lists per tool;
  a rule on the main field (`Bash(command:...)`) is ignored.

### File paths

- `Read(...)` and `Edit(...)` use gitignore patterns: `//` from the filesystem root, `~/` from the
  home folder, `/` from the settings file's own root (the project for project and local settings,
  the `.claude` folder for user settings), `./` from the project, and a bare name at any depth.
- In deny and ask rules a one-folder pattern such as `secrets/**` matches at any depth; in allow
  rules only at the top.
- A `!` pattern carves a path out of earlier rules from the same file, reads relative to the
  project, cannot reach `/`, `~/`, or `//` rules, and cannot reopen a file inside a blocked folder.
- A Read deny rule also blocks Edit and Write on that path. `Write(...)`, `NotebookEdit(...)`,
  `MultiEdit(...)`, and `Glob(...)` path rules are accepted but never used.
- Deny and ask rules apply when either the path or the file a symlink points at matches.
- In shell commands, Read rules cover `<` targets and Edit rules cover `>` and `tee` targets. The
  docs name `cat`, `head`, `tail`, `sed`, and `tee` as examples of file commands Claude Code
  recognizes and say the rules skip a command that names no file, such as `grep -r pattern .`.
  **Assumption**: the read-only commands `grep`, `wc`, `diff`, and `stat` count as file commands
  for the files they name. The tester adds a note when a deny rule names a file another command
  uses.
- Read-only set that runs without a prompt: `ls`, `cat`, `echo`, `pwd`, `head`, `tail`, `grep`,
  `find` (without `-exec` or `-delete`), `wc`, `which`, `diff`, `stat`, `du`, `cd` inside the
  project, and read-only `git`. **Assumption**: the tester's read-only git list is `status`, `diff`,
  `log`, `show`, `blame`, `rev-parse`, and similar reads; the docs give no list.
- Protected paths: the folders `.git`, `.config/git`, `.vscode`, `.idea`, `.husky`, `.cargo`,
  `.devcontainer`, `.yarn`, `.mvn`, and `.claude` (except `.claude/worktrees`), and shell startup,
  package-manager, and agent files such as `.zshrc`, `.bashrc`, `.profile`, `.gitconfig`,
  and `.mcp.json`.
- Critical paths for `rm`: the root, a top-level folder, the home folder, the project folder or a
  parent, a glob or trailing slash right after a variable (`"$DIR"/*`), a variable followed by a
  top-level name, and a recursive removal of a command substitution's output.

### Hooks

- Matchers: `*`, empty, or missing match every tool; letters, digits, `_`, `-`, spaces, `,`, and
  `|` form an exact name or list; anything else is an unanchored regular expression.
- The `if` field holds one permission rule: for shell commands it runs the hook when any command
  matches after leading assignments, and always when the command name comes from a variable.
- Exit 2 blocks; JSON decides on any other exit code; exit 1 and other codes without JSON are
  non-blocking errors, so the call goes ahead. JSON needs `hookEventName: "PreToolUse"` inside
  `hookSpecificOutput`. `defer` works only in `-p` runs.
- A timed-out hook decides nothing. The default timeout is 600 seconds; the tester waits at most
  `--hook-timeout` (10 by default) and reports a hook that hits it.
- `async` hooks cannot block; `http`, `prompt`, `agent`, and `mcp_tool` hooks are listed, not run.
- The same handler in two settings files runs once; a plugin's copy stays separate.
  **Assumption**: the same handler under two matchers in one file also runs once per call. The
  tester keeps every (matcher, handler) pair and runs each distinct handler once per call.
  `disableAllHooks` outside managed settings turns off every hook but managed ones;
  `allowManagedHooksOnly` keeps managed hooks only.
- The tester runs hooks only with `--run-hooks`, one at a time by default (`--hook-workers`). A
  hook that hits the tester's limit (`--hook-timeout`, 10 seconds) while Claude Code would wait
  longer makes that case "unknown"; after two timeouts the hook is not run again.
- Shell-form commands run through `sh -c` on macOS and Linux, or `bash -c` when the handler sets
  `"shell": "bash"`; exec form (`args`) runs without a shell, with `${CLAUDE_PROJECT_DIR}` and
  `${CLAUDE_PLUGIN_ROOT}` filled in. The tester skips `"shell": "powershell"` handlers.
- Plugin hooks: found through Claude Code's plugin list (`plugins/installed_plugins.json`) for
  plugins that `enabledPlugins` turns on. That file is internal, so this part is best effort.

### Sandbox

Source: [sandboxing](https://code.claude.com/docs/en/sandboxing). Each battery line lists what the
command needs to do its harm: `network`, `write-outside` (outside the project and the temp folder),
`write-git` (`.git/hooks` or `.git/config`), or `write-project`. For a command the sandbox's
auto-allow approves:

- **Network**: hosts start outside `network.allowedDomains`. Manual and `acceptEdits` modes ask
  ("asks first (needs network)", counted as asking only because of the mode); auto mode refuses
  the host unless the classifier approves the hosts the command lists; `dontAsk` refuses it.
  `strictAllowlist` (user or managed settings only) or `allowManagedDomainsOnly` refuses it in
  every mode ("stopped by the sandbox").
- **Writes** outside the working folders and the temp folder, and to `.git/hooks` or `.git/config`
  (protected even inside the project), fail in the sandbox unless `filesystem.disabled` (user or
  managed settings) turns filesystem isolation off. Claude may then retry outside the sandbox:
  that retry asks in Manual and `acceptEdits` modes, goes to the classifier in auto mode, is
  denied in `dontAsk`, and asks in every mode when an ask rule names
  `Bash(dangerouslyDisableSandbox:true)` or `blockReadsOutsideWorkingDirectories` is on. With
  `allowUnsandboxedCommands: false` there is no retry ("stopped by the sandbox").
- **Reads** that `filesystem.denyRead` covers (and no narrower `allowRead` reopens) follow the same
  retry rules. **Assumption**: only absolute and `~` paths in those lists are matched, and only for
  files a recognized file command or a `<` redirect names; `credentials` entries are not read.
- In-project deletes and secret reads inside the project run without asking.
- **Assumption**: a command an allow rule or the read-only set approves still runs in the sandbox,
  but the tester does not model what the sandbox then stops for it.

## Codex

Sources: [rules](https://learn.chatgpt.com/docs/agent-configuration/rules.md),
[approvals and security](https://learn.chatgpt.com/docs/agent-approvals-security.md),
[hooks](https://learn.chatgpt.com/docs/hooks.md).

- `prefix_rule()` calls in `rules/*.rules` next to each config layer (`~/.codex/rules/`, and the
  project's `.codex/rules/` only in trusted projects). The tester reads them with Python's parser
  and never runs the file; a rule built from variables or functions is skipped with a note.
- A rule matches when its pattern is a prefix of the command's argument list; a list inside the
  pattern means alternatives. The strictest match wins: `forbidden`, then `prompt`, then `allow`.
- A `bash -lc` script made only of plain words joined by `&&`, `||`, `;`, or `|` is split and each
  command checked. Redirection, substitution, variables, `NAME=value`, globs, or control flow keep
  the script whole as `["bash", "-lc", "<script>"]`, which prefix rules rarely match.
  **Assumption**: newline-separated scripts are not split.
- No matching rule: the command runs inside `sandbox_mode` without asking, unless it needs what the
  sandbox refuses. `workspace-write` refuses the network unless `[sandbox_workspace_write]
  network_access = true`, writes outside the project, and `.git` writes; `read-only` also refuses
  writes inside the project. Codex then asks to run the command outside the sandbox ("asks first
  (needs network)"), or, with `approval_policy = "never"`, the command fails ("stopped by the
  sandbox"). `danger-full-access` runs everything; a project with `trust_level = "untrusted"` asks
  first. **Assumption**: Codex keeps all of `.git` read-only, so it may also stop `git reset
  --hard` or `git branch -D`; the battery marks only `.git/hooks` and `.git/config` writes as
  `.git` writes.
- Edits through `apply_patch`: `workspace-write` edits inside the project run; `.git`, `.agents`,
  and `.codex` stay read-only, and writes outside the project ask (the sandbox).
- Hooks: `hooks.json` and `[hooks]` in `config.toml`, user layer always and project layer when
  trusted. Codex runs a hook only after you trust it in `/hooks`, which records
  `[hooks.state."<file>:pre_tool_use:<group>:<handler>"]` with `trusted_hash` in `config.toml`; the
  tester skips a hook without that record, or with `enabled = false`, and says so. Without Python
  3.11 it cannot read the record, says trust is unknown, and runs each hook as if trusted. Exit 2,
  a JSON `deny`, or `decision: "block"` blocks; `ask`, `continue`, and `stopReason` mark the hook
  failed and the call goes ahead. `[features] hooks = false` turns hooks off.
- `config.toml` needs Python 3.11 or newer (`tomllib`); on older Python it is skipped with a note.

## Gemini CLI

Sources: [policy engine](https://github.com/google-gemini/gemini-cli/blob/main/docs/reference/policy-engine.md),
[shell tool](https://github.com/google-gemini/gemini-cli/blob/main/docs/tools/shell.md),
[configuration](https://github.com/google-gemini/gemini-cli/blob/main/docs/reference/configuration.md),
[hooks reference](https://github.com/google-gemini/gemini-cli/blob/main/docs/hooks/reference.md),
[trusted folders](https://github.com/google-gemini/gemini-cli/blob/main/docs/cli/trusted-folders.md).

- `tools.exclude` blocks first; `tools.core` then allows only the tools and command prefixes it
  lists; `tools.confirmationRequired` always asks; `tools.allowed` skips the prompt. Prefixes in
  `run_shell_command(git)` match whole words.
- Policy files in `~/.gemini/policies/*.toml` (user tier) and the admin folder. The project's
  `.gemini/policies` folder does not work yet (issue #18186); the tester flags it. Final priority is
  the tier plus priority/1000, highest wins. The docs' tier table (user 4, admin 5) and worked
  examples (user 3.x, admin 4.x) disagree; the order is the same either way.
- `commandPrefix` is a plain string prefix; `commandRegex` and `argsPattern` test the arguments as
  sorted, compact JSON. An allow rule without `allowRedirection` asks when the command redirects.
- Chained commands split at `&&`, `||`, and `;`, as the shell tool page says, and the strictest
  part decides. **Assumption**: pipes are not split.
- With no matching rule, read tools run and write tools and the shell ask. `yolo` allows all but
  explicit denials; `autoEdit` allows edits; `plan` denies writes and the shell.
- Project settings apply only in a trusted folder (`~/.gemini/trustedFolders.json`).
- Hooks: `BeforeTool` in `settings.json`, matched against the tool name as a regular expression.
  Exit 2 or a JSON `deny` (alias `block`) blocks; other exit codes only warn. Timeouts are in
  milliseconds, and the tester flags values under 1000.

## OpenCode

Sources: [permissions](https://opencode.ai/docs/permissions), [config](https://opencode.ai/docs/config).
Not verified on a real install.

- `permission` in `opencode.json` or `opencode.jsonc`: global, then the `OPENCODE_CONFIG` file,
  then the project's `opencode.json` and `.opencode/` folder, merged by key.
- The last matching pattern wins, so a `"*"` catch-all listed after a deny cancels it; the tester
  flags that. `*` matches any text and `?` one character.
- Defaults: most tools run without asking; reading `.env` and `.env.*` is denied except
  `.env.example`; paths outside the project ask (`external_directory`).
- Shell rules match each parsed command. **Assumption**: the tester splits compound commands the way
  it does for Claude Code; OpenCode's own splitting is not documented in detail.
- Plugins in `.opencode/plugins/` are JavaScript or TypeScript code; they are listed, not run.

## Cursor

Sources: [CLI permissions](https://cursor.com/docs/cli/reference/permissions.md),
[hooks](https://cursor.com/docs/hooks.md),
[third-party hooks](https://cursor.com/docs/reference/third-party-hooks.md).

- `~/.cursor/cli-config.json` and the project's `.cursor/cli.json`: `Shell(name)` matches the first
  word of a command (globs allowed; `Shell(curl:*)` adds an argument pattern), `Read(glob)` and
  `Write(glob)` match paths, relative ones from the project. Deny beats allow. Reads run, shell
  commands and writes without an allow rule ask. **Assumption**: each command in a chain is checked
  on its own.
- The IDE keeps its command allowlist inside the app, where the tester cannot read it.
- Hooks in `.cursor/hooks.json` and the user folder: `preToolUse` (tool names `Shell`, `Read`,
  `Write`), `beforeShellExecution` (matched against the command text), and `beforeReadFile`. Exit 2
  or `permission: "deny"` blocks; invalid JSON on exit 0 blocks; crashes, timeouts, and other exit
  codes let the call through unless `failClosed` is set; `ask` is not enforced on `preToolUse`.
- Cursor also loads Claude Code hooks from `.claude/settings*.json` and the user settings file by
  default, but its shell tool is named `Shell`, so a `Bash` matcher never fires there.

## Replay

Replay reads session transcripts with `scripts/transcripts.py`, takes the newest N shell and
file-tool calls inside the `--since` window (a recently changed file can hold older records, which
are skipped), counts a forked session's copied calls once, and checks each call against the rules
of its own project folder. A Claude Code call is simulated in the permission mode its session
recorded for it; a subagent call uses its parent session's mode at that time; a call with neither
uses the mode from the settings, and `--mode` overrides all of them. Replay runs no hook unless you
pass `--replay-hooks` with `--run-hooks`, and then only this project's hooks, on calls from this
project. A replayed call counts as dangerous when one of the tester's hook checks matches it, or
when a file tool touches a secret file or a file other programs run later.

## Scoring

A `block` case counts as stopped only when it is blocked outright; an `ask` case counts when it
asks first or is blocked (`--fail-on-miss` uses this count). The report also sorts every case into
one of five groups: blocked; asks because of a rule, hook, or built-in check (these prompts stay
in auto mode); asks only because of the permission mode (gone in auto and `bypassPermissions`);
runs without asking (allow rules, the read-only set, sandbox auto-allow, and auto mode's
classifier); and unknown (a hook did not answer within the tester's limit).
