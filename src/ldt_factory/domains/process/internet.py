from __future__ import annotations

import hashlib
import json
import logging
import math
from pathlib import Path
from typing import Any, Callable

from ...checkpoint_utils import write_frame_csv_atomic
from ...context import RunContext
from ...geo import load_admin2
from ...io_utils import require_files
from ...logging_utils import logged_action


_ALGORITHM_VERSION = 2


def _file_signatures(path: Path) -> list[dict[str, Any]]:
    paths = [path]
    if path.suffix.lower() == ".shp":
        paths = sorted(
            candidate
            for candidate in path.parent.glob(f"{path.stem}.*")
            if candidate.is_file()
        )
    signatures = []
    for candidate in paths:
        stat = candidate.stat()
        signatures.append(
            {
                "path": str(candidate.resolve()),
                "size": stat.st_size,
                "mtime_ns": stat.st_mtime_ns,
            }
        )
    return signatures


def _cache_fingerprint(*, paths: list[Path], parameters: dict[str, Any]) -> str:
    payload = {
        "algorithm_version": _ALGORITHM_VERSION,
        "inputs": [signature for path in paths for signature in _file_signatures(path)],
        "parameters": parameters,
    }
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _write_parquet_atomic(frame: Any, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f"{path.stem}.tmp{path.suffix}")
    frame.to_parquet(temporary, index=False)
    temporary.replace(path)


def _tile_x(longitude: float, zoom: int) -> int:
    tile_count = 1 << zoom
    value = math.floor((max(-180.0, min(180.0, longitude)) + 180.0) / 360.0 * tile_count)
    return max(0, min(tile_count - 1, int(value)))


def _tile_y(latitude: float, zoom: int) -> int:
    tile_count = 1 << zoom
    latitude = max(-85.05112878, min(85.05112878, latitude))
    radians = math.radians(latitude)
    value = math.floor(
        (1.0 - math.asinh(math.tan(radians)) / math.pi) / 2.0 * tile_count
    )
    return max(0, min(tile_count - 1, int(value)))


def _quadkey_from_tile(x: int, y: int, zoom: int) -> str:
    digits = []
    for level in range(zoom, 0, -1):
        mask = 1 << (level - 1)
        digit = 0
        if x & mask:
            digit += 1
        if y & mask:
            digit += 2
        digits.append(str(digit))
    return "".join(digits)


def _quadkey_prefixes_for_bounds(
    bounds: tuple[float, float, float, float] | list[float], zoom: int
) -> list[str]:
    if zoom <= 0:
        raise ValueError("Internet quadkey prefix zoom must be greater than zero")
    west, south, east, north = (float(value) for value in bounds)
    x_min = _tile_x(west, zoom)
    x_max = _tile_x(east, zoom)
    y_min = _tile_y(north, zoom)
    y_max = _tile_y(south, zoom)
    return sorted(
        _quadkey_from_tile(x, y, zoom)
        for x in range(x_min, x_max + 1)
        for y in range(y_min, y_max + 1)
    )


def _prefix_upper_bound(prefix: str) -> str | None:
    digits = list(prefix)
    for index in range(len(digits) - 1, -1, -1):
        if digits[index] < "3":
            digits[index] = str(int(digits[index]) + 1)
            return "".join(digits[: index + 1])
    return None


def _prefix_filters(prefixes: list[str]) -> list[list[tuple[str, str, str]]]:
    filters = []
    for prefix in prefixes:
        conditions: list[tuple[str, str, str]] = [("quadkey", ">=", prefix)]
        upper = _prefix_upper_bound(prefix)
        if upper is not None:
            conditions.append(("quadkey", "<", upper))
        filters.append(conditions)
    return filters


def _read_fixed_candidates(path: Path, quadkeys: list[str]) -> Any:
    import pandas as pd
    import pyarrow.parquet as pq

    if not quadkeys:
        return pd.DataFrame(columns=["quadkey", "avg_d_kbps"])
    table = pq.read_table(
        path,
        columns=["quadkey", "avg_d_kbps"],
        filters=[("quadkey", "in", quadkeys)],
        use_threads=True,
    )
    frame = table.to_pandas()
    frame["quadkey"] = frame["quadkey"].astype(str)
    return frame


def _read_mobile_candidates(path: Path, prefixes: list[str]) -> Any:
    import pandas as pd
    import pyarrow.parquet as pq

    if not prefixes:
        return pd.DataFrame(columns=["quadkey", "avg_d_kbps", "tile"])
    schema = pq.read_schema(path)
    if "quadkey" not in schema.names or "avg_d_kbps" not in schema.names:
        raise ValueError(f"Mobile Ookla file lacks quadkey or avg_d_kbps: {path}")
    geometry_column = "tile" if "tile" in schema.names else "geometry"
    if geometry_column not in schema.names:
        raise ValueError(f"Mobile Ookla file lacks tile geometry: {path}")

    table = pq.read_table(
        path,
        columns=["quadkey", "avg_d_kbps", geometry_column],
        filters=_prefix_filters(prefixes),
        use_threads=True,
    )
    frame = table.to_pandas()
    frame["quadkey"] = frame["quadkey"].astype(str)
    exact = frame["quadkey"].str.startswith(tuple(prefixes), na=False)
    return frame.loc[exact].rename(columns={geometry_column: "tile"})


def _load_or_build_cache(
    *,
    path: Path,
    expected_columns: list[str],
    build: Callable[[], Any],
    logger: logging.Logger,
    phase: str,
) -> Any:
    import pandas as pd

    if path.is_file():
        cached = pd.read_parquet(path)
        missing = [column for column in expected_columns if column not in cached.columns]
        if not missing:
            logger.info(
                "%s cache reused rows=%d",
                phase,
                len(cached),
                extra={
                    "action": "reuse_cache",
                    "domain": "internet",
                    "phase": phase,
                    "path": str(path),
                },
            )
            return cached
        logger.warning(
            "%s cache ignored missing_columns=%s",
            phase,
            missing,
            extra={
                "action": "ignore_cache",
                "domain": "internet",
                "phase": phase,
                "path": str(path),
            },
        )
    frame = build()
    _write_parquet_atomic(frame, path)
    logger.info(
        "%s cache written rows=%d",
        phase,
        len(frame),
        extra={
            "action": "write_cache",
            "domain": "internet",
            "phase": phase,
            "path": str(path),
        },
    )
    return frame


def run(ctx: RunContext, logger: logging.Logger) -> None:
    import geopandas as gpd
    import pandas as pd
    import shapely
    from pyquadkey2 import quadkey as qk

    assets_parquet = ctx.config.shape_dir / "assets.parquet"
    assets_geojson = ctx.config.shape_dir / "assets.geojson"
    assets_path = assets_parquet if assets_parquet.is_file() else assets_geojson
    require_files([assets_path], "key-assets output")
    if assets_path.suffix.lower() == ".parquet":
        assets = gpd.read_parquet(assets_path, columns=["lon", "lat", "geometry"])
    else:
        assets = gpd.read_file(assets_path, columns=["lon", "lat"]).to_crs("EPSG:4326")
    assets = assets.to_crs("EPSG:4326")

    admin2 = load_admin2(ctx.config)
    admin2 = admin2.to_crs("EPSG:4326")
    source = ctx.config.source("internet")
    settings = ctx.config.data.get("processing", {}).get("internet", {})
    prefix_zoom = int(settings.get("mobile_prefix_zoom", 8))
    if prefix_zoom > 16:
        raise ValueError("processing.internet.mobile_prefix_zoom cannot exceed 16")
    dataset_root = Path(str(source["dataset_root"]))
    state_dir = ctx.config.workspace / "state" / "internet"
    state_dir.mkdir(parents=True, exist_ok=True)
    fixed_rows = []
    mobile_rows = []

    with logged_action(logger, "process", domain="internet"):
        asset_points = assets.copy()
        asset_points["geometry"] = gpd.points_from_xy(asset_points["lon"], asset_points["lat"])
        asset_points["quadkey"] = [
            str(qk.from_geo((geom.y, geom.x), 16)) for geom in asset_points.geometry
        ]
        asset_points = gpd.sjoin(asset_points, admin2, predicate="within", how="left")
        asset_quadkeys = asset_points["quadkey"].dropna().astype(str).unique().tolist()
        mobile_prefixes = _quadkey_prefixes_for_bounds(admin2.total_bounds, prefix_zoom)
        logger.info(
            "internet prepared assets=%d unique_quadkeys=%d mobile_prefixes=%d zoom=%d",
            len(asset_points),
            len(asset_quadkeys),
            len(mobile_prefixes),
            prefix_zoom,
            extra={"action": "prepare_filters", "domain": "internet"},
        )

        for year in ctx.config.years("indicators"):
            fixed_path = dataset_root / str(source["fixed_filename_template"]).format(year=year)
            mobile_path = dataset_root / str(source["mobile_filename_template"]).format(year=year)
            require_files([fixed_path, mobile_path], "Ookla parquet inputs")

            fixed_fingerprint = _cache_fingerprint(
                paths=[assets_path, ctx.config.boundary_path("admin2"), fixed_path],
                parameters={
                    "kind": "fixed",
                    "year": year,
                    "admin1": ctx.config.admin1,
                    "admin2": ctx.config.admin2,
                },
            )
            fixed_cache = state_dir / f"{ctx.config.iso3}-{year}-fixed-{fixed_fingerprint[:16]}.parquet"

            def build_fixed() -> Any:
                with logged_action(logger, "filter_fixed", domain="internet", phase=str(year)):
                    fixed = _read_fixed_candidates(fixed_path, asset_quadkeys)
                    logger.info(
                        "fixed year=%d candidate_rows=%d",
                        year,
                        len(fixed),
                        extra={"action": "filter_fixed", "domain": "internet", "phase": str(year)},
                    )
                    joined = asset_points.merge(
                        fixed.drop_duplicates("quadkey", keep="last"),
                        on="quadkey",
                        how="left",
                    )
                    grouped = joined.groupby(
                        [ctx.config.admin1, ctx.config.admin2], as_index=False
                    ).agg(
                        avg_d_kbps=("avg_d_kbps", "mean"),
                        key_structures=("quadkey", "size"),
                        key_structures_without_internet=(
                            "avg_d_kbps",
                            lambda values: 100.0 * values.isna().mean(),
                        ),
                    )
                    grouped["year"] = year
                    grouped["avg_d_mbps_broadband"] = grouped["avg_d_kbps"] / 1000.0
                    return grouped.drop(columns=["avg_d_kbps"])

            fixed_grouped = _load_or_build_cache(
                path=fixed_cache,
                expected_columns=[
                    ctx.config.admin1,
                    ctx.config.admin2,
                    "year",
                    "avg_d_mbps_broadband",
                    "key_structures",
                    "key_structures_without_internet",
                ],
                build=build_fixed,
                logger=logger,
                phase=f"fixed-{year}",
            )
            fixed_rows.append(fixed_grouped)

            mobile_fingerprint = _cache_fingerprint(
                paths=[ctx.config.boundary_path("admin2"), mobile_path],
                parameters={
                    "kind": "mobile",
                    "year": year,
                    "admin1": ctx.config.admin1,
                    "admin2": ctx.config.admin2,
                    "prefix_zoom": prefix_zoom,
                    "prefixes": mobile_prefixes,
                },
            )
            mobile_cache = state_dir / f"{ctx.config.iso3}-{year}-mobile-{mobile_fingerprint[:16]}.parquet"

            def build_mobile() -> Any:
                with logged_action(logger, "filter_mobile", domain="internet", phase=str(year)):
                    mobile = _read_mobile_candidates(mobile_path, mobile_prefixes)
                    logger.info(
                        "mobile year=%d candidate_rows=%d",
                        year,
                        len(mobile),
                        extra={"action": "filter_mobile", "domain": "internet", "phase": str(year)},
                    )
                    mobile = gpd.GeoDataFrame(
                        mobile,
                        geometry=shapely.from_wkt(mobile["tile"]),
                        crs="EPSG:4326",
                    )
                    spatial = gpd.sjoin(mobile, admin2, predicate="intersects", how="inner")
                    grouped = spatial.groupby(
                        [ctx.config.admin1, ctx.config.admin2], as_index=False
                    )["avg_d_kbps"].mean()
                    grouped["avg_d_mbps_mobile"] = grouped["avg_d_kbps"] / 1000.0
                    grouped["year"] = year
                    return grouped.drop(columns=["avg_d_kbps"])

            mobile_grouped = _load_or_build_cache(
                path=mobile_cache,
                expected_columns=[
                    ctx.config.admin1,
                    ctx.config.admin2,
                    "year",
                    "avg_d_mbps_mobile",
                ],
                build=build_mobile,
                logger=logger,
                phase=f"mobile-{year}",
            )
            mobile_rows.append(mobile_grouped)

        fixed_output = pd.concat(fixed_rows, ignore_index=True)
        mobile_output = pd.concat(mobile_rows, ignore_index=True)
        output = fixed_output.merge(
            mobile_output,
            on=[ctx.config.admin1, ctx.config.admin2, "year"],
            how="outer",
        )
        destination = ctx.output(f"{ctx.config.iso3}_internet.csv")
        write_frame_csv_atomic(output, destination)
        logger.info(
            "internet rows=%d",
            len(output),
            extra={"domain": "internet", "path": str(destination)},
        )
