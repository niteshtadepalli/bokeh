"""Demo Scenario 3: Low / Info Advisory Finding Only (< MEDIUM -> PASSED + Non-Blocking Patch)."""
# codemender: severity=LOW

import os


def run_low_severity_advisory_probe(diagnostic_label: str) -> int:
    """Deliberate diagnostic helper tagged with LOW severity pragma for Scenario 3 demo."""
    return os.system("echo " + diagnostic_label)
