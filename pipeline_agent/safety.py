"""Safety checks — flag destructive commands for human approval only."""

from __future__ import annotations

import re
from dataclasses import dataclass, field

# Patterns that must NEVER be executed automatically
DESTRUCTIVE_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    ("kubectl_delete", re.compile(r"kubectl\s+delete\b", re.IGNORECASE)),
    ("terraform_destroy", re.compile(r"terraform\s+destroy\b", re.IGNORECASE)),
    ("drop_database", re.compile(r"\bDROP\s+(DATABASE|TABLE|SCHEMA)\b", re.IGNORECASE)),
    ("rm_rf", re.compile(r"rm\s+-rf\s+/")),
    ("docker_prune", re.compile(r"docker\s+system\s+prune\s+-a", re.IGNORECASE)),
    ("aws_delete", re.compile(r"aws\s+\w+\s+delete-", re.IGNORECASE)),
    ("gcloud_delete", re.compile(r"gcloud\s+\w+\s+delete\b", re.IGNORECASE)),
    ("force_push", re.compile(r"git\s+push\s+.*--force", re.IGNORECASE)),
    ("truncate_table", re.compile(r"\bTRUNCATE\s+TABLE\b", re.IGNORECASE)),
]


@dataclass
class SafetyReport:
    flagged_commands: list[dict[str, str]] = field(default_factory=list)

    @property
    def has_destructive(self) -> bool:
        return len(self.flagged_commands) > 0


class SafetyChecker:
    """Scan LLM recommendations and flag destructive actions."""

    def scan_recommendations(self, text: str) -> SafetyReport:
        report = SafetyReport()
        lines = text.splitlines()

        for line_num, line in enumerate(lines, start=1):
            stripped = line.strip()
            if not stripped or stripped.startswith("#"):
                continue
            for category, pattern in DESTRUCTIVE_PATTERNS:
                if pattern.search(stripped):
                    report.flagged_commands.append(
                        {
                            "line": str(line_num),
                            "category": category,
                            "command": stripped[:200],
                        }
                    )

        return report

    def append_safety_warning(self, report_md: str, safety: SafetyReport) -> str:
        if not safety.has_destructive:
            return report_md

        warning = [
            "",
            "---",
            "",
            "## ⚠️ Human Approval Required",
            "",
            "The following commands were flagged as **potentially destructive**.",
            "They must **NOT** be executed automatically. Review and approve manually:",
            "",
        ]
        for item in safety.flagged_commands:
            warning.append(f"- **{item['category']}** (line {item['line']}): `{item['command']}`")
        warning.append("")

        return report_md + "\n".join(warning)
