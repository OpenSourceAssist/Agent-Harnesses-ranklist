# Copied from skills/evals/shared/safe.py by skills/evals/tools/sync_shared.py. Edit the source, then run the sync.
"""Make untrusted text safe to show in a Markdown report.

Text from a repository or a session transcript can hold secrets, carry
instructions for the agent that relays a report, or break a table. redact()
masks secrets; safe_text() also makes the text one inert line; code() puts
that line inside inline code.

Python 3.9+, standard library only.
"""

import re
import unicodedata

_MASK = "[REDACTED]"
# Word edges by ASCII rules. Python's \b counts a non-ASCII letter, such as an
# e with an acute accent, as part of the word, so a key right after one would
# stay unmasked.
_B = r"(?<![A-Za-z0-9_])"
_E = r"(?![A-Za-z0-9_])"
# Characters that draw nothing, besides the format characters (Unicode category
# Cf: zero-width spaces and joiners, direction marks, the soft hyphen): the
# combining grapheme joiner, Hangul fillers, Khmer inherent vowels, Mongolian
# and other variation selectors, and tag characters. Inside a key name they
# hide the key from the patterns below while the text still reads as the key.
_INVISIBLE_RE = re.compile(r"[\u034f\u115f\u1160\u17b4\u17b5\u180b-\u180f\u3164\ufe00-\ufe0f\uffa0"
                           r"\U000e0000-\U000e0fff]")
# Run time: hooks clean every tool call, and a tool input can be tens of
# kilobytes, so each pattern must take time in proportion to the text. A pattern
# that can start at many places inside one long run of characters, scan to the
# end of that run, and then fail takes time that grows with the square of the
# run (or worse). The patterns below limit or stop such scans; each one says how.
_SECRET_RES = [
    # PEM private key blocks, including a block cut off before its END line.
    re.compile(r"-----BEGIN [A-Z0-9 ]*PRIVATE KEY-----.*?(?:-----END [A-Z0-9 ]*PRIVATE KEY-----|\Z)", re.S),
    re.compile(_B + r"sk-ant-[A-Za-z0-9_\-]{16,}"),
    re.compile(_B + r"sk-[A-Za-z0-9_\-]{20,}"),
    re.compile(_B + r"gh[pousr]_[A-Za-z0-9]{20,}"),
    re.compile(_B + r"github_pat_[A-Za-z0-9_]{20,}"),
    re.compile(_B + r"glpat-[A-Za-z0-9_\-]{20,}"),
    re.compile(_B + r"xox[abposr]-[A-Za-z0-9\-]{10,}"),
    re.compile(_B + r"AIza[0-9A-Za-z_\-]{30,}"),
    re.compile(_B + r"(?:AKIA|ASIA)[0-9A-Z]{16}" + _E),
    re.compile(_B + r"[rsp]k_(?:live|test)_[0-9A-Za-z]{16,}"),
    re.compile(_B + r"npm_[A-Za-z0-9]{30,}"),
    re.compile(_B + r"hf_[A-Za-z0-9]{30,}"),
    # A JSON web token. The first part stops before a later "-eyJ", where the
    # next try starts, so no run is scanned again for each "-eyJ" in it.
    re.compile(_B + r"eyJ(?:[A-Za-z0-9_]|-(?!eyJ)){8,}\.[A-Za-z0-9_\-]{8,}\.[A-Za-z0-9_\-]{8,}"),
]
# A mysql or mariadb program name, as one word.
_MYSQL = _B + r"(?:mysql|mysqldump|mysqladmin|mysqlsh|mariadb|mariadb-dump)" + _E


def _credential(before, unquoted, after="", flags=0):
    """A credential after a fixed prefix. Group 1, which redact() keeps, is
    `before`, an optional quote (group 2), and `after`. The rest of the match
    is masked: with a quote, everything up to the matching quote or the end of
    the line, spaces included (a backslash escapes the next character, and the
    closing quote stays); without one, `unquoted`."""
    return re.compile(r"(%s([\"'])?%s)(?(2)(?:(?!\2)[^\\\n]|\\.)+|%s)" % (before, after, unquoted), flags)


# Keep group 1, mask the rest of the match.
_KEEP_PREFIX_RES = [
    # Credentials passed as command flags: sshpass -p X, mysql -pX, curl -u user:X, --password X.
    _credential(_B + r"sshpass\s+-p\s*", r"[^\s'\"]+"),
    # The search for -p stops at the next program name, where the next try starts.
    _credential(_MYSQL + r"(?:(?!" + _MYSQL + r")[^\n|;&])*?\s-p(?=[^\s-])", r"[^\s'\"]+", flags=re.I),
    # One "=" or spaces after the flag: a user name can start with "=", so "[=\s]+"
    # before it could split a run of "=" in every way.
    _credential(r"(?:^|\s)(?:-u|--user)(?:=|\s+)", r"[^\s@'\"]+", after=r"[^\s:@'\"]+:"),
    _credential(r"--pass(?:word|wd)?\s+", r"[^\s'\"-][^\s'\"]*", flags=re.I),
    re.compile(r"(?i)(" + _B + r"(?:bearer|basic)\s+)[A-Za-z0-9._~+/\-]{16,}=*"),
    # A URL scheme has at most 32 characters, so each try scans at most that far.
    re.compile(r"(?i)(" + _B + r"[a-z][a-z0-9+.\-]{0,31}://[^/\s:@]+:)[^@\s/]+(?=@)"),
    # A secret-like key and any non-empty value. The key is one whole name (a run
    # of letters, digits, and _ . -) that holds a keyword anywhere. A try starts
    # only where a name starts, and the lookahead stays inside that name, so each
    # name is scanned once. "tokens" alone names a count (max_tokens: 1000), not a secret.
    _credential(r"(?<![A-Za-z0-9_.\-])(?=[A-Za-z0-9_.\-]*?(?:api[_\-]?key|secret|token(?!s(?![A-Za-z0-9]))|"
                r"passw(?:or)?d|pwd|credential|private[_\-]?key|access[_\-]?key))"
                r"[A-Za-z0-9_.\-]+[\"']?\s*[:=]+\s*", r"[^\s\"',;}]+", flags=re.I),
]
_LONG_RUN_RE = re.compile(r"[A-Za-z0-9+=_\-]{40,}")


def _mask_long_run(m) -> str:
    s = m.group(0)
    if re.search(r"[0-9]", s) and re.search(r"[A-Za-z]", s):
        return _MASK
    return s


def _plain(text) -> str:
    """NFKC form (fullwidth and other compatibility letters become plain ones)
    without the characters that draw nothing."""
    if text.isascii():
        return text
    text = unicodedata.normalize("NFKC", text)
    return _INVISIBLE_RE.sub("", "".join(ch for ch in text if unicodedata.category(ch) != "Cf"))


def redact(text) -> str:
    """Mask API keys, tokens, private keys, and long secret-like strings. The
    text comes back in NFKC form without the characters that draw nothing, so a
    fullwidth or split key name is masked like the plain one."""
    if not text:
        return ""
    out = _plain(str(text))
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
