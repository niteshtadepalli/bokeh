# CodeMender Orchestrator: Configuration Reference

The CodeMender Orchestrator uses environment variables and project-level YAML
files to configure its behavior, authentication, and execution modes.

--------------------------------------------------------------------------------

## 1. Environment Variables

Environment variables are the primary method for configuring the orchestrator
when running in Docker or Cloud Run.

### Authentication & Repository (Required)

*   `GITHUB_REPO_URL`: Full HTTPS URL to the target GitHub repository (e.g.,
    `https://github.com/owner/repo`).
*   `GITHUB_APP_TOKEN` (or `GITHUB_PAT`, `GITHUB_TOKEN`, `GH_TOKEN`,
    `GITHUB_SECRET`): The authentication token used for cloning the repository,
    authenticating with the GitHub REST API, and pushing branches. *Note: These
    are explicitly scrubbed from child subprocesses for security.*

### Pipeline Customization

*   `CODEMENDER_BUILD_COMMAND`: Overrides the project's default build or test
    command (e.g., `npm test`). If not provided, the orchestrator prompts
    interactively (if running in a TTY).
*   `CODEMENDER_SCAN_TARGET`: The directory path within the repository to scan
    (defaults to `.`).
*   `CODEMENDER_CLEANUP_PORTS`: A comma-separated list of local ports to
    force-kill before executing the `CODEMENDER_BUILD_COMMAND`. This prevents
    port-collision failures during testing.
    *   *Default*: `3000, 3001, 5000, 8000, 8080, 8081, 9000`.
*   `CODEMENDER_FORCE_OVERWRITE`: If set to `true`, bypasses the "PR Spam
    Prevention" check. The orchestrator will attempt to recreate and force-push
    verify/fix branches even if they already exist on the remote.
*   `CODEMENDER_REPORT_BUCKET`: The name of a Google Cloud Storage (GCS) bucket
    where the final HTML summary report should be uploaded (primarily used in
    sequential mode).

### Execution Modes

*   `CODEMENDER_RUN_MODE`: Determines the orchestrator's behavior.
    *   `sequential` (default): Runs the entire scan, verify, and fix loop
        sequentially in a single process.
    *   `scan`: (Parallel Stage 1) Scans the repository, generates findings, and
        partitions them for workers.
    *   `worker`: (Parallel Stage 2) Downloads a specific partition of findings
        and processes the fixes.
    *   `aggregate`: (Parallel Stage 3) Merges all worker results into a final
        consolidated database and report.

### Parallel Execution State (Internal)

These variables are automatically injected by the Cloud Workflows coordinator
during parallel runs. **You do not need to set these manually.**

*   `CODEMENDER_SCAN_ID`: Unique identifier for the parallel scan run.
*   `CODEMENDER_GCS_BUCKET`: GCS bucket used for storing intermediate parallel
    state, workspaces, and manifests.
*   `CODEMENDER_MAX_TASKS`: Maximum number of parallel worker tasks (containers)
    to launch in Stage 2.
*   `CODEMENDER_TARGET_SHA`: The Git commit SHA representing the point-in-time
    codebase. Ensures all parallel workers branch from the exact same commit.
*   `CODEMENDER_BASE_WORKSPACE_URL`: GCS URL of the compressed, pre-scanned base
    repository workspace.
*   `CODEMENDER_PARTITION_URLS`: JSON array of GCS URLs containing the specific
    findings a worker should process.
*   `CODEMENDER_UPLOAD_URLS`: JSON array of GCS URLs where a worker should
    upload its resulting state databases.
*   `CODEMENDER_TOTAL_WORKERS`: The total number of workers spawned, used by the
    aggregator to know how many databases to merge.
