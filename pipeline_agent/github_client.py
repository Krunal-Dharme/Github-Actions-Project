"""GitHub API client — orchestrates core tools and issue management."""

from __future__ import annotations

import json
import logging
import sys
import time
import traceback
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from typing import Any

from github import Auth, Github, GithubException
from github.Issue import Issue
from github.Repository import Repository
from github.WorkflowRun import WorkflowRun
from tenacity import (
    retry,
    retry_if_exception,
    stop_after_attempt,
    wait_exponential,
)

from pipeline_agent.audit_logger import AuditAction, AuditLogger
from pipeline_agent.tools.github_tools import GitHubTools, JobStepFailure, WorkflowLogsResult

logger = logging.getLogger(__name__)


def _actor_login(actor: Any) -> str | None:
    if actor is None:
        return None
    if isinstance(actor, dict):
        return actor.get("login")
    return getattr(actor, "login", None)


def _github_status(exc: BaseException) -> int | None:
    if isinstance(exc, GithubException):
        return exc.status
    return None


def _is_non_retryable_github_error(exc: BaseException) -> bool:
    status = _github_status(exc)
    if status is None:
        return False
    # 404/403/401 will not succeed on retry — fail fast.
    return status in (401, 403, 404, 422)


class WorkflowRunFetchError(Exception):
    """Raised when a workflow run cannot be fetched from the GitHub API."""

    def __init__(
        self,
        message: str,
        *,
        api_call: str,
        repository: str,
        workflow_run_id: int,
        status: int | None = None,
        original: Exception | None = None,
    ) -> None:
        super().__init__(message)
        self.api_call = api_call
        self.repository = repository
        self.workflow_run_id = workflow_run_id
        self.status = status
        self.original = original


@dataclass
class WorkflowRunRef:
    """Workflow run metadata from the GitHub API or webhook event payload."""

    id: int
    name: str
    path: str
    head_branch: str
    head_sha: str
    conclusion: str
    html_url: str
    event: str
    run_attempt: int
    actor: Any = None
    triggering_actor: Any = None
    repository: str | None = None
    source: str = "api"

    @classmethod
    def from_api(cls, run: WorkflowRun, repository: str) -> WorkflowRunRef:
        return cls(
            id=run.id,
            name=run.name or "unknown",
            path=run.path or "",
            head_branch=run.head_branch or "",
            head_sha=run.head_sha or "",
            conclusion=run.conclusion or "unknown",
            html_url=run.html_url or "",
            event=run.event or "",
            run_attempt=run.run_attempt or 1,
            actor=run.actor,
            triggering_actor=getattr(run, "triggering_actor", None),
            repository=repository,
            source="api",
        )

    @classmethod
    def from_event(cls, wr: dict[str, Any], repository: str) -> WorkflowRunRef:
        return cls(
            id=int(wr["id"]),
            name=wr.get("name") or "unknown",
            path=wr.get("path") or "",
            head_branch=wr.get("head_branch") or "",
            head_sha=wr.get("head_sha") or "",
            conclusion=wr.get("conclusion") or "failure",
            html_url=wr.get("html_url") or "",
            event=wr.get("event") or "",
            run_attempt=wr.get("run_attempt") or 1,
            actor=wr.get("actor"),
            triggering_actor=wr.get("triggering_actor"),
            repository=repository,
            source="event",
        )

    @classmethod
    def minimal(cls, run_id: int, repository: str, conclusion: str = "failure") -> WorkflowRunRef:
        return cls(
            id=run_id,
            name="unknown",
            path="",
            head_branch="",
            head_sha="",
            conclusion=conclusion,
            html_url="",
            event="",
            run_attempt=1,
            repository=repository,
            source="minimal",
        )


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
        endpoint = f"GET /repos/{self.repository}"
        self._log_api_endpoint(endpoint)
        return self._gh.get_repo(self.repository)

    def parse_workflow_run_event(self, event_path: str) -> dict[str, Any]:
        with open(event_path, encoding="utf-8") as f:
            return json.load(f)

    @staticmethod
    def _log_api_endpoint(endpoint: str) -> None:
        line = f"[github-debug] API endpoint: {endpoint}"
        logger.info(line)
        print(line, file=sys.stderr)

    def log_debug_context(self, run_id: int, event_path: str | None = None) -> dict[str, Any]:
        """Log authenticated user, repository, and event payload run metadata."""
        auth_login = "unknown"
        try:
            auth_login = self._gh.get_user().login
        except GithubException as exc:
            logger.warning("Could not resolve authenticated GitHub user: %s", exc)

        event: dict[str, Any] = {}
        event_run_id: int | None = None
        event_name: str | None = None
        event_url: str | None = None
        if event_path:
            try:
                event = self.parse_workflow_run_event(event_path)
                wr = event.get("workflow_run", {})
                event_run_id = wr.get("id")
                event_name = wr.get("name")
                event_url = wr.get("html_url")
            except Exception as exc:
                logger.warning("Could not parse GITHUB_EVENT_PATH: %s", exc)

        debug = {
            "authenticated_user": auth_login,
            "repository": self.repository,
            "workflow_run_id_arg": run_id,
            "event_workflow_run_id": event_run_id,
            "event_workflow_name": event_name,
            "event_workflow_url": event_url,
            "ids_match": event_run_id is None or int(event_run_id) == int(run_id),
        }
        for key, value in debug.items():
            line = f"[github-debug] {key}={value}"
            logger.info(line)
            print(line, file=sys.stderr)
        return debug

    def _format_github_error(
        self,
        api_call: str,
        run_id: int,
        exc: Exception,
    ) -> str:
        status = _github_status(exc)
        return (
            f"GitHub API call failed: {api_call} | "
            f"repository={self.repository} | workflow_run_id={run_id} | "
            f"status={status} | error={exc}"
        )

    def _fetch_workflow_run_api(self, run_id: int) -> WorkflowRun:
        api_call = f"GET /repos/{self.repository}/actions/runs/{run_id}"
        self._log_api_endpoint(api_call)
        self.audit.log(
            AuditAction.GITHUB_FETCH,
            workflow_run_id=run_id,
            repository=self.repository,
            details={"resource": "workflow_run", "endpoint": api_call},
        )

        last_exc: Exception | None = None
        for attempt in range(3):
            try:
                return self.repo.get_workflow_run(run_id)
            except GithubException as exc:
                last_exc = exc
                detail = self._format_github_error(api_call, run_id, exc)
                logger.error(detail)
                print(detail, file=sys.stderr)
                print(traceback.format_exc(), file=sys.stderr)
                if _is_non_retryable_github_error(exc):
                    raise WorkflowRunFetchError(
                        detail,
                        api_call=api_call,
                        repository=self.repository,
                        workflow_run_id=run_id,
                        status=exc.status,
                        original=exc,
                    ) from exc
                if attempt >= 2:
                    raise WorkflowRunFetchError(
                        detail,
                        api_call=api_call,
                        repository=self.repository,
                        workflow_run_id=run_id,
                        status=exc.status,
                        original=exc,
                    ) from exc
                time.sleep(min(2**attempt, 30))
        raise WorkflowRunFetchError(
            self._format_github_error(api_call, run_id, last_exc or Exception("unknown")),
            api_call=api_call,
            repository=self.repository,
            workflow_run_id=run_id,
            status=_github_status(last_exc) if last_exc else None,
            original=last_exc,
        )

    def _repository_from_event(self, event: dict[str, Any]) -> str:
        wr = event.get("workflow_run", {})
        return (
            wr.get("repository", {}).get("full_name")
            or event.get("repository", {}).get("full_name")
            or self.repository
        )

    def resolve_workflow_run(
        self,
        run_id: int,
        event_path: str | None = None,
    ) -> tuple[WorkflowRunRef, list[str]]:
        """Resolve workflow run from API, falling back to GITHUB_EVENT_PATH on 404."""
        self.log_debug_context(run_id, event_path)
        warnings: list[str] = []
        event: dict[str, Any] | None = None

        if event_path:
            try:
                event = self.parse_workflow_run_event(event_path)
                event_run_id = event.get("workflow_run", {}).get("id")
                if event_run_id is not None and int(event_run_id) != int(run_id):
                    msg = (
                        f"workflow_run_id mismatch: CLI/env={run_id} "
                        f"vs github.event.workflow_run.id={event_run_id}; "
                        f"using event payload id={event_run_id}"
                    )
                    warnings.append(msg)
                    print(f"[github-debug] WARN: {msg}", file=sys.stderr)
                    run_id = int(event_run_id)
            except Exception as exc:
                msg = f"Failed to parse event payload: {exc}"
                warnings.append(msg)
                print(f"[github-debug] WARN: {msg}", file=sys.stderr)

        try:
            api_run = self._fetch_workflow_run_api(run_id)
            repo_name = self.repository
            if event:
                repo_name = self._repository_from_event(event)
            return WorkflowRunRef.from_api(api_run, repo_name), warnings
        except WorkflowRunFetchError as exc:
            tb = traceback.format_exc()
            warnings.append(str(exc))
            print(tb, file=sys.stderr)

            if event and event.get("workflow_run"):
                repo_name = self._repository_from_event(event)
                msg = (
                    f"Falling back to GITHUB_EVENT_PATH for workflow run #{run_id} "
                    f"(API {exc.api_call} returned {exc.status})"
                )
                warnings.append(msg)
                print(f"[github-debug] {msg}", file=sys.stderr)
                return WorkflowRunRef.from_event(event["workflow_run"], repo_name), warnings

            msg = (
                f"Workflow run API fetch failed and no event payload available; "
                f"using minimal context for run #{run_id}"
            )
            warnings.append(msg)
            print(f"[github-debug] WARN: {msg}", file=sys.stderr)
            return WorkflowRunRef.minimal(run_id, self.repository), warnings

    @retry(
        retry=retry_if_exception(lambda exc: not _is_non_retryable_github_error(exc)),
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=1, min=2, max=30),
        reraise=True,
    )
    def get_workflow_run(self, run_id: int) -> WorkflowRun:
        """Fetch workflow run from API. Prefer resolve_workflow_run() for fallback support."""
        return self._fetch_workflow_run_api(run_id)

    def collect_context(self, run: WorkflowRunRef) -> WorkflowContext:
        logs = self._safe_get_workflow_logs(run)
        keywords = self.tools.extract_error_keywords(logs.log_text)

        with ThreadPoolExecutor(max_workers=3) as pool:
            commits_future = pool.submit(
                self.tools.analyze_recent_commits,
                self.commit_lookback_hours,
                head_sha=run.head_sha or None,
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

    def _safe_get_workflow_logs(self, run: WorkflowRunRef) -> WorkflowLogsResult:
        endpoint = f"GET /repos/{self.repository}/actions/runs/{run.id}/logs"
        self._log_api_endpoint(endpoint)
        try:
            return self.tools.get_workflow_logs(run.id)
        except Exception as exc:
            detail = self._format_github_error(endpoint, run.id, exc)
            logger.warning("get_workflow_logs failed: %s", detail)
            print(detail, file=sys.stderr)
            print(traceback.format_exc(), file=sys.stderr)
            return WorkflowLogsResult(
                workflow_name=run.name or "unknown",
                conclusion=run.conclusion or "failure",
                started_at="",
                branch=run.head_branch or "",
                head_sha=run.head_sha or "",
                log_text=f"[Logs unavailable via API: {exc}]",
            )

    def _fetch_run_metadata(self, run: WorkflowRunRef) -> tuple[str, str | None]:
        commit_msg = ""
        if run.head_sha:
            endpoint = f"GET /repos/{self.repository}/commits/{run.head_sha}"
            self._log_api_endpoint(endpoint)
            try:
                commit = self.repo.get_commit(run.head_sha)
                commit_msg = (commit.commit.message or "")[:500]
            except GithubException as exc:
                detail = self._format_github_error(endpoint, run.id, exc)
                logger.warning(detail)
                print(detail, file=sys.stderr)

        _, actor_email = self.resolve_triggering_actor(run)
        return commit_msg, actor_email

    def resolve_triggering_actor(self, run: WorkflowRunRef) -> tuple[str, str | None]:
        """Prefer triggering_actor (who started the run) for notifications."""
        for attr in ("triggering_actor", "actor"):
            actor = getattr(run, attr, None)
            login = _actor_login(actor)
            if not login:
                continue
            email = None
            endpoint = f"GET /users/{login}"
            self._log_api_endpoint(endpoint)
            try:
                user = self._gh.get_user(login)
                email = user.email
            except GithubException as exc:
                detail = self._format_github_error(endpoint, run.id, exc)
                logger.debug(detail)
            return login, email
        return "unknown", None

    def find_existing_failure_issue(self, run_id: int, label: str) -> Issue | None:
        endpoint = f"GET /repos/{self.repository}/issues?labels={label}"
        self._log_api_endpoint(endpoint)
        title_prefix = f"[Pipeline Failure] Run #{run_id}"
        try:
            for issue in self.repo.get_issues(state="open", labels=[label]):
                if issue.title.startswith(title_prefix) or f"Run #{run_id}" in (issue.body or ""):
                    return issue
        except GithubException as exc:
            detail = self._format_github_error(endpoint, run_id, exc)
            logger.warning(detail)
            print(detail, file=sys.stderr)
        return None

    def create_issue(self, title: str, body: str, labels: list[str]) -> Issue:
        endpoint = f"POST /repos/{self.repository}/issues"
        self._log_api_endpoint(endpoint)
        return self.repo.create_issue(title=title, body=body, labels=labels)

    def update_issue(self, issue: Issue, body: str) -> Issue:
        endpoint = f"PATCH /repos/{self.repository}/issues/{issue.number}"
        self._log_api_endpoint(endpoint)
        issue.edit(body=body)
        return issue

    def close(self) -> None:
        self.tools.close()
        self._gh.close()
