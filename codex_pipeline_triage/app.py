"""FastAPI application entry point."""

import urllib.parse
from dataclasses import dataclass
from typing import Literal

from fastapi import FastAPI, Request, Response, status
from fastapi.responses import HTMLResponse, RedirectResponse
from pydantic import BaseModel, ConfigDict

from codex_pipeline_triage.auth import (
    AuthSettings,
    GitLabGroupAuthorizer,
    GitLabGroupMembershipClient,
    GitLabOAuthClient,
    InMemorySessionStore,
    OAuthError,
    OAuthStateStore,
    SessionRecord,
    UrllibGitLabGroupMembershipClient,
    UrllibGitLabOAuthClient,
    build_gitlab_authorization_url,
)
from codex_pipeline_triage.projects import (
    ProjectConnectionError,
    ProjectConnector,
    build_default_project_connector,
)

SERVICE_NAME = "codex-pipeline-triage"


class HealthResponse(BaseModel):
    """Health endpoint response."""

    model_config = ConfigDict(frozen=True)

    status: Literal["ok"] = "ok"
    service: str = SERVICE_NAME


@dataclass(frozen=True)
class AuthRuntime:
    """Resolved auth dependencies for the app factory."""

    settings: AuthSettings
    oauth_client: GitLabOAuthClient
    authorizer: GitLabGroupAuthorizer
    session_store: InMemorySessionStore
    oauth_state_store: OAuthStateStore


# FastAPI app factories collect dependency overrides and route closures in one place.
# pylint: disable=too-many-arguments,too-many-locals,too-many-statements
def create_app(
    *,
    auth_settings: AuthSettings | None = None,
    oauth_client: GitLabOAuthClient | None = None,
    group_membership_client: GitLabGroupMembershipClient | None = None,
    session_store: InMemorySessionStore | None = None,
    oauth_state_store: OAuthStateStore | None = None,
    project_connector: ProjectConnector | None = None,
) -> FastAPI:
    """Create the FastAPI app for local development and tests."""
    runtime = _build_auth_runtime(
        auth_settings=auth_settings,
        oauth_client=oauth_client,
        group_membership_client=group_membership_client,
        session_store=session_store,
        oauth_state_store=oauth_state_store,
    )
    resolved_project_connector = project_connector

    def get_project_connector() -> ProjectConnector:
        nonlocal resolved_project_connector
        if resolved_project_connector is None:
            resolved_project_connector = build_default_project_connector(
                runtime.settings
            )
        return resolved_project_connector

    fastapi_app = FastAPI(
        title="Codex Pipeline Triage",
        version="0.1.0",
    )

    @fastapi_app.get("/health", response_model=HealthResponse, tags=["system"])
    async def health() -> HealthResponse:
        return HealthResponse()

    @fastapi_app.get("/", response_class=HTMLResponse, tags=["auth"])
    async def index(request: Request) -> HTMLResponse:
        session = _get_current_session(request, runtime)
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
                    "<p>GitLab group authorization passed.</p>"
                    '<p><a href="/projects">Connected projects</a></p>'
                    '<form action="/logout" method="post">'
                    f'<input type="hidden" name="csrf_token" value="{csrf_token}">'
                    '<button type="submit">Log out</button>'
                    "</form>"
                ),
            )
        )

    @fastapi_app.get("/login", tags=["auth"])
    async def login() -> Response:
        if not runtime.settings.is_oauth_configured:
            return HTMLResponse(
                _page("GitLab Login", "<p>GitLab OAuth is not configured.</p>"),
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            )

        oauth_state = runtime.oauth_state_store.create_state()
        response = RedirectResponse(
            build_gitlab_authorization_url(runtime.settings, oauth_state),
            status_code=status.HTTP_303_SEE_OTHER,
        )
        _set_cookie(
            response,
            name=runtime.settings.oauth_state_cookie_name,
            value=oauth_state,
            max_age=runtime.settings.oauth_state_max_age_seconds,
            settings=runtime.settings,
        )
        return response

    @fastapi_app.get("/auth/gitlab/callback", tags=["auth"])
    async def gitlab_callback(
        request: Request,
        code: str | None = None,
        state: str | None = None,
    ) -> Response:
        expected_state = request.cookies.get(runtime.settings.oauth_state_cookie_name)
        if (
            not code
            or not state
            or state != expected_state
            or not runtime.oauth_state_store.consume_state(state)
        ):
            return HTMLResponse(
                _page("GitLab Login Failed", "<p>Invalid OAuth state.</p>"),
                status_code=status.HTTP_400_BAD_REQUEST,
            )

        try:
            # Pylint does not infer return types from structural Protocols here.
            # pylint: disable-next=assignment-from-no-return
            oauth_result = runtime.oauth_client.exchange_code_for_user(
                code=code,
                redirect_uri=runtime.settings.callback_url,
            )
        except OAuthError:
            response: Response = HTMLResponse(
                _page("GitLab Login Failed", "<p>GitLab OAuth failed.</p>"),
                status_code=status.HTTP_502_BAD_GATEWAY,
            )
            response.delete_cookie(runtime.settings.oauth_state_cookie_name, path="/")
            return response

        if not runtime.authorizer.is_authorized(
            identity=oauth_result.identity,
            access_token=oauth_result.access_token,
        ):
            response = RedirectResponse(
                "/access-denied",
                status_code=status.HTTP_303_SEE_OTHER,
            )
            response.delete_cookie(runtime.settings.oauth_state_cookie_name, path="/")
            return response

        session = runtime.session_store.create_session(oauth_result.identity)
        response = RedirectResponse("/", status_code=status.HTTP_303_SEE_OTHER)
        _set_cookie(
            response,
            name=runtime.settings.session_cookie_name,
            value=session.id,
            max_age=runtime.settings.session_cookie_max_age_seconds,
            settings=runtime.settings,
        )
        response.delete_cookie(runtime.settings.oauth_state_cookie_name, path="/")
        return response

    @fastapi_app.post("/logout", tags=["auth"])
    async def logout(request: Request) -> Response:
        session_id = request.cookies.get(runtime.settings.session_cookie_name)
        session = runtime.session_store.get_session(session_id)
        body = await request.body()
        form = urllib.parse.parse_qs(body.decode("utf-8"))
        submitted_csrf_token = form.get("csrf_token", [""])[0]
        if session is None or submitted_csrf_token != session.csrf_token:
            return HTMLResponse(
                _page("Logout Failed", "<p>Invalid session.</p>"),
                status_code=status.HTTP_400_BAD_REQUEST,
            )

        runtime.session_store.delete_session(session_id)
        response = RedirectResponse("/", status_code=status.HTTP_303_SEE_OTHER)
        response.delete_cookie(
            runtime.settings.session_cookie_name,
            path="/",
        )
        return response

    @fastapi_app.get("/projects", response_class=HTMLResponse, tags=["projects"])
    async def projects(request: Request) -> Response:
        session = _get_current_session(request, runtime)
        if session is None:
            return RedirectResponse("/login", status_code=status.HTTP_303_SEE_OTHER)

        connected_projects = get_project_connector().list_projects_for_user(
            session.identity.gitlab_user_id
        )
        csrf_token = _escape_html(session.csrf_token)
        if connected_projects:
            project_items = "".join(
                "<li>"
                f"{_escape_html(project.gitlab_project_path)} "
                f"(#{project.gitlab_project_id})"
                "</li>"
                for project in connected_projects
            )
            project_list = f"<ul>{project_items}</ul>"
        else:
            project_list = "<p>No connected projects.</p>"

        return HTMLResponse(
            _page(
                "Connected Projects",
                (
                    f"{project_list}"
                    '<p><a href="/projects/connect">Connect project</a></p>'
                    '<form action="/logout" method="post">'
                    f'<input type="hidden" name="csrf_token" value="{csrf_token}">'
                    '<button type="submit">Log out</button>'
                    "</form>"
                ),
            )
        )

    @fastapi_app.get(
        "/projects/connect",
        response_class=HTMLResponse,
        tags=["projects"],
    )
    async def connect_project_form(request: Request) -> Response:
        session = _get_current_session(request, runtime)
        if session is None:
            return RedirectResponse("/login", status_code=status.HTTP_303_SEE_OTHER)

        return HTMLResponse(_connect_project_page(session))

    @fastapi_app.post("/projects/connect", tags=["projects"])
    async def connect_project(request: Request) -> Response:
        session = _get_current_session(request, runtime)
        if session is None:
            return RedirectResponse("/login", status_code=status.HTTP_303_SEE_OTHER)

        body = await request.body()
        form = urllib.parse.parse_qs(body.decode("utf-8"))
        if _form_value(form, "csrf_token") != session.csrf_token:
            return HTMLResponse(
                _page("Connect Project Failed", "<p>Invalid session.</p>"),
                status_code=status.HTTP_400_BAD_REQUEST,
            )

        try:
            get_project_connector().connect_project(
                project_reference=_form_value(form, "project_reference"),
                project_token=_form_value(form, "project_token"),
                connected_by_gitlab_user_id=session.identity.gitlab_user_id,
            )
        except ProjectConnectionError as exc:
            return HTMLResponse(
                _connect_project_page(session, error=str(exc)),
                status_code=status.HTTP_400_BAD_REQUEST,
            )

        return RedirectResponse("/projects", status_code=status.HTTP_303_SEE_OTHER)

    @fastapi_app.get("/access-denied", response_class=HTMLResponse, tags=["auth"])
    async def access_denied() -> HTMLResponse:
        return HTMLResponse(
            _page("Access Denied", "<p>Your GitLab account is not authorized.</p>"),
            status_code=status.HTTP_403_FORBIDDEN,
        )

    return fastapi_app


def _build_auth_runtime(
    *,
    auth_settings: AuthSettings | None,
    oauth_client: GitLabOAuthClient | None,
    group_membership_client: GitLabGroupMembershipClient | None,
    session_store: InMemorySessionStore | None,
    oauth_state_store: OAuthStateStore | None,
) -> AuthRuntime:
    settings = auth_settings or AuthSettings.from_env()
    resolved_membership_client = (
        group_membership_client or UrllibGitLabGroupMembershipClient(settings)
    )
    return AuthRuntime(
        settings=settings,
        oauth_client=oauth_client or UrllibGitLabOAuthClient(settings),
        authorizer=GitLabGroupAuthorizer(
            settings=settings,
            membership_client=resolved_membership_client,
        ),
        session_store=session_store or InMemorySessionStore(),
        oauth_state_store=oauth_state_store or OAuthStateStore(),
    )


def _get_current_session(
    request: Request,
    runtime: AuthRuntime,
) -> SessionRecord | None:
    return runtime.session_store.get_session(
        request.cookies.get(runtime.settings.session_cookie_name)
    )


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


def _connect_project_page(
    session: SessionRecord,
    *,
    error: str | None = None,
) -> str:
    error_html = f"<p>{_escape_html(error)}</p>" if error else ""
    csrf_token = _escape_html(session.csrf_token)
    return _page(
        "Connect Project",
        (
            f"{error_html}"
            '<form action="/projects/connect" method="post">'
            f'<input type="hidden" name="csrf_token" value="{csrf_token}">'
            "<label>Project URL or ID "
            '<input name="project_reference" required></label>'
            "<label>Project token "
            '<input name="project_token" type="password" autocomplete="off" required>'
            "</label>"
            '<button type="submit">Connect project</button>'
            "</form>"
            "<p>Selected pipeline logs, diffs, metadata, and comments may be "
            "processed by OpenAI Codex during triage.</p>"
        ),
    )


def _form_value(form: dict[str, list[str]], field_name: str) -> str:
    return form.get(field_name, [""])[0]


def _escape_html(value: str) -> str:
    return (
        value.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
    )
