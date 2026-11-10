#!/bin/sh
# Runs once, when the MySQL data volume is first created (docker-entrypoint-initdb.d).
# Creates the read-only user seonaut-mcp connects with. For an existing database, run the
# SQL below by hand (see README, "Add to an existing SEOnaut").
set -eu
: "${SEONAUT_MCP_DB_PASSWORD:?Set SEONAUT_MCP_DB_PASSWORD for the db service}"
mysql -uroot -p"${MYSQL_ROOT_PASSWORD}" <<SQL
CREATE USER IF NOT EXISTS 'seonaut_mcp'@'%' IDENTIFIED BY '${SEONAUT_MCP_DB_PASSWORD}';
GRANT SELECT ON \`${MYSQL_DATABASE:-seonaut}\`.* TO 'seonaut_mcp'@'%';
FLUSH PRIVILEGES;
SQL
