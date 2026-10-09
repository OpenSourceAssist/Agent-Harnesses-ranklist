# Checkable rules: the rules.json format and tested patterns

A rule is **checkable** when one tool call, on its own, shows whether the rule was broken: the call runs a named command, touches a named file or folder, or uses a named tool. "Use pnpm, never npm" is checkable. "Keep functions short" is not, and neither is "run the tests before you commit", because that depends on the order of several calls. Checkable rules can become hooks; the rest stay as advice in the context file.

## rules.json

A JSON object with a `rules` list (a bare list works too). One entry per rule:

| Field | Required | Meaning |
|---|---|---|
| `id` | yes | 1 to 64 letters, digits, dots, dashes, or underscores. Reports and the hook's message use it. |
| `kind` | yes | `forbid_command`, `protect_path`, `forbid_tool`, or `advice` (kept as text, never counted). |
| `pattern` | yes, except for advice | A Python regular expression for `forbid_command` and `forbid_tool`; a glob for `protect_path`. |
| `tool` | no | Which calls the rule checks: `shell`, `edit`, `write`, `read`, or `any`, or several joined by `\|` such as `read\|shell`. Defaults: `shell` for commands, `edit\|write` for paths, `any` for tools. |
| `source` | no | Where the rule lives, such as `AGENTS.md:12`. |
| `text` | no | The rule as written. Headlines quote it. |
| `message` | no | What the agent reads when the hook blocks a call. Say what to do instead: "Use pnpm instead of npm." |

The kinds of call, which `tool` names: `shell` is any shell command (Claude Code Bash, Codex shell and exec, Gemini CLI run_shell_command, Cursor Shell). `edit` changes a file (Edit, MultiEdit, NotebookEdit, Codex apply_patch including new files, Gemini CLI replace, Cursor Delete). `write` creates or overwrites a whole file (Write, write_file). `read` reads one (Read, read_file, read_many_files). To protect a file from any change, use `edit|write`.

`rules.py` refuses a rule that would match everything: a regex that matches an empty command, or one that matches every sample in a list of ordinary commands such as `ls` and `git status`, and a glob that matches every sample path.

## How a command is matched

The hook does not match your regex against the raw command line alone. It splits the command the way a shell does and checks several views of it, so that a pattern anchored with `^` catches the command wherever it runs, and text that only mentions the command never counts:

1. The whole command, with the contents of quotes, heredoc bodies, and comments emptied. Use this view for rules that span a pipe, such as `\bpytest\b.*\|\s*tail\b`.
2. Each simple command: split on `;`, `&&`, `||`, `|`, `&`, newlines, and parentheses, with `$(...)` and backtick bodies added as commands of their own.
3. Each simple command unwrapped: leading `VAR=value` assignments, `sudo`, `env`, `timeout`, `nice`, `nohup`, `time`, `command`, `xargs`, and shell words such as `then` and `do` are dropped, and `/usr/local/bin/npm` becomes `npm`. The script inside `sh -c '...'`, `bash -lc "..."`, and `eval '...'`, and the command after `find -exec`, are checked as commands too.

Free text is removed before matching: the words of `echo` and `printf`, the search pattern of `grep`, `rg`, and similar tools, the script of `sed` and `awk`, and `git` and `gh` message values (`-m`, `-am`, `-sm`, `-m"..."`, `-F`, `--message`, `--grep`, `--title`, `--body`). Heredoc bodies are skipped too, including a commit message passed as `-m "$(cat <<'EOF' ... EOF)"`. When no part of the delimiter is quoted (`<<EOF`), the shell runs any `$(...)` or backtick command in the body, so the hook checks those commands. Any quoting (`<<'EOF'`, `<<E"OF"`, `<<\EOF`) keeps the body as text, and the heredoc still ends at the unquoted word (`EOF`). So `git commit -m "drop npm"` does not break a rule about npm, and a command chained after that heredoc is still checked. The delimiter is every character up to a blank or one of `;&|()<>`, so `<<EOF!` ends at a line `EOF!`. A heredoc ends only at a line that holds the delimiter alone; with `<<-`, that line may start with tabs. A heredoc fed to a shell (`bash <<'EOF'`, `sudo sh <<EOF`) is a script the shell runs, so the hook checks its body as commands, quoted or not. Paths and other arguments stay, so `cat .env` still matches `^cat\b.*\s\.env`. `command -v npm` only looks npm up, so it does not count as running npm.

A command with more than 5,000 simple commands is checked on its first 5,000.

## How a path is matched

`protect_path` globs follow gitignore habits:

- No slash: matches a file or folder name at any depth, in the project or outside it. `.env` matches `app/.env` but not `.env.example`; `*.lock` matches `yarn.lock` anywhere; `node_modules` matches everything inside any `node_modules` folder.
- With a slash: matches inside the project at any depth, and never outside it. `dist/**` matches `dist/app.js` and `packages/web/dist/app.js`, and the `dist` folder itself, but not `/elsewhere/dist/app.js`. A trailing slash (`dist/`) means the same.
- `./dist/**` matches only at the project root: the folder in `CLAUDE_PROJECT_DIR` or `GEMINI_PROJECT_DIR`, or Cursor's first workspace root, when the harness sets one; otherwise the working folder. `/etc/hosts` and `~/.zshrc` are absolute.
- `*` and `?` stay inside one folder; `**` crosses folders; `[abc]` and `[!abc]` are character sets.

A path rule with `shell` in its `tool` also checks the arguments and redirection targets (`> file`, `>> file`, `< file`) of each shell command. Shell path checks cannot tell a read from a write: any argument that names the path counts, so `cp .env.example .env`, `touch .env`, and `git diff dist` all count. The one exception is redirection: `>` and `>>` only write, so a rule whose `tool` has `read` but not `edit` or `write` skips their targets. Commands that only look at a name, such as `ls`, `stat`, `test`, and `[`, are skipped. Code inside a script (`python3 -c "open('.env')"`) is not read. The hook cannot tell whether a `cd` ran (`false && cd /tmp`, `cd /tmp &`), so it checks each relative path twice: from the folder the command starts in, and from the folder that the `cd`, `pushd`, and `popd` before it lead to. A match from either one blocks the call. So `cd src && echo x > ../dist/app.js` counts as writing `dist/app.js`, and `cd /tmp && echo x > dist/app.js` counts too: a false alarm the hook accepts so that it fails closed. A `cd` inside `( ... )`, `$(...)`, or a pipeline (`cd /tmp | true`) ends there, because the shell runs those in a subshell.

## Tested patterns

Copy the entries you need into your rules.json. The `examples` field is documentation: each `block` example breaks the rule and each `allow` example does not (a test in this skill's evals checks every one). A `Read`, `Edit`, `Write`, or `Tool` prefix marks a file or tool call; anything else is a shell command.

```json
{
  "rules": [
    {"id": "use-pnpm", "kind": "forbid_command", "text": "Use pnpm, never npm.",
     "pattern": "^npm(?:\\s|$)", "message": "Use pnpm instead of npm.",
     "examples": {"block": ["npm install", "cd web && npm ci", "bash -c 'npm test'", "CI=1 npm run build"],
                  "allow": ["pnpm install", "npx vitest", "echo 'do not use npm'", "grep -r npm package.json",
                            "npm-check-updates"]}},
    {"id": "no-force-push", "kind": "forbid_command", "text": "Never force-push.",
     "pattern": "^git\\b.*\\spush\\b.*(?:\\s--force(?:-with-lease)?\\b|\\s-[a-zA-Z]*f[a-zA-Z]*\\b|\\s\\+\\S)",
     "message": "Push without --force, or ask the user first.",
     "examples": {"block": ["git push --force", "git push -f origin main", "git -C app push origin +main",
                            "git push --force-with-lease"],
                  "allow": ["git push origin main", "git push -u origin feature-flag",
                            "git commit -m 'no push --force here'", "git commit -am 'no push --force here'",
                            "git commit -m\"no push --force here\""]}},
    {"id": "no-hard-reset", "kind": "forbid_command", "text": "Never discard work with git reset --hard.",
     "pattern": "^git\\b.*\\sreset\\b.*\\s--hard\\b", "message": "Use git stash, or ask the user first.",
     "examples": {"block": ["git reset --hard HEAD~1", "git -C app reset --hard"],
                  "allow": ["git reset HEAD notes.txt", "git reset --soft HEAD~1"]}},
    {"id": "no-recursive-force-delete", "kind": "forbid_command", "text": "Never delete folders with rm -rf.",
     "pattern": "^rm\\b(?=.*\\s(?:-[a-zA-Z]*[rR][a-zA-Z]*|--recursive)(?:\\s|$))(?=.*\\s(?:-[a-zA-Z]*f[a-zA-Z]*|--force)(?:\\s|$))",
     "message": "Delete the files one by one, or ask the user first.",
     "examples": {"block": ["rm -rf build", "rm -r -f build", "sudo rm -fr tmp/cache",
                            "find . -name cache -exec rm -rf {} +"],
                  "allow": ["rm build.log", "rm -r build", "rm -f build.log"]}},
    {"id": "commit-identity", "kind": "forbid_command", "text": "Commit only as the GitHub noreply address.",
     "pattern": "(?:^git\\b.*(?:\\s-c\\s+user\\.email=|\\sconfig\\b.*\\suser\\.email\\s|\\scommit\\b.*\\s--author[= ])|(?:^|\\s)GIT_(?:AUTHOR|COMMITTER)_EMAIL=)[^@]*@(?!users\\.noreply\\.github\\.com\\b)[\\w.-]+\\.[a-z]{2,}",
     "message": "Commit with your users.noreply.github.com address.",
     "examples": {"block": ["git -c user.email=me@example.com commit -m fix",
                            "git config --global user.email me@example.com",
                            "GIT_AUTHOR_EMAIL=me@example.com git commit -m fix",
                            "git commit --author='Me <me@example.com>' -m fix"],
                  "allow": ["git -c user.email=1+me@users.noreply.github.com commit -m fix",
                            "git log --author=me@example.com", "git config user.email",
                            "git commit -m 'mail me@example.com'"]}},
    {"id": "no-env-reads", "kind": "protect_path", "tool": "read|shell", "text": "Never read .env files.",
     "pattern": ".env", "message": "Ask the user for the value you need.",
     "examples": {"block": ["Read /repo/.env", "cat .env", "cat config/.env", "sudo cat .env", "wc -l < .env",
                            "cp .env.example .env", "touch .env", "docker compose --env-file .env up",
                            "python3 -m venv .env"],
                  "allow": ["Read /repo/.env.example", "cat .env.example", "ls -la .env", "echo KEY=1 >> .env"]}},
    {"id": "no-generated-edits", "kind": "protect_path", "tool": "edit|write",
     "text": "Never edit generated files in dist/.", "pattern": "dist/**",
     "message": "Change the generator instead, then rebuild.",
     "examples": {"block": ["Edit /repo/dist/app.js", "Write /repo/dist/new.js", "Edit /repo/packages/web/dist/app.js"],
                  "allow": ["Read /repo/dist/app.js", "Edit /repo/src/app.js", "Edit /repo/docs/dist.md"]}},
    {"id": "no-dist-anywhere", "kind": "protect_path", "tool": "edit|write|shell",
     "text": "Never touch dist/, not even from the shell.", "pattern": "dist/**",
     "message": "Change the source and let the build write dist/.",
     "examples": {"block": ["Edit /repo/dist/app.js", "echo x > dist/app.js", "tsc --outDir=dist", "git diff dist"],
                  "allow": ["Read /repo/dist/app.js", "tsc --outDir=build", "ls dist"]}},
    {"id": "no-lockfile-edits", "kind": "protect_path", "tool": "edit|write",
     "text": "Never edit lockfiles by hand.", "pattern": "*.lock",
     "message": "Change the manifest and let the package manager update the lockfile.",
     "examples": {"block": ["Edit /repo/yarn.lock", "Write /repo/crates/core/Cargo.lock"],
                  "allow": ["Edit /repo/src/lock.py", "Read /repo/yarn.lock"]}},
    {"id": "no-slack-posts", "kind": "forbid_tool", "text": "Never post to Slack.",
     "pattern": "^(?:mcp__slack__|mcp_slack_)", "message": "Draft the message and show it to the user instead.",
     "examples": {"block": ["Tool mcp__slack__post_message", "Tool mcp_slack_post_message"],
                  "allow": ["Tool mcp__github__create_issue", "Tool mcp_github_create_issue"]}}
  ]
}
```

## Regex pitfalls

- **Anchor command rules with `^`.** `^npm(?:\s|$)` matches the command npm in every view above; plain `npm` also matches `pnpm` and `npm-check-updates`.
- **End a command name with `(?:\s|$)`, not `\b`.** `\b` counts a dash as a word end, so `^npm\b` matches `npm-check-updates`.
- **Allow for options before a subcommand.** `^git\s+push` misses `git -C app push`; `^git\b.*\spush\b` catches it.
- **Escape for JSON.** Each regex backslash is written twice inside a JSON string: `\\s` in the file is `\s` in the regex.
- **Matching is case-sensitive.** Start with `(?i)` to ignore case.
- **Avoid nested repeats** such as `(a+)+`, and two `.*` in one pattern: each is slow on a long command, and two `.*` take time that grows with the square of the command's length. A slow regex makes the hook time out, and a timed-out hook lets the call through. One `.*`, or `[^;|&]*`, is enough for most rules.
- **Write for every Python version.** The hook runs on whatever `python3` the harness finds, as old as 3.9. `rules.py` refuses atomic groups `(?>...)` and possessive quantifiers such as `++` (errors before Python 3.11), and a global flag such as `(?i)` anywhere but the start (an error from Python 3.11 on).
- **Name MCP tools per harness.** Claude Code and Codex call them `mcp__server__tool`, Gemini CLI `mcp_server_tool`, and Cursor `MCP:tool` (the tool name only). When Gemini CLI is a target too, match both forms: `^(?:mcp__slack__|mcp_slack_)`.
- **Test before you install.** `rules.py count` shows examples of what each pattern matched in your sessions, and `rules.py test` replays them through the hook.

## What stays advice

- Style and judgment ("prefer small functions", "write clear names").
- Order and sequence ("run the tests before you commit", "read a file before you edit it"). A Stop hook is the right tool for "finish only after the tests pass"; see [claim-check](../../claim-check/).
- Rules about content inside files ("no console.log in committed code"): a linter or a pre-commit git hook checks those better than an agent hook.
- Commands built at run time: `eval "$CMD"`, a script the agent writes and then runs, or text piped into `sh`. The hook sees the outer command only.
