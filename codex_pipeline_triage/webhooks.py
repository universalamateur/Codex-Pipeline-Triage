"""GitLab webhook intake for fixture-driven pipeline events."""

from __future__ import annotations

import hashlib
import hmac
import json
import secrets
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from codex_pipeline_triage.context import ContextBuildError, PipelineContextBuilder
from codex_pipeline_triage.models import (
    ConnectedProject,
    InternalTarget,
    IssueTarget,
    MergeRequestTarget,
    PipelineKind,
    PipelineMonitor,
    ReportTarget,
    TriageRun,
)
from codex_pipeline_triage.persistence import PersistenceStore
from codex_pipeline_triage.projects import ProjectConnectionError, ProjectConnector
from codex_pipeline_triage.reporting import MockMrReporter

GITLAB_EVENT_HEADER = "Pipeline Hook"
PLANNED_ISSUE_IID = 0


class WebhookError(RuntimeError):
    """Base error for rejected webhook intake."""


class WebhookUnauthorizedError(WebhookError):
    """Raised when webhook authentication fails."""


class WebhookIgnoredError(WebhookError):
    """Raised when a valid webhook should not create a run."""


class WebhookBadRequestError(WebhookError):
    """Raised when a webhook payload cannot be safely interpreted."""


class GitLabPipelineObject(BaseModel):
    """Subset of GitLab pipeline object attributes needed by intake."""

    model_config = ConfigDict(frozen=True, extra="ignore")

    id: int
    status: str
    ref: str = ""
    sha: str = ""
    source: str = ""
    tag: bool = False


class GitLabProjectObject(BaseModel):
    """Subset of GitLab project attributes needed by intake."""

    model_config = ConfigDict(frozen=True, extra="ignore")

    id: int


class GitLabMergeRequestObject(BaseModel):
    """Subset of GitLab merge request attributes needed for target routing."""

    model_config = ConfigDict(frozen=True, extra="ignore")

    iid: int | None = None


class GitLabPipelinePayload(BaseModel):
    """Minimal GitLab Pipeline-event payload schema."""

    model_config = ConfigDict(frozen=True, extra="ignore")

    object_kind: str
    object_attributes: GitLabPipelineObject
    project: GitLabProjectObject
    merge_request: GitLabMergeRequestObject | None = None
    builds: list[dict[str, Any]] = Field(default_factory=list)


@dataclass(frozen=True)
class PipelineClassification:
    """Deterministic routing decision for a verified failed Pipeline event."""

    kind: PipelineKind
    report_target: ReportTarget


@dataclass(frozen=True)
class WebhookIntakeResult:
    """Outcome returned by accepted or ignored webhook intake."""

    status_code: int
    triage_run: TriageRun | None = None


@dataclass(frozen=True)
class GitLabWebhookIntake:
    """Verify GitLab Pipeline-event webhooks and create initial run records."""

    project_connector: ProjectConnector
    persistence_store: PersistenceStore
    context_builder: PipelineContextBuilder | None = None
    mock_mr_reporter: MockMrReporter | None = None

    # pylint: disable-next=too-many-locals,too-many-return-statements,too-many-branches
    async def handle(
        self,
        *,
        connected_project_id: str,
        event_header: str | None,
        token_header: str | None,
        raw_body: bytes,
    ) -> WebhookIntakeResult:
        try:
            connected_project = self.project_connector.get_any_project(
                connected_project_id
            )
        except ProjectConnectionError as exc:
            raise WebhookUnauthorizedError("Connected project was not found") from exc
        if not connected_project.enabled:
            raise WebhookUnauthorizedError("Connected project is disabled")
        if not connected_project.webhook_secret_hash:
            raise WebhookUnauthorizedError("Webhook secret is not configured")
        if event_header != GITLAB_EVENT_HEADER:
            raise WebhookIgnoredError("Webhook event is not a pipeline event")
        if not _verify_webhook_token(
            token_header=token_header,
            expected_hash=connected_project.webhook_secret_hash,
        ):
            raise WebhookUnauthorizedError("Webhook token is invalid")

        payload = _parse_payload(raw_body)
        if payload.project.id != connected_project.gitlab_project_id:
            raise WebhookUnauthorizedError("Webhook project does not match")
        if payload.object_kind != "pipeline":
            raise WebhookIgnoredError("Webhook object is not a pipeline")
        monitor_result = self._handle_monitor_event(
            connected_project=connected_project,
            payload=payload,
        )
        if monitor_result is not None:
            return monitor_result
        if payload.object_attributes.status != "failed":
            return WebhookIntakeResult(status_code=204)
        classification = _classify_pipeline(payload)

        # Pylint does not infer return types from structural Protocols here.
        # pylint: disable-next=assignment-from-no-return
        existing_run = self.persistence_store.get_triage_run_by_pipeline(
            gitlab_project_id=payload.project.id,
            pipeline_id=payload.object_attributes.id,
        )
        if existing_run is not None:
            return await self._handle_existing_run(
                connected_project=connected_project,
                existing_run=existing_run,
            )

        now = datetime.now(tz=timezone.utc)
        triage_run = TriageRun(
            id=f"run-{secrets.token_urlsafe(16)}",
            connected_project_id=connected_project.id,
            gitlab_project_id=payload.project.id,
            pipeline_id=payload.object_attributes.id,
            job_ids=_failed_job_ids(payload.builds),
            ref=payload.object_attributes.ref,
            sha=payload.object_attributes.sha,
            pipeline_kind=classification.kind,
            report_target=classification.report_target,
            status="ignored",
            adapter_mode="mock",
            fallback_reason="Spike 5.1 context only; triage not started.",
            input_digest=_input_digest(raw_body),
            created_at=now,
            updated_at=now,
        )
        # Pylint does not infer return types from structural Protocols here.
        # pylint: disable-next=assignment-from-no-return
        created_run = self.persistence_store.create_triage_run(triage_run)
        if self.context_builder is None:
            return WebhookIntakeResult(status_code=202, triage_run=created_run)

        try:
            context_run = self.context_builder.build_for_run(
                connected_project=connected_project,
                triage_run=created_run,
            )
        except ContextBuildError:
            failed_run = created_run.model_copy(
                update={
                    "status": "failed",
                    "fallback_reason": "Context builder failed.",
                    "updated_at": datetime.now(tz=timezone.utc),
                }
            )
            return WebhookIntakeResult(
                status_code=202,
                triage_run=self.persistence_store.update_triage_run(failed_run),
            )
        if self.mock_mr_reporter is None:
            return WebhookIntakeResult(status_code=202, triage_run=context_run)
        return WebhookIntakeResult(
            status_code=202,
            triage_run=await self.mock_mr_reporter.report_for_run_async(
                connected_project=connected_project,
                triage_run=context_run,
            ),
        )

    async def _handle_existing_run(
        self,
        *,
        connected_project: ConnectedProject,
        existing_run: TriageRun,
    ) -> WebhookIntakeResult:
        if (
            self.mock_mr_reporter is None
            or existing_run.context_json is None
            or existing_run.gitlab_note_ids
        ):
            return WebhookIntakeResult(status_code=204, triage_run=existing_run)

        reported_run = await self.mock_mr_reporter.report_for_run_async(
            connected_project=connected_project,
            triage_run=existing_run,
        )
        if not reported_run.gitlab_note_ids:
            return WebhookIntakeResult(status_code=204, triage_run=reported_run)
        return WebhookIntakeResult(status_code=202, triage_run=reported_run)

    def _handle_monitor_event(
        self,
        *,
        connected_project: ConnectedProject,
        payload: GitLabPipelinePayload,
    ) -> WebhookIntakeResult | None:
        if payload.object_attributes.status not in {"success", "failed"}:
            return None
        monitor = self._find_monitor(payload)
        if monitor is None or self.mock_mr_reporter is None:
            return None
        if monitor.status != "waiting":
            # Pylint does not infer return types from structural Protocols here.
            # pylint: disable-next=assignment-from-no-return
            triage_run = self.persistence_store.get_triage_run(monitor.triage_run_id)
            return WebhookIntakeResult(
                status_code=204,
                triage_run=triage_run,
            )
        triage_run = self.mock_mr_reporter.report_monitor_event(
            connected_project=connected_project,
            monitor=monitor,
            pipeline_id=payload.object_attributes.id,
            pipeline_status=payload.object_attributes.status,
            sha=payload.object_attributes.sha,
        )
        return WebhookIntakeResult(status_code=202, triage_run=triage_run)

    def _find_monitor(
        self,
        payload: GitLabPipelinePayload,
    ) -> PipelineMonitor | None:  # pylint: disable=not-an-iterable
        # Pylint does not infer return types from structural Protocols here.
        # pylint: disable-next=assignment-from-no-return
        monitors = self.persistence_store.list_pipeline_monitors_for_project(
            payload.project.id
        )
        # pylint: disable-next=not-an-iterable
        for monitor in monitors:
            if _monitor_matches_pipeline(monitor, payload):
                return monitor
        return None


def _parse_payload(raw_body: bytes) -> GitLabPipelinePayload:
    try:
        parsed = json.loads(raw_body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise WebhookBadRequestError("Webhook payload was not JSON") from exc
    try:
        return GitLabPipelinePayload.model_validate(parsed)
    except ValidationError as exc:
        raise WebhookBadRequestError("Webhook payload did not match schema") from exc


def _classify_pipeline(payload: GitLabPipelinePayload) -> PipelineClassification:
    source = payload.object_attributes.source.strip()
    project_id = payload.project.id

    if source in {"parent_pipeline", "pipeline"}:
        return PipelineClassification(
            kind="child_or_parent",
            report_target=InternalTarget(project_id=project_id),
        )

    if source == "merge_request_event" or payload.merge_request is not None:
        merge_request_iid = (
            payload.merge_request.iid if payload.merge_request is not None else None
        )
        if merge_request_iid is None:
            return PipelineClassification(
                kind="merge_request",
                report_target=InternalTarget(project_id=project_id),
            )
        return PipelineClassification(
            kind="merge_request",
            report_target=MergeRequestTarget(
                project_id=project_id,
                merge_request_iid=merge_request_iid,
            ),
        )

    if payload.object_attributes.tag:
        return PipelineClassification(
            kind="tag",
            report_target=InternalTarget(project_id=project_id),
        )

    if payload.object_attributes.ref:
        return PipelineClassification(
            kind="branch",
            report_target=IssueTarget(
                project_id=project_id,
                issue_iid=PLANNED_ISSUE_IID,
            ),
        )

    return PipelineClassification(
        kind="unknown",
        report_target=InternalTarget(project_id=project_id),
    )


def _monitor_matches_pipeline(
    monitor: PipelineMonitor,
    payload: GitLabPipelinePayload,
) -> bool:
    pipeline = payload.object_attributes
    if monitor.expected_pipeline_id is not None:
        return monitor.expected_pipeline_id == pipeline.id
    if monitor.expected_ref != pipeline.ref:
        return False
    if monitor.expected_sha is not None and monitor.expected_sha != pipeline.sha:
        return False
    return True


def _verify_webhook_token(token_header: str | None, expected_hash: str) -> bool:
    if not token_header or not expected_hash.startswith("sha256:"):
        return False
    actual_hash = _hash_webhook_token(token_header)
    return hmac.compare_digest(actual_hash, expected_hash)


def _hash_webhook_token(raw_token: str) -> str:
    digest = hashlib.sha256(raw_token.encode("utf-8")).hexdigest()
    return f"sha256:{digest}"


def _input_digest(raw_body: bytes) -> str:
    digest = hashlib.sha256(raw_body).hexdigest()
    return f"sha256:{digest}"


def _failed_job_ids(builds: list[dict[str, Any]]) -> list[int]:
    job_ids: list[int] = []
    for build in builds:
        if build.get("status") != "failed":
            continue
        job_id = build.get("id")
        if isinstance(job_id, int):
            job_ids.append(job_id)
    return job_ids
