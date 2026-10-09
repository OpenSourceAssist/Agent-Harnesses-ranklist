"""Harness adapters: how to run each coding agent headless and read its output.

Each adapter is a small class with the same methods:
- available(): the program is on PATH.
- version(): "<program> --version", first line.
- build_command(prompt, workdir, caps): the argument list for one run. caps has
  "test_cmd" (the repository's test command) and "max_usd" (the budget left).
- parse_output(stdout, test_cmd=""): {cost_usd, cost_source, tokens, model, turns,
  error, denials, worked, test_runs, test_failures}. cost_source is "reported"
  (the harness said), "tokens" (tokens times the price table), "partial" (a
  stopped run, priced from what it reported), or None. worked is True when the
  output shows the model did work. error is the harness's own message.
- priced(): whether a run's dollar cost can be measured; unpriced_reason() and
  unpriced_fix() say why not and what to do; can_run_unpriced says whether
  --allow-unpriced may run it anyway.
- cost_range(): (low, high) dollars for one run, for estimates.

Every command uses the most restrictive settings that still let the agent edit
files in its workspace and run the test command. Flags come from the headless
docs of each harness, checked 2026-09-28 (see references/harness-commands.md).
OpenCode is left out: the JSON event format of `opencode run --format json` is
not documented. Python 3.9+, standard library only.
"""

from __future__ import annotations

import json
import math
import os
import re
import shutil
import subprocess

from common import clean_env
from pricing import cost_usd, price_for
from safe import code, safe_text

# Token use assumed for one run when estimating cost (Anthropic meanings:
# input is uncached input). A short fix and a long one; both are guesses.
LOW_RUN = {"input": 20_000, "cache_read": 150_000, "cache_write": 20_000, "output": 4_000}
HIGH_RUN = {"input": 150_000, "cache_read": 3_000_000, "cache_write": 150_000, "output": 50_000}


def command_parts(test_cmd) -> list:
    """The simple commands inside a compound test command, such as
    "npm ci && npm test". Permission rules match each part on its own."""
    return [p.strip() for p in re.split(r"&&|\|\||;|\|", test_cmd) if p.strip()]


def _int(value) -> int:
    return value if isinstance(value, int) and not isinstance(value, bool) else 0


def _json(text):
    try:
        return json.loads(text)
    except ValueError:
        return None


def _records(stdout) -> list:
    """Every JSON object printed one per line."""
    return [r for r in (_json(line) for line in stdout.splitlines() if line.startswith("{")) if isinstance(r, dict)]


_AUTH_RE = re.compile(r"(?i)authenticat|oauth|not logged in|please (log|sign) ?in|invalid api key|unauthorized")
_VERSION_RE = re.compile(r"(?i)newer version|please upgrade|update required")
_MISSING_RE = re.compile(r"(?i)no such file or directory|command not found")


def fatal_hint(label, binary, error) -> str:
    """The fix for an error that will repeat on every task (a login, an old
    version, a missing program), or "" for an error of one run."""
    if _AUTH_RE.search(error):
        return "sign in: run %s once" % binary
    if _VERSION_RE.search(error):
        return "upgrade %s" % label
    if _MISSING_RE.search(error):
        return "install %s, or put %s on PATH" % (label, binary)
    return ""


def _add(total, part) -> dict:
    return {k: (total or {}).get(k, 0) + part.get(k, 0) for k in ("input", "cache_read", "cache_write", "output")}


class Harness:
    name = ""
    label = ""
    binary = ""
    low_model = high_model = None
    can_run_unpriced = True

    def __init__(self, model=None):
        self.model = model

    def available(self) -> bool:
        return shutil.which(self.binary) is not None

    def version(self) -> str:
        try:
            res = subprocess.run([self.binary, "--version"], capture_output=True, text=True, timeout=30,
                                 stdin=subprocess.DEVNULL, env=clean_env())
        except (OSError, subprocess.TimeoutExpired):
            return ""
        lines = [line for line in res.stdout.splitlines() if line.strip()]
        return safe_text(lines[0], 60) if lines else ""

    def priced(self) -> bool:
        return self.model is not None and price_for(self.model) is not None

    def unpriced_reason(self) -> str:
        return "the price table has no price for %s" % code(self.model, 60)

    def unpriced_fix(self) -> str:
        return "Pass --allow-unpriced %s to run it anyway; its runs count $0" % self.name

    def estimate_models(self) -> tuple:
        """(model for the low estimate, model for the high one), or () when unpriced."""
        if self.priced():
            return (self.model, self.model)
        return (self.low_model, self.high_model) if self.low_model else ()

    def cost_range(self):
        models = self.estimate_models()
        return (cost_usd(LOW_RUN, models[0]), cost_usd(HIGH_RUN, models[1])) if models else None

    def fallback_cost(self) -> float:
        """What the spend cap counts for a run whose cost could not be read."""
        rng = self.cost_range()
        return rng[1] if rng else 0.0


class ClaudeCode(Harness):
    name, label, binary = "claude-code", "Claude Code", "claude"
    low_model, high_model = "claude-sonnet-5-5", "claude-opus-5-5"

    def priced(self) -> bool:
        return True  # the result carries total_cost_usd

    def estimate_models(self) -> tuple:
        if self.model and price_for(self.model):
            return (self.model, self.model)
        return (self.low_model, self.high_model)

    def build_command(self, prompt, workdir, caps) -> list:
        # -p cannot answer permission prompts, so anything not allowed here is denied.
        rules = []
        for part in command_parts(caps["test_cmd"]):
            rules += ["Bash(%s)" % part, "Bash(%s *)" % part]
        argv = [self.binary, "-p", prompt, "--output-format", "stream-json", "--verbose",
                "--permission-mode", "acceptEdits", "--allowedTools", ",".join(rules),
                "--no-session-persistence", "--strict-mcp-config"]  # no --mcp-config: no MCP server loads
        if caps.get("max_usd") is not None:
            cents = math.floor(caps["max_usd"] * 100 + 1e-9)
            argv += ["--max-budget-usd", "%.2f" % (max(cents, 1) / 100)]
        if self.model:
            argv += ["--model", self.model]
        return argv

    def parse_output(self, stdout, test_cmd="") -> dict:
        result, messages = None, {}
        for rec in _records(stdout):
            if rec.get("type") == "result":
                result = rec
            elif rec.get("type") == "assistant":
                msg = rec.get("message") or {}
                if msg.get("id") and isinstance(msg.get("usage"), dict) and msg.get("model") != "<synthetic>":
                    messages[msg["id"]] = (msg.get("model") or "", msg["usage"])  # split records: keep the last
        if result is None and "{" in stdout:
            whole = _json(stdout[stdout.find("{"):])
            result = whole if isinstance(whole, dict) and whole.get("type") == "result" else None
        if result is not None:
            by_model = result.get("modelUsage") or {}
            tokens = None
            for usage in by_model.values():
                tokens = _add(tokens, {"input": _int(usage.get("inputTokens")),
                                       "cache_read": _int(usage.get("cacheReadInputTokens")),
                                       "cache_write": _int(usage.get("cacheCreationInputTokens")),
                                       "output": _int(usage.get("outputTokens"))})
            cost = result.get("total_cost_usd")
            cost = float(cost) if isinstance(cost, (int, float)) and not isinstance(cost, bool) else None
            error = ""
            if result.get("is_error"):  # the message, never the subtype ("success" on a failed login)
                errors = result.get("errors") if isinstance(result.get("errors"), list) else []
                error = safe_text(result.get("result") or (errors[0] if errors else "")
                                  or result.get("terminal_reason") or "error")
            return {"cost_usd": cost, "cost_source": "reported" if cost is not None else None, "tokens": tokens,
                    "model": max(by_model, key=lambda m: by_model[m].get("costUSD") or 0) if by_model else "",
                    "turns": result.get("num_turns"), "error": error,
                    "denials": len(result.get("permission_denials") or []), "worked": bool(messages),
                    "test_runs": None, "test_failures": None}
        tokens, cost, output_by_model = None, 0.0, {}
        for model, usage in messages.values():
            part = {"input": _int(usage.get("input_tokens")), "cache_read": _int(usage.get("cache_read_input_tokens")),
                    "cache_write": _int(usage.get("cache_creation_input_tokens")), "output": _int(usage.get("output_tokens")),
                    "cache_write_1h": _int((usage.get("cache_creation") or {}).get("ephemeral_1h_input_tokens"))}
            tokens = _add(tokens, part)
            price = cost_usd(part, model)
            cost = None if cost is None or price is None else cost + price
            output_by_model[model] = output_by_model.get(model, 0) + part["output"]
        if tokens is None:
            cost = None
        return {"cost_usd": cost, "cost_source": "partial" if cost is not None else None, "tokens": tokens,
                "model": max(output_by_model, key=output_by_model.get) if output_by_model else "", "turns": None,
                "error": "no final result: the run was stopped" if messages else "no output", "denials": 0,
                "worked": bool(messages), "test_runs": None, "test_failures": None}


def _codex_config_model():
    """The top-level `model` in Codex's config.toml, or None."""
    home = os.environ.get("CODEX_HOME") or os.path.join(os.path.expanduser("~"), ".codex")
    try:
        with open(os.path.join(home, "config.toml"), encoding="utf-8") as fh:
            lines = fh.read().splitlines()
    except OSError:
        return None
    for line in lines:
        if line.strip().startswith("["):
            return None  # later keys belong to tables such as [profiles.x]
        m = re.match(r"""\s*model\s*=\s*["']([^"']+)["']""", line)
        if m:
            return m.group(1)
    return None


class Codex(Harness):
    name, label, binary = "codex", "Codex", "codex"
    can_run_unpriced = False  # without a model there is no price, and no cap

    def __init__(self, model=None):
        # `codex exec --json` reports tokens but neither model nor cost, so the
        # model comes from --model or config.toml, and the price from pricing.py.
        super().__init__(model or _codex_config_model())

    def unpriced_reason(self) -> str:
        return "Codex's config names no model" if self.model is None else super().unpriced_reason()

    def unpriced_fix(self) -> str:
        return "Pin one with --model codex=<id>"

    def build_command(self, prompt, workdir, caps) -> list:
        argv = [self.binary, "exec", "--json", "--sandbox", "workspace-write", "--ephemeral"]
        if self.model:
            argv += ["--model", self.model]
        return argv + [prompt]

    def parse_output(self, stdout, test_cmd="") -> dict:
        tokens, error, denials, worked, runs, failures = None, "", 0, False, 0, 0
        parts = command_parts(test_cmd) if test_cmd else []
        for rec in _records(stdout):
            kind = rec.get("type")
            if kind == "turn.completed":
                u = rec.get("usage") or {}
                tokens = _add(tokens, {"input": _int(u.get("input_tokens")) - _int(u.get("cached_input_tokens")),
                                       "cache_read": _int(u.get("cached_input_tokens")),
                                       "cache_write": _int(u.get("cache_write_input_tokens")),
                                       "output": _int(u.get("output_tokens"))})  # includes reasoning
            elif kind == "turn.failed":
                error = (rec.get("error") or {}).get("message") or "turn failed"
            elif kind == "error":
                error = rec.get("message") or "error"
            elif kind == "item.completed":
                worked = True
                item = rec.get("item") or {}
                if item.get("type") == "command_execution":
                    denials += item.get("status") == "declined"
                    if any(part in str(item.get("command") or "") for part in parts):  # a run of the test command
                        runs += 1
                        code = item.get("exit_code")
                        failures += item.get("status") == "failed" or (isinstance(code, int) and code != 0)
        cost = cost_usd(tokens, self.model) if tokens is not None and self.model else None
        return {"cost_usd": cost, "cost_source": "tokens" if cost is not None else None, "tokens": tokens,
                "model": self.model or "", "turns": None, "error": safe_text(error), "denials": int(denials),
                "worked": worked, "test_runs": runs, "test_failures": int(failures)}


class GeminiCli(Harness):
    name, label, binary = "gemini-cli", "Gemini CLI", "gemini"

    def __init__(self, model=None):
        if model:
            raise ValueError("Gemini CLI takes no --model here: its headless docs list no model flag. "
                             "Set the model in Gemini CLI's settings instead.")
        super().__init__()

    def priced(self) -> bool:
        return False  # no cost field, and pricing.py has no confirmed Gemini prices

    def unpriced_reason(self) -> str:
        return "it reports no cost, and the price table has no Gemini prices"

    def build_command(self, prompt, workdir, caps) -> list:
        # --skip-trust: a new folder is untrusted, and untrusted folders turn off auto_edit.
        argv = [self.binary, "--output-format", "json", "--approval-mode", "auto_edit", "--skip-trust",
                "--allowed-tools"]
        argv += ["run_shell_command(%s)" % part for part in command_parts(caps["test_cmd"])]
        return argv + ["-p", prompt]

    def parse_output(self, stdout, test_cmd="") -> dict:
        doc = _json(stdout[stdout.find("{"):]) if "{" in stdout else None
        if not isinstance(doc, dict):
            return {"cost_usd": None, "cost_source": None, "tokens": None, "model": "", "turns": None,
                    "error": "no JSON result", "denials": 0, "worked": False, "test_runs": None,
                    "test_failures": None}
        stats = doc.get("stats") or {}
        models = stats.get("models") or {}
        tokens, cost, turns, size = None, 0.0 if models else None, 0, {}
        for name, entry in models.items():
            t = entry.get("tokens") or {}
            cached = _int(t.get("cached"))
            part = {"input": _int(t["input"]) if "input" in t else _int(t.get("prompt")) - cached,
                    "cache_read": cached, "cache_write": 0,
                    "output": _int(t.get("candidates")) + _int(t.get("thoughts"))}
            tokens = _add(tokens, part)
            price = cost_usd(part, name)
            cost = None if cost is None or price is None else cost + price
            turns += _int((entry.get("api") or {}).get("totalRequests"))
            size[name] = sum(part.values())
        error = doc.get("error")
        return {"cost_usd": cost, "cost_source": "tokens" if cost is not None else None, "tokens": tokens,
                "model": max(size, key=size.get) if size else "", "turns": turns if models else None,
                "error": safe_text(error.get("message") or "error") if isinstance(error, dict) else "",
                "denials": _int(((stats.get("tools") or {}).get("totalDecisions") or {}).get("reject")),
                "worked": turns > 0, "test_runs": None, "test_failures": None}


ADAPTERS = {cls.name: cls for cls in (ClaudeCode, Codex, GeminiCli)}
