"""OpenRouter tools agent — LangChain bind_tools loop (no langchain.agents dependency)."""

from __future__ import annotations

from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage
from langchain_core.tools import BaseTool

from pipeline_monitor.llm.prompts import INVESTIGATION_SYSTEM_PROMPT


def run_tools_agent(
    llm: BaseChatModel,
    tools: list[BaseTool],
    user_input: str,
    *,
    max_iterations: int = 3,
) -> str:
    """Run a tool-calling agent loop using OpenRouter via ChatOpenAI.bind_tools.

    Mirrors the user's ``create_openai_tools_agent`` + ``AgentExecutor`` pattern
    without importing ``langchain.agents`` (better compatibility across Python versions).
    """
    tool_map = {tool.name: tool for tool in tools}
    llm_with_tools = llm.bind_tools(tools)
    messages: list = [
        SystemMessage(content=INVESTIGATION_SYSTEM_PROMPT),
        HumanMessage(content=user_input),
    ]

    for _ in range(max_iterations):
        response = llm_with_tools.invoke(messages)
        if not isinstance(response, AIMessage) or not response.tool_calls:
            content = response.content if hasattr(response, "content") else str(response)
            return content if isinstance(content, str) else str(content)

        messages.append(response)
        for call in response.tool_calls:
            tool = tool_map.get(call["name"])
            if tool is None:
                observation = f"Unknown tool: {call['name']}"
            else:
                observation = tool.invoke(call.get("args") or {})
            messages.append(
                ToolMessage(
                    content=str(observation),
                    tool_call_id=call["id"],
                )
            )

    final = llm.invoke(messages)
    content = final.content if hasattr(final, "content") else str(final)
    return content if isinstance(content, str) else str(content)
