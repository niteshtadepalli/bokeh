# CodeMender GitHub Actions Orchestrator Guide

This guide provides step-by-step instructions for onboarding repositories to
**CodeMender Orchestrator** using native **GitHub Actions (GHA)** workflows.

CodeMender runs as a decentralized, 3-stage parallel pipeline inside
containerized GitHub Actions runners, enabling automated vulnerability scanning,
exploit verification, and patch synthesis with **zero external cloud storage
buckets or dedicated compute infrastructure required** (requiring only Vertex AI
API access for AI model reasoning).

--------------------------------------------------------------------------------

## 1. Architecture & Execution Model Overview

CodeMender Orchestrator in GitHub Actions operates as a decentralized, 3-stage
parallel scanning and automated remediation pipeline running completely within
containerized GitHub Actions runners:

*   **Universal Container Runner (`codemender-runner`)**: Pre-baked container
    image (`ghcr.io/ilbzzz/codemender-runner:latest` or custom
    Bring-Your-Own-Image) containing multi-language toolchains (Node.js, Python,
    Java, Go), build essentials, and the `cm` Go binary in `/usr/local/bin/cm`.
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

| Feature | Scheduled Nightly Scan | Internal Pull Request Scan | Fork Pull Request Scan |
| :--- | :--- | :--- | :--- |
| **Trigger Event** | `schedule` (cron) / `workflow_dispatch` | `pull_request` (`types: [labeled]`) | `pull_request` (`types: [labeled]`) |
| **Activation Condition** | Cron triggers on default branch | `codemender-scan` label on PR | `codemender-scan` label on PR |
| **Target Base Ref** | Default branch (`main` / `master`) | PR Base branch (e.g. `main`) | PR Base branch |
| **Scan Scope** | Entire repository (`cm find .`) | Differential: PR changed lines only | Differential: PR changed lines only |
| **Legacy Tech Debt** | Discovered & triaged | Marked `PRE_EXISTING_IGNORED` & suppressed | Marked `PRE_EXISTING_IGNORED` & suppressed |
| **Duplicate Handling** | Marked `SKIPPED_DUPLICATE` (kept in SARIF as `underReview`) | Skipped if fix branch/PR exists | Skipped if fix branch/PR exists |
| **Remediation Action** | Pushes `codemender/fix-...` & opens PR to `main` | Pushes `codemender/fix-...` & opens Child PR to developer branch | Posts Markdown review comment with diff on Fork PR |
| **Reporting Output** | Full SARIF alert inventory, HTML, JSON, Step Summary | PR-scoped SARIF, Child PR, Step Summary | PR-scoped SARIF, PR Review Comment, Step Summary |

--------------------------------------------------------------------------------

### Mode A: Scheduled (Nightly) Scans

Scheduled scans run on a recurring timer (e.g. weekly on Sunday or nightly at
2:00 AM UTC) to audit the entire repository, maintain security alert
inventories, and remediate technical debt.

1.  **Triggering**: Configured via `schedule.cron` in
    `.github/workflows/codemender.yml` (or on-demand via `workflow_dispatch`).
2.  **Full Repository Scanning & Self-Healing Deduplication**: Discovers all
    vulnerabilities across the entire codebase (`cm find .`), applying the
    **Hybrid Deduplication & Dead Branch Reaper**:
    *   **Live Branch Check**: If an active open PR exists for the finding's
        branch, it is marked `SKIPPED_DUPLICATE`.
    *   **Dead Branch Reaper**: If a remote branch exists but has **no open PR**
        (due to closed/rejected PRs, merged leftovers, or interrupted test
        runs), Stage 1 autonomously prunes the dead branch via GitHub REST API /
        Git CLI and retains the finding as `ACTIVE` for fresh remediation.
3.  **Worker Remediation & Mainline PRs**: Verifies findings (`cm verify`),
    synthesizes patches (`cm fix`), and opens **Pull Requests targeting the
    default branch (`main`)**. If PR creation fails after pushing to origin,
    workers execute an **atomic rollback** by deleting the remote branch to
    prevent orphan accumulation.
4.  **Final Outputs & Security Tab Inventory**: Uploads complete `report.sarif`
    to the GitHub Security Tab (tagging duplicate findings with `underReview`
    suppression metadata), renders `$GITHUB_STEP_SUMMARY`, and saves
    downloadable HTML/JSON triage reports.

--------------------------------------------------------------------------------

### Mode B: Pull Request Scans ("Clean as You Code")

Pull Request scans ensure that no new security vulnerabilities are merged into
the codebase, while preventing legacy repository debt from blocking developer
pull requests.

1.  **Triggering & Labeling (`types: [labeled]`)**:
    *   Activates on `pull_request` events when the **`codemender-scan`** label
        is attached.
    *   **Zero Noise**: Using `types: [labeled]` avoids spawning 1-second
        "Skipped" runs when regular PRs are opened or pushed to.
2.  **Differential PR Scanning & Deduplication**:
    *   Calculates merge-base diff hunks (`git diff -U0 origin/<base>...HEAD`)
        to isolate modified lines.
    *   **Pre-Existing Tech Debt Suppression**: Findings outside modified lines
        are marked `PRE_EXISTING_IGNORED` and excluded from worker tasks and PR
        reports.
    *   **Autonomous Deduplication**: Employs the JIT Dead Branch Reaper to
        ensure unmerged/closed branch leftovers never suppress genuine
        regressions.
3.  **Worker Remediation Routing**:
    *   **Internal PRs**: Opens a **Child Pull Request targeting the developer's
        feature branch (`pr_head_ref`)**, allowing 1-click merging into the PR.
    *   **Fork PRs**: Respects fork security boundaries by posting an inline
        **Markdown Review Comment** on the PR with exploit analysis, unified
        patch diff, and `git apply` commands.
4.  **Final Outputs**:
    *   Purges `PRE_EXISTING_IGNORED` records so PR status checks and SARIF
        annotations strictly reflect vulnerabilities on the PR diff.
    *   Enforces a blocking Quality Gate (`fail_on_findings: true`) if
        unresolved vulnerabilities remain on the PR diff.

--------------------------------------------------------------------------------

## 3. Phase 1: One-Time Platform & Authentication Setup

Before running CodeMender across target repositories, complete this one-time
setup for your organization or user account:

1.  **GitHub App Creation**: Creates the dedicated bot identity for minting
    ephemeral 60-minute installation tokens with least-privilege permissions.
2.  **GCP Workload Identity Federation (WIF) & PR Trigger Label Setup**:
    Configures keyless OIDC authentication for Vertex AI Gemini LLM access and
    creates the `codemender-scan` PR trigger label (**Path A: Automated
    Terraform** or **Path B: Manual CLI**).
3.  **Secret Injection**: Sets the GitHub Actions secrets across target
    repositories.

--------------------------------------------------------------------------------

### Step 1.1: GitHub App Creation & Permissions

Creating a dedicated GitHub App provides a secure bot identity for CodeMender.
Using a GitHub App offers major advantages over Personal Access Tokens (PATs):

*   **Ephemeral Scoped Credentials**: Tokens are minted on-demand with a
    60-minute lifespan and strictly scoped repository permissions via
    `actions/create-github-app-token`.
*   **Clean Bot Attribution**: Automated branches, Pull Requests, and review
    comments are attributed to your bot identity rather than personal user
    accounts.
*   **High API Rate Limits**: GitHub Apps receive a dedicated 5,000 to 15,000
    requests/hour API rate limit separate from user rate limits.
*   **Branch Protection Compatibility**: GitHub Apps can be explicitly allowed
    to bypass branch protection rules to push automated remediation branches
    without granting broad administrative rights to user accounts.

> [!NOTE]
> **Official GitHub Documentation References**:
> *   [Registering a GitHub App](https://docs.github.com/en/apps/creating-github-apps/registering-a-github-app/registering-a-github-app)
> *   [Authenticating with GitHub Apps](https://docs.github.com/en/apps/creating-github-apps/authenticating-with-a-github-app/about-authentication-with-a-github-app)
> *   [Managing Private Keys for GitHub Apps](https://docs.github.com/en/apps/creating-github-apps/authenticating-with-a-github-app/managing-private-keys-for-github-apps)
> *   [Installing Your Own GitHub App](https://docs.github.com/en/apps/using-github-apps/installing-your-own-github-app)

--------------------------------------------------------------------------------

#### 1. Navigate to GitHub App Registration

*   **Personal User Account**: Go to
    **[GitHub Settings $\rightarrow$ Developer settings $\rightarrow$ GitHub Apps $\rightarrow$ New GitHub App](https://github.com/settings/apps/new)**.
*   **Organization Account**: Go to **Organization Settings $\rightarrow$
    Developer settings $\rightarrow$ GitHub Apps $\rightarrow$ New GitHub App**
    (`https://github.com/organizations/<your-org>/settings/apps/new`).

#### 2. Fill in General Registration Details

1.  **GitHub App name**: Enter a unique descriptive name (e.g. `codemender-bot`
    or `codemender-bot-<org>`).
2.  **Homepage URL**: Enter your repository URL (e.g.
    `https://github.com/cloud-ai-fde/codemender-agent`).
3.  **Webhook**:
    *   **Uncheck "Active"** (CodeMender is triggered directly by GitHub Actions
        runner jobs, so no incoming webhook listener or public webhook URL is
        required).

#### 3. Configure Repository Permissions

Under **Repository permissions**, configure the following minimum access scopes:

| Permission | Access Level | Purpose in CodeMender |
| :--- | :---: | :--- |
| **Contents** | `Read and write` | Checkout repository code, clone base refs, and push automated `codemender/fix-...` branches. |
| **Pull requests** | `Read and write` | Open automated Child PRs on internal feature branches and post review comments on Fork PRs. |
| **Commit statuses** | `Read and write` | Post dedicated `CodeMender / Security Gate` pass/fail status checks on Pull Requests. |
| **Code scanning alerts** | `Read and write` | Upload `report.sarif` findings to the GitHub Security Tab (`security-events: write`). |
| **Issues** | `Read and write` | Post threaded remediation comments and patch diffs on Fork Pull Requests. |

> [!TIP]
> All other **Organization permissions**, **User permissions**, and **Subscribe to events** can remain set to **No access** / unselected.

#### 4. Select Installation Scope & Create App

1.  Under **Where can this GitHub App be installed?**, select:
    *   **Only on this account** (if the App will be used only within your
        organization or personal account).
    *   **Any account** (if you plan to distribute the App across multiple
        separate external organizations).
2.  Click **Create GitHub App**.

#### 5. Save App ID & Generate Private Key

Once created, you will be redirected to the App's **General Settings** page:

1.  **Note the App ID**: Copy the numeric **App ID** displayed at the top under
    **About** (e.g. `123456`). This will be used as `GH_APP_ID`.
2.  **Generate Private Key**:
    *   Scroll down to the **Private keys** section at the bottom of the page.
    *   Click **Generate a private key**.
    *   Your browser will automatically download an RSA private key file in
        `.pem` format (e.g. `codemender-bot.2026-08-31.private-key.pem`).
    *   Keep this file secure! It will be used as `GH_APP_PRIVATE_KEY` in
        [Step 1.3](#step-13-secret-injection).

#### 6. Install the App on Repositories

Before the App can mint tokens for a repository, it must be installed:

1.  In the left sidebar of your App settings, click **Install App**.
2.  Click **Install** next to your organization or user account.
3.  Under **Repository access**, choose:
    *   **All repositories** *(Recommended)*: Allows onboarding any new
        repository without returning to App settings.
    *   **Only select repositories**: Explicitly pick the target repositories
        you want CodeMender to scan.
4.  Click **Install** / **Save**.

--------------------------------------------------------------------------------

### Step 1.2: GCP Workload Identity Federation (WIF) & PR Trigger Label Setup

Choose between **Path A (Automated Terraform)** or **Path B (Manual CLI)** to
provision the GCP WIF infrastructure, set up the `codemender-scan` PR trigger
label, and configure the initial secrets:

```
                  ┌──────────────────────────────────────────────┐
                  │  Choose Setup Method for GCP WIF & GitHub    │
                  └──────────────────────┬───────────────────────┘
                                         │
                 ┌───────────────────────┴───────────────────────┐
                 ▼                                               ▼
    ┌───────────────────────────┐                 ┌───────────────────────────┐
    │ Path A: Automated         │                 │ Path B: Manual            │
    │ Terraform Setup           │                 │ gcloud & gh CLI           │
    │ (Recommended)             │                 │ (Interactive / Ad-Hoc)    │
    └───────────────────────────┘                 └───────────────────────────┘
```

#### Path A: Automated Provisioning via Terraform (Recommended)

The repository provides a complete Terraform module at
[`terraform/gha_wif/`](../../terraform/gha_wif/) that automates:

*   Enabling required GCP APIs (`aiplatform.googleapis.com`,
    `iam.googleapis.com`, `iamcredentials.googleapis.com`).
*   Creating the dedicated Service Account (`codemender-gha-sa`) with
    `roles/aiplatform.user`.
*   Creating the Workload Identity Pool and OIDC Provider with custom scoping.
*   Configuring the `codemender-scan` PR trigger label on target repositories.
*   Configuring the 3 non-sensitive GitHub Actions secrets
    (`GCP_WORKLOAD_IDENTITY_PROVIDER`, `GCP_SERVICE_ACCOUNT`, `GH_APP_ID`).

##### 1. Create and Configure `terraform.tfvars`

Navigate to the Terraform directory and create your `terraform.tfvars` file by
running the following command:

```bash
cd terraform/gha_wif

cat << 'EOF' > terraform.tfvars
# =============================================================================
# CodeMender GitHub Actions WIF & GitHub Configuration
# =============================================================================

# 1. Google Cloud Project Configuration (REQUIRED)
# The GCP Project ID hosting Vertex AI where Gemini LLM APIs will be queried.
gcp_project_id = "your-gcp-project-id"

# The GCP region for resource deployment (default: "global")
gcp_region     = "global"


# 2. GitHub Organization / User Configuration (REQUIRED)
# GitHub organization name (e.g. "my-org") or username (e.g. "octocat").
github_owner = "your-github-org-or-username"

# Numeric App ID from Step 1.1 (from GitHub App settings page, e.g. "123456").
# Configured automatically as the GH_APP_ID secret in target repositories.
github_app_id = "123456"


# 3. Workload Identity Federation (WIF) Scoping Type (REQUIRED)
# Choose one of:
#   - "org"          : Authorizes ALL repositories under github_owner organization (Recommended for orgs)
#   - "user"         : Authorizes ALL repositories under github_owner user account (For personal accounts)
#   - "repositories" : Restricts GCP WIF access strictly to repositories listed in 'wif_allowed_repositories'
github_scope_type = "org"


# 4. Target GitHub Repositories for Actions Secrets & PR Label Setup
# List of repository names where Terraform will configure GitHub Secrets
# (GCP_WORKLOAD_IDENTITY_PROVIDER, GCP_SERVICE_ACCOUNT, GH_APP_ID) and the
# 'codemender-scan' PR trigger label.
#
# NOTE:
#   - When github_scope_type is "org" or "user": Set this list to the repos you are onboarding.
#   - When github_scope_type is "repositories": You can leave this empty ([]), and Terraform
#     will automatically use 'wif_allowed_repositories' below.
#
# Examples: ["backend-service", "frontend-app", "juice-shop-local"]
target_github_repositories = [
  "backend-service",
  "frontend-app"
]


# =============================================================================
# OPTIONAL ADVANCED CONFIGURATIONS (Uncomment if customizing)
# =============================================================================

# -----------------------------------------------------------------------------
# WIF Allowed Repositories (REQUIRED ONLY IF github_scope_type = "repositories")
# -----------------------------------------------------------------------------
# Defines the GCP IAM security perimeter (which repositories are allowed to
# authenticate with GCP Vertex AI). Must be formatted as "owner/repo".
# (Ignored when github_scope_type is "org" or "user").
#
# wif_allowed_repositories = [
#   "your-org/backend-service",
#   "your-org/frontend-app"
# ]

# Custom Service Account ID (default: "codemender-gha-sa")
# gcp_service_account_id = "codemender-gha-sa"

# Custom Workload Identity Pool ID (default: "codemender-gha-pool")
# gcp_wif_pool_id = "codemender-gha-pool"

# Custom Workload Identity Provider ID (default: "codemender-gha-provider")
# gcp_wif_provider_id = "codemender-gha-provider"
EOF
```

> [!TIP]
> **Understanding `target_github_repositories` vs.
> `wif_allowed_repositories`**: * **`target_github_repositories` (GitHub API)**:
> Tells Terraform which repositories should receive the GitHub Actions Secrets
> and the `codemender-scan` label. * **`wif_allowed_repositories` (GCP IAM /
> WIF)**: Tells GCP IAM which repositories are cryptographically authorized to
> assume the GCP Service Account via OIDC.
>
> **Rules of Thumb**: 1. **Org-Wide Scope (`github_scope_type = "org"`)**: Set
> `target_github_repositories = ["repo-1", "repo-2"]`. Leave
> `wif_allowed_repositories` commented out (GCP automatically trusts the whole
> org). 2. **Repository List Scope (`github_scope_type = "repositories"`)**: Set
> `wif_allowed_repositories = ["org/repo-1", "org/repo-2"]`. You can leave
> `target_github_repositories = []` (Terraform automatically applies to the same
> list).

##### 2. Apply Terraform

Authenticate with GCP and GitHub, then apply:

*   **Option 1: Running in GCP Cloud Shell (Fastest & Recommended)**: In Cloud
    Shell, `gcloud` is already logged in. If you are logged into GitHub CLI (`gh
    auth login`), export your active token directly:

    ```bash
    # Set active GCP project in Cloud Shell
    gcloud config set project "your-gcp-project-id"

    # Export GitHub token directly from active GitHub CLI session
    export GITHUB_TOKEN=$(gh auth token)

    # Initialize and apply
    terraform init
    terraform apply
    ```

*   **Option 2: Running in Local Workstation / Terminal**:

    ```bash
    # 1. Log in to GCP Application Default Credentials
    gcloud auth application-default login

    # 2. Export your GitHub Personal Access Token (with repo admin access)
    export GITHUB_TOKEN="ghp_yourManagementTokenWithAdminRepoAccess"

    # 3. Initialize and apply
    terraform init
    terraform apply
    ```

> [!NOTE]
> Terraform automatically populates `GCP_WORKLOAD_IDENTITY_PROVIDER`,
> `GCP_SERVICE_ACCOUNT`, `GH_APP_ID`, and creates the `codemender-scan` label on
> all specified `target_github_repositories`. Proceed to
> [Step 1.3](#step-13-secret-injection) for the single decoupled step to inject
> `GH_APP_PRIVATE_KEY`.

--------------------------------------------------------------------------------

#### Path B: Manual Provisioning via CLI (`gcloud` & `gh`)

If you prefer to configure resources manually without Terraform:

##### 1. Create Workload Identity Pool, Service Account & IAM Role

```bash
# 1. Base configuration variables
PROJECT_ID="your-gcp-project-id"
PROJECT_NUMBER=$(gcloud projects describe "$PROJECT_ID" --format='value(projectNumber)')
POOL_NAME="codemender-gha-pool"
PROVIDER_NAME="codemender-gha-provider"
SA_NAME="codemender-gha-sa"
SA_EMAIL="${SA_NAME}@${PROJECT_ID}.iam.gserviceaccount.com"

# 2. Enable required APIs
gcloud services enable aiplatform.googleapis.com iam.googleapis.com iamcredentials.googleapis.com --project="$PROJECT_ID"

# 3. Create the Workload Identity Pool
gcloud iam workload-identity-pools create "$POOL_NAME" \
    --project="$PROJECT_ID" \
    --location="global" \
    --display-name="CodeMender GitHub Actions Pool"

# 4. Create the dedicated Service Account
gcloud iam service-accounts create "$SA_NAME" \
    --project="$PROJECT_ID" \
    --display-name="CodeMender Runner Service Account"

# 5. Grant Vertex AI User role for LLM inference
gcloud projects add-iam-policy-binding "$PROJECT_ID" \
    --member="serviceAccount:${SA_EMAIL}" \
    --role="roles/aiplatform.user"
```

##### 2. Configure OIDC Provider & IAM Binding by Scope

Choose the scoping model that matches your setup:

*   **Option 1: Organization Scope (All Repos under an Organization)**:

    ```bash
    GITHUB_ORG="your-github-org"

    gcloud iam workload-identity-pools providers create-oidc "$PROVIDER_NAME" \
        --project="$PROJECT_ID" \
        --location="global" \
        --workload-identity-pool="$POOL_NAME" \
        --display-name="GitHub Org Provider" \
        --attribute-mapping="google.subject=assertion.sub,attribute.actor=assertion.actor,attribute.repository=assertion.repository,attribute.repository_owner=assertion.repository_owner" \
        --attribute-condition="assertion.repository_owner == '$GITHUB_ORG'" \
        --issuer-uri="https://token.actions.githubusercontent.com"

    gcloud iam service-accounts add-iam-policy-binding "$SA_EMAIL" \
        --project="$PROJECT_ID" \
        --role="roles/iam.workloadIdentityUser" \
        --member="principalSet://iam.googleapis.com/projects/${PROJECT_NUMBER}/locations/global/workloadIdentityPools/${POOL_NAME}/attribute.repository_owner/${GITHUB_ORG}"
    ```

*   **Option 2: User Scope (All Repos under a Personal Account)**:

    ```bash
    GITHUB_USER="your-github-username"

    gcloud iam workload-identity-pools providers create-oidc "$PROVIDER_NAME" \
        --project="$PROJECT_ID" \
        --location="global" \
        --workload-identity-pool="$POOL_NAME" \
        --display-name="GitHub User Provider" \
        --attribute-mapping="google.subject=assertion.sub,attribute.actor=assertion.actor,attribute.repository=assertion.repository,attribute.repository_owner=assertion.repository_owner" \
        --attribute-condition="assertion.repository_owner == '$GITHUB_USER'" \
        --issuer-uri="https://token.actions.githubusercontent.com"

    gcloud iam service-accounts add-iam-policy-binding "$SA_EMAIL" \
        --project="$PROJECT_ID" \
        --role="roles/iam.workloadIdentityUser" \
        --member="principalSet://iam.googleapis.com/projects/${PROJECT_NUMBER}/locations/global/workloadIdentityPools/${POOL_NAME}/attribute.repository_owner/${GITHUB_USER}"
    ```

*   **Option 3: Specific List of Named Repositories**:

    ```bash
    gcloud iam workload-identity-pools providers create-oidc "$PROVIDER_NAME" \
        --project="$PROJECT_ID" \
        --location="global" \
        --workload-identity-pool="$POOL_NAME" \
        --display-name="GitHub Specific Repos Provider" \
        --attribute-mapping="google.subject=assertion.sub,attribute.actor=assertion.actor,attribute.repository=assertion.repository" \
        --attribute-condition="assertion.repository in ['your-org/repo-a', 'your-org/repo-b']" \
        --issuer-uri="https://token.actions.githubusercontent.com"

    ALLOWED_REPOS=("your-org/repo-a" "your-org/repo-b")
    for REPO in "${ALLOWED_REPOS[@]}"; do
        gcloud iam service-accounts add-iam-policy-binding "$SA_EMAIL" \
            --project="$PROJECT_ID" \
            --role="roles/iam.workloadIdentityUser" \
            --member="principalSet://iam.googleapis.com/projects/${PROJECT_NUMBER}/locations/global/workloadIdentityPools/${POOL_NAME}/attribute.repository/${REPO}"
    done
    ```

##### 3. Create PR Trigger Label via GitHub CLI

Create the `codemender-scan` label on your target repository:

```bash
gh label create "codemender-scan" \
    --repo "your-org/your-repo" \
    --description "Triggers CodeMender automated security scan and remediation" \
    --color "0E8A16" \
    --force
```

--------------------------------------------------------------------------------

### Step 1.3: Secret Injection

To keep your private key strictly out of version control, plaintext `.tfvars`
files, and Terraform state files, secrets are configured as follows:

#### If You Used Path A (Terraform Automation):

Terraform has already provisioned the GCP WIF infrastructure and populated
`GCP_WORKLOAD_IDENTITY_PROVIDER`, `GCP_SERVICE_ACCOUNT`, `GH_APP_ID`, and the
`codemender-scan` label.

Inject the private key directly via GitHub CLI:

```bash
# Inject the private key into your target repository
gh secret set GH_APP_PRIVATE_KEY --repo "your-org/your-repo" < path/to/your-app-private-key.pem

# Or inject as an Organization Secret (accessible across all repositories)
gh secret set GH_APP_PRIVATE_KEY --org "your-org" --visibility all < path/to/your-app-private-key.pem
```

#### If You Used Path B (Manual CLI):

Set all 4 secrets via GitHub CLI:

```bash
TARGET_REPO="your-org/your-repo"
WIF_PROVIDER_URI="projects/${PROJECT_NUMBER}/locations/global/workloadIdentityPools/${POOL_NAME}/providers/${PROVIDER_NAME}"

# Configure all 4 Actions secrets
gh secret set GCP_WORKLOAD_IDENTITY_PROVIDER --repo "$TARGET_REPO" --body "$WIF_PROVIDER_URI"
gh secret set GCP_SERVICE_ACCOUNT --repo "$TARGET_REPO" --body "$SA_EMAIL"
gh secret set GH_APP_ID --repo "$TARGET_REPO" --body "123456"
gh secret set GH_APP_PRIVATE_KEY --repo "$TARGET_REPO" < path/to/your-app-private-key.pem
```

> [!NOTE]
> **Enterprise Alternative for Large Teams (GCP Secret Manager +
> Terraform Data Source)**: If your organization manages hundreds of
> repositories with central Terraform CI/CD, you can store the private key in
> Google Cloud Secret Manager (`codemender-github-app-key`) and read it in
> Terraform using `data.google_secret_manager_secret_version` to populate GitHub
> secrets automatically without manual CLI steps.

--------------------------------------------------------------------------------

## 4. Phase 2: Target Repository Onboarding

Once Phase 1 is complete for your organization or user account, onboarding any
new target repository requires only a 3-step checklist:

```
┌──────────────────────────────────────────────────────────────────────────────┐
│                  Phase 2: Target Repository Onboarding Checklist             │
├──────────────────────────────────────────────────────────────────────────────┤
│  [ ] Step 2.1: Install GitHub App on Target Repository                       │
│  [ ] Step 2.2: Grant GHCR Package Access (if runner image is private)         │
│  [ ] Step 2.3: Add Caller Workflow (.github/workflows/codemender.yml)        │
└──────────────────────────────────────────────────────────────────────────────┘
```

### Step 2.1: Install GitHub App on Target Repository

1.  Navigate to your GitHub App installations dashboard:
    *   **Personal Account**:
        `https://github.com/settings/apps/<your-app-name>/installations`
    *   **Organization**:
        `https://github.com/organizations/<your-org>/settings/apps/<your-app-name>/installations`
2.  Click **Configure** next to the installation entry.
3.  Under **Repository access**, select **All repositories** (recommended) or
    choose the specific repository.
4.  Click **Save**.

### Step 2.2: Grant GHCR Package Access (for Private Images)

GitHub Actions runners need permission to pull the runner container image.

*   **Public Package (`ghcr.io/ilbzzz/codemender-runner:latest`)**: No action
    needed. Any repository can pull the public image immediately.
*   **Private Package**:
    1.  Go to your GitHub profile or organization $\rightarrow$ **Packages** tab
        $\rightarrow$ select **`codemender-runner`**.
    2.  Click **Package settings** (sidebar) $\rightarrow$ scroll to **Manage
        Actions access**.
    3.  Click **Add repository** $\rightarrow$ select your target repository
        $\rightarrow$ assign role **Read**.
    4.  Ensure your caller workflow permissions block includes `packages: read`.

### Step 2.3: Add Caller Workflow (`.github/workflows/codemender.yml`)

Add a workflow file at `.github/workflows/codemender.yml` on the default branch
(`main` or `master`).

--------------------------------------------------------------------------------

#### ⚠️ CRITICAL SETUP DISTINCTION: Organization vs. Personal Private Repositories

Depending on whether your target repository belongs to an **Organization** or a
**Personal User Account**, choose the matching setup below:

```
┌──────────────────────────────────────────────────────────────────────────────────────────────────┐
│                                CHOOSE YOUR REPOSITORY SETUP SCENARIO                             │
├─────────────────────────────────────────────────┬────────────────────────────────────────────────┤
│ Scenario A: Organization / Public Repository    │ Scenario B: Private Repo in Personal Account   │
│ (Standard Turnkey Reusable Workflow)            │ (MOST COMMON FOR TESTERS & INDIVIDUAL USERS)   │
├─────────────────────────────────────────────────┼────────────────────────────────────────────────┤
│ • Repositories in the same GitHub Organization   │ • Testing in personal private repositories     │
│ • Or any Public repository                      │ • Cross-repo reusable calls BLOCKED by GitHub  │
│                                                 │                                                │
│ Action:                                         │ Action (2 Steps Required):                     │
│ Use standard remote reusable workflow path:     │ 1. COPY 'codemender_parallel.yml' into target │
│   uses: org/codemender-agent/.../parallel.yml   │    repo's '.github/workflows/' directory.     │
│                                                 │ 2. Call local workflow in 'codemender.yml':    │
│                                                 │    uses: ./.github/workflows/parallel.yml      │
└─────────────────────────────────────────────────┴────────────────────────────────────────────────┘
```

> [!CAUTION]
> **Why Copying `codemender_parallel.yml` is Mandatory for Personal
> Private Repositories**: GitHub Actions enforces a strict platform-level
> security boundary: **cross-repository reusable workflow calls (`uses:
> <user>/<repo>/...`) between private repositories are strictly prohibited under
> personal user accounts**.
>
> If you attempt to call `uses: your-user/codemender-agent/...` from another
> private personal repository, GitHub will immediately fail with:
>
> ```text
> Error: .github/workflows/codemender.yml: action not found / repository not found or access denied
> ```
>
> 💡 **Important Note on Docker Image Visibility**: Even if your runner container
> image (`ghcr.io/...`) is **Public**, GitHub Actions still requires the **YAML
> workflow file itself** (`codemender_parallel.yml`) to be present inside the
> target private repository.

--------------------------------------------------------------------------------

#### Setup Instructions by Scenario:

*   **Scenario A: Organization Account / Public Repositories**: In
    `.github/workflows/codemender.yml`, reference the centralized reusable
    workflow:

    ```yaml
        uses: your-org/codemender-agent/.github/workflows/codemender_parallel.yml@main
        with:
          runner_image: 'ghcr.io/your-org/codemender-runner:latest'
    ```

*   **Scenario B: Private Repositories under Personal Accounts (Testers &
    Personal Repos)**:

    1.  **Copy the Reusable Orchestrator Workflow**: Copy
        `codemender_parallel.yml` from this repository directly into the target
        repository:

        ```text
        your-target-repo/
        ├── .github/
        │   └── workflows/
        │       ├── codemender_parallel.yml  <-- (COPIED HERE)
        │       └── codemender.yml           <-- (CALLER WORKFLOW)
        ```
    2.  **Configure Local Call in `.github/workflows/codemender.yml`**:

        ```yaml
            uses: ./.github/workflows/codemender_parallel.yml
            with:
              # Point to your personal or public runner image on GHCR:
              runner_image: 'ghcr.io/ilbzzz/codemender-runner:latest'
              build_command: 'npm test'
            secrets:
              gcp_workload_identity_provider: ${{ secrets.GCP_WORKLOAD_IDENTITY_PROVIDER }}
              gcp_service_account: ${{ secrets.GCP_SERVICE_ACCOUNT }}
              github_app_id: ${{ secrets.GH_APP_ID }}
              github_app_private_key: ${{ secrets.GH_APP_PRIVATE_KEY }}
        ```

--------------------------------------------------------------------------------

## 5. Caller Workflow Examples & Customization

Create `.github/workflows/codemender.yml` in your target repository:

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

> [!TIP]
> **Customizing Pull Request Triggers**: By default, CodeMender triggers
> on-demand when the `codemender-scan` label is added to a PR (`types:
> [labeled]`). To configure automatic label application, alternative trigger
> patterns (such as scanning on commit push or scanning every PR), or file path
> filtering, see
> [Section 8: Pull Request Trigger Reference & Patterns](#8-appendix-pull-request-trigger-reference--patterns).

### Example 1: Production Standard Workflow (Scheduled & Pull Request CI)

This is the recommended turnkey configuration for standard web and backend
repositories:

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
    # Trigger on-demand when 'codemender-scan' label is added to a PR.
    # (To configure scans on push, scan-every-PR, or path filters, see Section 8).
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

    # -------------------------------------------------------------------------
    # Reusable Orchestrator Workflow Call:
    # Option A (Org Account / Public Repo): uses: your-org/codemender-agent/.github/workflows/codemender_parallel.yml@main
    # Option B (Personal Private Repo / Testers): Copy 'codemender_parallel.yml' to .github/workflows/ and call locally:
    # -------------------------------------------------------------------------
    uses: ./.github/workflows/codemender_parallel.yml
    # uses: your-org/codemender-agent/.github/workflows/codemender_parallel.yml@main

    with:
      # Universal or Personal Runner Container Image (GHCR):
      runner_image: 'ghcr.io/ilbzzz/codemender-runner:latest'
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
interactive UI inputs for `workflow_dispatch`, and documents every available
workflow input, secret option, and runtime toggle:

```yaml
name: CodeMender Security Remediation (Custom & Interactive)

on:
  schedule:
    - cron: '0 2 * * 0'  # Weekly on Sunday at 2:00 AM UTC
  pull_request:
    # Pattern B: Triggers when 'codemender-scan' label is attached AND re-scans on new commits pushed to the PR
    types: [labeled, synchronize]
    branches: [main, master]
    # paths-ignore: ['docs/**', '**.md', '.github/**']
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
      skip_exploit_verification:
        description: 'Skip dynamic exploit verification (cm verify --skip-exploit-verification)'
        required: false
        default: false
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
      # 1. RUNNER & INFRASTRUCTURE CONFIGURATION (OPTIONAL)
      # =======================================================================
      # Optional: Specify a Bring-Your-Own-Image (BYOI) if your build requires specialized toolchains.
      # Default: 'ghcr.io/ilbzzz/codemender-runner:latest'
      runner_image: 'ghcr.io/ilbzzz/codemender-runner:latest'

      # Optional: Custom runner sizing (e.g. 'ubuntu-latest-8-cores') or self-hosted runner labels.
      # Default: 'ubuntu-latest'
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

      # Optional: Skip dynamic exploit verification during Stage 2 (cm verify --skip-exploit-verification).
      # ⚠️ Fallback Default (false): Generates & verifies PoC exploits dynamically before synthesis.
      skip_exploit_verification: ${{ inputs.skip_exploit_verification || false }}

      # =======================================================================
      # 3. PARALLELISM & CONCURRENCY
      # =======================================================================
      # Maximum number of parallel worker tasks in Stage 2.
      # ⚠️ Fallback Default (6): Sets concurrency for automated PR scans and Nightly runs.
      max_tasks: ${{ inputs.max_tasks && fromJson(inputs.max_tasks) || 6 }}

      # =======================================================================
      # 4. AI MODEL CONFIGURATION (OPTIONAL)
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
      # 5. GCP AUTHENTICATION (CHOOSE WIF OR STATIC SA KEY)
      # =======================================================================
      # Option A: Workload Identity Federation (WIF) - RECOMMENDED (Keyless)
      gcp_workload_identity_provider: ${{ secrets.GCP_WORKLOAD_IDENTITY_PROVIDER }}
      gcp_service_account: ${{ secrets.GCP_SERVICE_ACCOUNT }}

      # Option B: Static GCP Service Account JSON Key (Alternative if WIF is not configured)
      # gcp_sa_key: ${{ secrets.GCP_SA_KEY }}

      # =======================================================================
      # 6. GITHUB AUTHENTICATION (CHOOSE GITHUB APP OR CUSTOM PAT)
      # =======================================================================
      # Option A: GitHub App Credentials - RECOMMENDED (Bypasses branch protection & posts as bot)
      github_app_id: ${{ secrets.GH_APP_ID }}
      github_app_private_key: ${{ secrets.GH_APP_PRIVATE_KEY }}

      # Option B: Custom Personal Access Token (Alternative if GitHub App is not configured)
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
    secrets:
      gcp_workload_identity_provider: ${{ secrets.GCP_WORKLOAD_IDENTITY_PROVIDER }}
      gcp_service_account: ${{ secrets.GCP_SERVICE_ACCOUNT }}
      github_app_id: ${{ secrets.GH_APP_ID }}
      github_app_private_key: ${{ secrets.GH_APP_PRIVATE_KEY }}
```

--------------------------------------------------------------------------------

## 6. Triggering, Verification & Review Surfaces

### How to Trigger Scans

1.  **Pull Request Scan**: Add the **`codemender-scan`** label to any open PR.
    CodeMender will calculate the differential merge-base, verify and patch any
    newly introduced vulnerabilities, and open a Child PR targeting the feature
    branch.
2.  **Scheduled Scan**: Runs automatically based on the `schedule.cron`
    configuration on your default branch.
3.  **Manual On-Demand Scan**:

    *   **Via GitHub Web UI**: Navigate to **Actions $\rightarrow$ CodeMender
        Security Remediation $\rightarrow$ Run workflow**.
    *   **Via GitHub CLI (`gh`)**:

        ```bash
        # Trigger scan on default branch
        gh workflow run codemender.yml

        # Trigger scan with custom inputs
        gh workflow run codemender.yml --ref main -f scan_target="." -f max_tasks="10"
        ```

--------------------------------------------------------------------------------

### Review Surfaces

CodeMender provides 4 integrated review surfaces:

#### 1. In-PR Child Pull Requests (Internal PRs)

When a vulnerability is discovered on an active internal Pull Request,
CodeMender creates a **Child Pull Request** targeting the developer's feature
branch (`pr_head_ref`).

*   **Zero Merge Collisions**: Developers review the fix in isolation.
*   **1-Click Merge**: Merging the Child PR incorporates the security patch
    directly into the developer's branch.

#### 2. Fork Pull Request Review Comments

For Pull Requests originating from repository forks, Child PR creation is
skipped to respect security boundaries. CodeMender posts a Markdown review
comment directly on the Fork PR containing:

*   Exploit analysis and vulnerability summary.
*   Unified patch diff.
*   One-line copyable local `git apply` instructions.

#### 3. GitHub Actions Step Summary (`$GITHUB_STEP_SUMMARY`)

Every CI run renders a Markdown summary dashboard directly in the GitHub Actions
run overview, showing:

*   Remediation Overview (Total Discovered, Fixed, Verified, Pre-Existing
    Ignored, Skipped Duplicates).
*   Discovered Findings & Status Table.
*   LLM Token Usage Summary.

#### 4. GitHub Security Tab (SARIF Integration)

*   **Nightly Scans on `main`**: All findings (including existing remediations
    marked `SKIPPED_DUPLICATE`) are published to SARIF with `underReview`
    suppression metadata, keeping the repository Security Tab alert inventory
    accurate without prematurely closing open alerts.
*   **PR Scans**: Untouched legacy tech debt (`PRE_EXISTING_IGNORED`) is
    excluded from PR SARIF uploads to ensure developer PR checks remain focused
    strictly on new changes ("Clean as You Code").

--------------------------------------------------------------------------------

## 7. Operational Reference & Advanced Configurations

### Building & Publishing the Standard Runner Base Image

The CodeMender runner base image (`ghcr.io/<org>/codemender-runner:latest`)
contains the pre-baked standard LTS language runtimes (Python 3.11, Node.js 22
LTS, Go 1.22+, OpenJDK 17), build essentials (`gcc`, `g++`, `make`, `git`,
`curl`, `fuser`, `unzip`), the `cm` Go binary in `/usr/local/bin/cm`, and the
isolated orchestrator Python virtual environment in `/opt/codemender/venv`.

#### Method A: Automated CI Workflow (Recommended)

The repository includes a ready-to-use GitHub Actions workflow at
`.github/workflows/build_runner_image.yml` that builds and publishes the image
automatically:

1.  **Automatic Build**: Runs automatically whenever `Dockerfile`,
    `codemender_agent/**`, or `requirements.txt` are pushed to `main`.
2.  **Manual Dispatch**: Run manually via **Actions $\rightarrow$ Build &
    Publish CodeMender Runner Image $\rightarrow$ Run workflow**.

#### Method B: Manual Local Build & Push via Docker CLI

```bash
# 1. Download the CodeMender Go CLI binary into the repository root
URL="https://artifactregistry.googleapis.com/download/v1/projects/cmoc-prod/locations/us/repositories/codemender-cli-production/files/cm%3Astable%3Acm-linux-amd64.zip:download?alt=media"
curl -fsSL -o cm-linux-amd64.zip "$URL"
unzip -q -o cm-linux-amd64.zip cm
chmod +x cm

# 2. Log in to GitHub Container Registry (GHCR) with 'write:packages' PAT
echo "$GITHUB_PAT" | docker login ghcr.io -u "your-github-username" --password-stdin

# 3. Build and push the base image
IMAGE_NAME="ghcr.io/your-org-or-username/codemender-runner:latest"
docker build -t "$IMAGE_NAME" .
docker push "$IMAGE_NAME"
```

---

### Bring-Your-Own-Image (BYOI) Custom Toolchains

If your repository requires specialized build tools (such as Rust, PHP, C++,
custom SDKs, or database engines for unit test validation), you can create a
custom runner image that inherits from the standard CodeMender base runner.

#### 1. Create a Custom Dockerfile

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

#### 2. Build and Publish Image to GHCR

Build and push your image to GitHub Container Registry
(`ghcr.io/your-org/my-custom-runner:latest`).

#### 3. Pass Custom Image in Reusable Workflow

```yaml
    uses: your-org/codemender-workflows/.github/workflows/codemender_parallel.yml@main
    with:
      runner_image: ghcr.io/your-org/my-custom-runner:latest
      build_command: 'composer install && cargo test'
    secrets:
      ...
```

---

### Workflow Inputs (`with:`)

| Parameter | Type | Default | Description |
| :--- | :--- | :--- | :--- |
| `runner_image` | `string` | `ghcr.io/ilbzzz/codemender-runner:latest` | Universal container runner image or Bring-Your-Own-Image (BYOI). |
| `runner_type` | `string` | `ubuntu-latest` | GitHub Actions runner machine label. |
| `scan_target` | `string` | `.` | Target subdirectory path(s) to scan. |
| `build_command` | `string` | `""` | Custom build/test verification command (auto-detected if omitted). |
| `max_tasks` | `number` | `10` | Maximum number of parallel worker tasks in dynamic matrix. |
| `sandbox_enabled` | `boolean` | `true` | Enable `cm` sandbox filesystem & network isolation in container. |
| `intermediate_artifact_retention_days` | `number` | `3` | Retention period (days) for base state and worker shard artifacts. |
| `report_artifact_retention_days` | `number` | `90` | Retention period (days) for final HTML, JSON, and SARIF triage reports. |
| `upload_sarif` | `boolean` | `true` | Upload generated `report.sarif` findings to GitHub Security Tab. |
| `fail_on_findings` | `boolean` | `true` *(on PR)*, `false` *(on Nightly)* | Exit with non-zero code in Stage 3 if actionable vulnerabilities are detected on PR diff. |
| `model` | `string` | `""` *(CodeMender default)* | Global Gemini model override across all stages. Check up-to-date defaults & supported models [here](https://docs.cloud.google.com/gemini-enterprise-agent-platform/codemender#specifying-the-model). |
| `find_model` | `string` | `""` *(inherits `model`)* | Dedicated model override for Stage 1 vulnerability discovery (`cm find`). |
| `verify_model` | `string` | `""` *(inherits `model`)* | Dedicated model override for Stage 2 exploit verification (`cm verify`). |
| `fix_model` | `string` | `""` *(inherits `model`)* | Dedicated model override for Stage 2 patch synthesis (`cm fix`). |
| `skip_exploit_verification` | `boolean` | `false` | When `true`, skips dynamic exploit verification (`cm verify --skip-exploit-verification`) and generates patches directly. |

---

### Workflow Secrets (`secrets:`)

| Secret Name | Required | Description |
| :--- | :--- | :--- |
| `gcp_workload_identity_provider` | Yes (if WIF) | Google Cloud Workload Identity Provider resource URI. |
| `gcp_service_account` | Yes (if WIF) | Google Cloud Service Account email for Vertex AI impersonation. |
| `gcp_sa_key` | Optional | Direct Service Account JSON key (alternative to Workload Identity). |
| `github_app_id` | Recommended | GitHub App ID for automatic 60-minute installation token minting. |
| `github_app_private_key` | Recommended | GitHub App private key (`.pem`) for installation token minting. |
| `custom_github_token` | Optional | Fallback GitHub Token or PAT (if GitHub App is not configured). |

---

### Advanced AI Model & Execution Flags (`env:`)

| Environment Variable | Default | Description |
| :--- | :--- | :--- |
| `CODEMENDER_MODEL` | *(CodeMender default)* | Base Gemini model override used across all discovery, verification, and fix stages. Check up-to-date defaults & supported models [here](https://docs.cloud.google.com/gemini-enterprise-agent-platform/codemender#specifying-the-model). |
| `CODEMENDER_FIND_MODEL` | *(inherits base)* | Dedicated model override for Stage 1 vulnerability discovery (`cm find`). |
| `CODEMENDER_VERIFY_MODEL` | *(inherits base)* | Dedicated model override for Stage 2 exploit PoC generation & verification. |
| `CODEMENDER_FIX_MODEL` | *(inherits base)* | Dedicated model override for Stage 2 code patch synthesis (`cm fix`). |
| `CODEMENDER_FAIL_ON_FINDINGS` | `true` *(on PR)*, `false` *(on Nightly)* | Exit with non-zero status in Stage 3 if actionable vulnerabilities are detected on PR. |
| `CODEMENDER_SKIP_EXPLOIT_VERIFICATION` | `false` | When `true`, skips dynamic exploit verification and generates patches directly. |
| `CODEMENDER_SANDBOX_ENABLED` | `true` | Enable `cm` process namespace and filesystem isolation. |
| `CODEMENDER_SANDBOX_NETWORK_PROFILE` | `permissive-open` | Sandbox network policy (`permissive-open` or `restricted-local`). |
| `CODEMENDER_FORCE_OVERWRITE` | `false` | When `true`, overwrites existing branches and PRs instead of skipping duplicates. |

--------------------------------------------------------------------------------

### Troubleshooting & Common Failure Modes

#### 1. WIF Authentication Error: `Google auth failed: 403 Forbidden` / `IAM_PERMISSION_DENIED`

*   **Cause**: The GitHub runner OIDC token does not match the provider's
    `attribute_condition` or the runner Service Account does not have
    `roles/iam.workloadIdentityUser`.
*   **Fix**:
    1.  Check that `github_owner` / `allowed_repositories` in your Terraform
        configuration or `gcloud` command includes the repository triggering the
        workflow.
    2.  Verify the caller workflow has top-level permission `permissions:
        id-token: write`.

#### 2. GitHub Token Error: `HttpError: Resource not accessible by integration`

*   **Cause**: The GitHub App is missing required permissions or is not
    installed on the target repository.
*   **Fix**:
    1.  Navigate to **Settings $\rightarrow$ Developer settings $\rightarrow$
        GitHub Apps $\rightarrow$ [Your App] $\rightarrow$ Permissions &
        events** and verify `Contents (rw)`, `Pull requests (rw)`, and `Security
        events (rw)`.
    2.  Check the **Install App** tab to ensure the App is installed on the
        target repository.

#### 3. Build & Test Verification Failure in Stage 2 (`cm fix`)

*   **Cause**: The `build_command` failed or timed out during patch validation.
*   **Fix**:
    1.  Verify that `build_command` (e.g. `npm test`, `pytest`, `mvn test`) runs
        cleanly locally in a fresh checkout.
    2.  Ensure that the test suite does not require external network databases
        that are blocked by the sandbox (or set
        `CODEMENDER_SANDBOX_NETWORK_PROFILE: permissive-open`).

--------------------------------------------------------------------------------

## 8. Appendix: Pull Request Trigger Reference & Patterns

In GitHub Actions, the `pull_request` event provides granular controls over
which PR lifecycle events trigger the workflow.

> [!NOTE]
> **Official GitHub Documentation**: For full syntax specifications and
> event definitions, see the
> [GitHub Actions Events: pull_request](https://docs.github.com/en/actions/writing-workflows/choosing-when-your-workflow-runs/events-that-trigger-workflows#pull_request)
> documentation.

### 8.1 Automating the `codemender-scan` Label (Default Trigger Flow)

Because CodeMender defaults to the `codemender-scan` label trigger to preserve
AI model tokens and CI runner minutes, teams frequently automate the label
attachment process to fit their developer workflows. Several practical
strategies include:

*   **Path-Based Auto-Labeling (e.g., via `actions/labeler`)**: Automatically
    attach `codemender-scan` when a Pull Request modifies files in
    security-sensitive paths (e.g., `src/**`, `api/**`, `backend/**`, or
    specific file extensions). Modifying non-code files (such as documentation
    or assets) will not trigger the label.
*   **ChatOps / Slash Commands (e.g., `/codemender` or `/scan`)**: Allow
    developers and reviewers to trigger scans on-demand by commenting
    `/codemender` or `/scan` on the Pull Request. A lightweight comment-handling
    action validates the commenter's permissions and applies the label.
*   **Author Role & Contributor Tiering**: Automatically attach
    `codemender-scan` for Pull Requests opened by internal team members or
    organization collaborators, while requiring manual maintainer approval or
    labeling for external/fork contributors.
*   **Draft-to-Ready State Transitions**: Automatically apply `codemender-scan`
    only when a Draft PR is transitioned to "Ready for review", preventing
    premature scans on incomplete work-in-progress code.
*   **Post-Scan Label Lifecycle Cleanup**: Remove the `codemender-scan` label
    upon pipeline completion so that authors can easily re-trigger a fresh scan
    simply by re-attaching the label.

### 8.2 Alternative PR Trigger Patterns & Reference

If your team prefers an automated trigger model other than label-based gating,
you can configure GitHub Actions to trigger scans using alternative activity
types and filter rules.

#### 1. Practical Trigger Patterns for CodeMender

You can choose one of the following patterns based on your team's workflow and
token budget:

*   **Pattern A: On-Demand Label-Triggered (Recommended Default)**:
    -   `types: [labeled]`
    -   *Behavior*: Scans run **only** when a developer or reviewer attaches the
        `codemender-scan` label.
    -   *Advantages*: Highest cost and token efficiency. Avoids running on
        unfinished WIP commits and avoids generating 1-second skipped runs on
        untagged PRs.
*   **Pattern B: Label-Triggered + Auto Re-scan on Push**:
    -   `types: [labeled, synchronize]`
    -   *Behavior*: Scans run when `codemender-scan` is added, and
        **automatically re-scans** whenever the author pushes new commits to
        that PR.
    -   *Advantages*: Ideal for iterative remediation. The author can see if
        their fixes resolved the findings without having to remove and re-add
        the label.
*   **Pattern C: Universal CI Security Gate (Scan Every PR Automatically)**:
    -   `types: [opened, synchronize, reopened]`
    -   *Behavior*: Scans **every single PR** automatically upon creation and on
        every commit push, without requiring any label.
    -   *How to enable*: Update `types:` and remove the
        `contains(github.event.pull_request.labels.*.name, 'codemender-scan')`
        check from the job's `if:` condition.
    -   *Advantages*: Maximum enforcement for zero-trust or highly regulated
        repositories.
*   **Pattern D: Ready-for-Review (Draft PR Optimization)**:
    -   `types: [ready_for_review, synchronize]`
    -   *Behavior*: Skips initial Draft PRs completely and only starts scanning
        once the PR is marked "Ready for review", continuing to scan on
        subsequent pushes.

#### 2. Filtering by Branches and File Paths

*   **Target Branches (`branches: [...]`)**: Restrict PR scans to PRs targeting
    specific branches (e.g. `branches: [main, master, 'release/**']`).
*   **Path Filtering (`paths:` or `paths-ignore:`)**: Save LLM tokens and CI
    runner minutes by ignoring PRs that only modify documentation or CI
    configurations:

    ```yaml
    pull_request:
      types: [labeled, synchronize]
      branches: [main, master]
      paths-ignore:
        - 'docs/**'
        - '**.md'
        - '.github/**'
    ```

#### 3. Available GitHub PR Activity Types (`types: [...]`)

If `types:` is omitted under `pull_request:`, GitHub Actions defaults to
`[opened, synchronize, reopened]`.

| Activity Type | When It Triggers | Relevance to CodeMender |
| :--- | :--- | :--- |
| **`labeled`** *(Recommended)* | A label is added to the PR | **Primary trigger for on-demand scans**. Allows scanning only when `codemender-scan` label is attached, preventing token waste on WIP drafts. |
| **`synchronize`** | New commits are pushed to the PR head branch | **Iterative development**. Auto-triggers a re-scan when new commits are pushed to an already-labeled PR. Stale in-flight runs are auto-canceled via concurrency. |
| **`opened`** | A new PR is created | **Universal scan**. Used when teams want CodeMender to automatically scan *every* newly opened PR. |
| **`reopened`** | A previously closed PR is reopened | **Universal scan**. Ensures newly revived PRs are validated against current base branch code. |
| **`ready_for_review`** | A Draft PR is converted to ready for review | **Draft workflow**. Prevents running scans while developers are actively drafting WIP code. |
| `unlabeled` | A label is removed from the PR | Rarely used for CodeMender. |
| `edited` | PR title, description, or base branch is modified | Useful if changing the target base branch requires a differential re-scan. |
| `closed` | PR is closed or merged | Rarely used for active scanning. |
| `converted_to_draft` | PR is converted back to draft status | Rarely used. |
| `review_requested` | A review is requested on the PR | Alternative trigger if scans should run only upon formal review request. |
