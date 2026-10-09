#!/usr/bin/env python3
"""Probe what this shell can reach: secret files, trust files, Docker, sudo.

Run it through the agent's own shell tool, so it measures the agent's real
sandbox. It edits no existing file: secret files are opened and closed without
reading a byte, existing files are opened for append and closed without
writing, and a folder gets one empty test file that is deleted at once.
"""
from __future__ import annotations

import argparse
import errno
import fnmatch
import glob
import json
import os
import platform
import re
import secrets
import socket
import stat
import subprocess
import sys

try:
    import tomllib  # Python 3.11+; without it the Codex config is skipped with a note
except ImportError:
    tomllib = None

# The shared text cleaner (sync_shared.py copies it here from skills/evals/shared/).
# File, folder, user, and variable names and settings values are untrusted: they
# can hold a secret, carry instructions for the agent that relays the report, or
# break a table. safe_text() masks secrets and makes them one inert line; code()
# also puts that line inside inline code for the Markdown report.
from safe import code, safe_text  # noqa: E402

VERSION = "1.0.0"
HERE = os.path.dirname(os.path.abspath(__file__))
TARGETS_FILE = os.path.join(HERE, "targets.json")
SCORED_RISKS = ("critical", "high", "medium")
TEMP_PREFIX = ".sandbox-check-"
SF_DATALESS = 0x40000000  # macOS: file content lives in the cloud; opening it downloads it
_NONBLOCK, _CLOEXEC = getattr(os, "O_NONBLOCK", 0), getattr(os, "O_CLOEXEC", 0)
READ_FLAGS = os.O_RDONLY | _NONBLOCK | _CLOEXEC
APPEND_FLAGS = os.O_WRONLY | os.O_APPEND | _NONBLOCK | _CLOEXEC  # no O_CREAT, no O_TRUNC
CREATE_FLAGS = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0) | _CLOEXEC
SUDO_PATHS = ("/usr/bin/sudo", "/bin/sudo", "/usr/local/bin/sudo", "/run/wrappers/bin/sudo")
TRUE_PATHS = ("/usr/bin/true", "/bin/true")
LOOKUP_REFUSED = "this shell cannot look the file up"
INSIDE_REFUSED = "inside a folder this shell cannot look into"
REPLACEABLE = "the file is read-only, but its folder is writable, so the file can be replaced"


def find_sudo(exists=os.path.exists):
    """sudo from a system folder only. A repository could plant a fake `sudo` on PATH."""
    return next((path for path in SUDO_PATHS if exists(path)), None)


def sudo_command(exists=os.path.exists):
    """`sudo -n true` with both programs taken from system folders, never from PATH."""
    sudo = find_sudo(exists)
    true = next((path for path in TRUE_PATHS if exists(path)), None)
    return [sudo, "-n", true] if sudo and true else None


def load_targets(path=TARGETS_FILE):
    with open(path, encoding="utf-8") as fh:
        return json.load(fh)


class Probes:
    """Every probe that touches a socket, sudo, or the network. Tests pass fakes."""

    def __init__(self, env):
        self.env = env

    def identity(self):
        uid = os.getuid() if hasattr(os, "getuid") else -1
        euid = os.geteuid() if hasattr(os, "geteuid") else -1
        try:
            import pwd
            user = pwd.getpwuid(uid).pw_name
        except (ImportError, KeyError):
            user = self.env.get("USER") or "unknown"
        return {"user": user, "uid": uid, "euid": euid}

    def unix_connect(self, path):
        """Connect to a local socket and close it at once; nothing is sent. 0 or the errno."""
        if not hasattr(socket, "AF_UNIX"):
            return errno.EAFNOSUPPORT
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.settimeout(2)
        try:
            sock.connect(path)
            return 0
        except socket.timeout:
            return errno.ETIMEDOUT
        except OSError as exc:
            return exc.errno or errno.EIO
        finally:
            sock.close()

    def docker_connect(self, path):
        return self.unix_connect(path)

    def ssh_agent_connect(self, path):
        return self.unix_connect(path)

    def sudo_check(self):
        """'open' when `sudo -n true` works: root without a password prompt."""
        if not find_sudo():
            return "missing"
        command = sudo_command()
        if not command:
            return "unknown"
        try:
            done = subprocess.run(command, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                  stderr=subprocess.DEVNULL, timeout=5)
        except (OSError, subprocess.TimeoutExpired):
            return "unknown"
        return "open" if done.returncode == 0 else "blocked"

    def dns_lookup(self, host):
        try:
            socket.getaddrinfo(host, 443, type=socket.SOCK_STREAM)
            return True
        except OSError:
            return False

    def tcp_connect(self, host, port):
        """Open a TCP connection and close it. No data is sent."""
        try:
            socket.create_connection((host, port), timeout=3).close()
            return True
        except OSError:
            return False


def is_cloud_placeholder(st):
    return bool(getattr(st, "st_flags", 0) & SF_DATALESS)


def is_within(path, base):
    path, base = os.path.normpath(path), os.path.normpath(base)
    return path == base or path.startswith(base.rstrip(os.sep) + os.sep)


def make_check(cid, category, group, risk, target, status, detail, **extra):
    check = {"id": cid, "category": category, "group": group, "risk": risk,
             "target": safe_text(target), "status": status, "detail": detail}
    check.update(extra)
    return check


# --- where targets live -----------------------------------------------------

class Paths:
    """Resolves {placeholders} to absolute paths plus a display form.

    Display forms never contain an environment variable's value: a folder moved
    by CODEX_HOME shows as $CODEX_HOME, the home folder as ~.
    """

    def __init__(self, roots, env, home, project, sysroot):
        self.env, self.project = env, project
        self.values = {"home": home, "project": project, "sysroot": sysroot.rstrip(os.sep)}
        self.labels = {"home": "~", "project": ".", "sysroot": ""}
        for name, spec in roots.items():
            var = spec.get("env")
            if var and env.get(var):
                self.values[name] = self.env_path(env[var])
                self.labels[name] = "$" + var
            elif "default" in spec:
                resolved = self.expand(spec["default"])
                if resolved:
                    self.values[name], self.labels[name] = resolved

    def env_path(self, value):
        if value.startswith("~" + os.sep):
            value = os.path.join(self.values["home"], value[2:])
        return os.path.normpath(os.path.join(self.project, value))

    def expand(self, template):
        """'{home}/.x' -> (absolute path, display) or None when a root is unknown."""
        name, _, rest = template[1:].partition("}")
        if self.values.get(name) is None:
            return None
        return os.path.normpath(self.values[name] + rest), self.labels[name] + rest

    def display_under(self, template, path):
        """Display form of a path found under the root a template names."""
        name = template[1:].partition("}")[0]
        return self.labels[name] + path[len(self.values[name]):]

    def relative(self, path):
        rel = os.path.relpath(path, self.project)
        return rel if rel == "." or rel.startswith("..") else "." + os.sep + rel

    def show(self, path):
        """Display form of any path: relative inside the project, else under the
        most specific known root ($VARIABLE or ~), else as it is."""
        path = os.path.normpath(path)
        if is_within(path, self.project):
            return self.relative(path)
        roots = [n for n, v in self.values.items() if v and n not in ("project", "sysroot") and is_within(path, v)]
        if roots:
            best = max(roots, key=lambda n: len(self.values[n]))
            return self.labels[best] + path[len(self.values[best]):]
        sysroot = self.values["sysroot"]
        if sysroot and is_within(path, sysroot):
            return path[len(sysroot):] or os.sep
        return path


# --- access checks ----------------------------------------------------------

def lookup(path):
    """(status, stat result) where status is None when the path exists."""
    try:
        return None, os.stat(path)
    except (FileNotFoundError, NotADirectoryError):
        return "missing", None
    except PermissionError:
        return "blocked", None
    except OSError:
        return "unknown", None


def refusal(exc):
    """Status for a failed open: a refusal is 'blocked', anything else 'unknown'."""
    if isinstance(exc, PermissionError) or exc.errno == errno.EROFS:
        return "blocked", "refused: " + (errno.errorcode.get(exc.errno) or "denied")
    return "unknown", errno.errorcode.get(exc.errno) or exc.__class__.__name__


def check_read(path):
    """Open the file and close it at once. Not one byte is read."""
    status, st = lookup(path)
    if status:
        return status, LOOKUP_REFUSED if status == "blocked" else ""
    if not stat.S_ISREG(st.st_mode):
        return "missing", "not a regular file"
    if is_cloud_placeholder(st):
        return "skipped", "stored in the cloud; not opened, so it is not downloaded"
    try:
        fd = os.open(path, READ_FLAGS)
    except OSError as exc:
        return refusal(exc)
    os.close(fd)
    return "open", "opened and closed; nothing read"


class Writes:
    """Write checks. They share the list of test files that could not be deleted
    and ask each folder once: can this shell create a file here?"""

    def __init__(self, paths):
        self.paths, self.leftovers, self.folders = paths, [], {}

    def create(self, folder, display):
        """Create one empty, uniquely named file in the folder, then delete it."""
        key = os.path.realpath(folder)
        if key not in self.folders:
            self.folders[key] = self._create(folder, display)
        return self.folders[key]

    def _create(self, folder, display):
        status, st = lookup(folder)
        if status:
            return status, LOOKUP_REFUSED if status == "blocked" else ""
        if not stat.S_ISDIR(st.st_mode):
            return "unknown", "not a folder"
        if is_cloud_placeholder(st):
            return "skipped", "stored in the cloud; left alone"
        name = TEMP_PREFIX + secrets.token_hex(8) + ".tmp"
        path = os.path.join(folder, name)
        try:
            fd = os.open(path, CREATE_FLAGS, 0o600)
        except OSError as exc:
            return refusal(exc)
        try:
            os.close(fd)
        finally:
            try:
                os.unlink(path)
            except OSError:
                self.leftovers.append(safe_text(display.rstrip("/") + "/" + name))
        return "open", "created and deleted one empty test file"

    def append(self, path):
        """Open an existing file for append and close it. Nothing is written."""
        status, st = lookup(path)
        if status:
            return status, LOOKUP_REFUSED if status == "blocked" else ""
        if not stat.S_ISREG(st.st_mode):
            return "unknown", "not a regular file"
        if is_cloud_placeholder(st):
            return "skipped", "stored in the cloud; not opened, so it is not downloaded"
        try:
            fd = os.open(path, APPEND_FLAGS)
        except OSError as exc:
            # EACCES comes from the file's own permissions. A sandbox refuses with
            # EPERM or EROFS, and then the folder test would not speak for this name.
            if exc.errno == errno.EACCES and self.replaceable(path, st):
                return "open", REPLACEABLE
            return refusal(exc)
        os.close(fd)
        return "open", "opened for append and closed; nothing written"

    def replaceable(self, path, st):
        folder = os.path.dirname(path)
        if self.create(folder, self.paths.show(folder))[0] != "open":
            return False
        folder_st = lookup(folder)[1]
        sticky = bool(folder_st and folder_st.st_mode & stat.S_ISVTX)
        return not (sticky and hasattr(os, "geteuid") and st.st_uid != os.geteuid())


def refusing_folder(path):
    """The folder that stopped a refused lookup of path, or None when the refusal
    is about that one name (a sandbox rule on the file itself).

    Walk up to the deepest folder this shell can stat. If that folder will not
    list its files, it is the refusing folder. Otherwise the refusal sits on
    its child on the way down: that child, unless the child is path itself."""
    child, folder = path, os.path.dirname(path)
    while lookup(folder)[0] is not None:
        parent = os.path.dirname(folder)
        if parent == folder:
            return None
        child, folder = folder, parent
    if listable(folder) is not True:
        return folder
    return None if child == path else child


def glob_base(pattern):
    base = []
    for part in pattern.split(os.sep):
        if any(ch in part for ch in "*?["):
            break
        base.append(part)
    return os.sep.join(base) or os.sep


def listable(folder):
    """True, False when access is refused, None when the folder is missing."""
    try:
        os.listdir(folder)
        return True
    except PermissionError:
        return False
    except OSError:
        return None


class Refused:
    """One blocked row per folder that refuses lookups; files inside it are 'hidden'."""

    def __init__(self, paths, category):
        self.paths, self.category, self.folders = paths, category, set()

    def settle(self, entry, risk, path, status, detail, found):
        """Turn a refused lookup of a file inside a refusing folder into 'hidden'."""
        if status == "blocked" and detail == LOOKUP_REFUSED:
            folder = refusing_folder(path)
            if folder:
                self.row(entry, risk, folder, found)
                return "hidden", INSIDE_REFUSED
        return status, detail

    def row(self, entry, risk, folder, found):
        if folder not in self.folders:
            self.folders.add(folder)
            found.append(make_check(entry["id"], self.category, entry["group"], risk, self.paths.show(folder),
                                    "blocked", "this shell cannot look inside this folder"))


# --- secret files -----------------------------------------------------------

def secret_candidates(entry, paths, env):
    """Yield (path, display, kind) for one secret_files entry.

    kind "fixed" is listed even when missing; "found" (a glob match) only when
    present; "refused" is a folder that would not list its files.
    """
    for template in entry.get("paths", []):
        resolved = paths.expand(template)
        if resolved:
            yield resolved[0], resolved[1], "fixed"
    for var in entry.get("env_paths", []):
        parts = [p for p in env.get(var, "").split(os.pathsep) if p]
        for i, part in enumerate(parts, 1):
            label = "$" + var if len(parts) == 1 else "$%s (%d of %d)" % (var, i, len(parts))
            yield paths.env_path(part), label, "fixed"
    excluded = entry.get("exclude", [])
    for template in entry.get("globs", []):
        resolved = paths.expand(template)
        if not resolved:
            continue
        base = glob_base(resolved[0])
        ok = listable(base)
        if ok is False:
            yield base, paths.display_under(template, base), "refused"
        elif ok:
            matches = [m for m in sorted(glob.glob(resolved[0]))
                       if not any(fnmatch.fnmatch(os.path.basename(m), x) for x in excluded)]
            hidden = any(ch in part for part in resolved[0].split(os.sep)[:-1] for ch in "*?[")
            for i, match in enumerate(matches, 1):
                # A folder name matched by a wildcard can be an account name: show the pattern instead.
                shown = "%s (%d of %d)" % (resolved[1], i, len(matches)) if hidden else paths.display_under(template, match)
                yield match, shown, "found"
    if entry.get("pub_siblings"):
        resolved = paths.expand(entry["pub_siblings"])
        if resolved and listable(resolved[0]):
            names = set(os.listdir(resolved[0]))
            for name in sorted(names):
                if name.endswith(".pub") and name[:-4] in names:
                    path = os.path.join(resolved[0], name[:-4])
                    yield path, paths.display_under(entry["pub_siblings"], path), "found"
    if entry.get("env_walk"):
        for folder in walk_up(paths.project, paths.values["home"]):
            for name in env_file_names(folder, entry):
                path = os.path.join(folder, name)
                yield path, paths.relative(path), "found"


def walk_up(project, home):
    """The project and its parents; stops at the home folder when inside it."""
    folders, current = [], project
    inside_home = is_within(project, home)
    while True:
        folders.append(current)
        parent = os.path.dirname(current)
        if parent == current or (inside_home and os.path.normpath(current) == os.path.normpath(home)):
            return folders
        current = parent


def env_file_names(folder, entry):
    try:
        names = sorted(os.listdir(folder))
    except PermissionError:
        names = entry.get("walk_names", [])
    except OSError:
        return []
    return [n for n in names
            if any(fnmatch.fnmatch(n, p) for p in entry["env_walk"])
            and not any(fnmatch.fnmatch(n, x) for x in entry.get("exclude", []))]


def probe_secret_files(targets, paths, env):
    found, refused = [], Refused(paths, "secret-files")
    for entry in targets["secret_files"]:
        seen = set()
        for path, display, kind in secret_candidates(entry, paths, env):
            if path in seen:
                continue
            seen.add(path)
            if kind == "refused":
                refused.row(entry, "high", refusing_folder(path) or path, found)
                continue
            status, detail = refused.settle(entry, "high", path, *check_read(path), found=found)
            if status == "missing" and kind == "found":
                continue
            found.append(make_check(entry["id"], "secret-files", entry["group"], "high", display, status, detail))
    return found


# --- trust files: things a program outside the sandbox later runs ------------

def find_git_dir(project, home):
    """The folder that holds hooks and config for the repository around project."""
    for folder in walk_up(project, home):
        dot_git = os.path.join(folder, ".git")
        if os.path.isdir(dot_git):
            return dot_git
        if os.path.isfile(dot_git):
            return git_common_dir(dot_git, folder)
    return None


def git_common_dir(pointer, folder):
    """Follow a worktree or submodule '.git' file to the folder git really uses."""
    try:
        with open(pointer, encoding="utf-8") as fh:
            first = fh.readline().strip()
    except OSError:
        return None
    if not first.startswith("gitdir:"):
        return None
    gitdir = os.path.normpath(os.path.join(folder, first[len("gitdir:"):].strip()))
    try:
        with open(os.path.join(gitdir, "commondir"), encoding="utf-8") as fh:
            return os.path.normpath(os.path.join(gitdir, fh.readline().strip()))
    except OSError:
        return gitdir


def probe_trust_files(targets, paths, platform_name, writes):
    found, refused = [], Refused(paths, "trust-files")
    for entry in targets["trust_files"]:
        if entry.get("os") and entry["os"] != platform_name:
            continue
        for template in entry["paths"]:
            resolved = paths.expand(template)
            if not resolved:
                continue
            path, display = resolved
            result = writes.create(path, display) if entry["kind"] == "dir" else writes.append(path)
            status, detail = refused.settle(entry, entry["risk"], path, *result, found=found)
            found.append(make_check(entry["id"], "trust-files", entry["group"], entry["risk"], display,
                                    status, detail, why=entry["why"]))
    return found


def probe_folders(targets, paths, writes):
    """Can the shell create files in the home folder, the parent folder, the project?

    Returns the checks and the ids of outside folders that are the project itself."""
    found, seen, same_as_project = [], set(), set()
    project = os.path.realpath(paths.project)
    for entry in targets["folders"]:
        path = paths.expand(entry["path"])[0]
        real = os.path.realpath(path)
        if real in seen and entry["id"] != "project-folder":
            continue
        seen.add(real)
        if entry["category"] == "outside-project" and real == project:
            same_as_project.add(entry["id"])
        status, detail = writes.create(path, entry["display"])
        found.append(make_check(entry["id"], entry["category"], entry["group"], entry["risk"],
                                entry["display"], status, detail))
    return found, same_as_project


# --- environment, sockets, privilege, network -------------------------------

def probe_env(targets, env):
    """Names and value lengths of variables whose names look like secrets."""
    include = re.compile(targets["secret_env"]["include"], re.IGNORECASE)
    exclude = re.compile(targets["secret_env"]["exclude"], re.IGNORECASE)
    found = [{"name": safe_text(name), "length": len(value)} for name, value in env.items()
             if value and include.search(name) and not exclude.search(name)]
    found.sort(key=lambda item: item["name"])
    check = make_check("secret-env", "secret-env", "Secret-like environment variables", "medium",
                       "%d variables" % len(found), "open" if found else "blocked",
                       "names and lengths only" if found else "no secret-like names")
    return found, [check]


def probe_sockets(targets, paths, env, probes):
    """Docker or Podman sockets and the SSH agent: connect, then close."""
    candidates = [paths.expand(t) for t in targets["docker_sockets"]]
    docker_host = env.get("DOCKER_HOST", "")
    if docker_host.startswith("unix://"):
        candidates.append((docker_host[len("unix://"):], "$DOCKER_HOST"))
    found, seen = [], set()
    for resolved in candidates:
        if not resolved or not os.path.exists(resolved[0]):
            continue
        real = os.path.realpath(resolved[0])
        if real in seen:
            continue
        seen.add(real)
        code = probes.docker_connect(resolved[0])
        found.append(socket_check("docker", "Docker control", "critical", resolved[1], code))
    if not found:
        found.append(make_check("docker", "docker", "Docker control", "critical", "Docker socket",
                                "missing", "no socket found"))
    agent = env.get("SSH_AUTH_SOCK", "")
    if agent and os.path.exists(agent):
        found.append(socket_check("ssh-agent", "SSH agent", "high", "$SSH_AUTH_SOCK",
                                  probes.ssh_agent_connect(agent)))
    else:
        found.append(make_check("ssh-agent", "ssh-agent", "SSH agent", "high", "$SSH_AUTH_SOCK",
                                "missing", "no agent socket"))
    return found


def socket_check(cid, group, risk, target, code):
    """A socket probe's errno becomes a status: a stale socket file is not a refusal."""
    name = errno.errorcode.get(code, str(code))
    if code == 0:
        status, detail = "open", "connected and closed; nothing sent"
    elif code in (errno.ECONNREFUSED, errno.ENOENT):
        status, detail = "missing", "socket file exists but nothing is listening now (%s)" % name
    elif code in (errno.EACCES, errno.EPERM):
        status, detail = "blocked", "refused: " + name
    else:
        status, detail = "unknown", name
    return make_check(cid, cid, group, risk, target, status, detail)


def probe_privilege(identity, probes, skip_sudo):
    root = identity["euid"] == 0
    sudo = "skipped" if skip_sudo else probes.sudo_check()
    return [
        make_check("root", "privilege", "Root and sudo", "critical", "user id %s" % identity["euid"],
                   "open" if root else "blocked", "runs as root" if root else "not root"),
        make_check("sudo", "privilege", "Root and sudo", "critical", "sudo -n true", sudo,
                   {"open": "sudo works without a password", "blocked": "sudo needs a password",
                    "missing": "sudo is not installed", "skipped": "skipped (--skip-sudo)"}.get(sudo, "")),
    ]


def probe_network(targets, probes, network):
    net = targets["network"]
    plan = [("dns", "info", host, (host,)) for host in net["dns"]]
    plan += [("outbound", "medium", "%s:%d" % (h, p), (h, p)) for h, p in net["outbound"]]
    plan.append(("metadata", "high", "%s:%d" % tuple(net["metadata"]), tuple(net["metadata"])))
    found = []
    for cid, risk, target, args in plan:
        if not network:
            status, detail = "skipped", "not checked; run with --network"
        else:
            ok = probes.dns_lookup(*args) if cid == "dns" else probes.tcp_connect(*args)
            status = "open" if ok else "blocked"
            detail = ("resolved" if cid == "dns" else "connected and closed; nothing sent") if ok else "failed"
        found.append(make_check(cid, "network", "Network", risk, target, status, detail))
    return found


def where_it_runs(targets, paths, env, identity, platform_name):
    signs = targets["container_signs"]
    containers = [shown for path, shown in map(paths.expand, signs["files"]) if os.path.exists(path)]
    try:
        with open(paths.expand(signs["cgroup_file"])[0], encoding="utf-8", errors="replace") as fh:
            cgroup = fh.read(65536)
    except OSError:
        cgroup = ""
    containers += ["cgroup: " + word for word in signs["cgroup_words"] if word in cgroup]
    containers += [name for name in signs["env"] if env.get(name)]
    sandbox = []
    for marker in targets["sandbox_markers"]:
        value = env.get(marker["name"])
        if value:
            shown = "%s=%s" % (marker["name"], value) if value in marker["values"] else marker["name"] + " (set)"
            sandbox.append(shown)
    if platform_name == "darwin":
        system = "macOS " + (platform.mac_ver()[0] or platform.release())
    else:
        system = "%s %s" % (platform.system(), platform.release())
    return {"user": safe_text(identity["user"]), "uid": identity["uid"], "root": identity["euid"] == 0,
            "os": safe_text(system), "python": platform.python_version(),
            "harnesses": [h for h, names in targets["harness_markers"].items() if any(env.get(n) for n in names)],
            "sandbox_signs": sandbox, "container_signs": containers}


# --- what the settings claim ------------------------------------------------

HARNESS_NAMES = {"claude-code": "Claude Code", "codex": "Codex", "gemini-cli": "Gemini CLI",
                 "cursor": "Cursor", "opencode": "OpenCode"}
RISK_ORDER = {"critical": 0, "high": 1, "medium": 2, "info": 3}
GEMINI_SANDBOX_KINDS = ("docker", "podman", "sandbox-exec", "runsc", "lxc")


def parse_settings(path):
    """(data, problem). Callers keep only the sandbox keys; nothing else is printed."""
    try:
        if path.endswith(".toml"):
            if tomllib is None:
                return None, "parsing TOML needs Python 3.11 or newer"
            with open(path, "rb") as fh:
                return tomllib.load(fh), None
        with open(path, encoding="utf-8") as fh:
            return json.load(fh), None
    except OSError:
        return None, "it could not be read"
    except ValueError:
        return None, "it could not be parsed"


def read_settings(targets, paths, platform_name, notes):
    found = []
    for harness, layers in targets["settings"].items():
        name, loaded = HARNESS_NAMES[harness], []
        for layer in layers:
            if layer.get("os") and layer["os"] != platform_name:
                continue
            files = []
            if "path" in layer:
                files.append(paths.expand(layer["path"]))
            else:
                folder, shown = paths.expand(layer["dropins"])
                ok = listable(folder)
                if ok:
                    files += [(os.path.join(folder, n), shown + "/" + n)
                              for n in sorted(os.listdir(folder)) if n.endswith(".json")]
                elif ok is False:
                    notes.append("%s settings folder %s could not be listed, so its files were not read." % (
                        name, code(shown)))
            for resolved in files:
                if not resolved or not os.path.isfile(resolved[0]):
                    continue
                shown = safe_text(resolved[1])
                data, problem = parse_settings(resolved[0])
                if problem or not isinstance(data, dict):
                    notes.append("%s settings file %s was skipped: %s." % (
                        name, code(shown), problem or "it is not an object"))
                    continue
                loaded.append((layer["layer"], shown, data))
        if loaded:
            found.append({"harness": harness, "sources": [shown for _, shown, _ in loaded],
                          "claims": CLAIMS[harness](loaded)})
    return found


def listed(value):
    """The strings in a settings list; anything that is not a list counts as empty."""
    return [x for x in value if isinstance(x, str)] if isinstance(value, list) else []


def claude_claims(layers):
    """Merge sandbox keys the way Claude Code does: booleans from the highest
    layer that sets them, lists merged. Layers arrive lowest first."""
    claims = {"sandbox_enabled": None, "allow_unsandboxed": None, "filesystem_disabled": None,
              "excluded_commands": [], "docker_socket_allowed": False, "credential_rules": 0}
    excluded = set()
    for layer, _, data in layers:
        box = data.get("sandbox")
        if not isinstance(box, dict):
            continue
        if isinstance(box.get("enabled"), bool):
            claims["sandbox_enabled"] = box["enabled"]
        if isinstance(box.get("allowUnsandboxedCommands"), bool):
            claims["allow_unsandboxed"] = box["allowUnsandboxedCommands"]
        files = box.get("filesystem")
        if layer in ("user", "managed") and isinstance(files, dict) and isinstance(files.get("disabled"), bool):
            claims["filesystem_disabled"] = files["disabled"]  # project settings cannot set this key
        excluded.update(safe_text(x) for x in listed(box.get("excludedCommands")))
        net = box.get("network")
        if isinstance(net, dict):
            sockets = listed(net.get("allowUnixSockets"))
            if net.get("allowAllUnixSockets") is True or any(x.endswith(("docker.sock", "podman.sock")) for x in sockets):
                claims["docker_socket_allowed"] = True
        creds = box.get("credentials")
        if isinstance(creds, dict):
            claims["credential_rules"] += sum(len(creds.get(k)) for k in ("files", "envVars")
                                              if isinstance(creds.get(k), list))
    claims["excluded_commands"] = sorted(excluded)
    return claims


def codex_claims(layers):
    claims = {"sandbox_mode": None, "network_access": None, "ignore_default_excludes": None}
    for _, _, data in layers:
        if isinstance(data.get("sandbox_mode"), str):
            claims["sandbox_mode"] = safe_text(data["sandbox_mode"])
        write = data.get("sandbox_workspace_write")
        if isinstance(write, dict) and isinstance(write.get("network_access"), bool):
            claims["network_access"] = write["network_access"]
        policy = data.get("shell_environment_policy")
        if isinstance(policy, dict) and isinstance(policy.get("ignore_default_excludes"), bool):
            claims["ignore_default_excludes"] = policy["ignore_default_excludes"]
    return claims


def gemini_claims(layers):
    claims = {"sandbox": None}
    for _, _, data in layers:
        tools = data.get("tools")
        value = tools.get("sandbox") if isinstance(tools, dict) else None
        if isinstance(value, bool):
            claims["sandbox"] = value
        elif isinstance(value, str):
            claims["sandbox"] = value if value in GEMINI_SANDBOX_KINDS else bool(value)
    return claims


CLAIMS = {"claude-code": claude_claims, "codex": codex_claims, "gemini-cli": gemini_claims}


def sandbox_state(harness, claims, signs):
    """(is a sandbox claimed for this shell, the sentence that says why)."""
    if harness == "claude-code":
        return claims.get("sandbox_enabled") is True, "Claude Code settings turn the sandbox on"
    if harness == "codex":
        mode = claims.get("sandbox_mode")
        if mode in ("read-only", "workspace-write"):
            return True, "Codex settings say %s" % code(mode)
        return any(s.startswith("CODEX_SANDBOX") for s in signs), "Codex marks this shell as sandboxed"
    if harness == "gemini-cli":
        if claims.get("sandbox"):
            return True, "Gemini CLI settings turn the sandbox on"
        return any(s.startswith("SANDBOX") for s in signs), "Gemini CLI marks this shell as sandboxed"
    return any(s.startswith("CURSOR_SANDBOX") for s in signs), "Cursor marks this shell as sandboxed"


def sandbox_off(harness, claims):
    rights = "so shell commands run with your full user rights"
    if harness == "claude-code":
        return ("The Claude Code sandbox is off: no settings file the probe read sets sandbox.enabled to "
                "true, %s." % rights)
    if harness == "codex" and claims.get("sandbox_mode") == "danger-full-access":
        return "Codex settings set sandbox_mode to danger-full-access, %s." % rights
    if harness == "gemini-cli":
        return "The Gemini CLI sandbox is off: no tools.sandbox setting and no SANDBOX marker, %s." % rights
    return None


def protects(harness, check, trust, same_as_project):
    """Would this harness's sandbox block the write by default?"""
    if check["category"] == "outside-project":
        return check["id"] not in same_as_project  # the project itself is writable in every sandbox
    entry = trust.get(check["id"]) if check["category"] == "trust-files" else None
    return bool(entry) and (entry.get("outside", False) or harness in entry.get("protected_by", []))


def quoted(items):
    return listing([code(item) for item in items])


def find_gaps(settings, checks, where, targets, env_found, same_as_project, notes):
    claims_by = {s["harness"]: s["claims"] for s in settings}
    trust = {e["id"]: e for e in targets["trust_files"]}
    opened = [c for c in checks if c["status"] == "open"]
    home = [c["status"] for c in checks if c["id"] == "home-folder"]
    gaps = []
    for harness in where["harnesses"]:
        claims, name = claims_by.get(harness, {}), HARNESS_NAMES[harness]

        def add(risk, message):
            gaps.append({"harness": harness, "risk": risk, "message": message})

        active, basis = sandbox_state(harness, claims, where["sandbox_signs"])
        message = None if active else sandbox_off(harness, claims)
        if message and harness == "claude-code" and home == ["blocked"]:
            # --settings, MDM, and server-managed settings are out of reach, and the home folder refused a write.
            notes.append("Claude Code: no settings file the probe read sets sandbox.enabled to true, yet new files "
                         "in your home folder were blocked, so a sandbox from --settings or managed settings may be on.")
        elif message:
            add("critical", message)
        protected = sorted((c for c in opened if protects(harness, c, trust, same_as_project)), key=lambda c: (
            RISK_ORDER[c["risk"]], not trust.get(c["id"], {}).get("outside", c["category"] == "outside-project")))
        if active and protected:
            add("critical", "%s, yet %s is writable from this shell (%d places the sandbox should protect). "
                "Either this command ran outside the sandbox, or the sandbox allows more than its "
                "settings suggest." % (basis, code(protected[0]["target"]), len(protected)))
        handoff = [c["target"] for c in opened
                   if c["category"] == "trust-files" and not protects(harness, c, trust, same_as_project)]
        if active and handoff:
            add("high", "The %s sandbox leaves %s writable, and programs outside the sandbox run what is "
                "there later." % (name, quoted(handoff)))
        if active and any(c["id"] == "docker" for c in opened):
            add("critical", "The %s sandbox lets this shell connect to the Docker socket, and Docker can start "
                "a container that mounts your home folder outside any sandbox." % name)
        if harness == "claude-code" and active:
            claude_gaps(claims, checks, add)
        if harness == "codex" and env_found and claims.get("ignore_default_excludes") is not False:
            add("medium", "Codex passes variables named like KEY, SECRET, or TOKEN to shell commands unless "
                "shell_environment_policy.ignore_default_excludes is false; %d are visible here." % len(env_found))
    return sorted(gaps, key=lambda g: RISK_ORDER[g["risk"]])


def claude_gaps(claims, checks, add):
    docker = [c for c in checks if c["id"] == "docker" and c["status"] in ("open", "blocked")]
    if docker and any(x.strip().startswith("docker") for x in claims.get("excluded_commands", [])):
        add("critical", "sandbox.excludedCommands runs docker outside the sandbox, so the agent can control "
            "Docker even when this probe could not connect.")
    if claims.get("docker_socket_allowed"):
        add("critical", "sandbox.network allows the Docker socket, so sandboxed commands can control Docker.")
    if claims.get("filesystem_disabled") is True:
        add("critical", "sandbox.filesystem.disabled is true, so sandboxed commands can write anywhere you can.")
    readable = [c for c in checks if c["category"] == "secret-files" and c["status"] == "open"]
    if readable:
        add("high", "%s readable from this shell; the sandbox blocks only files listed in "
            "sandbox.credentials.files with mode deny." % (plural(len(readable), "secret file") +
                                                          (" is" if len(readable) == 1 else " are")))
    if claims.get("allow_unsandboxed") is not False:
        add("medium", "sandbox.allowUnsandboxedCommands is not false, so Claude can retry a blocked command "
            "outside the sandbox after asking you.")


def listing(items, limit=3):
    shown = ", ".join(items[:limit])
    return shown + (" and %d more" % (len(items) - limit) if len(items) > limit else "")


# --- score and report ------------------------------------------------------

def score(checks):
    by_risk = {risk: {"open": 0, "checked": 0} for risk in SCORED_RISKS}
    for c in checks:
        if c["risk"] in by_risk and c["status"] in ("open", "blocked"):
            by_risk[c["risk"]]["checked"] += 1
            by_risk[c["risk"]]["open"] += c["status"] == "open"
    return {"open": sum(v["open"] for v in by_risk.values()),
            "checked": sum(v["checked"] for v in by_risk.values()),
            "by_risk": by_risk}


def plural(n, word):
    return "%d %s%s" % (n, word, "" if n == 1 else "s")


def make_headline(checks, env_found, harnesses):
    """One sentence: secret files first, then the worst places the shell reached."""
    subject = "Your agent" if harnesses else "This shell"
    opened = [c for c in checks if c["status"] == "open"]
    ids, groups = {c["id"] for c in opened}, {c["group"] for c in opened}
    secret_files = sum(c["category"] == "secret-files" for c in opened)
    harness_files = sum(c["group"] == "Harness settings and hooks" for c in opened)
    phrases = [
        (secret_files, "open " + plural(secret_files, "secret file")),
        ("root" in ids, "act as root"),
        ("sudo" in ids and "root" not in ids, "run sudo without a password"),
        ("docker" in ids, "control Docker"),
        ("git-hooks" in ids, "write to your git hooks"),
        ("git-hooks" not in ids and bool({"git-config", "gitconfig"} & ids), "change your git config"),
        ("Shell startup files" in groups, "change your shell startup files"),
        ("Login items" in groups, "add programs that start at login"),
        ("authorized-keys" in ids, "add SSH login keys"),
        (harness_files, "change " + plural(harness_files, "harness settings file")),
        (bool({"vscode", "venv-bin"} & ids), "change files your editor runs"),
        ("home-folder" in ids, "write to files in your home folder"),
        ("parent-folder" in ids, "write to files in the parent folder"),
        ("ssh-agent" in ids, "use your SSH agent"),
        ("metadata" in ids, "reach the cloud metadata service"),
        ("outbound" in ids, "connect to the internet directly"),
        (env_found, "see " + plural(len(env_found), "secret-like environment variable")),
    ]
    parts = [text for present, text in phrases if present][:3]
    total = score(checks)
    if not parts and total["open"]:
        return "%s can reach %d of %d checked targets; the table below lists them." % (
            subject, total["open"], total["checked"])
    if not parts:
        return "%s could not reach any secret file, trust file, Docker socket, or sudo: 0 of %d checks open." % (
            subject, total["checked"])
    if len(parts) == 3:
        return "%s can %s, %s, and %s." % (subject, parts[0], parts[1], parts[2])
    return "%s can %s." % (subject, " and ".join(parts))


def table_rows(result):
    """(risk, area, open, found) per group, worst risk first."""
    groups = {}
    for c in result["checks"]:
        if c["risk"] in SCORED_RISKS and c["status"] in ("open", "blocked"):
            groups.setdefault((RISK_ORDER[c["risk"]], c["risk"], c["group"]), []).append(c)
    rows = []
    for (_, risk, group), items in sorted(groups.items(), key=lambda kv: kv[0][0]):
        opened = [c["target"] for c in items if c["status"] == "open"]
        if group == "Secret-like environment variables":
            count, found = plural(len(result["env"]), "name"), "listed below" if opened else "none"
        else:
            count = "%d of %d" % (len(opened), len(items))
            found = quoted(opened)
        rows.append((risk.capitalize(), group, count, found or "all blocked"))
    if not result["network_checked"]:
        rows.append(("Info", "Network", "not checked", "run again with `--network` to test it"))
    return rows


def where_line(result):
    w = result["where"]
    agents = ", ".join(HARNESS_NAMES.get(h, h) for h in w["harnesses"])
    status = {c["id"]: c["status"] for c in result["checks"] if c["category"] in ("project", "outside-project")}
    parts = ["user %s (uid %s%s) on %s" % (code(w["user"]), w["uid"], ", root" if w["root"] else "", code(w["os"])),
             "agent: " + (agents or "none found, so this looks like your own terminal"),
             "container signs: " + (", ".join(w["container_signs"]) or "none"),
             "sandbox markers: " + (", ".join(w["sandbox_signs"]) or "none"),
             "new files in the project: " + ("allowed" if status.get("project-folder") == "open" else "blocked"),
             "new files in your home folder: " + ("allowed" if status.get("home-folder") == "open" else "blocked")]
    return "; ".join(parts)


def render_markdown(result):
    s, by = result["score"], result["score"]["by_risk"]
    lines = ["**%s**" % result["headline"], "",
             "%d of %d checks open: critical %d of %d, high %d of %d, medium %d of %d." % (
                 s["open"], s["checked"], by["critical"]["open"], by["critical"]["checked"],
                 by["high"]["open"], by["high"]["checked"], by["medium"]["open"], by["medium"]["checked"]),
             "", "Where it runs: %s." % where_line(result), "",
             "| Risk | Area | Open | Found |", "|---|---|---|---|"]
    lines += ["| %s | %s | %s | %s |" % row for row in table_rows(result)]
    if result["env"]:
        lines += ["", "Secret-like environment variables (names and lengths only): " + ", ".join(
            "%s (%d characters)" % (code(e["name"]), e["length"]) for e in result["env"]) + "."]
    if result["gaps"]:
        lines += ["", "Settings compared with what the probe found:"]
        lines += ["- %s (%s): %s" % (HARNESS_NAMES[g["harness"]], g["risk"], g["message"]) for g in result["gaps"]]
    if result["leftovers"]:
        lines += ["", "These test files could not be deleted; remove them by hand: " + quoted(result["leftovers"]) + "."]
    if result["notes"]:
        lines += ["", "Notes:"] + ["- " + note for note in result["notes"]]
    lines += ["", "How it checks: secret files are opened and closed without reading a byte, existing files "
              "are opened for append and closed without writing, and a folder gets one empty test file that "
              "is deleted at once. Targets that do not exist are listed only in `--json` output."]
    return "\n".join(lines) + "\n"


def run(project, env=None, probes=None, network=False, sysroot="/", platform_name=None, skip_sudo=False):
    env = os.environ if env is None else env
    probes = probes or Probes(env)
    platform_name = platform_name or sys.platform
    home = os.path.abspath(env.get("HOME") or os.path.expanduser("~"))
    project = os.path.abspath(project)
    targets = load_targets()
    paths = Paths(targets["roots"], env, home, project, sysroot)
    identity = probes.identity()
    notes, writes = [], Writes(paths)
    git_dir = find_git_dir(project, home)
    if git_dir:
        paths.values["git"], paths.labels["git"] = git_dir, paths.relative(git_dir)
    else:
        notes.append("The project is not inside a git repository, so its .git/hooks and .git/config were not checked.")
    proxies = [name for name in targets["network"]["proxy_env"] if env.get(name)]
    if proxies:
        notes.append("Proxy variables are set (%s): tools that honor them may reach the network "
                     "through the proxy even when direct connections fail." % ", ".join(proxies))
    env_found, checks = probe_env(targets, env)
    checks = probe_secret_files(targets, paths, env) + checks
    checks += probe_trust_files(targets, paths, platform_name, writes)
    folders, same_as_project = probe_folders(targets, paths, writes)
    checks += folders
    if "home-folder" in same_as_project:
        notes.append("The home folder is the project folder here, so a sandbox that allows the project also "
                     "allows new files in the home folder.")
    checks += probe_sockets(targets, paths, env, probes)
    checks += probe_privilege(identity, probes, skip_sudo)
    checks += probe_network(targets, probes, network)
    unknown = sum(c["status"] == "unknown" for c in checks)
    if unknown:
        notes.append("%s hit an unexpected error and %s not scored; the detail field in --json says why." % (
            plural(unknown, "check"), "is" if unknown == 1 else "are"))
    where = where_it_runs(targets, paths, env, identity, platform_name)
    settings = read_settings(targets, paths, platform_name, notes)
    gaps = find_gaps(settings, checks, where, targets, env_found, same_as_project, notes)
    return {"tool": "sandbox-check", "version": VERSION,
            "headline": make_headline(checks, env_found, where["harnesses"]),
            "where": where, "score": score(checks), "checks": checks, "env": env_found,
            "settings": settings, "gaps": gaps,
            "network_checked": bool(network), "notes": notes, "leftovers": writes.leftovers}


def main(argv=None, env=None, probes=None, sysroot="/", platform_name=None):
    parser = argparse.ArgumentParser(
        prog="probe.py", description=__doc__.strip().split("\n\n")[0],
        epilog="Exit codes: 0 done, 1 a check at or above --fail-on is open, 2 usage error.")
    parser.add_argument("--project", default=".", help="the folder the agent works in (default: current folder)")
    parser.add_argument("--network", action="store_true",
                        help="also test DNS and direct TCP connections to github.com:443, pypi.org:443, and "
                             "the cloud metadata address 169.254.169.254:80 (3-second timeout, no data sent)")
    parser.add_argument("--skip-sudo", action="store_true",
                        help="do not run `sudo -n true`; on shared machines sudo may log or report the attempt")
    parser.add_argument("--json", action="store_true", help="print machine-readable JSON")
    parser.add_argument("--out", metavar="PATH", help="write the report to this file instead of printing it")
    parser.add_argument("--fail-on", choices=SCORED_RISKS,
                        help="exit 1 when any check at this risk level or worse is open")
    args = parser.parse_args(argv)
    if not os.path.isdir(args.project):
        print("error: project folder not found: %s" % args.project, file=sys.stderr)
        return 2
    result = run(args.project, env=env, probes=probes, network=args.network, sysroot=sysroot,
                 platform_name=platform_name, skip_sudo=args.skip_sudo)
    text = json.dumps(result, indent=2, ensure_ascii=False) + "\n" if args.json else render_markdown(result)
    if args.out:
        try:
            with open(args.out, "w", encoding="utf-8") as fh:
                fh.write(text)
        except OSError as exc:
            print("error: cannot write the report to %s: %s" % (args.out, exc.strerror or exc), file=sys.stderr)
            return 2
        print("Report written to %s" % args.out)
    else:
        sys.stdout.write(text)
    if args.fail_on and any(c["status"] == "open" and RISK_ORDER[c["risk"]] <= RISK_ORDER[args.fail_on]
                            for c in result["checks"]):
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
