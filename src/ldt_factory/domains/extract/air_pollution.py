from __future__ import annotations

import logging
import math
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import requests

from ...context import RunContext
from ...geo import load_admin0
from ...logging_utils import logged_action
from .._api_cache import RatePacer, atomic_write_json, read_json, request_json_with_retry


POLLUTANTS = {"pm25": "pm2_5", "pm10": "pm10", "no2": "no2"}


def summarize_payload(
    payload: dict[str, Any],
    *,
    grid_id: int,
    requested_year: int,
    lon: float,
    lat: float,
) -> dict[str, Any]:
    """Reduce an hourly OpenWeather response to per-cell annual sufficient statistics."""
    annual: dict[int, dict[str, Any]] = {}
    for item in payload.get("list", []):
        try:
            year = datetime.fromtimestamp(int(item["dt"]), tz=timezone.utc).year
        except (KeyError, TypeError, ValueError, OSError):
            continue
        record = annual.setdefault(
            year,
            {
                "year": year,
                "observation_count": 0,
                "pollutants": {
                    name: {"sum": 0.0, "count": 0} for name in POLLUTANTS
                },
            },
        )
        record["observation_count"] += 1
        components = item.get("components") or {}
        for output_name, source_name in POLLUTANTS.items():
            value = components.get(source_name)
            try:
                numeric = float(value)
            except (TypeError, ValueError):
                continue
            if not math.isfinite(numeric):
                continue
            stats = record["pollutants"][output_name]
            stats["sum"] += numeric
            stats["count"] += 1

    rows = []
    for year in sorted(annual):
        record = annual[year]
        for stats in record["pollutants"].values():
            stats["mean"] = stats["sum"] / stats["count"] if stats["count"] else None
        rows.append(record)
    return {
        "schema_version": 1,
        "status": "complete",
        "grid_id": int(grid_id),
        "requested_year": int(requested_year),
        "lon": float(lon),
        "lat": float(lat),
        "annual": rows,
    }


def _valid_summary(path: Path, *, grid_id: int, year: int) -> bool:
    try:
        payload = read_json(path)
    except (OSError, ValueError):
        return False
    return (
        payload.get("schema_version") == 1
        and payload.get("status") == "complete"
        and payload.get("grid_id") == grid_id
        and payload.get("requested_year") == year
        and isinstance(payload.get("annual"), list)
    )


def _write_grid(grid: Any, path: Path) -> None:
    temporary = path.with_name(f"{path.stem}.tmp{path.suffix}")
    if temporary.exists():
        temporary.unlink()
    grid.to_file(temporary, driver="GeoJSON", index=False)
    temporary.replace(path)


def _write_manifest(
    path: Path,
    *,
    year: int,
    expected: int,
    completed: int,
) -> None:
    atomic_write_json(
        path,
        {
            "schema_version": 1,
            "year": year,
            "status": "complete" if completed == expected else "in_progress",
            "expected_cells": expected,
            "completed_cells": completed,
            "pending_cells": expected - completed,
        },
    )


def run(ctx: RunContext, logger: logging.Logger) -> None:
    import geopandas as gpd
    import numpy as np
    from shapely.geometry import box
    from shapely.prepared import prep

    admin0 = load_admin0(ctx.config)
    admin0 = admin0.to_crs("EPSG:4326")
    source = ctx.config.source("openweathermap")
    api_key_env = source.get("api_key_env")
    if not api_key_env:
        raise ValueError("sources.openweathermap.api_key_env is required")
    grid_size = float(source.get("grid_degrees", 0.045))
    if grid_size <= 0:
        raise ValueError("sources.openweathermap.grid_degrees must be positive")
    rpm = max(1, int(source.get("requests_per_minute", 60)))
    max_retries = max(0, int(source.get("max_retries", 4)))
    backoff_seconds = max(0.0, float(source.get("retry_backoff_seconds", 2.0)))
    manifest_interval = max(1, int(source.get("manifest_update_interval", 25)))
    trust_env = bool(ctx.config.data.get("network", {}).get("use_environment_proxy", True))

    minx, miny, maxx, maxy = admin0.total_bounds
    country = prep(admin0.geometry.union_all())
    cells = []
    for x in np.arange(minx, maxx, grid_size):
        for y in np.arange(miny, maxy, grid_size):
            cell = box(x, y, x + grid_size, y + grid_size)
            if country.intersects(cell):
                cells.append(cell)
    grid = gpd.GeoDataFrame({"grid_id": range(len(cells))}, geometry=cells, crs="EPSG:4326")
    grid_path = ctx.raw("air_pollution", "grid.geojson")
    with logged_action(logger, "write_grid", domain="air_pollution", path=str(grid_path)):
        _write_grid(grid, grid_path)
        logger.info(
            "air-pollution grid cells=%d grid_degrees=%s",
            len(grid),
            grid_size,
            extra={"action": "write_grid", "domain": "air_pollution", "path": str(grid_path)},
        )

    session = requests.Session()
    session.trust_env = trust_env
    session.headers.update({"User-Agent": "wb-ldt-datacube-factory/0.1"})
    pacer = RatePacer(rpm)
    api_key: str | None = None
    try:
        for year in ctx.config.years("indicators"):
            year_dir = ctx.raw("air_pollution", "annual", str(year))
            manifest_path = year_dir / "manifest.json"
            completed = 0
            _write_manifest(manifest_path, year=year, expected=len(grid), completed=completed)
            try:
                start = int(datetime(year, 1, 1, tzinfo=timezone.utc).timestamp())
                end = int(datetime(year + 1, 1, 1, tzinfo=timezone.utc).timestamp()) - 1
                for row in grid.itertuples():
                    grid_id = int(row.grid_id)
                    point = row.geometry.centroid
                    output = year_dir / f"{grid_id}.json"
                    if _valid_summary(output, grid_id=grid_id, year=year):
                        completed += 1
                    else:
                        legacy = ctx.raw("air_pollution", f"{grid_id}_{year}.json")
                        migrated = False
                        if legacy.is_file():
                            try:
                                legacy_payload = read_json(legacy)
                                if isinstance(legacy_payload.get("list"), list):
                                    atomic_write_json(
                                        output,
                                        summarize_payload(
                                            legacy_payload,
                                            grid_id=grid_id,
                                            requested_year=year,
                                            lon=point.x,
                                            lat=point.y,
                                        ),
                                    )
                                    migrated = True
                                    logger.info(
                                        "migrated legacy response to annual cache",
                                        extra={
                                            "action": "migrate_cache",
                                            "domain": "air_pollution",
                                            "phase": f"{year}:{grid_id}",
                                            "path": str(output),
                                        },
                                    )
                            except (OSError, ValueError):
                                migrated = False

                        if not migrated:
                            if api_key is None:
                                api_key = ctx.require_env(str(api_key_env))
                            with logged_action(
                                logger,
                                "request",
                                domain="air_pollution",
                                phase=f"{year}:{grid_id}",
                                path=str(output),
                            ):
                                payload = request_json_with_retry(
                                    session,
                                    "https://api.openweathermap.org/data/2.5/air_pollution/history",
                                    params={
                                        "lat": point.y,
                                        "lon": point.x,
                                        "start": start,
                                        "end": end,
                                        "appid": api_key,
                                    },
                                    pacer=pacer,
                                    timeout=(15, 120),
                                    max_retries=max_retries,
                                    backoff_seconds=backoff_seconds,
                                    logger=logger,
                                    domain="air_pollution",
                                )
                                atomic_write_json(
                                    output,
                                    summarize_payload(
                                        payload,
                                        grid_id=grid_id,
                                        requested_year=year,
                                        lon=point.x,
                                        lat=point.y,
                                    ),
                                )
                        completed += 1

                    if completed % manifest_interval == 0:
                        _write_manifest(
                            manifest_path,
                            year=year,
                            expected=len(grid),
                            completed=completed,
                        )
                        logger.info(
                            "air-pollution extraction progress year=%d completed=%d total=%d",
                            year,
                            completed,
                            len(grid),
                            extra={"action": "progress", "domain": "air_pollution", "phase": str(year)},
                        )
            finally:
                _write_manifest(
                    manifest_path,
                    year=year,
                    expected=len(grid),
                    completed=completed,
                )
    finally:
        session.close()
