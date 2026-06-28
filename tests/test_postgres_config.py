"""Tests for Postgres DSN resolution from environment components."""

import os

import pytest

from pipeline_agent.config import Settings


@pytest.fixture
def clear_postgres_env(monkeypatch):
    for key in (
        "DATABASE_URL",
        "POSTGRES_DSN",
        "POSTGRES_HOST",
        "POSTGRES_PORT",
        "POSTGRES_DB",
        "POSTGRES_USER",
        "POSTGRES_PASSWORD",
        "POSTGRES_SSLMODE",
    ):
        monkeypatch.delenv(key, raising=False)


def test_resolved_postgres_dsn_prefers_database_url(clear_postgres_env, monkeypatch):
    monkeypatch.setenv("GITHUB_TOKEN", "test")
    monkeypatch.setenv("GITHUB_REPOSITORY", "owner/repo")
    monkeypatch.setenv("OPENROUTER_API_KEY", "test")
    monkeypatch.setenv("DATABASE_URL", "postgresql://u:p@host:5432/pipeline_history")

    settings = Settings()  # type: ignore[call-arg]
    assert settings.resolved_postgres_dsn == "postgresql://u:p@host:5432/pipeline_history"


def test_resolved_postgres_dsn_builds_from_components(clear_postgres_env, monkeypatch):
    monkeypatch.setenv("GITHUB_TOKEN", "test")
    monkeypatch.setenv("GITHUB_REPOSITORY", "owner/repo")
    monkeypatch.setenv("OPENROUTER_API_KEY", "test")
    monkeypatch.setenv("POSTGRES_HOST", "localhost")
    monkeypatch.setenv("POSTGRES_PORT", "5432")
    monkeypatch.setenv("POSTGRES_DB", "pipeline_history")
    monkeypatch.setenv("POSTGRES_USER", "pipeline_user")
    monkeypatch.setenv("POSTGRES_PASSWORD", "Pipeline@123")

    settings = Settings()  # type: ignore[call-arg]
    dsn = settings.resolved_postgres_dsn
    assert dsn is not None
    assert "pipeline_user" in dsn
    assert "Pipeline%40123" in dsn
    assert "pipeline_history" in dsn
    assert "localhost:5432" in dsn
