"""Tests for refactored GitHub tools."""

from unittest.mock import MagicMock, patch

from pipeline_agent.audit_logger import AuditLogger
from pipeline_agent.tools.github_tools import GitHubTools


def _make_tools() -> GitHubTools:
    return GitHubTools(
        token="test-token",
        repository="owner/repo",
        audit=AuditLogger(level="ERROR"),
    )


def test_extract_error_keywords_from_python_traceback():
    log = "ModuleNotFoundError: No module named 'pandas'"
    assert "pandas" in GitHubTools.extract_error_keywords(log)


def test_extract_error_keywords_fallback_line():
    log = "Step failed\nSomething went wrong\nBuild FAILED: unit tests"
    keywords = GitHubTools.extract_error_keywords(log)
    assert "FAILED" in keywords or "failed" in keywords.lower()


def test_format_commits_report_empty():
    report = GitHubTools.format_commits_report([], hours=24)
    assert "No commits found" in report


def test_search_similar_issues_parses_search_api_response():
    tools = _make_tools()
    mock_response = MagicMock()
    mock_response.status_code = 200
    mock_response.json.return_value = {
        "total_count": 1,
        "items": [
            {
                "number": 42,
                "title": "Fix pandas import",
                "state": "closed",
                "html_url": "https://github.com/owner/repo/issues/42",
                "comments": 1,
                "comments_url": "https://api.github.com/repos/owner/repo/issues/42/comments",
            }
        ],
    }

    with patch.object(tools._http, "get", return_value=mock_response) as mock_get:
        mock_get.side_effect = [
            mock_response,
            MagicMock(
                status_code=200,
                json=lambda: [{"body": "Install pandas in requirements.txt"}],
            ),
        ]
        results = tools.search_similar_issues("pandas ModuleNotFoundError")

    assert len(results) == 1
    assert results[0].number == 42
    assert results[0].solution_hint == "Install pandas in requirements.txt"


def test_get_workflow_logs_builds_failed_job_summary():
    tools = _make_tools()

    run_payload = {
        "name": "CI",
        "conclusion": "failure",
        "created_at": "2026-01-01T00:00:00Z",
        "head_branch": "main",
        "head_sha": "abc123",
    }
    jobs_payload = {
        "jobs": [
            {
                "id": 99,
                "name": "test",
                "conclusion": "failure",
                "steps": [{"name": "pytest", "conclusion": "failure"}],
            }
        ]
    }

    run_resp = MagicMock(status_code=200, raise_for_status=lambda: None)
    run_resp.json.return_value = run_payload
    jobs_resp = MagicMock(status_code=200, raise_for_status=lambda: None)
    jobs_resp.json.return_value = jobs_payload

    with patch.object(tools._http, "get", side_effect=[run_resp, jobs_resp]):
        with patch.object(tools, "_download_job_log", return_value="line1\nERROR: boom\nline3"):
            result = tools.get_workflow_logs(12345)

    assert result.workflow_name == "CI"
    assert len(result.failed_jobs) == 1
    assert result.failed_jobs[0].job_name == "test"
    assert "ERROR" in result.failed_jobs[0].log_excerpt
    assert "line3" in result.failed_jobs[0].last_lines
    assert "Failed Job: test" in result.to_summary()
