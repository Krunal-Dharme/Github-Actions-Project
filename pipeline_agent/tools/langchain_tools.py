"""LangChain tool wrappers for GitHubTools (refactored from original @tool snippets)."""

from __future__ import annotations

from langchain_core.tools import StructuredTool

from pipeline_agent.tools.github_tools import GitHubTools


def build_github_langchain_tools(
    github_tools: GitHubTools,
    workflow_run_id: int,
    commit_lookback_hours: int = 24,
    max_commits: int = 10,
) -> list[StructuredTool]:
    """Wrap production GitHubTools as LangChain tools for agent mode."""

    def get_workflow_logs() -> str:
        """Fetch logs from the failed GitHub Actions workflow run under investigation."""
        result = github_tools.get_workflow_logs(workflow_run_id)
        return result.to_summary()

    def analyze_recent_commits(hours: int = commit_lookback_hours) -> str:
        """Analyze recent commits that might have caused the failure."""
        commits = github_tools.analyze_recent_commits(
            hours=hours,
            limit=max_commits,
        )
        return github_tools.format_commits_report(commits, hours)

    def search_similar_issues(error_keywords: str) -> str:
        """Search GitHub issues for similar error messages or problems."""
        issues = github_tools.search_similar_issues(error_keywords)
        return github_tools.format_issues_report(issues, error_keywords)

    return [
        StructuredTool.from_function(
            func=get_workflow_logs,
            name="get_workflow_logs",
            description=(
                "Fetch logs from a failed GitHub Actions workflow run. "
                f"Investigating run ID {workflow_run_id}."
            ),
        ),
        StructuredTool.from_function(
            func=analyze_recent_commits,
            name="analyze_recent_commits",
            description=(
                "Analyze recent commits that might have caused the failure. "
                "Returns author, message, and files changed."
            ),
        ),
        StructuredTool.from_function(
            func=search_similar_issues,
            name="search_similar_issues",
            description=(
                "Search GitHub issues for similar error messages. "
                "Pass keywords extracted from the error log."
            ),
        ),
    ]
