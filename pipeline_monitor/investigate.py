"""Unified investigation entry points (§8)."""

from __future__ import annotations

from pipeline_monitor.config import Settings, load_settings
from pipeline_monitor.investigator import Investigator


def investigate_failure(
    workflow_run_id: str,
    settings: Settings | None = None,
) -> dict:
    """Investigate a failed GitHub Actions workflow run."""
    settings = settings or load_settings()
    settings.workflow_run_id = int(workflow_run_id)

    investigator = Investigator(settings)
    try:
        return investigator.investigate_failure(workflow_run_id)
    finally:
        investigator._cleanup()


def handle_success(
    workflow_run_id: str,
    settings: Settings | None = None,
) -> dict:
    """Handle a successful workflow run — lightweight summary, optional email, no LLM."""
    settings = settings or load_settings()
    settings.workflow_run_id = int(workflow_run_id)

    investigator = Investigator(settings)
    try:
        return investigator.handle_success(workflow_run_id)
    finally:
        investigator._cleanup()
