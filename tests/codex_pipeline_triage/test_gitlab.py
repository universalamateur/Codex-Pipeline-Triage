"""Tests for the controlled glab executor seam."""

from pathlib import Path
from subprocess import CompletedProcess
from unittest.mock import patch

import pytest

from codex_pipeline_triage.gitlab import (
    GlabApiRequest,
    GlabExecutor,
    GlabExecutorError,
)


def test_glab_api_uses_controlled_environment(tmp_path: Path) -> None:
    completed = CompletedProcess(
        args=[],
        returncode=0,
        stdout='{"id": 1}',
        stderr="",
    )
    executor = GlabExecutor(
        config_dir=tmp_path,
        hostname="gitlab.example.com",
        timeout_seconds=3,
    )

    with patch(
        "codex_pipeline_triage.gitlab.subprocess.run",
        return_value=completed,
    ) as run_mock:
        result = executor.api(
            GlabApiRequest(endpoint="projects/1"),
            token="secret-token",
        )

    assert result == {"id": 1}
    command = run_mock.call_args.args[0]
    assert command == [
        "glab",
        "api",
        "projects/1",
        "--hostname",
        "gitlab.example.com",
        "--method",
        "GET",
        "--output",
        "json",
    ]
    assert "secret-token" not in command
    env = run_mock.call_args.kwargs["env"]
    assert env["GLAB_CONFIG_DIR"] == str(tmp_path)
    assert env["GITLAB_TOKEN"] == "secret-token"
    assert env["GLAB_NO_PROMPT"] == "true"
    assert "NO_PROMPT" not in env


def test_glab_api_requires_stdout_to_be_json(tmp_path: Path) -> None:
    completed = CompletedProcess(
        args=[],
        returncode=0,
        stdout=(
            "DEPRECATION WARNING: The environment variable NO_PROMPT has been "
            'deprecated.\n{"id": 1}'
        ),
        stderr="",
    )
    executor = GlabExecutor(config_dir=tmp_path)

    with patch(
        "codex_pipeline_triage.gitlab.subprocess.run",
        return_value=completed,
    ):
        with pytest.raises(GlabExecutorError, match="invalid JSON"):
            executor.api(GlabApiRequest(endpoint="projects/1"))


def test_glab_api_reports_cli_failures(tmp_path: Path) -> None:
    completed = CompletedProcess(
        args=[],
        returncode=1,
        stdout="",
        stderr="401 unauthorized",
    )
    executor = GlabExecutor(config_dir=tmp_path)

    with patch(
        "codex_pipeline_triage.gitlab.subprocess.run",
        return_value=completed,
    ):
        with pytest.raises(GlabExecutorError, match="401 unauthorized"):
            executor.api(GlabApiRequest(endpoint="projects/1"))


def test_glab_api_rejects_non_api_path(tmp_path: Path) -> None:
    executor = GlabExecutor(config_dir=tmp_path)

    with pytest.raises(ValueError, match="GitLab API path"):
        executor.api(GlabApiRequest(endpoint="https://gitlab.com/api/v4/projects"))
