#!/usr/bin/env bash
# PreToolUse hook for Claude Code: blocks dangerous shell commands anywhere in
# the command line, including chained ones (`ls && git push --force`), which
# permission rules can miss. Exit 2 blocks the call and shows stderr to Claude.
# Needs jq or python3 to read the hook's JSON input.

input="$(cat)"
if command -v jq >/dev/null 2>&1; then
  cmd="$(printf '%s' "$input" | jq -r '.tool_input.command // ""')"
else
  cmd="$(printf '%s' "$input" | python3 -c 'import json,sys; print(json.load(sys.stdin).get("tool_input",{}).get("command",""))')"
fi

block() {
  echo "Blocked by .claude/hooks/guard.sh: $1. Ask the user to run it themselves if it is really needed." >&2
  exit 2
}

# Destructive git
# git, then global options such as -C <dir> or -c <key=value>, then a subcommand that may be quoted.
git_cmd='git([[:space:]]+-[^[:space:];&|]+([[:space:]]+("[^"]*"|'"'"'[^'"'"']*'"'"'|[^-[:space:];&|"'"'"'][^[:space:];&|]*))?)*[[:space:]]+["'"'"']?'
printf '%s' "$cmd" | grep -Eq "$git_cmd"'push["'"'"']?([[:space:]][^;&|]*)?(--force|[[:space:]]-f([[:space:]]|$)|--mirror|--delete|[[:space:]]:[^[:space:]]|[[:space:]]["'"'"']?[+][^[:space:]])' && block "force-push or remote delete"
printf '%s' "$cmd" | grep -Eq 'git[[:space:]]+reset[[:space:]]+--hard' && block "git reset --hard discards work"
printf '%s' "$cmd" | grep -Eq 'git[[:space:]]+clean[[:space:]]+-[a-zA-Z]*f' && block "git clean deletes untracked files"
printf '%s' "$cmd" | grep -Eq 'git[[:space:]]+(checkout|restore)[[:space:]]+(--[[:space:]]+)?\.([[:space:]]|$)' && block "discarding all local changes"
printf '%s' "$cmd" | grep -Eq 'git[[:space:]]+branch[[:space:]]+-D' && block "force-deleting a branch"

# Destructive files and privilege
# rm: -r and -f in one flag (-rf, -fr) or split (-r -f, -R --force), in any order.
printf '%s' "$cmd" | grep -Eq '(^|[^[:alnum:]_.-])rm[[:space:]]([^;&|]*[[:space:]])?(-[a-zA-Z]*([rR][a-zA-Z]*f|f[a-zA-Z]*[rR])|(-[a-zA-Z]*[rR][a-zA-Z]*|--recursive)[[:space:]]([^;&|]*[[:space:]])?(-[a-zA-Z]*f|--force)|(-[a-zA-Z]*f[a-zA-Z]*|--force)[[:space:]]([^;&|]*[[:space:]])?(-[a-zA-Z]*[rR]|--recursive))' && block "recursive forced delete"
printf '%s' "$cmd" | grep -Eq '(^|[^[:alnum:]_.-])find[[:space:]]([^;&|]*[[:space:]])?(-delete([^[:alnum:]_-]|$)|-(exec|execdir|ok|okdir)[[:space:]]+([^[:space:];&|]*/)?rm[[:space:]])' && block "find that deletes files"
# sudo only where a command starts: after a separator, a find -exec, or wrappers such as env, nohup, or xargs.
printf '%s' "$cmd" | grep -Eq '(^|[;&|({!`]|[[:space:]]-(exec|execdir|ok|okdir))[[:space:]]*(([[:alpha:]_][[:alnum:]_]*=[^[:space:]]*|env|command|builtin|exec|nohup|time|nice|timeout|stdbuf|xargs|then|do|else|elif|-[^[:space:];&|]*|[0-9][^[:space:];&|]*)[[:space:]]+)*([^[:space:];&|]*/)?sudo[[:space:]]' && block "sudo"

# Secrets: Read() deny rules cover cat, head, tail, sed, and tee, but not every
# reader, and not a script that opens the file itself.
# Example files (.env.example, .env.sample, .env.template) are not secrets.
scan="$(printf '%s' "$cmd" | sed -E 's/\.env\.(example|sample|template)//g')"
printf '%s' "$scan" | grep -Eq '(cat|less|more|head|tail|grep|awk|sed|scp|base64|xxd)[^;&|]*(\.env([.[:space:]]|$)|\.pem|id_rsa|id_ed25519|\.aws/credentials|\.ssh/)' && block "reading secrets through the shell"
printf '%s' "$scan" | grep -Eq '(^|[^[:alnum:]_.-])(python[0-9.]*|node|ruby|perl)[[:space:]].*(\.env([^[:alnum:]_-]|$)|\.pem|id_rsa|id_ed25519|\.aws/credentials|\.ssh/)' && block "reading secrets with a one-line script"
printf '%s' "$cmd" | grep -Eq '(^|[;&|[:space:]])(printenv|env)([[:space:]]*$|[[:space:]]*[;&|])' && block "dumping environment variables"

# Remote code
printf '%s' "$cmd" | grep -Eq '(curl|wget)[^;&]*\|[[:space:]]*(sudo[[:space:]]+)?(ba|z)?sh([^[:alnum:]_.-]|$)' && block "piping a download into a shell"
printf '%s' "$cmd" | grep -Eq 'base64[[:space:]][^;&|]*-(d|D|-decode)[^;&]*\|[[:space:]]*(sudo[[:space:]]+)?(ba|z|da)?sh([[:space:]]|$)' && block "piping decoded text into a shell"
printf '%s' "$cmd" | grep -Eq '(curl|wget)[[:space:]][^|]*(;|&&|\|\|)[[:space:]]*(sudo[[:space:]]+)?(((ba|z|da)?sh|source|\.)[[:space:]]+[^-[:space:]]|chmod[[:space:]][^;&|]*[+]x)' && block "downloading a file and then running it"

# Files that run later or loosen these settings: git hooks, shell startup files,
# launch agents, and Claude Code's own settings and hooks. Reading them is fine.
protected='(\.git/hooks|Library/Launch(Agents|Daemons)|\.claude/(settings[^/[:space:]]*\.json|hooks)|\.(zshrc|zshenv|zprofile|zlogin|bashrc|bash_profile|bash_login|profile))'
printf '%s' "$cmd" | grep -Eq '>[[:space:]]*[^[:space:];&|]*'"$protected"'([^[:alnum:]_.-]|$)' && block "redirecting into a git hook, shell startup file, launch agent, or Claude Code setting"
printf '%s' "$cmd" | grep -Eq '(^|[^[:alnum:]_.-])(tee|touch|chmod|chown|truncate|dd|mv|rm|unlink)[[:space:]][^;&|]*'"$protected"'([^[:alnum:]_.-]|$)' && block "changing a git hook, shell startup file, launch agent, or Claude Code setting"
printf '%s' "$cmd" | grep -Eq '(^|[^[:alnum:]_.-])(cp|ln|install|rsync)[[:space:]][^;&|]*[[:space:]]["'"'"']?[^[:space:];&|]*'"$protected"'(/[^[:space:];&|]*)?["'"'"']?[[:space:]]*($|[;&|)])' && block "copying into a git hook, shell startup file, launch agent, or Claude Code setting"
printf '%s' "$cmd" | grep -Eq '(^|[^[:alnum:]_.-])sed[[:space:]]([^;&|]*[[:space:]])?(-[a-zA-Z]*[iI]|--in-place)[^;&|]*'"$protected"'([^[:alnum:]_.-]|$)' && block "editing a git hook, shell startup file, launch agent, or Claude Code setting"

exit 0
