from __future__ import annotations

import argparse
import datetime as dt
import json
import sys

from .config import load_config
from .context import RunContext
from .domain_runner import DOMAINS, run_domain
from .inspection import build_plan, load_status, run_preflight
from .logging_utils import configure_logging
from .orchestrator import run_pipeline, run_unit
from .prerequisites import PREREQUISITES
from .task_state import TaskKey
from .web import WEB_SOURCES


def _ctx(
    config_path: str,
    run_id: str | None,
    unit: str,
    *,
    resume: bool = False,
    force: bool = False,
):
    config = load_config(config_path)
    config.prepare_directories()
    resolved_id = run_id or dt.datetime.now().strftime("%Y%m%dT%H%M%S")
    ctx = RunContext(config, resolved_id, resume=resume, force=force)
    return ctx, configure_logging(ctx.log_dir / f"{unit}.jsonl", f"ldt.{unit}")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="ldt-factory")
    commands = parser.add_subparsers(dest="command", required=True)

    def configured(name, *, execution: bool = False):
        command = commands.add_parser(name)
        command.add_argument("--config", required=True)
        command.add_argument("--run-id")
        if execution:
            command.add_argument(
                "--resume",
                action=argparse.BooleanOptionalAction,
                default=None,
                help="reuse completed task manifests (pipeline default comes from YAML)",
            )
            command.add_argument(
                "--force",
                action="store_true",
                help="rerun task wrappers even when a completed manifest is current",
            )
        return command

    configured("validate")
    plan = configured("plan")
    plan.add_argument("--include-optional", action="store_true")
    plan.add_argument("--json", action="store_true")
    preflight = configured("preflight")
    preflight.add_argument("--include-optional", action="store_true")
    preflight.add_argument("--json", action="store_true")
    status = configured("status")
    status.add_argument("--json", action="store_true")
    configured("prepare-boundaries", execution=True)
    web = configured("run-web", execution=True)
    web.add_argument("--source", choices=WEB_SOURCES, required=True)
    prereq = configured("run-prerequisite", execution=True)
    prereq.add_argument("--name", choices=PREREQUISITES, required=True)
    domain = configured("run-domain", execution=True)
    domain.add_argument("--name", choices=DOMAINS, required=True)
    domain.add_argument("--phase", choices=("extract", "process", "all"), default="all")
    combine = configured("combine", execution=True)
    combine.add_argument("--include-accessibility", action="store_true")
    quality = configured("quality", execution=True)
    quality.add_argument("--include-accessibility", action="store_true")
    run = configured("run", execution=True)
    run.add_argument("--include-optional", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.command == "plan":
        config = load_config(args.config, require_boundaries=False)
        payload = build_plan(config, include_optional=args.include_optional)
        if args.json:
            print(json.dumps(payload, indent=2))
        else:
            print(
                f"Pipeline plan for {payload['country']}: max_parallel={payload['max_parallel']} "
                f"worker_threads={payload['worker_threads']} resume={payload['resume_completed']}"
            )
            for stage in payload["stages"]:
                print(f"{stage['name']}:")
                for task in stage["tasks"]:
                    resources = ", ".join(
                        f"{name}={amount}" for name, amount in task["resources"].items()
                    ) or "standard"
                    dependencies = ", ".join(task["depends_on"])
                    suffix = f"; depends on {dependencies}" if dependencies else ""
                    print(f"  {task['task_id']} [{resources}]{suffix}")
        return 0
    if args.command == "preflight":
        config = load_config(args.config, require_boundaries=False)
        payload = run_preflight(config, include_optional=args.include_optional)
        if args.json:
            print(json.dumps(payload, indent=2))
        else:
            print(
                f"Preflight for {payload['country']}: {payload['status'].upper()} "
                f"({payload['errors']} errors, {payload['warnings']} warnings)"
            )
            for check in payload["checks"]:
                print(f"[{check['status'].upper()}] {check['name']}: {check['detail']}")
        return 2 if payload["status"] == "blocked" else 0
    if args.command == "status":
        config = load_config(args.config, require_boundaries=False)
        try:
            payload = load_status(config, run_id=args.run_id)
        except (FileNotFoundError, ValueError) as error:
            print(str(error), file=sys.stderr)
            return 1
        if args.json:
            print(json.dumps(payload, indent=2))
        else:
            counts = ", ".join(f"{name}={count}" for name, count in sorted(payload["status_counts"].items()))
            print(
                f"Run {payload['run_id']}: {str(payload['status']).upper()} "
                f"recorded={payload['recorded']}/{payload['requested']} pending={payload['pending']}"
            )
            if counts:
                print(f"Tasks: {counts}")
            for task_id, task in sorted(payload["tasks"].items()):
                elapsed = task.get("elapsed_seconds")
                suffix = f" ({elapsed:.3f}s)" if isinstance(elapsed, (int, float)) else ""
                print(f"  {task_id}: {task.get('status', 'unknown')}{suffix}")
        return 0
    if args.command == "validate":
        config = load_config(args.config)
        print(f"Valid configuration for {config.country_name} ({config.iso3})")
        return 0
    if args.command == "run":
        run_id = run_pipeline(
            args.config,
            include_optional=args.include_optional,
            run_id=args.run_id,
            resume=args.resume,
            force=args.force,
        )
        print(f"Run completed: {run_id}")
        return 0

    direct_resume = bool(getattr(args, "resume", False))
    direct_force = bool(getattr(args, "force", False))
    if direct_force:
        direct_resume = False
    ctx, logger = _ctx(
        args.config,
        args.run_id,
        args.command,
        resume=direct_resume,
        force=direct_force,
    )
    if args.command == "prepare-boundaries":
        run_unit(ctx, TaskKey("boundary", "normalized"), logger)
        print(ctx.config.dataset_dir / f"GPBP_LDT_{ctx.config.iso3}_admin_2_regions.geojson")
    elif args.command == "run-web":
        run_unit(ctx, TaskKey("web", args.source), logger)
    elif args.command == "run-prerequisite":
        run_unit(ctx, TaskKey("prerequisite", args.name), logger)
    elif args.command == "run-domain":
        run_domain(ctx, args.name, args.phase, logger, resume=ctx.resume, force=ctx.force)
    elif args.command == "combine":
        phase = "accessibility" if args.include_accessibility else "standard"
        run_unit(ctx, TaskKey("combine", "indicators", phase), logger)
    elif args.command == "quality":
        phase = "accessibility" if args.include_accessibility else "standard"
        run_unit(ctx, TaskKey("quality", "publication", phase), logger)
        print(ctx.config.workspace / "quality" / "report.html")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
