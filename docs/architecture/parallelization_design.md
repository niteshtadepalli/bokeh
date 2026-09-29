# Parallelization Implementation Guardrails & Source of Truth

This document serves as the absolute source of truth and guardrails for
implementing parallel processing (verification and fixing loops) in the
CodeMender Orchestrator.

--------------------------------------------------------------------------------

## 1. The Problem (Plain English)

Currently, the CodeMender Orchestrator scans a repository and fixes every
vulnerability one by one in a single, sequential loop.

To verify that a fix is safe, the orchestrator compiles the code and runs the
repository's entire test suite. For large codebases or repositories with
multiple security findings, running these compilation and test steps
sequentially results in a significant execution time bottleneck. This causes two
major operational issues:

1.  **Delayed Developer Feedback**: Software development teams have to wait
    unnecessarily long for automated security patches to be generated, reviewed,
    and proposed.
2.  **Queue Backlog**: On large repositories with dozens of findings, a
    single-threaded scan loop slows down nightly security batch pipelines,
    delaying automated remediation PRs across an engineering organization.

We need to execute the verification and fixing steps **in parallel** to reduce
the total processing time. However, we must do this without clashing on local
files, without causing conflicts on shared network test ports, without causing
inconsistencies due to non-deterministic AI scans, and **most importantly**,
without creating security loopholes that allow untrusted code executed during
tests to compromise the cloud infrastructure.

--------------------------------------------------------------------------------

## 2. The Technical Plan (Jargon-Light)

To solve the speed problem safely, we break the execution down into a
**three-stage sequential pipeline with a parallel middle layer**. We use a
shared, secure storage bucket (Google Cloud Storage or GitHub Actions Artifacts)
to coordinate state between the stages using SQLite database tarballs and
explicit partition lists.

```
[ Stage 1: SCAN & DISPATCH (Coordinator) ]
  └── Run 1 Container (Coordinator)
        ├── Clones repo, runs 'cm init' and 'cm find .' to find all vulnerabilities once
        ├── Records target Git commit SHA
        ├── Filters out findings whose remote branches or open PRs already exist (prevents duplicate PRs & idle workers)
        ├── Soft-deletes (Dismisses) the skipped findings in local state.db to preserve telemetry without scheduling workers
        ├── Tars baseline ~/.codemender/ directory (contains state.db and identity.key) -> workspace_base.tar.gz
        ├── Extracts active finding IDs and intelligently partitions them into N lists (partition_i.json)
        └── Saves workspace_base.tar.gz, partition files, and manifest.json (findings_count, target_sha) to GCS
              │
              ▼ (Triggers automatically when Scan finishes)
[ Stage 2: FIX (Parallel Shards) ]
  ├── Run N Containers in Parallel (Workers)
        ├── Each container clones repo, checkouts target_sha
        ├── Downloads & extracts workspace_base.tar.gz to ~/.codemender/ (via Signed URL)
        ├── Downloads its assigned partition list (partition_i.json)
        ├── Runs idempotency check (skips if remote branch/PR exists and finding is verified)
        ├── Each container runs 'cm fix <id>' (and optional 'cm verify <id>' if skip_verify=False) -> Delivers PR suggestions / PRs
        └── Uploads its mutated database to GCS as worker_i_state.db (via Signed URL)
              │
              ▼ (Triggers automatically when all Workers finish)
[ Stage 3: AGGREGATE ]
  └── Run 1 Container (Aggregator)
        ├── Downloads workspace_base.tar.gz and all worker_i_state.db files from GCS
        ├── Merges worker databases into base state.db via SQLite UPSERT (using updated_at timestamps)
        ├── Deletes 'DISMISSED' findings from local state.db to keep the report clean
        ├── Compiles final consolidated HTML report (cm report -f html)
        └── Uploads final report to GCS (generates temporary signed access URL)
```

### Environment Variable Contracts for Stage Dispatches

When containers run in serverless execution environments (GCP Cloud Run Jobs or
GitHub Actions), the exact same container image is used. The entrypoint script
(`orchestrator.py`) relies on environment variables to determine its execution
role and parameters:

| Variable Name | Required Stage(s) | Description |
| :--- | :--- | :--- |
| `CODEMENDER_RUN_MODE` | **All** | Execution stage mode: `scan` (Stage 1), `worker` (Stage 2), or `aggregate` (Stage 3).<br/>Default fallback: `sequential`. |
| `CODEMENDER_SCAN_ID` | **All** | Unique identifier for the scan run (e.g., `scan-20260720-203000`).<br/>Used as the GCS folder prefix: `scans/<scan_id>/`. |
| `CODEMENDER_GCS_BUCKET` | **All** | Name of the GCS bucket for state artifacts and report uploads. |
| `CODEMENDER_MAX_TASKS` | **Stage 1 (Scan)** | Configurable upper limit cap for parallel worker tasks (default: `10` or `20`). |
| `CLOUD_RUN_TASK_INDEX`<br/>*(or `CODEMENDER_WORKER_INDEX`)* | **Stage 2 (Worker)** | 0-indexed worker task number. Cloud Run Jobs automatically injects `CLOUD_RUN_TASK_INDEX`. |
| `CLOUD_RUN_TASK_COUNT`<br/>*(or `CODEMENDER_TOTAL_WORKERS`)* | **Stage 2 (Worker)** | Total parallel worker count $N$. Cloud Run Jobs automatically injects `CLOUD_RUN_TASK_COUNT`. |
| `CODEMENDER_TARGET_SHA` | **Stage 2 (Worker)** | The Git commit SHA representing the point-in-time codebase. Ensures all parallel workers branch from the exact same commit. |
| `CODEMENDER_BASE_WORKSPACE_URL` | **Stage 2** | GCP Signed URL to download `workspace_base.tar.gz`. |
| `CODEMENDER_PARTITION_URLS` | **Stage 2** | JSON-serialized array of signed download URLs for partitions (indexed by task index). |
| `CODEMENDER_UPLOAD_URLS` | **Stage 2** | JSON-serialized array of signed upload URLs for worker databases (indexed by task index). |
| `CODEMENDER_CLEANUP_PORTS` | **Worker / Seq** | Optional comma-separated list of TCP port numbers to terminate before running code verifications (defaults to common web framework ports: `3000, 3001, 5000, 8000, 8080, 8081, 9000`). |




### How the Parallel Worker Count (N) is Determined & Scaled

1.  **Dynamic Derivation & Remote Branch Filtering in Stage 1**: When Stage 1
    (Scan Phase) completes, the coordinator extracts discovered findings from
    `state.db`. Unless `CODEMENDER_FORCE_OVERWRITE=true`, it checks
    `check_remote_branch_exists()` and `is_duplicate_pr()` for each finding and
    **filters out findings whose feature branches or PRs already exist on remote**.
    The skipped findings are marked as `DISMISSED` with `muted=1` in `state.db`
    to retain telemetry. The remaining active findings count is `active_findings_count`.
2.  **Handling Cloud Run Task Limits & Quotas**: GCP Cloud Run Jobs support
    executing up to **10,000 tasks** per job run. While GCP can physically scale
    to thousands of tasks, spinning up hundreds of parallel containers
    simultaneously introduces two major external bottlenecks:
    *   **GitHub Secondary Rate Limits**: Pushing dozens of branches and opening
        PRs at the exact same second will trigger GitHub API rate-limit blocks.
    *   **LLM Backend Quota**: Calling the CodeMender backend concurrently from
        too many workers can exhaust API rate limits (HTTP 429).
3.  **The `MAX_TASKS` Cap & Partitioning Strategy**: To ensure optimal
    performance without hitting quota limits, the pipeline uses a configurable
    upper limit parameter: `MAX_TASKS` (default: `10` or `20`, configured via
    `CODEMENDER_MAX_TASKS`). The worker count `N` is calculated as:

    `N = min(active_findings_count, MAX_TASKS)`

    *   **Scenario A (Small/Medium scan)**: If 4 active findings remain, Stage 2
        triggers `N = 4` worker tasks. Each task gets a partition with **1
        finding ID**.
    *   **Scenario B (Large scan)**: If 40 active findings remain and
        `MAX_TASKS=10`, Stage 2 triggers `N = 10` worker tasks. Stage 1
        partitions finding IDs into 10 explicit partition JSON files
        (`partition_0.json` .. `partition_9.json`), so each worker processes **4
        findings** sequentially in its isolated VM container.
    *   **Scenario C (Zero active findings)**: If 0 active findings remain (all
        findings were resolved, false positives, or already have open PRs),
        Stage 2 and Stage 3 are skipped entirely.

### Core Security Guardrail

The containers running the CodeMender CLI (which compile code and run tests)
**never** hold administrative GCP credentials to trigger other containers. The
orchestration pipeline is managed entirely **externally** by a secure control
plane (Google Cloud Workflows or the GitHub Actions engine) that holds the
execution permissions.

--------------------------------------------------------------------------------

## 3. Resilience, Retries & Artifact Lifecycle

### Zero Findings & Stage 1 Retries

*   **Scan Verification**: Stage 1 retries `cm find .` up to **3 times** if 0
    findings are initially returned, ensuring transient backend network errors
    do not cause false-positive zero findings.
*   **Workflow Skipping**: If 0 findings persist after 3 retries, Stage 1 writes
    `{"findings_count": 0}` to `manifest.json` on GCS and exits with code `0`.
    The external workflow reads `manifest.json` and skips Stage 2 and Stage 3.

### Two-Tier Retry Model

1.  **Tier 1 (Finding-Level Retry inside Worker)**:
    *   Each worker retries `cm find verify <finding_id>` up to **3 times** per
        finding before marking the finding unverified and moving to the next
        finding in its partition.
    *   If a worker finishes its partition (even if some findings failed Tier 1
        verification), it uploads `worker_i_state.db` and exits with code `0`
        (Success). **Tier 2 retries will NOT be triggered for successful worker
        exits.**
2.  **Tier 2 (Task/Container-Level Retry)**:
    *   Triggered **ONLY** when a worker container encounters an abnormal status
        (e.g., container OOM crash, uncaught fatal Python exception, SIGKILL,
        infrastructure failure, or non-zero exit code).
    *   GCP Cloud Run Jobs automatically retries failed tasks
        (`--max-retries=3`).
    *   **Stage 3 Resiliency**: If a worker fails completely after all Tier 2
        retries, Stage 3 merges all available `worker_*_state.db` files from
        GCS, generates the partial summary report, and logs warnings for missing
        worker tasks.

### Code Consistency (Git SHA Pinning)

To prevent code drift during parallel execution:

*   Stage 1 records the exact Git commit SHA scanned and saves it as
    `target_sha` in `manifest.json`.
*   Stage 2 workers read `target_sha` and execute `git checkout $target_sha`
    immediately after cloning the repository. This ensures all workers operate
    on the exact same code baseline analyzed during the scan phase.

### Security & GCS Signed URLs

To mitigate the risk of untrusted code execution (tests) exploiting cloud
credentials:

*   Worker containers do not run with GCP Service Account credentials that have
    write access to the GCS bucket.
*   The external control plane (e.g., Cloud Workflows) generates short-lived GCP
    Signed URLs for:
    *   Downloading `workspace_base.tar.gz` and `partition_i.json`.
    *   Uploading `worker_i_state.db`.
*   Workers use these Signed URLs via standard HTTPS tools (like `curl`),
    keeping the execution environment completely credential-free.

### Concurrency Lock Isolation (Project ID)

The CodeMender server restricts concurrency by allowing only one active session
per `project_id`.

*   To allow parallel workers to execute concurrently without triggering
    `DIFFERENT_OWNER_CONFLICT` or `SAME_OWNER_CONFLICT` blocks on the server,
    each worker must have a unique `project_id`.
*   This is achieved by **not** transferring the `.cm_project` file (created in
    Stage 1) to Stage 2 workers.
*   When workers clone the repository fresh, they will lack `.cm_project`. The
    CLI commands (`cm fix`/`cm verify`) will automatically generate a new random
    `project_id` UUID in the workspace, ensuring clean isolation.

### Worker Idempotency & Deduplication

If a worker is retried (Tier 2), it must handle previously processed findings
gracefully:

*   Before attempting to fix a finding, the worker checks if a remote branch/PR
    already exists for it.
*   If the branch exists, the worker runs `cm verify <finding_id>` to check if
    the vulnerability is already resolved.
*   If verified as resolved, it updates the local status and skips the `cm fix`
    step to avoid redundant LLM calls and PR updates.
*   Fingerprint-based deduplication (native to `cm`) is used to ensure identical
    findings across runs are mapped to the same ID.

### GCS Intermediate Artifact Lifecycle

To prevent intermediate state tarballs and identity keys from lingering
indefinitely in storage:

*   **GCS Lifecycle Policy**: A standard GCP Object Lifecycle rule is configured
    on the bucket to automatically delete objects under the `scans/` prefix
    after **7 days**.
*   **Stage 3 Cleanup**: Stage 3 can optionally purge `scans/<scan_id>/` after
    successfully uploading the final HTML report to `reports/`.

--------------------------------------------------------------------------------

## 4. Alternatives Considered & Ruled Out

During the design phase, several alternative parallelization architectures were
evaluated and rejected:

### Ruled Out

1.  **Local Multiprocessing (Single Container Process Pool)**:
    *   *Idea*: Spawning a Python process pool to clone the repository to
        multiple temporary folders inside a single container instance and run
        fixes in parallel.
    *   *Why Ruled Out*: Heavy compilation tasks running concurrently would
        trigger Out-of-Memory (OOM) crashes on the serverless container
        instance. Additionally, tests trying to bind to the same hardcoded
        network ports (e.g. `3000`) would collide and crash.
2.  **Stateless Modulo Sharding with Worker Polling (Single Job)**:
    *   *Idea*: Booting N containers in parallel at the same time. Task 0
        performs the scan, while Tasks 1..N idle-sleep and poll GCS waiting for
        Task 0 to upload the scan state.
    *   *Why Ruled Out*: Highly inefficient and wasteful. Worker containers
        would sit idle consuming billing seconds while waiting for the scan to
        finish, resulting in unnecessary infrastructure costs.
3.  **Dynamic Job Triggering from within the Container**:
    *   *Idea*: The coordinator container runs the scan, calculates the number
        of findings, and makes a GCP API call from *inside* the container to
        trigger the parallel workers.
    *   *Why Ruled Out*: **Severe security risk**. This requires exposing write
        access tokens (permissions to run Cloud Run Jobs and override
        specifications) to the container. Because the container compiles and
        executes untrusted code from the target repository, a compromised
        dependency could fetch the token from the metadata server and trigger
        arbitrary containers, leading to privilege escalation and billing
        exploits.
4.  **SARIF Import/Export State Merging (`cm import`)**:
    *   *Idea*: Relying on `cm export` and `cm import` CLI commands to shuttle
        findings state between workers and aggregator.
    *   *Why Ruled Out*: Restoring and merging raw SQLite database tarballs
        (`workspace_base.tar.gz` and `worker_i_state.db`) provides 100%
        full-fidelity state preservation without depending on CLI format
        conversions.

--------------------------------------------------------------------------------

## 5. Detailed Implementation (Modular Package Architecture)

To implement this plan within the refactored `codemender_agent/` package
structure, we will create or modify the following files:

### 1. `orchestrator.py` (Entrypoint Dispatcher)

*   **Purpose**: Read `CODEMENDER_RUN_MODE` environment variable (`sequential`,
    `scan`, `worker`, `aggregate`) and dispatch to the corresponding runner
    module in `codemender_agent/runners/`.
*   **Detailed Changes**:
    *   `sequential`: Calls
        `codemender_agent.runners.sequential.run_sequential_pipeline()`.
    *   `scan`: Calls `codemender_agent.runners.scan.run_scan_pipeline()`.
    *   `worker`: Calls `codemender_agent.runners.worker.run_worker_pipeline()`.
    *   `aggregate`: Calls
        `codemender_agent.runners.aggregate.run_aggregate_pipeline()`.

### 2. `codemender_agent/runners/` Subpackage

*   **`sequential.py`** (Existing): Sequential single-loop execution runner for
    local runs and simple Cloud Run Jobs.
*   **`scan.py`** (New File): Stage 1 Coordinator runner.
    *   Clones repo (`git clone --depth 1`) and initializes CodeMender (`cm
        init`, `cm find .` with retries).
    *   Records the target Git commit SHA.
    *   Checks `check_remote_branch_exists()` and `is_duplicate_pr()` for each finding and
        **filters out findings whose feature branches or PRs already exist on remote** (unless
        `CODEMENDER_FORCE_OVERWRITE=true`).
    *   Mutates the filtered findings in `state.db` to `status='DISMISSED'`, `muted=1` for telemetry retention.
    *   Creates `workspace_base.tar.gz` from `~/.codemender/`.
    *   Extracts remaining active finding IDs, partitions them into $N$
        `partition_i.json` files using a round-robin distribution (ensuring
        even load and exactly $N$ partitions), and writes `manifest.json`
        (`active_findings_count`, `target_sha`).
    *   Uploads tarball, partition lists, and manifest to GCS under
        `scans/[scan_id]/`.
*   **`worker.py`** (New File): Stage 2 Parallel Worker runner.
    *   Clones target repository (`git clone`) and checkouts `target_sha` (read
        from `manifest.json`).
    *   Downloads & extracts `workspace_base.tar.gz` to `~/.codemender/` (via
        GCS Signed URL) to restore baseline state and `identity.key`.
    *   Downloads assigned partition list `partition_[worker_index].json` (via
        GCS Signed URL).
    *   For each assigned finding, checks if a remote branch/PR already exists.
        If so, runs `cm verify` to see if it is resolved. If already resolved,
        skips `cm fix` (idempotency).
    *   Executes `cm fix` (and `cm verify` if `skip_verify=False`) for
        unresolved findings; delivers PR review suggestions or pushes feature
        branches & PRs.
    *   Uploads mutated state database to
        `scans/[scan_id]/worker_[worker_index]_state.db` on GCS (via GCS Signed
        URL).
*   **`aggregate.py`** (New File): Stage 3 Aggregator runner.
    *   Downloads `workspace_base.tar.gz` and all available `worker_*_state.db`
        files from GCS.
    *   Merges worker database tables into base `state.db` using selective SQLite
        UPSERT queries to prevent lost updates:
        *   **Findings Merge**:
            ```sql
            INSERT INTO main.findings (
                finding_id, session_id, title, file_path, severity, confidence, analysis, snippet, vuln_type, vuln_id,
                verified, muted, mute_reason, created_at, fingerprint, status, source_stage, finding_json, updated_at,
                start_line, end_line, dismiss_reason, confidence_level
            )
            SELECT 
                finding_id, session_id, title, file_path, severity, confidence, analysis, snippet, vuln_type, vuln_id,
                verified, muted, mute_reason, created_at, fingerprint, status, source_stage, finding_json, updated_at,
                start_line, end_line, dismiss_reason, confidence_level
            FROM worker.findings
            ON CONFLICT(finding_id) DO UPDATE SET
                session_id = excluded.session_id,
                title = excluded.title,
                file_path = excluded.file_path,
                severity = excluded.severity,
                confidence = excluded.confidence,
                analysis = excluded.analysis,
                snippet = excluded.snippet,
                vuln_type = excluded.vuln_type,
                vuln_id = excluded.vuln_id,
                verified = excluded.verified,
                muted = excluded.muted,
                mute_reason = excluded.mute_reason,
                status = excluded.status,
                source_stage = excluded.source_stage,
                finding_json = excluded.finding_json,
                updated_at = excluded.updated_at,
                start_line = excluded.start_line,
                end_line = excluded.end_line,
                dismiss_reason = excluded.dismiss_reason,
                confidence_level = excluded.confidence_level
            WHERE excluded.updated_at > main.findings.updated_at OR main.findings.updated_at = '' OR main.findings.updated_at IS NULL;
            ```
        *   **Sessions Merge**:
            ```sql
            INSERT INTO main.sessions (session_id, operation_name, session_type, status, pipeline_mode, target, created_at, updated_at, project_root)
            SELECT session_id, operation_name, session_type, status, pipeline_mode, target, created_at, updated_at, project_root
            FROM worker.sessions
            ON CONFLICT(session_id) DO UPDATE SET
                status = excluded.status,
                updated_at = excluded.updated_at
            WHERE excluded.updated_at > main.sessions.updated_at;
            ```
        *   **Artifacts Merge** (prevents duplicate key conflicts on autoincrement ID and filters ghost findings):
            ```sql
            INSERT INTO main.artifacts (session_id, filename, original_path, purpose, finding_id, created_at)
            SELECT session_id, filename, original_path, purpose, finding_id, created_at
            FROM worker.artifacts AS w
            WHERE (w.finding_id IS NULL OR EXISTS (
                SELECT 1 FROM main.findings AS m
                WHERE m.finding_id = w.finding_id
            )) AND NOT EXISTS (
                SELECT 1 FROM main.artifacts AS m
                WHERE m.session_id = w.session_id AND m.filename = w.filename
            );
            ```
        *   **Patches Merge** (merges fix diffs and reasoning from worker nodes, ignoring ghost patches):
            ```sql
            INSERT INTO main.patches (
                patch_id, finding_id, session_id, diff, reasoning, status, backup_path,
                target_file, edited_files, validation_result, created_at
            )
            SELECT 
                patch_id, finding_id, session_id, diff, reasoning, status, backup_path,
                target_file, edited_files, validation_result, created_at
            FROM worker.patches AS w
            WHERE EXISTS (
                SELECT 1 FROM main.findings AS m
                WHERE m.finding_id = w.finding_id
            )
            ON CONFLICT(patch_id) DO UPDATE SET
                finding_id = excluded.finding_id,
                session_id = excluded.session_id,
                diff = excluded.diff,
                reasoning = excluded.reasoning,
                status = excluded.status,
                backup_path = excluded.backup_path,
                target_file = excluded.target_file,
                edited_files = excluded.edited_files,
                validation_result = excluded.validation_result,
                created_at = excluded.created_at;
            ```

    *   Deletes all `DISMISSED` findings from the local `state.db` to ensure a clean HTML output.
    *   Generates final consolidated HTML report (`cm report -f html`) and
        uploads to GCS with signed access URL.


### 3. `codemender_agent/storage.py` (Updated)

*   **Purpose**: Add helper methods to handle uploading/downloading tarballs and
    database shards to/from GCS.
*   **Functions**: `upload_file_to_gcs()`, `download_file_from_gcs()`,
    `list_gcs_blobs()`.

### 4. `tests/` Submodule Unit Tests

*   **`tests/test_runners_scan.py`**: Unit tests for Stage 1 tarball creation,
    ID partitioning, and GCS manifest uploads.
*   **`tests/test_runners_worker.py`**: Unit tests for workspace extraction,
    partition file reading, and finding fixes.
*   **`tests/test_runners_aggregate.py`**: Unit tests for SQLite database
    merging (`INSERT OR REPLACE`) and report compilation.

### 5. `docs/guides/production_run.md` (Updated)

*   **Purpose**: Update deployment documentation for parallel workflow
    orchestration.
*   **Detailed Changes**:
    *   Document deploying Google Cloud Workflows
        (`gcp_parallel_workflow.yaml`).
    *   (DEFERRED) Document GitHub Actions matrix workflow (`gha_parallel_workflow.yaml`).
    *   Detail required IAM roles (`roles/workflows.invoker`,
        `roles/run.developer`).

### 6. Workflow Configuration Templates (New Files)

*   **`gcp_parallel_workflow.yaml`**: Cloud Workflows definition managing Stage
    1 → Stage 2 (N parallel tasks) → Stage 3 on GCP.
*   **`gha_parallel_workflow.yaml`** (DEFERRED): GitHub Actions workflow template managing
    parallel matrix builds with job artifacts (moved to Future Work).

### 7. Intermediate File Schemas (New JSON Specs)

To ensure interoperability between runners, the following JSON schemas are defined for files stored in the GCS transit directory:

#### A. `manifest.json`
Located at `scans/[scan_id]/manifest.json`. Records metadata about the scan run.

```json
{
  "$schema": "http://json-schema.org/draft-07/schema#",
  "title": "CodeMenderScanManifest",
  "type": "object",
  "properties": {
    "findings_count": {
      "type": "integer",
      "description": "Total number of active findings after remote branch filtering."
    },
    "target_sha": {
      "type": "string",
      "description": "The exact Git commit SHA scanned in Stage 1."
    },
    "base_workspace_url": {
      "type": "string",
      "format": "uri",
      "description": "GCS Signed URL to download workspace_base.tar.gz."
    },
    "partition_urls": {
      "type": "array",
      "items": { "type": "string", "format": "uri" },
      "description": "List of GCS Signed URLs for partition JSON files."
    },
    "upload_urls": {
      "type": "array",
      "items": { "type": "string", "format": "uri" },
      "description": "List of GCS Signed URLs for uploading worker DB shards."
    }
  },
  "required": ["findings_count", "target_sha", "base_workspace_url", "partition_urls", "upload_urls"]
}
```

#### B. `partition_i.json`
Located at `scans/[scan_id]/partition_[index].json`. Defines the work unit for worker `index`.

```json
{
  "$schema": "http://json-schema.org/draft-07/schema#",
  "title": "CodeMenderPartition",
  "type": "object",
  "properties": {
    "partition_index": {
      "type": "integer",
      "description": "The 0-based index of this partition."
    },
    "finding_ids": {
      "type": "array",
      "items": {
        "type": "string",
        "format": "uuid"
      },
      "description": "List of finding UUIDs assigned to this worker task."
    }
  },
  "required": ["partition_index", "finding_ids"]
}
```

--------------------------------------------------------------------------------

## 6. Future Work

### 6.1. GitHub Actions Dynamic Matrix Support
*   **Goal**: Enable native parallel execution in GitHub Actions using a dynamic matrix.
*   **Mechanism**:
    *   Stage 1 (Coordinator) will output a JSON array of partition indices (e.g., `[0, 1, 2]`) to GHA Runner outputs (e.g., `echo "matrix=[0,1,2]" >> $GITHUB_OUTPUT`).
    *   Stage 2 (Workers) will use this output to dynamically define its matrix:
        ```yaml
        strategy:
          matrix:
            worker_index: ${{ fromJson(needs.scan.outputs.matrix) }}
        ```
    *   This allows GHA to scale workers dynamically based on the number of findings, matching the GCP Cloud Workflows capability.

