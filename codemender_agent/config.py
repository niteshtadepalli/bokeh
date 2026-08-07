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

"""Configuration injection and environment security module for CodeMender Agent."""

import logging
import os
import sys
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

import yaml

logger = logging.getLogger("codemender-orchestrator")

SENSITIVE_ENV_VARS = [
    "GITHUB_APP_TOKEN",
    "GITHUB_PAT",
    "GITHUB_TOKEN",
    "GH_TOKEN",
    "GITHUB_SECRET",
]


@dataclass(frozen=True)
class OrchestratorConfig:
  """Centralized immutable configuration for the CodeMender Orchestrator."""

  cli_version: str = "preview"
  model: Optional[str] = None
  find_model: Optional[str] = None
  verify_model: Optional[str] = None
  fix_model: Optional[str] = None
  skip_exploit_verification: bool = False
  scan_id: Optional[str] = None
  gcs_bucket: Optional[str] = None
  report_bucket: Optional[str] = None
  repo_url: Optional[str] = None
  github_token: Optional[str] = None
  build_command: Optional[str] = None
  scan_target: str = "."
  max_tasks: int = 20
  force_overwrite: bool = False
  cleanup_ports: List[int] = field(
      default_factory=lambda: [3000, 3001, 5000, 8000, 8080, 8081, 9000]
  )

  @classmethod
  def from_env(cls) -> "OrchestratorConfig":
    """Loads configuration from environment variables safely in one place."""
    cli_version = os.environ.get("CODEMENDER_CLI_VERSION", "preview").lower()
    model = os.environ.get("CODEMENDER_MODEL")
    find_model = os.environ.get("CODEMENDER_FIND_MODEL") or model
    verify_model = os.environ.get("CODEMENDER_VERIFY_MODEL") or model
    fix_model = os.environ.get("CODEMENDER_FIX_MODEL") or model
    skip_exploit = (
        os.environ.get("CODEMENDER_SKIP_EXPLOIT_VERIFICATION", "false").lower()
        == "true"
    )
    scan_id = os.environ.get("CODEMENDER_SCAN_ID")
    gcs_bucket = os.environ.get("CODEMENDER_GCS_BUCKET")
    report_bucket = os.environ.get("CODEMENDER_REPORT_BUCKET") or gcs_bucket
    repo_url = os.environ.get("GITHUB_REPO_URL")
    github_token = (
        os.environ.get("GITHUB_APP_TOKEN")
        or os.environ.get("GITHUB_PAT")
        or os.environ.get("GITHUB_TOKEN")
    )
    build_command = os.environ.get("CODEMENDER_BUILD_COMMAND")
    scan_target = os.environ.get("CODEMENDER_SCAN_TARGET", ".")
    try:
      max_tasks = int(os.environ.get("CODEMENDER_MAX_TASKS", "20"))
    except ValueError:
      max_tasks = 20
    force_overwrite = (
        os.environ.get("CODEMENDER_FORCE_OVERWRITE", "false").lower() == "true"
    )

    ports_env = os.environ.get("CODEMENDER_CLEANUP_PORTS")
    if ports_env:
      try:
        cleanup_ports = [
            int(p.strip()) for p in ports_env.split(",") if p.strip()
        ]
      except ValueError:
        cleanup_ports = [3000, 3001, 5000, 8000, 8080, 8081, 9000]
    else:
      cleanup_ports = [3000, 3001, 5000, 8000, 8080, 8081, 9000]

    return cls(
        cli_version=cli_version,
        model=model,
        find_model=find_model,
        verify_model=verify_model,
        fix_model=fix_model,
        skip_exploit_verification=skip_exploit,
        scan_id=scan_id,
        gcs_bucket=gcs_bucket,
        report_bucket=report_bucket,
        repo_url=repo_url,
        github_token=github_token,
        build_command=build_command,
        scan_target=scan_target,
        max_tasks=max_tasks,
        force_overwrite=force_overwrite,
        cleanup_ports=cleanup_ports,
    )


def get_scrubbed_env() -> Dict[str, str]:
  """Returns a copy of environment variables with sensitive credentials removed.

  This prevents credential exfiltration during untrusted LLM code execution
  inside the child subprocesses.
  """
  env = dict(os.environ)
  for var in SENSITIVE_ENV_VARS:
    if var in env:
      del env[var]
  return env


def get_github_credentials() -> Tuple[str, str]:
  """Retrieves repo URL and GitHub access token from environment variables."""
  repo_url = os.environ.get("GITHUB_REPO_URL")
  if not repo_url:
    logger.error("Environment variable GITHUB_REPO_URL is required.")
    raise ValueError("Environment variable GITHUB_REPO_URL is required.")

  token = (
      os.environ.get("GITHUB_APP_TOKEN")
      or os.environ.get("GITHUB_PAT")
      or os.environ.get("GITHUB_TOKEN")
  )
  if not token:
    logger.error(
        "One of GITHUB_APP_TOKEN, GITHUB_PAT, or GITHUB_TOKEN environment"
        " variables is required."
    )
    raise ValueError(
        "One of GITHUB_APP_TOKEN, GITHUB_PAT, or GITHUB_TOKEN environment"
        " variables is required."
    )

  return repo_url.strip(), token.strip()


def inject_codemender_config(repo_dir: str) -> None:
  """Reads project-level and environment configs and merges them into ~/.codemender/config.yaml."""
  _ = os.environ.get("CODEMENDER_CLI_VERSION", "preview").lower()
  home_dir = os.path.expanduser("~")
  global_config_path = os.path.join(home_dir, ".codemender", "config.yaml")

  # 1. Load global default config created by 'cm init'
  if os.path.exists(global_config_path):
    try:
      with open(global_config_path, "r") as f:
        config_data = yaml.safe_load(f) or {}
    except Exception as e:
      logger.warning("Could not read global config.yaml: %s. Re-creating.", e)
      config_data = {}
  else:
    config_data = {}

  # Initialize keys if missing
  if "build" not in config_data or config_data["build"] is None:
    config_data["build"] = {}
  if "vcs" not in config_data or config_data["vcs"] is None:
    config_data["vcs"] = {}
  if (
      "commands" not in config_data["vcs"]
      or config_data["vcs"]["commands"] is None
  ):
    config_data["vcs"]["commands"] = {}

  # 2. Defaults (VCS is always git inside the orchestrator clone)
  config_data["vcs"]["type"] = "git"

  # Set default project path to the repository directory to restrict agent scope
  if not config_data.get("project_paths"):
    config_data["project_paths"] = [os.path.abspath(repo_dir)]

  # 3. Read Environment Variable configurations (A: Env Overrides)
  env_build_cmd = os.environ.get("CODEMENDER_BUILD_COMMAND")
  if env_build_cmd:
    clean_build_cmd = env_build_cmd.strip().strip("'\"")
    logger.info(
        "Applying env override CODEMENDER_BUILD_COMMAND: %s", clean_build_cmd
    )
    config_data["build"]["command"] = clean_build_cmd

  env_model = os.environ.get("CODEMENDER_MODEL")
  if env_model:
    config_data["model"] = env_model.strip()

  # 4. Read Repository-Level config (B: Config-as-Code - takes precedence)
  project_config = None
  for filename in [
      ".codemender.yaml",
      "codemender.yaml",
      ".codemender.yml",
      "codemender.yml",
  ]:
    local_path = os.path.join(repo_dir, filename)
    if os.path.exists(local_path):
      try:
        with open(local_path, "r") as f:
          project_config = yaml.safe_load(f)
        logger.info("Found repository-level configuration: %s", filename)
        break
      except Exception as e:
        logger.warning("Failed to parse local config file %s: %s", filename, e)

  if project_config and isinstance(project_config, dict):
    if "build" in project_config and isinstance(project_config["build"], dict):
      config_data["build"].update(project_config["build"])
    if "vcs" in project_config and isinstance(project_config["vcs"], dict):
      if "commands" in project_config["vcs"] and isinstance(
          project_config["vcs"]["commands"], dict
      ):
        config_data["vcs"]["commands"].update(project_config["vcs"]["commands"])
      for k, v in project_config["vcs"].items():
        if k != "commands":
          config_data["vcs"][k] = v
    for key in ["scan", "project_paths", "output", "tools"]:
      if key in project_config:
        config_data[key] = project_config[key]

  # 5. Interactive Terminal Prompt Fallback
  if not config_data["build"].get("command"):
    if sys.stdin.isatty():
      try:
        prompt_cmd = input(
            "\n⚠️  No build command configured for this project.\nPlease enter"
            " the build/test command (e.g., 'npm test') or press Enter to"
            " skip: "
        ).strip()
        if prompt_cmd:
          config_data["build"]["command"] = prompt_cmd
      except (KeyboardInterrupt, EOFError):
        logger.warning("\nPrompt interrupted. Skipping build command.")
    else:
      logger.warning(
          "No build command configured (running in headless environment)."
          " Post-fix verification will be skipped."
      )

  # 6. Force disable confirmations for headless execution safety (AFTER repository config merge)
  if "tools" not in config_data or config_data["tools"] is None:
    config_data["tools"] = {}
  config_data["tools"]["confirm_commands"] = False
  config_data["tools"]["confirm_writes"] = False

  # 7. Save the merged configuration back to ~/.codemender/config.yaml using atomic rename
  try:
    os.makedirs(os.path.dirname(global_config_path), exist_ok=True)
    tmp_config_path = global_config_path + ".tmp"
    with open(tmp_config_path, "w") as f:
      yaml.safe_dump(config_data, f, default_flow_style=False)
    os.replace(tmp_config_path, global_config_path)
    logger.info(
        "Successfully injected configurations into %s", global_config_path
    )
  except Exception as e:
    logger.error("Failed to write global config.yaml: %s", e)


def get_cleanup_ports() -> List[int]:
  """Retrieves the list of ports to free before verification tasks."""
  return OrchestratorConfig.from_env().cleanup_ports


