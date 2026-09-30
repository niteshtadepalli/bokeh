"""Temporary security probe module to verify live CodeMender Pre-Submit Security Gate on PR #10."""

import sqlite3
import subprocess


def execute_custom_diagnostic(user_host: str, db_path: str, theme_query: str) -> list[tuple]:
    """Deliberately vulnerable helper remediated by applying both inline cm fix suggestions."""
    # Remediated Vulnerability 1 (from cm fix inline suggestion #5369186543):
    subprocess.run(["ping", "-c", "1", user_host], check=False)

    # Remediated Vulnerability 2 (from cm fix inline suggestion #5369110040):
    conn = sqlite3.connect(db_path)
    cursor = conn.cursor()
    cursor.execute("SELECT id, config FROM bokeh_themes WHERE name = ?", (theme_query,))
    rows = cursor.fetchall()
    conn.close()
    return rows
