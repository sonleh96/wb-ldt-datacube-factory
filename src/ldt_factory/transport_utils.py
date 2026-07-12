from __future__ import annotations

import json
import logging
from pathlib import Path

from .checkpoint_utils import (
    checkpoint_matches,
    path_signature,
    write_checkpoint_manifest,
    write_frame_parquet_atomic,
)


def read_transport_source(
    path: Path,
    *,
    allowed_classes: set[str] | None = None,
):
    """Read only transport fields needed downstream, using Arrow and OGR filtering."""
    import geopandas as gpd
    import pyogrio

    available = set(pyogrio.read_info(path)["fields"])
    columns = [name for name in ("osm_id", "fclass", "name", "ref") if name in available]
    where = None
    if allowed_classes:
        quoted = ", ".join(f"'{value}'" for value in sorted(allowed_classes))
        where = f"fclass IN ({quoted})"
    return gpd.read_file(path, columns=columns, where=where, use_arrow=True)


def assign_and_clip_lines(
    lines,
    admin2,
    *,
    admin1_column: str,
    admin2_column: str,
    logger: logging.Logger | None = None,
    label: str = "transport",
):
    """Assign contained lines cheaply and clip only boundary-crossing lines."""
    import geopandas as gpd
    import numpy as np
    import pandas as pd
    import shapely

    if lines.crs != admin2.crs:
        lines = lines.to_crs(admin2.crs)
    lines = lines.reset_index(drop=True).copy()
    lines["_source_row"] = np.arange(len(lines), dtype=np.int64)
    districts = admin2[[admin1_column, admin2_column, "geometry"]].reset_index(drop=True)

    contained = gpd.sjoin(lines, districts, how="inner", predicate="within")
    contained_ids = contained["_source_row"].unique()
    contained = contained.drop(columns=["index_right"], errors="ignore")

    crossing_input = lines[~lines["_source_row"].isin(contained_ids)].copy()
    if logger:
        logger.info(
            "%s fast assignment contained_rows=%d boundary_candidate_rows=%d",
            label,
            len(contained),
            len(crossing_input),
            extra={"action": f"assign_{label}", "domain": "transport"},
        )

    if crossing_input.empty:
        result = contained
    else:
        candidates = gpd.sjoin(crossing_input, districts, how="inner", predicate="intersects")
        if candidates.empty:
            result = contained
        else:
            candidates = candidates.reset_index(drop=True)
            right_geometry = districts.geometry.iloc[
                candidates["index_right"].to_numpy()
            ].reset_index(drop=True)
            clipped_geometry = shapely.intersection(
                candidates.geometry.array,
                right_geometry.array,
            )
            candidates = candidates.drop(columns=["index_right"], errors="ignore")
            candidates = gpd.GeoDataFrame(
                candidates,
                geometry=clipped_geometry,
                crs=lines.crs,
            ).explode(index_parts=False, ignore_index=True)
            candidates = candidates[
                (~candidates.geometry.is_empty)
                & candidates.geometry.geom_type.isin(
                    ["LineString", "MultiLineString", "LinearRing"]
                )
            ].copy()
            result = pd.concat([contained, candidates], ignore_index=True)

    result = gpd.GeoDataFrame(result, geometry="geometry", crs=lines.crs)
    result = result.drop(columns=["_source_row"], errors="ignore")
    return result.reset_index(drop=True)


def add_length_km(lines, metric_crs: str):
    """Calculate projected length without replacing the original geometries."""
    result = lines.copy()
    result["length_km"] = lines.to_crs(metric_crs).geometry.length.to_numpy() / 1000.0
    return result


def validate_checkpoint(frame, *, admin1_column: str, admin2_column: str, path: Path) -> None:
    required = {admin1_column, admin2_column, "length_km", "geometry"}
    missing = required - set(frame.columns)
    if missing:
        raise ValueError(f"Transport checkpoint {path} is missing columns: {sorted(missing)}")
    if frame.crs is None:
        raise ValueError(f"Transport checkpoint {path} has no CRS")


def checkpoint_manifest_path(path: Path) -> Path:
    return path.with_suffix(".manifest.json")


def transport_checkpoint_inputs(
    *,
    source: Path,
    boundary: Path,
    allowed_classes: set[str] | None,
    metric_crs: str,
) -> dict:
    return {
        "algorithm": "transport-assign-clip-v3",
        "source": path_signature(source, shapefile_family=True),
        "boundary": path_signature(boundary, shapefile_family=True),
        "allowed_classes": sorted(allowed_classes) if allowed_classes else None,
        "metric_crs": metric_crs,
    }


def validate_checkpoint_file(
    path: Path,
    *,
    admin1_column: str,
    admin2_column: str,
    expected_inputs: dict,
) -> int:
    """Validate a GeoParquet checkpoint without decoding all geometries."""
    import pyarrow.parquet as pq

    manifest = checkpoint_manifest_path(path)
    if not checkpoint_matches(path, manifest, expected_inputs):
        raise ValueError(f"Transport checkpoint fingerprint is stale or missing: {path}")
    parquet = pq.ParquetFile(path)
    required = {admin1_column, admin2_column, "length_km", "geometry"}
    missing = required - set(parquet.schema.names)
    if missing:
        raise ValueError(f"Transport checkpoint {path} is missing columns: {sorted(missing)}")
    metadata = parquet.schema_arrow.metadata or {}
    try:
        geo = json.loads(metadata[b"geo"])
        primary = geo["primary_column"]
        crs = geo["columns"][primary].get("crs")
    except (KeyError, TypeError, json.JSONDecodeError) as exc:
        raise ValueError(f"Transport checkpoint {path} has invalid GeoParquet metadata") from exc
    if crs is None:
        raise ValueError(f"Transport checkpoint {path} has no CRS")
    return int(parquet.metadata.num_rows)


def read_checkpoint_totals(path: Path, *, admin1_column: str, admin2_column: str):
    import pandas as pd

    frame = pd.read_parquet(
        path,
        columns=[admin1_column, admin2_column, "length_km"],
    )
    totals = frame.groupby(
        [admin1_column, admin2_column], as_index=False
    )["length_km"].sum()
    return totals, len(frame)


def write_checkpoint(frame, path: Path, *, inputs: dict | None = None) -> None:
    """Write a GeoParquet checkpoint atomically."""
    write_frame_parquet_atomic(frame, path, row_group_size=50_000)
    if inputs is not None:
        write_checkpoint_manifest(
            checkpoint_manifest_path(path),
            inputs,
            rows=len(frame),
            columns=list(frame.columns),
            crs=str(frame.crs),
        )
