# Iterative Spike Plan

> Purpose: build Codex Pipeline Triage in reviewable slices. Each spike must produce a handoff, receive review, and wait for Falko's manual-test feedback before the next spike starts.

## Operating Model

Work proceeds in stages. A stage contains one or more spikes. A spike is the smallest slice that can be implemented, tested, reviewed, and manually checked.

No team should silently continue from one spike to the next.

Required loop:

```text
Dev team implements one spike
  -> dev handoff with changed files, tests, risks, manual test steps
  -> pair code reviewer team reviews the spike
  -> Falko manually tests or gives explicit skip
  -> next spike starts only after feedback is incorporated or accepted
```

## Stage Gates

Each stage has three gates:

1. **Implementation gate:** relevant automated tests pass or failures are explicitly documented.
2. **Review gate:** pair code reviewer team returns GREEN, YELLOW, or RED.
3. **Manual gate:** Falko confirms the expected behavior manually, or explicitly allows moving forward without manual validation.

Do not proceed on RED. On YELLOW, only proceed if Falko accepts the residual risk.

## Handoff Format

Every dev spike must end with:

```text
Spike: <id and title>
Status: ready for review | blocked | partial
Changed files:
- <path>
Tests run:
- <command> -> <result>
Behavior delivered:
- <concrete behavior>
Manual test steps for Falko:
1. ...
Known gaps / risks:
- ...
Next recommended spike:
- ...
```

Every review must end with:

```text
Review for spike: <id and title>
Verdict: GREEN | YELLOW | RED
Findings:
- [severity] <file:line> <issue>
Manual test focus:
- ...
Must fix before next spike:
- ...
Can defer:
- ...
```

## Test And Fixture Requirements

As soon as webhook intake starts, maintain a fixture matrix with:

- Fixture name.
- Pipeline kind.
- Expected HTTP status.
- Expected run status.
- Expected report target.
- Expected GitLab side effects.

Minimum fixture categories:

- Failed MR pipeline.
- Failed branch pipeline.
- Tag pipeline.
- Child/parent pipeline.
- Unknown pipeline.
- Non-failed pipeline.
- Malformed payload.
- Unconnected project.
- Disabled project.
- Duplicate delivery.
- Trace/diff/comment containing fake secrets.
- Trace/diff/comment containing prompt-injection text.

Manual gates must record:

- Setup.
- Command or UI path.
- Fixture or GitLab project used.
- Expected status/output.
- Expected persisted state.
- GitLab artifact to inspect.
- Cleanup/reset steps.
- Accepted residual risk, if any.

Python style requirements:

- Use pytest for tests.
- Use `unittest.mock` at service, filesystem, `glab`, network, and Codex boundaries.
- Prefer small typed fixtures and named parametrized cases when parameter lists become hard to read.
- Keep imports sorted with isort's Black profile.
- Keep formatting automated with Black.
- Run flake8, pylint, and mypy locally and in CI once CI exists.
- Add dependency scanning, secret detection, and Semgrep/SAST once CI is introduced.

Design principles review:

- Understandability: obvious module names, route names, and state names.
- Maintainability: explicit policies and typed boundaries, no hidden side effects.
- Simplicity: one direct path per spike, no general framework before the second real use case.

## Stage 0 - Planning And Repo Readiness

Status: current.

Goal: make the repo understandable before runtime implementation.

Deliverables:

- README.
- SPEC.
- DEMO-STATE.
- AGENTS instructions.
- Iterative spike plan.
- Start prompts.
- Ignored local handoff context.

Exit criteria:

- Docs explain behavior, constraints, and build order.
- `.local/` is ignored.
- No runtime code is implied to exist.

## Stage 1 - Runtime Scaffold

Goal: create the smallest runnable app and test harness.

### Spike 1.1 - Framework Decision And Skeleton

Build:

- Confirm the Python-first stack in [ADR-0001-runtime-stack.md](ADR-0001-runtime-stack.md), or write a follow-up ADR if concrete evidence blocks it.
- Add Python project setup with `uv`.
- Add `pyproject.toml` with GitLab-style tool configuration for Black, isort, flake8, pylint, mypy, pytest, and pytest-cov.
- Add a package layout that follows the GitLab Python project guide, adapted for an API service:
  - `codex_pipeline_triage/`
  - `tests/codex_pipeline_triage/`
  - `scripts/` only when a script is genuinely needed.
- Add FastAPI route skeleton.
- Add package scripts or Makefile targets for `format`, `lint`, `typecheck`, `test`, and `test-cov`.
- Add health endpoint.
- Add minimal app shell only if needed.
- Verify local `glab` availability and add a mocked executor wrapper seam.
- Probe the experimental Codex Python SDK path and document whether it is viable.

Tests:

- Health endpoint test.
- Typecheck/lint baseline.
- Mocked `glab` executor wrapper test.
- Test files mirror the package path and use `test_<module>.py` naming.

Manual test:

- Start dev server.
- Open health endpoint or first app page.

Stop after this spike for review.

### Spike 1.2 - Persistence Boundary

Build:

- Add persistence abstraction.
- Add SQLite or JSON-backed demo store.
- Model users, connected projects, triage runs, action logs, monitors.

Tests:

- Store create/read/update tests.
- Data isolation tests.

Manual test:

- Run a small script or route that proves persisted state survives restart.

Stop after this spike for review.

## Stage 2 - Login And App Authorization

Goal: GitLab login works, but only configured GitLab group members can enter.

### Spike 2.1 - GitLab OAuth Login

Build:

- OAuth callback route.
- Session cookie.
- Login/logout UI.
- Access denied page.

Tests:

- OAuth callback mocked success.
- Invalid state rejected.
- Session cookie shape.

Manual test:

- Login with GitLab using a demo OAuth app.

Stop after this spike for review.

### Spike 2.2 - GitLab Group Gate

Build:

- GitLab group membership authorization by configured group ID.
- Deny-by-default behavior.

Tests:

- Allowed user succeeds.
- Unknown user denied.
- Group lookup failure fails closed.

Manual test:

- Confirm allowed user can enter and a non-allowed test user cannot.

Stop after this spike for review.

## Stage 3 - Connected Projects

Goal: authorized users can connect a synthetic GitLab project in the allowed GitLab group without exposing secrets.

### Spike 3.1 - Project Connection Form And Token Validation

Build:

- Project URL/ID input.
- Project token input.
- Validate token by reading project metadata.
- Validate project belongs to the configured allowed group.
- Store token through secret boundary or encrypted field.

Tests:

- Valid token path mocked.
- Invalid token path.
- Project outside allowed group rejected.
- Token never rendered back to client.

Manual test:

- Connect one synthetic GitLab demo project.

Stop after this spike for review.

### Spike 3.2 - Webhook Setup Page

Build:

- Generate per-project webhook secret.
- Show webhook URL and setup instructions.
- Persist webhook secret hash.

Tests:

- Secret generated once.
- Secret hash stored, raw secret not logged.

Manual test:

- Configure GitLab project webhook with Pipeline events enabled and Job events disabled.

Stop after this spike for review.

## Stage 4 - Pipeline Event Intake

Goal: failed Pipeline events create triage runs; non-failed events are ignored.

### Spike 4.1 - Fixture-Driven Webhook Route

Build:

- Pipeline event payload schema.
- Webhook token/signature verification.
- Ignore non-pipeline and non-failed pipeline events.
- Dedupe by project and pipeline ID.

Tests:

- Bad token returns 401.
- Success pipeline returns 204.
- Failed pipeline creates one triage run.
- Duplicate event does not duplicate.

Manual test:

- Replay fixture payload locally.

Stop after this spike for review.

### Spike 4.2 - Pipeline Classification

Build:

- Classify MR, branch, tag, child/parent, unknown.
- Determine report target.

Tests:

- MR fixture routes to MR target.
- Branch fixture routes to issue target.
- Tag/unknown are report-only.

Manual test:

- Replay MR and branch fixtures.

Stop after this spike for review.

## Stage 5 - GitLab Context And Mock Reporting

Goal: produce useful GitLab reports without real Codex.

### Spike 5.1 - Context Builder

Build:

- Fetch pipeline jobs.
- Fetch job traces.
- Fetch MR diff or branch commit diff.
- Truncate and redact context.

Tests:

- API client mocked.
- Trace truncation.
- Secret redaction.

Manual test:

- Run against a synthetic failed pipeline and inspect stored bounded context.

Stop after this spike for review.

### Spike 5.2 - Mock Triage And MR Note

Build:

- Deterministic mock triage.
- MR note renderer.
- Post MR note through mocked and real GitLab paths.

Tests:

- Note includes hypothesis, evidence, confidence, action, fallback/mock marker.
- MR note API payload correct.

Manual test:

- Fail synthetic MR pipeline and see one GitLab MR note.

Stop after this spike for review.

### Spike 5.3 - Branch Issue Reporting

Build:

- Create or reuse issue for branch pipeline.
- Post issue note.

Tests:

- Branch pipeline creates issue target.
- Existing issue reused.

Manual test:

- Fail synthetic branch pipeline and see issue report.

Stop after this spike for review.

## Stage 6 - Codex SDK Triage

Goal: real programmatic Codex use replaces mock triage in real mode.

### Spike 6.1 - Codex Adapter Boundary

Build:

- Server-only Codex SDK adapter.
- Prefer the experimental Codex Python SDK if Spike 1.1 proved it viable.
- Structured output schema.
- Timeout and fallback.

Tests:

- SDK mocked at module boundary.
- Malformed final response triggers fallback.
- Client/browser code cannot import Codex adapter.
- Invalid enum, out-of-range confidence, empty output, timeout, and SDK exception trigger fallback.

Manual test:

- Run one real Codex triage against stored synthetic context.

Stop after this spike for review.

### Spike 6.2 - Real Mode GitLab Report

Build:

- `PIPELINE_TRIAGE_MODE=codex`.
- Real Codex output rendered to MR/issue.
- Persist adapter mode and redacted schema-validated output.

Tests:

- Real mode path uses Codex adapter when configured.
- Missing key falls back visibly.

Manual test:

- Fail synthetic MR pipeline and confirm MR note says real Codex path was used.

Stop after this spike for review.

## Stage 7 - Controlled Actions

Goal: take useful actions only when project policy allows them.

### Spike 7.1 - Retry Action

Build:

- Action planner enforces `retrySafe` plus policy.
- Retry job or failed pipeline.
- Post action note.

Tests:

- Retry blocked when `retrySafe=false`.
- Retry blocked when policy disabled.
- Retry blocked by default policy.
- Retry API payload correct.

Manual test:

- Synthetic transient failure gets retried and note is posted.

Stop after this spike for review.

### Spike 7.2 - Fix MR Creation

Build:

- Fixer agent produces patch in scratch checkout.
- Executor creates bot branch commit through GitLab REST Commit API.
- Executor creates fix MR.
- Link fix MR back to original MR or issue.

Tests:

- Fix MR blocked by default policy.
- Commit and MR payloads correct.
- No direct commits to user branch unless explicitly enabled.

Manual test:

- Enable policy for synthetic project and confirm fix MR appears.

Stop after this spike for review.

## Stage 8 - Monitoring And Demo Polish

Goal: close the loop and make the demo recordable.

### Spike 8.1 - Monitor Follow-Up Pipeline

Build:

- Monitor records.
- Later Pipeline events update monitor status.
- Bounded polling fallback if needed.
- Final result posted to MR or issue.

Tests:

- Later pass event closes monitor.
- Later fail event reports failure.
- Timeout path posts timed-out status.

Manual test:

- Trigger fix MR and observe final status report.

Stop after this spike for review.

### Spike 8.2 - Demo Hardening

Build:

- Seed/demo setup instructions.
- Run detail polish.
- Loom script.
- Final README updates.

Tests:

- Full happy-path smoke.
- All unit tests.

Manual test:

- Full dry run under five minutes.

Stop for final review.
