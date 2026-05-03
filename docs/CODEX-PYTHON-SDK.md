# Codex Python SDK Install And Probe

> Status: Spike 1.1 reviewer evidence
> Last updated: 2026-05-03

## Purpose

Spike 1.1 originally blocked on the experimental Codex Python SDK because
`codex_app_server` was not importable. Falko asked to keep the project
Python-first, so the SDK was installed from the official Codex repository into
this repo's local `uv` environment and re-tested.

This document records the exact local setup and proof.

## Source

Official guidance:

- OpenAI Codex SDK docs: https://developers.openai.com/codex/sdk#python-library

The Python SDK is experimental and requires:

- Python 3.10 or later.
- A local checkout of the open-source Codex repo.
- Editable install from `sdk/python`.
- For local repo development, an explicit `AppServerConfig(codex_bin=...)`
  pointing at a local `codex` binary.

## Local Install Commands

From this repo root:

```bash
git clone --depth 1 https://github.com/openai/codex .local/openai-codex
uv pip install -e .local/openai-codex/sdk/python
```

The checkout is intentionally under `.local/`, which is ignored by Git.

Installed source:

```text
remote: https://github.com/openai/codex
commit: 35aaa5d9fcb606fb6f27dd5747ecab3f4ba0c07e
commit date: 2026-05-01 23:33:32 -0700
commit subject: Bound websocket request sends with idle timeout (#20751)
```

Installed package evidence:

```text
-e file:///Users/falko/git/codex-pipeline-triage/.local/openai-codex/sdk/python
```

## Import Proof

Command:

```bash
.venv/bin/python -c 'from codex_app_server import AsyncCodex, AppServerConfig; print("codex python sdk import ok")'
```

Result:

```text
codex python sdk import ok
```

## Runtime Probe

The probe script is:

```text
scripts/probe_codex_python_sdk.py
```

It:

- imports `codex_app_server`;
- creates `AppServerConfig(codex_bin="/opt/homebrew/bin/codex")`;
- starts `AsyncCodex` with an async context manager;
- starts a Codex thread;
- runs a synthetic GitLab pipeline prompt;
- wraps the full lifecycle in `asyncio.wait_for`;
- validates `result.final_response` with a Pydantic schema;
- exits non-zero on import failure, timeout, empty output, validation failure,
  or runtime failure.

Command:

```bash
uv run python scripts/probe_codex_python_sdk.py \
  --codex-bin /opt/homebrew/bin/codex \
  --timeout-seconds 90
```

Result:

```json
{"root_cause_hypothesis":"The pipeline failed, but the pipeline-level signal alone is not enough to determine whether the cause is code, configuration, infrastructure, or a dependency problem.","category":"unknown","confidence":0.23,"evidence":[{"source":"pipeline","snippet":"Synthetic pipeline summary: status=failed, ref=main, source=push, with no job trace, diff, or test report attached."}],"retry_safe":false,"recommended_action":"recommend_only","suggested_fix":"Collect the failed job trace, relevant commit diff, and pipeline configuration before attempting a retry or proposing a fix.","needs_human_review":true}
```

## Current Conclusion

The Python SDK path is now viable for Spike 1.1:

- install surface works;
- `codex_app_server` import works;
- local `codex` binary is used explicitly;
- the async context manager starts and shuts down;
- one real Codex SDK turn completed inside a bounded timeout;
- the final response was parsed and validated with Pydantic.

The TypeScript sidecar fallback is not needed while this setup remains
reproducible.

## Reviewer Notes

- This is still an experimental SDK path. Keep the adapter boundary narrow.
- Do not import Codex SDK code into browser/client modules.
- Do not pass GitLab tokens or raw unbounded GitLab logs to Codex.
- Keep the app's final validation in Python with Pydantic before action
  planning or GitLab executor code.
- Re-run the runtime probe before accepting Spike 6.1 as real-mode ready.
