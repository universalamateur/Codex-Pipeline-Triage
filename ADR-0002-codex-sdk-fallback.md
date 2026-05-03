# ADR 0002 - Codex SDK Fallback Adapter

> Status: rejected for now
> Date: 2026-05-03

## Context

Spike 1.1 originally had to prove that the experimental Codex Python SDK can support
real programmatic Codex use with:

- installable local SDK surface;
- bounded timeout behavior;
- clean shutdown;
- JSON output parsed and validated by Pydantic.

The current official Codex SDK documentation describes the TypeScript SDK as
the server-side library path for application integration. It describes the
Python SDK as experimental, controlling a local Codex app-server over JSON-RPC,
and requiring a local checkout of the open-source Codex repository.

Local Spike 1.1 evidence:

- `codex --version` works: `codex-cli 0.128.0`.
- `glab --version` works: `glab 1.93.0`.
- `python3 -c 'import codex_app_server'` fails with
  `ModuleNotFoundError: No module named 'codex_app_server'`.
- `find /Users/falko/git -maxdepth 4 -type d -path '*/sdk/python'` found no
  local Codex Python SDK checkout.
- `uv run python scripts/probe_codex_python_sdk.py --timeout-seconds 15`
  exits with `blocked: codex_app_server is not importable`.

Follow-up evidence later installed the Python SDK from the official Codex repo
checkout under `.local/openai-codex` and ran a successful bounded SDK probe.
See [docs/CODEX-PYTHON-SDK.md](docs/CODEX-PYTHON-SDK.md).

This ADR is therefore not accepted while that Python path remains
reproducible. Keep it only as a contingency if the experimental Python SDK
regresses or becomes impractical for Spike 6.1.

## Rejected Fallback Decision

Keep the main application Python-first:

- FastAPI for HTTP routes, webhooks, sessions, and tests.
- Pydantic for request and output validation.
- SQLite for local/demo persistence.
- Deterministic Python executor code for GitLab actions.

If the Python SDK breaks again, fallback only the Codex adapter to a minimal server-side Node/TypeScript
sidecar using `@openai/codex-sdk`.

The Python app will call the sidecar through a narrow local process boundary:

```text
Python orchestrator
  -> redacted bounded context JSON on stdin
  -> TypeScript Codex adapter with timeout
  -> JSON result on stdout
  -> Python Pydantic validation
  -> deterministic action planner
```

The sidecar must:

- run server-side only;
- accept only redacted bounded context;
- use the Codex SDK, not a generic Responses API replacement;
- enforce a hard timeout;
- return only JSON matching the `TriageResult` contract;
- never receive GitLab tokens;
- never call GitLab;
- never authorize actions;
- exit non-zero on SDK errors, timeout, empty output, or malformed output.

The Python app remains responsible for:

- Pydantic validation;
- fallback visibility;
- persisted run and action audit records;
- project policy checks;
- all GitLab side effects through deterministic executor code.

## Consequences If Reopened

- Stage 1 can keep the Python/FastAPI skeleton.
- Product implementation should stop again until Falko accepts or rejects this
  fallback ADR.
- Spike 6.1 should implement the accepted adapter boundary. If this ADR is
  accepted, Spike 6.1 adds the TypeScript sidecar and Python process wrapper.
- The OpenAI demo's programmatic Codex requirement remains intact because the
  fallback uses `@openai/codex-sdk` server-side.
- The repository will need a small Node toolchain only for the Codex adapter if
  this ADR is accepted.

## Rejected Alternatives

- Continue with the Python SDK without proof. Rejected before the follow-up
  install. The follow-up probe now provides enough proof for Spike 1.1.
- Replace Codex with the generic OpenAI Responses API. Rejected because the
  implementation contract requires a real Codex surface.
- Rewrite the whole app in TypeScript. Rejected because the Python-first app
  stack is still simple and viable; only the Codex adapter is blocked.
