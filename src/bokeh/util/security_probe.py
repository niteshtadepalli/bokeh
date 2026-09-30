"""Temporary security probe module to verify Scenario 3B (LOW advisory finding in Non-Blocking Mode) on PR #10."""
# codemender: severity=LOW

import os


def run_low_severity_advisory_probe(diagnostic_label: str) -> int:
    """Deliberate diagnostic helper tagged with LOW severity pragma for Scenario 3B testing."""
    return os.system("echo " + diagnostic_label)



