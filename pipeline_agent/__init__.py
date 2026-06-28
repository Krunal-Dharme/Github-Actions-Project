"""AI Pipeline Health Monitor — GitHub Actions failure investigation agent."""

from pipeline_agent.agent_investigator import (
    AgentInvestigator,
    Investigator,
    handle_success,
    investigate_failure,
)

__version__ = "1.0.0"
__all__ = [
    "AgentInvestigator",
    "Investigator",
    "handle_success",
    "investigate_failure",
    "__version__",
]
