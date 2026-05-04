"""Run the Spike 6.1 Codex adapter against a persisted pipeline context."""

# flake8: noqa: E402
# isort: skip_file

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from codex_pipeline_triage.codex_adapter import (
    build_default_codex_adapter,
)  # noqa: E402
from codex_pipeline_triage.persistence import SqliteStore  # noqa: E402


async def run(args: argparse.Namespace) -> dict[str, object]:
    """Load one stored context and run the server-side Codex adapter."""
    store = SqliteStore(args.db_path)
    triage_run = store.get_triage_run_by_pipeline(
        gitlab_project_id=args.project_id,
        pipeline_id=args.pipeline_id,
    )
    if triage_run is None:
        raise RuntimeError("triage run was not found")
    if triage_run.context_json is None:
        raise RuntimeError("triage run does not have persisted context_json")

    adapter = build_default_codex_adapter(
        model=args.model,
        timeout_seconds=args.timeout_seconds,
        codex_bin=args.codex_bin,
    )
    outcome = await adapter.triage(triage_run.context_json)
    return {
        "adapter_mode": outcome.adapter_mode,
        "fallback_reason": outcome.fallback_reason,
        "triage_result": outcome.triage_result.model_dump(mode="json"),
    }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--db-path", type=Path, default=Path(".local/triage.sqlite"))
    parser.add_argument("--project-id", type=int, required=True)
    parser.add_argument("--pipeline-id", type=int, required=True)
    parser.add_argument("--model", default="gpt-5.4")
    parser.add_argument("--timeout-seconds", type=float, default=90.0)
    parser.add_argument("--codex-bin", type=Path)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    try:
        result = asyncio.run(run(args))
    except RuntimeError as exc:
        print(f"blocked: {exc}", file=sys.stderr)
        return 2
    print(json.dumps(result, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
