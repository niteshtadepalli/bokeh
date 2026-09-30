"""Temporary security probe module to verify Test 6B (remediated CRITICAL vulnerability in Non-Blocking Mode) on PR #10."""

import ipaddress
import subprocess


def execute_custom_diagnostic(user_host: str) -> int:
    """Remediated helper with strict IP validation and '--' option terminator."""
    validated_ip = str(ipaddress.ip_address(user_host))
    result = subprocess.run(["/bin/ping", "-c", "1", "--", validated_ip], capture_output=True, check=False)
    return result.returncode


