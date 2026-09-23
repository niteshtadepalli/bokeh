"""Unit tests for the CodeMender 3-Stage Pre-Submit Security Gate workflow.

This test suite directly parses `.github/workflows/codemender_parallel.yml` and
executes the actual embedded Python scripts for Stage 1 (`scan`), Stage 1.5
(`security-gate`), Stage 2 (`worker` 2.1, 2.2, 2.3), and Stage 3 (`aggregate`)
against mocked `cm` CLI outputs and a mocked GitHub REST API.
"""

from __future__ import annotations

import io
import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
import urllib.request
from pathlib import Path
from unittest import mock

import yaml

REPO_ROOT = Path(__file__).resolve().parents[3]
CALLER_WORKFLOW = REPO_ROOT / ".github" / "workflows" / "codemender.yml"
PARALLEL_WORKFLOW = REPO_ROOT / ".github" / "workflows" / "codemender_parallel.yml"


def load_workflow_step_python(job_name: str, step_name_substring: str) -> str:
    """Extracts the embedded `python3 - <<'EOF' ... EOF` script from a workflow step."""
    with open(PARALLEL_WORKFLOW, "r", encoding="utf-8") as f:
        doc = yaml.safe_load(f)
    steps = doc["jobs"][job_name]["steps"]
    for step in steps:
        if step_name_substring in str(step.get("name", "")):
            run_cmd = step.get("run", "")
            marker = "python3 - <<'EOF'\n"
            if marker in run_cmd:
                return run_cmd.split(marker, 1)[1].rsplit("\nEOF", 1)[0]
    raise KeyError(f"Step matching {step_name_substring!r} not found in job {job_name!r}")


class FakeGitHubServer:
    """In-memory mock of GitHub REST API endpoints for Statuses, Issue Comments, and PR Reviews."""

    def __init__(self) -> None:
        self.statuses: list[dict] = []
        self.issue_comments: list[dict] = []
        self.pr_reviews: list[dict] = []
        self._next_comment_id = 1000
        self._next_review_id = 5000

    def urlopen(self, req: urllib.request.Request, timeout: float = 15):
        url = req.full_url
        method = req.get_method()
        body_bytes = req.data or b""
        payload = json.loads(body_bytes.decode("utf-8")) if body_bytes else {}

        # 1. Commit Statuses: POST /repos/{owner}/{repo}/statuses/{sha}
        if "/statuses/" in url and method == "POST":
            self.statuses.append(payload)
            return self._response(201, {"state": payload.get("state"), "description": payload.get("description")})

        # 2. List PR Issue Comments: GET /repos/{owner}/{repo}/issues/{pr}/comments
        if "/issues/" in url and "/comments" in url and method == "GET" and "/issues/comments/" not in url:
            return self._response(200, list(self.issue_comments))

        # 3. Get Single Issue Comment: GET /repos/{owner}/{repo}/issues/comments/{id}
        if "/issues/comments/" in url and method == "GET":
            cid = int(url.rsplit("/", 1)[1])
            for c in self.issue_comments:
                if c["id"] == cid:
                    return self._response(200, c)
            return self._response(404, {})

        # 4. Create Issue Comment: POST /repos/{owner}/{repo}/issues/{pr}/comments
        if "/issues/" in url and url.endswith("/comments") and method == "POST":
            cid = self._next_comment_id
            self._next_comment_id += 1
            comment = {"id": cid, "body": payload.get("body", "")}
            self.issue_comments.append(comment)
            return self._response(201, comment)

        # 5. Update Issue Comment: PATCH /repos/{owner}/{repo}/issues/comments/{id}
        if "/issues/comments/" in url and method == "PATCH":
            cid = int(url.rsplit("/", 1)[1])
            for c in self.issue_comments:
                if c["id"] == cid:
                    c["body"] = payload.get("body", "")
                    return self._response(200, c)
            return self._response(404, {})

        # 6. Create PR Review (Inline Suggestions): POST /repos/{owner}/{repo}/pulls/{pr}/reviews
        if "/pulls/" in url and url.endswith("/reviews") and method == "POST":
            rid = self._next_review_id
            self._next_review_id += 1
            review = {
                "id": rid,
                "html_url": f"https://github.com/test/repo/pull/1#pullrequestreview-{rid}",
                "body": payload.get("body", ""),
                "comments": payload.get("comments", []),
            }
            self.pr_reviews.append(review)
            return self._response(200, review)

        raise AssertionError(f"Unhandled mock GitHub API request: {method} {url}")

    @staticmethod
    def _response(status: int, data: object):
        raw = json.dumps(data).encode("utf-8")

        class _Resp(io.BytesIO):
            def __init__(self) -> None:
                super().__init__(raw)
                self.status = status

            def __enter__(self):
                return self

            def __exit__(self, *args):
                self.close()

        return _Resp()


class TestCodeMenderSecurityGate(unittest.TestCase):
    """Comprehensive unit tests for all 8 CodeMender Security Gate scenarios and edge cases."""

    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.workspace = Path(self.temp_dir.name) / "workspace"
        self.home_dir = Path(self.temp_dir.name) / "home"
        self.workspace.mkdir(parents=True, exist_ok=True)
        (self.home_dir / ".codemender").mkdir(parents=True, exist_ok=True)
        self.github_output = self.workspace / "github_output.txt"
        self.step_summary = self.workspace / "step_summary.md"
        self.github_output.write_text("", encoding="utf-8")
        self.step_summary.write_text("", encoding="utf-8")
        self.gh = FakeGitHubServer()

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def _run_step(self, job: str, step_substr: str, env_overrides: dict[str, str], subprocess_handler) -> int:
        """Executes an embedded workflow Python step with isolated env, filesystem, and mocks."""
        code = load_workflow_step_python(job, step_substr)
        base_env = {
            "PATH": os.environ.get("PATH", ""),
            "HOME": str(self.home_dir),
            "GITHUB_WORKSPACE": str(self.workspace),
            "GITHUB_OUTPUT": str(self.github_output),
            "GITHUB_STEP_SUMMARY": str(self.step_summary),
            "REPO_FULL_NAME": "niteshtadepalli/bokeh",
            "PR_NUMBER": "1",
            "TARGET_SHA": "abcdef1234567890",
            "BASE_REF": "branch-4.0",
            "RUN_URL": "https://github.com/niteshtadepalli/bokeh/actions/runs/999",
            "GITHUB_TOKEN": "ghs_test_token",
            "MIN_SEVERITY": "MEDIUM",
            "FAIL_ON_FINDINGS": "true",
            "SANDBOX_ENABLED": "true",
            "MAX_TASKS": "10",
        }
        base_env.update(env_overrides)

        def fake_expanduser(p: str) -> str:
            if p.startswith("~"):
                return str(self.home_dir) + p[1:]
            return p

        exit_code = 0
        old_cwd = os.getcwd()
        old_sys_path = list(sys.path)
        try:
            os.chdir(self.workspace)
            with (
                mock.patch.dict(os.environ, base_env, clear=True),
                mock.patch("os.path.expanduser", side_effect=fake_expanduser),
                mock.patch("urllib.request.urlopen", side_effect=self.gh.urlopen),
                mock.patch("subprocess.run", side_effect=subprocess_handler),
                mock.patch("time.sleep", return_value=None),
            ):
                try:
                    exec(compile(code, f"{job}:{step_substr}", "exec"), {"__name__": "__main__"})
                except SystemExit as exc:
                    exit_code = int(exc.code or 0)
        finally:
            os.chdir(old_cwd)
            sys.path[:] = old_sys_path
        return exit_code

    def _parse_github_output(self) -> dict[str, str]:
        out = {}
        for line in self.github_output.read_text(encoding="utf-8").splitlines():
            if "=" in line:
                k, v = line.split("=", 1)
                out[k.strip()] = v.strip()
        return out

    # -------------------------------------------------------------------------
    # Test 1: YAML & Embedded Python Compilation
    # -------------------------------------------------------------------------
    def test_01_workflow_yaml_and_embedded_python_syntax(self) -> None:
        with open(CALLER_WORKFLOW, "r", encoding="utf-8") as f:
            caller_doc = yaml.safe_load(f)
        self.assertIn("jobs", caller_doc)
        self.assertIn("remediate", caller_doc["jobs"])

        with open(PARALLEL_WORKFLOW, "r", encoding="utf-8") as f:
            parallel_doc = yaml.safe_load(f)
        self.assertEqual(set(parallel_doc["jobs"].keys()), {"scan", "security-gate", "worker", "aggregate"})

        compiled_steps = 0
        for job_name, job in parallel_doc["jobs"].items():
            for step in job.get("steps", []):
                run_cmd = step.get("run", "")
                if "python3 - <<'EOF'\n" in run_cmd:
                    py_src = run_cmd.split("python3 - <<'EOF'\n", 1)[1].rsplit("\nEOF", 1)[0]
                    compile(py_src, f"{job_name}:{step.get('name')}", "exec")
                    compiled_steps += 1
        self.assertEqual(compiled_steps, 8, "Expected all 8 embedded Python scripts across the 4 jobs to compile")

    # -------------------------------------------------------------------------
    # Test 2: Diff Target Resolution (Code vs. Non-Code / Deleted Files)
    # -------------------------------------------------------------------------
    def test_02_diff_target_resolution_code_vs_non_code(self) -> None:
        # Case A: Only .github/ files (including .github/workflows/tests/*.py), .gitignore, or a deleted .py file -> skip_scan=true
        gh_test_file = self.workspace / ".github" / "workflows" / "tests" / "test_codemender_security_gate.py"
        gh_test_file.parent.mkdir(parents=True, exist_ok=True)
        gh_test_file.write_text("# workflow unit test\n", encoding="utf-8")

        def subproc_non_code(cmd, *args, **kwargs):
            if cmd[:2] == ["git", "diff"]:
                return subprocess.CompletedProcess(
                    cmd,
                    0,
                    stdout=".github/workflows/codemender.yml\n.github/workflows/tests/test_codemender_security_gate.py\n.gitignore\nsrc/deleted_file.py\n",
                    stderr="",
                )
            return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

        rc = self._run_step(
            "scan",
            "Resolve PR Diff Scan Targets",
            {"IS_PR": "true", "DIFF_SCOPED": "true", "BASE_REF": "branch-4.0", "DEFAULT_SCAN_TARGET": "src/bokeh"},
            subproc_non_code,
        )
        self.assertEqual(rc, 0)
        outputs = self._parse_github_output()
        self.assertEqual(outputs.get("skip_scan"), "true")
        self.assertEqual(outputs.get("resolved_target"), "")

        # Case B: Existing .py file modified -> skip_scan=false and target resolved
        self.github_output.write_text("", encoding="utf-8")
        probe_file = self.workspace / "src" / "bokeh" / "probe.py"
        probe_file.parent.mkdir(parents=True, exist_ok=True)
        probe_file.write_text("print('hello')\n", encoding="utf-8")

        def subproc_with_code(cmd, *args, **kwargs):
            if cmd[:2] == ["git", "diff"]:
                return subprocess.CompletedProcess(cmd, 0, stdout="src/bokeh/probe.py\n.gitignore\n", stderr="")
            return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

        rc = self._run_step(
            "scan",
            "Resolve PR Diff Scan Targets",
            {"IS_PR": "true", "DIFF_SCOPED": "true", "BASE_REF": "branch-4.0", "DEFAULT_SCAN_TARGET": "src/bokeh"},
            subproc_with_code,
        )
        self.assertEqual(rc, 0)
        outputs = self._parse_github_output()
        self.assertEqual(outputs.get("skip_scan"), "false")
        self.assertEqual(outputs.get("resolved_target"), "src/bokeh/probe.py")

    # -------------------------------------------------------------------------
    # Test 3: Stage 1 Immediate Block & Security Gate Status
    # -------------------------------------------------------------------------
    def test_03_stage1_and_security_gate_blocking_on_high_and_critical(self) -> None:
        probe_rel = "src/bokeh/util/probe.py"
        probe_abs = self.workspace / probe_rel
        probe_abs.parent.mkdir(parents=True, exist_ok=True)
        probe_abs.write_text("import os, pickle, sqlite3\n", encoding="utf-8")

        mock_findings = [
            {
                "FindingID": "11111111-aaaa-bbbb-cccc-000000000001",
                "FilePath": probe_rel,
                "StartLine": 10,
                "Severity": "CRITICAL",
                "Title": "OS Command Injection in run_report",
                "Description": "Untrusted shell input passed to subprocess.",
            },
            {
                "FindingID": "22222222-aaaa-bbbb-cccc-000000000002",
                "FilePath": probe_rel,
                "StartLine": 25,
                "Severity": "HIGH",
                "Title": "SQL Injection in lookup_theme",
                "Description": "Unparameterized SQL query string.",
            },
        ]

        def stage1_subproc(cmd, *args, **kwargs):
            if cmd[:2] == ["git", "diff"] and "--name-only" in cmd:
                return subprocess.CompletedProcess(cmd, 0, stdout=f"{probe_rel}\n", stderr="")
            if cmd[:2] == ["cm", "report"] and "json" in cmd:
                return subprocess.CompletedProcess(cmd, 0, stdout=json.dumps({"findings": mock_findings}), stderr="")
            return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

        rc1 = self._run_step("scan", "Execute `cm find`", {"SCAN_TARGETS": probe_rel}, stage1_subproc)
        self.assertEqual(rc1, 0)
        outputs = self._parse_github_output()
        self.assertEqual(outputs["findings_count"], "2")
        self.assertEqual(outputs["blocking_count"], "2")
        self.assertEqual(outputs["advisory_count"], "0")
        self.assertEqual(json.loads(outputs["matrix"]), [0, 1])

        self.assertEqual(len(self.gh.issue_comments), 1)
        sticky_body = self.gh.issue_comments[0]["body"]
        self.assertIn("❌ **BLOCKED** — `2` vulnerability(ies) `>= MEDIUM`", sticky_body)
        self.assertIn("<!-- cm-row:11111111-aaaa-bbbb-cccc-000000000001 -->", sticky_body)
        self.assertIn("<!-- cm-row:22222222-aaaa-bbbb-cccc-000000000002 -->", sticky_body)

        rc_gate = self._run_step(
            "security-gate",
            "Enforce Immediate Pre-Submit Security Gate",
            {
                "SCAN_RESULT": "success",
                "FINDINGS_COUNT": "2",
                "BLOCKING_COUNT": "2",
                "ADVISORY_COUNT": "0",
            },
            stage1_subproc,
        )
        self.assertEqual(rc_gate, 0)
        self.assertEqual(self.gh.statuses[-1]["state"], "failure")
        self.assertIn("BLOCKED: 2 vulnerability(ies) >= MEDIUM", self.gh.statuses[-1]["description"])
        self.assertIn("❌ BLOCKED (Pending Stage 2 Verification)", self.step_summary.read_text(encoding="utf-8"))

    # -------------------------------------------------------------------------
    # Test 4: Stage 2 Parallel Workers (2.1 -> 2.2 -> 2.3) & Stage 3 Shard Merge
    # -------------------------------------------------------------------------
    def test_04_stage2_parallel_workers_and_stage3_shard_merge(self) -> None:
        self.test_03_stage1_and_security_gate_blocking_on_high_and_critical()
        probe_rel = "src/bokeh/util/probe.py"
        sample_diff = (
            f"diff --git a/{probe_rel} b/{probe_rel}\n"
            f"--- a/{probe_rel}\n"
            f"+++ b/{probe_rel}\n"
            "@@ -10,1 +10,1 @@\n"
            "-    os.system(user_cmd)\n"
            "+    subprocess.run(['echo', user_cmd], check=True)\n"
        )

        def worker_subproc(cmd, *args, **kwargs):
            if cmd[:2] == ["cm", "verify"]:
                return subprocess.CompletedProcess(cmd, 0, stdout="Status: CONFIRMED EXPLOITABLE\n", stderr="")
            if cmd[:2] == ["cm", "fix"]:
                return subprocess.CompletedProcess(cmd, 0, stdout="Patch generated.\n", stderr="")
            if cmd[:2] == ["git", "diff"]:
                return subprocess.CompletedProcess(cmd, 0, stdout=sample_diff, stderr="")
            return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

        for w_idx in ("0", "1"):
            self._run_step("worker", "Initialize Worker Workspace", {"WORKER_INDEX": w_idx}, worker_subproc)
            self._run_step("worker", "Stage 2.1: Exploit Verification", {"WORKER_INDEX": w_idx}, worker_subproc)
            self._run_step("worker", "Stage 2.2: Patch Synthesis", {"WORKER_INDEX": w_idx}, worker_subproc)
            self._run_step("worker", "Stage 2.3: Post Inline PR Review Suggestions", {"WORKER_INDEX": w_idx}, worker_subproc)

        self.assertEqual(len(self.gh.pr_reviews), 2)
        self.assertIn("```suggestion", self.gh.pr_reviews[0]["comments"][0]["body"])
        live_sticky = self.gh.issue_comments[0]["body"]
        self.assertIn("Stage 2 Complete: 2/2 Findings Processed", live_sticky)
        self.assertIn("✅ **Patch Ready**", live_sticky)

        rc_agg = self._run_step("aggregate", "Update Sticky PR Comment", {}, worker_subproc)
        self.assertEqual(rc_agg, 1)
        report_data = json.loads((self.workspace / "report.json").read_text(encoding="utf-8"))
        self.assertEqual(len(report_data["findings"]), 2)

    # -------------------------------------------------------------------------
    # Test 5: Non-Blocking Advisory Finding (LOW < MEDIUM)
    # -------------------------------------------------------------------------
    def test_05_low_advisory_finding_passes_gate_and_preserves_banner(self) -> None:
        probe_rel = "src/bokeh/util/advisory.py"
        (self.workspace / "src" / "bokeh" / "util").mkdir(parents=True, exist_ok=True)
        (self.workspace / probe_rel).write_text("import hashlib\n", encoding="utf-8")

        low_finding = [
            {
                "FindingID": "33333333-aaaa-bbbb-cccc-000000000003",
                "FilePath": probe_rel,
                "StartLine": 5,
                "Severity": "LOW",
                "Title": "Weak MD5 hash used for cache key",
                "Description": "Use SHA-256 instead of MD5.",
            }
        ]

        def subproc(cmd, *args, **kwargs):
            if cmd[:2] == ["git", "diff"] and "--name-only" in cmd:
                return subprocess.CompletedProcess(cmd, 0, stdout=f"{probe_rel}\n", stderr="")
            if cmd[:2] == ["cm", "report"] and "json" in cmd:
                return subprocess.CompletedProcess(cmd, 0, stdout=json.dumps({"findings": low_finding}), stderr="")
            return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

        self._run_step("scan", "Execute `cm find`", {"SCAN_TARGETS": probe_rel}, subproc)
        outputs = self._parse_github_output()
        self.assertEqual(outputs["blocking_count"], "0")
        self.assertEqual(outputs["advisory_count"], "1")

        rc_gate = self._run_step(
            "security-gate",
            "Enforce Immediate Pre-Submit Security Gate",
            {"SCAN_RESULT": "success", "FINDINGS_COUNT": "1", "BLOCKING_COUNT": "0", "ADVISORY_COUNT": "1"},
            subproc,
        )
        self.assertEqual(rc_gate, 0)
        self.assertEqual(self.gh.statuses[-1]["state"], "success")
        self.assertIn("1 Low/Info advisory finding(s)", self.gh.statuses[-1]["description"])

        # Run Worker 0 + Stage 3 -> must exit 0 and preserve "(1 advisory finding(s))" in Stage 3 banner
        self._run_step("worker", "Initialize Worker Workspace", {"WORKER_INDEX": "0"}, subproc)
        self._run_step("worker", "Stage 2.1: Exploit Verification", {"WORKER_INDEX": "0"}, subproc)
        self._run_step("worker", "Stage 2.2: Patch Synthesis", {"WORKER_INDEX": "0"}, subproc)
        self._run_step("worker", "Stage 2.3: Post Inline PR Review Suggestions", {"WORKER_INDEX": "0"}, subproc)

        rc_agg = self._run_step("aggregate", "Update Sticky PR Comment", {}, subproc)
        self.assertEqual(rc_agg, 0)
        self.assertIn("(`1` advisory finding(s))", self.gh.issue_comments[0]["body"])
        self.assertIn("ℹ️ Advisory", self.gh.issue_comments[0]["body"])

    # -------------------------------------------------------------------------
    # Test 6: False-Positive Dismissal Auto-Unblocks Stage 3 & Filters SARIF
    # -------------------------------------------------------------------------
    def test_06_false_positive_dismissal_auto_unblocks_stage3_and_filters_sarif(self) -> None:
        probe_rel = "src/bokeh/util/fp_probe.py"
        (self.workspace / "src" / "bokeh" / "util").mkdir(parents=True, exist_ok=True)
        (self.workspace / probe_rel).write_text("def lookup(name): pass\n", encoding="utf-8")

        fp_finding = [
            {
                "FindingID": "4d395fe6-f3eb-5048-a6e7-327e97638e0e",
                "FilePath": probe_rel,
                "StartLine": 35,
                "Severity": "HIGH",
                "Title": "SQL Injection in lookup_theme_by_name",
                "Description": "Guarded by strict allowlist.",
            }
        ]
        raw_sarif = {
            "version": "2.1.0",
            "runs": [
                {
                    "tool": {"driver": {"name": "CodeMender"}},
                    "results": [
                        {
                            "ruleId": "SQLI",
                            "message": {"text": "Finding 4d395fe6-f3eb-5048-a6e7-327e97638e0e"},
                            "locations": [
                                {
                                    "physicalLocation": {
                                        "artifactLocation": {"uri": probe_rel},
                                        "region": {"startLine": 35},
                                    }
                                }
                            ],
                        }
                    ],
                }
            ],
        }

        def subproc(cmd, *args, **kwargs):
            if cmd[:2] == ["git", "diff"] and "--name-only" in cmd:
                return subprocess.CompletedProcess(cmd, 0, stdout=f"{probe_rel}\n", stderr="")
            if cmd[:2] == ["cm", "report"] and "json" in cmd:
                return subprocess.CompletedProcess(cmd, 0, stdout=json.dumps({"findings": fp_finding}), stderr="")
            if cmd[:2] == ["cm", "report"] and "sarif" in cmd:
                return subprocess.CompletedProcess(cmd, 0, stdout=json.dumps(raw_sarif), stderr="")
            if cmd[:2] == ["cm", "verify"]:
                return subprocess.CompletedProcess(cmd, 0, stdout="Verdict: FALSE_POSITIVE (not exploitable)\n", stderr="")
            if cmd[:2] == ["cm", "fix"]:
                raise AssertionError("cm fix should NOT be called for a DISMISSED_FALSE_POSITIVE finding!")
            return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

        # Stage 1 + security-gate blocks initially
        self._run_step("scan", "Execute `cm find`", {"SCAN_TARGETS": probe_rel}, subproc)
        self._run_step(
            "security-gate",
            "Enforce Immediate Pre-Submit Security Gate",
            {"SCAN_RESULT": "success", "FINDINGS_COUNT": "1", "BLOCKING_COUNT": "1", "ADVISORY_COUNT": "0"},
            subproc,
        )
        self.assertEqual(self.gh.statuses[-1]["state"], "failure")

        # Stage 2.1 dismisses finding as FALSE_POSITIVE; Stage 2.2 & 2.3 skip cm fix and inline review
        self._run_step("worker", "Initialize Worker Workspace", {"WORKER_INDEX": "0"}, subproc)
        self._run_step("worker", "Stage 2.1: Exploit Verification", {"WORKER_INDEX": "0"}, subproc)
        self._run_step("worker", "Stage 2.2: Patch Synthesis", {"WORKER_INDEX": "0"}, subproc)
        self._run_step("worker", "Stage 2.3: Post Inline PR Review Suggestions", {"WORKER_INDEX": "0"}, subproc)
        self.assertEqual(len(self.gh.pr_reviews), 0)

        # Stage 3 auto-unblocks Commit Status -> "success", updates sticky banner, and strips FP from report.sarif
        rc_agg = self._run_step("aggregate", "Update Sticky PR Comment", {}, subproc)
        self.assertEqual(rc_agg, 0)
        self.assertEqual(self.gh.statuses[-1]["state"], "success")
        self.assertIn("1 false positive(s) dismissed by cm verify", self.gh.statuses[-1]["description"])
        self.assertIn("✅ **PASSED (Auto-Unblocked)**", self.gh.issue_comments[0]["body"])
        self.assertIn("⚪ Dismissed (FP)", self.gh.issue_comments[0]["body"])

        filtered_sarif = json.loads((self.workspace / "report.sarif").read_text(encoding="utf-8"))
        self.assertEqual(len(filtered_sarif["runs"][0]["results"]), 0, "Dismissed FP must be stripped from report.sarif")

    # -------------------------------------------------------------------------
    # Test 7: Fail-Closed Hardening (Verify Error / "UNVERIFIED" / Missing Worker Shard)
    # -------------------------------------------------------------------------
    def test_07_fail_closed_on_verify_error_or_missing_worker_shard(self) -> None:
        probe_rel = "src/bokeh/util/fail_closed.py"
        (self.workspace / "src" / "bokeh" / "util").mkdir(parents=True, exist_ok=True)
        (self.workspace / probe_rel).write_text("eval(user_input)\n", encoding="utf-8")

        high_finding = [
            {
                "FindingID": "77777777-aaaa-bbbb-cccc-000000000007",
                "FilePath": probe_rel,
                "StartLine": 1,
                "Severity": "HIGH",
                "Title": "Code Injection via eval",
                "Description": "Arbitrary code execution.",
            }
        ]

        # Even if `cm verify` prints "UNVERIFIED" or exits non-zero, it MUST NOT dismiss the finding
        def subproc_unverified(cmd, *args, **kwargs):
            if cmd[:2] == ["git", "diff"] and "--name-only" in cmd:
                return subprocess.CompletedProcess(cmd, 0, stdout=f"{probe_rel}\n", stderr="")
            if cmd[:2] == ["cm", "report"] and "json" in cmd:
                return subprocess.CompletedProcess(cmd, 0, stdout=json.dumps({"findings": high_finding}), stderr="")
            if cmd[:2] == ["cm", "verify"]:
                return subprocess.CompletedProcess(cmd, 0, stdout="Result: UNVERIFIED (timeout)\n", stderr="")
            return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

        self._run_step("scan", "Execute `cm find`", {"SCAN_TARGETS": probe_rel}, subproc_unverified)
        self._run_step("worker", "Initialize Worker Workspace", {"WORKER_INDEX": "0"}, subproc_unverified)
        self._run_step("worker", "Stage 2.1: Exploit Verification", {"WORKER_INDEX": "0"}, subproc_unverified)

        shard_path = self.workspace / ".codemender_transit" / "shards" / "worker_0" / "results_worker_0.json"
        shard_items = json.loads(shard_path.read_text(encoding="utf-8"))
        self.assertEqual(shard_items[0]["verified_status"], "CONFIRMED")

        # Now simulate a worker crash where `results_worker_0.json` was deleted/never uploaded:
        # Stage 3 (`aggregate`) must fall back to `active_findings.json` and still fail closed (exit 1)!
        shard_path.unlink()
        rc_agg = self._run_step("aggregate", "Update Sticky PR Comment", {}, subproc_unverified)
        self.assertEqual(rc_agg, 1, "Stage 3 must fail closed (exit 1) when a worker shard is missing")

    # -------------------------------------------------------------------------
    # Test 8: Sticky Comment Auto-Resolution on Clean Rescan / File Deletion (`skip_scan=true`)
    # -------------------------------------------------------------------------
    def test_08_sticky_comment_resolved_on_clean_rescan_or_skip_scan(self) -> None:
        # Seed an existing sticky comment from a previous blocked commit
        self.gh.issue_comments.append(
            {
                "id": 5800237576,
                "body": "<!-- codemender-security-summary -->\n### 🛡️ Old Blocked Report\n**Gate Status:** ❌ **BLOCKED**",
            }
        )

        # Run `security-gate` with FINDINGS_COUNT="" (simulating `skip_scan=true` after deleting the vulnerable file)
        rc_gate = self._run_step(
            "security-gate",
            "Enforce Immediate Pre-Submit Security Gate",
            {
                "SCAN_RESULT": "success",
                "FINDINGS_COUNT": "",
                "BLOCKING_COUNT": "0",
                "ADVISORY_COUNT": "0",
                "TARGET_SHA": "d55260c297555863",
            },
            lambda cmd, *a, **kw: subprocess.CompletedProcess(cmd, 0, stdout="", stderr=""),
        )
        self.assertEqual(rc_gate, 0)
        self.assertEqual(self.gh.statuses[-1]["state"], "success")
        updated_comment = self.gh.issue_comments[0]["body"]
        self.assertIn("*(All Findings Resolved)*", updated_comment)
        self.assertIn("(`d55260c2`)", updated_comment)


if __name__ == "__main__":
    unittest.main()
