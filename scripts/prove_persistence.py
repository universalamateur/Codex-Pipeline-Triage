"""Create and reload demo persistence records for Spike 1.2 manual testing."""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--db-path",
        default=".local/spike-1.2-persistence.sqlite",
        help="SQLite file to create and reopen.",
    )
    return parser.parse_args()


def main() -> int:
    # pylint: disable=import-outside-toplevel
    from codex_pipeline_triage.models import (
        AppUser,
        ConnectedProject,
        ProjectActionPolicy,
    )
    from codex_pipeline_triage.persistence import SqliteStore

    args = parse_args()
    db_path = Path(args.db_path)
    now = datetime.now(tz=timezone.utc)
    user = AppUser(
        id="demo-user",
        gitlab_user_id=1001,
        gitlab_username="demo-user",
        display_name="Demo User",
        created_at=now,
        updated_at=now,
    )
    project = ConnectedProject(
        id="demo-project",
        gitlab_project_id=2002,
        gitlab_project_path="demo/checkout-service",
        display_name="checkout-service",
        token_ciphertext="ref://local-demo-project-token",
        webhook_secret_hash="sha256:local-demo-webhook-placeholder",
        action_policy=ProjectActionPolicy(),
        connected_by_gitlab_user_id=user.gitlab_user_id,
        enabled=True,
        created_at=now,
        updated_at=now,
    )

    store = SqliteStore(db_path)
    if store.get_user(user.id) is None:
        store.create_user(user)
    else:
        store.update_user(user)

    if store.get_connected_project(project.id) is None:
        store.create_connected_project(project)
    else:
        store.update_connected_project(project)

    reopened = SqliteStore(db_path)
    persisted_user = reopened.get_user(user.id)
    persisted_project = reopened.get_connected_project(project.id)
    if persisted_user is None or persisted_project is None:
        raise RuntimeError("persistence proof failed after reopening the store")

    print(
        json.dumps(
            {
                "db_path": str(db_path),
                "persisted_user": persisted_user.gitlab_username,
                "persisted_project": persisted_project.gitlab_project_path,
                "project_enabled": persisted_project.enabled,
            },
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
