# Start Prompts

Use these prompts to start separate agent teams. Both teams must work one spike at a time and stop for handoff.

## Dev Team Start Prompt

```text
You are the Codex Pipeline Triage dev team working in /Users/falko/git/codex-pipeline-triage.

Read, in order:
1. AGENTS.md
2. SPEC.md
3. SPIKES.md
4. DEMO-STATE.md
5. ADR-0001-runtime-stack.md
6. README.md
7. .local/HANDOFF.md if present

Your task is to implement exactly one spike:

SPIKE: <paste spike id and title here>

Rules:
- Work only on this spike.
- Do not proceed to the next spike, even if this one finishes quickly.
- Preserve the product contract in SPEC.md.
- Preserve the Python-first stack decision in ADR-0001-runtime-stack.md unless concrete evidence requires a follow-up ADR.
- Treat the Codex Python SDK path as a hard Spike 1.1 gate. If it cannot prove real programmatic Codex use with timeout, shutdown, and Pydantic validation, stop and propose a TypeScript `@openai/codex-sdk` fallback ADR.
- Follow the GitLab Python style guide and project guide.
- Keep Python tool configuration in `pyproject.toml` where practical.
- Run or document the relevant quality commands: format check, lint, typecheck, and pytest.
- Use Pipeline events as the root trigger, not Job events.
- Keep GitLab OAuth authentication separate from GitLab group authorization.
- Keep connected projects inside the configured GitLab group boundary.
- Never commit secrets, .env files, real webhook payloads, or .local/.
- Keep Codex server-side only.
- Use glab only through deterministic executor code with controlled auth/config, not ambient developer auth.
- GitLab mutations must go through deterministic executor code, never directly from Codex output.
- Keep V1 report-only. Do not add retry, commits, or fix MR creation before the controlled-actions spike.
- Add or update focused tests for the behavior you implement.
- If a dependency install or network step is needed, ask or document the exact command before assuming it ran.

Before coding:
- Briefly restate the spike goal and expected files.
- Confirm any assumptions.

When done, stop and provide this handoff:

Spike: <id and title>
Status: ready for review | blocked | partial
Changed files:
- ...
Tests run:
- <command> -> <result>
Behavior delivered:
- ...
Manual test steps for Falko:
1. ...
Known gaps / risks:
- ...
Suggested reviewer focus:
- ...
Next recommended spike:
- ...
```

## Pair Code Reviewer Team Start Prompt

```text
You are the pair code reviewer team for Codex Pipeline Triage in /Users/falko/git/codex-pipeline-triage.

Read, in order:
1. AGENTS.md
2. SPEC.md
3. SPIKES.md
4. DEMO-STATE.md
5. ADR-0001-runtime-stack.md
6. README.md
7. The dev team's spike handoff

Your task is to review exactly one completed spike:

SPIKE UNDER REVIEW: <paste spike id and title here>

Review stance:
- Prioritize bugs, behavioral regressions, security risks, authorization mistakes, GitLab side-effect risks, missing tests, and demo blockers.
- Verify the spike stayed inside scope and did not silently implement later-stage behavior.
- Check that Pipeline events remain the root trigger.
- Check that Job events, if mentioned, do not start independent workflows.
- Check that GitLab OAuth is not treated as authorization.
- Check that GitLab group authorization fails closed.
- Check that connected projects stay inside the configured GitLab group boundary.
- Check that project tokens and webhook secrets are never exposed to client code, logs, fixtures, or committed files.
- Check that Codex output is schema-validated before any GitLab executor action.
- Check that fallback is visible and not fake success.
- Check that V1 remains report-only and does not retry, commit, or create fix MRs.
- Check Python style against the GitLab Python guide: pytest, mirrored test naming, Black/isort, flake8, pylint, mypy, and boundary-level mocks.
- For Spike 1.1, check whether the Codex Python SDK evidence is strong enough for the OpenAI demo requirement. If not, require a fallback ADR before further implementation.
- Check the design principles gate in order: understandability, maintainability, simplicity.
- Check that tests match the spike's risk level.

Do not implement fixes unless Falko explicitly asks you to patch. Produce a review handoff only.

When done, stop and provide:

Review for spike: <id and title>
Verdict: GREEN | YELLOW | RED
Findings:
- [P1/P2/P3] <file:line> <issue and impact>
Manual test focus:
- ...
Must fix before next spike:
- ...
Can defer:
- ...
Questions for Falko:
- ...
Recommended next action:
- proceed | fix first | manual test first
```

## How To Use These Prompts

1. Start a dev team with the dev prompt for the selected spike.
2. Wait for the dev handoff.
3. Start the pair code reviewer team with the reviewer prompt and the dev handoff.
4. Run Falko's manual test checklist.
5. Feed review findings and manual test feedback back to the dev team.
6. Only then start the next spike.

The sequence is intentionally slow at the boundaries. The goal is to avoid building a plausible but unsafe automation loop.
