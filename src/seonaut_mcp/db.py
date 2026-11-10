"""Read-only access to SEOnaut's MySQL database."""

from __future__ import annotations

import threading
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from datetime import date, datetime
from decimal import Decimal
from typing import Any

import pymysql
import pymysql.cursors

from .config import DatabaseSettings

# The SEOnaut schema version (schema_migrations.version) this release was tested against.
TESTED_SCHEMA_VERSION = 75


class Database:
    """A small connection pool. Every session is put in READ ONLY mode, so even a database user
    with write rights cannot change SEOnaut's data through this server."""

    def __init__(self, settings: DatabaseSettings, pool_size: int = 8) -> None:
        self._settings = settings
        self._pool: list[pymysql.connections.Connection] = []
        self._lock = threading.Lock()
        self._pool_size = pool_size

    def _connect(self) -> pymysql.connections.Connection:
        s = self._settings
        conn = pymysql.connect(
            host=s.host,
            port=s.port,
            user=s.user,
            password=s.password,
            database=s.name,
            connect_timeout=s.connect_timeout,
            read_timeout=60,
            charset="utf8mb4",
            cursorclass=pymysql.cursors.DictCursor,
            autocommit=True,
        )
        with conn.cursor() as cur:
            cur.execute("SET SESSION TRANSACTION READ ONLY")
            cur.execute("SET SESSION time_zone = '+00:00'")
        return conn

    @contextmanager
    def connection(self) -> Iterator[pymysql.connections.Connection]:
        with self._lock:
            conn = self._pool.pop() if self._pool else None
        if conn is not None:
            try:
                conn.ping(reconnect=False)
            except pymysql.Error:
                conn = None
        if conn is None:
            conn = self._connect()
        healthy = True
        try:
            yield conn
        except pymysql.OperationalError:
            healthy = False
            raise
        finally:
            with self._lock:
                if healthy and len(self._pool) < self._pool_size:
                    self._pool.append(conn)
                else:
                    conn.close()

    def all(self, sql: str, params: Sequence[Any] = ()) -> list[dict[str, Any]]:
        with self.connection() as conn, conn.cursor() as cur:
            cur.execute(sql, params)
            return [_clean(row) for row in cur.fetchall()]

    def one(self, sql: str, params: Sequence[Any] = ()) -> dict[str, Any] | None:
        rows = self.all(sql, params)
        return rows[0] if rows else None

    def scalar(self, sql: str, params: Sequence[Any] = ()) -> Any:
        row = self.one(sql, params)
        return next(iter(row.values())) if row else None

    def schema_version(self) -> int | None:
        value = self.scalar("SELECT version FROM schema_migrations LIMIT 1")
        return int(value) if value is not None else None

    def close(self) -> None:
        with self._lock:
            for conn in self._pool:
                conn.close()
            self._pool.clear()


def _clean(row: dict[str, Any]) -> dict[str, Any]:
    """Make a row JSON-friendly: datetimes as ISO 8601 UTC, decimals as numbers, bytes as text."""
    out: dict[str, Any] = {}
    for key, value in row.items():
        if isinstance(value, datetime):
            out[key] = value.replace(microsecond=0).isoformat() + "Z"
        elif isinstance(value, date):
            out[key] = value.isoformat()
        elif isinstance(value, Decimal):
            out[key] = int(value) if value == value.to_integral_value() else float(value)
        elif isinstance(value, bytes):
            out[key] = value.decode("utf-8", "replace")
        else:
            out[key] = value
    return out
