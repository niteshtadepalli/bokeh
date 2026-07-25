"""Stage 2: Parallel Worker runner for CodeMender Agent."""

import json
import logging
import os
import shutil
import sys
import tarfile
import time
from typing import Optional

from codemender_agent.codemender.cli import parse_findings_json
from codemender_agent.codemender.db import get_finding_status
from codemender_agent.codemender.db import is_finding_verified
from codemender_agent.config import get_cleanup_ports
from codemender_agent.config import get_github_credentials
from codemender_agent.config import get_scrubbed_env
from codemender_agent.config import inject_codemender_config
from codemender_agent.storage import download_from_url
from codemender_agent.storage import upload_to_url
from codemender_agent.utils import free_port
from codemender_agent.utils import run_command
from codemender_agent.vcs.git import generate_branch_name
from codemender_agent.vcs.git import get_git_auth_header
from codemender_agent.vcs.git import parse_repo_owner_and_name
from codemender_agent.vcs.git import sanitize_git_url
from codemender_agent.vcs.git import setup_local_git_excludes
from codemender_agent.vcs.github import check_remote_branch_exists
from codemender_agent.vcs.github import create_pull_request
from codemender_agent.vcs.github import get_default_branch

logger = logging.getLogger("codemender-orchestrator")


def _setup_git_and_checkout(
    clean_repo_url: str,
    token: str,
    repo_dir: str,
    workspace_dir: str,
    target_sha: Optional[str],
    owner: str,
    repo_name: str,
) -> str:
  """Clones the repository and checkouts the target branch/SHA.

  Returns:
    The name of the checked-out branch (default branch).
  """
  logger.info("Cloning repository: %s", clean_repo_url)
  if os.path.exists(repo_dir):
    shutil.rmtree(repo_dir)

  clone_cmd = [
      "git",
      "-c",
      get_git_auth_header(token),
      "clone",
      clean_repo_url,
      repo_dir,
  ]
  run_command(clone_cmd, cwd=workspace_dir)

  # Determine default branch
  try:
    default_branch = run_command(
        ["git", "branch", "--show-current"], cwd=repo_dir
    ).stdout.strip()
  except Exception:  # pylint: disable=broad-exception-caught
    default_branch = ""
  if not default_branch:
    default_branch = get_default_branch(token, owner, repo_name)

  if target_sha:
    logger.info("Checking out target SHA: %s", target_sha)
    run_command(["git", "checkout", "-f", target_sha], cwd=repo_dir)
  else:
    logger.warning("CODEMENDER_TARGET_SHA not set, using default branch.")
    logger.info("Using default branch: %s", default_branch)
    run_command(["git", "checkout", "-f", default_branch], cwd=repo_dir)

  run_command(["git", "config", "user.name", "CodeMender Agent"], cwd=repo_dir)
  run_command(
      ["git", "config", "user.email", "codemender-agent@google.com"],
      cwd=repo_dir,
  )
  setup_local_git_excludes(repo_dir)

  return default_branch


def _restore_state(
    base_workspace_url: str,
    partition_url: str,
    workspace_dir: str,
    worker_index: int,
    codemender_home: str,
) -> tuple[str, list[str]]:
  """Downloads and extracts base workspace tarball and worker partition file."""
  # Clean and prepare ~/.codemender/
  if os.path.exists(codemender_home):
    shutil.rmtree(codemender_home)
  os.makedirs(codemender_home, exist_ok=True)

  # Download base workspace tarball
  tarball_path = os.path.join(workspace_dir, "workspace_base.tar.gz")
  logger.info("Downloading base workspace from signed URL...")
  if not download_from_url(base_workspace_url, tarball_path):
    logger.critical("Failed to download base workspace.")
    sys.exit(1)

  # Extract base workspace
  logger.info(
      "Extracting base workspace to %s", os.path.dirname(codemender_home)
  )
  with tarfile.open(tarball_path, "r:gz") as tar:
    tar.extractall(path=os.path.dirname(codemender_home))

  # Download partition JSON file
  partition_path = os.path.join(workspace_dir, f"partition_{worker_index}.json")
  logger.info("Downloading partition from signed URL...")
  if not download_from_url(partition_url, partition_path):
    logger.critical("Failed to download partition.")
    sys.exit(1)

  with open(partition_path, "r") as f:
    partition_data = json.load(f)
  finding_ids = partition_data.get("finding_ids", [])
  logger.info("Worker assigned findings: %s", finding_ids)

  return partition_path, finding_ids


def _process_finding(
    finding_id: str,
    finding: dict[str, any],
    repo_dir: str,
    cm_binary: str,
    scrubbed_env: dict[str, str],
    clean_repo_url: str,
    token: str,
    owner: str,
    repo_name: str,
    default_branch: str,
    state_db_path: str,
) -> None:
  """Handles verification and fixing loop for a single finding."""
  vuln_type = finding.get("VulnType") or "vulnerability"
  file_path = finding.get("FilePath") or "unknown_file"
  title = finding.get("Title") or f"Security Fix for {vuln_type}"
  severity = finding.get("Severity") or "UNKNOWN"
  analysis = finding.get("Analysis") or "Automated fix generated by CodeMender."
  fingerprint = (
      finding.get("Fingerprint")
      or finding.get("fingerprint")
      or finding.get("FindingID")
      or ""
  )


  branch_name = generate_branch_name(vuln_type, fingerprint)

  logger.info("Processing finding %s (Branch: %s)", finding_id, branch_name)

  # Check if remote branch already exists to skip if resolved (Idempotency)
  force_overwrite = (
      os.environ.get("CODEMENDER_FORCE_OVERWRITE", "false").lower() == "true"
  )
  if not force_overwrite and check_remote_branch_exists(
      clean_repo_url, token, branch_name, cwd=repo_dir
  ):
    logger.info(
        "Remote branch %s already exists. Skipping verify and fix.",
        branch_name,
    )

    return

  # Normal Verify -> Fix loop
  max_verify_attempts = 3
  verified = False

  for attempt in range(1, max_verify_attempts + 1):
    logger.info(
        "Verifying finding %s (Attempt %d/%d)...",
        finding_id,
        attempt,
        max_verify_attempts,
    )
    for port in get_cleanup_ports():
      free_port(port)


    run_command(["git", "checkout", "-f", default_branch], cwd=repo_dir)
    run_command(
        ["git", "clean", "-fd", "-e", ".cm_project", "-e", ".exploit"],
        cwd=repo_dir,
    )

    verify_res = run_command(
        [cm_binary, "find", "verify", finding_id, "--yes"],
        cwd=repo_dir,
        env=scrubbed_env,
        check=False,
    )
    for port in get_cleanup_ports():
      free_port(port)

    if verify_res.returncode == 0 and is_finding_verified(
        state_db_path, finding_id
    ):
      logger.info("Successfully verified finding %s.", finding_id)
      verified = True
      break
    else:
      logger.warning(
          "Attempt %d failed to verify finding %s.", attempt, finding_id
      )
      if attempt < max_verify_attempts:
        time.sleep(5)

  if not verified:
    logger.error(
        "Verification failed for finding %s. Skipping fix.", finding_id
    )
    return

  # Run fix
  logger.info("Applying fix for finding %s...", finding_id)
  fix_res = run_command(
      [cm_binary, "fix", finding_id, "--yes"],
      cwd=repo_dir,
      env=scrubbed_env,
      check=False,
  )
  for port in get_cleanup_ports():
    free_port(port)

  finding_status = get_finding_status(state_db_path, finding_id)

  if fix_res.returncode != 0 or finding_status != "FIXED":
    logger.warning(
        "Fix failed for finding %s (status: %s)", finding_id, finding_status
    )
    return

  status_res = run_command(["git", "status", "--porcelain"], cwd=repo_dir)
  if not status_res.stdout.strip():
    logger.warning("No changes detected after fix for finding %s", finding_id)
    return

  # Commit, Push, and Create PR
  try:
    run_command(["git", "checkout", "-B", branch_name], cwd=repo_dir)
    run_command(["git", "add", "-u"], cwd=repo_dir)
    commit_msg = f"fix(security): resolve {vuln_type} in {file_path}"
    run_command(["git", "commit", "-m", commit_msg], cwd=repo_dir)

    logger.info("Pushing branch %s...", branch_name)
    push_cmd = ["git", "-c", get_git_auth_header(token), "push"]
    if force_overwrite:
      push_cmd.append("-f")
    push_cmd.extend(["origin", branch_name])
    run_command(push_cmd, cwd=repo_dir)

    # Create Pull Request
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
  except Exception as e:  # pylint: disable=broad-exception-caught
    logger.error("Error creating branch/PR for finding %s: %s", finding_id, e)
  finally:
    run_command(["git", "checkout", "-f", default_branch], cwd=repo_dir)
    run_command(
        ["git", "clean", "-fd", "-e", ".cm_project", "-e", ".exploit"],
        cwd=repo_dir,
    )


def run_worker_pipeline() -> None:
  """Executes Stage 2: Download state, run verify/fix on partition, upload mutated state."""
  worker_index = int(
      os.environ.get(
          "CLOUD_RUN_TASK_INDEX", os.environ.get("CODEMENDER_WORKER_INDEX", "0")
      )
  )
  logger.info("Starting Worker %d", worker_index)

  base_workspace_url = os.environ.get("CODEMENDER_BASE_WORKSPACE_URL")
  partition_urls_json = os.environ.get("CODEMENDER_PARTITION_URLS")
  upload_urls_json = os.environ.get("CODEMENDER_UPLOAD_URLS")

  if not base_workspace_url or not partition_urls_json or not upload_urls_json:
    logger.critical("Missing required signed URLs in environment.")
    sys.exit(1)

  try:
    partition_urls = json.loads(partition_urls_json)
    upload_urls = json.loads(upload_urls_json)
  except Exception as e:  # pylint: disable=broad-exception-caught
    logger.critical("Failed to parse partition or upload URLs JSON: %s", e)
    sys.exit(1)

  if worker_index >= len(partition_urls) or worker_index >= len(upload_urls):
    logger.critical(
        "Worker index %d out of bounds for URLs (partitions: %d, uploads: %d)",
        worker_index,
        len(partition_urls),
        len(upload_urls),
    )
    sys.exit(1)

  partition_url = partition_urls[worker_index]
  upload_url = upload_urls[worker_index]

  workspace_dir = os.environ.get("WORKSPACE_DIR", os.getcwd())
  repo_url, token = get_github_credentials()
  clean_repo_url = sanitize_git_url(repo_url)
  owner, repo_name = parse_repo_owner_and_name(clean_repo_url)
  repo_dir = os.path.join(workspace_dir, repo_name)
  scrubbed_env = get_scrubbed_env()

  # 1. Clone repo and setup target SHA
  target_sha = os.environ.get("CODEMENDER_TARGET_SHA")
  default_branch = _setup_git_and_checkout(
      clean_repo_url,
      token,
      repo_dir,
      workspace_dir,
      target_sha,
      owner,
      repo_name,
  )

  # 2. Restore base workspace and partition
  codemender_home = os.path.expanduser("~/.codemender")
  _, finding_ids = _restore_state(
      base_workspace_url,
      partition_url,
      workspace_dir,
      worker_index,
      codemender_home,
  )

  state_db_path = os.path.join(codemender_home, "state.db")

  # If partition has no findings, upload unmodified base DB and exit
  if not finding_ids:
    logger.info("No findings in partition. Exiting.")
    if not upload_to_url(state_db_path, upload_url):
      logger.critical("Failed to upload unmodified database.")
      sys.exit(1)
    sys.exit(0)

  inject_codemender_config(repo_dir)
  cm_binary = shutil.which("cm") or "cm"

  # 3. Retrieve findings from restored state db
  try:
    report_res = run_command(
        [cm_binary, "report", "--format", "json"],
        cwd=repo_dir,
        env=scrubbed_env,
        check=True,
        capture_stderr=False,
    )
    all_findings = parse_findings_json(report_res.stdout)
  except Exception as e:  # pylint: disable=broad-exception-caught
    logger.critical("Failed to run cm report in worker: %s", e)
    sys.exit(1)

  findings_dict = {f["FindingID"]: f for f in all_findings if "FindingID" in f}

  # 4. Run verify/fix loop for each assigned finding
  for finding_id in finding_ids:
    finding = findings_dict.get(finding_id)
    if not finding:
      logger.warning(
          "Finding %s not found in restored database, skipping.", finding_id
      )
      continue

    _process_finding(
        finding_id,
        finding,
        repo_dir,
        cm_binary,
        scrubbed_env,
        clean_repo_url,
        token,
        owner,
        repo_name,
        default_branch,
        state_db_path,
    )

  # 5. Upload mutated state DB
  logger.info("Uploading mutated database to GCS signed URL...")
  if not upload_to_url(state_db_path, upload_url):
    logger.error("Failed to upload mutated database.")
    sys.exit(1)

  logger.info("Stage 2 (Worker) completed successfully.")
