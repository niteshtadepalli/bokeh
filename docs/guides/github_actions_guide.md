# CodeMender GitHub Actions Orchestrator Guide

This guide provides step-by-step instructions for onboarding repositories to
**CodeMender Orchestrator** using native **GitHub Actions (GHA)** workflows.

CodeMender runs as a decentralized, 3-stage parallel pipeline inside
containerized GitHub Actions runners, enabling automated vulnerability scanning,
exploit verification, and patch synthesis with **zero external cloud storage
buckets** required.

--------------------------------------------------------------------------------

## Architecture Overview

```
+---------------------------------------------------------------------------------------+
|                                 TRIGGER INITIATION                                    |
|   1. Scheduled Nightly (cron on default branch)                                       |
|   2. Manual On-Demand (workflow_dispatch)                                             |
|   3. Labeled Pull Request (pull_request event with 'security-scan' label)             |
+---------------------------------------------------------------------------------------+
                                           │
                                           ▼
+---------------------------------------------------------------------------------------+
|                              STAGE 1: SCAN & PARTITION                                |
|   Job: scan (Timeout: 55m)                                                            |
|   - Full repository vulnerability discovery via 'cm find .'                           |
|   - Differential PR filtering (untouched legacy debt marked PRE_EXISTING_IGNORED)      |
|   - Universal deduplication against open PRs and existing branches (SKIPPED_DUPLICATE)|
|   - Partitions findings across N dynamic worker buckets                               |
|   - Archives base state and uploads artifact 'codemender-base-state'                  |
|   - Emits GITHUB_OUTPUT: matrix=[0..N-1], findings_count=<count>                      |
+---------------------------------------------------------------------------------------+
                                           │
                                           ▼ (if findings_count > 0)
+---------------------------------------------------------------------------------------+
|                            STAGE 2: PARALLEL WORKERS (Matrix)                         |
|   Job: worker (Timeout: 55m, fail-fast: false)                                        |
|   - Downloads base state artifact and checks out working base ref                     |
|   - Targeted live deduplication check (O(1) git ls-remote + PR lookup)                |
|   - Exploit verification ('cm verify') & patch synthesis ('cm fix')                   |
|   - Surgical Git staging querying SQLite 'patches.edited_files' with 3-tier fallback  |
|   - Remediation routing:                                                              |
|       * Nightly/Manual: Pushes 'codemender/fix-...' & opens PR to main                |
|       * Internal PR: Pushes 'codemender/fix-...' & opens Child PR to developer branch|
|       * Fork PR: Posts review comment on Fork PR with diff and 'git apply' block      |
|   - Uploads mutated shard artifact 'worker-shard-<index>'                             |
+---------------------------------------------------------------------------------------+
                                           │
                                           ▼ (always() after scan succeeds)
+---------------------------------------------------------------------------------------+
|                              STAGE 3: AGGREGATE & REPORTING                           |
|   Job: aggregate (Timeout: 30m)                                                       |
|   - Downloads all worker database shards and merges via clean UPDATE queries          |
|   - Scoped reporting:                                                                 |
|       * Nightly scans: Retains SKIPPED_DUPLICATE with SARIF suppressions (underReview)|
|       * PR scans: Purges PRE_EXISTING_IGNORED for clean "Clean as You Code" summary   |
|   - Generates 4-tier reporting surfaces:                                              |
|       1. Uploads SARIF to GitHub Security Tab ('github/codeql-action/upload-sarif')   |
|       2. Renders Markdown dashboard to $GITHUB_STEP_SUMMARY (1000 KiB guardrail)      |
|       3. In-PR Child PR descriptions or Fork PR review comments                       |
|       4. Uploads downloadable HTML & JSON reports ('codemender-report-...')           |
+---------------------------------------------------------------------------------------+
```

--------------------------------------------------------------------------------

## 1. Prerequisites

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

## 2. Setting Up GCP Workload Identity Federation (WIF)

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

## 3. GitHub App Configuration

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

## 4. Enabling CodeMender in Any Target Repository

To enable CodeMender security scanning and automated remediation on any target repository (e.g. `your-org/backend-service` or `username/juice-shop-local`), follow this 4-step onboarding checklist.

### Understanding the Containerized Runner Model

CodeMender runs as a containerized pipeline inside GitHub Actions. You do not need to install tools, runtimes, or dependencies directly on host runner machines:
- The reusable workflow automatically executes inside the pre-built CodeMender base runner image (`ghcr.io/ilbzzz/codemender-runner:latest` or your organization's runner).
- The container image contains pre-baked multi-language toolchains (Python 3.11, Node.js 20 LTS, Java 17, Go 1.22+), build essentials (`gcc`, `make`, `curl`, `git`), and the `cm` Go binary in `/usr/local/bin/cm`.
- Target repositories only need to configure authentication secrets and add a caller workflow file (`.github/workflows/codemender.yml`).

---

### Step 1: Install the GitHub App on the Target Repository

1. Navigate to your GitHub App settings or installation dashboard:
   - **Personal Account**: `https://github.com/settings/apps/<your-app-name>/installations`
   - **Organization**: `https://github.com/organizations/<your-org>/settings/apps/<your-app-name>/installations`
2. Click **Configure** next to the installation entry.
3. Under **Repository access**:
   - Select **All repositories** (recommended for org-wide coverage), OR
   - Select **Only select repositories** and pick your target repository (e.g. `juice-shop-local`).
4. Click **Save**.

> [!NOTE]
> Installing the GitHub App grants CodeMender temporary, least-privilege tokens to checkout code, push remediation branches (`codemender/fix-...`), open Child PRs, and upload SARIF security alerts.

---

### Step 2: Configure Actions Secrets

Add the required credentials so the runner can authenticate with GCP Vertex AI (Gemini LLM APIs) and GitHub:

#### Option A: Organization-Level Secrets (Recommended for Organizations)
If you manage a GitHub Organization, configure these secrets once at **Organization Settings $\rightarrow$ Secrets and variables $\rightarrow$ Actions**. All repositories will inherit them automatically:
- `GCP_WORKLOAD_IDENTITY_PROVIDER`
- `GCP_SERVICE_ACCOUNT`
- `GH_APP_ID`
- `GH_APP_PRIVATE_KEY`

#### Option B: Repository-Level Secrets (Personal Accounts or Individual Repos)
In your target repository:
1. Go to **Settings $\rightarrow$ Secrets and variables $\rightarrow$ Actions**.
2. Click **New repository secret** and add the following 4 secrets:
   - `GCP_WORKLOAD_IDENTITY_PROVIDER`: e.g. `projects/123456789/locations/global/workloadIdentityPools/github-actions-pool/providers/github-actions-provider`
   - `GCP_SERVICE_ACCOUNT`: e.g. `codemender-runner-sa@your-gcp-project.iam.gserviceaccount.com`
   - `GH_APP_ID`: Application ID of your GitHub App
   - `GH_APP_PRIVATE_KEY`: Complete PEM content of the private key (`-----BEGIN RSA PRIVATE KEY...`)

> [!IMPORTANT]
> **Verify GCP WIF Scoping**: Ensure the target repository is allowed by your GCP Workload Identity Provider attribute condition:
> - If configured with **User Scope** (`assertion.repository_owner == '<user>'`) or **Org Scope** (`assertion.repository_owner == '<org>'`), all repositories owned by that user/org are authenticated automatically.
> - If configured with **Specific Repositories Scope** (Section 2, Option C), add the new repository to the provider's allowed repository list and IAM policy bindings.

---

### Step 3: Grant GHCR Package Access to Target Repository

GitHub Actions runners need permission to pull the runner container image (`ghcr.io/ilbzzz/codemender-runner:latest`).

#### If the Container Package is Public:
- No package access configuration is needed. Any target repository can pull the runner image with zero setup.

#### If the Container Package is Private:
1. Go to your GitHub profile or organization $\rightarrow$ click the **Packages** tab.
2. Select the **`codemender-runner`** package.
3. Click **Package settings** (in the right sidebar):
   - Personal account URL: `https://github.com/users/<username>/packages/container/codemender-runner/settings`
   - Organization URL: `https://github.com/orgs/<org>/packages/container/codemender-runner/settings`
4. Under **Manage Actions access**:
   - Click **Add repository** $\rightarrow$ select your target repository $\rightarrow$ select role **Read**.
5. In your caller workflow, ensure the top-level permissions block contains `packages: read`.

---

### Step 4: Add Caller Workflow (`.github/workflows/codemender.yml`)

Add `.github/workflows/codemender.yml` to the default branch of the target repository:

#### Calling Reusable Workflow (Organizations & Public Workflow Repos)
```yaml
name: CodeMender Security Remediation

on:
  schedule:
    - cron: '0 2 * * 0'  # Weekly on Sunday at 2:00 AM UTC
  pull_request:
    types: [opened, synchronize, labeled]
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
  remediate:
    if: >
      github.event_name == 'schedule' ||
      github.event_name == 'workflow_dispatch' ||
      (github.event_name == 'pull_request' && contains(github.event.pull_request.labels.*.name, 'security-scan'))
    uses: ilbzzz/codemender-agent/.github/workflows/codemender_parallel.yml@main
    with:
      # Optional: custom test verification command for target repo
      build_command: 'npm test'
    secrets:
      gcp_workload_identity_provider: ${{ secrets.GCP_WORKLOAD_IDENTITY_PROVIDER }}
      gcp_service_account: ${{ secrets.GCP_SERVICE_ACCOUNT }}
      github_app_id: ${{ secrets.GH_APP_ID }}
      github_app_private_key: ${{ secrets.GH_APP_PRIVATE_KEY }}
```

#### Calling Local Workflow (Personal Accounts with Private Repositories)
> [!NOTE]
> GitHub disallows cross-repository reusable workflow calls between private repositories under personal user accounts.
> For private personal repositories, copy `codemender_parallel.yml` into `.github/workflows/` of the target repository and call it locally:
```yaml
    uses: ./.github/workflows/codemender_parallel.yml
```

--------------------------------------------------------------------------------

## 5. Example Caller Workflows

Create a workflow file in your repository at `.github/workflows/codemender.yml`.

### Example 1: Standard Workflow (Scheduled & Pull Request Scans)

```yaml
name: CodeMender Security Remediation

on:
  schedule:
    - cron: '0 2 * * 0'  # Weekly on Sunday at 2:00 AM UTC
  pull_request:
    types: [opened, synchronize, labeled]
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
  remediate:
    # Only run on Schedule, Manual Trigger, or PRs with 'security-scan' label
    if: >
      github.event_name == 'schedule' ||
      github.event_name == 'workflow_dispatch' ||
      (github.event_name == 'pull_request' && contains(github.event.pull_request.labels.*.name, 'security-scan'))
    uses: ilbzzz/codemender-agent/.github/workflows/codemender_parallel.yml@main
    secrets:
      gcp_workload_identity_provider: ${{ secrets.GCP_WORKLOAD_IDENTITY_PROVIDER }}
      gcp_service_account: ${{ secrets.GCP_SERVICE_ACCOUNT }}
      github_app_id: ${{ secrets.GH_APP_ID }}
      github_app_private_key: ${{ secrets.GH_APP_PRIVATE_KEY }}
```

--------------------------------------------------------------------------------

### Example 2: Custom Configuration with All Optional Flags Configured

```yaml
name: CodeMender Security Remediation (Custom Flags)

on:
  schedule:
    - cron: '0 2 * * 0'
  pull_request:
    types: [opened, synchronize, labeled]
    branches: [main, master]
  workflow_dispatch:
    inputs:
      scan_target:
        description: 'Target subdirectory to scan'
        required: false
        default: 'src/backend'
        type: string
      build_command:
        description: 'Custom build & test command for verification (e.g. npm test, pytest)'
        required: false
        default: ''
        type: string
      max_tasks:
        description: 'Maximum parallel worker tasks'
        required: false
        default: '6'
        type: string

permissions:
  id-token: write
  contents: write
  pull-requests: write
  security-events: write
  actions: read
  packages: read

jobs:
  remediate:
    if: >
      github.event_name == 'schedule' ||
      github.event_name == 'workflow_dispatch' ||
      (github.event_name == 'pull_request' && contains(github.event.pull_request.labels.*.name, 'security-scan'))
    uses: ilbzzz/codemender-agent/.github/workflows/codemender_parallel.yml@main
    with:
      # Container image for runner execution (Default: ghcr.io/<org>/codemender-runner:latest)
      runner_image: ghcr.io/ilbzzz/codemender-runner:latest

      # Runner machine label (Default: ubuntu-latest)
      runner_type: ubuntu-latest

      # Target directory or semicolon-separated paths to scan (Default: '.')
      scan_target: ${{ inputs.scan_target || '.' }}

      # Custom build/test verification command executed before opening PRs (Default: auto-detected if empty)
      build_command: ${{ inputs.build_command }}

      # Maximum parallel worker tasks in Stage 2 (Default: 10 for Nightly, 4 for PR)
      max_tasks: ${{ inputs.max_tasks && fromJson(inputs.max_tasks) || 6 }}

      # Upload SARIF report to GitHub Security Tab (Default: true)
      upload_sarif: true

      # Retention period in days for intermediate base & shard artifacts (Default: 3)
      intermediate_artifact_retention_days: 3

      # Retention period in days for final HTML/JSON triage reports (Default: 90)
      report_artifact_retention_days: 90
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

## 6. Building & Publishing the Standard Runner Base Image

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

## 7. Bring-Your-Own-Image (BYOI) Custom Toolchains

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

## 8. Reviewing & Triaging Remediations

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

## 9. Configuration Reference Table

### Workflow Inputs (`with:`)

Parameter                              | Type      | Default                                  | Description
:------------------------------------- | :-------- | :--------------------------------------- | :----------
`runner_image`                         | `string`  | `ghcr.io/<org>/codemender-runner:latest` | Container image for runner execution.
`runner_type`                          | `string`  | `ubuntu-latest`                          | GitHub Actions runner label.
`scan_target`                          | `string`  | `.`                                      | Target directory or semicolon-separated paths to scan.
`build_command`                        | `string`  | `""`                                     | Custom build/test command for verification.
`max_tasks`                            | `number`  | `4` (PR) / `10` (Nightly)                | Maximum parallel worker tasks in dynamic matrix.
`intermediate_artifact_retention_days` | `number`  | `3`                                      | Retention period in days for base state & shard artifacts.
`report_artifact_retention_days`       | `number`  | `90`                                     | Retention period in days for final HTML/JSON reports.
`upload_sarif`                         | `boolean` | `true`                                   | Whether to upload `report.sarif` to GitHub Security Tab.

### Workflow Secrets (`secrets:`)

| Secret Name                      | Required     | Description                |
| :------------------------------- | :----------- | :------------------------- |
| `gcp_workload_identity_provider` | Yes (if WIF) | Google Cloud Workload      |
:                                  :              : Identity Provider resource :
:                                  :              : URI.                       :
| `gcp_service_account`            | Yes (if WIF) | Google Cloud Service       |
:                                  :              : Account email for          :
:                                  :              : impersonation.             :
| `gcp_sa_key`                     | Optional     | Direct Service Account     |
:                                  :              : JSON key (alternative to   :
:                                  :              : WIF).                      :
| `github_app_id`                  | Recommended  | GitHub App ID for token    |
:                                  :              : generation.                :
| `github_app_private_key`         | Recommended  | GitHub App Private Key     |
:                                  :              : (`.pem`) for token         :
:                                  :              : generation.                :
| `custom_github_token`            | Optional     | Fallback GitHub Token /    |
:                                  :              : PAT (if GitHub App not     :
:                                  :              : configured).               :

### Advanced AI Model & Execution Flags (`env:`)

| Environment Variable                   | Default          | Description      |
| :------------------------------------- | :--------------- | :--------------- |
| `CODEMENDER_MODEL`                     | `gemini-1.5-pro` | Base Gemini LLM  |
:                                        :                  : used across all  :
:                                        :                  : discovery,       :
:                                        :                  : verification,    :
:                                        :                  : and fix stages.  :
| `CODEMENDER_FIND_MODEL`                | *(inherits       | Dedicated model  |
:                                        : base)*           : for Stage 1      :
:                                        :                  : vulnerability    :
:                                        :                  : discovery.       :
| `CODEMENDER_VERIFY_MODEL`              | *(inherits       | Dedicated model  |
:                                        : base)*           : for Stage 2      :
:                                        :                  : exploit PoC      :
:                                        :                  : generation and   :
:                                        :                  : verification.    :
| `CODEMENDER_FIX_MODEL`                 | *(inherits       | Dedicated model  |
:                                        : base)*           : for Stage 2 code :
:                                        :                  : remediation.     :
| `CODEMENDER_SKIP_EXPLOIT_VERIFICATION` | `false`          | When set to      |
:                                        :                  : `true`, skips    :
:                                        :                  : writing dynamic  :
:                                        :                  : exploit PoCs and :
:                                        :                  : goes directly to :
:                                        :                  : patch            :
:                                        :                  : generation.      :
