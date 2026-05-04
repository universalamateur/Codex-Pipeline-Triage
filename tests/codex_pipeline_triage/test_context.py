"""Tests for Spike 5.1 bounded GitLab context building."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from codex_pipeline_triage.context import (
    MAX_DIFF_CHARS,
    MAX_METADATA_CHARS,
    MAX_TRACE_CHARS,
    GitLabContextJob,
    GitLabDiffFile,
    PipelineContextBuilder,
)
from codex_pipeline_triage.models import (
    ConnectedProject,
    InternalTarget,
    IssueTarget,
    MergeRequestTarget,
    PipelineKind,
    ProjectActionPolicy,
    TriageRun,
)
from codex_pipeline_triage.persistence import SqliteStore
from tests.codex_pipeline_triage.test_projects import RecordingProjectTokenStore


def test_context_builder_fetches_failed_job_traces_and_mr_diff(
    tmp_path: Path,
) -> None:
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
    context_client = FakeGitLabContextClient(
        traces={
            4001: (
                "pytest failed\n"
                "Authorization: Bearer synthetic-value\n"
                "PRIVATE-TOKEN: private-token-value\n"
                "password = password-value\n"
                "secret: secret-value\n"
                "api_key: api-key-value\n" + ("x" * (MAX_TRACE_CHARS + 50))
            )
        },
        mr_diffs=[
            GitLabDiffFile(
                old_path="checkout.py",
                new_path="checkout.py",
                diff=(
                    "- total = subtotal\n"
                    "+ total = subtotal + tax\n"
                    "password=hidden\n"
                    "PRIVATE-TOKEN: diff-token\n"
                    "password = diff-password\n"
                    "secret: diff-secret\n"
                    "api_key: diff-api-key"
                ),
            )
        ],
    )

    updated_run = PipelineContextBuilder(
        gitlab_context_client=context_client,
        token_store=token_store,
        persistence_store=store,
    ).build_for_run(connected_project=project, triage_run=run)

    context = updated_run.context_json
    assert context is not None
    assert context.context_digest == updated_run.context_digest
    assert [job.id for job in context.jobs] == [4001, 4002]
    persisted_context = context.model_dump_json()
    assert "synthetic-value" not in persisted_context
    assert "private-token-value" not in persisted_context
    assert "password-value" not in persisted_context
    assert "secret-value" not in persisted_context
    assert "api-key-value" not in persisted_context
    assert "diff-token" not in persisted_context
    assert "diff-password" not in persisted_context
    assert "diff-secret" not in persisted_context
    assert "diff-api-key" not in persisted_context
    assert len(context.failed_job_traces) == 1
    assert context.failed_job_traces[0].job_id == 4001
    assert "synthetic-value" not in context.failed_job_traces[0].trace_excerpt
    assert "[REDACTED]" in context.failed_job_traces[0].trace_excerpt
    assert context.failed_job_traces[0].truncated is True
    assert len(context.failed_job_traces[0].trace_excerpt) <= MAX_TRACE_CHARS
    assert context.failed_job_traces[0].trace_digest == sha256(
        context_client.traces[4001]
    )
    assert len(context.diffs) == 1
    assert "hidden" not in context.diffs[0].diff_excerpt
    assert "[REDACTED]" in context.diffs[0].diff_excerpt
    assert context_client.calls == [
        ("jobs", project.gitlab_project_id, run.pipeline_id, "project-token"),
        ("trace", project.gitlab_project_id, 4001, "project-token"),
        ("mr_diffs", project.gitlab_project_id, 17, "project-token"),
    ]
    assert store.get_triage_run(run.id) == updated_run


def test_context_builder_bounds_and_redacts_metadata(tmp_path: Path) -> None:
    store = SqliteStore(tmp_path / "triage.sqlite")
    token_store = RecordingProjectTokenStore()
    token_store.tokens["secret-ref:1"] = "project-token"
    project = make_connected_project()
    run = make_triage_run(
        project,
        pipeline_kind="branch",
        report_target=IssueTarget(project_id=project.gitlab_project_id, issue_iid=0),
    )
    store.create_connected_project(project)
    store.create_triage_run(run)
    context_client = FakeGitLabContextClient(
        jobs=[
            GitLabContextJob(
                id=4001,
                name="test PRIVATE-TOKEN: job-token " + ("n" * MAX_METADATA_CHARS),
                status="failed",
                stage="password = stage-password " + ("s" * MAX_METADATA_CHARS),
                web_url=(
                    "https://gitlab.example.com/job/4001?api_key=url-key"
                    + ("u" * MAX_METADATA_CHARS)
                ),
            )
        ],
        traces={4001: "failed"},
        commit_diffs=[
            GitLabDiffFile(
                old_path="old/secret: old-path " + ("o" * MAX_METADATA_CHARS),
                new_path="new/password = new-path " + ("p" * MAX_METADATA_CHARS),
                diff="diff",
            )
        ],
    )

    updated_run = PipelineContextBuilder(
        gitlab_context_client=context_client,
        token_store=token_store,
        persistence_store=store,
    ).build_for_run(connected_project=project, triage_run=run)

    assert updated_run.context_json is not None
    context = updated_run.context_json
    assert len(context.jobs[0].name) <= MAX_METADATA_CHARS
    assert len(context.jobs[0].stage or "") <= MAX_METADATA_CHARS
    assert len(context.jobs[0].web_url or "") <= MAX_METADATA_CHARS
    assert len(context.failed_job_traces[0].job_name) <= MAX_METADATA_CHARS
    assert len(context.diffs[0].old_path) <= MAX_METADATA_CHARS
    assert len(context.diffs[0].new_path) <= MAX_METADATA_CHARS
    persisted_context = context.model_dump_json()
    assert "job-token" not in persisted_context
    assert "stage-password" not in persisted_context
    assert "url-key" not in persisted_context
    assert "old-path" not in persisted_context
    assert "new-path" not in persisted_context
    assert "[REDACTED]" in persisted_context


def test_context_builder_fetches_branch_commit_diff(tmp_path: Path) -> None:
    store = SqliteStore(tmp_path / "triage.sqlite")
    token_store = RecordingProjectTokenStore()
    token_store.tokens["secret-ref:1"] = "project-token"
    project = make_connected_project()
    run = make_triage_run(
        project,
        pipeline_kind="branch",
        report_target=IssueTarget(project_id=project.gitlab_project_id, issue_iid=0),
    )
    store.create_connected_project(project)
    store.create_triage_run(run)
    context_client = FakeGitLabContextClient(
        commit_diffs=[
            GitLabDiffFile(
                old_path="tax.py",
                new_path="tax.py",
                diff="- return 0\n+ return rate",
            )
        ]
    )

    updated_run = PipelineContextBuilder(
        gitlab_context_client=context_client,
        token_store=token_store,
        persistence_store=store,
    ).build_for_run(connected_project=project, triage_run=run)

    assert updated_run.context_json is not None
    assert updated_run.context_json.pipeline_kind == "branch"
    assert updated_run.context_json.diffs[0].new_path == "tax.py"
    assert ("commit_diffs", project.gitlab_project_id, run.sha, "project-token") in (
        context_client.calls
    )


def test_context_builder_keeps_internal_pipeline_report_only(
    tmp_path: Path,
) -> None:
    store = SqliteStore(tmp_path / "triage.sqlite")
    token_store = RecordingProjectTokenStore()
    token_store.tokens["secret-ref:1"] = "project-token"
    project = make_connected_project()
    run = make_triage_run(
        project,
        pipeline_kind="tag",
        report_target=InternalTarget(project_id=project.gitlab_project_id),
    )
    store.create_connected_project(project)
    store.create_triage_run(run)
    context_client = FakeGitLabContextClient()

    updated_run = PipelineContextBuilder(
        gitlab_context_client=context_client,
        token_store=token_store,
        persistence_store=store,
    ).build_for_run(connected_project=project, triage_run=run)

    assert updated_run.context_json is not None
    assert updated_run.context_json.diffs == []
    assert all(call[0] != "mr_diffs" for call in context_client.calls)
    assert all(call[0] != "commit_diffs" for call in context_client.calls)


def test_context_builder_truncates_diff_excerpts(tmp_path: Path) -> None:
    store = SqliteStore(tmp_path / "triage.sqlite")
    token_store = RecordingProjectTokenStore()
    token_store.tokens["secret-ref:1"] = "project-token"
    project = make_connected_project()
    run = make_triage_run(
        project,
        pipeline_kind="branch",
        report_target=IssueTarget(project_id=project.gitlab_project_id, issue_iid=0),
    )
    store.create_connected_project(project)
    store.create_triage_run(run)
    context_client = FakeGitLabContextClient(
        commit_diffs=[
            GitLabDiffFile(
                old_path="large.txt",
                new_path="large.txt",
                diff="y" * (MAX_DIFF_CHARS + 20),
            )
        ]
    )

    updated_run = PipelineContextBuilder(
        gitlab_context_client=context_client,
        token_store=token_store,
        persistence_store=store,
    ).build_for_run(connected_project=project, triage_run=run)

    assert updated_run.context_json is not None
    diff = updated_run.context_json.diffs[0]
    assert diff.truncated is True
    assert len(diff.diff_excerpt) == MAX_DIFF_CHARS


@dataclass
class FakeGitLabContextClient:
    """Read-only mocked GitLab context boundary for tests."""

    jobs: list[GitLabContextJob] = field(
        default_factory=lambda: [
            GitLabContextJob(
                id=4001,
                name="test",
                status="failed",
                stage="test",
                web_url="https://gitlab.example.com/job/4001",
            ),
            GitLabContextJob(
                id=4002,
                name="lint",
                status="success",
                stage="test",
                web_url="https://gitlab.example.com/job/4002",
            ),
        ]
    )
    traces: dict[int, str] = field(default_factory=lambda: {4001: "pytest failed"})
    mr_diffs: list[GitLabDiffFile] = field(default_factory=list)
    commit_diffs: list[GitLabDiffFile] = field(default_factory=list)
    calls: list[tuple[str, int, int | str, str]] = field(default_factory=list)

    def list_pipeline_jobs(
        self,
        *,
        project_id: int,
        pipeline_id: int,
        project_token: str,
    ) -> list[GitLabContextJob]:
        self.calls.append(("jobs", project_id, pipeline_id, project_token))
        return self.jobs

    def get_job_trace(
        self,
        *,
        project_id: int,
        job_id: int,
        project_token: str,
    ) -> str:
        self.calls.append(("trace", project_id, job_id, project_token))
        return self.traces[job_id]

    def list_merge_request_diffs(
        self,
        *,
        project_id: int,
        merge_request_iid: int,
        project_token: str,
    ) -> list[GitLabDiffFile]:
        self.calls.append(("mr_diffs", project_id, merge_request_iid, project_token))
        return self.mr_diffs

    def list_commit_diffs(
        self,
        *,
        project_id: int,
        sha: str,
        project_token: str,
    ) -> list[GitLabDiffFile]:
        self.calls.append(("commit_diffs", project_id, sha, project_token))
        return self.commit_diffs


def make_connected_project() -> ConnectedProject:
    return ConnectedProject(
        id="connected-project-1",
        gitlab_project_id=2002,
        gitlab_project_path="universalamateur1/checkout-service",
        display_name="checkout-service",
        token_ciphertext="secret-ref:1",
        webhook_secret_hash="sha256:webhook-secret",
        action_policy=ProjectActionPolicy(),
        connected_by_gitlab_user_id=1001,
        enabled=True,
        created_at=TEST_TIME,
        updated_at=TEST_TIME,
    )


def make_triage_run(
    project: ConnectedProject,
    *,
    pipeline_kind: PipelineKind,
    report_target: MergeRequestTarget | IssueTarget | InternalTarget,
) -> TriageRun:
    return TriageRun(
        id="run-1",
        connected_project_id=project.id,
        gitlab_project_id=project.gitlab_project_id,
        pipeline_id=9001,
        job_ids=[4001],
        ref="feature/checkout-tax",
        sha="abc123",
        pipeline_kind=pipeline_kind,
        report_target=report_target,
        status="ignored",
        adapter_mode="mock",
        fallback_reason="Spike 5.1 context only; triage not started.",
        input_digest="sha256:webhook",
        created_at=TEST_TIME,
        updated_at=TEST_TIME,
    )


def sha256(value: str) -> str:
    return f"sha256:{hashlib.sha256(value.encode('utf-8')).hexdigest()}"


TEST_TIME = datetime(2026, 5, 4, 12, 0, tzinfo=timezone.utc)
