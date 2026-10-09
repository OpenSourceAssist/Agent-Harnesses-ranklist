# Skills for agent harnesses

A skill is a folder your coding agent loads when a task calls for it: a `SKILL.md` file that tells the agent what to do, plus the scripts it runs. The ten skills here check your own agent setup and hand back a result with a number in it that you can verify: what your agent can reach, which of its "tests pass" claims had no passing run behind them, where your tokens went, which guardrails let a dangerous command through.

They work in Claude Code, Codex, Cursor, Gemini CLI, OpenCode, and any agent that reads `SKILL.md` files.

## Skills

| Skill | What you get |
|---|---|
| [harness-test-drive](harness-test-drive/) | Claude Code, Codex, and Gemini CLI run tasks taken from your own git history, scored by your own tests, with the pass rate, minutes, and cost of each. |
| [agents-md-checker](agents-md-checker/) | Which instruction files each coding agent really loads from your repo, what gets cut, and whether the commands in them still work. |
| [tool-design-checker](tool-design-checker/) | A grade for each MCP tool the way a model reads it, the token load of every server you have installed, and the tools that collide across servers. |
| [sandbox-check](sandbox-check/) | What your agent can reach right now: secret files, git hooks, shell startup files, Docker, sudo, and the network, compared with what your sandbox settings claim. |
| [guardrail-tester](guardrail-tester/) | How many of about 90 dangerous commands your permission rules and hooks block, how many only ask, and which ones run without asking. |
| [runaway-guard](runaway-guard/) | A hook that stops a live session when the agent loops, keeps failing, or spends past a dollar cap you set. |
| [claim-check](claim-check/) | How often your agent said tests passed without a passing run to back it up, and whether the current change weakened your tests. |
| [rules-to-guards](rules-to-guards/) | The AGENTS.md rules your agent keeps breaking, counted in your recent sessions, and turned into tested hooks. |
| [session-waste-report](session-waste-report/) | Where your agent wastes tokens, money, and time, such as cache rebuilds after pauses and oversized tool results, with one fix for each. |
| [regression-finder](regression-finder/) | Your sessions split by harness version, model, or week, with the point where your agent started working differently. |

## Install

One skill, in any agent:

```sh
npx skills add https://github.com/RyanAlberts/best-of-Agent-Harnesses/tree/main/skills/sandbox-check
```

See all ten and pick:

```sh
npx skills add RyanAlberts/best-of-Agent-Harnesses --list
```

All ten in Claude Code:

```
/plugin marketplace add RyanAlberts/best-of-Agent-Harnesses
/plugin install harness-skills@agent-harnesses
```

By hand: copy a skill folder into `~/.claude/skills/` (Claude Code) or `~/.agents/skills/` (Codex, Gemini CLI, Cursor, and OpenCode all read it). If your agent has this repo's [MCP server](../mcp/), it can install one for you with `get_skill`.

## What they have in common

- **A script does the checking.** The agent runs it, reads the result, and explains it. Python 3.9 or newer and the standard library, so there is nothing else to install.
- **Your data stays on your machine.** The skills read your files, settings, and session logs where they are. Anything that changes a file, installs a hook, starts a program, or spends money is shown to you first and waits for your yes.
- **No network by default.** Three skills reach the network, and only when you ask: harness-test-drive runs the coding agents you choose, which call their model providers; sandbox-check tests outbound connections with `--network`; tool-design-checker starts your MCP servers with `--launch` or contacts remote ones with `--remote`.
- **Untrusted text stays inert.** Commands, file names, and log text are masked for secrets and kept on one line before they reach a report, so a repository cannot slip instructions into what your agent tells you.
- **One sentence first.** Every report leads with a headline and a number from your own setup.

## Read a skill before you install it

A skill runs with your agent's access: your files, your shell, your credentials. Read its `SKILL.md` and scripts before you install it, from this repo or anywhere else. Every skill here passes a static security scan on every change, runs nothing when you install it, and never downloads code to run.

## How they are tested

Each skill passes strict checks against the [Agent Skills specification](https://agentskills.io/specification), a security scan, trigger routing across all ten skills, and its own test suite in CI. Before release, each one was also reviewed by a separate agent that used it on a real machine with Claude Code, Codex, and Gemini CLI installed, and the skills that run code or connect to servers went through a second, adversarial review. The details and commands are in [evals/README.md](evals/README.md).

## License

The skills are MIT-licensed ([LICENSE](LICENSE)). The folder layout, the registry, and the eval tools in `evals/tools/` come from Shubham Saboo's [agent_skills collection](https://github.com/Shubhamsaboo/awesome-llm-apps/tree/main/agent_skills) in awesome-llm-apps; the tools are used under the Apache License 2.0.
