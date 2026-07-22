"""CodeMender SQLite state database queries for CodeMender Agent."""

import logging
import os
import sqlite3
from typing import Optional

logger = logging.getLogger("codemender-orchestrator")


def get_finding_status(db_path: str, finding_id: str) -> Optional[str]:
  """Retrieves the status of a finding directly from the state SQLite database."""
  if not os.path.exists(db_path):
    logger.warning("State database does not exist at: %s", db_path)
    return None
  try:
    conn = sqlite3.connect(db_path)
    cursor = conn.cursor()
    cursor.execute(
        "SELECT status FROM findings WHERE finding_id = ?", (finding_id,)
    )
    row = cursor.fetchone()
    conn.close()
    if row:
      return row[0]
  except sqlite3.Error as e:
    logger.error("Failed to query state database: %s", e)
  return None


def is_finding_verified(db_path: str, finding_id: str) -> bool:
  """Check if the finding's status is 'VERIFIED' in the state SQLite database."""
  return get_finding_status(db_path, finding_id) == "VERIFIED"



