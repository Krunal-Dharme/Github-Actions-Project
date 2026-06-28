"""OpenRouter-only LLM initialization (no OpenAI fallback)."""

from __future__ import annotations

from langchain_openai import ChatOpenAI


class OpenRouterConfigurationError(RuntimeError):
    """Raised when OpenRouter is not configured."""


def get_llm(
    *,
    api_key: str | None,
    model_name: str = "anthropic/claude-3.5-sonnet",
    temperature: float = 0.0,
    base_url: str = "https://openrouter.ai/api/v1",
    referer: str = "https://github.com/pipeline-health-monitor",
    max_tokens: int | None = None,
) -> ChatOpenAI:
    """Initialize the LLM using OpenRouter only.

    Uses LangChain's ChatOpenAI with ``openai_api_base`` pointed at OpenRouter.
    There is intentionally no fallback to api.openai.com.
    """
    if not api_key or not api_key.strip():
        raise OpenRouterConfigurationError(
            "OPENROUTER_API_KEY is required. This project uses OpenRouter only."
        )

    kwargs: dict = {
        "model": model_name,
        "openai_api_key": api_key,
        "openai_api_base": base_url.rstrip("/"),
        "temperature": temperature,
        "default_headers": {
            "HTTP-Referer": referer,
            "X-Title": "Pipeline Health Monitor Agent",
        },
    }
    if max_tokens is not None:
        kwargs["max_tokens"] = max_tokens

    return ChatOpenAI(**kwargs)
