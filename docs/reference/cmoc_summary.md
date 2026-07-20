# CMOC — CodeMender on Cloud

**Architecture & Deep Dive Summary**

CodeMender on Cloud (CMOC) is a multi-stage AI pipeline that scans source code
for security vulnerabilities and generates validated patches. It leverages
Agency-powered LLM agents using a distributed client-server architecture.

## 1. High-Level Architecture

CMOC employs an **AIP-151 Long-Running Operations (LRO) API** via Stubby
(historically gRPC). The architecture relies on an "inversion of control" model
where the server coordinates the AI agent's reasoning, but tools (e.g.,
`run_command`, `read_file`) are executed strictly on the client side. This
design preserves the client's local workspace context, auth context, and sandbox
boundaries.

### Interaction Flow (The Polling Mechanism)

1.  **Initiation**: The client calls `StartSession(StartSessionRequest)`. The
    server persists the session into its database (Spanner) and immediately
    returns an `Operation` object.
2.  **Worker Claim**: On the server, a background `WorkerReceiver` thread (or
    push queue) claims the queued session via a distributed lease and launches
    the Agency AI pipeline (`CLASSIFIER`, `INVESTIGATOR`, etc.).
3.  **Long Polling**: The client enters a polling loop in
    `clients/.../polling/loop.go`, calling `WaitOperation`. The server blocks
    this request for up to 5 seconds to minimize idle polling.
4.  **Tool Execution**: When the AI agent decides to use a tool, the server
    halts the agent, sets the session status to `WAITING_FOR_TOOL`, and returns
    the `ToolRequest` via the unblocked `WaitOperation`.
5.  **Client Response**: The client locally executes the tool using its
    dispatcher, sends a `KeepAlive` heartbeat if the tool is slow, and submits
    the output via `SubmitToolResult`.
6.  **Resumption**: The server transitions the session back to `RUNNING`, waking
    the agent with the tool's output, while the client resumes long-polling.
7.  **Completion**: When the pipeline concludes, the server sets
    `Operation.done = true`. The client receives the `SessionResult` containing
    the findings or file edits.

## 2. Directory Structure & Key Components

### `proto/` (Service Interfaces)

Contains `codemender_service.proto` which defines the exact contract between
client and server.

-   **Key RPCs**: `StartSession`, `SubmitToolResult`, `ResumeSession`,
    `GetStageReport`, `KeepAlive`. (It also mixes in
    `google.longrunning.Operations`).
-   **Models**: Defines `ToolRequest`, `FileContentResult`, `CommandResult`, and
    `SessionResult` (which holds `Finding`s and `FileEdit`s).

### `server/` (Backend Logic)

The Python-based production server responsible for managing sessions and running
the AI pipeline.

-   **`stubby/servicer.py`**: The live RPC handler. It validates auth (LOAS3 /
    Ed25519) and handles rate limits/admission control.
-   **`worker.py`**: The background processor. It claims sessions, renews
    leases, and routes the session to the appropriate pipeline processor.
-   **`find_session.py` & `fix_session.py`**: The main orchestration layers.
    They break down the task into `PipelineStage`s (e.g., `SCAN`, `FIND`,
    `FULL`, `VERIFY`). Each stage utilizes a specific agent prompt (from the
    `prompts/` directory) and passes contextual state to the Agency SDK runner.
-   **`structured_thinking*.py`**: Alternate pipelines allowing complex
    reasoning topologies.
-   **`storage/`**: The persistence layer. Supports in-memory for testing and
    Spanner for production. Spanner handles deduplication (blocking duplicate
    scans on the same project), progress tracking, and checkpointing.

### `clients/` (CLI & Polling)

Contains the client logic. Divided into `blaze_cli` (internal Google3 using
LOAS) and `standalone_cli` (cross-platform external).

-   **`internal/polling/loop.go`**: The core polling logic. It drives the
    `WaitOperation` calls, incrementally saves streaming findings, updates the
    UI with curated progress entries, and executes `ToolRequest`s via local
    plugins/dispatchers.
-   **Heartbeats**: It includes a `StartHeartbeat` mechanism that sends
    `KeepAlive` requests during slow tool executions to prevent the server from
    timing out the session lease.

### `docs/` (Design Documents)

Extensive architecture and design documentation.

-   **`codemender_architecture.md`**: Broad overview.
-   **`codemender_server_design.md` & `codemender_client_design.md`**: Granular
    design docs.
-   **`codemender_session_management.md`**: Describes the state machine
    (`QUEUED` → `RUNNING` ↔ `WAITING_FOR_TOOL` → `COMPLETED`/`FAILED`).

### `evals/` (Evaluation Infrastructure)

Contains infrastructure for benchmarking CMOC. Includes scripts for patch
evaluation (`patcheval`), stability measurements, parsing evaluation
trajectories, and automated testing via sandbox configurations.

### `pod/` (Deployments)

Contains Borg/Kubernetes pod definitions to deploy the `codemender_stubby`
server securely inside Google's infrastructure.

## 3. Resilience & Security

-   **Stateless RPCs**: The Stubby handlers have no in-memory session state; all
    state lives in Spanner, allowing GFEs to freely load-balance requests.
-   **Checkpoints & Resumability**: AI stages are checkpointed individually. If
    a session fails, `ResumeSession` allows it to restart from the last
    completed stage.
-   **Data Scrubbing**: Source code payloads and prompt configurations are
    aggressively scrubbed from the DB the moment a session enters a terminal
    state (`COMPLETED`, `FAILED`, `CANCELLED`).
