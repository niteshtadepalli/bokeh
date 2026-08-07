# Copyright 2026 Google LLC
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""Unit tests for Stage 3 Aggregator runner."""

import json
import os
import sqlite3
import tempfile
import unittest
from unittest.mock import MagicMock, patch

from codemender_agent.runners.aggregate import (
    _inject_token_metrics_into_html,
    merge_db,
    run_aggregate_pipeline,
)


class TestAggregateRunner(unittest.TestCase):

  def setUp(self):
    self.temp_dir = tempfile.TemporaryDirectory()
    self.workspace_dir = self.temp_dir.name

    self.env_patcher = patch.dict(
        os.environ,
        {
            "HOME": self.workspace_dir,
            "CODEMENDER_SCAN_ID": "test-scan-123",
            "CODEMENDER_GCS_BUCKET": "test-bucket",
            "WORKSPACE_DIR": self.workspace_dir,
            "GITHUB_REPO_URL": "https://github.com/owner/repo.git",
            "GITHUB_TOKEN": "fake-token",
            "CODEMENDER_BUILD_COMMAND": "echo 'build'",
        },
    )
    self.env_patcher.start()

  def tearDown(self):
    self.env_patcher.stop()
    self.temp_dir.cleanup()

  def create_test_db(
      self,
      path,
      findings_data,
      sessions_data=None,
      artifacts_data=None,
      patches_data=None,
  ):
    conn = sqlite3.connect(path)
    cursor = conn.cursor()

    cursor.execute("""
        CREATE TABLE findings (
            finding_id TEXT PRIMARY KEY, session_id TEXT, title TEXT, file_path TEXT, severity TEXT, confidence TEXT,
            analysis TEXT, snippet TEXT, vuln_type TEXT, vuln_id TEXT, verified INTEGER, muted INTEGER,
            mute_reason TEXT, created_at TEXT, fingerprint TEXT, status TEXT, source_stage TEXT, finding_json TEXT,
            updated_at TEXT, start_line INTEGER, end_line INTEGER, dismiss_reason TEXT, confidence_level TEXT
        )
    """)
    cursor.execute("""
        CREATE TABLE sessions (
            session_id TEXT PRIMARY KEY, operation_name TEXT, session_type TEXT, status TEXT, pipeline_mode TEXT,
            target TEXT, created_at TEXT, updated_at TEXT, project_root TEXT
        )
    """)
    cursor.execute("""
        CREATE TABLE artifacts (
            id INTEGER PRIMARY KEY AUTOINCREMENT, session_id TEXT, filename TEXT, original_path TEXT, purpose TEXT,
            finding_id TEXT, created_at TEXT
        )
    """)
    cursor.execute("""
        CREATE TABLE patches (
            patch_id TEXT PRIMARY KEY, finding_id TEXT, session_id TEXT, diff TEXT, reasoning TEXT,
            status TEXT DEFAULT 'pending', backup_path TEXT, target_file TEXT DEFAULT '',
            edited_files TEXT DEFAULT '[]', validation_result TEXT DEFAULT '', created_at TEXT
        )
    """)

    for f in findings_data:
      cursor.execute(
          """
          INSERT INTO findings (finding_id, title, status, updated_at)
          VALUES (?, ?, ?, ?)
      """,
          (f["finding_id"], f["title"], f["status"], f["updated_at"]),
      )

    if sessions_data:
      for s in sessions_data:
        cursor.execute(
            """
            INSERT INTO sessions (session_id, status, updated_at)
            VALUES (?, ?, ?)
        """,
            (s["session_id"], s["status"], s["updated_at"]),
        )

    if artifacts_data:
      for a in artifacts_data:
        cursor.execute(
            """
            INSERT INTO artifacts (session_id, filename, finding_id)
            VALUES (?, ?, ?)
        """,
            (a["session_id"], a["filename"], a.get("finding_id")),
        )

    if patches_data:
      for p in patches_data:
        cursor.execute(
            """
            INSERT INTO patches (
                patch_id, finding_id, session_id, diff, status, backup_path,
                target_file, edited_files, validation_result, created_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
            (
                p["patch_id"],
                p["finding_id"],
                p["session_id"],
                p["diff"],
                p.get("status", "applied"),
                p.get("backup_path", ""),
                p.get("target_file", ""),
                p.get("edited_files", "[]"),
                p.get("validation_result", ""),
                p.get("created_at", ""),
            ),
        )

    conn.commit()
    conn.close()

  def test_merge_db_success(self):
    base_db = os.path.join(self.workspace_dir, "base_state.db")
    worker_db = os.path.join(self.workspace_dir, "worker_state.db")

    self.create_test_db(
        base_db,
        [
            {
                "finding_id": "fid-1",
                "title": "Old Title 1",
                "status": "DETECTED",
                "updated_at": "2026-07-20T10:00:00Z",
            },
            {
                "finding_id": "fid-2",
                "title": "Title 2",
                "status": "DETECTED",
                "updated_at": "2026-07-20T10:00:00Z",
            },
        ],
        [{
            "session_id": "sess-1",
            "status": "RUNNING",
            "updated_at": "2026-07-20T10:00:00Z",
        }],
        [{"session_id": "sess-1", "filename": "art-1", "finding_id": "fid-1"}],
        [{
            "patch_id": "pid-1",
            "finding_id": "fid-1",
            "session_id": "sess-1",
            "diff": "old-diff",
            "target_file": "old_file.py",
        }],
    )

    self.create_test_db(
        worker_db,
        [
            {
                "finding_id": "fid-1",
                "title": "Updated Title 1",
                "status": "FIXED",
                "updated_at": "2026-07-21T12:00:00Z",
            },
            {
                "finding_id": "fid-3",
                "title": "Title 3",
                "status": "FIXED",
                "updated_at": "2026-07-21T12:00:00Z",
            },
        ],
        [{
            "session_id": "sess-1",
            "status": "COMPLETED",
            "updated_at": "2026-07-21T12:00:00Z",
        }],
        [
            {
                "session_id": "sess-1",
                "filename": "art-1",
                "finding_id": "fid-1",
            },
            {
                "session_id": "sess-1",
                "filename": "art-2",
                "finding_id": "fid-2",
            },
            {
                "session_id": "sess-1",
                "filename": "art-3",
                "finding_id": "fid-3",
            },
        ],
        [
            {
                "patch_id": "pid-1",
                "finding_id": "fid-1",
                "session_id": "sess-1",
                "diff": "new-diff",
                "target_file": "file1.py",
                "edited_files": '["file1.py"]',
                "validation_result": "passed",
            },
            {
                "patch_id": "pid-2",
                "finding_id": "fid-2",
                "session_id": "sess-1",
                "diff": "diff-2",
                "target_file": "file2.py",
                "edited_files": '["file2.py"]',
                "validation_result": "passed",
            },
            {
                "patch_id": "pid-3",
                "finding_id": "fid-3",
                "session_id": "sess-1",
                "diff": "ghost-diff",
                "target_file": "ghost.py",
            },
        ],
    )

    merge_db(base_db, worker_db)

    conn = sqlite3.connect(base_db)
    cursor = conn.cursor()

    cursor.execute(
        "SELECT finding_id, title, status, updated_at FROM findings ORDER BY"
        " finding_id"
    )
    findings = cursor.fetchall()
    self.assertEqual(len(findings), 2)
    self.assertEqual(
        findings[0],
        ("fid-1", "Updated Title 1", "FIXED", "2026-07-21T12:00:00Z"),
    )
    self.assertEqual(
        findings[1], ("fid-2", "Title 2", "DETECTED", "2026-07-20T10:00:00Z")
    )

    cursor.execute("SELECT session_id, status, updated_at FROM sessions")
    sessions = cursor.fetchall()
    self.assertEqual(len(sessions), 1)
    self.assertEqual(
        sessions[0], ("sess-1", "COMPLETED", "2026-07-21T12:00:00Z")
    )

    cursor.execute(
        "SELECT session_id, filename FROM artifacts ORDER BY filename"
    )
    artifacts = cursor.fetchall()
    self.assertEqual(len(artifacts), 2)
    self.assertEqual(artifacts[0], ("sess-1", "art-1"))
    self.assertEqual(artifacts[1], ("sess-1", "art-2"))

    # Assert patches are merged and ghost patch (pid-3) is excluded
    cursor.execute(
        "SELECT patch_id, finding_id, diff, target_file, edited_files,"
        " validation_result FROM patches ORDER BY patch_id"
    )
    patches = cursor.fetchall()
    self.assertEqual(len(patches), 2)
    self.assertEqual(
        patches[0],
        ("pid-1", "fid-1", "new-diff", "file1.py", '["file1.py"]', "passed"),
    )
    self.assertEqual(
        patches[1],
        ("pid-2", "fid-2", "diff-2", "file2.py", '["file2.py"]', "passed"),
    )

    conn.close()

  @patch("codemender_agent.runners.aggregate.run_command")
  @patch("codemender_agent.runners.aggregate.download_file_from_gcs")
  @patch("codemender_agent.runners.aggregate.list_gcs_blobs")
  @patch("codemender_agent.runners.aggregate.upload_and_sign_report")
  @patch("codemender_agent.runners.aggregate.merge_db")
  @patch("tarfile.open")
  @patch("shutil.which")
  def test_aggregate_pipeline_success(
      self,
      mock_which,
      _mock_tarfile_open,
      mock_merge_db,
      mock_upload_and_sign_report,
      mock_list_gcs_blobs,
      mock_download_gcs,
      mock_run_cmd,
  ):
    mock_which.return_value = "/bin/cm"
    mock_list_gcs_blobs.return_value = [
        "scans/test-scan-123/worker_0_state.db",
        "scans/test-scan-123/worker_1_state.db",
    ]
    mock_upload_and_sign_report.return_value = "https://report-url"

    def download_side_effect(dest_path, _bucket, blob):
      if "manifest.json" in blob:
        with open(dest_path, "w") as f:
          json.dump({"findings_count": 2, "target_sha": "abc123commitsha"}, f)
        return True
      return True

    mock_download_gcs.side_effect = download_side_effect

    mock_default = MagicMock()
    mock_default.stdout = ""
    mock_default.returncode = 0
    mock_run_cmd.return_value = mock_default

    db_path = os.path.join(self.workspace_dir, ".codemender", "state.db")

    def mock_extractall(*_args, **_kwargs):
      db_dir = os.path.join(self.workspace_dir, ".codemender")
      os.makedirs(db_dir, exist_ok=True)
      conn = sqlite3.connect(db_path)
      conn.execute(
          "CREATE TABLE findings (finding_id TEXT PRIMARY KEY, status TEXT)"
      )
      conn.execute("INSERT INTO findings VALUES ('fid-1', 'OPEN')")
      conn.execute("INSERT INTO findings VALUES ('fid-2', 'DISMISSED')")
      conn.commit()
      conn.close()

    _mock_tarfile_open.return_value.__enter__.return_value.extractall.side_effect = (
        mock_extractall
    )

    run_aggregate_pipeline()

    # Assert DISMISSED finding was removed from local state.db for clean HTML report
    conn = sqlite3.connect(db_path)
    cursor = conn.cursor()
    cursor.execute("SELECT finding_id, status FROM findings ORDER BY finding_id")
    rows = cursor.fetchall()
    conn.close()

    self.assertEqual(len(rows), 1)
    self.assertEqual(rows[0][0], "fid-1")
    self.assertEqual(rows[0][1], "OPEN")

    mock_download_gcs.assert_any_call(
        os.path.join(self.workspace_dir, "manifest.json"),
        "test-bucket",
        "scans/test-scan-123/manifest.json",
    )

    mock_download_gcs.assert_any_call(
        os.path.join(self.workspace_dir, "workspace_base.tar.gz"),
        "test-bucket",
        "scans/test-scan-123/workspace_base.tar.gz",
    )

    mock_download_gcs.assert_any_call(
        os.path.join(self.workspace_dir, "worker_dbs", "worker_0_state.db"),
        "test-bucket",
        "scans/test-scan-123/worker_0_state.db",
    )
    mock_download_gcs.assert_any_call(
        os.path.join(self.workspace_dir, "worker_dbs", "worker_1_state.db"),
        "test-bucket",
        "scans/test-scan-123/worker_1_state.db",
    )

    self.assertEqual(mock_merge_db.call_count, 2)

    report_called = False
    for call in mock_run_cmd.call_args_list:
      cmd = call[0][0]
      if "report" in cmd and "html" in cmd:
        report_called = True
    self.assertTrue(report_called)

    mock_upload_and_sign_report.assert_called_once()

  def test_inject_token_metrics_into_html(self):
    html_path = os.path.join(self.workspace_dir, "report.html")
    with open(html_path, "w", encoding="utf-8") as f:
      f.write("<!DOCTYPE html><html><head><title>Report</title></head><body><h1>Scan Summary</h1></body></html>")

    token_totals = {
        "in_tokens": 12500,
        "out_tokens": 800,
        "total_tokens": 13300,
    }

    _inject_token_metrics_into_html(html_path, token_totals)

    with open(html_path, "r", encoding="utf-8") as f:
      content = f.read()

    self.assertIn("codemender-token-metrics-banner", content)
    self.assertIn("12,500", content)
    self.assertIn("800", content)
    self.assertIn("13,300", content)
    self.assertIn("⚡ LLM Token Usage Summary", content)


if __name__ == "__main__":
  unittest.main()
