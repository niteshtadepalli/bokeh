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
import os
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


_CM_DEFAULT_MODEL_CACHE: Dict[str, str] = {}


def get_cm_default_model(
    cm_binary: Optional[str] = None,
    env: Optional[Dict[str, str]] = None,
    cwd: Optional[str] = None,
) -> str:
  """Detects the CodeMender CLI's built-in default model at runtime via `cm find --help`.

  Avoids hardcoding model names in the orchestrator so that whenever the CLI's
  built-in default model updates (e.g. `gemini-3.8-flash`), telemetry and HTML
  reports automatically reflect the active native default model.
  """
  candidates: List[str] = []
  if cm_binary:
    candidates.append(cm_binary)
  which_cm = shutil.which("cm")
  if which_cm and which_cm not in candidates:
    candidates.append(which_cm)
  for fallback_path in (
      "/usr/local/bin/cm",
      os.path.expanduser("~/bin/cm"),
      "./cm",
  ):
    if os.path.isfile(fallback_path) and fallback_path not in candidates:
      candidates.append(fallback_path)

  for bin_path in candidates:
    if bin_path in _CM_DEFAULT_MODEL_CACHE:
      return _CM_DEFAULT_MODEL_CACHE[bin_path]
    if not (os.path.isfile(bin_path) and os.access(bin_path, os.X_OK)):
      resolved = shutil.which(bin_path)
      if not resolved:
        continue
      bin_path = resolved
      if bin_path in _CM_DEFAULT_MODEL_CACHE:
        return _CM_DEFAULT_MODEL_CACHE[bin_path]

    try:
      res = subprocess.run(
          [bin_path, "find", "--help"],
          cwd=cwd,
          env=env,
          capture_output=True,
          text=True,
          timeout=5,
          check=False,
      )
      combined = (res.stdout or "") + "\n" + (res.stderr or "")
      match = re.search(
          r'--model\s+string[^\n]*?\(default\s+"([^"]+)"\)',
          combined,
          re.IGNORECASE,
      )
      if match:
        detected = match.group(1).strip()
        if detected:
          _CM_DEFAULT_MODEL_CACHE[bin_path] = detected
          return detected
    except Exception:  # pylint: disable=broad-exception-caught
      continue

  return "cm-default"


def ensure_cm_updated(
    cm_binary: Optional[str] = None,
    env: Optional[Dict[str, str]] = None,
    cwd: Optional[str] = None,
) -> str:
  """Executes `cm update` when CODEMENDER_AUTO_UPDATE is enabled (default: true).

  Ensures headless Cloud Run / CI containers self-update to the latest stable
  CodeMender CLI release before starting Stage 1 (`scan.py`) or `sequential.py`.
  If running inside an air-gapped VPC-SC perimeter where update servers are
  unreachable, logs a warning and continues cleanly with the existing binary.
  """
  bin_path = cm_binary or shutil.which("cm") or "cm"
  auto_update_env = (
      os.environ.get("CODEMENDER_AUTO_UPDATE", "true").strip().lower()
  )
  if auto_update_env in ("false", "0", "no", "off"):
    logger.info(
        "CODEMENDER_AUTO_UPDATE=%s; skipping cm auto-update check.",
        auto_update_env,
    )
    return bin_path

  resolved_bin = shutil.which(bin_path) if not os.path.isfile(bin_path) else bin_path
  if not resolved_bin or not os.path.isfile(resolved_bin):
    return bin_path

  try:
    logger.info("Checking for CodeMender CLI updates via '%s update'...", resolved_bin)
    res = subprocess.run(
        [resolved_bin, "update"],
        cwd=cwd,
        env=env,
        capture_output=True,
        text=True,
        timeout=45,
        check=False,
    )
    if res.returncode == 0:
      _CM_DEFAULT_MODEL_CACHE.pop(resolved_bin, None)
      _CM_DEFAULT_MODEL_CACHE.pop(bin_path, None)
      out_lines = [
          line.strip()
          for line in (res.stdout or "").splitlines()
          if line.strip() and "[INFO]" not in line
      ]
      if out_lines:
        logger.info("CodeMender CLI auto-update status: %s", out_lines[-1])
    else:
      logger.warning(
          "CodeMender CLI self-update returned exit code %d (continuing with existing binary): %s",
          res.returncode,
          (res.stderr or "").strip(),
      )
  except Exception as e:  # pylint: disable=broad-exception-caught
    logger.warning(
        "CodeMender CLI auto-update skipped due to error (continuing with existing binary): %s",
        e,
    )

  return resolved_bin


def stage_cm_binary_for_archive(
    codemender_home: str,
    cm_binary: Optional[str] = None,
) -> Optional[str]:
  """Copies the active `cm` binary into `~/.codemender/bin/cm` before archiving `workspace_base.tar.gz`.

  This propagates Stage 1's self-updated `cm` binary directly to all parallel
  Stage 2 workers and Stage 3 aggregator containers via GCS without redundant downloads.
  """
  bin_path = cm_binary or shutil.which("cm") or "cm"
  resolved = bin_path if os.path.isfile(bin_path) else shutil.which(bin_path)
  if not resolved or not os.path.isfile(resolved):
    return None

  staged_dir = os.path.join(codemender_home, "bin")
  staged_path = os.path.join(staged_dir, "cm")
  try:
    os.makedirs(staged_dir, exist_ok=True)
    if os.path.abspath(resolved) != os.path.abspath(staged_path):
      shutil.copy2(resolved, staged_path)
      os.chmod(staged_path, 0o755)
    logger.info("Staged updated cm binary into %s for worker/aggregator propagation.", staged_path)
    return staged_path
  except Exception as e:  # pylint: disable=broad-exception-caught
    logger.warning("Could not stage cm binary into %s: %s", staged_path, e)
    return None


def restore_staged_cm_binary(
    codemender_home: str,
    install_path: str = "/usr/local/bin/cm",
) -> str:
  """Installs `~/.codemender/bin/cm` extracted from `workspace_base.tar.gz` into `/usr/local/bin/cm`.

  Guarantees Stage 2 workers and Stage 3 aggregator run the exact same CLI
  version as Stage 1. Falls back to `~/.codemender/bin/cm` on PATH if
  `/usr/local/bin/cm` is not writable.
  """
  staged_path = os.path.join(codemender_home, "bin", "cm")
  if not os.path.isfile(staged_path):
    return shutil.which("cm") or "cm"

  try:
    os.chmod(staged_path, 0o755)
  except Exception:  # pylint: disable=broad-exception-caught
    pass

  _CM_DEFAULT_MODEL_CACHE.clear()
  try:
    install_dir = os.path.dirname(install_path)
    if os.path.isdir(install_dir) and os.access(install_dir, os.W_OK):
      tmp_dest = f"{install_path}.tmp.{os.getpid()}"
      shutil.copy2(staged_path, tmp_dest)
      os.chmod(tmp_dest, 0o755)
      os.replace(tmp_dest, install_path)
      logger.info("Restored staged cm binary from workspace archive to %s", install_path)
      return install_path
  except Exception as e:  # pylint: disable=broad-exception-caught
    logger.debug("Could not overwrite %s (%s); using staged binary at %s", install_path, e, staged_path)

  staged_dir = os.path.dirname(staged_path)
  current_path = os.environ.get("PATH", "")
  if staged_dir not in current_path.split(os.pathsep):
    os.environ["PATH"] = f"{staged_dir}{os.pathsep}{current_path}"
  return staged_path


