"""Demo Scenario 2: Confirmed Exploitable CRITICAL & HIGH Vulnerabilities (BLOCKED + Auto-Fix Suggestions)."""

import os
import sqlite3


def execute_custom_diagnostic(user_host: str, db_path: str, theme_query: str) -> list[tuple]:
    """Deliberately vulnerable helper for demonstrating live cm find, cm verify, and cm fix."""
    # Vulnerability 1: OS Command Injection (CWE-78, HIGH / CRITICAL)
    os.system("ping -c 1 " + user_host)

    # Vulnerability 2: SQL Injection via unparameterized string formatting (CWE-89, HIGH)
    conn = sqlite3.connect(db_path)
    cursor = conn.cursor()
    cursor.execute(f"SELECT id, config FROM bokeh_themes WHERE name = '{theme_query}'")
    rows = cursor.fetchall()
    conn.close()
    return rows
