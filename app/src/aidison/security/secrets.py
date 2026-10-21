"""Secret detection and redaction for boundaries that must never retain credentials."""

from __future__ import annotations

import re


class SecretExposureError(ValueError):
    """Potential credential material tried to cross a non-secret boundary."""


_SECRET_PATTERNS = (
    re.compile(r"(?i)\b(?:authorization\s*:\s*bearer|bearer)\s+[a-z0-9._~+/=-]{8,}"),
    re.compile(
        r"(?i)\b(?:api[_-]?key|secret|password|access[_-]?token)\s*[:=]\s*['\"]?[^\s'\"]{8,}"
    ),
    re.compile(r"\bsk-[a-zA-Z0-9_-]{16,}\b"),
    re.compile(r"\bgh[pousr]_[a-zA-Z0-9]{16,}\b"),
    re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
)


def redact_secrets(value: str) -> str:
    """Return a diagnostic-safe string without leaking a matched credential."""

    redacted = value
    for pattern in _SECRET_PATTERNS:
        redacted = pattern.sub("[REDACTED_SECRET]", redacted)
    return redacted


def assert_secret_free(value: str, *, boundary: str) -> None:
    """Fail before a likely secret enters prompt, Artifact, event, or diagnostic text."""

    if any(pattern.search(value) for pattern in _SECRET_PATTERNS):
        raise SecretExposureError(f"secret-like material cannot cross {boundary} boundary")


__all__ = ["SecretExposureError", "assert_secret_free", "redact_secrets"]
