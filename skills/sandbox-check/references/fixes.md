# Fixes, per harness

Checked 2026-09-28. Use the section for the harness that ran the probe (the report's "agent" line), then "For any harness". Each fix names the exact setting. Show the user the change before making it, and run the probe again in a new session afterwards to confirm the rows turned blocked.

## Claude Code

Source: <https://code.claude.com/docs/en/sandboxing> and <https://code.claude.com/docs/en/settings-reference>.

The sandbox is off by default and covers the Bash, PowerShell, and Monitor tools. When it is on, commands can write only the working folder, a temporary folder, and folders you added; the `.claude` settings files, `.mcp.json`, `.vscode`, shell startup files in the working folder, and hooks and config inside `.git` stay protected.

1. **Turn the sandbox on.** Run `/sandbox`, or set it in your user settings (`settings.json` in your Claude Code folder) so every project gets it:

   ```json
   {
     "sandbox": {
       "enabled": true,
       "allowUnsandboxedCommands": false,
       "credentials": {
         "files": [
           { "path": "~/.config/gh/hosts.yml", "mode": "deny" },
           { "path": "~/.kube/config", "mode": "deny" }
         ],
         "envVars": [
           { "name": "GITHUB_TOKEN", "mode": "deny" }
         ]
       }
     },
     "permissions": {
       "blockReadsOutsideWorkingDirectories": true
     }
   }
   ```

2. **List every readable secret file.** The docs say there is no built-in credential deny list: only the files and variables you list are blocked. Add each file the probe marked open (your SSH folder and cloud credential files included) under `sandbox.credentials.files` with `"mode": "deny"`, and each secret-like variable under `sandbox.credentials.envVars`. `"mode": "mask"` keeps tools such as `gh` working by showing commands a stand-in value; it is honored only from user or managed settings.
3. **Close the escape hatch.** `"allowUnsandboxedCommands": false` stops Claude from retrying a blocked command outside the sandbox (shown as strict sandbox mode in `/sandbox`).
4. **Keep Docker out.** Leave `docker` out of `sandbox.excludedCommands` and keep the Docker socket out of `sandbox.network.allowUnixSockets`. An excluded command runs with no sandbox at all.
5. **Leave filesystem isolation on.** With `sandbox.filesystem.disabled` set, the docs warn that a command can write shell startup files, programs on your `PATH`, or Claude Code settings, and widen its own access on the next run.
6. **Strip credentials from every subprocess**, sandboxed or not, with the `CLAUDE_CODE_SUBPROCESS_ENV_SCRUB` environment variable.
7. **Add permission rules as a second layer.** A `Read(.env)` deny rule does not stop `cat .env` in the shell; the sandbox does. This repo's [safe settings template](../../../templates/claude-code-safe-settings/) adds deny rules and a guard hook.

## Codex

Sources: <https://learn.chatgpt.com/docs/config-file/config-reference.md>, <https://learn.chatgpt.com/docs/agent-approvals-security.md>.

1. **Use `workspace-write`.** Set `sandbox_mode = "workspace-write"` in `config.toml` in your Codex folder, or pass `--sandbox workspace-write`. Avoid `danger-full-access` and `--dangerously-bypass-approvals-and-sandbox` (alias `--yolo`).
2. **Know what it leaves open.** `workspace-write` keeps `.git`, `.agents`, and `.codex` read-only, but the rest of the project stays writable, including `.vscode`, `.venv`, and `.claude`. Your editor and other agents trust those. Review them after a session on an untrusted repository, and do not open that repository in an editor that runs tasks automatically.
3. **Keep network off.** `[sandbox_workspace_write] network_access = false` is the default; leave it.
4. **Filter secret variables.** Set `[shell_environment_policy] ignore_default_excludes = false` so Codex drops variables whose names contain KEY, SECRET, or TOKEN before running shell commands. By default Codex keeps them.
5. **Docker.** OpenAI classed Docker socket exposure as informational (Cloud Security Alliance note), so the sandbox is not the fix: stop Docker Desktop, Colima, or OrbStack while an agent works, or run the agent where the socket does not exist.
6. **Update** to Codex CLI 0.95.0 or later for the "GitPwned" allowlist fix.

## Gemini CLI

Sources: <https://github.com/google-gemini/gemini-cli/blob/main/docs/cli/sandbox.md>, <https://github.com/google-gemini/gemini-cli/blob/main/docs/cli/trusted-folders.md>.

1. **Turn the sandbox on.** It is off by default. Use `gemini --sandbox`, set `GEMINI_SANDBOX` to `docker`, `podman`, or `sandbox-exec`, or set `"tools": { "sandbox": true }` in `settings.json`. Inside it, commands see `SANDBOX` in their environment, which the probe reports.
2. **Pick a stricter macOS profile.** The default `SEATBELT_PROFILE` is `permissive-open`: writes restricted, network allowed. The `restrictive-` profiles apply stricter rules, and `strict-proxied` also restricts reads and sends network traffic through a proxy.
3. **Keep network off** inside the sandbox: `tools.sandboxNetworkAccess` defaults to false.
4. **Keep folder trust on** (`security.folderTrust.enabled`, default true). Untrusted folders skip project settings, `.env` files, and MCP servers.
5. **Move API keys out of shell startup files.** Gemini CLI's own authentication guide warns that any process launched from that shell can read them.
6. **Docker.** Google called the Docker socket exposure documented and out of scope (Cloud Security Alliance note); treat it as for Codex.

## Cursor

Sources: <https://cursor.com/docs/agent/security/run-modes>, and the Cloud Security Alliance note in [trust-handoff.md](trust-handoff.md).

1. **Use the sandboxed run mode.** Inside it, commands see `CURSOR_SANDBOX` (`seatbelt` on macOS, `native` on Linux). Writes are limited to the workspace and temporary folders, `.git/config`, `.git/hooks`, and `.vscode` are protected, and network is blocked until your network mode or `sandbox.json` opens it.
2. **Tighten `sandbox.json`** (in `.cursor` in your home folder, or in the project, which wins).
3. **Update** to Cursor 3.0.0 or later (hook settings, git metadata, and Docker socket fixes) and 3.1.2 or later (virtualenv fix).

## OpenCode

Source: <https://opencode.ai/docs/permissions>.

OpenCode has no operating-system sandbox, only permission rules. By default `read` denies `*.env` files (except `*.env.example`) and `external_directory` asks before touching folders outside the project. Keep those, remember that the last matching rule wins, and run OpenCode inside a dev container when the repository is untrusted.

## For any harness

- **Run the agent in a dev container or virtual machine** that mounts only the project: no home folder, no Docker socket, no SSH agent. The [sandboxing guide](../../../comparisons/sandboxed-code-execution.md) compares the options.
- **Use a separate user account for agent work.** Its home folder holds none of your credentials, shell startup files, or login items.
- **Keep credentials out of plain files.** The GitHub CLI stores its token in the system keychain unless you pass `--insecure-storage`; Docker can use a keychain helper through `credsStore`; short-lived cloud logins (such as AWS SSO) expire on their own.
- **Remove passwordless sudo** (`NOPASSWD` rules) on machines where agents run.
- **Stop Docker** (Docker Desktop, Colima, OrbStack, Rancher Desktop) when the agent does not need it.
- **Start the agent without your SSH agent** when it does not need to push: unset `SSH_AUTH_SOCK` in the shell that launches it.
