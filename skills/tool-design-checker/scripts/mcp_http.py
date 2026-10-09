#!/usr/bin/env python3
"""List one remote MCP server's tools over Streamable HTTP. This file makes network calls.

It follows the MCP specification (revision 2026-07-28): first a modern
`tools/list` POST that carries its protocol version in headers and `_meta`;
if the server answers with a 4xx that is not a modern MCP error, it falls
back to the legacy flow (`initialize`, the Mcp-Session-Id header,
`notifications/initialized`, then `tools/list`). Answers may be JSON or an
SSE stream. It does not sign in (OAuth) and does not speak the deprecated
HTTP+SSE transport; both are reported as errors.

Usage:
    python3 mcp_http.py [--header "Name: value"] [--timeout 20] [--json] <url>

Standard library only, Python 3.9+. Header values are sent to the server you
name and never printed.
Exit codes: 0 tools listed, 2 usage error or the server could not be listed.
"""

from __future__ import annotations

import argparse
import http.client
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from mcp_client import (CLIENT_INFO, LEGACY_VERSION, MODERN_VERSION,  # noqa: E402
                        UNSUPPORTED_VERSION, McpError, excerpt, exit_on_signals, inline, modern_meta,
                        safe_text)

ACCEPT = "application/json, text/event-stream"
MODERN_ERRORS = {-32020, -32021, UNSUPPORTED_VERSION}
BODY_LIMIT = 4 << 20  # one answer; tools/list pages are far smaller
READ_ERRORS = (OSError, ValueError, http.client.HTTPException)
VERSION_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
SESSION_RE = re.compile(r"^[\x21-\x7e]{1,512}$")


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """Hand 3xx answers back as errors. Following a redirect would send the
    configured headers, often a token, to whatever host the server names."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


_OPENER = urllib.request.build_opener(_NoRedirect)


class _Http:
    def __init__(self, url: str, headers: dict, deadline: float, mask=()):
        self.url, self.headers, self.deadline, self.mask = url, dict(headers), deadline, list(mask)
        netloc = url.split("/")[2] if "://" in url else url.split("/")[0]
        self.host = safe_text(netloc.rsplit("@", 1)[-1], 100)

    def remaining(self) -> float:
        left = self.deadline - time.monotonic()
        if left <= 0:
            raise McpError("timeout", "no answer from %s within the time limit" % self.host)
        return left

    def post(self, message: dict, extra: dict):
        """Return (status, headers, the JSON-RPC answer to message or None)."""
        headers = {"Content-Type": "application/json", "Accept": ACCEPT}
        headers.update(self.headers)
        headers.update(extra)
        request = urllib.request.Request(self.url, data=json.dumps(message).encode("utf-8"),
                                         headers=headers, method="POST")
        try:
            response = _OPENER.open(request, timeout=self.remaining())
        except urllib.error.HTTPError as exc:
            if 300 <= exc.code < 400:
                exc.close()
                raise McpError("network", "%s redirected (HTTP %d); not followed" % (self.host, exc.code))
            try:
                answer = self._answer(exc, None)
            except READ_ERRORS + (AttributeError,):
                answer = None
            except McpError:
                answer = None  # an error answer that is too slow or too big says nothing useful
            finally:
                exc.close()
            return exc.code, exc.headers, answer
        except READ_ERRORS as exc:
            reason = getattr(exc, "reason", exc)
            raise McpError("network", "could not reach %s (%s)" % (self.host, excerpt(str(reason), self.mask)))
        with response:
            try:
                return response.status, response.headers, self._answer(response, message.get("id"))
            except READ_ERRORS as exc:
                raise McpError("network", "%s stopped sending its answer (%s)" % (
                    self.host, exc.__class__.__name__))

    def _answer(self, response, wanted):
        # An HTTPError wraps the real response in .fp; a normal response is read directly.
        stream = response.fp if isinstance(response, urllib.error.HTTPError) and response.fp else response
        kind = (response.headers.get("Content-Type") or "").split(";")[0].strip().lower()
        if kind == "text/event-stream":
            for text in self._events(stream):
                msg = _json(text)
                if isinstance(msg, dict) and msg.get("id") == wanted and ("result" in msg or "error" in msg):
                    return msg
            return None
        msg = _json(b"".join(self._chunks(stream)).decode("utf-8", "replace"))
        if isinstance(msg, list):
            msg = next((m for m in msg if isinstance(m, dict) and m.get("id") == wanted), None)
        return msg if isinstance(msg, dict) else None

    def _chunks(self, stream):
        """The body in chunks, under one deadline for the whole answer and a size cap.
        read1 returns as soon as any bytes arrive, so a server that trickles bytes
        still meets the deadline."""
        read = getattr(stream, "read1", None) or stream.read
        total = 0
        while True:
            self.remaining()
            chunk = read(65536)
            if not chunk:
                return
            total += len(chunk)
            if total > BODY_LIMIT:
                raise McpError("network", "%s sent an answer larger than %d MB" % (self.host, BODY_LIMIT >> 20))
            yield chunk

    def _events(self, stream):
        data, pending = [], b""
        for chunk in self._chunks(stream):
            lines = (pending + chunk).split(b"\n")
            pending = lines.pop()
            for raw in lines:
                line = raw.decode("utf-8", "replace").rstrip("\r")
                if not line:
                    if data:
                        yield "\n".join(data)
                        data = []
                    continue
                field, _, value = line.partition(":")
                if field == "data":
                    data.append(value[1:] if value.startswith(" ") else value)
        if pending.strip():
            field, _, value = pending.decode("utf-8", "replace").rstrip("\r").partition(":")
            if field == "data":
                data.append(value[1:] if value.startswith(" ") else value)
        if data:
            yield "\n".join(data)

    def delete(self, extra: dict):
        headers = dict(self.headers)
        headers.update(extra)
        try:
            _OPENER.open(urllib.request.Request(self.url, headers=headers, method="DELETE"),
                         timeout=min(2.0, max(0.1, self.deadline - time.monotonic()))).close()
        except READ_ERRORS:
            pass  # ending the session is a courtesy; the server times it out anyway


def _json(text):
    try:
        return json.loads(text)
    except ValueError:
        return None


def _check_auth(http: _Http, status: int):
    if status in (401, 403):
        raise McpError("auth", "%s needs sign-in (HTTP %d); this checker does not sign in. List the "
                       "tools from your harness and pass the saved list to lint --tools." % (http.host, status))


def _error_text(msg, mask=()) -> str:
    err = msg.get("error") if isinstance(msg, dict) else None
    err = err if isinstance(err, dict) else {}
    return "error %s: %s" % (safe_text(err.get("code"), 20), excerpt(str(err.get("message", "")), mask))


def _pages(http: _Http, first: dict, next_page) -> tuple:
    """Collect tools across pages. next_page(cursor, request id) returns the next answer."""
    tools, pages, notes, seen, msg, rid = [], 0, [], set(), first, 10
    while True:
        result = msg.get("result") if isinstance(msg, dict) else None
        if not isinstance(result, dict) or not isinstance(result.get("tools"), list):
            raise McpError("protocol", "tools/list failed (%s)" % _error_text(msg, http.mask))
        tools.extend(t for t in result["tools"] if isinstance(t, dict))
        pages += 1
        cursor = result.get("nextCursor")
        if cursor is None:  # an empty string is a valid cursor, not the end
            return tools, pages, notes
        if cursor in seen:
            notes.append("the server repeated a page cursor; listing stopped there")
            return tools, pages, notes
        seen.add(cursor)
        rid += 1
        msg = next_page(cursor, rid)


def list_tools_http(url: str, headers=None, timeout: float = 20.0, mask=()) -> dict:
    """List a Streamable HTTP server's tools. headers: sent as given, never printed."""
    started = time.monotonic()
    http = _Http(url, headers or {}, started + timeout, mask)
    modern = {"MCP-Protocol-Version": MODERN_VERSION, "Mcp-Method": "tools/list"}
    first = {"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {"_meta": modern_meta(MODERN_VERSION)}}
    status, _headers, msg = http.post(first, modern)
    _check_auth(http, status)
    error = msg.get("error") if isinstance(msg, dict) else None
    error = error if isinstance(error, dict) else {}
    code = error.get("code")
    legacy_version = LEGACY_VERSION

    def done(tools, pages, notes, era, version, info):
        return {"tools": tools, "era": era, "protocol_version": version, "server_info": info,
                "pages": pages, "stdout_noise": 0, "notes": notes,
                "seconds": round(time.monotonic() - started, 2)}

    if status == 200 and isinstance(msg, dict) and "result" in msg:
        def modern_page(cursor, rid):
            params = {"cursor": cursor, "_meta": modern_meta(MODERN_VERSION)}
            return http.post({"jsonrpc": "2.0", "id": rid, "method": "tools/list", "params": params}, modern)[2]
        tools, pages, notes = _pages(http, msg, modern_page)
        era = "modern" if isinstance(msg["result"], dict) and "resultType" in msg["result"] else "legacy"
        return done(tools, pages, notes, era, MODERN_VERSION if era == "modern" else "unknown", {})
    if status == 404 and code == -32601:
        return done([], 0, ["the server does not offer tools/list"], "modern", MODERN_VERSION, {})
    if code == UNSUPPORTED_VERSION:
        data = error.get("data") if isinstance(error.get("data"), dict) else {}
        supported = data.get("supported") if isinstance(data.get("supported"), list) else []
        legacy = sorted(v for v in supported if isinstance(v, str) and VERSION_RE.match(v) and v <= LEGACY_VERSION)
        if not legacy:
            raise McpError("protocol", "the server supports only protocol versions %s; this checker "
                           "speaks %s and the legacy versions up to %s" % (
                               safe_text(", ".join(map(str, supported)), 120, http.mask) or "(none listed)",
                               MODERN_VERSION, LEGACY_VERSION))
        legacy_version = legacy[-1]
    elif code in MODERN_ERRORS or status == 200:
        raise McpError("protocol", "the server rejected tools/list (%s)" % _error_text(msg, http.mask))

    # Legacy Streamable HTTP (2025-03-26 to 2025-11-25): initialize, then a session.
    init = {"jsonrpc": "2.0", "id": 2, "method": "initialize",
            "params": {"protocolVersion": legacy_version, "capabilities": {}, "clientInfo": dict(CLIENT_INFO)}}
    status, headers_in, msg = http.post(init, {})
    _check_auth(http, status)
    if status in (404, 405) or (status >= 400 and msg is None):
        raise McpError("protocol", "%s did not accept a Streamable HTTP request (HTTP %d); it may use "
                       "the deprecated HTTP+SSE transport, which this checker does not speak" % (http.host, status))
    if not isinstance(msg, dict) or not isinstance(msg.get("result"), dict):
        raise McpError("protocol", "the server rejected initialize (%s)" % _error_text(msg, http.mask))
    version = msg["result"].get("protocolVersion")
    version = version if isinstance(version, str) and VERSION_RE.match(version) else legacy_version
    session = {"MCP-Protocol-Version": version}
    session_id = headers_in.get("Mcp-Session-Id")
    if session_id:
        if not SESSION_RE.match(session_id):
            raise McpError("protocol", "the server sent a session id that is not visible ASCII")
        session["Mcp-Session-Id"] = session_id
    try:
        http.post({"jsonrpc": "2.0", "method": "notifications/initialized"}, session)

        def legacy_page(cursor, rid):
            params = {} if cursor is None else {"cursor": cursor}
            return http.post({"jsonrpc": "2.0", "id": rid, "method": "tools/list", "params": params}, session)[2]
        tools, pages, notes = _pages(http, legacy_page(None, 3), legacy_page)
        return done(tools, pages, notes, "legacy", version, msg["result"].get("serverInfo") or {})
    finally:
        if "Mcp-Session-Id" in session:
            http.delete(session)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        description="List the tools of one remote MCP server over Streamable HTTP (a network call).")
    parser.add_argument("url", help="the server's MCP endpoint, for example https://example.com/mcp")
    parser.add_argument("--header", action="append", default=[],
                        help='a request header as "Name: value" (repeatable); values are never printed')
    parser.add_argument("--timeout", type=float, default=20.0, help="seconds for the whole listing (default 20)")
    parser.add_argument("--json", action="store_true", help="print the full tool list as JSON")
    args = parser.parse_args(argv)
    headers = {}
    for item in args.header:
        name, sep, value = item.partition(":")
        if not sep or not name.strip():
            parser.error('headers look like "Name: value"')
        headers[name.strip()] = value.strip()
    try:
        result = list_tools_http(args.url, headers=headers, timeout=args.timeout, mask=list(headers.values()))
    except McpError as exc:
        print("error (%s): %s" % (exc.kind, inline(exc, 400, list(headers.values()))), file=sys.stderr)
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
