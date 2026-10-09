#!/usr/bin/env python3
"""Show what the runaway guard has counted for a session, and start the count over.

With no flags it reports the session the guard checked most recently: dollars
spent against the cap, how often each trip wire stepped in, and the latest
trips. --session picks another session (the full id or its first characters),
--list shows every session the guard knows, and --reset starts the counts of
one session over: spend counts from $0 again, and the loop and failure counts
clear. The session total stays in the record.

Exit codes: 0 done, 2 bad arguments or no session matching --session.

Python 3.9+, standard library only. Reads and writes only the guard's own
state folder: ${XDG_STATE_HOME:-~/.local/state}/runaway-guard.
"""

from __future__ import annotations

import argparse
import json
import os
import sys

import guard
from safe import code, safe_text

safe = safe_text
WIRES = ("loop", "failures", "spend")


# ---------------------------------------------------------------------------
# Loading
# ---------------------------------------------------------------------------

def sessions(folder) -> list:
    """(path, state) for every session file, most recently checked first."""
    found = []
    for e in guard._scandir(folder):
        if e.name.endswith(".json") and e.name != guard.CONFIG_NAME:
            state = guard.load_state(e.path)
            if state is not None:
                found.append((e.path, state))
    found.sort(key=lambda item: (str(item[1].get("updated") or ""), os.path.getmtime(item[0])), reverse=True)
    return found


def pick(found, wanted, parser):
    if not found:
        return None
    if not wanted:
        return found[0]
    exact = [f for f in found if f[1].get("session_id") == wanted
             or os.path.basename(f[0]) == wanted + ".json"]
    if exact:
        return exact[0]
    prefixed = [f for f in found if str(f[1].get("session_id") or "").startswith(wanted)]
    if len(prefixed) == 1:
        return prefixed[0]
    if prefixed:
        parser.error("%d sessions start with %s; give more of the id" % (len(prefixed), safe(wanted, 80)))
    parser.error("no session matches %s; run with --list to see the sessions the guard knows"
                 % safe(wanted, 80))


def errors_logged(folder) -> int:
    try:
        with open(os.path.join(folder, "errors.log"), encoding="utf-8", errors="replace") as fh:
            return sum(1 for line in fh if line.strip())
    except OSError:
        return 0


# ---------------------------------------------------------------------------
# Summaries
# ---------------------------------------------------------------------------

def _number(value, default=0.0) -> float:
    return value if isinstance(value, (int, float)) and not isinstance(value, bool) else default


def summary(path, state, folder) -> dict:
    counts = state.get("counts") if isinstance(state.get("counts"), dict) else {}
    limits = dict(guard.DEFAULTS)
    if isinstance(state.get("limits"), dict):
        limits.update({k: v for k, v in state["limits"].items() if k in guard.DEFAULTS})
    total = _number(state.get("cost_usd"))
    streaks = state.get("streaks") if isinstance(state.get("streaks"), dict) else {}
    trips = [{"wire": safe(t.get("wire"), 20), "at": safe(t.get("at"), 30), "tool": safe(t.get("tool"), 80),
              "detail": safe(t.get("detail"), 80)}
             for t in state.get("trips") or [] if isinstance(t, dict)]
    return {
        "session": safe(state.get("session_id"), 140),
        "harness": safe(state.get("harness"), 20),
        "created": safe(state.get("created"), 30),
        "updated": safe(state.get("updated"), 30),
        "spent_usd": max(0.0, total - _number(state.get("baseline_usd"))),
        "total_usd": total,
        "cap_usd": limits["spend_cap_usd"],
        "limits": limits,
        "trips": {w: int(_number(counts.get(w), 0)) for w in WIRES + ("warning",)},
        "recent_trips": trips[-10:],
        "failure_streak": int(max([_number(v, 0) for v in streaks.values()] or [0])),
        "transcripts": len(state.get("files") or {}),
        "estimated_usd": _number(state.get("estimated_usd")),
        "estimated_tokens": int(_number(state.get("estimated_tokens"), 0)),
        "estimated_models": [safe(m, 60) for m in state.get("estimated_models") or []],
        "resets": int(_number(state.get("resets"), 0)),
        "errors_logged": errors_logged(folder),
        "state_path": path,
    }


def when(iso) -> str:
    """2026-09-28T10:02:05Z as 2026-09-28 10:02 UTC."""
    return iso[:10] + " " + iso[11:16] + " UTC" if len(iso) >= 16 and iso[10:11] == "T" else iso


def _short(session) -> str:
    return session[:8] if len(session) > 12 else session


def _stepped_in(s) -> int:
    return sum(s["trips"][w] for w in WIRES)


def headline(s, markdown=True) -> str:
    """The one-line summary; the session id sits in inline code for the markdown report, plain for JSON."""
    cap = s["cap_usd"]
    money = ("has spent $%.2f of its $%.2f cap" % (s["spent_usd"], cap) if cap
             else "has spent $%.2f (no spend cap is set)" % s["spent_usd"])
    n = _stepped_in(s)
    fired = "no trip wire has fired" if not n else "the guard stepped in %d time%s" % (n, "" if n == 1 else "s")
    session = _short(s["session"])
    return "Session %s %s; %s." % (code(session) if markdown else session, money, fired)


def report(s, reset_note="") -> str:
    lim = s["limits"]
    cap = s["cap_usd"]
    rows = [
        ("Spend", "$%.2f per session, warning at %d%%" % (cap, round(100 * lim["warn_at"])) if cap else "off",
         "$%.2f" % s["spent_usd"] + (" (%d%%)" % round(100 * s["spent_usd"] / cap) if cap else ""),
         "%d call%s blocked" % (s["trips"]["spend"], "" if s["trips"]["spend"] == 1 else "s")),
        ("Loop", "the same call %d times with nothing changed" % lim["loop_repeats"] if lim["loop_repeats"]
         else "off", "", "%d call%s blocked" % (s["trips"]["loop"], "" if s["trips"]["loop"] == 1 else "s")),
        ("Failures", "%d failed calls in a row" % lim["failure_streak"] if lim["failure_streak"] else "off",
         "%d in a row" % s["failure_streak"],
         "stepped in %d time%s" % (s["trips"]["failures"], "" if s["trips"]["failures"] == 1 else "s")),
    ]
    out = ["**%s**" % headline(s), ""]
    if reset_note:
        out += [reset_note, ""]
    out += ["| Trip wire | Limit | Now | Stepped in |", "|---|---|---|---|"]
    out += ["| %s |" % " | ".join(row) for row in rows]
    if s["recent_trips"]:
        out += ["", "Latest events:"]
        out += ["- %s, %s: %s, %s" % (when(t["at"]), t["wire"], code(t["tool"]) if t["tool"] else "a tool call",
                                      t["detail"])
                for t in s["recent_trips"][-5:]]
    out.append("")
    if s["trips"]["warning"]:
        out.append("Spend warnings shown: %d." % s["trips"]["warning"])
    if s["resets"]:
        out.append("Counts started over %d time%s; the session total so far is $%.2f."
                   % (s["resets"], "" if s["resets"] == 1 else "s", s["total_usd"]))
    if s["estimated_tokens"]:
        out.append("$%.2f of the total is an estimate: no price is known for %s, so its %s tokens are priced "
                   "like the most expensive model of the same family." % (
                       s["estimated_usd"], ", ".join(code(m) for m in s["estimated_models"]) or "a model",
                       "{:,}".format(s["estimated_tokens"])))
    out.append("Transcripts read: %d. Last check: %s. Limits as of that check." % (s["transcripts"], when(s["updated"])))
    if s["errors_logged"]:
        out.append("The guard logged %d internal error%s and let those calls through; see errors.log in the "
                   "state folder." % (s["errors_logged"], "" if s["errors_logged"] == 1 else "s"))
    return "\n".join(out) + "\n"


def list_report(summaries) -> str:
    out = ["**Runaway guard knows %d session%s.**" % (len(summaries), "" if len(summaries) == 1 else "s"), "",
           "| Session | Last check | Spent | Cap | Stepped in |", "|---|---|---|---|---|"]
    for s in summaries:
        out.append("| %s | %s | $%.2f | %s | %d |" % (code(s["session"]), when(s["updated"]), s["spent_usd"],
                                                    "$%.2f" % s["cap_usd"] if s["cap_usd"] else "off",
                                                    _stepped_in(s)))
    return "\n".join(out) + "\n"


# ---------------------------------------------------------------------------
# Reset
# ---------------------------------------------------------------------------

def reset(path, state) -> dict:
    """Start the counts over under the session lock; returns the state as saved."""
    lock = guard._acquire(path[:-len(".json")] + ".lock")
    if lock is None:
        raise RuntimeError("the session is busy; try again in a moment")
    try:
        state = guard.load_state(path) or state
        state["baseline_usd"] = _number(state.get("cost_usd"))
        state["agents"], state["streaks"], state["trips"] = {}, {}, []
        state["counts"] = {w: 0 for w in WIRES + ("warning",)}
        state["warned_cap"] = None
        state["resets"] = int(_number(state.get("resets"), 0)) + 1
        state["updated"] = guard._now_iso()
        guard.save_state(path, state)
        return state
    finally:
        guard._release(lock)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        description="Show the runaway guard's spend and trip counts for a session, or start them over.")
    parser.add_argument("--session", metavar="ID", help="the session id, or its first characters "
                                                        "(default: the session checked most recently)")
    parser.add_argument("--list", action="store_true", help="list every session the guard knows")
    parser.add_argument("--reset", action="store_true",
                        help="start the counts over: spend from $0, loop and failure counts cleared")
    parser.add_argument("--json", action="store_true", help="print machine-readable JSON")
    parser.add_argument("--out", metavar="PATH", help="write the report to this file instead of printing it")
    args = parser.parse_args(argv)
    if args.list and args.reset:
        parser.error("--reset works on one session; leave out --list")
    folder = guard.state_dir()
    found = sessions(folder)

    if args.list:
        summaries = [summary(p, st, folder) for p, st in found]
        text = (json.dumps({"state_folder": folder, "sessions": summaries}, indent=2) + "\n" if args.json
                else list_report(summaries))
    else:
        chosen = pick(found, args.session, parser)
        if chosen is None:
            if args.reset:
                parser.error("there is no session to reset yet")
            head = "Runaway guard has not checked any session yet."
            text = (json.dumps({"headline": head, "state_folder": folder, "session": None}, indent=2) + "\n"
                    if args.json else
                    "**%s**\n\nIt keeps its counts in %s. If the hook is installed, the first tool call of a "
                    "new session creates them.\n" % (head, safe(folder, 300)))
        else:
            path, state = chosen
            note = ""
            if args.reset:
                try:
                    state = reset(path, state)
                except RuntimeError as exc:
                    print("error: %s" % exc, file=sys.stderr)
                    return 2
                note = ("Reset done: spend counts from $0.00 again, and the loop and failure counts are "
                        "cleared. The session total so far, $%.2f, stays in the record."
                        % _number(state.get("cost_usd")))
            s = summary(path, state, folder)
            if args.json:
                text = json.dumps(dict(s, headline=headline(s, markdown=False)), indent=2) + "\n"
            else:
                text = report(s, note)
    if args.out:
        try:
            with open(args.out, "w", encoding="utf-8") as fh:
                fh.write(text)
        except OSError as exc:
            print("error: cannot write the report to %s: %s" % (safe(args.out, 300), exc.strerror or exc),
                  file=sys.stderr)
            return 2
        print("Report written to %s" % safe(args.out, 300))
    else:
        sys.stdout.write(text)
    return 0


if __name__ == "__main__":
    sys.exit(main())
