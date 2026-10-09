#!/usr/bin/env python3
"""A minimal MCP client over stdio that lists one server's tools.

It speaks both protocol eras of the MCP specification (revision 2026-07-28):
- modern servers answer a `server/discover` probe, and every request carries
  its protocol version in `_meta`;
- legacy servers (revision 2025-11-25 and earlier) need the `initialize`
  handshake followed by `notifications/initialized`.
The client probes first, falls back to `initialize` on any other answer or on
silence, pages through `tools/list` by cursor, and then shuts the server down:
close its input, wait, terminate, kill.

Usage:
    python3 mcp_client.py [--timeout 20] [--json] -- <server command> [args...]

Standard library only, Python 3.9+. It starts only the command you give it.
Exit codes: 0 tools listed, 2 usage error or the server could not be listed.
"""

from __future__ import annotations

import argparse
import json
import os
import queue
import re
import signal
import subprocess
import sys
import threading
import time
import unicodedata
from typing import Optional

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from safe import code  # noqa: E402
from safe import redact as shared_redact  # noqa: E402

MODERN_VERSION = "2026-07-28"
LEGACY_VERSION = "2025-11-25"
CLIENT_INFO = {"name": "tool-design-checker", "version": "1.0.0"}
UNSUPPORTED_VERSION = -32022
STDERR_KEEP = 4000
EXCERPT = 160
MAX_SERVER_REQUESTS = 100
MAX_LINE = 4 << 20  # one JSON-RPC message; a longer line is dropped as noise
MIN_SECRET = 6      # shorter mask values would hide ordinary words
STOP_WAIT = 1.0     # seconds to wait after closing a server's input, and again after SIGTERM
# Characters that draw nothing but are not control characters: Hangul fillers, the
# blank braille pattern, the combining grapheme joiner, Khmer inherent vowels, and
# variation selectors. They can hide text inside a line that looks empty.
BLANK_GLYPHS = {"\u3164", "\u115f", "\u1160", "\uffa0", "\u2800", "\u034f", "\u17b4", "\u17b5"}

# Shapes the shared redact() in safe.py does not mask: sk- and glpat- keys of 16 to 19
# characters, bearer and basic tokens of 6 to 15 characters, and key, token, or auth
# values written after a space or with 4 or 5 characters.
EXTRA_PATTERNS = [
    re.compile(r"\bsk-[A-Za-z0-9_-]{16,}"),
    re.compile(r"\bglpat-[A-Za-z0-9_-]{16,}"),
]
BEARER = re.compile(r"(?i)\b(bearer|basic)\s+[A-Za-z0-9._~+/=-]{6,}")
KEY_VALUE = re.compile(
    r"(?i)\b([a-z0-9_.-]*(?:key|token|secret|passw(?:or)?d|pwd|auth|credential)s?)"
    r"(\s*[:=]\s*|\s+)([\"']?)([^\s\"',;&]{4,})")


class McpError(Exception):
    """Listing failed. kind: launch | timeout | exited | protocol."""

    def __init__(self, kind: str, message: str):
        super().__init__(message)
        self.kind = kind


def redact(text: str, mask=()) -> str:
    """Mask known secret values (mask: for example a server's configured env values),
    the extra shapes above, and then every shape the shared redact() in safe.py knows."""
    for value in sorted({m for m in mask if m and len(m) >= MIN_SECRET}, key=len, reverse=True):
        text = text.replace(value, "***")
    text = BEARER.sub(lambda m: m.group(1) + " ***", text)
    text = KEY_VALUE.sub(lambda m: m.group(1) + m.group(2) + m.group(3) + "***", text)
    for pattern in EXTRA_PATTERNS:
        text = pattern.sub("***", text)
    return shared_redact(text)


def invisible(ch: str) -> bool:
    return (not ch.isprintable() or ch in BLANK_GLYPHS or "\ufe00" <= ch <= "\ufe0f"
            or unicodedata.category(ch) == "Cn")


def inert(text: str) -> str:
    """One plain line: invisible and control characters and line breaks become spaces,
    backticks and table pipes are replaced, and runs of spaces collapse."""
    text = "".join(" " if invisible(ch) else ch for ch in text)
    return " ".join(text.replace("`", "'").replace("|", "/").split())


def safe_text(text, limit: int = EXCERPT, mask=()) -> str:
    """Make untrusted text (a tool name or description, a server's message, a config
    value) safe to show in a report: secrets masked, the text made one inert line (see
    inert()), and the result cut to `limit` characters. A hostile server can write
    instructions into its descriptions; this keeps them one inert line."""
    text = inert(redact(text if isinstance(text, str) else str(text), mask))
    return text if len(text) <= limit else text[:max(limit - 3, 0)] + "..."


def excerpt(text: str, mask=()) -> str:
    return safe_text(text, EXCERPT, mask)


def inline(text, limit: int = EXCERPT, mask=()) -> str:
    """Untrusted text for a Markdown report or a printed line: safe_text() with the mask,
    then code() from safe.py, which puts it inside inline code so links, HTML, and bare
    URLs stay literal."""
    return code(safe_text(text, limit, mask), limit)


def modern_meta(version: str) -> dict:
    return {
        "io.modelcontextprotocol/protocolVersion": version,
        "io.modelcontextprotocol/clientInfo": dict(CLIENT_INFO),
        "io.modelcontextprotocol/clientCapabilities": {},
    }


class _Connection:
    """One server process: JSON-RPC lines out on stdin, lines in from stdout."""

    def __init__(self, argv, env=None, cwd=None, mask=()):
        self.argv = list(argv)
        self.mask = list(mask)
        self.noise = 0
        self.next_id = 0
        self.stash = {}
        self.server_requests = 0
        self.inbox = queue.Queue()
        self.stderr_tail = b""
        try:
            # A session of its own lets close() stop children too (npx, uv run, and
            # similar wrappers start the real server as a child process).
            self.proc = subprocess.Popen(
                self.argv, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                stderr=subprocess.PIPE, env=env, cwd=cwd, start_new_session=os.name == "posix")
        except (OSError, ValueError) as exc:
            name = self.argv[0] if self.argv else "(empty command)"
            raise McpError("launch", "could not start %s: %s" % (
                excerpt(name, self.mask), exc.__class__.__name__))
        self.threads = [threading.Thread(target=self._read_stdout, daemon=True),
                        threading.Thread(target=self._read_stderr, daemon=True)]
        for thread in self.threads:
            thread.start()

    def _read_stdout(self):
        """Split stdout into lines in fixed-size chunks; a line longer than MAX_LINE is
        dropped (counted as noise) instead of held in memory."""
        parts, size, skipping = [], 0, False
        for chunk in iter(lambda: self.proc.stdout.read1(65536), b""):
            start = 0
            while True:
                end = chunk.find(b"\n", start)
                piece = chunk[start:] if end < 0 else chunk[start:end]
                if not skipping:
                    parts.append(piece)
                    size += len(piece)
                    if size > MAX_LINE:
                        self.noise += 1
                        parts, size, skipping = [], 0, True
                if end < 0:
                    break
                if not skipping:
                    self._take_line(b"".join(parts))
                parts, size, skipping, start = [], 0, False, end + 1
        if parts and not skipping:
            self._take_line(b"".join(parts))
        self.inbox.put(None)

    def _take_line(self, raw: bytes):
        line = raw.decode("utf-8", "replace").strip()
        if not line:
            return
        try:
            msg = json.loads(line)
        except ValueError:
            msg = None
        if isinstance(msg, dict) and msg.get("jsonrpc") == "2.0":
            self.inbox.put(msg)
        else:
            self.noise += 1

    def _read_stderr(self):
        for raw in iter(lambda: self.proc.stderr.read1(1024), b""):
            self.stderr_tail = (self.stderr_tail + raw)[-STDERR_KEEP:]

    def send(self, msg: dict):
        try:
            self.proc.stdin.write((json.dumps(msg) + "\n").encode("utf-8"))
            self.proc.stdin.flush()
        except (BrokenPipeError, OSError, ValueError):
            pass  # the reader sees the exit and reports it

    def request(self, method: str, params: dict) -> int:
        self.next_id += 1
        self.send({"jsonrpc": "2.0", "id": self.next_id, "method": method, "params": params})
        return self.next_id

    def notify(self, method: str):
        self.send({"jsonrpc": "2.0", "method": method})

    def wait_for(self, ids, until: float, what: str, soft: bool = False) -> Optional[dict]:
        """Return the first response whose id is in ids. soft: None at `until`."""
        for rid in ids:
            if rid in self.stash:
                return self.stash.pop(rid)
        while True:
            remaining = until - time.monotonic()
            if remaining <= 0:
                if soft:
                    return None
                raise McpError("timeout", "no answer to %s within the time limit" % what)
            try:
                msg = self.inbox.get(timeout=remaining)
            except queue.Empty:
                continue
            if msg is None:
                self.inbox.put(None)  # stay at end of stream for later waits
                raise McpError("exited", self._exit_message(what))
            rid = msg.get("id")
            if rid is not None and not isinstance(rid, (int, str)):
                continue  # an id no request of ours can have
            if "method" in msg:
                if "id" in msg:  # a request from the server: answer so it never blocks
                    self.server_requests += 1
                    if self.server_requests > MAX_SERVER_REQUESTS:
                        raise McpError("protocol", "the server sent too many requests")
                    if msg["method"] == "ping":
                        self.send({"jsonrpc": "2.0", "id": msg["id"], "result": {}})
                    else:
                        self.send({"jsonrpc": "2.0", "id": msg["id"],
                                   "error": {"code": -32601, "message": "Method not found"}})
                continue  # notifications are ignored
            if rid in ids:
                return msg
            self.stash[rid] = msg

    def _exit_message(self, what: str) -> str:
        try:
            code = self.proc.wait(timeout=2)
        except subprocess.TimeoutExpired:
            code = None
        self.threads[1].join(timeout=1)  # let the stderr reader catch the last bytes
        said = excerpt(self.stderr_tail.decode("utf-8", "replace"), self.mask)
        text = "the server closed its output before answering %s" % what
        if code is not None:
            text = "the server exited with code %s before answering %s" % (code, what)
        return text + (' (it printed: "%s")' % said if said else "")

    def _signal(self, name: str):
        try:
            if os.name == "posix":
                os.killpg(self.proc.pid, getattr(signal, name))
            elif name == "SIGTERM":
                self.proc.terminate()
            else:
                self.proc.kill()
        except OSError:
            pass  # already gone

    def close(self):
        """Close the server's input, then terminate, then kill: the server and its children.
        Signals that arrive meanwhile wait until the cleanup is done."""
        latch = _SignalLatch()
        try:
            self._stop()
        finally:
            latch.release()

    def _stop(self):
        proc = self.proc
        try:
            proc.stdin.close()
        except OSError:
            pass
        for step in (None, "SIGTERM", "SIGKILL"):
            if step:
                self._signal(step)
            try:
                proc.wait(timeout=STOP_WAIT if step != "SIGKILL" else 2)
                break
            except subprocess.TimeoutExpired:
                continue
        if os.name == "posix":
            try:
                os.killpg(proc.pid, 0)
                leftovers = True
            except OSError:
                leftovers = False
            if leftovers:  # children the server left behind
                self._signal("SIGTERM")
                time.sleep(0.2)
                self._signal("SIGKILL")
        for thread in self.threads:
            thread.join(timeout=1)
        if not any(thread.is_alive() for thread in self.threads):
            for stream in (proc.stdout, proc.stderr):
                stream.close()


class _SignalLatch:
    """Hold SIGTERM, SIGHUP, and SIGINT while servers are being stopped, then act on them.
    A second signal during cleanup would otherwise abort the kill of a server that
    ignores SIGTERM."""

    NAMES = ("SIGTERM", "SIGHUP", "SIGINT")

    def __init__(self):
        self.caught, self.saved = None, {}
        for name in self.NAMES:
            number = getattr(signal, name, None)
            if number is None:
                continue
            try:
                self.saved[number] = signal.signal(number, self._hold)
            except (ValueError, OSError):  # not the main thread: nothing to hold
                continue

    def _hold(self, signum, frame):
        self.caught = signum

    def release(self):
        for number, handler in self.saved.items():
            try:
                signal.signal(number, signal.SIG_DFL if handler is None else handler)
            except (ValueError, OSError):
                continue
        if self.caught is not None:
            sys.exit(130 if self.caught == signal.SIGINT else 143)


def _d(value) -> dict:
    return value if isinstance(value, dict) else {}


def _l(value) -> list:
    return value if isinstance(value, list) else []


def _error_text(msg: dict, mask=()) -> str:
    err = _d(msg.get("error"))
    return "error %s: %s" % (safe_text(err.get("code"), 20), excerpt(str(err.get("message", "")), mask))


def _handshake(conn: _Connection, deadline: float, probe_timeout: float):
    """Return (era, protocol version, server info)."""
    probe = conn.request("server/discover", {"_meta": modern_meta(MODERN_VERSION)})
    answer = conn.wait_for({probe}, min(deadline, time.monotonic() + probe_timeout),
                           "server/discover", soft=True)
    legacy_version = LEGACY_VERSION
    if answer is not None and "result" in answer:
        result = _d(answer["result"])
        versions = _l(result.get("supportedVersions"))
        info = _d(_d(result.get("_meta")).get("io.modelcontextprotocol/serverInfo"))
        if MODERN_VERSION in versions:
            return "modern", MODERN_VERSION, info
        raise McpError("protocol", "the server supports protocol versions %s; this checker "
                       "speaks %s and the legacy versions up to %s" % (
                           safe_text(", ".join(map(str, versions)), 120, conn.mask) or "(none listed)",
                           MODERN_VERSION, LEGACY_VERSION))
    if answer is not None and _d(answer.get("error")).get("code") == UNSUPPORTED_VERSION:
        supported = _l(_d(_d(answer["error"]).get("data")).get("supported"))
        legacy = sorted(v for v in supported if isinstance(v, str) and v <= LEGACY_VERSION)
        if not legacy:
            raise McpError("protocol", "the server supports only protocol versions %s; this "
                           "checker speaks %s and the legacy versions up to %s" % (
                               safe_text(", ".join(map(str, supported)), 120, conn.mask) or "(none listed)",
                               MODERN_VERSION, LEGACY_VERSION))
        legacy_version = legacy[-1]
    # Legacy fallback. A late answer to the probe may still arrive; keep it.
    init = conn.request("initialize", {"protocolVersion": legacy_version, "capabilities": {},
                                       "clientInfo": dict(CLIENT_INFO)})
    waiting = {init} if answer is not None else {init, probe}
    late_probe = None
    while True:
        msg = conn.wait_for(waiting, deadline, "initialize")
        if msg.get("id") == probe:
            late_probe = msg
            waiting.discard(probe)
            continue
        if "result" in msg:
            result = _d(msg["result"])
            conn.notify("notifications/initialized")
            return "legacy", str(result.get("protocolVersion") or legacy_version), \
                result.get("serverInfo") or {}
        late = _d(_d(late_probe).get("result"))
        if MODERN_VERSION in _l(late.get("supportedVersions")):
            info = _d(_d(late.get("_meta")).get("io.modelcontextprotocol/serverInfo"))
            return "modern", MODERN_VERSION, info
        raise McpError("protocol", "the server rejected initialize (%s)" % _error_text(msg, conn.mask))


def list_tools_stdio(argv, env=None, cwd=None, timeout: float = 20.0,
                     probe_timeout: float = 3.0, mask=()) -> dict:
    """Start a stdio server, list all its tools, and shut it down.

    env: the full environment for the server (None inherits this process's).
    mask: secret values (for example config env values) to hide in messages.
    """
    started = time.monotonic()
    deadline = started + timeout
    conn = _Connection(argv, env=env, cwd=cwd, mask=mask)
    try:
        era, version, info = _handshake(conn, deadline, probe_timeout)
        tools, pages, notes, seen, cursor = [], 0, [], set(), None
        while True:
            params = {} if cursor is None else {"cursor": cursor}
            if era == "modern":
                params["_meta"] = modern_meta(version)
            msg = conn.wait_for({conn.request("tools/list", params)}, deadline, "tools/list")
            if "error" in msg:
                raise McpError("protocol", "tools/list failed (%s)" % _error_text(msg, conn.mask))
            result = _d(msg.get("result"))
            page = result.get("tools")
            if not isinstance(page, list):
                raise McpError("protocol", "the tools/list answer has no tools list")
            tools.extend(t for t in page if isinstance(t, dict))
            pages += 1
            cursor = result.get("nextCursor")
            if cursor is None:  # an empty string is a valid cursor, not the end
                break
            if cursor in seen:
                notes.append("the server repeated a page cursor; listing stopped there")
                break
            seen.add(cursor)
        return {"tools": tools, "era": era, "protocol_version": version,
                "server_info": info if isinstance(info, dict) else {},
                "pages": pages, "stdout_noise": conn.noise, "notes": notes,
                "seconds": round(time.monotonic() - started, 2)}
    finally:
        conn.close()


def exit_on_signals():
    """Turn SIGTERM, SIGHUP, and SIGINT into a normal exit (codes 143, 143, 130), so
    cleanup code runs and servers stop."""
    for name, code in (("SIGTERM", 143), ("SIGHUP", 143), ("SIGINT", 130)):
        if hasattr(signal, name):
            signal.signal(getattr(signal, name), lambda signum, frame, code=code: sys.exit(code))


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        description="List the tools of one stdio MCP server. Put the server command after --.")
    parser.add_argument("--timeout", type=float, default=20.0,
                        help="seconds to wait for the whole listing (default 20)")
    parser.add_argument("--json", action="store_true", help="print the full tool list as JSON")
    parser.add_argument("command", nargs=argparse.REMAINDER, help="the server command and its arguments")
    args = parser.parse_args(argv)
    command = args.command[1:] if args.command[:1] == ["--"] else args.command
    if not command:
        parser.error("give the server command after --")
    try:
        result = list_tools_stdio(command, timeout=args.timeout)
    except McpError as exc:
        print("error (%s): %s" % (exc.kind, inline(exc, 400)), file=sys.stderr)
        return 2
    if args.json:
        print(json.dumps(result, indent=2))
    else:
        print("%d tools (%s protocol %s)" % (len(result["tools"]), result["era"],
                                            inline(result["protocol_version"], 40)))
        for tool in result["tools"]:
            print("- %s" % inline(tool.get("name"), 128))
    return 0


if __name__ == "__main__":
    exit_on_signals()
    sys.exit(main())
