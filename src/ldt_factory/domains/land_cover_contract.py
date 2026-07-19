from __future__ import annotations

import re
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from ..config import FactoryConfig


RASTER_BACKEND = "raster_download"
GEE_REDUCE_REGIONS_BACKEND = "gee_reduce_regions"
LAND_COVER_BACKENDS = {RASTER_BACKEND, GEE_REDUCE_REGIONS_BACKEND}
NODATA_CLASS = 255
ADMIN_AREA_COLUMN = "admin_area_km2"
DYNAMIC_WORLD_CLASSES = {
    0: "water",
    1: "tree",
    2: "grass",
    3: "flood_vegetation",
    4: "crops",
    5: "shrub_and_scrub",
    6: "built",
    7: "bare",
    8: "snow_and_ice",
}
GEE_ALGORITHM_VERSION = "dynamic-world-reduce-regions-v1"
DEFAULT_INTERMEDIATE_ASSET_PREFIX = "ldt_factory_land_cover"


def land_cover_backend(config: FactoryConfig) -> str:
    return str(config.source("land_cover").get("backend", RASTER_BACKEND)).strip().lower()


def raster_path(config: FactoryConfig, year: int) -> Path:
    return config.raw_dir / "land_cover" / f"{config.iso3}_{year}.tif"


def count_table_path(config: FactoryConfig, year: int) -> Path:
    return config.raw_dir / "land_cover" / f"{config.iso3}_{year}_counts.csv"


def count_manifest_path(config: FactoryConfig, year: int) -> Path:
    return count_table_path(config, year).with_suffix(".manifest.json")


def gee_task_path(config: FactoryConfig, year: int) -> Path:
    return config.workspace / "state" / "land_cover" / f"gee_export_{year}.json"


def extraction_artifacts(config: FactoryConfig) -> list[Path]:
    years = config.years("land_cover")
    if land_cover_backend(config) == GEE_REDUCE_REGIONS_BACKEND:
        return [
            path
            for year in years
            for path in (count_table_path(config, year), count_manifest_path(config, year))
        ]
    return [raster_path(config, year) for year in years]


def intermediate_asset_prefix(config: FactoryConfig) -> str:
    value = str(
        config.source("land_cover").get(
            "intermediate_asset_prefix", DEFAULT_INTERMEDIATE_ASSET_PREFIX
        )
    ).strip()
    if not re.fullmatch(r"[A-Za-z][A-Za-z0-9_-]*", value):
        raise ValueError(
            "sources.land_cover.intermediate_asset_prefix must start with a letter "
            "and contain only letters, numbers, underscores, or hyphens"
        )
    return value


def normalize_class_counts(
    frame,
    *,
    admin1: str,
    admin2: str,
    expected_year: int,
):
    """Validate and normalize one annual Dynamic World class-count table."""
    import numpy as np
    import pandas as pd

    class_columns = list(DYNAMIC_WORLD_CLASSES.values())
    required = [admin1, admin2, "year", *class_columns]
    missing = [column for column in required if column not in frame.columns]
    if missing:
        raise ValueError(f"Land-cover class counts are missing columns: {missing}")

    output = frame[required].copy()
    for column in (admin1, admin2):
        if output[column].isna().any():
            raise ValueError(f"Land-cover class counts contain missing {column!r} values")
        output[column] = output[column].astype(str)
        if output[column].str.strip().eq("").any():
            raise ValueError(f"Land-cover class counts contain blank {column!r} values")

    years = pd.to_numeric(output["year"], errors="coerce")
    if years.isna().any() or not years.eq(expected_year).all():
        observed = sorted(set(output.loc[years.notna(), "year"].astype(str)))
        raise ValueError(
            f"Land-cover class counts for {expected_year} contain unexpected years: {observed}"
        )
    output["year"] = int(expected_year)

    for column in class_columns:
        original = output[column]
        values = pd.to_numeric(original, errors="coerce")
        invalid = values.isna() & original.notna()
        if invalid.any():
            raise ValueError(f"Land-cover class count {column!r} contains non-numeric values")
        values = values.fillna(0.0).astype(float)
        if (values < 0).any():
            raise ValueError(f"Land-cover class count {column!r} contains negative values")
        if not np.allclose(values, np.round(values), atol=1e-6, rtol=0):
            raise ValueError(f"Land-cover class count {column!r} contains fractional pixels")
        output[column] = np.round(values).astype(float)

    if output.duplicated([admin1, admin2]).any():
        raise ValueError("Land-cover class counts contain duplicate administrative keys")
    return output


def validate_admin_keys(frame, admin2_frame, *, admin1: str, admin2: str) -> None:
    expected_frame = admin2_frame[[admin1, admin2]].copy()
    for column in (admin1, admin2):
        expected_frame[column] = expected_frame[column].astype(str)
    expected = set(expected_frame.itertuples(index=False, name=None))
    observed = set(frame[[admin1, admin2]].itertuples(index=False, name=None))
    missing = expected - observed
    extra = observed - expected
    if missing or extra:
        raise ValueError(
            "Land-cover class-count keys do not match the configured admin-2 boundary "
            f"(missing={len(missing)}, extra={len(extra)})"
        )
