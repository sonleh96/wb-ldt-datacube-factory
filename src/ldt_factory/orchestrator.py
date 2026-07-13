from __future__ import annotations

import concurrent.futures
import datetime as dt
import logging
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from .combine import run as combine_run
from .config import FactoryConfig, load_config
from .context import RunContext
from .domain_runner import run_domain
from .geo import write_normalized_boundaries
from .logging_utils import configure_logging
from .locks import workspace_lock
from .prerequisites import PREREQUISITES, run as run_prerequisite
from .quality import run as quality_run
from .resources import ResourcePolicy
from .task_state import RunState, TaskKey, TaskResult, TaskStateStore, redact_sensitive
from .web import WEB_SOURCES, run as run_web


@dataclass(frozen=True)
class StageFailure:
    task_id: str
    error_type: str
    error_message: str


class PipelineStageError(RuntimeError):
    def __init__(self, stage: str, failures: list[StageFailure]):
        self.stage = stage
        self.failures = failures
        details = "; ".join(f"{item.task_id}: {item.error_type}: {item.error_message}" for item in failures)
        super().__init__(f"Pipeline stage {stage!r} failed ({details})")


def _run_operation(ctx: RunContext, task: TaskKey, logger: logging.Logger) -> list[TaskResult]:
    if task.kind == "domain":
        return run_domain(ctx, task.name, str(task.phase), logger, resume=ctx.resume, force=ctx.force)

    if task.kind == "web":
        operation: Callable[[], None] = lambda: run_web(ctx, task.name, logger)
    elif task.kind == "prerequisite":
        operation = lambda: run_prerequisite(ctx, task.name, logger)
    elif task.kind == "boundary":
        operation = lambda: write_normalized_boundaries(ctx.config)
    elif task.kind == "combine":
        operation = lambda: combine_run(ctx, logger)
    elif task.kind == "quality":
        operation = lambda: quality_run(ctx, logger)
    else:
        raise ValueError(f"Unknown task kind: {task.kind}")

    result = TaskStateStore(ctx.config).execute(
        ctx,
        task,
        logger,
        operation,
        resume=ctx.resume,
        force=ctx.force,
    )
    return [result]


def run_unit(ctx: RunContext, task: TaskKey, logger: logging.Logger) -> list[TaskResult]:
    """Run one state-managed unit; used by both the CLI and the pipeline."""

    return _run_operation(ctx, task, logger)


def build_pipeline_stages(
    config: FactoryConfig,
    *,
    include_optional: bool = False,
) -> list[tuple[str, list[TaskKey]]]:
    pipeline = config.pipeline
    domains = list(dict.fromkeys(pipeline.get("main_domains", [])))
    if "accessibility" not in domains:
        domains.append("accessibility")
    if include_optional:
        domains.extend(name for name in pipeline.get("optional_domains", []) if name not in domains)
    return [
        ("boundaries", [TaskKey("boundary", "normalized")]),
        ("web", [TaskKey("web", name) for name in WEB_SOURCES]),
        ("prerequisites", [TaskKey("prerequisite", name) for name in PREREQUISITES]),
        (
            "domains",
            [
                *[TaskKey("domain", name, "extract") for name in domains],
                *[TaskKey("domain", name, "process") for name in domains],
            ],
        ),
        (
            "combine",
            [TaskKey("combine", "indicators", "accessibility")],
        ),
        (
            "quality",
            [TaskKey("quality", "publication", "accessibility")],
        ),
    ]


def _worker(
    config_path: str,
    run_id: str,
    task: TaskKey,
    resume: bool,
    force: bool,
) -> list[TaskResult]:
    config = load_config(config_path)
    config.prepare_directories()
    worker_threads = str(ResourcePolicy.from_config(config).worker_threads)
    for variable in (
        "OMP_NUM_THREADS",
        "OPENBLAS_NUM_THREADS",
        "MKL_NUM_THREADS",
        "NUMEXPR_NUM_THREADS",
        "GDAL_NUM_THREADS",
    ):
        os.environ[variable] = worker_threads
    try:
        import pyarrow as pa

        pa.set_cpu_count(int(worker_threads))
        pa.set_io_thread_count(int(worker_threads))
    except ImportError:
        pass
    ctx = RunContext(config, run_id, resume=resume, force=force)
    phase_suffix = f"-{task.phase}" if task.phase else ""
    logger = configure_logging(
        ctx.log_dir / f"{task.kind}-{task.name}{phase_suffix}.jsonl",
        f"ldt.{task.kind}.{task.name}{phase_suffix}",
    )
    return _run_operation(ctx, task, logger)


def _progress_fields(
    *,
    completed: int,
    total: int,
    failed: int,
    skipped: int,
    pending: int,
) -> dict[str, object]:
    return {
        "progress_current": completed + failed,
        "progress_total": total,
        "progress_percent": round(100 * (completed + failed) / total, 1) if total else 100.0,
        "completed_count": completed,
        "failed_count": failed,
        "skipped_count": skipped,
        "pending_count": pending,
    }


def _parallel(
    config_path: str,
    run_id: str,
    stage: str,
    tasks: list[TaskKey],
    policy: ResourcePolicy,
    logger: logging.Logger,
    run_state: RunState,
    *,
    resume: bool,
    force: bool,
) -> list[TaskResult]:
    if not tasks:
        return []

    total = len(tasks)
    completed = failed_count = skipped = 0
    failures: list[StageFailure] = []
    results: list[TaskResult] = []
    pending = list(tasks)
    batch_task_ids = {task.id for task in tasks}
    successful_task_ids: set[str] = set()
    running: dict[concurrent.futures.Future[list[TaskResult]], TaskKey] = {}
    used: dict[str, int] = {}
    stop_submitting = False
    store = TaskStateStore(load_config(config_path))
    executor = concurrent.futures.ProcessPoolExecutor(max_workers=min(policy.max_parallel, total))
    try:
        while pending or running:
            submitted = True
            while not stop_submitting and len(running) < policy.max_parallel and submitted:
                submitted = False
                for index, task in enumerate(pending):
                    phase_dependency = TaskKey("domain", task.name, "extract").id
                    if (
                        task.kind == "domain"
                        and task.phase == "process"
                        and phase_dependency in batch_task_ids
                        and phase_dependency not in successful_task_ids
                    ):
                        continue
                    if not policy.can_reserve(task, used):
                        continue
                    pending.pop(index)
                    policy.reserve(task, used)
                    future = executor.submit(_worker, config_path, run_id, task, resume, force)
                    running[future] = task
                    submitted = True
                    logger.info(
                        "task dispatched: %s resources=%s",
                        task.id,
                        ",".join(sorted(policy.requirements(task))) or "standard",
                        extra={
                            "task_id": task.id,
                            "kind": task.kind,
                            "domain": task.name if task.kind == "domain" else None,
                            "phase": task.phase,
                            "status": "dispatched",
                            "resource_class": sorted(policy.requirements(task)),
                            "run_id": run_id,
                            **_progress_fields(
                                completed=completed,
                                total=total,
                                failed=failed_count,
                                skipped=skipped,
                                pending=len(pending) + len(running),
                            ),
                        },
                    )
                    break

            if not running:
                if stop_submitting:
                    break
                blocked = {task.id: policy.requirements(task) for task in pending}
                raise RuntimeError(f"No pending task fits the configured resource limits: {blocked}")

            done, _ = concurrent.futures.wait(running, return_when=concurrent.futures.FIRST_COMPLETED)
            for future in done:
                task = running.pop(future)
                policy.release(task, used)
                try:
                    task_results = future.result()
                except Exception as error:
                    failed_count += 1
                    stop_submitting = True
                    manifest = store.read(task) or {}
                    if manifest.get("status") == "running":
                        store.mark_abandoned(task, run_id=run_id, reason=f"Worker exited: {type(error).__name__}: {error}")
                        manifest = store.read(task) or manifest
                    failure = StageFailure(
                        task.id,
                        type(error).__name__,
                        redact_sensitive(store.config, error),
                    )
                    failures.append(failure)
                    failed_result = TaskResult(
                        task.id,
                        str(manifest.get("status", "failed")),
                        str(manifest.get("fingerprint", "unknown")),
                        float(manifest.get("elapsed_seconds", 0.0)),
                        tuple(manifest.get("artifacts", [])),
                        str(manifest.get("error_type", failure.error_type)),
                        str(manifest.get("error_message", failure.error_message)),
                    )
                    run_state.record(failed_result)
                    logger.error(
                        "task failed: %s: %s: %s",
                        task.id,
                        failure.error_type,
                        failure.error_message,
                        extra={
                            "task_id": task.id,
                            "kind": task.kind,
                            "domain": task.name if task.kind == "domain" else None,
                            "phase": task.phase,
                            "status": "failed",
                            "error_type": failure.error_type,
                            "error_message": failure.error_message,
                            "run_id": run_id,
                            **_progress_fields(
                                completed=completed,
                                total=total,
                                failed=failed_count,
                                skipped=skipped,
                                pending=len(pending) + len(running),
                            ),
                        },
                    )
                    continue

                for result in task_results:
                    results.append(result)
                    run_state.record(result)
                    successful_task_ids.add(result.task_id)
                    if result.status == "skipped":
                        skipped += 1
                    else:
                        completed += 1
                    logger.info(
                        "task finished: %s status=%s elapsed=%.3fs",
                        result.task_id,
                        result.status,
                        result.elapsed_seconds,
                        extra={
                            "task_id": result.task_id,
                            "status": result.status,
                            "elapsed_seconds": result.elapsed_seconds,
                            "run_id": run_id,
                            **_progress_fields(
                                completed=completed,
                                total=total,
                                failed=failed_count,
                                skipped=skipped,
                                pending=len(pending) + len(running),
                            ),
                        },
                    )
    except BaseException:
        for future, task in running.items():
            future.cancel()
            store.mark_abandoned(task, run_id=run_id, reason="Orchestrator was interrupted")
        raise
    finally:
        executor.shutdown(wait=True, cancel_futures=True)

    if failures:
        raise PipelineStageError(stage, failures)
    return results


def _summary(run_state: RunState) -> dict[str, int]:
    tasks = run_state.payload.get("tasks", {}).values()
    statuses = [item.get("status") for item in tasks]
    requested = len(run_state.payload.get("requested_tasks", []))
    recorded = len(statuses)
    return {
        "completed": statuses.count("completed"),
        "skipped": statuses.count("skipped"),
        "failed": statuses.count("failed"),
        "interrupted": statuses.count("interrupted"),
        "recorded": recorded,
        "requested": requested,
        "pending": max(0, requested - recorded),
    }


def _capture_terminal_manifests(
    run_state: RunState,
    store: TaskStateStore,
    tasks: list[TaskKey],
    run_id: str,
) -> None:
    recorded = run_state.payload.get("tasks", {})
    for task in tasks:
        if task.id in recorded:
            continue
        manifest = store.read(task) or {}
        status = str(manifest.get("status", ""))
        if manifest.get("run_id") != run_id or status not in {"failed", "interrupted"}:
            continue
        run_state.record(
            TaskResult(
                task.id,
                status,
                str(manifest.get("fingerprint", "unknown")),
                float(manifest.get("elapsed_seconds", 0.0)),
                tuple(manifest.get("artifacts", [])),
                str(manifest.get("error_type", "UnknownError")),
                str(manifest.get("error_message", "")),
            )
        )


def _run_pipeline_unlocked(
    config_path: str | Path,
    *,
    include_optional: bool = False,
    run_id: str | None = None,
    resume: bool | None = None,
    force: bool = False,
) -> str:
    config = load_config(config_path)
    config.prepare_directories()
    run_id = run_id or dt.datetime.now().strftime("%Y%m%dT%H%M%S")
    resume = config.resume_completed if resume is None else resume
    if force:
        resume = False
    ctx = RunContext(config, run_id, resume=bool(resume), force=force)
    logger = configure_logging(ctx.log_dir / "orchestrator.jsonl", "ldt.orchestrator")
    policy = ResourcePolicy.from_config(config)

    stages = dict(build_pipeline_stages(config, include_optional=include_optional))
    boundary_task = stages["boundaries"][0]
    web_tasks = stages["web"]
    prerequisite_tasks = stages["prerequisites"]
    domain_tasks = stages["domains"]
    combine_task = stages["combine"][0]
    quality_task = stages["quality"][0]
    requested = [task for stage_tasks in stages.values() for task in stage_tasks]
    run_state = RunState(config, run_id)
    task_store = TaskStateStore(config)
    run_state.start(resume=bool(resume), force=force, requested_tasks=[task.id for task in requested])
    logger.info(
        "pipeline started: run_id=%s tasks=%d max_parallel=%d",
        run_id,
        len(requested),
        policy.max_parallel,
        extra={
            "run_id": run_id,
            "status": "running",
            "progress_current": 0,
            "progress_total": len(requested),
            "progress_percent": 0.0,
            "resource_class": policy.limits,
        },
    )

    try:
        for result in _run_operation(ctx, boundary_task, logger):
            run_state.record(result)
        logger.info("pipeline stage started: web", extra={"phase": "web", "run_id": run_id, "status": "running"})
        _parallel(str(config.path), run_id, "web", web_tasks, policy, logger, run_state, resume=bool(resume), force=force)
        logger.info(
            "pipeline stage started: prerequisites",
            extra={"phase": "prerequisites", "run_id": run_id, "status": "running"},
        )
        _parallel(
            str(config.path),
            run_id,
            "prerequisites",
            prerequisite_tasks,
            policy,
            logger,
            run_state,
            resume=bool(resume),
            force=force,
        )
        logger.info("pipeline stage started: domains", extra={"phase": "domains", "run_id": run_id, "status": "running"})
        _parallel(
            str(config.path),
            run_id,
            "domains",
            domain_tasks,
            policy,
            logger,
            run_state,
            resume=bool(resume),
            force=force,
        )
        for result in _run_operation(ctx, combine_task, logger):
            run_state.record(result)
        for result in _run_operation(ctx, quality_task, logger):
            run_state.record(result)
    except KeyboardInterrupt:
        _capture_terminal_manifests(run_state, task_store, requested, run_id)
        summary = _summary(run_state)
        run_state.finish("interrupted", summary=summary)
        logger.warning(
            "pipeline interrupted: run_id=%s completed=%d skipped=%d pending=%d",
            run_id,
            summary["completed"],
            summary["skipped"],
            summary["pending"],
            extra={"run_id": run_id, "status": "interrupted", **{f"{key}_count": value for key, value in summary.items()}},
        )
        raise
    except Exception as error:
        _capture_terminal_manifests(run_state, task_store, requested, run_id)
        summary = _summary(run_state)
        error_message = redact_sensitive(config, error)
        run_state.finish(
            "failed",
            summary={**summary, "error_type": type(error).__name__, "error_message": error_message},
        )
        logger.error(
            "pipeline failed: run_id=%s error=%s: %s",
            run_id,
            type(error).__name__,
            error_message,
            extra={
                "run_id": run_id,
                "status": "failed",
                "error_type": type(error).__name__,
                "error_message": str(error),
                "completed_count": summary["completed"],
                "skipped_count": summary["skipped"],
                "failed_count": summary["failed"],
                "pending_count": summary["pending"],
            },
        )
        raise

    summary = _summary(run_state)
    run_state.finish("completed", summary=summary)
    logger.info(
        "pipeline completed: run_id=%s completed=%d skipped=%d",
        run_id,
        summary["completed"],
        summary["skipped"],
        extra={
            "run_id": run_id,
            "status": "completed",
            "progress_current": len(requested),
            "progress_total": len(requested),
            "progress_percent": 100.0,
            "completed_count": summary["completed"],
            "skipped_count": summary["skipped"],
            "failed_count": summary["failed"],
            "pending_count": summary["pending"],
        },
    )
    return run_id


def run_pipeline(
    config_path: str | Path,
    *,
    include_optional: bool = False,
    run_id: str | None = None,
    resume: bool | None = None,
    force: bool = False,
) -> str:
    """Run one orchestrated pipeline while preventing a second full-run writer."""
    config = load_config(config_path)
    config.prepare_directories()
    lock_path = config.workspace / "state" / "pipeline.lock"
    with workspace_lock(lock_path, label=f"{config.country_name} ({config.iso3}) workspace"):
        return _run_pipeline_unlocked(
            config_path,
            include_optional=include_optional,
            run_id=run_id,
            resume=resume,
            force=force,
        )
