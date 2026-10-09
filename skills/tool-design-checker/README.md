# Grade your MCP tools the way a model reads them

Point it at an MCP server or a saved tool list to grade each tool's purpose, parameters, annotations, and token size, or let it find the MCP servers configured across your coding agents and count what they load into every session.

## What you get

A sample report from a test fixture: three made-up servers configured in Claude Code and Cursor, run with `installed --launch`. The long "Fix:" texts and four of the five tool blocks are cut here.

```
**Your 3 MCP servers load 10 tools and about 820 tokens into every Claude Code session. 4 tools have an unclear purpose, and 1 pair collides across servers.**

Folder: `~/code/my-app`

| Harness | Servers | Tools | Tokens (estimate) | Unclear purpose | Collisions | Config files read |
|---|---|---|---|---|---|---|
| Claude Code | 3 | 10 | 823 | 4 | 1 | `~/.claude.json`, `~/code/my-app/.mcp.json` |
| Codex | 0 | 0 | 0 | 0 | 0 | none found |
| Gemini CLI | 0 | 0 | 0 | 0 | 0 | none found |
| Cursor | 1 | 5 | 590 | 1 | 0 | `~/.cursor/mcp.json` |
| OpenCode | 0 | 0 | 0 | 0 | 0 | none found |

## Servers

| Server | Where | Runs | Status | Grade | Tools | Tokens |
|---|---|---|---|---|---|---|
| `docs` | Claude Code project (project file) | `docs-mcp --site ./docs` | listed | D (68) | 3 | 116 |
| `github` | Claude Code user; Cursor global | `github-mcp` (env: `GITHUB_TOKEN`) | listed | A (90) | 5 | 590 |
| `wiki` | Claude Code user | `wiki-mcp --space ENG` | listed | B (80) | 2 | 117 |

## Tools that collide across servers

- `search` (`docs`) and `search` (`wiki`), in Claude Code: the names mean the same thing and at least one description is unclear. Fix: ...

## Fixes for the weakest tools

**`search` (`docs`)** (F, 48)
- medium, short-description: The description has 1 sentence; the guidance is at least 3. Fix: ...
- medium, unclear-purpose: The description mostly repeats the name; it adds 1 new word. Fix: ...
- medium, param-no-description: Parameters without a description: `q`. Fix: ...
- low, readonly-hint-missing: The name says it only reads, but readOnlyHint is not set to true. Fix: ...
- medium, list-without-limit: A list or search tool with no limit, page, or cursor parameter. Fix: ...
```

Before anything starts, a plain `installed` run prints the same server list with "With --launch, 3 stdio servers would start (1 from files inside this project: `docs`)." For one server, `lint` prints the same grades and fixes for each of its tools, headed by a line such as "16 tools grade B (86 of 100) and add about 2,700 tokens of definitions to every session that loads them".

## Install

```
npx skills add https://github.com/RyanAlberts/best-of-Agent-Harnesses/tree/main/skills/tool-design-checker
```

In Claude Code, all ten at once:

```
/plugin marketplace add RyanAlberts/best-of-Agent-Harnesses
/plugin install harness-skills@agent-harnesses
```

Manual: copy `skills/tool-design-checker/` into your agent's skills folder: `~/.claude/skills/` (Claude Code), `~/.agents/skills/` (Codex, Gemini CLI, Cursor, and OpenCode all read it).

## Use it

Ask your agent: "How many tools and tokens do my MCP servers load?" or "Lint the tool descriptions in my MCP server before I publish it."

Or run the script from your project folder (the path below is the Claude Code install; use wherever you put the skill):

```
python3 ~/.claude/skills/tool-design-checker/scripts/tools_check.py installed            # list servers; starts nothing
python3 ~/.claude/skills/tool-design-checker/scripts/tools_check.py installed --launch   # start each stdio server and grade its tools
python3 ~/.claude/skills/tool-design-checker/scripts/tools_check.py lint --tools tools.json
python3 ~/.claude/skills/tool-design-checker/scripts/tools_check.py lint --server "node build/index.js" --cwd ~/code/my-server
```

`lint --tools` reads an MCP `tools/list` result, an OpenAI function list, or an Anthropic tools list. `lint --server` takes one plain command, with no `&&`, `;`, pipes, redirects, or `VAR=value` prefix; `--cwd` sets the folder it starts in. `installed` also takes `--harness`, `--only name1,name2`, and `--include-unapproved`. Add `--json` for machine-readable output, `--out report.md` to save the report, and `--fail-under C` to fail a build when a grade drops below C.

## How it works

1. **Reads the definitions.** A tool list comes from a file, or from a server the script starts and asks over MCP. The client speaks both the current MCP protocol (revision 2026-07-28) and the older `initialize` handshake, pages through the whole list, and stops each server within about 30 seconds.
2. **Checks each tool** for the description smells measured in the paper "MCP Tool Descriptions Are Smelly!" (a smell is a common flaw in a tool description; 97.1% of 856 real tools had at least one) and the advice in Anthropic's tool-writing guide: a missing or thin description, no word on what it returns or when to use it, parameters without descriptions, dates and IDs with no format, a read tool without `readOnlyHint`, a search tool with no limit, and more.
3. **Grades** each tool from 100 down (A to F), then each server, with the rubric printed under every report.
4. **Counts the load.** In `installed` mode it reads the MCP config files of Claude Code, Codex, Gemini CLI, Cursor, and OpenCode, applies each one's precedence, trust, approval, and tool filters, counts each server once, and totals tools, tokens, unclear purposes, and colliding tool pairs for each harness.

Every rule, threshold, and source is in [references/smells.md](references/smells.md); every config file and harness rule is in [references/mcp-configs.md](references/mcp-configs.md). Tool names, descriptions, and server messages are untrusted text: the report shows each one inside inline code, on a single line with secrets masked and backticks, pipes, and control characters replaced, so a hostile server cannot break the tables, add links or HTML, or slip instructions into them.

## Works with

| Harness or format | Support |
|---|---|
| Claude Code | Reads user, local, and project (`.mcp.json`) servers; skips `.mcp.json` servers you have not approved in Claude Code unless you pass `--include-unapproved`. |
| Codex | Reads user and trusted-project `config.toml`; needs Python 3.11 or newer (macOS ships 3.9). |
| Gemini CLI | Reads user, project, and extension servers, with trusted-folder rules. |
| Cursor | Reads global and project `mcp.json`. |
| OpenCode | Reads global `opencode.json` and every `opencode.json` or `opencode.jsonc` from the project folder up to the repository root; built from the docs, not tested on a real install. |
| Tool lists | MCP `tools/list` results, OpenAI function lists, Anthropic tools lists. |
| Servers | stdio servers with `--launch`; Streamable HTTP servers with `--launch --remote`. Servers on the deprecated HTTP+SSE transport or on WebSocket are listed and skipped. |

## Limits

- The checks match words and schema fields, so they catch missing pieces, not wrong ones: a description can pass and still say something false. They assume English descriptions.
- Two of the paper's six parts stay ungraded: stated limitations, because a word search cannot tell a real limit from filler, and examples, because the paper found that removing them made no measurable difference.
- Token counts are estimates (JSON characters divided by 4). Each harness wraps tools its own way, and harnesses that search for tools on demand load less per session.
- `installed` reads the config files listed in [references/mcp-configs.md](references/mcp-configs.md). Servers from plugins, claude.ai connectors, and admin-managed files are outside that list, and so are Cursor's on and off switches. It checks one folder at a time (`--project`).
- `--remote` lists Streamable HTTP servers that need no sign-in. Servers that need OAuth are reported, and redirects are reported instead of followed, so your headers go only to the host you configured.
- A server that moves its own child processes out of its process group can leave them running.

## Privacy

- It reads harness config files and the tool lists servers return. Env values and header values from your configs are never printed, only their names. Commands are shown with secret-looking values masked, and URLs show only the host and common path words such as `api` and `mcp`.
- Before anything is printed or saved, one last pass masks every secret the run knows about, wherever it appears in the report, including text a server sends back: config env and header values, bearer tokens with and without the "Bearer " prefix, passwords written inside URLs, every expanded `${VAR}` or `{env:NAME}` value, and the values of variables with names such as TOKEN, KEY, SECRET, PASSWORD, AUTH, or SESSION that a launched server gets. Values shorter than 6 characters are left alone, so ordinary words are not hidden.
- Without `--launch`, nothing starts and nothing is sent anywhere.
- `--launch` starts your configured servers on this machine, and each one runs its own code: it may download packages (`npx -y`, `uvx`) or call its own services over the network. The skill asks before launching and names the servers that come from files inside the project.
- The environment a launched server gets follows its harness. Codex servers get only HOME, LOGNAME, PATH, SHELL, USER, __CF_USER_TEXT_ENCODING, LANG, LC_ALL, TERM, TMPDIR, and TZ, plus the names in their `env_vars` and their `env` values. Gemini CLI servers get this shell's environment without the variables whose names contain TOKEN, SECRET, PASSWORD, KEY, AUTH, CREDENTIAL, PRIVATE, or CERT, plus their `env` values. Claude Code, Cursor, and OpenCode servers get this shell's environment plus their `env` values (the sources this checker follows do not document a smaller set for those harnesses). A server configured in several harnesses starts with the smallest of these.
- `--remote` sends MCP requests, with your configured headers, to the remote servers you configured, and nowhere else.

## Related

- [Progressive disclosure](../../comparisons/progressive-disclosure.md): why every loaded tool definition costs context in every session, and how harnesses load less.
- [This repo's MCP server](../../mcp/) is a worked example. From the repository root, `python3 skills/tool-design-checker/scripts/tools_check.py lint --server "uv run --with 'mcp<2' python mcp/server.py"` graded it B (86) on 2026-09-28.
- [agents-md-checker](../agents-md-checker/) checks the instruction files each agent loads; [session-waste-report](../session-waste-report/) finds where tokens go inside sessions.
- Built on: Hasan, Li, Rajbahadur, Adams, and Hassan, ["Model Context Protocol (MCP) Tool Descriptions Are Smelly!"](https://arxiv.org/abs/2602.14878) (arXiv:2602.14878); Anthropic's [Writing effective tools for agents](https://www.anthropic.com/engineering/writing-tools-for-agents) and [tool definition best practices](https://platform.claude.com/docs/en/agents-and-tools/tool-use/define-tools); the [MCP specification](https://modelcontextprotocol.io/specification/2026-07-28). Related tools: [lintlang](https://github.com/hermes-labs-ai/lintlang) lints tool definitions in files, and [MCP Inspector](https://github.com/modelcontextprotocol/inspector) calls a server's tools by hand.
