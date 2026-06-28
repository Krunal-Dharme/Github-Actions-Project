"""Detect and redact secrets from agent outputs before publish or storage."""

from __future__ import annotations

import re
import sys
from dataclasses import dataclass, field
from typing import ClassVar


@dataclass
class ScanResult:
    original: str
    redacted: str
    findings: list[str] = field(default_factory=list)

    @property
    def had_secrets(self) -> bool:
        return len(self.findings) > 0


class SecretsScanner:
    """Detect and redact secrets from agent outputs."""

    PATTERNS: ClassVar[dict[str, str]] = {
        "aws_key": r"AKIA[0-9A-Z]{16}",
        "github_token": r"gh[pousr]_[A-Za-z0-9_]{36,255}",
        "generic_api_key": r'api[_-]?key["\']?\s*[:=]\s*["\']?([a-zA-Z0-9_\-]{20,})',
        "password": r'password["\']?\s*[:=]\s*["\']?([^\s"\']{8,})',
        "private_key": r"-----BEGIN (RSA |OPENSSH )?PRIVATE KEY-----",
        "jwt": r"eyJ[A-Za-z0-9-_=]+\.eyJ[A-Za-z0-9-_=]+\.?[A-Za-z0-9-_.+/=]*",
        "connection_string": r"(postgres|mysql|mongodb)://[^:]+:[^@]+@",
        # Production extensions (same redaction pipeline)
        "openrouter_key": r"sk-or-v1-[A-Za-z0-9]{20,}",
        "bearer_token": r"(?i)bearer\s+[A-Za-z0-9_\-.]{16,}",
        "slack_webhook": r"https://hooks\.slack\.com/services/[A-Za-z0-9/]+",
    }

    @staticmethod
    def scan(text: str) -> tuple[bool, list[str]]:
        found_secrets: list[str] = []
        for secret_type, pattern in SecretsScanner.PATTERNS.items():
            if re.search(pattern, text, re.IGNORECASE):
                found_secrets.append(secret_type)
        return len(found_secrets) > 0, found_secrets

    @staticmethod
    def redact(text: str) -> str:
        redacted = text
        for secret_type, pattern in SecretsScanner.PATTERNS.items():
            redacted = re.sub(
                pattern,
                f"[REDACTED:{secret_type.upper()}]",
                redacted,
                flags=re.IGNORECASE,
            )
        return redacted

    def scan_and_redact(self, text: str) -> ScanResult:
        """Scan and redact in one step (used by the investigation pipeline)."""
        had_secrets, findings = self.scan(text)
        redacted = self.redact(text) if had_secrets else text
        return ScanResult(original=text, redacted=redacted, findings=findings)


def safe_output(text: str) -> str:
    """Process agent output to remove secrets before displaying or sending."""
    has_secrets, secret_types = SecretsScanner.scan(text)
    if has_secrets:
        print(
            f"WARNING: Detected secrets in output: {', '.join(secret_types)}",
            file=sys.stderr,
        )
        return SecretsScanner.redact(text)
    return text
