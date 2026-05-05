"""Tests for Spike 5.2 mock triage and MR-note reporting."""

# pylint: disable=duplicate-code,too-many-lines

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal, cast
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
    FixFileChange,
    FixPatch,
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
    DeterministicScratchFixer,
    GitLabIssue,
    GlabGitLabFixMrClient,
    GlabGitLabIssueClient,
    GlabGitLabMrNoteClient,
    GlabGitLabRetryClient,
    MockMrReporter,
    MockReportingError,
    TriageModeSettings,
    bot_fix_mr_unavailable_reason,
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


def test_glab_retry_client_uses_expected_api_payloads() -> None:
    executor = Mock()
    executor.api.side_effect = [{"id": 5001}, {"id": 9001}]
    client = GlabGitLabRetryClient(executor=executor)

    job_id = client.retry_job(
        project_id=2002,
        job_id=4001,
        project_token="project-token",
    )
    pipeline_id = client.retry_pipeline(
        project_id=2002,
        pipeline_id=9001,
        project_token="project-token",
    )

    assert job_id == 5001
    assert pipeline_id == 9001
    requests = [call.args[0] for call in executor.api.call_args_list]
    assert requests == [
        GlabApiRequest(
            endpoint="projects/2002/jobs/4001/retry",
            method="POST",
        ),
        GlabApiRequest(
            endpoint="projects/2002/pipelines/9001/retry",
            method="POST",
        ),
    ]
    assert all(
        call.kwargs["token"] == "project-token" for call in executor.api.call_args_list
    )


def test_glab_fix_mr_client_uses_expected_api_payloads() -> None:
    executor = Mock()
    executor.api.side_effect = [{"id": "abc123commit"}, {"iid": 44}]
    client = GlabGitLabFixMrClient(executor=executor)
    fix_patch = make_fix_patch()

    commit_sha = client.create_commit(
        project_id=2002,
        fix_patch=fix_patch,
        project_token="project-token",
    )
    merge_request_iid = client.create_merge_request(
        project_id=2002,
        fix_patch=fix_patch,
        project_token="project-token",
    )

    assert commit_sha == "abc123commit"
    assert merge_request_iid == 44
    requests = [call.args[0] for call in executor.api.call_args_list]
    assert requests[0] == GlabApiRequest(
        endpoint="projects/2002/repository/commits",
        method="POST",
        json_body={
            "branch": "codex-fix/pipeline-1001-abc123",
            "start_branch": "feature/checkout",
            "commit_message": "Add Codex triage fix artifact",
            "actions": [
                {
                    "action": "create",
                    "file_path": "codex-triage/fix-1001.md",
                    "content": "bounded fix content",
                }
            ],
        },
    )
    assert requests[0].fields is None
    assert "actions[][action]" not in str(requests[0].json_body)
    assert requests[1] == GlabApiRequest(
        endpoint="projects/2002/merge_requests",
        method="POST",
        fields={
            "source_branch": "codex-fix/pipeline-1001-abc123",
            "target_branch": "feature/checkout",
            "title": "Codex fix for pipeline 1001",
            "description": "Links back to the original MR.",
            "remove_source_branch": "true",
        },
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


def test_reporter_retries_job_and_posts_action_note_when_policy_allows(
    tmp_path: Path,
) -> None:
    store, project, run = make_context_ready_mr_run(tmp_path)
    retry_project = project.model_copy(
        update={"action_policy": ProjectActionPolicy(auto_retry=True)}
    )
    token_store = RecordingProjectTokenStore()
    token_store.tokens["secret-ref:1"] = "project-token"
    note_client = RecordingMrNoteClient(note_ids=[9001, 9002])
    retry_client = RecordingRetryClient(job_result_id=5001)
    codex_adapter = RecordingCodexAdapter(
        outcome=CodexTriageOutcome(
            adapter_mode="codex",
            fallback_reason=None,
            triage_result=retry_triage_result(
                recommended_action="retry_job",
                retry_safe=True,
            ),
        )
    )

    reported_run = MockMrReporter(
        mr_note_client=note_client,
        token_store=token_store,
        persistence_store=store,
        retry_client=retry_client,
        triage_mode=PIPELINE_TRIAGE_MODE_CODEX,
        codex_adapter=codex_adapter,
    ).report_for_run(connected_project=retry_project, triage_run=run)

    assert reported_run.status == "actioned"
    assert reported_run.action_plan is not None
    assert reported_run.action_plan.action == "retry_job"
    assert reported_run.gitlab_note_ids == [9001, 9002]
    assert retry_client.job_calls == [
        {
            "project_id": project.gitlab_project_id,
            "job_id": 4001,
            "project_token": "project-token",
        }
    ]
    assert not retry_client.pipeline_calls
    assert len(note_client.calls) == 2
    action_note = cast(str, note_client.calls[1]["body"])
    assert "Codex Pipeline Triage Action" in action_note
    assert "retry_job" in action_note
    assert "completed" in action_note
    assert "5001" in action_note
    assert "project-token" not in action_note
    action_logs = store.list_action_logs_for_run(run.id)
    assert [action_log.action for action_log in action_logs].count("post_mr_note") == 2
    retry_log = next(
        action_log for action_log in action_logs if action_log.action == "retry_job"
    )
    assert retry_log.status == "completed"
    assert retry_log.external_id == "5001"


def test_reporter_retries_pipeline_when_policy_allows(tmp_path: Path) -> None:
    store, project, run = make_context_ready_mr_run(tmp_path)
    retry_project = project.model_copy(
        update={"action_policy": ProjectActionPolicy(auto_retry=True)}
    )
    token_store = RecordingProjectTokenStore()
    token_store.tokens["secret-ref:1"] = "project-token"
    retry_client = RecordingRetryClient(pipeline_result_id=9102)
    codex_adapter = RecordingCodexAdapter(
        outcome=CodexTriageOutcome(
            adapter_mode="codex",
            fallback_reason=None,
            triage_result=retry_triage_result(
                recommended_action="retry_pipeline",
                retry_safe=True,
            ),
        )
    )

    reported_run = MockMrReporter(
        mr_note_client=RecordingMrNoteClient(note_ids=[9001, 9002]),
        token_store=token_store,
        persistence_store=store,
        retry_client=retry_client,
        triage_mode=PIPELINE_TRIAGE_MODE_CODEX,
        codex_adapter=codex_adapter,
    ).report_for_run(connected_project=retry_project, triage_run=run)

    assert reported_run.status == "actioned"
    assert reported_run.action_plan is not None
    assert reported_run.action_plan.action == "retry_pipeline"
    assert not retry_client.job_calls
    assert retry_client.pipeline_calls == [
        {
            "project_id": project.gitlab_project_id,
            "pipeline_id": run.pipeline_id,
            "project_token": "project-token",
        }
    ]


def test_retry_blocked_when_retry_safe_false(tmp_path: Path) -> None:
    store, project, run = make_context_ready_mr_run(tmp_path)
    retry_project = project.model_copy(
        update={"action_policy": ProjectActionPolicy(auto_retry=True)}
    )
    reported_run, retry_client = report_retry_recommendation(
        connected_project=retry_project,
        run=run,
        store=store,
        retry_safe=False,
    )

    assert reported_run.status == "posted"
    assert reported_run.action_plan is not None
    assert reported_run.action_plan.action == "recommend_only"
    assert not retry_client.job_calls
    action_logs = store.list_action_logs_for_run(run.id)
    assert sorted(action_log.action for action_log in action_logs) == [
        "post_mr_note",
        "retry_job",
    ]
    retry_log = next(
        action_log for action_log in action_logs if action_log.action == "retry_job"
    )
    assert retry_log.policy_decision == "blocked"
    assert retry_log.status == "skipped"
    assert retry_log.fallback_reason == (
        "Retry blocked because Codex did not mark the result retry_safe."
    )


def test_retry_blocked_when_project_policy_is_recommend_only(tmp_path: Path) -> None:
    store, project, run = make_context_ready_mr_run(tmp_path)
    retry_project = project.model_copy(
        update={
            "action_policy": ProjectActionPolicy(
                recommend_only=True,
                auto_retry=True,
            )
        }
    )
    reported_run, retry_client = report_retry_recommendation(
        connected_project=retry_project,
        run=run,
        store=store,
        retry_safe=True,
    )

    assert reported_run.status == "posted"
    assert reported_run.action_plan is not None
    assert reported_run.action_plan.action == "recommend_only"
    assert not retry_client.job_calls
    action_logs = store.list_action_logs_for_run(run.id)
    retry_log = next(
        action_log for action_log in action_logs if action_log.action == "retry_job"
    )
    assert retry_log.policy_decision == "blocked"
    assert retry_log.status == "skipped"
    assert retry_log.fallback_reason == (
        "Retry blocked because project policy is recommend-only."
    )


def test_retry_blocked_by_default_project_policy(tmp_path: Path) -> None:
    store, project, run = make_context_ready_mr_run(tmp_path)
    reported_run, retry_client = report_retry_recommendation(
        connected_project=project,
        run=run,
        store=store,
        retry_safe=True,
    )

    assert project.action_policy.auto_retry is False
    assert reported_run.status == "posted"
    assert reported_run.action_plan is not None
    assert reported_run.action_plan.action == "recommend_only"
    assert not retry_client.job_calls
    action_logs = store.list_action_logs_for_run(run.id)
    retry_log = next(
        action_log for action_log in action_logs if action_log.action == "retry_job"
    )
    assert retry_log.policy_decision == "blocked"
    assert retry_log.status == "skipped"
    assert retry_log.fallback_reason == (
        "Retry blocked because project policy has auto_retry disabled."
    )


# pylint: disable-next=too-many-locals
def test_reporter_creates_fix_mr_when_policy_allows(tmp_path: Path) -> None:
    store, project, run = make_context_ready_mr_run(tmp_path)
    fix_project = project.model_copy(
        update={"action_policy": ProjectActionPolicy(auto_create_fix_mr=True)}
    )
    token_store = RecordingProjectTokenStore()
    token_store.tokens["secret-ref:1"] = "project-token"
    note_client = RecordingMrNoteClient(note_ids=[9001, 9002])
    fixer = RecordingFixer()
    fix_mr_client = RecordingFixMrClient()
    codex_adapter = RecordingCodexAdapter(
        outcome=CodexTriageOutcome(
            adapter_mode="codex",
            fallback_reason=None,
            triage_result=fix_mr_triage_result(),
        )
    )

    reporter = MockMrReporter(
        mr_note_client=note_client,
        token_store=token_store,
        persistence_store=store,
        fixer=fixer,
        fix_mr_client=fix_mr_client,
        triage_mode=PIPELINE_TRIAGE_MODE_CODEX,
        codex_adapter=codex_adapter,
    )

    reported_run = reporter.report_for_run(
        connected_project=fix_project,
        triage_run=run,
    )
    replayed_run = reporter.report_for_run(
        connected_project=fix_project,
        triage_run=reported_run,
    )

    assert reported_run.status == "monitoring"
    assert replayed_run == reported_run
    assert reported_run.fix_merge_request_iid == 44
    assert reported_run.action_plan is not None
    assert reported_run.action_plan.action == "create_fix_mr"
    assert reported_run.action_plan.requires_fixer_agent is True
    assert reported_run.gitlab_note_ids == [9001, 9002]
    assert fixer.calls == [{"pipeline_id": run.pipeline_id}]
    assert fix_mr_client.commit_calls == [
        {
            "project_id": project.gitlab_project_id,
            "source_branch": "codex-fix/pipeline-1001-abc123",
            "target_branch": run.ref,
            "project_token": "project-token",
        }
    ]
    assert fix_mr_client.mr_calls == [
        {
            "project_id": project.gitlab_project_id,
            "source_branch": "codex-fix/pipeline-1001-abc123",
            "target_branch": run.ref,
            "project_token": "project-token",
        }
    ]
    assert fix_mr_client.commit_calls[0]["source_branch"] != run.ref
    assert len(note_client.calls) == 2
    action_note = cast(str, note_client.calls[1]["body"])
    assert "Codex Pipeline Triage Action" in action_note
    assert "create_fix_mr" in action_note
    assert "codex-fix/pipeline-1001-abc123" in action_note
    assert "44" in action_note
    assert "project-token" not in action_note
    action_logs = store.list_action_logs_for_run(run.id)
    action_counts = {
        action: [log.action for log in action_logs].count(action)
        for action in {log.action for log in action_logs}
    }
    assert action_counts == {
        "post_mr_note": 2,
        "create_commit": 1,
        "create_merge_request": 1,
    }
    assert all(log.status == "completed" for log in action_logs)
    monitors = store.list_pipeline_monitors_for_run(run.id)
    assert len(monitors) == 1
    assert monitors[0].status == "waiting"
    assert monitors[0].expected_ref == "codex-fix/pipeline-1001-abc123"
    assert monitors[0].expected_sha == "abc123commit"
    assert monitors[0].expected_pipeline_id is None
    assert len(fix_mr_client.commit_calls) == 1
    assert len(fix_mr_client.mr_calls) == 1


# pylint: disable-next=too-many-locals
def test_reporter_manual_fix_mr_uses_existing_executor_path(
    tmp_path: Path,
) -> None:
    store, project, run = make_context_ready_mr_run(tmp_path)
    fix_project = project.model_copy(
        update={"action_policy": ProjectActionPolicy(auto_create_fix_mr=True)}
    )
    store.update_connected_project(fix_project)
    triaged_run = store.update_triage_run(
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
    note_client = RecordingMrNoteClient(note_ids=[9002])
    fixer = RecordingFixer()
    fix_mr_client = RecordingFixMrClient()
    reporter = MockMrReporter(
        mr_note_client=note_client,
        token_store=token_store,
        persistence_store=store,
        fixer=fixer,
        fix_mr_client=fix_mr_client,
    )

    actioned_run = reporter.create_fix_mr_for_run(
        connected_project=fix_project,
        triage_run=triaged_run,
    )
    replayed_run = reporter.create_fix_mr_for_run(
        connected_project=fix_project,
        triage_run=actioned_run,
    )

    assert actioned_run.status == "monitoring"
    assert replayed_run == actioned_run
    assert actioned_run.fix_merge_request_iid == 44
    assert actioned_run.action_plan is not None
    assert actioned_run.action_plan.action == "create_fix_mr"
    assert fixer.calls == [{"pipeline_id": run.pipeline_id}]
    assert fix_mr_client.commit_calls == [
        {
            "project_id": project.gitlab_project_id,
            "source_branch": "codex-fix/pipeline-1001-abc123",
            "target_branch": run.ref,
            "project_token": "project-token",
        }
    ]
    assert fix_mr_client.mr_calls == [
        {
            "project_id": project.gitlab_project_id,
            "source_branch": "codex-fix/pipeline-1001-abc123",
            "target_branch": run.ref,
            "project_token": "project-token",
        }
    ]
    assert fix_mr_client.commit_calls[0]["source_branch"] != run.ref
    assert len(note_client.calls) == 1
    action_note = cast(str, note_client.calls[0]["body"])
    assert "Codex Pipeline Triage Action" in action_note
    assert "create_fix_mr" in action_note
    assert "project-token" not in action_note
    action_logs = store.list_action_logs_for_run(run.id)
    action_counts = {
        action: [log.action for log in action_logs].count(action)
        for action in {log.action for log in action_logs}
    }
    assert action_counts == {
        "post_mr_note": 1,
        "create_commit": 1,
        "create_merge_request": 1,
    }
    monitors = store.list_pipeline_monitors_for_run(run.id)
    assert len(monitors) == 1
    assert monitors[0].expected_ref == "codex-fix/pipeline-1001-abc123"
    assert monitors[0].expected_sha == "abc123commit"


def test_bot_fix_mr_unavailable_reason_enforces_manual_button_gates(
    tmp_path: Path,
) -> None:
    store, project, run = make_context_ready_mr_run(tmp_path)
    eligible_project = project.model_copy(
        update={"action_policy": ProjectActionPolicy(auto_create_fix_mr=True)}
    )
    triaged_run = run.model_copy(update={"triage_json": fix_mr_triage_result()})

    assert (
        bot_fix_mr_unavailable_reason(
            connected_project=eligible_project,
            triage_run=triaged_run,
            action_logs=[],
        )
        is None
    )
    assert "schema-valid triage output" in cast(
        str,
        bot_fix_mr_unavailable_reason(
            connected_project=eligible_project,
            triage_run=run,
            action_logs=[],
        ),
    )

    branch_path = tmp_path / "branch"
    branch_path.mkdir()
    _, _, branch_run = make_context_ready_branch_run(branch_path)
    branch_run = branch_run.model_copy(update={"triage_json": fix_mr_triage_result()})
    assert "merge-request runs" in cast(
        str,
        bot_fix_mr_unavailable_reason(
            connected_project=eligible_project,
            triage_run=branch_run,
            action_logs=[],
        ),
    )

    assert "auto_create_fix_mr disabled" in cast(
        str,
        bot_fix_mr_unavailable_reason(
            connected_project=project,
            triage_run=triaged_run,
            action_logs=[],
        ),
    )
    recommend_only_project = project.model_copy(
        update={
            "action_policy": ProjectActionPolicy(
                auto_create_fix_mr=True,
                recommend_only=True,
            )
        }
    )
    assert "recommend-only" in cast(
        str,
        bot_fix_mr_unavailable_reason(
            connected_project=recommend_only_project,
            triage_run=triaged_run,
            action_logs=[],
        ),
    )
    existing_fix_run = triaged_run.model_copy(update={"fix_merge_request_iid": 44})
    assert "already exists" in cast(
        str,
        bot_fix_mr_unavailable_reason(
            connected_project=eligible_project,
            triage_run=existing_fix_run,
            action_logs=[],
        ),
    )
    assert store.get_triage_run(run.id) is not None


def test_monitor_pass_event_closes_monitor_and_posts_mr_note(tmp_path: Path) -> None:
    store, project, reporter, note_client, monitoring_run = make_monitoring_fix_mr_run(
        tmp_path
    )
    monitor = store.list_pipeline_monitors_for_run(monitoring_run.id)[0]

    completed_run = reporter.report_monitor_event(
        connected_project=project,
        monitor=monitor,
        pipeline_id=monitoring_run.pipeline_id + 100,
        pipeline_status="success",
        sha=monitor.expected_sha or "",
    )
    replayed_run = reporter.report_monitor_event(
        connected_project=project,
        monitor=store.get_pipeline_monitor(monitor.id) or monitor,
        pipeline_id=monitoring_run.pipeline_id + 100,
        pipeline_status="success",
        sha=monitor.expected_sha or "",
    )

    assert completed_run is not None
    assert replayed_run == completed_run
    assert completed_run.status == "completed"
    assert completed_run.fallback_reason is None
    assert completed_run.gitlab_note_ids == [9001, 9002, 9003]
    updated_monitor = store.get_pipeline_monitor(monitor.id)
    assert updated_monitor is not None
    assert updated_monitor.status == "passed"
    assert updated_monitor.expected_pipeline_id == monitoring_run.pipeline_id + 100
    assert len(note_client.calls) == 3
    monitor_note = cast(str, note_client.calls[2]["body"])
    assert "Codex Pipeline Triage Monitor" in monitor_note
    assert "Follow-up pipeline passed." in monitor_note
    assert "project-token" not in monitor_note
    action_logs = store.list_action_logs_for_run(monitoring_run.id)
    assert [log.action for log in action_logs].count("post_mr_note") == 3


def test_monitor_fail_event_reports_failure(tmp_path: Path) -> None:
    store, project, reporter, note_client, monitoring_run = make_monitoring_fix_mr_run(
        tmp_path
    )
    monitor = store.list_pipeline_monitors_for_run(monitoring_run.id)[0]

    failed_run = reporter.report_monitor_event(
        connected_project=project,
        monitor=monitor,
        pipeline_id=monitoring_run.pipeline_id + 101,
        pipeline_status="failed",
        sha=monitor.expected_sha or "",
    )

    assert failed_run is not None
    assert failed_run.status == "failed"
    assert failed_run.fallback_reason == "Follow-up pipeline failed."
    assert failed_run.gitlab_note_ids == [9001, 9002, 9003]
    updated_monitor = store.get_pipeline_monitor(monitor.id)
    assert updated_monitor is not None
    assert updated_monitor.status == "failed"
    assert updated_monitor.expected_pipeline_id == monitoring_run.pipeline_id + 101
    monitor_note = cast(str, note_client.calls[2]["body"])
    assert "Follow-up pipeline failed." in monitor_note
    assert "project-token" not in monitor_note


def test_monitor_timeout_posts_timed_out_status(tmp_path: Path) -> None:
    store, project, reporter, note_client, monitoring_run = make_monitoring_fix_mr_run(
        tmp_path
    )
    monitor = store.list_pipeline_monitors_for_run(monitoring_run.id)[0]

    timed_out_run = reporter.report_monitor_timeout(
        connected_project=project,
        monitor=monitor,
    )

    assert timed_out_run is not None
    assert timed_out_run.status == "failed"
    assert timed_out_run.fallback_reason == "Follow-up pipeline timed out."
    assert timed_out_run.gitlab_note_ids == [9001, 9002, 9003]
    updated_monitor = store.get_pipeline_monitor(monitor.id)
    assert updated_monitor is not None
    assert updated_monitor.status == "timed_out"
    monitor_note = cast(str, note_client.calls[2]["body"])
    assert "Follow-up pipeline timed out." in monitor_note
    assert "project-token" not in monitor_note


def test_fix_mr_blocked_by_default_project_policy(tmp_path: Path) -> None:
    store, project, run = make_context_ready_mr_run(tmp_path)
    token_store = RecordingProjectTokenStore()
    token_store.tokens["secret-ref:1"] = "project-token"
    fixer = RecordingFixer()
    fix_mr_client = RecordingFixMrClient()
    codex_adapter = RecordingCodexAdapter(
        outcome=CodexTriageOutcome(
            adapter_mode="codex",
            fallback_reason=None,
            triage_result=fix_mr_triage_result(),
        )
    )

    reported_run = MockMrReporter(
        mr_note_client=RecordingMrNoteClient(),
        token_store=token_store,
        persistence_store=store,
        fixer=fixer,
        fix_mr_client=fix_mr_client,
        triage_mode=PIPELINE_TRIAGE_MODE_CODEX,
        codex_adapter=codex_adapter,
    ).report_for_run(connected_project=project, triage_run=run)

    assert project.action_policy.auto_create_fix_mr is False
    assert reported_run.status == "posted"
    assert reported_run.fix_merge_request_iid is None
    assert reported_run.action_plan is not None
    assert reported_run.action_plan.action == "recommend_only"
    assert not fixer.calls
    assert not fix_mr_client.commit_calls
    assert not fix_mr_client.mr_calls
    action_logs = store.list_action_logs_for_run(run.id)
    assert sorted(log.action for log in action_logs) == [
        "create_merge_request",
        "post_mr_note",
    ]
    skipped_log = next(
        log for log in action_logs if log.action == "create_merge_request"
    )
    assert skipped_log.policy_decision == "blocked"
    assert skipped_log.status == "skipped"
    assert skipped_log.fallback_reason == (
        "Fix MR blocked because project policy has auto_create_fix_mr disabled."
    )


def test_fix_mr_blocked_when_project_policy_is_recommend_only(
    tmp_path: Path,
) -> None:
    store, project, run = make_context_ready_mr_run(tmp_path)
    blocked_project = project.model_copy(
        update={
            "action_policy": ProjectActionPolicy(
                recommend_only=True,
                auto_create_fix_mr=True,
            )
        }
    )
    token_store = RecordingProjectTokenStore()
    token_store.tokens["secret-ref:1"] = "project-token"
    fixer = RecordingFixer()
    fix_mr_client = RecordingFixMrClient()
    codex_adapter = RecordingCodexAdapter(
        outcome=CodexTriageOutcome(
            adapter_mode="codex",
            fallback_reason=None,
            triage_result=fix_mr_triage_result(),
        )
    )

    reported_run = MockMrReporter(
        mr_note_client=RecordingMrNoteClient(),
        token_store=token_store,
        persistence_store=store,
        fixer=fixer,
        fix_mr_client=fix_mr_client,
        triage_mode=PIPELINE_TRIAGE_MODE_CODEX,
        codex_adapter=codex_adapter,
    ).report_for_run(connected_project=blocked_project, triage_run=run)

    assert reported_run.status == "posted"
    assert reported_run.fix_merge_request_iid is None
    assert reported_run.action_plan is not None
    assert reported_run.action_plan.action == "recommend_only"
    assert not fixer.calls
    assert not fix_mr_client.commit_calls
    assert not fix_mr_client.mr_calls
    action_logs = store.list_action_logs_for_run(run.id)
    skipped_log = next(
        log for log in action_logs if log.action == "create_merge_request"
    )
    assert skipped_log.policy_decision == "blocked"
    assert skipped_log.status == "skipped"
    assert skipped_log.fallback_reason == (
        "Fix MR blocked because project policy is recommend-only."
    )


def test_fix_mr_commit_failure_marks_run_failed(tmp_path: Path) -> None:
    store, project, run = make_context_ready_mr_run(tmp_path)
    fix_project = project.model_copy(
        update={"action_policy": ProjectActionPolicy(auto_create_fix_mr=True)}
    )
    token_store = RecordingProjectTokenStore()
    token_store.tokens["secret-ref:1"] = "project-token"
    fix_mr_client = RecordingFixMrClient(commit_error="commit failed")
    codex_adapter = RecordingCodexAdapter(
        outcome=CodexTriageOutcome(
            adapter_mode="codex",
            fallback_reason=None,
            triage_result=fix_mr_triage_result(),
        )
    )

    failed_run = MockMrReporter(
        mr_note_client=RecordingMrNoteClient(),
        token_store=token_store,
        persistence_store=store,
        fixer=RecordingFixer(),
        fix_mr_client=fix_mr_client,
        triage_mode=PIPELINE_TRIAGE_MODE_CODEX,
        codex_adapter=codex_adapter,
    ).report_for_run(connected_project=fix_project, triage_run=run)

    assert failed_run.status == "failed"
    assert failed_run.fallback_reason == "Fix commit creation failed."
    assert len(fix_mr_client.commit_calls) == 1
    assert not fix_mr_client.mr_calls
    action_logs = store.list_action_logs_for_run(run.id)
    commit_log = next(log for log in action_logs if log.action == "create_commit")
    assert commit_log.status == "failed"
    assert commit_log.fallback_reason == "Fix commit creation failed."


def test_fix_mr_create_failure_marks_run_failed_after_commit(tmp_path: Path) -> None:
    store, project, run = make_context_ready_mr_run(tmp_path)
    fix_project = project.model_copy(
        update={"action_policy": ProjectActionPolicy(auto_create_fix_mr=True)}
    )
    token_store = RecordingProjectTokenStore()
    token_store.tokens["secret-ref:1"] = "project-token"
    fix_mr_client = RecordingFixMrClient(mr_error="MR failed")
    codex_adapter = RecordingCodexAdapter(
        outcome=CodexTriageOutcome(
            adapter_mode="codex",
            fallback_reason=None,
            triage_result=fix_mr_triage_result(),
        )
    )

    failed_run = MockMrReporter(
        mr_note_client=RecordingMrNoteClient(),
        token_store=token_store,
        persistence_store=store,
        fixer=RecordingFixer(),
        fix_mr_client=fix_mr_client,
        triage_mode=PIPELINE_TRIAGE_MODE_CODEX,
        codex_adapter=codex_adapter,
    ).report_for_run(connected_project=fix_project, triage_run=run)

    assert failed_run.status == "failed"
    assert failed_run.fallback_reason == "Fix merge request creation failed."
    assert len(fix_mr_client.commit_calls) == 1
    assert len(fix_mr_client.mr_calls) == 1
    action_logs = store.list_action_logs_for_run(run.id)
    commit_log = next(log for log in action_logs if log.action == "create_commit")
    mr_log = next(log for log in action_logs if log.action == "create_merge_request")
    assert commit_log.status == "completed"
    assert mr_log.status == "failed"
    assert mr_log.fallback_reason == "Fix merge request creation failed."


def test_deterministic_fixer_bounds_and_redacts_patch_content(
    tmp_path: Path,
) -> None:
    _, _, run = make_context_ready_mr_run(tmp_path)
    triage_result = fix_mr_triage_result().model_copy(
        update={
            "root_cause_hypothesis": "PRIVATE-TOKEN: hypothesis-token",
            "suggested_fix": "password = fix-password",
        }
    )

    fix_patch = DeterministicScratchFixer().create_patch(
        triage_run=run,
        triage_result=triage_result,
    )

    source_branch = str(fix_patch.model_dump()["source_branch"])
    target_branch = str(fix_patch.model_dump()["target_branch"])
    assert source_branch.startswith("codex-fix/pipeline-")
    assert source_branch != run.ref
    assert target_branch == run.ref
    assert fix_patch.changes[0].file_path == "codex-triage/fix-9001.md"
    assert "hypothesis-token" not in fix_patch.changes[0].content
    assert "fix-password" not in fix_patch.changes[0].content
    assert "[REDACTED]" in fix_patch.changes[0].content


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
    note_ids: list[int] = field(default_factory=list)
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
        if self.note_ids:
            return self.note_ids[len(self.calls) - 1]
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


@dataclass
class RecordingRetryClient:
    """Fake retry client that records controlled retry calls."""

    job_result_id: int = 5001
    pipeline_result_id: int = 9102
    job_calls: list[dict[str, object]] = field(default_factory=list)
    pipeline_calls: list[dict[str, object]] = field(default_factory=list)

    def retry_job(
        self,
        *,
        project_id: int,
        job_id: int,
        project_token: str,
    ) -> int:
        self.job_calls.append(
            {
                "project_id": project_id,
                "job_id": job_id,
                "project_token": project_token,
            }
        )
        return self.job_result_id

    def retry_pipeline(
        self,
        *,
        project_id: int,
        pipeline_id: int,
        project_token: str,
    ) -> int:
        self.pipeline_calls.append(
            {
                "project_id": project_id,
                "pipeline_id": pipeline_id,
                "project_token": project_token,
            }
        )
        return self.pipeline_result_id


@dataclass
class RecordingFixer:
    """Fake fixer that returns one schema-valid patch."""

    # pylint: disable-next=unnecessary-lambda
    fix_patch: FixPatch = field(default_factory=lambda: make_fix_patch())
    calls: list[dict[str, object]] = field(default_factory=list)

    def create_patch(
        self,
        *,
        triage_run: TriageRun,
        triage_result: TriageResult,
    ) -> FixPatch:
        del triage_result
        self.calls.append({"pipeline_id": triage_run.pipeline_id})
        return self.fix_patch.model_copy(
            update={"target_branch": triage_run.ref},
        )


@dataclass
class RecordingFixMrClient:
    """Fake fix MR client that records controlled GitLab mutations."""

    commit_sha: str = "abc123commit"
    merge_request_iid: int = 44
    commit_error: str | None = None
    mr_error: str | None = None
    commit_calls: list[dict[str, object]] = field(default_factory=list)
    mr_calls: list[dict[str, object]] = field(default_factory=list)

    def create_commit(
        self,
        *,
        project_id: int,
        fix_patch: FixPatch,
        project_token: str,
    ) -> str:
        self.commit_calls.append(
            {
                "project_id": project_id,
                "source_branch": fix_patch.source_branch,
                "target_branch": fix_patch.target_branch,
                "project_token": project_token,
            }
        )
        if self.commit_error is not None:
            raise MockReportingError(self.commit_error)
        return self.commit_sha

    def create_merge_request(
        self,
        *,
        project_id: int,
        fix_patch: FixPatch,
        project_token: str,
    ) -> int:
        self.mr_calls.append(
            {
                "project_id": project_id,
                "source_branch": fix_patch.source_branch,
                "target_branch": fix_patch.target_branch,
                "project_token": project_token,
            }
        )
        if self.mr_error is not None:
            raise MockReportingError(self.mr_error)
        return self.merge_request_iid


def report_retry_recommendation(
    *,
    connected_project: ConnectedProject,
    run: TriageRun,
    store: SqliteStore,
    retry_safe: bool,
) -> tuple[TriageRun, RecordingRetryClient]:
    token_store = RecordingProjectTokenStore()
    token_store.tokens["secret-ref:1"] = "project-token"
    retry_client = RecordingRetryClient()
    codex_adapter = RecordingCodexAdapter(
        outcome=CodexTriageOutcome(
            adapter_mode="codex",
            fallback_reason=None,
            triage_result=retry_triage_result(
                recommended_action="retry_job",
                retry_safe=retry_safe,
            ),
        )
    )
    reported_run = MockMrReporter(
        mr_note_client=RecordingMrNoteClient(),
        token_store=token_store,
        persistence_store=store,
        retry_client=retry_client,
        triage_mode=PIPELINE_TRIAGE_MODE_CODEX,
        codex_adapter=codex_adapter,
    ).report_for_run(connected_project=connected_project, triage_run=run)
    return reported_run, retry_client


def make_monitoring_fix_mr_run(
    tmp_path: Path,
) -> tuple[
    SqliteStore,
    ConnectedProject,
    MockMrReporter,
    RecordingMrNoteClient,
    TriageRun,
]:
    """Create a fix-MR run with one waiting monitor for monitor tests."""
    store, project, run = make_context_ready_mr_run(tmp_path)
    fix_project = project.model_copy(
        update={"action_policy": ProjectActionPolicy(auto_create_fix_mr=True)}
    )
    token_store = RecordingProjectTokenStore()
    token_store.tokens["secret-ref:1"] = "project-token"
    note_client = RecordingMrNoteClient(note_ids=[9001, 9002, 9003])
    reporter = MockMrReporter(
        mr_note_client=note_client,
        token_store=token_store,
        persistence_store=store,
        fixer=RecordingFixer(),
        fix_mr_client=RecordingFixMrClient(),
        triage_mode=PIPELINE_TRIAGE_MODE_CODEX,
        codex_adapter=RecordingCodexAdapter(
            outcome=CodexTriageOutcome(
                adapter_mode="codex",
                fallback_reason=None,
                triage_result=fix_mr_triage_result(),
            )
        ),
    )
    monitoring_run = reporter.report_for_run(
        connected_project=fix_project,
        triage_run=run,
    )
    return store, fix_project, reporter, note_client, monitoring_run


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


def retry_triage_result(
    *,
    recommended_action: str,
    retry_safe: bool,
) -> TriageResult:
    """Build a schema-valid Codex-like retry recommendation."""
    return TriageResult(
        root_cause_hypothesis="The failed job appears to be a transient runner issue.",
        category="infra",
        confidence=0.81,
        evidence=[
            EvidenceItem(
                source="job_trace",
                snippet="Runner lost contact before executing the test body.",
            )
        ],
        retry_safe=retry_safe,
        recommended_action=cast(
            Literal[
                "recommend_only",
                "retry_job",
                "retry_pipeline",
                "create_fix_mr",
            ],
            recommended_action,
        ),
        suggested_fix="Retry the failed work once before deeper investigation.",
        needs_human_review=False,
    )


def fix_mr_triage_result() -> TriageResult:
    """Build a schema-valid Codex-like fix MR recommendation."""
    return TriageResult(
        root_cause_hypothesis="Checkout tax rounding changed and needs a small fix.",
        category="code-bug",
        confidence=0.84,
        evidence=[
            EvidenceItem(
                source="mr_diff",
                file="checkout/tax.py",
                snippet="Tax calculation now rounds before applying discounts.",
            )
        ],
        retry_safe=False,
        recommended_action="create_fix_mr",
        suggested_fix="Restore discount-before-rounding behavior in checkout tax.",
        needs_human_review=False,
    )


def make_fix_patch() -> FixPatch:
    """Build a schema-valid one-file patch for executor tests."""
    return FixPatch(
        source_branch="codex-fix/pipeline-1001-abc123",
        target_branch="feature/checkout",
        commit_message="Add Codex triage fix artifact",
        merge_request_title="Codex fix for pipeline 1001",
        merge_request_description="Links back to the original MR.",
        changes=[
            FixFileChange(
                action="create",
                file_path="codex-triage/fix-1001.md",
                content="bounded fix content",
            )
        ],
    )


def sha256(value: str) -> str:
    return f"sha256:{hashlib.sha256(value.encode('utf-8')).hexdigest()}"
