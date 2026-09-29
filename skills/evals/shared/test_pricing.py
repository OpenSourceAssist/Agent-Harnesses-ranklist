"""Tests for pricing.py. Expected prices are typed from the harness facts file
(Q9, checked 2026-09-28), not read from the module under test."""

import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import pricing as P  # noqa: E402


class U:
    """A usage object with the transcripts.Usage fields (pricing.py must not need transcripts.py)."""

    def __init__(self, input=0, cache_read=0, cache_write=0, cache_write_1h=0, output=0, reasoning=0):
        self.input, self.cache_read, self.cache_write = input, cache_read, cache_write
        self.cache_write_1h, self.output, self.reasoning = cache_write_1h, output, reasoning


M = 1_000_000

# model: (input, output, cache_read, cache_write_5m, cache_write_1h), USD per 1M tokens
FACTS_Q9 = {
    "claude-fable-5-1": (10.00, 50.00, 0.25, 12.50, 20.00),
    "claude-fable-5": (10.00, 50.00, 1.00, 12.50, 20.00),
    "claude-opus-5-5": (4.00, 20.00, 0.20, 5.00, 8.00),
    "claude-opus-5": (5.00, 25.00, 0.50, 6.25, 10.00),
    "claude-opus-4-8": (5.00, 25.00, 0.50, 6.25, 10.00),
    "claude-opus-4-7": (5.00, 25.00, 0.50, 6.25, 10.00),
    "claude-opus-4-6": (5.00, 25.00, 0.50, 6.25, 10.00),
    "claude-opus-4-5": (5.00, 25.00, 0.50, 6.25, 10.00),
    "claude-sonnet-5-5": (2.00, 10.00, 0.20, 2.50, 4.00),
    "claude-sonnet-5": (2.00, 10.00, 0.20, 2.50, 4.00),
    "claude-sonnet-4-6": (3.00, 15.00, 0.30, 3.75, 6.00),
    "claude-sonnet-4-5": (3.00, 15.00, 0.30, 3.75, 6.00),
    "claude-haiku-4-5": (1.00, 5.00, 0.10, 1.25, 2.00),
    "gpt-6-astra": (10.00, 50.00, 1.00, 12.50, 12.50),
    "gpt-6-sol": (2.00, 10.00, 0.20, 2.50, 2.50),
    "gpt-6-luna": (0.10, 0.50, 0.01, 0.125, 0.125),
    "gpt-5.6-sol": (4.00, 20.00, 0.40, 5.00, 5.00),
    "gpt-5.6-terra": (2.00, 12.00, 0.20, 2.50, 2.50),
    "gpt-5.6-luna": (0.20, 1.20, 0.02, 0.25, 0.25),
    "gpt-5.5": (5.00, 30.00, 0.50, None, None),
    "gpt-5.3-codex": (1.75, 14.00, 0.175, None, None),
}


@pytest.mark.parametrize("model", sorted(FACTS_Q9))
def test_every_model_in_the_facts_file_has_its_listed_prices(model):
    inp, out, read, w5m, w1h = FACTS_Q9[model]
    assert P.price_for(model) == {"input": inp, "output": out, "cache_read": read,
                                  "cache_write_5m": w5m, "cache_write_1h": w1h}


@pytest.mark.parametrize("model", sorted(FACTS_Q9))
def test_one_million_of_each_token_type_costs_the_sum_of_the_rates(model):
    inp, out, read, w5m, w1h = FACTS_Q9[model]
    usage = U(input=M, cache_read=M, cache_write=2 * M, cache_write_1h=M, output=M)
    write_5m = w5m if w5m is not None else inp
    write_1h = w1h if w1h is not None else write_5m
    assert P.cost_usd(usage, model) == pytest.approx(inp + read + write_5m + write_1h + out)


@pytest.mark.parametrize("model,same_as", [
    ("claude-opus-5-5[1m]", "claude-opus-5-5"),
    ("claude-haiku-4-5-20251001", "claude-haiku-4-5"),
    ("claude-sonnet-4-5-20250929", "claude-sonnet-4-5"),
    ("gpt-6-sol-2026-09-01", "gpt-6-sol"),
    ("claude-opus-4-7-thinking-high", "claude-opus-4-7"),
    ("claude-opus-5-5-thinking", "claude-opus-5-5"),
    ("claude-sonnet-5-5-preview", "claude-sonnet-5-5"),
])
def test_suffixes_are_stripped_and_the_longest_known_prefix_wins(model, same_as):
    assert P.price_for(model) == P.price_for(same_as) is not None


@pytest.mark.parametrize("model", [
    "claude-opus-5-6",          # a newer version is not priced as an older one
    "gpt-5.5-pro", "gpt-6-astra-mini", "gpt-6-luna-nano",   # another size is another price
    "gemini-3.1-pro", "gemini-2.5-flash", "codex-auto-review", "<synthetic>", "", None,
])
def test_unknown_models_are_unpriced(model):
    assert P.price_for(model) is None
    assert P.cost_usd(U(input=M, output=M), model) is None


def test_cache_writes_split_between_the_5_minute_and_1_hour_rates():
    usage = U(input=M, cache_read=M, cache_write=3 * M, cache_write_1h=M, output=M)
    # Opus 5.5: 4.00 input + 0.20 read + 2 x 5.00 (5-minute) + 1 x 8.00 (1-hour) + 20.00 output
    assert P.cost_usd(usage, "claude-opus-5-5") == pytest.approx(42.20)


def test_cache_writes_with_an_unknown_split_use_the_5_minute_rate():
    assert P.cost_usd(U(cache_write=2 * M), "claude-opus-5-5") == pytest.approx(10.00)
    assert P.cost_usd(U(cache_write=2 * M), "claude-sonnet-4-6") == pytest.approx(7.50)


def test_a_1_hour_part_larger_than_all_writes_is_capped():
    assert P.cost_usd(U(cache_write=M, cache_write_1h=5 * M), "claude-haiku-4-5") == pytest.approx(2.00)


def test_cache_reads_use_each_models_own_rate():
    reads = U(cache_read=10 * M)
    assert P.cost_usd(reads, "claude-fable-5-1") == pytest.approx(2.50)   # 0.025 x input
    assert P.cost_usd(reads, "claude-fable-5") == pytest.approx(10.00)    # 0.1 x input
    assert P.cost_usd(reads, "claude-opus-5-5") == pytest.approx(2.00)    # 0.05 x input


def test_reasoning_is_part_of_output_and_is_not_charged_twice():
    assert P.cost_usd(U(output=M, reasoning=M), "gpt-6-sol") == pytest.approx(10.00)


def test_cache_writes_without_a_listed_price_are_charged_as_input():
    assert P.cost_usd(U(cache_write=M), "gpt-5.5") == pytest.approx(5.00)


def test_a_small_session_costs_what_the_rates_say():
    # 12,000 uncached input, 480,000 cache reads, 30,000 1-hour writes, 6,000 output on Opus 5.5
    usage = U(input=12000, cache_read=480000, cache_write=30000, cache_write_1h=30000, output=6000)
    assert P.cost_usd(usage, "claude-opus-5-5") == pytest.approx(0.048 + 0.096 + 0.24 + 0.12)


def test_cost_accepts_a_plain_dict():
    usage = {"input": M, "cache_read": 0, "cache_write": 0, "cache_write_1h": 0, "output": 2 * M}
    assert P.cost_usd(usage, "gpt-6-astra") == pytest.approx(110.00)
    assert P.cost_usd({"output": M}, "claude-haiku-4-5") == pytest.approx(5.00)


def test_price_for_returns_a_copy():
    P.price_for("claude-opus-5-5")["input"] = 999
    assert P.price_for("claude-opus-5-5")["input"] == 4.00


def test_every_price_entry_can_be_used():
    for model in P.PRICES:
        assert P.cost_usd(U(input=1, cache_read=1, cache_write=2, cache_write_1h=1, output=1), model) > 0


@pytest.mark.parametrize("cloud_id, known", [
    ("us.anthropic.claude-opus-4-8-v1:0", "claude-opus-4-8"),
    ("anthropic.claude-sonnet-5-v1:0", "claude-sonnet-5"),
    ("eu.anthropic.claude-haiku-4-5-20251001-v1:0", "claude-haiku-4-5"),
    ("global.anthropic.claude-opus-5-5-v1", "claude-opus-5-5"),
    ("claude-opus-4-8@20260101", "claude-opus-4-8"),
    ("claude-sonnet-5@latest", "claude-sonnet-5"),
])
def test_cloud_provider_ids_price_like_the_model(cloud_id, known):
    assert P.price_for(cloud_id) == P.price_for(known) and P.price_for(known) is not None


def test_cloud_normalizing_keeps_unknown_versions_unknown():
    assert P.price_for("us.anthropic.claude-opus-5-6-v1:0") is None
