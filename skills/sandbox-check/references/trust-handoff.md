# Trust handoffs: why each target matters

Checked 2026-09-28. Read this when you need to explain a row of the report, or when the user asks why a writable file is dangerous.

A sandbox limits what the agent's own commands can do. A **trust handoff** is the way around it: the agent writes a file, and later a program that runs outside the sandbox (git, your shell, your editor, the login system, or the agent harness itself) reads that file and does what it says, with your full rights. Pillar Security's summary of a week of sandbox escapes in July 2026 makes the point plainly: an agent that can write the future inputs of other programs was never really sandboxed.

## Sources

- Pillar Security, "The Week of Sandbox Escapes", 2026-07-20, by Eilon Cohen, Dan Lisichkin, and Ariel Fogel: <https://www.pillar.security/blog/the-week-of-sandbox-escapes>
- Cloud Security Alliance AI Safety Initiative, "AI Coding Agent Sandbox Escapes: The Trust Handoff Flaw", research note, 2026-07-22: <https://labs.cloudsecurityalliance.org/research/csa-research-note-ai-coding-agent-sandbox-escapes-20260722-c/>
- Claude Code sandbox, section "Protected paths": <https://code.claude.com/docs/en/sandboxing>
- Codex sandbox modes (in `workspace-write`, `.git`, `.agents`, and `.codex` stay read-only): <https://learn.chatgpt.com/docs/agent-approvals-security.md>
- Cursor sandbox and its protected paths: <https://cursor.com/docs/agent/security/run-modes>
- Gemini CLI sandbox (it confines writes to the project folder): <https://github.com/google-gemini/gemini-cli/blob/main/docs/cli/sandbox.md>

## The documented escapes

| Case | Product | ID | What the agent wrote or reached | What ran it later | Status in the sources |
|---|---|---|---|---|---|
| Hook settings | Cursor | CVE-2026-48124, GHSA-pc9j-3qc2-95wv (CVSS 8.5) | hooks in `.claude/settings.local.json` | Cursor ran the hook command outside the sandbox, with no approval | fixed in Cursor 3.0.0 |
| Virtualenv interpreter | Cursor | GHSA-p9g2-cr55-cw9c | replaced the interpreter in `.venv/bin` | the editor's Python extension ran it while looking for interpreters | fixed; the CSA note says update to 3.1.2 |
| Git metadata folder | Cursor | no CVE | a git metadata folder not named `.git`, with its own fsmonitor program | the Git extension ran that program | fixed in Cursor 3.0.0 |
| "GitPwned" | Codex CLI | no CVE; CSA rates it CVSS 8.6 | `git show --output=./.git/config` wrote an external diff tool into the git config; the allowlist trusted `git show` by name | git ran the tool on the next `git diff` | fixed in Codex CLI 0.95.0 |
| Docker socket | Cursor, Codex CLI, Gemini CLI | GHSA-v4xv-rqh3-w9mc (Cursor's advisory) | connected to `/var/run/docker.sock` | Docker started a privileged container that mounted the home folder | fixed in Cursor 3.0.0; per CSA, OpenAI called its exposure informational and Google called it documented and out of scope, so neither changed |
| VS Code task | Antigravity | no CVE | `.vscode/tasks.json` | the host ran the task outside Secure Mode | Pillar calls it hard to exploit; no fix reported |
| Seatbelt denylist | Antigravity | no CVE | nothing: a denylist-style macOS profile left system features reachable | commands ran outside the sandbox | no fix reported |
| "DuneSlide" | Cursor | CVE-2026-50548, CVE-2026-50549 (CVSS 9.8 each) | prompt-injected commands abused working-folder and symlink checks | they overwrote the sandbox helper and wrote outside the workspace | listed by CSA without further detail |

## Each target the probe checks

"Protected by default" means the harness's own sandbox blocks the write when it is on, per the docs above. Outside the project folder, every one of these sandboxes blocks writes by default (Claude Code, Codex, and Cursor also allow temporary folders).

| Target | Who trusts it, and when | Probe | Protected by default |
|---|---|---|---|
| Git hooks folder (`.git/hooks`) | git runs these scripts on commit, checkout, merge, and push | one empty test file, created and deleted | Claude Code, Codex, Cursor |
| Git config (`.git/config`) | git runs the fsmonitor, diff, and merge programs it names (the GitPwned case) | opened for append, closed, nothing written | Claude Code, Codex, Cursor |
| Global git config (`.gitconfig` in your home folder) | every git command in every repository reads it | append check | outside the project |
| Shell startup files (`.zshrc`, `.zshenv`, `.zprofile`, `.zlogin`, `.bashrc`, `.bash_profile`, `.bash_login`, `.profile`, and fish's `config.fish` and `conf.d` folder under `.config/fish`) | every new terminal runs them. The Claude Code docs warn that a command able to write them can widen its own access on the next run | append check; test file for `conf.d` | outside the project |
| Login items (`Library/LaunchAgents` on macOS; `.config/systemd/user` and `.config/autostart` on Linux) | the system starts these programs at every login | test file | outside the project |
| SSH login keys (`authorized_keys` in your SSH folder) | a key added here lets someone log in, when remote login is on | append check | outside the project |
| Editor folder (`.vscode`) | VS Code and its forks run `tasks.json` tasks, some on folder open (the Antigravity case) | test file | Claude Code, Cursor |
| Python virtualenv (`.venv/bin`) | the editor's Python extension runs the interpreter to inspect it (Cursor GHSA-p9g2-cr55-cw9c) | test file | none of the defaults |
| Harness settings and hooks | the harness runs the hooks, MCP servers, and plugins listed there, often outside its own sandbox (Cursor CVE-2026-48124) | append check, or a test file for plugin and rule folders | Claude Code protects its own `.claude` settings and `.mcp.json`; Codex keeps `.codex` read-only; Cursor protects its sensitive config files |

The harness settings files, by harness:

- Claude Code: `.claude/settings.json`, `.claude/settings.local.json`, and `.mcp.json` in the project; `settings.json` in your Claude Code folder (moved by `CLAUDE_CONFIG_DIR`) and `.claude.json` in your home folder. Also the `skills` and `agents` folders, in `.claude` in the project and in your Claude Code folder, because skill and subagent frontmatter can define hooks. Cursor also runs hooks from the `.claude` settings files by default.
- Codex: `.codex/config.toml` and `.codex/hooks.json` in the project (read in trusted projects only); `config.toml`, `hooks.json`, and the `rules` folder in your Codex folder (moved by `CODEX_HOME`). Codex asks you to trust a changed hook again before it runs.
- Gemini CLI: `.gemini/settings.json` in the project; `settings.json` and `trustedFolders.json` in the `.gemini` folder in your home folder. Gemini CLI treats a changed project hook as new and untrusted.
- Cursor: `hooks.json`, `mcp.json`, and `sandbox.json` in `.cursor` in the project; the same plus `cli-config.json` in the `.cursor` folder in your home folder.
- OpenCode: `opencode.json` and the `.opencode/plugins` folder in the project; `opencode.json` and `plugins` in its folder under `.config`. OpenCode has no operating-system sandbox, so nothing here is protected by default.

## Reach that needs no file at all

| Target | Why it matters | Probe |
|---|---|---|
| Docker or Podman socket | whoever can connect controls Docker, and Docker can start a container that mounts your home folder. The Claude Code docs say that allowing `/var/run/docker.sock` through the sandbox "effectively grants access to the host system" | connect, then close; nothing sent |
| SSH agent (`SSH_AUTH_SOCK`) | commands can use your SSH keys through the agent without ever reading a key file: push to GitHub, log in to servers | connect, then close |
| `sudo -n true` | success means root without a password prompt, right now | runs `sudo -n true`; never prompts |
| Running as root | every file on the machine or container is writable | reads the user id |
| New files in your home folder | if this works, most targets above are within reach even when they do not exist yet | test file in the home folder |
| Cloud metadata address (169.254.169.254), with `--network` only | on cloud machines this address hands out the machine's own credentials | TCP connect, then close |

## What "missing" means

The probe does not test a target that does not exist yet, because creating it would plant the very file other programs trust. If the agent can create files in the folder that would hold it, it can usually create the target too, unless a sandbox blocks that name: Claude Code blocks creating several of these by name (the `.claude` settings files, `.mcp.json`, `.vscode`, and hooks and config inside `.git`). So a missing target is not a clean bill of health, and the report does not count it either way.

A folder that refuses lookups is different: the probe cannot tell whether the files inside it exist. The report shows that folder once, as blocked, and each file inside it as `hidden`, which is not counted. A read-only file in a folder the shell can write counts as open, because the shell can delete it and write a new one in its place (dotfiles managed by Nix home-manager are an example).
