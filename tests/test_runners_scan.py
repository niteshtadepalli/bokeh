"""Unit tests for Stage 1 Scan runner."""

import json
import os
import tempfile
import unittest
from unittest.mock import MagicMock, patch

from codemender_agent.runners.scan import run_scan_pipeline


class TestScanRunner(unittest.TestCase):

  def setUp(self):
    self.temp_dir = tempfile.TemporaryDirectory()
    self.workspace_dir = self.temp_dir.name

    # Setup common env vars
    self.env_patcher = patch.dict(
        os.environ,
        {
            "CODEMENDER_SCAN_ID": "test-scan-123",
            "CODEMENDER_GCS_BUCKET": "test-bucket",
            "WORKSPACE_DIR": self.workspace_dir,
            "GITHUB_REPO_URL": "https://github.com/owner/repo.git",
            "GITHUB_TOKEN": "fake-token",
            "CODEMENDER_SCAN_TARGET": ".",
            "CODEMENDER_BUILD_COMMAND": "echo 'build'",
        },
    )
    self.env_patcher.start()

  def tearDown(self):
    self.env_patcher.stop()
    self.temp_dir.cleanup()

  @patch("codemender_agent.runners.scan.generate_signed_url")
  @patch("codemender_agent.runners.scan.run_command")
  @patch("codemender_agent.runners.scan.upload_file_to_gcs")
  @patch("codemender_agent.runners.scan.check_remote_branch_exists")
  @patch("codemender_agent.runners.scan.get_default_branch")
  @patch("codemender_agent.runners.scan.make_tarfile")
  @patch("shutil.which")
  def test_scan_pipeline_success(
      self,
      mock_which,
      _mock_make_tarfile,
      mock_get_default_branch,
      mock_check_remote_branch_exists,
      mock_upload_gcs,
      mock_run_cmd,
      mock_generate_signed_url,
  ):
    mock_which.return_value = "/bin/cm"
    mock_get_default_branch.return_value = "main"
    mock_check_remote_branch_exists.return_value = False

    # Mock git rev-parse HEAD
    mock_git_rev = MagicMock()
    mock_git_rev.stdout = "abc123commitsha"

    # Mock cm report --format json
    mock_cm_report = MagicMock()
    mock_cm_report.stdout = json.dumps([
        {
            "FindingID": "fid-1",
            "Status": "DETECTED",
            "VulnType": "SQL_INJECTION",
            "FilePath": "db.py",
        },
        {
            "FindingID": "fid-2",
            "Status": "DETECTED",
            "VulnType": "XSS",
            "FilePath": "app.py",
        },
    ])

    # Mock other commands
    mock_default = MagicMock()
    mock_default.stdout = ""

    def run_cmd_side_effect(cmd, *_args, **_kwargs):
      cmd_str = " ".join(cmd)
      if "rev-parse" in cmd_str:
        return mock_git_rev
      elif "report" in cmd_str:
        return mock_cm_report
      else:
        return mock_default

    mock_generate_signed_url.side_effect = (
        lambda bucket, blob, method="GET", **kwargs: (
            f"http://signed-url/{blob}?method={method}"
        )
    )
    mock_run_cmd.side_effect = run_cmd_side_effect
    mock_upload_gcs.return_value = True

    run_scan_pipeline()

    # Verify manifest was uploaded
    manifest_uploaded = False
    partitions_uploaded = 0
    workspace_uploaded = False

    for call in mock_upload_gcs.call_args_list:
      local_path, bucket, dest_blob = call[0]
      self.assertEqual(bucket, "test-bucket")
      if "manifest.json" in dest_blob:
        manifest_uploaded = True
        with open(local_path, "r") as f:
          manifest_data = json.load(f)
          self.assertEqual(manifest_data["findings_count"], 2)
          self.assertEqual(manifest_data["target_sha"], "abc123commitsha")
          self.assertEqual(
              manifest_data["base_workspace_url"],
              "http://signed-url/scans/test-scan-123/workspace_base.tar.gz"
              "?method=GET",
          )
          self.assertEqual(
              manifest_data["partition_urls"],
              [
                  "http://signed-url/scans/test-scan-123/partition_0.json"
                  "?method=GET",
                  "http://signed-url/scans/test-scan-123/partition_1.json"
                  "?method=GET",
              ],
          )
          self.assertEqual(
              manifest_data["upload_urls"],
              [
                  "http://signed-url/scans/test-scan-123/worker_0_state.db"
                  "?method=PUT",
                  "http://signed-url/scans/test-scan-123/worker_1_state.db"
                  "?method=PUT",
              ],
          )
      elif "partition_" in dest_blob:
        partitions_uploaded += 1
        with open(local_path, "r") as f:
          part_data = json.load(f)
          self.assertIn("partition_index", part_data)
          self.assertIn("finding_ids", part_data)
      elif "workspace_base.tar.gz" in dest_blob:
        workspace_uploaded = True

    self.assertTrue(manifest_uploaded)
    self.assertTrue(workspace_uploaded)
    self.assertEqual(partitions_uploaded, 2)

  @patch("codemender_agent.runners.scan.run_command")
  @patch("codemender_agent.runners.scan.upload_file_to_gcs")
  @patch("codemender_agent.runners.scan.get_default_branch")
  @patch("shutil.which")
  def test_scan_pipeline_zero_findings(
      self,
      mock_which,
      mock_get_default_branch,
      mock_upload_gcs,
      mock_run_cmd,
  ):
    mock_which.return_value = "/bin/cm"
    mock_get_default_branch.return_value = "main"

    mock_git_rev = MagicMock()
    mock_git_rev.stdout = "abc123commitsha"

    mock_cm_report = MagicMock()
    mock_cm_report.stdout = "[]"

    mock_default = MagicMock()
    mock_default.stdout = ""

    def run_cmd_side_effect(cmd, *_args, **_kwargs):
      cmd_str = " ".join(cmd)
      if "rev-parse" in cmd_str:
        return mock_git_rev
      elif "report" in cmd_str:
        return mock_cm_report
      else:
        return mock_default

    mock_run_cmd.side_effect = run_cmd_side_effect
    mock_upload_gcs.return_value = True

    with self.assertRaises(SystemExit) as cm:
      run_scan_pipeline()

    self.assertEqual(cm.exception.code, 0)

    manifest_uploaded = False
    for call in mock_upload_gcs.call_args_list:
      local_path, _, dest_blob = call[0]
      if "manifest.json" in dest_blob:
        manifest_uploaded = True
        with open(local_path, "r") as f:
          manifest_data = json.load(f)
          self.assertEqual(manifest_data["findings_count"], 0)
          self.assertEqual(manifest_data["target_sha"], "abc123commitsha")

    self.assertTrue(manifest_uploaded)


if __name__ == "__main__":
  unittest.main()
