"""Tests for Spike 4.1 fixture-driven GitLab webhook intake."""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, cast

from fastapi.testclient import TestClient

from codex_pipeline_triage.app import create_app
from codex_pipeline_triage.auth import InMemorySessionStore
from codex_pipeline_triage.context import (
    ContextBuildError,
    GitLabContextJob,
    PipelineContextBuilder,
)
from codex_pipeline_triage.models import (
    ConnectedProject,
    InternalTarget,
    IssueTarget,
    MergeRequestTarget,
    PipelineContext,
)
from codex_pipeline_triage.persistence import SqliteStore
from codex_pipeline_triage.projects import (
    ProjectConnector,
)
from codex_pipeline_triage.reporting import MockMrReporter
from tests.codex_pipeline_triage.helpers import make_auth_settings
from tests.codex_pipeline_triage.test_context import (
    FakeGitLabContextClient,
    make_connected_project,
    make_triage_run,
)
from tests.codex_pipeline_triage.test_projects import (
    FakeGitLabProjectClient,
    RecordingProjectTokenStore,
)
from tests.codex_pipeline_triage.test_reporting import (
    RecordingIssueClient,
    RecordingMrNoteClient,
)

FIXTURE_DIR = Path(__file__).parent / "fixtures"


def test_webhook_rejects_bad_token(tmp_path: Path) -> None:
    client, store, project = make_webhook_client(tmp_path)

    response = client.post(
        f"/webhooks/gitlab/{project.id}",
        content=failed_pipeline_body(),
        headers=webhook_headers(token="wrong-token"),
    )

    assert response.status_code == 401
    assert store.list_triage_runs_for_project(project.id) == []


def test_webhook_rejects_unknown_connected_project(tmp_path: Path) -> None:
    client, store, project = make_webhook_client(tmp_path)

    response = client.post(
        "/webhooks/gitlab/missing-project",
        content=failed_pipeline_body(),
        headers=webhook_headers(),
    )

    assert response.status_code == 401
    assert store.list_triage_runs_for_project(project.id) == []


def test_webhook_rejects_bad_token_before_payload_validation(tmp_path: Path) -> None:
    client, store, project = make_webhook_client(tmp_path)

    response = client.post(
        f"/webhooks/gitlab/{project.id}",
        content=b"{not-json",
        headers=webhook_headers(token="wrong-token"),
    )

    assert response.status_code == 401
    assert store.list_triage_runs_for_project(project.id) == []


def test_webhook_ignores_non_failed_pipeline(tmp_path: Path) -> None:
    client, store, project = make_webhook_client(tmp_path)
    payload = failed_pipeline_payload()
    object_attributes = cast(dict[str, Any], payload["object_attributes"])
    object_attributes["status"] = "success"

    response = client.post(
        f"/webhooks/gitlab/{project.id}",
        json=payload,
        headers=webhook_headers(),
    )

    assert response.status_code == 204
    assert store.list_triage_runs_for_project(project.id) == []


def test_webhook_ignores_non_pipeline_event(tmp_path: Path) -> None:
    client, store, project = make_webhook_client(tmp_path)

    response = client.post(
        f"/webhooks/gitlab/{project.id}",
        content=failed_pipeline_body(),
        headers=webhook_headers(event="Job Hook"),
    )

    assert response.status_code == 204
    assert store.list_triage_runs_for_project(project.id) == []


def test_webhook_ignores_job_shaped_job_hook(tmp_path: Path) -> None:
    client, store, project = make_webhook_client(tmp_path)

    response = client.post(
        f"/webhooks/gitlab/{project.id}",
        json={
            "object_kind": "build",
            "build_id": 4001,
            "build_status": "failed",
            "project_id": project.gitlab_project_id,
        },
        headers=webhook_headers(event="Job Hook"),
    )

    assert response.status_code == 204
    assert store.list_triage_runs_for_project(project.id) == []


def test_failed_pipeline_webhook_creates_triage_run(tmp_path: Path) -> None:
    client, store, project = make_webhook_client(tmp_path)

    response = client.post(
        f"/webhooks/gitlab/{project.id}",
        content=failed_pipeline_body(),
        headers=webhook_headers(),
    )

    assert response.status_code == 202
    runs = store.list_triage_runs_for_project(project.id)
    assert len(runs) == 1
    assert runs[0].gitlab_project_id == project.gitlab_project_id
    assert runs[0].pipeline_id == 9102
    assert runs[0].job_ids == [4201]
    assert runs[0].ref == "feature/checkout-tax"
    assert runs[0].sha == "branchabc123"
    assert runs[0].pipeline_kind == "branch"
    assert isinstance(runs[0].report_target, IssueTarget)
    assert runs[0].report_target.project_id == project.gitlab_project_id
    assert runs[0].report_target.issue_iid == 7001
    assert runs[0].issue_iid == 7001
    assert runs[0].status == "posted"
    assert runs[0].adapter_mode == "mock"
    assert runs[0].fallback_reason == "Spike 5.3 deterministic branch issue reporting."
    assert runs[0].input_digest == expected_digest(failed_pipeline_body())
    assert runs[0].context_json is not None
    assert runs[0].context_digest == runs[0].context_json.context_digest
    action_logs = store.list_action_logs_for_run(runs[0].id)
    assert {action_log.action for action_log in action_logs} == {
        "create_issue",
        "post_issue_note",
    }


def test_mr_pipeline_routes_to_merge_request_target(tmp_path: Path) -> None:
    client, store, project = make_webhook_client(tmp_path)
    raw_body = fixture_body("pipeline_failed_mr.json")

    response = client.post(
        f"/webhooks/gitlab/{project.id}",
        content=raw_body,
        headers=webhook_headers(),
    )

    assert response.status_code == 202
    runs = store.list_triage_runs_for_project(project.id)
    assert len(runs) == 1
    assert runs[0].pipeline_kind == "merge_request"
    assert isinstance(runs[0].report_target, MergeRequestTarget)
    assert runs[0].report_target.project_id == project.gitlab_project_id
    assert runs[0].report_target.merge_request_iid == 17
    assert runs[0].pipeline_id == 9101
    assert runs[0].job_ids == [4101]
    assert runs[0].input_digest == expected_digest(raw_body)
    assert runs[0].context_json is not None
    assert runs[0].context_json.report_target.type == "merge_request"
    assert runs[0].status == "posted"
    assert runs[0].triage_json is not None
    assert runs[0].action_plan is not None
    assert runs[0].gitlab_note_ids == [9001]
    action_logs = store.list_action_logs_for_run(runs[0].id)
    assert len(action_logs) == 1
    assert action_logs[0].action == "post_mr_note"
    assert action_logs[0].status == "completed"


def test_branch_pipeline_routes_to_issue_target(tmp_path: Path) -> None:
    client, store, project = make_webhook_client(tmp_path)
    raw_body = fixture_body("pipeline_failed_branch.json")

    response = client.post(
        f"/webhooks/gitlab/{project.id}",
        content=raw_body,
        headers=webhook_headers(),
    )

    assert response.status_code == 202
    runs = store.list_triage_runs_for_project(project.id)
    assert len(runs) == 1
    assert runs[0].pipeline_kind == "branch"
    assert isinstance(runs[0].report_target, IssueTarget)
    assert runs[0].report_target.project_id == project.gitlab_project_id
    assert runs[0].report_target.issue_iid == 7001
    assert runs[0].issue_iid == 7001
    assert runs[0].context_json is not None
    assert runs[0].context_json.report_target.type == "issue"
    assert runs[0].status == "posted"
    assert runs[0].gitlab_note_ids == [9101]
    action_logs = store.list_action_logs_for_run(runs[0].id)
    action_logs_by_action = {
        action_log.action: action_log for action_log in action_logs
    }
    assert set(action_logs_by_action) == {"create_issue", "post_issue_note"}
    assert action_logs_by_action["create_issue"].status == "completed"
    assert action_logs_by_action["post_issue_note"].status == "completed"


def test_tag_pipeline_is_report_only(tmp_path: Path) -> None:
    client, store, project = make_webhook_client(tmp_path)

    response = client.post(
        f"/webhooks/gitlab/{project.id}",
        content=fixture_body("pipeline_failed_tag.json"),
        headers=webhook_headers(),
    )

    assert response.status_code == 202
    runs = store.list_triage_runs_for_project(project.id)
    assert len(runs) == 1
    assert runs[0].pipeline_kind == "tag"
    assert isinstance(runs[0].report_target, InternalTarget)
    assert runs[0].context_json is not None
    assert runs[0].context_json.diffs == []
    assert runs[0].status == "ignored"
    assert store.list_action_logs_for_run(runs[0].id) == []


def test_child_or_parent_pipeline_is_report_only(tmp_path: Path) -> None:
    client, store, project = make_webhook_client(tmp_path)

    response = client.post(
        f"/webhooks/gitlab/{project.id}",
        content=fixture_body("pipeline_failed_child.json"),
        headers=webhook_headers(),
    )

    assert response.status_code == 202
    runs = store.list_triage_runs_for_project(project.id)
    assert len(runs) == 1
    assert runs[0].pipeline_kind == "child_or_parent"
    assert isinstance(runs[0].report_target, InternalTarget)
    assert runs[0].context_json is not None
    assert runs[0].context_json.diffs == []
    assert store.list_action_logs_for_run(runs[0].id) == []


def test_unknown_pipeline_is_report_only(tmp_path: Path) -> None:
    client, store, project = make_webhook_client(tmp_path)

    response = client.post(
        f"/webhooks/gitlab/{project.id}",
        content=fixture_body("pipeline_failed_unknown.json"),
        headers=webhook_headers(),
    )

    assert response.status_code == 202
    runs = store.list_triage_runs_for_project(project.id)
    assert len(runs) == 1
    assert runs[0].pipeline_kind == "unknown"
    assert isinstance(runs[0].report_target, InternalTarget)
    assert runs[0].context_json is not None
    assert runs[0].context_json.diffs == []
    assert store.list_action_logs_for_run(runs[0].id) == []


def test_duplicate_pipeline_webhook_does_not_create_second_run(
    tmp_path: Path,
) -> None:
    client, store, project = make_webhook_client(tmp_path)

    first_response = client.post(
        f"/webhooks/gitlab/{project.id}",
        content=failed_pipeline_body(),
        headers=webhook_headers(),
    )
    second_response = client.post(
        f"/webhooks/gitlab/{project.id}",
        content=failed_pipeline_body(),
        headers=webhook_headers(),
    )

    assert first_response.status_code == 202
    assert second_response.status_code == 204
    runs = store.list_triage_runs_for_project(project.id)
    assert len(runs) == 1
    assert runs[0].gitlab_note_ids == [9101]
    assert len(store.list_action_logs_for_run(runs[0].id)) == 2


def test_duplicate_mr_pipeline_webhook_does_not_create_second_note(
    tmp_path: Path,
) -> None:
    client, store, project = make_webhook_client(tmp_path)
    raw_body = fixture_body("pipeline_failed_mr.json")

    first_response = client.post(
        f"/webhooks/gitlab/{project.id}",
        content=raw_body,
        headers=webhook_headers(),
    )
    second_response = client.post(
        f"/webhooks/gitlab/{project.id}",
        content=raw_body,
        headers=webhook_headers(),
    )

    assert first_response.status_code == 202
    assert second_response.status_code == 204
    runs = store.list_triage_runs_for_project(project.id)
    assert len(runs) == 1
    assert runs[0].gitlab_note_ids == [9001]
    assert len(store.list_action_logs_for_run(runs[0].id)) == 1


def test_existing_context_mr_run_without_note_is_reported_on_replay(
    tmp_path: Path,
) -> None:
    client, store, project = make_webhook_client(tmp_path)
    report_target = MergeRequestTarget(
        project_id=project.gitlab_project_id,
        merge_request_iid=17,
    )
    context = PipelineContext(
        project_id=project.gitlab_project_id,
        pipeline_id=9101,
        pipeline_kind="merge_request",
        report_target=report_target,
        jobs=[],
        failed_job_traces=[],
        diffs=[],
        context_digest="sha256:existing-context",
        created_at=TEST_TIME,
    )
    existing_run = make_triage_run(
        project,
        pipeline_kind="merge_request",
        report_target=report_target,
    ).model_copy(
        update={
            "id": "existing-run-with-context",
            "pipeline_id": 9101,
            "job_ids": [4101],
            "ref": "feature/checkout-tax",
            "sha": "mrabc123",
            "context_json": context,
            "context_digest": context.context_digest,
        }
    )
    store.create_triage_run(existing_run)

    first_response = client.post(
        f"/webhooks/gitlab/{project.id}",
        content=fixture_body("pipeline_failed_mr.json"),
        headers=webhook_headers(),
    )
    second_response = client.post(
        f"/webhooks/gitlab/{project.id}",
        content=fixture_body("pipeline_failed_mr.json"),
        headers=webhook_headers(),
    )

    assert first_response.status_code == 202
    assert second_response.status_code == 204
    runs = store.list_triage_runs_for_project(project.id)
    assert len(runs) == 1
    assert runs[0].status == "posted"
    assert runs[0].gitlab_note_ids == [9001]
    assert len(store.list_action_logs_for_run(runs[0].id)) == 1


def test_failed_pipeline_marks_run_failed_when_context_build_fails(
    tmp_path: Path,
) -> None:
    client, store, project = make_webhook_client(
        tmp_path,
        context_client=FailingGitLabContextClient(),
    )

    response = client.post(
        f"/webhooks/gitlab/{project.id}",
        content=failed_pipeline_body(),
        headers=webhook_headers(),
    )

    assert response.status_code == 202
    runs = store.list_triage_runs_for_project(project.id)
    assert len(runs) == 1
    assert runs[0].status == "failed"
    assert runs[0].fallback_reason == "Context builder failed."
    assert runs[0].context_json is None
    assert store.list_action_logs_for_run(runs[0].id) == []


def test_webhook_rejects_project_id_mismatch(tmp_path: Path) -> None:
    client, store, project = make_webhook_client(tmp_path)
    payload = failed_pipeline_payload()
    project_payload = cast(dict[str, Any], payload["project"])
    project_payload["id"] = 9999

    response = client.post(
        f"/webhooks/gitlab/{project.id}",
        json=payload,
        headers=webhook_headers(),
    )

    assert response.status_code == 401
    assert store.list_triage_runs_for_project(project.id) == []


def test_webhook_rejects_disabled_project(tmp_path: Path) -> None:
    client, store, project = make_webhook_client(tmp_path, enabled=False)

    response = client.post(
        f"/webhooks/gitlab/{project.id}",
        content=failed_pipeline_body(),
        headers=webhook_headers(),
    )

    assert response.status_code == 401
    assert store.list_triage_runs_for_project(project.id) == []


def test_webhook_rejects_malformed_payload(tmp_path: Path) -> None:
    client, store, project = make_webhook_client(tmp_path)

    response = client.post(
        f"/webhooks/gitlab/{project.id}",
        content=b"{not-json",
        headers=webhook_headers(),
    )

    assert response.status_code == 400
    assert store.list_triage_runs_for_project(project.id) == []


def make_webhook_client(
    tmp_path: Path,
    *,
    enabled: bool = True,
    context_client: FakeGitLabContextClient | None = None,
) -> tuple[TestClient, SqliteStore, ConnectedProject]:
    settings = make_auth_settings()
    store = SqliteStore(tmp_path / "triage.sqlite")
    connected_project = make_connected_project().model_copy(
        update={
            "webhook_secret_hash": webhook_secret_hash("webhook-secret"),
            "enabled": enabled,
        }
    )
    store.create_connected_project(connected_project)
    token_store = RecordingProjectTokenStore()
    token_store.tokens["secret-ref:1"] = "project-token"
    project_connector = ProjectConnector(
        settings=settings,
        gitlab_project_client=FakeGitLabProjectClient(),
        token_store=token_store,
        persistence_store=store,
    )
    context_builder = PipelineContextBuilder(
        gitlab_context_client=context_client or FakeGitLabContextClient(),
        token_store=token_store,
        persistence_store=store,
    )
    mock_mr_reporter = MockMrReporter(
        mr_note_client=RecordingMrNoteClient(),
        issue_client=RecordingIssueClient(),
        token_store=token_store,
        persistence_store=store,
    )
    client = TestClient(
        create_app(
            auth_settings=settings,
            session_store=InMemorySessionStore(),
            project_connector=project_connector,
            context_builder=context_builder,
            mock_mr_reporter=mock_mr_reporter,
        ),
        base_url="https://testserver",
    )
    return client, store, connected_project


TEST_TIME = datetime(2026, 5, 4, 12, 0, tzinfo=timezone.utc)


def failed_pipeline_body() -> bytes:
    return fixture_body("pipeline_failed_branch.json")


def failed_pipeline_payload() -> dict[str, object]:
    return cast(dict[str, object], json.loads(failed_pipeline_body().decode("utf-8")))


def fixture_body(file_name: str) -> bytes:
    return (FIXTURE_DIR / file_name).read_bytes()


def webhook_headers(
    *,
    token: str = "webhook-secret",
    event: str = "Pipeline Hook",
) -> dict[str, str]:
    return {
        "X-Gitlab-Event": event,
        "X-Gitlab-Token": token,
        "Content-Type": "application/json",
    }


def webhook_secret_hash(raw_secret: str) -> str:
    return f"sha256:{hashlib.sha256(raw_secret.encode('utf-8')).hexdigest()}"


def expected_digest(raw_body: bytes) -> str:
    return f"sha256:{hashlib.sha256(raw_body).hexdigest()}"


class FailingGitLabContextClient(FakeGitLabContextClient):
    """Fake context client that simulates a read failure."""

    def list_pipeline_jobs(
        self,
        *,
        project_id: int,
        pipeline_id: int,
        project_token: str,
    ) -> list[GitLabContextJob]:
        raise ContextBuildError("synthetic context failure")
