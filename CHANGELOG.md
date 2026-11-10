# Changelog

## 0.1.0

First release.

- Streamable HTTP MCP server with read-only tools: `list_projects`, `list_crawls`, `crawl_summary`, `list_issues`,
  `pages_with_issue`, `page_report`, `search_pages`, `broken_links`, `list_issue_types`, `whoami`.
- Bearer tokens tied to SEOnaut users: `seonaut-mcp token create` on the server or `POST /auth/token` with the
  SEOnaut email and password. Tokens stop working when the user is deleted or changes password, or when revoked.
- Database sessions in `READ ONLY` mode; queries limited to the token user's projects.
- Docker image for `linux/amd64` and `linux/arm64`; tested against SEOnaut database version 75.
