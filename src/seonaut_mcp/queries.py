"""Read-only queries on SEOnaut's database. Every query is limited to the projects of one user,
mirroring SEOnaut's own ownership check (projects.user_id)."""

from __future__ import annotations

import hashlib
import json
from functools import lru_cache
from importlib import resources
from typing import Any

from .auth import User
from .db import Database

PRIORITY_NAMES = {1: "critical", 2: "alert", 3: "warning"}
PRIORITY_IDS = {name: pid for pid, name in PRIORITY_NAMES.items()}

CRAWL_COLUMNS = """
    c.id, c.project_id, c.start, c.end, c.issues_end, c.total_urls, c.total_issues,
    c.critical_issues, c.alert_issues, c.warning_issues, c.blocked_by_robotstxt, c.noindex
"""

PAGE_LIST_COLUMNS = "pr.id, pr.url, pr.title, pr.status_code, pr.depth, pr.words, pr.media_type"

PAGE_SORTS = {
    "url": "pr.url ASC",
    "depth": "pr.depth DESC, pr.url ASC",
    "words": "pr.words ASC, pr.url ASC",
    "size": "pr.size DESC, pr.url ASC",
    "ttfb": "pr.ttfb DESC, pr.url ASC",
}


class NotFound(LookupError):
    """The requested project, crawl, page or issue type does not exist for this user."""


@lru_cache(maxsize=1)
def issue_catalog() -> dict[str, dict[str, str]]:
    """Titles and descriptions of SEOnaut issue types (from SEOnaut's English translations)."""
    text = resources.files("seonaut_mcp").joinpath("issue_catalog.json").read_text("utf-8")
    return json.loads(text)


def url_hash(url: str) -> str:
    """SEOnaut stores sha256(url) as url_hash."""
    return hashlib.sha256(url.encode()).hexdigest()


def crawl_status(crawl: dict[str, Any]) -> str:
    if not crawl.get("end"):
        return "crawling"
    if not crawl.get("issues_end"):
        return "analysing"
    return "finished"


def _describe(issue_type: str) -> dict[str, str]:
    entry = issue_catalog().get(issue_type, {})
    return {"title": entry.get("title", issue_type), "description": entry.get("description", "")}


class Queries:
    def __init__(self, db: Database, max_rows: int = 500, seonaut_url: str = "") -> None:
        self.db = db
        self.max_rows = max_rows
        self.seonaut_url = seonaut_url

    # -- helpers -------------------------------------------------------------------------------

    def _limit(self, limit: int) -> int:
        return max(1, min(int(limit), self.max_rows))

    def _link(self, path: str) -> str | None:
        return f"{self.seonaut_url}{path}" if self.seonaut_url else None

    def project(self, user: User, project_id: int) -> dict[str, Any]:
        row = self.db.one(
            """SELECT id, url, created, ignore_robotstxt, follow_nofollow, include_noindex, crawl_sitemap,
                      allow_subdomains, basic_auth, check_external_links, archive, user_agent
               FROM projects WHERE id = %s AND user_id = %s AND deleting = 0""",
            (project_id, user.id),
        )
        if not row:
            raise NotFound(f"Project {project_id} not found for this account. Use list_projects.")
        return row

    def crawl(self, user: User, project_id: int) -> dict[str, Any]:
        """The project's latest finished crawl. SEOnaut deletes the pages and issues of the previous
        crawl when a new one finishes, so only this crawl has page-level data."""
        self.project(user, project_id)
        row = self.db.one(
            f"""SELECT {CRAWL_COLUMNS} FROM crawls c
                WHERE c.project_id = %s AND c.issues_end IS NOT NULL
                ORDER BY c.start DESC, c.id DESC LIMIT 1""",
            (project_id,),
        )
        if not row:
            raise NotFound(
                f"Project {project_id} has no finished crawl yet. Start a crawl in SEOnaut and wait until it "
                "finishes (list_crawls shows its status)."
            )
        row["status"] = crawl_status(row)
        return row

    def issue_type_id(self, issue_type: str) -> int:
        value = self.db.scalar("SELECT id FROM issue_types WHERE type = %s", (issue_type,))
        if value is None:
            raise NotFound(f"Unknown issue type {issue_type!r}. Use list_issue_types.")
        return int(value)

    def _issue_groups(self, crawl_id: int, priority: int | None = None) -> list[dict[str, Any]]:
        where, params = "i.crawl_id = %s", [crawl_id]
        if priority is not None:
            where += " AND it.priority = %s"
            params.append(priority)
        rows = self.db.all(
            f"""SELECT it.type, it.priority, COUNT(DISTINCT i.pagereport_id) AS pages
                FROM issues i JOIN issue_types it ON it.id = i.issue_type_id
                WHERE {where}
                GROUP BY it.id, it.type, it.priority
                ORDER BY it.priority ASC, pages DESC, it.type ASC""",
            params,
        )
        return [
            {
                "type": r["type"],
                "priority": PRIORITY_NAMES.get(r["priority"], str(r["priority"])),
                "pages": r["pages"],
                **_describe(r["type"]),
            }
            for r in rows
        ]

    def _page(self, crawl_id: int, page_id: int | None, url: str | None) -> dict[str, Any]:
        columns = """id, url, scheme, redirect_url, refresh, status_code, content_type, media_type, lang,
                     title, description, robots, canonical, h1, h2, words, size, noindex,
                     robotstxt_blocked, crawled, in_sitemap, depth, ttfb, url_hash"""
        if page_id is not None:
            row = self.db.one(
                f"SELECT {columns} FROM pagereports WHERE id = %s AND crawl_id = %s", (page_id, crawl_id)
            )
        elif url:
            candidates = [url]
            candidates.append(url[:-1] if url.endswith("/") else url + "/")
            row = None
            for candidate in candidates:
                row = self.db.one(
                    f"SELECT {columns} FROM pagereports WHERE url_hash = %s AND crawl_id = %s LIMIT 1",
                    (url_hash(candidate), crawl_id),
                )
                if row:
                    break
        else:
            raise ValueError("Give either page_id or url")
        if not row:
            raise NotFound("Page not found in this crawl. Use search_pages to find it.")
        return row

    # -- tools ---------------------------------------------------------------------------------

    def list_projects(self, user: User) -> dict[str, Any]:
        projects = self.db.all(
            """SELECT id, url, created, crawl_sitemap, check_external_links, ignore_robotstxt
               FROM projects WHERE user_id = %s AND deleting = 0 ORDER BY url ASC""",
            (user.id,),
        )
        if projects:
            ids = [p["id"] for p in projects]
            marks = ",".join(["%s"] * len(ids))
            latest = self.db.all(
                f"""SELECT {CRAWL_COLUMNS} FROM crawls c
                    WHERE c.id IN (SELECT MAX(id) FROM crawls WHERE project_id IN ({marks})
                                   GROUP BY project_id)""",
                ids,
            )
            by_project = {c["project_id"]: c for c in latest}
            for p in projects:
                crawl = by_project.get(p["id"])
                if crawl:
                    crawl["status"] = crawl_status(crawl)
                    crawl.pop("project_id", None)
                p["latest_crawl"] = crawl
                p["dashboard_url"] = self._link(f"/dashboard?pid={p['id']}")
        return {"account": user.email, "projects": projects}

    def list_crawls(self, user: User, project_id: int, limit: int = 10) -> dict[str, Any]:
        self.project(user, project_id)
        crawls = self.db.all(
            f"SELECT {CRAWL_COLUMNS} FROM crawls c WHERE c.project_id = %s ORDER BY c.start DESC, c.id DESC "
            "LIMIT %s",
            (project_id, self._limit(limit)),
        )
        for c in crawls:
            c["status"] = crawl_status(c)
            c.pop("project_id", None)
        return {"project_id": project_id, "crawls": crawls}

    def crawl_summary(self, user: User, project_id: int) -> dict[str, Any]:
        project = self.project(user, project_id)
        crawl = self.crawl(user, project_id)
        cid = crawl["id"]
        extra = (
            self.db.one(
                """SELECT robotstxt_exists, sitemap_exists, sitemap_blocked, links_internal_follow,
                      links_internal_nofollow, links_external_follow, links_external_nofollow,
                      links_sponsored, links_ugc
               FROM crawls WHERE id = %s""",
                (cid,),
            )
            or {}
        )
        status_codes = self.db.all(
            """SELECT status_code, COUNT(*) AS pages FROM pagereports
               WHERE crawl_id = %s AND crawled = 1 GROUP BY status_code ORDER BY status_code""",
            (cid,),
        )
        html = (
            self.db.one(
                """SELECT COUNT(*) AS pages, ROUND(AVG(ttfb)) AS avg_ttfb_ms, ROUND(AVG(words)) AS avg_words,
                      SUM(noindex = 1) AS noindex, SUM(in_sitemap = 1) AS in_sitemap,
                      MAX(depth) AS max_depth
               FROM pagereports WHERE crawl_id = %s AND crawled = 1 AND media_type = 'text/html'""",
                (cid,),
            )
            or {}
        )
        issues = self._issue_groups(cid)
        total_types = int(self.db.scalar("SELECT COUNT(*) FROM issue_types") or 0)
        return {
            "project": {"id": project["id"], "url": project["url"]},
            "crawl": crawl,
            "site": {
                "robots_txt_exists": bool(extra.get("robotstxt_exists")),
                "sitemap_exists": bool(extra.get("sitemap_exists")),
                "sitemap_blocked_by_robots_txt": bool(extra.get("sitemap_blocked")),
            },
            "html_pages": html,
            "status_codes": status_codes,
            "links": {k.removeprefix("links_"): v for k, v in extra.items() if k.startswith("links_")},
            "issues_by_priority": {
                name: [i for i in issues if i["priority"] == name] for name in PRIORITY_NAMES.values()
            },
            "issue_types_failed": len(issues),
            "issue_types_passed": max(total_types - len(issues), 0),
            "dashboard_url": self._link(f"/dashboard?pid={project_id}"),
        }

    def list_issues(
        self,
        user: User,
        project_id: int,
        priority: str | None = None,
        include_passed: bool = False,
    ) -> dict[str, Any]:
        crawl = self.crawl(user, project_id)
        pid = None
        if priority:
            if priority not in PRIORITY_IDS:
                raise ValueError(f"priority must be one of {sorted(PRIORITY_IDS)}")
            pid = PRIORITY_IDS[priority]
        issues = self._issue_groups(crawl["id"], pid)
        for issue in issues:
            issue["issues_url"] = self._link(f"/issues/view?pid={project_id}&eid={issue['type']}")
        result: dict[str, Any] = {"project_id": project_id, "crawl_id": crawl["id"], "issues": issues}
        if include_passed:
            failed = {i["type"] for i in issues}
            where, params = "", []
            if pid is not None:
                where, params = "WHERE priority = %s", [pid]
            all_types = self.db.all(f"SELECT type, priority FROM issue_types {where} ORDER BY type", params)
            result["passed"] = [
                {
                    "type": t["type"],
                    "priority": PRIORITY_NAMES.get(t["priority"]),
                    "title": _describe(t["type"])["title"],
                }
                for t in all_types
                if t["type"] not in failed
            ]
        return result

    def pages_with_issue(
        self,
        user: User,
        project_id: int,
        issue_type: str,
        limit: int = 50,
        offset: int = 0,
    ) -> dict[str, Any]:
        crawl = self.crawl(user, project_id)
        type_id = self.issue_type_id(issue_type)
        total = self.db.scalar(
            "SELECT COUNT(DISTINCT pagereport_id) FROM issues WHERE crawl_id = %s AND issue_type_id = %s",
            (crawl["id"], type_id),
        )
        pages = self.db.all(
            f"""SELECT {PAGE_LIST_COLUMNS} FROM pagereports pr
                WHERE pr.id IN (SELECT DISTINCT pagereport_id FROM issues
                                WHERE crawl_id = %s AND issue_type_id = %s)
                ORDER BY pr.url ASC LIMIT %s OFFSET %s""",
            (crawl["id"], type_id, self._limit(limit), max(int(offset), 0)),
        )
        return {
            "project_id": project_id,
            "crawl_id": crawl["id"],
            "issue": {"type": issue_type, **_describe(issue_type)},
            "total": int(total or 0),
            "offset": max(int(offset), 0),
            "pages": pages,
        }

    def page_report(
        self,
        user: User,
        project_id: int,
        url: str | None = None,
        page_id: int | None = None,
        link_limit: int = 100,
    ) -> dict[str, Any]:
        crawl = self.crawl(user, project_id)
        cid = crawl["id"]
        page = self._page(cid, page_id, url)
        pid, phash = page["id"], page.pop("url_hash")
        n = self._limit(link_limit)
        issues = self.db.all(
            """SELECT it.type, it.priority FROM issues i JOIN issue_types it ON it.id = i.issue_type_id
               WHERE i.pagereport_id = %s AND i.crawl_id = %s GROUP BY it.id, it.type, it.priority
               ORDER BY it.priority, it.type""",
            (pid, cid),
        )
        inlinks_total = self.db.scalar(
            "SELECT COUNT(*) FROM links WHERE crawl_id = %s AND url_hash = %s", (cid, phash)
        )
        inlinks = self.db.all(
            """SELECT src.url AS source_url, l.text, l.nofollow FROM links l
               JOIN pagereports src ON src.id = l.pagereport_id
               WHERE l.crawl_id = %s AND l.url_hash = %s ORDER BY src.url LIMIT %s""",
            (cid, phash, n),
        )
        outlinks = self.db.all(
            """SELECT l.url, l.text, l.nofollow, l.rel, t.status_code AS target_status
               FROM links l LEFT JOIN pagereports t ON t.crawl_id = l.crawl_id AND t.url_hash = l.url_hash
               WHERE l.pagereport_id = %s ORDER BY l.id LIMIT %s""",
            (pid, n),
        )
        external = self.db.all(
            """SELECT url, text, nofollow, sponsored, ugc, status_code FROM external_links
               WHERE pagereport_id = %s ORDER BY id LIMIT %s""",
            (pid, n),
        )
        images = self.db.all(
            "SELECT url, alt FROM images WHERE pagereport_id = %s ORDER BY id LIMIT %s", (pid, n)
        )
        hreflangs = self.db.all(
            "SELECT from_lang, to_url, to_lang FROM hreflangs WHERE pagereport_id = %s ORDER BY id", (pid,)
        )
        counts = self.db.one(
            """SELECT (SELECT COUNT(*) FROM scripts WHERE pagereport_id = %s) AS scripts,
                      (SELECT COUNT(*) FROM styles WHERE pagereport_id = %s) AS styles,
                      (SELECT COUNT(*) FROM links WHERE pagereport_id = %s) AS internal_links,
                      (SELECT COUNT(*) FROM external_links WHERE pagereport_id = %s) AS external_links,
                      (SELECT COUNT(*) FROM images WHERE pagereport_id = %s) AS images""",
            (pid, pid, pid, pid, pid),
        )
        return {
            "project_id": project_id,
            "crawl_id": cid,
            "page": page,
            "issues": [
                {"type": i["type"], "priority": PRIORITY_NAMES.get(i["priority"]), **_describe(i["type"])}
                for i in issues
            ],
            "counts": {**(counts or {}), "inlinks": int(inlinks_total or 0)},
            "inlinks": inlinks,
            "internal_links": outlinks,
            "external_links": external,
            "images": images,
            "hreflangs": hreflangs,
        }

    def search_pages(
        self,
        user: User,
        project_id: int,
        url_contains: str | None = None,
        title_contains: str | None = None,
        status_code: int | None = None,
        min_status: int | None = None,
        max_status: int | None = None,
        noindex: bool | None = None,
        in_sitemap: bool | None = None,
        html_only: bool = True,
        min_depth: int | None = None,
        sort: str = "url",
        limit: int = 50,
        offset: int = 0,
    ) -> dict[str, Any]:
        crawl = self.crawl(user, project_id)
        if sort not in PAGE_SORTS:
            raise ValueError(f"sort must be one of {sorted(PAGE_SORTS)}")
        where, params = ["pr.crawl_id = %s", "pr.crawled = 1"], [crawl["id"]]

        def like(text: str) -> str:
            return "%" + text.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"

        if url_contains:
            where.append("pr.url LIKE %s")
            params.append(like(url_contains))
        if title_contains:
            where.append("pr.title LIKE %s")
            params.append(like(title_contains))
        if status_code is not None:
            where.append("pr.status_code = %s")
            params.append(status_code)
        if min_status is not None:
            where.append("pr.status_code >= %s")
            params.append(min_status)
        if max_status is not None:
            where.append("pr.status_code <= %s")
            params.append(max_status)
        if noindex is not None:
            where.append("pr.noindex = %s")
            params.append(1 if noindex else 0)
        if in_sitemap is not None:
            where.append("pr.in_sitemap = %s")
            params.append(1 if in_sitemap else 0)
        if html_only:
            where.append("pr.media_type = 'text/html'")
        if min_depth is not None:
            where.append("pr.depth >= %s")
            params.append(min_depth)
        clause = " AND ".join(where)
        total = self.db.scalar(f"SELECT COUNT(*) FROM pagereports pr WHERE {clause}", params)
        pages = self.db.all(
            f"""SELECT {PAGE_LIST_COLUMNS}, pr.noindex, pr.in_sitemap, pr.ttfb, pr.size
                FROM pagereports pr WHERE {clause}
                ORDER BY {PAGE_SORTS[sort]} LIMIT %s OFFSET %s""",
            [*params, self._limit(limit), max(int(offset), 0)],
        )
        return {
            "project_id": project_id,
            "crawl_id": crawl["id"],
            "total": int(total or 0),
            "offset": max(int(offset), 0),
            "pages": pages,
        }

    def broken_links(
        self,
        user: User,
        project_id: int,
        include_external: bool = True,
        include_redirects: bool = False,
        include_resources: bool = True,
        limit: int = 100,
        offset: int = 0,
    ) -> dict[str, Any]:
        project = self.project(user, project_id)
        crawl = self.crawl(user, project_id)
        cid = crawl["id"]
        n, off = self._limit(limit), max(int(offset), 0)
        condition = "t.status_code >= 300" if include_redirects else "t.status_code >= 400"
        internal_total = self.db.scalar(
            f"""SELECT COUNT(*) FROM links l
                JOIN pagereports t ON t.crawl_id = l.crawl_id AND t.url_hash = l.url_hash
                WHERE l.crawl_id = %s AND {condition}""",
            (cid,),
        )
        internal = self.db.all(
            f"""SELECT src.url AS source_url, l.url AS target_url, t.status_code, t.redirect_url, l.text
                FROM links l
                JOIN pagereports t ON t.crawl_id = l.crawl_id AND t.url_hash = l.url_hash
                JOIN pagereports src ON src.id = l.pagereport_id
                WHERE l.crawl_id = %s AND {condition}
                ORDER BY t.status_code DESC, l.url, src.url LIMIT %s OFFSET %s""",
            (cid, n, off),
        )
        result: dict[str, Any] = {
            "project_id": project_id,
            "crawl_id": cid,
            "internal": {"total": int(internal_total or 0), "offset": off, "links": internal},
        }
        if include_resources:
            # Images, scripts and stylesheets that load a URL which returned an error. These tables have
            # no url_hash column, so the hash is computed the way SEOnaut does: sha256 of the URL.
            parts = [
                f"""SELECT '{kind}' AS kind, src.url AS source_url, r.url AS target_url, t.status_code
                    FROM {table} r
                    JOIN pagereports t ON t.crawl_id = r.crawl_id AND t.url_hash = SHA2(r.url, 256)
                    JOIN pagereports src ON src.id = r.pagereport_id
                    WHERE r.crawl_id = %s AND {condition}"""
                for kind, table in (("image", "images"), ("script", "scripts"), ("stylesheet", "styles"))
            ]
            union = " UNION ALL ".join(parts)
            resources_total = self.db.scalar(f"SELECT COUNT(*) FROM ({union}) x", (cid, cid, cid))
            resources = self.db.all(
                f"{union} ORDER BY status_code DESC, target_url, source_url LIMIT %s OFFSET %s",
                (cid, cid, cid, n, off),
            )
            result["resources"] = {"total": int(resources_total or 0), "offset": off, "items": resources}
        if include_external:
            ext_condition = "e.status_code >= 300" if include_redirects else "e.status_code >= 400"
            ext_total = self.db.scalar(
                f"SELECT COUNT(*) FROM external_links e WHERE e.crawl_id = %s AND {ext_condition}", (cid,)
            )
            external = self.db.all(
                f"""SELECT src.url AS source_url, e.url AS target_url, e.status_code, e.text
                    FROM external_links e JOIN pagereports src ON src.id = e.pagereport_id
                    WHERE e.crawl_id = %s AND {ext_condition}
                    ORDER BY e.status_code DESC, e.url, src.url LIMIT %s OFFSET %s""",
                (cid, n, off),
            )
            result["external"] = {
                "checked": bool(project["check_external_links"]),
                "total": int(ext_total or 0),
                "offset": off,
                "links": external,
            }
            if not project["check_external_links"]:
                result["external"]["note"] = (
                    "External links are not checked for this project. Turn on 'check external links' in "
                    "the SEOnaut project settings to see their status."
                )
        return result

    def list_issue_types(self) -> dict[str, Any]:
        rows = self.db.all("SELECT type, priority FROM issue_types ORDER BY priority, type")
        return {
            "issue_types": [
                {"type": r["type"], "priority": PRIORITY_NAMES.get(r["priority"]), **_describe(r["type"])}
                for r in rows
            ]
        }
