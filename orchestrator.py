#!/usr/bin/env python3
"""CodeMender Orchestrator Entrypoint.

Decentralized, automated orchestration script that runs within a team's secure
infrastructure (e.g., Cloud Run Job) to validate and fix security
vulnerabilities using the CodeMender CLI and open GitHub Pull Requests.
"""

import logging
import os
import sys

from codemender_agent.runners.sequential import run_sequential_pipeline

# Configure global root logger for all package submodules
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)


def main() -> None:
  """Main execution entrypoint for CodeMender Orchestrator."""
  run_mode = os.environ.get("CODEMENDER_RUN_MODE", "sequential").lower()

  if run_mode == "sequential":
    run_sequential_pipeline()
  else:
    # Default fallback to sequential pipeline
    run_sequential_pipeline()


if __name__ == "__main__":
  main()
