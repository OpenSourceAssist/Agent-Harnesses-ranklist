# Check what your coding agent can reach

A one-minute probe that runs through your agent's own shell and shows which secret files, trust files, Docker, and sudo access it really has, then compares that with what your sandbox settings claim.

## What you get

A sample report. The numbers and names are invented; your agent prints its own.

```
**Your agent can open 7 secret files, control Docker, and write to your git hooks.**

22 of 27 checks open: critical 4 of 8, high 16 of 17, medium 2 of 2.

Where it runs: user `sam` (uid 501) on macOS 26.1; agent: Claude Code; container signs: none;
sandbox markers: none; new files in the project: allowed; new files in your home folder: allowed.

| Risk | Area | Open | Found |
|---|---|---|---|
| Critical | Docker control | 1 of 1 | `~/.docker/run/docker.sock` |
| Critical | Git hooks and config | 2 of 3 | `./.git/hooks`, `./.git/config` |
| Critical | Shell startup files | 1 of 2 | `~/.zshrc` |
| Critical | Root and sudo | 0 of 2 | all blocked |
| High | Cloud credentials | 2 of 2 | `~/.kube/config`, `~/.azure/msal_token_cache.json` |
| High | Package and git tokens | 2 of 3 | `~/.config/gh/hosts.yml`, `~/.git-credentials` |
| High | Harness logins | 1 of 1 | `~/.codex/auth.json` |
| High | .env files | 2 of 2 | `./.env`, `../.env.local` |
| High | Harness settings and hooks | 5 of 5 | `./.claude/settings.local.json`, `~/.codex/config.toml`, `~/.gemini/settings.json` and 2 more |
| High | Editor tasks and settings | 1 of 1 | `./.vscode` |
| High | Python virtualenv | 1 of 1 | `./.venv/bin` |
| High | New files in your home folder | 1 of 1 | `~` |
| High | SSH agent | 1 of 1 | `$SSH_AUTH_SOCK` |
| Medium | Secret-like environment variables | 3 names | listed below |
| Medium | New files in the parent folder | 1 of 1 | `..` |
| Info | Network | not checked | run again with `--network` to test it |

Secret-like environment variables (names and lengths only): `GITHUB_TOKEN` (40 characters),
`OPENAI_API_KEY` (164 characters), `NPM_TOKEN` (36 characters).

Settings compared with what the probe found:
- Claude Code (critical): The Claude Code sandbox is off: no settings file sets sandbox.enabled
  to true, so shell commands run with your full user rights.
```

With the sandbox on, the same table turns mostly blocked, and the settings section points at what still gets through, such as "The Codex sandbox leaves `./.vscode`, `./.venv/bin` writable, and programs outside the sandbox run what is there later."

## Install

```
npx skills add https://github.com/RyanAlberts/best-of-Agent-Harnesses/tree/main/skills/sandbox-check
```

In Claude Code, all ten at once:

```
/plugin marketplace add RyanAlberts/best-of-Agent-Harnesses
/plugin install harness-skills@agent-harnesses
```

Manual: copy `skills/sandbox-check/` into your agent's skills folder: `~/.claude/skills/` (Claude Code), `~/.agents/skills/` (Codex, Gemini CLI, Cursor, and OpenCode all read it).

## Use it

Ask your agent: "What can you actually reach on this machine? Run the sandbox check."

The agent explains what the probe touches, waits for your go-ahead, runs it once through its normal shell tool, and offers the network check as a separate choice. To run the script yourself, call it from the project folder:

```bash
python3 ~/.claude/skills/sandbox-check/scripts/probe.py --project .
```

Run from the agent's shell, it measures the agent. Run from your own terminal, it measures your terminal, which is what an agent without a sandbox gets. Add `--network` to test outbound connections, `--json` for every target, `--skip-sudo` on shared machines, and `--fail-on critical` to exit 1 when a Critical check is open.

## How it works

The probe is one Python script with a target list beside it. It checks:

- **Secret files**: SSH private keys, AWS, Google Cloud, Azure, and Kubernetes credentials, the Docker login file, npm, PyPI, netrc, and git credential files, the GitHub CLI login, each harness's own login file, and `.env` files in the project and its parent folders.
- **Secret-like environment variables**: names and value lengths only.
- **Trust handoffs**: files a program outside the sandbox later runs. Git hooks and git config, shell startup files (zsh, bash, and fish), login items, SSH login keys, the editor folder, the project's virtualenv, and the settings, hook, skill, and subagent files of Claude Code, Codex, Gemini CLI, Cursor, and OpenCode.
- **Everything else in reach**: new files in your home folder and the parent folder, the Docker or Podman socket, the SSH agent, `sudo -n true`, and whether it runs as root. With `--network`: DNS, direct connections to github.com and pypi.org, and the cloud metadata address.

It edits no existing file. Secret files are opened and closed without reading a byte. Existing files are opened for append and closed at once, so their contents and modified times stay the same. A folder gets one empty test file that is deleted at once; only the folder's own modified time shows it happened. Sockets are connected and closed, with no data sent. The probe tests only targets that exist, because creating a missing one would plant the very file other programs trust.

Every file, folder, and variable name in the report passes through a filter that keeps it on one plain line, so a hostile file name stays inert text.

Then it reads the sandbox keys of your harness settings (Claude Code's settings layers, Codex `sandbox_mode`, Gemini CLI `tools.sandbox`) and flags gaps, such as "Codex settings say workspace-write, yet `~/.zshrc` is writable from this shell." The [trust handoff reference](references/trust-handoff.md) explains each target with the documented escapes and CVE ids, and the [fixes reference](references/fixes.md) gives the setting that closes each row.

## Works with

| Harness | Detected from | Settings compared | Support |
|---|---|---|---|
| Claude Code | `CLAUDECODE` | `sandbox` keys in user, project, local, and managed settings | full; smoke-tested on macOS |
| Codex | `CODEX_SANDBOX`, `CODEX_THREAD_ID`, and other `CODEX_` variables | `sandbox_mode`, network access, and secret filtering in `config.toml` | full with Python 3.11 or newer; the stock macOS `python3` (3.9) skips the settings comparison |
| Gemini CLI | `GEMINI_CLI`; `SANDBOX` inside its sandbox | `tools.sandbox` | full |
| Cursor | `CURSOR_SANDBOX` inside its sandbox | the marker stands in for settings | partial |
| OpenCode | reported as a plain shell | permission rules only, so there is nothing to compare | its settings, plugins, and login file are checked |
| Any shell or container | reported as a plain shell | | every check runs |

It runs on macOS and Linux, including WSL2 and containers, with Python 3.9 or newer and nothing else to install. Windows outside WSL2 is not supported.

## Limits

- It measures one shell at one moment. An agent's other tools can have different access: Claude Code's sandbox covers only its Bash, PowerShell, and Monitor tools, while its file tools follow permission rules instead.
- It tests only targets that exist. If the agent can create files in the folder that would hold one, it can usually create it.
- When a folder refuses lookups, the report shows that folder once as blocked and the files inside it as hidden, because the probe cannot tell whether they exist.
- It knows only the secrets on its list. The macOS Keychain, password managers, web browsers, tokens inside other files, and `.env` files in the project's subfolders are outside it.
- It checks `.git/hooks` and `.git/config`; a custom `core.hooksPath` folder is outside its list, though write access to `.git/config` is enough to set one.
- It reaches Docker through local sockets only; Docker over TCP (`DOCKER_HOST=tcp://...`) is outside its list.
- A variable that Claude Code's sandbox masks shows the length of its stand-in value.
- `sudo -n true` also succeeds for a few minutes after a sudo password was typed in the same terminal session. That access is real while it lasts, and it expires.

## Privacy

- **Read**: file and folder metadata, and whether each secret file can be opened. Never the contents of a secret file. Harness settings files are parsed for their sandbox keys only.
- **Printed**: paths, with your home folder as `~` and moved folders as `$VARIABLE`; variable names and value lengths; your user name and user id. Never a secret value.
- **Sent**: only with `--network`: DNS lookups and TCP connections to github.com, pypi.org, and 169.254.169.254, carrying no data.
- **Written**: one empty test file per folder, deleted at once, and the report file if you pass `--out`.
- **Noticed**: `sudo -n true` may leave a line in the system log. On a work machine, endpoint security and audit rules may log the sudo attempt, the test file in your login-items folder, and the write-opens of shell startup files and `authorized_keys`. On Linux, each append check sends a file-close event to programs watching that file. Connecting to a socket-activated Docker or Podman service starts it.

## Related

- [Agent sandboxing: what it is and how to pick](../../comparisons/sandboxed-code-execution.md): the options for running an agent in a box.
- [Safe Claude Code settings](../../templates/claude-code-safe-settings/): permission rules and a guard hook to pair with the sandbox.
- [guardrail-tester](../guardrail-tester/): tests whether your permission rules and hooks stop dangerous commands.
- The trust-handoff targets come from Pillar Security's [The Week of Sandbox Escapes](https://www.pillar.security/blog/the-week-of-sandbox-escapes) and the Cloud Security Alliance's [research note on the trust handoff flaw](https://labs.cloudsecurityalliance.org/research/csa-research-note-ai-coding-agent-sandbox-escapes-20260722-c/).
- For enforcement rather than an audit, see Anthropic's [sandbox-runtime](https://github.com/anthropics/sandbox-runtime).
