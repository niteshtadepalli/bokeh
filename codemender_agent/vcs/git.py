"""Git URL and repository manipulation utilities for CodeMender Agent."""

import base64
import logging

import os
import re
from typing import Tuple

logger = logging.getLogger("codemender-orchestrator")


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


def parse_repo_owner_and_name(repo_url: str) -> Tuple[str, str]:
  """Extracts (owner, repo_name) from a GitHub repository URL."""
  clean_url = sanitize_git_url(repo_url).rstrip("/")
  if clean_url.endswith(".git"):
    clean_url = clean_url[:-4]

  match = re.search(r"[:/]([^/]+)/([^/]+)$", clean_url)
  if match:
    return match.group(1), match.group(2)
  raise ValueError(f"Could not parse owner and repo name from URL: {repo_url}")


def generate_branch_name(vuln_type: str, fingerprint: str) -> str:
  """Generates a stable, idempotent Git branch name based on VulnType and Fingerprint hash."""
  vuln_clean = re.sub(
      r"[^a-zA-Z0-9\-_]", "-", (vuln_type or "vuln").strip().lower()
  )
  suffix = fingerprint[:8]
  return f"codemender/fix-{vuln_clean}-{suffix}"




def setup_local_git_excludes(repo_dir: str) -> None:
  """Appends CodeMender metadata paths to local git excludes to prevent staging them."""
  exclude_path = os.path.join(repo_dir, ".git", "info", "exclude")
  try:
    os.makedirs(os.path.dirname(exclude_path), exist_ok=True)
    # Read existing excludes to prevent duplicates
    existing_content = ""
    if os.path.exists(exclude_path):
      with open(exclude_path, "r") as f:
        existing_content = f.read()

    new_entries = []
    for entry in [".cm_project", ".exploit"]:
      if entry not in existing_content:
        new_entries.append(entry)

    if new_entries:
      with open(exclude_path, "a") as f:
        if existing_content and not existing_content.endswith("\n"):
          f.write("\n")
        f.write("\n".join(new_entries) + "\n")
      logger.info("Successfully added local git excludes: %s", new_entries)
  except Exception as e:
    logger.warning("Failed to configure local git excludes: %s", e)
