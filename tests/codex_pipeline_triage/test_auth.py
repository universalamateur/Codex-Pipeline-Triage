"""Tests for the Spike 2.1 GitLab OAuth login boundary."""

from __future__ import annotations

from typing import Protocol
from urllib.parse import parse_qs, urlparse

from fastapi.testclient import TestClient

from codex_pipeline_triage.app import create_app
from codex_pipeline_triage.auth import AuthSettings, GitLabIdentity


class OAuthClientLike(Protocol):
    """Protocol matching the app's OAuth client boundary."""

    calls: list[tuple[str, str]]

    def exchange_code_for_user(self, code: str, redirect_uri: str) -> GitLabIdentity:
        """Return a fake identity for route tests."""


class FakeOAuthClient:
    """Mock GitLab OAuth boundary used by route tests."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []

    def exchange_code_for_user(self, code: str, redirect_uri: str) -> GitLabIdentity:
        self.calls.append((code, redirect_uri))
        return GitLabIdentity(
            gitlab_user_id=1001,
            gitlab_username="demo-user",
            display_name="Demo User",
        )


def make_settings() -> AuthSettings:
    return AuthSettings(
        app_base_url="https://testserver",
        gitlab_base_url="https://gitlab.example.com",
        gitlab_oauth_client_id="client-id",
        gitlab_oauth_client_secret="client-secret",
        secure_cookies=True,
    )


def make_client(
    oauth_client: OAuthClientLike,
) -> TestClient:
    return TestClient(
        create_app(auth_settings=make_settings(), oauth_client=oauth_client),
        base_url="https://testserver",
    )


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
    assert query["scope"] == ["read_user"]
    assert query["state"][0]
    assert "pipeline_triage_oauth_state=" in response.headers["set-cookie"]
    assert "HttpOnly" in response.headers["set-cookie"]
    assert "Secure" in response.headers["set-cookie"]
    assert "SameSite=lax" in response.headers["set-cookie"]


def test_oauth_callback_success_creates_server_side_session_cookie() -> None:
    fake_oauth_client = FakeOAuthClient()

    with make_client(fake_oauth_client) as client:
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
    session_cookie = _find_cookie_header(
        callback_response.headers.get_list("set-cookie"),
        "pipeline_triage_session=",
    )
    assert session_cookie is not None
    assert "oauth-code" not in session_cookie
    assert "HttpOnly" in session_cookie
    assert "Secure" in session_cookie
    assert "SameSite=lax" in session_cookie
    assert "Max-Age=28800" in session_cookie


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
