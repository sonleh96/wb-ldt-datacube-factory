from __future__ import annotations

import argparse

from .config import load_plan_config
from .registry import load_admin_registry


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="ldt-plans")
    commands = parser.add_subparsers(dest="command", required=True)

    validate = commands.add_parser("validate-config")
    validate.add_argument("--config", required=True)
    validate.add_argument("--data-root")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.command == "validate-config":
        config = load_plan_config(args.config, data_root=args.data_root)
        areas = load_admin_registry(config)
        print(f"Valid development-plan configuration for {config.country_name} ({config.iso3})")
        print(f"Admin-2 areas: {len(areas)}")
        print(f"Registry: {config.registry_path}")
        print(f"Workspace: {config.workspace}")
        print(f"Destination: gs://{config.bucket}/{config.storage_prefix}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
