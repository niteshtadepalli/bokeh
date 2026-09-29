# CodeMender Orchestrator: Implementation Guardrails & Source of Truth

## 1. The Problem

The central CodeMender AI backend provides powerful, LLM-driven vulnerability
finding and fixing capabilities. However, to prove that a generated fix actually
works, CodeMender must run the repository's specific compilers, linters, and
test suites.

A centralized backend cannot securely or practically host the thousands of
diverse, proprietary build environments and toolchains required by different
engineering teams. Furthermore, attempting to run all these execution
environments centrally would create massive compute bottlenecks and quickly
exhaust global API rate limits.

Therefore, engineering teams require a decentralized, automated orchestration
script (the "CodeMender Orchestrator"). This orchestrator must run within the
team's own secure infrastructure (e.g., as a Cloud Run Job) to:

1.  Provide the specific local environment and toolchain needed to validate
    fixes.
2.  Automate the end-to-end Git workflow (cloning, branching, pushing, and
    creating PRs).
3.  Distribute the compute load and bypass centralized rate limits by executing
    nightly batch scans locally.

## 2. The Technical Plan

At a high level, the system relies on four main components working together
securely:

1.  **The CodeMender Backend (The "Brain"):** Google's central servers run the
    AI models. They analyze the code, reason about the vulnerabilities, and
    suggest fixes. However, the backend never directly accesses a team's private
    repository or runs their build tools.
2.  **The CodeMender CLI (The "Translator"):** A small binary tool that runs
    locally. It handles the secure communication with the backend and translates
    the AI's suggestions into local file edits and test commands.
3.  **The Custom Environment (The "Workshop"):** A Docker container (e.g., a
    Google Cloud Run Job) built and owned by the engineering team. It contains
    the orchestrator, the CodeMender CLI, and the exact compilers/tools (like
    Node.js, Go, or Java) needed to build that team's specific codebase.
4.  **The Orchestrator Script (The "Manager"):** The script we are building. It
    runs inside the Custom Environment, typically on a nightly schedule. It acts
    as the manager: cloning the code from Git, telling the CLI to find bugs,
    telling the CLI to fix those bugs, and finally taking the successful fixes
    and opening Pull Requests for human review.

## 3. Alternatives Considered & Future Work

During the design phase, several alternative approaches were discussed and ruled
out or deferred to keep the initial Minimum Viable Product (MVP) focused and
reliable:

### Ruled Out

*   **Centralized "Fat" Images:** We considered having the central CodeMender
    backend execute the fixes using a massive Docker image containing every
    popular compiler. This was rejected because maintaining versions for every
    language (Node, Go, Java, Python) is an impossible maintenance burden.
*   **Google-Internal Traffic Prioritization:** We initially explored using
    internal `SHEDDABLE` QoS tags to prevent nightly batch scans from
    overwhelming the backend. This was rejected because external GCP traffic
    (the BYOP Cloud Run jobs) cannot set internal criticalities. Instead, the
    orchestrator script must handle standard HTTP 429 rate-limit backoffs.
*   **Complex PR Grouping:** Grouping multiple findings into a single Pull
    Request to avoid "PR spam" was discussed. For the MVP, we chose a simpler,
    more robust sequential loop (one finding = one branch/PR) to avoid merge
    conflicts across multiple automated patches.
*   **Hardcoded Preview API Key Strategy:** Distributing preview binaries with
    hardcoded embedded API keys is accepted as a temporary measure for the MVP
    experience.

### Deferred to Future Work

*   **Parallelization:** To speed up scans on massive repositories, we discussed
    fanning out the fixes across parallel Cloud Run tasks using a shared Google
    Cloud Storage (GCS) state. The MVP will instead use a straightforward
    sequential loop.
*   **Thundering Herd Problem Mitigation:** Implementing randomized jitter
    (e.g., sleeping a random duration before starting) for nightly scheduled
    jobs to avoid thousands of concurrent jobs overwhelming the central backend
    simultaneously. Manual testing will be performed first.
*   **Robust Rate-Limit (429) Handling:** Currently deferred for the MVP. Future
    work will evaluate handling HTTP 429 backoffs directly within the CLI binary
    rather than trying to wrap and retry CLI executions from the Python script.
*   **Job Checkpointing & Timeouts:** Cloud Run Jobs have maximum timeouts. For
    massive repositories, the MVP might fail if it exceeds the timeout. Future
    work will include checkpointing progress (e.g., skipping findings that
    already have open PRs) to resume gracefully after a timeout.
*   **Missing Dependency Authentication Context:** Injecting corporate registry
    tokens (like `.npmrc` or PyPI authentications) to resolve private
    dependencies during validation is deferred beyond the MVP.

## 4. Detailed Implementation

To build this Minimum Viable Product, we will create the following files in the
`codemender-agent` repository. No other files should be necessary.

### 1. `orchestrator.py`

*   **Purpose:** The core execution script (The "Manager").
*   **Rationale:** While Bash could theoretically run the CLI, Python is
    required for robust error handling, parsing the JSON output of `cm report
    --format json`, implementing credential scrubbing, and securely interacting
    with the Git remote API to create Pull Requests.
*   **Key Responsibilities:**
    *   Fetching the GitHub App token or Personal Access Token (PAT) from the
        environment.
    *   **Credential Scrubbing:** Explicitly removing `GITHUB_APP_TOKEN` and
        `GITHUB_PAT` from the environment passed to child subprocesses (`cm
        find`, `cm verify`, `cm fix`) to prevent exfiltration during untrusted
        LLM code execution.
    *   Running the single-sync Git workflow (`git clone --depth 1` at the
        beginning of the scan).
    *   Running `cm` commands via subprocess, passing the `--yes` (or `-y`) flag
        to prevent execution hangs in headless containers:
    *   `cm init` (and `cm init --verify`)
    *   `cm find .` (or specific paths)
    *   `cm report --format json` (to extract findings)
    *   `cm find verify <finding_id> --yes`
    *   `cm fix <finding_id> --yes`
    *   Controlling the sequential "Fix (optional Verify) -> Suggestion / PR" loop.
        By executing the remediation operation immediately after each
        successful fix (instead of waiting for the end of the entire loop), the
        orchestrator ensures partial progress is preserved if the job times out
        or fails midway.
    *   **PR Spam Prevention (Sliding Window Deduplication):** Because
        LLM-extracted code snippets fluctuate between scans (altering the
        default fingerprint), the orchestrator ignores the upstream fingerprint
        and generates a deterministic branch hash using `filePath`, `vulnType`,
        and `startLine`. To absorb minor LLM line number jitter and support
        multiple distinct vulnerabilities in the same file, the orchestrator
        also uses the GitHub API to query open PRs. It checks if an open PR
        exists for the same file and vulnerability type within a 15-line sliding
        window of the new finding's `startLine`. If found, it skips fixing to
        guarantee true idempotency and zero PR spam. Skipped findings are
        mutated in `state.db` as `status='DISMISSED'` and `muted=1` to retain telemetry
        and uploaded to GCS in the base workspace tarball, but are explicitly
        deleted from the database before generating the final HTML report.
    *   Pushing the fixed branch directly to remote and creating the PR via the
        GitHub REST API immediately after each fix is generated using the
        scrubbed token.

### 2. `Dockerfile`

*   **Purpose:** Defines the deployment container for Google Cloud Run.
*   **Rationale:** Cloud Run requires a container image. This Dockerfile serves
    as the template for the "Custom Environment". It will demonstrate how an
    engineering team packages the orchestrator script alongside the CodeMender
    CLI and their necessary build tools.
*   **Key Responsibilities:**
    *   Starting from a stable base image (e.g., `python:3.11-slim`).
    *   Installing required system dependencies (`git`, `curl`).
    *   Installing the `cm` (CodeMender) CLI binary (either by copying a locally
        available binary during the build or downloading it from an internal GCS
        bucket).
    *   Installing the Python dependencies.
    *   Setting `orchestrator.py` as the container entrypoint.

### 3. `requirements.txt`

*   **Purpose:** Lists the Python dependencies for the orchestrator.
*   **Rationale:** The orchestrator will need third-party libraries that aren't
    in the Python standard library to keep the code clean and maintainable.
*   **Key Responsibilities:**
    *   Specify `requests` for interacting with the GitHub API to open Pull
        Requests.
    *   Specify `PyJWT` or `PyGithub` if choosing the GitHub App authentication
        route (to generate JWTs from the private key).
    *   Specify `tenacity` or `backoff` (optional) to handle retry logic
        gracefully if network calls fail.

### 4. `README.md`

*   **Purpose:** Documentation for the engineering teams.
*   **Rationale:** Because this is a BYOP (Bring Your Own Project) model, the
    central security team must provide clear instructions to the engineering
    teams on how to deploy this orchestrator into their own GCP projects.
*   **Key Responsibilities:**
    *   Instructions on how to modify the Dockerfile for custom toolchains.
    *   Instructions on how to deploy the image to Cloud Run.
    *   Instructions on configuring the GitHub App token in Google Secret
        Manager.

## 5. Key Constraints & Execution Rules

To ensure the orchestrator works reliably in production and handles edge cases,
the code must strictly adhere to the following rules:

1.  **Required Environment Variables:** The `orchestrator.py` script must not
    hardcode any sensitive credentials or repository URLs. It must expect
    `GITHUB_REPO_URL` and `GITHUB_APP_TOKEN` (or similarly named variables) to
    be injected into the Cloud Run environment.
2.  **The "Single-Sync" Git Rule:** To minimize merge conflicts on highly active
    repositories, the script **MUST** sync only once at the beginning of the
    scan (`git clone --depth 1` or `git pull`). It then pushes branches directly
    to origin and creates PRs immediately after each fix is generated,
    delegating merge conflict resolution entirely to GitHub's PR mergeability
    check and dropping post-fix rebase steps.
3.  **Workspace Reset Rule:** Because the CodeMender CLI automatically runs `git
    checkout HEAD -- . && git clean -fd` inside its own commands before
    executing validation or fixes, manual workspace resets are not required in
    the Python script. However, the Python script **MUST** use forced checkout
    (`git checkout -f`) when switching branches.
4.  **Credential Scrubbing Rule:** The orchestrator **MUST** scrub sensitive
    credentials from subprocess environments before calling `cm verify` or `cm
    fix` to eliminate the RCE security exfiltration risk.
5.  **Resource Requirements:** The `README.md` must explicitly document that
    Cloud Run deployments should be configured to use the **Cloud Run Gen 2
    Execution Environment** with a minimum of `--ephemeral-storage=10Gi` (or
    higher) to safely hold large git history and build caches without exhausting
    container RAM.

## 6. Appendix: `cm report` JSON Schema

When `orchestrator.py` runs `cm report --format json`, the output conforms to
the following schema. The script will primarily extract the `FindingID` field to
pass to the `cm find verify` and `cm fix` commands. **Note:** Fields like
`DismissReason`, `ConfidenceLevel`, `Fingerprint`, and `UpdatedAt` may return as
empty strings (`""`) instead of being omitted. The orchestrator must handle
these empty strings as null/absent states rather than expecting non-empty
values.

```json
{
  "title": "VulnerabilityFindingsList",
  "type": "array",
  "items": {
    "type": "object",
    "properties": {
      "FindingID": {
        "type": "string",
        "format": "uuid",
        "description": "Unique UUID identifier for the finding."
      },
      "SessionID": {
        "type": "string",
        "format": "uuid",
        "description": "Unique UUID identifier for the analysis session."
      },
      "Title": {
        "type": "string",
        "description": "The title or name of the vulnerability."
      },
      "FilePath": {
        "type": "string",
        "description": "The absolute or relative file path where the vulnerability resides."
      },
      "Severity": {
        "type": "string",
        "enum": ["INFO", "LOW", "MEDIUM", "HIGH", "CRITICAL"],
        "description": "The severity rating of the vulnerability."
      },
      "Confidence": {
        "type": "integer",
        "minimum": 0,
        "maximum": 100,
        "description": "The confidence score of the finding as a percentage."
      },
      "Analysis": {
        "type": "string",
        "description": "Detailed technical analysis explaining how the vulnerability functions."
      },
      "Snippet": {
        "type": "string",
        "description": "The code snippet containing the vulnerable logic."
      },
      "VulnType": {
        "type": "string",
        "description": "High-level classification of the vulnerability type (e.g., XXE, RCE, IDOR)."
      },
      "VulnID": {
        "type": "string",
        "description": "Reference identifier, such as a CWE ID or a custom compound key."
      },
      "Fingerprint": {
        "type": "string",
        "description": "A unique structural code fingerprint, if generated."
      },
      "Status": {
        "type": "string",
        "enum": ["OPEN", "VERIFIED", "FALSE_POSITIVE", "RESOLVED"],
        "description": "The triage status of the vulnerability finding."
      },
      "SourceStage": {
        "type": "string",
        "description": "The stage of the pipeline where this finding was sourced."
      },
      "FindingJSON": {
        "type": "string",
        "description": "Raw underlying finding details represented as a stringified JSON blob."
      },
      "UpdatedAt": {
        "type": "string",
        "description": "Timestamp indicating when this finding was last modified."
      },
      "StartLine": {
        "type": "integer",
        "description": "Starting line number of the vulnerable block."
      },
      "EndLine": {
        "type": "integer",
        "description": "Ending line number of the vulnerable block."
      },
      "DismissReason": {
        "type": "string",
        "description": "Reason given if the finding was dismissed or ignored."
      },
      "ConfidenceLevel": {
        "type": "string",
        "description": "Qualitative confidence level descriptor."
      }
    },
    "required": [
      "FindingID",
      "SessionID",
      "Title",
      "FilePath",
      "Severity",
      "Confidence",
      "Analysis",
      "Snippet",
      "VulnType",
      "VulnID",
      "Fingerprint",
      "Status",
      "SourceStage",
      "FindingJSON",
      "UpdatedAt",
      "StartLine",
      "EndLine",
      "DismissReason",
      "ConfidenceLevel"
    ],
    "additionalProperties": false
  }
}
```
