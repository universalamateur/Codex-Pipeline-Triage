# Demo Talk Track

Plain-language five-minute recording script for Codex Pipeline Triage.

Do not show tokens, webhook secrets, cookies, tunnel URLs, raw webhook payloads,
raw job traces, raw diffs, or raw Codex output while recording.

## Prep Tabs

Open these in Chrome before recording. Keep the app server and Cloudflare tunnel
terminals running, but hidden.

Do not record the tunnel terminal, webhook secret, project token, OAuth callback
values, cookies, raw payloads, `.env` contents, raw traces, or raw diffs.

**App Tabs**

- App home: `http://127.0.0.1:8765/`
- Health check: `http://127.0.0.1:8765/health`
- Connected projects: `http://127.0.0.1:8765/projects`
- Webhook setup page:
  `http://127.0.0.1:8765/projects/<connected_project_id>/webhook`
- Run history:
  `http://127.0.0.1:8765/projects/<connected_project_id>/runs`
- Run detail:
  `http://127.0.0.1:8765/projects/<connected_project_id>/runs/<run_id>`

**GitLab Tabs**

- Original fresh demo MR:
  `https://gitlab.com/universalamateur1/dev-sec-ops-with-git-lab/-/merge_requests/<mr_iid>`
- Original MR pipeline:
  `https://gitlab.com/universalamateur1/dev-sec-ops-with-git-lab/-/pipelines/<pipeline_id>`
- Codex note on the original MR:
  `https://gitlab.com/universalamateur1/dev-sec-ops-with-git-lab/-/merge_requests/<mr_iid>#note_<codex_note_id>`
- After clicking `Create bot fix MR`, open the bot fix MR:
  `https://gitlab.com/universalamateur1/dev-sec-ops-with-git-lab/-/merge_requests/<fix_mr_iid>`
- After clicking `Create bot fix MR`, open the action note:
  `https://gitlab.com/universalamateur1/dev-sec-ops-with-git-lab/-/merge_requests/<mr_iid>#note_<action_note_id>`

**Terminal**

Do not share terminal for the main story.

Only show a clean terminal at the end if you want to show test output. Keep env
vars, tunnel logs, tokens, webhook secrets, and `.env` contents off screen.

**How-Built Tabs**

- Repo overview or README.
- `SPEC.md` or `DEMO-SCRIPT.md`.
- Clean test output or quality-gate summary.

## 0:00-0:20 - App And Access

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

## 0:20-0:45 - Webhook Setup

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

## 0:45-1:10 - Failed MR Pipeline

**On Screen**

- Open the fresh synthetic merge request.
- Show the failed pipeline.
- Show the failed job name: `Codex:demo-fail`.

**Say**

Here is the fresh demo merge request.

It has one intentional failing job called `Codex:demo-fail`.

This gives the app a clean failure to analyze.

No customer or production repository is involved.

## 1:10-1:45 - App Intake And Persistence

**On Screen**

- Open run history.
- Open the run detail page.
- Show pipeline ID, run status, kind, adapter, context digest, and note ID.

**Say**

The webhook reached the app.

The app verified the project and webhook token.

It classified the event as a merge request pipeline.

Then it fetched bounded context and stored the run.

This page is reading persisted state: the pipeline, the adapter mode, the
context digest, and the GitLab note ID.

That gives the workflow an audit trail instead of a one-time log message.

## 1:45-2:25 - Codex Result

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

## 2:25-3:05 - Safety Boundary

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

## 3:05-3:45 - Controlled Fix MR

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

This is the wow moment: a failed GitLab pipeline turns into a Codex triage note
and a safe bot fix MR, without auto-merge.

## 3:45-5:00 - How I Built This

**On Screen**

- Show the repo overview, README, or `SPEC.md`.
- Show the latest test output or quality-gate summary.
- End on the run detail with the fix MR, commit SHA, action logs, and monitor
  row visible.

**Say**

I built this with Codex in small slices.

First the spec, then webhook intake, then the Codex adapter, then GitLab
write-back, and finally the controlled fix MR path.

The stack is intentionally simple: Python, FastAPI, Pydantic, SQLite, and a
deterministic GitLab executor.

Codex is used programmatically inside the app, server-side only.

The app persists each run, action log, GitLab note ID, fix MR ID, commit SHA,
and monitor record.

The meaningful tests cover auth gates, webhook validation, schema validation,
policy checks, report posting, and the fix MR action.

The demo is intentionally small.

Codex analyzes and recommends.

The app validates, applies policy, and performs only deterministic GitLab
actions.

That keeps the workflow useful, auditable, and safe.
