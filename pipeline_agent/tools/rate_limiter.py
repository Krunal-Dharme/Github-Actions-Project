"""Rate limiting and cost controls for LLM usage and investigations."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

from pipeline_agent.audit_logger import AuditAction, AuditLogger


@dataclass
class RateLimitState:
    investigations_today: int = 0
    llm_calls_this_run: int = 0
    last_reset: datetime = field(default_factory=lambda: datetime.now(UTC))


class RateLimiter:
    """In-memory rate limiter; persisted counts come from Postgres when enabled."""

    def __init__(
        self,
        max_investigations_per_day: int,
        max_llm_calls_per_run: int,
        audit: AuditLogger,
    ) -> None:
        self.max_investigations_per_day = max_investigations_per_day
        self.max_llm_calls_per_run = max_llm_calls_per_run
        self.audit = audit
        self._state = RateLimitState()

    def sync_daily_count(self, count: int) -> None:
        self._state.investigations_today = count

    def can_investigate(self, workflow_run_id: int) -> bool:
        if self._state.investigations_today >= self.max_investigations_per_day:
            self.audit.log(
                AuditAction.RATE_LIMIT_HIT,
                workflow_run_id=workflow_run_id,
                details={
                    "limit": "daily_investigations",
                    "count": self._state.investigations_today,
                    "max": self.max_investigations_per_day,
                },
                level="WARNING",
            )
            return False
        return True

    def record_investigation(self) -> None:
        self._state.investigations_today += 1

    def can_call_llm(self, workflow_run_id: int) -> bool:
        if self._state.llm_calls_this_run >= self.max_llm_calls_per_run:
            self.audit.log(
                AuditAction.RATE_LIMIT_HIT,
                workflow_run_id=workflow_run_id,
                details={
                    "limit": "llm_calls_per_run",
                    "count": self._state.llm_calls_this_run,
                    "max": self.max_llm_calls_per_run,
                },
                level="WARNING",
            )
            return False
        return True

    def record_llm_call(self) -> None:
        self._state.llm_calls_this_run += 1

    @staticmethod
    def is_within_dedupe_window(
        last_investigated_at: datetime | None,
        window_hours: int,
    ) -> bool:
        if last_investigated_at is None:
            return False
        cutoff = datetime.now(UTC) - timedelta(hours=window_hours)
        if last_investigated_at.tzinfo is None:
            last_investigated_at = last_investigated_at.replace(tzinfo=UTC)
        return last_investigated_at > cutoff
