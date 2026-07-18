#!/usr/bin/env python3
"""CodeMender Orchestrator Entrypoint.

Decentralized, automated orchestration script that runs within a team's secure
infrastructure (e.g., Cloud Run Job) to validate and fix security
vulnerabilities using the CodeMender CLI and open GitHub Pull Requests.
"""

import os

# Re-export all public symbols for 100% backward compatibility
from codemender_agent.codemender.cli import extract_session_id, parse_findings_json
from codemender_agent.codemender.db import get_finding_status, is_finding_verified
from codemender_agent.config import SENSITIVE_ENV_VARS, get_github_credentials, get_scrubbed_env, inject_codemender_config
from codemender_agent.runners.sequential import run_sequential_pipeline
from codemender_agent.storage import DummyStorage, storage, upload_and_sign_report
from codemender_agent.utils import free_port, retry_on_exception, run_command
from codemender_agent.vcs.git import enforce_https_url, generate_branch_name, get_git_auth_header, parse_repo_owner_and_name, sanitize_git_url, setup_local_git_excludes
from codemender_agent.vcs.github import _fetch_default_branch_via_api, _get_branch_via_api, check_remote_branch_exists, create_pull_request, get_default_branch

__all__ = [
    "SENSITIVE_ENV_VARS",
    "DummyStorage",
    "_fetch_default_branch_via_api",
    "_get_branch_via_api",
    "check_remote_branch_exists",
    "create_pull_request",
    "enforce_https_url",
    "extract_session_id",
    "free_port",
    "generate_branch_name",
    "get_default_branch",
    "get_git_auth_header",
    "get_github_credentials",
    "get_finding_status",
    "get_scrubbed_env",
    "inject_codemender_config",
    "is_finding_verified",
    "main",
    "parse_findings_json",
    "parse_repo_owner_and_name",
    "retry_on_exception",
    "run_command",
    "run_sequential_pipeline",
    "sanitize_git_url",
    "setup_local_git_excludes",
    "storage",
    "upload_and_sign_report",
]


def main() -> None:
  """Main execution entrypoint for CodeMender Orchestrator."""
  run_mode = os.environ.get("CODEMENDER_RUN_MODE", "sequential").lower()

  if run_mode == "sequential":
    run_sequential_pipeline()
  else:
    # Default fallback to sequential pipeline
    run_sequential_pipeline()


if __name__ == "__main__":
  main()
