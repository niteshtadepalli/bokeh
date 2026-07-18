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

### How the Parallel Worker Count ($N$) is Determined & Scaled

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
    $N$ is calculated as: $$N = \min(\text{findings\_count},
    \text{MAX\_TASKS})$$

    *   **Scenario A (Small/Medium scan)**: If 4 findings are discovered, Stage
        2 triggers $N = 4$ worker tasks. Each task processes **1 finding**.
    *   **Scenario B (Large scan)**: If 40 findings are discovered and
        `MAX_TASKS=10`, Stage 2 triggers $N = 10$ worker tasks. Using modulo
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
    *   *Idea*: Booting $N$ containers in parallel at the same time. Task 0
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

## 4. Detailed Implementation (File changes)

To implement this plan, we will create or modify the following files. No other
files should be touched.

### 1. `orchestrator.py` (Modified)

*   **Purpose**: Update the execution entrypoint to support polymorphic run
    modes.
*   **Detailed Changes**:
    *   Read the environment variable `CODEMENDER_RUN_MODE` (defaults to
        `sequential`).
    *   **`sequential` mode (Default)**: Keep the existing codebase unchanged.
        It clones, scans, and fixes in a single loop locally. This preserves the
        local-run and dry-run capabilities out-of-the-box.
    *   **`scan` mode**: Run `cm find .`, export findings to `/tmp/scan.sarif`,
        copy `/root/.codemender/identity.key`, and upload both to GCS under the
        prefix `scans/[scan_id]/`.
    *   **`worker` mode**:
        *   Download `identity.key` from GCS to `/root/.codemender/identity.key`
            *before* running `cm init`.
        *   Download `scan.sarif` and run `cm import /tmp/scan.sarif`.
        *   Determine assigned findings using modulo filtering: `finding_index %
            total_workers == worker_index`.
        *   Run the verify and fix loop for assigned findings.
        *   Export the updated database state to `/tmp/worker_[index].sarif` and
            upload to GCS.
    *   **`aggregate` mode**:
        *   Download `scan.sarif` and all `worker_*.sarif` files from GCS.
        *   Import them all sequentially to merge states.
        *   Compile the consolidated HTML report and upload it to GCS.

### 2. `test_orchestrator.py` (Modified)

*   **Purpose**: Add unit test coverage for the new sharding and consolidation
    logic.
*   **Detailed Changes**:
    *   Test GCS download/upload fallback mocks.
    *   Test the modulo partitioning logic (verifying that findings are divided
        evenly and deterministically across different indices and counts).
    *   Test the SARIF import merging sequence to ensure no updates are dropped.

### 3. `production_run_guide.md` (Modified)

*   **Purpose**: Add user documentation for deploying the parallel workflow.
*   **Detailed Changes**:
    *   Provide step-by-step instructions on creating the Cloud Workflows
        definition and linking it to Cloud Scheduler.
    *   Document the required IAM permissions for the Workflow service account.

### 4. `gcp_parallel_workflow.yaml` (New File)

*   **Purpose**: The Google Cloud Workflows YAML definition.
*   **Rationale**: Serves as the template that customers deploy to orchestrate
    the sequential scan, parallel fix, and aggregation container tasks on GCP.

### 5. `gha_parallel_workflow.yaml` (New File)

*   **Purpose**: A template GitHub Actions workflow.
*   **Rationale**: Serves as the template showing how to run the parallel scan
    and fix pipeline completely for free inside GitHub Actions runner pools
    using matrix builds and job artifacts, without needing GCP resources.
