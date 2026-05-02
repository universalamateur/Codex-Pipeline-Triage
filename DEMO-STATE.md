# Demo State

> Last updated: 2026-05-02

## Current Repo State

The repo is in planning/spec stage.

Implemented:

- Project README.
- Application spec.
- Runtime stack ADR.
- Demo state document.
- Agent/developer instructions.
- Iterative spike plan.
- Start prompts for dev and reviewer teams.
- Git ignore rules for local context and secrets.
- Local-only handoff folder under `.local/`.

Not implemented yet:

- Runtime app.
- GitLab OAuth.
- Webhook receiver.
- Codex SDK adapter.
- Persistence.
- Tests.

## Demo Goal

Show a GitLab-native workflow where a failed eCommerce CI pipeline is analyzed by Codex and reported back inside GitLab.

The recording should communicate:

- The app has login/authorization.
- Authorization is GitLab group-gated.
- The app persists connected projects and triage runs.
- GitLab Pipeline events trigger the workflow.
- Codex is used programmatically with the Codex SDK.
- Output is schema-validated.
- GitLab actions are controlled by policy.
- The result appears where developers already work: MR notes or issues.

## Demo Repository

Use a synthetic GitLab project, not a customer or private production project.

Recommended scenario:

```text
checkout-service
```

Intentional failure:

- Area: cart, checkout, tax, discount, or inventory reservation.
- Failure type: one failing unit/integration test with a clear assertion diff.
- Pipeline: small and fast enough for a five-minute recording.
- MR path: fail on a merge request first.
- Branch path: fail on a branch without MR as optional second scenario.

## Demo Flow V1

V1 should prove the reporting loop before automated fixes. V1 is report-only.

```text
1. User logs into app with GitLab.
2. App allows the user through GitLab group membership.
3. User opens connected project setup.
4. GitLab demo project has Pipeline events webhook configured.
5. User pushes broken checkout MR.
6. Pipeline fails.
7. Webhook fires.
8. App verifies webhook.
9. App fetches failed jobs, traces, and MR diff.
10. Server-side Codex SDK adapter returns structured triage.
11. App validates output.
12. App posts MR note.
13. App persists triage run.
```

Success screenshot:

- GitLab MR shows a "Codex Pipeline Triage" note.
- Note includes hypothesis, evidence, confidence, suggested fix, and action policy.
- App triage-run detail shows adapter mode, GitLab target, and persisted output.

## Demo Flow V2

V2 adds branch pipeline issue reporting:

```text
1. User pushes broken branch without MR.
2. Pipeline fails.
3. App creates or reuses issue.
4. App posts triage analysis to issue.
```

## Demo Flow V3

V3 adds controlled opt-in actions:

- Retry transient failures.
- Create bot fix branch.
- Create fix MR.
- Monitor follow-up pipeline.
- Report final pass/fail status.

Do not start with V3. The OpenAI demo is stronger if V1 is reliable and easy to explain.

## Five-Minute Loom Shape

| Time | Segment | Show |
|---:|---|---|
| 0:00-0:25 | Setup | Logged-in app, connected GitLab project, Pipeline events configured. |
| 0:25-1:05 | Trigger | Broken eCommerce MR pipeline fails. |
| 1:05-1:55 | Intake | App run detail: pipeline classified as MR pipeline, failed job fetched. |
| 1:55-2:45 | Codex | Server-side Codex SDK call, structured output validation, timeout/fallback. |
| 2:45-3:35 | Result | MR note appears with diagnosis and suggested fix. |
| 3:35-4:20 | Safety | Allowlist auth, project token boundary, schema validation, no direct Codex mutations. |
| 4:20-5:00 | Tests | Show focused tests and close with next actions. |

## Acceptance Criteria For Demo Readiness

- [ ] Login works with GitLab OAuth/OIDC.
- [ ] User outside the configured GitLab group is denied.
- [ ] Connected project outside the configured GitLab group is rejected.
- [ ] Project connection can store token and webhook secret server-side.
- [ ] Failed MR Pipeline event triggers a triage run.
- [ ] Non-failed Pipeline events are ignored.
- [ ] Codex SDK path runs in real mode at least once before recording.
- [ ] Mock mode remains deterministic for tests.
- [ ] MR receives exactly one readable report for the demo failure.
- [ ] V1 does not retry jobs, create commits, or open fix MRs.
- [ ] App run history persists after restart.
- [ ] Tests pass.
- [ ] Loom dry run fits under five minutes.

## Iterative Delivery State

Delivery model:

```text
dev spike -> reviewer handoff -> Falko manual test -> fix/accept -> next spike
```

Current stage:

```text
Stage 0 - Planning And Repo Readiness
```

Next spike:

```text
Spike 1.1 - Framework Decision And Skeleton
```

Use [SPIKES.md](SPIKES.md) for the full plan and [START-PROMPTS.md](START-PROMPTS.md) to start the dev and pair code reviewer teams.

## Demo Talking Points

- "Pipeline events are the root trigger because the app reasons at pipeline level, not job level."
- "GitLab login proves identity; the app allowlist decides access."
- "Codex proposes analysis and actions; deterministic code executes only allowed GitLab actions."
- "The default path is reporting. Retry and fix MRs are explicit project-policy upgrades."
- "Every result is reported back into GitLab and stored in the app."
