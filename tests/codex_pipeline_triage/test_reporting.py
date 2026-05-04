"""Tests for Spike 5.2 mock triage and MR-note reporting."""

# pylint: disable=duplicate-code

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from pathlib import Path
from typing import cast
from unittest.mock import Mock

import pytest

from codex_pipeline_triage.codex_adapter import CodexTriageOutcome
from codex_pipeline_triage.context import PipelineContextBuilder
from codex_pipeline_triage.gitlab import GlabApiRequest
from codex_pipeline_triage.models import (
    ActionPlan,
    ConnectedProject,
    DiffFileContext,
    EvidenceItem,
    IssueTarget,
    JobTraceContext,
    MergeRequestTarget,
    PipelineContext,
    ProjectActionPolicy,
    TriageResult,
    TriageRun,
)
from codex_pipeline_triage.persistence import SqliteStore
from codex_pipeline_triage.projects import InMemoryProjectTokenSecretStore
from codex_pipeline_triage.reporting import (
    MISSING_CODEX_KEY_FALLBACK_REASON,
    MOCK_MARKER,
    PIPELINE_TRIAGE_MODE_CODEX,
    GitLabIssue,
    GlabGitLabIssueClient,
    GlabGitLabMrNoteClient,
    MockMrReporter,
    MockReportingError,
    TriageModeSettings,
    build_branch_issue_title,
    build_mock_triage,
    render_mr_note,
)
from tests.codex_pipeline_triage.test_context import (
    FakeGitLabContextClient,
    make_connected_project,
    make_triage_run,
)
from tests.codex_pipeline_triage.test_projects import RecordingProjectTokenStore


def test_mock_triage_includes_required_fields(tmp_path: Path) -> None:
    _, _, run = make_context_ready_mr_run(tmp_path)
    assert run.context_json is not None

    triage_result = build_mock_triage(run.context_json)

    assert MOCK_MARKER in triage_result.root_cause_hypothesis
    assert triage_result.evidence
    assert triage_result.confidence == 0.42
    assert triage_result.recommended_action == "recommend_only"
    assert triage_result.suggested_fix
    assert triage_result.needs_human_review is True


def test_mock_triage_bounds_long_job_name_and_empty_evidence(
    tmp_path: Path,
) -> None:
    _, _, run = make_context_ready_mr_run(tmp_path)
    assert run.context_json is not None
    context = run.context_json.model_copy(
        update={
            "failed_job_traces": [
                JobTraceContext(
                    job_id=4001,
                    job_name="job-" + ("x" * 1000),
                    trace_excerpt="",
                    trace_digest="sha256:empty-trace",
                    truncated=False,
                )
            ],
            "diffs": [
                DiffFileContext(
                    old_path="before.py",
                    new_path="after.py",
                    diff_excerpt="",
                    diff_digest="sha256:empty-diff",
                    truncated=False,
                )
            ],
        }
    )

    triage_result = build_mock_triage(context)

    assert len(triage_result.root_cause_hypothesis) <= 280
    assert triage_result.evidence[0].snippet == (
        "Trace excerpt was empty after bounding."
    )
    assert triage_result.evidence[1].snippet == "Diff excerpt was empty after bounding."


def test_mr_note_body_renders_bounded_redacted_mock_context(tmp_path: Path) -> None:
    _, _, run = make_context_ready_mr_run(tmp_path)
    assert run.context_json is not None
    triage_result = build_mock_triage(run.context_json)
    unsafe_result = triage_result.model_copy(
        update={
            "root_cause_hypothesis": (
                f"[{MOCK_MARKER}] password = hypothesis-password " + ("x" * 1000)
            ),
            "suggested_fix": "PRIVATE-TOKEN: fix-token\nReview the mock evidence.",
        }
    )
    action_plan = ActionPlan(
        action="recommend_only",
        reason="Spike 5.2 mock triage is report-only.",
        requires_fixer_agent=False,
    )

    note_body = render_mr_note(
        triage_run=run.model_copy(update={"fallback_reason": "secret: fallback-value"}),
        triage_result=unsafe_result,
        action_plan=action_plan,
    )

    assert "Codex Pipeline Triage (MOCK)" in note_body
    assert "deterministic mock triage" in note_body
    assert "hypothesis-password" not in note_body
    assert "fix-token" not in note_body
    assert "fallback-value" not in note_body
    assert "[REDACTED]" in note_body
    assert "Recommended action" in note_body
    assert "Confidence" in note_body


def test_glab_mr_note_client_uses_expected_api_payload() -> None:
    executor = Mock()
    executor.api.return_value = {"id": 5005}
    client = GlabGitLabMrNoteClient(executor=executor)

    note_id = client.post_merge_request_note(
        project_id=2002,
        merge_request_iid=17,
        body="body",
        project_token="project-token",
    )

    assert note_id == 5005
    request = executor.api.call_args.args[0]
    assert request == GlabApiRequest(
        endpoint="projects/2002/merge_requests/17/notes",
        method="POST",
        fields={"body": "body"},
    )
    assert executor.api.call_args.kwargs["token"] == "project-token"


def test_glab_issue_client_uses_expected_api_payloads() -> None:
    executor = Mock()
    executor.api.side_effect = [
        [{"iid": 7001, "title": "Pipeline failed on main at abc123"}],
        {"iid": 7002},
        {"id": 9101},
    ]
    client = GlabGitLabIssueClient(executor=executor)

    existing_issue = client.find_open_issue(
        project_id=2002,
        title="Pipeline failed on main at abc123",
        project_token="project-token",
    )
    created_iid = client.create_issue(
        project_id=2002,
        title="Pipeline failed on main at abc123",
        description="description",
        project_token="project-token",
    )
    note_id = client.post_issue_note(
        project_id=2002,
        issue_iid=7002,
        body="body",
        project_token="project-token",
    )

    assert existing_issue == GitLabIssue(
        iid=7001,
        title="Pipeline failed on main at abc123",
    )
    assert created_iid == 7002
    assert note_id == 9101
    requests = [call.args[0] for call in executor.api.call_args_list]
    assert requests[0].endpoint.startswith("projects/2002/issues?")
    assert "state=opened" in requests[0].endpoint
    assert "search=Pipeline+failed+on+main+at+abc123" in requests[0].endpoint
    assert "in=title" in requests[0].endpoint
    assert requests[1] == GlabApiRequest(
        endpoint="projects/2002/issues",
        method="POST",
        fields={
            "title": "Pipeline failed on main at abc123",
            "description": "description",
        },
    )
    assert requests[2] == GlabApiRequest(
        endpoint="projects/2002/issues/7002/notes",
        method="POST",
        fields={"body": "body"},
    )
    assert all(
        call.kwargs["token"] == "project-token" for call in executor.api.call_args_list
    )


def test_mock_reporter_persists_action_before_posting_mr_note(
    tmp_path: Path,
) -> None:
    store, project, run = make_context_ready_mr_run(tmp_path)
    token_store = RecordingProjectTokenStore()
    token_store.tokens["secret-ref:1"] = "project-token"
    note_client = RecordingMrNoteClient(
        store=store,
        triage_run_id=run.id,
    )

    reported_run = MockMrReporter(
        mr_note_client=note_client,
        token_store=token_store,
        persistence_store=store,
    ).report_for_run(connected_project=project, triage_run=run)

    assert reported_run.status == "posted"
    assert reported_run.adapter_mode == "mock"
    assert reported_run.triage_json is not None
    assert MOCK_MARKER in reported_run.triage_json.root_cause_hypothesis
    assert reported_run.action_plan is not None
    assert reported_run.action_plan.action == "recommend_only"
    assert reported_run.gitlab_note_ids == [9001]
    assert len(note_client.calls) == 1
    note_call = note_client.calls[0]
    assert note_call["project_id"] == project.gitlab_project_id
    assert note_call["merge_request_iid"] == 17
    assert note_call["project_token"] == "project-token"
    note_body = cast(str, note_call["body"])
    assert MOCK_MARKER in note_body
    assert "project-token" not in note_body
    action_logs = store.list_action_logs_for_run(run.id)
    assert len(action_logs) == 1
    assert action_logs[0].action == "post_mr_note"
    assert action_logs[0].status == "completed"
    assert action_logs[0].external_id == "9001"
    assert action_logs[0].request_digest == sha256(note_body)


def test_mock_reporter_marks_run_failed_when_token_lookup_fails(
    tmp_path: Path,
) -> None:
    store, project, run = make_context_ready_mr_run(tmp_path)
    note_client = RecordingMrNoteClient()

    failed_run = MockMrReporter(
        mr_note_client=note_client,
        token_store=InMemoryProjectTokenSecretStore(),
        persistence_store=store,
    ).report_for_run(connected_project=project, triage_run=run)

    assert failed_run.status == "failed"
    assert failed_run.fallback_reason == "MR note post failed."
    assert failed_run.gitlab_note_ids == []
    assert not note_client.calls
    action_logs = store.list_action_logs_for_run(run.id)
    assert len(action_logs) == 1
    assert action_logs[0].action == "post_mr_note"
    assert action_logs[0].status == "failed"
    assert action_logs[0].external_id is None
    assert store.get_triage_run(run.id) == failed_run


def test_mock_reporter_marks_run_failed_when_mr_note_post_fails(
    tmp_path: Path,
) -> None:
    store, project, run = make_context_ready_mr_run(tmp_path)
    token_store = RecordingProjectTokenStore()
    token_store.tokens["secret-ref:1"] = "project-token"
    note_client = FailingMrNoteClient()

    failed_run = MockMrReporter(
        mr_note_client=note_client,
        token_store=token_store,
        persistence_store=store,
    ).report_for_run(connected_project=project, triage_run=run)

    assert failed_run.status == "failed"
    assert failed_run.fallback_reason == "MR note post failed."
    assert failed_run.gitlab_note_ids == []
    assert len(note_client.calls) == 1
    action_logs = store.list_action_logs_for_run(run.id)
    assert len(action_logs) == 1
    assert action_logs[0].action == "post_mr_note"
    assert action_logs[0].status == "failed"
    assert action_logs[0].external_id is None


def test_mock_reporter_does_not_duplicate_mr_notes(tmp_path: Path) -> None:
    store, project, run = make_context_ready_mr_run(tmp_path)
    token_store = RecordingProjectTokenStore()
    token_store.tokens["secret-ref:1"] = "project-token"
    note_client = RecordingMrNoteClient()
    reporter = MockMrReporter(
        mr_note_client=note_client,
        token_store=token_store,
        persistence_store=store,
    )

    first_run = reporter.report_for_run(connected_project=project, triage_run=run)
    second_run = reporter.report_for_run(
        connected_project=project,
        triage_run=first_run,
    )

    assert first_run.gitlab_note_ids == [9001]
    assert second_run.gitlab_note_ids == [9001]
    assert len(note_client.calls) == 1
    assert len(store.list_action_logs_for_run(run.id)) == 1


def test_real_mode_reporter_uses_codex_adapter_and_posts_mr_note(
    tmp_path: Path,
) -> None:
    store, project, run = make_context_ready_mr_run(tmp_path)
    token_store = RecordingProjectTokenStore()
    token_store.tokens["secret-ref:1"] = "project-token"
    note_client = RecordingMrNoteClient(
        store=store,
        triage_run_id=run.id,
    )
    codex_adapter = RecordingCodexAdapter(
        outcome=CodexTriageOutcome(
            adapter_mode="codex",
            fallback_reason=None,
            triage_result=unsafe_real_triage_result(),
        )
    )

    reported_run = MockMrReporter(
        mr_note_client=note_client,
        token_store=token_store,
        persistence_store=store,
        triage_mode=PIPELINE_TRIAGE_MODE_CODEX,
        codex_adapter=codex_adapter,
    ).report_for_run(connected_project=project, triage_run=run)

    assert codex_adapter.contexts == [run.context_json]
    assert reported_run.status == "posted"
    assert reported_run.adapter_mode == "codex"
    assert reported_run.fallback_reason is None
    assert reported_run.triage_json is not None
    persisted_run = reported_run.model_dump_json()
    assert "hypothesis-token" not in persisted_run
    assert "evidence-token" not in persisted_run
    assert "fix-key" not in persisted_run
    assert "[REDACTED]" in persisted_run
    assert reported_run.action_plan is not None
    assert (
        reported_run.action_plan.reason == "Spike 6.2 real Codex triage is report-only."
    )
    assert reported_run.gitlab_note_ids == [9001]
    assert len(note_client.calls) == 1
    note_body = cast(str, note_client.calls[0]["body"])
    assert "Codex Pipeline Triage (CODEX)" in note_body
    assert "real Codex SDK triage ran server-side" in note_body
    assert "deterministic mock triage" not in note_body
    assert "project-token" not in note_body
    assert "hypothesis-token" not in note_body
    assert "evidence-token" not in note_body
    assert "fix-key" not in note_body


def test_real_mode_reporter_uses_codex_for_branch_issue_note(
    tmp_path: Path,
) -> None:
    store, project, run = make_context_ready_branch_run(tmp_path)
    token_store = RecordingProjectTokenStore()
    token_store.tokens["secret-ref:1"] = "project-token"
    issue_client = RecordingIssueClient()
    codex_adapter = RecordingCodexAdapter(
        outcome=CodexTriageOutcome(
            adapter_mode="codex",
            fallback_reason=None,
            triage_result=unsafe_real_triage_result(),
        )
    )

    reported_run = MockMrReporter(
        mr_note_client=RecordingMrNoteClient(),
        issue_client=issue_client,
        token_store=token_store,
        persistence_store=store,
        triage_mode=PIPELINE_TRIAGE_MODE_CODEX,
        codex_adapter=codex_adapter,
    ).report_for_run(connected_project=project, triage_run=run)

    assert codex_adapter.contexts == [run.context_json]
    assert reported_run.status == "posted"
    assert reported_run.adapter_mode == "codex"
    assert reported_run.action_plan is not None
    assert reported_run.action_plan.action == "create_issue"
    assert reported_run.action_plan.reason == (
        "Spike 6.2 real Codex branch issue reporting is report-only."
    )
    assert issue_client.create_calls[0]["project_token"] == "project-token"
    assert issue_client.note_calls[0]["project_token"] == "project-token"
    note_body = cast(str, issue_client.note_calls[0]["body"])
    assert "Codex Pipeline Triage (CODEX)" in note_body
    assert "real Codex SDK triage ran server-side" in note_body
    assert "project-token" not in note_body
    assert "hypothesis-token" not in note_body


def test_real_mode_missing_key_falls_back_visibly(tmp_path: Path) -> None:
    store, project, run = make_context_ready_mr_run(tmp_path)
    token_store = RecordingProjectTokenStore()
    token_store.tokens["secret-ref:1"] = "project-token"
    note_client = RecordingMrNoteClient()

    reported_run = MockMrReporter(
        mr_note_client=note_client,
        token_store=token_store,
        persistence_store=store,
        triage_mode=PIPELINE_TRIAGE_MODE_CODEX,
        codex_unavailable_reason=MISSING_CODEX_KEY_FALLBACK_REASON,
    ).report_for_run(connected_project=project, triage_run=run)

    assert reported_run.status == "posted"
    assert reported_run.adapter_mode == "mock"
    assert reported_run.fallback_reason == MISSING_CODEX_KEY_FALLBACK_REASON
    assert reported_run.triage_json is not None
    assert MOCK_MARKER in reported_run.triage_json.root_cause_hypothesis
    assert len(note_client.calls) == 1
    note_body = cast(str, note_client.calls[0]["body"])
    assert "Codex Pipeline Triage (MOCK)" in note_body
    assert "real Codex triage did not produce accepted output" in note_body
    assert MISSING_CODEX_KEY_FALLBACK_REASON in note_body
    assert "project-token" not in note_body


def test_triage_mode_settings_loads_documented_codex_env(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("PIPELINE_TRIAGE_MODE", "codex")
    monkeypatch.setenv("PIPELINE_TRIAGE_CODEX_MODEL", "gpt-test")
    monkeypatch.setenv("PIPELINE_TRIAGE_CODEX_TIMEOUT_SECONDS", "12.5")
    monkeypatch.setenv("PIPELINE_TRIAGE_CODEX_BIN", "/opt/homebrew/bin/codex")
    monkeypatch.setenv("OPENAI_API_KEY", "fake-openai-key")

    settings = TriageModeSettings.from_env()

    assert settings.triage_mode == "codex"
    assert settings.codex_model == "gpt-test"
    assert settings.codex_timeout_seconds == 12.5
    assert settings.codex_bin == Path("/opt/homebrew/bin/codex")
    assert settings.has_codex_key


def test_mock_reporter_creates_issue_and_posts_issue_note(tmp_path: Path) -> None:
    store, project, run = make_context_ready_branch_run(tmp_path)
    token_store = RecordingProjectTokenStore()
    token_store.tokens["secret-ref:1"] = "project-token"
    note_client = RecordingMrNoteClient()
    issue_client = RecordingIssueClient()

    reported_run = MockMrReporter(
        mr_note_client=note_client,
        issue_client=issue_client,
        token_store=token_store,
        persistence_store=store,
    ).report_for_run(connected_project=project, triage_run=run)

    assert reported_run.status == "posted"
    assert isinstance(reported_run.report_target, IssueTarget)
    assert reported_run.report_target.issue_iid == 7001
    assert reported_run.issue_iid == 7001
    assert reported_run.action_plan is not None
    assert reported_run.action_plan.action == "create_issue"
    assert reported_run.gitlab_note_ids == [9101]
    assert not note_client.calls
    assert issue_client.find_calls == [
        {
            "project_id": project.gitlab_project_id,
            "title": build_branch_issue_title(run),
            "project_token": "project-token",
        }
    ]
    assert len(issue_client.create_calls) == 1
    assert issue_client.create_calls[0]["title"] == build_branch_issue_title(run)
    assert len(issue_client.note_calls) == 1
    assert issue_client.note_calls[0]["issue_iid"] == 7001
    note_body = cast(str, issue_client.note_calls[0]["body"])
    assert MOCK_MARKER in note_body
    assert "project-token" not in note_body
    action_logs = store.list_action_logs_for_run(run.id)
    action_logs_by_action = {
        action_log.action: action_log for action_log in action_logs
    }
    assert set(action_logs_by_action) == {"create_issue", "post_issue_note"}
    assert action_logs_by_action["create_issue"].status == "completed"
    assert action_logs_by_action["create_issue"].external_id == "7001"
    assert action_logs_by_action["post_issue_note"].status == "completed"
    assert action_logs_by_action["post_issue_note"].external_id == "9101"


def test_mock_reporter_reuses_existing_issue(tmp_path: Path) -> None:
    store, project, run = make_context_ready_branch_run(tmp_path)
    token_store = RecordingProjectTokenStore()
    token_store.tokens["secret-ref:1"] = "project-token"
    issue_client = RecordingIssueClient(existing_issue_iid=7007)

    reported_run = MockMrReporter(
        mr_note_client=RecordingMrNoteClient(),
        issue_client=issue_client,
        token_store=token_store,
        persistence_store=store,
    ).report_for_run(connected_project=project, triage_run=run)

    assert reported_run.status == "posted"
    assert reported_run.issue_iid == 7007
    assert isinstance(reported_run.report_target, IssueTarget)
    assert reported_run.report_target.issue_iid == 7007
    assert not issue_client.create_calls
    assert len(issue_client.note_calls) == 1
    assert issue_client.note_calls[0]["issue_iid"] == 7007
    action_logs = store.list_action_logs_for_run(run.id)
    assert [action_log.action for action_log in action_logs] == ["post_issue_note"]
    assert action_logs[0].status == "completed"


def test_mock_reporter_does_not_duplicate_issue_notes(tmp_path: Path) -> None:
    store, project, run = make_context_ready_branch_run(tmp_path)
    token_store = RecordingProjectTokenStore()
    token_store.tokens["secret-ref:1"] = "project-token"
    issue_client = RecordingIssueClient()
    reporter = MockMrReporter(
        mr_note_client=RecordingMrNoteClient(),
        issue_client=issue_client,
        token_store=token_store,
        persistence_store=store,
    )

    first_run = reporter.report_for_run(connected_project=project, triage_run=run)
    second_run = reporter.report_for_run(
        connected_project=project,
        triage_run=first_run,
    )

    assert first_run.gitlab_note_ids == [9101]
    assert second_run.gitlab_note_ids == [9101]
    assert len(issue_client.create_calls) == 1
    assert len(issue_client.note_calls) == 1
    assert len(store.list_action_logs_for_run(run.id)) == 2


def test_mock_reporter_blocks_branch_issue_when_project_policy_disallows(
    tmp_path: Path,
) -> None:
    store, project, run = make_context_ready_branch_run(tmp_path)
    blocked_project = project.model_copy(
        update={
            "action_policy": ProjectActionPolicy(
                recommend_only=True,
                auto_create_issue=False,
            )
        }
    )
    token_store = RecordingProjectTokenStore()
    token_store.tokens["secret-ref:1"] = "project-token"
    issue_client = RecordingIssueClient()

    reported_run = MockMrReporter(
        mr_note_client=RecordingMrNoteClient(),
        issue_client=issue_client,
        token_store=token_store,
        persistence_store=store,
    ).report_for_run(connected_project=blocked_project, triage_run=run)

    assert reported_run.status == "triaged"
    assert reported_run.issue_iid is None
    assert reported_run.gitlab_note_ids == []
    assert reported_run.action_plan is not None
    assert reported_run.action_plan.action == "recommend_only"
    assert reported_run.fallback_reason == (
        "Branch issue reporting blocked by project policy."
    )
    assert not issue_client.find_calls
    assert not issue_client.create_calls
    assert not issue_client.note_calls
    action_logs = store.list_action_logs_for_run(run.id)
    assert len(action_logs) == 1
    assert action_logs[0].action == "create_issue"
    assert action_logs[0].policy_decision == "blocked"
    assert action_logs[0].status == "skipped"


def test_mock_reporter_marks_run_failed_when_issue_lookup_fails(
    tmp_path: Path,
) -> None:
    store, project, run = make_context_ready_branch_run(tmp_path)
    token_store = RecordingProjectTokenStore()
    token_store.tokens["secret-ref:1"] = "project-token"
    issue_client = RecordingIssueClient(find_error="Issue lookup failed.")

    failed_run = MockMrReporter(
        mr_note_client=RecordingMrNoteClient(),
        issue_client=issue_client,
        token_store=token_store,
        persistence_store=store,
    ).report_for_run(connected_project=project, triage_run=run)

    assert failed_run.status == "failed"
    assert failed_run.fallback_reason == "Issue lookup failed."
    assert len(issue_client.find_calls) == 1
    assert not issue_client.create_calls
    assert not issue_client.note_calls
    assert store.list_action_logs_for_run(run.id) == []


def test_mock_reporter_marks_run_failed_when_issue_creation_fails(
    tmp_path: Path,
) -> None:
    store, project, run = make_context_ready_branch_run(tmp_path)
    token_store = RecordingProjectTokenStore()
    token_store.tokens["secret-ref:1"] = "project-token"
    issue_client = RecordingIssueClient(create_error="Issue creation failed.")

    failed_run = MockMrReporter(
        mr_note_client=RecordingMrNoteClient(),
        issue_client=issue_client,
        token_store=token_store,
        persistence_store=store,
    ).report_for_run(connected_project=project, triage_run=run)

    assert failed_run.status == "failed"
    assert failed_run.fallback_reason == "Issue creation failed."
    assert len(issue_client.find_calls) == 1
    assert len(issue_client.create_calls) == 1
    assert not issue_client.note_calls
    action_logs = store.list_action_logs_for_run(run.id)
    assert len(action_logs) == 1
    assert action_logs[0].action == "create_issue"
    assert action_logs[0].status == "failed"
    assert action_logs[0].fallback_reason == "Issue creation failed."


def test_mock_reporter_marks_run_failed_when_issue_note_post_fails(
    tmp_path: Path,
) -> None:
    store, project, run = make_context_ready_branch_run(tmp_path)
    token_store = RecordingProjectTokenStore()
    token_store.tokens["secret-ref:1"] = "project-token"
    issue_client = RecordingIssueClient(note_error="Issue note post failed.")

    failed_run = MockMrReporter(
        mr_note_client=RecordingMrNoteClient(),
        issue_client=issue_client,
        token_store=token_store,
        persistence_store=store,
    ).report_for_run(connected_project=project, triage_run=run)

    assert failed_run.status == "failed"
    assert failed_run.issue_iid == 7001
    assert failed_run.fallback_reason == "Issue note post failed."
    assert len(issue_client.find_calls) == 1
    assert len(issue_client.create_calls) == 1
    assert len(issue_client.note_calls) == 1
    action_logs_by_action = {
        action_log.action: action_log
        for action_log in store.list_action_logs_for_run(run.id)
    }
    assert action_logs_by_action["create_issue"].status == "completed"
    assert action_logs_by_action["post_issue_note"].status == "failed"


@dataclass
class RecordingCodexAdapter:
    """Fake Codex adapter that records contexts passed to real mode."""

    outcome: CodexTriageOutcome
    contexts: list[PipelineContext | None] = field(default_factory=list)

    async def triage(self, context: PipelineContext) -> CodexTriageOutcome:
        self.contexts.append(context)
        return self.outcome


@dataclass
class RecordingMrNoteClient:
    """Fake MR note client that records calls and can inspect persistence."""

    note_id: int = 9001
    store: SqliteStore | None = None
    triage_run_id: str | None = None
    calls: list[dict[str, object]] = field(default_factory=list)

    def post_merge_request_note(
        self,
        *,
        project_id: int,
        merge_request_iid: int,
        body: str,
        project_token: str,
    ) -> int:
        if self.store is not None and self.triage_run_id is not None:
            action_logs = self.store.list_action_logs_for_run(self.triage_run_id)
            assert len(action_logs) == 1
            assert action_logs[0].status == "planned"
            planned_run = self.store.get_triage_run(self.triage_run_id)
            assert planned_run is not None
            assert planned_run.status == "triaged"
        self.calls.append(
            {
                "project_id": project_id,
                "merge_request_iid": merge_request_iid,
                "body": body,
                "project_token": project_token,
            }
        )
        return self.note_id


@dataclass
class FailingMrNoteClient:
    """Fake MR note client that simulates a deterministic GitLab post failure."""

    calls: list[dict[str, object]] = field(default_factory=list)

    def post_merge_request_note(
        self,
        *,
        project_id: int,
        merge_request_iid: int,
        body: str,
        project_token: str,
    ) -> int:
        self.calls.append(
            {
                "project_id": project_id,
                "merge_request_iid": merge_request_iid,
                "body": body,
                "project_token": project_token,
            }
        )
        raise MockReportingError("simulated MR-note post failure")


@dataclass
class RecordingIssueClient:  # pylint: disable=too-many-instance-attributes
    """Fake issue client that records branch issue reporting calls."""

    existing_issue_iid: int | None = None
    issue_iid: int = 7001
    note_id: int = 9101
    find_error: str | None = None
    create_error: str | None = None
    note_error: str | None = None
    find_calls: list[dict[str, object]] = field(default_factory=list)
    create_calls: list[dict[str, object]] = field(default_factory=list)
    note_calls: list[dict[str, object]] = field(default_factory=list)

    def find_open_issue(
        self,
        *,
        project_id: int,
        title: str,
        project_token: str,
    ) -> GitLabIssue | None:
        self.find_calls.append(
            {
                "project_id": project_id,
                "title": title,
                "project_token": project_token,
            }
        )
        if self.find_error is not None:
            raise MockReportingError(self.find_error)
        if self.existing_issue_iid is None:
            return None
        return GitLabIssue(iid=self.existing_issue_iid, title=title)

    def create_issue(
        self,
        *,
        project_id: int,
        title: str,
        description: str,
        project_token: str,
    ) -> int:
        self.create_calls.append(
            {
                "project_id": project_id,
                "title": title,
                "description": description,
                "project_token": project_token,
            }
        )
        if self.create_error is not None:
            raise MockReportingError(self.create_error)
        return self.issue_iid

    def post_issue_note(
        self,
        *,
        project_id: int,
        issue_iid: int,
        body: str,
        project_token: str,
    ) -> int:
        self.note_calls.append(
            {
                "project_id": project_id,
                "issue_iid": issue_iid,
                "body": body,
                "project_token": project_token,
            }
        )
        if self.note_error is not None:
            raise MockReportingError(self.note_error)
        return self.note_id


def make_context_ready_mr_run(
    tmp_path: Path,
) -> tuple[SqliteStore, ConnectedProject, TriageRun]:
    store = SqliteStore(tmp_path / "triage.sqlite")
    token_store = RecordingProjectTokenStore()
    token_store.tokens["secret-ref:1"] = "project-token"
    project = make_connected_project()
    run = make_triage_run(
        project,
        pipeline_kind="merge_request",
        report_target=MergeRequestTarget(
            project_id=project.gitlab_project_id,
            merge_request_iid=17,
        ),
    )
    store.create_connected_project(project)
    store.create_triage_run(run)
    return (
        store,
        project,
        PipelineContextBuilder(
            gitlab_context_client=FakeGitLabContextClient(),
            token_store=token_store,
            persistence_store=store,
        ).build_for_run(connected_project=project, triage_run=run),
    )


def make_context_ready_branch_run(
    tmp_path: Path,
) -> tuple[SqliteStore, ConnectedProject, TriageRun]:
    store = SqliteStore(tmp_path / "triage.sqlite")
    token_store = RecordingProjectTokenStore()
    token_store.tokens["secret-ref:1"] = "project-token"
    project = make_connected_project()
    run = make_triage_run(
        project,
        pipeline_kind="branch",
        report_target=IssueTarget(
            project_id=project.gitlab_project_id,
            issue_iid=0,
        ),
    )
    store.create_connected_project(project)
    store.create_triage_run(run)
    return (
        store,
        project,
        PipelineContextBuilder(
            gitlab_context_client=FakeGitLabContextClient(),
            token_store=token_store,
            persistence_store=store,
        ).build_for_run(connected_project=project, triage_run=run),
    )


def unsafe_real_triage_result() -> TriageResult:
    """Build schema-valid Codex-like output with secret-shaped text."""
    return TriageResult(
        root_cause_hypothesis="secret: hypothesis-token caused the checkout failure",
        category="unknown",
        confidence=0.73,
        evidence=[
            EvidenceItem(
                source="job_trace",
                file="PRIVATE-TOKEN: file-token",
                snippet="PRIVATE-TOKEN: evidence-token from the failed job",
            )
        ],
        retry_safe=False,
        recommended_action="recommend_only",
        suggested_fix="api_key: fix-key Review the failing assertion manually.",
        needs_human_review=True,
    )


def sha256(value: str) -> str:
    return f"sha256:{hashlib.sha256(value.encode('utf-8')).hexdigest()}"
