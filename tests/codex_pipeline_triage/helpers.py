"""Shared test helpers."""

from codex_pipeline_triage.auth import AuthSettings


def make_auth_settings() -> AuthSettings:
    return AuthSettings(
        app_base_url="https://testserver",
        gitlab_base_url="https://gitlab.example.com",
        gitlab_oauth_client_id="client-id",
        gitlab_oauth_client_secret="client-secret",
        secure_cookies=True,
        auth_allowlist_mode="gitlab_group",
        allowed_gitlab_group_id=59032064,
    )
