# Tool-definition smells: what each check looks for and how to fix it

A model picks a tool, and fills in its arguments, from three things only: the tool's name, its description, and its input schema (the JSON Schema that lists the parameters). A smell is a common flaw in a tool description. This file lists every check `tools_check.py` runs on those three things, why each problem hurts the model, and the fix.

The checks are fixed text and schema rules. They call no model, so they can tell whether a description says what a tool returns, but not whether that statement is true. Treat a finding as a strong hint to reread the tool, and treat a clean result as "nothing obvious is missing".

Checked 2026-09-28.

## Sources

1. Mohammed Mehedi Hasan, Hao Li, Gopi Krishnan Rajbahadur, Bram Adams, Ahmed E. Hassan. "Model Context Protocol (MCP) Tool Descriptions Are Smelly! Towards Improving AI Agent Efficiency with Augmented MCP Tool Descriptions." arXiv:2602.14878, version 3, 2026-05-31. https://arxiv.org/abs/2602.14878 (called "the paper" below).
2. Anthropic Engineering, "Writing effective tools for agents", 2025-09-11. https://www.anthropic.com/engineering/writing-tools-for-agents (called "Anthropic's post" below).
3. Anthropic documentation, "Define tools", section "Best practices for tool definitions". https://platform.claude.com/docs/en/agents-and-tools/tool-use/define-tools (called "Anthropic's docs" below).
4. MCP specification, revision 2026-07-28: Tools (https://modelcontextprotocol.io/specification/2026-07-28/server/tools) and the schema reference for `ToolAnnotations` (https://modelcontextprotocol.io/specification/2026-07-28/schema).

Related tools: [lintlang](https://github.com/hermes-labs-ai/lintlang) lints tool definitions saved in files, and [MCP Inspector](https://github.com/modelcontextprotocol/inspector) lets you call a server's tools by hand. This checker adds grading against the paper's categories and a view of the servers you have installed.

## What the paper found

The paper scored 856 tools from 103 MCP servers on six parts of a good description, each on a 1 to 5 scale, with a panel of three models as judges (Section 4.3.2). A score under 3 counts as a smell (Section 4.1.4). Results (Section 5.1, Figure 7):

| Part of the description | Smell when it scores under 3 | Share of tools with the smell |
|---|---|---|
| Purpose: what the tool does | Unclear Purpose | 56% |
| Guidelines: when and how to use it | Missing Usage Guidelines | 89.3% |
| Limitations: constraints and cases where it fails | Unstated Limitations | 89.8% |
| Parameter explanation: what each input means | Opaque Parameters | 84.3% |
| Length and completeness: at least three to four sentences | Underspecified or Incomplete | 79.1% |
| Examples: examples of correct use | Exemplar Issues | 77.9% |

In total, 97.1% of tools had at least one smell. Rewriting descriptions to cover every part raised task success by a median of 5.85 percentage points, but agents also took 67.46% more steps, and results got worse in 16.67% of cases (Section 5.2). Removing the examples did not measurably hurt (Section 5.3), and short descriptions that keep the purpose and the usage guidance sometimes matched or beat the full ones (Sections 5.3 and 7.1). The paper's advice to server developers includes running smell detection in review or continuous integration and treating a smelly description as a reason to hold a release (Section 7.1).

That is why the grades here weigh purpose, usage, and parameters most, never ask for examples in general, and flag very long definitions too: longer is not always better.

## How the paper's parts map to the checks

| Paper's part | Checks here | Not covered, and why |
|---|---|---|
| Purpose | `missing-description`, `unclear-purpose`, `no-return-info` | Whether the stated purpose is correct |
| Guidelines | `no-usage-guidance` (and `unclear-purpose` when return and usage are both missing) | Whether the guidance is good advice |
| Limitations | none | A word search cannot tell a real limit from filler; review these by hand |
| Parameter explanation | `param-no-description`, `vague-param-name`, `format-without-example`, `required-not-in-schema` | Whether each description is right |
| Length and completeness | `short-description` | Whether the length fits the tool's complexity |
| Examples | only `format-without-example`, for dates and IDs | Examples in general: the paper found they did not help |

The other checks come from Anthropic's post and docs and from the MCP specification.

## The checks

Severity sets the grade cost (see "Grades" below): high 30 points, medium 12, low 4.

### missing-description (high)

- **Trigger:** the tool has no description, or only whitespace.
- **Why it hurts:** the model has only the name to go on. Anthropic's docs call detailed descriptions "by far the most important factor in tool performance".
- **Fix:** write 3 to 4 sentences: what the tool does, what it returns, when to use it (and when to use another tool), and any limit.

### short-description (medium; low for a tool with no parameters)

- **Trigger:** fewer than 3 sentences. A sentence is a run of at least two words ending in `.`, `!`, or `?`, a bullet, a blank-line paragraph, or a `name: text` line; "e.g." and similar abbreviations do not end a sentence.
- **Why it hurts:** Anthropic's docs ask for "at least 3–4 sentences for each tool description, more if the tool is complex", and the paper's Length and Completeness part sets the same floor. A tool with no inputs is simpler, so the cost is lower (the paper found compact descriptions can be enough for simple tools).
- **Fix:** grow it to at least 3 sentences. The good example in Anthropic's docs says what the tool retrieves (the current price for one ticker), what it returns (the latest trade price in USD), when to use it, and what it does not return.

### unclear-purpose (medium)

- **Trigger:** either of these:
  - the description adds fewer than 3 new content words beyond the words of the tool name (common words such as "tool", "data", or "information" do not count), so it mostly repeats the name; or
  - it says neither what the tool returns nor when to use it (see the two checks below for the cues).
- **Why it hurts:** the paper's most basic smell. Without a purpose the model guesses, picks the wrong tool, or passes wrong arguments.
- **Fix:** open with a sentence that says what the tool does and what comes back, in words the name does not already say, then say when to use it.

### no-return-info (low)

- **Trigger:** the description never says what comes back. Cues include words such as "returns", "responds with", "outputs", "results"; a description of the output schema; and, for tools whose names do not change data, an opening verb such as "Gets", "Lists", or "Searches", or an output noun such as "list", "record", "markdown", "JSON", or "summary". A tool that creates, changes, or deletes something must say what it returns in words.
- **Why it hurts:** the paper's top Purpose score requires the return data; Anthropic's good example says "The tool will return the latest trade price in USD".
- **Fix:** add one sentence, for example "Returns the new issue's number and URL."

### no-usage-guidance (low)

- **Trigger:** no usage cue, such as "when", "use it", "use this", "instead of", "rather than", "only for", "do not use", "if you", "before calling", "designed for", a quoted example request (`for "..."`), or the name of another tool in the same server.
- **Why it hurts:** Missing Usage Guidelines was the second most common smell (89.3%), and the paper names purpose plus guidelines as the parts to prioritize. Anthropic's docs ask for "When it should be used (and when it shouldn't)".
- **Fix:** add when to use it and when to reach for a sibling tool: "Use it when the user asks about X; to read one item in full, use get_x."

### param-no-description (medium when half or more of the parameters lack one; low otherwise)

- **Trigger:** a parameter, at any depth, has no `description` in the schema (local `$ref` links are followed) and the tool description has no `name: text` line for it. A schema that refers back to itself (a tree of nodes, for example) is walked once; the report adds the note "recursive schema at" and the path where the walk stopped.
- **Why it hurts:** Opaque Parameters affected 84.3% of tools. The paper judges parameters against the schema, because that is where the model reads them.
- **Fix:** give each parameter a description with its meaning, its unit or format, and an example value.

### vague-param-name (medium)

- **Trigger:** an undescribed parameter named `id`, `data`, `value`, `input`, `params`, `args`, `arg`, `payload`, `obj`, `item`, `val`, or `user`.
- **Why it hurts:** Anthropic's post asks for unambiguous parameter names, for example `user_id` instead of `user`.
- **Fix:** rename it to say what it holds, and describe it.

### required-not-in-schema (high)

- **Trigger:** a name in a `required` list is not defined under `properties`.
- **Why it hurts:** the model must send a field whose type and meaning it cannot know, so calls fail validation.
- **Fix:** define it, or remove it from `required`.

### schema-invalid (high)

- **Trigger:** the input schema is missing, not a JSON object, or its `type` is not `"object"`. (OpenAI function lists may leave out `parameters` for a tool with no inputs; that is allowed.)
- **Why it hurts:** the MCP specification says `inputSchema` "MUST be a valid JSON Schema object (not null)" with type object; clients may drop or reject the tool.
- **Fix:** `{"type": "object", "properties": {...}}`; for no inputs the specification recommends `{"type": "object", "additionalProperties": false}`.

### large-enum (low)

- **Trigger:** any `enum` with more than 30 values.
- **Why it hurts:** every value costs tokens in every session, and long lists make the choice harder.
- **Fix:** group the values, accept free text that the server checks, or add a tool that lists the allowed values.

### deep-nesting (low)

- **Trigger:** objects nested more than 3 levels below the top of the input (an array of objects counts as a level).
- **Why it hurts:** Anthropic's docs recommend `input_examples` for "complex inputs, nested objects, or format-sensitive parameters" because models make more argument mistakes there.
- **Fix:** lift nested fields to the top level or split the tool; if the shape must stay, show an example input in the description.

### format-without-example (medium for dates and times; low for IDs)

- **Trigger:** a string parameter whose name marks a date or time (`date`, `time`, `timestamp`, `since`, `until`, `deadline`, names ending in `_at` or `At`) or an ID (last word `id`, `ids`, `uuid`, `guid`) has no `format`, `pattern`, `enum`, `examples`, or `default`, and its description (plus any description line that names it) gives no digits, format words ("YYYY", "ISO 8601", "UTC", "seconds", "format"), or example words ("e.g.", "for example", "such as"). For IDs, words that say where the value comes from ("from", "returned by") also count.
- **Why it hurts:** the paper's motivating case (Section 2) is a stock-price tool with vague `start_date` and `end_date`: the model sent multi-year ranges until the description said "yyyy-mm-dd". Anthropic's post found agents handle meaningful identifiers better than opaque ones.
- **Fix:** "ISO 8601 date such as 2026-01-31", `"format": "date"`, or "issue number from search_issues, such as 42".

### readonly-hint-missing (low; MCP tools only)

- **Trigger:** the name's first verb is a read verb (`get`, `list`, `search`, `find`, `read`, `fetch`, `query`, `describe`, `show`, `view`, `lookup`, `count`, `inspect`, `browse`, `preview`, `retrieve`), no word in the name creates, changes, or deletes something, and `annotations.readOnlyHint` is not `true`.
- **Why it hurts:** in the MCP schema `readOnlyHint` defaults to false and `destructiveHint` to true, so a client must treat an unannotated tool as one that may change or destroy data, and may ask the user before every call. Anthropic's post: "tool annotations help disclose which tools require open-world access or make destructive changes."
- **Fix:** if the tool never changes anything, add `"annotations": {"readOnlyHint": true}`. Check first: a wrong `readOnlyHint` lets clients skip the confirmation for a call that writes.

### hint-contradiction (high; MCP tools only)

- **Trigger:** a word in the name is a delete-type verb (`delete`, `remove`, `drop`, `destroy`, `purge`, `erase`, `wipe`, `truncate`, `overwrite`, `reset`, `revoke`, `kill`, `terminate`, `uninstall`, `unlink`, `rm`, `del`, `clear`, `discard`) and the annotations say `readOnlyHint: true` or `destructiveHint: false`.
- **Why it hurts:** a client that trusts the hint may skip the confirmation for a call that deletes data. (The specification also says clients must treat hints from untrusted servers as untrusted; a wrong hint is still a bug.)
- **Fix:** set `readOnlyHint` to false and `destructiveHint` to true, or rename the tool.

### destructive-hint-missing (low; MCP tools only)

- **Trigger:** a delete-type verb in the name, and no `destructiveHint` key.
- **Why it hurts:** little in practice, because the default is already "destructive"; the finding asks you to state it so readers and clients see the intent.
- **Fix:** `"annotations": {"destructiveHint": true}`.

### list-without-limit (medium)

- **Trigger:** the name contains `list`, `search`, or `browse`, the tool takes at least one parameter, and no top-level parameter is a limit. A parameter is a limit when its whole name, in snake_case, is one of `limit`, `max_results`, `max`, `top`, `top_k`, `n`, `k`, `count`, `page`, `page_size`, `per_page`, `offset`, `cursor`, `after`, `before`, `first`, `last`, `skip`, `take`, `page_token`, or `next_token` (so `maxResults` and `pageSize` count), or when it is an integer or number whose name contains `limit`, `max`, `top`, `count`, `size`, `page`, `offset`, or `per`. A list tool with no parameters at all is not flagged, and neither is a query tool such as `run_query`, whose answer size the query itself sets.
- **Why it hurts:** Anthropic's post recommends pagination, range selection, filtering, or truncation, with sensible defaults, for any tool whose answer can be large, and says Claude Code cuts tool responses at 25,000 tokens by default.
- **Fix:** add `max_results` with a small default and a cursor or page parameter.

### large-definition (low)

- **Trigger:** the definition is over 1,500 tokens (estimate below).
- **Why it hurts:** every loaded tool costs its full definition in every session, before any work starts; the paper notes that practitioners warn very long descriptions crowd the context window (Section 5.3).
- **Fix:** cut repeated text, move reference material into an MCP resource or a docs tool, shorten long enums.

### invalid-name (medium)

- **Trigger:** the name is not 1 to 128 characters of `A-Z`, `a-z`, `0-9`, `_`, `-`, and `.` (the MCP specification's naming guidance).
- **Fix:** rename within those characters.

### Server findings

Each server finding costs the server 5 points (at most 20 in total).

- **duplicate-name:** two tools in one server share a name; the specification says names "SHOULD be unique within a server".
- **inconsistent-naming:** multi-word names mix snake_case, camelCase, kebab-case, or dot.case. Pick one style and rename the rest.
- **near-duplicate:** two tools whose names mean the same thing once synonyms are folded (find, query, and lookup count as search; fetch, read, retrieve, show, view, and describe count as get; remove and rm as delete; add, new, make, and insert as create; edit, modify, change, and patch as update), or whose descriptions share at least 60% of their content words (both need 6 or more). Singular and plural names (`get_user`, `get_users`) are not folded. Anthropic's post: "Too many tools or overlapping tools can also distract agents from pursuing efficient strategies." Merge them, or make each description say when to use it instead of the other.
- **stdout-noise:** the server wrote lines to stdout that are not MCP messages. The stdio transport says a server "MUST NOT write anything to its stdout that is not a valid MCP message". Send logs to stderr.

## Collisions across servers (installed mode)

Two tools on different servers collide when their descriptions share at least 60% of their content words, or when their names mean the same thing and at least one of the two has an unclear purpose. Harnesses add the server name to each tool name, so two `search` tools stay tellable apart only through their descriptions. The MCP specification notes that clients combining servers "MAY encounter naming collisions", and Anthropic's post warns that tools that "overlap in function or have a vague purpose" confuse agents. Fix: turn one off in the harness where both load, or make the descriptions say what sets each apart.

## Grades

- A tool starts at 100 and loses 30 per high, 12 per medium, and 4 per low finding (never below 0).
- A server's score is the mean of its tool scores, minus 5 per server finding, at most 20.
- Letters: A is 90 and up, B 80 and up, C 70 and up, D 60 and up, F below 60.
- `--fail-under C` exits with code 1 when a server grade is below C, for use in continuous integration.

Calibration: Anthropic's own "good" example tool grades A (96; only the missing `readOnlyHint`), and its "poor" example grades D (60). This repository's MCP server graded B (86) on 2026-09-28.

## Token estimate

The size of a tool is the length of `{"name", "description", "inputSchema"}` written as compact JSON, divided by 4 and rounded up. It is an estimate: each harness wraps tools in its own format and may add the server name to each tool name, and a harness that searches for tools on demand (instead of loading every definition up front) pays less per session.
