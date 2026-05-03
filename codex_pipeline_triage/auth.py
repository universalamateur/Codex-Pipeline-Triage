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


class AuthSettings(BaseModel):
    """Configuration needed for the Spike 2.1 GitLab login path."""

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


class OAuthError(RuntimeError):
    """Raised when the GitLab OAuth exchange fails."""


class GitLabOAuthClient(Protocol):
    """Boundary for exchanging an OAuth code for a GitLab identity."""

    def exchange_code_for_user(self, code: str, redirect_uri: str) -> GitLabIdentity:
        """Return a GitLab identity without exposing OAuth token material."""


class UrllibGitLabOAuthClient:
    """Small standard-library GitLab OAuth client for manual demo testing."""

    def __init__(self, settings: AuthSettings, timeout_seconds: float = 15.0) -> None:
        self._settings = settings
        self._timeout_seconds = timeout_seconds

    def exchange_code_for_user(self, code: str, redirect_uri: str) -> GitLabIdentity:
        token = self._exchange_code_for_token(code=code, redirect_uri=redirect_uri)
        return self._fetch_user(token)

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
            "scope": "read_user",
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
