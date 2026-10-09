"""Tests for skills/evals/tools/sync_shared.py. Every run points --root at a
temporary tree, so the real skills folder is never touched."""

import os
import subprocess
import sys

TOOLS = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "tools")
SCRIPT = os.path.join(TOOLS, "sync_shared.py")

TRANSCRIPT_SKILLS = ["guardrail-tester", "runaway-guard", "claim-check", "rules-to-guards",
                     "session-waste-report", "regression-finder"]
PRICING_SKILLS = ["harness-test-drive", "runaway-guard", "session-waste-report", "regression-finder"]
# transcripts.py imports safe.py, so every skill with transcripts.py gets safe.py too.
SAFE_SKILLS = TRANSCRIPT_SKILLS + ["harness-test-drive", "tool-design-checker", "agents-md-checker", "sandbox-check"]
ALL_TEN = ["harness-test-drive", "agents-md-checker", "sandbox-check", "guardrail-tester", "runaway-guard",
           "claim-check", "rules-to-guards", "session-waste-report", "regression-finder", "tool-design-checker"]
HEADER = ("# Copied from skills/evals/shared/%s by skills/evals/tools/sync_shared.py. "
          "Edit the source, then run the sync.\n")


def make_repo(root, skills):
    shared = root / "skills" / "evals" / "shared"
    shared.mkdir(parents=True)
    (shared / "transcripts.py").write_text('"""transcripts source"""\nX = 1\n')
    (shared / "pricing.py").write_text('"""pricing source"""\nY = 2\n')
    (shared / "safe.py").write_text('"""safe source"""\nZ = 3\n')
    for name in skills:
        (root / "skills" / name).mkdir()
    return root


def run(root, *args):
    r = subprocess.run([sys.executable, SCRIPT, "--root", str(root)] + list(args),
                       capture_output=True, text=True)
    return r.returncode, r.stdout + r.stderr


def copies(root):
    found = []
    for name in sorted(os.listdir(str(root / "skills"))):
        scripts = root / "skills" / name / "scripts"
        if scripts.is_dir():
            found.extend("%s/%s" % (name, f) for f in sorted(os.listdir(str(scripts))))
    return found


def test_sync_copies_each_module_into_the_skills_that_list_it(tmp_path):
    make_repo(tmp_path, ALL_TEN)
    code, _out = run(tmp_path)
    assert code == 0
    want = sorted(["%s/transcripts.py" % s for s in TRANSCRIPT_SKILLS] + ["%s/pricing.py" % s for s in PRICING_SKILLS]
                  + ["%s/safe.py" % s for s in SAFE_SKILLS])
    assert copies(tmp_path) == want


def test_copy_starts_with_the_header_line_and_then_the_exact_source(tmp_path):
    make_repo(tmp_path, ["runaway-guard"])
    run(tmp_path)
    copy = (tmp_path / "skills" / "runaway-guard" / "scripts" / "transcripts.py").read_text()
    assert copy == HEADER % "transcripts.py" + '"""transcripts source"""\nX = 1\n'


def test_sync_skips_skill_folders_that_do_not_exist_and_says_so(tmp_path):
    make_repo(tmp_path, ["claim-check"])
    code, out = run(tmp_path)
    assert code == 0
    assert copies(tmp_path) == ["claim-check/safe.py", "claim-check/transcripts.py"]
    assert not (tmp_path / "skills" / "runaway-guard").exists()
    assert "runaway-guard" in out and "not found" in out


def test_check_passes_after_a_sync_and_changes_nothing(tmp_path):
    make_repo(tmp_path, ["runaway-guard", "harness-test-drive"])
    run(tmp_path)
    target = tmp_path / "skills" / "runaway-guard" / "scripts" / "pricing.py"
    before = (target.read_text(), os.path.getmtime(str(target)))
    assert run(tmp_path, "--check")[0] == 0
    assert (target.read_text(), os.path.getmtime(str(target))) == before


def test_check_fails_when_a_copy_differs_and_leaves_it_alone(tmp_path):
    make_repo(tmp_path, ["regression-finder"])
    run(tmp_path)
    target = tmp_path / "skills" / "regression-finder" / "scripts" / "transcripts.py"
    target.write_text(target.read_text() + "# local edit\n")
    code, out = run(tmp_path, "--check")
    assert code == 1 and "regression-finder/scripts/transcripts.py" in out
    assert target.read_text().endswith("# local edit\n")


def test_check_fails_when_an_existing_skill_folder_lacks_its_copy(tmp_path):
    make_repo(tmp_path, ["session-waste-report"])
    code, out = run(tmp_path, "--check")
    assert code == 1 and "session-waste-report/scripts/pricing.py" in out
    assert copies(tmp_path) == []


def test_check_passes_when_no_skill_folder_exists_yet(tmp_path):
    make_repo(tmp_path, [])
    assert run(tmp_path, "--check")[0] == 0


def test_sync_replaces_a_stale_copy(tmp_path):
    make_repo(tmp_path, ["claim-check"])
    stale = tmp_path / "skills" / "claim-check" / "scripts" / "transcripts.py"
    stale.parent.mkdir()
    stale.write_text("old\n")
    assert run(tmp_path)[0] == 0
    assert stale.read_text().endswith("X = 1\n")


def test_a_missing_source_is_a_usage_error(tmp_path):
    make_repo(tmp_path, ["claim-check"])
    (tmp_path / "skills" / "evals" / "shared" / "pricing.py").unlink()
    assert run(tmp_path)[0] == 2
    assert copies(tmp_path) == []
