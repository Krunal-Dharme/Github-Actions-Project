"""OpenRouter LLM client — sole LLM provider via LangChain ChatOpenAI."""

from __future__ import annotations

from dataclasses import dataclass

from langchain_core.messages import HumanMessage, SystemMessage
from tenacity import retry, stop_after_attempt, wait_exponential

from pipeline_monitor.agent.executor import run_tools_agent
from pipeline_monitor.audit_logger import AuditAction, AuditLogger
from pipeline_monitor.llm.openrouter import get_llm
from pipeline_monitor.llm.prompts import INVESTIGATION_SYSTEM_PROMPT
from pipeline_monitor.tools.langchain_tools import build_github_langchain_tools
from pipeline_monitor.tools.github_tools import GitHubTools


@dataclass
class LLMResponse:
    content: str
    model: str
    prompt_tokens: int
    completion_tokens: int
    total_tokens: int


SUCCESS_SYSTEM_PROMPT = """Summarize this successful pipeline run in 2-3 sentences.
Include: workflow name, branch, triggering actor, and key jobs that ran.
Keep it brief — no deep analysis needed."""


class OpenRouterClient:
    """OpenRouter-only LLM client. No OpenAI API fallback."""

    def __init__(
        self,
        api_key: str,
        model: str,
        audit: AuditLogger,
        base_url: str = "https://openrouter.ai/api/v1",
        max_tokens: int = 2048,
        temperature: float = 0.0,
        referer: str = "https://github.com/pipeline-health-monitor",
        agent_max_iterations: int = 3,
    ) -> None:
        self.model = model
        self.audit = audit
        self.max_tokens = max_tokens
        self.agent_max_iterations = agent_max_iterations
        self._llm = get_llm(
            api_key=api_key,
            model_name=model,
            temperature=temperature,
            base_url=base_url,
            referer=referer,
            max_tokens=max_tokens,
        )

    @retry(stop=stop_after_attempt(3), wait=wait_exponential(multiplier=1, min=2, max=30))
    def analyze_failure(
        self,
        context_md: str,
        workflow_run_id: int,
    ) -> LLMResponse:
        """Single-call analysis (V1 fast path): evidence pre-gathered, one LLM request."""
        return self._invoke(
            system=INVESTIGATION_SYSTEM_PROMPT,
            user=context_md,
            workflow_run_id=workflow_run_id,
        )

    @retry(stop=stop_after_attempt(2), wait=wait_exponential(multiplier=1, min=2, max=15))
    def summarize_success(
        self,
        context_md: str,
        workflow_run_id: int,
    ) -> LLMResponse:
        return self._invoke(
            system=SUCCESS_SYSTEM_PROMPT,
            user=context_md,
            workflow_run_id=workflow_run_id,
            max_tokens=256,
        )

    def analyze_with_agent(
        self,
        agent_input: str,
        workflow_run_id: int,
        github_tools: GitHubTools,
        *,
        commit_lookback_hours: int = 24,
        max_commits: int = 10,
    ) -> LLMResponse:
        """Agent mode: LLM calls GitHub tools dynamically via LangChain AgentExecutor."""
        tools = build_github_langchain_tools(
            github_tools,
            workflow_run_id=workflow_run_id,
            commit_lookback_hours=commit_lookback_hours,
            max_commits=max_commits,
        )

        self.audit.log(
            AuditAction.LLM_REQUEST,
            workflow_run_id=workflow_run_id,
            details={
                "model": self.model,
                "mode": "langchain_agent",
                "max_iterations": self.agent_max_iterations,
            },
        )

        content = run_tools_agent(
            self._llm,
            tools,
            agent_input,
            max_iterations=self.agent_max_iterations,
        )

        self.audit.log(
            AuditAction.LLM_RESPONSE,
            workflow_run_id=workflow_run_id,
            details={"model": self.model, "mode": "langchain_agent", "output_chars": len(content)},
        )

        return LLMResponse(
            content=content,
            model=self.model,
            prompt_tokens=0,
            completion_tokens=0,
            total_tokens=0,
        )

    def _invoke(
        self,
        system: str,
        user: str,
        workflow_run_id: int,
        max_tokens: int | None = None,
    ) -> LLMResponse:
        self.audit.log(
            AuditAction.LLM_REQUEST,
            workflow_run_id=workflow_run_id,
            details={"model": self.model, "mode": "direct", "prompt_chars": len(user)},
        )

        llm = self._llm
        if max_tokens is not None:
            llm = llm.bind(max_tokens=max_tokens)

        response = llm.invoke([SystemMessage(content=system), HumanMessage(content=user)])
        content = response.content if isinstance(response.content, str) else str(response.content)

        prompt_tokens, completion_tokens, total_tokens = _extract_token_usage(response)

        self.audit.log(
            AuditAction.LLM_RESPONSE,
            workflow_run_id=workflow_run_id,
            details={
                "model": self.model,
                "mode": "direct",
                "prompt_tokens": prompt_tokens,
                "completion_tokens": completion_tokens,
            },
        )

        return LLMResponse(
            content=content,
            model=self.model,
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            total_tokens=total_tokens,
        )

    def close(self) -> None:
        pass


def _extract_token_usage(response: object) -> tuple[int, int, int]:
    usage_meta = getattr(response, "usage_metadata", None) or {}
    if usage_meta:
        inp = int(usage_meta.get("input_tokens", 0) or 0)
        out = int(usage_meta.get("output_tokens", 0) or 0)
        return inp, out, inp + out

    meta = getattr(response, "response_metadata", None) or {}
    usage = meta.get("token_usage") or meta.get("usage") or {}
    inp = int(usage.get("prompt_tokens", 0) or usage.get("input_tokens", 0) or 0)
    out = int(usage.get("completion_tokens", 0) or usage.get("output_tokens", 0) or 0)
    total = int(usage.get("total_tokens", 0) or inp + out)
    return inp, out, total
