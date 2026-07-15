#!/usr/bin/env python3
"""Unit tests for CodeMender Orchestrator."""

import json
import os
import unittest
from unittest.mock import MagicMock, patch
import orchestrator
import requests


class TestOrchestrator(unittest.TestCase):

  def test_enforce_https_url(self):
    """Verify that SSH git repository URLs are correctly converted to HTTPS."""
    test_cases = [
        (
            "git@github.com:my-org/my-repo.git",
            "https://github.com/my-org/my-repo.git",
        ),
        (
            "ssh://git@github.com/my-org/my-repo.git",
            "https://github.com/my-org/my-repo.git",
        ),
        ("git@github.corp.com:org/repo", "https://github.corp.com/org/repo"),
        ("https://github.com/org/repo.git", "https://github.com/org/repo.git"),
    ]
    for url, expected in test_cases:
      with self.subTest(url=url):
        self.assertEqual(orchestrator.enforce_https_url(url), expected)

  def test_sanitize_git_url(self):
    """Verify embedded credentials are removed from Git URLs."""
    test_cases = [
        (
            "https://github.com/my-org/my-repo.git",
            "https://github.com/my-org/my-repo.git",
        ),
        (
            "https://x-access-token:token123@github.com/my-org/my-repo.git",
            "https://github.com/my-org/my-repo.git",
        ),
        (
            "http://user:pass@github.corp.com:8443/org/repo",
            "http://github.corp.com:8443/org/repo",
        ),
        ("git@github.com:org/repo.git", "https://github.com/org/repo.git"),
    ]
    for url, expected in test_cases:
      with self.subTest(url=url):
        self.assertEqual(orchestrator.sanitize_git_url(url), expected)

  def test_credential_scrubbing(self):
    """Verify sensitive credentials are scrubbed from environment variables."""
    test_env = {
        "PATH": "/usr/bin",
        "GITHUB_APP_TOKEN": "secret_app_token",
        "GITHUB_PAT": "secret_pat",
        "GITHUB_TOKEN": "secret_token",
        "GH_TOKEN": "secret_gh_token",
        "GITHUB_SECRET": "secret_github",
        "CUSTOM_VAR": "keep_me",
    }
    with patch.dict(os.environ, test_env, clear=True):
      scrubbed = orchestrator.get_scrubbed_env()
      self.assertIn("PATH", scrubbed)
      self.assertIn("CUSTOM_VAR", scrubbed)
      self.assertNotIn("GITHUB_APP_TOKEN", scrubbed)
      self.assertNotIn("GITHUB_PAT", scrubbed)
      self.assertNotIn("GITHUB_TOKEN", scrubbed)
      self.assertNotIn("GH_TOKEN", scrubbed)
      self.assertNotIn("GITHUB_SECRET", scrubbed)

  def test_parse_repo_owner_and_name(self):
    """Test parsing owner and repo from HTTPS, SSH, and authenticated URLs."""
    test_cases = [
        ("https://github.com/my-org/my-repo.git", ("my-org", "my-repo")),
        (
            "https://x-access-token:tok@github.com/my-org/my-repo",
            ("my-org", "my-repo"),
        ),
        ("git@github.com:my-org/my-repo.git", ("my-org", "my-repo")),
        ("https://github.com/user/project/", ("user", "project")),
    ]
    for url, expected in test_cases:
      with self.subTest(url=url):
        owner, repo = orchestrator.parse_repo_owner_and_name(url)
        self.assertEqual((owner, repo), expected)

  def test_generate_branch_name(self):
    """Test branch name generation and hashing."""
    branch1 = orchestrator.generate_branch_name(
        "XXE Vulnerability", "src/parser.py"
    )
    branch2 = orchestrator.generate_branch_name(
        "XXE Vulnerability", "src/parser.py"
    )
    branch3 = orchestrator.generate_branch_name(
        "XXE Vulnerability", "src/other.py"
    )

    self.assertTrue(branch1.startswith("codemender/fix-xxe-vulnerability-"))
    self.assertEqual(branch1, branch2)  # Idempotency check
    self.assertNotEqual(
        branch1, branch3
    )  # Different files get different hashes

  def test_parse_findings_json(self):
    """Test findings JSON parsing, handling array, dict wrappers, and empty strings."""
    raw_json = json.dumps([{
        "FindingID": "12345678-1234-1234-1234-123456789012",
        "VulnType": "SQL Injection",
        "FilePath": "app/db.py",
        "DismissReason": "",  # Empty string state
        "ConfidenceLevel": "HIGH",
    }])

    findings = orchestrator.parse_findings_json(raw_json)
    self.assertEqual(len(findings), 1)
    self.assertEqual(
        findings[0]["FindingID"], "12345678-1234-1234-1234-123456789012"
    )
    self.assertIsNone(
        findings[0]["DismissReason"]
    )  # Converted empty string to None
    self.assertEqual(findings[0]["ConfidenceLevel"], "HIGH")

  def test_parse_findings_json_polluted(self):
    """Verify robust JSON extraction from stdout containing log pollution."""
    polluted_json = (
        "WARNING: backend connection is degraded (retrying...)\n"
        "[\n"
        "  {\n"
        '    "FindingID": "123",\n'
        '    "VulnType": "SQLi",\n'
        '    "FilePath": "a.py"\n'
        "  }\n"
        "]\n"
        "INFO: Serialized 1 findings."
    )
    findings = orchestrator.parse_findings_json(polluted_json)
    self.assertEqual(len(findings), 1)
    self.assertEqual(findings[0]["FindingID"], "123")
    self.assertEqual(findings[0]["VulnType"], "SQLi")
    self.assertEqual(findings[0]["FilePath"], "a.py")

  @patch("requests.get")
  def test_check_remote_branch_exists(self, mock_get):
    """Test branch existence check via GitHub API."""
    mock_resp_exists = MagicMock()
    mock_resp_exists.status_code = 200
    mock_get.return_value = mock_resp_exists

    exists = orchestrator.check_remote_branch_exists(
        "https://github.com/org/repo.git",
        "fake_token",
        "codemender/fix-sqli-12345678",
    )
    self.assertTrue(exists)

    mock_resp_not_found = MagicMock()
    mock_resp_not_found.status_code = 404
    mock_get.return_value = mock_resp_not_found

    exists = orchestrator.check_remote_branch_exists(
        "https://github.com/org/repo.git",
        "fake_token",
        "codemender/fix-sqli-99999999",
    )
    self.assertFalse(exists)

  @patch("requests.post")
  def test_create_pull_request(self, mock_post):
    """Test Pull Request creation via GitHub API."""
    mock_resp = MagicMock()
    mock_resp.status_code = 201
    mock_resp.json.return_value = {
        "html_url": "https://github.com/org/repo/pull/42"
    }
    mock_post.return_value = mock_resp

    pr_url = orchestrator.create_pull_request(
        token="fake_token",
        owner="org",
        repo="repo",
        title="fix(security): resolve SQLi",
        body="Fix details",
        head_branch="codemender/fix-sqli-123",
        base_branch="main",
    )

    self.assertEqual(pr_url, "https://github.com/org/repo/pull/42")
    mock_post.assert_called_once()

  @patch("requests.post")
  def test_create_pull_request_already_exists(self, mock_post):
    """Verify that HTTP 422 'pull request already exists' is intercepted gracefully."""
    mock_resp = MagicMock()
    mock_resp.status_code = 422
    mock_resp.json.return_value = {
        "message": "Validation Failed",
        "errors": [{
            "resource": "PullRequest",
            "code": "custom",
            "message": (
                "A pull request already exists for my-org:codemender/fix-sqli."
            ),
        }],
    }
    mock_post.return_value = mock_resp

    pr_url = orchestrator.create_pull_request(
        token="fake_token",
        owner="org",
        repo="repo",
        title="fix(security): resolve SQLi",
        body="Fix details",
        head_branch="codemender/fix-sqli",
        base_branch="main",
    )

    self.assertEqual(pr_url, "EXISTING_PR")
    mock_post.assert_called_once()

  @patch("requests.get")
  @patch("time.sleep")  # Avoid delay in tests
  def test_retry_on_exception_decorator(self, mock_sleep, mock_get):
    """Verify that the custom retry decorator retries on requests exceptions."""
    mock_resp = MagicMock()
    # Simulate two transient failures (500 Internal Error) then a success
    mock_resp.raise_for_status.side_effect = [
        requests.exceptions.HTTPError("500 Server Error"),
        requests.exceptions.HTTPError("500 Server Error"),
        None,
    ]
    mock_resp.status_code = 200
    mock_resp.json.return_value = {"default_branch": "prod"}
    mock_get.return_value = mock_resp

    branch = orchestrator.get_default_branch("fake_token", "org", "repo")
    self.assertEqual(branch, "prod")
    self.assertEqual(mock_get.call_count, 3)
    self.assertEqual(mock_sleep.call_count, 2)


if __name__ == "__main__":
  unittest.main()
