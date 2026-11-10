-- A read-only database user for seonaut-mcp. Runs once, when the MySQL volume is first created.
CREATE USER IF NOT EXISTS 'seonaut_ro'@'%' IDENTIFIED BY 'seonaut_ro';
GRANT SELECT ON seonaut.* TO 'seonaut_ro'@'%';
FLUSH PRIVILEGES;
