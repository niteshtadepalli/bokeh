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
import tarfile
import tempfile
import unittest
from unittest.mock import MagicMock, patch

from codemender_agent.runners.aggregate import (
    _inject_token_metrics_into_html,
    _render_step_summary,
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
          INSERT INTO findings (
              finding_id, title, status, updated_at, file_path, start_line, vuln_type, vuln_id, severity
          )
          VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
      """,
          (
              f["finding_id"],
              f["title"],
              f["status"],
              f["updated_at"],
              f.get("file_path", ""),
              f.get("start_line", 0),
              f.get("vuln_type", ""),
              f.get("vuln_id", ""),
              f.get("severity", ""),
          ),
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

  def test_merge_db_file_hashes(self):
    base_db = os.path.join(self.workspace_dir, "base_hashes.db")
    worker_db = os.path.join(self.workspace_dir, "worker_hashes.db")

    conn1 = sqlite3.connect(base_db)
    conn1.execute(
        "CREATE TABLE file_hashes (file_path TEXT PRIMARY KEY, hash TEXT)"
    )
    conn1.execute("INSERT INTO file_hashes VALUES ('main.py', 'hash1')")
    conn1.commit()
    conn1.close()

    conn2 = sqlite3.connect(worker_db)
    conn2.execute(
        "CREATE TABLE file_hashes (file_path TEXT PRIMARY KEY, hash TEXT)"
    )
    conn2.execute("INSERT INTO file_hashes VALUES ('main.py', 'updated_hash1')")
    conn2.execute("INSERT INTO file_hashes VALUES ('utils.py', 'hash2')")
    conn2.commit()
    conn2.close()

    merge_db(base_db, worker_db)

    conn = sqlite3.connect(base_db)
    cursor = conn.cursor()
    cursor.execute("SELECT file_path, hash FROM file_hashes ORDER BY file_path")
    rows = cursor.fetchall()
    self.assertEqual(len(rows), 2)
    self.assertEqual(rows[0], ("main.py", "updated_hash1"))
    self.assertEqual(rows[1], ("utils.py", "hash2"))
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

  def test_inject_token_metrics_into_html_single_model(self):
    html_path = os.path.join(self.workspace_dir, "report.html")
    with open(html_path, "w", encoding="utf-8") as f:
      f.write("<!DOCTYPE html><html><head><title>Report</title></head><body><h1>Scan Summary</h1></body></html>")

    token_totals = {
        "gemini-2.5-flash": {
            "in_tokens": 12500,
            "out_tokens": 800,
            "total_tokens": 13300,
        }
    }

    _inject_token_metrics_into_html(html_path, token_totals)

    with open(html_path, "r", encoding="utf-8") as f:
      content = f.read()

    self.assertIn("codemender-token-metrics-banner", content)
    self.assertIn("12,500", content)
    self.assertIn("800", content)
    self.assertIn("13,300", content)
    self.assertIn("(Model: <code>gemini-2.5-flash</code>)", content)
    self.assertNotIn("Per-Model Breakdown", content)

  def test_inject_token_metrics_into_html_default_model(self):
    html_path = os.path.join(self.workspace_dir, "report_default.html")
    with open(html_path, "w", encoding="utf-8") as f:
      f.write("<!DOCTYPE html><html><head><title>Report</title></head><body><h1>Scan Summary</h1></body></html>")

    token_totals = {
        "default": {
            "in_tokens": 31000,
            "out_tokens": 651,
            "total_tokens": 31000,
        }
    }

    _inject_token_metrics_into_html(html_path, token_totals)

    with open(html_path, "r", encoding="utf-8") as f:
      content = f.read()

    self.assertIn("codemender-token-metrics-banner", content)
    self.assertIn("31,000", content)
    self.assertIn("651", content)
    self.assertIn("(Model: <code>default</code>)", content)
    self.assertNotIn("Per-Model Breakdown", content)

  def test_inject_token_metrics_into_html_multi_model(self):
    html_path = os.path.join(self.workspace_dir, "report_multi.html")
    with open(html_path, "w", encoding="utf-8") as f:
      f.write("<!DOCTYPE html><html><head><title>Report</title></head><body><h1>Scan Summary</h1></body></html>")

    token_totals = {
        "gemini-2.5-flash": {
            "in_tokens": 10000,
            "out_tokens": 500,
            "total_tokens": 10500,
        },
        "gemini-2.5-pro": {
            "in_tokens": 5000,
            "out_tokens": 300,
            "total_tokens": 5300,
        },
    }

    _inject_token_metrics_into_html(html_path, token_totals)

    with open(html_path, "r", encoding="utf-8") as f:
      content = f.read()

    self.assertIn("codemender-token-metrics-banner", content)
    # Grand totals: 15,000 in, 800 out, 15,800 total
    self.assertIn("15,000", content)
    self.assertIn("800", content)
    self.assertIn("15,800", content)
    # Table breakdown
    self.assertIn("Per-Model Breakdown", content)
    self.assertIn("gemini-2.5-flash", content)
    self.assertIn("10,000", content)
    self.assertIn("gemini-2.5-pro", content)
    self.assertIn("5,000", content)

  @patch("codemender_agent.runners.aggregate._generate_and_upload_report")
  @patch("codemender_agent.runners.aggregate._aggregate_token_metrics")
  @patch("codemender_agent.runners.aggregate._download_and_merge_worker_dbs")
  @patch("codemender_agent.runners.aggregate.list_gcs_blobs")
  @patch("codemender_agent.runners.aggregate.download_file_from_gcs")
  @patch("codemender_agent.runners.aggregate.run_command")
  @patch("tarfile.open")
  def test_run_aggregate_pipeline_passes_token_totals_to_report(
      self,
      mock_tarfile_open,
      mock_run_cmd,
      mock_download_gcs,
      mock_list_gcs,
      mock_download_merge,
      mock_aggregate_tokens,
      mock_generate_report,
  ):
    manifest_data = {
        "findings_count": 2,
        "target_sha": "abc123sha",
        "partition_urls": ["http://url1", "http://url2"],
        "upload_urls": ["http://u1", "http://u2"],
    }
    manifest_path = os.path.join(self.workspace_dir, "manifest.json")
    with open(manifest_path, "w") as f:
      json.dump(manifest_data, f)

    mock_download_gcs.return_value = True
    mock_list_gcs.return_value = [
        "scans/test-scan-123/worker_0_state.db",
        "scans/test-scan-123/worker_1_state.db",
    ]
    mock_aggregate_tokens.return_value = {
        "gemini-2.5-flash": {
            "in_tokens": 15000,
            "out_tokens": 900,
            "total_tokens": 15900,
        }
    }

    with patch.dict(os.environ, {"CODEMENDER_CLI_VERSION": "preview"}):
      run_aggregate_pipeline()

    mock_aggregate_tokens.assert_called_once_with(
        self.workspace_dir,
        "test-bucket",
        "test-scan-123",
        [
            "scans/test-scan-123/worker_0_state.db",
            "scans/test-scan-123/worker_1_state.db",
        ],
    )
    mock_generate_report.assert_called_once()
    self.assertEqual(
        mock_generate_report.call_args.kwargs.get("token_totals"),
        {
            "gemini-2.5-flash": {
                "in_tokens": 15000,
                "out_tokens": 900,
                "total_tokens": 15900,
            }
        },
    )

  def test_render_step_summary_formatting(self):
    """Test step summary Markdown formatting with stats and findings table."""
    from codemender_agent.config import OrchestratorConfig
    from codemender_agent.runners.aggregate import _render_step_summary

    db_path = os.path.join(self.workspace_dir, "summary_test.db")
    self.create_test_db(
        db_path,
        [
            {
                "finding_id": "fid-1",
                "title": "SQL Injection in Login",
                "status": "FIXED",
                "updated_at": "2026-08-01",
                "severity": "CRITICAL",
                "vuln_type": "SQL Injection",
                "vuln_id": "CWE-89",
                "file_path": "routes/login.ts",
                "start_line": 42,
            },
            {
                "finding_id": "fid-2",
                "title": "XSS in Profile",
                "status": "PRE_EXISTING_IGNORED",
                "updated_at": "2026-08-01",
                "severity": "HIGH",
                "vuln_type": "Cross-Site Scripting",
                "vuln_id": "CWE-79",
                "file_path": "routes/profile.ts",
                "start_line": 15,
            },
            {
                "finding_id": "fid-3",
                "title": "CSRF in Settings",
                "status": "SKIPPED_DUPLICATE",
                "updated_at": "2026-08-01",
                "severity": "MEDIUM",
                "vuln_type": "CSRF",
                "vuln_id": "CWE-352",
                "file_path": "routes/settings.ts",
                "start_line": 100,
            },
        ],
    )

    summary_file = os.path.join(self.workspace_dir, "step_summary.md")
    with patch.dict(os.environ, {"GITHUB_STEP_SUMMARY": summary_file}):
      config = OrchestratorConfig(is_pr_scan=False)
      summary_md, count = _render_step_summary(
          db_path,
          config,
          owner="my-org",
          repo_name="my-repo",
          target_sha="abc123456789",
          token_totals={"gemini-2.5-flash": {"in_tokens": 100, "out_tokens": 50, "total_tokens": 150}},
          finding_prs={"fid-1": "https://github.com/my-org/my-repo/pull/42"},
      )

    self.assertEqual(count, 3)
    self.assertIn("# 🛡️ CodeMender Security Remediation Summary", summary_md)
    self.assertIn("my-org/my-repo", summary_md)
    self.assertIn("Nightly Repository Scan", summary_md)
    self.assertIn("fid-1", summary_md)
    self.assertIn("🔴 CRITICAL", summary_md)
    self.assertIn("🟠 HIGH", summary_md)
    self.assertIn("🟡 MEDIUM", summary_md)
    self.assertIn("SQL Injection (CWE-89)", summary_md)
    self.assertIn("[FIXED (#42)](https://github.com/my-org/my-repo/pull/42)", summary_md)
    self.assertIn("PRE_EXISTING_IGNORED", summary_md)
    self.assertIn("SKIPPED_DUPLICATE", summary_md)
    self.assertIn("Interactive Security Report & Export Artifacts", summary_md)
    self.assertIn("150", summary_md)
    self.assertIn("| `gemini-2.5-flash` | 100 | 50 | 150 |", summary_md)
    self.assertTrue(os.path.exists(summary_file))

  def test_render_step_summary_truncation(self):
    """Test step summary truncation when exceeding 1000 KiB buffer size."""
    from codemender_agent.config import OrchestratorConfig
    from codemender_agent.runners.aggregate import _render_step_summary

    db_path = os.path.join(self.workspace_dir, "summary_large.db")
    # Create DB with very large number of findings
    large_findings = [
        {"finding_id": f"fid-{i}", "title": f"Vulnerability {i} " + ("x" * 200), "status": "DETECTED", "updated_at": "2026-08-01"}
        for i in range(5000)
    ]
    self.create_test_db(db_path, large_findings)

    config = OrchestratorConfig(is_pr_scan=False)
    summary_md, count = _render_step_summary(
        db_path,
        config,
        owner="my-org",
        repo_name="my-repo",
    )

    self.assertLessEqual(len(summary_md.encode("utf-8")), 1000 * 1024 + 100)
    self.assertIn("Summary truncated", summary_md)

  def test_sanitize_sarif_file(self):
    """Test SARIF file path sanitization, message deduplication, and duplicate suppression injection."""
    from codemender_agent.runners.aggregate import _sanitize_sarif_file

    repo_dir = os.path.join(self.workspace_dir, "my-repo")
    os.makedirs(repo_dir, exist_ok=True)
    sarif_path = os.path.join(self.workspace_dir, "test.sarif")

    raw_sarif = {
        "version": "2.1.0",
        "runs": [
            {
                "tool": {
                    "driver": {
                        "name": "CodeMender",
                        "rules": [
                            {
                                "id": "fid-1",
                                "name": "SQL Injection",
                                "shortDescription": {"text": "SQL Injection"},
                                "fullDescription": {"text": "SQL Injection"},
                            },
                            {
                                "id": "fid-2",
                                "name": "XSS",
                                "shortDescription": {"text": "XSS"},
                                "fullDescription": {"text": "Cross-site scripting vulnerability"},
                            },
                        ],
                    }
                },
                "results": [
                    {
                        "ruleId": "fid-1",
                        "ruleIndex": 0,
                        "message": {
                            "text": (
                                "SQL Injection in User Login: ## Root Cause Analysis (RCA)\n"
                                "User input from `username` is directly concatenated into SQL query."
                            )
                        },
                        "locations": [
                            {
                                "physicalLocation": {
                                    "artifactLocation": {
                                        "uri": f"{repo_dir}/src/db/user.py"
                                    }
                                }
                            }
                        ],
                        "properties": {"finding_id": "fid-1", "status": "SKIPPED_DUPLICATE"},
                    },
                    {
                        "ruleId": "fid-2",
                        "ruleIndex": 1,
                        "message": {"text": "XSS in Profile Page"},
                        "locations": [
                            {
                                "physicalLocation": {
                                    "artifactLocation": {
                                        "uri": "src/web/app.py"
                                    }
                                }
                            }
                        ],
                        "properties": {"finding_id": "fid-2", "status": "FIXED"},
                    },
                ],
            }
        ],
    }

    # Write raw sarif with trailing log line (simulating cm report shutdown logs)
    with open(sarif_path, "w", encoding="utf-8") as f:
      f.write(json.dumps(raw_sarif) + "\n2026-08-28T16:35:50Z [INFO] 📄 Session log: /github/home/log.log\n")

    # Sanitize for Nightly scan (is_pr_scan=False)
    _sanitize_sarif_file(sarif_path, repo_dir, skipped_finding_ids={"fid-1"}, is_pr_scan=False)

    with open(sarif_path, "r", encoding="utf-8") as f:
      sanitized = json.load(f)

    results = sanitized["runs"][0]["results"]
    rules = sanitized["runs"][0]["tool"]["driver"]["rules"]

    # 1. Path should be relative
    self.assertEqual(results[0]["locations"][0]["physicalLocation"]["artifactLocation"]["uri"], "src/db/user.py")

    # 2. Result message should be deduplicated (concise title only)
    self.assertEqual(results[0]["message"]["text"], "SQL Injection in User Login")
    self.assertEqual(results[1]["message"]["text"], "XSS in Profile Page")

    # 3. Rule details should have formatted markdown help
    self.assertEqual(rules[0]["shortDescription"]["text"], "SQL Injection in User Login")
    self.assertIn("## Root Cause Analysis (RCA)", rules[0]["help"]["markdown"])
    self.assertEqual(rules[0]["fullDescription"]["text"], "## Root Cause Analysis (RCA)")

    # 4. Suppressions should be present for fid-1
    self.assertIn("suppressions", results[0])
    self.assertEqual(results[0]["suppressions"][0]["status"], "underReview")

    # 5. No suppressions for fid-2
    self.assertNotIn("suppressions", results[1])
    # Rule without concatenated analysis retains fullDescription and populates help
    self.assertEqual(rules[1]["help"]["markdown"], "Cross-site scripting vulnerability")

  @patch("codemender_agent.runners.aggregate.post_commit_status")
  @patch("codemender_agent.runners.aggregate.run_command")
  @patch("codemender_agent.runners.aggregate.merge_db")
  @patch("shutil.which")
  def test_aggregate_pipeline_github_actions_mode(
      self,
      mock_which,
      mock_merge_db,
      mock_run_cmd,
      mock_post_status,
  ):
    """Test full aggregate pipeline execution in github_actions storage mode."""
    mock_which.return_value = "/bin/cm"
    mock_default = MagicMock()
    mock_default.stdout = ""
    mock_default.returncode = 0
    mock_run_cmd.return_value = mock_default

    # Create local transit structure
    transit_base = os.path.join(self.workspace_dir, ".codemender_transit", "base")
    os.makedirs(transit_base, exist_ok=True)
    with open(os.path.join(transit_base, "manifest.json"), "w") as f:
      json.dump({"findings_count": 1, "target_sha": "def456sha"}, f)

    # Create base DB in ~/.codemender
    db_dir = os.path.join(self.workspace_dir, ".codemender")
    os.makedirs(db_dir, exist_ok=True)
    base_db = os.path.join(db_dir, "state.db")
    self.create_test_db(
        base_db,
        [
            {"finding_id": "fid-1", "title": "SQL Injection", "status": "DETECTED", "updated_at": "2026-08-01"},
            {"finding_id": "fid-2", "title": "Pre-existing XSS", "status": "PRE_EXISTING_IGNORED", "updated_at": "2026-08-01"},
        ],
    )

    tarball_file = os.path.join(transit_base, "workspace_base.tar.gz")
    with tarfile.open(tarball_file, "w:gz") as tar:
      tar.add(db_dir, arcname=".codemender")

    # Create worker shard in .codemender_transit/shards/worker_0/
    shard_dir = os.path.join(self.workspace_dir, ".codemender_transit", "shards", "worker_0")
    os.makedirs(shard_dir, exist_ok=True)
    worker_db = os.path.join(shard_dir, "worker_0_state.db")
    self.create_test_db(
        worker_db,
        [{"finding_id": "fid-1", "title": "SQL Injection", "status": "FIXED", "updated_at": "2026-08-02"}],
    )

    with patch.dict(
        os.environ,
        {
            "CODEMENDER_STORAGE_MODE": "github_actions",
            "CODEMENDER_IS_PR_SCAN": "true",
            "CODEMENDER_TOTAL_WORKERS": "1",
            "GITHUB_TOKEN": "valid-token",
        },
    ):
      run_aggregate_pipeline()

    # Verify merge_db called for shard
    mock_merge_db.assert_called()

    # Verify report commands executed
    cmd_names = [call[0][0] for call in mock_run_cmd.call_args_list]
    html_called = any("report" in c and "html" in c for c in cmd_names)
    sarif_called = any("report" in c and "sarif" in c for c in cmd_names)
    self.assertTrue(html_called)
    self.assertTrue(sarif_called)

    # Verify commit status was posted as failure to block target PR
    mock_post_status.assert_called_once()
    status_kwargs = mock_post_status.call_args.kwargs
    self.assertEqual(status_kwargs["state"], "failure")
    self.assertEqual(status_kwargs["context"], "CodeMender / Security Gate")
    self.assertIn("Security Gate FAILED", status_kwargs["description"])

  @patch("codemender_agent.runners.aggregate.post_commit_status")
  @patch("codemender_agent.runners.aggregate.run_command")
  @patch("codemender_agent.runners.aggregate.merge_db")
  @patch("shutil.which")
  def test_aggregate_pipeline_pr_scan_soft_gate_when_fail_on_findings_false(
      self,
      mock_which,
      mock_merge_db,
      mock_run_cmd,
      mock_post_status,
  ):
    """Test aggregate pipeline posts success commit status on PR scan when CODEMENDER_FAIL_ON_FINDINGS=false."""
    mock_which.return_value = "/bin/cm"
    mock_default = MagicMock()
    mock_default.stdout = ""
    mock_default.returncode = 0
    mock_run_cmd.return_value = mock_default

    transit_base = os.path.join(self.workspace_dir, ".codemender_transit", "base")
    os.makedirs(transit_base, exist_ok=True)
    with open(os.path.join(transit_base, "manifest.json"), "w") as f:
      json.dump({"findings_count": 1, "target_sha": "def456sha"}, f)

    db_dir = os.path.join(self.workspace_dir, ".codemender")
    os.makedirs(db_dir, exist_ok=True)
    base_db = os.path.join(db_dir, "state.db")
    self.create_test_db(
        base_db,
        [{"finding_id": "fid-1", "title": "SQL Injection", "status": "FIXED", "updated_at": "2026-08-01"}],
    )

    tarball_file = os.path.join(transit_base, "workspace_base.tar.gz")
    with tarfile.open(tarball_file, "w:gz") as tar:
      tar.add(db_dir, arcname=".codemender")

    shard_dir = os.path.join(self.workspace_dir, ".codemender_transit", "shards", "worker_0")
    os.makedirs(shard_dir, exist_ok=True)
    worker_db = os.path.join(shard_dir, "worker_0_state.db")
    self.create_test_db(
        worker_db,
        [{"finding_id": "fid-1", "title": "SQL Injection", "status": "FIXED", "updated_at": "2026-08-02"}],
    )

    with patch.dict(
        os.environ,
        {
            "CODEMENDER_STORAGE_MODE": "github_actions",
            "CODEMENDER_IS_PR_SCAN": "true",
            "CODEMENDER_FAIL_ON_FINDINGS": "false",
            "CODEMENDER_TOTAL_WORKERS": "1",
            "GITHUB_TOKEN": "valid-token",
        },
    ):
      run_aggregate_pipeline()

    self.assertTrue(mock_merge_db.called)
    # Verify commit status was posted as success
    mock_post_status.assert_called_once()
    status_kwargs = mock_post_status.call_args.kwargs
    self.assertEqual(status_kwargs["state"], "success")

  @patch("codemender_agent.runners.aggregate.run_command")
  @patch("codemender_agent.runners.aggregate.merge_db")
  @patch("shutil.which")
  def test_aggregate_pipeline_nightly_mode_does_not_fail(
      self,
      mock_which,
      mock_merge_db,
      mock_run_cmd,
  ):
    """Test aggregate pipeline does not fail on Nightly scans even with findings."""
    mock_which.return_value = "/bin/cm"
    mock_default = MagicMock()
    mock_default.stdout = ""
    mock_default.returncode = 0
    mock_run_cmd.return_value = mock_default

    transit_base = os.path.join(self.workspace_dir, ".codemender_transit", "base")
    os.makedirs(transit_base, exist_ok=True)
    with open(os.path.join(transit_base, "manifest.json"), "w") as f:
      json.dump({"findings_count": 1, "target_sha": "def456sha"}, f)

    db_dir = os.path.join(self.workspace_dir, ".codemender")
    os.makedirs(db_dir, exist_ok=True)
    base_db = os.path.join(db_dir, "state.db")
    self.create_test_db(
        base_db,
        [{"finding_id": "fid-1", "title": "SQL Injection", "status": "DETECTED", "updated_at": "2026-08-01"}],
    )

    tarball_file = os.path.join(transit_base, "workspace_base.tar.gz")
    with tarfile.open(tarball_file, "w:gz") as tar:
      tar.add(db_dir, arcname=".codemender")

    shard_dir = os.path.join(self.workspace_dir, ".codemender_transit", "shards", "worker_0")
    os.makedirs(shard_dir, exist_ok=True)
    worker_db = os.path.join(shard_dir, "worker_0_state.db")
    self.create_test_db(
        worker_db,
        [{"finding_id": "fid-1", "title": "SQL Injection", "status": "FIXED", "updated_at": "2026-08-02"}],
    )

    with patch.dict(
        os.environ,
        {
            "CODEMENDER_STORAGE_MODE": "github_actions",
            "CODEMENDER_IS_PR_SCAN": "false",
            "CODEMENDER_TOTAL_WORKERS": "1",
        },
    ):
      run_aggregate_pipeline()

    self.assertTrue(mock_merge_db.called)

  def test_render_step_summary_pr_scan_omits_pre_existing_findings(self):
    """Verify that _render_step_summary omits PRE_EXISTING_IGNORED findings on PR scans."""
    from codemender_agent.config import OrchestratorConfig

    db_dir = os.path.join(self.workspace_dir, "test_pr_summary")
    os.makedirs(db_dir, exist_ok=True)
    base_db = os.path.join(db_dir, "state.db")
    self.create_test_db(
        base_db,
        [
            {"finding_id": "fid-1", "title": "SQL Injection in PR diff", "status": "FIXED", "updated_at": "2026-08-01"},
            {"finding_id": "fid-2", "title": "Pre-existing XSS", "status": "PRE_EXISTING_IGNORED", "updated_at": "2026-08-01"},
            {"finding_id": "fid-3", "title": "Dismissed Finding", "status": "DISMISSED", "updated_at": "2026-08-01"},
        ],
    )

    summary_file = os.path.join(self.workspace_dir, "pr_step_summary.md")
    cfg = OrchestratorConfig(is_pr_scan=True, github_step_summary=summary_file)
    summary_md, count = _render_step_summary(
        base_db,
        cfg,
        owner="ilbzzz",
        repo_name="juice-shop-local",
        target_sha="1e677199",
    )

    self.assertEqual(count, 1)
    self.assertIn("Pull Request Scan (Clean as You Code)", summary_md)
    self.assertIn("Security Gate Status: FAILED", summary_md)
    # Total should reflect ONLY the 1 PR-scoped finding
    self.assertIn("| 1 | 1 | 0 | 0 | 0 | 0 |", summary_md)
    # fid-1 should be listed in the table
    self.assertIn("`fid-1`", summary_md)
    # fid-2 and fid-3 should NOT be in the table
    self.assertNotIn("`fid-2`", summary_md)
    self.assertNotIn("`fid-3`", summary_md)
    self.assertNotIn("Pre-existing XSS", summary_md)


if __name__ == "__main__":
  unittest.main()
