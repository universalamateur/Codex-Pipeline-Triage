"""Tests for Spike 3.1 project connection."""

# pylint: disable=duplicate-code

from __future__ import annotations

import hashlib
from collections.abc import Callable
from datetime import datetime, timezone
from pathlib import Path
from typing import cast
from unittest.mock import Mock

import pytest
from fastapi.testclient import TestClient

from codex_pipeline_triage.app import create_app
from codex_pipeline_triage.auth import GitLabIdentity, InMemorySessionStore
from codex_pipeline_triage.gitlab import GlabExecutor, GlabExecutorError
from codex_pipeline_triage.models import (
    ActionPlan,
    ConnectedProject,
    EvidenceItem,
    GitLabActionLog,
    MergeRequestTarget,
    PipelineMonitor,
    TriageResult,
    TriageRun,
)
from codex_pipeline_triage.persistence import SqliteStore
from codex_pipeline_triage.projects import (
    GitLabProjectMetadata,
    GlabGitLabProjectClient,
    ProjectConnectionError,
    ProjectConnector,
)
from tests.codex_pipeline_triage.helpers import make_auth_settings


class FakeGitLabProjectClient:
    """Mock GitLab project metadata boundary used by route tests."""

    def __init__(
        self,
        *,
        metadata: GitLabProjectMetadata | None = None,
        error: ProjectConnectionError | None = None,
    ) -> None:
        self.metadata = metadata or GitLabProjectMetadata(
            project_id=2002,
            path_with_namespace="universalamateur1/checkout-service",
            display_name="checkout-service",
            namespace_id=59032064,
        )
        self.error = error
        self.calls: list[tuple[str, str]] = []

    def get_project_metadata(
        self,
        *,
        project_reference: str,
        project_token: str,
    ) -> GitLabProjectMetadata:
        self.calls.append((project_reference, project_token))
        if self.error is not None:
            raise self.error
        return self.metadata


class RecordingProjectTokenStore:
    """Fake token boundary that records raw tokens server-side only."""

    def __init__(self) -> None:
        self.tokens: dict[str, str] = {}

    def store_project_token(self, project_token: str) -> str:
        secret_ref = f"secret-ref:{len(self.tokens) + 1}"
        self.tokens[secret_ref] = project_token
        return secret_ref

    def retrieve_project_token(self, secret_ref: str) -> str:
        return self.tokens[secret_ref]


def make_authenticated_client(
    tmp_path: Path,
    gitlab_project_client: FakeGitLabProjectClient,
    webhook_secret_generator: Callable[[], str] | None = None,
) -> tuple[TestClient, SqliteStore, RecordingProjectTokenStore]:
    settings = make_auth_settings()
    session_store = InMemorySessionStore()
    session = session_store.create_session(
        GitLabIdentity(
            gitlab_user_id=1001,
            gitlab_username="demo-user",
            display_name="Demo User",
        )
    )
    persistence_store = SqliteStore(tmp_path / "triage.sqlite")
    token_store = RecordingProjectTokenStore()
    project_connector = ProjectConnector(
        settings=settings,
        gitlab_project_client=gitlab_project_client,
        token_store=token_store,
        persistence_store=persistence_store,
        webhook_secret_generator=webhook_secret_generator
        or (lambda: "test-webhook-secret"),
    )
    client = TestClient(
        create_app(
            auth_settings=settings,
            session_store=session_store,
            project_connector=project_connector,
        ),
        base_url="https://testserver",
    )
    client.cookies.set(settings.session_cookie_name, session.id)
    return client, persistence_store, token_store


def test_connect_project_validates_token_and_stores_secret_reference(
    tmp_path: Path,
) -> None:
    raw_project_token = "raw-project-token"
    gitlab_project_client = FakeGitLabProjectClient()
    client, persistence_store, token_store = make_authenticated_client(
        tmp_path,
        gitlab_project_client,
    )

    response = client.post(
        "/projects/connect",
        data={
            "csrf_token": _csrf_token_from_cookie_session(client),
            "project_reference": (
                "https://gitlab.example.com/universalamateur1/checkout-service"
            ),
            "project_token": raw_project_token,
        },
        follow_redirects=False,
    )
    projects_response = client.get("/projects")

    assert response.status_code == 303
    assert response.headers["location"] == "/projects"
    assert gitlab_project_client.calls == [
        (
            "https://gitlab.example.com/universalamateur1/checkout-service",
            raw_project_token,
        )
    ]
    stored_projects = persistence_store.list_connected_projects_for_user(1001)
    assert len(stored_projects) == 1
    assert stored_projects[0].gitlab_project_id == 2002
    assert (
        stored_projects[0].gitlab_project_path == "universalamateur1/checkout-service"
    )
    assert stored_projects[0].token_ciphertext == "secret-ref:1"
    assert token_store.tokens == {"secret-ref:1": raw_project_token}
    assert raw_project_token not in projects_response.text
    assert "universalamateur1/checkout-service" in projects_response.text


def test_connect_project_rejects_invalid_token_without_storing_project(
    tmp_path: Path,
) -> None:
    raw_project_token = "invalid-project-token"
    gitlab_project_client = FakeGitLabProjectClient(
        error=ProjectConnectionError("Project token validation failed")
    )
    client, persistence_store, token_store = make_authenticated_client(
        tmp_path,
        gitlab_project_client,
    )

    response = client.post(
        "/projects/connect",
        data={
            "csrf_token": _csrf_token_from_cookie_session(client),
            "project_reference": "2002",
            "project_token": raw_project_token,
        },
    )

    assert response.status_code == 400
    assert "Project token validation failed" in response.text
    assert raw_project_token not in response.text
    assert persistence_store.list_connected_projects_for_user(1001) == []
    assert not token_store.tokens


def test_connect_project_rejects_project_outside_allowed_group(
    tmp_path: Path,
) -> None:
    raw_project_token = "outside-group-token"
    gitlab_project_client = FakeGitLabProjectClient(
        metadata=GitLabProjectMetadata(
            project_id=3003,
            path_with_namespace="other-group/checkout-service",
            display_name="checkout-service",
            namespace_id=777777,
        )
    )
    client, persistence_store, token_store = make_authenticated_client(
        tmp_path,
        gitlab_project_client,
    )

    response = client.post(
        "/projects/connect",
        data={
            "csrf_token": _csrf_token_from_cookie_session(client),
            "project_reference": "other-group/checkout-service",
            "project_token": raw_project_token,
        },
    )

    assert response.status_code == 400
    assert "outside the configured GitLab group" in response.text
    assert raw_project_token not in response.text
    assert persistence_store.list_connected_projects_for_user(1001) == []
    assert not token_store.tokens


def test_project_token_is_never_rendered_back_to_client(tmp_path: Path) -> None:
    raw_project_token = "token-that-must-not-render"
    gitlab_project_client = FakeGitLabProjectClient()
    client, _, _ = make_authenticated_client(tmp_path, gitlab_project_client)

    form_response = client.get("/projects/connect")
    post_response = client.post(
        "/projects/connect",
        data={
            "csrf_token": _csrf_token_from_cookie_session(client),
            "project_reference": "universalamateur1/checkout-service",
            "project_token": raw_project_token,
        },
    )
    list_response = client.get("/projects")

    assert raw_project_token not in form_response.text
    assert raw_project_token not in post_response.text
    assert raw_project_token not in list_response.text


def test_glab_project_client_reads_metadata_with_project_token() -> None:
    raw_project_token = "metadata-project-token"
    executor_mock = Mock(spec=GlabExecutor)
    executor_mock.api.return_value = {
        "id": 2002,
        "path_with_namespace": "universalamateur1/checkout-service",
        "name": "checkout-service",
        "namespace": {"id": 59032064},
    }
    project_client = GlabGitLabProjectClient(
        settings=make_auth_settings(),
        executor=cast(GlabExecutor, executor_mock),
    )

    metadata = project_client.get_project_metadata(
        project_reference="universalamateur1/checkout-service",
        project_token=raw_project_token,
    )

    assert metadata == GitLabProjectMetadata(
        project_id=2002,
        path_with_namespace="universalamateur1/checkout-service",
        display_name="checkout-service",
        namespace_id=59032064,
    )
    request = executor_mock.api.call_args.args[0]
    assert request.endpoint == "projects/universalamateur1%2Fcheckout-service"
    assert executor_mock.api.call_args.kwargs["token"] == raw_project_token


def test_glab_project_client_maps_executor_failure_to_connection_error() -> None:
    executor_mock = Mock(spec=GlabExecutor)
    executor_mock.api.side_effect = GlabExecutorError("401 unauthorized")
    project_client = GlabGitLabProjectClient(
        settings=make_auth_settings(),
        executor=cast(GlabExecutor, executor_mock),
    )

    with pytest.raises(ProjectConnectionError, match="token validation failed"):
        project_client.get_project_metadata(
            project_reference="universalamateur1/checkout-service",
            project_token="invalid-project-token",
        )


def test_webhook_secret_is_generated_once_and_hash_is_stored(
    tmp_path: Path,
) -> None:
    raw_webhook_secret = "raw-webhook-secret"
    client, persistence_store, _ = make_authenticated_client(
        tmp_path,
        FakeGitLabProjectClient(),
        webhook_secret_generator=lambda: raw_webhook_secret,
    )
    connected_project = _connect_project_for_test(client, persistence_store)

    first_response = client.post(
        f"/projects/{connected_project.id}/webhook-secret",
        data={"csrf_token": _csrf_token_from_cookie_session(client)},
    )
    stored_project = persistence_store.get_connected_project(connected_project.id)
    second_response = client.post(
        f"/projects/{connected_project.id}/webhook-secret",
        data={"csrf_token": _csrf_token_from_cookie_session(client)},
    )

    assert first_response.status_code == 200
    assert raw_webhook_secret in first_response.text
    assert stored_project is not None
    assert stored_project.webhook_secret_hash == _expected_secret_hash(
        raw_webhook_secret
    )
    assert raw_webhook_secret not in stored_project.model_dump_json()
    assert second_response.status_code == 200
    assert raw_webhook_secret not in second_response.text
    assert (
        persistence_store.get_connected_project(connected_project.id) == stored_project
    )


def test_webhook_setup_page_shows_instructions_without_raw_secret_after_generation(
    tmp_path: Path,
) -> None:
    raw_webhook_secret = "raw-webhook-secret"
    client, persistence_store, _ = make_authenticated_client(
        tmp_path,
        FakeGitLabProjectClient(),
        webhook_secret_generator=lambda: raw_webhook_secret,
    )
    connected_project = _connect_project_for_test(client, persistence_store)
    client.post(
        f"/projects/{connected_project.id}/webhook-secret",
        data={"csrf_token": _csrf_token_from_cookie_session(client)},
    )

    response = client.get(f"/projects/{connected_project.id}/webhook")

    assert response.status_code == 200
    assert "https://testserver/webhooks/gitlab/" in response.text
    assert "Pipeline events" in response.text
    assert "Job events disabled" in response.text
    assert "already been generated" in response.text
    assert raw_webhook_secret not in response.text


def test_project_pages_show_run_history_and_detail_without_tokens(
    tmp_path: Path,
) -> None:
    raw_project_token = "token-that-must-not-render"
    client, persistence_store, token_store = make_authenticated_client(
        tmp_path,
        FakeGitLabProjectClient(),
    )
    connected_project = _connect_project_for_test(
        client,
        persistence_store,
        project_token=raw_project_token,
    )
    token_store.tokens["secret-ref:1"] = raw_project_token
    triage_run = _create_run_detail_records(persistence_store, connected_project)

    projects_response = client.get("/projects")
    history_response = client.get(f"/projects/{connected_project.id}/runs")
    detail_response = client.get(
        f"/projects/{connected_project.id}/runs/{triage_run.id}"
    )

    assert projects_response.status_code == 200
    assert "Run history" in projects_response.text
    assert history_response.status_code == 200
    assert "Pipeline 9001" in history_response.text
    assert "status=monitoring" in history_response.text
    assert detail_response.status_code == 200
    assert "Run Detail" in detail_response.text
    assert "create_merge_request" in detail_response.text
    assert "Follow-up Monitors" in detail_response.text
    assert "codex-fix/pipeline-9001-abc123" in detail_response.text
    assert "x" * 260 not in detail_response.text
    assert "raw-project-token" not in detail_response.text
    assert raw_project_token not in projects_response.text
    assert raw_project_token not in history_response.text


def test_run_detail_rejects_run_outside_connected_project(tmp_path: Path) -> None:
    client, persistence_store, _ = make_authenticated_client(
        tmp_path,
        FakeGitLabProjectClient(),
    )
    connected_project = _connect_project_for_test(client, persistence_store)
    other_project = connected_project.model_copy(
        update={
            "id": "connected-project-other",
            "gitlab_project_id": 3003,
            "gitlab_project_path": "universalamateur1/other",
        }
    )
    persistence_store.create_connected_project(other_project)
    report_target = MergeRequestTarget(
        project_id=other_project.gitlab_project_id,
        merge_request_iid=1,
    )
    triage_run = _make_triage_run(
        other_project,
        report_target=report_target,
    )
    persistence_store.create_triage_run(triage_run)

    response = client.get(f"/projects/{connected_project.id}/runs/{triage_run.id}")

    assert response.status_code == 404


def test_webhook_secret_generation_rejects_invalid_csrf(
    tmp_path: Path,
) -> None:
    raw_webhook_secret = "raw-webhook-secret"
    client, persistence_store, _ = make_authenticated_client(
        tmp_path,
        FakeGitLabProjectClient(),
        webhook_secret_generator=lambda: raw_webhook_secret,
    )
    connected_project = _connect_project_for_test(client, persistence_store)

    response = client.post(
        f"/projects/{connected_project.id}/webhook-secret",
        data={"csrf_token": "wrong-token"},
    )

    assert response.status_code == 400
    assert raw_webhook_secret not in response.text
    stored_project = persistence_store.get_connected_project(connected_project.id)
    assert stored_project is not None
    assert stored_project.webhook_secret_hash == ""


def _csrf_token_from_cookie_session(client: TestClient) -> str:
    response = client.get("/")
    marker = 'name="csrf_token" value="'
    start = response.text.index(marker) + len(marker)
    end = response.text.index('"', start)
    return response.text[start:end]


def _connect_project_for_test(
    client: TestClient,
    persistence_store: SqliteStore,
    *,
    project_token: str = "raw-project-token",
) -> ConnectedProject:
    response = client.post(
        "/projects/connect",
        data={
            "csrf_token": _csrf_token_from_cookie_session(client),
            "project_reference": "universalamateur1/checkout-service",
            "project_token": project_token,
        },
        follow_redirects=False,
    )
    assert response.status_code == 303
    connected_projects = persistence_store.list_connected_projects_for_user(1001)
    assert len(connected_projects) == 1
    return connected_projects[0]


def _create_run_detail_records(
    persistence_store: SqliteStore,
    connected_project: ConnectedProject,
) -> TriageRun:
    report_target = MergeRequestTarget(
        project_id=connected_project.gitlab_project_id,
        merge_request_iid=17,
    )
    triage_result = TriageResult(
        root_cause_hypothesis="Checkout rounding failed.",
        category="code-bug",
        confidence=0.84,
        evidence=[
            EvidenceItem(
                source="mr_diff",
                file="checkout/tax.py",
                snippet="Discounts were rounded before tax.",
            )
        ],
        retry_safe=False,
        recommended_action="create_fix_mr",
        suggested_fix="Restore discount-before-rounding behavior.",
        needs_human_review=False,
    )
    triage_run = _make_triage_run(
        connected_project,
        report_target=report_target,
    ).model_copy(
        update={
            "status": "monitoring",
            "ref": "feature/" + ("x" * 400),
            "fallback_reason": None,
            "context_digest": "sha256:context",
            "triage_json": triage_result,
            "action_plan": ActionPlan(
                action="create_fix_mr",
                reason="Fix MR allowed by project policy.",
                requires_fixer_agent=True,
            ),
            "gitlab_note_ids": [331, 332],
            "fix_merge_request_iid": 2,
        }
    )
    persistence_store.create_triage_run(triage_run)
    persistence_store.create_action_log(
        GitLabActionLog(
            id="action-1",
            triage_run_id=triage_run.id,
            idempotency_key="create-fix-mr:2002:9001",
            action="create_merge_request",
            report_target=report_target,
            policy_decision="allowed",
            request_digest="sha256:request",
            external_id="2",
            status="completed",
            created_at=TEST_TIME,
            updated_at=TEST_TIME,
        )
    )
    persistence_store.create_pipeline_monitor(
        PipelineMonitor(
            id="monitor-1",
            triage_run_id=triage_run.id,
            gitlab_project_id=connected_project.gitlab_project_id,
            expected_ref="codex-fix/pipeline-9001-abc123",
            expected_sha="fixsha123",
            expected_pipeline_id=None,
            report_target=report_target,
            status="waiting",
            created_at=TEST_TIME,
            updated_at=TEST_TIME,
        )
    )
    return triage_run


def _make_triage_run(
    connected_project: ConnectedProject,
    *,
    report_target: MergeRequestTarget,
) -> TriageRun:
    return TriageRun(
        id="run-1",
        connected_project_id=connected_project.id,
        gitlab_project_id=connected_project.gitlab_project_id,
        pipeline_id=9001,
        job_ids=[4001],
        ref="feature/checkout-tax",
        sha="abc123",
        pipeline_kind="merge_request",
        report_target=report_target,
        status="ignored",
        adapter_mode="mock",
        fallback_reason="Spike 5.1 context only; triage not started.",
        input_digest="sha256:webhook",
        created_at=TEST_TIME,
        updated_at=TEST_TIME,
    )


def _expected_secret_hash(raw_secret: str) -> str:
    digest = hashlib.sha256(raw_secret.encode("utf-8")).hexdigest()
    return f"sha256:{digest}"


TEST_TIME = datetime(2026, 5, 4, 12, 0, tzinfo=timezone.utc)
