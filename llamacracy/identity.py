# SPDX-License-Identifier: AGPL-3.0-or-later
"""Identity comes from oauth2-proxy's forwarded headers. The app writes no auth
code -- it trusts X-Forwarded-* because it only ever listens on 127.0.0.1 with
oauth2-proxy in front (see deploy/). If those headers are absent and DEV_MODE
is unset we refuse the request loudly: the app must never serve open.

Users are keyed on the OIDC `sub` claim (X-Forwarded-User), never the email.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from fastapi import Depends, HTTPException, Request

from .apikeys import hash_key
from .config import Settings, get_settings
from .db import Database, get_db, now

log = logging.getLogger("llamacracy.identity")

_H_SUB = "x-forwarded-user"
_H_EMAIL = "x-forwarded-email"
_H_USERNAME = "x-forwarded-preferred-username"


@dataclass(frozen=True)
class Principal:
    user_id: int
    sub: str
    email: str
    display_name: str
    is_admin: bool
    disabled: bool


async def _upsert_user(db: Database, sub: str, email: str, name: str,
                       is_admin: bool) -> Principal:
    row = await db.fetch_one("SELECT * FROM users WHERE oidc_sub = ?", (sub,))
    ts = now()
    if row is None:
        uid = await db.insert(
            "INSERT INTO users (oidc_sub, email, display_name, is_admin, created_at, last_active_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (sub, email, name or email.split("@")[0], int(is_admin), ts, ts),
        )
        return Principal(uid, sub, email, name or email.split("@")[0], is_admin, False)

    # keep email / name / admin flag fresh; identity stays pinned to sub
    await db.execute(
        "UPDATE users SET email = ?, display_name = ?, is_admin = ?, last_active_at = ? "
        "WHERE id = ?",
        (email, name or row["display_name"] or email.split("@")[0],
         int(is_admin), ts, row["id"]),
    )
    return Principal(
        row["id"], sub, email,
        name or row["display_name"] or email.split("@")[0],
        is_admin, bool(row["disabled"]),
    )


async def get_principal(
    request: Request,
    settings: Settings = Depends(get_settings),
    db: Database = Depends(get_db),
) -> Principal:
    if settings.is_dev:
        sub, email, name = settings.dev_sub, settings.dev_email, settings.dev_name
    else:
        sub = request.headers.get(_H_SUB, "").strip()
        email = request.headers.get(_H_EMAIL, "").strip().lower()
        name = request.headers.get(_H_USERNAME, "").strip()
        if not sub or not email:
            log.error(
                "request with no identity headers (X-Forwarded-User/Email) and "
                "DEV_MODE unset -- refusing. Is oauth2-proxy in front? path=%s",
                request.url.path,
            )
            raise HTTPException(
                status_code=503,
                detail="identity headers missing; the app must run behind oauth2-proxy",
            )

    is_admin = email in settings.admin_email_set
    principal = await _upsert_user(db, sub, email, name, is_admin)
    if principal.disabled:
        raise HTTPException(status_code=403, detail="account disabled")
    return principal


async def require_admin(principal: Principal = Depends(get_principal)) -> Principal:
    if not principal.is_admin:
        raise HTTPException(status_code=403, detail="admin only")
    return principal


_UNAUTHORIZED = HTTPException(
    status_code=401, detail="missing or invalid API key",
    headers={"WWW-Authenticate": "Bearer"},
)


async def get_principal_api_key(
    request: Request,
    db: Database = Depends(get_db),
) -> Principal:
    """Identity for the OpenAI-compatible endpoint (/v1/*) -- a bearer API
    key instead of oauth2-proxy's forwarded headers. That endpoint is reached
    by IDE tools directly, outside the browser session, so oauth2-proxy skips
    auth on those paths entirely (deploy/oauth2-proxy.cfg) and this is the
    only gate. Keys are generated from the usage page and stored as a sha256
    hash only (see llamacracy/apikeys.py)."""
    auth = request.headers.get("authorization", "")
    scheme, _, token = auth.partition(" ")
    if scheme.lower() != "bearer" or not token.strip():
        raise _UNAUTHORIZED
    row = await db.fetch_one(
        "SELECT k.id AS key_id, u.id, u.oidc_sub, u.email, u.display_name, "
        "  u.is_admin, u.disabled "
        "FROM api_keys k JOIN users u ON u.id = k.user_id WHERE k.key_hash = ?",
        (hash_key(token.strip()),),
    )
    if row is None:
        raise _UNAUTHORIZED
    if row["disabled"]:
        raise HTTPException(status_code=403, detail="account disabled")
    # is_admin is cached on the user row from their last OIDC login; fine to
    # trust here too -- an API key can't grant admin, only reflect it.
    await db.execute("UPDATE api_keys SET last_used_at = ? WHERE id = ?", (now(), row["key_id"]))
    return Principal(
        row["id"], row["oidc_sub"], row["email"], row["display_name"],
        bool(row["is_admin"]), bool(row["disabled"]),
    )
