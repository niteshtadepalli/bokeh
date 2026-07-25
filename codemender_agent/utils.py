"""System and Subprocess utilities for CodeMender Agent."""

import fcntl
from functools import wraps
import logging
import os
import subprocess
import sys
import time
from typing import Dict, List, Optional

import requests

logger = logging.getLogger("codemender-orchestrator")


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


def run_command(
    cmd: List[str],
    cwd: Optional[str] = None,
    env: Optional[Dict[str, str]] = None,
    check: bool = True,
    capture_stderr: bool = True,
) -> subprocess.CompletedProcess:
  """Executes a subprocess command, streaming stdout/stderr in real-time."""
  # Scrub Authorization headers or token values from logs
  log_cmd_parts = []
  for arg in cmd:
    if "http.extraheader=AUTHORIZATION:" in arg:
      log_cmd_parts.append(
          "git -c http.extraheader=AUTHORIZATION: Basic [REDACTED]"
      )
    else:
      log_cmd_parts.append(arg)

  cmd_str_short = " ".join(log_cmd_parts)
  if len(cmd_str_short) > 80:
    cmd_str_short = cmd_str_short[:77] + "..."

  logger.info("Executing command: %s", " ".join(log_cmd_parts))

  # Start the process with stderr redirected to stdout to stream both
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

  # Assert process.stdout is not None for type-checking safety
  assert process.stdout is not None

  # Make stdout non-blocking to prevent hangs on leaked background process pipes
  fd = process.stdout.fileno()
  fl = fcntl.fcntl(fd, fcntl.F_GETFL)
  fcntl.fcntl(fd, fcntl.F_SETFL, fl | os.O_NONBLOCK)

  # Write start delimiter
  sys.stdout.write(f"\n>>> [SUBPROCESS START] {cmd_str_short} >>>\n")
  sys.stdout.flush()

  stdout_lines = []
  
  # Stream output line-by-line in a non-blocking poll loop
  while True:
    try:
      line = process.stdout.readline()
      if line:
        sys.stdout.write(line)
        sys.stdout.flush()
        stdout_lines.append(line)
        continue
    except (IOError, ValueError):
      # No data currently available
      pass

    # Check if the main process has terminated
    return_code = process.poll()
    if return_code is not None:
      # Read any last bytes remaining in the buffer before breaking
      while True:
        try:
          line = process.stdout.readline()
          if not line:
            break
          sys.stdout.write(line)
          sys.stdout.flush()
          stdout_lines.append(line)
        except (IOError, ValueError):
          break
      break

    time.sleep(0.1)

  process.stdout.close()
  full_stdout = "".join(stdout_lines)

  # Write end delimiter
  sys.stdout.write(
      f"<<< [SUBPROCESS END] {cmd_str_short} (EXIT: {return_code}) <<<\n\n"
  )
  sys.stdout.flush()

  if check and return_code != 0:
    logger.error("Command failed with code %d", return_code)
    raise subprocess.CalledProcessError(return_code, cmd, full_stdout, "")

  return subprocess.CompletedProcess(cmd, return_code, full_stdout, "")


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
