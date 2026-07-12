from __future__ import annotations

import logging

from ...checkpoint_utils import (
    checkpoint_matches,
    path_signature,
    write_checkpoint_manifest,
    write_frame_csv_atomic,
    write_frame_parquet_atomic,
)
from ...context import RunContext
from ...geo import load_admin2
from ...io_utils import require_files
from ...logging_utils import logged_action


def _event_count(series, threshold_kelvin: float, consecutive_days: int) -> int:
    count = run = 0
    for value in series:
        if value > threshold_kelvin:
            run += 1
            if run == consecutive_days:
                count += 1
                run = 0
        else:
            run = 0
    return count


def _has_event(series, threshold_kelvin: float, consecutive_days: int) -> int:
    """Return as soon as one qualifying event exists.

    The published indicator is binary spatial exposure, so computing all event
    counts performs unnecessary work while producing the same risk mask.
    """
    run = 0
    for value in series:
        if value > threshold_kelvin:
            run += 1
            if run == consecutive_days:
                return 1
        else:
            run = 0
    return 0


def _event_detector():
    try:
        from numba import njit

        return njit(nogil=True)(_has_event)
    except ImportError:
        return _has_event


def _edges(values):
    import numpy as np

    values = np.asarray(values, dtype=float)
    if len(values) < 2:
        raise ValueError("Heatwave grid must have at least two cells per spatial dimension")
    mids = (values[:-1] + values[1:]) / 2
    return np.concatenate(([values[0] - (mids[0] - values[0])], mids, [values[-1] + (values[-1] - mids[-1])]))


def run(ctx: RunContext, logger: logging.Logger) -> None:
    # Load the NetCDF C extension before GDAL-backed modules on Windows; see
    # the matching extraction import-order note.
    import netCDF4  # noqa: F401
    import geopandas as gpd
    import numpy as np
    import pandas as pd
    import shapely
    import xarray as xr

    source = ctx.config.source("heatwaves")
    files = sorted(ctx.raw("heatwaves").glob(f"*_{ctx.config.iso3}.nc"))
    if not files:
        raise FileNotFoundError("No clipped heatwave netCDFs were found")
    year = int(ctx.config.data["years"]["transport"])
    roads_path = ctx.config.shape_dir / f"roads_intersect_{year}.parquet"
    rails_path = ctx.config.shape_dir / f"rails_intersect_{year}.parquet"
    require_files([roads_path, rails_path], "heatwave transport inputs")
    admin2 = load_admin2(ctx.config)
    admin_columns = [ctx.config.admin1, ctx.config.admin2]
    threshold_kelvin = float(source.get("threshold_c", 40)) + 273.15
    consecutive_days = int(source.get("consecutive_days", 5))
    spatial_chunk = int(source.get("processing_spatial_chunk_cells", 16))
    dask_workers = int(source.get("dask_workers", 2))
    state_dir = ctx.config.workspace / "state" / "heatwaves"
    risk_checkpoint = state_dir / "risk_mask.parquet"
    risk_manifest = risk_checkpoint.with_suffix(".manifest.json")
    risk_inputs = {
        "algorithm": "binary-heatwave-mask-v2",
        "netcdf": [path_signature(path) for path in files],
        "variable": str(source.get("variable", "tasmax")),
        "threshold_kelvin": threshold_kelvin,
        "consecutive_days": consecutive_days,
    }

    with logged_action(logger, "process", domain="heatwaves"):
        if checkpoint_matches(risk_checkpoint, risk_manifest, risk_inputs):
            risk_mask = gpd.read_parquet(risk_checkpoint)
            logger.info(
                "reused heatwave risk mask geometries=%d",
                len(risk_mask),
                extra={"action": "checkpoint_reuse", "domain": "heatwaves", "path": str(risk_checkpoint)},
            )
        else:
            with logged_action(logger, "detect_risk_cells", domain="heatwaves"):
                with xr.open_mfdataset(files, combine="by_coords", chunks={}) as dataset:
                    variable = str(source.get("variable", "tasmax"))
                    if variable not in dataset:
                        candidates = [name for name, value in dataset.data_vars.items() if "time" in value.dims]
                        if len(candidates) != 1:
                            raise ValueError(
                                f"Cannot identify heatwave variable {variable!r}; candidates={candidates}"
                            )
                        variable = candidates[0]
                    data = dataset[variable]
                    x_name = next((name for name in ("x", "lon", "longitude") if name in data.dims), None)
                    y_name = next((name for name in ("y", "lat", "latitude") if name in data.dims), None)
                    if not x_name or not y_name or "time" not in data.dims:
                        raise ValueError(f"Unexpected heatwave dimensions: {data.dims}")
                    data = data.chunk(
                        {"time": -1, x_name: spatial_chunk, y_name: spatial_chunk}
                    )
                    logger.info(
                        "heatwave detection dimensions=%s chunks=%s workers=%d",
                        dict(data.sizes),
                        data.chunksizes,
                        dask_workers,
                        extra={"action": "detect_risk_cells", "domain": "heatwaves"},
                    )
                    detected = xr.apply_ufunc(
                        _event_detector(),
                        data,
                        input_core_dims=[["time"]],
                        output_core_dims=[[]],
                        kwargs={
                            "threshold_kelvin": threshold_kelvin,
                            "consecutive_days": consecutive_days,
                        },
                        vectorize=True,
                        dask="parallelized",
                        dask_gufunc_kwargs={"allow_rechunk": False},
                        output_dtypes=[np.int8],
                    ).compute(scheduler="threads", num_workers=dask_workers)
                    x_edges = _edges(data[x_name].values)
                    y_edges = _edges(data[y_name].values)

                positive_y, positive_x = np.nonzero(np.asarray(detected.values) > 0)
                if len(positive_x):
                    geometries = shapely.box(
                        np.minimum(x_edges[positive_x], x_edges[positive_x + 1]),
                        np.minimum(y_edges[positive_y], y_edges[positive_y + 1]),
                        np.maximum(x_edges[positive_x], x_edges[positive_x + 1]),
                        np.maximum(y_edges[positive_y], y_edges[positive_y + 1]),
                    )
                    union = shapely.union_all(geometries)
                    risk_mask = gpd.GeoDataFrame(
                        {"risk": [1]}, geometry=[union], crs="EPSG:4326"
                    )
                else:
                    risk_mask = gpd.GeoDataFrame(
                        {"risk": pd.Series(dtype="int8")},
                        geometry=gpd.GeoSeries([], crs="EPSG:4326"),
                    )
                write_frame_parquet_atomic(risk_mask, risk_checkpoint)
                write_checkpoint_manifest(
                    risk_manifest,
                    risk_inputs,
                    positive_cells=int(len(positive_x)),
                    geometries=len(risk_mask),
                )
                logger.info(
                    "heatwave mask created positive_cells=%d dissolved_geometries=%d",
                    len(positive_x),
                    len(risk_mask),
                    extra={"action": "write_risk_mask", "domain": "heatwaves", "path": str(risk_checkpoint)},
                )

        def risk_length(path, column):
            lines = gpd.read_parquet(
                path,
                columns=[*admin_columns, "length_km", "geometry"],
            ).to_crs("EPSG:4326")
            if risk_mask.empty or lines.empty:
                return admin2[admin_columns].assign(**{column: 0.0})
            mask_geometry = risk_mask.to_crs(lines.crs).geometry.union_all()
            intersects = shapely.intersects(lines.geometry.array, mask_geometry)
            candidates = lines.loc[intersects].copy()
            if candidates.empty:
                return admin2[admin_columns].assign(**{column: 0.0})
            covered = shapely.covered_by(candidates.geometry.array, mask_geometry)
            candidates[column] = 0.0
            candidates.loc[covered, column] = candidates.loc[covered, "length_km"].to_numpy()
            if (~covered).any():
                partial_geometry = shapely.intersection(
                    candidates.loc[~covered].geometry.array,
                    mask_geometry,
                )
                partial = gpd.GeoSeries(partial_geometry, crs=lines.crs).to_crs(
                    str(
                        ctx.config.data.get("processing", {})
                        .get("transport", {})
                        .get("metric_crs", "EPSG:6933")
                    )
                )
                candidates.loc[~covered, column] = partial.length.to_numpy() / 1000.0
            logger.info(
                "heatwave network filtered source=%s total=%d candidates=%d fully_covered=%d",
                path.name,
                len(lines),
                len(candidates),
                int(covered.sum()),
                extra={"action": "risk_length", "domain": "heatwaves", "path": str(path)},
            )
            return candidates.groupby(admin_columns, as_index=False)[column].sum()

        roads = risk_length(roads_path, "road_length_heatwave_risk")
        rails = risk_length(rails_path, "railway_length_heatwave_risk")
        output = (
            admin2[admin_columns]
            .merge(roads, on=admin_columns, how="left")
            .merge(rails, on=admin_columns, how="left")
            .fillna(0)
        )
        output["year"] = int(ctx.config.data["years"]["static_merge"])
        write_frame_csv_atomic(output, ctx.output(f"{ctx.config.iso3}_heatwaves.csv"))
        logger.info("heatwave rows=%d", len(output), extra={"domain": "heatwaves"})
