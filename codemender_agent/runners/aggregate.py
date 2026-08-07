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

"""Stage 3: Aggregator runner for CodeMender Agent."""

from contextlib import closing
import json
import logging
import os
import re
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
from codemender_agent.utils import build_cm_command
from codemender_agent.utils import run_command
from codemender_agent.vcs.git import get_git_auth_header
from codemender_agent.vcs.git import parse_repo_owner_and_name
from codemender_agent.vcs.git import sanitize_git_url
from codemender_agent.vcs.git import setup_local_git_excludes
from codemender_agent.vcs.github import get_default_branch

logger = logging.getLogger("codemender-orchestrator")


def _get_table_columns(cursor: sqlite3.Cursor, table_name: str, db_prefix: str = "main") -> list[str]:
  """Returns list of column names for a table in specified attached database."""
  try:
    cursor.execute(f"PRAGMA {db_prefix}.table_info({table_name})")
    rows = cursor.fetchall()
    return [r[1] for r in rows]
  except sqlite3.Error:
    return []


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
    cursor.execute("ATTACH DATABASE ? AS worker", (worker_db_path,))

    # 1. Findings Merge:
    main_cols = _get_table_columns(cursor, "findings", "main")
    worker_cols = _get_table_columns(cursor, "findings", "worker")
    common_cols = [c for c in worker_cols if c in main_cols]

    if common_cols and "finding_id" in common_cols:
      cols_str = ", ".join(common_cols)
      update_cols = [c for c in common_cols if c != "finding_id"]
      if update_cols:
        set_clause = ", ".join([
            f"{c} = (SELECT {c} FROM worker.findings WHERE finding_id ="
            " main.findings.finding_id)"
            for c in update_cols
        ])
        where_cond = ""
        if "updated_at" in common_cols:
          where_cond = (
              " AND ((SELECT updated_at FROM worker.findings WHERE finding_id ="
              " main.findings.finding_id) >= main.findings.updated_at OR"
              " main.findings.updated_at = '' OR main.findings.updated_at IS"
              " NULL)"
          )
        cursor.execute(f"""
            UPDATE main.findings
            SET {set_clause}
            WHERE finding_id IN (SELECT finding_id FROM worker.findings)
            {where_cond};
        """)

      cursor.execute(f"""
          INSERT OR IGNORE INTO main.findings ({cols_str})
          SELECT {cols_str} FROM worker.findings AS w
          WHERE EXISTS (
              SELECT 1 FROM main.findings AS m
              WHERE m.finding_id = w.finding_id
          );
      """)

    # 2. Sessions Merge:
    main_cols = _get_table_columns(cursor, "sessions", "main")
    worker_cols = _get_table_columns(cursor, "sessions", "worker")
    common_cols = [c for c in worker_cols if c in main_cols]

    if common_cols and "session_id" in common_cols:
      cols_str = ", ".join(common_cols)
      update_cols = [c for c in common_cols if c != "session_id"]
      if update_cols:
        set_clause = ", ".join([
            f"{c} = (SELECT {c} FROM worker.sessions WHERE session_id ="
            " main.sessions.session_id)"
            for c in update_cols
        ])
        cursor.execute(f"""
            UPDATE main.sessions
            SET {set_clause}
            WHERE session_id IN (SELECT session_id FROM worker.sessions);
        """)

      cursor.execute(f"""
          INSERT OR IGNORE INTO main.sessions ({cols_str})
          SELECT {cols_str} FROM worker.sessions;
      """)

    # 3. Artifacts Merge:
    main_cols = _get_table_columns(cursor, "artifacts", "main")
    worker_cols = _get_table_columns(cursor, "artifacts", "worker")
    common_cols = [c for c in worker_cols if c in main_cols and c not in ["id", "artifact_id"]]

    if common_cols:
      cols_str = ", ".join(common_cols)
      cursor.execute(f"""
          INSERT OR IGNORE INTO main.artifacts ({cols_str})
          SELECT {cols_str} FROM worker.artifacts AS w
          WHERE (w.finding_id IS NULL OR EXISTS (
              SELECT 1 FROM main.findings AS m
              WHERE m.finding_id = w.finding_id
          )) AND NOT EXISTS (
              SELECT 1 FROM main.artifacts AS m
              WHERE m.session_id = w.session_id AND m.filename = w.filename
          );
      """)

    # 4. Patches Merge:
    main_cols = _get_table_columns(cursor, "patches", "main")
    worker_cols = _get_table_columns(cursor, "patches", "worker")
    common_cols = [c for c in worker_cols if c in main_cols]

    if common_cols and "patch_id" in common_cols:
      cols_str = ", ".join(common_cols)
      cursor.execute(f"""
          INSERT OR REPLACE INTO main.patches ({cols_str})
          SELECT {cols_str} FROM worker.patches AS w
          WHERE EXISTS (
              SELECT 1 FROM main.findings AS m
              WHERE m.finding_id = w.finding_id
          );
      """)

    # 5. File Hashes Merge:
    cursor.execute("SELECT name FROM main.sqlite_master WHERE type='table' AND name='file_hashes'")
    if cursor.fetchone():
      main_cols = _get_table_columns(cursor, "file_hashes", "main")
      worker_cols = _get_table_columns(cursor, "file_hashes", "worker")
      common_cols = [c for c in worker_cols if c in main_cols]

      if common_cols and "file_path" in common_cols:
        cols_str = ", ".join(common_cols)
        cursor.execute(f"""
            INSERT OR REPLACE INTO main.file_hashes ({cols_str})
            SELECT {cols_str} FROM worker.file_hashes;
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


def _aggregate_token_metrics(
    workspace_dir: str,
    bucket_name: str,
    scan_id: str,
    worker_db_blobs: list[str],
) -> dict[str, int]:
  """Downloads scan_metadata.json and all worker metadata files to aggregate total token usage."""
  totals = {"in_tokens": 0, "out_tokens": 0, "total_tokens": 0}

  # 1. Download Stage 1 scan_metadata.json
  scan_meta_local = os.path.join(workspace_dir, "scan_metadata.json")
  if download_file_from_gcs(scan_meta_local, bucket_name, f"scans/{scan_id}/scan_metadata.json"):
    try:
      with open(scan_meta_local, "r") as f:
        scan_meta = json.load(f)
      scan_tokens = scan_meta.get("token_usage", {})
      totals["in_tokens"] += scan_tokens.get("in_tokens", 0)
      totals["out_tokens"] += scan_tokens.get("out_tokens", 0)
      totals["total_tokens"] += scan_tokens.get("total_tokens", 0)
    except Exception as e:  # pylint: disable=broad-exception-caught
      logger.warning("Failed to parse scan_metadata.json: %s", e)

  # 2. Discover and download Stage 2 worker metadata JSONs
  all_blobs = list_gcs_blobs(bucket_name, prefix=f"scans/{scan_id}/worker_")
  meta_blobs = sorted(list(set(
      [b for b in all_blobs if b.endswith("_metadata.json")]
      + [b.replace("_state.db", "_metadata.json") for b in worker_db_blobs]
  )))
  for meta_blob in meta_blobs:
    local_meta = os.path.join(workspace_dir, os.path.basename(meta_blob))
    if download_file_from_gcs(local_meta, bucket_name, meta_blob):
      try:
        with open(local_meta, "r") as f:
          w_meta = json.load(f)
        w_tokens = w_meta.get("token_usage", {})
        totals["in_tokens"] += w_tokens.get("in_tokens", 0)
        totals["out_tokens"] += w_tokens.get("out_tokens", 0)
        totals["total_tokens"] += w_tokens.get("total_tokens", 0)
      except Exception as e:  # pylint: disable=broad-exception-caught
        logger.warning("Failed to parse worker metadata %s: %s", meta_blob, e)

  logger.info(
      "\n"
      "======================================================================\n"
      "⚡ TOTAL AGGREGATED TOKEN USAGE:\n"
      "   Input Tokens:  %d\n"
      "   Output Tokens: %d\n"
      "   Total Tokens:  %d\n"
      "======================================================================\n",
      totals["in_tokens"],
      totals["out_tokens"],
      totals["total_tokens"],
  )
  return totals


def _inject_token_metrics_into_html(
    html_path: str, token_totals: Optional[dict[str, int]]
) -> None:
  """Injects a Token Usage Summary card between the report title and finding count section."""
  if not os.path.exists(html_path) or not token_totals:
    return

  in_tokens = token_totals.get("in_tokens", 0)
  out_tokens = token_totals.get("out_tokens", 0)
  total_tokens = token_totals.get("total_tokens", 0)

  banner_html = f"""
  <div id="codemender-token-metrics-banner" style="background: white; border-radius: 8px; padding: 20px; margin-bottom: 25px; box-shadow: 0 2px 4px rgba(0,0,0,0.1); font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif;">
    <h3 style="margin-bottom: 12px; font-size: 0.95rem; color: #16213e; font-weight: 600; text-transform: uppercase; letter-spacing: 0.05em; display: flex; align-items: center; gap: 8px;">
      ⚡ LLM Token Usage Summary
    </h3>
    <div style="display: flex; gap: 40px; flex-wrap: wrap;">
      <div>
        <span style="font-size: 0.8rem; text-transform: uppercase; color: #6c757d; font-weight: 600; display: block; margin-bottom: 4px;">Input Tokens</span>
        <span style="font-size: 1.5rem; font-weight: 700; color: #0d6efd;">{in_tokens:,}</span>
      </div>
      <div>
        <span style="font-size: 0.8rem; text-transform: uppercase; color: #6c757d; font-weight: 600; display: block; margin-bottom: 4px;">Output Tokens</span>
        <span style="font-size: 1.5rem; font-weight: 700; color: #198754;">{out_tokens:,}</span>
      </div>
      <div>
        <span style="font-size: 0.8rem; text-transform: uppercase; color: #6c757d; font-weight: 600; display: block; margin-bottom: 4px;">Total Tokens</span>
        <span style="font-size: 1.5rem; font-weight: 700; color: #212529;">{total_tokens:,}</span>
      </div>
    </div>
  </div>
"""
  try:
    with open(html_path, "r", encoding="utf-8") as f:
      content = f.read()

    # 1. Target right before <div class="cards"> (inside main container, above count cards)
    match = re.search(r"(<div\s+class=[\"']cards[\"'][^>]*>)", content, re.IGNORECASE)
    if not match:
      # 2. Fallback: right after </header>
      match = re.search(r"(</header>)", content, re.IGNORECASE)
    if not match:
      # 3. Fallback: right after <h1> title tag
      match = re.search(
          r"(<h1[^>]*>.*?CodeMender Security Report.*?</h1>)",
          content,
          re.IGNORECASE | re.DOTALL,
      )
    if not match:
      # 4. Fallback to <body> tag
      match = re.search(r"(<body[^>]*>)", content, re.IGNORECASE)

    if match:
      if match.group(1).lower().startswith("<div"):
        # Insert BEFORE <div class="cards">
        pos = match.start()
        new_content = content[:pos] + banner_html + "\n  " + content[pos:]
      else:
        # Insert AFTER match tag
        pos = match.end()
        new_content = content[:pos] + "\n" + banner_html + content[pos:]
    else:
      new_content = banner_html + "\n" + content

    with open(html_path, "w", encoding="utf-8") as f:
      f.write(new_content)
    logger.info("Successfully injected Token Usage Summary into HTML report.")
  except Exception as e:  # pylint: disable=broad-exception-caught
    logger.warning("Failed to inject token usage into HTML report: %s", e)


def _generate_and_upload_report(
    repo_dir: str,
    scrubbed_env: dict[str, str],
    cm_binary: str,
    codemender_home: str,
    bucket_name: str,
    owner: str,
    repo_name: str,
    token_totals: Optional[dict[str, int]] = None,
) -> None:
  """Generates final HTML report using cm CLI and uploads it to GCS."""
  cli_version = os.environ.get("CODEMENDER_CLI_VERSION", "preview").lower()
  logger.info("Generating final consolidated HTML summary report...")
  report_cmd = build_cm_command(
      cm_binary, "report", extra_flags=["-f", "html"], cli_version=cli_version
  )
  report_res = run_command(
      report_cmd,
      cwd=repo_dir,
      env=scrubbed_env,
      check=False,
  )

  if report_res.returncode == 0:
    local_report_path = os.path.join(codemender_home, "reports/report.html")
    _inject_token_metrics_into_html(local_report_path, token_totals)
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

  # Aggregate Token Metrics (Preview Mode only)
  token_totals = None
  cli_version = os.environ.get("CODEMENDER_CLI_VERSION", "preview").lower()
  if cli_version == "preview":
    token_totals = _aggregate_token_metrics(
        workspace_dir, bucket_name, scan_id, worker_db_blobs
    )

  # 6. Generate final report and upload
  inject_codemender_config(repo_dir)

  # Delete SKIPPED_DUPLICATE and DISMISSED findings from local db to keep HTML report clean
  try:
    with closing(sqlite3.connect(base_db_path)) as conn:
      conn.execute(
          "DELETE FROM findings WHERE status IN ('SKIPPED_DUPLICATE',"
          " 'DISMISSED')"
      )
      conn.commit()
    logger.info(
        "Removed SKIPPED_DUPLICATE and DISMISSED findings from local state.db"
        " for report."
    )
  except sqlite3.Error as e:  # pylint: disable=broad-exception-caught
    logger.warning("Failed to remove SKIPPED_DUPLICATE findings from state.db: %s", e)

  cm_binary = shutil.which("cm") or "cm"
  _generate_and_upload_report(
      repo_dir,
      scrubbed_env,
      cm_binary,
      codemender_home,
      bucket_name,
      owner,
      repo_name,
      token_totals=token_totals,
  )

  logger.info("Stage 3 (Aggregate) completed successfully.")

