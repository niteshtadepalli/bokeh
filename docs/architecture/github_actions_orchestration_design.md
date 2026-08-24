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
cloud schedulers.

Engineering teams need a native, self-contained GitHub Actions solution that can
be onboarded onto any repository with a simple 10-line reusable workflow call.
This solution must:

1.  Support scheduled repository-wide remediation, manual on-demand triggers,
    and targeted scanning on active Pull Requests without overwhelming
    developers with pre-existing legacy tech debt.
2.  Scale parallel workers dynamically without race conditions, branch
    collisions, or GitHub API rate limit exhaustion.
3.  Operate with zero external cloud bucket dependencies while respecting
    cross-VM isolation.
4.  Securely isolate credentials, preventing supply-chain token exfiltration and
    handling fork PR boundaries safely.

--------------------------------------------------------------------------------

## 2. The Technical Plan

The GitHub Actions integration provides a decentralized, 3-stage parallel
pipeline running inside containerized GitHub Actions runner instances,
coordinated by an **Organization Reusable Workflow** and packaged in a
standardized base container hosted on **GitHub Container Registry (GHCR)** with
full support for **Bring-Your-Own-Image (BYOI)** custom toolchains.

```
+---------------------------------------------------------------------------------------------------------+
|                                    CENTRAL PACKAGING & REUSABLE WORKFLOW                                |
|                                                                                                         |
|  1. Central Base Image: ghcr.io/<org>/codemender-runner:latest (Pre-baked LTS toolchains + cm + venv)   |
|  2. Custom Runner Images: Optional BYOI image input (FROM ghcr.io/<org>/codemender-runner:latest)       |
|  3. Central Reusable Workflow: .github/workflows/codemender_parallel.yml@v1                             |
+---------------------------------------------------------------------------------------------------------+
                                                     │
                                                     ▼
+---------------------------------------------------------------------------------------------------------+
|                                       STAGE 1: SCAN & PARTITION                                         |
|  Job: scan (runs on: configurable runner_type, e.g. ubuntu-latest, container: runner_image)              |
|  Timeout: 55 mins (Hard cap < 60-min token TTL; eliminates in-Python refresh loops)                     |
|                                                                                                         |
|  - Token Minting: actions/create-github-app-token@v1 (or GITHUB_TOKEN) provides 60-min installation token|
|  - Checkout target commit: Pin developer's commit (github.event.pull_request.head.sha || github.sha)    |
|  - Execute 'cm find .' -> Full repo scan discovers all vulnerabilities (eliminates cross-file blindspots)|
|  - Differential PR Filtering (if PR scan):                                                             |
|      * Intersect findings with 'git diff -U0 origin/<pr_base_ref>...HEAD' modified line hunks           |
|      * Untouched pre-existing tech debt marked PRE_EXISTING_IGNORED in state.db (omitted from workers)   |
|  - Universal Deduplication (All scans: Nightly & PR): check 'git ls-remote' and open PRs                |
|      * Existing branches/PRs marked SKIPPED_DUPLICATE in state.db (omitted from workers; 0 token waste)  |
|  - Partition active findings into N worker buckets (partition_0.json .. partition_N.json)               |
|  - Archive ~/.codemender/ -> workspace_base.tar.gz in .codemender_transit/base/                         |
|  - Upload GHA Artifact: 'codemender-base-state' (retention: inputs.intermediate_artifact_retention_days) |
|  - Emit GITHUB_OUTPUT: matrix=[0, 1, ..., N-1] (fallback 'matrix=[0]', 'findings_count=0' on 0 findings) |
+---------------------------------------------------------------------------------------------------------+
                                                     │
                                                     ▼ (if: needs.scan.outputs.findings_count != '0' && != '')
+---------------------------------------------------------------------------------------------------------+
|                               STAGE 2: PARALLEL WORKERS (Dynamic Matrix)                                |
|  Job: worker (strategy.matrix.worker_index = [0..N-1], fail-fast = false, default max_tasks: 4 PR / 10 Nightly)
|  Timeout: 55 mins (Hard cap < 60-min token TTL; eliminates in-Python refresh loops)                     |
|                                                                                                         |
|  - Download Artifact 'codemender-base-state' & restore ~/.codemender/state.db                            |
|  - Checkout working_base_ref (target_sha for PR scans, default_branch for Nightly)                      |
|  - Read partition_${{ matrix.worker_index }}.json                                                       |
|  - For each assigned finding:                                                                           |
|      * Targeted Live Dedup: O(1) 'git ls-remote' + 'GET /pulls?head={owner}:{branch}'                   |
|      * Verify finding exploitability: 'cm verify -y --bypass-warning <id>' (Tier 1 retries)             |
|      * Generate and apply patch: 'cm fix -y --bypass-warning <id>'                                      |
|      * Surgical Git Staging: Query SQLite 'patches.edited_files' with 3-tier fallback to stage fix files|
|      * Push remediation branch:                                                                         |
|          - If Nightly / Manual Scan: Push 'codemender/fix-<vuln>-<hash>' & open top-level PR to main    |
|          - If Internal PR Scan: Push 'codemender/fix-<vuln>-<hash>' & open Child PR to pr_head_ref      |
|          - If Fork PR Scan: Skip Child PR; post Markdown review comment on Fork PR (pr_number) with diff|
|  - Save mutated state.db to .codemender_transit/shards/worker_${i}/worker_${i}_state.db                 |
|  - Upload GHA Artifact: 'worker-shard-${{ matrix.worker_index }}' (retention: intermediate days)        |
+---------------------------------------------------------------------------------------------------------+
                                                     │
                                                     ▼ (if: always() && needs.scan.result == 'success' && ...)
+---------------------------------------------------------------------------------------------------------+
|                                    STAGE 3: AGGREGATOR & REPORTING                                      |
|  Job: aggregate (needs: [scan, worker])                                                                 |
|  Timeout: 30 mins                                                                                       |
|                                                                                                         |
|  - Download 'codemender-base-state' and all available 'worker-shard-*' artifacts                        |
|  - Merge worker SQLite database shards into base state.db using clean UPDATE statements                 |
|  - Scoped Reporting Surfaces:                                                                           |
|      * Nightly Scan on main: Retain SKIPPED_DUPLICATE in report.sarif (status: underReview) for Security Tab|
|      * PR Scan: Omit PRE_EXISTING_IGNORED from HTML & PR SARIF; render clean "Clean as You Code" summary|
|  - Compile reports: 'cm report -f html' and 'cm report -f sarif' (relative file paths sanitized)        |
|  - Publish 4-Tier Reporting Surfaces:                                                                   |
|      1. Upload SARIF to GitHub Security Tab ('github/codeql-action/upload-sarif', continue-on-error)     |
|      2. Render Markdown overview to $GITHUB_STEP_SUMMARY (truncated at 1000 KiB buffer guardrail)       |
|      3. In-PR Context: Embedded Child PR descriptions or Fork PR Markdown review comments               |
|      4. Upload downloadable HTML/JSON report artifacts: 'codemender-final-report' (retention: 90 days)  |
+---------------------------------------------------------------------------------------------------------+
```

### Key Components & Operational Flow

1.  **Packaging & Universal Runner Image
    (`ghcr.io/<org>/codemender-runner:latest`)**:

    *   A pre-built multi-toolchain container hosted on GHCR packaging standard
        LTS runtime versions (Python 3.11, Node 20 LTS, Go stable, OpenJDK
        17/21), standard build essentials (`gcc`, `g++`, `make`, `git`, `curl`,
        `fuser`, `unzip`), the `cm` Go CLI binary in `/usr/local/bin/cm`, and
        the Python orchestrator.
    *   The orchestrator runs inside a dedicated, isolated virtual environment
        (`/opt/codemender/venv/bin/python3`), completely protecting orchestrator
        dependencies (`requests`, `pyyaml`, `google-cloud-storage`) from being
        mutated by repository test suites or build scripts.
    *   **Bring-Your-Own-Image (BYOI) Support**: Repositories requiring custom
        dependencies (e.g. PHP, Rust, proprietary toolchains) build a simple
        Dockerfile `FROM ghcr.io/<org>/codemender-runner:latest` and supply
        their image via `inputs.runner_image`.

2.  **Simplified Token Lifecycle Management & Hard Stage Timeouts ($\le 55\text{
    min}$)**:

    *   **60-Minute Token TTL Hard Cap**: GitHub App Installation Access Tokens
        have an immutable expiration lifespan of **60 minutes**. To completely
        eliminate the need for complex in-process token refresh logic,
        background renewal threads, or cryptographic dependencies (`PyJWT`,
        `cryptography`) in the Python runtime, all workflow jobs strictly
        enforce hard timeout caps:
        *   **Stage 1 (`scan`)**: Hard timeout **55 minutes**.
        *   **Stage 2 (`worker`)**: Hard timeout **55 minutes**.
        *   **Stage 3 (`aggregate`)**: Hard timeout **30 minutes**.
    *   This ensures that every Git command, GitHub REST API request, and patch
        verification subprocess completes well within the initial token's
        validity window.
    *   Token generation is handled natively at the workflow runner level via
        `actions/create-github-app-token@v1` (which dynamically resolves
        installation IDs without requiring `GITHUB_APP_INSTALLATION_ID`) or via
        standard `GITHUB_TOKEN` / PAT.
    *   The Python orchestrator receives a standard Bearer token via
        `os.environ["GITHUB_TOKEN"]`, keeping runtime dependencies minimal and
        hermetic.

3.  **Strict Trigger Restrictions & Security Boundaries**:

    *   Scans are strictly triggered in three ways only:
        1.  **Manual trigger** (`workflow_dispatch`)
        2.  **Nightly scan** (`schedule` on default branch)
        3.  **Certain PRs** (targeting `main`/`master` branch only) triggered by
            a specific tag/label only (e.g. `security-scan`).
    *   **Internal PR Scans**: Run with secure access to repository secrets.
        Propose remediation via Child PRs targeting the developer's feature
        branch (`pr_head_ref`).
    *   **Fork PR Scans**: Automated Child PR branch pushes are skipped
        (avoiding HTTP 403/422 errors on cross-repo boundaries). Instead,
        CodeMender calls the GitHub REST API using `pr_number` to post a
        structured Markdown review comment directly on the Fork PR with the
        patch diff and local `git apply` instructions.

4.  **Stage 1 Coordinator (`scan`)**:

    *   Clones the repository and pins the developer's exact commit
        (`target_sha = github.event.pull_request.head.sha || github.sha`).
    *   Executes a full repository scan via `cm find .` so the backend engine
        has complete codebase context, catching cross-file taint flows and
        semantic couplings.
    *   **Differential PR Filtering**: For PR scans, intersects discovered
        findings with `git diff -U0 origin/<pr_base_ref>...HEAD` modified line
        hunks (where `pr_base_ref` is the target branch, e.g. `main`). Untouched
        pre-existing findings are marked `PRE_EXISTING_IGNORED` in `state.db`
        and omitted from active worker partitions, keeping PR reviews completely
        noise-free.
    *   **Universal Deduplication (Nightly & PR scans)**: Evaluates findings
        against open PRs and existing remote branches using global deterministic
        branch naming (`codemender/fix-<vuln_type>-<fingerprint>`). Findings
        with existing remediation branches or open PRs are marked
        `SKIPPED_DUPLICATE` in `state.db` and omitted from worker partitions
        (burning 0 worker compute).
    *   Partitions active findings across $N$ tasks (default `max_tasks: 4` for
        PR scans, `10` for Nightly scans).
    *   Archives `~/.codemender/` into `workspace_base.tar.gz` in
        `.codemender_transit/base/`, uploads the `codemender-base-state`
        artifact, and outputs `matrix=[0, 1, ..., N-1]` (or fallback
        `matrix=[0]`, `findings_count=0` on clean scans).

5.  **Stage 2 Matrix Workers (`worker`)**:

    *   Launches $N$ concurrent runner instances via dynamic matrix expansion
        (`strategy: fail-fast: false`, `timeout-minutes: 55`).
    *   Restores `workspace_base.tar.gz` and checkouts `working_base_ref`
        (`target_sha` for PR scans, `default_branch` for Nightly).
    *   Performs **Targeted Live Deduplication**: $O(1)$ check via `git
        ls-remote` and `GET
        /repos/{owner}/{repo}/pulls?head={owner}:{branch}&state=open`. Resilient
        across worker retries with zero pagination over entire PR lists.
    *   Executes exploit verification (`cm verify`) and patch synthesis (`cm
        fix`).
    *   **Surgical Git Staging**: Queries SQLite `patches.edited_files` to stage
        *only* the exact files modified or created by CodeMender, with a 3-tier
        fallback hierarchy (`edited_files` $\rightarrow$ `target_file`
        $\rightarrow$ `git add -u` + `finding.FilePath`).
    *   **Global Branch Naming & Dual PR Routing**:
        *   All scans use canonical naming:
            `codemender/fix-<vuln_type>-<fingerprint>`.
        *   **Nightly / Manual Scans**: Pushes branch and opens a top-level PR
            targeting `main`.
        *   **Internal PR Scans**: Pushes branch and opens a Child PR targeting
            the developer's feature branch (`pr_head_ref`, populated from
            `GITHUB_HEAD_REF`).
        *   **Fork PR Scans**: Posts a Markdown review comment on the Fork PR
            (`pr_number`) with findings, patch diff, and local `git apply`
            instructions.
    *   Saves mutated `worker_${i}_state.db` to
        `.codemender_transit/shards/worker_${i}/` and uploads as run artifact.

6.  **Stage 3 Aggregator (`aggregate`)**:

    *   Downloads all available `worker-shard-*` artifacts into isolated
        subdirectories.
    *   Merges worker SQLite database shards into base `state.db` using clean
        `UPDATE` queries.
    *   **Scoped Reporting Surfaces**:
        *   **Nightly Scans on `main`**: Retains `SKIPPED_DUPLICATE` records in
            `state.db` and exports them with SARIF suppression metadata
            (`suppressions: [{ kind: "external", status: "underReview" }]`),
            preventing GitHub Code Scanning from falsely closing open alerts in
            the repository's Security Tab.
        *   **PR Scans**: Purges `PRE_EXISTING_IGNORED` findings before
            generating the per-scan HTML report and PR SARIF upload. Renders a
            concise "Clean as You Code" quality gate card in
            `$GITHUB_STEP_SUMMARY` without cluttering developer PR reviews with
            unrelated legacy debt.
    *   Compiles HTML and SARIF reports (`cm report`).
    *   Publishes the **4-Tier Reporting Model**:
        1.  **Security Dashboard**: Uploads SARIF to GitHub Security Tab
            (`github/codeql-action/upload-sarif@v3`, with `continue-on-error:
            true`).
        2.  **CI Run Overview**: Renders a Markdown dashboard in
            `$GITHUB_STEP_SUMMARY` (truncated at 1000 KiB buffer limit).
        3.  **In-PR Context**: Embedded Child PR descriptions or Fork PR review
            comments.
        4.  **Triage Artifacts**: Uploads `report.html` and `report.json` as
            `codemender-final-report` (90-day retention).

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
    feature branch (`pr_head_ref`)** (or posting comments on Fork PRs)
    eliminates push collisions, gives the developer isolated 1-click review, and
    leaves their working tree undisturbed.

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
    and PR scans. Disallowing force-overwrite on PR scans (`force_overwrite =
    False`) guarantees that PR scans never overwrite existing branches on
    `main`.

### 3. Requiring External Cloud Storage (GCS/S3) for GHA Transit

-   **What was considered:** Forcing GitHub Actions workflows to provision and
    pass a Google Cloud Storage bucket for intermediate state tarballs and
    database shards.
-   **Why it was ruled out:** Requiring cloud buckets adds infrastructure
    management friction and defeats the purpose of native GitHub Actions
    onboarding. Using native GitHub Actions Artifacts
    (`actions/upload-artifact@v4` / `actions/download-artifact@v4`) with an
    isolated local directory layout (`.codemender_transit/shards/worker_${i}/`)
    makes the workflow **100% self-contained in GitHub** with zero cloud bucket
    dependencies.

### 4. Running `cm find` on Changed Files Only (Cross-File Blindness)

-   **What was considered:** Passing only modified files (`cm find file1.py
    file2.py`) during Stage 1 PR scans to save scan time.
-   **Why it was ruled out:** Changes in modified files frequently introduce
    vulnerabilities in untouched files via **Cross-File Taint Flow / Semantic
    Coupling** (e.g. relaxing an input sanitizer in `utils/sanitizer.js`
    introduces SQL injection in untouched `routes/profile.js`). Running `cm find
    .` across the entire repository in Stage 1 and filtering findings via diff
    hunk intersection in Python provides full vulnerability discovery with zero
    PR noise.

### 5. Dynamic Toolchain Resolution via `mise` at Runtime

-   **What was considered:** Embedding `mise` to download compilers and language
    runtimes on the fly over the public internet during CI runner execution.
-   **Why it was ruled out:** Downloading runtimes during CI runs is fragile,
    slows down execution, and fails in corporate air-gapped or
    firewall-restricted runner environments. Pre-baking standard LTS runtimes
    into the universal base image and supporting Bring-Your-Own-Image (BYOI) for
    specialized toolchains makes runs 100% hermetic, fast, and offline-capable.

### 6. Blind Root-Level Package Auto-Installation (`npm ci`, `pip install`)

-   **What was considered:** Running hardcoded `npm ci`, `pip install`, `go mod
    download`, and `mvn dependency:go-offline` blindly at the repository root.
-   **Why it was ruled out:** Fails on repositories using alternative package
    managers (`yarn`, `pnpm`, `poetry`), monorepos with sub-packages in nested
    directories, and private registries requiring authentication. Relying on
    `CODEMENDER_BUILD_COMMAND` in `.codemender.yaml` and custom container images
    gives teams full control over dependency resolution.

### 7. Purging `SKIPPED_DUPLICATE` Records from SARIF Exports on `main`

-   **What was considered:** Deleting duplicate/already-open findings from
    `state.db` before generating `report.sarif` on `main`.
-   **Why it was ruled out:** When an existing finding is omitted from an
    uploaded SARIF report on `main`, GitHub Code Scanning interprets its absence
    as "Fixed" and automatically closes the security alert in the GitHub
    Security Tab. Exporting `SKIPPED_DUPLICATE` records on `main` with SARIF
    suppression metadata (`suppressions: [{ kind: "external", status:
    "underReview" }]`) keeps the alert open in a triaged state without polluting
    active CI runs.

### 8. Exposing Pre-Existing Legacy Debt in Pull Request Review Surfaces

-   **What was considered:** Including all pre-existing repository findings in
    PR review comments, PR step summaries, and PR SARIF uploads.
-   **Why it was ruled out:** Overwhelming developers with dozens of unrelated
    legacy vulnerabilities causes alert fatigue, confusion about PR merge
    blockers, and exposes sensitive vulnerability backlogs before patches are
    merged on `main`. Industry best practices (e.g. SonarCloud "Clean as You
    Code", GitHub CodeQL PR checks) dictate scoping PR surfaces strictly to
    newly modified code while managing total backlog via Nightly scans on
    `main`.

### 9. In-Python Token Refresh Loops and RS256 Cryptographic Signing

-   **What was considered:** Implementing in-Python cryptographic token managers
    with `PyJWT`/`cryptography` to sign RS256 JWTs and refresh tokens during
    long runs.
-   **Why it was ruled out:** Adds heavy cryptographic dependencies to
    `requirements.txt` and complicates the runtime. Because all jobs enforce
    strict timeouts $\le 55\text{ min}$, generating a standard 60-minute
    installation token via `actions/create-github-app-token@v1` at the job start
    guarantees complete token validity without any in-Python refresh overhead.

### 10. Staging Commits with Broad `git add -A`

-   **What was considered:** Replacing `git add -u` with a broad `git add -A` /
    `git add .` to capture newly created security fix helper files.
-   **Why it was ruled out:** If `.gitignore` is incomplete, `git add -A` can
    accidentally stage compiled binaries (`.class`, `.pyc`), test databases,
    coverage directories, and internal CodeMender files. Querying SQLite
    `patches.edited_files` with a 3-tier fallback hierarchy stages *only* the
    exact files modified or created by CodeMender.

### 11. Checking Out Default `github.sha` for Pull Request Scans

-   **What was considered:** Using `${{ github.sha }}` directly in
    `actions/checkout` for `pull_request` events.
-   **Why it was ruled out:** In GHA `pull_request` events, `github.sha` points
    to an ephemeral test merge commit (`refs/pull/<PR_ID>/merge`), not the
    developer's actual commit. Branching from this commit pollutes Git history
    with unrelated commits from `main`. PR scans must pin `target_sha =
    github.event.pull_request.head.sha`.

### 12. Allowing `FORCE_OVERWRITE` on Pull Request Scans

-   **What was considered:** Permitting `CODEMENDER_FORCE_OVERWRITE=true` to
    force-push branches during PR scans.
-   **Why it was ruled out:** Force-pushing during PR scans creates race
    conditions and risks clobbering active branches. `force_overwrite` is
    strictly disabled on all PR scans and reserved exclusively for Nightly scans
    on `main`.

--------------------------------------------------------------------------------

## 4. Detailed Implementation Plan

This section enumerates every file that will be created or modified in the
repository to implement native GitHub Actions support.

```
.
├── .github/
│   └── workflows/
│       ├── build_runner_image.yml         # (NEW) CI workflow with dual-sourcing to build & publish runner
│       └── codemender_parallel.yml        # (NEW) Central Reusable Workflow definition
├── Dockerfile                             # (MODIFIED) Multi-toolchain LTS base with isolated /opt/codemender/venv
├── requirements.txt                       # (UNCHANGED) Preserves lightweight dependencies without crypto bloat
├── codemender_agent/
│   ├── config.py                          # (MODIFIED) Extended with GHA storage, PR branch refs, and pr_number
│   ├── storage.py                         # (MODIFIED) Modular TransitStorageAdapter for GCS and GHA artifacts
│   ├── runners/
│   │   ├── scan.py                        # (MODIFIED) Full scan, diff hunk filtering, and GITHUB_OUTPUT matrix
│   │   ├── worker.py                      # (MODIFIED) Working base ref, surgical git staging, Child PR & Fork comments
│   │   └── aggregate.py                   # (MODIFIED) Ingest transit shards, clean UPDATE DB merge, scoped reporting
│   └── vcs/
│       ├── git.py                         # (MODIFIED) Global deterministic branch naming & git diff hunk parsing
│       └── github.py                      # (MODIFIED) Fork PR comments & targeted O(1) dedup lookups
├── tests/
│   ├── test_storage.py                    # (MODIFIED) Unit tests for TransitStorageAdapter
│   ├── test_vcs_github.py                 # (MODIFIED) Unit tests for create_pr_comment & targeted dedup
│   ├── test_runners_scan.py               # (MODIFIED) Tests for diff hunk filtering & dynamic matrix output
│   ├── test_runners_worker.py             # (MODIFIED) Tests for working_base_ref & surgical staging fallbacks
│   ├── test_runners_aggregate.py          # (MODIFIED) Tests for scoped reporting & summary truncation
│   └── e2e_test_local.py                  # (MODIFIED) End-to-end local simulation updated for GHA mode
└── docs/
    └── guides/
        └── github_actions_guide.md        # (NEW) Developer and administrator onboarding guide for GHA
```

--------------------------------------------------------------------------------

### 1. `Dockerfile`

*   **Why change:** Serves as the universal multi-toolchain base container
    published to GHCR, supporting inheritance for custom BYOI images.
*   **Detailed changes:**

    *   Base on `ubuntu:22.04`.
    *   Install core system utilities: `git`, `curl`, `jq`, `tar`, `gzip`,
        `unzip`, `fuser`, `ca-certificates`, `build-essential` (`gcc`, `g++`,
        `make`).
    *   Install LTS language runtimes: Python 3.11 (`python3-pip`,
        `python3-venv`), Node.js 20 LTS (`npm`, `yarn`, `pnpm`), Go (latest
        stable), OpenJDK 17/21 (`maven`, `gradle`).
    *   Install CodeMender Go CLI (`cm`) into `/usr/local/bin/cm`:

        ```dockerfile
        COPY cm /usr/local/bin/cm
        RUN chmod +x /usr/local/bin/cm
        ```
    *   Copy `codemender_agent/` and `orchestrator.py` into `/opt/codemender`.
    *   Create dedicated Python virtual environment:

        ```dockerfile
        RUN python3 -m venv /opt/codemender/venv && \
            /opt/codemender/venv/bin/pip install --no-cache-dir -r /opt/codemender/requirements.txt
        ```
    *   Set `PYTHONPATH=/opt/codemender` and entrypoint to
        `/opt/codemender/venv/bin/python3 /opt/codemender/orchestrator.py`.

--------------------------------------------------------------------------------

### 2. `.github/workflows/build_runner_image.yml` (New File)

*   **Why create:** Automates building and publishing the multi-toolchain runner
    container to GHCR with layer caching and dual CLI binary sourcing.
*   **Detailed implementation:**

    *   **Triggers:** Push to `main` (when `Dockerfile`, `codemender_agent/**`,
        or `requirements.txt` change), release tags, or manual
        `workflow_dispatch`.
    *   **Inputs for `workflow_dispatch`:**
        *   `cm_version`: Specific version/tag from Artifact Registry (default:
            `"stable"`).
        *   `cm_custom_url`: Optional direct download URL for custom/rollback
            binary.
    *   **Binary Resolution Step:**

        ```bash
        if [ -n "${{ inputs.cm_custom_url }}" ]; then
          echo "Downloading custom cm binary from ${{ inputs.cm_custom_url }}..."
          curl -fsSL -o cm-custom.zip "${{ inputs.cm_custom_url }}"
          unzip -q -o cm-custom.zip cm || cp cm-custom.zip cm
          chmod +x cm
        elif [ -f "./cm" ]; then
          echo "Using local cm binary present in build context."
          chmod +x cm
        else
          VERSION="${{ inputs.cm_version || 'stable' }}"
          echo "Downloading CodeMender CLI version: ${VERSION} from Artifact Registry..."
          URL="https://artifactregistry.googleapis.com/download/v1/projects/cmoc-prod/locations/us/repositories/codemender-cli-production/files/cm%3A${VERSION}%3Acm-linux-amd64.zip:download?alt=media"
          curl -fsSL -o cm-linux-amd64.zip "$URL"
          unzip -q -o cm-linux-amd64.zip cm
          chmod +x cm
        fi
        ```
    *   Uses `docker/setup-buildx-action@v3` and `docker/login-action@v3`
        (logging in to `ghcr.io` with `${{ secrets.GITHUB_TOKEN }}`).
    *   Builds and pushes `ghcr.io/<org>/codemender-runner:latest` and tagged
        versions with Docker layer caching (`type=gha`).

--------------------------------------------------------------------------------

### 3. `.github/workflows/codemender_parallel.yml` (New File)

*   **Why create:** The central Organization Reusable Workflow (`workflow_call`)
    that orchestrates the 3-stage pipeline across calling repositories.
*   **Detailed implementation:**

    1.  **Workflow Permissions:**

        ```yaml
        permissions:
          id-token: write          # Mandatory for GCP Workload Identity Federation
          contents: write          # To checkout code and push remediation branches
          pull-requests: write     # To create Child PRs and post review comments
          security-events: write   # Mandatory to upload SARIF to GitHub Security Tab
          actions: read            # To download workflow run artifacts
        ```
    2.  **Inputs & Secrets Declaration:**

        *   `inputs`:
            *   `runner_image`: Container image to use (default:
                `"ghcr.io/<org>/codemender-runner:latest"`).
            *   `runner_type`: GHA runner label (default: `"ubuntu-latest"`).
            *   `scan_target`: Subdirectory to scan (default: `"."`).
            *   `build_command`: Custom build/test command (optional).
            *   `max_tasks`: Max parallel workers (default: `4` for PRs, `10`
                for Nightly).
            *   `intermediate_artifact_retention_days`: Retention for state
                tarballs (default: `3`).
            *   `report_artifact_retention_days`: Retention for final reports
                (default: `90`).
            *   `upload_sarif`: Upload SARIF to Security Tab (default: `true`).
        *   `secrets`: `gcp_workload_identity_provider`, `gcp_service_account`,
            `gcp_sa_key`, `github_app_id`, `github_app_private_key`,
            `github_token`.
    3.  **Job 1: `scan` (Timeout: 55 mins):**

        *   `runs-on: ${{ inputs.runner_type }}`, `container: { image: ${{
            inputs.runner_image }} }`.
        *   Mints token via `actions/create-github-app-token@v1` (if App secrets
            provided) or uses `secrets.github_token`.
        *   Authenticates with GCP via `google-github-actions/auth@v2` (for
            Vertex AI / Gemini LLM backend calls).
        *   Configures `git config --global --add safe.directory
            "$GITHUB_WORKSPACE"`.
        *   Checks out code pinning `target_sha: ${{
            github.event.pull_request.head.sha || github.sha }}`.
        *   Executes `orchestrator.py` with:
            *   `CODEMENDER_RUN_MODE=scan`
            *   `CODEMENDER_STORAGE_MODE=github_actions`
            *   `CODEMENDER_PR_BASE_REF=${{ github.event.pull_request.base.ref
                || '' }}`
            *   `CODEMENDER_PR_HEAD_REF=${{ github.event.pull_request.head.ref
                || '' }}`
            *   `CODEMENDER_PR_NUMBER=${{ github.event.pull_request.number || ''
                }}`
            *   `CODEMENDER_IS_FORK_PR=${{
                github.event.pull_request.head.repo.full_name !=
                github.repository }}`
        *   Emits `$GITHUB_OUTPUT` parameters (`matrix`, `findings_count`,
            `target_sha`).
        *   Uploads artifact `codemender-base-state` from
            `.codemender_transit/base/` (`retention-days: ${{
            inputs.intermediate_artifact_retention_days }}`).
    4.  **Job 2: `worker` (Timeout: 55 mins):**

        *   `needs: scan`, `if: needs.scan.outputs.findings_count != '0' &&
            needs.scan.outputs.findings_count != ''`.
        *   `strategy: { fail-fast: false, matrix: { worker_index: ${{
            fromJson(needs.scan.outputs.matrix) }} } }`.
        *   `runs-on: ${{ inputs.runner_type }}`, `container: { image: ${{
            inputs.runner_image }} }`.
        *   Configures `git config safe.directory`.
        *   Downloads artifact `codemender-base-state`.
        *   Executes `orchestrator.py` with `CODEMENDER_RUN_MODE=worker`,
            `CODEMENDER_WORKER_INDEX=${{ matrix.worker_index }}`, and PR
            targeting environment variables.
        *   Uploads artifact `worker-shard-${{ matrix.worker_index }}` from
            `.codemender_transit/shards/worker_${{ matrix.worker_index }}/`
            (`retention-days: ${{ inputs.intermediate_artifact_retention_days
            }}`).
    5.  **Job 3: `aggregate` (Timeout: 30 mins):**

        *   `needs: [scan, worker]`, `if: always() && needs.scan.result ==
            'success' && needs.scan.outputs.findings_count != '0' &&
            needs.scan.outputs.findings_count != ''`.
        *   `runs-on: ${{ inputs.runner_type }}`, `container: { image: ${{
            inputs.runner_image }} }`.
        *   Downloads `codemender-base-state` and all available `worker-shard-*`
            artifacts (`pattern: worker-shard-*`, `merge-multiple: true`).
        *   Executes `orchestrator.py` with `CODEMENDER_RUN_MODE=aggregate`.
        *   Uploads `report.sarif` via `github/codeql-action/upload-sarif@v3`
            (`if: inputs.upload_sarif == true`, `continue-on-error: true`).
        *   Uploads `codemender-final-report` artifact (`report.html`,
            `report.json`, `retention-days: ${{
            inputs.report_artifact_retention_days }}`).

--------------------------------------------------------------------------------

### 4. `codemender_agent/config.py`

*   **Why change:** Central configuration reader must parse GHA-specific runtime
    parameters, PR branch references, and PR numbers without breaking GCP
    defaults.
*   **Detailed changes:**

    1.  Add fields to `OrchestratorConfig`:
        *   `storage_mode: str = "gcs"` (defaults to `"github_actions"` if
            `GITHUB_ACTIONS == "true"` or `CODEMENDER_STORAGE_MODE ==
            "github_actions"`).
        *   `is_pr_scan: bool = False` (parsed from `CODEMENDER_IS_PR_SCAN` or
            `GITHUB_EVENT_NAME == "pull_request"`).
        *   `pr_base_ref: Optional[str] = None` (parsed from
            `CODEMENDER_PR_BASE_REF` or `GITHUB_BASE_REF`, representing target
            branch for diff).
        *   `pr_head_ref: Optional[str] = None` (parsed from
            `CODEMENDER_PR_HEAD_REF` or `GITHUB_HEAD_REF`, representing
            developer's feature branch for Child PR targeting).
        *   `is_fork_pr: bool = False` (parsed from `CODEMENDER_IS_FORK_PR`).
        *   `pr_number: Optional[int] = None` (parsed from
            `CODEMENDER_PR_NUMBER`).
        *   `intermediate_retention_days: int = 3`.
    2.  In `inject_codemender_config()`:

        *   Preserve existing sandbox and tool configuration.
        *   Ensure `~/.codemender/config.yaml` explicitly sets:

            ```yaml
            vcs:
              type: git
              commands:
                reset: "git checkout HEAD -- . && git clean -fd"
            ```

--------------------------------------------------------------------------------

### 5. `codemender_agent/storage.py`

*   **Why change:** Implement modular `TransitStorageAdapter` supporting both
    GCS signed URLs and local filesystem GHA artifacts.
*   **Detailed changes:**
    1.  Define abstract `TransitStorageAdapter` with concrete implementations:
        *   `GCSTransitStorageAdapter`: Manages GCS bucket signed
            upload/download URLs.
        *   `GitHubActionsTransitStorageAdapter`: Manages local directory
            structure:
            *   Base: `.codemender_transit/base/`
            *   Worker Shards: `.codemender_transit/shards/worker_${i}/`
    2.  Update helper functions `upload_file_to_gcs` and
        `download_file_from_gcs` to delegate seamlessly to the active adapter.

--------------------------------------------------------------------------------

### 6. `codemender_agent/runners/scan.py`

*   **Why change:** Stage 1 runner must execute full repo scan, apply
    differential line hunk filtering, perform universal deduplication across
    both Nightly and PR modes, and emit GHA outputs.
*   **Detailed changes:**
    1.  In `run_scan_pipeline()`:
        *   Clone repo and execute full `cm find .` scan.
    2.  In `_filter_findings()`:
        *   If `is_pr_scan == True`:
            *   Parse changed line hunks from `git diff -U0
                origin/<pr_base_ref>...HEAD`.
            *   If finding range does not intersect modified lines: mark status
                as `PRE_EXISTING_IGNORED` in `state.db` and exclude from worker
                partitions.
        *   **Universal Deduplication (Nightly & PR scans)**: Check
            `check_remote_branch_exists()` and `is_duplicate_pr()` for each
            finding. If remediation branch or open PR exists (and
            `force_overwrite == False`): mark status as `SKIPPED_DUPLICATE` in
            `state.db` and exclude from worker partitions (burning 0 worker
            compute).
    3.  In `_save_and_upload_state()`:
        *   If `storage_mode == "github_actions"`:
            *   Copy base archive and partition files to
                `.codemender_transit/base/`.
            *   Write to `$GITHUB_OUTPUT`:
                *   `matrix=[0, 1, ..., N-1]` (or `[0]` if 0 findings).
                *   `findings_count=<count>`.
                *   `target_sha=<sha>`.

--------------------------------------------------------------------------------

### 7. `codemender_agent/runners/worker.py`

*   **Why change:** Stage 2 runner must checkout working base ref, perform
    surgical git staging with 3-tier fallback, and handle Child PRs / Fork
    review comments.
*   **Detailed changes:**
    1.  In `_setup_git_and_checkout()`:
        *   Define `working_base_ref = target_sha if (is_pr_scan and target_sha)
            else default_branch`.
        *   Checkout `working_base_ref` and configure `git safe.directory`.
    2.  In `_process_finding()`:
        *   Enforce `force_overwrite = False` if `is_pr_scan == True`.
        *   Perform targeted live check (`git ls-remote` + `GET
            /pulls?head=...`).
        *   Execute `cm verify` and `cm fix`.
        *   **Surgical Staging with 3-Tier Fallback**:
            1.  Query `SELECT edited_files, target_file FROM patches WHERE
                finding_id = ?`.
            2.  If `edited_files` exists and parses to valid file paths: stage
                modified and new files with `git add <file1> <file2>`.
            3.  Else if `target_file` is non-empty: stage `target_file`.
            4.  Else fallback to standard tracked staging: `git add -u` and `git
                add <finding.FilePath>`.
        *   Push branch `codemender/fix-<vuln_type>-<fingerprint>`.
        *   **PR Routing**:
            *   If `is_fork_pr == True`: Call `create_pr_comment()` on Fork PR
                (`pr_number`) with patch diff and local `git apply` block.
            *   If `is_pr_scan == True`: Call `create_pull_request(head=branch,
                base=config.pr_head_ref or default_branch)`.
            *   If Nightly / Manual: Call `create_pull_request(head=branch,
                base=default_branch)`.
    3.  In `run_worker_pipeline()`:
        *   Save mutated database to
            `.codemender_transit/shards/worker_${i}/worker_${i}_state.db`.
        *   Set file permissions (`chmod -R a+rw .codemender_transit`).

--------------------------------------------------------------------------------

### 8. `codemender_agent/runners/aggregate.py`

*   **Why change:** Stage 3 aggregator must merge local DB shards, enforce
    scoped reporting surfaces, and publish SARIF / Summary outputs.
*   **Detailed changes:**
    1.  In `run_aggregate_pipeline()`:
        *   Discover all `worker_*_state.db` files in
            `.codemender_transit/shards/`.
        *   Execute `merge_db()` using clean `UPDATE` queries.
        *   **Scoped Reporting Logic**:
            *   **Nightly Scans on `main`**: Retain `SKIPPED_DUPLICATE` records
                in `state.db` and map them to SARIF suppressions (`suppressions:
                [{ kind: "external", status: "underReview" }]`) so the Security
                Tab alert backlog remains accurate without closing pending
                issues.
            *   **PR Scans**: Purge `PRE_EXISTING_IGNORED` findings before
                running `cm report -f html` and generating PR SARIF so developer
                PR checks focus purely on newly introduced regressions.
        *   Execute `cm report -f html` and `cm report -f sarif`.
        *   Sanitize SARIF paths to be repository-relative.
        *   Render Markdown dashboard and append to `$GITHUB_STEP_SUMMARY`
            (truncated at 1000 KiB buffer limit).

--------------------------------------------------------------------------------

### 9. `codemender_agent/vcs/git.py` & `vcs/github.py`

*   **Why change:** Implement global deterministic branch naming, diff hunk
    extraction, Fork PR review comments, and targeted O(1) deduplication.
*   **Detailed changes:**
    1.  In `vcs/github.py`:
        *   Update `check_remote_branch_exists()` to query exact ref via `git
            ls-remote`.
        *   Update `is_duplicate_pr()` to support targeted query by head branch
            (`GET
            /repos/{owner}/{repo}/pulls?head={owner}:{branch}&state=open`).
        *   Implement `create_pr_comment(token, owner, repo, pr_number, body)`
            to post review comments on Fork PRs.
    2.  In `vcs/git.py`:
        *   Enforce global deterministic naming:
            `codemender/fix-<vuln_type>-<fingerprint>` where fingerprint is
            `SHA256(filePath + vulnType + startLine)[:8]`.
        *   Implement `get_pr_changed_lines(repo_dir, base_ref)`: Parses Unified
            Diff hunks (`git diff -U0 origin/<base_ref>...HEAD`) to extract
            modified line numbers per file.

--------------------------------------------------------------------------------

### 10. `tests/` Test Suite Updates

*   **Why change:** Validate GHA mode, transit storage, Child PR base refs, Fork
    PR comments, surgical staging fallbacks, and PR scoping without requiring
    live cloud infrastructure.
*   **Detailed changes:**
    1.  `tests/test_storage.py`: Unit tests for
        `GitHubActionsTransitStorageAdapter`.
    2.  `tests/test_vcs_github.py`: Unit tests for `create_pr_comment` and
        targeted head-branch duplicate PR checks.
    3.  `tests/test_runners_scan.py`: Tests for diff hunk filtering, universal
        deduplication on `main`, and `$GITHUB_OUTPUT` fallback formatting.
    4.  `tests/test_runners_worker.py`: Tests for `working_base_ref`, surgical
        staging 3-tier fallback, and Child PR `pr_head_ref` targeting.
    5.  `tests/test_runners_aggregate.py`: Tests for scoped SARIF suppressions,
        PR report filtering, and step summary truncation.
    6.  `tests/e2e_test_local.py`: Full 3-stage simulation executing in
        `github_actions` storage mode.

--------------------------------------------------------------------------------

### 11. `docs/guides/github_actions_guide.md` (New File)

*   **Why create:** Comprehensive documentation for enterprise developers and
    security administrators onboarding GitHub Actions.
*   **Contents:**
    *   Setting up Workload Identity Federation (WIF) in GCP and configuring
        GitHub repository permissions.
    *   Setting up the GitHub App for least-privilege token generation.
    *   Example caller workflows for Nightly scheduled scans and labeled PR
        scans.
    *   Creating custom BYOI Docker images for specialized language stacks.
    *   Triage guide for reviewing Child PRs, viewing `$GITHUB_STEP_SUMMARY`,
        and managing GitHub Code Scanning alerts.

--------------------------------------------------------------------------------

## 5. Summary Reference Table

| Dimension         | GCP Cloud Run Mode (Existing)  | GitHub Actions Mode (New)                     |
| :---------------- | :----------------------------- | :-------------------------------------------- |
| **Trigger         | Cloud Scheduler $\rightarrow$  | GitHub Schedule (cron), `workflow_dispatch`,  |
: Mechanism**       : Cloud Workflows JSON payload   : or `pull_request` label                       :
| **Control Plane** | Google Cloud Workflows         | GHA Reusable Workflow                         |
:                   : (`gcp_parallel_workflow.yaml`) : (`.github/workflows/codemender_parallel.yml`) :
| **Worker          | Cloud Run Job Task Array       | GHA Dynamic Matrix (`strategy.matrix:         |
: Scaling**         : (`taskCount\: N`)              : [0..N-1]`, `fail-fast\: false`)               :
| **State Transit** | Google Cloud Storage Bucket +  | GHA Run Artifacts v4                          |
:                   : Signed URLs                    : (`.codemender_transit/shards/worker_${i}/`)   :
| **Container       | Cloud Run Container Instance   | Universal Base (`codemender-runner`) or BYOI  |
: Runtime**         :                                : Custom Image (`runner_image`)                 :
| **Backend Auth**  | Cloud Run Service Account      | Workload Identity Federation (OIDC) or SA Key |
:                   : (built-in)                     : JSON                                          :
| **Token           | Secret Manager                 | `actions/create-github-app-token@v1` or       |
: Lifecycle**       : (`GITHUB_APP_TOKEN`)           : `GITHUB_TOKEN` ($\le 55\text{ min}$ timeout)  :
| **Nightly         | Top-level PRs against `main`   | Top-level PRs against `main`                  |
: Remediation**     : (`codemender/fix-...`)         : (`codemender/fix-...`)                        :
| **Internal PR     | N/A                            | **Child PRs targeting developer's feature     |
: Remediation**     :                                : branch (`pr_head_ref`)**                      :
| **Fork PR         | N/A                            | **Markdown review comments on Fork PR         |
: Remediation**     :                                : (`pr_number`) with diff & apply**             :
| **PR Finding      | N/A                            | **Differential PR Scan (Untouched findings    |
: Scope**           :                                : ignored & omitted from PR reports)**          :
| **Staging         | `git add -u`                   | **Surgical Staging (`patches.edited_files`    |
: Mechanism**       :                                : with 3-tier fallback)**                       :
| **Reporting       | GCS HTML Report (Signed URL in | Scoped SARIF (Security Tab) +                 |
: Surfaces**        : logs)                          : `$GITHUB_STEP_SUMMARY` + GHA Artifact         :
| **Deduplication** | Global deterministic branch    | Global deterministic branch hash (`git        |
:                   : hash                           : ls-remote` + open PR check on all scans)      :
:                   : (`check_remote_branch_exists`) :                                               :

--------------------------------------------------------------------------------

## 6. Future Work & Extensibility

### 1. In-Job Dynamic Token Refresh for Long-Running Stages (> 60 mins)

*   **Context & Baseline:** The V1 architecture strictly enforces a hard timeout
    cap of **$\le 55\text{ minutes}$** per stage, cleanly fitting within
    GitHub's standard 60-minute App Installation Token TTL and keeping runtime
    dependencies lightweight (avoiding `PyJWT`, `cryptography`, and background
    renewal threads).
*   **Extensibility Trigger:** If future enterprise deployments on massive
    monorepos or repositories with exceptionally heavy test suites require
    single scan or worker tasks to run longer than 55 minutes, token refresh
    capabilities can be introduced as an optional enhancement.
*   **Proposed Post-V1 Architecture:**
    1.  **Direct App Key / OIDC Minting:** Allow the orchestrator container to
        receive the GitHub App Private Key secret or authenticate via Workload
        Identity Federation (WIF) OIDC token exchange.
    2.  **In-Python `GitHubTokenManager`**: Introduce a thread-safe token
        provider in `codemender_agent/vcs/github.py` that checks the active
        token's expiration timestamp before every network operation and
        automatically mints a fresh 60-minute installation token via RS256 JWT
        signing when TTL $< 5\text{ minutes}$.
    3.  **Configurable Job Timeouts:** Allow repositories to configure higher
        workflow job timeouts (e.g. `timeout-minutes: 180` or `360`) in
        `.github/workflows/codemender_parallel.yml` without encountering HTTP
        401 Bad Credentials errors.
