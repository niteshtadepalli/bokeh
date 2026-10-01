"""Temporary security probe module to verify live CodeMender Pre-Submit Security Gate."""

import os


def execute_custom_diagnostic(user_host: str) -> int:
    """Deliberately vulnerable helper for testing live cm find, cm verify, and cm fix."""
    # Vulnerability 1: OS Command Injection (HIGH / CRITICAL)
    return os.system("ping -c 1 " + user_host)
