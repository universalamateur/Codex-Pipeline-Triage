"""Tests for the FastAPI application skeleton."""

# pylint: disable=duplicate-code

from pathlib import Path

from fastapi.testclient import TestClient

from codex_pipeline_triage.app import create_app
from codex_pipeline_triage.auth import (
    GitLabIdentity,
    InMemorySessionStore,
    SessionRecord,
)
from codex_pipeline_triage.models import (
    ConnectedProject,
    ProjectActionPolicy,
    TriageRun,
)
from codex_pipeline_triage.persistence import SqliteStore
from codex_pipeline_triage.projects import ProjectConnector
from codex_pipeline_triage.reporting import MockMrReporter
from tests.codex_pipeline_triage.helpers import make_auth_settings
from tests.codex_pipeline_triage.test_projects import (
    FakeGitLabProjectClient,
    RecordingProjectTokenStore,
)
from tests.codex_pipeline_triage.test_reporting import (
    RecordingFixer,
    RecordingFixMrClient,
    RecordingMrNoteClient,
    fix_mr_triage_result,
    make_context_ready_mr_run,
)


def test_health_endpoint_returns_ok() -> None:
    with TestClient(create_app()) as client:
        response = client.get("/health")

    assert response.status_code == 200
    assert response.json() == {
        "service": "codex-pipeline-triage",
        "status": "ok",
    }


def test_run_detail_create_fix_mr_button_executes_controlled_action(
    tmp_path: Path,
) -> None:
    client, session, store, project, run, fix_mr_client = make_manual_fix_mr_app(
        tmp_path
    )

    detail_response = client.get(f"/projects/{project.id}/runs/{run.id}")

    assert detail_response.status_code == 200
    assert "Create bot fix MR" in detail_response.text
    assert "Fix MR IID" in detail_response.text
    assert "Fix commit SHA" in detail_response.text

    action_response = client.post(
        f"/projects/{project.id}/runs/{run.id}/create-fix-mr",
        data={"csrf_token": session.csrf_token},
        follow_redirects=False,
    )

    assert action_response.status_code == 303
    updated_run = store.get_triage_run(run.id)
    assert updated_run is not None
    assert updated_run.status == "monitoring"
    assert updated_run.fix_merge_request_iid == 44
    assert updated_run.action_plan is not None
    assert updated_run.action_plan.action == "create_fix_mr"
    assert len(fix_mr_client.commit_calls) == 1
    assert len(fix_mr_client.mr_calls) == 1
    assert fix_mr_client.commit_calls[0]["source_branch"] != run.ref
    assert store.list_pipeline_monitors_for_run(run.id)

    after_response = client.get(f"/projects/{project.id}/runs/{run.id}")

    assert after_response.status_code == 200
    assert "Create bot fix MR" not in after_response.text
    assert "44" in after_response.text
    assert "abc123commit" in after_response.text
    assert "codex-fix/pipeline-1001-abc123" in after_response.text


def test_run_detail_create_fix_mr_post_fails_closed_when_policy_blocks(
    tmp_path: Path,
) -> None:
    client, session, store, project, run, fix_mr_client = make_manual_fix_mr_app(
        tmp_path,
        auto_create_fix_mr=False,
    )

    detail_response = client.get(f"/projects/{project.id}/runs/{run.id}")
    action_response = client.post(
        f"/projects/{project.id}/runs/{run.id}/create-fix-mr",
        data={"csrf_token": session.csrf_token},
    )

    assert detail_response.status_code == 200
    assert "Create bot fix MR" not in detail_response.text
    assert "auto_create_fix_mr disabled" in detail_response.text
    assert action_response.status_code == 400
    assert "auto_create_fix_mr disabled" in action_response.text
    assert store.get_triage_run(run.id) == run
    assert not fix_mr_client.commit_calls
    assert not fix_mr_client.mr_calls


def make_manual_fix_mr_app(
    tmp_path: Path,
    *,
    auto_create_fix_mr: bool = True,
) -> tuple[
    TestClient,
    SessionRecord,
    SqliteStore,
    ConnectedProject,
    TriageRun,
    RecordingFixMrClient,
]:
    settings = make_auth_settings()
    store, project, run = make_context_ready_mr_run(tmp_path)
    project = project.model_copy(
        update={
            "action_policy": ProjectActionPolicy(
                auto_create_fix_mr=auto_create_fix_mr,
            )
        }
    )
    store.update_connected_project(project)
    run = store.update_triage_run(
        run.model_copy(
            update={
                "status": "posted",
                "triage_json": fix_mr_triage_result().model_copy(
                    update={"recommended_action": "recommend_only"}
                ),
                "fallback_reason": None,
                "gitlab_note_ids": [9001],
            }
        )
    )
    token_store = RecordingProjectTokenStore()
    token_store.tokens["secret-ref:1"] = "project-token"
    fix_mr_client = RecordingFixMrClient()
    session_store = InMemorySessionStore()
    session = session_store.create_session(
        GitLabIdentity(
            gitlab_user_id=project.connected_by_gitlab_user_id,
            gitlab_username="allowed-user",
        )
    )
    project_connector = ProjectConnector(
        settings=settings,
        gitlab_project_client=FakeGitLabProjectClient(),
        token_store=token_store,
        persistence_store=store,
    )
    reporter = MockMrReporter(
        mr_note_client=RecordingMrNoteClient(note_ids=[9002]),
        token_store=token_store,
        persistence_store=store,
        fixer=RecordingFixer(),
        fix_mr_client=fix_mr_client,
    )
    client = TestClient(
        create_app(
            auth_settings=settings,
            session_store=session_store,
            project_connector=project_connector,
            mock_mr_reporter=reporter,
        ),
        base_url="https://testserver",
    )
    client.cookies.set(settings.session_cookie_name, session.id)
    return client, session, store, project, run, fix_mr_client
