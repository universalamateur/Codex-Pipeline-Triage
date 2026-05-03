"""Connected-project validation and token storage boundaries."""

from __future__ import annotations

import os
import secrets
import urllib.parse
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Protocol, cast

from codex_pipeline_triage.auth import AuthSettings
from codex_pipeline_triage.gitlab import (
    GlabApiRequest,
    GlabExecutor,
    GlabExecutorError,
    JsonResponse,
)
from codex_pipeline_triage.models import ConnectedProject, ProjectActionPolicy
from codex_pipeline_triage.persistence import PersistenceStore, SqliteStore

DEFAULT_TRIAGE_DB_PATH = ".local/triage.sqlite"
DEFAULT_GLAB_CONFIG_DIR = ".local/glab-projects"


class ProjectConnectionError(RuntimeError):
    """Raised when a project cannot be safely connected."""


@dataclass(frozen=True)
class GitLabProjectMetadata:
    """Bounded GitLab project metadata needed for connection policy."""

    project_id: int
    path_with_namespace: str
    display_name: str
    namespace_id: int


class GitLabProjectClient(Protocol):
    """Boundary for validating a project token against GitLab metadata."""

    def get_project_metadata(
        self,
        *,
        project_reference: str,
        project_token: str,
    ) -> GitLabProjectMetadata:
        """Return metadata visible to the provided project token."""
        raise NotImplementedError


class ProjectTokenSecretStore(Protocol):
    """Server-side boundary for storing raw project token material."""

    def store_project_token(self, project_token: str) -> str:
        """Store the raw token and return a non-secret reference."""
        raise NotImplementedError


@dataclass
class InMemoryProjectTokenSecretStore:
    """Demo-local secret boundary that keeps raw tokens out of persistence."""

    _tokens: dict[str, str] = field(default_factory=dict)

    def store_project_token(self, project_token: str) -> str:
        secret_ref = f"secret-ref:{secrets.token_urlsafe(24)}"
        self._tokens[secret_ref] = project_token
        return secret_ref


@dataclass(frozen=True)
class GlabGitLabProjectClient:
    """Validate project tokens through the deterministic glab executor."""

    settings: AuthSettings
    executor: GlabExecutor

    def get_project_metadata(
        self,
        *,
        project_reference: str,
        project_token: str,
    ) -> GitLabProjectMetadata:
        normalized_reference = _normalize_project_reference(
            project_reference,
            self.settings.gitlab_base_url,
        )
        endpoint = f"projects/{urllib.parse.quote(normalized_reference, safe='')}"

        try:
            response = self.executor.api(
                GlabApiRequest(endpoint=endpoint),
                token=project_token,
            )
        except GlabExecutorError as exc:
            raise ProjectConnectionError("Project token validation failed") from exc

        return _metadata_from_response(response)


@dataclass(frozen=True)
class ProjectConnector:
    """Connect authorized GitLab projects without exposing project tokens."""

    settings: AuthSettings
    gitlab_project_client: GitLabProjectClient
    token_store: ProjectTokenSecretStore
    persistence_store: PersistenceStore

    def connect_project(
        self,
        *,
        project_reference: str,
        project_token: str,
        connected_by_gitlab_user_id: int,
    ) -> ConnectedProject:
        if not self.settings.is_group_authorization_configured:
            raise ProjectConnectionError("GitLab project group gate is not configured")

        cleaned_reference = project_reference.strip()
        cleaned_token = project_token.strip()
        if not cleaned_reference or not cleaned_token:
            raise ProjectConnectionError("Project reference and token are required")

        metadata = self.gitlab_project_client.get_project_metadata(
            project_reference=cleaned_reference,
            project_token=cleaned_token,
        )
        if metadata.namespace_id != self.settings.allowed_gitlab_group_id:
            raise ProjectConnectionError(
                "Project is outside the configured GitLab group"
            )

        token_ref = self.token_store.store_project_token(cleaned_token)
        now = datetime.now(tz=timezone.utc)
        connected_project = ConnectedProject(
            id=f"connected-project-{secrets.token_urlsafe(16)}",
            gitlab_project_id=metadata.project_id,
            gitlab_project_path=metadata.path_with_namespace,
            display_name=metadata.display_name,
            token_ciphertext=token_ref,
            webhook_secret_hash="",
            action_policy=ProjectActionPolicy(),
            connected_by_gitlab_user_id=connected_by_gitlab_user_id,
            enabled=True,
            created_at=now,
            updated_at=now,
        )
        return self.persistence_store.create_connected_project(connected_project)

    def list_projects_for_user(self, gitlab_user_id: int) -> list[ConnectedProject]:
        return self.persistence_store.list_connected_projects_for_user(gitlab_user_id)


def build_default_project_connector(settings: AuthSettings) -> ProjectConnector:
    """Build the default project connector for the local demo app."""
    db_path = Path(os.environ.get("TRIAGE_DATA_FILE", DEFAULT_TRIAGE_DB_PATH))
    executor = GlabExecutor(
        config_dir=Path(DEFAULT_GLAB_CONFIG_DIR),
        hostname=_hostname_from_base_url(settings.gitlab_base_url),
    )
    return ProjectConnector(
        settings=settings,
        gitlab_project_client=GlabGitLabProjectClient(
            settings=settings,
            executor=executor,
        ),
        token_store=InMemoryProjectTokenSecretStore(),
        persistence_store=SqliteStore(db_path),
    )


def _metadata_from_response(response: JsonResponse) -> GitLabProjectMetadata:
    if not isinstance(response, dict):
        raise ProjectConnectionError("GitLab project response was not an object")

    project_id = response.get("id")
    path_with_namespace = response.get("path_with_namespace")
    name = response.get("name")
    namespace = response.get("namespace")
    if not isinstance(namespace, dict):
        raise ProjectConnectionError("GitLab project response omitted namespace")

    namespace_id = cast(dict[str, object], namespace).get("id")
    if (
        not isinstance(project_id, int)
        or not isinstance(path_with_namespace, str)
        or not isinstance(name, str)
        or not isinstance(namespace_id, int)
    ):
        raise ProjectConnectionError("GitLab project response omitted metadata")

    return GitLabProjectMetadata(
        project_id=project_id,
        path_with_namespace=path_with_namespace,
        display_name=name,
        namespace_id=namespace_id,
    )


def _normalize_project_reference(project_reference: str, gitlab_base_url: str) -> str:
    value = project_reference.strip()
    if not value:
        raise ProjectConnectionError("Project reference is required")

    parsed = urllib.parse.urlparse(value)
    if parsed.scheme or parsed.netloc:
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            raise ProjectConnectionError("Project URL is not supported")
        expected_host = _hostname_from_base_url(gitlab_base_url)
        if parsed.netloc != expected_host:
            raise ProjectConnectionError("Project URL host is not configured GitLab")
        value = parsed.path.strip("/")

    if value.endswith(".git"):
        value = value[:-4]
    if not value:
        raise ProjectConnectionError("Project reference is required")
    return value


def _hostname_from_base_url(gitlab_base_url: str) -> str:
    parsed = urllib.parse.urlparse(gitlab_base_url)
    if not parsed.netloc:
        raise ProjectConnectionError("GitLab base URL is not configured")
    return parsed.netloc
