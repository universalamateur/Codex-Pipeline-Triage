"""FastAPI application entry point."""

import urllib.parse
from typing import Literal

from fastapi import FastAPI, Request, Response, status
from fastapi.responses import HTMLResponse, RedirectResponse
from pydantic import BaseModel, ConfigDict

from codex_pipeline_triage.auth import (
    AuthSettings,
    GitLabOAuthClient,
    InMemorySessionStore,
    OAuthError,
    OAuthStateStore,
    UrllibGitLabOAuthClient,
    build_gitlab_authorization_url,
)

SERVICE_NAME = "codex-pipeline-triage"


class HealthResponse(BaseModel):
    """Health endpoint response."""

    model_config = ConfigDict(frozen=True)

    status: Literal["ok"] = "ok"
    service: str = SERVICE_NAME


def create_app(
    *,
    auth_settings: AuthSettings | None = None,
    oauth_client: GitLabOAuthClient | None = None,
    session_store: InMemorySessionStore | None = None,
    oauth_state_store: OAuthStateStore | None = None,
) -> FastAPI:
    """Create the FastAPI app for local development and tests."""
    settings = auth_settings or AuthSettings.from_env()
    resolved_oauth_client = oauth_client or UrllibGitLabOAuthClient(settings)
    resolved_session_store = session_store or InMemorySessionStore()
    resolved_state_store = oauth_state_store or OAuthStateStore()
    fastapi_app = FastAPI(
        title="Codex Pipeline Triage",
        version="0.1.0",
    )

    @fastapi_app.get("/health", response_model=HealthResponse, tags=["system"])
    async def health() -> HealthResponse:
        return HealthResponse()

    @fastapi_app.get("/", response_class=HTMLResponse, tags=["auth"])
    async def index(request: Request) -> HTMLResponse:
        session = resolved_session_store.get_session(
            request.cookies.get(settings.session_cookie_name)
        )
        if session is None:
            return HTMLResponse(
                _page(
                    "Codex Pipeline Triage",
                    '<a href="/login">Sign in with GitLab</a>',
                )
            )

        username = _escape_html(session.identity.gitlab_username)
        csrf_token = _escape_html(session.csrf_token)
        return HTMLResponse(
            _page(
                "Codex Pipeline Triage",
                (
                    f"<p>Signed in as {username}.</p>"
                    "<p>Authorization gate comes in Spike 2.2.</p>"
                    '<form action="/logout" method="post">'
                    f'<input type="hidden" name="csrf_token" value="{csrf_token}">'
                    '<button type="submit">Log out</button>'
                    "</form>"
                ),
            )
        )

    @fastapi_app.get("/login", tags=["auth"])
    async def login() -> Response:
        if not settings.is_oauth_configured:
            return HTMLResponse(
                _page("GitLab Login", "<p>GitLab OAuth is not configured.</p>"),
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            )

        oauth_state = resolved_state_store.create_state()
        response = RedirectResponse(
            build_gitlab_authorization_url(settings, oauth_state),
            status_code=status.HTTP_303_SEE_OTHER,
        )
        _set_cookie(
            response,
            name=settings.oauth_state_cookie_name,
            value=oauth_state,
            max_age=settings.oauth_state_max_age_seconds,
            settings=settings,
        )
        return response

    @fastapi_app.get("/auth/gitlab/callback", tags=["auth"])
    async def gitlab_callback(
        request: Request,
        code: str | None = None,
        state: str | None = None,
    ) -> Response:
        expected_state = request.cookies.get(settings.oauth_state_cookie_name)
        if (
            not code
            or not state
            or state != expected_state
            or not resolved_state_store.consume_state(state)
        ):
            return HTMLResponse(
                _page("GitLab Login Failed", "<p>Invalid OAuth state.</p>"),
                status_code=status.HTTP_400_BAD_REQUEST,
            )

        try:
            identity = resolved_oauth_client.exchange_code_for_user(
                code=code,
                redirect_uri=settings.callback_url,
            )
        except OAuthError:
            response: Response = HTMLResponse(
                _page("GitLab Login Failed", "<p>GitLab OAuth failed.</p>"),
                status_code=status.HTTP_502_BAD_GATEWAY,
            )
            response.delete_cookie(settings.oauth_state_cookie_name, path="/")
            return response

        session = resolved_session_store.create_session(identity)
        response = RedirectResponse("/", status_code=status.HTTP_303_SEE_OTHER)
        _set_cookie(
            response,
            name=settings.session_cookie_name,
            value=session.id,
            max_age=settings.session_cookie_max_age_seconds,
            settings=settings,
        )
        response.delete_cookie(settings.oauth_state_cookie_name, path="/")
        return response

    @fastapi_app.post("/logout", tags=["auth"])
    async def logout(request: Request) -> Response:
        session_id = request.cookies.get(settings.session_cookie_name)
        session = resolved_session_store.get_session(session_id)
        body = await request.body()
        form = urllib.parse.parse_qs(body.decode("utf-8"))
        submitted_csrf_token = form.get("csrf_token", [""])[0]
        if session is None or submitted_csrf_token != session.csrf_token:
            return HTMLResponse(
                _page("Logout Failed", "<p>Invalid session.</p>"),
                status_code=status.HTTP_400_BAD_REQUEST,
            )

        resolved_session_store.delete_session(session_id)
        response = RedirectResponse("/", status_code=status.HTTP_303_SEE_OTHER)
        response.delete_cookie(
            settings.session_cookie_name,
            path="/",
        )
        return response

    @fastapi_app.get("/access-denied", response_class=HTMLResponse, tags=["auth"])
    async def access_denied() -> HTMLResponse:
        return HTMLResponse(
            _page("Access Denied", "<p>Your GitLab account is not authorized.</p>"),
            status_code=status.HTTP_403_FORBIDDEN,
        )

    return fastapi_app


app = create_app()


def _set_cookie(
    response: Response,
    *,
    name: str,
    value: str,
    max_age: int,
    settings: AuthSettings,
) -> None:
    response.set_cookie(
        key=name,
        value=value,
        max_age=max_age,
        httponly=True,
        secure=settings.use_secure_cookies,
        samesite="lax",
    )


def _page(title: str, body: str) -> str:
    return (
        "<!doctype html>"
        '<html lang="en">'
        "<head>"
        '<meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width, initial-scale=1">'
        f"<title>{_escape_html(title)}</title>"
        "</head>"
        "<body>"
        f"<main><h1>{_escape_html(title)}</h1>{body}</main>"
        "</body>"
        "</html>"
    )


def _escape_html(value: str) -> str:
    return (
        value.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
    )
