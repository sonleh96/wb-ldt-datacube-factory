from __future__ import annotations

import hashlib
import json
import logging
from pathlib import Path
from typing import Any

from ...checkpoint_utils import write_frame_csv_atomic
from ...context import RunContext
from ...geo import load_admin2
from ...io_utils import require_files
from ...logging_utils import logged_action


_ALGORITHM_VERSION = 2


def _file_signature(path: Path) -> dict[str, Any]:
    stat = path.stat()
    return {
        "path": str(path.resolve()),
        "size": stat.st_size,
        "mtime_ns": stat.st_mtime_ns,
    }


def _input_fingerprint(
    *,
    lines_path: Path,
    raster_path: Path,
    label: str,
    threshold: float,
    batch_size: int,
    admin1_column: str,
    admin2_column: str,
) -> str:
    payload = {
        "algorithm_version": _ALGORITHM_VERSION,
        "lines": _file_signature(lines_path),
        "raster": _file_signature(raster_path),
        "label": label,
        "threshold": threshold,
        "batch_size": batch_size,
        "admin1_column": admin1_column,
        "admin2_column": admin2_column,
    }
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _write_parquet_atomic(frame: Any, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f"{path.stem}.tmp{path.suffix}")
    frame.to_parquet(temporary, index=False)
    temporary.replace(path)


def _line_parquet_metadata(path: Path, admin1_column: str, admin2_column: str):
    import pyarrow.parquet as pq

    required = [admin1_column, admin2_column, "length_km", "geometry"]
    parquet = pq.ParquetFile(path)
    available = set(parquet.schema.names)
    missing = [column for column in required if column not in available]
    if missing:
        raise ValueError(
            f"Flood transport checkpoint is missing required columns {missing}: {path}"
        )
    metadata = parquet.schema_arrow.metadata or {}
    try:
        geo = json.loads(metadata[b"geo"])
        geometry_column = geo["primary_column"]
        crs = geo["columns"][geometry_column].get("crs")
    except (KeyError, TypeError, json.JSONDecodeError) as exc:
        raise ValueError(f"Flood transport input has invalid GeoParquet metadata: {path}") from exc
    return parquet, required, crs


def _record_batch_to_lines(batch: Any, *, crs: Any):
    import geopandas as gpd

    frame = batch.to_pandas()
    geometry = gpd.GeoSeries.from_wkb(frame.pop("geometry"), crs=crs)
    return gpd.GeoDataFrame(frame, geometry=geometry, crs=crs)


def _single_cell_depths(
    geometries: Any, raster_array: Any, affine: Any, nodata: float | None
) -> tuple[Any, Any]:
    """Return positions and cell values only for geometries strictly inside one cell.

    This is an exact fast path for north-up rasters. Geometries touching a cell
    edge, empty/degenerate geometries, rotated rasters, and out-of-raster
    geometries are deliberately left to rasterstats.
    """
    import numpy as np
    import shapely

    if not math_isclose_zero(affine.b) or not math_isclose_zero(affine.d):
        return np.array([], dtype="int64"), np.array([], dtype="float64")

    bounds = geometries.bounds
    inverse = ~affine
    x0 = inverse.a * bounds["minx"].to_numpy() + inverse.c
    x1 = inverse.a * bounds["maxx"].to_numpy() + inverse.c
    y0 = inverse.e * bounds["miny"].to_numpy() + inverse.f
    y1 = inverse.e * bounds["maxy"].to_numpy() + inverse.f
    column_low = np.minimum(x0, x1)
    column_high = np.maximum(x0, x1)
    row_low = np.minimum(y0, y1)
    row_high = np.maximum(y0, y1)
    epsilon = 1e-9
    finite = np.isfinite(column_low + column_high + row_low + row_high)
    columns = np.floor(np.where(finite, column_low, -1)).astype("int64")
    rows = np.floor(np.where(finite, row_low, -1)).astype("int64")
    nondegenerate = shapely.length(geometries.array) > 0
    contained = (
        finite
        & nondegenerate
        & (column_low > columns + epsilon)
        & (column_high < columns + 1 - epsilon)
        & (row_low > rows + epsilon)
        & (row_high < rows + 1 - epsilon)
        & (rows >= 0)
        & (columns >= 0)
        & (rows < raster_array.shape[0])
        & (columns < raster_array.shape[1])
    )
    positions = np.flatnonzero(contained)
    values = np.asarray(raster_array[rows[positions], columns[positions]], dtype="float64")
    if nodata is not None:
        if np.isnan(nodata):
            values[np.isnan(values)] = np.nan
        else:
            values[values == nodata] = np.nan
    return positions, values


def math_isclose_zero(value: float, tolerance: float = 1e-12) -> bool:
    return abs(float(value)) <= tolerance


def _summarize_line_risk(
    *,
    lines_path: Path,
    raster_path: Path,
    raster_array: Any,
    affine: Any,
    nodata: float | None,
    admin1_column: str,
    admin2_column: str,
    output_name: str,
    label: str,
    threshold: float,
    batch_size: int,
    state_dir: Path,
    resume: bool,
    logger: logging.Logger,
) -> Any:
    import numpy as np
    import pandas as pd
    from rasterstats import zonal_stats

    parquet, required_columns, crs = _line_parquet_metadata(
        lines_path, admin1_column, admin2_column
    )
    total = int(parquet.metadata.num_rows)
    fingerprint = _input_fingerprint(
        lines_path=lines_path,
        raster_path=raster_path,
        label=label,
        threshold=threshold,
        batch_size=batch_size,
        admin1_column=admin1_column,
        admin2_column=admin2_column,
    )
    checkpoint_dir = state_dir / f"{label}-{fingerprint[:16]}"
    checkpoint_dir.mkdir(parents=True, exist_ok=True)
    logger.info(
        "%s loaded rows=%d batch_size=%d checkpoint=%s",
        label,
        total,
        batch_size,
        checkpoint_dir,
        extra={"action": "load_lines", "domain": "flood", "phase": label},
    )

    ranges = [(start, min(start + batch_size, total)) for start in range(0, total, batch_size)]
    checkpoints = [
        checkpoint_dir / f"batch-{start:09d}-{stop:09d}.parquet"
        for start, stop in ranges
    ]
    all_cached = resume and all(path.is_file() for path in checkpoints)
    record_batches = None if all_cached else iter(
        parquet.iter_batches(
            batch_size=batch_size,
            columns=required_columns,
            use_threads=True,
        )
    )
    batches = []
    for batch_number, ((start, stop), checkpoint) in enumerate(
        zip(ranges, checkpoints, strict=True), start=1
    ):
        record_batch = next(record_batches) if record_batches is not None else None
        if record_batch is not None and record_batch.num_rows != stop - start:
            raise RuntimeError(
                f"Unexpected Flood batch size at {start}: {record_batch.num_rows} != {stop - start}"
            )
        if resume and checkpoint.is_file():
            grouped = pd.read_parquet(checkpoint)
            logger.info(
                "%s batch=%d reused rows=%d/%d",
                label,
                batch_number,
                stop,
                total,
                extra={
                    "action": "reuse_batch_checkpoint",
                    "domain": "flood",
                    "phase": label,
                    "path": str(checkpoint),
                },
            )
        else:
            if record_batch is None:
                raise RuntimeError("Flood batch reader was not initialized")
            batch = _record_batch_to_lines(record_batch, crs=crs)
            fast_positions, fast_values = _single_cell_depths(
                batch.geometry, raster_array, affine, nodata
            )
            depths = pd.Series(float("nan"), index=batch.index, dtype="float64")
            if len(fast_positions):
                depths.iloc[fast_positions] = fast_values
            exact_positions = pd.RangeIndex(len(batch)).difference(fast_positions)
            if len(exact_positions):
                exact_values = zonal_stats(
                    batch.geometry.iloc[exact_positions],
                    raster_array,
                    affine=affine,
                    stats=["mean"],
                    nodata=nodata,
                )
                depths.iloc[exact_positions] = np.asarray(
                    [
                        np.nan if item.get("mean") is None else float(item["mean"])
                        for item in exact_values
                    ],
                    dtype="float64",
                )
            at_risk = batch.loc[depths.fillna(0).ge(threshold)]
            grouped = (
                at_risk.groupby([admin1_column, admin2_column], as_index=False)["length_km"]
                .sum()
                .rename(columns={"length_km": output_name})
            )
            if grouped.empty:
                grouped = pd.DataFrame(columns=[admin1_column, admin2_column, output_name])
            _write_parquet_atomic(grouped, checkpoint)
            logger.info(
                "%s batch=%d processed rows=%d/%d at_risk=%d single_cell=%d exact=%d",
                label,
                batch_number,
                stop,
                total,
                len(at_risk),
                len(fast_positions),
                len(exact_positions),
                extra={
                    "action": "process_batch",
                    "domain": "flood",
                    "phase": label,
                    "path": str(checkpoint),
                },
            )
        batches.append(grouped)

    nonempty_batches = [batch for batch in batches if not batch.empty]
    if not nonempty_batches:
        return pd.DataFrame(columns=[admin1_column, admin2_column, output_name])
    return (
        pd.concat(nonempty_batches, ignore_index=True)
        .groupby([admin1_column, admin2_column], as_index=False)[output_name]
        .sum()
    )


def run(ctx: RunContext, logger: logging.Logger) -> None:
    import rasterio

    year = int(ctx.config.data["years"]["transport"])
    static_merge_year = int(ctx.config.data["years"]["static_merge"])
    settings = ctx.config.data.get("processing", {}).get("flood", {})
    threshold = float(settings.get("depth_threshold_m", 0.3))
    batch_size = int(settings.get("batch_size", 50_000))
    resume = bool(settings.get("resume_from_checkpoints", True))
    if batch_size <= 0:
        raise ValueError("processing.flood.batch_size must be greater than zero")

    raster_path = ctx.raw("flood", f"{ctx.config.iso3}_flood.tif")
    roads_path = ctx.config.shape_dir / f"roads_intersect_{year}.parquet"
    rails_path = ctx.config.shape_dir / f"rails_intersect_{year}.parquet"
    require_files([raster_path, roads_path, rails_path], "flood inputs")
    admin2 = load_admin2(ctx.config)

    with logged_action(logger, "process", domain="flood"):
        with rasterio.open(raster_path) as dataset:
            array = dataset.read(1)
            affine = dataset.transform
            nodata = dataset.nodata
            logger.info(
                "flood raster loaded width=%d height=%d nodata=%s",
                dataset.width,
                dataset.height,
                nodata,
                extra={
                    "action": "load_raster",
                    "domain": "flood",
                    "path": str(raster_path),
                },
            )

        state_dir = ctx.config.workspace / "state" / "flood"
        roads = _summarize_line_risk(
            lines_path=roads_path,
            raster_path=raster_path,
            raster_array=array,
            affine=affine,
            nodata=nodata,
            admin1_column=ctx.config.admin1,
            admin2_column=ctx.config.admin2,
            output_name="road_length_flood_risk",
            label="roads",
            threshold=threshold,
            batch_size=batch_size,
            state_dir=state_dir,
            resume=resume,
            logger=logger,
        )
        rails = _summarize_line_risk(
            lines_path=rails_path,
            raster_path=raster_path,
            raster_array=array,
            affine=affine,
            nodata=nodata,
            admin1_column=ctx.config.admin1,
            admin2_column=ctx.config.admin2,
            output_name="railway_length_flood_risk",
            label="rails",
            threshold=threshold,
            batch_size=batch_size,
            state_dir=state_dir,
            resume=resume,
            logger=logger,
        )
        output = (
            admin2[[ctx.config.admin1, ctx.config.admin2]]
            .merge(roads, how="left")
            .merge(rails, how="left")
            .fillna(0)
        )
        # The notebook assigns 2025 only so this static risk snapshot joins the
        # 2021-2025 indicator panel. It is not the hazard scenario year.
        output["year"] = static_merge_year
        output["flood_scenario_year"] = 2030
        output["flood_return_period_years"] = 100
        destination = ctx.output(f"{ctx.config.iso3}_flood.csv")
        write_frame_csv_atomic(output, destination)
        logger.info(
            "flood rows=%d",
            len(output),
            extra={"domain": "flood", "path": str(destination)},
        )
