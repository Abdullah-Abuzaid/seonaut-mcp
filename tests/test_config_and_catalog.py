import pytest

from seonaut_mcp.config import ConfigError, Settings
from seonaut_mcp.queries import crawl_status, issue_catalog, url_hash


def test_secret_is_required(monkeypatch):
    monkeypatch.setenv("SEONAUT_MCP_SECRET", "short")
    with pytest.raises(ConfigError, match="SEONAUT_MCP_SECRET"):
        Settings.from_env()


def test_settings_from_env(monkeypatch):
    monkeypatch.setenv("SEONAUT_MCP_SECRET", "x" * 40)
    monkeypatch.setenv("SEONAUT_DATABASE_SERVER", "mysql.internal")  # SEOnaut's own variable name
    monkeypatch.setenv("SEONAUT_MCP_DB_USER", "seonaut_ro")
    monkeypatch.setenv("SEONAUT_MCP_PASSWORD_LOGIN", "false")
    monkeypatch.setenv("SEONAUT_MCP_REVOKED_TOKENS", "a, b ,")
    monkeypatch.setenv("SEONAUT_MCP_ALLOWED_HOSTS", "seo.example.com")
    settings = Settings.from_env()
    assert settings.database.host == "mysql.internal"
    assert settings.database.user == "seonaut_ro"
    assert settings.password_login is False
    assert settings.revoked_tokens == frozenset({"a", "b"})
    assert settings.allowed_hosts == ["seo.example.com"]


def test_bad_boolean(monkeypatch):
    monkeypatch.setenv("SEONAUT_MCP_SECRET", "x" * 40)
    monkeypatch.setenv("SEONAUT_MCP_PASSWORD_LOGIN", "maybe")
    with pytest.raises(ConfigError):
        Settings.from_env()


def test_issue_catalog_has_seonaut_issue_types():
    catalog = issue_catalog()
    assert len(catalog) >= 79
    assert catalog["ERROR_EMPTY_TITLE"]["title"] == "Pages with missing title"
    assert all(entry["title"] and entry["description"] for entry in catalog.values())


def test_url_hash_is_hex_sha256():
    # SEOnaut stores the hex sha256 of the URL; the integration tests check it against real rows.
    assert url_hash("abc") == "ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad"


def test_crawl_status():
    assert crawl_status({"end": None, "issues_end": None}) == "crawling"
    assert crawl_status({"end": "x", "issues_end": None}) == "analysing"
    assert crawl_status({"end": "x", "issues_end": "y"}) == "finished"
