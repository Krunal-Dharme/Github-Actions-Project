"""Tests for investigate_failure structured response (§8)."""

from pipeline_agent.report_generator import (
    build_investigation_result,
    extract_risk_level,
    extract_root_cause,
)


def test_build_investigation_result_contract():
    result = build_investigation_result(
        success=True,
        workflow_run_id="12345",
        analysis="# Report",
        root_cause="Missing dependency",
        risk_level="Low",
        issue_number=42,
    )
    assert result["success"] is True
    assert result["workflow_run_id"] == "12345"
    assert result["analysis"] == "# Report"
    assert result["root_cause"] == "Missing dependency"
    assert result["risk_level"] == "Low"
    assert result["error"] is None
    assert result["issue_number"] == 42


def test_extract_risk_level_from_llm_output():
    text = "**Root Cause**: foo\n**Risk Level**: Medium\n**Requires Human Approval**: No"
    assert extract_risk_level(text) == "Medium"


def test_extract_risk_level_unknown_when_missing():
    assert extract_risk_level("no risk here") == "Unknown"


def test_extract_root_cause_and_risk_together():
    analysis = (
        "**Root Cause**: Docker build failed due to missing base image\n"
        "**Risk Level**: [High]\n"
    )
    assert "Docker build failed" in extract_root_cause(analysis)
    assert extract_risk_level(analysis) == "High"
