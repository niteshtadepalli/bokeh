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

# ---------------------------------------------------------------------------
# BigQuery analytics warehouse for CodeMender scan telemetry.
#
# Why this exists: the reports bucket in storage.tf carries a bucket-wide
# 90-day delete lifecycle rule with no prefix condition, so every report.html,
# report.json, and state.db is destroyed after 90 days. Without this dataset
# there is no durable record that a scan ever ran. BigQuery gives telemetry a
# lifetime independent of the artifact retention window.
#
# Native tables (rather than GCS-backed external/BigLake tables) are used
# deliberately: external tables reading from that same bucket would be
# silently emptied by the lifecycle rule, and they cannot carry partitioning,
# clustering, or the per-column descriptions that Gemini Conversational
# Analytics relies on.
#
# NOTE ON COLUMN DESCRIPTIONS: the `description` on every field below is a
# functional requirement, not documentation. Gemini in BigQuery
# (Conversational Analytics / Data Canvas) uses these strings to ground
# natural-language queries. Removing or weakening them degrades answer
# quality, particularly for the columns with non-obvious semantics
# (report_uri, duration_seconds, skipped_duplicate_count).
#
# SCHEMA EVOLUTION: treat this schema as ADDITIVE-ONLY. The upstream source is
# a `state.db` owned by the external `cm` binary, whose schema this repository
# does not control. The exporter reads via an explicit column allowlist and
# tolerates missing columns, so adding fields here is safe while removing or
# retyping them is not.
# ---------------------------------------------------------------------------

resource "google_bigquery_dataset" "telemetry" {
  count = var.enable_bigquery_telemetry ? 1 : 0

  dataset_id    = var.bigquery_dataset_id
  project       = var.project_id
  location      = var.bigquery_location != "" ? var.bigquery_location : var.region
  friendly_name = "CodeMender Security Telemetry (${var.resource_prefix})"
  description   = <<-EOT
    Cross-repository CodeMender security scan telemetry. Contains one row per
    scan execution in `scan_runs` and one row per detected vulnerability in
    `vulnerability_findings`. Populated by the CodeMender orchestrator at the
    end of every scan. This dataset is the only durable record of scan
    history: the underlying GCS report artifacts are deleted after 90 days.
  EOT

  # Telemetry is the system of record for scan history, so it must survive a
  # `terraform destroy` of the compute stack unless explicitly overridden.
  delete_contents_on_destroy = var.bigquery_delete_contents_on_destroy

  depends_on = [google_project_service.enabled_services["bigquery.googleapis.com"]]
}

resource "google_bigquery_table" "scan_runs" {
  count = var.enable_bigquery_telemetry ? 1 : 0

  dataset_id          = google_bigquery_dataset.telemetry[0].dataset_id
  table_id            = "scan_runs"
  project             = var.project_id
  deletion_protection = var.bigquery_deletion_protection

  description = <<-EOT
    One row per CodeMender scan execution, including runs that found nothing
    and runs that failed. Use this table to answer questions about scan
    coverage, failure rates, remediation throughput, and LLM token spend
    across repositories and over time. Join to `vulnerability_findings` on
    `scan_id`.
  EOT

  time_partitioning {
    type  = "DAY"
    field = "scan_timestamp"
  }

  # Chosen for the dominant query shape: "posture for repository X over time"
  # and "which runs failed".
  clustering = ["repository", "status"]

  schema = jsonencode([
    {
      name        = "scan_id"
      type        = "STRING"
      mode        = "REQUIRED"
      description = "Unique identifier for this scan execution. Join key to vulnerability_findings.scan_id. Normally one row per scan_id."
    },
    {
      name        = "scan_timestamp"
      type        = "TIMESTAMP"
      mode        = "REQUIRED"
      description = "UTC time the telemetry row was emitted, which is effectively when the scan finished. This column is the partitioning key; always filter on it for efficient queries."
    },
    {
      name        = "repository"
      type        = "STRING"
      mode        = "NULLABLE"
      description = "Repository scanned, as 'owner/name' (for example 'acme/payments-api'). Use this to group or compare results across repositories."
    },
    {
      name        = "target_branch"
      type        = "STRING"
      mode        = "NULLABLE"
      description = "Git branch that was scanned. NULL when the scan ran against the repository default branch without an explicit branch override."
    },
    {
      name        = "target_sha"
      type        = "STRING"
      mode        = "NULLABLE"
      description = "Full Git commit SHA that was scanned. Identifies the exact code state the findings refer to."
    },
    {
      name        = "scan_target"
      type        = "STRING"
      mode        = "NULLABLE"
      description = "Path or subdirectory within the repository that was scanned. '.' means the whole repository."
    },
    {
      name        = "stage"
      type        = "STRING"
      mode        = "NULLABLE"
      description = "Pipeline stage that emitted this row: 'scan' for clean or early-failing runs, 'aggregate' for runs that produced findings, 'sequential' for single-task runs. This is an internal provenance field and is usually not interesting to end users."
    },
    {
      name        = "status"
      type        = "STRING"
      mode        = "REQUIRED"
      description = "Outcome of the scan: 'SUCCESS' if the scan completed (including finding zero vulnerabilities), or 'FAILED' if it errored out. A FAILED run means the repository was NOT successfully scanned and its security posture is unknown for that night."
    },
    {
      name        = "failure_reason"
      type        = "STRING"
      mode        = "NULLABLE"
      description = "Short description of why the scan failed. Always NULL when status is 'SUCCESS'."
    },
    {
      name        = "duration_seconds"
      type        = "FLOAT"
      mode        = "NULLABLE"
      description = "End-to-end wall-clock duration of the scan in seconds, measured from the start of stage 1. For runs that ended early (clean repository, or an error) this is the duration up to that point, not a full scan."
    },
    {
      name        = "cm_version"
      type        = "STRING"
      mode        = "NULLABLE"
      description = "Version of the CodeMender 'cm' scanner binary used, stored as the bare version number (e.g. '0.9.0') so releases group cleanly. Useful for correlating shifts in finding counts against scanner upgrades. Falls back to the binary's raw version banner if no version number could be parsed from it."
    },
    {
      name        = "find_model"
      type        = "STRING"
      mode        = "NULLABLE"
      description = "LLM model actually used for the vulnerability discovery (find) phase: the explicitly configured model when one was set, otherwise the scanner's built-in default detected at run time. Lines up with token_totals.model for the same run; if the default could not be detected, the same placeholder used for token accounting is recorded here rather than a real model name."
    },
    {
      name        = "verify_model"
      type        = "STRING"
      mode        = "NULLABLE"
      description = "LLM model actually used for the exploit verification phase: the explicitly configured model when one was set, otherwise the scanner's built-in default detected at run time. Lines up with token_totals.model for the same run; if the default could not be detected, the same placeholder used for token accounting is recorded here rather than a real model name."
    },
    {
      name        = "fix_model"
      type        = "STRING"
      mode        = "NULLABLE"
      description = "LLM model actually used for the patch generation (fix) phase: the explicitly configured model when one was set, otherwise the scanner's built-in default detected at run time. Lines up with token_totals.model for the same run; if the default could not be detected, the same placeholder used for token accounting is recorded here rather than a real model name."
    },
    {
      name        = "total_findings_count"
      type        = "INTEGER"
      mode        = "NULLABLE"
      description = "Total number of vulnerabilities the scanner detected before any filtering, including duplicates and pre-existing findings."
    },
    {
      name        = "active_findings_count"
      type        = "INTEGER"
      mode        = "NULLABLE"
      description = "Number of actionable vulnerabilities remaining after filtering out duplicates and pre-existing findings. This is the number that actually required attention from this run."
    },
    {
      name        = "skipped_duplicate_count"
      type        = "INTEGER"
      mode        = "NULLABLE"
      description = "Number of findings that were skipped because an open pull request or remediation branch already exists for them, or because they pre-date the change under review. These are real vulnerabilities that are already being tracked elsewhere, not false positives."
    },
    {
      name        = "fixed_count"
      type        = "INTEGER"
      mode        = "NULLABLE"
      description = "Number of findings this run successfully remediated with a generated patch. Divide by active_findings_count for the auto-fix rate."
    },
    {
      name        = "failed_fix_count"
      type        = "INTEGER"
      mode        = "NULLABLE"
      description = "Number of findings where patch generation or pull request creation was attempted but failed. These need manual remediation."
    },
    {
      name        = "report_uri"
      type        = "STRING"
      mode        = "NULLABLE"
      description = "Cloud Storage URI (gs://...) of the generated HTML report. IMPORTANT: this is NULL for clean runs, because no report is produced when there are zero findings, and the object is permanently deleted 90 days after the scan by the reports bucket lifecycle rule, after which this URI is a dead link. Do not treat a NULL or dead report_uri as evidence that a scan did not happen."
    },
    {
      name        = "execution_url"
      type        = "STRING"
      mode        = "NULLABLE"
      description = "Link to the orchestration execution (for example the Cloud Workflows execution page) for this scan, for operational debugging."
    },
    {
      name        = "token_totals"
      type        = "RECORD"
      mode        = "REPEATED"
      description = "LLM token consumption for this scan, broken down per model. One entry per model used. UNNEST this to attribute cost by model."
      fields = [
        {
          name        = "model"
          type        = "STRING"
          mode        = "NULLABLE"
          description = "Name of the LLM model these token counts apply to."
        },
        {
          name        = "in_tokens"
          type        = "INTEGER"
          mode        = "NULLABLE"
          description = "Input (prompt) tokens consumed by this model during the scan."
        },
        {
          name        = "out_tokens"
          type        = "INTEGER"
          mode        = "NULLABLE"
          description = "Output (completion) tokens produced by this model during the scan."
        },
        {
          name        = "total_tokens"
          type        = "INTEGER"
          mode        = "NULLABLE"
          description = "Total tokens (input plus output) for this model during the scan."
        },
      ]
    },
  ])
}

resource "google_bigquery_table" "vulnerability_findings" {
  count = var.enable_bigquery_telemetry ? 1 : 0

  dataset_id          = google_bigquery_dataset.telemetry[0].dataset_id
  table_id            = "vulnerability_findings"
  project             = var.project_id
  deletion_protection = var.bigquery_deletion_protection

  description = <<-EOT
    One row per vulnerability detected by a CodeMender scan. Use this table to
    answer questions about vulnerability types, severities, affected files, and
    remediation outcomes. Join to `scan_runs` on `scan_id` to bring in run-level
    context such as the commit SHA or the branch.
  EOT

  time_partitioning {
    type  = "DAY"
    field = "scan_timestamp"
  }

  # Chosen for the dominant query shape: "HIGH severity SQL injection findings
  # in repository X".
  clustering = ["repository", "severity", "vuln_type"]

  schema = jsonencode(concat([
    {
      name        = "finding_id"
      type        = "STRING"
      mode        = "REQUIRED"
      description = "Identifier for this vulnerability finding, unique within a scan."
    },
    {
      name        = "scan_id"
      type        = "STRING"
      mode        = "REQUIRED"
      description = "Identifier of the scan that produced this finding. Join key to scan_runs.scan_id."
    },
    {
      name        = "scan_timestamp"
      type        = "TIMESTAMP"
      mode        = "REQUIRED"
      description = "UTC time of the scan that produced this finding. This column is the partitioning key; always filter on it for efficient queries."
    },
    {
      name        = "repository"
      type        = "STRING"
      mode        = "NULLABLE"
      description = "Repository the finding was detected in, as 'owner/name'."
    },
    {
      name        = "title"
      type        = "STRING"
      mode        = "NULLABLE"
      description = "Short human-readable summary of the vulnerability."
    },
    {
      name        = "vuln_type"
      type        = "STRING"
      mode        = "NULLABLE"
      description = "Category of vulnerability, for example 'SQL Injection', 'Path Traversal', or 'Cross-Site Scripting'."
    },
    {
      name        = "cwe_id"
      type        = "STRING"
      mode        = "NULLABLE"
      description = "Common Weakness Enumeration identifier in 'CWE-nnn' form, when one could be determined. NULL when the scanner did not associate the finding with a CWE."
    },
    {
      name        = "severity"
      type        = "STRING"
      mode        = "NULLABLE"
      description = "Severity of the vulnerability, one of CRITICAL, HIGH, MEDIUM, LOW, or INFO. CRITICAL and HIGH are the findings that normally warrant immediate action."
    },
    {
      name        = "confidence_level"
      type        = "STRING"
      mode        = "NULLABLE"
      description = "How confident the scanner is that this is a genuine vulnerability rather than a false positive, typically HIGH, MEDIUM, or LOW."
    },
    {
      name        = "file_path"
      type        = "STRING"
      mode        = "NULLABLE"
      description = "Repository-relative path of the source file containing the vulnerability."
    },
    {
      name        = "start_line"
      type        = "INTEGER"
      mode        = "NULLABLE"
      description = "First line number of the vulnerable code region."
    },
    {
      name        = "end_line"
      type        = "INTEGER"
      mode        = "NULLABLE"
      description = "Last line number of the vulnerable code region."
    },
    {
      name        = "status"
      type        = "STRING"
      mode        = "NULLABLE"
      description = "Lifecycle state of the finding at the end of the scan. 'FIXED' or 'REMEDIATED' means a patch was generated. 'SKIPPED_DUPLICATE' means an open pull request or branch already addresses it. 'PRE_EXISTING_IGNORED' means it was not introduced by the change under review. 'DETECTED' means it was found but not remediated."
    },
    {
      name        = "source_stage"
      type        = "STRING"
      mode        = "NULLABLE"
      description = "Which scanner phase produced this finding (for example the discovery or verification phase). Internal provenance field."
    },
    {
      name        = "verified"
      type        = "BOOLEAN"
      mode        = "NULLABLE"
      description = "TRUE when the scanner confirmed the vulnerability is genuinely exploitable via exploit verification. Verified findings are the highest-confidence subset."
    },
    {
      name        = "muted"
      type        = "BOOLEAN"
      mode        = "NULLABLE"
      description = "TRUE when the finding was suppressed from the report. Muted findings are still real detections; see mute_reason for why they were suppressed."
    },
    {
      name        = "mute_reason"
      type        = "STRING"
      mode        = "NULLABLE"
      description = "Explanation of why the finding was muted or dismissed, for example that a duplicate pull request already exists."
    },
    {
      name        = "fingerprint"
      type        = "STRING"
      mode        = "NULLABLE"
      description = "Stable content hash of the finding. The same underlying vulnerability keeps the same fingerprint across scans, so use this (not finding_id) to track a single issue over time."
    },
    {
      name        = "fix_pr_url"
      type        = "STRING"
      mode        = "NULLABLE"
      description = "URL of the pull request that remediates this finding, when one exists."
    },
    {
      name        = "patch_status"
      type        = "STRING"
      mode        = "NULLABLE"
      description = "Outcome of the most recent patch generation attempt for this finding. NULL when no patch was attempted."
    },
    ],
    # Sensitive columns: LLM-written analysis prose and verbatim source code.
    # Provisioned only when var.bigquery_include_snippets is true, matching the
    # runtime CODEMENDER_BQ_INCLUDE_SNIPPETS gate. Default is OFF, so a
    # deployment stays metadata-only unless source replication into the
    # warehouse has been explicitly approved.
    var.bigquery_include_snippets ? [
      {
        name        = "analysis"
        type        = "STRING"
        mode        = "NULLABLE"
        description = "LLM-generated explanation of why this code is vulnerable and how it could be exploited. Free-form prose."
      },
      {
        name        = "snippet"
        type        = "STRING"
        mode        = "NULLABLE"
        description = "Verbatim excerpt of the vulnerable source code. Contains real application source."
      },
  ] : []))
}
