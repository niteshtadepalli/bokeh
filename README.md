# CodeMender Orchestrator

The CodeMender Orchestrator is an automated execution runner designed to run
within an engineering team's own infrastructure (e.g., as a Google Cloud Run
Job). It automates local vulnerability scanning, validation, automated patching
via the CodeMender CLI (`cm`), and Pull Request generation on GitHub.

--------------------------------------------------------------------------------

## Architecture Overview (Bring Your Own Project - BYOP)

To validate and fix code vulnerabilities, CodeMender must run your codebase's
specific compilers, linters, and test suites. Because a central backend cannot
securely host thousands of custom environments, the orchestrator executes inside
your own secure container:

1.  **CodeMender Backend ("The Brain")**: Central LLM service that analyzes
    vulnerabilities and reasons about fixes.
2.  **CodeMender CLI (`cm`) ("The Translator")**: Local CLI tool handling server
    communication, workspace edits, and local test validation.
3.  **Custom Container Environment ("The Workshop")**: Cloud Run Job container
    holding `orchestrator.py`, the `cm` binary, and your language toolchains
    (Node.js, Go, Java, Python, etc.).
4.  **Orchestrator Script (`orchestrator.py`) ("The Manager")**: Clones code,
    runs scans (`cm find`), verifies findings (`cm verify`), applies fixes (`cm
    fix`), and creates Pull Requests on GitHub.

### Orchestration Flowchart

```mermaid
graph TD
    Start[Start Orchestrator] --> Sync[1. Clone / Pull Repository]
    Sync --> Init[2. Initialize CodeMender]
    Init --> Scan[3. Scan Codebase<br/>'cm find .']
    Scan --> Report[4. Get Findings Report<br/>'cm report']
    Report --> LoopStart{5. Loop: Each Finding}

    LoopStart --> CheckBranch{Branch Exists on GitHub?}
    CheckBranch -- Yes & --force not set --> Skip[Skip Finding]
    CheckBranch -- No OR --force set --> Verify[6. Verify Finding<br/>'cm find verify']

    Verify --> IsVerified{Verified in DB?<br/>'VERIFIED'}
    IsVerified -- No (after 3 tries) --> Skip
    IsVerified -- Yes --> ApplyFix[7. Apply Fix on Default Branch<br/>'cm fix']

    ApplyFix --> IsFixed{Fix Succeeded?<br/>'FIXED'}
    IsFixed -- No --> ResetDefault[Reset default branch]
    ResetDefault --> Skip

    IsFixed -- Yes --> HasChanges{Uncommitted Changes?}
    HasChanges -- No --> ResetDefault
    HasChanges -- Yes --> SwitchBranch[8. Checkout Feature Branch]

    SwitchBranch --> Commit[9. Commit Changes]
    Commit --> Push[10. Push Branch]
    Push --> PR[11. Open Pull Request]
    PR --> ResetDefault2[Reset default branch]
    ResetDefault2 --> NextFinding[Next Finding]
    Skip --> NextFinding
    NextFinding --> LoopStart
```

--------------------------------------------------------------------------------

## Key Constraints & Operational Rules

-   **Credential Scrubbing**: `orchestrator.py` explicitly scrubs sensitive
    credentials (`GITHUB_APP_TOKEN`, `GITHUB_PAT`, `GITHUB_TOKEN`) from the
    subprocess environment before invoking `cm` commands (`cm verify`, `cm fix`)
    to eliminate remote code execution (RCE) exfiltration risks.
-   **Single-Sync Git Rule**: The orchestrator syncs only once at the beginning
    of the scan (`git clone --depth 1`). Fixed branches are pushed directly to
    remote and PRs opened immediately, delegating merge conflict resolution to
    GitHub's PR mergeability checks.
-   **PR Spam Prevention**: Branch names are deterministically derived using a
    hash of `VulnType` + `FilePath` (e.g., `codemender/fix-sqli-a1b2c3d4`). If a
    branch already exists on origin, the orchestrator skips duplicate `cm fix`
    operations.
-   **Workspace Reset**: Uses forced branch checkout (`git checkout -f`) when
    switching branches between findings.
-   **GCS Summary Reports**: At the end of the orchestrator run, it
    automatically compiles an interactive HTML summary report (`cm report -f
    html`). If `CODEMENDER_REPORT_BUCKET` is configured, the report is uploaded
    to GCS and a secure, temporary Signed URL (valid for 3 days) is printed in
    the job output logs for easy developer review.

--------------------------------------------------------------------------------

## Setup & Deployment Guide

### 1. Configure GitHub Authentication in Google Secret Manager

Create a secret in Google Secret Manager containing a GitHub Personal Access
Token (PAT) or GitHub App Token with repo access:

```bash
gcloud secrets create GITHUB_APP_TOKEN --replication-policy="automatic"
echo -n "ghp_your_github_access_token" | gcloud secrets versions add GITHUB_APP_TOKEN --data-file=-
```

### 2. Customize the Dockerfile for Your Language Toolchain

Modify `Dockerfile` to include the compilers, language runtime, and build tools
needed to build and test your codebase:

#### For Node.js / TypeScript:

```dockerfile
RUN curl -fsSL https://deb.nodesource.com/setup_20.x | bash - && \
    apt-get install -y nodejs
```

#### For Go:

```dockerfile
COPY --from=golang:1.22 /usr/local/go /usr/local/go
ENV PATH="/usr/local/go/bin:${PATH}"
```

#### For Java / Maven / Gradle:

```dockerfile
RUN apt-get update && apt-get install -y default-jdk maven gradle
```

### 3. Build and Push Container Image to Artifact Registry

```bash
# Create Artifact Registry Repository if not exists
gcloud artifacts repositories create codemender-runner \
    --repository-format=docker \
    --location=us-central1

# Build and push container image using the secure cloudbuild.yaml flow
gcloud builds submit --config=cloudbuild.yaml \
    --substitutions=_RELEASES_BUCKET="codemender-releases-${PROJECT_ID}" .
```

### 4. Deploy Cloud Run Job

> [!IMPORTANT] **Resource Requirements**: Cloud Run deployments **MUST** use the
> **Cloud Run Gen 2 Execution Environment** with a minimum of
> `--ephemeral-storage=10Gi` (or higher) to safely hold large git histories,
> build caches, and test artifacts without exhausting RAM.

Deploy the Cloud Run Job using `gcloud`:

```bash
gcloud run jobs create codemender-nightly-scan \
    --image=us-central1-docker.pkg.dev/$PROJECT_ID/codemender-runner/orchestrator:latest \
    --region=us-central1 \
    --execution-environment=gen2 \
    --ephemeral-storage=10Gi \
    --memory=4Gi \
    --cpu=2 \
    --set-env-vars="GITHUB_REPO_URL=https://github.com/your-org/your-repo.git,CODEMENDER_BUILD_COMMAND='npm install && npm test',CODEMENDER_REPORT_BUCKET=my-gcs-reports-bucket" \
    --set-secrets="GITHUB_APP_TOKEN=GITHUB_APP_TOKEN:latest"
```

### 4.b Configure GCS Summary Report IAM Permissions (Optional)

If you configure `CODEMENDER_REPORT_BUCKET` to upload summary reports to GCS,
you must grant the Cloud Run Job's Service Account (e.g. the default Compute
Engine service account) the required IAM permissions:

1.  **GCS Write Access**: Grant the Service Account the **Storage Object
    Creator** role (`roles/storage.objectCreator`) on the GCS bucket.
2.  **Signed URL Generation**: Generating v4 Signed URLs dynamically in Cloud
    Run requires the Service Account to have the **Service Account Token
    Creator** role (`roles/iam.serviceAccountTokenCreator`) on **itself**
    (allowing the client library to call the IAM SignBlob API on behalf of the
    runtime identity).

```bash
# Grant GCS Write Access
gcloud storage buckets add-iam-policy-binding gs://my-gcs-reports-bucket \
    --member="serviceAccount:$PROJECT_NUMBER-compute@developer.gserviceaccount.com" \
    --role="roles/storage.objectCreator"

# Grant Service Account Token Creator role to itself (to support SignBlob API)
gcloud iam service-accounts add-iam-policy-binding $PROJECT_NUMBER-compute@developer.gserviceaccount.com \
    --member="serviceAccount:$PROJECT_NUMBER-compute@developer.gserviceaccount.com" \
    --role="roles/iam.serviceAccountTokenCreator"
```

### 5. Schedule Nightly Batch Scans with Cloud Scheduler

To trigger batch vulnerability scans automatically every night:

```bash
gcloud scheduler jobs create http codemender-nightly-trigger \
    --location=us-central1 \
    --schedule="0 2 * * *" \
    --uri="https://us-central1-run.googleapis.com/apis/run.googleapis.com/v1/namespaces/$PROJECT_ID/jobs/codemender-nightly-scan:run" \
    --http-method=POST \
    --oauth-service-account-email="$PROJECT_NUMBER-compute@developer.gserviceaccount.com"
```

--------------------------------------------------------------------------------

## Local Development & Testing

Run unit tests locally:

```bash
python3 -m unittest test_orchestrator.py
```

Run orchestrator manually against a target repository:

```bash
export GITHUB_REPO_URL="https://github.com/your-org/your-repo.git"
export GITHUB_TOKEN="ghp_your_token"
python3 orchestrator.py
```

--------------------------------------------------------------------------------

## Future Work

-   **Automatic PR Re-opening on Force-Push**: When running in overwrite mode
    (`CODEMENDER_FORCE_OVERWRITE=true`), force-pushing new commits to a branch
    with a closed/unmerged PR is successful, but creating a new PR fails on
    GitHub API validation (HTTP 422). Future work should query existing pull
    requests and, if one is closed, programmatically re-open it via `PATCH
    /repos/{owner}/{repo}/pulls/{number}`.
-   **State Database Checkpointing (Persistence)**: Since Cloud Run Job
    execution is stateless, the SQLite state database (`~/.codemender/state.db`)
    is destroyed at the end of the run. A GCS checkpoint sync step should be
    introduced at startup and shutdown to pull/push the state database,
    preserving historically verified finding statuses (e.g., preserving manually
    flagged `FALSE_POSITIVE` or `RESOLVED` statuses).
-   **Workstation/Runner Command Injection Sandboxing**: Verification
    agent-generated exploit scripts (`exploit.sh`) are executed directly on the
    host VM/runner shell. Since these scripts are generated entirely by LLMs,
    malicious target project code could trigger command injections (exfiltrating
    Git secrets or accessing metadata services). Future work should isolate
    exploit verification executions inside an unprivileged Docker container or
    gVisor sandbox container.
