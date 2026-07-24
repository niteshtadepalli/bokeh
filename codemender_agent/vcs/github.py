"""GitHub REST API integration for CodeMender Agent."""

import logging
from typing import Optional

from codemender_agent.utils import retry_on_exception, run_command
from codemender_agent.vcs.git import get_git_auth_header, parse_repo_owner_and_name, sanitize_git_url
import requests

logger = logging.getLogger("codemender-orchestrator")


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


@retry_on_exception(max_tries=5, initial_delay=3, backoff_factor=2)
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
