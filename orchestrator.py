#!/usr/bin/env python3
"""CodeMender Orchestrator.

Decentralized, automated orchestration script that runs within a team's secure
infrastructure (e.g., Cloud Run Job) to validate and fix security
vulnerabilities using the CodeMender CLI and open GitHub Pull Requests.
"""

import base64
from functools import wraps
import hashlib
import json
import logging
import os
import re
import shutil
import subprocess
import sys
import time
from typing import Any, Dict, List, Optional, Tuple
import requests
import yaml

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)
logger = logging.getLogger("codemender-orchestrator")

SENSITIVE_ENV_VARS = [
    "GITHUB_APP_TOKEN",
    "GITHUB_PAT",
    "GITHUB_TOKEN",
    "GH_TOKEN",
    "GITHUB_SECRET",
]


def retry_on_exception(max_tries=3, initial_delay=1, backoff_factor=2):
  """Decorator to retry transient network/command errors with exponential backoff."""

  def decorator(func):
    @wraps(func)
    def wrapper(*args, **kwargs):
      delay = initial_delay
      for attempt in range(1, max_tries + 1):
        try:
          return func(*args, **kwargs)
        except (requests.RequestException, subprocess.CalledProcessError) as e:
          if attempt == max_tries:
            raise
          logger.warning(
              "Attempt %d failed for %s: %s. Retrying in %d seconds...",
              attempt,
              func.__name__,
              e,
              delay,
          )
          time.sleep(delay)
          delay *= backoff_factor
      return None

    return wrapper

  return decorator


def enforce_https_url(url: str) -> str:
  """Converts SSH git URLs to HTTPS format to support token-based authentication."""
  url = url.strip()
  if url.startswith("git@") or url.startswith("ssh://"):
    # Match git@host:owner/repo.git or ssh://git@host/owner/repo.git
    match = re.search(r"(?:ssh://)?git@([^:/]+)[:/](.+)$", url)
    if match:
      host = match.group(1)
      path = match.group(2)
      return f"https://{host}/{path}"
  return url


def sanitize_git_url(url: str) -> str:
  """Removes embedded credentials and query parameters from Git URLs."""
  # Enforce HTTPS format
  cleaned = enforce_https_url(url)
  # Strip username/password credentials
  cleaned = re.sub(r"(https?://)[^@]+@", r"\1", cleaned)
  # Strip query parameters or fragment identifiers (?branch=main or #readme)
  cleaned = cleaned.split("?")[0].split("#")[0].strip()
  return cleaned


def get_git_auth_header(token: str) -> str:
  """Generates a Basic authentication header value for GitHub Git transactions."""
  auth_str = f"x-access-token:{token}"
  auth_b64 = base64.b64encode(auth_str.encode("utf-8")).decode("utf-8")
  return f"http.extraheader=AUTHORIZATION: Basic {auth_b64}"


def get_scrubbed_env() -> Dict[str, str]:
  """Returns a copy of environment variables with sensitive credentials removed.

  This prevents credential exfiltration during untrusted LLM code execution
  inside the child subprocesses.
  """
  # TODO: Improve credential scrubbing to detect and remove custom tokens,
  # keys, and tokens embedded in GITHUB_REPO_URL.
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


def parse_repo_owner_and_name(repo_url: str) -> Tuple[str, str]:
  """Extracts (owner, repo_name) from a GitHub repository URL."""
  clean_url = sanitize_git_url(repo_url).rstrip("/")
  if clean_url.endswith(".git"):
    clean_url = clean_url[:-4]

  match = re.search(r"[:/]([^/]+)/([^/]+)$", clean_url)
  if match:
    return match.group(1), match.group(2)
  raise ValueError(f"Could not parse owner and repo name from URL: {repo_url}")


def generate_branch_name(vuln_type: str, file_path: str) -> str:
  """Generates a stable, idempotent Git branch name based on VulnType and FilePath hash."""
  vuln_clean = re.sub(
      r"[^a-zA-Z0-9\-_]", "-", (vuln_type or "vuln").strip().lower()
  )
  path_hash = hashlib.sha256((file_path or "").encode("utf-8")).hexdigest()[:8]
  return f"codemender/fix-{vuln_clean}-{path_hash}"


def run_command(
    cmd: List[str],
    cwd: Optional[str] = None,
    env: Optional[Dict[str, str]] = None,
    check: bool = True,
    capture_stderr: bool = True,
) -> subprocess.CompletedProcess:
  """Executes a subprocess command, streaming stdout/stderr in real-time."""
  # Scrub Authorization headers or token values from logs
  log_cmd_parts = []
  for arg in cmd:
    if "http.extraheader=AUTHORIZATION:" in arg:
      log_cmd_parts.append(
          "git -c http.extraheader=AUTHORIZATION: Basic [REDACTED]"
      )
    else:
      log_cmd_parts.append(arg)

  cmd_str_short = " ".join(log_cmd_parts)
  if len(cmd_str_short) > 80:
    cmd_str_short = cmd_str_short[:77] + "..."

  logger.info("Executing command: %s", " ".join(log_cmd_parts))

  # Start the process with stderr redirected to stdout to stream both
  process = subprocess.Popen(
      cmd,
      cwd=cwd,
      env=env,
      stdin=subprocess.DEVNULL,
      stdout=subprocess.PIPE,
      stderr=subprocess.STDOUT if capture_stderr else sys.stderr,
      text=True,
      bufsize=1,  # Line-buffered
  )

  # Write start delimiter
  sys.stdout.write(f"\n>>> [SUBPROCESS START] {cmd_str_short} >>>\n")
  sys.stdout.flush()

  stdout_lines = []
  # Stream output line-by-line in real-time
  assert process.stdout is not None
  for line in iter(process.stdout.readline, ""):
    sys.stdout.write(line)
    sys.stdout.flush()
    stdout_lines.append(line)

  process.stdout.close()
  return_code = process.wait()
  full_stdout = "".join(stdout_lines)

  # Write end delimiter
  sys.stdout.write(
      f"<<< [SUBPROCESS END] {cmd_str_short} (EXIT: {return_code}) <<<\n\n"
  )
  sys.stdout.flush()

  if check and return_code != 0:
    logger.error("Command failed with code %d", return_code)
    raise subprocess.CalledProcessError(return_code, cmd, full_stdout, "")

  return subprocess.CompletedProcess(cmd, return_code, full_stdout, "")


@retry_on_exception(max_tries=3)
def _get_branch_via_api(
    owner: str, repo: str, branch_name: str, token: str
) -> Optional[bool]:
  """Triggers GitHub branch status checks with raise_for_status validation."""
  headers = {
      "Authorization": f"Bearer {token}",
      "Accept": "application/vnd.github+json",
  }
  api_url = (
      f"https://api.github.com/repos/{owner}/{repo}/branches/{branch_name}"
  )
  resp = requests.get(api_url, headers=headers, timeout=10)
  if resp.status_code == 200:
    return True
  elif resp.status_code == 404:
    return False
  resp.raise_for_status()
  return None


def check_remote_branch_exists(
    repo_url: str, token: str, branch_name: str, cwd: Optional[str] = None
) -> bool:
  """Checks if a branch already exists on the remote repository."""
  sanitized_url = sanitize_git_url(repo_url)
  try:
    owner, repo = parse_repo_owner_and_name(sanitized_url)
    res = _get_branch_via_api(owner, repo, branch_name, token)
    if res is not None:
      return res
  except Exception as e:
    logger.warning(
        "GitHub API branch check failed after retries (%s), falling back to"
        " git ls-remote",
        e,
    )

  # TODO: Avoid passing token in process arguments to prevent exposure in /proc/cmdline
  cmd = [
      "git",
      "-c",
      get_git_auth_header(token),
      "ls-remote",
      "--heads",
      sanitized_url,
      f"refs/heads/{branch_name}",
  ]
  res = run_command(cmd, cwd=cwd, check=True)
  return bool(res.stdout and branch_name in res.stdout)


def parse_findings_json(json_str: str) -> List[Dict[str, Any]]:
  """Parses `cm report --format json` output handling empty strings for optional fields."""
  clean_str = json_str.strip()
  if not clean_str:
    return []

  # Find the start of the JSON array or object
  start_idx = clean_str.find("[")
  if start_idx == -1:
    start_idx = clean_str.find("{")

  if start_idx == -1:
    logger.error("No valid JSON array or object found in report.")
    return []

  try:
    decoder = json.JSONDecoder()
    data, _ = decoder.raw_decode(clean_str, start_idx)
  except json.JSONDecodeError as e:
    logger.error(
        "Failed to parse JSON findings report: %s\nOriginal string: %s",
        e,
        json_str,
    )
    return []

  if isinstance(data, dict):
    findings = data.get("findings", data.get("items", []))
  elif isinstance(data, list):
    findings = data
  else:
    findings = []

  cleaned_findings = []
  for item in findings:
    if not isinstance(item, dict):
      continue
    cleaned = {}
    for k, v in item.items():
      if v == "":
        cleaned[k] = None
      else:
        cleaned[k] = v
    cleaned_findings.append(cleaned)

  return cleaned_findings


@retry_on_exception(max_tries=3)
def _fetch_default_branch_via_api(token: str, owner: str, repo: str) -> str:
  """Queries repository metadata from GitHub with raise_for_status checks."""
  headers = {
      "Authorization": f"Bearer {token}",
      "Accept": "application/vnd.github+json",
  }
  resp = requests.get(
      f"https://api.github.com/repos/{owner}/{repo}",
      headers=headers,
      timeout=10,
  )
  resp.raise_for_status()
  return resp.json().get("default_branch", "main")


def get_default_branch(token: str, owner: str, repo: str) -> str:
  """Gets the default branch name for a repository via GitHub API or defaults to main."""
  try:
    return _fetch_default_branch_via_api(token, owner, repo)
  except Exception as e:
    logger.warning(
        "Could not determine default branch via API after retries: %s", e
    )
  return "main"


@retry_on_exception(max_tries=3)
def create_pull_request(
    token: str,
    owner: str,
    repo: str,
    title: str,
    body: str,
    head_branch: str,
    base_branch: str,
) -> Optional[str]:
  """Creates a Pull Request on GitHub using REST API."""
  url = f"https://api.github.com/repos/{owner}/{repo}/pulls"
  headers = {
      "Authorization": f"Bearer {token}",
      "Accept": "application/vnd.github+json",
  }
  payload = {
      "title": title,
      "body": body,
      "head": head_branch,
      "base": base_branch,
  }
  resp = requests.post(url, headers=headers, json=payload, timeout=15)

  # Gracefully intercept HTTP 422 "PR already exists" validation failure
  if resp.status_code == 422:
    try:
      resp_data = resp.json()
      errors = resp_data.get("errors", [])
      messages = [e.get("message", "") for e in errors if isinstance(e, dict)]
      if any(
          "already exists" in msg for msg in messages
      ) or "already exists" in resp_data.get("message", ""):
        logger.info(
            "A Pull Request already exists on GitHub for branch %s. Skipping PR"
            " creation.",
            head_branch,
        )
        return "EXISTING_PR"
    except Exception:
      pass

  resp.raise_for_status()  # Throws HTTPError to trigger backoff retry decorator
  pr_url = resp.json().get("html_url")
  logger.info("Successfully created Pull Request: %s", pr_url)
  return pr_url


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


def main() -> None:
  """Main execution entrypoint for CodeMender Orchestrator."""
  repo_url, token = get_github_credentials()
  clean_repo_url = sanitize_git_url(repo_url)
  owner, repo_name = parse_repo_owner_and_name(clean_repo_url)
  scrubbed_env = get_scrubbed_env()

  workspace_dir = os.environ.get("WORKSPACE_DIR", os.getcwd())

  # Step 1: Single-Sync Git Rule - clone repository if not present or pull latest
  logger.info("Syncing repository: %s", clean_repo_url)
  repo_dir = os.path.join(workspace_dir, repo_name)

  if not os.path.exists(os.path.join(repo_dir, ".git")):
    clone_cmd = [
        "git",
        "-c",
        get_git_auth_header(token),
        "clone",
        "--depth",
        "1",
        clean_repo_url,
        repo_dir,
    ]
    run_command(clone_cmd, cwd=workspace_dir)
  else:
    logger.info("Repository directory exists, fetching latest state...")
    # Get current checked out branch to fetch correctly
    try:
      curr_branch = run_command(
          ["git", "branch", "--show-current"], cwd=repo_dir
      ).stdout.strip()
    except Exception:
      curr_branch = "main"
    if not curr_branch:
      curr_branch = "main"

    # Fetch latest state from remote for this specific branch
    fetch_cmd = [
        "git",
        "-c",
        get_git_auth_header(token),
        "fetch",
        "origin",
        curr_branch,
    ]
    run_command(fetch_cmd, cwd=repo_dir)
    # Update local tracking state to match remote head
    run_command(["git", "checkout", "-f", curr_branch], cwd=repo_dir)
    run_command(
        ["git", "reset", "--hard", f"origin/{curr_branch}"], cwd=repo_dir
    )

  # Determine default branch dynamically from current checkout
  try:
    default_branch = run_command(
        ["git", "branch", "--show-current"], cwd=repo_dir
    ).stdout.strip()
  except Exception:
    default_branch = ""
  if not default_branch:
    default_branch = get_default_branch(token, owner, repo_name)

  logger.info("Using default branch: %s", default_branch)
  run_command(["git", "checkout", "-f", default_branch], cwd=repo_dir)

  # Configure Git committer identity locally to prevent commit failures in headless environments
  run_command(["git", "config", "user.name", "CodeMender Agent"], cwd=repo_dir)
  run_command(
      ["git", "config", "user.email", "codemender-agent@google.com"],
      cwd=repo_dir,
  )

  # Step 2: Initialize CodeMender CLI (Fail-fast with clear errors)
  logger.info("Initializing CodeMender CLI...")
  cm_binary = shutil.which("cm") or "cm"

  try:
    run_command(
        [cm_binary, "init"],
        cwd=repo_dir,
        env=scrubbed_env,
        check=True,
    )
    # Inject configurations right here, after cm init creates the config file!
    inject_codemender_config(repo_dir)

    run_command(
        [cm_binary, "init", "--verify"],
        cwd=repo_dir,
        env=scrubbed_env,
        check=True,
    )
  except Exception as e:
    logger.critical(
        "CodeMender initialization failed. Please check if the 'cm' binary is"
        " properly installed, credentials/config are correct, or the backend is"
        " reachable: %s",
        e,
    )
    sys.exit(1)

  # Step 3: Run `cm find .` and generate report (Fail-fast with clear errors)
  logger.info("Scanning codebase for findings...")
  try:
    run_command(
        [cm_binary, "find", "."],
        cwd=repo_dir,
        env=scrubbed_env,
        check=True,
    )
  except Exception as e:
    logger.critical(
        "CodeMender vulnerability scanning failed. Please check the scan path"
        " or network connection to backend: %s",
        e,
    )
    sys.exit(1)

  report_res = run_command(
      [cm_binary, "report", "--format", "json"],
      cwd=repo_dir,
      env=scrubbed_env,
      check=True,
      capture_stderr=False,
  )
  findings = parse_findings_json(report_res.stdout)
  logger.info("Found %d vulnerability finding(s).", len(findings))

  # Step 4: Sequential Verify -> Fix -> Branch -> Push -> PR loop
  for idx, finding in enumerate(findings, start=1):
    finding_id = finding.get("FindingID")
    if not finding_id:
      logger.warning("Finding missing FindingID at index %d, skipping.", idx)
      continue

    # Filter out findings that are already marked as FALSE_POSITIVE or RESOLVED
    status = finding.get("Status")
    if status in ["FALSE_POSITIVE", "RESOLVED"]:
      logger.info(
          "Skipping finding %s because status is %s.", finding_id, status
      )
      continue

    vuln_type = finding.get("VulnType") or "vulnerability"
    file_path = finding.get("FilePath") or "unknown_file"
    title = finding.get("Title") or f"Security Fix for {vuln_type}"
    severity = finding.get("Severity") or "UNKNOWN"
    analysis = (
        finding.get("Analysis") or "Automated fix generated by CodeMender."
    )

    branch_name = generate_branch_name(vuln_type, file_path)
    logger.info(
        "Processing finding %d/%d [ID: %s, VulnType: %s, Branch: %s]",
        idx,
        len(findings),
        finding_id,
        vuln_type,
        branch_name,
    )

    # PR Spam Prevention Check
    if check_remote_branch_exists(
        clean_repo_url, token, branch_name, cwd=repo_dir
    ):
      logger.info(
          "Remote branch %s already exists. Skipping finding %s to prevent"
          " duplicate PRs.",
          branch_name,
          finding_id,
      )
      continue

    # Force checkout default branch before verify/fix
    run_command(["git", "checkout", "-f", default_branch], cwd=repo_dir)
    run_command(["git", "clean", "-fd"], cwd=repo_dir)

    # Verify finding
    logger.info("Verifying finding %s...", finding_id)
    verify_res = run_command(
        [cm_binary, "find", "verify", finding_id, "--yes"],
        cwd=repo_dir,
        env=scrubbed_env,
        check=False,
    )
    if verify_res.returncode != 0:
      logger.warning(
          "Verification failed for finding %s (code %d). Skipping fix.",
          finding_id,
          verify_res.returncode,
      )
      continue

    # Fix finding
    logger.info("Applying fix for finding %s...", finding_id)
    fix_res = run_command(
        [cm_binary, "fix", finding_id, "--yes"],
        cwd=repo_dir,
        env=scrubbed_env,
        check=False,
    )
    if fix_res.returncode != 0:
      logger.warning(
          "Fix failed for finding %s (code %d). Skipping.",
          finding_id,
          fix_res.returncode,
      )
      continue

    # Check if changes were produced (Only stage modified tracked files to avoid build garbage)
    status_res = run_command(["git", "status", "--porcelain"], cwd=repo_dir)
    if not status_res.stdout.strip():
      logger.warning(
          "Fix command executed but no file changes were detected for"
          " finding %s.",
          finding_id,
      )
      continue

    # Create branch, commit, push, and open PR
    try:
      # Use checkout -B to force overwrite existing local branch names from dirty interrupts
      run_command(["git", "checkout", "-B", branch_name], cwd=repo_dir)
      # Use 'git add -u' to only stage changes to existing tracked files, preventing log pollution
      run_command(["git", "add", "-u"], cwd=repo_dir)
      commit_msg = f"fix(security): resolve {vuln_type} in {file_path}"
      run_command(["git", "commit", "-m", commit_msg], cwd=repo_dir)

      logger.info("Pushing branch %s to remote...", branch_name)
      # TODO: Avoid passing token in process arguments to prevent exposure in /proc/cmdline
      push_cmd = [
          "git",
          "-c",
          get_git_auth_header(token),
          "push",
          "origin",
          branch_name,
      ]
      run_command(push_cmd, cwd=repo_dir)

      pr_title = (
          f"fix(security): resolve {vuln_type} vulnerability in {file_path}"
      )
      pr_body = (
          "### CodeMender Security Fix\n\n"
          f"**Finding ID**: `{finding_id}`\n"
          f"**Title**: {title}\n"
          f"**Severity**: {severity}\n"
          f"**Vulnerability Type**: {vuln_type}\n"
          f"**File Path**: `{file_path}`\n\n"
          f"#### Analysis\n{analysis}\n\n"
          "---\n"
          "*Automatically generated by CodeMender Orchestrator.*"
      )

      create_pull_request(
          token=token,
          owner=owner,
          repo=repo_name,
          title=pr_title,
          body=pr_body,
          head_branch=branch_name,
          base_branch=default_branch,
      )

    except Exception as e:
      logger.error("Error creating branch/PR for finding %s: %s", finding_id, e)
    finally:
      # Workspace Reset Rule: switch back to default branch
      run_command(["git", "checkout", "-f", default_branch], cwd=repo_dir)
      run_command(["git", "clean", "-fd"], cwd=repo_dir)

  logger.info("CodeMender Orchestration completed successfully.")


if __name__ == "__main__":
  main()
