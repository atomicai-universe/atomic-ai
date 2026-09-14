"""Secret scrubbing for metadata used in responses, logs, and audit records.

This module provides :func:`scrub`, which returns a deep-copied structure with
values redacted whenever their key matches a sensitive pattern. It is used to
ensure OAuth credentials and session tokens never leak into API responses,
logs, or audit records.

Requirements: 6.4 (exclude decrypted OAuth tokens from responses/logs/audit),
15.4 (error output excludes secret values).
"""

from __future__ import annotations

import re
from typing import Any

# Value substituted in place of any redacted secret.
REDACTED = "***"

# Case-insensitive substring fragments. A key is considered sensitive when any
# of these fragments appears anywhere within it. Kept as a module-level
# constant so callers/tests can inspect or extend the configured set.
SENSITIVE_KEY_PATTERNS: tuple[str, ...] = (
    "access_token",
    "refresh_token",
    "encrypted_access_token",
    "encrypted_refresh_token",
    "session_token",
    "token",
    "code_verifier",
    "client_secret",
    "authorization",
    "password",
    "passwd",
    "secret",
    "api_key",
    "apikey",
    "nonce",
    "credential",
    "private_key",
)

# Precompiled case-insensitive matcher over the fragments above.
_SENSITIVE_KEY_RE = re.compile(
    "|".join(re.escape(fragment) for fragment in SENSITIVE_KEY_PATTERNS),
    re.IGNORECASE,
)


def is_sensitive_key(key: Any) -> bool:
    """Return True if ``key`` looks like it holds a secret value."""
    if not isinstance(key, str):
        return False
    return _SENSITIVE_KEY_RE.search(key) is not None


def scrub(obj: Any) -> Any:
    """Return a deep-copied structure with sensitive values redacted.

    Recursively walks dicts and lists/tuples. Any dict value whose key matches
    a sensitive pattern is replaced with :data:`REDACTED`. The input is never
    mutated; a new structure is returned. Non-container scalars are returned
    unchanged.
    """
    if isinstance(obj, dict):
        result: dict[Any, Any] = {}
        for key, value in obj.items():
            if is_sensitive_key(key):
                result[key] = REDACTED
            else:
                result[key] = scrub(value)
        return result

    if isinstance(obj, (list, tuple)):
        scrubbed = [scrub(item) for item in obj]
        return type(obj)(scrubbed) if isinstance(obj, tuple) else scrubbed

    if isinstance(obj, set):
        # Sets can't contain sensitive keys (no key/value), but recurse for
        # nested containers is not possible on unhashable results; return a
        # shallow copy to avoid mutating the input.
        return set(obj)

    return obj
