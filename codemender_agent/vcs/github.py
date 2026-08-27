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

"""GitHub REST API integration for CodeMender Agent."""

import logging
import re
from typing import Optional

import requests
from codemender_agent.utils import retry_on_exception, run_command
from codemender_agent.vcs.git import get_git_auth_header, parse_repo_owner_and_name, sanitize_git_url

logger = logging.getLogger("codemender-orchestrator")


@retry_on_exception(max_tries=3)
def _get_branch_via_api(
    owner: str, repo: str, branch_name: str, token: str
) -> Optional[bool]:
  """Triggers GitHub branch status checks with raise_for_status validation."""
  # 1. Return None for fake tokens in mock/unit testing environments
  if token == "fake-token":
    return None
  # 2. Build HTTP request headers with authorization bearer
  headers = {
      "Authorization": f"Bearer {token}",
      "Accept": "application/vnd.github+json",
      "X-GitHub-Api-Version": "2022-11-28",
  }
  api_url = (
      f"https://api.github.com/repos/{owner}/{repo}/branches/{branch_name}"
  )
  # 3. Query GitHub REST API endpoint for branch existence
  resp = requests.get(api_url, headers=headers, timeout=10)
  if resp.status_code == 200:
    return True
  elif resp.status_code == 404:
    return False
  # Trigger retry decorator on server error or rate limiting
  resp.raise_for_status()
  return None


def check_remote_branch_exists(
    repo_url: str, token: str, branch_name: str, cwd: Optional[str] = None
) -> bool:
  """Checks if a branch already exists on the remote repository."""
  sanitized_url = sanitize_git_url(repo_url)
  # 1. Attempt O(1) branch existence check via GitHub REST API
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

  # 2. Fallback to git ls-remote CLI command if API is inaccessible or rate-limited
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
  # 1. Fallback to main branch for mock token in test suites
  if token == "fake-token":
    return "main"
  # 2. Build GitHub API request headers
  headers = {
      "Authorization": f"Bearer {token}",
      "Accept": "application/vnd.github+json",
      "X-GitHub-Api-Version": "2022-11-28",
  }
  # 3. Query repository metadata endpoint to get repository default branch
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
def _check_duplicate_pr_api(
    repo_url: str,
    token: str,
    file_path: str,
    vuln_type: str,
    start_line: int,
    head_branch: Optional[str] = None,
) -> bool:
  """Checks GitHub API for existing duplicate PRs traversing pagination headers."""
  if token == "fake-token":
    return False
  sanitized_url = sanitize_git_url(repo_url)
  owner, repo = parse_repo_owner_and_name(sanitized_url)
  headers = {
      "Authorization": f"Bearer {token}",
      "Accept": "application/vnd.github+json",
      "X-GitHub-Api-Version": "2022-11-28",
  }

  # 1. If head_branch is specified, perform O(1) targeted search first
  if head_branch:
    target_url = f"https://api.github.com/repos/{owner}/{repo}/pulls?head={owner}:{head_branch}&state=open"
    resp = requests.get(target_url, headers=headers, timeout=15)
    if resp.status_code == 403 and any(
        msg in resp.text.lower() for msg in ["rate limit", "abuse detection"]
    ):
      resp.raise_for_status()
    # Check if a pull request exists matching this exact head branch
    if resp.status_code == 200:
      prs = resp.json()
      if isinstance(prs, list) and len(prs) > 0:
        logger.info(
            "Found existing open PR targeting head branch %s: %s",
            head_branch,
            prs[0].get("html_url"),
        )
        return True

  # 2. Paginate over open pull requests to check finding descriptions
  url: Optional[str] = (
      f"https://api.github.com/repos/{owner}/{repo}/pulls?state=open&per_page=100"
  )

  with requests.Session() as session:
    while url:
      # Fetch page of open pull requests
      resp = session.get(url, headers=headers, timeout=15)
      if resp.status_code == 403 and any(
          msg in resp.text.lower() for msg in ["rate limit", "abuse detection"]
      ):
        resp.raise_for_status()
      resp.raise_for_status()

      prs = resp.json()
      if not isinstance(prs, list):
        break

      # 3. Check each open PR for matching vulnerability signatures
      for pr in prs:
        if head_branch and pr.get("head", {}).get("ref") == head_branch:
          return True
        body = pr.get("body") or ""
        # Match PR body against vulnerability metadata and target file path
        if "CodeMender Security Fix" in body and file_path in body and vuln_type in body:
          match = re.search(r"\*\*Start Line\*\*:\s*(\d+)", body, re.IGNORECASE)
          if match:
            existing_line = int(match.group(1))
            # Match within 15 lines of original vulnerability location
            if abs(existing_line - start_line) <= 15:
              logger.info(
                  "Found existing PR (%s) covering %s in %s near line %d.",
                  pr.get("html_url"),
                  vuln_type,
                  file_path,
                  start_line,
              )
              return True

      # 4. Traverse next page link if present in Link header
      url = resp.links.get("next", {}).get("url")

  return False


def is_duplicate_pr(
    repo_url: str,
    token: str,
    file_path: str,
    vuln_type: str,
    start_line: int,
    head_branch: Optional[str] = None,
) -> bool:
  """Checks if an open PR already exists for the same vulnerability near the same line."""
  try:
    return _check_duplicate_pr_api(
        repo_url, token, file_path, vuln_type, start_line, head_branch=head_branch
    )
  except Exception as e:
    logger.warning(
        "GitHub API check for duplicate PR failed after retries (%s), assuming no duplicate PR.",
        e,
    )
    return False


@retry_on_exception(max_tries=5, initial_delay=3, backoff_factor=2)
def create_pr_comment(
    token: str,
    owner: str,
    repo: str,
    pr_number: int,
    body: str,
) -> Optional[str]:
  """Posts a Markdown review comment on a Pull Request (or Issue)."""
  # 1. Handle mock GitHub token in test environments
  if token == "fake-token":
    logger.info("Mock GitHub token detected ('fake-token'), simulating PR comment.")
    return f"https://github.com/{owner}/{repo}/issues/{pr_number}#issuecomment-1"

  # 2. Build comment API URL and authorization headers
  url = f"https://api.github.com/repos/{owner}/{repo}/issues/{pr_number}/comments"
  headers = {
      "Authorization": f"Bearer {token}",
      "Accept": "application/vnd.github+json",
      "X-GitHub-Api-Version": "2022-11-28",
  }
  payload = {"body": body}

  # 3. Post review comment payload to GitHub Issue/PR comments API
  resp = requests.post(url, headers=headers, json=payload, timeout=15)
  if resp.status_code == 403 and any(
      msg in resp.text.lower() for msg in ["rate limit", "abuse detection"]
  ):
    # Retry on rate limiting or abuse detection triggers
    resp.raise_for_status()
  # Validate response status code
  resp.raise_for_status()
  comment_url = resp.json().get("html_url")
  logger.info("Successfully posted comment on PR #%d: %s", pr_number, comment_url)
  return comment_url


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
  # 1. Handle mock token in test suites
  if token == "fake-token":
    logger.info("Mock GitHub token detected ('fake-token'), simulating PR creation.")
    return f"https://github.com/{owner}/{repo}/pull/1"

  # 2. Prepare PR creation payload with title, body, head branch, and base branch
  url = f"https://api.github.com/repos/{owner}/{repo}/pulls"
  headers = {
      "Authorization": f"Bearer {token}",
      "Accept": "application/vnd.github+json",
      "X-GitHub-Api-Version": "2022-11-28",
  }
  payload = {
      "title": title,
      "body": body,
      "head": head_branch,
      "base": base_branch,
  }
  # 3. Submit Pull Request creation request
  resp = requests.post(url, headers=headers, json=payload, timeout=15)

  # Gracefully intercept HTTP 422 "PR already exists" validation failure
  if resp.status_code == 422:
    try:
      resp_data = resp.json()
      errors = resp_data.get("errors") or []
      messages = [e.get("message") or "" for e in errors if isinstance(e, dict)]
      top_msg = resp_data.get("message") or ""
      if any(
          "already exists" in msg for msg in messages
      ) or "already exists" in top_msg:
        logger.info(
            "A Pull Request already exists on GitHub for branch %s. Skipping PR"
            " creation.",
            head_branch,
        )
        return "EXISTING_PR"
    except Exception:
      pass

  if resp.status_code == 403 and any(
      msg in resp.text.lower() for msg in ["rate limit", "abuse detection"]
  ):
    resp.raise_for_status()
  resp.raise_for_status()  # Throws HTTPError to trigger backoff retry decorator
  pr_url = resp.json().get("html_url")
  logger.info("Successfully created Pull Request: %s", pr_url)
  return pr_url
