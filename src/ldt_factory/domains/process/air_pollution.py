from __future__ import annotations

import hashlib
import logging
import math
import re
from collections import defaultdict
from pathlib import Path
from typing import Any, Iterable

from ...context import RunContext
from ...geo import load_admin2
from ...logging_utils import logged_action
from .._api_cache import atomic_write_json, read_json


POLLUTANTS = {"pm25": "pm2_5", "pm10": "pm10", "no2": "no2"}
LEGACY_NAME = re.compile(r"^(\d+)_(\d{4})\.json$")


def _atomic_csv(frame: Any, path: Path) -> None:
    temporary = path.with_name(f"{path.name}.tmp")
    frame.to_csv(temporary, index=False)
    temporary.replace(path)


def _number(value: Any) -> float | None:
    try:
        numeric = float(value)
    except (TypeError, ValueError):
        return None
    return numeric if math.isfinite(numeric) else None


def _consume_compact(
    payload: dict[str, Any],
    aggregates: dict[tuple[int, int], dict[str, list[float | int]]],
) -> tuple[int, int]:
    if payload.get("schema_version") != 1 or payload.get("status") != "complete":
        raise ValueError("Incomplete or unsupported air-pollution cache record")
    grid_id = int(payload["grid_id"])
    requested_year = int(payload["requested_year"])
    for annual in payload.get("annual", []):
        year = int(annual["year"])
        target = aggregates[(grid_id, year)]
        pollutant_stats = annual.get("pollutants") or {}
        for name in POLLUTANTS:
            stats = pollutant_stats.get(name) or {}
            value_sum = _number(stats.get("sum"))
            try:
                count = int(stats.get("count", 0))
            except (TypeError, ValueError):
                count = 0
            if value_sum is not None and count > 0:
                target[name][0] += value_sum
                target[name][1] += count
    return grid_id, requested_year


def _consume_legacy(
    payload: dict[str, Any],
    aggregates: dict[tuple[int, int], dict[str, list[float | int]]],
) -> int:
    from datetime import datetime, timezone

    grid_id = int(payload["factory_grid_id"])
    observations = 0
    for item in payload.get("list", []):
        try:
            year = datetime.fromtimestamp(int(item["dt"]), tz=timezone.utc).year
        except (KeyError, TypeError, ValueError, OSError):
            continue
        observations += 1
        target = aggregates[(grid_id, year)]
        components = item.get("components") or {}
        for output_name, source_name in POLLUTANTS.items():
            numeric = _number(components.get(source_name))
            if numeric is not None:
                target[output_name][0] += numeric
                target[output_name][1] += 1
    return observations


def _new_aggregates() -> dict[tuple[int, int], dict[str, list[float | int]]]:
    return defaultdict(lambda: {name: [0.0, 0] for name in POLLUTANTS})


def _mapping_signature(grid: Any, admin2: Any, admin1_name: str, admin2_name: str) -> str:
    digest = hashlib.sha256()
    digest.update(str(grid.crs).encode("utf-8"))
    for row in grid[["grid_id", "geometry"]].sort_values("grid_id").itertuples(index=False):
        digest.update(str(int(row.grid_id)).encode("ascii"))
        digest.update(row.geometry.wkb)
    digest.update(str(admin2.crs).encode("utf-8"))
    ordered = admin2[[admin1_name, admin2_name, "geometry"]].sort_values(
        [admin1_name, admin2_name], kind="stable"
    )
    for row in ordered.itertuples(index=False, name=None):
        digest.update(str(row[0]).encode("utf-8"))
        digest.update(str(row[1]).encode("utf-8"))
        digest.update(row[2].wkb)
    return digest.hexdigest()


def _load_or_build_mapping(ctx: RunContext, grid: Any, admin2: Any, logger: logging.Logger) -> Any:
    import geopandas as gpd
    import pandas as pd

    admin2 = admin2.to_crs(grid.crs)
    signature = _mapping_signature(grid, admin2, ctx.config.admin1, ctx.config.admin2)
    cache = ctx.raw("air_pollution", "grid_admin2.csv")
    metadata = ctx.raw("air_pollution", "grid_admin2.meta.json")
    if cache.is_file() and metadata.is_file():
        try:
            meta = read_json(metadata)
            if meta.get("signature") == signature:
                mapping = pd.read_csv(cache)
                required = {"grid_id", ctx.config.admin1, ctx.config.admin2}
                if required.issubset(mapping.columns):
                    mapping["grid_id"] = mapping["grid_id"].astype(int)
                    logger.info(
                        "reused air-pollution grid/admin cache rows=%d",
                        len(mapping),
                        extra={"action": "load_mapping_cache", "domain": "air_pollution", "path": str(cache)},
                    )
                    return mapping[["grid_id", ctx.config.admin1, ctx.config.admin2]]
        except (OSError, ValueError, TypeError):
            pass

    with logged_action(logger, "build_mapping_cache", domain="air_pollution", path=str(cache)):
        joined = gpd.sjoin(
            grid[["grid_id", "geometry"]],
            admin2[[ctx.config.admin1, ctx.config.admin2, "geometry"]],
            predicate="intersects",
            how="inner",
        )
        mapping = joined[["grid_id", ctx.config.admin1, ctx.config.admin2]].copy()
        mapping["grid_id"] = mapping["grid_id"].astype(int)
        _atomic_csv(mapping, cache)
        atomic_write_json(
            metadata,
            {"schema_version": 1, "signature": signature, "rows": len(mapping)},
        )
        return mapping


def _compact_paths(root: Path) -> Iterable[Path]:
    annual = root / "annual"
    if not annual.is_dir():
        return []
    return sorted(path for path in annual.glob("*/*.json") if path.name != "manifest.json")


def run(ctx: RunContext, logger: logging.Logger) -> None:
    import geopandas as gpd
    import pandas as pd

    admin2 = load_admin2(ctx.config)
    grid_path = ctx.raw("air_pollution", "grid.geojson")
    if not grid_path.is_file():
        raise FileNotFoundError(f"Air-pollution grid is missing: {grid_path}")
    grid = gpd.read_file(grid_path)
    root = ctx.raw("air_pollution")

    with logged_action(logger, "process", domain="air_pollution"):
        aggregates = _new_aggregates()
        compact_request_keys: set[tuple[int, int]] = set()
        compact_files = 0
        for path in _compact_paths(root):
            payload = read_json(path)
            compact_request_keys.add(_consume_compact(payload, aggregates))
            compact_files += 1

        legacy_files = 0
        legacy_observations = 0
        for path in sorted(root.glob("*.json")):
            match = LEGACY_NAME.match(path.name)
            if not match:
                continue
            request_key = (int(match.group(1)), int(match.group(2)))
            if request_key in compact_request_keys:
                continue
            payload = read_json(path)
            legacy_observations += _consume_legacy(payload, aggregates)
            legacy_files += 1

        if not aggregates:
            raise ValueError("No valid OpenWeatherMap observations were found")

        rows = []
        for (grid_id, year), stats_by_pollutant in sorted(aggregates.items()):
            row: dict[str, Any] = {"grid_id": grid_id, "year": year}
            for name, (value_sum, count) in stats_by_pollutant.items():
                row[f"{name}_sum"] = value_sum
                row[f"{name}_count"] = count
            rows.append(row)
        annual = pd.DataFrame(rows)
        mapping = _load_or_build_mapping(ctx, grid, admin2, logger)
        joined = annual.merge(mapping, on="grid_id", how="inner")
        keys = [ctx.config.admin1, ctx.config.admin2, "year"]
        sufficient_columns = [
            f"{name}_{stat}"
            for name in POLLUTANTS
            for stat in ("sum", "count")
        ]
        output = joined.groupby(keys, as_index=False)[sufficient_columns].sum()
        for name in POLLUTANTS:
            output[name] = output[f"{name}_sum"].div(
                output[f"{name}_count"].where(output[f"{name}_count"] > 0)
            )
        output = output[[*keys, *POLLUTANTS]]
        output_path = ctx.output(f"{ctx.config.iso3}_air_pollution.csv")
        _atomic_csv(output, output_path)
        logger.info(
            "air-pollution rows=%d compact_files=%d legacy_files=%d legacy_hourly_observations=%d",
            len(output),
            compact_files,
            legacy_files,
            legacy_observations,
            extra={"domain": "air_pollution", "path": str(output_path)},
        )
