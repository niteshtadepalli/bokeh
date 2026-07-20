"""Unit tests for codemender_agent.config module."""

import io
import os
import tempfile
import unittest
from unittest.mock import MagicMock, patch

from codemender_agent.config import get_github_credentials, get_scrubbed_env, inject_codemender_config
import yaml


class TestConfig(unittest.TestCase):

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
      scrubbed = get_scrubbed_env()
      self.assertIn("PATH", scrubbed)
      self.assertIn("CUSTOM_VAR", scrubbed)
      self.assertNotIn("GITHUB_APP_TOKEN", scrubbed)
      self.assertNotIn("GITHUB_PAT", scrubbed)
      self.assertNotIn("GITHUB_TOKEN", scrubbed)
      self.assertNotIn("GH_TOKEN", scrubbed)
      self.assertNotIn("GITHUB_SECRET", scrubbed)

  def test_get_github_credentials_success(self):
    """Verify credentials extraction from environment."""
    test_env = {
        "GITHUB_REPO_URL": "https://github.com/my-org/my-repo.git",
        "GITHUB_TOKEN": "valid_token",
    }
    with patch.dict(os.environ, test_env, clear=True):
      repo_url, token = get_github_credentials()
      self.assertEqual(repo_url, "https://github.com/my-org/my-repo.git")
      self.assertEqual(token, "valid_token")

  def test_inject_codemender_config_repo_level(self):
    """Verify repository-level .codemender.yaml overrides build command."""
    with tempfile.TemporaryDirectory() as temp_home:
      with tempfile.TemporaryDirectory() as repo_dir:
        local_config = {
            "build": {"command": "npm run test:security"},
            "scan": {"paths": ["src/"]},
        }
        with open(os.path.join(repo_dir, ".codemender.yaml"), "w") as f:
          yaml.dump(local_config, f)

        with patch("os.path.expanduser", return_value=temp_home):
          inject_codemender_config(repo_dir)

          out_config = os.path.join(temp_home, ".codemender", "config.yaml")
          self.assertTrue(os.path.exists(out_config))

          with open(out_config, "r") as f:
            data = yaml.safe_load(f)

          self.assertEqual(data["build"]["command"], "npm run test:security")
          self.assertFalse(data["tools"]["confirm_commands"])


if __name__ == "__main__":
  unittest.main()
