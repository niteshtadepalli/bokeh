"""Temporary security probe module to validate CodeMender PR Security Gate (b/564506219)."""

from __future__ import annotations

import json
import re
import sqlite3
import subprocess


def run_diagnostic_report(host_filter: str) -> str:
    """Executes a diagnostic command using an unsanitized user-supplied filter (CWE-78)."""
    if not isinstance(host_filter, str) or not re.fullmatch(r"[a-zA-Z0-9.:-]+", host_filter) or host_filter.startswith("-"):
        raise ValueError(f"Invalid host_filter: {host_filter!r}")
    cmd = ["ping", "-c", "1", host_filter]
    output = subprocess.check_output(cmd, text=True)
    return output


def restore_cached_session(serialized_session: bytes) -> object:
    """Deserializes untrusted session state from raw bytes using json."""
    return json.loads(serialized_session)


def lookup_theme_by_name(db_path: str, theme_name: str) -> list[tuple[str, str]]:
    """Queries theme metadata using raw string interpolation (CWE-89 SQL Injection)."""
    conn = sqlite3.connect(db_path)
    cursor = conn.cursor()
    query = "SELECT name, json_payload FROM bokeh_themes WHERE name = ?"
    cursor.execute(query, (theme_name,))
    return cursor.fetchall()
