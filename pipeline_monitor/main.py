"""CLI entry point — JSON on stdout, logs on stderr."""

from __future__ import annotations

import argparse
import json
import sys

from pipeline_monitor.config import Settings, load_settings
from pipeline_monitor.investigate import handle_success, investigate_failure
from pipeline_monitor.investigator import Investigator


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Pipeline Health Monitor — process a GitHub Actions workflow run.",
    )
    parser.add_argument(
        "--workflow-run-id",
        "-r",
        type=int,
        default=None,
        help="GitHub Actions workflow run ID.",
    )
    parser.add_argument(
        "--status",
        "-s",
        default=None,
        choices=["failure", "success"],
        help="Workflow conclusion: failure (full investigation) or success (lightweight).",
    )
    return parser


def _resolve_run_id_and_status(
    args: argparse.Namespace,
    settings: Settings,
) -> tuple[int, str]:
    """Resolve run ID and status from CLI args, env, or webhook event."""
    run_id = args.workflow_run_id or settings.workflow_run_id
    status = args.status or settings.workflow_run_status

    investigator = Investigator(settings)
    try:
        if run_id is None or status is None:
            if settings.github_event_path:
                event = investigator.github.parse_workflow_run_event(settings.github_event_path)
                wr = event.get("workflow_run", {})
                run_id = run_id or wr.get("id")
                status = status or wr.get("conclusion")

        if run_id is not None and status is None:
            run_ref, _ = investigator.github.resolve_workflow_run(
                int(run_id), settings.github_event_path
            )
            status = run_ref.conclusion or "unknown"

        if run_id is None:
            print(
                "ERROR: --workflow-run-id (or WORKFLOW_RUN_ID) is required.",
                file=sys.stderr,
            )
            sys.exit(2)

        if status not in ("failure", "success"):
            # cancelled, timed_out, etc. — investigate as failure
            if status in ("cancelled", "timed_out", "action_required", "skipped", "neutral"):
                return int(run_id), status
            print(
                f"ERROR: unsupported status '{status}'. Use --status failure or success.",
                file=sys.stderr,
            )
            sys.exit(2)

        return int(run_id), status
    finally:
        investigator._cleanup()


def _emit_result(result: dict) -> None:
    """Print structured JSON to stdout only."""
    sys.stdout.write(json.dumps(result))
    sys.stdout.write("\n")
    sys.stdout.flush()


def _exit_code(result: dict) -> int:
    if result.get("success"):
        return 0
    if result.get("error"):
        return 0  # expected skips (rate limit, dedupe) still exit 0
    return 1


def main(argv: list[str] | None = None) -> None:
    args = build_parser().parse_args(argv)
    settings = load_settings()

    if args.workflow_run_id is not None:
        settings.workflow_run_id = args.workflow_run_id
    if args.status is not None:
        settings.workflow_run_status = args.status

    run_id, status = _resolve_run_id_and_status(args, settings)

    if status == "success":
        result = handle_success(str(run_id), settings)
    else:
        # failure, cancelled, timed_out, and other non-success conclusions
        result = investigate_failure(str(run_id), settings)

    _emit_result(result)
    sys.exit(_exit_code(result))


if __name__ == "__main__":
    main()
