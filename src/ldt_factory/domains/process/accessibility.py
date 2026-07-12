from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from ...context import RunContext
from ...geo import load_admin2
from ...io_utils import require_files
from ...logging_utils import logged_action
from .._api_cache import (
    accessibility_signature,
    build_accessibility_entries,
    read_json,
    resolve_assets_path,
)


def _atomic_csv(frame: Any, path: Path) -> None:
    temporary = path.with_name(f"{path.name}.tmp")
    frame.to_csv(temporary, index=False)
    temporary.replace(path)


def _population_sums(geometries: list[Any], population_path: Path) -> list[float]:
    from rasterstats import zonal_stats

    values = [0.0] * len(geometries)
    valid = [
        index
        for index, geometry in enumerate(geometries)
        if geometry is not None and not geometry.is_empty
    ]
    if not valid:
        return values
    stats = zonal_stats([geometries[index] for index in valid], population_path, stats=["sum"])
    for index, item in zip(valid, stats):
        values[index] = float(item.get("sum") or 0)
    return values


def run(ctx: RunContext, logger: logging.Logger) -> None:
    import geopandas as gpd
    import rasterio
    from shapely.geometry import shape
    from shapely.ops import unary_union

    assets_path = resolve_assets_path(ctx.config.shape_dir)
    manifest_path = ctx.raw("accessibility", "manifest.json")
    year = int(ctx.config.data["years"]["static_merge"])
    population_path = ctx.raw(
        "population",
        f"{ctx.config.iso3.lower()}_pop_{year}_CN_100m_R2025A_v1.tif",
    )
    require_files([assets_path, manifest_path, population_path], "accessibility inputs")

    source = ctx.config.source("mapbox")
    distance = int(source.get("walking_distance_meters", 10000))
    profile = str(source.get("profile", "walking"))
    assets = gpd.read_parquet(assets_path) if assets_path.suffix == ".parquet" else gpd.read_file(assets_path)
    assets = assets.to_crs("EPSG:4326")
    expected_entries = build_accessibility_entries(
        assets,
        distance_meters=distance,
        profile=profile,
    )
    expected_signature = accessibility_signature(
        expected_entries,
        distance_meters=distance,
        profile=profile,
    )
    manifest = read_json(manifest_path)
    if (
        manifest.get("schema_version") != 1
        or manifest.get("status") != "complete"
        or manifest.get("assets_signature") != expected_signature
        or manifest.get("entries") != expected_entries
    ):
        raise RuntimeError(
            "Accessibility cache is incomplete or stale; run the accessibility extraction phase first"
        )

    admin2 = load_admin2(ctx.config)
    admin2 = admin2.to_crs("EPSG:4326")

    with logged_action(logger, "process", domain="accessibility"):
        polygons: dict[str, list[Any]] = {"school": [], "hospital": []}
        for entry in expected_entries:
            cache_path = ctx.raw("accessibility", "isochrones", f"{entry['cache_key']}.json")
            if not cache_path.is_file():
                raise FileNotFoundError(
                    f"Accessibility cache record is missing: {cache_path}; rerun extraction to resume"
                )
            payload = read_json(cache_path)
            if (
                payload.get("schema_version") != 1
                or payload.get("status") != "complete"
                or payload.get("cache_key") != entry["cache_key"]
                or payload.get("category") != entry["category"]
            ):
                raise RuntimeError(f"Accessibility cache record is invalid or stale: {cache_path}")
            geometry = payload.get("geometry")
            if geometry is not None:
                polygons[entry["category"]].append(shape(geometry))

        with rasterio.open(population_path) as population:
            raster_crs = population.crs
        if raster_crs is None:
            raise ValueError(f"Population raster has no CRS: {population_path}")
        admin_raster = admin2.to_crs(raster_crs)
        output = admin2[[ctx.config.admin1, ctx.config.admin2]].copy()
        totals = _population_sums(list(admin_raster.geometry), population_path)

        for category in ("school", "hospital"):
            union = unary_union(polygons[category]) if polygons[category] else None
            if union is None or union.is_empty:
                accessible = [0.0] * len(admin2)
            else:
                union_raster = gpd.GeoSeries([union], crs="EPSG:4326").to_crs(raster_crs).iloc[0]
                clipped = [geometry.intersection(union_raster) for geometry in admin_raster.geometry]
                accessible = _population_sums(clipped, population_path)
            output[f"{category}_accessibility"] = [
                100.0 * value / total if total else 0.0
                for value, total in zip(accessible, totals)
            ]

        output["year"] = year
        output_path = ctx.output(f"{ctx.config.iso3}_accessibility.csv")
        _atomic_csv(output, output_path)
        logger.info(
            "accessibility rows=%d cached_isochrones=%d",
            len(output),
            len(expected_entries),
            extra={"domain": "accessibility", "path": str(output_path)},
        )
