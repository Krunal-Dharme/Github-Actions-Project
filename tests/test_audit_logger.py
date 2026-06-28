"""Tests for audit logger file + Postgres integration."""

import json
from unittest.mock import MagicMock

from pipeline_agent.audit_logger import AuditAction, AuditLogger


def test_log_investigation_writes_jsonl(tmp_path):
    audit = AuditLogger(log_dir=str(tmp_path), postgres_audit_enabled=False)
    audit.log_investigation({"workflow_run_id": 123, "status": "started"})

    files = list(tmp_path.glob("audit_*.jsonl"))
    assert len(files) == 1
    entry = json.loads(files[0].read_text(encoding="utf-8").strip())
    assert entry["event_type"] == "investigation"
    assert entry["workflow_run_id"] == 123


def test_log_tool_call_writes_audit_file(tmp_path):
    audit = AuditLogger(log_dir=str(tmp_path), postgres_audit_enabled=False)
    audit.log_tool_call("get_workflow_logs", {"workflow_run_id": 1}, "log preview", 0.42)

    entry = json.loads(list(tmp_path.glob("audit_*.jsonl"))[0].read_text(encoding="utf-8").strip())
    assert entry["event_type"] == "tool_call"
    assert entry["tool"] == "get_workflow_logs"
    assert entry["duration_seconds"] == 0.42


def test_log_security_event_writes_security_file(tmp_path):
    audit = AuditLogger(log_dir=str(tmp_path), postgres_audit_enabled=False)
    audit.log_security_event("secrets_redacted", {"phase": "llm_output", "findings": ["github_token"]})

    files = list(tmp_path.glob("security_*.jsonl"))
    assert len(files) == 1
    entry = json.loads(files[0].read_text(encoding="utf-8").strip())
    assert entry["event_type"] == "security"
    assert entry["security_event"] == "secrets_redacted"


def test_log_routes_security_actions(tmp_path):
    audit = AuditLogger(log_dir=str(tmp_path), postgres_audit_enabled=False)
    audit.log(
        AuditAction.SECRETS_REDACTED,
        workflow_run_id=99,
        details={"phase": "report"},
    )

    assert list(tmp_path.glob("security_*.jsonl"))
    security_entry = json.loads(list(tmp_path.glob("security_*.jsonl"))[0].read_text(encoding="utf-8").strip())
    assert security_entry["security_event"] == "secrets_redacted"
    assert security_entry["details"]["phase"] == "report"


def test_postgres_audit_persistence(tmp_path):
    store = MagicMock()
    audit = AuditLogger(log_dir=str(tmp_path), postgres_audit_enabled=True)
    audit.set_postgres_store(store)
    audit.log_investigation({"action": "investigation_started", "workflow_run_id": 456})

    store.save_audit_event.assert_called_once()
    payload = store.save_audit_event.call_args[0][0]
    assert payload["event_type"] == "investigation"
    assert payload["workflow_run_id"] == 456
