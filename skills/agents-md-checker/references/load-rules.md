# How each agent loads instruction files

`scripts/load_map.py` applies the rules on this page. They come from each agent's own documentation or source code, checked on 2026-09-28. Where the documentation is silent, the page says what the checker assumes, so you can judge a result before acting on it.

Words used below:

- **Start folder**: the folder where the agent starts (`--cwd`; the repo root by default).
- **At start**: loaded into every session before the first prompt.
- **On demand**: loaded later, when the agent works on files in that folder.
- **Size only**: files outside the repo (your home folder, folders above the repo, system folders) are measured. Their text is never read or printed, so imports inside them are not followed. A link inside the repo that points to a file outside it counts as outside.
- Token counts are estimates: bytes divided by 4.

## Claude Code

Sources: https://code.claude.com/docs/en/memory and https://code.claude.com/docs/en/settings

- **Order.** The managed file (`/Library/Application Support/ClaudeCode/CLAUDE.md` on macOS, `/etc/claude-code/CLAUDE.md` on Linux), then your user file `~/.claude/CLAUDE.md` and `~/.claude/rules/`, then `CLAUDE.md`, `.claude/CLAUDE.md`, `.claude/rules/*.md`, and `CLAUDE.local.md` in every folder from the top of the file system down to the start folder. Closer folders load later. `CLAUDE_CONFIG_DIR` moves the user files.
- **Below the start folder**, these files load on demand.
- **AGENTS.md** (version 2.1.277 and later): Claude Code reads `AGENTS.md` and `.claude/AGENTS.md` only when no `CLAUDE.md`, `.claude/CLAUDE.md`, or `CLAUDE.local.md` exists in the start folder or any folder above it. That includes folders above the repo, such as a stray `~/CLAUDE.md`. Your user file, the managed file, and `.claude/rules/` do not count.
- **The switch** `pluginConfigs["agents-md@builtin"].options.instructionFiles` in a settings file: `claude-md-or-agents-md` (the default), `claude-md-and-agents-md` (read both), `claude-md` (never read AGENTS.md), or `managed-only` (taken only from user or managed settings).
- **Never read**: `AGENTS.override.md`, `AGENTS.local.md`, `.agents/`.
- **Imports**: `@path` anywhere outside code spans, code blocks, and comments; relative to the importing file, or `~/`, or absolute; up to 4 hops. Claude Code asks once before loading an import from outside the working folder; the map notes those files.
- **Size**: a file over 4 MiB is skipped. The documented guidance is under 200 lines per file.
- **`claudeMdExcludes`**: glob patterns matched against absolute paths; the lists from all settings files add up. The managed file cannot be excluded.
- **HTML comments** on their own lines are stripped before loading, so the loaded size leaves them out.

What the checker assumes:

- A `.claude/rules/` folder counts in every folder of the walk, the same way `.claude/CLAUDE.md` does.
- Project settings come from the start folder first, then the repo root (Claude Code writes `settings.local.json` at the git root).
- An `@path` at the end of a sentence is read without the final punctuation mark.
- `managed-only` mode is not modeled: the other files show as unverified.
- Not mapped: auto memory (`MEMORY.md`), folders added with `--add-dir`, and `--settings`.

## Codex

Sources: https://learn.chatgpt.com/docs/agent-configuration/agents-md.md and https://github.com/openai/codex/blob/main/codex-rs/core/src/agents_md.rs

- **Global file**: `$CODEX_HOME/AGENTS.override.md` when it exists and is not empty, otherwise `$CODEX_HOME/AGENTS.md`. `CODEX_HOME` defaults to `~/.codex`.
- **Project root**: the nearest folder, from the start folder up, that holds an entry of `project_root_markers` (default `.git`, a folder or a file). No marker, or an empty list, means the start folder only.
- **One file per folder**, from the project root down to the start folder: `AGENTS.override.md`, else `AGENTS.md`, else each name in `project_doc_fallback_filenames`.
- **Empty files** are skipped and use none of the budget.
- **The limit**: `project_doc_max_bytes` (default 32 KiB) is one budget shared by all project files. The file that crosses it is cut; later files are dropped.
- **Trust**: a project marked `trust_level = "untrusted"` in `~/.codex/config.toml` loads no project file.
- **Below the start folder**: never read.
- **Imports**: none.

What the checker assumes:

- An empty `AGENTS.override.md` still takes its folder's slot, so `AGENTS.md` in that folder is not read. This follows the source ("the first of" the names), not a documented example.
- Reading `config.toml` needs Python 3.11 or newer (`tomllib`). On older Python the checker uses the defaults and says so in the report.
- A project that `config.toml` does not list yet: Codex asks whether to trust it on first launch. The map shows what loads after you trust it.
- Not read: profile files (`--profile`) and `-c` overrides.

## Gemini CLI

Sources: https://github.com/google-gemini/gemini-cli/blob/main/docs/cli/gemini-md.md, docs/reference/memport.md, docs/reference/configuration.md, and docs/cli/trusted-folders.md

- **Global file**: `~/.gemini/GEMINI.md` (one per configured file name).
- **Project files**: the configured file names in the start folder and each folder above it, up to the first folder holding a `context.memoryBoundaryMarkers` entry (default `.git`).
- **Below the start folder**: loaded just in time, when a tool touches that folder.
- **`context.fileName`**: a name or a list of names; default `GEMINI.md`. So Gemini CLI reads `AGENTS.md` only when you set it, for example `{"context": {"fileName": ["AGENTS.md", "GEMINI.md"]}}`.
- **Imports**: `@./file.md`, `@../file.md`, `@/absolute/file.md`, up to 5 levels, not inside code. The checker follows only these documented forms. A bare `@AGENTS.md` is reported as `gemini-import-form` and not counted as loaded.
- **Trust**: untrusted folders ignore `.gemini/settings.json`. `~/.gemini/trustedFolders.json` (or `GEMINI_CLI_TRUSTED_FOLDERS_PATH`) maps paths to `TRUST_FOLDER`, `TRUST_PARENT` (trusts the parent folder and everything in it), or `DO_NOT_TRUST`. `security.folderTrust.enabled: false` turns the check off.

What the checker assumes:

- With no boundary marker, only the start folder is read.
- A folder not listed in `trustedFolders.json`: Gemini CLI asks on first launch; the map applies your project settings.
- Not mapped: `context.includeDirectories`, `context.discoveryMaxDirs` (default 200), and system-wide settings files.

## OpenCode

Source: https://opencode.ai/docs/rules

- **Project**: `AGENTS.md` found from the start folder upward; when there is none, `CLAUDE.md` instead.
- **Global**: `~/.config/opencode/AGENTS.md`, else `~/.claude/CLAUDE.md`.
- **Switches**: `OPENCODE_DISABLE_CLAUDE_CODE=1` turns off both `CLAUDE.md` fallbacks; `OPENCODE_DISABLE_CLAUDE_CODE_PROMPT=1` turns off the `~/.claude/CLAUDE.md` one.
- **`instructions`** in `opencode.json`: file paths, globs such as `.cursor/rules/*.md`, or URLs.

What the checker assumes:

- The upward search stops at the git root, and every `AGENTS.md` it passes loads. The docs say only that the first match wins between `AGENTS.md` and `CLAUDE.md`.
- The switches are read from the shell that runs the checker.
- URLs are listed, not fetched. `instructions` in your global OpenCode config are counted, not mapped.

## Cursor

Source: https://cursor.com/docs/rules.md

- **Rules**: `.cursor/rules/**/*.mdc`. Frontmatter decides the type: `alwaysApply: true` loads always; `globs` attaches the rule to matching files; a `description` alone lets the agent decide; none of these makes it manual (you @-mention it). Plain `.md` files in that folder are ignored. Guidance: under 500 lines.
- **AGENTS.md** at the project root, and in subfolders, where it applies when you work in that subfolder together with the files above it.
- **User Rules and Team Rules** live in the app, not in files.

Unverified, so shown as such: the legacy `.cursorrules` file (not in the current docs), whether Cursor reads `CLAUDE.md`, and `.cursor/rules` folders nested below the project root.

## GitHub Copilot

Sources: https://docs.github.com/en/copilot/reference/custom-instructions-support and https://docs.github.com/en/copilot/how-tos/copilot-cli/customize-copilot/add-custom-instructions

- `.github/copilot-instructions.md` for the whole repo; `.github/instructions/**/*.instructions.md` with `applyTo` globs; the agent files `AGENTS.md`, `CLAUDE.md`, and `GEMINI.md`; and Copilot CLI personal files in `~/.copilot/`. `COPILOT_CUSTOM_INSTRUCTIONS_DIRS` adds folders.
- Copilot CLI follows `@relative/path` references inside these files. The checker notes them but does not expand them.
- The coding agent, the CLI, editor chat, and code review each read a different subset. Check GitHub's support table for the one you use.

## Aider

Source: https://aider.chat/docs/usage/conventions.html

- Aider loads no instruction file on its own. It reads the files listed under `read:` in `.aider.conf.yml`, or passed with `--read` or `/read`.
- The checker reads `.aider.conf.yml` in the repo root and the start folder. It does not see `~/.aider.conf.yml` or command-line flags.
