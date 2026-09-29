"""Redact secrets from text before it is recorded or shown.

Command output from a sandbox (the tails of setup and validate steps) ends up
in run records, bot comments and job logs. :func:`redact` replaces what looks
like a credential with :data:`REDACTED`:

- the value of an ``Authorization``, ``Proxy-Authorization``, ``Cookie``,
  ``Set-Cookie``, ``X-Api-Key`` or ``Api-Key`` header, and a ``Bearer`` or
  ``Basic`` credential anywhere;
- the user information of a URL (``https://user:token@host`` becomes
  ``https://[REDACTED]@host``);
- tokens with a well-known shape: OpenAI and Anthropic keys, GitHub, GitLab,
  npm and Slack tokens, AWS access key ids, Google API keys and OAuth access
  tokens, JSON Web Tokens and OpenShell credential placeholders;
- the value in ``NAME=value`` or ``"name": "value"`` when the name looks like
  a credential (token, secret, password, key, credential, auth, cookie or the
  word pat);
- every literal value given in *secrets*, such as the credentials the host
  holds (:func:`secret_values`).

It is a best-effort filter for accidental leaks, not a guarantee: a secret
with no recognizable shape that the host does not hold passes through.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping

REDACTED = "[REDACTED]"

# Shortest host value treated as a secret. Shorter values (``1``, ``true``, a
# region) would redact ordinary words.
MIN_SECRET_LENGTH = 8

# A variable name that looks like it holds a credential. ``pat`` (personal
# access token) only counts as a whole ``_``-separated word, so GOPATH is not one.
_SECRET_WORDS = (
    r"token|secret|passw(?:or)?d|api[_-]?key|private[_-]?key|access[_-]?key"
    r"|credential|auth|cookie|session"
)
_SECRET_NAME_RE = re.compile(
    rf"(?i)^(?:[A-Za-z0-9_.-]*(?:{_SECRET_WORDS})[A-Za-z0-9_.-]*"
    r"|(?:[A-Za-z0-9]+_)*pat(?:_[A-Za-z0-9]+)*)$"
)
# The same in the text patterns, which match a name only where a run of name
# characters starts (_NAME_START) and with every repetition bounded. With
# unbounded repetitions and a match at any word boundary, each start
# position could scan the rest of the line, so a long line of name
# characters (``a.b.a.b.``) took quadratic time. A name with more than
# _NAME_PART characters before or after its credential word is not matched.
_NAME_START = r"(?<![A-Za-z0-9_.-])"
_NAME_PART = 128
_SECRET_NAME = (
    rf"[A-Za-z0-9_.-]{{0,{_NAME_PART}}}(?:{_SECRET_WORDS})[A-Za-z0-9_.-]{{0,{_NAME_PART}}}"
    r"|(?:[A-Za-z0-9]{1,32}_){0,8}pat(?:_[A-Za-z0-9]{1,32}){0,8}"
)
# Longest URL scheme and user information matched.
_URL_PART = 256

_PATTERNS: tuple[tuple[re.Pattern[str], str], ...] = (
    # Header values, to the end of the line.
    (
        re.compile(
            r"(?im)\b((?:proxy-)?authorization|set-cookie|cookie|x-api-key|api-key)"
            r"(\s*[:=]\s*)(\S[^\r\n]*)"
        ),
        rf"\1\2{REDACTED}",
    ),
    (re.compile(r"(?i)\b(bearer|basic)(\s+)[A-Za-z0-9._~+/=-]{8,}"), rf"\1\2{REDACTED}"),
    # URL user information.
    (
        re.compile(
            rf"(?i)\b([a-z][a-z0-9+.-]{{0,31}}://)[^\s/@:]{{0,{_URL_PART}}}:[^\s/@]{{0,{_URL_PART}}}@"
        ),
        rf"\1{REDACTED}@",
    ),
    (
        re.compile(rf"(?i)\b([a-z][a-z0-9+.-]{{0,31}}://)[^\s/@:]{{16,{_URL_PART}}}@"),
        rf"\1{REDACTED}@",
    ),
    # NAME=value and "name": "value" with a credential-like name.
    (
        re.compile(
            rf"(?i){_NAME_START}({_SECRET_NAME})(\s*=\s*)"
            r"(?:\"[^\"\r\n]*\"|'[^'\r\n]*'|[^\s'\"]+)"
        ),
        rf"\1\2{REDACTED}",
    ),
    (
        re.compile(rf"(?i)([\"']?){_NAME_START}({_SECRET_NAME})\1(\s*:\s*)([\"'])[^\"'\r\n]+\4"),
        rf"\1\2\1\3\4{REDACTED}\4",
    ),
    # Well-known token shapes.
    # The distinctive prefixes match even when glued to other text.
    (re.compile(r"sk-(?:ant|proj|svcacct)-[A-Za-z0-9_-]{16,}"), REDACTED),
    (re.compile(r"\bsk-[A-Za-z0-9_-]{16,}"), REDACTED),
    (re.compile(r"\b(?:gh[pousr]_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,})"), REDACTED),
    (re.compile(r"\bglpat-[A-Za-z0-9_-]{16,}"), REDACTED),
    (re.compile(r"\bnpm_[A-Za-z0-9]{30,}"), REDACTED),
    (re.compile(r"\bxox[abprs]-[A-Za-z0-9-]{10,}"), REDACTED),
    (re.compile(r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b"), REDACTED),
    (re.compile(r"\bAIza[0-9A-Za-z_-]{35}"), REDACTED),
    (re.compile(r"\bya29\.[0-9A-Za-z_.-]{20,}"), REDACTED),
    # Only where a run of token characters starts (as for names above).
    (
        re.compile(
            r"(?<![A-Za-z0-9_-])eyJ[A-Za-z0-9_-]{8,}\.eyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}"
        ),
        REDACTED,
    ),
    (re.compile(r"openshell:resolve:env:[A-Za-z0-9_:.-]+"), REDACTED),
)


def is_secret_name(name: str) -> bool:
    """Whether the variable *name* looks like it holds a credential."""
    return bool(_SECRET_NAME_RE.fullmatch(name))


def secret_values(env: Mapping[str, str]) -> tuple[str, ...]:
    """The values of *env* whose names look like credentials, longest first.

    Values shorter than :data:`MIN_SECRET_LENGTH` are left out, as are values
    that are not strings. Surrounding whitespace is stripped, so a key read
    from a file with a trailing newline still matches.
    """
    values = {
        value.strip()
        for name, value in env.items()
        if isinstance(value, str) and is_secret_name(name)
    }
    return tuple(sorted((v for v in values if len(v) >= MIN_SECRET_LENGTH), key=len, reverse=True))


def redact(text: str, secrets: Iterable[str] = ()) -> str:
    """Return *text* with credentials replaced by :data:`REDACTED` (see the module docstring)."""
    for secret in sorted(
        {s for s in secrets if isinstance(s, str) and len(s) >= MIN_SECRET_LENGTH},
        key=len,
        reverse=True,
    ):
        text = text.replace(secret, REDACTED)
    for pattern, replacement in _PATTERNS:
        text = pattern.sub(replacement, text)
    return text
