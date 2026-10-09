# How commands are found, checked, and run

`scripts/commands.py` does this work; `scripts/check.py` includes it in its report.

## Which commands count

The checker reads every project file that at least one agent loads (at start or on demand), and takes:

- every line of a code block marked `bash`, `sh`, `shell`, `zsh`, or `fish`, with `\` line breaks joined, comments and heredoc bodies dropped;
- only the `$ ` prompt lines of a `console` or `shell-session` block, or of a shell block that uses `$ ` prompts (the other lines are output);
- a code block with no language only when every line in it starts with a known command (`npm`, `make`, `pytest`, `uv`, `cargo`, and similar);
- inline code that starts with a known command and has an argument, such as `` `npm test` `` (a few runners count alone, such as `` `pytest` ``).

A command is **mentioned as something not to do** when it follows words such as "never", "don't", "not", "avoid", or "instead of" in the same clause ("Use `pnpm`, not `npm install`"), or is followed by "is deprecated". Those commands are listed but never checked, counted, or run. "Don't forget to run `npm test`" and "If you are not sure, run `make test`" still count as documented.

The same command in two files is one entry with both sources. Files whose real text is outside the repo (including links that point out of it) are never read, so their commands are not checked.

## Checks made without running anything

Each command runs from the folder its file applies to: the file's own folder for `AGENTS.md`, `CLAUDE.md`, and `GEMINI.md`; the repo root for files in `.github/`, `.cursor/`, and `.claude/`; and the importing file's folder for imported files. A leading `cd folder &&` moves it.

| Command | What is checked |
|---|---|
| any command | the program is on PATH, or the path it names exists; a program found only in the repo's `.venv/bin` or `venv/bin` is unverified (it works once that environment is active) |
| `npm run X`, `npm test`, `npm start`, `pnpm X`, `yarn X`, `bun run X` | script `X` exists in the nearest `package.json`; for `pnpm X`, `yarn X`, and `bun X`, an installed program named `X` also counts |
| `make X` (also `-C folder`, `-f file`, `--file=file`) | the Makefile exists and has target `X` |
| `just X` | the justfile exists and has recipe `X` or alias `X` |
| `uv run X`, `poetry run X` | the script path exists; `poetry` also needs a `pyproject.toml` |
| `python script.py`, `node file.js`, `bash script.sh`, `./script` | the script exists |
| `pytest path::test` | the test file exists (option values are skipped) |
| `cargo ...` | a `Cargo.toml` exists |
| `cd folder`, `source file` | the folder or file exists |

Results: **ok**; **fails**, with the reason; or **unverified**, when the checker cannot tell. Unverified cases: a workspace flag (`--filter`, `-w`, `--prefix`), a Makefile that includes other files or uses pattern rules, a placeholder such as `<target>`, `path/to`, or `...`, or a path inside a folder that a setup step creates (`.venv`, `node_modules`, `dist`, `build`).

## What --run runs: an allowlist

A command runs with `--run` only when all of these hold:

1. Its static check is **ok** (not failing, not unverified).
2. It is a known **test, lint, type check, or build** step, or a lone `--help` or `--version` of a program on PATH (never of a project script such as `./scripts/deploy.sh`).
3. Everything it would execute is on the allowlist: the script bodies of `npm run X` and similar (including npm `pre` and `post` scripts), the recipes of `make X` and of every prerequisite target (after `NAME = value`, `:=`, `?=`, and `+=` variables and command-line overrides are substituted), the recipes of `just X`, the `$(shell ...)` calls that make runs while reading the Makefile, and the backtick calls in a justfile. Arguments after the script name count too, so `npm test -- -u` is judged as `jest -u`.
4. The Makefile, justfile, and package manager settings it reaches use only the features listed below, so the lines the checker reads are the lines that run.

Inside a script body or recipe, a line may hold only: known test, lint, type check, and build programs (`pytest`, `jest`, `vitest`, `eslint`, `ruff check`, `mypy`, `tsc`, `go test`, `cargo build`, and similar); nested `npm`, `pnpm`, `yarn`, `bun`, `make`, or `just` calls, judged the same way; `node`, `python`, or `tsx` running a file inside the repo; the read-only helpers in the table below, in the forms the table allows; `cd` into an existing folder inside the repo; `true`, `exit`, and the shell options `set -e`, `-u`, `-x`, `-o pipefail`, and similar (not `set -k`); and `env` or `cross-env` with plain variable assignments. Anything else is held back with the reason "the script runs X, which this checker does not classify".

A body line may write files only inside the repo's build folders (`dist`, `build`, `out`, `target`, `coverage`, `node_modules`, `.venv`, `venv`, `.tox`, `.nox`, `.next`, `__pycache__`); a write anywhere else, new file or not, is held back.

## Read-only helpers: what the audit checked

Each helper was checked for options that write files, run other programs, or change the machine. These forms are held back even inside an allowlisted script:

| Helper | Held back when |
|---|---|
| `git` (only `log`, `diff`, `show`, `status`, `describe`, `rev-parse`, `ls-files`, `branch --show-current`) | a global option before the subcommand other than `--no-pager` (such as `-c`, `-C`, `--git-dir`, `--work-tree`, `--exec-path`, `--config-env`); `--output`, `-o`, or `--output-directory`, which write files; `--ext-diff`, `--textconv`, or `--show-signature`, which run other programs; a git variable that changes what git runs or reads (such as `GIT_EXTERNAL_DIFF`, `GIT_DIR`, `GIT_PAGER`, `GIT_CONFIG_*`); a repo `.git/config` that names a program (`core.fsmonitor` set to a path, `diff.external`, `diff.*.command`, `diff.*.textconv`, `filter.*`, `gpg.program`, `include.path`, `includeIf.*.path`); a `.git` file that points to another folder |
| `date` | any argument other than `+FORMAT` or a display flag (`-u`, `-R`, `-j`, `-I`, `--iso-8601`, `--rfc-3339`), since `-s`, `--set`, and a bare date set the clock |
| `hostname` | any argument other than a display flag (`-s`, `-f`, `-d`, `-i`, `-I`, `-A`, `-a`), since a name or `-F` sets the host name |
| `rg` | `--pre` or `--hostname-bin`, which run other programs, or `RIPGREP_CONFIG_PATH`, which can add them |
| `printf` | `-v`, which sets a shell variable |
| `tee`, `sort`, `uniq`, `sed`, `awk` | always: they are not helpers, because they can write files |
| `ls`, `cat`, `head`, `tail`, `wc`, `cut`, `tr`, `echo`, `grep`, `egrep`, `fgrep`, `jq`, `column`, `uname`, `pwd`, `whoami`, `basename`, `dirname`, `which`, `test`, `[` | an `--output`, `--out`, `--output-file`, `--outfile`, or `--output-directory` option; their manual pages list no option that runs another program or changes the machine |

## Shell features that hide what runs

A command is held back, at the top level and inside scripts, when it:

- uses a shell variable such as `$ARGS` or `${NAME}`, `$'...'` quoting, or brace expansion such as `{a,b}`, since the shell can turn these into options (`git log $'--output=x'` writes a file);
- defines a shell function (`jest() ( ... ); jest`);
- uses a wildcard in a folder that holds a file whose name starts with `-`, which the shell would pass as an option;
- names a place outside the repo in any argument: an absolute path that exists on this machine, a path starting with `~`, or a `..` path that leaves the repo (`--outputFile=/tmp/x`, `--junitxml=../x.xml`).

## Makefiles the checker can read

`make` is held back when the Makefile it reads:

- includes other files (`include`, `-include`, `sinclude`, `load`), or a caller sets `MAKEFILES`;
- uses pattern rules, static pattern rules, suffix rules such as `.c.o:`, or `.DEFAULT`;
- uses `ifeq`, `ifneq`, `ifdef`, `ifndef`, `define`, `undefine`, or `vpath`;
- has a line the checker cannot read, such as `$(RULES)` alone on a line or a computed name like `$(X) = value`;
- uses `$(file ...)`, `$(eval ...)`, or `$(guile ...)`;
- sets `SHELL`, `.SHELLFLAGS`, `.RECIPEPREFIX`, `.ONESHELL`, `VPATH`, `.EXTRA_PREREQS`, a blocked variable, or `MAKEFLAGS` with anything beyond plain flags such as `-s` or `--no-print-directory`, including target-specific settings such as `test: SHELL = ./x.sh`;
- can rebuild the Makefile itself (a rule for it, or a `Makefile.sh` next to it);
- reaches a name make would build with a built-in rule: a prerequisite with no rule that does not exist yet, or a name with no recipe that sits next to a source file such as `main.c` for `main.o`.

A tab-indented line outside a rule is read the way make reads it, as an ordinary line. Lines that end with `\` are joined first. Prerequisites are followed to any depth. A `?=` or `+=` variable is unknown when a calling script or a parent make sets it in the environment, and a parent make's command-line variables reach the makes it starts.

## Justfiles the checker can read

`just` is held back when the justfile:

- imports other files or declares modules (`import`, `mod`);
- uses a setting other than `quiet`, `ignore-comments`, `positional-arguments`, `allow-duplicate-recipes`, or `allow-duplicate-variables`;
- uses `shell()`, exports a blocked variable (including a `$NAME` parameter), or has a line the checker cannot read, such as a parameter with a default value or a multi-line string;
- reaches a recipe that is a script (a `#!` line or `[script]`), that uses an attribute other than `private`, `doc`, `group`, `confirm`, the operating system attributes, `positional-arguments`, `default`, `metadata`, `parallel`, `exit-message`, or `no-exit-message`, or that passes arguments to another recipe;
- uses a `{{ }}` expression other than a parameter or a variable whose value is a plain quoted string.

Recipes that share a name, such as one per operating system, are judged together. `[default]` picks the recipe that `just` runs when no name is given.

## Package manager settings

`npm`, `pnpm`, `yarn`, and `bun` scripts are held back when the repo, from the package folder up to the repo root, has an `.npmrc` that sets `script-shell`, `node-options`, or `onload-script`; for pnpm, a `pnpm-workspace.yaml` that sets `scriptShell` or `nodeOptions`; for yarn, a `.yarnrc.yml` that sets `yarnPath` or `plugins`, or a `.yarnrc` that sets `yarn-path`.

Examples that run: `pytest -q`, `python -m unittest`, `go test ./...`, `cargo test`, `npm test` (when the script is `jest`), `ruff check .`, `eslint src`, `mypy src`, `tsc --noEmit`, `npm run build` (when the script is `tsc -p .`), `make test` (when the recipe is `pytest -q`), `uv run --no-sync pytest`.

**Never run**, whatever the name: installs (`npm install`, `pnpm add`, `yarn`, pip, `pipenv install`, `uv sync`, `poetry install`, `brew install`, `cargo install`, `go install`, `playwright install`, and similar); deploys and cloud changes (`vercel`, `netlify deploy`, `terraform apply`, `kubectl apply`, `helm install`, any `aws`, `gcloud`, or `az` command); publishes (`npm publish`, `twine upload`, `cargo publish`, `docker push`, `gh release`); pushes (`git push`); deletes (`rm`, `find -delete`, `rsync --delete`, `git clean`, `git reset --hard`, `docker system prune`, targets named `clean`); elevated rights (`sudo`, `doas`, `su`, and `go test -exec sudo`); and anything that pipes a download into a shell or an interpreter. A script or target whose name says deploy, release, publish, push, clean, or install is never run either.

**Not run** (listed with the reason): commands that change files (`--fix`, `--write`, `--update`, `-u` snapshot updates, `--basetemp`, `ruff format`, `black` without `--check`, `pre-commit run`, and git commands such as `commit`, `pull`, and `submodule update`); watch and server modes; `npx` and other commands that may download a package; `uv run` without `--no-sync` or `--offline` (or `--frozen` with an existing `.venv`), `hatch run`, `tox`, `nox`, and `python -m build`, which create environments and install packages first; network tools such as `curl` and `wget`; flags that make a runner read another package or file (`--workspaces`, `--prefix`, `--filter`, a `just` or unknown `make` option); `cd` to a folder outside the repo or to `~` or a variable; output written to a file; command substitution and heredocs; commands with a placeholder; and anything else that is not a test, lint, type check, or build.

**Variables that change what runs** hold a command back too: `PATH`, `NODE_OPTIONS`, `GOFLAGS`, `PYTEST_ADDOPTS`, `LD_PRELOAD`, `BASH_ENV`, `PS4`, `CDPATH`, `RIPGREP_CONFIG_PATH`, `MAKEFLAGS`, `MAKEFILES`, git's own variables, a name ending in `_RUNNER` or `_WRAPPER`, and names starting with `npm_config_`, `JUST_`, or `YARN_`.

After a pipe, only the read-only helpers above are allowed. `tee`, `sed`, `awk`, `sort`, and `uniq` can write files, so a command that pipes into them is not run.

## How a run works

Commands run one at a time, each from its folder, through `bash -o pipefail -c`, with input closed and a timeout (`--timeout`, default 120 seconds). With pipefail, `npm test | tail -5` fails when `npm test` fails. A command that runs past the timeout is stopped along with its process group; the report says it did not finish, suggests a longer `--timeout`, and does not count it as a failure. The report keeps the exit code, the time, and the last lines of output, with secret-looking values replaced and cut to 160 characters. The first test command that runs sets the test loop time in the headline.

`--run` runs the project's own test and build code, which can do anything that code does. The checker screens the documented commands and the scripts and recipes they reach, not the code those scripts load, so use `--run` only on a repo whose tests you would run yourself, and prefer a container or a separate machine for code you have not reviewed.
