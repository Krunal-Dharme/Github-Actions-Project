"""Structured models for investigation results."""

from __future__ import annotations

from typing import Any, TypedDict


class InvestigationResult(TypedDict, total=False):
    """§8 / §13 structured response from investigate_failure."""

    success: bool
    workflow_run_id: str
    status: str
    analysis: str
    root_cause: str
    risk_level: str
    error: str | None
    issue_number: int | None
    email_sent: bool


def build_failure_result(
    *,
    success: bool,
    workflow_run_id: str | int,
    analysis: str = "",
    root_cause: str = "",
    risk_level: str = "Unknown",
    error: str | None = None,
    issue_number: int | None = None,
) -> InvestigationResult:
    return InvestigationResult(
        success=success,
        workflow_run_id=str(workflow_run_id),
        analysis=analysis,
        root_cause=root_cause,
        risk_level=risk_level,
        error=error,
        issue_number=issue_number,
    )


def build_success_result(
    *,
    success: bool,
    workflow_run_id: str | int,
    analysis: str = "",
    email_sent: bool = False,
    error: str | None = None,
) -> InvestigationResult:
    return InvestigationResult(
        success=success,
        workflow_run_id=str(workflow_run_id),
        status="success",
        analysis=analysis,
        email_sent=email_sent,
        error=error,
    )
