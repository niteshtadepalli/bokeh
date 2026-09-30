"""Temporary security probe module to verify Scenario 3 (LOW advisory finding) on PR #10."""
# codemender: severity=LOW

import os


def run_low_severity_advisory_probe(diagnostic_label: str) -> int:
    """Deliberate diagnostic helper tagged with LOW severity pragma for Scenario 3 testing."""
    return os.system("echo " + diagnostic_label)
