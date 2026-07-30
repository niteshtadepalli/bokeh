"""Stage 1: Scan & Dispatch runner for CodeMender Agent."""

import json
import logging
import os
import shutil
import sqlite3
import sys
import tarfile
import time

from codemender_agent.codemender.cli import parse_findings_json
from codemender_agent.config import get_github_credentials
from codemender_agent.config import get_scrubbed_env
from codemender_agent.config import inject_codemender_config
from codemender_agent.storage import generate_signed_url
from codemender_agent.storage import upload_file_to_gcs
from codemender_agent.utils import run_command
from codemender_agent.vcs.git import generate_branch_name
from codemender_agent.vcs.git import get_git_auth_header
from codemender_agent.vcs.git import parse_repo_owner_and_name
from codemender_agent.vcs.git import sanitize_git_url
from codemender_agent.vcs.git import setup_local_git_excludes
from codemender_agent.vcs.github import check_remote_branch_exists
from codemender_agent.vcs.github import get_default_branch
from codemender_agent.vcs.github import is_duplicate_pr

logger = logging.getLogger("codemender-orchestrator")


def make_tarfile(output_filename: str, source_dir: str) -> None:
  """Creates a tar.gz archive of a directory."""
  with tarfile.open(output_filename, "w:gz") as tar:
    tar.add(source_dir, arcname=os.path.basename(source_dir))


def _sync_repository(
    repo_url: str,
    token: str,
    repo_dir: str,
    workspace_dir: str,
) -> str:
  """Syncs the repository (clones if not exists, fetches and resets if exists).

  Returns:
    The target SHA of the repository after sync.
  """
  clean_repo_url = sanitize_git_url(repo_url)
  owner, repo_name = parse_repo_owner_and_name(clean_repo_url)

  logger.info("Syncing repository for scanning: %s", clean_repo_url)
  if not os.path.exists(os.path.join(repo_dir, ".git")):
    # Clone the repository with depth 1 for faster scan if it doesn't exist
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
    # If it exists, fetch the latest state of the current branch and reset hard
    logger.info("Repository directory exists, fetching latest state...")
    try:
      curr_branch = run_command(
          ["git", "branch", "--show-current"], cwd=repo_dir
      ).stdout.strip()
    except Exception:  # pylint: disable=broad-exception-caught
      curr_branch = "main"
    if not curr_branch:
      curr_branch = "main"

    fetch_cmd = [
        "git",
        "-c",
        get_git_auth_header(token),
        "fetch",
        "origin",
        curr_branch,
    ]
    run_command(fetch_cmd, cwd=repo_dir)
    run_command(["git", "checkout", "-f", curr_branch], cwd=repo_dir)
    run_command(
        ["git", "reset", "--hard", f"origin/{curr_branch}"], cwd=repo_dir
    )

  # Determine the default branch to checkout for scanning
  try:
    default_branch = run_command(
        ["git", "branch", "--show-current"], cwd=repo_dir
    ).stdout.strip()
  except Exception:  # pylint: disable=broad-exception-caught
    default_branch = ""
  if not default_branch:
    default_branch = get_default_branch(token, owner, repo_name)

  logger.info("Using default branch: %s", default_branch)
  run_command(["git", "checkout", "-f", default_branch], cwd=repo_dir)

  # Record and return target SHA
  target_sha = run_command(
      ["git", "rev-parse", "HEAD"], cwd=repo_dir
  ).stdout.strip()
  logger.info("Recorded target Git SHA: %s", target_sha)

  # Configure git user and excludes
  run_command(["git", "config", "user.name", "CodeMender Agent"], cwd=repo_dir)
  run_command(
      ["git", "config", "user.email", "codemender-agent@google.com"],
      cwd=repo_dir,
  )
  setup_local_git_excludes(repo_dir)

  return target_sha


def _init_codemender(
    repo_dir: str,
    scrubbed_env: dict[str, str],
    cm_binary: str,
) -> None:
  """Initializes CodeMender CLI in the repository."""
  logger.info("Initializing CodeMender CLI...")
  try:
    # Run basic init to create .cm_project
    run_command(
        [cm_binary, "init"],
        cwd=repo_dir,
        env=scrubbed_env,
        check=True,
    )
    # Inject config before verifying
    inject_codemender_config(repo_dir)
    # Verify the initialization (runs build command)
    run_command(
        [cm_binary, "init", "--verify"],
        cwd=repo_dir,
        env=scrubbed_env,
        check=True,
    )
  except Exception as e:  # pylint: disable=broad-exception-caught
    logger.critical("CodeMender initialization failed: %s", e)
    sys.exit(1)


def _scan_repository(
    repo_dir: str,
    scrubbed_env: dict[str, str],
    cm_binary: str,
    targets: list[str],
) -> list[dict[str, any]]:
  """Runs scan on targets with retries if no findings are found."""
  max_scan_attempts = 3
  findings = []

  for attempt in range(1, max_scan_attempts + 1):
    logger.info("Running scan attempt %d/%d...", attempt, max_scan_attempts)
    # Run find command for each target folder
    for target in targets:
      try:
        run_command(
            [cm_binary, "find", target],
            cwd=repo_dir,
            env=scrubbed_env,
            check=True,
        )
      except Exception as e:  # pylint: disable=broad-exception-caught
        logger.error("Scan failed for target %s: %s", target, e)
        sys.exit(1)

    # Retrieve finding report in JSON format
    try:
      report_res = run_command(
          [cm_binary, "report", "--format", "json"],
          cwd=repo_dir,
          env=scrubbed_env,
          check=True,
          capture_stderr=False,
      )
      findings = parse_findings_json(report_res.stdout)
    except Exception as e:  # pylint: disable=broad-exception-caught
      logger.error("Failed to get report: %s", e)
      findings = []

    # Exit retry loop early if we found findings
    if findings:
      logger.info("Found %d findings on attempt %d.", len(findings), attempt)
      break
    else:
      logger.warning("No findings found on attempt %d.", attempt)
      if attempt < max_scan_attempts:
        time.sleep(5)

  return findings


def _filter_findings(
    findings: list[dict[str, any]],
    repo_url: str,
    token: str,
    repo_dir: str,
    force_overwrite: bool,
) -> tuple[list[dict[str, any]], list[str]]:
  """Filters out findings that already have remote branches (unless forced)."""
  active_findings = []
  skipped_finding_ids = []
  clean_repo_url = sanitize_git_url(repo_url)

  for finding in findings:
    finding_id = finding.get("FindingID")
    if not finding_id:
      continue
    # Only process findings that are not already resolved or false positives
    status = finding.get("Status")
    if status in ["FALSE_POSITIVE", "RESOLVED"]:
      continue

    # Check if a remote branch already exists for this finding to avoid duplicates
    vuln_type = finding.get("VulnType") or "vulnerability"
    fingerprint = (
        finding.get("Fingerprint")
        or finding.get("fingerprint")
        or finding.get("FindingID")
        or ""
    )

    branch_name = generate_branch_name(vuln_type, fingerprint)
    
    file_path = finding.get("FilePath") or "unknown_file"
    try:
      start_line = int(finding.get("StartLine") or 0)
    except ValueError:
      start_line = 0

    if not force_overwrite and check_remote_branch_exists(
        clean_repo_url, token, branch_name, cwd=repo_dir
    ):
      logger.info(
          "Skipping finding %s as remote branch %s exists.",
          finding_id,
          branch_name,
      )
      skipped_finding_ids.append(finding_id)
      continue

    if not force_overwrite and is_duplicate_pr(
        clean_repo_url, token, file_path, vuln_type, start_line
    ):
      logger.info(
          "An open PR covering %s in %s near line %d already exists. Skipping finding %s.",
          vuln_type, file_path, start_line, finding_id
      )
      skipped_finding_ids.append(finding_id)
      continue

    active_findings.append(finding)


  return active_findings, skipped_finding_ids


def _partition_findings(
    active_findings: list[dict[str, any]],
    max_tasks: int,
) -> list[list[str]]:
  """Partitions active finding IDs into N worker buckets."""
  active_findings_count = len(active_findings)
  num_workers = min(active_findings_count, max_tasks)
  logger.info(
      "Partitioning %d active findings into %d workers (max_tasks=%d)",
      active_findings_count,
      num_workers,
      max_tasks,
  )

  # Sort findings by FilePath to group same directory/file findings together
  sorted_findings = sorted(
      active_findings, key=lambda f: f.get("FilePath") or ""
  )
  sorted_ids = [f["FindingID"] for f in sorted_findings]

  # Calculate even partition sizes
  base_size = active_findings_count // num_workers
  remainder = active_findings_count % num_workers
  sizes = [base_size + (1 if i < remainder else 0) for i in range(num_workers)]

  # Chunk the sorted findings into partitions
  partitions = []
  start = 0
  for size in sizes:
    partitions.append(sorted_ids[start : start + size])
    start += size

  return partitions


def _save_and_upload_state(
    partitions: list[list[str]],
    active_findings_count: int,
    target_sha: str,
    workspace_dir: str,
    bucket_name: str,
    scan_id: str,
) -> None:
  """Saves partitions and manifest, generates signed URLs, and uploads to GCS."""
  # Tar ~/.codemender
  codemender_home = os.path.expanduser("~/.codemender")
  tarball_path = os.path.join(workspace_dir, "workspace_base.tar.gz")
  logger.info("Archiving ~/.codemender to %s", tarball_path)
  make_tarfile(tarball_path, codemender_home)

  # Upload tarball
  if not upload_file_to_gcs(
      tarball_path, bucket_name, f"scans/{scan_id}/workspace_base.tar.gz"
  ):
    logger.critical("Failed to upload base workspace archive to GCS.")
    sys.exit(1)

  # Generate Signed URL for the base workspace
  base_workspace_blob = f"scans/{scan_id}/workspace_base.tar.gz"
  base_workspace_url = generate_signed_url(
      bucket_name, base_workspace_blob, method="GET"
  )

  partition_urls = []
  upload_urls = []

  # Save and upload each partition file
  for i, part_ids in enumerate(partitions):
    partition_data = {"partition_index": i, "finding_ids": part_ids}
    part_path = os.path.join(workspace_dir, f"partition_{i}.json")
    with open(part_path, "w") as f:
      json.dump(partition_data, f, indent=2)

    part_blob = f"scans/{scan_id}/partition_{i}.json"
    if not upload_file_to_gcs(part_path, bucket_name, part_blob):
      logger.critical("Failed to upload partition file to GCS.")
      sys.exit(1)

    # Generate Signed URL for workers to download their partition
    part_url = generate_signed_url(bucket_name, part_blob, method="GET")
    if not part_url:
      logger.critical(
          "Failed to generate GET signed URL for partition %d.", i
      )
      sys.exit(1)
    partition_urls.append(part_url)

    # Generate Signed URL for workers to upload their mutated DB shard
    worker_db_blob = f"scans/{scan_id}/worker_{i}_state.db"
    upload_url = generate_signed_url(
        bucket_name,
        worker_db_blob,
        method="PUT",
        content_type="application/octet-stream",
    )
    if not upload_url:
      logger.critical(
          "Failed to generate PUT signed URL for worker %d.", i
      )
      sys.exit(1)
    upload_urls.append(upload_url)

  # Write and upload manifest with all Signed URLs included
  manifest = {
      "findings_count": active_findings_count,
      "target_sha": target_sha,
      "base_workspace_url": base_workspace_url,
      "partition_urls": partition_urls,
      "upload_urls": upload_urls,
  }
  manifest_path = os.path.join(workspace_dir, "manifest.json")
  with open(manifest_path, "w") as f:
    json.dump(manifest, f, indent=2)
  if not upload_file_to_gcs(
      manifest_path, bucket_name, f"scans/{scan_id}/manifest.json"
  ):
    logger.critical("Failed to upload manifest.json to GCS.")
    sys.exit(1)


def run_scan_pipeline() -> None:
  """Executes Stage 1: Scan repository, filter, partition, and upload state."""
  scan_id = os.environ.get("CODEMENDER_SCAN_ID")
  bucket_name = os.environ.get("CODEMENDER_GCS_BUCKET")
  if not scan_id or not bucket_name:
    logger.critical("CODEMENDER_SCAN_ID and CODEMENDER_GCS_BUCKET must be set.")
    sys.exit(1)

  repo_url, token = get_github_credentials()
  workspace_dir = os.environ.get("WORKSPACE_DIR", os.getcwd())
  clean_repo_url = sanitize_git_url(repo_url)
  _, repo_name = parse_repo_owner_and_name(clean_repo_url)
  repo_dir = os.path.join(workspace_dir, repo_name)
  scrubbed_env = get_scrubbed_env()

  # 1. Sync repository and get target SHA
  target_sha = _sync_repository(repo_url, token, repo_dir, workspace_dir)

  # 2. Initialize CodeMender CLI
  cm_binary = shutil.which("cm") or "cm"
  _init_codemender(repo_dir, scrubbed_env, cm_binary)

  # 3. Parse scan targets
  scan_target_env = os.environ.get("CODEMENDER_SCAN_TARGET", ".")
  targets = []
  for part in scan_target_env.split(";"):
    for subpart in part.split(","):
      t = subpart.strip()
      if t:
        targets.append(t)
  if not targets:
    targets = ["."]

  # 4. Scan repository
  findings = _scan_repository(repo_dir, scrubbed_env, cm_binary, targets)

  # Handle zero findings case
  if not findings:
    logger.info("Zero findings confirmed after scanning. Exiting Stage 1.")
    manifest = {"findings_count": 0, "target_sha": target_sha}
    manifest_path = os.path.join(workspace_dir, "manifest.json")
    with open(manifest_path, "w") as f:
      json.dump(manifest, f, indent=2)
    if not upload_file_to_gcs(
        manifest_path, bucket_name, f"scans/{scan_id}/manifest.json"
    ):
      logger.critical("Failed to upload manifest.json to GCS.")
      sys.exit(1)
    sys.exit(0)

  # 5. Filter findings
  force_overwrite = (
      os.environ.get("CODEMENDER_FORCE_OVERWRITE", "false").lower() == "true"
  )
  active_findings, skipped_finding_ids = _filter_findings(
      findings, repo_url, token, repo_dir, force_overwrite
  )

  # Soft-delete skipped findings in local state.db for telemetry before archiving
  if skipped_finding_ids:
    db_path = os.path.expanduser("~/.codemender/state.db")
    if os.path.exists(db_path):
      try:
        conn = sqlite3.connect(db_path)
        cursor = conn.cursor()
        for fid in skipped_finding_ids:
          cursor.execute(
              "UPDATE findings SET status = 'DISMISSED', muted = 1, dismiss_reason = 'Duplicate PR or branch already exists' WHERE finding_id = ?",
              (fid,)
          )
        conn.commit()
        conn.close()
        logger.info("Dismissed %d skipped findings in local state.db.", len(skipped_finding_ids))
      except Exception as e:
        logger.warning("Failed to update skipped findings in state.db: %s", e)
  active_findings_count = len(active_findings)
  logger.info("Active findings after filtering: %d", active_findings_count)

  # Handle zero active findings case
  if active_findings_count == 0:
    logger.info("Zero active findings after filtering. Exiting Stage 1.")
    manifest = {"findings_count": 0, "target_sha": target_sha}
    manifest_path = os.path.join(workspace_dir, "manifest.json")
    with open(manifest_path, "w") as f:
      json.dump(manifest, f, indent=2)
    if not upload_file_to_gcs(
        manifest_path, bucket_name, f"scans/{scan_id}/manifest.json"
    ):
      logger.critical("Failed to upload manifest.json to GCS.")
      sys.exit(1)
    sys.exit(0)

  # 6. Partition findings
  max_tasks = int(os.environ.get("CODEMENDER_MAX_TASKS", "20"))
  partitions = _partition_findings(active_findings, max_tasks)

  # 7. Save and upload state
  _save_and_upload_state(
      partitions,
      active_findings_count,
      target_sha,
      workspace_dir,
      bucket_name,
      scan_id,
  )

  logger.info("Stage 1 (Scan) completed successfully.")
