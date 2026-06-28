"""OpenRouter LLM layer — sole LLM provider."""

from pipeline_monitor.llm.openrouter import OpenRouterConfigurationError, get_llm
from pipeline_monitor.llm.prompts import AGENT_INVESTIGATION_INPUT, INVESTIGATION_SYSTEM_PROMPT

__all__ = [
    "AGENT_INVESTIGATION_INPUT",
    "INVESTIGATION_SYSTEM_PROMPT",
    "OpenRouterConfigurationError",
    "get_llm",
]
