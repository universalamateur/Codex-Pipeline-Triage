"""Tests for the Spike 2.1 GitLab OAuth login boundary."""

from __future__ import annotations

from typing import Protocol
from urllib.parse import parse_qs, urlparse

import pytest
from fastapi.testclient import TestClient

from codex_pipeline_triage.app import create_app
from codex_pipeline_triage.auth import (
    GITLAB_OAUTH_SCOPES,
    AuthorizationError,
    AuthSettings,
    GitLabIdentity,
    GitLabOAuthResult,
)
from tests.codex_pipeline_triage.helpers import make_auth_settings


class OAuthClientLike(Protocol):
    """Protocol matching the app's OAuth client boundary."""

    calls: list[tuple[str, str]]

    def exchange_code_for_user(
        self,
        code: str,
        redirect_uri: str,
    ) -> GitLabOAuthResult:
        """Return a fake identity for route tests."""


class FakeOAuthClient:
    """Mock GitLab OAuth boundary used by route tests."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []

    def exchange_code_for_user(
        self,
        code: str,
        redirect_uri: str,
    ) -> GitLabOAuthResult:
        self.calls.append((code, redirect_uri))
        return GitLabOAuthResult(
            identity=GitLabIdentity(
                gitlab_user_id=1001,
                gitlab_username="demo-user",
                display_name="Demo User",
            ),
            access_token="oauth-access-token",
        )


class FakeGroupMembershipClient:
    """Mock GitLab group membership boundary used by route tests."""

    def __init__(self, *, is_member: bool = True, fail_lookup: bool = False) -> None:
        self.is_member = is_member
        self.fail_lookup = fail_lookup
        self.calls: list[tuple[str, int, int]] = []

    def is_group_member(
        self,
        *,
        access_token: str,
        gitlab_user_id: int,
        group_id: int,
    ) -> bool:
        self.calls.append((access_token, gitlab_user_id, group_id))
        if self.fail_lookup:
            raise AuthorizationError("GitLab group lookup failed")
        return self.is_member


def make_client(
    oauth_client: OAuthClientLike,
    group_membership_client: FakeGroupMembershipClient | None = None,
    auth_settings: AuthSettings | None = None,
) -> TestClient:
    settings = auth_settings or make_auth_settings()
    return TestClient(
        create_app(
            auth_settings=settings,
            oauth_client=oauth_client,
            group_membership_client=group_membership_client
            or FakeGroupMembershipClient(),
        ),
        base_url="https://testserver",
    )


def test_auth_settings_loads_documented_group_gate_config(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("AUTH_ALLOWLIST_MODE", "gitlab_group")
    monkeypatch.setenv("ALLOWED_GITLAB_GROUP_ID", "59032064")

    settings = AuthSettings.from_env()

    assert settings.auth_allowlist_mode == "gitlab_group"
    assert settings.allowed_gitlab_group_id == 59032064
    assert settings.is_group_authorization_configured


def test_login_redirect_sets_oauth_state_cookie() -> None:
    fake_oauth_client = FakeOAuthClient()

    with make_client(fake_oauth_client) as client:
        response = client.get("/login", follow_redirects=False)

    assert response.status_code == 303
    redirect = urlparse(response.headers["location"])
    query = parse_qs(redirect.query)
    assert redirect.scheme == "https"
    assert redirect.netloc == "gitlab.example.com"
    assert redirect.path == "/oauth/authorize"
    assert query["client_id"] == ["client-id"]
    assert query["redirect_uri"] == ["https://testserver/auth/gitlab/callback"]
    assert query["response_type"] == ["code"]
    assert query["scope"] == [" ".join(GITLAB_OAUTH_SCOPES)]
    assert set(query["scope"][0].split()) == {"read_user", "read_api"}
    assert query["state"][0]
    assert "pipeline_triage_oauth_state=" in response.headers["set-cookie"]
    assert "HttpOnly" in response.headers["set-cookie"]
    assert "Secure" in response.headers["set-cookie"]
    assert "SameSite=lax" in response.headers["set-cookie"]


def test_oauth_callback_success_creates_server_side_session_cookie() -> None:
    fake_oauth_client = FakeOAuthClient()
    group_membership_client = FakeGroupMembershipClient(is_member=True)

    with make_client(fake_oauth_client, group_membership_client) as client:
        login_response = client.get("/login", follow_redirects=False)
        state = parse_qs(urlparse(login_response.headers["location"]).query)["state"][0]
        callback_response = client.get(
            f"/auth/gitlab/callback?code=oauth-code&state={state}",
            follow_redirects=False,
        )

    assert callback_response.status_code == 303
    assert callback_response.headers["location"] == "/"
    assert fake_oauth_client.calls == [
        ("oauth-code", "https://testserver/auth/gitlab/callback")
    ]
    assert group_membership_client.calls == [("oauth-access-token", 1001, 59032064)]
    session_cookie = _find_cookie_header(
        callback_response.headers.get_list("set-cookie"),
        "pipeline_triage_session=",
    )
    assert session_cookie is not None
    assert "oauth-code" not in session_cookie
    assert "oauth-access-token" not in session_cookie
    assert "HttpOnly" in session_cookie
    assert "Secure" in session_cookie
    assert "SameSite=lax" in session_cookie
    assert "Max-Age=28800" in session_cookie


def test_oauth_callback_denies_non_member_before_session_creation() -> None:
    fake_oauth_client = FakeOAuthClient()
    group_membership_client = FakeGroupMembershipClient(is_member=False)

    with make_client(fake_oauth_client, group_membership_client) as client:
        login_response = client.get("/login", follow_redirects=False)
        state = parse_qs(urlparse(login_response.headers["location"]).query)["state"][0]
        callback_response = client.get(
            f"/auth/gitlab/callback?code=oauth-code&state={state}",
            follow_redirects=False,
        )
        index_response = client.get("/")

    assert callback_response.status_code == 303
    assert callback_response.headers["location"] == "/access-denied"
    assert "pipeline_triage_session=" not in callback_response.headers.get(
        "set-cookie",
        "",
    )
    assert "Sign in with GitLab" in index_response.text


def test_oauth_callback_fails_closed_when_group_config_is_missing() -> None:
    fake_oauth_client = FakeOAuthClient()
    group_membership_client = FakeGroupMembershipClient(is_member=True)
    settings = make_auth_settings().model_copy(
        update={"auth_allowlist_mode": "", "allowed_gitlab_group_id": None}
    )

    with make_client(fake_oauth_client, group_membership_client, settings) as client:
        login_response = client.get("/login", follow_redirects=False)
        state = parse_qs(urlparse(login_response.headers["location"]).query)["state"][0]
        callback_response = client.get(
            f"/auth/gitlab/callback?code=oauth-code&state={state}",
            follow_redirects=False,
        )

    assert callback_response.status_code == 303
    assert callback_response.headers["location"] == "/access-denied"
    assert not group_membership_client.calls


def test_oauth_callback_fails_closed_when_group_lookup_fails() -> None:
    fake_oauth_client = FakeOAuthClient()
    group_membership_client = FakeGroupMembershipClient(fail_lookup=True)

    with make_client(fake_oauth_client, group_membership_client) as client:
        login_response = client.get("/login", follow_redirects=False)
        state = parse_qs(urlparse(login_response.headers["location"]).query)["state"][0]
        callback_response = client.get(
            f"/auth/gitlab/callback?code=oauth-code&state={state}",
            follow_redirects=False,
        )

    assert callback_response.status_code == 303
    assert callback_response.headers["location"] == "/access-denied"
    assert group_membership_client.calls == [("oauth-access-token", 1001, 59032064)]


def test_oauth_callback_rejects_invalid_state() -> None:
    fake_oauth_client = FakeOAuthClient()

    with make_client(fake_oauth_client) as client:
        client.get("/login", follow_redirects=False)
        response = client.get(
            "/auth/gitlab/callback?code=oauth-code&state=wrong-state",
            follow_redirects=False,
        )

    assert response.status_code == 400
    assert not fake_oauth_client.calls
    assert "pipeline_triage_session=" not in response.headers.get("set-cookie", "")


def test_logout_requires_session_csrf_token() -> None:
    fake_oauth_client = FakeOAuthClient()

    with make_client(fake_oauth_client) as client:
        login_response = client.get("/login", follow_redirects=False)
        state = parse_qs(urlparse(login_response.headers["location"]).query)["state"][0]
        client.get(
            f"/auth/gitlab/callback?code=oauth-code&state={state}",
            follow_redirects=False,
        )
        response = client.post(
            "/logout",
            data={"csrf_token": "wrong-token"},
            follow_redirects=False,
        )

    assert response.status_code == 400


def test_access_denied_page_exists_without_group_gate_logic() -> None:
    fake_oauth_client = FakeOAuthClient()

    with make_client(fake_oauth_client) as client:
        response = client.get("/access-denied")

    assert response.status_code == 403
    assert "not authorized" in response.text


def _find_cookie_header(headers: list[str], cookie_prefix: str) -> str | None:
    for header in headers:
        if header.startswith(cookie_prefix):
            return header
    return None
