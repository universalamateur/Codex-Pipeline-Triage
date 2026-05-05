# Demo Script

> Purpose: five-minute Loom runbook for Codex Pipeline Triage.
> Keep real tokens, webhook secrets, OAuth values, cookies, tunnel URLs, and raw
> webhook payloads in local-only files or environment variables. Do not paste
> them into this file.

## Preflight

- Use only the synthetic GitLab demo project.
- Start the app locally and confirm `/health` returns
  `{"status":"ok","service":"codex-pipeline-triage"}`.
- Sign in with GitLab as an allowed group member.
- Confirm the synthetic project is connected and the webhook page shows Pipeline
  events enabled and Job events disabled.
- Note the actual local uvicorn port. The examples below use `8765`, but use
  the port the app is really listening on.
- For GitLab webhook delivery to a laptop, run a temporary tunnel locally:

```bash
cloudflared tunnel --url http://127.0.0.1:<uvicorn-port>
```

- Keep `APP_BASE_URL` on the local app origin used for GitLab OAuth, for example
  `http://127.0.0.1:<uvicorn-port>`.
- When configuring GitLab, manually combine the temporary HTTPS tunnel origin
  with `/webhooks/gitlab/<connected_project_id>`. Do not use the app-rendered
  localhost webhook URL in GitLab.
- Use the temporary HTTPS tunnel URL only inside local GitLab webhook settings.
  Do not record or commit the URL.
- For the real Codex path, run with `PIPELINE_TRIAGE_MODE=codex` and
  `OPENAI_API_KEY` set locally. On this workstation, the local Codex binary may
  also need `PIPELINE_TRIAGE_CODEX_BIN=/opt/homebrew/bin/codex`.
- Keep controlled actions disabled until after the real Codex MR-note path is
  shown. Enable fix MR policy only for the controlled-action segment.
- Prepare the fresh failed MR through `.local/Fresh_MR_Demo_Prep.md` before
  starting the timed dry run. The timed script assumes the fresh MR pipeline,
  failed job ID, and app run already exist.

## Five-Minute Flow

| Time | Segment | Show | Talk track |
|---:|---|---|---|
| 0:00-0:25 | App and access | Health endpoint, signed-in app, connected project | This is a GitLab-native pipeline triage app. GitLab OAuth proves identity, and group membership gates access. |
| 0:25-0:55 | Webhook setup | Project webhook page and GitLab webhook config checkboxes only | The root trigger is GitLab Pipeline events. Job events stay disabled so individual job noise does not start workflows. Avoid showing or redact the webhook URL and secret token fields while recording. |
| 0:55-1:25 | Fresh failed MR | Helper-created synthetic MR failed pipeline in GitLab | The demo project is synthetic. The fresh prep helper created this MR and failed job so the demo starts from clean state. |
| 1:25-2:05 | Intake and run detail | App run history/detail | The app verified the webhook, classified the pipeline, fetched bounded context, and persisted the run. |
| 2:05-2:50 | Codex result | MR note marked `CODEX` | Codex ran server-side, returned structured output, and Pydantic validation accepted it before any GitLab action. |
| 2:50-3:35 | Safety boundary | Run detail and action logs | Codex does not mutate GitLab. Deterministic executor code posts notes only after schema validation and policy checks. |
| 3:35-4:30 | Controlled fix MR | Run-detail `Create bot fix MR` button, fix MR IID, commit SHA, action logs, monitor row | A human clicks the policy-gated action. The deterministic executor creates a bot fix MR and monitor row. It still never auto-merges. |
| 4:30-5:00 | Tests and close | Terminal test output | The important paths are covered by unit tests and manual gates. The demo is intentionally small and auditable. |

## What To Show In The App

1. `/health` returns `{"status":"ok","service":"codex-pipeline-triage"}`.
2. `/` shows signed-in state and GitLab group authorization.
3. `/projects` lists the synthetic connected project.
4. `/projects/<id>/webhook` shows the webhook URL and the Pipeline/Job event instructions.
5. `/projects/<id>/runs` shows the failed pipeline run.
6. `/projects/<id>/runs/<run-id>` shows:
   - pipeline ID and status.
   - pipeline kind.
   - adapter mode.
   - fallback reason, if any.
   - recommended action and policy action.
   - `Create bot fix MR` button only when the run and policy allow it.
   - fix MR IID after the controlled action runs.
   - fix commit SHA after the controlled action runs.
   - GitLab action logs.
   - follow-up monitor rows.

The run detail page must not show project tokens, webhook secrets, OAuth values,
cookies, tunnel URLs, raw webhook payloads, raw traces, raw diffs, or raw Codex
output.

## Controlled-Action Segment

Use this only after the main real Codex MR-note path is clear.

- Use the run-detail `Create bot fix MR` button.
- Show the button only when policy has `auto_create_fix_mr=true`,
  `recommend_only=false`, the run target is a merge request, schema-valid triage
  output exists, and no fix MR already exists.
- After clicking, show the fix MR IID, commit SHA, completed `create_commit`,
  `create_merge_request`, and `post_mr_note` action logs, plus the waiting
  monitor row.
- Monitor result: show the original run moving to `monitoring`, one waiting
  monitor row, and the later pass/fail monitor note.
- Explicitly say: no auto-merge, no direct Codex GitLab mutation, no unbounded
  loop.

## Closing Checks

Run or show the latest quality gate summary:

```bash
.venv/bin/python -m pytest -p no:cacheprovider
.venv/bin/python -m black --check .
.venv/bin/python -m isort --check-only .
.venv/bin/python -m flake8
.venv/bin/python -m mypy codex_pipeline_triage tests
PYLINTHOME=/private/tmp/codex-pipeline-triage-pylint .venv/bin/python -m pylint codex_pipeline_triage tests
```

Final line:

> This demo shows a real Codex SDK triage path inside a GitLab workflow, with
> every GitLab mutation kept behind schema validation, project policy, and
> deterministic executor code.
