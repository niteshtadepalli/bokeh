#!/usr/bin/env python3
"""Unit tests for CodeMender Orchestrator."""

import base64
import io
import json
import os
import sqlite3
import sys
import tempfile
import unittest
from unittest.mock import MagicMock, mock_open, patch
import orchestrator
import requests
import yaml


class MockFile(io.StringIO):

  def __enter__(self):
    return self

  def __exit__(self, exc_type, exc_val, exc_tb):
    pass


class TestOrchestrator(unittest.TestCase):

  def test_get_git_auth_header(self):
    """Verify the generation of HTTP Basic auth header for Git."""
    token = "fake_token"
    header = orchestrator.get_git_auth_header(token)

    # Expecting: http.extraheader=AUTHORIZATION: Basic <base64(x-access-token:fake_token)>
    expected_token_b64 = base64.b64encode(b"x-access-token:fake_token").decode(
        "utf-8"
    )
    self.assertEqual(
        header, f"http.extraheader=AUTHORIZATION: Basic {expected_token_b64}"
    )

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
    """Verify embedded credentials and parameters are removed from Git URLs."""
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
            "http://user:pass@github.corp.com:8443/org/repo?branch=main#readme",
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

  def test_parse_findings_json_polluted_brackets(self):
    """Verify robust JSON extraction from stdout when trailing logs contain brackets."""
    polluted_json = (
        "[\n"
        "  {\n"
        '    "FindingID": "19855282-768a-42b6-b386-0057eb99940a",\n'
        '    "VulnType": "XXE",\n'
        '    "FilePath": "lib/xml.ts"\n'
        "  }\n"
        "]\n"
        "2026-07-16T00:08:15Z [INFO]  📄 Session log: pending.log"
    )
    findings = orchestrator.parse_findings_json(polluted_json)
    self.assertEqual(len(findings), 1)
    self.assertEqual(
        findings[0]["FindingID"], "19855282-768a-42b6-b386-0057eb99940a"
    )
    self.assertEqual(findings[0]["VulnType"], "XXE")
    self.assertEqual(findings[0]["FilePath"], "lib/xml.ts")

  def test_extract_session_id_success(self):
    """Verify session UUID is correctly extracted from cm find stdout."""
    find_output = (
        "🔍 Discovering files in /workspace/juice-shop...\n🚀 Starting FIND"
        " session (mode: SCAN)...\n   Server: codemender_prod\n   Session:"
        " f7f7b492-3564-4dc0-bc8f-2020554ebe24\n   Operation:"
        " sessions/f7f7b492-3564-4dc0-bc8f-2020554ebe24/operations/ad76b900\n"
    )
    session_id = orchestrator.extract_session_id(find_output)
    self.assertEqual(session_id, "f7f7b492-3564-4dc0-bc8f-2020554ebe24")

  def test_extract_session_id_missing(self):
    """Verify None is returned when session UUID is not present in output."""
    find_output = (
        "🔍 Discovering files in /workspace/juice-shop...\n"
        "No vulnerabilities found!\n"
    )
    session_id = orchestrator.extract_session_id(find_output)
    self.assertIsNone(session_id)

  def test_is_finding_verified(self):
    """Test is_finding_verified helper with different database states."""
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as tmp:
      tmp_db_path = tmp.name

    try:
      # Initialize schema and insert test rows
      conn = sqlite3.connect(tmp_db_path)
      cursor = conn.cursor()
      cursor.execute("""
        CREATE TABLE findings (
          finding_id TEXT PRIMARY KEY,
          status TEXT
        )
      """)
      cursor.executemany(
          "INSERT INTO findings (finding_id, status) VALUES (?, ?)",
          [
              ("finding-1", "VERIFIED"),
              ("finding-2", "EXPLOIT_FAILED"),
              ("finding-3", "OPEN"),
          ],
      )
      conn.commit()
      conn.close()

      # Run assertions
      self.assertTrue(
          orchestrator.is_finding_verified(tmp_db_path, "finding-1")
      )
      self.assertFalse(
          orchestrator.is_finding_verified(tmp_db_path, "finding-2")
      )
      self.assertFalse(
          orchestrator.is_finding_verified(tmp_db_path, "finding-3")
      )
      self.assertFalse(
          orchestrator.is_finding_verified(tmp_db_path, "non-existent")
      )
      self.assertFalse(
          orchestrator.is_finding_verified("missing_file.db", "finding-1")
      )

    finally:
      if os.path.exists(tmp_db_path):
        os.remove(tmp_db_path)

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

  @patch("os.path.exists")
  @patch("os.makedirs")
  @patch("sys.stdin.isatty")
  def test_inject_codemender_config_env_only(
      self, mock_isatty, mock_makedirs, mock_exists
  ):
    """Test configuration injection using only environment variables."""
    mock_exists.side_effect = lambda path: (
        ".codemender/config.yaml" in path or path.endswith(".codemender")
    )
    mock_isatty.return_value = False

    test_env = {
        "CODEMENDER_BUILD_COMMAND": "npm test",
        "CODEMENDER_VCS_TYPE": "git",
    }

    default_config = yaml.safe_dump({
        "build": {"command": ""},
        "vcs": {"type": "", "commands": {"reset": ""}},
        "tools": {"confirm_commands": True, "confirm_writes": True},
    })

    written_data = MockFile()

    def mock_open_fn(_path, mode="r", *_args, **_kwargs):
      if "w" in mode:
        return written_data
      return MockFile(default_config)

    with (
        patch.dict(os.environ, test_env, clear=True),
        patch("builtins.open", mock_open_fn),
    ):

      orchestrator.inject_codemender_config("/dummy/repo")

    self.assertGreater(len(written_data.getvalue()), 0)
    merged_config = yaml.safe_load(written_data.getvalue())

    self.assertEqual(merged_config["build"]["command"], "npm test")
    self.assertEqual(merged_config["vcs"]["type"], "git")
    self.assertEqual(merged_config["project_paths"], ["/dummy/repo"])
    self.assertFalse(merged_config["tools"]["confirm_commands"])
    self.assertFalse(merged_config["tools"]["confirm_writes"])
    mock_makedirs.assert_called()

  @patch("os.path.exists")
  @patch("os.makedirs")
  @patch("sys.stdin.isatty")
  def test_inject_codemender_config_env_quotes_stripped(
      self, mock_isatty, mock_makedirs, mock_exists
  ):
    """Verify surrounding single/double quotes in build command are stripped."""
    mock_exists.side_effect = lambda path: (
        ".codemender/config.yaml" in path or path.endswith(".codemender")
    )
    mock_isatty.return_value = False

    test_env = {
        "CODEMENDER_BUILD_COMMAND": "'npm install && npm test'",
        "CODEMENDER_VCS_TYPE": "git",
    }

    default_config = yaml.safe_dump({
        "build": {"command": ""},
        "vcs": {"type": "", "commands": {"reset": ""}},
        "tools": {"confirm_commands": True, "confirm_writes": True},
    })

    written_data = MockFile()

    def mock_open_fn(_path, mode="r", *_args, **_kwargs):
      if "w" in mode:
        return written_data
      return MockFile(default_config)

    with (
        patch.dict(os.environ, test_env, clear=True),
        patch("builtins.open", mock_open_fn),
    ):
      orchestrator.inject_codemender_config("/dummy/repo")

    self.assertGreater(len(written_data.getvalue()), 0)
    merged_config = yaml.safe_load(written_data.getvalue())

    self.assertEqual(
        merged_config["build"]["command"], "npm install && npm test"
    )
    mock_makedirs.assert_called()

  @patch("os.path.exists")
  @patch("os.makedirs")
  @patch("sys.stdin.isatty")
  def test_inject_codemender_config_repo_precedence(
      self, mock_isatty, mock_makedirs, mock_exists
  ):
    """Verify repository-level .codemender.yaml overrides environment variables."""
    mock_exists.side_effect = (
        lambda path: ".codemender.yaml" in path
        or "config.yaml" in path
        or path.endswith(".codemender")
    )
    mock_isatty.return_value = False

    test_env = {
        "CODEMENDER_BUILD_COMMAND": "env_build_cmd",
    }

    local_config = yaml.safe_dump({
        "build": {"command": "mvn clean test"},
        "vcs": {"type": "custom", "commands": {"reset": "./reset.sh"}},
        "project_paths": ["services/user"],
    })

    default_config = yaml.safe_dump({
        "build": {"command": ""},
        "vcs": {"type": "", "commands": {"reset": ""}},
        "tools": {"confirm_commands": True, "confirm_writes": True},
    })

    written_data = MockFile()

    def mock_open_fn(path, mode="r", *_args, **_kwargs):
      if ".codemender.yaml" in path:
        return MockFile(local_config)
      if "w" in mode:
        return written_data
      return MockFile(default_config)

    with (
        patch.dict(os.environ, test_env, clear=True),
        patch("builtins.open", mock_open_fn),
    ):

      orchestrator.inject_codemender_config("/dummy/repo")

    self.assertGreater(len(written_data.getvalue()), 0)
    merged_config = yaml.safe_load(written_data.getvalue())

    self.assertEqual(merged_config["build"]["command"], "mvn clean test")
    self.assertEqual(merged_config["vcs"]["type"], "custom")
    self.assertEqual(merged_config["vcs"]["commands"]["reset"], "./reset.sh")
    self.assertEqual(merged_config["project_paths"], ["services/user"])
    mock_makedirs.assert_called()

  @patch("os.path.exists")
  @patch("os.makedirs")
  @patch("sys.stdin.isatty")
  @patch("builtins.input")
  def test_inject_codemender_config_interactive_prompt(
      self, mock_input, mock_isatty, mock_makedirs, mock_exists
  ):
    """Verify manual interactive fallback prompt works when sys.stdin is a TTY."""
    mock_exists.side_effect = (
        lambda path: "config.yaml" in path or path.endswith(".codemender")
    )
    mock_isatty.return_value = True
    mock_input.return_value = "interactive_npm_test"

    default_config = yaml.safe_dump({
        "build": {"command": ""},
        "vcs": {"type": "", "commands": {"reset": ""}},
        "tools": {"confirm_commands": True, "confirm_writes": True},
    })

    written_data = MockFile()

    def mock_open_fn(_path, mode="r", *_args, **_kwargs):
      if "w" in mode:
        return written_data
      return MockFile(default_config)

    with (
        patch.dict(os.environ, {}, clear=True),
        patch("builtins.open", mock_open_fn),
    ):

      orchestrator.inject_codemender_config("/dummy/repo")

    self.assertGreater(len(written_data.getvalue()), 0)
    merged_config = yaml.safe_load(written_data.getvalue())

    self.assertEqual(merged_config["build"]["command"], "interactive_npm_test")
    mock_makedirs.assert_called()


if __name__ == "__main__":
  unittest.main()
