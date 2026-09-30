"""Temporary security probe module to verify Test 2B (confirmed CRITICAL vulnerability in Non-Blocking Mode) on PR #10."""

import subprocess


def execute_custom_diagnostic(user_host: str) -> int:
    """Deliberately vulnerable helper for testing live cm find, cm verify, and cm fix in Non-Blocking Mode."""
    result = subprocess.run(["ping", "-c", "1", user_host], capture_output=True)
    return result.returncode

