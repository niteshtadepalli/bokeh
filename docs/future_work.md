# CodeMender Orchestrator: Future Work & Technical Debt Roadmap

This document outlines architectural refactorings, reliability improvements, and performance optimizations planned for future iterations of CodeMender Orchestrator.

---

## 1. Modular Service Layer Refactoring (Deconstructing "God Runners")
- **Context:** The runner modules (`runners/scan.py`, `runners/worker.py`, `runners/aggregate.py`) currently mix subprocess execution, Git operations, SQLite schema operations, Cloud Storage operations, PR routing, Markdown rendering, and SARIF transformation into monolithic files.
- **Planned Work:** Decompose the runner pipelines into decoupled service classes adhering to the Single Responsibility Principle:
  - `VCSStagingService`: Encapsulates Git cloning, branch creation, surgical staging, diff parsing, and GitHub PR creation.
  - `DatabaseMergerService`: Encapsulates SQLite schema inspection, table merging, and conflict resolution.
  - `ReportGenerationService`: Encapsulates Markdown Step Summary rendering, SARIF path sanitization, and HTML token metric injection.
  - `CLIExecutionService`: Encapsulates CodeMender CLI process execution, model flag injection, and port cleanup.

---

## 2. Structured Query Building for SQLite State Merging
- **Context:** In `runners/aggregate.py:merge_db`, SQL `UPDATE` and `INSERT` clauses are synthesized dynamically via string interpolation with Python f-strings.
- **Planned Work:**
  - Transition SQLite operations to parameterized query helpers or lightweight table mappers.
  - Enforce explicit schema migrations for worker state databases to ensure schema backward compatibility across CLI versions.

---

## 3. Granular Exception Handling & Error Propagation
- **Context:** Several areas currently use broad `except Exception:` blocks or swallow errors (e.g., in JSON decoding patch files, copying SARIF reports, or querying SQLite tables).
- **Planned Work:**
  - Audit and replace all broad `except Exception:` blocks with specific exception classes (`json.JSONDecodeError`, `sqlite3.DatabaseError`, `subprocess.CalledProcessError`, `FileNotFoundError`).
  - Introduce structured error logging that preserves stack traces and distinguishes transient retriable errors from unrecoverable system faults.

---

## 4. Typed Domain Exception Hierarchy
- **Context:** Deeply nested helper functions invoke `sys.exit(1)` directly, hindering caller cleanup, context management, and isolated unit testing.
- **Planned Work:**
  - Introduce a comprehensive domain exception hierarchy:
    ```python
    class CodeMenderError(Exception): """Base exception."""
    class ConfigurationError(CodeMenderError): """Invalid configuration."""
    class VCSOperationError(CodeMenderError): """Git or GitHub operation failed."""
    class StorageOperationError(CodeMenderError): """Transit or GCS operation failed."""
    class RunnerExecutionError(CodeMenderError): """Stage execution failed."""
    ```
  - Refactor leaf functions to raise typed domain exceptions and allow top-level entrypoints (`orchestrator.py`) to handle exit codes, cleanup, and telemetry.

---

## 5. Typed Command Execution Results
- **Context:** `codemender_agent/utils.py:run_command` monkey-patches standard library `subprocess.CompletedProcess` instances by dynamically attaching a `token_usage` attribute (`res.token_usage = token_usage`).
- **Planned Work:**
  - Define a dedicated dataclass:
    ```python
    @dataclass
    class CommandResult:
      completed_process: subprocess.CompletedProcess
      token_usage: Optional[dict[str, int]] = None
    ```
  - Update all consumers to use the typed wrapper.

---

## 6. Scoped Subprocess Output Token Parsing
- **Context:** `run_command` evaluates regular expression pattern matches across `full_stdout` on every command executed by the system (including frequent Git queries like `git status` or `git rev-parse`).
- **Planned Work:**
  - Parameterize `run_command` with `parse_tokens: bool = False`.
  - Enable token parsing strictly when executing `cm` CLI actions (`find`, `verify`, `fix`).

---

## 7. Concurrent Multi-Worker Shard Download in Aggregator
- **Context:** In Stage 3 (Aggregate), worker database shards and token metadata JSON files are downloaded sequentially in a `for` loop.
- **Planned Work:**
  - Parallelize shard downloads using `concurrent.futures.ThreadPoolExecutor` to minimize aggregate stage latency on large worker matrices (e.g., 50+ workers).

---

## 8. Subprocess Execution Timeouts & Process Group Management
- **Context:** In `codemender_agent/utils.py:run_command`, child subprocesses (such as build commands, test runners, or long-running CLI invocations) stream standard output synchronously line-by-line.
- **Planned Work:**
  - Introduce configurable execution timeouts for long-running commands.
  - Launch child processes in dedicated process groups (`start_new_session=True` / `os.setsid`) to ensure all subprocess children are cleanly killed via `SIGTERM`/`SIGKILL` on timeout, preventing orphaned processes and pipe deadlocks.

---

## 9. Workflow Concurrency Cancellation Groups for Rapid PR Pushes
- **Context:** When rapid commits are pushed to the same Pull Request, GitHub Actions initiates parallel workflow runs that compete for LLM quota and race to create Child PRs.
- **Planned Work:**
  - Introduce top-level workflow concurrency cancellation groups in caller or reusable workflows:
    ```yaml
    concurrency:
      group: codemender-${{ github.workflow }}-${{ github.event.pull_request.number || github.ref }}
      cancel-in-progress: true
    ```

---

## 10. Robust Fork PR Head Commit Resolution via Pull Refs
- **Context:** When a pull request originates from a fork repository, the head commit SHA is not present in default branch heads of upstream origin. Directly running `git fetch origin <target_sha>` may fail on remote servers that disable fetching unadvertised raw SHAs.
- **Planned Work:**
  - Update `_sync_repository` (Scan), `_setup_git_and_checkout` (Worker), and `run_aggregate_pipeline` (Aggregator) to fetch GitHub's guaranteed pull ref `refs/pull/<pr_number>/head` whenever `is_pr_scan` is true and `pr_number` is provided:
    ```python
    if is_pr_scan and pr_number:
      fetch_pr_cmd = ["git", "-c", get_git_auth_header(token), "fetch", "origin", f"refs/pull/{pr_number}/head"]
      run_command(fetch_pr_cmd, cwd=repo_dir, check=False)
    ```


