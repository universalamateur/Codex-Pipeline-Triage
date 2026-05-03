"""Probe the experimental Codex Python SDK for Spike 1.1."""

from __future__ import annotations

import argparse
import asyncio
import importlib
import sys
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field, ValidationError


class ProbeEvidenceItem(BaseModel):
    """Minimal evidence item matching the future triage shape."""

    source: Literal["pipeline", "job_trace", "mr_diff", "commit_diff", "test_report"]
    snippet: str = Field(min_length=1, max_length=400)


class ProbeTriageResult(BaseModel):
    """Small Pydantic gate for Codex JSON output."""

    root_cause_hypothesis: str = Field(min_length=1, max_length=280)
    category: Literal[
        "test-flake",
        "code-bug",
        "infra",
        "config",
        "dependency",
        "unknown",
    ]
    confidence: float = Field(ge=0, le=1)
    evidence: list[ProbeEvidenceItem] = Field(max_length=5)
    retry_safe: bool
    recommended_action: Literal[
        "recommend_only",
        "retry_job",
        "retry_pipeline",
        "create_fix_mr",
    ]
    suggested_fix: str = Field(min_length=1, max_length=800)
    needs_human_review: bool


async def run_probe(model: str, codex_bin: Path | None) -> ProbeTriageResult:
    """Run one real Codex prompt through the experimental Python SDK."""
    sdk = importlib.import_module("codex_app_server")
    async_codex = getattr(sdk, "AsyncCodex")
    app_server_config = getattr(sdk, "AppServerConfig")
    config = None
    if codex_bin is not None:
        config = app_server_config(codex_bin=str(codex_bin))

    prompt = (
        "Return only JSON for a synthetic failed GitLab pipeline. "
        "Use category unknown, recommended_action recommend_only, "
        "one evidence item with source pipeline, and no markdown."
    )

    async with async_codex(config=config) as codex:
        thread = await codex.thread_start(model=model)
        result = await thread.run(prompt)

    final_response = getattr(result, "final_response", "")
    if not isinstance(final_response, str) or not final_response.strip():
        raise RuntimeError("Codex returned an empty final_response")

    return ProbeTriageResult.model_validate_json(final_response)


async def run_probe_with_timeout(
    model: str, timeout_seconds: float, codex_bin: Path | None
) -> ProbeTriageResult:
    """Bound the full SDK lifecycle, including context-manager shutdown."""
    return await asyncio.wait_for(
        run_probe(model=model, codex_bin=codex_bin), timeout=timeout_seconds
    )


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--model", default="gpt-5.4")
    parser.add_argument("--timeout-seconds", type=float, default=60.0)
    parser.add_argument("--codex-bin", type=Path)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    try:
        result = asyncio.run(
            run_probe_with_timeout(
                model=args.model,
                timeout_seconds=args.timeout_seconds,
                codex_bin=args.codex_bin,
            )
        )
    except ModuleNotFoundError as exc:
        if exc.name == "codex_app_server":
            print("blocked: codex_app_server is not importable", file=sys.stderr)
            return 2
        raise
    except asyncio.TimeoutError:
        print("blocked: Codex Python SDK probe timed out", file=sys.stderr)
        return 3
    except ValidationError as exc:
        print(
            f"blocked: Codex output failed Pydantic validation: {exc}", file=sys.stderr
        )
        return 4
    except RuntimeError as exc:
        print(f"blocked: {exc}", file=sys.stderr)
        return 5

    print(result.model_dump_json())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
