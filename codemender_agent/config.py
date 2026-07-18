"""Configuration injection and environment security module for CodeMender Agent."""

import logging
import os
import sys
from typing import Dict, Tuple

import yaml

logger = logging.getLogger("codemender-orchestrator")

SENSITIVE_ENV_VARS = [
    "GITHUB_APP_TOKEN",
    "GITHUB_PAT",
    "GITHUB_TOKEN",
    "GH_TOKEN",
    "GITHUB_SECRET",
]


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
    sys.exit(1)

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
    sys.exit(1)

  return repo_url, token


def inject_codemender_config(repo_dir: str) -> None:
  """Reads project-level and environment configs and merges them into ~/.codemender/config.yaml."""
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

  # 6. Force disable confirmations for headless execution safety
  if "tools" not in config_data or config_data["tools"] is None:
    config_data["tools"] = {}
  config_data["tools"]["confirm_commands"] = False
  config_data["tools"]["confirm_writes"] = False

  # 7. Save the merged configuration back to ~/.codemender/config.yaml
  try:
    os.makedirs(os.path.dirname(global_config_path), exist_ok=True)
    with open(global_config_path, "w") as f:
      yaml.safe_dump(config_data, f, default_flow_style=False)
    logger.info(
        "Successfully injected configurations into %s", global_config_path
    )
  except Exception as e:
    logger.error("Failed to write global config.yaml: %s", e)
