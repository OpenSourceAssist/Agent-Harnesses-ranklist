"""Token prices and cost math for the models that coding agents use.

Prices are USD per 1 million tokens, copied from the official pricing pages on
PRICES_CHECKED (see the source on each group below). Usage fields follow
Anthropic meanings, as transcripts.Usage does: `input` is uncached input,
`cache_read` and `cache_write` are separate, and `output` already includes
reasoning tokens, so reasoning is never charged twice.

Not covered, so a computed cost can differ from a bill:
- Batch discounts, fast mode, US-only inference (1.1x), and web search fees.
- GPT long-context rates (prompts over 272K input tokens cost 2x input and
  cache, 1.5x output). Codex keeps its context window below that (258,400
  tokens in local rollouts), so standard rates apply.
- Gemini models: no price was confirmed on Google's official pricing page, so
  Gemini usage is reported as unpriced tokens.
- Prices on Amazon Bedrock and Google Vertex AI, which those platforms set.

Python 3.9+, standard library only.
"""

from __future__ import annotations

import re
from typing import Optional

PRICES_CHECKED = "2026-09-28"


def _anthropic(inp, cache_read, write_5m, write_1h, out) -> dict:
    return {"input": inp, "output": out, "cache_read": cache_read,
            "cache_write_5m": write_5m, "cache_write_1h": write_1h}


def _openai(inp, cached, write, out) -> dict:
    # One cache-write rate; None where the pricing page lists none.
    return {"input": inp, "output": out, "cache_read": cached,
            "cache_write_5m": write, "cache_write_1h": write}


PRICES = {
    # Anthropic API: https://platform.claude.com/docs/en/about-claude/pricing.md
    # Cache reads are 0.1x input, except 0.025x on Fable 5.1 and Mythos 5.1 and 0.05x on Opus 5.5.
    "claude-fable-5-1": _anthropic(10.00, 0.25, 12.50, 20.00, 50.00),
    "claude-mythos-5-1": _anthropic(10.00, 0.25, 12.50, 20.00, 50.00),  # Fable 5.1 prices; id follows the naming pattern
    "claude-fable-5": _anthropic(10.00, 1.00, 12.50, 20.00, 50.00),
    "claude-opus-5-5": _anthropic(4.00, 0.20, 5.00, 8.00, 20.00),
    "claude-opus-5": _anthropic(5.00, 0.50, 6.25, 10.00, 25.00),
    "claude-opus-4-8": _anthropic(5.00, 0.50, 6.25, 10.00, 25.00),
    "claude-opus-4-7": _anthropic(5.00, 0.50, 6.25, 10.00, 25.00),
    "claude-opus-4-6": _anthropic(5.00, 0.50, 6.25, 10.00, 25.00),
    "claude-opus-4-5": _anthropic(5.00, 0.50, 6.25, 10.00, 25.00),
    "claude-sonnet-5-5": _anthropic(2.00, 0.20, 2.50, 4.00, 10.00),
    "claude-sonnet-5": _anthropic(2.00, 0.20, 2.50, 4.00, 10.00),
    "claude-sonnet-4-6": _anthropic(3.00, 0.30, 3.75, 6.00, 15.00),
    "claude-sonnet-4-5": _anthropic(3.00, 0.30, 3.75, 6.00, 15.00),
    "claude-haiku-4-5": _anthropic(1.00, 0.10, 1.25, 2.00, 5.00),
    # OpenAI API, standard rates up to 272K input tokens: https://developers.openai.com/api/docs/pricing.md
    # and the model pages https://developers.openai.com/api/docs/models/gpt-6-sol.md (also gpt-6-astra, gpt-6-luna).
    "gpt-6-astra": _openai(10.00, 1.00, 12.50, 50.00),
    "gpt-6-sol": _openai(2.00, 0.20, 2.50, 10.00),
    "gpt-6-luna": _openai(0.10, 0.01, 0.125, 0.50),
    "gpt-5.6-sol": _openai(4.00, 0.40, 5.00, 20.00),
    "gpt-5.6-terra": _openai(2.00, 0.20, 2.50, 12.00),
    "gpt-5.6-luna": _openai(0.20, 0.02, 0.25, 1.20),
    "gpt-5.5": _openai(5.00, 0.50, None, 30.00),
    "gpt-5.3-codex": _openai(1.75, 0.175, None, 14.00),
}

_CONTEXT_SUFFIX_RE = re.compile(r"\[[^\]]*\]$")            # claude-opus-5-5[1m]
_DATE_SUFFIX_RE = re.compile(r"-(?:\d{8}|\d{4}-\d\d-\d\d)$")  # -20251001 or -2026-09-01
# Cloud ids for the same models: Amazon Bedrock (us.anthropic.claude-opus-4-8-v1:0)
# and Google Vertex AI (claude-opus-4-8@20260101). They get the model's list price:
# the clouds set their own prices, which can differ a little (regional endpoints),
# but a list price keeps a spend cap working where "unknown" would count $0.
_PROVIDER_PREFIX_RE = re.compile(r"^(?:(?:us|eu|apac|jp|au|global|us-gov)\.)?(?:anthropic|openai)\.")
_PROVIDER_SUFFIX_RE = re.compile(r"(?:-v\d+(?::\d+)?|@\d{8}|@latest)$")
_SEPARATORS = "-_.@:"
_SIZE_WORDS = {"mini", "nano", "pro", "max", "lite", "turbo"}  # another size is another price


def price_for(model) -> Optional[dict]:
    """Prices for a model id: the exact id, then the id without a cloud
    provider prefix or suffix (Bedrock, Vertex AI), a context suffix, or a
    date suffix, then the longest known id it starts with. A newer version
    number or another size after the known id (claude-opus-5-6, gpt-5.5-pro)
    does not count as a match. Returns None when the model is unknown."""
    if not isinstance(model, str) or not model.strip():
        return None
    name = model.strip()
    if name not in PRICES:
        name = _PROVIDER_SUFFIX_RE.sub("", _CONTEXT_SUFFIX_RE.sub("", _PROVIDER_PREFIX_RE.sub("", name)))
        name = _DATE_SUFFIX_RE.sub("", name)
    if name not in PRICES:
        best = ""
        for known in PRICES:
            if len(name) > len(known) and name.startswith(known) and name[len(known)] in _SEPARATORS:
                next_word = re.split(r"[-_.@:]", name[len(known) + 1:])[0]
                if not next_word.isdigit() and next_word not in _SIZE_WORDS and len(known) > len(best):
                    best = known
        name = best
    return dict(PRICES[name]) if name in PRICES else None


def _count(usage, key) -> int:
    value = usage.get(key, 0) if isinstance(usage, dict) else getattr(usage, key, 0)
    return value if isinstance(value, int) and not isinstance(value, bool) else 0


def cost_usd(usage, model) -> Optional[float]:
    """Dollar cost of one usage (a transcripts.Usage or a dict with the same
    keys) on one model. None when the model is unknown: report those tokens as
    unpriced. Cache writes without a known 1-hour part are priced at the
    5-minute rate; a model with no listed cache-write price charges writes as input."""
    p = price_for(model)
    if p is None:
        return None
    write = _count(usage, "cache_write")
    write_1h = min(_count(usage, "cache_write_1h"), write)
    rate_5m = p["cache_write_5m"] if p["cache_write_5m"] is not None else p["input"]
    rate_1h = p["cache_write_1h"] if p["cache_write_1h"] is not None else rate_5m
    total = (_count(usage, "input") * p["input"] + _count(usage, "cache_read") * p["cache_read"]
             + (write - write_1h) * rate_5m + write_1h * rate_1h + _count(usage, "output") * p["output"])
    return total / 1_000_000
