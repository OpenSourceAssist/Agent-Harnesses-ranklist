#!/usr/bin/env python3
"""Copy the shared modules into the skills that use them.

A skill installs as one folder, so it cannot import code from another folder.
The source of truth is skills/evals/shared/. This script copies each module to
skills/<skill>/scripts/<module> with a first line that names the source. It
only writes into skill folders that already exist and lists the ones it skips.

Usage:
    python3 skills/evals/tools/sync_shared.py            copy the modules
    python3 skills/evals/tools/sync_shared.py --check    change nothing; fail when a copy differs
    --root <path>   repository root (default: three folders above this file)

Exit codes: 0 all copies match (or were written), 1 --check found a copy that
is missing or differs from its source, 2 usage error (a source is missing).
Python 3.9+, standard library only.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

MODULES = {
    "transcripts.py": ("guardrail-tester", "runaway-guard", "claim-check", "rules-to-guards",
                       "session-waste-report", "regression-finder"),
    "pricing.py": ("harness-test-drive", "runaway-guard", "session-waste-report", "regression-finder"),
    # transcripts.py imports safe.py, so every skill that gets transcripts.py needs safe.py too.
    "safe.py": ("guardrail-tester", "runaway-guard", "claim-check", "rules-to-guards",
                "session-waste-report", "regression-finder",
                "harness-test-drive", "tool-design-checker", "agents-md-checker", "sandbox-check"),
}
HEADER = ("# Copied from skills/evals/shared/{module} by skills/evals/tools/sync_shared.py. "
          "Edit the source, then run the sync.\n")


def sync(root, check=False):
    """Copy or compare every module. Returns (exit code, report lines)."""
    root = Path(root)
    shared = root / "skills" / "evals" / "shared"
    missing = [m for m in MODULES if not (shared / m).is_file()]
    if missing:  # write nothing unless every source is there
        return 2, ["error: source not found: skills/evals/shared/%s" % m for m in missing]
    lines, code = [], 0
    for module, skills in MODULES.items():
        wanted = HEADER.format(module=module) + (shared / module).read_text(encoding="utf-8")
        for skill in skills:
            if not (root / "skills" / skill).is_dir():
                lines.append("skipped %s: skill folder not found" % skill)
                continue
            target = root / "skills" / skill / "scripts" / module
            rel = target.relative_to(root).as_posix()
            current = target.read_text(encoding="utf-8") if target.is_file() else None
            if current == wanted:
                lines.append("up to date: %s" % rel)
            elif check:
                code = 1
                lines.append("%s: %s (run the sync)" % ("missing" if current is None else "differs", rel))
            else:
                target.parent.mkdir(exist_ok=True)
                target.write_text(wanted, encoding="utf-8")
                lines.append("copied: %s" % rel)
    return code, lines


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Copy skills/evals/shared modules into the skills that use them.")
    parser.add_argument("--check", action="store_true", help="change nothing; exit 1 if a copy is missing or differs")
    parser.add_argument("--root", default=str(Path(__file__).resolve().parents[3]), help="repository root")
    args = parser.parse_args(argv)
    code, lines = sync(args.root, check=args.check)
    print("\n".join(lines))
    return code


if __name__ == "__main__":
    sys.exit(main())
