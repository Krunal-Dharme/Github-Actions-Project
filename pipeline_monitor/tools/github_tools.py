"""Core GitHub access tools — refactored from agent_investigator snippets.

Production improvements over the original snippets:
- httpx with retries, zip-log support, parallel job log downloads
- Structured dataclass returns (not raw strings) + formatted summaries for LLM
- GitHub Search API for issue lookup (instead of scanning all issues)
- Time-window commit analysis with per-commit file change details
- Audit logging, type hints, no dotenv (config injected by caller)
"""

from __future__ import annotations

import re
import time
import zipfile
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from io import BytesIO
from typing import Any

import httpx
from tenacity import retry, retry_if_exception, stop_after_attempt, wait_exponential

from pipeline_monitor.audit_logger import AuditLogger


def _is_non_retryable_http_error(exc: BaseException) -> bool:
    if isinstance(exc, httpx.HTTPStatusError):
        return exc.response.status_code in (401, 403, 404, 422)
    return False


@dataclass
class JobStepFailure:
    job_name: str
    step_name: str
    conclusion: str
    log_excerpt: str
    last_lines: str = ""


@dataclass
class WorkflowLogsResult:
    workflow_name: str
    conclusion: str
    started_at: str
    branch: str
    head_sha: str
    failed_jobs: list[JobStepFailure] = field(default_factory=list)
    log_text: str = ""

    def to_summary(self) -> str:
        """Human-readable summary matching the original get_workflow_logs tool shape."""
        lines = [
            f"Workflow: {self.workflow_name}",
            f"Status: {self.conclusion}",
            f"Started: {self.started_at}",
            f"Branch: {self.branch}",
            "",
        ]
        for job in self.failed_jobs:
            lines.extend(
                [
                    f"Failed Job: {job.job_name}",
                    f"Failed Step: {job.step_name}",
                    f"Conclusion: {job.conclusion}",
                    "",
                    "Error excerpt:",
                    job.log_excerpt,
                    "",
                    "Last 50 lines of logs:",
                    job.last_lines,
                    "",
                ]
            )
        return "\n".join(lines)


@dataclass
class CommitInfo:
    sha: str
    author: str
    message: str
    date: str
    files_changed: list[str] = field(default_factory=list)

    def to_summary_line(self) -> str:
        files = ", ".join(self.files_changed[:5])
        extra = ""
        if len(self.files_changed) > 5:
            extra = f" ... and {len(self.files_changed) - 5} more files"
        return (
            f"Commit {self.sha} by {self.author} ({self.date})\n"
            f"Message: {self.message}\n"
            f"Files changed: {files}{extra}"
        )


@dataclass
class SimilarIssueInfo:
    number: int
    title: str
    state: str
    url: str
    solution_hint: str | None = None

    def to_summary_line(self) -> str:
        lines = [f"#{self.number}: {self.title}", f"State: {self.state}", f"URL: {self.url}"]
        if self.solution_hint:
            lines.append(f"Solution hint: {self.solution_hint}")
        return "\n".join(lines)


class GitHubTools:
    """Production GitHub tools used by the investigation agent."""

    LOG_TAIL_LINES = 50
    MAX_FILES_PREVIEW = 5

    def __init__(
        self,
        token: str,
        repository: str,
        audit: AuditLogger,
        api_url: str = "https://api.github.com",
        max_log_chars: int = 80_000,
    ) -> None:
        self.repository = repository
        self.audit = audit
        self.max_log_chars = max_log_chars
        self._token = token
        self._api_base = api_url.rstrip("/")
        self._http = httpx.Client(
            headers={
                "Authorization": f"Bearer {token}",
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": "2022-11-28",
            },
            timeout=45.0,
        )

    def close(self) -> None:
        self._http.close()

    def _timed_tool_call(self, tool_name: str, args: dict[str, Any], func: Any) -> Any:
        start = time.monotonic()
        result = func()
        duration = time.monotonic() - start
        preview = result.to_summary() if hasattr(result, "to_summary") else result
        if isinstance(preview, list):
            preview = f"{len(preview)} items"
        self.audit.log_tool_call(tool_name, args, preview, duration)
        return result

    # ------------------------------------------------------------------ #
    # Tool 1: get_workflow_logs (from snippet — improved)
    # ------------------------------------------------------------------ #

    @retry(
        retry=retry_if_exception(lambda exc: not _is_non_retryable_http_error(exc)),
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=1, min=2, max=20),
        reraise=True,
    )
    def get_workflow_logs(self, workflow_run_id: int) -> WorkflowLogsResult:
        """Fetch logs from a failed GitHub Actions workflow run."""
        return self._timed_tool_call(
            "get_workflow_logs",
            {"workflow_run_id": workflow_run_id, "repository": self.repository},
            lambda: self._fetch_workflow_logs(workflow_run_id),
        )

    def _fetch_workflow_logs(self, workflow_run_id: int) -> WorkflowLogsResult:
        run_url = f"{self._api_base}/repos/{self.repository}/actions/runs/{workflow_run_id}"
        print(f"[github-debug] API endpoint: GET {run_url}", flush=True)
        run_response = self._http.get(run_url)
        if run_response.status_code == 404:
            return WorkflowLogsResult(
                workflow_name="unknown",
                conclusion="failure",
                started_at="",
                branch="",
                head_sha="",
                log_text=(
                    f"[Logs unavailable: GET {run_url} returned 404 "
                    f"for repository={self.repository}]"
                ),
            )
        run_response.raise_for_status()
        run_data = run_response.json()

        jobs_url = f"{run_url}/jobs"
        print(f"[github-debug] API endpoint: GET {jobs_url}", flush=True)
        jobs_response = self._http.get(jobs_url, params={"per_page": 100})
        jobs_response.raise_for_status()
        jobs_data = jobs_response.json()

        result = WorkflowLogsResult(
            workflow_name=run_data.get("name", "unknown"),
            conclusion=run_data.get("conclusion", "unknown"),
            started_at=run_data.get("created_at", ""),
            branch=run_data.get("head_branch", ""),
            head_sha=run_data.get("head_sha", ""),
        )

        failed_entries = [
            job for job in jobs_data.get("jobs", []) if job.get("conclusion") == "failure"
        ]

        log_parts: list[str] = []
        if failed_entries:
            with ThreadPoolExecutor(max_workers=min(4, len(failed_entries))) as pool:
                futures = {
                    pool.submit(self._download_job_log, job["id"]): job for job in failed_entries
                }
                for future in as_completed(futures):
                    job = futures[future]
                    job_log = future.result()
                    job_name = job.get("name", "unknown")
                    log_parts.append(f"=== JOB: {job_name} ===\n{job_log}")
                    result.failed_jobs.append(self._build_job_failure(job, job_log))

        combined = "\n\n".join(log_parts)
        if len(combined) > self.max_log_chars:
            combined = combined[: self.max_log_chars] + "\n\n[LOG TRUNCATED]"
        result.log_text = combined
        return result

    def _build_job_failure(self, job: dict[str, Any], job_log: str) -> JobStepFailure:
        steps = job.get("steps", [])
        failed_step = next((s for s in steps if s.get("conclusion") == "failure"), None)
        step_name = failed_step["name"] if failed_step else "unknown"
        log_lines = job_log.splitlines()
        last_lines = "\n".join(log_lines[-self.LOG_TAIL_LINES :])
        return JobStepFailure(
            job_name=job.get("name", "unknown"),
            step_name=step_name,
            conclusion="failure",
            log_excerpt=self._extract_error_excerpt(job_log),
            last_lines=last_lines,
        )

    def _download_job_log(self, job_id: int) -> str:
        url = f"{self._api_base}/repos/{self.repository}/actions/jobs/{job_id}/logs"
        with httpx.Client(
            headers={
                "Authorization": f"Bearer {self._token}",
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": "2022-11-28",
            },
            timeout=45.0,
            follow_redirects=True,
        ) as client:
            response = client.get(url)
            if response.status_code == 404:
                return "[Log unavailable]"
            response.raise_for_status()
            content = response.content

        if content[:2] == b"PK":
            return self._extract_zip_logs(content)
        return content.decode("utf-8", errors="replace")

    @staticmethod
    def _extract_zip_logs(content: bytes) -> str:
        parts: list[str] = []
        with zipfile.ZipFile(BytesIO(content)) as zf:
            for name in sorted(zf.namelist()):
                parts.append(f"--- {name} ---\n{zf.read(name).decode('utf-8', errors='replace')}")
        return "\n".join(parts)

    @staticmethod
    def _extract_error_excerpt(log: str, context_lines: int = 40) -> str:
        lines = log.splitlines()
        error_indices = [
            i
            for i, line in enumerate(lines)
            if re.search(r"(?i)(error|failed|exception|traceback|fatal)", line)
        ]
        if not error_indices:
            return "\n".join(lines[-context_lines:])
        idx = error_indices[-1]
        start = max(0, idx - context_lines // 2)
        end = min(len(lines), idx + context_lines // 2)
        return "\n".join(lines[start:end])

    # ------------------------------------------------------------------ #
    # Tool 2: analyze_recent_commits (from snippet — improved)
    # ------------------------------------------------------------------ #

    def analyze_recent_commits(
        self,
        hours: int = 24,
        *,
        head_sha: str | None = None,
        limit: int = 10,
    ) -> list[CommitInfo]:
        """Analyze recent commits that might have caused the failure."""
        return self._timed_tool_call(
            "analyze_recent_commits",
            {"hours": hours, "head_sha": head_sha, "limit": limit, "repository": self.repository},
            lambda: self._fetch_recent_commits(hours, head_sha=head_sha, limit=limit),
        )

    def _fetch_recent_commits(
        self,
        hours: int,
        *,
        head_sha: str | None = None,
        limit: int = 10,
    ) -> list[CommitInfo]:
        since = (datetime.now(UTC) - timedelta(hours=hours)).strftime("%Y-%m-%dT%H:%M:%SZ")
        commits_url = f"{self._api_base}/repos/{self.repository}/commits"
        params: dict[str, str | int] = {"since": since, "per_page": limit}
        if head_sha:
            params["sha"] = head_sha

        response = self._http.get(commits_url, params=params)
        response.raise_for_status()
        commits = response.json()

        if not commits:
            return []

        results: list[CommitInfo] = []
        with ThreadPoolExecutor(max_workers=min(4, len(commits))) as pool:
            futures = {
                pool.submit(self._fetch_commit_files, c["sha"]): c for c in commits[:limit]
            }
            for future in as_completed(futures):
                commit = futures[future]
                files = future.result()
                message = commit["commit"]["message"].split("\n")[0]
                results.append(
                    CommitInfo(
                        sha=commit["sha"][:7],
                        author=commit["commit"]["author"]["name"],
                        message=message[:300],
                        date=commit["commit"]["author"]["date"],
                        files_changed=files,
                    )
                )

        results.sort(key=lambda c: c.date, reverse=True)
        return results

    def _fetch_commit_files(self, sha: str) -> list[str]:
        url = f"{self._api_base}/repos/{self.repository}/commits/{sha}"
        response = self._http.get(url)
        if response.status_code != 200:
            return []
        data = response.json()
        return [f["filename"] for f in data.get("files", [])]

    # ------------------------------------------------------------------ #
    # Tool 3: search_similar_issues (from snippet — improved)
    # ------------------------------------------------------------------ #

    def search_similar_issues(
        self,
        error_keywords: str,
        limit: int = 5,
    ) -> list[SimilarIssueInfo]:
        """Search GitHub issues for similar error messages using the Search API."""
        keywords = error_keywords.strip()
        if not keywords:
            return []

        return self._timed_tool_call(
            "search_similar_issues",
            {"error_keywords": keywords[:120], "limit": limit, "repository": self.repository},
            lambda: self._search_similar_issues(keywords, limit),
        )

    def _search_similar_issues(self, keywords: str, limit: int) -> list[SimilarIssueInfo]:
        query = f"repo:{self.repository} {keywords} is:issue"
        search_url = f"{self._api_base}/search/issues"
        response = self._http.get(
            search_url,
            params={"q": query, "sort": "relevance", "per_page": limit},
        )
        if response.status_code == 422:
            # Query too long or invalid — fall back to shortened keywords
            short = " ".join(keywords.split()[:6])
            response = self._http.get(
                search_url,
                params={"q": f"repo:{self.repository} {short} is:issue", "per_page": limit},
            )
        response.raise_for_status()
        payload = response.json()

        if payload.get("total_count", 0) == 0:
            return []

        results: list[SimilarIssueInfo] = []
        for issue in payload.get("items", [])[:limit]:
            hint = None
            if issue.get("state") == "closed" and issue.get("comments", 0) > 0:
                hint = self._fetch_solution_hint(issue.get("comments_url", ""))
            results.append(
                SimilarIssueInfo(
                    number=issue["number"],
                    title=issue["title"],
                    state=issue["state"],
                    url=issue["html_url"],
                    solution_hint=hint,
                )
            )
        return results

    def _fetch_solution_hint(self, comments_url: str) -> str | None:
        if not comments_url:
            return None
        response = self._http.get(comments_url, params={"per_page": 1})
        if response.status_code != 200:
            return None
        comments = response.json()
        if not comments:
            return None
        return comments[0]["body"][:200]

    @staticmethod
    def extract_error_keywords(log_text: str) -> str:
        """Build a search query string from workflow log text."""
        patterns = [
            r"(?i)ModuleNotFoundError:\s*No module named '([^']+)'",
            r"(?i)ImportError:\s*(.+)",
            r"(?i)(SyntaxError:.+)",
            r"(?i)npm ERR!\s*(.+)",
            r"(?i)pytest\.(?:\w+\.)?(\w+Error)",
            r"(?i)Error:\s*(.+)",
        ]
        keywords: list[str] = []
        for pattern in patterns:
            match = re.search(pattern, log_text)
            if match:
                token = match.group(1).strip()[:80]
                if token and token not in keywords:
                    keywords.append(token)
        if keywords:
            return " ".join(keywords[:4])
        # Fallback: last non-empty error-ish line
        for line in reversed(log_text.splitlines()):
            if re.search(r"(?i)(error|failed|exception)", line):
                return line.strip()[:100]
        return ""

    @staticmethod
    def format_commits_report(commits: list[CommitInfo], hours: int) -> str:
        if not commits:
            return f"No commits found in the last {hours} hours."
        lines = [f"Recent commits (last {hours} hours):\n"]
        for commit in commits:
            lines.append(commit.to_summary_line())
            lines.append("")
        return "\n".join(lines)

    @staticmethod
    def format_issues_report(issues: list[SimilarIssueInfo], keywords: str) -> str:
        if not issues:
            return f"No similar issues found for keywords: {keywords}"
        lines = [f"Found {len(issues)} similar issues:\n"]
        for issue in issues:
            lines.append(issue.to_summary_line())
            lines.append("")
        return "\n".join(lines)
