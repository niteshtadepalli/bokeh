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
        ├── Filters out findings whose remote branches already exist (prevents duplicate PRs & idle workers)
        ├── Tars baseline ~/.codemender/ directory (contains state.db and identity.key) -> workspace_base.tar.gz
        ├── Extracts active finding IDs and intelligently partitions them into N lists (partition_i.json)
        └── Saves workspace_base.tar.gz, partition files, and manifest.json (findings_count) to GCS
              │
              ▼ (Triggers automatically when Scan finishes)
[ Stage 2: FIX (Parallel Shards) ]
  ├── Run N Containers in Parallel (Workers)
        ├── Each container clones repo, downloads & extracts workspace_base.tar.gz to ~/.codemender/
        ├── Each container downloads its assigned partition list (partition_i.json)
        ├── Each container runs 'cm verify <id>' & 'cm fix <id>' ONLY for assigned IDs -> Pushes PRs
        └── Uploads its mutated database to GCS as worker_i_state.db
              │
              ▼ (Triggers automatically when all Workers finish)
[ Stage 3: AGGREGATE ]
  └── Run 1 Container (Aggregator)
        ├── Downloads workspace_base.tar.gz and all worker_i_state.db files from GCS
        ├── Merges worker databases into base state.db via SQLite 'INSERT OR REPLACE INTO findings'
        ├── Compiles final consolidated HTML report (cm report -f html)
        └── Uploads final report to GCS (generates temporary signed access URL)
```

### Environment Variable Contracts for Stage Dispatches

When containers run in serverless execution environments (GCP Cloud Run Jobs or
GitHub Actions), the exact same container image is used. The entrypoint script
(`orchestrator.py`) relies on environment variables to determine its execution
role and parameters:

| Variable Name                  | Required   | Description              |
:                                : Stage(s)   :                          :
| :----------------------------- | :--------- | :----------------------- |
| `CODEMENDER_RUN_MODE`          | **All**    | Execution stage mode:    |
:                                :            : `scan` (Stage 1),        :
:                                :            : `worker` (Stage 2), or   :
:                                :            : `aggregate` (Stage 3).   :
:                                :            : Default fallback\:       :
:                                :            : `sequential`.            :
| `CODEMENDER_SCAN_ID`           | **All**    | Unique identifier for    |
:                                :            : the scan run (e.g.,      :
:                                :            : `scan-20260720-203000`). :
:                                :            : Used as the GCS folder   :
:                                :            : prefix\:                 :
:                                :            : `scans/<scan_id>/`.      :
| `CODEMENDER_GCS_BUCKET`        | **All**    | Name of the GCS bucket   |
:                                :            : for state artifacts and  :
:                                :            : report uploads.          :
| `CODEMENDER_MAX_TASKS`         | **Stage 1  | Configurable upper limit |
:                                : (Scan)**   : cap for parallel worker  :
:                                :            : tasks (default\: `10` or :
:                                :            : `20`).                   :
| `CLOUD_RUN_TASK_INDEX`<br>*(or | **Stage 2  | 0-indexed worker task    |
: `CODEMENDER_WORKER_INDEX`)*    : (Worker)** : number. Cloud Run Jobs   :
:                                :            : automatically injects    :
:                                :            : `CLOUD_RUN_TASK_INDEX`.  :
| `CLOUD_RUN_TASK_COUNT`<br>*(or | **Stage 2  | Total parallel worker    |
: `CODEMENDER_TOTAL_WORKERS`)*   : (Worker)** : count $N$. Cloud Run     :
:                                :            : Jobs automatically       :
:                                :            : injects                  :
:                                :            : `CLOUD_RUN_TASK_COUNT`.  :

### How the Parallel Worker Count (N) is Determined & Scaled

1.  **Dynamic Derivation & Remote Branch Filtering in Stage 1**: When Stage 1
    (Scan Phase) completes, the coordinator extracts discovered findings from
    `state.db`. Unless `CODEMENDER_FORCE_OVERWRITE=true`, it checks
    `check_remote_branch_exists()` for each finding and **filters out findings
    whose feature branches already exist on remote**. The remaining active
    findings count is `active_findings_count`.
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
    *   Checks `check_remote_branch_exists()` for each finding and **filters out
        findings whose feature branches already exist on remote** (unless
        `CODEMENDER_FORCE_OVERWRITE=true`).
    *   Creates `workspace_base.tar.gz` from `~/.codemender/`.
    *   Extracts remaining active finding IDs, partitions them into
        `partition_i.json` files, and writes `manifest.json`
        (`active_findings_count`).
    *   Uploads tarball, partition lists, and manifest to GCS under
        `scans/[scan_id]/`.
*   **`worker.py`** (New File): Stage 2 Parallel Worker runner.
    *   Clones target repository (`git clone --depth 1`) using GitHub token.
    *   Downloads & extracts `workspace_base.tar.gz` to `~/.codemender/` to
        restore baseline state and `identity.key`.
    *   Downloads assigned partition list `partition_[worker_index].json`
        containing pre-filtered active finding IDs.
    *   Executes `cm verify` (Tier 1 retry up to 3x) and `cm fix` for assigned
        IDs; pushes feature branches & PRs.
    *   Uploads mutated state database to
        `scans/[scan_id]/worker_[worker_index]_state.db` on GCS.
*   **`aggregate.py`** (New File): Stage 3 Aggregator runner.
    *   Downloads `workspace_base.tar.gz` and all available `worker_*_state.db`
        files from GCS.
    *   Merges worker database tables into base `state.db` via SQLite `INSERT OR
        REPLACE INTO findings`.
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
    *   Document GitHub Actions matrix workflow (`gha_parallel_workflow.yaml`).
    *   Detail required IAM roles (`roles/workflows.invoker`,
        `roles/run.developer`).

### 6. Workflow Configuration Templates (New Files)

*   **`gcp_parallel_workflow.yaml`**: Cloud Workflows definition managing Stage
    1 → Stage 2 (N parallel tasks) → Stage 3 on GCP.
*   **`gha_parallel_workflow.yaml`**: GitHub Actions workflow template managing
    parallel matrix builds with job artifacts.
