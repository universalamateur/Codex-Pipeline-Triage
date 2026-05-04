"""Mock triage and deterministic MR-note reporting."""

# pylint: disable=duplicate-code,too-many-lines

from __future__ import annotations

import asyncio
import hashlib
import os
import secrets
import urllib.parse
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal, Protocol

from pydantic import ValidationError

from codex_pipeline_triage.codex_adapter import (
    DEFAULT_CODEX_MODEL,
    DEFAULT_CODEX_TIMEOUT_SECONDS,
    CodexTriageAdapter,
    CodexTriageOutcome,
    build_default_codex_adapter,
)
from codex_pipeline_triage.context import redact_untrusted_text
from codex_pipeline_triage.gitlab import (
    GlabApiRequest,
    GlabExecutor,
    GlabExecutorError,
    JsonResponse,
)
from codex_pipeline_triage.models import (
    ActionPlan,
    ConnectedProject,
    EvidenceItem,
    GitLabActionLog,
    IssueTarget,
    MergeRequestTarget,
    PipelineContext,
    TriageResult,
    TriageRun,
)
from codex_pipeline_triage.persistence import PersistenceStore
from codex_pipeline_triage.projects import (
    DEFAULT_GLAB_CONFIG_DIR,
    ProjectConnectionError,
    ProjectConnector,
    ProjectTokenSecretStore,
)

MAX_NOTE_SNIPPET_CHARS = 600
MAX_TRIAGE_HYPOTHESIS_CHARS = 280
MAX_TRIAGE_EVIDENCE_CHARS = 400
MOCK_MARKER = "MOCK TRIAGE"
EMPTY_DIFF_EVIDENCE = "Diff excerpt was empty after bounding."
EMPTY_TRACE_EVIDENCE = "Trace excerpt was empty after bounding."
PIPELINE_TRIAGE_MODE_MOCK: Literal["mock"] = "mock"
PIPELINE_TRIAGE_MODE_CODEX: Literal["codex"] = "codex"
MISSING_CODEX_KEY_FALLBACK_REASON = (
    "Codex real mode is configured but OPENAI_API_KEY is missing."
)
MISSING_CODEX_ADAPTER_FALLBACK_REASON = (
    "Codex real mode is configured but the Codex adapter is unavailable."
)
EMPTY_CODEX_TEXT_FALLBACK = "Codex output was empty after redaction."


@dataclass(frozen=True)
class GitLabIssue:
    """Small issue record returned by the deterministic GitLab boundary."""

    iid: int
    title: str


class MockReportingError(RuntimeError):
    """Raised when mock MR reporting cannot complete safely."""


class CodexTriageProvider(Protocol):
    """Server-side Codex triage boundary used by real reporting mode."""

    async def triage(self, context: PipelineContext) -> CodexTriageOutcome:
        """Return schema-validated Codex triage or visible fallback."""
        raise NotImplementedError


class GitLabMrNoteClient(Protocol):
    """Deterministic boundary for posting MR notes."""

    def post_merge_request_note(
        self,
        *,
        project_id: int,
        merge_request_iid: int,
        body: str,
        project_token: str,
    ) -> int:
        """Post one MR note and return the GitLab note ID."""
        raise NotImplementedError


class GitLabIssueClient(Protocol):
    """Deterministic boundary for branch issue reporting."""

    def find_open_issue(
        self,
        *,
        project_id: int,
        title: str,
        project_token: str,
    ) -> GitLabIssue | None:
        """Return an open issue with the exact title when one exists."""
        raise NotImplementedError

    def create_issue(
        self,
        *,
        project_id: int,
        title: str,
        description: str,
        project_token: str,
    ) -> int:
        """Create one GitLab issue and return its IID."""
        raise NotImplementedError

    def post_issue_note(
        self,
        *,
        project_id: int,
        issue_iid: int,
        body: str,
        project_token: str,
    ) -> int:
        """Post one issue note and return the GitLab note ID."""
        raise NotImplementedError


@dataclass(frozen=True)
class GlabGitLabMrNoteClient:
    """Post MR notes through the deterministic glab executor."""

    executor: GlabExecutor

    def post_merge_request_note(
        self,
        *,
        project_id: int,
        merge_request_iid: int,
        body: str,
        project_token: str,
    ) -> int:
        try:
            response = self.executor.api(
                GlabApiRequest(
                    endpoint=(
                        f"projects/{project_id}/merge_requests/"
                        f"{merge_request_iid}/notes"
                    ),
                    method="POST",
                    fields={"body": body},
                ),
                token=project_token,
            )
        except GlabExecutorError as exc:
            raise MockReportingError("GitLab MR note post failed") from exc
        return _note_id_from_response(response)


@dataclass(frozen=True)
class GlabGitLabIssueClient:
    """Create/reuse issues and post issue notes through glab."""

    executor: GlabExecutor

    def find_open_issue(
        self,
        *,
        project_id: int,
        title: str,
        project_token: str,
    ) -> GitLabIssue | None:
        query = urllib.parse.urlencode(
            {
                "state": "opened",
                "search": title,
                "in": "title",
            }
        )
        try:
            response = self.executor.api(
                GlabApiRequest(endpoint=f"projects/{project_id}/issues?{query}"),
                token=project_token,
            )
        except GlabExecutorError as exc:
            raise MockReportingError("GitLab issue lookup failed") from exc
        return _exact_issue_from_response(response, title=title)

    def create_issue(
        self,
        *,
        project_id: int,
        title: str,
        description: str,
        project_token: str,
    ) -> int:
        try:
            response = self.executor.api(
                GlabApiRequest(
                    endpoint=f"projects/{project_id}/issues",
                    method="POST",
                    fields={
                        "title": title,
                        "description": description,
                    },
                ),
                token=project_token,
            )
        except GlabExecutorError as exc:
            raise MockReportingError("GitLab issue creation failed") from exc
        return _issue_iid_from_response(response)

    def post_issue_note(
        self,
        *,
        project_id: int,
        issue_iid: int,
        body: str,
        project_token: str,
    ) -> int:
        try:
            response = self.executor.api(
                GlabApiRequest(
                    endpoint=f"projects/{project_id}/issues/{issue_iid}/notes",
                    method="POST",
                    fields={"body": body},
                ),
                token=project_token,
            )
        except GlabExecutorError as exc:
            raise MockReportingError("GitLab issue note post failed") from exc
        return _note_id_from_response(response)


@dataclass(frozen=True)
class TriageModeSettings:
    """Runtime switch for mock or real Codex triage."""

    triage_mode: Literal["mock", "codex"] = PIPELINE_TRIAGE_MODE_MOCK
    codex_model: str = DEFAULT_CODEX_MODEL
    codex_timeout_seconds: float = DEFAULT_CODEX_TIMEOUT_SECONDS
    codex_bin: Path | None = None
    openai_api_key: str = field(default="", repr=False)

    @classmethod
    def from_env(cls) -> "TriageModeSettings":
        """Load triage-mode settings from process environment."""
        mode = os.environ.get("PIPELINE_TRIAGE_MODE", PIPELINE_TRIAGE_MODE_MOCK)
        codex_bin = os.environ.get("PIPELINE_TRIAGE_CODEX_BIN", "")
        return cls(
            triage_mode=_triage_mode_from_env(mode),
            codex_model=(
                os.environ.get("PIPELINE_TRIAGE_CODEX_MODEL") or DEFAULT_CODEX_MODEL
            ),
            codex_timeout_seconds=_float_from_env(
                os.environ.get("PIPELINE_TRIAGE_CODEX_TIMEOUT_SECONDS"),
                DEFAULT_CODEX_TIMEOUT_SECONDS,
            ),
            codex_bin=Path(codex_bin) if codex_bin else None,
            openai_api_key=os.environ.get("OPENAI_API_KEY", ""),
        )

    @property
    def has_codex_key(self) -> bool:
        """Return whether real Codex mode has the documented API key."""
        return bool(self.openai_api_key)


@dataclass(frozen=True)
class MockTriagePlan:
    """Persisted triage state prepared before GitLab side effects."""

    triage_run: TriageRun
    triage_result: TriageResult
    action_plan: ActionPlan


@dataclass(frozen=True)
class MockMrReporter:
    """Create triage and report to GitLab MR or branch issue targets."""

    mr_note_client: GitLabMrNoteClient
    token_store: ProjectTokenSecretStore
    persistence_store: PersistenceStore
    issue_client: GitLabIssueClient | None = None
    triage_mode: Literal["mock", "codex"] = PIPELINE_TRIAGE_MODE_MOCK
    codex_adapter: CodexTriageProvider | None = None
    codex_unavailable_reason: str | None = None

    def report_for_run(
        self,
        *,
        connected_project: ConnectedProject,
        triage_run: TriageRun,
    ) -> TriageRun:
        return asyncio.run(
            self.report_for_run_async(
                connected_project=connected_project,
                triage_run=triage_run,
            )
        )

    async def report_for_run_async(
        self,
        *,
        connected_project: ConnectedProject,
        triage_run: TriageRun,
    ) -> TriageRun:
        if isinstance(triage_run.report_target, MergeRequestTarget):
            return await self._report_mr_for_run(
                connected_project=connected_project,
                triage_run=triage_run,
            )
        if (
            isinstance(triage_run.report_target, IssueTarget)
            and triage_run.pipeline_kind == "branch"
        ):
            return await self._report_branch_issue_for_run(
                connected_project=connected_project,
                triage_run=triage_run,
            )
        return triage_run

    async def _report_mr_for_run(  # pylint: disable=too-many-return-statements
        self,
        *,
        connected_project: ConnectedProject,
        triage_run: TriageRun,
    ) -> TriageRun:
        report_target = triage_run.report_target
        if not isinstance(report_target, MergeRequestTarget):
            return triage_run
        if triage_run.context_json is None:
            return self._mark_failed(triage_run, "Mock reporting requires context.")
        if triage_run.gitlab_note_ids:
            return triage_run
        if self._existing_mr_note_log(triage_run) is not None:
            return triage_run

        try:
            plan = await self._prepare_triage(
                triage_run=triage_run,
                action="recommend_only",
                reason="Spike 5.2 mock triage is report-only.",
                fallback_reason="Spike 5.2 deterministic mock triage.",
            )
        except MockReportingError as exc:
            return self._mark_failed(triage_run, str(exc))
        note_body = render_mr_note(
            triage_run=plan.triage_run,
            triage_result=plan.triage_result,
            action_plan=plan.action_plan,
        )
        now = datetime.now(tz=timezone.utc)
        action_log = GitLabActionLog(
            id=f"action-{secrets.token_urlsafe(16)}",
            triage_run_id=plan.triage_run.id,
            idempotency_key=_mr_note_idempotency_key(plan.triage_run),
            action="post_mr_note",
            report_target=report_target,
            policy_decision="allowed",
            request_digest=_request_digest(note_body),
            status="planned",
            created_at=now,
            updated_at=now,
        )
        # Pylint does not infer return types from structural Protocols here.
        # pylint: disable-next=assignment-from-no-return
        action_log = self.persistence_store.create_action_log(action_log)

        try:
            project_token = self.token_store.retrieve_project_token(
                connected_project.token_ciphertext
            )
            note_id = self.mr_note_client.post_merge_request_note(
                project_id=plan.triage_run.gitlab_project_id,
                merge_request_iid=report_target.merge_request_iid,
                body=note_body,
                project_token=project_token,
            )
        except (MockReportingError, ProjectConnectionError):
            return self._fail_after_action_log(
                triage_run=plan.triage_run,
                action_log=action_log,
                fallback_reason="MR note post failed.",
            )

        completed_log = action_log.model_copy(
            update={
                "status": "completed",
                "external_id": str(note_id),
                "updated_at": datetime.now(tz=timezone.utc),
            }
        )
        self.persistence_store.update_action_log(completed_log)
        posted_run = plan.triage_run.model_copy(
            update={
                "status": "posted",
                "gitlab_note_ids": [*plan.triage_run.gitlab_note_ids, note_id],
                "updated_at": datetime.now(tz=timezone.utc),
            }
        )
        return self.persistence_store.update_triage_run(posted_run)

    # pylint: disable-next=too-many-return-statements
    async def _report_branch_issue_for_run(
        self,
        *,
        connected_project: ConnectedProject,
        triage_run: TriageRun,
    ) -> TriageRun:
        if triage_run.context_json is None:
            return self._mark_failed(triage_run, "Mock reporting requires context.")
        if triage_run.gitlab_note_ids:
            return triage_run
        if self._existing_issue_note_log(triage_run) is not None:
            return triage_run
        if self._existing_create_issue_log(triage_run) is not None:
            return triage_run

        if not _branch_issue_policy_allows(connected_project):
            try:
                plan = await self._prepare_triage(
                    triage_run=triage_run,
                    action="recommend_only",
                    reason="Project policy blocks branch issue reporting.",
                    fallback_reason="Branch issue reporting blocked by project policy.",
                )
            except MockReportingError as exc:
                return self._mark_failed(triage_run, str(exc))
            return self._record_policy_blocked_issue(plan.triage_run)

        if self.issue_client is None:
            return self._mark_failed(triage_run, "Issue reporting is not configured.")

        failed_run_target = triage_run
        try:
            plan = await self._prepare_triage(
                triage_run=triage_run,
                action="create_issue",
                reason="Spike 5.3 branch issue reporting is report-only.",
                fallback_reason="Spike 5.3 deterministic branch issue reporting.",
            )
            failed_run_target = plan.triage_run
            project_token = self.token_store.retrieve_project_token(
                connected_project.token_ciphertext
            )
            issue_iid = self._ensure_branch_issue(
                triage_run=plan.triage_run,
                project_token=project_token,
            )
        except (MockReportingError, ProjectConnectionError) as exc:
            return self._mark_failed(failed_run_target, str(exc))

        issue_run = plan.triage_run.model_copy(
            update={
                "report_target": IssueTarget(
                    project_id=plan.triage_run.gitlab_project_id,
                    issue_iid=issue_iid,
                ),
                "issue_iid": issue_iid,
                "updated_at": datetime.now(tz=timezone.utc),
            }
        )
        # Pylint does not infer return types from structural Protocols here.
        # pylint: disable-next=assignment-from-no-return
        issue_run = self.persistence_store.update_triage_run(issue_run)
        note_body = render_issue_note(
            triage_run=issue_run,
            triage_result=plan.triage_result,
            action_plan=plan.action_plan,
        )
        now = datetime.now(tz=timezone.utc)
        action_log = GitLabActionLog(
            id=f"action-{secrets.token_urlsafe(16)}",
            triage_run_id=issue_run.id,
            idempotency_key=_issue_note_idempotency_key(issue_run),
            action="post_issue_note",
            report_target=issue_run.report_target,
            policy_decision="allowed",
            request_digest=_request_digest(note_body),
            status="planned",
            created_at=now,
            updated_at=now,
        )
        # Pylint does not infer return types from structural Protocols here.
        # pylint: disable-next=assignment-from-no-return
        action_log = self.persistence_store.create_action_log(action_log)
        try:
            note_id = self.issue_client.post_issue_note(
                project_id=issue_run.gitlab_project_id,
                issue_iid=issue_iid,
                body=note_body,
                project_token=project_token,
            )
        except MockReportingError:
            return self._fail_after_action_log(
                triage_run=issue_run,
                action_log=action_log,
                fallback_reason="Issue note post failed.",
            )

        completed_log = action_log.model_copy(
            update={
                "status": "completed",
                "external_id": str(note_id),
                "updated_at": datetime.now(tz=timezone.utc),
            }
        )
        self.persistence_store.update_action_log(completed_log)
        posted_run = issue_run.model_copy(
            update={
                "status": "posted",
                "gitlab_note_ids": [*issue_run.gitlab_note_ids, note_id],
                "updated_at": datetime.now(tz=timezone.utc),
            }
        )
        return self.persistence_store.update_triage_run(posted_run)

    def _record_policy_blocked_issue(self, triage_run: TriageRun) -> TriageRun:
        now = datetime.now(tz=timezone.utc)
        fallback_reason = "Branch issue reporting blocked by project policy."
        action_log = GitLabActionLog(
            id=f"action-{secrets.token_urlsafe(16)}",
            triage_run_id=triage_run.id,
            idempotency_key=_create_issue_idempotency_key(triage_run),
            action="create_issue",
            report_target=triage_run.report_target,
            policy_decision="blocked",
            request_digest=_request_digest(fallback_reason),
            status="skipped",
            fallback_reason=fallback_reason,
            created_at=now,
            updated_at=now,
        )
        # Pylint does not infer return types from structural Protocols here.
        # pylint: disable-next=assignment-from-no-return
        self.persistence_store.create_action_log(action_log)
        return triage_run

    async def _prepare_triage(
        self,
        *,
        triage_run: TriageRun,
        action: Literal["recommend_only", "create_issue"],
        reason: str,
        fallback_reason: str,
    ) -> MockTriagePlan:
        if self.triage_mode == PIPELINE_TRIAGE_MODE_CODEX:
            return await self._prepare_codex_triage(
                triage_run=triage_run,
                action=action,
                reason=reason,
            )
        return self._prepare_mock_triage(
            triage_run=triage_run,
            action=action,
            reason=reason,
            fallback_reason=fallback_reason,
        )

    async def _prepare_codex_triage(
        self,
        *,
        triage_run: TriageRun,
        action: Literal["recommend_only", "create_issue"],
        reason: str,
    ) -> MockTriagePlan:
        if triage_run.context_json is None:
            raise MockReportingError("Codex reporting requires context.")

        if self.codex_unavailable_reason is not None or self.codex_adapter is None:
            fallback_reason = (
                self.codex_unavailable_reason or MISSING_CODEX_ADAPTER_FALLBACK_REASON
            )
            outcome = CodexTriageOutcome(
                adapter_mode="mock",
                triage_result=build_mock_triage(triage_run.context_json),
                fallback_reason=fallback_reason,
            )
        else:
            outcome = await self.codex_adapter.triage(triage_run.context_json)

        triage_result = sanitize_triage_result(outcome.triage_result)
        action_plan = ActionPlan(
            action=action,
            reason=_real_mode_action_reason(action=action, default_reason=reason),
            requires_fixer_agent=False,
        )
        planned_run = triage_run.model_copy(
            update={
                "status": "triaged",
                "adapter_mode": outcome.adapter_mode,
                "fallback_reason": outcome.fallback_reason,
                "triage_json": triage_result,
                "action_plan": action_plan,
                "updated_at": datetime.now(tz=timezone.utc),
            }
        )
        # Pylint does not infer return types from structural Protocols here.
        # pylint: disable-next=assignment-from-no-return
        persisted_run = self.persistence_store.update_triage_run(planned_run)
        return MockTriagePlan(
            triage_run=persisted_run,
            triage_result=triage_result,
            action_plan=action_plan,
        )

    def _prepare_mock_triage(
        self,
        *,
        triage_run: TriageRun,
        action: Literal["recommend_only", "create_issue"],
        reason: str,
        fallback_reason: str,
    ) -> MockTriagePlan:
        if triage_run.context_json is None:
            raise MockReportingError("Mock reporting requires context.")
        try:
            triage_result = build_mock_triage(triage_run.context_json)
        except ValidationError as exc:
            raise MockReportingError("Mock triage validation failed.") from exc
        action_plan = ActionPlan(
            action=action,
            reason=reason,
            requires_fixer_agent=False,
        )
        planned_run = triage_run.model_copy(
            update={
                "status": "triaged",
                "adapter_mode": "mock",
                "fallback_reason": fallback_reason,
                "triage_json": triage_result,
                "action_plan": action_plan,
                "updated_at": datetime.now(tz=timezone.utc),
            }
        )
        # Pylint does not infer return types from structural Protocols here.
        # pylint: disable-next=assignment-from-no-return
        persisted_run = self.persistence_store.update_triage_run(planned_run)
        return MockTriagePlan(
            triage_run=persisted_run,
            triage_result=triage_result,
            action_plan=action_plan,
        )

    def _ensure_branch_issue(
        self,
        *,
        triage_run: TriageRun,
        project_token: str,
    ) -> int:
        if self.issue_client is None:
            raise MockReportingError("Issue reporting is not configured.")
        title = build_branch_issue_title(triage_run)
        existing_issue = self.issue_client.find_open_issue(
            project_id=triage_run.gitlab_project_id,
            title=title,
            project_token=project_token,
        )
        if existing_issue is not None:
            return existing_issue.iid

        description = render_issue_description(triage_run=triage_run, title=title)
        now = datetime.now(tz=timezone.utc)
        action_log = GitLabActionLog(
            id=f"action-{secrets.token_urlsafe(16)}",
            triage_run_id=triage_run.id,
            idempotency_key=_create_issue_idempotency_key(triage_run),
            action="create_issue",
            report_target=IssueTarget(
                project_id=triage_run.gitlab_project_id,
                issue_iid=0,
            ),
            policy_decision="allowed",
            request_digest=_request_digest(f"{title}\n{description}"),
            status="planned",
            created_at=now,
            updated_at=now,
        )
        # Pylint does not infer return types from structural Protocols here.
        # pylint: disable-next=assignment-from-no-return
        action_log = self.persistence_store.create_action_log(action_log)
        try:
            issue_iid = self.issue_client.create_issue(
                project_id=triage_run.gitlab_project_id,
                title=title,
                description=description,
                project_token=project_token,
            )
        except MockReportingError:
            self.persistence_store.update_action_log(
                action_log.model_copy(
                    update={
                        "status": "failed",
                        "fallback_reason": "Issue creation failed.",
                        "updated_at": datetime.now(tz=timezone.utc),
                    }
                )
            )
            raise
        self.persistence_store.update_action_log(
            action_log.model_copy(
                update={
                    "status": "completed",
                    "external_id": str(issue_iid),
                    "updated_at": datetime.now(tz=timezone.utc),
                }
            )
        )
        return issue_iid

    def _existing_mr_note_log(
        self,
        triage_run: TriageRun,
    ) -> GitLabActionLog | None:  # pylint: disable=not-an-iterable
        # Pylint does not infer return types from structural Protocols here.
        # pylint: disable-next=assignment-from-no-return
        action_logs = self.persistence_store.list_action_logs_for_run(triage_run.id)
        # pylint: disable-next=not-an-iterable
        for action_log in action_logs:
            if action_log.idempotency_key == _mr_note_idempotency_key(triage_run):
                return action_log
        return None

    def _existing_issue_note_log(
        self,
        triage_run: TriageRun,
    ) -> GitLabActionLog | None:  # pylint: disable=not-an-iterable
        # Pylint does not infer return types from structural Protocols here.
        # pylint: disable-next=assignment-from-no-return
        action_logs = self.persistence_store.list_action_logs_for_run(triage_run.id)
        # pylint: disable-next=not-an-iterable
        for action_log in action_logs:
            if action_log.idempotency_key == _issue_note_idempotency_key(triage_run):
                return action_log
        return None

    def _existing_create_issue_log(
        self,
        triage_run: TriageRun,
    ) -> GitLabActionLog | None:  # pylint: disable=not-an-iterable
        # Pylint does not infer return types from structural Protocols here.
        # pylint: disable-next=assignment-from-no-return
        action_logs = self.persistence_store.list_action_logs_for_run(triage_run.id)
        # pylint: disable-next=not-an-iterable
        for action_log in action_logs:
            if action_log.idempotency_key == _create_issue_idempotency_key(triage_run):
                return action_log
        return None

    def _mark_failed(self, triage_run: TriageRun, fallback_reason: str) -> TriageRun:
        return self.persistence_store.update_triage_run(
            triage_run.model_copy(
                update={
                    "status": "failed",
                    "fallback_reason": fallback_reason,
                    "updated_at": datetime.now(tz=timezone.utc),
                }
            )
        )

    def _fail_after_action_log(
        self,
        *,
        triage_run: TriageRun,
        action_log: GitLabActionLog,
        fallback_reason: str,
    ) -> TriageRun:
        failed_log = action_log.model_copy(
            update={
                "status": "failed",
                "fallback_reason": fallback_reason,
                "updated_at": datetime.now(tz=timezone.utc),
            }
        )
        self.persistence_store.update_action_log(failed_log)
        failed_run = triage_run.model_copy(
            update={
                "status": "failed",
                "fallback_reason": fallback_reason,
                "updated_at": datetime.now(tz=timezone.utc),
            }
        )
        return self.persistence_store.update_triage_run(failed_run)


def build_mock_triage(context: PipelineContext) -> TriageResult:
    """Build deterministic mock triage from bounded context."""
    failed_trace = context.failed_job_traces[0] if context.failed_job_traces else None
    diff = context.diffs[0] if context.diffs else None
    job_name = _note_text(
        failed_trace.job_name if failed_trace is not None else "unknown job",
        max_chars=120,
        fallback="unknown job",
    )
    evidence: list[EvidenceItem] = []
    if failed_trace is not None:
        evidence.append(
            EvidenceItem(
                source="job_trace",
                snippet=_note_text(
                    failed_trace.trace_excerpt,
                    max_chars=MAX_TRIAGE_EVIDENCE_CHARS,
                    fallback=EMPTY_TRACE_EVIDENCE,
                ),
            )
        )
    if diff is not None:
        evidence.append(
            EvidenceItem(
                source=(
                    "mr_diff"
                    if context.pipeline_kind == "merge_request"
                    else "commit_diff"
                ),
                file=diff.new_path,
                snippet=_note_text(
                    diff.diff_excerpt,
                    max_chars=MAX_TRIAGE_EVIDENCE_CHARS,
                    fallback=EMPTY_DIFF_EVIDENCE,
                ),
            )
        )
    if not evidence:
        evidence.append(
            EvidenceItem(
                source="pipeline",
                snippet=f"Pipeline {context.pipeline_id} failed.",
            )
        )

    return TriageResult(
        root_cause_hypothesis=_note_text(
            (
                f"[{MOCK_MARKER}] Pipeline {context.pipeline_id} failed in "
                f"{job_name}; inspect the bounded job trace and diff context."
            ),
            max_chars=MAX_TRIAGE_HYPOTHESIS_CHARS,
            fallback=f"[{MOCK_MARKER}] Pipeline {context.pipeline_id} failed.",
        ),
        category="unknown",
        confidence=0.42,
        evidence=evidence[:5],
        retry_safe=False,
        recommended_action="recommend_only",
        suggested_fix=(
            "This deterministic mock result is report-only. Review the "
            "failed job trace and MR diff before making changes."
        ),
        needs_human_review=True,
    )


def sanitize_triage_result(triage_result: TriageResult) -> TriageResult:
    """Redact and bound schema-valid triage output before persistence/rendering."""
    evidence = [
        EvidenceItem(
            source=item.source,
            file=(
                _note_text(item.file, max_chars=160) if item.file is not None else None
            ),
            line=item.line,
            snippet=_note_text(
                item.snippet,
                max_chars=MAX_TRIAGE_EVIDENCE_CHARS,
                fallback=EMPTY_CODEX_TEXT_FALLBACK,
            ),
        )
        for item in triage_result.evidence[:5]
    ]
    return TriageResult(
        root_cause_hypothesis=_note_text(
            triage_result.root_cause_hypothesis,
            max_chars=MAX_TRIAGE_HYPOTHESIS_CHARS,
            fallback=EMPTY_CODEX_TEXT_FALLBACK,
        ),
        category=triage_result.category,
        confidence=triage_result.confidence,
        evidence=evidence,
        retry_safe=triage_result.retry_safe,
        recommended_action=triage_result.recommended_action,
        suggested_fix=_note_text(
            triage_result.suggested_fix,
            max_chars=800,
            fallback=EMPTY_CODEX_TEXT_FALLBACK,
        ),
        needs_human_review=triage_result.needs_human_review,
    )


def render_mr_note(
    *,
    triage_run: TriageRun,
    triage_result: TriageResult,
    action_plan: ActionPlan,
) -> str:
    """Render a bounded MR note body with visible adapter mode."""
    evidence_lines = "\n".join(
        f"- `{item.source}`"
        f"{f' in `{_note_text(item.file, max_chars=160)}`' if item.file else ''}: "
        f"{_inline_code(item.snippet)}"
        for item in triage_result.evidence
    )
    fallback_reason = (
        _note_text(triage_run.fallback_reason, max_chars=200)
        if triage_run.fallback_reason is not None
        else None
    )
    header, adapter_marker = _note_adapter_marker(triage_run)
    fallback_line = (
        [f"**Fallback reason:** `{fallback_reason}`"] if fallback_reason else []
    )
    return "\n".join(
        [
            header,
            "",
            adapter_marker,
            "",
            f"**Pipeline:** `{triage_run.pipeline_id}`",
            f"**Ref:** `{_note_text(triage_run.ref, max_chars=160)}`",
            f"**SHA:** `{_note_text(triage_run.sha, max_chars=80)}`",
            f"**Confidence:** `{triage_result.confidence:.2f}`",
            f"**Recommended action:** `{triage_result.recommended_action}`",
            f"**Policy action:** `{action_plan.action}`",
            "",
            "**Hypothesis**",
            "",
            _note_text(triage_result.root_cause_hypothesis, max_chars=400),
            "",
            "**Evidence**",
            "",
            evidence_lines,
            "",
            "**Suggested fix**",
            "",
            _note_text(triage_result.suggested_fix, max_chars=800),
            "",
            *fallback_line,
        ]
    )


def render_issue_note(
    *,
    triage_run: TriageRun,
    triage_result: TriageResult,
    action_plan: ActionPlan,
) -> str:
    """Render a bounded branch issue note body."""
    return render_mr_note(
        triage_run=triage_run,
        triage_result=triage_result,
        action_plan=action_plan,
    )


def build_branch_issue_title(triage_run: TriageRun) -> str:
    """Build the deterministic branch issue title from run identity."""
    ref = _note_text(triage_run.ref, max_chars=120, fallback="unknown-ref").replace(
        "\n",
        " ",
    )
    short_sha = _note_text(triage_run.sha[:8], max_chars=12, fallback="unknown")
    return _note_text(
        f"Pipeline failed on {ref} at {short_sha}",
        max_chars=200,
        fallback=f"Pipeline {triage_run.pipeline_id} failed",
    )


def render_issue_description(*, triage_run: TriageRun, title: str) -> str:
    """Render the bounded deterministic issue description."""
    branch_reporting_mode = (
        "real Codex branch reporting"
        if triage_run.adapter_mode == PIPELINE_TRIAGE_MODE_CODEX
        and triage_run.fallback_reason is None
        else "mock branch reporting"
    )
    return "\n".join(
        [
            title,
            "",
            f"Created by Codex Pipeline Triage {branch_reporting_mode}.",
            "",
            f"Pipeline: `{triage_run.pipeline_id}`",
            f"Ref: `{_note_text(triage_run.ref, max_chars=160)}`",
            f"SHA: `{_note_text(triage_run.sha, max_chars=80)}`",
            f"Adapter mode: `{triage_run.adapter_mode}`",
        ]
    )


def _branch_issue_policy_allows(connected_project: ConnectedProject) -> bool:
    policy = connected_project.action_policy
    return policy.auto_create_issue and not policy.recommend_only


def build_default_mock_mr_reporter(
    project_connector: ProjectConnector,
) -> MockMrReporter:
    """Build the default reporter for local app execution."""
    triage_settings = TriageModeSettings.from_env()
    executor = GlabExecutor(
        config_dir=Path(DEFAULT_GLAB_CONFIG_DIR),
        hostname=_hostname_from_base_url(project_connector.settings.gitlab_base_url),
    )
    codex_adapter = _build_codex_adapter_from_settings(triage_settings)
    codex_unavailable_reason = _codex_unavailable_reason(triage_settings)
    return MockMrReporter(
        mr_note_client=GlabGitLabMrNoteClient(executor=executor),
        issue_client=GlabGitLabIssueClient(executor=executor),
        token_store=project_connector.token_store,
        persistence_store=project_connector.persistence_store,
        triage_mode=triage_settings.triage_mode,
        codex_adapter=codex_adapter,
        codex_unavailable_reason=codex_unavailable_reason,
    )


def _build_codex_adapter_from_settings(
    settings: TriageModeSettings,
) -> CodexTriageAdapter | None:
    if settings.triage_mode != PIPELINE_TRIAGE_MODE_CODEX:
        return None
    if not settings.has_codex_key:
        return None
    return build_default_codex_adapter(
        model=settings.codex_model,
        timeout_seconds=settings.codex_timeout_seconds,
        codex_bin=settings.codex_bin,
    )


def _codex_unavailable_reason(settings: TriageModeSettings) -> str | None:
    if settings.triage_mode != PIPELINE_TRIAGE_MODE_CODEX:
        return None
    if not settings.has_codex_key:
        return MISSING_CODEX_KEY_FALLBACK_REASON
    return None


def _triage_mode_from_env(value: str) -> Literal["mock", "codex"]:
    if value.strip().lower() == PIPELINE_TRIAGE_MODE_CODEX:
        return PIPELINE_TRIAGE_MODE_CODEX
    return PIPELINE_TRIAGE_MODE_MOCK


def _float_from_env(value: str | None, default: float) -> float:
    if value is None or not value.strip():
        return default
    try:
        return float(value)
    except ValueError:
        return default


def _note_adapter_marker(triage_run: TriageRun) -> tuple[str, str]:
    if (
        triage_run.adapter_mode == PIPELINE_TRIAGE_MODE_CODEX
        and triage_run.fallback_reason is None
    ):
        return (
            "### Codex Pipeline Triage (CODEX)",
            "**Codex path marker:** real Codex SDK triage ran server-side.",
        )
    return (
        "### Codex Pipeline Triage (MOCK)",
        (
            "**Mock/fallback marker:** deterministic mock triage or visible "
            "fallback; real Codex triage did not produce accepted output."
        ),
    )


def _real_mode_action_reason(
    *,
    action: Literal["recommend_only", "create_issue"],
    default_reason: str,
) -> str:
    if default_reason.startswith("Project policy"):
        return default_reason
    if action == "create_issue":
        return "Spike 6.2 real Codex branch issue reporting is report-only."
    return "Spike 6.2 real Codex triage is report-only."


def _note_id_from_response(response: JsonResponse) -> int:
    if not isinstance(response, dict):
        raise MockReportingError("GitLab MR note response was not an object")
    note_id = response.get("id")
    if not isinstance(note_id, int):
        raise MockReportingError("GitLab MR note response omitted note ID")
    return note_id


def _issue_iid_from_response(response: JsonResponse) -> int:
    if not isinstance(response, dict):
        raise MockReportingError("GitLab issue response was not an object")
    issue_iid = response.get("iid")
    if not isinstance(issue_iid, int):
        raise MockReportingError("GitLab issue response omitted issue IID")
    return issue_iid


def _exact_issue_from_response(
    response: JsonResponse,
    *,
    title: str,
) -> GitLabIssue | None:
    if not isinstance(response, list):
        raise MockReportingError("GitLab issue lookup response was not a list")
    for item in response:
        if not isinstance(item, dict) or item.get("title") != title:
            continue
        issue_iid = item.get("iid")
        if not isinstance(issue_iid, int):
            raise MockReportingError("GitLab issue lookup omitted issue IID")
        return GitLabIssue(iid=issue_iid, title=title)
    return None


def _mr_note_idempotency_key(triage_run: TriageRun) -> str:
    return f"post-mr-note:{triage_run.gitlab_project_id}:{triage_run.pipeline_id}"


def _create_issue_idempotency_key(triage_run: TriageRun) -> str:
    return f"create-issue:{triage_run.gitlab_project_id}:{triage_run.pipeline_id}"


def _issue_note_idempotency_key(triage_run: TriageRun) -> str:
    return f"post-issue-note:{triage_run.gitlab_project_id}:{triage_run.pipeline_id}"


def _request_digest(note_body: str) -> str:
    return f"sha256:{hashlib.sha256(note_body.encode('utf-8')).hexdigest()}"


def _inline_code(value: str) -> str:
    escaped = _note_text(value, max_chars=MAX_NOTE_SNIPPET_CHARS).replace("`", "'")
    return f"`{escaped}`"


def _note_text(value: str, *, max_chars: int, fallback: str = "") -> str:
    redacted = redact_untrusted_text(value).replace("@", "[at]")
    bounded = redacted[:max_chars]
    if bounded:
        return bounded
    return fallback[:max_chars]


def _hostname_from_base_url(gitlab_base_url: str) -> str:
    parsed = urllib.parse.urlparse(gitlab_base_url)
    if not parsed.netloc:
        raise MockReportingError("GitLab base URL is not configured")
    return parsed.netloc
