# ADR 0001 - Runtime Stack

> Status: accepted for Spike 1.1 validation
> Date: 2026-05-02

## Context

Codex Pipeline Triage should stay small enough to explain and maintain. The app needs:

- GitLab OAuth callback and group authorization.
- GitLab Pipeline-event webhook intake.
- Server-side Codex triage.
- SQLite persistence.
- A small configuration and run-history UI.
- Focused unit and fixture tests.

The workstation already has `glab` available, and GitLab's CLI exposes `glab api` for authenticated REST and GraphQL calls. OpenAI documents a Codex Python SDK, but the Python library is experimental and controls a local Codex app-server over JSON-RPC.

## Decision

Use a Python-first stack:

- Python 3.10+ managed with `uv`.
- FastAPI for HTTP routes, webhooks, sessions, and tests.
- Pydantic for request, event, and Codex-output validation.
- SQLite for demo persistence.
- pytest for tests.
- `glab` CLI wrapped by deterministic executor code for GitLab API calls.
- Codex Python SDK if Spike 1.1 verifies the local SDK installation and app-server flow.

FastHTML may be used for the small UI only if it reduces complexity compared with templates. It should not drive the webhook or executor architecture.

Follow the GitLab Python project and style guides where they fit this repo:

- Keep project metadata and tool configuration in `pyproject.toml` where practical.
- Use pytest for tests and pytest-cov once coverage is useful.
- Use Black, isort, flake8, pylint, and mypy as the initial quality gate.
- Use Pydantic for validation and settings.
- Use FastAPI for API/webhook routes.
- Use structured logging for executor and webhook paths.
- Name unit tests after the files they cover, for example `tests/codex_pipeline_triage/test_webhooks.py` for `codex_pipeline_triage/webhooks.py`.
- Use `unittest.mock` at service/API boundaries.
- Use named, self-documenting parametrized cases when test cases grow beyond a few simple values.

Intentional deviation: GitLab's project guide uses Poetry examples. This repo uses `uv` for the same dependency and environment-management role because the local workflow already standardizes on `uv`.

## Boundaries

- Do not use a developer's ambient `glab` session for app execution.
- Run `glab` non-interactively through a wrapper that controls environment, token source, timeout, arguments, output parsing, and logging.
- Do not pass secrets on command lines where they can appear in process listings or logs.
- Keep Codex server-side only.
- Validate Codex output with Pydantic before any action planning.
- GitLab mutations go through executor code after policy checks.

## Spike 1.1 Validation

Spike 1.1 must prove or document:

- FastAPI health route and test harness work.
- `uv` project setup is reproducible.
- `pyproject.toml` contains Black, isort, flake8, pylint, mypy, pytest, and pytest-cov configuration.
- Makefile or equivalent scripts expose `format`, `lint`, `typecheck`, `test`, and `test-cov`.
- `glab --version` and a mocked `glab api` wrapper path work.
- The Codex Python SDK path is viable or blocked with concrete evidence.
- If blocked, the smallest fallback adapter is proposed in a follow-up ADR before implementation moves beyond the skeleton.

## References

- OpenAI Codex SDK: https://developers.openai.com/codex/sdk
- GitLab CLI: https://docs.gitlab.com/cli/
- `glab api`: https://docs.gitlab.com/cli/api/
- GitLab Python style guide: https://docs.gitlab.com/development/python_guide/styleguide/
- GitLab Python project guide: https://docs.gitlab.com/development/python_guide/create_project/
