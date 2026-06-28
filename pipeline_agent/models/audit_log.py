"""Audit log event models."""

from __future__ import annotations

from enum import Enum
from typing import Any, TypedDict


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


class AuditLogEntry(TypedDict, total=False):
    timestamp: str
    event_type: str
    action: str
    workflow_run_id: int | None
    repository: str | None
    actor: str | None
    details: dict[str, Any]
    security_event: str
    tool: str
    duration_seconds: float
