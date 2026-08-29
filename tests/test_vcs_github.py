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

"""Unit tests for codemender_agent.vcs.github module."""

import unittest
from unittest.mock import MagicMock, patch

from codemender_agent.vcs.github import check_remote_branch_exists, create_pull_request, get_default_branch


class TestVcsGithub(unittest.TestCase):

  @patch("codemender_agent.vcs.github._get_branch_via_api")
  def test_check_remote_branch_exists_true(self, mock_api):
    """Verify branch check returns True when GitHub API finds the branch."""
    mock_api.return_value = True
    exists = check_remote_branch_exists(
        "https://github.com/org/repo.git", "fake_token", "feature-branch"
    )
    self.assertTrue(exists)
    mock_api.assert_called_once_with(
        "org", "repo", "feature-branch", "fake_token"
    )

  @patch("requests.post")
  def test_create_pull_request_success(self, mock_post):
    """Verify successful Pull Request creation."""
    mock_resp = MagicMock()
    mock_resp.status_code = 201
    mock_resp.json.return_value = {
        "html_url": "https://github.com/org/repo/pull/42"
    }
    mock_post.return_value = mock_resp

    pr_url = create_pull_request(
        token="token",
        owner="org",
        repo="repo",
        title="Fix SQLi",
        body="Details",
        head_branch="codemender/fix-sqli",
        base_branch="main",
    )
    self.assertEqual(pr_url, "https://github.com/org/repo/pull/42")

  @patch("requests.get")
  def test_get_default_branch_api_success(self, mock_get):
    """Verify fetching default branch via GitHub REST API."""
    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.json.return_value = {"default_branch": "development"}
    mock_get.return_value = mock_resp

    branch = get_default_branch("token", "org", "repo")
    self.assertEqual(branch, "development")

  @patch("requests.post")
  def test_create_pr_comment_success(self, mock_post):
    """Verify posting review comment on PRs."""
    from codemender_agent.vcs.github import create_pr_comment

    mock_resp = MagicMock()
    mock_resp.status_code = 201
    mock_resp.json.return_value = {
        "html_url": "https://github.com/org/repo/pull/42#issuecomment-1"
    }
    mock_post.return_value = mock_resp

    comment_url = create_pr_comment(
        token="valid-token",
        owner="org",
        repo="repo",
        pr_number=42,
        body="## Security Fix Proposal",
    )
    self.assertEqual(
        comment_url, "https://github.com/org/repo/pull/42#issuecomment-1"
    )
    mock_post.assert_called_once()

  @patch("requests.get")
  def test_is_duplicate_pr_with_head_branch(self, mock_get):
    """Verify targeted O(1) duplicate PR lookup with head_branch parameter."""
    from codemender_agent.vcs.github import is_duplicate_pr

    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.json.return_value = [
        {"title": "Fix SQL Injection", "state": "open"}
    ]
    mock_get.return_value = mock_resp

    is_dup = is_duplicate_pr(
        repo_url="https://github.com/org/repo.git",
        token="token",
        file_path="db.py",
        vuln_type="SQL_INJECTION",
        start_line=10,
        head_branch="codemender/fix-sql_injection-abc12345",
    )
    self.assertTrue(is_dup)
    mock_get.assert_called_once()
    call_args = mock_get.call_args
    self.assertIn("head=org:codemender/fix-sql_injection-abc12345", call_args[0][0])

  @patch("requests.post")
  def test_post_commit_status_success(self, mock_post):
    """Verify posting a commit status check on a PR commit SHA."""
    from codemender_agent.vcs.github import post_commit_status

    mock_resp = MagicMock()
    mock_resp.status_code = 201
    mock_resp.json.return_value = {"state": "failure"}
    mock_post.return_value = mock_resp

    success = post_commit_status(
        token="valid-token",
        owner="org",
        repo="repo",
        sha="abcdef123456",
        state="failure",
        description="Security Gate FAILED: 1 vulnerability detected",
        context="CodeMender / Security Gate",
        target_url="https://github.com/org/repo/pull/42",
    )
    self.assertTrue(success)
    mock_post.assert_called_once()
    url = mock_post.call_args[0][0]
    payload = mock_post.call_args[1]["json"]
    self.assertIn("/repos/org/repo/statuses/abcdef123456", url)
    self.assertEqual(payload["state"], "failure")
    self.assertEqual(payload["context"], "CodeMender / Security Gate")
    self.assertEqual(payload["target_url"], "https://github.com/org/repo/pull/42")

  def test_post_commit_status_fake_token(self):
    """Verify post_commit_status handles fake-token gracefully in unit tests."""
    from codemender_agent.vcs.github import post_commit_status

    success = post_commit_status(
        token="fake-token",
        owner="org",
        repo="repo",
        sha="abcdef123456",
        state="success",
        description="Security Gate PASSED",
    )
    self.assertTrue(success)

  @patch("requests.post")
  def test_post_commit_status_api_error_returns_false(self, mock_post):
    """Verify post_commit_status returns False on network error without throwing."""
    import requests
    from codemender_agent.vcs.github import post_commit_status

    mock_post.side_effect = requests.exceptions.RequestException("Connection error")

    success = post_commit_status(
        token="valid-token",
        owner="org",
        repo="repo",
        sha="abcdef123456",
        state="failure",
        description="Security Gate FAILED",
    )
    self.assertFalse(success)

  def test_delete_remote_branch_safety_guard_rejects_main(self):
    """Verify delete_remote_branch strictly rejects non-codemender branches."""
    from codemender_agent.vcs.github import delete_remote_branch

    # Refuse to delete protected or non-codemender branches
    self.assertFalse(
        delete_remote_branch("https://github.com/org/repo.git", "token", "main")
    )
    self.assertFalse(
        delete_remote_branch("https://github.com/org/repo.git", "token", "master")
    )
    self.assertFalse(
        delete_remote_branch("https://github.com/org/repo.git", "token", "feature/my-branch")
    )

  @patch("requests.delete")
  def test_delete_remote_branch_api_success(self, mock_delete):
    """Verify deleting branch via GitHub REST API with 204 status."""
    from codemender_agent.vcs.github import delete_remote_branch

    mock_resp = MagicMock()
    mock_resp.status_code = 204
    mock_delete.return_value = mock_resp

    success = delete_remote_branch(
        repo_url="https://github.com/org/repo.git",
        token="valid-token",
        branch_name="codemender/fix-sqli-abc12345",
    )
    self.assertTrue(success)
    mock_delete.assert_called_once()
    self.assertIn(
        "/repos/org/repo/git/refs/heads/codemender/fix-sqli-abc12345",
        mock_delete.call_args[0][0],
    )

  @patch("requests.delete")
  def test_delete_remote_branch_api_404_idempotent(self, mock_delete):
    """Verify deleting already-deleted branch returns True idempotently on 404/422."""
    from codemender_agent.vcs.github import delete_remote_branch

    mock_resp = MagicMock()
    mock_resp.status_code = 404
    mock_delete.return_value = mock_resp

    success = delete_remote_branch(
        repo_url="https://github.com/org/repo.git",
        token="valid-token",
        branch_name="codemender/fix-sqli-abc12345",
    )
    self.assertTrue(success)

  @patch("codemender_agent.vcs.github.run_command")
  @patch("requests.delete")
  def test_delete_remote_branch_api_fail_falls_back_to_cli(
      self, mock_delete, mock_run_command
  ):
    """Verify falling back to Git CLI push --delete if REST API raises error."""
    import requests
    from codemender_agent.vcs.github import delete_remote_branch

    mock_delete.side_effect = requests.exceptions.RequestException("API error")
    mock_cli_res = MagicMock()
    mock_cli_res.returncode = 0
    mock_run_command.return_value = mock_cli_res

    success = delete_remote_branch(
        repo_url="https://github.com/org/repo.git",
        token="valid-token",
        branch_name="codemender/fix-sqli-abc12345",
    )
    self.assertTrue(success)
    mock_run_command.assert_called_once()
    cmd = mock_run_command.call_args[0][0]
    self.assertIn("--delete", cmd)
    self.assertIn("codemender/fix-sqli-abc12345", cmd)


if __name__ == "__main__":
  unittest.main()
