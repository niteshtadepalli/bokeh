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

"""System and Subprocess utilities for CodeMender Agent."""

from functools import wraps
import logging
import os
import re
import subprocess
import sys
import time
from typing import Dict, List, Optional

import requests

logger = logging.getLogger("codemender-orchestrator")


def parse_token_metric(token_str: str) -> int:
  """Converts human-readable token metric strings with SI suffixes into integers.

  Supports k/K (thousands), m/M (millions), g/G (billions).
  e.g. "41k" -> 41000, "41.5k" -> 41500, "1.2M" -> 1200000, "1.5G" -> 1500000000, "561" -> 561.
  """
  token_str = token_str.strip()
  if not token_str:
    raise ValueError("Empty token metric string.")

  unit_multipliers = {
      "k": 1000,
      "m": 1000000,
      "g": 1000000000,
  }

  last_char = token_str[-1].lower()
  if last_char in unit_multipliers:
    val = float(token_str[:-1])
    return int(val * unit_multipliers[last_char])

  return int(float(token_str))


def resolve_command_model(command_name: str) -> Optional[str]:
  """Implements model precedence hierarchy: CODEMENDER_<COMMAND>_MODEL > CODEMENDER_MODEL > None."""
  cmd_override = os.environ.get(f"CODEMENDER_{command_name.upper()}_MODEL")
  if cmd_override:
    return cmd_override
  return os.environ.get("CODEMENDER_MODEL")


def accumulate_model_token_usage(
    usage_dict: dict[str, dict[str, int]],
    model_name: str,
    token_metrics: Optional[dict[str, int]],
) -> None:
  """Accumulates in/out/total tokens into usage_dict keyed by model_name."""
  if not token_metrics or not isinstance(token_metrics, dict):
    return
  if model_name not in usage_dict:
    usage_dict[model_name] = {"in_tokens": 0, "out_tokens": 0, "total_tokens": 0}
  usage_dict[model_name]["in_tokens"] += token_metrics.get("in_tokens", 0)
  usage_dict[model_name]["out_tokens"] += token_metrics.get("out_tokens", 0)
  usage_dict[model_name]["total_tokens"] += token_metrics.get("total_tokens", 0)


def build_cm_command(
    cm_binary: str,
    action: str,
    target_or_id: Optional[str] = None,
    cli_version: Optional[str] = None,
    extra_flags: Optional[List[str]] = None,
) -> List[str]:
  """Centralized command builder for CodeMender CLI invocations."""
  if cli_version is None:
    cli_version = os.environ.get("CODEMENDER_CLI_VERSION", "preview").lower()
  else:
    cli_version = cli_version.lower()

  if action in ["find", "verify", "fix"] and not target_or_id:
    raise ValueError(f"Action '{action}' requires a valid target or finding ID.")

  if cli_version == "preview":
    model = resolve_command_model(action)
    model_flags = ["--model", model] if model else []

    if action == "find":
      cmd = [cm_binary, "find", "-y"] + model_flags + [target_or_id]
    elif action == "verify":
      skip_flag = (
          ["--skip-exploit-verification"]
          if os.environ.get("CODEMENDER_SKIP_EXPLOIT_VERIFICATION", "").lower() == "true"
          else []
      )
      cmd = (
          [cm_binary, "verify", "-y", "--bypass-warning"]
          + model_flags
          + skip_flag
          + [target_or_id]
      )
    elif action == "fix":
      cmd = (
          [cm_binary, "fix", "-y", "--bypass-warning"]
          + model_flags
          + [target_or_id]
      )
    elif action == "init":
      cmd = [cm_binary, "init"]
      if extra_flags:
        cmd.extend(extra_flags)
    else:
      cmd = [cm_binary, action]
      if extra_flags:
        cmd.extend(extra_flags)
  else:  # legacy
    if action == "find":
      cmd = [cm_binary, "find", target_or_id]
    elif action == "verify":
      cmd = [cm_binary, "find", "verify", target_or_id, "--yes"]
    elif action == "fix":
      cmd = [cm_binary, "fix", target_or_id, "--yes"]
    elif action == "init":
      cmd = [cm_binary, "init"]
      if extra_flags:
        cmd.extend(extra_flags)
    else:
      cmd = [cm_binary, action]
      if extra_flags:
        cmd.extend(extra_flags)

  return [arg for arg in cmd if arg is not None]


def retry_on_exception(max_tries=3, initial_delay=1, backoff_factor=2):
  """Decorator to retry transient network/command errors with exponential backoff."""

  def decorator(func):
    @wraps(func)
    def wrapper(*args, **kwargs):
      delay = initial_delay
      for attempt in range(1, max_tries + 1):
        try:
          return func(*args, **kwargs)
        except (requests.RequestException, subprocess.CalledProcessError) as e:
          if attempt == max_tries:
            raise
          logger.warning(
              "Attempt %d failed for %s: %s. Retrying in %d seconds...",
              attempt,
              getattr(func, "__name__", str(func)),
              e,
              delay,
          )
          time.sleep(delay)
          delay *= backoff_factor
      return None

    return wrapper

  return decorator


SECRET_PATTERNS = [
    re.compile(r"http\.extraheader=AUTHORIZATION:.*", re.IGNORECASE),
    re.compile(r"(ghp_|ghs_|github_pat_|bearer\s+)[a-zA-Z0-9_\-\.]+", re.IGNORECASE),
]


def redact_sensitive_arg(arg: str) -> str:
  """Redacts secret credentials from command argument strings for log safety."""
  for pattern in SECRET_PATTERNS:
    if pattern.search(arg):
      return pattern.sub("[REDACTED_SECRET]", arg)
  return arg


def run_command(
    cmd: List[str],
    cwd: Optional[str] = None,
    env: Optional[Dict[str, str]] = None,
    check: bool = True,
    capture_stderr: bool = True,
) -> subprocess.CompletedProcess:
  """Executes a subprocess command, streaming stdout/stderr in real-time."""
  log_cmd_parts = [redact_sensitive_arg(arg) for arg in cmd]
  cmd_str_short = " ".join(log_cmd_parts)
  if len(cmd_str_short) > 80:
    cmd_str_short = cmd_str_short[:77] + "..."

  logger.info("Executing command: %s", " ".join(log_cmd_parts))

  # Start process with line-buffered stdout streaming
  process = subprocess.Popen(
      cmd,
      cwd=cwd,
      env=env,
      stdin=subprocess.DEVNULL,
      stdout=subprocess.PIPE,
      stderr=subprocess.STDOUT if capture_stderr else sys.stderr,
      text=True,
      bufsize=1,  # Line-buffered
  )

  assert process.stdout is not None

  # Write start delimiter
  sys.stdout.write(f"\n>>> [SUBPROCESS START] {cmd_str_short} >>>\n")
  sys.stdout.flush()

  stdout_lines = []

  # Stream output line-by-line cleanly without busy-wait sleep loops
  for line in process.stdout:
    sys.stdout.write(line)
    sys.stdout.flush()
    stdout_lines.append(line)

  process.stdout.close()
  return_code = process.wait()
  full_stdout = "".join(stdout_lines)

  # Write end delimiter
  sys.stdout.write(
      f"<<< [SUBPROCESS END] {cmd_str_short} (EXIT: {return_code}) <<<\n\n"
  )
  sys.stdout.flush()

  if check and return_code != 0:
    logger.error("Command failed with code %d", return_code)
    raise subprocess.CalledProcessError(return_code, cmd, full_stdout, "")

  cli_version = os.environ.get("CODEMENDER_CLI_VERSION", "preview").lower()
  token_usage = None
  if cli_version == "preview":
    matches = re.findall(
        r"Tokens:\s*([0-9.kMgG]+)\s*in\s*/\s*([0-9.kMgG]+)\s*out\s*/\s*([0-9.kMgG]+)\s*total",
        full_stdout,
    )
    if matches:
      in_tokens = 0
      out_tokens = 0
      total_tokens = 0
      for m in matches:
        try:
          in_tokens += parse_token_metric(m[0])
          out_tokens += parse_token_metric(m[1])
          total_tokens += parse_token_metric(m[2])
        except ValueError:
          pass
      token_usage = {
          "in_tokens": in_tokens,
          "out_tokens": out_tokens,
          "total_tokens": total_tokens,
      }
    else:
      token_usage = {"in_tokens": 0, "out_tokens": 0, "total_tokens": 0}

  res = subprocess.CompletedProcess(cmd, return_code, full_stdout, "")
  res.token_usage = token_usage
  return res


def free_port(port: int):
  """Attempts to kill any process listening on the specified port."""
  try:
    subprocess.run(
        ["fuser", "-k", f"{port}/tcp"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        check=False,
    )
  except FileNotFoundError:
    logger.warning("fuser command not found. Skipping port %d cleanup.", port)

