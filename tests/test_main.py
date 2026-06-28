"""Tests for main CLI routing and JSON stdout."""

import json
from io import StringIO
from unittest.mock import MagicMock, patch

import pytest

from pipeline_agent.main import _emit_result, _exit_code, build_parser, main


def test_cli_accepts_workflow_run_id_and_status():
    parser = build_parser()
    args = parser.parse_args(["--workflow-run-id", "987654321", "--status", "failure"])
    assert args.workflow_run_id == 987654321
    assert args.status == "failure"


def test_cli_status_success():
    parser = build_parser()
    args = parser.parse_args(["-r", "123", "-s", "success"])
    assert args.status == "success"


def test_emit_result_writes_json_to_stdout(capsys):
    _emit_result({"success": True, "workflow_run_id": "1"})
    captured = capsys.readouterr()
    payload = json.loads(captured.out.strip())
    assert payload["success"] is True
    assert captured.err == ""
    assert "[github-debug]" not in captured.out


def test_github_tools_debug_writes_to_stderr_not_stdout(capsys):
    """Regression: debug lines must not pollute investigation.json (stdout redirect)."""
    from pipeline_agent.tools.github_tools import GitHubTools
    from pipeline_agent.audit_logger import AuditLogger

    tools = GitHubTools(
        token="test-token",
        repository="owner/repo",
        audit=AuditLogger(log_dir=".agent_logs"),
    )
    try:
        tools._fetch_workflow_logs(12345)
    except Exception:
        pass
    captured = capsys.readouterr()
    assert "[github-debug]" not in captured.out
    if captured.err:
        assert "[github-debug]" in captured.err
    tools.close()


def test_exit_code_success():
    assert _exit_code({"success": True}) == 0


def test_exit_code_error_skip():
    assert _exit_code({"success": False, "error": "dedupe"}) == 0


@patch("pipeline_agent.main.load_settings")
@patch("pipeline_agent.main.investigate_failure")
@patch("pipeline_agent.main._resolve_run_id_and_status")
def test_main_failure_calls_investigate_failure(mock_resolve, mock_investigate, mock_settings, capsys):
    mock_settings.return_value = MagicMock()
    mock_resolve.return_value = (42, "failure")
    mock_investigate.return_value = {
        "success": True,
        "workflow_run_id": "42",
        "analysis": "# report",
        "root_cause": "test",
        "risk_level": "Low",
        "error": None,
    }

    with pytest.raises(SystemExit) as exc:
        main(["--workflow-run-id", "42", "--status", "failure"])

    assert exc.value.code == 0
    mock_investigate.assert_called_once()
    payload = json.loads(capsys.readouterr().out.strip())
    assert payload["workflow_run_id"] == "42"


@patch("pipeline_agent.main.load_settings")
@patch("pipeline_agent.main.handle_success")
@patch("pipeline_agent.main._resolve_run_id_and_status")
def test_main_success_calls_handle_success(mock_resolve, mock_handle, mock_settings, capsys):
    mock_settings.return_value = MagicMock()
    mock_resolve.return_value = (99, "success")
    mock_handle.return_value = {
        "success": True,
        "workflow_run_id": "99",
        "status": "success",
        "analysis": "Pipeline succeeded",
        "email_sent": False,
        "error": None,
    }

    with pytest.raises(SystemExit) as exc:
        main(["--workflow-run-id", "99", "--status", "success"])

    assert exc.value.code == 0
    mock_handle.assert_called_once()
    payload = json.loads(capsys.readouterr().out.strip())
    assert payload["status"] == "success"
