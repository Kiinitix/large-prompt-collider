"""
Real(ish) redaction for request/response body previews.

This replaces the old placeholder that just reported byte counts. It's a
regex-based scrubber, not a full PII-detection model -- good enough to stop
obvious secrets and common PII shapes from leaking into the events DB, not
a substitute for a proper DLP tool if you're handling regulated data.

Patterns covered:
  - API keys / bearer tokens (OpenAI-style sk-..., generic Bearer tokens)
  - Email addresses
  - Phone numbers (loose, international-ish)
  - Credit card numbers (13-19 digits, with optional separators)
  - US Social Security Numbers
  - IPv4 addresses

Anything matched is replaced with a `[REDACTED:<label>]` marker so the
shape of the text is still visible for debugging without leaking the value.
"""
import re
from typing import Optional

_PATTERNS: list[tuple[str, re.Pattern]] = [
    ("API_KEY", re.compile(r"\b(sk|pk|api)-[A-Za-z0-9_-]{16,}\b")),
    ("BEARER_TOKEN", re.compile(r"\bBearer\s+[A-Za-z0-9\-_.~+/]{16,}=*\b")),
    ("EMAIL", re.compile(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b")),
    ("SSN", re.compile(r"\b\d{3}-\d{2}-\d{4}\b")),
    ("CREDIT_CARD", re.compile(r"\b(?:\d[ -]*?){13,19}\b")),
    ("PHONE", re.compile(r"\b(?:\+?\d{1,3}[-.\s]?)?\(?\d{3}\)?[-.\s]?\d{3}[-.\s]?\d{4}\b")),
    ("IPV4", re.compile(r"\b(?:\d{1,3}\.){3}\d{1,3}\b")),
]


def scrub(text: str) -> str:
    """Replace known-sensitive patterns in-place, preserving surrounding text."""
    for label, pattern in _PATTERNS:
        text = pattern.sub(f"[REDACTED:{label}]", text)
    return text


def make_preview(
    body: bytes,
    *,
    redact: bool = True,
    preview_chars: int = 200,
) -> Optional[str]:
    """Return a preview of a request/response body.

    redact=True (default): decode, scrub known-sensitive patterns, then
    truncate. This is safe to store in the events DB for debugging.

    redact=False: raw truncated text, no scrubbing. Only use this in a
    fully trusted, closed test environment -- see the security notes in
    docs/DOCUMENTATION.md.
    """
    if not body:
        return None
    text = body.decode("utf-8", errors="replace")
    if redact:
        text = scrub(text)
    return text[:preview_chars]
