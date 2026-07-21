from __future__ import annotations

import argparse
import datetime as dt
import json
from pathlib import Path

from .acquisition import GCSObjectStore, acquire_approved
from .config import load_plan_config
from .discovery import discover_all
from .exa import ExaClient
from .evaluation import evaluate_run
from .registry import load_admin_registry
from .review import apply_review_workbook, export_review_workbook
from ..logging_utils import configure_logging


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="ldt-plans")
    commands = parser.add_subparsers(dest="command", required=True)

    def configured(name: str, *, run_id: bool = False):
        command = commands.add_parser(name)
        command.add_argument("--config", required=True)
        command.add_argument("--data-root")
        if run_id:
            command.add_argument("--run-id", required=True)
        return command

    configured("validate-config")
    discover = configured("discover")
    discover.add_argument("--run-id")
    discover.add_argument("--admin2-id", action="append", default=[])
    discover.add_argument("--limit", type=int)
    discover.add_argument("--force", action="store_true")
    export = configured("export-review", run_id=True)
    export.add_argument("--output")
    apply_review = configured("apply-review", run_id=True)
    apply_review.add_argument("--workbook", required=True)
    configured("acquire", run_id=True)
    evaluate = configured("evaluate", run_id=True)
    evaluate.add_argument("--gold")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    config = load_plan_config(args.config, data_root=args.data_root)
    areas = load_admin_registry(config)
    if args.command == "validate-config":
        print(f"Valid development-plan configuration for {config.country_name} ({config.iso3})")
        print(f"Admin-2 areas: {len(areas)}")
        print(f"Registry: {config.registry_path}")
        print(f"Workspace: {config.workspace}")
        print(f"Destination: gs://{config.bucket}/{config.storage_prefix}")
    elif args.command == "discover":
        run_id = args.run_id or dt.datetime.now().strftime("%Y%m%dT%H%M%S")
        selected = areas
        if args.admin2_id:
            requested = set(args.admin2_id)
            selected = [area for area in areas if area.admin2_id in requested]
            missing = sorted(requested - {area.admin2_id for area in selected})
            if missing:
                raise ValueError(f"Unknown admin2 IDs: {', '.join(missing)}")
        if args.limit is not None and args.limit < 1:
            raise ValueError("--limit must be at least 1")
        config.prepare_run(run_id)
        logger = configure_logging(
            config.run_dir(run_id) / "logs" / "discovery.jsonl",
            "ldt.plans.discovery",
        )
        selections = discover_all(
            config,
            selected,
            run_id=run_id,
            client=ExaClient.from_config(config),
            logger=logger,
            force=args.force,
            limit=args.limit,
        )
        counts = {
            status: sum(item.proposed_decision == status for item in selections)
            for status in ("AUTO_ACCEPT", "REVIEW", "MISSING")
        }
        print(f"Discovery run: {run_id}")
        print(", ".join(f"{key}={value}" for key, value in counts.items()))
    elif args.command == "export-review":
        output = Path(args.output) if args.output else None
        workbook = export_review_workbook(config, args.run_id, areas, output=output)
        print(workbook)
    elif args.command == "apply-review":
        destination = apply_review_workbook(
            config,
            args.run_id,
            Path(args.workbook),
            areas,
        )
        print(destination)
    elif args.command == "acquire":
        config.prepare_run(args.run_id)
        logger = configure_logging(
            config.run_dir(args.run_id) / "logs" / "acquisition.jsonl",
            "ldt.plans.acquisition",
        )
        documents = acquire_approved(
            config,
            areas,
            run_id=args.run_id,
            content_client=ExaClient.from_config(config),
            object_store=GCSObjectStore(config.bucket),
            logger=logger,
        )
        print(f"Verified GCS documents: {len(documents)}")
    elif args.command == "evaluate":
        report = evaluate_run(
            config,
            args.run_id,
            areas,
            gold_path=Path(args.gold) if args.gold else None,
        )
        print(json.dumps(report, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
