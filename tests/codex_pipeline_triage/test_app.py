"""Tests for the FastAPI application skeleton."""

from fastapi.testclient import TestClient

from codex_pipeline_triage.app import create_app


def test_health_endpoint_returns_ok() -> None:
    with TestClient(create_app()) as client:
        response = client.get("/health")

    assert response.status_code == 200
    assert response.json() == {
        "service": "codex-pipeline-triage",
        "status": "ok",
    }
