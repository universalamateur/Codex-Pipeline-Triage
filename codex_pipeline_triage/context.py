"""Bounded GitLab context builder for failed pipeline runs."""

from __future__ import annotations

import hashlib
import json
import re
import urllib.parse
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Protocol

from pydantic import BaseModel, ConfigDict, ValidationError

from codex_pipeline_triage.gitlab import (
    GlabApiRequest,
    GlabExecutor,
    GlabExecutorError,
    JsonResponse,
)
from codex_pipeline_triage.models import (
    ConnectedProject,
    DiffFileContext,
    JobTraceContext,
    MergeRequestTarget,
    PipelineContext,
    PipelineJobSummary,
    TriageRun,
)
from codex_pipeline_triage.persistence import PersistenceStore
from codex_pipeline_triage.projects import (
    DEFAULT_GLAB_CONFIG_DIR,
    ProjectConnectionError,
    ProjectConnector,
    ProjectTokenSecretStore,
)

MAX_TRACE_CHARS = 4000
MAX_DIFF_CHARS = 6000
MAX_METADATA_CHARS = 240
REDACTED = "[REDACTED]"
_SECRET_PATTERNS = (
    re.compile(r"(?i)\b(authorization\s*:\s*bearer)\s+\S+"),
    re.compile(
        r"(?i)\b([A-Z0-9_.-]*(?:token|password|secret|api[_-]?key|apikey))"
        r"(\s*[:=]\s*)\S+"
    ),
)


class ContextBuildError(RuntimeError):
    """Raised when bounded GitLab context cannot be assembled safely."""


class GitLabContextJob(PipelineJobSummary):
    """GitLab job metadata returned by the context client."""


class GitLabDiffFile(BaseModel):
    """GitLab diff file returned by the context client."""

    model_config = ConfigDict(frozen=True, extra="ignore")

    old_path: str
    new_path: str
    diff: str


class GitLabContextClient(Protocol):
    """Read-only GitLab context boundary."""

    def list_pipeline_jobs(
        self,
        *,
        project_id: int,
        pipeline_id: int,
        project_token: str,
    ) -> list[GitLabContextJob]:
        """Return jobs for one pipeline."""
        raise NotImplementedError

    def get_job_trace(
        self,
        *,
        project_id: int,
        job_id: int,
        project_token: str,
    ) -> str:
        """Return raw trace text for one job."""
        raise NotImplementedError

    def list_merge_request_diffs(
        self,
        *,
        project_id: int,
        merge_request_iid: int,
        project_token: str,
    ) -> list[GitLabDiffFile]:
        """Return diff files for one merge request."""
        raise NotImplementedError

    def list_commit_diffs(
        self,
        *,
        project_id: int,
        sha: str,
        project_token: str,
    ) -> list[GitLabDiffFile]:
        """Return diff files for one commit."""
        raise NotImplementedError


@dataclass(frozen=True)
class GlabGitLabContextClient:
    """Read GitLab context through the deterministic glab executor."""

    executor: GlabExecutor

    def list_pipeline_jobs(
        self,
        *,
        project_id: int,
        pipeline_id: int,
        project_token: str,
    ) -> list[GitLabContextJob]:
        try:
            response = self.executor.api(
                GlabApiRequest(
                    endpoint=f"projects/{project_id}/pipelines/{pipeline_id}/jobs"
                ),
                token=project_token,
            )
        except GlabExecutorError as exc:
            raise ContextBuildError("GitLab pipeline jobs lookup failed") from exc
        return _jobs_from_response(response)

    def get_job_trace(
        self,
        *,
        project_id: int,
        job_id: int,
        project_token: str,
    ) -> str:
        try:
            return self.executor.api_text(
                GlabApiRequest(endpoint=f"projects/{project_id}/jobs/{job_id}/trace"),
                token=project_token,
            )
        except GlabExecutorError as exc:
            raise ContextBuildError("GitLab job trace lookup failed") from exc

    def list_merge_request_diffs(
        self,
        *,
        project_id: int,
        merge_request_iid: int,
        project_token: str,
    ) -> list[GitLabDiffFile]:
        try:
            response = self.executor.api(
                GlabApiRequest(
                    endpoint=(
                        f"projects/{project_id}/merge_requests/"
                        f"{merge_request_iid}/diffs"
                    )
                ),
                token=project_token,
            )
        except GlabExecutorError as exc:
            raise ContextBuildError("GitLab merge request diff lookup failed") from exc
        return _diffs_from_response(response)

    def list_commit_diffs(
        self,
        *,
        project_id: int,
        sha: str,
        project_token: str,
    ) -> list[GitLabDiffFile]:
        quoted_sha = urllib.parse.quote(sha, safe="")
        try:
            response = self.executor.api(
                GlabApiRequest(
                    endpoint=(
                        f"projects/{project_id}/repository/commits/"
                        f"{quoted_sha}/diff"
                    )
                ),
                token=project_token,
            )
        except GlabExecutorError as exc:
            raise ContextBuildError("GitLab commit diff lookup failed") from exc
        return _diffs_from_response(response)


@dataclass(frozen=True)
class PipelineContextBuilder:
    """Build and persist bounded context for one triage run."""

    gitlab_context_client: GitLabContextClient
    token_store: ProjectTokenSecretStore
    persistence_store: PersistenceStore

    def build_for_run(
        self,
        *,
        connected_project: ConnectedProject,
        triage_run: TriageRun,
    ) -> TriageRun:
        try:
            project_token = self.token_store.retrieve_project_token(
                connected_project.token_ciphertext
            )
        except ProjectConnectionError as exc:
            raise ContextBuildError("Project token could not be resolved") from exc

        jobs = self.gitlab_context_client.list_pipeline_jobs(
            project_id=triage_run.gitlab_project_id,
            pipeline_id=triage_run.pipeline_id,
            project_token=project_token,
        )
        failed_jobs = [job for job in jobs if job.status == "failed"]
        failed_traces = [
            self._build_trace_context(
                project_id=triage_run.gitlab_project_id,
                job=job,
                project_token=project_token,
            )
            for job in failed_jobs
        ]
        diffs = self._build_diff_contexts(
            triage_run=triage_run,
            project_token=project_token,
        )

        context_without_digest = PipelineContext(
            project_id=triage_run.gitlab_project_id,
            pipeline_id=triage_run.pipeline_id,
            pipeline_kind=triage_run.pipeline_kind,
            report_target=triage_run.report_target,
            jobs=[_job_summary(job) for job in jobs],
            failed_job_traces=failed_traces,
            diffs=diffs,
            context_digest="sha256:pending",
            created_at=datetime.now(tz=timezone.utc),
        )
        context_digest = _context_digest(context_without_digest)
        context = context_without_digest.model_copy(
            update={"context_digest": context_digest}
        )
        updated_run = triage_run.model_copy(
            update={
                "context_json": context,
                "context_digest": context_digest,
                "updated_at": datetime.now(tz=timezone.utc),
            }
        )
        return self.persistence_store.update_triage_run(updated_run)

    def _build_trace_context(
        self,
        *,
        project_id: int,
        job: GitLabContextJob,
        project_token: str,
    ) -> JobTraceContext:
        raw_trace = self.gitlab_context_client.get_job_trace(
            project_id=project_id,
            job_id=job.id,
            project_token=project_token,
        )
        bounded_trace = _bound_text(raw_trace, MAX_TRACE_CHARS)
        return JobTraceContext(
            job_id=job.id,
            job_name=_bound_metadata(job.name),
            trace_excerpt=bounded_trace.text,
            trace_digest=_digest(raw_trace),
            truncated=bounded_trace.truncated,
        )

    def _build_diff_contexts(
        self,
        *,
        triage_run: TriageRun,
        project_token: str,
    ) -> list[DiffFileContext]:
        if isinstance(triage_run.report_target, MergeRequestTarget):
            diffs = self.gitlab_context_client.list_merge_request_diffs(
                project_id=triage_run.gitlab_project_id,
                merge_request_iid=triage_run.report_target.merge_request_iid,
                project_token=project_token,
            )
        elif triage_run.pipeline_kind == "branch":
            diffs = self.gitlab_context_client.list_commit_diffs(
                project_id=triage_run.gitlab_project_id,
                sha=triage_run.sha,
                project_token=project_token,
            )
        else:
            diffs = []

        return [_diff_context(diff) for diff in diffs]


def build_default_context_builder(
    project_connector: ProjectConnector,
) -> PipelineContextBuilder:
    """Build the default context builder for local app execution."""
    executor = GlabExecutor(
        config_dir=Path(DEFAULT_GLAB_CONFIG_DIR),
        hostname=_hostname_from_base_url(project_connector.settings.gitlab_base_url),
    )
    return PipelineContextBuilder(
        gitlab_context_client=GlabGitLabContextClient(executor=executor),
        token_store=project_connector.token_store,
        persistence_store=project_connector.persistence_store,
    )


def _diff_context(diff: GitLabDiffFile) -> DiffFileContext:
    bounded_diff = _bound_text(diff.diff, MAX_DIFF_CHARS)
    return DiffFileContext(
        old_path=_bound_metadata(diff.old_path),
        new_path=_bound_metadata(diff.new_path),
        diff_excerpt=bounded_diff.text,
        diff_digest=_digest(diff.diff),
        truncated=bounded_diff.truncated,
    )


@dataclass(frozen=True)
class _BoundedText:
    text: str
    truncated: bool


def _jobs_from_response(response: JsonResponse) -> list[GitLabContextJob]:
    if not isinstance(response, list):
        raise ContextBuildError("GitLab pipeline jobs response was not a list")
    try:
        return [GitLabContextJob.model_validate(job) for job in response]
    except ValidationError as exc:
        raise ContextBuildError("GitLab pipeline jobs response was invalid") from exc


def _diffs_from_response(response: JsonResponse) -> list[GitLabDiffFile]:
    if not isinstance(response, list):
        raise ContextBuildError("GitLab diff response was not a list")
    try:
        return [GitLabDiffFile.model_validate(diff) for diff in response]
    except ValidationError as exc:
        raise ContextBuildError("GitLab diff response was invalid") from exc


def _job_summary(job: GitLabContextJob) -> PipelineJobSummary:
    return PipelineJobSummary(
        id=job.id,
        name=_bound_metadata(job.name),
        status=_bound_metadata(job.status),
        stage=_bound_optional_metadata(job.stage),
        web_url=_bound_optional_metadata(job.web_url),
    )


def _bound_metadata(raw_text: str) -> str:
    return _bound_text(raw_text, MAX_METADATA_CHARS).text


def _bound_optional_metadata(raw_text: str | None) -> str | None:
    if raw_text is None:
        return None
    return _bound_metadata(raw_text)


def _bound_text(raw_text: str, max_chars: int) -> _BoundedText:
    redacted = _redact(raw_text)
    truncated = len(redacted) > max_chars
    if truncated:
        return _BoundedText(text=redacted[:max_chars], truncated=True)
    return _BoundedText(text=redacted, truncated=False)


def _redact(raw_text: str) -> str:
    return redact_untrusted_text(raw_text)


def redact_untrusted_text(raw_text: str) -> str:
    """Redact obvious secret-like values from untrusted GitLab text."""
    redacted = raw_text
    for pattern in _SECRET_PATTERNS:
        redacted = pattern.sub(_redact_match, redacted)
    return redacted


def _redact_match(match: re.Match[str]) -> str:
    prefix = match.group(1)
    separator = match.group(2) if len(match.groups()) >= 2 else " "
    if prefix.lower().startswith("authorization"):
        return f"{prefix} {REDACTED}"
    return f"{prefix}{separator}{REDACTED}"


def _digest(raw_text: str) -> str:
    return f"sha256:{hashlib.sha256(raw_text.encode('utf-8')).hexdigest()}"


def _context_digest(context: PipelineContext) -> str:
    payload = context.model_copy(update={"context_digest": ""}).model_dump(mode="json")
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return _digest(encoded)


def _hostname_from_base_url(gitlab_base_url: str) -> str:
    parsed = urllib.parse.urlparse(gitlab_base_url)
    if not parsed.netloc:
        raise ContextBuildError("GitLab base URL is not configured")
    return parsed.netloc
