"""Authentication primitives: password hashing and signed access tokens.

Reading the map and the statistics is public. Changing something (accepting or
rejecting a burned-area polygon, re-queuing an event) needs a bearer token obtained
with an e-mail and password. Passwords are stored as Argon2id hashes; tokens are
short-lived HS256 JWTs carrying the user id and role.
"""

from __future__ import annotations

import datetime as dt
import uuid
from dataclasses import dataclass
from typing import Any, Literal

import jwt
from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError

Role = Literal["analyst", "admin"]

_hasher = PasswordHasher()
_ALGORITHM = "HS256"
_ISSUER = "narsat-api"


@dataclass(frozen=True)
class User:
    user_id: uuid.UUID
    email: str
    role: Role


class TokenError(ValueError):
    """The token is missing, malformed, expired or signed with another key."""


def hash_password(password: str) -> str:
    return _hasher.hash(password)


def verify_password(password: str, stored_hash: str) -> bool:
    try:
        return _hasher.verify(stored_hash, password)
    except (VerificationError, InvalidHashError):
        return False


def needs_rehash(stored_hash: str) -> bool:
    """True when the hash was made with weaker parameters than the current defaults."""
    return _hasher.check_needs_rehash(stored_hash)


def issue_token(user: User, secret: str, ttl_minutes: int, now: dt.datetime | None = None) -> str:
    issued = now or dt.datetime.now(dt.UTC)
    claims: dict[str, Any] = {
        "iss": _ISSUER,
        "sub": str(user.user_id),
        "email": user.email,
        "role": user.role,
        "iat": issued,
        "exp": issued + dt.timedelta(minutes=ttl_minutes),
    }
    return jwt.encode(claims, secret, algorithm=_ALGORITHM)


def read_token(token: str, secret: str) -> User:
    try:
        claims = jwt.decode(
            token,
            secret,
            algorithms=[_ALGORITHM],
            issuer=_ISSUER,
            options={"require": ["exp", "sub", "role", "iss"]},
        )
        role = claims["role"]
        if role not in ("analyst", "admin"):
            raise TokenError("unknown role")
        return User(uuid.UUID(claims["sub"]), str(claims.get("email", "")), role)
    except jwt.PyJWTError as exc:
        raise TokenError(str(exc)) from exc
    except (KeyError, ValueError) as exc:
        raise TokenError("malformed token") from exc
