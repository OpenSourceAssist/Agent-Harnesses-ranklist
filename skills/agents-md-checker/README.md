# Check which instruction files your coding agents load

See which instruction files Claude Code, Codex, Gemini CLI, OpenCode, Cursor, GitHub Copilot, and Aider load from your repo, what each one cuts or skips, and whether the commands in those files still work.

## What you get

A sample report (the numbers are invented):

```text
**Claude Code ignores your AGENTS.md because a CLAUDE.md exists, Codex cuts its last 6 KB,
and 2 of 7 documented commands fail.**

| Agent          | Loads at session start               | Size                        | Cut or skipped         |
|----------------|--------------------------------------|-----------------------------|------------------------|
| Claude Code    | CLAUDE.md, ~/.claude/CLAUDE.md       | 5.1 KB, about 1,305 tokens  | skips AGENTS.md        |
| Codex          | AGENTS.md                            | 32 KB, about 8,192 tokens   | 6 KB cut               |
| Gemini CLI     | nothing                              | none                        | nothing                |
| OpenCode       | AGENTS.md                            | 38 KB, about 9,728 tokens   | skips CLAUDE.md        |
| Cursor         | AGENTS.md, .cursor/rules/style.mdc   | 39 KB, about 9,901 tokens   | skips .cursor/rules/api.md |
| GitHub Copilot | AGENTS.md, CLAUDE.md                 | 40 KB, about 10,102 tokens  | nothing                |
| Aider          | nothing                              | none                        | nothing                |

Failing commands:
  npm run lint   AGENTS.md:14  `package.json` has no script `lint`
  make docs      AGENTS.md:16  `Makefile` has no target `docs`

Next steps:
  1. Add `@AGENTS.md` as the first line of `CLAUDE.md` so Claude Code reads both.
  2. Move long sections into linked docs so Codex keeps the whole file.
  3. Fix `npm run lint` in `AGENTS.md:14`.
```

The full report also lists every file per agent with its status, the commands `--run` would execute, contradictions between the files (package manager, test runner, Node and Python versions), and links that point at nothing.

## Install

```
npx skills add https://github.com/RyanAlberts/best-of-Agent-Harnesses/tree/main/skills/agents-md-checker
```

In Claude Code, all ten at once:

```
/plugin marketplace add RyanAlberts/best-of-Agent-Harnesses
/plugin install harness-skills@agent-harnesses
```

Manual: copy `skills/agents-md-checker/` into your agent's skills folder: `~/.claude/skills/` (Claude Code), `~/.agents/skills/` (Codex, Gemini CLI, Cursor, and OpenCode all read it).

## Use it

Ask your agent: "Which instruction files does each coding agent load from this repo, and do the commands in our AGENTS.md still work?"

Or run the script yourself from your repo, using the folder where the skill is installed (this is the Claude Code path; use yours):

```
python3 "$HOME/.claude/skills/agents-md-checker/scripts/check.py" --repo .
```

That only reads files. To also run the documented tests, lint, type checks, and builds, add `--run` (the agent asks you first and shows each command with the script lines it runs). `--run` runs the project's own test and build code, which can do anything that code does. The checker screens the documented commands and the scripts and recipes they reach, not the code those scripts load, so use `--run` only on a repo whose tests you would run yourself. Add `--cwd packages/api` if you start the agent in a subfolder, and `--json` for machine-readable output. `scripts/load_map.py` and `scripts/commands.py` run each half alone.

## How it works

- **Load map.** For each agent, the script applies that agent's documented load rules to your folders: which file names it reads, how far up and down it walks, what size limit it applies, which settings change it, and which imports it follows. The rules and their sources are in [references/load-rules.md](references/load-rules.md).
- **Commands.** It pulls commands out of shell code blocks and inline code in every file an agent loads, then checks each one without running it: the npm script, Makefile target, or just recipe exists; the program is installed; the script path exists.
- **Run, only if you agree.** `--run` is an allowlist. A command runs only when it, and every script body, Make recipe, and prerequisite it reaches (after variable substitution), is a known test, lint, type check, or build step, or a lone `--help` or `--version` of an installed program, and its static check passed. Makefile, justfile, and shell features that can hide what runs (includes, conditionals, shell variables) also hold a command back. Anything the checker cannot classify is held back; the report says why ([the full rules](references/command-checks.md)). Commands run one at a time, with pipefail and a timeout.
- **Cross-file checks.** It flags files that disagree on the package manager, the test runner, or the Node and Python versions, and paths that no longer exist.

## Works with

| Agent | What the checker maps | Support |
|---|---|---|
| Claude Code | `CLAUDE.md` in every folder up to the top, `CLAUDE.local.md`, `.claude/rules`, the `AGENTS.md` rule, `@` imports, `claudeMdExcludes`, `instructionFiles` | Rules from the docs |
| Codex | `AGENTS.override.md`, `AGENTS.md`, fallback names, the 32 KiB budget, project trust | Rules from the docs and source; reading `config.toml` needs Python 3.11+ |
| Gemini CLI | `GEMINI.md` up to the `.git` folder, `context.fileName`, `@` imports, trusted folders | Rules from the docs |
| OpenCode | `AGENTS.md`, the `CLAUDE.md` fallback, `instructions` | Rules from the docs |
| Cursor | `.cursor/rules/*.mdc` rule types, `AGENTS.md` | Partial: User and Team Rules live in the app |
| GitHub Copilot | `copilot-instructions.md`, `.instructions.md` files, `AGENTS.md`, `CLAUDE.md`, `GEMINI.md` | Partial: each Copilot surface reads a different subset |
| Aider | files listed under `read:` in `.aider.conf.yml` | Partial: `--read` flags are not visible |

## Limits

- It works from files alone and leaves the agents closed. The rules match each agent's documentation as of 2026-09-28, and agents change. Where the docs are silent, the report marks the file unverified, and [references/load-rules.md](references/load-rules.md) lists every assumption.
- Files outside the repo are measured by size only, so the imports and commands inside them stay unchecked.
- Settings that live only in an app (Cursor User and Team Rules, Copilot organization instructions) are out of its reach.
- The allowlist reads the commands and the scripts they reach, not the code those scripts load (see Use it).
- Contradiction checks cover the package manager, the test runner, and Node and Python versions. Style and format linting is out of scope: use [agnix](https://github.com/agent-sh/agnix) or [driftlint](https://github.com/alifurkangokce/driftlint) for that.

## Privacy

The scripts read the instruction files and config files in your repo on your machine. Instruction files in your home folder and system folders (such as `~/.claude/CLAUDE.md` or `~/.codex/AGENTS.md`) are measured by size only; their text is never read or printed. From agent settings files in your home folder, the scripts read only the few settings that change loading (for example the Codex trust level for this project and the Gemini CLI file-name setting), and show only those values.

The checker makes no network calls; commands run with `--run` can, for example to download dependencies. Without `--run` nothing is executed; with it, only the listed commands run, and output excerpts in the report have secret-looking values replaced and are cut to 160 characters. File names, commands, and output from the repo are printed as inert text inside inline code (no line breaks, backticks, or table pipes), so links and HTML in them do not render, and a hostile repo cannot break the report or slip instructions into it.

## Related

- [One AGENTS.md for every coding agent](../../templates/agents-md/): the template this checker pairs with.
- [Write one AGENTS.md for every coding agent](../../playbooks/one-agents-md-for-every-coding-agent.md): the step-by-step playbook.
- [Context files for agents](../../comparisons/progressive-disclosure.md): why short instruction files with linked docs work better.
- Builds on the [AGENTS.md](https://agents.md) convention. Claude Code's built-in `/doctor prompt-audit` reviews Claude Code's own files with the model; this checker is a script, covers seven agents, and runs the commands.
