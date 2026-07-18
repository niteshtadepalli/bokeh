"""Unit tests for codemender_agent.codemender.cli module."""

import unittest

from codemender_agent.codemender.cli import extract_session_id, parse_findings_json


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


if __name__ == "__main__":
  unittest.main()
