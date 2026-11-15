"""API-key check for the scoring API's write route (``POST /score``).

The key comes from the environment variable ``API_KEY`` only (Compose: the gitignored ``.env``;
Kubernetes: Secret ``fraud-auth``, key ``api-key``). Clients send it in the ``X-API-Key``
header; it is compared in constant time and never logged. ``/health``, ``/model``, ``/docs``
and ``/openapi.json`` stay open (read-only).

Fail closed: with no key configured the API still starts (health checks keep working), logs an
ERROR, and answers every ``POST /score`` with 401.
"""

from __future__ import annotations

import hmac
import os

API_KEY_HEADER = "X-API-Key"
MIN_KEY_LENGTH = 16
PLACEHOLDER = "change-me"  # the .env.example / k8s example values contain it


class ApiKeyError(ValueError):
    """The configured key is unusable (too short)."""


def load_api_key() -> str | None:
    """``API_KEY`` (stripped), or None if it is unset or empty. A key shorter than
    MIN_KEY_LENGTH is rejected, so a placeholder such as ``change-me`` cannot be used."""
    key = os.environ.get("API_KEY", "").strip()
    if not key:
        return None
    if len(key) < MIN_KEY_LENGTH:
        raise ApiKeyError(f"API_KEY must be at least {MIN_KEY_LENGTH} characters")
    if PLACEHOLDER in key.lower():
        raise ApiKeyError("API_KEY is still the .env.example placeholder; set a random key")
    return key


def key_matches(expected: str | None, given: str | None) -> bool:
    """Constant-time comparison; False when no key is configured or none was sent."""
    if not expected or not given:
        return False
    return hmac.compare_digest(expected.encode("utf-8"), given.encode("utf-8"))
