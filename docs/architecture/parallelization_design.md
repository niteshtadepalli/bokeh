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
shared, secure folder (Google Cloud Storage or GitHub Actions Artifacts) to
coordinate state between the stages.

```
[ Stage 1: SCAN ]
  └── Run 1 Container (Coordinator)
        ├── Clones repo, runs 'cm find .' to find all vulnerabilities once
        ├── Exports finding details (scan.sarif) and cryptographic keys (identity.key)
        └── Saves both files to GCS
              │
              ▼ (Triggers automatically when Scan finishes)
[ Stage 2: FIX (Parallel Shards) ]
  ├── Run N Containers in Parallel (Workers)
        ├── Each container downloads the keyset (inherits the same Client ID)
        ├── Each container downloads & imports scan.sarif (restores baseline state database)
        ├── Each container processes a math-filtered slice (modulo) of the findings list
        ├── Runs verify & fix in parallel VM sandboxes -> Pushes PRs
        └── Exports its updated state to a unique file (worker_i.sarif) and saves to GCS
              │
              ▼ (Triggers automatically when all Workers finish)
[ Stage 3: AGGREGATE ]
  └── Run 1 Container (Aggregator)
        ├── Downloads scan.sarif and all worker_*.sarif files
        ├── Merges all files back into a single database (cm import)
        ├── Compiles the final consolidated HTML report (cm report -f html)
        └── Uploads the final report to GCS (generates temporary signed access URL)
```

### How the Parallel Worker Count (N) is Determined & Scaled

1.  **Dynamic Derivation from Stage 1**: When Stage 1 (Scan Phase) completes,
    the coordinator parses the output `scan.sarif` file to extract the exact
    number of discovered vulnerabilities (`findings_count`).
2.  **Handling Cloud Run Task Limits & Quotas**: GCP Cloud Run Jobs support
    executing up to **10,000 tasks** per job run. While GCP can physically scale
    to thousands of tasks, spinning up hundreds of parallel containers
    simultaneously introduces two major external bottlenecks:
    *   **GitHub Secondary Rate Limits**: Pushing dozens of branches and opening
        PRs at the exact same second will trigger GitHub API rate-limit blocks.
    *   **LLM Backend Quota**: Calling the CodeMender backend concurrently from
        too many workers can exhaust API rate limits (HTTP 429).
3.  **The `MAX_TASKS` Cap & Modulo Partitioning Formula**: To ensure optimal
    performance without hitting quota limits, the pipeline uses a configurable
    upper limit parameter: `MAX_TASKS` (default: `10` or `20`). The worker count
    `N` is calculated as:

    `N = min(findings_count, MAX_TASKS)`

    *   **Scenario A (Small/Medium scan)**: If 4 findings are discovered, Stage
        2 triggers `N = 4` worker tasks. Each task processes **1 finding**.
    *   **Scenario B (Large scan)**: If 40 findings are discovered and
        `MAX_TASKS=10`, Stage 2 triggers `N = 10` worker tasks. Using modulo
        partitioning (`finding_index % N == task_index`), each worker task
        processes **4 findings** sequentially in its isolated VM container.
    *   **Scenario C (Zero findings)**: If 0 findings are discovered, Stage 2
        and Stage 3 are skipped entirely.

### Core Security Guardrail:

The containers running the CodeMender CLI (which compile code and run tests)
**never** hold administrative GCP credentials to trigger other containers. The
orchestration pipeline is managed entirely **externally** by a secure control
plane (Google Cloud Workflows or the GitHub Actions engine) that holds the
execution permissions.

--------------------------------------------------------------------------------

## 3. Alternatives Considered & Ruled Out

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

--------------------------------------------------------------------------------

## 4. Detailed Implementation (Modular Package Architecture)

To implement this plan within the refactored `codemender_agent/` package structure, we will create or modify the following files:

### 1. `orchestrator.py` (Entrypoint Facade)

*   **Purpose**: Read `CODEMENDER_RUN_MODE` environment variable (`sequential`, `scan`, `worker`, `aggregate`) and dispatch to the corresponding runner module in `codemender_agent/runners/`.
*   **Detailed Changes**:
    *   `sequential`: Calls `codemender_agent.runners.sequential.run_sequential_pipeline()`.
    *   `scan`: Calls `codemender_agent.runners.scan.run_scan_pipeline()`.
    *   `worker`: Calls `codemender_agent.runners.worker.run_worker_pipeline()`.
    *   `aggregate`: Calls `codemender_agent.runners.aggregate.run_aggregate_pipeline()`.

### 2. `codemender_agent/runners/` Subpackage

*   **`sequential.py`** (Existing): Sequential single-loop execution runner for local runs and simple Cloud Run Jobs.
*   **`scan.py`** (New File): Stage 1 Coordinator runner.
    *   Clones repo and initializes CodeMender.
    *   Runs `cm find .` and exports `/tmp/scan.sarif`.
    *   Copies `/root/.codemender/identity.key`.
    *   Uploads both artifacts to GCS under `scans/[scan_id]/`.
*   **`worker.py`** (New File): Stage 2 Parallel Worker runner.
    *   Downloads `identity.key` to `/root/.codemender/identity.key` *before* running `cm init` to inherit the session Client ID.
    *   Downloads `scan.sarif` and imports baseline state (`cm import`).
    *   Applies modulo filtering: `finding_index % total_workers == worker_index`.
    *   Executes `verify` and `fix` loop for assigned slice; pushes feature branches & PRs.
    *   Exports shard state to `/tmp/worker_[index].sarif` and uploads to GCS.
*   **`aggregate.py`** (New File): Stage 3 Aggregator runner.
    *   Downloads `scan.sarif` and all `worker_*.sarif` files from GCS.
    *   Merges all SARIF files sequentially into the SQLite state DB (`cm import`).
    *   Generates final consolidated HTML report (`cm report -f html`) and uploads to GCS with signed access URL.

### 3. `tests/` Submodule Unit Tests

*   **`tests/test_runners_scan.py`**: Unit tests for Stage 1 artifact generation and GCS uploads.
*   **`tests/test_runners_worker.py`**: Unit tests for modulo partitioning, identity key restoration, SARIF importing, and shard fixing.
*   **`tests/test_runners_aggregate.py`**: Unit tests for multi-SARIF merging and HTML report compilation.

### 4. `docs/guides/production_run.md` (Updated)

*   **Purpose**: Update deployment documentation for parallel workflow orchestration.
*   **Detailed Changes**:
    *   Document deploying Google Cloud Workflows (`gcp_parallel_workflow.yaml`).
    *   Document GitHub Actions matrix workflow (`gha_parallel_workflow.yaml`).
    *   Detail required IAM roles (`roles/workflows.invoker`, `roles/run.developer`).

### 5. Workflow Configuration Templates (New Files)

*   **`gcp_parallel_workflow.yaml`**: Cloud Workflows definition managing Stage 1 → Stage 2 (N parallel tasks) → Stage 3 on GCP.
*   **`gha_parallel_workflow.yaml`**: GitHub Actions workflow template managing parallel matrix builds with job artifacts.
