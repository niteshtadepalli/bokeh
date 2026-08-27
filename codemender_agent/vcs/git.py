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

"""Git URL and repository manipulation utilities for CodeMender Agent."""

import base64
import hashlib
import logging
import os
import re
from typing import Optional, Tuple

from codemender_agent.utils import retry_on_exception

logger = logging.getLogger("codemender-orchestrator")


def normalize_repo_relative_path(path: str, repo_dir: Optional[str] = None) -> str:
  """Normalizes a file path to be strictly repository-relative with forward slashes and no leading './'."""
  if not path:
    return ""
  p = path.strip().replace("\\", "/")
  if repo_dir and os.path.isabs(p):
    try:
      p = os.path.relpath(p, repo_dir).replace("\\", "/")
    except ValueError:
      pass
  p = re.sub(r"^\.?/+", "", p)
  return p


def compute_finding_fingerprint(
    file_path: str, vuln_type: str, start_line: int
) -> str:
  """Computes a deterministic 8-character SHA256 fingerprint for a finding."""
  norm_path = normalize_repo_relative_path(file_path)
  norm_type = (vuln_type or "vulnerability").strip().lower()
  raw_hash_str = f"{norm_path}|{norm_type}|{start_line}"
  return hashlib.sha256(raw_hash_str.encode("utf-8")).hexdigest()[:8]


def get_finding_branch_name(
    file_path: str, vuln_type: str, start_line: int
) -> str:
  """Generates a canonical branch name for a finding using its deterministic fingerprint."""
  fp = compute_finding_fingerprint(file_path, vuln_type, start_line)
  return generate_branch_name(vuln_type, fp)


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




def clean_workspace(repo_dir: str, exclude_dirs: Optional[Tuple[str, ...]] = None) -> None:
  """Resets working directory and cleans untracked files while preserving CLI metadata directories."""
  if exclude_dirs is None:
    exclude_dirs = (".cm_project", ".exploit")

  cmd = ["git", "clean", "-fd"]
  for ex in exclude_dirs:
    cmd.extend(["-e", ex])

  from codemender_agent.utils import run_command
  run_command(cmd, cwd=repo_dir, check=False)


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


def get_pr_changed_lines(repo_dir: str, base_ref: str) -> Optional[dict[str, set[int]]]:
  """Parses Unified Diff hunks to extract modified line numbers per file.

  Runs 'git diff -U0 origin/<base_ref>...HEAD' (falling back to '<base_ref>...HEAD' or 'origin/<base_ref>')
  and parses the diff hunk headers (@@ -old_start,old_count +new_start,new_count @@).

  Returns:
    Dict mapping repository-relative file paths to sets of 1-based modified line numbers,
    or None if git diff execution failed across all candidate targets.
  """
  from codemender_agent.utils import run_command

  clean_base = base_ref.strip()
  if clean_base.startswith("refs/heads/"):
    clean_base = clean_base[11:]

  diff_targets = [
      f"origin/{clean_base}...HEAD",
      f"{clean_base}...HEAD",
      f"origin/{clean_base}",
      clean_base,
  ]
  diff_output: Optional[str] = None
  for target in diff_targets:
    try:
      res = run_command(
          ["git", "diff", "-U0", target], cwd=repo_dir, check=False
      )
      if res.returncode == 0:
        diff_output = res.stdout
        break
    except Exception:  # pylint: disable=broad-exception-caught
      pass

  if diff_output is None:
    return None

  changed_lines: dict[str, set[int]] = {}
  if not diff_output:
    return changed_lines

  current_file: Optional[str] = None
  for line in diff_output.splitlines():
    if line.startswith("+++ b/"):
      current_file = normalize_repo_relative_path(line[6:].strip())
      if current_file and current_file not in changed_lines:
        changed_lines[current_file] = set()
    elif line.startswith("+++ /dev/null"):
      current_file = None
    elif line.startswith("@@ ") and current_file:
      # Parse hunk header: @@ -old_start,old_count +new_start,new_count @@
      # or @@ -old_start +new_start @@
      match = re.search(r"@@ -\d+(?:,\d+)? \+(\d+)(?:,(\d+))? @@", line)
      if match:
        new_start = int(match.group(1))
        new_count = int(match.group(2)) if match.group(2) is not None else 1
        if new_count == 0:
          changed_lines[current_file].add(new_start)
        else:
          for l in range(new_start, new_start + new_count):
            changed_lines[current_file].add(l)

  return changed_lines


@retry_on_exception(max_tries=3, initial_delay=2, backoff_factor=2)
def push_branch_to_remote(
    repo_dir: str,
    token: str,
    branch_name: str,
    force: bool = False,
) -> None:
  """Pushes a local branch to origin with exponential backoff retries."""
  push_cmd = ["git", "-c", get_git_auth_header(token), "push"]
  if force:
    push_cmd.append("-f")
  push_cmd.extend(["origin", branch_name])
  from codemender_agent.utils import run_command
  run_command(push_cmd, cwd=repo_dir, check=True)
