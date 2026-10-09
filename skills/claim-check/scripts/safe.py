# Copied from skills/evals/shared/safe.py by skills/evals/tools/sync_shared.py. Edit the source, then run the sync.
"""Make untrusted text safe to show in a Markdown report.

Text from a repository or a session transcript can hold secrets, carry
instructions for the agent that relays a report, or break a table. redact()
masks secrets; safe_text() also makes the text one inert line; code() puts
that line inside inline code.

Python 3.9+, standard library only.
"""

import re

_MASK = "[REDACTED]"
_SECRET_RES = [
    # PEM private key blocks, including a block cut off before its END line.
    re.compile(r"-----BEGIN [A-Z0-9 ]*PRIVATE KEY-----.*?(?:-----END [A-Z0-9 ]*PRIVATE KEY-----|\Z)", re.S),
    re.compile(r"\bsk-ant-[A-Za-z0-9_\-]{16,}"),
    re.compile(r"\bsk-[A-Za-z0-9_\-]{20,}"),
    re.compile(r"\bgh[pousr]_[A-Za-z0-9]{20,}"),
    re.compile(r"\bgithub_pat_[A-Za-z0-9_]{20,}"),
    re.compile(r"\bglpat-[A-Za-z0-9_\-]{20,}"),
    re.compile(r"\bxox[abposr]-[A-Za-z0-9\-]{10,}"),
    re.compile(r"\bAIza[0-9A-Za-z_\-]{30,}"),
    re.compile(r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b"),
    re.compile(r"\b[rsp]k_(?:live|test)_[0-9A-Za-z]{16,}"),
    re.compile(r"\bnpm_[A-Za-z0-9]{30,}"),
    re.compile(r"\bhf_[A-Za-z0-9]{30,}"),
    re.compile(r"\beyJ[A-Za-z0-9_\-]{8,}\.[A-Za-z0-9_\-]{8,}\.[A-Za-z0-9_\-]{8,}"),
]
# Keep group 1, mask the rest of the match.
_KEEP_PREFIX_RES = [
    # Credentials passed as command flags: sshpass -p X, mysql -pX, curl -u user:X, --password X.
    re.compile(r"(\bsshpass\s+-p\s*)[^\s'\"]+"),
    re.compile(r"(?i)(\b(?:mysql|mysqldump|mysqladmin|mysqlsh|mariadb|mariadb-dump)\b[^\n|;&]*?\s-p)(?=[^\s-])[^\s'\"]+"),
    re.compile(r"((?:^|\s)(?:-u|--user)[=\s]+[^\s:@'\"]+:)[^\s@'\"]+"),
    re.compile(r"(?i)(--pass(?:word|wd)?\s+)[^\s'\"-][^\s'\"]*"),
    re.compile(r"(?i)(\b(?:bearer|basic)\s+)[A-Za-z0-9._~+/\-]{16,}=*"),
    re.compile(r"(?i)(\b[a-z][a-z0-9+.\-]*://[^/\s:@]+:)[^@\s/]+(?=@)"),
    re.compile(r"(?i)(\b[A-Za-z0-9_.\-]*(?:api[_\-]?key|secret|token|passw(?:or)?d|pwd|credential|"
               r"private[_\-]?key|access[_\-]?key)[A-Za-z0-9_.\-]*[\"']?\s*[:=]\s*[\"']?)[^\s\"',;}]{6,}"),
]
_LONG_RUN_RE = re.compile(r"[A-Za-z0-9+=_\-]{40,}")


def _mask_long_run(m) -> str:
    s = m.group(0)
    if re.search(r"[0-9]", s) and re.search(r"[A-Za-z]", s):
        return _MASK
    return s


def redact(text) -> str:
    """Mask API keys, tokens, private keys, and long secret-like strings."""
    if not text:
        return ""
    out = str(text)
    for rx in _SECRET_RES:
        out = rx.sub(_MASK, out)
    for rx in _KEEP_PREFIX_RES:
        out = rx.sub(lambda m: m.group(1) + _MASK, out)
    return _LONG_RUN_RE.sub(_mask_long_run, out)


def safe_text(text, limit=160) -> str:
    """Make untrusted text (a command, a path, a claim, a tool description) safe
    to show in a markdown report: secrets masked, control characters and line
    breaks turned into spaces, backticks and table pipes replaced, runs of
    spaces collapsed, and the result cut to `limit` characters. Text from a
    repository or a transcript can carry instructions or break a table; this
    keeps it one inert line."""
    s = redact(text if isinstance(text, str) else str(text))
    s = "".join(ch if ch.isprintable() else " " for ch in s)
    s = " ".join(s.replace("`", "'").replace("|", "/").split())
    return s if len(s) <= limit else s[: max(limit - 3, 0)] + "..."


def code(text, limit=160) -> str:
    """Untrusted text for a Markdown report, inside inline code so links, HTML,
    and bare URLs stay literal; safe_text already replaces backticks and pipes.
    Text that is empty after cleaning shows as `(empty)`, because a bare pair
    of backticks renders as two stray backticks."""
    return "`%s`" % (safe_text(text, limit) or "(empty)")
