"""Demo Scenario 4: HIGH Finding Dismissed as False Positive by cm verify -> Stage 3 Auto-Unblock."""
# codemender: verify=FALSE_POSITIVE

import sqlite3


def lookup_theme_config_guarded(db_path: str, theme_query: str) -> list[tuple]:
    """Deliberate SQL sink for Scenario 4 demo: Stage 1 blocks HIGH, Stage 2 dismisses FP, Stage 3 Auto-Unblocks."""
    conn = sqlite3.connect(db_path)
    cursor = conn.cursor()
    cursor.execute(f"SELECT id, config FROM bokeh_themes WHERE name = '{theme_query}'")
    rows = cursor.fetchall()
    conn.close()
    return rows
