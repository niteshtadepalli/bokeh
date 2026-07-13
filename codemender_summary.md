# CodeMender Architecture Summary

## Overview
CodeMender is an enterprise-grade, AI-powered system designed to find and fix security vulnerabilities in codebases. By integrating Large Language Models (LLMs) with robust program analysis tooling (static scanning, execution-based validation, and test running), CodeMender reduces complex vulnerability reasoning into automated, actionable patches. It is designed to be highly secure, resilient, and integrated directly into developer workflows via CLI and IDE extensions.

## Major Subdirectories & Components

### 1. `cmoc` (CodeMender on Cloud)
- **Purpose**: The production server backend for CodeMender.
- **Functionality**: `cmoc` operates a client-server model offering Long-Running Operations (LRO) over gRPC/Stubby. Business logic (and LLM interactions) lives securely on Google's servers, while the client acts as a thin worker that executes tools locally. 
- **Architecture**: It uses a shared Spanner database to maintain session state, ensuring server replicas remain stateless. Agent pipelines traverse multiple stages: Classifier, Investigator, Validator, Critique, and Fix. Tool requests to read files or run tests are relayed back to the client securely via a polling loop without requiring any inbound network connections.

### 2. `jetski`
- **Purpose**: The SecureCoder IDE integration for Antigravity (Google's VS Code-based internal IDE).
- **Functionality**: Connects the developer's local workspace to CodeMender. The VS Code extension delegates to local scanners (like Semgrep or Wiz CLI) and converts findings into UI diagnostics, CodeLens actions ("Fix with SecureCoder"), and Hover elements. 
- **Architecture**: It includes a standalone agent CLI and registers Antigravity "plugins" that inject specific agent skills (e.g., threat modeling, PoC generation) so the LLM can resolve issues comprehensively within the IDE's Agent panel.

### 3. `runner`
- **Purpose**: A command-line execution harness for CodeMender agents.
- **Functionality**: Provides a binary interface for onboarding repositories, finding vulnerabilities (using task JSON files), and generating fixes. 
- **Architecture**: It can run agents locally or within sandboxed XBOX environments. `runner` supports resuming from checkpoints and can be deployed as an autonomous background sidecar (via Marina).

### 4. `api` & `proto`
- **Purpose**: Interface and message format definitions.
- **Functionality**: `proto` contains Protocol Buffer definitions (`tasks.proto`, `codemender_service.proto`) specifying the data formats for sessions, tasks, and findings. `api` contains Stubby service specs and basic clients for programmatic integrations.

### 5. `codemender_agent_plugin`
- **Purpose**: A standalone Model Context Protocol (MCP) server.
- **Functionality**: Exposes security scanning capabilities (leveraging `semgrep-core`) and vulnerability suppression (ignore lists) via MCP tools like `scan_file` and `ignore_vulnerability`. It allows any MCP-compatible agent (like Claude Code or Gemini) to interact with CodeMender functionality locally.

### 6. `benchmarks`
- **Purpose**: Continuous evaluation and performance measurement.
- **Functionality**: Includes tools and configurations using the `patcheval` framework running on XManager. It assesses the Pass@1/Pass@4 success rates of various CodeMender configurations (e.g., vanilla baseline vs. full agent with skills), generating side-by-side comparison reports.

### 7. `utils` & `datatypes`
- **Purpose**: Shared infrastructure and domain models.
- **Functionality**: `utils` houses LLM client wrappers (Gemini, Anthropic), XBOX/Riptide sandbox helpers, OCI image management, and token usage trackers. `datatypes` provides the Pydantic/dataclass models representing findings, tasks, and environments used across the Python codebase.

## Component Interactions

1. **Initiation**: A developer initiates a CodeMender scan through the IDE (`jetski` extension) or via the CLI (`cmoc/clients` or `runner`).
2. **Session Creation**: The client sends a request to the `cmoc` server, which initializes an asynchronous session persisted in Spanner.
3. **Agent Pipeline**: The server orchestrates LLM pipelines. For example, during a "Find" task, the code moves through Classifier -> Investigator -> Validator -> Critique. 
4. **Secure Tool Execution**: If the server-side agent needs local context (e.g., reading a file, running a shell command), it stores a `ToolRequest` in Spanner. The client, running a continuous polling loop, fetches the request, executes it securely in its local workspace or sandbox, and returns the result.
5. **Remediation & Reporting**: Once the vulnerability is confirmed, the "Fix" agent pipeline generates a patch. The client then surfaces the patch in the IDE or stages it via VCS (`cm vcs`), alongside generating reports in various formats (SARIF, Markdown, HTML).
