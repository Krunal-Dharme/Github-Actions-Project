# Security Checklist

Production security requirements for the Pipeline Health Monitor agent (`pipeline_agent/`).

## GitHub Permissions

- [x] **Minimal required permissions** — The workflow uses read-only access plus issues write only:
  - `actions: read` — fetch workflow runs and logs
  - `contents: read` — read commits and repository metadata
  - `issues: write` — create/update investigation issues
  - `pull-requests: read` — optional context for related PRs
- [x] **No admin or deployment permissions** — No `write` on contents, packages, deployments, or environments.
- [x] **Self-trigger prevention** — The investigate workflow skips runs triggered by itself.

## Tools (Read-Only by Design)

- [x] **GitHub tools are read-only** — `get_workflow_logs`, `analyze_recent_commits`, and `search_similar_issues` only fetch data; they never mutate repository state.
- [x] **Issue writes are isolated** — Issue creation is either deferred to the GitHub Actions workflow (`SKIP_GITHUB_ISSUE_PUBLISH=true`) or handled by `IssueManager` with issues scope only.
- [x] **Postgres is optional** — When enabled, used for investigation history and audit persistence only.

## Secrets Protection

- [x] **Secrets scanner active on all outputs** — `pipeline_agent/tools/secrets_scanner.py` scans workflow logs, LLM responses, and final reports before publishing.
- [x] **Redaction before LLM and publish** — `safe_output()` redacts tokens, passwords, API keys, and connection strings.
- [x] **LLM API keys stored as secrets** — `OPENROUTER_API_KEY` must be set via GitHub Secrets or `.env` (never committed).
- [x] **No secrets in code** — All credentials load from environment via `pipeline_agent/config.py`.
- [x] **`.env` excluded** — Use `.env.example` as a template only; never commit real secrets.

## Audit & Monitoring

- [x] **Audit logging enabled** — `pipeline_agent/audit_logger.py` writes structured JSONL to `AUDIT_LOG_DIR` and stderr.
- [x] **Security events tracked** — Secrets redaction, rate-limit hits, and destructive command flags are logged to `security_*.jsonl`.
- [x] **Optional Postgres audit trail** — When `POSTGRES_AUDIT_ENABLED=true`, audit events persist to GCP Postgres.
- [x] **Investigation artifacts** — GitHub Actions uploads `investigation.json` for post-mortem review.

## Rate Limiting & Cost Controls

- [x] **Daily investigation cap** — `MAX_INVESTIGATIONS_PER_DAY` (default: 50).
- [x] **Per-run LLM limit** — `MAX_LLM_CALLS_PER_RUN` (default: 1).
- [x] **Dedup window** — Same error fingerprint within `DEDUPE_WINDOW_HOURS` is skipped.
- [x] **Log truncation** — `MAX_LOG_CHARS` limits data sent to the LLM.

## Token Scoping

- [x] **GitHub token scoped correctly** — Use the default `GITHUB_TOKEN` with workflow-level permissions only; do not use PATs with admin or repo write scope.
- [x] **OpenRouter key is LLM-only** — No cross-service reuse of API keys.

## Human-in-the-Loop

- [x] **No production modifications without approval** — The agent analyzes and reports only; it never pushes code, merges PRs, or deploys.
- [x] **Destructive commands labeled, never executed** — `pipeline_agent/safety.py` flags commands like `kubectl delete`, `terraform destroy`, and `DROP TABLE`; they appear in reports with explicit human-approval warnings only.
- [x] **Suggested commands are advisory** — All remediation steps require manual review and execution.

## Deployment Verification

Before enabling in production, confirm:

1. Repository secrets: `OPENROUTER_API_KEY` (required), optional `DATABASE_URL`, SMTP credentials.
2. Repository variables: model name, feature flags, rate limits.
3. Workflow permissions match the checklist above.
4. `.env.example` documents all variables; no real values in the repo.
5. Audit logs are retained and reviewed periodically.

## Reporting Issues

If you suspect a secret leak or security regression:

1. Rotate affected credentials immediately.
2. Review audit logs under `AUDIT_LOG_DIR` or Postgres audit tables.
3. Check GitHub issue comments and investigation artifacts for redaction gaps.
