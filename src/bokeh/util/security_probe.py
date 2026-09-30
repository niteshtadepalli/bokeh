"""Temporary security probe module to verify Test 2B (confirmed CRITICAL vulnerability in Non-Blocking Mode) on PR #10."""

import os


def execute_custom_diagnostic(user_host: str) -> int:
    """Deliberately vulnerable helper for testing live cm find, cm verify, and cm fix in Non-Blocking Mode."""
    # Vulnerability 1: OS Command Injection (CRITICAL / HIGH)
    return os.system("ping -c 1 " + user_host)

