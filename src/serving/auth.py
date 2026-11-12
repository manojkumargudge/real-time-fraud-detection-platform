"""API-key check for the scoring API's write route (``POST /score``).

The key comes from ``API_KEY`` or, preferred for Docker/Kubernetes secrets, from the file named
by ``API_KEY_FILE`` (first line, stripped). Clients send it in the ``X-API-Key`` header; it is
compared in constant time. ``/health``, ``/model``, ``/docs`` and ``/openapi.json`` stay open.

Fail closed: with no key configured the API still starts (health checks keep working), logs an
ERROR, and answers every ``POST /score`` with 401.
"""

from __future__ import annotations

import hmac
import logging
import os
from pathlib import Path

logger = logging.getLogger(__name__)

API_KEY_HEADER = "X-API-Key"
MIN_KEY_LENGTH = 16


class ApiKeyError(ValueError):
    """The configured key is unusable (too short, or the key file is unreadable)."""


def load_api_key() -> str | None:
    """``API_KEY``, else the first line of ``API_KEY_FILE``; None if neither is set or the file
    does not exist. A key shorter than MIN_KEY_LENGTH is rejected."""
    key = os.environ.get("API_KEY", "").strip()
    path = os.environ.get("API_KEY_FILE", "").strip()
    if not key and path:
        p = Path(path)
        if p.is_file():
            try:
                key = p.read_text(encoding="utf-8").splitlines()[0].strip()
            except (OSError, IndexError) as exc:
                raise ApiKeyError(f"cannot read the API key from {p}") from exc
    if not key:
        return None
    if len(key) < MIN_KEY_LENGTH:
        raise ApiKeyError(f"the API key must be at least {MIN_KEY_LENGTH} characters")
    return key


def key_matches(expected: str | None, given: str | None) -> bool:
    """Constant-time comparison; False when no key is configured or none was sent."""
    if not expected or not given:
        return False
    return hmac.compare_digest(expected.encode("utf-8"), given.encode("utf-8"))
