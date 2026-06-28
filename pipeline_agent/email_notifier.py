"""Backward-compatible re-export — use tools.notifications directly."""

from pipeline_agent.tools.notifications import EmailNotifier

__all__ = ["EmailNotifier"]
