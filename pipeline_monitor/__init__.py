"""AI Pipeline Health Monitor — GitHub Actions failure investigation agent."""

from pipeline_monitor.investigate import handle_success, investigate_failure

__version__ = "1.0.0"
__all__ = ["handle_success", "investigate_failure", "__version__"]