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

"""Unit tests for codemender_agent.config module."""

import os
import tempfile
import unittest
import unittest.mock

from codemender_agent.config import get_cleanup_ports
from codemender_agent.config import get_github_credentials
from codemender_agent.config import get_scrubbed_env
from codemender_agent.config import inject_codemender_config
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
    with unittest.mock.patch.dict(os.environ, test_env, clear=True):
      scrubbed = get_scrubbed_env()
      self.assertIn("PATH", scrubbed)
      self.assertIn("CUSTOM_VAR", scrubbed)
      self.assertNotIn("GITHUB_APP_TOKEN", scrubbed)
      self.assertNotIn("GITHUB_PAT", scrubbed)
      self.assertNotIn("GITHUB_TOKEN", scrubbed)
      self.assertNotIn("GH_TOKEN", scrubbed)
      self.assertNotIn("GITHUB_SECRET", scrubbed)

  def test_get_scrubbed_env_with_repo_dir(self):
    """Verify get_scrubbed_env creates and configures local .codemender_cache paths."""
    with tempfile.TemporaryDirectory() as repo_dir:
      scrubbed = get_scrubbed_env(repo_dir=repo_dir)
      expected_cache = os.path.join(repo_dir, ".codemender_cache")
      self.assertEqual(scrubbed.get("XDG_CACHE_HOME"), expected_cache)
      self.assertEqual(scrubbed.get("npm_config_cache"), os.path.join(expected_cache, "npm"))
      self.assertEqual(scrubbed.get("TMPDIR"), os.path.join(expected_cache, "tmp"))
      self.assertEqual(scrubbed.get("PIP_CACHE_DIR"), os.path.join(expected_cache, "pip"))
      self.assertTrue(os.path.isdir(os.path.join(expected_cache, "tmp")))
      self.assertTrue(os.path.isdir(os.path.join(expected_cache, "npm")))
      self.assertTrue(os.path.isdir(os.path.join(expected_cache, "pip")))

  def test_get_github_credentials_success(self):
    """Verify credentials extraction from environment."""
    test_env = {
        "GITHUB_REPO_URL": "https://github.com/my-org/my-repo.git",
        "GITHUB_TOKEN": "valid_token",
    }
    with unittest.mock.patch.dict(os.environ, test_env, clear=True):
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

        with unittest.mock.patch("os.path.expanduser", return_value=temp_home):
          inject_codemender_config(repo_dir)

          out_config = os.path.join(temp_home, ".codemender", "config.yaml")
          self.assertTrue(os.path.exists(out_config))

          with open(out_config, "r") as f:
            data = yaml.safe_load(f)

          self.assertEqual(data["build"]["command"], "npm run test:security")
          self.assertFalse(data["tools"]["confirm_commands"])

  def test_get_cleanup_ports_default(self):
    """Verify get_cleanup_ports returns default list when env is unset."""
    with unittest.mock.patch.dict(os.environ, {}, clear=True):
      ports = get_cleanup_ports()
      self.assertEqual(ports, [3000, 3001, 5000, 8000, 8080, 8081, 9000])

  def test_get_cleanup_ports_override(self):
    """Verify get_cleanup_ports parses comma-separated override list."""
    test_env = {"CODEMENDER_CLEANUP_PORTS": "3000, 8080, 9999"}
    with unittest.mock.patch.dict(os.environ, test_env, clear=True):
      ports = get_cleanup_ports()
      self.assertEqual(ports, [3000, 8080, 9999])

  def test_get_cleanup_ports_invalid_fallback(self):
    """Verify get_cleanup_ports falls back to default on parse errors."""
    test_env = {"CODEMENDER_CLEANUP_PORTS": "3000, abc, 9999"}
    with unittest.mock.patch.dict(os.environ, test_env, clear=True):
      ports = get_cleanup_ports()
      self.assertEqual(ports, [3000, 3001, 5000, 8000, 8080, 8081, 9000])





  def test_inject_codemender_config_sandbox(self):
    """Verify sandbox settings and project_paths normalization in config.yaml."""
    with tempfile.TemporaryDirectory() as temp_home:
      with tempfile.TemporaryDirectory() as repo_dir:
        local_config = {
            "build": {"command": "npm test"},
            "project_paths": ["src", "routes", os.path.abspath("/custom/abs/path")],
        }
        with open(os.path.join(repo_dir, ".codemender.yaml"), "w") as f:
          yaml.dump(local_config, f)

        with unittest.mock.patch("os.path.expanduser", return_value=temp_home):
          inject_codemender_config(repo_dir)

          out_config = os.path.join(temp_home, ".codemender", "config.yaml")
          self.assertTrue(os.path.exists(out_config))

          with open(out_config, "r") as f:
            data = yaml.safe_load(f)

          # Verify sandbox defaults
          self.assertIn("sandbox", data)
          self.assertTrue(data["sandbox"]["enabled"])
          self.assertEqual(data["sandbox"]["mounts"]["target_dir"], os.path.abspath(repo_dir))
          self.assertEqual(data["sandbox"]["network"]["profile"], "permissive-open")

          # Verify project_paths are all absolute
          self.assertIn("project_paths", data)
          for p in data["project_paths"]:
            self.assertTrue(os.path.isabs(p), f"Path {p} is not absolute")
          self.assertIn(os.path.abspath(os.path.join(repo_dir, "src")), data["project_paths"])
          self.assertIn(os.path.abspath(os.path.join(repo_dir, "routes")), data["project_paths"])
          self.assertIn(os.path.abspath("/custom/abs/path"), data["project_paths"])

  def test_detect_build_command_nodejs(self):
    """Verify detect_build_command finds npm test in package.json."""
    from codemender_agent.config import detect_build_command
    with tempfile.TemporaryDirectory() as repo_dir:
      with open(os.path.join(repo_dir, "package.json"), "w") as f:
        f.write('{"name": "test-pkg", "scripts": {"test": "mocha"}}')
      self.assertEqual(detect_build_command(repo_dir), "npm test")

  def test_detect_build_command_python(self):
    """Verify detect_build_command finds pytest when pyproject.toml exists."""
    from codemender_agent.config import detect_build_command
    with tempfile.TemporaryDirectory() as repo_dir:
      with open(os.path.join(repo_dir, "pyproject.toml"), "w") as f:
        f.write("[tool.pytest]")
      self.assertEqual(detect_build_command(repo_dir), "pytest")

  def test_detect_build_command_go(self):
    """Verify detect_build_command finds go test when go.mod exists."""
    from codemender_agent.config import detect_build_command
    with tempfile.TemporaryDirectory() as repo_dir:
      with open(os.path.join(repo_dir, "go.mod"), "w") as f:
        f.write("module example.com/test")
      self.assertEqual(detect_build_command(repo_dir), "go test ./...")

  def test_inject_codemender_config_auto_detects_build_command(self):
    """Verify inject_codemender_config auto-detects build command if unset."""
    with tempfile.TemporaryDirectory() as temp_home:
      with tempfile.TemporaryDirectory() as repo_dir:
        with open(os.path.join(repo_dir, "package.json"), "w") as f:
          f.write('{"scripts": {"test": "jest"}}')
        with unittest.mock.patch("os.path.expanduser", return_value=temp_home):
          inject_codemender_config(repo_dir)
          out_config = os.path.join(temp_home, ".codemender", "config.yaml")
          with open(out_config, "r") as f:
            data = yaml.safe_load(f)
          self.assertEqual(data["build"]["command"], "npm test")

  def test_inject_codemender_config_env_override_takes_precedence(self):
    """Verify env override CODEMENDER_BUILD_COMMAND takes precedence over repo config."""
    from codemender_agent.config import OrchestratorConfig
    with tempfile.TemporaryDirectory() as temp_home:
      with tempfile.TemporaryDirectory() as repo_dir:
        local_config = {"build": {"command": "npm test"}}
        with open(os.path.join(repo_dir, ".codemender.yaml"), "w") as f:
          yaml.dump(local_config, f)
        test_env = {"CODEMENDER_BUILD_COMMAND": "npm run custom:test"}
        with unittest.mock.patch.dict(os.environ, test_env, clear=True):
          cfg = OrchestratorConfig.from_env()
          with unittest.mock.patch("os.path.expanduser", return_value=temp_home):
            inject_codemender_config(repo_dir, config=cfg)
            out_config = os.path.join(temp_home, ".codemender", "config.yaml")
            with open(out_config, "r") as f:
              data = yaml.safe_load(f)
            self.assertEqual(data["build"]["command"], "npm run custom:test")

  def test_inject_codemender_config_composite_build_command(self):
    """Verify composite build commands with ampersands are safely preserved."""
    from codemender_agent.config import OrchestratorConfig
    with tempfile.TemporaryDirectory() as temp_home:
      with tempfile.TemporaryDirectory() as repo_dir:
        test_env = {"CODEMENDER_BUILD_COMMAND": "npm install && npm test"}
        with unittest.mock.patch.dict(os.environ, test_env, clear=True):
          cfg = OrchestratorConfig.from_env()
          with unittest.mock.patch("os.path.expanduser", return_value=temp_home):
            inject_codemender_config(repo_dir, config=cfg)
            out_config = os.path.join(temp_home, ".codemender", "config.yaml")
            with open(out_config, "r") as f:
              data = yaml.safe_load(f)
            self.assertEqual(data["build"]["command"], "npm install && npm test")

  def test_orchestrator_config_skip_verify_default(self):
    """Verify skip_verify defaults to True when CODEMENDER_SKIP_VERIFY is unset."""
    from codemender_agent.config import OrchestratorConfig
    with unittest.mock.patch.dict(os.environ, {}, clear=True):
      cfg = OrchestratorConfig.from_env()
      self.assertTrue(cfg.skip_verify)

  def test_orchestrator_config_skip_verify_false(self):
    """Verify skip_verify is False when CODEMENDER_SKIP_VERIFY is false/0/no."""
    from codemender_agent.config import OrchestratorConfig
    for false_val in ("false", "0", "no"):
      with unittest.mock.patch.dict(
          os.environ, {"CODEMENDER_SKIP_VERIFY": false_val}, clear=True
      ):
        cfg = OrchestratorConfig.from_env()
        self.assertFalse(
            cfg.skip_verify,
            f"Expected False for CODEMENDER_SKIP_VERIFY={false_val}",
        )

  def test_orchestrator_config_skip_verify_true(self):
    """Verify skip_verify is True when CODEMENDER_SKIP_VERIFY is true/1/yes."""
    from codemender_agent.config import OrchestratorConfig
    for true_val in ("true", "1", "yes"):
      with unittest.mock.patch.dict(
          os.environ, {"CODEMENDER_SKIP_VERIFY": true_val}, clear=True
      ):
        cfg = OrchestratorConfig.from_env()
        self.assertTrue(
            cfg.skip_verify,
            f"Expected True for CODEMENDER_SKIP_VERIFY={true_val}",
        )


if __name__ == "__main__":
  unittest.main()
