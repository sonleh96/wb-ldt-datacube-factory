from __future__ import annotations

import importlib
import logging

from .context import RunContext
from .task_state import TaskKey, TaskResult, TaskStateStore

DOMAINS = (
    "flood",
    "land_cover",
    "luminosity",
    "air_pollution",
    "emissions",
    "heatwaves",
    "internet",
    "tourism",
    "accessibility",
)


def run_domain(
    ctx: RunContext,
    name: str,
    phase: str,
    logger: logging.Logger,
    *,
    resume: bool | None = None,
    force: bool | None = None,
) -> list[TaskResult]:
    if name not in DOMAINS:
        raise ValueError(f"Unknown domain: {name}")
    phases = ("extract", "process") if phase == "all" else (phase,)
    results: list[TaskResult] = []
    store = TaskStateStore(ctx.config)
    resume = ctx.resume if resume is None else resume
    force = ctx.force if force is None else force
    for selected in phases:
        if selected not in ("extract", "process"):
            raise ValueError(f"Unknown phase: {selected}")
        module = importlib.import_module(f"ldt_factory.domains.{selected}.{name}")
        task = TaskKey("domain", name, selected)
        results.append(
            store.execute(
                ctx,
                task,
                logger,
                lambda module=module: module.run(ctx, logger),
                resume=bool(resume),
                force=bool(force),
            )
        )
    return results
