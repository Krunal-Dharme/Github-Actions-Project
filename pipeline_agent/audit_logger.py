"""Structured audit logging for security compliance and debugging."""

from __future__ import annotations

import json
import logging
import sys
from datetime import UTC, datetime
from enum import Enum
from pathlib import Path
from typing import Any, Protocol

logger = logging.getLogger(__name__)


class AuditAction(str, Enum):
    INVESTIGATION_STARTED = "investigation_started"
    INVESTIGATION_COMPLETED = "investigation_completed"
    INVESTIGATION_SKIPPED = "investigation_skipped"
    GITHUB_FETCH = "github_fetch"
    LLM_REQUEST = "llm_request"
    LLM_RESPONSE = "llm_response"
    SECRETS_SCAN = "secrets_scan"
    SECRETS_REDACTED = "secrets_redacted"
    ISSUE_CREATED = "issue_created"
    ISSUE_UPDATED = "issue_updated"
    EMAIL_SENT = "email_sent"
    POSTGRES_WRITE = "postgres_write"
    RATE_LIMIT_HIT = "rate_limit_hit"
    DESTRUCTIVE_FLAGGED = "destructive_flagged"
    ERROR = "error"


_SECURITY_ACTIONS = frozenset(
    {
        AuditAction.SECRETS_SCAN,
        AuditAction.SECRETS_REDACTED,
        AuditAction.DESTRUCTIVE_FLAGGED,
        AuditAction.RATE_LIMIT_HIT,
    }
)


class AuditEventWriter(Protocol):
    """Optional long-term audit storage (e.g. Postgres on GCP)."""

    def save_audit_event(self, entry: dict[str, Any]) -> None: ...


class AuditLogger:
    """Log all agent actions for security auditing.

    Writes to:
    - JSONL files under ``log_dir`` (audit_*.jsonl, security_*.jsonl)
    - stdout (captured by GitHub Actions)
    - Optional Postgres when ``set_postgres_store`` is configured
    """

    def __init__(
        self,
        log_dir: str = ".agent_logs",
        level: str = "INFO",
        *,
        postgres_audit_enabled: bool = True,
    ) -> None:
        self.log_dir = Path(log_dir)
        self.log_dir.mkdir(parents=True, exist_ok=True)
        self._postgres_audit_enabled = postgres_audit_enabled
        self._postgres_store: AuditEventWriter | None = None

        self._stdout = logging.getLogger("pipeline_agent.audit")
        self._stdout.setLevel(getattr(logging, level.upper(), logging.INFO))
        if not self._stdout.handlers:
            handler = logging.StreamHandler(sys.stderr)
            handler.setFormatter(logging.Formatter("%(message)s"))
            self._stdout.addHandler(handler)

    def set_postgres_store(self, store: AuditEventWriter | None) -> None:
        """Attach Postgres store for long-term audit trail."""
        self._postgres_store = store

    @staticmethod
    def _utcnow() -> datetime:
        return datetime.now(UTC)

    @staticmethod
    def _date_stamp(when: datetime | None = None) -> str:
        return (when or datetime.now(UTC)).strftime("%Y-%m-%d")

    def _write_entry(self, filename: str, entry: dict[str, Any]) -> None:
        log_file = self.log_dir / filename
        with open(log_file, "a", encoding="utf-8") as f:
            f.write(json.dumps(entry, default=str) + "\n")
        self._stdout.info(json.dumps(entry, default=str))
        self._persist_to_postgres(entry)

    def _persist_to_postgres(self, entry: dict[str, Any]) -> None:
        if not self._postgres_audit_enabled or self._postgres_store is None:
            return
        try:
            self._postgres_store.save_audit_event(entry)
        except Exception as exc:
            logger.warning("Failed to persist audit event to Postgres: %s", exc)

    def log_investigation(self, event_data: dict[str, Any]) -> None:
        """Log investigation lifecycle events."""
        when = self._utcnow()
        log_entry = {
            "timestamp": when.isoformat(),
            "event_type": "investigation",
            **event_data,
        }
        filename = f"audit_{self._date_stamp(when)}.jsonl"
        self._write_entry(filename, log_entry)

    def log_tool_call(
        self,
        tool_name: str,
        args: dict[str, Any],
        result: Any,
        duration: float,
    ) -> None:
        """Log GitHub tool invocations with timing."""
        when = self._utcnow()
        log_entry = {
            "timestamp": when.isoformat(),
            "event_type": "tool_call",
            "tool": tool_name,
            "arguments": args,
            "result_preview": str(result)[:200],
            "duration_seconds": round(duration, 4),
        }
        filename = f"audit_{self._date_stamp(when)}.jsonl"
        self._write_entry(filename, log_entry)

    def log_security_event(self, event_type: str, details: dict[str, Any]) -> None:
        """Log security-related events (secrets, rate limits, destructive flags)."""
        when = self._utcnow()
        log_entry = {
            "timestamp": when.isoformat(),
            "event_type": "security",
            "security_event": event_type,
            **details,
        }
        filename = f"security_{self._date_stamp(when)}.jsonl"
        self._write_entry(filename, log_entry)

    def log(
        self,
        action: AuditAction,
        *,
        workflow_run_id: int | None = None,
        repository: str | None = None,
        actor: str | None = None,
        details: dict[str, Any] | None = None,
        level: str = "INFO",
    ) -> None:
        """Unified audit entry — routes to investigation or security log methods."""
        payload = {
            "action": action.value,
            "workflow_run_id": workflow_run_id,
            "repository": repository,
            "actor": actor,
            "details": details or {},
            "level": level,
        }
        if action in _SECURITY_ACTIONS:
            self.log_security_event(action.value, payload)
        else:
            self.log_investigation(payload)
