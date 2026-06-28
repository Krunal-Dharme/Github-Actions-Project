"""Configuration loaded from environment variables."""

from __future__ import annotations

from typing import Literal
from urllib.parse import quote_plus

from pydantic import AliasChoices, Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    # GitHub
    github_token: str = Field(..., alias="GITHUB_TOKEN")
    github_repository: str = Field(
        ...,
        validation_alias=AliasChoices("GITHUB_REPOSITORY", "GITHUB_REPO"),
    )
    github_event_path: str | None = Field(default=None, alias="GITHUB_EVENT_PATH")
    github_api_url: str = Field(default="https://api.github.com", alias="GITHUB_API_URL")

    # Version 1: explicit invocation (CLI or workflow env)
    workflow_run_id: int | None = Field(default=None, alias="WORKFLOW_RUN_ID")
    workflow_run_status: str | None = Field(default=None, alias="WORKFLOW_RUN_STATUS")

    # OpenRouter (sole LLM provider)
    openrouter_api_key: str = Field(..., alias="OPENROUTER_API_KEY")
    openrouter_model: str = Field(
        default="anthropic/claude-3.5-sonnet",
        validation_alias=AliasChoices("OPENROUTER_MODEL", "MODEL_NAME"),
    )
    openrouter_base_url: str = Field(
        default="https://openrouter.ai/api/v1",
        alias="OPENROUTER_BASE_URL",
    )
    openrouter_max_tokens: int = Field(default=2048, alias="OPENROUTER_MAX_TOKENS")
    openrouter_temperature: float = Field(default=0.0, alias="OPENROUTER_TEMPERATURE")
    openrouter_referer: str = Field(
        default="https://github.com/pipeline-health-monitor",
        alias="OPENROUTER_REFERER",
    )
    use_langchain_agent: bool = Field(default=False, alias="USE_LANGCHAIN_AGENT")
    agent_max_iterations: int = Field(default=3, alias="AGENT_MAX_ITERATIONS")

    # Optional Postgres (investigation history + audit trail)
    postgres_enabled: bool = Field(
        default=False,
        validation_alias=AliasChoices("POSTGRES_ENABLED", "ENABLE_DB"),
    )
    postgres_dsn: str | None = Field(
        default=None,
        validation_alias=AliasChoices("POSTGRES_DSN", "DATABASE_URL"),
    )
    postgres_host: str | None = Field(
        default=None,
        validation_alias=AliasChoices("POSTGRES_HOST", "PGHOST"),
    )
    postgres_port: int = Field(
        default=5432,
        validation_alias=AliasChoices("POSTGRES_PORT", "PGPORT"),
    )
    postgres_database: str | None = Field(
        default=None,
        validation_alias=AliasChoices("POSTGRES_DB", "POSTGRES_DATABASE", "PGDATABASE"),
    )
    postgres_user: str | None = Field(
        default=None,
        validation_alias=AliasChoices("POSTGRES_USER", "PGUSER"),
    )
    postgres_password: str | None = Field(
        default=None,
        validation_alias=AliasChoices("POSTGRES_PASSWORD", "PGPASSWORD"),
    )
    postgres_sslmode: str = Field(
        default="prefer",
        validation_alias=AliasChoices("POSTGRES_SSLMODE", "PGSSLMODE"),
    )

    # Email (SMTP)
    email_enabled: bool = Field(
        default=False,
        validation_alias=AliasChoices("EMAIL_ENABLED", "ENABLE_EMAIL_NOTIFICATIONS"),
    )
    smtp_host: str | None = Field(
        default=None,
        validation_alias=AliasChoices("SMTP_HOST", "EMAIL_SMTP_HOST"),
    )
    smtp_port: int = Field(
        default=587,
        validation_alias=AliasChoices("SMTP_PORT", "EMAIL_SMTP_PORT"),
    )
    smtp_user: str | None = Field(
        default=None,
        validation_alias=AliasChoices("SMTP_USER", "EMAIL_USERNAME"),
    )
    smtp_password: str | None = Field(
        default=None,
        validation_alias=AliasChoices("SMTP_PASSWORD", "EMAIL_PASSWORD"),
    )
    smtp_from: str | None = Field(
        default=None,
        validation_alias=AliasChoices("SMTP_FROM", "EMAIL_FROM"),
    )
    email_recipients: str = Field(
        default="",
        validation_alias=AliasChoices("EMAIL_RECIPIENTS", "EMAIL_TO"),
    )
    notify_on_success: bool = Field(default=False, alias="NOTIFY_ON_SUCCESS")

    # Rate limiting & cost controls
    max_investigations_per_day: int = Field(default=50, alias="MAX_INVESTIGATIONS_PER_DAY")
    max_llm_calls_per_run: int = Field(default=1, alias="MAX_LLM_CALLS_PER_RUN")
    max_log_chars: int = Field(default=80_000, alias="MAX_LOG_CHARS")
    max_commits_to_analyze: int = Field(default=10, alias="MAX_COMMITS_TO_ANALYZE")
    commit_lookback_hours: int = Field(default=24, alias="COMMIT_LOOKBACK_HOURS")
    dedupe_window_hours: int = Field(default=24, alias="DEDUPE_WINDOW_HOURS")

    # Issue management
    issue_label: str = Field(default="pipeline-failure", alias="ISSUE_LABEL")
    issue_update_existing: bool = Field(default=True, alias="ISSUE_UPDATE_EXISTING")
    skip_github_issue_publish: bool = Field(default=False, alias="SKIP_GITHUB_ISSUE_PUBLISH")

    # Audit
    audit_log_dir: str = Field(default=".agent_logs", alias="AUDIT_LOG_DIR")
    audit_log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR"] = Field(
        default="INFO", alias="AUDIT_LOG_LEVEL"
    )
    postgres_audit_enabled: bool = Field(default=True, alias="POSTGRES_AUDIT_ENABLED")

    @field_validator("workflow_run_id", mode="before")
    @classmethod
    def parse_workflow_run_id(cls, v: object) -> int | None:
        if v is None or v == "":
            return None
        return int(v)

    @field_validator("postgres_enabled", mode="before")
    @classmethod
    def parse_postgres_enabled(cls, v: object) -> bool:
        if isinstance(v, bool):
            return v
        return str(v).lower() in ("1", "true", "yes", "on")

    @field_validator(
        "email_enabled",
        "notify_on_success",
        "issue_update_existing",
        "use_langchain_agent",
        "postgres_audit_enabled",
        "skip_github_issue_publish",
        mode="before",
    )
    @classmethod
    def parse_bool_flags(cls, v: object) -> bool:
        if isinstance(v, bool):
            return v
        return str(v).lower() in ("1", "true", "yes", "on")

    @property
    def recipient_list(self) -> list[str]:
        if not self.email_recipients.strip():
            return []
        return [r.strip() for r in self.email_recipients.split(",") if r.strip()]

    @property
    def repo_owner(self) -> str:
        return self.github_repository.split("/")[0]

    @property
    def repo_name(self) -> str:
        return self.github_repository.split("/")[1]

    @property
    def resolved_postgres_dsn(self) -> str | None:
        """Full connection string from DATABASE_URL or individual POSTGRES_* vars."""
        if self.postgres_dsn and str(self.postgres_dsn).strip():
            return str(self.postgres_dsn).strip()
        if (
            self.postgres_host
            and self.postgres_database
            and self.postgres_user
            and self.postgres_password
        ):
            user = quote_plus(self.postgres_user)
            password = quote_plus(self.postgres_password)
            return (
                f"postgresql://{user}:{password}@{self.postgres_host}:"
                f"{self.postgres_port}/{self.postgres_database}"
                f"?sslmode={self.postgres_sslmode}"
            )
        return None


def load_settings() -> Settings:
    return Settings()  # type: ignore[call-arg]
