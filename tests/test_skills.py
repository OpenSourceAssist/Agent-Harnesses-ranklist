"""Skills: every folder under skills/ must pass the same bar in CI that it
passed when it was built (strict lint, security scan, trigger routing, its own
tests), and must reach every published surface (README, harnesses.json index,
site page with JSON-LD, sitemap, llms-full, the plugin marketplace)."""

import ast
import json
import re
import subprocess
import sys

import pytest

import build_site
import generate

ROOT = generate.REPO_ROOT
SKILLS = ROOT / "skills"
TOOLS = SKILLS / "evals" / "tools"
NAMES = sorted(p.parent.name for p in SKILLS.glob("*/SKILL.md"))


def _run(*args, cwd=ROOT):
    return subprocess.run([sys.executable, *map(str, args)], cwd=cwd, capture_output=True, text=True)


def test_ten_skills_ship():
    assert len(NAMES) >= 10, NAMES


def test_index_covers_every_skill_folder():
    index = generate.skills_index()
    assert [s["name"] for s in index] == NAMES
    for s in index:
        assert s["summary"] and not s["summary"].startswith(("#", "|", "[", "<", "`")), s["name"]
        assert s["license"] == "MIT" and s["version"], s["name"]
        paths = [f["path"] for f in s["files"]]
        assert "SKILL.md" in paths and "LICENSE.txt" in paths, s["name"]
        assert not [p for p in paths if "__pycache__" in p or ".pytest_cache" in p or p.endswith(".pyc")]


@pytest.mark.parametrize("name", NAMES)
def test_skill_passes_strict_lint(name):
    r = _run(TOOLS / "skill_lint.py", SKILLS / name, "--strict")
    assert r.returncode == 0, r.stdout + r.stderr


def test_security_scanner_finds_no_critical():
    r = _run(TOOLS / "skill_scanner.py", SKILLS)
    assert r.returncode == 0, r.stdout + r.stderr


def test_trigger_routing():
    r = _run(TOOLS / "run_trigger_evals.py")
    assert r.returncode == 0, r.stdout + r.stderr


def test_registry_matches_frontmatter_and_readmes():
    r = _run(TOOLS / "registry_lint.py", "--check", "--root", ROOT)
    assert r.returncode == 0, r.stdout + r.stderr


def test_shared_copies_match_their_source():
    r = _run(TOOLS / "sync_shared.py", "--check")
    assert r.returncode == 0, r.stdout + r.stderr


@pytest.mark.parametrize("name", NAMES + ["shared", "tools"])
def test_skill_evals_pass(name):
    """Each eval folder runs in its own pytest process: same-named helper
    modules in different skills would clash in one process."""
    r = _run("-m", "pytest", "-q", "-p", "no:cacheprovider", SKILLS / "evals" / name)
    assert r.returncode == 0, r.stdout[-3000:] + r.stderr[-3000:]


def test_scripts_parse_as_python_39():
    """Skills promise Python 3.9 (the macOS system python3)."""
    scripts = sorted(SKILLS.glob("*/scripts/**/*.py"))
    assert scripts
    for f in scripts:
        ast.parse(f.read_text(), filename=str(f), feature_version=(3, 9))


def test_skill_dir_paths_are_quoted():
    """Skill folders can sit under paths with spaces, so every command in a
    SKILL.md that starts from <skill-dir> quotes the whole script path."""
    for name in NAMES:
        text = (SKILLS / name / "SKILL.md").read_text()
        bare = re.findall(r'\b(?:python3?(?:\.\d+)?|sh|bash|node)\s+<skill-dir>/\S+', text)
        assert not bare, (name, bare[:3])


def test_every_skill_carries_the_mit_license():
    license_text = (SKILLS / "LICENSE").read_text()
    for name in NAMES:
        assert (SKILLS / name / "LICENSE.txt").read_text() == license_text, name


def test_no_em_dash_in_skill_files():
    for f in sorted(SKILLS.rglob("*")):
        if f.is_file() and f.suffix in {".md", ".py", ".json", ".txt"} and "tools" not in f.parts:
            assert "—" not in f.read_text(), f.relative_to(ROOT)


def test_every_skill_file_is_tracked_by_git():
    """.gitignore hides .claude/, CLAUDE.md (case-insensitive on macOS), *.log,
    build/ and more; a hidden skill file would 404 for anyone who installs it."""
    r = subprocess.run(["git", "ls-files", "--others", "--ignored", "--exclude-standard", "skills"],
                       cwd=ROOT, capture_output=True, text=True)
    ignored = [line for line in r.stdout.splitlines()
               if "__pycache__" not in line and ".pytest_cache" not in line and not line.endswith(".DS_Store")]
    assert ignored == []


def test_plugin_marketplace_lists_every_skill():
    market = json.loads((ROOT / ".claude-plugin" / "marketplace.json").read_text())
    plugin = next(p for p in market["plugins"] if p["name"] == "harness-skills")
    assert plugin["source"] == "./skills" and plugin["strict"] is False
    assert sorted(s.removeprefix("./") for s in plugin["skills"]) == NAMES


def test_readme_lists_every_skill():
    readme = (ROOT / "README.md").read_text()
    for name in NAMES:
        assert f"](skills/{name}/)" in readme, name


def test_site_pages_for_skills(tmp_path, monkeypatch):
    out = tmp_path / "site"
    monkeypatch.setattr(build_site, "OUT", out)
    stats = build_site.build()
    assert stats["skills"] == len(NAMES)
    sitemap = (out / "sitemap.xml").read_text()
    for name in NAMES:
        html = (out / "skills" / name / "index.html").read_text()
        blocks = [json.loads(m) for m in re.findall(r'<script type="application/ld\+json">(.*?)</script>', html, re.S)]
        code = next(b for b in blocks if b["@type"] == "SoftwareSourceCode")
        assert code["license"] == "https://opensource.org/licenses/MIT"
        assert f"skills/{name}" in code["codeRepository"]
        assert 'id="file-SKILL.md"' in html and "Copy</button>" in html
        assert f"npx skills add https://github.com/RyanAlberts/best-of-Agent-Harnesses/tree/main/skills/{name}" in html
        assert f"/skills/{name}/" in sitemap
    index = (out / "index.html").read_text()
    assert 'id="skills"' in index and all(f"/skills/{n}/" in index for n in NAMES)
    assert "# Skills, full text of each SKILL.md" in (out / "llms-full.txt").read_text()
