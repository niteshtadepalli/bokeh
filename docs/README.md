# Documentation index

## Customer documentation

These are the documents the customer handoff contains. The export
(`scripts/handoff/`) installs `customer/README.md` as the snapshot's
`README.md`.

*   [Customer README](customer/README.md): what it does, which flow to use,
    what gets deployed, onboarding, results and the security summary.
*   [How it works](customer/how_it_works.md): the scheduled scan, pull request
    scan and GitOps flows with diagrams, where data lives, and the security
    model.
*   [Operations](customer/operations.md): repositories, `deployment.yaml`,
    GitHub App and Wiz secrets, results, CLI upgrades, updates and
    troubleshooting.
*   [GitOps with Cloud Build](guides/gitops_cloud_build.md): bootstrap, plan and
    apply on pull requests, image rollout.
*   [GitHub Actions fallback for Terraform](examples/github-actions/terraform.yml)
    (example workflow).

## Reference (upstream)

These come from the upstream repository and are kept here unchanged, so
upstream syncs stay clean. They are not part of the customer handoff: some
describe superseded deployment paths (manual `gcloud`, `terraform.tfvars`) or
the upstream design history.

*   [GitHub Actions guide](guides/github_actions_guide.md)
*   [Configuration reference](guides/configuration_reference.md) (environment
    variables)
*   [Terraform deployment guide](guides/terraform_deployment_guide.md)
    (`terraform.tfvars` based)
*   [Production run guide](guides/production_run.md) (manual `gcloud`
    deployment)
*   [Local run guide](guides/local_run.md)
*   [Deployment automation design](architecture/deployment_automation.md)
*   [Parallelization design](architecture/parallelization_design.md)
*   [GitHub Actions orchestration design](architecture/github_actions_orchestration_design.md)
*   [Guardrails](architecture/guardrails.md)
*   [Public preview upgrade design](architecture/codemender_public_preview_upgrade_design.md)
*   [Future work](future_work.md)

The repository-level [README](../README.md) keeps the upstream layout and the
upstream sync instructions.
