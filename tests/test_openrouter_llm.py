"""Tests for OpenRouter-only LLM setup."""

import pytest

from pipeline_agent.llm.openrouter import OpenRouterConfigurationError, get_llm
from pipeline_agent.llm.prompts import INVESTIGATION_SYSTEM_PROMPT


def test_get_llm_requires_openrouter_key():
    with pytest.raises(OpenRouterConfigurationError, match="OPENROUTER_API_KEY"):
        get_llm(api_key=None)


def test_get_llm_rejects_empty_key():
    with pytest.raises(OpenRouterConfigurationError):
        get_llm(api_key="   ")


def test_get_llm_openrouter_base_url():
    llm = get_llm(api_key="sk-or-v1-test", model_name="anthropic/claude-3.5-sonnet")
    assert str(llm.openai_api_base).rstrip("/") == "https://openrouter.ai/api/v1"
    assert llm.model_name == "anthropic/claude-3.5-sonnet"
    assert llm.temperature == 0.0


def test_investigation_prompt_has_strict_output_format():
    assert "**Root Cause**:" in INVESTIGATION_SYSTEM_PROMPT
    assert "**Requires Human Approval**:" in INVESTIGATION_SYSTEM_PROMPT
    assert "destructive" in INVESTIGATION_SYSTEM_PROMPT.lower()


def test_extract_root_cause_bold_format():
    from pipeline_agent.report_generator import extract_root_cause

    content = "**Root Cause**: Missing dependency in requirements.txt\n\n**Evidence**:\n- pip error"
    assert extract_root_cause(content) == "Missing dependency in requirements.txt"
