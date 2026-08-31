# Copyright 2026 Google LLC
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""Unit tests for codemender_agent.codemender.cli module."""

import unittest
from unittest.mock import MagicMock, patch

from codemender_agent.codemender.cli import (
    extract_session_id,
    log_cm_version,
    parse_findings_json,
)


class TestCodeMenderCli(unittest.TestCase):

  def test_extract_session_id(self):
    """Verify regex extraction of Session UUID from cm find stdout."""
    find_output = """
        🚀 Starting FIND session (mode: SCAN)...
        Server: codemender_prod
        Session: 63198618-4ce9-41fa-b973-436e1451a3d0
        Operation: sessions/63198618-4ce9-41fa-b973-436e1451a3d0/operations/aeabc910
    """
    session_id = extract_session_id(find_output)
    self.assertEqual(session_id, "63198618-4ce9-41fa-b973-436e1451a3d0")

  def test_parse_findings_json_valid(self):
    """Verify parsing valid JSON findings output."""
    raw_json = """[
      {
        "FindingID": "f123",
        "VulnType": "SQL Injection",
        "FilePath": "app.py",
        "Status": "NEW"
      }
    ]"""
    findings = parse_findings_json(raw_json)
    self.assertEqual(len(findings), 1)
    self.assertEqual(findings[0]["FindingID"], "f123")
    self.assertEqual(findings[0]["VulnType"], "SQL Injection")

  @patch("codemender_agent.codemender.cli.run_command")
  def test_log_cm_version_success(self, mock_run_cmd):
    """Verify log_cm_version logs and returns version string on success."""
    mock_run_cmd.return_value = MagicMock(
        returncode=0,
        stdout="cm version v0.1.0-20260515-vMvg-916238397\n",
        stderr="",
    )
    version = log_cm_version("cm")
    self.assertEqual(version, "cm version v0.1.0-20260515-vMvg-916238397")
    mock_run_cmd.assert_called_once_with(
        ["cm", "--version"],
        cwd=None,
        env=None,
        check=False,
        capture_stderr=True,
    )

  @patch("codemender_agent.codemender.cli.run_command")
  def test_log_cm_version_failure(self, mock_run_cmd):
    """Verify log_cm_version handles command error gracefully."""
    mock_run_cmd.return_value = MagicMock(
        returncode=1,
        stdout="",
        stderr="command not found",
    )
    version = log_cm_version("cm")
    self.assertIsNone(version)

  @patch("codemender_agent.codemender.cli.run_command")
  def test_log_cm_version_exception(self, mock_run_cmd):
    """Verify log_cm_version handles execution exceptions gracefully."""
    mock_run_cmd.side_effect = RuntimeError("Execution failed")
    version = log_cm_version("cm")
    self.assertIsNone(version)


if __name__ == "__main__":
  unittest.main()

