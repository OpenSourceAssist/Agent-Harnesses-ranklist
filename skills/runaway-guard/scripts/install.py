#!/usr/bin/env python3
"""Install or remove the runaway guard hook for Claude Code or Codex.

Without --write it only prints the planned change as a diff. With --write it
merges one PreToolUse entry into the settings file (the hooks already there
stay as they are) and, when --cap is given, sets spend_cap_usd in the guard's
own settings file. --uninstall removes only the guard's own entries; with no
--harness, --scope, or --settings it checks all four places the guard can be.

Where it writes:
  Claude Code, user scope:     ~/.claude/settings.json (or $CLAUDE_CONFIG_DIR/settings.json)
  Claude Code, project scope:  <project>/.claude/settings.local.json (not shared with the team)
  Codex, user scope:           ~/.codex/hooks.json (or $CODEX_HOME/hooks.json)
  Codex, project scope:        <project>/.codex/hooks.json
  The cap, user scope:         ${XDG_STATE_HOME:-~/.local/state}/runaway-guard/runaway-guard.json
  The cap, project scope:      <project>/.claude/ or <project>/.codex/runaway-guard.json
  A copy of each settings file as it was before the install, so that
  --uninstall can put it back exactly: <state folder>/installs/

Exit codes: 0 done (or dry run shown), 2 bad arguments, a settings file it
cannot merge safely, or a file it cannot write (then nothing is changed).

Python 3.9+, standard library only. Nothing leaves the machine.
"""

from __future__ import annotations

import argparse
import difflib
import hashlib
import json
import os
import re
import shlex
import sys

import guard

HERE = os.path.dirname(os.path.abspath(__file__))
TIMEOUT = 10  # seconds; a hook that times out lets the call through
OURS_RE = re.compile(r"runaway-guard[^/\\'\"]*[/\\]scripts[/\\]guard\.py")
HARNESS_NAMES = {"claude-code": "Claude Code", "codex": "Codex"}
safe = guard.transcripts.safe_text


class Refuse(Exception):
    """A file the installer will not touch, with the reason."""


class WriteFailed(Exception):
    """A file that could not be written; earlier writes of the same run were undone."""


# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------

def _home(env) -> str:
    return env.get("HOME") or os.path.expanduser("~")


def settings_path(harness, scope, project, env) -> str:
    if harness == "claude-code":
        if scope == "user":
            return os.path.join(env.get("CLAUDE_CONFIG_DIR") or os.path.join(_home(env), ".claude"),
                                "settings.json")
        return os.path.join(project, ".claude", "settings.local.json")
    if scope == "user":
        return os.path.join(env.get("CODEX_HOME") or os.path.join(_home(env), ".codex"), "hooks.json")
    return os.path.join(project, ".codex", "hooks.json")


def config_path(harness, scope, project, env) -> str:
    if scope == "user":
        return os.path.join(guard.state_dir(env), guard.CONFIG_NAME)
    return os.path.join(project, ".claude" if harness == "claude-code" else ".codex", guard.CONFIG_NAME)


def hook_command(harness) -> str:
    """The command the harness runs: python3 plus the absolute path of guard.py.
    '|| true' keeps a moved or deleted skill folder from blocking every tool call:
    python3 exits 2 on a missing script, and exit 2 blocks the call. guard.py
    itself always exits 0, so its decision still passes through."""
    command = "python3 " + shlex.quote(os.path.join(HERE, "guard.py"))
    return command + (" --harness codex" if harness == "codex" else "") + " || true"


def status_command() -> str:
    return "python3 " + shlex.quote(os.path.join(HERE, "status.py"))


def shown(path, env) -> str:
    """A path for the report: the home folder as ~, made safe for markdown."""
    home = _home(env).rstrip(os.sep)
    if home and (path == home or path.startswith(home + os.sep)):
        path = "~" + path[len(home):]
    return safe(path, 300)


# ---------------------------------------------------------------------------
# Reading and merging
# ---------------------------------------------------------------------------

def _load(path, env, check_hooks=True):
    """(text, data) of a JSON settings file; ('', {}) when it does not exist."""
    if not os.path.exists(path):
        return "", {}
    try:
        with open(path, encoding="utf-8") as fh:
            text = fh.read()
    except (OSError, UnicodeDecodeError) as exc:
        raise Refuse("cannot read %s (%s)" % (shown(path, env), type(exc).__name__))
    if not text.strip():
        return text, {}
    try:
        data = json.loads(text)
    except ValueError as exc:
        raise Refuse("%s is not valid JSON (line %d: %s); fix it, then run this again"
                     % (shown(path, env), exc.lineno, exc.msg))
    if not isinstance(data, dict):
        raise Refuse("%s does not hold a JSON object" % shown(path, env))
    if check_hooks:
        hooks = data.get("hooks")
        if hooks is not None and not isinstance(hooks, dict):
            raise Refuse("in %s, \"hooks\" is not an object" % shown(path, env))
        if isinstance(hooks, dict) and "PreToolUse" in hooks and not isinstance(hooks["PreToolUse"], list):
            raise Refuse("in %s, \"hooks.PreToolUse\" is not a list" % shown(path, env))
    return text, data


def _is_ours(handler) -> bool:
    return isinstance(handler, dict) and bool(OURS_RE.search(str(handler.get("command") or "")))


def without_ours(data):
    """A copy of the settings with the guard's handlers removed, and how many were removed.
    A matcher group left empty by the removal goes too; so do the PreToolUse list and
    the hooks object when the guard was all they held."""
    new = json.loads(json.dumps(data))
    hooks = new.get("hooks")
    groups = hooks.get("PreToolUse") if isinstance(hooks, dict) else None
    if not isinstance(groups, list):
        return new, 0
    kept, removed = [], 0
    for group in groups:
        handlers = group.get("hooks") if isinstance(group, dict) else None
        if isinstance(handlers, list):
            left = [h for h in handlers if not _is_ours(h)]
            removed += len(handlers) - len(left)
            if len(left) < len(handlers):
                if not left:
                    continue
                group["hooks"] = left
        kept.append(group)
    if removed:
        if kept:
            hooks["PreToolUse"] = kept
        else:
            del hooks["PreToolUse"]
            if not hooks:
                del new["hooks"]
    return new, removed


def other_hooks(data) -> int:
    """PreToolUse handlers that are not the guard's."""
    hooks = data.get("hooks")
    groups = hooks.get("PreToolUse") if isinstance(hooks, dict) else None
    return sum(1 for g in groups or [] if isinstance(g, dict)
               for h in (g.get("hooks") if isinstance(g.get("hooks"), list) else []) if not _is_ours(h))


def with_ours(data, command):
    new, _ = without_ours(data)
    entry = {"hooks": [{"type": "command", "command": command, "timeout": TIMEOUT}]}
    new.setdefault("hooks", {}).setdefault("PreToolUse", []).append(entry)
    return new


def _indent(text):
    """The indentation the file already uses: a tab, a number of spaces, or 2."""
    m = re.search(r"^\{[ \t]*\r?\n([ \t]+)\S", text or "")
    if m:
        return "\t" if m.group(1).startswith("\t") else len(m.group(1))
    return 2


def dump(data, like) -> str:
    return json.dumps(data, indent=_indent(like), ensure_ascii=False) + "\n"


def diff(old, new, path, env) -> str:
    """A unified diff for the report, with secrets masked and backticks replaced,
    so a token in a settings file never reaches the screen."""
    name = shown(path, env)
    lines = difflib.unified_diff(old.splitlines(True), new.splitlines(True),
                                 "%s (now)" % name, "%s (after)" % name)
    return "".join(guard.transcripts.redact(line.rstrip("\n")).replace("`", "'") + "\n" for line in lines)


# ---------------------------------------------------------------------------
# Writing: one step per file, undone as a whole when any file fails
# ---------------------------------------------------------------------------

def _read_or_none(path):
    try:
        with open(path, encoding="utf-8") as fh:
            return fh.read()
    except FileNotFoundError:
        return None


def write(path, text) -> None:
    """Replace the file in one step (None removes it). A symlinked file is written
    through its link. The temp file is created with the final mode, so a private
    file is never readable by others, even for a moment."""
    real = os.path.realpath(path)
    if text is None:
        os.remove(real)
        return
    os.makedirs(os.path.dirname(real), exist_ok=True)
    mode = os.stat(real).st_mode & 0o777 if os.path.exists(real) else 0o600
    tmp = real + ".runaway-guard.tmp"
    try:
        os.remove(tmp)  # a leftover from a crash could have looser permissions
    except FileNotFoundError:
        pass
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, mode)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(text)
        os.chmod(tmp, mode)  # the umask may have removed bits the original file had
        os.replace(tmp, real)
    except OSError:
        try:
            os.remove(tmp)
        except OSError:
            pass
        raise


def apply(changes) -> None:
    """Write each (path, text) in order; on a failure undo the earlier ones."""
    done = []
    for path, text in changes:
        try:
            before = _read_or_none(os.path.realpath(path))
            write(path, text)
        except OSError as exc:
            for undo_path, undo_text in reversed(done):
                try:
                    write(undo_path, undo_text)
                except OSError:
                    pass
            raise WriteFailed("cannot write %s (%s)" % (path, exc.strerror or exc))
        done.append((path, before))


# A copy of each settings file as it was before the install, so that uninstall can
# put it back byte for byte when nothing else changed it in between.

def _backup_path(target, env) -> str:
    key = hashlib.sha256(os.path.realpath(target).encode("utf-8", "surrogatepass")).hexdigest()[:16]
    return os.path.join(guard.state_dir(env), "installs", key + ".json")


def _backup(target, env) -> dict:
    record = guard._read_json(_backup_path(target, env))
    return record if isinstance(record.get("written"), str) else {}


def remember_install(target, old_text, new_text, data, env) -> None:
    """Keep the file as it was before the guard was added (None when it did not exist)."""
    record = _backup(target, env)
    if record and record["written"] == old_text:
        original = record.get("original")        # a re-install: the first original stays
    elif without_ours(data)[1] == 0:
        original = old_text if os.path.exists(target) else None
    else:
        _forget_install(target, env)              # installed before this record existed
        return
    write(_backup_path(target, env), json.dumps({"path": os.path.realpath(target), "original": original,
                                                 "written": new_text}))


def _forget_install(target, env) -> None:
    try:
        os.remove(_backup_path(target, env))
    except OSError:
        pass


# ---------------------------------------------------------------------------
# Report
# ---------------------------------------------------------------------------

def _hooks(n) -> str:
    return "%d PreToolUse hook%s" % (n, "" if n == 1 else "s")


def _money(value) -> str:
    return "$%.2f" % value


def headline(harness, limits) -> str:
    parts = []
    if limits["loop_repeats"]:
        parts.append("the same call repeats %d times" % limits["loop_repeats"])
    if limits["failure_streak"]:
        parts.append("%d calls fail in a row" % limits["failure_streak"])
    if limits["spend_cap_usd"]:
        parts.append("spend passes %s" % _money(limits["spend_cap_usd"]))
    name = HARNESS_NAMES[harness]
    if not parts:
        return "Runaway guard will run in %s with every trip wire turned off, so it will stop nothing." % name
    if len(parts) == 1:
        listed = parts[0]
    else:
        listed = ", ".join(parts[:-1]) + (", or " if len(parts) > 2 else " or ") + parts[-1]
    return "Runaway guard will stop %s when %s." % (name, listed)


def _cap_source(source, env) -> str:
    kind, where = source
    if kind == "environment":
        return "%s is set in this shell" % where
    if kind == "project":
        return "the project file %s sets a lower cap, and a project file can lower a limit but never raise one" \
               % shown(where, env)
    return "your user file %s sets a lower cap, and a project file can lower a limit but never raise one" \
           % shown(where, env)


def notes_for(harness, scope, settings, limits, sources, cap, env) -> list:
    notes = []
    if cap is not None and limits["spend_cap_usd"] != cap:
        notes.append("The cap stays %s because %s." % (_money(limits["spend_cap_usd"]),
                                                       _cap_source(sources["spend_cap_usd"], env)))
    if settings.get("disableAllHooks") is True:
        notes.append("This settings file sets disableAllHooks, which turns every hook off: the guard will "
                     "not run until that setting is removed.")
    if harness == "claude-code":
        notes.append("Claude Code normally applies hook changes to sessions that are already running. A "
                     "session the guard already counts is blocked at its next tool call once it is over the "
                     "cap; start its count over with %s --session <id> --reset. A session the guard checks "
                     "for the first time after it has spent more than the cap is counted from that point, "
                     "and the guard says so once." % safe(status_command(), 400))
        if scope == "user":
            notes.append("Cursor also runs the hooks in Claude Code settings files. The guard is built to "
                         "recognize Cursor and let its calls through; that has not been tested in Cursor.")
    else:
        notes.append("Codex runs a new or changed hook only after you trust it: start Codex, type /hooks, "
                     "and trust the runaway-guard entry.")
        if scope == "project":
            notes.append("Codex runs project hooks only in projects you have marked as trusted.")
        if limits["failure_streak"] or limits["spend_cap_usd"]:
            notes.append("Codex hooks cannot ask you a question or show a warning, so after a run of failed "
                         "calls the guard blocks the next call and tells the agent to ask you, and the "
                         "early spend warning shows only in status.py.")
    for key, name in sorted(guard.ENV_NAMES.items()):
        if env.get(name) and not (key == "spend_cap_usd" and cap is not None and limits[key] != cap):
            notes.append("%s is set in this shell. Where the agent runs with it set, it wins over the "
                         "settings file." % name)
    notes.append("Run --uninstall before you move, update, or remove this skill, then install again from "
                 "the new place: the hook entry points at this folder.")
    if limits["spend_cap_usd"]:
        notes.append("Dollar amounts are estimates from token counts at API list prices. On a subscription "
                     "plan they measure usage, not your bill.")
    return notes


def emit(text, out_path) -> int:
    """Print the report, or write it to --out and say where."""
    if not out_path:
        sys.stdout.write(text)
        return 0
    try:
        with open(out_path, "w", encoding="utf-8") as fh:
            fh.write(text)
    except OSError as exc:
        print("error: cannot write the report to %s: %s" % (safe(out_path, 300), exc.strerror or exc),
              file=sys.stderr)
        return 2
    print("Report written to %s" % safe(out_path, 300))
    return 0


def _changed_nothing(reason) -> int:
    print("Runaway guard changed nothing: %s." % safe(str(reason), 600), file=sys.stderr)
    return 2


# ---------------------------------------------------------------------------
# Install
# ---------------------------------------------------------------------------

def install(args, env, project) -> int:
    harness, scope = args.harness or "claude-code", args.scope or "user"
    target = os.path.abspath(args.settings) if args.settings else settings_path(harness, scope, project, env)
    cap_file = config_path(harness, scope, project, env)
    command = hook_command(harness)
    try:
        old_text, data = _load(target, env)
        cap_old, cap_data = _load(cap_file, env, check_hooks=False) if args.cap is not None else ("", {})
    except Refuse as exc:
        return _changed_nothing(exc)
    new_data = with_ours(data, command)
    new_text = old_text if old_text and new_data == data else dump(new_data, old_text)
    cap_new = cap_old
    if args.cap is not None and cap_data.get("spend_cap_usd") != args.cap:
        cap_new = dump(dict(cap_data, spend_cap_usd=args.cap), cap_old)

    # The limits the guard will use after this change.
    user_path = os.path.join(guard.state_dir(env), guard.CONFIG_NAME)
    user_cfg = guard._read_json(user_path)
    project_file = guard.project_config_path(project)
    project_cfg = guard._read_json(project_file) if project_file else {}
    if args.cap is not None:
        if scope == "user":
            user_cfg = dict(user_cfg, spend_cap_usd=args.cap)
        else:
            project_cfg = dict(project_cfg, spend_cap_usd=args.cap)
            project_file = project_file or cap_file
    limits, sources = guard.merge_limits(env, user_cfg, project_cfg, user_path, project_file)

    settings_changed, cap_changed = new_text != old_text, cap_new != cap_old
    written = False
    if args.write and (settings_changed or cap_changed):
        changes = ([(cap_file, cap_new)] if cap_changed else []) + ([(target, new_text)] if settings_changed else [])
        try:
            apply(changes)
        except WriteFailed as exc:
            return _changed_nothing(exc)
        if settings_changed:
            try:
                remember_install(target, old_text, new_text, data, env)
            except OSError as exc:
                guard._log_error(exc)  # uninstall then removes the entry without restoring the old text
        written = True
    notes = notes_for(harness, scope, new_data, limits, sources, args.cap, env)

    if args.json:
        return emit(json.dumps({
            "headline": headline(harness, limits), "action": "install", "harness": harness, "scope": scope,
            "settings_path": target, "config_path": cap_file, "command": command,
            "cap_usd": limits["spend_cap_usd"], "limits": limits, "changed": settings_changed or cap_changed,
            "written": written, "other_hooks": other_hooks(data), "notes": notes,
        }, indent=2) + "\n", args.out)

    out = ["**%s**" % headline(harness, limits), ""]
    if not (settings_changed or cap_changed):
        out.append("Nothing to change: %s already matches." % shown(target, env))
    elif written:
        out.append("Done: the change below is written.")
    else:
        out.append("Dry run: nothing is written yet. Run the same command with --write to apply this change.")
    if settings_changed:
        others = other_hooks(data)
        what = "adds one PreToolUse hook entry" + (
            " and leaves the %s already there unchanged" % _hooks(others) if others else "")
        out += ["", "Settings file %s: %s." % (shown(target, env), what), "", "```diff",
                diff(old_text, new_text, target, env).rstrip("\n"), "```"]
    if cap_changed:
        out += ["", "Guard settings file %s: spend cap %s." % (shown(cap_file, env), _money(args.cap)), "",
                "```diff", diff(cap_old, cap_new, cap_file, env).rstrip("\n"), "```"]
    out += ["", "The hook command: `%s`" % safe(command, 400), "", "Notes:"] + ["- " + n for n in notes]
    return emit("\n".join(out) + "\n", args.out)


# ---------------------------------------------------------------------------
# Uninstall
# ---------------------------------------------------------------------------

def _uninstall_plan(harness, scope, target, env) -> dict:
    old_text, data = _load(target, env)
    new_data, removed = without_ours(data)
    plan = {"harness": harness, "scope": scope, "settings_path": target, "removed": removed,
            "other_hooks": other_hooks(data), "old_text": old_text, "new_text": old_text,
            "restored_exactly": False, "reformatted": False}
    if removed:
        record = _backup(target, env)
        if record and record["written"] == old_text:  # nothing else changed it: put the old file back
            plan["new_text"], plan["restored_exactly"] = record.get("original"), True
        else:
            plan["new_text"] = dump(new_data, old_text)
            plan["reformatted"] = dump(data, old_text) != old_text
    return plan


def uninstall(args, env, project) -> int:
    if args.settings:
        places = [(args.harness or "claude-code", args.scope or "user", os.path.abspath(args.settings))]
    else:
        places = [(h, sc, settings_path(h, sc, project, env))
                  for h in ([args.harness] if args.harness else ["claude-code", "codex"])
                  for sc in ([args.scope] if args.scope else ["user", "project"])]
    try:
        plans = [_uninstall_plan(h, sc, path, env) for h, sc, path in places]
    except Refuse as exc:
        return _changed_nothing(exc)
    found = [p for p in plans if p["removed"]]
    written = False
    if args.write and found:
        try:
            apply([(p["settings_path"], p["new_text"]) for p in found])
        except WriteFailed as exc:
            return _changed_nothing(exc)
        for p in found:
            _forget_install(p["settings_path"], env)
        written = True
    removed = sum(p["removed"] for p in found)
    if not found:
        title = "Runaway guard is not installed in any of the %d settings files checked." % len(plans)
    elif len(found) == 1:
        p = found[0]
        title = "Runaway guard will be removed from %s%s." % (
            shown(p["settings_path"], env),
            ", leaving the other %s there unchanged" % _hooks(p["other_hooks"]) if p["other_hooks"] else "")
    else:
        title = "Runaway guard will be removed from %d settings files: %s." % (
            len(found), " and ".join(shown(p["settings_path"], env) for p in found))
    notes = []
    for p in found:
        if p["new_text"] is None:
            notes.append("%s held only the guard, so it is removed as before the install."
                         % shown(p["settings_path"], env))
        elif p["reformatted"]:
            notes.append("%s changed after the install, so only the guard's entry is removed, and the file is "
                         "written again as formatted JSON." % shown(p["settings_path"], env))
    if found:
        notes.append("The guard's own settings and session files stay in %s; delete that folder to remove "
                     "them too." % shown(guard.state_dir(env), env))

    if args.json:
        return emit(json.dumps({
            "headline": title, "action": "uninstall", "hook_entries_removed": removed,
            "changed": bool(found), "written": written, "notes": notes,
            "targets": [{k: p[k] for k in ("harness", "scope", "settings_path", "removed", "other_hooks",
                                           "restored_exactly")} for p in plans],
        }, indent=2) + "\n", args.out)

    out = ["**%s**" % title, ""]
    if not found:
        out += ["Checked:"] + ["- %s" % shown(p["settings_path"], env) for p in plans]
    else:
        out.append("Done: the change below is written." if written else
                   "Dry run: nothing is written yet. Run the same command with --write to apply this change.")
        for p in found:
            what = "removes %d runaway-guard hook entr%s" % (p["removed"], "y" if p["removed"] == 1 else "ies")
            out += ["", "Settings file %s: %s." % (shown(p["settings_path"], env), what), "", "```diff",
                    diff(p["old_text"], p["new_text"] or "", p["settings_path"], env).rstrip("\n"), "```"]
    if notes:
        out += ["", "Notes:"] + ["- " + n for n in notes]
    return emit("\n".join(out) + "\n", args.out)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def _cap(text):
    try:
        value = float(text)
    except ValueError:
        raise argparse.ArgumentTypeError("the cap is a dollar amount, such as 10 or 2.5")
    if not value >= 0 or value == float("inf"):
        raise argparse.ArgumentTypeError("the cap must be 0 (off) or more")
    return int(value) if value == int(value) else value


def _parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description="Install or remove the runaway guard hook. Prints the planned change as a diff; "
                    "--write applies it.")
    p.add_argument("--harness", choices=("claude-code", "codex"),
                   help="which agent gets the hook (install default: claude-code; uninstall default: both)")
    p.add_argument("--scope", choices=("user", "project"),
                   help="user: every session of this user; project: only sessions in --project "
                        "(install default: user; uninstall default: both)")
    p.add_argument("--project", default=".", help="the project folder (default: the current folder)")
    p.add_argument("--cap", type=_cap, metavar="USD",
                   help="spend cap in dollars per session; 0 turns the spend wire off "
                        "(default: keep the cap already set, else $10)")
    p.add_argument("--settings", metavar="PATH", help="change this settings file instead of the usual one")
    p.add_argument("--uninstall", action="store_true", help="remove the guard's hook entries")
    p.add_argument("--write", action="store_true", help="apply the change (without it, nothing is written)")
    p.add_argument("--json", action="store_true", help="print machine-readable JSON instead of markdown")
    p.add_argument("--out", metavar="PATH", help="write the report to this file instead of printing it")
    return p


def main(argv=None) -> int:
    parser = _parser()
    args = parser.parse_args(argv)
    if args.uninstall and args.cap is not None:
        parser.error("--cap does not go with --uninstall")
    project = os.path.abspath(args.project)
    return uninstall(args, os.environ, project) if args.uninstall else install(args, os.environ, project)


if __name__ == "__main__":
    sys.exit(main())
