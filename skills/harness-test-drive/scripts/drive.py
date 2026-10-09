"""Run coding agents on mined tasks, score them with the repository's tests,
and report the scoreboard.

    python3 drive.py estimate --tasks tasks.json [--harness claude-code,codex,gemini-cli]
    python3 drive.py run --tasks tasks.json --harness claude-code,codex --max-usd 5
        [--timeout 900] [--keep] [--allow-unpriced gemini-cli] [--model claude-code=<id>]
    python3 drive.py report [--results results.jsonl] [--json] [--out <path>]

Each run gets a fresh copy of the repository at the commit before the fix (a new
git repository with one commit, so the fix is not in its history), the commit
message as the prompt, and the harness's most restrictive settings that still
allow edits and the test command. Afterwards the agent's diff, minus test
files, is applied to a clean copy with the fix commit's tests, and the test
command decides: passed or failed. A harness that exits with an error and
changes nothing "could not run"; that run is not scored.

Money: `run` needs --max-usd. It stops starting runs once the counted spend
reaches the cap, passes the budget left to harnesses that take a per-run cap
(Claude Code --max-budget-usd), and appends every run to results.jsonl, so a
rerun resumes and counts earlier spend. SIGINT, SIGTERM, and SIGHUP stop the
agent, record the run with its spend, and delete the copy. After a SIGKILL,
inflight.json lets the next run count the lost run. Exit codes: 0 success,
2 usage or input error, 130 stopped. Python 3.9+, standard library only.
"""

from __future__ import annotations

import argparse
import contextlib
import datetime
import json
import math
import os
import sys

import harnesses
from common import (apply_patch, changed_files, classify, duration, install_stop_handlers, make_workspace,
                    remove_workspace, reset_workspace, restore_handlers, run_command, run_tests, score_patch,
                    workspace_head, write_files)
from harnesses import HIGH_RUN, LOW_RUN, fatal_hint
from pricing import PRICES_CHECKED
from safe import code, safe_text

DEFAULT_TASKS = os.path.join(".harness-test-drive", "tasks.json")
DEFAULT_RESULTS = os.path.join(".harness-test-drive", "results.jsonl")
SCORED = ("passed", "failed")

PROMPT = """Make the change described below in this repository.

Change request:
{task}

When you finish, the repository's own tests will check your work. Test files you edit are put back first, so \
change the code, not the tests. You can run the tests with this command: {test_cmd}
Do not commit. No one will answer questions, so make reasonable choices and complete the change.
"""


class UsageError(Exception):
    """Bad input: the message says what to change."""


def _now() -> str:
    return datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _stderr(line) -> None:
    print(line, file=sys.stderr, flush=True)


def build_prompt(task, test_cmd) -> str:
    text = task["subject"] + ("\n\n" + task["body"] if task.get("body") else "")
    return PROMPT.format(task=text, test_cmd=test_cmd)


def load_results(path) -> list:
    """Every run recorded in results.jsonl; a missing file is an empty list."""
    records = []
    try:
        with open(path, encoding="utf-8") as fh:
            for line in fh:
                try:
                    rec = json.loads(line)
                except ValueError:
                    continue  # a line cut short when a run was interrupted
                if isinstance(rec, dict) and rec.get("task") and rec.get("harness"):
                    records.append(rec)
    except OSError:
        pass
    return records


def _append(path, rec) -> None:
    with open(path, "a", encoding="utf-8") as fh:
        fh.write(json.dumps(rec) + "\n")


def _read(path) -> str:
    try:
        with open(path, encoding="utf-8", errors="replace") as fh:
            return fh.read()
    except OSError:
        return ""


def _record(task, harness, label, version="") -> dict:
    return {"schema": "harness-test-drive/result/1", "task": task.get("id", ""), "commit": task.get("commit", ""),
            "subject": safe_text(task.get("subject") or "", 100), "gold_lines": task.get("gold_lines"),
            "harness": harness, "label": label, "version": version, "started": _now(),
            "status": "error", "timed_out": False, "exit_code": None, "error": "", "hint": "",
            "agent_seconds": None, "test_seconds": None, "cost_usd": None, "cost_source": None, "charged_usd": 0.0,
            "tokens": None, "model": "", "turns": None, "denials": 0, "test_runs": None, "test_failures": None,
            "lines_changed": None, "files_touched": None, "log": "", "diff": ""}


def _charge(rec, adapter, out_path, err_path, test_cmd) -> None:
    """Read the harness output into `rec`, and set what the spend cap counts:
    the measured cost; else, for a priced harness, a high estimate when the run
    was cut off at the time limit (one per 900 seconds) or its output shows model
    work; else nothing (an unpriced harness, or one that never reached a model)."""
    stdout, stderr = _read(out_path), _read(err_path)
    try:
        parsed = adapter.parse_output(stdout, test_cmd=test_cmd)
    except Exception:  # unexpected output must not lose the run or its spend
        parsed = {"cost_usd": None, "cost_source": None, "tokens": None, "model": "", "turns": None,
                  "error": "output could not be read", "denials": 0, "worked": bool(stdout.strip()),
                  "test_runs": None, "test_failures": None}
    lines = [ln for ln in stderr.splitlines() if ln.strip()]
    rec.update(cost_usd=parsed["cost_usd"], tokens=parsed["tokens"], model=safe_text(parsed["model"], 60),
               turns=parsed["turns"], denials=parsed["denials"], test_runs=parsed.get("test_runs"),
               test_failures=parsed.get("test_failures"),
               error=safe_text(rec["error"] or parsed["error"] or (lines[-1] if rec["exit_code"] and lines else "")))
    if parsed["cost_usd"] is not None:
        rec.update(cost_source=parsed["cost_source"], charged_usd=parsed["cost_usd"])
    elif not adapter.priced():
        rec.update(cost_source="unpriced", charged_usd=0.0)
    elif rec["timed_out"]:
        rec.update(cost_source="unmeasured",
                   charged_usd=adapter.fallback_cost() * max(1.0, (rec["agent_seconds"] or 0) / 900.0))
    elif parsed.get("worked"):
        rec.update(cost_source="unmeasured", charged_usd=adapter.fallback_cost())
    else:
        rec.update(cost_source=None, charged_usd=0.0)


def run_one(doc, task, adapter, budget_left, timeout, keep, work_root, logs_dir, version, inflight) -> dict:
    """One agent on one task: copy, agent, diff, then the score in a clean copy.
    Ctrl-C (or SIGTERM, SIGHUP) returns the record with status "interrupted"."""
    rec = _record(task, adapter.name, adapter.label, version)
    name = "%s-%s" % (task["id"], adapter.name)
    out_path, err_path = os.path.join(logs_dir, name + ".out"), os.path.join(logs_dir, name + ".err")
    setup_cmd, test_timeout = doc.get("setup_cmd"), doc.get("test_timeout", 600)
    ws, started, charged = None, False, False
    try:
        ws = make_workspace(doc["repo"], task["parent"], root=work_root)
        base = workspace_head(ws)
        if setup_cmd and run_command(setup_cmd, ws, test_timeout)["exit"] != 0:
            rec["error"] = "the setup command failed"
            return rec
        argv = adapter.build_command(build_prompt(task, doc["test_cmd"]), ws,
                                     {"test_cmd": doc["test_cmd"], "max_usd": budget_left})
        with open(inflight, "w", encoding="utf-8") as fh:  # what a SIGKILL would lose
            json.dump({"task": task["id"], "harness": adapter.name, "label": adapter.label, "started": rec["started"],
                       "charged_usd": adapter.fallback_cost() if adapter.priced() else 0.0, "workspace": ws}, fh)
        rec["log"], started = out_path, True
        agent = run_command(argv, ws, timeout, out_path, err_path)
        rec.update(timed_out=agent["timed_out"], exit_code=agent["exit"], agent_seconds=agent["seconds"])
        _charge(rec, adapter, out_path, err_path, doc["test_cmd"])
        charged = True
        rec["diff"] = os.path.join(logs_dir, name + ".diff")
        rec["lines_changed"], touched = changed_files(ws, base, rec["diff"])
        rec["files_touched"] = len(touched)
        if not rec["timed_out"] and rec["exit_code"] != 0 and not touched:  # could not run: nothing to score
            rec["error"] = rec["error"] or "exited with code %s and changed nothing" % rec["exit_code"]
            rec["hint"] = fatal_hint(adapter.label, getattr(adapter, "binary", adapter.name), rec["error"])
            return rec
        # Score exactly the saved diff, minus test files, in a clean copy with the fix commit's tests.
        patch = score_patch(ws, base, [p for p in touched if classify(p) != "test"])
        reset_workspace(ws, base)
        apply_patch(ws, patch)
        write_files(doc["repo"], task["commit"], task["hidden_tests"], ws)
        if setup_cmd and run_command(setup_cmd, ws, test_timeout)["exit"] != 0:
            rec.update(status="failed", error="the setup command failed after the agent's changes")
            return rec
        tests = run_tests(doc["test_cmd"], ws, test_timeout, os.path.join(logs_dir, name + ".tests"))
        rec.update(status="passed" if tests["exit"] == 0 else "failed", test_seconds=tests["seconds"])
    except KeyboardInterrupt:
        rec.update(status="interrupted", error="stopped by the user")
    except (OSError, RuntimeError, ValueError) as err:
        rec["error"] = safe_text(err)
        rec["hint"] = fatal_hint(adapter.label, getattr(adapter, "binary", adapter.name), rec["error"])
    finally:
        try:
            if started and not charged:
                _charge(rec, adapter, out_path, err_path, doc["test_cmd"])
        finally:
            if ws and keep:
                rec["workspace"] = ws
            elif ws:
                remove_workspace(ws)
    return rec


def _alive(pid) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


@contextlib.contextmanager
def _lock(results_path):
    """One run per results file: <results>.lock holds the pid of the run."""
    path = results_path + ".lock"
    for _ in range(2):
        try:
            fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644)
        except FileExistsError:
            try:
                pid = int(_read(path).strip() or 0)
            except ValueError:
                pid = 0
            if pid > 0 and _alive(pid):
                raise UsageError("Another drive.py run (process %d) is using %s. Let it finish, or stop it first."
                                 % (pid, code(results_path, 300)))
            with contextlib.suppress(FileNotFoundError):
                os.unlink(path)  # left by a run that is gone
            continue
        with os.fdopen(fd, "w") as fh:
            fh.write(str(os.getpid()))
        break
    else:
        raise UsageError("Could not lock %s." % code(path, 300))
    try:
        yield
    finally:
        with contextlib.suppress(OSError):
            os.unlink(path)


def _recover(inflight, results_path, doc) -> None:
    """Turn a run lost to SIGKILL (inflight.json left behind) into an
    'interrupted' record that counts its estimated spend, and delete its copy."""
    try:
        with open(inflight, encoding="utf-8") as fh:
            left = json.load(fh)
    except FileNotFoundError:
        return
    except (OSError, ValueError):
        left = None
    if isinstance(left, dict) and left.get("task") and left.get("harness"):
        recorded = any(r["task"] == left["task"] and r["harness"] == left["harness"]
                       and r.get("started") == left.get("started") for r in load_results(results_path))
        if not recorded:
            task = next((t for t in doc.get("tasks", []) if t.get("id") == left["task"]), {"id": left["task"]})
            rec = _record(task, str(left["harness"]), safe_text(left.get("label") or left["harness"], 40))
            try:
                charge = max(0.0, float(left.get("charged_usd") or 0.0))
            except (TypeError, ValueError):
                charge = 0.0
            rec.update(status="interrupted", started=safe_text(left.get("started") or "", 40) or rec["started"],
                       error="the run was killed before it finished; its cost is an estimate",
                       cost_source="unmeasured", charged_usd=charge)
            _append(results_path, rec)
        ws = left.get("workspace")
        if (isinstance(ws, str) and os.path.basename(ws).startswith("harness-test-drive-")
                and os.path.isdir(os.path.join(ws, ".git"))):
            remove_workspace(ws)
    with contextlib.suppress(FileNotFoundError):
        os.unlink(inflight)


def _done(rec) -> bool:
    """A recorded run that needs no retry. An interrupted run, or an error that
    cost nothing (a login to renew, a program to install), runs again."""
    return rec.get("status") != "interrupted" and not (rec.get("status") == "error" and not rec.get("charged_usd"))


def run(doc, results_path, adapters, max_usd, timeout=900, keep=False, allow_unpriced=(), work_root=None,
        progress=_stderr) -> dict:
    """Run every (task, harness) pair not yet done in results_path, until the spend cap."""
    tasks = [t for t in doc.get("tasks", []) if t.get("validated")]
    if not tasks:
        raise UsageError("The tasks file has no validated tasks. Run mine_tasks.py with --validate first.")
    if not (isinstance(max_usd, (int, float)) and math.isfinite(max_usd) and max_usd > 0):
        raise UsageError("--max-usd must be a dollar amount above zero.")
    if timeout < 1:
        raise UsageError("--timeout must be at least 1 second.")
    if not os.path.isdir(doc.get("repo") or ""):
        raise UsageError("The repository in the tasks file is gone: %s" % code(doc.get("repo") or "", 300))
    installed = [a for a in adapters if a.available()]
    allowed = set(allow_unpriced or ())
    for a in installed:
        if not a.priced() and not (a.can_run_unpriced and a.name in allowed):
            raise UsageError("%s cannot be priced: %s. %s." % (a.label, a.unpriced_reason(), a.unpriced_fix()))
    summary = {"results_file": safe_text(results_path, 300), "max_usd": max_usd, "runs": 0, "already_done": 0,
               "stopped_at_cap": False, "not_started": 0, "spent_usd": 0.0, "skipped": {},
               "not_installed": [a.name for a in adapters if not a.available()]}
    folder = os.path.dirname(os.path.abspath(results_path))
    os.makedirs(folder, exist_ok=True)
    inflight = os.path.join(folder, "inflight.json")
    with _lock(results_path):
        _recover(inflight, results_path, doc)
        earlier = load_results(results_path)
        done = {(r["task"], r["harness"]) for r in earlier if _done(r)}
        spent = sum(r.get("charged_usd") or 0.0 for r in earlier)  # every run costs, retried ones too
        pending = [(t, a) for t in tasks for a in installed if (t["id"], a.name) not in done]
        summary["already_done"] = len(tasks) * len(installed) - len(pending)
        logs_dir = os.path.join(folder, "logs")
        os.makedirs(logs_dir, exist_ok=True)
        versions = {a.name: a.version() for a in installed} if pending else {}
        for i, (task, adapter) in enumerate(pending):
            if adapter.name in summary["skipped"]:
                continue
            if spent >= max_usd:
                summary.update(stopped_at_cap=True, not_started=sum(
                    1 for _, a in pending[i:] if a.name not in summary["skipped"]))
                break
            progress("[%d/%d] %s on %s: %s" % (i + 1, len(pending), adapter.label, task["id"][:7],
                                              code(task["subject"], 60)))
            rec = run_one(doc, task, adapter, max_usd - spent, timeout, keep, work_root, logs_dir,
                          versions[adapter.name], inflight)
            _append(results_path, rec)
            with contextlib.suppress(FileNotFoundError):
                os.unlink(inflight)
            spent += rec["charged_usd"]
            summary["runs"] += 1
            if rec["status"] == "interrupted":
                raise KeyboardInterrupt
            if rec["status"] == "error":
                progress("        could not run (%s), $%.2f counted" % (code(rec["error"], 120), rec["charged_usd"]))
                if rec["hint"]:
                    summary["skipped"][adapter.name] = rec["hint"]
                    progress("        Fix: %s. The other %s runs are skipped." % (rec["hint"], adapter.label))
                continue
            progress("        %s in %s, $%.2f counted, $%.2f of $%.2f spent" % (
                rec["status"], duration(rec["agent_seconds"]), rec["charged_usd"], spent, max_usd))
    summary["spent_usd"] = round(spent, 4)
    return summary


# --- Scoreboard ---------------------------------------------------------------------------

def _median(values):
    vals = sorted(v for v in values if isinstance(v, (int, float)))
    if not vals:
        return None
    mid = len(vals) // 2
    return vals[mid] if len(vals) % 2 else (vals[mid - 1] + vals[mid]) / 2


def _and(items) -> str:
    items = list(items)
    if len(items) < 3:
        return " and ".join(items)
    return ", ".join(items[:-1]) + ", and " + items[-1]


def _money(value) -> str:
    return "$%.2f" % value


def _plural(n, word) -> str:
    return "%d %s%s" % (n, word, "" if n == 1 else "s")


def _shown(x) -> str:
    """A harness's name in markdown: the label from the harness table, or, for a
    harness the table does not know, the recorded label in inline code."""
    adapter = harnesses.ADAPTERS.get(x["harness"])
    return adapter.label if adapter else code(x["label"], 40)


def _fix(r) -> str:
    """A run's fix in markdown: rebuilt from the harness table and the run's error,
    or else the recorded hint in inline code."""
    adapter = harnesses.ADAPTERS.get(r["harness"])
    rebuilt = fatal_hint(adapter.label, adapter.binary, r.get("error") or "") if adapter else ""
    return rebuilt or code(r["hint"], 80)


def summarize(records) -> dict:
    """The scoreboard. Harnesses are compared only on the tasks every one of them
    finished; a harness with no finished run is listed as could not run. The
    headline and notes are markdown sentences, so the error text in them sits in
    inline code, and harness names and fixes come from the harness table (or sit
    in inline code); the other text fields are plain, with secrets masked."""
    rep = {"headline": "No runs recorded yet.", "tasks_compared": 0, "harnesses": [], "tasks": [],
           "spent_usd": 0.0, "runs": 0, "notes": []}
    latest, names, labels, order, info = {}, [], {}, [], {}
    for r in records:
        latest[(r["task"], r["harness"])] = r  # a rerun of the same pair replaces the older record
        if r["harness"] not in labels:
            names.append(r["harness"])
            adapter = harnesses.ADAPTERS.get(r["harness"])
            labels[r["harness"]] = adapter.label if adapter else safe_text(r.get("label") or r["harness"], 40)
        if r["task"] not in info:
            order.append(r["task"])
            info[r["task"]] = r
    if not records:
        return rep
    finished = {key: r for key, r in latest.items() if r.get("status") in SCORED}
    ran = [h for h in names if any(n == h for _, n in finished)]
    common = [t for t in order if ran and all((t, h) in finished for h in ran)]
    for h in names:
        runs = [finished[(t, h)] for t in common] if h in ran else []
        passed = sum(r["status"] == "passed" for r in runs)
        costs = [r.get("cost_usd") for r in runs]
        unmeasured = sum(c is None for c in costs)
        total = round(sum(c for c in costs if c is not None), 4)
        minutes = _median(r.get("agent_seconds") for r in runs)
        errors = [r for (t, n), r in latest.items() if n == h and (t, n) not in finished]
        rep["harnesses"].append({
            "harness": h, "label": labels[h],
            "versions": sorted({safe_text(r.get("version") or "", 60)
                                for (t, n), r in latest.items() if n == h} - {""}),
            "runs": len(runs), "passed": passed, "pass_rate": round(passed / len(runs), 3) if runs else None,
            "median_minutes": round(minutes / 60, 1) if minutes is not None else None,
            "cost_per_pass": round(total / passed, 4) if passed and not unmeasured else None,
            "total_cost": total if runs and unmeasured < len(runs) else None, "unmeasured_runs": unmeasured,
            "median_lines_changed": _median(r.get("lines_changed") for r in runs),
            "timeouts": sum(bool(r.get("timed_out")) for r in runs), "errors": len(errors),
            "could_not_run": None if h in ran else (safe_text(errors[0].get("error") or "", 100)
                                                    if errors else "no result")})
    compared = sorted([x for x in rep["harnesses"] if x["could_not_run"] is None],
                      key=lambda x: (-x["passed"], x["cost_per_pass"] is None, x["cost_per_pass"] or 0))
    parts = []
    for x in compared:
        part = "%s passed %d" % (_shown(x), x["passed"])
        if x["passed"] and x["cost_per_pass"] is not None:
            part += " at %s each" % _money(x["cost_per_pass"])
        elif x["passed"]:
            part += " (cost not measured)"
        parts.append(part)
    cannot = ["%s could not run (%s)" % (_shown(x), code(x["could_not_run"], 100))
              for x in rep["harnesses"] if x["could_not_run"]]
    if common:
        rep["headline"] = "On %s from your git history, %s%s." % (
            _plural(len(common), "task"), _and(parts), "; " + "; ".join(cannot) if cannot else "")
    elif cannot and not compared:
        rep["headline"] = "No harness could run: %s." % "; ".join(cannot)
    else:
        rep["headline"] = "No task has a result from every harness yet."
    rep["tasks_compared"] = len(common)
    rep["runs"] = len(latest)
    rep["spent_usd"] = round(sum(r.get("charged_usd") or 0.0 for r in records), 4)  # every run, reruns too
    for t in order:
        rep["tasks"].append({"task": t, "commit": info[t].get("commit", ""),
                             "subject": safe_text(info[t].get("subject") or "", 100),
                             "gold_lines": info[t].get("gold_lines"),
                             "results": {h: {"status": latest[(t, h)].get("status"),
                                             "timed_out": bool(latest[(t, h)].get("timed_out")),
                                             "minutes": round((latest[(t, h)].get("agent_seconds") or 0) / 60, 1),
                                             "cost_usd": latest[(t, h)].get("cost_usd"),
                                             "error": safe_text(latest[(t, h)].get("error") or "", 120)}
                                         for h in names if (t, h) in latest}})
    _notes(rep, latest, finished, order, common)
    return rep


def _notes(rep, latest, finished, order, common) -> None:
    notes = rep["notes"]
    left_out = len(order) - len(common)
    if left_out and common:
        notes.append("%s left out of the comparison because not every harness finished %s; the per-task "
                     "table shows them." % (_plural(left_out, "task") + (" is" if left_out == 1 else " are"),
                                            "it" if left_out == 1 else "them"))
    timeouts = sum(bool(r.get("timed_out")) for r in latest.values())
    if timeouts:
        notes.append("%s hit the time limit and %s scored on the changes the agent had made."
                     % (_plural(timeouts, "run"), "was" if timeouts == 1 else "were"))
    for x in rep["harnesses"]:
        mine = [r for (t, n), r in latest.items() if n == x["harness"] and (t, n) not in finished]
        stopped = [r for r in mine if r.get("status") == "interrupted"]
        failed = [r for r in mine if r.get("status") != "interrupted"]
        hinted = [r for r in failed if r.get("hint")]
        if stopped:
            notes.append("%s: %s stopped before %s finished; the next run starts %s again." % (
                _shown(x), _plural(len(stopped), "run") + (" was" if len(stopped) == 1 else " were"),
                "it" if len(stopped) == 1 else "they", "it" if len(stopped) == 1 else "them"))
        if failed:
            notes.append("%s: %s could not run, for example: %s%s" % (
                _shown(x), _plural(len(failed), "run"),
                code(failed[0]["error"], 120) if failed[0].get("error") else "unknown",
                ". Fix: %s." % _fix(hinted[0]) if hinted else "."))
        counted = [r for (t, n), r in latest.items() if n == x["harness"] and isinstance(r.get("test_runs"), int)]
        test_runs = sum(r["test_runs"] for r in counted)
        test_failures = sum(r.get("test_failures") or 0 for r in counted)
        if test_failures:
            notes.append("%s's own runs of the test command failed %d of %d times. Its sandbox blocks the network "
                         "and writes outside the copy, so a test command that needs either fails there; a setup "
                         "that installs into the copy and a test command that runs offline let it check its work."
                         % (_shown(x), test_failures, test_runs))
    unmeasured = [r for r in latest.values() if r.get("cost_source") in ("unmeasured", "unpriced")]
    if unmeasured:
        notes.append("Cost not measured for %s. The spend cap counted a high estimate for priced harnesses "
                     "and $0 for unpriced ones." % _plural(len(unmeasured), "run"))
    if any(r.get("cost_source") == "partial" for r in latest.values()):
        notes.append("Some runs were stopped before the harness reported its cost; their cost counts only the "
                     "replies sent before the stop.")
    notes.append("Dollar figures are API list prices: what the harness reported, or its tokens times the price table "
                 "checked %s. On a subscription plan, runs count against the plan's limits instead." % PRICES_CHECKED)


def _cell(res) -> str:
    if res is None:
        return "not run"
    if res["status"] == "error":
        return "could not run"
    if res["status"] not in SCORED:  # "interrupted" is the skill's own; any other status is recorded text
        return res["status"] if res["status"] == "interrupted" else code(res["status"], 40)
    cell = res["status"] + (" (time limit)" if res["timed_out"] else "") + ", %.1f min" % res["minutes"]
    return cell + (", " + _money(res["cost_usd"]) if res["cost_usd"] is not None else "")


def render_report(rep) -> str:
    lines = ["**%s**" % rep["headline"], ""]
    if not rep["harnesses"]:
        return "\n".join(lines)
    if rep["tasks_compared"]:
        lines += ["| Harness | Version | Passed | Pass rate | Median minutes | Cost per pass | Total cost | "
                  "Median lines changed |", "|---|---|---|---|---|---|---|---|"]
        for x in rep["harnesses"]:
            version = ", ".join(code(v, 60) for v in x["versions"]) or "unknown"
            if x["could_not_run"]:
                lines.append("| %s | %s | could not run | n/a | n/a | n/a | n/a | n/a |" % (_shown(x), version))
                continue
            if x["total_cost"] is None:
                total = "not measured"
            else:
                total = _money(x["total_cost"]) + (" + %d not measured" % x["unmeasured_runs"]
                                                     if x["unmeasured_runs"] else "")
            lines.append("| %s | %s | %d of %d | %d%% | %s | %s | %s | %s |" % (
                _shown(x), version, x["passed"], x["runs"], round(100 * x["pass_rate"]),
                "%.1f" % x["median_minutes"] if x["median_minutes"] is not None else "n/a",
                _money(x["cost_per_pass"]) if x["cost_per_pass"] is not None else "n/a", total,
                "%g" % x["median_lines_changed"] if x["median_lines_changed"] is not None else "n/a"))
        lines.append("")
    labels = [(x["harness"], _shown(x)) for x in rep["harnesses"]]
    lines += ["Per task (the size of the original fix in changed lines, then each harness):", "",
              "| Task | Change | Original fix lines | " + " | ".join(label for _, label in labels) + " |",
              "|---|---|---|" + "---|" * len(labels)]
    for t in rep["tasks"]:
        lines.append("| %s | %s | %s | %s |" % (
            code(safe_text(t["task"], 40)[:7]), code(t["subject"], 100),
            "?" if t["gold_lines"] is None else t["gold_lines"] if isinstance(t["gold_lines"], int)
            else code(t["gold_lines"], 20),
            " | ".join(_cell(t["results"].get(h)) for h, _ in labels)))
    lines += [""] + ["- " + note for note in rep["notes"]]
    lines += ["", "Counted toward the spend cap: %s." % _money(rep["spent_usd"])]
    return "\n".join(lines) + "\n"


# --- Estimate -------------------------------------------------------------------------------

def estimate(doc, adapters) -> dict:
    n = len([t for t in doc.get("tasks", []) if t.get("validated")])
    rows = []
    for a in adapters:
        rng = a.cost_range()
        rows.append({"harness": a.name, "label": a.label, "installed": a.available(), "runs": n,
                     "per_run": [round(rng[0], 4), round(rng[1], 4)] if rng else None,
                     "total": [round(rng[0] * n, 4), round(rng[1] * n, 4)] if rng else None,
                     "models": [safe_text(m, 60) for m in a.estimate_models()],
                     "unpriced_reason": None if rng else a.unpriced_reason()})
    priced = [r for r in rows if r["total"]]
    return {"tasks": n, "runs": n * len(rows), "harnesses": rows,
            "total": [round(sum(r["total"][0] for r in priced), 4), round(sum(r["total"][1] for r in priced), 4)]
            if priced else None,
            "unpriced": [r["label"] for r in rows if not r["total"]], "prices_checked": PRICES_CHECKED,
            "tokens_per_run": [sum(LOW_RUN.values()), sum(HIGH_RUN.values())]}


def render_estimate(est) -> str:
    k = len(est["harnesses"])
    head = "%s: %s on %d harness%s." % (_plural(est["runs"], "run"), _plural(est["tasks"], "task"),
                                        k, "" if k == 1 else "es")
    if est["total"]:
        head += " Estimated cost at API prices: %s to %s" % (_money(est["total"][0]), _money(est["total"][1]))
        head += (", plus %s, which cannot be priced." % _and(est["unpriced"])) if est["unpriced"] else "."
    elif est["unpriced"]:
        head += " %s cannot be priced." % _and(est["unpriced"])
    lines = ["**%s**" % head, "", "| Harness | Found on this computer | Runs | Per run | Total | Priced as |",
             "|---|---|---|---|---|---|"]
    for r in est["harnesses"]:
        if r["per_run"]:
            per = "%s to %s" % (_money(r["per_run"][0]), _money(r["per_run"][1]))
            total = "%s to %s" % (_money(r["total"][0]), _money(r["total"][1]))
            low, high = (code(m, 60) for m in r["models"])
            basis = low if low == high else "%s to %s" % (low, high)
        else:
            per = total = "n/a"
            basis = "cannot be priced: %s" % r["unpriced_reason"]
        lines.append("| %s | %s | %d | %s | %s | %s |" % (r["label"], "yes" if r["installed"] else "no",
                                                        r["runs"], per, total, basis))
    lines += ["", "- The real cost depends on the model each harness uses and on your plan. On a subscription plan "
              "(such as Claude Max or ChatGPT), runs count against the plan's limits instead of a bill.",
              "- Each run is assumed to use %s tokens (a short fix) to %s tokens (a long one), mostly cached "
              "reads. Prices checked %s." % ("{:,}".format(est["tokens_per_run"][0]),
                                            "{:,}".format(est["tokens_per_run"][1]), est["prices_checked"]),
              "- The cap limits counted spend. Claude Code stops itself at the budget left. A Codex run has no "
              "limit except --timeout, and a run cut off there is counted as an estimate, so the bill can pass the "
              "cap by more than one run. Runs allowed with --allow-unpriced count $0."]
    missing = [r["label"] for r in est["harnesses"] if not r["installed"]]
    if missing:
        lines.append("- Not found on this computer: %s. Install it or leave it out." % _and(missing))
    if not est["tasks"]:
        lines.append("- The tasks file has no validated tasks. Run mine_tasks.py with --validate first.")
    return "\n".join(lines) + "\n"


# --- Command line ---------------------------------------------------------------------------------

def _load_tasks(path) -> dict:
    try:
        with open(path, encoding="utf-8") as fh:
            doc = json.load(fh)
    except (OSError, ValueError):
        raise UsageError("Cannot read the tasks file %s. Run mine_tasks.py first." % code(path, 300))
    if not isinstance(doc, dict) or "tasks" not in doc:
        raise UsageError("%s is not a tasks file from mine_tasks.py." % code(path, 300))
    return doc


def _names(text) -> list:
    return list(dict.fromkeys(n.strip() for n in (text or "").split(",") if n.strip()))


def _adapters(names, models, registry) -> list:
    wanted = _names(names)
    unknown = [n for n in wanted if n not in registry]
    if unknown or not wanted:
        raise UsageError("Unknown harness %s. Known: %s." % (safe_text(", ".join(unknown) or names, 80),
                                                             ", ".join(sorted(registry))))
    pinned = {}
    for pair in _names(models):
        name, sep, model = pair.partition("=")
        if not sep or name.strip() not in wanted or not model.strip():
            raise UsageError("--model takes harness=model pairs for the chosen harnesses, such as "
                             "claude-code=claude-sonnet-5-5.")
        pinned[name.strip()] = model.strip()
    try:
        return [registry[n](model=pinned.get(n)) for n in wanted]
    except ValueError as err:
        raise UsageError(str(err))


def _emit(markdown, data, args) -> None:
    """Print the markdown report, or JSON with --json; --out always gets the markdown."""
    sys.stdout.write(json.dumps(data, indent=2) + "\n" if args.json else markdown)
    if args.out:
        with open(args.out, "w", encoding="utf-8") as fh:
            fh.write(markdown)


def main(argv=None, registry=None) -> int:
    registry = registry or harnesses.ADAPTERS
    parser = argparse.ArgumentParser(description="Test-drive coding agents on tasks mined from your git history.")
    sub = parser.add_subparsers(dest="command", required=True)
    p_run = sub.add_parser("run", help="run the agents and score them")
    p_est = sub.add_parser("estimate", help="count the runs and estimate the cost")
    p_rep = sub.add_parser("report", help="print the scoreboard from results.jsonl")
    for p in (p_run, p_est):
        p.add_argument("--tasks", default=DEFAULT_TASKS, help="tasks.json from mine_tasks.py (default %s)" % DEFAULT_TASKS)
    p_run.add_argument("--harness", required=True, help="comma-separated: %s" % ", ".join(sorted(registry)))
    p_est.add_argument("--harness", default=",".join(sorted(registry)), help="comma-separated (default: all)")
    p_run.add_argument("--max-usd", type=float, required=True, help="total spend cap in dollars, required")
    p_run.add_argument("--timeout", type=int, default=900, help="seconds per agent run (default 900)")
    p_run.add_argument("--keep", action="store_true", help="keep each run's workspace folder")
    p_run.add_argument("--allow-unpriced", metavar="HARNESSES",
                       help="comma-separated harnesses to run although their cost cannot be measured (counted as $0)")
    p_run.add_argument("--model", help="pin models: harness=model pairs, comma-separated")
    p_run.add_argument("--results", help="results.jsonl (default: next to the tasks file)")
    p_rep.add_argument("--results", default=DEFAULT_RESULTS, help="results.jsonl (default %s)" % DEFAULT_RESULTS)
    for p in (p_run, p_est, p_rep):
        p.add_argument("--json", action="store_true", help="print JSON instead of markdown")
        p.add_argument("--out", help="also write the markdown to this file")
    args = parser.parse_args(argv)
    handlers = install_stop_handlers()
    try:
        return _main(args, registry)
    except KeyboardInterrupt:
        _stderr("Stopped. The run in progress is recorded with its spend and its copy is deleted; run the same "
                "command to continue.")
        return 130
    finally:
        restore_handlers(handlers)


def _main(args, registry) -> int:
    try:
        if args.command == "report":
            rep = summarize(load_results(args.results))
            _emit(render_report(rep), rep, args)
            return 0
        doc = _load_tasks(args.tasks)
        adapters = _adapters(args.harness, getattr(args, "model", None), registry)
        if args.command == "estimate":
            est = estimate(doc, adapters)
            _emit(render_estimate(est), est, args)
            return 0
        chosen = {a.name for a in adapters}
        allowed = _names(args.allow_unpriced)
        for name in allowed:
            if name not in chosen:
                raise UsageError("%s is not among the chosen harnesses (--harness)." % safe_text(name, 40))
        results = args.results or os.path.join(os.path.dirname(os.path.abspath(args.tasks)), "results.jsonl")
        summary = run(doc, results, adapters, args.max_usd, args.timeout, args.keep, allowed)
    except UsageError as err:
        print("error: %s" % err, file=sys.stderr)
        return 2
    if summary["not_installed"]:
        _stderr("Not found on this computer, so not run: %s." % ", ".join(summary["not_installed"]))
    for name, hint in summary["skipped"].items():
        _stderr("%s could not run. Fix: %s. Its other runs were skipped; run the same command again after that."
                % (name, hint))
    if summary["stopped_at_cap"]:
        _stderr("Stopped at the %s cap with %s not started. Finished runs are kept: rerun with a higher "
                "--max-usd to continue." % (_money(args.max_usd), _plural(summary["not_started"], "run")))
    _stderr("Results: %s" % code(results, 300))
    rep = summarize(load_results(results))
    _emit(render_report(rep), dict(rep, run=summary), args)
    return 0


if __name__ == "__main__":
    sys.exit(main())
