#!/usr/bin/env python3
"""Check a coding agent's "tests pass" claims against its own session
transcripts, and look for weakened tests in the current git diff.

    python3 claims.py scan [--since 30d] [--harness all] [--project DIR] [--json] [--out PATH] [--fail]
    python3 claims.py diff [--repo .] [--base REF] [--json] [--out PATH] [--fail]

Read-only. Python 3.9+, standard library only. Nothing leaves the machine.
Exit codes: 0 done, 1 findings with --fail, 2 usage or input error.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time

sys.dont_write_bytecode = True  # leave no __pycache__ in the skill folder
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import evidence as E  # noqa: E402
import transcripts as T  # noqa: E402
import weakened as W  # noqa: E402
from safe import code  # noqa: E402

LABELS = ("backed", "stale", "contradicted", "unsupported", "unclear")
NOT_BACKED = ("stale", "contradicted", "unsupported")
MEANINGS = {
    "backed": "The latest matching run before the claim passed, and no code changed after it.",
    "stale": "The run passed, but code changed after it and nothing ran again.",
    "contradicted": "The latest matching run failed.",
    "unsupported": "No matching run happened in the session before the claim.",
    "unclear": "The evidence could not be read or ordered; the why column says which case.",
}
NAMES = {"claude-code": "Claude Code", "codex": "Codex", "gemini-cli": "Gemini CLI", "opencode": "OpenCode"}
_WORST = {"contradicted": 0, "unsupported": 1, "stale": 2, "unclear": 3, "backed": 4}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _days(text) -> int:
    m = re.match(r"^\s*(\d+)\s*([dDwW]?)\s*$", text or "")
    if not m or int(m.group(1)) <= 0:
        raise argparse.ArgumentTypeError("use a number of days such as 30d, or weeks such as 2w")
    return int(m.group(1)) * (7 if m.group(2).lower() == "w" else 1)


def _home_path(path) -> str:
    """A path for display: the home folder shown as ~, made safe to print."""
    home = os.path.expanduser("~")
    path = str(path or "")
    if home and home != "/" and (path == home or path.startswith(home + os.sep)):
        path = "~" + path[len(home):]
    return T.safe_text(path, 160)


def _when(ts) -> str:
    t = E.epoch(ts)
    return time.strftime("%Y-%m-%d %H:%M UTC", time.gmtime(t)) if t is not None else "unknown time"


def _plural(n, one, many=None) -> str:
    return "%d %s" % (n, one if n == 1 else (many or one + "s"))


def _write(text, out_path) -> None:
    if out_path:
        with open(out_path, "w", encoding="utf-8") as fh:
            fh.write(text)
        print("Report written to %s" % out_path)
    else:
        try:
            sys.stdout.reconfigure(errors="replace")
        except (AttributeError, ValueError):
            pass
        sys.stdout.write(text)


# ---------------------------------------------------------------------------
# scan
# ---------------------------------------------------------------------------

def _example(claim) -> dict:
    run = claim["run"]
    return {
        "label": claim["label"], "why": claim["why"], "kind": claim["kind"], "harness": claim["harness"],
        "session": T.safe_text(claim["session"], 60), "time": _when(claim["ts"]),
        "project": _home_path(claim["cwd"]), "transcript": _home_path(claim["path"]),
        "claim": T.safe_text(claim["excerpt"], 160),
        "run": None if run is None else {"command": T.safe_text(run["command"], 160), "result": run["result"],
                                         "detail": T.safe_text(run["detail"], 80), "time": _when(run["ts"])},
        "changed": [_home_path(p) for p in claim["changed"][:5]], "changed_count": len(claim["changed"]),
    }


def _headline(n, not_backed, unclear, days, sessions) -> str:
    window = "the last %s" % _plural(days, "day")
    if sessions == 0:
        return "No agent sessions from %s were found on this machine." % window
    if n == 0:
        return "No claims that tests or builds passed were found in %s from %s." % (_plural(sessions, "session"),
                                                                                    window)
    first = "Found %s that tests or builds passed in %s." % (_plural(n, "claim"), window)
    if not_backed:
        return "%s %d of those claims %s not backed by a passing run." % (first, not_backed,
                                                                        "was" if not_backed == 1 else "were")
    if unclear:
        return "%s None was contradicted, stale, or unsupported; %d could not be checked." % (first, unclear)
    return first + " Every one was backed by a passing run."


def scan(days=30, harness="all", project=None, examples=10) -> dict:
    """Label every claim in the main sessions of the last `days` days."""
    found = T.find_sessions(harness=harness, since_days=days, project=project)
    loaded, seen = [], set()
    for h, p in found:
        seen.add(p)
        loaded.append(T.load_session(h, p))
    mains = [s for s in loaded if not s.is_subagent]
    for s in mains:
        if s.harness == "claude-code":
            for p in E.subagent_files(s.path):
                if p not in seen:
                    seen.add(p)
                    loaded.append(T.load_session("claude-code", p))
    subs = [s for s in loaded if s.is_subagent]
    children = {}
    for s in subs:
        children.setdefault((s.harness, s.parent_id), []).append(s)
    # Count each claim once and only inside the window: a forked session repeats
    # earlier records, and a recently modified file can hold months-old ones.
    # The walk itself still reads every earlier record as evidence.
    counted = {id(e) for _s, e in T.unique_events(mains, since=T.cutoff(days))}
    claims = []
    for s in mains:
        claims.extend(c for c in E.label_claims(s, children.get((s.harness, s.id), []))
                      if id(c["event"]) in counted)

    by_label = {label: 0 for label in LABELS}
    by_kind = {kind: {label: 0 for label in LABELS} for kind in (E.TESTS, E.BUILD)}
    by_harness = {}
    for c in claims:
        by_label[c["label"]] += 1
        by_kind[c["kind"]][c["label"]] += 1
        by_harness.setdefault(c["harness"], {label: 0 for label in LABELS})[c["label"]] += 1
    harnesses = {}
    for s in mains:
        harnesses[s.harness] = harnesses.get(s.harness, 0) + 1
    not_backed = sum(by_label[label] for label in NOT_BACKED)
    worst = sorted((c for c in claims if c["label"] in NOT_BACKED),
                   key=lambda c: (_WORST[c["label"]], -(E.epoch(c["ts"]) or 0)))
    unclear = sorted((c for c in claims if c["label"] == "unclear"), key=lambda c: -(E.epoch(c["ts"]) or 0))
    skipped = sum(len(s.warnings) for s in loaded)

    notes = ["Claims are found by their wording, so a claim in unusual phrasing can be missed."]
    if skipped:
        notes.append("%s could not be read and %s skipped; the format is internal to each harness and changes "
                     "between versions." % (_plural(skipped, "transcript line"), "was" if skipped == 1 else "were"))
    silent = sum(1 for c in claims if c["why"] == "no tool calls recorded")
    if silent:
        notes.append("%s came from transcripts that record no tool calls, so %s could not be checked and %s "
                     "counted as unclear." % (_plural(silent, "claim"), "it" if silent == 1 else "they",
                                              "is" if silent == 1 else "are"))
    if subs:
        notes.append("Claims inside subagent transcripts are not counted; the runs and file changes in them count "
                     "as evidence for the main session.")
    if harness in (None, "all") and os.path.isdir(os.path.expanduser("~/.cursor")):
        notes.append("Cursor keeps no test results or times in its transcripts, so its sessions are not checked.")
    if "opencode" in harnesses:
        notes.append("OpenCode support follows its documented database layout and is not verified on a real "
                     "install.")
    return {
        "headline": _headline(len(claims), not_backed, by_label["unclear"], days, len(mains)),
        "window_days": days, "sessions": len(mains), "subagent_sessions": len(subs), "harnesses": harnesses,
        "claims": len(claims), "not_backed": not_backed, "by_label": by_label, "by_kind": by_kind,
        "by_harness": by_harness, "examples": [_example(c) for c in worst[:max(0, examples)]],
        "unclear_examples": [_example(c) for c in unclear[:max(0, examples)]],
        "notes": notes, "skipped_lines": skipped,
    }


def _example_lines(examples) -> list:
    """Numbered evidence for each example claim. Every piece of transcript
    text goes through code(), so it sits in inline code as one inert line."""
    lines = []
    for i, ex in enumerate(examples, 1):
        what = "test" if ex["kind"] == E.TESTS else "build"
        lines.append("%d. **%s** (%s claim), %s, %s, session %s, project %s" % (
            i, ex["label"], what, ex["time"], NAMES.get(ex["harness"], ex["harness"]), code(ex["session"][:12]),
            code(ex["project"]) if ex["project"] else "unknown"))
        lines.append("   - Claim: %s" % code(ex["claim"]))
        if ex.get("why"):
            lines.append("   - Why: %s." % ex["why"])
        run = ex["run"]
        if run is None:
            lines.append("   - No %s run before this claim in the session." % what)
            continue
        verb = {"pass": "passed", "fail": "failed", "unknown": "ended with an unreadable result"}[run["result"]]
        detail = " (%s)" % code(run["detail"], 80) if run["detail"] else ""
        changed = ""
        if ex["changed_count"]:
            shown = ", ".join(code(p) for p in ex["changed"])
            more = " and %d more" % (ex["changed_count"] - len(ex["changed"])) \
                if ex["changed_count"] > len(ex["changed"]) else ""
            changed = "; then %s changed: %s%s" % (_plural(ex["changed_count"], "file"), shown, more)
        lines.append("   - Last %s run: %s %s%s at %s%s." % (what, code(run["command"]), verb, detail, run["time"],
                                                           changed))
    return lines


def render_scan(data) -> str:
    lines = ["**%s**" % data["headline"], ""]
    if data["sessions"]:
        per = ", ".join("%s %d" % (NAMES.get(h, h), n) for h, n in sorted(data["harnesses"].items()))
        sub = " plus %s," % _plural(data["subagent_sessions"], "subagent session") if data["subagent_sessions"] \
            else ""
        counts = ", ".join("%d %s" % (data["by_label"][label], label) for label in LABELS)
        lines += ["Checked %s from the last %s (%s),%s and found %s: %s." % (
            _plural(data["sessions"], "session"), _plural(data["window_days"], "day"), per, sub,
            _plural(data["claims"], "claim"), counts), ""]
    if data["claims"]:
        lines += ["| Label | Claims | What it means |", "|---|---|---|"]
        lines += ["| %s | %d | %s |" % (label, data["by_label"][label], MEANINGS[label]) for label in LABELS]
        lines += ["", "| Harness | Claims | Backed | Stale | Contradicted | Unsupported | Unclear |",
                  "|---|---|---|---|---|---|---|"]
        for h, counts in sorted(data["by_harness"].items()):
            lines.append("| %s | %d | %s |" % (NAMES.get(h, h), sum(counts.values()),
                                                " | ".join(str(counts[label]) for label in LABELS)))
        lines.append("")
    for key, title in (("examples", "Claims that were not backed (worst first)"),
                       ("unclear_examples", "Claims that could not be checked")):
        if data.get(key):
            lines += ["## %s" % title, ""] + _example_lines(data[key]) + [""]
    if data["notes"]:
        lines += ["Notes:"] + ["- " + n for n in data["notes"]] + [""]
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# diff
# ---------------------------------------------------------------------------

SIGNALS = {  # kind: (one, many, what it means)
    "deleted-test-file": ("deleted test file", "deleted test files", "A test file was deleted."),
    "removed-test": ("removed test", "removed tests", "A test was removed and not added back anywhere."),
    "removed-assertions": ("test file with fewer assertions", "test files with fewer assertions",
                           "More assertions were removed than added."),
    "replaced-tests": ("set of tests folded into one parametrized test", "sets of tests folded into parametrized tests",
                       "Several tests were removed and one parametrized test was added; check that it covers each case."),
    "added-skip": ("new skip marker", "new skip markers", "A test is now skipped or expected to fail."),
    "added-focus": ("new focus marker", "new focus markers", "Only the focused tests run; the rest are left out."),
    "ignored-failure": ("test command allowed to fail", "test commands allowed to fail",
                        "A failing test command no longer fails the script or the CI run."),
    "lowered-coverage": ("lowered coverage threshold", "lowered coverage thresholds",
                         "The coverage minimum went down or was removed."),
}


def diff(repo=".", base=None) -> dict:
    found = W.find_signals(repo, base=base)
    counts = {}
    for s in found["signals"]:
        counts[s["kind"]] = counts.get(s["kind"], 0) + 1
    where = "The diff against %s" % T.safe_text(base, 60) if base else "The current diff"
    n = len(found["signals"])
    if n:
        parts = ", ".join(_plural(counts[k], SIGNALS[k][0], SIGNALS[k][1]) for k in SIGNALS if k in counts)
        headline = "%s shows %s of weakened tests: %s." % (where, _plural(n, "sign"), parts)
    else:
        headline = "%s shows no signs of weakened tests (%s checked)." % (
            where, _plural(found["files_changed"], "changed file"))
    return {"headline": headline, "repo": _home_path(found["repo"]), "base": found["base"],
            "files_changed": found["files_changed"], "counts": counts,
            "signals": [{"kind": s["kind"], "file": T.safe_text(s["file"], 160), "line": s["line"],
                         "detail": T.safe_text(s["detail"], 120)} for s in found["signals"]]}


def render_diff(data) -> str:
    lines = ["**%s**" % data["headline"], ""]
    if data["signals"]:
        lines += ["| Signal | File | Line | Detail |", "|---|---|---|---|"]
        for s in data["signals"]:
            lines.append("| %s | %s | %s | %s |" % (SIGNALS[s["kind"]][0], code(s["file"]), s["line"] or "",
                                                   code(s["detail"], 120)))
        lines += ["", "What each signal means:"]
        lines += ["- %s: %s" % (SIGNALS[k][0], SIGNALS[k][2]) for k in SIGNALS if k in data["counts"]]
        lines.append("")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Command line
# ---------------------------------------------------------------------------

def _parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(
        prog="claims.py",
        description="Check whether your coding agent's 'tests pass' claims were backed by a passing run, "
                    "and look for weakened tests in the current diff. Read-only; nothing leaves the machine.")
    sub = ap.add_subparsers(dest="command", metavar="{scan,diff}")
    sc = sub.add_parser("scan", help="label every tests-pass or build-passes claim in recent sessions")
    sc.add_argument("--since", type=_days, default=30, metavar="30d",
                    help="how far back to look: days (30d) or weeks (2w); default 30d")
    sc.add_argument("--harness", default="all", choices=("all",) + T.HARNESSES,
                    help="which harness to read; default all")
    sc.add_argument("--project", metavar="DIR", help="only sessions whose working folder is DIR or inside it")
    sc.add_argument("--examples", type=int, default=10, metavar="N",
                    help="how many unbacked claims to show; default 10")
    df = sub.add_parser("diff", help="look for weakened tests in the working tree's git diff")
    df.add_argument("--repo", default=".", metavar="DIR", help="the git repository to check; default .")
    df.add_argument("--base", metavar="REF",
                    help="compare with the point where the current branch left REF (such as main); "
                         "default: the last commit")
    for p in (sc, df):
        p.add_argument("--json", action="store_true", help="print machine-readable JSON")
        p.add_argument("--out", metavar="PATH", help="write the report to PATH instead of the screen")
        p.add_argument("--fail", action="store_true", help="exit 1 when a finding is reported")
    return ap


def main(argv=None) -> int:
    ap = _parser()
    args = ap.parse_args(argv)
    if args.command == "scan":
        data = scan(days=args.since, harness=args.harness, project=args.project, examples=args.examples)
        _write(json.dumps(data, indent=2) + "\n" if args.json else render_scan(data), args.out)
        return 1 if (args.fail and data["not_backed"]) else 0
    if args.command == "diff":
        try:
            data = diff(args.repo, args.base)
        except W.GitError as exc:
            print("claims.py diff: %s" % T.safe_text(str(exc), 300), file=sys.stderr)
            return 2
        _write(json.dumps(data, indent=2) + "\n" if args.json else render_diff(data), args.out)
        return 1 if (args.fail and data["signals"]) else 0
    ap.print_help()
    return 2


if __name__ == "__main__":
    sys.exit(main())
