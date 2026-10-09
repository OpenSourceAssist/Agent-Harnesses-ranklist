# How the skills are tested

Every skill in this folder passes four automated checks, and CI runs them on every push. A fifth set, the behavior cases, is written for a fresh agent to run. The test files live here, next to the skills rather than inside them, so when you install a skill you get only the files it needs to run.

| Check | What it proves | Command | Runs in CI |
|---|---|---|---|
| Structure | `SKILL.md` follows the [Agent Skills specification](https://agentskills.io/specification): name matches the folder, the description says when to use the skill, the body stays under 500 lines, and every file it mentions exists | `python3 skills/evals/tools/skill_lint.py skills/<name> --strict` | yes |
| Security | No hidden installers, no piping downloads into a shell, no undeclared network calls, no credential reads that the skill does not explain | `python3 skills/evals/tools/skill_scanner.py skills` | yes |
| Trigger routing | Each skill's example prompts pick that skill over all the others, and prompts that only sound similar do not | `python3 skills/evals/tools/run_trigger_evals.py` | yes |
| Scripts | Every classifier, threshold, and output field behaves as the skill promises, on synthetic data built at test time | `python3 -m pytest -q -p no:cacheprovider skills/evals/<name>` | yes |
| Behavior | A fresh agent given only the skill and a real request runs the right script, leads with the right result, and asks before changing anything | the prompts in `skills/evals/<name>/evals.json`, run with skill-creator's eval runner | no |

`tests/test_skills.py` wires the first four into the repo's test suite, along with the registry check, the shared-code check below, and a check that every script parses as Python 3.9.

## Files per skill

- `test_<name>.py`: the script tests. Fixtures are built in a temporary folder at test time, because this repo's `.gitignore` hides names such as `.claude/` and `CLAUDE.md`.
- `trigger-cases.json`: example prompts, each marked as one that should or should not trigger the skill.
- `evals.json`: behavior cases in the format used by Anthropic's [skill-creator](https://github.com/anthropics/skills/tree/main/skills/skill-creator), so its eval runner and viewer work on these files as they are.

## Shared code

Six skills read agent session logs, four compute cost, and ten clean untrusted text the same way before it goes in a report, so the log reader (`transcripts.py`), the price table (`pricing.py`), and the text cleaner (`safe.py`) live once, in `shared/`. Each skill still installs on its own, so `tools/sync_shared.py` copies the shared files into the skills that use them, and `sync_shared.py --check` fails the build if a copy drifts from its source. To change a shared file, edit it in `shared/`, run `python3 skills/evals/tools/sync_shared.py`, and commit both.

To show untrusted text in a Markdown report, a skill script uses `from safe import code, safe_text`: `safe_text()` gives one line with secrets masked, and `code()` puts that line inside inline code so links and HTML stay plain text. `transcripts.py` imports these functions from `safe.py`, so every skill that gets `transcripts.py` also gets `safe.py`.

## Adding a skill

1. Create `skills/<name>/` with `SKILL.md`, `README.md` (title on line 1, a one-sentence summary on line 3), `LICENSE.txt` (a copy of `skills/LICENSE`), `scripts/`, and `references/`.
2. Create `skills/evals/<name>/` with the three files above.
3. Add the name to `.claude-plugin/marketplace.json` and to `SKILL_GROUPS` in `scripts/generate.py`.
4. Run `python3 skills/evals/tools/registry_lint.py --write`, then `python3 scripts/generate.py`, then the test suite.

## Credits

The layout, the registry, and the tools in `tools/` come from Shubham Saboo's [agent_skills collection](https://github.com/Shubhamsaboo/awesome-llm-apps/tree/main/agent_skills) in awesome-llm-apps. The tools are used under the Apache License 2.0 (see `tools/LICENSE-APACHE-2.0`), with paths changed to fit this repo.
