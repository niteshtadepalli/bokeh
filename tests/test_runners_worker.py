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

"""Unit tests for Stage 2 Worker runner."""

import json
import os
import tempfile
import unittest
import unittest.mock

from codemender_agent.runners.worker import run_worker_pipeline


class TestWorkerRunner(unittest.TestCase):

  def setUp(self):
    self.temp_dir = tempfile.TemporaryDirectory()
    self.workspace_dir = self.temp_dir.name

    self.env_patcher = unittest.mock.patch.dict(
        os.environ,
        {
            "CODEMENDER_WORKER_INDEX": "0",
            "CODEMENDER_BASE_WORKSPACE_URL": "http://signed-url/base.tar.gz",
            "CODEMENDER_PARTITION_URLS": json.dumps(["http://signed-url/partition_0.json"]),
            "CODEMENDER_UPLOAD_URLS": json.dumps(["http://signed-url/upload_0.db"]),
            "WORKSPACE_DIR": self.workspace_dir,
            "GITHUB_REPO_URL": "https://github.com/owner/repo.git",
            "GITHUB_TOKEN": "fake-token",
            "CODEMENDER_TARGET_SHA": "abc123commitsha",
            "CODEMENDER_BUILD_COMMAND": "echo 'build'",
        },
    )
    self.env_patcher.start()

  def tearDown(self):
    self.env_patcher.stop()
    self.temp_dir.cleanup()

  @unittest.mock.patch("codemender_agent.runners.worker.run_command")
  @unittest.mock.patch("codemender_agent.runners.worker.download_from_url")
  @unittest.mock.patch("codemender_agent.runners.worker.upload_to_url")
  @unittest.mock.patch("codemender_agent.runners.worker.check_remote_branch_exists")
  @unittest.mock.patch("codemender_agent.runners.worker.create_pull_request")
  @unittest.mock.patch("codemender_agent.runners.worker.is_duplicate_pr")
  @unittest.mock.patch("codemender_agent.runners.worker.is_finding_verified")
  @unittest.mock.patch("codemender_agent.runners.worker.get_finding_status")
  @unittest.mock.patch("tarfile.open")
  @unittest.mock.patch("shutil.which")
  def test_worker_pipeline_success(
      self,
      mock_which,
      _mock_tarfile_open,
      mock_get_finding_status,
      mock_is_finding_verified,
      mock_is_duplicate_pr,
      mock_create_pr,
      mock_check_remote_branch_exists,
      mock_upload_to_url,
      mock_download_from_url,
      mock_run_cmd,
  ):
    mock_which.return_value = "/bin/cm"
    mock_check_remote_branch_exists.return_value = False
    mock_is_duplicate_pr.return_value = False
    mock_is_finding_verified.return_value = True
    mock_get_finding_status.return_value = "FIXED"
    mock_create_pr.return_value = True

    def download_side_effect(url, dest_path):
      if "partition" in url:
        with open(dest_path, "w") as f:
          json.dump({"partition_index": 0, "finding_ids": ["fid-1"]}, f)
        return True
      elif "base.tar.gz" in url:
        return True
      return False

    mock_download_from_url.side_effect = download_side_effect
    mock_upload_to_url.return_value = True

    mock_cm_report = unittest.mock.MagicMock()
    mock_cm_report.stdout = json.dumps([
        {
            "FindingID": "fid-1",
            "Status": "DETECTED",
            "VulnType": "SQL_INJECTION",
            "FilePath": "db.py",
            "Title": "SQL Injection in db.py",
            "Severity": "HIGH",
            "Analysis": "Fix it.",
        }
    ])
    mock_cm_report.returncode = 0

    mock_git_status = unittest.mock.MagicMock()
    mock_git_status.stdout = " M db.py"
    mock_git_status.returncode = 0

    mock_default = unittest.mock.MagicMock()
    mock_default.stdout = ""
    mock_default.returncode = 0

    def run_cmd_side_effect(cmd, *_args, **_kwargs):
      cmd_str = " ".join(cmd)
      if "report" in cmd_str:
        return mock_cm_report
      elif "status" in cmd_str:
        return mock_git_status
      else:
        return mock_default

    mock_run_cmd.side_effect = run_cmd_side_effect

    run_worker_pipeline()

    mock_download_from_url.assert_any_call("http://signed-url/base.tar.gz", os.path.join(self.workspace_dir, "workspace_base.tar.gz"))
    mock_download_from_url.assert_any_call("http://signed-url/partition_0.json", os.path.join(self.workspace_dir, "partition_0.json"))

    mock_run_cmd.assert_any_call(["git", "checkout", "-f", "abc123commitsha"], cwd=os.path.join(self.workspace_dir, "repo"))

    verify_called = False
    fix_called = False
    for call in mock_run_cmd.call_args_list:
      cmd = call[0][0]
      cmd_str = " ".join(cmd)
      if "find" in cmd_str and "verify" in cmd_str and "fid-1" in cmd_str:
        verify_called = True
      elif "fix" in cmd_str and "fid-1" in cmd_str:
        fix_called = True

    self.assertTrue(verify_called)
    self.assertTrue(fix_called)
    mock_create_pr.assert_called_once()
    mock_upload_to_url.assert_called_once_with(
        os.path.expanduser("~/.codemender/state.db"),
        "http://signed-url/upload_0.db"
    )

  @unittest.mock.patch("codemender_agent.runners.worker.run_command")
  @unittest.mock.patch("codemender_agent.runners.worker.download_from_url")
  @unittest.mock.patch("codemender_agent.runners.worker.upload_to_url")
  @unittest.mock.patch("codemender_agent.runners.worker.check_remote_branch_exists")
  @unittest.mock.patch("codemender_agent.runners.worker.create_pull_request")
  @unittest.mock.patch("codemender_agent.runners.worker.is_duplicate_pr")
  @unittest.mock.patch("codemender_agent.runners.worker.is_finding_verified")
  @unittest.mock.patch("codemender_agent.runners.worker.get_finding_status")
  @unittest.mock.patch("tarfile.open")
  @unittest.mock.patch("shutil.which")
  def test_worker_pipeline_idempotency_skip(
      self,
      mock_which,
      _mock_tarfile_open,
      mock_get_finding_status,
      mock_is_finding_verified,
      mock_is_duplicate_pr,
      mock_create_pr,
      mock_check_remote_branch_exists,
      mock_upload_to_url,
      mock_download_from_url,
      mock_run_cmd,
  ):
    del mock_get_finding_status
    mock_which.return_value = "/bin/cm"
    mock_check_remote_branch_exists.return_value = True
    mock_is_duplicate_pr.return_value = False
    mock_is_finding_verified.return_value = False
    mock_create_pr.return_value = True

    def download_side_effect(url, dest_path):
      if "partition" in url:
        with open(dest_path, "w") as f:
          json.dump({"partition_index": 0, "finding_ids": ["fid-1"]}, f)
        return True
      elif "base.tar.gz" in url:
        return True
      return False

    mock_download_from_url.side_effect = download_side_effect
    mock_upload_to_url.return_value = True

    mock_cm_report = unittest.mock.MagicMock()
    mock_cm_report.stdout = json.dumps([
        {
            "FindingID": "fid-1",
            "Status": "DETECTED",
            "VulnType": "SQL_INJECTION",
            "FilePath": "db.py",
            "Title": "SQL Injection in db.py",
            "Severity": "HIGH",
            "Analysis": "Fix it.",
        }
    ])
    mock_cm_report.returncode = 0

    mock_default = unittest.mock.MagicMock()
    mock_default.stdout = ""
    mock_default.returncode = 0

    def run_cmd_side_effect(cmd, *_args, **_kwargs):
      cmd_str = " ".join(cmd)
      if "report" in cmd_str:
        return mock_cm_report
      else:
        return mock_default

    mock_run_cmd.side_effect = run_cmd_side_effect

    run_worker_pipeline()

    mock_check_remote_branch_exists.assert_called_once()
    
    checkout_remote_called = False
    for call in mock_run_cmd.call_args_list:
      cmd = call[0][0]
      if len(cmd) >= 3 and cmd[0] == "git" and cmd[1] == "checkout" and "codemender/fix-" in cmd[2]:
        checkout_remote_called = True
    self.assertFalse(checkout_remote_called)

    fix_called = False
    for call in mock_run_cmd.call_args_list:
      cmd = call[0][0]
      if "fix" in cmd:
        fix_called = True
    self.assertFalse(fix_called)

    mock_create_pr.assert_not_called()
    mock_upload_to_url.assert_called_once()


if __name__ == "__main__":
  unittest.main()

