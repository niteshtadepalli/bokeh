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

"""Unit tests for Phase 2: Stage 1 Diff-Scoped Scan & Immediate Security Gate."""

import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import MagicMock, patch

from codemender_agent.runners.gate import (
    resolve_pr_diff_targets,
    run_security_gate_pipeline,
)
from codemender_agent.runners.scan import (
    build_pr_diff_context_prompt,
    classify_and_report_stage1_findings,
    execute_stage1_presubmit_scan,
)


class TestScanAndImmediateGate(unittest.TestCase):
  """Tests for preflight diff target resolution, Stage 1 classification, and immediate gate."""

  def setUp(self) -> None:
    self.temp_dir = tempfile.TemporaryDirectory()
    self.workspace = Path(self.temp_dir.name)

  def tearDown(self) -> None:
    self.temp_dir.cleanup()

  @patch("codemender_agent.runners.gate.subprocess.run")
  def test_01_non_code_pr_short_circuits_skip_scan(self, mock_run: MagicMock) -> None:
    (self.workspace / "README.md").write_text("# Docs", encoding="utf-8")
    mock_run.return_value = subprocess.CompletedProcess(
        args=[],
        returncode=0,
        stdout="README.md\n.github/workflows/ci.yml\nsrc/deleted_file.py\n",
        stderr="",
    )
    out_file = self.workspace / "github_output.txt"
    skip_scan, resolved = resolve_pr_diff_targets(
        workspace_dir=str(self.workspace),
        is_pr=True,
        diff_scoped=True,
        base_ref="main",
        default_scan_target="src",
        github_output=str(out_file),
    )
    self.assertTrue(skip_scan)
    self.assertEqual(resolved, "")
    self.assertIn("skip_scan=true", out_file.read_text(encoding="utf-8"))

  @patch("codemender_agent.runners.gate.subprocess.run")
  def test_02_diff_target_resolution_small_vs_large_pr_directory_grouping(
      self, mock_run: MagicMock
  ) -> None:
    files = []
    for i in range(20):
      rel = f"src/pkg_{i % 3}/mod_{i}.py"
      full = self.workspace / rel
      full.parent.mkdir(parents=True, exist_ok=True)
      full.write_text("x = 1\n", encoding="utf-8")
      files.append(rel)

    mock_run.return_value = subprocess.CompletedProcess(
        args=[], returncode=0, stdout="\n".join(files) + "\n", stderr=""
    )
    skip_scan, resolved = resolve_pr_diff_targets(
        workspace_dir=str(self.workspace),
        is_pr=True,
        diff_scoped=True,
        base_ref="main",
    )
    self.assertFalse(skip_scan)
    self.assertEqual(resolved, "src/pkg_0,src/pkg_1,src/pkg_2")

  @patch("subprocess.run")
  def test_03_build_pr_diff_context_prompt_scopes_to_source_code(
      self, mock_run: MagicMock
  ) -> None:
    recorded_cmds = []

    def side_effect(cmd, **_kwargs):
      recorded_cmds.append(cmd)
      if "--name-only" in cmd:
        return subprocess.CompletedProcess(
            cmd, 0, stdout="README.md\n.github/workflows/a.yml\nsrc/app.py\n", stderr=""
        )
      return subprocess.CompletedProcess(cmd, 0, stdout="+eval(x)\n", stderr="")

    mock_run.side_effect = side_effect
    mod_files, prompt = build_pr_diff_context_prompt(str(self.workspace), "main")
    self.assertEqual(mod_files, {"src/app.py"})
    self.assertIn("src/app.py", prompt)
    self.assertNotIn("README.md", prompt)
    self.assertEqual(recorded_cmds[-1][-2:], ["--", "src/app.py"])

  @patch("codemender_agent.vcs.github.post_or_update_sticky_comment")
  def test_04_stage1_classify_pragma_annotations_and_immediate_sticky(
      self, mock_sticky: MagicMock
  ) -> None:
    (self.workspace / "src").mkdir(parents=True, exist_ok=True)
    (self.workspace / "src" / "pragma.py").write_text(
        "# codemender: severity=LOW\nimport hashlib\n", encoding="utf-8"
    )
    (self.workspace / "src" / "vuln.py").write_text("eval(x)\n", encoding="utf-8")

    raw_findings = [
        {
            "FindingID": "11111111-aaaa",
            "FilePath": "src/vuln.py",
            "StartLine": 1,
            "Severity": "HIGH",
            "Title": "Code Injection",
            "Description": "Unsafe eval",
        },
        {
            "FindingID": "22222222-bbbb",
            "FilePath": "src/pragma.py",
            "StartLine": 2,
            "Severity": "HIGH",
            "Title": "Downgraded Finding",
            "Description": "Downgraded via pragma",
        },
        {
            "FindingID": "33333333-cccc",
            "FilePath": "src/untouched.py",
            "StartLine": 5,
            "Severity": "CRITICAL",
            "Title": "Pre-existing Issue",
            "Description": "Should be filtered out",
        },
    ]
    base_dir = self.workspace / "base"
    active, blocking_cnt, advisory_cnt = classify_and_report_stage1_findings(
        findings=raw_findings,
        workspace_dir=str(self.workspace),
        modified_files={"src/vuln.py", "src/pragma.py"},
        min_sev="MEDIUM",
        base_dir=str(base_dir),
        token="ghs_token",
        owner="org",
        repo="repo",
        pr_number=2,
        target_sha="abcdef123456",
        run_url="https://github.com/org/repo/actions/runs/1",
    )
    self.assertEqual(len(active), 2)
    self.assertEqual(blocking_cnt, 1)
    self.assertEqual(advisory_cnt, 1)
    saved = json.loads((base_dir / "active_findings.json").read_text(encoding="utf-8"))
    self.assertEqual(len(saved), 2)
    self.assertTrue(mock_sticky.called)
    sticky_body = mock_sticky.call_args.kwargs["body"]
    self.assertIn("<!-- cm-row:11111111 -->", sticky_body)
    self.assertIn("<!-- cm-row:22222222 -->", sticky_body)

  @patch("codemender_agent.runners.gate.resolve_sticky_comment_if_present")
  @patch("codemender_agent.runners.gate.post_commit_status")
  def test_05_security_gate_blocks_on_high_and_passes_on_advisory_with_sticky_resolution(
      self, mock_status: MagicMock, mock_resolve: MagicMock
  ) -> None:
    env_block = {
        "SCAN_RESULT": "success",
        "FINDINGS_COUNT": "1",
        "BLOCKING_COUNT": "1",
        "ADVISORY_COUNT": "0",
        "MIN_SEVERITY": "MEDIUM",
        "FAIL_ON_FINDINGS": "true",
        "PR_NUMBER": "2",
        "REPO_FULL_NAME": "org/repo",
        "TARGET_SHA": "abcdef123456",
        "GITHUB_TOKEN": "ghs_token",
    }
    with patch.dict(os.environ, env_block, clear=False):
      with self.assertRaises(SystemExit) as ctx:
        run_security_gate_pipeline()
      self.assertEqual(ctx.exception.code, 1)
      self.assertEqual(mock_status.call_args.kwargs["state"], "failure")

    env_clean = {
        **env_block,
        "FINDINGS_COUNT": "0",
        "BLOCKING_COUNT": "0",
    }
    with patch.dict(os.environ, env_clean, clear=False):
      rc = run_security_gate_pipeline()
      self.assertEqual(rc, 0)
      self.assertEqual(mock_status.call_args.kwargs["state"], "success")
      self.assertTrue(mock_resolve.called)

    # Configurable MIN_BLOCKING_SEVERITY=HIGH: 1 MEDIUM finding is non-blocking (< HIGH advisory)
    env_high_threshold = {
        "SCAN_RESULT": "success",
        "FINDINGS_COUNT": "1",
        "BLOCKING_COUNT": "0",
        "ADVISORY_COUNT": "1",
        "MIN_BLOCKING_SEVERITY": "HIGH",
        "FAIL_ON_FINDINGS": "true",
        "PR_NUMBER": "2",
        "REPO_FULL_NAME": "org/repo",
        "TARGET_SHA": "abcdef123456",
        "GITHUB_TOKEN": "ghs_token",
    }
    with patch.dict(os.environ, env_high_threshold, clear=False):
      rc_high = run_security_gate_pipeline()
      self.assertEqual(rc_high, 0)
      self.assertEqual(mock_status.call_args.kwargs["state"], "success")
      self.assertIn(
          "PASSED: 0 vulnerabilities >= HIGH in PR diff (1 < HIGH advisory finding(s)).",
          mock_status.call_args.kwargs["description"],
      )

  @patch("codemender_agent.runners.gate.post_commit_status")
  def test_06_security_gate_fails_closed_on_scan_crash_cancel_timeout(
      self, mock_status: MagicMock
  ) -> None:
    for bad_state in ("failure", "cancelled", "timed_out"):
      with patch.dict(
          os.environ,
          {
              "SCAN_RESULT": bad_state,
              "FINDINGS_COUNT": "0",
              "BLOCKING_COUNT": "0",
              "REPO_FULL_NAME": "org/repo",
              "TARGET_SHA": "abcdef123456",
              "GITHUB_TOKEN": "ghs_token",
          },
          clear=False,
      ):
        with self.assertRaises(SystemExit) as ctx:
          run_security_gate_pipeline()
        self.assertEqual(ctx.exception.code, 1)
        self.assertEqual(mock_status.call_args.kwargs["state"], "error")

  @patch("codemender_agent.vcs.github.post_or_update_sticky_comment")
  @patch("subprocess.run")
  def test_07_execute_stage1_presubmit_scan_sbox_retry_and_transit_archive(
      self, mock_run: MagicMock, mock_sticky: MagicMock
  ) -> None:
    (self.workspace / "src").mkdir(parents=True, exist_ok=True)
    (self.workspace / "src" / "app.py").write_text("eval(x)\n", encoding="utf-8")
    (self.workspace / ".cm_project").write_text("project: test\n", encoding="utf-8")
    out_file = self.workspace / "github_output.txt"
    cmds = []

    def side_effect(cmd, **_kwargs):
      cmds.append(cmd)
      if "--name-only" in cmd:
        return subprocess.CompletedProcess(cmd, 0, stdout="src/app.py\n", stderr="")
      if "diff" in cmd and "-U3" in cmd:
        return subprocess.CompletedProcess(cmd, 0, stdout="+eval(x)\n", stderr="")
      if cmd[:2] == ["cm", "find"] and "--unrestricted" not in cmd:
        return subprocess.CompletedProcess(
            cmd, 1, stdout="", stderr="sbox sandbox mount failed"
        )
      if cmd[:2] == ["cm", "find"] and "--unrestricted" in cmd:
        return subprocess.CompletedProcess(cmd, 0, stdout="ok", stderr="")
      if cmd[:4] == ["cm", "report", "--format", "json"]:
        return subprocess.CompletedProcess(
            cmd,
            0,
            stdout=json.dumps([
                {
                    "finding_id": "aaaa1111-2222",
                    "file_path": "src/app.py",
                    "line_number": 1,
                    "severity": "HIGH",
                    "title": "Code Injection",
                    "description": "Unsafe eval",
                }
            ]),
            stderr="",
        )
      if cmd[:4] == ["cm", "report", "--format", "sarif"]:
        return subprocess.CompletedProcess(
            cmd, 0, stdout='{"version":"2.1.0","runs":[]}', stderr=""
        )
      return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

    mock_run.side_effect = side_effect
    with patch.dict(
        os.environ,
        {
            "WORKSPACE_DIR": str(self.workspace),
            "SCAN_TARGET": "src/app.py",
            "IS_PR": "true",
            "DIFF_SCOPED": "true",
            "BASE_REF": "main",
            "MIN_BLOCKING_SEVERITY": "MEDIUM",
            "MAX_TASKS": "4",
            "TARGET_SHA": "deadbeef1234",
            "SCAN_ID": "scan_1",
            "GH_TOKEN": "ghs_token",
            "REPO_FULL": "org/repo",
            "PR_NUMBER": "2",
            "GITHUB_OUTPUT": str(out_file),
        },
        clear=False,
    ):
      active = execute_stage1_presubmit_scan()

    self.assertEqual(len(active), 1)
    self.assertTrue(any("--unrestricted" in c for c in cmds))
    base_dir = self.workspace / ".codemender_transit" / "base"
    self.assertTrue((base_dir / "partition_0.json").exists())
    self.assertTrue((base_dir / "codemender_home.tar.gz").exists())
    self.assertTrue((base_dir / ".cm_project").exists())
    out_text = out_file.read_text(encoding="utf-8")
    self.assertIn("blocking_count=1", out_text)
    self.assertTrue(mock_sticky.called)

  @patch("subprocess.run")
  def test_execute_stage1_presubmit_scan_skip_target_short_circuits_before_cm_init(
      self, mock_run: MagicMock
  ) -> None:
    out_file = self.workspace / "github_output_skip.txt"
    with patch.dict(
        os.environ,
        {
            "WORKSPACE_DIR": str(self.workspace),
            "SCAN_TARGET": "__SKIP_NO_SOURCE_CHANGES__",
            "TARGET_SHA": "cafebabe9999",
            "SCAN_ID": "scan_skip_1",
            "GITHUB_OUTPUT": str(out_file),
        },
        clear=False,
    ):
      active = execute_stage1_presubmit_scan()

    self.assertEqual(active, [])
    # Must never invoke `cm init` or `cm find` when skip_scan is true
    mock_run.assert_not_called()
    out_text = out_file.read_text(encoding="utf-8")
    self.assertIn("findings_count=0", out_text)
    self.assertIn("blocking_count=0", out_text)
    self.assertTrue((self.workspace / "report.sarif").exists())

  def test_classify_and_report_stage1_findings_extracts_snake_case_start_and_end_lines(
      self,
  ) -> None:
    (self.workspace / "src" / "app.py").parent.mkdir(parents=True, exist_ok=True)
    (self.workspace / "src" / "app.py").write_text("import os\nos.system(x)\n", encoding="utf-8")
    raw_findings = [
        {
            "finding_id": "snake-12345678",
            "file_path": "src/app.py",
            "start_line": 42,
            "end_line": 45,
            "severity": "HIGH",
            "title": "OS Command Injection",
            "description": "Snake case line numbers from live cm report --format json",
        }
    ]
    active, blocking, advisory = classify_and_report_stage1_findings(
        findings=raw_findings,
        workspace_dir=str(self.workspace),
        modified_files={"src/app.py"},
        min_sev="MEDIUM",
    )
    self.assertEqual(len(active), 1)
    self.assertEqual(blocking, 1)
    self.assertEqual(advisory, 0)
    self.assertEqual(active[0]["line_number"], 42)
    self.assertEqual(active[0]["StartLine"], 42)
    self.assertEqual(active[0]["EndLine"], 45)

  @patch("codemender_agent.runners.gate.subprocess.run")
  def test_08_resolve_pr_diff_targets_fetches_base_ref_on_shallow_clone(
      self, mock_run: MagicMock
  ) -> None:
    (self.workspace / "src").mkdir(parents=True, exist_ok=True)
    (self.workspace / "src" / "app.py").write_text("x = 1\n", encoding="utf-8")
    calls = []

    def side_effect(cmd, **_kwargs):
      calls.append(cmd)
      if len(calls) == 1:
        # Initial git diff fails because shallow clone lacks origin/branch-4.0
        return subprocess.CompletedProcess(
            cmd, 128, stdout="", stderr="fatal: ambiguous argument 'origin/branch-4.0...HEAD'"
        )
      if "fetch" in cmd:
        return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")
      return subprocess.CompletedProcess(cmd, 0, stdout="src/app.py\n", stderr="")

    mock_run.side_effect = side_effect
    skip_scan, resolved = resolve_pr_diff_targets(
        workspace_dir=str(self.workspace),
        is_pr=True,
        diff_scoped=True,
        base_ref="branch-4.0",
    )
    self.assertFalse(skip_scan)
    self.assertEqual(resolved, "src/app.py")
    self.assertEqual(len(calls), 3)
    self.assertIn("+refs/heads/branch-4.0:refs/remotes/origin/branch-4.0", calls[1])

  @patch("subprocess.run")
  def test_09_line_scoped_severity_pragma_and_find_model_flag(
      self, mock_run: MagicMock
  ) -> None:
    (self.workspace / "src").mkdir(parents=True, exist_ok=True)
    # Line 2 has a CRITICAL command injection; line 25 has a function-scoped # codemender: severity=LOW pragma
    src_lines = [
        "import os, hashlib",
        "def run_crit(x): os.system(x)",
        *[f"# filler line {i}" for i in range(3, 24)],
        "def weak_hash(p):",
        "    # codemender: severity=LOW",
        "    return hashlib.md5(p.encode()).hexdigest()",
    ]
    (self.workspace / "src" / "mixed_pragma.py").write_text(
        "\n".join(src_lines) + "\n", encoding="utf-8"
    )
    (self.workspace / ".cm_project").write_text("project: test\n", encoding="utf-8")
    recorded_find_cmd = []

    def side_effect(cmd, **_kwargs):
      if cmd == ["cm", "find", "--help"]:
        return subprocess.CompletedProcess(cmd, 0, stdout="Usage: cm find <path>\n", stderr="")
      if cmd[:2] == ["cm", "find"]:
        recorded_find_cmd.append(cmd)
        return subprocess.CompletedProcess(cmd, 0, stdout="ok", stderr="")
      if "--name-only" in cmd:
        return subprocess.CompletedProcess(cmd, 0, stdout="src/mixed_pragma.py\n", stderr="")
      if "diff" in cmd and "-U3" in cmd:
        return subprocess.CompletedProcess(cmd, 0, stdout="+os.system(x)\n", stderr="")
      if cmd[:4] == ["cm", "report", "--format", "json"]:
        return subprocess.CompletedProcess(
            cmd,
            0,
            stdout=json.dumps([
                {
                    "finding_id": "crit-0001",
                    "file_path": "src/mixed_pragma.py",
                    "start_line": 2,
                    "severity": "CRITICAL",
                    "title": "Command Injection",
                },
                {
                    "finding_id": "low-0002",
                    "file_path": "src/mixed_pragma.py",
                    "start_line": 26,
                    "severity": "HIGH",
                    "title": "Weak MD5",
                },
            ]),
            stderr="",
        )
      return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

    mock_run.side_effect = side_effect
    with patch.dict(
        os.environ,
        {
            "WORKSPACE_DIR": str(self.workspace),
            "SCAN_TARGET": "src/mixed_pragma.py",
            "IS_PR": "true",
            "DIFF_SCOPED": "true",
            "BASE_REF": "main",
            "FIND_MODEL": "gemini-2.5-pro",
        },
        clear=False,
    ):
      active = execute_stage1_presubmit_scan()

    self.assertEqual(len(active), 2)
    # Line 2 finding remains CRITICAL; Line 26 finding is downgraded to LOW
    by_id = {f["finding_id"]: f["severity"] for f in active}
    self.assertEqual(by_id["crit-0001"], "CRITICAL")
    self.assertEqual(by_id["low-0002"], "LOW")
    self.assertIn("--model", recorded_find_cmd[0])
    self.assertIn("gemini-2.5-pro", recorded_find_cmd[0])

  @patch("subprocess.run")
  def test_12_native_diff_flag_auto_detection_and_fallback(
      self, mock_run: MagicMock
  ) -> None:
    """Verifies --diff is used when supported by `cm find --help` (including 1-hop neighbor findings) and falls back to `-c` when absent."""
    (self.workspace / "src").mkdir(parents=True, exist_ok=True)
    (self.workspace / "src" / "sanitizer.py").write_text(
        "def sanitize(x): return x\n", encoding="utf-8"
    )
    (self.workspace / "src" / "caller.py").write_text(
        "import os\nfrom src.sanitizer import sanitize\ndef run(u): os.system(sanitize(u))\n",
        encoding="utf-8",
    )
    (self.workspace / ".cm_project").write_text("project: test\n", encoding="utf-8")

    # Case A: `cm find --help` advertises `--diff` -> uses `--diff=origin/main --fail-on=` and keeps 1-hop neighbor finding in `src/caller.py`
    recorded_diff_cmds = []

    def side_effect_with_diff(cmd, **_kwargs):
      if cmd == ["cm", "find", "--help"]:
        return subprocess.CompletedProcess(
            cmd, 0, stdout="Flags:\n  --diff string  Impact-aware PR delta check\n", stderr=""
        )
      if cmd[:2] == ["cm", "find"]:
        recorded_diff_cmds.append(cmd)
        return subprocess.CompletedProcess(cmd, 0, stdout="ok", stderr="")
      if "--name-only" in cmd:
        return subprocess.CompletedProcess(cmd, 0, stdout="src/sanitizer.py\n", stderr="")
      if "diff" in cmd and "-U3" in cmd:
        return subprocess.CompletedProcess(cmd, 0, stdout="+def sanitize(x): return x\n", stderr="")
      if cmd[:4] == ["cm", "report", "--format", "json"]:
        return subprocess.CompletedProcess(
            cmd,
            0,
            stdout=json.dumps([
                {
                    "finding_id": "hop1-0001",
                    "file_path": "src/caller.py",
                    "start_line": 3,
                    "severity": "HIGH",
                    "title": "1-Hop Command Injection via Modified Sanitizer",
                }
            ]),
            stderr="",
        )
      return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

    mock_run.side_effect = side_effect_with_diff
    with patch.dict(
        os.environ,
        {
            "WORKSPACE_DIR": str(self.workspace),
            "SCAN_TARGET": "src/sanitizer.py",
            "IS_PR": "true",
            "DIFF_SCOPED": "true",
            "BASE_REF": "main",
        },
        clear=False,
    ):
      active_with_diff = execute_stage1_presubmit_scan()

    self.assertEqual(len(recorded_diff_cmds), 1)
    self.assertIn("--diff=origin/main", recorded_diff_cmds[0])
    self.assertIn("--fail-on=", recorded_diff_cmds[0])
    self.assertNotIn("-c", recorded_diff_cmds[0])
    self.assertEqual(len(active_with_diff), 1)
    self.assertEqual(active_with_diff[0]["file_path"], "src/caller.py")

    # Case B: `cm find --help` does NOT advertise `--diff` -> falls back to `-c <context_prompt>`
    recorded_fallback_cmds = []

    def side_effect_without_diff(cmd, **_kwargs):
      if cmd == ["cm", "find", "--help"]:
        return subprocess.CompletedProcess(
            cmd, 0, stdout="Flags:\n  -c, --context <text>\n", stderr=""
        )
      if cmd[:2] == ["cm", "find"]:
        recorded_fallback_cmds.append(cmd)
        return subprocess.CompletedProcess(cmd, 0, stdout="ok", stderr="")
      if "--name-only" in cmd:
        return subprocess.CompletedProcess(cmd, 0, stdout="src/sanitizer.py\n", stderr="")
      if "diff" in cmd and "-U3" in cmd:
        return subprocess.CompletedProcess(cmd, 0, stdout="+def sanitize(x): return x\n", stderr="")
      if cmd[:4] == ["cm", "report", "--format", "json"]:
        return subprocess.CompletedProcess(
            cmd,
            0,
            stdout=json.dumps([
                {
                    "finding_id": "direct-0001",
                    "file_path": "src/sanitizer.py",
                    "start_line": 1,
                    "severity": "MEDIUM",
                    "title": "Direct Finding",
                },
                {
                    "finding_id": "untouched-0002",
                    "file_path": "src/caller.py",
                    "start_line": 3,
                    "severity": "HIGH",
                    "title": "Filtered Untouched File in Fallback Mode",
                },
            ]),
            stderr="",
        )
      return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

    mock_run.side_effect = side_effect_without_diff
    with patch.dict(
        os.environ,
        {
            "WORKSPACE_DIR": str(self.workspace),
            "SCAN_TARGET": "src/sanitizer.py",
            "IS_PR": "true",
            "DIFF_SCOPED": "true",
            "BASE_REF": "main",
        },
        clear=False,
    ):
      active_fallback = execute_stage1_presubmit_scan()

    self.assertEqual(len(recorded_fallback_cmds), 1)
    self.assertNotIn("--diff=origin/main", recorded_fallback_cmds[0])
    self.assertIn("-c", recorded_fallback_cmds[0])
    self.assertEqual(len(active_fallback), 1)
    self.assertEqual(active_fallback[0]["file_path"], "src/sanitizer.py")


if __name__ == "__main__":
  unittest.main()



