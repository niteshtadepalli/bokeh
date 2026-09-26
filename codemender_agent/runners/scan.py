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

"""Stage 1: Scan & Dispatch runner for CodeMender Agent."""

import json
import logging
import os
import shutil
import sqlite3
import sys
import tarfile
import time
from typing import Optional
import uuid

# CodeMender CLI JSON parser, version logging, and binary auto-update helpers
from codemender_agent.codemender.cli import ensure_cm_updated
from codemender_agent.codemender.cli import get_cm_default_model
from codemender_agent.codemender.cli import is_ci_gate_exit
from codemender_agent.codemender.cli import is_closed_finding_status
from codemender_agent.codemender.cli import log_cm_version
from codemender_agent.codemender.cli import parse_deep_scan_summary
from codemender_agent.codemender.cli import parse_findings_json
from codemender_agent.codemender.cli import stage_cm_binary_for_archive
from codemender_agent.runners.aggregate import _build_automation_details_id
from codemender_agent.runners.aggregate import _record_failure_marker
from codemender_agent.runners.aggregate import has_sarif_results
from codemender_agent.runners.aggregate import transform_json_to_sarif
# Configuration injection and credentials
from codemender_agent.config import OrchestratorConfig
from codemender_agent.config import get_github_credentials
from codemender_agent.config import get_scrubbed_env
from codemender_agent.config import inject_codemender_config
# Storage signed URL and upload utilities
from codemender_agent.storage import generate_signed_url
from codemender_agent.storage import upload_file_to_gcs
# BigQuery analytics telemetry (hard no-op unless CODEMENDER_BQ_DATASET is set)
from codemender_agent.telemetry import bigquery as bq_telemetry
from codemender_agent.utils import accumulate_model_token_usage
from codemender_agent.utils import build_cm_command
from codemender_agent.utils import render_token_usage_markdown
from codemender_agent.utils import resolve_command_model
from codemender_agent.utils import run_command
# Git branch derivation and diff hunk utilities
from codemender_agent.vcs.git import get_finding_branch_name
from codemender_agent.vcs.git import get_git_auth_header
from codemender_agent.vcs.git import get_pr_changed_lines
from codemender_agent.vcs.git import normalize_repo_relative_path
from codemender_agent.vcs.git import parse_repo_owner_and_name
from codemender_agent.vcs.git import sanitize_git_url
from codemender_agent.vcs.git import setup_local_git_excludes
# GitHub REST API check and deduplication helpers
from codemender_agent.vcs.github import STATUS_CONTEXT_PR
from codemender_agent.vcs.github import STATUS_CONTEXT_SCHEDULED
from codemender_agent.vcs.github import check_remote_branch_exists
from codemender_agent.vcs.github import delete_remote_branch
from codemender_agent.vcs.github import get_default_branch
from codemender_agent.vcs.github import is_duplicate_pr
from codemender_agent.vcs.github import post_commit_status
from codemender_agent.vcs.github import upload_sarif_to_code_scanning
# Opt-in Wiz SAST bridge (hard no-op unless enabled for this repository)
from codemender_agent.wiz.bridge import STATUS_NOT_ENABLED as WIZ_NOT_ENABLED
from codemender_agent.wiz.bridge import run_wiz_bridge
from codemender_agent.wiz.bridge import summary_line as wiz_summary_line
from codemender_agent.wiz.settings import WizBridgeSettings
from codemender_agent.wiz.settings import take_wiz_credentials

logger = logging.getLogger("codemender-orchestrator")


EXCLUDED_TAR_PATTERNS = {
    ".git",
    "__pycache__",
    ".venv",
    "node_modules",
    ".pytest_cache",
    ".codemender_cache",
}


def tar_filter(tarinfo: tarfile.TarInfo) -> Optional[tarfile.TarInfo]:
  """Filters out heavy/unnecessary metadata directories during workspace archiving."""
  base_name = os.path.basename(tarinfo.name)
  if base_name in EXCLUDED_TAR_PATTERNS or tarinfo.name.endswith(".pyc"):
    return None
  return tarinfo


def make_tarfile(output_filename: str, source_dir: str) -> None:
  """Creates a tar.gz archive of a directory excluding heavy cache/VCS paths."""
  with tarfile.open(output_filename, "w:gz") as tar:
    tar.add(source_dir, arcname=os.path.basename(source_dir), filter=tar_filter)


def _emit_github_output(
    outputs: dict[str, str],
    config: Optional[OrchestratorConfig] = None,
) -> None:
  """Emits outputs to GITHUB_OUTPUT environment file if running in GitHub Actions."""
  # 1. Resolve active GITHUB_OUTPUT environment file path
  cfg = config or OrchestratorConfig.from_env()
  output_file = cfg.github_output or os.environ.get("GITHUB_OUTPUT")
  if output_file:
    try:
      # 2. Append key-value pairs to the environment file
      with open(output_file, "a", encoding="utf-8") as f:
        for k, v in outputs.items():
          f.write(f"{k}={v}\n")
      logger.info("Successfully emitted GITHUB_OUTPUT: %s", outputs)
    except Exception as e:  # pylint: disable=broad-exception-caught
      # Log warning if writing to output file fails
      logger.warning("Failed to write to GITHUB_OUTPUT: %s", e)


def _write_clean_sarif_file(
    repo_dir: Optional[str],
    workspace_dir: str,
    repository: str = "",
    scan_target: str = "",
) -> str:
  """Generates a valid empty SARIF report when zero findings are discovered."""
  automation_id = _build_automation_details_id(
      repository=repository, scan_target=scan_target
  )
  clean_sarif = {
      "$schema": (
          "https://raw.githubusercontent.com/oasis-tcs/sarif-spec/master/Schemata/sarif-schema-2.1.0.json"
      ),
      "version": "2.1.0",
      "runs": [
          {
              "automationDetails": {
                  "id": automation_id,
              },
              "tool": {
                  "driver": {
                      "name": "CodeMender",
                      "semanticVersion": "1.0.0",
                      "rules": [],
                  }
              },
              "results": [],
          }
      ],
  }
  content = json.dumps(clean_sarif, indent=2)
  # Write clean SARIF to both repo_dir and workspace_dir for workflow actions
  for dest_dir in [repo_dir, workspace_dir]:
    if dest_dir and os.path.exists(dest_dir):
      sarif_path = os.path.join(dest_dir, "report.sarif")
      try:
        with open(sarif_path, "w", encoding="utf-8") as f:
          f.write(content)
        logger.info("Wrote clean SARIF report to %s", sarif_path)
      except Exception as e:  # pylint: disable=broad-exception-caught
        logger.warning(
            "Failed to write clean SARIF report to %s: %s", sarif_path, e
        )
  return os.path.join(workspace_dir, "report.sarif")


def _render_zero_findings_summary(
    owner: str,
    repo_name: str,
    target_sha: str,
    is_pr_scan: bool,
    config: Optional[OrchestratorConfig] = None,
    filtered_reasons: Optional[str] = None,
    token_totals: Optional[dict[str, dict[str, int]]] = None,
    wiz_note: Optional[str] = None,
) -> None:
  """Renders a reassuring Step Summary when zero findings are detected or all are ignored."""
  cfg = config or OrchestratorConfig.from_env()
  summary_file = cfg.github_step_summary or os.environ.get("GITHUB_STEP_SUMMARY")
  if not summary_file:
    return

  mode_desc = (
      "Pull Request Scan (Clean as You Code)"
      if is_pr_scan
      else "Nightly Repository Scan"
  )
  commit_desc = target_sha[:8] if target_sha else "HEAD"
  reason_note = (
      f"\n- **Note:** {filtered_reasons}"
      if filtered_reasons and not is_pr_scan
      else ""
  )

  token_md = render_token_usage_markdown(token_totals)
  token_section = f"\n{token_md}" if token_md else ""
  wiz_line = f"\n- **Wiz SAST:** {wiz_note}" if wiz_note else ""

  summary_md = f"""# 🛡️ CodeMender Security Remediation Summary

- **Repository:** `{owner}/{repo_name}`
- **Target Commit:** `{commit_desc}`
- **Execution Mode:** `{mode_desc}`{reason_note}{wiz_line}

### 📊 Remediation Overview

| Total Discovered | Remediated (Fixed) | Verified (Exploitable) | Pre-Existing Ignored | Skipped Duplicates | Other / Unfixed |
| :---: | :---: | :---: | :---: | :---: | :---: |
| 0 | 0 | 0 | 0 | 0 | 0 |

🎉 **No actionable security vulnerabilities detected.**
{token_section}"""
  try:
    with open(summary_file, "a", encoding="utf-8") as f:
      f.write(summary_md + "\n")
    logger.info("Wrote Zero-Findings Step Summary to %s", summary_file)
  except Exception as e:  # pylint: disable=broad-exception-caught
    logger.warning("Failed to write to GITHUB_STEP_SUMMARY (%s): %s", summary_file, e)


def _sync_repository(
    repo_url: str,
    token: str,
    repo_dir: str,
    workspace_dir: str,
    target_sha: Optional[str] = None,
    is_pr_scan: bool = False,
    pr_base_ref: Optional[str] = None,
) -> str:
  """Syncs the repository (clones if not exists, fetches and resets if exists).

  Returns:
    The target SHA of the repository after sync.
  """
  clean_repo_url = sanitize_git_url(repo_url)
  owner, repo_name = parse_repo_owner_and_name(clean_repo_url)

  logger.info("Syncing repository for scanning: %s", clean_repo_url)

  # 1. Fresh clone if repository directory does not already exist
  if not os.path.exists(os.path.join(repo_dir, ".git")):
    # Clean stale or non-git directory if present to prevent clone destination errors
    if os.path.exists(repo_dir):
      shutil.rmtree(repo_dir)

    # Configure git clone command with authorization header
    target_branch = (os.environ.get("CODEMENDER_TARGET_BRANCH") or "").strip()
    clone_cmd = [
        "git",
        "-c",
        get_git_auth_header(token),
        "clone",
    ]
    # In Nightly scans without target SHA use shallow clone depth=1; otherwise preserve full history
    if not is_pr_scan and not target_sha:
      clone_cmd.extend(["--depth", "1"])
      if target_branch:
        clone_cmd.extend(["--branch", target_branch])
    clone_cmd.extend([clean_repo_url, repo_dir])
    run_command(clone_cmd, cwd=workspace_dir)

    # In PR scans, fetch the target PR base reference branch from remote origin
    if is_pr_scan and pr_base_ref:
      fetch_base_cmd = [
          "git",
          "-c",
          get_git_auth_header(token),
          "fetch",
          "origin",
          pr_base_ref,
      ]
      run_command(fetch_base_cmd, cwd=repo_dir, check=False)
  else:
    # 2. Existing workspace: fetch latest branch state and reset working tree
    logger.info("Repository directory exists, fetching latest state...")
    target_branch = (os.environ.get("CODEMENDER_TARGET_BRANCH") or "").strip()
    try:
      curr_branch = target_branch or run_command(
          ["git", "branch", "--show-current"], cwd=repo_dir
      ).stdout.strip()
    except Exception:  # pylint: disable=broad-exception-caught
      curr_branch = ""
    if not curr_branch:
      curr_branch = get_default_branch(token, owner, repo_name)

    # Fetch latest commits from remote origin for current branch
    fetch_cmd = [
        "git",
        "-c",
        get_git_auth_header(token),
        "fetch",
        "origin",
        curr_branch,
    ]
    run_command(fetch_cmd, cwd=repo_dir, check=False)

    # In PR scans, ensure PR base reference branch is also fetched
    if is_pr_scan and pr_base_ref:
      fetch_base_cmd = [
          "git",
          "-c",
          get_git_auth_header(token),
          "fetch",
          "origin",
          pr_base_ref,
      ]
      run_command(fetch_base_cmd, cwd=repo_dir, check=False)

    if not is_pr_scan and not target_sha:
      # Force checkout and hard reset to clean up any untracked or modified artifacts
      run_command(["git", "checkout", "-f", curr_branch], cwd=repo_dir, check=False)
      run_command(
          ["git", "reset", "--hard", f"origin/{curr_branch}"], cwd=repo_dir, check=False
      )

  # 3. Checkout specific target commit SHA if requested, or determine default branch
  if target_sha:
    logger.info("Checking out explicit target SHA: %s", target_sha)
    fetch_target_cmd = [
        "git",
        "-c",
        get_git_auth_header(token),
        "fetch",
        "origin",
        target_sha,
    ]
    run_command(fetch_target_cmd, cwd=repo_dir, check=False)
    run_command(["git", "checkout", "-f", target_sha], cwd=repo_dir)
  elif not is_pr_scan:
    target_branch = (os.environ.get("CODEMENDER_TARGET_BRANCH") or "").strip()
    try:
      default_branch = target_branch or run_command(
          ["git", "branch", "--show-current"], cwd=repo_dir
      ).stdout.strip()
    except Exception:  # pylint: disable=broad-exception-caught
      default_branch = ""
    if not default_branch:
      default_branch = get_default_branch(token, owner, repo_name)

    logger.info("Using target/default branch: %s", default_branch)
    run_command(["git", "checkout", "-f", default_branch], cwd=repo_dir)

  # 4. Record and return the immutable target Git commit SHA
  target_sha_res = run_command(
      ["git", "rev-parse", "HEAD"], cwd=repo_dir
  ).stdout.strip()
  logger.info("Recorded target Git SHA: %s", target_sha_res)

  # 5. Configure local Git identity and exclusion patterns (.gitignore overrides)
  run_command(["git", "config", "user.name", "CodeMender Agent"], cwd=repo_dir)
  run_command(
      ["git", "config", "user.email", "codemender-agent@google.com"],
      cwd=repo_dir,
  )
  setup_local_git_excludes(repo_dir)

  return target_sha_res


def _init_codemender(
    repo_dir: str,
    scrubbed_env: dict[str, str],
    cm_binary: str,
    config: Optional[OrchestratorConfig] = None,
) -> None:
  """Initializes CodeMender CLI in the repository."""
  cfg = config or OrchestratorConfig.from_env()
  cli_version = cfg.cli_version
  logger.info("Initializing CodeMender CLI...")
  try:
    # 1. Run basic init to create .cm_project metadata
    init_cmd = build_cm_command(cm_binary, "init", cli_version=cli_version)
    run_command(
        init_cmd,
        cwd=repo_dir,
        env=scrubbed_env,
        check=True,
    )

    # 2. Inject repository and environment configs into ~/.codemender/config.yaml
    inject_codemender_config(repo_dir, config=cfg)

    # 3. Verify the initialization (validates build command in container environment)
    verify_init_cmd = build_cm_command(
        cm_binary, "init", extra_flags=["--verify"], cli_version=cli_version
    )
    run_command(
        verify_init_cmd,
        cwd=repo_dir,
        env=scrubbed_env,
        check=True,
    )

    # 4. Re-apply config injection so cm init --verify does not overwrite project_paths or sandbox settings
    inject_codemender_config(repo_dir, config=cfg)
  except Exception as e:  # pylint: disable=broad-exception-caught
    logger.critical("CodeMender initialization failed: %s", e)
    sys.exit(1)


def _scan_repository(
    repo_dir: str,
    scrubbed_env: dict[str, str],
    cm_binary: str,
    targets: list[str],
    config: Optional[OrchestratorConfig] = None,
    deep_summaries: Optional[list[dict[str, any]]] = None,
) -> tuple[list[dict[str, any]], dict[str, dict[str, int]]]:
  """Runs scan on targets with retries if no findings are found.

  Args:
    deep_summaries: When given, receives one parsed `cm find --deep` summary
      per target that ran in deep mode (files, failed files, total tokens).
  """
  cfg = config or OrchestratorConfig.from_env()
  cli_version = cfg.cli_version
  max_scan_attempts = int(os.environ.get("CODEMENDER_MAX_SCAN_ATTEMPTS", "1"))
  findings = []
  scan_token_usage: dict[str, dict[str, int]] = {}
  find_model = (
      cfg.find_model
      or resolve_command_model("find")
      or get_cm_default_model(cm_binary, env=scrubbed_env, cwd=repo_dir)
  )

  # Retry loop to account for transient cold-start, gRPC stream cancellation, or API rate-limit delays
  for attempt in range(1, max_scan_attempts + 1):
    logger.info("Running scan attempt %d/%d...", attempt, max_scan_attempts)
    had_find_error = False
    # 1. Execute 'cm find' across each configured target directory
    for target in targets:
      try:
        find_cmd = build_cm_command(
            cm_binary, "find", target, cli_version=cli_version
        )
        res = run_command(
            find_cmd,
            cwd=repo_dir,
            env=scrubbed_env,
            check=False,
        )
        # Capture and aggregate token usage telemetry
        token_usage = getattr(res, "token_usage", None)
        if isinstance(token_usage, dict):
          accumulate_model_token_usage(
              scan_token_usage, find_model, token_usage
          )
        find_stdout = getattr(res, "stdout", "")
        if not isinstance(find_stdout, str):
          find_stdout = ""
        deep_summary = parse_deep_scan_summary(find_stdout)
        if deep_summary:
          logger.info("Deep scan summary for %s: %s", target, deep_summary)
          if deep_summary.get("failed"):
            logger.warning(
                "Deep scan could not analyse %d of %d batches for %s; their"
                " files were not scanned.",
                deep_summary["failed"],
                deep_summary["batches"],
                target,
            )
          if deep_summaries is not None:
            deep_summaries.append({"target": target, **deep_summary})
        rc = getattr(res, "returncode", 0)
        if isinstance(rc, int) and is_ci_gate_exit(rc, find_stdout):
          logger.info(
              "cm find exited 1 for target %s because its CI gate matched"
              " blocking findings; treating this as findings present, not as"
              " a failed scan.",
              target,
          )
        elif isinstance(rc, int) and rc != 0:
          had_find_error = True
          logger.warning(
              "cm find returned non-zero exit code (%d) for target %s on attempt %d; checking state.db via cm report for incrementally saved findings...",
              rc,
              target,
              attempt,
          )
      except Exception as e:  # pylint: disable=broad-exception-caught
        had_find_error = True
        logger.warning(
            "Scan subprocess raised exception for target %s on attempt %d (%s); checking state.db via cm report...",
            target,
            attempt,
            e,
        )

    # 2. Retrieve structured vulnerability findings report in JSON format (cm find saves findings incrementally to state.db)
    try:
      # Construct 'cm report' command to export discovered findings as JSON
      report_cmd = build_cm_command(
          cm_binary,
          "report",
          extra_flags=["--format", "json"],
          cli_version=cli_version,
      )
      report_res = run_command(
          report_cmd,
          cwd=repo_dir,
          env=scrubbed_env,
          check=True,
          capture_stderr=False,
      )
      # Parse stdout JSON into structured Python dictionary list
      findings = parse_findings_json(report_res.stdout)
    except Exception as e:  # pylint: disable=broad-exception-caught
      logger.error("Failed to get report: %s", e)
      findings = []

    # 3. Exit retry loop early if findings were discovered or if scan completed cleanly with 0 findings
    if findings:
      logger.info("Found %d findings on attempt %d.", len(findings), attempt)
      break
    elif not had_find_error:
      logger.info("Scan completed cleanly with 0 findings on attempt %d.", attempt)
      break
    else:
      # Log retry status and delay before next attempt
      logger.warning(
          "No findings recovered after non-zero cm find exit on attempt %d/%d.",
          attempt,
          max_scan_attempts,
      )
      if attempt < max_scan_attempts:
        time.sleep(5)
      else:
        logger.error("All %d scan attempts failed with errors and 0 recovered findings.", max_scan_attempts)
        sys.exit(1)

  return findings, scan_token_usage


def _filter_findings(
    findings: list[dict[str, any]],
    repo_url: str,
    token: str,
    repo_dir: str,
    force_overwrite: bool,
    is_pr_scan: bool = False,
    pr_base_ref: Optional[str] = None,
) -> tuple[list[dict[str, any]], list[str], list[str]]:
  """Filters findings against PR modified hunks (if PR scan) and remote duplicates."""
  active_findings = []
  skipped_finding_ids = []
  ignored_finding_ids = []
  clean_repo_url = sanitize_git_url(repo_url)

  changed_lines = None
  if is_pr_scan and pr_base_ref:
    changed_lines = get_pr_changed_lines(repo_dir, pr_base_ref)
    if changed_lines is None:
      logger.warning(
          "PR Diff Hunk Analysis: git diff failed across all candidate targets for base ref '%s'."
          " Failing-open: retaining all findings without differential hunk suppression.",
          pr_base_ref,
      )
    else:
      logger.info(
          "PR Diff Hunk Analysis: Extracted modified lines across %d files from"
          " origin/%s...HEAD",
          len(changed_lines),
          pr_base_ref,
      )

  for finding in findings:
    finding_id = finding.get("FindingID")
    if not finding_id:
      logger.warning(
          "Finding record missing 'FindingID' (available keys: %s). Skipping."
          " This may indicate an upstream `cm report --format json` schema"
          " change.",
          sorted(finding.keys()),
      )
      continue
    # Only process findings that are still open: never send FIXED, DISMISSED
    # (for example rejected by verification) or false-positive findings to
    # the fix workers.
    status = finding.get("Status")
    if is_closed_finding_status(status):
      logger.info(
          "Skipping finding %s because its status is %s.", finding_id, status
      )
      continue

    file_path = normalize_repo_relative_path(
        finding.get("FilePath") or "unknown_file", repo_dir=repo_dir
    )
    try:
      start_line = int(finding.get("StartLine") or 0)
    except ValueError:
      start_line = 0
    try:
      end_line = int(finding.get("EndLine") or start_line)
    except ValueError:
      end_line = start_line

    # 1. PR Scoped Filtering: Differential check against changed hunks
    if is_pr_scan and pr_base_ref and changed_lines is not None:
      file_changed_lines = changed_lines.get(file_path, set())
      # Evaluate line ranges against PR modified hunks (start_line <= 0 falls back to {0})
      finding_lines = (
          set(range(start_line, max(start_line, end_line) + 1))
          if start_line > 0
          else {0}
      )
      intersection = file_changed_lines & finding_lines
      if not intersection:
        logger.info(
            "PR Differential Scan: Finding %s in %s (lines %d-%d) is"
            " pre-existing legacy debt (not modified in PR). Marking"
            " PRE_EXISTING_IGNORED.",
            finding_id,
            file_path,
            start_line,
            end_line,
        )
        finding["Status"] = "PRE_EXISTING_IGNORED"
        finding["status"] = "PRE_EXISTING_IGNORED"
        ignored_finding_ids.append(finding_id)
        continue
      else:
        logger.info(
            "PR Differential Scan: Finding %s in %s (lines %d-%d) matches PR modified lines %s. Retaining as active.",
            finding_id,
            file_path,
            start_line,
            end_line,
            sorted(intersection),
        )

    # 2. Universal Deduplication: Check if remote branch or PR already exists
    vuln_type = finding.get("VulnType") or "vulnerability"
    branch_name = get_finding_branch_name(file_path, vuln_type, start_line)

    if not force_overwrite and check_remote_branch_exists(
        clean_repo_url, token, branch_name, cwd=repo_dir
    ):
      has_active_pr = is_duplicate_pr(
          clean_repo_url,
          token,
          file_path,
          vuln_type,
          start_line,
          head_branch=branch_name,
      )
      if has_active_pr:
        logger.info(
            "Skipping finding %s as active PR exists for branch %s.",
            finding_id,
            branch_name,
        )
        finding["Status"] = "SKIPPED_DUPLICATE"
        finding["status"] = "SKIPPED_DUPLICATE"
        if isinstance(has_active_pr, str) and has_active_pr.startswith("http"):
          finding["pr_url"] = has_active_pr
        skipped_finding_ids.append(finding_id)
        continue
      else:
        logger.info(
            "Dead branch detected: %s exists on remote but has no active open PR."
            " Pruning dead branch to allow fresh remediation.",
            branch_name,
        )
        delete_remote_branch(clean_repo_url, token, branch_name, cwd=repo_dir)

    elif not force_overwrite:
      dup_pr = is_duplicate_pr(
          clean_repo_url,
          token,
          file_path,
          vuln_type,
          start_line,
          head_branch=branch_name,
      )
      if dup_pr:
        logger.info(
            "An open PR covering %s in %s near line %d already exists. Skipping"
            " finding %s.",
            vuln_type,
            file_path,
            start_line,
            finding_id,
        )
        finding["Status"] = "SKIPPED_DUPLICATE"
        finding["status"] = "SKIPPED_DUPLICATE"
        if isinstance(dup_pr, str) and dup_pr.startswith("http"):
          finding["pr_url"] = dup_pr
        skipped_finding_ids.append(finding_id)
        continue

    # Log active finding retained for Stage 2 remediation
    logger.info(
        "Retaining finding %s (%s in %s near line %d) for remediation.",
        finding_id,
        vuln_type,
        file_path,
        start_line,
    )
    active_findings.append(finding)

  return active_findings, skipped_finding_ids, ignored_finding_ids


def _partition_findings(
    active_findings: list[dict[str, any]],
    max_tasks: int,
) -> list[list[str]]:
  """Partitions active finding IDs into N worker buckets."""
  active_findings_count = len(active_findings)
  effective_max_tasks = max(1, max_tasks)
  num_workers = min(active_findings_count, effective_max_tasks, 10000)
  if num_workers <= 0:
    return []

  logger.info(
      "Partitioning %d active findings into %d workers (max_tasks=%d)",
      active_findings_count,
      num_workers,
      max_tasks,
  )

  # 1. Sort findings by FilePath to group same directory/file findings together
  sorted_findings = sorted(
      active_findings, key=lambda f: f.get("FilePath") or ""
  )
  sorted_ids = [f["FindingID"] for f in sorted_findings]

  # 2. Calculate even partition sizes across available workers
  base_size = active_findings_count // num_workers
  remainder = active_findings_count % num_workers
  sizes = [base_size + (1 if i < remainder else 0) for i in range(num_workers)]

  # 3. Chunk the sorted findings into worker partitions
  partitions = []
  start = 0
  for size in sizes:
    # Append slice to partitions list
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
    scan_token_usage: dict[str, dict[str, int]],
    skipped_duplicate_count: int,
    config: Optional[OrchestratorConfig] = None,
    cm_binary: Optional[str] = None,
    finding_prs: Optional[dict[str, str]] = None,
    started_at: Optional[str] = None,
    wiz_metadata: Optional[dict] = None,
    deep_summaries: Optional[list[dict]] = None,
) -> None:
  """Saves partitions and manifest, generates signed URLs, and uploads to GCS."""
  # Resolve active configuration instance
  cfg = config or OrchestratorConfig.from_env()

  # 1. Construct scan metadata dictionary with token telemetry and finding counts
  scan_metadata = {
      "scan_id": scan_id,
      "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
      "token_usage": scan_token_usage,
      "total_findings_count": active_findings_count + skipped_duplicate_count,
      "active_findings_count": active_findings_count,
      "skipped_duplicate_count": skipped_duplicate_count,
      "finding_prs": finding_prs or {},
  }
  # Stage 1 start time, so the aggregator can report true end-to-end duration
  # rather than just its own stage runtime.
  if started_at:
    scan_metadata["started_at"] = started_at
  # Wiz bridge outcome, read by the aggregator for the report and telemetry.
  if wiz_metadata:
    scan_metadata["wiz"] = wiz_metadata
  # Per-target deep-scan summaries (only present for `cm find --deep`).
  if deep_summaries:
    scan_metadata["deep_scan"] = deep_summaries
  force_verify = set((wiz_metadata or {}).get("force_verify_ids") or [])
  scan_meta_path = os.path.join(workspace_dir, "scan_metadata.json")
  with open(scan_meta_path, "w", encoding="utf-8") as f:
    json.dump(scan_metadata, f, indent=2)

  # 2. Upload scan_metadata.json to GCS bucket
  if not upload_file_to_gcs(
      scan_meta_path, bucket_name, f"scans/{scan_id}/scan_metadata.json"
  ):
    logger.critical("Failed to upload scan_metadata.json to GCS.")
    sys.exit(1)

  # 3. Stage active cm binary into ~/.codemender/bin/cm and archive ~/.codemender state directory
  codemender_home = os.path.expanduser("~/.codemender")
  stage_cm_binary_for_archive(codemender_home, cm_binary=cm_binary)
  tarball_path = os.path.join(workspace_dir, "workspace_base.tar.gz")
  logger.info("Archiving ~/.codemender to %s", tarball_path)
  make_tarfile(tarball_path, codemender_home)

  # 4. Upload workspace_base.tar.gz archive to GCS
  if not upload_file_to_gcs(
      tarball_path, bucket_name, f"scans/{scan_id}/workspace_base.tar.gz"
  ):
    logger.critical("Failed to upload base workspace archive to GCS.")
    sys.exit(1)

  # 5. Generate GET Signed URL for workers to download the base workspace
  base_workspace_blob = f"scans/{scan_id}/workspace_base.tar.gz"
  base_workspace_url = generate_signed_url(
      bucket_name,
      base_workspace_blob,
      expiration_days=cfg.intermediate_retention_days,
      method="GET",
  )

  partition_urls = []
  upload_urls = []
  metadata_urls = []

  # 6. Save each worker partition slice, upload it, and generate signed URLs
  for i, part_ids in enumerate(partitions):
    partition_data = {"partition_index": i, "finding_ids": part_ids}
    # Imported findings in this partition must be verified by the worker even
    # when verification is otherwise skipped.
    part_force_verify = [fid for fid in part_ids if fid in force_verify]
    if part_force_verify:
      partition_data["force_verify_ids"] = part_force_verify
    part_path = os.path.join(workspace_dir, f"partition_{i}.json")
    with open(part_path, "w", encoding="utf-8") as f:
      json.dump(partition_data, f, indent=2)

    part_blob = f"scans/{scan_id}/partition_{i}.json"
    if not upload_file_to_gcs(part_path, bucket_name, part_blob):
      logger.critical("Failed to upload partition file to GCS.")
      sys.exit(1)

    # Generate Signed URL for workers to download their partition
    part_url = generate_signed_url(
        bucket_name,
        part_blob,
        expiration_days=cfg.intermediate_retention_days,
        method="GET",
    )
    if not part_url:
      logger.critical("Failed to generate GET signed URL for partition %d.", i)
      sys.exit(1)
    partition_urls.append(part_url)

    # Generate Signed URL for workers to upload their mutated DB shard
    worker_db_blob = f"scans/{scan_id}/worker_{i}_state.db"
    upload_url = generate_signed_url(
        bucket_name,
        worker_db_blob,
        expiration_days=cfg.intermediate_retention_days,
        method="PUT",
        content_type="application/octet-stream",
    )
    if not upload_url:
      logger.critical("Failed to generate PUT signed URL for worker %d.", i)
      sys.exit(1)
    upload_urls.append(upload_url)

    # Generate Signed URL for workers to upload their token usage metadata JSON
    worker_meta_blob = f"scans/{scan_id}/worker_{i}_metadata.json"
    meta_put_url = generate_signed_url(
        bucket_name,
        worker_meta_blob,
        expiration_days=cfg.intermediate_retention_days,
        method="PUT",
        content_type="application/json",
    )
    if not meta_put_url:
      logger.critical(
          "Failed to generate PUT signed URL for worker %d metadata.", i
      )
      sys.exit(1)
    metadata_urls.append(meta_put_url)

  # 7. Construct manifest with all Signed URLs and upload to GCS
  manifest = {
      "findings_count": active_findings_count,
      "target_sha": target_sha,
      "base_workspace_url": base_workspace_url,
      "partition_urls": partition_urls,
      "upload_urls": upload_urls,
      "metadata_urls": metadata_urls,
  }
  manifest_path = os.path.join(workspace_dir, "manifest.json")
  with open(manifest_path, "w", encoding="utf-8") as f:
    json.dump(manifest, f, indent=2)
  if not upload_file_to_gcs(
      manifest_path, bucket_name, f"scans/{scan_id}/manifest.json"
  ):
    logger.critical("Failed to upload manifest.json to GCS.")
    sys.exit(1)

  # 8. Emit GitHub Actions matrix outputs for dynamic matrix orchestration
  matrix_json = (
      json.dumps(list(range(len(partitions)))) if partitions else "[0]"
  )
  _emit_github_output(
      {
          "matrix": matrix_json,
          "findings_count": str(active_findings_count),
          "target_sha": str(target_sha),
          "scan_id": str(scan_id),
      },
      config=cfg,
  )


def run_scan_pipeline() -> None:
  """Executes Stage 1: Scan repository, filter, partition, and upload state.

  The real work lives in `_run_scan_pipeline`; this wrapper exists purely so
  that every terminal path -- including the ten `sys.exit(1)` failure sites
  inside the body -- still produces exactly one `scan_runs` telemetry row.
  Placing the guard here rather than at each exit site keeps the failure
  accounting complete without scattering hooks through the pipeline.

  The guard re-raises whatever it caught, so exit codes are unchanged, and it
  is a hard no-op when telemetry is not configured.
  """
  ctx = bq_telemetry.ScanRunContext(stage="scan")
  with bq_telemetry.telemetry_run_guard(ctx):
    try:
      _run_scan_pipeline(ctx)
    except BaseException as exc:
      is_clean_exit = isinstance(exc, SystemExit) and exc.code in (0, None)
      if not is_clean_exit:
        try:
          cfg = OrchestratorConfig.from_env()
          _record_failure_marker(
              cfg.workspace_dir or os.getcwd(),
              cfg.gcs_bucket,
              ctx.scan_id or cfg.scan_id,
              "scan",
              ctx.target_sha or cfg.target_sha,
          )
          if (ctx.target_sha or cfg.target_sha) and ctx.repository and "/" in ctx.repository:
            token = cfg.github_token
            if not token:
              try:
                _, token = get_github_credentials(config=cfg)
              except Exception:  # pylint: disable=broad-exception-caught
                token = None
            if token:
              owner_part, repo_part = ctx.repository.split("/", 1)
              gate_ctx = (
                  STATUS_CONTEXT_PR
                  if cfg.is_pr_scan
                  else STATUS_CONTEXT_SCHEDULED
              )
              post_commit_status(
                  token=token,
                  owner=owner_part,
                  repo=repo_part,
                  sha=ctx.target_sha or cfg.target_sha,
                  state="error",
                  description="Scan failed during Stage 1.",
                  context=gate_ctx,
                  target_url=cfg.execution_url or None,
              )
        except Exception as status_err:  # pylint: disable=broad-exception-caught
          logger.warning("Failed to post Stage 1 error commit status: %s", status_err)
      raise


def _run_scan_pipeline(ctx: "bq_telemetry.ScanRunContext") -> None:
  """Stage 1 implementation. See `run_scan_pipeline` for the telemetry wrapper."""
  # Take the Wiz credentials out of the process environment before anything
  # can spawn a subprocess, so only wizcli itself can ever receive them.
  wiz_creds = take_wiz_credentials()
  wiz_settings = WizBridgeSettings.from_env()
  if not wiz_settings.enabled:
    ctx.wiz_status = WIZ_NOT_ENABLED
  config = OrchestratorConfig.from_env()
  scan_id = config.scan_id or f"scan_{time.strftime('%Y%m%d_%H%M%S')}_{uuid.uuid4().hex[:8]}"
  bucket_name = config.gcs_bucket

  # Seed telemetry context as early as possible so even an immediate
  # configuration failure below still produces an attributable FAILED row.
  ctx.apply_config(config)
  ctx.scan_id = scan_id

  # 1. Validate storage configuration when running in GCS mode
  if config.storage_mode == "gcs" and (not config.scan_id or not bucket_name):
    logger.critical(
        "CODEMENDER_SCAN_ID and CODEMENDER_GCS_BUCKET must be set when storage_mode is 'gcs'."
    )
    sys.exit(1)

  if not bucket_name:
    bucket_name = "default_bucket"

  # 2. Extract repository credentials and working directory paths
  repo_url, token = get_github_credentials(config=config)
  workspace_dir = config.workspace_dir or os.getcwd()
  clean_repo_url = sanitize_git_url(repo_url)
  owner, repo_name = parse_repo_owner_and_name(clean_repo_url)
  repo_dir = os.path.join(workspace_dir, repo_name)
  ctx.repository = f"{owner}/{repo_name}"
  ctx.repo_dir = repo_dir

  # 3. Synchronize repository and record the target commit SHA
  target_sha = _sync_repository(
      repo_url,
      token,
      repo_dir,
      workspace_dir,
      target_sha=config.target_sha,
      is_pr_scan=config.is_pr_scan,
      pr_base_ref=config.pr_base_ref,
  )
  ctx.target_sha = target_sha or ctx.target_sha

  if not config.is_pr_scan and target_sha and token:
    post_commit_status(
        token=token,
        owner=owner,
        repo=repo_name,
        sha=target_sha,
        state="pending",
        description="CodeMender scan in progress...",
        context=STATUS_CONTEXT_SCHEDULED,
        target_url=config.execution_url or None,
    )

  # 4. Initialize CodeMender CLI environment, self-update binary, and configure local cache paths
  scrubbed_env = get_scrubbed_env(repo_dir=repo_dir)
  cm_binary = ensure_cm_updated(
      shutil.which("cm") or "cm", env=scrubbed_env, cwd=repo_dir
  )
  # Capture the resolved version so analytics can correlate finding rates
  # against scanner upgrades.
  ctx.cm_version = log_cm_version(cm_binary, env=scrubbed_env, cwd=repo_dir)
  # The model columns have to name the model that actually ran. With no
  # override configured the scan uses the scanner's own built-in default, so
  # it is resolved here rather than left NULL -- otherwise every unoverridden
  # run, which is most of them, drops out of model comparisons entirely. The
  # lookup is cached and is repeated by the scan itself below, so this costs
  # nothing beyond the first call; it is still gated on telemetry being
  # configured so the failure-guard path stays cheap, and guarded so telemetry
  # can never be the thing that fails a scan.
  if bq_telemetry.telemetry_enabled():
    try:
      ctx.apply_default_model(
          get_cm_default_model(cm_binary, env=scrubbed_env, cwd=repo_dir)
      )
    except Exception as e:  # pylint: disable=broad-exception-caught
      logger.warning("Could not resolve the default model for telemetry: %s", e)
  _init_codemender(repo_dir, scrubbed_env, cm_binary, config=config)

  # 5. Parse scan targets (normalized to absolute paths to prevent sandbox mount errors)
  scan_target_env = config.scan_target
  targets = []
  for part in scan_target_env.split(";"):
    for subpart in part.split(","):
      t = subpart.strip()
      if t:
        abs_t = (
            t if os.path.isabs(t) else os.path.abspath(os.path.join(repo_dir, t))
        )
        targets.append(abs_t)
  if not targets:
    targets = [os.path.abspath(repo_dir)]

  # 6. Execute repository scan and accumulate token usage metrics
  deep_summaries: list[dict] = []
  findings, scan_token_usage = _scan_repository(
      repo_dir,
      scrubbed_env,
      cm_binary,
      targets,
      config=config,
      deep_summaries=deep_summaries,
  )

  # 6b. Opt-in Wiz SAST bridge: import eligible Wiz findings for mandatory
  #     verification. Never raises; a failure leaves CodeMender's own findings.
  wiz_result = run_wiz_bridge(
      settings=wiz_settings,
      creds=wiz_creds,
      repo_dir=repo_dir,
      cm_binary=cm_binary,
      cm_env=scrubbed_env,
      existing_findings=findings,
      cli_version=config.cli_version,
  )
  findings = wiz_result.findings
  wiz_metadata = wiz_result.to_metadata()
  ctx.apply_wiz(wiz_metadata)
  wiz_note = wiz_summary_line(wiz_metadata)

  # 7. Handle case where repository scan returns zero findings
  if not findings:
    logger.info("Zero findings confirmed after scanning. Exiting Stage 1.")
    # Generate schema-compliant clean SARIF for GitHub Code Scanning alert resolution
    sarif_path = _write_clean_sarif_file(
        repo_dir,
        workspace_dir,
        repository=f"{owner}/{repo_name}",
        scan_target=config.scan_target,
    )
    # Render clean Step Summary before exiting
    _render_zero_findings_summary(
        owner,
        repo_name,
        target_sha,
        config.is_pr_scan,
        config=config,
        token_totals=scan_token_usage,
        wiz_note=wiz_note,
    )
    # Build minimal manifest with findings_count = 0
    manifest = {"findings_count": 0, "target_sha": target_sha}
    manifest_path = os.path.join(workspace_dir, "manifest.json")
    with open(manifest_path, "w", encoding="utf-8") as f:
      json.dump(manifest, f, indent=2)
    # Upload zero findings manifest and clean SARIF / token usage to transit storage
    upload_file_to_gcs(
        manifest_path, bucket_name, f"scans/{scan_id}/manifest.json"
    )
    if sarif_path and os.path.exists(sarif_path):
      upload_file_to_gcs(
          sarif_path, bucket_name, f"scans/{scan_id}/report.sarif"
      )
    token_usage_path = os.path.join(workspace_dir, "token_usage.json")
    try:
      with open(token_usage_path, "w", encoding="utf-8") as f:
        json.dump(
            {
                "scan_id": scan_id,
                "generated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                "token_totals": scan_token_usage or {},
            },
            f,
            indent=2,
        )
      upload_file_to_gcs(
          token_usage_path, bucket_name, f"scans/{scan_id}/token_usage.json"
      )
    except Exception as e:  # pylint: disable=broad-exception-caught
      logger.warning("Failed to upload zero-findings token_usage.json: %s", e)
    # Emit zero findings output variables to GitHub Actions environment
    _emit_github_output(
        {
            "matrix": "[0]",
            "findings_count": "0",
            "target_sha": str(target_sha),
            "scan_id": str(scan_id),
        },
        config=config,
    )
    if not config.is_pr_scan and target_sha and token:
      post_commit_status(
          token=token,
          owner=owner,
          repo=repo_name,
          sha=target_sha,
          state="success",
          description="Scan complete: no active findings.",
          context=STATUS_CONTEXT_SCHEDULED,
          target_url=config.execution_url or None,
      )
      if sarif_path and os.path.exists(sarif_path):
        if has_sarif_results(sarif_path) or config.upload_empty_sarif:
          scan_ref = (
              f"refs/heads/{config.target_branch}"
              if config.target_branch
              else f"refs/heads/{get_default_branch(token, owner, repo_name)}"
          )
          upload_sarif_to_code_scanning(
              token=token,
              owner=owner,
              repo=repo_name,
              sarif_path=sarif_path,
              commit_sha=target_sha,
              ref=scan_ref,
          )
        else:
          logger.info(
              "Skipping GitHub Code Scanning SARIF upload because report.sarif contains 0 results "
              "(set CODEMENDER_UPLOAD_EMPTY_SARIF=true to auto-resolve existing alerts on empty runs)."
          )
    # Clean-repo terminal path. On the GCP path the coordinating workflow
    # short-circuits to completion when findings_count == 0, so Stage 3 never
    # runs -- this is the only opportunity to record that the scan happened.
    # report_uri stays NULL here because no HTML report is produced.
    ctx.total_findings_count = 0
    ctx.active_findings_count = 0
    ctx.skipped_duplicate_count = 0
    ctx.fixed_count = 0
    ctx.failed_fix_count = 0
    ctx.token_totals = scan_token_usage
    bq_telemetry.emit_scan_telemetry(ctx, status=bq_telemetry.STATUS_SUCCESS)
    sys.exit(0)

  # 8. Filter findings against PR differential hunks and deduplicate against open branches/PRs
  force_overwrite = config.force_overwrite
  active_findings, skipped_finding_ids, ignored_finding_ids = _filter_findings(
      findings,
      repo_url,
      token,
      repo_dir,
      force_overwrite,
      is_pr_scan=config.is_pr_scan,
      pr_base_ref=config.pr_base_ref,
  )

  # 9. Soft-delete skipped & ignored findings in local state.db for telemetry before archiving
  if skipped_finding_ids or ignored_finding_ids:
    db_path = os.path.expanduser("~/.codemender/state.db")
    if os.path.exists(db_path):
      try:
        # Open SQLite connection to record soft-deleted finding statuses
        conn = sqlite3.connect(db_path)
        cursor = conn.cursor()
        # Mark skipped duplicate findings in SQLite database
        for fid in skipped_finding_ids:
          cursor.execute(
              "UPDATE findings SET status = 'SKIPPED_DUPLICATE', muted = 1,"
              " mute_reason = 'Duplicate PR or branch already exists' WHERE"
              " finding_id = ?",
              (fid,),
          )
        # Mark pre-existing ignored findings in SQLite database
        for fid in ignored_finding_ids:
          # Execute soft-delete update query in local findings table
          cursor.execute(
              "UPDATE findings SET status = 'PRE_EXISTING_IGNORED', muted = 1,"
              " mute_reason = 'Pre-existing finding not touched in PR' WHERE"
              " finding_id = ?",
              (fid,),
          )
        conn.commit()
        conn.close()
        logger.info(
            "Dismissed %d skipped and %d ignored findings in local state.db.",
            len(skipped_finding_ids),
            len(ignored_finding_ids),
        )
      except Exception as e:  # pylint: disable=broad-exception-caught
        # Log warning if updating SQLite findings fails
        logger.warning("Failed to update findings in state.db: %s", e)

  # Compute active findings count after filtering
  active_findings_count = len(active_findings)
  logger.info("Active findings after filtering: %d", active_findings_count)

  # Detect total silent finding loss, which indicates upstream schema drift
  if (
      findings
      and active_findings_count == 0
      and not skipped_finding_ids
      and not ignored_finding_ids
  ):
    logger.error(
        "Parsed %d findings but retained 0 active with 0 skipped and 0 ignored."
        " The `cm report --format json` schema is likely unrecognized.",
        len(findings),
    )

  skipped_finding_prs = {
      str(f.get("FindingID") or f.get("finding_id")): str(f.get("pr_url"))
      for f in findings
      if isinstance(f, dict)
      and (f.get("FindingID") or f.get("finding_id")) in skipped_finding_ids
      and f.get("pr_url")
  }

  # 10. Handle case where all findings were filtered out
  if active_findings_count == 0:
    logger.info("Zero active findings after filtering. Exiting Stage 1.")
    if not config.is_pr_scan and skipped_finding_ids:
      # Synthesize rich SARIF with underReview suppressions so existing open alerts stay tracked on GitHub
      sarif_data = transform_json_to_sarif(
          findings=findings,
          repo_dir=repo_dir,
          skipped_finding_ids=set(skipped_finding_ids),
          finding_prs=skipped_finding_prs,
          is_pr_scan=False,
          repository=f"{owner}/{repo_name}",
          scan_target=config.scan_target,
      )
      sarif_path = os.path.join(workspace_dir, "report.sarif")
      for dest_dir in [repo_dir, workspace_dir]:
        if dest_dir and os.path.exists(dest_dir):
          try:
            with open(os.path.join(dest_dir, "report.sarif"), "w", encoding="utf-8") as f:
              json.dump(sarif_data, f, indent=2)
          except Exception as e:  # pylint: disable=broad-exception-caught
            logger.warning("Failed to write suppressed SARIF to %s: %s", dest_dir, e)
    else:
      # Generate schema-compliant clean SARIF for GitHub Code Scanning alert resolution
      sarif_path = _write_clean_sarif_file(
          repo_dir,
          workspace_dir,
          repository=f"{owner}/{repo_name}",
          scan_target=config.scan_target,
      )
    filtered_reason = (
        None
        if config.is_pr_scan
        else (
            f"{len(ignored_finding_ids)} pre-existing findings and"
            f" {len(skipped_finding_ids)} duplicate branches/PRs dismissed."
        )
    )
    # Render clean Step Summary before exiting
    _render_zero_findings_summary(
        owner,
        repo_name,
        target_sha,
        config.is_pr_scan,
        config=config,
        filtered_reasons=filtered_reason,
        token_totals=scan_token_usage,
        wiz_note=wiz_note,
    )
    # Build minimal manifest with findings_count = 0
    manifest = {"findings_count": 0, "target_sha": target_sha}
    manifest_path = os.path.join(workspace_dir, "manifest.json")
    with open(manifest_path, "w", encoding="utf-8") as f:
      json.dump(manifest, f, indent=2)
    # Upload filtered zero findings manifest and reports to transit storage
    upload_file_to_gcs(
        manifest_path, bucket_name, f"scans/{scan_id}/manifest.json"
    )
    if sarif_path and os.path.exists(sarif_path):
      upload_file_to_gcs(
          sarif_path, bucket_name, f"scans/{scan_id}/report.sarif"
      )
    token_usage_path = os.path.join(workspace_dir, "token_usage.json")
    try:
      with open(token_usage_path, "w", encoding="utf-8") as f:
        json.dump(
            {
                "scan_id": scan_id,
                "generated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                "token_totals": scan_token_usage or {},
            },
            f,
            indent=2,
        )
      upload_file_to_gcs(
          token_usage_path, bucket_name, f"scans/{scan_id}/token_usage.json"
      )
    except Exception as e:  # pylint: disable=broad-exception-caught
      logger.warning("Failed to upload filtered zero-findings token_usage.json: %s", e)
    # Emit zero findings output variables to GitHub Actions environment
    _emit_github_output(
        {
            "matrix": "[0]",
            "findings_count": "0",
            "target_sha": str(target_sha),
            "scan_id": str(scan_id),
        },
        config=config,
    )
    if not config.is_pr_scan and target_sha and token:
      post_commit_status(
          token=token,
          owner=owner,
          repo=repo_name,
          sha=target_sha,
          state="success",
          description="Scan complete: no active findings.",
          context=STATUS_CONTEXT_SCHEDULED,
          target_url=config.execution_url or None,
      )
      if sarif_path and os.path.exists(sarif_path):
        if has_sarif_results(sarif_path) or config.upload_empty_sarif:
          scan_ref = (
              f"refs/heads/{config.target_branch}"
              if config.target_branch
              else f"refs/heads/{get_default_branch(token, owner, repo_name)}"
          )
          upload_sarif_to_code_scanning(
              token=token,
              owner=owner,
              repo=repo_name,
              sarif_path=sarif_path,
              commit_sha=target_sha,
              ref=scan_ref,
          )
        else:
          logger.info(
              "Skipping GitHub Code Scanning SARIF upload because report.sarif contains 0 results "
              "(set CODEMENDER_UPLOAD_EMPTY_SARIF=true to auto-resolve existing alerts on empty runs)."
          )
    # All-findings-filtered terminal path (duplicates / pre-existing). Stage 3
    # is likewise skipped here, so record the run now. Keeping the raw and
    # active counts distinct is what lets analytics separate "genuinely clean"
    # from "everything was already tracked elsewhere".
    ctx.total_findings_count = len(findings)
    ctx.active_findings_count = 0
    ctx.skipped_duplicate_count = len(skipped_finding_ids) + len(ignored_finding_ids)
    ctx.fixed_count = 0
    ctx.failed_fix_count = 0
    ctx.token_totals = scan_token_usage
    bq_telemetry.emit_scan_telemetry(ctx, status=bq_telemetry.STATUS_SUCCESS)
    sys.exit(0)

  # 11. Partition findings into balanced worker buckets
  max_tasks = config.max_tasks
  partitions = _partition_findings(active_findings, max_tasks)

  # 12. Save partitioned manifests, archive workspace, generate signed URLs, and upload
  _save_and_upload_state(
      partitions,
      active_findings_count,
      target_sha,
      workspace_dir,
      bucket_name,
      scan_id,
      scan_token_usage,
      len(skipped_finding_ids) + len(ignored_finding_ids),
      config=config,
      cm_binary=cm_binary,
      finding_prs=skipped_finding_prs,
      started_at=ctx.started_at,
      wiz_metadata=wiz_metadata,
      deep_summaries=deep_summaries,
  )

  logger.info("Stage 1 (Scan) completed successfully.")
