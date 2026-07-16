# CodeMender Orchestrator

The CodeMender Orchestrator is an automated execution runner designed to run within an engineering team's own infrastructure (e.g., as a Google Cloud Run Job). It automates local vulnerability scanning, validation, automated patching via the CodeMender CLI (`cm`), and Pull Request generation on GitHub.

---

## Architecture Overview (Bring Your Own Project - BYOP)

To validate and fix code vulnerabilities, CodeMender must run your codebase's specific compilers, linters, and test suites. Because a central backend cannot securely host thousands of custom environments, the orchestrator executes inside your own secure container:

1. **CodeMender Backend ("The Brain")**: Central LLM service that analyzes vulnerabilities and reasons about fixes.
2. **CodeMender CLI (`cm`) ("The Translator")**: Local CLI tool handling server communication, workspace edits, and local test validation.
3. **Custom Container Environment ("The Workshop")**: Cloud Run Job container holding `orchestrator.py`, the `cm` binary, and your language toolchains (Node.js, Go, Java, Python, etc.).
4. **Orchestrator Script (`orchestrator.py`) ("The Manager")**: Clones code, runs scans (`cm find`), verifies findings (`cm verify`), applies fixes (`cm fix`), and creates Pull Requests on GitHub.

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

---

## Key Constraints & Operational Rules

- **Credential Scrubbing**: `orchestrator.py` explicitly scrubs sensitive credentials (`GITHUB_APP_TOKEN`, `GITHUB_PAT`, `GITHUB_TOKEN`) from the subprocess environment before invoking `cm` commands (`cm verify`, `cm fix`) to eliminate remote code execution (RCE) exfiltration risks.
- **Single-Sync Git Rule**: The orchestrator syncs only once at the beginning of the scan (`git clone --depth 1`). Fixed branches are pushed directly to remote and PRs opened immediately, delegating merge conflict resolution to GitHub's PR mergeability checks.
- **PR Spam Prevention**: Branch names are deterministically derived using a hash of `VulnType` + `FilePath` (e.g., `codemender/fix-sqli-a1b2c3d4`). If a branch already exists on origin, the orchestrator skips duplicate `cm fix` operations.
- **Workspace Reset**: Uses forced branch checkout (`git checkout -f`) when switching branches between findings.

---

## Setup & Deployment Guide

### 1. Configure GitHub Authentication in Google Secret Manager

Create a secret in Google Secret Manager containing a GitHub Personal Access Token (PAT) or GitHub App Token with repo access:

```bash
gcloud secrets create GITHUB_APP_TOKEN --replication-policy="automatic"
echo -n "ghp_your_github_access_token" | gcloud secrets versions add GITHUB_APP_TOKEN --data-file=-
```

### 2. Customize the Dockerfile for Your Language Toolchain

Modify `Dockerfile` to include the compilers, language runtime, and build tools needed to build and test your codebase:

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

# Build and push container image
gcloud builds submit --tag us-central1-docker.pkg.dev/$PROJECT_ID/codemender-runner/orchestrator:latest .
```

### 4. Deploy Cloud Run Job

> [!IMPORTANT]
> **Resource Requirements**: Cloud Run deployments **MUST** use the **Cloud Run Gen 2 Execution Environment** with a minimum of `--ephemeral-storage=10Gi` (or higher) to safely hold large git histories, build caches, and test artifacts without exhausting RAM.

Deploy the Cloud Run Job using `gcloud`:

```bash
gcloud run jobs create codemender-nightly-scan \
    --image=us-central1-docker.pkg.dev/$PROJECT_ID/codemender-runner/orchestrator:latest \
    --region=us-central1 \
    --execution-environment=gen2 \
    --ephemeral-storage=10Gi \
    --memory=4Gi \
    --cpu=2 \
    --set-env-vars="GITHUB_REPO_URL=https://github.com/your-org/your-repo.git,CODEMENDER_BUILD_COMMAND='npm install && npm test'" \
    --set-secrets="GITHUB_APP_TOKEN=GITHUB_APP_TOKEN:latest"
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

---

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
