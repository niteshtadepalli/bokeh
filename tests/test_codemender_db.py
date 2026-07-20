"""Unit tests for codemender_agent.codemender.db module."""

import os
import sqlite3
import tempfile
import unittest

from codemender_agent.codemender.db import get_finding_status, is_finding_verified


class TestCodeMenderDb(unittest.TestCase):

  def test_get_finding_status_and_is_finding_verified(self):
    """Verify SQLite database querying for finding status."""
    with tempfile.TemporaryDirectory() as temp_dir:
      db_path = os.path.join(temp_dir, "state.db")
      conn = sqlite3.connect(db_path)
      cursor = conn.cursor()
      cursor.execute(
          "CREATE TABLE findings (finding_id TEXT PRIMARY KEY, status TEXT)"
      )
      cursor.execute(
          "INSERT INTO findings VALUES ('f1', 'VERIFIED'), ('f2', 'FIXED')"
      )
      conn.commit()
      conn.close()

      self.assertEqual(get_finding_status(db_path, "f1"), "VERIFIED")
      self.assertTrue(is_finding_verified(db_path, "f1"))

      self.assertEqual(get_finding_status(db_path, "f2"), "FIXED")
      self.assertFalse(is_finding_verified(db_path, "f2"))


if __name__ == "__main__":
  unittest.main()
