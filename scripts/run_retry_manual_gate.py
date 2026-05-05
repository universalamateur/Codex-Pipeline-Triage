"""Run the local-only Spike 7.1 deterministic retry manual gate."""

# flake8: noqa: E402
# isort: skip_file

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal

from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from codex_pipeline_triage.app import create_app  # noqa: E402
from codex_pipeline_triage.auth import AuthSettings, InMemorySessionStore  # noqa: E402
from codex_pipeline_triage.codex_adapter import CodexTriageOutcome  # noqa: E402
from codex_pipeline_triage.gitlab import GlabExecutor  # noqa: E402
from codex_pipeline_triage.models import (  # noqa: E402
    ConnectedProject,
    DiffFileContext,
    EvidenceItem,
    JobTraceContext,
    MergeRequestTarget,
    PipelineContext,
    PipelineJobSummary,
    TriageResult,
    TriageRun,
)
from codex_pipeline_triage.persistence import SqliteStore  # noqa: E402
from codex_pipeline_triage.projects import (  # noqa: E402
    DEFAULT_GLAB_CONFIG_DIR,
    GitLabProjectMetadata,
    ProjectConnectionError,
    ProjectConnector,
)
from codex_pipeline_triage.reporting import (  # noqa: E402
    GlabGitLabIssueClient,
    GlabGitLabMrNoteClient,
    GlabGitLabRetryClient,
    MockMrReporter,
    PIPELINE_TRIAGE_MODE_CODEX,
)

MANUAL_GATE_FALLBACK_REASON = "Spike 7.1 deterministic manual retry gate."
RetryAction = Literal["retry_job", "retry_pipeline"]


@dataclass(frozen=True)
class EnvProjectTokenStore:
    """Resolve project tokens from one local env var without printing them."""

    env_name: str

    def store_project_token(self, project_token: str) -> str:
        del project_token
        raise ProjectConnectionError("Manual gate does not store project tokens.")

    def retrieve_project_token(self, secret_ref: str) -> str:
        del secret_ref
        project_token = os.environ.get(self.env_name, "").strip()
        if not project_token:
            raise ProjectConnectionError(
                f"Project token env var {self.env_name} is not set."
            )
        return project_token


class UnusedGitLabProjectClient:
    """Project metadata lookup is intentionally unused by the replay gate."""

    def get_project_metadata(
        self,
        *,
        project_reference: str,
        project_token: str,
    ) -> GitLabProjectMetadata:
        del project_reference, project_token
        raise ProjectConnectionError("Manual gate does not connect projects.")


@dataclass(frozen=True)
class ManualGateContextBuilder:
    """Persist a bounded synthetic context for the generated Pipeline Hook."""

    persistence_store: SqliteStore
    action: RetryAction

    def build_for_run(
        self,
        *,
        connected_project: ConnectedProject,
        triage_run: TriageRun,
    ) -> TriageRun:
        now = datetime.now(tz=timezone.utc)
        job_id = triage_run.job_ids[0] if triage_run.job_ids else 0
        context = PipelineContext(
            project_id=connected_project.gitlab_project_id,
            pipeline_id=triage_run.pipeline_id,
            pipeline_kind=triage_run.pipeline_kind,
            report_target=triage_run.report_target,
            jobs=[
                PipelineJobSummary(
                    id=job_id,
                    name="manual-gate-transient-test",
                    status="failed",
                    stage="test",
                    web_url=None,
                )
            ],
            failed_job_traces=[
                JobTraceContext(
                    job_id=job_id,
                    job_name="manual-gate-transient-test",
                    trace_excerpt=(
                        "Synthetic transient runner failure for Spike 7.1 "
                        "manual retry gate."
                    ),
                    trace_digest=_digest(f"trace:{triage_run.pipeline_id}:{job_id}"),
                    truncated=False,
                )
            ],
            diffs=[
                DiffFileContext(
                    old_path="manual_gate.py",
                    new_path="manual_gate.py",
                    diff_excerpt="No code diff is required for this transient gate.",
                    diff_digest=_digest(f"diff:{triage_run.pipeline_id}:{self.action}"),
                    truncated=False,
                )
            ],
            context_digest=_digest(
                f"context:{connected_project.gitlab_project_id}:"
                f"{triage_run.pipeline_id}:{self.action}"
            ),
            created_at=now,
        )
        updated_run = triage_run.model_copy(
            update={
                "context_json": context,
                "context_digest": context.context_digest,
                "fallback_reason": "Spike 7.1 manual gate context ready.",
                "updated_at": now,
            }
        )
        return self.persistence_store.update_triage_run(updated_run)


@dataclass(frozen=True)
class ManualRetryTriageProvider:
    """Return one schema-validated deterministic retry recommendation."""

    action: RetryAction

    async def triage(self, context: PipelineContext) -> CodexTriageOutcome:
        triage_result = TriageResult.model_validate(
            {
                "root_cause_hypothesis": (
                    "The manual gate context represents a transient runner "
                    "failure and is safe to retry once."
                ),
                "category": "infra",
                "confidence": 0.91,
                "evidence": [
                    EvidenceItem(
                        source="job_trace",
                        file=None,
                        line=None,
                        snippet=(
                            "Synthetic transient runner failure for Spike 7.1 "
                            "manual retry gate."
                        ),
                    ).model_dump(mode="json")
                ],
                "retry_safe": True,
                "recommended_action": self.action,
                "suggested_fix": "Retry once through the deterministic executor.",
                "needs_human_review": False,
            }
        )
        return CodexTriageOutcome(
            adapter_mode="mock",
            triage_result=triage_result,
            fallback_reason=MANUAL_GATE_FALLBACK_REASON,
        )


def run_gate(args: argparse.Namespace) -> dict[str, object]:
    webhook_token = os.environ.get(args.webhook_token_env, "").strip()
    if not webhook_token:
        raise RuntimeError(
            f"Webhook token env var {args.webhook_token_env} is not set."
        )

    store = SqliteStore(args.db_path)
    project = store.get_connected_project(args.connected_project_id)
    if project is None:
        raise RuntimeError("connected project was not found in the DB")
    if project.action_policy.recommend_only:
        raise RuntimeError("connected project policy is recommend_only=true")
    if not project.action_policy.auto_retry:
        raise RuntimeError("connected project policy has auto_retry=false")
    if project.gitlab_project_id != args.project_id:
        raise RuntimeError("connected project ID does not match --project-id")

    token_store = EnvProjectTokenStore(env_name=args.project_token_env)
    settings = AuthSettings(
        gitlab_base_url=args.gitlab_base_url,
        auth_allowlist_mode="gitlab_group",
        allowed_gitlab_group_id=0,
    )
    project_connector = ProjectConnector(
        settings=settings,
        gitlab_project_client=UnusedGitLabProjectClient(),
        token_store=token_store,
        persistence_store=store,
    )
    executor = GlabExecutor(
        config_dir=args.glab_config_dir,
        glab_bin=args.glab_bin,
        hostname=args.gitlab_hostname,
    )
    reporter = MockMrReporter(
        mr_note_client=GlabGitLabMrNoteClient(executor=executor),
        issue_client=GlabGitLabIssueClient(executor=executor),
        retry_client=GlabGitLabRetryClient(executor=executor),
        token_store=token_store,
        persistence_store=store,
        triage_mode=PIPELINE_TRIAGE_MODE_CODEX,
        codex_adapter=ManualRetryTriageProvider(action=args.action),
    )
    client = TestClient(
        create_app(
            auth_settings=settings,
            session_store=InMemorySessionStore(),
            project_connector=project_connector,
            context_builder=ManualGateContextBuilder(
                persistence_store=store,
                action=args.action,
            ),
            mock_mr_reporter=reporter,
        ),
        base_url="https://manual-gate.local",
    )

    raw_body = _pipeline_hook_body(args)
    path = f"/webhooks/gitlab/{args.connected_project_id}"
    first = client.post(path, content=raw_body, headers=_headers(webhook_token))
    first_snapshot = _snapshot(
        store=store,
        project=project,
        pipeline_id=args.pipeline_id,
        action=args.action,
    )
    duplicate = client.post(path, content=raw_body, headers=_headers(webhook_token))
    duplicate_snapshot = _snapshot(
        store=store,
        project=project,
        pipeline_id=args.pipeline_id,
        action=args.action,
    )
    job_hook = client.post(
        path,
        content=b'{"object_kind":"build","build_status":"failed"}',
        headers=_headers(webhook_token, event="Job Hook"),
    )
    job_snapshot = _snapshot(
        store=store,
        project=project,
        pipeline_id=args.pipeline_id,
        action=args.action,
    )

    result = {
        "first_status": first.status_code,
        "duplicate_status": duplicate.status_code,
        "job_hook_status": job_hook.status_code,
        "run": job_snapshot,
        "duplicate_idempotent": duplicate_snapshot == first_snapshot,
        "job_hook_ignored": job_snapshot == duplicate_snapshot,
    }
    _validate_gate_result(result)
    return result


def _pipeline_hook_body(args: argparse.Namespace) -> bytes:
    payload = {
        "object_kind": "pipeline",
        "object_attributes": {
            "id": args.pipeline_id,
            "status": "failed",
            "ref": args.ref,
            "sha": args.sha,
            "source": "merge_request_event",
            "tag": False,
        },
        "merge_request": {"iid": args.merge_request_iid},
        "project": {"id": args.project_id},
        "builds": [
            {
                "id": args.job_id,
                "name": "manual-gate-transient-test",
                "status": "failed",
            }
        ],
    }
    return json.dumps(payload, separators=(",", ":"), sort_keys=True).encode("utf-8")


def _headers(webhook_token: str, *, event: str = "Pipeline Hook") -> dict[str, str]:
    return {
        "X-Gitlab-Event": event,
        "X-Gitlab-Token": webhook_token,
        "Content-Type": "application/json",
    }


def _snapshot(
    *,
    store: SqliteStore,
    project: ConnectedProject,
    pipeline_id: int,
    action: RetryAction,
) -> dict[str, object]:
    run = store.get_triage_run_by_pipeline(
        gitlab_project_id=project.gitlab_project_id,
        pipeline_id=pipeline_id,
    )
    if run is None:
        return {"found": False}
    action_logs = store.list_action_logs_for_run(run.id)
    retry_logs = [log for log in action_logs if log.action == action]
    post_note_logs = [
        log for log in action_logs if log.action in ("post_mr_note", "post_issue_note")
    ]
    completed_retry_logs = [log for log in retry_logs if log.status == "completed"]
    return {
        "found": True,
        "pipeline_id": run.pipeline_id,
        "status": run.status,
        "adapter_mode": run.adapter_mode,
        "fallback_reason": run.fallback_reason,
        "recommended_action": (
            run.triage_json.recommended_action if run.triage_json else None
        ),
        "retry_safe": run.triage_json.retry_safe if run.triage_json else None,
        "action_plan": run.action_plan.action if run.action_plan else None,
        "note_count": len(run.gitlab_note_ids),
        "retry_log_count": len(retry_logs),
        "completed_retry_log_count": len(completed_retry_logs),
        "post_note_log_count": len(post_note_logs),
        "retry_external_ids": [log.external_id for log in completed_retry_logs],
        "action_log_count": len(action_logs),
    }


def _validate_gate_result(result: dict[str, object]) -> None:
    run = result["run"]
    if not isinstance(run, dict):
        raise RuntimeError("run snapshot was not available")
    expected = {
        "first_status": 202,
        "duplicate_status": 204,
        "job_hook_status": 204,
    }
    for key, value in expected.items():
        if result.get(key) != value:
            raise RuntimeError(f"{key} was {result.get(key)!r}, expected {value}")
    checks = {
        "found": True,
        "status": "actioned",
        "retry_safe": True,
        "note_count": 2,
        "retry_log_count": 1,
        "completed_retry_log_count": 1,
        "post_note_log_count": 2,
    }
    for key, value in checks.items():
        if run.get(key) != value:
            raise RuntimeError(f"run.{key} was {run.get(key)!r}, expected {value!r}")
    if not result.get("duplicate_idempotent"):
        raise RuntimeError("duplicate delivery changed the run/action snapshot")
    if not result.get("job_hook_ignored"):
        raise RuntimeError("Job Hook changed the run/action snapshot")


def _digest(value: str) -> str:
    return f"sha256:{hashlib.sha256(value.encode('utf-8')).hexdigest()}"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Run the local deterministic Spike 7.1 retry manual gate."
    )
    parser.add_argument("--db-path", type=Path, required=True)
    parser.add_argument("--connected-project-id", required=True)
    parser.add_argument("--project-id", type=int, required=True)
    parser.add_argument("--pipeline-id", type=int, required=True)
    parser.add_argument("--job-id", type=int, required=True)
    parser.add_argument("--merge-request-iid", type=int, required=True)
    parser.add_argument(
        "--action",
        choices=("retry_job", "retry_pipeline"),
        default="retry_job",
    )
    parser.add_argument("--ref", default="manual-gate/transient-retry")
    parser.add_argument("--sha", default="manualgate0000000000000000000000000000000000")
    parser.add_argument(
        "--project-token-env",
        default="PIPELINE_TRIAGE_MANUAL_PROJECT_TOKEN",
    )
    parser.add_argument(
        "--webhook-token-env",
        default="PIPELINE_TRIAGE_MANUAL_WEBHOOK_TOKEN",
    )
    parser.add_argument(
        "--glab-config-dir", type=Path, default=Path(DEFAULT_GLAB_CONFIG_DIR)
    )
    parser.add_argument("--glab-bin", default="glab")
    parser.add_argument("--gitlab-base-url", default="https://gitlab.com")
    parser.add_argument("--gitlab-hostname", default="gitlab.com")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    try:
        result = run_gate(args)
    except RuntimeError as exc:
        print(f"FAIL {exc}", file=sys.stderr)
        return 2
    print("PASS " + json.dumps(result, separators=(",", ":"), sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
