#!/usr/bin/env python3
"""Grade MCP tool definitions the way a model meets them.

Subcommands:
  lint       grade one tool list: a saved file, or a server you launch
  installed  find the MCP servers configured in your harnesses, and with
             --launch, list and grade their tools

Usage:
    python3 tools_check.py lint --tools tools.json [--json] [--out report.md] [--fail-under C]
    python3 tools_check.py lint --server "npx -y my-mcp-server" [--timeout 20]
    python3 tools_check.py installed [--harness all] [--project .] [--launch] [--remote] [--json]

The checks follow the tool-description smells in arXiv:2602.14878 and
Anthropic's guidance on writing tools for agents; references/smells.md has
every rule, threshold, and fix. Token counts are estimates: JSON characters
divided by 4.

Standard library only, Python 3.9+. Reads files and starts servers only as
asked; config env values and header values are never printed.
Exit codes: 0 done, 1 a grade is below --fail-under, 2 usage or input error.
"""

from __future__ import annotations

import argparse
import fnmatch
import functools
import itertools
import json
import os
import re
import shlex
import sys
from collections import Counter

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import mcp_client  # noqa: E402
import mcp_configs  # noqa: E402
from mcp_client import inline, safe_text  # noqa: E402
from safe import code  # noqa: E402

VERSION = "1.0.0"
WEIGHTS = {"high": 30, "medium": 12, "low": 4}
SERVER_FINDING_COST = 5
SERVER_FINDING_CAP = 20
GRADES = (("A", 90), ("B", 80), ("C", 70), ("D", 60), ("F", 0))
MIN_SENTENCES = 3
MIN_NEW_WORDS = 3
ENUM_MAX = 30
DEPTH_MAX = 3
LARGE_TOKENS = 1500
DESC_SIMILAR = 0.6
DESC_MIN_WORDS = 6
NAME_RE = re.compile(r"[A-Za-z0-9_.\-]{1,128}")

STOPWORDS = set("""a an the and or of to in on for with by from at as is are be been being this
that these those it its into over under than then there their them they you your we our not no
can will may might must should would could do does did done has have had any all each every some
such only also more most other which what when where who whom whose how why if else so but about
after before between during up down out off again once here very just one two per via""".split())
GENERIC = set("""tool tools function functions use used uses using call called calls allow allows
help helps perform performs handle handles provide provides method api endpoint request operation
data information info thing things stuff given specified various etc value values item items
object objects result results""".split())
READ_VERBS = set("""get list search find read fetch query describe show view lookup count inspect
browse preview retrieve""".split())
MUTATING_VERBS = set("""create add insert update edit modify patch put post send write save store
upload move rename merge publish deploy run execute exec start stop restart apply approve reject
close commit push install enable disable assign unassign invite pay transfer reply archive
unarchive restore sync import submit trigger invoke launch lock unlock fork grant share connect
disconnect attach detach append replace click press navigate drag upsert increment decrement
toggle cancel reopen set make change schedule""".split())
DESTRUCTIVE_VERBS = set("""delete remove drop destroy purge erase wipe truncate overwrite reset
revoke kill terminate uninstall unlink rm del clear discard""".split())
LIST_VERBS = {"list", "search", "browse"}
# A parameter counts as a limit when its whole name (in snake_case) is one of these,
# or when it is an integer or number whose name contains one of LIMIT_WORDS.
LIMIT_NAMES = {"limit", "max_results", "max", "top", "top_k", "n", "k", "count", "page", "page_size",
               "per_page", "offset", "cursor", "after", "before", "first", "last", "skip", "take",
               "page_token", "next_token"}
LIMIT_WORDS = {"limit", "max", "top", "count", "size", "page", "offset", "per"}
SEVERITY_ORDER = ("high", "medium", "low")
VAGUE_PARAMS = {"id", "data", "value", "input", "params", "args", "arg", "payload", "obj",
                "item", "val", "user"}
DATE_WORDS = {"date", "time", "timestamp", "datetime", "since", "until", "deadline", "birthday", "dob"}
ID_WORDS = {"id", "ids", "uuid", "guid"}
SYNONYMS = {"find": "search", "query": "search", "lookup": "search", "look": "search",
            "fetch": "get", "retrieve": "get", "read": "get", "show": "get", "view": "get",
            "describe": "get", "remove": "delete", "del": "delete", "rm": "delete",
            "destroy": "delete", "erase": "delete", "add": "create", "new": "create",
            "make": "create", "insert": "create", "edit": "update", "modify": "update",
            "change": "update", "patch": "update"}

RETURN_CUE = re.compile(
    r"\b(returns?|returned|returning|responds? with|outputs?|yields?|produces?|gives? back|"
    r"results?|responses?)\b", re.I)
RETRIEVAL_START = re.compile(
    r"^\W*(gets?|fetche?s?|retrieves?|lists?|search(es)?|finds?|reads?|looks? up|shows?|"
    r"quer(y|ies)|counts?|describes?|downloads?|exports?|checks?|calculates?|computes?|"
    r"converts?|generates?|summari[sz]es?|translates?|parses?|extracts?|resolves?|explains?|"
    r"compares?|recommends?|picks?|ranks?)\b", re.I)
OUTPUT_NOUN = re.compile(
    r"\b(records?|lists?|markdown|json|text|contents?|details|summary|summaries|report|table|"
    r"urls?|ids?|status|files?|paths?|guides?|matches|entries|rows|metadata|schema|html|csv|"
    r"images?|comparison|ranking|shortlist|recommendations?|counts?)\b", re.I)
USAGE_CUE = re.compile(
    r"\b(when|whenever|use (this|it|them|these|that|only|for|to|instead|after|before|with)|"
    r"used (for|to|with|by|after|before)|useful (for|when|if)|call (this|it)\b|instead of|"
    r"rather than|prefer|do not use|don't use|avoid|only (for|if|with|after|before)|not for|"
    r"before (calling|using|you)|after (calling|using|you)|"
    r"if (you|the user|a user|the agent|an agent|needed)|ideal for|best for|intended for|"
    r"designed for|meant for|good for|helpful for)\b|\bfor [\"\u201c]", re.I)
FORMAT_HINT = re.compile(
    r"\d|\byyyy|\bmm[-/]dd|\biso[ -]?8601|\brfc ?3339|\bepoch\b|\bunix\b|\butc\b|\bseconds?\b|"
    r"\bmilliseconds?\b|\be\.g\.|\bfor example\b|\bsuch as\b|\blike\b|\bformat\b", re.I)
PROVENANCE = re.compile(r"\b(from|returned by|given by|listed by|output of|as shown|obtained)\b", re.I)
ABBREVIATIONS = re.compile(r"\b(e\.g|i\.e|etc|vs|approx|incl)\.", re.I)
SENTENCE_SPLIT = re.compile(r"(?<=[.!?])\s+|\n\s*\n|\n\s*[-*\u2022]\s+|\n(?=\s*`?[A-Za-z_][\w.]*`?\s*[:=])")

FIXES = {
    "schema-invalid": 'Set inputSchema to a JSON Schema object with "type": "object" and its '
                      'properties; for a tool with no inputs use {"type": "object", '
                      '"additionalProperties": false}.',
    "missing-description": "Write 3 to 4 sentences: what the tool does, what it returns, when to use "
                           "it (and when to use another tool), and any limit.",
    "short-description": "Grow it to at least 3 sentences: what it does, what it returns, when to "
                         "use it, and any limit. Anthropic's guidance asks for 3 to 4 sentences per tool.",
    "unclear-purpose": "Open with a sentence that says what the tool does and what comes back, in "
                       "words the name does not already say, then say when to use it. Example: "
                       "\"Returns the latest trade price in USD for one ticker. Use it when the user "
                       "asks for a current price.\"",
    "no-return-info": "Add a sentence that says what comes back, for example \"Returns the new "
                      "issue's number and URL.\"",
    "no-usage-guidance": "Add when to use it and when to pick another tool, for example \"Use it "
                         "when the user asks about X; to read one item in full, use get_x.\"",
    "param-no-description": "Give each listed parameter a description: what it means, its unit or "
                            "format, and an example value.",
    "vague-param-name": "Rename it to say what it holds (user_id instead of id, search_text "
                        "instead of input), and add a description.",
    "required-not-in-schema": "Define each listed name under properties with a type and a "
                              "description, or remove it from required.",
    "large-enum": "Shorten the list: group the values, accept free text that the server checks, "
                  "or add a tool that lists the allowed values.",
    "deep-nesting": "Flatten the input: lift nested fields to the top level or split the tool. If "
                    "the shape must stay, add an example input to the description.",
    "format-without-example": "State the format and give an example, or set format or pattern in "
                              "the schema, for example \"ISO 8601 date such as 2026-01-31\" or "
                              "\"issue number from search_issues, such as 42\".",
    "readonly-hint-missing": "If the tool never changes anything, add \"annotations\": {\"readOnlyHint\": "
                             "true}. Check first: a wrong readOnlyHint lets clients skip the confirmation "
                             "for a call that writes.",
    "hint-contradiction": "Set readOnlyHint to false and destructiveHint to true, or rename the "
                          "tool if it does not delete or overwrite anything.",
    "destructive-hint-missing": "Add \"annotations\": {\"destructiveHint\": true} so the risk is "
                                "stated. The MCP default already treats a tool without hints as "
                                "possibly destructive, so this makes the intent explicit.",
    "list-without-limit": "Add a limit parameter with a small default (for example max_results, "
                          "default 20) and a cursor or page parameter to get more.",
    "large-definition": "Trim it: cut repeated text, move reference material into an MCP resource "
                        "or a docs tool, and shorten long enums.",
    "invalid-name": "Use 1 to 128 characters from A-Z, a-z, 0-9, underscore, hyphen, and dot.",
    "duplicate-name": "Give every tool in the server its own name.",
    "inconsistent-naming": "Pick one style (snake_case is the most common) and rename the others.",
    "near-duplicate": "Merge the two tools, or make each description say when to use it instead "
                      "of the other.",
    "stdout-noise": "Send logs to stderr. An MCP server must write only MCP messages to stdout.",
}


# ---------------------------------------------------------------------------
# text helpers
# ---------------------------------------------------------------------------

def name_words(name: str) -> list:
    spaced = re.sub(r"([a-z0-9])([A-Z])", r"\1 \2", name or "")
    spaced = re.sub(r"([A-Z]+)([A-Z][a-z])", r"\1 \2", spaced)
    return [w.lower() for w in re.split(r"[^A-Za-z0-9]+", spaced) if w]


def stem(word: str) -> str:
    if len(word) > 4 and word.endswith("ies"):
        return word[:-3] + "y"
    if len(word) > 4 and word.endswith(("ches", "shes", "sses", "xes", "zes")):
        return word[:-2]
    if len(word) > 3 and word.endswith("s") and not word.endswith("ss"):
        return word[:-1]
    return word


@functools.lru_cache(maxsize=4096)
def content_words(text: str) -> frozenset:
    return frozenset(stem(w) for w in re.findall(r"[a-z][a-z0-9']+", (text or "").lower())
                     if len(w) >= 3 and w not in STOPWORDS)


def as_list(value) -> list:
    return value if isinstance(value, list) else []


def sentence_count(text: str) -> int:
    text = ABBREVIATIONS.sub(lambda m: m.group(1), text.strip())
    return sum(1 for part in SENTENCE_SPLIT.split(text)
               if part and len(re.findall(r"[A-Za-z0-9]+", part)) >= 2)


def verb_class(name: str) -> str:
    """destructive | write | read | other, from the words of a tool name."""
    words = name_words(name)
    if any(w in DESTRUCTIVE_VERBS for w in words):
        return "destructive"
    for w in words:
        if w in READ_VERBS:
            return "write" if any(x in MUTATING_VERBS for x in words) else "read"
        if w in MUTATING_VERBS:
            return "write"
    return "other"


def mentions(text: str, name: str) -> list:
    """Lines of text that mention a parameter name (or its singular form)."""
    names = {name, name[:-1]} if name.endswith("s") and len(name) > 3 else {name}
    return [line for line in (text or "").splitlines()
            if any(re.search(r"(?<![A-Za-z0-9_])%s(?![A-Za-z0-9_])" % re.escape(n), line) for n in names)]


def documented_in_text(text: str, name: str) -> bool:
    pattern = r"(?mi)^[\s>*\-\u2022]*`?%s`?\s*(\([^)]*\))?\s*[:=\-\u2013\u2014]" % re.escape(name)
    return re.search(pattern, text or "") is not None


# ---------------------------------------------------------------------------
# schema helpers
# ---------------------------------------------------------------------------

def resolve(spec, root, depth=0):
    """Follow a local $ref (#/...). The referring schema's own keys win."""
    if not isinstance(spec, dict) or "$ref" not in spec or depth > 10:
        return spec if isinstance(spec, dict) else {}
    ref = spec["$ref"]
    target = root
    if isinstance(ref, str) and ref.startswith("#/"):
        for part in ref[2:].split("/"):
            part = part.replace("~1", "/").replace("~0", "~")
            target = target.get(part) if isinstance(target, dict) else None
    if not isinstance(target, dict):
        return {k: v for k, v in spec.items() if k != "$ref"}
    merged = dict(resolve(target, root, depth + 1))
    merged.update({k: v for k, v in spec.items() if k != "$ref"})
    return merged


def ref_of(raw):
    return raw["$ref"] if isinstance(raw, dict) and isinstance(raw.get("$ref"), str) else None


def object_children(spec, root, own_ref=None):
    """(object schema, the $ref that led to it) for objects nested directly under a property."""
    out = []
    for raw in [None, spec.get("items")] + as_list(spec.get("anyOf")) + \
            as_list(spec.get("oneOf")) + as_list(spec.get("allOf")):
        ref = own_ref if raw is None else ref_of(raw)
        candidate = spec if raw is None else resolve(raw, root)
        if isinstance(candidate.get("properties"), dict):
            out.append((candidate, ref))
        elif raw is not None and isinstance(candidate.get("items"), dict):
            items = resolve(candidate["items"], root)
            if isinstance(items.get("properties"), dict):
                out.append((items, ref_of(candidate["items"]) or ref))
    return out


def walk_params(schema, root, path="", depth=0, refs=(), notes=None, mask=()):
    """Yield (path, leaf name, resolved spec, depth) for every property, nested ones too.

    refs holds the $ref targets being expanded on the current path; meeting one of
    them again means the schema is recursive, so the walk stops there with a note."""
    props = schema.get("properties")
    if not isinstance(props, dict) or depth > 12:
        return
    for name, raw in props.items():
        spec = resolve(raw, root)
        here = "%s.%s" % (path, name) if path else name
        yield here, name, spec, depth + 1
        for child, ref in object_children(spec, root, ref_of(raw)):
            if ref and ref in refs:
                note = "recursive schema at %s" % inline(here, 120, mask)
                if notes is not None and note not in notes:
                    notes.append(note)
                continue
            yield from walk_params(child, root, here, depth + 1, refs + ((ref,) if ref else ()), notes, mask)


def string_like(spec, root) -> bool:
    kinds = spec.get("type")
    kinds = kinds if isinstance(kinds, list) else [kinds]
    if "string" in kinds:
        return True
    if "array" in kinds:
        items = resolve(spec.get("items"), root)
        return items.get("type") == "string"
    if kinds == [None]:
        return not any(k in spec for k in ("properties", "items", "enum", "const")) and \
            not any(resolve(s, root).get("type") not in (None, "string", "null")
                    for s in as_list(spec.get("anyOf")) + as_list(spec.get("oneOf")))
    return False


def big_enums(node, path="", out=None):
    out = [] if out is None else out
    if isinstance(node, dict):
        if isinstance(node.get("enum"), list) and len(node["enum"]) > ENUM_MAX:
            out.append((path or "(root)", len(node["enum"])))
        for key, value in node.items():
            big_enums(value, "%s.%s" % (path, key) if path else key, out)
    elif isinstance(node, list):
        for value in node:
            big_enums(value, path, out)
    return out


# ---------------------------------------------------------------------------
# normalizing and grading
# ---------------------------------------------------------------------------

def normalize_tools(data) -> list:
    """MCP tools/list (result or JSON-RPC answer), OpenAI or Anthropic tool lists -> MCP-shaped dicts."""
    if isinstance(data, dict):
        if isinstance(data.get("result"), dict):
            data = data["result"]
        if isinstance(data.get("tools"), list):
            data = data["tools"]
        elif isinstance(data.get("functions"), list):
            data = data["functions"]
        else:
            raise ValueError("no tool list found: expected a tools/list result, an OpenAI "
                             "function list, or an Anthropic tools list")
    if not isinstance(data, list):
        raise ValueError("expected a list of tools")
    out = []
    for index, item in enumerate(data):
        if not isinstance(item, dict):
            raise ValueError("tool %d is not a JSON object" % (index + 1))
        fmt, src = "mcp", item
        if item.get("type") == "function":
            fmt = "openai"
            src = item["function"] if isinstance(item.get("function"), dict) else item
            schema = src.get("parameters", {"type": "object", "properties": {}})
        elif "input_schema" in item:
            fmt, schema = "anthropic", item.get("input_schema")
        elif "inputSchema" in item:
            schema = item.get("inputSchema")
        elif "parameters" in item:
            fmt, schema = "openai", item.get("parameters")
        else:
            schema = None
        out.append({
            "name": str(src.get("name") or ""),
            "title": src.get("title") or "",
            "description": src.get("description") if isinstance(src.get("description"), str) else "",
            "inputSchema": schema,
            "annotations": item.get("annotations") if fmt == "mcp" and
            isinstance(item.get("annotations"), dict) else {},
            "outputSchema": item.get("outputSchema") if isinstance(item.get("outputSchema"), dict) else None,
            "format": fmt,
        })
    return out


def token_estimate(tool: dict) -> int:
    text = json.dumps({"name": tool.get("name", ""), "description": tool.get("description") or "",
                       "inputSchema": tool.get("inputSchema")}, separators=(",", ":"), ensure_ascii=False)
    return -(-len(text) // 4)


def grade_for(score: float) -> str:
    for letter, floor in GRADES:
        if score >= floor:
            return letter
    return "F"


def finding(check: str, severity: str, message: str) -> dict:
    return {"check": check, "severity": severity, "message": message, "fix": FIXES[check]}


def names_text(values, mask=()) -> str:
    """A list of untrusted names (parameters, tools), each inside inline code."""
    return ", ".join(inline(v, 80, mask) for v in values)


def is_limit_param(leaf: str, spec: dict) -> bool:
    if "_".join(name_words(leaf)) in LIMIT_NAMES:
        return True
    kinds = spec.get("type") if isinstance(spec.get("type"), list) else [spec.get("type")]
    return bool(set(name_words(leaf)) & LIMIT_WORDS) and any(k in ("integer", "number") for k in kinds)


def lint_tool(tool: dict, siblings=(), mask=()) -> dict:
    """Grade one tool. tool: MCP shape (see normalize_tools). siblings: other tool names in the
    server. mask: secret values to hide in any text taken from the tool."""
    if "format" not in tool:
        tool = normalize_tools([tool])[0]
    name = tool.get("name") or ""
    desc = (tool.get("description") or "").strip()
    schema = tool.get("inputSchema")
    found = []
    valid_schema = isinstance(schema, dict) and schema.get("type", "object") == "object" and \
        ("type" in schema or tool.get("format") != "mcp")
    notes = []
    params = list(walk_params(schema, schema, notes=notes, mask=mask)) if valid_schema else []
    top = [p for p in params if "." not in p[0]]

    if not NAME_RE.fullmatch(name):
        found.append(finding("invalid-name", "medium",
                             "The name %s is not 1 to 128 characters of A-Z, a-z, 0-9, _, - and ." %
                             inline(name, 80, mask)))
    if not valid_schema:
        found.append(finding("schema-invalid", "high",
                             "The input schema is missing or is not a JSON Schema object with "
                             "\"type\": \"object\"."))

    kind = verb_class(name)
    if not desc:
        found.append(finding("missing-description", "high", "The tool has no description."))
    else:
        sentences = sentence_count(desc)
        if sentences < MIN_SENTENCES:
            found.append(finding("short-description", "medium" if top else "low",
                                 "The description has %d sentence%s; the guidance is at least %d." % (
                                     sentences, "" if sentences == 1 else "s", MIN_SENTENCES)))
        name_stems = {stem(w) for w in name_words(name)}
        new_words = content_words(desc) - name_stems - GENERIC
        out_schema = tool.get("outputSchema") or {}
        described_output = bool(out_schema.get("description")) or any(
            isinstance(v, dict) and v.get("description") for v in (out_schema.get("properties") or {}).values())
        has_return = bool(RETURN_CUE.search(desc)) or described_output or (
            kind not in ("write", "destructive") and bool(RETRIEVAL_START.search(desc) or OUTPUT_NOUN.search(desc)))
        has_usage = bool(USAGE_CUE.search(desc)) or any(
            s != name and re.search(r"(?<![A-Za-z0-9_])%s(?![A-Za-z0-9_])" % re.escape(s), desc)
            for s in siblings if s)
        if len(new_words) < MIN_NEW_WORDS:
            found.append(finding("unclear-purpose", "medium",
                                 "The description mostly repeats the name; it adds %d new word%s." % (
                                     len(new_words), "" if len(new_words) == 1 else "s")))
        elif not has_return and not has_usage:
            found.append(finding("unclear-purpose", "medium",
                                 "The description says neither what the tool returns nor when to use it."))
        elif not has_return:
            found.append(finding("no-return-info", "low", "The description does not say what the tool returns."))
        elif not has_usage:
            found.append(finding("no-usage-guidance", "low",
                                 "The description does not say when to use the tool, or when to use another one."))

    undocumented, vague = [], []
    for path, leaf, spec, _depth in params:
        if spec.get("description") or documented_in_text(desc, leaf):
            continue
        (vague if leaf.lower() in VAGUE_PARAMS else undocumented).append(path)
    if vague:
        found.append(finding("vague-param-name", "medium",
                             "Vague parameter name without a description: %s." % names_text(vague, mask)))
    if undocumented:
        share = (len(undocumented) + len(vague)) / float(len(params))
        found.append(finding("param-no-description", "medium" if share >= 0.5 else "low",
                             "Parameters without a description: %s." % names_text(undocumented, mask)))

    if valid_schema:
        objects = [schema] + [c for _p, _l, spec, _d in params for c, _ref in object_children(spec, schema)]
        missing = []
        for obj in objects:
            declared = obj.get("properties") or {}
            required = obj.get("required") if isinstance(obj.get("required"), list) else []
            missing.extend(r for r in required if isinstance(r, str) and r not in declared)
        if missing:
            found.append(finding("required-not-in-schema", "high",
                                 "Required but not defined under properties: %s." % names_text(missing, mask)))
        enums = big_enums(schema)
        if enums:
            found.append(finding("large-enum", "low", "Enum with more than %d values: %s." % (
                ENUM_MAX, ", ".join("%s (%d)" % (inline(path, 80, mask), n) for path, n in enums))))
        deepest = max([d for _p, _l, spec, d in params if object_children(spec, schema)] or [0])
        if deepest > DEPTH_MAX:
            found.append(finding("deep-nesting", "low", "The input nests objects %d levels deep; "
                                 "the limit here is %d." % (deepest, DEPTH_MAX)))

    date_gaps, id_gaps = [], []
    for path, leaf, spec, _depth in params:
        if not string_like(spec, schema):
            continue
        words = name_words(leaf)
        is_date = bool(set(words) & DATE_WORDS) or leaf.endswith("_at") or bool(re.search(r"[a-z]At$", leaf))
        is_id = bool(words) and words[-1] in ID_WORDS
        if not (is_date or is_id):
            continue
        if any(spec.get(k) not in (None, "", [], {}) for k in ("format", "pattern", "enum", "const",
                                                                "examples", "example", "default")):
            continue
        items = resolve(spec.get("items"), schema)
        if any(items.get(k) for k in ("format", "pattern", "enum", "examples")):
            continue
        text = " ".join([spec.get("description") or ""] + mentions(desc, leaf))
        if is_date and not FORMAT_HINT.search(text):
            date_gaps.append(path)
        elif is_id and not is_date and not (FORMAT_HINT.search(text) or PROVENANCE.search(text)):
            id_gaps.append(path)
    if date_gaps:
        found.append(finding("format-without-example", "medium",
                             "Date or time parameter with no format or example: %s." % names_text(date_gaps, mask)))
    if id_gaps:
        found.append(finding("format-without-example", "low",
                             "ID parameter that says neither where the value comes from nor what it looks "
                             "like: %s." % names_text(id_gaps, mask)))

    if tool.get("format") == "mcp":
        hints = tool.get("annotations") or {}
        if kind == "destructive":
            if hints.get("readOnlyHint") is True or hints.get("destructiveHint") is False:
                found.append(finding("hint-contradiction", "high",
                                     "The name says it deletes or overwrites, but the annotations say "
                                     "it is read-only or not destructive."))
            elif "destructiveHint" not in hints:
                found.append(finding("destructive-hint-missing", "low",
                                     "The name says it deletes or overwrites, and destructiveHint is not set."))
        elif kind == "read" and hints.get("readOnlyHint") is not True:
            found.append(finding("readonly-hint-missing", "low",
                                 "The name says it only reads, but readOnlyHint is not set to true."))

    if valid_schema and top and set(name_words(name)) & LIST_VERBS:
        if not any(is_limit_param(leaf, spec) for _p, leaf, spec, _d in top):
            found.append(finding("list-without-limit", "medium",
                                 "A list or search tool with no limit, page, or cursor parameter."))

    tokens = token_estimate(tool)
    if tokens > LARGE_TOKENS:
        found.append(finding("large-definition", "low",
                             "The definition is about %d tokens; most tools need under %d." % (tokens, LARGE_TOKENS)))

    score = max(0, 100 - sum(WEIGHTS[f["severity"]] for f in found))
    main = min(found, key=lambda f: SEVERITY_ORDER.index(f["severity"]))["check"] if found else None
    return {"name": safe_text(name, 128, mask), "score": score, "grade": grade_for(score), "tokens": tokens,
            "main_finding": main, "findings": found, "description": safe_text(desc, 300, mask),
            "params": [safe_text(path, 80, mask) for path, _l, _s, _d in params], "notes": notes}


def naming_style(name: str):
    if "_" in name:
        return "snake_case"
    if "-" in name:
        return "kebab-case"
    if "." in name:
        return "dot.case"
    if re.search(r"[a-z0-9][A-Z]", name):
        return "camelCase"
    return None


def folded_name(name: str) -> tuple:
    return tuple(sorted(SYNONYMS.get(w, w) for w in name_words(name)))


def description_similarity(a: str, b: str):
    wa, wb = content_words(a), content_words(b)
    if len(wa) < DESC_MIN_WORDS or len(wb) < DESC_MIN_WORDS:
        return None
    return len(wa & wb) / float(len(wa | wb))


def similar_pairs(tools: list):
    """(tool a, tool b, reason, similarity) for tools a model could mix up."""
    pairs = []
    for a, b in itertools.combinations(tools, 2):
        if a.get("name") == b.get("name"):
            continue
        same_name = folded_name(a.get("name", "")) == folded_name(b.get("name", ""))
        sim = description_similarity(a.get("description") or "", b.get("description") or "")
        if same_name:
            pairs.append((a, b, "the names mean the same thing", sim))
        elif sim is not None and sim >= DESC_SIMILAR:
            pairs.append((a, b, "the descriptions share %d%% of their words" % round(sim * 100), sim))
    return pairs


def lint_server(tools: list, extra_findings=(), mask=()) -> dict:
    """Grade every tool of one server, then the server as a whole."""
    tools = normalize_tools(tools) if tools and "format" not in tools[0] else list(tools)
    names = [t.get("name") or "" for t in tools]
    results = [lint_tool(t, siblings=names, mask=mask) for t in tools]
    found = list(extra_findings)
    dupes = sorted(n for n, c in Counter(names).items() if c > 1)
    if dupes:
        found.append(finding("duplicate-name", "high", "More than one tool is named: %s." % names_text(dupes, mask)))
    styles = Counter(s for s in map(naming_style, names) if s)
    if len(styles) > 1:
        main_style = styles.most_common(1)[0][0]
        odd = [n for n in names if naming_style(n) not in (None, main_style)]
        found.append(finding("inconsistent-naming", "low", "Tool names mix %s; not %s: %s." % (
            " and ".join(sorted(styles)), main_style, names_text(odd, mask))))
    for a, b, reason, _sim in similar_pairs(tools):
        found.append(finding("near-duplicate", "medium", "%s and %s look alike: %s." % (
            inline(a.get("name"), 80, mask), inline(b.get("name"), 80, mask), reason)))
    if results:
        mean = sum(r["score"] for r in results) / float(len(results))
        score = int(round(max(0.0, mean - min(SERVER_FINDING_CAP, SERVER_FINDING_COST * len(found)))))
        grade = grade_for(score)
    else:
        score, grade = None, "n/a"
    return {"score": score, "grade": grade, "tools": results, "findings": found,
            "tokens": sum(r["tokens"] for r in results)}


def unclear_count(results: list) -> int:
    return sum(1 for r in results if any(f["check"] in ("unclear-purpose", "missing-description")
                                         for f in r["findings"]))


# ---------------------------------------------------------------------------
# reports
# ---------------------------------------------------------------------------

def about(n: int) -> str:
    if n >= 1000:
        return "{:,}".format(int(round(n / 100.0)) * 100)
    return str(int(round(n / 10.0)) * 10) if n >= 10 else str(n)


def plural(n: int, word: str, many: str = "") -> str:
    return "%d %s" % (n, word if n == 1 else (many or word + "s"))


def unclear_phrase(n: int) -> str:
    return "none has" if n == 0 else ("1 has" if n == 1 else "%d have" % n)


def md_cell(text) -> str:
    return str(text).replace("|", "\\|").replace("\n", " ")


def worst_first(results: list) -> list:
    return sorted(results, key=lambda r: (r["score"], r["name"]))


def tool_rows(results: list, limit: int = 0) -> list:
    rows = ["| Tool | Grade | Tokens | Findings |", "|---|---|---|---|"]
    ordered = worst_first(results)
    for r in ordered[:limit] if limit else ordered:
        checks = ", ".join(f["check"] for f in r["findings"]) or "none"
        rows.append("| %s | %s (%d) | %d | %s |" % (code(r["name"], 128), r["grade"], r["score"], r["tokens"], checks))
    return rows


def fix_lines(results: list, count: int = 5) -> list:
    """results may carry a "label": the tool name already in inline code, with its server."""
    lines = []
    for r in [r for r in worst_first(results) if r["findings"]][:count]:
        lines.append("")
        lines.append("**%s** (%s, %d)" % (r.get("label") or code(r["name"], 128), r["grade"], r["score"]))
        for f in r["findings"]:
            lines.append("- %s, %s: %s Fix: %s" % (f["severity"], f["check"], f["message"], f["fix"]))
    return lines


FOOTER = ("Grades: each tool starts at 100 and loses 30 per high, 12 per medium, and 4 per low "
          "finding (A 90+, B 80+, C 70+, D 60+, F below 60); a server grade is the mean tool score "
          "minus 5 per server finding, at most 20. Tokens are estimates: JSON characters of the "
          "name, description, and input schema divided by 4. Rules and sources: references/smells.md.")


def lint_report(source: str, server: dict, listing=None, notes=(), mask=()) -> dict:
    results = server["tools"]
    unclear = unclear_count(results)
    by_check = Counter(f["check"] for r in results for f in r["findings"])
    by_check.update(f["check"] for f in server["findings"])
    if len(results) == 1:
        headline = ("1 tool grades %s (%d of 100) and adds about %s tokens of definitions to every "
                    "session that loads it%s" % (server["grade"], server["score"], about(server["tokens"]),
                                                 "; it has an unclear purpose." if unclear else "."))
    elif results:
        worst = worst_first(results)[0]
        headline = ("%s grade %s (%d of 100) and add about %s tokens of definitions to every "
                    "session that loads them; %s an unclear purpose, and the weakest is %s (%s)." % (
                        plural(len(results), "tool"), server["grade"], server["score"],
                        about(server["tokens"]), unclear_phrase(unclear), code(worst["name"], 128),
                        worst["grade"]))
    else:
        headline = "The server lists 0 tools, so there is nothing to grade."
    tool_notes = ["%s: %s" % (code(r["name"], 128), n) for r in results for n in r["notes"]]
    return {
        "checker": "tool-design-checker", "version": VERSION, "mode": "lint",
        "source": safe_text(source, 300, mask), "headline": headline,
        "summary": {"tools": len(results), "grade": server["grade"], "score": server["score"],
                    "tokens": server["tokens"], "unclear_purpose": unclear,
                    "findings_by_check": dict(sorted(by_check.items()))},
        "server_findings": server["findings"], "tools": results, "listing": listing,
        "notes": [safe_text(n, 300, mask) for n in notes] + tool_notes,
    }


def render_lint(report: dict) -> str:
    s = report["summary"]
    lines = ["**%s**" % report["headline"], "", "Source: %s" % code(report["source"], 300)]
    listing = report.get("listing")
    if listing:
        lines.append("Listed over the %s protocol (version %s) in %s, %.1f s." % (
            safe_text(listing["era"], 20), code(listing["protocol_version"], 40),
            plural(listing["pages"], "page"), listing["seconds"]))
    lines += ["", "| Grade | Score | Tools | Tokens (estimate) | Unclear purpose |", "|---|---|---|---|---|",
              "| %s | %s | %d | %s | %d |" % (s["grade"], s["score"] if s["score"] is not None else "n/a",
                                            s["tools"], "{:,}".format(s["tokens"]), s["unclear_purpose"])]
    if report["tools"]:
        lines += ["", "## Tools, weakest first", ""] + tool_rows(report["tools"])
        fixes = fix_lines(report["tools"])
        if fixes:
            lines += ["", "## Fixes for the weakest tools"] + fixes
    if report["server_findings"]:
        lines += ["", "## Server findings", ""]
        lines += ["- %s, %s: %s Fix: %s" % (f["severity"], f["check"], f["message"], f["fix"])
                  for f in report["server_findings"]]
    if report["notes"]:
        lines += ["", "## Notes", ""] + ["- %s" % n for n in report["notes"]]
    lines += ["", FOOTER]
    return "\n".join(lines) + "\n"


# ---------------------------------------------------------------------------
# installed: every configured server, per-harness load, collisions
# ---------------------------------------------------------------------------

COLLISION_FIX = ("Keep one of the two, turn one off in the harness where both load, or make each "
                 "description say what sets it apart.")


def is_unclear(result: dict) -> bool:
    return any(f["check"] in ("unclear-purpose", "missing-description") for f in result["findings"])


def tool_visible(entry: dict, tool_name: str) -> bool:
    """Apply one harness entry's tool filters (Codex, Gemini CLI, OpenCode)."""
    if entry["include_tools"] is not None and tool_name not in entry["include_tools"]:
        return False
    if entry["exclude_tools"] and tool_name in entry["exclude_tools"]:
        return False
    full = "%s_%s" % (entry["name"], tool_name)
    return not any(fnmatch.fnmatchcase(full, pattern) for pattern in entry["tool_globs_off"])


def cross_server_collisions(listed: list, mask=()) -> list:
    """listed: [(server key, server name, [(tool, lint result)])] for the servers one harness
    loads. Returns the tool pairs on different servers that a model could mix up."""
    flat = [(key, name, tool, result) for key, name, pairs in listed for tool, result in pairs]
    out = []
    for (ka, sa, ta, ra), (kb, sb, tb, rb) in itertools.combinations(flat, 2):
        if ka == kb:
            continue
        sim = description_similarity(ta.get("description") or "", tb.get("description") or "")
        if sim is not None and sim >= DESC_SIMILAR:
            reason = "the descriptions share %d%% of their words" % round(sim * 100)
        elif folded_name(ta["name"]) == folded_name(tb["name"]) and (is_unclear(ra) or is_unclear(rb)):
            reason = "the names mean the same thing and at least one description is unclear"
        else:
            continue
        out.append({"a": {"server": sa, "tool": safe_text(ta["name"], 128, mask)},
                    "b": {"server": sb, "tool": safe_text(tb["name"], 128, mask)},
                    "reason": reason, "fix": COLLISION_FIX, "_key": (ka, ta["name"], kb, tb["name"])})
    return out


def launch_entry(enabled: list) -> dict:
    """The entry to launch a server with: the harness that passes the smallest environment."""
    return min(enabled, key=lambda e: (mcp_configs.BASE_RANK.get(e["harness"], 2),
                                       mcp_configs.HARNESSES.index(e["harness"])))


def launch_server(server: dict, entry: dict, args, project: str, mask=()) -> dict:
    """List one server's tools. Returns fields to merge into the server record.
    mask: every secret known for this run, hidden in all text the server sends back."""
    try:
        return _launch(server, entry, args, project, list(mask))
    except Exception as exc:  # a server can answer in ways nobody planned for; keep the report going
        return {"status": "failed", "error": "unexpected %s while listing tools" % exc.__class__.__name__}


def _launch(server: dict, entry: dict, args, project: str, mask: list) -> dict:
    transport = server["transport"]
    if transport == "stdio":
        argv, env, cwd, secrets, missing = mcp_configs.launch_spec(entry, os.environ, project)
        mask = mask + secrets
        if not argv or not argv[0]:
            return {"status": "skipped", "error": "the entry has no command"}
        try:
            # Harnesses start servers from the session's folder, so relative paths resolve there.
            result = mcp_client.list_tools_stdio(argv, env=env, cwd=cwd or project, timeout=args.timeout,
                                                 mask=mask)
        except mcp_client.McpError as exc:
            hint = " These variables are not set in this shell: %s." % ", ".join(missing) if missing else ""
            return {"status": "failed", "error": str(exc) + hint}
    elif transport == "http":
        if not args.remote:
            return {"status": "skipped", "error": "remote server; add --remote to list it (that sends "
                                                  "requests to %s)" % mcp_configs.host_of(entry["url"])}
        import mcp_http  # network code loads only with --remote
        url, headers, secrets, missing = mcp_configs.remote_spec(entry, os.environ, project)
        mask = mask + secrets
        try:
            result = mcp_http.list_tools_http(url, headers=headers, timeout=args.timeout, mask=mask)
        except mcp_client.McpError as exc:
            hint = " These variables are not set in this shell: %s." % ", ".join(missing) if missing else ""
            return {"status": "failed", "error": str(exc) + hint}
    elif transport == "sse":
        return {"status": "skipped", "error": "the deprecated HTTP+SSE transport is not supported"}
    elif transport == "ws":
        return {"status": "skipped", "error": "the WebSocket transport is not supported"}
    else:
        return {"status": "skipped", "error": "the entry has neither a command nor a url"}
    tools = normalize_tools(result["tools"]) if result["tools"] else []
    extra = []
    if result.get("stdout_noise"):
        extra.append(finding("stdout-noise", "medium", "The server wrote %s that are not MCP messages "
                             "to stdout." % plural(result["stdout_noise"], "line")))
    graded = lint_server(tools, extra, mask)
    return {"status": "listed", "error": "", "_tools": tools, "grade": graded["grade"],
            "score": graded["score"], "tokens": graded["tokens"], "tools_count": len(tools),
            "tools": graded["tools"], "findings": graded["findings"],
            "listing": {k: safe_text(result.get(k), 40, mask) if isinstance(result.get(k), str) else result.get(k)
                        for k in ("era", "protocol_version", "pages", "seconds")},
            "_notes": result.get("notes", [])}


def final_redact(text: str, mask) -> str:
    """The last pass over a rendered report, markdown or JSON: every mask value of 6 or
    more characters is replaced as written, as JSON writes it, and as safe_text() shows it."""
    forms = set()
    for value in mask:
        if not value or len(value) < mcp_client.MIN_SECRET:
            continue
        shown = mcp_client.inert(value)
        forms.update(f for f in (value, shown, json.dumps(value)[1:-1], json.dumps(shown)[1:-1])
                     if len(f) >= mcp_client.MIN_SECRET)
    for form in sorted(forms, key=len, reverse=True):
        text = text.replace(form, "***")
    return text


def headline_for(servers: list, per_harness: dict, harnesses: tuple, launched: bool) -> tuple:
    """(headline, the harness it describes)."""
    names = mcp_configs.HARNESS_NAMES
    used = [h for h in harnesses if per_harness[h]["servers"]]
    if not servers:
        return ("0 MCP servers are configured for this folder in %s." %
                ", ".join(names[h] for h in harnesses), None)
    if not used:
        return ("You have %s configured, and none of them loads in a session here; the Status column "
                "says why." % plural(len(servers), "MCP server"), None)
    if not launched:
        top = max(used, key=lambda h: per_harness[h]["servers"])
        count = per_harness[top]["servers"]
        return ("You have %s configured across %s; %s loads %s in every session. Run again with --launch "
                "to count %s tools and tokens." % (
                    plural(len(servers), "MCP server"), plural(len(used), "harness", "harnesses"), names[top],
                    "it" if len(servers) == 1 else ("%d of them" % count), "its" if len(servers) == 1 else "their"),
                top)
    top = max(used, key=lambda h: (per_harness[h]["tokens"], per_harness[h]["servers"]))
    row = per_harness[top]
    headline = ("Your %s %s %s and about %s tokens into every %s session. %s an unclear purpose, and %s "
                "across servers." % (
                    plural(row["servers"], "MCP server"), "loads" if row["servers"] == 1 else "load",
                    plural(row["tools"], "tool"), about(row["tokens"]), names[top],
                    "1 tool has" if row["unclear_purpose"] == 1 else "%d tools have" % row["unclear_purpose"],
                    "1 pair collides" if row["collisions"] == 1 else "%d pairs collide" % row["collisions"]))
    if row["unlisted"]:
        headline += " %s could not be listed." % plural(row["unlisted"], "server")
    return headline, top


def run_mask(entries: list, launches: list, project: str) -> list:
    """Every secret known for this run: config values, header values and bare tokens,
    passwords inside URLs, and, for each server that will be launched, the expanded values
    and the secret-looking variables it will get. Collected before the first launch, so a
    server that echoes another server's secret is masked too."""
    values = set()
    for entry in entries:
        values.update(mcp_configs.config_secrets(entry, os.environ))
    for transport, entry in launches:
        if transport == "stdio":
            values.update(mcp_configs.launch_spec(entry, os.environ, project)[3])
        else:
            values.update(mcp_configs.remote_spec(entry, os.environ, project)[2])
    return sorted(v for v in values if len(v) >= mcp_client.MIN_SECRET)


def installed_report(args, harnesses: tuple) -> tuple:
    """(report, mask): the mask goes to final_redact() and never into the report."""
    project = os.path.abspath(args.project)
    home = os.path.expanduser("~")
    entries, notes = mcp_configs.collect(project=project, harnesses=harnesses,
                                         include_unapproved=args.include_unapproved)
    groups = mcp_configs.group(entries)
    if args.only:
        wanted = {n.strip() for n in args.only.split(",") if n.strip()}
        groups = [g for g in groups if wanted & set(g["names"])]
        found = {n for g in groups for n in g["names"]}
        notes.extend("no server named %s" % inline(n, 100) for n in sorted(wanted - found))
    plan = []
    for group in groups:
        enabled = [e for e in group["entries"] if e["enabled"]]
        plan.append((group, enabled, launch_entry(enabled) if enabled else group["entries"][0]))
    launches = [(g["transport"], first) for g, enabled, first in plan if enabled and args.launch and (
        g["transport"] == "stdio" or (g["transport"] == "http" and args.remote))]
    mask = run_mask(entries, launches, project)
    servers = []
    for group, enabled, first in plan:
        record = {
            "name": safe_text(group["names"][0], 100, mask),
            "names": [safe_text(n, 100, mask) for n in group["names"]],
            "transport": group["transport"],
            "command": safe_text(mcp_configs.display_command(first, mask), 300, mask),
            "env_names": sorted({safe_text(k, 80, mask) for e in group["entries"] for k in e["env"]}),
            "header_names": sorted({safe_text(k, 80, mask) for e in group["entries"]
                                    for k in list(e["headers"]) + list(e["header_env"])}),
            "configured_in": [{"harness": e["harness"], "scope": e["scope"],
                               "config_path": safe_text(e["config_path"], 200, mask),
                               "name": safe_text(e["name"], 100, mask), "enabled": e["enabled"],
                               "status": safe_text(e["status"], 200, mask) or "on",
                               "project_file": e["project_file"]} for e in group["entries"]],
            "status": "not launched", "error": "", "grade": None, "score": None, "tokens": None,
            "tools_count": None, "tools": [], "findings": [], "listing": None,
        }
        if not enabled:
            record.update(status="skipped", error="turned off everywhere it is configured")
        elif args.launch:
            record.update(launch_server(group, first, args, project, mask))
            record["error"] = safe_text(record["error"], 300, mask)
        notes.extend("%s: %s" % (code(record["name"], 100), n) for n in record.pop("_notes", []))
        record["_group"], record["_enabled"] = group, enabled
        servers.append(record)

    per_harness, collisions = {}, {}
    for harness in harnesses:
        counted = args.launch
        row = {"servers": 0, "tools": 0 if counted else None, "tokens": 0 if counted else None,
               "unclear_purpose": 0 if counted else None, "collisions": 0 if counted else None, "unlisted": 0,
               "config_files": sorted({safe_text(e["config_path"], 200, mask) for e in entries
                                       if e["harness"] == harness})}
        visible = []
        for s in servers:
            here = [e for e in s["_group"]["entries"] if e["harness"] == harness and e["enabled"]]
            if not here:
                continue
            row["servers"] += 1
            if s["status"] != "listed":
                row["unlisted"] += 1
                continue
            pairs = [(t, r) for t, r in zip(s["_tools"], s["tools"]) if tool_visible(here[0], t["name"])]
            row["tools"] += len(pairs)
            row["tokens"] += sum(r["tokens"] for _t, r in pairs)
            row["unclear_purpose"] += sum(1 for _t, r in pairs if is_unclear(r))
            visible.append((s["_group"]["key"], s["name"], pairs))
        if counted:
            found = cross_server_collisions(visible, mask)
            row["collisions"] = len(found)
            for pair in found:
                key = pair.pop("_key")
                collisions.setdefault(key, dict(pair, harnesses=[]))["harnesses"].append(harness)
        per_harness[harness] = row

    headline, top = headline_for(servers, per_harness, harnesses, args.launch)
    ready = [s for s in servers if s["transport"] == "stdio" and s["_enabled"]]
    launch_plan = {"stdio": len(ready),
                   "from_project_files": [s["name"] for s in ready if any(e["project_file"] for e in s["_enabled"])],
                   "approved_by_project_settings": [s["name"] for s in ready
                                                    if any(e["approved_in_project"] for e in s["_enabled"])]}
    config_findings = [dict(f, server=safe_text(e["name"], 100, mask), harness=e["harness"],
                            config_path=safe_text(e["config_path"], 200, mask)) for e in entries for f in e["issues"]]
    listed = [s for s in servers if s["status"] == "listed"]
    top_row = per_harness[top] if top else {}
    for s in servers:
        for key in ("_group", "_enabled", "_tools"):
            s.pop(key, None)
    return {
        "checker": "tool-design-checker", "version": VERSION, "mode": "installed",
        "project": safe_text(mcp_configs.shown(project, home), 200, mask), "headline": headline,
        "launched": bool(args.launch), "remote": bool(args.remote),
        "summary": {"servers": len(servers), "listed": len(listed), "harness": top,
                    "tools": top_row.get("tools"), "tokens": top_row.get("tokens"),
                    "unclear_purpose": top_row.get("unclear_purpose"), "collisions": top_row.get("collisions")},
        "launch_plan": launch_plan, "harnesses": per_harness, "servers": servers,
        "collisions": list(collisions.values()), "config_findings": config_findings,
        # Each note is fixed text with any untrusted value already inside inline code.
        "notes": notes,
    }, mask


def render_installed(report: dict) -> str:
    names = mcp_configs.HARNESS_NAMES
    lines = ["**%s**" % report["headline"], "", "Folder: %s" % code(report["project"], 200)]
    plan = report["launch_plan"]
    if not report["launched"] and plan["stdio"]:
        parts = []
        if plan["from_project_files"]:
            parts.append("%d from files inside this project: %s" % (
                len(plan["from_project_files"]), ", ".join(code(n, 100) for n in plan["from_project_files"])))
        if plan["approved_by_project_settings"]:
            parts.append("approved by settings files inside this project: %s" %
                         ", ".join(code(n, 100) for n in plan["approved_by_project_settings"]))
        lines.append("With --launch, %s would start%s." % (
            plural(plan["stdio"], "stdio server"), " (%s)" % "; ".join(parts) if parts else ""))
    lines += ["", "| Harness | Servers | Tools | Tokens (estimate) | Unclear purpose | Collisions | Config files read |",
              "|---|---|---|---|---|---|---|"]
    for harness, row in report["harnesses"].items():
        counted = row["tools"] is not None
        tools = str(row["tools"]) if counted else "not counted"
        if counted and row["unlisted"]:
            tools += " (%d not listed)" % row["unlisted"]
        lines.append("| %s | %d | %s | %s | %s | %s | %s |" % (
            names[harness], row["servers"], tools, "{:,}".format(row["tokens"]) if counted else "not counted",
            row["unclear_purpose"] if counted else "-", row["collisions"] if counted else "-",
            ", ".join(code(p, 200) for p in row["config_files"]) or "none found"))
    if report["servers"]:
        lines += ["", "## Servers", "", "| Server | Where | Runs | Status | Grade | Tools | Tokens |",
                  "|---|---|---|---|---|---|---|"]
        for s in report["servers"]:
            where = "; ".join("%s %s%s%s" % (names[c["harness"]], c["scope"],
                                             " (project file)" if c["project_file"] else "",
                                             "" if c["enabled"] else " (off: %s)" % c["status"])
                              for c in s["configured_in"])
            runs = code(s["command"], 300)
            if s["env_names"]:
                runs += " (env: %s)" % ", ".join(code(n, 80) for n in s["env_names"])
            status = s["status"] + (": " + code(s["error"], 300) if s["error"] else "")
            grade = "%s (%d)" % (s["grade"], s["score"]) if s["score"] is not None else "-"
            lines.append("| %s | %s | %s | %s | %s | %s | %s |" % (
                ", ".join(code(n, 100) for n in s["names"]), md_cell(where), runs, status, grade,
                "-" if s["tools_count"] is None else s["tools_count"],
                "-" if s["tokens"] is None else "{:,}".format(s["tokens"])))
    if report["collisions"]:
        lines += ["", "## Tools that collide across servers", ""]
        lines += ["- %s (%s) and %s (%s), in %s: %s. Fix: %s" % (
            code(c["a"]["tool"], 128), code(c["a"]["server"], 100), code(c["b"]["tool"], 128),
            code(c["b"]["server"], 100),
            ", ".join(names[h] for h in c["harnesses"]), c["reason"], c["fix"]) for c in report["collisions"]]
    tagged = [dict(r, label="%s (%s)" % (code(r["name"], 128), code(s["name"], 100)))
              for s in report["servers"] for r in s["tools"]]
    fixes = fix_lines(tagged)
    if fixes:
        lines += ["", "## Fixes for the weakest tools"] + fixes
    server_findings = [(s["name"], f) for s in report["servers"] for f in s["findings"]]
    if server_findings or report["config_findings"]:
        lines += ["", "## Server and config findings", ""]
        lines += ["- %s: %s, %s: %s Fix: %s" % (code(n, 100), f["severity"], f["check"], f["message"], f["fix"])
                  for n, f in server_findings]
        lines += ["- %s in %s (%s): %s, %s: %s Fix: %s" % (
            code(f["server"], 100), names[f["harness"]], code(f["config_path"], 200), f["severity"], f["check"],
            f["message"], f["fix"])
            for f in report["config_findings"]]
    if report["notes"]:
        lines += ["", "## Notes", ""] + ["- %s" % n for n in report["notes"]]
    lines += ["", "Env values and header values are never shown; only their names. " + FOOTER]
    return "\n".join(lines) + "\n"


def run_installed(args) -> int:
    chosen = mcp_configs.HARNESSES if args.harness == "all" else tuple(
        h.strip() for h in args.harness.split(",") if h.strip())
    unknown = [h for h in chosen if h not in mcp_configs.HARNESSES]
    if unknown or not chosen:
        print("error: unknown harness %s; use all or: %s" % (
            inline(", ".join(unknown), 100), ", ".join(mcp_configs.HARNESSES)), file=sys.stderr)
        return 2
    if not os.path.isdir(args.project):
        print("error: %s is not a folder" % inline(args.project, 200), file=sys.stderr)
        return 2
    report, mask = installed_report(args, chosen)
    emit(json.dumps(report, indent=2) + "\n" if args.json else render_installed(report), args.out, mask)
    grades = [s["grade"] for s in report["servers"] if s["status"] == "listed"]
    return 1 if args.fail_under and any(below(g, args.fail_under) for g in grades) else 0


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

GRADE_ORDER = "ABCDF"
SHELL_TOKEN = re.compile(r"^[;&|<>()]+$")
ENV_PREFIX = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=")


def below(grade: str, floor: str) -> bool:
    return grade in GRADE_ORDER and GRADE_ORDER.index(grade) > GRADE_ORDER.index(floor)


def emit(text: str, out_path, mask=()) -> None:
    text = final_redact(text, mask)
    if out_path:
        with open(out_path, "w", encoding="utf-8") as fh:
            fh.write(text)
        print("wrote %s" % out_path, file=sys.stderr)
    else:
        sys.stdout.write(text)


def plain_command(line: str):
    """The argv of one plain command, or None when the line uses shell syntax
    (&&, ;, |, redirects, subshells) or starts with VAR=value."""
    lexer = shlex.shlex(line, posix=True, punctuation_chars=True)
    lexer.whitespace_split = True
    tokens = list(lexer)
    if not tokens or any(SHELL_TOKEN.match(t) for t in tokens) or ENV_PREFIX.match(tokens[0]):
        return None
    return shlex.split(line)


def run_lint(args) -> int:
    listing, notes, extra, mask = None, [], [], []
    if args.tools:
        source = args.tools
        try:
            if args.tools == "-":
                data = json.load(sys.stdin)
            else:
                with open(args.tools, encoding="utf-8") as fh:
                    data = json.load(fh)
            tools = normalize_tools(data)
        except (OSError, ValueError) as exc:
            print("error: cannot read tools from %s: %s" % (inline(args.tools, 200), inline(exc, 200)),
                  file=sys.stderr)
            return 2
    else:
        try:
            argv = plain_command(args.server)
        except ValueError as exc:
            print("error: cannot parse --server: %s" % inline(exc, 200), file=sys.stderr)
            return 2
        if argv is None:
            print("error: --server takes one plain command, with no &&, ;, |, redirects, or VAR=value "
                  "prefix. Put setup steps in a script, and use --cwd to run it from another folder.",
                  file=sys.stderr)
            return 2
        if not os.path.isdir(args.cwd):
            print("error: --cwd %s is not a folder" % inline(args.cwd, 200), file=sys.stderr)
            return 2
        source = args.server
        # The server inherits this shell's environment: its secret-looking values, and any
        # value given to a secret-looking flag or written into a URL, are masked everywhere.
        mask = mcp_configs.secret_env_values(os.environ) + mcp_configs.flag_secrets(argv[1:])
        for word in argv:
            mask.extend(mcp_configs.url_secrets(word))
        mask = sorted({v for v in mask if len(v) >= mcp_client.MIN_SECRET})
        try:
            result = mcp_client.list_tools_stdio(argv, cwd=args.cwd, timeout=args.timeout, mask=mask)
        except mcp_client.McpError as exc:
            print(final_redact("error: could not list the server's tools (%s): %s" % (
                exc.kind, inline(exc, 400, mask)), mask), file=sys.stderr)
            return 2
        except Exception as exc:  # anything else a server can cause
            print("error: could not list the server's tools (unexpected %s)" % exc.__class__.__name__,
                  file=sys.stderr)
            return 2
        tools = normalize_tools(result["tools"]) if result["tools"] else []
        listing = {k: safe_text(result[k], 40, mask) if isinstance(result[k], str) else result[k]
                   for k in ("era", "protocol_version", "pages", "stdout_noise", "seconds")}
        notes.extend(result["notes"])
        if result["stdout_noise"]:
            extra.append(finding("stdout-noise", "medium", "The server wrote %s that are not MCP "
                                 "messages to stdout." % plural(result["stdout_noise"], "line")))
    report = lint_report(mcp_client.redact(source), lint_server(tools, extra, mask), listing, notes, mask)
    emit(json.dumps(report, indent=2) + "\n" if args.json else render_lint(report), args.out, mask)
    return 1 if args.fail_under and below(report["summary"]["grade"], args.fail_under) else 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Grade MCP tool definitions: purpose, parameters, annotations, and token size.")
    sub = parser.add_subparsers(dest="command")
    lint = sub.add_parser("lint", help="grade one tool list (a file, or a server you launch)")
    src = lint.add_mutually_exclusive_group(required=True)
    src.add_argument("--tools", help="JSON file: MCP tools/list result, OpenAI or Anthropic tool list ('-' for stdin)")
    src.add_argument("--server", help="one plain stdio server command, in quotes (no &&, ;, |, redirects, "
                                      "or VAR=value prefix)")
    lint.add_argument("--cwd", default=".", help="folder to start the --server command in (default: the current folder)")
    lint.add_argument("--timeout", type=float, default=20.0, help="seconds to wait for the server (default 20)")
    lint.add_argument("--fail-under", choices=list("ABCD"), help="exit 1 when the server grade is below this")
    lint.add_argument("--json", action="store_true", help="print JSON instead of markdown")
    lint.add_argument("--out", help="write the report to this file")
    inst = sub.add_parser("installed", help="find the MCP servers your harnesses load; grade them with --launch")
    inst.add_argument("--harness", default="all",
                      help="all (default), or a comma list of: " + ", ".join(mcp_configs.HARNESSES))
    inst.add_argument("--project", default=".", help="folder whose sessions to check (default: current folder)")
    inst.add_argument("--only", help="only these servers, by config name (comma list)")
    inst.add_argument("--launch", action="store_true",
                      help="start each configured stdio server to list its tools (ask the user first)")
    inst.add_argument("--remote", action="store_true",
                      help="with --launch, also list Streamable HTTP servers over the network")
    inst.add_argument("--include-unapproved", action="store_true",
                      help="also count and launch .mcp.json servers not yet approved in Claude Code")
    inst.add_argument("--timeout", type=float, default=20.0, help="seconds per server (default 20)")
    inst.add_argument("--fail-under", choices=list("ABCD"), help="exit 1 when a server grade is below this")
    inst.add_argument("--json", action="store_true", help="print JSON instead of markdown")
    inst.add_argument("--out", help="write the report to this file")
    return parser


def main(argv=None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.command == "lint":
        return run_lint(args)
    if args.command == "installed":
        return run_installed(args)
    parser.print_help()
    return 2


if __name__ == "__main__":
    mcp_client.exit_on_signals()
    sys.exit(main())
