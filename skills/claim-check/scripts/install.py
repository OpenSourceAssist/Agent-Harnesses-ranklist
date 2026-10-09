#!/usr/bin/env python3
"""Install or remove the claim-check Stop hook in Claude Code or Codex settings.

    python3 install.py [--harness claude-code|codex] [--scope user|project|local] [--project DIR]
                       [--settings PATH] [--uninstall] [--write]

Dry run by default: prints the claim-check entry's lines of the change and
writes nothing; other lines of the file are counted, never printed. --write
applies it. Existing settings and hooks are kept; only the claim-check entry
is added or removed. A symlinked settings file is changed at its target. The
first --write saves the original as <file>.claim-check.bak, and --uninstall
restores it when nothing else changed since. Python 3.9+, standard library
only. Exit codes: 0 done, 2 usage error or a settings file that is not valid
JSON.
"""

from __future__ import annotations

import argparse
import difflib
import json
import os
import shlex
import sys
import re
import tempfile

sys.dont_write_bytecode = True  # leave no __pycache__ in the skill folder
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from transcripts import redact  # noqa: E402

HOOK = os.path.join(os.path.dirname(os.path.abspath(__file__)), "stop_hook.py")
NAMES = {"claude-code": "Claude Code", "codex": "Codex"}


def settings_path(harness, scope, project) -> str:
    """The settings file each harness reads for a scope (facts file Q2)."""
    project = os.path.abspath(project or ".")
    if harness == "claude-code":
        if scope == "user":
            base = os.environ.get("CLAUDE_CONFIG_DIR") or os.path.join(os.path.expanduser("~"), ".claude")
            return os.path.join(base, "settings.json")
        return os.path.join(project, ".claude", "settings.local.json" if scope == "local" else "settings.json")
    if scope == "local":
        raise ValueError("Codex has no local scope; use user or project")
    if scope == "user":
        return os.path.join(os.environ.get("CODEX_HOME") or os.path.join(os.path.expanduser("~"), ".codex"),
                            "hooks.json")
    return os.path.join(project, ".codex", "hooks.json")


def hook_command(harness) -> str:
    """The command the harness runs. If stop_hook.py is later moved or deleted,
    python3 exits 2, which a Stop hook reads as "do not stop"; the fallback
    after || lets the agent finish instead (Codex wants JSON on stdout)."""
    if harness == "codex":
        return "python3 %s --harness codex || echo '{}'" % shlex.quote(HOOK)
    return "python3 %s || true" % shlex.quote(HOOK)


def _ours(handler) -> bool:
    command = handler.get("command") if isinstance(handler, dict) else None
    return isinstance(command, str) and "stop_hook.py" in command and "claim-check" in command


def _without_ours(data) -> dict:
    data = json.loads(json.dumps(data))
    hooks = data.get("hooks")
    if not isinstance(hooks, dict) or not isinstance(hooks.get("Stop"), list):
        return data
    groups = []
    for group in hooks["Stop"]:
        if isinstance(group, dict) and isinstance(group.get("hooks"), list):
            kept = [h for h in group["hooks"] if not _ours(h)]
            if kept:
                groups.append(dict(group, hooks=kept))
            elif len(kept) == len(group["hooks"]):
                groups.append(group)
        else:
            groups.append(group)
    if groups:
        hooks["Stop"] = groups
    else:
        del hooks["Stop"]
        if not hooks:
            del data["hooks"]
    return data


def _with_ours(data, harness) -> dict:
    data = _without_ours(data)
    hooks = data.setdefault("hooks", {})
    hooks.setdefault("Stop", []).append({"hooks": [{"type": "command", "command": hook_command(harness),
                                                    "timeout": 60}]})
    return data


def _our_handlers(data) -> list:
    hooks = data.get("hooks") if isinstance(data, dict) else None
    groups = hooks.get("Stop") if isinstance(hooks, dict) else None
    return [h for g in (groups if isinstance(groups, list) else []) if isinstance(g, dict)
            for h in (g.get("hooks") if isinstance(g.get("hooks"), list) else []) if _ours(h)]


_STRUCTURE = re.compile(r'^(?:"(?:hooks|Stop)":\s*)?[\[\]{}]*$')


def _print_change(old_text, new_text, handlers, indent, path) -> None:
    """The diff lines of the claim-check entry only. Any other line that
    changes (formatting, or a compact file written out with indentation) is
    counted, never printed: settings files can hold keys and tokens."""
    ours = set()
    for h in handlers:
        ours.update(line.strip().rstrip(",") for line in json.dumps({"hooks": [h]}, indent=indent).splitlines())
    other = 0
    for line in difflib.unified_diff(old_text.splitlines(), new_text.splitlines(), path, path + " (after)", n=0,
                                     lineterm=""):
        if line.startswith(("---", "+++")):
            sys.stdout.write(line.replace("`", "'") + "\n")
            continue
        if line.startswith("@@"):
            continue
        content = line[1:].strip().rstrip(",")
        if content in ours or _STRUCTURE.match(content):
            sys.stdout.write(redact(line).replace("`", "'") + "\n")
        else:
            other += 1
    if other:
        print("%d other line%s will be reformatted; their content is not shown." % (other, "" if other == 1 else "s"))


def _indent(text) -> int:
    for line in text.splitlines()[1:]:
        if line.strip():
            return max(1, len(line) - len(line.lstrip(" "))) if line.startswith(" ") else 2
    return 2


def _write_atomic(path, text) -> None:
    folder = os.path.dirname(path) or "."
    os.makedirs(folder, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=folder, prefix=".claim-check-", suffix=".json")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(text)
        if os.path.exists(path):
            os.chmod(tmp, os.stat(path).st_mode & 0o777)
        os.replace(tmp, path)
    except BaseException:
        if os.path.exists(tmp):
            os.unlink(tmp)
        raise


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(
        prog="install.py",
        description="Add or remove the claim-check Stop hook. Prints the change as a diff; --write applies it.")
    ap.add_argument("--harness", default="claude-code", choices=("claude-code", "codex"))
    ap.add_argument("--scope", default="user", choices=("user", "project", "local"),
                    help="user: every project; project: shared project settings; local: this project, "
                         "only you (Claude Code only). Default user")
    ap.add_argument("--project", default=".", metavar="DIR", help="the project folder for project or local scope")
    ap.add_argument("--settings", metavar="PATH", help="use this settings file instead of the scope's file")
    ap.add_argument("--uninstall", action="store_true", help="remove the claim-check hook instead of adding it")
    ap.add_argument("--write", action="store_true", help="apply the change (without it, nothing is written)")
    args = ap.parse_args(argv)
    try:
        named = os.path.abspath(args.settings) if args.settings else settings_path(args.harness, args.scope,
                                                                                    args.project)
    except ValueError as exc:
        print("install.py: %s" % exc, file=sys.stderr)
        return 2
    path = os.path.realpath(named)  # a symlinked file (stow, chezmoi) is changed at its target, link kept
    if path != named:
        print("%s links to %s; the change goes to that file." % (named, path))
    old_text = ""
    if os.path.exists(path):
        with open(path, encoding="utf-8") as fh:
            old_text = fh.read()
    try:
        data = json.loads(old_text) if old_text.strip() else {}
        if not isinstance(data, dict):
            raise ValueError("not a JSON object")
    except ValueError:
        print("install.py: the settings file %s is not valid JSON; fix it first, nothing was changed." % path,
              file=sys.stderr)
        return 2
    new = _without_ours(data) if args.uninstall else _with_ours(data, args.harness)
    if new == data:
        print("The claim-check Stop hook is %s in %s; nothing to change." % (
            "not" if args.uninstall else "already", path))
        return 0
    indent = _indent(old_text)
    new_text = json.dumps(new, indent=indent, ensure_ascii=False) + "\n"
    print("Planned change to %s (%s, %s scope):\n" % (path, NAMES[args.harness],
                                                     "custom" if args.settings else args.scope))
    _print_change(old_text, new_text, _our_handlers(data if args.uninstall else new), indent, path)
    if not args.write:
        print("\nNothing was written. Run again with --write to apply.")
        return 0
    backup = path + ".claim-check.bak"
    if args.uninstall and os.path.exists(backup):
        with open(backup, encoding="utf-8") as fh:
            saved = fh.read()
        try:
            # the backup with the hook added and removed again, as this run changed the current file
            same = _without_ours(_with_ours(json.loads(saved), args.harness)) == new
        except ValueError:
            same = False
        if same:
            _write_atomic(path, saved)
            os.unlink(backup)
            print("\nRemoved the claim-check Stop hook from %s and restored the file as it was before." % path)
            return 0
        print("\nThe settings changed after the hook was added, so the backup %s was kept." % backup)
    elif not args.uninstall and old_text and not os.path.exists(backup):
        _write_atomic(backup, old_text)
        print("\nSaved the original settings to %s." % backup)
    _write_atomic(path, new_text)
    print("\n%s the claim-check Stop hook %s %s." % ("Removed" if args.uninstall else "Added",
                                                    "from" if args.uninstall else "to", path))
    if args.harness == "codex" and not args.uninstall:
        print("Codex runs a new or changed hook only after you review and trust it: open Codex and run /hooks. "
              "Project hooks load only in trusted projects.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
