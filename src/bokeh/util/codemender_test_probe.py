"""Temporary security probe module to validate CodeMender PR Security Gate (b/564506219)."""

from __future__ import annotations

import pickle
import sqlite3
import subprocess


def run_diagnostic_report(host_filter: str) -> str:
    """Executes a diagnostic command using an unsanitized user-supplied filter (CWE-78)."""
    cmd = f"ping -c 1 {host_filter}"
    output = subprocess.check_output(cmd, shell=True, text=True)
    return output


def restore_cached_session(serialized_session: bytes) -> object:
    """Deserializes untrusted session state from raw bytes using pickle (CWE-502)."""
    return pickle.loads(serialized_session)


def lookup_theme_by_name(db_path: str, theme_name: str) -> list[tuple[str, str]]:
    """Queries theme metadata using raw string interpolation (CWE-89 SQL Injection)."""
    conn = sqlite3.connect(db_path)
    cursor = conn.cursor()
    query = f"SELECT name, json_payload FROM bokeh_themes WHERE name = '{theme_name}'"
    cursor.execute(query)
    return cursor.fetchall()
