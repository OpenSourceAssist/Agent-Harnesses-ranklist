# Where each harness keeps its MCP servers, and how the checker reads them

`tools_check.py installed` reads these files for one folder (the `--project` folder, default the current one), applies each harness's own rules, and reports which servers load in a session started there. Checked 2026-09-28. `~` is your home folder.

Env values and header values are read only to start or reach a server with `--launch`; they are never printed. The report shows variable names and header names instead, and URLs show only the scheme, the host, and common path words such as `api`, `mcp`, and `v1`: other path segments, the query, and the fragment are masked. Every server name, command, path, and message in the report passes through `safe_text()`, which masks secrets and keeps it on one line with backticks, pipes, control characters, and blank-looking characters replaced; the Markdown report then shows it inside inline code (`code()` from `safe.py`), so links and HTML stay plain text. Before the report is printed or saved, a last pass masks every secret the run knows about (config values, header values and bare tokens, passwords inside URLs, expanded `${VAR}` and `{env:}` values, and secret-looking variables a launched server gets), in the forms JSON and `safe_text()` would show them too.

Config files are read only when they are regular files of at most 16 MB. A FIFO, a device (such as a link to `/dev/zero`), or a larger file is skipped with a note.

## Claude Code

Sources: https://code.claude.com/docs/en/mcp (scopes and entry shape), https://code.claude.com/docs/en/settings-reference (project server approval).

| Scope | File | Key |
|---|---|---|
| local (the default for `claude mcp add`) | `~/.claude.json` | `projects["<absolute folder path>"].mcpServers` |
| project (shared in the repo) | `.mcp.json` in the folder | `mcpServers` |
| user | `~/.claude.json` | `mcpServers` |

- One server name, one connection: the whole entry comes from the highest scope, local over project over user. The lower entries are reported as "replaced".
- `.mcp.json` servers need approval: Claude Code asks before it first starts one in an interactive session (headless runs load them without asking). The checker reads `enabledMcpjsonServers`, `disabledMcpjsonServers`, and `enableAllProjectMcpServers` from `projects["<path>"]` in `~/.claude.json` and from `~/.claude/settings.json`, `.claude/settings.json`, and `.claude/settings.local.json`. A disabled server is reported as off. A server that is neither approved nor disabled is reported as "not yet approved in Claude Code" and is neither counted nor launched, unless you pass `--include-unapproved`. A server approved only by `.claude/settings.json` or `.claude/settings.local.json` inside the project (files a repository can ship) is reported as "approved by .claude/settings.json in this project" and named separately before any launch.
- Entry: `type` (`stdio`, `http`, `sse`, `ws`), `command`, `args`, `env`, `url`, `headers`. An entry without `type` is read as stdio, so an entry with only a `url` cannot start; the checker flags it (`url-without-type`). Server names use letters, digits, `-`, and `_` (`invalid-server-name`).
- `.mcp.json` values may use `${VAR}` and `${VAR:-default}`; the checker expands them the same way when it launches a server, and treats every expanded value as a secret.
- For remote servers, Claude Code reads some credential variables as empty in `url` and `headers` (the docs list `ANTHROPIC_API_KEY`, `ANTHROPIC_AUTH_TOKEN`, `AWS_BEARER_TOKEN_BEDROCK`, `HTTPS_PROXY`, `NPM_TOKEN`); `--remote` blanks the same ones.
- Not read: servers from plugins, claude.ai connectors, admin-managed `managed-mcp.json`, and `mcpServers` in `~/.claude/settings.json` (not a documented settings key). Servers configured for other folders in `~/.claude.json` are counted in a note; pass `--project` to check one of them.
- UNVERIFIED: with `CLAUDE_CONFIG_DIR` set, the checker reads `$CLAUDE_CONFIG_DIR/.claude.json` when that file exists (and `~/.claude.json` otherwise), and reads user settings from `$CLAUDE_CONFIG_DIR/settings.json`.

## Codex

Source: https://learn.chatgpt.com/docs/extend/mcp.md

- Tables `[mcp_servers.<name>]` in `$CODEX_HOME/config.toml` (default `~/.codex/config.toml`) and in the folder's `.codex/config.toml`.
- The folder's file loads only in a trusted project: `[projects."<path>"] trust_level = "trusted"` in the user config, for the folder or a parent. Otherwise its servers are reported as off. A project entry replaces a user entry with the same name.
- Stdio keys: `command`, `args`, `env`, `env_vars` (names of variables to pass through), `cwd`. HTTP keys: `url`, `http_headers`, `env_http_headers` (header name to variable name), `bearer_token_env_var`. `enabled = false` turns a server off.
- Tool filters: `enabled_tools` keeps only the listed tools, then `disabled_tools` removes tools; the per-harness totals apply both.
- Needs Python 3.11 or newer, because reading TOML needs `tomllib`. On older Pythons (macOS ships 3.9) Codex is skipped with a note.
- Not read: profile files, `-c` overrides, managed config, and `.codex/config.toml` files in folders between the project root and a subfolder.

## Gemini CLI

Sources: https://github.com/google-gemini/gemini-cli/blob/main/docs/tools/mcp-server.md and https://github.com/google-gemini/gemini-cli/blob/main/docs/cli/trusted-folders.md

- `mcpServers` in `~/.gemini/settings.json` (user), `.gemini/settings.json` in the folder (project), and `~/.gemini/extensions/*/gemini-extension.json`. A later layer replaces an earlier entry with the same name: extension, then user, then project. Settings files may contain comments.
- Trusted folders: `~/.gemini/trustedFolders.json` (or `GEMINI_CLI_TRUSTED_FOLDERS_PATH`) maps paths to `TRUST_FOLDER`, `TRUST_PARENT` (trust the folder above), or `DO_NOT_TRUST`; the most specific rule wins. In a folder that is not trusted, Gemini CLI ignores the project settings and loads no MCP servers, so the checker reports every Gemini CLI server as off there. Setting `security.folderTrust.enabled` to false turns the trust check off.
- `mcp.allowed` (only these servers) and `mcp.excluded` (never these) turn servers off. Per server, `includeTools` keeps only the listed tools and `excludeTools` removes tools (exclude wins).
- Transports: `command` for stdio, `httpUrl` for Streamable HTTP, `url` for the deprecated HTTP+SSE transport (which the checker skips). `env` values may use `$VAR` or `${VAR}`.
- Gemini CLI names MCP tools `mcp_<server>_<tool>`, and its policy rules split the name at the first `_` after `mcp_`, so a server name with `_` breaks policy matching (`underscore-in-server-name`).
- Not read: system-wide settings.

## Cursor

Source: https://cursor.com/docs/mcp.md

- `mcpServers` in `~/.cursor/mcp.json` (global) and `.cursor/mcp.json` in the folder (project). The checker lets a project entry replace a global entry with the same name; Cursor's docs do not state the order.
- Keys: `command`, `args`, `env`, `envFile` (stdio), `url`, `headers`. Values may use `${env:NAME}`, `${userHome}`, and `${workspaceFolder}`.
- Cursor keeps its on and off switches for each server in its own settings, not in `mcp.json`, so the checker counts every server listed there. The Cursor CLI asks before it uses MCP servers (it has an `--approve-mcps` flag), but where that approval is stored is not documented, so the checker cannot tell approved servers from new ones; the servers from the project's `.cursor/mcp.json` are named separately before any launch.

## OpenCode

Sources: https://opencode.ai/docs/mcp-servers and https://opencode.ai/docs/config. Not verified on a real install (OpenCode was not installed on the machine used to test this skill).

- The `mcp` object in `$XDG_CONFIG_HOME/opencode/opencode.json` or `opencode.jsonc` (default `~/.config/opencode/`), then the file named by `OPENCODE_CONFIG`, then every `opencode.json` and `opencode.jsonc` from the repository root (the nearest folder above `--project` with a `.git`) down to `--project`. When no repository root is found, only `--project` itself is searched.
- Files merge key by key: a later file changes only the keys it sets for a server (for example `"enabled": false`), and the rest of the entry stays.
- Local servers: `{"type": "local", "command": ["cmd", "arg"], "environment": {...}}`. Remote: `{"type": "remote", "url": "...", "headers": {...}}`. `"enabled": false` turns a server off.
- Values may use `{env:NAME}` (a variable) and `{file:path}` (the contents of a file, relative to the config file's folder); the checker expands both when it launches a server and treats the results as secrets.
- OpenCode names MCP tools `<server>_<tool>`. A glob set to `false` in the top-level `tools` object (for example `"my-mcp*": false`) hides the matching tools; the per-harness totals apply it.
- There is no documented approval step for project servers, so they are counted; the servers from files inside the project are named separately before any launch.
- Not read: remote `.well-known/opencode` config, `.opencode/` folders, `OPENCODE_CONFIG_CONTENT`, and managed config files.

## One server, several harnesses

Two entries are the same server when they run the same command (compared by file name) with the same arguments, or point at the same URL (scheme and host lowercased, default port and trailing slash dropped). The checker starts each server once, then counts its tools, unclear purposes, and collisions for every harness that loads it, after that harness's tool filters.

## The environment a launched server gets

- Codex servers get only HOME, LOGNAME, PATH, SHELL, USER, __CF_USER_TEXT_ENCODING, LANG, LC_ALL, TERM, TMPDIR, and TZ, plus the names in their `env_vars` and their `env` values (openai/codex, `codex-rs/rmcp-client/src/utils.rs`).
- Gemini CLI servers get this shell's environment without the variables whose names contain TOKEN, SECRET, PASSWORD, KEY, AUTH, CREDENTIAL, PRIVATE, or CERT, plus their `env` values.
- Claude Code, Cursor, and OpenCode servers get this shell's environment plus their `env` values; the sources above do not document a smaller set for those harnesses.
- A server configured in several harnesses starts with the smallest of these: Codex's, then Gemini CLI's, then the others.

## How the tools are listed

The client follows the MCP specification, revision 2026-07-28: https://modelcontextprotocol.io/specification/2026-07-28/basic/versioning (eras and compatibility), https://modelcontextprotocol.io/specification/2026-07-28/basic/transports/stdio, https://modelcontextprotocol.io/specification/2026-07-28/basic/transports/streamable-http, and https://modelcontextprotocol.io/specification/2026-07-28/server/utilities/pagination.

- Revision 2026-07-28 removed the `initialize` handshake: each request carries its protocol version in `_meta`. Servers built for revision 2025-11-25 or earlier still need `initialize`. The client speaks both.
- stdio (`mcp_client.py`): it sends `server/discover` first. An answer means a modern server. Any other error, or silence for 3 seconds, means an older server: it sends `initialize` (asking for 2025-11-25 and accepting the version the server offers), then `notifications/initialized`. A late answer to the probe is still used.
- It then calls `tools/list` until there is no `nextCursor`. An empty string is a valid cursor, not the end. A repeated cursor stops the listing with a note.
- It answers a server's `ping` and refuses any other request from the server, so nothing waits on it; after 100 requests from the server it stops with "the server sent too many requests". Messages whose id is neither a number nor a string are ignored. Lines on stdout that are not MCP messages are skipped and counted (`stdout-noise`); stdout is read in chunks, and a line longer than 4 MB is dropped without being kept in memory.
- A launched server starts in the `--project` folder (or the entry's own `cwd`), as it would in a session there, with the environment described above.
- Each server gets 20 seconds to answer (`--timeout`). Then the client closes the server's input, waits a second, terminates it, waits a second, and kills it and any programs in its process group, so each server is gone within about 30 seconds. Stopping the checker with Ctrl-C, SIGTERM, or SIGHUP runs the same cleanup, and a second signal during the cleanup waits until it is done. A server that moves its own child processes out of its process group can leave them running.
- HTTP (`mcp_http.py`, only with `--launch --remote`): a modern `tools/list` POST first; if the server answers with a 4xx that is not a modern MCP error, it falls back to `initialize` with the `Mcp-Session-Id` header, and ends the session with DELETE when done. Answers may be JSON or an SSE stream; each is read in chunks under one deadline for the whole listing, and an answer larger than 4 MB is rejected. It does not follow redirects (a redirect is reported, so your headers reach only the configured host), does not sign in (a server that answers 401 or 403 is reported as needing sign-in), and skips the deprecated HTTP+SSE transport and WebSocket entries.
