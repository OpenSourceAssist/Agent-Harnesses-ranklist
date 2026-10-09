#!/usr/bin/env python3
"""Find the MCP servers configured for Claude Code, Codex, Gemini CLI, Cursor, and OpenCode.

Reads each harness's MCP config files (locations and rules with sources are in
references/mcp-configs.md), applies each harness's own precedence, trust,
approval, and on/off rules, and groups the same server configured in several
harnesses. It never starts anything; launch_spec() only builds the command
line and environment, and tools_check.py decides whether to run it.

Usage:
    python3 mcp_configs.py [--project .] [--harness all]

Prints the servers found, with commands shown with secret-looking values
masked. Env values and header values are never printed. Standard library
only; Codex's config.toml needs Python 3.11+ (tomllib) and is skipped with a
note on older Pythons.
"""

from __future__ import annotations

import argparse
import glob
import json
import os
import re
import stat
import sys

try:
    import tomllib
except ImportError:  # Python 3.9 and 3.10
    tomllib = None

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import mcp_client  # noqa: E402
from mcp_client import inline  # noqa: E402

HARNESSES = ("claude-code", "codex", "gemini-cli", "cursor", "opencode")
HARNESS_NAMES = {"claude-code": "Claude Code", "codex": "Codex", "gemini-cli": "Gemini CLI",
                 "cursor": "Cursor", "opencode": "OpenCode"}
# Claude Code reads these as empty in remote url and headers (facts file Q4.1).
CLAUDE_REMOTE_BLANK = {"ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "AWS_BEARER_TOKEN_BEDROCK",
                       "HTTPS_PROXY", "NPM_TOKEN"}
CLAUDE_NAME = re.compile(r"^[A-Za-z0-9_-]+$")
UNAPPROVED = "not yet approved in Claude Code"
# The environment Codex gives a stdio server (openai/codex codex-rs/rmcp-client/src/utils.rs),
# before the entry's env_vars and env.
CODEX_BASE_ENV = ("HOME", "LOGNAME", "PATH", "SHELL", "USER", "__CF_USER_TEXT_ENCODING", "LANG",
                  "LC_ALL", "TERM", "TMPDIR", "TZ")
# Gemini CLI drops inherited variables whose names contain one of these.
GEMINI_DROPPED = ("TOKEN", "SECRET", "PASSWORD", "KEY", "AUTH", "CREDENTIAL", "PRIVATE", "CERT")
# The smaller the base environment, the lower the rank: a server enabled in several
# harnesses is launched the way the most careful one would start it.
BASE_RANK = {"codex": 0, "gemini-cli": 1}
SECRET_WORDS = {"key", "token", "secret", "password", "passwd", "pwd", "credential", "credentials",
                "bearer", "pat", "apikey"}
SECRET_SHORT_FLAGS = {"-k"}
# Path words common in MCP URLs. Any other path segment may be a token, so it is masked.
COMMON_SEGMENTS = {"api", "apis", "mcp", "sse", "rpc", "jsonrpc", "stream", "streamable", "http", "https",
                   "message", "messages", "events", "server", "servers", "tools", "public", "rest",
                   "gateway", "proxy", "ws", "graphql", "beta", "alpha", "latest", "remote", "hosted",
                   "service", "services", "v", "mcp-server", "mcp_server"}
VERSION_SEGMENT = re.compile(r"^v[0-9]{1,3}[a-z]{0,6}$")
# Values of these inherited variables are treated as secrets when a server gets them.
SECRET_ENV_NAME = re.compile(r"(?i)TOKEN|KEY|SECRET|PASSW|AUTH|CREDENTIAL|PRIVATE|CERT|COOKIE|SESSION")
MAX_CONFIG_BYTES = 16 << 20  # far above any real config; stops /dev/zero-style reads
MAX_SECRET_FILE_BYTES = 1 << 20
URL_USERINFO = re.compile(r"^[A-Za-z][A-Za-z0-9+.-]*://([^/?#@]*)@")


class ConfigError(Exception):
    def __init__(self, path, reason):
        super().__init__(reason)
        self.path = path


def read_regular(path, limit):
    """The bytes of a regular file of at most `limit` bytes; None when it does not exist.
    FIFOs, devices (a link to /dev/zero), and oversized files raise ConfigError unread."""
    try:
        info = os.stat(path)
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise ConfigError(path, "cannot be read (%s)" % exc.__class__.__name__)
    if not stat.S_ISREG(info.st_mode):
        raise ConfigError(path, "is not a regular file")
    try:
        with open(path, "rb") as fh:
            data = fh.read(limit + 1)
    except OSError as exc:
        raise ConfigError(path, "cannot be read (%s)" % exc.__class__.__name__)
    if len(data) > limit:
        raise ConfigError(path, "is larger than %s" % size_text(limit))
    return data


def size_text(limit):
    return "%d MB" % (limit >> 20) if limit >= 1 << 20 else "%d bytes" % limit


def load_json(path):
    """Parse JSON that may carry // or /* */ comments and trailing commas (JSONC)."""
    data = read_regular(path, MAX_CONFIG_BYTES)
    if data is None:
        return None
    text = data.decode("utf-8-sig", "replace")
    try:
        return json.loads(text)
    except ValueError:
        pass
    out, i, n, in_str = [], 0, len(text), False
    while i < n:
        c = text[i]
        if in_str:
            out.append(c)
            if c == "\\" and i + 1 < n:
                out.append(text[i + 1])
                i += 2
                continue
            in_str = c != '"'
            i += 1
            continue
        if c == '"':
            in_str = True
        elif text.startswith("//", i):
            j = text.find("\n", i)
            i = n if j < 0 else j
            continue
        elif text.startswith("/*", i):
            j = text.find("*/", i + 2)
            i = n if j < 0 else j + 2
            continue
        out.append(c)
        i += 1
    try:
        return json.loads(re.sub(r",(\s*[}\]])", r"\1", "".join(out)))
    except ValueError as exc:
        raise ConfigError(path, "is not valid JSON (%s)" % str(exc).split(":")[0])


def load_config(path):
    """A JSON config file as a dict; None when the file does not exist."""
    data = load_json(path)
    if data is not None and not isinstance(data, dict):
        raise ConfigError(path, "is not a JSON object")
    return data


def load_toml(path):
    data = read_regular(path, MAX_CONFIG_BYTES)
    if data is None:
        return None
    try:
        return tomllib.loads(data.decode("utf-8", "replace"))
    except ValueError as exc:
        raise ConfigError(path, "is not valid TOML (%s)" % exc.__class__.__name__)


def shown(path, home):
    home = home.rstrip(os.sep)
    return "~" + path[len(home):] if path == home or path.startswith(home + os.sep) else path


def as_dict(value):
    return value if isinstance(value, dict) else {}


def as_list(value):
    return [str(v) for v in value] if isinstance(value, list) else []


def as_map(value):
    return {str(k): str(v) for k, v in value.items()} if isinstance(value, dict) else {}


def merge(base, over):
    """Merge two config objects key by key; nested objects merge too, other values replace."""
    out = dict(base)
    for key, value in over.items():
        out[key] = merge(out[key], value) if isinstance(value, dict) and isinstance(out.get(key), dict) else value
    return out


def new_entry(harness, scope, config_path, name, **kw):
    entry = {"harness": harness, "scope": scope, "config_path": config_path, "name": name,
             "transport": "stdio", "command": "", "args": [], "env": {}, "env_vars": [], "cwd": None,
             "env_file": None, "url": "", "headers": {}, "header_env": {}, "bearer_env": "",
             "enabled": True, "status": "", "include_tools": None, "exclude_tools": None,
             "tool_globs_off": [], "expand": None, "config_dir": None, "project_file": False,
             "approved_in_project": False, "issues": []}
    entry.update(kw)
    return entry


def issue(check, severity, message, fix):
    return {"check": check, "severity": severity, "message": message, "fix": fix}


def ancestors(path):
    path = os.path.realpath(path)
    while True:
        yield path
        parent = os.path.dirname(path)
        if parent == path:
            return
        path = parent


# ---------------------------------------------------------------------------
# one reader per harness: each returns (entries, notes)
# ---------------------------------------------------------------------------

def claude_approvals(settings_dir, project, local):
    """(approvals, disabled names): approvals maps a server name, or "*" for
    enableAllProjectMcpServers, to the places that approve it. Places inside the
    project are named by their file, so a repository that approves its own
    servers can be told apart."""
    layers = [("home .claude.json", local)]
    for label, path in (("user settings", os.path.join(settings_dir, "settings.json")),
                        (".claude/settings.json", os.path.join(project, ".claude", "settings.json")),
                        (".claude/settings.local.json", os.path.join(project, ".claude", "settings.local.json"))):
        try:
            layers.append((label, load_config(path) or {}))
        except ConfigError:
            continue
    approvals, disabled = {}, set()
    for label, layer in layers:
        for name in as_list(layer.get("enabledMcpjsonServers")):
            approvals.setdefault(name, []).append(label)
        if layer.get("enableAllProjectMcpServers") is True:
            approvals.setdefault("*", []).append(label)
        disabled |= set(as_list(layer.get("disabledMcpjsonServers")))
    return approvals, disabled


def read_claude(home, project, environ, opts):
    entries, notes = [], []
    cfg_dir = environ.get("CLAUDE_CONFIG_DIR")
    path = os.path.join(home, ".claude.json")
    if cfg_dir and os.path.exists(os.path.join(cfg_dir, ".claude.json")):
        path = os.path.join(cfg_dir, ".claude.json")
    data = load_config(path) or {}
    projects = as_dict(data.get("projects"))
    here = {project, os.path.realpath(project), os.path.abspath(project)}
    local = as_dict(next((projects[k] for k in projects if k in here), {}))
    mcp_path = os.path.join(project, ".mcp.json")
    mcp_json = load_config(mcp_path) or {}
    approvals, disabled = claude_approvals(cfg_dir or os.path.join(home, ".claude"), project, local)
    layers = (("local", local.get("mcpServers"), path),
              ("project", mcp_json.get("mcpServers"), mcp_path),
              ("user", data.get("mcpServers"), path))
    taken = {}
    for scope, servers, source in layers:
        for name, spec in as_dict(servers).items():
            if not isinstance(spec, dict):
                continue
            kind = str(spec.get("type") or "stdio")
            entry = new_entry("claude-code", scope, shown(source, home), name,
                              transport=kind if kind in ("stdio", "http", "sse", "ws") else "unknown",
                              command=str(spec.get("command") or ""), args=as_list(spec.get("args")),
                              env=as_map(spec.get("env")), url=str(spec.get("url") or ""),
                              headers=as_map(spec.get("headers")),
                              expand="claude" if scope == "project" else None, project_file=scope == "project")
            if kind == "stdio" and entry["url"] and not entry["command"]:
                entry["issues"].append(issue(
                    "url-without-type", "medium",
                    "This entry has a url but no type, so Claude Code reads it as a stdio server "
                    "and it cannot start.",
                    'Add "type": "http" (or "sse" for an older server) to the entry.'))
            if not CLAUDE_NAME.match(name):
                entry["issues"].append(issue(
                    "invalid-server-name", "low",
                    "Claude Code server names use only letters, digits, - and _.",
                    "Rename the server with only letters, digits, hyphens, and underscores."))
            approved_by = approvals.get(name, []) + approvals.get("*", [])
            if name in taken:
                entry.update(enabled=False, status="replaced by the %s entry with the same name" % taken[name])
            elif scope == "project" and name in disabled:
                entry.update(enabled=False, status="disabled for this project in Claude Code")
            elif scope == "project" and not (approved_by or opts.get("include_unapproved")):
                entry.update(enabled=False, status=UNAPPROVED)
            else:
                taken[name] = scope
                if scope == "project" and not approved_by:
                    entry["status"] = UNAPPROVED + "; included by --include-unapproved"
                elif scope == "project" and all(a.startswith(".claude/") for a in approved_by):
                    entry.update(status="approved by %s in this project" % " and ".join(sorted(set(approved_by))),
                                 approved_in_project=True)
            entries.append(entry)
    others = sum(1 for k, v in projects.items() if k not in here and isinstance(v, dict) and v.get("mcpServers"))
    if others:
        notes.append("Claude Code: %d other project folder%s in %s %s its own local MCP servers; "
                     "pass --project to check one of them." % (
                         others, "" if others == 1 else "s", inline(shown(path, home), 200),
                         "has" if others == 1 else "have"))
    return entries, notes


def codex_entry(name, spec, scope, source, home, **kw):
    url = str(spec.get("url") or "")
    return new_entry("codex", scope, shown(source, home), name,
                     transport="http" if url else "stdio", command=str(spec.get("command") or ""),
                     args=as_list(spec.get("args")), env=as_map(spec.get("env")),
                     env_vars=as_list(spec.get("env_vars")),
                     cwd=spec.get("cwd") if isinstance(spec.get("cwd"), str) else None, url=url,
                     headers=as_map(spec.get("http_headers")), header_env=as_map(spec.get("env_http_headers")),
                     bearer_env=str(spec.get("bearer_token_env_var") or ""),
                     enabled=spec.get("enabled", True) is not False,
                     status="" if spec.get("enabled", True) is not False else "enabled = false in the config",
                     include_tools=as_list(spec["enabled_tools"]) if isinstance(spec.get("enabled_tools"), list) else None,
                     exclude_tools=as_list(spec.get("disabled_tools")) or None, project_file=scope == "project", **kw)


def read_codex(home, project, environ, opts):
    if tomllib is None:
        return [], ["Codex: config.toml needs Python 3.11 or newer (tomllib); Codex servers were "
                    "skipped. Run the checker with python3.11 or newer to include them."]
    codex_home = environ.get("CODEX_HOME") or os.path.join(home, ".codex")
    user_path = os.path.join(codex_home, "config.toml")
    user = load_toml(user_path) or {}
    trust = None
    projects = as_dict(user.get("projects"))
    rules = {os.path.realpath(os.path.expanduser(k)): v.get("trust_level")
             for k, v in projects.items() if isinstance(v, dict)}
    for folder in ancestors(project):
        if folder in rules:
            trust = rules[folder]
            break
    entries, by_name = [], {}
    for name, spec in as_dict(user.get("mcp_servers")).items():
        if isinstance(spec, dict):
            by_name[name] = codex_entry(name, spec, "user", user_path, home)
    proj_path = os.path.join(project, ".codex", "config.toml")
    proj = load_toml(proj_path) or {}
    for name, spec in as_dict(proj.get("mcp_servers")).items():
        if not isinstance(spec, dict):
            continue
        entry = codex_entry(name, spec, "project", proj_path, home)
        if trust != "trusted":
            entry.update(enabled=False, status="Codex loads project config only in trusted projects, "
                                                "and this folder is not trusted")
            entries.append(entry)
            continue
        if name in by_name:
            by_name[name].update(enabled=False, status="replaced by the project entry with the same name")
            entries.append(by_name.pop(name))
        by_name[name] = entry
    return entries + list(by_name.values()), []


def gemini_trusted(rules, project):
    """True, False, or None (no rule) for a folder, from trustedFolders.json rules."""
    path, best = os.path.realpath(project), None
    for rule_path, value in (rules or {}).items():
        target = os.path.realpath(os.path.expanduser(str(rule_path)))
        if value == "TRUST_PARENT":
            target = os.path.dirname(target)
        if path == target or path.startswith(target.rstrip(os.sep) + os.sep):
            if best is None or len(target) > best[0]:
                best = (len(target), value != "DO_NOT_TRUST")
    return None if best is None else best[1]


def gemini_entry(name, spec, scope, source, home):
    url, http_url = str(spec.get("url") or ""), str(spec.get("httpUrl") or "")
    transport = "stdio" if spec.get("command") else ("http" if http_url else ("sse" if url else "unknown"))
    entry = new_entry("gemini-cli", scope, shown(source, home), name, transport=transport,
                      command=str(spec.get("command") or ""), args=as_list(spec.get("args")),
                      env=as_map(spec.get("env")), cwd=spec.get("cwd") if isinstance(spec.get("cwd"), str) else None,
                      url=http_url or url, headers=as_map(spec.get("headers")), expand="gemini",
                      include_tools=as_list(spec["includeTools"]) if isinstance(spec.get("includeTools"), list) else None,
                      exclude_tools=as_list(spec.get("excludeTools")) or None, project_file=scope == "project")
    if "_" in name:
        entry["issues"].append(issue(
            "underscore-in-server-name", "low",
            "Gemini CLI names MCP tools mcp_<server>_<tool> and its policy rules split at the first "
            "_ after mcp_, so policy rules for this server will not match.",
            "Rename the server without underscores, for example %s." % inline(name.replace("_", "-"), 100)))
    return entry


def read_gemini(home, project, environ, opts):
    user_path = os.path.join(home, ".gemini", "settings.json")
    proj_path = os.path.join(project, ".gemini", "settings.json")
    user = load_config(user_path) or {}
    trust_rules_path = environ.get("GEMINI_CLI_TRUSTED_FOLDERS_PATH") or \
        os.path.join(home, ".gemini", "trustedFolders.json")
    trust_on = as_dict(as_dict(user.get("security")).get("folderTrust")).get("enabled", True) is not False
    trusted = gemini_trusted(load_config(trust_rules_path), project) if trust_on else True
    proj = (load_config(proj_path) or {}) if trusted else {}
    by_name = {}
    for ext in sorted(glob.glob(os.path.join(home, ".gemini", "extensions", "*", "gemini-extension.json"))):
        try:
            data = load_config(ext) or {}
        except ConfigError:
            continue
        for name, spec in as_dict(data.get("mcpServers")).items():
            if isinstance(spec, dict):
                by_name[name] = gemini_entry(name, spec, "extension", ext, home)
    entries = []
    for scope, data, source in (("user", user, user_path), ("project", proj, proj_path)):
        for name, spec in as_dict(data.get("mcpServers")).items():
            if not isinstance(spec, dict):
                continue
            if name in by_name:
                by_name[name].update(enabled=False, status="replaced by the %s entry with the same name" % scope)
                entries.append(by_name.pop(name))
            by_name[name] = gemini_entry(name, spec, scope, source, home)
    mcp = as_dict(proj.get("mcp")) if isinstance(proj.get("mcp"), dict) else as_dict(user.get("mcp"))
    allowed = as_list(mcp.get("allowed")) if isinstance(mcp.get("allowed"), list) else None
    excluded = set(as_list(mcp.get("excluded")))
    for name, entry in by_name.items():
        if not trusted:
            entry.update(enabled=False, status="Gemini CLI loads no MCP servers in a folder you have "
                                               "not trusted")
        elif allowed is not None and name not in allowed:
            entry.update(enabled=False, status="not in mcp.allowed")
        elif name in excluded:
            entry.update(enabled=False, status="listed in mcp.excluded")
    return entries + list(by_name.values()), []


def read_cursor(home, project, environ, opts):
    by_name, entries = {}, []
    for scope, path in (("global", os.path.join(home, ".cursor", "mcp.json")),
                        ("project", os.path.join(project, ".cursor", "mcp.json"))):
        data = load_config(path) or {}
        for name, spec in as_dict(data.get("mcpServers")).items():
            if not isinstance(spec, dict):
                continue
            entry = new_entry("cursor", scope, shown(path, home), name,
                              transport="stdio" if spec.get("command") else ("http" if spec.get("url") else "unknown"),
                              command=str(spec.get("command") or ""), args=as_list(spec.get("args")),
                              env=as_map(spec.get("env")),
                              env_file=spec.get("envFile") if isinstance(spec.get("envFile"), str) else None,
                              url=str(spec.get("url") or ""), headers=as_map(spec.get("headers")), expand="cursor",
                              project_file=scope == "project")
            if name in by_name:
                by_name[name].update(enabled=False, status="replaced by the project entry with the same name")
                entries.append(by_name.pop(name))
            by_name[name] = entry
    return entries + list(by_name.values()), []


def opencode_files(home, project, environ):
    """(path, inside the project?) in load order: global, OPENCODE_CONFIG, then the
    folders from the repository root (the folder with .git) down to --project."""
    config_home = environ.get("XDG_CONFIG_HOME") or os.path.join(home, ".config")
    files = [(os.path.join(config_home, "opencode", n), False) for n in ("opencode.json", "opencode.jsonc")]
    if environ.get("OPENCODE_CONFIG"):
        files.append((environ["OPENCODE_CONFIG"], False))
    chain = []
    for folder in ancestors(project):
        chain.append(folder)
        if os.path.exists(os.path.join(folder, ".git")):
            break
    else:
        chain = [os.path.realpath(project)]  # no repository root: only the folder itself
    for folder in reversed(chain):
        files += [(os.path.join(folder, n), True) for n in ("opencode.json", "opencode.jsonc")]
    return files


def read_opencode(home, project, environ, opts):
    servers, where, tools_off = {}, {}, {}
    for path, inside in opencode_files(home, project, environ):
        data = load_config(path) or {}
        for name, spec in as_dict(data.get("mcp")).items():
            if isinstance(spec, dict):
                servers[name] = merge(servers.get(name, {}), spec)
                where[name] = (path, inside)
        for pattern, value in as_dict(data.get("tools")).items():
            tools_off[pattern] = value is False
    globs_off = sorted(p for p, off in tools_off.items() if off)
    entries = []
    for name, spec in servers.items():
        path, inside = where[name]
        command = as_list(spec.get("command"))
        remote = spec.get("type") == "remote"
        entries.append(new_entry(
            "opencode", "project" if inside else "global", shown(path, home), name,
            transport="http" if remote else "stdio",
            command=command[0] if command and not remote else "", args=command[1:] if not remote else [],
            env=as_map(spec.get("environment")), url=str(spec.get("url") or ""),
            headers=as_map(spec.get("headers")), enabled=spec.get("enabled", True) is not False,
            status="" if spec.get("enabled", True) is not False else "enabled is false in the config",
            tool_globs_off=globs_off, expand="opencode", config_dir=os.path.dirname(path), project_file=inside))
    return entries, []


READERS = {"claude-code": read_claude, "codex": read_codex, "gemini-cli": read_gemini,
           "cursor": read_cursor, "opencode": read_opencode}


def collect(home=None, project=None, harnesses=HARNESSES, environ=None, include_unapproved=False):
    """Return (entries, notes): one entry per server definition found, in harness order."""
    environ = os.environ if environ is None else environ
    home = home or os.path.expanduser("~")
    project = os.path.abspath(project or os.getcwd())
    opts = {"include_unapproved": include_unapproved}
    entries, notes = [], []
    for harness in harnesses:
        try:
            found, more = READERS[harness](home, project, environ, opts)
        except ConfigError as exc:
            notes.append("%s: %s %s, so its servers were skipped." % (
                HARNESS_NAMES[harness], inline(shown(exc.path, home), 200), exc))
            continue
        entries.extend(found)
        notes.extend(more)
    return entries, notes


# ---------------------------------------------------------------------------
# grouping and display
# ---------------------------------------------------------------------------

def normalize_url(url):
    url = re.sub(r"^([A-Za-z][A-Za-z0-9+.-]*://)", lambda m: m.group(1).lower(), url.strip())
    url = re.sub(r"^(https://[^/]+):443(?=/|$)|^(http://[^/]+):80(?=/|$)",
                 lambda m: m.group(1) or m.group(2), url)
    return url.rstrip("/")


def server_key(entry):
    if entry["transport"] == "stdio":
        return "stdio:" + json.dumps([os.path.basename(entry["command"]), entry["args"]])
    return "remote:" + normalize_url(entry["url"])


def group(entries):
    """Merge definitions of the same server (same command and args, or same URL)."""
    servers, index = [], {}
    for entry in entries:
        key = server_key(entry)
        if key not in index:
            index[key] = {"key": key, "names": [], "transport": entry["transport"], "entries": []}
            servers.append(index[key])
        server = index[key]
        if entry["name"] not in server["names"]:
            server["names"].append(entry["name"])
        server["entries"].append(entry)
    return servers


def secret_flag(flag):
    """True for a command-line flag whose value is likely a secret (--api-key, --bearer, --pat, -k)."""
    if flag in SECRET_SHORT_FLAGS:
        return True
    words = [w for w in re.split(r"[^a-z0-9]+", flag.lower()) if w]
    return any(w in SECRET_WORDS or w.startswith("auth") or w.endswith(("key", "token", "secret", "password"))
               for w in words)


def mask_url(url):
    """A URL showing only its scheme, host, and common path words: the user and password,
    every other path segment, the whole query, and the fragment are masked."""
    m = re.match(r"^([A-Za-z][A-Za-z0-9+.-]*://)?([^/?#]*)([^?#]*)(\?[^#]*)?(#.*)?$", url, re.S)
    scheme, netloc, path, query, fragment = (m.group(i) or "" for i in range(1, 6))
    if "@" in netloc:
        netloc = "***@" + netloc.rsplit("@", 1)[1]
    segments = [seg if not seg or seg.lower() in COMMON_SEGMENTS or VERSION_SEGMENT.match(seg.lower())
                else "***" for seg in path.split("/")]
    return scheme + netloc + "/".join(segments) + ("?***" if query else "") + ("#***" if fragment else "")


def url_secrets(url):
    """The user and password written inside a URL (scheme://user:password@host)."""
    m = URL_USERINFO.match(url or "")
    if not m:
        return []
    user, _, password = m.group(1).partition(":")
    return [v for v in (password, user) if v]


def token_parts(value):
    """A header value and, for "Bearer x" or "Basic x", the bare x as well."""
    parts = [value]
    m = re.match(r"(?i)^\s*(bearer|basic|token)\s+(\S+)\s*$", value or "")
    if m:
        parts.append(m.group(2))
    return parts


def flag_secrets(words):
    """Values given to secret-looking flags: --api-key X, --token=X, -k X."""
    out, hide_next = [], False
    for word in words:
        if hide_next and not word.startswith("-"):
            out.append(word)
        hide_next = False
        flag, eq, value = word.partition("=")
        if word.startswith("-") and secret_flag(flag):
            if eq:
                out.append(value)
            else:
                hide_next = True
    return out


def config_secrets(entry, environ):
    """Every value in one config entry that could be a secret, before any launch."""
    values = list(entry["env"].values()) + flag_secrets(entry["args"]) + url_secrets(entry["url"])
    for value in entry["headers"].values():
        values.extend(token_parts(value))
    for word in [entry["command"]] + entry["args"]:
        values.extend(url_secrets(word))
    for name in list(entry["header_env"].values()) + ([entry["bearer_env"]] if entry["bearer_env"] else []):
        if name in environ:
            values.extend([environ[name], "Bearer " + environ[name]])
    return [v for v in values if v]


def secret_env_values(env):
    """Values of the variables in a server's environment whose names look secret."""
    return [value for name, value in env.items() if value and SECRET_ENV_NAME.search(name)]


def host_of(url):
    """The host part of a URL, with or without a scheme, and never the user or password."""
    masked = mask_url(url)
    rest = masked.split("://", 1)[1] if "://" in masked else masked
    return re.split(r"[/?#]", rest, 1)[0].rsplit("@", 1)[-1]


def display_command(entry, secrets=()):
    """The command line or URL with secret-looking values masked (never env or header values)."""
    hidden = list(secrets) + list(entry["env"].values()) + list(entry["headers"].values())
    if entry["transport"] != "stdio":
        return mcp_client.redact(mask_url(entry["url"]), hidden)
    words, hide_next = [], False
    for word in [entry["command"]] + entry["args"]:
        if hide_next and not word.startswith("-"):
            words.append("***")
            hide_next = False
            continue
        hide_next = False
        flag, eq, _value = word.partition("=")
        if word.startswith("-") and secret_flag(flag):
            if eq:
                words.append(flag + "=***")
            else:
                words.append(word)
                hide_next = True
            continue
        if re.match(r"^[A-Za-z][A-Za-z0-9+.-]*://", word):
            word = mask_url(word)
        words.append(mcp_client.redact(word, hidden))
    return " ".join(w if w and not re.search(r"\s", w) else json.dumps(w) for w in words)


# ---------------------------------------------------------------------------
# launch details (tools_check.py runs them only with --launch)
# ---------------------------------------------------------------------------

def expand(value, style, environ, workspace, missing, found=None, config_dir=None):
    """Expand one harness's placeholders; every substituted value goes into `found`."""
    found = [] if found is None else found

    def var(name, default=None):
        if name in environ:
            found.append(environ[name])
            return environ[name]
        if default is None:
            missing.append(name)
            return ""
        return default

    def read_file(path):
        path = os.path.expanduser(path)
        if not os.path.isabs(path):
            path = os.path.join(config_dir or workspace, path)
        try:
            data = read_regular(path, MAX_SECRET_FILE_BYTES)
        except ConfigError:
            data = None
        if data is None:
            missing.append("file " + os.path.basename(path))
            return ""
        text = data.decode("utf-8", "replace").strip()
        found.append(text)
        return text

    if style == "claude":
        return re.sub(r"\$\{([A-Za-z_][A-Za-z0-9_]*)(?::-([^}]*))?\}",
                      lambda m: var(m.group(1), m.group(2)), value)
    if style == "cursor":
        value = re.sub(r"\$\{env:([A-Za-z_][A-Za-z0-9_]*)\}", lambda m: var(m.group(1)), value)
        value = value.replace("${userHome}", environ.get("HOME", os.path.expanduser("~")))
        return value.replace("${workspaceFolder}", workspace)
    if style == "gemini":
        return re.sub(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}|\$([A-Za-z_][A-Za-z0-9_]*)",
                      lambda m: var(m.group(1) or m.group(2)), value)
    if style == "opencode":
        value = re.sub(r"\{env:([A-Za-z_][A-Za-z0-9_]*)\}", lambda m: var(m.group(1)), value)
        return re.sub(r"\{file:([^}]+)\}", lambda m: read_file(m.group(1)), value)
    return value


def read_env_file(path):
    values = {}
    try:
        data = read_regular(path, MAX_SECRET_FILE_BYTES)
    except ConfigError:
        data = None
    for line in (data or b"").decode("utf-8", "replace").splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            key, _, value = line.partition("=")
            values[key.replace("export ", "").strip()] = value.strip().strip("'\"")
    return values


def base_env(entry, environ):
    """The inherited environment this entry's harness gives a stdio server."""
    if entry["harness"] == "codex":
        names = list(CODEX_BASE_ENV) + list(entry.get("env_vars") or [])
        return {name: environ[name] for name in names if name in environ}
    if entry["harness"] == "gemini-cli":
        return {name: value for name, value in environ.items()
                if not any(word in name.upper() for word in GEMINI_DROPPED)}
    return dict(environ)


def launch_spec(entry, environ, workspace):
    """(argv, env, cwd, secrets, missing variable names) for a stdio entry."""
    missing, found, style = [], [], entry.get("expand")
    config_dir = entry.get("config_dir")
    whole = style in ("claude", "cursor", "opencode")
    argv = [expand(v, style if whole else None, environ, workspace, missing, found, config_dir)
            for v in [entry["command"]] + entry["args"]]
    env = base_env(entry, environ)
    extra = read_env_file(expand(entry["env_file"], style, environ, workspace, missing, found, config_dir)) \
        if entry.get("env_file") else {}
    extra.update({k: expand(v, style, environ, workspace, missing, found, config_dir)
                  for k, v in entry["env"].items()})
    env.update(extra)
    words = argv[1:]
    secrets = list(extra.values()) + found + secret_env_values(env) + flag_secrets(words)
    for word in argv:
        secrets.extend(url_secrets(word))
    return argv, env, entry.get("cwd"), sorted({v for v in secrets if v}), sorted(set(missing))


def remote_spec(entry, environ, workspace):
    """(url, headers, secrets, missing variable names) for an http entry."""
    missing, found, style = [], [], entry.get("expand")
    view = environ
    if entry["harness"] == "claude-code":
        view = {k: v for k, v in environ.items() if k not in CLAUDE_REMOTE_BLANK}
    style = style if style in ("claude", "cursor", "opencode") else None
    url = expand(entry["url"], style, view, workspace, missing, found, entry.get("config_dir"))
    headers = {k: expand(v, style, view, workspace, missing, found, entry.get("config_dir"))
               for k, v in entry["headers"].items()}
    for header, name in entry["header_env"].items():
        if name in environ:
            headers[header] = environ[name]
        else:
            missing.append(name)
    if entry["bearer_env"]:
        if entry["bearer_env"] in environ:
            headers["Authorization"] = "Bearer " + environ[entry["bearer_env"]]
        else:
            missing.append(entry["bearer_env"])
    secrets = found + url_secrets(url) + url_secrets(entry["url"])
    for value in headers.values():
        secrets.extend(token_parts(value))
    return url, headers, sorted({v for v in secrets if v}), sorted(set(missing))


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="List the MCP servers configured for a folder across Claude Code, Codex, "
                    "Gemini CLI, Cursor, and OpenCode. Starts nothing; prints no env or header values.")
    parser.add_argument("--project", default=".", help="folder to check (default: current folder)")
    parser.add_argument("--harness", default="all", help="all, or a comma list of: " + ", ".join(HARNESSES))
    args = parser.parse_args(argv)
    chosen = HARNESSES if args.harness == "all" else tuple(h.strip() for h in args.harness.split(","))
    unknown = [h for h in chosen if h not in HARNESSES]
    if unknown:
        parser.error("unknown harness: %s" % ", ".join(unknown))
    entries, notes = collect(project=args.project, harnesses=chosen)
    for server in group(entries):
        print("%s (%s): %s" % (", ".join(inline(n, 100) for n in server["names"]), server["transport"],
                               inline(display_command(server["entries"][0]), 300)))
        for e in server["entries"]:
            print("  %s %s in %s%s" % (HARNESS_NAMES[e["harness"]], e["scope"], inline(e["config_path"], 200),
                                      "" if e["enabled"] else " (off: %s)" % e["status"]))
    for note in notes:
        print("note: %s" % note)
    return 0


if __name__ == "__main__":
    sys.exit(main())
