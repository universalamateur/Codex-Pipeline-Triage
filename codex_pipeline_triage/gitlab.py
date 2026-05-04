"""Deterministic GitLab CLI executor seam."""

from __future__ import annotations

import json
import os
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal, Mapping, cast

JsonResponse = dict[str, Any] | list[Any]
HttpMethod = Literal["GET", "POST", "PUT", "PATCH", "DELETE"]


class GlabExecutorError(RuntimeError):
    """Raised when the controlled glab executor cannot complete a request."""


@dataclass(frozen=True)
class GlabApiRequest:
    """A constrained GitLab API request for the glab wrapper."""

    endpoint: str
    method: HttpMethod = "GET"
    fields: Mapping[str, str] | None = None


@dataclass(frozen=True)
class GlabExecutor:
    """Run glab without ambient auth or interactive prompts."""

    config_dir: Path
    glab_bin: str = "glab"
    hostname: str = "gitlab.com"
    timeout_seconds: float = 20.0

    def api(self, request: GlabApiRequest, *, token: str | None = None) -> JsonResponse:
        """Execute a GitLab API request and parse the JSON response."""
        args = self._build_args(request, output="json")
        env = self._build_env(token=token)

        try:
            result = subprocess.run(
                args,
                capture_output=True,
                check=False,
                env=env,
                text=True,
                timeout=self.timeout_seconds,
            )
        except subprocess.TimeoutExpired as exc:
            raise GlabExecutorError("glab api timed out") from exc

        if result.returncode != 0:
            raise GlabExecutorError(result.stderr.strip() or "glab api failed")

        body = result.stdout.strip()
        if not body:
            return {}

        try:
            parsed = json.loads(body)
        except json.JSONDecodeError as exc:
            raise GlabExecutorError("glab api returned invalid JSON") from exc

        return cast(JsonResponse, parsed)

    def api_text(self, request: GlabApiRequest, *, token: str | None = None) -> str:
        """Execute a GitLab API request that returns raw text."""
        args = self._build_args(request, output="text")
        env = self._build_env(token=token)

        try:
            result = subprocess.run(
                args,
                capture_output=True,
                check=False,
                env=env,
                text=True,
                timeout=self.timeout_seconds,
            )
        except subprocess.TimeoutExpired as exc:
            raise GlabExecutorError("glab api timed out") from exc

        if result.returncode != 0:
            raise GlabExecutorError(result.stderr.strip() or "glab api failed")

        return result.stdout

    def _build_args(self, request: GlabApiRequest, *, output: str) -> list[str]:
        endpoint = request.endpoint.strip()
        if not endpoint or endpoint.startswith("-") or "://" in endpoint:
            raise ValueError("endpoint must be a GitLab API path")

        args = [
            self.glab_bin,
            "api",
            endpoint,
            "--hostname",
            self.hostname,
            "--method",
            request.method,
        ]
        if output == "json":
            args.extend(["--output", "json"])

        for key, value in (request.fields or {}).items():
            args.extend(["--field", f"{key}={value}"])

        return args

    def _build_env(self, *, token: str | None) -> dict[str, str]:
        env = {
            "GLAB_CONFIG_DIR": str(self.config_dir),
            "GLAB_NO_PROMPT": "true",
            "PATH": os.environ.get("PATH", ""),
        }
        if token is not None:
            env["GITLAB_TOKEN"] = token
        return env
