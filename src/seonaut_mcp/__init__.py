"""Unofficial MCP server for SEOnaut crawl results."""

from importlib.metadata import PackageNotFoundError, version

try:
    __version__ = version("seonaut-mcp")
except PackageNotFoundError:  # running from a source checkout
    __version__ = "0.0.0"
