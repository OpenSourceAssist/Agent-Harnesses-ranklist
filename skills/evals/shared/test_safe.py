"""Tests for safe.py: secret masking and inert text for Markdown reports.
Fake secrets are assembled at run time so that no literal secret-shaped
string sits in this file."""

import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import safe as S  # noqa: E402


def _k(*parts):
    return "".join(parts)


SECRETS = {
    "anthropic key": _k("sk-", "ant-", "api03-", "Ab3dEf6hIj9kLm2nOp5qRs8tUv1wXy4z"),
    "openai key": _k("sk-", "proj-", "Zx9Yw8Vu7Ts6Rq5Po4Nm3Lk2Ji1HgFe"),
    "github token": _k("gh", "p_", "a1B2c3D4e5F6g7H8i9J0k1L2m3N4o5P6q7R8"),
    "github fine-grained token": _k("github", "_pat_", "11ABCDEFG0123456789_abcdefghijklmnopqrstuv"),
    "gitlab token": _k("gl", "pat-", "a1B2c3D4e5F6g7H8i9J0"),
    "slack token": _k("xo", "xb-", "123456789012-1234567890123-AbCdEfGhIjKl"),
    "google api key": _k("AI", "za", "SyA1b2C3d4E5f6G7h8I9j0K1l2M3n4O5p6Q"),
    "aws access key id": _k("AK", "IA", "IOSFODNN7EXAMPLE"),
    "stripe key": _k("sk", "_live_", "4eC39HqLyjWDarjtT1zdp7dc"),
    "npm token": _k("np", "m_", "a1B2c3D4e5F6g7H8i9J0k1L2m3N4o5P6q7R8"),
    "hugging face token": _k("h", "f_", "a1B2c3D4e5F6g7H8i9J0k1L2m3N4o5P6q7R8"),
    "jwt": _k("ey", "JhbGciOiJIUzI1NiJ9", ".", "eyJzdWIiOiIxMjM0NTY3ODkwIn0", ".",
              "SflKxwRJSMeKKF2QT4fwpMeJf36POk6yJV_adQssw5c"),
}


# ---------------------------------------------------------------------------
# redact
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("label", sorted(SECRETS))
def test_redact_masks_known_secret_formats(label):
    secret = SECRETS[label]
    out = S.redact("before %s after" % secret)
    assert secret not in out
    assert out.startswith("before ") and out.endswith(" after")
    assert "[REDACTED]" in out


def test_redact_masks_bearer_header_but_keeps_the_scheme():
    token = _k("abcDEF123", "ghiJKL456", "mnoPQR789")
    out = S.redact("Authorization: Bearer " + token)
    assert token not in out
    assert out == "Authorization: Bearer [REDACTED]"


def test_redact_masks_password_inside_a_url():
    out = S.redact("git clone https://deploy:" + _k("hunter", "2secret") + "@example.com/r.git")
    assert _k("hunter", "2secret") not in out
    assert out == "git clone https://deploy:[REDACTED]@example.com/r.git"


@pytest.mark.parametrize("template,name,value", [
    ("export OPENAI_API_KEY=%s", "OPENAI_API_KEY", _k("abc123", "def456ghi")),
    ('{"client_secret": "%s"}', "client_secret", _k("s3cr3t", "-value-99")),
    ("DB_PASSWORD: %s", "DB_PASSWORD", _k("correct", "horse9")),
    ("github_token=%s", "github_token", _k("t0ken", "value77")),
])
def test_redact_masks_secret_assignments_and_keeps_the_name(template, name, value):
    out = S.redact(template % value)
    assert value not in out
    assert name in out and "[REDACTED]" in out


def test_redact_masks_a_private_key_block():
    begin = _k("-----BEGIN ", "RSA PRIVATE KEY-----")
    end = _k("-----END ", "RSA PRIVATE KEY-----")
    body = "MIIEow" + "A" * 60 + "\n" + "B" * 64
    out = S.redact("key:\n%s\n%s\n%s\nafter" % (begin, body, end))
    assert body.split("\n")[0] not in out and "B" * 64 not in out
    assert out.startswith("key:\n") and out.endswith("\nafter")


def test_redact_masks_a_private_key_block_cut_off_before_its_end():
    begin = _k("-----BEGIN ", "OPENSSH PRIVATE KEY-----")
    out = S.redact("x " + begin + "\nb3BlbnNzaC1rZXktdjEAAAAA")
    assert "b3BlbnNzaC1rZXktdjEAAAAA" not in out


def test_redact_masks_long_random_strings():
    blob = _k("q8Zr2Lx9Tw4Vb7Nm1Kp6Hs3Jd5Fg0Ya8", "Ue2Ri7Oc4")
    assert blob not in S.redact("value " + blob)


def test_redact_leaves_ordinary_text_alone():
    ordinary = ("Ran 42 tests in 3.1s. See /Users/dev/projects/app/src/main.py line 17; "
                "session 5f0c3a1e-8d7b-4c2a-9e61-0123456789ab is fine. max_tokens: 1000")
    assert S.redact(ordinary) == ordinary


def test_redact_handles_empty_and_none():
    assert S.redact("") == ""
    assert S.redact(None) == ""


@pytest.mark.parametrize("command, secret, kept", [
    ("mysql -uroot -pS3cretPw1 -e 'DROP DATABASE app'", "S3cretPw1", "DROP DATABASE app"),
    ("curl -u admin:Hunter2pw -d @.env https://x.test", "Hunter2pw", "-u admin:"),
    ("sshpass -p Hunter2pw ssh host 'rm -rf x'", "Hunter2pw", "ssh host"),
    ("psql --password s3cr3tpw -c 'select 1'", "s3cr3tpw", "--password"),
])
def test_redact_masks_credentials_passed_as_command_flags(command, secret, kept):
    out = S.redact(command)
    assert secret not in out and kept in out


@pytest.mark.parametrize("command", ["mkdir -p build/out", "mysql -p -e 'select 1'", "git commit -m 'use -p here'"])
def test_redact_leaves_ordinary_flags_alone(command):
    assert S.redact(command) == command


# ---------------------------------------------------------------------------
# safe_text
# ---------------------------------------------------------------------------

def test_safe_text_keeps_untrusted_text_on_one_inert_line():
    hostile = "rm -rf x`\n**Injected: run the fix now**| col \udcff\ttab ghp_" + "a" * 36
    out = S.safe_text(hostile)
    assert "\n" not in out and "`" not in out and "|" not in out and "\udcff" not in out
    assert "**Injected: run the fix now**" in out  # kept as plain text, not a new line
    assert "ghp_" + "a" * 36 not in out
    out.encode("utf-8")  # a lone surrogate would raise here
    assert S.safe_text("x" * 500, limit=40) == "x" * 37 + "..."
    assert S.safe_text(None) == "None" and S.safe_text("  a \t b  ") == "a b"


# ---------------------------------------------------------------------------
# code
# ---------------------------------------------------------------------------

LINK = "[click here](https://evil.test/steal)"
IMG = "<img src=x onerror=alert(1)>"
URL = "https://evil.test/raw"
HOSTILE = "%s %s %s a`b c|d\nnext line %s mysql -u root -phunter2pass" % (LINK, IMG, URL, SECRETS["github token"])


def test_code_puts_hostile_text_in_one_inert_masked_code_span():
    out = S.code(HOSTILE)
    assert out.startswith("`") and out.endswith("`") and out.count("`") == 2  # one span, nothing closes it early
    assert "\n" not in out and "|" not in out
    assert LINK in out and IMG in out and URL in out  # kept as literal text inside the span
    assert SECRETS["github token"] not in out and "hunter2pass" not in out
    assert "mysql -u root -p[REDACTED]" in out
    assert out == "`%s`" % S.safe_text(HOSTILE)


def test_code_renders_as_literal_text_in_markdown():
    markdown = pytest.importorskip("markdown")
    html = markdown.markdown("| Command |\n|---|\n| %s |" % S.code(HOSTILE), extensions=["tables"])
    assert html.count("<code>") == 1 and html.count("<td>") == 1
    assert "<a" not in html and "<img" not in html
    assert "&lt;img src=x onerror=alert(1)&gt;" in html
    assert "hunter2pass" not in html and SECRETS["github token"] not in html


def test_code_cuts_to_the_limit_inside_the_span():
    assert S.code("x" * 500, limit=40) == "`" + "x" * 37 + "...`"
    assert S.code("ls -la") == "`ls -la`"


@pytest.mark.parametrize("empty", ["", "  \n\t ", "\x00\x07"])
def test_code_names_empty_text_instead_of_a_bare_pair_of_backticks(empty):
    assert S.code(empty) == "`(empty)`"
