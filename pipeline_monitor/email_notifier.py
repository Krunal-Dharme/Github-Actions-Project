"""Email notifications via SMTP."""

from __future__ import annotations

import smtplib
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

from pipeline_monitor.audit_logger import AuditAction, AuditLogger
from pipeline_monitor.github_client import WorkflowContext


class EmailNotifier:
    def __init__(
        self,
        audit: AuditLogger,
        smtp_host: str,
        smtp_port: int,
        smtp_user: str,
        smtp_password: str,
        smtp_from: str,
        recipients: list[str],
    ) -> None:
        self.audit = audit
        self.smtp_host = smtp_host
        self.smtp_port = smtp_port
        self.smtp_user = smtp_user
        self.smtp_password = smtp_password
        self.smtp_from = smtp_from
        self.recipients = recipients

    def send_failure_report(
        self,
        ctx: WorkflowContext,
        report_md: str,
        issue_url: str | None = None,
    ) -> None:
        subject = f"[FAILURE] {ctx.workflow_name} — Run #{ctx.run_id} ({ctx.head_branch})"
        body = report_md
        if issue_url:
            body += f"\n\n---\nGitHub Issue: {issue_url}"

        self._send(subject, body, ctx)

    def send_success_summary(
        self,
        ctx: WorkflowContext,
        summary_md: str,
    ) -> None:
        subject = f"[SUCCESS] {ctx.workflow_name} — Run #{ctx.run_id} ({ctx.head_branch})"
        self._send(subject, summary_md, ctx)

    def _send(self, subject: str, body: str, ctx: WorkflowContext) -> None:
        if not self.recipients:
            return

        msg = MIMEMultipart("alternative")
        msg["Subject"] = subject
        msg["From"] = self.smtp_from
        msg["To"] = ", ".join(self.recipients)

        msg.attach(MIMEText(body, "plain", "utf-8"))

        with smtplib.SMTP(self.smtp_host, self.smtp_port, timeout=30) as server:
            server.starttls()
            server.login(self.smtp_user, self.smtp_password)
            server.sendmail(self.smtp_from, self.recipients, msg.as_string())

        self.audit.log(
            AuditAction.EMAIL_SENT,
            workflow_run_id=ctx.run_id,
            repository=None,
            actor=ctx.actor,
            details={"recipients": self.recipients, "subject": subject},
        )

    @staticmethod
    def resolve_recipients(
        configured: list[str],
        actor_email: str | None,
    ) -> list[str]:
        recipients = list(configured)
        if actor_email and actor_email not in recipients:
            recipients.append(actor_email)
        return recipients
