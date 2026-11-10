"""The MCP server: tools over Streamable HTTP, bearer-token auth, /auth/token and /healthz."""

from __future__ import annotations

import json
import logging
from collections.abc import Callable
from typing import Annotated, Any, Literal

import anyio
import pymysql
from mcp.server.mcpserver import Context, MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.server.transport_security import TransportSecuritySettings
from mcp.types import ToolAnnotations
from pydantic import Field
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.types import ASGIApp, Receive, Scope, Send

from . import __version__
from .auth import Authenticator, AuthError, LoginRateLimiter, User, bearer_token
from .config import Settings
from .db import TESTED_SCHEMA_VERSION, Database
from .queries import NotFound, Queries

log = logging.getLogger("seonaut_mcp")

INSTRUCTIONS = """\
Read-only access to SEOnaut, a self-hosted SEO crawler and auditor. You see the projects (websites) of
the SEOnaut account your token belongs to, with the results of their crawls.

Typical flow:
1. list_projects to find the site and its latest crawl.
2. crawl_summary for the health overview (issues by priority: critical > alert > warning).
3. list_issues, then pages_with_issue for the pages behind one issue type.
4. page_report for everything about one page (meta tags, headings, links, images, issues).
5. broken_links for links that point to 4xx/5xx pages, with the pages that contain them.

Crawls are started in SEOnaut itself. Tools read the latest finished crawl: SEOnaut deletes the pages and
issues of the previous crawl when a new one finishes, and keeps only its totals (see list_crawls). To
verify fixes, note list_issues before the site is re-crawled and compare it with list_issues afterwards.
Fix critical issues first, then alerts, then warnings."""

READ_ONLY = ToolAnnotations(
    read_only_hint=True, destructive_hint=False, idempotent_hint=True, open_world_hint=False
)

ProjectId = Annotated[int, Field(description="SEOnaut project id, from list_projects")]
Limit = Annotated[int, Field(ge=1, le=500, description="Maximum rows to return")]
Offset = Annotated[int, Field(ge=0, description="Rows to skip, for paging")]


def build_mcp(settings: Settings, auth: Authenticator, queries: Queries) -> MCPServer:
    mcp = MCPServer(name="seonaut", title="SEOnaut", version=__version__, instructions=INSTRUCTIONS)

    async def run(
        ctx: Context, fn: Callable[..., dict[str, Any]], *args: Any, **kwargs: Any
    ) -> dict[str, Any]:
        token = bearer_token(ctx.headers)
        if not token:
            raise ToolError("Missing bearer token")
        try:
            user: User = await anyio.to_thread.run_sync(auth.verify, token)
            return await anyio.to_thread.run_sync(lambda: fn(user, *args, **kwargs))
        except (AuthError, NotFound, ValueError) as exc:
            raise ToolError(str(exc)) from exc
        except pymysql.Error as exc:
            log.exception("Database error")
            raise ToolError(
                "Could not read SEOnaut's database. Ask the administrator to check the server."
            ) from exc

    @mcp.tool(annotations=READ_ONLY)
    async def list_projects(ctx: Context) -> dict[str, Any]:
        """List the SEOnaut projects (websites) of this account, each with its latest crawl and issue counts."""
        return await run(ctx, queries.list_projects)

    @mcp.tool(annotations=READ_ONLY)
    async def list_crawls(ctx: Context, project_id: ProjectId, limit: Limit = 10) -> dict[str, Any]:
        """List a project's crawls, newest first, with status (crawling, analysing, finished) and issue totals. Only totals are kept for older crawls."""
        return await run(ctx, queries.list_crawls, project_id, limit)

    @mcp.tool(annotations=READ_ONLY)
    async def crawl_summary(ctx: Context, project_id: ProjectId) -> dict[str, Any]:
        """Health overview of the latest finished crawl: pages, status codes, robots.txt and sitemap, links, and every failing
        issue type grouped by priority (critical, alert, warning) with the number of pages affected."""
        return await run(ctx, queries.crawl_summary, project_id)

    @mcp.tool(annotations=READ_ONLY)
    async def list_issues(
        ctx: Context,
        project_id: ProjectId,
        priority: Annotated[
            Literal["critical", "alert", "warning"] | None, Field(description="Only this priority")
        ] = None,
        include_passed: Annotated[bool, Field(description="Also list issue types with no pages")] = False,
    ) -> dict[str, Any]:
        """Failing issue types of the latest crawl with title, explanation, priority and number of pages affected."""
        return await run(ctx, queries.list_issues, project_id, priority, include_passed)

    @mcp.tool(annotations=READ_ONLY)
    async def pages_with_issue(
        ctx: Context,
        project_id: ProjectId,
        issue_type: Annotated[
            str, Field(description="Issue type, e.g. ERROR_EMPTY_TITLE (from list_issues)")
        ],
        limit: Limit = 50,
        offset: Offset = 0,
    ) -> dict[str, Any]:
        """Pages that have one issue type, with url, title, status code, depth and word count."""
        return await run(ctx, queries.pages_with_issue, project_id, issue_type, limit, offset)

    @mcp.tool(annotations=READ_ONLY)
    async def page_report(
        ctx: Context,
        project_id: ProjectId,
        url: Annotated[str | None, Field(description="Full page URL as crawled")] = None,
        page_id: Annotated[int | None, Field(description="Page id from another tool")] = None,
        link_limit: Annotated[int, Field(ge=1, le=500, description="Maximum links/images per list")] = 100,
    ) -> dict[str, Any]:
        """Everything about one page: status, title, meta description, canonical, robots, headings, language,
        word count, size, response time, its issues, links in and out (with target status), images with alt
        text and hreflangs. Give url or page_id."""
        return await run(ctx, queries.page_report, project_id, url, page_id, link_limit)

    @mcp.tool(annotations=READ_ONLY)
    async def search_pages(
        ctx: Context,
        project_id: ProjectId,
        url_contains: Annotated[str | None, Field(description="Text the URL contains")] = None,
        title_contains: Annotated[str | None, Field(description="Text the title contains")] = None,
        status_code: Annotated[int | None, Field(description="Exact HTTP status code")] = None,
        min_status: Annotated[int | None, Field(description="Lowest status code, e.g. 400")] = None,
        max_status: Annotated[int | None, Field(description="Highest status code, e.g. 599")] = None,
        noindex: Annotated[
            bool | None, Field(description="Only noindex (true) or indexable (false) pages")
        ] = None,
        in_sitemap: Annotated[
            bool | None, Field(description="Only pages in (true) or not in (false) the sitemap")
        ] = None,
        html_only: Annotated[bool, Field(description="Only HTML pages (no images, scripts, PDFs)")] = True,
        min_depth: Annotated[int | None, Field(description="Minimum clicks from the start page")] = None,
        sort: Annotated[
            Literal["url", "depth", "words", "size", "ttfb"],
            Field(description="url A-Z, deepest, fewest words, largest, or slowest first"),
        ] = "url",
        limit: Limit = 50,
        offset: Offset = 0,
    ) -> dict[str, Any]:
        """Find crawled pages by URL, title, status code, indexability, sitemap presence or depth."""
        return await run(
            ctx,
            queries.search_pages,
            project_id,
            url_contains,
            title_contains,
            status_code,
            min_status,
            max_status,
            noindex,
            in_sitemap,
            html_only,
            min_depth,
            sort,
            limit,
            offset,
        )

    @mcp.tool(annotations=READ_ONLY)
    async def broken_links(
        ctx: Context,
        project_id: ProjectId,
        include_external: Annotated[bool, Field(description="Also list broken external links")] = True,
        include_redirects: Annotated[bool, Field(description="Also list links to redirects (3xx)")] = False,
        include_resources: Annotated[
            bool, Field(description="Also list broken images, scripts and stylesheets")
        ] = True,
        limit: Limit = 100,
        offset: Offset = 0,
    ) -> dict[str, Any]:
        """Links, images, scripts and stylesheets that point to error pages (4xx/5xx), each with the page that
        contains it, so they can be fixed at the source."""
        return await run(
            ctx,
            queries.broken_links,
            project_id,
            include_external,
            include_redirects,
            include_resources,
            limit,
            offset,
        )

    @mcp.tool(annotations=READ_ONLY)
    async def list_issue_types(ctx: Context) -> dict[str, Any]:
        """Every check SEOnaut runs, with its priority, title and explanation."""
        return await run(ctx, lambda _user: queries.list_issue_types())

    @mcp.tool(annotations=READ_ONLY)
    async def whoami(ctx: Context) -> dict[str, Any]:
        """The SEOnaut account this token belongs to, and the server and database versions."""

        def info(user: User) -> dict[str, Any]:
            schema = queries.db.schema_version()
            return {
                "account": user.email,
                "server_version": __version__,
                "seonaut_schema_version": schema,
                "tested_schema_version": TESTED_SCHEMA_VERSION,
                "seonaut_url": settings.seonaut_url or None,
            }

        return await run(ctx, info)

    return mcp


class BearerGuard:
    """Reject requests to the MCP endpoint that lack a valid SEOnaut token."""

    def __init__(self, app: ASGIApp, auth: Authenticator, prefix: str = "/mcp") -> None:
        self.app = app
        self.auth = auth
        self.prefix = prefix

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] == "http" and scope["path"].rstrip("/").startswith(self.prefix):
            headers = {k.decode("latin-1").lower(): v.decode("latin-1") for k, v in scope["headers"]}
            token = bearer_token(headers)
            error = "Missing bearer token. Send 'Authorization: Bearer <token>'."
            if token:
                try:
                    await anyio.to_thread.run_sync(self.auth.verify, token)
                    error = ""
                except AuthError as exc:
                    error = str(exc)
                except pymysql.Error:
                    log.exception("Database error during authentication")
                    response = JSONResponse({"error": "Authentication backend unavailable"}, status_code=503)
                    await response(scope, receive, send)
                    return
            if error:
                response = JSONResponse(
                    {"error": "unauthorized", "message": error},
                    status_code=401,
                    headers={"WWW-Authenticate": 'Bearer realm="seonaut-mcp"'},
                )
                await response(scope, receive, send)
                return
        await self.app(scope, receive, send)


def create_app(settings: Settings | None = None) -> ASGIApp:
    settings = settings or Settings.from_env()
    db = Database(settings.database)
    auth = Authenticator(settings, db)
    queries = Queries(db, settings.max_rows, settings.seonaut_url)
    limiter = LoginRateLimiter(settings.login_attempts, settings.login_window_seconds)
    mcp = build_mcp(settings, auth, queries)

    @mcp.custom_route("/healthz", methods=["GET"], include_in_schema=False)
    async def healthz(_request: Request) -> Response:
        try:
            schema = await anyio.to_thread.run_sync(db.schema_version)
        except pymysql.Error:
            return JSONResponse({"status": "error", "database": "unreachable"}, status_code=503)
        return JSONResponse(
            {
                "status": "ok",
                "version": __version__,
                "seonaut_schema_version": schema,
                "tested_schema_version": TESTED_SCHEMA_VERSION,
            }
        )

    @mcp.custom_route("/auth/token", methods=["POST"], include_in_schema=False)
    async def token(request: Request) -> Response:
        if not settings.password_login:
            return JSONResponse({"error": "Password login is disabled on this server"}, status_code=404)
        client = request.client.host if request.client else "unknown"
        if limiter.blocked(client):
            return JSONResponse({"error": "Too many failed attempts. Try again later."}, status_code=429)
        try:
            if request.headers.get("content-type", "").startswith("application/json"):
                body = await request.json()
            else:
                body = dict(await request.form())
            email, password = str(body.get("email", "")), str(body.get("password", ""))
            days = body.get("days")
            days = None if days in (None, "") else int(days)
        except (ValueError, json.JSONDecodeError):
            return JSONResponse(
                {"error": 'Send JSON: {"email": ..., "password": ..., "days": optional}'}, 400
            )
        if not email or not password:
            return JSONResponse({"error": "email and password are required"}, status_code=400)
        if days is not None and (
            days < 0 or (settings.token_days and (days == 0 or days > settings.token_days))
        ):
            limit = f"1 to {settings.token_days}" if settings.token_days else "0 (no expiry) or more"
            return JSONResponse({"error": f"days must be {limit}"}, status_code=400)
        try:
            await anyio.to_thread.run_sync(auth.check_password, email, password)
            issued = await anyio.to_thread.run_sync(auth.issue, email, days)
        except AuthError:
            limiter.failed(client)
            return JSONResponse({"error": "Invalid email or password"}, status_code=401)
        except pymysql.Error:
            log.exception("Database error during login")
            return JSONResponse({"error": "Authentication backend unavailable"}, status_code=503)
        limiter.succeeded(client)
        return JSONResponse(
            {
                "token": issued.token,
                "token_id": issued.token_id,
                "account": issued.user.email,
                "expires_at": issued.expires_at,
            }
        )

    security = None
    if settings.allowed_hosts:
        security = TransportSecuritySettings(
            enable_dns_rebinding_protection=True,
            allowed_hosts=settings.allowed_hosts,
            allowed_origins=[f"https://{h}" for h in settings.allowed_hosts]
            + [f"http://{h}" for h in settings.allowed_hosts],
        )
    else:
        security = TransportSecuritySettings(enable_dns_rebinding_protection=False)

    app = mcp.streamable_http_app(
        streamable_http_path="/mcp",
        stateless_http=True,
        json_response=True,
        transport_security=security,
        host=settings.host,
    )
    return BearerGuard(app, auth)
