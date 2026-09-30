"""Temporary security probe module to verify Test 5A (Stage 1 cancellation -> fail-closed gate) on PR #10."""

import os


def run_cancel_probe(user_host: str) -> int:
    """Deliberate command sink for Test 5A fail-closed gate verification."""
    return os.system("ping -c 1 " + user_host)
