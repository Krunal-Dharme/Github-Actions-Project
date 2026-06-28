"""Optional Postgres persistence on GCP for investigation history."""

from __future__ import annotations

import json
from contextlib import contextmanager
from datetime import datetime
from typing import Any, Generator

import psycopg
from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool

from pipeline_agent.audit_logger import AuditAction, AuditLogger


SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS pipeline_investigations (
    id              BIGSERIAL PRIMARY KEY,
    workflow_run_id BIGINT NOT NULL,
    repository      TEXT NOT NULL,
    workflow_name   TEXT NOT NULL,
    conclusion      TEXT NOT NULL,
    actor           TEXT,
    head_sha        TEXT,
    head_branch     TEXT,
    root_cause      TEXT,
    report_md       TEXT,
    issue_number    INTEGER,
    llm_model       TEXT,
    prompt_tokens   INTEGER DEFAULT 0,
    completion_tokens INTEGER DEFAULT 0,
    investigated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    metadata        JSONB DEFAULT '{}'::jsonb,
    UNIQUE (repository, workflow_run_id)
);

CREATE INDEX IF NOT EXISTS idx_investigations_repo_time
    ON pipeline_investigations (repository, investigated_at DESC);

CREATE INDEX IF NOT EXISTS idx_investigations_conclusion
    ON pipeline_investigations (repository, conclusion, investigated_at DESC);

CREATE TABLE IF NOT EXISTS pipeline_audit_events (
    id              BIGSERIAL PRIMARY KEY,
    recorded_at     TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    event_type      TEXT NOT NULL,
    workflow_run_id BIGINT,
    repository      TEXT,
    security_event  TEXT,
    payload         JSONB NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_audit_events_time
    ON pipeline_audit_events (recorded_at DESC);

CREATE INDEX IF NOT EXISTS idx_audit_events_type
    ON pipeline_audit_events (event_type, recorded_at DESC);

CREATE INDEX IF NOT EXISTS idx_audit_events_repo_run
    ON pipeline_audit_events (repository, workflow_run_id, recorded_at DESC);
"""


class PostgresStore:
    def __init__(self, dsn: str, audit: AuditLogger) -> None:
        self.audit = audit
        self._pool = ConnectionPool(
            conninfo=dsn,
            min_size=1,
            max_size=3,
            kwargs={"row_factory": dict_row},
        )
        self._ensure_schema()

    def _ensure_schema(self) -> None:
        with self._connection() as conn:
            conn.execute(SCHEMA_SQL)
            conn.commit()

    @contextmanager
    def _connection(self) -> Generator[psycopg.Connection, None, None]:
        with self._pool.connection() as conn:
            yield conn

    def count_investigations_today(self, repository: str) -> int:
        with self._connection() as conn:
            row = conn.execute(
                """
                SELECT COUNT(*) AS cnt
                FROM pipeline_investigations
                WHERE repository = %s
                  AND investigated_at >= CURRENT_DATE
                """,
                (repository,),
            ).fetchone()
            return int(row["cnt"]) if row else 0

    def get_last_investigation_time(
        self,
        repository: str,
        workflow_name: str,
        error_fingerprint: str,
    ) -> datetime | None:
        with self._connection() as conn:
            row = conn.execute(
                """
                SELECT investigated_at
                FROM pipeline_investigations
                WHERE repository = %s
                  AND workflow_name = %s
                  AND metadata->>'error_fingerprint' = %s
                ORDER BY investigated_at DESC
                LIMIT 1
                """,
                (repository, workflow_name, error_fingerprint),
            ).fetchone()
            if row and row["investigated_at"]:
                return row["investigated_at"]
        return None

    def get_similar_historical_failures(
        self,
        repository: str,
        workflow_name: str,
        limit: int = 5,
    ) -> list[dict[str, Any]]:
        with self._connection() as conn:
            rows = conn.execute(
                """
                SELECT workflow_run_id, root_cause, investigated_at, head_sha, head_branch
                FROM pipeline_investigations
                WHERE repository = %s
                  AND workflow_name = %s
                  AND conclusion = 'failure'
                ORDER BY investigated_at DESC
                LIMIT %s
                """,
                (repository, workflow_name, limit),
            ).fetchall()
            return [dict(r) for r in rows]

    def save_investigation(
        self,
        *,
        workflow_run_id: int,
        repository: str,
        workflow_name: str,
        conclusion: str,
        actor: str,
        head_sha: str,
        head_branch: str,
        root_cause: str,
        report_md: str,
        issue_number: int | None,
        llm_model: str,
        prompt_tokens: int,
        completion_tokens: int,
        metadata: dict[str, Any] | None = None,
    ) -> None:
        with self._connection() as conn:
            conn.execute(
                """
                INSERT INTO pipeline_investigations (
                    workflow_run_id, repository, workflow_name, conclusion,
                    actor, head_sha, head_branch, root_cause, report_md,
                    issue_number, llm_model, prompt_tokens, completion_tokens, metadata
                ) VALUES (
                    %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s::jsonb
                )
                ON CONFLICT (repository, workflow_run_id)
                DO UPDATE SET
                    root_cause = EXCLUDED.root_cause,
                    report_md = EXCLUDED.report_md,
                    issue_number = EXCLUDED.issue_number,
                    llm_model = EXCLUDED.llm_model,
                    prompt_tokens = EXCLUDED.prompt_tokens,
                    completion_tokens = EXCLUDED.completion_tokens,
                    metadata = EXCLUDED.metadata,
                    investigated_at = NOW()
                """,
                (
                    workflow_run_id,
                    repository,
                    workflow_name,
                    conclusion,
                    actor,
                    head_sha,
                    head_branch,
                    root_cause,
                    report_md,
                    issue_number,
                    llm_model,
                    prompt_tokens,
                    completion_tokens,
                    json.dumps(metadata or {}),
                ),
            )
            conn.commit()

        self.audit.log(
            AuditAction.POSTGRES_WRITE,
            workflow_run_id=workflow_run_id,
            repository=repository,
            details={"table": "pipeline_investigations"},
        )

    def save_audit_event(self, entry: dict[str, Any]) -> None:
        """Persist audit/security/tool events for long-term audit trail."""
        workflow_run_id = entry.get("workflow_run_id")
        if workflow_run_id is None:
            details = entry.get("details") or {}
            if isinstance(details, dict):
                workflow_run_id = details.get("workflow_run_id")

        repository = entry.get("repository")
        if repository is None:
            details = entry.get("details") or {}
            if isinstance(details, dict):
                repository = details.get("repository")

        with self._connection() as conn:
            conn.execute(
                """
                INSERT INTO pipeline_audit_events (
                    recorded_at, event_type, workflow_run_id, repository,
                    security_event, payload
                ) VALUES (
                    COALESCE(%s::timestamptz, NOW()), %s, %s, %s, %s, %s::jsonb
                )
                """,
                (
                    entry.get("timestamp"),
                    entry.get("event_type", "unknown"),
                    workflow_run_id,
                    repository,
                    entry.get("security_event"),
                    json.dumps(entry),
                ),
            )
            conn.commit()

    def close(self) -> None:
        self._pool.close()
