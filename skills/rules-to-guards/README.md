# Turn the rules your agent breaks into hooks

Finds the rules in AGENTS.md, CLAUDE.md, and GEMINI.md that your coding agent breaks, counts every break in your recent sessions, and turns each broken rule into a hook (a check your agent runs before each tool call) that blocks it, tested against those real breaks.

## What you get

A sample run. The numbers and names are invented; your agent prints its own. First the count (`rules.py count --rules rules.json --project .`):

```
**Your agent broke 2 of your 4 checkable rules 22 times in the last 30 days; `Use pnpm, never npm` leads with 19.**

Checked 9,614 tool calls in 188 sessions from the last 30 days (Claude Code 171, Codex 17), in sessions under `~/code/shop`.

| Rule | Breaks | Ran | Stopped | Sessions | Last | Source |
|---|---|---|---|---|---|---|
| use-pnpm | 19 | 17 | 2 | 7 | 2026-09-19 | `AGENTS.md:14` |
| no-generated-edits | 3 | 3 | 0 | 2 | 2026-09-12 | `AGENTS.md:31` |
| no-force-push | 0 | 0 | 0 | 0 | never | `AGENTS.md:29` |
| no-env-reads | 0 | 0 | 0 | 0 | never | `AGENTS.md:30` |

Examples, newest first (secrets masked, cut to 160 characters):
- use-pnpm, Claude Code, 2026-09-19 14:02 UTC, ran: `cd web && npm install left-pad`
- no-generated-edits, Codex, 2026-09-12 09:40 UTC, ran: `apply_patch dist/app.js`
```

Then the replay, before anything is installed (`rules.py test --rules rules.json --project .`):

```
**Your agent broke 2 of these 4 rules 22 times in the last 30 days. The hook blocks all 22 and none of your last 400 other calls.**
```

## Install

```
npx skills add https://github.com/RyanAlberts/best-of-Agent-Harnesses/tree/main/skills/rules-to-guards
```

In Claude Code, all ten at once:

```
/plugin marketplace add RyanAlberts/best-of-Agent-Harnesses
/plugin install harness-skills@agent-harnesses
```

Manual: copy `skills/rules-to-guards/` into your agent's skills folder: `~/.claude/skills/` (Claude Code), `~/.agents/skills/` (Codex, Gemini CLI, Cursor, and OpenCode all read it).

## Use it

Ask your agent: "My agent keeps using npm even though AGENTS.md says pnpm. Find the rules it breaks and block them with a hook."

The agent lists the candidate rules, drafts `rules.json` with you, counts the breaks, replays them through a fresh hook, and shows you the settings change before it writes anything. To run the steps yourself from your project folder (`rules.json` can live anywhere; pass its path):

```bash
python3 "$HOME/.claude/skills/rules-to-guards/scripts/rules.py" extract --repo .
python3 "$HOME/.claude/skills/rules-to-guards/scripts/rules.py" count --rules rules.json --project .
python3 "$HOME/.claude/skills/rules-to-guards/scripts/rules.py" test --rules rules.json --project .
python3 "$HOME/.claude/skills/rules-to-guards/scripts/rules.py" generate --rules rules.json --harness claude-code --project .
```

`generate` only shows the change until you add `--write`. `generate --uninstall --write` takes the hook out again.

## How it works

1. **Extract.** It reads the context files coding agents load (AGENTS.md, CLAUDE.md, GEMINI.md, Cursor rules, Copilot instructions, and the files they import from inside the project) and lists the lines with never, don't, should not, always, must, avoid, and prefer, each with a hint: a command, a path, a tool, or advice.
2. **Write rules.json.** A checkable rule is a regular expression on shell commands, a glob (a file pattern such as `dist/**`) on file paths, or a regular expression on tool names, plus the message the agent sees when blocked. The [checkable rules reference](references/checkable-rules.md) has tested patterns for common rules.
3. **Count.** It reads your Claude Code, Codex, Gemini CLI, and OpenCode sessions and checks every tool call. Shell commands are split the way a shell splits them, so `cd web && npm ci`, `sudo npm i`, and `bash -c 'npm test'` count as npm, while `echo "never use npm"`, a grep pattern, or a commit message does not. A forked session repeats the calls it copied; they count once.
4. **Test.** It builds the hook and replays up to 200 of the newest breaks per rule (each must be blocked) and up to 400 other recent calls (each must be allowed), running the hook the way your harness would. Only the hook runs, fed each call as JSON; the recorded commands never run again.
5. **Generate.** It writes `rules_guard.py`, one Python file with your rules inside, and merges a pre-tool hook entry into the settings of each harness (the agent program, such as Claude Code or Codex), keeping the hooks already there and the file's own formatting. A second run keeps the rules the hook already holds; `--replace` drops them and names each one. The hook blocks with exit code 2 and a message, and allows the call on any error of its own. It also prints matching permission rules as an optional extra layer; those match the start of a command only, so they miss wrapped forms. The [hook formats reference](references/hook-formats.md) covers each harness.

## Works with

| Harness | Counts breaks | Hook it writes | Notes |
|---|---|---|---|
| Claude Code | yes | PreToolUse in `.claude/settings.local.json` or `~/.claude/settings.json` | full; smoke-tested on macOS |
| Codex | yes | PreToolUse in `.codex/hooks.json` or `~/.codex/hooks.json` | trust the hook in `/hooks` before it runs |
| Gemini CLI | yes | BeforeTool in `.gemini/settings.json` or `~/.gemini/settings.json` | accept the new project hook when asked |
| Cursor | no; it keeps no usable transcripts | preToolUse in `.cursor/hooks.json` or `~/.cursor/hooks.json` | not verified on a real install |
| OpenCode | yes, best effort; not verified on a real install | none: OpenCode has plugins, not shell hooks | |

It needs Python 3.9 or newer and nothing else, on macOS or Linux, including WSL2. The hook runs with the `python3` it finds on your PATH.

## Limits

- It checks one tool call at a time. Rules about order ("run the tests before you commit") or style stay advice; [claim-check](../claim-check/) covers the tests-pass case.
- It sees the command the agent sends. A command built at run time, such as `eval "$CMD"`, a script the agent writes and then runs, text piped into a shell, or a here-string sent to one (`bash <<< "..."`), gets past it. A command with more than 5,000 parts is checked on its first 5,000.
- Path rules read a shell command's arguments and redirections, not the code inside a script such as `python3 -c "..."`, and they cannot tell a read from a write: `git diff dist` counts as touching `dist`. The hook cannot tell whether a `cd` ran, so it checks each relative path twice: from the folder the command starts in and from the folder after any `cd` before it. Either match blocks, so `cd src && echo x > ../dist/a.js` is caught, and so is `cd /tmp && echo x > dist/a.js`, a false alarm the hook accepts.
- A pattern is only as good as its fit. Read the examples `count` shows, and let `test` prove the hook before you install it.
- `count` includes breaks from before a rule was written. Use `--since` with the date the rule was added.
- The settings entry names the hook by its absolute path on this machine, so keep that settings file out of git or have each teammate run `generate`.
- The agent could edit or delete its own hook file unless your permission rules protect it.
- Windows outside WSL2 is not supported.

## Privacy

- **Read**: your context files, your agents' session transcripts on this machine, and the settings files it merges into.
- **Printed**: rule lines, excerpts of matching commands and paths, and a diff of each settings file it would change, all with secrets masked; excerpts stay on one line and are cut to 160 characters.
- **Sent**: nothing. It makes no network calls.
- **Written**: nothing without `--write`. With it, the hook file and one entry per harness settings file, never through a symlink that leads outside the project unless you pass `--follow-symlinks`; `--out` writes the report where you say.
- **The hook** reads each tool call from your harness, blocks or allows it, and keeps no record.

## Related

- [guardrail-tester](../guardrail-tester/): tests whether your permission rules and hooks stop dangerous commands.
- [agents-md-checker](../agents-md-checker/): shows which context files each agent loads and what gets cut.
- [AGENTS.md template](../../templates/agents-md/) and [One AGENTS.md for every coding agent](../../playbooks/one-agents-md-for-every-coding-agent.md): write the rules in the first place.
- [Safe Claude Code settings](../../templates/claude-code-safe-settings/): permission rules and a guard hook for common dangerous commands.
- Credit: [claudecode-rule2hook](https://github.com/zxdxjtu/claudecode-rule2hook) first turned CLAUDE.md rules into Claude Code hooks; this skill adds counting the breaks first and a replay test. The demand shows in Claude Code issues [#42796](https://github.com/anthropics/claude-code/issues/42796) (instructions ignored) and [#6354](https://github.com/anthropics/claude-code/issues/6354) (CLAUDE.md forgotten after compaction).
