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
from ...logging_utils import logged_action

REQUIRED = {"source_id", "start_time", "lat", "lon", "emissions_quantity"}


def _compatible_sources(root):
    import pandas as pd

    paths = []
    for path in root.rglob("*_emissions_sources_*.csv"):
        if "confidence" in path.name or "ownership" in path.name:
            continue
        columns = set(pd.read_csv(path, nrows=0).columns)
        if REQUIRED.issubset(columns):
            paths.append(path)
    if not paths:
        raise ValueError(f"No compatible Climate TRACE source-emissions CSVs under {root}")
    return sorted(paths)


def _aggregate_sources(
    paths,
    admin2,
    *,
    admin_columns: list[str],
    allowed_years: set[int],
    chunksize: int,
    logger: logging.Logger,
    gas: str,
):
    import geopandas as gpd
    import pandas as pd

    aggregates = []
    rows_read = rows_joined = 0
    dtypes = {"source_id": "string"}
    for file_number, path in enumerate(paths, start=1):
        for chunk_number, frame in enumerate(
            pd.read_csv(
                path,
                usecols=list(REQUIRED),
                dtype=dtypes,
                chunksize=chunksize,
            ),
            start=1,
        ):
            rows_read += len(frame)
            frame["year"] = pd.to_datetime(frame["start_time"], errors="coerce").dt.year
            for column in ("lat", "lon", "emissions_quantity"):
                frame[column] = pd.to_numeric(frame[column], errors="coerce")
            frame = frame[
                frame["year"].isin(allowed_years)
                & frame["lat"].notna()
                & frame["lon"].notna()
                & frame["emissions_quantity"].notna()
            ].copy()
            if frame.empty:
                continue
            points = gpd.GeoDataFrame(
                frame[["year", "emissions_quantity"]],
                geometry=gpd.points_from_xy(frame["lon"], frame["lat"]),
                crs="EPSG:4326",
            )
            joined = gpd.sjoin(points, admin2, predicate="within", how="inner")
            rows_joined += len(joined)
            if not joined.empty:
                aggregates.append(
                    joined.groupby([*admin_columns, "year"], as_index=False)[
                        "emissions_quantity"
                    ].sum()
                )
            logger.info(
                "emissions chunk processed gas=%s file=%d/%d chunk=%d rows_read=%d rows_joined=%d",
                gas,
                file_number,
                len(paths),
                chunk_number,
                rows_read,
                rows_joined,
                extra={"action": "aggregate_chunk", "domain": "emissions", "phase": gas, "path": str(path)},
            )
    if not aggregates:
        return pd.DataFrame(columns=[*admin_columns, "year", "emissions_quantity"])
    return (
        pd.concat(aggregates, ignore_index=True)
        .groupby([*admin_columns, "year"], as_index=False)["emissions_quantity"]
        .sum()
    )


def run(ctx: RunContext, logger: logging.Logger) -> None:
    import pandas as pd

    admin2 = load_admin2(ctx.config).to_crs("EPSG:4326")
    admin_columns = [ctx.config.admin1, ctx.config.admin2]
    admin2 = admin2[[*admin_columns, "geometry"]]
    years = set(ctx.config.years("indicators"))
    settings = ctx.config.data.get("processing", {}).get("emissions", {})
    chunksize = int(settings.get("chunksize", 250_000))
    state_dir = ctx.config.workspace / "state" / "emissions"
    boundary_signature = path_signature(
        ctx.config.boundary_path("admin2"), shapefile_family=True
    )
    with logged_action(logger, "process", domain="emissions"):
        outputs = []
        for gas, column in (("CO2", "co2_emissions_quantity"), ("CH4", "ch4_emissions_quantity")):
            root = ctx.raw(f"climate_trace_{ctx.config.iso3}_{gas}")
            paths = _compatible_sources(root)
            checkpoint = state_dir / f"{gas.lower()}_admin_year.parquet"
            manifest = checkpoint.with_suffix(".manifest.json")
            expected = {
                "algorithm": "climate-trace-stream-aggregate-v2",
                "gas": gas,
                "years": sorted(years),
                "boundary": boundary_signature,
                "sources": [path_signature(path) for path in paths],
            }
            if checkpoint_matches(checkpoint, manifest, expected):
                grouped = pd.read_parquet(checkpoint)
                logger.info(
                    "reused emissions checkpoint gas=%s rows=%d",
                    gas,
                    len(grouped),
                    extra={"action": "checkpoint_reuse", "domain": "emissions", "phase": gas, "path": str(checkpoint)},
                )
            else:
                grouped = _aggregate_sources(
                    paths,
                    admin2,
                    admin_columns=admin_columns,
                    allowed_years=years,
                    chunksize=chunksize,
                    logger=logger,
                    gas=gas,
                )
                write_frame_parquet_atomic(grouped, checkpoint)
                write_checkpoint_manifest(manifest, expected, rows=len(grouped))
            grouped = grouped.rename(columns={"emissions_quantity": column})
            outputs.append(grouped)
        output = outputs[0].merge(outputs[1], on=[*admin_columns, "year"], how="outer")
        write_frame_csv_atomic(output, ctx.output(f"{ctx.config.iso3}_emissions.csv"))
        logger.info("emissions rows=%d", len(output), extra={"domain": "emissions"})
