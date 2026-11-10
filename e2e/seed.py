"""Seed the e2e stack: SEOnaut users, a project, and two crawls of the test website.

Crawl 1 runs on e2e/site (with known problems). Crawl 2 runs after e2e/fixes is copied over it, so
compare_crawls has something to show. Writes e2e/.state.json for the integration tests.

Usage (after `docker compose -f e2e/docker-compose.yml up -d --build`):
    python e2e/seed.py
"""

from __future__ import annotations

import json
import os
import shutil
import time
from pathlib import Path

import httpx
import pymysql

HERE = Path(__file__).resolve().parent
SEONAUT = os.environ.get("E2E_SEONAUT_URL", "http://127.0.0.1:9001")
DB = {
    "host": "127.0.0.1",
    "port": int(os.environ.get("E2E_DB_PORT", "33307")),
    "user": "root",
    "password": "root",
    "database": "seonaut",
}
MAIN = ("owner@example.com", "owner-password-123")
OTHER = ("other@example.com", "other-password-456")
SITE_URL = "http://site/"


def db_one(sql: str, params: tuple = ()) -> dict | None:
    conn = pymysql.connect(**DB, cursorclass=pymysql.cursors.DictCursor, autocommit=True)
    try:
        with conn.cursor() as cur:
            cur.execute(sql, params)
            return cur.fetchone()
    finally:
        conn.close()


def wait_for_seonaut(timeout: int = 300) -> None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            if httpx.get(f"{SEONAUT}/signin", timeout=5).status_code == 200:
                if db_one("SELECT version, dirty FROM schema_migrations WHERE dirty = 0"):
                    return
        except (httpx.HTTPError, pymysql.Error):
            pass
        time.sleep(3)
    raise SystemExit("SEOnaut did not start in time")


def session_for(email: str, password: str) -> httpx.Client:
    client = httpx.Client(base_url=SEONAUT, follow_redirects=False, timeout=30)
    if not db_one("SELECT id FROM users WHERE email = %s", (email,)):
        client.post("/signup", data={"email": email, "password": password})
    client.post("/signin", data={"email": email, "password": password})
    if client.get("/").status_code != 200:
        raise SystemExit(f"Could not sign in to SEOnaut as {email}")
    return client


def ensure_project(client: httpx.Client, email: str, url: str) -> int:
    user = db_one("SELECT id FROM users WHERE email = %s", (email,))
    row = db_one("SELECT id FROM projects WHERE user_id = %s AND url = %s", (user["id"], url))
    if not row:
        client.post(
            "/project/add",
            data={
                "url": url,
                "ignore_robotstxt": "false",
                "follow_nofollow": "false",
                "include_noindex": "false",
                "crawl_sitemap": "true",
                "allow_subdomains": "false",
                "check_external_links": "false",
                "basic_auth": "false",
                "archive": "false",
                "custom_user_agent": "false",
            },
        )
        row = db_one("SELECT id FROM projects WHERE user_id = %s AND url = %s", (user["id"], url))
    if not row:
        raise SystemExit(f"Could not create project {url}")
    return int(row["id"])


def crawl(client: httpx.Client, project_id: int, timeout: int = 300) -> int:
    before = db_one("SELECT COALESCE(MAX(id), 0) AS id FROM crawls WHERE project_id = %s", (project_id,))[
        "id"
    ]
    client.get("/crawl/start", params={"pid": project_id})
    deadline = time.time() + timeout
    while time.time() < deadline:
        row = db_one(
            "SELECT id, issues_end FROM crawls WHERE project_id = %s AND id > %s ORDER BY id DESC LIMIT 1",
            (project_id, before),
        )
        if row and row["issues_end"]:
            return int(row["id"])
        time.sleep(2)
    raise SystemExit("Crawl did not finish in time")


def main() -> None:
    # e2e/.site is bind-mounted into the web server: change its files, never replace the folder itself.
    live = HERE / ".site"
    live.mkdir(exist_ok=True)
    for old in live.iterdir():
        old.unlink()
    for path in (HERE / "site").iterdir():
        shutil.copy(path, live / path.name)

    wait_for_seonaut()
    owner = session_for(*MAIN)
    project_id = ensure_project(owner, MAIN[0], SITE_URL)
    first = crawl(owner, project_id)
    print(f"Crawl 1 finished: {first}")

    for path in (HERE / "fixes").iterdir():
        shutil.copy(path, live / path.name)
    time.sleep(1)  # crawls are ordered by start time (second precision)
    second = crawl(owner, project_id)
    print(f"Crawl 2 finished: {second}")

    other = session_for(*OTHER)
    other_project = ensure_project(other, OTHER[0], "http://site/about.html")

    state = {
        "owner": {"email": MAIN[0], "password": MAIN[1]},
        "other": {"email": OTHER[0], "password": OTHER[1]},
        "project_id": project_id,
        "other_project_id": other_project,
        "first_crawl_id": first,
        "second_crawl_id": second,
    }
    (HERE / ".state.json").write_text(json.dumps(state, indent=2))
    print(json.dumps(state, indent=2))


if __name__ == "__main__":
    main()
