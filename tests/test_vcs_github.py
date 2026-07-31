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


if __name__ == "__main__":
  unittest.main()
