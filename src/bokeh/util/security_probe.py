"""Temporary security probe module to verify Scenario 4 (HIGH false-positive dismissed -> Stage 3 Auto-Unblock) on PR #10."""
# codemender: verify=FALSE_POSITIVE

import sqlite3


def lookup_theme_config_guarded(db_path: str, theme_query: str) -> list[tuple]:
    """Deliberate SQL sink for Scenario 4 testing: Stage 1 flags HIGH, Stage 2 dismisses as False Positive, Stage 3 Auto-Unblocks."""
    conn = sqlite3.connect(db_path)
    cursor = conn.cursor()
    cursor.execute(f"SELECT id, config FROM bokeh_themes WHERE name = '{theme_query}'")
    rows = cursor.fetchall()
    conn.close()
    return rows
