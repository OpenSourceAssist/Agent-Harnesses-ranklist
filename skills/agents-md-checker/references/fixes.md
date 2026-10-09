# Fixes

One fix per finding. Each heading is the finding id from the report (`--json` shows it as `id`). Show the user the exact edit as a diff and apply it only after a clear yes. After an edit, run `python3 "<skill-dir>/scripts/check.py" --repo .` again from the repo and confirm the finding is gone.

The pattern that fixes most load problems: keep one `AGENTS.md` as the single source, and make every other file a pointer to it.

## Load findings

### claude-ignores-agents-md

Claude Code reads `AGENTS.md` only when no `CLAUDE.md` exists in the start folder or above. Pick one:

- Keep both files: make an import of `AGENTS.md` the first line of the `CLAUDE.md` that hides it, and keep only Claude-specific rules below it. The import path is relative to the file that holds the import: in a root `CLAUDE.md` write `@AGENTS.md`; in `.claude/CLAUDE.md` write `@../AGENTS.md`. The report's fix gives the exact line.
- When the hidden `AGENTS.md` sits in a subfolder below that `CLAUDE.md` (a package in a monorepo), add a `CLAUDE.md` in that subfolder whose first line is `@AGENTS.md`, so the package's rules load only where they apply.
- Use only `AGENTS.md`: move anything unique from `CLAUDE.md` into `AGENTS.md`, then delete `CLAUDE.md`.
- Read both without an import: in `.claude/settings.json` set
  `{"pluginConfigs": {"agents-md@builtin": {"options": {"instructionFiles": "claude-md-and-agents-md"}}}}`.

When the file that causes it sits above the repo (for example `~/CLAUDE.md`), move or rename that file. It affects every project below it.

### codex-cuts

Codex reads at most `project_doc_max_bytes` (32 KiB by default) of project instructions, counting every `AGENTS.md` from the project root down to the start folder. Pick one:

- Move long sections (release steps, style guides, architecture notes) into files under `docs/` and leave a one-line pointer in `AGENTS.md`. The agent opens the doc when the task needs it.
- Put folder-specific rules in an `AGENTS.md` inside that folder, so they load only when the agent starts there.
- Raise the limit in `~/.codex/config.toml`: `project_doc_max_bytes = 65536`. A bigger file costs context in every session.

### codex-untrusted

`~/.codex/config.toml` marks the project untrusted, so Codex loads no project file (and no project `.codex/config.toml`). If the repo is yours, change the entry to `trust_level = "trusted"`, or remove it and answer the trust question at the next launch.

### codex-empty-override

An empty `AGENTS.override.md` takes its folder's slot, so Codex never reads the `AGENTS.md` next to it. Delete the empty file, or move the rules into it.

### codex-reads-nothing

Codex reads `AGENTS.md` (and `AGENTS.override.md`), not `CLAUDE.md`. Pick one:

- Rename `CLAUDE.md` to `AGENTS.md`, then create a `CLAUDE.md` whose only line is `@AGENTS.md`.
- Tell Codex to read `CLAUDE.md`: `project_doc_fallback_filenames = ["CLAUDE.md"]` in `~/.codex/config.toml`.

### gemini-reads-nothing

Gemini CLI reads `GEMINI.md` unless told otherwise. Point it at the file the repo already has. With an `AGENTS.md`, pick one:

- Add `.gemini/settings.json`: `{"context": {"fileName": ["AGENTS.md", "GEMINI.md"]}}`.
- Add a `GEMINI.md` whose only line is `@./AGENTS.md`.

With only a `CLAUDE.md`, use `{"context": {"fileName": ["CLAUDE.md", "GEMINI.md"]}}` or a `GEMINI.md` whose only line is `@./CLAUDE.md`. With neither, write the `AGENTS.md` first, then use the first pair.

### gemini-import-form

Gemini CLI documents imports only as `@./file.md`, `@../file.md`, or `@/absolute/file.md`. Rewrite a bare `@AGENTS.md` as `@./AGENTS.md`. Claude Code reads that form too.

### gemini-settings-ignored

Gemini CLI ignores `.gemini/settings.json` in folders you have not trusted. Trust the folder when Gemini CLI asks, or remove its `DO_NOT_TRUST` entry from `~/.gemini/trustedFolders.json`. To make the setting apply everywhere, put `context.fileName` in `~/.gemini/settings.json` instead.

### opencode-reads-nothing

Add an `AGENTS.md` at the repo root. OpenCode falls back to `CLAUDE.md` only when no `AGENTS.md` exists and `OPENCODE_DISABLE_CLAUDE_CODE` is not set.

### cursor-reads-nothing

Cursor finds no `AGENTS.md` and no `.mdc` rule, the files its docs say it reads (whether it reads `CLAUDE.md` or `.cursorrules` is not documented). Add an `AGENTS.md` at the repo root; Cursor reads it.

### cursor-md-ignored

Cursor reads only `.mdc` files in `.cursor/rules/`. Rename each `.md` file to `.mdc` and add frontmatter:

```
---
description: When this rule applies, in one sentence
globs: src/**/*.ts
alwaysApply: false
---
```

Use `alwaysApply: true` for a rule that should always load.

### cursor-manual-rule

A rule with no `alwaysApply`, `globs`, or `description` applies only when someone @-mentions it. If it should apply on its own, add one of the three.

### cursor-rule-too-long / claude-file-long

Cursor's guidance is under 500 lines per rule and Claude Code's is under 200 lines per file. Move procedures and reference material into linked docs; keep what every session needs.

### cursor-legacy-rules

`.cursorrules` is the old single-file format and the current Cursor docs no longer mention it. Move its rules into `.cursor/rules/*.mdc` files or into `AGENTS.md`, then delete it.

### claude-file-too-large

Claude Code skips a memory file over 4 MiB entirely. Split it: keep the essentials, and move the rest into docs the agent opens when needed.

### broken-import

An `@path` import points at a file that does not exist, so the agent loads nothing from it. Fix the path (it is relative to the importing file) or remove the line.

### aider-not-configured

Aider loads no instruction file on its own. If the user runs Aider, add `read: AGENTS.md` to `.aider.conf.yml` at the repo root.

### aider-read-missing

Fix the file names under `read:` in `.aider.conf.yml`.

## Commands

- **A missing script, target, or recipe** (the report says `package.json` has no script `lint`, or `Makefile` has no target `docs`): rename the command in the instruction file to one that exists, or add the missing script. Check `package.json` scripts, the Makefile, or the justfile for the current name.
- **A program not on PATH**: the command names a tool that is not installed here. Either the instruction file should say how to install it, or the command is stale.
- **A missing file or folder**: the command names a path that moved. Update the path.
- **A failed run**: the output excerpt in the report shows why. When the command fails for everyone, fix the command in the instruction file; when it needs setup (a database, a service, an environment variable), write that setup step next to it.
- **Did not finish**: the command ran past `--timeout`. That is not a failure; rerun with a longer timeout, such as `--timeout 600`.
- **Held back by --run**: the reason names what the allowlist does not cover (a program, a flag, a variable, a script line, or a Makefile or justfile feature). Nothing needs fixing when the command is fine as written: the user can run it by hand. When the documented command is not the one people should use (it updates snapshots, installs first, or runs a server), replace it in the file with the plain test, lint, or build command.
- **A slow test loop**: agents run the test command many times per task. Document a fast command for one file or one test (for example `pytest tests/test_api.py -q` or `npm test -- api.test.ts`) next to the full suite.

## Contradictions

- **package-manager / lockfiles**: use one package manager in every instruction file and keep only its lockfile. The `packageManager` field in `package.json`, when present, is the tie-breaker.
- **test-runner**: name the one test runner the repo uses, with the exact command, in every file. Remove mentions of a runner that is not in the dependencies.
- **node-version / python-version**: make the version in the instruction files match `.nvmrc`, `.node-version`, `.python-version`, `.tool-versions`, `engines.node`, and `requires-python`. The version files win; they are what tools read.

## Dead paths

Update each path to where the file lives now, or delete the line. An instruction that points to a missing file costs the agent a failed lookup in every session that follows it.
