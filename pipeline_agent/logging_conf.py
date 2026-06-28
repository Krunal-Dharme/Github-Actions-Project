"""Central logging configuration — operational logs to stderr, JSON result to stdout."""

from __future__ import annotations

import logging
import sys


def configure_logging(level: str = "INFO") -> None:
    """Route application logs to stderr so stdout stays clean for JSON output."""
    level_name = str(level).upper() if level else "INFO"
    numeric_level = getattr(logging, level_name, logging.INFO)

    root = logging.getLogger("pipeline_agent")
    root.setLevel(numeric_level)
    if not root.handlers:
        handler = logging.StreamHandler(sys.stderr)
        handler.setFormatter(
            logging.Formatter("%(asctime)s [%(levelname)s] %(name)s: %(message)s")
        )
        root.addHandler(handler)

    # Ensure audit JSON lines use stderr (see audit_logger.py)
    audit = logging.getLogger("pipeline_agent.audit")
    audit.setLevel(numeric_level)
