# Codex Pipeline Triage

Codex Pipeline Triage is a GitLab-connected demo app for failed CI/CD pipelines. A failed GitLab pipeline event triggers the app, the app builds bounded context from GitLab, Codex analyzes the failure through the Codex SDK, and the app reports findings back into GitLab.

This repository is the implementation home for Codex Pipeline Triage. "Pipeline Fixxer" is retained only as historical planning context.

## Current State

Status: spec and demo planning.

There is no runtime implementation yet. The repo has been initialized with the product spec, demo state, and agent instructions so a dev team can build from a clear contract.

## Product Shape

```text
GitLab Pipeline event: failed
  -> webhook receiver verifies project webhook token
  -> intake planner classifies MR vs branch pipeline
  -> context builder fetches jobs, traces, diffs, and metadata
  -> Codex SDK triage agent returns structured analysis
  -> action planner applies project policy
  -> GitLab executor posts notes or creates issues
  -> later controlled-actions stages may retry, open fix MRs, and monitor follow-up pipelines
```

## Core Decisions

- Use **GitLab Pipeline events** as the primary trigger.
- Keep **GitLab Job events** disabled by default; they are optional telemetry later.
- Use GitLab OAuth/OIDC for app login, then enforce a GitLab group-membership gate.
- Restrict connected demo projects to the configured GitLab group.
- Use a GitLab service account or project/bot token for GitLab API actions.
- Prefer the GitLab CLI (`glab`) as the deterministic GitLab API executor surface.
- Use the OpenAI **Codex SDK** server-side for triage.
- Do not let Codex directly mutate GitLab. GitLab side effects happen only in deterministic executor code.
- Default project policy is conservative: report findings and create/reuse issues. Retry and fix MR creation are opt-in later actions.

## Behavior

### Merge Request Pipelines

When a failed pipeline belongs to a merge request:

1. Analyze the failed pipeline.
2. Post findings to the existing MR.
3. Persist the triage run and audit record.
4. Retry, fix MRs, and follow-up monitoring are later opt-in controlled actions.

### Branch Pipelines Without MR

When a failed pipeline is not attached to an MR:

1. Create or reuse a GitLab issue for the failed pipeline.
2. Post findings to the issue.
3. Persist the triage run and audit record.
4. Retry, fix MRs, and follow-up monitoring are later opt-in controlled actions.

## Demo Fit

This is intended to satisfy the OpenAI Codex demo-app requirement:

| Requirement | Implementation |
|---|---|
| Demo app for major eCommerce hackathon | Use a synthetic checkout/cart demo repo with an intentionally broken pipeline. |
| Codex can build impressive apps | The app is a working GitLab workflow, not a standalone toy UI. |
| Programmatic Codex use | Server-side Codex SDK call with schema-validated output. Spike 1.1 must prove the Python SDK path or trigger an ADR-backed fallback to `@openai/codex-sdk`. |
| Login / authorization | GitLab OAuth/OIDC plus GitLab group authorization. |
| Data persistence | Connected projects, triage runs, action logs, monitors. |
| Meaningful tests | Auth, webhook verification, pipeline filtering, Codex schema gate, GitLab action rendering. |
| Working UX | GitLab MR notes and issues are the primary UX; app UI shows configuration and run history. |

## Repository Docs

- [SPEC.md](SPEC.md) - application specification and implementation contract.
- [SPIKES.md](SPIKES.md) - iterative stage/spike plan with handoff gates.
- [START-PROMPTS.md](START-PROMPTS.md) - copy-paste prompts for the dev team and pair code reviewer team.
- [DEMO-STATE.md](DEMO-STATE.md) - demo scenario, recording plan, and current state.
- [AGENTS.md](AGENTS.md) - instructions for agents and developers working in this repo.
- [ADR-0001-runtime-stack.md](ADR-0001-runtime-stack.md) - current runtime stack decision.

Local-only context lives in `.local/`. It is intentionally ignored by Git and can contain absolute workstation paths and zettelkasten cross-links.

## Post-MVC Documentation TODO

After the MVC/demo path is complete, rebuild the public development artifacts as
a coherent tracked set:

- `AGENTS.md`
- public `ROADMAP.md`
- `SPEC.md`
- `SPIKES.md`
- `README.md`
- public ADRs and demo-development notes

Keep private prompts, reviewer handoffs, local manual-test evidence, workstation
paths, and sensitive/demo-only coordination notes in `.local/`.

## Planned Stack

- Python 3.10+
- `uv`
- FastAPI for HTTP routes, webhooks, sessions, and tests.
- FastHTML only if it keeps the configuration UI simpler than templates.
- OpenAI Codex Python SDK if Spike 1.1 verifies the experimental local SDK path.
- `glab` CLI for GitLab API execution through a deterministic wrapper.
- Pydantic
- SQLite for local/demo persistence
- pytest
- Black, isort, flake8, pylint, mypy, and pytest-cov

The stack should stay Python-first unless Spike 1.1 proves the Codex Python SDK path cannot support the demo. Any fallback must be captured in an ADR before implementation continues. The fallback must still satisfy the OpenAI assignment by using a real Codex surface, not a generic OpenAI Responses API call.

Python project setup follows GitLab's Python style and project guides, with one local deviation: `uv` replaces the Poetry examples as the dependency/environment manager.

## Environment Overview

Do not commit real values.

```text
APP_BASE_URL=
SESSION_SECRET=

GITLAB_BASE_URL=https://gitlab.com
GITLAB_OAUTH_CLIENT_ID=
GITLAB_OAUTH_CLIENT_SECRET=
AUTH_ALLOWLIST_MODE=gitlab_group
ALLOWED_GITLAB_GROUP_ID=
ALLOWED_GITLAB_PROJECT_GROUP_ID=
GITLAB_EXECUTOR_MODE=glab
GITLAB_SERVICE_ACCOUNT_USERNAME=

OPENAI_API_KEY=
PIPELINE_TRIAGE_MODE=mock
PIPELINE_TRIAGE_CODEX_MODEL=

TRIAGE_DATA_FILE=.local/triage-runs.json
```

Connected project tokens and webhook secrets are created through the app and stored server-side. They must never be exposed to the browser or logs. The `glab` wrapper must run non-interactively with a controlled token/config boundary, not with a developer's ambient personal session.

## Readiness For Teams

The repo is ready for dev, test, QA, and review teams to start **Stage 1 / Spike 1.1**.

Readiness boundaries:

- Runtime implementation has not started.
- Stage 1.1 is a validation spike, not a product feature spike.
- The main open architectural risk is the experimental Codex Python SDK path.
- If the Python SDK cannot satisfy real programmatic Codex use with clean timeout/shutdown and schema validation, stop and write an ADR for the TypeScript `@openai/codex-sdk` fallback before building further.
- V1 demo readiness means failed MR pipeline -> Codex triage -> MR note. Branch pipeline issue reporting is required for product completeness in later spikes, not for the first OpenAI demo cut.

## Iterative Build Model

Build this in spikes, not as one long implementation push.

Each spike follows this loop:

```text
dev team implements one spike
  -> dev handoff
  -> pair code reviewer team review
  -> Falko manual test or explicit skip
  -> fixes if needed
  -> next spike only after approval
```

Use [SPIKES.md](SPIKES.md) for the stage plan and [START-PROMPTS.md](START-PROMPTS.md) to launch the two teams.

## Next Build Step

Start with **Spike 1.1 - Framework Decision And Skeleton** from [SPIKES.md](SPIKES.md). Do not proceed to auth or GitLab integration until the skeleton has been reviewed and manually smoke-tested.
