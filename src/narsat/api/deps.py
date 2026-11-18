"""FastAPI dependencies: settings, database connections, the current user."""

from __future__ import annotations

import datetime as dt
from collections.abc import AsyncIterator, Callable, Coroutine
from typing import Annotated, Any

import psycopg
from fastapi import Depends, HTTPException, Request, status
from fastapi.security import OAuth2PasswordBearer
from psycopg_pool import AsyncConnectionPool

from narsat.api import auth
from narsat.config import BoundingBox, Settings

AsyncConn = psycopg.AsyncConnection[dict[str, Any]]

_oauth2 = OAuth2PasswordBearer(tokenUrl="/api/v1/auth/token", auto_error=False)


def get_settings(request: Request) -> Settings:
    settings: Settings = request.app.state.settings
    return settings


async def get_conn(request: Request) -> AsyncIterator[AsyncConn]:
    pool: AsyncConnectionPool[AsyncConn] = request.app.state.pool
    async with pool.connection() as conn:
        yield conn


SettingsDep = Annotated[Settings, Depends(get_settings)]
ConnDep = Annotated[AsyncConn, Depends(get_conn)]


async def current_user(
    settings: SettingsDep, token: Annotated[str | None, Depends(_oauth2)]
) -> auth.User:
    if not token:
        raise HTTPException(
            status.HTTP_401_UNAUTHORIZED,
            "authentication required",
            headers={"WWW-Authenticate": "Bearer"},
        )
    try:
        return auth.read_token(token, settings.api.jwt_secret.get_secret_value())
    except auth.TokenError as exc:
        raise HTTPException(
            status.HTTP_401_UNAUTHORIZED,
            "invalid or expired token",
            headers={"WWW-Authenticate": "Bearer"},
        ) from exc


UserDep = Annotated[auth.User, Depends(current_user)]


def require_role(*roles: str) -> Callable[[auth.User], Coroutine[Any, Any, auth.User]]:
    async def dependency(user: UserDep) -> auth.User:
        if user.role not in roles:
            raise HTTPException(status.HTTP_403_FORBIDDEN, "insufficient role")
        return user

    return dependency


def parse_bbox(value: str | None) -> BoundingBox | None:
    """``west,south,east,north`` query parameter."""
    if value is None:
        return None
    try:
        return BoundingBox.parse(value)
    except ValueError as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, f"bbox: {exc}") from exc


def ensure_utc(value: dt.datetime | None) -> dt.datetime | None:
    """Treat naive timestamps from query strings as UTC."""
    if value is None:
        return None
    return value if value.tzinfo else value.replace(tzinfo=dt.UTC)
