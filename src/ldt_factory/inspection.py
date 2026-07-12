from __future__ import annotations

import glob
import importlib.util
import json
import os
import shutil
from pathlib import Path
from typing import Any

from .config import FactoryConfig
from .domain_runner import DOMAINS
from .geo import load_admin2
from .orchestrator import build_pipeline_stages
from .raster_utils import validate_categorical_raster
from .resources import ResourcePolicy
from .task_state import TaskStateStore


def build_plan(config: FactoryConfig, *, include_optional: bool = False) -> dict[str, Any]:
    policy = ResourcePolicy.from_config(config)
    store = TaskStateStore(config)
    stages = []
    for stage_name, tasks in build_pipeline_stages(config, include_optional=include_optional):
        stages.append(
            {
                "name": stage_name,
                "tasks": [
                    {
                        "task_id": task.id,
                        "resources": policy.requirements(task),
                        "depends_on": [dependency.id for dependency in store.dependencies(task)],
                    }
                    for task in tasks
                ],
            }
        )
    return {
        "country": config.iso3,
        "max_parallel": policy.max_parallel,
        "worker_threads": policy.worker_threads,
        "resource_limits": policy.limits,
        "resume_completed": config.resume_completed,
        "include_optional": include_optional,
        "stages": stages,
    }


def _check(checks: list[dict[str, Any]], name: str, status: str, detail: str, **fields: Any) -> None:
    checks.append({"name": name, "status": status, "detail": detail, **fields})


def _selected_domains(config: FactoryConfig, include_optional: bool) -> set[str]:
    domains = set(config.pipeline.get("main_domains", []))
    if include_optional:
        domains.update(config.pipeline.get("optional_domains", []))
    return domains


def _credential_check(
    checks: list[dict[str, Any]],
    config: FactoryConfig,
    *,
    source: str,
    field: str,
    label: str,
    file_value: bool = False,
) -> None:
    env_name = config.source(source).get(field)
    if not env_name:
        _check(checks, label, "error", f"sources.{source}.{field} is not configured")
        return
    value = os.environ.get(str(env_name))
    if not value:
        _check(checks, label, "error", f"environment variable {env_name} is not set", env_name=str(env_name), present=False)
        return
    if file_value and not Path(value).expanduser().is_file():
        _check(
            checks,
            label,
            "error",
            f"environment variable {env_name} is set, but its referenced file does not exist",
            env_name=str(env_name),
            present=True,
        )
        return
    _check(checks, label, "ok", f"environment variable {env_name} is set", env_name=str(env_name), present=True)


def _air_pollution_estimate(config: FactoryConfig) -> dict[str, Any] | None:
    try:
        import geopandas as gpd
        import numpy as np
        from shapely.geometry import box
        from shapely.prepared import prep
    except ImportError:
        return None

    path = config.boundary_path("admin0")
    if not path.is_file():
        return None
    grid_degrees = float(config.source("openweathermap").get("grid_degrees", 0.045))
    if grid_degrees <= 0:
        raise ValueError("sources.openweathermap.grid_degrees must be positive")
    admin0 = gpd.read_file(path).to_crs("EPSG:4326")
    minx, miny, maxx, maxy = admin0.total_bounds
    country = prep(admin0.geometry.union_all())
    cells = 0
    for x in np.arange(minx, maxx, grid_degrees):
        for y in np.arange(miny, maxy, grid_degrees):
            if country.intersects(box(x, y, x + grid_degrees, y + grid_degrees)):
                cells += 1
    years = len(config.years("indicators"))
    calls = cells * years
    rpm = max(1, int(config.source("openweathermap").get("requests_per_minute", 60)))
    minutes = calls / rpm
    return {
        "grid_cells": cells,
        "years": years,
        "estimated_calls": calls,
        "requests_per_minute": rpm,
        "minimum_minutes": round(minutes, 1),
        "minimum_hours": round(minutes / 60.0, 2),
    }


def run_preflight(config: FactoryConfig, *, include_optional: bool = False) -> dict[str, Any]:
    checks: list[dict[str, Any]] = []
    domains = _selected_domains(config, include_optional)
    unknown_domains = sorted(domains - set(DOMAINS))
    _check(
        checks,
        "pipeline domains",
        "error" if unknown_domains else "ok",
        f"unknown domains: {', '.join(unknown_domains)}" if unknown_domains else f"{len(domains)} domain(s) selected",
    )

    for level in ("admin0", "admin1", "admin2"):
        path = config.boundary_path(level)
        _check(
            checks,
            f"boundary {level}",
            "ok" if path.is_file() else "error",
            f"file exists: {path}" if path.is_file() else f"file is missing: {path}",
        )
    try:
        admin2 = load_admin2(config)
        invalid = int((~admin2.geometry.is_valid).sum())
        empty = int(admin2.geometry.is_empty.sum())
        status = "warning" if invalid or empty else "ok"
        detail = f"{len(admin2)} unique admin-2 region(s); invalid={invalid}; empty={empty}"
        _check(
            checks,
            "admin-2 geometry contract",
            status,
            detail,
            rows=len(admin2),
            invalid_geometries=invalid,
            empty_geometries=empty,
        )
    except Exception as error:
        _check(
            checks,
            "admin-2 geometry contract",
            "error",
            f"could not load normalized admin-2 boundaries: {type(error).__name__}: {error}",
        )

    packages = {
        "geopandas",
        "numpy",
        "pandas",
        "pyarrow",
        "pyogrio",
        "rasterio",
        "rasterstats",
        "shapely",
    }
    if domains & {"flood", "land_cover", "luminosity"}:
        packages.update({"ee", "geemap"})
    if "heatwaves" in domains:
        packages.update({"dask", "netCDF4", "numba", "rioxarray", "xarray"})
    if "internet" in domains:
        packages.add("pyquadkey2")
    missing_packages = sorted(name for name in packages if importlib.util.find_spec(name) is None)
    _check(
        checks,
        "Python packages",
        "error" if missing_packages else "ok",
        f"missing: {', '.join(missing_packages)}" if missing_packages else f"all {len(packages)} required packages are importable",
        missing=missing_packages,
    )

    if domains & {"flood", "land_cover", "luminosity"}:
        _credential_check(
            checks,
            config,
            source="earth_engine",
            field="service_account_env",
            label="Earth Engine service account",
        )
        _credential_check(
            checks,
            config,
            source="earth_engine",
            field="key_file_env",
            label="Earth Engine key file",
            file_value=True,
        )
    if "air_pollution" in domains:
        _credential_check(
            checks,
            config,
            source="openweathermap",
            field="api_key_env",
            label="OpenWeatherMap API key",
        )
    if "accessibility" in domains:
        _credential_check(
            checks,
            config,
            source="mapbox",
            field="access_token_env",
            label="Mapbox access token",
        )

    if "heatwaves" in domains:
        pattern = str(config.source("heatwaves").get("source_glob", ""))
        matches = [Path(path) for path in glob.glob(pattern)] if pattern else []
        _check(
            checks,
            "Heatwave source files",
            "ok" if matches else "error",
            f"matched {len(matches)} file(s)" if matches else f"no files matched: {pattern or '<not configured>'}",
            matched_files=len(matches),
        )

    if "land_cover" in domains:
        existing = []
        invalid_rasters = []
        for year in config.years("land_cover"):
            path = config.raw_dir / "land_cover" / f"{config.iso3}_{year}.tif"
            if not path.is_file():
                continue
            existing.append(path)
            try:
                validate_categorical_raster(
                    path,
                    valid_classes=set(range(9)),
                    expected_nodata=255,
                )
            except ValueError as error:
                invalid_rasters.append(f"{year}: {error}")
        _check(
            checks,
            "Land Cover existing rasters",
            "warning" if invalid_rasters else "ok",
            (
                f"{len(invalid_rasters)} existing raster(s) are invalid and will be re-extracted"
                if invalid_rasters
                else f"{len(existing)} existing raster(s) validated; missing years will be extracted"
            ),
            existing_files=len(existing),
            invalid_files=len(invalid_rasters),
            invalid_details=invalid_rasters,
        )

    if "internet" in domains:
        source = config.source("internet")
        root = Path(str(source.get("dataset_root", ""))).expanduser()
        expected = []
        for year in config.years("indicators"):
            for field in ("fixed_filename_template", "mobile_filename_template"):
                template = source.get(field)
                if template:
                    expected.append(root / str(template).format(year=year))
        missing = [path for path in expected if not path.is_file()]
        _check(
            checks,
            "Ookla source files",
            "error" if missing else "ok",
            f"missing {len(missing)} of {len(expected)} expected file(s)" if missing else f"all {len(expected)} expected file(s) exist",
            expected_files=len(expected),
            missing_files=len(missing),
        )

    workspace_probe = config.workspace
    while not workspace_probe.exists() and workspace_probe != workspace_probe.parent:
        workspace_probe = workspace_probe.parent
    usage = shutil.disk_usage(workspace_probe)
    free_gb = usage.free / (1024**3)
    _check(
        checks,
        "free disk",
        "warning" if free_gb < 10 else "ok",
        f"{free_gb:.1f} GiB free on workspace volume",
        free_gib=round(free_gb, 2),
    )

    air_estimate = None
    if "air_pollution" in domains:
        try:
            air_estimate = _air_pollution_estimate(config)
        except Exception as error:
            _check(
                checks,
                "Air Pollution request estimate",
                "error",
                f"could not calculate estimate: {type(error).__name__}: {error}",
            )
    if air_estimate:
        _check(
            checks,
            "Air Pollution request estimate",
            "warning",
            (
                f"{air_estimate['estimated_calls']:,} calls at {air_estimate['requests_per_minute']}/min; "
                f"theoretical minimum {air_estimate['minimum_hours']:.2f} hours"
            ),
            **air_estimate,
        )

    errors = sum(check["status"] == "error" for check in checks)
    warnings = sum(check["status"] == "warning" for check in checks)
    return {
        "country": config.iso3,
        "status": "blocked" if errors else ("warning" if warnings else "ok"),
        "errors": errors,
        "warnings": warnings,
        "checks": checks,
        "air_pollution_estimate": air_estimate,
    }


def load_status(config: FactoryConfig, *, run_id: str | None = None) -> dict[str, Any]:
    run_root = config.workspace / "state" / "runs"
    if run_id:
        path = run_root / f"{run_id}.json"
    else:
        candidates = sorted(run_root.glob("*.json"), key=lambda item: item.stat().st_mtime_ns, reverse=True)
        if not candidates:
            raise FileNotFoundError(f"No pipeline run manifests exist under {run_root}")
        path = candidates[0]
    if not path.is_file():
        raise FileNotFoundError(f"Pipeline run manifest does not exist: {path}")
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as error:
        raise ValueError(f"Pipeline run manifest is invalid JSON: {path}") from error
    if not isinstance(payload, dict):
        raise ValueError(f"Pipeline run manifest must contain a JSON object: {path}")

    selected_run_id = str(payload.get("run_id", run_id or path.stem))
    tasks = dict(payload.get("tasks", {}))
    task_root = config.workspace / "state" / "tasks"
    for task_path in task_root.glob("*.json"):
        try:
            task_payload = json.loads(task_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if isinstance(task_payload, dict) and task_payload.get("run_id") == selected_run_id:
            tasks[str(task_payload.get("task_id", task_path.stem))] = task_payload

    status_counts: dict[str, int] = {}
    for task in tasks.values():
        status = str(task.get("status", "unknown"))
        status_counts[status] = status_counts.get(status, 0) + 1
    compact_tasks = {
        task_id: {
            key: task.get(key)
            for key in (
                "status",
                "attempt",
                "started_at",
                "updated_at",
                "finished_at",
                "elapsed_seconds",
                "artifact_count",
                "error_type",
                "error_message",
            )
            if task.get(key) is not None
        }
        for task_id, task in tasks.items()
    }
    requested = list(payload.get("requested_tasks", []))
    return {
        "manifest_path": str(path),
        "run_id": selected_run_id,
        "status": payload.get("status", "unknown"),
        "attempt": payload.get("attempt"),
        "started_at": payload.get("started_at"),
        "updated_at": payload.get("updated_at"),
        "finished_at": payload.get("finished_at"),
        "requested": len(requested),
        "recorded": len(tasks),
        "pending": max(0, len(requested) - len(tasks)),
        "status_counts": status_counts,
        "tasks": compact_tasks,
        "summary": payload.get("summary"),
    }
