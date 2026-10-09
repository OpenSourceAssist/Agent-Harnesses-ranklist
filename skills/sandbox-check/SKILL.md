---
name: sandbox-check
description: >-
  Sandbox audit that probes, from inside a live coding-agent session, what the
  agent can really reach: secret files it can open, secret-like environment
  variables, files that git, the shell, the editor, or the harness later run
  (git hooks, shell startup files, harness settings), writes outside the
  project, the Docker socket, the SSH agent, and passwordless sudo. Use when
  the user asks what the agent can access or write on this machine, whether
  the sandbox actually works, how big the blast radius is if the agent gets
  prompt-injected, whether SSH keys, cloud credentials, or .env files are
  exposed, or whether it could escape through Docker or sudo. Runs locally and
  edits no existing file: it creates and deletes one empty test file per
  folder it checks, and only the optional --network flag makes DNS lookups
  and TCP connections.
license: MIT
compatibility: "Python 3.9+ on macOS or Linux; Python 3.11+ to compare Codex settings. Without flags nothing touches the network. The optional --network flag makes DNS lookups and TCP connections (no data sent) to github.com, pypi.org, and the cloud metadata address 169.254.169.254."
metadata:
  author: "Ryan Alberts"
  version: "1.0.0"
  source: "https://github.com/RyanAlberts/best-of-Agent-Harnesses"
---

# Sandbox check

A coding agent's shell commands have whatever access that shell has, and a sandbox is only as good
as what actually gets through it. This skill runs one probe through the agent's own shell tool and
reports, headline first, what the agent can reach right now: secret files, secret-like environment
variables, the files other programs later trust and run (a **trust handoff**), the Docker socket,
the SSH agent, sudo, and, only when asked, the network. It reads no secret, prints no secret value,
and sends nothing anywhere unless you add `--network`.

## When to use

- The user asks what the agent can read, write, or reach on this machine or in this container.
- The user wants to know whether a sandbox really holds: Claude Code `/sandbox`, Codex sandbox
  modes, Gemini CLI `--sandbox`, or Cursor's sandbox.
- The user worries about prompt injection or a rogue agent and asks for the blast radius.
- The user asks whether SSH keys, cloud credentials, `.env` files, or tokens are exposed to the
  agent.
- Before an agent works unattended or on an untrusted repository.

## When not to use

- Testing whether permission rules or hooks block dangerous commands: use `guardrail-tester`.
- Stopping a session that loops or overspends: use `runaway-guard`.
- Checking which instruction files each agent loads: use `agents-md-checker`.
- Finding secrets committed to git history: use a secret scanner such as gitleaks. This skill checks
  access, not file contents.
- Turning a sandbox on: that is a settings change. This skill measures the result, and
  `references/fixes.md` names the setting.

## What the probe touches

Tell the user this, in short, before the first run. It is the whole contract:

- **Secret files**: opened and closed without reading a byte. Files that macOS keeps only in the
  cloud are skipped, so they stay in the cloud.
- **Existing files other programs trust**: opened for append and closed at once, so contents and
  modified times stay the same.
- **Folders**: one empty file named `.sandbox-check-` plus random letters and `.tmp` is created and
  deleted at once. That includes the login-items folder (`Library/LaunchAgents` on macOS; the
  autostart and systemd user folders on Linux). The folder's own modified time changes, and a file
  watcher (a dev server, a sync app) may notice for a moment. A test file that cannot be deleted is
  named in the report.
- **Targets that do not exist** stay absent: the probe tests only what is there.
- **Docker socket and SSH agent**: connect, then close, with no data sent. A socket-activated Docker
  or Podman service starts when something connects, so the probe can start it.
- **sudo**: `sudo -n true`, the form that fails instead of asking for a password. The system log
  may record the attempt; `--skip-sudo` leaves sudo alone.
- **Environment variables**: names and value lengths only.
- **Settings files** of Claude Code, Codex, and Gemini CLI: parsed for their sandbox keys, which
  are the only part the report shows.
- **Network**: used only with `--network`.

On a work machine, endpoint security and audit rules may log or flag the probe: the sudo attempt,
the test file in the login-items folder, and the write-opens of shell startup files and the SSH
`authorized_keys` file. On Linux, each append check also sends a file-close event to any program
watching that file.

`scripts/targets.json` lists every path the probe checks, so it names credential files by design.
Each of those lines carries the marker `skillscan:allow`, which tells this repository's security
scanner that the path is a probe target, not a file the skill reads.

## Steps

`<skill-dir>` means the folder that holds this SKILL.md (Claude Code shows it as the skill's base
directory). Run every command from the user's project folder.

1. **Explain the probe** in two or three sentences drawn from "What the probe touches", including
   the sudo attempt and the login-items test file that security tools may flag. Done when the user
   says to go ahead. Wait for that answer before step 2.

2. **Run the probe once, through your normal shell tool:**

   ```bash
   python3 "<skill-dir>/scripts/probe.py" --project .
   ```

   `--project` is the folder the agent works in. When `python3.11` or newer is installed (such as
   `python3.12` or `python3.13`), use it in place of `python3`: the stock macOS `python3` is 3.9,
   which skips the Codex settings comparison. The run takes about a second and exits 0 even when
   checks are blocked: blocked checks are the result, not an error. Run it exactly as sandboxed as
   every other command in this session, and report what the sandbox blocked as blocked. Keep it out
   of any sandbox bypass (such as a `dangerouslyDisableSandbox` retry): an unsandboxed rerun
   measures a different shell and turns a good result into a false alarm. Done when the output
   starts with a bold headline sentence, or you have told the user the exact error. Exit code 2
   means a bad argument or a missing project folder; Python also exits 2 when the script path is
   wrong.

3. **Offer the network check as its own choice.** Say that it looks up `github.com` and `pypi.org`,
   opens and closes a TCP connection to each on port 443 and to the cloud metadata address
   `169.254.169.254` on port 80, sends no data, and waits at most 3 seconds per host. Run it only
   after a clear yes:

   ```bash
   python3 "<skill-dir>/scripts/probe.py" --project . --network
   ```

   Done when the user declined, or the new report's Network rows read open or blocked.

4. **Choose the fixes.** Open `references/fixes.md` at the section for the harness named on the
   report's "agent" line, and pick the two or three fixes that close the most Critical and High
   rows. When that section has no fix for a row (the SSH agent, for example), use the section "For
   any harness". Use `references/trust-handoff.md` to explain why a row matters. Done when each
   chosen fix names the exact setting, flag, or command to change.

5. **Report** in the shape below. A fix that edits a settings file is a proposal: show the exact
   change, and apply it only after a clear yes.

## Read the results

- **Headline**: the sentence to lead with. It counts readable secret files, then names the worst
  reach: running as root, sudo without a password, control of Docker, writes to git hooks.
- **Score**: `N of M checks open`, split by risk. Only checks that ran are counted: missing,
  skipped, hidden, and unknown checks and Info rows are left out.
- **Status of each check**:
  - `open`: reachable from this shell. A read-only file in a writable folder also counts as open,
    because it can be replaced; its detail says so.
  - `blocked`: exists, but the open, write, or connect was refused. A folder that refuses lookups
    gets one blocked row.
  - `hidden`: inside a folder that refuses lookups, so the probe cannot tell whether it exists.
  - `missing`: not present, so not tested. A socket file with nothing listening reads missing too.
  - `skipped`: a cloud placeholder, `--skip-sudo`, or network not requested.
  - `unknown`: an unexpected error; `--json` has the detail.
- **Risk levels**:
  - Critical: control Docker, write git hooks or git config, change shell startup files, add login
    items or SSH login keys, sudo without a password, running as root.
  - High: readable secret files, harness settings, hooks, skills, and subagents, the editor folder
    and virtualenv, new files in the home folder, the SSH agent, the cloud metadata address.
  - Medium: secret-like environment variables, new files in the parent folder, direct internet
    connections.
  - Info: the project folder itself, and DNS.
- **Where it runs**: the user, the operating system, the harness (found from the variables each
  harness sets in its shell: `CLAUDECODE`, `CODEX_SANDBOX` and other `CODEX_` variables,
  `GEMINI_CLI`, `CURSOR_SANDBOX`), container signs, and sandbox markers. "Agent: none found" means
  the probe ran in a plain terminal, so the results show what any program started there can reach.
- **Settings compared with what the probe found**: gaps between what the settings of the harness
  that ran the probe claim and what got through.
  - "Settings say workspace-write, yet `~/.zshrc` is writable" means the command ran outside the
    sandbox, or the sandbox allows more than its settings suggest.
  - "The sandbox leaves `./.vscode` writable" is a trust handoff the sandbox allows by design;
    `references/trust-handoff.md` has the documented escapes that used it.
  - "The sandbox is off" is the finding that explains most open rows. The probe reads settings
    files only, so when writes to the home folder were blocked anyway, it turns this into a note: a
    sandbox from `--settings` or managed settings may be on.
- **Notes**: what was not checked and why, such as a Codex config skipped on Python older than 3.11.
- **`--json`** lists every target, missing ones included, each with `id`, `category`, `group`,
  `risk`, `target`, `status`, and `detail`, plus `settings`, `gaps`, and `leftovers`.

## Report to the user

1. The headline, verbatim, in bold.
2. The report's table cut to rows with something open, eight rows at most: Critical rows first,
   then High. If any open row does not fit, name the dropped rows in one line under the table.
3. The gaps from "Settings compared with what the probe found", one line each.
4. The two or three fixes from step 4, each with the exact setting and a pointer to
   `references/fixes.md`.
5. One closing line: offer the network check if it has not run, and name any test file the report
   says could not be deleted, so the user can remove it.

Quote paths exactly as the report prints them: it shows the home folder as `~`, a folder moved by a
variable as `$VARIABLE`, and names and lengths in place of secret values. Names from the file
system and values from settings arrive as one plain line, with secret-shaped text masked and
backticks and pipes replaced, and the Markdown report shows them inside inline code, so they stay
inert text.

## Files

- `scripts/probe.py`: the probe. Python 3.9+, standard library only. Flags: `--project`,
  `--network`, `--skip-sudo`, `--json`, `--out <path>`, `--fail-on critical|high|medium` (exit 1
  when a check at that level is open).
- `scripts/targets.json`: every path, socket, variable pattern, harness marker, and settings file
  the probe checks, with the scanner marker on each credential line.
- `scripts/safe.py`: the text cleaner that several skills in this repository share. It masks
  secret-shaped text and keeps each name from the file system or settings on one line, inside
  inline code in the Markdown report.
- `references/trust-handoff.md`: why each target matters, with the Pillar Security and Cloud
  Security Alliance findings and their CVE ids.
- `references/fixes.md`: fixes for Claude Code, Codex, Gemini CLI, Cursor, and OpenCode, plus fixes
  for any harness.
