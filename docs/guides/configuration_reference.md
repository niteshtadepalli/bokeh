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
*   `GITHUB_APP_TOKEN` (or `GITHUB_PAT`, `GITHUB_TOKEN`): The authentication token
    used for cloning the repository, authenticating with the GitHub REST API,
    and pushing branches. *Note: One of these three variables is required by the
    orchestrator. Additional token variables (`GH_TOKEN`, `GITHUB_SECRET`) are
    also explicitly scrubbed from child subprocesses for security.*

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
*   `CODEMENDER_PR_REMEDIATION_MODE`: How fixes are delivered on Pull Request
    scans.
    *   `review_suggestion` (*default*): posts the patch as one-click inline
        GitHub review suggestions on the Pull Request itself. No branch is
        pushed.
    *   `child_pr`: pushes a `codemender/fix-...` branch and opens a Child Pull
        Request targeting the developer's branch.
    *   Fork Pull Requests ignore this flag and always use `review_suggestion`,
        because the orchestrator cannot push a branch to a fork.
    *   A patch that cannot be expressed entirely as inline suggestions (it
        creates, renames or deletes a file, or touches lines outside the Pull
        Request diff) automatically falls back: to a Child Pull Request on
        internal PRs, or to a Markdown patch comment with `git apply`
        instructions on fork PRs.
*   `CODEMENDER_REPORT_BUCKET`: The name of a Google Cloud Storage (GCS) bucket
    where the final HTML summary report should be uploaded (primarily used in
    sequential mode).
*   `WORKSPACE_DIR`: The local filesystem directory where target repositories
    are cloned (defaults to current working directory).
*   `CODEMENDER_STORAGE_MODE`: Storage backend mode for artifacts and reports.
    Defaults to GCS; set to `"local"` to use local filesystem storage for
    testing without GCP credentials.
*   `CODEMENDER_LOCAL_STORAGE_DIR`: The local directory to store mocked GCS
    blobs when `CODEMENDER_STORAGE_MODE="local"` (defaults to
    `/tmp/codemender_local_storage`).

### CodeMender Public Preview & Model Configuration

*   `CODEMENDER_CLI_VERSION`: Determines CLI flag compatibility syntax.
    *   `preview` (default): Uses Public Preview CLI flag syntax (`-y`, `--bypass-warning`, `--skip-exploit-verification`, `--model`).
    *   `legacy`: Uses legacy CLI flag syntax (`cm find verify <ID> --yes`, `cm fix <ID> --yes`).
*   `CODEMENDER_MODEL`: Sets the global LLM model selection override across all CodeMender stages. If omitted, CodeMender uses its latest default model. Check up-to-date defaults and supported models in the [CodeMender documentation](https://docs.cloud.google.com/gemini-enterprise-agent-platform/codemender#specifying-the-model).
*   `CODEMENDER_FIND_MODEL`: Overrides the LLM model specifically for the Stage 1 scan (`cm find`) phase.
*   `CODEMENDER_VERIFY_MODEL`: Overrides the LLM model specifically for the Stage 2 verification (`cm verify`) phase.
*   `CODEMENDER_FIX_MODEL`: Overrides the LLM model specifically for the Stage 2 fix (`cm fix`) phase.
*   `CODEMENDER_SKIP_EXPLOIT_VERIFICATION`: Set to `"true"` to append `--skip-exploit-verification` during the verification phase, skipping compilation and execution of exploits.
*   `CODEMENDER_SKIP_VERIFY`: Set to `"false"` to run `cm verify` before `cm fix`, on every platform including Cloud Run. Defaults to `"true"`, which skips the verification phase and proceeds directly to patch synthesis.
*   `CODEMENDER_FIND_FLAGS`, `CODEMENDER_VERIFY_FLAGS`, `CODEMENDER_FIX_FLAGS`: Extra flags for `cm find`, `cm verify` and `cm fix`, written as a shell-style string (for example `CODEMENDER_FIND_FLAGS="--deep --deep-workers 4"`). The orchestrator reads `cm <command> --help` from the installed binary and drops, with a warning, any flag that binary does not support, so a configuration written for a newer CLI release still runs on an older one. `--model` and `--unrestricted` are always ignored here: set models with the model variables above and the sandbox with `CODEMENDER_SANDBOX_ENABLED`. When the help output cannot be read, the flags are passed through unchanged.

### Wiz SAST Bridge (Optional, Opt-In Per Repository)

The scan stage can run a Wiz CLI SAST scan (`wizcli scan dir`, results not
published to Wiz) and import eligible findings into CodeMender. Imported
findings are labelled like `SQL Injection (CWE-89)` and are **always** sent
through `cm verify`, even when `CODEMENDER_SKIP_VERIFY` is `"true"`. Only
findings that verification marks `VERIFIED` go on to fix PRs; dismissed ones
keep CodeMender's reason. Fix workers recognise an imported finding both from
the scan stage's list and from the marker sentence the bridge puts at the start
of its description, so either signal alone is enough to force verification. The
bridge is a hard no-op unless `CODEMENDER_WIZ_ENABLED` is `"true"`; providing
credentials alone never enables it. A Wiz failure never fails the scan: the
scan continues with CodeMender's own findings and records `wiz_status` as
`enabled`, `not_enabled`, or `failed` in the report and in BigQuery. When Wiz
credentials are configured in the deployment, repositories that have not opted
in show a "not enabled" line in the report so that "not on Wiz" is never read as
"clean". The bridge currently runs in the parallel pipeline's scan stage only
(`CODEMENDER_RUN_MODE=scan`); `sequential` mode does not call it (it logs a
warning if enabled), but still verifies any imported finding already in state
before fixing it.

*   `CODEMENDER_WIZ_ENABLED`: `"true"` to run the bridge for this scan. Defaults to off.
*   `CODEMENDER_WIZ_MIN_SEVERITY`: Lowest Wiz severity to import (`INFO`, `LOW`, `MEDIUM`, `HIGH`, `CRITICAL`). Defaults to `HIGH`.
*   `WIZ_CLIENT_ID` / `WIZ_CLIENT_SECRET`: Wiz service account credentials. They are removed from the process environment at start-up and passed only to `wizcli`, never to `cm` or any other subprocess, and are never logged.
*   `CODEMENDER_WIZ_LINE_WINDOW`: Lines of slack when matching a Wiz finding to a CodeMender finding for the same weakness in the same file. Defaults to `3`.
*   `CODEMENDER_WIZ_MAX_IMPORTS`: Maximum findings imported per scan, highest severity first. Defaults to `50`.
*   `CODEMENDER_WIZ_SCAN_TIMEOUT_SECONDS`: Timeout for the Wiz scan. Defaults to `900`.
*   `CODEMENDER_WIZCLI_PATH`: Path to a pre-installed `wizcli`. Otherwise `wizcli` on `PATH` is used, or a pinned version is downloaded and checked against its SHA-256 (`CODEMENDER_WIZCLI_VERSION`, `CODEMENDER_WIZCLI_SHA256`, `CODEMENDER_WIZCLI_URL`).
*   `CODEMENDER_WIZ_RESULTS_FILE`: Use an existing `wizcli` JSON result instead of running a scan (testing only).
*   `CODEMENDER_WIZ_SCAN_NAME`: Scan name passed to `wizcli`. Defaults to `codemender-sast-bridge`.

With Terraform, enable the bridge per repository in `target_repositories`:

```hcl
target_repositories = {
  "my-repo" = {
    repo_url = "https://github.com/org/my-repo.git"
    wiz      = { enabled = true, min_severity = "HIGH" }
  }
}
```

The two credentials must already exist in Secret Manager as
`<resource_prefix>-wiz-client-id` and `<resource_prefix>-wiz-client-secret`
(override with `wiz_client_id_secret_id` / `wiz_client_secret_secret_id`).
Terraform only reads them; it never creates, changes, or deletes them. When
at least one repository opts in, Terraform grants the runner service account
`secretAccessor` on both and mounts them on the runner job. The worker job
never receives them.

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
