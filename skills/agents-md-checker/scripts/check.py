#!/usr/bin/env python3
"""
One report on the agent instruction files in a repo.

Usage:
    python3 check.py [--repo .] [--cwd DIR] [--run] [--timeout 120]
                     [--json] [--out FILE] [--fail-on problem|warning]

It runs load_map.py (which files each coding agent loads, and what gets cut
or skipped) and commands.py (whether the documented commands still work),
then adds cross-file checks: contradictions (package manager, test runner,
Node and Python versions) and dead paths. The report leads with one headline
sentence and ends with the next steps.

Read-only unless --run is given; --run runs only the commands classified
safe (tests, lint, type checks, builds, --help, --version). Standard library
only, Python 3.9+.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys

import commands
import load_map
from safe import code, safe_text

LOCKFILES = (
    ("package-lock.json", "npm"), ("npm-shrinkwrap.json", "npm"), ("pnpm-lock.yaml", "pnpm"),
    ("yarn.lock", "yarn"), ("bun.lockb", "bun"), ("bun.lock", "bun"),
)
PACKAGE_MANAGERS = ("npm", "pnpm", "yarn", "bun")
JS_RUNNERS = ("jest", "vitest", "mocha", "ava")
PY_RUNNERS = ("pytest", "unittest", "nose2")
HISTORY_RE = re.compile(
    r"\b(no longer|dropped|drop|deprecated|removed|legacy|previously|used to|formerly|eol|end[- ]of[- ]life|"
    r"unsupported|not supported|instead of|rather than|never|don't|do not|avoid|older|upgraded? from|"
    r"migrat\w* from)\b", re.I)
NODE_CLAIM_RE = re.compile(
    r"\bnode(?:\.?js)?\s+(?:version\s+)?(>=|>|\^|~)?\s*v?(\d{1,2})(?:\.\d+){0,2}"
    r"(\+|\s+or\s+(?:later|newer|higher|above))?(?!\d)(?!\.\d)", re.I)
PY_CLAIM_RE = re.compile(
    r"\bpython\s*(?:version\s+)?(>=|>|~=|==)?\s*v?3\.(\d{1,2})(?:\.\d+)?"
    r"(\+|\s+or\s+(?:later|newer|higher|above))?(?!\d)(?!\.\d)", re.I)
LINK_RE = re.compile(r"!?\[[^\]]*\]\(\s*<?([^)\s>]+)>?(?:\s+[\"'][^\"']*[\"'])?\s*\)")
PATH_CODE_RE = re.compile(r"^(?:\./)?[\w@.+-]+(?:/[\w@.+-]+)+/?$")
DOMAIN_RE = re.compile(r"\.(com|org|io|dev|net|ai|app|co|sh|so|gg|xyz|me)$", re.I)
PLACEHOLDER_RE = re.compile(r"path/to|[<>{}$*?~]|\.\.\.|\byour[-_]|\bexample\b", re.I)
CREATE_RE = re.compile(r"\b(create[sd]?|creating|generate[sd]?|generating|add|adds|added|adding)\b", re.I)
LIST_AFTER_RE = re.compile(r"^\s*(,|and\b|or\b)\s*v?\d")


def finding(fid, severity, message, fix, evidence):
    return {"id": fid, "severity": severity, "message": message, "fix": fix, "evidence": evidence}


def documented(cmds):
    return [e for e in cmds["commands"] if not e["negated"]]


def segments_of(command):
    """argv lists of each simple command, with wrappers and uv/poetry run prefixes removed."""
    found = []
    for segment in commands.parse(command) or []:
        argv = commands.strip_wrappers(segment["argv"])
        if len(argv) > 1 and commands.program_name(argv[0]) in ("uv", "poetry", "pdm", "pipenv", "hatch") and argv[1] == "run":
            argv = commands.runner_rest(argv[2:])
        if argv:
            found.append(argv)
    return found


def package_manager_of(command):
    for argv in segments_of(command):
        name = commands.program_name(argv[0])
        if name in PACKAGE_MANAGERS and not any(a in ("-g", "--global") for a in argv[1:]):
            return name
    return None


def js_runner_of(command):
    for argv in segments_of(command):
        name = commands.program_name(argv[0])
        if name in JS_RUNNERS:
            return name
        if name in ("npx", "bunx", "pnpm", "yarn", "bun") and len(argv) > 1:
            rest = argv[2:] if argv[1] in ("exec", "x", "dlx") else argv[1:]
            if rest and rest[0] in JS_RUNNERS:
                return rest[0]
    return None


def py_runner_of(command):
    for argv in segments_of(command):
        name = commands.program_name(argv[0])
        if name in ("pytest", "py.test"):
            return "pytest"
        if name == "python" and argv[1:2] == ["-m"] and len(argv) > 2 and argv[2] in PY_RUNNERS:
            return argv[2]
        if name == "nose2":
            return "nose2"
    return None


def read_json(path):
    try:
        with open(path, "r", encoding="utf-8") as handle:
            data = json.load(handle)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def read_first_line(path):
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as handle:
            for line in handle:
                if line.strip() and not line.lstrip().startswith("#"):
                    return line.strip()
    except OSError:
        pass
    return ""


def uses_by(entries, detect):
    uses = {}
    for entry in entries:
        tool = detect(entry["command"])
        if tool:
            uses.setdefault(tool, []).extend(s for s in entry["sources"] if s not in uses.get(tool, []))
    return uses


def describe_uses(uses):
    return " and ".join("%s (%s)" % (tool, ", ".join(code(source, 120) for source in sources[:2]))
                        for tool, sources in sorted(uses.items()))


def package_manager_findings(repo, cmds):
    found = []
    uses = uses_by(documented(cmds), package_manager_of)
    locks = [(name, tool) for name, tool in LOCKFILES if os.path.isfile(os.path.join(repo, name))]
    lock_tools = sorted({tool for _name, tool in locks})
    if len(lock_tools) > 1:
        found.append(finding(
            "lockfiles", "warning",
            "The repo has lockfiles for %d package managers: %s." % (
                len(lock_tools), ", ".join("%s (%s)" % (n, t) for n, t in locks)),
            "Delete the lockfiles of the package managers you do not use, and name the one you use in AGENTS.md.",
            [n for n, _t in locks]))
    field = read_json(os.path.join(repo, "package.json")).get("packageManager")
    field_tool = field.split("@", 1)[0] if isinstance(field, str) else None
    facts = []
    if len(uses) > 1:
        facts.append("your instruction files use %s" % describe_uses(uses))
    evidence = [s for sources in uses.values() for s in sources]
    for tool, sources in sorted(uses.items()):
        if field_tool in PACKAGE_MANAGERS and tool != field_tool:
            facts.append("%s uses %s, but package.json sets packageManager to %s"
                         % (code(sources[0], 120), tool, code(field, 60)))
            evidence.append("package.json")
        elif not field_tool and len(lock_tools) == 1 and tool != lock_tools[0]:
            lock_name = next(n for n, t in locks if t == lock_tools[0])
            facts.append("%s uses %s, but the repo has %s (a %s lockfile)"
                         % (code(sources[0], 120), tool, lock_name, lock_tools[0]))
            evidence.append(lock_name)
    if facts:
        message = "; ".join(facts)
        found.append(finding(
            "package-manager", "warning", message[0].upper() + message[1:] + ".",
            "Use one package manager in every instruction file, and make it match the lockfile.",
            evidence))
    return found


def test_runner_findings(repo, cmds):
    found = []
    entries = documented(cmds)
    js = uses_by(entries, js_runner_of)
    package = read_json(os.path.join(repo, "package.json"))
    deps = set()
    for key in ("dependencies", "devDependencies"):
        if isinstance(package.get(key), dict):
            deps.update(package[key])
    facts, evidence = [], []
    if len(js) > 1:
        facts.append("your files name different JavaScript test runners: %s" % describe_uses(js))
    installed = [r for r in JS_RUNNERS if r in deps]
    for runner, sources in sorted(js.items()):
        if deps and runner not in deps and installed:
            facts.append("%s runs %s, but package.json lists %s and not %s" % (
                code(sources[0], 120), runner, " and ".join(installed), runner))
            evidence.append("package.json")
    python = uses_by(entries, py_runner_of)
    if len(python) > 1:
        facts.append("your files name different Python test runners: %s" % describe_uses(python))
    evidence += [s for group in (js, python) for sources in group.values() for s in sources]
    if facts:
        message = "; ".join(facts)
        found.append(finding(
            "test-runner", "warning", message[0].upper() + message[1:] + ".",
            "Name the one test runner the repo uses, with the exact command, in every instruction file.",
            evidence))
    return found


def prose_sentences(loadmap):
    """(source, sentence) pairs from the prose of every project file an agent reads."""
    for path, shown, _anchor in commands.source_files(loadmap):
        text = commands.read_text(path, loadmap["repo"])
        for number, line, kind, _lang in load_map.scan_markdown(text):
            if kind != "prose":
                continue
            for sentence in re.split(r"(?<=[.!?;])\s+", load_map.INLINE_CODE_RE.sub(" ", line)):
                if sentence.strip():
                    yield "%s:%d" % (shown, number), sentence


def version_claims(loadmap, pattern):
    claims = []
    for source, sentence in prose_sentences(loadmap):
        if HISTORY_RE.search(sentence):
            continue
        for match in pattern.finditer(sentence):
            if LIST_AFTER_RE.match(sentence[match.end():]):
                continue  # "Node 18 and 20" lists versions; it is not a claim about one of them
            op, number, plus = match.group(1), int(match.group(2)), match.group(3)
            minimum = bool(plus) or op in (">=", ">")
            if op == ">":
                number += 1
            claims.append({"source": safe_text(source, 120), "value": number, "minimum": minimum,
                           "text": safe_text(match.group(0).strip(), 60)})
    return claims


def tool_version(repo, names):
    path = os.path.join(repo, ".tool-versions")
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as handle:
            for line in handle:
                parts = line.split()
                if len(parts) >= 2 and parts[0] in names:
                    return parts[1], ".tool-versions"
    except OSError:
        pass
    return None, None


def node_clause_allows(clause, major):
    match = re.match(r"^(>=|<=|>|<|\^|~|=)?v?(\d+|x|\*)(?:\.(\d+|x|\*))?(?:\.(\d+|x|\*))?$", clause)
    if not match:
        return True
    op, value, minor, patch = match.group(1) or "", match.group(2), match.group(3), match.group(4)
    if value in ("x", "*"):
        return True
    value = int(value)
    partial = (minor not in (None, "0", "x", "*")) or (patch not in (None, "0", "x", "*"))
    if op == ">=":
        return major >= value
    if op == ">":
        return major >= value if partial else major > value
    if op == "<":
        return major <= value if partial else major < value
    if op == "<=":
        return major <= value
    return major == value


def node_range_allows(spec, major):
    spec = re.sub(r"(>=|<=|>|<|=|\^|~)\s+", r"\1", spec)
    for alternative in spec.split("||"):
        alternative = alternative.strip()
        hyphen = re.match(r"^v?(\d+)\S*\s+-\s+v?(\d+)", alternative)
        if hyphen:
            if int(hyphen.group(1)) <= major <= int(hyphen.group(2)):
                return True
            continue
        if all(node_clause_allows(c, major) for c in alternative.split()):
            return True
    return False


def python_spec_allows(spec, minor):
    version = (3, minor)
    for clause in spec.split(","):
        match = re.match(r"^\s*(~=|===|==|!=|<=|>=|<|>)\s*(\d+)(?:\.(\d+|\*))?(?:\.(\d+|\*))?\s*$", clause)
        if not match:
            continue
        op, major, mn, patch = match.group(1), int(match.group(2)), match.group(3), match.group(4)
        target = (major, int(mn) if mn not in (None, "*") else 0)
        has_patch = patch not in (None, "*", "0")
        if op == ">=":
            ok = version >= target
        elif op == ">":
            ok = version >= target if has_patch else (version > target if mn is not None else version[0] > major)
        elif op == "<":
            ok = version <= target if has_patch else (version < target if mn is not None else version[0] < major)
        elif op == "<=":
            ok = version <= target if mn is not None else version[0] <= major
        elif op in ("==", "==="):
            ok = version[0] == major if mn in (None, "*") else version == target
        elif op == "!=":
            ok = version[0] != major if mn == "*" else (version != target if patch in (None, "*") else True)
        else:  # ~=
            ok = version == target if patch is not None else (version >= target and version[0] == major)
        if not ok:
            return False
    return True


def version_findings(repo, loadmap):
    found = []
    node_claims = [c for c in version_claims(loadmap, NODE_CLAIM_RE) if 4 <= c["value"] <= 40]
    if node_claims:
        pinned, pin_source = None, None
        for name in (".nvmrc", ".node-version"):
            value = read_first_line(os.path.join(repo, name))
            if value:
                pinned, pin_source = value, name
                break
        if not pinned:
            pinned, pin_source = tool_version(repo, ("nodejs", "node"))
        pin_major = re.match(r"^v?(\d+)", pinned or "")
        package = read_json(os.path.join(repo, "package.json"))
        engines = package.get("engines", {}).get("node") if isinstance(package.get("engines"), dict) else None
        facts, evidence = [], []
        for claim in node_claims:
            if pin_major:
                pin = int(pin_major.group(1))
                if (claim["minimum"] and claim["value"] > pin) or (not claim["minimum"] and claim["value"] != pin):
                    facts.append("%s says %s, but %s pins Node %d" % (code(claim["source"], 120), code(claim["text"], 60),
                                                                     pin_source, pin))
                    evidence += [claim["source"], pin_source]
            if isinstance(engines, str) and not node_range_allows(engines, claim["value"]):
                facts.append("%s says %s, but package.json engines.node is %s" % (
                    code(claim["source"], 120), code(claim["text"], 60), code(engines, 60)))
                evidence += [claim["source"], "package.json"]
        if facts:
            found.append(finding(
                "node-version", "warning", "; ".join(facts[:3]) + ".",
                "Make the Node version in your instruction files match .nvmrc and package.json engines.",
                sorted(set(evidence))))
    python_claims = version_claims(loadmap, PY_CLAIM_RE)
    if python_claims:
        pinned = read_first_line(os.path.join(repo, ".python-version"))
        pin_source = ".python-version" if pinned else None
        if not pinned:
            pinned, pin_source = tool_version(repo, ("python",))
        pin_minor = re.match(r"^3\.(\d+)", (pinned or "").split()[0] if pinned else "")
        requires = None
        try:
            with open(os.path.join(repo, "pyproject.toml"), "r", encoding="utf-8", errors="replace") as handle:
                match = re.search(r"(?m)^\s*requires-python\s*=\s*[\"']([^\"']+)[\"']", handle.read())
                requires = match.group(1) if match else None
        except OSError:
            pass
        facts, evidence = [], []
        for claim in python_claims:
            if pin_minor:
                pin = int(pin_minor.group(1))
                if (claim["minimum"] and claim["value"] > pin) or (not claim["minimum"] and claim["value"] != pin):
                    facts.append("%s says %s, but %s pins Python 3.%d" % (code(claim["source"], 120), code(claim["text"], 60),
                                                                         pin_source, pin))
                    evidence += [claim["source"], pin_source]
            if requires and not python_spec_allows(requires, claim["value"]):
                facts.append("%s says %s, but pyproject.toml requires-python is %s" % (
                    code(claim["source"], 120), code(claim["text"], 60), code(requires, 60)))
                evidence += [claim["source"], "pyproject.toml"]
        if facts:
            found.append(finding(
                "python-version", "warning", "; ".join(facts[:3]) + ".",
                "Make the Python version in your instruction files match .python-version and requires-python.",
                sorted(set(evidence))))
    return found


def find_contradictions(repo, loadmap, cmds):
    return (package_manager_findings(repo, cmds) + test_runner_findings(repo, cmds)
            + version_findings(repo, loadmap))


def path_exists(candidate, bases):
    return any(os.path.exists(os.path.join(base, candidate)) for base in bases)


def find_dead_paths(loadmap):
    """Relative links and inline-code paths in instruction prose that point at nothing."""
    repo = loadmap["repo"]
    dead, seen = [], set()
    for path, shown, anchor in commands.source_files(loadmap):
        folder = os.path.dirname(path)
        text = commands.read_text(path, repo)
        for number, line, kind, _lang in load_map.scan_markdown(text):
            if kind != "prose":
                continue
            source = "%s:%d" % (shown, number)
            for sentence in re.split(r"(?<=[.!?;])\s+", line):
                if not CREATE_RE.search(load_map.INLINE_CODE_RE.sub(" ", sentence)):
                    dead_in_sentence(sentence, source, shown, folder, anchor, repo, dead, seen)
    return dead


def dead_in_sentence(sentence, source, shown, folder, anchor, repo, dead, seen):
    """Add the dead links and inline-code paths of one sentence to dead."""
    for match in LINK_RE.finditer(load_map.INLINE_CODE_RE.sub(" ", sentence)):
        target = match.group(1).split("#", 1)[0].split("?", 1)[0]
        if (not target or re.match(r"^[a-z][a-z0-9+.-]*:", target, re.I) or target.startswith(("/", "~"))
                or PLACEHOLDER_RE.search(target)):
            continue
        target = target.replace("%20", " ")
        if not os.path.exists(os.path.join(folder, target)) and (target, shown) not in seen:
            seen.add((target, shown))
            dead.append({"path": target, "source": source, "kind": "link"})
    for match in load_map.INLINE_CODE_RE.finditer(sentence):
        token = match.group(2).strip()
        if not PATH_CODE_RE.match(token) or PLACEHOLDER_RE.search(token):
            continue
        clean = re.sub(r"^(\./)+", "", token)
        first = clean.split("/")[0]
        if first in commands.GENERATED_DIRS or DOMAIN_RE.search(first) or first.startswith("@"):
            continue
        bases = [anchor, repo, folder]
        if not path_exists(first, bases):
            continue  # a path whose top folder does not exist is usually an example, not a reference
        if not path_exists(clean, bases) and (clean, shown) not in seen:
            seen.add((clean, shown))
            dead.append({"path": token, "source": source, "kind": "code"})


def headline(loadmap, cmds, contradictions, dead):
    if loadmap["headline"].startswith("No agent context files"):
        return loadmap["headline"]
    clauses = load_map.headline_clauses(loadmap["findings"])[:2]
    clause = commands.commands_clause(cmds)
    if clause:
        clauses.append(clause)
    if len(clauses) < 3 and contradictions:
        topics = {"package-manager": "the package manager", "lockfiles": "the package manager",
                  "test-runner": "the test runner", "node-version": "the Node version",
                  "python-version": "the Python version"}
        clauses.append("your files disagree on %s" % topics[contradictions[0]["id"]])
    if len(clauses) < 3 and dead:
        clauses.append("%s no longer %s" % (load_map.plural(len(dead), "referenced path"),
                                            "exists" if len(dead) == 1 else "exist"))
    if not clauses:
        return loadmap["headline"]
    text = load_map.join_clauses(clauses)
    return text[0].upper() + text[1:] + commands.test_loop_sentence(cmds)


def next_steps(loadmap, cmds, contradictions, dead):
    steps = []
    for item in loadmap["findings"]:
        if item["severity"] == "problem" and item["fix"] not in steps:
            steps.append(item["fix"])
    for entry in documented(cmds):
        where = (code(entry["command"], 120), code(entry["sources"][0], 120))
        if entry["static"] == "fail":
            steps.append("Fix %s in %s: %s." % (where + (entry["problems"][0],)))
        elif entry.get("run") and not entry["run"]["timed_out"] and entry["run"]["exit_code"] != 0:
            steps.append("Fix %s in %s: it exits with code %s." % (where + (entry["run"]["exit_code"],)))
    for entry in documented(cmds):
        if entry.get("run") and entry["run"]["timed_out"]:
            steps.append("%s (%s) %s." % (code(entry["command"], 120), code(entry["sources"][0], 120),
                                          commands.timeout_advice(entry["run"]["timeout"])))
    for item in loadmap["findings"]:
        if item["severity"] == "warning" and item["fix"] not in steps:
            steps.append(item["fix"])
    steps += [c["fix"] for c in contradictions]
    if dead:
        steps.append("Update or remove %s, starting with %s (%s)." % (
            load_map.plural(len(dead), "dead path"), code(dead[0]["path"], 120), code(dead[0]["source"], 120)))
    return steps[:3]


def run_check(repo, cwd=None, run=False, timeout=120, env=None, home=None, managed_dir="default", progress=None):
    loadmap = load_map.build_load_map(repo, cwd=cwd, home=home, env=env, managed_dir=managed_dir)
    cmds = commands.collect(loadmap, env=env, run=run, timeout=timeout, progress=progress)
    contradictions = find_contradictions(loadmap["repo"], loadmap, cmds)
    dead = find_dead_paths(loadmap)
    return {
        "tool": "agents-md-checker/check",
        "version": load_map.VERSION,
        "repo": loadmap["repo"],
        "cwd": loadmap["cwd"],
        "headline": headline(loadmap, cmds, contradictions, dead),
        "load_map": loadmap,
        "commands": cmds,
        "contradictions": contradictions,
        "dead_paths": dead,
        "next_steps": next_steps(loadmap, cmds, contradictions, dead),
    }


def render_markdown(result):
    loadmap, cmds = result["load_map"], result["commands"]
    line_text = load_map.line_text
    lines = ["**%s**" % line_text(result["headline"]), "",
             "Repo: %s. Start folder: %s. %s" % (code(result["repo"], 400), code(result["cwd"], 200), load_map.TOKEN_NOTE), "",
             "## What each agent loads", ""] + load_map.render_table(loadmap) + [""]
    if loadmap["findings"]:
        lines += ["## Load findings", ""] + load_map.render_findings(loadmap["findings"]) + [""]
    lines += commands.render_sections(cmds)
    if result["contradictions"]:
        lines += ["## Contradictions", ""]
        lines += ["- %s Fix: %s" % (line_text(c["message"]), line_text(c["fix"])) for c in result["contradictions"]] + [""]
    if result["dead_paths"]:
        lines += ["## Dead paths", ""]
        lines += ["- %s (%s, %s)" % (code(d["path"], 200), code(d["source"], 120), d["kind"])
                  for d in result["dead_paths"]] + [""]
    if result["next_steps"]:
        lines += ["## Next steps", ""]
        lines += ["%d. %s" % (i, line_text(step)) for i, step in enumerate(result["next_steps"], 1)] + [""]
    lines += ["## Files per agent", ""] + load_map.render_details(loadmap)
    for note in loadmap["notes"]:
        lines.append("- Note: %s" % line_text(note))
    return "\n".join(lines).rstrip() + "\n"


def exit_code(result, fail_on):
    if not fail_on:
        return 0
    levels = [f["severity"] for f in result["load_map"]["findings"]]
    levels += ["problem"] * result["commands"]["summary"]["failing"]
    levels += [c["severity"] for c in result["contradictions"]]
    levels += ["warning"] * len(result["dead_paths"])
    threshold = load_map.SEVERITY_RANK[fail_on]
    return 1 if any(load_map.SEVERITY_RANK[level] >= threshold for level in levels) else 0


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="One report on a repo's agent instruction files: what each agent loads, whether the "
                    "documented commands work, contradictions, and dead paths. Read-only unless --run.")
    load_map.add_common_arguments(parser)
    parser.add_argument("--run", action="store_true",
                        help="run the commands classified safe (tests, lint, type checks, builds, --help, --version)")
    parser.add_argument("--timeout", type=int, default=120, help="seconds before a running command is stopped (default 120)")
    args = parser.parse_args(argv)
    if args.timeout <= 0:
        print("error: --timeout must be above zero", file=sys.stderr)
        return 2
    try:
        repo, cwd = load_map.resolve_paths(args.repo, args.cwd)
    except load_map.UsageError as err:
        print("error: %s" % err, file=sys.stderr)
        return 2
    result = run_check(repo, cwd=cwd, run=args.run, timeout=args.timeout,
                       progress=lambda text: print(text, file=sys.stderr))
    text = json.dumps(load_map.public(result), indent=2) + "\n" if args.json else render_markdown(result)
    load_map.emit(text, args.out)
    return exit_code(result, args.fail_on)


if __name__ == "__main__":
    sys.exit(main())
