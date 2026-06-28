"""GitHub API client — orchestrates core tools and issue management."""

from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor
from typing import Any

from github import Auth, Github, GithubException
from github.Issue import Issue
from github.Repository import Repository
from github.WorkflowRun import WorkflowRun
from tenacity import retry, stop_after_attempt, wait_exponential

from pipeline_monitor.audit_logger import AuditAction, AuditLogger
from pipeline_monitor.tools.github_tools import GitHubTools, JobStepFailure


class WorkflowContext:
    """Aggregated evidence collected for one failed workflow run."""

    __slots__ = (
        "run_id",
        "workflow_name",
        "workflow_path",
        "head_branch",
        "head_sha",
        "head_commit_message",
        "actor",
        "actor_email",
        "conclusion",
        "html_url",
        "event",
        "run_attempt",
        "failed_jobs",
        "log_text",
        "recent_commits",
        "similar_issues",
        "logs_summary",
        "commits_summary",
        "issues_summary",
    )

    def __init__(
        self,
        *,
        run_id: int,
        workflow_name: str,
        workflow_path: str,
        head_branch: str,
        head_sha: str,
        head_commit_message: str,
        actor: str,
        actor_email: str | None,
        conclusion: str,
        html_url: str,
        event: str,
        run_attempt: int,
        failed_jobs: list[JobStepFailure] | None = None,
        log_text: str = "",
        recent_commits: list[dict[str, Any]] | None = None,
        similar_issues: list[dict[str, Any]] | None = None,
        logs_summary: str = "",
        commits_summary: str = "",
        issues_summary: str = "",
    ) -> None:
        self.run_id = run_id
        self.workflow_name = workflow_name
        self.workflow_path = workflow_path
        self.head_branch = head_branch
        self.head_sha = head_sha
        self.head_commit_message = head_commit_message
        self.actor = actor
        self.actor_email = actor_email
        self.conclusion = conclusion
        self.html_url = html_url
        self.event = event
        self.run_attempt = run_attempt
        self.failed_jobs = failed_jobs or []
        self.log_text = log_text
        self.recent_commits = recent_commits or []
        self.similar_issues = similar_issues or []
        self.logs_summary = logs_summary
        self.commits_summary = commits_summary
        self.issues_summary = issues_summary


class GitHubClient:
    """High-level GitHub client composing GitHubTools + PyGithub issue helpers."""

    def __init__(
        self,
        token: str,
        repository: str,
        audit: AuditLogger,
        api_url: str = "https://api.github.com",
        max_log_chars: int = 80_000,
        max_commits: int = 10,
        commit_lookback_hours: int = 24,
    ) -> None:
        self.audit = audit
        self.repository = repository
        self.max_commits = max_commits
        self.commit_lookback_hours = commit_lookback_hours
        self.tools = GitHubTools(
            token=token,
            repository=repository,
            audit=audit,
            api_url=api_url,
            max_log_chars=max_log_chars,
        )
        self._gh = Github(auth=Auth.Token(token), base_url=api_url)

    @property
    def repo(self) -> Repository:
        return self._gh.get_repo(self.repository)

    def parse_workflow_run_event(self, event_path: str) -> dict[str, Any]:
        with open(event_path, encoding="utf-8") as f:
            return json.load(f)

    @retry(stop=stop_after_attempt(3), wait=wait_exponential(multiplier=1, min=2, max=30))
    def get_workflow_run(self, run_id: int) -> WorkflowRun:
        self.audit.log(
            AuditAction.GITHUB_FETCH,
            workflow_run_id=run_id,
            repository=self.repository,
            details={"resource": "workflow_run"},
        )
        return self.repo.get_workflow_run(run_id)

    def collect_context(self, run: WorkflowRun) -> WorkflowContext:
        logs = self.tools.get_workflow_logs(run.id)
        keywords = self.tools.extract_error_keywords(logs.log_text)

        with ThreadPoolExecutor(max_workers=3) as pool:
            commits_future = pool.submit(
                self.tools.analyze_recent_commits,
                self.commit_lookback_hours,
                head_sha=run.head_sha,
                limit=self.max_commits,
            )
            issues_future = pool.submit(self.tools.search_similar_issues, keywords)
            meta_future = pool.submit(self._fetch_run_metadata, run)

            commits = commits_future.result()
            issues = issues_future.result()
            commit_msg, actor_email = meta_future.result()
            actor_login, _ = self.resolve_triggering_actor(run)

        return WorkflowContext(
            run_id=run.id,
            workflow_name=run.name or logs.workflow_name,
            workflow_path=run.path or "",
            head_branch=run.head_branch or logs.branch,
            head_sha=run.head_sha or logs.head_sha,
            head_commit_message=commit_msg,
            actor=actor_login,
            actor_email=actor_email,
            conclusion=run.conclusion or logs.conclusion,
            html_url=run.html_url or "",
            event=run.event or "",
            run_attempt=run.run_attempt or 1,
            failed_jobs=logs.failed_jobs,
            log_text=logs.log_text,
            recent_commits=[
                {
                    "sha": c.sha,
                    "author": c.author,
                    "message": c.message,
                    "date": c.date,
                    "files_changed": c.files_changed,
                }
                for c in commits
            ],
            similar_issues=[
                {
                    "number": str(i.number),
                    "title": i.title,
                    "url": i.url,
                    "state": i.state,
                    "solution_hint": i.solution_hint,
                }
                for i in issues
            ],
            logs_summary=logs.to_summary(),
            commits_summary=self.tools.format_commits_report(commits, self.commit_lookback_hours),
            issues_summary=self.tools.format_issues_report(issues, keywords),
        )

    def _fetch_run_metadata(self, run: WorkflowRun) -> tuple[str, str | None]:
        commit_msg = ""
        if run.head_sha:
            try:
                commit = self.repo.get_commit(run.head_sha)
                commit_msg = (commit.commit.message or "")[:500]
            except GithubException:
                pass

        _, actor_email = self.resolve_triggering_actor(run)
        return commit_msg, actor_email

    def resolve_triggering_actor(self, run: WorkflowRun) -> tuple[str, str | None]:
        """Prefer triggering_actor (who started the run) for notifications."""
        for attr in ("triggering_actor", "actor"):
            actor = getattr(run, attr, None)
            if actor is None:
                continue
            login = actor.login or "unknown"
            email = None
            try:
                user = self._gh.get_user(login)
                email = user.email
            except GithubException:
                pass
            return login, email
        return "unknown", None

    def find_existing_failure_issue(self, run_id: int, label: str) -> Issue | None:
        title_prefix = f"[Pipeline Failure] Run #{run_id}"
        try:
            for issue in self.repo.get_issues(state="open", labels=[label]):
                if issue.title.startswith(title_prefix) or f"Run #{run_id}" in (issue.body or ""):
                    return issue
        except GithubException:
            pass
        return None

    def create_issue(self, title: str, body: str, labels: list[str]) -> Issue:
        return self.repo.create_issue(title=title, body=body, labels=labels)

    def update_issue(self, issue: Issue, body: str) -> Issue:
        issue.edit(body=body)
        return issue

    def close(self) -> None:
        self.tools.close()
        self._gh.close()
