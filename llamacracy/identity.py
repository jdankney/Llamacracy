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
