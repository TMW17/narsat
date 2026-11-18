"""Login and identity endpoints."""

from __future__ import annotations

import time
from collections import defaultdict, deque
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Request, status
from fastapi.security import OAuth2PasswordRequestForm

from narsat.api import auth
from narsat.api.deps import ConnDep, SettingsDep, UserDep

router = APIRouter(tags=["auth"])

# Brute-force guard for the login endpoint: a sliding window per client address.
# It lives in process memory, which is enough for a single API instance; behind
# several replicas the same limit belongs in the reverse proxy.
MAX_ATTEMPTS = 10
WINDOW_SECONDS = 60.0
_attempts: dict[str, deque[float]] = defaultdict(deque)

# Verifying against a real hash when the e-mail is unknown keeps the response time
# the same whether or not the account exists.
_DUMMY_HASH = auth.hash_password("narsat-timing-equaliser")


def _throttle(client: str) -> None:
    now = time.monotonic()
    window = _attempts[client]
    while window and now - window[0] > WINDOW_SECONDS:
        window.popleft()
    if len(window) >= MAX_ATTEMPTS:
        raise HTTPException(
            status.HTTP_429_TOO_MANY_REQUESTS,
            "too many login attempts, retry in a minute",
            headers={"Retry-After": str(int(WINDOW_SECONDS))},
        )
    window.append(now)


@router.post("/auth/token")
async def login(
    request: Request,
    form: Annotated[OAuth2PasswordRequestForm, Depends()],
    conn: ConnDep,
    settings: SettingsDep,
) -> dict[str, Any]:
    """Exchange e-mail and password for a bearer token (OAuth2 password flow)."""
    _throttle(request.client.host if request.client else "unknown")
    row = await (
        await conn.execute(
            "SELECT user_id, email::text AS email, password_hash, role FROM app_user "
            "WHERE email = %(email)s AND is_active",
            {"email": form.username},
        )
    ).fetchone()
    valid = auth.verify_password(form.password, row["password_hash"] if row else _DUMMY_HASH)
    if row is None or not valid:
        raise HTTPException(
            status.HTTP_401_UNAUTHORIZED,
            "incorrect e-mail or password",
            headers={"WWW-Authenticate": "Bearer"},
        )
    if auth.needs_rehash(row["password_hash"]):
        await conn.execute(
            "UPDATE app_user SET password_hash = %(hash)s WHERE user_id = %(id)s",
            {"hash": auth.hash_password(form.password), "id": row["user_id"]},
        )
    user = auth.User(row["user_id"], row["email"], row["role"])
    ttl = settings.api.jwt_ttl_minutes
    return {
        "access_token": auth.issue_token(user, settings.api.jwt_secret.get_secret_value(), ttl),
        "token_type": "bearer",
        "expires_in": ttl * 60,
        "role": user.role,
    }


@router.get("/auth/me")
async def me(user: UserDep) -> dict[str, str]:
    """The identity carried by the presented token."""
    return {"user_id": str(user.user_id), "email": user.email, "role": user.role}
