"""Tests for secrets scanner, safety checker, and rate limiter."""

from datetime import UTC, datetime, timedelta

from pipeline_agent.audit_logger import AuditLogger
from pipeline_agent.safety import SafetyChecker
from pipeline_agent.tools.rate_limiter import RateLimiter
from pipeline_agent.tools.secrets_scanner import SecretsScanner, safe_output


def test_secrets_scanner_redacts_github_token():
    text = "Error: auth failed with ghp_abcdefghijklmnopqrstuvwxyz1234567890"
    had, types = SecretsScanner.scan(text)
    assert had
    assert "github_token" in types
    redacted = SecretsScanner.redact(text)
    assert "ghp_" not in redacted
    assert "[REDACTED:GITHUB_TOKEN]" in redacted


def test_secrets_scanner_clean_text():
    text = "Build failed: npm test returned exit code 1"
    had, types = SecretsScanner.scan(text)
    assert not had
    assert SecretsScanner.redact(text) == text


def test_safe_output_redacts_and_returns_clean_text():
    text = "password=\"supersecret123\""
    result = safe_output(text)
    assert "supersecret123" not in result
    assert "[REDACTED:PASSWORD]" in result


def test_safe_output_passes_through_clean_text():
    text = "Root cause: unit test timeout"
    assert safe_output(text) == text


def test_scan_and_redact_wrapper():
    scanner = SecretsScanner()
    text = "postgres://admin:secretpass@db.example.com:5432/app"
    result = scanner.scan_and_redact(text)
    assert result.had_secrets
    assert "connection_string" in result.findings
    assert "[REDACTED:CONNECTION_STRING]" in result.redacted


def test_safety_checker_flags_kubectl_delete():
    checker = SafetyChecker()
    text = "Run: kubectl delete pod my-pod -n production"
    report = checker.scan_recommendations(text)
    assert report.has_destructive
    assert any(c["category"] == "kubectl_delete" for c in report.flagged_commands)


def test_safety_checker_allows_safe_commands():
    checker = SafetyChecker()
    text = "Run: pip install -r requirements.txt\nRun: pytest tests/"
    report = checker.scan_recommendations(text)
    assert not report.has_destructive


def test_rate_limiter_daily_cap():
    audit = AuditLogger()
    limiter = RateLimiter(max_investigations_per_day=2, max_llm_calls_per_run=2, audit=audit)
    assert limiter.can_investigate(1)
    limiter.record_investigation()
    limiter.record_investigation()
    assert not limiter.can_investigate(2)


def test_rate_limiter_dedupe_window():
    recent = datetime.now(UTC) - timedelta(hours=1)
    old = datetime.now(UTC) - timedelta(hours=48)
    assert RateLimiter.is_within_dedupe_window(recent, 24)
    assert not RateLimiter.is_within_dedupe_window(old, 24)
