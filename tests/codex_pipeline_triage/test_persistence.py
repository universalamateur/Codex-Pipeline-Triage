"""Tests for the Spike 1.2 persistence boundary."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import pytest

from codex_pipeline_triage.models import (
    ActionPlan,
    AppUser,
    ConnectedProject,
    EvidenceItem,
    GitLabActionLog,
    MergeRequestTarget,
    PipelineMonitor,
    ProjectActionPolicy,
    TriageResult,
    TriageRun,
)
from codex_pipeline_triage.persistence import RecordNotFoundError, SqliteStore


def fixed_time() -> datetime:
    return datetime(2026, 5, 3, 12, 0, tzinfo=timezone.utc)


def make_user(gitlab_user_id: int = 1001) -> AppUser:
    return AppUser(
        id=f"user-{gitlab_user_id}",
        gitlab_user_id=gitlab_user_id,
        gitlab_username=f"user-{gitlab_user_id}",
        display_name="Demo User",
        created_at=fixed_time(),
        updated_at=fixed_time(),
    )


def make_project(
    user: AppUser,
    *,
    record_id: str = "project-1",
    gitlab_project_id: int = 2002,
) -> ConnectedProject:
    return ConnectedProject(
        id=record_id,
        gitlab_project_id=gitlab_project_id,
        gitlab_project_path=f"demo/project-{gitlab_project_id}",
        display_name=f"project-{gitlab_project_id}",
        token_ciphertext="ref://test-project-token",
        webhook_secret_hash="sha256:test-webhook-placeholder",
        action_policy=ProjectActionPolicy(),
        connected_by_gitlab_user_id=user.gitlab_user_id,
        enabled=True,
        created_at=fixed_time(),
        updated_at=fixed_time(),
    )


def make_triage_run(project: ConnectedProject) -> TriageRun:
    triage_json = TriageResult(
        root_cause_hypothesis="Synthetic checkout test failed.",
        category="code-bug",
        confidence=0.72,
        evidence=[
            EvidenceItem(
                source="job_trace",
                file="tests/test_checkout.py",
                line=42,
                snippet="assert total == expected_total",
            )
        ],
        retry_safe=False,
        recommended_action="recommend_only",
        suggested_fix="Inspect the checkout total calculation.",
        needs_human_review=True,
    )
    return TriageRun(
        id="run-1",
        connected_project_id=project.id,
        gitlab_project_id=project.gitlab_project_id,
        pipeline_id=3003,
        job_ids=[4004, 4005],
        ref="feature/checkout-tax",
        sha="abc123",
        pipeline_kind="merge_request",
        report_target=MergeRequestTarget(
            project_id=project.gitlab_project_id,
            merge_request_iid=17,
        ),
        status="triaged",
        adapter_mode="mock",
        fallback_reason=None,
        input_digest="sha256:triage-input",
        triage_json=triage_json,
        action_plan=ActionPlan(
            action="recommend_only",
            reason="V1 is report-only.",
            requires_fixer_agent=False,
        ),
        gitlab_note_ids=[5005],
        created_at=fixed_time(),
        updated_at=fixed_time(),
    )


def make_action_log(run: TriageRun) -> GitLabActionLog:
    return GitLabActionLog(
        id="action-1",
        triage_run_id=run.id,
        idempotency_key="post-mr-note:run-1",
        action="post_mr_note",
        report_target=run.report_target,
        policy_decision="allowed",
        request_digest="sha256:request",
        external_id="5005",
        status="completed",
        created_at=fixed_time(),
        updated_at=fixed_time(),
    )


def make_monitor(run: TriageRun) -> PipelineMonitor:
    return PipelineMonitor(
        id="monitor-1",
        triage_run_id=run.id,
        gitlab_project_id=run.gitlab_project_id,
        expected_ref=run.ref,
        expected_sha=run.sha,
        expected_pipeline_id=run.pipeline_id,
        report_target=run.report_target,
        status="waiting",
        created_at=fixed_time(),
        updated_at=fixed_time(),
    )


def test_sqlite_store_persists_records_across_reopen(tmp_path: Path) -> None:
    db_path = tmp_path / "triage.sqlite"
    store = SqliteStore(db_path)
    user = make_user()
    project = make_project(user)
    run = make_triage_run(project)
    action_log = make_action_log(run)
    monitor = make_monitor(run)

    store.create_user(user)
    store.create_connected_project(project)
    store.create_triage_run(run)
    store.create_action_log(action_log)
    store.create_pipeline_monitor(monitor)

    reopened = SqliteStore(db_path)
    assert reopened.get_user(user.id) == user
    assert reopened.get_connected_project(project.id) == project
    assert reopened.get_triage_run(run.id) == run
    assert reopened.get_action_log(action_log.id) == action_log
    assert reopened.get_pipeline_monitor(monitor.id) == monitor


def test_sqlite_store_updates_records(tmp_path: Path) -> None:
    store = SqliteStore(tmp_path / "triage.sqlite")
    user = make_user()
    project = make_project(user)
    store.create_user(user)
    store.create_connected_project(project)

    updated_project = project.model_copy(
        update={
            "enabled": False,
            "updated_at": datetime(2026, 5, 3, 13, 0, tzinfo=timezone.utc),
        }
    )

    store.update_connected_project(updated_project)

    assert store.get_connected_project(project.id) == updated_project


def test_sqlite_store_scopes_list_queries(tmp_path: Path) -> None:
    store = SqliteStore(tmp_path / "triage.sqlite")
    first_user = make_user(1001)
    second_user = make_user(1002)
    first_project = make_project(
        first_user, record_id="project-1", gitlab_project_id=2001
    )
    second_project = make_project(
        second_user,
        record_id="project-2",
        gitlab_project_id=2002,
    )
    first_run = make_triage_run(first_project)
    second_run = make_triage_run(second_project).model_copy(update={"id": "run-2"})
    first_action = make_action_log(first_run)
    second_action = make_action_log(second_run).model_copy(update={"id": "action-2"})
    first_monitor = make_monitor(first_run)
    second_monitor = make_monitor(second_run).model_copy(update={"id": "monitor-2"})

    store.create_user(first_user)
    store.create_user(second_user)
    store.create_connected_project(first_project)
    store.create_connected_project(second_project)
    store.create_triage_run(first_run)
    store.create_triage_run(second_run)
    store.create_action_log(first_action)
    store.create_action_log(second_action)
    store.create_pipeline_monitor(first_monitor)
    store.create_pipeline_monitor(second_monitor)

    assert store.list_connected_projects_for_user(first_user.gitlab_user_id) == [
        first_project
    ]
    assert store.list_triage_runs_for_project(first_project.id) == [first_run]
    assert store.list_action_logs_for_run(first_run.id) == [first_action]
    assert store.list_pipeline_monitors_for_run(first_run.id) == [first_monitor]


def test_sqlite_store_isolates_database_files(tmp_path: Path) -> None:
    first_store = SqliteStore(tmp_path / "first.sqlite")
    second_store = SqliteStore(tmp_path / "second.sqlite")
    user = make_user()

    first_store.create_user(user)

    assert first_store.get_user(user.id) == user
    assert second_store.get_user(user.id) is None


def test_sqlite_store_fails_closed_when_updating_missing_record(
    tmp_path: Path,
) -> None:
    store = SqliteStore(tmp_path / "triage.sqlite")
    user = make_user()

    with pytest.raises(RecordNotFoundError, match="record not found"):
        store.update_user(user)
