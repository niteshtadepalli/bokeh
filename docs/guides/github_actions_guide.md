# CodeMender GitHub Actions Orchestrator Guide

This guide provides step-by-step instructions for onboarding repositories to
**CodeMender Orchestrator** using native **GitHub Actions (GHA)** workflows.

CodeMender runs as a decentralized, 3-stage parallel pipeline inside
containerized GitHub Actions runners, enabling automated vulnerability scanning,
exploit verification, and patch synthesis with **zero external cloud storage
buckets or dedicated compute infrastructure required** (requiring only Vertex AI
API access for AI model reasoning).

--------------------------------------------------------------------------------

## 1. Architecture Overview

CodeMender Orchestrator in GitHub Actions operates as a decentralized, 3-stage
parallel scanning and automated remediation pipeline running completely within
containerized GitHub Actions runners without requiring external cloud storage
buckets or dedicated compute infrastructure:

*   **Universal Container Runner (`codemender-runner`)**: Pre-baked container
    image (`ghcr.io/ilbzzz/codemender-runner:latest` or custom BYOI) containing
    multi-language toolchains (Node.js, Python, Java, Go), build essentials, and
    the `cm` binary in `/usr/local/bin/cm`.
*   **Stage 1: Scan & Partitioning Job (`scan`)**: Ephemeral container job that
    discovers vulnerabilities (`cm find .`), executes differential PR filtering,
    deduplicates against existing branches, slices findings into $N$ worker
    partitions, and uploads the base workspace state.
*   **Stage 2: Parallel Remediation Matrix (`worker`)**: Dynamic parallel matrix
    of container jobs that download the base state, verify exploitability (`cm
    verify`), synthesize patches (`cm fix`), stage edits surgically, push fix
    branches, and open Pull Requests.
*   **Stage 3: Aggregation & Reporting (`aggregate`)**: Merges all worker shard
    databases into a single unified `state.db`, purges ignored findings for PRs,
    generates SARIF reports for the GitHub Security Tab, renders Step Summaries,
    and uploads downloadable HTML/JSON triage reports.
*   **GitHub Transit Artifacts Storage**: Native GitHub Actions artifact storage
    used to pass intermediate states (`codemender-base-state`, `worker-shard-*`)
    and final reports between stages with zero external infrastructure costs.
*   **Authentication & Credential Minting**:
    *   **Google Cloud Vertex AI**: Keyless OIDC authentication via Workload
        Identity Federation (WIF) for Gemini LLM reasoning.
    *   **GitHub REST & Git**: Ephemeral 60-minute tokens minted via GitHub App
        for secure branch creation, PR opening, and SARIF uploads.

### Execution Scope: Isolated Runners vs. Transit Storage

*   **Shared Transit Storage (Run Scope)**:
    *   `codemender-base-state`: Base workspace database and partition slices
        (retained for 3 days by default).
    *   `worker-shard-<index>`: Individual worker database shards and token
        metrics.
    *   `codemender-report-html`, `codemender-report-json`: Downloadable scan
        reports (retained for 90 days).
*   **Isolated Workspace (Per Runner Job)**:
    *   Each matrix task executes in its own isolated container filesystem with
        its own checked-out repository and ephemeral environment.
    *   The `cm` sandbox isolates child process execution (e.g. `npm test`,
        `pytest`) using process namespaces and mount isolation.

```mermaid
graph TD
    subgraph Triggers["Trigger Events"]
        Cron["⏰ Scheduled Cron (Nightly)"]
        PR["🏷️ Labeled PR ('codemender-scan')"]
        Manual["▶️ Manual (workflow_dispatch)"]
    end

    subgraph Stage1["Stage 1: Scan & Partition (scan job)"]
        Find["cm find . (Vulnerability Discovery)"]
        DiffFilter["Differential PR Filtering"]
        Dedup["Universal Remote Deduplication"]
        Slice["Partition Slicing (0..N-1)"]
    end

    subgraph Transit["GitHub Actions Transit Artifacts"]
        BaseArtifact[("📦 codemender-base-state\n(state.db + partition_*.json)")]
        ShardArtifacts[("📦 worker-shard-*\n(worker state.db + token metrics)")]
        ReportArtifacts[("📊 codemender-report-*\n(report.html + report.json)")]
    end

    subgraph Stage2["Stage 2: Parallel Workers (Matrix [0..N-1])"]
        Worker0["Worker 0\n(cm verify & cm fix)"]
        Worker1["Worker 1\n(cm verify & cm fix)"]
        WorkerN["Worker N-1\n(cm verify & cm fix)"]
    end

    subgraph Stage3["Stage 3: Aggregate & Report (aggregate job)"]
        Merge["Merge Shards into Unified state.db"]
        Purge["Differential Purging (PR mode)"]
        GenSARIF["Generate report.sarif"]
        RenderSummary["Render $GITHUB_STEP_SUMMARY"]
    end

    subgraph AuthServices["Authentication & Cloud AI"]
        WIF["GCP Workload Identity Federation (WIF)"]
        VertexAI["Google Cloud Vertex AI (Gemini Models)"]
        GHApp["GitHub App (60-min installation token)"]
    end

    subgraph Outputs["Reporting & Action Surfaces"]
        GHRepo["GitHub Repo: Fix Branches & PRs / Comments"]
        GHSecurity["GitHub Security Tab (SARIF Alerts)"]
        Dashboard["GitHub Actions Step Summary Dashboard"]
    end

    Cron --> Stage1
    PR --> Stage1
    Manual --> Stage1

    Find --> DiffFilter --> Dedup --> Slice
    Slice -->|"Upload Base State"| BaseArtifact

    BaseArtifact -->|"Download Base State"| Worker0
    BaseArtifact -->|"Download Base State"| Worker1
    BaseArtifact -->|"Download Base State"| WorkerN

    Worker0 -->|"Push Branch & PR / Comment"| GHRepo
    Worker1 -->|"Push Branch & PR / Comment"| GHRepo
    WorkerN -->|"Push Branch & PR / Comment"| GHRepo

    Worker0 -->|"Upload Shard 0"| ShardArtifacts
    Worker1 -->|"Upload Shard 1"| ShardArtifacts
    WorkerN -->|"Upload Shard N-1"| ShardArtifacts

    ShardArtifacts -->|"Download Shards"| Merge
    Merge --> Purge --> GenSARIF --> RenderSummary

    GenSARIF -->|"Upload SARIF"| GHSecurity
    RenderSummary -->|"Publish Markdown"| Dashboard
    Merge -->|"Upload Reports"| ReportArtifacts

    WIF -.->|"OIDC Auth"| VertexAI
    VertexAI -.->|"LLM Reasoning"| Stage1
    VertexAI -.->|"LLM Reasoning"| Stage2
    GHApp -.->|"Mint Token"| Stage1
    GHApp -.->|"Mint Token"| Stage2
    GHApp -.->|"Mint Token"| Stage3
```

--------------------------------------------------------------------------------

## 2. Scanning Execution Modes: Scheduled Nightly vs. Pull Request Scans

CodeMender provides tailored execution behaviors depending on whether the scan
is triggered on a recurring schedule or against an active Pull Request:

### Execution Modes Comparison

| Feature       | Scheduled Nightly    | Internal Pull Request  | Fork Pull Request Scan |
:               : Scan                 : Scan                   :                        :
| :------------ | :------------------- | :--------------------- | :--------------------- |
| **Trigger     | `schedule` (cron) /  | `pull_request`         | `pull_request`         |
: Event**       : `workflow_dispatch`  : (`types\: [labeled]`)  : (`types\: [labeled]`)  :
| **Activation  | Cron triggers on     | `codemender-scan`      | `codemender-scan`      |
: Condition**   : default branch       : label on PR            : label on PR            :
| **Target Base | Default branch       | PR Base branch (e.g.   | PR Base branch         |
: Ref**         : (`main` / `master`)  : `main`)                :                        :
| **Scan        | Entire repository    | Differential: PR       | Differential: PR       |
: Scope**       : (`cm find .`)        : changed lines only     : changed lines only     :
| **Legacy Tech | Discovered & triaged | Marked                 | Marked                 |
: Debt**        :                      : `PRE_EXISTING_IGNORED` : `PRE_EXISTING_IGNORED` :
:               :                      : & suppressed           : & suppressed           :
| **Duplicate   | Marked               | Skipped if fix         | Skipped if fix         |
: Handling**    : `SKIPPED_DUPLICATE`  : branch/PR exists       : branch/PR exists       :
:               : (kept in SARIF as    :                        :                        :
:               : `underReview`)       :                        :                        :
| **Remediation | Pushes               | Pushes                 | Posts Markdown review  |
: Action**      : `codemender/fix-...` : `codemender/fix-...` & : comment with diff on   :
:               : & opens PR to `main` : opens Child PR to      : Fork PR                :
:               :                      : developer branch       :                        :
| **Reporting   | Full SARIF alert     | PR-scoped SARIF, Child | PR-scoped SARIF, PR    |
: Output**      : inventory, HTML,     : PR, Step Summary       : Review Comment, Step   :
:               : JSON, Step Summary   :                        : Summary                :

--------------------------------------------------------------------------------

### Mode A: Scheduled (Nightly) Scans

Scheduled scans run on a recurring timer (e.g. weekly on Sunday or nightly at
2:00 AM UTC) to audit the entire repository, maintain security alert
inventories, and remediate technical debt.

1.  **Triggering**:
    *   Configured via `schedule.cron` in `.github/workflows/codemender.yml`.
    *   Can also be triggered on-demand via `workflow_dispatch` through the
        GitHub Web UI or `gh workflow run`.
2.  **Full Repository Scanning & Deduplication**:
    *   Stage 1 discovers all vulnerabilities across the entire codebase (`cm
        find .`).
    *   Performs universal deduplication against remote branches (`git
        ls-remote`) and open PRs. Findings with active remediations are marked
        `SKIPPED_DUPLICATE`.
    *   Slices unhandled findings across dynamic worker partitions.
3.  **Worker Remediation & Mainline PRs**:
    *   Stage 2 workers verify findings (`cm verify`) and synthesize code
        patches (`cm fix`).
    *   Pushes dedicated fix branches (`codemender/fix-...`) and opens **Pull
        Requests targeting the default branch (`main` or `master`)**.
4.  **Final Outputs & Security Tab Inventory**:
    *   Stage 3 aggregates all shards and uploads `report.sarif` to the GitHub
        Security Tab.
    *   Findings marked `SKIPPED_DUPLICATE` are tagged with `underReview`
        suppression metadata, ensuring active security alerts remain open
        without duplicate notifications.
    *   Renders a complete summary dashboard to `$GITHUB_STEP_SUMMARY` and
        uploads downloadable HTML/JSON triage reports.

--------------------------------------------------------------------------------

### Mode B: Pull Request Scans ("Clean as You Code")

Pull Request scans ensure that no new security vulnerabilities are merged into
the codebase, while preventing legacy repository debt from blocking developer
pull requests.

1.  **Triggering & Labeling (`types: [labeled]`)**:
    *   Triggered on `pull_request` events when a PR is **`labeled`** with the
        **`codemender-scan`** label.
    *   **Zero Noise & Skipped Runs**: By configuring `types: [labeled]` (rather
        than `opened` or `synchronize`), GitHub Actions will NOT spawn 1-second
        "Skipped" workflow runs when regular PRs are opened or pushed to. The
        workflow activates solely when the security label is attached.
    *   **Automating in Production**: Teams automate label assignment using
        GitHub Actions (`actions/labeler`), label bots, or workflow conditions
        (see Section 7).
2.  **Differential PR Scanning**:
    *   Stage 1 parses git merge-base diff hunks (`git diff -U0
        origin/main...HEAD`) to identify exact lines modified by the PR.
    *   **Pre-Existing Tech Debt Suppression**: Findings located outside the PR
        changed lines are marked `PRE_EXISTING_IGNORED` and excluded from
        remediation. Only vulnerabilities introduced or modified by the PR are
        assigned to workers.
3.  **Worker Remediation Routing**:
    *   **Internal PRs**: The worker verifies and patches the finding, pushes a
        fix branch, and opens a **Child Pull Request targeting the developer's
        feature branch (`pr_head_ref`)**. The developer can review the patch and
        merge it with 1 click directly into their branch.
    *   **Fork PRs**: For security boundaries, pushing branches to fork remotes
        is prevented. The worker posts a comprehensive **Markdown Review
        Comment** directly on the Fork PR with exploit analysis, unified patch
        diff, and copyable `git apply` instructions.
4.  **Final Outputs**:
    *   Stage 3 purges `PRE_EXISTING_IGNORED` records so PR checks and SARIF
        uploads reflect only new changes introduced by the pull request.
    *   Renders a focused differential table in `$GITHUB_STEP_SUMMARY` showing
        exact PR remediation status.

--------------------------------------------------------------------------------

## 3. Prerequisites

To run CodeMender in GitHub Actions, you need:

1.  **Google Cloud Platform (GCP) Credentials**: Used to authenticate with
    Vertex AI / Gemini LLM APIs for exploit verification and patch generation.
    -   Recommended: **Workload Identity Federation (WIF)** (keyless OIDC
        authentication).
    -   Alternative: **Service Account Key JSON** stored as a GitHub secret.
2.  **GitHub Authentication Token**:
    -   Recommended: **GitHub App** (auto-generates 60-minute installation
        tokens with granular permissions).
    -   Alternative: Standard `GITHUB_TOKEN` or Personal Access Token (PAT).

--------------------------------------------------------------------------------

## 4. Setting Up GCP Workload Identity Federation (WIF)

Workload Identity Federation allows GitHub Actions to securely call Google Cloud
Vertex AI without managing long-lived service account key JSONs.

### Step 1: Create the Workload Identity Pool & Service Account

First, set up your base GCP project variables and create the shared pool and
service account:

```bash
# 1. Base configuration variables
PROJECT_ID="your-gcp-project-id"
PROJECT_NUMBER=$(gcloud projects describe "$PROJECT_ID" --format='value(projectNumber)')
POOL_NAME="github-actions-pool"
PROVIDER_NAME="github-actions-provider"
SA_NAME="codemender-runner-sa"
SA_EMAIL="${SA_NAME}@${PROJECT_ID}.iam.gserviceaccount.com"

# 2. Create the Workload Identity Pool
gcloud iam workload-identity-pools create "$POOL_NAME" \
    --project="$PROJECT_ID" \
    --location="global" \
    --display-name="GitHub Actions Pool"

# 3. Create the dedicated service account
gcloud iam service-accounts create "$SA_NAME" \
    --project="$PROJECT_ID" \
    --display-name="CodeMender Runner Service Account"

# 4. Grant Vertex AI User role for LLM inference
gcloud projects add-iam-policy-binding "$PROJECT_ID" \
    --member="serviceAccount:${SA_EMAIL}" \
    --role="roles/aiplatform.user"
```

--------------------------------------------------------------------------------

### Step 2: Configure Provider & IAM Binding by Scope

Choose the scoping model that matches your setup:

#### Scope Option A: User Scope (All Repos under a Personal GitHub Account)

Allows **all repositories** owned by your personal GitHub username
(`github.com/<username>/*`) to authenticate and share the Workload Identity
Pool.

```bash
GITHUB_USER="your-github-username"

# 1. Create OIDC Provider scoped to user account
gcloud iam workload-identity-pools providers create-oidc "$PROVIDER_NAME" \
    --project="$PROJECT_ID" \
    --location="global" \
    --workload-identity-pool="$POOL_NAME" \
    --display-name="GitHub User Provider" \
    --attribute-mapping="google.subject=assertion.sub,attribute.actor=assertion.actor,attribute.repository=assertion.repository,attribute.repository_owner=assertion.repository_owner" \
    --attribute-condition="assertion.repository_owner == '$GITHUB_USER'" \
    --issuer-uri="https://token.actions.githubusercontent.com"

# 2. Grant impersonation to all repositories owned by the user
gcloud iam service-accounts add-iam-policy-binding "$SA_EMAIL" \
    --project="$PROJECT_ID" \
    --role="roles/iam.workloadIdentityUser" \
    --member="principalSet://iam.googleapis.com/projects/${PROJECT_NUMBER}/locations/global/workloadIdentityPools/${POOL_NAME}/attribute.repository_owner/${GITHUB_USER}"
```

--------------------------------------------------------------------------------

#### Scope Option B: Organization Scope (All Repos under a Company / Organization)

Allows **all repositories** within a GitHub Organization (`github.com/<org>/*`)
to authenticate with a single central Workload Identity configuration.

```bash
GITHUB_ORG="your-github-org"

# 1. Create OIDC Provider scoped to the organization
gcloud iam workload-identity-pools providers create-oidc "$PROVIDER_NAME" \
    --project="$PROJECT_ID" \
    --location="global" \
    --workload-identity-pool="$POOL_NAME" \
    --display-name="GitHub Org Provider" \
    --attribute-mapping="google.subject=assertion.sub,attribute.actor=assertion.actor,attribute.repository=assertion.repository,attribute.repository_owner=assertion.repository_owner" \
    --attribute-condition="assertion.repository_owner == '$GITHUB_ORG'" \
    --issuer-uri="https://token.actions.githubusercontent.com"

# 2. Grant impersonation to all repositories in the organization
gcloud iam service-accounts add-iam-policy-binding "$SA_EMAIL" \
    --project="$PROJECT_ID" \
    --role="roles/iam.workloadIdentityUser" \
    --member="principalSet://iam.googleapis.com/projects/${PROJECT_NUMBER}/locations/global/workloadIdentityPools/${POOL_NAME}/attribute.repository_owner/${GITHUB_ORG}"
```

--------------------------------------------------------------------------------

#### Scope Option C: Specific List of Repositories (1 or More Repos)

Restricts access strictly to a **specific list of named repositories** (e.g.
`your-org/repo-a`, `your-org/repo-b`), enforcing the principle of least
privilege.

```bash
# 1. Create OIDC Provider with a repository whitelist condition
# Example: single repo "your-org/repo-a" or multiple repos in a list
gcloud iam workload-identity-pools providers create-oidc "$PROVIDER_NAME" \
    --project="$PROJECT_ID" \
    --location="global" \
    --workload-identity-pool="$POOL_NAME" \
    --display-name="GitHub Specific Repos Provider" \
    --attribute-mapping="google.subject=assertion.sub,attribute.actor=assertion.actor,attribute.repository=assertion.repository" \
    --attribute-condition="assertion.repository in ['your-org/repo-a', 'your-org/repo-b']" \
    --issuer-uri="https://token.actions.githubusercontent.com"

# 2. Grant impersonation individually to each allowed repository
ALLOWED_REPOS=("your-org/repo-a" "your-org/repo-b")

for REPO in "${ALLOWED_REPOS[@]}"; do
    gcloud iam service-accounts add-iam-policy-binding "$SA_EMAIL" \
        --project="$PROJECT_ID" \
        --role="roles/iam.workloadIdentityUser" \
        --member="principalSet://iam.googleapis.com/projects/${PROJECT_NUMBER}/locations/global/workloadIdentityPools/${POOL_NAME}/attribute.repository/${REPO}"
done
```

--------------------------------------------------------------------------------

## 5. GitHub App Configuration

Creating a dedicated GitHub App ensures that tokens are minted with
least-privilege permissions and that automated commits/PRs are attributed
cleanly to the bot.

### Permissions Required:

-   **Repository Permissions**:
    -   `Contents: Read and write` (to checkout code and push remediation
        branches)
    -   `Pull requests: Read and write` (to open Child PRs and post review
        comments)
    -   `Code scanning alerts: Read and write` (maps to `security-events: write`
        in workflow YAML to upload SARIF reports to GitHub Security Tab)
    -   `Issues: Read and write` (for review comments on Fork PRs)

### Secrets to Configure in GitHub Repository:

-   `GH_APP_ID`: Application ID of your GitHub App.
-   `GH_APP_PRIVATE_KEY`: Private Key (`.pem` format) generated by the GitHub
    App.
-   `GCP_WORKLOAD_IDENTITY_PROVIDER`:
    `projects/<PROJECT_NUMBER>/locations/global/workloadIdentityPools/<POOL>/providers/<PROVIDER>`
-   `GCP_SERVICE_ACCOUNT`: `<SA_NAME>@<PROJECT_ID>.iam.gserviceaccount.com`

--------------------------------------------------------------------------------

## 6. Enabling CodeMender in Any Target Repository

To enable CodeMender security scanning and automated remediation on any target
repository (e.g. `your-org/backend-service` or `username/juice-shop-local`),
follow this 4-step onboarding checklist.

### Understanding the Containerized Runner Model

CodeMender runs as a containerized pipeline inside GitHub Actions. You do not
need to install tools, runtimes, or dependencies directly on host runner
machines:

-   The reusable workflow automatically executes inside the pre-built CodeMender
    base runner image (`ghcr.io/ilbzzz/codemender-runner:latest` or your
    organization's runner).
-   The container image contains pre-baked multi-language toolchains (Python
    3.11, Node.js 20 LTS, Java 17, Go 1.22+), build essentials (`gcc`, `make`,
    `curl`, `git`), and the `cm` Go binary in `/usr/local/bin/cm`.
-   Target repositories only need to configure authentication secrets and add a
    caller workflow file (`.github/workflows/codemender.yml`).

--------------------------------------------------------------------------------

### Step 1: Install the GitHub App on the Target Repository

1.  Navigate to your GitHub App settings or installation dashboard:
    -   **Personal Account**:
        `https://github.com/settings/apps/<your-app-name>/installations`
    -   **Organization**:
        `https://github.com/organizations/<your-org>/settings/apps/<your-app-name>/installations`
2.  Click **Configure** next to the installation entry.
3.  Under **Repository access**:
    -   Select **All repositories** (recommended for org-wide coverage), OR
    -   Select **Only select repositories** and pick your target repository
        (e.g. `juice-shop-local`).
4.  Click **Save**.

> [!NOTE]
> Installing the GitHub App grants CodeMender temporary, least-privilege
> tokens to checkout code, push remediation branches (`codemender/fix-...`),
> open Child PRs, and upload SARIF security alerts.

--------------------------------------------------------------------------------

### Step 2: Configure Actions Secrets

Add the required credentials so the runner can authenticate with GCP Vertex AI
(Gemini LLM APIs) and GitHub:

#### Option A: Organization-Level Secrets (Recommended for Organizations)

If you manage a GitHub Organization, configure these secrets once at
**Organization Settings $\rightarrow$ Secrets and variables $\rightarrow$
Actions**. All repositories will inherit them automatically:

-   `GCP_WORKLOAD_IDENTITY_PROVIDER`
-   `GCP_SERVICE_ACCOUNT`
-   `GH_APP_ID`
-   `GH_APP_PRIVATE_KEY`

#### Option B: Repository-Level Secrets (Personal Accounts or Individual Repos)

In your target repository:

1.  Go to **Settings $\rightarrow$ Secrets and variables $\rightarrow$
    Actions**.
2.  Click **New repository secret** and add the following 4 secrets:
    -   `GCP_WORKLOAD_IDENTITY_PROVIDER`: e.g.
        `projects/123456789/locations/global/workloadIdentityPools/github-actions-pool/providers/github-actions-provider`
    -   `GCP_SERVICE_ACCOUNT`: e.g.
        `codemender-runner-sa@your-gcp-project.iam.gserviceaccount.com`
    -   `GH_APP_ID`: Application ID of your GitHub App
    -   `GH_APP_PRIVATE_KEY`: Complete PEM content of the private key
        (`-----BEGIN RSA PRIVATE KEY...`)

> [!IMPORTANT]
> **Verify GCP WIF Scoping**: Ensure the target repository is
> allowed by your GCP Workload Identity Provider attribute condition: - If
> configured with **User Scope** (`assertion.repository_owner == '<user>'`) or
> **Org Scope** (`assertion.repository_owner == '<org>'`), all repositories
> owned by that user/org are authenticated automatically. - If configured with
> **Specific Repositories Scope** (Section 4, Option C), add the new repository
> to the provider's allowed repository list and IAM policy bindings.

--------------------------------------------------------------------------------

### Step 3: Grant GHCR Package Access to Target Repository

GitHub Actions runners need permission to pull the runner container image
(`ghcr.io/ilbzzz/codemender-runner:latest`).

#### If the Container Package is Public:

-   No package access configuration is needed. Any target repository can pull
    the runner image with zero setup.

#### If the Container Package is Private:

1.  Go to your GitHub profile or organization $\rightarrow$ click the
    **Packages** tab.
2.  Select the **`codemender-runner`** package.
3.  Click **Package settings** (in the right sidebar):
    -   Personal account URL:
        `https://github.com/users/<username>/packages/container/codemender-runner/settings`
    -   Organization URL:
        `https://github.com/orgs/<org>/packages/container/codemender-runner/settings`
4.  Under **Manage Actions access**:
    -   Click **Add repository** $\rightarrow$ select your target repository
        $\rightarrow$ select role **Read**.
5.  In your caller workflow, ensure the top-level permissions block contains
    `packages: read`.

--------------------------------------------------------------------------------

### Step 4: Add Caller Workflow (`.github/workflows/codemender.yml`)

Add `.github/workflows/codemender.yml` to the default branch of the target
repository:

#### Calling Reusable Workflow (Organizations & Public Workflow Repos)

```yaml
name: CodeMender Security Remediation

on:
  # ---------------------------------------------------------------------------
  # 1. Scheduled Recurring Audit (Nightly / Weekly Full Scans)
  # ---------------------------------------------------------------------------
  schedule:
    # Runs weekly on Sunday at 2:00 AM UTC (cron format: minute hour day month day-of-week).
    # Examples:
    #   - '0 2 * * 0' -> Weekly on Sunday at 02:00 UTC
    #   - '0 2 * * *' -> Nightly every day at 02:00 UTC
    #   - '0 0 1 * *' -> Monthly on the 1st at 00:00 UTC
    - cron: '0 2 * * 0'

  # ---------------------------------------------------------------------------
  # 2. Pull Request Scanning ("Clean as You Code")
  # ---------------------------------------------------------------------------
  pull_request:
    # Trigger ONLY when the 'codemender-scan' label is added to a PR.
    # Note: 'types: [labeled]' prevents generating 1-second skipped runs on untagged PRs.
    # To also auto-scan whenever new commits are pushed to an already-labeled PR, use:
    #   types: [labeled, synchronize]
    types: [labeled]

    # Target base branches to protect (e.g. main, master, release/*)
    branches: [main, master]

  # ---------------------------------------------------------------------------
  # 3. Manual On-Demand Scan (GitHub UI 'Run workflow' / gh CLI)
  # ---------------------------------------------------------------------------
  workflow_dispatch:

# -----------------------------------------------------------------------------
# GitHub Actions Permissions (Required by CodeMender Multi-Stage Pipeline)
# -----------------------------------------------------------------------------
permissions:
  id-token: write         # Required: GCP Workload Identity Federation (WIF) OIDC authentication
  contents: write         # Required: Pushing automated 'codemender/fix-...' git branches
  pull-requests: write    # Required: Opening Child Pull Requests or posting review comments
  security-events: write  # Required: Uploading SARIF reports to GitHub Code Scanning (Security Tab)
  actions: read           # Required: Passing intermediate state artifacts between runner jobs
  packages: read          # Required: Pulling runner container image from GitHub Container Registry (GHCR)

jobs:
  remediate:
    # Execution Guard: Run on Schedule, Manual Trigger, or PRs with 'codemender-scan' label
    if: >
      github.event_name == 'schedule' ||
      github.event_name == 'workflow_dispatch' ||
      (github.event_name == 'pull_request' && contains(github.event.pull_request.labels.*.name, 'codemender-scan'))

    # Call the reusable CodeMender orchestration workflow
    uses: ilbzzz/codemender-agent/.github/workflows/codemender_parallel.yml@main

    with:
      # -----------------------------------------------------------------------
      # Build & Test Verification Command (CRITICAL)
      # -----------------------------------------------------------------------
      # Command executed by 'cm fix' to ensure generated patches build and pass tests.
      # Leave empty ('') to auto-detect based on package.json, pom.xml, requirements.txt, etc.
      # Examples:
      #   - Node.js / TypeScript: 'npm test' or 'npm run test:ci' or 'yarn test'
      #   - Python:               'pytest' or 'python -m unittest discover tests'
      #   - Java / Maven:         'mvn clean test'
      #   - Java / Gradle:        './gradlew test'
      #   - Go:                   'go test ./...'
      #   - Rust:                 'cargo test'
      build_command: 'npm test'

      # Target directory path to scan (default: '.' for entire repository root)
      # Subdirectory example: 'src/backend' or multiple paths: 'backend;services/auth'
      scan_target: '.'

      # Maximum number of parallel worker tasks spawned in Stage 2 (default: 10)
      # Controls concurrency limit: e.g. 4 for PR scans, 10-20 for large repository sweeps
      max_tasks: 6

      # Enforce blocking Security Quality Gate on Pull Requests (default: true)
      # If true: Stage 3 exits with code 1 if active vulnerabilities exist on PR diff (blocking merge)
      # If false: Informational only; reports and PRs are created without failing the status check
      fail_on_findings: true

      # Upload SARIF findings to GitHub Security Tab and PR Files Changed annotations (default: true)
      upload_sarif: true

      # Optional AI Model Overrides (leave empty to use CodeMender's up-to-date default Gemini model)
      # Check latest supported models: https://docs.cloud.google.com/gemini-enterprise-agent-platform/codemender#specifying-the-model
      # model: ''         # Unified model across all stages
      # find_model: ''    # Dedicated model for Stage 1 discovery (cm find)
      # verify_model: ''  # Dedicated model for Stage 2 exploit verification (cm verify)
      # fix_model: ''     # Dedicated model for Stage 2 patch generation (cm fix)

    secrets:
      # --- Google Cloud Platform Authentication (Keyless Workload Identity Federation) ---
      gcp_workload_identity_provider: ${{ secrets.GCP_WORKLOAD_IDENTITY_PROVIDER }}
      gcp_service_account: ${{ secrets.GCP_SERVICE_ACCOUNT }}
      # (Alternative: Static GCP Service Account JSON key if not using WIF)
      # gcp_sa_key: ${{ secrets.GCP_SA_KEY }}

      # --- GitHub Authentication (GitHub App Bot) ---
      github_app_id: ${{ secrets.GH_APP_ID }}
      github_app_private_key: ${{ secrets.GH_APP_PRIVATE_KEY }}
      # (Alternative: Custom Personal Access Token if not using GitHub App)
      # custom_github_token: ${{ secrets.CUSTOM_GITHUB_TOKEN }}
```

#### Calling Local Workflow (Personal Accounts with Private Repositories)

> [!NOTE]
> GitHub disallows cross-repository reusable workflow calls between
> private repositories under personal user accounts. For private personal
> repositories, copy `codemender_parallel.yml` into `.github/workflows/` of the
> target repository and call it locally:

```yaml
    uses: ./.github/workflows/codemender_parallel.yml
```

--------------------------------------------------------------------------------

## 7. Example Caller Workflows

Create a workflow file in your repository at `.github/workflows/codemender.yml`.

> [!IMPORTANT]
> **Understanding `${{ inputs.* }}` vs. Fallback Defaults (`||`)**:
> In GitHub Actions, the `${{ inputs.* }}` context is **ONLY populated during
> manual `workflow_dispatch` executions**. When scans are triggered
> automatically by a **Pull Request** (`pull_request`) or a **Scheduled Nightly
> cron job** (`schedule`), `${{ inputs.* }}` evaluates to `null` / empty.
>
> Therefore, **the vast majority of your CI/CD scans will execute using the
> Fallback Default value** defined on the right-hand side of the `||` operator
> (e.g. `${{ inputs.fix_model || 'gemini-2.5-flash' }}` or `${{
> inputs.build_command || 'npm test' }}`).
>
> **Rule of Thumb**: Always set your repository's desired build commands, target
> paths, and model overrides as the **fallback default value** so they apply
> automatically to all PR and Nightly runs!

### Example 1: Production Standard Workflow (Scheduled & Pull Request CI)

This is the recommended turnkey configuration for standard web and backend
repositories. It provides automated weekly security sweeps, pull request
differential scanning, automated test validation, and blocking security quality
checks.

```yaml
name: CodeMender Security Remediation

on:
  # ---------------------------------------------------------------------------
  # 1. Scheduled Recurring Audit (Full Repository Sweeps)
  # ---------------------------------------------------------------------------
  schedule:
    # Runs weekly on Sunday at 2:00 AM UTC.
    # Cron format: minute (0-59) hour (0-23) day-of-month (1-31) month (1-12) day-of-week (0-6, 0=Sunday)
    # Examples:
    #   - '0 2 * * 0'  -> Weekly on Sunday at 02:00 UTC
    #   - '0 2 * * *'  -> Nightly every day at 02:00 UTC
    - cron: '0 2 * * 0'

  # ---------------------------------------------------------------------------
  # 2. Pull Request Scanning ("Clean as You Code")
  # ---------------------------------------------------------------------------
  pull_request:
    # Trigger ONLY when the 'codemender-scan' label is added to a PR.
    # Note: 'types: [labeled]' prevents generating 1-second skipped runs on untagged PRs.
    # To also auto-scan whenever new commits are pushed to an already-labeled PR, use:
    #   types: [labeled, synchronize]
    types: [labeled]

    # Target base branches to guard (e.g. main, master, release/*)
    branches: [main, master]

  # ---------------------------------------------------------------------------
  # 3. Manual On-Demand Trigger (GitHub UI / gh CLI)
  # ---------------------------------------------------------------------------
  workflow_dispatch:

# -----------------------------------------------------------------------------
# GitHub Actions Permissions (Required by CodeMender Multi-Stage Pipeline)
# -----------------------------------------------------------------------------
permissions:
  id-token: write         # Required: GCP Workload Identity Federation (WIF) OIDC authentication
  contents: write         # Required: Pushing automated 'codemender/fix-...' git branches
  pull-requests: write    # Required: Opening Child Pull Requests or posting review comments
  security-events: write  # Required: Uploading SARIF reports to GitHub Code Scanning (Security Tab)
  actions: read           # Required: Passing intermediate state artifacts between runner jobs
  packages: read          # Required: Pulling runner container image from GitHub Container Registry (GHCR)

jobs:
  remediate:
    # Execution Guard: Run on Schedule, Manual Dispatch, or PRs with 'codemender-scan' label
    if: >
      github.event_name == 'schedule' ||
      github.event_name == 'workflow_dispatch' ||
      (github.event_name == 'pull_request' && contains(github.event.pull_request.labels.*.name, 'codemender-scan'))

    # Call the reusable CodeMender orchestration workflow
    uses: ilbzzz/codemender-agent/.github/workflows/codemender_parallel.yml@main

    with:
      # --- Build & Test Verification (CRITICAL) ---
      # Command executed by 'cm fix' to ensure generated patches build and pass unit tests.
      # Leave empty ('') to auto-detect based on package.json, pom.xml, requirements.txt, etc.
      # Examples:
      #   - Node.js / TypeScript: 'npm test' or 'npm run test:ci' or 'yarn test'
      #   - Python:               'pytest' or 'python -m unittest discover tests'
      #   - Java / Maven:         'mvn clean test'
      #   - Java / Gradle:        './gradlew test'
      #   - Go:                   'go test ./...'
      #   - Rust:                 'cargo test'
      build_command: 'npm test'

      # Target directory path to scan (default: '.' for entire repository root)
      scan_target: '.'

      # Maximum number of parallel worker tasks spawned in Stage 2 (default: 10)
      max_tasks: 6

      # Enforce blocking Security Quality Gate on Pull Requests (default: true)
      # If true: Stage 3 exits with code 1 if active vulnerabilities exist on PR diff (blocking merge)
      # If false: Informational only; reports and PRs are created without failing the status check
      fail_on_findings: true

      # Upload SARIF findings to GitHub Security Tab and PR Files Changed annotations (default: true)
      upload_sarif: true

    secrets:
      # --- Google Cloud Platform Authentication (Keyless Workload Identity Federation) ---
      gcp_workload_identity_provider: ${{ secrets.GCP_WORKLOAD_IDENTITY_PROVIDER }}
      gcp_service_account: ${{ secrets.GCP_SERVICE_ACCOUNT }}

      # --- GitHub Authentication (GitHub App Bot) ---
      github_app_id: ${{ secrets.GH_APP_ID }}
      github_app_private_key: ${{ secrets.GH_APP_PRIVATE_KEY }}
```

--------------------------------------------------------------------------------

### Example 2: In-Depth Custom Configuration (All Available Inputs & Interactive UI)

This example demonstrates how to expose all configurable parameters as
interactive UI inputs for `workflow_dispatch`, and documents every single
available workflow input, secret option, and runtime toggle.

```yaml
name: CodeMender Security Remediation (Custom & Interactive)

on:
  schedule:
    - cron: '0 2 * * 0'  # Weekly on Sunday at 2:00 AM UTC
  pull_request:
    types: [labeled, synchronize]  # Triggers on label AND on new commits pushed to labeled PR
    branches: [main, master]
  workflow_dispatch:
    # Interactive UI inputs shown when manually clicking "Run workflow" in GitHub UI
    inputs:
      scan_target:
        description: 'Target subdirectory path(s) to scan (default: . for repository root)'
        required: false
        default: '.'
        type: string
      build_command:
        description: 'Custom build & test verification command (e.g. npm test, pytest, mvn test)'
        required: false
        default: 'npm test'
        type: string
      max_tasks:
        description: 'Maximum number of parallel worker tasks (Stage 2 concurrency)'
        required: false
        default: '6'
        type: string
      fail_on_findings:
        description: 'Enforce blocking Security Quality Gate on PR (fail status check if findings exist)'
        required: false
        default: true
        type: boolean
      model:
        description: 'Default Gemini model across all stages (leave empty for CodeMender default)'
        required: false
        default: ''
        type: string
      find_model:
        description: 'Dedicated model override for Stage 1 discovery (cm find)'
        required: false
        default: ''
        type: string
      verify_model:
        description: 'Dedicated model override for Stage 2 verification (cm verify)'
        required: false
        default: ''
        type: string
      fix_model:
        description: 'Dedicated model override for Stage 2 patch synthesis (cm fix)'
        required: false
        default: ''
        type: string

permissions:
  id-token: write         # Required: GCP Workload Identity Federation (WIF) OIDC token exchange
  contents: write         # Required: Creating 'codemender/fix-...' branches and pushing fixes
  pull-requests: write    # Required: Opening automated Child PRs and posting review comments
  security-events: write  # Required: Uploading SARIF alerts to GitHub Code Scanning
  actions: read           # Required: Transit state artifact management between runner jobs
  packages: read          # Required: Pulling container image from GitHub Container Registry (GHCR)

jobs:
  remediate:
    if: >
      github.event_name == 'schedule' ||
      github.event_name == 'workflow_dispatch' ||
      (github.event_name == 'pull_request' && contains(github.event.pull_request.labels.*.name, 'codemender-scan'))

    uses: ilbzzz/codemender-agent/.github/workflows/codemender_parallel.yml@main

    with:
      # =======================================================================
      # 1. RUNNER & INFRASTRUCTURE CONFIGURATION
      # =======================================================================
      # Container image executing the orchestrator stages.
      # Default: 'ghcr.io/ilbzzz/codemender-runner:latest'
      # You can supply a custom Bring-Your-Own-Image (BYOI) with specialized toolchains (e.g. Java 21, Rust):
      runner_image: 'ghcr.io/ilbzzz/codemender-runner:latest'

      # Runner machine label.
      # Default: 'ubuntu-latest'
      # Options: 'ubuntu-latest', 'ubuntu-22.04', larger runners ('ubuntu-latest-8-cores'), or self-hosted:
      runner_type: 'ubuntu-latest'

      # =======================================================================
      # 2. SCAN SCOPE & BUILD VALIDATION
      # =======================================================================
      # Subdirectory path(s) to scan. Semicolon-separated paths are supported.
      # ⚠️ Fallback Default ('.'): Used on all automated PR and Nightly scans (since inputs.scan_target is null).
      scan_target: ${{ inputs.scan_target || '.' }}

      # Custom build/test command executed by 'cm fix' to validate code patches.
      # ⚠️ Fallback Default ('npm test'): Set your repo's build/test command here so it runs on PR scans!
      # Examples: 'npm test', 'pytest', 'mvn test', 'go test ./...', 'cargo test'
      build_command: ${{ inputs.build_command || 'npm test' }}

      # =======================================================================
      # 3. PARALLELISM & CONCURRENCY
      # =======================================================================
      # Maximum number of parallel worker tasks in Stage 2.
      # ⚠️ Fallback Default (6): Sets concurrency for automated PR scans and Nightly runs.
      max_tasks: ${{ inputs.max_tasks && fromJson(inputs.max_tasks) || 6 }}

      # =======================================================================
      # 4. SECURITY QUALITY GATE & CI POLICIES
      # =======================================================================
      # Enforce blocking Security Quality Gate on Pull Requests.
      # ⚠️ Fallback Default (true): Blocks PR merge with exit code 1 if active vulnerabilities exist on PR diff.
      fail_on_findings: ${{ inputs.fail_on_findings != '' && inputs.fail_on_findings || true }}

      # Enable cm process sandbox isolation using Linux namespaces and mount protection.
      # Default: true (strongly recommended for security isolation during verify/fix).
      sandbox_enabled: true

      # =======================================================================
      # 5. REPORTING, SARIF, & ARTIFACT RETENTION
      # =======================================================================
      # Upload generated SARIF findings to the GitHub Security Tab and PR Files Changed tab.
      # Default: true
      upload_sarif: true

      # Retention period (in days) for intermediate transit artifacts (base state & worker shards).
      # Default: 3 days (automatically pruned by GitHub Actions).
      intermediate_artifact_retention_days: 3

      # Retention period (in days) for final downloadable triage reports (report.html, report.json, report.sarif).
      # Default: 90 days.
      report_artifact_retention_days: 90

      # =======================================================================
      # 6. AI MODEL CONFIGURATION (OPTIONAL)
      # =======================================================================
      # ⚠️ Fallback Defaults: During automated PR and Nightly scans, inputs.* is null.
      # If you want a specific model used on PR scans, specify it as the fallback value after '||'.
      # Leave empty ('') to use CodeMender's up-to-date default Gemini models.
      # Consult documentation for latest supported models:
      # https://docs.cloud.google.com/gemini-enterprise-agent-platform/codemender#specifying-the-model
      model: ${{ inputs.model || '' }}               # Global model override across all stages
      find_model: ${{ inputs.find_model || '' }}     # Dedicated model for Stage 1 discovery (cm find)
      verify_model: ${{ inputs.verify_model || '' }} # Dedicated model for Stage 2 exploit verification (cm verify)
      fix_model: ${{ inputs.fix_model || '' }}       # Dedicated model for Stage 2 patch synthesis (cm fix, e.g. 'gemini-2.5-flash')

    secrets:
      # =======================================================================
      # 7. GCP AUTHENTICATION (CHOOSE WIF OR STATIC SA KEY)
      # =======================================================================
      # Option A: Workload Identity Federation (WIF) - RECOMMENDED (Keyless)
      gcp_workload_identity_provider: ${{ secrets.GCP_WORKLOAD_IDENTITY_PROVIDER }}
      gcp_service_account: ${{ secrets.GCP_SERVICE_ACCOUNT }}

      # Option B: Static GCP Service Account JSON Key (Alternative if WIF is not configured)
      # gcp_sa_key: ${{ secrets.GCP_SA_KEY }}

      # =======================================================================
      # 8. GITHUB AUTHENTICATION (CHOOSE GITHUB APP OR CUSTOM PAT)
      # =======================================================================
      # Option A: GitHub App Credentials - RECOMMENDED (Bypasses branch protection & posts as bot)
      github_app_id: ${{ secrets.GH_APP_ID }}
      github_app_private_key: ${{ secrets.GH_APP_PRIVATE_KEY }}

      # Option B: Custom Personal Access Token (Alternative if GitHub App is not configured)
      # Note: Requires repo, pull_requests:write, security_events:write scopes.
      # custom_github_token: ${{ secrets.CUSTOM_GITHUB_TOKEN }}
```

--------------------------------------------------------------------------------

### Example 3: Monorepo / Multi-Target Scanning Workflow

If your repository contains multiple independent applications or services (e.g.
a Python backend and a TypeScript frontend in a single repository), you can
configure dedicated scan jobs with tailored build and test verification
commands:

```yaml
name: CodeMender Monorepo Security Remediation

on:
  schedule:
    - cron: '0 2 * * 0'  # Weekly on Sunday at 2:00 AM UTC
  pull_request:
    types: [labeled]
    branches: [main, master]
  workflow_dispatch:

permissions:
  id-token: write
  contents: write
  pull-requests: write
  security-events: write
  actions: read
  packages: read

jobs:
  # --- Job 1: Python Backend Service ---
  scan-backend:
    if: >
      github.event_name == 'schedule' ||
      github.event_name == 'workflow_dispatch' ||
      (github.event_name == 'pull_request' && contains(github.event.pull_request.labels.*.name, 'codemender-scan'))
    uses: ilbzzz/codemender-agent/.github/workflows/codemender_parallel.yml@main
    with:
      scan_target: 'services/backend'
      build_command: 'pytest services/backend/tests'
      max_tasks: 4
      fail_on_findings: true
    secrets:
      gcp_workload_identity_provider: ${{ secrets.GCP_WORKLOAD_IDENTITY_PROVIDER }}
      gcp_service_account: ${{ secrets.GCP_SERVICE_ACCOUNT }}
      github_app_id: ${{ secrets.GH_APP_ID }}
      github_app_private_key: ${{ secrets.GH_APP_PRIVATE_KEY }}

  # --- Job 2: TypeScript Frontend Service ---
  scan-frontend:
    if: >
      github.event_name == 'schedule' ||
      github.event_name == 'workflow_dispatch' ||
      (github.event_name == 'pull_request' && contains(github.event.pull_request.labels.*.name, 'codemender-scan'))
    uses: ilbzzz/codemender-agent/.github/workflows/codemender_parallel.yml@main
    with:
      scan_target: 'services/frontend'
      build_command: 'npm test --prefix services/frontend'
      max_tasks: 4
      fail_on_findings: true
    secrets:
      gcp_workload_identity_provider: ${{ secrets.GCP_WORKLOAD_IDENTITY_PROVIDER }}
      gcp_service_account: ${{ secrets.GCP_SERVICE_ACCOUNT }}
      github_app_id: ${{ secrets.GH_APP_ID }}
      github_app_private_key: ${{ secrets.GH_APP_PRIVATE_KEY }}
```

--------------------------------------------------------------------------------

### Manually Triggering On-Demand Scans

Once `.github/workflows/codemender.yml` is committed to your repository's
default branch (`main` or `master`), you can manually trigger security scans at
any time.

#### Option A: Via GitHub Web UI

1.  Go to your target repository on GitHub.
2.  Click the **Actions** tab.
3.  In the left sidebar under *Workflows*, select **CodeMender Security
    Remediation**.
4.  Click the **Run workflow** dropdown on the right.
5.  *(Optional)* Configure run parameters:
    -   **Use workflow from**: Select the branch to scan (e.g. `main` or a
        feature branch).
    -   **scan_target**: Subdirectory path to scan (default: `.` for entire
        repository).
    -   **max_tasks**: Maximum parallel worker tasks (default: `10`).
6.  Click the green **Run workflow** button.

#### Option B: Via GitHub CLI (`gh`)

```bash
# Trigger scan on the default branch
gh workflow run codemender.yml

# Trigger scan on a specific branch with custom inputs
gh workflow run codemender.yml --ref main -f scan_target="." -f max_tasks="10"
```

--------------------------------------------------------------------------------

## 8. Building & Publishing the Standard Runner Base Image

The CodeMender runner base image (`ghcr.io/<org>/codemender-runner:latest`)
contains the pre-baked standard LTS language runtimes (Python 3.11, Node.js 20
LTS, Go 1.22+, OpenJDK 17), build essentials (`gcc`, `g++`, `make`, `git`,
`curl`, `fuser`, `unzip`), the `cm` Go binary in `/usr/local/bin/cm`, and the
isolated orchestrator Python virtual environment in `/opt/codemender/venv`.

You can build and publish this base image to GitHub Container Registry (GHCR)
using either the automated GitHub Actions workflow or locally via the Docker
CLI.

--------------------------------------------------------------------------------

### Method A: Automated CI Workflow (Recommended)

The repository includes a ready-to-use GitHub Actions workflow at
`.github/workflows/build_runner_image.yml` that builds and publishes the image
automatically.

1.  **Automatic Build**: The workflow runs automatically whenever `Dockerfile`,
    `codemender_agent/**`, or `requirements.txt` are pushed to `main`.
2.  **Manual Dispatch**:
    -   Go to your repository on GitHub $\rightarrow$ **Actions** tab.
    -   Select **Build & Publish CodeMender Runner Image** in the left sidebar.
    -   Click **Run workflow** (optionally specify a `cm_version` tag, e.g.
        `stable`).
    -   The workflow will build and publish
        `ghcr.io/<your-org-or-user>/codemender-runner:latest`.

--------------------------------------------------------------------------------

### Method B: Manual Local Build & Push via Docker CLI

If you want to build and push the base image manually from your terminal:

```bash
# 1. Download the CodeMender Go CLI binary into the repository root
URL="https://artifactregistry.googleapis.com/download/v1/projects/cmoc-prod/locations/us/repositories/codemender-cli-production/files/cm%3Astable%3Acm-linux-amd64.zip:download?alt=media"
curl -fsSL -o cm-linux-amd64.zip "$URL"
unzip -q -o cm-linux-amd64.zip cm
chmod +x cm

# 2. Log in to GitHub Container Registry (GHCR)
# Create a Personal Access Token (classic) with 'write:packages' scope
echo "$GITHUB_PAT" | docker login ghcr.io -u "your-github-username" --password-stdin

# 3. Build and tag the base image
IMAGE_NAME="ghcr.io/your-org-or-username/codemender-runner:latest"
docker build -t "$IMAGE_NAME" .

# 4. Push the image to GHCR
docker push "$IMAGE_NAME"
```

--------------------------------------------------------------------------------

### Step 3: Configure Package Visibility in GHCR ⚠️ *(Important)*

By default, newly published packages in GHCR may be private. To allow GitHub
Actions workflows in your repositories to pull the runner image:

1.  Go to your GitHub profile or organization page $\rightarrow$ click the
    **Packages** tab.
2.  Select the **`codemender-runner`** package.
3.  Click **Package settings** (in the right sidebar).
4.  Scroll down to **Danger Zone** $\rightarrow$ **Change package visibility**:
    -   Set to **Public** (recommended for open/shared runner images), OR
    -   Under **Manage Actions access**, grant access to the specific
        repositories running the workflows.

--------------------------------------------------------------------------------

## 9. Bring-Your-Own-Image (BYOI) Custom Toolchains

If your repository requires specialized build tools (such as Rust, PHP, C++,
custom SDKs, or database engines for unit test validation), you can create a
custom runner image that inherits from the standard CodeMender base runner.

### Step 1: Create a Custom Dockerfile

```dockerfile
# Inherit from official CodeMender multi-toolchain base
FROM ghcr.io/your-org/codemender-runner:latest

# Install custom compilers or system packages
USER root
RUN apt-get update && apt-get install -y --no-install-recommends \
    php-cli \
    composer \
    rustc \
    cargo \
    && rm -rf /var/lib/apt/lists/*

# Pre-install global tools
RUN cargo install --locked cargo-audit
```

### Step 2: Build and Publish Image to GHCR

Build and push your image to GitHub Container Registry
(`ghcr.io/your-org/my-custom-runner:latest`).

### Step 3: Pass Custom Image in Reusable Workflow

```yaml
    uses: your-org/codemender-workflows/.github/workflows/codemender_parallel.yml@v1
    with:
      runner_image: ghcr.io/your-org/my-custom-runner:latest
      build_command: 'composer install && cargo test'
    secrets:
      ...
```

--------------------------------------------------------------------------------

## 10. Reviewing & Triaging Remediations

CodeMender provides 4 integrated review surfaces:

### 1. In-PR Child Pull Requests (Internal PRs)

When a vulnerability is discovered on an active internal Pull Request,
CodeMender creates a **Child Pull Request** targeting the developer's feature
branch (`pr_head_ref`).

-   **Zero Merge Collisions**: Developers review the fix in isolation.
-   **1-Click Merge**: Merging the Child PR incorporates the security patch
    directly into the developer's branch.

### 2. Fork Pull Request Review Comments

For Pull Requests originating from repository forks, Child PR creation is
skipped to respect security boundaries. CodeMender posts a Markdown review
comment directly on the Fork PR containing:

-   Exploit analysis and vulnerability summary.
-   Unified patch diff.
-   One-line copyable local `git apply` instructions.

### 3. GitHub Actions Step Summary (`$GITHUB_STEP_SUMMARY`)

Every CI run renders a Markdown summary dashboard directly in the GitHub Actions
run overview, showing:

-   Remediation Overview (Total Discovered, Fixed, Verified, Pre-Existing
    Ignored, Skipped Duplicates).
-   Discovered Findings & Status Table.
-   LLM Token Usage Summary.

### 4. GitHub Security Tab (SARIF Integration)

-   **Nightly Scans on `main`**: All findings (including existing remediations
    marked `SKIPPED_DUPLICATE`) are published to SARIF with `underReview`
    suppression metadata, keeping the repository Security Tab alert inventory
    accurate without prematurely closing open alerts.
-   **PR Scans**: Untouched legacy tech debt (`PRE_EXISTING_IGNORED`) is
    excluded from PR SARIF uploads to ensure developer PR checks remain focused
    strictly on new changes ("Clean as You Code").

--------------------------------------------------------------------------------

## 11. Configuration Reference Table

### Workflow Inputs (`with:`)

| Parameter                              | Type      | Default                                   | Description                                                                                              |
| :------------------------------------- | :-------- | :---------------------------------------- | :------------------------------------------------------------------------------------------------------- |
| `runner_image`                         | `string`  | `ghcr.io/ilbzzz/codemender-runner:latest` | Universal container runner image or Bring-Your-Own-Image (BYOI).                                         |
| `runner_type`                          | `string`  | `ubuntu-latest`                           | GitHub Actions runner machine label.                                                                     |
| `scan_target`                          | `string`  | `.`                                       | Target subdirectory path(s) to scan.                                                                     |
| `build_command`                        | `string`  | `""`                                      | Custom build/test verification command (auto-detected if omitted).                                       |
| `max_tasks`                            | `number`  | `10`                                      | Maximum number of parallel worker tasks in dynamic matrix.                                               |
| `sandbox_enabled`                      | `boolean` | `true`                                    | Enable `cm` sandbox filesystem & network isolation in container.                                         |
| `intermediate_artifact_retention_days` | `number`  | `3`                                       | Retention period (days) for base state and worker shard artifacts.                                       |
| `report_artifact_retention_days`       | `number`  | `90`                                      | Retention period (days) for final HTML, JSON, and SARIF triage reports.                                  |
| `upload_sarif`                         | `boolean` | `true`                                    | Upload generated `report.sarif` findings to GitHub Security Tab.                                         |
| `fail_on_findings`                     | `boolean` | `true` *(on PR)*, `false` *(on Nightly)*  | Exit with non-zero code in Stage 3 if actionable vulnerabilities are detected on PR diff.                |
| `model`                                | `string`  | `""` *(CodeMender default)*               | Global Gemini model override across all stages. Check up-to-date defaults & supported models             |
:                                        :           :                                           : [here](https\://docs.cloud.google.com/gemini-enterprise-agent-platform/codemender#specifying-the-model). :
| `find_model`                           | `string`  | `""` *(inherits `model`)*                 | Dedicated model override for Stage 1 vulnerability discovery (`cm find`).                                |
| `verify_model`                         | `string`  | `""` *(inherits `model`)*                 | Dedicated model override for Stage 2 exploit verification (`cm verify`).                                 |
| `fix_model`                            | `string`  | `""` *(inherits `model`)*                 | Dedicated model override for Stage 2 patch synthesis (`cm fix`).                                         |

--------------------------------------------------------------------------------

### Workflow Secrets (`secrets:`)

| Secret Name                      | Required     | Description                |
| :------------------------------- | :----------- | :------------------------- |
| `gcp_workload_identity_provider` | Yes (if WIF) | Google Cloud Workload      |
:                                  :              : Identity Provider resource :
:                                  :              : URI.                       :
| `gcp_service_account`            | Yes (if WIF) | Google Cloud Service       |
:                                  :              : Account email for Vertex   :
:                                  :              : AI impersonation.          :
| `gcp_sa_key`                     | Optional     | Direct Service Account     |
:                                  :              : JSON key (alternative to   :
:                                  :              : Workload Identity).        :
| `github_app_id`                  | Recommended  | GitHub App ID for          |
:                                  :              : automatic 60-minute        :
:                                  :              : installation token         :
:                                  :              : minting.                   :
| `github_app_private_key`         | Recommended  | GitHub App private key     |
:                                  :              : (`.pem`) for installation  :
:                                  :              : token minting.             :
| `custom_github_token`            | Optional     | Fallback GitHub Token or   |
:                                  :              : PAT (if GitHub App is not  :
:                                  :              : configured).               :

--------------------------------------------------------------------------------

### Advanced AI Model & Execution Flags (`env:`)

| Environment Variable                   | Default           | Description                                                                                              |
| :------------------------------------- | :---------------- | :------------------------------------------------------------------------------------------------------- |
| `CODEMENDER_MODEL`                     | *(CodeMender      | Base Gemini model override used across all discovery, verification, and fix stages. Check up-to-date     |
:                                        : default)*         : defaults & supported models                                                                              :
:                                        :                   : [here](https\://docs.cloud.google.com/gemini-enterprise-agent-platform/codemender#specifying-the-model). :
| `CODEMENDER_FIND_MODEL`                | *(inherits base)* | Dedicated model override for Stage 1 vulnerability discovery (`cm find`).                                |
| `CODEMENDER_VERIFY_MODEL`              | *(inherits base)* | Dedicated model override for Stage 2 exploit PoC generation & verification.                              |
| `CODEMENDER_FIX_MODEL`                 | *(inherits base)* | Dedicated model override for Stage 2 code patch synthesis (`cm fix`).                                    |
| `CODEMENDER_FAIL_ON_FINDINGS`          | `true` *(on PR)*, | Exit with non-zero status in Stage 3 if actionable vulnerabilities are detected on PR.                   |
:                                        : `false` *(on      :                                                                                                          :
:                                        : Nightly)*         :                                                                                                          :
| `CODEMENDER_SKIP_EXPLOIT_VERIFICATION` | `false`           | When `true`, skips dynamic exploit verification and generates patches directly.                          |
| `CODEMENDER_SANDBOX_ENABLED`           | `true`            | Enable `cm` process namespace and filesystem isolation.                                                  |
| `CODEMENDER_SANDBOX_NETWORK_PROFILE`   | `permissive-open` | Sandbox network policy (`permissive-open` or `restricted-local`).                                        |
| `CODEMENDER_FORCE_OVERWRITE`           | `false`           | When `true`, overwrites existing branches and PRs instead of skipping duplicates.                        |
