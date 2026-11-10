# Security

Please report vulnerabilities privately through GitHub's **Report a vulnerability** button on the Security tab of
this repository, not in a public issue.

## Deployment checklist

- Serve the MCP endpoint over HTTPS only; tokens are sent in the `Authorization` header.
- Use a database user with `SELECT` rights only (see README).
- Keep `SEONAUT_MCP_SECRET` out of version control. Changing it revokes every token.
- Prefer tokens with an expiry (`--days`) and revoke unused ones with `SEONAUT_MCP_REVOKED_TOKENS`.
- Set `SEONAUT_MCP_PASSWORD_LOGIN=false` if tokens are only ever made on the server.
- Set `SEONAUT_MCP_TRUSTED_PROXIES` to your reverse proxy's address so login rate limiting sees real client addresses.
