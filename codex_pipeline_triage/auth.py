"""GitLab OAuth and server-side session primitives."""

from __future__ import annotations

import json
import os
import secrets
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Protocol

from pydantic import BaseModel, ConfigDict

DEFAULT_APP_BASE_URL = "http://127.0.0.1:8000"
DEFAULT_GITLAB_BASE_URL = "https://gitlab.com"
GITLAB_GROUP_ALLOWLIST_MODE = "gitlab_group"
GITLAB_OAUTH_SCOPES = ("read_user", "read_api")


class AuthSettings(BaseModel):
    """Configuration needed for GitLab login and app authorization."""

    model_config = ConfigDict(frozen=True)

    app_base_url: str = DEFAULT_APP_BASE_URL
    gitlab_base_url: str = DEFAULT_GITLAB_BASE_URL
    gitlab_oauth_client_id: str = ""
    gitlab_oauth_client_secret: str = ""
    session_cookie_name: str = "pipeline_triage_session"
    oauth_state_cookie_name: str = "pipeline_triage_oauth_state"
    session_cookie_max_age_seconds: int = 60 * 60 * 8
    oauth_state_max_age_seconds: int = 60 * 10
    secure_cookies: bool | None = None
    auth_allowlist_mode: str = ""
    allowed_gitlab_group_id: int | None = None

    @classmethod
    def from_env(cls) -> AuthSettings:
        """Load settings from process environment without reading env files."""
        app_base_url = os.environ.get("APP_BASE_URL", DEFAULT_APP_BASE_URL)
        secure_cookies = _optional_bool(os.environ.get("SESSION_COOKIE_SECURE"))
        return cls(
            app_base_url=app_base_url,
            gitlab_base_url=os.environ.get(
                "GITLAB_BASE_URL",
                DEFAULT_GITLAB_BASE_URL,
            ),
            gitlab_oauth_client_id=os.environ.get("GITLAB_OAUTH_CLIENT_ID", ""),
            gitlab_oauth_client_secret=os.environ.get("GITLAB_OAUTH_CLIENT_SECRET", ""),
            secure_cookies=secure_cookies,
            auth_allowlist_mode=os.environ.get("AUTH_ALLOWLIST_MODE", ""),
            allowed_gitlab_group_id=_optional_int(
                os.environ.get("ALLOWED_GITLAB_GROUP_ID")
            ),
        )

    @property
    def callback_url(self) -> str:
        return f"{self.app_base_url.rstrip('/')}/auth/gitlab/callback"

    @property
    def use_secure_cookies(self) -> bool:
        if self.secure_cookies is not None:
            return self.secure_cookies
        return self.app_base_url.startswith("https://")

    @property
    def is_oauth_configured(self) -> bool:
        return bool(self.gitlab_oauth_client_id and self.gitlab_oauth_client_secret)

    @property
    def is_group_authorization_configured(self) -> bool:
        return (
            self.auth_allowlist_mode == GITLAB_GROUP_ALLOWLIST_MODE
            and self.allowed_gitlab_group_id is not None
        )


class GitLabIdentity(BaseModel):
    """GitLab identity returned after a successful OAuth callback."""

    model_config = ConfigDict(frozen=True)

    gitlab_user_id: int
    gitlab_username: str
    display_name: str | None = None


class SessionRecord(BaseModel):
    """Server-side authenticated-session record."""

    model_config = ConfigDict(frozen=True)

    id: str
    identity: GitLabIdentity
    csrf_token: str
    created_at: datetime


@dataclass(frozen=True)
class GitLabOAuthResult:
    """OAuth result kept server-side only for immediate authorization."""

    identity: GitLabIdentity
    access_token: str = field(repr=False)


class OAuthError(RuntimeError):
    """Raised when the GitLab OAuth exchange fails."""


class AuthorizationError(RuntimeError):
    """Raised when the GitLab authorization lookup cannot be completed."""


class GitLabOAuthClient(Protocol):
    """Boundary for exchanging an OAuth code for a GitLab identity."""

    def exchange_code_for_user(
        self,
        code: str,
        redirect_uri: str,
    ) -> GitLabOAuthResult:
        """Return a GitLab identity without exposing OAuth token material."""
        raise NotImplementedError


class GitLabGroupMembershipClient(Protocol):
    """Boundary for checking GitLab group membership."""

    def is_group_member(
        self,
        *,
        access_token: str,
        gitlab_user_id: int,
        group_id: int,
    ) -> bool:
        """Return whether the authenticated user belongs to the configured group."""
        raise NotImplementedError


class UrllibGitLabOAuthClient:
    """Small standard-library GitLab OAuth client for manual demo testing."""

    def __init__(self, settings: AuthSettings, timeout_seconds: float = 15.0) -> None:
        self._settings = settings
        self._timeout_seconds = timeout_seconds

    def exchange_code_for_user(
        self,
        code: str,
        redirect_uri: str,
    ) -> GitLabOAuthResult:
        token = self._exchange_code_for_token(code=code, redirect_uri=redirect_uri)
        return GitLabOAuthResult(identity=self._fetch_user(token), access_token=token)

    def _exchange_code_for_token(self, code: str, redirect_uri: str) -> str:
        if not self._settings.is_oauth_configured:
            raise OAuthError("GitLab OAuth is not configured")

        payload = urllib.parse.urlencode(
            {
                "client_id": self._settings.gitlab_oauth_client_id,
                "client_secret": self._settings.gitlab_oauth_client_secret,
                "code": code,
                "grant_type": "authorization_code",
                "redirect_uri": redirect_uri,
            }
        ).encode("utf-8")
        request = urllib.request.Request(
            self._url("/oauth/token"),
            data=payload,
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            method="POST",
        )
        response_data = self._read_json(request)
        token = response_data.get("access_token")
        if not isinstance(token, str) or not token:
            raise OAuthError("GitLab OAuth token response did not include access token")
        return token

    def _fetch_user(self, access_token: str) -> GitLabIdentity:
        request = urllib.request.Request(
            self._url("/api/v4/user"),
            headers={"Authorization": f"Bearer {access_token}"},
            method="GET",
        )
        response_data = self._read_json(request)
        user_id = response_data.get("id")
        username = response_data.get("username")
        if not isinstance(user_id, int) or not isinstance(username, str):
            raise OAuthError("GitLab user response did not include identity")
        name = response_data.get("name")
        return GitLabIdentity(
            gitlab_user_id=user_id,
            gitlab_username=username,
            display_name=name if isinstance(name, str) else None,
        )

    def _read_json(self, request: urllib.request.Request) -> dict[str, object]:
        try:
            with urllib.request.urlopen(
                request,
                timeout=self._timeout_seconds,
            ) as response:
                raw_body = response.read()
        except urllib.error.URLError as exc:
            raise OAuthError("GitLab OAuth request failed") from exc

        try:
            parsed = json.loads(raw_body.decode("utf-8"))
        except json.JSONDecodeError as exc:
            raise OAuthError("GitLab OAuth response was not JSON") from exc

        if not isinstance(parsed, dict):
            raise OAuthError("GitLab OAuth response was not an object")
        return parsed

    def _url(self, path: str) -> str:
        return f"{str(self._settings.gitlab_base_url).rstrip('/')}{path}"


class UrllibGitLabGroupMembershipClient:
    """Small GitLab group membership client for the demo authorization gate."""

    def __init__(self, settings: AuthSettings, timeout_seconds: float = 15.0) -> None:
        self._settings = settings
        self._timeout_seconds = timeout_seconds

    def is_group_member(
        self,
        *,
        access_token: str,
        gitlab_user_id: int,
        group_id: int,
    ) -> bool:
        request = urllib.request.Request(
            self._url(f"/api/v4/groups/{group_id}/members/all/{gitlab_user_id}"),
            headers={"Authorization": f"Bearer {access_token}"},
            method="GET",
        )
        try:
            response_data = self._read_json(request)
        except AuthorizationError as exc:
            if str(exc) == "GitLab group member was not found":
                return False
            raise

        return response_data.get("id") == gitlab_user_id

    def _read_json(self, request: urllib.request.Request) -> dict[str, object]:
        try:
            with urllib.request.urlopen(
                request,
                timeout=self._timeout_seconds,
            ) as response:
                raw_body = response.read()
        except urllib.error.HTTPError as exc:
            if exc.code == 404:
                raise AuthorizationError("GitLab group member was not found") from exc
            raise AuthorizationError("GitLab group lookup failed") from exc
        except urllib.error.URLError as exc:
            raise AuthorizationError("GitLab group lookup failed") from exc

        try:
            parsed = json.loads(raw_body.decode("utf-8"))
        except json.JSONDecodeError as exc:
            raise AuthorizationError("GitLab group response was not JSON") from exc

        if not isinstance(parsed, dict):
            raise AuthorizationError("GitLab group response was not an object")
        return parsed

    def _url(self, path: str) -> str:
        return f"{str(self._settings.gitlab_base_url).rstrip('/')}{path}"


@dataclass(frozen=True)
class GitLabGroupAuthorizer:
    """Apply the configured GitLab group authorization policy."""

    settings: AuthSettings
    membership_client: GitLabGroupMembershipClient

    def is_authorized(
        self,
        *,
        identity: GitLabIdentity,
        access_token: str,
    ) -> bool:
        if not self.settings.is_group_authorization_configured:
            return False

        group_id = self.settings.allowed_gitlab_group_id
        if group_id is None:
            return False

        try:
            return self.membership_client.is_group_member(
                access_token=access_token,
                gitlab_user_id=identity.gitlab_user_id,
                group_id=group_id,
            )
        except AuthorizationError:
            return False


@dataclass
class OAuthStateStore:
    """One-time server-side OAuth state store."""

    _states: set[str] = field(default_factory=set)

    def create_state(self) -> str:
        state = secrets.token_urlsafe(32)
        self._states.add(state)
        return state

    def consume_state(self, state: str) -> bool:
        if state not in self._states:
            return False
        self._states.remove(state)
        return True


@dataclass
class InMemorySessionStore:
    """Minimal server-side session store for the local demo app."""

    _sessions: dict[str, SessionRecord] = field(default_factory=dict)

    def create_session(self, identity: GitLabIdentity) -> SessionRecord:
        session = SessionRecord(
            id=secrets.token_urlsafe(32),
            identity=identity,
            csrf_token=secrets.token_urlsafe(32),
            created_at=datetime.now(tz=timezone.utc),
        )
        self._sessions[session.id] = session
        return session

    def get_session(self, session_id: str | None) -> SessionRecord | None:
        if not session_id:
            return None
        return self._sessions.get(session_id)

    def delete_session(self, session_id: str | None) -> None:
        if session_id:
            self._sessions.pop(session_id, None)


def build_gitlab_authorization_url(settings: AuthSettings, state: str) -> str:
    """Build the GitLab authorization redirect URL."""
    query = urllib.parse.urlencode(
        {
            "client_id": settings.gitlab_oauth_client_id,
            "redirect_uri": settings.callback_url,
            "response_type": "code",
            "scope": " ".join(GITLAB_OAUTH_SCOPES),
            "state": state,
        }
    )
    return f"{str(settings.gitlab_base_url).rstrip('/')}/oauth/authorize?{query}"


def _optional_bool(value: str | None) -> bool | None:
    if value is None:
        return None
    normalized = value.strip().lower()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    raise ValueError("SESSION_COOKIE_SECURE must be a boolean value")


def _optional_int(value: str | None) -> int | None:
    if value is None or not value.strip():
        return None
    try:
        return int(value)
    except ValueError as exc:
        raise ValueError("ALLOWED_GITLAB_GROUP_ID must be an integer") from exc
