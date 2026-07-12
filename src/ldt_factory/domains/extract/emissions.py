from __future__ import annotations

import logging

from ...context import RunContext
from ...logging_utils import logged_action


def run(ctx: RunContext, logger: logging.Logger) -> None:
    with logged_action(logger, "validate_input", domain="emissions"):
        for gas in ("CO2", "CH4"):
            root = ctx.raw(f"climate_trace_{ctx.config.iso3}_{gas}")
            if not root.is_dir():
                raise FileNotFoundError(f"Climate TRACE package is not extracted: {root}")
