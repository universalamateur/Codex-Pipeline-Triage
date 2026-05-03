"""Tests for Spike 3.1 project connection."""

from __future__ import annotations

from pathlib import Path
from typing import cast
from unittest.mock import Mock

import pytest
from fastapi.testclient import TestClient

from codex_pipeline_triage.app import create_app
from codex_pipeline_triage.auth import GitLabIdentity, InMemorySessionStore
from codex_pipeline_triage.gitlab import GlabExecutor, GlabExecutorError
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


def make_authenticated_client(
    tmp_path: Path,
    gitlab_project_client: FakeGitLabProjectClient,
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


def _csrf_token_from_cookie_session(client: TestClient) -> str:
    response = client.get("/")
    marker = 'name="csrf_token" value="'
    start = response.text.index(marker) + len(marker)
    end = response.text.index('"', start)
    return response.text[start:end]
