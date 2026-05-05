# Demo Talk Track

Plain-language five-minute recording script for Codex Pipeline Triage.

Do not show tokens, webhook secrets, cookies, tunnel URLs, raw webhook payloads,
raw job traces, raw diffs, or raw Codex output while recording.

## 0:00-0:25 - App And Access

**On Screen**

- Open the app.
- Show `/health`.
- Show that you are signed in.
- Show the connected GitLab project.

**Say**

This is Codex Pipeline Triage.

It watches failed GitLab pipelines and reports a safe triage result back into
GitLab.

GitLab is the OAuth provider here.

The login proves who I am.

Then the app checks whether my GitLab user belongs to the allowed group.

That group check is the app's RBAC gate.

So authentication is GitLab OAuth, and authorization is GitLab group
membership.

## 0:25-0:55 - Webhook Setup

**On Screen**

- Open the app webhook setup page.
- Show only the event instructions.
- In GitLab, show the webhook event checkboxes.
- Keep the webhook URL and secret token hidden or redacted.

**Say**

The trigger is a GitLab Pipeline event.

The app works at pipeline level, not job-event level.

Job events stay disabled, so every single job update does not start a new
workflow.

For this demo, only the synthetic GitLab project is connected.

## 0:55-1:25 - Failed MR Pipeline

**On Screen**

- Open the fresh synthetic merge request.
- Show the failed pipeline.
- Show the failed job name: `Codex:demo-fail`.

**Say**

Here is the fresh demo merge request.

It has one intentional failing job called `Codex:demo-fail`.

This gives the app a clean failure to analyze.

No customer or production repository is involved.

## 1:25-2:05 - App Intake

**On Screen**

- Open run history.
- Open the run detail page.
- Show pipeline ID, run status, kind, adapter, and context digest.

**Say**

The webhook reached the app.

The app verified the project and webhook token.

It classified the event as a merge request pipeline.

Then it fetched bounded context, stored the run, and kept an audit trail.

## 2:05-2:50 - Codex Result

**On Screen**

- Open the GitLab merge request note.
- Show the heading `Codex Pipeline Triage (CODEX)`.
- Show confidence, evidence, and suggested fix.

**Say**

Codex ran on the server side.

The browser never gets access to the Codex SDK or project token.

Codex returned structured JSON.

The app validated that JSON before it posted anything to GitLab.

The result is a clear MR note with the hypothesis, evidence, confidence, and
suggested fix.

## 2:50-3:35 - Safety Boundary

**On Screen**

- Go back to run detail.
- Show `Recommended action`.
- Show `Policy action`.
- Show the GitLab action log for the posted MR note.

**Say**

Codex does not mutate GitLab directly.

Codex gives a recommendation.

The app checks schema, project policy, and the target project before any GitLab
write happens.

The default path is report-only.

That is why the first action is just a GitLab MR note.

## 3:35-4:30 - Controlled Fix MR

**On Screen**

- Show the `Create bot fix MR` button.
- Click the button.
- Show the run detail after redirect.
- Show fix MR IID.
- Show fix commit SHA.
- Show completed action logs.
- Show the waiting monitor row.
- Open the fix MR in GitLab if time allows.

**Say**

Now I am choosing the controlled action.

This is a human click, and the project policy allows it.

The deterministic executor creates one bot branch commit and one separate fix
merge request.

It does not commit directly to the original branch.

It does not auto-merge.

The original MR gets an action note, and the app records the commit, fix MR, and
monitor state.

## 4:30-5:00 - Tests And Close

**On Screen**

- Show the latest test output or quality-gate summary.
- End on the app run detail or GitLab MR notes.

**Say**

The important paths are covered by tests and manual gates.

The demo is intentionally small.

The key point is the boundary.

Codex analyzes and recommends.

The app validates, applies policy, and performs only deterministic GitLab
actions.

That keeps the workflow useful, auditable, and safe.
