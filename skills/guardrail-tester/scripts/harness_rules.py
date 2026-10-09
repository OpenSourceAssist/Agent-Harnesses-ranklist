"""Simulate the rule systems of Codex, Gemini CLI, OpenCode, and Cursor.

Each follows its official docs (fetched 2026-09-28):
  Codex       https://learn.chatgpt.com/docs/agent-configuration/rules.md,
              https://learn.chatgpt.com/docs/agent-approvals-security.md,
              https://learn.chatgpt.com/docs/hooks.md
  Gemini CLI  docs/reference/policy-engine.md, docs/tools/shell.md,
              docs/reference/configuration.md, docs/hooks/reference.md
              in https://github.com/google-gemini/gemini-cli
  OpenCode    https://opencode.ai/docs/permissions
  Cursor      https://cursor.com/docs/cli/reference/permissions.md,
              https://cursor.com/docs/hooks.md,
              https://cursor.com/docs/reference/third-party-hooks.md
Support differs by harness; references/harness-rules.md lists what each
simulation covers and where the docs are silent.

Nothing here runs a command. Run with --help to print this text.
Python 3.9+, standard library only (TOML files need Python 3.11+).
"""

from __future__ import annotations

import ast
import copy
import fnmatch
import glob as globmod
import json
import os
import re
import sys
from dataclasses import dataclass, field

import claude_rules as C
import shell_split
from safe import code

try:
    import tomllib
except ImportError:  # Python 3.9 and 3.10
    tomllib = None

Result = C.Result
_RANK = {"deny": 3, "ask": 2, "allow": 1}
TOML_NOTE = "Reading %s needs Python 3.11 or newer; it was skipped."


@dataclass
class HookDef:
    harness: str
    event: str
    matcher: object
    command: str
    timeout: float = 600.0            # seconds
    source: str = ""
    source_path: str = ""
    run_dir: str = ""
    fail_closed: bool = False
    name: str = ""
    state_key: str = ""               # Codex: "<file>:pre_tool_use:<group>:<handler>" in [hooks.state]
    trust: str = ""                   # Codex: trusted | untrusted | off | unknown


@dataclass
class Found:
    layer: str
    path: str
    kind: str
    count: int = 0
    note: str = ""


def _strictest(results):
    best = None
    for r in results:
        if best is None or _RANK.get(r.verdict, 0) > _RANK.get(best.verdict, 0):
            best = r
    return best


def _read_text(path):
    with open(path, encoding="utf-8") as fh:
        return fh.read()


def _read_json(path):
    value = json.loads(_read_text(path))
    if not isinstance(value, dict):
        raise ValueError("not a JSON object")
    return value


def _read_toml(path):
    with open(path, "rb") as fh:
        return tomllib.load(fh)


def _home(home):
    return os.path.normpath(os.path.abspath(home)) if home else os.path.expanduser("~")


def _inside(path, folder):
    return path == folder or path.startswith(folder.rstrip("/") + "/")


def _hook_groups(hooks, event):
    groups = hooks.get(event) if isinstance(hooks, dict) else None
    return [g for g in groups if isinstance(g, dict)] if isinstance(groups, list) else []


# ---------------------------------------------------------------------------
# Codex: exec policy rules (Starlark prefix_rule), sandbox, hooks
# ---------------------------------------------------------------------------

@dataclass
class PrefixRule:
    pattern: list
    decision: str = "allow"
    justification: str = ""
    source: str = ""
    line: int = 0

    @property
    def text(self):
        return "%s:%d prefix_rule %s %s" % (self.source or "rules", self.line, json.dumps(self.pattern),
                                             self.decision)


@dataclass
class CodexConfig:
    rules: list = field(default_factory=list)
    cwd: str = "/"
    home: str = "/"
    sandbox_mode: str = "workspace-write"
    approval_policy: str = "on-request"
    network_access: bool = False       # [sandbox_workspace_write] network_access
    trust: str = ""                    # trusted | untrusted | "" (unknown)
    hooks: list = field(default_factory=list)
    hooks_enabled: bool = True
    found: list = field(default_factory=list)
    notes: list = field(default_factory=list)
    smells: list = field(default_factory=list)


_CODEX_DECISIONS = {"forbidden": 3, "prompt": 2, "allow": 1}


def codex_parse_rules(text, name="rules"):
    """prefix_rule() calls from a Starlark rules file. Parsed with Python's ast
    (Starlark syntax is a Python subset) and read with literal_eval: nothing in
    the file is executed."""
    try:
        tree = ast.parse(text)
    except SyntaxError as exc:
        return [], ["%s: cannot be read (line %s)" % (code(name), exc.lineno)]
    rules, warnings = [], []
    for node in tree.body:
        call = node.value if isinstance(node, ast.Expr) else None
        if not isinstance(call, ast.Call) or getattr(call.func, "id", "") != "prefix_rule":
            continue
        try:
            kwargs = {kw.arg: ast.literal_eval(kw.value) for kw in call.keywords if kw.arg}
            if call.args:
                kwargs.setdefault("pattern", ast.literal_eval(call.args[0]))
        except (ValueError, TypeError, SyntaxError):
            warnings.append("%s line %d: a prefix_rule uses expressions this test cannot read; skipped"
                            % (code(name), node.lineno))
            continue
        pattern = kwargs.get("pattern")
        decision = kwargs.get("decision", "allow")
        valid = isinstance(pattern, list) and pattern and all(
            isinstance(p, str) or (isinstance(p, list) and p and all(isinstance(x, str) for x in p))
            for p in pattern)
        if not valid or decision not in _CODEX_DECISIONS:
            warnings.append("%s line %d: a prefix_rule has an invalid pattern or decision; skipped"
                            % (code(name), node.lineno))
            continue
        rules.append(PrefixRule(pattern, decision, str(kwargs.get("justification") or ""), name, node.lineno))
    return rules, warnings


def codex_prefix_match(rules, argv):
    """(decision, rule) for the most restrictive rule whose pattern is a prefix of argv."""
    best = (None, None)
    for rule in rules:
        if len(argv) < len(rule.pattern):
            continue
        if all(argv[i] == p if isinstance(p, str) else argv[i] in p for i, p in enumerate(rule.pattern)):
            if best[0] is None or _CODEX_DECISIONS[rule.decision] > _CODEX_DECISIONS[best[0]]:
                best = (rule.decision, rule)
    return best


_CODEX_NOT_PLAIN = {"substitution", "variable", "glob", "redirection", "heredoc", "assignment", "control",
                    "subshell", "process_substitution", "arithmetic"}


def codex_split(script):
    """The commands of a plain-word script joined by && || ; or |, else None
    (Codex then matches the whole ["bash", "-lc", script] invocation)."""
    parsed = shell_split.parse(script)
    if parsed.error or parsed.dangling or not parsed.commands:
        return None
    if parsed.features & _CODEX_NOT_PLAIN or not parsed.ops <= {"&&", "||", ";", "|"}:
        return None
    if any(c.nested for c in parsed.commands):
        return None
    return [list(c.values) for c in parsed.commands]


def _codex_prompt(cfg, detail, rule=""):
    if cfg.approval_policy == "never":
        return Result("deny", "mode", detail + "; approval_policy never turns the request into a failure", rule)
    return Result("ask", "rule" if rule else "mode", detail, rule)


_NEEDS_TEXT = {"network": "needs network", "write-outside": "needs to write outside the project",
               "write-git": "needs to write .git", "write-project": "needs to write files"}


def _codex_blocked_need(cfg, needs):
    """The first need the sandbox refuses, or ""."""
    needs = set(needs or ())
    if cfg.sandbox_mode == "read-only":
        blocked = ("network", "write-outside", "write-git", "write-project")
    else:
        blocked = ("write-outside", "write-git") + (() if cfg.network_access else ("network",))
    return next((n for n in blocked if n in needs), "")


def _codex_sandbox(cfg, need):
    what = _NEEDS_TEXT[need]
    if cfg.approval_policy == "never":
        return Result("deny", "sandbox", "stopped by the sandbox (it %s), and approval_policy never turns the "
                                         "request to run outside it into a failure" % what)
    return Result("ask", "sandbox", "asks first (%s): the %s sandbox stops it and Codex asks to run it outside "
                                    "the sandbox" % (what, cfg.sandbox_mode))


def codex_evaluate(cfg, tool, tool_input, needs=()) -> Result:
    """`needs` says what the command needs to do its harm (network,
    write-outside, write-git, write-project); the sandbox decides on it."""
    tool_input = tool_input if isinstance(tool_input, dict) else {}
    if tool == "Bash":
        command = str(tool_input.get("command") or "")
        argvs = codex_split(command)
        parts = argvs if argvs is not None else [["bash", "-lc", command]]
        found = [codex_prefix_match(cfg.rules, argv) for argv in parts]
        for decision, rule in found:
            if decision == "forbidden":
                return Result("deny", "rule", "a forbidden exec-policy rule blocks it", rule.text)
        for decision, rule in found:
            if decision == "prompt":
                return _codex_prompt(cfg, "a prompt exec-policy rule asks first", rule.text)
        if parts and all(d == "allow" for d, _ in found):
            return Result("allow", "rule", "an allow rule runs it outside the sandbox without asking",
                          found[0][1].text)
        if cfg.sandbox_mode == "danger-full-access":
            return Result("allow", "mode", "danger-full-access runs it with no sandbox and no prompt")
        if cfg.trust == "untrusted":
            return _codex_prompt(cfg, "untrusted projects ask before any command no rule allows")
        need = _codex_blocked_need(cfg, needs)
        if need:
            return _codex_sandbox(cfg, need)
        return Result("allow", "sandbox", "runs inside the %s sandbox without asking; nothing it needs is outside "
                                          "the sandbox" % cfg.sandbox_mode)
    if tool == "apply_patch":
        path = C._expand(str(tool_input.get("file_path") or ""), cfg.cwd, cfg.home)
        if cfg.sandbox_mode == "danger-full-access":
            return Result("allow", "mode", "danger-full-access writes anywhere without a prompt")
        if cfg.sandbox_mode == "read-only":
            return _codex_sandbox(cfg, "write-project")
        rel = os.path.relpath(path, cfg.cwd).split(os.sep)
        protected = rel[0] in (".git", ".agents", ".codex")
        if _inside(path, cfg.cwd) and not protected:
            return Result("allow", "sandbox", "workspace-write edits inside the project without asking")
        return _codex_sandbox(cfg, "write-git" if protected else "write-outside")
    return Result("ask", "mode", "this test does not simulate this Codex tool")


def _codex_hooks(data, source, path, run_dir, out):
    hooks = data.get("hooks") if isinstance(data, dict) else None
    groups = hooks.get("PreToolUse") if isinstance(hooks, dict) else None
    for g, group in enumerate(groups if isinstance(groups, list) else []):
        if not isinstance(group, dict):
            continue
        handlers = group.get("hooks")
        for i, handler in enumerate(handlers if isinstance(handlers, list) else []):
            if not isinstance(handler, dict) or handler.get("type", "command") != "command":
                continue
            if handler.get("async"):
                continue
            timeout = handler.get("timeout")
            ok = isinstance(timeout, (int, float)) and not isinstance(timeout, bool) and timeout > 0
            out.append(HookDef("codex", "PreToolUse", group.get("matcher"), str(handler.get("command") or ""),
                               float(timeout) if ok else 600.0, source, path, run_dir,
                               state_key="%s:pre_tool_use:%d:%d" % (path, g, i)))


def _codex_trust(hook, state):
    """Codex runs a hook only after it is trusted in /hooks, which records
    [hooks.state."<file>:<event>:<group>:<handler>"] with trusted_hash."""
    if state is None:
        return "unknown"
    entry = state.get(hook.state_key)
    if not isinstance(entry, dict):
        return "untrusted"
    if entry.get("enabled") is False:
        return "off"
    return "trusted" if entry.get("trusted_hash") else "untrusted"


def codex_load(project, home=None) -> CodexConfig:
    cwd = os.path.normpath(os.path.abspath(project))
    home_dir = _home(home)
    root = os.environ.get("CODEX_HOME") if home is None and os.environ.get("CODEX_HOME") else os.path.join(home_dir, ".codex")
    cfg = CodexConfig(cwd=cwd, home=home_dir)
    user_toml = os.path.join(root, "config.toml")
    project_dir = os.path.join(cwd, ".codex")
    configs = []
    if os.path.isfile(user_toml):
        if tomllib is None:
            cfg.notes.append(TOML_NOTE % "~/.codex/config.toml (sandbox, approval, trust, and inline hooks)")
        else:
            try:
                configs.append(("user", user_toml, _read_toml(user_toml)))
            except (OSError, ValueError) as exc:
                cfg.notes.append("Could not read the Codex config (%s)." % type(exc).__name__)
    user = configs[0][2] if configs else {}
    projects = user.get("projects") if isinstance(user.get("projects"), dict) else {}
    repo = C._git_root(cwd)
    for key in (cwd, repo):
        entry = projects.get(key) if key else None
        if isinstance(entry, dict) and entry.get("trust_level") in ("trusted", "untrusted"):
            cfg.trust = entry["trust_level"]
            break
    project_toml = os.path.join(project_dir, "config.toml")
    if os.path.isfile(project_toml) and tomllib is not None:
        if cfg.trust == "trusted":
            try:
                configs.append(("project", project_toml, _read_toml(project_toml)))
            except (OSError, ValueError):
                cfg.notes.append("Could not read the project's .codex/config.toml.")
        else:
            cfg.notes.append("The project's .codex folder loads only in trusted projects; it was skipped.")
    for _, _, data in configs:
        if isinstance(data.get("sandbox_mode"), str):
            cfg.sandbox_mode = data["sandbox_mode"]
        workspace = data.get("sandbox_workspace_write")
        if isinstance(workspace, dict) and isinstance(workspace.get("network_access"), bool):
            cfg.network_access = workspace["network_access"]
        policy = data.get("approval_policy")
        if isinstance(policy, str):
            cfg.approval_policy = policy
        elif isinstance(policy, dict):
            cfg.approval_policy = "granular"
        features = data.get("features")
        if isinstance(features, dict) and (features.get("hooks") is False or features.get("codex_hooks") is False):
            cfg.hooks_enabled = False
    for layer, path, data in configs:
        _codex_hooks(data, layer, path, cwd, cfg.hooks)
        cfg.found.append(Found(layer, path, "config"))
    hook_files = [("user", os.path.join(root, "hooks.json"))]
    if cfg.trust == "trusted":
        hook_files.append(("project", os.path.join(project_dir, "hooks.json")))
    elif os.path.isfile(os.path.join(project_dir, "hooks.json")):
        cfg.notes.append("Project Codex hooks load only in trusted projects; they were skipped.")
    for layer, path in hook_files:
        if not os.path.isfile(path):
            continue
        try:
            before = len(cfg.hooks)
            _codex_hooks(_read_json(path), layer, path, cwd, cfg.hooks)
            cfg.found.append(Found(layer, path, "hooks", len(cfg.hooks) - before))
        except (OSError, ValueError) as exc:
            cfg.notes.append("Could not read %s (%s)." % (os.path.basename(path), type(exc).__name__))
    rule_dirs = [("user", os.path.join(root, "rules"))]
    if cfg.trust == "trusted":
        rule_dirs.append(("project", os.path.join(project_dir, "rules")))
    for layer, folder in rule_dirs:
        for path in sorted(globmod.glob(os.path.join(globmod.escape(folder), "*.rules"))):
            try:
                rules, warnings = codex_parse_rules(_read_text(path), os.path.basename(path))
            except OSError:
                continue
            cfg.rules.extend(rules)
            cfg.notes.extend(warnings)
            cfg.found.append(Found(layer, path, "rules", len(rules)))
    if not cfg.hooks_enabled and cfg.hooks:
        cfg.smells.append({"id": "codex-hooks-off", "severity": "high", "source": "config.toml",
                           "text": "[features] hooks = false turns every Codex hook off."})
        cfg.hooks = []
    state = None
    if tomllib is not None:
        state = {}
        for _, _, data in configs:
            hooks = data.get("hooks")
            entries = hooks.get("state") if isinstance(hooks, dict) else None
            if isinstance(entries, dict):
                state.update(entries)
    for hook in cfg.hooks:
        hook.trust = _codex_trust(hook, state)
    if cfg.hooks and state is None:
        cfg.notes.append("Codex hook trust is unknown: Codex keeps it in config.toml, which needs Python 3.11 or "
                         "newer to read. The test runs each hook as if you trusted it in /hooks.")
    untrusted = sum(1 for h in cfg.hooks if h.trust in ("untrusted", "off"))
    if untrusted:
        cfg.notes.append("%d Codex hook%s not trusted or turned off in /hooks, so Codex never runs %s; the test "
                         "skips %s too." % (untrusted, " is" if untrusted == 1 else "s are",
                                            "it" if untrusted == 1 else "them", "it" if untrusted == 1 else "them"))
    if cfg.sandbox_mode == "danger-full-access":
        cfg.smells.append({"id": "codex-no-sandbox", "severity": "high", "source": "config.toml",
                           "text": "sandbox_mode danger-full-access runs every command with no sandbox."})
    if cfg.approval_policy == "untrusted":
        cfg.smells.append({"id": "codex-untrusted-policy", "severity": "medium", "source": "config.toml",
                           "text": "approval_policy \"untrusted\" is no longer supported and can stop Codex "
                                   "from starting; use trust_level = \"untrusted\" on the project instead."})
    return cfg


# ---------------------------------------------------------------------------
# Gemini CLI: policy engine, tools.* settings, hooks
# ---------------------------------------------------------------------------

TIERS = {"default": 1, "extension": 2, "workspace": 3, "user": 4, "admin": 5}
READ_TOOLS = {"read_file", "read_many_files", "glob", "grep_search", "search_file_content", "list_directory",
              "google_web_search"}
WRITE_TOOLS = {"write_file", "replace"}
_MODE_NAMES = {"default": "default", "auto_edit": "autoEdit", "autoEdit": "autoEdit", "plan": "plan",
               "yolo": "yolo"}


@dataclass
class PolicyRule:
    tools: list
    decision: str                     # allow | deny | ask_user
    priority: float
    prefixes: list = field(default_factory=list)
    word_prefix: bool = False         # legacy tools.* entries match whole words
    command_regex: str = ""
    args_pattern: str = ""
    modes: list = field(default_factory=list)
    allow_redirection: bool = False
    mcp_name: str = ""
    source: str = ""
    text: str = ""

    def matches(self, tool, args, mode):
        if self.modes and mode not in self.modes:
            return False
        if self.mcp_name:
            server = self.mcp_name
            if not (server == "*" and tool.startswith("mcp_")) and not tool.startswith("mcp_%s_" % server):
                return False
        elif not any(t == "*" or fnmatch.fnmatchcase(tool, t) for t in self.tools):
            return False
        command = str(args.get("command") or "") if isinstance(args, dict) else ""
        if self.prefixes:
            if tool != "run_shell_command":
                return False
            if not any(_word_prefix(command, p) if self.word_prefix else command.startswith(p)
                       for p in self.prefixes):
                return False
        stable = json.dumps(args, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
        try:
            if self.command_regex and (tool != "run_shell_command" or
                                       not re.search('"command":"' + self.command_regex, stable)):
                return False
            if self.args_pattern and not re.search(self.args_pattern, stable):
                return False
        except re.error:
            return False
        return True


@dataclass
class GeminiConfig:
    rules: list = field(default_factory=list)
    core: object = None               # None, or list of (tool, prefix or None)
    mode: str = "default"
    hooks: list = field(default_factory=list)
    found: list = field(default_factory=list)
    notes: list = field(default_factory=list)
    smells: list = field(default_factory=list)
    cwd: str = "/"
    home: str = "/"


def _word_prefix(command, prefix):
    command = command.strip()
    return command == prefix or command.startswith(prefix + " ")


def _legacy_entry(entry):
    m = re.match(r"^\s*([A-Za-z0-9_.-]+)\s*(?:\((.*)\))?\s*$", str(entry), re.S)
    if not m:
        return None, None
    return m.group(1), (m.group(2).strip() if m.group(2) is not None else None)


def _legacy_rules(entries, decision, priority, source):
    out = []
    for entry in entries if isinstance(entries, list) else []:
        tool, prefix = _legacy_entry(entry)
        if tool:
            out.append(PolicyRule([tool], decision, priority, [prefix] if prefix else [], word_prefix=True,
                                  source=source, text="%s: %s" % (source, entry)))
    return out


def _policy_rules(text, tier, notes, source):
    if tomllib is None:
        notes.append(TOML_NOTE % ("Gemini CLI policy files (%s)" % source))
        return []
    try:
        data = tomllib.loads(text)
    except ValueError:
        notes.append("Could not read the Gemini CLI policy file %s." % source)
        return []
    out = []
    rules = data.get("rule")
    for i, raw in enumerate(rules if isinstance(rules, list) else []):
        if not isinstance(raw, dict) or raw.get("decision") not in ("allow", "deny", "ask_user"):
            continue
        if raw.get("interactive") is False or raw.get("subagent") or raw.get("toolAnnotations"):
            continue
        tools = raw.get("toolName", "*" if raw.get("mcpName") else None)
        tools = [tools] if isinstance(tools, str) else tools if isinstance(tools, list) else []
        prefixes = raw.get("commandPrefix")
        prefixes = [prefixes] if isinstance(prefixes, str) else prefixes if isinstance(prefixes, list) else []
        prefixes = [p for p in prefixes if isinstance(p, str)]
        if (prefixes or raw.get("commandRegex")) and not tools:
            tools = ["run_shell_command"]
        if not tools and not raw.get("mcpName"):
            continue
        priority = raw.get("priority", 0)
        priority = min(max(float(priority), 0.0), 999.0) if isinstance(priority, (int, float)) else 0.0
        modes = raw.get("modes")
        modes = [_MODE_NAMES.get(m, m) for m in modes if isinstance(m, str)] if isinstance(modes, list) else []
        out.append(PolicyRule([str(t) for t in tools], raw["decision"], TIERS[tier] + priority / 1000.0,
                              [str(p) for p in prefixes], False, str(raw.get("commandRegex") or ""),
                              str(raw.get("argsPattern") or ""), modes, bool(raw.get("allowRedirection")),
                              str(raw.get("mcpName") or ""), source, "%s rule %d" % (source, i + 1)))
    return out


def gemini_rules(settings, policy_texts):
    """(rules, notes) from merged settings.json and [(tier, toml text)] policy files."""
    notes = []
    tools = settings.get("tools") if isinstance(settings.get("tools"), dict) else {}
    rules = _legacy_rules(tools.get("exclude"), "deny", 10.0, "tools.exclude")
    rules += _legacy_rules(tools.get("confirmationRequired"), "ask_user", 9.0, "tools.confirmationRequired")
    rules += _legacy_rules(tools.get("allowed"), "allow", TIERS["user"], "tools.allowed")
    for tier, text in policy_texts:
        rules += _policy_rules(text, tier, notes, "%s policy" % tier)
    return rules, notes


def gemini_config(settings, policy_texts, mode=None, cwd="/", home="/") -> GeminiConfig:
    """A GeminiConfig from merged settings.json data and policy file texts."""
    settings = settings if isinstance(settings, dict) else {}
    rules, notes = gemini_rules(settings, policy_texts)
    cfg = GeminiConfig(rules=rules, notes=notes, cwd=cwd, home=home)
    tools = settings.get("tools") if isinstance(settings.get("tools"), dict) else {}
    if isinstance(tools.get("core"), list):
        cfg.core = [e for e in (_legacy_entry(x) for x in tools["core"]) if e[0]]
    general = settings.get("general") if isinstance(settings.get("general"), dict) else {}
    cfg.mode = _MODE_NAMES.get(mode or general.get("defaultApprovalMode"), "default")
    return cfg


def _default_rules(mode):
    rules = [PolicyRule(["*"], "ask_user", 1.0, source="default"),
             PolicyRule(sorted(READ_TOOLS), "allow", 1.05, source="default")]
    if mode == "yolo":
        rules.append(PolicyRule(["*"], "allow", 1.999, source="default"))
    elif mode == "autoEdit":
        rules.append(PolicyRule(sorted(WRITE_TOOLS), "allow", 1.5, source="default"))
    elif mode == "plan":
        rules.append(PolicyRule(sorted(WRITE_TOOLS | {"run_shell_command"}), "deny", 1.9, source="default"))
    return rules


def _gemini_core(cfg, tool, command):
    if cfg.core is None:
        return None
    entries = [e for e in cfg.core if e[0] == tool]
    if not entries:
        return Result("deny", "rule", "tools.core does not list this tool, so it is disabled", "tools.core")
    if tool == "run_shell_command" and not any(p is None or _word_prefix(command, p) for _, p in entries):
        return Result("deny", "rule", "tools.core lists no prefix that covers this command", "tools.core")
    return None


def _gemini_decide(cfg, tool, args, mode):
    command = str(args.get("command") or "")
    blocked = [r for r in cfg.rules if r.source == "tools.exclude" and r.matches(tool, args, mode)]
    if blocked:
        return Result("deny", "rule", "tools.exclude blocks it", blocked[0].text)
    core = _gemini_core(cfg, tool, command)
    if core is not None:
        return core
    candidates = [r for r in cfg.rules + _default_rules(mode) if r.matches(tool, args, mode)]
    if not candidates:
        return Result("ask", "mode", "no rule covers it, so Gemini CLI asks first")
    best = max(candidates, key=lambda r: (r.priority, {"deny": 3, "ask_user": 2, "allow": 1}[r.decision]))
    layer = "mode" if best.source == "default" else "rule"
    if best.decision == "deny":
        return Result("deny", layer, "a deny rule blocks it", best.text)
    if best.decision == "allow":
        if tool == "run_shell_command" and "redirection" in shell_split.parse(command).features \
                and not best.allow_redirection and best.source != "default":
            return Result("ask", "rule", "the allow rule does not permit redirection, so Gemini CLI asks",
                          best.text)
        return Result("allow", layer, "an allow rule approves it" if layer == "rule"
                      else "Gemini CLI runs it without asking in this mode", best.text)
    return Result("ask", layer, "Gemini CLI asks first", best.text)


def gemini_evaluate(cfg, tool, args, mode=None) -> Result:
    mode = _MODE_NAMES.get(mode or cfg.mode, "default")
    args = args if isinstance(args, dict) else {}
    if tool == "run_shell_command":
        parts = shell_split.split_chain(str(args.get("command") or "")) or [""]
        results = [_gemini_decide(cfg, tool, dict(args, command=part), mode) for part in parts]
        return _strictest(results)
    return _gemini_decide(cfg, tool, args, mode)


def _gemini_trusted(cwd, home, env_path):
    path = env_path or os.path.join(home, ".gemini", "trustedFolders.json")
    try:
        data = _read_json(path)
    except (OSError, ValueError):
        return None
    verdict = None
    for folder, level in data.items():
        folder = os.path.normpath(os.path.expanduser(folder)) if isinstance(folder, str) else ""
        if level == "DO_NOT_TRUST" and _inside(cwd, folder):
            return False
        if level == "TRUST_FOLDER" and _inside(cwd, folder):
            verdict = True
        if level == "TRUST_PARENT" and _inside(cwd, os.path.dirname(folder)):
            verdict = True
    return verdict


def gemini_load(project, home=None) -> GeminiConfig:
    cwd = os.path.normpath(os.path.abspath(project))
    home_dir = _home(home)
    cfg = GeminiConfig(cwd=cwd, home=home_dir)
    gem = os.path.join(home_dir, ".gemini")
    layers = []
    if home is None:
        system = ("/Library/Application Support/GeminiCli" if sys.platform == "darwin" else "/etc/gemini-cli")
        layers.append(("system", os.path.join(system, "settings.json")))
    layers.append(("user", os.path.join(gem, "settings.json")))
    merged, datas = {}, []
    for name, path in layers:
        if os.path.isfile(path):
            try:
                datas.append((name, path, _read_json(path)))
            except (OSError, ValueError):
                cfg.notes.append("Could not read the Gemini CLI %s settings." % name)
    trust_on = True
    for _, _, data in datas:
        sec = data.get("security") if isinstance(data.get("security"), dict) else {}
        ft = sec.get("folderTrust") if isinstance(sec.get("folderTrust"), dict) else {}
        if ft.get("enabled") is False:
            trust_on = False
    project_path = os.path.join(cwd, ".gemini", "settings.json")
    if os.path.isfile(project_path):
        trusted = _gemini_trusted(cwd, home_dir, os.environ.get("GEMINI_CLI_TRUSTED_FOLDERS_PATH") if home is None else None)
        if trust_on and not trusted:
            cfg.notes.append("Gemini CLI ignores this project's .gemini/settings.json until you trust the folder.")
        else:
            try:
                datas.append(("project", project_path, _read_json(project_path)))
            except (OSError, ValueError):
                cfg.notes.append("Could not read the project's .gemini/settings.json.")
    for name, path, data in datas:
        merged = C._merge(merged, {k: v for k, v in data.items() if k != "hooks"})
        cfg.found.append(Found(name, path, "settings"))
        hooks_cfg = data.get("hooksConfig") if isinstance(data.get("hooksConfig"), dict) else {}
        for group in _hook_groups(data.get("hooks"), "BeforeTool"):
            for handler in group.get("hooks") or []:
                if not isinstance(handler, dict) or handler.get("type", "command") != "command":
                    continue
                timeout = handler.get("timeout")
                ms = float(timeout) if isinstance(timeout, (int, float)) and timeout > 0 else 60000.0
                if ms < 1000:
                    cfg.smells.append({"id": "gemini-timeout-ms", "severity": "medium", "source": name,
                                       "text": "A Gemini CLI hook has timeout %g. Gemini CLI reads it in "
                                               "milliseconds, so the hook gets less than a second." % ms})
                cfg.hooks.append(HookDef("gemini-cli", "BeforeTool", group.get("matcher"),
                                         str(handler.get("command") or ""), ms / 1000.0, name, path, cwd,
                                         name=str(handler.get("name") or "")))
        del hooks_cfg
    hooks_config = merged.get("hooksConfig") if isinstance(merged.get("hooksConfig"), dict) else {}
    if hooks_config.get("enabled") is False:
        cfg.hooks = []
        cfg.notes.append("hooksConfig.enabled is false, so Gemini CLI runs no hooks.")
    disabled = hooks_config.get("disabled")
    disabled = {d for d in disabled if isinstance(d, str)} if isinstance(disabled, list) else set()
    cfg.hooks = [h for h in cfg.hooks if not (h.name and h.name in disabled)]
    texts = []
    for tier, folder in [("user", os.path.join(gem, "policies"))] + (
            [("admin", "/Library/Application Support/GeminiCli/policies" if sys.platform == "darwin"
              else "/etc/gemini-cli/policies")] if home is None else []):
        for path in sorted(globmod.glob(os.path.join(globmod.escape(folder), "*.toml"))):
            try:
                texts.append((tier, _read_text(path)))
                cfg.found.append(Found(tier, path, "policy"))
            except OSError:
                continue
    if globmod.glob(os.path.join(globmod.escape(os.path.join(cwd, ".gemini", "policies")), "*.toml")):
        cfg.smells.append({"id": "gemini-workspace-policy", "severity": "medium", "source": "project",
                           "text": "This project has .gemini/policies files, which Gemini CLI does not "
                                   "apply yet (issue #18186); move the rules to ~/.gemini/policies."})
    built = gemini_config(merged, texts, cwd=cwd, home=home_dir)
    cfg.rules, cfg.core, cfg.mode = built.rules, built.core, built.mode
    cfg.notes.extend(built.notes)
    security = merged.get("security") if isinstance(merged.get("security"), dict) else {}
    if datas and not security.get("disableYoloMode"):
        cfg.smells.append({"id": "gemini-yolo-allowed", "severity": "low", "source": "settings",
                           "text": "security.disableYoloMode is off, so --yolo can still approve every call."})
    return cfg


# ---------------------------------------------------------------------------
# OpenCode: permission config (last matching rule wins)
# ---------------------------------------------------------------------------

DEFAULT_OPENCODE = {"read": {"*": "allow", "*.env": "deny", "*.env.*": "deny", "*.env.example": "allow"},
                    "doom_loop": "ask", "external_directory": "ask"}
_OC_TOOLS = {"write": "edit", "patch": "edit", "multiedit": "edit", "edit": "edit", "read": "read",
             "bash": "bash"}


@dataclass
class OpenCodeConfig:
    permission: dict = field(default_factory=dict)
    cwd: str = "/"
    home: str = "/"
    plugins: list = field(default_factory=list)
    found: list = field(default_factory=list)
    notes: list = field(default_factory=list)
    smells: list = field(default_factory=list)


def parse_jsonc(text):
    """JSON with // and /* */ comments and trailing commas (opencode.jsonc)."""
    out, i, n, in_str = [], 0, len(text), False
    while i < n:
        c = text[i]
        if in_str:
            out.append(c)
            if c == "\\" and i + 1 < n:
                out.append(text[i + 1])
                i += 2
                continue
            if c == '"':
                in_str = False
            i += 1
            continue
        if c == '"':
            in_str = True
            out.append(c)
        elif text.startswith("//", i):
            j = text.find("\n", i)
            i = n if j < 0 else j
            continue
        elif text.startswith("/*", i):
            j = text.find("*/", i + 2)
            i = n if j < 0 else j + 2
            continue
        else:
            out.append(c)
        i += 1
    cleaned = re.sub(r",(\s*[}\]])", r"\1", "".join(out))
    return json.loads(cleaned)


def _oc_regex(pattern, home):
    if pattern.startswith("~"):
        pattern = home + pattern[1:]
    elif pattern.startswith("$HOME"):
        pattern = home + pattern[5:]
    return re.compile("".join(".*" if c == "*" else "." if c == "?" else re.escape(c) for c in pattern), re.S)


def _oc_match(pattern, subjects, home):
    rx = _oc_regex(pattern, home)
    return any(rx.fullmatch(s) for s in subjects)


def opencode_config(conf, cwd, home) -> OpenCodeConfig:
    cfg = OpenCodeConfig(cwd=os.path.normpath(cwd), home=os.path.normpath(home))
    conf = conf if isinstance(conf, dict) else {}
    perm = conf.get("permission")
    if isinstance(perm, str):
        merged = {"*": perm}
    else:
        merged = copy.deepcopy(DEFAULT_OPENCODE)
        for key, value in (perm or {}).items() if isinstance(perm, dict) else []:
            if isinstance(value, dict) and isinstance(merged.get(key), dict):
                merged[key].update(value)
            else:
                merged[key] = copy.deepcopy(value)
    tools = conf.get("tools")
    for key, enabled in (tools or {}).items() if isinstance(tools, dict) else []:
        if enabled is False:
            merged[_OC_TOOLS.get(key, key)] = "deny"
    cfg.permission = merged
    for key, value in merged.items():
        if not isinstance(value, dict):
            continue
        items = list(value.items())
        for i, (pattern, action) in enumerate(items):
            if action != "deny" or not isinstance(pattern, str):
                continue
            sample = pattern.replace("*", "x").replace("?", "x")
            later = [p for p, a in items[i + 1:] if a != "deny" and isinstance(p, str)
                     and _oc_match(p, [sample], cfg.home)]
            if later:
                cfg.smells.append({"id": "opencode-shadowed-deny", "severity": "high", "source": "opencode.json",
                                   "text": "In %s, the deny rule %s comes before %s. OpenCode uses the "
                                           "last matching rule, so the deny never applies; move it after."
                                           % (code("permission.%s" % key), code(json.dumps(pattern)),
                                              code(json.dumps(later[0])))})
    return cfg


def _oc_action(cfg, key, subjects):
    perm = cfg.permission
    value = perm.get(key, perm.get("*", "allow"))
    if isinstance(value, str):
        return value, "permission.%s" % key
    if isinstance(value, dict):
        hit = None
        for pattern, action in value.items():
            if isinstance(pattern, str) and isinstance(action, str) and _oc_match(pattern, subjects, cfg.home):
                hit = (action, "permission.%s %s" % (key, json.dumps(pattern)))
        if hit:
            return hit
    fallback = perm.get("*") if isinstance(perm.get("*"), str) else "allow"
    return fallback, "permission.*" if isinstance(perm.get("*"), str) else "the OpenCode default"


def _oc_result(action, where):
    if action == "deny":
        return Result("deny", "rule", "a deny rule blocks it", where)
    if action == "ask":
        return Result("ask", "rule", "OpenCode asks first", where)
    layer = "mode" if where == "the OpenCode default" else "rule"
    return Result("allow", layer, "OpenCode runs it without asking", where)


def opencode_evaluate(cfg, tool, tool_input) -> Result:
    tool_input = tool_input if isinstance(tool_input, dict) else {}
    key = _OC_TOOLS.get(tool, tool)
    if key == "bash":
        command = str(tool_input.get("command") or "")
        parsed = shell_split.parse(command)
        texts = [c.text for c in parsed.commands] if not parsed.error and parsed.commands else [command.strip()]
        return _strictest([_oc_result(*_oc_action(cfg, "bash", [t])) for t in texts])
    if key in ("read", "edit"):
        raw = str(tool_input.get("filePath") or tool_input.get("file_path") or "")
        path = C._expand(raw, cfg.cwd, cfg.home)
        subjects = [path]
        if _inside(path, cfg.cwd):
            subjects.append(os.path.relpath(path, cfg.cwd))
        results = [_oc_result(*_oc_action(cfg, key, subjects))]
        if not _inside(path, cfg.cwd):
            action, where = _oc_action(cfg, "external_directory", [path])
            results.append(_oc_result(action, where))
        return _strictest(results)
    return _oc_result(*_oc_action(cfg, key, [""]))


def opencode_load(project, home=None) -> OpenCodeConfig:
    cwd = os.path.normpath(os.path.abspath(project))
    home_dir = _home(home)
    xdg = os.environ.get("XDG_CONFIG_HOME") if home is None else None
    global_dir = os.path.join(xdg or os.path.join(home_dir, ".config"), "opencode")
    files = [("global", os.path.join(global_dir, name)) for name in ("opencode.json", "opencode.jsonc")]
    if home is None and os.environ.get("OPENCODE_CONFIG"):
        files.append(("OPENCODE_CONFIG", os.environ["OPENCODE_CONFIG"]))
    for name in ("opencode.json", "opencode.jsonc"):
        files.append(("project", os.path.join(cwd, name)))
        files.append(("project", os.path.join(cwd, ".opencode", name)))
    merged, found, notes = {}, [], []
    for layer, path in files:
        if not os.path.isfile(path):
            continue
        try:
            data = parse_jsonc(_read_text(path))
        except (OSError, ValueError):
            notes.append("Could not read %s." % code(os.path.basename(path)))
            continue
        if isinstance(data, dict):
            merged = C._merge(merged, data)
            found.append(Found(layer, path, "config"))
    cfg = opencode_config(merged, cwd, home_dir)
    cfg.found, cfg.notes = found, notes + cfg.notes
    for folder in (os.path.join(cwd, ".opencode", "plugins"), os.path.join(global_dir, "plugins")):
        for path in sorted(globmod.glob(os.path.join(globmod.escape(folder), "*"))):
            if path.endswith((".js", ".ts", ".mjs")):
                cfg.plugins.append(path)
    if cfg.plugins:
        cfg.notes.append("OpenCode plugins are JavaScript or TypeScript code, which this test does not run; "
                         "%d found." % len(cfg.plugins))
    return cfg


# ---------------------------------------------------------------------------
# Cursor: CLI permissions and hooks (plus the Claude Code hooks it imports)
# ---------------------------------------------------------------------------

@dataclass
class CursorConfig:
    allow: list = field(default_factory=list)
    deny: list = field(default_factory=list)
    cwd: str = "/"
    home: str = "/"
    hooks: list = field(default_factory=list)
    found: list = field(default_factory=list)
    notes: list = field(default_factory=list)
    smells: list = field(default_factory=list)


def _cursor_token(token):
    m = re.match(r"^\s*(Shell|Read|Write|WebFetch|Mcp)\((.*)\)\s*$", str(token), re.S)
    return (m.group(1), m.group(2).strip()) if m else (None, None)


def _cursor_shell_hit(spec, values):
    if not values:
        return False
    base, _, args = spec.partition(":")
    if not fnmatch.fnmatchcase(values[0], base.strip()):
        return False
    return fnmatch.fnmatchcase(" ".join(values[1:]), args.strip()) if _ else True


def _cursor_path_hit(spec, path, cwd, home, list_name="deny"):
    if spec.startswith("~"):
        spec = home + spec[1:]
    if os.path.isabs(spec):
        target = path
        body = spec.lstrip("/")
        target = path.lstrip("/")
    else:
        if not _inside(path, cwd):
            return False
        target, body = os.path.relpath(path, cwd), spec
    return C._gi_regex(body, list_name).fullmatch(target) is not None


def cursor_evaluate(cfg, tool, tool_input) -> Result:
    tool_input = tool_input if isinstance(tool_input, dict) else {}
    kind = {"Shell": "Shell", "Read": "Read", "Write": "Write", "Delete": "Write"}.get(tool, tool)
    if kind == "Shell":
        command = str(tool_input.get("command") or "")
        parsed = shell_split.parse(command)
        commands = [c.values for c in parsed.commands] if not parsed.error and parsed.commands else [command.split()]
        results = []
        for values in commands:
            hit = lambda tokens: next((t for t in tokens if _cursor_token(t)[0] == "Shell"
                                       and _cursor_shell_hit(_cursor_token(t)[1], values)), None)
            denied, allowed = hit(cfg.deny), hit(cfg.allow)
            if denied:
                results.append(Result("deny", "rule", "a deny rule blocks it", denied))
            elif allowed:
                results.append(Result("allow", "rule", "an allow rule approves it", allowed))
            else:
                results.append(Result("ask", "mode", "no allow rule covers it, so Cursor asks first"))
        return _strictest(results)
    path = C._expand(str(tool_input.get("file_path") or ""), cfg.cwd, cfg.home)
    hit = lambda tokens: next((t for t in tokens if _cursor_token(t)[0] == kind
                               and _cursor_path_hit(_cursor_token(t)[1], path, cfg.cwd, cfg.home)), None)
    denied = hit(cfg.deny)
    if denied:
        return Result("deny", "rule", "a deny rule blocks it", denied)
    if kind == "Read":
        return Result("allow", "mode", "Cursor reads files without asking unless a deny rule covers them")
    allowed = next((t for t in cfg.allow if _cursor_token(t)[0] == kind
                    and _cursor_path_hit(_cursor_token(t)[1], path, cfg.cwd, cfg.home, "allow")), None)
    if allowed:
        return Result("allow", "rule", "an allow rule approves it", allowed)
    return Result("ask", "mode", "no allow rule covers it, so Cursor asks first")


CURSOR_EVENTS = ("preToolUse", "beforeShellExecution", "beforeReadFile")


def cursor_load(project, home=None, claude_hooks=()) -> CursorConfig:
    cwd = os.path.normpath(os.path.abspath(project))
    home_dir = _home(home)
    cfg = CursorConfig(cwd=cwd, home=home_dir)
    for layer, path in (("user", os.path.join(home_dir, ".cursor", "cli-config.json")),
                        ("project", os.path.join(cwd, ".cursor", "cli.json"))):
        if not os.path.isfile(path):
            continue
        try:
            perm = _read_json(path).get("permissions") or {}
        except (OSError, ValueError):
            cfg.notes.append("Could not read %s." % os.path.basename(path))
            continue
        cfg.allow += [t for t in perm.get("allow") or [] if isinstance(t, str)]
        cfg.deny += [t for t in perm.get("deny") or [] if isinstance(t, str)]
        cfg.found.append(Found(layer, path, "permissions", len(perm.get("allow") or []) + len(perm.get("deny") or [])))
    hook_files = [("project", os.path.join(cwd, ".cursor", "hooks.json"), cwd),
                  ("user", os.path.join(home_dir, ".cursor", "hooks.json"), os.path.join(home_dir, ".cursor"))]
    if home is None and sys.platform == "darwin":
        hook_files.insert(0, ("enterprise", "/Library/Application Support/Cursor/hooks.json",
                              "/Library/Application Support/Cursor"))
    for layer, path, run_dir in hook_files:
        if not os.path.isfile(path):
            continue
        try:
            hooks = _read_json(path).get("hooks") or {}
        except (OSError, ValueError):
            cfg.notes.append("Could not read %s." % code(path))
            continue
        count = 0
        for event in CURSOR_EVENTS:
            for handler in hooks.get(event) or [] if isinstance(hooks, dict) else []:
                if not isinstance(handler, dict) or handler.get("type", "command") != "command":
                    continue
                timeout = handler.get("timeout")
                cfg.hooks.append(HookDef("cursor", event, handler.get("matcher"), str(handler.get("command") or ""),
                                         float(timeout) if isinstance(timeout, (int, float)) and timeout > 0 else 30.0,
                                         layer, path, run_dir, bool(handler.get("failClosed"))))
                count += 1
                if not handler.get("failClosed"):
                    cfg.smells.append({"id": "cursor-fail-open", "severity": "low", "source": layer,
                                       "text": "A Cursor %s hook has no failClosed, so a crash or timeout lets "
                                               "the call through." % event})
        cfg.found.append(Found(layer, path, "hooks", count))
    for spec in claude_hooks:
        if spec.source not in ("user", "project", "local"):
            continue
        cfg.hooks.append(HookDef("cursor", "preToolUse", spec.matcher, spec.command,
                                 float(spec.timeout or 30.0), "claude-" + spec.source, spec.source_path, cwd))
        if C.hook_matcher_matches(spec.matcher, "Bash") and not C.hook_matcher_matches(spec.matcher, "Shell"):
            cfg.smells.append({"id": "cursor-bash-matcher", "severity": "medium", "source": "claude-" + spec.source,
                               "text": "Cursor also loads this Claude Code hook, but its matcher %s never fires "
                                       "there: Cursor calls its shell tool Shell." % code(json.dumps(spec.matcher))})
    if any(h.source.startswith("claude-") for h in cfg.hooks):
        cfg.notes.append("Cursor loads Claude Code hooks when \"Include Third-Party Plugins, Skills, and Other "
                         "Configs\" is on (the default); this test assumes it is on.")
    cfg.notes.append("The Cursor IDE keeps its command allowlist inside the app, which this test cannot read; "
                     "the Cursor results cover the CLI permission files and hooks.")
    return cfg


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] in ("-h", "--help"):
        print(__doc__.strip())
        sys.exit(0)
    print("harness_rules is a helper module for test_guards.py; run it with --help for details.")
