# CodeMender Pre-Submit Security Gate — Stakeholder Demo

This documentation-only Pull Request demonstrates **Scenario 1: Non-Code PR Fast-Skip (`skip_scan=true`)**.

Because this PR modifies only Markdown documentation (`docs/SECURITY_GATE_DEMO.md`) and zero source code files:
1. Stage 1 `preflight` (`resolve_pr_diff_targets()`) detects `0` modified source code files and emits `skip_scan=true`.
2. GCP Workload Identity Federation (WIF), `cm` CLI installation, `cm find`, Stage 2 `worker` matrix, and Stage 3 `aggregate` are all skipped.
3. The `CodeMender / Security Gate` commit status check is marked **PASSED** in ~12 seconds at zero LLM token cost.
