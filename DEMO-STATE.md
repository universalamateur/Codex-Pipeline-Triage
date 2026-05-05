# Demo State

> Last updated: 2026-05-05

## Current Repo State

The repo is in Spike 8.2 demo-hardening stage.

Implemented:

- FastAPI app with health endpoint, GitLab OAuth login/logout, and GitLab group gate.
- Connected-project setup with project-token validation and GitLab group boundary.
- Webhook setup with generated per-project secret hash.
- GitLab Pipeline Hook intake, classification, duplicate handling, and Job Hook ignore behavior.
- Bounded/redacted context builder for jobs, traces, and diffs.
- Server-side Codex Python SDK adapter with timeout, read-only controls, Pydantic validation, and visible fallback.
- MR note reporting, branch issue reporting, retry action, fix MR creation, and follow-up monitor result notes through deterministic GitLab executor/client code.
- SQLite persistence for projects, runs, action logs, and monitor records.
- Run history and run detail pages for demo inspection.
- Focused and full automated test coverage through Spike 8.2.

Out of scope for this demo cut:

- Auto-merge.
- Unbounded autonomous loops.
- Customer or private production repositories.
- Browser-side Codex SDK usage.
- Background polling worker beyond the deterministic timeout path.

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

V1 proves the reporting loop before controlled actions.

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
14. App run detail shows status, adapter mode, action logs, and monitor state.
```

Success screenshot:

- GitLab MR shows a "Codex Pipeline Triage" note.
- Note includes hypothesis, evidence, confidence, suggested fix, and action policy.
- App triage-run detail shows adapter mode, GitLab target, and persisted output.

## Manual Webhook Testing

GitLab cannot deliver webhooks to `127.0.0.1` on a local laptop. For manual
webhook setup and replay gates, expose the local app with a temporary tunnel:

```bash
cloudflared tunnel --url http://127.0.0.1:8765
```

Use the generated HTTPS tunnel origin as the temporary `APP_BASE_URL` and
configure GitLab with the app-generated webhook path. Do not commit or paste the
ephemeral tunnel URL, tunnel session logs, webhook secrets, project tokens,
OAuth callback values, cookies, or raw webhook payloads from real projects.

Manual webhook configuration for the demo project:

- Enable Pipeline events.
- Keep Job events disabled.
- Keep SSL verification enabled.
- Use only synthetic GitLab demo projects.

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

Use V3 only after the real Codex MR-note path is shown. Keep the story clear:
Codex recommends, project policy gates, deterministic executor mutates GitLab,
and monitors report the follow-up result. Never imply auto-merge.

## Five-Minute Loom Shape

| Time | Segment | Show |
|---:|---|---|
| 0:00-0:25 | Setup | Logged-in app, connected GitLab project, Pipeline events configured. |
| 0:25-1:05 | Trigger | Broken eCommerce MR pipeline fails. |
| 1:05-1:55 | Intake | App run detail: pipeline classified as MR pipeline, failed job fetched. |
| 1:55-2:45 | Codex | Server-side Codex SDK call, structured output validation, timeout/fallback. |
| 2:45-3:35 | Result | MR note appears with diagnosis and suggested fix. |
| 3:35-4:20 | Controlled action | Optional fix MR and follow-up monitor result, clearly policy-gated. |
| 4:20-4:45 | Safety | Group gate, token boundary, schema validation, no direct Codex mutations, no auto-merge. |
| 4:45-5:00 | Tests | Show focused tests and close with final readiness. |

## Acceptance Criteria For Demo Readiness

- [x] Login works with GitLab OAuth/OIDC.
- [x] User outside the configured GitLab group is denied.
- [x] Connected project outside the configured GitLab group is rejected.
- [x] Project connection can store token and webhook secret server-side.
- [x] Failed MR Pipeline event triggers a triage run.
- [x] Non-failed Pipeline events are ignored.
- [x] Codex SDK path runs in real mode at least once before recording.
- [x] Mock mode remains deterministic for tests.
- [x] MR receives exactly one readable report for the initial demo failure.
- [x] Controlled retry/fix MR actions are policy-gated and disabled unless explicitly enabled.
- [x] Follow-up monitor failure path has passed manual gate.
- [x] App run history persists after restart.
- [x] Tests pass.
- [ ] Final Loom dry run fits under five minutes.

## Iterative Delivery State

Delivery model:

```text
dev spike -> reviewer handoff -> Falko manual test -> fix/accept -> next spike
```

Current stage:

```text
Stage 8 - Monitoring And Demo Polish
```

Next spike:

```text
Spike 8.2 - Demo Hardening
```

Use [DEMO-SCRIPT.md](DEMO-SCRIPT.md) for the five-minute recording path and
keep manual-test evidence in `.local/`.

## Demo Talking Points

- "Pipeline events are the root trigger because the app reasons at pipeline level, not job level."
- "GitLab login proves identity; configured GitLab group membership decides app access."
- "Codex proposes analysis and actions; deterministic code executes only allowed GitLab actions."
- "The default path is reporting. Retry and fix MRs are explicit project-policy upgrades."
- "Every result is reported back into GitLab and stored in the app."
