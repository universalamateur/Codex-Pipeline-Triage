# Codex Pipeline Triage Spec

> Status: draft implementation contract
> Last updated: 2026-05-02

## Purpose

Codex Pipeline Triage is a GitLab-connected app that reacts to failed GitLab CI/CD pipelines, analyzes the failure with Codex, reports findings in the correct GitLab work item, and later may perform constrained opt-in follow-up actions such as retrying jobs or opening a bot-created fix merge request.

The app exists to demonstrate programmatic Codex use inside a real developer workflow.

## Non-Goals

- No auto-merge.
- No unbounded autonomous loop.
- No customer or production repositories in the demo.
- No direct Codex access to GitLab tokens.
- No browser-side Codex SDK usage.
- No silent fallback from real Codex to fake success.

## V1 Scope

V1 is report-only:

- GitLab login.
- GitLab group authorization.
- Connected synthetic demo project under the configured group.
- Failed merge request Pipeline-event intake.
- Bounded GitLab context.
- Codex triage through the server-side SDK adapter.
- Pydantic validation of Codex output.
- GitLab MR note.
- Persisted run and action audit record.

Later controlled actions:

- Branch-pipeline issue reporting.
- Retry jobs or pipelines.
- Create bot fix branches and fix MRs.
- Monitor follow-up pipelines.

## Primary Workflow

```text
GitLab Pipeline event reports failed pipeline
  -> verify project webhook token/signature
  -> classify pipeline kind and report target
  -> fetch bounded GitLab context
  -> run Codex SDK triage
  -> validate structured output
  -> apply project action policy
  -> persist run and action log
  -> execute allowed GitLab report action
  -> monitor follow-up pipeline if needed
  -> report outcome back into GitLab
```

## OpenAI Demo Requirements

| Requirement | Contract |
|---|---|
| Impressive demo app | GitLab-native CI failure workflow with visible MR/issue output. |
| Programmatic Codex | Codex SDK used server-side in the triage stage. Prefer the experimental Codex Python SDK if Spike 1.1 verifies it. |
| Login / authorization | GitLab OAuth/OIDC plus GitLab group membership check. |
| Persistence | Store users, connected projects, triage runs, action logs, and monitor records. |
| Tests | Unit tests for auth, webhook verification, event routing, schema validation, and GitLab output rendering. |
| Five-minute Loom | Use synthetic eCommerce repo: break pipeline, show webhook, Codex triage, GitLab report, and tests. |

## Event Source Decision

Use GitLab **Pipeline events** as the primary trigger.

Reasons:

- The app cares about the pipeline-level decision point: the pipeline failed.
- Pipeline events include pipeline status, source, ref, SHA, MR context when available, and a `builds` array with job summaries.
- The event can route MR pipelines and branch pipelines differently.

Do not use GitLab Job events as the root trigger. Job events fire for every job status change and are too noisy for starting full workflows. They may be added later as telemetry to update an existing triage run.

Connected project defaults:

```text
Pipeline events: enabled
Job events: disabled
```

## Pipeline Classification

The intake planner classifies the failed pipeline:

```ts
type PipelineKind =
  | "merge_request"
  | "branch"
  | "tag"
  | "child_or_parent"
  | "unknown";
```

Classification rules:

- `merge_request`: pipeline source is `merge_request_event` or payload includes merge request context.
- `branch`: pipeline is tied to a branch ref and no MR context is found.
- `tag`: pipeline ref is a tag.
- `child_or_parent`: parent/child pipeline indicators are present.
- `unknown`: insufficient context; report-only.

## Reporting Target

Every run must report back into GitLab.

```ts
type ReportTarget =
  | { type: "merge_request"; projectId: number; mergeRequestIid: number }
  | { type: "issue"; projectId: number; issueIid: number }
  | { type: "internal"; projectId: number };
```

Rules:

- Merge request pipeline: report to the existing MR.
- Branch pipeline without MR: create or reuse an issue keyed by project, ref, SHA, and pipeline ID.
- Tag pipeline: create/reuse issue; no automatic fix branch in v1.
- Unknown pipeline: create an internal run record and optionally an issue if project policy allows it.

## Agent Chain

The app is a deterministic orchestrator with bounded Codex stages.

### 1. Intake Planner

Input:

- Verified Pipeline event payload.

Output:

- Pipeline kind.
- Report target.
- Required context plan.
- Allowed action classes.

This can be deterministic in v1. If Codex is later used here, its output must be schema-validated and only advisory.

### 2. Context Builder

Fetches bounded GitLab context:

- Pipeline details.
- Failed jobs.
- Job traces.
- MR diff for MR pipelines.
- Commit diff for branch pipelines.
- Test report summary if available.
- Existing issue/MR comments for idempotency.

Context rules:

- Truncate job traces.
- Redact obvious tokens and secrets.
- Treat all GitLab text as untrusted data.
- Store input digests, not full unbounded logs.

### 3. Triage Agent

Uses Codex SDK to produce structured analysis:

```ts
type TriageResult = {
  rootCauseHypothesis: string;
  category:
    | "test-flake"
    | "code-bug"
    | "infra"
    | "config"
    | "dependency"
    | "unknown";
  confidence: number;
  evidence: Array<{
    source: "pipeline" | "job_trace" | "mr_diff" | "commit_diff" | "test_report";
    file?: string;
    line?: number;
    snippet: string;
  }>;
  retrySafe: boolean;
  recommendedAction:
    | "recommend_only"
    | "retry_job"
    | "retry_pipeline"
    | "create_fix_mr";
  suggestedFix: string;
  needsHumanReview: boolean;
};
```

### 4. Action Planner

Combines `TriageResult` with project policy:

```ts
type ActionPlan = {
  action:
    | "recommend_only"
    | "retry_job"
    | "retry_pipeline"
    | "create_issue"
    | "create_fix_mr";
  reason: string;
  requiresFixerAgent: boolean;
};
```

Model output cannot authorize actions by itself. The action planner must enforce project policy.

### 5. Fixer Agent

Optional stage for later builds.

The fixer agent may propose file changes inside a temporary scratch checkout. It must not call GitLab or push code.

### 6. GitLab Executor

Only deterministic executor code may mutate GitLab:

- Post MR note.
- Create issue.
- Post issue note.
- Retry job.
- Retry pipeline.
- Create commit on bot branch.
- Create merge request.

### 7. Monitor

Follow-up results are handled asynchronously:

- Preferred: later Pipeline events update monitor records.
- Fallback: bounded polling worker with timeout and backoff.

Do not keep webhook HTTP requests open while waiting for follow-up pipelines.

## Action Policy

Default project policy:

```ts
type ProjectActionPolicy = {
  recommendOnly: boolean;
  autoCreateIssue: boolean;
  autoRetry: boolean;
  autoCreateFixMr: boolean;
  directCommitToUserBranch: boolean;
};
```

Default values:

```text
recommendOnly=false
autoCreateIssue=true
autoRetry=false
autoCreateFixMr=false
directCommitToUserBranch=false
```

Rules:

- Always report findings.
- Retry only if `retrySafe === true` and policy allows retry.
- Retry is disabled by default and must not run before the controlled-actions spike enables it.
- Create fix MRs only when policy explicitly allows it.
- Prefer bot-owned fix branches over direct commits to developer branches.
- Never merge automatically.
- Fix MRs are same-project only in v1; forked MRs, protected branches, deleted source branches, or insufficient permissions must fall back to report-only behavior unless a later reviewed policy explicitly allows them.

## Merge Request Pipeline Behavior

For failed MR pipelines:

1. Create triage run.
2. Fetch failed jobs, traces, and MR diff.
3. Post initial MR note with findings.
4. Persist action audit record.
5. Later, retry only if the controlled-actions stage is enabled, `retrySafe === true`, and policy allows retry.
6. Later, if fix MR is allowed:
   - Create bot branch from the MR source branch head.
   - Commit proposed fix to the bot branch.
   - Create a fix MR targeting the original MR source branch.
   - Post fix MR link back to the original MR.
7. Later, monitor follow-up pipeline and post result back to the original MR.

## Branch Pipeline Behavior

For failed branch pipelines without MR:

1. Create or reuse issue titled `Pipeline failed on <ref> at <short_sha>`.
2. Post triage analysis to the issue.
3. Persist action audit record.
4. Later, retry only if the controlled-actions stage is enabled, `retrySafe === true`, and policy allows retry.
5. Later, if fix MR is allowed:
   - Create bot branch from the failed SHA.
   - Commit proposed fix.
   - Create MR targeting the failed branch.
   - Link MR from the issue.
6. Later, monitor fix MR pipeline and update the issue.

## Authentication And Authorization

GitLab OAuth/OIDC authenticates users. It does not authorize app access.

Login flow:

```text
GitLab OAuth callback
  -> verify OAuth state and token
  -> read GitLab user identity
  -> check app authorization policy
  -> create app session only if allowed
```

Authorization mode for the demo:

```text
AUTH_ALLOWLIST_MODE=gitlab_group
ALLOWED_GITLAB_GROUP_ID=123456
```

Rules:

- Users must be members of the configured GitLab group.
- Connected projects must belong to the configured GitLab group, or to `ALLOWED_GITLAB_PROJECT_GROUP_ID` if a separate project group is configured.
- A GitLab service account can be invited to the group and used for the app's project/bot token boundary.
- Group lookup failure fails closed.

Session requirements:

- HTTP-only secure same-site session cookie.
- Server-side session store.
- No GitLab tokens in browser storage.
- Do not persist GitLab OAuth access or refresh tokens in v1. If a later reviewed requirement needs persistence, store them encrypted, scoped minimally, and expire them aggressively.
- Access-denied page for authenticated but unauthorized users.
- CSRF or equivalent origin-bound protection on all session-authenticated mutating routes.
- Per-route authorization checks for project connection, policy changes, webhook-secret regeneration, retry, and fix-MR enablement.

## Project Connection

Authorized users connect GitLab projects:

1. Enter project URL or ID.
2. Provide project access token or bot token.
3. App validates token by reading project metadata.
4. App verifies the project belongs to the allowed GitLab group.
5. App generates webhook secret.
6. User configures GitLab project webhook for Pipeline events.
7. App stores project config.

Token recommendation for demo:

- Project access token.
- Role: Developer. Do not use Maintainer by default.
- Scope: `api`.

Reason: REST API writes are needed for notes, issues, retries, commits, and MRs. `read_api` is insufficient after the app starts reporting or acting. Do not add `write_repository` unless using Git-over-HTTPS push rather than the REST Commit API.

Token handling:

- Store only encrypted token material or a secret-store reference.
- Never render tokens back to the browser.
- Never pass tokens as command-line arguments.
- Never log tokens, webhook secrets, OAuth codes, OAuth tokens, or raw authorization headers.
- Document token revocation for the demo service account or project token.

## Codex SDK Contract

The app calls Codex only from server-side code.

Preferred implementation is the experimental Codex Python SDK if Spike 1.1 verifies it locally:

```python
from codex_app_server import AsyncCodex

async with AsyncCodex() as codex:
    thread = await codex.thread_start(model=model)
    result = await thread.run(prompt)
```

Adapter requirements:

- Treat the Python SDK as experimental until Spike 1.1 proves install, execution, timeout, and shutdown behavior.
- Run Codex with read-only intent and no approvals.
- Frame GitLab logs, diffs, comments, and commit messages as untrusted data, never instructions.
- Require Codex to return JSON matching `TriageResult`.
- Validate the final response with Pydantic before it reaches the action planner.
- Store `fallbackReason` and show visible fallback if Codex times out, throws, returns empty output, or fails validation.
- If the Python SDK cannot support the demo cleanly, stop and write a follow-up ADR for the smallest fallback adapter before continuing.

## Persistence Model

Minimum records:

```ts
type ConnectedProject = {
  id: string;
  gitlabProjectId: number;
  gitlabProjectPath: string;
  displayName: string;
  tokenCiphertext: string;
  webhookSecretHash: string;
  actionPolicy: ProjectActionPolicy;
  connectedByGitlabUserId: number;
  enabled: boolean;
  createdAt: string;
  updatedAt: string;
};

type TriageRun = {
  id: string;
  connectedProjectId: string;
  gitlabProjectId: number;
  pipelineId: number;
  jobIds: number[];
  ref: string;
  sha: string;
  pipelineKind: PipelineKind;
  reportTarget: ReportTarget;
  status: "ignored" | "triaged" | "posted" | "actioned" | "monitoring" | "completed" | "failed";
  adapterMode: "mock" | "codex";
  fallbackReason?: string;
  inputDigest: string;
  triageJson?: TriageResult;
  actionPlan?: ActionPlan;
  gitlabNoteIds: number[];
  issueIid?: number;
  fixMergeRequestIid?: number;
  createdAt: string;
  updatedAt: string;
};

type GitLabActionLog = {
  id: string;
  triageRunId: string;
  idempotencyKey: string;
  action:
    | "post_mr_note"
    | "create_issue"
    | "post_issue_note"
    | "retry_job"
    | "retry_pipeline"
    | "create_commit"
    | "create_merge_request";
  reportTarget: ReportTarget;
  policyDecision: "allowed" | "blocked" | "fallback";
  requestDigest: string;
  externalId?: string;
  status: "planned" | "started" | "completed" | "failed" | "skipped";
  fallbackReason?: string;
  createdAt: string;
  updatedAt: string;
};

type PipelineMonitor = {
  id: string;
  triageRunId: string;
  gitlabProjectId: number;
  expectedRef: string;
  expectedSha?: string;
  expectedPipelineId?: number;
  reportTarget: ReportTarget;
  status: "waiting" | "passed" | "failed" | "timed_out";
  createdAt: string;
  updatedAt: string;
};
```

SQLite is preferred for implementation. JSON file persistence is acceptable only before project tokens, webhook secrets, sessions, or real GitLab side effects are introduced.

For GitLab side effects, persist intent before executing the action, then mark the action complete or failed after the external call returns.

## GitLab APIs

GitLab API execution should use deterministic executor code. The preferred executor surface is the GitLab CLI:

- Use `glab api` for REST and GraphQL calls.
- Run with `NO_PROMPT=true` and a controlled `GLAB_CONFIG_DIR` under `.local/`.
- Provide auth through the app's server-side secret boundary.
- Parse JSON output and map failures into typed executor errors.
- Do not rely on a developer's ambient authenticated `glab` session.

Needed API operations:

- List pipeline jobs: `GET /projects/:id/pipelines/:pipeline_id/jobs`
- Get job trace: `GET /projects/:id/jobs/:job_id/trace`
- Retry job: `POST /projects/:id/jobs/:job_id/retry`
- Retry failed/canceled pipeline jobs: `POST /projects/:id/pipelines/:pipeline_id/retry`
- Create issue: `POST /projects/:id/issues`
- Post MR note: `POST /projects/:id/merge_requests/:merge_request_iid/notes`
- Post issue note: `POST /projects/:id/issues/:issue_iid/notes`
- Create commit: `POST /projects/:id/repository/commits`
- Create merge request: `POST /projects/:id/merge_requests`

## Webhook Verification

Verification order:

1. Parse only enough body/header data to identify the candidate project.
2. Lookup the connected project and require `enabled === true`.
3. Require the GitLab Pipeline-event header.
4. Hash and constant-time compare the GitLab webhook token.
5. Confirm payload project ID matches the connected project.
6. Ignore non-failed pipelines.
7. Dedupe by project ID and pipeline ID before creating a run.

GitLab's project webhook token is the primary v1 verifier. If a later GitLab signature mechanism is added, document the exact header and algorithm before implementation.

## UI Requirements

No marketing landing page.

Screens:

1. Login.
2. Access denied.
3. Project list.
4. Connect project.
5. Project webhook setup instructions.
6. Triage run list.
7. Triage run detail.

GitLab MR notes and issues are the main user experience. The app UI exists to configure projects and audit runs.

## GitLab Report Rendering

Rendered MR notes and issue comments must be deterministic and safe:

- Escape or code-fence untrusted snippets from logs, diffs, comments, commit messages, and model output.
- Apply length limits to the full report and to each evidence snippet.
- Run redaction after Codex output and before rendering.
- Suppress or neutralize user mentions, issue/MR autolinks, and external links where practical unless the link was produced by deterministic executor code.
- Never render raw GitLab logs, raw diffs, raw Codex output, tokens, webhook secrets, OAuth codes, OAuth tokens, or raw authorization headers.
- Include fallback status whenever mock mode or fallback behavior was used.

## Tests

Minimum test coverage:

- Allowed GitLab user can create session.
- Unauthorized GitLab user gets access denied.
- Project token validation handles success and failure.
- Bad webhook token returns `401`.
- Non-failed pipeline returns `204`.
- Failed MR pipeline creates triage run and MR report target.
- Failed branch pipeline creates issue report target.
- Duplicate pipeline event does not create duplicate GitLab reports.
- Codex malformed output triggers visible fallback.
- Action planner blocks fix MR when policy disallows it.
- Retry action requires `retrySafe === true`.
- GitLab note/issue rendering includes hypothesis, evidence, confidence, action, and fallback reason when present.
- GitLab note/issue rendering escapes or code-fences untrusted snippets, truncates oversized content, suppresses unsafe mentions/links where practical, and redacts secrets after Codex output.
- Malformed payloads fail safely.
- Disabled or unconnected projects are rejected.
- Group lookup failure fails closed.
- Prompt-injection fixtures cannot override policy or trigger GitLab actions.
- Fake secrets in traces, diffs, commit messages, and MR text are absent from Codex prompts, persisted context, rendered notes, and logs.
- Invalid Codex enum values, out-of-range confidence, oversized evidence snippets, timeout, SDK exception, partial output, and empty output all trigger visible fallback.
- GitLab API failures for `401`, `403`, `404`, rate limits, protected branches, network errors, and note/issue failures are mapped to safe run states.

## Security And Governance

Top risks:

- Unauthorized access if GitLab OAuth is treated as authorization.
- Project token leakage.
- Prompt injection from logs, diffs, commit messages, or MR descriptions.
- Data leakage to Codex from private repositories.
- Unwanted GitLab mutations from over-broad action policy.

Mitigations:

- Explicit allowlist/group gate.
- Group allowlist for connected demo projects.
- Server-side token storage with encryption or secret-store boundary.
- Redaction and truncation before Codex.
- Redaction after Codex before rendering or persistence.
- Synthetic demo repos only.
- Deterministic executor and project policy for all GitLab side effects.
- Transparent comments: generated by Codex, confidence, evidence, action taken.

## Privacy And Data Handling

Demo assumptions:

- Synthetic demo repositories only.
- No customer repositories.
- No production repositories.
- No real personal data in fixtures.
- No product analytics or tracking in v1.

Data minimization:

- Fetch failed-job trace excerpts only.
- Apply byte and line caps before Codex.
- Fetch only diff hunks relevant to the failure when practical.
- Use existing MR/issue comments only for idempotency checks; do not persist full comment bodies unless required for a reviewed feature.
- Store input digests and bounded evidence snippets, not raw unbounded logs or diffs.

User and project notice:

- The connect-project UI must state that selected pipeline logs, diffs, metadata, and GitLab comments may be processed by OpenAI Codex for triage.
- GitLab reports should identify that the triage was generated with Codex and include confidence, evidence, action taken, and fallback status.

Retention:

- Stage 0 keeps the current demo-local retention posture.
- Demo data remains in the local SQLite or JSON store until manually reset or deleted.
- No production retention, deletion, residency, or compliance guarantee is implied until a later reviewed requirement adds one.

## External References

- GitLab Pipeline events: https://docs.gitlab.com/user/project/integrations/webhook_events/#pipeline-events
- GitLab Job events: https://docs.gitlab.com/user/project/integrations/webhook_events/#job-events
- GitLab Jobs API: https://docs.gitlab.com/api/jobs/
- GitLab Pipelines API: https://docs.gitlab.com/api/pipelines/
- GitLab Issues API: https://docs.gitlab.com/api/issues/
- GitLab Merge Requests API: https://docs.gitlab.com/api/merge_requests/
- GitLab Commits API: https://docs.gitlab.com/api/commits/
- GitLab CLI: https://docs.gitlab.com/cli/
- glab api: https://docs.gitlab.com/cli/api/
- GitLab Python style guide: https://docs.gitlab.com/development/python_guide/styleguide/
- GitLab Python project guide: https://docs.gitlab.com/development/python_guide/create_project/
- OpenAI Codex SDK: https://developers.openai.com/codex/sdk
