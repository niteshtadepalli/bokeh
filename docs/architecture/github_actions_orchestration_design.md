# CodeMender Orchestrator: GitHub Actions Native Orchestration Specification & Guardrails

This document serves as the absolute source of truth and architectural
guardrails for deploying and executing the **CodeMender Orchestrator** natively
within **GitHub Actions (GHA)**, enabling automated, parallel vulnerability
remediation that is 100% self-contained in GitHub while preserving complete
backward compatibility with existing Google Cloud Platform (GCP) deployments.

--------------------------------------------------------------------------------

## 1. The Problem

The CodeMender AI backend requires local compilers, linters, and test suites to
validate vulnerabilities and prove that generated security patches work. While
the Orchestrator currently runs on Google Cloud infrastructure (Cloud Run Jobs,
Cloud Workflows, and Cloud Storage), enterprise engineering teams whose source
code and CI/CD workflows reside entirely on GitHub face significant onboarding
friction when forced to provision external cloud storage buckets, IAM roles, and
cloud schedulers. Engineering teams need a native, self-contained GitHub Actions
solution that can be onboarded onto any repository with a simple 10-line
workflow call. This solution must support both scheduled repository-wide
remediation and targeted scanning on active Pull Requests, scale parallel
workers dynamically without race conditions, and operate with zero external
cloud bucket dependencies.

--------------------------------------------------------------------------------

## 2. The Technical Plan

The GitHub Actions integration provides a decentralized, 3-stage parallel
pipeline running inside containerized GitHub Actions runner instances,
coordinated by an **Organization Reusable Workflow** and packaged in a
standardized container hosted on **GitHub Container Registry (GHCR)**.

```
+---------------------------------------------------------------------------------------------------------+
|                                    CENTRAL PACKAGING & REUSABLE WORKFLOW                                |
|                                                                                                         |
|  1. Central GHCR Image: ghcr.io/<org>/codemender-runner:v1 (Pre-baked runtimes + mise + cm + agent)      |
|  2. Central Reusable Workflow: .github/workflows/codemender-parallel.yml@v1                             |
+---------------------------------------------------------------------------------------------------------+
                                                     │
                                                     ▼
+---------------------------------------------------------------------------------------------------------+
|                                       STAGE 1: SCAN & PARTITION                                         |
|  Job: scan (runs on: configurable runner_type, e.g. ubuntu-latest)                                      |
|                                                                                                         |
|  - Authenticate to GCP (Workload Identity Federation or SA Key) -> Sets GOOGLE_APPLICATION_CREDENTIALS  |
|  - Clone repository & record target Git commit SHA (target_sha)                                         |
|  - Dynamic setup: auto-detect package manifests (package.json, requirements.txt, go.mod, pom.xml)        |
|  - Execute 'cm find .' -> Discover vulnerability findings                                               |
|  - Deduplicate: check 'git ls-remote' for existing 'codemender/fix-<vuln>-<hash>' branches             |
|  - Partition active findings into N worker buckets (partition_0.json .. partition_N.json)               |
|  - Archive ~/.codemender/ -> workspace_base.tar.gz                                                      |
|  - Upload GHA Artifact: 'codemender-base-state'                                                         |
|  - Emit GITHUB_OUTPUT: matrix=[0, 1, ..., N-1], findings_count, target_sha                              |
+---------------------------------------------------------------------------------------------------------+
                                                     │
                                                     ▼ (if: needs.scan.outputs.findings_count != '0')
+---------------------------------------------------------------------------------------------------------+
|                               STAGE 2: PARALLEL WORKERS (Dynamic Matrix)                                |
|  Job: worker (strategy.matrix.worker_index = [0..N-1], strategy.fail-fast = false)                      |
|                                                                                                         |
|  - Authenticate to GCP & mint ephemeral, least-privilege GitHub App Installation Token (1-hour TTL)     |
|  - Download Artifact 'codemender-base-state' & extract ~/.codemender/                                   |
|  - Read partition_${{ matrix.worker_index }}.json                                                       |
|  - For each assigned finding:                                                                           |
|      * Verify finding exploitability: 'cm verify -y --bypass-warning <id>' (Tier 1 retries)             |
|      * Generate and apply patch: 'cm fix -y --bypass-warning <id>'                                      |
|      * Stage fixed files & commit to branch 'codemender/fix-<vuln>-<hash>'                              |
|      * Open PR:                                                                                         |
|          - If Nightly Scan: Open top-level PR (head: codemender/fix-..., base: main)                    |
|          - If PR Scan: Open Child PR (head: codemender/fix-..., base: developer's PR branch)            |
|  - Upload GHA Artifact: 'worker-shard-${{ matrix.worker_index }}' (worker state.db & metadata JSON)     |
+---------------------------------------------------------------------------------------------------------+
                                                     │
                                                     ▼ (if: always() && needs.scan.outputs.findings_count != '0')
+---------------------------------------------------------------------------------------------------------+
|                                    STAGE 3: AGGREGATOR & REPORTING                                      |
|  Job: aggregate (needs: [scan, worker])                                                                 |
|                                                                                                         |
|  - Download Artifact 'codemender-base-state' & all 'worker-shard-*' artifacts                           |
|  - Merge worker SQLite database shards into base state.db using dynamic SQLite UPSERT                   |
|  - Purge SKIPPED_DUPLICATE records from merged state.db                                                 |
|  - Compile reports: 'cm report -f html' and 'cm report -f sarif'                                        |
|  - Publish 4-Tier Reporting Surfaces:                                                                   |
|      1. Upload SARIF to GitHub Security Tab ('github/codeql-action/upload-sarif')                       |
|      2. Render Markdown overview to $GITHUB_STEP_SUMMARY (PR links, stats, token breakdown table)       |
|      3. Upload downloadable HTML/JSON report artifacts (retained 90 days)                               |
+---------------------------------------------------------------------------------------------------------+
```

### Key Components & Operational Flow

1.  **Packaging & Universal Runner Image
    (`ghcr.io/<org>/codemender-runner:latest`)**:

    *   A pre-built multi-toolchain container hosted on GitHub Container
        Registry (GHCR). It packages standard LTS runtime versions (Python,
        Node.js, Go, OpenJDK), standard build essentials (`gcc`, `g++`, `make`,
        `git`, `curl`, `fuser`), the `cm` Go CLI binary, and the Python
        orchestrator.
    *   Embeds `mise` (universal version manager) to automatically download and
        activate non-standard language runtime versions on the fly when
        repository version files (`.nvmrc`, `.python-version`, `.tool-versions`)
        are detected.
    *   Auto-installs repository package dependencies during initialization
        (`npm ci`, `pip install`, `go mod download`, `mvn
        dependency:go-offline`, `cargo fetch`).

2.  **Stage 1 Coordinator (`scan`)**:

    *   Clones the repository, pins the target Git commit SHA (`target_sha`),
        and runs `cm find .`.
    *   Evaluates findings against open Pull Requests and existing remote
        branches using globally deterministic branch naming
        (`codemender/fix-<vuln_type>-<fingerprint>`).
    *   Partitions active findings across $N$ tasks (capped by `max_tasks`),
        archives `~/.codemender/` into `workspace_base.tar.gz`, uploads the
        `codemender-base-state` artifact, and outputs `matrix=[0, 1, ..., N-1]`
        to `$GITHUB_OUTPUT`.

3.  **Stage 2 Matrix Workers (`worker`)**:

    *   Uses GHA dynamic matrix expansion (`strategy.matrix.worker_index = ${{
        fromJson(needs.scan.outputs.matrix) }}`) to launch $N$ concurrent runner
        instances (`runs-on: ${{ inputs.runner_type }}`).
    *   Restores `workspace_base.tar.gz` and processes assigned findings from
        `partition_${{ matrix.worker_index }}.json`.
    *   Executes exploit reproduction (`cm verify`) and remediation (`cm fix`).
    *   Commits fixes to branch `codemender/fix-<vuln>-<hash>` and pushes to
        remote.
    *   **Dual PR Remediation Strategy**:
        *   **Nightly Scans**: Opens a top-level PR targeting `main`.
        *   **PR Scans**: Opens a **Child PR targeting the developer's pending
            PR branch**, providing isolated 1-click review without push race
            conditions.
    *   Uploads mutated `worker_${{ matrix.worker_index }}_state.db` and
        metadata as run artifacts.

4.  **Stage 3 Aggregator (`aggregate`)**:

    *   Downloads all `worker-shard-*` artifacts, merges tables into `state.db`
        via SQLite `ATTACH DATABASE` and dynamic UPSERT queries, and purges
        `SKIPPED_DUPLICATE` records.
    *   Ingests token metrics from all worker metadata JSONs and aggregates
        per-model consumption.
    *   Compiles HTML and SARIF reports (`cm report`).
    *   Publishes the **4-Tier Reporting Model**:
        1.  **Security Dashboard**: Uploads SARIF to **GitHub Security Tab
            $\rightarrow$ Code scanning alerts**.
        2.  **CI Run Overview**: Renders a rich Markdown dashboard in
            **`$GITHUB_STEP_SUMMARY`** with severity cards, PR links, and token
            metrics.
        3.  **In-PR Context**: Embeds analysis and finding metadata inside the
            created Child/Top-level PR descriptions.
        4.  **Triage Artifacts**: Uploads `report.html` and `report.json` as
            downloadable run artifacts (90-day retention).

--------------------------------------------------------------------------------

## 3. Alternatives Considered & Ruled Out

To serve as guardrails against future architectural drift or regression, the
following major design alternatives were evaluated and explicitly rejected:

### 1. Pushing Fix Commits Directly to the Active PR Source Branch

-   **What was considered:** For PR scans, having Stage 2 workers commit and
    push directly to the developer's source branch (e.g. `git push origin
    HEAD:feature/payments`).
-   **Why it was ruled out:** When multiple vulnerabilities are verified in
    parallel, multiple workers attempting to push to the same branch
    simultaneously trigger non-fast-forward git push rejections. Furthermore,
    mutating a developer's feature branch while they are actively coding locally
    causes unexpected local merge conflicts. Creating **Child PRs targeting the
    feature branch** eliminates all push collisions, gives the developer
    isolated 1-click review, and leaves their working tree undisturbed.

### 2. PR-Scoped Branch Naming (`codemender/pr42-fix-...`)

-   **What was considered:** Prefixing branch names with the PR number (e.g.
    `codemender/pr42-fix-sqli-7a8b9c1d`).
-   **Why it was ruled out:** PR-specific scoping breaks Git-level deduplication
    between Nightly scans and PR scans. If a vulnerability already has an open
    remediation branch created by a Nightly scan on `main`, PR-scoped naming
    would fail to match `check_remote_branch_exists()`, burning unnecessary
    worker compute and LLM tokens. Using **Unscoped Global Deterministic Branch
    Naming (`codemender/fix-<vuln>-<hash>`)** turns `git ls-remote` into the
    single source of truth and instantly skips duplicates across both Nightly
    and PR scans.

### 3. Requiring External Cloud Storage (GCS/S3) for GHA Transit

-   **What was considered:** Forcing GitHub Actions workflows to provision and
    pass a Google Cloud Storage bucket for intermediate state tarballs and
    database shards.
-   **Why it was ruled out:** Requiring cloud buckets adds infrastructure
    management friction and defeats the purpose of native GitHub Actions
    onboarding. Using native GitHub Actions Artifacts
    (`actions/upload-artifact@v4` / `actions/download-artifact@v4`) makes the
    workflow **100% self-contained in GitHub** with zero cloud bucket
    dependencies.

### 4. Monolithic "Fat" Container Image with All Historic Runtimes

-   **What was considered:** Building a massive ~10 GB Docker image containing
    every historic version of Node (14..22), Python (3.7..3.12), Java (8..21),
    and Ruby.
-   **Why it was ruled out:** Massive images suffer from slow cold-start pull
    times (1–3 minutes per runner). A lean base image (~1.5 GB) containing
    current LTS runtimes paired with `mise` (which downloads standalone
    precompiled toolchains on the fly in seconds) provides high speed and broad
    version coverage.

### 5. Maintaining Separate Sequential and Parallel Workflows for GHA

-   **What was considered:** Supporting a standalone single-job sequential
    workflow alongside the parallel matrix workflow.
-   **Why it was ruled out:** Maintaining duplicate workflow logic introduces CI
    drift. The 3-stage dynamic matrix workflow naturally collapses to 1 worker
    when 1 finding is present and skips cleanly when 0 findings are found,
    making a separate sequential pipeline redundant.

### 6. Storing Long-Lived GCP Service Account JSON Keys Exclusively

-   **What was considered:** Requiring users to export a GCP Service Account
    private key JSON file and store it in GitHub Secrets.
-   **Why it was ruled out:** Long-lived private key files present security
    risks and maintenance overhead (key rotation). Implementing **Workload
    Identity Federation (WIF / OIDC)** via `google-github-actions/auth@v2`
    provides keyless, short-lived (1-hour) authentication, while keeping Service
    Account JSON keys as an optional testing fallback.

### 7. Exposing Broad `GITHUB_TOKEN` Write Permissions to Worker Subprocesses

-   **What was considered:** Passing `GITHUB_TOKEN` with write access into the
    default container environment during `cm verify`.
-   **Why it was ruled out:** If untrusted test suites or build scripts run
    during `cm verify`, credentials in the environment could be exfiltrated. The
    orchestrator scrubs all tokens from child subprocess environments, passes
    credentials strictly inline during `git push`, and utilizes short-lived
    GitHub App Installation Tokens.

--------------------------------------------------------------------------------

## 4. Detailed Implementation Plan

This section enumerates every file that will be created or modified in the
repository to implement native GitHub Actions support.

```
.
├── .github/
│   └── workflows/
│       ├── build_runner_image.yml         # (NEW) CI workflow to build & publish runner to GHCR
│       └── codemender_parallel.yml        # (NEW) Central Reusable Workflow definition
├── Dockerfile                             # (MODIFIED) Updated with multi-toolchain LTS, mise, build essentials
├── codemender_agent/
│   ├── config.py                          # (MODIFIED) Extended with GHA storage mode and PR scan properties
│   ├── storage.py                         # (MODIFIED) Added GHA local transit storage abstraction
│   ├── runners/
│   │   ├── scan.py                        # (MODIFIED) Output GITHUB_OUTPUT matrix & bundle GHA transit files
│   │   ├── worker.py                      # (MODIFIED) Added Child PR base branch targeting & transit save
│   │   └── aggregate.py                   # (MODIFIED) Ingest transit shards, write $GITHUB_STEP_SUMMARY, export SARIF
│   └── vcs/
│       ├── git.py                         # (MODIFIED) Enforce global deterministic branch naming
│       └── github.py                      # (MODIFIED) Enhanced open PR deduplication & custom base branch support
├── tests/
│   ├── test_storage.py                    # (MODIFIED) Unit tests for GHA transit storage mode
│   ├── test_vcs_github.py                 # (MODIFIED) Unit tests for Child PR creation & branch checks
│   └── e2e_test_local.py                  # (MODIFIED) End-to-end local simulation updated for GHA mode
└── docs/
    └── guides/
        └── github_actions_guide.md        # (NEW) Developer and administrator onboarding guide for GHA
```

--------------------------------------------------------------------------------

### 1. `Dockerfile`

*   **Why change:** Must serve as the universal multi-toolchain container
    published to GHCR.
*   **Detailed changes:**
    *   Base on `ubuntu:22.04` (or `debian:bookworm-slim`).
    *   Install core system utilities: `git`, `curl`, `jq`, `tar`, `gzip`,
        `fuser`, `ca-certificates`, `build-essential` (`gcc`, `g++`, `make`).
    *   Install LTS language runtimes: Python 3.11/3.12 (`python3-pip`,
        `python3-venv`), Node.js 20 LTS (`npm`, `yarn`, `pnpm`), Go (latest
        stable), OpenJDK 17/21 (`maven`, `gradle`).
    *   Install `mise` binary into `/usr/local/bin/mise` for dynamic runtime
        resolution.
    *   Install CodeMender Go CLI (`cm`) into `/usr/local/bin/cm`.
    *   Copy `codemender_agent/` and `orchestrator.py` into `/opt/codemender`.
    *   Set `PYTHONPATH=/opt/codemender` and entrypoint to `python3
        /opt/codemender/orchestrator.py`.

--------------------------------------------------------------------------------

### 2. `.github/workflows/build_runner_image.yml` (New File)

*   **Why create:** Automates building and publishing the multi-toolchain runner
    container to GHCR.
*   **Detailed implementation:**
    *   Triggers on push to `main` (when `Dockerfile` or `codemender_agent/**`
        changes) or release tags.
    *   Uses `docker/setup-buildx-action@v3` and `docker/login-action@v3`
        (logging in to `ghcr.io` with `${{ secrets.GITHUB_TOKEN }}`).
    *   Builds and pushes `ghcr.io/<org>/codemender-runner:latest` and tagged
        versions with Docker layer caching (`type=gha`).

--------------------------------------------------------------------------------

### 3. `.github/workflows/codemender_parallel.yml` (New File)

*   **Why create:** The central Organization Reusable Workflow (`workflow_call`)
    that orchestrates the 3-stage pipeline across any calling repository.
*   **Detailed implementation:**
    1.  **Inputs & Secrets Declaration:**
        *   `inputs`: `scan_target` (string, default: `"."`), `build_command`
            (string, optional), `runner_type` (string, default:
            `"ubuntu-latest"`), `max_tasks` (number, default: `10`),
            `cli_version` (string, default: `"preview"`), `model` (string,
            optional), `models` (string, optional).
        *   `secrets`: `gcp_workload_identity_provider` (optional),
            `gcp_service_account` (optional), `gcp_sa_key` (optional),
            `github_app_id` (optional), `github_app_private_key` (optional),
            `github_token` (optional).
    2.  **Job 1: `scan`:**
        *   `runs-on: ${{ inputs.runner_type }}` inside `container:
            ghcr.io/<org>/codemender-runner:latest`.
        *   Authenticates with GCP via `google-github-actions/auth@v2` (WIF or
            SA key fallback).
        *   Checks out code (`actions/checkout@v4` with `fetch-depth: 1`).
        *   Executes `orchestrator.py` with `CODEMENDER_RUN_MODE=scan` and
            `CODEMENDER_STORAGE_MODE=github_actions`.
        *   Emits `$GITHUB_OUTPUT` parameters (`matrix`, `findings_count`,
            `target_sha`).
        *   Uploads artifact `codemender-base-state` (`workspace_base.tar.gz`,
            `partition_*.json`, `scan_metadata.json`).
    3.  **Job 2: `worker`:**
        *   `needs: scan`, `if: needs.scan.outputs.findings_count != '0'`.
        *   `runs-on: ${{ inputs.runner_type }}`, `container:
            ghcr.io/<org>/codemender-runner:latest`.
        *   `strategy: { fail-fast: false, matrix: { worker_index: ${{
            fromJson(needs.scan.outputs.matrix) }} } }`.
        *   Authenticates with GCP (WIF/SA key) and generates GitHub App token
            (via `actions/create-github-app-token@v1` or secret fallback).
        *   Downloads artifact `codemender-base-state`.
        *   Executes `orchestrator.py` with `CODEMENDER_RUN_MODE=worker`,
            `CODEMENDER_WORKER_INDEX=${{ matrix.worker_index }}`, and PR
            targeting flags.
        *   Uploads artifact `worker-shard-${{ matrix.worker_index }}`
            (`worker_${{ matrix.worker_index }}_state.db` and metadata JSON).
    4.  **Job 3: `aggregate`:**
        *   `needs: [scan, worker]`, `if: always() &&
            needs.scan.outputs.findings_count != '0'`.
        *   `runs-on: ${{ inputs.runner_type }}`, `container:
            ghcr.io/<org>/codemender-runner:latest`.
        *   Authenticates with GCP.
        *   Downloads `codemender-base-state` and all `worker-shard-*` artifacts
            (`pattern: worker-shard-*`, `merge-multiple: true`).
        *   Executes `orchestrator.py` with `CODEMENDER_RUN_MODE=aggregate`.
        *   Uploads `report.sarif` to GitHub Security Tab via
            `github/codeql-action/upload-sarif@v3`.
        *   Uploads `codemender-final-report` artifact (`report.html`,
            `report.json`, retention: 90 days).

--------------------------------------------------------------------------------

### 4. `codemender_agent/config.py`

*   **Why change:** Central configuration reader must parse GHA-specific runtime
    parameters without breaking GCP defaults.
*   **Detailed changes:**
    1.  Add fields to `OrchestratorConfig`:
        *   `storage_mode: str = "gcs"` (defaults to `"github_actions"` if
            `GITHUB_ACTIONS == "true"` or `CODEMENDER_STORAGE_MODE ==
            "github_actions"`).
        *   `is_pr_scan: bool = False` (parsed from `CODEMENDER_IS_PR_SCAN` or
            `GITHUB_EVENT_NAME == "pull_request"`).
        *   `pr_base_branch: Optional[str] = None` (parsed from
            `CODEMENDER_PR_BASE_BRANCH` or `GITHUB_HEAD_REF`).
    2.  In `inject_codemender_config()`:
        *   Preserve existing sandbox and tool configuration.
        *   Execute dynamic dependency detection routine (`npm ci`, `pip
            install`, `go mod download`, `mvn dependency:go-offline`, `cargo
            fetch`) before running `cm init --verify`.

--------------------------------------------------------------------------------

### 5. `codemender_agent/storage.py`

*   **Why change:** Implement filesystem-based transit storage mode for GitHub
    Actions artifacts alongside existing GCS and Local modes.
*   **Detailed changes:**
    1.  Add helper `_get_gha_transit_dir() -> str`:
        *   Resolves transit directory at
            `$GITHUB_WORKSPACE/.codemender_transit/` (or
            `/tmp/codemender_transit/`).
    2.  Update `upload_file_to_gcs()` and `download_file_from_gcs()`:
        *   If `storage_mode == "github_actions"`: copy files to/from
            `_get_gha_transit_dir()` using standard `shutil.copy()`.
    3.  Update `generate_signed_url()`:
        *   If `storage_mode == "github_actions"`: return local `file://` URI
            pointing to the transit path.

--------------------------------------------------------------------------------

### 6. `codemender_agent/runners/scan.py`

*   **Why change:** Stage 1 runner must emit `$GITHUB_OUTPUT` variables and
    prepare artifacts in GHA mode while retaining GCS signed URL generation in
    GCP mode.
*   **Detailed changes:**
    1.  In `_save_and_upload_state()`:
        *   Check `storage_mode`. If `"github_actions"`:
        *   Copy `workspace_base.tar.gz`, `partition_*.json`, and
            `scan_metadata.json` to the transit folder.
        *   Write outputs to `$GITHUB_OUTPUT`:
            *   `matrix=[0, 1, ..., N-1]` (JSON string)
            *   `findings_count=<count>`
            *   `target_sha=<sha>`
        *   If `"gcs"`: execute existing GCS bucket upload and Signed URL
            generation logic unchanged.

--------------------------------------------------------------------------------

### 7. `codemender_agent/runners/worker.py`

*   **Why change:** Stage 2 runner must target the developer's pending PR branch
    when running PR scans and stage shard artifacts for GHA upload.
*   **Detailed changes:**
    1.  In `_setup_git_and_checkout()`:
        *   If `is_pr_scan == True` and `pr_base_branch`: checkout
            `pr_base_branch` as the working base.
    2.  In `_process_finding()`:
        *   Determine `base_branch`:
        *   If `is_pr_scan == True` $\rightarrow$ `base_branch = pr_base_branch`
            (e.g. `feature/payments`).
        *   If `is_pr_scan == False` $\rightarrow$ `base_branch =
            default_branch` (e.g. `main`).
        *   Create branch `codemender/fix-<vuln_type>-<fingerprint>`.
        *   Commit fix and push to `origin/codemender/fix-...`.
        *   Call `create_pull_request(head=branch_name, base=base_branch)`:
        *   Opens a Child PR targeting the feature branch during PR scans.
        *   Opens a top-level PR targeting `main` during Nightly scans.
    3.  In `run_worker_pipeline()`:
        *   If `storage_mode == "github_actions"`: copy
            `worker_${worker_index}_state.db` and
            `worker_${worker_index}_metadata.json` to the transit directory for
            GHA artifact upload.

--------------------------------------------------------------------------------

### 8. `codemender_agent/runners/aggregate.py`

*   **Why change:** Stage 3 aggregator must merge local DB shards from GHA
    transit artifacts, export SARIF format, and publish `$GITHUB_STEP_SUMMARY`.
*   **Detailed changes:**
    1.  In `run_aggregate_pipeline()`:
        *   If `storage_mode == "github_actions"`: discover all
            `worker_*_state.db` files in the local transit directory.
        *   Execute `merge_db()` (using existing SQLite `ATTACH DATABASE` and
            dynamic UPSERT queries).
        *   Purge `SKIPPED_DUPLICATE` records from `state.db`.
        *   Execute `cm report -f html` and `cm report -f sarif`.
        *   Generate GitHub Flavored Markdown dashboard and append directly to
            `$GITHUB_STEP_SUMMARY`:
        *   Total findings scanned, verified, and fixed.
        *   Table of links to created Pull Requests.
        *   Per-Model LLM Token Consumption breakdown table.

--------------------------------------------------------------------------------

### 9. `codemender_agent/vcs/git.py` & `vcs/github.py`

*   **Why change:** Enforce global deterministic branch naming and child PR base
    targeting.
*   **Detailed changes:**
    1.  In `generate_branch_name()`:
        *   Enforce global deterministic naming:
            `codemender/fix-<vuln_type>-<fingerprint>` where fingerprint is
            `SHA256(filePath + vulnType + startLine)[:8]`.
    2.  In `is_duplicate_pr()`:
        *   Query open PRs via GitHub API.
        *   Check if an **open** PR exists covering `(filePath, vulnType,
            startLine)` within a 15-line window.
        *   If remote branch exists **AND** open PR exists $\rightarrow$ return
            `True` (skip).
        *   If remote branch exists **BUT** previous PR is closed/rejected
            $\rightarrow$ return `False` (allow `git push -f` overwrite when
            `CODEMENDER_FORCE_OVERWRITE=true`).
    3.  In `create_pull_request()`:
        *   Accept `base_branch` argument dynamically (allowing `base:
            "feature/payments"` or `base: "main"`).

--------------------------------------------------------------------------------

### 10. `tests/` Test Suite Updates

*   **Why change:** Validate GHA mode, transit storage, and Child PR generation
    without requiring live cloud infrastructure.
*   **Detailed changes:**
    1.  `tests/test_storage.py`: Add test cases verifying `upload_file_to_gcs`
        and `download_file_from_gcs` in `CODEMENDER_STORAGE_MODE=github_actions`
        mode.
    2.  `tests/test_vcs_github.py`: Add test cases verifying
        `create_pull_request` passes custom `base` parameters for Child PRs.
    3.  `tests/test_runners_scan.py` & `test_runners_worker.py`: Add test cases
        verifying `$GITHUB_OUTPUT` emission and matrix JSON parsing.
    4.  `tests/e2e_test_local.py`: Add a full 3-stage local simulation test
        executing in `github_actions` storage mode.

--------------------------------------------------------------------------------

### 11. `docs/guides/github_actions_guide.md` (New File)

*   **Why create:** Comprehensive documentation for enterprise developers and
    security administrators onboarding GitHub Actions.
*   **Contents:**
    *   Setting up Workload Identity Federation (WIF) in GCP and configuring
        GitHub repository permissions.
    *   Setting up the GitHub App for least-privilege token generation.
    *   Example caller workflow for Nightly scans and PR label-based scans.
    *   Triage guide for reviewing Child PRs, viewing `$GITHUB_STEP_SUMMARY`,
        and accessing GitHub Code Scanning alerts.

--------------------------------------------------------------------------------

## 5. Summary Reference Table

Dimension               | GCP Cloud Run Mode (Existing)                                   | GitHub Actions Mode (New)
:---------------------- | :-------------------------------------------------------------- | :------------------------
**Trigger Mechanism**   | Cloud Scheduler $\rightarrow$ Cloud Workflows JSON payload      | GitHub Schedule (cron), `workflow_dispatch`, or `pull_request` label
**Control Plane**       | Google Cloud Workflows (`gcp_parallel_workflow.yaml`)           | GHA Reusable Workflow (`.github/workflows/codemender-parallel.yml`)
**Worker Scaling**      | Cloud Run Job Task Array (`taskCount: N`)                       | GHA Dynamic Matrix (`strategy.matrix: [0..N-1]`)
**State Transit**       | Google Cloud Storage Bucket + Signed URLs                       | GitHub Actions Run Artifacts (`actions/upload-artifact@v4`)
**Backend Auth**        | Cloud Run Service Account (built-in)                            | Workload Identity Federation (OIDC) or SA Key JSON
**PR Auth**             | Secret Manager (`GITHUB_APP_TOKEN`)                             | Ephemeral GitHub App Token or Repository Secret
**Nightly Remediation** | Top-level PRs against `main`                                    | Top-level PRs against `main`
**PR Remediation**      | N/A                                                             | **Child PRs targeting the pending PR branch**
**Reporting Surfaces**  | GCS HTML Report (Signed URL in logs)                            | SARIF (Security Tab) + `$GITHUB_STEP_SUMMARY` + GHA Artifact
**Deduplication**       | Global deterministic branch hash (`check_remote_branch_exists`) | Global deterministic branch hash (`git ls-remote` + open PR check)
