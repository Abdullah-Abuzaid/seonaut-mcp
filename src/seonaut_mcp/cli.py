"""Command line: run the server, create or check tokens, check the database connection."""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from datetime import UTC, datetime

from . import __version__
from .config import ConfigError, Settings


def _settings() -> Settings:
    try:
        return Settings.from_env()
    except ConfigError as exc:
        sys.exit(f"Configuration error: {exc}")


def cmd_serve(_args: argparse.Namespace) -> None:
    import uvicorn

    from .server import create_app

    settings = _settings()
    logging.basicConfig(level=os.environ.get("SEONAUT_MCP_LOG_LEVEL", "INFO").upper())
    uvicorn.run(
        create_app(settings),
        host=settings.host,
        port=settings.port,
        proxy_headers=True,
        forwarded_allow_ips=os.environ.get("SEONAUT_MCP_TRUSTED_PROXIES", "127.0.0.1"),
        log_level=os.environ.get("SEONAUT_MCP_LOG_LEVEL", "info").lower(),
    )


def cmd_token_create(args: argparse.Namespace) -> None:
    from .auth import Authenticator, AuthError
    from .db import Database

    settings = _settings()
    auth = Authenticator(settings, Database(settings.database))
    try:
        issued = auth.issue(args.email, args.days)
    except AuthError as exc:
        sys.exit(str(exc))
    expires = datetime.fromtimestamp(issued.expires_at, UTC).isoformat() if issued.expires_at else "never"
    if args.json:
        print(
            json.dumps(
                {
                    "token": issued.token,
                    "token_id": issued.token_id,
                    "account": issued.user.email,
                    "expires_at": issued.expires_at,
                }
            )
        )
        return
    print(f"Account:  {issued.user.email}")
    print(f"Token id: {issued.token_id}  (add it to SEONAUT_MCP_REVOKED_TOKENS to revoke this token)")
    print(f"Expires:  {expires}")
    print()
    print(issued.token)


def cmd_token_check(args: argparse.Namespace) -> None:
    from .auth import Authenticator, AuthError
    from .db import Database

    settings = _settings()
    auth = Authenticator(settings, Database(settings.database))
    try:
        claims = auth.decode(args.token)
        user = auth.verify(args.token)
    except AuthError as exc:
        sys.exit(f"Invalid: {exc}")
    expires = datetime.fromtimestamp(claims["exp"], UTC).isoformat() if "exp" in claims else "never"
    print(f"Valid for {user.email} (token id {claims['jti']}, expires {expires})")


def cmd_check(_args: argparse.Namespace) -> None:
    from .db import TESTED_SCHEMA_VERSION, Database

    settings = Settings.from_env(require_secret=False)
    db = Database(settings.database)
    try:
        version = db.schema_version()
        users = db.scalar("SELECT COUNT(*) FROM users WHERE deleting = 0")
        projects = db.scalar("SELECT COUNT(*) FROM projects WHERE deleting = 0")
    except Exception as exc:  # noqa: BLE001 - report any connection problem plainly
        sys.exit(f"Cannot read the SEOnaut database: {exc}")
    print(
        f"Database OK: schema version {version} (tested with {TESTED_SCHEMA_VERSION}), "
        f"{users} users, {projects} projects"
    )
    if version != TESTED_SCHEMA_VERSION:
        print("Note: this SEOnaut version differs from the one this release was tested with.")


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="seonaut-mcp", description="MCP server for SEOnaut")
    parser.add_argument("--version", action="version", version=f"seonaut-mcp {__version__}")
    sub = parser.add_subparsers(dest="command")

    sub.add_parser("serve", help="Run the MCP server (default)").set_defaults(func=cmd_serve)
    sub.add_parser("check", help="Check the database connection").set_defaults(func=cmd_check)

    token = sub.add_parser("token", help="Create or check access tokens")
    token_sub = token.add_subparsers(dest="token_command", required=True)
    create = token_sub.add_parser("create", help="Create a token for a SEOnaut user")
    create.add_argument("--email", required=True, help="Email of an existing SEOnaut user")
    create.add_argument("--days", type=int, default=0, help="Days until it expires (0 = never)")
    create.add_argument("--json", action="store_true", help="Print JSON")
    create.set_defaults(func=cmd_token_create)
    check = token_sub.add_parser("check", help="Check a token")
    check.add_argument("token")
    check.set_defaults(func=cmd_token_check)

    args = parser.parse_args(argv)
    if not getattr(args, "func", None):
        args.func = cmd_serve
    args.func(args)


if __name__ == "__main__":
    main()
