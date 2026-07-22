"""Unit tests for codemender_agent.vcs.git module."""

import base64
import os
import tempfile
import unittest

from codemender_agent.vcs.git import (
    enforce_https_url,
    generate_branch_name,
    get_git_auth_header,
    parse_repo_owner_and_name,
    sanitize_git_url,
    setup_local_git_excludes,
)



class TestVcsGit(unittest.TestCase):

  def test_get_git_auth_header(self):
    """Verify the generation of HTTP Basic auth header for Git."""
    token = "fake_token"
    header = get_git_auth_header(token)
    expected_token_b64 = base64.b64encode(b"x-access-token:fake_token").decode(
        "utf-8"
    )
    self.assertEqual(
        header, f"http.extraheader=AUTHORIZATION: Basic {expected_token_b64}"
    )

  def test_enforce_https_url(self):
    """Verify SSH git repository URLs are correctly converted to HTTPS."""
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
        self.assertEqual(enforce_https_url(url), expected)

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
        self.assertEqual(sanitize_git_url(url), expected)

  def test_parse_repo_owner_and_name(self):
    """Verify parsing owner and repo name from GitHub URLs."""
    owner, repo = parse_repo_owner_and_name(
        "https://github.com/my-org/my-repo.git"
    )
    self.assertEqual(owner, "my-org")
    self.assertEqual(repo, "my-repo")

  def test_generate_branch_name(self):
    """Verify creation of idempotent branch names."""
    branch = generate_branch_name("SQL Injection", "abcdef1234567890")
    self.assertEqual(branch, "codemender/fix-sql-injection-abcdef12")



  def test_setup_local_git_excludes(self):
    """Verify local git excludes are correctly appended without duplicates."""
    with tempfile.TemporaryDirectory() as repo_dir:
      git_info_dir = os.path.join(repo_dir, ".git", "info")
      os.makedirs(git_info_dir, exist_ok=True)
      exclude_path = os.path.join(git_info_dir, "exclude")

      setup_local_git_excludes(repo_dir)

      self.assertTrue(os.path.exists(exclude_path))
      with open(exclude_path, "r") as f:
        content = f.read()

      self.assertIn(".cm_project", content)
      self.assertIn(".exploit", content)


if __name__ == "__main__":
  unittest.main()
