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

import json
import logging
import re
import shutil
import subprocess
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple

from codemender_agent.utils import (
    build_cm_command,
    extract_json_from_output,
    run_command,
)

logger = logging.getLogger("codemender-orchestrator")


@dataclass
class TokenUsage:
  """Container for accumulated token usage metrics."""

  in_tokens: int = 0
  out_tokens: int = 0
  total_tokens: int = 0

  def add(self, other: "TokenUsage") -> None:
    self.in_tokens += other.in_tokens
    self.out_tokens += other.out_tokens
    self.total_tokens += other.total_tokens

  def to_dict(self) -> Dict[str, int]:
    return {
        "in_tokens": self.in_tokens,
        "out_tokens": self.out_tokens,
        "total_tokens": self.total_tokens,
    }


class CodeMenderCLIAdapter:
  """Adapter isolating CodeMender CLI command construction, execution, and output harvesting."""

  def __init__(
      self, binary_path: Optional[str] = None, cli_version: str = "preview"
  ):
    self.binary_path = binary_path or shutil.which("cm") or "cm"
    self.cli_version = cli_version

  def execute(
      self,
      action: str,
      target_or_id: Optional[str] = None,
      extra_flags: Optional[List[str]] = None,
      cwd: Optional[str] = None,
      env: Optional[Dict[str, str]] = None,
      check: bool = True,
      capture_stderr: bool = True,
  ) -> Tuple[subprocess.CompletedProcess, TokenUsage]:
    """Builds and executes a CodeMender CLI command, extracting token usage metrics."""
    cmd = build_cm_command(
        self.binary_path,
        action,
        target_or_id=target_or_id,
        extra_flags=extra_flags,
        cli_version=self.cli_version,
    )
    res = run_command(
        cmd, cwd=cwd, env=env, check=check, capture_stderr=capture_stderr
    )
    usage = TokenUsage()
    if hasattr(res, "token_usage") and isinstance(res.token_usage, dict):
      usage = TokenUsage(
          in_tokens=res.token_usage.get("in_tokens", 0),
          out_tokens=res.token_usage.get("out_tokens", 0),
          total_tokens=res.token_usage.get("total_tokens", 0),
      )
    return res, usage


# Canonical PascalCase finding schema per docs/architecture/guardrails.md Section 6.
# cm CLI 0.7.0 (cl/974628022) switched `cm report --format json` to snake_case keys,
# so both casings are normalized to the canonical form for version-agnostic parsing.
_FINDING_KEY_ALIASES = {
    "finding_id": "FindingID",
    "session_id": "SessionID",
    "title": "Title",
    "file_path": "FilePath",
    "severity": "Severity",
    "confidence": "Confidence",
    "analysis": "Analysis",
    "snippet": "Snippet",
    "vuln_type": "VulnType",
    "vuln_id": "VulnID",
    "fingerprint": "Fingerprint",
    "status": "Status",
    "source_stage": "SourceStage",
    "finding_json": "FindingJSON",
    "updated_at": "UpdatedAt",
    "start_line": "StartLine",
    "end_line": "EndLine",
    "dismiss_reason": "DismissReason",
    "confidence_level": "ConfidenceLevel",
}


def parse_findings_json(json_str: str) -> List[Dict[str, Any]]:
  """Parses `cm report --format json` output normalizing keys to PascalCase."""
  data = extract_json_from_output(json_str)
  if data is None:
    logger.error("No valid JSON array or object found in report.")
    return []

  if isinstance(data, dict):
    findings = data.get("findings", data.get("items", []))
  elif isinstance(data, list):
    findings = data
  else:
    findings = []

  cleaned_findings = []
  for item in findings:
    if not isinstance(item, dict):
      continue
    cleaned = {}
    for k, v in item.items():
      if v == "":
        cleaned[k] = None
      else:
        cleaned[k] = v
      # Mirror snake_case keys onto the canonical PascalCase name. The original
      # key is retained so downstream snake_case readers keep working, and an
      # explicit PascalCase key already present in the payload always wins.
      canonical = _FINDING_KEY_ALIASES.get(k)
      if canonical and canonical not in item:
        cleaned[canonical] = cleaned[k]
    cleaned_findings.append(cleaned)

  return cleaned_findings


def extract_session_id(find_stdout: str) -> Optional[str]:
  """Extracts the CodeMender session ID from 'cm find' output."""
  # Match UUID format session ID (e.g. Session: f7f7b492-3564-4dc0-bc8f-2020554ebe24)
  match = re.search(
      r"Session:\s*([a-f0-9]{8}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{12})",
      find_stdout,
      re.IGNORECASE,
  )
  if match:
    return match.group(1)
  return None


def log_cm_version(
    cm_binary: Optional[str] = None,
    env: Optional[Dict[str, str]] = None,
    cwd: Optional[str] = None,
) -> Optional[str]:
  """Runs `cm --version` and logs the CodeMender CLI binary version.

  Args:
    cm_binary: Path or name of the cm executable.
    env: Environment variables for the subprocess.
    cwd: Working directory for running the command.

  Returns:
    The output version string if successfully retrieved, or None.
  """
  bin_path = cm_binary or shutil.which("cm") or "cm"
  try:
    res = run_command(
        [bin_path, "--version"],
        cwd=cwd,
        env=env,
        check=False,
        capture_stderr=True,
    )
    if res.returncode == 0:
      version_str = res.stdout.strip()
      if version_str:
        logger.info("CodeMender CLI version: %s", version_str)
        return version_str
      logger.warning("CodeMender CLI returned empty version output.")
    else:
      logger.warning(
          "Failed to retrieve CodeMender CLI version (exit code %d): %s",
          res.returncode,
          res.stderr.strip() if getattr(res, "stderr", None) else "",
      )
  except Exception as e:  # pylint: disable=broad-exception-caught
    logger.warning("Error checking CodeMender CLI version: %s", e)
  return None

