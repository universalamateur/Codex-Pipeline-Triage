.PHONY: format lint typecheck test test-cov run

format:
	uv run black .
	uv run isort .

lint:
	uv run black --check .
	uv run isort --check-only .
	uv run flake8 .
	uv run pylint codex_pipeline_triage tests

typecheck:
	uv run mypy codex_pipeline_triage tests

test:
	uv run pytest

test-cov:
	uv run pytest --cov=codex_pipeline_triage --cov-report=term-missing

run:
	uv run uvicorn codex_pipeline_triage.app:create_app --factory --reload
