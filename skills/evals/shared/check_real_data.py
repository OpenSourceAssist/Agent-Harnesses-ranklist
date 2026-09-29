#!/usr/bin/env python3
"""Read-only check of transcripts.py and pricing.py against this machine's sessions.

Loads the recent sessions of every harness found here and prints aggregate
counts only: no file contents, prompts, messages, or paths. For Claude Code it
also compares computed cost with the `cost-state` totals Claude Code records.

Usage:
    python3 skills/evals/shared/check_real_data.py [--since 30] [--harness NAME] [--json]
    --home <folder>   read this folder instead of the real home folder (tests)

Exit codes: 0 done (also when no harness is found), 2 usage error.
Python 3.9+, standard library only.
"""

from __future__ import annotations

import argparse
import collections
import glob
import json
import os
import re
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import pricing  # noqa: E402
import transcripts  # noqa: E402

_LINE_PREFIX_RE = re.compile(r"^line \d+: ")


def _line_count(harness, path, session) -> int:
    if harness == "opencode":  # database rows are not lines; count what was read
        return len(session.events)
    try:
        with open(path, "rb") as fh:
            return sum(1 for _ in fh)
    except OSError:
        return 0


def _cost_state_check(home, since) -> dict:
    """Compare the last cost-state total in each main Claude Code transcript with
    the cost computed from that transcript and its subagent files."""
    sessions = within = 0
    recorded = computed = 0.0
    for harness, path in transcripts.find_sessions("claude-code", since, home=home, include_subagents=False):
        total = None
        with open(path, "rb") as fh:
            for raw in fh:
                if b'"cost-state"' in raw:
                    try:
                        rec = json.loads(raw)
                    except ValueError:
                        continue
                    if rec.get("type") == "cost-state" and rec.get("modelUsage") and \
                            isinstance(rec.get("totalCostUSD"), (int, float)):
                        total = float(rec["totalCostUSD"])
        if total is None:
            continue
        side = os.path.join(glob.escape(path[:-len(".jsonl")]), "subagents")
        files = [path] + sorted(glob.glob(os.path.join(side, "agent-*.jsonl")) +
                                glob.glob(os.path.join(side, "workflows", "*", "agent-*.jsonl")))
        cost = 0.0
        for f in files:
            for model, usage in transcripts.load_session(harness, f).usage_by_model().items():
                cost += pricing.cost_usd(usage, model) or 0.0
        sessions += 1
        within += abs(cost - total) < 0.01
        recorded += total
        computed += cost
    return {"sessions": sessions, "within_1_cent": within, "recorded_usd": round(recorded, 4),
            "computed_usd": round(computed, 4)}


def check(harness, since, home=None) -> dict:
    started = time.time()
    counts = collections.Counter()
    by_kind, denials, events, warnings = (collections.Counter() for _ in range(4))
    tokens, seen, repeated = {}, set(), collections.Counter()
    # Oldest first, so that when a forked session copies earlier records (same
    # ids), the original keeps them and the copy is counted as repeated.
    for h, path in reversed(transcripts.find_sessions(harness, since, home=home)):
        s = transcripts.load_session(h, path)
        counts["sessions"] += 1
        counts["subagent_sessions" if s.is_subagent else "main_sessions"] += 1
        counts["lines"] += _line_count(h, path, s)
        for e in s.events:
            if e.id and (e.kind, e.id) in seen:
                repeated[e.kind] += 1
                continue
            seen.add((e.kind, e.id))
            events[e.kind] += 1
            if e.tool is not None:
                by_kind[e.tool.kind] += 1
                counts["tool_errors"] += e.tool.is_error
                if e.tool.denied:
                    denials[e.tool.denied] += 1
            if e.usage is not None:
                tokens[e.model] = tokens.get(e.model, transcripts.Usage()) + e.usage
        warnings.update(_LINE_PREFIX_RE.sub("", w) for w in s.warnings)
    costs = {m: pricing.cost_usd(u, m) for m, u in tokens.items()}
    report = {
        "sessions": counts["sessions"], "main_sessions": counts["main_sessions"],
        "subagent_sessions": counts["subagent_sessions"], "lines": counts["lines"],
        "events": sum(events.values()), "events_by_kind": dict(events),
        "tool_calls": sum(by_kind.values()), "tool_calls_by_kind": dict(by_kind),
        "tool_errors": counts["tool_errors"], "denials_by_kind": dict(denials),
        "repeated_responses": repeated["assistant"], "repeated_tool_calls": repeated["tool"],
        "interrupts": events["interrupt"], "compactions": events["compaction"], "api_errors": events["api_error"],
        "tokens_by_model": {m: vars(u) for m, u in sorted(tokens.items())},
        "cost_by_model_usd": {m: round(c, 4) for m, c in sorted(costs.items()) if c is not None},
        "unpriced_models": sorted(m for m, c in costs.items() if c is None),
        "warnings": sum(warnings.values()), "top_warnings": [list(x) for x in warnings.most_common(5)],
        "warnings_percent_of_lines": round(100.0 * sum(warnings.values()) / max(counts["lines"], 1), 4),
    }
    if harness == "claude-code":
        report["cost_state_check"] = _cost_state_check(home, since)
    report["seconds"] = round(time.time() - started, 2)
    return report


def _text(report) -> str:
    out = ["**Real-data check: %d harness(es), last %s days.**" % (len(report["harnesses"]), report["since_days"])]
    if not report["harnesses"]:
        out.append("No harness data found.")
    for name, r in report["harnesses"].items():
        out.append("")
        out.append("## %s" % name)
        out.append("- sessions: %d (main %d, subagent %d), lines %d, events %d, read in %.1f s" % (
            r["sessions"], r["main_sessions"], r["subagent_sessions"], r["lines"], r["events"], r["seconds"]))
        out.append("- tool calls: %d %s; errors %d; denials %s" % (
            r["tool_calls"], json.dumps(r["tool_calls_by_kind"], sort_keys=True), r["tool_errors"],
            json.dumps(r["denials_by_kind"], sort_keys=True)))
        out.append("- interrupts %d, compactions %d, API errors %d" % (r["interrupts"], r["compactions"], r["api_errors"]))
        out.append("- copied from other sessions and counted once: %d responses, %d tool calls" % (
            r["repeated_responses"], r["repeated_tool_calls"]))
        for model, u in r["tokens_by_model"].items():
            cost = r["cost_by_model_usd"].get(model)
            out.append("- %s: input %d, cache read %d, cache write %d (1-hour %d), output %d (reasoning %d); %s" % (
                model or "(no model)", u["input"], u["cache_read"], u["cache_write"], u["cache_write_1h"],
                u["output"], u["reasoning"], "$%.4f" % cost if cost is not None else "unpriced"))
        out.append("- warnings: %d (%.4f%% of lines) %s" % (r["warnings"], r["warnings_percent_of_lines"],
                                                           json.dumps(r["top_warnings"])))
        if "cost_state_check" in r:
            c = r["cost_state_check"]
            out.append("- cost-state check: %d sessions, %d within 1 cent; recorded $%.4f, computed $%.4f" % (
                c["sessions"], c["within_1_cent"], c["recorded_usd"], c["computed_usd"]))
    return "\n".join(out)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Aggregate check of transcripts.py and pricing.py on real sessions.")
    parser.add_argument("--since", type=int, default=30, help="days of sessions to read (default 30)")
    parser.add_argument("--harness", choices=transcripts.HARNESSES, help="check one harness only")
    parser.add_argument("--home", help="read this folder instead of the home folder")
    parser.add_argument("--json", action="store_true", help="print JSON")
    args = parser.parse_args(argv)
    found = transcripts.detect_harnesses(home=args.home)
    names = [h for h in transcripts.HARNESSES if h in found and args.harness in (None, h)]
    report = {"since_days": args.since, "harnesses": {h: check(h, args.since, args.home) for h in names}}
    print(json.dumps(report, indent=2) if args.json else _text(report))
    return 0


if __name__ == "__main__":
    sys.exit(main())
