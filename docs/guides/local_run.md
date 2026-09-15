# CodeMender Orchestrator: Local Run Guide

This guide provides step-by-step instructions to run and test the CodeMender
Orchestrator script locally on your workstation.

--------------------------------------------------------------------------------

## Prerequisites

Before running the orchestrator, ensure you have the following installed on your
machine:

1.  **Python 3.8+** (with `pip` package manager)
2.  **Git** (CLI client)
3.  **CodeMender CLI (`cm`)**:
    -   For a **real run**: Ensure the `cm` binary is installed and available in
        your shell's `PATH`.
    -   For a **mock/dry run**: Follow
        [Step 4](#step-4-optional-create-a-mock-cm-cli-for-testing) to create a
        mock `cm` executable.

--------------------------------------------------------------------------------

## Step-by-Step Execution Guide

### Step 1: Navigate to the Agent Repository

Open your terminal and change directory to the repository folder:

```bash
cd /path/to/codemender-agent
```

### Step 2: Install Python Dependencies (Optional)

> [!NOTE]
> **Zero-Setup Testing**: The orchestrator is designed with safe import
> fallbacks. If the `google-cloud-storage` library is missing locally, the
> script automatically switches to internal dummy mocks, allowing you to run
> unit tests (`python3 -m unittest discover tests`) and dry-runs **without
> installing any local dependencies**.

If you need to install the dependencies locally (e.g. to test actual GCS
uploads), ensure `pip3` is installed on your workstation, then run:

```bash
pip3 install -r requirements.txt
```

### Step 3: Make the Orchestrator Executable (Optional)

This is only required if you want to execute the script directly as
`./orchestrator.py`. If you run it using `python3 orchestrator.py`, you can skip
this step.

```bash
chmod +x orchestrator.py
```

### Step 4: (Optional) Create a Mock `cm` CLI for Testing

If you do not have the real `cm` backend set up locally, or want to dry-run the
Git cloning, branch switching, pushing, and GitHub Pull Request loop without
calling actual AI models, create a mock `cm` script:

1.  Create a file named `cm` in `/tmp/bin/` (or any directory in your `$PATH`):

    ```bash
    mkdir -p /tmp/bin
    cat << 'EOF' > /tmp/bin/cm
    #!/bin/bash
    # Mock CodeMender CLI
    CMD=$1
    shift

    case "$CMD" in
      init)
        echo "Mock CM: Initialized."
        exit 0
        ;;
      find)
        if [ "$1" = "verify" ]; then
          echo "Mock CM: Verified finding $2."
          exit 0
        else
          echo "Mock CM: Scanning code path."
          exit 0
        fi
        ;;
      report)
        # Outputs a mock vulnerability finding schema
        echo '[
          {
            "FindingID": "11111111-2222-3333-4444-555555555555",
            "Title": "SQL Injection in database handler",
            "FilePath": "app/db.py",
            "Severity": "HIGH",
            "VulnType": "SQL_INJECTION",
            "Analysis": "Unsanitized user input concatenated in SQL query.",
            "Status": "OPEN"
          }
        ]'
        exit 0
        ;;
      fix)
        echo "Mock CM: Applying fix for finding $1..."
        # Simulate file modifications for git add -u to pick up
        if [ -f "app/db.py" ]; then
          echo "# Fixed SQLi" >> app/db.py
          exit 0
        else
          echo "Error: app/db.py not found during fix."
          exit 1
        fi
        ;;
      *)
        echo "Mock CM: Unknown command $CMD"
        exit 1
        ;;
    esac
    EOF
    chmod +x /tmp/bin/cm
    ```

2.  Add `/tmp/bin` to your environment path:

    ```bash
    export PATH="/tmp/bin:$PATH"
    ```

### Step 5: Configure GitHub Token & Target Repository

Generate a GitHub Personal Access Token (PAT) with `repo` scopes. Set the
following environment variables:

```bash
# The Git repository to scan and fix (supports HTTP/HTTPS and SSH formats)
export GITHUB_REPO_URL="https://github.com/your-username/your-test-repo.git"

# Your GitHub PAT or App Installation Token
export GITHUB_PAT="ghp_your_github_token_here"

# (Optional) Clean workspace directory to clone the target repository
export WORKSPACE_DIR="/tmp/codemender-workspaces"
mkdir -p "$WORKSPACE_DIR"

# (Optional) Set to true to bypass PR checks and force push code updates
export CODEMENDER_FORCE_OVERWRITE="false"

# (Optional) GCS bucket to upload and sign the final HTML summary report
export CODEMENDER_REPORT_BUCKET="my-gcs-reports-bucket"

# (Optional) Targeted subdirectory path(s) to restrict the initial scan scope.
# Multiple directories can be specified as a semicolon-separated or comma-separated list (e.g. "api;server/shared").
# Defaults to "." (the entire repository) if not specified.
export CODEMENDER_SCAN_TARGET="api;server/shared"

# (Optional) CodeMender CLI Version mode ("preview" or "legacy")
export CODEMENDER_CLI_VERSION="preview"

# (Optional) Custom LLM model selection overrides
export CODEMENDER_MODEL="gemini-2.5-flash"
# export CODEMENDER_FIND_MODEL="gemini-2.5-pro"
# export CODEMENDER_SKIP_EXPLOIT_VERIFICATION="true"
# export CODEMENDER_SKIP_VERIFY="false" # Set to false to run cm verify before cm fix (default: true)
# export CODEMENDER_PR_REMEDIATION_MODE="review_suggestion" # "review_suggestion" (default) or "child_pr"
```

> [!NOTE]
> **GCS Report Authorization**: If you configure
> `CODEMENDER_REPORT_BUCKET` for local runs, you must ensure your terminal is
> authenticated with GCP and local Application Default Credentials (ADC) are set
> up. Run:
>
> ```bash
> gcloud auth application-default login
> ```

### Step 6: Execute the Orchestrator

Run the script:

```bash
python3 orchestrator.py
```

--------------------------------------------------------------------------------

## Verifying Success

If the run is successful, you should see logs showing:

1.  The target repository cloned or fetched under `$WORKSPACE_DIR`.
2.  Git committer identity configured.
3.  The codebase scanned for findings.
4.  For each finding:
    -   Fix synthesized with `cm fix` (or optionally verified first if `CODEMENDER_SKIP_VERIFY="false"`).
    -   Remediation applied via inline review suggestions on PR diff, or dedicated fix branch pushed and Pull Request opened on GitHub (with PR URL printed in logs).
5.  Clean workspace reset back to default branch.
