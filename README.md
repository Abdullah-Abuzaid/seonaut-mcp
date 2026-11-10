# seonaut-mcp

An [MCP](https://modelcontextprotocol.io) server that gives AI agents **read-only** access to the crawl results of
[SEOnaut](https://github.com/StJudeWasHere/seonaut), the open-source SEO auditing tool. It runs as one more container
next to SEOnaut, and its accounts are **SEOnaut's own users**: each token sees exactly the projects its user sees in
SEOnaut.

> **Unofficial.** This project is not affiliated with or endorsed by SEOnaut. It reads SEOnaut's database and is
> tested against a specific SEOnaut database version (see [Compatibility](#compatibility)).

- **No UI needed.** Tokens are created with one command or one HTTP call, then sent as a `Bearer` header. Any MCP
  client that supports remote servers with custom headers works.
- **Read-only, twice over.** Use a database user with `SELECT` only; the server also puts every database session in
  `READ ONLY` mode.
- **Per-user isolation.** Every query is filtered by the token's SEOnaut user, as SEOnaut itself does.
- **Ready-made image.** `ghcr.io/abdullah-abuzaid/seonaut-mcp` for `linux/amd64` and `linux/arm64`.

## Tools

| Tool | What it returns |
|---|---|
| `list_projects` | The account's projects (websites), each with its latest crawl and issue counts |
| `list_crawls` | A project's crawls, newest first, with status and totals |
| `crawl_summary` | Health overview: pages, status codes, robots.txt and sitemap, links, failing issue types by priority |
| `list_issues` | Failing issue types with title, explanation, priority and pages affected (optionally the passed ones) |
| `pages_with_issue` | The pages that have one issue type |
| `page_report` | Everything about one page: meta tags, headings, canonical, robots, language, size, response time, issues, links in and out, images and alt text, hreflangs |
| `search_pages` | Pages filtered by URL, title, status code, indexability, sitemap presence or depth |
| `broken_links` | Links, images, scripts and stylesheets that point to 4xx/5xx URLs, with the page that contains each |
| `list_issue_types` | Every check SEOnaut runs, with priority, title and explanation |
| `whoami` | The token's account and the server and database versions |

All tools carry the MCP `readOnlyHint`. Issue priorities follow SEOnaut: **critical**, **alert**, **warning**.

**Only the latest crawl has page-level data.** SEOnaut deletes the pages and issues of the previous crawl when a new
one finishes and keeps only its totals, so the tools read the latest finished crawl and `list_crawls` shows the
history of totals. To check fixes, record `list_issues` before re-crawling and compare it afterwards. Crawls are
started in SEOnaut (manually or on its own schedule).

## Quick start (new SEOnaut install)

```bash
curl -O https://raw.githubusercontent.com/Abdullah-Abuzaid/seonaut-mcp/main/docker-compose.example.yml
curl -O https://raw.githubusercontent.com/Abdullah-Abuzaid/seonaut-mcp/main/.env.example
mkdir mysql-init && curl -o mysql-init/10-seonaut-mcp-user.sh \
  https://raw.githubusercontent.com/Abdullah-Abuzaid/seonaut-mcp/main/mysql-init/10-seonaut-mcp-user.sh
mv docker-compose.example.yml docker-compose.yml && cp .env.example .env   # then edit .env
docker compose up -d
```

Open SEOnaut on port 9000, sign up, add a project and run a crawl. Then create a token (next section).

## Add to an existing SEOnaut

1. Create a read-only database user (in the MySQL container, as root):

   ```sql
   CREATE USER 'seonaut_mcp'@'%' IDENTIFIED BY 'a-strong-password';
   GRANT SELECT ON seonaut.* TO 'seonaut_mcp'@'%';
   ```

2. Add the service to SEOnaut's `docker-compose.yml`, on the same network as the database:

   ```yaml
     seonaut-mcp:
       image: ghcr.io/abdullah-abuzaid/seonaut-mcp:latest   # or pin a version, e.g. :0.1.0
       restart: unless-stopped
       environment:
         SEONAUT_MCP_DB_HOST: db
         SEONAUT_MCP_DB_USER: seonaut_mcp
         SEONAUT_MCP_DB_PASSWORD: ${SEONAUT_MCP_DB_PASSWORD}
         SEONAUT_MCP_SECRET: ${SEONAUT_MCP_SECRET}      # 32+ random characters
         SEONAUT_MCP_SEONAUT_URL: https://seonaut.example.com
       depends_on: [db]
       ports: ["9100:9100"]
   ```

3. `docker compose up -d seonaut-mcp`, then check it: `docker compose exec seonaut-mcp seonaut-mcp check`.

**Put it behind HTTPS** (the same reverse proxy as SEOnaut is fine): tokens travel in a request header.

## Tokens

A token belongs to one SEOnaut user. Two ways to get one, neither needs a browser:

```bash
# On the server (no password needed: you have access to the container)
docker compose exec seonaut-mcp seonaut-mcp token create --email seo@example.com --days 365

# From anywhere, with the SEOnaut email and password
curl -X POST https://mcp.example.com/auth/token \
  -H 'Content-Type: application/json' \
  -d '{"email": "seo@example.com", "password": "...", "days": 365}'
```

`--days 0` (the default) makes a token that does not expire. A token stops working when:

- the user is deleted in SEOnaut, or changes their password;
- its id is listed in `SEONAUT_MCP_REVOKED_TOKENS` (comma-separated; the id is printed when the token is created);
- `SEONAUT_MCP_SECRET` changes (revokes every token).

Failed logins on `/auth/token` are limited per client address (5 per 15 minutes by default). Set
`SEONAUT_MCP_PASSWORD_LOGIN=false` to allow only tokens made on the server.

**One account for a whole team.** Create a SEOnaut user for the team, create its projects under it, and share one
token through your MCP client's configuration. Everyone then sees the same projects. Tools are read-only, so a shared
token cannot change anything; rotate it by revoking its id and issuing a new one.

## Connect an MCP client

The endpoint is `https://<host>/mcp` (Streamable HTTP).

**Claude Code**

```bash
claude mcp add --transport http seonaut https://mcp.example.com/mcp \
  --header "Authorization: Bearer <token>"
```

**JSON configuration** (Claude Code `.mcp.json`, and clients that use the same format)

```json
{
  "mcpServers": {
    "seonaut": {
      "type": "http",
      "url": "https://mcp.example.com/mcp",
      "headers": { "Authorization": "Bearer ${SEONAUT_MCP_TOKEN}" }
    }
  }
}
```

**Clients that only start local (stdio) servers** can use a stdio-to-HTTP bridge such as
[`mcp-remote`](https://www.npmjs.com/package/mcp-remote):

```json
{
  "mcpServers": {
    "seonaut": {
      "command": "npx",
      "args": ["-y", "mcp-remote", "https://mcp.example.com/mcp", "--header", "Authorization: Bearer ${SEONAUT_MCP_TOKEN}"]
    }
  }
}
```

## Configuration

| Variable | Default | Meaning |
|---|---|---|
| `SEONAUT_MCP_SECRET` | *(required)* | Signs tokens. 32+ characters |
| `SEONAUT_MCP_DB_HOST` | `db` | MySQL host (`SEONAUT_DATABASE_SERVER` also works) |
| `SEONAUT_MCP_DB_PORT` | `3306` | MySQL port |
| `SEONAUT_MCP_DB_USER` | `seonaut` | Database user; use a `SELECT`-only user |
| `SEONAUT_MCP_DB_PASSWORD` | | Its password |
| `SEONAUT_MCP_DB_NAME` | `seonaut` | Database name |
| `SEONAUT_MCP_PORT` | `9100` | HTTP port |
| `SEONAUT_MCP_SEONAUT_URL` | | Public SEOnaut address, for links to its dashboard and issue pages |
| `SEONAUT_MCP_TOKEN_DAYS` | `0` | Maximum token lifetime for `/auth/token` (0 = no limit) |
| `SEONAUT_MCP_PASSWORD_LOGIN` | `true` | Enable `POST /auth/token` |
| `SEONAUT_MCP_REVOKED_TOKENS` | | Comma-separated token ids to reject |
| `SEONAUT_MCP_LOGIN_ATTEMPTS` | `5` | Failed logins allowed per address and window |
| `SEONAUT_MCP_LOGIN_WINDOW_SECONDS` | `900` | Rate-limit window |
| `SEONAUT_MCP_TRUSTED_PROXIES` | `127.0.0.1` | Proxy addresses whose `X-Forwarded-For` is trusted (for rate limiting) |
| `SEONAUT_MCP_ALLOWED_HOSTS` | | Comma-separated `Host` values to accept (DNS-rebinding protection); empty = any |
| `SEONAUT_MCP_MAX_ROWS` | `500` | Upper limit for `limit` arguments |
| `SEONAUT_MCP_USER_CACHE_SECONDS` | `30` | How long a verified token is cached |
| `SEONAUT_MCP_LOG_LEVEL` | `info` | Log level |

Other endpoints: `GET /healthz` (no auth) reports the server version and SEOnaut's database version.

## Compatibility

The server reads SEOnaut's tables directly, so a SEOnaut upgrade that changes them can break a tool. Each release
states the SEOnaut database version it was tested with (`schema_migrations.version`, shown by `/healthz`,
`whoami` and `seonaut-mcp check`). A weekly CI job tests the latest SEOnaut image so changes are caught early.

| seonaut-mcp | SEOnaut database version |
|---|---|
| 0.1.x | 75 |

## Development

```bash
python -m venv .venv && .venv/bin/pip install -e ".[dev]"
pytest                                             # unit tests

# End-to-end: SEOnaut + MySQL + a test website with known SEO problems + this server
docker compose -f e2e/docker-compose.yml up -d --build
python e2e/seed.py                                 # users, a project, two crawls
pytest -m integration
docker compose -f e2e/docker-compose.yml down -v
```

`scripts/update_issue_catalog.py` refreshes the issue titles and descriptions from SEOnaut's translation file.

Releases: push a tag `vX.Y.Z`; GitHub Actions tests it and publishes the image as `X.Y.Z`, `X.Y` and `latest`.
Every push to `main` publishes `main`.

## License

MIT. Issue titles and descriptions come from SEOnaut's translations (MIT, Copyright (c) 2022 SEOnaut); see
[LICENSE](LICENSE).
