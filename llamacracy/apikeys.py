"""API keys for the OpenAI-compatible endpoint (Phase 8.2 -- Continue.dev and
similar tools). Bearer-token auth for /v1/* only; the web UI keeps using
oauth2-proxy. Only a sha256 hash is ever stored -- the plaintext key is shown
once at creation time and can't be retrieved again, same as a GitHub PAT.
"""

from __future__ import annotations

import hashlib
import secrets

PREFIX = "llk_"        # "llamacracy key" -- recognizable, not meaningful


def generate() -> tuple[str, str]:
    """Returns (plaintext key -- show once, sha256 hex digest -- store)."""
    key = PREFIX + secrets.token_urlsafe(32)
    return key, hash_key(key)


def hash_key(key: str) -> str:
    return hashlib.sha256(key.encode("utf-8")).hexdigest()
