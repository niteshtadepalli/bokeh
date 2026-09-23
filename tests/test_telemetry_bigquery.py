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

"""Unit tests for the BigQuery analytics telemetry exporter."""

import os
import sqlite3
import tempfile
import unittest
from unittest.mock import MagicMock, patch

from codemender_agent.telemetry import bigquery as bq


# The exact 23-column `findings` schema emitted by the external `cm` binary,
# reproduced verbatim so these tests fail if our allowlist drifts from reality.
_REAL_FINDINGS_COLUMNS = [
    "finding_id", "session_id", "title", "file_path", "severity", "confidence",
    "analysis", "snippet", "vuln_type", "vuln_id", "verified", "muted",
    "mute_reason", "created_at", "fingerprint", "status", "source_stage",
    "finding_json", "updated_at", "start_line", "end_line", "dismiss_reason",
    "confidence_level",
]

_REAL_PATCHES_COLUMNS = [
    "patch_id", "finding_id", "session_id", "diff", "reasoning", "status",
    "backup_path", "created_at", "validation_result", "target_file",
    "edited_files",
]


def _make_state_db(path, columns=_REAL_FINDINGS_COLUMNS, rows=None,
                   patches_columns=_REAL_PATCHES_COLUMNS, patches_rows=None):
  """Builds a state.db with an arbitrary findings/patches schema."""
  conn = sqlite3.connect(path)
  cur = conn.cursor()
  cur.execute(f"CREATE TABLE findings ({', '.join(f'{c} TEXT' for c in columns)})")
  for row in rows or []:
    placeholders = ", ".join("?" for _ in columns)
    cur.execute(
        f"INSERT INTO findings ({', '.join(columns)}) VALUES ({placeholders})",
        [row.get(c) for c in columns],
    )
  if patches_columns is not None:
    cur.execute(
        f"CREATE TABLE patches ({', '.join(f'{c} TEXT' for c in patches_columns)})"
    )
    for row in patches_rows or []:
      placeholders = ", ".join("?" for _ in patches_columns)
      cur.execute(
          f"INSERT INTO patches ({', '.join(patches_columns)}) VALUES ({placeholders})",
          [row.get(c) for c in patches_columns],
      )
  conn.commit()
  conn.close()


class TestTokenFlattening(unittest.TestCase):
  """token_usage.json is keyed per model; BigQuery needs a repeated struct."""

  def test_flattens_per_model_usage(self):
    rows = bq.flatten_token_totals({
        "gemini-2.5-pro": {"in_tokens": 100, "out_tokens": 20, "total_tokens": 120},
        "gemini-2.5-flash": {"in_tokens": 5, "out_tokens": 1, "total_tokens": 6},
    })
    self.assertEqual(len(rows), 2)
    # Sorted by model name for deterministic output.
    self.assertEqual(rows[0]["model"], "gemini-2.5-flash")
    self.assertEqual(rows[1]["model"], "gemini-2.5-pro")
    self.assertEqual(rows[1]["in_tokens"], 100)
    self.assertEqual(rows[1]["total_tokens"], 120)

  def test_derives_total_when_absent(self):
    rows = bq.flatten_token_totals({"m": {"in_tokens": 7, "out_tokens": 3}})
    self.assertEqual(rows[0]["total_tokens"], 10)

  def test_handles_scalar_shape(self):
    """Some cm versions emit a bare scalar rather than a nested dict."""
    rows = bq.flatten_token_totals({"default": 42})
    self.assertEqual(rows[0]["total_tokens"], 42)
    self.assertEqual(rows[0]["in_tokens"], 0)

  def test_empty_and_malformed_inputs(self):
    self.assertEqual(bq.flatten_token_totals(None), [])
    self.assertEqual(bq.flatten_token_totals({}), [])
    self.assertEqual(bq.flatten_token_totals("not-a-dict"), [])
    self.assertEqual(bq.flatten_token_totals({"m": ["unexpected"]}), [])


class TestStateDbSnapshot(unittest.TestCase):
  """The cm-owned schema can drift, so reads must be allowlisted and tolerant."""

  def setUp(self):
    self.tmp = tempfile.TemporaryDirectory()
    self.db_path = os.path.join(self.tmp.name, "state.db")

  def tearDown(self):
    self.tmp.cleanup()

  def test_reads_real_23_column_schema(self):
    _make_state_db(self.db_path, rows=[{
        "finding_id": "f1",
        "title": "SQL Injection",
        "file_path": "app/db.py",
        "severity": "HIGH",
        "confidence_level": "HIGH",
        "vuln_type": "SQL Injection",
        "vuln_id": "CWE-89",
        "status": "FIXED",
        "start_line": "10",
        "end_line": "12",
        "analysis": "prose",
        "snippet": "SELECT *",
    }])
    snapshot = bq.snapshot_state_db_findings(self.db_path)
    self.assertEqual(len(snapshot), 1)
    self.assertEqual(snapshot[0]["finding_id"], "f1")
    self.assertEqual(snapshot[0]["severity"], "HIGH")

  def test_allowlist_excludes_non_exported_columns(self):
    """Columns outside the allowlist must never be read, even though they exist."""
    _make_state_db(self.db_path, rows=[{"finding_id": "f1"}])
    snapshot = bq.snapshot_state_db_findings(self.db_path)
    # `finding_json` and `session_id` exist in the real schema but are
    # deliberately not exported.
    self.assertNotIn("finding_json", snapshot[0])
    self.assertNotIn("session_id", snapshot[0])
    self.assertNotIn("finding_json", bq.FINDINGS_COLUMN_ALLOWLIST)

  def test_tolerates_missing_columns_from_older_cm(self):
    """An older cm lacking newer columns must not break the export."""
    reduced = ["finding_id", "title", "severity", "status"]
    _make_state_db(self.db_path, columns=reduced,
                   rows=[{"finding_id": "f1", "title": "t", "severity": "LOW",
                          "status": "DETECTED"}])
    snapshot = bq.snapshot_state_db_findings(self.db_path)
    self.assertEqual(len(snapshot), 1)
    self.assertEqual(snapshot[0]["title"], "t")
    self.assertNotIn("fingerprint", snapshot[0])

  def test_tolerates_extra_columns_from_newer_cm(self):
    """A newer cm adding columns must be silently ignored, not crash."""
    extended = _REAL_FINDINGS_COLUMNS + ["brand_new_column_v1", "another_new_one"]
    _make_state_db(self.db_path, columns=extended,
                   rows=[{"finding_id": "f1", "brand_new_column_v1": "x"}])
    snapshot = bq.snapshot_state_db_findings(self.db_path)
    self.assertEqual(len(snapshot), 1)
    self.assertNotIn("brand_new_column_v1", snapshot[0])

  def test_joins_patch_status(self):
    _make_state_db(
        self.db_path,
        rows=[{"finding_id": "f1"}],
        patches_rows=[
            {"patch_id": "p1", "finding_id": "f1", "status": "APPLIED",
             "created_at": "2026-01-01"},
            {"patch_id": "p2", "finding_id": "f1", "status": "FIXED",
             "created_at": "2026-01-02"},
        ],
    )
    snapshot = bq.snapshot_state_db_findings(self.db_path)
    # Latest patch attempt wins.
    self.assertEqual(snapshot[0]["patch_status"], "FIXED")

  def test_missing_patches_table_is_survivable(self):
    _make_state_db(self.db_path, rows=[{"finding_id": "f1"}], patches_columns=None)
    snapshot = bq.snapshot_state_db_findings(self.db_path)
    self.assertEqual(len(snapshot), 1)

  def test_missing_findings_table_returns_empty(self):
    conn = sqlite3.connect(self.db_path)
    conn.execute("CREATE TABLE unrelated (x TEXT)")
    conn.commit()
    conn.close()
    self.assertEqual(bq.snapshot_state_db_findings(self.db_path), [])

  def test_nonexistent_and_corrupt_db_return_empty(self):
    self.assertEqual(bq.snapshot_state_db_findings("/no/such/file.db"), [])
    self.assertEqual(bq.snapshot_state_db_findings(""), [])
    corrupt = os.path.join(self.tmp.name, "corrupt.db")
    with open(corrupt, "wb") as f:
      f.write(b"this is definitely not sqlite")
    self.assertEqual(bq.snapshot_state_db_findings(corrupt), [])


class TestSchemaMapping(unittest.TestCase):
  """state.db rows must map onto the declared BigQuery column set."""

  def setUp(self):
    self.ctx = bq.ScanRunContext(
        stage="aggregate",
        scan_id="scan-1",
        repository="acme/widgets",
        target_branch="main",
        target_sha="abc123",
        scan_target=".",
        cm_version="cm 0.9.0",
        find_model="gemini-2.5-pro",
        active_findings_count=2,
        report_uri="gs://bucket/reports/x.html",
    )

  def test_scan_run_row_shape(self):
    self.ctx.token_totals = {"m": {"in_tokens": 1, "out_tokens": 2, "total_tokens": 3}}
    row = bq.build_scan_run_row(self.ctx, status=bq.STATUS_SUCCESS)
    self.assertEqual(row["scan_id"], "scan-1")
    self.assertEqual(row["repository"], "acme/widgets")
    self.assertEqual(row["status"], "SUCCESS")
    self.assertEqual(row["cm_version"], "cm 0.9.0")
    self.assertEqual(row["report_uri"], "gs://bucket/reports/x.html")
    self.assertIsInstance(row["duration_seconds"], float)
    self.assertEqual(row["token_totals"][0]["total_tokens"], 3)

  def test_failed_row_carries_reason(self):
    row = bq.build_scan_run_row(
        self.ctx, status=bq.STATUS_FAILED, failure_reason="boom"
    )
    self.assertEqual(row["status"], "FAILED")
    self.assertEqual(row["failure_reason"], "boom")

  def test_explicit_duration_overrides_elapsed(self):
    row = bq.build_scan_run_row(self.ctx, bq.STATUS_SUCCESS, duration_seconds=930.5)
    self.assertEqual(row["duration_seconds"], 930.5)

  def test_finding_row_mapping_and_coercion(self):
    rows = bq.build_finding_rows(self.ctx, [{
        "finding_id": "f1",
        "title": "SQL Injection in login",
        "vuln_type": "SQL Injection",
        "vuln_id": "cwe-89",
        "severity": "high",
        "confidence_level": "medium",
        "file_path": "app/db.py",
        "start_line": "10",
        "end_line": "12",
        "status": "fixed",
        "verified": 1,
        "muted": 0,
        "fingerprint": "deadbeef",
    }], finding_prs={"f1": "https://github.com/acme/widgets/pull/7"},
        with_snippets=False)
    row = rows[0]
    self.assertEqual(row["scan_id"], "scan-1")
    self.assertEqual(row["cwe_id"], "CWE-89")       # normalized to upper case
    self.assertEqual(row["severity"], "HIGH")       # normalized
    self.assertEqual(row["confidence_level"], "MEDIUM")
    self.assertEqual(row["start_line"], 10)         # coerced to int
    self.assertIs(row["verified"], True)            # coerced to bool
    self.assertIs(row["muted"], False)
    self.assertEqual(row["fix_pr_url"], "https://github.com/acme/widgets/pull/7")

  def test_cwe_inferred_from_title_when_vuln_id_absent(self):
    rows = bq.build_finding_rows(
        self.ctx, [{"finding_id": "f1", "title": "Path traversal (CWE-22) found"}]
    )
    self.assertEqual(rows[0]["cwe_id"], "CWE-22")

  def test_cwe_none_when_undeterminable(self):
    rows = bq.build_finding_rows(self.ctx, [{"finding_id": "f1", "title": "Bug"}])
    self.assertIsNone(rows[0]["cwe_id"])

  def test_rows_without_finding_id_are_dropped(self):
    rows = bq.build_finding_rows(
        self.ctx, [{"title": "orphan"}, {"finding_id": "", "title": "empty"},
                   {"finding_id": "ok"}]
    )
    self.assertEqual([r["finding_id"] for r in rows], ["ok"])

  def test_remediation_summary(self):
    counts = bq.summarize_remediation([
        {"finding_id": "a", "status": "FIXED"},
        {"finding_id": "b", "status": "PR_CREATION_FAILED"},
        {"finding_id": "c", "status": "SKIPPED_DUPLICATE"},
        {"finding_id": "d", "status": "DETECTED"},
    ])
    self.assertEqual(counts["fixed"], 1)
    self.assertEqual(counts["failed_fix"], 1)
    self.assertEqual(counts["skipped_duplicate"], 1)
    self.assertEqual(counts["total"], 4)


class TestSnippetGating(unittest.TestCase):
  """Source code and LLM prose must stay out of the warehouse by default."""

  def setUp(self):
    self.ctx = bq.ScanRunContext(scan_id="s", repository="a/b")
    self.finding = {
        "finding_id": "f1",
        "analysis": "This is exploitable because...",
        "snippet": "query = 'SELECT * FROM u WHERE id=' + uid",
    }

  def test_excluded_by_default(self):
    with patch.dict(os.environ, {}, clear=True):
      row = bq.build_finding_rows(self.ctx, [self.finding])[0]
    self.assertNotIn("analysis", row)
    self.assertNotIn("snippet", row)

  def test_excluded_when_flag_is_false(self):
    with patch.dict(os.environ, {bq.ENV_INCLUDE_SNIPPETS: "false"}, clear=True):
      row = bq.build_finding_rows(self.ctx, [self.finding])[0]
    self.assertNotIn("snippet", row)

  def test_included_only_when_opted_in(self):
    with patch.dict(os.environ, {bq.ENV_INCLUDE_SNIPPETS: "true"}, clear=True):
      row = bq.build_finding_rows(self.ctx, [self.finding])[0]
    self.assertEqual(row["snippet"], "query = 'SELECT * FROM u WHERE id=' + uid")
    self.assertEqual(row["analysis"], "This is exploitable because...")


class TestUnsetDatasetIsHardNoOp(unittest.TestCase):
  """With no dataset configured, absolutely nothing may touch BigQuery."""

  def test_telemetry_disabled_without_dataset(self):
    with patch.dict(os.environ, {}, clear=True):
      self.assertFalse(bq.telemetry_enabled())
      self.assertFalse(bq.BigQueryTelemetryExporter().enabled)

  def test_blank_dataset_is_treated_as_unset(self):
    with patch.dict(os.environ, {bq.ENV_DATASET: "   "}, clear=True):
      self.assertFalse(bq.telemetry_enabled())

  def test_no_client_is_ever_constructed(self):
    """The client factory must not be invoked at all when unconfigured."""
    factory = MagicMock(side_effect=AssertionError("client must not be built"))
    with patch.dict(os.environ, {}, clear=True):
      exporter = bq.BigQueryTelemetryExporter(client_factory=factory)
      result = bq.emit_scan_telemetry(
          bq.ScanRunContext(scan_id="s"),
          findings=[{"finding_id": "f1"}],
          exporter=exporter,
      )
    self.assertFalse(result)
    factory.assert_not_called()

  def test_no_insert_calls_are_made(self):
    client = MagicMock()
    with patch.dict(os.environ, {}, clear=True):
      exporter = bq.BigQueryTelemetryExporter(client=client)
      bq.emit_scan_telemetry(bq.ScanRunContext(scan_id="s"), exporter=exporter)
    client.insert_rows_json.assert_not_called()


class TestExportWhenEnabled(unittest.TestCase):

  def setUp(self):
    self.client = MagicMock()
    self.client.insert_rows_json.return_value = []
    self.env = patch.dict(
        os.environ,
        {bq.ENV_DATASET: "codemender_telemetry", bq.ENV_PROJECT: "proj-1"},
        clear=True,
    )
    self.env.start()
    self.exporter = bq.BigQueryTelemetryExporter(client=self.client)

  def tearDown(self):
    self.env.stop()

  def test_writes_both_tables(self):
    ok = bq.emit_scan_telemetry(
        bq.ScanRunContext(scan_id="s1", repository="a/b"),
        findings=[{"finding_id": "f1", "status": "FIXED"}],
        exporter=self.exporter,
    )
    self.assertTrue(ok)
    tables = [c.args[0] for c in self.client.insert_rows_json.call_args_list]
    self.assertIn("proj-1.codemender_telemetry.scan_runs", tables)
    self.assertIn("proj-1.codemender_telemetry.vulnerability_findings", tables)

  def test_counts_are_derived_from_findings(self):
    ctx = bq.ScanRunContext(scan_id="s1", repository="a/b")
    bq.emit_scan_telemetry(
        ctx,
        findings=[{"finding_id": "f1", "status": "FIXED"},
                  {"finding_id": "f2", "status": "PR_CREATION_FAILED"}],
        exporter=self.exporter,
    )
    run_row = self.client.insert_rows_json.call_args_list[0].args[1][0]
    self.assertEqual(run_row["fixed_count"], 1)
    self.assertEqual(run_row["failed_fix_count"], 1)

  def test_rejected_rows_are_reported_as_failure_not_raised(self):
    self.client.insert_rows_json.return_value = [{"index": 0, "errors": ["bad"]}]
    ok = bq.emit_scan_telemetry(
        bq.ScanRunContext(scan_id="s1"), exporter=self.exporter
    )
    self.assertFalse(ok)

  def test_second_emit_for_same_context_is_suppressed(self):
    ctx = bq.ScanRunContext(scan_id="s1")
    bq.emit_scan_telemetry(ctx, exporter=self.exporter)
    self.client.insert_rows_json.reset_mock()
    bq.emit_scan_telemetry(ctx, exporter=self.exporter)
    self.client.insert_rows_json.assert_not_called()


class TestTelemetryNeverFailsTheScan(unittest.TestCase):
  """A broken warehouse must degrade telemetry only, never the scan."""

  def test_raising_client_does_not_propagate(self):
    client = MagicMock()
    client.insert_rows_json.side_effect = RuntimeError("BigQuery is on fire")
    with patch.dict(os.environ, {bq.ENV_DATASET: "ds"}, clear=True):
      exporter = bq.BigQueryTelemetryExporter(client=client)
      result = bq.emit_scan_telemetry(
          bq.ScanRunContext(scan_id="s1"),
          findings=[{"finding_id": "f1"}],
          exporter=exporter,
      )
    self.assertFalse(result)

  def test_raising_client_factory_does_not_propagate(self):
    """Covers a missing google-cloud-bigquery install or broken credentials."""
    def _boom(_project):
      raise ImportError("No module named 'google.cloud.bigquery'")

    with patch.dict(os.environ, {bq.ENV_DATASET: "ds"}, clear=True):
      exporter = bq.BigQueryTelemetryExporter(client_factory=_boom)
      result = bq.emit_scan_telemetry(
          bq.ScanRunContext(scan_id="s1"), exporter=exporter
      )
    self.assertFalse(result)

  def test_unserializable_context_does_not_propagate(self):
    class Exploding:

      def __str__(self):
        raise ValueError("cannot stringify")

    ctx = bq.ScanRunContext(scan_id="s1")
    ctx.repository = Exploding()
    client = MagicMock()
    client.insert_rows_json.return_value = []
    with patch.dict(os.environ, {bq.ENV_DATASET: "ds"}, clear=True):
      exporter = bq.BigQueryTelemetryExporter(client=client)
      result = bq.emit_scan_telemetry(ctx, exporter=exporter)
    self.assertFalse(result)


class TestFailureGuard(unittest.TestCase):
  """Every abnormal termination must still yield exactly one FAILED row."""

  def setUp(self):
    self.client = MagicMock()
    self.client.insert_rows_json.return_value = []
    self.env = patch.dict(os.environ, {bq.ENV_DATASET: "ds"}, clear=True)
    self.env.start()

  def tearDown(self):
    self.env.stop()

  def _rows(self):
    return [c.args[1][0] for c in self.client.insert_rows_json.call_args_list]

  def test_nonzero_sys_exit_emits_failed_row_and_reraises(self):
    ctx = bq.ScanRunContext(stage="scan", scan_id="s1", repository="a/b")
    exporter = bq.BigQueryTelemetryExporter(client=self.client)
    with patch.object(bq, "BigQueryTelemetryExporter", return_value=exporter):
      with self.assertRaises(SystemExit) as cm:
        with bq.telemetry_run_guard(ctx):
          raise SystemExit(1)
    self.assertEqual(cm.exception.code, 1)  # exit code preserved
    rows = self._rows()
    self.assertEqual(len(rows), 1)
    self.assertEqual(rows[0]["status"], "FAILED")
    self.assertEqual(rows[0]["repository"], "a/b")

  def test_exit_zero_does_not_emit(self):
    ctx = bq.ScanRunContext(scan_id="s1")
    exporter = bq.BigQueryTelemetryExporter(client=self.client)
    with patch.object(bq, "BigQueryTelemetryExporter", return_value=exporter):
      with self.assertRaises(SystemExit):
        with bq.telemetry_run_guard(ctx):
          raise SystemExit(0)
    self.client.insert_rows_json.assert_not_called()

  def test_unhandled_exception_emits_failed_row_and_reraises(self):
    ctx = bq.ScanRunContext(scan_id="s1")
    exporter = bq.BigQueryTelemetryExporter(client=self.client)
    with patch.object(bq, "BigQueryTelemetryExporter", return_value=exporter):
      with self.assertRaises(ValueError):
        with bq.telemetry_run_guard(ctx):
          raise ValueError("kaboom")
    rows = self._rows()
    self.assertEqual(rows[0]["status"], "FAILED")
    self.assertIn("kaboom", rows[0]["failure_reason"])

  def test_guard_does_not_double_emit_after_success_row(self):
    """A path that emitted its own SUCCESS row then exits 1 must not add a row."""
    ctx = bq.ScanRunContext(scan_id="s1")
    exporter = bq.BigQueryTelemetryExporter(client=self.client)
    with patch.object(bq, "BigQueryTelemetryExporter", return_value=exporter):
      with self.assertRaises(SystemExit):
        with bq.telemetry_run_guard(ctx):
          bq.emit_scan_telemetry(ctx, status=bq.STATUS_SUCCESS, exporter=exporter)
          raise SystemExit(1)
    rows = self._rows()
    self.assertEqual(len(rows), 1)
    self.assertEqual(rows[0]["status"], "SUCCESS")

  def test_clean_completion_emits_nothing(self):
    ctx = bq.ScanRunContext(scan_id="s1")
    exporter = bq.BigQueryTelemetryExporter(client=self.client)
    with patch.object(bq, "BigQueryTelemetryExporter", return_value=exporter):
      with bq.telemetry_run_guard(ctx):
        pass
    self.client.insert_rows_json.assert_not_called()

  def test_guard_is_inert_when_telemetry_disabled(self):
    with patch.dict(os.environ, {}, clear=True):
      ctx = bq.ScanRunContext(scan_id="s1")
      with self.assertRaises(SystemExit):
        with bq.telemetry_run_guard(ctx):
          raise SystemExit(1)
    self.client.insert_rows_json.assert_not_called()


class TestConfigResolution(unittest.TestCase):

  def test_project_falls_back_to_ambient_gcp_project(self):
    with patch.dict(os.environ, {"GOOGLE_CLOUD_PROJECT": "ambient"}, clear=True):
      self.assertEqual(bq.resolve_project(), "ambient")

  def test_explicit_project_wins(self):
    with patch.dict(
        os.environ,
        {bq.ENV_PROJECT: "explicit", "GOOGLE_CLOUD_PROJECT": "ambient"},
        clear=True,
    ):
      self.assertEqual(bq.resolve_project(), "explicit")

  def test_table_id_omits_project_when_unknown(self):
    with patch.dict(os.environ, {bq.ENV_DATASET: "ds"}, clear=True):
      exporter = bq.BigQueryTelemetryExporter()
      self.assertEqual(exporter.table_id("scan_runs"), "ds.scan_runs")


class TestOrchestratorConfigWiring(unittest.TestCase):
  """The telemetry settings must be reachable from the central config object."""

  def test_defaults_are_off(self):
    from codemender_agent.config import OrchestratorConfig
    with patch.dict(os.environ, {}, clear=True):
      cfg = OrchestratorConfig.from_env()
    self.assertIsNone(cfg.bq_dataset)
    self.assertFalse(cfg.bq_include_snippets)

  def test_parses_env(self):
    from codemender_agent.config import OrchestratorConfig
    with patch.dict(
        os.environ,
        {"CODEMENDER_BQ_DATASET": "codemender_telemetry",
         "CODEMENDER_BQ_INCLUDE_SNIPPETS": "true",
         "GOOGLE_CLOUD_PROJECT": "p1"},
        clear=True,
    ):
      cfg = OrchestratorConfig.from_env()
    self.assertEqual(cfg.bq_dataset, "codemender_telemetry")
    self.assertTrue(cfg.bq_include_snippets)
    self.assertEqual(cfg.bq_project, "p1")

  def test_blank_dataset_normalizes_to_none(self):
    from codemender_agent.config import OrchestratorConfig
    with patch.dict(os.environ, {"CODEMENDER_BQ_DATASET": "  "}, clear=True):
      cfg = OrchestratorConfig.from_env()
    self.assertIsNone(cfg.bq_dataset)


class TestRunnerWiring(unittest.TestCase):
  """Proves the guard is actually attached to the real runner entry points.

  The unit tests above prove `telemetry_run_guard` behaves correctly in
  isolation; these prove it was not forgotten at the call site. Both pipelines
  are driven to their first `sys.exit(1)` by omitting required GCS config.
  """

  def setUp(self):
    self.tmp = tempfile.TemporaryDirectory()
    self.client = MagicMock()
    self.client.insert_rows_json.return_value = []
    self.exporter = bq.BigQueryTelemetryExporter(
        dataset="ds", project="p", client=self.client
    )

  def tearDown(self):
    self.tmp.cleanup()

  def _emitted_rows(self):
    return [c.args[1][0] for c in self.client.insert_rows_json.call_args_list]

  def test_scan_pipeline_failure_emits_failed_row(self):
    from codemender_agent.runners.scan import run_scan_pipeline

    env = {
        "WORKSPACE_DIR": self.tmp.name,
        "HOME": self.tmp.name,
        bq.ENV_DATASET: "ds",
        "CODEMENDER_STORAGE_MODE": "gcs",
        # CODEMENDER_SCAN_ID / _GCS_BUCKET deliberately omitted -> exit(1).
    }
    with patch.dict(os.environ, env, clear=True):
      with patch.object(bq, "BigQueryTelemetryExporter", return_value=self.exporter):
        with self.assertRaises(SystemExit) as cm:
          run_scan_pipeline()
    self.assertEqual(cm.exception.code, 1)
    rows = self._emitted_rows()
    self.assertEqual(len(rows), 1)
    self.assertEqual(rows[0]["status"], "FAILED")
    self.assertEqual(rows[0]["stage"], "scan")

  def test_aggregate_pipeline_failure_emits_failed_row(self):
    from codemender_agent.runners.aggregate import run_aggregate_pipeline

    env = {
        "WORKSPACE_DIR": self.tmp.name,
        "HOME": self.tmp.name,
        bq.ENV_DATASET: "ds",
        "CODEMENDER_STORAGE_MODE": "gcs",
    }
    with patch.dict(os.environ, env, clear=True):
      with patch.object(bq, "BigQueryTelemetryExporter", return_value=self.exporter):
        with self.assertRaises(SystemExit) as cm:
          run_aggregate_pipeline()
    self.assertEqual(cm.exception.code, 1)
    rows = self._emitted_rows()
    self.assertEqual(len(rows), 1)
    self.assertEqual(rows[0]["status"], "FAILED")
    self.assertEqual(rows[0]["stage"], "aggregate")

  def test_pipelines_emit_nothing_when_telemetry_unconfigured(self):
    """The same failure with no dataset configured must touch BigQuery zero times."""
    from codemender_agent.runners.aggregate import run_aggregate_pipeline

    env = {
        "WORKSPACE_DIR": self.tmp.name,
        "HOME": self.tmp.name,
        "CODEMENDER_STORAGE_MODE": "gcs",
    }
    with patch.dict(os.environ, env, clear=True):
      with self.assertRaises(SystemExit):
        run_aggregate_pipeline()
    self.client.insert_rows_json.assert_not_called()


class TestStreamingInsertLimits(unittest.TestCase):
  """A large scan must not lose its entire findings export to request limits.

  BigQuery's insertAll caps a request at 50,000 rows and 10 MB, and rejects an
  over-limit request wholesale. Telemetry is also the very last thing a scan
  does, so an unbounded request would stall the scan itself.
  """

  def setUp(self):
    self.client = MagicMock()
    self.client.insert_rows_json.return_value = []
    self.exporter = bq.BigQueryTelemetryExporter(
        dataset="ds", project="p", client=self.client
    )

  def test_row_count_is_chunked(self):
    rows = [{"finding_id": f"f{i}"} for i in range(bq.MAX_ROWS_PER_REQUEST * 2 + 7)]
    self.assertTrue(self.exporter.insert_rows("vulnerability_findings", rows))
    calls = self.client.insert_rows_json.call_args_list
    self.assertEqual(len(calls), 3)
    self.assertEqual(
        sum(len(c.args[1]) for c in calls), len(rows), "no rows may be dropped"
    )
    for call in calls:
      self.assertLessEqual(len(call.args[1]), bq.MAX_ROWS_PER_REQUEST)

  def test_payload_bytes_are_chunked(self):
    # ~1 MB per row, so the 9 MB budget must split these across requests.
    rows = [{"finding_id": f"f{i}", "snippet": "x" * (1024 * 1024)}
            for i in range(12)]
    self.exporter.insert_rows("vulnerability_findings", rows)
    calls = self.client.insert_rows_json.call_args_list
    self.assertGreater(len(calls), 1, "9 MB budget should have forced a split")
    self.assertEqual(sum(len(c.args[1]) for c in calls), len(rows))

  def test_single_oversized_row_is_still_attempted(self):
    rows = [{"finding_id": "f1", "snippet": "x" * (bq.MAX_REQUEST_BYTES + 1024)}]
    self.exporter.insert_rows("vulnerability_findings", rows)
    self.assertEqual(self.client.insert_rows_json.call_count, 1)

  def test_insert_is_time_bounded(self):
    """An unbounded insert would hang the scan on a wedged BigQuery endpoint."""
    self.exporter.insert_rows("scan_runs", [{"scan_id": "s1"}])
    kwargs = self.client.insert_rows_json.call_args.kwargs
    self.assertIn("timeout", kwargs)
    self.assertIsNotNone(kwargs["timeout"])
    self.assertLessEqual(kwargs["timeout"], 120)

  def test_insert_tolerates_schema_drift(self):
    """One bad or extra column must not reject every other row in the batch."""
    self.exporter.insert_rows("vulnerability_findings", [{"finding_id": "f1"}])
    kwargs = self.client.insert_rows_json.call_args.kwargs
    self.assertTrue(kwargs["skip_invalid_rows"])
    self.assertTrue(kwargs["ignore_unknown_values"])

  def test_partial_rejection_is_reported_but_not_raised(self):
    self.client.insert_rows_json.return_value = [{"index": 0, "errors": ["x"]}]
    self.assertFalse(
        self.exporter.insert_rows("vulnerability_findings", [{"finding_id": "f"}])
    )


class TestGuardExportsStashedFindings(unittest.TestCase):
  """A failure after findings were captured must not discard them.

  The aggregate stage snapshots findings before its cleanup DELETE, but can
  still die afterwards in report generation or upload. Those findings are
  fully valid data the scan already paid to produce.
  """

  def setUp(self):
    self.client = MagicMock()
    self.client.insert_rows_json.return_value = []
    self.exporter = bq.BigQueryTelemetryExporter(
        dataset="ds", project="p", client=self.client
    )
    self.env = patch.dict(os.environ, {bq.ENV_DATASET: "ds"}, clear=True)
    self.env.start()
    self.patcher = patch.object(
        bq, "BigQueryTelemetryExporter", return_value=self.exporter
    )
    self.patcher.start()

  def tearDown(self):
    self.patcher.stop()
    self.env.stop()

  def _rows_for(self, table_suffix):
    for call in self.client.insert_rows_json.call_args_list:
      if call.args[0].endswith(table_suffix):
        return call.args[1]
    return []

  def test_late_exit_still_exports_findings(self):
    ctx = bq.ScanRunContext(stage="aggregate", scan_id="s1", repository="a/b")
    ctx.pending_findings = [
        {"finding_id": "f1", "status": "FIXED", "severity": "HIGH"},
        {"finding_id": "f2", "status": "DETECTED", "severity": "LOW"},
    ]
    ctx.pending_finding_prs = {"f1": "https://example.test/pr/1"}

    with self.assertRaises(SystemExit):
      with bq.telemetry_run_guard(ctx):
        raise SystemExit(1)

    run_rows = self._rows_for("scan_runs")
    self.assertEqual(run_rows[0]["status"], "FAILED")
    finding_rows = self._rows_for("vulnerability_findings")
    self.assertEqual(
        {r["finding_id"] for r in finding_rows},
        {"f1", "f2"},
        "findings captured before the failure must still be exported",
    )
    self.assertEqual(finding_rows[0]["fix_pr_url"], "https://example.test/pr/1")

  def test_no_stash_means_run_row_only(self):
    ctx = bq.ScanRunContext(stage="scan", scan_id="s1")
    with self.assertRaises(SystemExit):
      with bq.telemetry_run_guard(ctx):
        raise SystemExit(1)
    self.assertEqual(self._rows_for("vulnerability_findings"), [])
    self.assertEqual(len(self._rows_for("scan_runs")), 1)

  def test_aggregate_stashes_snapshot_on_context(self):
    """The aggregate runner must actually populate the stash, not just support it."""
    import inspect

    from codemender_agent.runners import aggregate

    source = inspect.getsource(aggregate._run_aggregate_pipeline)
    self.assertIn("ctx.pending_findings", source)
    self.assertIn("ctx.pending_finding_prs", source)


if __name__ == "__main__":
  unittest.main()

