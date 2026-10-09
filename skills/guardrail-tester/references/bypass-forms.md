# Why rules miss these forms

A permission rule matches the text of a command, not the program it starts. Claude Code's own
[permissions page](https://code.claude.com/docs/en/permissions#bash-rule-limits) says a Bash deny or
ask rule "isn't a security boundary around the program". Claude Code
[issue #30519](https://github.com/anthropics/claude-code/issues/30519) collects the reports behind
that: deny rules slipping past multi-line commands and reordered flags, and community guard hooks
built as workarounds without tests against hostile input. This page explains each form in
`scripts/battery.json` and what catches it. Checked 2026-09-28.

The battery spells each form with stand-in names (a missing `./guardrail-tester-probe/` folder, the
remote `probe-remote`, the branch `probe-branch`, and hosts ending in `.invalid`) so that a command
does nothing if a hook runs it. The examples below use everyday names.

Three layers can stop a command, and each sees it differently:

- **Permission rules** compare the command text with a pattern, after the harness splits chains
  and strips a few wrappers. They miss any spelling their pattern does not name.
- **A PreToolUse hook** reads the whole command line and can test it however it likes, so one
  regular expression covers many spellings. It still sees only text: a script that opens a file
  itself, or a downloaded program, runs whatever it contains.
- **The sandbox** limits what a running command can touch, whatever its spelling. It is the only
  layer that holds against a determined attacker; `sandbox-check` measures it.

## Flags and spelling

| Form | Example | Why a prefix rule misses it | What catches it |
|---|---|---|---|
| Flags in another order | `rm -fr build`, `rm -r -f build`, `rm --recursive --force build` | `Bash(rm -rf *)` names one spelling | hook check rm-recursive, or one deny rule per spelling |
| Flag after the arguments | `git push origin main --force` | the rule starts with `git push --force` | hook check git-force-push |
| Force with no flag | `git push origin +main` | a plus sign before the branch forces the push | hook check git-force-push |
| Another flag with the same effect | `git push --force-with-lease`, `--mirror`, `--delete`, `origin :main` | each needs its own rule | hook check git-force-push |
| Option before the subcommand | `git -C . push --force`, `git -c push.default=current push` | the text no longer starts with `git push` | hook check git-force-push |
| Quoted subcommand | `git 'push' --force origin main` | Claude Code keeps quotes when matching | hook check git-force-push |

The last three rows come from the Claude Code permissions page, which lists them as forms a
`Bash(git push *)` rule does not stop.

## Paths, escapes, and wrappers

| Form | Example | Why a prefix rule misses it | What catches it |
|---|---|---|---|
| Full path to the program | `/bin/rm -rf build`, `/usr/bin/sudo whoami` | the first word is a path | hook checks rm-recursive and privilege |
| Backslash before the name | `\rm -rf build` | the text starts with a backslash | hook check rm-recursive |
| A wrapper the harness strips | `command rm`, `timeout 60 git push --force`, bare `xargs rm -rf` | nothing: Claude Code strips these, so rules still match | deny rules work here |
| A wrapper it does not strip | `env GIT_TRACE=1 git push -f`, `xargs -0 rm -rf`, `docker exec app rm -rf /data` | the rule sees `env`, `xargs`, or `docker` first | hook checks |
| A variable assignment in front | `GIT_TRACE=1 git push -f origin main` | nothing for Claude Code deny rules, which match past it | deny rules work here |
| A shell inside a string | `sh -c 'rm -rf build'`, `bash -c '...'`, `eval "rm -rf build"` | the rule sees `sh`, `bash`, or `eval` | hook checks, which read inside the string |

## Chains and nesting

Claude Code splits commands at `&&`, `||`, `;`, `|`, `|&`, `&`, and newlines, and checks commands
inside `( )`, `$( )`, backticks, and loop bodies against deny and ask rules. So `npm test && git push
--force`, a second line, `(cd build && rm -rf .)`, `echo "$(rm -rf build)"`, and `for d in a b; do rm
-rf "$d"; done` are all caught by a matching deny rule in Claude Code. Other harnesses differ:

- **Codex** splits a `bash -lc` script only when it is plain words joined by `&&`, `||`, `;`, or `|`.
  A redirection, a variable, a glob, or a substitution keeps the whole script as one argument
  list, so `rm -rf build > /dev/null` never meets a `prefix_rule(["rm"])`.
- **Gemini CLI** splits at `&&`, `||`, and `;`, then checks each part.
- **Cursor** matches only the first word of a command in `Shell(...)` rules.

## Code that the rule never sees

| Form | Example | Why a prefix rule misses it | What catches it |
|---|---|---|---|
| Base64-encoded command | `echo cm0gLXJmIGJ1aWxk \| base64 -d \| sh` <!-- skillscan:allow --> | the dangerous text is encoded | hook check decoded-exec |
| Download run by a shell | a download piped into `sh`, `bash <(curl ...)`, or saved and run | the rule would have to name `sh` | hook checks pipe-to-shell and download-then-run |
| An interpreter one-liner | `python3 -c "print(open('.env').read())"`, Python running downloaded code | the file name or URL sits inside program text | hook checks secret-files and download-then-run |

## Secrets outside the file rules

Claude Code applies `Read(...)` deny rules to its Read tool, to `<` redirections, and to the file
commands it recognizes (its docs name `cat`, `head`, `tail`, `sed`, and `tee` as examples; the tester
also counts `grep`, `wc`, `diff`, and `stat` for the files they name). The rules do not
reach `grep -r API_KEY .` (it names no file), `cp .env /tmp/`, `base64 .env`, an upload such as
`curl -d @.env`, a command that prints the environment, or a script that opens the file itself. The hook checks secret-files,
grep-secrets, env-dump, and upload catch those spellings.

## Writes to files other programs run

Git runs `.git/hooks/*`, the shell runs startup files, and each agent reads its own settings file.
Claude Code asks before writing its protected paths (`.git`, `.claude`, `.zshrc`, and others) in
every mode except `bypassPermissions`, but a prompt is easy to approve by reflex. A deny rule such
as `Edit(.git/**)`, `Edit(.claude/settings*.json)`, or `Edit(~/.zshrc)` blocks the file tools
outright (the narrower `.claude` pattern keeps skills and agents editable), and the hook check
tamper-files covers shell writes. A deny rule for `.env` is written as `Read(.env)`, `Read(.env.*)`,
`Read(!.env.example)`, and `Read(!.env.sample)`, so example files stay readable. The Codex config and the macOS login items folder are
not on Claude Code's protected list, so only a rule or a hook stops those writes.

## The fixes the tester suggests

- A **deny rule** only when the simulation shows it blocks that case and it leaves a list of
  everyday commands (such as `git push origin main` or `rm notes.txt`) and files (such as
  `.env.example`, `.envrc`, and `.claude/skills/`) alone. A rule that would block normal work is
  left out, and the hook check is the fix instead.
- A **hook check** as an extended regular expression that a hook can test with `grep -Eq`, the way
  `templates/claude-code-safe-settings/.claude/hooks/guard.sh` in this repository does. For a
  maintained guard with many more patterns, use
  [dcg](https://github.com/Dicklesworthstone/destructive_command_guard) and test it here.
