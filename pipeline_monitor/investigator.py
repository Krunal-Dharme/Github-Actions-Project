"""Main investigation orchestrator — Version 1 failure path."""

from __future__ import annotations

import hashlib
import sys
import time
import traceback
from typing import TYPE_CHECKING, Any

from pipeline_monitor.audit_logger import AuditAction, AuditLogger
from pipeline_monitor.config import Settings
from pipeline_monitor.email_notifier import EmailNotifier
from pipeline_monitor.github_client import GitHubClient, WorkflowContext, WorkflowRunRef
from pipeline_monitor.issue_manager import IssueManager
from pipeline_monitor.openrouter_client import OpenRouterClient
from pipeline_monitor.postgres_store import PostgresStore
from pipeline_monitor.rate_limiter import RateLimiter
from pipeline_monitor.llm.prompts import AGENT_INVESTIGATION_INPUT
from pipeline_monitor.report_generator import (
    build_context_for_llm,
    build_failure_report,
    build_investigation_result,
    build_success_result,
    build_success_summary,
    extract_risk_level,
    extract_root_cause,
)
from pipeline_monitor.safety import SafetyChecker
from pipeline_monitor.secrets_scanner import SecretsScanner, safe_output

if TYPE_CHECKING:
    from github.Issue import Issue

MONITOR_WORKFLOW_PATHS = ("investigate-failures", "pipeline-health-monitor")
V1_TARGET_SECONDS = 90


class Investigator:
    """Orchestrates the Version 1 pipeline failure investigation lifecycle."""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.audit = AuditLogger(
            log_dir=settings.audit_log_dir,
            level=settings.audit_log_level,
            postgres_audit_enabled=settings.postgres_audit_enabled,
        )
        self.safety_checker = SafetyChecker()
        self.rate_limiter = RateLimiter(
            max_investigations_per_day=settings.max_investigations_per_day,
            max_llm_calls_per_run=settings.max_llm_calls_per_run,
            audit=self.audit,
        )
        self._started_at = time.monotonic()

        self.github = GitHubClient(
            token=settings.github_token,
            repository=settings.github_repository,
            audit=self.audit,
            api_url=settings.github_api_url,
            max_log_chars=settings.max_log_chars,
            max_commits=settings.max_commits_to_analyze,
            commit_lookback_hours=settings.commit_lookback_hours,
        )
        self.llm = OpenRouterClient(
            api_key=settings.openrouter_api_key,
            model=settings.openrouter_model,
            audit=self.audit,
            base_url=settings.openrouter_base_url,
            max_tokens=settings.openrouter_max_tokens,
            temperature=settings.openrouter_temperature,
            referer=settings.openrouter_referer,
            agent_max_iterations=settings.agent_max_iterations,
        )
        self.issue_manager = IssueManager(
            github=self.github,
            audit=self.audit,
            label=settings.issue_label,
            update_existing=settings.issue_update_existing,
        )

        self.postgres: PostgresStore | None = None
        if settings.postgres_enabled and settings.resolved_postgres_dsn:
            try:
                self.postgres = PostgresStore(settings.resolved_postgres_dsn, self.audit)
                if settings.postgres_audit_enabled:
                    self.audit.set_postgres_store(self.postgres)
            except Exception as exc:
                print(
                    f"WARN: Postgres unavailable — continuing without DB history: {exc}",
                    file=sys.stderr,
                )

        self.email: EmailNotifier | None = None
        if settings.email_enabled and settings.smtp_host:
            self.email = EmailNotifier(
                audit=self.audit,
                smtp_host=settings.smtp_host,
                smtp_port=settings.smtp_port,
                smtp_user=settings.smtp_user or "",
                smtp_password=settings.smtp_password or "",
                smtp_from=settings.smtp_from or settings.smtp_user or "",
                recipients=settings.recipient_list,
            )

    def run(self) -> int:
        try:
            run_id, status, workflow_path = self._resolve_run()
            if self._is_self_trigger(workflow_path):
                self.audit.log(
                    AuditAction.INVESTIGATION_SKIPPED,
                    workflow_run_id=run_id,
                    details={"reason": "self-trigger"},
                )
                return 0

            if status != "failure":
                self.audit.log(
                    AuditAction.INVESTIGATION_SKIPPED,
                    workflow_run_id=run_id,
                    details={"reason": f"status={status}", "v1_scope": "failures_only"},
                )
                return 0

            result = self.investigate_failure(str(run_id))
            return 0 if result.get("success") or result.get("error") else 1

        except Exception as exc:
            self.audit.log(
                AuditAction.ERROR,
                details={"error": str(exc), "type": type(exc).__name__},
                level="ERROR",
            )
            print(f"ERROR: {exc}", file=sys.stderr)
            return 1
        finally:
            self._cleanup()

    def _resolve_run(self) -> tuple[int, str, str]:
        """Resolve workflow_run_id, status, and workflow path from CLI/env or webhook event."""
        if self.settings.workflow_run_id is not None:
            run_id = self.settings.workflow_run_id
            status = self.settings.workflow_run_status
            workflow_path = ""
            if self.settings.github_event_path:
                event = self.github.parse_workflow_run_event(self.settings.github_event_path)
                workflow_path = event.get("workflow_run", {}).get("path", "")
                if status is None:
                    status = event.get("workflow_run", {}).get("conclusion")
            if status is None:
                run_ref, _ = self.github.resolve_workflow_run(
                    run_id, self.settings.github_event_path
                )
                status = run_ref.conclusion or "unknown"
                if not workflow_path:
                    workflow_path = run_ref.path or ""
            return run_id, status, workflow_path

        if not self.settings.github_event_path:
            raise ValueError(
                "Provide --workflow-run-id or set WORKFLOW_RUN_ID / GITHUB_EVENT_PATH"
            )
        event = self.github.parse_workflow_run_event(self.settings.github_event_path)
        wr = event["workflow_run"]
        return wr["id"], wr.get("conclusion", "unknown"), wr.get("path", "")

    @staticmethod
    def _is_self_trigger(workflow_path: str) -> bool:
        return any(marker in workflow_path for marker in MONITOR_WORKFLOW_PATHS)

    def _elapsed_ms(self) -> int:
        return int((time.monotonic() - self._started_at) * 1000)

    def _sync_rate_limits(self) -> None:
        if self.postgres:
            count = self.postgres.count_investigations_today(self.settings.github_repository)
            self.rate_limiter.sync_daily_count(count)

    def investigate_failure(self, workflow_run_id: str | int) -> dict:
        """Investigate a failed workflow run and return a structured result dict (§8)."""
        run_id = int(workflow_run_id)

        self.audit.log(
            AuditAction.INVESTIGATION_STARTED,
            workflow_run_id=run_id,
            repository=self.settings.github_repository,
            details={"version": "1", "entrypoint": "investigate_failure"},
        )

        try:
            self._sync_rate_limits()

            if not self.rate_limiter.can_investigate(run_id):
                msg = "Daily investigation limit reached"
                self.audit.log(
                    AuditAction.INVESTIGATION_SKIPPED,
                    workflow_run_id=run_id,
                    details={"reason": msg},
                )
                return build_investigation_result(
                    success=False,
                    workflow_run_id=run_id,
                    error=msg,
                )

            run_ref, fetch_warnings = self.github.resolve_workflow_run(
                run_id, self.settings.github_event_path
            )
            if fetch_warnings:
                for warning in fetch_warnings:
                    print(f"[github-debug] {warning}", file=sys.stderr)

            historical = self._load_historical_failures(run_ref.name or "unknown")

            if self.settings.use_langchain_agent:
                ctx = self._build_minimal_context(run_ref)
            else:
                ctx = self.github.collect_context(run_ref)
                self.audit.log(
                    AuditAction.GITHUB_FETCH,
                    workflow_run_id=run_id,
                    details={"phase": "context_complete", "elapsed_ms": self._elapsed_ms()},
                )
                if self._is_duplicate_failure(ctx):
                    return build_investigation_result(
                        success=False,
                        workflow_run_id=run_id,
                        error="Duplicate failure skipped (dedupe window)",
                    )

            log_had_secrets = False
            if ctx.log_text:
                log_had_secrets, _ = SecretsScanner.scan(ctx.log_text)
                ctx.log_text = self._sanitize(ctx.log_text, run_id, phase="workflow_logs")

            if not self.rate_limiter.can_call_llm(run_id):
                report_md = self._sanitize(
                    self._fallback_report(ctx, "LLM call limit reached for this run."),
                    run_id,
                    phase="fallback_report",
                )
                issue = self._maybe_publish_issue(ctx, report_md)
                self._notify_failure(ctx, report_md, issue)
                return build_investigation_result(
                    success=True,
                    workflow_run_id=run_id,
                    analysis=report_md,
                    root_cause="LLM analysis skipped (rate limit)",
                    risk_level="Unknown",
                    issue_number=issue.number if issue else None,
                )

            self.rate_limiter.record_llm_call()
            if self.settings.use_langchain_agent:
                llm_response = self._run_agent_analysis(run_id, ctx, historical)
            else:
                llm_response = self._run_direct_analysis(run_id, ctx, historical)

            llm_had_secrets, _ = SecretsScanner.scan(llm_response.content)
            llm_response.content = self._sanitize(llm_response.content, run_id, phase="llm_output")

            safety = self.safety_checker.scan_recommendations(llm_response.content)
            if safety.has_destructive:
                self.audit.log(
                    AuditAction.DESTRUCTIVE_FLAGGED,
                    workflow_run_id=run_id,
                    details={"commands": safety.flagged_commands},
                    level="WARNING",
                )

            report_md = build_failure_report(
                ctx,
                llm_response,
                secrets_redacted=log_had_secrets or llm_had_secrets,
            )
            report_md = self.safety_checker.append_safety_warning(report_md, safety)
            report_md = self._sanitize(report_md, run_id, phase="report")

            self.audit.log(
                AuditAction.SECRETS_SCAN,
                workflow_run_id=run_id,
                details={"phase": "report_final"},
            )

            root_cause = extract_root_cause(llm_response.content)
            risk_level = extract_risk_level(llm_response.content)
            report_md = self._append_postgres_correlation(ctx, report_md, root_cause)

            issue = self._maybe_publish_issue(ctx, report_md)
            self.rate_limiter.record_investigation()
            fingerprint = self._error_fingerprint(ctx)
            self._persist(ctx, report_md, llm_response, issue, fingerprint)
            self._notify_failure(ctx, report_md, issue)

            elapsed = self._elapsed_ms()
            self.audit.log(
                AuditAction.INVESTIGATION_COMPLETED,
                workflow_run_id=run_id,
                repository=self.settings.github_repository,
                details={
                    "issue_number": issue.number if issue else None,
                    "issue_deferred_to_actions": self.settings.skip_github_issue_publish,
                    "elapsed_ms": elapsed,
                    "within_v1_target": elapsed <= V1_TARGET_SECONDS * 1000,
                    "llm_mode": "agent" if self.settings.use_langchain_agent else "direct",
                    "root_cause": root_cause[:120],
                    "risk_level": risk_level,
                },
            )
            if elapsed > V1_TARGET_SECONDS * 1000:
                print(
                    f"WARN: investigation took {elapsed}ms (V1 target: {V1_TARGET_SECONDS}s)",
                    file=sys.stderr,
                )

            warning_note = "\n".join(fetch_warnings) if fetch_warnings else None
            result = build_investigation_result(
                success=True,
                workflow_run_id=run_id,
                analysis=report_md,
                root_cause=root_cause,
                risk_level=risk_level,
                issue_number=issue.number if issue else None,
                detailed_error=warning_note,
            )
            return result

        except Exception as exc:
            tb = traceback.format_exc()
            print(tb, file=sys.stderr)
            self.audit.log(
                AuditAction.ERROR,
                workflow_run_id=run_id,
                details={"error": str(exc), "type": type(exc).__name__, "traceback": tb},
                level="ERROR",
            )
            return self._recover_from_investigation_error(run_id, exc, tb)

    def handle_success(self, workflow_run_id: str | int) -> dict:
        """Lightweight success handler — no LLM; optional email when configured."""
        run_id = int(workflow_run_id)

        self.audit.log(
            AuditAction.INVESTIGATION_STARTED,
            workflow_run_id=run_id,
            repository=self.settings.github_repository,
            details={"status": "success", "entrypoint": "handle_success"},
        )

        try:
            run_ref, _ = self.github.resolve_workflow_run(
                run_id, self.settings.github_event_path
            )
            ctx = self._build_minimal_context(run_ref)
            summary_md = build_success_summary(ctx, summary=None)

            email_sent = False
            if self.settings.notify_on_success and self.email:
                recipients = EmailNotifier.resolve_recipients(
                    self.settings.recipient_list,
                    ctx.actor_email,
                )
                self.email.recipients = recipients
                self.email.send_success_summary(ctx, summary_md)
                email_sent = True

            self.audit.log(
                AuditAction.INVESTIGATION_COMPLETED,
                workflow_run_id=run_id,
                repository=self.settings.github_repository,
                details={"status": "success", "email_sent": email_sent, "elapsed_ms": self._elapsed_ms()},
            )

            return build_success_result(
                success=True,
                workflow_run_id=run_id,
                analysis=summary_md,
                email_sent=email_sent,
            )

        except Exception as exc:
            self.audit.log(
                AuditAction.ERROR,
                workflow_run_id=run_id,
                details={"error": str(exc), "type": type(exc).__name__},
                level="ERROR",
            )
            return build_success_result(
                success=False,
                workflow_run_id=run_id,
                error=str(exc),
            )

    def _handle_failure(self, run_id: int) -> int:
        """Legacy handler — delegates to investigate_failure."""
        result = self.investigate_failure(run_id)
        return 0 if result.get("success") or result.get("error") else 1

    def _sanitize(self, text: str, run_id: int, *, phase: str) -> str:
        """Run safe_output on text; audit and stderr warning when secrets are found."""
        had_secrets, findings = SecretsScanner.scan(text)
        if not had_secrets:
            return text
        self.audit.log(
            AuditAction.SECRETS_REDACTED,
            workflow_run_id=run_id,
            details={"phase": phase, "findings": findings},
            level="WARNING",
        )
        return safe_output(text)

    def _load_historical_failures(self, workflow_name: str) -> list[dict] | None:
        if not self.postgres:
            return None
        return self.postgres.get_similar_historical_failures(
            self.settings.github_repository,
            workflow_name,
        )

    def _is_duplicate_failure(self, ctx: WorkflowContext) -> bool:
        if not self.postgres:
            return False
        fingerprint = self._error_fingerprint(ctx)
        last = self.postgres.get_last_investigation_time(
            self.settings.github_repository,
            ctx.workflow_name,
            fingerprint,
        )
        if RateLimiter.is_within_dedupe_window(last, self.settings.dedupe_window_hours):
            self.audit.log(
                AuditAction.INVESTIGATION_SKIPPED,
                workflow_run_id=ctx.run_id,
                details={"reason": "dedupe", "fingerprint": fingerprint},
            )
            return True
        return False

    def _append_postgres_correlation(
        self,
        ctx: WorkflowContext,
        report_md: str,
        root_cause: str,
    ) -> str:
        """Append past similar failures from Postgres when the same root cause recurred."""
        if not self.postgres or not root_cause.strip():
            return report_md
        try:
            similar = self.postgres.find_similar_past_root_causes(
                self.settings.github_repository,
                root_cause,
                workflow_name=ctx.workflow_name,
                limit=5,
            )
            similar = [
                row
                for row in similar
                if int(row.get("workflow_run_id", 0)) != ctx.run_id
            ]
            if not similar:
                return report_md
            lines = ["", "---", "", "## Similar Past Failures (database history)", ""]
            for row in similar:
                issue_ref = (
                    f" — issue #{row['issue_number']}"
                    if row.get("issue_number")
                    else ""
                )
                lines.append(
                    f"- Run #{row['workflow_run_id']} ({row.get('investigated_at')}): "
                    f"{(row.get('root_cause') or '')[:200]}{issue_ref}"
                )
            return report_md + "\n".join(lines)
        except Exception as exc:
            print(f"WARN: Postgres correlation lookup failed: {exc}", file=sys.stderr)
            return report_md

    def _maybe_publish_issue(self, ctx: WorkflowContext, report_md: str) -> Issue | None:
        if self.settings.skip_github_issue_publish:
            return None
        return self.issue_manager.publish_failure_report(ctx, report_md)

    def _build_minimal_context(self, run: WorkflowRunRef) -> WorkflowContext:
        actor_login, actor_email = self.github.resolve_triggering_actor(run)
        commit_msg = ""
        if run.head_sha:
            try:
                commit = self.github.repo.get_commit(run.head_sha)
                commit_msg = (commit.commit.message or "")[:500]
            except Exception:
                pass
        return WorkflowContext(
            run_id=run.id,
            workflow_name=run.name or "unknown",
            workflow_path=run.path or "",
            head_branch=run.head_branch or "",
            head_sha=run.head_sha or "",
            head_commit_message=commit_msg,
            actor=actor_login,
            actor_email=actor_email,
            conclusion=run.conclusion or "failure",
            html_url=run.html_url or "",
            event=run.event or "",
            run_attempt=run.run_attempt or 1,
        )

    def _recover_from_investigation_error(
        self,
        run_id: int,
        exc: Exception,
        tb: str,
    ) -> dict:
        """Produce an investigation report even when the main path fails."""
        detailed = f"{type(exc).__name__}: {exc}"
        try:
            run_ref, warnings = self.github.resolve_workflow_run(
                run_id, self.settings.github_event_path
            )
            ctx = self.github.collect_context(run_ref)
            note = "; ".join(warnings) if warnings else detailed
            report_md = self._fallback_report(
                ctx,
                f"Investigation recovered after error: {detailed}",
            )
            report_md += f"\n\n## Agent Error Traceback\n```\n{tb}\n```\n"
            issue = self._maybe_publish_issue(ctx, report_md)
            self._notify_failure(ctx, report_md, issue)
            return build_investigation_result(
                success=True,
                workflow_run_id=run_id,
                analysis=report_md,
                root_cause=extract_root_cause(report_md) or "Recovered after agent error",
                risk_level=extract_risk_level(report_md),
                error=str(exc),
                detailed_error=note,
                traceback_str=tb,
                issue_number=issue.number if issue else None,
            )
        except Exception:
            tb2 = traceback.format_exc()
            print(tb2, file=sys.stderr)
            report_md = (
                f"# Pipeline Failure Report — Run #{run_id}\n\n"
                f"**Error**: {detailed}\n\n"
                f"## Traceback\n```\n{tb}\n```\n"
            )
            return build_investigation_result(
                success=True,
                workflow_run_id=run_id,
                analysis=report_md,
                root_cause="Investigation agent error — see traceback",
                risk_level="Unknown",
                error=str(exc),
                detailed_error=detailed,
                traceback_str=tb,
            )

    def _run_direct_analysis(self, run_id: int, ctx: WorkflowContext, historical: list | None):
        context_md = build_context_for_llm(ctx, historical)
        llm_response = self.llm.analyze_failure(context_md, run_id)
        self.audit.log(
            AuditAction.LLM_RESPONSE,
            workflow_run_id=run_id,
            details={"phase": "analysis_complete", "mode": "direct", "elapsed_ms": self._elapsed_ms()},
        )
        return llm_response

    def _run_agent_analysis(self, run_id: int, ctx: WorkflowContext, historical: list | None):
        agent_input = AGENT_INVESTIGATION_INPUT.format(
            run_id=run_id,
            repository=self.settings.github_repository,
            workflow_name=ctx.workflow_name,
            branch=ctx.head_branch,
            commit_sha=ctx.head_sha[:8] if ctx.head_sha else "unknown",
        )
        if historical:
            history_lines = "\n".join(
                f"- Run #{h.get('workflow_run_id')}: {(h.get('root_cause') or '')[:120]}"
                for h in historical[:3]
            )
            agent_input += f"\n\nHistorical failures from database:\n{history_lines}"

        llm_response = self.llm.analyze_with_agent(
            agent_input,
            run_id,
            self.github.tools,
            commit_lookback_hours=self.settings.commit_lookback_hours,
            max_commits=self.settings.max_commits_to_analyze,
        )
        self.audit.log(
            AuditAction.LLM_RESPONSE,
            workflow_run_id=run_id,
            details={"phase": "analysis_complete", "mode": "agent", "elapsed_ms": self._elapsed_ms()},
        )
        return llm_response

    def _fallback_report(self, ctx: WorkflowContext, reason: str) -> str:
        failed = "\n".join(
            f"- {j.job_name}/{j.step_name}: {j.log_excerpt[:500]}"
            for j in ctx.failed_jobs
        )
        return (
            f"# Pipeline Failure Report — {ctx.workflow_name}\n\n"
            f"**Run**: [#{ctx.run_id}]({ctx.html_url})\n\n"
            f"> Automated LLM analysis skipped: {reason}\n\n"
            f"## Failed Steps\n{failed}\n"
        )

    def _persist(
        self,
        ctx: WorkflowContext,
        report_md: str,
        llm_response: Any,
        issue: Issue | None,
        fingerprint: str,
    ) -> None:
        if not self.postgres:
            return
        self.postgres.save_investigation(
            workflow_run_id=ctx.run_id,
            repository=self.settings.github_repository,
            workflow_name=ctx.workflow_name,
            conclusion=ctx.conclusion,
            actor=ctx.actor,
            head_sha=ctx.head_sha,
            head_branch=ctx.head_branch,
            root_cause=extract_root_cause(llm_response.content),
            report_md=report_md,
            issue_number=issue.number if issue else None,
            llm_model=llm_response.model,
            prompt_tokens=llm_response.prompt_tokens,
            completion_tokens=llm_response.completion_tokens,
            metadata={"error_fingerprint": fingerprint, "elapsed_ms": self._elapsed_ms()},
        )

    def _notify_failure(self, ctx: WorkflowContext, report_md: str, issue: Issue | None) -> None:
        if not self.email:
            return
        recipients = EmailNotifier.resolve_recipients(
            self.settings.recipient_list,
            ctx.actor_email,
        )
        self.email.recipients = recipients
        issue_url = issue.html_url if issue else None
        self.email.send_failure_report(ctx, report_md, issue_url)

    @staticmethod
    def _error_fingerprint(ctx: WorkflowContext) -> str:
        parts = [ctx.workflow_name]
        for job in ctx.failed_jobs:
            parts.append(f"{job.job_name}:{job.step_name}")
            parts.append(job.log_excerpt[:200])
        raw = "|".join(parts)
        return hashlib.sha256(raw.encode()).hexdigest()[:16]

    def _cleanup(self) -> None:
        self.github.close()
        self.llm.close()
        if self.postgres:
            self.postgres.close()
