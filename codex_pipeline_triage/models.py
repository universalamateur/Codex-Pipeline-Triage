"""Persistence and workflow models for Codex Pipeline Triage."""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

PipelineKind = Literal["merge_request", "branch", "tag", "child_or_parent", "unknown"]
RunStatus = Literal[
    "ignored",
    "triaged",
    "posted",
    "actioned",
    "monitoring",
    "completed",
    "failed",
]
AdapterMode = Literal["mock", "codex"]
GitLabAction = Literal[
    "post_mr_note",
    "create_issue",
    "post_issue_note",
    "retry_job",
    "retry_pipeline",
    "create_commit",
    "create_merge_request",
]
PolicyDecision = Literal["allowed", "blocked", "fallback"]
ActionLogStatus = Literal["planned", "started", "completed", "failed", "skipped"]
MonitorStatus = Literal["waiting", "passed", "failed", "timed_out"]


class AppModel(BaseModel):
    """Base model for immutable app records."""

    model_config = ConfigDict(frozen=True)


class AppUser(AppModel):
    """Authenticated app user record."""

    id: str
    gitlab_user_id: int
    gitlab_username: str
    display_name: str | None = None
    created_at: datetime
    updated_at: datetime


class ProjectActionPolicy(AppModel):
    """Project-level policy gate for Codex recommendations."""

    recommend_only: bool = False
    auto_create_issue: bool = True
    auto_retry: bool = False
    auto_create_fix_mr: bool = False
    direct_commit_to_user_branch: bool = False


class MergeRequestTarget(AppModel):
    """Report target for a merge request pipeline."""

    type: Literal["merge_request"] = "merge_request"
    project_id: int
    merge_request_iid: int


class IssueTarget(AppModel):
    """Report target for a branch, tag, or later issue-backed pipeline."""

    type: Literal["issue"] = "issue"
    project_id: int
    issue_iid: int


class InternalTarget(AppModel):
    """Internal-only target when GitLab reporting cannot be planned safely."""

    type: Literal["internal"] = "internal"
    project_id: int


ReportTarget = Annotated[
    MergeRequestTarget | IssueTarget | InternalTarget,
    Field(discriminator="type"),
]


class EvidenceItem(AppModel):
    """Bounded evidence item returned by the triage stage."""

    source: Literal["pipeline", "job_trace", "mr_diff", "commit_diff", "test_report"]
    file: str | None = None
    line: int | None = Field(default=None, ge=0)
    snippet: str = Field(min_length=1, max_length=400)


class TriageResult(AppModel):
    """Schema-validated Codex triage output."""

    root_cause_hypothesis: str = Field(min_length=1, max_length=280)
    category: Literal[
        "test-flake",
        "code-bug",
        "infra",
        "config",
        "dependency",
        "unknown",
    ]
    confidence: float = Field(ge=0, le=1)
    evidence: list[EvidenceItem] = Field(max_length=5)
    retry_safe: bool
    recommended_action: Literal[
        "recommend_only",
        "retry_job",
        "retry_pipeline",
        "create_fix_mr",
    ]
    suggested_fix: str = Field(min_length=1, max_length=800)
    needs_human_review: bool


class FixFileChange(AppModel):
    """One bounded file change proposed by the fixer stage."""

    action: Literal["create", "update"]
    file_path: str = Field(min_length=1, max_length=240)
    content: str = Field(min_length=1, max_length=5000)

    @field_validator("file_path")
    @classmethod
    def validate_file_path(cls, value: str) -> str:
        """Reject paths that could escape a scratch checkout."""
        normalized = value.strip()
        parts = normalized.split("/")
        if (
            normalized.startswith("/")
            or normalized.startswith("-")
            or "://" in normalized
            or any(part in {"", ".", ".."} for part in parts)
        ):
            raise ValueError("fix file path must be repo-relative")
        return normalized


class FixPatch(AppModel):
    """Schema-validated fixer output before deterministic GitLab execution."""

    source_branch: str = Field(min_length=1, max_length=160)
    target_branch: str = Field(min_length=1, max_length=160)
    commit_message: str = Field(min_length=1, max_length=200)
    merge_request_title: str = Field(min_length=1, max_length=200)
    merge_request_description: str = Field(min_length=1, max_length=1000)
    changes: list[FixFileChange] = Field(min_length=1, max_length=1)

    @field_validator("source_branch", "target_branch")
    @classmethod
    def validate_branch(cls, value: str) -> str:
        """Keep branch names bounded and non-option-like for executor calls."""
        normalized = value.strip()
        if (
            not normalized
            or normalized.startswith("-")
            or "://" in normalized
            or normalized.endswith("/")
        ):
            raise ValueError("branch name is not safe")
        return normalized


class PipelineJobSummary(AppModel):
    """Bounded pipeline job metadata fetched from GitLab."""

    id: int
    name: str
    status: str
    stage: str | None = None
    web_url: str | None = None


class JobTraceContext(AppModel):
    """Redacted and truncated failed-job trace excerpt."""

    job_id: int
    job_name: str
    trace_excerpt: str
    trace_digest: str
    truncated: bool


class DiffFileContext(AppModel):
    """Redacted and truncated diff excerpt."""

    old_path: str
    new_path: str
    diff_excerpt: str
    diff_digest: str
    truncated: bool


class PipelineContext(AppModel):
    """Bounded context assembled before Codex triage."""

    project_id: int
    pipeline_id: int
    pipeline_kind: PipelineKind
    report_target: ReportTarget
    jobs: list[PipelineJobSummary]
    failed_job_traces: list[JobTraceContext]
    diffs: list[DiffFileContext]
    context_digest: str
    created_at: datetime


class ActionPlan(AppModel):
    """Policy-checked action selected after triage."""

    action: Literal[
        "recommend_only",
        "retry_job",
        "retry_pipeline",
        "create_issue",
        "create_fix_mr",
    ]
    reason: str
    requires_fixer_agent: bool


class ConnectedProject(AppModel):
    """GitLab project connected to the app through a server-side token boundary."""

    id: str
    gitlab_project_id: int
    gitlab_project_path: str
    display_name: str
    token_ciphertext: str
    webhook_secret_hash: str
    action_policy: ProjectActionPolicy
    connected_by_gitlab_user_id: int
    enabled: bool
    created_at: datetime
    updated_at: datetime


class TriageRun(AppModel):
    """Persisted record for one pipeline triage workflow."""

    id: str
    connected_project_id: str
    gitlab_project_id: int
    pipeline_id: int
    job_ids: list[int]
    ref: str
    sha: str
    pipeline_kind: PipelineKind
    report_target: ReportTarget
    status: RunStatus
    adapter_mode: AdapterMode
    fallback_reason: str | None = None
    input_digest: str
    context_json: PipelineContext | None = None
    context_digest: str | None = None
    triage_json: TriageResult | None = None
    action_plan: ActionPlan | None = None
    gitlab_note_ids: list[int] = Field(default_factory=list)
    issue_iid: int | None = None
    fix_merge_request_iid: int | None = None
    created_at: datetime
    updated_at: datetime


class GitLabActionLog(AppModel):
    """Audit record for one planned or executed GitLab side effect."""

    id: str
    triage_run_id: str
    idempotency_key: str
    action: GitLabAction
    report_target: ReportTarget
    policy_decision: PolicyDecision
    request_digest: str
    external_id: str | None = None
    status: ActionLogStatus
    fallback_reason: str | None = None
    created_at: datetime
    updated_at: datetime


class PipelineMonitor(AppModel):
    """Follow-up monitor state for later pipeline events or polling."""

    id: str
    triage_run_id: str
    gitlab_project_id: int
    expected_ref: str
    expected_sha: str | None = None
    expected_pipeline_id: int | None = None
    report_target: ReportTarget
    status: MonitorStatus
    created_at: datetime
    updated_at: datetime
