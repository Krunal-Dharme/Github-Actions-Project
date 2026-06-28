"""GitHub issue creation and updates for investigation reports."""

from __future__ import annotations

from github.Issue import Issue

from pipeline_monitor.audit_logger import AuditAction, AuditLogger
from pipeline_monitor.github_client import GitHubClient, WorkflowContext


class IssueManager:
    def __init__(
        self,
        github: GitHubClient,
        audit: AuditLogger,
        label: str = "pipeline-failure",
        update_existing: bool = True,
    ) -> None:
        self.github = github
        self.audit = audit
        self.label = label
        self.update_existing = update_existing

    def publish_failure_report(
        self,
        ctx: WorkflowContext,
        report_md: str,
    ) -> Issue:
        title = f"[Pipeline Failure] {ctx.workflow_name} — Run #{ctx.run_id}"
        labels = [self.label, "ai-investigation"]

        if self.update_existing:
            existing = self.github.find_existing_failure_issue(ctx.run_id, self.label)
            if existing:
                updated = self.github.update_issue(existing, report_md)
                self.audit.log(
                    AuditAction.ISSUE_UPDATED,
                    workflow_run_id=ctx.run_id,
                    repository=self.github.repository,
                    details={"issue_number": existing.number},
                )
                return updated

        issue = self.github.create_issue(title=title, body=report_md, labels=labels)
        self.audit.log(
            AuditAction.ISSUE_CREATED,
            workflow_run_id=ctx.run_id,
            repository=self.github.repository,
            details={"issue_number": issue.number},
        )
        return issue
