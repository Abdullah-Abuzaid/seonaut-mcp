"""Settings, read only from environment variables."""

from __future__ import annotations

import os
from dataclasses import dataclass, field

MIN_SECRET_LENGTH = 32


class ConfigError(ValueError):
    """A required setting is missing or invalid."""


def _bool(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None or raw.strip() == "":
        return default
    value = raw.strip().lower()
    if value in ("1", "true", "yes", "on"):
        return True
    if value in ("0", "false", "no", "off"):
        return False
    raise ConfigError(f"{name} must be true or false, got {raw!r}")


def _int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if raw is None or raw.strip() == "":
        return default
    try:
        return int(raw)
    except ValueError as exc:
        raise ConfigError(f"{name} must be a whole number, got {raw!r}") from exc


def _list(name: str) -> list[str]:
    raw = os.environ.get(name, "")
    return [item.strip() for item in raw.split(",") if item.strip()]


def _first(*names: str, default: str = "") -> str:
    """Return the first variable that is set, so SEOnaut's own SEONAUT_DATABASE_* names also work."""
    for name in names:
        value = os.environ.get(name)
        if value:
            return value
    return default


@dataclass(frozen=True)
class DatabaseSettings:
    host: str = "db"
    port: int = 3306
    user: str = "seonaut"
    password: str = ""
    name: str = "seonaut"
    connect_timeout: int = 10


@dataclass(frozen=True)
class Settings:
    database: DatabaseSettings = field(default_factory=DatabaseSettings)
    secret: str = ""
    host: str = "0.0.0.0"  # noqa: S104 - a container must listen on all interfaces
    port: int = 9100
    allowed_hosts: list[str] = field(default_factory=list)
    password_login: bool = True
    token_days: int = 0  # 0 = tokens from /auth/token never expire
    revoked_tokens: frozenset[str] = frozenset()
    seonaut_url: str = ""
    user_cache_seconds: int = 30
    login_attempts: int = 5
    login_window_seconds: int = 900
    max_rows: int = 500

    @classmethod
    def from_env(cls, require_secret: bool = True) -> Settings:
        database = DatabaseSettings(
            host=_first("SEONAUT_MCP_DB_HOST", "SEONAUT_DATABASE_SERVER", default="db"),
            port=int(_first("SEONAUT_MCP_DB_PORT", "SEONAUT_DATABASE_PORT", default="3306")),
            user=_first("SEONAUT_MCP_DB_USER", "SEONAUT_DATABASE_USER", default="seonaut"),
            password=_first("SEONAUT_MCP_DB_PASSWORD", "SEONAUT_DATABASE_PASSWORD"),
            name=_first("SEONAUT_MCP_DB_NAME", "SEONAUT_DATABASE_DATABASE", default="seonaut"),
            connect_timeout=_int("SEONAUT_MCP_DB_CONNECT_TIMEOUT", 10),
        )
        secret = os.environ.get("SEONAUT_MCP_SECRET", "")
        if require_secret and len(secret) < MIN_SECRET_LENGTH:
            raise ConfigError(
                f"SEONAUT_MCP_SECRET must be at least {MIN_SECRET_LENGTH} characters. "
                'Generate one with: python -c "import secrets; print(secrets.token_urlsafe(48))"'
            )
        token_days = _int("SEONAUT_MCP_TOKEN_DAYS", 0)
        if token_days < 0:
            raise ConfigError("SEONAUT_MCP_TOKEN_DAYS must be 0 (no expiry) or more")
        return cls(
            database=database,
            secret=secret,
            host=os.environ.get("SEONAUT_MCP_HOST", "0.0.0.0"),  # noqa: S104
            port=_int("SEONAUT_MCP_PORT", 9100),
            allowed_hosts=_list("SEONAUT_MCP_ALLOWED_HOSTS"),
            password_login=_bool("SEONAUT_MCP_PASSWORD_LOGIN", True),
            token_days=token_days,
            revoked_tokens=frozenset(_list("SEONAUT_MCP_REVOKED_TOKENS")),
            seonaut_url=os.environ.get("SEONAUT_MCP_SEONAUT_URL", "").rstrip("/"),
            user_cache_seconds=_int("SEONAUT_MCP_USER_CACHE_SECONDS", 30),
            login_attempts=_int("SEONAUT_MCP_LOGIN_ATTEMPTS", 5),
            login_window_seconds=_int("SEONAUT_MCP_LOGIN_WINDOW_SECONDS", 900),
            max_rows=_int("SEONAUT_MCP_MAX_ROWS", 500),
        )
