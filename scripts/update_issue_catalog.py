"""Regenerate src/seonaut_mcp/issue_catalog.json from SEOnaut's English translation file.

Usage:
    python scripts/update_issue_catalog.py path/to/seonaut/translations/translation.en.yaml

The issue type names come from SEOnaut's issue_types table; their titles and descriptions come
from SEOnaut's translations (MIT licence, Copyright (c) 2022 SEOnaut).
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

OUT = Path(__file__).resolve().parents[1] / "src" / "seonaut_mcp" / "issue_catalog.json"
LINE = re.compile(r"^([A-Za-z0-9_]+):\s*(.*)$")


def unquote(value: str) -> str:
    value = value.strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
        value = value[1:-1]
        if value.startswith("'"):
            value = value.replace("''", "'")
    return value.replace('\\"', '"')


def main(path: str) -> None:
    entries: dict[str, str] = {}
    for line in Path(path).read_text(encoding="utf-8").splitlines():
        match = LINE.match(line)
        if match:
            entries[match.group(1)] = unquote(match.group(2))

    catalog: dict[str, dict[str, str]] = {}
    for key, title in entries.items():
        if key.endswith("_DESC"):
            continue
        description = entries.get(f"{key}_DESC")
        if description and (key.startswith("ERROR_") or key in KNOWN_NON_ERROR_TYPES):
            catalog[key] = {"title": title, "description": description}

    OUT.write_text(json.dumps(dict(sorted(catalog.items())), indent=2, ensure_ascii=False) + "\n", "utf-8")
    print(f"Wrote {len(catalog)} issue types to {OUT}")


# Issue types whose names do not start with ERROR_ (see SEOnaut's issue_types table).
KNOWN_NON_ERROR_TYPES = {
    "HTTP_SCHEME",
    "DEAD_END",
    "NO_INDEX_IN_SITEMAP",
    "BLOCKED_IN_SITEMAP",
    "NON_CANONICAL_IN_SITEMAP",
    "INCOMING_FOLLOW_NOFOLLOW",
    "INVALID_LANG",
}

if __name__ == "__main__":
    if len(sys.argv) != 2:
        sys.exit(__doc__)
    main(sys.argv[1])
