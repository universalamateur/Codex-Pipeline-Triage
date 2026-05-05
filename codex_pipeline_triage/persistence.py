"""SQLite-backed persistence boundary for the local demo app."""

from __future__ import annotations

import sqlite3
from collections.abc import Sequence
from contextlib import closing
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol, TypeVar

from pydantic import BaseModel

from codex_pipeline_triage.models import (
    AppUser,
    ConnectedProject,
    GitLabActionLog,
    PipelineMonitor,
    TriageRun,
)

ModelT = TypeVar("ModelT", bound=BaseModel)


class StoreError(RuntimeError):
    """Base error for persistence failures."""


class RecordNotFoundError(StoreError):
    """Raised when an update targets a missing record."""


class PersistenceStore(Protocol):  # pylint: disable=too-many-public-methods
    """Typed persistence operations needed by the app workflow."""

    def create_user(self, user: AppUser) -> AppUser: ...

    def get_user(self, record_id: str) -> AppUser | None: ...

    def update_user(self, user: AppUser) -> AppUser: ...

    def create_connected_project(
        self, connected_project: ConnectedProject
    ) -> ConnectedProject: ...

    def get_connected_project(self, record_id: str) -> ConnectedProject | None: ...

    def update_connected_project(
        self, connected_project: ConnectedProject
    ) -> ConnectedProject: ...

    def list_connected_projects_for_user(
        self, gitlab_user_id: int
    ) -> list[ConnectedProject]: ...

    def create_triage_run(self, triage_run: TriageRun) -> TriageRun: ...

    def get_triage_run(self, record_id: str) -> TriageRun | None: ...

    def update_triage_run(self, triage_run: TriageRun) -> TriageRun: ...

    def list_triage_runs_for_project(
        self, connected_project_id: str
    ) -> list[TriageRun]: ...

    def get_triage_run_by_pipeline(
        self, *, gitlab_project_id: int, pipeline_id: int
    ) -> TriageRun | None: ...

    def create_action_log(self, action_log: GitLabActionLog) -> GitLabActionLog: ...

    def get_action_log(self, record_id: str) -> GitLabActionLog | None: ...

    def update_action_log(self, action_log: GitLabActionLog) -> GitLabActionLog: ...

    def list_action_logs_for_run(self, triage_run_id: str) -> list[GitLabActionLog]: ...

    def create_pipeline_monitor(self, monitor: PipelineMonitor) -> PipelineMonitor: ...

    def get_pipeline_monitor(self, record_id: str) -> PipelineMonitor | None: ...

    def update_pipeline_monitor(self, monitor: PipelineMonitor) -> PipelineMonitor: ...

    def list_pipeline_monitors_for_run(
        self, triage_run_id: str
    ) -> list[PipelineMonitor]: ...

    def list_pipeline_monitors_for_project(
        self, gitlab_project_id: int
    ) -> list[PipelineMonitor]: ...


@dataclass(frozen=True)
class SqliteStore:  # pylint: disable=too-many-public-methods
    """Small SQLite store with one JSON payload table per record type."""

    db_path: Path

    def __post_init__(self) -> None:
        if str(self.db_path) != ":memory:":
            self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._ensure_schema()

    def create_user(self, user: AppUser) -> AppUser:
        self._insert_record(
            "app_users",
            user.id,
            user,
            extra_columns=(("gitlab_user_id", user.gitlab_user_id),),
        )
        return user

    def get_user(self, record_id: str) -> AppUser | None:
        return self._get_record("app_users", record_id, AppUser)

    def update_user(self, user: AppUser) -> AppUser:
        self._update_record(
            "app_users",
            user.id,
            user,
            extra_columns=(("gitlab_user_id", user.gitlab_user_id),),
        )
        return user

    def create_connected_project(
        self, connected_project: ConnectedProject
    ) -> ConnectedProject:
        self._insert_record(
            "connected_projects",
            connected_project.id,
            connected_project,
            extra_columns=(
                (
                    "connected_by_gitlab_user_id",
                    connected_project.connected_by_gitlab_user_id,
                ),
                ("gitlab_project_id", connected_project.gitlab_project_id),
            ),
        )
        return connected_project

    def get_connected_project(self, record_id: str) -> ConnectedProject | None:
        return self._get_record("connected_projects", record_id, ConnectedProject)

    def update_connected_project(
        self, connected_project: ConnectedProject
    ) -> ConnectedProject:
        self._update_record(
            "connected_projects",
            connected_project.id,
            connected_project,
            extra_columns=(
                (
                    "connected_by_gitlab_user_id",
                    connected_project.connected_by_gitlab_user_id,
                ),
                ("gitlab_project_id", connected_project.gitlab_project_id),
            ),
        )
        return connected_project

    def list_connected_projects_for_user(
        self, gitlab_user_id: int
    ) -> list[ConnectedProject]:
        return self._list_records(
            "connected_projects",
            "connected_by_gitlab_user_id",
            gitlab_user_id,
            ConnectedProject,
        )

    def create_triage_run(self, triage_run: TriageRun) -> TriageRun:
        self._insert_record(
            "triage_runs",
            triage_run.id,
            triage_run,
            extra_columns=(
                ("connected_project_id", triage_run.connected_project_id),
                ("gitlab_project_id", triage_run.gitlab_project_id),
                ("pipeline_id", triage_run.pipeline_id),
            ),
        )
        return triage_run

    def get_triage_run(self, record_id: str) -> TriageRun | None:
        return self._get_record("triage_runs", record_id, TriageRun)

    def update_triage_run(self, triage_run: TriageRun) -> TriageRun:
        self._update_record(
            "triage_runs",
            triage_run.id,
            triage_run,
            extra_columns=(
                ("connected_project_id", triage_run.connected_project_id),
                ("gitlab_project_id", triage_run.gitlab_project_id),
                ("pipeline_id", triage_run.pipeline_id),
            ),
        )
        return triage_run

    def list_triage_runs_for_project(
        self, connected_project_id: str
    ) -> list[TriageRun]:
        return self._list_records(
            "triage_runs",
            "connected_project_id",
            connected_project_id,
            TriageRun,
        )

    def get_triage_run_by_pipeline(
        self, *, gitlab_project_id: int, pipeline_id: int
    ) -> TriageRun | None:
        with closing(self._connect()) as connection:
            row = connection.execute(
                """
                SELECT payload
                FROM triage_runs
                WHERE gitlab_project_id = ? AND pipeline_id = ?
                ORDER BY id
                LIMIT 1
                """,
                (gitlab_project_id, pipeline_id),
            ).fetchone()

        if row is None:
            return None
        return TriageRun.model_validate_json(row["payload"])

    def create_action_log(self, action_log: GitLabActionLog) -> GitLabActionLog:
        self._insert_record(
            "gitlab_action_logs",
            action_log.id,
            action_log,
            extra_columns=(
                ("triage_run_id", action_log.triage_run_id),
                ("idempotency_key", action_log.idempotency_key),
            ),
        )
        return action_log

    def get_action_log(self, record_id: str) -> GitLabActionLog | None:
        return self._get_record("gitlab_action_logs", record_id, GitLabActionLog)

    def update_action_log(self, action_log: GitLabActionLog) -> GitLabActionLog:
        self._update_record(
            "gitlab_action_logs",
            action_log.id,
            action_log,
            extra_columns=(
                ("triage_run_id", action_log.triage_run_id),
                ("idempotency_key", action_log.idempotency_key),
            ),
        )
        return action_log

    def list_action_logs_for_run(self, triage_run_id: str) -> list[GitLabActionLog]:
        return self._list_records(
            "gitlab_action_logs",
            "triage_run_id",
            triage_run_id,
            GitLabActionLog,
        )

    def create_pipeline_monitor(self, monitor: PipelineMonitor) -> PipelineMonitor:
        self._insert_record(
            "pipeline_monitors",
            monitor.id,
            monitor,
            extra_columns=(("triage_run_id", monitor.triage_run_id),),
        )
        return monitor

    def get_pipeline_monitor(self, record_id: str) -> PipelineMonitor | None:
        return self._get_record("pipeline_monitors", record_id, PipelineMonitor)

    def update_pipeline_monitor(self, monitor: PipelineMonitor) -> PipelineMonitor:
        self._update_record(
            "pipeline_monitors",
            monitor.id,
            monitor,
            extra_columns=(("triage_run_id", monitor.triage_run_id),),
        )
        return monitor

    def list_pipeline_monitors_for_run(
        self, triage_run_id: str
    ) -> list[PipelineMonitor]:
        return self._list_records(
            "pipeline_monitors",
            "triage_run_id",
            triage_run_id,
            PipelineMonitor,
        )

    def list_pipeline_monitors_for_project(
        self, gitlab_project_id: int
    ) -> list[PipelineMonitor]:
        return [
            monitor
            for monitor in self._list_all_records(
                "pipeline_monitors",
                PipelineMonitor,
            )
            if monitor.gitlab_project_id == gitlab_project_id
        ]

    def _ensure_schema(self) -> None:
        schema = (
            """
            CREATE TABLE IF NOT EXISTS app_users (
                id TEXT PRIMARY KEY,
                gitlab_user_id INTEGER NOT NULL UNIQUE,
                payload TEXT NOT NULL
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS connected_projects (
                id TEXT PRIMARY KEY,
                connected_by_gitlab_user_id INTEGER NOT NULL,
                gitlab_project_id INTEGER NOT NULL,
                payload TEXT NOT NULL
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS triage_runs (
                id TEXT PRIMARY KEY,
                connected_project_id TEXT NOT NULL,
                gitlab_project_id INTEGER NOT NULL,
                pipeline_id INTEGER NOT NULL,
                payload TEXT NOT NULL
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS gitlab_action_logs (
                id TEXT PRIMARY KEY,
                triage_run_id TEXT NOT NULL,
                idempotency_key TEXT NOT NULL,
                payload TEXT NOT NULL
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS pipeline_monitors (
                id TEXT PRIMARY KEY,
                triage_run_id TEXT NOT NULL,
                payload TEXT NOT NULL
            )
            """,
        )
        with closing(self._connect()) as connection:
            with connection:
                for statement in schema:
                    connection.execute(statement)

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.db_path)
        connection.row_factory = sqlite3.Row
        return connection

    def _insert_record(
        self,
        table: str,
        record_id: str,
        record: BaseModel,
        *,
        extra_columns: Sequence[tuple[str, str | int]] = (),
    ) -> None:
        columns = ["id", *(column for column, _ in extra_columns), "payload"]
        placeholders = ", ".join("?" for _ in columns)
        values: list[str | int] = [
            record_id,
            *(value for _, value in extra_columns),
            record.model_dump_json(),
        ]
        statement = (
            f"INSERT INTO {table} ({', '.join(columns)}) VALUES ({placeholders})"
        )
        with closing(self._connect()) as connection:
            with connection:
                connection.execute(statement, values)

    def _get_record(
        self,
        table: str,
        record_id: str,
        model_type: type[ModelT],
    ) -> ModelT | None:
        with closing(self._connect()) as connection:
            row = connection.execute(
                f"SELECT payload FROM {table} WHERE id = ?",
                (record_id,),
            ).fetchone()

        if row is None:
            return None
        return model_type.model_validate_json(row["payload"])

    def _update_record(
        self,
        table: str,
        record_id: str,
        record: BaseModel,
        *,
        extra_columns: Sequence[tuple[str, str | int]] = (),
    ) -> None:
        assignments = [f"{column} = ?" for column, _ in extra_columns]
        assignments.append("payload = ?")
        values: list[str | int] = [
            *(value for _, value in extra_columns),
            record.model_dump_json(),
            record_id,
        ]
        statement = f"UPDATE {table} SET {', '.join(assignments)} WHERE id = ?"
        with closing(self._connect()) as connection:
            with connection:
                cursor = connection.execute(statement, values)

        if cursor.rowcount == 0:
            raise RecordNotFoundError(f"{table} record not found: {record_id}")

    def _list_records(
        self,
        table: str,
        filter_column: str,
        filter_value: str | int,
        model_type: type[ModelT],
    ) -> list[ModelT]:
        with closing(self._connect()) as connection:
            rows = connection.execute(
                f"""
                SELECT payload
                FROM {table}
                WHERE {filter_column} = ?
                ORDER BY id
                """,
                (filter_value,),
            ).fetchall()

        return [model_type.model_validate_json(row["payload"]) for row in rows]

    def _list_all_records(
        self,
        table: str,
        model_type: type[ModelT],
    ) -> list[ModelT]:
        with closing(self._connect()) as connection:
            rows = connection.execute(f"""
                SELECT payload
                FROM {table}
                ORDER BY id
                """).fetchall()

        return [model_type.model_validate_json(row["payload"]) for row in rows]
