#!/usr/bin/env python3
"""
Check the commands that agent instruction files tell the agent to run.

Usage:
    python3 commands.py [--repo .] [--cwd DIR] [--run] [--timeout 120]
                        [--json] [--out FILE] [--fail-on problem|warning]

It takes commands from shell code blocks and command-like inline code in
every instruction file an agent loads from the repo (see load_map.py) and
checks each one without running it: package.json scripts, Makefile targets,
justfile recipes, uv and poetry projects, programs on PATH, and the script
and folder paths the command names.

--run is an allowlist. A command runs only when everything that would
execute (the command, and the script bodies, recipes, and prerequisites it
reaches, after variable substitution) is a known test, lint, type check, or
build step, or a lone --help or --version. Anything this checker cannot
classify is held back. See references/command-checks.md. Output excerpts are
redacted and cut to 160 characters.

Standard library only, Python 3.9+.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shlex
import shutil
import signal
import subprocess
import sys
import time

import load_map
from safe import code, safe_text

SAFE = "safe"
NEVER = "never"
NOT_RUN = "not_run"
SAFE_KINDS = ("test", "typecheck", "lint", "build", "help", "version")
NOT_A_CHECK = "not a test, lint, type check, or build command"
PLACEHOLDER_REASON = "has a placeholder such as <file>"
INSTALLS_FIRST = "creates the environment and installs packages first"
EXCERPT_LIMIT = 160
MAX_DEPTH = 6

SHELL_LANGS = {"sh", "bash", "shell", "zsh", "fish", "ksh", "shellscript"}
CONSOLE_LANGS = {"console", "shell-session", "sh-session", "shellsession", "terminal"}
PLAIN_LANGS = {"", "text", "txt", "plaintext", "plain"}

# First words that make inline code or an unlabeled code block count as a command.
COMMAND_WORDS = {
    "npm", "pnpm", "yarn", "bun", "npx", "bunx", "node", "deno", "tsx", "make", "just", "uv", "uvx", "poetry",
    "pip", "pip3", "pipx", "python", "python3", "pytest", "tox", "nox", "ruff", "black", "isort", "flake8",
    "pylint", "mypy", "pyright", "go", "cargo", "rustup", "gradle", "mvn", "dotnet", "swift", "xcodebuild",
    "bazel", "cmake", "ctest", "docker", "docker-compose", "kubectl", "helm", "terraform", "git", "gh", "jest",
    "vitest", "mocha", "eslint", "prettier", "tsc", "biome", "turbo", "nx", "rake", "bundle", "rspec", "rails",
    "composer", "phpunit", "mix", "sbt", "bash", "sh", "zsh", "pre-commit", "golangci-lint", "shellcheck",
    "hatch", "pdm", "pipenv", "conda", "brew", "sudo", "curl", "wget", "rm", "cd", "source", "vercel",
    "netlify", "firebase", "twine", "aws", "gcloud", "az", "flutter", "dart", "gofmt", "rubocop",
}
STANDALONE = {"pytest", "tox", "nox", "jest", "vitest", "mocha", "mypy", "pyright", "tsc", "rspec", "phpunit", "ctest"}
SHELL_KEYWORDS = {"if", "then", "else", "elif", "fi", "for", "while", "until", "do", "done", "case", "esac",
                  "function", "select", "{", "}"}
SETUP_WORDS = {"export", "set", "unset", "alias", "echo", "printf"}
SHELL_BUILTINS = {"cd", "echo", "export", "set", "unset", "alias", "source", ".", "true", "false", "test", "[",
                  "exit", "pwd", "read", "printf", "type", "command", "builtin", "eval", "exec", "wait", "trap",
                  "ulimit", "umask", "shift", "local", "return", ":"}
# Read-only filters allowed after a pipe (and anywhere in a script body). sort and uniq can write files.
HARMLESS = {"true", ":", "echo", "head", "tail", "grep", "egrep", "fgrep", "rg", "wc", "cut", "tr", "cat", "jq",
            "column"}
BODY_BUILTINS = {"true", ":", "exit"}
# Read-only programs a script body may call, for example inside a Makefile $(shell ...).
READ_ONLY = {"date", "uname", "pwd", "whoami", "hostname", "basename", "dirname", "printf", "ls", "which", "test", "["}
GIT_READ_ONLY = {"describe", "rev-parse", "log", "status", "diff", "show", "ls-files"}
NETWORK_HEADS = {"curl", "wget", "ssh", "scp", "sftp", "ftp", "nc", "ncat", "telnet", "http", "https"}
GENERATED_DIRS = {".venv", "venv", "node_modules", "dist", "build", "target", ".tox", ".nox", "out",
                  "coverage", "__pycache__", ".next"}
# Variables that change which program runs, or what it runs with.
BLOCKED_VARIABLES = {"GOFLAGS", "NODE_OPTIONS", "PYTEST_ADDOPTS", "PATH", "LD_PRELOAD", "LD_LIBRARY_PATH",
                     "DYLD_INSERT_LIBRARIES", "DYLD_LIBRARY_PATH", "BASH_ENV", "ENV", "SHELLOPTS", "BASHOPTS", "PS4",
                     "CDPATH", "RIPGREP_CONFIG_PATH", "MAKEFLAGS", "MFLAGS", "GNUMAKEFLAGS", "MAKEFILES",
                     "MAKEOVERRIDES"}
BLOCKED_PREFIXES = ("NPM_CONFIG_", "GIT_CONFIG", "GIT_TRACE", "JUST_", "YARN_")

PROMPT_RE = re.compile(r"^\s*[$%]\s+")
HEREDOC_RE = re.compile(r"<<-?\s*(['\"]?)([A-Za-z_][A-Za-z0-9_]*)\1")
ASSIGN_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=")
PYTHON_RE = re.compile(r"^python(\d+(\.\d+)?)?$")
TEMPLATE_RE = re.compile(r"<[A-Za-z][\w .:/|-]*>")
NEGATION_RE = re.compile(
    r"\b(never|don't|dont|do not|not|avoid|instead of|rather than|no longer|shouldn't|should not|must not|"
    r"mustn't|cannot|can't)\b((?:\W+\w+){0,3})\W*$", re.I)
NEGATION_EXCEPTIONS = re.compile(r"\b(forget|skip|without|before|until|unless|only)\b", re.I)
AFTER_NEGATION_RE = re.compile(r"^\W*(?:is|are)?\s*(deprecated|forbidden|banned|not allowed|no longer)\b", re.I)

GIT_VALUE_OPTIONS = {"-c", "-C", "--git-dir", "--work-tree", "--namespace", "--exec-path", "--config-env"}
OPERATORS = {"&&", "||", ";", "|", "&", "|&", ";;"}
REDIRECTS = {">", ">>", "<", ">&", "&>", "<&", ">|", "<<", "<<<", "&>>", "<>", "<<-"}
WRITE_REDIRECTS = {">", ">>", "&>", ">|", "&>>"}

NETWORK_RES = [  # a download piped into a shell or interpreter
    re.compile(r"\b(curl|wget)\b[^|]*\|\s*(sudo\s+)?(ba|z|da|k|fi)?sh\b"),
    re.compile(r"\b(curl|wget)\b[^|]*\|\s*(sudo\s+)?(python[\d.]*|node|perl|ruby|php)\b"),
    re.compile(r"<\(\s*(curl|wget)\b"),
    re.compile(r"(\$\(|`)\s*(curl|wget)\b"),
    re.compile(r"(?i)\b(iwr|irm|invoke-webrequest|invoke-restmethod)\b.*\|\s*iex\b"),
]
NEVER_RES = [
    ("privileged", "runs with elevated rights", [
        r"^(sudo|doas|su|pkexec|runas)\b", r"(^|\s)-exec(=|\s+)['\"]?(sudo|doas|su)\b"]),
    ("push", "pushes to a git remote", [r"^git\s+push\b", r"^(jj|hg|sl)\s+(git\s+)?push\b"]),
    ("publish", "publishes a package or release", [
        r"^(npm|pnpm|yarn|bun)\s+(npm\s+)?publish\b", r"^twine\s+upload\b",
        r"^(cargo|poetry|uv|hatch|flit|pdm)\s+publish\b", r"^gem\s+push\b", r"^docker\s+push\b",
        r"^gh\s+release\b", r"^mvn\b.*\bdeploy\b", r"^gradle\b.*\bpublish", r"^dotnet\s+nuget\s+push\b",
        r"^(vsce|ovsx)\s+publish\b"]),
    ("deploy", "deploys or changes cloud resources", [
        r"^vercel(\s+(deploy|promote|--prod)\b|$)",
        r"^(netlify|fly|flyctl|firebase|wrangler|serverless|sls|eb|amplify)\s+(deploy|publish|push)\b",
        r"^railway\s+up\b", r"^(surge|heroku)\b", r"^(terraform|tofu)\s+(apply|destroy|import|taint|state)\b",
        r"^pulumi\s+(up|destroy|refresh|import)\b",
        r"^kubectl\s+(apply|create|delete|replace|patch|scale|rollout|edit|drain|set)\b",
        r"^helm\s+(install|upgrade|uninstall|delete|rollback)\b", r"^(cdk|sam)\s+(deploy|destroy)\b",
        r"^(aws|gcloud|az|doctl|oci)\b"]),
    ("delete", "deletes files or data", [
        r"^(rm|rmdir|unlink|shred|srm|trash|rimraf|del-cli)\b", r"^find\b.*\s-delete\b",
        r"^find\b.*\s-exec(dir)?\s+rm\b", r"^rsync\b.*\s--delete",
        r"^git\s+(clean|reset\s+--hard|checkout\s+--|restore|branch\s+-D|stash\s+(drop|clear)|rm)\b",
        r"^docker\s+(rm|rmi|system\s+prune|volume\s+(rm|prune)|image\s+prune|container\s+prune|builder\s+prune)\b",
        r"^docker[- ]compose\b.*\bdown\b.*\s(-v|--volumes)\b", r"^xargs\b.*\brm\b", r"^(dropdb|dropuser|truncate)\b",
        r"(?i)\bdrop\s+(table|database|schema)\b"]),
    ("install", "installs packages", [
        r"^(npm|pnpm|yarn|bun)\s+(install|i|ci|add|update|up|upgrade|remove|rm|uninstall|un|link|dedupe|prune|rebuild)\b",
        r"^(yarn|bundle)$", r"^(pip|pip3|pipx)\s+(install|uninstall|download)\b",
        r"^python\s+-m\s+pip\s+(install|uninstall)\b",
        r"^uv\s+(sync|add|remove|lock|venv|pip\s+(install|uninstall|sync)|tool\s+(install|upgrade)|python\s+install)\b",
        r"^poetry\s+(install|add|remove|update|lock|self)\b", r"^pipenv\s+(install|uninstall|sync|update|lock)\b",
        r"^(brew|apt|apt-get|yum|dnf|apk|pacman|port|snap|choco|winget|scoop)\s+(install|upgrade|update|remove|uninstall|reinstall|tap)\b",
        r"^gem\s+(install|update|uninstall)\b", r"^bundle\s+(install|update)\b",
        r"^cargo\s+(install|add|remove|update)\b", r"^go\s+(install|get|mod\s+(download|tidy|vendor))\b",
        r"^composer\s+(install|update|require|remove)\b",
        r"^(conda|mamba|micromamba)\s+(install|create|update|remove|env\s+create)\b",
        r"^(mise|asdf|nvm|fnm|volta|pyenv|rbenv|rustup)\s+(install|uninstall|update)\b",
        r"^pre-commit\s+install\b", r"^mvn\b.*\binstall\b", r"^playwright\s+install\b"]),
]
NEVER_RES = [(kind, reason, [re.compile(p) for p in patterns]) for kind, reason, patterns in NEVER_RES]
NAME_KINDS = [
    ("publish", re.compile(r"(^|[:_.-])publish([:_.-]|$)")),
    ("push", re.compile(r"(^|[:_.-])push([:_.-]|$)")),
    ("deploy", re.compile(r"(^|[:_.-])(deploy|release|ship)([:_.-]|$)")),
    ("delete", re.compile(r"(^|[:_.-])(clean|distclean|clobber|purge|nuke|reset)([:_.-]|$)")),
    ("install", re.compile(r"^(install|setup|bootstrap|deps|init)([:_.-]|$)")),
    ("modify", re.compile(r"(^|[:_.-])(fix|format|fmt|write|codegen|generate|gen|migrate|seed|update|upgrade|bump|snapshot)([:_.-]|$)")),
    ("test", re.compile(r"^(test|tests|unit|unittest|integration|e2e|spec|check|verify)([:_.-][\w:.-]*)?$")),
    ("lint", re.compile(r"^(lint|linter|eslint|ruff|flake8|pylint|stylelint)([:_.-][\w:.-]*)?$")),
    ("typecheck", re.compile(r"^(typecheck|type-check|types|check-types|tsc|mypy|pyright)([:_.-][\w:.-]*)?$")),
    ("build", re.compile(r"^(build|compile|bundle|all)([:_.-][\w:.-]*)?$")),
]
NEVER_REASONS = {kind: reason for kind, reason, _patterns in NEVER_RES}
MODIFY_RES = [re.compile(p) for p in (
    r"(^|\s)--(fix|fix-only|write|apply|update|update-snapshots?|updateSnapshot|test-update-snapshots|basetemp)(\s|=|$)",
    r"^ruff\s+format\b(?!.*--(check|diff))", r"^black\b(?!.*--(check|diff))",
    r"^isort\b(?!.*--(check|check-only|diff))", r"^(cargo\s+fmt|go\s+fmt|rustfmt|clang-format|swift-format|terraform\s+fmt)\b(?!.*(--check|-check))",
    r"^dotnet\s+format\b(?!.*--verify-no-changes)", r"^mix\s+format\b(?!.*--check-formatted)",
    r"^gofmt\b.*\s-w\b", r"^prettier\b.*\s-w\b", r"^rubocop\b.*\s(-a|-A|--autocorrect|--auto-correct)\b",
    r"^codespell\b.*\s-w\b", r"^ktlint\b.*\s-F\b", r"^(jest|vitest|bun\s+test)\b.*\s(-u|--update)\b",
    r"^pre-commit\s+run\b",
    r"^git\s+(commit|merge|rebase|tag|stash|checkout|switch|cherry-pick|revert|am|apply|add|mv|init|pull|fetch|"
    r"clone|submodule|reset|worktree|config)\b",
)]
WATCH_RE = re.compile(r"(^|\s)(--watch|--watchAll|--serve)(\s|=|$)|^(tsc|rollup|webpack)\b.*\s-w\b|^vitest\s+watch\b|^nodemon\b")
DOWNLOADERS = {("npx", ""), ("bunx", ""), ("uvx", ""), ("pnpm", "dlx"), ("yarn", "dlx"), ("npm", "exec"),
               ("pipx", "run"), ("bun", "x")}
HEAD_KINDS = {
    "pytest": "test", "py.test": "test", "jest": "test", "vitest": "test", "mocha": "test", "ava": "test",
    "rspec": "test", "phpunit": "test", "ctest": "test", "bats": "test",
    "flake8": "lint", "pylint": "lint", "eslint": "lint", "stylelint": "lint", "markdownlint": "lint",
    "markdownlint-cli2": "lint", "shellcheck": "lint", "hadolint": "lint", "yamllint": "lint",
    "actionlint": "lint", "rubocop": "lint", "swiftlint": "lint", "ktlint": "lint", "tflint": "lint",
    "vale": "lint", "codespell": "lint", "standard": "lint", "xo": "lint", "oxlint": "lint", "black": "lint",
    "isort": "lint", "prettier": "lint", "gofmt": "lint",
    "mypy": "typecheck", "pyright": "typecheck", "basedpyright": "typecheck", "pytype": "typecheck",
    "vue-tsc": "typecheck", "webpack": "build", "rollup": "build", "esbuild": "build", "ninja": "build",
}
SUBCOMMAND_KINDS = {
    "go": {"test": "test", "vet": "typecheck", "build": "build"},
    "cargo": {"test": "test", "nextest": "test", "check": "typecheck", "clippy": "lint", "build": "build",
              "doc": "build", "fmt": "lint"},
    "swift": {"test": "test", "build": "build"},
    "dotnet": {"test": "test", "build": "build", "format": "lint"},
    "deno": {"test": "test", "lint": "lint", "check": "typecheck", "fmt": "lint"},
    "bun": {"test": "test"},
    "mix": {"test": "test", "compile": "build", "credo": "lint", "dialyzer": "typecheck", "format": "lint"},
    "sbt": {"test": "test", "compile": "build"},
    "stack": {"test": "test", "build": "build"},
    "cabal": {"test": "test", "build": "build"},
    "flutter": {"test": "test", "analyze": "lint"},
    "dart": {"test": "test", "analyze": "lint"},
    "mvn": {"test": "test", "verify": "test", "compile": "build", "package": "build"},
    "gradle": {"test": "test", "check": "test", "build": "build", "assemble": "build"},
    "bazel": {"test": "test", "build": "build"},
    "zig": {"build": "build"},
    "ruff": {"check": "lint", "format": "lint"},
    "biome": {"check": "lint", "lint": "lint", "ci": "lint", "format": "lint"},
    "golangci-lint": {"run": "lint"},
    "uv": {"build": "build"},
    "poetry": {"build": "build", "check": "lint"},
    "hatch": {"build": "build"},
    "vite": {"build": "build"},
    "next": {"build": "build", "lint": "lint"},
    "nuxt": {"build": "build"},
    "astro": {"build": "build", "check": "typecheck"},
    "cmake": {"--build": "build"},
    "ty": {"check": "typecheck"},
    "node": {"--test": "test"},
    "terraform": {"validate": "lint", "fmt": "lint"},
    "playwright": {"test": "test"},
}
KIND_ORDER = {"test": 0, "typecheck": 1, "lint": 2, "build": 3, "help": 4, "version": 5}
JS_TOOLS = ("npm", "pnpm", "yarn", "bun")
WORKSPACE_FLAGS = {"--prefix", "-C", "--dir", "--workspace", "-w", "--workspaces", "-ws", "--filter", "-F",
                   "-r", "--recursive", "--workspace-root", "--cwd", "--include-workspace-root"}
JS_VALUE_FLAGS = {"--prefix", "-C", "--dir", "--workspace", "-w", "--filter", "-F", "--loglevel", "--registry",
                  "--cache", "--userconfig", "--cwd"}
PNPM_BUILTINS = {"add", "install", "i", "update", "up", "remove", "rm", "un", "uninstall", "link", "ln", "unlink",
                 "import", "rebuild", "rb", "prune", "fetch", "patch", "patch-commit", "audit", "list", "ls", "ll",
                 "outdated", "why", "licenses", "exec", "dlx", "create", "init", "publish", "pack", "config", "store",
                 "root", "bin", "env", "setup", "server", "doctor", "recursive", "run", "test", "t", "start",
                 "deploy", "help", "self-update", "approve-builds", "ignored-builds"}
YARN_BUILTINS = {"add", "audit", "autoclean", "bin", "cache", "check", "config", "create", "dedupe", "dlx", "exec",
                 "explain", "generate-lock-entry", "global", "help", "import", "info", "init", "install", "licenses",
                 "link", "list", "login", "logout", "node", "npm", "outdated", "owner", "pack", "patch", "plugin",
                 "policies", "publish", "rebuild", "remove", "run", "search", "set", "tag", "team", "test", "unlink",
                 "unplug", "up", "upgrade", "upgrade-interactive", "version", "versions", "why", "workspace",
                 "workspaces", "start"}
BUN_BUILTINS = {"run", "test", "x", "repl", "exec", "install", "i", "add", "a", "remove", "rm", "update",
                "outdated", "link", "unlink", "pm", "build", "init", "create", "c", "upgrade", "publish", "patch",
                "audit", "info", "completions", "help"}
UV_VALUE_FLAGS = {"--with", "--with-editable", "--with-requirements", "--python", "-p", "--extra", "--group",
                  "--only-group", "--package", "--directory", "--project", "--env-file", "--index", "--index-url",
                  "--default-index"}
MAKE_VALUE_FLAGS = {"-C", "--directory", "-f", "--file", "--makefile"}
MAKE_PLAIN_FLAGS = {"-k", "--keep-going", "-s", "--silent", "--quiet", "-B", "--always-make", "-w",
                    "--print-directory", "--no-print-directory", "-r", "--no-builtin-rules", "-R",
                    "--no-builtin-variables", "-j", "--jobs"}
MAKE_DEFAULTS = {"MAKE": "make", "RM": "rm -f", "CC": "cc", "CXX": "c++", "AR": "ar", "SHELL": "/bin/sh",
                 "CURDIR": ".", "MAKEFLAGS": ""}
PYTEST_VALUE_OPTS = {"-k", "-m", "-c", "-o", "-p", "-n", "-W", "-r", "--rootdir", "--junitxml", "--junit-xml",
                     "--cov", "--cov-report", "--cov-config", "--maxfail", "--deselect", "--ignore", "--ignore-glob",
                     "--tb", "--basetemp", "--log-level", "--durations", "--confcutdir", "--timeout", "--capture",
                     "--import-mode", "--dist", "--numprocesses", "--reruns", "--color", "--code-highlight"}
SCRIPT_VALUE_FLAGS = {"python": {"-W", "-X"}, "node": {"-r", "--require", "--import", "--loader",
                                                     "--experimental-loader", "--env-file", "--conditions", "-C"}}
SCRIPT_STOP_FLAGS = {"python": {"-m", "-c"}, "node": {"-e", "--eval", "-p", "--print", "--test", "--run"},
                     "bash": {"-c"}, "sh": {"-c"}, "zsh": {"-c"}, "ruby": {"-e"}, "perl": {"-e", "-E"},
                     "php": {"-r"}, "ts-node": {"-e"}, "tsx": {"-e", "--eval"}}
REPO_SCRIPT_RUNNERS = ("node", "python", "tsx")
# Audit of the helper lists (see references/command-checks.md): the forms below write files, run programs,
# or change machine state, so they are held back even though the program itself is on a read-only list.
OUTPUT_FLAG_RE = re.compile(r"^--(output|out|output-file|outfile|output-directory)(=|$)")
GIT_WRITE_FLAGS = {"--output", "-o", "--output-directory"}
GIT_EXEC_FLAGS = {"--ext-diff", "--textconv", "--show-signature"}
GIT_RISKY_CONFIG_RE = re.compile(
    r"^(core\.fsmonitor|diff\.external|diff\..+\.(command|textconv)|filter\..+\.(clean|smudge|process)|"
    r"gpg(\..+)?\.program|include\.path|includeif\..+\.path)$", re.I)
GIT_VARIABLES = {"GIT_EXTERNAL_DIFF", "GIT_DIFF_OPTS", "GIT_PAGER", "GIT_DIR", "GIT_WORK_TREE", "GIT_EXEC_PATH",
                 "GIT_SSH", "GIT_SSH_COMMAND", "GIT_ASKPASS", "GIT_EDITOR", "GIT_SEQUENCE_EDITOR", "GIT_PROXY_COMMAND",
                 "GIT_INDEX_FILE", "GIT_OBJECT_DIRECTORY", "GIT_ALTERNATE_OBJECT_DIRECTORIES", "GIT_NAMESPACE",
                 "GIT_CEILING_DIRECTORIES"}
DATE_READ_FLAGS = {"-u", "--utc", "--universal", "-R", "--rfc-2822", "--rfc-email", "-j"}
HOSTNAME_READ_FLAGS = {"-s", "--short", "-f", "--fqdn", "--long", "-d", "--domain", "-i", "--ip-address", "-I",
                       "--all-ip-addresses", "-A", "--all-fqdns", "-a", "--alias"}
RG_EXEC_FLAGS = {"--pre", "--hostname-bin"}
MAKE_INCLUDE_REASON = "the Makefile includes other files or uses pattern rules this checker did not read"
MAKE_SPECIAL_VARIABLES = {"SHELL": "changes how recipes run", ".SHELLFLAGS": "changes how recipes run",
                          ".RECIPEPREFIX": "changes how recipes run",
                          "VPATH": "lets make find files this checker did not check",
                          ".EXTRA_PREREQS": "adds prerequisites this checker did not read"}
MAKE_FLAG_VARIABLES = {"MAKEFLAGS", "MFLAGS", "GNUMAKEFLAGS"}
SAFE_MAKE_FLAG_RE = re.compile(r"^(-?[BikrRsSw]+|-j\d*|--jobs(=\d+)?|-O\w*|--output-sync(=\w+)?|--(silent|quiet|"
                               r"no-print-directory|print-directory|keep-going|no-builtin-rules|no-builtin-variables|"
                               r"warn-undefined-variables|always-make|ignore-errors))$")
# make's built-in suffix list: a target named .c.o or .sh is a suffix rule (an old-style pattern rule).
MAKE_SUFFIXES = {".out", ".a", ".ln", ".o", ".c", ".cc", ".C", ".cpp", ".p", ".f", ".F", ".m", ".r", ".y", ".l",
                 ".ym", ".yl", ".s", ".S", ".mod", ".sym", ".def", ".h", ".info", ".dvi", ".tex", ".texinfo",
                 ".texi", ".txinfo", ".w", ".ch", ".web", ".sh", ".elc", ".el"}
MAKE_MODIFIERS = r"(?:(?:export|override|private|unexport)\s+)*"
MAKE_DIRECTIVE_RE = re.compile(r"^" + MAKE_MODIFIERS + r"(ifeq|ifneq|ifdef|ifndef|define|undefine|vpath)(\s|\(|$)")
MAKE_PARSE_TIME = (("file", "uses $(file ...), which writes files while make reads the Makefile"),
                   ("guile", "uses $(guile ...), which runs code while make reads the Makefile"),
                   ("eval", "uses $(eval ...), which defines rules this checker did not read"))
SAFE_JUST_SETTINGS = {"quiet", "ignore-comments", "positional-arguments", "allow-duplicate-recipes",
                      "allow-duplicate-variables"}
SAFE_JUST_ATTRIBUTES = {"private", "doc", "group", "confirm", "no-exit-message", "exit-message", "no-quiet",
                        "linux", "macos", "unix", "windows", "openbsd", "freebsd", "netbsd", "dragonfly", "android",
                        "ios", "positional-arguments", "default", "metadata", "parallel"}
JUST_VALUE = r"""'[^']*'|"(?:[^"\\]|\\.)*"|`[^`]*`|\([^()]*\)|[A-Za-z_][\w-]*"""  # a parameter default
JUST_PARAM_RE = re.compile(r"([+*]?)(\$?)([A-Za-z_][\w-]*)(?:\s*=\s*(" + JUST_VALUE + r"))?")
JUST_RECIPE_RE = re.compile(r"^@?([A-Za-z_][\w-]*)((?:\s+[+*]?\$?[A-Za-z_][\w-]*(?:\s*=\s*(?:" + JUST_VALUE
                            + r"))?)*)\s*:(?!=)(.*)$")
NPMRC_RISKY_KEYS = {"script-shell", "node-options", "onload-script"}
DEVICE_PATHS = {"/dev/null", "/dev/stdout", "/dev/stderr"}


# ------------------------------------------------------------- extraction


def strip_comment(text):
    quote = None
    for index, char in enumerate(text):
        if quote:
            if char == quote:
                quote = None
            continue
        if char in "'\"":
            quote = char
        elif char == "#" and (index == 0 or text[index - 1].isspace()):
            return text[:index].rstrip()
    return text


def words_after_assignments(text):
    tokens = text.split()
    while tokens and ASSIGN_RE.match(tokens[0]):
        tokens.pop(0)
    return tokens


def command_like(text):
    tokens = words_after_assignments(text)
    if not tokens:
        return False
    first = tokens[0]
    if first.startswith("./") and len(first) > 2:
        return True
    if first in COMMAND_WORDS or PYTHON_RE.match(first):
        return len(tokens) >= 2 or first in STANDALONE
    return False


def worth_keeping(command):
    tokens = words_after_assignments(command)
    return bool(tokens) and tokens[0] not in SHELL_KEYWORDS and tokens[0] not in SETUP_WORDS


def block_commands(lines, console):
    """Commands in one fenced block; lines are (line_number, text). A console block keeps only prompt lines."""
    found = []
    pending = None
    heredoc_end = None
    for number, raw in lines:
        if heredoc_end is not None:
            if raw.strip() == heredoc_end:
                heredoc_end = None
            continue
        text = raw.rstrip()
        if pending is None:
            prompt = PROMPT_RE.match(text)
            if console and not prompt:
                continue
            if prompt:
                text = text[prompt.end():]
            text = text.strip()
            if not text or text.startswith("#"):
                continue
            start = number
        else:
            start, previous = pending
            text = previous + " " + text.strip()
        if text.endswith("\\"):
            pending = (start, text[:-1].rstrip())
            continue
        pending = None
        heredoc = HEREDOC_RE.search(text)
        if heredoc:
            heredoc_end = heredoc.group(2)
        command = strip_comment(text).strip()
        if command and worth_keeping(command):
            found.append((start, command))
    return found


def negated(line, start, end):
    before = re.split(r"[.!?;:,]\s", line[:start])[-1]
    before = load_map.INLINE_CODE_RE.sub("x", before)
    match = NEGATION_RE.search(before)
    if match and not NEGATION_EXCEPTIONS.search(match.group(0)):
        return True
    return bool(AFTER_NEGATION_RE.match(line[end:]))


def extract_commands(text):
    """Commands in markdown: shell blocks, unlabeled blocks made only of commands, and command-like inline code."""
    found = []
    block = None
    for number, line, kind, lang in load_map.scan_markdown(text or ""):
        if kind == "fence":
            if block is None:
                block = (lang, [])
            else:
                block_lang, lines = block
                prompts = any(PROMPT_RE.match(l) for _n, l in lines)
                if block_lang in SHELL_LANGS or block_lang in CONSOLE_LANGS:
                    console = block_lang in CONSOLE_LANGS or prompts
                    for start, command in block_commands(lines, console):
                        found.append({"command": command, "line": start, "source": "block", "negated": False})
                elif block_lang in PLAIN_LANGS:
                    commands = block_commands(lines, False)
                    content = [l for _n, l in lines if l.strip() and not l.strip().startswith("#")]
                    if commands and all(command_like(PROMPT_RE.sub("", l)) for l in content):
                        for start, command in commands:
                            found.append({"command": command, "line": start, "source": "block", "negated": False})
                block = None
            continue
        if kind == "code" and block is not None:
            block[1].append((number, line))
            continue
        if kind != "prose":
            continue
        for match in load_map.INLINE_CODE_RE.finditer(line):
            span = PROMPT_RE.sub("", match.group(2).strip())
            if command_like(span):
                found.append({"command": span, "line": number, "source": "inline",
                              "negated": negated(line, match.start(), match.end())})
    return found


# --------------------------------------------------------------- parsing


def has_placeholder(text):
    return bool(TEMPLATE_RE.search(text)) or "path/to" in text or "..." in text.split()


def is_template(token):
    return token == "..." or "path/to" in token or bool(TEMPLATE_RE.search(token))


def parse(command):
    """Split a command line into simple commands: [{"argv", "redirects"}], or None when it cannot be parsed."""
    try:
        lexer = shlex.shlex(command, posix=True, punctuation_chars=True)
        lexer.whitespace_split = True
        tokens = list(lexer)
    except ValueError:
        return None
    segments = [{"argv": [], "redirects": []}]
    index = 0
    while index < len(tokens):
        token = tokens[index]
        if token in OPERATORS:
            segments.append({"argv": [], "redirects": []})
        elif token in REDIRECTS or (token.isdigit() and index + 1 < len(tokens) and tokens[index + 1] in REDIRECTS):
            if token.isdigit():
                index += 1
                token = tokens[index]
            target = tokens[index + 1] if index + 1 < len(tokens) else ""
            segments[-1]["redirects"].append((token, target))
            index += 1
        elif token not in ("(", ")", "<("):
            segments[-1]["argv"].append(token)
        index += 1
    return [s for s in segments if s["argv"] or s["redirects"]]


def split_words(text):
    try:
        return shlex.split(text)
    except ValueError:
        return text.split()


def program_name(token):
    name = os.path.basename(token)
    if PYTHON_RE.match(name):
        return "python"
    return {"gradlew": "gradle", "mvnw": "mvn"}.get(name, name)


def assignments_of(argv):
    """NAME=value words that apply to the program: leading ones and those after env or cross-env."""
    found = []
    words = list(argv)
    while words:
        if ASSIGN_RE.match(words[0]):
            found.append(words.pop(0))
        elif words[0] in ("env", "cross-env", "cross-env-shell"):
            words.pop(0)
            while words and (ASSIGN_RE.match(words[0]) or words[0].startswith("-")):
                if ASSIGN_RE.match(words[0]):
                    found.append(words[0])
                del words[:2 if words[0] in ("-u", "--unset") else 1]
        else:
            break
    return [tuple(word.split("=", 1)) for word in found]


def blocked_variable(name):
    upper = name.upper()
    return (upper in BLOCKED_VARIABLES or upper in GIT_VARIABLES or upper.endswith("_RUNNER")
            or upper.endswith("_WRAPPER") or upper.startswith(BLOCKED_PREFIXES))


def strip_wrappers(argv):
    """Drop leading VAR=value assignments and wrappers such as env, timeout, nice, and time."""
    argv = list(argv)
    while argv:
        if ASSIGN_RE.match(argv[0]):
            argv.pop(0)
            continue
        head = argv[0]
        if head in ("time", "nohup", "command", "builtin", "exec", "noglob", "caffeinate"):
            argv.pop(0)
        elif head == "timeout":
            argv.pop(0)
            while argv and argv[0].startswith("-"):
                del argv[:2 if argv[0] in ("-s", "-k", "--signal", "--kill-after") else 1]
            if argv:
                argv.pop(0)  # the duration
        elif head == "nice":
            argv.pop(0)
            if argv and argv[0] == "-n":
                del argv[:2]
            elif argv and re.match(r"^-\d+$", argv[0]):
                argv.pop(0)
        elif head in ("env", "cross-env", "cross-env-shell"):
            argv.pop(0)
            while argv and (ASSIGN_RE.match(argv[0]) or argv[0].startswith("-")):
                del argv[:2 if argv[0] in ("-u", "--unset") else 1]
        elif head in ("stdbuf", "dotenv", "env-cmd"):
            argv.pop(0)
            while argv and argv[0].startswith("-"):
                done = argv.pop(0) == "--"
                if done:
                    break
        else:
            break
    return argv


def verdict(kind, safety, reason=""):
    return {"kind": kind, "safety": safety, "reason": reason}


def joined_command(argv):
    """The command as one string, with the program's base name and git's global options removed."""
    name = program_name(argv[0])
    args = list(argv[1:])
    if name == "git":
        while args and args[0].startswith("-"):
            flag = args.pop(0)
            if flag in GIT_VALUE_OPTIONS and args:
                args.pop(0)
    return " ".join([name] + args)


def never_kind(argv):
    joined = joined_command(argv)
    for kind, reason, patterns in NEVER_RES:
        if any(p.search(joined) for p in patterns):
            return kind, reason
    return None


def never_anywhere(argv):
    """never_kind at every word, so wrapped forms (bash -c '...', npx rimraf, xargs rm) are found too."""
    for start in range(len(argv)):
        danger = never_kind(argv[start:])
        if danger:
            return danger
    return None


def name_kind(name):
    lowered = name.lower()
    for kind, pattern in NAME_KINDS:
        if pattern.search(lowered):
            return kind
    return None


def within(path, root):
    return load_map.within(path, root)


def rel(path, repo):
    return os.path.relpath(path, repo).replace(os.sep, "/")


def generated(path, repo):
    parts = rel(path, repo).split("/")
    return bool(parts) and parts[0] in GENERATED_DIRS


def existing_file(directory, names):
    """The first of names that exists in directory, spelled as it is on disk (file systems may ignore case)."""
    try:
        entries = os.listdir(directory)
    except OSError:
        return None
    lowered = {entry.lower(): entry for entry in entries}
    for name in names:
        actual = name if name in entries else lowered.get(name.lower())
        if actual and os.path.isfile(os.path.join(directory, actual)):
            return os.path.join(directory, actual)
    return None


def find_up(start, repo, names):
    """First of names found in start or a parent folder, stopping at the repo root (no limit when repo is None)."""
    names = (names,) if isinstance(names, str) else names
    current = os.path.abspath(start)
    stop = os.path.abspath(repo) if repo else None
    while True:
        path = existing_file(current, names)
        if path:
            return path
        parent = os.path.dirname(current)
        if parent == current or (stop and (current == stop or not current.startswith(stop + os.sep))):
            return None
        current = parent


def package_scripts(path):
    try:
        with open(path, "r", encoding="utf-8") as handle:
            data = json.load(handle)
    except (OSError, ValueError):
        return {}
    scripts = data.get("scripts") if isinstance(data, dict) else None
    return {k: v for k, v in scripts.items() if isinstance(v, str)} if isinstance(scripts, dict) else {}


def js_script(argv):
    """(tool, script name or None, subcommand, uses a workspace flag) for npm, pnpm, yarn, and bun."""
    tool = program_name(argv[0])
    rest = argv[1:]
    workspace = False
    index = 0
    while index < len(rest) and rest[index].startswith("-"):
        flag = rest[index].split("=", 1)[0]
        workspace = workspace or flag in WORKSPACE_FLAGS
        index += 2 if flag in JS_VALUE_FLAGS and "=" not in rest[index] else 1
    if index >= len(rest):
        return tool, None, "", workspace
    sub = rest[index]
    args = rest[index + 1:]
    workspace = workspace or any(a.split("=", 1)[0] in WORKSPACE_FLAGS for a in args if a != "--")
    positional = [a for a in args if not a.startswith("-")]
    if sub in ("run", "run-script", "rum", "urn"):
        return tool, (positional[0] if positional else None), "run", workspace
    if tool == "npm" and sub in ("test", "t", "tst"):
        return tool, "test", "test", workspace
    if tool == "npm" and sub in ("start", "stop", "restart"):
        return tool, sub, sub, workspace
    if tool == "pnpm" and sub in ("test", "t", "start"):
        return tool, "test" if sub in ("test", "t") else "start", sub, workspace
    if tool == "yarn" and sub in ("test", "start"):
        return tool, sub, sub, workspace
    builtins = {"pnpm": PNPM_BUILTINS, "yarn": YARN_BUILTINS, "bun": BUN_BUILTINS}.get(tool)
    if builtins is not None and sub not in builtins:
        return tool, sub, "implicit", workspace
    return tool, None, sub, workspace


def script_arguments(argv, name):
    """Arguments given after the script name; npm passes them to the script (a lone -- is dropped)."""
    tail = argv[1:]
    for index, token in enumerate(tail):
        if token == name or (name == "test" and token in ("t", "tst")):
            return [a for a in tail[index + 1:] if a != "--"]
    return []


def make_parts(argv):
    """(folder, makefile, targets, variable overrides, flags this checker does not read) for a make command."""
    directory, makefile, targets, overrides, unknown = None, None, [], {}, []
    rest = argv[1:]
    index = 0
    while index < len(rest):
        arg = rest[index]
        if not arg.startswith("-") and ASSIGN_RE.match(arg):
            name, value = arg.split("=", 1)
            overrides[name] = value
        elif arg.startswith("--") and "=" in arg:
            flag, value = arg.split("=", 1)
            if flag == "--directory":
                directory = value
            elif flag in ("--file", "--makefile"):
                makefile = value
            elif flag not in ("--jobs", "--load-average"):
                unknown.append(flag)
        elif arg in MAKE_VALUE_FLAGS and (directory if arg in ("-C", "--directory") else makefile) is not None:
            unknown.append(arg)  # a second -C or -f: make reads both, this checker reads one
            index += 1
        elif arg in MAKE_VALUE_FLAGS:
            value = rest[index + 1] if index + 1 < len(rest) else None
            if arg in ("-C", "--directory"):
                directory = value
            else:
                makefile = value
            index += 1
        elif arg.startswith("-C") and len(arg) > 2:
            directory = arg[2:]
        elif arg.startswith("-f") and len(arg) > 2:
            makefile = arg[2:]
        elif arg in ("-j", "--jobs", "-l") and index + 1 < len(rest) and rest[index + 1].isdigit():
            index += 1
        elif re.match(r"^-j\d+$", arg) or arg in MAKE_PLAIN_FLAGS:
            pass
        elif arg.startswith("-"):
            unknown.append(arg)
        else:
            targets.append(arg)
        index += 1
    return directory, makefile, targets, overrides, unknown


TARGET_RE = re.compile(r"^([^\s:=#][^:=#]*?)\s*::?(?!=)\s*(.*)$")
MAKE_ASSIGN_RE = re.compile(r"^" + MAKE_MODIFIERS + r"([A-Za-z_.][\w.-]*)\s*(:::=|::=|:=|\?=|\+=|!=|=)\s*(.*)$")


def make_shell_calls(line):
    """Commands inside $(shell ...) or ${shell ...}; make runs them while reading the Makefile."""
    calls = []
    for match in re.finditer(r"\$[({]shell\s", line):
        opener = line[match.start() + 1]
        closer = ")" if opener == "(" else "}"
        depth, index = 1, match.end()
        while index < len(line) and depth:
            depth += 1 if line[index] == opener else -1 if line[index] == closer else 0
            index += 1
        calls.append(line[match.end():index - 1 if depth == 0 else index])
    return calls


def replace_shell_calls(text, marker):
    out, index = [], 0
    for match in re.finditer(r"\$[({]shell\s", text):
        if match.start() < index:
            continue
        opener = text[match.start() + 1]
        closer = ")" if opener == "(" else "}"
        depth, end = 1, match.end()
        while end < len(text) and depth:
            depth += 1 if text[end] == opener else -1 if text[end] == closer else 0
            end += 1
        out.append(text[index:match.start()] + marker)
        index = end
    return "".join(out) + text[index:]


def make_lines(text):
    """Lines as make reads them: a line that ends with a backslash continues on the next one."""
    lines = []
    for line in text.splitlines():
        if lines and lines[-1].endswith("\\"):
            lines[-1] = lines[-1][:-1].rstrip() + " " + (line[1:] if line.startswith("\t") else line).strip()
        else:
            lines.append(line)
    return lines


def parse_makefile(path):
    """Targets, variables, and anything that stops this checker from knowing what make runs ("unsafe")."""
    info = {"targets": {}, "includes": False, "dynamic": False, "default": None, "shell": [], "variables": {},
            "unsafe": [], "weak": set(), "exported": set(), "suffixes": set(MAKE_SUFFIXES)}
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as handle:
            lines = make_lines(handle.read())
    except OSError:
        return info
    unsafe, variables, targets = info["unsafe"], info["variables"], info["targets"]
    current = None
    for line in lines:
        info["shell"].extend(make_shell_calls(line))
        for function, reason in MAKE_PARSE_TIME:
            if re.search(r"\$[({]%s\s" % function, line) and reason not in unsafe:
                unsafe.append(reason)
        if line.startswith("\t") and current:
            for name in current:
                targets[name]["recipe"].append(line[1:].strip())
            continue
        stripped = line.strip()  # outside a rule, make reads a tab-indented line as an ordinary line
        if not stripped or stripped.startswith("#"):
            continue
        current = None
        directive = MAKE_DIRECTIVE_RE.match(stripped)
        if directive:
            unsafe.append("the Makefile uses %s, which this checker does not follow" % directive.group(1))
            continue
        if re.match(r"^-?(include|sinclude|load)\s", stripped):
            info["includes"] = True
            continue
        assigned = MAKE_ASSIGN_RE.match(stripped)
        if assigned:  # NAME = value, NAME := value, NAME ?= value, NAME += value, NAME != command
            name, op, value = assigned.groups()
            if "export" in stripped[:assigned.start(1)].split():
                info["exported"].add(name)
            if op in ("?=", "+=") and name not in variables:
                info["weak"].add(name)  # a value from the environment wins over ?= and is extended by +=
            elif op not in ("?=", "+="):
                info["weak"].discard(name)
            if op == "!=":
                info["shell"].append(value)
                variables[name] = "shell-output"
            elif op == "?=":
                variables.setdefault(name, value)
            elif op == "+=":
                variables[name] = (variables.get(name, "") + " " + value).strip()
            else:
                variables[name] = value
            continue
        export = re.match(r"^(export|unexport)(\s+(.*))?$", stripped)
        if export:  # a bare export passes every variable to the recipes
            names = (export.group(3) or "").split()
            if export.group(1) == "export":
                info["exported"].update(names or ["*"])
            else:
                info["exported"].difference_update(names)
            continue
        if re.match(r"^(else|endif|endef)(\s|$)", stripped):
            continue
        match = TARGET_RE.match(stripped)
        if not match:  # for example $(RULES) or $(NAME) = value: make expands the line, this checker cannot
            unsafe.append("the Makefile has a line this checker cannot read (%s)" % code(stripped, 60))
            continue
        names = match.group(1).split()
        target_variable = MAKE_ASSIGN_RE.match(match.group(2))
        if target_variable:  # test: NAME = value sets NAME while make builds test
            name, op, value = target_variable.groups()
            variables[name] = "$(%s)" % name  # a value this checker cannot place, so a recipe that uses it waits
            info["exported"].add(name)
            if op == "!=":
                info["shell"].append(value)
            continue
        parts = match.group(2).split(";", 1)
        prereqs = [p for p in parts[0].split() if p != "|"]
        if "%" in parts[0]:
            info["dynamic"] = True  # a static pattern rule
        current = names
        for name in names:
            if "%" in name or "$" in name:
                info["dynamic"] = True
            entry = targets.setdefault(name, {"prereqs": [], "recipe": []})
            entry["prereqs"].extend(prereqs)
            if len(parts) > 1 and parts[1].strip():
                entry["recipe"].append(parts[1].strip())
            if info["default"] is None and not name.startswith(".") and "%" not in name and "$" not in name:
                info["default"] = name
    info["suffixes"].update(targets.get(".SUFFIXES", {}).get("prereqs", []))
    for name in targets:
        suffixes = re.findall(r"\.[^.]+", name)
        if name == ".DEFAULT" or (re.match(r"^(\.[^.\s/]+){1,2}$", name)
                                  and all(suffix in info["suffixes"] for suffix in suffixes)):
            info["dynamic"] = True
    return info


def visit_targets(targets, goal):
    """Names make visits for goal, prerequisites first, however deep they go."""
    order, seen, stack = [], set(), [(goal, False)]
    while stack:
        name, done = stack.pop()
        if done:
            order.append(name)
        elif name not in seen:
            seen.add(name)
            stack.append((name, True))
            stack.extend((prereq, False) for prereq in reversed(targets.get(name, {}).get("prereqs", [])))
    return order


def builtin_sources(base, name, suffixes):
    """True when files next to name could feed one of make's built-in rules (x.c for x.o, RCS or SCCS copies)."""
    folder, leaf = os.path.split(os.path.normpath(os.path.join(base, name)))
    stem = leaf.rsplit(".", 1)[0] if "." in leaf.lstrip(".") else leaf
    try:
        entries = os.listdir(folder)
    except OSError:
        return False
    return any(entry != leaf and (entry in ("RCS", "SCCS") or entry in (leaf + ",v", "s." + leaf) or (
        entry.startswith(stem + ".") and os.path.splitext(entry)[1] in suffixes)) for entry in entries)


def builtin_rule_problem(names, info, base):
    """A reason when make may run a rule for one of names that is not in the Makefile, else None."""
    targets = info["targets"]
    phony = set(targets.get(".PHONY", {}).get("prereqs", []))
    for name in names:
        if "$" in name:
            return "uses a make variable or function this checker cannot resolve (%s)" % code(name, 60)
        target = targets.get(name)
        if name in phony or (target and target["recipe"]):
            continue
        if (target is None and not os.path.exists(os.path.join(base, name))) or builtin_sources(
                base, name, info["suffixes"]):
            return "make may build %s with a built-in rule this checker does not read" % code(name, 60)
    return None


def make_substitute(text, variables):
    """Expand $(NAME) and ${NAME} from the Makefile and the command line; $(shell ...) becomes a marker."""
    text = re.sub(r"\$\([@<^?*+|][DF]\)|\$[@<^?*+|]", "auto", replace_shell_calls(text, "shell-output"))

    def expand(match):
        name = match.group(1) or match.group(2)
        if name in variables:
            return variables[name]
        return MAKE_DEFAULTS.get(name, match.group(0))

    for _ in range(8):
        expanded = replace_shell_calls(re.sub(r"\$\(([A-Za-z_][\w.]*)\)|\$\{([A-Za-z_][\w.]*)\}", expand, text),
                                       "shell-output")
        if expanded == text:
            break
        text = expanded
    return text.replace("$$", "$")


def normalize_recipe(line):
    return re.sub(r"^[@+-]+", "", line.strip())


def find_makefile(base, makefile=None):
    if makefile:
        path = os.path.join(base, makefile)
        return path if os.path.isfile(path) else None
    return existing_file(base, ("GNUmakefile", "makefile", "Makefile"))


def just_value(text):
    """The value of a plain just string ('...', or "..." without escapes), else None."""
    text = re.sub(r"\s+#.*$", "", text.strip())
    literal = re.match(r"^'([^'\n]*)'$", text) or re.match(r'^"([^"\\\n]*)"$', text)
    return literal.group(1) if literal else None


def parse_justfile(path):
    """Recipes, variables, and anything that stops this checker from knowing what just runs ("unsafe")."""
    info = {"recipes": {}, "aliases": {}, "imports": False, "default": None, "shell": [], "variables": {},
            "settings": [], "exported": [], "unsafe": []}
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as handle:
            lines = handle.read().splitlines()
    except OSError:
        return info
    current = None
    attributes = []
    for line in lines:
        if line[:1] in " \t" and line.strip():
            if current:
                recipe = info["recipes"][current]
                text = line.strip()
                if text.startswith("#!") and not recipe["recipe"]:
                    recipe["flags"].append("script")  # a shebang recipe runs as one script
                elif not text.startswith("#"):
                    recipe["recipe"].append(text)
            continue
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        current = None
        if re.search(r"\bshell\s*\(", line):
            info["unsafe"].append("the justfile uses shell(), which runs commands when just reads the justfile")
        if line.startswith("["):
            bare = re.sub(r"'[^']*'|\"(?:[^\"\\]|\\.)*\"", "", line)
            attributes += re.findall(r"([A-Za-z][\w-]*)\s*(?=\(|:|,|\])", bare)
            continue
        info["shell"].extend(re.findall(r"`([^`]+)`", line))  # just runs backticks while reading the file
        setting = re.match(r"^set\s+([\w-]+)", line)
        alias = re.match(r"^alias\s+([\w-]+)\s*:=\s*([\w-]+)\s*(#.*)?$", line)
        variable = re.match(r"^(export\s+)?([A-Za-z_][\w-]*)\s*:=\s*(.+)$", line)
        recipe = JUST_RECIPE_RE.match(line)
        if setting:
            info["settings"].append(setting.group(1))
        elif alias:
            info["aliases"][alias.group(1)] = alias.group(2)
        elif re.match(r"^(import|mod)\b", line):
            info["imports"] = True
        elif variable:
            value = variable.group(3).strip()
            info["variables"][variable.group(2)] = "shell-output" if value.startswith("`") else just_value(value)
            if variable.group(1):
                info["exported"].append(variable.group(2))
        elif recipe:
            current = recipe.group(1)
            entry = info["recipes"].setdefault(current, {"prereqs": [], "recipe": [], "flags": [], "params": []})
            deps = re.sub(r"'[^']*'|\"(?:[^\"\\]|\\.)*\"", "", recipe.group(3))  # drop quoted arguments
            entry["prereqs"] += re.findall(r"[\w-]+", deps)  # a second definition adds to the first
            entry["flags"] += attributes
            entry["params"] = JUST_PARAM_RE.findall(recipe.group(2))
            entry["flags"] += ["(arguments)"] if "(" in recipe.group(3) else []  # test: (build "x") passes x
            info["default"] = current if "default" in attributes else info["default"] or current
        else:
            info["unsafe"].append("the justfile has a line this checker cannot read (%s)" % code(line, 60))
        attributes = []
    return info


def just_expand(line, variables, values):
    """(line with each {{ name }} replaced, a reason when an expression cannot be resolved or None)."""
    problems = []

    def expand(match):
        name = match.group(1).strip()
        if values.get(name) is not None:
            return values[name]
        if name not in values and re.match(r"^[A-Za-z_][\w-]*$", name) and variables.get(name) is not None:
            return variables[name]
        problems.append(name)
        return ""

    text = re.sub(r"\{\{(.*?)\}\}", expand, line)
    if problems:
        return text, "uses a just expression this checker cannot resolve (%s)" % code(problems[0], 60)
    return text, None


def find_justfile(start, repo):
    return find_up(start, repo, ("justfile", "Justfile", ".justfile"))


# ---------------------------------------------------------- classification


def best_kind(kinds):
    kinds = [k for k in kinds if k]
    return min(kinds, key=lambda k: KIND_ORDER.get(k, 9)) if kinds else None


def git_dir_for(start):
    current = os.path.abspath(start)
    while True:
        candidate = os.path.join(current, ".git")
        if os.path.lexists(candidate):
            return candidate
        parent = os.path.dirname(current)
        if parent == current:
            return None
        current = parent


def git_config_keys(path):
    """(section.key, value) pairs from a git config file, section and key names lowercased."""
    keys, section = [], ""
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as handle:
            lines = handle.read().splitlines()
    except OSError:
        return keys
    for line in lines:
        text = line.strip()
        if not text or text[0] in "#;":
            continue
        header = re.match(r'^\[\s*([A-Za-z0-9.-]+)(?:\s+"((?:[^"\\]|\\.)*)")?\s*\]', text)
        if header:
            section = header.group(1).lower() + ("." + header.group(2) if header.group(2) is not None else "")
            text = text[header.end():].strip()
            if not text:
                continue
        entry = re.match(r"^([A-Za-z][A-Za-z0-9-]*)\s*(?:=\s*(.*))?$", text)
        if entry and section:
            keys.append((section + "." + entry.group(1).lower(), (entry.group(2) or "").strip()))
    return keys


def git_config_problem(cwd):
    """A reason when the repo's own git config makes read-only git commands run programs, else None."""
    git = git_dir_for(cwd)
    if git is None:
        return None
    if not os.path.isdir(git):
        return "the repo's .git is a file that points elsewhere, so this checker cannot read its git config"
    for name in ("config", "config.worktree"):
        for key, value in git_config_keys(os.path.join(git, name)):
            if GIT_RISKY_CONFIG_RE.match(key):
                if key == "core.fsmonitor" and value.lower() in ("", "true", "false", "yes", "no", "on", "off", "0", "1"):
                    continue
                return "the repo's git config runs a program (%s)" % code(key, 60)
    return None


def git_problem(argv, cwd):
    args = argv[1:]
    index = 0
    while index < len(args) and args[index].startswith("-"):
        if args[index] != "--no-pager":
            return "uses a git option this checker does not read (%s)" % code(args[index], 40)
        index += 1
    for arg in args[index + 1:]:
        if arg in GIT_WRITE_FLAGS or arg.startswith(("--output=", "--output-directory=")):
            return "git writes a file with %s" % code(arg, 40)
        if arg in GIT_EXEC_FLAGS:
            return "git runs another program with %s" % code(arg, 40)
    return git_config_problem(cwd)


def helper_problem(argv, cwd):
    """A reason when a read-only helper is used in a form that writes, runs programs, or changes the machine."""
    head = program_name(argv[0])
    args = argv[1:]
    for arg in args:
        if OUTPUT_FLAG_RE.match(arg):
            return "%s writes a file with %s" % (code(head, 40), code(arg, 40))
    if head == "git":
        return git_problem(argv, cwd)
    if head == "date":
        for arg in args:
            if not (arg.startswith("+") or arg in DATE_READ_FLAGS
                    or re.match(r"^(-I[a-z]*|--iso-8601(=\w+)?|--rfc-3339=\w+)$", arg)):
                return "date %s can set the clock" % code(arg, 40)
    if head == "hostname":
        for arg in args:
            if arg not in HOSTNAME_READ_FLAGS:
                return "hostname %s can rename this machine" % code(arg, 40)
    if head == "rg":
        for arg in args:
            if arg.split("=", 1)[0] in RG_EXEC_FLAGS:
                return "rg runs another program with %s" % code(arg, 40)
    if head == "printf" and any(arg.startswith("-v") for arg in args):
        return "printf -v sets a shell variable"
    return None


def shell_expansions(raw):
    """What the shell expands in raw that this checker's word splitting does not: $NAME, $'...', and {a,b}."""
    found = set()
    quote = None
    index = 0
    while index < len(raw):
        char = raw[index]
        following = raw[index + 1:index + 2]
        if quote == "'":
            quote = None if char == "'" else quote
        elif char == "\\":
            index += 1  # the next character is literal
        elif char == "$" and following in ("'", '"') and quote is None:
            found.add("quoting")
        elif char == "$" and re.match(r"[A-Za-z_{@*#?!$0-9-]", following):
            found.add("variable")
        elif quote == '"':
            quote = None if char == '"' else quote
        elif char in "'\"":
            quote = char
        elif char == "{" and re.match(r"\{[^{}\s'\"]*(,|\.\.)[^{}\s'\"]*\}", raw[index:]):
            found.add("brace")
        elif char in "*?[":
            found.add("glob")
        index += 1
    return found


def outside_argument(argv, cwd, repo):
    """The first argument that names a place outside the repo (an absolute path, ~, or ..), else None."""
    for word in (w for w in argv[1:] if not re.search(r"https?://", w)):  # a web address is not a path
        for piece in re.split(r"[=:,]", re.sub(r"^-[A-Za-z]+(?=[/~])", "", word)):
            if piece.startswith("/") and not os.path.lexists("/" + piece.strip("/").split("/")[0]):
                continue  # /api/ or /node_modules/ is a pattern, not a place on this machine
            if piece.startswith("~") or ((piece.startswith("/") or ".." in piece.split("/")) and piece not in DEVICE_PATHS
                                         and not within(os.path.normpath(os.path.join(cwd, piece)), repo)):
                return word
    return None


def dash_names(folder):
    """True when folder holds a file whose name starts with -, which a wildcard would pass as an option."""
    try:
        return any(entry.startswith("-") for entry in os.listdir(folder))
    except OSError:
        return False


def allowed_set(argv):
    """set -e, set -eu, set -o pipefail and similar: shell options only."""
    options = argv[1:]
    return (program_name(argv[0]) == "set" and bool(options)
            and all(re.match(r"^[-+][aBCeEfnuvxo]+$", o) or o in ("pipefail", "errexit", "nounset") for o in options))


def inherit(inherited, env=None, make=None):
    """What a command passes to the commands it starts: environment values and make command-line variables."""
    inherited = inherited or {"env": {}, "make": {}}
    return {"env": dict(inherited["env"], **(env or {})), "make": dict(inherited["make"], **(make or {}))}


def classify(command, base_dir, repo=None, depth=0, body=False, trace=None, inherited=None):
    """Decide whether --run may execute a command. --run is an allowlist.

    Returns {"kind", "safety", "reason"}. safety is "safe" only when every part
    that would execute is a known test, lint, type check, or build step (or a
    lone --help or --version); "never" for installs, deploys, publishes, pushes,
    deletes, elevated rights, and downloads piped into a shell; "not_run" for
    anything else, including anything this checker cannot classify.
    body=True judges a script body, recipe, or prerequisite: there a line may
    also be a read-only helper, a nested runner, or a node, python, or tsx
    script inside the repo. trace collects every body line reached, so the user
    sees what would run. inherited carries the environment values and make
    variables that the calling scripts pass down.
    """
    repo = os.path.abspath(repo or base_dir)
    trace = [] if trace is None else trace
    raw = command.strip()
    if not body and has_placeholder(raw):
        return verdict("other", NOT_RUN, PLACEHOLDER_REASON)
    for pattern in NETWORK_RES:
        if pattern.search(raw):
            return verdict("network", NEVER, "pipes a download into a shell or interpreter")
    segments = parse(raw)
    if segments is None:
        return verdict("other", NOT_RUN, "could not parse the command")
    if HEREDOC_RE.search(raw):
        return verdict("other", NOT_RUN, "uses a heredoc")
    if any(mark in raw for mark in ("$(", "`", "<(", ">(")):
        return verdict("other", NOT_RUN, "uses command substitution")
    expansions = shell_expansions(raw)
    cwd = base_dir
    kinds = []
    for segment in segments:
        pairs = assignments_of(segment["argv"])
        for _name, value in pairs:
            danger = never_anywhere(split_words(value))
            if danger:
                return verdict(danger[0], NEVER, danger[1])
        for name, _value in pairs:
            if blocked_variable(name):
                return verdict("other", NOT_RUN, "sets %s, which changes what the command runs" % code(name, 60))
        argv = strip_wrappers(segment["argv"])
        writes = [t for op, t in segment["redirects"] if op in WRITE_REDIRECTS
                  and t not in ("/dev/null", "/dev/stdout", "/dev/stderr") and not t.startswith("&") and not t.isdigit()]
        for target in writes:
            if not body:
                return verdict("other", NOT_RUN, "writes output to a file (%s)" % code(target, 80))
            path = os.path.normpath(os.path.join(cwd, target))
            in_build = within(path, repo) and generated(path, repo)
            if os.path.exists(path) and not in_build:
                return verdict("other", NOT_RUN, "overwrites %s, a file that already exists" % code(target, 80))
            if not in_build:
                return verdict("other", NOT_RUN, "writes %s outside the build folders" % code(target, 80))
        if not argv:
            continue
        danger = never_anywhere(argv) if body else never_kind(argv)
        if danger:
            return verdict(danger[0], NEVER, danger[1])
        joined = joined_command(argv)
        if WATCH_RE.search(joined):
            return verdict("other", NOT_RUN, "keeps running (watch or server mode)")
        if any(p.search(joined) for p in MODIFY_RES):
            return verdict("other", NOT_RUN, "changes files (%s)" % code(joined, 60))
        head = program_name(argv[0])
        if head == "cd":
            target = argv[1] if len(argv) > 1 else ""
            if not target or target.startswith(("~", "$", "-")):
                return verdict("other", NOT_RUN, "changes to a folder this checker cannot check (%s)"
                               % (code(target, 60) if target else "home"))
            folder = os.path.normpath(os.path.join(cwd, target))
            if not os.path.isdir(folder) or not within(folder, repo):
                return verdict("other", NOT_RUN, "changes to a folder that is not in the repo (%s)" % code(target, 60))
            cwd = folder
            continue
        if "quoting" in expansions:
            return verdict("other", NOT_RUN, "uses $'...' quoting, which this checker does not read")
        if "variable" in expansions:
            return verdict("other", NOT_RUN, "uses a shell variable, so this checker cannot tell what it runs")
        if "brace" in expansions:
            return verdict("other", NOT_RUN, "uses brace expansion ({a,b}), which this checker does not read")
        if re.search(r"\(\s*\)", raw):
            return verdict("other", NOT_RUN, "defines a shell function")
        if "glob" in expansions and any(re.search(r"[*?[]", w) for w in argv[1:]) and dash_names(cwd):
            return verdict("other", NOT_RUN, "a file name in this folder starts with -, so a wildcard could pass "
                           "it as an option")
        outside = outside_argument(argv, cwd, repo)
        if outside:
            return verdict("other", NOT_RUN, "uses a path outside the repo (%s)" % code(outside, 60))
        if head in HARMLESS:
            problem = helper_problem(argv, cwd)
            if problem:
                return verdict("other", NOT_RUN, problem)
        if head in BODY_BUILTINS or head in HARMLESS or (body and allowed_set(argv)):
            continue
        result = (runner_verdict(argv, cwd, repo, depth, body, trace, inherit(inherited, dict(pairs)))
                  or direct_verdict(argv, body, cwd, repo))
        if result["safety"] != SAFE:
            return result
        kinds.append(result["kind"])
    kind = best_kind(kinds)
    if body:
        return verdict(kind, SAFE, "")
    if kind is None or any(k is None for k in kinds):
        return verdict(kind or "other", NOT_RUN, NOT_A_CHECK)
    return verdict(kind, SAFE, "")


def judge_lines(lines, base, repo, depth, trace, context, inherited=None):
    """Judge each (source, line) as a script body; return the first held-back verdict or None."""
    for source, line in lines:
        if depth >= MAX_DEPTH:
            return verdict("other", NOT_RUN, "scripts call each other too deeply to judge")
        trace.append({"from": source, "line": line})
        if re.search(r"\$[({]", line) and context.startswith("make"):
            return verdict("other", NOT_RUN, "uses a make variable or function this checker cannot resolve (in %s)"
                           % context)
        result = classify(line, base, repo, depth + 1, body=True, trace=trace, inherited=inherited)
        if result["safety"] != SAFE:
            return verdict(result["kind"], result["safety"], "%s (in %s)" % (result["reason"], context))
    return None


def named_verdict(name, kind, body):
    if kind in NEVER_REASONS:
        return verdict(kind, NEVER, "%s (%s)" % (NEVER_REASONS[kind], code(name, 60)))
    if kind == "install":
        return verdict("install", NEVER, "installs packages (%s)" % code(name, 60))
    if kind == "modify":
        return verdict("other", NOT_RUN, "changes files (%s)" % code(name, 60))
    if kind not in SAFE_KINDS and not body:
        return verdict("other", NOT_RUN, NOT_A_CHECK)
    return None


def runner_verdict(argv, cwd, repo, depth, body, trace, inherited=None):
    head = program_name(argv[0])
    args = argv[1:]
    sub = args[0] if args else ""
    if (head, sub) in DOWNLOADERS or (head, "") in DOWNLOADERS:
        return verdict("other", NOT_RUN, "may download and run a package")
    if head in JS_TOOLS:
        return js_verdict(argv, cwd, repo, depth, body, trace, inherited)
    if head == "make":
        return make_verdict(argv, cwd, repo, depth, body, trace, inherited)
    if head == "just":
        return just_verdict(argv, cwd, repo, depth, body, trace, inherited)
    if head == "uv" and sub == "run":
        return uv_verdict(args[1:], cwd, repo, depth, body, trace, inherited)
    if head == "hatch" and sub == "run":
        return verdict("other", NOT_RUN, INSTALLS_FIRST)
    if head in ("poetry", "pdm", "pipenv") and sub == "run":
        rest = runner_rest(args[1:])
        if not rest:
            return verdict("other", NOT_RUN, "nothing to run")
        return classify(shlex_join(rest), cwd, repo, depth + 1, body, trace, inherited)
    return None


def js_config_problem(tool, start, repo):
    """A reason when the repo's package manager config changes how scripts run, else None."""
    current = os.path.abspath(start)
    stop = os.path.abspath(repo)
    while True:
        npmrc = existing_file(current, (".npmrc",))
        if npmrc:
            for line in read_text(npmrc, repo).splitlines():
                key = line.split("=", 1)[0].strip().lower()
                if "=" in line and key in NPMRC_RISKY_KEYS:
                    return "the repo's .npmrc sets %s, which changes how scripts run" % key
        workspace = existing_file(current, ("pnpm-workspace.yaml",)) if tool == "pnpm" else None
        for key in re.findall(r"(?m)^(scriptShell|nodeOptions|script-shell|node-options)\s*:",
                              read_text(workspace, repo) if workspace else ""):
            return "the repo's pnpm-workspace.yaml sets %s, which changes how scripts run" % key
        if tool == "yarn":
            yarnrc = existing_file(current, (".yarnrc.yml",))
            for key in re.findall(r"(?m)^(yarnPath|plugins)\s*:", read_text(yarnrc, repo) if yarnrc else ""):
                return "the repo's .yarnrc.yml sets %s, which runs code from the repo as part of yarn" % key
            legacy = existing_file(current, (".yarnrc",))
            if legacy and re.search(r"(?m)^\s*-*yarn-path\b", read_text(legacy, repo)):
                return "the repo's .yarnrc sets yarn-path, which runs code from the repo in place of yarn"
        parent = os.path.dirname(current)
        if current == stop or parent == current or not current.startswith(stop):
            return None
        current = parent


def js_verdict(argv, cwd, repo, depth, body, trace, inherited=None):
    tool, name, subcommand, workspace = js_script(argv)
    if workspace:
        return verdict("other", NOT_RUN, "runs scripts in another package that this checker did not read")
    if name is None:
        if tool == "bun" and subcommand == "test":
            return verdict("test", SAFE)
        if tool == "pnpm" and subcommand == "exec" and len(argv) > 2:
            return classify(shlex_join(argv[2:]), cwd, repo, depth + 1, body, trace, inherited)
        return None
    kind = name_kind(name)
    early = named_verdict(name, kind, body)
    if early:
        return early
    package = find_up(cwd, repo, "package.json")
    scripts = package_scripts(package) if package else {}
    problem = js_config_problem(tool, os.path.dirname(package) if package else cwd, repo)
    if problem:
        return verdict("other", NOT_RUN, problem)
    if name not in scripts:
        if subcommand == "implicit":
            return classify(shlex_join(argv[argv.index(name, 1):]), cwd, repo, depth + 1, body, trace, inherited)
        return verdict("other", NOT_RUN, "no %s script to run" % code(name, 60))
    extra = script_arguments(argv, name)
    label = "%s script" % rel(package, repo)
    lines = []
    for script in ("pre" + name, name, "post" + name):
        text = scripts.get(script)
        if text:
            lines.append(("%s %s" % (label, script),
                          text + (" " + shlex_join(extra) if script == name and extra else "")))
    held = judge_lines(lines, os.path.dirname(package), repo, depth, trace, "the %s script" % code(name, 60),
                       inherited)
    return held or verdict(kind if kind in SAFE_KINDS else None, SAFE)


def make_verdict(argv, cwd, repo, depth, body, trace, inherited=None):
    inherited = inherit(inherited)
    directory, makefile, targets, overrides, unknown = make_parts(argv)
    overrides = dict(inherited["make"], **overrides)  # a parent make passes its command-line variables down
    if unknown:
        return verdict("other", NOT_RUN, "uses a make option this checker does not read (%s)" % code(unknown[0], 40))
    base = os.path.normpath(os.path.join(cwd, directory)) if directory else cwd
    if directory and (not os.path.isdir(base) or not within(base, repo)):
        return verdict("other", NOT_RUN, "runs make in a folder that is not in the repo")
    path = find_makefile(base, makefile)
    if not path or not within(path, repo):
        return verdict("other", NOT_RUN, "no Makefile in the repo to read")
    info = parse_makefile(path)
    if info["includes"] or info["dynamic"]:
        return verdict("other", NOT_RUN, MAKE_INCLUDE_REASON)
    variables = dict(info["variables"])
    variables.update(overrides)
    for name, value in variables.items():
        if name in MAKE_SPECIAL_VARIABLES:
            return verdict("other", NOT_RUN, "the Makefile sets %s, which %s" % (name, MAKE_SPECIAL_VARIABLES[name]))
        if name in MAKE_FLAG_VARIABLES:
            if not all(SAFE_MAKE_FLAG_RE.match(word) for word in value.split()):
                return verdict("other", NOT_RUN, "the Makefile sets %s, which changes how make runs" % name)
        elif blocked_variable(name):
            return verdict("other", NOT_RUN, "the Makefile sets %s, which changes what recipes run" % code(name, 60))
    if info["unsafe"]:
        return verdict("other", NOT_RUN, info["unsafe"][0])
    if ".ONESHELL" in info["targets"]:
        return verdict("other", NOT_RUN, "the Makefile sets .ONESHELL, which changes how recipes run")
    if any(os.path.normpath(os.path.join(base, t)) == os.path.normpath(path) for t in info["targets"]) or \
            builtin_sources(os.path.dirname(path), os.path.basename(path), info["suffixes"]):
        return verdict("other", NOT_RUN, "make may rebuild the Makefile itself with a rule this checker does not read")
    for name in info["weak"] & set(inherited["env"]) - set(overrides):
        variables[name] = "$(%s)" % name  # the caller's environment supplies a value this checker does not see
    default = make_substitute(variables.get(".DEFAULT_GOAL", ""), variables).split()
    default = default[0] if default else info["default"]
    goals = targets or ([default] if default else [])
    if not goals:
        return verdict("other", NOT_RUN, "the Makefile has no target")
    label = rel(path, repo)
    kinds = []
    for goal in goals:
        if is_template(goal):
            return verdict("other", NOT_RUN, PLACEHOLDER_REASON)
        if goal not in info["targets"]:
            return verdict("other", NOT_RUN, "the Makefile has no %s target" % code(goal, 60))
        kind = name_kind(goal) or ("build" if not targets else None)
        visited = visit_targets(info["targets"], goal)
        problem = builtin_rule_problem(visited, info, base)
        if problem:
            return verdict("other", NOT_RUN, problem)
        order = [t for t in visited if t in info["targets"]]
        for name in order:
            early = named_verdict(name, name_kind(name), True)
            if early:
                return verdict(early["kind"], early["safety"], "%s (make %s runs the %s target)"
                               % (early["reason"], code(goal, 60), code(name, 60)))
        early = named_verdict(goal, kind, body)
        if early:
            return early
        lines = [("%s $(shell) call" % label, make_substitute(call, variables)) for call in info["shell"]]
        lines += [("%s target %s" % (label, name), make_substitute(normalize_recipe(line), variables))
                  for name in order for line in info["targets"][name]["recipe"]]
        exported = set(variables) if "*" in info["exported"] or ".EXPORT_ALL_VARIABLES" in info["targets"] \
            else info["exported"] & set(variables)
        passed = inherit(inherited, dict(overrides, **{n: variables[n] for n in exported}), overrides)
        held = judge_lines([(s, l) for s, l in lines if l], base, repo, depth, trace, "make %s" % code(goal, 60),
                           passed)
        if held:
            return held
        kinds.append(kind)
    return verdict(best_kind(kinds), SAFE)


def just_verdict(argv, cwd, repo, depth, body, trace, inherited=None):
    rest = argv[1:]
    if rest and rest[0].startswith("-"):
        return verdict("other", NOT_RUN, "uses a just option this checker does not read (%s)" % code(rest[0], 40))
    path = find_justfile(cwd, repo)
    if not path:
        return verdict("other", NOT_RUN, "no justfile in the repo to read")
    info = parse_justfile(path)
    if info["imports"]:
        return verdict("other", NOT_RUN, "the justfile imports other files this checker did not read")
    if info["unsafe"]:
        return verdict("other", NOT_RUN, info["unsafe"][0])
    for setting in info["settings"]:
        if setting not in SAFE_JUST_SETTINGS:
            return verdict("other", NOT_RUN, "the justfile sets %s, which changes how recipes run" % code(setting, 60))
    for name in info["exported"]:
        if blocked_variable(name):
            return verdict("other", NOT_RUN, "the justfile exports %s, which changes what recipes run" % code(name, 60))
    recipe = info["aliases"].get(rest[0], rest[0]) if rest else info["default"]
    params = rest[1:]
    if not recipe or recipe not in info["recipes"]:
        return verdict("other", NOT_RUN, "the justfile has no %s recipe" % (code(recipe, 60) if recipe else "default"))
    kind = name_kind(recipe)
    visited = visit_targets(info["recipes"], recipe)
    missing_recipe = next((r for r in visited if r not in info["recipes"]), None)
    if missing_recipe:
        return verdict("other", NOT_RUN, "the justfile has no %s recipe to read" % code(missing_recipe, 60))
    label = rel(path, repo)
    lines = [("%s backtick" % label, call) for call in info["shell"]]
    exported = {name: info["variables"].get(name) or "" for name in info["exported"]}
    for name in visited:
        early = named_verdict(name, name_kind(name), name != recipe or body)
        if early:
            return early
        entry = info["recipes"][name]
        if "script" in entry["flags"]:
            return verdict("other", NOT_RUN, "the %s recipe is a script (a #! line or [script]) this checker cannot read"
                           % code(name, 60))
        if "(arguments)" in entry["flags"]:
            return verdict("other", NOT_RUN, "the %s recipe passes arguments to another recipe, which this checker "
                           "does not follow" % code(name, 60))
        unread = next((flag for flag in entry["flags"] if flag not in SAFE_JUST_ATTRIBUTES), None)
        if unread:
            return verdict("other", NOT_RUN, "the %s recipe uses [%s], which this checker does not read"
                           % (code(name, 60), code(unread, 40)))
        values = {}
        for index, (variadic, dollar, param, default) in enumerate(entry["params"]):
            given = params[index:] if variadic else params[index:index + 1]
            if name == recipe and given:
                values[param] = " ".join(given)
            else:  # the default, or None when it is not a plain string
                values[param] = just_value(default) if default else ""
            if dollar and blocked_variable(param):  # a $NAME parameter is exported to the recipe
                return verdict("other", NOT_RUN, "the %s recipe exports %s, which changes what recipes run"
                               % (code(name, 60), code(param, 60)))
            if dollar:
                exported[param] = values[param] or ""
        for line in entry["recipe"]:
            text, problem = just_expand(re.sub(r"^[@-]+", "", line), info["variables"], values)
            if problem:
                return verdict("other", NOT_RUN, "%s (in just %s)" % (problem, code(recipe, 60)))
            lines.append(("%s recipe %s" % (label, name), text))
    held = judge_lines([(s, l) for s, l in lines if l], os.path.dirname(path), repo, depth, trace,
                       "just %s" % code(recipe, 60), inherit(inherited, exported))
    return held or verdict(kind if kind in SAFE_KINDS else None, SAFE)


def uv_verdict(args, cwd, repo, depth, body, trace, inherited=None):
    flags = []
    index = 0
    while index < len(args) and args[index].startswith("-"):
        if args[index] == "--":
            index += 1
            break
        flags.append(args[index].split("=", 1)[0])
        index += 2 if args[index] in UV_VALUE_FLAGS else 1
    rest = args[index:]
    pyproject = find_up(cwd, repo, "pyproject.toml")
    project = os.path.dirname(pyproject) if pyproject else cwd
    synced = ("--no-sync" in flags or "--offline" in flags
              or ("--frozen" in flags and os.path.isdir(os.path.join(project, ".venv"))))
    if not synced:
        return verdict("other", NOT_RUN, INSTALLS_FIRST)
    if not rest:
        return verdict("other", NOT_RUN, "nothing to run")
    return classify(shlex_join(rest), cwd, repo, depth + 1, body, trace, inherited)


def runner_rest(args):
    index = 0
    while index < len(args) and args[index].startswith("-"):
        if args[index] == "--":
            index += 1
            break
        index += 2 if args[index] in UV_VALUE_FLAGS else 1
    return args[index:]


def shlex_join(parts):
    return " ".join(shlex.quote(p) for p in parts)


def runs_repo_file(argv, cwd, repo):
    """node, python, or tsx running a script file inside the repo."""
    name = program_name(argv[0])
    args = argv[1:]
    index = 0
    while index < len(args):
        arg = args[index]
        if arg in SCRIPT_STOP_FLAGS.get(name, ()):
            return False
        if arg.startswith("-"):
            index += 2 if arg in SCRIPT_VALUE_FLAGS.get(name, ()) else 1
            continue
        path = os.path.normpath(os.path.join(cwd, arg))
        return os.path.isfile(path) and within(path, repo)
    return False


def direct_verdict(argv, body, cwd, repo):
    head = program_name(argv[0])
    args = argv[1:]
    joined = joined_command(argv)
    if head == "python" and args[:1] == ["-m"] and len(args) > 1:
        module = args[1]
        if module in ("pytest", "unittest", "nose2"):
            return verdict("test", SAFE)
        if module == "build":
            return verdict("other", NOT_RUN, "creates a build environment and installs packages")
        return direct_verdict(args[1:], body, cwd, repo)
    if any(p.search(joined) for p in MODIFY_RES):
        return verdict("other", NOT_RUN, "changes files (%s)" % code(joined, 60))
    if WATCH_RE.search(joined):
        return verdict("other", NOT_RUN, "keeps running (watch or server mode)")
    if head in NETWORK_HEADS:
        return verdict("other", NOT_RUN, "uses the network (%s)" % head)
    on_path = "/" not in argv[0]
    if on_path and args and args[-1] in ("--help", "--version") and (len(args) == 1 or not any(
            "/" in a or "." in a for a in args[:-1])) and head not in ("python", "node", "bash", "sh", "zsh"):
        return verdict("help" if args[-1] == "--help" else "version", SAFE)
    if on_path and len(args) == 1 and args[0] in ("--help", "--version"):
        return verdict("help" if args[0] == "--help" else "version", SAFE)
    if head in ("tox", "nox") or (head == "hatch" and args[:1] == ["test"]):
        return verdict("other", NOT_RUN, "creates environments and installs packages")
    if head == "tsc":
        return verdict("typecheck" if any(a in ("--noEmit", "-noEmit") for a in args) else "build", SAFE)
    if head == "ruff" and args and not args[0].startswith("-") and args[0] not in SUBCOMMAND_KINDS["ruff"]:
        return verdict("lint", SAFE)
    kind = HEAD_KINDS.get(head)
    if kind is None and head in SUBCOMMAND_KINDS:
        sub = next((a for a in args if not a.startswith("-") or a in SUBCOMMAND_KINDS[head]), "")
        kind = SUBCOMMAND_KINDS[head].get(sub)
    if kind:
        return verdict(kind, SAFE)
    if body:
        git_sub = joined.split()[1] if head == "git" and len(joined.split()) > 1 else ""
        if head in READ_ONLY or git_sub in GIT_READ_ONLY or joined == "git branch --show-current":
            problem = helper_problem(argv, cwd)
            return verdict("other", NOT_RUN, problem) if problem else verdict(None, SAFE)
        if head in REPO_SCRIPT_RUNNERS and runs_repo_file(argv, cwd, repo):
            return verdict(None, SAFE)
        return verdict("other", NOT_RUN, "the script runs %s, which this checker does not classify" % code(head, 60))
    if "/" in argv[0]:
        return verdict("other", NOT_RUN, "runs a project script that this checker does not classify")
    return verdict("other", NOT_RUN, NOT_A_CHECK)


# ----------------------------------------------------------- static check


def missing(token, cwd, repo, problems, unverified):
    first = re.sub(r"^(\./)+", "", token).split("/")[0]
    if first in GENERATED_DIRS:
        unverified.append("%s does not exist yet (a setup step creates it)" % code(token, 120))
    else:
        where = rel(cwd, repo) if repo and os.path.abspath(cwd).startswith(os.path.abspath(repo)) else cwd
        problems.append("%s does not exist (looked in %s)" % (code(token, 120), code(where, 120)))


def check_path(token, cwd, repo, problems, unverified, want_dir=False):
    if is_template(token) or any(c in token for c in "*?$~") or token in ("-", ""):
        return
    path = os.path.join(cwd, token)
    ok = os.path.isdir(path) if want_dir else os.path.exists(path)
    if not ok:
        missing(token, cwd, repo, problems, unverified)


def check_pytest_args(args, cwd, repo, problems, unverified):
    skip = False
    for arg in args:
        if skip:
            skip = False
            continue
        if arg.startswith("-"):
            skip = arg in PYTEST_VALUE_OPTS
            continue
        target = arg.split("::", 1)[0].split("[", 1)[0]
        if "/" in target or target.endswith(".py"):
            check_path(target, cwd, repo, problems, unverified)


def check_script_arg(argv, cwd, repo, problems, unverified):
    name = program_name(argv[0])
    args = argv[1:]
    if name == "python" and args[:2] == ["-m", "pytest"]:
        check_pytest_args(args[2:], cwd, repo, problems, unverified)
        return
    if name == "deno":
        if args[:1] != ["run"]:
            return
        args = args[1:]
    index = 0
    while index < len(args):
        arg = args[index]
        if arg in SCRIPT_STOP_FLAGS.get(name, ()):
            return
        if arg.startswith("-"):
            index += 2 if arg in SCRIPT_VALUE_FLAGS.get(name, ()) else 1
            continue
        check_path(arg, cwd, repo, problems, unverified)
        return


def static_check(command, base_dir, repo, env):
    """Check a command without running it. Returns {"status": ok|fail|unverified, "problems", "notes"}."""
    if has_placeholder(command):
        return {"status": "unverified", "problems": [], "notes": [PLACEHOLDER_REASON]}
    problems, unverified = [], []
    segments = parse(command)
    if segments is None:
        return {"status": "unverified", "problems": [], "notes": ["could not parse the command"]}
    path_env = (env or os.environ).get("PATH", "")
    cwd = base_dir
    for segment in segments:
        argv = strip_wrappers(segment["argv"])
        if not argv:
            continue
        head = argv[0]
        name = program_name(head)
        args = argv[1:]
        if name == "cd":
            if args and not is_template(args[0]) and "$" not in args[0] and not args[0].startswith(("~", "-")):
                target = os.path.join(cwd, args[0])
                if os.path.isdir(target):
                    cwd = target
                else:
                    missing(args[0], cwd, repo, problems, unverified)
            continue
        if name in ("source", "."):
            if args:
                check_path(args[0], cwd, repo, problems, unverified)
            continue
        if name in SHELL_BUILTINS:
            continue
        if "/" in head:
            if not os.path.exists(os.path.join(cwd, head)):
                missing(head, cwd, repo, problems, unverified)
                continue
        elif not shutil.which(head, path=path_env):
            venv = next((os.path.join(folder, env_dir, "bin", head) for folder in (cwd, base_dir, repo) if folder
                         for env_dir in (".venv", "venv") if os.path.isfile(os.path.join(folder, env_dir, "bin", head))), None)
            if venv:
                unverified.append("%s is not on PATH, but %s exists; the command works once that virtual "
                                  "environment is active" % (code(head, 60), code(rel(venv, repo), 120)))
            else:
                problems.append("%s is not installed here (not found on PATH)" % code(head, 60))
            continue
        if name in JS_TOOLS:
            check_js(argv, cwd, repo, problems, unverified)
        elif name == "make":
            check_make(argv, cwd, repo, problems, unverified)
        elif name == "just":
            check_just(argv, cwd, repo, problems, unverified)
        elif name in ("uv", "poetry", "pdm", "pipenv") and args[:1] == ["run"]:
            if name == "poetry" and not find_up(cwd, repo, "pyproject.toml"):
                problems.append("poetry needs a pyproject.toml, and there is none in %s or above"
                                % code(rel(cwd, repo), 120))
            rest = runner_rest(args[1:])
            if rest:
                inner = program_name(rest[0])
                if inner in ("python", "node", "bash", "sh", "deno"):
                    check_script_arg(rest, cwd, repo, problems, unverified)
                elif inner == "pytest":
                    check_pytest_args(rest[1:], cwd, repo, problems, unverified)
                elif "/" in rest[0] or rest[0].endswith(".py"):
                    check_path(rest[0], cwd, repo, problems, unverified)
        elif name == "pytest":
            check_pytest_args(args, cwd, repo, problems, unverified)
        elif name in ("python", "node", "bash", "sh", "zsh", "deno", "ruby", "perl", "php", "tsx", "ts-node"):
            check_script_arg(argv, cwd, repo, problems, unverified)
        elif name == "cargo" and not find_up(cwd, repo, "Cargo.toml"):
            problems.append("cargo needs a Cargo.toml, and there is none in %s or above" % code(rel(cwd, repo), 120))
        elif name == "go" and args[:1] == ["run"] and len(args) > 1 and (args[1].startswith("./") or args[1].endswith(".go")):
            check_path(args[1], cwd, repo, problems, unverified)
    status = "fail" if problems else ("unverified" if unverified else "ok")
    return {"status": status, "problems": problems, "notes": unverified}


def check_js(argv, cwd, repo, problems, unverified):
    tool, name, subcommand, workspace = js_script(argv)
    if workspace:
        unverified.append("uses a workspace or folder flag, so the script was not looked up")
        return
    if name is None or is_template(name):
        return
    package = find_up(cwd, repo, "package.json")
    if not package:
        problems.append("no package.json in %s or above" % code(rel(cwd, repo), 120))
        return
    scripts = package_scripts(package)
    label = code(rel(package, repo), 120)
    shown = code(name, 60)
    if name in scripts:
        return
    if tool == "npm" and subcommand == "start" and os.path.isfile(os.path.join(os.path.dirname(package), "server.js")):
        return
    if subcommand == "implicit":
        if tool == "bun" and os.path.isfile(os.path.join(cwd, name)):
            return
        modules = os.path.join(os.path.dirname(package), "node_modules")
        if not os.path.isdir(modules):
            unverified.append("%s is not a script in %s, and dependencies are not installed, so it may be a "
                              "package program" % (shown, label))
        elif not os.path.exists(os.path.join(modules, ".bin", name)):
            problems.append("%s is neither a script in %s nor an installed package program" % (shown, label))
        return
    problems.append("%s has no script %s" % (label, shown))


def check_make(argv, cwd, repo, problems, unverified):
    directory, makefile, targets, _overrides, unknown = make_parts(argv)
    if unknown:
        unverified.append("uses a make option this checker does not read (%s)" % code(unknown[0], 40))
        return
    base = os.path.join(cwd, directory) if directory else cwd
    if directory and not os.path.isdir(base):
        missing(directory, cwd, repo, problems, unverified)
        return
    path = find_makefile(base, makefile)
    if not path:
        problems.append("no Makefile in %s" % code(rel(base, repo) if repo else base, 120))
        return
    info = parse_makefile(path)
    label = code(rel(path, repo), 120)
    for target in targets:
        if is_template(target) or target in info["targets"]:
            continue
        if info["includes"] or info["dynamic"]:
            unverified.append("%s has no %s target, but it includes other files or pattern rules"
                              % (label, code(target, 60)))
        else:
            problems.append("%s has no target %s" % (label, code(target, 60)))


def check_just(argv, cwd, repo, problems, unverified):
    args = argv[1:]
    if any(a.split("=", 1)[0] in ("--justfile", "-f", "--working-directory", "-d") for a in args):
        unverified.append("uses a justfile or folder flag, so the recipe was not looked up")
        return
    recipe = next((a for a in args if not a.startswith("-")), None)
    path = find_justfile(cwd, repo)
    if not path:
        problems.append("no justfile in %s or above" % code(rel(cwd, repo), 120))
        return
    info = parse_justfile(path)
    label = code(rel(path, repo), 120)
    if recipe is None:
        if not info["recipes"]:
            problems.append("%s has no recipes" % label)
        return
    if is_template(recipe) or recipe in info["recipes"] or recipe in info["aliases"]:
        return
    if info["imports"]:
        unverified.append("%s has no %s recipe, but it imports other files" % (label, code(recipe, 60)))
    else:
        problems.append("%s has no recipe %s" % (label, code(recipe, 60)))


# -------------------------------------------------------------------- run


def output_tail(text):
    lines = [l.strip() for l in (text or "").splitlines() if l.strip() and not re.fullmatch(r"[-=_*#~.\s]+", l.strip())]
    return safe_text(" | ".join(lines[-3:]), 100000)[-EXCERPT_LIMIT:]


def run_command(command, cwd, timeout, env):
    """Run one command through bash with pipefail, input closed, and a timeout for its whole process group."""
    started = time.monotonic()
    bash = shutil.which("bash", path=env.get("PATH", "")) or "/bin/bash"
    try:
        proc = subprocess.Popen([bash, "-o", "pipefail", "-c", command], cwd=cwd, env=env, stdin=subprocess.DEVNULL,
                                stdout=subprocess.PIPE, stderr=subprocess.STDOUT, start_new_session=True)
    except OSError as err:
        return {"exit_code": None, "seconds": 0.0, "timed_out": False, "timeout": timeout,
                "output_tail": output_tail(str(err))}
    timed_out = False
    try:
        output, _ = proc.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        timed_out = True
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except (AttributeError, OSError):
            proc.kill()
        try:
            output, _ = proc.communicate(timeout=2)
        except subprocess.TimeoutExpired:
            # A process that left the group still holds the output pipe; stop reading instead of waiting for it.
            output = b""
            proc.stdout.close()
            proc.wait()
    return {
        "exit_code": None if timed_out else proc.returncode,
        "seconds": round(time.monotonic() - started, 2),
        "timed_out": timed_out,
        "timeout": timeout,
        "output_tail": output_tail(output.decode("utf-8", errors="replace") if output else ""),
    }


def fmt_duration(seconds):
    if seconds < 10:
        return "%.1f s" % seconds
    if seconds < 60:
        return "%d s" % round(seconds)
    minutes, secs = divmod(int(round(seconds)), 60)
    if minutes < 60:
        return "%d min %d s" % (minutes, secs)
    hours, minutes = divmod(minutes, 60)
    return "%d h %d min" % (hours, minutes)


def timeout_advice(timeout):
    return "did not finish in %d s; rerun with --timeout %d" % (timeout, 600 if timeout < 600 else timeout * 2)


# ---------------------------------------------------------------- collect


def source_files(loadmap):
    """Project files any agent reads (at start, on demand, or conditionally), in load-map order."""
    seen, files = set(), []
    for harness in loadmap["harnesses"]:
        for item in harness["files"]:
            path = item.get("_abs")
            if item["scope"] != "project" or item["status"] == load_map.SKIPPED or not path or path in seen:
                continue
            seen.add(path)
            files.append((path, item["path"], item.get("_anchor") or loadmap["repo"]))
    return files


def read_text(path, repo):
    """Text of a file whose real location is inside the repo; "" for anything else, such as a link out of it."""
    if not within(path, repo):
        return ""
    try:
        if os.path.getsize(path) > load_map.READ_LIMIT:
            return ""
        with open(path, "r", encoding="utf-8", errors="replace") as handle:
            return handle.read()
    except OSError:
        return ""


def collect(loadmap, env=None, run=False, timeout=120, progress=None):
    """Extract, check, and (with run=True) execute the documented commands."""
    env = dict(os.environ if env is None else env)
    repo = loadmap["repo"]
    entries, order = {}, []
    for path, shown, anchor in source_files(loadmap):
        for item in extract_commands(read_text(path, repo)):
            key = (item["command"], anchor)
            source = "%s:%d" % (shown, item["line"])
            if key in entries:
                entry = entries[key]
                if source not in entry["sources"]:
                    entry["sources"].append(source)
                entry["negated"] = entry["negated"] and item["negated"]
                continue
            entries[key] = {"command": item["command"], "sources": [source], "folder": rel(anchor, repo),
                            "negated": item["negated"], "_anchor": anchor}
            order.append(key)
    commands = [entries[key] for key in order]
    for entry in commands:
        if entry["negated"]:
            entry.update(kind="", safety="", safety_reason="mentioned as something not to do", static="skipped",
                         problems=[], notes=[], runs=[], run=None)
            continue
        trace = []
        judged = classify(entry["command"], entry["_anchor"], repo, trace=trace)
        checked = static_check(entry["command"], entry["_anchor"], repo, env)
        entry.update(kind=judged["kind"], safety=judged["safety"], safety_reason=judged["reason"],
                     static=checked["status"], problems=checked["problems"], notes=checked["notes"],
                     runs=trace if judged["safety"] == SAFE else [], run=None)
    runnable = [e for e in commands if e["safety"] == SAFE and e["static"] == "ok"]
    if run:
        for entry in runnable:
            if progress:
                progress("running: %s" % safe_text(entry["command"]))
            entry["run"] = run_command(entry["command"], entry["_anchor"], timeout, env)
    documented = [e for e in commands if not e["negated"]]
    ran = [e for e in documented if e["run"]]
    timed_out = [e for e in ran if e["run"]["timed_out"]]
    run_failed = [e for e in ran if not e["run"]["timed_out"] and e["run"]["exit_code"] != 0]
    failing = [e for e in documented if e["static"] == "fail" or e in run_failed]
    test_loop = None
    for entry in ran:
        if entry["kind"] == "test":
            test_loop = {"command": entry["command"], "seconds": entry["run"]["seconds"],
                         "exit_code": entry["run"]["exit_code"], "timed_out": entry["run"]["timed_out"],
                         "timeout": entry["run"]["timeout"]}
            break
    summary = {
        "documented": len(documented),
        "static_ok": sum(1 for e in documented if e["static"] == "ok"),
        "static_fail": sum(1 for e in documented if e["static"] == "fail"),
        "unverified": sum(1 for e in documented if e["static"] == "unverified"),
        "safe": sum(1 for e in documented if e["safety"] == SAFE),
        "never": sum(1 for e in documented if e["safety"] == NEVER),
        "not_run": sum(1 for e in documented if e["safety"] == NOT_RUN),
        "ran": len(ran),
        "run_fail": len(run_failed),
        "timed_out": len(timed_out),
        "failing": len(failing),
    }
    result = {
        "tool": "agents-md-checker/commands",
        "version": load_map.VERSION,
        "repo": repo,
        "commands": commands,
        "would_run": [e["command"] for e in runnable],
        "ran": bool(run),
        "summary": summary,
        "test_loop": test_loop,
    }
    result["headline"] = commands_headline(result)
    return result


def commands_clause(result):
    summary = result["summary"]
    if not summary["documented"]:
        return None
    if summary["failing"]:
        return "%d of %d documented commands %s" % (summary["failing"], summary["documented"],
                                                     "fails" if summary["failing"] == 1 else "fail")
    if summary["documented"] == 1:
        return "the 1 documented command checks out"
    return "all %d documented commands check out" % summary["documented"]


def test_loop_sentence(result):
    loop = result.get("test_loop")
    if not loop:
        return ""
    command = code(loop["command"], 120)
    if loop["timed_out"]:
        return " Your test command %s %s." % (command, timeout_advice(loop["timeout"]))
    return " Your test command %s takes %s." % (command, fmt_duration(loop["seconds"]))


def commands_headline(result):
    clause = commands_clause(result)
    if clause is None:
        return "No commands found in the instruction files agents load from this repo."
    return clause[0].upper() + clause[1:] + "." + test_loop_sentence(result)


def run_cell(entry):
    if entry["negated"]:
        return "not run: mentioned as something not to do"
    outcome = entry.get("run")
    if outcome:
        if outcome["timed_out"]:
            return timeout_advice(outcome["timeout"])
        if outcome["exit_code"] == 0:
            return "passed in %s" % fmt_duration(outcome["seconds"])
        tail = outcome["output_tail"]
        return "failed (exit %s) in %s%s" % (outcome["exit_code"], fmt_duration(outcome["seconds"]),
                                            ": %s" % code(tail) if tail else "")
    if entry["safety"] == SAFE:
        if entry["static"] == "ok":
            return "safe: runs with --run"
        return "not run: the static check did not pass"
    if entry["safety"] == NEVER:
        return "never run: %s" % entry["safety_reason"]
    return "not run: %s" % entry["safety_reason"]


def static_cell(entry):
    if entry["static"] == "fail":
        return "fails: " + "; ".join(entry["problems"])
    if entry["static"] == "unverified":
        return "unverified: " + "; ".join(entry["notes"])
    return entry["static"]


def render_sections(result):
    cell = load_map.cell
    lines = ["## Documented commands", ""]
    if not result["commands"]:
        return lines + ["No commands found in the instruction files.", ""]
    lines += ["| Command | Found in | Static check | Run |", "|---|---|---|---|"]
    for entry in result["commands"]:
        where = ", ".join(code(source, 120) for source in entry["sources"][:2]) + (
            " and more" if len(entry["sources"]) > 2 else "")
        lines.append("| %s | %s | %s | %s |" % (code(entry["command"], 200), where, cell(static_cell(entry)),
                                                 cell(run_cell(entry))))
    lines.append("")
    if not result["ran"]:
        runnable = [e for e in result["commands"] if e["command"] in result["would_run"] and not e["negated"]]
        if runnable:
            lines.append("With --run, these would run (tests, lint, type checks, builds, --help, --version), one at "
                         "a time with a timeout, each in the folder of the file that documents it:")
            for entry in runnable:
                lines.append("- %s (in %s)%s" % (code(entry["command"], 200), code(entry["folder"] or ".", 200),
                                                 ", which runs:" if entry["runs"] else ""))
                for step in entry["runs"]:
                    lines.append("  - %s (%s)" % (code(step["line"], 200), code(step["from"], 120)))
        else:
            lines.append("With --run, nothing would run: no documented command is a safe test, lint, type check, "
                         "or build that passed its check.")
        lines.append("")
    return lines


def render_markdown(result):
    return "\n".join(["**%s**" % load_map.line_text(result["headline"]), ""]
                     + render_sections(result)).rstrip() + "\n"


def exit_code(result, fail_on):
    if fail_on and result["summary"]["failing"]:
        return 1
    return 0


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="Check the commands that agent instruction files document. Static checks by default; "
                    "--run runs only allowlisted commands (tests, lint, type checks, builds, --help, --version).")
    load_map.add_common_arguments(parser)
    parser.add_argument("--run", action="store_true", help="run the allowlisted commands, each with a timeout")
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
    loadmap = load_map.build_load_map(repo, cwd=cwd)
    result = collect(loadmap, run=args.run, timeout=args.timeout,
                     progress=lambda text: print(text, file=sys.stderr))
    text = json.dumps(load_map.public(result), indent=2) + "\n" if args.json else render_markdown(result)
    load_map.emit(text, args.out)
    return exit_code(result, args.fail_on)


if __name__ == "__main__":
    sys.exit(main())
