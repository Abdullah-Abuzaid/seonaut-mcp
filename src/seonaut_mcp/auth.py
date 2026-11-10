"""Authentication with SEOnaut's own user accounts.

Tokens are signed JWTs (HS256) and are never stored. A token names a SEOnaut user and carries a
fingerprint of that user's password hash, so it stops working as soon as the user is deleted or
changes their password in SEOnaut. Single tokens can be revoked by listing their id (jti) in
SEONAUT_MCP_REVOKED_TOKENS.
"""

from __future__ import annotations

import hashlib
import hmac
import threading
import time
import uuid
from collections import deque
from dataclasses import dataclass
from typing import Any

import bcrypt
import jwt

from .config import Settings
from .db import Database

ISSUER = "seonaut-mcp"
ALGORITHM = "HS256"


class AuthError(Exception):
    """Authentication failed. The message is safe to show to the client."""


@dataclass(frozen=True)
class User:
    id: int
    email: str


@dataclass(frozen=True)
class IssuedToken:
    token: str
    token_id: str
    user: User
    expires_at: int | None


class Authenticator:
    def __init__(self, settings: Settings, db: Database) -> None:
        self._settings = settings
        self._db = db
        self._cache: dict[str, tuple[float, User]] = {}
        self._cache_lock = threading.Lock()
        # Used for unknown users so a failed login costs the same time whether or not the email exists.
        self._dummy_hash = bcrypt.hashpw(b"seonaut-mcp-dummy", bcrypt.gensalt(10)).decode()

    # -- users ---------------------------------------------------------------------------------

    def _user_row_by_email(self, email: str) -> dict[str, Any] | None:
        return self._db.one(
            "SELECT id, email, password FROM users WHERE email = %s AND deleting = 0",
            (email.strip(),),
        )

    def _user_row_by_id(self, user_id: int) -> dict[str, Any] | None:
        return self._db.one(
            "SELECT id, email, password FROM users WHERE id = %s AND deleting = 0",
            (user_id,),
        )

    def _fingerprint(self, password_hash: str) -> str:
        digest = hmac.new(self._settings.secret.encode(), password_hash.encode(), hashlib.sha256)
        return digest.hexdigest()[:24]

    def check_password(self, email: str, password: str) -> User:
        """Check an email and password against SEOnaut's bcrypt hashes."""
        row = self._user_row_by_email(email)
        stored = row["password"] if row else self._dummy_hash
        try:
            ok = bcrypt.checkpw(password.encode(), stored.encode())
        except ValueError:
            ok = False
        if not row or not ok:
            raise AuthError("Invalid email or password")
        return User(id=int(row["id"]), email=row["email"])

    # -- tokens --------------------------------------------------------------------------------

    def issue(self, email: str, days: int | None = None) -> IssuedToken:
        """Create a token for an existing SEOnaut user (no password needed: callers are trusted)."""
        row = self._user_row_by_email(email)
        if not row:
            raise AuthError(f"No active SEOnaut user with email {email!r}")
        now = int(time.time())
        days = self._settings.token_days if days is None else days
        token_id = uuid.uuid4().hex
        claims: dict[str, Any] = {
            "iss": ISSUER,
            "sub": str(row["id"]),
            "email": row["email"],
            "pwf": self._fingerprint(row["password"]),
            "iat": now,
            "jti": token_id,
        }
        expires_at = now + days * 86400 if days else None
        if expires_at:
            claims["exp"] = expires_at
        token = jwt.encode(claims, self._settings.secret, algorithm=ALGORITHM)
        return IssuedToken(token, token_id, User(int(row["id"]), row["email"]), expires_at)

    def decode(self, token: str) -> dict[str, Any]:
        try:
            claims = jwt.decode(
                token,
                self._settings.secret,
                algorithms=[ALGORITHM],
                issuer=ISSUER,
                options={"require": ["sub", "iat", "jti", "pwf"]},
            )
        except jwt.ExpiredSignatureError as exc:
            raise AuthError("Token expired") from exc
        except jwt.InvalidTokenError as exc:
            raise AuthError("Invalid token") from exc
        if claims["jti"] in self._settings.revoked_tokens:
            raise AuthError("Token revoked")
        return claims

    def verify(self, token: str) -> User:
        """Return the SEOnaut user a token belongs to, or raise AuthError."""
        claims = self.decode(token)
        now = time.monotonic()
        with self._cache_lock:
            cached = self._cache.get(token)
            if cached and cached[0] > now:
                return cached[1]
        row = self._user_row_by_id(int(claims["sub"]))
        if not row or not hmac.compare_digest(self._fingerprint(row["password"]), claims["pwf"]):
            raise AuthError("Token no longer valid: the user was removed or changed their password")
        user = User(id=int(row["id"]), email=row["email"])
        with self._cache_lock:
            if len(self._cache) > 10_000:
                self._cache.clear()
            self._cache[token] = (now + self._settings.user_cache_seconds, user)
        return user


class LoginRateLimiter:
    """Allow a limited number of failed logins per client address in a time window."""

    def __init__(self, attempts: int, window_seconds: int) -> None:
        self._attempts = attempts
        self._window = window_seconds
        self._failures: dict[str, deque[float]] = {}
        self._lock = threading.Lock()

    def _recent(self, key: str, now: float) -> deque[float]:
        failures = self._failures.setdefault(key, deque())
        while failures and failures[0] <= now - self._window:
            failures.popleft()
        return failures

    def blocked(self, key: str) -> bool:
        with self._lock:
            return len(self._recent(key, time.monotonic())) >= self._attempts

    def failed(self, key: str) -> None:
        with self._lock:
            now = time.monotonic()
            self._recent(key, now).append(now)
            if len(self._failures) > 10_000:
                self._failures = {k: v for k, v in self._failures.items() if v}

    def succeeded(self, key: str) -> None:
        with self._lock:
            self._failures.pop(key, None)


def bearer_token(headers: Any) -> str | None:
    """Extract the bearer token from a headers mapping (case-insensitive)."""
    if headers is None:
        return None
    value = headers.get("authorization") or headers.get("Authorization")
    if not value:
        return None
    scheme, _, token = value.partition(" ")
    if scheme.lower() != "bearer" or not token.strip():
        return None
    return token.strip()
