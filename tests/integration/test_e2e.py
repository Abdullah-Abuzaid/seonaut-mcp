"""End-to-end tests against the e2e stack (SEOnaut + MySQL + test site + this server in Docker).

docker compose -f e2e/docker-compose.yml up -d --build
python e2e/seed.py
pytest -m integration
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

import bcrypt
import httpx
import pymysql
import pytest
from mcp.client.session import ClientSession
from mcp.client.streamable_http import streamable_http_client
from mcp.shared._httpx_utils import create_mcp_http_client

STATE_FILE = Path(__file__).resolve().parents[2] / "e2e" / ".state.json"
MCP_URL = os.environ.get("E2E_MCP_URL", "http://127.0.0.1:9101")
DB = {
    "host": "127.0.0.1",
    "port": int(os.environ.get("E2E_DB_PORT", "33307")),
    "user": "root",
    "password": "root",
    "database": "seonaut",
}

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(not STATE_FILE.exists(), reason="run e2e/seed.py first"),
]


@pytest.fixture(scope="module")
def state() -> dict[str, Any]:
    return json.loads(STATE_FILE.read_text())


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


def get_token(email: str, password: str) -> str:
    r = httpx.post(f"{MCP_URL}/auth/token", json={"email": email, "password": password}, timeout=30)
    assert r.status_code == 200, r.text
    return r.json()["token"]


@pytest.fixture(scope="module")
def owner_token(state) -> str:
    return get_token(state["owner"]["email"], state["owner"]["password"])


async def call(token: str, tool: str, args: dict[str, Any] | None = None) -> tuple[bool, Any]:
    """Call one tool over Streamable HTTP. Returns (is_error, structured result or error text)."""
    async with create_mcp_http_client(headers={"Authorization": f"Bearer {token}"}) as http:
        async with streamable_http_client(f"{MCP_URL}/mcp", http_client=http) as streams:
            read, write = streams[0], streams[1]
            async with ClientSession(read, write) as session:
                await session.initialize()
                result = await session.call_tool(tool, args or {})
                if result.is_error:
                    return True, " ".join(getattr(c, "text", "") for c in result.content)
                return False, result.structured_content


async def tool_names(token: str) -> set[str]:
    async with create_mcp_http_client(headers={"Authorization": f"Bearer {token}"}) as http:
        async with streamable_http_client(f"{MCP_URL}/mcp", http_client=http) as streams:
            async with ClientSession(streams[0], streams[1]) as session:
                await session.initialize()
                listed = await session.list_tools()
                for tool in listed.tools:
                    assert tool.annotations and tool.annotations.read_only_hint, tool.name
                return {t.name for t in listed.tools}


# -- HTTP and auth ------------------------------------------------------------------------------


def test_healthz():
    r = httpx.get(f"{MCP_URL}/healthz", timeout=10)
    assert r.status_code == 200
    assert r.json()["status"] == "ok"
    assert r.json()["seonaut_schema_version"] == r.json()["tested_schema_version"]


def test_mcp_requires_token():
    r = httpx.post(f"{MCP_URL}/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"}, timeout=10)
    assert r.status_code == 401
    assert r.headers["www-authenticate"].startswith("Bearer")


def test_mcp_rejects_garbage_token():
    r = httpx.post(
        f"{MCP_URL}/mcp",
        json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
        headers={"Authorization": "Bearer not-a-token"},
        timeout=10,
    )
    assert r.status_code == 401


def test_token_endpoint_rejects_wrong_password(state):
    r = httpx.post(
        f"{MCP_URL}/auth/token", json={"email": state["owner"]["email"], "password": "nope"}, timeout=30
    )
    assert r.status_code == 401


# -- tools --------------------------------------------------------------------------------------


@pytest.mark.anyio
async def test_tools_are_listed_and_read_only(owner_token):
    names = await tool_names(owner_token)
    assert {
        "list_projects",
        "list_crawls",
        "crawl_summary",
        "list_issues",
        "pages_with_issue",
        "page_report",
        "search_pages",
        "broken_links",
        "list_issue_types",
        "whoami",
    } <= names


@pytest.mark.anyio
async def test_whoami(owner_token, state):
    err, data = await call(owner_token, "whoami")
    assert not err, data
    assert data["account"] == state["owner"]["email"]


@pytest.mark.anyio
async def test_list_projects_only_shows_own_projects(owner_token, state):
    err, data = await call(owner_token, "list_projects")
    assert not err, data
    ids = [p["id"] for p in data["projects"]]
    assert ids == [state["project_id"]]
    assert data["projects"][0]["latest_crawl"]["status"] == "finished"
    assert data["projects"][0]["dashboard_url"].endswith(f"/dashboard?pid={state['project_id']}")


@pytest.mark.anyio
async def test_other_users_project_is_hidden(owner_token, state):
    err, message = await call(owner_token, "crawl_summary", {"project_id": state["other_project_id"]})
    assert err and "not found" in message


@pytest.mark.anyio
async def test_list_crawls_keeps_totals_history(owner_token, state):
    err, data = await call(owner_token, "list_crawls", {"project_id": state["project_id"]})
    assert not err, data
    crawls = data["crawls"]
    assert [c["id"] for c in crawls] == [state["second_crawl_id"], state["first_crawl_id"]]
    # The fixes removed alert issues (missing title, duplicate title/description, missing alt text).
    assert crawls[0]["alert_issues"] < crawls[1]["alert_issues"]


@pytest.mark.anyio
async def test_crawl_summary(owner_token, state):
    err, data = await call(owner_token, "crawl_summary", {"project_id": state["project_id"]})
    assert not err, data
    assert data["crawl"]["id"] == state["second_crawl_id"]
    assert data["site"]["robots_txt_exists"] and data["site"]["sitemap_exists"]
    critical = {i["type"] for i in data["issues_by_priority"]["critical"]}
    assert {"ERROR_40x", "ERROR_30x"} <= critical
    assert {s["status_code"] for s in data["status_codes"]} >= {200, 301, 404}


@pytest.mark.anyio
async def test_list_issues_with_priority(owner_token, state):
    err, data = await call(
        owner_token, "list_issues", {"project_id": state["project_id"], "priority": "critical"}
    )
    assert not err, data
    assert all(i["priority"] == "critical" for i in data["issues"])
    by_type = {i["type"]: i for i in data["issues"]}
    assert by_type["ERROR_40x"]["pages"] == 3
    assert by_type["ERROR_40x"]["title"] and by_type["ERROR_40x"]["description"]


@pytest.mark.anyio
async def test_list_issues_include_passed(owner_token, state):
    err, data = await call(
        owner_token, "list_issues", {"project_id": state["project_id"], "include_passed": True}
    )
    assert not err, data
    passed = {p["type"] for p in data["passed"]}
    assert "ERROR_EMPTY_TITLE" in passed  # fixed before the second crawl


@pytest.mark.anyio
async def test_pages_with_issue(owner_token, state):
    err, data = await call(
        owner_token, "pages_with_issue", {"project_id": state["project_id"], "issue_type": "ERROR_40x"}
    )
    assert not err, data
    assert data["total"] == 3
    assert "http://site/gone.html" in {p["url"] for p in data["pages"]}


@pytest.mark.anyio
async def test_pages_with_unknown_issue(owner_token, state):
    err, message = await call(
        owner_token, "pages_with_issue", {"project_id": state["project_id"], "issue_type": "NOPE"}
    )
    assert err and "Unknown issue type" in message


@pytest.mark.anyio
async def test_page_report(owner_token, state):
    err, data = await call(
        owner_token, "page_report", {"project_id": state["project_id"], "url": "http://site/products.html"}
    )
    assert not err, data
    assert data["page"]["title"].startswith("Products")
    assert data["images"] == [{"url": "http://site/mug.jpg", "alt": "Blue glazed ceramic mug"}]
    assert {link["url"] for link in data["internal_links"]} >= {"http://site/", "http://site/about.html"}
    assert data["counts"]["inlinks"] >= 2


@pytest.mark.anyio
async def test_page_report_finds_url_without_trailing_slash(owner_token, state):
    err, data = await call(
        owner_token, "page_report", {"project_id": state["project_id"], "url": "http://site"}
    )
    assert not err, data
    assert data["page"]["url"] == "http://site/"
    statuses = {link["url"]: link["target_status"] for link in data["internal_links"]}
    assert statuses["http://site/gone.html"] == 404


@pytest.mark.anyio
async def test_search_pages(owner_token, state):
    err, data = await call(
        owner_token,
        "search_pages",
        {"project_id": state["project_id"], "min_status": 400, "html_only": False},
    )
    assert not err, data
    assert data["total"] == 3
    err, data = await call(
        owner_token, "search_pages", {"project_id": state["project_id"], "title_contains": "care"}
    )
    assert not err, data
    assert [p["url"] for p in data["pages"]] == ["http://site/no-title.html"]


@pytest.mark.anyio
async def test_broken_links(owner_token, state):
    err, data = await call(owner_token, "broken_links", {"project_id": state["project_id"]})
    assert not err, data
    pairs = {(link["source_url"], link["target_url"]) for link in data["internal"]["links"]}
    assert ("http://site/", "http://site/gone.html") in pairs
    assert ("http://site/products.html", "http://site/gone.html") not in pairs  # removed by the fix
    resources = {(r["kind"], r["source_url"], r["target_url"]) for r in data["resources"]["items"]}
    assert resources == {
        ("image", "http://site/", "http://site/logo.png"),
        ("image", "http://site/products.html", "http://site/mug.jpg"),
    }
    assert data["external"]["checked"] is False and "note" in data["external"]
    err, data = await call(
        owner_token, "broken_links", {"project_id": state["project_id"], "include_redirects": True}
    )
    assert ("http://site/", "http://site/old-page") in {
        (link["source_url"], link["target_url"]) for link in data["internal"]["links"]
    }


@pytest.mark.anyio
async def test_list_issue_types(owner_token):
    err, data = await call(owner_token, "list_issue_types")
    assert not err, data
    assert len(data["issue_types"]) >= 79


def test_url_hash_matches_database(state):
    from seonaut_mcp.queries import url_hash

    conn = pymysql.connect(**DB, cursorclass=pymysql.cursors.DictCursor)
    with conn, conn.cursor() as cur:
        cur.execute("SELECT url, url_hash FROM pagereports WHERE crawl_id = %s", (state["second_crawl_id"],))
        rows = cur.fetchall()
    assert rows and all(url_hash(r["url"]) == r["url_hash"] for r in rows)


@pytest.mark.anyio
async def test_password_change_revokes_token(state):
    """Runs last in this module: changes the second user's password in SEOnaut's database."""
    email, password = state["other"]["email"], state["other"]["password"]
    token = get_token(email, password)
    err, data = await call(token, "list_projects")
    assert not err and [p["id"] for p in data["projects"]] == [state["other_project_id"]]

    new_hash = bcrypt.hashpw(b"changed-password", bcrypt.gensalt(4, prefix=b"2a")).decode()
    conn = pymysql.connect(**DB, autocommit=True)
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT password FROM users WHERE email = %s", (email,))
            old_hash = cur.fetchone()[0]
            cur.execute("UPDATE users SET password = %s WHERE email = %s", (new_hash, email))
        r = httpx.post(
            f"{MCP_URL}/mcp",
            json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
            headers={"Authorization": f"Bearer {token}"},
            timeout=10,
        )
        assert r.status_code == 401
    finally:
        with conn.cursor() as cur:
            cur.execute("UPDATE users SET password = %s WHERE email = %s", (old_hash, email))
        conn.close()
