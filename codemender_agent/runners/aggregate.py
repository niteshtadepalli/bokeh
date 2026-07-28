"""Stage 3: Aggregator runner for CodeMender Agent."""

import json
import logging
import os
import shutil
import sqlite3
import sys
import tarfile
import time
from typing import Optional

from codemender_agent.config import get_github_credentials
from codemender_agent.config import get_scrubbed_env
from codemender_agent.config import inject_codemender_config
from codemender_agent.storage import download_file_from_gcs
from codemender_agent.storage import list_gcs_blobs
from codemender_agent.storage import upload_and_sign_report
from codemender_agent.utils import run_command
from codemender_agent.vcs.git import get_git_auth_header
from codemender_agent.vcs.git import parse_repo_owner_and_name
from codemender_agent.vcs.git import sanitize_git_url
from codemender_agent.vcs.git import setup_local_git_excludes
from codemender_agent.vcs.github import get_default_branch

logger = logging.getLogger("codemender-orchestrator")


def merge_db(base_db_path: str, worker_db_path: str) -> None:
  """Merges worker database tables into base database using selective SQLite UPSERT."""
  if not os.path.exists(worker_db_path):
    logger.warning("Worker database file not found: %s", worker_db_path)
    return

  logger.info("Merging %s into %s...", worker_db_path, base_db_path)
  conn = sqlite3.connect(base_db_path)
  cursor = conn.cursor()
  try:
    # Attach the worker database shard
    cursor.execute(f"ATTACH DATABASE '{worker_db_path}' AS worker")

    # 1. Findings Merge:
    # We copy findings from worker to main. On conflict (finding_id already exists),
    # we update the fields only if the worker's finding has a newer updated_at timestamp.
    # Note: 'WHERE true' before ON CONFLICT is a workaround to resolve SQLite parsing
    # ambiguity between SELECT's join clauses and the ON CONFLICT clause.
    cursor.execute("""
        INSERT INTO main.findings (
            finding_id, session_id, title, file_path, severity, confidence, analysis, snippet, vuln_type, vuln_id,
            verified, muted, mute_reason, created_at, fingerprint, status, source_stage, finding_json, updated_at,
            start_line, end_line, dismiss_reason, confidence_level
        )
        SELECT 
            finding_id, session_id, title, file_path, severity, confidence, analysis, snippet, vuln_type, vuln_id,
            verified, muted, mute_reason, created_at, fingerprint, status, source_stage, finding_json, updated_at,
            start_line, end_line, dismiss_reason, confidence_level
        FROM worker.findings AS w
        WHERE EXISTS (
            SELECT 1 FROM main.findings AS m
            WHERE m.finding_id = w.finding_id
        )
        ON CONFLICT(finding_id) DO UPDATE SET
            session_id = excluded.session_id,
            title = excluded.title,
            file_path = excluded.file_path,
            severity = excluded.severity,
            confidence = excluded.confidence,
            analysis = excluded.analysis,
            snippet = excluded.snippet,
            vuln_type = excluded.vuln_type,
            vuln_id = excluded.vuln_id,
            verified = excluded.verified,
            muted = excluded.muted,
            mute_reason = excluded.mute_reason,
            status = excluded.status,
            source_stage = excluded.source_stage,
            finding_json = excluded.finding_json,
            updated_at = excluded.updated_at,
            start_line = excluded.start_line,
            end_line = excluded.end_line,
            dismiss_reason = excluded.dismiss_reason,
            confidence_level = excluded.confidence_level
        WHERE excluded.updated_at > findings.updated_at OR findings.updated_at = '' OR findings.updated_at IS NULL;
    """)

    # 2. Sessions Merge:
    # Similarly, update session status only if the worker's session record is newer.
    # Uses the same 'WHERE true' workaround.
    cursor.execute("""
        INSERT INTO main.sessions (session_id, operation_name, session_type, status, pipeline_mode, target, created_at, updated_at, project_root)
        SELECT session_id, operation_name, session_type, status, pipeline_mode, target, created_at, updated_at, project_root
        FROM worker.sessions
        WHERE true
        ON CONFLICT(session_id) DO UPDATE SET
            status = excluded.status,
            updated_at = excluded.updated_at
        WHERE excluded.updated_at > sessions.updated_at;
    """)

    # 3. Artifacts Merge:
    # Insert new artifacts from worker. Since ID is autoincrement, we match on
    # session_id and filename to prevent duplicates and avoid key conflicts.
    cursor.execute("""
        INSERT INTO main.artifacts (session_id, filename, original_path, purpose, finding_id, created_at)
        SELECT session_id, filename, original_path, purpose, finding_id, created_at
        FROM worker.artifacts AS w
        WHERE (w.finding_id IS NULL OR EXISTS (
            SELECT 1 FROM main.findings AS m
            WHERE m.finding_id = w.finding_id
        )) AND NOT EXISTS (
            SELECT 1 FROM main.artifacts AS m
            WHERE m.session_id = w.session_id AND m.filename = w.filename
        );
    """)

    # 4. Patches Merge:
    # We copy patches from worker to main. On conflict (patch_id already exists),
    # we update the fields. We filter to ensure the patch refers to a finding
    # that exists in the main findings table (preventing ghost patches).
    cursor.execute("""
        INSERT INTO main.patches (
            patch_id, finding_id, session_id, diff, reasoning, status, backup_path,
            target_file, edited_files, validation_result, created_at
        )
        SELECT 
            patch_id, finding_id, session_id, diff, reasoning, status, backup_path,
            target_file, edited_files, validation_result, created_at
        FROM worker.patches AS w
        WHERE EXISTS (
            SELECT 1 FROM main.findings AS m
            WHERE m.finding_id = w.finding_id
        )
        ON CONFLICT(patch_id) DO UPDATE SET
            finding_id = excluded.finding_id,
            session_id = excluded.session_id,
            diff = excluded.diff,
            reasoning = excluded.reasoning,
            status = excluded.status,
            backup_path = excluded.backup_path,
            target_file = excluded.target_file,
            edited_files = excluded.edited_files,
            validation_result = excluded.validation_result,
            created_at = excluded.created_at;
    """)

    conn.commit()

    logger.info("Merged %s successfully.", worker_db_path)
  except sqlite3.Error as e:  # pylint: disable=broad-exception-caught
    logger.error("Failed to merge database %s: %s", worker_db_path, e)
    conn.rollback()
  finally:
    try:
      cursor.execute("DETACH DATABASE worker")
    except sqlite3.Error:  # pylint: disable=broad-exception-caught
      pass
    conn.close()


def _verify_worker_db_counts(
    worker_db_blobs: list[str],
    total_workers_env: Optional[str],
) -> None:
  """Verifies if the number of downloaded DBs matches expected worker count."""
  if not total_workers_env:
    return

  try:
    expected_workers = int(total_workers_env)
    found_indices = set()
    for blob in worker_db_blobs:
      basename = os.path.basename(blob)
      # Parse index from format: worker_[index]_state.db
      parts = basename.split("_")
      if len(parts) >= 2:
        try:
          idx = int(parts[1])
          found_indices.add(idx)
        except ValueError:
          pass

    missing_workers = set(range(expected_workers)) - found_indices
    if missing_workers:
      logger.warning(
          "Missing databases for worker tasks: %s. "
          "The final report might be incomplete.",
          list(missing_workers),
      )
  except ValueError:
    logger.warning(
        "Invalid CODEMENDER_TOTAL_WORKERS or CLOUD_RUN_TASK_COUNT value: %s",
        total_workers_env,
    )


def _download_and_merge_worker_dbs(
    worker_db_blobs: list[str],
    temp_db_dir: str,
    bucket_name: str,
    base_db_path: str,
) -> None:
  """Downloads each worker DB shard from GCS and merges it into base DB."""
  for blob in worker_db_blobs:
    local_worker_db = os.path.join(temp_db_dir, os.path.basename(blob))
    logger.info("Downloading %s...", blob)
    if download_file_from_gcs(local_worker_db, bucket_name, blob):
      merge_db(base_db_path, local_worker_db)
    else:
      logger.error("Failed to download worker DB: %s", blob)


def _generate_and_upload_report(
    repo_dir: str,
    scrubbed_env: dict[str, str],
    cm_binary: str,
    codemender_home: str,
    bucket_name: str,
    owner: str,
    repo_name: str,
) -> None:
  """Generates final HTML report using cm CLI and uploads it to GCS."""
  logger.info("Generating final consolidated HTML summary report...")
  report_res = run_command(
      [cm_binary, "report", "-f", "html"],
      cwd=repo_dir,
      env=scrubbed_env,
      check=False,
  )

  if report_res.returncode == 0:
    local_report_path = os.path.join(codemender_home, "reports/report.html")
    report_bucket = os.environ.get("CODEMENDER_REPORT_BUCKET") or bucket_name
    dest_blob = (
        f"reports/{owner}_{repo_name}/"
        f"report_{time.strftime('%Y%m%d-%H%M%S')}.html"
    )

    logger.info("Uploading final report to GCS bucket %s...", report_bucket)
    signed_url = upload_and_sign_report(
        local_report_path, report_bucket, dest_blob
    )
    if signed_url:
      logger.info(
          "\n"
          "======================================================================\n"
          "📊 CONSOLIDATED CODEMENDER SUMMARY REPORT GENERATED:\n"
          "👉 %s\n"
          "======================================================================\n",
          signed_url,
      )
    else:
      logger.critical(
          "Failed to upload or generate signed URL for the consolidated GCS"
          " report."
      )
      sys.exit(1)
  else:
    logger.error(
        "Failed to execute 'cm report -f html' in aggregator (code %d).",
        report_res.returncode,
    )
    sys.exit(1)


def run_aggregate_pipeline() -> None:
  """Executes Stage 3: Download all worker states, merge DBs, generate report, and upload."""
  scan_id = os.environ.get("CODEMENDER_SCAN_ID")
  bucket_name = os.environ.get("CODEMENDER_GCS_BUCKET")
  if not scan_id or not bucket_name:
    logger.critical("CODEMENDER_SCAN_ID and CODEMENDER_GCS_BUCKET must be set.")
    sys.exit(1)

  workspace_dir = os.environ.get("WORKSPACE_DIR", os.getcwd())
  repo_url, token = get_github_credentials()
  clean_repo_url = sanitize_git_url(repo_url)
  owner, repo_name = parse_repo_owner_and_name(clean_repo_url)
  repo_dir = os.path.join(workspace_dir, repo_name)
  scrubbed_env = get_scrubbed_env()

  # 1. Download manifest
  manifest_path = os.path.join(workspace_dir, "manifest.json")
  logger.info("Downloading manifest.json...")
  if not download_file_from_gcs(
      manifest_path, bucket_name, f"scans/{scan_id}/manifest.json"
  ):
    logger.critical("Failed to download manifest.json.")
    sys.exit(1)

  with open(manifest_path, "r") as f:
    manifest = json.load(f)

  target_sha = manifest.get("target_sha")
  findings_count = manifest.get("findings_count", 0)

  if findings_count == 0:
    logger.info("Manifest indicates 0 findings. Nothing to aggregate.")
    sys.exit(0)

  # 2. Clone repository to have files ready for report generation
  logger.info("Cloning repository for report generation: %s", clean_repo_url)
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

  # Checkout target commit SHA
  if target_sha:
    logger.info("Checking out target SHA: %s", target_sha)
    run_command(["git", "checkout", "-f", target_sha], cwd=repo_dir)
  else:
    logger.warning("Target SHA not found in manifest, using default branch.")
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

  setup_local_git_excludes(repo_dir)

  # 3. Download & Extract base workspace_base.tar.gz to restored state
  codemender_home = os.path.expanduser("~/.codemender")
  if os.path.exists(codemender_home):
    shutil.rmtree(codemender_home)
  os.makedirs(codemender_home, exist_ok=True)

  tarball_path = os.path.join(workspace_dir, "workspace_base.tar.gz")
  logger.info("Downloading base workspace...")
  if not download_file_from_gcs(
      tarball_path, bucket_name, f"scans/{scan_id}/workspace_base.tar.gz"
  ):
    logger.critical("Failed to download base workspace.")
    sys.exit(1)

  logger.info(
      "Extracting base workspace to %s", os.path.dirname(codemender_home)
  )
  with tarfile.open(tarball_path, "r:gz") as tar:
    tar.extractall(path=os.path.dirname(codemender_home))

  base_db_path = os.path.join(codemender_home, "state.db")

  # 4. List and verify worker DBs in GCS
  logger.info("Listing worker databases in GCS...")
  all_blobs = list_gcs_blobs(bucket_name, f"scans/{scan_id}/")
  worker_db_blobs = [
      b
      for b in all_blobs
      if b.startswith(f"scans/{scan_id}/worker_") and b.endswith("_state.db")
  ]
  logger.info("Found worker DB blobs: %s", worker_db_blobs)

  total_workers_env = os.environ.get(
      "CODEMENDER_TOTAL_WORKERS"
  ) or os.environ.get("CLOUD_RUN_TASK_COUNT")
  _verify_worker_db_counts(worker_db_blobs, total_workers_env)

  # 5. Download and merge worker database shards
  temp_db_dir = os.path.join(workspace_dir, "worker_dbs")
  os.makedirs(temp_db_dir, exist_ok=True)
  _download_and_merge_worker_dbs(
      worker_db_blobs, temp_db_dir, bucket_name, base_db_path
  )

  # 6. Generate final report and upload
  inject_codemender_config(repo_dir)

  # Delete DISMISSED findings from local db to keep HTML report clean
  try:
    conn = sqlite3.connect(base_db_path)
    conn.execute("DELETE FROM findings WHERE status = 'DISMISSED'")
    conn.commit()
    conn.close()
    logger.info("Removed DISMISSED findings from local state.db for a clean HTML report.")
  except sqlite3.Error as e:
    logger.warning("Failed to remove DISMISSED findings from state.db: %s", e)

  cm_binary = shutil.which("cm") or "cm"
  _generate_and_upload_report(
      repo_dir,
      scrubbed_env,
      cm_binary,
      codemender_home,
      bucket_name,
      owner,
      repo_name,
  )

  logger.info("Stage 3 (Aggregate) completed successfully.")
