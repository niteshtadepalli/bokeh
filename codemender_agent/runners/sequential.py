"""Sequential single-task execution pipeline runner for CodeMender Agent."""

import logging
import os
import shutil
import sys
import time

from codemender_agent.codemender.cli import extract_session_id, parse_findings_json
from codemender_agent.codemender.db import get_finding_status, is_finding_verified
from codemender_agent.config import get_github_credentials, get_scrubbed_env, inject_codemender_config
from codemender_agent.storage import upload_and_sign_report
from codemender_agent.utils import free_port, run_command
from codemender_agent.vcs.git import generate_branch_name, get_git_auth_header, parse_repo_owner_and_name, sanitize_git_url, setup_local_git_excludes
from codemender_agent.vcs.github import check_remote_branch_exists, create_pull_request, get_default_branch

logger = logging.getLogger("codemender-orchestrator")


def run_sequential_pipeline() -> None:
  """Executes the single-task sequential scan, verify, fix, and PR pipeline."""
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
    try:
      curr_branch = run_command(
          ["git", "branch", "--show-current"], cwd=repo_dir
      ).stdout.strip()
    except Exception:
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

  run_command(["git", "config", "user.name", "CodeMender Agent"], cwd=repo_dir)
  run_command(
      ["git", "config", "user.email", "codemender-agent@google.com"],
      cwd=repo_dir,
  )

  setup_local_git_excludes(repo_dir)

  # Step 2: Initialize CodeMender CLI
  logger.info("Initializing CodeMender CLI...")
  cm_binary = shutil.which("cm") or "cm"

  try:
    run_command(
        [cm_binary, "init"],
        cwd=repo_dir,
        env=scrubbed_env,
        check=True,
    )
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

  # Step 3: Parse scan targets (separated by comma or semicolon) and run `cm find` sequentially
  scan_target_env = os.environ.get("CODEMENDER_SCAN_TARGET", ".")
  targets = []
  for part in scan_target_env.split(";"):
    for subpart in part.split(","):
      t = subpart.strip()
      if t:
        targets.append(t)
  if not targets:
    targets = ["."]

  logger.info("Starting CodeMender scanning for targets: %s", targets)

  for target in targets:
    logger.info("Running scan for target: '%s'...", target)
    try:
      find_res = run_command(
          [cm_binary, "find", target],
          cwd=repo_dir,
          env=scrubbed_env,
          check=True,
      )
      session_id = extract_session_id(find_res.stdout)
      if session_id:
        logger.info("Detected active scan session ID: %s for target '%s'", session_id, target)
      else:
        logger.warning("Could not extract active session ID from scan output for target '%s'.", target)
    except Exception as e:
      logger.critical(
          "CodeMender vulnerability scanning failed for target '%s'. Stopping pipeline: %s",
          target,
          e,
      )
      sys.exit(1)

  # Fetch all findings from local SQLite database across all target sessions
  report_cmd = [cm_binary, "report", "--format", "json"]

  report_res = run_command(
      report_cmd,
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
    fingerprint = (
        finding.get("Fingerprint")
        or finding.get("fingerprint")
        or finding.get("FindingID")
        or ""
    )


    branch_name = generate_branch_name(vuln_type, fingerprint)

    logger.info(
        "Processing finding %d/%d [ID: %s, VulnType: %s, Branch: %s]",
        idx,
        len(findings),
        finding_id,
        vuln_type,
        branch_name,
    )

    force_overwrite = (
        os.environ.get("CODEMENDER_FORCE_OVERWRITE", "false").lower() == "true"
    )
    if not force_overwrite and check_remote_branch_exists(
        clean_repo_url, token, branch_name, cwd=repo_dir
    ):
      logger.info(
          "Remote branch %s already exists. Skipping finding %s to prevent"

          " duplicate PRs.",
          branch_name,
          finding_id,
      )
      continue

    max_verify_attempts = 3
    verified = False
    state_db_path = os.path.expanduser("~/.codemender/state.db")

    for attempt in range(1, max_verify_attempts + 1):
      logger.info(
          "Verifying finding %s (Attempt %d/%d)...",
          finding_id,
          attempt,
          max_verify_attempts,
      )

      free_port(3000)
      free_port(3001)

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

      if verify_res.returncode == 0 and is_finding_verified(
          state_db_path, finding_id
      ):
        logger.info(
            "Successfully verified finding %s on attempt %d.",
            finding_id,
            attempt,
        )
        verified = True
        break
      else:
        logger.warning(
            "Attempt %d/%d failed to verify finding %s.",
            attempt,
            max_verify_attempts,
            finding_id,
        )
        if attempt < max_verify_attempts:
          logger.info("Retrying verification in 5 seconds...")
          time.sleep(5)

    if not verified:
      logger.error(
          "Verification failed for finding %s after %d attempts. Skipping fix.",
          finding_id,
          max_verify_attempts,
      )
      continue

    logger.info(
        "Applying fix for finding %s on %s branch...",
        finding_id,
        default_branch,
    )
    fix_res = run_command(
        [cm_binary, "fix", finding_id, "--yes"],
        cwd=repo_dir,
        env=scrubbed_env,
        check=False,
    )

    finding_status = get_finding_status(state_db_path, finding_id)
    if fix_res.returncode != 0 or finding_status != "FIXED":
      logger.warning(
          "Fix failed to apply successfully for finding %s (code %d, status"
          " %s). Skipping.",
          finding_id,
          fix_res.returncode,
          finding_status,
      )
      run_command(["git", "checkout", "-f", default_branch], cwd=repo_dir)
      run_command(
          ["git", "clean", "-fd", "-e", ".cm_project", "-e", ".exploit"],
          cwd=repo_dir,
      )
      continue

    status_res = run_command(["git", "status", "--porcelain"], cwd=repo_dir)
    if not status_res.stdout.strip():
      logger.warning(
          "Fix command executed but no file changes were detected for"
          " finding %s.",
          finding_id,
      )
      continue

    try:
      run_command(["git", "checkout", "-B", branch_name], cwd=repo_dir)
      run_command(["git", "add", "-u"], cwd=repo_dir)
      commit_msg = f"fix(security): resolve {vuln_type} in {file_path}"
      run_command(["git", "commit", "-m", commit_msg], cwd=repo_dir)

      logger.info("Pushing branch %s to remote...", branch_name)
      push_cmd = [
          "git",
          "-c",
          get_git_auth_header(token),
          "push",
      ]
      if force_overwrite:
        push_cmd.append("-f")
      push_cmd.extend(["origin", branch_name])

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
      run_command(["git", "checkout", "-f", default_branch], cwd=repo_dir)
      run_command(
          ["git", "clean", "-fd", "-e", ".cm_project", "-e", ".exploit"],
          cwd=repo_dir,
      )

  # Generate final HTML report
  logger.info("Generating final HTML summary report...")
  report_res = run_command(
      [cm_binary, "report", "-f", "html"],
      cwd=repo_dir,
      env=scrubbed_env,
      check=False,
  )
  if report_res.returncode == 0:
    local_report_path = os.path.expanduser("~/.codemender/reports/report.html")
    report_bucket = os.environ.get("CODEMENDER_REPORT_BUCKET")
    if report_bucket:
      dest_blob = f"reports/{owner}_{repo_name}/report_{time.strftime('%Y%m%d-%H%M%S')}.html"
      signed_url = upload_and_sign_report(
          local_report_path, report_bucket, dest_blob
      )
      if signed_url:
        logger.info(
            "\n"
            "======================================================================\n"
            "📊 CODEMENDER SUMMARY REPORT GENERATED:\n"
            "👉 %s\n"
            "======================================================================\n",
            signed_url,
        )
      else:
        logger.error("Failed to generate signed URL for the GCS report.")
    else:
      logger.info(
          "Local HTML report generated at %s (CODEMENDER_REPORT_BUCKET not set,"
          " skipped GCS upload).",
          local_report_path,
      )
  else:
    logger.error(
        "Failed to execute 'cm report -f html' (code %d).",
        report_res.returncode,
    )

  logger.info("CodeMender Orchestration completed successfully.")
