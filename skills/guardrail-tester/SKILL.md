---
name: guardrail-tester
description: >-
  Guardrail tester that checks whether the permission rules and PreToolUse
  hooks already set up in Claude Code, Codex, Gemini CLI, OpenCode, or Cursor
  stop a battery of dangerous commands, including wrapped, reordered, and
  full-path forms that slip past prefix rules, and measures prompt friction
  on the latest tool calls. Use when the user asks whether deny rules or
  hooks block force pushes, rm -rf, secret reads, or downloads piped into a
  shell; wants to test or audit guardrails, or find a bypass in permission
  settings or exec policy; asks how many prompts or blocks the rules cause;
  or just installed a guard hook and wants proof it holds. Local only, no
  network; executes the user's own hook commands only with their yes.
license: MIT
compatibility: "Python 3.9+, standard library only. Reading Codex config.toml and Gemini CLI policy files needs Python 3.11+. Makes no network calls; the only commands it runs are the user's own hook commands, fed test JSON, and only with --run-hooks after the user says yes."
metadata:
  author: "Ryan Alberts"
  version: "1.0.0"
  source: "https://github.com/RyanAlberts/best-of-Agent-Harnesses"
---

# Guardrail tester

Permission rules match the text of a command, so `git push origin +main`, `rm -r -f build`, and
`bash -c '...'` can walk past a deny rule written for the plain form. This skill checks the rules
and hooks the user already has against about 90 dangerous commands in those forms, then replays
their recent tool calls to count how often the rules would ask or block. The user gets a headline
("Your Claude Code guardrails block 58 of 90 dangerous commands outright. 24 more stop at a
prompt..."), every miss with a tested fix, and their friction numbers. Everything is read and
simulated locally; nothing is sent anywhere, and it runs the user's hook commands only after they
say yes.

## When to use

- The user asks whether their deny rules, ask rules, or hooks really stop a dangerous command.
- The user wants to test, audit, or find bypasses in their guardrails or permission settings.
- The user asks how many prompts their rules cause, or wants fewer without losing safety.
- The user just installed a guard (a hook, dcg, the repo's safe-settings template) and wants proof.

## When not to use

- What the agent can reach on this machine (secret files, Docker, sudo): use `sandbox-check`.
- Turning a rule from AGENTS.md or CLAUDE.md into a hook: use `rules-to-guards`.
- Stopping a session that loops or overspends: use `runaway-guard`.
- Building a guard from scratch: recommend dcg or `templates/claude-code-safe-settings/` in this
  repository, then test it here.

## What the tester runs

Say this to the user in short before the first run with hooks. It is the whole contract.

- **The battery commands never run.** Each is only text inside the JSON a hook reads on stdin.
  The tester never runs them; a hook that runs or forwards its input would. Each battery command
  is also written to do nothing if run: its targets sit under a missing `./guardrail-tester-probe/`
  folder, its remotes and branches (`probe-remote`, `probe-branch`) are made up, and its hosts end
  in `.invalid`, a name reserved for addresses that resolve nowhere.
- **Rules are simulated** from each harness's documented matching rules, so results are labeled
  "simulated". `references/harness-rules.md` says what each harness simulation covers.
- **It runs your hook commands only after you say yes** (the `--run-hooks` flag). Each matching
  PreToolUse hook command then runs once per battery case, one run at a time, with a 10-second
  limit, from the folder its harness uses (usually the project), with the session's environment
  variables plus `CLAUDE_PROJECT_DIR`. A hook that writes a log or keeps state records those test
  calls. A hook that times out twice is not run again.
- **Replay** reads session transcripts on this machine, read-only, masks secrets in its output,
  and checks the rules only. With `--replay-hooks` (which needs `--run-hooks`) it also runs this
  project's hooks on replayed calls from this project. Calls from other project folders are
  checked against their own rules; their hooks never run.

`scripts/battery.json` holds the dangerous commands on purpose: they are the test. Each line
carries the marker `skillscan:allow`, which tells this repository's security scanner that the line
is test data, not a command the skill runs.

## Steps

`<skill-dir>` means the folder that holds this SKILL.md (Claude Code shows it as the skill's base
directory). Run every command from the user's project folder, and give each Bash call a 10-minute
timeout (600000 ms): hooks run one at a time, so a slow hook makes the run long.

1. **Run the rules-only test**, which reads settings and runs no hook:

   ```bash
   python3 "<skill-dir>/scripts/test_guards.py" --project . --harness claude-code
   ```

   Add `--harness` for the harness you are running in (`claude-code`, `codex`, `gemini-cli`,
   `opencode`, or `cursor`) unless the user asks about all; without it the tester covers every
   harness with settings on this machine. Done when the output starts with a bold headline, or
   you have told the user the error (exit code 2 means a bad argument or an unreadable battery
   file).

2. **Tell the user what was found**: the settings files and every hook command from the report's
   "What was found" section, quoted exactly. Done when the user has seen the list.

3. **Read each hook script, then ask before running hooks.** Open the script each hook command
   runs. If a script runs, evaluates, or sends its input anywhere (`eval`, `bash -c "$cmd"`, a
   `curl` with the input, a queue), or has other side effects such as writing a log, tell the user
   exactly that and recommend testing without hooks. Otherwise name the hook commands and say each
   will get test JSON for about 90 battery calls. On a clear yes, run this with a 10-minute Bash
   timeout:

   ```bash
   python3 "<skill-dir>/scripts/test_guards.py" --project . --harness claude-code --run-hooks --replay 500
   ```

   Add `--replay-hooks` only when the user also agrees that this project's hooks see up to 500 of
   their recent real calls. When the user declines, or a hook has side effects, run the same
   command without `--run-hooks`. Done when the headline matches the choice: it starts "Your ...
   guardrails block" after hooks ran, and "Without running your N hooks" when hooks exist and were
   skipped.

4. **Pick the fixes.** For each row in "Misses, worst first", take the Fix column: a deny rule
   the simulation proved catches that case and leaves everyday commands and files such as
   `.env.example` alone, or a named hook check (an extended regular expression printed below the
   tables). Use `references/bypass-forms.md` to explain why a form slips. Done when every miss you
   report has its fix or the note "use the sandbox".

5. **Offer the change.** Draft the settings edit or hook lines as a diff against the user's file,
   show it, and apply it only after a clear yes. Then rerun step 3 to show the new count.

## Read the results

- **Headline**: how many battery commands are blocked outright; how many more stop at a prompt,
  with the number that ask only because of the permission mode in parentheses; how many run
  without asking, with the worst example. For Codex: how many its rules block and how many its
  sandbox stops or asks about. The replay friction comes last when replay ran.
- **Table columns**: Blocked; Not blocked; Asks (rule, hook, or check), prompts that still appear
  in auto mode; Asks (mode only), prompts that auto and bypassPermissions modes drop; Runs without
  asking, which counts allow rules, the read-only set, sandbox auto-allow, and auto mode's
  classifier.
- **Mode**: with no `defaultMode` in the settings, the tester simulates auto mode, the built-in
  default on Claude Code 2.1.283 and later, and adds a "Claude Code in Manual mode" row.
  `--mode` simulates another mode. Plan mode lasts only until a plan is approved.
- **Should**: `block` cases count as stopped only when blocked outright, because people approve
  prompts by reflex. `ask` cases count when they prompt or are blocked. `--fail-on-miss` uses this
  count.
- **What happens**: `blocked`, `asks first`, `runs without asking`, `left to the auto-mode
  classifier`, or `unknown`. The part in parentheses names the layer that decided: a hook, a rule,
  a built-in check (protected paths, critical-path removals, the read-only command set), the
  mode, or the sandbox. With a sandbox on, a command that needs the network or writes outside the
  project shows `asks first (needs network)` or `stopped by the sandbox`.
- **Unknown**: a hook did not answer within the tester's limit while the harness would wait
  longer. Unknown cases are not counted as misses; raise `--hook-timeout` to settle them.
- **Misses** are listed worst first: runs without asking, then asks only because of the mode,
  then asks because of a rule, hook, or check.
- **Friction**: replayed calls that would ask or be blocked, each simulated in the permission mode
  its session recorded, and real dangerous calls (matched by the hook checks) that ran and would
  still not be blocked as expected. A long command shows as the hook checks it matches.
- **Configuration problems**: for example a hook that exits 1 (the call goes ahead), a hook with
  no timeout, a `Bash` matcher that Cursor never fires, a Codex hook nobody trusted in `/hooks`,
  a shadowed OpenCode deny, or bypass mode left available.
- **Notes on single cases**: where Claude Code might apply a Read rule the docs do not extend to a
  command. `--json` has every case with `verdict`, `bucket`, `decided_by`, `rules_verdict`, and
  `fix`, plus `hook_checks`, every hook check by name.

## Report to the user

1. The headline, verbatim, in bold.
2. The misses table, cut to its first eight rows (the report lists the worst first), each with its
   fix.
3. The friction line and up to three dangerous replayed calls, quoted as the report prints them.
4. Configuration problems, high first, one line each.
5. The two or three fixes that close the most misses, then the offer from step 5. Point to dcg
   and `templates/claude-code-safe-settings/` rather than writing a new guard.

## Files

- `scripts/test_guards.py`: the tester. Flags: `--harness`, `--project`, `--run-hooks`,
  `--no-hooks` (the default), `--hook-workers N` (default 1), `--replay N`, `--replay-hooks`,
  `--since DAYS`, `--mode`, `--battery`, `--hook-timeout`, `--home`, `--json`, `--out`,
  `--fail-on-miss` (exit 1 when a case is not blocked as expected).
- `scripts/battery.json`: the dangerous commands, one case per line, each with what it needs to
  do its harm (`network`, `write-outside`, `write-git`, `write-project`).
- `scripts/claude_rules.py`, `scripts/harness_rules.py`: rule simulation per harness.
- `scripts/hook_runner.py`: runs one hook command with test JSON and reads its decision.
- `scripts/shell_split.py`: splits a command line into the commands it runs.
- `scripts/transcripts.py`: reads session transcripts for replay (shared copy).
- `scripts/safe.py`: masks secrets and puts text from settings, hooks, and transcripts in inline
  code for the report (shared copy).
- `references/bypass-forms.md`: each form in the battery and why rules miss it.
- `references/harness-rules.md`: matching rules per harness, with sources and the date checked.
